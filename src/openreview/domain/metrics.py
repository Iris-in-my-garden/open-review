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
    changed_symbols: int
    retrieved_symbols: int
    symbol_coverage: float
    index_context_files: int
    context_file_list: List[str]
    index_context_file_list: List[str]
    hybrid_enabled: bool
    graph_enabled: bool
    graph_contexts: int
    graph_context_kinds: Dict[str, int]
    graph_context_files: int
    graph_error: int
    stats: Dict[str, float]


class BudgetMetrics(TypedDict):
    max_llm_calls: int
    used_llm_calls: int
    max_input_tokens: int
    used_input_tokens: int
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
    reviewer_mode: str
    aspects: int
    llm_calls: int
    estimated_patch_tokens: int
    estimated_context_tokens: int
    triage: Dict[str, object]
    impact: Dict[str, object]
    static_analysis: Dict[str, object]
    validation: Dict[str, int]
    verifier: Dict[str, int]
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
