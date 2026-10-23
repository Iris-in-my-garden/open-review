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
from pathlib import Path
from typing import Literal, Optional, Type, TypeVar

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

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        key = self._key(system_prompt, user_prompt, response_schema)
        path = self._path(key)

        if self.mode != "record" and path.exists():
            cached = self._load(path, response_schema)
            if cached is not None:
                self.hits += 1
                return cached

        if self.mode == "replay":
            raise LLMCacheMiss(f"No recorded LLM response for key {key}")

        self.misses += 1
        result = self.inner.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=response_schema,
        )
        self._save(path, result)
        return result

    def _key(
        self, system_prompt: str, user_prompt: str, response_schema: Type[BaseModel]
    ) -> str:
        model = getattr(self.inner, "model", type(self.inner).__name__)
        digest = hashlib.sha256()
        for part in (
            str(model),
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
