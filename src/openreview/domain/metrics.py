"""Typed metrics schema for a single review run.

The runtime still emits plain ``dict`` objects for JSON compatibility, but
these TypedDicts document the contract consumed by evaluation/reporting and
give mypy something to check inside the collectors.
"""

from __future__ import annotations

from typing import Dict, List, TypedDict

from openreview.domain.versions import METRICS_SCHEMA_VERSION


class ErrorRecord(TypedDict):
    category: str
    node: str
    error_type: str
    message: str
    batch: int | None


class DiffMetrics(TypedDict):
    files: int
    additions: int
    deletions: int
    languages: List[str]


class ContextGapMetrics(TypedDict, total=False):
    batch_context_occurrences: int
    cross_batch_duplicates: int
    retrieval_candidates: int
    retrieval_deduped: int
    retrieval_truncated: int
    ast_truncated: int
    search_targets_truncated: int
    prompt_contexts_used: int
    prompt_contexts_omitted: int
    prompt_context_used_tokens: int
    prompt_context_omitted_tokens: int


class RetrievalMetrics(TypedDict, total=False):
    contexts: int
    context_files: int
    changed_files: int
    covered_changed_files: int
    changed_file_coverage: float
    context_tokens: int
    unique_contexts: int
    duplicate_contexts: int
    context_kinds: Dict[str, int]
    context_priorities: Dict[str, int]
    context_tokens_by_priority: Dict[str, int]
    folded_contexts: int
    changed_symbols: int
    retrieved_symbols: int
    symbol_coverage: float
    index_context_files: int
    context_file_list: List[str]
    index_context_file_list: List[str]
    retrieval_mode: str
    graph_enabled: bool
    graph_contexts: int
    graph_context_kinds: Dict[str, int]
    graph_context_files: int
    graph_error: int
    stats: Dict[str, float]
    context_gap: ContextGapMetrics


class BudgetMetrics(TypedDict):
    max_llm_calls: int
    used_llm_calls: int
    max_input_tokens: int
    used_input_tokens: int
    max_output_tokens: int
    used_output_tokens: int
    max_seconds: float
    elapsed_seconds: float


class CacheMetrics(TypedDict, total=False):
    review_hit: bool
    key: str
    llm_hits: int
    llm_misses: int
    served_from_cache: bool
    cache_lookup_seconds: float
    current_run_seconds: float


class OutcomeMetrics(TypedDict):
    outcome: Dict[str, object]
    failed_batches: int
    budget_exhausted: bool
    degraded_reasons: List[str]
    errors: List[ErrorRecord]


class VerificationStages(TypedDict):
    raw_count: int
    validator_kept: int
    verifier_candidates: int
    final_count: int


class ValidatorDrops(TypedDict):
    by_diff_scope: int
    by_evidence_mismatch: int
    by_low_confidence: int
    deduplicated: int


class VerifierDrops(TypedDict):
    rejected: int
    dropped_by_low_confidence: int


class VerificationDrops(TypedDict):
    validator: ValidatorDrops
    verifier: VerifierDrops


class VerificationEvents(TypedDict):
    stripped_invalid_patch: int
    ungrounded_evidence: int
    missing_evidence: int
    line_relocated: int
    needs_more_context: int
    not_checked_by_verifier: int


class VerificationConservation(TypedDict):
    validator_balanced: bool | None
    verifier_balanced: bool | None


class VerificationMetrics(TypedDict):
    """Finding funnel counts, drop causes, and non-exclusive diagnostics.

    Drop causes are mutually exclusive per finding within each stage.
    Events may overlap with drops and must not be used for conservation checks.
    """

    schema_version: int
    available: bool
    stages: VerificationStages
    drops: VerificationDrops
    events: VerificationEvents
    conservation: VerificationConservation


class RunMetrics(TypedDict, total=False):
    schema_version: int
    run_id: str
    versions: Dict[str, str | int]
    stages_seconds: Dict[str, float]
    total_seconds: float
    files: int
    diff: DiffMetrics
    retrieval: RetrievalMetrics
    batches: int
    ablation: str
    retrieval_mode: str
    aspects: int
    llm_calls: int
    llm_calls_detail: List[Dict[str, object]]
    estimated_patch_tokens: int
    estimated_context_tokens: int
    triage: Dict[str, object]
    impact: Dict[str, object]
    static_analysis: Dict[str, object]
    validation: Dict[str, int]
    verifier: Dict[str, int]
    verification: VerificationMetrics
    cache: CacheMetrics
    budget: BudgetMetrics
    llm_usage: Dict[str, int]
    metrics_source: str
    errors: List[ErrorRecord]
    outcome: Dict[str, object]
    failed_batches: int
    budget_exhausted: bool
    degraded_reasons: List[str]


def metric_schema_version() -> int:
    return METRICS_SCHEMA_VERSION
