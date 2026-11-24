"""Hybrid code retrieval：AST 结构 + import/call graph。

编排顺序：

1. 先增量刷新 ``RepoIndex``（只重排 diff 改动过的文件）
2. 再并行跑本地 AST 解析与图谱检索
3. 最终由 ``plan_contexts`` 按 Token 预算 + 文件 Round-Robin 装配

向量检索和 FTS5 都不在这里启用。对 code review 来说，精确符号和结构关系比
“语义相似”更重要；语义检索更适合历史 review 知识（Phase 3）。
"""

from __future__ import annotations

import inspect
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from openreview.repo.diff_parser import DiffParser, ParsedDiff
from openreview.retriever.base import Retriever
from openreview.retriever.code_context import (
    CodeContext,
    ContextPriority,
    dedupe_contexts,
)
from openreview.retriever.context_planner import plan_contexts
from openreview.retriever.graph import GraphRetriever
from openreview.retriever.index import RepoIndex, index_cache_key, module_name
from openreview.retriever.query import QueryBuilder, RetrievalQuery
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_TOKENS = 8192


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
        self._index_key_cache: Dict[str, str] = {}
        self.last_stats: Dict[str, float] = {}

    def close(self) -> None:
        for index in self._index_cache.values():
            index.close()
        self._index_cache.clear()
        self._index_key_cache.clear()

    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
        *,
        plan: bool = True,
    ) -> List[CodeContext]:
        repo_path = Path(repo_path).resolve()
        stats: Dict[str, float] = {
            "ast_contexts": 0,
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
            selected = (
                self._plan(contexts, stats)
                if plan
                else self._prepare_candidates(contexts, stats)
            )
            self.last_stats = stats
            return selected

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

        graph_available = (
            index is not None and query is not None and self.graph_enabled
        )
        fallback_enabled = not graph_available
        graph_stats: Dict[str, float] = {}
        ast_contexts: List[CodeContext] = []
        graph_contexts: List[CodeContext] = []

        def _run_ast() -> List[CodeContext]:
            # grep 兜底只在索引或图不可用时启用，避免正常路径重复检索。
            return self._retrieve_ast(
                repo_path,
                parsed_diff,
                base_path,
                allow_global_fallback=fallback_enabled,
            )

        def _run_graph() -> List[CodeContext]:
            if not self.graph_enabled or index is None or query is None:
                return []
            return list(
                self.graph_retriever.retrieve(
                    repo_path, index, query, stats=graph_stats
                )
            )

        graph_failed = False
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
                graph_failed = True
                logger.warning("Graph retrieval failed: %s", exc)

        self._merge_ast_stats(stats)
        # 图在运行时失败时，索引/图链路实际不可用，补一次 grep 兜底。
        if graph_failed and graph_available:
            try:
                fallback_contexts = self._retrieve_ast(
                    repo_path,
                    parsed_diff,
                    base_path,
                    allow_global_fallback=True,
                )
                ast_contexts = [*ast_contexts, *fallback_contexts]
                self._merge_ast_stats(stats, accumulate=True)
            except Exception as exc:  # noqa: BLE001 - 兜底失败保持原结果
                logger.warning("AST fallback retrieval failed: %s", exc)

        contexts = [*ast_contexts, *graph_contexts]
        contexts = self._drop_test_file_p4_contexts(parsed_diff, contexts)
        stats.update(graph_stats)

        if plan:
            ranked = self._plan(contexts, stats)
        else:
            # 调用方（executor）会把 impact/static 合并后统一规划，这里只
            # 负责去重并报告候选数，避免同一批上下文被规划两次。
            ranked = self._prepare_candidates(contexts, stats)
        self.last_stats = stats
        return ranked

    @staticmethod
    def _drop_test_file_p4_contexts(
        parsed_diff: ParsedDiff,
        contexts: Sequence[CodeContext],
    ) -> List[CodeContext]:
        """P0：测试/benchmark/setup 文件不注入 P4 引用（当前主要是 caller/impact）。

        caller 上下文只在签名变更时产生，本质是跨文件拓扑证据。测试、
        benchmark 和 setup 里的调用点既多又和线上行为无关，整类跳过可以
        显著降低噪声。
        """
        only_tests = bool(parsed_diff.files) and all(
            file.is_test_file for file in parsed_diff.files
        )
        result: List[CodeContext] = []
        for context in contexts:
            if context.priority == ContextPriority.REFERENCE and (
                only_tests
                or DiffParser.is_low_value_reference_path(context.file_path)
            ):
                continue
            result.append(context)
        return result

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

    def _merge_ast_stats(
        self, stats: Dict[str, float], *, accumulate: bool = False
    ) -> None:
        ast_stats = getattr(self.ast_retriever, "last_stats", None)
        if not isinstance(ast_stats, dict):
            return
        for key, value in ast_stats.items():
            if not isinstance(value, (int, float)):
                continue
            if accumulate:
                stats[key] = stats.get(key, 0) + value
            else:
                stats[key] = value

    def _index_key(self, repo_path: Path) -> str:
        """Stable index key: shared git dir + HEAD, not the worktree path."""
        path_key = str(repo_path)
        key = self._index_key_cache.get(path_key)
        if key is None:
            key = index_cache_key(repo_path)
            self._index_key_cache[path_key] = key
        return key

    def _get_index(
        self, repo_path: Path, stats: Dict[str, float]
    ) -> Optional[RepoIndex]:
        key = self._index_key(repo_path)
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
            index = RepoIndex.for_worktree(
                repo_path, cache_dir=self.index_cache_dir, cache_key=key
            )
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

    def _prepare_candidates(
        self,
        contexts: Sequence[CodeContext],
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        """Dedupe candidates without spending the final token budget."""
        deduped = dedupe_contexts(contexts)
        stats["contexts_candidates"] = len(contexts)
        stats["contexts_deduped"] = len(contexts) - len(deduped)
        return deduped

    def _plan(
        self,
        contexts: Sequence[CodeContext],
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        deduped = self._prepare_candidates(contexts, stats)
        selected = plan_contexts(
            deduped, self.max_context_tokens, stats=stats
        )
        # 兼容旧 metrics：把预算淘汰映射回 truncated。
        stats["contexts_truncated"] = int(
            stats.get("contexts_omitted_by_priority", 0)
        )
        stats["contexts_selected"] = len(selected)
        return selected
