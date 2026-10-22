"""使用 GitHub REST API 拉取公开 PR 信息，并可选地发表行内 Review 评论。"""
import logging
import os
import time
from typing import Dict, List, Optional

import httpx
from pydantic import BaseModel

from openreview.github.pr_url import GitHubPRRef

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 8.0
MAX_RATE_LIMIT_WAIT_SECONDS = 30.0


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
        max_retries: int = 2,
        backoff_factor: float = 0.5,
    ):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.max_retries = max(0, max_retries)
        self.backoff_factor = max(0.0, backoff_factor)
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

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """带指数退避与限流处理的请求封装。"""
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= self.max_retries:
                    raise GitHubAPIError(f"GitHub request failed: {exc}") from exc
                delay = min(MAX_BACKOFF_SECONDS, self.backoff_factor * (2 ** attempt))
                logger.warning(
                    "GitHub request error (attempt %d/%d), retry in %.1fs: %s",
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue

            if attempt < self.max_retries and (
                response.status_code in RETRYABLE_STATUS
                or (
                    response.status_code == 403
                    and response.headers.get("X-RateLimit-Remaining") == "0"
                )
            ):
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "GitHub API %d (attempt %d/%d), retry in %.1fs",
                    response.status_code,
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                )
                time.sleep(delay)
                continue

            return response

        raise GitHubAPIError("GitHub request failed after retries.")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(MAX_RATE_LIMIT_WAIT_SECONDS, max(0.0, float(retry_after)))
            except ValueError:
                pass

        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset:
            try:
                wait = int(reset) - int(time.time())
                return min(MAX_RATE_LIMIT_WAIT_SECONDS, max(0.0, float(wait)))
            except ValueError:
                pass

        return min(MAX_BACKOFF_SECONDS, self.backoff_factor * (2 ** attempt))

    def get_pull_request(self, ref: GitHubPRRef) -> PullRequestInfo:
        url = f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.pr_number}"
        response = self._request("GET", url)
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
            response = self._request("GET", url, params=params)
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

    def create_review(
        self,
        ref: GitHubPRRef,
        body: str,
        comments: List[Dict],
        event: str = "COMMENT",
        commit_id: Optional[str] = None,
    ) -> Dict:
        """发表一次 PR Review，可携带行内评论。"""
        if event not in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}:
            raise ValueError(f"Unsupported review event: {event}")

        url = f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.pr_number}/reviews"
        payload: Dict = {
            "body": body,
            "event": event,
            "comments": comments,
        }
        if commit_id:
            payload["commit_id"] = commit_id

        response = self._request("POST", url, json=payload)
        if response.status_code not in (200, 201):
            raise GitHubAPIError(
                f"GitHub API error [{response.status_code}]: {response.text}"
            )
        return response.json()
