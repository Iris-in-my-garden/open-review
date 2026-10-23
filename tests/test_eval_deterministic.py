import json
import subprocess
from pathlib import Path

from evaluation.deterministic import (
    compute_deterministic_metrics,
    evidence_is_grounded,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    base = _commit(repo, "base")
    (repo / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    head = _commit(repo, "head")
    return repo, base, head


def _write_result(results_dir: Path, instance_id: str, base: str, head: str) -> None:
    comments = [
        {
            "path": "src/app.py",
            "start_line": 2,
            "end_line": 2,
            "content": "Operator inverted.",
            "severity": "high",
            "category": "bugs",
            "confidence": 0.9,
            "source_aspects": ["bugs", "security"],
            "evidence": "return a + b",
        },
        {
            "path": "src/app.py",
            "start_line": 99,
            "end_line": 99,
            "content": "Line outside diff.",
            "severity": "high",
            "category": "bugs",
            "confidence": 0.8,
            "source_aspects": ["bugs"],
            "evidence": "this evidence is definitely not here",
        },
        {
            "path": "src/app.py",
            "start_line": None,
            "end_line": None,
            "content": "No line and no evidence.",
            "severity": "medium",
            "category": "clean_code",
            "confidence": 0.7,
            "source_aspects": [],
            "evidence": None,
        },
    ]
    payload = {
        "instance_id": instance_id,
        "repo": "owner/repo",
        "base_commit": base,
        "head_commit": head,
        "reviewer": "openreview",
        "status": "ok",
        "duration_seconds": 1.5,
        "review": {
            "comments": comments,
            "summary": {"input_tokens": 100, "output_tokens": 20},
            "metrics": {
                "llm_calls": 4,
                "llm_usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "triage": {"skipped_llm": True, "reason": "docs-only"},
                "retrieval": {
                    "contexts": 5,
                    "changed_file_coverage": 0.5,
                },
                "validation": {
                    "candidates": 3,
                    "kept": 2,
                    "invalid_line": 1,
                },
            },
        },
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"{instance_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_evidence_is_grounded():
    assert evidence_is_grounded("return a + b", "def add(a, b):\n    return a + b")
    assert not evidence_is_grounded(
        "this evidence is definitely not here", "def add(a, b):\n    return a + b"
    )
    assert not evidence_is_grounded("", "anything")


def test_deterministic_metrics_aggregate(tmp_path: Path, monkeypatch):
    repo, base, head = _make_repo(tmp_path)
    instance_id = "owner__repo@abc1234"
    results_dir = tmp_path / "results"
    _write_result(results_dir, instance_id, base, head)

    monkeypatch.setattr(
        "evaluation.deterministic.repo_cache_path",
        lambda cache_dir, repo_name: repo,
    )
    instance = {
        "instance_id": instance_id,
        "split": "dev",
        "repo": "owner/repo",
        "base_commit": base,
        "head_commit": head,
    }

    metrics = compute_deterministic_metrics(
        [instance],
        results_dir=results_dir,
        repo_cache=tmp_path / "cache",
        run_id="test",
        split="dev",
    )

    assert metrics["results_ok"] == 1
    assert metrics["issues"] == 3
    assert metrics["validity"]["invalid_line_count"] == 1
    assert metrics["validity"]["missing_line_count"] == 1
    assert metrics["validity"]["invalid_line_rate"] == 0.333
    assert metrics["evidence"]["checked"] == 2
    assert metrics["evidence"]["grounded"] == 1
    assert metrics["evidence"]["coverage"] == 0.5
    assert metrics["evidence"]["missing_rate"] == 0.333
    assert metrics["triage"]["skip_rate"] == 1.0
    assert metrics["triage"]["reasons"] == {"docs-only": 1}
    assert metrics["attribution"]["coverage"] == 0.667
    assert metrics["attribution"]["multi_aspect_rate"] == 0.333
    assert metrics["attribution"]["aspect_counts"] == {"bugs": 2, "security": 1}
    assert metrics["retrieval"]["contexts"] == 5
    assert metrics["retrieval"]["mean_changed_file_coverage"] == 0.5
    assert metrics["validator"]["candidates"] == 3
    assert metrics["validator"]["invalid_line"] == 1
    assert metrics["cost"]["llm_calls"] == 4
    assert metrics["cost"]["input_tokens"] == 100
    assert metrics["cost"]["duration_seconds"] == 1.5


def test_deterministic_metrics_counts_missing_result(tmp_path: Path):
    instance = {
        "instance_id": "owner__repo@deadbee",
        "split": "dev",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
    }

    metrics = compute_deterministic_metrics(
        [instance],
        results_dir=tmp_path / "empty",
        repo_cache=tmp_path / "cache",
    )

    assert metrics["results_missing"] == 1
    assert metrics["results_ok"] == 0
    assert metrics["issues"] == 0
    assert metrics["per_instance"][0]["status"] == "missing"
