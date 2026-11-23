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


def _ensure_commit_from_pull_ref(
    repo: Path,
    commit: str,
    pull_number: int,
    *,
    timeout: int,
) -> None:
    """Fetch a PR ref as a fallback for commits omitted from branch refspecs."""
    ref = f"refs/pull/{pull_number}/head"
    result = run_git(
        repo,
        "fetch",
        "origin",
        ref,
        timeout=timeout,
        check=False,
    )
    if result.returncode == 0 and _has_commit(repo, commit):
        return
    raise RepoError(
        f"commit {commit} is not available from origin {ref}"
        + (f": {result.stderr.strip()}" if result.stderr.strip() else "")
    )


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
    path = _ensure_repo_checkout(
        cache_dir, repo, clone_url, timeout=timeout
    )
    # Cached commits need no network access. Fetch only when a required object
    # is missing; ensure_commit falls back to a full fetch if a direct fetch fails.
    ensure_commit(path, base_commit, timeout=timeout)
    ensure_commit(path, head_commit, timeout=timeout)
    return path


def _ensure_repo_checkout(
    cache_dir: Path,
    repo: str,
    clone_url: str,
    *,
    timeout: int,
) -> Path:
    """Clone a repository if needed, without checking any commit."""
    path = repo_cache_path(cache_dir, repo)
    if not (path / ".git").exists():
        if path.exists():
            # 半截目录通常来自上次中断；直接清掉重来。
            shutil.rmtree(path, ignore_errors=True)
        print(f"[repo] clone {repo} -> {path}")
        _clone(path, clone_url, timeout=timeout)
    return path


def _print_prepare_progress(completed: int, total: int, *, width: int = 24) -> None:
    filled = round(width * completed / total)
    bar = "#" * filled + "-" * (width - filled)
    print(f"[repo-cache] [{bar}] {completed}/{total} repositories", flush=True)


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
    worker_count = min(max_workers, len(by_repo))
    print(
        f"[repo-cache] preparing {len(by_repo)} repositories "
        f"({worker_count} concurrent workers)",
        flush=True,
    )
    _print_prepare_progress(0, len(by_repo))

    def prepare_one(
        repo: str, repo_instances: List[Dict[str, Any]]
    ) -> List[str]:
        first = repo_instances[0]
        commit_pull_numbers: Dict[str, set[int]] = {}
        for instance in repo_instances:
            pull_number = instance.get("pr_number")
            for commit in (instance["base_commit"], instance["head_commit"]):
                commit_pull_numbers.setdefault(commit, set())
                if pull_number is not None:
                    commit_pull_numbers[commit].add(int(pull_number))
        failures: List[str] = []
        try:
            repo_path = _ensure_repo_checkout(
                cache_dir,
                repo,
                first.get("clone_url") or f"https://github.com/{repo}.git",
                timeout=timeout,
            )
        except RepoError as error:
            return [f"{repo}: clone/cache preparation failed: {error}"]

        for commit, pull_numbers in sorted(commit_pull_numbers.items()):
            try:
                ensure_commit(repo_path, commit, timeout=timeout)
            except RepoError as error:
                recovered = False
                fallback_errors: List[str] = []
                for pull_number in sorted(pull_numbers):
                    try:
                        _ensure_commit_from_pull_ref(
                            repo_path,
                            commit,
                            pull_number,
                            timeout=timeout,
                        )
                    except RepoError as fallback_error:
                        fallback_errors.append(str(fallback_error))
                    else:
                        recovered = True
                        break
                if not recovered:
                    detail = "; ".join(fallback_errors)
                    failures.append(
                        f"{repo} @ {commit}: {error}"
                        + (f"; PR-ref fallback: {detail}" if detail else "")
                    )
        return failures

    completed = 0
    failures: List[str] = []
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="repo-cache",
    ) as executor:
        futures = {
            executor.submit(prepare_one, repo, repo_instances): repo
            for repo, repo_instances in by_repo.items()
        }
        for future in as_completed(futures):
            repo = futures[future]
            repo_failures = future.result()
            failures.extend(repo_failures)
            completed += 1
            _print_prepare_progress(completed, len(by_repo))
            if repo_failures:
                print(
                    f"[repo-cache] {repo}: {len(repo_failures)} failure(s), "
                    "continuing",
                    flush=True,
                )
    if failures:
        failure_list = "\n".join(f"- {failure}" for failure in sorted(failures))
        raise RepoError(
            f"仓库预热已完成，但有 {len(failures)} 项失败；其余仓库/commit 已继续处理：\n"
            f"{failure_list}"
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
