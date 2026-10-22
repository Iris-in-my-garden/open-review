import httpx
import pytest

from openreview.github.client import GitHubClient, GitHubPRRef, GitHubAPIError


def _client_with_handler(handler, token: str | None = None) -> GitHubClient:
    """用 httpx.MockTransport 替代真实网络，避免引入 respx 依赖。"""
    return GitHubClient(token=token, transport=httpx.MockTransport(handler))


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
