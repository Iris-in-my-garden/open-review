# tests/test_reviewer.py
from unittest.mock import MagicMock, patch
import pytest

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.grep_retriever import CodeContext
from openreview.review.reviewer import Reviewer, ReviewResultSchema, PotentialIssueSchema


@patch("openreview.reviewer.OpenAI")
def test_reviewer_review_success(mock_openai_class):
    # 构造 Mock 返回数据
    mock_parsed_result = ReviewResultSchema(
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

    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(parsed=mock_parsed_result))]

    mock_client = MagicMock()
    mock_client.beta.chat.completions.parse.return_value = mock_response
    mock_openai_class.return_value = mock_client

    # 执行 Review
    reviewer = Reviewer(api_key="sk-test-key")

    pr_info = {
        "title": "Feature: Auth",
        "body": "Add login functionality",
        "number": 42,
        "base": {"repo": {"name": "demo-repo", "owner": {"login": "demo-owner"}}},
    }
    parsed_diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/auth.py",
                status="modified",
                patch="@@ -0,0 +1,5 @@\n+def login(): pass",
            )
        ]
    )
    contexts = [
        CodeContext(
            file_path="src/config.py",
            reason="Reference",
            snippet="SECRET='123'",
            score=0.8,
        )
    ]

    result = reviewer.review(pr_info, parsed_diff, contexts)

    # 校验调用与解析
    assert result.owner == "demo-owner"
    assert result.repo == "demo-repo"
    assert result.pr_number == 42
    assert result.risk_level == "Medium"
    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].file == "src/auth.py"
    assert result.final_decision == "Request Changes"

    # 校验 OpenAI 方法是否被正确调用
    mock_client.beta.chat.completions.parse.assert_called_once()