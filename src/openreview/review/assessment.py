"""风险等级与合并决策的统一计算。

聚合阶段（OutputParser）和校验阶段（FindingValidator）必须使用同一套规则，
否则会出现“报告写着 HIGH / 8 个问题，但正文显示 No issues”的自相矛盾。
"""

from collections import Counter
from typing import TYPE_CHECKING, Iterable, Literal, Protocol, Tuple, cast

if TYPE_CHECKING:
    from openreview.review.report_writer import ReviewResult

Severity = Literal["critical", "high", "medium", "low"]

SEVERITY_WEIGHTS: dict[Severity, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

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
    "致命": "critical",
    "严重": "critical",
    "高": "high",
    "中": "medium",
    "低": "low",
}


class IssueLike(Protocol):
    severity: Severity
    confidence: float
    file: str


def normalize_severity(value: str) -> Severity:
    """把 LLM 输出的大小写/同义词统一成标准等级。"""
    normalized = (value or "").strip().lower()
    normalized = _SEVERITY_ALIASES.get(normalized, normalized)
    if normalized in SEVERITY_WEIGHTS:
        return cast(Severity, normalized)
    return "medium"


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


def _merge_source_aspects(target: IssueLike, *others: IssueLike) -> None:
    """把重复问题命中的维度合并到保留的那一条上。

    去重按 severity/confidence 选代表，但归因必须保留全部报告者，否则
    clean_code 这类低严重级别维度的贡献会被系统性低估。
    """
    merged = list(getattr(target, "source_aspects", []) or [])
    for other in others:
        for aspect in getattr(other, "source_aspects", []) or []:
            if aspect not in merged:
                merged.append(aspect)
    try:
        target.source_aspects = merged  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        # 第三方/测试替身可能不允许写属性；归因缺失不应影响去重结果。
        pass


def _merge_context_refs(target: IssueLike, *others: IssueLike) -> None:
    """重复问题合并时保留所有被引用的 context id。"""
    merged = list(getattr(target, "context_refs", []) or [])
    for other in others:
        for ref in getattr(other, "context_refs", []) or []:
            if ref and ref not in merged:
                merged.append(ref)
    try:
        target.context_refs = merged  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        pass


def deduplicate_issues(issues: Iterable[IssueLike]) -> list:
    """跨维度去重，保留严重级别更高（同级则置信度更高）的那一条。

    返回的代表项会合并所有重复项的来源维度（source_aspects），供评测做
    维度贡献度统计。
    """
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
            _merge_source_aspects(issue, existing)
            _merge_context_refs(issue, existing)
            best[key] = issue
        else:
            _merge_source_aspects(existing, issue)
            _merge_context_refs(existing, issue)

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

