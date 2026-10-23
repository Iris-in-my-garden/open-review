from pathlib import Path

from openreview.github.client import ChangedFile, PullRequestInfo
from openreview.review.report_writer import PotentialIssue, ReviewResult
from openreview.services.review_service import ReviewPipeline


class FakeGitHubClient:
    def __init__(self, files=None):
        self.reviews = []
        self.files = files

    def get_pull_request(self, ref):
        return PullRequestInfo(
            owner=ref.owner,
            repo=ref.repo,
            pr_number=ref.pr_number,
            title="PR title",
            body="PR body",
            base_branch="main",
            head_branch="feature",
            base_sha="base-sha",
            head_sha="head-sha",
            html_url="https://github.com/org/repo/pull/1",
        )

    def list_changed_files(self, ref):
        if self.files is not None:
            return self.files
        return [
            ChangedFile(
                filename=f"src/file_{index}.py",
                status="modified",
                additions=1,
                deletions=0,
                changes=1,
                patch="@@ -0,0 +1,1 @@\n+x = 1\n",
            )
            for index in range(2)
        ]

    def create_review(self, ref, body, comments, event="COMMENT", commit_id=None):
        self.reviews.append(
            {
                "ref": ref,
                "body": body,
                "comments": comments,
                "event": event,
                "commit_id": commit_id,
            }
        )
        return {"id": 1}

    def close(self):
        pass


class FakeRepoManager:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.base_calls = 0

    def prepare_pr_repo(self, owner, repo, pr_number):
        return self.repo_path

    def prepare_base_worktree(self, owner, repo, base_sha):
        self.base_calls += 1
        return self.repo_path


class FakeRetriever:
    def __init__(self):
        self.calls = []

    def retrieve(self, repo_path, parsed_diff, base_path=None):
        self.calls.append(base_path)
        return []


class FakeReviewer:
    def __init__(self):
        self.calls = []

    def review(self, pr_info, parsed_diff, contexts, repo_path=None, aspects=None):
        self.calls.append((parsed_diff, repo_path))
        return ReviewResult(
            owner=pr_info.owner,
            repo=pr_info.repo,
            pr_number=pr_info.pr_number,
            summary=f"batch {len(self.calls)}",
            risk_level="HIGH",
            risk_reason="reason",
            potential_issues=[
                PotentialIssue(
                    file=parsed_diff.files[0].filename,
                    severity="high",
                    category="security",
                    confidence=0.9,
                    reasoning="Issue.",
                    suggestion="Fix it.",
                    line=1,
                )
            ],
            final_decision="REQUEST_CHANGES",
            final_summary="summary",
            review_notes=[],
        )


def test_pipeline_batches_merges_and_posts_comments(tmp_path: Path):
    pipeline = ReviewPipeline(mock_llm=True, max_batch_tokens=1)
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()
    pipeline.reviewer = FakeReviewer()

    report_path = pipeline.run(
        "https://github.com/org/repo/pull/1",
        tmp_path / "reports",
        post_comments=True,
    )

    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "2 个批次" in content
    assert "## 8. Review Notes" in content
    assert "### Run Metrics" in content
    assert "**Batches**: 2" in content

    # 两个文件各成一批，检索器与 reviewer 都被调用两次
    assert len(pipeline.retriever.calls) == 2
    assert len(pipeline.reviewer.calls) == 2
    assert pipeline.repo_mgr.base_calls == 1

    # 行内评论已发回 GitHub
    assert len(pipeline.gh_client.reviews) == 1
    review = pipeline.gh_client.reviews[0]
    assert review["event"] == "COMMENT"
    assert review["commit_id"] == "head-sha"
    assert len(review["comments"]) == 2
    assert review["comments"][0]["line"] == 1

    pipeline.close()


def test_pipeline_triage_skips_llm_for_docs_only_pr(tmp_path: Path):
    pipeline = ReviewPipeline(mock_llm=True)
    pipeline.gh_client = FakeGitHubClient(
        files=[
            ChangedFile(
                filename="README.md",
                status="modified",
                additions=1,
                deletions=0,
                changes=1,
                patch="@@ -0,0 +1,1 @@\n+docs\n",
            )
        ]
    )
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()
    pipeline.reviewer = FakeReviewer()

    report_path = pipeline.run(
        "https://github.com/org/repo/pull/1", tmp_path / "reports"
    )

    content = report_path.read_text(encoding="utf-8")
    assert pipeline.reviewer.calls == []
    assert pipeline.retriever.calls == []
    assert "未运行 LLM 审查" in content
    assert "**LLM Calls**: 0" in content


def test_pipeline_degrades_gracefully_when_call_budget_runs_out(tmp_path: Path):
    pipeline = ReviewPipeline(mock_llm=True, max_batch_tokens=1, budget_calls=2)
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()
    pipeline.reviewer = FakeReviewer()

    report_path = pipeline.run(
        "https://github.com/org/repo/pull/1", tmp_path / "reports"
    )

    content = report_path.read_text(encoding="utf-8")
    # 第一个批次用 2 个维度的预算跑完，第二个批次因预算耗尽被跳过
    assert len(pipeline.reviewer.calls) == 1
    assert "预算" in content


def test_pipeline_review_cache_skips_second_run(tmp_path: Path):
    pipeline = ReviewPipeline(
        mock_llm=True,
        review_cache_enabled=True,
        review_cache_dir=str(tmp_path / "cache"),
    )
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()
    pipeline.reviewer = FakeReviewer()

    first = pipeline.run(
        "https://github.com/org/repo/pull/1", tmp_path / "reports"
    )
    second = pipeline.run(
        "https://github.com/org/repo/pull/1", tmp_path / "reports"
    )

    assert first == second
    assert len(pipeline.reviewer.calls) == 1
    assert pipeline.repo_mgr.base_calls == 1
    content = second.read_text(encoding="utf-8")
    assert "命中本地 review 缓存" in content

