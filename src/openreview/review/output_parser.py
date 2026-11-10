import re
from typing import Any, List, Literal, Optional, Type

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from openreview.review.assessment import (
    SEVERITY_WEIGHTS,
    Severity,
    assess_risk,
    decide,
    deduplicate_issues,
    normalize_severity,
    unique_files,
)
from openreview.review.report_writer import (
    IssueScope,
    IssueSide,
    PotentialIssue,
    ReviewResult,
    normalize_scope,
    normalize_side,
)

IssueCategory = Literal[
    "bugs", "security", "performance", "clean_code", "test_gap"
]
_CATEGORY_VALUES = frozenset(
    {"bugs", "security", "performance", "clean_code", "test_gap"}
)
# 模型经常返回单复数/同义词；收敛后再校验，避免一个拼写让整条响应解析失败。
_CATEGORY_ALIASES = {
    "bug": "bugs",
    "logic": "bugs",
    "correctness": "bugs",
    "sec": "security",
    "security_issue": "security",
    "perf": "performance",
    "performance_issue": "performance",
    "clean": "clean_code",
    "clean-code": "clean_code",
    "code_quality": "clean_code",
    "maintainability": "clean_code",
    "test": "test_gap",
    "tests": "test_gap",
    "test_coverage": "test_gap",
    "testing": "test_gap",
}

# 未知分类不能一律收敛到 clean_code：模型偶尔会把安全/性能问题写错类别，
# 直接映射成 clean_code 会让这类 finding 看起来“只是可维护性问题”。
# 英文关键词按单词边界匹配，避免 "auth" 命中 "author"、"test" 命中 "latest"。
_CATEGORY_KEYWORDS = (
    (
        "security",
        (
            "security", "secret", "credential", "password", "injection", "xss",
            "csrf", "ssrf", "authentication", "authorization", "permission",
            "crypto", "certificate", "vulnerability", "密钥", "证书", "注入",
            "鉴权", "权限", "安全",
        ),
    ),
    (
        "performance",
        (
            "performance", "latency", "memory", "cpu", "n+1", "complexity",
            "slow", "cache", "throughput", "性能", "复杂度", "内存", "延迟",
        ),
    ),
    (
        "test_gap",
        ("test", "tests", "coverage", "assert", "fixture", "mock", "测试", "覆盖"),
    ),
    (
        "bugs",
        (
            "bug", "null", "none", "exception", "error", "crash", "race",
            "overflow", "off-by-one", "undefined", "panic", "deadlock",
            "空指针", "异常", "越界", "逻辑",
        ),
    ),
)


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    if keyword.isascii():
        return re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])")
    return re.compile(escaped)


_CATEGORY_PATTERNS = tuple(
    (category, tuple(_keyword_pattern(word) for word in words))
    for category, words in _CATEGORY_KEYWORDS
)


def _infer_category(text: str) -> str:
    lowered = text.lower()
    for category, patterns in _CATEGORY_PATTERNS:
        if any(pattern.search(lowered) for pattern in patterns):
            return category
    return "clean_code"


class PotentialIssueSchema(BaseModel):
    file: str = Field(description="发生问题的代码文件路径")
    severity: Severity = Field(
        description=(
            "严重程度，取值范围: critical, high, medium, low。"
            "critical=可达路径上会导致数据损坏/权限绕过/远程代码执行/服务崩溃；"
            "high=特定输入下产生错误结果或存在明确可利用的安全缺陷；"
            "medium=边界或异常处理不完整但触发条件较窄；"
            "low=可维护性问题且不影响当前行为。不确定时用 medium。"
        )
    )
    category: IssueCategory = Field(
        description=(
            "问题分类，必须是 bugs / security / performance / clean_code / "
            "test_gap 之一"
        )
    )
    confidence: float = Field(
        description=(
            "置信度分数 0.0 ~ 1.0。系统会丢弃 confidence < 0.6 的问题；"
            "真实但证据只覆盖部分路径时给 0.6–0.7 并在 reasoning 中说明。"
        )
    )
    reasoning: str = Field(description="问题的原理解释与上下文背景")
    suggestion: str = Field(description="具体的修复或重构建议代码")
    line: Optional[int] = Field(
        default=None,
        description="问题所在的新文件行号（必须来自 Patch 中带行号的新增行或上下文行）",
    )
    end_line: Optional[int] = Field(
        default=None,
        description="多行问题的结束行号（可选，需 >= line）",
    )
    scope: IssueScope = Field(
        default="introduced",
        description=(
            "问题来源：introduced=本次 diff 新增/修改的代码引入；"
            "pre_existing=未改动代码里的历史问题（默认会被系统过滤）"
        ),
    )
    side: IssueSide = Field(
        default="RIGHT",
        description=(
            "行号属于哪一侧：RIGHT=新文件侧（新增/上下文行）；"
            "LEFT=删除行（使用旧文件行号）"
        ),
    )
    evidence: Optional[str] = Field(
        default=None,
        description=(
            "支撑该问题的原始代码片段，必须逐字来自 Patch 或 Context；"
            "只填代码本身，不要带左侧行号、`|` 或 `+/-` 前缀"
        ),
    )
    suggested_patch: Optional[str] = Field(
        default=None,
        description="可直接应用的 unified diff 修复片段（可选）",
    )
    context_refs: List[str] = Field(
        default_factory=list,
        description=(
            "引用的上下文 ID 列表，例如 [\"ctx-a1b2c3d4\"]；"
            "只能引用 Patch/Context 中实际出现的 [ctx-...]"
        ),
    )
    # 内部字段：由聚合层写入，不暴露给模型（SkipJsonSchema），也不参与序列化。
    source_aspects: SkipJsonSchema[List[str]] = Field(
        default_factory=list,
        exclude=True,
        description="报告该问题的审查维度，用于评测归因",
    )

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, value: object) -> Severity:
        return normalize_severity(str(value))

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: object) -> str:
        text = (
            str(value or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        text = _CATEGORY_ALIASES.get(text, text)
        # 未知分类先按关键词推断，再退回 clean_code，避免把安全问题误标成可维护性。
        return text if text in _CATEGORY_VALUES else _infer_category(text)

    @field_validator("scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: object) -> IssueScope:
        return normalize_scope(value)

    @field_validator("side", mode="before")
    @classmethod
    def _normalize_side(cls, value: object) -> IssueSide:
        return normalize_side(value)

    @model_validator(mode="before")
    @classmethod
    def _infer_unknown_category(cls, data: Any) -> Any:
        """仅当模型给出未知 category 时，用 reasoning/suggestion 推断。

        不能在所有 ``clean_code`` 上做推断：模型明确选择 clean_code 时，
        描述里出现 "test" / "error" 等词并不代表分类错了。
        """
        if not isinstance(data, dict):
            return data
        raw = str(data.get("category") or "").strip().lower()
        raw = raw.replace("-", "_").replace(" ", "_")
        if not raw or raw in _CATEGORY_VALUES or raw in _CATEGORY_ALIASES:
            return data
        inferred = _infer_category(
            f"{data.get('reasoning', '')} {data.get('suggestion', '')}"
        )
        if inferred == "clean_code":
            return data
        return {**data, "category": inferred}


class AspectReviewOutputSchema(BaseModel):
    """单次审查（single-pass 或单个 aspect）的标准输出结构"""
    aspect_summary: str = Field(description="本次审查的整体摘要")
    issues: List[PotentialIssueSchema] = Field(default_factory=list, description="本次发现的潜在问题列表")

class AspectReviewTaskResult(BaseModel):
    """单次审查任务执行结果的结构化封装"""
    aspect: str = Field(description="审查来源（single_shot 或 clean_code/bugs/performance/security）")
    data: AspectReviewOutputSchema = Field(description="该来源对应的结构化审查输出")

class ReviewResultSchema(BaseModel):
    summary: str
    risk_level: str
    risk_reason: str
    key_changes: List[str]
    potential_issues: List[PotentialIssueSchema]
    missing_test_suggestions: List[str]
    evidence: List[str]
    final_decision: str
    final_summary: str


class OutputParser:
    def schema_for_aspect(self, aspect: str) -> Type[AspectReviewOutputSchema]:
        """single-pass 和 multi-aspect 共用该 Schema，确保结构化输出稳定。"""
        return AspectReviewOutputSchema

    def aggregate_aspects(self, all_aspect_results: List[AspectReviewTaskResult]) -> ReviewResultSchema:
        """
        将结构化的多个 Worker 审查结果进行聚合与去重整理，组合成最终的 ReviewResultSchema
        """
        all_issues: List[PotentialIssueSchema] = []
        aspect_summaries = []

        for item in all_aspect_results:
            aspect = item.aspect
            data = item.data

            if not data:
                continue

            aspect_summaries.append(f"[{aspect.upper()}] {data.aspect_summary}")
            if data.issues:
                # 在去重前记录来源维度：去重会保留 severity 最高的一条，
                # 如果不记来源，clean_code 等低严重级别维度的贡献会被系统性吞掉。
                # single_shot 没有独立维度，用 finding 自己的 category 归因，
                # 这样评测里的 aspect_counts 仍然可读。
                for issue in data.issues:
                    source = (
                        issue.category
                        if aspect == "single_shot" and issue.category
                        else aspect
                    )
                    issue.source_aspects = list(
                        dict.fromkeys([*issue.source_aspects, source])
                    )
                all_issues.extend(data.issues)

        # 同一问题可能被多个维度重复报告，先去重，避免重复计数抬高风险等级
        all_issues = deduplicate_issues(all_issues)

        # 统一严重级别，避免 High / HIGH / major 混排
        for issue in all_issues:
            issue.severity = normalize_severity(issue.severity)

        # 按照严重程度降序排序 (Critical/High 优先展示)
        all_issues.sort(
            key=lambda x: SEVERITY_WEIGHTS.get(normalize_severity(x.severity), 0),
            reverse=True,
        )

        risk_level, risk_reason = assess_risk(all_issues)
        final_decision = decide(risk_level)

        combined_summary = "\n".join(aspect_summaries)

        return ReviewResultSchema(
            summary=combined_summary,
            risk_level=risk_level,
            risk_reason=risk_reason,
            key_changes=["代码评审已完成"],
            potential_issues=all_issues,
            missing_test_suggestions=["建议针对本次审查发现的边界条件与潜在逻辑漏洞补充自动化单元测试。"],
            evidence=unique_files(all_issues),
            final_decision=final_decision,
            final_summary=f"评审结束。最终决策: {final_decision} (风险等级: {risk_level})"
        )


    def build_final_result(self, review_data: ReviewResultSchema, metadata: dict) -> ReviewResult:
        return ReviewResult(
            owner=metadata.get("owner", "unknown"),
            repo=metadata.get("repo", "unknown"),
            pr_number=metadata.get("pr_number", 0),
            summary=review_data.summary,
            risk_level=review_data.risk_level,
            risk_reason=review_data.risk_reason,
            key_changes=review_data.key_changes,
            potential_issues=[
                PotentialIssue(
                    file=issue.file,
                    severity=issue.severity,
                    category=issue.category,
                    confidence=issue.confidence,
                    reasoning=issue.reasoning,
                    suggestion=issue.suggestion,
                    line=issue.line,
                    end_line=issue.end_line,
                    scope=issue.scope,
                    side=issue.side,
                    evidence=issue.evidence,
                    suggested_patch=issue.suggested_patch,
                    context_refs=list(issue.context_refs),
                    source_aspects=list(issue.source_aspects),
                )
                for issue in review_data.potential_issues
            ],
            missing_test_suggestions=review_data.missing_test_suggestions,
            evidence=review_data.evidence,
            final_decision=review_data.final_decision,
            final_summary=review_data.final_summary,
        )