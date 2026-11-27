"""Repair evidence anchors in an existing results directory without re-running review.

The LLM often reports multi-line evidence on a single line, or inserts ``...``
between quoted snippets.  The validator now canonicalizes those anchors for new
runs; this module applies the same repair to results produced before that fix,
so deterministic metrics / gates can be evaluated on corrected anchors.

Only ``start_line``, ``end_line`` and ``evidence`` are rewritten.  Finding
content, severity, category and confidence are untouched.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping

from evaluation.aacr import instances_for_split, load_manifest, load_records
from evaluation.repos import read_file_at_commit, repo_cache_path
from openreview.repo.diff_parser import DiffParser
from openreview.repo.local import collect_local_changes
from openreview.review.evidence_anchor import canonicalize_evidence_span
from openreview.review.redaction import redact_text

DEFAULT_MANIFEST = Path("evaluation/manifests/aacr_pygo_v3.json")
DEFAULT_SOURCE = Path("evaluation/.cache/aacr/positive_samples.json")
DEFAULT_REPO_CACHE = Path("evaluation/.cache/repos")


def _source_lines(repo: Path, commit: str, path: str) -> Dict[int, str]:
    text = read_file_at_commit(repo, commit, path)
    if text is None:
        return {}
    return {
        number: line
        for number, line in enumerate(text.splitlines(), start=1)
    }


def _visible_lines(file_diff: Any, side: str) -> List[int]:
    return list(
        file_diff.old_visible_lines
        if str(side).upper() == "LEFT"
        else file_diff.visible_lines
    )


def repair_payload(
    payload: Dict[str, Any],
    *,
    instance: Dict[str, Any],
    repo: Path,
) -> tuple[int, int]:
    """Repair one result payload in place.  Returns (changed, skipped)."""
    head_commit = instance["head_commit"]
    base_commit = instance["base_commit"]
    changes = collect_local_changes(
        repo, base_commit, head_commit
    )
    parsed = DiffParser.parse_diff([change.model_dump() for change in changes])
    file_diffs = {file.filename: file for file in parsed.files}
    comments = (payload.get("review") or {}).get("comments") or []
    changed = 0
    skipped = 0
    for comment in comments:
        path = str(comment.get("path") or "")
        file_diff = file_diffs.get(path)
        if file_diff is None:
            skipped += 1
            continue
        side = str(comment.get("side") or "RIGHT").upper()
        line = comment.get("start_line")
        if line is None:
            skipped += 1
            continue
        commit = base_commit if side == "LEFT" else head_commit
        raw_source = _source_lines(repo, commit, path)
        if not raw_source:
            skipped += 1
            continue
        redacted_source: Mapping[int, str] = {
            number: redact_text(text)[0]
            for number, text in raw_source.items()
        }
        span = canonicalize_evidence_span(
            redact_text(str(comment.get("evidence") or ""))[0],
            line=int(line),
            side=side,
            visible_lines=_visible_lines(file_diff, side),
            source_lines=redacted_source,
            prefer_line=int(line),
        )
        if span is None:
            skipped += 1
            continue
        if (
            span.start_line == comment.get("start_line")
            and span.end_line
            == (comment.get("end_line") or comment.get("start_line"))
            and span.text == comment.get("evidence")
        ):
            continue
        comment["start_line"] = span.start_line
        comment["end_line"] = span.end_line
        comment["evidence"] = span.text
        changed += 1
    return changed, skipped


def repair_results(
    *,
    manifest_path: Path,
    source_path: Path,
    split: str,
    results_dir: Path,
    output_dir: Path,
    repo_cache: Path,
) -> Dict[str, int]:
    manifest = load_manifest(manifest_path)
    records = load_records(source_path)
    instances = instances_for_split(manifest, records, split)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(results_dir, output_dir)

    stats = {"instances": 0, "changed": 0, "skipped": 0}
    for instance in instances:
        path = output_dir / f"{instance['instance_id'].replace('/', '__')}.json"
        if not path.exists():
            continue
        repo = repo_cache_path(repo_cache, instance["repo"])
        if not (repo / ".git").exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        changed, skipped = repair_payload(
            payload, instance=instance, repo=repo
        )
        stats["instances"] += 1
        stats["changed"] += changed
        stats["skipped"] += skipped
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-cache", type=Path, default=DEFAULT_REPO_CACHE)
    args = parser.parse_args()
    stats = repair_results(
        manifest_path=args.manifest,
        source_path=args.source,
        split=args.split,
        results_dir=args.results_dir,
        output_dir=args.output_dir,
        repo_cache=args.repo_cache,
    )
    print(
        "repair done: instances={instances} changed={changed} "
        "skipped={skipped}".format(**stats)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
