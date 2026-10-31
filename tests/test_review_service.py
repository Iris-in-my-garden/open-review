from pathlib import Path

from openreview.github.client import ChangedFile, PullRequestInfo
from openreview.retriever.code_context import CodeContext
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.report_writer import PotentialIssue, ReviewResult
from openreview.review.single_shot import SingleShotReviewer
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


class FakeContextRetriever:
    """返回带 context_id / symbol 的结构化上下文，用于验证指标链路。"""

    def __init__(self):
        self.calls = 0
        self.last_stats = {"index_symbol_hits": 1}

    def retrieve(self, repo_path, parsed_diff, base_path=None):
        self.calls += 1
        filename = parsed_diff.files[0].filename
        return [
            CodeContext(
                file_path=filename,
                reason="definition",
                snippet="def x(): ...",
                score=0.9,
                kind="definition",
                symbol="x",
                start_line=1,
                end_line=1,
            )
        ]


class FakeContextReviewer(FakeReviewer):
    def review(self, pr_info, parsed_diff, contexts, repo_path=None, aspects=None):
        result = super().review(
            pr_info, parsed_diff, contexts, repo_path=repo_path, aspects=aspects
        )
        result.potential_issues[0].context_refs = [
            context.ensure_context_id() for context in contexts
        ]
        return result


class FakeCachedReviewer(FakeReviewer):
    """模拟“reviewer 使用预算客户端但本次全部命中缓存”的情况。"""

    class _CachedClient:
        has_budget = True

    def __init__(self):
        super().__init__()
        self.llm_client = self._CachedClient()


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
    pipeline.retriever = FakeContextRetriever()
    pipeline.reviewer = FakeReviewer()

    first = pipeline.run(
        "https://github.com/org/repo/pull/1", tmp_path / "reports"
    )
    first_metrics = dict(pipeline.last_result.metrics)
    second = pipeline.run(
        "https://github.com/org/repo/pull/1", tmp_path / "reports"
    )
    second_metrics = dict(pipeline.last_result.metrics)

    assert first == second
    assert len(pipeline.reviewer.calls) == 1
    assert pipeline.repo_mgr.base_calls == 1
    content = second.read_text(encoding="utf-8")
    assert "命中本地 review 缓存" in content
    assert "**Files**: 2" in content

    # cache hit 时 analyze/verify 被跳过，但报告不能把上一次 run 的
    # files / batches / aspects / retrieval 指标覆盖成 0。
    assert first_metrics["files"] == 2
    assert second_metrics["files"] == first_metrics["files"]
    assert second_metrics["batches"] == first_metrics["batches"]
    assert second_metrics["aspects"] == first_metrics["aspects"]
    assert second_metrics["retrieval"]["contexts"] == 1
    assert second_metrics["retrieval"] == first_metrics["retrieval"]
    assert second_metrics["metrics_source"] == "review_cache"
    assert second_metrics["cache"]["served_from_cache"] is True
    assert second_metrics["cache"]["review_hit"] is True


def test_pipeline_surfaces_retrieval_metrics_and_context_refs(tmp_path: Path):
    files = [
        ChangedFile(
            filename=f"src/file_{index}.py",
            status="modified",
            additions=1,
            deletions=0,
            changes=1,
            patch="@@ -0,0 +1,1 @@\n+def x():\n",
        )
        for index in range(2)
    ]
    pipeline = ReviewPipeline(mock_llm=True, max_batch_tokens=1)
    pipeline.gh_client = FakeGitHubClient(files=files)
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeContextRetriever()
    pipeline.reviewer = FakeContextReviewer()

    pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")

    assert pipeline.last_state is not None
    result = pipeline.last_state.review_result
    assert result is not None
    retrieval = result.metrics["retrieval"]
    assert retrieval["contexts"] == 2
    assert retrieval["unique_contexts"] == 2
    assert retrieval["duplicate_contexts"] == 0
    assert retrieval["context_file_list"] == ["src/file_0.py", "src/file_1.py"]
    assert retrieval["index_context_file_list"] == [
        "src/file_0.py",
        "src/file_1.py",
    ]
    assert retrieval["context_kinds"] == {"definition": 2}
    assert retrieval["stats"] == {"index_symbol_hits": 2}
    assert retrieval["symbol_coverage"] == 1.0

    # context_refs 从 prompt 上下文 -> finding -> validator -> result 全链路保留。
    assert all(issue.context_refs for issue in result.potential_issues)

    pipeline.close()


def test_pipeline_llm_usage_is_per_run_not_cumulative(tmp_path: Path):
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=10_000,
        verifier_enabled=False,
    )
    pipeline.gh_client = FakeGitHubClient(
        files=[
            ChangedFile(
                filename="src/file_0.py",
                status="modified",
                additions=1,
                deletions=0,
                changes=1,
                patch="@@ -0,0 +1,1 @@\n+x = 1\n",
            )
        ]
    )
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()

    pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")
    first = dict(pipeline.last_result.metrics["llm_usage"])
    pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")
    second = dict(pipeline.last_result.metrics["llm_usage"])

    # pipeline 在评测中会被复用；第二次 run 的报告不能包含第一次的调用。
    assert first["calls"] > 0
    assert second["calls"] == first["calls"]

    pipeline.close()


def test_pipeline_does_not_charge_budget_for_client_side_cache_hits(tmp_path: Path):
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=1,
        budget_calls=10,
        verifier_enabled=False,
    )
    pipeline.gh_client = FakeGitHubClient(
        files=[
            ChangedFile(
                filename="src/file_0.py",
                status="modified",
                additions=1,
                deletions=0,
                changes=1,
                patch="@@ -0,0 +1,1 @@\n+x = 1\n",
            )
        ]
    )
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()
    pipeline.reviewer = FakeCachedReviewer()

    pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")

    assert pipeline.last_state is not None
    # reviewer 自带预算客户端且本次没有真实调用（缓存命中），
    # _node_analyze 不应再按维度数补扣预算。
    assert pipeline.last_state.budget.used_llm_calls == 0

    pipeline.close()


def test_pipeline_defaults_to_single_shot_one_call_per_batch(tmp_path: Path):
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=1,
        verifier_enabled=False,
    )
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()

    try:
        pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")

        assert isinstance(pipeline.reviewer, SingleShotReviewer)
        assert pipeline.last_result is not None
        metrics = pipeline.last_result.metrics
        assert metrics["reviewer_mode"] == "single"
        # 两个文件各成一批，single-pass 每批 1 次调用。
        assert metrics["batches"] == 2
        assert metrics["llm_calls"] == 2
    finally:
        pipeline.close()


def test_pipeline_multi_aspect_mode_keeps_four_calls_per_batch(tmp_path: Path):
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=1,
        reviewer_mode="multi_aspect",
        verifier_enabled=False,
    )
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()

    try:
        pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")

        assert isinstance(pipeline.reviewer, ReviewOrchestrator)
        assert not isinstance(pipeline.reviewer, SingleShotReviewer)
        assert pipeline.last_result is not None
        metrics = pipeline.last_result.metrics
        assert metrics["reviewer_mode"] == "multi_aspect"
        assert metrics["batches"] == 2
        assert metrics["llm_calls"] == 8
    finally:
        pipeline.close()


def test_single_shot_budget_does_not_partially_run_aspects(tmp_path: Path):
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=1,
        budget_calls=1,
        verifier_enabled=False,
    )
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()

    try:
        pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")

        assert pipeline.last_result is not None
        assert pipeline.last_state is not None
        assert pipeline.last_result.metrics["llm_calls"] == 1
        assert any(
            "LLM 调用预算已用尽" in note for note in pipeline.last_state.notes
        )
    finally:
        pipeline.close()
