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
    """降级兜底仍使用 git grep，需要仓库至少有初始索引。"""
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
    """降级兜底里，引用所在行不应被标成 Definition 并拿高分。"""
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

    contexts = TreeSitterLocalRetriever().retrieve(
        sample_repo, diff, allow_global_fallback=True
    )
    by_reason = {context.reason: context for context in contexts}

    definition_reasons = [r for r in by_reason if "definition of 'target'" in r]
    reference_reasons = [r for r in by_reason if "reference to 'target'" in r]

    assert definition_reasons, by_reason.keys()
    assert reference_reasons, by_reason.keys()
    assert by_reason[definition_reasons[0]].score == SCORE_DEFINITION
    assert by_reason[reference_reasons[0]].score == SCORE_REFERENCE


def test_default_retrieval_does_not_grep_the_whole_repo(
    sample_repo: Path, monkeypatch
):
    """跨文件引用由 GraphRetriever 负责，AST 默认路径不能再现场 grep。"""
    from openreview.retriever import tree_sitter_retriever as module

    def _fail(*args, **kwargs):
        raise AssertionError("default AST path must not scan repository")

    monkeypatch.setattr(module, "search_symbols", _fail)
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

    assert not any(
        context.kind in {"definition", "reference"} for context in contexts
    )


def test_git_grep_tolerates_non_utf8_source(tmp_path: Path):
    """git grep 输出里混入非 UTF-8 字节时，检索不能把整次 review 挂掉。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_bytes(
        b"def target(x):\n"
        b"    return x\n"
        b"\n"
        b"def caller():\n"
        b'    return target("\xf0\x28\x8c\x28")\n'
    )
    _init_repo(tmp_path)

    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="pkg/mod.py",
                status="modified",
                changed_symbols=["target"],
                changed_lines=[1],
            )
        ]
    )

    contexts = TreeSitterLocalRetriever().retrieve(tmp_path, diff)

    assert contexts


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


def test_only_relevant_test_function_is_extracted(sample_repo: Path):
    """不再注入整个测试文件，只提取真正引用变更符号的测试函数。"""
    (sample_repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import target\n"
        "\n"
        "def test_target():\n"
        "    assert target(1) == 2\n"
        "\n"
        "def test_unrelated():\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
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

    assert [context.symbol for context in test_contexts] == ["test_target"]
    assert "test_unrelated" not in test_contexts[0].snippet
    assert "from pkg.mod import target" not in test_contexts[0].snippet


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


def test_reference_search_timeout_is_bounded(monkeypatch, tmp_path: Path):
    """检索超时只能降级，不能让 review 挂住，且要留下可见计数。"""

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    retriever = TreeSitterLocalRetriever()
    contexts = retriever._search_references(tmp_path, {"target"}, set(), {})

    assert contexts == []
    assert retriever.last_stats["grep_timeouts"] == 1


def test_reference_contexts_are_capped(tmp_path: Path):
    """高频符号不能把一次检索放大成上万条候选。"""
    from openreview.retriever.tree_sitter_retriever import (
        MAX_REFERENCE_CONTEXTS,
    )

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "def target(x):\n    return x + 1\n", encoding="utf-8"
    )
    for index in range(MAX_REFERENCE_CONTEXTS + 40):
        (pkg / f"use_{index}.py").write_text(
            "from pkg.mod import target\n"
            "\n"
            f"def use_{index}():\n"
            "    return target(1)\n",
            encoding="utf-8",
        )
    _init_repo(tmp_path)

    retriever = TreeSitterLocalRetriever()
    contexts = retriever._search_references(tmp_path, {"target"}, set(), {})

    assert len(contexts) <= MAX_REFERENCE_CONTEXTS
    assert retriever.last_stats["reference_contexts_truncated"] > 0
    assert retriever.last_stats["grep_timeouts"] == 0


def test_test_files_skip_p5_field_contexts(tmp_path: Path):
    """P0：测试文件不注入结构体字段，避免把测试表整段带进 prompt。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "program_test.go").write_text(
        "package pkg\n"
        "\n"
        "type testFile struct {\n"
        "    fileName string\n"
        "}\n"
        "\n"
        "func TestProgram() {\n"
        "    file := testFile{fileName: \"a\"}\n"
        "    _ = file.fileName\n"
        "}\n",
        encoding="utf-8",
    )
    _init_repo(tmp_path)

    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="pkg/program_test.go",
                status="modified",
                changed_lines=[9],
                is_test_file=True,
            )
        ]
    )

    contexts = TreeSitterLocalRetriever().retrieve(tmp_path, diff)

    assert not any(context.priority == 5 for context in contexts)


def test_p5_fields_from_same_struct_are_grouped(tmp_path: Path):
    """P2：同一 struct 的分散字段合并成一个带 type 包裹的 context。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.go").write_text(
        "package pkg\n"
        "\n"
        "type Service struct {\n"
        "    timeout int\n"
        "    retries int\n"
        "}\n"
        "\n"
        "func Run() int {\n"
        "    svc := Service{timeout: 1, retries: 2}\n"
        "    return svc.timeout + svc.retries\n"
        "}\n",
        encoding="utf-8",
    )
    _init_repo(tmp_path)

    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="pkg/mod.go",
                status="modified",
                changed_lines=[10],
            )
        ]
    )

    contexts = TreeSitterLocalRetriever().retrieve(tmp_path, diff)
    field_contexts = [context for context in contexts if context.priority == 5]

    assert len(field_contexts) == 1
    assert field_contexts[0].symbol == "Service"
    assert "type Service struct {" in field_contexts[0].snippet
    assert "timeout int" in field_contexts[0].snippet
    assert "retries int" in field_contexts[0].snippet
