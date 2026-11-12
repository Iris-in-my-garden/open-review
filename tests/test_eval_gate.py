from __future__ import annotations

import json
from pathlib import Path

from evaluation.gate import GateThresholds, evaluate_gate, load_metrics


def _metrics(**overrides):
    metrics = {
        "instances": 10,
        "results_ok": 10,
        "results_error": 0,
        "results_missing": 0,
        "validity": {
            "unknown_file_rate": 0.0,
            "invalid_line_rate": 0.0,
        },
        "evidence": {
            "checked": 10,
            "ungrounded": 0,
            "coverage": 1.0,
        },
        "attribution": {"coverage": 1.0},
    }
    metrics.update(overrides)
    return metrics


def test_gate_passes_on_clean_deterministic_metrics():
    result = evaluate_gate(_metrics())

    assert result.passed
    assert result.failures == []


def test_gate_fails_on_invented_lines():
    result = evaluate_gate(
        _metrics(
            validity={"unknown_file_rate": 0.0, "invalid_line_rate": 0.2}
        )
    )

    assert not result.passed
    assert any("invalid_line_rate" in failure for failure in result.failures)


def test_gate_fails_when_a_result_is_missing():
    result = evaluate_gate(
        _metrics(results_ok=9, results_missing=1)
    )

    assert not result.passed
    assert any("results_missing=1" in failure for failure in result.failures)
    assert any("results_ok_rate" in failure for failure in result.failures)


def test_gate_fails_on_ungrounded_evidence():
    result = evaluate_gate(
        _metrics(evidence={"checked": 10, "ungrounded": 2, "coverage": 0.8})
    )

    assert not result.passed
    assert any("ungrounded_rate" in failure for failure in result.failures)


def test_gate_thresholds_are_overridable_for_smoke_runs():
    result = evaluate_gate(
        _metrics(
            results_ok=9,
            results_missing=1,
            evidence={"checked": 10, "ungrounded": 1, "coverage": 0.5},
        ),
        GateThresholds(
            min_results_ok_rate=0.9,
            max_ungrounded_rate=0.2,
            min_evidence_coverage=0.5,
        ),
    )

    # results_missing 仍然是硬错误，不能通过阈值放宽。
    assert not result.passed
    assert any("results_missing" in failure for failure in result.failures)


def test_load_metrics_rejects_non_object(tmp_path: Path):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    try:
        load_metrics(path)
    except ValueError as error:
        assert "expected a JSON object" in str(error)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("load_metrics should reject a JSON array")
