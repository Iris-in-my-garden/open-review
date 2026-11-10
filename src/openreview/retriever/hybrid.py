"""Hybrid code retrieval：AST 结构 + 符号索引 + import/call graph。

它是现有 ``TreeSitterLocalRetriever`` 的超集，不替换原有的 AST 主路径：

1. AST enclosing：变更行所属函数/类，权重最高
2. FTS5 符号索引：精确定义 + BM25 召回
3. import/call graph：谁 import 了变更模块、谁调用了变更符号、变更函数调用了谁

向量检索不在这里默认开启。对 code review 来说，精确符号和结构关系比
“语义相似”更重要；语义检索更适合历史 review 知识（Phase 3）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from openreview.repo.diff_parser import DiffParser, ParsedDiff
from openreview.retriever.ast_utils import read_line_range
from openreview.retriever.base import Retriever
from openreview.retriever.code_context import CodeContext, make_context_id
from openreview.retriever.graph import GraphRetriever
from openreview.retriever.index import RepoIndex, module_name
from openreview.retriever.query import QueryBuilder, RetrievalQuery
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever

logger = logging.getLogger(__name__)

MAX_HYBRID_CONTEXTS = 24
MAX_SYMBOL_CONTEXTS = 12


class HybridRetriever:
    def __init__(
        self,
        ast_retriever: Optional[Retriever] = None,
        index_cache_dir: Optional[str | Path] = None,
        enabled: bool = True,
        max_contexts: int = MAX_HYBRID_CONTEXTS,
        graph_enabled: bool = True,
        graph_retriever: Optional[GraphRetriever] = None,
    ):
        self.ast_retriever: Retriever = ast_retriever or TreeSitterLocalRetriever()
        self.index_cache_dir = (
            Path(index_cache_dir).expanduser().resolve()
            if index_cache_dir is not None
            else None
        )
        self.enabled = enabled
        self.graph_enabled = graph_enabled
        self.graph_retriever = graph_retriever or GraphRetriever()
        self.max_contexts = max(1, max_contexts)
        self.query_builder = QueryBuilder()
        self._index_cache: Dict[str, RepoIndex] = {}
        self.last_stats: Dict[str, float] = {}

    def close(self) -> None:
        for index in self._index_cache.values():
            index.close()
        self._index_cache.clear()

    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> List[CodeContext]:
        repo_path = Path(repo_path).resolve()
        contexts = list(
            self.ast_retriever.retrieve(repo_path, parsed_diff, base_path=base_path)
        )
        stats: Dict[str, float] = {
            "ast_contexts": len(contexts),
            "index_symbol_hits": 0,
            "graph_caller_hits": 0,
            "graph_callee_hits": 0,
            "index_importer_hits": 0,
            "index_dependency_hits": 0,
            "index_error": 0,
            "graph_error": 0,
        }

        if not self.enabled:
            self.last_stats = stats
            return _rank_and_dedupe(contexts, self.max_contexts)

        index = self._get_index(repo_path, stats)
        if index is not None:
            try:
                query = self.query_builder.build(parsed_diff, index=index)
                contexts.extend(
                    self._symbol_contexts(repo_path, index, query, stats)
                )
                if self.graph_enabled:
                    contexts.extend(
                        self.graph_retriever.retrieve(
                            repo_path, index, query, stats=stats
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - 检索增强失败不能拖垮 review
                stats["index_error"] = 1
                logger.warning("Hybrid retrieval failed, falling back to AST: %s", exc)

        self.last_stats = stats
        return _rank_and_dedupe(contexts, self.max_contexts)

    def related_files(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> Dict[str, Set[str]]:
        """返回变更文件之间的轻量依赖关系。

        仅用于 dependency-aware batching：把有 import 关系或测试-源码关系
        的文件排进同一批次，让模型能看到跨文件交互。这不是完整 call graph，
        失败时返回空关系，batching 退化为原始顺序。

        ``base_path`` 仅为与 ``retrieve`` 接口对齐；import 关系取自 head
        侧的仓库索引。
        """
        filenames = [f.filename for f in parsed_diff.files if f.filename]
        related: Dict[str, Set[str]] = {name: set() for name in filenames}

        def _link(left: str, right: str) -> None:
            if left and right and left != right:
                related.setdefault(left, set()).add(right)
                related.setdefault(right, set()).add(left)

        # 测试文件即使不 import 源码，也应与对应源码同批审查。
        sources = [name for name in filenames if not DiffParser.is_test_path(name)]
        tests = [name for name in filenames if DiffParser.is_test_path(name)]
        for source in sources:
            for test in tests:
                if DiffParser.is_linked_test(source, test):
                    _link(source, test)

        if not self.enabled or not filenames:
            return related

        stats: Dict[str, float] = {}
        index = self._get_index(Path(repo_path).resolve(), stats)
        if index is None:
            return related

        by_module: Dict[str, str] = {}
        for name in filenames:
            by_module.setdefault(module_name(name), name)

        try:
            for edge in index.imports_of(filenames):
                target = by_module.get(edge.dst_module)
                if target is None:
                    continue
                _link(edge.src_file, target)
        except Exception as exc:  # noqa: BLE001 - 依赖关系失败不应阻断 review
            logger.debug("Dependency lookup failed: %s", exc)
        return related

    def _get_index(
        self, repo_path: Path, stats: Dict[str, float]
    ) -> Optional[RepoIndex]:
        key = str(repo_path)
        cached = self._index_cache.get(key)
        if cached is not None:
            try:
                cached.ensure(repo_path)
                stats.update(
                    {
                        "index_reused": int(cached.last_stats.get("index_reused", 0)),
                        "index_symbols": int(cached.last_stats.get("index_symbols", 0)),
                    }
                )
                return cached
            except Exception as exc:  # noqa: BLE001
                logger.debug("Index refresh failed for %s: %s", repo_path, exc)
                cached.close()
                self._index_cache.pop(key, None)

        try:
            index = RepoIndex.for_worktree(repo_path, cache_dir=self.index_cache_dir)
        except Exception as exc:  # noqa: BLE001
            stats["index_error"] = 1
            logger.warning("Failed to build repo index for %s: %s", repo_path, exc)
            return None

        self._index_cache[key] = index
        stats.update(
            {
                "index_reused": int(index.last_stats.get("index_reused", 0)),
                "index_symbols": int(index.last_stats.get("index_symbols", 0)),
                "index_files_indexed": int(
                    index.last_stats.get("index_files_indexed", 0)
                ),
                "index_build_seconds": round(
                    float(index.last_stats.get("index_build_seconds", 0.0)), 3
                ),
            }
        )
        return index

    def _symbol_contexts(
        self,
        repo_path: Path,
        index: RepoIndex,
        query: RetrievalQuery,
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        contexts: List[CodeContext] = []
        for symbol in index.search_symbols(query.terms, limit=MAX_SYMBOL_CONTEXTS):
            snippet = read_line_range(
                repo_path / symbol.file_path, symbol.start_line, symbol.end_line
            )
            if not snippet:
                continue
            score = 0.9 if symbol.name in query.symbols else 0.72
            kind = "definition" if symbol.kind.endswith("definition") else "symbol"
            contexts.append(
                CodeContext(
                    file_path=symbol.file_path,
                    reason=(
                        f"{kind} `{symbol.qualname}` "
                        f"(lines {symbol.start_line}-{symbol.end_line})"
                    ),
                    snippet=snippet,
                    score=score,
                    kind=kind,
                    symbol=symbol.name,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    context_id=make_context_id(
                        symbol.file_path,
                        symbol.start_line,
                        symbol.end_line,
                        kind,
                    ),
                )
            )
            stats["index_symbol_hits"] += 1
        return contexts


def _rank_and_dedupe(
    contexts: Sequence[CodeContext], max_contexts: int
) -> List[CodeContext]:
    """同一文件/行范围只保留分数最高的一条，避免 AST + FTS 重复注入。"""
    best: Dict[tuple, CodeContext] = {}
    order: List[tuple] = []
    for context in contexts:
        context.ensure_context_id()
        key = _context_key(context)
        existing = best.get(key)
        if existing is None:
            best[key] = context
            order.append(key)
        elif context.score > existing.score:
            best[key] = context

    ranked = sorted(
        (best[key] for key in order),
        key=lambda item: item.score,
        reverse=True,
    )
    return ranked[:max_contexts]


def _context_key(context: CodeContext) -> tuple:
    if context.start_line is not None or context.end_line is not None:
        return (
            context.file_path,
            context.revision,
            context.start_line,
            context.end_line,
        )
    return (context.file_path, context.revision, context.kind, context.snippet[:80])
