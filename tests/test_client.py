import json
import time

import httpx
import pytest

from openreview.github.client import GitHubAPIError, GitHubClient, GitHubPRRef


def _client_with_handler(handler, token: str | None = None) -> GitHubClient:
    """用 httpx.MockTransport 替代真实网络，避免引入 respx 依赖。"""
    return GitHubClient(token=token, transport=httpx.MockTransport(handler))


def test_client_sets_explicit_request_timeout():
    """大 PR 的 patch 响应可能很慢，不能依赖 httpx 的 5 秒默认值。"""
    with _client_with_handler(lambda request: httpx.Response(200, json={})) as client:
        timeout = client.client.timeout

    assert timeout.connect == 10.0
    assert timeout.read == 30.0
    assert timeout.write == 30.0
    assert timeout.pool == 30.0


def test_get_pull_request_success():
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1342)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octocat/Hello-World/pulls/1342"
        return httpx.Response(
            200,
            json={
                "title": "Add new feature",
                "body": "Description of the feature",
                "base": {"ref": "main", "sha": "base_sha_abc"},
                "head": {"ref": "feature", "sha": "head_sha_xyz"},
                "html_url": "https://github.com/octocat/Hello-World/pull/1342",
            },
        )

    with _client_with_handler(handler, token="test-token") as client:
        pr_info = client.get_pull_request(ref)

    assert pr_info.title == "Add new feature"
    assert pr_info.base_branch == "main"
    assert pr_info.head_branch == "feature"
    assert pr_info.base_sha == "base_sha_abc"
    assert pr_info.head_sha == "head_sha_xyz"
    assert pr_info.html_url == "https://github.com/octocat/Hello-World/pull/1342"


def test_get_pull_request_api_error():
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=999)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with _client_with_handler(handler) as client:
        with pytest.raises(GitHubAPIError):
            client.get_pull_request(ref)


def test_list_changed_files_pagination():
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1342)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octocat/Hello-World/pulls/1342/files"
        assert request.url.params["page"] == "1"
        assert request.url.params["per_page"] == "100"
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "main.py",
                    "status": "modified",
                    "additions": 5,
                    "deletions": 2,
                    "changes": 7,
                    "patch": "@@ -1,3 +1,6 @@",
                }
            ],
        )

    with _client_with_handler(handler) as client:
        files = client.list_changed_files(ref)

    assert len(files) == 1
    assert files[0].filename == "main.py"
    assert files[0].additions == 5
    assert files[0].deletions == 2
    assert files[0].changes == 7
    assert files[0].patch == "@@ -1,3 +1,6 @@"


def test_create_review_posts_body_and_inline_comments():
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1342)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/octocat/Hello-World/pulls/1342/reviews"
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 99})

    comments = [
        {"path": "main.py", "line": 12, "side": "RIGHT", "body": "issue"}
    ]
    with _client_with_handler(handler) as client:
        response = client.create_review(
            ref,
            body="review body",
            comments=comments,
            event="COMMENT",
            commit_id="head-sha",
        )

    assert response == {"id": 99}
    payload = captured["payload"]
    assert payload["body"] == "review body"
    assert payload["event"] == "COMMENT"
    assert payload["comments"] == comments
    assert payload["commit_id"] == "head-sha"


def test_retries_on_retryable_status():
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, json={"message": "temporary"})
        return httpx.Response(
            200,
            json={
                "title": "ok",
                "body": "",
                "base": {"ref": "main", "sha": "base"},
                "head": {"ref": "feature", "sha": "head"},
                "html_url": "https://github.com/octocat/Hello-World/pull/1",
            },
        )

    with GitHubClient(
        transport=httpx.MockTransport(handler),
        max_retries=1,
        backoff_factor=0,
    ) as client:
        pr_info = client.get_pull_request(ref)

    assert pr_info.title == "ok"
    assert len(calls) == 2


def test_retries_on_rate_limit_exhausted():
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time())),
                },
                json={"message": "rate limited"},
            )
        return httpx.Response(
            200,
            json={
                "title": "ok",
                "body": "",
                "base": {"ref": "main", "sha": "base"},
                "head": {"ref": "feature", "sha": "head"},
                "html_url": "https://github.com/octocat/Hello-World/pull/1",
            },
        )

    with GitHubClient(
        transport=httpx.MockTransport(handler),
        max_retries=1,
        backoff_factor=0,
    ) as client:
        pr_info = client.get_pull_request(ref)

    assert pr_info.title == "ok"
    assert len(calls) == 2


def test_create_review_does_not_retry_post_on_transient_error():
    """POST review 非幂等：5xx 后重试可能产生重复行内评论。"""
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"message": "temporary"})

    with GitHubClient(
        transport=httpx.MockTransport(handler),
        max_retries=3,
        backoff_factor=0,
    ) as client:
        with pytest.raises(GitHubAPIError, match="500"):
            client.create_review(ref, body="body", comments=[])

    assert len(calls) == 1


def test_upsert_review_deletes_only_managed_reviews_then_creates():
    from openreview.domain.markers import REVIEW_MARKER

    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1)
    deleted = []
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "body": f"old managed\n{REVIEW_MARKER}"},
                    {"id": 2, "body": "human review"},
                    {"id": 3, "body": f"older managed\n{REVIEW_MARKER}"},
                ],
            )
        if request.method == "DELETE" and "/reviews/" in path:
            deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        if request.method == "POST" and path.endswith("/reviews"):
            posts.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 99})
        raise AssertionError(f"unexpected {request.method} {path}")

    with GitHubClient(transport=httpx.MockTransport(handler)) as client:
        result = client.upsert_review(
            ref, body="new body", comments=[], commit_id="head-sha"
        )

    assert result == {"review": {"id": 99}, "deleted": 2}
    assert deleted == ["1", "3"]
    assert posts[0]["body"] == "new body"
    assert posts[0]["commit_id"] == "head-sha"


def test_delete_review_treats_missing_review_as_success():
    """并发/重跑时另一进程可能已删除；404 应视为已收敛。"""
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(404, json={"message": "Not Found"})

    with GitHubClient(transport=httpx.MockTransport(handler)) as client:
        client.delete_review(ref, 123)
