"""Bounded symbol search over a checked-out workspace.

The AST retriever needs "where else is this symbol referenced" answers.  The
obvious implementation, one ``git grep -e sym1 -e sym2 ...`` call, degrades
badly: git builds a single alternation regex, so cost grows with every extra
pattern *and* every matching line.  Measured on a 676-file / 10 MB Go repo:

* 1 pattern   -> 0.03s
* 10 patterns -> 2.8s
* 20 patterns -> 5.6s
* same 20 patterns with ripgrep -> 0.02s

With ``MAX_SEARCH_TARGETS = 50`` a normal PR therefore hit the 15s timeout in
every batch and review silently continued without reference context.  This
module keeps the same semantics but picks a search engine that stays bounded:

1. ``rg`` when it is installed (fixed strings, word boundaries, source globs);
2. otherwise ``git grep`` split into tiny pattern chunks with a shared time
   budget, so a pathological repo loses context instead of hanging review;
3. as a last resort, a bounded Python scan of source files.

All engines return the same ``(relative_path, line, text)`` triples, so the
caller does not care which one ran.  Every path is filtered through
:mod:`openreview.retriever.pathing`, which keeps grep / AST / index / impact
aligned on what counts as source.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from openreview.retriever.pathing import is_excluded_path, is_source_file

logger = logging.getLogger(__name__)

# Total wall-clock budget for one symbol search.  Retrieval is an enhancement,
# never a reason to stall a review.
GREP_TIMEOUT_SECONDS = 10.0
# ``git grep`` cost grows super-linearly with the number of ``-e`` patterns.
# Four keeps a chunk in the sub-200ms range on a mid-size repository.
GIT_GREP_CHUNK_SIZE = 4
# Hard cap on returned lines.  The retriever only keeps a handful of scored
# contexts; anything beyond this is parsing work we would throw away.
MAX_GREP_HITS = 2000
# Python fallback guards: never walk an unbounded tree.
MAX_FALLBACK_FILES = 5000
MAX_FALLBACK_FILE_SIZE = 500 * 1024

GrepHit = Tuple[str, int, str]


@dataclass
class GrepResult:
    """Hits plus enough metadata to explain a degraded search."""

    hits: List[GrepHit] = field(default_factory=list)
    engine: str = "none"
    targets: int = 0
    timeouts: int = 0
    truncated: bool = False
    # True only when the engine could not run at all (git missing, not a
    # repository).  Timeouts are *not* "unavailable": the engine works, it is
    # just slow, and rescanning the same tree in Python would be slower still.
    unavailable: bool = False


def _normalise_targets(targets: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for target in targets:
        cleaned = (target or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


def _parse_lines(
    output: str, max_hits: int
) -> Tuple[List[GrepHit], bool]:
    hits: List[GrepHit] = []
    seen: set[Tuple[str, int]] = set()
    truncated = False
    for raw in output.splitlines():
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        path, line_text, code = parts
        try:
            line = int(line_text)
        except ValueError:
            continue
        key = (path, line)
        if key in seen:
            continue
        seen.add(key)
        if len(hits) >= max_hits:
            truncated = True
            break
        hits.append((path, line, code))
    return hits, truncated


def _filter_hits(
    hits: Sequence[GrepHit], suffixes: Optional[Sequence[str]]
) -> List[GrepHit]:
    """Enforce the shared pruning policy regardless of which engine ran.

    Glob/pathspec filters are advisory (``docs/conf.py`` still matches
    ``*.py``), so the final say must come from ``pathing``.
    """
    allowed_suffixes = set(suffixes) if suffixes else None
    kept: List[GrepHit] = []
    for path, line, code in hits:
        if is_excluded_path(path):
            continue
        if allowed_suffixes is not None and Path(path).suffix not in allowed_suffixes:
            continue
        kept.append((path, line, code))
    return kept


def _path_globs(suffixes: Optional[Sequence[str]]) -> List[str]:
    return [f"*{suffix}" for suffix in suffixes or ()]


def _search_with_rg(
    repo_path: Path,
    targets: Sequence[str],
    suffixes: Optional[Sequence[str]],
    timeout: float,
    max_hits: int,
) -> Optional[GrepResult]:
    executable = shutil.which("rg")
    if not executable:
        return None

    command = [
        executable,
        "--no-heading",
        "--line-number",
        "--fixed-strings",
        "--word-regexp",
        "--no-messages",
        "--color",
        "never",
        "--no-config",
        # Deterministic output: truncation must not depend on walk order, and
        # review metrics/cache must be reproducible across runs.
        "--sort",
        "path",
        "-f",
        "-",
    ]
    for glob in _path_globs(suffixes):
        command.extend(["--glob", glob])

    try:
        process = subprocess.run(
            command,
            cwd=repo_path,
            input="\n".join(targets),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "ripgrep timed out after %.0fs for %d targets; "
            "continuing without reference context",
            timeout,
            len(targets),
        )
        return GrepResult(
            engine="rg",
            targets=len(targets),
            timeouts=1,
        )
    except OSError as exc:  # pragma: no cover - exotic environments
        logger.warning("ripgrep failed: %s", exc)
        return None

    if process.returncode not in (0, 1):
        logger.warning(
            "ripgrep failed with code %d: %s",
            process.returncode,
            process.stderr.strip()[:200],
        )
        return None

    hits, truncated = _parse_lines(process.stdout, max_hits=max_hits)
    return GrepResult(
        hits=hits,
        engine="rg",
        targets=len(targets),
        truncated=truncated,
    )


def _search_with_git_grep(
    repo_path: Path,
    targets: Sequence[str],
    suffixes: Optional[Sequence[str]],
    timeout: float,
    max_hits: int,
) -> GrepResult:
    result = GrepResult(engine="git-grep", targets=len(targets))
    failed_chunks = 0
    pathspecs = _path_globs(suffixes)
    remaining = timeout
    chunks = [
        targets[index : index + GIT_GREP_CHUNK_SIZE]
        for index in range(0, len(targets), GIT_GREP_CHUNK_SIZE)
    ]

    for chunk in chunks:
        if remaining <= 0:
            result.timeouts += 1
            logger.warning(
                "git grep budget exhausted after %d/%d chunks; "
                "returning partial reference context",
                chunks.index(chunk),
                len(chunks),
            )
            break

        command = ["git", "-C", str(repo_path), "grep", "-n", "-I", "-w", "-F"]
        for target in chunk:
            command.extend(["-e", target])
        if pathspecs:
            command.append("--")
            command.extend(pathspecs)

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            result.timeouts += 1
            logger.warning(
                "git grep timed out for %d targets; continuing without "
                "git-grep context",
                len(chunk),
            )
            continue
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Git is required but not found in system path."
            ) from exc

        if process.returncode not in (0, 1):
            logger.warning("git grep failed with code %d", process.returncode)
            failed_chunks += 1
            continue

        hits, truncated = _parse_lines(
            process.stdout, max_hits=max_hits - len(result.hits)
        )
        result.hits.extend(hits)
        if truncated:
            result.truncated = True
        if truncated or len(result.hits) >= max_hits:
            result.truncated = True
            break

    result.unavailable = failed_chunks > 0 and failed_chunks == len(chunks)
    return result


def _search_with_python(
    repo_path: Path,
    targets: Sequence[str],
    suffixes: Optional[Sequence[str]],
    max_hits: int,
) -> GrepResult:
    """Last-resort scan used when neither rg nor git grep is usable."""
    import re

    result = GrepResult(engine="python", targets=len(targets))
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(target) for target in targets) + r")\b"
    )
    scanned = 0
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(repo_path).as_posix()
        except ValueError:
            continue
        if suffixes and not is_source_file(relative, suffixes):
            continue
        if not suffixes and not is_source_file(relative, {path.suffix}):
            continue
        scanned += 1
        if scanned > MAX_FALLBACK_FILES:
            result.truncated = True
            break
        try:
            if path.stat().st_size > MAX_FALLBACK_FILE_SIZE:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            result.hits.append((relative, line_number, line))
            if len(result.hits) >= max_hits:
                result.truncated = True
                return result
    return result


def search_symbols(
    repo_path: Path,
    targets: Iterable[str],
    *,
    suffixes: Optional[Sequence[str]] = ('.py', '.go'),
    timeout: float = GREP_TIMEOUT_SECONDS,
    max_hits: int = MAX_GREP_HITS,
) -> GrepResult:
    """Find whole-word references to *targets* under *repo_path*.

    ``suffixes`` limits the scan to review-relevant source files.  Pass
    ``None`` to search every non-excluded file (impact analysis wants config
    and doc references too).
    """
    repo_path = Path(repo_path)
    ordered = _normalise_targets(targets)
    if not ordered or not repo_path.is_dir():
        return GrepResult(engine="none", targets=len(ordered))

    result = _search_with_rg(
        repo_path, ordered, suffixes, timeout, max_hits=max_hits
    )
    if result is not None:
        result.hits = _filter_hits(result.hits, suffixes)
        return result

    result = _search_with_git_grep(
        repo_path, ordered, suffixes, timeout, max_hits=max_hits
    )
    result.hits = _filter_hits(result.hits, suffixes)
    if not result.unavailable:
        return result

    # git grep itself failed (missing git, corrupt worktree).  A bounded scan
    # is better than silently returning "no references".
    fallback = _search_with_python(repo_path, ordered, suffixes, max_hits)
    fallback.timeouts = result.timeouts
    return fallback
