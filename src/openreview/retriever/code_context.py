from __future__ import annotations

import hashlib
from typing import Optional

from pydantic import BaseModel


def make_context_id(
    file_path: str,
    start_line: Optional[int],
    end_line: Optional[int],
    kind: str,
    revision: str = "head",
    fallback: str = "",
) -> str:
    """生成稳定、可引用的 context id。

    行号缺失时（impact/static 等）用 fallback（通常是 snippet 或 reason）
    参与哈希，保证同一段上下文在多次运行中得到相同 id。
    """
    raw = "|".join(
        [
            file_path,
            str(start_line or ""),
            str(end_line or ""),
            kind,
            revision,
            fallback,
        ]
    )
    return f"ctx-{hashlib.sha1(raw.encode('utf-8', errors='ignore')).hexdigest()[:8]}"


class CodeContext(BaseModel):
    file_path: str
    reason: str
    snippet: str
    score: float
    revision: str = "head"

    # Phase 1：让上下文可引用、可评测。
    context_id: str = ""
    kind: str = "snippet"
    symbol: str = ""
    start_line: Optional[int] = None
    end_line: Optional[int] = None

    def ensure_context_id(self) -> str:
        if not self.context_id:
            self.context_id = make_context_id(
                file_path=self.file_path,
                start_line=self.start_line,
                end_line=self.end_line,
                kind=self.kind,
                revision=self.revision,
                fallback=self.snippet[:120],
            )
        return self.context_id
