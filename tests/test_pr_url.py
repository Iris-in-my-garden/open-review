import pytest

from openreview.github.pr_url import parse_pr_url


def test_parse_pr_url_success():
    valid_cases = [
        (
            "http://github.com/octocat/Hello-World/pull/1342/",
            {"owner": "octocat", "repo": "Hello-World", "pr_number": 1342},
        ),
        (
            "https://www.github.com/octocat/repo-name.js/pull/1",
            {"owner": "octocat", "repo": "repo-name.js", "pr_number": 1},
        ),
        (
            "https://github.com/my-owner-123/my_repo/pull/9999",
            {"owner": "my-owner-123", "repo": "my_repo", "pr_number": 9999},
        ),
    ]
    for url, expected in valid_cases:
        ref = parse_pr_url(url)
        assert ref.owner == expected["owner"]
        assert ref.repo == expected["repo"]
        assert ref.pr_number == expected["pr_number"]

def test_parse_pr_url_invalid():
    invalid_urls = [
        "https://github.com/octocat/Hello-World/issues/1342",  # wrong path
        "https://github.com/octocat/Hello-World/pull/abc",     # non-numeric pr_number
        "https://gitlab.com/octocat/Hello-World/pull/1342",    # wrong domain
        "https://github.com//Hello-World/pull/1342",           # missing owner
        "not-a-url",                                           # malformed string
    ]
    for url in invalid_urls:
        with pytest.raises(ValueError):
            parse_pr_url(url)