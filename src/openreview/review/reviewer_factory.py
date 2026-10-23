"""Reviewer 工厂：multi 为四维度并行，single 为单 Prompt 基线。"""

from pathlib import Path
from typing import List, Optional, Protocol

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.finding_validator import FindingValidator
from openreview.review.llm_client import AbstractLLMClient
from openreview.review.output_parser import OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.report_writer import ReviewResult
from openreview.review.simple_reviewer import Reviewer

REVIEWER_NAMES = ("multi", "single")


class ReviewerProtocol(Protocol):
    aspects: List[str]

    def review(
        self,
        pr_info: PullRequestInfo,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext],
        repo_path: Optional[Path] = None,
        aspects: Optional[List[str]] = None,
    ) -> ReviewResult:
        ...


def build_reviewer(
    mode: str = "multi",
    *,
    prompt_builder: PromptBuilder,
    llm_client: AbstractLLMClient,
    output_parser: OutputParser,
    validator: FindingValidator,
    max_workers: int = 4,
    aspect_timeout: float = 90.0,
) -> ReviewerProtocol:
    normalized = (mode or "multi").strip().lower()
    if normalized in {"multi", "orchestrator", "aspect"}:
        return ReviewOrchestrator(
            prompt_builder=prompt_builder,
            llm_client=llm_client,
            output_parser=output_parser,
            validator=validator,
            max_workers=max_workers,
            aspect_timeout=aspect_timeout,
        )
    if normalized in {"single", "simple", "single_prompt"}:
        return Reviewer(
            prompt_builder=prompt_builder,
            llm_client=llm_client,
            output_parser=output_parser,
            validator=validator,
        )
    raise ValueError(
        f"Unknown reviewer: {mode!r}. Expected one of {REVIEWER_NAMES}."
    )
