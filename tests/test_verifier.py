from typing import Type, TypeVar

from pydantic import BaseModel

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.review.report_writer import PotentialIssue, ReviewResult
from openreview.review.verifier import FindingVerifier, VerifierResult

T = TypeVar("T", bound=BaseModel)


class ScriptedVerifierLLM:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = 0

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        self.calls += 1
        return response_schema(
            results=[
                VerifierResult(
                    issue_id=issue_id,
                    verdict=verdict,
                    confidence=0.9 if verdict == "verified" else 0.5,
                    reason=f"{verdict} reason",
                )
                for issue_id, verdict in self.verdicts.items()
            ]
        )


def _issue(line: int, severity: str = "high") -> PotentialIssue:
    return PotentialIssue(
        file="src/a.py",
        severity=severity,
        category="bugs",
        confidence=0.8,
        reasoning=f"issue at {line}",
        suggestion="fix it",
        line=line,
    )


def _result(issues) -> ReviewResult:
    return ReviewResult(
        owner="org",
        repo="repo",
        pr_number=1,
        summary="summary",
        risk_level="HIGH",
        risk_reason="reason",
        potential_issues=list(issues),
        final_decision="REQUEST_CHANGES",
        final_summary="summary",
    )


def _diff() -> ParsedDiff:
    return ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/a.py",
                status="modified",
                patch="@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n",
                changed_lines=[1, 2],
            )
        ]
    )


def test_verifier_rejects_findings_and_recomputes_risk():
    llm = ScriptedVerifierLLM({"issue-1": "rejected", "issue-2": "verified"})
    verifier = FindingVerifier(llm_client=llm, batch_size=10, max_attempts=1)

    result, stats = verifier.verify(
        _result([_issue(1), _issue(2)]), _diff(), contexts=[]
    )

    assert llm.calls == 1
    assert stats.candidates == 2
    assert stats.rejected == 1
    assert stats.verified == 1
    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].line == 2
    assert result.risk_level == "HIGH"
    assert any("Verifier 拒绝" in note for note in result.review_notes)


def test_verifier_lowers_confidence_when_context_is_insufficient():
    llm = ScriptedVerifierLLM({"issue-1": "needs_more_context"})
    verifier = FindingVerifier(llm_client=llm, batch_size=10, max_attempts=1)

    result, stats = verifier.verify(_result([_issue(1)]), _diff(), contexts=[])

    assert stats.needs_more_context == 1
    assert result.potential_issues[0].confidence == 0.7


class LowConfidenceVerifiedLLM:
    def generate_structured(self, system_prompt, user_prompt, response_schema):
        return response_schema(
            results=[
                VerifierResult(
                    issue_id="issue-1",
                    verdict="verified",
                    confidence=0.2,
                    reason="looks plausible",
                )
            ]
        )


def test_verifier_does_not_accept_low_confidence_verified():
    verifier = FindingVerifier(
        llm_client=LowConfidenceVerifiedLLM(), batch_size=10, max_attempts=1
    )

    result, stats = verifier.verify(_result([_issue(1)]), _diff(), contexts=[])

    assert stats.verified == 0
    assert stats.needs_more_context == 1
    assert stats.downgraded_low_confidence == 1
    assert result.potential_issues[0].confidence == 0.7


def test_verifier_treats_unknown_verdict_as_needs_more_context():
    llm = ScriptedVerifierLLM({"issue-1": "definitely-not-a-verdict"})
    verifier = FindingVerifier(llm_client=llm, batch_size=10, max_attempts=1)

    result, stats = verifier.verify(_result([_issue(1)]), _diff(), contexts=[])

    # 未知 verdict 绝不能默认成 verified；应降级为需要更多上下文。
    assert stats.verified == 0
    assert stats.needs_more_context == 1
    assert result.potential_issues[0].confidence == 0.7
