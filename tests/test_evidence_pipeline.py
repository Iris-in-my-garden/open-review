from __future__ import annotations

from types import SimpleNamespace

from openreview.retriever.code_context import CodeContext
from openreview.services.evidence_pipeline import (
    MAX_GLOBAL_IMPACT_FALLBACK,
    MAX_IMPACT_PER_BATCH,
    EvidencePipeline,
)


def _batch(*symbols: str):
    return SimpleNamespace(
        files=[
            SimpleNamespace(
                filename="src/a.py",
                changed_symbols=list(symbols),
                removed_symbols=[],
                enclosing_contexts=[],
            )
        ]
    )


def _impact(symbol: str, score: float = 0.7) -> CodeContext:
    return CodeContext(
        file_path=f"src/use_{symbol}.py",
        reason=f"impact reference to {symbol}",
        snippet=f"{symbol}()",
        score=score,
        kind="impact",
        symbol=symbol,
        start_line=1,
        end_line=1,
    )


def test_impact_contexts_are_scoped_to_batch_symbols():
    batch = _batch("login")
    impact = [_impact("login"), _impact("logout")]

    extras = EvidencePipeline.extra_contexts(batch, impact, [])

    assert [context.symbol for context in extras] == ["login"]


def test_impact_contexts_fall_back_to_global_only_for_first_batch():
    batch = _batch("unrelated")
    impact = [_impact(f"symbol_{index}", score=0.9 - index / 100) for index in range(5)]

    first = EvidencePipeline.extra_contexts(
        batch, impact, [], include_global_impact=True
    )
    later = EvidencePipeline.extra_contexts(batch, impact, [])

    assert len(first) == MAX_GLOBAL_IMPACT_FALLBACK
    assert later == []


def test_impact_contexts_are_capped_per_batch():
    batch = _batch("login")
    impact = [_impact("login", score=0.9 - index / 100) for index in range(10)]

    extras = EvidencePipeline.extra_contexts(batch, impact, [])

    assert len(extras) == MAX_IMPACT_PER_BATCH


def test_static_contexts_stay_batch_scoped():
    batch = _batch("login")
    static = [
        CodeContext(
            file_path="src/a.py",
            reason="static:ruff",
            snippet="x = 1",
            score=0.6,
            kind="static",
        ),
        CodeContext(
            file_path="src/b.py",
            reason="static:ruff",
            snippet="y = 1",
            score=0.6,
            kind="static",
        ),
    ]

    extras = EvidencePipeline.extra_contexts(batch, [], static)

    assert [context.file_path for context in extras] == ["src/a.py"]
