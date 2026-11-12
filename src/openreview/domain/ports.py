"""Explicit ports for the service layer.

The executor used to discover optional capabilities with ad-hoc ``getattr``
checks. These Protocols document the required surface and the optional
capability protocols make the fallback paths explicit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Type, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from openreview.domain.models import ChangedFile, PullRequestInfo, ReviewResult
    from openreview.github.pr_url import GitHubPRRef
    from openreview.repo.diff_parser import ParsedDiff
    from openreview.retriever.code_context import CodeContext
    from openreview.review.workflow import ReviewBudget

T = TypeVar("T", bound=BaseModel)


class LLMClientPort(Protocol):
    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T: ...


class GitHubPort(Protocol):
    def get_pull_request(self, ref: "GitHubPRRef") -> "PullRequestInfo": ...

    def list_changed_files(self, ref: "GitHubPRRef") -> List["ChangedFile"]: ...

    def create_review(
        self,
        ref: "GitHubPRRef",
        body: str,
        comments: List[Dict],
        event: str = "COMMENT",
        commit_id: Optional[str] = None,
    ) -> Dict: ...


class RetrieverPort(Protocol):
    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: "ParsedDiff",
        base_path: Optional[Path] = None,
    ) -> List["CodeContext"]: ...


class DependencyAwareRetriever(Protocol):
    def related_files(
        self,
        repo_path: Path,
        parsed_diff: "ParsedDiff",
        base_path: Optional[Path] = None,
    ) -> Dict[str, set[str]]: ...


class ReviewerPort(Protocol):
    def review(
        self,
        pr_info: "PullRequestInfo",
        parsed_diff: "ParsedDiff",
        contexts: List["CodeContext"],
        repo_path: Optional[Path] = None,
        aspects: Optional[List[str]] = None,
    ) -> "ReviewResult": ...


class VerifierPort(Protocol):
    def verify(
        self,
        result: "ReviewResult",
        parsed_diff: "ParsedDiff",
        contexts: List["CodeContext"],
        repo_path: Optional[Path] = None,
        budget: Optional["ReviewBudget"] = None,
    ) -> tuple["ReviewResult", Any]: ...


class RepoManagerPort(Protocol):
    def prepare_pr_repo(self, owner: str, repo: str, pr_number: int) -> Path: ...

    def prepare_base_worktree(
        self, owner: str, repo: str, base_sha: str
    ) -> Path: ...


class WorktreeRepoManager(Protocol):
    def prepare_pr_worktree(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        head_sha: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Path: ...

    def release_worktree(self, owner: str, repo: str, path: Path) -> None: ...


class ReviewCachePort(Protocol):
    enabled: bool

    def get(self, key: str) -> Optional["ReviewResult"]: ...

    def set(self, key: str, result: "ReviewResult") -> None: ...
