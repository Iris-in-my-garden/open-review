""" 使用 GitHub REST API 拉取公开 PR 信息 """
import os
from typing import List, Optional
import httpx
from pydantic import BaseModel

from openreview.github.pr_url import GitHubPRRef


class PullRequestInfo(BaseModel):
    """ 基本元数据信息"""
    owner: str
    repo: str
    pr_number: int
    title: str
    body: Optional[str] = None
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    html_url: str

class ChangedFile(BaseModel):
    """ pr 相关的文件信息"""
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None

class GitHubAPIError(Exception):
    """Custom exception raised for GitHub API errors."""
    pass

class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers=self._get_headers(),
            transport=transport,
        )

    def _get_headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def close(self):
        """显式关闭底层 httpx 客户端"""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def get_pull_request(self, ref: GitHubPRRef) -> PullRequestInfo:
        url = f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.pr_number}"
        response = self.client.get(url)
        if response.status_code != 200:
            raise GitHubAPIError(
                f"GitHub API error [{response.status_code}]: {response.text}"
            )
        data = response.json()
        return PullRequestInfo(
            owner=ref.owner,
            repo=ref.repo,
            pr_number=ref.pr_number,
            title=data.get("title", ""),
            body=data.get("body"),
            base_branch=data["base"]["ref"],
            head_branch=data["head"]["ref"],
            base_sha=data["base"]["sha"],
            head_sha=data["head"]["sha"],
            html_url=data["html_url"],
        )

    def list_changed_files(self, ref: GitHubPRRef) -> List[ChangedFile]:
        files: List[ChangedFile] = []
        page = 1
        per_page = 100
        while True:
            url = f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.pr_number}/files"
            params = {"page": page, "per_page": per_page}
            response = self.client.get(url, params=params)
            if response.status_code != 200:
                raise GitHubAPIError(
                    f"GitHub API error [{response.status_code}]: {response.text}"
                )
            data = response.json()
            if not data:
                break
            for item in data:
                files.append(
                    ChangedFile(
                        filename=item["filename"],
                        status=item["status"],
                        additions=item["additions"],
                        deletions=item["deletions"],
                        changes=item["changes"],
                        patch=item.get("patch"),
                    )
                )
            if len(data) < per_page:
                break
            page += 1
        return files