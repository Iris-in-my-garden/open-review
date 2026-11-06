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
        ("something_else", "clean_code"),
    ],
)
def test_category_is_normalized_to_schema_enum(raw: str, expected: str):
    assert _issue(category=raw).category == expected


def test_schema_descriptions_carry_operational_thresholds():
    schema = PotentialIssueSchema.model_json_schema()
    props = schema["properties"]

    assert "0.6" in props["confidence"]["description"]
    assert "critical" in props["severity"]["description"]
    assert "bugs" in props["category"]["description"]
    assert "左侧行号" in props["evidence"]["description"]
