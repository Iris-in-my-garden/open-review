"""使用 GitHub REST API 拉取公开 PR 信息，并可选地发表行内 Review 评论。"""
import logging
import os
import time
from typing import Dict, List, Optional

import httpx

from openreview.domain.markers import REVIEW_MARKER
from openreview.domain.models import ChangedFile, PullRequestInfo
from openreview.github.pr_url import GitHubPRRef

logger = logging.getLogger(__name__)

__all__ = [
    "ChangedFile",
    "GitHubAPIError",
    "GitHubClient",
    "PullRequestInfo",
]

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 8.0
MAX_RATE_LIMIT_WAIT_SECONDS = 30.0
# 大 PR 的 files?per_page=100 响应可能包含大量 patch，httpx 默认 5s 容易超时。
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


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
            timeout=httpx.Timeout(
                DEFAULT_REQUEST_TIMEOUT_SECONDS,
                connect=DEFAULT_CONNECT_TIMEOUT_SECONDS,
            ),
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

    def _request(
        self, method: str, url: str, *, retry: bool = True, **kwargs
    ) -> httpx.Response:
        """带指数退避与限流处理的请求封装。

        ``retry=False`` is used for non-idempotent writes (creating a review).
        Retrying a POST after a 5xx can create duplicate comments when the
        server committed the request but the response was lost. The caller
        can safely rerun because ``upsert_review`` replaces its old review.
        """
        attempts = self.max_retries + 1 if retry else 1
        for attempt in range(attempts):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= attempts - 1:
                    raise GitHubAPIError(f"GitHub request failed: {exc}") from exc
                delay = min(MAX_BACKOFF_SECONDS, self.backoff_factor * (2 ** attempt))
                logger.warning(
                    "GitHub request error (attempt %d/%d), retry in %.1fs: %s",
                    attempt + 1,
                    attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue

            if attempt < attempts - 1 and (
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
                    attempts,
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

        response = self._request("POST", url, retry=False, json=payload)
        if response.status_code not in (200, 201):
            raise GitHubAPIError(
                f"GitHub API error [{response.status_code}]: {response.text}"
            )
        return response.json()

    def list_reviews(self, ref: GitHubPRRef, per_page: int = 100) -> List[Dict]:
        """List reviews for a PR, paginated.

        Used to find the previous managed review before creating a new one.
        """
        reviews: List[Dict] = []
        page = 1
        while True:
            url = f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.pr_number}/reviews"
            response = self._request(
                "GET", url, params={"page": page, "per_page": per_page}
            )
            if response.status_code != 200:
                raise GitHubAPIError(
                    f"GitHub API error [{response.status_code}]: {response.text}"
                )
            data = response.json()
            if not data:
                break
            reviews.extend(data)
            if len(data) < per_page:
                break
            page += 1
        return reviews

    def delete_review(self, ref: GitHubPRRef, review_id: int) -> None:
        """Delete one review and its inline comments.

        A 404 is treated as success: because deletion is idempotent, this makes
        retries and concurrent runs converge instead of failing repeatedly.
        """
        url = (
            f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.pr_number}"
            f"/reviews/{review_id}"
        )
        response = self._request("DELETE", url)
        if response.status_code not in (200, 204, 404):
            raise GitHubAPIError(
                f"GitHub API error [{response.status_code}]: {response.text}"
            )

    def upsert_review(
        self,
        ref: GitHubPRRef,
        body: str,
        comments: List[Dict],
        event: str = "COMMENT",
        commit_id: Optional[str] = None,
    ) -> Dict:
        """Replace the previous managed review, then create a fresh one.

        This is the idempotent path used by the pipeline. It deliberately
        deletes all managed reviews (identified by ``REVIEW_MARKER``), including
        reviews on older commits, so a force-push does not leave stale comments
        pointing at lines that no longer exist.
        """
        managed = [
            review
            for review in self.list_reviews(ref)
            if REVIEW_MARKER in str(review.get("body") or "")
        ]
        deleted = 0
        for review in managed:
            review_id = review.get("id")
            if isinstance(review_id, int):
                self.delete_review(ref, review_id)
                deleted += 1

        created = self.create_review(
            ref,
            body=body,
            comments=comments,
            event=event,
            commit_id=commit_id,
        )
        return {"review": created, "deleted": deleted}
