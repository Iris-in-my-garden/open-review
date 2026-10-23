"""Retriever 协议：上下文检索器的统一接口，便于测试时替换实现。"""

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
