import subprocess
from pathlib import Path

import pytest

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.tree_sitter_retriever import (
    SCORE_BASE_DEFINITION,
    SCORE_DEFINITION,
    SCORE_ENCLOSING,
    SCORE_REFERENCE,
    SCORE_TEST_FILE,
    TreeSitterLocalRetriever,
)


def _init_repo(tmp_path: Path) -> None:
    """git grep 只检索被 Git 跟踪的文件。"""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "def target(x):\n"
        "    return x + 1\n"
        "\n"
        "def caller():\n"
        "    return target(1)\n"
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_mod.py").write_text(
        "from pkg.mod import target\n"
        "\n"
        "def test_target():\n"
        "    assert target(1) == 2\n"
    )
    _init_repo(tmp_path)
    return tmp_path


def test_body_only_change_returns_enclosing_function(sample_repo: Path):
    """PR 只改函数体（签名未变）时，也必须能检索到所在函数。"""
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="pkg/mod.py",
                status="modified",
                changed_symbols=[],
                changed_lines=[2],  # 只改了 return x + 1
            )
        ]
    )

    contexts = TreeSitterLocalRetriever().retrieve(sample_repo, diff)

    assert contexts
    enclosing = contexts[0]
    assert enclosing.file_path == "pkg/mod.py"
    assert enclosing.score == SCORE_ENCLOSING
    assert "def target(x):" in enclosing.snippet
    assert "return x + 1" in enclosing.snippet
    assert "Enclosing function_definition 'target'" in enclosing.reason


def test_reference_is_not_misclassified_as_definition(sample_repo: Path):
    """引用所在行位于函数体内，但不应被标成 Definition 并拿高分。"""
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="pkg/mod.py",
                status="modified",
                changed_symbols=["target"],
                changed_lines=[],
            )
        ]
    )

    contexts = TreeSitterLocalRetriever().retrieve(sample_repo, diff)
    by_reason = {context.reason: context for context in contexts}

    definition_reasons = [r for r in by_reason if "definition of 'target'" in r]
    reference_reasons = [r for r in by_reason if "reference to 'target'" in r]

    assert definition_reasons, by_reason.keys()
    assert reference_reasons, by_reason.keys()
    assert by_reason[definition_reasons[0]].score == SCORE_DEFINITION
    assert by_reason[reference_reasons[0]].score == SCORE_REFERENCE


def test_linked_test_file_is_retrieved(sample_repo: Path):
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="pkg/mod.py",
                status="modified",
                changed_symbols=["target"],
                changed_lines=[2],
            )
        ]
    )

    contexts = TreeSitterLocalRetriever().retrieve(sample_repo, diff)
    test_contexts = [c for c in contexts if c.file_path == "tests/test_mod.py"]

    assert test_contexts
    assert test_contexts[0].score == SCORE_TEST_FILE
    assert "def test_target" in test_contexts[0].snippet


def test_base_revision_context_is_used_for_removed_symbols(tmp_path: Path):
    head_repo = tmp_path / "head"
    base_repo = tmp_path / "base"
    head_repo.mkdir()
    base_repo.mkdir()
    (head_repo / "mod.py").write_text("def keep():\n    pass\n")
    (base_repo / "mod.py").write_text(
        "def gone():\n    return 1\n\ndef keep():\n    pass\n"
    )
    _init_repo(head_repo)
    _init_repo(base_repo)

    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="mod.py",
                status="modified",
                removed_symbols=["gone"],
                changed_lines=[1],
            )
        ]
    )

    contexts = TreeSitterLocalRetriever().retrieve(
        head_repo, diff, base_path=base_repo
    )
    base_contexts = [context for context in contexts if context.revision == "base"]

    assert base_contexts
    assert any("def gone():" in context.snippet for context in base_contexts)
    assert all(
        context.score <= SCORE_BASE_DEFINITION for context in base_contexts
    )
