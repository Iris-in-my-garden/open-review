import pytest

from openreview.review.output_parser import PotentialIssueSchema


def _issue(**overrides) -> PotentialIssueSchema:
    payload = {
        "file": "src/a.py",
        "severity": "high",
        "category": "bugs",
        "confidence": 0.9,
        "reasoning": "reason",
        "suggestion": "fix",
    }
    payload.update(overrides)
    return PotentialIssueSchema(**payload)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bugs", "bugs"),
        ("Bug", "bugs"),
        ("SECURITY", "security"),
        ("perf", "performance"),
        ("clean-code", "clean_code"),
        ("test coverage", "test_gap"),
        ("security issue", "security"),
        ("something_else", "clean_code"),
    ],
)
def test_category_is_normalized_to_schema_enum(raw: str, expected: str):
    assert _issue(category=raw).category == expected


def test_unknown_category_is_refined_from_reasoning():
    issue = _issue(
        category="unknown",
        reasoning="Hardcoded API key allows authentication bypass.",
    )

    assert issue.category == "security"


def test_explicit_clean_code_is_not_overridden_by_keywords():
    issue = _issue(
        category="clean_code",
        reasoning="Duplicate logic in the test helper makes the author's intent unclear.",
    )

    assert issue.category == "clean_code"


def test_scope_and_side_are_normalized():
    issue = _issue(scope="historical", side="left")

    assert issue.scope == "pre_existing"
    assert issue.side == "LEFT"


def test_schema_descriptions_carry_operational_thresholds():
    schema = PotentialIssueSchema.model_json_schema()
    props = schema["properties"]

    assert "0.6" in props["confidence"]["description"]
    assert "critical" in props["severity"]["description"]
    assert "bugs" in props["category"]["description"]
    assert "左侧行号" in props["evidence"]["description"]
