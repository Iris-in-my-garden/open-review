"""轻量 token 预算与 PR 分批工具。

这里不引入 tiktoken，使用保守的字符/token 估算（默认 1 token ≈ 3 字符），
用于在超长 PR 上做“够用就好”的预算控制，而不是精确计费。
"""

import math
from typing import List

from openreview.repo.diff_parser import ParsedDiff, ParsedChangedFile


CHARS_PER_TOKEN = 3.0
TRUNCATION_MARKER = "\n... [内容因 token 预算被截断] ...\n"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """按 token 预算截断文本，尽量在换行处断开。"""
    if not text:
        return text, False
    if max_tokens <= 0:
        return "", True
    if estimate_tokens(text) <= max_tokens:
        return text, False

    max_chars = max(0, int(max_tokens * CHARS_PER_TOKEN))
    cut = text[:max_chars]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    return cut.rstrip() + TRUNCATION_MARKER, True


def file_patch_cost(file_diff: ParsedChangedFile) -> int:
    """文件 patch 的估算成本，额外加一点元数据开销。"""
    return estimate_tokens(file_diff.patch or "") + 40


def split_parsed_diff(
    parsed_diff: ParsedDiff, max_tokens_per_batch: int
) -> List[ParsedDiff]:
    """按文件贪心分批，保证单批 patch 成本不超过预算。

    单个文件自身超预算时，会独占一个批次，由 PromptBuilder 再做截断，
    避免把同一个文件拆到两个批次里造成上下文割裂。
    """
    if max_tokens_per_batch <= 0 or not parsed_diff.files:
        return [parsed_diff]

    batches: List[ParsedDiff] = []
    current_files: List[ParsedChangedFile] = []
    current_cost = 0

    def flush() -> None:
        nonlocal current_files, current_cost
        if current_files:
            batches.append(
                ParsedDiff(
                    files=current_files,
                    total_additions=sum(f.additions for f in current_files),
                    total_deletions=sum(f.deletions for f in current_files),
                )
            )
            current_files = []
            current_cost = 0

    for file_diff in parsed_diff.files:
        cost = file_patch_cost(file_diff)
        if current_files and current_cost + cost > max_tokens_per_batch:
            flush()
        current_files.append(file_diff)
        current_cost += cost

    flush()
    return batches or [parsed_diff]
