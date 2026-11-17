"""AST 分支折叠：把超长方法压缩到 token 预算内。

Code review 需要的是“变更行周围的完整语义”，而不是一个 800 行函数的
全部实现。这里用 tree-sitter 找到可折叠的 block / 嵌套定义，把未变更的
大段分支替换成 ``# ... (N lines folded)``。

硬约束：``changed_lines`` 命中的行永远不能被折叠。任何折叠结果都必须
保留这些行，否则 reviewer 会看不到自己正在审查的代码。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Set, Tuple

from openreview.retriever.ast_utils import DEFINITION_TYPES, find_enclosing_definition, parse_text
from openreview.review.token_budget import estimate_tokens

# Python 的函数体是 block；Go 的函数体是 block；声明列表用于 class/struct。
COLLAPSIBLE_BLOCKS = {"block", "statement_list", "declaration_list"}
MIN_COLLAPSE_LINES = 6
MARKERS = {
    ".py": "# ...",
    ".go": "// ...",
}
DEFAULT_MARKER = "// ..."

_INDENT = re.compile(r"^[ \t]*")


def fold_snippet(
    text: str,
    suffix: str,
    start_line: int,
    end_line: int,
    changed_lines: Sequence[int],
    max_tokens: int,
) -> Tuple[str, bool]:
    """折叠 ``start_line``–``end_line`` 之间的超长定义。

    返回 ``(snippet, folded)``；``folded`` 只表示是否真的省略了内容。
    如果解析失败，退化为保留头尾与变更行附近的窗口。
    """
    lines = text.splitlines()
    if not lines:
        return "", False

    start = max(1, min(start_line, len(lines)))
    end = max(start, min(end_line, len(lines)))
    protected = {
        int(line)
        for line in changed_lines
        if start <= int(line) <= end
    }

    snippet = "\n".join(lines[start - 1 : end])
    if max_tokens <= 0 or estimate_tokens(snippet) <= max_tokens:
        return snippet, False

    parsed = parse_text(text, suffix)
    if parsed is not None:
        _, _, tree = parsed
        node = find_enclosing_definition(tree, start - 1)
        if node is not None:
            root = node
            if root.parent is not None and root.parent.type == "decorated_definition":
                root = root.parent
            collapses = _collect_collapses(root, protected, MIN_COLLAPSE_LINES)
            folded = _render(lines, start, end, collapses, suffix)
            if estimate_tokens(folded) <= max_tokens:
                return folded, True

            # 第二次更激进：只保留控制流头部和变更行。
            collapses = _collect_collapses(root, protected, 2)
            folded = _render(lines, start, end, collapses, suffix)
            if estimate_tokens(folded) <= max_tokens:
                return folded, True

    return _fold_window(lines, start, end, protected, max_tokens, suffix)


def _row_span(node) -> Tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def _intersects(start: int, end: int, protected: Set[int]) -> bool:
    if not protected:
        return False
    return any(start <= line <= end for line in protected)


def _collect_collapses(
    node,
    protected: Set[int],
    min_lines: int,
) -> List[Tuple[int, int]]:
    """收集需要折叠的 1-based 行区间（含首尾）。"""
    collapses: List[Tuple[int, int]] = []

    def walk(current) -> None:
        for child in current.children:
            child_start, child_end = _row_span(child)
            touches_changed = _intersects(child_start, child_end, protected)

            if child.type in COLLAPSIBLE_BLOCKS:
                if touches_changed:
                    walk(child)
                elif child_end - child_start + 1 >= min_lines:
                    collapses.append((child_start, child_end))
                else:
                    walk(child)
                continue

            if child.type in DEFINITION_TYPES and child.id != node.id:
                body = child.child_by_field_name("body")
                if body is not None:
                    body_start, body_end = _row_span(body)
                    if not _intersects(body_start, body_end, protected):
                        collapses.append((body_start, body_end))
                        continue
                elif not touches_changed:
                    collapses.append((child_start, child_end))
                    continue
                walk(child)
                continue

            walk(child)

    walk(node)
    return _merge_ranges(collapses)


def _merge_ranges(ranges: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    ordered = sorted(ranges)
    merged: List[Tuple[int, int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _render(
    lines: List[str],
    start: int,
    end: int,
    collapses: Sequence[Tuple[int, int]],
    suffix: str,
) -> str:
    marker = MARKERS.get(suffix, DEFAULT_MARKER)
    clipped: List[Tuple[int, int]] = []
    for raw_start, raw_end in collapses:
        range_start = max(start, raw_start)
        range_end = min(end, raw_end)
        if range_start <= range_end:
            clipped.append((range_start, range_end))
    clipped = _merge_ranges(clipped)

    output: List[str] = []
    index = start
    range_index = 0
    while index <= end:
        if range_index < len(clipped) and index == clipped[range_index][0]:
            collapse_start, collapse_end = clipped[range_index]
            match = _INDENT.match(lines[collapse_start - 1])
            indent = match.group(0) if match else ""
            omitted = collapse_end - collapse_start + 1
            output.append(f"{indent}{marker} ({omitted} lines folded)")
            index = collapse_end + 1
            range_index += 1
            continue
        output.append(lines[index - 1])
        index += 1
    return "\n".join(output)


def _fold_window(
    lines: List[str],
    start: int,
    end: int,
    protected: Set[int],
    max_tokens: int,
    suffix: str,
) -> Tuple[str, bool]:
    """兜底：保留定义头、变更行附近和尾部，其余折叠。

    先保留 8 行头 + changed lines ±3 + 4 行尾；如果仍然超预算，逐步缩小
    头尾窗口，但 always keep protected 行。
    """
    marker = MARKERS.get(suffix, DEFAULT_MARKER)

    for head, tail, radius in ((8, 4, 3), (4, 2, 2), (2, 1, 1)):
        keep: Set[int] = set(range(start, min(end, start + head - 1) + 1))
        keep.update(range(max(start, end - tail + 1), end + 1))
        for line in protected:
            keep.update(range(max(start, line - radius), min(end, line + radius) + 1))

        output: List[str] = []
        index = start
        while index <= end:
            if index in keep:
                output.append(lines[index - 1])
                index += 1
                continue
            gap_start = index
            while index <= end and index not in keep:
                index += 1
            omitted = index - gap_start
            if omitted <= 0:
                continue
            match = _INDENT.match(lines[gap_start - 1])
            indent = match.group(0) if match else ""
            output.append(f"{indent}{marker} ({omitted} lines folded)")
        rendered = "\n".join(output)
        if estimate_tokens(rendered) <= max_tokens:
            return rendered, True

    # 极端情况：changed lines 本身已经超预算，只保留变更行。
    rendered = "\n".join(lines[line - 1] for line in sorted(protected) if start <= line <= end)
    return rendered or "\n".join(lines[start - 1 : end]), True
