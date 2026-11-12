"""Deterministic evaluation gate.

The semantic AACR judge costs money and is non-deterministic, so it cannot be
a required CI check. This gate instead enforces engineering invariants that
should never regress: findings must point at real files/lines, evidence must be
grounded, and every run must produce a result. It reads the deterministic
metrics JSON written by ``python -m evaluation.cli metrics``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping


@dataclass(frozen=True)
class GateThresholds:
    """Hard limits for a run.

    Defaults describe "no known-bad findings": a finding invented for a file or
    line outside the diff, or evidence that cannot be located, is always a
    regression. Coverage floors are intentionally lenient because mock/dev
    runs are not expected to reproduce the full benchmark.
    """

    min_results_ok_rate: float = 1.0
    max_unknown_file_rate: float = 0.0
    max_invalid_line_rate: float = 0.0
    max_ungrounded_rate: float = 0.0
    min_evidence_coverage: float = 0.8
    min_attribution_coverage: float = 0.9

    def as_dict(self) -> Dict[str, float]:
        return {
            "min_results_ok_rate": self.min_results_ok_rate,
            "max_unknown_file_rate": self.max_unknown_file_rate,
            "max_invalid_line_rate": self.max_invalid_line_rate,
            "max_ungrounded_rate": self.max_ungrounded_rate,
            "min_evidence_coverage": self.min_evidence_coverage,
            "min_attribution_coverage": self.min_attribution_coverage,
        }


DEFAULT_THRESHOLDS = GateThresholds()


@dataclass
class GateResult:
    passed: bool
    failures: List[str]
    checked: Dict[str, float]


def _rate(numerator: Any, denominator: Any) -> float:
    try:
        denominator_value = float(denominator)
    except (TypeError, ValueError):
        return 0.0
    if denominator_value <= 0:
        return 0.0
    try:
        return float(numerator) / denominator_value
    except (TypeError, ValueError):
        return 0.0


def evaluate_gate(
    metrics: Mapping[str, Any],
    thresholds: GateThresholds = DEFAULT_THRESHOLDS,
) -> GateResult:
    instances = int(metrics.get("instances") or 0)
    results_ok = int(metrics.get("results_ok") or 0)
    results_error = int(metrics.get("results_error") or 0)
    results_missing = int(metrics.get("results_missing") or 0)
    results_ok_rate = _rate(results_ok, instances)

    validity = metrics.get("validity") or {}
    unknown_file_rate = float(validity.get("unknown_file_rate") or 0.0)
    invalid_line_rate = float(validity.get("invalid_line_rate") or 0.0)

    evidence = metrics.get("evidence") or {}
    checked = int(evidence.get("checked") or 0)
    ungrounded = int(evidence.get("ungrounded") or 0)
    ungrounded_rate = _rate(ungrounded, checked)
    evidence_coverage = float(evidence.get("coverage") or 0.0)

    attribution = metrics.get("attribution") or {}
    attribution_coverage = float(attribution.get("coverage") or 0.0)

    failures: List[str] = []
    if instances <= 0:
        failures.append("instances must be > 0 (no evaluation data)")
    if results_error:
        failures.append(f"results_error={results_error} (expected 0)")
    if results_missing:
        failures.append(f"results_missing={results_missing} (expected 0)")
    if results_ok_rate < thresholds.min_results_ok_rate:
        failures.append(
            f"results_ok_rate={results_ok_rate:.3f} < "
            f"{thresholds.min_results_ok_rate:.3f}"
        )
    if unknown_file_rate > thresholds.max_unknown_file_rate:
        failures.append(
            f"unknown_file_rate={unknown_file_rate:.3f} > "
            f"{thresholds.max_unknown_file_rate:.3f}"
        )
    if invalid_line_rate > thresholds.max_invalid_line_rate:
        failures.append(
            f"invalid_line_rate={invalid_line_rate:.3f} > "
            f"{thresholds.max_invalid_line_rate:.3f}"
        )
    if ungrounded_rate > thresholds.max_ungrounded_rate:
        failures.append(
            f"ungrounded_rate={ungrounded_rate:.3f} > "
            f"{thresholds.max_ungrounded_rate:.3f}"
        )
    if evidence_coverage < thresholds.min_evidence_coverage:
        failures.append(
            f"evidence_coverage={evidence_coverage:.3f} < "
            f"{thresholds.min_evidence_coverage:.3f}"
        )
    if attribution_coverage < thresholds.min_attribution_coverage:
        failures.append(
            f"attribution_coverage={attribution_coverage:.3f} < "
            f"{thresholds.min_attribution_coverage:.3f}"
        )

    return GateResult(
        passed=not failures,
        failures=failures,
        checked={
            "instances": float(instances),
            "results_ok_rate": round(results_ok_rate, 3),
            "unknown_file_rate": round(unknown_file_rate, 3),
            "invalid_line_rate": round(invalid_line_rate, 3),
            "ungrounded_rate": round(ungrounded_rate, 3),
            "evidence_coverage": round(evidence_coverage, 3),
            "attribution_coverage": round(attribution_coverage, 3),
        },
    )


def load_metrics(path: str | Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return data


def print_gate_result(result: GateResult, path: str | Path) -> None:
    print(f"[gate] metrics: {path}")
    for name, value in result.checked.items():
        print(f"[gate] {name}={value}")
    if result.passed:
        print("[gate] PASS")
        return
    print("[gate] FAIL")
    for failure in result.failures:
        print(f"[gate] - {failure}")


__all__ = [
    "DEFAULT_THRESHOLDS",
    "GateResult",
    "GateThresholds",
    "evaluate_gate",
    "load_metrics",
    "print_gate_result",
]
