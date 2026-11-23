"""Context-level deterministic filters applied before prompt assembly.

The retriever deliberately over-fetches definitions so the planner can keep
useful evidence under a token budget.  One class of evidence is redundant,
though: a ``base_definition`` that is already fully visible as deleted lines
in the patch.  Sending it again costs tokens without adding information.

This module keeps that rule in one place.  The caller must provide the set of
files whose patches are fully visible in the prompt; otherwise the filter keeps
everything.  That guard matters because the prompt builder truncates patches
per batch, and a base definition may be the only surviving copy of code whose
patch was cut off.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Set, Tuple

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.code_context import CodeContext


def is_base_definition_covered_by_deleted_lines(
    context: CodeContext,
    file_diff: ParsedChangedFile,
) -> bool:
    """Return True when every line of a base definition is deleted in the patch."""
    if context.kind != "base_definition" or context.revision != "base":
        return False
    if context.start_line is None or context.end_line is None:
        return False
    if context.end_line < context.start_line:
        return False

    deleted = set(file_diff.deleted_lines or [])
    if not deleted:
        return False
    return all(
        line in deleted
        for line in range(context.start_line, context.end_line + 1)
    )


def filter_redundant_base_contexts(
    contexts: Iterable[CodeContext],
    parsed_diff: ParsedDiff,
    *,
    fully_visible_files: Optional[Set[str]] = None,
) -> Tuple[List[CodeContext], List[CodeContext]]:
    """Drop base definitions already present as deleted patch lines.

    Returns ``(kept, dropped)``.  ``fully_visible_files`` is a safety guard:
    when it is ``None`` the filter is a no-op, because the caller has not
    proven that the patch survived prompt truncation.  When provided, only
    files in that set are considered.
    """
    if fully_visible_files is None:
        return list(contexts), []

    by_filename = {file.filename: file for file in parsed_diff.files}
    kept: List[CodeContext] = []
    dropped: List[CodeContext] = []
    for context in contexts:
        file_diff = by_filename.get(context.file_path)
        if (
            file_diff is not None
            and context.file_path in fully_visible_files
            and is_base_definition_covered_by_deleted_lines(context, file_diff)
        ):
            dropped.append(context)
            continue
        kept.append(context)
    return kept, dropped
