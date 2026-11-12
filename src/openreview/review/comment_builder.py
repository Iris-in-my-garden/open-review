"""把 ReviewResult 转成 GitHub Review API 的 body / inline comments。

Two production concerns are encoded here:

1. **Signal-to-noise**: only critical/high findings become inline comments.
   Medium/low findings stay in the review summary, so a noisy model cannot
   post 30 low-value comments and train developers to ignore the bot.
2. **Idempotency**: every review/comment carries a stable marker. The GitHub
   client uses the review marker to find and replace its previous review
   instead of appending a new one on every push.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List, Sequence

from openreview.domain.markers import COMMENT_MARKER_PREFIX, REVIEW_MARKER
from openreview.domain.models import PotentialIssue, ReviewResult

MAX_INLINE_COMMENTS = 10
# Review body 也要有上限：否则一次异常输出可能生成上千行评论正文，
# 触发 GitHub body size limit 并让评论完全发不出去。
MAX_BODY_ISSUES = 30
INLINE_SEVERITIES = frozenset({"critical", "high"})


def _issue_fingerprint(issue: PotentialIssue) -> str:
    """Stable identity for a finding, used to de-duplicate across runs."""
    payload = "\0".join(
        [
            issue.file or "",
            str(issue.line or ""),
            str(issue.end_line or ""),
            issue.side or "",
            issue.category or "",
            issue.reasoning or "",
        ]
    )
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:12]


def select_inline_issues(
    result: ReviewResult,
    *,
    inline_severities: Iterable[str] = INLINE_SEVERITIES,
    max_comments: int = MAX_INLINE_COMMENTS,
) -> List[PotentialIssue]:
    """Return the bounded set of findings that deserve an inline comment."""
    allowed = {str(severity).lower() for severity in inline_severities}
    selected: List[PotentialIssue] = []
    seen: set[str] = set()
    for issue in result.potential_issues:
        if not issue.line or str(issue.severity).lower() not in allowed:
            continue
        fingerprint = _issue_fingerprint(issue)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        selected.append(issue)
        if len(selected) >= max_comments:
            break
    return selected


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
    lines.extend(
        [
            "",
            f"_confidence: {issue.confidence:.2f}_",
            "",
            f"{COMMENT_MARKER_PREFIX}{_issue_fingerprint(issue)} -->",
        ]
    )
    return "\n".join(lines)


def build_review_body(
    result: ReviewResult,
    *,
    inline_issues: Sequence[PotentialIssue] | None = None,
) -> str:
    """Build the top-level review body.

    ``inline_issues`` should be the same list passed to
    :func:`build_review_comments`; anything not posted inline is summarized
    here so no finding silently disappears from the review.
    """
    inline_keys = (
        {_issue_fingerprint(issue) for issue in inline_issues}
        if inline_issues is not None
        else {
            _issue_fingerprint(issue)
            for issue in select_inline_issues(result)
        }
    )
    other_issues = [
        issue
        for issue in result.potential_issues
        if _issue_fingerprint(issue) not in inline_keys
    ]
    lines = [
        "## OpenReview 自动审查结果",
        "",
        f"- **Risk Level**: {result.risk_level}",
        f"- **Decision**: {result.final_decision}",
        f"- **Issues**: {len(result.potential_issues)}",
        f"- **Inline comments**: {len(inline_keys)} "
        "(仅 critical/high 发为行内评论)",
        "",
        result.risk_reason,
    ]
    if other_issues:
        lines.extend(["", "### 其他问题（未发为行内评论）"])
        for issue in other_issues[:MAX_BODY_ISSUES]:
            location = f"`{issue.file}`"
            if issue.line:
                location += f":{issue.line}"
            lines.append(
                f"- {location} **[{issue.severity}][{issue.category}]** "
                f"{issue.reasoning}"
            )
        hidden = len(other_issues) - MAX_BODY_ISSUES
        if hidden > 0:
            lines.append(f"- ...另有 {hidden} 条问题未在正文展示。")
    lines.extend(
        [
            "",
            "---",
            "_由 OpenReview 生成，请结合人工判断。_",
            "",
            REVIEW_MARKER,
        ]
    )
    return "\n".join(lines)


def build_review_comments(
    result: ReviewResult,
    *,
    inline_issues: Sequence[PotentialIssue] | None = None,
) -> List[Dict]:
    selected = (
        list(inline_issues)
        if inline_issues is not None
        else select_inline_issues(result)
    )
    comments: List[Dict] = []
    for issue in selected:
        if not issue.line:
            continue
        side = "LEFT" if issue.side == "LEFT" else "RIGHT"
        comment = {
            "path": issue.file,
            "line": issue.line,
            "side": side,
            "body": _issue_markdown(issue),
        }
        if issue.end_line and issue.end_line > issue.line:
            comment["start_line"] = issue.line
            comment["start_side"] = side
            comment["line"] = issue.end_line
        comments.append(comment)
    return comments
