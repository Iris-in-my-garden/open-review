"""Split a manifest into repo-aware shards and merge sharded run results.

The review pipeline is serial across PRs, so the practical way to shorten a
20-PR dev run is to start a few independent processes.  Shards must be built
along repository boundaries: concurrent ``git worktree`` / index operations
on the same repository are the main source of avoidable contention.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

DEFAULT_SUMMARY = "summary_openreview.json"


def build_shard_manifests(
    manifest_path: Path,
    shard_groups: Mapping[str, Sequence[str]],
    *,
    output_dir: Path,
    tune_manifest_path: Optional[Path] = None,
) -> Dict[str, Path]:
    """Write one manifest per shard plus an optional combined tune manifest."""
    base = _load_json(Path(manifest_path))
    by_id = {item["instance_id"]: item for item in base["instances"]}

    seen: Dict[str, str] = {}
    all_ids: List[str] = []
    for shard, ids in shard_groups.items():
        if not ids:
            raise ValueError(f"shard {shard!r} is empty")
        for instance_id in ids:
            if instance_id not in by_id:
                raise ValueError(
                    f"instance not found in {manifest_path}: {instance_id}"
                )
            if instance_id in seen:
                raise ValueError(
                    f"instance {instance_id} appears in both "
                    f"{seen[instance_id]!r} and {shard!r}"
                )
            seen[instance_id] = shard
            all_ids.append(instance_id)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {}
    for shard, ids in shard_groups.items():
        shard_manifest = _with_instances(base, [by_id[i] for i in ids])
        path = output_dir / f"{shard}.json"
        _write_json(path, shard_manifest)
        paths[shard] = path

    if tune_manifest_path is not None:
        ordered = [
            item
            for item in base["instances"]
            if item["instance_id"] in set(all_ids)
        ]
        _write_json(Path(tune_manifest_path), _with_instances(base, ordered))
    return paths


def merge_results(
    run_dirs: Sequence[Path],
    *,
    output_dir: Path,
    instance_order: Optional[Sequence[str]] = None,
    force: bool = False,
) -> List[Dict[str, Any]]:
    """Merge per-shard result JSONs and summaries into one run directory."""
    output_dir = Path(output_dir)
    if output_dir.exists():
        if not force:
            raise FileExistsError(
                f"{output_dir} already exists; pass force=True to replace it"
            )
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows_by_id: Dict[str, Dict[str, Any]] = {}
    for run_dir in run_dirs:
        run_dir = Path(run_dir)
        if not run_dir.exists():
            raise FileNotFoundError(f"shard result dir not found: {run_dir}")
        for path in run_dir.glob("*.json"):
            if path.name.startswith("summary_"):
                continue
            shutil.copy2(path, output_dir / path.name)
        summary_path = run_dir / DEFAULT_SUMMARY
        if not summary_path.exists():
            continue
        for row in _load_json(summary_path):
            instance_id = row["instance_id"]
            if instance_id in rows_by_id:
                raise ValueError(
                    f"duplicate summary row for {instance_id}; "
                    "check the shard manifests"
                )
            rows_by_id[instance_id] = row

    if instance_order is not None:
        rows = [
            rows_by_id[instance_id]
            for instance_id in instance_order
            if instance_id in rows_by_id
        ]
    else:
        rows = sorted(rows_by_id.values(), key=lambda row: row["instance_id"])

    _write_json(output_dir / DEFAULT_SUMMARY, rows)
    return rows


def semantic_scores(
    metrics_dir: Path,
    *,
    instance_ids: Optional[Iterable[str]] = None,
) -> Optional[Dict[str, float]]:
    """Aggregate AACR semantic P/R/F1 from a metrics directory or JSON file."""
    path = _latest_semantic_metrics(Path(metrics_dir))
    if path is None:
        return None
    rows = _load_json(path).get("eval_res") or []
    if instance_ids is not None:
        wanted = set(instance_ids)
        rows = [row for row in rows if row["instance_id"] in wanted]

    generated = sum(len(row.get("generated_comments") or []) for row in rows)
    references = sum(len(row.get("comments") or []) for row in rows)
    matched_generated = sum(
        1
        for row in rows
        for comment in (row.get("generated_comments") or [])
        if comment.get("hit_new_gt")
    )
    matched_references = sum(
        1
        for row in rows
        for comment in (row.get("comments") or [])
        if comment.get("file_semantic_match")
    )
    precision = matched_generated / generated if generated else 0.0
    recall = matched_references / references if references else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "instances": float(len(rows)),
        "generated": float(generated),
        "references": float(references),
        "matched_generated": float(matched_generated),
        "matched_references": float(matched_references),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _with_instances(
    base: Mapping[str, Any], instances: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    manifest = dict(base)
    manifest["instances"] = list(instances)
    manifest["counts"] = _recount(instances)
    return manifest


def _recount(instances: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, Any] = {"total": len(instances)}
    for split in ("dev", "holdout", "test"):
        count = sum(1 for item in instances if item["split"] == split)
        if count:
            counts[split] = count
    by_language = Counter(item["language"] for item in instances)
    counts["by_language"] = dict(sorted(by_language.items()))
    by_split_language: Dict[str, Dict[str, int]] = {}
    repositories_by_split: Dict[str, int] = {}
    for split in ("dev", "holdout", "test"):
        split_items = [item for item in instances if item["split"] == split]
        if not split_items:
            continue
        by_split_language[split] = dict(
            sorted(Counter(item["language"] for item in split_items).items())
        )
        repositories_by_split[split] = len(
            {item["repo"] for item in split_items}
        )
    if by_split_language:
        counts["by_split_language"] = by_split_language
    if repositories_by_split:
        counts["repositories_by_split"] = repositories_by_split
    return counts


def _latest_semantic_metrics(path: Path) -> Optional[Path]:
    if path.is_file():
        return path
    if not path.exists():
        return None
    candidates = sorted(path.glob("metrics_openreview_*.json"))
    return candidates[-1] if candidates else None


def _load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_shards(values: Sequence[str]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--shard must be name=id1,id2: {value!r}")
        name, raw_ids = value.split("=", 1)
        ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
        groups[name.strip()] = ids
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="write shard manifests")
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument(
        "--shard",
        action="append",
        required=True,
        help="name=instance_id1,instance_id2",
    )
    build.add_argument("--tune-manifest", type=Path, default=None)

    merge = subparsers.add_parser("merge", help="merge shard result dirs")
    merge.add_argument("--output-dir", type=Path, required=True)
    merge.add_argument("--run", type=Path, action="append", required=True)
    merge.add_argument("--instance-order-manifest", type=Path, default=None)
    merge.add_argument("--force", action="store_true")

    compare = subparsers.add_parser("compare", help="compare semantic metrics")
    compare.add_argument("--baseline-metrics", type=Path, required=True)
    compare.add_argument("--candidate-metrics", type=Path, required=True)
    compare.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="restrict comparison to this manifest's instances",
    )

    args = parser.parse_args()
    if args.command == "build":
        paths = build_shard_manifests(
            args.manifest,
            _parse_shards(args.shard),
            output_dir=args.output_dir,
            tune_manifest_path=args.tune_manifest,
        )
        for name, path in paths.items():
            print(f"{name}: {path}")
        return 0

    if args.command == "merge":
        instance_order = None
        if args.instance_order_manifest is not None:
            instance_order = [
                item["instance_id"]
                for item in _load_json(args.instance_order_manifest)["instances"]
            ]
        rows = merge_results(
            args.run,
            output_dir=args.output_dir,
            instance_order=instance_order,
            force=args.force,
        )
        print(f"merged {len(rows)} instances -> {args.output_dir}")
        return 0

    instance_ids = None
    if args.manifest is not None:
        instance_ids = [
            item["instance_id"]
            for item in _load_json(args.manifest)["instances"]
        ]
    baseline = semantic_scores(
        args.baseline_metrics, instance_ids=instance_ids
    )
    candidate = semantic_scores(
        args.candidate_metrics, instance_ids=instance_ids
    )
    if baseline is None or candidate is None:
        print("semantic metrics missing; compare skipped")
        return 0
    print("| Config | N | Findings | Precision | Recall | F1 |")
    print("|---|---:|---:|---:|---:|---:|")
    for name, scores in (("baseline", baseline), ("candidate", candidate)):
        print(
            f"| {name} | {int(scores['instances'])} | "
            f"{int(scores['generated'])} | "
            f"{scores['precision']:.3f} | "
            f"{scores['recall']:.3f} | {scores['f1']:.3f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
