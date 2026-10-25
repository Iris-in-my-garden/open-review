"""基于 SQLite FTS5 的仓库符号索引。

设计目标不是“把仓库切成固定 chunk 做向量检索”，而是给 code review 提供
确定性、可增量、可解释的检索底座：

- 符号定义：name / qualname / kind / file / start_line / end_line
- 符号检索：SQLite FTS5（BM25）+ 精确名字匹配
- 跨文件关系：imports / calls（best-effort，只作为 candidate）
- 增量更新：按 git blob SHA 判断文件是否变化，只重排变更文件

索引默认落在平台缓存目录，不写进仓库；同一个仓库的多次 review 复用同一个
SQLite 文件，按 head SHA 做增量更新。
"""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from filelock import FileLock

from openreview.repo.manager import default_cache_dir
from openreview.retriever import ast_utils

logger = logging.getLogger(__name__)

INDEX_SCHEMA_VERSION = "1"
RETRIEVER_VERSION = "hybrid-v1"
MAX_INDEXED_FILE_SIZE = 500 * 1024
MAX_INDEXED_FILES = 20000
EXCLUDED_DIRS = {
    ".git",
    ".idea",
    ".tox",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "vendor",
    "site-packages",
}
SUPPORTED_SUFFIXES = {".py", ".go"}
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def default_index_cache_dir() -> Path:
    env_dir = os.getenv("OPENREVIEW_INDEX_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (default_cache_dir().parent / "index").resolve()


def git_blob_sha(data: bytes) -> str:
    """计算与 git 一致的 blob SHA，用于增量判断。"""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def module_name(rel_path: str) -> str:
    """把仓库相对路径转成可检索的模块名（Python 为主）。"""
    path = Path(rel_path)
    if path.suffix == ".py":
        parts = list(path.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    return "/".join(path.parent.parts)


@dataclass(frozen=True)
class IndexedSymbol:
    name: str
    qualname: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class ImportEdge:
    src_file: str
    dst_module: str
    symbol: str
    line: int


@dataclass(frozen=True)
class CallEdge:
    caller_file: str
    caller_symbol: str
    callee_name: str
    line: int


class RepoIndex:
    """一个仓库一个 SQLite 索引；连接生命周期由调用方管理。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self.fts_enabled = True
        self.last_stats: Dict[str, Any] = {}
        self._init_schema()

    @classmethod
    def for_worktree(
        cls,
        repo_path: str | Path,
        cache_dir: str | Path | None = None,
        force: bool = False,
    ) -> "RepoIndex":
        repo_path = Path(repo_path).expanduser().resolve()
        cache_root = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else default_index_cache_dir()
        )
        key = hashlib.sha1(str(repo_path).encode("utf-8")).hexdigest()[:12]
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_path.name or "repo")
        index = cls(cache_root / f"{safe_name}-{key}.sqlite3")
        index.ensure(repo_path, force=force)
        return index

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "RepoIndex":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                language TEXT NOT NULL,
                blob_sha TEXT NOT NULL,
                size INTEGER NOT NULL,
                module TEXT NOT NULL DEFAULT '',
                indexed_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                qualname TEXT NOT NULL,
                kind TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                signature TEXT NOT NULL DEFAULT '',
                doc TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src_file TEXT NOT NULL,
                dst_module TEXT NOT NULL,
                symbol TEXT NOT NULL DEFAULT '',
                line INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_imports_src ON imports(src_file);
            CREATE INDEX IF NOT EXISTS idx_imports_dst ON imports(dst_module);

            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_file TEXT NOT NULL,
                caller_symbol TEXT NOT NULL DEFAULT '',
                callee_name TEXT NOT NULL,
                line INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_file);
            CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name);
            """
        )

        try:
            self._conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
                    name, qualname, signature, doc,
                    content='symbols',
                    content_rowid='id',
                    tokenize='unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
                    INSERT INTO symbols_fts(rowid, name, qualname, signature, doc)
                    VALUES (new.id, new.name, new.qualname, new.signature, new.doc);
                END;

                CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualname, signature, doc)
                    VALUES ('delete', old.id, old.name, old.qualname, old.signature, old.doc);
                END;

                CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualname, signature, doc)
                    VALUES ('delete', old.id, old.name, old.qualname, old.signature, old.doc);
                    INSERT INTO symbols_fts(rowid, name, qualname, signature, doc)
                    VALUES (new.id, new.name, new.qualname, new.signature, new.doc);
                END;
                """
            )
        except sqlite3.OperationalError as exc:
            # 某些 Python 构建没有 FTS5；降级为 LIKE 搜索，不阻断 review。
            logger.warning("SQLite FTS5 unavailable, falling back to LIKE search: %s", exc)
            self.fts_enabled = False

    # ------------------------------------------------------------------
    # build / update
    # ------------------------------------------------------------------
    def ensure(self, repo_path: Path, force: bool = False) -> Dict[str, Any]:
        """确保索引覆盖当前 worktree 的 HEAD；返回本次构建统计。"""
        started = time.monotonic()
        repo_path = Path(repo_path).resolve()
        head_sha = _git_head(repo_path) or "unknown"
        lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")

        with FileLock(str(lock_path), timeout=120):
            old_head = self._meta("head_sha")
            schema_version = self._meta("schema_version")
            if schema_version and schema_version != INDEX_SCHEMA_VERSION:
                force = True
            if not force and old_head == head_sha:
                self.last_stats = {
                    "index_head": head_sha,
                    "index_reused": 1,
                    "index_build_seconds": round(time.monotonic() - started, 3),
                }
                return self.last_stats

            indexed = 0
            skipped = 0
            if old_head and old_head != "unknown" and head_sha != "unknown" and not force:
                changes = _git_changes(repo_path, old_head, head_sha)
            else:
                changes = None

            if changes is None:
                # 首次构建、或者 diff 不可用：全量扫描并清理 stale 文件。
                files = list(_iter_source_files(repo_path))
                tracked = set(files)
                for row in self._conn.execute("SELECT path FROM files").fetchall():
                    if row["path"] not in tracked:
                        self._delete_file(row["path"])
                for rel_path in files:
                    result = self._index_if_changed(repo_path, rel_path)
                    if result:
                        indexed += 1
                    else:
                        skipped += 1
            else:
                for status, old_path, new_path in changes:
                    if status == "D":
                        self._delete_file(old_path)
                        continue
                    if status == "R" and old_path != new_path:
                        self._delete_file(old_path)
                    result = self._index_if_changed(repo_path, new_path)
                    if result:
                        indexed += 1
                    else:
                        skipped += 1

            self._set_meta("head_sha", head_sha)
            self._set_meta("schema_version", INDEX_SCHEMA_VERSION)
            self._set_meta("indexed_at", str(time.time()))
            self._conn.commit()

        self.last_stats = {
            "index_head": head_sha,
            "index_files_indexed": indexed,
            "index_files_skipped": skipped,
            "index_symbols": self.count("symbols"),
            "index_imports": self.count("imports"),
            "index_calls": self.count("calls"),
            "index_build_seconds": round(time.monotonic() - started, 3),
            "index_reused": 0,
        }
        return self.last_stats

    def _index_if_changed(self, repo_path: Path, rel_path: str) -> bool:
        candidate = (repo_path / rel_path).resolve()
        try:
            candidate.relative_to(repo_path)
        except ValueError:
            return False
        if not candidate.is_file() or candidate.suffix not in SUPPORTED_SUFFIXES:
            # 文件被删除 / 改成不支持的类型时，必须清掉旧符号。
            self._delete_file(rel_path)
            return False
        try:
            if candidate.stat().st_size > MAX_INDEXED_FILE_SIZE:
                self._delete_file(rel_path)
                return False
            data = candidate.read_bytes()
        except OSError:
            self._delete_file(rel_path)
            return False

        digest = git_blob_sha(data)
        row = self._conn.execute(
            "SELECT blob_sha FROM files WHERE path = ?", (rel_path,)
        ).fetchone()
        if row is not None and row["blob_sha"] == digest:
            return False

        text = data.decode("utf-8", errors="ignore")
        self._delete_file(rel_path)
        self._index_file(repo_path, rel_path, text, digest, len(data))
        return True

    def _index_file(
        self,
        repo_path: Path,
        rel_path: str,
        text: str,
        digest: str,
        size: int,
    ) -> None:
        suffix = Path(rel_path).suffix
        parsed = ast_utils.parse_text(text, suffix)
        symbols: List[Tuple[str, str, str, int, int, str]] = []
        imports: List[Tuple[str, str, int]] = []
        calls: List[Tuple[str, str, int]] = []

        if parsed is not None:
            _, source, tree = parsed
            lines = text.splitlines()
            for name, qualname, kind, start_line, end_line in ast_utils.iter_definitions(
                tree, source
            ):
                signature = ""
                if 0 < start_line <= len(lines):
                    signature = lines[start_line - 1].strip()[:240]
                symbols.append(
                    (name, qualname, kind, start_line, end_line, signature)
                )

            if suffix == ".py":
                imports, calls = _python_imports_and_calls(text)
            elif suffix == ".go":
                imports, calls = _go_imports_and_calls(tree, source)

        self._conn.executemany(
            """
            INSERT INTO symbols(name, qualname, kind, file_path, start_line, end_line, signature)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (name, qualname, kind, rel_path, start_line, end_line, signature)
                for name, qualname, kind, start_line, end_line, signature in symbols
            ],
        )
        self._conn.executemany(
            "INSERT INTO imports(src_file, dst_module, symbol, line) VALUES (?, ?, ?, ?)",
            [(rel_path, module, symbol, line) for module, symbol, line in imports],
        )
        # calls 里 caller_symbol 在 _python/_go 提取时已经算好。
        self._conn.executemany(
            "INSERT INTO calls(caller_file, caller_symbol, callee_name, line) VALUES (?, ?, ?, ?)",
            [(rel_path, caller, callee, line) for caller, callee, line in calls],
        )
        self._conn.execute(
            """
            INSERT INTO files(path, language, blob_sha, size, module, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                language = excluded.language,
                blob_sha = excluded.blob_sha,
                size = excluded.size,
                module = excluded.module,
                indexed_at = excluded.indexed_at
            """,
            (
                rel_path,
                suffix.lstrip("."),
                digest,
                size,
                module_name(rel_path),
                time.time(),
            ),
        )

    def _delete_file(self, rel_path: str) -> None:
        self._conn.execute("DELETE FROM symbols WHERE file_path = ?", (rel_path,))
        self._conn.execute("DELETE FROM imports WHERE src_file = ?", (rel_path,))
        self._conn.execute("DELETE FROM calls WHERE caller_file = ?", (rel_path,))
        self._conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))

    def _meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def count(self, table: str) -> int:
        if table not in {"symbols", "imports", "calls", "files"}:
            raise ValueError(f"Unsupported table: {table}")
        row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"]) if row is not None else 0

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def search_symbols(
        self, terms: Sequence[str], limit: int = 30
    ) -> List[IndexedSymbol]:
        cleaned = [term.strip() for term in terms if term and term.strip()]
        if not cleaned:
            return []

        # 1) 精确名字优先：对代码检索来说，`foo_bar` 比语义相似更可靠。
        placeholders = ",".join("?" for _ in cleaned)
        exact_rows = self._conn.execute(
            f"SELECT * FROM symbols WHERE name IN ({placeholders}) LIMIT ?",
            (*cleaned, max(1, limit)),
        ).fetchall()
        seen = {int(row["id"]) for row in exact_rows}
        results = [
            _row_to_symbol(row, score=1.0) for row in exact_rows
        ]

        # 2) FTS5/BM25 兜底：覆盖 qualname、signature 和前缀匹配。
        if len(results) < limit:
            remaining = limit - len(results)
            if self.fts_enabled:
                query = _fts_query(cleaned)
                if query:
                    try:
                        rows = self._conn.execute(
                            """
                            SELECT s.*, bm25(symbols_fts) AS rank
                            FROM symbols_fts
                            JOIN symbols s ON s.id = symbols_fts.rowid
                            WHERE symbols_fts MATCH ?
                            ORDER BY rank
                            LIMIT ?
                            """,
                            (query, remaining + len(seen)),
                        ).fetchall()
                        for row in rows:
                            if int(row["id"]) in seen:
                                continue
                            rank = float(row["rank"] or 0.0)
                            results.append(
                                _row_to_symbol(
                                    row, score=round(0.7 - min(abs(rank) / 100.0, 0.25), 3)
                                )
                            )
                            seen.add(int(row["id"]))
                            if len(results) >= limit:
                                break
                    except sqlite3.OperationalError as exc:
                        logger.debug("FTS query failed, falling back to LIKE: %s", exc)

            if len(results) < limit:
                like = f"%{cleaned[0]}%"
                rows = self._conn.execute(
                    "SELECT * FROM symbols WHERE name LIKE ? OR qualname LIKE ? "
                    "ORDER BY name LIMIT ?",
                    (like, like, remaining),
                ).fetchall()
                for row in rows:
                    if int(row["id"]) in seen:
                        continue
                    results.append(_row_to_symbol(row, score=0.5))
                    seen.add(int(row["id"]))
                    if len(results) >= limit:
                        break

        return results

    def importers_of(
        self, modules: Sequence[str], limit: int = 20
    ) -> List[ImportEdge]:
        cleaned = [module.strip() for module in modules if module and module.strip()]
        if not cleaned:
            return []
        rows: List[sqlite3.Row] = []
        for module in cleaned:
            rows.extend(
                self._conn.execute(
                    "SELECT src_file, dst_module, symbol, line FROM imports "
                    "WHERE dst_module = ? OR dst_module LIKE ? LIMIT ?",
                    (module, f"%.{module}", max(1, limit)),
                ).fetchall()
            )
        result: List[ImportEdge] = []
        seen: set[Tuple[str, str, int]] = set()
        for row in rows:
            key = (row["src_file"], row["dst_module"], int(row["line"]))
            if key in seen:
                continue
            seen.add(key)
            result.append(
                ImportEdge(
                    src_file=row["src_file"],
                    dst_module=row["dst_module"],
                    symbol=row["symbol"],
                    line=int(row["line"]),
                )
            )
            if len(result) >= limit:
                break
        return result

    def imports_of(self, files: Sequence[str]) -> List[ImportEdge]:
        cleaned = [file for file in files if file]
        if not cleaned:
            return []
        placeholders = ",".join("?" for _ in cleaned)
        rows = self._conn.execute(
            f"SELECT src_file, dst_module, symbol, line FROM imports "
            f"WHERE src_file IN ({placeholders})",
            tuple(cleaned),
        ).fetchall()
        return [
            ImportEdge(
                src_file=row["src_file"],
                dst_module=row["dst_module"],
                symbol=row["symbol"],
                line=int(row["line"]),
            )
            for row in rows
        ]

    def definitions_in_modules(
        self, modules: Sequence[str], limit: int = 20
    ) -> List[IndexedSymbol]:
        cleaned = [module for module in modules if module]
        if not cleaned:
            return []
        rows: List[sqlite3.Row] = []
        for module in cleaned:
            rows.extend(
                self._conn.execute(
                    """
                    SELECT s.* FROM symbols s
                    JOIN files f ON f.path = s.file_path
                    WHERE f.module = ? OR f.module LIKE ?
                    LIMIT ?
                    """,
                    (module, f"%.{module}", max(1, limit)),
                ).fetchall()
            )
        result: List[IndexedSymbol] = []
        seen: set[int] = set()
        for row in rows:
            if int(row["id"]) in seen:
                continue
            seen.add(int(row["id"]))
            result.append(_row_to_symbol(row, score=0.65))
            if len(result) >= limit:
                break
        return result

    def callers_of(self, names: Sequence[str], limit: int = 20) -> List[CallEdge]:
        cleaned = [name for name in names if name]
        if not cleaned:
            return []
        placeholders = ",".join("?" for _ in cleaned)
        rows = self._conn.execute(
            f"SELECT caller_file, caller_symbol, callee_name, line FROM calls "
            f"WHERE callee_name IN ({placeholders}) LIMIT ?",
            (*cleaned, max(1, limit)),
        ).fetchall()
        return [
            CallEdge(
                caller_file=row["caller_file"],
                caller_symbol=row["caller_symbol"],
                callee_name=row["callee_name"],
                line=int(row["line"]),
            )
            for row in rows
        ]


def _row_to_symbol(row: sqlite3.Row, score: float) -> IndexedSymbol:
    return IndexedSymbol(
        name=row["name"],
        qualname=row["qualname"],
        kind=row["kind"],
        file_path=row["file_path"],
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        signature=row["signature"],
        score=score,
    )


def _fts_query(terms: Sequence[str]) -> str:
    quoted = []
    for term in terms:
        safe = term.replace('"', '""')
        quoted.append(f'"{safe}"')
    return " OR ".join(quoted)


def _python_imports_and_calls(
    text: str,
) -> Tuple[List[Tuple[str, str, int]], List[Tuple[str, str, int]]]:
    imports: List[Tuple[str, str, int]] = []
    calls: List[Tuple[str, str, int]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return imports, calls

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, alias.asname or "", node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append((module, alias.name, node.lineno))

    class _CallVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: List[str] = []
            self.items: List[Tuple[str, str, int]] = []

        def _visit_def(self, node: ast.AST) -> None:
            name = getattr(node, "name", "")
            self.stack.append(name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_def(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_def(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_def(node)

        def visit_Call(self, node: ast.Call) -> None:
            callee = _call_name(node.func)
            if callee:
                self.items.append(
                    (".".join(self.stack), callee.split(".")[-1], node.lineno)
                )
            self.generic_visit(node)

    visitor = _CallVisitor()
    visitor.visit(tree)
    calls.extend(visitor.items)
    return imports, calls


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""


def _go_imports_and_calls(
    tree: Any, source: bytes
) -> Tuple[List[Tuple[str, str, int]], List[Tuple[str, str, int]]]:
    imports: List[Tuple[str, str, int]] = []
    calls: List[Tuple[str, str, int]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "import_spec":
            path_node = node.child_by_field_name("path")
            if path_node is not None:
                raw = ast_utils.node_text(path_node, source).strip().strip('"')
                if raw:
                    imports.append((raw, "", node.start_point[0] + 1))
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                raw = ast_utils.node_text(fn, source).strip()
                if raw:
                    enclosing = ast_utils.find_enclosing_definition(
                        tree, node.start_point[0]
                    )
                    caller = (
                        ast_utils.definition_qualname(enclosing, source)
                        if enclosing is not None
                        else ""
                    )
                    calls.append((caller, raw.split(".")[-1], node.start_point[0] + 1))
        stack.extend(node.children)
    return imports, calls


def _git_head(repo_path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_changes(
    repo_path: Path, old_head: str, new_head: str
) -> Optional[List[Tuple[str, str, str]]]:
    """返回 (status, old_path, new_path)；diff 不可用时返回 None。"""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "diff",
                "--name-status",
                "-z",
                f"{old_head}..{new_head}",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    parts = result.stdout.decode("utf-8", errors="ignore").split("\0")
    changes: List[Tuple[str, str, str]] = []
    index = 0
    while index < len(parts):
        status = parts[index].strip()
        index += 1
        if not status:
            continue
        code = status[0]
        if code in {"R", "C"}:
            if index + 1 >= len(parts):
                break
            old_path = parts[index]
            new_path = parts[index + 1]
            index += 2
            changes.append((code, old_path, new_path))
        else:
            if index >= len(parts):
                break
            path = parts[index]
            index += 1
            changes.append((code, path, path))
    return changes


def _iter_source_files(repo_path: Path) -> Iterable[str]:
    """优先用 git ls-files，非 git 目录降级为 rglob。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "ls-files", "-z"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            raw_paths = result.stdout.decode("utf-8", errors="ignore").split("\0")
        else:
            raw_paths = []
    except (OSError, subprocess.TimeoutExpired):
        raw_paths = []

    if raw_paths:
        for count, rel_path in enumerate(raw_paths):
            if count >= MAX_INDEXED_FILES:
                logger.warning(
                    "Repo has more than %d indexable files; truncating index.",
                    MAX_INDEXED_FILES,
                )
                return
            if _is_indexable(rel_path):
                yield rel_path
        return

    for count, path in enumerate(sorted(repo_path.rglob("*"))):
        if count >= MAX_INDEXED_FILES:
            logger.warning(
                "Repo has more than %d files; truncating index.", MAX_INDEXED_FILES
            )
            return
        if not path.is_file():
            continue
        rel_path = path.relative_to(repo_path).as_posix()
        if _is_indexable(rel_path):
            yield rel_path


def _is_indexable(rel_path: str) -> bool:
    path = Path(rel_path)
    if path.suffix not in SUPPORTED_SUFFIXES:
        return False
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    return True
