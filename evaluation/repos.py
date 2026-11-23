"""评测用的仓库缓存：clone / fetch / 按 commit 读取文件。

和线上 review 的区别：
- 线上由 ``RepoManager`` 管理 GitHub PR 的 head/base worktree；
- 评测输入是任意 repo + 两个 SHA，这里只保证 commit 可用，真正的
  head/base worktree 由 ``ReviewPipeline.run_local`` 创建并清理。
"""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_TIMEOUT = 1800
MAX_PREPARE_WORKERS = 3


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
    # 在无网 CI 上直接挂住。评测仓库是一次性准备，优先保证可预测。
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["git", "clone", "--progress", "--no-checkout", clone_url, str(path)]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RepoError(f"git 命令超时: {' '.join(command)}") from error
    if result.returncode != 0:
        raise RepoError(
            f"git 命令失败 ({result.returncode}): {' '.join(command)}"
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
    """返回包含所需 commits 的本地仓库；对象已缓存时不发起网络 fetch。"""
    path = repo_cache_path(cache_dir, repo)
    if not (path / ".git").exists():
        if path.exists():
            # 半截目录通常来自上次中断；直接清掉重来。
            shutil.rmtree(path, ignore_errors=True)
        print(f"[repo] clone {repo} -> {path}")
        _clone(path, clone_url, timeout=timeout)

    # Cached commits need no network access. Fetch only when a required object
    # is missing; ensure_commit falls back to a full fetch if a direct fetch fails.
    ensure_commit(path, base_commit, timeout=timeout)
    ensure_commit(path, head_commit, timeout=timeout)
    return path


def prepare_repositories(
    instances: Sequence[Dict[str, Any]],
    *,
    cache_dir: Path,
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int = MAX_PREPARE_WORKERS,
) -> int:
    """Pre-populate repo caches concurrently and ensure all requested commits."""
    by_repo: Dict[str, List[Dict[str, Any]]] = {}
    for instance in instances:
        by_repo.setdefault(instance["repo"], []).append(instance)

    if not by_repo:
        return 0
    if max_workers < 1:
        raise ValueError("max_workers must be positive")

    def prepare_one(repo: str, repo_instances: List[Dict[str, Any]]) -> None:
        first = repo_instances[0]
        repo_path = ensure_repo(
            cache_dir,
            repo,
            first.get("clone_url") or f"https://github.com/{repo}.git",
            first["base_commit"],
            first["head_commit"],
            timeout=timeout,
        )
        commits = {
            commit
            for instance in repo_instances
            for commit in (instance["base_commit"], instance["head_commit"])
        }
        commits.discard(first["base_commit"])
        commits.discard(first["head_commit"])
        for commit in sorted(commits):
            ensure_commit(repo_path, commit, timeout=timeout)

    completed = 0
    progress_lock = Lock()
    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(by_repo)),
        thread_name_prefix="repo-cache",
    ) as executor:
        futures = {
            executor.submit(prepare_one, repo, repo_instances): repo
            for repo, repo_instances in by_repo.items()
        }
        for future in as_completed(futures):
            repo = futures[future]
            future.result()
            with progress_lock:
                completed += 1
                print(
                    f"[repo-cache] ({completed}/{len(by_repo)}) ready {repo}",
                    flush=True,
                )
    return len(by_repo)


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
