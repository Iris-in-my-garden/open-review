"""Context-level deterministic filters applied before prompt assembly.

The retriever deliberately over-fetches definitions so the planner can keep
useful evidence under a token budget.  One class of evidence is redundant,
though: a ``base_definition`` that is already fully visible on the old side of
the patch (deleted lines *and* unchanged context lines).  Sending it again
costs tokens without adding information.

The prompt builder truncates patches per batch, so "visible" is not the same
as "the file's patch was fully rendered".  A truncated patch still shows a
prefix of its hunks, and a base definition inside that prefix is just as
redundant.  Callers therefore pass the old-side line numbers that actually
survived truncation.  Without that information the filter keeps everything,
because a base definition may be the only surviving copy of code whose patch
was cut off.
"""

from __future__ import annotations

from typing import AbstractSet, Iterable, List, Mapping, Optional, Set, Tuple

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.code_context import CodeContext


def _old_side_visible_lines(file_diff: ParsedChangedFile) -> Set[int]:
    """Return the old-file lines already visible in the patch.

    ``old_visible_lines`` is the authoritative field: it contains deleted
    lines plus unchanged context lines.  Older/synthetic diffs may only carry
    ``deleted_lines``, so fall back to that to preserve previous behaviour.
    """
    return set(file_diff.old_visible_lines or file_diff.deleted_lines or [])


def is_base_definition_visible_in_patch(
    context: CodeContext,
    file_diff: ParsedChangedFile,
    *,
    visible_lines: Optional[AbstractSet[int]] = None,
) -> bool:
    """Return True when every line of a base definition is visible in the patch.

    The patch shows both deleted lines and unchanged context lines on the old
    side.  If the whole base definition is already there, re-injecting it as
    ``base_definition`` is pure duplication.

    ``visible_lines`` overrides the file's full old-side visibility with the
    subset that actually survived prompt truncation.
    """
    if context.kind != "base_definition" or context.revision != "base":
        return False
    if context.start_line is None or context.end_line is None:
        return False
    if context.end_line < context.start_line:
        return False

    visible = (
        set(visible_lines)
        if visible_lines is not None
        else _old_side_visible_lines(file_diff)
    )
    if not visible:
        return False
    return all(
        line in visible
        for line in range(context.start_line, context.end_line + 1)
    )


# Backwards-compatible name: the old filter only considered deleted lines, but
# the predicate now covers the full old side of the patch.
is_base_definition_covered_by_deleted_lines = is_base_definition_visible_in_patch


def filter_redundant_base_contexts(
    contexts: Iterable[CodeContext],
    parsed_diff: ParsedDiff,
    *,
    fully_visible_files: Optional[Set[str]] = None,
    visible_old_lines_by_file: Optional[Mapping[str, AbstractSet[int]]] = None,
) -> Tuple[List[CodeContext], List[CodeContext]]:
    """Drop base definitions already visible on the old side of the patch.

    Returns ``(kept, dropped)``.

    ``visible_old_lines_by_file`` is the precise form: it maps each file to
    the old-side line numbers actually rendered in the prompt, including the
    prefix that survives patch truncation.  Files absent from the mapping are
    left alone.

    ``fully_visible_files`` is the coarser, backwards-compatible form: only
    files whose whole patch survived truncation are considered, and their
    visibility is taken from the parsed diff.  It is ignored when the precise
    mapping is provided.  When both are ``None`` the filter is a no-op,
    because the caller has not proven which lines survived truncation.
    """
    if fully_visible_files is None and visible_old_lines_by_file is None:
        return list(contexts), []

    by_filename = {file.filename: file for file in parsed_diff.files}
    kept: List[CodeContext] = []
    dropped: List[CodeContext] = []
    for context in contexts:
        file_diff = by_filename.get(context.file_path)
        if file_diff is None:
            kept.append(context)
            continue

        visible: Optional[Set[int]] = None
        if visible_old_lines_by_file is not None:
            lines = visible_old_lines_by_file.get(context.file_path)
            if lines is None:
                kept.append(context)
                continue
            visible = set(lines)
        elif context.file_path not in (fully_visible_files or set()):
            kept.append(context)
            continue

        if is_base_definition_visible_in_patch(
            context, file_diff, visible_lines=visible
        ):
            dropped.append(context)
            continue
        kept.append(context)
    return kept, dropped
