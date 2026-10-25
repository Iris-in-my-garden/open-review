"""Phase 1 检索质量指标（完全零成本，不调用 LLM）。

这里回答四个面试里最容易被追问的问题：

1. 检索有没有覆盖到 reference comment 指向的文件？ -> file recall
2. 变更符号有没有被检索回来？ -> symbol coverage
3. finding 有没有真的引用注入的 context？ -> citation rate
4. 上下文有没有重复/膨胀？ -> duplicate rate / avg contexts

这些指标只读已经落盘的结果 JSON，不重新跑 review，也不依赖 AACR judge。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from evaluation.results import result_path


def _reference_paths(comments: Sequence[Any]) -> set[str]:
    paths: set[str] = set()
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        path = (
            comment.get("path")
            or comment.get("file")
            or comment.get("filename")
            or comment.get("file_path")
        )
        if path:
            paths.add(str(path))
    return paths


def _rate(numerator: float, denominator: float) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def compute_retrieval_metrics(
    instances: Sequence[Dict[str, Any]],
    results_dir: Path,
    run_id: str = "",
    split: str = "all",
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    reference_paths_total = 0
    reference_paths_matched = 0
    index_paths_matched = 0
    findings_total = 0
    findings_with_refs = 0
    context_refs_total = 0
    contexts_total = 0
    duplicate_contexts_total = 0
    patch_checked = 0
    patch_valid = 0
    patch_invalid = 0
    context_kinds: Dict[str, int] = {}
    retrieval_stats: Dict[str, float] = {}
    symbol_coverages: List[float] = []

    for instance in instances:
        instance_id = str(instance.get("instance_id") or "")
        if not instance_id:
            continue
        path = result_path(results_dir, instance_id)
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("status") not in (None, "ok"):
            continue

        review = payload.get("review") or {}
        metrics = (review.get("metrics") or {}).get("retrieval") or {}
        comments = review.get("comments") or []
        if not isinstance(comments, list):
            comments = []

        ref_paths = _reference_paths(instance.get("comments") or [])
        context_files = {
            str(item) for item in (metrics.get("context_file_list") or []) if item
        }
        index_files = {
            str(item)
            for item in (metrics.get("index_context_file_list") or [])
            if item
        }
        matched_refs = ref_paths & context_files
        matched_index_refs = ref_paths & index_files

        findings = len(comments)
        cited = sum(
            1
            for comment in comments
            if isinstance(comment, dict) and comment.get("context_refs")
        )
        refs = sum(
            len(comment.get("context_refs") or [])
            for comment in comments
            if isinstance(comment, dict)
        )
        contexts = int(metrics.get("contexts") or 0)
        duplicates = int(metrics.get("duplicate_contexts") or 0)
        checkable_patches = [
            comment
            for comment in comments
            if isinstance(comment, dict)
            and comment.get("suggested_patch_valid") is not None
        ]
        valid_patches = sum(
            1 for comment in checkable_patches if comment.get("suggested_patch_valid")
        )
        invalid_patches = len(checkable_patches) - valid_patches

        symbol_coverage = float(metrics.get("symbol_coverage") or 0.0)
        if metrics.get("changed_symbols"):
            symbol_coverages.append(symbol_coverage)

        kinds = metrics.get("context_kinds") or {}
        if isinstance(kinds, dict):
            for key, value in kinds.items():
                context_kinds[str(key)] = context_kinds.get(str(key), 0) + int(value)
        stats = metrics.get("stats") or {}
        if isinstance(stats, dict):
            for key, value in stats.items():
                if isinstance(value, (int, float)):
                    retrieval_stats[key] = retrieval_stats.get(key, 0) + float(value)

        rows.append(
            {
                "instance_id": instance_id,
                "reference_files": len(ref_paths),
                "file_recall": _rate(len(matched_refs), len(ref_paths)),
                "index_file_recall": _rate(
                    len(matched_index_refs), len(ref_paths)
                ),
                "findings": findings,
                "cited_findings": cited,
                "citation_rate": _rate(cited, findings),
                "contexts": contexts,
                "duplicate_contexts": duplicates,
                "symbol_coverage": symbol_coverage,
                "patch_checked": len(checkable_patches),
                "patch_valid": valid_patches,
                "patch_invalid": invalid_patches,
            }
        )

        reference_paths_total += len(ref_paths)
        reference_paths_matched += len(matched_refs)
        index_paths_matched += len(matched_index_refs)
        findings_total += findings
        findings_with_refs += cited
        context_refs_total += refs
        contexts_total += contexts
        duplicate_contexts_total += duplicates
        patch_checked += len(checkable_patches)
        patch_valid += valid_patches
        patch_invalid += invalid_patches

    return {
        "run_id": run_id,
        "split": split,
        "instances": len(rows),
        "retrieval": {
            "file_recall": _rate(reference_paths_matched, reference_paths_total),
            "index_file_recall": _rate(index_paths_matched, reference_paths_total),
            "symbol_coverage": _mean(symbol_coverages),
            "citation_rate": _rate(findings_with_refs, findings_total),
            "context_refs_total": context_refs_total,
            "contexts_total": contexts_total,
            "avg_contexts": _rate(contexts_total, len(rows)),
            "duplicate_context_rate": _rate(
                duplicate_contexts_total, contexts_total
            ),
            "context_kinds": context_kinds,
            "stats": {
                key: round(value, 3) for key, value in sorted(retrieval_stats.items())
            },
        },
        "patch_validation": {
            "checked": patch_checked,
            "valid": patch_valid,
            "invalid": patch_invalid,
            "valid_rate": _rate(patch_valid, patch_checked),
        },
        "per_instance": rows,
    }


def write_retrieval_metrics(
    metrics: Dict[str, Any], metrics_dir: Path, run_id: str = ""
) -> Path:
    metrics_dir = Path(metrics_dir)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{run_id}" if run_id else ""
    path = metrics_dir / f"retrieval{suffix}.json"
    path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def print_retrieval_summary(metrics: Dict[str, Any]) -> None:
    retrieval = metrics.get("retrieval") or {}
    patch = metrics.get("patch_validation") or {}
    print("\n" + "=" * 60)
    print(
        f"Retrieval metrics | run={metrics.get('run_id')} "
        f"split={metrics.get('split')} instances={metrics.get('instances')}"
    )
    print("=" * 60)
    print(f"- file recall:        {_fmt(retrieval.get('file_recall'))}")
    print(f"- index file recall:  {_fmt(retrieval.get('index_file_recall'))}")
    print(f"- symbol coverage:    {_fmt(retrieval.get('symbol_coverage'))}")
    print(f"- citation rate:      {_fmt(retrieval.get('citation_rate'))}")
    print(f"- avg contexts:       {retrieval.get('avg_contexts', 0)}")
    print(
        f"- duplicate contexts: {_fmt(retrieval.get('duplicate_context_rate'))}"
    )
    print(
        f"- patch valid:        {_fmt(patch.get('valid_rate'))} "
        f"({patch.get('valid', 0)}/{patch.get('checked', 0)})"
    )
    print("=" * 60 + "\n")


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)
