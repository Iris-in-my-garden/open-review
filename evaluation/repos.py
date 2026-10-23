"""评测用的仓库缓存：clone / fetch / 按 commit 读取文件。

和线上 review 的区别：
- 线上由 ``RepoManager`` 管理 GitHub PR 的 head/base worktree；
- 评测输入是任意 repo + 两个 SHA，这里只保证 commit 可用，真正的
  head/base worktree 由 ``ReviewPipeline.run_local`` 创建并清理。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

DEFAULT_TIMEOUT = 1800


class RepoError(RuntimeError):
    """评测仓库准备失败。"""


def run_git(
    repo: Optional[Path],
    *args: str,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(args)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RepoError(f"git 命令超时: {' '.join(command)}") from error
    if check and result.returncode != 0:
        raise RepoError(
            f"git 命令失败 ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result


def repo_cache_path(cache_dir: Path, repo: str) -> Path:
    """owner/name -> <cache>/owner__name，和 AACR 的目录约定保持一致。"""
    return Path(cache_dir) / repo.replace("/", "__")


def _clone(
    path: Path,
    clone_url: str,
    *,
    timeout: int,
) -> None:
    # 刻意不用 --filter=blob:none：review 内部的 impact / retriever 会跑
    # `git grep`，partial clone 会为缺失的 blob 触发懒加载，轻则变慢，重则
    # 在无网 CI 上直接挂住。评测仓库是一次性成本，优先保证可预测。
    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        None,
        "clone",
        "--no-checkout",
        clone_url,
        str(path),
        timeout=timeout,
    )


def _has_commit(repo: Path, commit: str) -> bool:
    result = run_git(
        repo,
        "cat-file",
        "-e",
        f"{commit}^{{commit}}",
        timeout=120,
        check=False,
    )
    return result.returncode == 0


def ensure_commit(repo: Path, commit: str, *, timeout: int = DEFAULT_TIMEOUT) -> None:
    """确保 commit 在本地对象库中；缺失时先定向 fetch，再全量 fetch。"""
    if _has_commit(repo, commit):
        return

    direct = run_git(
        repo,
        "fetch",
        "origin",
        commit,
        timeout=timeout,
        check=False,
    )
    if direct.returncode == 0 and _has_commit(repo, commit):
        return

    run_git(
        repo,
        "fetch",
        "--all",
        "--tags",
        "--prune",
        timeout=timeout,
        check=False,
    )
    if not _has_commit(repo, commit):
        raise RepoError(f"commit 不可用: {commit} (repo={repo})")


def ensure_repo(
    cache_dir: Path,
    repo: str,
    clone_url: str,
    base_commit: str,
    head_commit: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> Path:
    """返回一个包含 base/head commit 的本地仓库（不 checkout）。"""
    path = repo_cache_path(cache_dir, repo)
    if not (path / ".git").exists():
        if path.exists():
            # 半截目录通常来自上次中断；直接清掉重来。
            shutil.rmtree(path, ignore_errors=True)
        print(f"[repo] clone {repo} -> {path}")
        _clone(path, clone_url, timeout=timeout)
    else:
        # 已有缓存：先尝试更新 refs；失败不致命，只要 commit 已存在即可。
        run_git(
            path,
            "fetch",
            "--all",
            "--tags",
            "--prune",
            timeout=timeout,
            check=False,
        )

    ensure_commit(path, base_commit, timeout=timeout)
    ensure_commit(path, head_commit, timeout=timeout)
    return path


def read_file_at_commit(
    repo: Path,
    commit: str,
    path: str,
    *,
    timeout: int = 120,
) -> Optional[str]:
    """读取某个 commit 下的文件内容；不存在/二进制不可读时返回 None。"""
    result = run_git(
        repo,
        "show",
        f"{commit}:{path}",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout
