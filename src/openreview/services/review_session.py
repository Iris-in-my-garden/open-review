"""Run-scoped review session.

A pipeline can be reused (evaluation loops, batch review). Everything that is
mutable per run -- budgeted LLM client, reviewer, verifier, retrieval stats,
run id and executor -- belongs to a :class:`ReviewSession`, not to the
long-lived facade. Concurrent runs therefore cannot share a budget ledger or
overwrite each other's ``last_*`` results.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from openreview.repo.local import LocalReviewInput
from openreview.review.workflow import ReviewState
from openreview.services.review_executor import ReviewExecutor


@dataclass
class ReviewSession:
    run_id: str
    executor: ReviewExecutor
    budgeted_client: Any
    llm_client: Any
    reviewer: Any
    verifier: Any
    retriever: Any

    @classmethod
    def new_run_id(cls) -> str:
        return uuid.uuid4().hex

    def new_state(
        self,
        pr_url: str,
        output_dir: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        post_comments: bool = False,
        *,
        local_mode: bool = False,
        local_input: Optional[LocalReviewInput] = None,
    ) -> ReviewState:
        state = self.executor.new_state(
            pr_url=pr_url,
            output_dir=output_dir,
            progress_cb=progress_cb,
            post_comments=post_comments,
            local_mode=local_mode,
            local_input=local_input,
            run_id=self.run_id,
        )
        return state

    def execute(self, state: ReviewState) -> Path:
        return self.executor.execute(state)
