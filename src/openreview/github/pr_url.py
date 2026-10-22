"""将GitHub Pull Request URL 解析为结构化"""
import re

from pydantic import BaseModel

PR_URL_PATTERN = re.compile(
    r"""^
    https?://(?:www\.)?github\.com/
    (?P<owner>[a-zA-Z0-9][-a-zA-Z0-9]*)/
    (?P<repo>[\w.-]+)/
    pull/
    (?P<pr_number>\d+)
    /?
    $""",
    re.VERBOSE
)

class GitHubPRRef(BaseModel):
    owner: str
    repo: str
    pr_number: int

def parse_pr_url(url: str) -> GitHubPRRef:
    match = PR_URL_PATTERN.match(url.strip())
    if match is None:
        raise ValueError(f"Invalid PR URL: {url}")
    return GitHubPRRef(
        owner=match.group("owner"),
        repo=match.group("repo"),
        pr_number=int(match.group("pr_number")),
    )

