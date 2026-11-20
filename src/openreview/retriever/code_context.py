from __future__ import annotations

import hashlib
from enum import IntEnum
from typing import Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel


class ContextPriority(IntEnum):
    """上下文优先级。数字越小，越先进入 prompt。

    这个字段是必填的，没有隐式默认值：新增证据源必须明确声明自己属于
    哪一层，避免又悄悄退回 catch-all。
    """

    STATIC = 1      # 确定性静态分析（只看 changed lines）
    ENCLOSING = 2   # 变更行所在的函数/类
    DEFINITION = 3  # callee / dependency / base revision 被删除定义
    REFERENCE = 4   # caller / impact / grep 兜底引用
    FIELD = 5       # 被引用的类/结构体字段与模块常量
    TEST = 6        # 引用变更符号的关联测试函数


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

    # 上下文扩展优先级（必填）：1=确定性静态证据，2=变更方法/类，
    # 3=定义（callee/dependency/base），4=引用（caller/impact/grep 兜底），
    # 5=类/文件级字段，6=关联测试。数字越大优先级越低。
    priority: ContextPriority
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


# 这些 kind 抽取的是“定义节点”，同一个节点可能被 AST / index / graph
# 以不同 kind 重复找到。
DEFINITION_NODE_KINDS = frozenset(
    {"enclosing", "definition", "base_definition", "callee", "dependency"}
)


def _ranges_overlap(left: CodeContext, right: CodeContext) -> bool:
    assert left.start_line is not None and left.end_line is not None
    assert right.start_line is not None and right.end_line is not None
    return not (
        left.end_line < right.start_line or right.end_line < left.start_line
    )


def _is_redundant_context(context: CodeContext, kept: CodeContext) -> bool:
    """判断 ``context`` 是否已经被 ``kept`` 覆盖，可以直接合并掉。

    只比较同一文件、同一 revision 的上下文；base 与 head 的行号空间不同，
    不能互相覆盖。
    """
    if context.file_path != kept.file_path or context.revision != kept.revision:
        return False
    if (
        context.start_line is None
        or context.end_line is None
        or kept.start_line is None
        or kept.end_line is None
    ):
        return False

    # 同一个 AST 节点：定义类证据的 symbol 相同且行号范围有交集。
    if (
        context.symbol
        and context.symbol == kept.symbol
        and context.kind in DEFINITION_NODE_KINDS
        and kept.kind in DEFINITION_NODE_KINDS
        and _ranges_overlap(context, kept)
    ):
        return True

    # P2 包含 P5：字段代码已经出现在未折叠的 enclosing 片段里时，再单独
    # 注入一条 field 只会重复占用预算。
    if (
        kept.kind == "enclosing"
        and context.kind == "field"
        and not kept.folded
        and kept.start_line <= context.start_line
        and context.end_line <= kept.end_line
    ):
        return True

    return False


def merge_contexts(contexts: Iterable[CodeContext]) -> List[CodeContext]:
    """在注入 prompt 前合并重复上下文。

    去重顺序（强 → 弱）：

    1. 完全相同的 file + revision + 行号范围，只保留一条；
    2. 同一个 AST 节点（相同 symbol、定义类 kind、范围重叠），保留优先级
       更高 / score 更高的一条；
    3. P2 enclosing 完整包含 P5 field 时，合并掉 field。

    结果按 ``(priority, -score)`` 排序，保证高价值证据先进入预算。
    """
    ordered = sorted(
        dedupe_contexts(contexts),
        key=lambda context: (context.priority, -context.score),
    )
    merged: List[CodeContext] = []
    for context in ordered:
        if any(_is_redundant_context(context, kept) for kept in merged):
            continue
        merged.append(context)
    return merged
