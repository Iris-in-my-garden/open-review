"""本地仓库模式：不依赖 GitHub API，直接用 base/head SHA 做一次 review。

评测（AACR-Bench 等）和离线回放拿到的通常是“仓库 + base_commit + head_commit”，
而不是一个可访问的 GitHub PR。这里负责两件事：

1. 用 detached worktree 准备 head / base 两份工作区，避免污染调用方的仓库；
2. 用 ``git diff`` 生成与 GitHub ChangedFile 等价的文件列表，复用现有 DiffParser。

刻意使用 ``--no-renames``：rename 会被拆成 delete + add，语义上仍然正确，
但解析逻辑更简单，也不会因为路径引用问题漏掉文件。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from openreview.domain.models import ChangedFile
from openreview.repo.manager import RepoCommandError

logger = logging.getLogger(__name__)


def filter_changes_by_suffixes(
    files: Sequence[ChangedFile],
    suffixes: Optional[Sequence[str]],
) -> Tuple[List[ChangedFile], List[ChangedFile]]:
    """Split changes into (kept, skipped) by file suffix.

    Used by local/eval runs that intentionally review only one language
    (e.g. AACR Go/Python cases). An empty/None suffix list means no filtering.
    """
    allowed = {
        suffix.lower()
        for suffix in (suffixes or ())
        if suffix and suffix.strip()
    }
    if not allowed:
        return list(files), []
    kept: List[ChangedFile] = []
    skipped: List[ChangedFile] = []
    for file in files:
        if Path(file.filename).suffix.lower() in allowed:
            kept.append(file)
        else:
            skipped.append(file)
    return kept, skipped

_STATUS_MAP = {
    "A": "added",
    "D": "removed",
    "M": "modified",
    "T": "modified",  # type change
    "U": "modified",
}


@dataclass
class _WorktreeSlot:
    """One detached worktree owned by :class:`WorktreePool`."""

    repo_root: Path
    path: Path
    sha: str
    in_use: bool = False


class WorktreePool:
    """Reuse detached worktrees across local reviews of the same repository.

    A large repository can contain tens of thousands of files (generated
    fixtures, testdata, ...), so ``git worktree add`` can take tens of seconds.
    Switching a reused worktree to another commit only updates files that
    differ, which is much cheaper. Slots are returned to the pool on cleanup
    and physically removed by :meth:`close`.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = (
            Path(root).expanduser().resolve()
            if root is not None
            else Path(tempfile.mkdtemp(prefix="openreview_worktrees_"))
        )
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._idle: Dict[str, List[_WorktreeSlot]] = {}
        self._all: List[_WorktreeSlot] = []
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root

    def acquire(self, repo_root: Path, sha: str) -> _WorktreeSlot:
        """Return an idle slot checked out at ``sha`` (creating one if needed)."""
        repo_root = Path(repo_root).resolve()
        key = str(repo_root)

        with self._lock:
            if self._closed:
                raise RepoCommandError(
                    command=["git", "worktree", "add"],
                    returncode=-1,
                    stderr="WorktreePool is closed",
                )
            idle = self._idle.setdefault(key, [])
            slot = self._pop_slot(idle, sha)
            if slot is None and idle:
                slot = idle.pop()

        if slot is None:
            return self._create_slot(repo_root, sha)

        if slot.sha != sha:
            try:
                self._checkout(slot.path, sha)
            except RepoCommandError as exc:
                logger.warning(
                    "Failed to reuse worktree %s for %s: %s",
                    slot.path,
                    sha[:8],
                    exc.stderr or exc,
                )
                self._dispose_slot(slot)
                return self._create_slot(repo_root, sha)
            slot.sha = sha
        slot.in_use = True
        return slot

    def release(self, slot: _WorktreeSlot) -> None:
        with self._lock:
            if not slot.in_use:
                return
            slot.in_use = False
            if self._closed:
                return
            self._idle.setdefault(str(slot.repo_root), []).append(slot)

    def close(self) -> None:
        """Remove every pooled worktree and the pool's temporary root."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            slots = list(self._all)
            self._all.clear()
            self._idle.clear()

        repos = {slot.repo_root for slot in slots}
        for slot in slots:
            self._remove_worktree(slot.repo_root, slot.path)
        for repo_root in repos:
            try:
                _git(repo_root, "worktree", "prune", check=False)
            except Exception:  # noqa: BLE001 - pool close is best-effort
                logger.debug("Could not prune worktrees for %s", repo_root)
        shutil.rmtree(self._root, ignore_errors=True)

    def __enter__(self) -> "WorktreePool":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @staticmethod
    def _pop_slot(
        idle: List[_WorktreeSlot], sha: str
    ) -> Optional[_WorktreeSlot]:
        for index, slot in enumerate(idle):
            if slot.sha == sha:
                return idle.pop(index)
        return None

    def _create_slot(self, repo_root: Path, sha: str) -> _WorktreeSlot:
        path = self._root / f"wt-{uuid.uuid4().hex}"
        try:
            _git(
                repo_root,
                "worktree",
                "add",
                "--detach",
                "--force",
                str(path),
                sha,
            )
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            _git(repo_root, "worktree", "prune", check=False)
            raise
        slot = _WorktreeSlot(repo_root=repo_root, path=path, sha=sha, in_use=True)
        with self._lock:
            self._all.append(slot)
        return slot

    @staticmethod
    def _checkout(path: Path, sha: str) -> None:
        _git(path, "checkout", "--force", "--detach", sha)

    def _dispose_slot(self, slot: _WorktreeSlot) -> None:
        with self._lock:
            try:
                self._all.remove(slot)
            except ValueError:
                pass
            idle = self._idle.get(str(slot.repo_root))
            if idle is not None:
                try:
                    idle.remove(slot)
                except ValueError:
                    pass
        self._remove_worktree(slot.repo_root, slot.path)

    @staticmethod
    def _remove_worktree(repo_root: Path, path: Path) -> None:
        try:
            _git(
                repo_root,
                "worktree",
                "remove",
                "--force",
                str(path),
                check=False,
            )
        except Exception:  # noqa: BLE001 - pool cleanup is best-effort
            logger.debug("Could not remove worktree %s", path)
        shutil.rmtree(path, ignore_errors=True)


@dataclass
class LocalReviewInput:
    """一次本地 review 的输入。owner/repo/pr_number 只用于报告元数据。"""

    repo_path: Path
    base_sha: str
    head_sha: str
    owner: str = "local"
    repo: str = "repo"
    pr_number: int = 0
    title: Optional[str] = None
    body: Optional[str] = None
    pr_url: Optional[str] = None
    keep_worktrees: bool = False
    # Eval-only language scope: keep only these suffixes (None = no filter).
    allowed_suffixes: Optional[Tuple[str, ...]] = None


@dataclass
class LocalWorkspace:
    """head/base 两个 detached worktree 的生命周期封装。"""

    repo_path: Path
    head_path: Path
    base_path: Optional[Path]
    temp_root: Optional[Path]
    keep: bool = False
    pool: Optional[WorktreePool] = None
    head_slot: Optional[_WorktreeSlot] = None
    base_slot: Optional[_WorktreeSlot] = None

    def cleanup(self) -> None:
        if self.keep:
            logger.info("Keeping local worktrees at %s", self.temp_root)
            return

        if self.pool is not None:
            # Return slots to the pool instead of deleting them. Idempotent:
            # clear the references first so a second cleanup is a no-op.
            head_slot, base_slot = self.head_slot, self.base_slot
            self.head_slot = None
            self.base_slot = None
            if head_slot is not None:
                self.pool.release(head_slot)
            if base_slot is not None:
                self.pool.release(base_slot)
            return

        for worktree in (self.head_path, self.base_path):
            if worktree is None or not worktree.exists():
                continue
            _git(
                self.repo_path,
                "worktree",
                "remove",
                "--force",
                str(worktree),
                check=False,
            )

        # 兜底：worktree 元数据可能已经损坏，直接删目录再 prune。
        if self.temp_root is not None:
            shutil.rmtree(self.temp_root, ignore_errors=True)
        _git(self.repo_path, "worktree", "prune", check=False)

    def __enter__(self) -> "LocalWorkspace":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()


def _git(
    repo_path: Path,
    *args: str,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """统一执行 git，错误时抛出带 stderr 的 RepoCommandError。"""
    cmd = ["git", "-C", str(repo_path), *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else "Command timed out"
        raise RepoCommandError(command=cmd, returncode=-1, stderr=stderr) from exc

    if check and result.returncode != 0:
        raise RepoCommandError(
            command=cmd,
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )
    return result


def resolve_repo_root(repo_path: str | Path) -> Path:
    """返回 git 工作区根目录；bare 仓库回退到自身路径。"""
    path = Path(repo_path).expanduser().resolve()
    if not path.exists():
        raise RepoCommandError(
            command=["git", "-C", str(path), "rev-parse"],
            returncode=-1,
            stderr=f"Repository path does not exist: {path}",
        )

    result = _git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()

    # bare repo 没有 worktree，但 git worktree add 仍然可用。
    bare = _git(path, "rev-parse", "--is-bare-repository", check=False)
    if bare.returncode == 0 and bare.stdout.strip() == "true":
        return path

    raise RepoCommandError(
        command=["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        returncode=result.returncode,
        stderr=result.stderr.strip() or "Not a git repository",
    )


def _validate_commit(repo_path: Path, sha: str, label: str) -> None:
    if not sha or not sha.strip():
        raise ValueError(f"{label} is empty")
    result = _git(
        repo_path, "cat-file", "-e", f"{sha.strip()}^{{commit}}", check=False
    )
    if result.returncode != 0:
        raise RepoCommandError(
            command=["git", "-C", str(repo_path), "cat-file", "-e", sha],
            returncode=result.returncode,
            stderr=f"{label} commit not found: {sha}",
        )


def prepare_local_workspace(
    repo_path: str | Path,
    base_sha: str,
    head_sha: str,
    *,
    keep: bool = False,
    pool: Optional[WorktreePool] = None,
) -> LocalWorkspace:
    """为 head/base 各创建一个 detached worktree。

    ``pool`` 存在且 ``keep`` 为 False 时，从复用池获取 worktree；调用
    ``LocalWorkspace.cleanup()`` 只会把 worktree 归还池子，真正删除交给
    ``WorktreePool.close()``。
    """
    repo_root = resolve_repo_root(repo_path)
    base = base_sha.strip()
    head = head_sha.strip()
    _validate_commit(repo_root, base, "base_sha")
    _validate_commit(repo_root, head, "head_sha")
    if base == head:
        raise ValueError("base_sha and head_sha are identical; nothing to review")

    if pool is not None and not keep:
        head_slot = pool.acquire(repo_root, head)
        base_slot: Optional[_WorktreeSlot] = None
        try:
            base_slot = pool.acquire(repo_root, base)
        except RepoCommandError as exc:
            # base 侧只用于增强上下文，失败不应阻断 review。
            logger.warning("Failed to acquire base worktree: %s", exc.stderr)
        return LocalWorkspace(
            repo_path=repo_root,
            head_path=head_slot.path,
            base_path=base_slot.path if base_slot is not None else None,
            temp_root=pool.root,
            keep=False,
            pool=pool,
            head_slot=head_slot,
            base_slot=base_slot,
        )

    temp_root = Path(tempfile.mkdtemp(prefix="openreview_local_"))
    head_path = temp_root / "head"
    base_dir = temp_root / "base"
    base_path: Optional[Path] = base_dir

    try:
        _git(
            repo_root,
            "worktree",
            "add",
            "--detach",
            "--force",
            str(head_path),
            head,
        )
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise

    try:
        _git(
            repo_root,
            "worktree",
            "add",
            "--detach",
            "--force",
            str(base_dir),
            base,
        )
    except RepoCommandError as exc:
        # base 侧只用于增强上下文，失败不应阻断 review。
        logger.warning("Failed to create base worktree: %s", exc.stderr)
        shutil.rmtree(base_dir, ignore_errors=True)
        base_path = None

    return LocalWorkspace(
        repo_path=repo_root,
        head_path=head_path,
        base_path=base_path,
        temp_root=temp_root,
        keep=keep,
    )


def _parse_numstat(output: str) -> Dict[str, Tuple[int, int]]:
    """解析 ``git diff --numstat -z``：add\\tdel\\tpath\\0。"""
    stats: Dict[str, Tuple[int, int]] = {}
    for record in output.split("\0"):
        if not record:
            continue
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        stats[path] = (
            0 if added == "-" else int(added),
            0 if deleted == "-" else int(deleted),
        )
    return stats


def _parse_name_status(output: str) -> List[Tuple[str, str]]:
    """解析 ``git diff --name-status -z``：status\\0path\\0。"""
    records = output.split("\0")
    result: List[Tuple[str, str]] = []
    for index in range(0, len(records) - 1, 2):
        status = records[index].strip()
        path = records[index + 1]
        if not status or not path:
            continue
        result.append((status, path))
    return result


def _split_patch_by_file(patch: str) -> List[str]:
    """Split a combined ``git diff --patch`` output into per-file chunks."""
    if not patch:
        return []
    chunks: List[str] = []
    current: List[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            chunks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("".join(current))
    return chunks


def resolve_merge_base(
    repo_path: str | Path,
    base_sha: str,
    head_sha: str,
) -> str:
    """返回 GitHub 三点 diff（``base...head``）使用的 merge-base。

    数据集里的 ``base_sha`` 经常是采集时 base 分支的 tip，而不是 PR 分支
    实际分叉的 commit。直接做 ``base..head`` 会把 base 分支后来合入的无关
    改动也算进来，导致 review 一个巨大的伪 diff。GitHub PR 展示的是
    ``merge-base(base, head)..head``，这里返回同一个起点。
    """
    repo_root = resolve_repo_root(repo_path)
    base = base_sha.strip()
    head = head_sha.strip()
    _validate_commit(repo_root, base, "base_sha")
    _validate_commit(repo_root, head, "head_sha")

    result = _git(repo_root, "merge-base", base, head)
    merge_base = result.stdout.strip()
    if not merge_base:
        raise RepoCommandError(
            command=["git", "-C", str(repo_root), "merge-base", base, head],
            returncode=1,
            stderr="merge-base returned an empty commit",
        )
    return merge_base


def collect_local_changes(
    repo_path: str | Path,
    base_sha: str,
    head_sha: str,
) -> List[ChangedFile]:
    """生成与 GitHub ``list_changed_files`` 等价的文件列表。

    使用三点 diff（``base...head``）：GitHub 的 PR 视图也是以 merge-base
    为起点的。这样即使 ``base_sha`` 是 base 分支的较新 tip，也不会把
    分支分叉后 base 分支上的无关改动算进本次 PR。
    """
    repo_root = resolve_repo_root(repo_path)
    base = base_sha.strip()
    head = head_sha.strip()
    _validate_commit(repo_root, base, "base_sha")
    _validate_commit(repo_root, head, "head_sha")
    diff_range = f"{base}...{head}"

    numstat = _git(
        repo_root,
        "diff",
        "--no-renames",
        "--numstat",
        "-z",
        diff_range,
    ).stdout
    name_status = _git(
        repo_root,
        "diff",
        "--no-renames",
        "--name-status",
        "-z",
        diff_range,
    ).stdout

    stats = _parse_numstat(numstat)
    entries = _parse_name_status(name_status)
    # Render every patch in one git process. The previous per-file loop issued
    # one `git diff` per changed file, which dominated large local reviews.
    combined_patch = _git(
        repo_root,
        "diff",
        "--no-renames",
        "--patch",
        "--no-color",
        "--no-ext-diff",
        "--unified=3",
        diff_range,
    ).stdout
    chunks = _split_patch_by_file(combined_patch)
    # `git diff --name-status` and `git diff --patch` walk the same tree in the
    # same order, so chunks align one-to-one with entries. Fall back to
    # per-file diffs only if that assumption does not hold (e.g. git version
    # differences or unusual path handling).
    patch_by_path = (
        dict(zip((path for _, path in entries), chunks, strict=True))
        if len(chunks) == len(entries)
        else {}
    )

    changed: List[ChangedFile] = []
    for raw_status, path in entries:
        status = _STATUS_MAP.get(raw_status[:1], "modified")
        additions, deletions = stats.get(path, (0, 0))
        patch = patch_by_path.get(path)
        if patch is None:
            patch = _git(
                repo_root,
                "diff",
                "--no-renames",
                "--patch",
                "--no-color",
                "--no-ext-diff",
                "--unified=3",
                diff_range,
                "--",
                path,
            ).stdout
        changed.append(
            ChangedFile(
                filename=path,
                status=status,
                additions=additions,
                deletions=deletions,
                changes=additions + deletions,
                patch=patch or None,
            )
        )

    return changed
