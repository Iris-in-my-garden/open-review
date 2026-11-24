"""本地单文件 AST 检索。

职责边界刻意收窄：

- P2：变更行所属函数/类的完整 AST 片段（超长时折叠，保留 changed lines）
- P3：删除符号在 base revision 同文件中的历史定义
- P5：被变更方法引用的类/结构体字段与模块常量
- P6：按需提取真正引用变更符号的关联测试函数（最多 2 个）

跨文件引用、import/call graph 由 ``HybridRetriever`` + ``GraphRetriever``
负责。这里的全局 grep 只在索引或图不可用（``allow_global_fallback=True``）
时启用，作为最后兜底，而不是正常路径。
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from tree_sitter import Language, Node, Parser, Tree

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever import ast_utils
from openreview.retriever.code_context import (
    CodeContext,
    ContextPriority,
    make_context_id,
)
from openreview.retriever.folding import fold_snippet
from openreview.retriever.globals import (
    DEFAULT_FIELD_WINDOW_RADIUS,
    FieldDefinition,
    collect_referenced_fields_from_tree,
)
from openreview.retriever.grep import search_symbols
from openreview.retriever.pathing import is_source_file
from openreview.review.token_budget import estimate_tokens

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 500 * 1024  # 500 KB 限制
# 仅用于索引崩溃时的全局 grep 兜底。
MAX_SEARCH_TARGETS = 50
MAX_REFERENCE_CONTEXTS = 200
MAX_TEST_FUNCTION_CONTEXTS = 2
# 单个 enclosing 方法超过该 token 数时做 AST 折叠；折叠会保留全部变更行。
MAX_ENCLOSING_SNIPPET_TOKENS = 1600
# 函数体小于该行数且整段都已出现在 diff hunk 里时，P2 片段完全是重复内容。
MIN_ENCLOSING_BODY_LINES = 5

# 打分：变更行所在的函数/类 > 符号定义 > 关联测试 > 符号引用 > base 侧历史代码
SCORE_ENCLOSING = 0.95
SCORE_DEFINITION = 0.90
SCORE_TEST_FUNCTION = 0.85
# 兼容旧名字：测试上下文不再注入整个文件，但分数语义不变。
SCORE_TEST_FILE = SCORE_TEST_FUNCTION
SCORE_REFERENCE = 0.65
SCORE_BASE_DEFINITION = 0.80
SCORE_BASE_REFERENCE = 0.55

DEFINITION_TYPES = {
    "function_definition", "class_definition", "method_definition",  # Python
    "function_declaration", "method_declaration", "type_declaration",  # Golang
}
TEST_PREFIXES = {
    ".py": ("test_",),
    ".go": ("Test", "Benchmark", "Example", "Fuzz"),
}

LoadedFile = Tuple[str, List[str], bytes, Tree]
# (file, start_row, end_row, revision)：base/head 同一行范围是不同证据。
NodeRangeKey = Tuple[str, int, int, str]


def _symbol_search_pattern(symbols: Set[str]) -> Optional[re.Pattern[str]]:
    """Compile one alternation for "does this body reference any symbol".

    ``_collect_test_contexts`` used to run one regex per symbol for every test
    function. Large linked test files with many symbols turned that into tens
    of thousands of scans; a single alternation keeps it linear.
    """
    targets = sorted(
        {
            target
            for symbol in symbols
            if symbol and (target := symbol.rsplit(".", 1)[-1])
        },
        key=len,
        reverse=True,
    )
    if not targets:
        return None
    return re.compile(
        r"\b(?:" + "|".join(re.escape(target) for target in targets) + r")\b"
    )



class TreeSitterLocalRetriever:
    """纯本地、单文件的 Tree-sitter 结构检索器。"""

    def __init__(self):
        self.parsers = {}
        # 供 HybridRetriever 汇总：AST 层自己的截断/目标裁剪也应该可见。
        self.last_stats: Dict[str, float] = {}

        try:
            import tree_sitter_python as tspython

            self.parsers[".py"] = Parser(Language(tspython.language()))
        except ImportError:
            pass

        try:
            import tree_sitter_go as tsgo

            self.parsers[".go"] = Parser(Language(tsgo.language()))
        except ImportError:
            pass

    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
        *,
        allow_global_fallback: bool = False,
    ) -> List[CodeContext]:
        repo_path = Path(repo_path).resolve()
        self.last_stats = {}
        if not repo_path.exists() or not repo_path.is_dir():
            return []

        # 同一次检索内复用已读取/已解析的文件，避免同一文件被反复 parse
        file_cache: Dict[Path, Optional[LoadedFile]] = {}
        seen_node_ranges: Set[NodeRangeKey] = set()

        # P2：变更行所在的函数/类。即使 PR 只改函数体、没有改签名，
        #     也能拿到完整上下文。
        enclosing_contexts, definition_names = self._collect_changed_line_contexts(
            repo_path, parsed_diff, seen_node_ranges, file_cache
        )
        # P5：只补充被变更方法引用的类/结构体字段和模块常量。
        field_contexts = self._collect_field_contexts(
            repo_path, parsed_diff, file_cache
        )
        # Base：同文件旧版本中的被删除定义，用于历史对比。
        base_contexts = self._collect_base_contexts(
            repo_path, parsed_diff, base_path, seen_node_ranges, file_cache
        )
        # Test：只提取真正引用变更符号的测试函数，而不是整个测试文件。
        test_contexts = self._collect_test_contexts(
            repo_path, parsed_diff, definition_names, seen_node_ranges, file_cache
        )

        contexts: List[CodeContext] = [
            *enclosing_contexts,
            *field_contexts,
            *base_contexts,
            *test_contexts,
        ]

        # 索引不可用时的降级兜底：现场 grep 找跨文件引用。
        if allow_global_fallback:
            targets = self._extract_search_targets(parsed_diff) | definition_names
            contexts.extend(
                self._search_references(
                    repo_path, targets, seen_node_ranges, file_cache
                )
            )
            self.last_stats["grep_fallback"] = 1

        contexts.sort(key=lambda item: item.score, reverse=True)
        self.last_stats["ast_contexts"] = len(contexts)
        logger.debug("Retrieved %d local AST contexts", len(contexts))
        return contexts

    # ------------------------------------------------------------------
    # P2 enclosing
    # ------------------------------------------------------------------
    def _collect_changed_line_contexts(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        seen_node_ranges: Set[NodeRangeKey],
        file_cache: Dict[Path, Optional[LoadedFile]],
    ) -> Tuple[List[CodeContext], Set[str]]:
        """把每个变更行映射到其所在的函数/类，并抽取完整 AST 片段。"""
        contexts: List[CodeContext] = []
        definition_names: Set[str] = set()

        for file_diff in parsed_diff.files:
            if not file_diff.changed_lines or file_diff.status == "removed":
                continue
            if Path(file_diff.filename).suffix not in self.parsers:
                continue

            abs_path = self._resolve_repo_file(repo_path, file_diff.filename)
            if abs_path is None:
                continue

            loaded = self._load_file(abs_path, file_cache)
            if loaded is None:
                continue

            _, lines, source, tree = loaded
            text = "\n".join(lines)
            for line_number in file_diff.changed_lines:
                target_idx = line_number - 1
                if target_idx < 0 or target_idx >= len(lines):
                    continue

                node = self._find_enclosing_definition(tree.root_node, target_idx)
                if node is None:
                    continue

                name = self._definition_name(node, source)
                if name:
                    definition_names.add(name)

                start_row, end_row = node.start_point[0], node.end_point[0]
                # 微小型 enclosing 如果整段都已出现在 hunk 上下文里，
                # 注入 P2 只是重复 diff 内容，直接跳过。
                if self._is_fully_visible_tiny_enclosing(
                    file_diff, start_row, end_row
                ):
                    self.last_stats["tiny_enclosing_skipped"] = (
                        self.last_stats.get("tiny_enclosing_skipped", 0) + 1
                    )
                    continue

                unique_key = (
                    file_diff.filename,
                    start_row,
                    end_row,
                    "head",
                )
                if unique_key in seen_node_ranges:
                    continue
                seen_node_ranges.add(unique_key)

                snippet = "\n".join(lines[start_row : end_row + 1])
                folded = False
                if estimate_tokens(snippet) > MAX_ENCLOSING_SNIPPET_TOKENS:
                    snippet, folded = fold_snippet(
                        text,
                        Path(file_diff.filename).suffix,
                        start_row + 1,
                        end_row + 1,
                        file_diff.changed_lines,
                        MAX_ENCLOSING_SNIPPET_TOKENS,
                    )
                    if folded:
                        self.last_stats["folded_contexts"] = (
                            self.last_stats.get("folded_contexts", 0) + 1
                        )
                display_name = name or "unknown"
                reason = (
                    f"Enclosing {node.type} '{display_name}' of changed line "
                    f"{line_number} (lines {start_row + 1}-{end_row + 1})"
                )
                if folded:
                    reason += "; long body folded, changed lines preserved"
                contexts.append(
                    CodeContext(
                        file_path=file_diff.filename,
                        reason=reason,
                        snippet=snippet,
                        score=SCORE_ENCLOSING,
                        kind="enclosing",
                        priority=ContextPriority.ENCLOSING,
                        folded=folded,
                        symbol=display_name if name else "",
                        related_changed_symbols=[name] if name else [],
                        start_line=start_row + 1,
                        end_line=end_row + 1,
                        context_id=make_context_id(
                            file_diff.filename,
                            start_row + 1,
                            end_row + 1,
                            "enclosing",
                        ),
                    )
                )

        return contexts, definition_names

    @staticmethod
    def _is_fully_visible_tiny_enclosing(
        file_diff: ParsedChangedFile,
        start_row: int,
        end_row: int,
    ) -> bool:
        """函数体 < ``MIN_ENCLOSING_BODY_LINES`` 且整段都在 hunk 上下文里。"""
        body_lines = end_row - start_row  # 去掉声明行
        if body_lines >= MIN_ENCLOSING_BODY_LINES:
            return False
        visible = set(file_diff.visible_lines or [])
        if not visible:
            return False
        # start_row/end_row 是 0-based；visible_lines 是 1-based。
        return all(
            line in visible for line in range(start_row + 1, end_row + 2)
        )

    # ------------------------------------------------------------------
    # P5 fields
    # ------------------------------------------------------------------
    def _collect_field_contexts(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        file_cache: Dict[Path, Optional[LoadedFile]],
    ) -> List[CodeContext]:
        """P5：收集变更行窗口内引用到的类/结构体字段与模块常量。

        P0 起 P5 不再扫描整个变更函数：
        - 测试文件直接跳过；
        - 只扫描 changed line 附近的窗口；
        - 同一 struct 的多个字段合并成一个 context。
        """
        contexts: List[CodeContext] = []

        for file_diff in parsed_diff.files:
            if not file_diff.changed_lines or file_diff.status == "removed":
                continue
            # 测试文件的 P5 价值极低，且很容易把整张测试表带进 prompt。
            if file_diff.is_test_file:
                continue
            suffix = Path(file_diff.filename).suffix
            if suffix not in self.parsers:
                continue
            abs_path = self._resolve_repo_file(repo_path, file_diff.filename)
            if abs_path is None:
                continue
            loaded = self._load_file(abs_path, file_cache)
            if loaded is None:
                continue

            text, _, source, tree = loaded
            fields: List[FieldDefinition] = []
            seen_fields: Set[Tuple[str, str, int, int]] = set()
            # Per-file cache: Go field candidates and package type maps depend
            # only on the tree, so reuse them across all changed-line scopes.
            field_cache: Dict[str, Any] = {}
            # Group changed lines by enclosing definition. Calling the field
            # collector once per line re-scanned the whole tree (and rebuilt
            # the Go type graph) for every line; a generated 480 KB Go file
            # with ~100 changed lines made this the dominant retrieval cost.
            field_lines: Dict[Tuple[int, int], List[int]] = {}
            for line_number in file_diff.changed_lines:
                node = ast_utils.find_enclosing_definition(
                    tree, max(0, line_number - 1)
                )
                scope_key = (
                    (node.start_point[0], node.end_point[0])
                    if node is not None
                    else (line_number, line_number)
                )
                field_lines.setdefault(scope_key, []).append(line_number)

            for group_lines in field_lines.values():
                for field in collect_referenced_fields_from_tree(
                    text,
                    source,
                    tree,
                    suffix,
                    min(group_lines),
                    max(group_lines),
                    changed_lines=group_lines,
                    window_radius=DEFAULT_FIELD_WINDOW_RADIUS,
                    cache=field_cache,
                ):
                    key = (
                        field.parent_type,
                        field.name,
                        field.start_line,
                        field.end_line,
                    )
                    if key in seen_fields:
                        continue
                    seen_fields.add(key)
                    fields.append(field)

            contexts.extend(
                self._build_field_contexts(file_diff.filename, fields)
            )
        return contexts

    @staticmethod
    def _build_field_contexts(
        file_path: str,
        fields: Sequence[FieldDefinition],
    ) -> List[CodeContext]:
        """把同一 struct 的分散字段合并为一个 P5 context。"""
        if not fields:
            return []

        contexts: List[CodeContext] = []
        by_parent: Dict[str, List[FieldDefinition]] = {}
        for field in fields:
            if field.parent_type:
                by_parent.setdefault(field.parent_type, []).append(field)
            else:
                contexts.append(
                    TreeSitterLocalRetriever._field_context(
                        file_path, field, field.name, field.source
                    )
                )

        for parent_type, group in by_parent.items():
            group.sort(key=lambda item: item.start_line)
            if len(group) == 1:
                field = group[0]
                snippet = field.source
                reason = (
                    f"field `{field.name}` of `{parent_type}` referenced "
                    f"by changed function"
                )
            else:
                snippet = (
                    f"type {parent_type} struct {{\n"
                    + "\n".join(field.source for field in group)
                    + "\n}"
                )
                reason = (
                    f"fields of `{parent_type}` referenced by changed function"
                )
            contexts.append(
                TreeSitterLocalRetriever._field_context(
                    file_path,
                    group[0],
                    parent_type,
                    snippet,
                    reason=reason,
                    end_line=max(field.end_line for field in group),
                )
            )
        return contexts

    @staticmethod
    def _field_context(
        file_path: str,
        field: FieldDefinition,
        symbol: str,
        snippet: str,
        *,
        reason: str = "",
        end_line: Optional[int] = None,
    ) -> CodeContext:
        return CodeContext(
            file_path=file_path,
            reason=reason or f"field `{field.name}` referenced by changed function",
            snippet=snippet,
            score=0.4,
            kind="field",
            priority=ContextPriority.FIELD,
            symbol=symbol,
            start_line=field.start_line,
            end_line=end_line or field.end_line,
            context_id=make_context_id(
                file_path,
                field.start_line,
                end_line or field.end_line,
                "field",
                fallback=symbol,
            ),
        )

    # ------------------------------------------------------------------
    # Base revision (same file only)
    # ------------------------------------------------------------------
    def _collect_base_contexts(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path],
        seen_node_ranges: Set[NodeRangeKey],
        file_cache: Dict[Path, Optional[LoadedFile]],
    ) -> List[CodeContext]:
        """在 base revision 的同名文件中查找被删除的本地定义。"""
        if base_path is None:
            return []
        base_root = Path(base_path).resolve()
        if not base_root.is_dir():
            return []

        contexts: List[CodeContext] = []
        for file_diff in parsed_diff.files:
            removed = {
                symbol.strip()
                for symbol in (file_diff.removed_symbols or [])
                if symbol and symbol.strip()
            }
            if not removed:
                continue
            suffix = Path(file_diff.filename).suffix
            if suffix not in self.parsers:
                continue

            base_file = self._resolve_repo_file(base_root, file_diff.filename)
            if base_file is None:
                continue
            loaded = self._load_file(base_file, file_cache)
            if loaded is None:
                continue

            _, lines, source, tree = loaded
            for node in self._iter_definitions(tree.root_node):
                qualname = ast_utils.definition_qualname(node, source)
                name = self._definition_name(node, source)
                if not name:
                    continue
                if name not in removed and qualname not in removed:
                    continue

                start_row, end_row = node.start_point[0], node.end_point[0]
                key = (file_diff.filename, start_row, end_row, "base")
                if key in seen_node_ranges:
                    continue
                seen_node_ranges.add(key)
                contexts.append(
                    CodeContext(
                        file_path=file_diff.filename,
                        reason=(
                            f"[base revision] removed definition "
                            f"`{qualname or name}` "
                            f"(lines {start_row + 1}-{end_row + 1})"
                        ),
                        snippet="\n".join(lines[start_row : end_row + 1]),
                        score=SCORE_BASE_DEFINITION,
                        revision="base",
                        kind="base_definition",
                        priority=ContextPriority.DEFINITION,
                        symbol=name,
                        related_changed_symbols=[name],
                        start_line=start_row + 1,
                        end_line=end_row + 1,
                        context_id=make_context_id(
                            file_diff.filename,
                            start_row + 1,
                            end_row + 1,
                            "base_definition",
                            revision="base",
                        ),
                    )
                )
        return contexts

    # ------------------------------------------------------------------
    # Linked test functions
    # ------------------------------------------------------------------
    def _collect_test_contexts(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        enclosing_names: Set[str],
        seen_node_ranges: Set[NodeRangeKey],
        file_cache: Dict[Path, Optional[LoadedFile]],
    ) -> List[CodeContext]:
        """只提取引用变更符号的测试函数，而不是整个测试文件。"""
        symbols = self._extract_search_targets(parsed_diff) | enclosing_names
        if not symbols:
            return []
        symbol_pattern = _symbol_search_pattern(symbols)

        source_files = [
            file_diff.filename
            for file_diff in parsed_diff.files
            if file_diff.filename
            and not file_diff.is_test_file
            and not file_diff.is_docs_file
            and not file_diff.is_config_file
        ]
        if not source_files:
            return []

        contexts: List[CodeContext] = []
        for source_file in sorted(set(source_files)):
            for test_file in self._linked_test_candidates(repo_path, source_file):
                abs_path = self._resolve_repo_file(repo_path, test_file)
                if abs_path is None:
                    continue
                loaded = self._load_file(abs_path, file_cache)
                if loaded is None:
                    continue

                _, lines, source, tree = loaded
                for node, name in self._iter_test_functions(tree, source, test_file):
                    if not self._references_any(node, source, symbol_pattern):
                        continue
                    key = (
                        test_file,
                        node.start_point[0],
                        node.end_point[0],
                        "head",
                    )
                    if key in seen_node_ranges:
                        continue
                    seen_node_ranges.add(key)

                    start_row, end_row = node.start_point[0], node.end_point[0]
                    matched = self._matched_symbol(node, source, symbols, name)
                    contexts.append(
                        CodeContext(
                            file_path=test_file,
                            reason=(
                                f"Linked test `{name}` references "
                                f"changed symbol `{matched}`"
                            ),
                            snippet="\n".join(lines[start_row : end_row + 1]),
                            score=SCORE_TEST_FUNCTION,
                            revision="head",
                            kind="test",
                            priority=ContextPriority.TEST,
                            symbol=name,
                            related_changed_symbols=[matched] if matched else [],
                            start_line=start_row + 1,
                            end_line=end_row + 1,
                            context_id=make_context_id(
                                test_file,
                                start_row + 1,
                                end_row + 1,
                                "test",
                                revision="head",
                            ),
                        )
                    )
                    if len(contexts) >= MAX_TEST_FUNCTION_CONTEXTS:
                        self.last_stats["test_functions_truncated"] = (
                            self.last_stats.get("test_functions_truncated", 0) + 1
                        )
                        return contexts
        return contexts

    def _linked_test_candidates(
        self, repo_path: Path, source_file: str
    ) -> List[str]:
        """按常见命名约定推导测试文件路径，不做全仓扫描。"""
        path = Path(source_file)
        suffix = path.suffix
        if suffix == ".py":
            names = [f"test_{path.stem}.py", f"{path.stem}_test.py"]
        elif suffix == ".go":
            names = [f"{path.stem}_test.go"]
        else:
            return []

        directories = [
            path.parent,
            path.parent / "tests",
            path.parent / "test",
            Path("tests"),
            Path("test"),
            Path("tests") / path.parent.name,
            Path("test") / path.parent.name,
        ]
        candidates: List[str] = []
        for directory in directories:
            for name in names:
                candidate = (directory / name).as_posix()
                if candidate.startswith("./"):
                    candidate = candidate[2:]
                if candidate not in candidates:
                    candidates.append(candidate)
        for name in names:
            if name not in candidates:
                candidates.append(name)

        existing: List[str] = []
        for candidate in candidates:
            resolved = self._resolve_repo_file(repo_path, candidate)
            if resolved is not None and resolved.is_file():
                existing.append(candidate)
        return existing

    def _iter_test_functions(
        self, tree: Tree, source: bytes, file_path: str
    ) -> Iterable[Tuple[Node, str]]:
        prefixes = TEST_PREFIXES.get(Path(file_path).suffix, ())
        if not prefixes:
            return
        for node in self._iter_functions(tree.root_node):
            name = self._definition_name(node, source)
            if name and name.startswith(prefixes):
                yield node, name

    def _iter_functions(self, root: Node) -> Iterable[Node]:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in {
                "function_definition",
                "function_declaration",
                "method_declaration",
            }:
                yield node
            stack.extend(reversed(node.children))

    @staticmethod
    def _references_any(
        node: Node, source: bytes, pattern: Optional[re.Pattern[str]]
    ) -> bool:
        if pattern is None:
            return False
        return bool(pattern.search(ast_utils.node_text(node, source)))

    def _matched_symbol(
        self,
        node: Node,
        source: bytes,
        symbols: Set[str],
        test_name: str,
    ) -> str:
        body = ast_utils.node_text(node, source)
        for symbol in sorted(symbols, key=len, reverse=True):
            target = symbol.rsplit(".", 1)[-1]
            if target and re.search(rf"\b{re.escape(target)}\b", body):
                return symbol
        for symbol in sorted(symbols, key=len, reverse=True):
            target = symbol.rsplit(".", 1)[-1]
            if target and target.lower() in test_name.lower():
                return symbol
        return "unknown"

    # ------------------------------------------------------------------
    # Global grep fallback (index unavailable)
    # ------------------------------------------------------------------
    def _extract_search_targets(self, parsed_diff: ParsedDiff) -> Set[str]:
        """从 Diff 中收集所有变更/删除符号与 Hunk 上下文符号。"""
        targets: Set[str] = set()
        for file_diff in parsed_diff.files:
            targets.update(file_diff.changed_symbols or [])
            targets.update(file_diff.removed_symbols or [])
            targets.update(file_diff.enclosing_contexts or [])
        return {target.strip() for target in targets if target and target.strip()}

    def _search_references(
        self,
        repo_path: Path,
        targets: Set[str],
        seen_node_ranges: Set[NodeRangeKey],
        file_cache: Dict[Path, Optional[LoadedFile]],
        revision: str = "head",
    ) -> List[CodeContext]:
        """索引不可用时的最后兜底：现场 grep 找跨文件定义/引用。"""
        contexts: List[CodeContext] = []

        # 超长 PR 必须截断时，优先保留更长、更具体的符号。
        valid_targets = sorted(
            (target for target in targets if target and target.strip()),
            key=lambda target: (-len(target), target),
        )
        if not valid_targets:
            return []

        if len(valid_targets) > MAX_SEARCH_TARGETS:
            logger.warning(
                "Too many search targets (%d), truncating to %d",
                len(valid_targets),
                MAX_SEARCH_TARGETS,
            )
            self.last_stats["search_targets_truncated"] = (
                self.last_stats.get("search_targets_truncated", 0)
                + len(valid_targets)
                - MAX_SEARCH_TARGETS
            )
            valid_targets = valid_targets[:MAX_SEARCH_TARGETS]

        search = search_symbols(
            repo_path,
            valid_targets,
            suffixes=tuple(self.parsers),
        )
        self.last_stats["grep_hits"] = (
            self.last_stats.get("grep_hits", 0) + len(search.hits)
        )
        logger.debug(
            "reference search engine=%s hits=%d timeouts=%d",
            search.engine,
            len(search.hits),
            search.timeouts,
        )
        self.last_stats["grep_timeouts"] = (
            self.last_stats.get("grep_timeouts", 0) + search.timeouts
        )
        if search.truncated:
            self.last_stats["grep_hits_capped"] = (
                self.last_stats.get("grep_hits_capped", 0) + 1
            )

        for hit_index, (rel_path_str, line_number, _) in enumerate(search.hits):
            try:
                if not self._is_valid_source_file(rel_path_str):
                    continue

                abs_file_path = self._resolve_repo_file(repo_path, rel_path_str)
                if abs_file_path is None:
                    continue

                (
                    snippet,
                    reason,
                    is_def,
                    node_range,
                    matched_symbol,
                ) = self._extract_snippet_with_ast(
                    abs_file_path, line_number, valid_targets, file_cache
                )

                if not snippet or not node_range:
                    continue

                unique_key = (
                    rel_path_str,
                    node_range[0],
                    node_range[1],
                    revision,
                )
                if unique_key in seen_node_ranges:
                    continue
                seen_node_ranges.add(unique_key)

                if revision == "base":
                    score = (
                        SCORE_BASE_DEFINITION if is_def else SCORE_BASE_REFERENCE
                    )
                    reason = f"[base revision] {reason}"
                else:
                    score = SCORE_DEFINITION if is_def else SCORE_REFERENCE

                kind = "definition" if is_def else "reference"
                contexts.append(
                    CodeContext(
                        file_path=rel_path_str,
                        reason=reason,
                        snippet=snippet,
                        score=score,
                        revision=revision,
                        kind=kind,
                        priority=(
                            ContextPriority.DEFINITION
                            if is_def
                            else ContextPriority.REFERENCE
                        ),
                        symbol=matched_symbol,
                        related_changed_symbols=(
                            [matched_symbol] if matched_symbol else []
                        ),
                        start_line=node_range[0] + 1,
                        end_line=node_range[1] + 1,
                        context_id=make_context_id(
                            rel_path_str,
                            node_range[0] + 1,
                            node_range[1] + 1,
                            kind,
                            revision=revision,
                        ),
                    )
                )
                if len(contexts) >= MAX_REFERENCE_CONTEXTS:
                    self.last_stats["reference_contexts_truncated"] = (
                        self.last_stats.get("reference_contexts_truncated", 0)
                        + len(search.hits)
                        - hit_index
                        - 1
                    )
                    break
            except Exception as exc:  # noqa: BLE001 - 单行解析失败不应中断整体检索
                logger.debug(
                    "Failed to parse grep hit %s:%s: %s",
                    rel_path_str,
                    line_number,
                    exc,
                )
                continue

        return contexts

    def _extract_snippet_with_ast(
        self,
        file_path: Path,
        line_number: int,
        targets: List[str],
        file_cache: Dict[Path, Optional[LoadedFile]],
    ) -> Tuple[str, str, bool, Optional[Tuple[int, int]], str]:
        """利用 Tree-sitter 纯语法结构分析，截取边界并判定 Definition / Reference"""
        loaded = self._load_file(file_path, file_cache)
        if loaded is None:
            return "", "", False, None, ""

        _, lines, source, tree = loaded
        target_idx = line_number - 1
        if target_idx < 0 or target_idx >= len(lines):
            return "", "", False, None, ""

        target_line = lines[target_idx]
        target_node = self._find_enclosing_definition(tree.root_node, target_idx)

        if not target_node:
            start_row = max(0, target_idx - 7)
            end_row = min(len(lines) - 1, target_idx + 7)
            snippet = "\n".join(lines[start_row : end_row + 1])
            matched = next((t for t in targets if t in target_line), "unknown")
            return (
                snippet,
                f"Fallback snippet around line {line_number} for '{matched}'",
                False,
                (start_row, end_row),
                matched,
            )

        start_row, end_row = target_node.start_point[0], target_node.end_point[0]
        snippet = "\n".join(lines[start_row : end_row + 1])

        definition_name = self._definition_name(target_node, source)
        matched_target = next(
            (t for t in targets if t in target_line),
            definition_name or "unknown",
        )

        is_def = (
            definition_name in targets
            and target_node.start_point[0] == target_idx
        )

        reason = (
            f"AST-enclosed definition of '{matched_target}' "
            f"(lines {start_row + 1}-{end_row + 1})"
            if is_def
            else f"AST-enclosed reference to '{matched_target}' "
            f"(lines {start_row + 1}-{end_row + 1})"
        )
        return snippet, reason, is_def, (start_row, end_row), matched_target

    # ------------------------------------------------------------------
    # tree / file helpers
    # ------------------------------------------------------------------
    def _iter_definitions(self, root: Node) -> Iterable[Node]:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in DEFINITION_TYPES:
                yield node
            stack.extend(reversed(node.children))

    def _resolve_repo_file(
        self, repo_path: Path, rel_path_str: str
    ) -> Optional[Path]:
        """把仓库相对路径解析为绝对路径，并阻止 ../ 越界。"""
        candidate = (repo_path / rel_path_str).resolve()
        try:
            candidate.relative_to(repo_path)
        except ValueError:
            logger.warning("Skip path outside repository: %s", rel_path_str)
            return None
        return candidate

    def _load_file(
        self, file_path: Path, cache: Dict[Path, Optional[LoadedFile]]
    ) -> Optional[LoadedFile]:
        if file_path in cache:
            return cache[file_path]

        parser = self.parsers.get(file_path.suffix)
        if parser is None:
            cache[file_path] = None
            return None

        try:
            if not file_path.exists() or file_path.stat().st_size > MAX_FILE_SIZE:
                cache[file_path] = None
                return None
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            source = text.encode("utf-8")
            tree = parser.parse(source)
            loaded: LoadedFile = (text, text.splitlines(), source, tree)
            cache[file_path] = loaded
            return loaded
        except OSError as exc:
            logger.debug("Failed to read %s: %s", file_path, exc)
            cache[file_path] = None
            return None

    def _is_valid_source_file(self, rel_path_str: str) -> bool:
        """与索引/impact 共用同一套目录剪枝规则，避免检索口径漂移。"""
        return is_source_file(rel_path_str, self.parsers)

    @staticmethod
    def _definition_name(node: Node, source: bytes) -> Optional[str]:
        """读取定义节点的名字。Go 的 type_declaration 需要再下钻一层 type_spec。"""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for child in node.children:
                if child.type in {"type_spec", "type_alias"}:
                    name_node = child.child_by_field_name("name")
                    if name_node is not None:
                        break
        if name_node is None:
            return None
        return source[name_node.start_byte : name_node.end_byte].decode(
            "utf-8", "ignore"
        )

    def _find_enclosing_definition(
        self, node: Node, target_row: int
    ) -> Optional[Node]:
        """递归遍历 AST，寻找包含 target_row 的最小定义节点"""
        best_match = None

        def traverse(curr: Node):
            nonlocal best_match
            if curr.start_point[0] <= target_row <= curr.end_point[0]:
                if curr.type in DEFINITION_TYPES:
                    best_match = curr
                for child in curr.children:
                    traverse(child)

        traverse(node)
        return best_match
