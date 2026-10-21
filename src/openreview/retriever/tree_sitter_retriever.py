import subprocess
import json
import re
from pathlib import Path
from typing import List, Set, Optional

# 引入 tree-sitter
from tree_sitter import Language, Parser, Node

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext

MAX_CONTEXT_COUNT = 20
MAX_FILE_SIZE = 500 * 1024  # 500 KB 限制


class TreeSitterLocalRetriever:
    """
    工程级高性能本地混合检索器 (Local Hybrid Pipeline + Tree-sitter AST 解析)。
    支持 Python 与 Golang 的 AST 边界精准切块。
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

        # 从 parsed_diff 提取所有变更的符号
        symbols = self._extract_symbols(parsed_diff)
        print(f"[DEBUG] TreeSitterLocalRetriever symbols: {symbols}")
        if not symbols:
            return []

        # 使用 git grep 高速检索所有符号的引用与定义
        contexts = self._search_with_git_grep(repo_path, symbols)

        contexts.sort(key=lambda x: x.score, reverse=True)
        print(f"[DEBUG] TreeSitterLocalRetriever Contexts count: {len(contexts)}")
        return contexts[:MAX_CONTEXT_COUNT]

    def _extract_symbols(self, parsed_diff: ParsedDiff) -> Set[str]:
        symbols: Set[str] = set()
        for file_diff in parsed_diff.files:
            if hasattr(file_diff, "changed_symbols") and file_diff.changed_symbols:
                symbols.update(file_diff.changed_symbols)
        return symbols

    def _search_with_git_grep(self, repo_path: Path, symbols: Set[str]) -> List[CodeContext]:
        contexts: List[CodeContext] = []
        seen_keys: Set[str] = set()

        valid_symbols = [s.strip() for s in symbols if s and s.strip()]
        if not valid_symbols:
            return []

        # 使用 git grep 原生的 -w (--word-regexp) 参数，安全、原生、无惧转义陷阱
        cmd = [
            "git",
            "-C", str(repo_path),
            "grep",
            "-n",
            "-I",
            "-w"
        ]

        # 动态追加所有需要检索的符号
        for sym in valid_symbols:
            cmd.extend(["-e", sym])

        try:
            process = subprocess.run(cmd, capture_output=True, text=True, check=False)
            print(f"[DEBUG] git grep returncode: {process.returncode}")

            if process.returncode not in (0, 1):
                return []

            for line in process.stdout.splitlines():
                try:
                    parts = line.split(":", 2)
                    if len(parts) < 3:
                        continue

                    rel_path_str, line_str, _ = parts
                    try:
                        line_number = int(line_str)
                    except ValueError:
                        continue

                    abs_file_path = (repo_path / rel_path_str).resolve()
                    if not abs_file_path.exists() or abs_file_path.stat().st_size > MAX_FILE_SIZE:
                        continue

                    rel_path = rel_path_str
                    snippet, reason, is_def = self._extract_snippet_with_ast(
                        abs_file_path, line_number, symbols
                    )

                    key = f"{rel_path}:{line_number}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        score = 0.90 if is_def else 0.65

                        contexts.append(
                            CodeContext(
                                file_path=rel_path,
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
            self, file_path: Path, line_number: int, symbols: Set[str]
    ) -> tuple[str, str, bool]:
        """利用 Tree-sitter 解析文件 AST，精准截取包含目标行的函数/类/结构体完整边界"""
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            target_idx = line_number - 1

            suffix = file_path.suffix
            parser = self.parsers.get(suffix)

            if parser:
                tree = parser.parse(text.encode("utf-8"))
                root_node = tree.root_node

                target_node = self._find_enclosing_definition(root_node, target_idx)

                if target_node:
                    # 修正：使用元组索引 [0] 获取行号 (row)
                    start_row = target_node.start_point[0]
                    end_row = target_node.end_point[0]

                    snippet_lines = lines[start_row: end_row + 1]
                    snippet = "\n".join(snippet_lines)

                    target_line = lines[target_idx] if 0 <= target_idx < len(lines) else ""
                    matched_symbol = next((sym for sym in symbols if sym in target_line), "unknown")

                    definition_types = {
                        "function_definition", "class_definition", "method_definition",  # Python
                        "assignment",  # 允许捕获全局变量赋值
                        "function_declaration", "method_declaration", "type_declaration"  # Golang
                    }

                    is_def = target_node.type in definition_types
                    reason = f"AST-enclosed definition of '{matched_symbol}' (lines {start_row + 1}-{end_row + 1})" if is_def else f"AST-enclosed reference to '{matched_symbol}'"
                    return snippet, reason, is_def

            # 没有对应解析器时的通用兜底
            print(f"[DEBUG] TreeSitterLocalRetriever fallback for {file_path.name}")
            start_line = max(0, target_idx - 10)
            end_line = min(len(lines), target_idx + 15)
            snippet = "\n".join(lines[start_line:end_line])

            target_line = lines[target_idx] if 0 <= target_idx < len(lines) else ""
            matched_symbol = next((sym for sym in symbols if sym in target_line), "unknown")
            is_def = any(kw in target_line for kw in ["def ", "class ", "func ", "type "])
            reason = f"Fallback snippet around line {line_number}"

            return snippet, reason, is_def

        except Exception as e:
            print(f"[DEBUG] AST extraction failed exception: {e}")
            return "", "AST extraction failed", False

    def _find_enclosing_definition(self, node: Node, target_row: int) -> Optional[Node]:
        """递归遍历 AST，寻找包含 target_row 的最小定义节点"""
        best_match = None

        definition_types = {
            "function_definition", "class_definition", "method_definition",
            "function_declaration", "method_declaration", "type_declaration"
        }

        def traverse(curr: Node):
            nonlocal best_match
            # 修正：使用 [0] 获取行号
            if curr.start_point[0] <= target_row <= curr.end_point[0]:
                if curr.type in definition_types:
                    best_match = curr
                for child in curr.children:
                    traverse(child)

        traverse(node)
        return best_match