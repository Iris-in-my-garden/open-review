from types import SimpleNamespace

from evaluation.context_dry_run import build_context_dry_run
from openreview.domain.models import PullRequestInfo
from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.code_context import CodeContext, ContextPriority
from openreview.review.prompt_builder import PromptBuilder


def test_context_dry_run_reports_context_and_prompt_estimates(tmp_path):
    parsed_diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/example.py",
                status="modified",
                additions=1,
                deletions=1,
                patch="@@ -1 +1 @@\n-old()\n+new()",
                changed_symbols=["new"],
            )
        ]
    )
    context = CodeContext(
        file_path="src/example.py",
        reason="enclosing function",
        snippet="def new():\n    return True",
        score=1.0,
        kind="enclosing",
        symbol="new",
        related_changed_symbols=["new"],
        start_line=1,
        end_line=2,
        priority=ContextPriority.ENCLOSING,
    )
    state = SimpleNamespace(
        local_workspace=None,
        retrieval_stats={},
        local_repo_path=tmp_path,
        base_path=None,
        selected_aspects=["bugs"],
    )
    prompt_builder = PromptBuilder()
    executor = SimpleNamespace(
        _node_load_local=lambda current: setattr(
            current,
            "pr_info",
            PullRequestInfo(
                owner="owner",
                repo="repo",
                pr_number=1,
                title="AACR fixture",
                base_branch="base",
                head_branch="head",
                base_sha="b" * 40,
                head_sha="h" * 40,
                html_url="",
            ),
        ),
        _node_prepare_local=lambda current: None,
        _node_parse_diff=lambda current: setattr(
            current, "parsed_diff", parsed_diff
        ),
        _node_triage=lambda current: None,
        batch_planner=SimpleNamespace(plan=lambda current, retriever: [parsed_diff]),
        evidence_pipeline=SimpleNamespace(
            collect=lambda current: ([], []),
            extra_contexts=lambda *args, **kwargs: [],
        ),
    )
    session = SimpleNamespace(
        executor=executor,
        retriever=SimpleNamespace(retrieve=lambda *args, **kwargs: [context]),
        reviewer=SimpleNamespace(
            prompt_builder=prompt_builder,
            _focus_instruction=lambda aspects: "",
        ),
        new_state=lambda *args, **kwargs: state,
    )
    pipeline = SimpleNamespace(
        _new_session=lambda: session,
        config=SimpleNamespace(max_context_tokens=1000, max_completion_tokens=20),
    )

    report = build_context_dry_run(
        {
            "instance_id": "owner__repo@abc1234",
            "repo": "owner/repo",
            "base_commit": "b" * 40,
            "head_commit": "h" * 40,
        },
        repo_path=tmp_path,
        pipeline=pipeline,
    )

    batch = report["batches"][0]
    context_report = batch["contexts"][0]
    assert report["llm_called"] is False
    assert context_report["text"] == context.snippet
    assert context_report["type"] == "enclosing"
    assert context_report["reason"] == "enclosing function"
    assert context_report["related_changed_symbols"] == ["new"]
    assert batch["changed_symbols"] == ["new"]
    assert context_report["priority"] == "P2"
    assert context_report["snippet_tokens_estimate"] > 0
    assert context_report["prompt_segment_tokens_estimate"] > 0
    assert context_report["included_in_prompt"] is True
    assert batch["final_prompt_tokens_estimate"] == (
        batch["system_prompt_tokens_estimate"]
        + batch["user_prompt_tokens_estimate"]
    )
