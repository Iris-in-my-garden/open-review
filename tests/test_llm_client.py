import sys
import threading
from unittest.mock import patch

import pytest

from openreview.review.llm_client import LLMClient, MockLLMClient
from openreview.review.output_parser import AspectReviewOutputSchema
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
