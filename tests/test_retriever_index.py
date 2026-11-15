from __future__ import annotations

import subprocess
from pathlib import Path

from openreview.repo.diff_parser import DiffParser, ParsedChangedFile, ParsedDiff
from openreview.retriever import index as index_module
from openreview.retriever.hybrid import HybridRetriever
from openreview.retriever.index import RepoIndex, _iter_source_files
from openreview.retriever.pathing import is_source_file
from openreview.retriever.query import MAX_ADDED_LINE_IDENTIFIERS, QueryBuilder
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


def _write_repo(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "auth.py").write_text(
        "import os\n"
        "from pkg.models import User\n"
        "\n"
        "class AuthService:\n"
        "    def login(self, user: User):\n"
        "        return make_token(user)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "models.py").write_text(
        "class User:\n    pass\n", encoding="utf-8"
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.auth import AuthService\n"
        "\n"
        "def handle(user):\n"
        "    return AuthService().login(user)\n",
        encoding="utf-8",
    )


def test_hybrid_related_files_links_imports_and_tests(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    (root / "tests").mkdir()
    (root / "tests" / "test_auth.py").write_text(
        "from pkg.auth import AuthService\n", encoding="utf-8"
    )
    _init_repo(root)

    parsed = ParsedDiff(
        files=[
            ParsedChangedFile(filename="pkg/auth.py", status="modified"),
            ParsedChangedFile(filename="pkg/api.py", status="modified"),
            ParsedChangedFile(filename="tests/test_auth.py", status="modified"),
        ]
    )
    retriever = HybridRetriever()
    try:
        related = retriever.related_files(root, parsed)
    finally:
        retriever.close()

    assert "pkg/api.py" in related["pkg/auth.py"]
    assert "tests/test_auth.py" in related["pkg/auth.py"]


def test_repo_index_builds_symbols_imports_and_search(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        assert index.last_stats["index_symbols"] >= 4
        symbols = index.search_symbols(["login"])
        assert symbols
        assert symbols[0].qualname == "AuthService.login"
        assert symbols[0].start_line == 5

        importers = index.importers_of(["pkg.auth"])
        assert any(edge.src_file == "pkg/api.py" for edge in importers)

        imports = index.imports_of(["pkg/auth.py"])
        assert any(edge.dst_module == "pkg.models" for edge in imports)

        definitions = index.definitions_in_modules(["pkg.models"])
        assert any(symbol.name == "User" for symbol in definitions)
    finally:
        index.close()


def test_repo_index_is_incremental_by_git_blob_sha(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        assert any(symbol.name == "login" for symbol in index.search_symbols(["login"]))

        auth = root / "pkg" / "auth.py"
        auth.write_text(
            auth.read_text(encoding="utf-8").replace("login", "logout"),
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=a@b.c",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "rename login",
            ],
            cwd=root,
            check=True,
        )

        index.ensure(root)
        assert not index.search_symbols(["login"])
        assert any(symbol.name == "logout" for symbol in index.search_symbols(["logout"]))
        # 只重排变更文件：diff 里只有 pkg/auth.py，其它文件不应被重新索引。
        assert index.last_stats["index_files_indexed"] == 1
    finally:
        index.close()


def test_query_builder_extracts_symbols_modules_and_imports(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        parsed = DiffParser.parse_diff(
            [
                {
                    "filename": "pkg/api.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": (
                        "@@ -3,3 +3,3 @@\n"
                        "-def handle(user):\n"
                        "-    return None\n"
                        "+def handle(user, extra):\n"
                        "+    return AuthService().login(user)\n"
                    ),
                }
            ]
        )
        query = QueryBuilder().build(parsed, index=index)
        assert "handle" in query.symbols
        assert "pkg.api" in query.modules
        assert "pkg.auth" in query.imports
        assert "AuthService" in query.terms
    finally:
        index.close()


def test_query_builder_derives_enclosing_symbols_from_ast(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        # 只改函数体、不改签名：diff 正则提取不到 changed_symbols。
        parsed = DiffParser.parse_diff(
            [
                {
                    "filename": "pkg/auth.py",
                    "status": "modified",
                    "patch": (
                        "@@ -5,2 +5,2 @@\n"
                        "     def login(self, user: User):\n"
                        "-        return None\n"
                        "+        return make_token(user)\n"
                    ),
                }
            ]
        )
        query = QueryBuilder().build(parsed, index=index)

        assert "login" in query.symbols
        assert "AuthService.login" in query.symbols
    finally:
        index.close()


def test_hybrid_retriever_adds_index_contexts_with_ids(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    parsed = DiffParser.parse_diff(
        [
            {
                "filename": "pkg/auth.py",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": (
                    "@@ -4,3 +4,3 @@\n"
                    " class AuthService:\n"
                    "     def login(self, user: User):\n"
                    "-        return None\n"
                    "+        return make_token(user)\n"
                ),
            }
        ]
    )
    retriever = HybridRetriever(index_cache_dir=tmp_path / "idx")
    try:
        contexts = retriever.retrieve(root, parsed)
        assert contexts
        assert retriever.last_stats["index_symbol_hits"] >= 1
        assert any(context.kind == "enclosing" for context in contexts)
        assert any(context.kind in {"import", "dependency"} for context in contexts)
        ids = [context.context_id for context in contexts]
        assert all(ids)
        assert len(ids) == len(set(ids))
    finally:
        retriever.close()


def test_index_like_lookups_escape_wildcards(tmp_path: Path):
    """符号/模块里的 ``_`` 不能被 SQL LIKE 当成“任意单字符”。"""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "foo_bar.py").write_text(
        "def get_user():\n    return 1\n", encoding="utf-8"
    )
    (root / "pkg" / "fooXbar.py").write_text(
        "def getXuser():\n    return 2\n", encoding="utf-8"
    )
    (root / "pkg" / "user.py").write_text(
        "from pkg.foo_bar import get_user\n", encoding="utf-8"
    )
    (root / "pkg" / "decoy.py").write_text(
        "from pkg.fooXbar import getXuser\n", encoding="utf-8"
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        importers = index.importers_of(["pkg.foo_bar"])
        importer_files = {edge.src_file for edge in importers}
        assert "pkg/user.py" in importer_files
        assert "pkg/decoy.py" not in importer_files

        definitions = index.definitions_in_modules(["pkg.foo_bar"])
        definition_names = {symbol.name for symbol in definitions}
        assert "get_user" in definition_names
        assert "getXuser" not in definition_names

        # 精确命中之后仍会走 FTS/LIKE 兜底，兜底同样必须转义。
        symbols = index.search_symbols(["get_user"], limit=20)
        symbol_names = {symbol.name for symbol in symbols}
        assert "get_user" in symbol_names
        assert "getXuser" not in symbol_names
    finally:
        index.close()


def test_query_builder_caps_added_line_identifiers():
    patch = "\n".join(
        f"+alpha_{index:03d} = compute_{index:03d}" for index in range(100)
    )
    parsed = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="pkg/sample.py",
                status="modified",
                patch=patch,
            )
        ]
    )

    identifiers, _ = QueryBuilder._collect_added_line_terms(parsed)

    assert len(identifiers) == MAX_ADDED_LINE_IDENTIFIERS
    assert len(identifiers) == len(set(identifiers))


def test_index_file_cap_counts_only_indexable_files(tmp_path: Path, monkeypatch):
    """文档/图片不能挤占源码索引名额。

    旧实现按 git ls-files 的原始顺序计数；当仓库里非源码文件很多时，
    可能在扫到 Python/Go 文件之前就达到 MAX_INDEXED_FILES。
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.md").write_text("# docs\n", encoding="utf-8")
    (root / "b.py").write_text("def first():\n    pass\n", encoding="utf-8")
    (root / "c.py").write_text("def second():\n    pass\n", encoding="utf-8")
    _init_repo(root)

    monkeypatch.setattr(index_module, "MAX_INDEXED_FILES", 1)

    files = list(_iter_source_files(root))

    assert files == ["b.py"]


def test_retrieval_mechanisms_share_path_filters(tmp_path: Path):
    """AST / index 必须对 docs、examples、vendor 做一致剪枝。"""
    assert is_source_file("pkg/app.py", {".py"})
    assert not is_source_file("docs/conf.py", {".py"})
    assert not is_source_file("examples/demo.py", {".py"})
    assert not is_source_file("vendor/lib.go", {".go"})

    retriever = TreeSitterLocalRetriever()
    assert retriever._is_valid_source_file("pkg/app.py")
    assert not retriever._is_valid_source_file("docs/conf.py")
    assert not retriever._is_valid_source_file("examples/demo.py")

    assert index_module._is_indexable("pkg/app.py")
    assert not index_module._is_indexable("docs/conf.py")
    assert not index_module._is_indexable("vendor/lib.go")


def test_hybrid_reports_candidates_deduped_and_truncated(tmp_path: Path):
    """检索上限造成的丢失必须可观测，而不是静默截断。"""
    from openreview.retriever.code_context import CodeContext

    class _FakeAstRetriever:
        last_stats = {"ast_contexts": 4, "ast_contexts_truncated": 1}

        def retrieve(self, repo_path, parsed_diff, base_path=None):
            return [
                CodeContext(
                    file_path="pkg/app.py",
                    reason=f"definition {line}",
                    snippet=f"def f{line}(): pass",
                    score=0.9 - line / 100,
                    kind="definition",
                    start_line=line,
                    end_line=line,
                )
                for line in range(1, 5)
            ]

    retriever = HybridRetriever(
        ast_retriever=_FakeAstRetriever(),
        enabled=False,
        max_contexts=2,
    )

    contexts = retriever.retrieve(tmp_path, ParsedDiff(files=[]))

    assert len(contexts) == 2
    assert retriever.last_stats["contexts_candidates"] == 4
    assert retriever.last_stats["contexts_deduped"] == 0
    assert retriever.last_stats["contexts_truncated"] == 2
    assert retriever.last_stats["ast_contexts_truncated"] == 1

