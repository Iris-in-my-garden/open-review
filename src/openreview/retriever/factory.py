"""Retriever 工厂：AST 优先，grep 作为不支持语言的兜底。

默认的 ``tree_sitter`` 不是单纯的 AST 检索器，而是一个组合检索器：
- Python / Go：走 tree-sitter，拿函数/类边界与定义/引用区分
- 其他语言：tree-sitter 没有对应的 parser，自动降级到 grep，至少提供
  符号级上下文，而不是直接返回空结果

``--retriever grep`` 仍然保留，用于强制走纯文本检索（例如排查 AST 解析异常）。
"""

from pathlib import Path
from typing import List

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.base import Retriever
from openreview.retriever.code_context import CodeContext
from openreview.retriever.grep_retriever import GrepRetriever
from openreview.retriever.tree_sitter_retriever import (
    MAX_CONTEXT_COUNT,
    TreeSitterLocalRetriever,
)

RETRIEVER_NAMES = ("tree_sitter", "grep")


class AstWithGrepFallbackRetriever:
    """AST 优先；对没有 parser 的语言自动降级到 grep。"""

    def __init__(self):
        self.primary = TreeSitterLocalRetriever()
        self.fallback = GrepRetriever()
        self.supported_suffixes = set(self.primary.parsers)

    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Path | None = None,
    ) -> List[CodeContext]:
        contexts = self.primary.retrieve(repo_path, parsed_diff, base_path=base_path)

        unsupported = [
            file_diff
            for file_diff in parsed_diff.files
            if not file_diff.is_docs_file
            and not file_diff.is_config_file
            and Path(file_diff.filename).suffix not in self.supported_suffixes
        ]
        if not unsupported:
            return contexts

        fallback_diff = parsed_diff.model_copy(update={"files": unsupported})
        contexts.extend(
            self.fallback.retrieve(repo_path, fallback_diff, base_path=base_path)
        )
        return contexts[:MAX_CONTEXT_COUNT]


def build_retriever(name: str = "tree_sitter") -> Retriever:
    normalized = (name or "tree_sitter").strip().lower().replace("-", "_")
    if normalized in {"tree_sitter", "treesitter", "ast", "auto"}:
        return AstWithGrepFallbackRetriever()
    if normalized in {"grep", "grep_retriever"}:
        return GrepRetriever()
    raise ValueError(
        f"Unknown retriever: {name!r}. Expected one of {RETRIEVER_NAMES}."
    )
