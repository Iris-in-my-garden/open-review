from pathlib import Path

import pytest

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.base import EmptyRetriever
from openreview.retriever.hybrid import HybridRetriever
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever
from openreview.review.ablation import ABLATION_LEVELS, resolve_ablation
from openreview.review.config import ReviewConfig
from openreview.services.review_service import ReviewPipeline


@pytest.mark.parametrize(
    ("name", "retrieval_mode", "verifier"),
    [
        ("diff", "none", False),
        ("ast", "ast", False),
        ("graph", "graph", False),
        ("verifier", "graph", True),
    ],
)
def test_ablation_levels_only_change_one_variable(
    name: str, retrieval_mode: str, verifier: bool
):
    spec = resolve_ablation(name)

    assert spec.retrieval_mode == retrieval_mode
    assert spec.verifier_enabled is verifier
    # 其余证据增强在消融模式下固定关闭，保证相邻档位只差一个变量。
    assert spec.triage_enabled is False
    assert spec.impact_enabled is False
    assert spec.static_analysis_enabled is False


def test_resolve_ablation_rejects_unknown_level():
    with pytest.raises(ValueError, match="ablation must be one of"):
        resolve_ablation("unknown-level")


def test_ablation_overrides_conflicting_config_flags():
    config = ReviewConfig(
        ablation="diff",
        triage_enabled=True,
        impact_enabled=True,
        static_analysis_enabled=True,
        verifier_enabled=True,
    )

    assert config.ablation == "diff"
    assert config.retrieval_mode == "none"
    assert config.triage_enabled is False
    assert config.impact_enabled is False
    assert config.static_analysis_enabled is False
    assert config.verifier_enabled is False


def test_production_default_keeps_graph_and_verifier():
    config = ReviewConfig()

    assert config.ablation is None
    assert config.retrieval_mode == "graph"
    assert config.verifier_enabled is True
    assert config.triage_enabled is True
    assert config.impact_enabled is True


@pytest.mark.parametrize(
    ("name", "retriever_type"),
    [
        ("diff", EmptyRetriever),
        ("ast", TreeSitterLocalRetriever),
        ("graph", HybridRetriever),
        ("verifier", HybridRetriever),
    ],
)
def test_pipeline_builds_retriever_for_each_ablation(
    name: str, retriever_type: type
):
    pipeline = ReviewPipeline(mock_llm=True, ablation=name)
    try:
        assert isinstance(pipeline.retriever, retriever_type)
    finally:
        pipeline.close()


def test_empty_retriever_returns_no_contexts(tmp_path: Path):
    retriever = EmptyRetriever()

    contexts = retriever.retrieve(tmp_path, ParsedDiff(files=[]))

    assert contexts == []
    assert retriever.last_stats == {}


def test_ablation_levels_are_stable():
    assert ABLATION_LEVELS == ("diff", "ast", "graph", "verifier")
