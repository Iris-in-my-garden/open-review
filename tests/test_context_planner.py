"""P1-P6 上下文预算装配的行为约束。"""

from openreview.retriever.code_context import (
    CodeContext,
    ContextPriority,
    merge_contexts,
)
from openreview.retriever.context_planner import CONTEXT_OVERHEAD_TOKENS, plan_contexts
from openreview.review.token_budget import estimate_tokens


def _context(
    file_path: str,
    *,
    priority: ContextPriority,
    kind: str = "snippet",
    snippet: str = "x" * 300,
    start_line: int = 1,
    end_line: int = 1,
    score: float = 0.5,
    symbol: str = "",
    folded: bool = False,
) -> CodeContext:
    return CodeContext(
        file_path=file_path,
        reason="test",
        snippet=snippet,
        score=score,
        kind=kind,
        priority=priority,
        symbol=symbol,
        folded=folded,
        start_line=start_line,
        end_line=end_line,
    )


def _cost(context: CodeContext) -> int:
    return estimate_tokens(context.snippet) + CONTEXT_OVERHEAD_TOKENS


def test_plan_contexts_fills_in_strict_priority_order():
    p2 = _context("src/a.py", priority=ContextPriority.ENCLOSING)
    p3 = _context(
        "src/b.py",
        priority=ContextPriority.DEFINITION,
        snippet="y" * 300,
    )
    # 只够 P2 + 一小段 P3：P2 必须先被完整保留。
    budget = _cost(p2) + 10

    selected = plan_contexts([p3, p2], budget)

    assert [item.priority for item in selected] == [ContextPriority.ENCLOSING]


def test_plan_contexts_uses_leftover_budget_for_lower_priority():
    p2 = _context("src/a.py", priority=ContextPriority.ENCLOSING)
    p3 = _context(
        "src/b.py", priority=ContextPriority.DEFINITION, snippet="z"
    )
    budget = _cost(p2) + _cost(p3)

    selected = plan_contexts([p3, p2], budget)

    assert [item.priority for item in selected] == [
        ContextPriority.ENCLOSING,
        ContextPriority.DEFINITION,
    ]


def test_plan_contexts_round_robin_keeps_every_file_represented():
    # src/a.py 有三个变更点，src/b.py 只有一个；预算只够两份。
    contexts = [
        _context(
            "src/a.py",
            priority=ContextPriority.ENCLOSING,
            start_line=index,
            end_line=index,
        )
        for index in range(1, 4)
    ]
    contexts.append(
        _context(
            "src/b.py",
            priority=ContextPriority.ENCLOSING,
            start_line=1,
            end_line=1,
        )
    )
    budget = _cost(contexts[0]) * 2 + 5

    selected = plan_contexts(contexts, budget)

    assert len(selected) == 2
    assert {item.file_path for item in selected} == {"src/a.py", "src/b.py"}


def test_plan_contexts_dedupes_same_range_by_priority():
    low_priority = _context(
        "src/a.py", priority=ContextPriority.TEST, score=0.99
    )
    high_priority = _context(
        "src/a.py", priority=ContextPriority.ENCLOSING, score=0.1
    )

    selected = plan_contexts([low_priority, high_priority], 10_000)

    assert len(selected) == 1
    assert selected[0].priority == ContextPriority.ENCLOSING


def test_impact_and_caller_share_p4_and_dedupe():
    """impact 与 caller 同为 P4；同一代码范围只保留一条。"""
    caller = _context(
        "src/a.py",
        priority=ContextPriority.REFERENCE,
        kind="caller",
        start_line=10,
        end_line=12,
        score=0.6,
    )
    impact = _context(
        "src/a.py",
        priority=ContextPriority.REFERENCE,
        kind="impact",
        start_line=10,
        end_line=12,
        score=0.9,
    )

    selected = plan_contexts([caller, impact], 10_000)

    assert len(selected) == 1
    assert selected[0].kind == "impact"


def test_plan_contexts_records_omission_stats():
    contexts = [
        _context(
            "src/a.py",
            priority=ContextPriority.ENCLOSING,
            start_line=1,
            end_line=1,
        ),
        _context(
            "src/a.py",
            priority=ContextPriority.REFERENCE,
            start_line=2,
            end_line=2,
        ),
    ]
    stats: dict[str, float] = {}

    selected = plan_contexts([*contexts], _cost(contexts[0]), stats=stats)

    assert [item.priority for item in selected] == [ContextPriority.ENCLOSING]
    assert stats["contexts_by_priority"] == 1
    assert stats["contexts_omitted_by_priority"] == 1
    assert stats["context_tokens_omitted_by_priority"] == _cost(contexts[1])
    assert stats["context_tokens_by_priority_detail"] == {
        "P2": _cost(contexts[0])
    }


def test_plan_contexts_caps_static_field_and_test_buckets():
    from openreview.retriever.context_planner import (
        P1_MAX_TOKENS,
        P5_MAX_TOKENS,
        P6_MAX_TOKENS,
    )

    buckets = {
        ContextPriority.STATIC: [
            _context(
                f"src/s{index}.py",
                priority=ContextPriority.STATIC,
                kind="static",
                start_line=index,
                end_line=index,
            )
            for index in range(1, 30)
        ],
        ContextPriority.FIELD: [
            _context(
                f"src/f{index}.py",
                priority=ContextPriority.FIELD,
                kind="field",
                start_line=index,
                end_line=index,
            )
            for index in range(1, 30)
        ],
        ContextPriority.TEST: [
            _context(
                f"src/t{index}.py",
                priority=ContextPriority.TEST,
                kind="test",
                start_line=index,
                end_line=index,
            )
            for index in range(1, 30)
        ],
    }
    stats: dict[str, float] = {}

    selected = plan_contexts(
        [item for group in buckets.values() for item in group],
        100_000,
        stats=stats,
    )

    used = {
        priority: sum(
            _cost(item) for item in selected if item.priority == priority
        )
        for priority in buckets
    }
    assert used[ContextPriority.STATIC] <= P1_MAX_TOKENS
    assert used[ContextPriority.FIELD] <= P5_MAX_TOKENS
    assert used[ContextPriority.TEST] <= P6_MAX_TOKENS
    assert stats["contexts_omitted_by_cap"] > 0


def test_dependency_has_separate_cap_inside_p3():
    """dependency 与 callee/base 同为 P3，但只限制 dependency 自己。"""
    from openreview.retriever.context_planner import DEPENDENCY_MAX_TOKENS

    dependencies = [
        _context(
            f"src/dep{index}.py",
            priority=ContextPriority.DEFINITION,
            kind="dependency",
            start_line=index,
            end_line=index,
        )
        for index in range(1, 30)
    ]
    callees = [
        _context(
            f"src/callee{index}.py",
            priority=ContextPriority.DEFINITION,
            kind="callee",
            snippet="z" * 300,
            start_line=index,
            end_line=index,
        )
        for index in range(1, 30)
    ]

    selected = plan_contexts([*dependencies, *callees], 100_000)

    dependency_tokens = sum(
        _cost(item) for item in selected if item.kind == "dependency"
    )
    callee_tokens = sum(
        _cost(item) for item in selected if item.kind == "callee"
    )
    assert dependency_tokens <= DEPENDENCY_MAX_TOKENS
    assert callee_tokens > DEPENDENCY_MAX_TOKENS


def test_context_priorities_follow_agreed_hierarchy():
    """P1 static → P2 enclosing → P3 definition → P4 reference → P5 field → P6 test。"""
    contexts = [
        _context("src/s.py", priority=ContextPriority.STATIC, kind="static"),
        _context("src/e.py", priority=ContextPriority.ENCLOSING, kind="enclosing"),
        _context("src/d.py", priority=ContextPriority.DEFINITION, kind="callee"),
        _context("src/r.py", priority=ContextPriority.REFERENCE, kind="impact"),
        _context("src/f.py", priority=ContextPriority.FIELD, kind="field"),
        _context("src/t.py", priority=ContextPriority.TEST, kind="test"),
    ]

    selected = plan_contexts(list(reversed(contexts)), 100_000)

    assert [item.priority for item in selected] == [
        ContextPriority.STATIC,
        ContextPriority.ENCLOSING,
        ContextPriority.DEFINITION,
        ContextPriority.REFERENCE,
        ContextPriority.FIELD,
        ContextPriority.TEST,
    ]


def test_merge_contexts_collapses_same_ast_node_with_overlapping_ranges():
    """同一文件、同一 symbol 的定义节点，即使范围略有偏差也只保留一条。"""
    enclosing = _context(
        "src/a.py",
        priority=ContextPriority.ENCLOSING,
        kind="enclosing",
        symbol="login",
        start_line=10,
        end_line=20,
        score=0.95,
    )
    callee = _context(
        "src/a.py",
        priority=ContextPriority.DEFINITION,
        kind="callee",
        symbol="login",
        start_line=18,
        end_line=25,
        score=0.90,
    )

    merged = merge_contexts([callee, enclosing])

    assert merged == [enclosing]


def test_merge_contexts_drops_field_contained_in_unfolded_enclosing():
    """P2 已经完整包含 P5 字段代码时，field 是重复内容。"""
    enclosing = _context(
        "src/a.py",
        priority=ContextPriority.ENCLOSING,
        kind="enclosing",
        start_line=10,
        end_line=40,
        score=0.95,
    )
    field = _context(
        "src/a.py",
        priority=ContextPriority.FIELD,
        kind="field",
        start_line=15,
        end_line=16,
        score=0.8,
    )

    assert merge_contexts([field, enclosing]) == [enclosing]


def test_merge_contexts_keeps_field_outside_or_when_enclosing_is_folded():
    """超出 enclosing 范围、或 enclosing 已折叠时，field 仍应保留。"""
    folded_enclosing = _context(
        "src/a.py",
        priority=ContextPriority.ENCLOSING,
        kind="enclosing",
        start_line=10,
        end_line=40,
        folded=True,
    )
    inside = _context(
        "src/a.py",
        priority=ContextPriority.FIELD,
        kind="field",
        start_line=15,
        end_line=16,
    )
    outside = _context(
        "src/a.py",
        priority=ContextPriority.FIELD,
        kind="field",
        start_line=41,
        end_line=42,
    )

    merged = merge_contexts([inside, outside, folded_enclosing])

    assert {item.kind for item in merged} == {"enclosing", "field"}
    assert any(item.start_line == 41 for item in merged)


def test_merge_contexts_does_not_mix_base_and_head_ranges():
    """base 与 head 行号空间不同，即使范围相同也不能互相覆盖。"""
    head = _context(
        "src/a.py",
        priority=ContextPriority.ENCLOSING,
        kind="enclosing",
        symbol="login",
        start_line=10,
        end_line=20,
    )
    base = head.model_copy(
        update={
            "revision": "base",
            "kind": "base_definition",
            "priority": ContextPriority.DEFINITION,
        }
    )

    merged = merge_contexts([head, base])

    assert {item.revision for item in merged} == {"head", "base"}
