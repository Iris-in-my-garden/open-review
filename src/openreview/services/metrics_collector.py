"""Typed run metrics collection.

The executor used to own ~150 lines of metrics dictionary assembly. This
collector keeps the schema in one place, adds ``run_id`` / ``schema_version``
and records structured errors without changing the keys consumed by reports
and evaluation.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, cast

from openreview.domain.metrics import (
    CacheMetrics,
    ErrorRecord,
    OutcomeMetrics,
    RunMetrics,
    ValidatorDrops,
    VerificationMetrics,
    VerifierDrops,
    metric_schema_version,
)
from openreview.domain.versions import (
    ARTIFACT_VERSIONS,
    VERIFICATION_METRICS_SCHEMA_VERSION,
)
from openreview.review.config import ReviewConfig
from openreview.review.file_filter import FILE_FILTER_POLICY_VERSION
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
        raw_diff = getattr(state, "raw_parsed_diff", None) or parsed_diff
        raw_files = raw_diff.files if raw_diff else []
        skipped_files = list(getattr(state, "skipped_files", []) or [])
        skipped_categories = Counter(
            str(item.get("category") or "other") for item in skipped_files
        )
        changed_files = {file.filename for file in diff_files}
        context_files = {context.file_path for context in state.all_contexts}
        covered_files = changed_files & context_files
        context_ids = {context.ensure_context_id() for context in state.all_contexts}
        context_kinds: Dict[str, int] = {}
        priority_counts: Dict[str, int] = {}
        priority_tokens: Dict[str, int] = {}
        folded_contexts = 0
        for context in state.all_contexts:
            context_kinds[context.kind] = context_kinds.get(context.kind, 0) + 1
            key = f"P{int(context.priority)}"
            priority_counts[key] = priority_counts.get(key, 0) + 1
            priority_tokens[key] = priority_tokens.get(key, 0) + estimate_tokens(
                context.snippet
            )
            if context.folded:
                folded_contexts += 1
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
        # test/impact/static 不是符号索引的直接产物。
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
        context_stats = getattr(state, "context_stats", {})
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
            "filtering": {
                "enabled": config.file_filter_enabled,
                "policy": FILE_FILTER_POLICY_VERSION,
                "raw_files": len(raw_files),
                "reviewable_files": len(diff_files),
                "skipped_files": len(skipped_files),
                "skipped_additions": sum(
                    int(item.get("additions") or 0) for item in skipped_files
                ),
                "skipped_deletions": sum(
                    int(item.get("deletions") or 0) for item in skipped_files
                ),
                "categories": dict(sorted(skipped_categories.items())),
                "files": skipped_files[:100],
                "language_filter": {
                    "skipped_files": len(
                        getattr(state, "language_skipped_files", []) or []
                    ),
                    "summary": getattr(state, "language_filter_summary", ""),
                },
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
                "context_priorities": priority_counts,
                "context_tokens_by_priority": priority_tokens,
                "folded_contexts": folded_contexts,
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
                "retrieval_mode": config.retrieval_mode,
                "graph_enabled": config.retrieval_mode == "graph",
                "graph_contexts": graph_contexts,
                "graph_context_kinds": graph_context_kinds,
                "graph_context_files": len(graph_context_files),
                "graph_error": int(state.retrieval_stats.get("graph_error", 0)),
                "stats": dict(state.retrieval_stats),
                # 区分“没检索到”和“检索到但被 caps / prompt 预算丢掉”。
                "context_gap": {
                    "batch_context_occurrences": int(
                        context_stats.get("batch_context_occurrences", 0)
                    ),
                    "cross_batch_duplicates": int(
                        context_stats.get(
                            "cross_batch_duplicate_contexts", 0
                        )
                    ),
                    "retrieval_candidates": int(
                        state.retrieval_stats.get("contexts_candidates", 0)
                    ),
                    "retrieval_deduped": int(
                        state.retrieval_stats.get("contexts_deduped", 0)
                    ),
                    "retrieval_truncated": int(
                        state.retrieval_stats.get("contexts_truncated", 0)
                    ),
                    "ast_truncated": int(
                        state.retrieval_stats.get("ast_contexts_truncated", 0)
                    ),
                    "search_targets_truncated": int(
                        state.retrieval_stats.get("search_targets_truncated", 0)
                    ),
                    "prompt_contexts_used": int(
                        context_stats.get("prompt_contexts_used", 0)
                    ),
                    "prompt_contexts_omitted": int(
                        context_stats.get("prompt_contexts_omitted", 0)
                    ),
                    "prompt_context_used_tokens": int(
                        context_stats.get("prompt_context_used_tokens", 0)
                    ),
                    "prompt_context_omitted_tokens": int(
                        context_stats.get(
                            "prompt_context_omitted_tokens", 0
                        )
                    ),
                },
            },
            "batches": len(state.batches),
            "ablation": config.ablation or "production",
            "retrieval_mode": config.retrieval_mode,
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
            "verification": self._verification_metrics(state),
            "cache": self._cache_metrics(state, llm_client),
            "budget": {
                "max_llm_calls": state.budget.max_llm_calls,
                "used_llm_calls": state.budget.used_llm_calls,
                "max_input_tokens": state.budget.max_input_tokens,
                "estimated_input_tokens": state.budget.estimated_input_tokens,
                "used_input_tokens": state.budget.used_input_tokens,
                "max_output_tokens": state.budget.max_output_tokens,
                "used_output_tokens": state.budget.used_output_tokens,
                "reserved_output_tokens": state.budget.reserved_output_tokens,
                "max_seconds": state.budget.max_seconds,
                "elapsed_seconds": round(state.budget.elapsed(), 3),
                "input_tokens_over_budget": bool(
                    state.budget.max_input_tokens > 0
                    and state.budget.used_input_tokens
                    > state.budget.max_input_tokens
                ),
                "output_tokens_over_budget": bool(
                    state.budget.max_output_tokens > 0
                    and state.budget.used_output_tokens
                    > state.budget.max_output_tokens
                ),
                "input_token_limit": (
                    "estimated before request; reconciled with provider usage "
                    "after response"
                ),
                "input_usage_source": (
                    "provider usage when available; estimate for requests without usage"
                ),
                "output_token_limit": (
                    "reserved before request; enforced by max_completion_tokens"
                ),
                "time_limit": (
                    "request timeout is capped by remaining run budget; "
                    "client-side cancellation is not guaranteed"
                ),
            },
            "llm_usage": self._usage_delta(
                state.usage_start,
                self.usage_snapshot(llm_client),
            ),
        }
        run_usage = self._run_usage(state, llm_client)
        if run_usage is not None:
            metrics["llm_usage"] = run_usage
        metrics.update(self.outcome_metrics(state, outcome))
        return metrics

    def cache_hit_metrics(
        self,
        state: ReviewState,
        cached_metrics: Dict[str, Any],
        outcome: ReviewOutcome,
        llm_client: Any = None,
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
        metrics["stages_seconds"] = dict(state.stages)
        metrics["total_seconds"] = round(sum(state.stages.values()), 3)
        metrics["llm_calls"] = state.budget.used_llm_calls
        metrics["llm_calls_detail"] = self._call_details(state, llm_client)
        run_usage = self._run_usage(state, llm_client)
        metrics["llm_usage"] = run_usage or {
            "calls": 0,
            "estimated_prompt_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        budget: Dict[str, Any] = dict(metrics.get("budget") or {})
        budget.update(
            {
                "max_llm_calls": state.budget.max_llm_calls,
                "used_llm_calls": state.budget.used_llm_calls,
                "max_input_tokens": state.budget.max_input_tokens,
                "estimated_input_tokens": state.budget.estimated_input_tokens,
                "used_input_tokens": state.budget.used_input_tokens,
                "max_output_tokens": state.budget.max_output_tokens,
                "used_output_tokens": state.budget.used_output_tokens,
                "reserved_output_tokens": state.budget.reserved_output_tokens,
                "max_seconds": state.budget.max_seconds,
                "elapsed_seconds": round(state.budget.elapsed(), 3),
                "input_tokens_over_budget": False,
                "output_tokens_over_budget": False,
            }
        )
        metrics["budget"] = budget
        metrics.update(self.outcome_metrics(state, outcome))
        return metrics  # type: ignore[return-value]

    @staticmethod
    def _verification_metrics(state: Any) -> VerificationMetrics:
        validation = getattr(state, "validation_stats", {}) or {}
        verifier = getattr(state, "verifier_stats", {}) or {}

        def count(source: Dict[str, Any], key: str) -> int:
            return max(0, int(source.get(key, 0) or 0))

        raw_count = count(validation, "candidates")
        available = "candidates" in validation
        validator_kept = (
            count(validation, "validator_kept")
            if "validator_kept" in validation
            else count(validation, "kept")
        )
        verifier_candidates = count(verifier, "candidates")
        result = getattr(state, "review_result", None)
        final_count = len(result.potential_issues) if result is not None else 0

        verified = count(verifier, "verified")
        rejected = count(verifier, "rejected")
        needs_more_context = count(verifier, "needs_more_context")
        verifier_decided = verified + rejected + needs_more_context
        validation_drops: ValidatorDrops = {
            "by_diff_scope": (
                count(validation, "dropped_unknown_file")
                + count(validation, "dropped_pre_existing")
                + count(validation, "invalid_line")
            ),
            # Evidence mismatch currently lowers confidence; only actual
            # finding removals belong in this mutually exclusive drop bucket.
            "by_evidence_mismatch": 0,
            "by_low_confidence": count(validation, "low_confidence"),
            "deduplicated": (
                count(validation, "deduplicated")
                + count(validation, "cross_batch_deduplicated")
            ),
        }
        verifier_drops: VerifierDrops = {
            "rejected": rejected,
            "dropped_by_low_confidence": count(
                verifier, "dropped_low_confidence"
            ),
        }
        validator_balanced: bool | None = None
        verifier_balanced: bool | None = None
        if available:
            validator_balanced = raw_count == (
                validator_kept
                + validation_drops["by_diff_scope"]
                + validation_drops["by_evidence_mismatch"]
                + validation_drops["by_low_confidence"]
                + validation_drops["deduplicated"]
            )
            verifier_balanced = validator_kept == (
                final_count
                + verifier_drops["rejected"]
                + verifier_drops["dropped_by_low_confidence"]
            )

        return {
            "schema_version": VERIFICATION_METRICS_SCHEMA_VERSION,
            "available": available,
            "stages": {
                "raw_count": raw_count,
                "validator_kept": validator_kept,
                "verifier_candidates": verifier_candidates,
                "final_count": final_count,
            },
            "drops": {
                "validator": validation_drops,
                "verifier": verifier_drops,
            },
            "events": {
                "stripped_invalid_patch": count(validation, "patch_invalid"),
                "ungrounded_evidence": count(validation, "ungrounded_evidence"),
                "missing_evidence": count(validation, "missing_evidence"),
                "line_relocated": count(validation, "line_relocated"),
                "needs_more_context": needs_more_context,
                "not_checked_by_verifier": max(
                    0, verifier_candidates - verifier_decided
                ),
            },
            "conservation": {
                "validator_balanced": validator_balanced,
                "verifier_balanced": verifier_balanced,
            },
        }

    @staticmethod
    def _call_details(state: ReviewState, llm_client: Any) -> list[Dict[str, Any]]:
        """Return provider attempts explicitly tagged with this run identity."""
        log = getattr(llm_client, "call_log", []) or []
        tagged = [dict(item) for item in log if item.get("run_id") == state.run_id]
        if tagged or any("run_id" in item for item in log):
            return tagged
        # Backward compatibility for third-party clients that don't tag events.
        start = max(0, int(getattr(state, "llm_call_start", 0) or 0))
        return [dict(item, run_id=state.run_id) for item in log[start:]]

    @classmethod
    def _run_usage(
        cls, state: ReviewState, llm_client: Any
    ) -> Dict[str, int] | None:
        log = getattr(llm_client, "call_log", None)
        if not isinstance(log, list):
            return None
        if any("run_id" in item for item in log):
            details = cls._call_details(state, llm_client)
            return {
                "calls": len(details),
                "estimated_prompt_tokens": sum(
                    int(item.get("estimated_prompt_tokens", 0) or 0)
                    for item in details
                ),
                "prompt_tokens": sum(
                    int(item.get("prompt_tokens", 0) or 0) for item in details
                ),
                "completion_tokens": sum(
                    int(item.get("completion_tokens", 0) or 0)
                    for item in details
                ),
                "total_tokens": sum(
                    int(item.get("total_tokens", 0) or 0) for item in details
                ),
            }
        return None

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
