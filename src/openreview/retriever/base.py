"""Retriever contracts and a run-scoped adapter.

``Retriever`` is the minimal required surface. Optional capabilities
(dependency-aware batching, retrieval stats) are declared as separate
protocols so the executor can use explicit capability checks instead of
guessing arbitrary attribute names.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Set, runtime_checkable

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext


@runtime_checkable
class Retriever(Protocol):
    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> List[CodeContext]: ...


@runtime_checkable
class DependencyAwareRetriever(Protocol):
    def related_files(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> Dict[str, Set[str]]: ...


@runtime_checkable
class StatsReportingRetriever(Protocol):
    last_stats: Dict[str, float]


class ScopedRetriever:
    """Serialize access to a shared retriever and snapshot per-run stats.

    The underlying ``HybridRetriever`` keeps an index cache that is expensive
    to rebuild, so runs should share the retriever. ``last_stats`` is mutable
    instance state, though: two concurrent runs would otherwise read each
    other's counters. This adapter holds a pipeline-wide lock and copies stats
    while the lock is held, giving each run an isolated snapshot.
    """

    def __init__(self, inner: Retriever, lock: Optional[threading.Lock] = None):
        self.inner = inner
        self._lock = lock or threading.Lock()
        self.last_stats: Dict[str, float] = {}

    def retrieve(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> List[CodeContext]:
        with self._lock:
            contexts = self.inner.retrieve(
                repo_path, parsed_diff, base_path=base_path
            )
            stats = getattr(self.inner, "last_stats", None)
            self.last_stats = dict(stats) if isinstance(stats, dict) else {}
            return contexts

    def related_files(
        self,
        repo_path: Path,
        parsed_diff: ParsedDiff,
        base_path: Optional[Path] = None,
    ) -> Dict[str, Set[str]]:
        getter = getattr(self.inner, "related_files", None)
        if not callable(getter):
            return {}
        with self._lock:
            return dict(getter(repo_path, parsed_diff, base_path=base_path) or {})

    def close(self) -> None:
        closer = getattr(self.inner, "close", None)
        if callable(closer):
            closer()
