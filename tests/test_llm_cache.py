import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


class SlowFakeInner(FakeInner):
    """模拟真实 provider：并发调用时能暴露出重复请求和计数竞争。"""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        with self._lock:
            self.calls += 1
            call_number = self.calls
        time.sleep(0.05)
        return response_schema(value=f"call-{call_number}")


def test_concurrent_same_key_calls_provider_once(tmp_path: Path):
    inner = SlowFakeInner()
    client = FileLLMCacheClient(inner, tmp_path, mode="auto")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: client.generate_structured("sys", "user", Output),
                range(8),
            )
        )

    # 只有拿到 per-key 锁的第一个线程会调用 provider，其余线程等待后
    # 读取刚写入的 cache，不能每个线程都打一次真实模型。
    assert inner.calls == 1
    assert client.misses == 1
    assert client.hits == 7
    assert {result.value for result in results} == {"call-1"}


def test_concurrent_distinct_keys_keep_counters_consistent(tmp_path: Path):
    inner = SlowFakeInner()
    client = FileLLMCacheClient(inner, tmp_path, mode="auto")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: client.generate_structured(
                    "sys", f"user-{index}", Output
                ),
                range(8),
            )
        )

    assert inner.calls == 8
    assert client.misses == 8
    assert client.hits == 0
