from openreview.domain.markers import COMMENT_MARKER_PREFIX, REVIEW_MARKER
from openreview.review.comment_builder import (
    MAX_INLINE_COMMENTS,
    build_review_body,
    build_review_comments,
    select_inline_issues,
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


def test_deleted_line_issue_uses_left_side():
    issue = PotentialIssue(
        file="src/a.py",
        severity="high",
        category="security",
        confidence=0.9,
        reasoning="A required validation was deleted.",
        suggestion="Restore it.",
        line=8,
        side="LEFT",
    )

    comments = build_review_comments(_result([issue]))

    assert comments[0]["side"] == "LEFT"
    assert comments[0]["line"] == 8


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
    assert "其他问题" in body
    assert "File is too large." in body


def test_only_high_signal_findings_become_inline_comments():
    """低价值 finding 留在 summary，避免 bot 刷屏训练开发者忽略评论。"""
    medium = PotentialIssue(
        file="src/a.py",
        severity="medium",
        category="clean_code",
        confidence=0.8,
        reasoning="Could be shorter.",
        suggestion="Refactor.",
        line=3,
    )
    low = PotentialIssue(
        file="src/b.py",
        severity="low",
        category="style",
        confidence=0.6,
        reasoning="Minor style.",
        suggestion="Rename.",
        line=5,
    )
    critical = PotentialIssue(
        file="src/c.py",
        severity="critical",
        category="security",
        confidence=0.95,
        reasoning="SQL injection.",
        suggestion="Parameterize.",
        line=7,
    )

    result = _result([medium, low, critical])
    issues = select_inline_issues(result)
    comments = build_review_comments(result, inline_issues=issues)
    body = build_review_body(result, inline_issues=issues)

    assert [(c["path"], c["line"]) for c in comments] == [("src/c.py", 7)]
    assert "Could be shorter." in body
    assert "Minor style." in body
    assert "SQL injection." not in body


def test_inline_comments_are_capped_but_all_findings_are_reported():
    issues = [
        PotentialIssue(
            file=f"src/f{index}.py",
            severity="high",
            category="security",
            confidence=0.9,
            reasoning=f"Secret {index}.",
            suggestion="Use env vars.",
            line=index + 1,
        )
        for index in range(MAX_INLINE_COMMENTS + 3)
    ]

    result = _result(issues)
    selected = select_inline_issues(result)
    comments = build_review_comments(result, inline_issues=selected)
    body = build_review_body(result, inline_issues=selected)

    assert len(comments) == MAX_INLINE_COMMENTS
    assert len(selected) == MAX_INLINE_COMMENTS
    # 未发为行内评论的 finding 仍然在 review body 里可追溯。
    assert f"Secret {MAX_INLINE_COMMENTS + 2}." in body


def test_duplicate_findings_get_one_inline_comment():
    issue = PotentialIssue(
        file="src/a.py",
        severity="high",
        category="security",
        confidence=0.9,
        reasoning="Same finding.",
        suggestion="Fix.",
        line=10,
    )

    result = _result([issue, issue.model_copy(deep=True)])
    comments = build_review_comments(result)

    assert len(comments) == 1


def test_review_artifacts_carry_stable_markers():
    issue = PotentialIssue(
        file="src/a.py",
        severity="high",
        category="security",
        confidence=0.9,
        reasoning="Hardcoded secret.",
        suggestion="Use env vars.",
        line=10,
    )
    result = _result([issue])

    first_body = build_review_body(result)
    second_body = build_review_body(result)
    comment_body = build_review_comments(result)[0]["body"]

    assert REVIEW_MARKER in first_body
    assert first_body == second_body
    assert COMMENT_MARKER_PREFIX in comment_body

