import hashlib
from pathlib import Path

import pytest

from evaluation.aacr import (
    ManifestError,
    build_manifest,
    instances_for_split,
    load_manifest,
    parse_github_pr,
    write_manifest,
)
from evaluation.cli import _estimate_aacr_review


def _record(language: str, repo: str, index: int) -> dict:
    head = hashlib.sha256(f"head-{index}".encode()).hexdigest()
    base = hashlib.sha256(f"base-{index}".encode()).hexdigest()
    return {
        "githubPrUrl": f"https://github.com/{repo}/pull/{index}",
        "source_commit": base,
        "target_commit": head,
        "project_main_language": language,
        "change_line_count": index * 10,
        "category": "Bug Fix",
        "comments": [
            {
                "path": "src/app.py",
                "note": f"comment {index}",
                "from_line": 1,
                "to_line": 2,
                "side": "right",
            }
        ],
    }


def _records() -> list[dict]:
    records = []
    index = 1
    for language in ("Python", "Go"):
        for repo_index in range(5):
            for _ in range(2):
                records.append(
                    _record(
                        language,
                        f"owner{repo_index}/{language.lower()}-repo",
                        index,
                    )
                )
                index += 1
    # 不支持的语言应被过滤
    records.append(_record("Rust", "rust/lang", index))
    return records


def test_build_manifest_is_deterministic_and_stratified():
    records = _records()

    first = build_manifest(records, seed=7)
    second = build_manifest(records, seed=7)

    assert first == second
    assert first["counts"] == {
        "total": 20,
        "dev": 4,
        "test": 12,
        "holdout": 4,
        "by_language": {"Go": 10, "Python": 10},
        "by_split_language": {
            "dev": {"Go": 2, "Python": 2},
            "test": {"Go": 6, "Python": 6},
            "holdout": {"Go": 2, "Python": 2},
        },
        "repositories_by_split": {"dev": 2, "test": 6, "holdout": 2},
    }
    instances = first["instances"]
    assert len({item["instance_id"] for item in instances}) == 20
    assert {item["split"] for item in instances} == {"dev", "test", "holdout"}
    split_by_repo = {}
    for item in instances:
        split_by_repo.setdefault(item["repo"], item["split"])
        assert split_by_repo[item["repo"]] == item["split"]
    assert len(split_by_repo) == 10
    assert all(item["base_commit"] != item["head_commit"] for item in instances)


def test_manifest_roundtrip_and_split_lookup(tmp_path: Path):
    manifest = build_manifest(_records(), seed=7)
    path = write_manifest(manifest, tmp_path / "manifest.json")
    loaded = load_manifest(path)

    assert loaded == manifest
    dev = instances_for_split(loaded, _records(), "dev")
    assert len(dev) == 4
    assert all(item["comments"] for item in dev)

    holdout = instances_for_split(loaded, _records(), "holdout")
    assert len(holdout) == 4
    assert {item["language"] for item in holdout} == {"Python", "Go"}
    assert {item["repo"] for item in dev}.isdisjoint(
        item["repo"] for item in holdout
    )


def test_manifest_rejects_repository_leakage(tmp_path: Path):
    manifest = build_manifest(_records(), seed=7)
    repo = next(
        item["repo"]
        for item in manifest["instances"]
        if item["split"] == "dev"
    )
    matching = [item for item in manifest["instances"] if item["repo"] == repo]
    matching[-1]["split"] = "test"
    path = write_manifest(manifest, tmp_path / "leaked.json")

    with pytest.raises(ManifestError, match="跨 split"):
        load_manifest(path)


def test_parse_github_pr():
    assert parse_github_pr("https://github.com/psf/requests/pull/5711") == (
        "psf/requests",
        5711,
    )
    assert parse_github_pr("not a pr") is None


def test_estimate_reflects_retrieval_ablation():
    instances = [{"change_line_count": 100, "reference_comments": 3}]

    diff = _estimate_aacr_review(instances, ablation="diff")
    ast = _estimate_aacr_review(instances, ablation="ast")
    graph = _estimate_aacr_review(instances, ablation="graph")
    verifier = _estimate_aacr_review(instances, ablation="verifier")

    assert diff["retrieval_tokens"] == 0
    assert diff["total_tokens"] < ast["total_tokens"] < graph["total_tokens"]
    assert graph["retrieval_tokens"] == verifier["retrieval_tokens"]
    assert verifier["total_tokens"] > graph["total_tokens"]
    assert diff["llm_calls"] == 1
    assert verifier["llm_calls"] == 2
