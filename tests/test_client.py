import pytest
from github.client import GitHubClient, GitHubPRRef, GitHubAPIError


def test_get_pull_request_success(respx_mock):
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1342)
    url = "https://api.github.com/repos/octocat/Hello-World/pulls/1342"

    respx_mock.get(url).respond(
        200,
        json={
            "title": "Add new feature",
            "body": "Description of the feature",
            "base": {"ref": "main", "sha": "base_sha_abc"},
            "head": {"ref": "feature", "sha": "head_sha_xyz"},
            "html_url": "https://github.com/octocat/Hello-World/pull/1342",
        },
    )

    client = GitHubClient(token="test-token")
    pr_info = client.get_pull_request(ref)

    assert pr_info.title == "Add new feature"
    assert pr_info.base_branch == "main"
    assert pr_info.head_branch == "feature"
    assert pr_info.base_sha == "base_sha_abc"
    assert pr_info.head_sha == "head_sha_xyz"
    assert pr_info.html_url == "https://github.com/octocat/Hello-World/pull/1342"


def test_get_pull_request_api_error(respx_mock):
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=999)
    url = "https://api.github.com/repos/octocat/Hello-World/pulls/999"

    respx_mock.get(url).respond(404, json={"message": "Not Found"})

    client = GitHubClient()
    with pytest.raises(GitHubAPIError):
        client.get_pull_request(ref)


def test_list_changed_files_pagination(respx_mock):
    ref = GitHubPRRef(owner="octocat", repo="Hello-World", pr_number=1342)
    url = "https://api.github.com/repos/octocat/Hello-World/pulls/1342/files"

    # Mock page 1 (returns 1 item, assuming per_page < 100 or final page)
    respx_mock.get(url, params={"page": 1, "per_page": 100}).respond(
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

    client = GitHubClient()
    files = client.list_changed_files(ref)

    assert len(files) == 1
    assert files[0].filename == "main.py"
    assert files[0].additions == 5
    assert files[0].deletions == 2
    assert files[0].changes == 7
    assert files[0].patch == "@@ -1,3 +1,6 @@"