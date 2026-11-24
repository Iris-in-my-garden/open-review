"""Language scope for AACR-style evaluation.

The manifest language is a repository/PR bucket, not a per-file guarantee:
Go repositories can vendor C/C++/CUDA code, and Python repositories can carry
C++ extensions. Pure Go/Python evaluation should review only the primary
language, so the evaluation layer filters changed files by the manifest
``language`` before batching.

This is intentionally separate from ``openreview.review.file_filter``:
production reviews must not silently ignore real cross-language source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

LANGUAGE_SUFFIXES: dict[str, Tuple[str, ...]] = {
    "Go": (".go",),
    "Python": (".py", ".pyi"),
}


def allowed_suffixes(language: Optional[str]) -> Tuple[str, ...]:
    """Return the source suffixes for a manifest language.

    Unknown/empty languages return an empty tuple, which means "no language
    filter" rather than "skip everything".
    """
    if not language:
        return ()
    key = language.strip().title()
    return LANGUAGE_SUFFIXES.get(key, ())


def filter_comments_by_language(
    comments: Iterable[Any],
    language: Optional[str],
) -> List[Any]:
    """Keep reference comments that point at the primary language.

    AACR cases are repository-language buckets, but a PR can also contain
    C/C++/CUDA/notebook/doc comments. Evaluation must use the same scope as the
    review queue, otherwise those comments become unavoidable false negatives.
    """
    suffixes = set(allowed_suffixes(language))
    items = list(comments)
    if not suffixes:
        return items

    kept: List[Any] = []
    for comment in items:
        if not isinstance(comment, dict):
            continue
        path = (
            comment.get("path")
            or comment.get("file")
            or comment.get("filename")
            or comment.get("file_path")
        )
        # Comments without a path are kept for downstream validators to decide;
        # path-bearing comments must match the primary language.
        if not path or Path(str(path)).suffix.lower() in suffixes:
            kept.append(comment)
    return kept

