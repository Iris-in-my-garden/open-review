"""从 Diff 构造检索 query。

Natural-language RAG 的 query 是用户问题；code review 的 query 是 diff。
这里把 diff 里有信息量的信号抽出来，而不是只拿 changed_symbols 去 grep：

- 变更/删除符号 + 包围符号
- 变更文件对应的模块名（用于找 importer）
- 变更文件自己的 import（用于找依赖定义）
- 新增行里的标识符和字符串（错误串、配置 key、SQL 等）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.index import RepoIndex, module_name

MAX_QUERY_SYMBOLS = 40
MAX_QUERY_TERMS = 60
MAX_ADDED_LINE_IDENTIFIERS = 30
MAX_STRING_TERMS = 10

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STRING_LITERAL = re.compile(r"""["']([^"'\n]{4,80})["']""")

# 这些名字出现在几乎每个文件里，作为 FTS query 只会制造噪声。
_COMMON_NAMES = {
    "self",
    "cls",
    "main",
    "init",
    "get",
    "set",
    "run",
    "call",
    "update",
    "process",
    "handle",
    "execute",
    "return",
    "import",
    "class",
    "def",
    "func",
    "type",
    "string",
    "error",
    "value",
    "data",
    "result",
    "request",
    "response",
    "context",
    "args",
    "kwargs",
    "true",
    "false",
    "none",
    "nil",
}


@dataclass
class RetrievalQuery:
    symbols: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    modules: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    terms: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.terms


class QueryBuilder:
    def build(
        self,
        parsed_diff: ParsedDiff,
        index: Optional[RepoIndex] = None,
    ) -> RetrievalQuery:
        files = [file.filename for file in parsed_diff.files if file.filename]
        symbols = self._collect_symbols(parsed_diff)
        modules = self._module_variants(files)
        imports = self._collect_imports(index, files)
        added_identifiers, string_terms = self._collect_added_line_terms(parsed_diff)

        terms = _unique(
            [
                *symbols,
                *modules,
                *imports,
                *added_identifiers,
                *string_terms,
            ]
        )[:MAX_QUERY_TERMS]
        return RetrievalQuery(
            symbols=symbols,
            files=files,
            modules=modules,
            imports=imports,
            terms=terms,
        )

    @staticmethod
    def _collect_symbols(parsed_diff: ParsedDiff) -> List[str]:
        symbols: List[str] = []
        for file_diff in parsed_diff.files:
            candidates = [
                *(file_diff.changed_symbols or []),
                *(file_diff.removed_symbols or []),
                *(file_diff.enclosing_contexts or []),
            ]
            for symbol in candidates:
                cleaned = symbol.strip()
                if (
                    cleaned
                    and len(cleaned) >= 3
                    and cleaned.lower() not in _COMMON_NAMES
                    and cleaned not in symbols
                ):
                    symbols.append(cleaned)
        return symbols[:MAX_QUERY_SYMBOLS]

    @staticmethod
    def _module_variants(files: Sequence[str]) -> List[str]:
        variants: List[str] = []
        for filename in files:
            module = module_name(filename)
            if not module:
                continue
            parts = module.split(".")
            # 同时加入全路径和尾部路径，兼容 `src.pkg.mod` / `pkg.mod` 两种 import。
            for start in range(max(0, len(parts) - 3), len(parts)):
                candidate = ".".join(parts[start:])
                if len(candidate) >= 3 and candidate not in variants:
                    variants.append(candidate)
        return variants[:MAX_QUERY_SYMBOLS]

    @staticmethod
    def _collect_imports(
        index: Optional[RepoIndex], files: Sequence[str]
    ) -> List[str]:
        if index is None:
            return []
        imports: List[str] = []
        for edge in index.imports_of(files):
            for candidate in _module_variants_for(edge.dst_module):
                if candidate and candidate not in imports:
                    imports.append(candidate)
        return imports[:MAX_QUERY_SYMBOLS]

    @staticmethod
    def _collect_added_line_terms(
        parsed_diff: ParsedDiff,
    ) -> tuple[List[str], List[str]]:
        identifiers: List[str] = []
        strings: List[str] = []
        for file_diff in parsed_diff.files:
            for raw_line in (file_diff.patch or "").splitlines():
                if not raw_line.startswith("+") or raw_line.startswith("+++"):
                    continue
                line = raw_line[1:]
                for match in _IDENTIFIER.findall(line):
                    if (
                        match.lower() not in _COMMON_NAMES
                        and match not in identifiers
                    ):
                        identifiers.append(match)
                        if len(identifiers) >= MAX_ADDED_LINE_IDENTIFIERS:
                            break
                for match in _STRING_LITERAL.findall(line):
                    cleaned = match.strip()
                    if cleaned and cleaned not in strings:
                        strings.append(cleaned)
                        if len(strings) >= MAX_STRING_TERMS:
                            break
                if (
                    len(identifiers) >= MAX_ADDED_LINE_IDENTIFIERS
                    and len(strings) >= MAX_STRING_TERMS
                ):
                    break
        return identifiers, strings


def _module_variants_for(module: str) -> List[str]:
    cleaned = module.strip().strip(".")
    if not cleaned:
        return []
    parts = cleaned.split(".")
    variants: List[str] = []
    for start in range(max(0, len(parts) - 3), len(parts)):
        candidate = ".".join(parts[start:])
        if len(candidate) >= 3 and candidate not in variants:
            variants.append(candidate)
    return variants


def _unique(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
