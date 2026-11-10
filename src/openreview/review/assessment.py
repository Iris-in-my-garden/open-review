"""风险等级与合并决策的统一计算。

聚合阶段（OutputParser）和校验阶段（FindingValidator）必须使用同一套规则，
否则会出现“报告写着 HIGH / 8 个问题，但正文显示 No issues”的自相矛盾。
"""

import re
from collections import Counter
from typing import TYPE_CHECKING, Iterable, Literal, Optional, Protocol, Tuple, cast

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


_LEVEL_ORDER: list[Severity] = ["low", "medium", "high", "critical"]
_REASONING_TOKENS = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+")
_STOP_TOKENS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on",
    "and", "or", "for", "with", "this", "that", "it", "be", "as", "at",
    "by", "from", "should", "could", "would", "may", "might", "can",
    "问题", "代码", "建议", "可能", "如果", "因为", "所以", "这个", "那个",
}


def normalize_severity(value: str) -> Severity:
    """把 LLM 输出的大小写/同义词统一成标准等级。"""
    normalized = (value or "").strip().lower()
    normalized = _SEVERITY_ALIASES.get(normalized, normalized)
    if normalized in SEVERITY_WEIGHTS:
        return cast(Severity, normalized)
    return "medium"


def _calibrated_severity(issue: IssueLike) -> Severity:
    """按置信度校准严重级别。

    validator 的阈值是 0.6，但 0.6 的 high 与 0.9 的 high 不应等价。
    低于 0.6 视为低置信噪声；0.6–0.75 降一级；更高保持原级别。
    """
    severity = normalize_severity(issue.severity)
    confidence = float(getattr(issue, "confidence", 1.0) or 0.0)
    if confidence < 0.6:
        return "low"
    if confidence < 0.75:
        index = max(0, _LEVEL_ORDER.index(severity) - 1)
        return _LEVEL_ORDER[index]
    return severity


def assess_risk(issues: Iterable[IssueLike]) -> Tuple[str, str]:
    """根据问题集合计算风险等级与可读的解释。

    严重级别按 confidence 校准，避免多个 0.6 的推测性 high 把 PR 直接
    推到 HIGH / REQUEST_CHANGES；同时不再因为“low 问题数量多”升级风险。
    """
    issues = list(issues)
    counts = Counter(_calibrated_severity(issue) for issue in issues)

    if counts["critical"]:
        level = "CRITICAL"
    elif counts["high"]:
        level = "HIGH"
    elif counts["medium"]:
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


def _issue_text(issue: IssueLike) -> str:
    parts = [
        getattr(issue, "reasoning", "") or "",
        getattr(issue, "suggestion", "") or "",
    ]
    return " ".join(" ".join(parts).lower().split())


def _issue_tokens(issue: IssueLike) -> set[str]:
    return {
        token
        for token in _REASONING_TOKENS.findall(_issue_text(issue))
        if token not in _STOP_TOKENS and len(token) > 1
    }


def _text_similarity(left: IssueLike, right: IssueLike) -> float:
    """token Jaccard / containment 的较大值。

    纯 exact-string 去重无法合并“hardcoded secret in login”与
    “hardcoded API key in login function”这类改写；containment 能覆盖
    短描述被长描述包含的情况，Jaccard 则防止只共享一个通用词就合并。
    """
    left_tokens = _issue_tokens(left)
    right_tokens = _issue_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    jaccard = intersection / union if union else 0.0
    containment = intersection / min(len(left_tokens), len(right_tokens))
    return max(jaccard, containment)


def _reasoning_similarity(left: IssueLike, right: IssueLike) -> float:
    left_text = " ".join((getattr(left, "reasoning", "") or "").lower().split())
    right_text = " ".join((getattr(right, "reasoning", "") or "").lower().split())
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    left_tokens = set(_REASONING_TOKENS.findall(left_text)) - _STOP_TOKENS
    right_tokens = set(_REASONING_TOKENS.findall(right_text)) - _STOP_TOKENS
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(
        len(left_tokens), len(right_tokens)
    )


def _line_distance(left: IssueLike, right: IssueLike) -> Optional[int]:
    left_line = getattr(left, "line", None)
    right_line = getattr(right, "line", None)
    if left_line is None or right_line is None:
        return None
    try:
        return abs(int(left_line) - int(right_line))
    except (TypeError, ValueError):
        return None


def _is_duplicate(left: IssueLike, right: IssueLike) -> bool:
    if (left.file or "").strip().lower() != (right.file or "").strip().lower():
        return False

    similarity = _text_similarity(left, right)
    distance = _line_distance(left, right)
    left_category = (getattr(left, "category", "") or "").strip().lower()
    right_category = (getattr(right, "category", "") or "").strip().lower()

    if distance is None:
        # 至少一侧没有行号：reasoning 几乎相同（不同维度对同一问题
        # 常只改 suggestion）时合并；否则要求整段描述高度相似且类别一致，
        # 避免把“同文件的两个不同文件级问题”误合并。
        reasoning_similarity = _reasoning_similarity(left, right)
        if reasoning_similarity >= 0.8:
            return True
        return (
            similarity >= 0.75
            and bool(left_category)
            and left_category == right_category
        )
    if distance <= 2:
        return similarity >= 0.5
    if distance <= 8:
        return similarity >= 0.7
    return False


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
    """跨维度语义去重，保留严重级别更高（同级则置信度更高）的那一条。

    不再使用精确字符串指纹：不同维度/不同批次对同一问题的措辞几乎不会
    完全一致。这里改为“同文件 + 行号邻近 + token 相似度”聚类。
    返回的代表项会合并所有重复项的来源维度（source_aspects），供评测做
    维度贡献度统计。
    """
    representatives: list[IssueLike] = []

    for issue in issues:
        match_index: Optional[int] = None
        for index, existing in enumerate(representatives):
            if _is_duplicate(existing, issue):
                match_index = index
                break

        if match_index is None:
            representatives.append(issue)
            continue

        existing = representatives[match_index]
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
            representatives[match_index] = issue
        else:
            _merge_source_aspects(existing, issue)
            _merge_context_refs(existing, issue)

    return representatives


def _unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def merge_review_results(results: list) -> "ReviewResult":
    """合并多个批次（plan_review_batches）的审查结果，并重新计算全局结论。"""
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

