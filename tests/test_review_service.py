import json
from pathlib import Path

import pytest

from openreview.github.client import ChangedFile, PullRequestInfo
from openreview.retriever.code_context import CodeContext
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.report_writer import PotentialIssue, ReviewResult
from openreview.review.single_shot import SingleShotReviewer
from openreview.review.token_budget import (
    DEFAULT_HARD_PROMPT_TOKENS,
    DEFAULT_SOFT_PROMPT_TOKENS,
    context_reserve_tokens_for,
)
from openreview.review.workflow import ReviewStatus
from openreview.services.review_executor import ReviewExecutor
from openreview.services.review_service import ReviewPipeline


def test_narrow_aspects_keeps_security_when_budget_is_tight():
    narrowed = ReviewExecutor._narrow_aspects(
        ["clean_code", "bugs", "performance", "security"], 1
    )

    assert narrowed == ["security"]


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

    def review(
        self,
        pr_info,
        parsed_diff,
        contexts,
        repo_path=None,
        aspects=None,
    ):
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


class FakeGraphContextRetriever(FakeContextRetriever):
    """模拟 call graph / import graph 实际注入的上下文。"""

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
            ),
            CodeContext(
                file_path="src/api.py",
                reason="caller `handle` calls changed symbol `x`",
                snippet="def handle():\n    x()",
                score=0.58,
                kind="caller",
                symbol="handle",
                start_line=1,
                end_line=2,
            ),
            CodeContext(
                file_path="src/app.py",
                reason="import of `src.file_0`",
                snippet="from src.file_0 import x",
                score=0.6,
                kind="import",
                symbol="x",
                start_line=1,
                end_line=1,
            ),
        ]


class FakeContextReviewer(FakeReviewer):
    def review(
        self,
        pr_info,
        parsed_diff,
        contexts,
        repo_path=None,
        aspects=None,
    ):
        result = super().review(
            pr_info,
            parsed_diff,
            contexts,
            repo_path=repo_path,
            aspects=aspects,
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


class FlakyReviewer(FakeReviewer):
    """第一批失败、后续批次成功，用于验证批级故障隔离。"""

    def __init__(self, fail_all: bool = False):
        super().__init__()
        self.fail_all = fail_all

    def review(
        self,
        pr_info,
        parsed_diff,
        contexts,
        repo_path=None,
        aspects=None,
    ):
        self.calls.append((parsed_diff, repo_path))
        if self.fail_all or len(self.calls) == 1:
            raise RuntimeError("provider timeout")
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
    assert pipeline.last_outcome is not None
    assert pipeline.last_outcome.status == ReviewStatus.PARTIAL
    assert pipeline.last_outcome.budget_exhausted is True
    assert pipeline.last_result is not None
    assert pipeline.last_result.metrics["outcome"]["status"] == "partial"


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


def test_pipeline_reports_actual_graph_contexts(tmp_path: Path):
    files = [
        ChangedFile(
            filename="src/file_0.py",
            status="modified",
            additions=1,
            deletions=0,
            changes=1,
            patch="@@ -0,0 +1,1 @@\n+def x():\n",
        )
    ]
    pipeline = ReviewPipeline(mock_llm=True, max_batch_tokens=1)
    pipeline.gh_client = FakeGitHubClient(files=files)
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeGraphContextRetriever()
    pipeline.reviewer = FakeContextReviewer()

    try:
        pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")

        retrieval = pipeline.last_result.metrics["retrieval"]
        assert retrieval["context_kinds"] == {
            "caller": 1,
            "definition": 1,
            "import": 1,
        }
        assert retrieval["graph_contexts"] == 2
        assert retrieval["graph_context_kinds"] == {"caller": 1, "import": 1}
        assert retrieval["graph_context_files"] == 2
        assert retrieval["graph_enabled"] is True
    finally:
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


def test_pipeline_isolates_batch_failure_and_keeps_successful_batches(
    tmp_path: Path,
):
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=1,
        verifier_enabled=False,
    )
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()
    pipeline.reviewer = FlakyReviewer()

    try:
        report_path = pipeline.run(
            "https://github.com/org/repo/pull/1", tmp_path / "reports"
        )

        # 两个文件各成一批：第一批失败，第二批仍然完成并写入报告。
        assert len(pipeline.reviewer.calls) == 2
        assert report_path.exists()
        assert pipeline.last_result is not None
        assert len(pipeline.last_result.potential_issues) == 1
        assert pipeline.last_state is not None
        assert any(
            "批次 1/2 审查失败" in note for note in pipeline.last_state.notes
        )
        assert pipeline.last_outcome is not None
        assert pipeline.last_outcome.status == ReviewStatus.PARTIAL
        assert pipeline.last_outcome.failed_batches == 1
        assert pipeline.last_outcome.total_batches == 2
        assert pipeline.last_result.metrics["outcome"]["status"] == "partial"
        assert pipeline.last_result.metrics["failed_batches"] == 1
    finally:
        pipeline.close()


def test_pipeline_raises_when_every_batch_fails(tmp_path: Path):
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=1,
        verifier_enabled=False,
    )
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()
    pipeline.reviewer = FlakyReviewer(fail_all=True)

    try:
        with pytest.raises(RuntimeError, match="batch\\(es\\) failed"):
            pipeline.run(
                "https://github.com/org/repo/pull/1", tmp_path / "reports"
            )
        # 单个批次失败不会中断循环，所有批次都被尝试过。
        assert len(pipeline.reviewer.calls) == 2
        assert pipeline.last_outcome is not None
        assert pipeline.last_outcome.status == ReviewStatus.FAILED
        assert pipeline.last_outcome.failed_batches == 2
    finally:
        pipeline.close()


def test_partial_review_is_not_written_to_review_cache(tmp_path: Path):
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=1,
        verifier_enabled=False,
        review_cache_enabled=True,
        review_cache_dir=str(tmp_path / "cache"),
    )
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()
    pipeline.reviewer = FlakyReviewer()

    try:
        pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")
        assert pipeline.last_outcome is not None
        assert pipeline.last_outcome.status == ReviewStatus.PARTIAL
        assert len(pipeline.reviewer.calls) == 2

        # PARTIAL 结果不能写 cache，否则下一次 run 会误命中并伪装成完整成功。
        pipeline.run("https://github.com/org/repo/pull/1", tmp_path / "reports")
        assert len(pipeline.reviewer.calls) == 4
    finally:
        pipeline.close()


def test_review_cache_key_includes_output_affecting_config(tmp_path: Path):
    def key_for(**kwargs) -> str:
        pipeline = ReviewPipeline(
            mock_llm=True,
            hybrid_retrieval_enabled=False,
            verifier_enabled=False,
            **kwargs,
        )
        try:
            executor = pipeline._executor()
            state = executor.new_state(
                "https://github.com/org/repo/pull/1", tmp_path
            )
            state.pr_info = PullRequestInfo(
                owner="org",
                repo="repo",
                pr_number=1,
                title="PR",
                body="",
                base_branch="main",
                head_branch="feature",
                base_sha="base-sha",
                head_sha="head-sha",
                html_url="https://github.com/org/repo/pull/1",
            )
            return executor._cache_key(state)
        finally:
            pipeline.close()

    base = key_for()
    # 以下配置都会改变 prompt / batching / 后处理结果，必须进入 cache key。
    assert key_for(max_batch_tokens=1) != base
    assert key_for(max_patch_tokens=100) != base
    assert key_for(max_context_tokens=100) != base
    assert key_for(soft_prompt_tokens=9_000) != base
    assert key_for(hard_prompt_tokens=15_000) != base
    assert key_for(static_tools=("ruff", "mypy")) != base
    assert key_for(verify_severities=("high",)) != base
    assert key_for(max_workers=2) != base
    assert key_for(aspect_timeout=10.0) != base


def test_pipeline_derives_effective_prompt_caps_internally():
    pipeline = ReviewPipeline(mock_llm=True)
    try:
        # 外部传入的 max_patch/context 只是硬上限；真正的预算由内部策略决定。
        assert pipeline.config.max_patch_tokens == DEFAULT_HARD_PROMPT_TOKENS
        assert pipeline.config.max_context_tokens == context_reserve_tokens_for(
            DEFAULT_SOFT_PROMPT_TOKENS
        )
        assert pipeline.config.soft_prompt_tokens == DEFAULT_SOFT_PROMPT_TOKENS
        assert pipeline.config.hard_prompt_tokens == DEFAULT_HARD_PROMPT_TOKENS
    finally:
        pipeline.close()


def test_debug_dir_is_not_part_of_review_cache_key(tmp_path: Path):
    def key_for(debug_dir: str) -> str:
        pipeline = ReviewPipeline(
            mock_llm=True,
            hybrid_retrieval_enabled=False,
            verifier_enabled=False,
            debug_dir=debug_dir,
        )
        try:
            executor = pipeline._executor()
            state = executor.new_state(
                "https://github.com/org/repo/pull/1", tmp_path
            )
            state.pr_info = PullRequestInfo(
                owner="org",
                repo="repo",
                pr_number=1,
                title="PR",
                body="",
                base_branch="main",
                head_branch="feature",
                base_sha="base-sha",
                head_sha="head-sha",
                html_url="https://github.com/org/repo/pull/1",
            )
            return executor._cache_key(state)
        finally:
            pipeline.close()

    assert key_for(str(tmp_path / "a")) == key_for(str(tmp_path / "b"))


def test_pipeline_dumps_prompts_responses_and_finding_decisions(tmp_path: Path):
    debug_dir = tmp_path / "debug"
    pipeline = ReviewPipeline(
        mock_llm=True,
        max_batch_tokens=1,
        debug_dir=str(debug_dir),
    )
    pipeline.gh_client = FakeGitHubClient()
    pipeline.repo_mgr = FakeRepoManager(tmp_path)
    pipeline.retriever = FakeRetriever()

    try:
        pipeline.run(
            "https://github.com/org/repo/pull/1", tmp_path / "reports"
        )

        # 两个文件各成一批：prompt / response / validation 都应有 2 份。
        prompts = sorted((debug_dir / "prompts").glob("*.user.txt"))
        assert len(prompts) == 2
        assert "untrusted_input" in prompts[0].read_text(encoding="utf-8")

        responses = sorted((debug_dir / "llm").glob("*.parsed.json"))
        assert len(responses) == 2

        validation = sorted((debug_dir / "validation").glob("*.json"))
        assert len(validation) == 2
        records = json.loads(validation[0].read_text(encoding="utf-8"))
        assert records
        assert {"status", "confidence_before", "confidence_after"} <= set(
            records[0]
        )

        # verifier 至少处理了 high 问题。
        verifier_files = sorted((debug_dir / "verifier").glob("*.json"))
        assert verifier_files

        state = json.loads(
            (debug_dir / "state.json").read_text(encoding="utf-8")
        )
        assert len(state["batches"]) == 2
        assert state["stats"]["validation"]["candidates"] >= 1
        assert (debug_dir / "metrics.json").exists()
    finally:
        pipeline.close()
