"""Retriever 协议：让 grep 基线与 AST 检索可以互相替换，用于 ablation。"""

from pathlib import Path
from typing import List, Optional, Protocol

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext


class Retriever(Protocol):
    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> List[CodeContext]:
        ...
