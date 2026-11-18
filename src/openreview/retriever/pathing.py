"""Shared repository path filtering for every retrieval mechanism.

AST retrieval, the SQLite symbol index and impact analysis used to keep their
own directory exclusion lists.  The lists drifted (for example ``docs`` and
``examples`` were skipped by one path but indexed by another), which made
"what can be retrieved" depend on which mechanism happened to run.  Keep the
policy in one place so AST / index / graph / impact agree on the source surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Collection

# Directories that never contain review-relevant source code.  ``vendor`` is
# excluded from all mechanisms: third-party code is not part of the PR's
# change surface, and indexing it mostly adds noise.
EXCLUDED_DIRS = frozenset(
    {
        ".cache",
        ".git",
        ".idea",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "docs",
        "examples",
        "node_modules",
        "site-packages",
        "vendor",
        "venv",
    }
)


def is_excluded_path(rel_path: str | Path) -> bool:
    """Return True when any path component is a known non-source directory."""
    return any(part in EXCLUDED_DIRS for part in Path(rel_path).parts)


def is_source_file(rel_path: str | Path, suffixes: Collection[str]) -> bool:
    """True when *rel_path* is a supported source file outside excluded dirs."""
    path = Path(rel_path)
    return path.suffix in suffixes and not is_excluded_path(path)
