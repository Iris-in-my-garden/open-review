"""Batch planning with explicit optional retriever capabilities."""

from __future__ import annotations

import logging
from typing import Dict, Set

from openreview.repo.diff_parser import ParsedDiff
from openreview.review.config import ReviewConfig
from openreview.review.token_budget import (
    context_reserve_tokens_for,
    plan_review_batches,
)
from openreview.review.workflow import ReviewState

logger = logging.getLogger(__name__)


class BatchPlanner:
    def __init__(self, config: ReviewConfig):
        self.config = config

    def plan(self, state: ReviewState, retriever) -> list[ParsedDiff]:
        if state.parsed_diff is None:
            return []
        return plan_review_batches(
            state.parsed_diff,
            soft_prompt_tokens=self.config.soft_prompt_tokens,
            hard_prompt_tokens=self.config.hard_prompt_tokens,
            max_batch_tokens=self.config.max_batch_tokens,
            context_reserve_tokens=context_reserve_tokens_for(
                self.config.soft_prompt_tokens
            ),
            dependencies=self._dependencies(state, retriever),
        )

    def _dependencies(self, state: ReviewState, retriever) -> Dict[str, Set[str]]:
        """获取文件依赖关系，用于 dependency-aware batching。

        retriever 不提供该能力时返回空映射，分批退化为原始 diff 顺序。
        """
        getter = getattr(retriever, "related_files", None)
        if (
            not callable(getter)
            or state.parsed_diff is None
            or state.local_repo_path is None
        ):
            return {}
        try:
            related = getter(state.local_repo_path, state.parsed_diff)
        except Exception as exc:  # noqa: BLE001 - 依赖分析失败不应阻断 review
            logger.debug("Dependency-aware batching disabled: %s", exc)
            return {}
        return dict(related or {})
