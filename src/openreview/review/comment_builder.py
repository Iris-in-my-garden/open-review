"""把 ReviewResult 转成 GitHub Review API 的 body / inline comments。"""

from typing import Dict, List

from openreview.review.report_writer import PotentialIssue, ReviewResult

MAX_INLINE_COMMENTS = 30


def _issue_markdown(issue: PotentialIssue) -> str:
    lines = [
        f"**[{issue.severity.upper()}][{issue.category}]** {issue.reasoning}",
        "",
        f"**建议**：{issue.suggestion}",
    ]
    if issue.evidence:
        lines.extend(["", f"**证据**：`{issue.evidence}`"])
    if issue.suggested_patch:
        lines.extend(["", "```diff", issue.suggested_patch, "```"])
    lines.extend(["", f"_confidence: {issue.confidence:.2f}_"])
    return "\n".join(lines)


def build_review_body(result: ReviewResult) -> str:
    file_level = [issue for issue in result.potential_issues if not issue.line]
    lines = [
        "## OpenReview 自动审查结果",
        "",
        f"- **Risk Level**: {result.risk_level}",
        f"- **Decision**: {result.final_decision}",
        f"- **Issues**: {len(result.potential_issues)}",
        "",
        result.risk_reason,
    ]
    if file_level:
        lines.extend(["", "### 文件级问题（无法定位到具体行）"])
        for issue in file_level[:MAX_INLINE_COMMENTS]:
            lines.append(
                f"- `{issue.file}` **[{issue.severity}][{issue.category}]** "
                f"{issue.reasoning}"
            )
    lines.extend(["", "---", "_由 OpenReview 生成，请结合人工判断。_"])
    return "\n".join(lines)


def build_review_comments(result: ReviewResult) -> List[Dict]:
    comments: List[Dict] = []
    for issue in result.potential_issues:
        if not issue.line or len(comments) >= MAX_INLINE_COMMENTS:
            continue

        comment = {
            "path": issue.file,
            "line": issue.line,
            "side": "RIGHT",
            "body": _issue_markdown(issue),
        }
        if issue.end_line and issue.end_line > issue.line:
            comment["start_line"] = issue.line
            comment["start_side"] = "RIGHT"
            comment["line"] = issue.end_line
        comments.append(comment)
    return comments
