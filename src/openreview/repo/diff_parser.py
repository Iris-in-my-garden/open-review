""" 从 patch 中粗略提取函数名、类名、导出的变量名，简单判断patch的文件类型等"""

from pathlib import Path
import re
from typing import List, Optional
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

# 提取新增行正则模式（仅匹配以 '+' 开头，且排除 '+++' 文件的行）
SYMBOL_PATTERNS = {
    "python": [
        re.compile(r"^\+\s*def\s+([a-zA-Z_]\w*)\s*\("),
        re.compile(r"^\+\s*class\s+([a-zA-Z_]\w*)\s*[\(:]"),
    ],
    "go": [
        re.compile(r"^\+\s*func\s+(?:\([^\)]+\)\s+)?([a-zA-Z_]\w*)\s*\("),
        re.compile(r"^\+\s*type\s+([a-zA-Z_]\w*)\s+(?:struct|interface)"),
    ],
    "java": [
        re.compile(
            r"^\+\s*(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?class\s+([a-zA-Z_]\w*)"
        ),
        re.compile(
            r"^\+\s*(?:public|protected|private)\s+[\w<>\[\]]+\s+([a-zA-Z_]\w*)\s*\("
        ),
    ],
}


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

        # 检查路径中是否包含 test/tests 目录
        if "test" in parts or "tests" in parts or "__tests__" in parts:
            return True
        # 检查文件名规则
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

    # todo 新增行里如果没有函数名，就解析不出来symbol
    @classmethod
    def extract_symbols(cls, patch: Optional[str], language: Optional[str]) -> List[str]:
        if not patch or not language or language not in SYMBOL_PATTERNS:
            return []

        symbols = []
        patterns = SYMBOL_PATTERNS[language]

        for line in patch.splitlines():
            # 过滤统一 diff 头部
            if line.startswith("+++"):
                continue
            # 仅匹配新增的行
            if line.startswith("+"):
                for pattern in patterns:
                    match = pattern.search(line)
                    if match:
                        symbol = match.group(1)
                        if symbol not in symbols:
                            symbols.append(symbol)
        return symbols

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
        symbols = cls.extract_symbols(patch, language)

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
        )

    @classmethod
    def parse_diff(cls, changed_files: List[dict]) -> ParsedDiff:
        parsed_files = [cls.parse_file(f) for f in changed_files]
        total_add = sum(f.additions for f in parsed_files)
        total_del = sum(f.deletions for f in parsed_files)
        return ParsedDiff(
            files=parsed_files, total_additions=total_add, total_deletions=total_del
        )