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

from typing import Optional, Tuple

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
