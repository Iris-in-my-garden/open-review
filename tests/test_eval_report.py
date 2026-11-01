import json
from pathlib import Path

import pytest

from evaluation.cli import main
from evaluation.report import (
    build_row,
    load_run,
    parse_run_spec,
    render_table,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(tmp_path: Path) -> Path:
    metrics_dir = tmp_path / "dev-single"
    metrics_dir.mkdir()
    _write(
        metrics_dir / "deterministic_dev-single.json",
        {
            "instances": 2,
            "results_ok": 2,
            "issues": 10,
            "validity": {"invalid_line_rate": 0.0},
            "evidence": {"coverage": 0.9},
            "attribution": {"coverage": 1.0},
            "cost": {
                "llm_calls": 4,
                "llm_calls_avg": 2.0,
                "input_tokens": 1000,
                "output_tokens": 200,
                "duration_seconds_avg": 12.5,
            },
        },
    )
    _write(
        metrics_dir / "retrieval_dev-single.json",
        {"retrieval": {"file_recall": 0.8}},
    )
    _write(
        metrics_dir / "metrics_openreview_20260101_120000.json",
        {
            "summary": {
                "file_semantic_match_rate": 0.6,
                "file_semantic_recall_rate": 0.5,
                "balanced_score": 0.545,
            }
        },
    )
    return metrics_dir


def test_load_run_builds_row_and_markdown(tmp_path: Path):
    metrics_dir = _fixture(tmp_path)
    spec = parse_run_spec(f"single={metrics_dir}")

    deterministic, retrieval, semantic = load_run(spec)
    row = build_row(
        spec.label,
        deterministic,
        retrieval,
        semantic,
    )

    assert row["instances"] == 2
    assert row["precision"] == 0.6
    assert row["recall"] == 0.5
    assert row["retrieval_file_recall"] == 0.8
    assert row["total_tokens"] == 1200

    table = render_table([row])
    assert "| single | 2/2 | 10 | 60.0% | 50.0% | 54.5% |" in table
    assert "90.0%" in table
    assert "80.0%" in table
    assert "1.2k" in table


def test_missing_semantic_file_renders_dash(tmp_path: Path):
    metrics_dir = tmp_path / "dev-mock"
    metrics_dir.mkdir()
    _write(
        metrics_dir / "deterministic_dev-mock.json",
        {
            "instances": 1,
            "results_ok": 1,
            "issues": 0,
            "validity": {"invalid_line_rate": 0.0},
            "evidence": {"coverage": 0.0},
            "attribution": {"coverage": 0.0},
            "cost": {"llm_calls": 1, "llm_calls_avg": 1.0},
        },
    )

    spec = parse_run_spec(f"mock={metrics_dir}")
    deterministic, retrieval, semantic = load_run(spec)
    row = build_row("mock", deterministic, retrieval, semantic)

    assert row["precision"] is None
    table = render_table([row])
    assert "| mock | 1/1 | 0 | - | - | - |" in table


def test_parse_run_spec_rejects_malformed_value():
    with pytest.raises(ValueError):
        parse_run_spec("single")


def test_report_cli_writes_markdown(tmp_path: Path):
    metrics_dir = _fixture(tmp_path)
    out = tmp_path / "eval.md"

    code = main(
        [
            "report",
            "--run",
            f"single={metrics_dir}",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    content = out.read_text(encoding="utf-8")
    assert "# OpenReview 评测结果" in content
    assert "| single | 2/2 |" in content
    assert "1.2k" in content
