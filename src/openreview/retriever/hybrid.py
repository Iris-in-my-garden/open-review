"""Hybrid code retrieval：AST 结构 + 符号索引 + import/call graph。

编排顺序：

1. 先增量刷新 ``RepoIndex``（只重排 diff 改动过的文件）
2. 再并行跑本地 AST 解析与图谱检索
3. FTS5 只对图谱未命中的残余符号做 Top-3 兜底
4. 最终由 ``plan_contexts`` 按 Token 预算 + 文件 Round-Robin 装配

向量检索不在这里默认开启。对 code review 来说，精确符号和结构关系比
“语义相似”更重要；语义检索更适合历史 review 知识（Phase 3）。
"""

from __future__ import annotations

import inspect
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from openreview.repo.diff_parser import DiffParser, ParsedDiff
from openreview.retriever.ast_utils import read_line_range
from openreview.retriever.base import Retriever
from openreview.retriever.code_context import (
    CodeContext,
    dedupe_contexts,
    make_context_id,
)
from openreview.retriever.context_planner import plan_contexts
from openreview.retriever.graph import GraphRetriever
from openreview.retriever.index import RepoIndex, module_name
from openreview.retriever.query import QueryBuilder, RetrievalQuery
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever

logger = logging.getLogger(__name__)

# 仅对图谱未命中的残余符号做兜底，避免 FTS 重新引入大量弱相关定义。
MAX_FTS_FALLBACK_CONTEXTS = 3
DEFAULT_CONTEXT_TOKENS = 8192

_QUALIFIED = re.compile(r"`([^`]+)`")


class HybridRetriever:
    def __init__(
        self,
        ast_retriever: Optional[Retriever] = None,
        index_cache_dir: Optional[str | Path] = None,
        enabled: bool = True,
        max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
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
        self.max_context_tokens = max(1, max_context_tokens)
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
        stats: Dict[str, float] = {
            "ast_contexts": 0,
            "index_symbol_hits": 0,
            "graph_caller_hits": 0,
            "graph_callee_hits": 0,
            "index_dependency_hits": 0,
            "index_error": 0,
            "graph_error": 0,
        }

        if not self.enabled:
            contexts = self._retrieve_ast(
                repo_path, parsed_diff, base_path, allow_global_fallback=False
            )
            self._merge_ast_stats(stats)
            ranked = self._plan(contexts, stats)
            self.last_stats = stats
            return ranked

        # 关键顺序：先刷新索引，再并行 AST / graph；这样 AST 能知道索引是
        # 否可用，从而只在索引崩溃时启用全局 grep 兜底。
        index = self._get_index(repo_path, stats)
        query: Optional[RetrievalQuery] = None
        if index is not None:
            try:
                query = self.query_builder.build(parsed_diff, index=index)
            except Exception as exc:  # noqa: BLE001 - query 构造失败不能拖垮 AST
                stats["index_error"] = 1
                logger.warning("Failed to build retrieval query: %s", exc)

        graph_stats: Dict[str, float] = {}
        ast_contexts: List[CodeContext] = []
        graph_contexts: List[CodeContext] = []

        def _run_ast() -> List[CodeContext]:
            return self._retrieve_ast(
                repo_path,
                parsed_diff,
                base_path,
                allow_global_fallback=index is None,
            )

        def _run_graph() -> List[CodeContext]:
            if index is None or query is None or not self.graph_enabled:
                return []
            return list(
                self.graph_retriever.retrieve(
                    repo_path, index, query, stats=graph_stats
                )
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            ast_future = executor.submit(_run_ast)
            graph_future = executor.submit(_run_graph)
            try:
                ast_contexts = ast_future.result()
            except Exception as exc:  # noqa: BLE001 - AST 失败退化为无上下文
                stats["ast_error"] = 1
                logger.warning("AST retrieval failed: %s", exc)
            try:
                graph_contexts = graph_future.result()
            except Exception as exc:  # noqa: BLE001 - 图检索失败不能阻断 AST
                stats["graph_error"] = 1
                logger.warning("Graph retrieval failed: %s", exc)

        contexts = [*ast_contexts, *graph_contexts]
        self._merge_ast_stats(stats)
        stats.update(graph_stats)

        if index is not None and query is not None:
            contexts.extend(
                self._fts_fallback_contexts(
                    repo_path,
                    index,
                    query,
                    graph_contexts,
                    stats,
                )
            )

        ranked = self._plan(contexts, stats)
        self.last_stats = stats
        return ranked

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

    def _retrieve_ast(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path],
        *,
        allow_global_fallback: bool,
    ) -> List[CodeContext]:
        kwargs: Dict[str, Any] = {"base_path": base_path}
        parameters: Mapping[str, inspect.Parameter]
        try:
            parameters = inspect.signature(self.ast_retriever.retrieve).parameters
        except (TypeError, ValueError):  # pragma: no cover - 内置/扩展函数
            parameters = {}
        if "allow_global_fallback" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs["allow_global_fallback"] = allow_global_fallback
        return list(self.ast_retriever.retrieve(repo_path, parsed_diff, **kwargs))

    def _merge_ast_stats(self, stats: Dict[str, float]) -> None:
        ast_stats = getattr(self.ast_retriever, "last_stats", None)
        if isinstance(ast_stats, dict):
            stats.update(
                {
                    key: value
                    for key, value in ast_stats.items()
                    if isinstance(value, (int, float))
                }
            )

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

    def _fts_fallback_contexts(
        self,
        repo_path: Path,
        index: RepoIndex,
        query: RetrievalQuery,
        graph_contexts: Sequence[CodeContext],
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        """FTS 只补充图谱完全没覆盖到的残余符号，Top-3 封顶。"""
        residual = _residual_symbols(query, graph_contexts)
        if not residual:
            return []

        covered_ranges = {
            (context.file_path, context.start_line, context.end_line)
            for context in graph_contexts
        }
        contexts: List[CodeContext] = []
        # 多取一些候选：与图谱已覆盖范围重合的结果会被跳过。
        for symbol in index.search_symbols(
            residual, limit=MAX_FTS_FALLBACK_CONTEXTS * 2
        ):
            key = (symbol.file_path, symbol.start_line, symbol.end_line)
            if key in covered_ranges:
                continue
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
                        f"FTS fallback {kind} `{symbol.qualname}` "
                        f"(lines {symbol.start_line}-{symbol.end_line})"
                    ),
                    snippet=snippet,
                    score=score,
                    kind=kind,
                    priority=6,
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
            if len(contexts) >= MAX_FTS_FALLBACK_CONTEXTS:
                break
        return contexts

    def _plan(
        self,
        contexts: Sequence[CodeContext],
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        deduped = dedupe_contexts(contexts)
        stats["contexts_candidates"] = len(contexts)
        stats["contexts_deduped"] = len(contexts) - len(deduped)
        selected = plan_contexts(
            deduped, self.max_context_tokens, stats=stats
        )
        # 兼容旧 metrics：把预算淘汰映射回 truncated。
        stats["contexts_truncated"] = int(
            stats.get("contexts_omitted_by_priority", 0)
        )
        stats["contexts_selected"] = len(selected)
        return selected


def _residual_symbols(
    query: RetrievalQuery, graph_contexts: Sequence[CodeContext]
) -> List[str]:
    """找出图检索没有覆盖到的 query 符号 / 词项。"""
    covered: Set[str] = set()
    for context in graph_contexts:
        if context.symbol:
            covered.add(context.symbol)
        covered.update(_QUALIFIED.findall(context.reason))

    residual: List[str] = []
    seen: Set[str] = set()
    for term in [*query.symbols, *query.terms]:
        name = term.strip()
        if not name or name in seen:
            continue
        short = name.rsplit(".", 1)[-1]
        if name in covered or short in covered:
            continue
        seen.add(name)
        residual.append(name)
    return residual
