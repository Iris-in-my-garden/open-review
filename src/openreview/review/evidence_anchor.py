"""把模型引用的 evidence 对齐到真实源码的连续行区间。

模型经常把多行 evidence 挂在单行上，或在 evidence 里用 ``...`` 省略中间
内容。Validator 本身能通过窗口/token 匹配确认证据“存在”，但确定性指标
会严格按 ``start_line..end_line`` 截源码，于是把本来正确的 finding 判成
ungrounded。

这里做的是纯锚点修复：在可见 diff 行范围内找到 evidence 的真实起止行，
并返回源码原文。调用方负责把 ``line/end_line/evidence`` 写回 finding。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
ELLIPSIS_LINE = re.compile(r"^\s*(?:\.{3,}|…)\s*$")
INLINE_ELLIPSIS = re.compile(r"(?:\.{3,}|…)")
MAX_EVIDENCE_LINES = 60


@dataclass(frozen=True)
class EvidenceSpan:
    """Evidence 在源码中的连续区间（1-based，含首尾）。"""

    start_line: int
    end_line: int
    text: str


def canonicalize_evidence_span(
    evidence: str,
    *,
    line: Optional[int],
    side: str = "RIGHT",
    visible_lines: Sequence[int],
    source_lines: Mapping[int, str],
    prefer_line: Optional[int] = None,
    max_lines: int = MAX_EVIDENCE_LINES,
) -> Optional[EvidenceSpan]:
    """返回 evidence 对应的源码连续区间；无法唯一定位时返回 ``None``。

    ``source_lines`` 是 1-based 行号到源码行的映射。``prefer_line`` 用于
    尽量保留模型报告的行号：如果它落在 evidence 区间内，区间从该行开始，
    避免把行内评论移到证据前面的无关上下文。
    """
    text = (evidence or "").strip()
    if len(text) < 6 or line is None:
        return None
    if str(side).upper() not in {"LEFT", "RIGHT"}:
        return None

    visible = sorted(
        {
            int(number)
            for number in visible_lines
            if int(number) in source_lines
        }
    )
    if not visible:
        return None
    visible_set = set(visible)
    anchor = int(prefer_line if prefer_line is not None else line)

    segments = _evidence_segments(text, max_lines=max_lines)
    if not segments:
        return None

    # 先找完整连续片段。多个片段都能匹配时，优先保留离报告行最近
    # 的那一段；例如 evidence 用 ``...`` 串起 5 个 log 行，模型报告
    # 的是第 1 行，就不应因为最后一段 token 更多而把锚点挪到最后。
    exact_candidates: list[tuple[EvidenceSpan, int, int]] = []
    for segment in segments:
        target = _normalize("\n".join(segment))
        if not target:
            continue
        token_count = sum(
            len(TOKEN_PATTERN.findall(line)) for line in segment
        )
        for start in visible:
            span_lines = list(range(start, start + len(segment)))
            if any(number not in visible_set for number in span_lines):
                continue
            snippet = "\n".join(source_lines[number] for number in span_lines)
            if _normalize(snippet) != target:
                continue
            exact_candidates.append(
                (
                    _trim_span(
                        EvidenceSpan(start, span_lines[-1], snippet),
                        anchor=anchor,
                        source_lines=source_lines,
                    ),
                    len(segment),
                    token_count,
                )
            )
    if exact_candidates:
        return min(
            exact_candidates,
            key=lambda item: (
                0
                if item[0].start_line <= anchor <= item[0].end_line
                else 1,
                abs(item[0].start_line - anchor),
                -item[1],
                -item[2],
            ),
        )[0]

    # 缩进/空格/少量 token 差异时，按行 token 包含做一次有界回退。
    for segment in sorted(segments, key=_segment_score, reverse=True):
        span = _locate_by_line_tokens(
            segment,
            visible=visible,
            visible_set=visible_set,
            source_lines=source_lines,
            anchor=anchor,
            max_lines=max_lines,
        )
        if span is not None:
            return span

    # 最后一种情况：evidence 只有一部分落在 diff 可见行内（其余是未展示
    # 的上下文）。取证据中能连续对齐到源码的最长片段，避免把整个 finding
    # 判成 ungrounded。
    for segment in sorted(segments, key=_segment_score, reverse=True):
        span = _locate_longest_visible_run(
            segment,
            visible=visible,
            source_lines=source_lines,
            anchor=anchor,
            max_lines=max_lines,
        )
        if span is not None:
            return span
    return None


def _evidence_segments(
    evidence: str, *, max_lines: int
) -> list[list[str]]:
    """把 evidence 拆成不含 ``...`` 的连续片段。"""
    text = evidence.replace("\r\n", "\n").replace("\r", "\n").strip()
    raw_lines = [
        line
        for line in text.splitlines()
        if not line.strip().startswith("```")
    ]

    segments: list[list[str]] = []
    current: list[str] = []
    for line in raw_lines:
        if ELLIPSIS_LINE.match(line):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(line)
    if current:
        segments.append(current)

    # 行内省略号（例如 ``foo(...) ... bar()``）拆成更小的片段。
    expanded: list[list[str]] = []
    for segment in segments:
        if len(segment) == 1 and INLINE_ELLIPSIS.search(segment[0]):
            expanded.extend(
                [part]
                for part in INLINE_ELLIPSIS.split(segment[0])
                if part.strip()
            )
        else:
            expanded.append(segment)

    cleaned: list[list[str]] = []
    for segment in expanded:
        lines = list(segment)
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines or len(lines) > max_lines:
            continue
        if sum(len(TOKEN_PATTERN.findall(line)) for line in lines) < 2:
            continue
        cleaned.append(lines)
    return cleaned


def _segment_score(segment: Sequence[str]) -> tuple[int, int]:
    return (
        len(segment),
        sum(len(TOKEN_PATTERN.findall(line)) for line in segment),
    )


def _locate_by_line_tokens(
    segment: Sequence[str],
    *,
    visible: Sequence[int],
    visible_set: set[int],
    source_lines: Mapping[int, str],
    anchor: int,
    max_lines: int,
) -> Optional[EvidenceSpan]:
    positions: list[int] = []
    for evidence_line in segment:
        evidence_tokens = set(TOKEN_PATTERN.findall(evidence_line.lower()))
        if len(evidence_tokens) < 2:
            return None
        reference = positions[-1] if positions else anchor
        candidates = [
            number
            for number in visible
            if _token_containment(evidence_tokens, source_lines[number]) >= 0.9
        ]
        if not candidates:
            return None
        positions.append(
            min(candidates, key=lambda number: (abs(number - reference), number))
        )

    start = min(positions)
    end = max(positions)
    if end - start + 1 > max_lines:
        return None
    if any(number not in visible_set for number in range(start, end + 1)):
        return None
    snippet = "\n".join(source_lines[number] for number in range(start, end + 1))
    return _trim_span(
        EvidenceSpan(start, end, snippet),
        anchor=anchor,
        source_lines=source_lines,
    )


def _locate_longest_visible_run(
    segment: Sequence[str],
    *,
    visible: Sequence[int],
    source_lines: Mapping[int, str],
    anchor: int,
    max_lines: int,
) -> Optional[EvidenceSpan]:
    """找 evidence 与源码连续对齐的最长片段。

    适合“evidence 有 8 行，但 diff 只展示了其中 1 行”的情况：整段无法
    匹配，但这一行仍然是真实证据，不应因此判 ungrounded。
    """
    items: list[tuple[set[str], list[int]]] = []
    for evidence_line in segment:
        tokens = set(TOKEN_PATTERN.findall(evidence_line.lower()))
        if len(tokens) < 2:
            continue
        candidates = [
            number
            for number in visible
            if _token_containment(tokens, source_lines[number]) >= 0.9
        ]
        if candidates:
            items.append((tokens, candidates))
    if not items:
        return None

    best: Optional[tuple[tuple[int, int, int, int], int, int]] = None
    for start_index in range(len(items)):
        tokens, candidates = items[start_index]
        for start_line in candidates:
            run_tokens = len(tokens)
            end_line = start_line
            next_expected = start_line + 1
            for later_tokens, later_candidates in items[start_index + 1 :]:
                if next_expected not in later_candidates:
                    break
                run_tokens += len(later_tokens)
                end_line = next_expected
                next_expected += 1
            length = end_line - start_line + 1
            if length > max_lines:
                continue
            contains_anchor = 1 if start_line <= anchor <= end_line else 0
            score = (
                contains_anchor,
                length,
                run_tokens,
                -abs(start_line - anchor),
            )
            if best is None or score > best[0]:
                best = (score, start_line, end_line)

    if best is None:
        return None
    _, start_line, end_line = best
    snippet = "\n".join(
        source_lines[number] for number in range(start_line, end_line + 1)
    )
    return _trim_span(
        EvidenceSpan(start_line, end_line, snippet),
        anchor=anchor,
        source_lines=source_lines,
    )


def _trim_span(
    span: EvidenceSpan,
    *,
    anchor: int,
    source_lines: Mapping[int, str],
) -> EvidenceSpan:
    """尽量保留模型报告的行号，同时保证区间连续且能覆盖 evidence。"""
    if not span.start_line <= anchor <= span.end_line:
        return span
    if anchor == span.start_line:
        return span
    text = "\n".join(
        source_lines[number]
        for number in range(anchor, span.end_line + 1)
    )
    return EvidenceSpan(anchor, span.end_line, text)


def _token_containment(evidence_tokens: set[str], haystack: str) -> float:
    haystack_tokens = set(TOKEN_PATTERN.findall(haystack.lower()))
    if not evidence_tokens:
        return 0.0
    return len(evidence_tokens & haystack_tokens) / len(evidence_tokens)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())
