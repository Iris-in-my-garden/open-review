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

# Hunk Header 的数字部分：@@ -old_start,old_count +new_start,new_count @@
HUNK_RANGE_PATTERN = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)


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
    removed_symbols: List[str] = Field(default_factory=list)
    enclosing_contexts: List[str] = Field(default_factory=list)  # 新增：Hunk 带来的上下文父函数/类
    # 新增：变更后的新文件行号（用于反查“变更行所属的函数/类”）
    changed_lines: List[int] = Field(default_factory=list)
    # 新增：Patch 中新文件侧可见的行号（新增行 + 上下文行，用于校验行号是否落在 diff 内）
    visible_lines: List[int] = Field(default_factory=list)


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

        if lower_name == "__init__.py":
            return False
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
    def is_test_path(filename: str) -> bool:
        """公开的测试文件判断，供检索器与测试缺口分析复用。"""
        return DiffParser._is_test_file(filename)

    @staticmethod
    def is_linked_test(source_file: str, test_path: str) -> bool:
        """判断 test_path 是否像 source_file 对应的测试文件。

        支持两类约定：
        1. 文件名约定：test_mod.py / mod_test.py / mod.test.ts / modTest.java
        2. 目录约定：httpx/_models.py ↔ tests/models/test_cookies.py
        """
        source = Path(source_file)
        test = Path(test_path)
        source_stem = source.stem.lower().lstrip("_")
        test_stem = test.stem.lower().lstrip("_")
        candidates = {
            f"test_{source_stem}",
            f"{source_stem}_test",
            f"{source_stem}.test",
            f"{source_stem}.spec",
            f"{source_stem}test",
        }
        if test_stem in candidates or source_stem == test_stem:
            return True

        source_dirs = {
            part.lower().lstrip("_")
            for part in source.parent.parts
            if part not in (".", "")
        }
        test_dirs = {
            part.lower().lstrip("_")
            for part in test.parent.parts
            if part not in (".", "")
        }
        if source_stem in test_dirs:
            return True
        if source_dirs & test_dirs and source_stem in test_stem:
            return True
        return False

    @staticmethod
    def _detect_language(ext: str) -> Optional[str]:
        return LANGUAGE_MAP.get(ext.lower())

    @classmethod
    def extract_symbols_and_contexts(cls, patch: Optional[str], language: Optional[str]):
        """同时从 Hunk 头部和变更行中提取新增/删除符号与上下文。

        返回 (added_symbols, removed_symbols, enclosing_contexts)：
        - 新增行里的定义进入 changed_symbols（真正需要被审查的符号）
        - 删除行里的定义进入 removed_symbols（用于检索仍在引用它的代码）
        - Hunk Header 中的函数/类签名进入 enclosing_contexts
        """
        if not patch:
            return [], [], []

        added: List[str] = []
        removed: List[str] = []
        contexts: List[str] = []
        patterns = SYMBOL_PATTERNS.get(language, []) if language else []

        def _append_unique(target: List[str], value: str) -> None:
            if value not in target:
                target.append(value)

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
                            _append_unique(contexts, m.group(1))
                continue

            # 2. 匹配新增 (+) 或删除 (-) 行中的符号定义
            if line.startswith("+") or line.startswith("-"):
                target = added if line.startswith("+") else removed
                content = line[1:]
                for pattern in patterns:
                    match = pattern.search(content)
                    if match:
                        _append_unique(target, match.group(1))

        return added, removed, contexts

    @staticmethod
    def extract_changed_lines(patch: Optional[str]) -> List[int]:
        """从 patch 中提取“新增行”在 PR head 文件里的行号。

        纯删除的 Hunk 没有新增行，此时退回该 Hunk 的起始行，保证仍能定位到
        被修改函数的位置。
        """
        if not patch:
            return []

        changed_lines: List[int] = []
        new_line: Optional[int] = None
        hunk_start: Optional[int] = None
        hunk_has_added = False

        def flush_hunk() -> None:
            if hunk_start is not None and not hunk_has_added:
                changed_lines.append(max(1, hunk_start))

        for raw_line in patch.splitlines():
            header = HUNK_RANGE_PATTERN.match(raw_line)
            if header:
                flush_hunk()
                hunk_start = int(header.group(3))
                new_line = hunk_start
                hunk_has_added = False
                continue

            if new_line is None:
                continue

            if raw_line.startswith("+"):
                changed_lines.append(new_line)
                hunk_has_added = True
                new_line += 1
            elif raw_line.startswith("-"):
                # 删除行不对应 head 文件中的行号
                continue
            elif raw_line.startswith(" "):
                new_line += 1
            # "\ No newline at end of file" 等元信息忽略

        flush_hunk()

        # 去重并保持顺序
        seen: Set[int] = set()
        unique_lines: List[int] = []
        for line in changed_lines:
            if line not in seen:
                seen.add(line)
                unique_lines.append(line)
        return unique_lines

    @staticmethod
    def extract_visible_lines(patch: Optional[str]) -> List[int]:
        """提取 Patch 中新文件侧可见的所有行号（新增行 + 上下文行）。"""
        if not patch:
            return []

        visible: List[int] = []
        new_line: Optional[int] = None

        for raw_line in patch.splitlines():
            header = HUNK_RANGE_PATTERN.match(raw_line)
            if header:
                new_line = int(header.group(3))
                continue

            if new_line is None:
                continue

            if raw_line.startswith("+"):
                visible.append(new_line)
                new_line += 1
            elif raw_line.startswith(" "):
                visible.append(new_line)
                new_line += 1
            # 删除行不在新文件中

        seen: Set[int] = set()
        unique_lines: List[int] = []
        for line in visible:
            if line not in seen:
                seen.add(line)
                unique_lines.append(line)
        return unique_lines

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
        added_symbols, removed_symbols, contexts = cls.extract_symbols_and_contexts(
            patch, language
        )
        changed_lines = cls.extract_changed_lines(patch)
        visible_lines = cls.extract_visible_lines(patch)

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
            changed_symbols=added_symbols,
            removed_symbols=removed_symbols,
            enclosing_contexts=contexts,
            changed_lines=changed_lines,
            visible_lines=visible_lines,
        )

    @classmethod
    def parse_diff(cls, changed_files: List[dict]) -> ParsedDiff:
        parsed_files = [cls.parse_file(f) for f in changed_files]
        total_add = sum(f.additions for f in parsed_files)
        total_del = sum(f.deletions for f in parsed_files)
        return ParsedDiff(
            files=parsed_files, total_additions=total_add, total_deletions=total_del
        )