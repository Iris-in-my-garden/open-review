from openreview.review.assessment import merge_review_results
from openreview.review.report_writer import PotentialIssue, ReviewResult


def _result(issues, notes=None) -> ReviewResult:
    return ReviewResult(
        owner="org",
        repo="project",
        pr_number=1,
        summary="batch summary",
        risk_level="HIGH",
        risk_reason="pre",
        potential_issues=issues,
        final_decision="REQUEST_CHANGES",
        final_summary="pre",
        review_notes=notes or [],
    )


def test_merge_review_results_merges_paraphrased_findings():
    from openreview.review.assessment import deduplicate_issues

    first = PotentialIssue(
        file="src/auth.py",
        severity="high",
        category="security",
        confidence=0.9,
        reasoning="Hardcoded secret in login().",
        suggestion="Use env vars.",
        line=10,
    )
    paraphrase = PotentialIssue(
        file="src/auth.py",
        severity="critical",
        category="bugs",
        confidence=0.8,
        reasoning="Hardcoded API key in the login function.",
        suggestion="Use environment variables.",
        line=11,
    )
    unrelated = PotentialIssue(
        file="src/auth.py",
        severity="high",
        category="performance",
        confidence=0.8,
        reasoning="N+1 query in the dashboard loader.",
        suggestion="Batch the queries.",
        line=11,
    )

    deduped = deduplicate_issues([first, paraphrase, unrelated])

    assert len(deduped) == 2
    assert deduped[0].severity == "critical"
    assert "login" in deduped[0].reasoning.lower()
    assert any("N+1" in issue.reasoning for issue in deduped)


def test_risk_is_calibrated_by_confidence():
    from openreview.review.assessment import assess_risk

    speculative = PotentialIssue(
        file="src/a.py",
        severity="high",
        category="bugs",
        confidence=0.61,
        reasoning="Possibly null.",
        suggestion="Add a guard.",
        line=1,
    )

    level, _ = assess_risk([speculative])

    assert level == "MEDIUM"


def test_merge_review_results_deduplicates_and_recomputes():
    duplicate_high = PotentialIssue(
        file="src/a.py",
        severity="high",
        category="bugs",
        confidence=0.9,
        reasoning="Same issue.",
        suggestion="Fix.",
        line=1,
    )
    duplicate_critical = PotentialIssue(
        file="src/a.py",
        severity="critical",
        category="security",
        confidence=0.8,
        reasoning="same issue.",
        suggestion="Fix.",
        line=1,
    )

    merged = merge_review_results(
        [
            _result([duplicate_high], notes=["batch 1"]),
            _result([duplicate_critical], notes=["batch 2"]),
        ]
    )

    assert len(merged.potential_issues) == 1
    assert merged.potential_issues[0].severity == "critical"
    assert merged.risk_level == "CRITICAL"
    assert merged.final_decision == "REQUEST_CHANGES"
    assert merged.review_notes == ["batch 1", "batch 2"]
    assert "2 个批次" in merged.final_summary
