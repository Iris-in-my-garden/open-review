import hashlib
from pathlib import Path

from evaluation.aacr import (
    build_manifest,
    instances_for_split,
    load_manifest,
    parse_github_pr,
    write_manifest,
)


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
        "dev": 10,
        "test": 2,
        "holdout": 8,
        "by_language": {"Go": 10, "Python": 10},
    }
    instances = first["instances"]
    assert len({item["instance_id"] for item in instances}) == 20
    assert {item["split"] for item in instances} == {"dev", "test", "holdout"}
    # 每个 repo 的 dev 样本恰好一条
    dev_repos = [item["repo"] for item in instances if item["split"] == "dev"]
    assert len(dev_repos) == len(set(dev_repos)) == 10
    assert all(item["base_commit"] != item["head_commit"] for item in instances)


def test_manifest_roundtrip_and_split_lookup(tmp_path: Path):
    manifest = build_manifest(_records(), seed=7)
    path = write_manifest(manifest, tmp_path / "manifest.json")
    loaded = load_manifest(path)

    assert loaded == manifest
    dev = instances_for_split(loaded, _records(), "dev")
    assert len(dev) == 10
    assert all(item["comments"] for item in dev)

    holdout = instances_for_split(loaded, _records(), "holdout")
    assert len(holdout) == 8
    assert {item["language"] for item in holdout} == {"Python", "Go"}


def test_parse_github_pr():
    assert parse_github_pr("https://github.com/psf/requests/pull/5711") == (
        "psf/requests",
        5711,
    )
    assert parse_github_pr("not a pr") is None
