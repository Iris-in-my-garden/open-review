"""Single source of truth for artifact / cache versions.

Cache keys and debug artifacts must agree on which prompt, validator,
retriever and cache schema produced a result. Keeping the constants in one
place prevents a partial bump (for example prompt v7 with validator v1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

PROMPT_VERSION = "review-v6"
VALIDATOR_VERSION = "validator-v2"
RETRIEVER_VERSION = "hybrid-v2"
REVIEW_CACHE_SCHEMA_VERSION = 2
METRICS_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ArtifactVersions:
    prompt: str = PROMPT_VERSION
    validator: str = VALIDATOR_VERSION
    retriever: str = RETRIEVER_VERSION
    review_cache_schema: int = REVIEW_CACHE_SCHEMA_VERSION
    metrics_schema: int = METRICS_SCHEMA_VERSION

    def cache_parts(self) -> List[str]:
        """Stable, ordered version manifest for cache keys and debug output."""
        return [
            f"prompt={self.prompt}",
            f"validator={self.validator}",
            f"retriever={self.retriever}",
            f"review_cache_schema={self.review_cache_schema}",
        ]

    def as_dict(self) -> dict[str, str | int]:
        return {
            "prompt": self.prompt,
            "validator": self.validator,
            "retriever": self.retriever,
            "review_cache_schema": self.review_cache_schema,
            "metrics_schema": self.metrics_schema,
        }


ARTIFACT_VERSIONS = ArtifactVersions()
