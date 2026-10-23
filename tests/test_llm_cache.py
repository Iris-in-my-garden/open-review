from pathlib import Path
from typing import Type, TypeVar

import pytest
from pydantic import BaseModel

from openreview.review.llm_cache import FileLLMCacheClient, LLMCacheMiss

T = TypeVar("T", bound=BaseModel)


class Output(BaseModel):
    value: str


class FakeInner:
    model = "fake-model"

    def __init__(self):
        self.calls = 0

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        self.calls += 1
        return response_schema(value=f"call-{self.calls}")


def test_auto_mode_replays_recorded_response(tmp_path: Path):
    inner = FakeInner()
    client = FileLLMCacheClient(inner, tmp_path, mode="auto")

    first = client.generate_structured("sys", "user", Output)
    second = client.generate_structured("sys", "user", Output)

    assert first.value == "call-1"
    assert second.value == "call-1"
    assert inner.calls == 1
    assert client.hits == 1
    assert client.misses == 1


def test_prompt_change_produces_cache_miss(tmp_path: Path):
    inner = FakeInner()
    client = FileLLMCacheClient(inner, tmp_path, mode="auto")

    client.generate_structured("sys", "user-a", Output)
    result = client.generate_structured("sys", "user-b", Output)

    assert result.value == "call-2"
    assert inner.calls == 2


def test_replay_mode_raises_on_missing_record(tmp_path: Path):
    client = FileLLMCacheClient(FakeInner(), tmp_path, mode="replay")

    with pytest.raises(LLMCacheMiss):
        client.generate_structured("sys", "user", Output)


def test_model_name_is_forwarded_for_review_cache_keys(tmp_path: Path):
    client = FileLLMCacheClient(FakeInner(), tmp_path)

    assert client.model == "fake-model"
