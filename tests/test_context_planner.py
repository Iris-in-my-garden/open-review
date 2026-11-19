"""P2-P5 上下文预算装配的行为约束。"""

from openreview.retriever.code_context import CodeContext
from openreview.retriever.context_planner import CONTEXT_OVERHEAD_TOKENS, plan_contexts
from openreview.review.token_budget import estimate_tokens


def _context(
    file_path: str,
    *,
    priority: int,
    snippet: str = "x" * 300,
    start_line: int = 1,
    end_line: int = 1,
    score: float = 0.5,
) -> CodeContext:
    return CodeContext(
        file_path=file_path,
        reason="test",
        snippet=snippet,
        score=score,
        priority=priority,
        start_line=start_line,
        end_line=end_line,
    )


def _cost(context: CodeContext) -> int:
    return estimate_tokens(context.snippet) + CONTEXT_OVERHEAD_TOKENS


def test_plan_contexts_fills_in_strict_priority_order():
    p2 = _context("src/a.py", priority=2)
    p3 = _context("src/b.py", priority=3, snippet="y" * 300)
    # 只够 P2 + 一小段 P3：P2 必须先被完整保留。
    budget = _cost(p2) + 10

    selected = plan_contexts([p3, p2], budget)

    assert [item.priority for item in selected] == [2]


def test_plan_contexts_uses_leftover_budget_for_lower_priority():
    p2 = _context("src/a.py", priority=2)
    p3 = _context("src/b.py", priority=3, snippet="z")
    budget = _cost(p2) + _cost(p3)

    selected = plan_contexts([p3, p2], budget)

    assert [item.priority for item in selected] == [2, 3]


def test_plan_contexts_round_robin_keeps_every_file_represented():
    # src/a.py 有三个变更点，src/b.py 只有一个；预算只够两份。
    contexts = [
        _context("src/a.py", priority=2, start_line=index, end_line=index)
        for index in range(1, 4)
    ]
    contexts.append(_context("src/b.py", priority=2, start_line=1, end_line=1))
    budget = _cost(contexts[0]) * 2 + 5

    selected = plan_contexts(contexts, budget)

    assert len(selected) == 2
    assert {item.file_path for item in selected} == {"src/a.py", "src/b.py"}


def test_plan_contexts_dedupes_same_range_by_priority():
    low_priority = _context("src/a.py", priority=6, score=0.99)
    high_priority = _context("src/a.py", priority=2, score=0.1)

    selected = plan_contexts([low_priority, high_priority], 10_000)

    assert len(selected) == 1
    assert selected[0].priority == 2


def test_plan_contexts_records_omission_stats():
    contexts = [
        _context("src/a.py", priority=2, start_line=1, end_line=1),
        _context("src/a.py", priority=4, start_line=2, end_line=2),
    ]
    stats: dict[str, float] = {}

    selected = plan_contexts([*contexts], _cost(contexts[0]), stats=stats)

    assert [item.priority for item in selected] == [2]
    assert stats["contexts_by_priority"] == 1
    assert stats["contexts_omitted_by_priority"] == 1
    assert stats["context_tokens_omitted_by_priority"] == _cost(contexts[1])
    assert stats["context_tokens_by_priority_detail"] == {"P2": _cost(contexts[0])}


def test_plan_contexts_caps_low_priority_buckets():
    from openreview.retriever.context_planner import (
        P5_MAX_TOKENS,
        P6_MAX_TOKENS,
    )

    p5 = [
        _context("src/a.py", priority=5, start_line=index, end_line=index)
        for index in range(1, 20)
    ]
    p6 = [
        _context("src/b.py", priority=6, start_line=index, end_line=index)
        for index in range(1, 30)
    ]
    stats: dict[str, float] = {}

    selected = plan_contexts([*p5, *p6], 100_000, stats=stats)

    p5_tokens = sum(_cost(item) for item in selected if item.priority == 5)
    p6_tokens = sum(_cost(item) for item in selected if item.priority == 6)
    assert p5_tokens <= P5_MAX_TOKENS
    assert p6_tokens <= P6_MAX_TOKENS
    assert stats["contexts_omitted_by_cap"] > 0
