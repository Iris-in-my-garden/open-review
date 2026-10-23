"""零成本确定性指标：不调用裁判模型，CI 每次都能跑。

这些指标不替代 AACR 的语义 P/R，而是回答“系统有没有基本工程问题”：

- validity: 文件/行号是否真的落在 diff 里
- evidence: 引用证据能否在 head 源码或 patch 中定位
- triage: docs-only / test-only 是否按预期跳过 LLM
- attribution: 跨维度去重后，source_aspects 是否完整
- validator: 过滤/降权原因分布（来自 pipeline metrics）
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from evaluation.repos import read_file_at_commit, repo_cache_path
from evaluation.results import result_path
from openreview.repo.diff_parser import DiffParser, ParsedChangedFile
from openreview.repo.local import collect_local_changes

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
EVIDENCE_MIN_CHARS = 6
EVIDENCE_TOKEN_OVERLAP = 0.8


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def evidence_is_grounded(evidence: str, haystack: str) -> bool:
    if not evidence or len(evidence.strip()) < EVIDENCE_MIN_CHARS or not haystack:
        return False
    evidence_tokens = set(TOKEN_PATTERN.findall(evidence.lower()))
    if len(evidence_tokens) < 3:
        return _normalize(evidence) in _normalize(haystack)
    haystack_tokens = set(TOKEN_PATTERN.findall(haystack.lower()))
    if not haystack_tokens:
        return False
    overlap = len(evidence_tokens & haystack_tokens) / len(evidence_tokens)
    return overlap >= EVIDENCE_TOKEN_OVERLAP


def _parsed_diff(
    repo_path: Optional[Path],
    base_commit: str,
    head_commit: str,
) -> Optional[Dict[str, ParsedChangedFile]]:
    if repo_path is None or not (repo_path / ".git").exists():
        return None
    try:
        changes = collect_local_changes(repo_path, base_commit, head_commit)
        parsed = DiffParser.parse_diff(
            [change.model_dump() for change in changes]
        )
    except Exception:  # noqa: BLE001 - 指标计算失败不应拖垮整批评测
        return None
    return {file.filename: file for file in parsed.files}


def _load_result(results_dir: Path, instance_id: str) -> Optional[Dict[str, Any]]:
    path = result_path(results_dir, instance_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def compute_deterministic_metrics(
    instances: List[Dict[str, Any]],
    *,
    results_dir: Path,
    repo_cache: Path,
    run_id: str = "",
    split: str = "all",
) -> Dict[str, Any]:
    results_dir = Path(results_dir)
    repo_cache = Path(repo_cache)

    counters = {
        "results_present": 0,
        "results_ok": 0,
        "results_error": 0,
        "results_missing": 0,
        "issues": 0,
        "unknown_file": 0,
        "invalid_line": 0,
        "missing_line": 0,
        "evidence_checked": 0,
        "evidence_grounded": 0,
        "evidence_missing": 0,
        "evidence_ungrounded": 0,
        "triage_skipped": 0,
        "attributed_issues": 0,
        "multi_aspect_issues": 0,
        "grounding_unavailable": 0,
    }
    triage_reasons: Counter[str] = Counter()
    aspect_counts: Counter[str] = Counter()
    validator_totals: Counter[str] = Counter()
    retrieval_coverages: List[float] = []
    retrieval_contexts = 0
    cost = {
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "duration_seconds": 0.0,
    }
    per_instance: List[Dict[str, Any]] = []

    for instance in instances:
        instance_id = instance["instance_id"]
        payload = _load_result(results_dir, instance_id)
        row: Dict[str, Any] = {
            "instance_id": instance_id,
            "split": instance.get("split"),
            "status": "missing",
            "issues": 0,
        }
        if payload is None:
            counters["results_missing"] += 1
            per_instance.append(row)
            continue

        counters["results_present"] += 1
        status = str(payload.get("status") or "ok")
        row["status"] = status
        if status != "ok":
            counters["results_error"] += 1
            row["error"] = payload.get("error")
            per_instance.append(row)
            continue

        counters["results_ok"] += 1
        review = payload.get("review") or {}
        comments = [
            comment
            for comment in (review.get("comments") or [])
            if isinstance(comment, dict)
        ]
        metrics = review.get("metrics") or {}
        row["issues"] = len(comments)
        counters["issues"] += len(comments)

        triage = metrics.get("triage") or {}
        if triage.get("skipped_llm"):
            counters["triage_skipped"] += 1
            triage_reasons[str(triage.get("reason") or "unknown")] += 1
        row["triage_skipped"] = bool(triage.get("skipped_llm"))

        validation = metrics.get("validation") or {}
        if isinstance(validation, dict):
            validator_totals.update(
                {
                    str(key): int(value)
                    for key, value in validation.items()
                    if isinstance(value, (int, float))
                }
            )

        retrieval = metrics.get("retrieval") or {}
        row["retrieval_coverage"] = float(
            retrieval.get("changed_file_coverage") or 0.0
        )
        retrieval_coverages.append(row["retrieval_coverage"])
        retrieval_contexts += int(retrieval.get("contexts") or 0)

        row["llm_calls"] = int(metrics.get("llm_calls") or 0)
        usage = metrics.get("llm_usage") or {}
        row["input_tokens"] = int(usage.get("prompt_tokens") or 0)
        row["output_tokens"] = int(usage.get("completion_tokens") or 0)
        budget = metrics.get("budget") or {}
        if row["input_tokens"] == 0:
            row["input_tokens"] = int(budget.get("used_input_tokens") or 0)
        row["duration_seconds"] = float(payload.get("duration_seconds") or 0)
        cost["llm_calls"] += row["llm_calls"]
        cost["input_tokens"] += row["input_tokens"]
        cost["output_tokens"] += row["output_tokens"]
        cost["duration_seconds"] += row["duration_seconds"]

        candidate_repo = repo_cache_path(repo_cache, instance["repo"])
        repo_path: Optional[Path] = (
            candidate_repo if (candidate_repo / ".git").exists() else None
        )
        if repo_path is None:
            counters["grounding_unavailable"] += 1
        row["grounding_available"] = repo_path is not None

        diff_files = _parsed_diff(
            repo_path, instance["base_commit"], instance["head_commit"]
        )
        head_cache: Dict[str, Optional[str]] = {}
        row_invalid = 0
        row_missing_line = 0
        row_unknown = 0
        row_grounded = 0
        row_evidence_checked = 0
        row_missing_evidence = 0

        for comment in comments:
            path = str(comment.get("path") or "")
            line = comment.get("start_line")
            evidence = comment.get("evidence")
            aspects = comment.get("source_aspects") or []

            if line is None:
                counters["missing_line"] += 1
                row_missing_line += 1
            elif diff_files is None:
                pass
            elif path not in diff_files:
                counters["unknown_file"] += 1
                row_unknown += 1
            elif int(line) not in diff_files[path].visible_lines:
                counters["invalid_line"] += 1
                row_invalid += 1

            if aspects:
                counters["attributed_issues"] += 1
                if len(aspects) > 1:
                    counters["multi_aspect_issues"] += 1
                for aspect in aspects:
                    aspect_counts[str(aspect)] += 1

            if not evidence:
                counters["evidence_missing"] += 1
                row_missing_evidence += 1
                continue
            if diff_files is None:
                continue

            file_diff = diff_files.get(path)
            if file_diff is None:
                continue
            if path not in head_cache:
                head_cache[path] = (
                    read_file_at_commit(
                        repo_path, instance["head_commit"], path
                    )
                    if repo_path is not None
                    else None
                )
            haystack = "\n".join(
                part for part in [head_cache[path] or "", file_diff.patch or ""] if part
            )
            counters["evidence_checked"] += 1
            row_evidence_checked += 1
            if evidence_is_grounded(str(evidence), haystack):
                counters["evidence_grounded"] += 1
                row_grounded += 1
            else:
                counters["evidence_ungrounded"] += 1

        attributed = sum(
            1 for comment in comments if (comment.get("source_aspects") or [])
        )
        row.update(
            {
                "invalid_line": row_invalid,
                "unknown_file": row_unknown,
                "missing_line": row_missing_line,
                "evidence_checked": row_evidence_checked,
                "evidence_grounded": row_grounded,
                "evidence_missing": row_missing_evidence,
                "attribution_coverage": _rate(attributed, len(comments)),
            }
        )
        per_instance.append(row)

    issues = counters["issues"]
    triage_base = counters["results_ok"]
    aggregate = {
        "run_id": run_id,
        "split": split,
        "instances": len(instances),
        "results_present": counters["results_present"],
        "results_ok": counters["results_ok"],
        "results_error": counters["results_error"],
        "results_missing": counters["results_missing"],
        "issues": issues,
        "validity": {
            "unknown_file_rate": _rate(counters["unknown_file"], issues),
            "invalid_line_rate": _rate(counters["invalid_line"], issues),
            "missing_line_rate": _rate(counters["missing_line"], issues),
            "unknown_file_count": counters["unknown_file"],
            "invalid_line_count": counters["invalid_line"],
            "missing_line_count": counters["missing_line"],
        },
        "evidence": {
            "checked": counters["evidence_checked"],
            "grounded": counters["evidence_grounded"],
            "ungrounded": counters["evidence_ungrounded"],
            "missing": counters["evidence_missing"],
            "coverage": _rate(
                counters["evidence_grounded"], counters["evidence_checked"]
            ),
            "missing_rate": _rate(counters["evidence_missing"], issues),
            "grounding_unavailable_instances": counters["grounding_unavailable"],
        },
        "triage": {
            "evaluated": triage_base,
            "skipped": counters["triage_skipped"],
            "skip_rate": _rate(counters["triage_skipped"], triage_base),
            "reasons": dict(triage_reasons.most_common()),
        },
        "attribution": {
            "attributed_issues": counters["attributed_issues"],
            "multi_aspect_issues": counters["multi_aspect_issues"],
            "coverage": _rate(counters["attributed_issues"], issues),
            "multi_aspect_rate": _rate(counters["multi_aspect_issues"], issues),
            "aspect_counts": dict(aspect_counts.most_common()),
        },
        "retrieval": {
            "contexts": retrieval_contexts,
            "mean_changed_file_coverage": (
                round(sum(retrieval_coverages) / len(retrieval_coverages), 3)
                if retrieval_coverages
                else 0.0
            ),
        },
        "validator": dict(validator_totals),
        "cost": {
            **cost,
            "llm_calls_avg": _rate(int(cost["llm_calls"]), triage_base),
            "duration_seconds_avg": (
                round(cost["duration_seconds"] / triage_base, 2)
                if triage_base
                else 0.0
            ),
        },
        "per_instance": per_instance,
    }
    return aggregate


def write_metrics(metrics: Dict[str, Any], metrics_dir: Path, run_id: str = "") -> Path:
    metrics_dir = Path(metrics_dir)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{run_id}" if run_id else ""
    path = metrics_dir / f"deterministic{suffix}.json"
    path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def print_summary(metrics: Dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print(f"Deterministic metrics | run={metrics.get('run_id')} split={metrics.get('split')}")
    print("=" * 60)
    print(
        f"instances: {metrics['instances']} | ok: {metrics['results_ok']} | "
        f"error: {metrics['results_error']} | missing: {metrics['results_missing']}"
    )
    print(f"issues: {metrics['issues']}")
    print(
        "validity: "
        f"invalid_line={metrics['validity']['invalid_line_rate']} "
        f"missing_line={metrics['validity']['missing_line_rate']} "
        f"unknown_file={metrics['validity']['unknown_file_rate']}"
    )
    print(
        "evidence: "
        f"coverage={metrics['evidence']['coverage']} "
        f"missing_rate={metrics['evidence']['missing_rate']} "
        f"checked={metrics['evidence']['checked']}"
    )
    print(
        f"triage: skip_rate={metrics['triage']['skip_rate']} "
        f"reasons={metrics['triage']['reasons']}"
    )
    print(
        f"attribution: coverage={metrics['attribution']['coverage']} "
        f"multi_aspect_rate={metrics['attribution']['multi_aspect_rate']} "
        f"counts={metrics['attribution']['aspect_counts']}"
    )
    print(
        f"retrieval: contexts={metrics['retrieval']['contexts']} "
        f"mean_changed_file_coverage="
        f"{metrics['retrieval']['mean_changed_file_coverage']}"
    )
    print(
        f"cost: llm_calls={metrics['cost']['llm_calls']} "
        f"input_tokens={metrics['cost']['input_tokens']} "
        f"duration={metrics['cost']['duration_seconds']}s"
    )
    print("=" * 60)
