from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from openreview.repo.diff_parser import DiffParser, ParsedChangedFile, ParsedDiff
from openreview.retriever import index as index_module
from openreview.retriever.hybrid import HybridRetriever
from openreview.retriever.index import RepoIndex, _iter_source_files, repo_cache_key
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


def test_repo_index_migrates_v1_schema_columns(tmp_path: Path):
    """旧缓存库没有 doc / callee_qualifier，打开时不能直接崩。"""
    import sqlite3

    db_path = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            qualname TEXT NOT NULL,
            kind TEXT NOT NULL,
            file_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            signature TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_file TEXT NOT NULL,
            caller_symbol TEXT NOT NULL DEFAULT '',
            callee_name TEXT NOT NULL,
            line INTEGER NOT NULL
        );
        """
    )
    conn.close()

    index = RepoIndex(db_path)
    try:
        symbol_columns = {
            row["name"]
            for row in index._conn.execute("PRAGMA table_info(symbols)")
        }
        call_columns = {
            row["name"]
            for row in index._conn.execute("PRAGMA table_info(calls)")
        }
        assert "doc" in symbol_columns
        assert "callee_qualifier" in call_columns
    finally:
        index.close()


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
                # 只暴露变更行本身，避免微小型 enclosing 因“整段都在
                # hunk 里”被新的 P2 过滤规则跳过。
                "patch": (
                    "@@ -6 +6 @@\n"
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
        assert any(context.kind == "enclosing" for context in contexts)
        assert any(context.kind == "dependency" for context in contexts)
        assert not any(context.kind == "import" for context in contexts)
        ids = [context.context_id for context in contexts]
        assert all(ids)
        assert len(ids) == len(set(ids))
    finally:
        retriever.close()


def test_hybrid_enables_grep_fallback_only_without_index(tmp_path: Path):
    """只有索引不可用时，AST 才被允许现场 grep 兜底。"""
    from openreview.retriever.code_context import CodeContext, ContextPriority

    class _RecordingAstRetriever:
        last_stats = {"ast_contexts": 0}

        def __init__(self) -> None:
            self.flags: list[bool] = []

        def retrieve(
            self,
            repo_path,
            parsed_diff,
            base_path=None,
            *,
            allow_global_fallback=False,
        ):
            self.flags.append(allow_global_fallback)
            return [
                CodeContext(
                    file_path="pkg/app.py",
                    reason="enclosing",
                    snippet="def f(): pass",
                    score=0.9,
                    kind="enclosing",
                    priority=ContextPriority.ENCLOSING,
                )
            ]

    ast_retriever = _RecordingAstRetriever()
    retriever = HybridRetriever(ast_retriever=ast_retriever)
    retriever._get_index = lambda *_: None  # type: ignore[method-assign]
    try:
        contexts = retriever.retrieve(tmp_path, ParsedDiff(files=[]))
    finally:
        retriever.close()

    assert contexts
    assert ast_retriever.flags == [True]


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

        # 精确命中之后仍会走 LIKE 兜底，兜底同样必须转义。
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
    """Token 预算造成的丢失必须可观测，而不是静默截断。"""
    from openreview.retriever.code_context import CodeContext, ContextPriority

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
                    priority=ContextPriority.DEFINITION,
                    start_line=line,
                    end_line=line,
                )
                for line in range(1, 5)
            ]

    retriever = HybridRetriever(
        ast_retriever=_FakeAstRetriever(),
        enabled=False,
        max_context_tokens=130,
    )

    contexts = retriever.retrieve(tmp_path, ParsedDiff(files=[]))

    assert len(contexts) == 2
    assert retriever.last_stats["contexts_candidates"] == 4
    assert retriever.last_stats["contexts_deduped"] == 0
    assert retriever.last_stats["contexts_truncated"] == 2
    assert retriever.last_stats["context_tokens_remaining"] >= 0
    assert retriever.last_stats["ast_contexts_truncated"] == 1


def test_hybrid_plan_false_returns_unplanned_candidates(tmp_path: Path):
    """executor 统一规划时，retriever 只返回去重后的候选。"""
    from openreview.retriever.code_context import CodeContext, ContextPriority

    class _FakeAstRetriever:
        last_stats = {"ast_contexts": 4}

        def retrieve(self, repo_path, parsed_diff, base_path=None):
            return [
                CodeContext(
                    file_path="pkg/app.py",
                    reason=f"definition {line}",
                    snippet=f"def f{line}(): pass",
                    score=0.9 - line / 100,
                    kind="definition",
                    priority=ContextPriority.DEFINITION,
                    start_line=line,
                    end_line=line,
                )
                for line in range(1, 5)
            ]

    retriever = HybridRetriever(
        ast_retriever=_FakeAstRetriever(),
        enabled=False,
        max_context_tokens=60,
    )

    contexts = retriever.retrieve(tmp_path, ParsedDiff(files=[]), plan=False)

    assert len(contexts) == 4
    assert retriever.last_stats["contexts_candidates"] == 4
    assert retriever.last_stats["contexts_deduped"] == 0
    # 未规划就不能提前报告预算淘汰/选中数，否则 executor 会重复计数。
    assert "contexts_truncated" not in retriever.last_stats
    assert "contexts_selected" not in retriever.last_stats


def test_supports_plan_detects_optional_keyword(tmp_path: Path):
    from openreview.retriever.base import supports_plan

    class OldRetriever:
        def retrieve(self, repo_path, parsed_diff, base_path=None):
            return []

    class NewRetriever:
        def retrieve(self, repo_path, parsed_diff, base_path=None, *, plan=True):
            return []

    assert supports_plan(NewRetriever()) is True
    assert supports_plan(OldRetriever()) is False
    assert supports_plan(object()) is False


def test_index_cache_is_reused_across_linked_worktrees(tmp_path: Path):
    """Per-run worktrees at the same commit must share one SQLite index."""
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(worktree), "HEAD"],
        cwd=root,
        check=True,
    )

    cache_dir = tmp_path / "idx"
    first = RepoIndex.for_worktree(root, cache_dir=cache_dir)
    first_db = first.db_path
    first.close()

    second = RepoIndex.for_worktree(worktree, cache_dir=cache_dir)
    try:
        assert second.db_path == first_db
        assert second.last_stats["index_reused"] == 1
    finally:
        second.close()


def test_prune_index_cache_respects_exclude_size_and_age(tmp_path: Path):
    cache_dir = tmp_path / "idx"
    cache_dir.mkdir()
    old = cache_dir / "old.sqlite3"
    keep = cache_dir / "keep.sqlite3"
    old.write_bytes(b"x" * 4096)
    keep.write_bytes(b"x" * 4096)
    os.utime(old, (1, 1))

    removed = index_module.prune_index_cache(
        cache_dir,
        exclude={keep},
        max_bytes=1,
        max_age_seconds=0,
    )
    assert removed == 1
    assert not old.exists()
    assert keep.exists()

    aged = cache_dir / "aged.sqlite3"
    aged.write_bytes(b"x")
    os.utime(aged, (1, 1))
    removed = index_module.prune_index_cache(
        cache_dir,
        exclude={keep},
        max_bytes=10**9,
        max_age_seconds=60,
        now=10**6,
    )
    assert removed == 1
    assert not aged.exists()
    assert keep.exists()



def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", message],
        cwd=root,
        check=True,
    )


def _write_many_files(root: Path, count: int = 12) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / "pkg" / f"m{index}.py").write_text(
            f"def f{index}():\n    return {index}\n\n"
            f"def g{index}():\n    return f{index}()\n",
            encoding="utf-8",
        )


def test_index_seeds_from_latest_head_and_indexes_only_diff(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_many_files(root)
    _init_repo(root)
    cache_dir = tmp_path / "idx"

    first = RepoIndex.for_worktree(root, cache_dir=cache_dir)
    try:
        first_symbols = first.count("symbols")
        assert first_symbols == 24
        assert first.last_stats.get("index_seeded") is None
    finally:
        first.close()

    seed_path = cache_dir / f"seed-{repo_cache_key(root)}.sqlite3"
    assert seed_path.is_file()

    for index in (0, 1):
        (root / "pkg" / f"m{index}.py").write_text(
            f"def f{index}():\n    return {index + 100}\n\n"
            f"def g{index}():\n    return f{index}()\n",
            encoding="utf-8",
        )
    _commit(root, "small change")

    second = RepoIndex.for_worktree(root, cache_dir=cache_dir)
    try:
        # 只复制 seed + 应用 2 个文件的差集，不再全量扫描 12 个文件。
        assert second.last_stats["index_seeded"] == 1
        assert second.last_stats["index_seed_diff_files"] == 2
        assert second.last_stats["index_files_indexed"] == 2
        assert second.count("symbols") == first_symbols
        assert any(symbol.name == "f1" for symbol in second.search_symbols(["f1"]))
    finally:
        second.close()


def test_index_skips_seed_when_diff_is_too_large(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_many_files(root, count=10)
    _init_repo(root)
    cache_dir = tmp_path / "idx"

    first = RepoIndex.for_worktree(root, cache_dir=cache_dir)
    first.close()

    # 8/10 文件变化，ratio 0.8 > 默认 0.35，复制 seed 不划算，应全量重建。
    for index in range(8):
        (root / "pkg" / f"m{index}.py").write_text(
            f"def f{index}():\n    return {index + 100}\n\n"
            f"def g{index}():\n    return f{index}()\n",
            encoding="utf-8",
        )
    _commit(root, "large change")

    second = RepoIndex.for_worktree(root, cache_dir=cache_dir)
    try:
        assert second.last_stats.get("index_seeded") is None
        assert second.last_stats["index_files_indexed"] == 10
        assert second.count("symbols") == 20
    finally:
        second.close()


def test_index_seed_can_be_disabled_by_env(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _write_many_files(root, count=6)
    _init_repo(root)
    cache_dir = tmp_path / "idx"

    RepoIndex.for_worktree(root, cache_dir=cache_dir).close()

    monkeypatch.setenv("OPENREVIEW_INDEX_SEED", "0")
    (root / "pkg" / "m0.py").write_text(
        "def f0():\n    return 100\n\n"
        "def g0():\n    return f0()\n",
        encoding="utf-8",
    )
    _commit(root, "small change")

    second = RepoIndex.for_worktree(root, cache_dir=cache_dir)
    try:
        assert second.last_stats.get("index_seeded") is None
        # 关闭 seed 后，新 HEAD 仍是全量扫描。
        assert second.last_stats["index_files_indexed"] == 6
    finally:
        second.close()


def test_index_seed_falls_back_when_seed_commit_is_missing(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_many_files(root, count=6)
    _init_repo(root)
    cache_dir = tmp_path / "idx"

    RepoIndex.for_worktree(root, cache_dir=cache_dir).close()

    # 模拟 seed 来自另一个 clone：head_sha 在当前仓库不存在。
    seed_path = cache_dir / f"seed-{repo_cache_key(root)}.sqlite3"
    conn = sqlite3.connect(str(seed_path))
    try:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'head_sha'",
            ("0" * 40,),
        )
        conn.commit()
    finally:
        conn.close()

    (root / "pkg" / "m0.py").write_text(
        "def f0():\n    return 100\n\n"
        "def g0():\n    return f0()\n",
        encoding="utf-8",
    )
    _commit(root, "small change")

    second = RepoIndex.for_worktree(root, cache_dir=cache_dir)
    try:
        assert second.last_stats.get("index_seeded") is None
        assert second.last_stats["index_files_indexed"] == 6
        assert second.count("symbols") == 12
    finally:
        second.close()


def test_prune_index_cache_pins_seed_from_size_but_not_age(tmp_path: Path):
    cache_dir = tmp_path / "idx"
    cache_dir.mkdir()
    seed = cache_dir / "seed-abc.sqlite3"
    normal = cache_dir / "normal.sqlite3"
    seed.write_bytes(b"x" * 4096)
    normal.write_bytes(b"x" * 4096)
    os.utime(seed, (1, 1))
    os.utime(normal, (2, 2))

    removed = index_module.prune_index_cache(
        cache_dir, max_bytes=1, max_age_seconds=0
    )
    assert removed == 1
    assert seed.exists()  # seed 不参与大小淘汰
    assert not normal.exists()

    removed = index_module.prune_index_cache(
        cache_dir,
        max_bytes=10**9,
        max_age_seconds=60,
        now=10**6,
    )
    assert removed == 1
    assert not seed.exists()  # 长期未使用仍会被 age GC


def test_go_index_extracts_imports_and_qualified_callers(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "main.go").write_text(
        "package pkg\n"
        "\n"
        'import "fmt"\n'
        "\n"
        "func helper() {\n"
        '    fmt.Println("x")\n'
        "}\n"
        "\n"
        "func Run() {\n"
        "    helper()\n"
        "    helper()\n"
        "}\n"
        "\n"
        "func (s *Server) Do() {\n"
        "    helper()\n"
        "}\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        imports = index.imports_of(["pkg/main.go"])
        assert [(edge.dst_module, edge.line) for edge in imports] == [("fmt", 3)]

        run_calls = index.callees_of(["Run"])
        assert {(edge.callee_name, edge.line) for edge in run_calls} == {
            ("helper", 10),
            ("helper", 11),
        }
        assert {edge.caller_symbol for edge in run_calls} == {"Run"}

        method_calls = index.callees_of(["Do"])
        assert {(edge.callee_name, edge.line) for edge in method_calls} == {
            ("helper", 15)
        }
        assert {edge.caller_symbol for edge in method_calls} == {"Server.Do"}
    finally:
        index.close()


def test_go_imports_normalize_module_path_for_dependency(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n")
    (root / "lib").mkdir()
    (root / "app").mkdir()
    (root / "lib" / "lib.go").write_text(
        "package lib\n\nfunc Helper() int { return 1 }\n"
    )
    (root / "app" / "app.go").write_text(
        "package app\n\n"
        'import l "example.com/demo/lib"\n\n'
        "func Run() int { return l.Helper() }\n"
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        imports = index.imports_of(["app/app.go"])
        assert [(edge.dst_module, edge.symbol) for edge in imports] == [
            ("lib", "l")
        ]
        definitions = index.definitions_in_modules(["lib"], limit=5)
        assert {item.name for item in definitions} == {"Helper"}
    finally:
        index.close()


def test_go_call_extraction_does_not_rescan_tree_per_call(monkeypatch):
    """回归保护：Go calls 必须单次遍历，不能再逐 call 全树查找。"""
    from openreview.retriever import ast_utils
    from openreview.retriever.index import _go_imports_and_calls

    parsed = ast_utils.parse_text(
        "package pkg\n"
        "\n"
        "func Run() {\n"
        "    helper()\n"
        "}\n",
        ".go",
    )
    assert parsed is not None
    _, source, tree = parsed

    def _boom(*args, **kwargs):
        raise AssertionError("find_enclosing_definition is O(tree) per call")

    monkeypatch.setattr(ast_utils, "find_enclosing_definition", _boom)

    imports, calls = _go_imports_and_calls(tree, source)
    assert imports == []
    assert calls == [("Run", "helper", "", 4)]
