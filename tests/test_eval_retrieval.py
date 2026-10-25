from __future__ import annotations

import json
from pathlib import Path

from evaluation.retrieval import (
    compute_retrieval_metrics,
    write_retrieval_metrics,
)


def _write_result(results_dir: Path, instance_id: str, payload: dict) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"{instance_id.replace('/', '__')}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_compute_retrieval_metrics(tmp_path: Path):
    results_dir = tmp_path / "results"
    _write_result(
        results_dir,
        "org/repo/1",
        {
            "instance_id": "org/repo/1",
            "status": "ok",
            "review": {
                "comments": [
                    {
                        "path": "src/auth.py",
                        "context_refs": ["ctx-a"],
                        "suggested_patch_valid": True,
                    },
                    {
                        "path": "src/auth.py",
                        "context_refs": [],
                        "suggested_patch_valid": None,
                    },
                ],
                "metrics": {
                    "retrieval": {
                        "contexts": 2,
                        "duplicate_contexts": 0,
                        "context_file_list": ["src/auth.py", "src/api.py"],
                        "index_context_file_list": ["src/auth.py"],
                        "changed_symbols": 2,
                        "symbol_coverage": 0.5,
                        "context_kinds": {"enclosing": 1, "import": 1},
                        "stats": {"index_symbol_hits": 1},
                    }
                },
            },
        },
    )
    _write_result(
        results_dir,
        "org/repo/2",
        {
            "instance_id": "org/repo/2",
            "status": "ok",
            "review": {
                "comments": [],
                "metrics": {
                    "retrieval": {
                        "contexts": 1,
                        "duplicate_contexts": 1,
                        "context_file_list": ["src/other.py"],
                        "index_context_file_list": [],
                        "changed_symbols": 0,
                        "symbol_coverage": 0.0,
                    }
                },
            },
        },
    )

    instances = [
        {
            "instance_id": "org/repo/1",
            "comments": [{"path": "src/auth.py", "body": "bug"}],
        },
        {"instance_id": "org/repo/2", "comments": []},
    ]

    metrics = compute_retrieval_metrics(
        instances, results_dir, run_id="test", split="dev"
    )

    retrieval = metrics["retrieval"]
    assert retrieval["file_recall"] == 1.0
    assert retrieval["index_file_recall"] == 1.0
    assert retrieval["symbol_coverage"] == 0.5
    assert retrieval["citation_rate"] == 0.5
    assert retrieval["avg_contexts"] == 1.5
    assert retrieval["duplicate_context_rate"] == 0.333
    assert retrieval["context_kinds"] == {"enclosing": 1, "import": 1}
    assert metrics["patch_validation"]["valid_rate"] == 1.0

    path = write_retrieval_metrics(metrics, tmp_path / "metrics", run_id="test")
    assert path.exists()
    assert path.name == "retrieval_test.json"
