from pathlib import Path

import pytest

from openreview.review.workflow import (
    BoundedWorkflow,
    BudgetExceeded,
    ReviewBudget,
    ReviewState,
    WorkflowNode,
)


def _state(tmp_path: Path) -> ReviewState:
    return ReviewState(pr_url="https://github.com/org/repo/pull/1", output_dir=tmp_path)


def test_bounded_workflow_skips_disabled_nodes(tmp_path: Path):
    calls = []
    workflow = BoundedWorkflow(
        [
            WorkflowNode("first", lambda state: calls.append("first")),
            WorkflowNode(
                "second",
                lambda state: calls.append("second"),
                enabled=lambda state: False,
            ),
            WorkflowNode("third", lambda state: calls.append("third")),
        ]
    )

    state = workflow.run(_state(tmp_path))

    assert calls == ["first", "third"]
    assert [event.status for event in state.trace] == ["ok", "skipped", "ok"]


def test_bounded_workflow_records_error_and_reraises(tmp_path: Path):
    def boom(state):
        raise ValueError("bad")

    workflow = BoundedWorkflow([WorkflowNode("boom", boom)])

    with pytest.raises(ValueError, match="bad"):
        workflow.run(_state(tmp_path))


def test_bounded_workflow_rejects_too_many_nodes(tmp_path: Path):
    workflow = BoundedWorkflow(
        [WorkflowNode(f"n{index}", lambda state: None) for index in range(3)],
        max_steps=2,
    )

    with pytest.raises(RuntimeError, match="max_steps"):
        workflow.run(_state(tmp_path))


def test_budget_enforces_calls_tokens_and_time():
    budget = ReviewBudget(max_llm_calls=2, max_input_tokens=100, max_seconds=0)
    assert budget.allow(calls=2, tokens=100)
    assert not budget.allow(calls=3)
    assert not budget.allow(tokens=101)

    budget.consume(calls=2, tokens=100)
    assert budget.remaining_calls() == 0
    assert budget.remaining_tokens() == 0
    with pytest.raises(BudgetExceeded):
        budget.require(calls=1)


def test_budget_zero_means_unlimited():
    budget = ReviewBudget()
    assert budget.remaining_calls() is None
    assert budget.remaining_tokens() is None
    assert budget.allow(calls=10_000, tokens=10_000_000)
