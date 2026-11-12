"""把一次 review 的中间结果写到磁盘，供本地 debug 和评测复现。

默认关闭：只有显式传入 ``debug_dir`` 时才创建。写入失败不能影响 review
本身，因此所有 I/O / 序列化异常都降级为 warning。

产物结构::

    <debug_dir>/
      state.json              # PR、parsed_diff、batches、contexts、stats、trace
      metrics.json
      prompts/01_single_shot.system.txt
      prompts/01_single_shot.user.txt
      llm/01_single_shot.parsed.json
      validation/01.json      # 每条 finding 的置信度变化与丢弃原因
      verifier/01.json        # 每条 finding 的 verdict / reason / confidence
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _to_jsonable(value: Any) -> Any:
    """把 Pydantic / dataclass / Path / 容器递归转换成可 JSON 序列化的值。"""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class DebugSink:
    """线程安全的中间结果写入器。

    多维度审查会在线程池里并发调用，文件名用自增序号保证不冲突；
    写文件和取序号都在同一把锁内完成。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # 基础写入
    # ------------------------------------------------------------------
    def _next_index(self, key: str) -> int:
        with self._lock:
            value = self._counters.get(key, 0) + 1
            self._counters[key] = value
            return value

    def _write_text(self, relative: str, content: str) -> None:
        try:
            path = self.root / relative
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to write debug artifact %s: %s", relative, exc)

    def write_text(self, relative: str, content: str) -> None:
        self._write_text(relative, content)

    def write_json(self, relative: str, data: Any) -> None:
        try:
            content = json.dumps(
                _to_jsonable(data),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Failed to serialize debug artifact %s: %s", relative, exc
            )
            return
        self._write_text(relative, content)

    # ------------------------------------------------------------------
    # 语义化记录
    # ------------------------------------------------------------------
    def record_prompt(
        self, name: str, system_prompt: str, user_prompt: str
    ) -> None:
        index = self._next_index("prompt")
        prefix = f"prompts/{index:02d}_{name}"
        self.write_text(f"{prefix}.system.txt", system_prompt)
        self.write_text(f"{prefix}.user.txt", user_prompt)

    def record_llm_response(self, name: str, data: Any) -> None:
        index = self._next_index("llm")
        self.write_json(f"llm/{index:02d}_{name}.parsed.json", data)

    def record_validation(self, records: List[Dict[str, Any]]) -> None:
        index = self._next_index("validation")
        self.write_json(f"validation/{index:02d}.json", records)

    def record_verifier(self, records: List[Dict[str, Any]]) -> None:
        index = self._next_index("verifier")
        self.write_json(f"verifier/{index:02d}.json", records)

    def record_state(self, state: Any) -> None:
        """把 ReviewState 的关键字段落盘；不依赖 workflow 模块，避免循环导入。"""
        result = getattr(state, "review_result", None)
        batches = getattr(state, "batches", None) or []
        payload = {
            "run_id": getattr(state, "run_id", ""),
            "pr_url": getattr(state, "pr_url", ""),
            "pr": getattr(state, "pr_info", None),
            "parsed_diff": getattr(state, "parsed_diff", None),
            "batches": [
                [getattr(file, "filename", "") for file in batch.files]
                for batch in batches
            ],
            "contexts": list(getattr(state, "all_contexts", None) or []),
            "stats": {
                "retrieval": dict(getattr(state, "retrieval_stats", None) or {}),
                "validation": dict(
                    getattr(state, "validation_stats", None) or {}
                ),
                "verifier": dict(getattr(state, "verifier_stats", None) or {}),
            },
            "impact_findings": list(
                getattr(state, "impact_findings", None) or []
            ),
            "static_findings": list(
                getattr(state, "static_findings", None) or []
            ),
            "notes": list(getattr(state, "notes", None) or []),
            "degraded_reasons": list(
                getattr(state, "degraded_reasons", None) or []
            ),
            "stages": dict(getattr(state, "stages", None) or {}),
            "trace": list(getattr(state, "trace", None) or []),
            "outcome": getattr(state, "outcome", None),
            "result": result,
        }
        self.write_json("state.json", payload)
        if result is not None:
            self.write_json(
                "metrics.json", getattr(result, "metrics", None) or {}
            )
