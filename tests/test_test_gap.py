from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.test_gap import analyze_test_gaps


def _diff() -> ParsedDiff:
    return ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="pkg/service.py",
                status="modified",
                changed_symbols=["create_user"],
                changed_lines=[10],
            )
        ]
    )


def test_no_linked_test_file_is_reported():
    suggestions = analyze_test_gaps(_diff(), [])

    assert len(suggestions) == 1
    assert "未发现与 `pkg/service.py` 关联的测试文件" in suggestions[0]


def test_linked_test_with_changed_symbol_produces_no_suggestion():
    contexts = [
        CodeContext(
            file_path="tests/test_service.py",
            reason="Linked test file",
            snippet="from pkg.service import create_user\n\ndef test_create():\n    create_user()",
            score=0.85,
        )
    ]

    assert analyze_test_gaps(_diff(), contexts) == []


def test_linked_test_without_changed_symbol_is_reported():
    contexts = [
        CodeContext(
            file_path="tests/test_service.py",
            reason="Linked test file",
            snippet="def test_unrelated():\n    assert True",
            score=0.85,
        )
    ]

    suggestions = analyze_test_gaps(_diff(), contexts)

    assert len(suggestions) == 1
    assert "未出现变更符号（create_user）" in suggestions[0]
