"""风险等级与合并决策的统一计算。

聚合阶段（OutputParser）和校验阶段（FindingValidator）必须使用同一套规则，
否则会出现“报告写着 HIGH / 8 个问题，但正文显示 No issues”的自相矛盾。
"""

from collections import Counter
from typing import Iterable, Protocol, Tuple


SEVERITY_WEIGHTS = {"critical": 4, "high": 3, "medium": 2, "low": 1}

# 兼容 LLM 可能返回的同义词
_SEVERITY_ALIASES = {
    "blocker": "critical",
    "severe": "critical",
    "major": "high",
    "moderate": "medium",
    "minor": "low",
    "trivial": "low",
    "info": "low",
    "informational": "low",
}


class IssueLike(Protocol):
    severity: str
    confidence: float
    file: str


def normalize_severity(value: str) -> str:
    """把 LLM 输出的大小写/同义词统一成标准等级。"""
    normalized = (value or "").strip().lower()
    normalized = _SEVERITY_ALIASES.get(normalized, normalized)
    return normalized if normalized in SEVERITY_WEIGHTS else "medium"


def assess_risk(issues: Iterable[IssueLike]) -> Tuple[str, str]:
    """根据问题集合计算风险等级与可读的解释。"""
    issues = list(issues)
    counts = Counter(normalize_severity(issue.severity) for issue in issues)

    if counts["critical"]:
        level = "CRITICAL"
    elif counts["high"]:
        level = "HIGH"
    elif counts["medium"] or len(issues) > 3:
        level = "MEDIUM"
    else:
        level = "LOW"

    if not issues:
        reason = "校验后未保留任何高置信度问题。"
    else:
        reason = (
            f"共保留 {len(issues)} 个高置信度问题"
            f"（Critical {counts['critical']} / High {counts['high']} / "
            f"Medium {counts['medium']} / Low {counts['low']}）。"
        )
    return level, reason


def decide(risk_level: str) -> str:
    """风险等级到合并建议的映射。"""
    level = (risk_level or "").upper()
    if level in {"CRITICAL", "HIGH"}:
        return "REQUEST_CHANGES"
    if level == "MEDIUM":
        return "COMMENT"
    return "APPROVE"


def unique_files(issues: Iterable[IssueLike]) -> list[str]:
    """按出现顺序去重，避免 Evidence 里同一文件重复出现。"""
    seen: set[str] = set()
    files: list[str] = []
    for issue in issues:
        if issue.file and issue.file not in seen:
            seen.add(issue.file)
            files.append(issue.file)
    return files


def _fingerprint(issue: IssueLike) -> tuple[str, ...]:
    """用于跨维度去重的轻量指纹。

    优先使用“文件 + 归一化 reasoning”，因为不同维度对同一问题的描述往往
    高度相似；reasoning 为空时退回 category + suggestion。
    """
    reasoning = " ".join((getattr(issue, "reasoning", "") or "").lower().split())
    file_key = (issue.file or "").strip().lower()
    line_key = str(getattr(issue, "line", None) or "")
    if reasoning:
        return (file_key, line_key, reasoning)

    category = (getattr(issue, "category", "") or "").strip().lower()
    suggestion = " ".join(
        (getattr(issue, "suggestion", "") or "").lower().split()
    )
    return (file_key, line_key, category, suggestion)


def deduplicate_issues(issues: Iterable[IssueLike]) -> list:
    """跨维度去重，保留严重级别更高（同级则置信度更高）的那一条。"""
    best: dict[tuple[str, ...], IssueLike] = {}
    order: list[tuple[str, ...]] = []

    for issue in issues:
        key = _fingerprint(issue)
        if key not in best:
            best[key] = issue
            order.append(key)
            continue

        existing = best[key]
        existing_rank = (
            SEVERITY_WEIGHTS.get(normalize_severity(existing.severity), 0),
            existing.confidence,
        )
        new_rank = (
            SEVERITY_WEIGHTS.get(normalize_severity(issue.severity), 0),
            issue.confidence,
        )
        if new_rank > existing_rank:
            best[key] = issue

    return [best[key] for key in order]


def _unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def merge_review_results(results: list) -> "ReviewResult":
    """合并多个批次（split_parsed_diff）的审查结果，并重新计算全局结论。"""
    from openreview.review.report_writer import ReviewResult

    if not results:
        raise ValueError("No review results to merge.")

    issues = []
    for result in results:
        issues.extend(result.potential_issues)

    issues = deduplicate_issues(issues)
    for issue in issues:
        issue.severity = normalize_severity(issue.severity)

    risk_level, risk_reason = assess_risk(issues)
    final_decision = decide(risk_level)
    first = results[0]

    return ReviewResult(
        owner=first.owner,
        repo=first.repo,
        pr_number=first.pr_number,
        summary="\n\n".join(result.summary for result in results if result.summary),
        risk_level=risk_level,
        risk_reason=risk_reason,
        key_changes=_unique_strings(
            change for result in results for change in result.key_changes
        ),
        potential_issues=issues,
        missing_test_suggestions=_unique_strings(
            suggestion
            for result in results
            for suggestion in result.missing_test_suggestions
        ),
        evidence=unique_files(issues),
        final_decision=final_decision,
        final_summary=(
            f"合并 {len(results)} 个批次，保留 {len(issues)} 个问题。"
            f"最终决策: {final_decision} (风险等级: {risk_level})"
        ),
        review_notes=_unique_strings(
            note for result in results for note in result.review_notes
        ),
    )

