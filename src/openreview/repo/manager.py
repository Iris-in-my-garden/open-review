"""把 GitHub PR 的 head/base 版本准备到本地缓存仓库。

P2 改动：
- 仓库级 FileLock，避免并发 clone/checkout 互相踩踏
- 默认使用 blobless partial clone（``--filter=blob:none``），大仓库更快
- 默认缓存目录放到平台缓存目录，而不是相对当前工作目录
- 超过缓存上限时按最近使用时间淘汰旧仓库
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)


def default_cache_dir() -> Path:
    """返回平台相关的默认缓存目录。"""
    env_dir = os.getenv("OPENREVIEW_CACHE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Caches" / "openreview" / "repos").resolve()
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return (base / "openreview" / "Cache" / "repos").resolve()

    xdg_cache = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache"))
    return (xdg_cache / "openreview" / "repos").resolve()


class RepoCommandError(Exception):
    """当 Git 命令执行失败时抛出的自定义异常"""

    def __init__(self, command: list[str], returncode: int, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"Command '{' '.join(command)}' failed with exit code {returncode}.\nStderr: {stderr}"
        )


class RepoManager:

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        timeout: int = 120,
        lock_timeout: int = 120,
        max_cache_size_gb: float = 10.0,
        clone_filter: bool = True,
    ):
        self.base_cache_dir = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir is not None
            else default_cache_dir()
        )
        self.base_cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.lock_timeout = lock_timeout
        self.max_cache_size_bytes = int(max_cache_size_gb * 1024**3)
        self.clone_filter = clone_filter
        self._locks_dir = self.base_cache_dir / ".locks"
        self._locks_dir.mkdir(exist_ok=True)
        # Active paths must not be evicted by another run's cache pruning.
        # A per-run worktree *and* the shared clone that backs it are leased:
        # the worktree's .git file points into the clone, so evicting the
        # clone would break git commands inside an active review. Refcounted
        # because one clone can back several concurrent worktrees.
        self._active_paths: dict[Path, int] = {}
        self._worktree_repo_leases: dict[Path, Path] = {}
        self._active_lock = threading.Lock()

    def _hold_path_unlocked(self, path: Path) -> None:
        self._active_paths[path] = self._active_paths.get(path, 0) + 1

    def _release_path_unlocked(self, path: Path) -> None:
        remaining = self._active_paths.get(path, 0) - 1
        if remaining > 0:
            self._active_paths[path] = remaining
        else:
            self._active_paths.pop(path, None)

    @contextmanager
    def _path_lease(self, path: Path) -> Iterator[None]:
        """Keep ``path`` out of cache pruning for the duration of the block."""
        resolved = Path(path).resolve()
        with self._active_lock:
            self._hold_path_unlocked(resolved)
        try:
            yield
        finally:
            with self._active_lock:
                self._release_path_unlocked(resolved)

    def _hold_worktree(self, worktree: Path, repo_dir: Path) -> None:
        """Lease a worktree and its backing clone until :meth:`release_worktree`."""
        resolved_worktree = Path(worktree).resolve()
        resolved_repo = Path(repo_dir).resolve()
        with self._active_lock:
            self._hold_path_unlocked(resolved_worktree)
            self._hold_path_unlocked(resolved_repo)
            self._worktree_repo_leases[resolved_worktree] = resolved_repo

    def _release_worktree_lease(self, worktree: Path) -> None:
        resolved = Path(worktree).resolve()
        with self._active_lock:
            repo_dir = self._worktree_repo_leases.pop(resolved, None)
            self._release_path_unlocked(resolved)
            if repo_dir is not None:
                self._release_path_unlocked(repo_dir)

    @contextmanager
    def _repo_lock(self, owner: str, repo: str) -> Iterator[None]:
        lock_path = self._locks_dir / f"{owner}__{repo}.lock"
        try:
            with FileLock(str(lock_path), timeout=self.lock_timeout):
                yield
        except Timeout as exc:
            raise RepoCommandError(
                ["filelock", str(lock_path)],
                -1,
                f"Timed out waiting for repository lock after {self.lock_timeout}s",
            ) from exc

    def _run_git_cmd(
        self, args: list[str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess:
        """统一封装 subprocess.run 执行 git 命令"""
        cmd = ["git"] + args
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
            if result.returncode != 0:
                raise RepoCommandError(
                    command=cmd, returncode=result.returncode, stderr=result.stderr
                )
            return result
        except subprocess.TimeoutExpired as e:
            stderr_msg = e.stderr if isinstance(e.stderr, str) else "Command timed out"
            raise RepoCommandError(
                command=cmd, returncode=-1, stderr=stderr_msg
            ) from e

    def _clone_repo(self, repo_url: str, target_dir: Path) -> None:
        clone_args = ["clone", "--no-tags"]
        if self.clone_filter:
            clone_args.append("--filter=blob:none")
        clone_args.extend([repo_url, str(target_dir)])

        if not self.clone_filter:
            self._run_git_cmd(clone_args)
            return

        try:
            self._run_git_cmd(clone_args)
        except RepoCommandError as exc:
            logger.warning(
                "Partial clone failed, falling back to full clone: %s", exc.stderr
            )
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            self._run_git_cmd(
                ["clone", "--no-tags", repo_url, str(target_dir)]
            )

    def prepare_pr_repo(self, owner: str, repo: str, pr_number: int) -> Path:
        """克隆/更新仓库并 checkout 到指定的 PR 分支"""
        repo_url = f"https://github.com/{owner}/{repo}.git"
        target_dir = self.base_cache_dir / f"{owner}__{repo}"

        # Hold the shared checkout while fetching/checking out so another
        # repository's cache pruning cannot delete it mid-operation.
        with self._path_lease(target_dir):
            with self._repo_lock(owner, repo):
                if not target_dir.exists():
                    self._clone_repo(repo_url, target_dir)

                # 先清理可能存在的脏文件，保证工作区纯净
                self._run_git_cmd(["reset", "--hard"], cwd=target_dir)

                # 拉取最新 PR HEAD 到 FETCH_HEAD
                self._run_git_cmd(
                    ["fetch", "origin", f"pull/{pr_number}/head"], cwd=target_dir
                )

                # 强制切到 pr-xxx 分支并重置到 FETCH_HEAD
                self._run_git_cmd(
                    ["checkout", "-B", f"pr-{pr_number}", "FETCH_HEAD"], cwd=target_dir
                )

                base_dir = self.base_cache_dir / f"{owner}__{repo}__base"
                self._prune_cache(exclude={target_dir, base_dir})
                return target_dir.resolve()

    def _git_ref_exists(self, ref: str, cwd: Path) -> bool:
        try:
            self._run_git_cmd(["cat-file", "-e", f"{ref}^{{commit}}"], cwd=cwd)
            return True
        except RepoCommandError:
            return False

    def prepare_pr_worktree(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        head_sha: str | None = None,
        run_id: str | None = None,
    ) -> Path:
        """Fetch a PR head into a per-run detached worktree.

        ``prepare_pr_repo`` mutates one shared checkout. Two concurrent reviews
        of the same repository could therefore read whichever PR was checked
        out last. This method keeps the shared clone as an object store and
        gives every run its own worktree, so the returned path is stable for
        the lifetime of that run.
        """
        repo_url = f"https://github.com/{owner}/{repo}.git"
        repo_dir = self.base_cache_dir / f"{owner}__{repo}"
        suffix = (run_id or head_sha or f"pr{pr_number}")[:12]
        worktree_dir = (
            self.base_cache_dir
            / f"{owner}__{repo}__pr_{pr_number}_{suffix}"
        )

        with self._path_lease(repo_dir):
            with self._repo_lock(owner, repo):
                if not repo_dir.exists():
                    self._clone_repo(repo_url, repo_dir)

                self._run_git_cmd(
                    ["fetch", "origin", f"pull/{pr_number}/head"], cwd=repo_dir
                )
                fetched = self._run_git_cmd(
                    ["rev-parse", "FETCH_HEAD"], cwd=repo_dir
                ).stdout.strip()
                target = (
                    head_sha
                    if head_sha and self._git_ref_exists(head_sha, repo_dir)
                    else fetched
                )

                self._remove_worktree(repo_dir, worktree_dir)
                self._run_git_cmd(
                    ["worktree", "add", "--detach", str(worktree_dir), target],
                    cwd=repo_dir,
                )
                resolved = worktree_dir.resolve()
                # Lease worktree + backing clone for the whole review run;
                # release_worktree() drops both.
                self._hold_worktree(resolved, repo_dir)
                self._prune_cache(exclude={repo_dir, resolved})
                logger.debug("PR worktree ready at %s (%s)", worktree_dir, target)
                return resolved

    def _remove_worktree(self, repo_dir: Path, worktree_dir: Path) -> None:
        if worktree_dir.exists():
            try:
                self._run_git_cmd(
                    ["worktree", "remove", "--force", str(worktree_dir)],
                    cwd=repo_dir,
                )
            except RepoCommandError:
                # 目录存在但未注册为 worktree，直接清理后 prune
                shutil.rmtree(worktree_dir, ignore_errors=True)
                self._run_git_cmd(["worktree", "prune"], cwd=repo_dir)
        else:
            # 缓存淘汰可能删掉了目录但留下 worktree 元数据
            self._run_git_cmd(["worktree", "prune"], cwd=repo_dir)

    def release_worktree(self, owner: str, repo: str, path: Path) -> None:
        """Remove a worktree created by :meth:`prepare_pr_worktree`."""
        repo_dir = self.base_cache_dir / f"{owner}__{repo}"
        resolved = Path(path).resolve()
        try:
            if not resolved.is_relative_to(self.base_cache_dir.resolve()):
                logger.warning("Refusing to release path outside cache: %s", path)
                return
        except ValueError:
            return
        with self._repo_lock(owner, repo):
            try:
                if repo_dir.exists():
                    self._remove_worktree(repo_dir, resolved)
                else:
                    shutil.rmtree(resolved, ignore_errors=True)
            finally:
                # Even if git cleanup fails, drop the lease so the cache can
                # eventually reclaim the path instead of leaking it forever.
                self._release_worktree_lease(resolved)

    def prepare_base_worktree(
        self,
        owner: str,
        repo: str,
        base_sha: str,
        *,
        worktree_name: str | None = None,
    ) -> Path:
        """把 PR 的 base SHA checkout 到独立 worktree，用于查看被删除代码的上下文。"""
        repo_dir = self.base_cache_dir / f"{owner}__{repo}"
        name = (
            f"{owner}__{repo}__{worktree_name}"
            if worktree_name
            else f"{owner}__{repo}__base"
        )
        base_dir = self.base_cache_dir / name

        with self._path_lease(repo_dir):
            with self._repo_lock(owner, repo):
                if not repo_dir.exists():
                    raise RepoCommandError(
                        ["git", "worktree", "add"],
                        -1,
                        f"Repository cache not found: {repo_dir}",
                    )

                if not self._git_ref_exists(base_sha, repo_dir):
                    self._run_git_cmd(["fetch", "origin", base_sha], cwd=repo_dir)

                self._remove_worktree(repo_dir, base_dir)

                self._run_git_cmd(
                    ["worktree", "add", "--detach", str(base_dir), base_sha],
                    cwd=repo_dir,
                )
                resolved = base_dir.resolve()
                if worktree_name:
                    # Only run-scoped base worktrees are leased/released; the
                    # legacy shared path keeps its historical lifetime.
                    self._hold_worktree(resolved, repo_dir)
                self._prune_cache(exclude={repo_dir, resolved})
                logger.debug("Base worktree ready at %s (%s)", base_dir, base_sha)
                return resolved

    def _prune_cache(self, exclude: set[Path] | None = None) -> None:
        """按 mtime 淘汰旧仓库，保证缓存不超过配置上限。

        ``max_cache_size_gb=0`` 表示禁用淘汰，便于测试或用户自行管理。
        """
        if self.max_cache_size_bytes <= 0:
            return

        exclude = {path.resolve() for path in (exclude or set())}
        with self._active_lock:
            exclude.update(self._active_paths)
        entries: list[tuple[float, int, Path]] = []
        total_size = 0

        for entry in self.base_cache_dir.iterdir():
            if entry.name == ".locks" or not entry.is_dir():
                continue
            size = self._dir_size(entry)
            total_size += size
            if entry.resolve() in exclude:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                # Another concurrent prune removed it between iterdir/stat.
                total_size -= size
                continue
            entries.append((mtime, size, entry))

        if total_size <= self.max_cache_size_bytes:
            return

        entries.sort(key=lambda item: item[0])
        for _, size, entry in entries:
            if total_size <= self.max_cache_size_bytes:
                break
            logger.info(
                "Evicting repository cache %s (%.1f MB)",
                entry,
                size / (1024 * 1024),
            )
            shutil.rmtree(entry, ignore_errors=True)
            total_size -= size

    @staticmethod
    def _dir_size(path: Path) -> int:
        total = 0
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
        return total
