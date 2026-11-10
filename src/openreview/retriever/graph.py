"""基于符号索引的 import / call graph 检索。

Phase 1 的 ``HybridRetriever`` 已经能按符号名和 import 找上下文；这里补上
真正跨文件的“谁调用了变更符号 / 变更函数调用了谁”，把影响面从文件名提升
到函数级。

约束：
- call graph 是 best-effort，不宣称完整类型解析；Python 用 ``ast``，Go 暂不
  默认启用（索引里已有 calls，但 receiver/interface 解析还不够可靠）。
- 所有结果只是 candidate context，最终仍由 LLM + validator 判断。
- call graph 和 import graph 分阶段兜底：某一类关系失败时，其他上下文仍可用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from openreview.retriever.ast_utils import read_line_range
from openreview.retriever.code_context import CodeContext, make_context_id
from openreview.retriever.index import RepoIndex, module_name
from openreview.retriever.query import RetrievalQuery

logger = logging.getLogger(__name__)

MAX_CALLER_CONTEXTS = 8
MAX_CALLEE_CONTEXTS = 8
MAX_IMPORTER_CONTEXTS = 6
MAX_DEPENDENCY_CONTEXTS = 6
CALL_SITE_RADIUS = 5


class GraphRetriever:
    """用 import/call 关系补充跨文件上下文。"""

    def __init__(
        self,
        max_callers: int = MAX_CALLER_CONTEXTS,
        max_callees: int = MAX_CALLEE_CONTEXTS,
        max_importers: int = MAX_IMPORTER_CONTEXTS,
        max_dependencies: int = MAX_DEPENDENCY_CONTEXTS,
        python_calls_only: bool = True,
    ):
        self.max_callers = max(0, max_callers)
        self.max_callees = max(0, max_callees)
        self.max_importers = max(0, max_importers)
        self.max_dependencies = max(0, max_dependencies)
        self.python_calls_only = python_calls_only

    def retrieve(
        self,
        repo_path: Path,
        index: RepoIndex,
        query: RetrievalQuery,
        stats: Optional[Dict[str, float]] = None,
    ) -> List[CodeContext]:
        repo_path = Path(repo_path).resolve()
        stats = stats if stats is not None else {}
        for key in (
            "graph_caller_hits",
            "graph_callee_hits",
            "index_importer_hits",
            "index_dependency_hits",
            "graph_error",
        ):
            stats.setdefault(key, 0)

        contexts: List[CodeContext] = []
        if query.symbols:
            contexts.extend(
                self._collect(
                    "caller", self._caller_contexts, repo_path, index, query, stats
                )
            )
            contexts.extend(
                self._collect(
                    "callee", self._callee_contexts, repo_path, index, query, stats
                )
            )
        contexts.extend(
            self._collect(
                "importer", self._importer_contexts, repo_path, index, query, stats
            )
        )
        contexts.extend(
            self._collect(
                "dependency",
                self._dependency_contexts,
                repo_path,
                index,
                query,
                stats,
            )
        )
        return _dedupe(contexts)

    @staticmethod
    def _collect(
        label: str,
        collector,
        repo_path: Path,
        index: RepoIndex,
        query: RetrievalQuery,
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        """单项图检索失败时只记录错误，不影响其他类型上下文。"""
        try:
            return collector(repo_path, index, query, stats)
        except Exception as exc:  # noqa: BLE001 - 图检索是增强，失败不能阻断 review
            stats["graph_error"] = float(stats.get("graph_error", 0)) + 1
            logger.warning("%s graph retrieval failed: %s", label, exc)
            return []

    # ------------------------------------------------------------------
    # call graph
    # ------------------------------------------------------------------
    def _caller_contexts(
        self,
        repo_path: Path,
        index: RepoIndex,
        query: RetrievalQuery,
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        if self.max_callers <= 0:
            return []
        contexts: List[CodeContext] = []
        seen: set[tuple[str, int, int]] = set()
        for edge in index.callers_of(
            query.symbols, limit=max(self.max_callers * 8, 32)
        ):
            if self.python_calls_only and not edge.caller_file.endswith(".py"):
                continue
            enclosing = index.enclosing_symbol(edge.caller_file, edge.line)
            if enclosing is not None:
                start, end = enclosing.start_line, enclosing.end_line
                symbol = enclosing.qualname
                reason = (
                    f"caller `{enclosing.qualname}` calls changed symbol "
                    f"`{edge.callee_name}` at line {edge.line}"
                )
            else:
                start = max(1, edge.line - CALL_SITE_RADIUS)
                end = edge.line + CALL_SITE_RADIUS
                symbol = edge.caller_symbol or edge.callee_name
                reason = (
                    f"call site `{edge.callee_name}` "
                    f"(caller symbol unknown) at line {edge.line}"
                )
            key = (edge.caller_file, start, end)
            if key in seen:
                continue
            seen.add(key)
            snippet = read_line_range(repo_path / edge.caller_file, start, end)
            if not snippet:
                continue
            contexts.append(
                CodeContext(
                    file_path=edge.caller_file,
                    reason=reason,
                    snippet=snippet,
                    score=0.58,
                    kind="caller",
                    symbol=symbol,
                    start_line=start,
                    end_line=end,
                    context_id=make_context_id(
                        edge.caller_file,
                        start,
                        end,
                        "caller",
                        fallback=edge.callee_name,
                    ),
                )
            )
            stats["graph_caller_hits"] += 1
            if len(contexts) >= self.max_callers:
                break
        return contexts

    @staticmethod
    def _resolve_definition(
        index: RepoIndex,
        edge,
        definitions: Sequence,
    ):
        """在同名定义中消歧。

        ``search_symbols`` 只按名字召回；``login`` 可能同时存在于多个
        Service 类里。优先选调用方同文件的定义，其次是调用方 import 的
        模块里的定义，最后才退回索引评分。
        """
        if not definitions:
            return None
        if len(definitions) == 1:
            return definitions[0]

        imports_of = getattr(index, "imports_of", None)
        imported_modules: set[str] = set()
        imported_symbols: set[str] = set()
        if callable(imports_of):
            try:
                for item in imports_of([edge.caller_file]):
                    imported_modules.add(item.dst_module)
                    if item.symbol:
                        imported_symbols.add(item.symbol)
            except Exception:  # noqa: BLE001 - import 图失败不应阻断 callee 检索
                pass

        def _rank(definition) -> float:
            score = float(getattr(definition, "score", 0.0) or 0.0)
            if definition.file_path == edge.caller_file:
                score += 3.0
            if module_name(definition.file_path) in imported_modules:
                score += 2.0
            if definition.name in imported_symbols:
                score += 1.0
            return score

        return max(definitions, key=_rank)

    def _callee_contexts(
        self,
        repo_path: Path,
        index: RepoIndex,
        query: RetrievalQuery,
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        if self.max_callees <= 0:
            return []
        contexts: List[CodeContext] = []
        seen: set[tuple[str, int]] = set()
        for edge in index.callees_of(
            query.symbols, limit=max(self.max_callees * 8, 32)
        ):
            if self.python_calls_only and not edge.caller_file.endswith(".py"):
                continue
            definitions = [
                item
                for item in index.search_symbols([edge.callee_name], limit=16)
                if item.name == edge.callee_name
            ]
            definition = self._resolve_definition(index, edge, definitions)
            if definition is None:
                continue
            key = (definition.file_path, definition.start_line)
            if key in seen:
                continue
            seen.add(key)
            snippet = read_line_range(
                repo_path / definition.file_path,
                definition.start_line,
                definition.end_line,
            )
            if not snippet:
                continue
            contexts.append(
                CodeContext(
                    file_path=definition.file_path,
                    reason=(
                        f"`{edge.caller_symbol or 'caller'}` calls "
                        f"`{edge.callee_name}`; definition "
                        f"(lines {definition.start_line}-{definition.end_line})"
                    ),
                    snippet=snippet,
                    score=0.56,
                    kind="callee",
                    symbol=definition.name,
                    start_line=definition.start_line,
                    end_line=definition.end_line,
                    context_id=make_context_id(
                        definition.file_path,
                        definition.start_line,
                        definition.end_line,
                        "callee",
                    ),
                )
            )
            stats["graph_callee_hits"] += 1
            if len(contexts) >= self.max_callees:
                break
        return contexts

    # ------------------------------------------------------------------
    # import graph
    # ------------------------------------------------------------------
    def _importer_contexts(
        self,
        repo_path: Path,
        index: RepoIndex,
        query: RetrievalQuery,
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        if self.max_importers <= 0:
            return []
        contexts: List[CodeContext] = []
        seen: set[tuple[str, int]] = set()
        for edge in index.importers_of(
            query.modules, limit=max(self.max_importers * 3, self.max_importers)
        ):
            key = (edge.src_file, edge.line)
            if key in seen:
                continue
            seen.add(key)
            start = max(1, edge.line - 4)
            end = edge.line + 8
            snippet = read_line_range(repo_path / edge.src_file, start, end)
            if not snippet:
                continue
            contexts.append(
                CodeContext(
                    file_path=edge.src_file,
                    reason=(
                        f"import of `{edge.dst_module}`"
                        + (f" (`{edge.symbol}`)" if edge.symbol else "")
                    ),
                    snippet=snippet,
                    score=0.6,
                    kind="import",
                    symbol=edge.symbol,
                    start_line=start,
                    end_line=end,
                    context_id=make_context_id(
                        edge.src_file, start, end, "import", fallback=edge.dst_module
                    ),
                )
            )
            stats["index_importer_hits"] += 1
            if len(contexts) >= self.max_importers:
                break
        return contexts

    def _dependency_contexts(
        self,
        repo_path: Path,
        index: RepoIndex,
        query: RetrievalQuery,
        stats: Dict[str, float],
    ) -> List[CodeContext]:
        if self.max_dependencies <= 0:
            return []
        contexts: List[CodeContext] = []
        for symbol in index.definitions_in_modules(
            query.imports, limit=max(self.max_dependencies * 2, self.max_dependencies)
        ):
            snippet = read_line_range(
                repo_path / symbol.file_path, symbol.start_line, symbol.end_line
            )
            if not snippet:
                continue
            contexts.append(
                CodeContext(
                    file_path=symbol.file_path,
                    reason=(
                        f"imported dependency `{symbol.qualname}` "
                        f"(lines {symbol.start_line}-{symbol.end_line})"
                    ),
                    snippet=snippet,
                    score=0.62,
                    kind="dependency",
                    symbol=symbol.name,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    context_id=make_context_id(
                        symbol.file_path,
                        symbol.start_line,
                        symbol.end_line,
                        "dependency",
                    ),
                )
            )
            stats["index_dependency_hits"] += 1
            if len(contexts) >= self.max_dependencies:
                break
        return contexts


def _dedupe(contexts: Sequence[CodeContext]) -> List[CodeContext]:
    best: Dict[tuple, CodeContext] = {}
    for context in contexts:
        key = (
            context.file_path,
            context.start_line,
            context.end_line,
            context.kind,
        )
        existing = best.get(key)
        if existing is None or context.score > existing.score:
            best[key] = context
    return list(best.values())
