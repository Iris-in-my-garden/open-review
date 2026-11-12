"""Review cache key construction.

Keeping this in one collaborator makes the contract explicit: the key is
``PR identity + model sampling + artifact versions + output-affecting
config``. Callers no longer need to remember which fields to enumerate.
"""

from __future__ import annotations

from typing import Any, Optional

from openreview.domain.models import PullRequestInfo
from openreview.domain.versions import ARTIFACT_VERSIONS, ArtifactVersions
from openreview.review.cache import fingerprint
from openreview.review.config import ReviewConfig


class CacheKeyBuilder:
    def __init__(
        self,
        config: ReviewConfig,
        versions: ArtifactVersions = ARTIFACT_VERSIONS,
    ):
        self.config = config
        self.versions = versions

    def build(
        self,
        pr_url: str,
        pr_info: Optional[PullRequestInfo],
        llm_client: Any,
    ) -> str:
        if pr_info is None:
            return ""
        return fingerprint(
            pr_url,
            pr_info.base_sha,
            pr_info.head_sha,
            str(getattr(llm_client, "model", "unknown")),
            str(getattr(llm_client, "temperature", "")),
            str(getattr(llm_client, "seed", "")),
            *self.versions.cache_parts(),
            *self.config.cache_key_parts(),
        )
