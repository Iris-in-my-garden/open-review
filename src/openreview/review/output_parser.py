from typing import List, Type, Dict, Any
from pydantic import BaseModel, Field

from openreview.review.assessment import (
    SEVERITY_WEIGHTS,
    assess_risk,
    decide,
    deduplicate_issues,
    normalize_severity,
    unique_files,
)
from openreview.review.report_writer import PotentialIssue, ReviewResult

class PotentialIssueSchema(BaseModel):
    file: str = Field(description="发生问题的代码文件路径")
    severity: str = Field(description="严重程度，取值范围: critical, high, medium, low")
    category: str = Field(description="问题分类，对应当前的审查维度")
    confidence: float = Field(description="置信度分数 0.0 ~ 1.0")
    reasoning: str = Field(description="问题的原理解释与上下文背景")
    suggestion: str = Field(description="具体的修复或重构建议代码")

class AspectReviewOutputSchema(BaseModel):
    """单个 Worker（维度）的标准输出结构"""
    aspect_summary: str = Field(description="该维度的整体审查摘要")
    issues: List[PotentialIssueSchema] = Field(default_factory=list, description="该维度发现的潜在问题列表")

class AspectReviewTaskResult(BaseModel):
    """ 单个 Worker 任务执行结果的结构化封装"""
    aspect: str = Field(description="审查维度名称（如 clean_code, bugs, performance, security）")
    data: AspectReviewOutputSchema = Field(description="该维度对应的结构化审查输出")

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
    @property
    def schema(self) -> Type[BaseModel]:
        return ReviewResultSchema

    def schema_for_aspect(self, aspect: str) -> Type[BaseModel]:
        """所有 Worker 维度统一使用该 Schema，确保 LLM 结构化输出稳定性"""
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
            key_changes=["多维度并行代码评审已完成"],
            potential_issues=all_issues,
            missing_test_suggestions=["建议针对本次审查发现的边界条件与潜在逻辑漏洞补充自动化单元测试。"],
            evidence=unique_files(all_issues),
            final_decision=final_decision,
            final_summary=f"多维度评审结束。最终决策: {final_decision} (风险等级: {risk_level})"
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
                )
                for issue in review_data.potential_issues
            ],
            missing_test_suggestions=review_data.missing_test_suggestions,
            evidence=review_data.evidence,
            final_decision=review_data.final_decision,
            final_summary=review_data.final_summary,
        )