# openreview/output_parser.py
from typing import List
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

    def parse(self, raw_data: ReviewResultSchema, metadata: dict) -> ReviewResult:
        return ReviewResult(
            owner=metadata.get("owner", "unknown"),
            repo=metadata.get("repo", "unknown"),
            pr_number=metadata.get("pr_number", 0),
            summary=raw_data.summary,
            risk_level=raw_data.risk_level,
            risk_reason=raw_data.risk_reason,
            key_changes=raw_data.key_changes,
            potential_issues=[
                PotentialIssue(
                    file=issue.file,
                    severity=issue.severity,
                    category=issue.category,
                    confidence=issue.confidence,
                    reasoning=issue.reasoning,
                    suggestion=issue.suggestion,
                )
                for issue in raw_data.potential_issues
            ],
            missing_test_suggestions=raw_data.missing_test_suggestions,
            evidence=raw_data.evidence,
            final_decision=raw_data.final_decision,
            final_summary=raw_data.final_summary,
        )