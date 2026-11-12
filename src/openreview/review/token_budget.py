"""轻量 token 预算与 PR 分批工具。

这里不引入 tiktoken，使用保守的字符/token 估算（默认 1 token ≈ 3 字符），
用于在超长 PR 上做“够用就好”的预算控制，而不是精确计费。
"""

import math
import re
from typing import Dict, Iterable, List, Mapping, Optional

from openreview.repo.diff_parser import (
    HUNK_CONTEXT_PATTERN,
    DiffParser,
    ParsedChangedFile,
    ParsedDiff,
)

CHARS_PER_TOKEN = 3.0
CJK_TOKEN_WEIGHT = 2.2
_CJK_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3040-\u30ff\uac00-\ud7af]"
)
TRUNCATION_MARKER = "\n... [内容因 token 预算被截断] ...\n"

# prompt 预算的内部策略：不暴露给 CLI，用户只控制成本护栏。
# soft 用于把超限内容打包成多个调用；hard 是触发拆分的上限。
DEFAULT_SOFT_PROMPT_TOKENS = 10_000
DEFAULT_HARD_PROMPT_TOKENS = 14_000
# system + 文件摘要 + 任务/输出约束的保守估算。
PROMPT_OVERHEAD_TOKENS = 2_000


def estimate_tokens(text: str) -> int:
    """Conservative token estimate without a model-specific tokenizer.

    A flat ``len/3`` underestimates CJK text badly: Chinese is closer to 1.0-
    1.5 characters per token, not 3. Since PR descriptions and comments are
    often Chinese, the old heuristic could under-count by 2-3x and silently
    blow the hard budget. ASCII/code keeps the historical 3 chars/token, so
    existing prompt sizing stays stable.
    """
    if not text:
        return 0
    cjk = len(_CJK_PATTERN.findall(text))
    if cjk == 0:
        return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))
    non_cjk = max(0, len(text) - cjk)
    # CJK characters carry roughly 2.2x the token weight of ASCII on modern
    # tokenizers; this is intentionally pessimistic for budget safety.
    effective_chars = non_cjk + cjk * CJK_TOKEN_WEIGHT
    return max(1, math.ceil(effective_chars / CHARS_PER_TOKEN))


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


def context_reserve_tokens_for(soft_prompt_tokens: int) -> int:
    """给检索上下文预留的 token 数，避免 patch 把预算吃光。"""
    return max(2_000, max(0, soft_prompt_tokens) // 4)


def _hunk_symbol_key(hunk: str) -> str:
    """用 hunk header 末尾的函数/类签名作为分组键。

    第一个 hunk 可能带着 diff 元信息前缀，所以不能只看第一行。
    """
    for line in hunk.splitlines():
        if line.startswith("@@"):
            match = HUNK_CONTEXT_PATTERN.match(line)
            return match.group(1).strip() if match else ""
    return ""


def _group_hunks_by_symbol(hunks: List[str]) -> List[List[str]]:
    """把连续、属于同一符号的 hunk 合在一起，避免拆散一个函数。"""
    groups: List[List[str]] = []
    current: List[str] = []
    current_key: str | None = None

    for hunk in hunks:
        key = _hunk_symbol_key(hunk)
        if current and key != current_key:
            groups.append(current)
            current = []
        current.append(hunk)
        current_key = key

    if current:
        groups.append(current)
    return groups


def _slice_oversized_hunk(
    file_diff: ParsedChangedFile,
    hunk: str,
    patch_soft: int,
) -> ParsedChangedFile:
    """单个 hunk 仍超预算时截断，保证不会生成无限大的单次调用。"""
    truncated, _ = truncate_to_tokens(hunk, max(1, patch_soft - 40))
    return DiffParser.slice_changed_file(file_diff, [truncated])


def _file_to_units(
    file_diff: ParsedChangedFile,
    patch_soft: int,
) -> List[ParsedChangedFile]:
    """把一个文件拆成尽量大的、语义连续的 review unit。"""
    if not file_diff.patch or file_patch_cost(file_diff) <= patch_soft:
        return [file_diff]

    hunks = DiffParser.split_patch_into_hunks(file_diff.patch)
    if len(hunks) <= 1:
        # 单个 hunk 无法再按语义切分，只能截断。
        return [
            _slice_oversized_hunk(file_diff, hunks[0], patch_soft)
            if hunks
            else file_diff
        ]

    units: List[ParsedChangedFile] = []
    for group in _group_hunks_by_symbol(hunks):
        group_cost = estimate_tokens("".join(group)) + 40
        if group_cost <= patch_soft:
            units.append(DiffParser.slice_changed_file(file_diff, group))
            continue

        # 同一个符号仍然太大，退化为逐个 hunk；单个 hunk 过大再截断。
        for hunk in group:
            if estimate_tokens(hunk) + 40 <= patch_soft:
                units.append(DiffParser.slice_changed_file(file_diff, [hunk]))
            else:
                units.append(
                    _slice_oversized_hunk(file_diff, hunk, patch_soft)
                )
    return units


def _coalesce_same_file_units(
    files: List[ParsedChangedFile],
) -> List[ParsedChangedFile]:
    """把同一批次里来自同一文件的 hunk unit 合回一个文件。

    下游 ``FindingValidator`` 用 ``{filename: file}`` 建索引；如果同一批里
    同一个文件出现多次，只有最后一个 unit 的 ``visible_lines`` 会生效，
    前面 unit 的合法行号会被误判为无效。合并后 patch 仍是合法的多 hunk
    diff，行号元数据由 ``with_patch`` 重新计算。
    """
    merged: List[ParsedChangedFile] = []
    for file_diff in files:
        if merged and merged[-1].filename == file_diff.filename:
            previous = merged[-1]
            merged[-1] = DiffParser.with_patch(
                previous,
                (previous.patch or "") + (file_diff.patch or ""),
            )
        else:
            merged.append(file_diff)
    return merged


def _order_units_by_dependency(
    units: List[ParsedChangedFile],
    dependencies: Optional[Mapping[str, Iterable[str]]],
) -> List[ParsedChangedFile]:
    """把有依赖关系的文件排在一起，让它们在预算允许时进入同一批。

    batch 是按顺序贪心打包的；如果 ``a.py`` 和它的调用方 / 测试文件被
    分到不同批次，模型就看不到跨文件交互，容易漏报真实问题或产生误报。
    这里用并查集把依赖图拆成连通分量，再按第一次出现的顺序稳定排序。
    同一文件的所有 hunk unit 共享同一个分量，因此文件内部顺序不变。
    """
    if not dependencies:
        return units

    parent: Dict[str, str] = {}

    def find(name: str) -> str:
        parent.setdefault(name, name)
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for source, targets in dependencies.items():
        if not source:
            continue
        for target in targets:
            if target:
                union(source, target)

    group_order: Dict[str, int] = {}
    for unit in units:
        root = find(unit.filename)
        group_order.setdefault(root, len(group_order))

    # sorted 是稳定排序：同一连通分量内保持原始 diff 顺序。
    return sorted(units, key=lambda unit: group_order[find(unit.filename)])


def _pack_units(
    units: List[ParsedChangedFile],
    target_tokens: int,
) -> List[List[ParsedChangedFile]]:
    """按顺序贪心打包；单个 unit 超预算时独占一个 bin。"""
    bins: List[List[ParsedChangedFile]] = []
    current: List[ParsedChangedFile] = []
    current_cost = 0

    for unit in units:
        cost = file_patch_cost(unit)
        if current and current_cost + cost > target_tokens:
            bins.append(_coalesce_same_file_units(current))
            current = []
            current_cost = 0
        current.append(unit)
        current_cost += cost

    if current:
        bins.append(_coalesce_same_file_units(current))
    return bins


def _make_batch(files: List[ParsedChangedFile]) -> ParsedDiff:
    return ParsedDiff(
        files=files,
        total_additions=sum(f.additions for f in files),
        total_deletions=sum(f.deletions for f in files),
    )


def plan_review_batches(
    parsed_diff: ParsedDiff,
    *,
    soft_prompt_tokens: int = DEFAULT_SOFT_PROMPT_TOKENS,
    hard_prompt_tokens: int = DEFAULT_HARD_PROMPT_TOKENS,
    max_batch_tokens: int = 0,
    prompt_overhead_tokens: int = PROMPT_OVERHEAD_TOKENS,
    context_reserve_tokens: int | None = None,
    dependencies: Optional[Mapping[str, Iterable[str]]] = None,
) -> List[ParsedDiff]:
    """按最终 prompt 预算规划 review batch。

    策略：

    - 总 patch 成本 <= ``patch_hard`` 时**不拆**，保留完整上下文；
    - 超过 ``patch_hard`` 才拆，并尽量按 ``patch_soft`` 把 bin 装到接近上限；
    - 文件过大时先按 hunk / 符号切片，保持语义连续；
    - ``max_batch_tokens`` 作为外部硬上限（兼容旧调用），只能让 batch 更小；
    - ``dependencies`` 描述文件之间的依赖（import / 测试关联），用于把相关
      文件尽量排进同一批次。
    """
    if not parsed_diff.files:
        return [parsed_diff]
    if soft_prompt_tokens <= 0 or hard_prompt_tokens <= 0:
        return split_parsed_diff(parsed_diff, max_batch_tokens)

    if context_reserve_tokens is None:
        context_reserve_tokens = context_reserve_tokens_for(
            soft_prompt_tokens
        )

    patch_soft = max(
        1,
        soft_prompt_tokens - prompt_overhead_tokens - context_reserve_tokens,
    )
    patch_hard = max(
        1,
        hard_prompt_tokens - prompt_overhead_tokens - context_reserve_tokens,
    )
    if max_batch_tokens > 0:
        patch_soft = min(patch_soft, max_batch_tokens)
        patch_hard = min(patch_hard, max_batch_tokens)
    patch_soft = min(patch_soft, patch_hard)

    total_cost = sum(file_patch_cost(f) for f in parsed_diff.files)
    if total_cost <= patch_hard:
        return [parsed_diff]

    units: List[ParsedChangedFile] = []
    for file_diff in parsed_diff.files:
        units.extend(_file_to_units(file_diff, patch_soft))
    if not units:
        return [parsed_diff]

    units = _order_units_by_dependency(units, dependencies)

    # 先决定最少需要几个 bin，再把预算平均分配，避免最后一个 bin 过小。
    bin_count = max(1, math.ceil(total_cost / patch_soft))
    target = min(patch_soft, max(1, math.ceil(total_cost / bin_count)))
    bins = _pack_units(units, target)
    return [_make_batch(batch) for batch in bins]
