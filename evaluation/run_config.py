"""Stable, non-secret configuration identity for reusable evaluation results."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from evaluation.aacr import sha256_file
from openreview.domain.versions import ARTIFACT_VERSIONS
from openreview.review.llm_client import (
    DEFAULT_REASONING_EFFORT,
    resolve_base_url,
    resolve_model,
    resolve_verifier_model,
)


def expected_run_config(
    *,
    manifest_path: Path,
    split: str,
    ablation: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    verifier_model: Optional[str] = None,
    thinking: bool = True,
    verifier_thinking: bool = False,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    mock_llm: bool = False,
) -> Dict[str, Any]:
    resolved_model = resolve_model(model)
    resolved_verifier_model = resolve_verifier_model(
        resolved_model, verifier_model
    )
    resolved_base_url = resolve_base_url(resolved_model, base_url) or ""
    return {
        # v2: 增加 verifier 模型 / thinking / reasoning effort，
        # 避免换模型后误复用旧结果。
        "schema_version": 2,
        "manifest_sha256": sha256_file(manifest_path),
        "split": split,
        "ablation": ablation or "production",
        "mock_llm": mock_llm,
        "model": resolved_model,
        "verifier_model": resolved_verifier_model,
        "thinking": thinking,
        "verifier_thinking": verifier_thinking,
        "reasoning_effort": reasoning_effort,
        "base_url_sha256": hashlib.sha256(
            resolved_base_url.encode("utf-8")
        ).hexdigest(),
        "artifacts": ARTIFACT_VERSIONS.as_dict(),
    }
