from __future__ import annotations

import concurrent.futures
import threading
from typing import Type, TypeVar

import pytest
from pydantic import BaseModel

from openreview.review.llm_client import BudgetedLLMClient
from openreview.review.workflow import BudgetExceeded, ReviewBudget

T = TypeVar("T", bound=BaseModel)


class Output(BaseModel):
    value: int


class FakeClient:
    def __init__(self):
        self.model = "fake"
        self.temperature = 0.0
        self.seed = 7
        self.usage = {"prompt_tokens": 3, "completion_tokens": 1}
        self.calls = 0

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        self.calls += 1
        return response_schema(value=1)  # type: ignore[call-arg]


def test_budgeted_client_enforces_hard_call_limit_across_retries():
    inner = FakeClient()
    budget = ReviewBudget(max_llm_calls=1)
    client = BudgetedLLMClient(inner, budget)

    assert client.generate_structured("sys", "user", Output).value == 1
    with pytest.raises(BudgetExceeded):
        client.generate_structured("sys", "user", Output)

    # 第二次调用在进入 provider 前就被拦截，不会产生真实费用。
    assert inner.calls == 1
    assert budget.used_llm_calls == 1
    assert client.temperature == 0.0
    assert client.seed == 7


def test_budgeted_client_enforces_token_budget_before_call():
    inner = FakeClient()
    budget = ReviewBudget(max_input_tokens=1)
    client = BudgetedLLMClient(inner, budget)

    with pytest.raises(BudgetExceeded):
        client.generate_structured("system prompt", "user prompt", Output)

    assert inner.calls == 0
    assert budget.used_input_tokens == 0


def test_budgeted_client_can_be_rebound_per_run():
    inner = FakeClient()
    client = BudgetedLLMClient(inner)
    first = ReviewBudget(max_llm_calls=1)
    second = ReviewBudget(max_llm_calls=1)

    client.bind_budget(first)
    client.generate_structured("sys", "user", Output)
    with pytest.raises(BudgetExceeded):
        client.generate_structured("sys", "user", Output)

    client.bind_budget(second)
    client.generate_structured("sys", "user", Output)
    assert first.used_llm_calls == 1
    assert second.used_llm_calls == 1


def test_budget_reserve_is_atomic_under_concurrency():
    """reserve() 必须把 check + consume 放在同一个临界区。

    否则 4 个 aspect 线程可能同时看到“还剩 2 次调用”，然后各扣一次，
    最终 used_llm_calls 超过 max_llm_calls。
    """
    budget = ReviewBudget(max_llm_calls=2)
    workers = 16
    barrier = threading.Barrier(workers)
    outcomes: list[bool] = []
    outcomes_lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            budget.reserve(calls=1)
        except BudgetExceeded:
            with outcomes_lock:
                outcomes.append(False)
        else:
            with outcomes_lock:
                outcomes.append(True)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(True) == 2
    assert budget.used_llm_calls == 2


def test_budgeted_client_concurrent_calls_never_exceed_limit():
    inner = FakeClient()
    budget = ReviewBudget(max_llm_calls=2)
    client = BudgetedLLMClient(inner, budget)
    workers = 8
    barrier = threading.Barrier(workers)

    def call() -> bool:
        barrier.wait()
        try:
            client.generate_structured("sys", "user", Output)
        except BudgetExceeded:
            return False
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(call) for _ in range(workers)]
        results = [future.result() for future in futures]

    assert results.count(True) == 2
    assert inner.calls == 2
    assert budget.used_llm_calls == 2
