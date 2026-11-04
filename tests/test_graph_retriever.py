from __future__ import annotations

import subprocess
from pathlib import Path

from openreview.retriever.graph import GraphRetriever
from openreview.retriever.index import ImportEdge, RepoIndex
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
    )


def test_graph_retriever_expands_callers_callees_and_imports(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    _init_repo(root)

    index = RepoIndex.for_worktree(root, cache_dir=tmp_path / "idx")
    try:
        stats: dict[str, float] = {}
        contexts = GraphRetriever().retrieve(root, index, _query(), stats=stats)

        kinds = {context.kind for context in contexts}
        assert {"caller", "callee", "import", "dependency"} <= kinds

        caller = next(context for context in contexts if context.kind == "caller")
        assert caller.file_path == "pkg/api.py"
        assert caller.symbol == "handle"
        assert "def handle" in caller.snippet

        callee = next(context for context in contexts if context.kind == "callee")
        assert callee.file_path == "pkg/util.py"
        assert callee.symbol == "make_token"
        assert "def make_token" in callee.snippet

        importer = next(context for context in contexts if context.kind == "import")
        assert importer.file_path == "pkg/api.py"
        assert "from pkg.auth import AuthService" in importer.snippet

        dependency = next(
            context for context in contexts if context.kind == "dependency"
        )
        assert dependency.file_path == "pkg/models.py"
        assert dependency.symbol == "User"

        assert stats["graph_caller_hits"] == 1
        assert stats["graph_callee_hits"] == 1
        assert stats["index_importer_hits"] == 1
        assert stats["index_dependency_hits"] == 1
        assert stats["graph_error"] == 0
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
            RetrievalQuery(symbols=["login"], files=["pkg/auth.py"]),
            stats=stats,
        )

        callers = [context for context in contexts if context.kind == "caller"]
        # handle 调用了 3 次 login，但同一个 caller 只应注入一次。
        assert {context.symbol for context in callers} == {"handle", "other"}
        assert len(callers) == 2
        assert stats["graph_caller_hits"] == 2
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


def test_call_graph_failure_does_not_drop_import_contexts(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "api.py").write_text(
        "from pkg.auth import AuthService\n",
        encoding="utf-8",
    )

    class BrokenCallGraphIndex:
        def callers_of(self, *_args, **_kwargs):
            raise RuntimeError("call graph unavailable")

        def callees_of(self, *_args, **_kwargs):
            raise RuntimeError("call graph unavailable")

        def importers_of(self, *_args, **_kwargs):
            return [
                ImportEdge(
                    src_file="pkg/api.py",
                    dst_module="pkg.auth",
                    symbol="AuthService",
                    line=1,
                )
            ]

        def definitions_in_modules(self, *_args, **_kwargs):
            return []

    stats: dict[str, float] = {}
    contexts = GraphRetriever().retrieve(
        root,
        BrokenCallGraphIndex(),  # type: ignore[arg-type]
        RetrievalQuery(symbols=["login"], modules=["pkg.auth"]),
        stats=stats,
    )

    assert any(context.kind == "import" for context in contexts)
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
