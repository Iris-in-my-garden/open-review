"""基于 changed file 和 changed symbols，在本地 repo 中检索相关上下文。"""

import re
from pathlib import Path
from typing import List, Set

from pydantic import BaseModel

from openreview.repo.diff_parser import ParsedDiff

EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "venv",
    ".venv",
    "__pycache__",
}
MAX_FILE_SIZE = 500 * 1024  # 500 KB
MAX_SNIPPET_LINES = 40
MAX_CONTEXT_COUNT = 20


class CodeContext(BaseModel):
    file_path: str
    reason: str
    snippet: str
    score: float


class GrepRetriever:

    def retrieve(self, repo_path: Path, parsed_diff: ParsedDiff) -> List[CodeContext]:
        repo_path = Path(repo_path).resolve()
        if not repo_path.exists() or not repo_path.is_dir():
            return []

        results: List[CodeContext] = []
        seen_keys: Set[str] = set()  # 防止重复添加相同的 file_path + snippet

        # 1. 策略一：寻找对应的测试文件
        for changed_file in parsed_diff.files:
            test_contexts = self._find_test_files(repo_path, changed_file.filename)
            for ctx in test_contexts:
                key = f"{ctx.file_path}:{ctx.reason}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    results.append(ctx)

        # 提取所有改动到的 Symbol
        all_symbols = set()
        for file in parsed_diff.files:
            all_symbols.update(file.changed_symbols)

        # 2. 策略二：在 repo 全局检索 changed_symbols
        if all_symbols:
            symbol_contexts = self._search_symbols(repo_path, all_symbols)
            for ctx in symbol_contexts:
                key = f"{ctx.file_path}:{ctx.snippet[:50]}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    results.append(ctx)

        # 按照 score 降序排序并截断至 MAX_CONTEXT_COUNT (20)
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:MAX_CONTEXT_COUNT]

    def _is_excluded(self, path: Path) -> bool:
        """检查路径是否包含被排除的目录"""
        for part in path.parts:
            if part in EXCLUDE_DIRS:
                return True
        return False

    def _is_binary(self, file_path: Path) -> bool:
        """简单的二进制文件检测：读取前 1024 字节检查是否包含 NULL 字符"""
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(1024)
                return b"\x00" in chunk
        except Exception:
            return True

    def _find_test_files(
        self, repo_path: Path, changed_filename: str
    ) -> List[CodeContext]:
        contexts = []
        file_path = Path(changed_filename)
        stem = file_path.stem  # 例如 "bar"

        # 如果修改的文件本身就是测试文件，跳过反向查找
        if stem.startswith("test_") or stem.endswith("_test"):
            return contexts

        possible_test_names = {f"test_{stem}.py", f"{stem}_test.py"}

        for p in repo_path.rglob("*"):
            if not p.is_file() or self._is_excluded(p.relative_to(repo_path)):
                continue

            if p.name in possible_test_names:
                try:
                    if p.stat().st_size > MAX_FILE_SIZE or self._is_binary(p):
                        continue
                    text = p.read_text(encoding="utf-8", errors="ignore")
                    lines = text.splitlines()[:MAX_SNIPPET_LINES]
                    rel_path = str(p.relative_to(repo_path))

                    contexts.append(
                        CodeContext(
                            file_path=rel_path,
                            reason=f"Matched test file for {changed_filename}",
                            snippet="\n".join(lines),
                            score=0.9,  # 测试文件关联赋高权重
                        )
                    )
                except Exception:
                    continue
        return contexts

    def _search_symbols(
        self, repo_path: Path, symbols: Set[str]
    ) -> List[CodeContext]:
        contexts = []
        if not symbols:
            return contexts

        # 预编译正则提高效率
        patterns = {
            symbol: re.compile(rf"\b{re.escape(symbol)}\b") for symbol in symbols
        }

        for p in repo_path.rglob("*"):
            if not p.is_file():
                continue

            rel_path = p.relative_to(repo_path)
            if self._is_excluded(rel_path):
                continue

            try:
                if p.stat().st_size > MAX_FILE_SIZE or self._is_binary(p):
                    continue

                text = p.read_text(encoding="utf-8", errors="ignore")
                lines = text.splitlines()

                for symbol, pattern in patterns.items():
                    for idx, line in enumerate(lines):
                        if pattern.search(line):
                            # 获取包含该 symbol 的前后行片段（最多 40 行）
                            start_line = max(0, idx - 5)
                            end_line = min(len(lines), start_line + MAX_SNIPPET_LINES)
                            snippet_lines = lines[start_line:end_line]

                            contexts.append(
                                CodeContext(
                                    file_path=str(rel_path),
                                    reason=f"Symbol '{symbol}' referenced at line {idx + 1}",
                                    snippet="\n".join(snippet_lines),
                                    score=0.7,
                                )
                            )
                            # 每个文件对同一 symbol 提取一次 snippet 即可
                            break
            except Exception:
                continue

        return contexts