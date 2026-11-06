"""LLM 响应 record / replay。

用途：
- 开发阶段：录制一次真实模型响应，后续改聚合/validator/图路由时离线回放
- 评测阶段：不重复调用模型就能比较不同后处理策略
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, Literal, Optional, Type, TypeVar

from filelock import FileLock
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
CacheMode = Literal["auto", "record", "replay"]


class LLMCacheMiss(RuntimeError):
    pass


class FileLLMCacheClient:
    def __init__(
        self,
        inner,
        cache_dir: str | Path,
        mode: CacheMode = "auto",
    ):
        self.inner = inner
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.mode = mode
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        # 多维度 reviewer 会在线程池里并发调用同一个 wrapper；计数和
        # “读 cache + 调 provider + 写 cache”都需要保护。
        self._counter_lock = threading.Lock()
        self._key_locks_lock = threading.Lock()
        self._key_locks: Dict[str, threading.Lock] = {}

    @property
    def model(self) -> str:
        """把内层客户端的模型名透出去，供 review cache key 使用。"""
        return str(getattr(self.inner, "model", type(self.inner).__name__))

    @property
    def usage(self) -> dict:
        return getattr(self.inner, "usage", {})

    def __getattr__(self, name: str):
        # cache wrapper 需要把 bind_budget / has_budget 透传给内层客户端。
        return getattr(object.__getattribute__(self, "inner"), name)

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        key = self._key(system_prompt, user_prompt, response_schema)
        path = self._path(key)

        # 快速路径：缓存已存在时不需要拿 per-key 锁。
        if self.mode != "record" and path.exists():
            cached = self._load(path, response_schema)
            if cached is not None:
                self._record_hit()
                return cached

        if self.mode == "replay":
            self._record_miss()
            raise LLMCacheMiss(f"No recorded LLM response for key {key}")

        # 同一个 prompt 的并发调用只允许一个线程走到 provider；其他线程
        # 等它写回 cache 后直接复用结果。不同 key 仍然可以并行。
        with self._lock_for_key(key):
            if self.mode != "record" and path.exists():
                cached = self._load(path, response_schema)
                if cached is not None:
                    self._record_hit()
                    return cached

            if self.mode == "replay":
                self._record_miss()
                raise LLMCacheMiss(f"No recorded LLM response for key {key}")

            self._record_miss()
            result = self.inner.generate_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
            )
            self._save(path, result)
            return result

    def _lock_for_key(self, key: str) -> threading.Lock:
        with self._key_locks_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _record_hit(self) -> None:
        with self._counter_lock:
            self.hits += 1

    def _record_miss(self) -> None:
        with self._counter_lock:
            self.misses += 1

    def _key(
        self, system_prompt: str, user_prompt: str, response_schema: Type[BaseModel]
    ) -> str:
        model = getattr(self.inner, "model", type(self.inner).__name__)
        digest = hashlib.sha256()
        for part in (
            str(model),
            # 采样参数必须进 key，否则改 temperature/seed 后会误命中旧响应。
            str(getattr(self.inner, "temperature", "")),
            str(getattr(self.inner, "seed", "")),
            response_schema.__module__,
            response_schema.__name__,
            system_prompt,
            user_prompt,
        ):
            digest.update(part.encode("utf-8", errors="ignore"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _load(
        self, path: Path, response_schema: Type[T]
    ) -> Optional[T]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") != response_schema.__name__:
                return None
            return response_schema.model_validate(payload["data"])
        except (OSError, ValueError, json.JSONDecodeError, KeyError):
            return None

    def _save(self, path: Path, result: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path) + ".lock")
        with lock:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "schema": type(result).__name__,
                            "data": result.model_dump(mode="json"),
                        },
                        handle,
                        ensure_ascii=False,
                    )
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
