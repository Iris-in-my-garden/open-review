"""Retriever 工厂：保留 grep 基线，方便做“无检索 / grep / AST”的 ablation。"""

from openreview.retriever.base import Retriever
from openreview.retriever.grep_retriever import GrepRetriever
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever

RETRIEVER_NAMES = ("tree_sitter", "grep")


def build_retriever(name: str = "tree_sitter") -> Retriever:
    normalized = (name or "tree_sitter").strip().lower().replace("-", "_")
    if normalized in {"tree_sitter", "treesitter", "ast"}:
        return TreeSitterLocalRetriever()
    if normalized in {"grep", "grep_retriever", "baseline"}:
        return GrepRetriever()
    raise ValueError(
        f"Unknown retriever: {name!r}. Expected one of {RETRIEVER_NAMES}."
    )
