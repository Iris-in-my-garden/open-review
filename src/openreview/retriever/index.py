"""基于 SQLite 的仓库符号与关系索引。

设计目标不是“把仓库切成固定 chunk 做向量检索”，而是给 code review 提供
确定性、可增量、可解释的检索底座：

- 符号定义：name / qualname / kind / file / start_line / end_line
- 符号查找：精确名字匹配 + LIKE 兜底（不使用 FTS5）
- 跨文件关系：imports / calls（best-effort，只作为 candidate）
- 增量更新：按 git blob SHA 判断文件是否变化，只重排变更文件

索引默认落在平台缓存目录，不写进仓库；同一个仓库的多次 review 复用同一个
SQLite 文件，按 head SHA 做增量更新。
"""

from __future__ import annotations

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

from filelock import FileLock, Timeout

from openreview.domain.versions import RETRIEVER_VERSION as RETRIEVER_VERSION
from openreview.repo.manager import default_cache_dir
from openreview.retriever import ast_utils
from openreview.retriever.pathing import is_source_file

logger = logging.getLogger(__name__)

INDEX_SCHEMA_VERSION = "6"
MAX_INDEXED_FILE_SIZE = 500 * 1024
MAX_INDEXED_FILES = 20000
SUPPORTED_SUFFIXES = {".py", ".go"}
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Index files are keyed by (shared repo identity, HEAD), so per-run worktrees
# converge on the same SQLite file. Keep that cache bounded instead of letting
# every review leave another multi-megabyte database behind.
INDEX_CACHE_MAX_BYTES = 2 * 1024**3
INDEX_CACHE_MAX_AGE_SECONDS = 30 * 24 * 3600
# Baseline snapshot: copy the latest fully-built index, then let ``ensure()``
# apply only ``seed_head..HEAD``. Fall back to a full build when the seed is
# missing, stale, or the diff is too large to be cheaper than re-indexing.
INDEX_SEED_MAX_RATIO = 0.35
_SEED_PREFIX = "seed-"


def _seed_enabled() -> bool:
    value = os.getenv("OPENREVIEW_INDEX_SEED")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _seed_max_ratio() -> float:
    value = os.getenv("OPENREVIEW_INDEX_SEED_MAX_RATIO")
    if not value:
        return INDEX_SEED_MAX_RATIO
    try:
        ratio = float(value)
    except ValueError:
        return INDEX_SEED_MAX_RATIO
    return ratio if ratio > 0 else INDEX_SEED_MAX_RATIO


def _bump_mtime(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass



def default_index_cache_dir() -> Path:
    env_dir = os.getenv("OPENREVIEW_INDEX_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (default_cache_dir().parent / "index").resolve()


def _git_identity(repo_path: Path) -> tuple[str, str]:
    """Return ``(shared git dir, HEAD)`` for a checkout or worktree.

    Linked worktrees have different ``--git-dir`` paths but share
    ``--git-common-dir``. Keying by the common dir plus HEAD lets a PR
    worktree and a local worktree at the same commit reuse one index, while
    still keeping different commits (and concurrent reviews) isolated.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "rev-parse",
                "--git-common-dir",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return str(repo_path), ""
    if result.returncode != 0:
        return str(repo_path), ""

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return str(repo_path), ""

    common = Path(lines[0]).expanduser()
    if not common.is_absolute():
        common = (repo_path / common).resolve()
    return str(common.resolve()), lines[1]


def index_cache_key(repo_path: str | Path) -> str:
    """Stable cache key for a repository revision.

    Falls back to the absolute worktree path for non-git directories, which
    matches the historical behavior for fixtures and ad-hoc trees.
    """
    resolved = Path(repo_path).expanduser().resolve()
    common, head = _git_identity(resolved)
    raw = f"{common}\0{head}" if head else str(resolved)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def repo_cache_key(repo_path: str | Path) -> str:
    """Stable key for a repository across revisions (used for seed snapshots)."""
    resolved = Path(repo_path).expanduser().resolve()
    common, _head = _git_identity(resolved)
    raw = common or str(resolved)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _seed_path(cache_root: Path, repo_path: Path) -> Path:
    return cache_root / f"{_SEED_PREFIX}{repo_cache_key(repo_path)}.sqlite3"


def _seed_lock_path(seed_path: Path) -> Path:
    return seed_path.with_suffix(seed_path.suffix + ".lock")



def _index_db_sidecars(db_path: Path) -> list[Path]:
    return [
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]


def _remove_index_db(db_path: Path) -> bool:
    """Delete one cached index and its WAL sidecars.

    The ``.lock`` file is acquired non-blocking first so an index that is
    currently being built by another process is never deleted underneath it.
    Returns ``True`` only if at least one file was removed.
    """
    lock = FileLock(str(db_path.with_suffix(db_path.suffix + ".lock")))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        logger.debug("Skipping active index cache %s", db_path)
        return False

    removed = False
    try:
        for path in _index_db_sidecars(db_path):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.debug("Could not remove index cache file %s: %s", path, exc)
    finally:
        lock.release()
    return removed


def prune_index_cache(
    cache_dir: str | Path,
    *,
    exclude: Iterable[str | Path] = (),
    max_bytes: int = INDEX_CACHE_MAX_BYTES,
    max_age_seconds: float = INDEX_CACHE_MAX_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Evict cached index databases that are too old or exceed the size cap.

    Only ``*.sqlite3`` files (plus their WAL sidecars) are considered, so
    unrelated files in the cache directory are left untouched. ``exclude``
    should contain the index currently in use by the caller.

    Returns the number of databases removed.
    """
    cache_root = Path(cache_dir).expanduser().resolve()
    if not cache_root.is_dir():
        return 0

    excluded = {Path(path).expanduser().resolve() for path in exclude}
    current_time = time.time() if now is None else now

    try:
        db_paths = list(cache_root.glob("*.sqlite3"))
    except OSError:
        return 0

    candidates: list[tuple[float, int, Path]] = []
    total = 0
    for db_path in db_paths:
        size = 0
        try:
            for path in _index_db_sidecars(db_path):
                if path.exists():
                    size += path.stat().st_size
            mtime = db_path.stat().st_mtime
        except OSError:
            # Another concurrent prune may have removed it already.
            continue
        # Seed snapshots are pinned against size-based eviction: they are the
        # baseline that makes cross-PR incremental indexing cheap. Age-based GC
        # still applies, and ``_try_seed``/``_refresh_seed`` bump their mtime.
        is_seed = db_path.name.startswith(_SEED_PREFIX)
        if not is_seed:
            total += size
        if db_path.resolve() in excluded:
            continue
        candidates.append((mtime, size, db_path))

    removed = 0

    if max_age_seconds > 0:
        for mtime, size, db_path in sorted(candidates, key=lambda item: item[0]):
            if current_time - mtime <= max_age_seconds:
                continue
            if _remove_index_db(db_path):
                removed += 1
                if not db_path.name.startswith(_SEED_PREFIX):
                    total -= size
                candidates = [item for item in candidates if item[2] != db_path]

    if max_bytes <= 0 or total <= max_bytes:
        return removed

    for _, size, db_path in sorted(candidates, key=lambda item: item[0]):
        if total <= max_bytes:
            break
        if db_path.name.startswith(_SEED_PREFIX):
            continue
        if _remove_index_db(db_path):
            removed += 1
            total -= size

    return removed


def git_blob_sha(data: bytes) -> str:
    """计算与 git 一致的 blob SHA，用于增量判断。"""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def module_name(rel_path: str) -> str:
    """把仓库相对路径转成可检索的模块名。"""
    path = Path(rel_path)
    if path.suffix == ".py":
        parts = list(path.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    if path.suffix == ".go":
        # Go 的“模块”是目录（package）；仓库根目录用 "." 表示。
        return "/".join(path.parent.parts) or "."
    return "/".join(path.parent.parts)


def _parse_go_module(gomod: Path) -> Optional[str]:
    try:
        text = gomod.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*module\s+([^\s#]+)", text)
    return match.group(1).strip().strip('"') if match else None


def _go_module_for(
    repo_path: Path, rel_path: str, cache: Dict[str, Optional[str]]
) -> Optional[str]:
    """Nearest ``go.mod`` module path for a Go file (monorepo friendly)."""
    repo_path = Path(repo_path).resolve()
    current = (repo_path / rel_path).parent
    checked: List[str] = []
    while True:
        key = str(current)
        if key in cache:
            module = cache[key]
            break
        checked.append(key)
        gomod = current / "go.mod"
        if gomod.is_file():
            module = _parse_go_module(gomod)
            break
        if current == repo_path or current.parent == current:
            module = None
            break
        current = current.parent
    for item in checked:
        cache[item] = module
    return module


def _normalize_go_import(dst_module: str, go_module: Optional[str]) -> str:
    """Map ``module/sub/pkg`` to the repo-relative package directory."""
    if not go_module:
        return dst_module
    if dst_module == go_module:
        return "."
    prefix = go_module + "/"
    if dst_module.startswith(prefix):
        return dst_module[len(prefix) :]
    return dst_module


@dataclass(frozen=True)
class IndexedSymbol:
    name: str
    qualname: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str = ""
    doc: str = ""
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
    callee_qualifier: str = ""


@dataclass(frozen=True)
class _SeedInfo:
    head_sha: str
    schema_version: str
    file_count: int
    indexed_at: float


@dataclass(frozen=True)
class _SeedResult:
    head_sha: str
    changed_files: int


def _read_seed_info(db_path: Path) -> Optional[_SeedInfo]:
    """Read seed metadata without modifying the snapshot.

    The caller holds the seed lock, so a normal read-write connection is safe
    even for WAL databases (read-only opens can fail when ``-shm`` is absent).
    """
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        meta = {
            str(row["key"]): str(row["value"])
            for row in conn.execute("SELECT key, value FROM meta").fetchall()
        }
        file_row = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()
        file_count = int(file_row["n"]) if file_row is not None else 0
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    head = meta.get("head_sha")
    if not head or head == "unknown":
        return None
    try:
        indexed_at = float(meta.get("indexed_at", "0") or 0)
    except ValueError:
        indexed_at = 0.0
    return _SeedInfo(
        head_sha=head,
        schema_version=meta.get("schema_version", ""),
        file_count=file_count,
        indexed_at=indexed_at,
    )


def _copy_sqlite(src_path: Path, dst: sqlite3.Connection) -> bool:
    """Copy one SQLite database into an open connection via the backup API."""
    try:
        src = sqlite3.connect(str(src_path), timeout=30)
    except sqlite3.Error as exc:
        logger.debug("Could not open seed index %s: %s", src_path, exc)
        return False
    try:
        src.backup(dst)
        dst.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("Could not copy seed index %s: %s", src_path, exc)
        return False
    finally:
        src.close()


class RepoIndex:
    """一个仓库一个 SQLite 索引；连接生命周期由调用方管理。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self.last_stats: Dict[str, Any] = {}
        self._go_module_cache: Dict[str, Optional[str]] = {}
        self._init_schema()

    @classmethod
    def for_worktree(
        cls,
        repo_path: str | Path,
        cache_dir: str | Path | None = None,
        force: bool = False,
        *,
        cache_key: str | None = None,
    ) -> "RepoIndex":
        repo_path = Path(repo_path).expanduser().resolve()
        cache_root = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else default_index_cache_dir()
        )
        # Stable key: per-run worktrees of the same repository revision share
        # one SQLite file instead of rebuilding a fresh index every review.
        key = cache_key or index_cache_key(repo_path)
        db_path = cache_root / f"{key}.sqlite3"
        index = cls(db_path)
        try:
            head_sha = _git_head(repo_path) or "unknown"
            seed = index._try_seed(
                repo_path, cache_root, head_sha, force=force
            )
            index.ensure(repo_path, force=force)
            if seed is not None:
                index.last_stats.update(
                    {
                        "index_seeded": 1,
                        "index_seed_head": seed.head_sha,
                        "index_seed_diff_files": seed.changed_files,
                    }
                )
            if not int(index.last_stats.get("index_reused", 0)):
                index._refresh_seed(repo_path, cache_root)
        except BaseException:
            index.close()
            raise
        # Age-based GC should mean "unused", not "not rebuilt": a reused
        # index is only read, so bump its mtime explicitly.
        try:
            os.utime(db_path, None)
        except OSError:
            pass
        index._prune_cache(cache_root, db_path)
        return index

    @staticmethod
    def _prune_cache(cache_root: Path, active_db: Path) -> None:
        try:
            prune_index_cache(cache_root, exclude=(active_db,))
        except Exception:  # noqa: BLE001 - cache GC must never fail a review
            logger.debug("Index cache pruning failed", exc_info=True)

    # ------------------------------------------------------------------
    # baseline snapshot seed
    # ------------------------------------------------------------------
    def _try_seed(
        self,
        repo_path: Path,
        cache_root: Path,
        head_sha: str,
        *,
        force: bool,
    ) -> Optional[_SeedResult]:
        """Copy the latest seed DB, then let ``ensure`` apply only the diff."""
        if force or head_sha == "unknown" or not _seed_enabled():
            return None
        if self._meta("head_sha") is not None:
            # The target already has an index (possibly for this same HEAD).
            return None

        seed_path = _seed_path(cache_root, repo_path)
        if not seed_path.is_file():
            return None

        lock = FileLock(str(_seed_lock_path(seed_path)))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            logger.debug("Seed index is busy: %s", seed_path)
            return None

        try:
            info = _read_seed_info(seed_path)
            if info is None:
                return None
            if info.schema_version and info.schema_version != INDEX_SCHEMA_VERSION:
                return None
            if info.file_count <= 0:
                return None
            if info.head_sha == head_sha:
                changes: List[Tuple[str, str, str]] = []
            else:
                diff = _git_changes(repo_path, info.head_sha, head_sha)
                if diff is None:
                    # Seed commit missing (shallow/foreign clone) or git failed.
                    return None
                changes = diff

            changed = sum(
                1
                for status, old_path, new_path in changes
                if _is_indexable(old_path if status == "D" else new_path)
            )
            ratio = changed / max(info.file_count, 1)
            if ratio > _seed_max_ratio():
                logger.debug(
                    "Skip seed %s: diff ratio %.3f > %.3f",
                    seed_path,
                    ratio,
                    _seed_max_ratio(),
                )
                return None

            if not _copy_sqlite(seed_path, self._conn):
                # A failed backup can leave a partially written destination.
                self._recreate_connection()
                return None
        finally:
            lock.release()

        _bump_mtime(seed_path)
        logger.info(
            "Seeded index from %s (diff files=%d)", info.head_sha[:12], changed
        )
        return _SeedResult(head_sha=info.head_sha, changed_files=changed)

    def _refresh_seed(self, repo_path: Path, cache_root: Path) -> bool:
        """Publish this successfully indexed HEAD as the new baseline snapshot."""
        if not _seed_enabled():
            return False
        head_sha = _git_head(repo_path)
        if not head_sha:
            return False

        seed_path = _seed_path(cache_root, repo_path)
        lock = FileLock(str(_seed_lock_path(seed_path)))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return False

        try:
            existing = _read_seed_info(seed_path) if seed_path.is_file() else None
            if existing is not None and existing.head_sha == head_sha:
                return False
            try:
                active_ts = float(self._meta("indexed_at") or 0)
            except (TypeError, ValueError):
                active_ts = 0.0
            if existing is not None and existing.indexed_at > active_ts:
                # A concurrent build already published a newer baseline.
                return False

            tmp_path = seed_path.with_name(
                f"{seed_path.name}.tmp-{os.getpid()}-{time.time_ns()}"
            )
            try:
                dest = sqlite3.connect(str(tmp_path), timeout=30)
                try:
                    self._conn.backup(dest)
                    dest.commit()
                    # Publish a standalone snapshot: no WAL dependency, so a
                    # later copy/open never needs stale ``-wal``/``-shm`` files.
                    dest.execute("PRAGMA journal_mode=DELETE")
                finally:
                    dest.close()
                os.replace(tmp_path, seed_path)
                # Remove sidecars left by an older WAL-mode seed file.
                for sidecar in (
                    seed_path.with_name(seed_path.name + "-wal"),
                    seed_path.with_name(seed_path.name + "-shm"),
                ):
                    try:
                        sidecar.unlink()
                    except OSError:
                        pass
            except (sqlite3.Error, OSError) as exc:
                logger.debug("Could not refresh seed index %s: %s", seed_path, exc)
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                return False
        finally:
            lock.release()

        _bump_mtime(seed_path)
        logger.info("Refreshed seed index at %s", head_sha[:12])
        return True

    def _recreate_connection(self) -> None:
        """Recreate an empty database after a failed seed copy."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        for path in _index_db_sidecars(self.db_path):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

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
                callee_qualifier TEXT NOT NULL DEFAULT '',
                line INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_file);
            CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name);
            """
        )

        # v1 -> v2：旧索引的 calls 表没有 qualifier，用于过滤标准库/第三方
        # 调用。schema version 会触发重建，但这里仍做一次兼容迁移，避免
        # 用户在一个已存在的缓存库上直接崩溃。
        call_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(calls)").fetchall()
        }
        if "callee_qualifier" not in call_columns:
            self._conn.execute(
                "ALTER TABLE calls ADD COLUMN callee_qualifier TEXT NOT NULL DEFAULT ''"
            )

        # 同理，v1 的 symbols 表没有 doc；schema bump 会触发重建，但重建只
        # 重新 INSERT，不会自动加列。
        symbol_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(symbols)").fetchall()
        }
        if "doc" not in symbol_columns:
            self._conn.execute(
                "ALTER TABLE symbols ADD COLUMN doc TEXT NOT NULL DEFAULT ''"
            )

        # 旧版本曾用 FTS5 做符号检索。清理遗留的触发器和虚拟表，避免缓存库
        # 继续维护一份无人读取的索引；新库不产生任何 FTS 写入。
        legacy_fts = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'symbols_fts'"
        ).fetchone()
        if legacy_fts is not None:
            self._conn.executescript(
                """
                DROP TRIGGER IF EXISTS symbols_ai;
                DROP TRIGGER IF EXISTS symbols_ad;
                DROP TRIGGER IF EXISTS symbols_au;
                DROP TABLE IF EXISTS symbols_fts;
                """
            )

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
                # A schema bump changes how rows are written.  `_index_if_changed`
                # skips unchanged blobs, so without a full reset old files would
                # keep stale columns/values after the version bump.
                self._reset_index()
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
        symbols: List[Tuple[str, str, str, int, int, str, str]] = []
        imports: List[Tuple[str, str, int]] = []
        calls: List[Tuple[str, str, str, int]] = []

        if parsed is not None:
            _, source, tree = parsed
            lines = text.splitlines()
            for name, qualname, kind, start_line, end_line, doc in ast_utils.iter_definitions(
                tree, source
            ):
                signature = ""
                if 0 < start_line <= len(lines):
                    signature = lines[start_line - 1].strip()[:240]
                symbols.append(
                    (name, qualname, kind, start_line, end_line, signature, doc)
                )

            if suffix == ".py":
                imports, calls = _python_imports_and_calls(tree, source)
            elif suffix == ".go":
                imports, calls = _go_imports_and_calls(tree, source)
                go_module = _go_module_for(
                    repo_path, rel_path, self._go_module_cache
                )
                imports = [
                    (_normalize_go_import(module, go_module), alias, line)
                    for module, alias, line in imports
                ]

        self._conn.executemany(
            """
            INSERT INTO symbols(name, qualname, kind, file_path, start_line, end_line, signature, doc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (name, qualname, kind, rel_path, start_line, end_line, signature, doc)
                for name, qualname, kind, start_line, end_line, signature, doc in symbols
            ],
        )
        self._conn.executemany(
            "INSERT INTO imports(src_file, dst_module, symbol, line) VALUES (?, ?, ?, ?)",
            [(rel_path, module, symbol, line) for module, symbol, line in imports],
        )
        # calls 里 caller_symbol 在 _python/_go 提取时已经算好。
        self._conn.executemany(
            """
            INSERT INTO calls(caller_file, caller_symbol, callee_name, callee_qualifier, line)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (rel_path, caller, callee, qualifier, line)
                for caller, callee, qualifier, line in calls
            ],
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

    def _reset_index(self) -> None:
        """Drop all indexed rows after an incompatible schema change."""
        for table in ("symbols", "imports", "calls", "files"):
            self._conn.execute(f"DELETE FROM {table}")
        self._conn.execute(
            "DELETE FROM meta WHERE key IN ('head_sha', 'indexed_at')"
        )
        self._conn.commit()

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

        # 2) LIKE 兜底：覆盖 qualname 与部分前缀匹配。
        if len(results) < limit:
            remaining = limit - len(results)
            like = f"%{_escape_like(cleaned[0])}%"
            rows = self._conn.execute(
                "SELECT * FROM symbols WHERE name LIKE ? ESCAPE '\\' "
                "OR qualname LIKE ? ESCAPE '\\' "
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
                    "WHERE dst_module = ? OR dst_module LIKE ? ESCAPE '\\' "
                    "LIMIT ?",
                    (module, f"%.{_escape_like(module)}", max(1, limit)),
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
                    WHERE f.module = ? OR f.module LIKE ? ESCAPE '\\'
                    LIMIT ?
                    """,
                    (module, f"%.{_escape_like(module)}", max(1, limit)),
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
            f"SELECT caller_file, caller_symbol, callee_name, callee_qualifier, line "
            f"FROM calls WHERE callee_name IN ({placeholders}) LIMIT ?",
            (*cleaned, max(1, limit)),
        ).fetchall()
        return [
            CallEdge(
                caller_file=row["caller_file"],
                caller_symbol=row["caller_symbol"],
                callee_name=row["callee_name"],
                line=int(row["line"]),
                callee_qualifier=row["callee_qualifier"] or "",
            )
            for row in rows
        ]

    def callees_of(self, callers: Sequence[str], limit: int = 20) -> List[CallEdge]:
        """返回 ``caller_symbol`` 调用了谁。

        调用图是 best-effort：``caller_symbol`` 来自定义嵌套栈，顶层函数可能
        是空字符串；命中不到时调用方应回退到同文件调用点，而不是报错。
        """
        cleaned = [name for name in callers if name]
        if not cleaned:
            return []
        clauses: List[str] = []
        params: List[str] = []
        for name in cleaned:
            clauses.append("caller_symbol = ?")
            params.append(name)
            # 方法调用：diff 里通常只有 ``login``，索引里是 ``AuthService.login``。
            # 必须转义 LIKE 通配符：Python 符号里的 ``_`` 不是“任意单字符”。
            clauses.append("caller_symbol LIKE ? ESCAPE '\\'")
            params.append(f"%.{_escape_like(name)}")
        rows = self._conn.execute(
            "SELECT caller_file, caller_symbol, callee_name, callee_qualifier, line "
            "FROM calls "
            f"WHERE {' OR '.join(clauses)} LIMIT ?",
            (*params, max(1, limit)),
        ).fetchall()
        return [
            CallEdge(
                caller_file=row["caller_file"],
                caller_symbol=row["caller_symbol"],
                callee_name=row["callee_name"],
                line=int(row["line"]),
                callee_qualifier=row["callee_qualifier"] or "",
            )
            for row in rows
        ]

    def enclosing_symbol(
        self, file_path: str, line: int
    ) -> Optional[IndexedSymbol]:
        """返回覆盖指定行的最小符号定义，用于把调用点扩展成完整函数。"""
        if not file_path or line <= 0:
            return None
        row = self._conn.execute(
            """
            SELECT * FROM symbols
            WHERE file_path = ? AND start_line <= ? AND end_line >= ?
            ORDER BY (end_line - start_line) ASC
            LIMIT 1
            """,
            (file_path, line, line),
        ).fetchone()
        return _row_to_symbol(row, score=1.0) if row is not None else None


def _escape_like(value: str) -> str:
    """转义 SQL LIKE 的通配符，避免符号名里的 ``_`` / ``%`` 误匹配。"""
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _row_to_symbol(row: sqlite3.Row, score: float) -> IndexedSymbol:
    return IndexedSymbol(
        name=row["name"],
        qualname=row["qualname"],
        kind=row["kind"],
        file_path=row["file_path"],
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        signature=row["signature"],
        doc=row["doc"] if "doc" in row.keys() else "",
        score=score,
    )


_PYTHON_DEFINITION_TYPES = {"function_definition", "class_definition"}


def _python_imports_and_calls(
    tree: Any, source: bytes
) -> Tuple[List[Tuple[str, str, int]], List[Tuple[str, str, str, int]]]:
    """Extract imports/calls from the tree-sitter tree built for symbols.

    The index already parses every Python file with tree-sitter. Parsing the
    same text again with :mod:`ast` doubled indexing CPU, so both views are
    derived from the same tree here.
    """
    imports: List[Tuple[str, str, int]] = []
    calls: List[Tuple[str, str, str, int]] = []
    definitions: List[str] = []
    stack: List[Tuple[Any, bool]] = [(tree.root_node, False)]

    while stack:
        node, exiting = stack.pop()
        if exiting:
            definitions.pop()
            continue

        node_type = node.type
        if node_type in _PYTHON_DEFINITION_TYPES:
            name_node = node.child_by_field_name("name")
            definitions.append(
                ast_utils.node_text(name_node, source) if name_node is not None else ""
            )
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.children))
            continue

        if node_type == "import_statement":
            imports.extend(_python_import_statement(node, source))
        elif node_type == "import_from_statement":
            imports.extend(_python_import_from_statement(node, source))
        elif node_type == "call":
            raw = _tree_call_name(node.child_by_field_name("function"), source)
            if raw:
                parts = raw.split(".")
                calls.append(
                    (
                        ".".join(definitions),
                        parts[-1],
                        ".".join(parts[:-1]),
                        node.start_point[0] + 1,
                    )
                )

        stack.extend((child, False) for child in reversed(node.children))

    return imports, calls


def _python_import_statement(
    node: Any, source: bytes
) -> List[Tuple[str, str, int]]:
    result: List[Tuple[str, str, int]] = []
    line = node.start_point[0] + 1
    for child in node.children:
        if child.type == "dotted_name":
            module = ast_utils.node_text(child, source)
            if module:
                result.append((module, "", line))
        elif child.type == "aliased_import":
            name_node = child.child_by_field_name("name")
            alias_node = child.child_by_field_name("alias")
            module = (
                ast_utils.node_text(name_node, source) if name_node is not None else ""
            )
            alias = (
                ast_utils.node_text(alias_node, source) if alias_node is not None else ""
            )
            if module:
                result.append((module, alias, line))
    return result


def _python_import_from_statement(
    node: Any, source: bytes
) -> List[Tuple[str, str, int]]:
    result: List[Tuple[str, str, int]] = []
    line = node.start_point[0] + 1
    module_node = node.child_by_field_name("module_name")
    # stdlib ``ast`` strips the leading dots and exposes the remaining module
    # (``from .pkg import x`` -> module ``pkg``); match that here.
    module = (
        ast_utils.node_text(module_node, source).lstrip(".")
        if module_node is not None
        else ""
    )
    for child in node.children:
        if child is module_node:
            continue
        if child.type == "dotted_name":
            name = ast_utils.node_text(child, source)
            if name:
                result.append((module, name, line))
        elif child.type == "aliased_import":
            name_node = child.child_by_field_name("name")
            name = (
                ast_utils.node_text(name_node, source) if name_node is not None else ""
            )
            if name:
                result.append((module, name, line))
    return result


def _tree_call_name(node: Any, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "identifier":
        return ast_utils.node_text(node, source)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        left = _tree_call_name(obj, source)
        right = ast_utils.node_text(attr, source) if attr is not None else ""
        if left and right:
            return f"{left}.{right}"
        return right or left
    return ast_utils.node_text(node, source).strip()


_GO_DEFINITION_TYPES = {
    "function_declaration",
    "method_declaration",
    "type_declaration",
}


def _go_imports_and_calls(
    tree: Any, source: bytes
) -> Tuple[List[Tuple[str, str, int]], List[Tuple[str, str, str, int]]]:
    imports: List[Tuple[str, str, int]] = []
    calls: List[Tuple[str, str, str, int]] = []
    definitions: List[str] = []
    # Track the enclosing definition while walking once. The previous version
    # called ``find_enclosing_definition`` (a full tree traversal) for every
    # call expression, which was quadratic: a 457 KB generated Go test file
    # with ~8k calls took ~9s to index.
    stack: List[Tuple[Any, bool]] = [(tree.root_node, False)]
    while stack:
        node, exiting = stack.pop()
        if exiting:
            definitions.pop()
            continue

        node_type = node.type
        if node_type in _GO_DEFINITION_TYPES:
            # Include the receiver type for methods: `Server.Do` instead of `Do`.
            name = ast_utils.definition_qualname(node, source)
            if not name:
                name = ast_utils.definition_name(node, source) or ""
            definitions.append(name)
            stack.append((node, True))
            stack.extend(
                (child, False) for child in reversed(node.children)
            )
            continue

        if node_type == "import_spec":
            path_node = node.child_by_field_name("path")
            if path_node is not None:
                raw = ast_utils.node_text(path_node, source).strip().strip('"')
                if raw:
                    alias_node = node.child_by_field_name("name")
                    alias = (
                        ast_utils.node_text(alias_node, source).strip()
                        if alias_node is not None
                        else ""
                    )
                    imports.append((raw, alias, node.start_point[0] + 1))
        elif node_type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                raw = ast_utils.node_text(fn, source).strip()
                # Generic instantiation: `pkg.Func[T]()` -> `pkg.Func`.
                raw = re.sub(r"\[[^\]]*\]$", "", raw).strip()
                if raw:
                    caller = ".".join(name for name in definitions if name)
                    calls.append(
                        (
                            caller,
                            raw.split(".")[-1],
                            ".".join(raw.split(".")[:-1]),
                            node.start_point[0] + 1,
                        )
                    )
        stack.extend((child, False) for child in reversed(node.children))
    return imports, calls


def _git_head(repo_path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    """优先用 git ls-files，非 git 目录降级为 rglob。

    ``MAX_INDEXED_FILES`` 只统计真正可索引的源码文件。之前按
    ``git ls-files`` 的原始顺序计数，会把文档、图片、生成物也算进上限，
    大型 monorepo 可能在扫到 Python/Go 文件之前就提前停止。
    """
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
        yield from _yield_indexable(raw_paths)
        return

    rel_paths = (
        path.relative_to(repo_path).as_posix()
        for path in sorted(repo_path.rglob("*"))
        if path.is_file()
    )
    yield from _yield_indexable(rel_paths)


def _yield_indexable(rel_paths: Iterable[str]) -> Iterable[str]:
    """Yield at most ``MAX_INDEXED_FILES`` indexable paths, counting only those."""
    indexed = 0
    for rel_path in rel_paths:
        if not _is_indexable(rel_path):
            continue
        if indexed >= MAX_INDEXED_FILES:
            logger.warning(
                "Repo has more than %d indexable files; truncating index.",
                MAX_INDEXED_FILES,
            )
            return
        indexed += 1
        yield rel_path


def _is_indexable(rel_path: str) -> bool:
    return is_source_file(rel_path, SUPPORTED_SUFFIXES)
