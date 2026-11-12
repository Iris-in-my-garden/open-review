"""Backward-compatible facade for review assessment policies.

The implementation now lives in :mod:`openreview.domain.policies` and
:mod:`openreview.domain.severity`. Existing imports from this module remain
valid, which keeps the public API stable while the dependency direction is
``review -> domain`` instead of ``domain -> review``.
"""

from openreview.domain.policies import (
    assess_risk,
    decide,
    deduplicate_issues,
    merge_review_results,
    unique_files,
)
from openreview.domain.severity import (
    SEVERITY_WEIGHTS,
    IssueLike,
    Severity,
    normalize_severity,
)

__all__ = [
    "SEVERITY_WEIGHTS",
    "IssueLike",
    "Severity",
    "assess_risk",
    "decide",
    "deduplicate_issues",
    "merge_review_results",
    "normalize_severity",
    "unique_files",
]
