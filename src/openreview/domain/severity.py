"""Severity vocabulary shared by models, policies and renderers."""

from __future__ import annotations

from typing import Literal, Protocol, cast

Severity = Literal["critical", "high", "medium", "low"]

SEVERITY_WEIGHTS: dict[Severity, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

# 兼容 LLM 可能返回的同义词
_SEVERITY_ALIASES = {
    "blocker": "critical",
    "severe": "critical",
    "major": "high",
    "moderate": "medium",
    "minor": "low",
    "trivial": "low",
    "info": "low",
    "informational": "low",
    "致命": "critical",
    "严重": "critical",
    "高": "high",
    "中": "medium",
    "低": "low",
}

_LEVEL_ORDER: list[Severity] = ["low", "medium", "high", "critical"]


class IssueLike(Protocol):
    severity: Severity
    confidence: float
    file: str


def normalize_severity(value: str) -> Severity:
    """把 LLM 输出的大小写/同义词统一成标准等级。"""
    normalized = (value or "").strip().lower()
    normalized = _SEVERITY_ALIASES.get(normalized, normalized)
    if normalized in SEVERITY_WEIGHTS:
        return cast(Severity, normalized)
    return "medium"


def calibrated_severity(issue: IssueLike) -> Severity:
    """按置信度校准严重级别。

    validator 的阈值是 0.6，但 0.6 的 high 与 0.9 的 high 不应等价。
    低于 0.6 视为低置信噪声；0.6–0.75 降一级；更高保持原级别。
    """
    severity = normalize_severity(issue.severity)
    confidence = float(getattr(issue, "confidence", 1.0) or 0.0)
    if confidence < 0.6:
        return "low"
    if confidence < 0.75:
        index = max(0, _LEVEL_ORDER.index(severity) - 1)
        return _LEVEL_ORDER[index]
    return severity
