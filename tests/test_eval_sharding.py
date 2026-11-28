import json
from pathlib import Path

import pytest

from evaluation.sharding import (
    build_shard_manifests,
    merge_results,
    semantic_scores,
)


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _manifest() -> dict:
    return {
        "version": "v3",
        "instances": [
            {
                "instance_id": "repo-a@1",
                "repo": "repo-a",
                "split": "dev",
                "language": "Go",
            },
            {
                "instance_id": "repo-a@2",
                "repo": "repo-a",
                "split": "dev",
                "language": "Go",
            },
            {
                "instance_id": "repo-b@1",
                "repo": "repo-b",
                "split": "dev",
                "language": "Python",
            },
        ],
    }


def test_build_shard_manifests_keeps_repo_groups_and_recounts(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    _write(manifest_path, _manifest())

    paths = build_shard_manifests(
        manifest_path,
        {"s0": ["repo-a@1", "repo-a@2"], "s1": ["repo-b@1"]},
        output_dir=tmp_path / "shards",
        tune_manifest_path=tmp_path / "tune.json",
    )

    s0 = json.loads(paths["s0"].read_text())
    s1 = json.loads(paths["s1"].read_text())
    tune = json.loads((tmp_path / "tune.json").read_text())

    assert [item["instance_id"] for item in s0["instances"]] == [
        "repo-a@1",
        "repo-a@2",
    ]
    assert [item["instance_id"] for item in s1["instances"]] == ["repo-b@1"]
    assert [item["instance_id"] for item in tune["instances"]] == [
        "repo-a@1",
        "repo-a@2",
        "repo-b@1",
    ]
    assert s0["counts"]["total"] == 2
    assert s1["counts"]["by_language"] == {"Python": 1}


def test_build_shard_manifests_rejects_duplicate_instances(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    _write(manifest_path, _manifest())

    with pytest.raises(ValueError, match="appears in both"):
        build_shard_manifests(
            manifest_path,
            {"s0": ["repo-a@1"], "s1": ["repo-a@1"]},
            output_dir=tmp_path / "shards",
        )


def test_merge_results_copies_payloads_and_orders_summary(tmp_path):
    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    _write(run_a / "repo-a@1.json", {"instance_id": "repo-a@1"})
    _write(
        run_a / "summary_openreview.json",
        [{"instance_id": "repo-a@1", "status": "ok"}],
    )
    _write(run_b / "repo-b@1.json", {"instance_id": "repo-b@1"})
    _write(
        run_b / "summary_openreview.json",
        [{"instance_id": "repo-b@1", "status": "ok"}],
    )

    rows = merge_results(
        [run_a, run_b],
        output_dir=tmp_path / "merged",
        instance_order=["repo-b@1", "repo-a@1"],
    )

    assert [row["instance_id"] for row in rows] == ["repo-b@1", "repo-a@1"]
    assert (tmp_path / "merged" / "repo-a@1.json").exists()
    assert (tmp_path / "merged" / "repo-b@1.json").exists()
    summary = json.loads(
        (tmp_path / "merged" / "summary_openreview.json").read_text()
    )
    assert [row["instance_id"] for row in summary] == ["repo-b@1", "repo-a@1"]


def test_merge_results_requires_force_for_existing_output(tmp_path):
    run = tmp_path / "run"
    _write(run / "summary_openreview.json", [])
    output = tmp_path / "merged"
    output.mkdir()

    with pytest.raises(FileExistsError):
        merge_results([run], output_dir=output)

    assert merge_results([run], output_dir=output, force=True) == []


def test_semantic_scores_filters_instances(tmp_path):
    metrics = {
        "eval_res": [
            {
                "instance_id": "repo-a@1",
                "generated_comments": [{"hit_new_gt": True}],
                "comments": [
                    {"file_semantic_match": True},
                    {"file_semantic_match": False},
                ],
            },
            {
                "instance_id": "repo-b@1",
                "generated_comments": [{"hit_new_gt": False}],
                "comments": [{"file_semantic_match": False}],
            },
        ]
    }
    _write(tmp_path / "metrics" / "metrics_openreview_x.json", metrics)

    scores = semantic_scores(
        tmp_path / "metrics", instance_ids=["repo-a@1"]
    )

    assert scores is not None
    assert scores["generated"] == 1
    assert scores["references"] == 2
    assert scores["matched_generated"] == 1
    assert scores["matched_references"] == 1
    assert scores["precision"] == 1.0
    assert scores["recall"] == 0.5
    assert scores["f1"] == pytest.approx(2 / 3)
