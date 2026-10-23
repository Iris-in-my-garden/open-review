from typing import Type, TypeVar

import pytest
from pydantic import BaseModel

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.assessment import merge_review_results
from openreview.review.output_parser import (
    PotentialIssueSchema,
)
from openreview.review.report_writer import PotentialIssue, ReviewResult

T = TypeVar("T", bound=BaseModel)


def _pr() -> PullRequestInfo:
    return PullRequestInfo(
        owner="org",
        repo="project",
        pr_number=1,
        title="title",
        body="body",
        base_branch="main",
        head_branch="feature",
        base_sha="base",
        head_sha="head",
        html_url="https://github.com/org/project/pull/1",
    )


def _diff() -> ParsedDiff:
    return ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/a.py",
                status="modified",
                patch="@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n",
                changed_lines=[1, 2],
                visible_lines=[1, 2],
            )
        ]
    )


def _issue() -> PotentialIssueSchema:
    return PotentialIssueSchema(
        file="src/a.py",
        severity="high",
        category="bugs",
        confidence=0.9,
        reasoning="Potential null dereference in this branch.",
        suggestion="Add a guard.",
        line=1,
        evidence="x = 1",
    )


class AspectFailingLLM:
    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        if "QA" in system_prompt:  # bugs 维度
            raise RuntimeError("simulated provider error")
        return response_schema(aspect_summary="ok", issues=[_issue()])


class AlwaysFailingLLM:
    def generate_structured(self, system_prompt, user_prompt, response_schema):
        raise RuntimeError("provider down")


def test_one_aspect_failure_is_surfaced_in_review_notes():
    orchestrator = ReviewOrchestrator(
        llm_client=AspectFailingLLM(),
        aspects=["bugs", "security"],
        max_attempts=1,
        aspect_timeout=5,
    )

    result = orchestrator.review(_pr(), _diff(), [])

    assert len(result.potential_issues) == 1
    assert any("`bugs`" in note for note in result.review_notes)
    assert result.risk_level == "HIGH"


def test_all_aspects_failing_raises():
    orchestrator = ReviewOrchestrator(
        llm_client=AlwaysFailingLLM(),
        aspects=["bugs", "security"],
        max_attempts=1,
        aspect_timeout=5,
    )

    with pytest.raises(RuntimeError, match="所有维度"):
        orchestrator.review(_pr(), _diff(), [])


def _result(issues, notes=None) -> ReviewResult:
    return ReviewResult(
        owner="org",
        repo="project",
        pr_number=1,
        summary="batch summary",
        risk_level="HIGH",
        risk_reason="pre",
        potential_issues=issues,
        final_decision="REQUEST_CHANGES",
        final_summary="pre",
        review_notes=notes or [],
    )


class AlwaysSameIssueLLM:
    """每个维度都返回同一个问题，用于验证跨维度归因。"""

    def generate_structured(self, system_prompt, user_prompt, response_schema):
        return response_schema(aspect_summary="ok", issues=[_issue()])


def test_aggregate_merges_source_aspects_for_duplicate_findings():
    orchestrator = ReviewOrchestrator(
        llm_client=AlwaysSameIssueLLM(),
        aspects=["bugs", "security"],
    )

    result = orchestrator.review(_pr(), _diff(), [])

    assert len(result.potential_issues) == 1
    assert set(result.potential_issues[0].source_aspects) == {"bugs", "security"}


def test_merge_review_results_deduplicates_and_recomputes():
    duplicate_high = PotentialIssue(
        file="src/a.py",
        severity="high",
        category="bugs",
        confidence=0.9,
        reasoning="Same issue.",
        suggestion="Fix.",
        line=1,
    )
    duplicate_critical = PotentialIssue(
        file="src/a.py",
        severity="critical",
        category="security",
        confidence=0.8,
        reasoning="same issue.",
        suggestion="Fix.",
        line=1,
    )

    merged = merge_review_results(
        [
            _result([duplicate_high], notes=["batch 1"]),
            _result([duplicate_critical], notes=["batch 2"]),
        ]
    )

    assert len(merged.potential_issues) == 1
    assert merged.potential_issues[0].severity == "critical"
    assert merged.risk_level == "CRITICAL"
    assert merged.final_decision == "REQUEST_CHANGES"
    assert merged.review_notes == ["batch 1", "batch 2"]
    assert "2 个批次" in merged.final_summary
