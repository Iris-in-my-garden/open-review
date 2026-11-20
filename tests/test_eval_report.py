import copy
import json
from pathlib import Path

import pytest

from evaluation.cli import main
from evaluation.report import (
    RunSpec,
    _bootstrap_confidence_intervals,
    build_row,
    load_run,
    parse_run_spec,
    render_report,
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
        {
            "retrieval": {
                "file_recall": 0.8,
                "symbol_coverage": 0.75,
                "citation_rate": 0.5,
                "graph_contexts_avg": 2.5,
            }
        },
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
    assert row["retrieval_symbol_coverage"] == 0.75
    assert row["retrieval_citation_rate"] == 0.5
    assert row["graph_contexts_avg"] == 2.5
    assert row["total_tokens"] == 1200

    table = render_table([row])
    assert "| single | 2/2 | 10 | 60.0% | 50.0% | 54.5% |" in table
    assert "90.0%" in table
    assert "80.0%" in table
    assert "75.0%" in table
    assert "2.50 | 100.0% |" in table
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


def test_semantic_strata_and_repository_bootstrap_are_reproducible():
    deterministic = {
        "per_instance": [
            {"instance_id": "py-1", "repo": "owner/py-one", "language": "Python"},
            {"instance_id": "py-2", "repo": "owner/py-two", "language": "Python"},
            {"instance_id": "go-1", "repo": "owner/go-one", "language": "Go"},
            {"instance_id": "go-2", "repo": "owner/go-two", "language": "Go"},
        ]
    }
    semantic = {
        "ex_info": {"score_mode": "file_semantic"},
        "eval_res": [
            {
                "instance_id": "py-1",
                "repo": "owner/py-one",
                "comments": [{"note": "a", "file_semantic_match": True}],
                "generated_comments": [{"hit_new_gt": True}],
            },
            {
                "instance_id": "py-2",
                "repo": "owner/py-two",
                "comments": [{"note": "b", "file_semantic_match": False}],
                "generated_comments": [{"hit_new_gt": False}],
            },
            {
                "instance_id": "go-1",
                "repo": "owner/go-one",
                "comments": [
                    {"note": "c", "file_semantic_match": True},
                    {"note": "d", "file_semantic_match": False},
                ],
                "generated_comments": [{"hit_new_gt": True}],
            },
            {
                "instance_id": "go-2",
                "repo": "owner/go-two",
                "comments": [{"note": "e", "file_semantic_match": False}],
                "generated_comments": [{"hit_new_gt": False}],
            },
        ],
    }
    row = build_row("grouped", deterministic, {}, semantic)
    repeat = build_row("grouped", deterministic, {}, semantic)
    strata = row["semantic_strata"]

    assert strata["by_language"]["Python"]["instances"] == 2
    assert strata["by_language"]["Python"]["precision"] == 0.5
    assert strata["by_language"]["Go"]["recall"] == 1 / 3
    assert strata["by_repo"]["owner/py-one"]["balanced"] == 1.0
    assert strata["bootstrap_95_ci"] == repeat["semantic_strata"]["bootstrap_95_ci"]
    overall_ci = strata["bootstrap_95_ci"]["intervals"]["overall"]
    assert 0.0 <= overall_ci["balanced"][0] <= overall_ci["balanced"][1] <= 1.0
    singleton_ci = _bootstrap_confidence_intervals(
        {
            "owner/only-repo": {
                "language": "Go",
                "instances": 1,
                "generated": 1,
                "expected": 1,
                "matched_generated": 1,
                "matched_reference": 1,
            }
        }
    )
    assert singleton_ci["intervals"]["overall"]["balanced"] == [None, None]

    report = render_report([RunSpec("grouped", Path("metrics"))], [row])
    assert "## 语义指标分层与置信区间" in report
    assert "仓库簇 bootstrap" in report
    assert "owner/py-one" in report
    assert "Python" in report

    improved_semantic = copy.deepcopy(semantic)
    improved_semantic["eval_res"][1]["comments"][0][
        "file_semantic_match"
    ] = True
    improved_semantic["eval_res"][1]["generated_comments"][0]["hit_new_gt"] = True
    improved = build_row("improved", deterministic, {}, improved_semantic)
    comparison_report = render_report(
        [RunSpec("grouped", Path("metrics")), RunSpec("improved", Path("metrics"))],
        [row, improved],
    )
    assert "## 配置间配对比较" in comparison_report
    assert "grouped vs improved" in comparison_report
    assert "Paired PRs" in comparison_report


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
