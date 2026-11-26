from __future__ import annotations

import subprocess
from pathlib import Path

from openreview.retriever.ast_utils import FileLineCache
from openreview.retriever.code_context import (
    CodeContext,
    ContextPriority,
    dedupe_contexts,
)
from openreview.retriever.graph import GraphRetriever
from openreview.retriever.index import IndexedSymbol, RepoIndex
from openreview.retriever.query import RetrievalQuery


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
        "from pkg.models import User\n"
        "from pkg.util import make_token\n"
        "\n"
        "class AuthService:\n"
        "    def login(self, user: User):\n"
        "        return make_token(user)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.auth import AuthService\n"
        "\n"
        "def handle(user):\n"
        "    return AuthService().login(user)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "models.py").write_text(
        "class User:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (root / "pkg" / "util.py").write_text(
        "def make_token(user):\n"
        "    return 'token'\n",
        encoding="utf-8",
    )


def _query() -> RetrievalQuery:
    return RetrievalQuery(
        symbols=["login"],
        files=["pkg/auth.py"],
        modules=["pkg.auth"],
        imports=["pkg.models"],
        terms=["login"],
        signature_changed_symbols=["login"],
    )


def test_graph_retriever_expands_callers_callees_and_dependencies(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        stats: dict[str, float] = {}
        contexts = GraphRetriever().retrieve(root, index, _query(), stats=stats)

        kinds = {context.kind for context in contexts}
        assert kinds == {"caller", "callee", "dependency"}

        caller = next(context for context in contexts if context.kind == "caller")
        assert caller.file_path == "pkg/api.py"
        assert caller.symbol == "handle"
        assert "def handle" in caller.snippet

        callee = next(context for context in contexts if context.kind == "callee")
        assert callee.file_path == "pkg/util.py"
        assert callee.symbol == "make_token"
        assert "def make_token" in callee.snippet

        dependency = next(
            context for context in contexts if context.kind == "dependency"
        )
        assert dependency.file_path == "pkg/models.py"
        assert dependency.symbol == "User"
        assert dependency.priority == ContextPriority.DEFINITION

        assert stats["graph_caller_hits"] == 1
        assert stats["graph_callee_hits"] == 1
        assert stats["index_dependency_hits"] == 1
        assert stats["graph_error"] == 0
        # P6 不再收集“谁 import 了变更模块”。
        assert "index_importer_hits" not in stats
    finally:
        index.close()


def test_graph_context_ids_are_stable_and_unique(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    retriever = GraphRetriever()
    try:
        first = retriever.retrieve(root, index, _query())
        second = retriever.retrieve(root, index, _query())

        first_ids = sorted(context.ensure_context_id() for context in first)
        second_ids = sorted(context.ensure_context_id() for context in second)
        assert first_ids == second_ids
        assert len(first_ids) == len(set(first_ids))
        assert all(context_id.startswith("ctx-") for context_id in first_ids)
    finally:
        index.close()


def test_caller_contexts_dedupe_multiple_call_sites(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "auth.py").write_text(
        "def login(user):\n"
        "    return user\n",
        encoding="utf-8",
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.auth import login\n"
        "\n"
        "def handle(user):\n"
        "    login(user)\n"
        "    login(user)\n"
        "    login(user)\n"
        "\n"
        "def other(user):\n"
        "    return login(user)\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        stats: dict[str, float] = {}
        contexts = GraphRetriever(max_callers=8).retrieve(
            root,
            index,
            RetrievalQuery(
                symbols=["login"],
                files=["pkg/auth.py"],
                signature_changed_symbols=["login"],
            ),
            stats=stats,
        )

        callers = [context for context in contexts if context.kind == "caller"]
        # handle 调用了 3 次 login，但同一个 caller 只应注入一次。
        assert {context.symbol for context in callers} == {"handle", "other"}
        assert len(callers) == 2
        assert stats["graph_caller_hits"] == 2
    finally:
        index.close()


def test_callee_definition_is_disambiguated_by_import(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "util.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (root / "pkg" / "other.py").write_text(
        "def helper():\n    return 2\n", encoding="utf-8"
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.other import helper\n"
        "\n"
        "def get_user():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        contexts = GraphRetriever().retrieve(
            root,
            index,
            RetrievalQuery(symbols=["get_user"], files=["pkg/api.py"]),
        )
        callee = next(
            context for context in contexts if context.kind == "callee"
        )
        assert callee.file_path == "pkg/other.py"
    finally:
        index.close()


def test_callees_of_escapes_like_wildcards(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "util.py").write_text(
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.util import helper\n"
        "\n"
        "def get_user():\n"
        "    return helper()\n"
        "\n"
        "def getXuser():\n"
        "    return helper()\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        edges = index.callees_of(["get_user"])
        assert {edge.caller_symbol for edge in edges} == {"get_user"}
    finally:
        index.close()


def test_call_graph_failure_does_not_drop_dependency_contexts(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "models.py").write_text(
        "class User:\n    pass\n",
        encoding="utf-8",
    )

    class BrokenCallGraphIndex:
        def callers_of(self, *_args, **_kwargs):
            raise RuntimeError("call graph unavailable")

        def callees_of(self, *_args, **_kwargs):
            raise RuntimeError("call graph unavailable")

        def definitions_in_modules(self, *_args, **_kwargs):
            return [
                IndexedSymbol(
                    name="User",
                    qualname="User",
                    kind="class_definition",
                    file_path="pkg/models.py",
                    start_line=1,
                    end_line=2,
                )
            ]

    stats: dict[str, float] = {}
    contexts = GraphRetriever().retrieve(
        root,
        BrokenCallGraphIndex(),  # type: ignore[arg-type]
        RetrievalQuery(
            symbols=["login"],
            modules=["pkg.auth"],
            imports=["pkg.models"],
            signature_changed_symbols=["login"],
        ),
        stats=stats,
    )

    assert any(context.kind == "dependency" for context in contexts)
    assert stats["graph_error"] == 2


def test_graph_retriever_ignores_non_python_callers_when_configured(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    (root / "pkg" / "main.go").write_text(
        "package pkg\n"
        "\n"
        "func main() {\n"
        "    login(user)\n"
        "}\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        stats: dict[str, float] = {}
        contexts = GraphRetriever(python_calls_only=True).retrieve(
            root, index, _query(), stats=stats
        )
        caller_files = {
            context.file_path for context in contexts if context.kind == "caller"
        }
        assert "pkg/main.go" not in caller_files
    finally:
        index.close()


def test_context_dedupe_uses_range_identity_across_kinds():
    """同一段代码由 AST / index / graph 以不同 kind 找到时只保留一条。"""
    contexts = [
        CodeContext(
            file_path="pkg/auth.py",
            reason="enclosing",
            snippet="def login():\n    pass",
            score=0.95,
            kind="enclosing",
            priority=ContextPriority.ENCLOSING,
            start_line=10,
            end_line=11,
        ),
        CodeContext(
            file_path="pkg/auth.py",
            reason="definition",
            snippet="def login():\n    pass",
            score=0.90,
            kind="definition",
            priority=ContextPriority.DEFINITION,
            start_line=10,
            end_line=11,
        ),
        CodeContext(
            file_path="pkg/api.py",
            reason="reference",
            snippet="login()",
            score=0.60,
            kind="reference",
            priority=ContextPriority.REFERENCE,
            start_line=3,
            end_line=3,
        ),
    ]

    deduped = dedupe_contexts(contexts)

    assert len(deduped) == 2
    assert deduped[0].kind == "enclosing"
    assert deduped[0].score == 0.95


def test_caller_contexts_only_emitted_for_signature_changes(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        contexts = GraphRetriever().retrieve(
            root,
            index,
            RetrievalQuery(symbols=["login"], files=["pkg/auth.py"]),
        )

        # 只改函数体时，调用点不是证据，不应注入 caller 上下文。
        assert not any(context.kind == "caller" for context in contexts)
    finally:
        index.close()


def test_callee_contexts_ignore_stdlib_and_third_party_calls(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    # 仓库里存在同名定义，但调用方 import 的是标准库 json，不能误匹配。
    (root / "pkg" / "other.py").write_text(
        "def loads(payload):\n"
        "    return payload\n",
        encoding="utf-8",
    )
    (root / "pkg" / "api.py").write_text(
        "import json\n"
        "\n"
        "def parse(payload):\n"
        "    return json.loads(payload)\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        contexts = GraphRetriever().retrieve(
            root,
            index,
            RetrievalQuery(symbols=["parse"], files=["pkg/api.py"]),
        )

        assert not any(
            context.kind == "callee" and context.symbol == "loads"
            for context in contexts
        )
    finally:
        index.close()


def test_short_private_helper_expands_full_body(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "api.py").write_text(
        "def _short(x):\n"
        "    return x + 1\n"
        "\n"
        "def get_user():\n"
        "    return _short(1)\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        contexts = GraphRetriever().retrieve(
            root,
            index,
            RetrievalQuery(symbols=["get_user"], files=["pkg/api.py"]),
        )
        callee = next(
            context for context in contexts if context.kind == "callee"
        )

        assert callee.symbol == "_short"
        assert "return x + 1" in callee.snippet
        assert "full body" in callee.reason
    finally:
        index.close()


def test_long_private_helper_keeps_signature_only(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    body = "".join(f"    value += {index}\n" for index in range(30))
    (root / "pkg" / "api.py").write_text(
        "def _long(value):\n"
        f"{body}"
        "    return value\n"
        "\n"
        "def get_user():\n"
        "    return _long(1)\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        contexts = GraphRetriever().retrieve(
            root,
            index,
            RetrievalQuery(symbols=["get_user"], files=["pkg/api.py"]),
        )
        callee = next(
            context for context in contexts if context.kind == "callee"
        )

        assert callee.symbol == "_long"
        assert "def _long(value):" in callee.snippet
        assert "value += 0" not in callee.snippet
        assert "signature" in callee.reason
    finally:
        index.close()


def test_callee_imports_are_batched_by_caller_file(tmp_path: Path):
    """多个 callee 边共享同一个 caller 时应只查一次 imports。"""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "util.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    for name in ("a", "b", "c"):
        (root / "pkg" / f"{name}.py").write_text(
            "from pkg.util import helper\n"
            "\n"
            f"def run_{name}():\n"
            "    return helper()\n",
            encoding="utf-8",
        )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    calls: list[list[str]] = []

    class RecordingIndex:
        def __init__(self, inner: RepoIndex) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        def imports_of(self, files):
            calls.append(list(files))
            return self._inner.imports_of(files)

    try:
        GraphRetriever(max_callees=8).retrieve(
            root,
            RecordingIndex(index),  # type: ignore[arg-type]
            RetrievalQuery(
                symbols=["run_a", "run_b", "run_c"],
                files=["pkg/a.py", "pkg/b.py", "pkg/c.py"],
            ),
        )
    finally:
        index.close()

    # 三个 caller 文件 -> 一次批量查询，而不是三条 SQL。
    assert len(calls) == 1
    assert set(calls[0]) == {"pkg/a.py", "pkg/b.py", "pkg/c.py"}


def test_file_line_cache_reads_once_per_path(tmp_path: Path, monkeypatch):
    path = tmp_path / "a.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    reads: list[Path] = []
    original = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    cache = FileLineCache()
    assert cache.read_range(path, 1, 1) == "one"
    assert cache.read_range(path, 2, 3) == "two\nthree"
    assert cache.lines(path) == ["one", "two", "three"]

    assert reads.count(path) == 1


def test_graph_retriever_uses_injected_line_cache(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    class RecordingCache(FileLineCache):
        def __init__(self) -> None:
            super().__init__()
            self.paths: list[Path] = []

        def lines(self, path: Path):
            self.paths.append(Path(path))
            return super().lines(path)

    cache = RecordingCache()
    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        GraphRetriever(line_cache=cache).retrieve(root, index, _query())
    finally:
        index.close()

    names = {path.name for path in cache.paths}
    # caller / callee / dependency 都来自共享缓存，不再各自直接读文件。
    assert {"api.py", "util.py", "models.py"} <= names



def test_graph_skips_low_value_reference_paths(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "benchmarks").mkdir()
    (root / "pkg" / "auth.py").write_text(
        "def login(user):\n    return user\n", encoding="utf-8"
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.auth import login\n"
        "\n"
        "def handle(user):\n"
        "    return login(user)\n",
        encoding="utf-8",
    )
    (root / "benchmarks" / "bench.py").write_text(
        "from pkg.auth import login\n"
        "\n"
        "def bench(user):\n"
        "    return login(user)\n",
        encoding="utf-8",
    )
    (root / "benchmarks" / "helpers.py").write_text(
        "def bench_helper():\n    return 1\n", encoding="utf-8"
    )
    (root / "pkg" / "runner.py").write_text(
        "from benchmarks.helpers import bench_helper\n"
        "\n"
        "def run():\n"
        "    return bench_helper()\n",
        encoding="utf-8",
    )
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        contexts = GraphRetriever().retrieve(
            root,
            index,
            RetrievalQuery(
                symbols=["run"],
                files=["pkg/runner.py"],
                signature_changed_symbols=["login"],
            ),
        )

        caller_files = {
            context.file_path for context in contexts if context.kind == "caller"
        }
        assert caller_files == {"pkg/api.py"}
        assert not any(
            context.kind == "callee"
            and context.file_path == "benchmarks/helpers.py"
            for context in contexts
        )
    finally:
        index.close()


def test_graph_retriever_finds_go_callers_callees_and_dependencies(tmp_path: Path):
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
        'import "example.com/demo/lib"\n\n'
        "func Run() int { return lib.Helper() }\n"
    )
    (root / "app" / "caller.go").write_text(
        "package app\n\nfunc Use() int { return Run() }\n"
    )
    _init_repo(root)

    query = RetrievalQuery(
        symbols=["Run"],
        files=["app/app.go"],
        modules=["app"],
        imports=["lib"],
        terms=["Run"],
        signature_changed_symbols=["Run"],
    )
    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        stats: dict[str, float] = {}
        contexts = GraphRetriever().retrieve(root, index, query, stats=stats)

        by_kind = {}
        for context in contexts:
            by_kind.setdefault(context.kind, set()).add(
                (context.file_path, context.symbol)
            )
        assert ("app/caller.go", "Use") in by_kind.get("caller", set())
        assert ("lib/lib.go", "Helper") in by_kind.get("callee", set())
        assert ("lib/lib.go", "Helper") in by_kind.get("dependency", set())
        assert stats["graph_caller_hits"] >= 1
        assert stats["graph_callee_hits"] >= 1
        assert stats["index_dependency_hits"] >= 1
    finally:
        index.close()
