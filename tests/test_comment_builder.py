from openreview.review.comment_builder import (
    build_review_body,
    build_review_comments,
)
from openreview.review.report_writer import PotentialIssue, ReviewResult


def _result(issues):
    return ReviewResult(
        owner="org",
        repo="project",
        pr_number=1,
        summary="summary",
        risk_level="HIGH",
        risk_reason="reason",
        potential_issues=issues,
        final_decision="REQUEST_CHANGES",
        final_summary="summary",
    )


def test_inline_comments_are_built_for_line_level_issues():
    issue = PotentialIssue(
        file="src/a.py",
        severity="high",
        category="security",
        confidence=0.9,
        reasoning="Hardcoded secret.",
        suggestion="Use env vars.",
        line=10,
        end_line=12,
        evidence="password = '123'",
        suggested_patch="-password = '123'\n+password = os.environ['PASSWORD']",
    )

    comments = build_review_comments(_result([issue]))

    assert len(comments) == 1
    comment = comments[0]
    assert comment["path"] == "src/a.py"
    assert comment["line"] == 12
    assert comment["start_line"] == 10
    assert comment["side"] == "RIGHT"
    assert "Hardcoded secret." in comment["body"]
    assert "Use env vars." in comment["body"]
    assert "password = '123'" in comment["body"]


def test_file_level_issues_go_to_review_body():
    issue = PotentialIssue(
        file="src/a.py",
        severity="medium",
        category="clean_code",
        confidence=0.8,
        reasoning="File is too large.",
        suggestion="Split the module.",
    )

    result = _result([issue])
    comments = build_review_comments(result)
    body = build_review_body(result)

    assert comments == []
    assert "文件级问题" in body
    assert "File is too large." in body
