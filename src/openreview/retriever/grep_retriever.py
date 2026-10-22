import logging
import re
from pathlib import Path
from typing import List, Optional, Set

from openreview.repo.diff_parser import DiffParser, ParsedDiff
from openreview.retriever.code_context import CodeContext

logger = logging.getLogger(__name__)

EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "venv",
    ".venv",
    "__pycache__",
    ".tox",
    ".idea",
}
MAX_FILE_SIZE = 500 * 1024  # 500 KB
MAX_SNIPPET_LINES = 40
MAX_CONTEXT_COUNT = 20


class GrepRetriever:

    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> List[CodeContext]:
        _ = base_path  # grep 基线暂不读取 base 版本，仅保持接口一致
        repo_path = Path(repo_path).resolve()
        if not repo_path.exists() or not repo_path.is_dir():
            return []

        results: List[CodeContext] = []
        seen_keys: Set[str] = set()

        # 1. 预处理：复用统一的测试文件约定，避免和 AST 检索器行为分叉
        changed_files = [f.filename for f in parsed_diff.files]

        def is_linked_test(rel_path: str) -> bool:
            return DiffParser.is_test_path(rel_path) and any(
                DiffParser.is_linked_test(source, rel_path)
                for source in changed_files
            )

        # 2. 预处理：收集所有需要搜索的 Symbol 并预编译正则
        all_symbols: Set[str] = set()
        for file in parsed_diff.files:
            all_symbols.update(file.changed_symbols)

        symbol_patterns = {
            symbol: re.compile(rf"\b{re.escape(symbol)}\b")
            for symbol in all_symbols
        }

        # 定义匹配正则（用于识别是否为函数/类定义）
        def_pattern = re.compile(r"\b(def|class|func|interface|type|struct)\s+")

        # 3. 单次遍历仓库（Single-Pass Scan）：兼顾测试文件查找与符号全局检索
        for p in repo_path.rglob("*"):
            if not p.is_file():
                continue

            rel_path = p.relative_to(repo_path)
            if self._is_excluded(rel_path):
                continue

            try:
                if p.stat().st_size > MAX_FILE_SIZE or self._is_binary(p):
                    continue

                rel_path_str = str(rel_path)
                text = None  # 延迟读取，按需加载

                # 策略一：检查是否为关联的测试文件
                if is_linked_test(str(rel_path)):
                    text = p.read_text(encoding="utf-8", errors="ignore")
                    lines = text.splitlines()[:MAX_SNIPPET_LINES]
                    key = f"{rel_path_str}:test_file"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append(
                            CodeContext(
                                file_path=rel_path_str,
                                reason="Matched test file for changes",
                                snippet="\n".join(lines),
                                score=0.9,  # 测试文件保持高权重
                            )
                        )

                # 策略二：在当前文件中检索 changed_symbols
                if symbol_patterns:
                    if text is None:
                        text = p.read_text(encoding="utf-8", errors="ignore")
                    lines = text.splitlines()

                    for symbol, pattern in symbol_patterns.items():
                        for idx, line in enumerate(lines):
                            if pattern.search(line):
                                start_line = max(0, idx - 5)
                                end_line = min(len(lines), start_line + MAX_SNIPPET_LINES)
                                snippet_lines = lines[start_line:end_line]

                                # 区分定义与引用，动态赋分
                                is_definition = bool(def_pattern.search(line) and symbol in line)
                                score = 0.85 if is_definition else 0.65

                                # 修复点：已将末尾的 /> 改为正确的闭合括号 )
                                reason = (
                                    f"Definition of '{symbol}' at line {idx + 1}"
                                    if is_definition
                                    else f"Reference to '{symbol}' at line {idx + 1}"
                                )

                                key = f"{rel_path_str}:{symbol}:{idx}"
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    results.append(
                                        CodeContext(
                                            file_path=rel_path_str,
                                            reason=reason,
                                            snippet="\n".join(snippet_lines),
                                            score=score,
                                        )
                                    )
                                # 每个文件对该符号仅提取首个显著匹配，避免单个大文件产生过多重复上下文
                                break

            except Exception:
                continue

        # 4. 统一降序排序并截断至最大限制
        results.sort(key=lambda x: x.score, reverse=True)
        logger.debug("GrepRetriever contexts: %d", len(results))
        return results[:MAX_CONTEXT_COUNT]

    def _is_excluded(self, path: Path) -> bool:
        for part in path.parts:
            if part in EXCLUDE_DIRS:
                return True
        return False

    def _is_binary(self, file_path: Path) -> bool:
        try:
            with open(file_path, "rb") as f:
                return b"\x00" in f.read(1024)
        except Exception:
            return True