""" 给定owner、repo、pr_number，把Github仓库clone到本地缓存目录，并且checkout到pr分支"""

from pathlib import Path
import subprocess


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
        else:
            # 2. 目录已存在，执行 fetch origin
            self._run_git_cmd(["fetch", "origin"], cwd=target_dir)

        # 3. Fetch 指定 PR 的 head
        pr_branch = f"pr-{pr_number}"
        fetch_ref = f"pull/{pr_number}/head:{pr_branch}"
        self._run_git_cmd(["fetch", "origin", fetch_ref], cwd=target_dir)

        # 4. Checkout 到 PR 分支
        self._run_git_cmd(["checkout", pr_branch], cwd=target_dir)

        # 5. 返回 Path 对象
        return target_dir.resolve()