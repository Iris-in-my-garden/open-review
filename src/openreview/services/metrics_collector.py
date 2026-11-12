"""Typed run metrics collection.

The executor used to own ~150 lines of metrics dictionary assembly. This
collector keeps the schema in one place, adds ``run_id`` / ``schema_version``
and records structured errors without changing the keys consumed by reports
and evaluation.
"""

from __future__ import annotations

from typing import Any, Dict, cast

from openreview.domain.metrics import (
    CacheMetrics,
    ErrorRecord,
    OutcomeMetrics,
    RunMetrics,
    metric_schema_version,
)
from openreview.domain.versions import ARTIFACT_VERSIONS
from openreview.review.config import ReviewConfig
from openreview.review.token_budget import estimate_tokens
from openreview.review.workflow import ReviewOutcome, ReviewState


class MetricsCollector:
    def __init__(self, config: ReviewConfig):
        self.config = config

    def build(
        self,
        state: ReviewState,
        llm_client: Any,
        outcome: ReviewOutcome,
    ) -> RunMetrics:
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
        metrics: RunMetrics = {
            "schema_version": metric_schema_version(),
            "run_id": state.run_id,
            "versions": ARTIFACT_VERSIONS.as_dict(),
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
            "llm_calls_detail": self._call_details(state, llm_client),
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
            "cache": self._cache_metrics(state, llm_client),
            "budget": {
                "max_llm_calls": state.budget.max_llm_calls,
                "used_llm_calls": state.budget.used_llm_calls,
                "max_input_tokens": state.budget.max_input_tokens,
                "used_input_tokens": state.budget.used_input_tokens,
                "max_output_tokens": state.budget.max_output_tokens,
                "used_output_tokens": state.budget.used_output_tokens,
                "max_seconds": state.budget.max_seconds,
                "elapsed_seconds": round(state.budget.elapsed(), 3),
            },
            "llm_usage": self._usage_delta(
                state.usage_start, self.usage_snapshot(llm_client)
            ),
        }
        metrics.update(self.outcome_metrics(state, outcome))
        return metrics

    def cache_hit_metrics(
        self,
        state: ReviewState,
        cached_metrics: Dict[str, Any],
        outcome: ReviewOutcome,
    ) -> RunMetrics:
        """Preserve cached analysis metrics, refresh run-scoped fields."""
        metrics = dict(cached_metrics)
        cache: CacheMetrics = cast(
            CacheMetrics, dict(metrics.get("cache") or {})
        )
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
        # run identity must describe this run, not the cached one.
        metrics["schema_version"] = metric_schema_version()
        metrics["run_id"] = state.run_id
        metrics["versions"] = ARTIFACT_VERSIONS.as_dict()
        metrics.update(self.outcome_metrics(state, outcome))
        return metrics  # type: ignore[return-value]

    @staticmethod
    def _call_details(state: ReviewState, llm_client: Any) -> list[Dict[str, Any]]:
        """Per-call model/latency/token details for this run only."""
        log = getattr(llm_client, "call_log", []) or []
        start = max(0, int(getattr(state, "llm_call_start", 0) or 0))
        details = [dict(item) for item in log[start:]]
        for item in details:
            item.setdefault("run_id", state.run_id)
        return details

    def _cache_metrics(self, state: ReviewState, llm_client: Any) -> CacheMetrics:
        return {
            "review_hit": state.cache_hit,
            "key": state.cache_key[:16] if state.cache_key else "",
            "llm_hits": max(
                0,
                int(getattr(llm_client, "hits", 0) or 0)
                - state.cache_hits_start,
            ),
            "llm_misses": max(
                0,
                int(getattr(llm_client, "misses", 0) or 0)
                - state.cache_misses_start,
            ),
        }

    @staticmethod
    def usage_snapshot(llm_client: Any) -> Dict[str, int]:
        """读取客户端当前的累计 usage，作为一次 run 的起点。"""
        usage = getattr(llm_client, "usage", {}) or {}
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

    @staticmethod
    def outcome_metrics(
        state: ReviewState, outcome: ReviewOutcome
    ) -> OutcomeMetrics:
        return {
            "outcome": outcome.as_dict(),
            "failed_batches": state.failed_batches,
            "budget_exhausted": state.budget_exhausted,
            "degraded_reasons": list(state.degraded_reasons),
            "errors": list(state.errors),  # type: ignore[arg-type]
        }


__all__ = ["ErrorRecord", "MetricsCollector"]
