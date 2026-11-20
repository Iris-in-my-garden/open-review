import concurrent.futures
import sys
import threading
from unittest.mock import patch

import pytest

from openreview.review.llm_client import (
    BudgetedLLMClient,
    LLMClient,
    MockLLMClient,
)
from openreview.review.output_parser import AspectReviewOutputSchema
from openreview.review.workflow import ReviewBudget
from openreview.services.review_service import ReviewPipeline


def test_llm_client_requires_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        LLMClient()


def test_llm_client_reads_model_and_base_url_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example.com/v1")

    with patch("openreview.review.llm_client.OpenAI") as mock_openai:
        client = LLMClient()

    kwargs = mock_openai.call_args.kwargs
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["base_url"] == "https://gateway.example.com/v1"
    assert client.model == "custom-model"


def test_pipeline_does_not_reuse_github_token_as_llm_key():
    with (
        patch("openreview.services.review_service.GitHubClient") as mock_github,
        patch("openreview.services.review_service.LLMClient") as mock_llm,
    ):
        ReviewPipeline(github_token="ghp_github_token")

    # GitHub Token 只能传给 GitHubClient，不能传给 LLMClient
    assert mock_github.call_args.kwargs["token"] == "ghp_github_token"
    assert mock_llm.call_args.kwargs["api_key"] is None


def test_mock_client_usage_increment_is_thread_safe():
    client = MockLLMClient()
    workers = 8
    calls_per_worker = 100
    barrier = threading.Barrier(workers)
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def worker() -> None:
        barrier.wait()
        for _ in range(calls_per_worker):
            client.generate_structured("sys", "", AspectReviewOutputSchema)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(old_interval)

    assert client.usage["calls"] == workers * calls_per_worker


def test_llm_client_records_per_call_log_and_output_cap(monkeypatch):
    from types import SimpleNamespace

    from pydantic import BaseModel

    class Output(BaseModel):
        value: int

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch("openreview.review.llm_client.OpenAI") as mock_openai:
        client = LLMClient(max_completion_tokens=256)
        parse = mock_openai.return_value.beta.chat.completions.parse
        parse.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        refusal=None, parsed=Output(value=1)
                    )
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            ),
        )

        result = client.generate_structured("sys", "user", Output)

    assert result.value == 1
    assert parse.call_args.kwargs["max_completion_tokens"] == 256
    assert client.last_usage == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }
    assert len(client.call_log) == 1
    detail = client.call_log[0]
    assert detail["model"] == client.model
    assert detail["prompt_tokens"] == 100
    assert detail["completion_tokens"] == 20
    assert detail["total_tokens"] == 120
    assert detail["latency_ms"] >= 0


def test_budgeted_request_caps_output_and_tags_run(monkeypatch):
    from types import SimpleNamespace

    from pydantic import BaseModel

    class Output(BaseModel):
        value: int

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch("openreview.review.llm_client.OpenAI") as mock_openai:
        client = LLMClient(max_completion_tokens=200, max_retries=2)
        parse = mock_openai.return_value.beta.chat.completions.parse
        parse.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(refusal=None, parsed=Output(value=1))
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=40,
                completion_tokens=20,
                total_tokens=60,
            ),
        )
        budget = ReviewBudget(
            max_input_tokens=10_000,
            max_output_tokens=50,
            max_seconds=30,
        )
        budgeted = BudgetedLLMClient(client, budget, run_id="run-a")

        assert budgeted.generate_structured("sys", "user", Output).value == 1

    request = parse.call_args.kwargs
    assert request["max_completion_tokens"] == 50
    assert request["timeout"] <= 30
    assert mock_openai.call_args.kwargs["max_retries"] == 0
    assert client.call_log[0]["run_id"] == "run-a"
    assert client.call_log[0]["attempt"] == 1
    assert budget.used_output_tokens == 20
    assert budget.reserved_output_tokens == 0


def test_shared_provider_logs_and_budgets_are_isolated_across_runs(monkeypatch):
    from types import SimpleNamespace

    from pydantic import BaseModel

    class Output(BaseModel):
        value: int

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    barrier = threading.Barrier(2)
    with patch("openreview.review.llm_client.OpenAI") as mock_openai:
        client = LLMClient()

        def parse(**request):
            user_prompt = request["messages"][1]["content"]
            barrier.wait(timeout=2)
            prompt_tokens = 10 if user_prompt == "a" else 30
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            refusal=None, parsed=Output(value=prompt_tokens)
                        )
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=1,
                    total_tokens=prompt_tokens + 1,
                ),
            )

        mock_openai.return_value.beta.chat.completions.parse.side_effect = parse
        budgets = [
            ReviewBudget(max_input_tokens=1_000),
            ReviewBudget(max_input_tokens=1_000),
        ]
        clients = [
            BudgetedLLMClient(client, budgets[0], run_id="run-a"),
            BudgetedLLMClient(client, budgets[1], run_id="run-b"),
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: pair[0].generate_structured(
                        "sys", pair[1], Output
                    ),
                    zip(clients, ("a", "b"), strict=True),
                )
            )

    assert [result.value for result in results] == [10, 30]
    assert {entry["run_id"] for entry in client.call_log} == {"run-a", "run-b"}
    assert {
        entry["run_id"]: entry["prompt_tokens"] for entry in client.call_log
    } == {"run-a": 10, "run-b": 30}
    assert budgets[0].used_input_tokens == 10
    assert budgets[1].used_input_tokens == 30
