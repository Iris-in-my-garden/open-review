import json
import subprocess
import threading
import time

import pytest

from evaluation.repos import RepoError, prepare_repositories
from evaluation.results import (
    has_reusable_results,
    result_path,
    run_reviews,
    write_result,
)
from evaluation.run_config import expected_run_config
from openreview.domain.models import ReviewResult


def _instance(instance_id: str, repo: str = "owner/repo") -> dict:
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": f"base-{instance_id}",
        "head_commit": f"head-{instance_id}",
    }


def _payload(instance: dict, config: dict) -> dict:
    return {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "base_commit": instance["base_commit"],
        "head_commit": instance["head_commit"],
        "status": "ok",
        "evaluation_config": config,
        "review": {"comments": [], "summary": {}, "metrics": {}},
    }


def test_reuse_requires_complete_results_and_exact_config(tmp_path):
    instances = [_instance("owner__repo@one"), _instance("owner__repo@two")]
    config = {"model": "gpt-4o-mini", "ablation": "ast"}
    results_dir = tmp_path / "results"

    assert not has_reusable_results(
        instances, results_dir=results_dir, evaluation_config=config
    )
    write_result(results_dir, _payload(instances[0], config))
    assert not has_reusable_results(
        instances, results_dir=results_dir, evaluation_config=config
    )
    write_result(results_dir, _payload(instances[1], config))

    assert has_reusable_results(
        instances, results_dir=results_dir, evaluation_config=config
    )
    assert not has_reusable_results(
        instances,
        results_dir=results_dir,
        evaluation_config={"model": "gpt-4o", "ablation": "ast"},
    )

    payload_path = result_path(results_dir, instances[1]["instance_id"])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["status"] = "partial"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    assert not has_reusable_results(
        instances, results_dir=results_dir, evaluation_config=config
    )


def test_prepare_repositories_once_per_unique_repo(tmp_path, monkeypatch):
    instances = [
        _instance("one", "owner/first"),
        _instance("two", "owner/first"),
        _instance("three", "owner/second"),
    ]
    prepared = []
    checked_commits = []

    def fake_checkout(cache_dir, repo, clone_url, **kwargs):
        prepared.append(repo)
        return tmp_path / repo.replace("/", "__")

    monkeypatch.setattr("evaluation.repos._ensure_repo_checkout", fake_checkout)
    monkeypatch.setattr(
        "evaluation.repos.ensure_commit",
        lambda repo_path, commit, **kwargs: checked_commits.append(
            (repo_path, commit)
        ),
    )

    count = prepare_repositories(instances, cache_dir=tmp_path)

    assert count == 2
    assert set(prepared) == {"owner/first", "owner/second"}
    assert len(checked_commits) == 6


def test_prepare_repositories_collects_commit_failures_and_continues(
    tmp_path, monkeypatch
):
    instances = [
        _instance("one", "owner/first"),
        _instance("two", "owner/first"),
        _instance("three", "owner/second"),
    ]
    checked_commits = []

    monkeypatch.setattr(
        "evaluation.repos._ensure_repo_checkout",
        lambda cache_dir, repo, clone_url, **kwargs: (
            tmp_path / repo.replace("/", "__")
        ),
    )

    def fake_ensure_commit(repo_path, commit, **kwargs):
        checked_commits.append(commit)
        if commit in {"base-one", "head-three"}:
            raise RepoError(f"unavailable {commit}")

    monkeypatch.setattr("evaluation.repos.ensure_commit", fake_ensure_commit)

    with pytest.raises(RepoError, match="unavailable base-one") as error:
        prepare_repositories(instances, cache_dir=tmp_path)

    assert set(checked_commits) == {
        "base-one",
        "head-one",
        "base-two",
        "head-two",
        "base-three",
        "head-three",
    }
    assert "owner/first @ base-one" in str(error.value)
    assert "owner/second @ head-three" in str(error.value)
    assert "其余仓库/commit 已继续处理" in str(error.value)


def test_prepare_repositories_uses_at_most_three_workers(tmp_path, monkeypatch):
    instances = [_instance(str(index), f"owner/repo-{index}") for index in range(6)]
    lock = threading.Lock()
    running = 0
    max_running = 0

    def fake_checkout(cache_dir, repo, clone_url, **kwargs):
        nonlocal running, max_running
        with lock:
            running += 1
            max_running = max(max_running, running)
        time.sleep(0.03)
        with lock:
            running -= 1
        return tmp_path / repo.replace("/", "__")

    monkeypatch.setattr("evaluation.repos._ensure_repo_checkout", fake_checkout)
    monkeypatch.setattr("evaluation.repos.ensure_commit", lambda *args, **kwargs: None)

    count = prepare_repositories(instances, cache_dir=tmp_path)

    assert count == len(instances)
    assert 1 < max_running <= 3


def test_clone_enables_live_progress_and_inherits_stderr(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("evaluation.repos.subprocess.run", fake_run)

    from evaluation.repos import _clone

    _clone(tmp_path / "cache" / "repo", "https://example.test/repo.git", timeout=10)

    command, kwargs = calls[0]
    assert "--progress" in command
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert "stderr" not in kwargs


def test_ensure_repo_does_not_fetch_when_required_commits_are_cached(
    tmp_path, monkeypatch
):
    repo_path = tmp_path / "cache" / "owner__repo"
    (repo_path / ".git").mkdir(parents=True)
    git_calls = []
    monkeypatch.setattr("evaluation.repos._has_commit", lambda *args: True)
    monkeypatch.setattr(
        "evaluation.repos.run_git",
        lambda *args, **kwargs: git_calls.append(args),
    )

    from evaluation.repos import ensure_repo

    assert (
        ensure_repo(
            tmp_path / "cache",
            "owner/repo",
            "https://github.com/owner/repo.git",
            "base",
            "head",
        )
        == repo_path
    )
    assert git_calls == []


def test_run_config_does_not_store_base_url(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    config = expected_run_config(
        manifest_path=manifest,
        split="dev",
        ablation="graph",
        model="gpt-4o-mini",
        base_url="https://example.test/secret-path",
    )
    assert config["model"] == "gpt-4o-mini"
    assert config["ablation"] == "graph"
    assert "secret-path" not in repr(config)


def test_resume_skips_only_matching_result_config(tmp_path, monkeypatch):
    instance = _instance("owner__repo@one")
    instance["split"] = "dev"
    instance["pr_number"] = 1
    instance["github_pr_url"] = "https://github.com/owner/repo/pull/1"
    monkeypatch.setattr(
        "evaluation.repos.ensure_repo", lambda *args, **kwargs: tmp_path / "repo"
    )

    class FakePipeline:
        def __init__(self):
            self.last_result = ReviewResult(
                owner="owner",
                repo="repo",
                pr_number=1,
                summary="",
                risk_level="LOW",
                risk_reason="",
                potential_issues=[],
                final_decision="APPROVE",
                final_summary="",
                metrics={"llm_calls": 0},
            )
            self.calls = 0

        def run_local(self, *args, **kwargs):
            self.calls += 1

    pipeline = FakePipeline()
    results_dir = tmp_path / "results"
    config = {"manifest_sha256": "abc", "model": "gpt-4o-mini"}

    run_reviews(
        [instance],
        repo_cache=tmp_path / "cache",
        results_dir=results_dir,
        pipeline=pipeline,
        evaluation_config=config,
    )
    run_reviews(
        [instance],
        repo_cache=tmp_path / "cache",
        results_dir=results_dir,
        pipeline=pipeline,
        resume=True,
        evaluation_config=config,
    )
    assert pipeline.calls == 1

    run_reviews(
        [instance],
        repo_cache=tmp_path / "cache",
        results_dir=results_dir,
        pipeline=pipeline,
        resume=True,
        evaluation_config={"manifest_sha256": "abc", "model": "different"},
    )
    assert pipeline.calls == 2
