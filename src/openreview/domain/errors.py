"""Error taxonomy shared by services and infrastructure adapters.

The taxonomy is intentionally small: callers only need to distinguish bad
input/config, provider failures, repository failures, cache failures and
budget exhaustion. ``classify_error`` gives metrics/notes a stable category
without leaking exception class names into the report schema.
"""

from __future__ import annotations


class ReviewError(Exception):
    """Base class for expected review failures."""


class ConfigurationError(ReviewError, ValueError):
    """Invalid user configuration (mode, token caps, cache mode, ...)."""


class ReviewInputError(ReviewError, ValueError):
    """Invalid PR URL / local commit range."""


class ExternalServiceError(ReviewError):
    """A remote or local external service failed."""


class ProviderError(ExternalServiceError):
    """LLM provider failed (timeout, refusal, malformed structured output)."""


class RepositoryError(ExternalServiceError):
    """Git/repository preparation failed."""


class CacheError(ReviewError):
    """Review/LLM cache read or write failed."""


class BudgetExceeded(ReviewError, RuntimeError):
    """LLM 调用或 token 预算不足时抛出。"""


def classify_error(exc: BaseException) -> str:
    """Map an exception to a stable metric category."""
    if isinstance(exc, BudgetExceeded):
        return "budget"
    if isinstance(exc, ConfigurationError):
        return "configuration"
    if isinstance(exc, ReviewInputError):
        return "input"
    if isinstance(exc, ProviderError):
        return "provider"
    if isinstance(exc, RepositoryError):
        return "repository"
    if isinstance(exc, CacheError):
        return "cache"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network"
    if isinstance(exc, ValueError):
        return "validation"
    return "internal"
