"""跨文件影响分析：找出变更/删除符号在其他文件中的引用。

目标不是做完整类型推导，而是给 reviewer 提供低成本的“影响候选”：
- 删除的符号是否还有残留引用
- 修改的函数/类被哪些文件调用
- 变更文件的 import 是否被其他模块依赖

所有结果都是 candidate，而不是绝对正确的 call graph。
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional

from pydantic import BaseModel, Field

from openreview.repo.diff_parser import ParsedDiff

logger = logging.getLogger(__name__)

MAX_IMPACT_FINDINGS = 30
MAX_SYMBOLS = 40
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
}
COMMON_NAMES = {
    "main",
    "__init__",
    "get",
    "set",
    "run",
    "call",
    "update",
    "process",
    "handle",
    "execute",
}


class ImpactFinding(BaseModel):
    symbol: str
    file: str
    line: int
    code: str
    kind: str = "reference"
    confidence: float = 0.7


class ImpactReport(BaseModel):
    findings: List[ImpactFinding] = Field(default_factory=list)
    changed_symbols: List[str] = Field(default_factory=list)
    truncated: bool = False

    def to_notes(self) -> List[str]:
        notes = []
        for item in self.findings:
            notes.append(
                f"[impact] `{item.symbol}` 在 `{item.file}:{item.line}` "
                f"存在引用：`{item.code.strip()[:120]}`"
            )
        if self.truncated:
            notes.append("[impact] 影响分析结果过多，已截断。")
        return notes


class ImpactAnalyzer:
    def __init__(self, max_findings: int = MAX_IMPACT_FINDINGS):
        self.max_findings = max_findings

    def analyze(
        self,
        parsed_diff: ParsedDiff,
        repo_path: Path,
    ) -> ImpactReport:
        repo_path = Path(repo_path).resolve()
        symbols = self._collect_symbols(parsed_diff)
        if not symbols or not repo_path.exists():
            return ImpactReport(changed_symbols=symbols)

        changed_files = {item.filename for item in parsed_diff.files}
        pattern = r"\b(" + "|".join(re.escape(symbol) for symbol in symbols) + r")\b"
        raw_matches = self._git_grep(repo_path, pattern)
        if raw_matches is None:
            raw_matches = self._filesystem_grep(repo_path, pattern)

        findings: List[ImpactFinding] = []
        seen: set[tuple[str, str, int]] = set()
        for path, line, code in raw_matches:
            if path in changed_files:
                continue
            symbol = self._find_symbol_in_line(code, symbols)
            if symbol is None:
                continue
            key = (path, symbol, line)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ImpactFinding(
                    symbol=symbol,
                    file=path,
                    line=line,
                    code=code.strip(),
                    confidence=self._confidence(symbol, code),
                )
            )
            if len(findings) >= self.max_findings:
                break

        return ImpactReport(
            findings=findings,
            changed_symbols=symbols,
            truncated=len(findings) >= self.max_findings,
        )

    @staticmethod
    def _collect_symbols(parsed_diff: ParsedDiff) -> List[str]:
        symbols: List[str] = []
        for file_diff in parsed_diff.files:
            for symbol in file_diff.changed_symbols + file_diff.removed_symbols:
                if (
                    symbol
                    and symbol not in COMMON_NAMES
                    and len(symbol) >= 3
                    and symbol not in symbols
                ):
                    symbols.append(symbol)
        return symbols[:MAX_SYMBOLS]

    @staticmethod
    def _find_symbol_in_line(code: str, symbols: Iterable[str]) -> Optional[str]:
        for symbol in symbols:
            if re.search(rf"\b{re.escape(symbol)}\b", code):
                return symbol
        return None

    @staticmethod
    def _confidence(symbol: str, code: str) -> float:
        # 定义行/导入行通常比普通调用更有信息量；短名字降低置信度。
        if re.match(rf"\s*(def|class|func|type|import|from)\b", code):
            return 0.8
        if len(symbol) <= 4:
            return 0.55
        return 0.75

    @staticmethod
    def _git_grep(repo_path: Path, pattern: str) -> Optional[List[tuple[str, int, str]]]:
        try:
            result = subprocess.run(
                ["git", "grep", "-n", "-E", pattern, "--", "."],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        if result.returncode not in (0, 1):
            return None

        matches: List[tuple[str, int, str]] = []
        for raw in result.stdout.splitlines():
            parts = raw.split(":", 2)
            if len(parts) != 3:
                continue
            path, line_text, code = parts
            try:
                line = int(line_text)
            except ValueError:
                continue
            matches.append((path, line, code))
        return matches

    @staticmethod
    def _filesystem_grep(
        repo_path: Path, pattern: str
    ) -> List[tuple[str, int, str]]:
        regex = re.compile(pattern)
        matches: List[tuple[str, int, str]] = []
        for path in repo_path.rglob("*"):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            try:
                if path.stat().st_size > 500 * 1024:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append((str(path.relative_to(repo_path)), line_no, line))
                    if len(matches) >= MAX_IMPACT_FINDINGS * 4:
                        return matches
        return matches
