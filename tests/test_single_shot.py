from typing import Type, TypeVar

from pydantic import BaseModel

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.review.output_parser import PotentialIssueSchema
from openreview.review.single_shot import SingleShotReviewer

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


class CapturingLLM:
    def __init__(self):
        self.calls = []

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        self.calls.append(user_prompt)
        return response_schema(aspect_summary="ok", issues=[_issue()])


def test_single_shot_uses_one_call_and_keeps_triage_focus():
    llm = CapturingLLM()
    reviewer = SingleShotReviewer(llm_client=llm, aspect_timeout=5)

    result = reviewer.review(
        _pr(),
        _diff(),
        [],
        aspects=["clean_code", "bugs"],
    )

    assert len(llm.calls) == 1
    assert "本次 triage 重点" in llm.calls[0]
    assert "代码规范与可维护性" in llm.calls[0]
    assert "逻辑错误与边界条件" in llm.calls[0]
    assert len(result.potential_issues) == 1
    # single_shot 没有独立维度，用 finding 的 category 做归因。
    assert result.potential_issues[0].source_aspects == ["bugs"]
