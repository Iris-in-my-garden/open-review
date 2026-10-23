import json
from pathlib import Path

from evaluation.results import build_result_payload, run_reviews, write_result
from openreview.review.report_writer import PotentialIssue, ReviewResult


def _instance() -> dict:
    return {
        "instance_id": "owner__repo@abc1234",
        "split": "dev",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "head_commit": "b" * 40,
        "pr_number": 7,
        "github_pr_url": "https://github.com/owner/repo/pull/7",
    }


def _result() -> ReviewResult:
    return ReviewResult(
        owner="owner",
        repo="repo",
        pr_number=7,
        summary="Fix add().",
        risk_level="HIGH",
        risk_reason="Logic bug.",
        potential_issues=[
            PotentialIssue(
                file="src/app.py",
                severity="high",
                category="bugs",
                confidence=0.9,
                reasoning="The operator is inverted.",
                suggestion="Use + instead of -.",
                line=12,
                evidence="return a + b",
                source_aspects=["bugs", "security"],
            )
        ],
        final_decision="REQUEST_CHANGES",
        final_summary="Fix it.",
        metrics={
            "llm_calls": 4,
            "llm_usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


def test_build_result_payload_matches_aacr_ocr_shape():
    payload = build_result_payload(
        _instance(), _result(), duration_seconds=1.25
    )

    assert payload["reviewer"] == "openreview"
    assert payload["status"] == "ok"
    assert payload["duration_seconds"] == 1.25
    assert payload["review"]["summary"]["input_tokens"] == 100
    assert payload["review"]["summary"]["output_tokens"] == 20
    comment = payload["review"]["comments"][0]
    assert comment["path"] == "src/app.py"
    assert comment["start_line"] == 12
    assert comment["end_line"] == 12
    assert "The operator is inverted." in comment["content"]
    assert "Suggestion: Use + instead of -." in comment["content"]
    assert comment["source_aspects"] == ["bugs", "security"]
    assert payload["token_usage"] == {"input_tokens": 100, "output_tokens": 20}


def test_write_result_uses_safe_instance_id(tmp_path: Path):
    payload = build_result_payload(
        _instance(), _result(), duration_seconds=0.5
    )
    path = write_result(tmp_path, payload)

    assert path.name == "owner__repo@abc1234.json"
    assert json.loads(path.read_text())["instance_id"] == "owner__repo@abc1234"


class _FakePipeline:
    def __init__(self, result: ReviewResult):
        self.last_result = result
        self.calls = []

    def run_local(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return Path("report.md")


def test_run_reviews_writes_summary_and_result(tmp_path: Path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.setattr(
        "evaluation.repos.ensure_repo", lambda *args, **kwargs: repo_path
    )
    pipeline = _FakePipeline(_result())

    rows = run_reviews(
        [_instance()],
        repo_cache=tmp_path / "cache",
        results_dir=tmp_path / "results",
        pipeline=pipeline,
    )

    assert rows[0]["status"] == "ok"
    assert rows[0]["issues"] == 1
    assert (tmp_path / "results" / "owner__repo@abc1234.json").exists()
    assert (tmp_path / "results" / "summary_openreview.json").exists()
    assert pipeline.calls[0][1]["owner"] == "owner"
    assert pipeline.calls[0][1]["repo"] == "repo"
