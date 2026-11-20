"""按优先级和 Token 预算装配 diff 上下文。

填充顺序（严格）：

1. 确定性静态证据（ruff/mypy，限 changed lines）
2. 变更方法完整代码（必要时 AST 折叠）
3. 定义：Callee 签名 / dependency / base revision 被删除定义
4. 引用：Caller 调用点 / impact / grep 兜底引用
5. 被方法引用的类/文件级字段
6. 关联测试函数（最多 1-2 个）

Diff 由 PromptBuilder 单独按 patch 预算处理，不进入本模块。

P2/P3 采用按文件 Round-Robin：先给每个变更文件的第一个变更点分配上下文，
还有预算再分配第二个，避免大文件把后面的文件“饿死”。

P1/P5/P6 有独立的 soft cap；dependency 虽然是 P3，但单独限 1000 tokens，
避免 import 依赖把同层的 callee/base 定义挤出预算。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Mapping, Optional, Sequence

from openreview.retriever.code_context import (
    CodeContext,
    ContextPriority,
    merge_contexts,
)
from openreview.review.token_budget import estimate_tokens

CONTEXT_OVERHEAD_TOKENS = 24
ROUND_ROBIN_PRIORITIES = {
    ContextPriority.ENCLOSING,
    ContextPriority.DEFINITION,
}
# 低优先级上下文不能自动吃满剩余预算，避免 Lost in the Middle。
P1_MAX_TOKENS = 500  # static：确定性工具信号，且只覆盖 changed lines
P5_MAX_TOKENS = 1000  # 字段
P6_MAX_TOKENS = 500  # 关联测试，最多 1-2 个函数
DEPENDENCY_MAX_TOKENS = 1000  # dependency 单独 cap，不与 callee/base 争预算
DEFAULT_PRIORITY_TOKEN_CAPS: Dict[ContextPriority, int] = {
    ContextPriority.STATIC: P1_MAX_TOKENS,
    ContextPriority.FIELD: P5_MAX_TOKENS,
    ContextPriority.TEST: P6_MAX_TOKENS,
}
DEFAULT_KIND_TOKEN_CAPS: Dict[str, int] = {
    "dependency": DEPENDENCY_MAX_TOKENS,
}


def priority_token_cap(priority: ContextPriority) -> Optional[int]:
    return DEFAULT_PRIORITY_TOKEN_CAPS.get(priority)


def kind_token_cap(kind: str) -> Optional[int]:
    return DEFAULT_KIND_TOKEN_CAPS.get(kind)


def plan_contexts(
    contexts: Sequence[CodeContext],
    max_tokens: int,
    *,
    stats: Optional[Dict[str, float]] = None,
    priority_caps: Optional[Mapping[ContextPriority, int]] = None,
    kind_caps: Optional[Mapping[str, int]] = None,
) -> List[CodeContext]:
    """按 priority 顺序填充预算，返回最终注入 prompt 的上下文列表。"""
    stats = stats if stats is not None else {}
    for key in (
        "contexts_by_priority",
        "context_tokens_by_priority",
        "contexts_omitted_by_priority",
        "context_tokens_omitted_by_priority",
        "contexts_omitted_by_cap",
        "context_tokens_omitted_by_cap",
    ):
        stats.setdefault(key, 0)

    caps = dict(DEFAULT_PRIORITY_TOKEN_CAPS)
    if priority_caps:
        caps.update(priority_caps)
    dependency_caps = dict(DEFAULT_KIND_TOKEN_CAPS)
    if kind_caps:
        dependency_caps.update(kind_caps)

    # 装配前统一去重：相同范围 / 同一 AST 节点 / P2 包含 P5 的 field。
    deduped = merge_contexts(contexts)
    buckets: Dict[ContextPriority, List[CodeContext]] = {}
    for context in deduped:
        buckets.setdefault(context.priority, []).append(context)

    remaining = max(0, max_tokens)
    selected: List[CodeContext] = []
    priority_tokens: Dict[ContextPriority, int] = {}
    kind_tokens: Dict[str, int] = {}

    for priority in sorted(buckets):
        candidates = buckets[priority]
        ordered = (
            _round_robin_by_file(candidates)
            if priority in ROUND_ROBIN_PRIORITIES
            else candidates
        )
        priority_cap = caps.get(priority)
        for context in ordered:
            cost = estimate_tokens(context.snippet) + CONTEXT_OVERHEAD_TOKENS
            context.token_cost = cost
            if cost > remaining:
                stats["contexts_omitted_by_priority"] = int(
                    stats["contexts_omitted_by_priority"]
                ) + 1
                stats["context_tokens_omitted_by_priority"] = int(
                    stats.get("context_tokens_omitted_by_priority", 0)
                ) + cost
                continue
            used_by_priority = priority_tokens.get(priority, 0)
            kind_cap = dependency_caps.get(context.kind)
            used_by_kind = kind_tokens.get(context.kind, 0)
            if (
                priority_cap is not None
                and used_by_priority + cost > priority_cap
            ) or (kind_cap is not None and used_by_kind + cost > kind_cap):
                stats["contexts_omitted_by_cap"] = int(
                    stats.get("contexts_omitted_by_cap", 0)
                ) + 1
                stats["context_tokens_omitted_by_cap"] = int(
                    stats.get("context_tokens_omitted_by_cap", 0)
                ) + cost
                continue
            selected.append(context)
            remaining -= cost
            priority_tokens[priority] = used_by_priority + cost
            kind_tokens[context.kind] = used_by_kind + cost

    stats["contexts_by_priority"] = int(
        stats.get("contexts_by_priority", 0)
    ) + len(selected)
    stats["context_tokens_by_priority"] = int(
        stats.get("context_tokens_by_priority", 0)
    ) + sum(priority_tokens.values())
    stats["context_tokens_remaining"] = remaining
    detail = stats.get("context_tokens_by_priority_detail")
    merged: Dict[str, float] = dict(detail) if isinstance(detail, dict) else {}
    for priority, tokens in priority_tokens.items():
        key = f"P{int(priority)}"
        merged[key] = float(merged.get(key, 0.0)) + tokens
    stats["context_tokens_by_priority_detail"] = merged  # type: ignore[assignment]
    return selected


def _round_robin_by_file(
    candidates: Sequence[CodeContext],
) -> List[CodeContext]:
    """按文件轮询：每个文件先出一个，再进入下一轮。"""
    groups: "OrderedDict[str, List[CodeContext]]" = OrderedDict()
    for context in candidates:
        groups.setdefault(context.file_path, []).append(context)

    ordered: List[CodeContext] = []
    while groups:
        empty: List[str] = []
        for filename, items in groups.items():
            if items:
                ordered.append(items.pop(0))
            if not items:
                empty.append(filename)
        for filename in empty:
            groups.pop(filename, None)
    return ordered
