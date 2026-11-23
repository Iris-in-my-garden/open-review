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

import inspect
import json
import logging
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from openreview.domain.models import PullRequestInfo, ReviewResult
from openreview.domain.policies import merge_review_results
from openreview.github.pr_url import parse_pr_url
from openreview.repo.local import (
    LocalReviewInput,
    collect_local_changes,
    prepare_local_workspace,
)
from openreview.repo.manager import RepoCommandError
from openreview.retriever.code_context import context_identity
from openreview.retriever.context_planner import plan_contexts
from openreview.review.cache import ReviewCache
from openreview.review.comment_builder import (
    build_review_body,
    build_review_comments,
    select_inline_issues,
)
from openreview.review.config import ReviewConfig
from openreview.review.context_filter import filter_redundant_base_contexts
from openreview.review.file_filter import filter_parsed_diff, summarize_skipped
from openreview.review.report_writer import ReportWriter
from openreview.review.token_budget import estimate_tokens
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
    WorkflowNode,
)
from openreview.services.batch_planner import BatchPlanner
from openreview.services.cache_key import CacheKeyBuilder
from openreview.services.evidence_pipeline import EvidencePipeline
from openreview.services.metrics_collector import MetricsCollector
from openreview.services.outcome_resolver import OutcomeResolver

logger = logging.getLogger(__name__)

# 与 ``ReviewExecutor._build_workflow`` 的节点顺序保持一致，避免 CLI
# 显示固定的 ``[step/8]`` 而实际工作流有 13 个节点。
_PROGRESS_STEPS = {
    "parse_url": 1,
    "fetch_pr": 2,
    "load_local": 3,
    "cache_lookup": 4,
    "prepare_repo": 5,
    "prepare_local": 6,
    "parse_diff": 7,
    "triage": 8,
    "analyze": 9,
    "verify": 10,
    "write_report": 11,
    "post_comments": 12,
    "finalize": 13,
}


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
        run_id: Optional[str] = None,
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
        self.run_id = run_id

        self.cache_key_builder = CacheKeyBuilder(config)
        self.metrics_collector = MetricsCollector(config)
        self.evidence_pipeline = EvidencePipeline(config)
        self.batch_planner = BatchPlanner(config)
        self.outcome_resolver = OutcomeResolver()

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
        run_id: Optional[str] = None,
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
                max_output_tokens=config.budget_output_tokens,
                max_seconds=config.budget_seconds,
            ),
            local_mode=local_mode,
            local_input=local_input,
            run_id=run_id or self.run_id or uuid.uuid4().hex,
            progress_total=len(self._build_workflow().nodes),
        )
        # 每次 run 使用独立预算；executor 持有本次 run 专属的 budgeted client。
        bind_budget = getattr(self.budgeted_client, "bind_budget", None)
        if callable(bind_budget):
            bind_budget(state.budget)
        # 保留累计 usage 快照，供未提供 run-tagged call log 的自定义客户端回退。
        state.usage_start = self._usage_snapshot()
        state.cache_hits_start = int(getattr(self.llm_client, "hits", 0) or 0)
        state.cache_misses_start = int(getattr(self.llm_client, "misses", 0) or 0)
        return state

    def execute(self, state: ReviewState) -> Path:
        try:
            state = self._build_workflow().run(state)
        except Exception as exc:
            node = next(
                (
                    event.node
                    for event in reversed(state.trace)
                    if event.status == "error"
                ),
                "workflow",
            )
            state.record_error(node, exc)
            outcome = self.outcome_resolver.resolve(state, failed=True)
            state.outcome = outcome
            metrics: Dict[str, Any]
            try:
                metrics = dict(
                    self.metrics_collector.build(
                        state, self.llm_client, outcome
                    )
                )
            except Exception:
                logger.exception(
                    "Failed to collect full metrics for failed review run %s",
                    state.run_id,
                )
                metrics = dict(state.metrics())
                metrics.update(
                    {
                        "outcome": outcome.as_dict(),
                        "failed_batches": state.failed_batches,
                        "budget_exhausted": state.budget_exhausted,
                        "degraded_reasons": list(state.degraded_reasons),
                        "errors": list(state.errors),
                    }
                )

            state.metrics_snapshot = dict(metrics)
            if state.review_result is not None:
                state.review_result.metrics = dict(metrics)
            if state.report_path is not None and state.review_result is not None:
                try:
                    state.report_path.write_text(
                        ReportWriter.render_markdown(state.review_result),
                        encoding="utf-8",
                    )
                except Exception:
                    logger.exception(
                        "Failed to update failed review report %s",
                        state.report_path,
                    )
            try:
                state.output_dir.mkdir(parents=True, exist_ok=True)
                metrics_path = (
                    state.output_dir / f"run_{state.run_id}_metrics.json"
                )
                metrics_path.write_text(
                    json.dumps(metrics, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                logger.exception(
                    "Failed to persist metrics for failed review run %s",
                    state.run_id,
                )
            if self.debug_sink is not None:
                self.debug_sink.record_state(state)
            self.last_state = state
            self.last_result = state.review_result
            self.last_outcome = outcome
            raise
        finally:
            self._release_worktrees(state)

        self.last_state = state
        self.last_result = state.review_result
        if state.report_path is None:
            outcome = self.outcome_resolver.resolve(state, failed=True)
            state.outcome = outcome
            self.last_outcome = outcome
            raise RuntimeError("Review workflow finished without a report path.")

        self.last_outcome = state.outcome or self.outcome_resolver.resolve(state)
        logger.info("Stage timings: %s", state.stages)
        return state.report_path

    def _release_worktrees(self, state: ReviewState) -> None:
        """Release per-run git worktrees; shared checkouts are left alone."""
        if state.local_mode or state.pr_ref is None:
            return
        release = getattr(self.repo_mgr, "release_worktree", None)
        if not callable(release):
            return
        for path in (state.base_worktree, state.pr_worktree):
            if path is None:
                continue
            try:
                release(state.pr_ref.owner, state.pr_ref.repo, path)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask result
                logger.warning("Failed to release worktree %s: %s", path, exc)

    # ------------------------------------------------------------------
    # 节点实现
    # ------------------------------------------------------------------
    def _node_parse_url(self, state: ReviewState) -> None:
        self._log(state, "parse_url", "Parsing PR URL...")
        with self._timed(state, "parse_url"):
            state.pr_ref = parse_pr_url(state.pr_url)

    def _node_fetch_pr(self, state: ReviewState) -> None:
        if state.pr_ref is None:
            raise RuntimeError("PR ref is missing; parse_url must run first.")
        self._log(state, "fetch_pr", "Fetching PR metadata & changed files...")
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
        self._log(state, "prepare_local", "Preparing local base/head worktrees...")
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
        self._log(state, "prepare_repo", "Preparing local repository cache...")
        with self._timed(state, "prepare_repo"):
            prepare_worktree = getattr(self.repo_mgr, "prepare_pr_worktree", None)
            if callable(prepare_worktree):
                state.pr_worktree = prepare_worktree(
                    state.pr_ref.owner,
                    state.pr_ref.repo,
                    state.pr_ref.pr_number,
                    head_sha=state.pr_info.head_sha,
                    run_id=state.run_id,
                )
                state.local_repo_path = state.pr_worktree
            else:
                state.local_repo_path = self.repo_mgr.prepare_pr_repo(
                    state.pr_ref.owner, state.pr_ref.repo, state.pr_ref.pr_number
                )

            try:
                state.base_path = self._prepare_base_worktree(state)
            except RepoCommandError as exc:
                # base 侧只是增强上下文，失败不应阻断主流程
                state.base_error = exc.stderr
                logger.warning("Failed to prepare base worktree: %s", exc)

    def _prepare_base_worktree(self, state: ReviewState) -> Path:
        assert state.pr_ref is not None and state.pr_info is not None
        prepare_base = self.repo_mgr.prepare_base_worktree
        worktree_name = f"base_{state.pr_info.base_sha[:8]}_{state.run_id[:8]}"
        try:
            supports_name = "worktree_name" in inspect.signature(
                prepare_base
            ).parameters
        except (TypeError, ValueError):
            supports_name = False
        if not supports_name:
            # 兼容旧 RepoManager / 测试替身：使用共享 base worktree。
            return prepare_base(
                state.pr_ref.owner,
                state.pr_ref.repo,
                state.pr_info.base_sha,
            )
        path = prepare_base(
            state.pr_ref.owner,
            state.pr_ref.repo,
            state.pr_info.base_sha,
            worktree_name=worktree_name,
        )
        state.base_worktree = path
        return path

    def _node_parse_diff(self, state: ReviewState) -> None:
        self._log(state, "parse_diff", "Parsing diffs and extracting symbols...")
        with self._timed(state, "parse_diff"):
            state.parsed_diff = self.diff_parser.parse_diff(
                [file.model_dump() for file in state.raw_files]
            )

    def _node_triage(self, state: ReviewState) -> None:
        if state.parsed_diff is None or state.pr_info is None:
            raise RuntimeError("Parsed diff is missing.")

        state.raw_parsed_diff = state.parsed_diff
        if self.config.file_filter_enabled:
            filtered = filter_parsed_diff(state.raw_parsed_diff)
            state.parsed_diff = filtered.diff
            state.skipped_files = [
                decision.as_dict() for decision in filtered.skipped
            ]
            state.file_filter_summary = filtered.skipped_summary()
            if state.skipped_files:
                state.add_note(
                    "[filter] 硬规则跳过 "
                    f"{len(state.skipped_files)} 个低价值文件"
                    f"（{state.file_filter_summary}）"
                )
        else:
            state.skipped_files = []
            state.file_filter_summary = ""

        if not state.parsed_diff.files:
            if state.skipped_files:
                reason = (
                    "硬规则跳过了所有变更文件"
                    f"（{state.file_filter_summary or '低价值文件'}），"
                    "无需运行 LLM 审查"
                )
            else:
                reason = "没有可审查的文件"
            decision = TriageDecision([], True, reason)
        elif self.config.triage_enabled:
            decision = triage(state.parsed_diff)
            if state.skipped_files:
                decision = TriageDecision(
                    list(decision.aspects),
                    decision.skip_llm,
                    f"{decision.reason}；硬规则跳过 "
                    f"{len(state.skipped_files)} 个文件"
                    f"（{state.file_filter_summary}）",
                )
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
            report_diff = state.raw_parsed_diff or state.parsed_diff
            state.review_result = build_skipped_result(
                state.pr_info, report_diff, decision
            )
            if state.skipped_files:
                state.review_result.review_notes.append(
                    "硬规则未送入 LLM 的文件："
                    + summarize_skipped(state.skipped_files)
                )
            logger.info("Triage skipped LLM review: %s", decision.reason)

    def _node_analyze(self, state: ReviewState) -> None:
        if (
            state.parsed_diff is None
            or state.pr_info is None
            or state.local_repo_path is None
        ):
            raise RuntimeError("Repository or parsed diff is missing.")

        batches = self.batch_planner.plan(state, self.retriever)
        state.batches = batches
        self._log(
            state,
            "analyze",
            f"Retrieving code context and reviewing "
            f"{len(state.parsed_diff.files)} files in {len(batches)} batch(es)...",
        )

        impact_contexts, static_contexts = self.evidence_pipeline.collect(state)

        prompt_builder = getattr(self.reviewer, "prompt_builder", None)
        if prompt_builder is not None and hasattr(prompt_builder, "reset_context_stats"):
            prompt_builder.reset_context_stats()

        batch_results: List[ReviewResult] = []
        # 跨 batch 去重只影响 state/metrics/verifier 的“本次唯一上下文”，
        # 不改变每个 batch 自己的 prompt：后一个 batch 仍然需要它自己的上下文。
        seen_context_keys: set[tuple] = set()
        with self._timed(state, "retrieve_and_review"):
            for index, batch in enumerate(batches, start=1):
                self._log(
                    state,
                    "analyze",
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
                    self.evidence_pipeline.extra_contexts(
                        batch,
                        impact_contexts,
                        static_contexts,
                        include_global_impact=index == 1,
                    )
                )
                if prompt_builder is not None and hasattr(
                    prompt_builder, "fully_visible_patch_files"
                ):
                    visible_patch_files = prompt_builder.fully_visible_patch_files(
                        batch
                    )
                    contexts, redundant = filter_redundant_base_contexts(
                        contexts,
                        batch,
                        fully_visible_files=visible_patch_files,
                    )
                    if redundant:
                        state.context_stats["redundant_base_contexts"] = (
                            state.context_stats.get(
                                "redundant_base_contexts", 0
                            )
                            + len(redundant)
                        )
                        state.context_stats["redundant_base_context_tokens"] = (
                            state.context_stats.get(
                                "redundant_base_context_tokens", 0
                            )
                            + sum(
                                estimate_tokens(context.snippet)
                                for context in redundant
                            )
                        )
                contexts = plan_contexts(
                    contexts,
                    self.config.max_context_tokens,
                    stats=state.retrieval_stats,
                )
                state.context_stats["batch_context_occurrences"] = (
                    state.context_stats.get("batch_context_occurrences", 0)
                    + len(contexts)
                )
                unique_contexts = [
                    context
                    for context in contexts
                    if context_identity(context) not in seen_context_keys
                ]
                for context in unique_contexts:
                    seen_context_keys.add(context_identity(context))
                state.context_stats["cross_batch_duplicate_contexts"] = (
                    state.context_stats.get(
                        "cross_batch_duplicate_contexts", 0
                    )
                    + len(contexts)
                    - len(unique_contexts)
                )
                state.all_contexts.extend(unique_contexts)

                estimated_tokens = sum(
                    estimate_tokens(context.snippet) for context in contexts
                ) + sum(estimate_tokens(file.patch or "") for file in batch.files)
                aspects = state.selected_aspects or list(ALL_ASPECTS)
                calls_per_batch = self._reviewer_call_count(aspects)
                # 先用“剩余调用数”收窄维度，再检查 token / 时间预算。
                # 自定义 reviewer 可以只跑部分维度；单遍模式无法拆分，只能整批跳过。
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
                    aspects = self._narrow_aspects(aspects, remaining_calls)
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
                    state.record_error(
                        "analyze.batch", exc, batch=index, degrade=False
                    )
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

        prompt_context_stats = self._collect_prompt_context_stats(state)
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
        batch_validator_kept = sum(
            len(result.potential_issues) for result in batch_results
        )
        state.validation_stats["cross_batch_deduplicated"] = (
            batch_validator_kept - len(review_result.potential_issues)
        )
        state.validation_stats["validator_kept"] = len(
            review_result.potential_issues
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
        omitted = int(prompt_context_stats.get("prompt_contexts_omitted", 0))
        if omitted:
            omitted_tokens = int(
                prompt_context_stats.get("prompt_context_omitted_tokens", 0)
            )
            note = (
                f"有 {omitted} 段检索上下文因 prompt 预算被省略"
                f"（约 {omitted_tokens} tokens），关键证据可能不在本次输入中。"
            )
            review_result.review_notes.append(note)
            state.add_note(f"[retrieval] {note}")
        if state.skipped_files:
            note = (
                f"按硬规则跳过 {len(state.skipped_files)} 个低价值文件"
                f"（{state.file_filter_summary or '见 metrics.filtering'}），"
                "未送入 LLM；完整清单见 metrics.filtering。"
            )
            review_result.review_notes.append(note)
            state.add_note(f"[filter] {note}")
        state.review_result = review_result

    def _collect_prompt_context_stats(self, state: ReviewState) -> Dict[str, int]:
        """把 PromptBuilder 的上下文使用/省略统计并回本次 run。

        检索层可能产出 24 段上下文，但 prompt 预算只装得下一部分；以前这个
        差额只写在 prompt 文本里，Run Metrics 看不到。现在显式记录，便于
        区分“没检索到”和“检索到但被预算丢掉”。
        """
        prompt_builder = getattr(self.reviewer, "prompt_builder", None)
        stats = getattr(prompt_builder, "context_stats", None)
        if not isinstance(stats, dict):
            return {}
        recorded = {
            "prompt_contexts_used": int(stats.get("used_contexts", 0)),
            "prompt_contexts_omitted": int(stats.get("omitted_contexts", 0))
            + int(state.retrieval_stats.get("contexts_omitted_by_priority", 0)),
            "prompt_context_used_tokens": int(
                stats.get("used_context_tokens", 0)
            ),
            "prompt_context_omitted_tokens": int(
                stats.get("omitted_context_tokens", 0)
            )
            + int(
                state.retrieval_stats.get(
                    "context_tokens_omitted_by_priority", 0
                )
            ),
        }
        state.context_stats.update(recorded)
        return recorded

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
        self._log(state, "write_report", "Writing markdown report...")
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
        self._log(state, "post_comments", "Posting review comments to GitHub...")
        with self._timed(state, "post_comments"):
            inline_issues = select_inline_issues(state.review_result)
            comments = build_review_comments(
                state.review_result, inline_issues=inline_issues
            )
            body = build_review_body(
                state.review_result, inline_issues=inline_issues
            )
            upsert = getattr(self.gh_client, "upsert_review", None)
            if callable(upsert):
                result = upsert(
                    state.pr_ref,
                    body=body,
                    comments=comments,
                    event="COMMENT",
                    commit_id=state.pr_info.head_sha,
                )
                deleted = int((result or {}).get("deleted", 0))
                if deleted:
                    state.add_note(
                        f"[comments] 已替换 {deleted} 条旧 review，"
                        f"发布 {len(comments)} 条行内评论。"
                    )
            else:
                # Backward-compatible fallback for tests / custom clients.
                self.gh_client.create_review(
                    state.pr_ref,
                    body=body,
                    comments=comments,
                    event="COMMENT",
                    commit_id=state.pr_info.head_sha,
                )
        logger.info("Posted %d inline comments", len(comments))

    def _node_finalize(self, state: ReviewState) -> None:
        if state.review_result is None:
            raise RuntimeError("Review result is missing; cannot finalize.")
        result = state.review_result
        outcome = self.outcome_resolver.resolve(state)
        state.outcome = outcome

        if state.cache_hit and result.metrics:
            # 缓存命中时 analyze/verify 节点都被跳过，state 里没有文件、批次、
            # 检索和 token 信息。保留缓存里的原始 metrics，只补充本次 run
            # 的 cache 命中信息与 run identity。
            result.metrics = dict(
                self.metrics_collector.cache_hit_metrics(
                    state, result.metrics, outcome, self.llm_client
                )
            )
        else:
            result.metrics = dict(
                self.metrics_collector.build(state, self.llm_client, outcome)
            )
        state.metrics_snapshot = dict(result.metrics)
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
    # 工具方法
    # ------------------------------------------------------------------
    def _reviewer_call_count(self, aspects: Sequence[str]) -> int:
        """每个 batch 预计消耗的 LLM 调用数。

        single-pass reviewer 显式声明 ``calls_per_review=1``；测试里的自定义
        reviewer 没有这个属性，按 aspect 数量计算。
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

    @staticmethod
    def _narrow_aspects(aspects: Sequence[str], remaining_calls: int) -> List[str]:
        """预算不足时优先保留 security。

        ``aspects[:remaining_calls]`` 会按声明顺序把 security 放在最后，
        导致预算紧张时恰好丢掉最不该丢的维度。这里显式按风险优先级排序。
        """
        priority = ["security", "bugs", "performance", "clean_code"]
        ordered = [aspect for aspect in priority if aspect in aspects]
        ordered.extend(aspect for aspect in aspects if aspect not in ordered)
        return ordered[: max(0, remaining_calls)]

    def _cache_key(self, state: ReviewState) -> str:
        return self.cache_key_builder.build(
            state.pr_url, state.pr_info, self.llm_client
        )

    def _usage_snapshot(self) -> Dict[str, int]:
        """读取客户端当前的累计 usage，作为一次 run 的起点。"""
        return self.metrics_collector.usage_snapshot(self.llm_client)

    @staticmethod
    def _log(state: ReviewState, node: str, message: str) -> None:
        step = _PROGRESS_STEPS.get(node)
        if step is not None:
            state.progress_step = step
        if state.progress_cb:
            state.progress_cb(state.progress_step, message)

    @staticmethod
    @contextmanager
    def _timed(state: ReviewState, stage: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            state.stages[stage] = round(time.perf_counter() - start, 3)
