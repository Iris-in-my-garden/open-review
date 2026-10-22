""" 给定owner、repo、pr_number，把Github仓库clone到本地缓存目录，并且checkout到pr分支"""

from pathlib import Path
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


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

    def __init__(self, cache_dir: str | Path = ".cache/repos", timeout: int = 60):
        self.base_cache_dir = Path(cache_dir)
        self.timeout = timeout

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

    def prepare_pr_repo(self, owner: str, repo: str, pr_number: int) -> Path:
        """克隆/更新仓库并 checkout 到指定的 PR 分支"""
        repo_url = f"https://github.com/{owner}/{repo}.git"
        target_dir = self.base_cache_dir / f"{owner}__{repo}"

        # 确保缓存根目录存在
        target_dir.parent.mkdir(parents=True, exist_ok=True)

        if not target_dir.exists():
            # 1. 目录不存在，执行 clone
            self._run_git_cmd(["clone", repo_url, str(target_dir)])

        pr_branch = f"pr-{pr_number}"

        # 先清理可能存在的脏文件（可选，保证工作区纯净）
        self._run_git_cmd(["reset", "--hard"], cwd=target_dir)

        # 拉取最新 PR HEAD 到 FETCH_HEAD
        self._run_git_cmd(["fetch", "origin", f"pull/{pr_number}/head"], cwd=target_dir)

        # 强制切到 pr-xxx 分支并重置到 FETCH_HEAD
        self._run_git_cmd(["checkout", "-B", pr_branch, "FETCH_HEAD"], cwd=target_dir)

        # 5. 返回 Path 对象
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
        repo_dir = (self.base_cache_dir / f"{owner}__{repo}").resolve()
        base_dir = (self.base_cache_dir / f"{owner}__{repo}__base").resolve()

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
                    ["worktree", "remove", "--force", str(base_dir)], cwd=repo_dir
                )
            except RepoCommandError:
                # 目录存在但未注册为 worktree，直接清理后 prune
                shutil.rmtree(base_dir, ignore_errors=True)
                self._run_git_cmd(["worktree", "prune"], cwd=repo_dir)

        self._run_git_cmd(
            ["worktree", "add", "--detach", str(base_dir), base_sha], cwd=repo_dir
        )
        logger.debug("Base worktree ready at %s (%s)", base_dir, base_sha)
        return base_dir