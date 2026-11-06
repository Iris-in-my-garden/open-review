"""Review 工作流的执行器。

``ReviewPipeline`` 负责构造客户端、缓存和 retriever，本模块只负责把
``ReviewState`` 跑过一遍有界工作流。拆开之后：

- 配置集中在 :class:`~openreview.review.config.ReviewConfig`；
- 节点逻辑集中在 :class:`ReviewExecutor`；
- ``ReviewPipeline`` 只保留 facade / 生命周期 / 最近一次结果。

执行器每次 run 都新建一个，从 pipeline 读取当前组件。这样测试或调用方在
构造 pipeline 后替换 ``reviewer`` / ``retriever`` 仍然生效。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from openreview.github.client import PullRequestInfo
from openreview.github.pr_url import parse_pr_url
from openreview.repo.local import (
    LocalReviewInput,
    collect_local_changes,
    prepare_local_workspace,
)
from openreview.repo.manager import RepoCommandError
from openreview.retriever.code_context import CodeContext
from openreview.retriever.index import RETRIEVER_VERSION
from openreview.review.assessment import merge_review_results
from openreview.review.cache import PROMPT_VERSION, ReviewCache, fingerprint
from openreview.review.comment_builder import (
    build_review_body,
    build_review_comments,
)
from openreview.review.config import ReviewConfig
from openreview.review.report_writer import ReportWriter, ReviewResult
from openreview.review.token_budget import estimate_tokens, split_parsed_diff
from openreview.review.triage import (
    ALL_ASPECTS,
    TriageDecision,
    build_skipped_result,
    triage,
)
from openreview.review.workflow import (
    BoundedWorkflow,
    BudgetExceeded,
    ReviewBudget,
    ReviewOutcome,
    ReviewState,
    ReviewStatus,
    WorkflowNode,
)

logger = logging.getLogger(__name__)


class ReviewExecutor:
    """把 ``ReviewState`` 跑过完整的 review 工作流。"""

    def __init__(
        self,
        *,
        config: ReviewConfig,
        gh_client,
        repo_mgr,
        diff_parser,
        retriever,
        reviewer,
        verifier,
        review_cache: ReviewCache,
        llm_client,
        budgeted_client,
        debug_sink=None,
    ):
        self.config = config
        self.gh_client = gh_client
        self.repo_mgr = repo_mgr
        self.diff_parser = diff_parser
        self.retriever = retriever
        self.reviewer = reviewer
        self.verifier = verifier
        self.review_cache = review_cache
        self.llm_client = llm_client
        self.budgeted_client = budgeted_client
        self.debug_sink = debug_sink

        self.last_state: Optional[ReviewState] = None
        self.last_result: Optional[ReviewResult] = None
        self.last_outcome: Optional[ReviewOutcome] = None

    # ------------------------------------------------------------------
    # 工作流装配与生命周期
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

    def new_state(
        self,
        pr_url: str,
        output_dir: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        post_comments: bool = False,
        *,
        local_mode: bool = False,
        local_input: Optional[LocalReviewInput] = None,
    ) -> ReviewState:
        config = self.config
        state = ReviewState(
            pr_url=pr_url,
            output_dir=Path(output_dir),
            post_comments=post_comments,
            progress_cb=progress_cb,
            budget=ReviewBudget(
                max_llm_calls=config.budget_calls,
                max_input_tokens=config.budget_tokens,
                max_seconds=config.budget_seconds,
            ),
            local_mode=local_mode,
            local_input=local_input,
        )
        # 每次 run 使用独立预算；客户端在本次 run 内共享同一本账。
        # ReviewOrchestrator 返回前会等待所有 worker 结束，因此复用客户端
        # 不会出现“上一次 run 的超时线程扣本次预算”的竞态。
        self.budgeted_client.bind_budget(state.budget)
        # 记录 run 开始时的累计值，后续用 delta 计算本次真实成本。
        state.usage_start = self._usage_snapshot()
        state.cache_hits_start = int(getattr(self.llm_client, "hits", 0) or 0)
        state.cache_misses_start = int(getattr(self.llm_client, "misses", 0) or 0)
        return state

    def execute(self, state: ReviewState) -> Path:
        try:
            state = self._build_workflow().run(state)
        except Exception:
            outcome = self._compute_outcome(state, failed=True)
            state.outcome = outcome
            self.last_state = state
            self.last_result = state.review_result
            self.last_outcome = outcome
            raise

        self.last_state = state
        self.last_result = state.review_result
        if state.report_path is None:
            outcome = self._compute_outcome(state, failed=True)
            state.outcome = outcome
            self.last_outcome = outcome
            raise RuntimeError("Review workflow finished without a report path.")

        self.last_outcome = state.outcome or self._compute_outcome(state)
        logger.info("Stage timings: %s", state.stages)
        return state.report_path

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

        if self.config.triage_enabled:
            decision = triage(state.parsed_diff)
        else:
            default_aspects = list(getattr(self.reviewer, "aspects", ALL_ASPECTS))
            decision = TriageDecision(
                default_aspects, False, "triage 已关闭，覆盖完整审查范围"
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

        batches = split_parsed_diff(
            state.parsed_diff, self.config.max_batch_tokens
        )
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
                calls_per_batch = self._reviewer_call_count(aspects)
                # 先用“剩余调用数”收窄维度，再检查 token / 时间预算。
                # 多维度模式可以只跑部分维度；单遍模式无法拆分，只能整批跳过。
                remaining_calls = state.budget.remaining_calls()
                if (
                    remaining_calls is not None
                    and remaining_calls < calls_per_batch
                ):
                    if remaining_calls <= 0:
                        state.budget_exhausted = True
                        state.degrade(
                            "[budget] LLM 调用预算已用尽，"
                            f"批次 {index}/{len(batches)} 起未执行审查。"
                        )
                        logger.warning(
                            "LLM call budget exhausted before batch %d/%d",
                            index,
                            len(batches),
                        )
                        break
                    if not self._can_narrow_aspects():
                        state.budget_exhausted = True
                        state.degrade(
                            "[budget] 剩余调用数不足，单遍审查无法拆分维度，"
                            f"批次 {index}/{len(batches)} 起未执行审查。"
                        )
                        logger.warning(
                            "LLM call budget too small for single-pass batch %d/%d",
                            index,
                            len(batches),
                        )
                        break
                    aspects = aspects[:remaining_calls]
                    state.selected_aspects = list(aspects)
                    calls_per_batch = self._reviewer_call_count(aspects)
                    state.add_note(
                        f"[budget] 剩余调用数不足，仅运行 {len(aspects)} 个维度："
                        + ", ".join(aspects)
                    )

                # 先按调用数 + 保守 token 下界做快速检查；真正的 token
                # 扣减由 BudgetedLLMClient 在每次调用前精确执行。
                preflight_tokens = estimated_tokens * max(1, calls_per_batch) + 2000
                if not state.budget.allow(
                    calls=calls_per_batch, tokens=preflight_tokens
                ):
                    state.budget_exhausted = True
                    state.degrade(
                        "[budget] LLM 调用或 token 预算不足，"
                        f"批次 {index}/{len(batches)} 起未执行审查。"
                    )
                    logger.warning(
                        "Budget exhausted before batch %d/%d", index, len(batches)
                    )
                    break

                calls_before = state.budget.used_llm_calls
                try:
                    result = self.reviewer.review(
                        pr_info=state.pr_info,
                        parsed_diff=batch,
                        contexts=contexts,
                        repo_path=state.local_repo_path,
                        aspects=aspects,
                    )
                except BudgetExceeded:
                    state.budget_exhausted = True
                    state.degrade(
                        "[budget] LLM 预算在批次 "
                        f"{index}/{len(batches)} 内耗尽，后续批次未执行。"
                    )
                    logger.warning(
                        "Budget exhausted during batch %d/%d", index, len(batches)
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - 单批失败不应丢掉已完成的批次
                    state.failed_batches += 1
                    state.degrade(
                        f"[error] 批次 {index}/{len(batches)} 审查失败"
                        f"（{type(exc).__name__}），已跳过。"
                    )
                    logger.warning(
                        "Batch %d/%d review failed: %s", index, len(batches), exc
                    )
                    continue
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
                        calls=calls_per_batch, tokens=estimated_tokens
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

        state.batch_results = batch_results
        if not batch_results:
            raise RuntimeError(
                "No review results were produced. "
                "Check the LLM budget or the review cache configuration."
                + (
                    f" ({state.failed_batches} batch(es) failed;"
                    " see review notes.)"
                    if state.failed_batches
                    else ""
                )
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
        if state.failed_batches:
            review_result.review_notes.append(
                f"本次有 {state.failed_batches}/{len(batches)} 个批次审查失败，"
                "结果不完整。"
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
        outcome = self._compute_outcome(state)
        state.outcome = outcome

        if state.cache_hit and result.metrics:
            # 缓存命中时 analyze/verify 节点都被跳过，state 里没有文件、批次、
            # 检索和 token 信息。直接重算会把上一次 run 的指标覆盖成 0，
            # 所以保留缓存里的原始 metrics，只补充本次 cache 命中信息。
            metrics = dict(result.metrics)
            cache = dict(metrics.get("cache") or {})
            cache.update(
                {
                    "review_hit": True,
                    "key": state.cache_key[:16] if state.cache_key else "",
                    "served_from_cache": True,
                    "cache_lookup_seconds": round(
                        float(state.stages.get("cache_lookup", 0.0)), 3
                    ),
                    "current_run_seconds": round(sum(state.stages.values()), 3),
                }
            )
            metrics["cache"] = cache
            metrics["metrics_source"] = "review_cache"
            metrics.update(self._outcome_metrics(state, outcome))
            result.metrics = metrics
        else:
            result.metrics = self._build_metrics(state)
        logger.info("Review metrics: %s", result.metrics)

        # 报告首次写入时 metrics 还没补齐；这里补齐后重新落盘，
        # 让阶段耗时包含报告生成与（可选的）评论回写。
        if state.report_path is not None:
            state.report_path.write_text(
                ReportWriter.render_markdown(result), encoding="utf-8"
            )

        # 只缓存完整结果。PARTIAL 结果如果写入缓存，下一次 run 会直接命中，
        # 把“部分 batch 失败 / 预算耗尽”伪装成完整成功。
        if (
            self.review_cache.enabled
            and not state.cache_hit
            and state.cache_key
            and outcome.ok
        ):
            self.review_cache.set(state.cache_key, result)

        if self.debug_sink is not None:
            self.debug_sink.record_state(state)

    # ------------------------------------------------------------------
    # 证据增强
    # ------------------------------------------------------------------
    def _collect_impact_contexts(self, state: ReviewState) -> List[CodeContext]:
        if not self.config.impact_enabled or state.parsed_diff is None:
            return []
        if state.local_repo_path is None:
            return []

        # 延迟 import：impact analyzer 会加载 tree-sitter，仅需要时才付成本。
        from openreview.review.impact import ImpactAnalyzer

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
        if not self.config.static_analysis_enabled or state.parsed_diff is None:
            return []
        if state.local_repo_path is None:
            return []

        from openreview.review.static_analysis import StaticAnalysisRunner

        with self._timed(state, "static_analysis"):
            report = StaticAnalysisRunner(tools=self.config.static_tools).run(
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
    def _reviewer_call_count(self, aspects: Sequence[str]) -> int:
        """每个 batch 预计消耗的 LLM 调用数。

        single-pass reviewer 显式声明 ``calls_per_review=1``；多维度模式和
        测试里的自定义 reviewer 没有这个属性，按 aspect 数量计算。
        """
        explicit = getattr(self.reviewer, "calls_per_review", None)
        if explicit is not None:
            return max(1, int(explicit))
        return max(1, len(aspects))

    def _can_narrow_aspects(self) -> bool:
        """预算不足时是否可以只跑部分 aspect。"""
        return bool(
            getattr(self.reviewer, "supports_aspect_narrowing", True)
        )

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
            *self.config.cache_key_parts(),
        )

    def _usage_snapshot(self) -> Dict[str, int]:
        """读取客户端当前的累计 usage，作为一次 run 的起点。"""
        usage = getattr(self.llm_client, "usage", {}) or {}
        return {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, (int, float))
        }

    @staticmethod
    def _usage_delta(
        before: Dict[str, int], after: Dict[str, int]
    ) -> Dict[str, int]:
        """计算本次 run 的 usage 增量，避免评测复用 pipeline 时累计污染。"""
        keys = sorted(set(before) | set(after))
        return {
            key: int(after.get(key, 0)) - int(before.get(key, 0))
            for key in keys
        }

    def _build_metrics(self, state: ReviewState) -> Dict[str, object]:
        config = self.config
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
        index_kinds = {
            "symbol",
            "definition",
            "import",
            "dependency",
            "caller",
            "callee",
            "reference",
        }
        index_context_files = {
            context.file_path
            for context in state.all_contexts
            if context.kind in index_kinds
        }
        graph_kinds = {"caller", "callee", "import", "dependency"}
        graph_context_kinds = {
            kind: context_kinds[kind]
            for kind in sorted(graph_kinds)
            if context_kinds.get(kind)
        }
        graph_contexts = sum(graph_context_kinds.values())
        graph_context_files = {
            context.file_path
            for context in state.all_contexts
            if context.kind in graph_kinds
        }
        metrics: Dict[str, object] = {
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
                    round(
                        len(changed_symbols & retrieved_symbols)
                        / len(changed_symbols),
                        3,
                    )
                    if changed_symbols
                    else 0.0
                ),
                "index_context_files": len(index_context_files),
                "context_file_list": sorted(context_files)[:100],
                "index_context_file_list": sorted(index_context_files)[:100],
                "hybrid_enabled": config.hybrid_retrieval_enabled,
                "graph_enabled": config.graph_retrieval_enabled,
                "graph_contexts": graph_contexts,
                "graph_context_kinds": graph_context_kinds,
                "graph_context_files": len(graph_context_files),
                "graph_error": int(state.retrieval_stats.get("graph_error", 0)),
                "stats": dict(state.retrieval_stats),
            },
            "batches": len(state.batches),
            "reviewer_mode": config.reviewer_mode,
            "aspects": len(state.selected_aspects),
            "llm_calls": state.budget.used_llm_calls,
            "estimated_patch_tokens": patch_tokens,
            "estimated_context_tokens": context_tokens,
            "triage": {
                "enabled": config.triage_enabled,
                "skipped_llm": state.skipped_llm,
                "reason": state.triage_reason,
                "aspects": list(state.selected_aspects),
            },
            "impact": {
                "enabled": config.impact_enabled,
                "findings": state.impact_count,
            },
            "static_analysis": {
                "enabled": config.static_analysis_enabled,
                "findings": state.static_count,
            },
            "validation": dict(state.validation_stats),
            "verifier": dict(state.verifier_stats),
            "cache": {
                "review_hit": state.cache_hit,
                "key": state.cache_key[:16] if state.cache_key else "",
                "llm_hits": max(
                    0,
                    int(getattr(self.llm_client, "hits", 0) or 0)
                    - state.cache_hits_start,
                ),
                "llm_misses": max(
                    0,
                    int(getattr(self.llm_client, "misses", 0) or 0)
                    - state.cache_misses_start,
                ),
            },
            "budget": {
                "max_llm_calls": state.budget.max_llm_calls,
                "used_llm_calls": state.budget.used_llm_calls,
                "max_input_tokens": state.budget.max_input_tokens,
                "used_input_tokens": state.budget.used_input_tokens,
                "max_seconds": state.budget.max_seconds,
                "elapsed_seconds": round(state.budget.elapsed(), 3),
            },
            "llm_usage": self._usage_delta(
                state.usage_start, self._usage_snapshot()
            ),
        }
        metrics.update(
            self._outcome_metrics(
                state, state.outcome or self._compute_outcome(state)
            )
        )
        return metrics

    @staticmethod
    def _outcome_metrics(
        state: ReviewState, outcome: ReviewOutcome
    ) -> Dict[str, object]:
        return {
            "outcome": outcome.as_dict(),
            "failed_batches": state.failed_batches,
            "budget_exhausted": state.budget_exhausted,
            "degraded_reasons": list(state.degraded_reasons),
        }

    def _compute_outcome(
        self, state: ReviewState, *, failed: bool = False
    ) -> ReviewOutcome:
        if failed:
            status = ReviewStatus.FAILED
        elif state.skipped_llm:
            status = ReviewStatus.SKIPPED
        elif state.failed_batches or state.budget_exhausted:
            status = ReviewStatus.PARTIAL
        else:
            status = ReviewStatus.COMPLETED
        return ReviewOutcome(
            status=status,
            report_path=state.report_path,
            failed_batches=state.failed_batches,
            total_batches=len(state.batches),
            budget_exhausted=state.budget_exhausted,
            degraded_reasons=list(state.degraded_reasons),
        )

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
