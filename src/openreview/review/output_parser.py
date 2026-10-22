from typing import List, Type, Dict, Any
from pydantic import BaseModel, Field

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

        # 按照严重程度降序排序 (Critical/High 优先展示)
        severity_weight = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        all_issues.sort(key=lambda x: severity_weight.get(x.severity.lower(), 0), reverse=True)

        # 综合风险等级评估
        has_critical = any(i.severity.lower() == "critical" for i in all_issues)
        has_high = any(i.severity.lower() == "high" for i in all_issues)

        risk_level = "LOW"
        if has_critical:
            risk_level = "CRITICAL"
        elif has_high:
            risk_level = "HIGH"
        elif len(all_issues) > 3:
            risk_level = "MEDIUM"

        final_decision = "APPROVE" if risk_level in ["LOW", "MEDIUM"] else "REQUEST_CHANGES"

        combined_summary = "\n".join(aspect_summaries)

        return ReviewResultSchema(
            summary=combined_summary,
            risk_level=risk_level,
            risk_reason=f"基于多维度（Clean Code、逻辑Bug、性能优化、安全漏洞）分析，共发现 {len(all_issues)} 个潜在问题。",
            key_changes=["多维度并行代码评审已完成"],
            potential_issues=all_issues,
            missing_test_suggestions=["建议针对本次审查发现的边界条件与潜在逻辑漏洞补充自动化单元测试。"],
            evidence=[issue.file for issue in all_issues],
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