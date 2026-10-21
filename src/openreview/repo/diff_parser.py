from pathlib import Path
import re
from typing import List, Optional, Set
from pydantic import BaseModel, Field

# 常见后缀映射与类别定义
DOCS_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}
CONFIG_EXTENSIONS = {
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".env",
    ".xml",
    ".conf",
}
LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
}

# 符号匹配正则模式（支持新增和删除行）
SYMBOL_PATTERNS = {
    "python": [
        re.compile(r"^\s*def\s+([a-zA-Z_]\w*)\s*\("),
        re.compile(r"^\s*class\s+([a-zA-Z_]\w*)\s*[\(:]"),
    ],
    "go": [
        re.compile(r"^\s*func\s+(?:\([^\)]+\)\s+)?([a-zA-Z_]\w*)\s*\("),
        re.compile(r"^\s*type\s+([a-zA-Z_]\w*)\s+(?:struct|interface)"),
    ],
    "javascript": [
        re.compile(r"^\s*(?:async\s+)?function\s+([a-zA-Z_]\w*)\s*\("),
        re.compile(r"^\s*(?:const|let|var)\s+([a-zA-Z_]\w*)\s*=\s*(?:async\s*)?\("),
        re.compile(r"^\s*class\s+([a-zA-Z_]\w*)"),
    ],
    "typescript": [
        re.compile(r"^\s*(?:async\s+)?function\s+([a-zA-Z_]\w*)\s*\("),
        re.compile(r"^\s*(?:const|let|var)\s+([a-zA-Z_]\w*)\s*:\s*[\w<>\[\]]+\s*=\s*"),
        re.compile(r"^\s*class\s+([a-zA-Z_]\w*)"),
    ],
    "rust": [
        re.compile(r"^\s*(?:pub\s+)?fn\s+([a-zA-Z_]\w*)\s*\("),
        re.compile(r"^\s*(?:pub\s+)?struct\s+([a-zA-Z_]\w*)"),
        re.compile(r"^\s*(?:pub\s+)?enum\s+([a-zA-Z_]\w*)"),
    ],
}

# Hunk Header 提取上下文的正则（匹配 @@ ... @@ 后面的函数签名）
HUNK_CONTEXT_PATTERN = re.compile(r"^@@\s+.*?\s+@@\s*(.*)$")


class ParsedChangedFile(BaseModel):
    filename: str
    status: str
    additions: int = 0
    deletions: int = 0
    patch: Optional[str] = None
    is_test_file: bool = False
    is_config_file: bool = False
    is_docs_file: bool = False
    language: Optional[str] = None
    changed_symbols: List[str] = Field(default_factory=list)
    enclosing_contexts: List[str] = Field(default_factory=list)  # 新增：Hunk 带来的上下文父函数/类


class ParsedDiff(BaseModel):
    files: List[ParsedChangedFile] = Field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0


class DiffParser:

    @staticmethod
    def _is_test_file(filename: str) -> bool:
        path = Path(filename)
        lower_name = path.name.lower()
        parts = [p.lower() for p in path.parts]

        if "test" in parts or "tests" in parts or "__tests__" in parts:
            return True
        if (
            lower_name.startswith("test_")
            or lower_name.endswith("_test.py")
            or lower_name.endswith("_test.go")
            or lower_name.endswith("test.js")
            or lower_name.endswith("spec.js")
            or lower_name.endswith("test.ts")
            or lower_name.endswith("spec.ts")
            or lower_name.endswith("test.java")
        ):
            return True
        return False

    @staticmethod
    def _detect_language(ext: str) -> Optional[str]:
        return LANGUAGE_MAP.get(ext.lower())

    @classmethod
    def extract_symbols_and_contexts(cls, patch: Optional[str], language: Optional[str]):
        """同时从 Hunk 头部和变更行中提取符号与上下文"""
        if not patch:
            return [], []

        symbols: Set[str] = set()
        contexts: Set[str] = set()
        patterns = SYMBOL_PATTERNS.get(language, []) if language else []

        for line in patch.splitlines():
            # 1. 提取 Hunk Header 的上下文（如 def iter_raw(...)）
            if line.startswith("@@"):
                match = HUNK_CONTEXT_PATTERN.match(line)
                if match and match.group(1).strip():
                    ctx_text = match.group(1).strip()
                    # 尝试从上下文中提取函数名或类名
                    for pattern in patterns:
                        m = pattern.search(ctx_text)
                        if m:
                            contexts.add(m.group(1))
                continue

            # 2. 匹配新增 (+) 或删除 (-) 行中的符号定义
            if line.startswith("+") or line.startswith("-"):
                # 去除前缀符号 + 或 -
                content = line[1:]
                for pattern in patterns:
                    match = pattern.search(content)
                    if match:
                        symbols.add(match.group(1))

        return list(symbols), list(contexts)

    @classmethod
    def parse_file(cls, file_data: dict) -> ParsedChangedFile:
        filename = file_data.get("filename", "")
        ext = Path(filename).suffix.lower()

        is_docs = ext in DOCS_EXTENSIONS or filename.startswith("docs/")
        is_config = ext in CONFIG_EXTENSIONS or filename in [
            "Dockerfile",
            "Makefile",
        ]
        is_test = cls._is_test_file(filename)
        language = cls._detect_language(ext)

        patch = file_data.get("patch")
        symbols, contexts = cls.extract_symbols_and_centers = cls.extract_symbols_and_contexts(patch, language)

        return ParsedChangedFile(
            filename=filename,
            status=file_data.get("status", "unknown"),
            additions=file_data.get("additions", 0),
            deletions=file_data.get("deletions", 0),
            patch=patch,
            is_test_file=is_test,
            is_config_file=is_config,
            is_docs_file=is_docs,
            language=language,
            changed_symbols=symbols,
            enclosing_contexts=contexts,
        )

    @classmethod
    def parse_diff(cls, changed_files: List[dict]) -> ParsedDiff:
        parsed_files = [cls.parse_file(f) for f in parsed_files] if 'parsed_files' in locals() else [cls.parse_file(f) for f in changed_files]
        total_add = sum(f.additions for f in parsed_files)
        total_del = sum(f.deletions for f in parsed_files)
        return ParsedDiff(
            files=parsed_files, total_additions=total_add, total_deletions=total_del
        )