"""按优先级和 Token 预算装配 diff 上下文。

填充顺序（严格）：

1. Git Diff（由 PromptBuilder 单独按 patch 预算处理，不进入本模块）
2. 变更方法完整代码（必要时 AST 折叠）
3. Callee 签名 / 注释 / 异常
4. Caller 调用点（仅签名变更时产生）
5. 被方法引用的类/文件级字段
6. 其他补充证据（impact/static/reference/test）

P2/P3 采用按文件 Round-Robin：先给每个变更文件的第一个变更点分配上下文，
还有预算再分配第二个，避免大文件把后面的文件“饿死”。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from openreview.retriever.code_context import CodeContext, context_identity
from openreview.review.token_budget import estimate_tokens

CONTEXT_OVERHEAD_TOKENS = 24
ROUND_ROBIN_PRIORITIES = {2, 3}
# 低优先级上下文不能自动吃满剩余预算。P5/P6 是补充证据，设置软上限
# 是为了避免 Lost in the Middle；超限部分仍会保留在 state/metrics 中。
P5_MAX_TOKENS = 1000
P6_MAX_TOKENS = 2000
DEFAULT_PRIORITY_TOKEN_CAPS: Dict[int, int] = {
    5: P5_MAX_TOKENS,
    6: P6_MAX_TOKENS,
}


def plan_contexts(
    contexts: Sequence[CodeContext],
    max_tokens: int,
    *,
    stats: Optional[Dict[str, float]] = None,
    priority_caps: Optional[Mapping[int, int]] = None,
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

    deduped = _dedupe_by_priority(contexts)
    buckets: Dict[int, List[CodeContext]] = {}
    for context in deduped:
        buckets.setdefault(max(1, context.priority), []).append(context)

    remaining = max(0, max_tokens)
    selected: List[CodeContext] = []
    priority_tokens: Dict[int, int] = {}

    for priority in sorted(buckets):
        candidates = buckets[priority]
        ordered = (
            _round_robin_by_file(candidates)
            if priority in ROUND_ROBIN_PRIORITIES
            else candidates
        )
        cap = caps.get(priority)
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
            if cap is not None and priority_tokens.get(priority, 0) + cost > cap:
                stats["contexts_omitted_by_cap"] = int(
                    stats.get("contexts_omitted_by_cap", 0)
                ) + 1
                stats["context_tokens_omitted_by_cap"] = int(
                    stats.get("context_tokens_omitted_by_cap", 0)
                ) + cost
                continue
            selected.append(context)
            remaining -= cost
            priority_tokens[priority] = priority_tokens.get(priority, 0) + cost

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
        key = f"P{priority}"
        merged[key] = float(merged.get(key, 0.0)) + tokens
    stats["context_tokens_by_priority_detail"] = merged  # type: ignore[assignment]
    return selected


def _dedupe_by_priority(contexts: Iterable[CodeContext]) -> List[CodeContext]:
    """同一代码范围只保留 priority 更高（数值更小）、score 更高的一条。"""
    best: Dict[tuple, CodeContext] = {}
    order: List[tuple] = []
    for context in contexts:
        context.ensure_context_id()
        key = context_identity(context)
        existing = best.get(key)
        if existing is None:
            best[key] = context
            order.append(key)
            continue
        if (context.priority, -context.score) < (
            existing.priority,
            -existing.score,
        ):
            best[key] = context
    return [best[key] for key in order]


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
