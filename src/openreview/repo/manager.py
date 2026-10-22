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

    def prepare_base_worktree(
        self, owner: str, repo: str, base_sha: str
    ) -> Path:
        """把 PR 的 base SHA checkout 到独立 worktree，用于查看被删除代码的上下文。"""
        repo_dir = self.base_cache_dir / f"{owner}__{repo}"
        base_dir = self.base_cache_dir / f"{owner}__{repo}__base"

        with self._repo_lock(owner, repo):
            if not repo_dir.exists():
                raise RepoCommandError(
                    ["git", "worktree", "add"],
                    -1,
                    f"Repository cache not found: {repo_dir}",
                )

            if not self._git_ref_exists(base_sha, repo_dir):
                self._run_git_cmd(["fetch", "origin", base_sha], cwd=repo_dir)

            if base_dir.exists():
                try:
                    self._run_git_cmd(
                        ["worktree", "remove", "--force", str(base_dir)],
                        cwd=repo_dir,
                    )
                except RepoCommandError:
                    # 目录存在但未注册为 worktree，直接清理后 prune
                    shutil.rmtree(base_dir, ignore_errors=True)
                    self._run_git_cmd(["worktree", "prune"], cwd=repo_dir)
            else:
                # 缓存淘汰可能删掉了目录但留下 worktree 元数据
                self._run_git_cmd(["worktree", "prune"], cwd=repo_dir)

            self._run_git_cmd(
                ["worktree", "add", "--detach", str(base_dir), base_sha],
                cwd=repo_dir,
            )
            self._prune_cache(exclude={repo_dir, base_dir})
            logger.debug("Base worktree ready at %s (%s)", base_dir, base_sha)
            return base_dir.resolve()

    def _prune_cache(self, exclude: set[Path] | None = None) -> None:
        """按 mtime 淘汰旧仓库，保证缓存不超过配置上限。

        ``max_cache_size_gb=0`` 表示禁用淘汰，便于测试或用户自行管理。
        """
        if self.max_cache_size_bytes <= 0:
            return

        exclude = {path.resolve() for path in (exclude or set())}
        entries: list[tuple[float, int, Path]] = []
        total_size = 0

        for entry in self.base_cache_dir.iterdir():
            if entry.name == ".locks" or not entry.is_dir():
                continue
            size = self._dir_size(entry)
            total_size += size
            if entry.resolve() not in exclude:
                entries.append((entry.stat().st_mtime, size, entry))

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
