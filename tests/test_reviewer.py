# tests/test_reviewer.py
from typing import Type, TypeVar

from pydantic import BaseModel

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.output_parser import (
    PotentialIssueSchema,
    ReviewResultSchema,
)
from openreview.review.simple_reviewer import Reviewer

T = TypeVar("T", bound=BaseModel)


class FakeLLMClient:
    """按 Schema 返回预置结果，避免测试依赖真实 OpenAI SDK。"""

    def __init__(self, result: BaseModel):
        self.result = result
        self.calls = []

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_schema": response_schema,
            }
        )
        return self.result


def _pr_info() -> PullRequestInfo:
    return PullRequestInfo(
        owner="demo-owner",
        repo="demo-repo",
        pr_number=42,
        title="Feature: Auth",
        body="Add login functionality",
        base_branch="main",
        head_branch="feature",
        base_sha="base-sha",
        head_sha="head-sha",
        html_url="https://github.com/demo-owner/demo-repo/pull/42",
    )


def _parsed_diff() -> ParsedDiff:
    return ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/auth.py",
                status="modified",
                patch="@@ -0,0 +1,5 @@\n+def login(): pass",
                changed_symbols=["login"],
                changed_lines=[1],
            )
        ]
    )


def test_reviewer_review_success():
    mock_result = ReviewResultSchema(
        summary="Add user authentication endpoint.",
        risk_level="Medium",
        risk_reason="Involves auth logic changes.",
        key_changes=["Add login route in auth.py"],
        potential_issues=[
            PotentialIssueSchema(
                file="src/auth.py",
                severity="High",
                category="Security",
                confidence=0.9,
                reasoning="Missing password hashing.",
                suggestion="Use bcrypt to hash passwords.",
            )
        ],
        missing_test_suggestions=["Add integration tests for login."],
        evidence=["Line 10 in auth.py"],
        final_decision="Request Changes",
        final_summary="Please fix the password hashing issue.",
    )
    llm = FakeLLMClient(mock_result)

    reviewer = Reviewer(llm_client=llm)
    contexts = [
        CodeContext(
            file_path="src/config.py",
            reason="Reference",
            snippet="SECRET='123'",
            score=0.8,
        )
    ]

    result = reviewer.review(_pr_info(), _parsed_diff(), contexts)

    # 校验调用与解析
    assert result.owner == "demo-owner"
    assert result.repo == "demo-repo"
    assert result.pr_number == 42
    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].file == "src/auth.py"
    # severity 会被归一化为小写，便于跨维度排序
    assert result.potential_issues[0].severity == "high"
    # 风险等级与决策由校验后的结果统一重算
    assert result.risk_level == "HIGH"
    assert result.final_decision == "REQUEST_CHANGES"

    assert len(llm.calls) == 1
    assert llm.calls[0]["response_schema"] is ReviewResultSchema
    assert "src/auth.py" in llm.calls[0]["user_prompt"]
