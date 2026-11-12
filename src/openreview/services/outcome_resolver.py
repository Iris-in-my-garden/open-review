"""Map workflow state to a terminal outcome."""

from __future__ import annotations

from openreview.review.workflow import (
    ReviewOutcome,
    ReviewState,
    ReviewStatus,
)


class OutcomeResolver:
    def resolve(self, state: ReviewState, *, failed: bool = False) -> ReviewOutcome:
        if failed:
            status = ReviewStatus.FAILED
        elif state.skipped_llm:
            status = ReviewStatus.SKIPPED
        elif state.failed_batches or state.budget_exhausted:
            status = ReviewStatus.PARTIAL
        else:
            status = ReviewStatus.COMPLETED
        return ReviewOutcome(
            status=status,
            report_path=state.report_path,
            failed_batches=state.failed_batches,
            total_batches=len(state.batches),
            budget_exhausted=state.budget_exhausted,
            degraded_reasons=list(state.degraded_reasons),
        )
