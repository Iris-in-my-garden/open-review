# openreview/output_parser.py
from typing import List,Type
from pydantic import BaseModel
from openreview.review.report_writer import PotentialIssue, ReviewResult

class PotentialIssueSchema(BaseModel):
    file: str
    severity: str
    category: str
    confidence: float
    reasoning: str
    suggestion: str

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