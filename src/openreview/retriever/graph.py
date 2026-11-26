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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from openreview.repo.diff_parser import DiffParser
from openreview.retriever import ast_utils
from openreview.retriever.ast_utils import FileLineCache
from openreview.retriever.code_context import (
    CodeContext,
    ContextPriority,
    dedupe_contexts,
    make_context_id,
)
from openreview.retriever.index import RepoIndex, module_name
from openreview.retriever.query import RetrievalQuery

logger = logging.getLogger(__name__)

MAX_CALLER_CONTEXTS = 8
MAX_CALLEE_CONTEXTS = 8
MAX_DEPENDENCY_CONTEXTS = 6
CALL_SITE_RADIUS = 5
# 同文件/同包的短小私有 helper 直接展开完整函数体：签名往往不足以判断
# 调用语义，而几十行的成本可控。
MAX_PRIVATE_HELPER_LINES = 25


@dataclass(frozen=True)
class _FileImports:
    modules: frozenset[str]
    symbols: frozenset[str]
    # Go: package name/alias -> imported module directories.
    package_modules: Dict[str, frozenset[str]] = field(default_factory=dict)


_EMPTY_IMPORTS = _FileImports(frozenset(), frozenset())


class GraphRetriever:
    """用 import/call 关系补充跨文件上下文。"""

    def __init__(
        self,
        max_callers: int = MAX_CALLER_CONTEXTS,
        max_callees: int = MAX_CALLEE_CONTEXTS,
        max_dependencies: int = MAX_DEPENDENCY_CONTEXTS,
        python_calls_only: bool = False,
        line_cache: Optional[FileLineCache] = None,
    ):
        self.max_callers = max(0, max_callers)
        self.max_callees = max(0, max_callees)
        self.max_dependencies = max(0, max_dependencies)
        self.python_calls_only = python_calls_only
        self.line_cache = line_cache or FileLineCache()

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
            "index_dependency_hits",
            "graph_error",
        ):
            stats.setdefault(key, 0)

        contexts: List[CodeContext] = []
        name_cache: Dict[str, List] = {}
        if query.symbols:
            contexts.extend(
                self._collect(
                    "caller",
                    self._caller_contexts,
                    repo_path,
                    index,
                    query,
                    stats,
                    name_cache,
                )
            )
            contexts.extend(
                self._collect(
                    "callee", self._callee_contexts, repo_path, index, query, stats
                )
            )
        # P6 只保留“变更模块 import 了哪些仓库内定义”。反向的“谁 import 了
        # 变更模块”噪声大且与调用点重复，已移除。
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
        # 图检索内不同 kind 是不同证据（同一段代码可能既是 import 行也是
        # 调用点），这里按 kind 去重，跨机制的去重交给 Hybrid/planner。
        return dedupe_contexts(contexts, include_kind=True)

    @staticmethod
    def _collect(
        label: str,
        collector,
        *args,
    ) -> List[CodeContext]:
        """单项图检索失败时只记录错误，不影响其他类型上下文。"""
        try:
            return collector(*args)
        except Exception as exc:  # noqa: BLE001 - 图检索是增强，失败不能阻断 review
            stats = args[3] if len(args) > 3 else {}
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
        name_cache: Optional[Dict[str, List]] = None,
    ) -> List[CodeContext]:
        if self.max_callers <= 0:
            return []
        # 只有签名/返回值变更时，调用点才需要作为证据注入；否则完整调用方
        # 函数只会占用预算，和 diff 审查目标无关。
        targets = query.signature_changed_symbols
        if not targets:
            return []

        contexts: List[CodeContext] = []
        # 同一个调用方的多个调用点只注入一次：以“文件 + 调用方符号”去重，
        # 而不是按行号范围，否则同一个函数里的 3 次调用会占 3 份预算。
        seen: set[tuple[str, str]] = set()
        edges = index.callers_of(
            targets, limit=max(self.max_callers * 8, 32)
        )
        imports_by_file = self._imports_for_files(
            index, [edge.caller_file for edge in edges]
        )
        for edge in edges:
            if edge.caller_file.endswith(".go"):
                if not self._accept_go_caller(
                    edge, index, imports_by_file, name_cache if name_cache is not None else {}
                ):
                    continue
            elif self.python_calls_only:
                continue
            if DiffParser.is_low_value_reference_path(edge.caller_file):
                continue
            symbol = edge.caller_symbol or edge.callee_name
            key = (edge.caller_file, symbol)
            if key in seen:
                continue
            seen.add(key)
            start = max(1, edge.line - CALL_SITE_RADIUS)
            end = edge.line + CALL_SITE_RADIUS
            snippet = self.line_cache.read_range(
                repo_path / edge.caller_file, start, end
            )
            if not snippet:
                continue
            contexts.append(
                CodeContext(
                    file_path=edge.caller_file,
                    reason=(
                        f"call site `{edge.callee_name}` at line {edge.line}; "
                        f"signature changed"
                    ),
                    snippet=snippet,
                    score=0.58,
                    kind="caller",
                    priority=ContextPriority.REFERENCE,
                    symbol=symbol,
                    related_changed_symbols=[edge.callee_name],
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
    def _go_definitions(
        index: RepoIndex, name: str, cache: Dict[str, List]
    ) -> List:
        if name in cache:
            return cache[name]
        definitions = [
            item
            for item in index.search_symbols([name], limit=32)
            if item.file_path.endswith(".go")
            and item.kind in {"function_declaration", "method_declaration"}
        ]
        cache[name] = definitions
        return definitions

    def _accept_go_caller(
        self,
        edge,
        index: RepoIndex,
        imports_by_file: Dict[str, "_FileImports"],
        name_cache: Dict[str, List],
    ) -> bool:
        """Only inject Go call sites we can attribute with reasonable confidence.

        Go has no type-checker here: a call like ``s.Do()`` exposes the receiver
        variable, not its type. Accept package-qualified calls, same-package
        calls, and receiver calls only when the method name is unambiguous in
        the repository; everything else would create high-noise caller
        contexts for names like ``Get``/``Do``/``String``.
        """
        if edge.caller_file.endswith("_test.go"):
            return False
        definitions = self._go_definitions(index, edge.callee_name, name_cache)
        if not definitions:
            return False
        qualifier = (edge.callee_qualifier or "").strip()
        if qualifier:
            imported = imports_by_file.get(edge.caller_file, _EMPTY_IMPORTS)
            target_modules = imported.package_modules.get(qualifier)
            if target_modules:
                # Require the callee definition to live in the imported package;
                # this keeps `log.Error` or a same-named local symbol out.
                return any(
                    module_name(definition.file_path) in target_modules
                    for definition in definitions
                )
        # Receiver/local-variable calls: only when the method name is unique.
        return len(definitions) == 1

    @staticmethod
    def _imports_for_files(
        index: RepoIndex, files: Sequence[str]
    ) -> Dict[str, "_FileImports"]:
        """Batch ``imports_of`` by caller file instead of one SQL per edge."""
        unique = sorted({file for file in files if file})
        if not unique:
            return {}
        imports_of = getattr(index, "imports_of", None)
        if not callable(imports_of):
            return {}
        try:
            edges = imports_of(unique)
        except Exception as exc:  # noqa: BLE001 - import 图失败不应阻断 callee 检索
            logger.debug("Batched import lookup failed: %s", exc)
            return {}

        modules: Dict[str, set[str]] = {}
        symbols: Dict[str, set[str]] = {}
        package_modules: Dict[str, Dict[str, set[str]]] = {}
        for item in edges:
            modules.setdefault(item.src_file, set()).add(item.dst_module)
            if item.symbol:
                symbols.setdefault(item.src_file, set()).add(item.symbol)
            if item.src_file.endswith(".go") and item.dst_module:
                package = item.symbol or Path(item.dst_module).name
                if package:
                    package_modules.setdefault(item.src_file, {}).setdefault(
                        package, set()
                    ).add(item.dst_module)
        return {
            file: _FileImports(
                frozenset(modules.get(file, ())),
                frozenset(symbols.get(file, ())),
                {
                    package: frozenset(destinations)
                    for package, destinations in package_modules.get(
                        file, {}
                    ).items()
                },
            )
            for file in unique
        }

    @staticmethod
    def _resolve_definition(
        edge,
        definitions: Sequence,
        imported: "_FileImports",
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

        def _rank(definition) -> float:
            score = float(getattr(definition, "score", 0.0) or 0.0)
            if definition.file_path == edge.caller_file:
                score += 3.0
            elif (
                definition.file_path.endswith(".go")
                and edge.caller_file.endswith(".go")
                and Path(definition.file_path).parent
                == Path(edge.caller_file).parent
            ):
                score += 2.5
            if module_name(definition.file_path) in imported.modules:
                score += 2.0
            if definition.name in imported.symbols:
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
        edges = index.callees_of(
            query.symbols, limit=max(self.max_callees * 8, 32)
        )
        imports_by_file = self._imports_for_files(
            index, [edge.caller_file for edge in edges]
        )
        for edge in edges:
            if self.python_calls_only and not edge.caller_file.endswith(".py"):
                continue
            imported = imports_by_file.get(edge.caller_file, _EMPTY_IMPORTS)
            definitions = [
                item
                for item in index.search_symbols([edge.callee_name], limit=16)
                if item.name == edge.callee_name
            ]
            definition = self._resolve_definition(edge, definitions, imported)
            if definition is None:
                continue
            if DiffParser.is_low_value_reference_path(definition.file_path):
                continue
            if not self._is_local_callee(edge, definition, imported):
                # 标准库 / 第三方调用没有仓库内定义，直接跳过；同名仓库符号
                # 的误匹配也在这里被 import 关系过滤掉。
                continue
            key = (definition.file_path, definition.start_line)
            if key in seen:
                continue
            seen.add(key)
            expanded = self._should_expand_helper(edge, definition)
            snippet = (
                self.line_cache.read_range(
                    repo_path / definition.file_path,
                    definition.start_line,
                    definition.end_line,
                )
                if expanded
                else self._callee_signature_snippet(repo_path, definition)
            )
            if not snippet:
                continue
            contexts.append(
                CodeContext(
                    file_path=definition.file_path,
                    reason=(
                        f"callee `{definition.qualname}` "
                        + (
                            f"full body of short private helper "
                            f"(lines {definition.start_line}-{definition.end_line})"
                            if expanded
                            else f"signature "
                            f"(lines {definition.start_line}-{definition.end_line})"
                        )
                    ),
                    snippet=snippet,
                    score=0.72,
                    kind="callee",
                    priority=ContextPriority.DEFINITION,
                    symbol=definition.name,
                    related_changed_symbols=[
                        name
                        for name in query.symbols
                        if edge.caller_symbol == name
                        or edge.caller_symbol.endswith(f".{name}")
                    ],
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

    @staticmethod
    def _is_local_callee(edge, definition, imported: "_FileImports") -> bool:
        """只保留“仓库内、且调用方确实 import 了该模块”的 callee。

        这是标准库/第三方过滤的核心：索引里没有定义的名字自然被丢弃；
        同名定义则通过 import 关系消歧，避免把 ``json.loads`` 误判成仓库
        里另一个 ``loads``。
        """
        if definition.file_path == edge.caller_file:
            return True
        # Go 的包边界就是目录：同目录不同文件仍属于同一个 package。
        if (
            definition.file_path.endswith(".go")
            and edge.caller_file.endswith(".go")
            and Path(definition.file_path).parent
            == Path(edge.caller_file).parent
        ):
            return True
        if not imported.modules:
            return False
        target = module_name(definition.file_path)
        for module in imported.modules:
            if not module:
                continue
            if module == target:
                return True
            if target.startswith(module + ".") or module.startswith(target + "."):
                return True
        return False

    @staticmethod
    def _should_expand_helper(edge, definition) -> bool:
        """同文件/同包的短小私有 helper 才展开完整函数体。"""
        name = definition.name or ""
        if not name:
            return False
        is_private = name.startswith("_") or name[0].islower()
        if not is_private:
            return False
        if definition.end_line - definition.start_line + 1 > MAX_PRIVATE_HELPER_LINES:
            return False
        if definition.file_path == edge.caller_file:
            return True
        # Go 的包边界就是目录；Python 同目录也视为同包（保守近似）。
        return (
            Path(definition.file_path).parent
            == Path(edge.caller_file).parent
        )

    def _callee_signature_snippet(self, repo_path: Path, definition) -> str:
        """只保留签名 + docstring + raise 语句，而不是完整函数体。"""
        path = repo_path / definition.file_path
        # Reuse the run-scoped line cache: the same file is often read again
        # for the full body or for neighbouring call sites.
        lines = self.line_cache.lines(path)
        if not lines:
            return definition.signature or ""
        parsed = ast_utils.parse_text("\n".join(lines), path.suffix)
        if parsed is None:
            return definition.signature or ""
        _, source, tree = parsed
        node = ast_utils.find_enclosing_definition(tree, definition.start_line - 1)
        if node is None:
            return definition.signature or ""

        root = node
        if root.parent is not None and root.parent.type == "decorated_definition":
            root = root.parent
        body = node.child_by_field_name("body")
        header_end = body.start_point[0] if body is not None else node.end_point[0] + 1
        # Go 常见单行函数：body 与签名同在一行，此时至少保留签名这一行。
        header_end = max(root.start_point[0] + 1, header_end)
        header = "\n".join(lines[root.start_point[0] : header_end])

        parts = [header]
        doc = definition.doc or ast_utils.definition_doc(node, source)
        if doc:
            parts.append(f'"""{doc.strip()}"""')

        raises: List[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "raise_statement":
                start = current.start_point[0]
                end = current.end_point[0]
                raises.append("\n".join(lines[start : end + 1]))
                if len(raises) >= 5:
                    break
                continue
            stack.extend(current.children)
        if raises:
            parts.append("\n".join(raises))
        return "\n".join(part for part in parts if part).strip()

    # ------------------------------------------------------------------
    # import graph
    # ------------------------------------------------------------------
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
            if DiffParser.is_low_value_reference_path(symbol.file_path):
                continue
            snippet = self.line_cache.read_range(
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
                    priority=ContextPriority.DEFINITION,
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
