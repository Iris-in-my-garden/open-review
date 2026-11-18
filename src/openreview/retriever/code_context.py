from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List, Optional, Tuple

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

    # 上下文扩展优先级：1=Diff，2=变更方法，3=Callee 签名，4=Caller 调用点，
    # 5=类/文件级字段，6=其他补充证据（impact/static/reference/test）。
    priority: int = 6
    # 是否做过 AST 分支折叠；折叠时 changed lines 必须被保留。
    folded: bool = False
    # 装配时计算的估算成本，便于 metrics / 调试。
    token_cost: int = 0

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


def context_identity(
    context: CodeContext, *, include_kind: bool = False
) -> Tuple[str, str, Optional[int], Optional[int], str]:
    """Canonical dedupe key shared by AST / hybrid / graph / executor.

    A code range is the strongest identity: the same ``file:start-end`` is the
    same evidence regardless of which mechanism found it (AST enclosing, index
    definition, graph caller, impact reference).  Keeping ``kind`` out of the
    key is deliberate; otherwise the same snippet can be injected twice with
    different labels and waste prompt budget.  Contexts without line ranges
    fall back to file + revision + a short snippet fingerprint.
    """
    if context.start_line is not None or context.end_line is not None:
        return (
            context.file_path,
            context.revision,
            context.start_line,
            context.end_line,
            context.kind if include_kind else "",
        )
    return (
        context.file_path,
        context.revision,
        None,
        None,
        context.kind if include_kind else context.snippet[:120],
    )


def dedupe_contexts(
    contexts: Iterable[CodeContext], *, include_kind: bool = False
) -> List[CodeContext]:
    """Keep the highest-scored context per canonical identity, preserving order."""
    best: Dict[tuple, CodeContext] = {}
    order: List[tuple] = []
    for context in contexts:
        context.ensure_context_id()
        key = context_identity(context, include_kind=include_kind)
        existing = best.get(key)
        if existing is None:
            best[key] = context
            order.append(key)
        elif (context.priority, -context.score) < (
            existing.priority,
            -existing.score,
        ):
            # 同一代码范围优先保留更高优先级的证据（如 P4 调用点胜过
            # P6 import 行），优先级相同时再比较 score。
            best[key] = context
    return [best[key] for key in order]
