import subprocess
from pathlib import Path

import pytest

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.tree_sitter_retriever import (
    SCORE_DEFINITION,
    SCORE_ENCLOSING,
    SCORE_REFERENCE,
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
