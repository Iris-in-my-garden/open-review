"""Stable markers written into GitHub artifacts.

The marker is the only reliable way to identify reviews created by this tool.
It must not depend on the authenticated user, because GitHub Apps and PATs can
be rotated, and it must be stable across prompt/model versions so that a new
run can find and replace the previous review.
"""

from __future__ import annotations

REVIEW_MARKER = "<!-- openreview:managed-review:v1 -->"
COMMENT_MARKER_PREFIX = "<!-- openreview:finding:"

__all__ = ["COMMENT_MARKER_PREFIX", "REVIEW_MARKER"]
