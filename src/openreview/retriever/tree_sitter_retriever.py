import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# 引入 tree-sitter
from tree_sitter import Language, Node, Parser, Tree

from openreview.repo.diff_parser import DiffParser, ParsedDiff
from openreview.retriever.code_context import CodeContext, make_context_id
from openreview.retriever.folding import fold_snippet
from openreview.retriever.globals import collect_referenced_fields
from openreview.retriever.grep import search_symbols
from openreview.retriever.pathing import is_source_file
from openreview.review.token_budget import estimate_tokens

logger = logging.getLogger(__name__)

MAX_CONTEXT_COUNT = 64
MAX_FILE_SIZE = 500 * 1024  # 500 KB 限制
# 大 PR（尤其是包含生成代码的 PR）可能有几百个符号。参考检索的耗时随
# 目标数量增长，50 个目标已经足够覆盖主要变更，同时把单次检索压到秒级。
MAX_SEARCH_TARGETS = 50
# 匹配行再多也只会留下少量高分上下文；限制 AST 解析量，避免 `context` /
# `New` 这类高频符号把一次检索放大到上万条候选。
MAX_REFERENCE_CONTEXTS = 200
MAX_TEST_CONTEXT_COUNT = 8
MAX_TEST_SNIPPET_LINES = 120
# 单个 enclosing 方法超过该 token 数时做 AST 折叠；折叠会保留全部变更行。
MAX_ENCLOSING_SNIPPET_TOKENS = 1600

# 打分：变更行所在的函数/类 > 符号定义 > 关联测试 > 符号引用 > base 侧历史代码
SCORE_ENCLOSING = 0.95
SCORE_DEFINITION = 0.90
SCORE_TEST_FILE = 0.85
SCORE_REFERENCE = 0.65
SCORE_BASE_DEFINITION = 0.80
SCORE_BASE_REFERENCE = 0.55

DEFINITION_TYPES = {
    "function_definition", "class_definition", "method_definition",  # Python
    "function_declaration", "method_declaration", "type_declaration",  # Golang
}

LoadedFile = Tuple[str, List[str], bytes, Tree]


class TreeSitterLocalRetriever:
    """
    通用工程级本地混合检索器 (Path Pruning + AST Boundary Extraction + Ref/Def Marking)。
    具备强泛化能力，适用于各种 Python/Go 语言仓库。
    """

    def __init__(self):
        self.parsers = {}
        # 供 HybridRetriever 汇总：AST 层自己的截断/目标裁剪也应该可见。
        self.last_stats: Dict[str, float] = {}

        # 1. 初始化 Python Parser
        try:
            import tree_sitter_python as tspython
            py_lang = Language(tspython.language())
            self.parsers[".py"] = Parser(py_lang)
        except ImportError:
            pass

        # 2. 初始化 Golang Parser
        try:
            import tree_sitter_go as tsgo
            go_lang = Language(tsgo.language())
            self.parsers[".go"] = Parser(go_lang)
        except ImportError:
            pass

    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> List[CodeContext]:
        repo_path = Path(repo_path).resolve()
        self.last_stats = {}
        if not repo_path.exists() or not repo_path.is_dir():
            return []

        # 同一次检索内复用已读取/已解析的文件，避免同一文件被反复 parse
        file_cache: Dict[Path, Optional[LoadedFile]] = {}
        seen_node_ranges: Set[Tuple[str, int, int]] = set()

        # 1. 先处理“变更行所在的函数/类”。即使 PR 只改了函数体、没有改签名，
        #    也能拿到完整上下文（修复只改函数体时检索为空的问题）。
        enclosing_contexts, enclosing_names = self._collect_changed_line_contexts(
            repo_path, parsed_diff, seen_node_ranges, file_cache
        )
        # P5：只补充被变更方法引用的类/模块字段，预算不足时才自然被裁掉。
        field_contexts = self._collect_field_contexts(
            repo_path, parsed_diff, file_cache
        )

        # 2. 再用符号做全局检索。把变更行所属的函数/类也加入搜索目标，
        #    这样能顺带找到它的调用方。
        search_targets = self._extract_search_targets(parsed_diff) | enclosing_names
        logger.debug("Search targets: %s", sorted(search_targets))

        contexts = enclosing_contexts + self._search_references(
            repo_path, search_targets, seen_node_ranges, file_cache
        )

        # 3. 关联测试文件：用于测试缺口分析，而不是让模型凭空猜“应该有测试”
        contexts.extend(
            self._collect_test_contexts(
                repo_path, parsed_diff, seen_node_ranges, file_cache
            )
        )
        contexts.extend(field_contexts)

        # 4. base 侧历史代码：只查被删除的符号，补充“删掉的函数原来长什么样”
        removed_targets = {
            symbol
            for file_diff in parsed_diff.files
            for symbol in (file_diff.removed_symbols or [])
        }
        if base_path and removed_targets:
            base_path = Path(base_path).resolve()
            if base_path.is_dir():
                contexts.extend(
                    self._search_references(
                        base_path,
                        removed_targets,
                        set(),
                        file_cache,
                        revision="base",
                    )
                )

        contexts.sort(key=lambda x: x.score, reverse=True)
        self.last_stats["ast_contexts"] = len(contexts)
        self.last_stats["ast_contexts_truncated"] = max(
            0, len(contexts) - MAX_CONTEXT_COUNT
        )
        logger.debug("Retrieved %d contexts", len(contexts))
        return contexts[:MAX_CONTEXT_COUNT]

    def _extract_search_targets(self, parsed_diff: ParsedDiff) -> Set[str]:
        """从 Diff 中收集所有变更/删除符号与 Hunk 上下文符号。"""
        targets: Set[str] = set()
        for file_diff in parsed_diff.files:
            targets.update(file_diff.changed_symbols or [])
            targets.update(getattr(file_diff, "removed_symbols", []) or [])
            targets.update(file_diff.enclosing_contexts or [])
        return {target.strip() for target in targets if target and target.strip()}

    def _collect_changed_line_contexts(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        seen_node_ranges: Set[Tuple[str, int, int]],
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

                unique_key = (file_diff.filename, node.start_point[0], node.end_point[0])
                if unique_key in seen_node_ranges:
                    continue
                seen_node_ranges.add(unique_key)

                start_row, end_row = node.start_point[0], node.end_point[0]
                snippet = "\n".join(lines[start_row:end_row + 1])
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
                        priority=2,
                        folded=folded,
                        symbol=display_name if name else "",
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

    def _collect_field_contexts(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        file_cache: Dict[Path, Optional[LoadedFile]],
    ) -> List[CodeContext]:
        """P5：收集被变更方法引用的类/模块字段定义。"""
        contexts: List[CodeContext] = []
        seen: Set[Tuple[str, int, int]] = set()

        for file_diff in parsed_diff.files:
            if not file_diff.changed_lines or file_diff.status == "removed":
                continue
            suffix = Path(file_diff.filename).suffix
            if suffix != ".py":
                continue
            abs_path = self._resolve_repo_file(repo_path, file_diff.filename)
            if abs_path is None:
                continue
            loaded = self._load_file(abs_path, file_cache)
            if loaded is None:
                continue

            text, lines, source, tree = loaded
            method_spans: Set[Tuple[int, int]] = set()
            for line_number in file_diff.changed_lines:
                node = self._find_enclosing_definition(tree.root_node, line_number - 1)
                if node is None:
                    continue
                method_spans.add((node.start_point[0] + 1, node.end_point[0] + 1))

            for start_line, end_line in sorted(method_spans):
                for field in collect_referenced_fields(
                    text, suffix, start_line, end_line
                ):
                    key = (file_diff.filename, field.start_line, field.end_line)
                    if key in seen:
                        continue
                    seen.add(key)
                    contexts.append(
                        CodeContext(
                            file_path=file_diff.filename,
                            reason=f"field `{field.name}` referenced by changed method",
                            snippet=field.source,
                            score=0.4,
                            kind="field",
                            priority=5,
                            symbol=field.name,
                            start_line=field.start_line,
                            end_line=field.end_line,
                            context_id=make_context_id(
                                file_diff.filename,
                                field.start_line,
                                field.end_line,
                                "field",
                                fallback=field.name,
                            ),
                        )
                    )
        return contexts

    def _collect_test_contexts(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        seen_node_ranges: Set[Tuple[str, int, int]],
        file_cache: Dict[Path, Optional[LoadedFile]],
    ) -> List[CodeContext]:
        """找到与变更模块关联的测试文件，作为测试缺口分析的依据。"""
        source_files = {
            file_diff.filename
            for file_diff in parsed_diff.files
            if not file_diff.is_test_file
            and not file_diff.is_docs_file
            and not file_diff.is_config_file
        }
        if not source_files:
            return []

        try:
            process = subprocess.run(
                ["git", "-C", str(repo_path), "ls-files"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Git is required but not found in system path.") from exc

        if process.returncode != 0:
            return []

        tracked_tests = [
            rel_path
            for rel_path in process.stdout.splitlines()
            if self._is_valid_source_file(rel_path)
            and DiffParser.is_test_path(rel_path)
        ]

        selected: List[str] = []
        for source_file in sorted(source_files):
            matches = [
                rel_path
                for rel_path in tracked_tests
                if DiffParser.is_linked_test(source_file, rel_path)
            ]
            selected.extend(matches[:2])
            if len(selected) >= MAX_TEST_CONTEXT_COUNT:
                break

        selected = selected[:MAX_TEST_CONTEXT_COUNT]

        contexts: List[CodeContext] = []
        for rel_path in dict.fromkeys(selected):
            unique_key = (rel_path, -1, -1)
            if unique_key in seen_node_ranges:
                continue

            abs_path = self._resolve_repo_file(repo_path, rel_path)
            if abs_path is None:
                continue
            loaded = self._load_file(abs_path, file_cache)
            if loaded is None:
                continue

            _, lines, _, _ = loaded
            seen_node_ranges.add(unique_key)
            end_line = min(len(lines), MAX_TEST_SNIPPET_LINES)
            contexts.append(
                CodeContext(
                    file_path=rel_path,
                    reason="Linked test file for changed module",
                    snippet="\n".join(lines[:MAX_TEST_SNIPPET_LINES]),
                    score=SCORE_TEST_FILE,
                    revision="head",
                    kind="test",
                    priority=6,
                    start_line=1,
                    end_line=end_line,
                    context_id=make_context_id(
                        rel_path, 1, end_line, "test", revision="head"
                    ),
                )
            )

        return contexts

    def _resolve_repo_file(self, repo_path: Path, rel_path_str: str) -> Optional[Path]:
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

    def _search_references(
        self,
        repo_path: Path,
        targets: Set[str],
        seen_node_ranges: Set[Tuple[str, int, int]],
        file_cache: Dict[Path, Optional[LoadedFile]],
        revision: str = "head",
    ) -> List[CodeContext]:
        contexts: List[CodeContext] = []

        # 超长 PR 必须截断时，优先保留更长、更具体的符号；按字母序截断容易
        # 留下 `init` / `x` / `Context` 这类高频噪声，挤掉真正有信息量的名字。
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

                # 核心：通过 Tree-sitter 提取 AST 边界并判断定义/引用
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

                unique_key = (rel_path_str, node_range[0], node_range[1])
                if unique_key in seen_node_ranges:
                    continue
                seen_node_ranges.add(unique_key)

                # 定义给予较高权重，引用给予基础权重；base 侧整体降权
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
                        priority=6,
                        symbol=matched_symbol,
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

        # 如果不在任何函数/类定义内（例如顶层代码），采用行窗口兜底
        if not target_node:
            start_row = max(0, target_idx - 7)
            end_row = min(len(lines) - 1, target_idx + 7)
            snippet = "\n".join(lines[start_row:end_row + 1])
            matched = next((t for t in targets if t in target_line), "unknown")
            return (
                snippet,
                f"Fallback snippet around line {line_number} for '{matched}'",
                False,
                (start_row, end_row),
                matched,
            )

        start_row, end_row = target_node.start_point[0], target_node.end_point[0]
        snippet = "\n".join(lines[start_row:end_row + 1])

        definition_name = self._definition_name(target_node, source)
        matched_target = next(
            (t for t in targets if t in target_line),
            definition_name or "unknown",
        )

        # 关键修复：只有“命中行就是该定义节点的声明行”时才算 Definition。
        # 原实现只要命中行位于某个函数内部就判为 Definition，导致所有引用都被
        # 打成 0.90 分，检索排序失真。
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
        return source[name_node.start_byte:name_node.end_byte].decode("utf-8", "ignore")

    def _find_enclosing_definition(self, node: Node, target_row: int) -> Optional[Node]:
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
