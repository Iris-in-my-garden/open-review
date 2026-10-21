import subprocess
from pathlib import Path
from typing import List, Set, Optional, Tuple

# 引入 tree-sitter
from tree_sitter import Language, Parser, Node

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext

MAX_CONTEXT_COUNT = 20
MAX_FILE_SIZE = 500 * 1024  # 500 KB 限制


class TreeSitterLocalRetriever:
    """
    通用工程级本地混合检索器 (Path Pruning + AST Boundary Extraction + Ref/Def Marking)。
    具备强泛化能力，适用于各种 Python/Go 语言仓库。
    """

    def __init__(self):
        self.parsers = {}

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

    def retrieve(self, repo_path: Path, parsed_diff: ParsedDiff) -> List[CodeContext]:
        print(f"[DEBUG] TreeSitterLocalRetriever parsed_diff: {parsed_diff}, repo_path: {repo_path}")
        repo_path = Path(repo_path).resolve()
        if not repo_path.exists() or not repo_path.is_dir():
            return []

        search_targets = self._extract_search_targets(parsed_diff)
        print(f"[DEBUG] TreeSitterLocalRetriever search_targets: {search_targets}")
        if not search_targets:
            return []

        contexts = self._search_with_git_grep(repo_path, search_targets)

        contexts.sort(key=lambda x: x.score, reverse=True)
        print(f"[DEBUG] TreeSitterLocalRetriever Contexts count: {len(contexts)}")
        return contexts[:MAX_CONTEXT_COUNT]

    def _extract_search_targets(self, parsed_diff: ParsedDiff) -> Set[str]:
        """从 Diff 中收集所有变更符号与上下文符号"""
        targets: Set[str] = set()
        for file_diff in parsed_diff.files:
            if hasattr(file_diff, "changed_symbols") and file_diff.changed_symbols:
                targets.update(file_diff.changed_symbols)
            if hasattr(file_diff, "enclosing_contexts") and file_diff.enclosing_contexts:
                targets.update(file_diff.enclosing_contexts)
        return targets

    def _is_valid_source_file(self, rel_path_str: str) -> bool:
        """【通用路径剪枝】仅基于目录结构过滤文档与缓存，不硬编码任何具体文件名"""
        path = Path(rel_path_str)
        parts = path.parts

        # 排除常见文档、打包、虚机及缓存目录
        excluded_dirs = {"docs", "site-packages", ".cache", "examples", "node_modules", "dist", "build"}
        if any(p in parts for p in excluded_dirs):
            return False

        # 仅放行支持的源码文件后缀
        return path.suffix in self.parsers

    def _search_with_git_grep(self, repo_path: Path, targets: Set[str]) -> List[CodeContext]:
        contexts: List[CodeContext] = []
        # 使用 AST 节点范围作为去重 Key，消除同一代码块多行命中引起的重复
        seen_node_ranges: Set[Tuple[str, int, int]] = set()

        valid_targets = [s.strip() for s in targets if s and s.strip()]
        if not valid_targets:
            return []

        cmd = [
            "git",
            "-C", str(repo_path),
            "grep",
            "-n",
            "-I",
            "-w"
        ]

        for sym in valid_targets:
            cmd.extend(["-e", sym])

        try:
            process = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if process.returncode not in (0, 1):
                return []

            for line in process.stdout.splitlines():
                try:
                    parts = line.split(":", 2)
                    if len(parts) < 3:
                        continue

                    rel_path_str, line_str, _ = parts

                    if not self._is_valid_source_file(rel_path_str):
                        continue

                    try:
                        line_number = int(line_str)
                    except ValueError:
                        continue

                    abs_file_path = (repo_path / rel_path_str).resolve()
                    if not abs_file_path.exists() or abs_file_path.stat().st_size > MAX_FILE_SIZE:
                        continue

                    # 核心：通过 Tree-sitter 提取 AST 边界并判断定义/引用
                    snippet, reason, is_def, node_range = self._extract_snippet_with_ast(
                        abs_file_path, line_number, valid_targets
                    )

                    if not snippet or not node_range:
                        continue

                    unique_key = (rel_path_str, node_range[0], node_range[1])
                    if unique_key not in seen_node_ranges:
                        seen_node_ranges.add(unique_key)

                        # 定义给予较高权重，引用给予基础权重
                        score = 0.90 if is_def else 0.65

                        contexts.append(
                            CodeContext(
                                file_path=rel_path_str,
                                reason=reason,
                                snippet=snippet,
                                score=score
                            )
                        )
                except Exception as e:
                    print(f"[DEBUG] error parsing grep line: {e}")
                    continue

        except FileNotFoundError:
            raise RuntimeError("Git is required but not found in system path.")

        return contexts

    def _extract_snippet_with_ast(
            self, file_path: Path, line_number: int, targets: List[str]
    ) -> Tuple[str, str, bool, Optional[Tuple[int, int]]]:
        """利用 Tree-sitter 纯语法结构分析，截取边界并判定 Definition / Reference"""
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            target_idx = line_number - 1

            suffix = file_path.suffix
            parser = self.parsers.get(suffix)
            if not parser:
                return "", "", False, None

            tree = parser.parse(text.encode("utf-8"))
            root_node = tree.root_node

            # 寻找包含目标行的最小定义节点
            target_node = self._find_enclosing_definition(root_node, target_idx)

            # 如果不在任何函数/类定义内（例如顶层代码），可根据需要决定是否降级，这里采用稳妥的行窗口兜底或直接过滤
            if not target_node:
                # 纯通用兜底：以目标行为中心切片 15 行
                start_row = max(0, target_idx - 7)
                end_row = min(len(lines) - 1, target_idx + 7)
                snippet = "\n".join(lines[start_row:end_row + 1])
                target_line = lines[target_idx] if 0 <= target_idx < len(lines) else ""
                matched = next((t for t in targets if t in target_line), "unknown")
                return snippet, f"Fallback snippet around line {line_number} for '{matched}'", False, (start_row,
                                                                                                       end_row)

            start_row = target_node.start_point[0]
            end_row = target_node.end_point[0]

            snippet_lines = lines[start_row: end_row + 1]
            snippet = "\n".join(snippet_lines)

            target_line = lines[target_idx] if 0 <= target_idx < len(lines) else ""
            matched_target = next((t for t in targets if t in target_line), "unknown")

            # 纯 AST 节点类型判定
            definition_types = {
                "function_definition", "class_definition", "method_definition",  # Python
                "function_declaration", "method_declaration", "type_declaration"  # Golang
            }

            is_def = target_node.type in definition_types

            reason = (
                f"AST-enclosed definition of '{matched_target}' (lines {start_row + 1}-{end_row + 1})"
                if is_def
                else f"AST-enclosed reference to '{matched_target}' (lines {start_row + 1}-{end_row + 1})"
            )
            return snippet, reason, is_def, (start_row, end_row)

        except Exception as e:
            print(f"[DEBUG] AST extraction failed exception: {e}")
            return "", "", False, None

    def _find_enclosing_definition(self, node: Node, target_row: int) -> Optional[Node]:
        """递归遍历 AST，寻找包含 target_row 的最小定义节点"""
        best_match = None

        definition_types = {
            "function_definition", "class_definition", "method_definition",
            "function_declaration", "method_declaration", "type_declaration"
        }

        def traverse(curr: Node):
            nonlocal best_match
            if curr.start_point[0] <= target_row <= curr.end_point[0]:
                if curr.type in definition_types:
                    best_match = curr
                for child in curr.children:
                    traverse(child)

        traverse(node)
        return best_match