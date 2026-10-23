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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from openreview.github.client import ChangedFile
from openreview.repo.manager import RepoCommandError

logger = logging.getLogger(__name__)

_STATUS_MAP = {
    "A": "added",
    "D": "removed",
    "M": "modified",
    "T": "modified",  # type change
    "U": "modified",
}


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


@dataclass
class LocalWorkspace:
    """head/base 两个 detached worktree 的生命周期封装。"""

    repo_path: Path
    head_path: Path
    base_path: Optional[Path]
    temp_root: Optional[Path]
    keep: bool = False

    def cleanup(self) -> None:
        if self.keep:
            logger.info("Keeping local worktrees at %s", self.temp_root)
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
) -> LocalWorkspace:
    """为 head/base 各创建一个 detached worktree。"""
    repo_root = resolve_repo_root(repo_path)
    base = base_sha.strip()
    head = head_sha.strip()
    _validate_commit(repo_root, base, "base_sha")
    _validate_commit(repo_root, head, "head_sha")
    if base == head:
        raise ValueError("base_sha and head_sha are identical; nothing to review")

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


def collect_local_changes(
    repo_path: str | Path,
    base_sha: str,
    head_sha: str,
) -> List[ChangedFile]:
    """生成与 GitHub ``list_changed_files`` 等价的文件列表。"""
    repo_root = resolve_repo_root(repo_path)
    base = base_sha.strip()
    head = head_sha.strip()
    _validate_commit(repo_root, base, "base_sha")
    _validate_commit(repo_root, head, "head_sha")

    numstat = _git(
        repo_root,
        "diff",
        "--no-renames",
        "--numstat",
        "-z",
        base,
        head,
    ).stdout
    name_status = _git(
        repo_root,
        "diff",
        "--no-renames",
        "--name-status",
        "-z",
        base,
        head,
    ).stdout

    stats = _parse_numstat(numstat)
    changed: List[ChangedFile] = []
    for raw_status, path in _parse_name_status(name_status):
        status = _STATUS_MAP.get(raw_status[:1], "modified")
        additions, deletions = stats.get(path, (0, 0))
        patch = _git(
            repo_root,
            "diff",
            "--no-renames",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--unified=3",
            base,
            head,
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
