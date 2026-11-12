"""Domain layer: models, policies, versions and contracts.

The modules here must not import the service/CLI layers. Infrastructure
packages (GitHub, retriever, report writer) re-export the canonical models
from this package so existing imports keep working.
"""

from openreview.domain.errors import (
    BudgetExceeded,
    ConfigurationError,
    ExternalServiceError,
    ProviderError,
    RepositoryError,
    ReviewError,
    ReviewInputError,
    classify_error,
)
from openreview.domain.models import (
    ChangedFile,
    IssueScope,
    IssueSide,
    PotentialIssue,
    PullRequestInfo,
    ReviewResult,
)
from openreview.domain.policies import (
    assess_risk,
    decide,
    deduplicate_issues,
    merge_review_results,
    unique_files,
)
from openreview.domain.severity import Severity, normalize_severity
from openreview.domain.versions import ARTIFACT_VERSIONS, ArtifactVersions

__all__ = [
    "ARTIFACT_VERSIONS",
    "ArtifactVersions",
    "BudgetExceeded",
    "ChangedFile",
    "ConfigurationError",
    "ExternalServiceError",
    "IssueScope",
    "IssueSide",
    "PotentialIssue",
    "ProviderError",
    "PullRequestInfo",
    "RepositoryError",
    "ReviewError",
    "ReviewInputError",
    "ReviewResult",
    "Severity",
    "assess_risk",
    "classify_error",
    "decide",
    "deduplicate_issues",
    "merge_review_results",
    "normalize_severity",
    "unique_files",
]
