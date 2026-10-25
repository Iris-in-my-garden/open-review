"""本地 review 结果缓存，用于增量审查和降低重复 LLM 调用。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from filelock import FileLock

from openreview.repo.manager import default_cache_dir
from openreview.review.report_writer import ReviewResult

logger = logging.getLogger(__name__)

PROMPT_VERSION = "review-v3"


def default_review_cache_dir() -> Path:
    env_dir = os.getenv("OPENREVIEW_REVIEW_CACHE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (default_cache_dir().parent / "review").resolve()


def fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


class ReviewCache:
    def __init__(self, cache_dir: Optional[str | Path] = None, enabled: bool = True):
        self.enabled = enabled
        self.cache_dir = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else default_review_cache_dir()
        )
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Optional[ReviewResult]:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return ReviewResult.model_validate(payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring corrupt review cache %s: %s", path, exc)
            return None

    def set(self, key: str, result: ReviewResult) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        lock = FileLock(str(path) + ".lock")
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(result.model_dump(mode="json"), handle, ensure_ascii=False)
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"
