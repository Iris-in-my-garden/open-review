# openreview/services/review_service.py
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from openreview.github.client import GitHubClient, PullRequestInfo
from openreview.github.pr_url import parse_pr_url
from openreview.repo.diff_parser import DiffParser
from openreview.repo.local import (
    LocalReviewInput,
    collect_local_changes,
    prepare_local_workspace,
)
from openreview.repo.manager import RepoCommandError, RepoManager
from openreview.retriever.base import Retriever
from openreview.retriever.code_context import CodeContext
from openreview.retriever.hybrid import HybridRetriever
from openreview.retriever.index import RETRIEVER_VERSION
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.assessment import merge_review_results
from openreview.review.cache import PROMPT_VERSION, ReviewCache, fingerprint
from openreview.review.comment_builder import (
    build_review_body,
    build_review_comments,
)
from openreview.review.finding_validator import FindingValidator
from openreview.review.impact import ImpactAnalyzer
from openreview.review.llm_cache import FileLLMCacheClient
from openreview.review.llm_client import (
    AbstractLLMClient,
    BudgetedLLMClient,
    LLMClient,
    MockLLMClient,
)
from openreview.review.output_parser import OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.report_writer import ReportWriter, ReviewResult
from openreview.review.static_analysis import StaticAnalysisRunner
from openreview.review.token_budget import estimate_tokens, split_parsed_diff
from openreview.review.triage import (
    ALL_ASPECTS,
    TriageDecision,
    build_skipped_result,
    triage,
)
from openreview.review.verifier import FindingVerifier
from openreview.review.workflow import (
    BoundedWorkflow,
    ReviewBudget,
    ReviewState,
    WorkflowNode,
)

logger = logging.getLogger(__name__)


class ReviewPipeline:
    """有界工作流编排：每个节点只做一件事，条件边由代码显式声明。

    这里刻意不引入“自主 Agent”：PR Review 的步骤是稳定的，真正需要动态
    决策的只有 triage、证据增强、verifier 回炉和失败兜底。用 BoundedWorkflow
    把这些决策显式化，既能控制成本，也方便做消融和回放。
    """

    def __init__(
        self,
        mock_llm: bool = False,
        github_token: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        max_batch_tokens: int = 60000,
        max_patch_tokens: int = 48000,
        max_context_tokens: int = 16000,
        max_workers: int = 4,
        aspect_timeout: float = 90.0,
        llm_timeout: float = 60.0,
        llm_max_retries: int = 0,
        temperature: float = 0.0,
        seed: Optional[int] = None,
        hybrid_retrieval_enabled: bool = True,
        index_cache_dir: Optional[str] = None,
        triage_enabled: bool = True,
        impact_enabled: bool = True,
        static_analysis_enabled: bool = False,
        static_tools: Sequence[str] = ("ruff",),
        verifier_enabled: bool = True,
        verify_severities: Sequence[str] = ("critical", "high"),
        budget_calls: int = 0,
        budget_tokens: int = 0,
        budget_seconds: float = 0.0,
        review_cache_enabled: bool = False,
        review_cache_dir: Optional[str] = None,
        llm_cache_dir: Optional[str] = None,
        llm_cache_mode: str = "auto",
    ):
        self.gh_client = GitHubClient(token=github_token)
        self.repo_mgr = RepoManager()
        self.diff_parser = DiffParser()
        # HybridRetriever 以 tree-sitter AST 为主路径，FTS5 符号索引只做增强；
        # 关闭时行为与旧版 TreeSitterLocalRetriever 完全一致，方便做消融。
        self.hybrid_retrieval_enabled = hybrid_retrieval_enabled
        if hybrid_retrieval_enabled:
            self.retriever: Retriever = HybridRetriever(
                index_cache_dir=index_cache_dir
            )
        else:
            self.retriever = TreeSitterLocalRetriever()
        self.max_batch_tokens = max_batch_tokens

        self.triage_enabled = triage_enabled
        self.impact_enabled = impact_enabled
        self.static_analysis_enabled = static_analysis_enabled
        self.static_tools = tuple(static_tools)
        self.verifier_enabled = verifier_enabled
        self.verify_severities = tuple(verify_severities)
        self.budget_calls = budget_calls
        self.budget_tokens = budget_tokens
        self.budget_seconds = budget_seconds

        # 根据标志直接切换实例，不需要用 MagicMock 动态拼装。
        # 注意：GitHub Token 与 LLM API Key 是两套完全独立的凭证。
        effective_llm_timeout = (
            min(llm_timeout, aspect_timeout) if aspect_timeout > 0 else llm_timeout
        )
        base_client: AbstractLLMClient = (
            MockLLMClient()
            if mock_llm
            else LLMClient(
                api_key=llm_api_key,
                model=llm_model,
                base_url=llm_base_url,
                timeout=effective_llm_timeout,
                max_retries=llm_max_retries,
                temperature=temperature,
                seed=seed,
            )
        )
        # 预算包在真实客户端外面：aspect / verifier / retry 的每次调用都扣账。
        self._budgeted_client = BudgetedLLMClient(base_client)
        client: AbstractLLMClient = self._budgeted_client
        # record/replay 放在预算外层：cache hit 不消耗真实调用预算。
        if llm_cache_dir:
            client = FileLLMCacheClient(
                self._budgeted_client, llm_cache_dir, llm_cache_mode  # type: ignore[arg-type]
            )
        self.llm_client = client

        self.reviewer = ReviewOrchestrator(
            prompt_builder=PromptBuilder(
                max_patch_tokens=max_patch_tokens,
                max_context_tokens=max_context_tokens,
            ),
            llm_client=self.llm_client,
            output_parser=OutputParser(),
            validator=FindingValidator(),
            max_workers=max_workers,
            aspect_timeout=aspect_timeout,
        )
        self.verifier = (
            FindingVerifier(
                self.llm_client,
                verify_severities=self.verify_severities,
            )
            if verifier_enabled
            else None
        )
        self.review_cache = ReviewCache(
            review_cache_dir, enabled=review_cache_enabled
        )

        # run()/run_local() 结束后保留最后一次结果，供 CLI 导出 JSON 或评测读取。
        self.last_result: Optional[ReviewResult] = None
        self.last_state: Optional[ReviewState] = None

    def close(self) -> None:
        self.gh_client.close()
        close_retriever = getattr(self.retriever, "close", None)
        if callable(close_retriever):
            close_retriever()

    def __enter__(self) -> "ReviewPipeline":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 工作流装配
    # ------------------------------------------------------------------
    def _build_workflow(self) -> BoundedWorkflow:
        return BoundedWorkflow(
            [
                WorkflowNode(
                    "parse_url",
                    self._node_parse_url,
                    enabled=lambda state: not state.local_mode,
                ),
                WorkflowNode(
                    "fetch_pr",
                    self._node_fetch_pr,
                    enabled=lambda state: not state.local_mode,
                ),
                WorkflowNode(
                    "load_local",
                    self._node_load_local,
                    enabled=lambda state: state.local_mode,
                ),
                WorkflowNode(
                    "cache_lookup",
                    self._node_cache_lookup,
                    enabled=lambda state: self.review_cache.enabled,
                ),
                WorkflowNode(
                    "prepare_repo",
                    self._node_prepare_repo,
                    enabled=lambda state: not state.cache_hit and not state.local_mode,
                ),
                WorkflowNode(
                    "prepare_local",
                    self._node_prepare_local,
                    enabled=lambda state: state.local_mode and not state.cache_hit,
                ),
                WorkflowNode(
                    "parse_diff",
                    self._node_parse_diff,
                    enabled=lambda state: not state.cache_hit,
                ),
                WorkflowNode(
                    "triage",
                    self._node_triage,
                    enabled=lambda state: not state.cache_hit,
                ),
                WorkflowNode(
                    "analyze",
                    self._node_analyze,
                    enabled=lambda state: (
                        not state.cache_hit and not state.skipped_llm
                    ),
                ),
                WorkflowNode(
                    "verify",
                    self._node_verify,
                    enabled=lambda state: (
                        self.verifier is not None
                        and not state.cache_hit
                        and not state.skipped_llm
                        and state.review_result is not None
                    ),
                ),
                WorkflowNode("write_report", self._node_write_report),
                WorkflowNode(
                    "post_comments",
                    self._node_post_comments,
                    enabled=lambda state: (
                        state.post_comments
                        and not state.cache_hit
                        and not state.local_mode
                    ),
                ),
                WorkflowNode("finalize", self._node_finalize),
            ]
        )

    def _new_state(
        self,
        pr_url: str,
        output_dir: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        post_comments: bool = False,
        *,
        local_mode: bool = False,
        local_input: Optional[LocalReviewInput] = None,
    ) -> ReviewState:
        state = ReviewState(
            pr_url=pr_url,
            output_dir=Path(output_dir),
            post_comments=post_comments,
            progress_cb=progress_cb,
            budget=ReviewBudget(
                max_llm_calls=self.budget_calls,
                max_input_tokens=self.budget_tokens,
                max_seconds=self.budget_seconds,
            ),
            local_mode=local_mode,
            local_input=local_input,
        )
        # 每次 run 使用独立预算；客户端在本次 run 内共享同一本账。
        self._budgeted_client.bind_budget(state.budget)
        return state

    def _execute(self, state: ReviewState) -> Path:
        state = self._build_workflow().run(state)
        self.last_state = state
        self.last_result = state.review_result
        if state.report_path is None:
            raise RuntimeError("Review workflow finished without a report path.")
        logger.info("Stage timings: %s", state.stages)
        return state.report_path

    def run(
        self,
        pr_url: str,
        output_dir: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        post_comments: bool = False,
    ) -> Path:
        """运行完整的 PR Review 流程。

        :param progress_cb: 可选的回调函数，用于向外推送进度提示，形如 cb(step_number,
        message)
        :param post_comments: 是否通过 GitHub Review API 发表行内评论
        """
        state = self._new_state(
            pr_url=pr_url,
            output_dir=output_dir,
            progress_cb=progress_cb,
            post_comments=post_comments,
        )
        return self._execute(state)

    def run_local(
        self,
        repo_path: str | Path,
        base_sha: str,
        head_sha: str,
        output_dir: Optional[Path] = None,
        *,
        owner: str = "local",
        repo: str = "repo",
        pr_number: int = 0,
        title: Optional[str] = None,
        body: Optional[str] = None,
        pr_url: Optional[str] = None,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        keep_worktrees: bool = False,
    ) -> Path:
        """不依赖 GitHub API，在本地仓库上跑完整 review。

        适用于代码评测（AACR-Bench 等）和离线回放：输入是仓库路径 +
        base/head commit，pipeline 会创建临时 detached worktree，结束后清理。
        需要 GitHub 写操作的 ``post_comments`` 在本地模式下不生效。
        """
        resolved_repo = Path(repo_path).expanduser().resolve()
        base = base_sha.strip()
        head = head_sha.strip()
        if not base or not head:
            raise ValueError("base_sha and head_sha are required")
        if base == head:
            raise ValueError("base_sha and head_sha are identical; nothing to review")

        synthetic_url = pr_url or (
            f"local://{resolved_repo}@{base[:7]}..{head[:7]}"
        )
        local_input = LocalReviewInput(
            repo_path=resolved_repo,
            base_sha=base,
            head_sha=head,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            title=title,
            body=body,
            pr_url=pr_url,
            keep_worktrees=keep_worktrees,
        )
        state = self._new_state(
            pr_url=synthetic_url,
            output_dir=output_dir or Path("reports"),
            progress_cb=progress_cb,
            local_mode=True,
            local_input=local_input,
        )
        try:
            return self._execute(state)
        finally:
            if state.local_workspace is not None:
                state.local_workspace.cleanup()

    # ------------------------------------------------------------------
    # 节点实现
    # ------------------------------------------------------------------
    def _node_parse_url(self, state: ReviewState) -> None:
        self._log(state, 1, "Parsing PR URL...")
        with self._timed(state, "parse_url"):
            state.pr_ref = parse_pr_url(state.pr_url)

    def _node_fetch_pr(self, state: ReviewState) -> None:
        if state.pr_ref is None:
            raise RuntimeError("PR ref is missing; parse_url must run first.")
        self._log(state, 2, "Fetching PR metadata & changed files...")
        with self._timed(state, "fetch_pr"):
            state.pr_info = self.gh_client.get_pull_request(state.pr_ref)
            state.raw_files = self.gh_client.list_changed_files(state.pr_ref)

    def _node_load_local(self, state: ReviewState) -> None:
        if state.local_input is None:
            raise RuntimeError("local_mode requires local_input.")
        local = state.local_input
        if local.pr_url:
            state.pr_url = local.pr_url
        state.pr_info = PullRequestInfo(
            owner=local.owner,
            repo=local.repo,
            pr_number=local.pr_number,
            title=local.title
            or f"Local review {local.base_sha[:7]}..{local.head_sha[:7]}",
            body=local.body,
            base_branch=local.base_sha[:7],
            head_branch=local.head_sha[:7],
            base_sha=local.base_sha,
            head_sha=local.head_sha,
            html_url=local.pr_url or state.pr_url,
        )

    def _node_prepare_local(self, state: ReviewState) -> None:
        if state.local_input is None:
            raise RuntimeError("local_mode requires local_input.")
        local = state.local_input
        self._log(state, 3, "Preparing local base/head worktrees...")
        with self._timed(state, "prepare_local"):
            workspace = prepare_local_workspace(
                local.repo_path,
                local.base_sha,
                local.head_sha,
                keep=local.keep_worktrees,
            )
            # 先挂到 state 上：即使后面的 diff 生成失败，run_local 的 finally
            # 也能清理 worktree。
            state.local_workspace = workspace
            state.local_repo_path = workspace.head_path
            state.base_path = workspace.base_path
            state.raw_files = collect_local_changes(
                workspace.repo_path, local.base_sha, local.head_sha
            )

    def _node_cache_lookup(self, state: ReviewState) -> None:
        if state.pr_info is None:
            return
        state.cache_key = self._cache_key(state)
        cached = self.review_cache.get(state.cache_key)
        if cached is not None:
            cached.review_notes.append(
                "命中本地 review 缓存（head_sha 未变化），本次未调用 LLM。"
            )
            state.review_result = cached
            state.cache_hit = True
            logger.info("Review cache hit: %s", state.cache_key[:12])

    def _node_prepare_repo(self, state: ReviewState) -> None:
        if state.pr_ref is None or state.pr_info is None:
            raise RuntimeError("PR metadata is missing.")
        self._log(state, 3, "Preparing local repository cache...")
        with self._timed(state, "prepare_repo"):
            state.local_repo_path = self.repo_mgr.prepare_pr_repo(
                state.pr_ref.owner, state.pr_ref.repo, state.pr_ref.pr_number
            )

            try:
                state.base_path = self.repo_mgr.prepare_base_worktree(
                    state.pr_ref.owner, state.pr_ref.repo, state.pr_info.base_sha
                )
            except RepoCommandError as exc:
                # base 侧只是增强上下文，失败不应阻断主流程
                state.base_error = exc.stderr
                logger.warning("Failed to prepare base worktree: %s", exc)

    def _node_parse_diff(self, state: ReviewState) -> None:
        self._log(state, 4, "Parsing diffs and extracting symbols...")
        with self._timed(state, "parse_diff"):
            state.parsed_diff = self.diff_parser.parse_diff(
                [file.model_dump() for file in state.raw_files]
            )

    def _node_triage(self, state: ReviewState) -> None:
        if state.parsed_diff is None or state.pr_info is None:
            raise RuntimeError("Parsed diff is missing.")

        if self.triage_enabled:
            decision = triage(state.parsed_diff)
        else:
            default_aspects = list(getattr(self.reviewer, "aspects", ALL_ASPECTS))
            decision = TriageDecision(
                default_aspects, False, "triage 已关闭，运行完整维度"
            )

        state.selected_aspects = list(decision.aspects)
        state.skipped_llm = decision.skip_llm
        state.triage_reason = decision.reason
        state.add_note(f"[triage] {decision.reason}")

        if decision.skip_llm:
            state.review_result = build_skipped_result(
                state.pr_info, state.parsed_diff, decision
            )
            logger.info("Triage skipped LLM review: %s", decision.reason)

    def _node_analyze(self, state: ReviewState) -> None:
        if (
            state.parsed_diff is None
            or state.pr_info is None
            or state.local_repo_path is None
        ):
            raise RuntimeError("Repository or parsed diff is missing.")

        batches = split_parsed_diff(state.parsed_diff, self.max_batch_tokens)
        state.batches = batches
        self._log(
            state,
            5,
            f"Retrieving code context and reviewing "
            f"{len(state.parsed_diff.files)} files in {len(batches)} batch(es)...",
        )

        impact_contexts = self._collect_impact_contexts(state)
        static_contexts = self._collect_static_contexts(state)

        batch_results: List[ReviewResult] = []
        with self._timed(state, "retrieve_and_review"):
            for index, batch in enumerate(batches, start=1):
                self._log(
                    state,
                    6,
                    f"Running AI Code Reviewer "
                    f"(batch {index}/{len(batches)}, {len(batch.files)} files)...",
                )
                contexts = self.retriever.retrieve(
                    state.local_repo_path, batch, base_path=state.base_path
                )
                # HybridRetriever 的索引命中/构建耗时按批次累加，供评测分析。
                retrieval_stats = getattr(self.retriever, "last_stats", None)
                if retrieval_stats:
                    for key, value in retrieval_stats.items():
                        if isinstance(value, (int, float)):
                            state.retrieval_stats[key] = (
                                state.retrieval_stats.get(key, 0) + value
                            )
                contexts.extend(
                    self._extra_contexts(batch, impact_contexts, static_contexts)
                )
                contexts = _dedupe_contexts(contexts)
                state.all_contexts.extend(contexts)

                estimated_tokens = sum(
                    estimate_tokens(context.snippet) for context in contexts
                ) + sum(estimate_tokens(file.patch or "") for file in batch.files)
                aspects = state.selected_aspects or list(ALL_ASPECTS)
                # 先用“剩余调用数”收窄维度，再检查 token / 时间预算。
                # 这样小预算至少能产出部分维度，而不是整单失败。
                remaining_calls = state.budget.remaining_calls()
                if remaining_calls is not None and remaining_calls < len(aspects):
                    if remaining_calls <= 0:
                        state.add_note(
                            "[budget] LLM 调用预算已用尽，"
                            f"批次 {index}/{len(batches)} 起未执行审查。"
                        )
                        logger.warning(
                            "LLM call budget exhausted before batch %d/%d",
                            index,
                            len(batches),
                        )
                        break
                    aspects = aspects[:remaining_calls]
                    state.selected_aspects = list(aspects)
                    state.add_note(
                        f"[budget] 剩余调用数不足，仅运行 {len(aspects)} 个维度："
                        + ", ".join(aspects)
                    )

                # 先按调用数 + 保守 token 下界做快速检查；真正的 token
                # 扣减由 BudgetedLLMClient 在每次调用前精确执行。
                preflight_tokens = estimated_tokens * max(1, len(aspects)) + 2000
                if not state.budget.allow(
                    calls=len(aspects), tokens=preflight_tokens
                ):
                    state.add_note(
                        "[budget] LLM 调用或 token 预算不足，"
                        f"批次 {index}/{len(batches)} 起未执行审查。"
                    )
                    logger.warning(
                        "Budget exhausted before batch %d/%d", index, len(batches)
                    )
                    break

                calls_before = state.budget.used_llm_calls
                result = self.reviewer.review(
                    pr_info=state.pr_info,
                    parsed_diff=batch,
                    contexts=contexts,
                    repo_path=state.local_repo_path,
                    aspects=aspects,
                )
                # 真实客户端由 BudgetedLLMClient 扣账；FakeReviewer / 自定义
                # reviewer 绕过客户端时，这里补一次，保证预算语义一致。
                # 注意：LLM cache hit 也不会增加 used_llm_calls，但它本来就
                # 不应该消耗预算；因此只有 reviewer 不依赖预算客户端时才补账。
                reviewer_client = getattr(self.reviewer, "llm_client", None)
                reviewer_has_budget = bool(
                    getattr(reviewer_client, "has_budget", False)
                )
                if (
                    state.budget.used_llm_calls == calls_before
                    and not reviewer_has_budget
                ):
                    state.budget.consume(
                        calls=len(aspects), tokens=estimated_tokens
                    )
                batch_results.append(result)

                # 多批次时累加 validator 计数；只保留最后一批会低估失败模式。
                validator = getattr(self.reviewer, "validator", None)
                stats = getattr(validator, "last_stats", None)
                if stats:
                    for key, value in stats.items():
                        state.validation_stats[key] = (
                            state.validation_stats.get(key, 0) + int(value)
                        )

        if not batch_results:
            raise RuntimeError(
                "No review results were produced. "
                "Check the LLM budget or the review cache configuration."
            )

        review_result = (
            batch_results[0]
            if len(batch_results) == 1
            else merge_review_results(batch_results)
        )
        if len(batches) > 1:
            review_result.review_notes.append(
                f"PR 较大，已按 token 预算拆分为 {len(batches)} 个批次审查。"
            )
        if state.base_error:
            review_result.review_notes.append(
                f"base 版本上下文不可用（{state.base_error}），"
                "删除代码的上下文可能不完整。"
            )
        state.review_result = review_result

    def _node_verify(self, state: ReviewState) -> None:
        if (
            self.verifier is None
            or state.review_result is None
            or state.parsed_diff is None
        ):
            return
        with self._timed(state, "verify"):
            state.review_result, stats = self.verifier.verify(
                state.review_result,
                state.parsed_diff,
                state.all_contexts,
                repo_path=state.local_repo_path,
                budget=state.budget,
            )
            state.verifier_stats = stats.as_dict()

    def _node_write_report(self, state: ReviewState) -> None:
        if state.review_result is None:
            raise RuntimeError("Review result is missing; cannot write report.")
        self._log(state, 7, "Writing markdown report...")
        with self._timed(state, "write_report"):
            state.report_path = ReportWriter.write(
                state.review_result, state.output_dir
            )

    def _node_post_comments(self, state: ReviewState) -> None:
        if (
            state.review_result is None
            or state.pr_ref is None
            or state.pr_info is None
        ):
            return
        self._log(state, 8, "Posting review comments to GitHub...")
        with self._timed(state, "post_comments"):
            comments = build_review_comments(state.review_result)
            self.gh_client.create_review(
                state.pr_ref,
                body=build_review_body(state.review_result),
                comments=comments,
                event="COMMENT",
                commit_id=state.pr_info.head_sha,
            )
        logger.info("Posted %d inline comments", len(comments))

    def _node_finalize(self, state: ReviewState) -> None:
        if state.review_result is None:
            raise RuntimeError("Review result is missing; cannot finalize.")
        result = state.review_result

        result.metrics = self._build_metrics(state)
        logger.info("Review metrics: %s", result.metrics)

        # 报告首次写入时 metrics 还没补齐；这里补齐后重新落盘，
        # 让阶段耗时包含报告生成与（可选的）评论回写。
        if state.report_path is not None:
            state.report_path.write_text(
                ReportWriter.render_markdown(result), encoding="utf-8"
            )

        if (
            self.review_cache.enabled
            and not state.cache_hit
            and state.cache_key
        ):
            self.review_cache.set(state.cache_key, result)

    # ------------------------------------------------------------------
    # 证据增强
    # ------------------------------------------------------------------
    def _collect_impact_contexts(self, state: ReviewState) -> List[CodeContext]:
        if not self.impact_enabled or state.parsed_diff is None:
            return []
        if state.local_repo_path is None:
            return []

        with self._timed(state, "impact_analysis"):
            report = ImpactAnalyzer().analyze(
                state.parsed_diff, state.local_repo_path
            )
        state.impact_findings = report.to_notes()
        state.impact_count = len(report.findings)
        if report.findings:
            state.add_note(
                f"[impact] 发现 {len(report.findings)} 处跨文件引用，"
                "已作为上下文注入。"
            )
        return report.to_contexts(state.local_repo_path)

    def _collect_static_contexts(self, state: ReviewState) -> List[CodeContext]:
        if not self.static_analysis_enabled or state.parsed_diff is None:
            return []
        if state.local_repo_path is None:
            return []

        with self._timed(state, "static_analysis"):
            report = StaticAnalysisRunner(tools=self.static_tools).run(
                state.local_repo_path, state.parsed_diff
            )
        state.static_findings = report.to_notes()
        state.static_count = len(report.findings)
        for error in report.errors:
            state.add_note(f"[static] {error}")
        if report.findings:
            state.add_note(
                f"[static] 发现 {len(report.findings)} 个静态分析信号，"
                "已作为上下文注入。"
            )
        return report.to_contexts(state.local_repo_path)

    @staticmethod
    def _extra_contexts(
        batch,
        impact_contexts: List[CodeContext],
        static_contexts: List[CodeContext],
    ) -> List[CodeContext]:
        batch_files = {file.filename for file in batch.files}
        # impact 指向的是“变更文件之外”的引用，全部保留；
        # static 信号只取当前批次涉及的文件，避免重复注入。
        extras = list(impact_contexts)
        extras.extend(
            context
            for context in static_contexts
            if context.file_path in batch_files
        )
        return extras

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _cache_key(self, state: ReviewState) -> str:
        if state.pr_info is None:
            return ""
        return fingerprint(
            state.pr_url,
            state.pr_info.base_sha,
            state.pr_info.head_sha,
            str(getattr(self.llm_client, "model", "unknown")),
            str(getattr(self.llm_client, "temperature", "")),
            str(getattr(self.llm_client, "seed", "")),
            PROMPT_VERSION,
            RETRIEVER_VERSION,
            f"triage={int(self.triage_enabled)}",
            f"impact={int(self.impact_enabled)}",
            f"static={int(self.static_analysis_enabled)}",
            f"verifier={int(self.verifier_enabled)}",
            f"hybrid={int(self.hybrid_retrieval_enabled)}",
        )

    def _build_metrics(self, state: ReviewState) -> Dict[str, object]:
        parsed_diff = state.parsed_diff
        patch_tokens = sum(
            estimate_tokens(file.patch or "")
            for file in (parsed_diff.files if parsed_diff else [])
        )
        context_tokens = sum(
            estimate_tokens(context.snippet) for context in state.all_contexts
        )
        diff_files = parsed_diff.files if parsed_diff else []
        changed_files = {file.filename for file in diff_files}
        context_files = {context.file_path for context in state.all_contexts}
        covered_files = changed_files & context_files
        context_ids = {context.ensure_context_id() for context in state.all_contexts}
        context_kinds: Dict[str, int] = {}
        for context in state.all_contexts:
            context_kinds[context.kind] = context_kinds.get(context.kind, 0) + 1
        changed_symbols = {
            symbol
            for file in diff_files
            for symbol in (file.changed_symbols or [])
            if symbol
        }
        retrieved_symbols = {
            context.symbol for context in state.all_contexts if context.symbol
        }
        # 只有“符号/定义/import/依赖/引用”这类结构化上下文才算索引召回；
        # test/impact/static 不是 FTS 索引的直接产物。
        index_kinds = {"symbol", "definition", "import", "dependency", "reference"}
        index_context_files = {
            context.file_path
            for context in state.all_contexts
            if context.kind in index_kinds
        }
        return {
            "stages_seconds": dict(state.stages),
            "total_seconds": round(sum(state.stages.values()), 3),
            "files": len(diff_files),
            "diff": {
                "files": len(diff_files),
                "additions": sum(file.additions for file in diff_files),
                "deletions": sum(file.deletions for file in diff_files),
                "languages": sorted(
                    {file.language for file in diff_files if file.language}
                ),
            },
            "retrieval": {
                "contexts": len(state.all_contexts),
                "context_files": len(context_files),
                "changed_files": len(changed_files),
                "covered_changed_files": len(covered_files),
                "changed_file_coverage": (
                    round(len(covered_files) / len(changed_files), 3)
                    if changed_files
                    else 0.0
                ),
                "context_tokens": context_tokens,
                "unique_contexts": len(context_ids),
                "duplicate_contexts": len(state.all_contexts) - len(context_ids),
                "context_kinds": context_kinds,
                "changed_symbols": len(changed_symbols),
                "retrieved_symbols": len(retrieved_symbols),
                "symbol_coverage": (
                    round(len(changed_symbols & retrieved_symbols) / len(changed_symbols), 3)
                    if changed_symbols
                    else 0.0
                ),
                "index_context_files": len(index_context_files),
                "context_file_list": sorted(context_files)[:100],
                "index_context_file_list": sorted(index_context_files)[:100],
                "hybrid_enabled": self.hybrid_retrieval_enabled,
                "stats": dict(state.retrieval_stats),
            },
            "batches": len(state.batches),
            "aspects": len(state.selected_aspects),
            "llm_calls": state.budget.used_llm_calls,
            "estimated_patch_tokens": patch_tokens,
            "estimated_context_tokens": context_tokens,
            "triage": {
                "enabled": self.triage_enabled,
                "skipped_llm": state.skipped_llm,
                "reason": state.triage_reason,
                "aspects": list(state.selected_aspects),
            },
            "impact": {
                "enabled": self.impact_enabled,
                "findings": state.impact_count,
            },
            "static_analysis": {
                "enabled": self.static_analysis_enabled,
                "findings": state.static_count,
            },
            "validation": dict(state.validation_stats),
            "verifier": dict(state.verifier_stats),
            "cache": {
                "review_hit": state.cache_hit,
                "key": state.cache_key[:16] if state.cache_key else "",
                "llm_hits": getattr(self.llm_client, "hits", 0),
                "llm_misses": getattr(self.llm_client, "misses", 0),
            },
            "budget": {
                "max_llm_calls": state.budget.max_llm_calls,
                "used_llm_calls": state.budget.used_llm_calls,
                "max_input_tokens": state.budget.max_input_tokens,
                "used_input_tokens": state.budget.used_input_tokens,
                "max_seconds": state.budget.max_seconds,
                "elapsed_seconds": round(state.budget.elapsed(), 3),
            },
            "llm_usage": dict(getattr(self.llm_client, "usage", {})),
        }

    @staticmethod
    def _log(state: ReviewState, step: int, message: str) -> None:
        if state.progress_cb:
            state.progress_cb(step, message)

    @staticmethod
    @contextmanager
    def _timed(state: ReviewState, stage: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            state.stages[stage] = round(time.perf_counter() - start, 3)


def _dedupe_contexts(contexts: List[CodeContext]) -> List[CodeContext]:
    """同一文件、同一片段只保留一次，避免 impact/static 与 AST 检索重复。"""
    seen: set[tuple[str, str, str]] = set()
    result: List[CodeContext] = []
    for context in contexts:
        key = (context.file_path, context.revision, context.snippet)
        if key in seen:
            continue
        seen.add(key)
        result.append(context)
    return result
