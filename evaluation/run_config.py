"""Stable, non-secret configuration identity for reusable evaluation results."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Optional

from evaluation.aacr import sha256_file
from openreview.domain.versions import ARTIFACT_VERSIONS


def expected_run_config(
    *,
    manifest_path: Path,
    split: str,
    ablation: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    mock_llm: bool = False,
) -> Dict[str, Any]:
    resolved_model = model or os.getenv("OPENAI_MODEL") or "gpt-4o"
    resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL") or ""
    return {
        "schema_version": 1,
        "manifest_sha256": sha256_file(manifest_path),
        "split": split,
        "ablation": ablation or "production",
        "mock_llm": mock_llm,
        "model": resolved_model,
        "base_url_sha256": hashlib.sha256(
            resolved_base_url.encode("utf-8")
        ).hexdigest(),
        "artifacts": ARTIFACT_VERSIONS.as_dict(),
    }
