"""把 ``ReviewResult`` 转成 AACR 统一评测框架能消费的结果 JSON。

AACR 的 OCR 结果格式是::

    {
      "review": {
        "comments": [{"path": ..., "start_line": ..., "end_line": ..., "content": ...}],
        "summary": {"input_tokens": ..., "output_tokens": ...}
      },
      "duration_seconds": ...
    }

我们额外保留 severity / category / confidence / source_aspects / evidence，
方便做零成本归因分析；AACR 的评测器会忽略不认识的字段。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from openreview.review.report_writer import PotentialIssue, ReviewResult

REVIEWER_NAME = "openreview"


def safe_id(instance_id: str) -> str:
    return instance_id.replace("/", "__")


def result_path(results_dir: Path, instance_id: str) -> Path:
    return Path(results_dir) / f"{safe_id(instance_id)}.json"


def issue_comment(issue: PotentialIssue) -> Dict[str, Any]:
    """一条 finding -> AACR comment，同时保留评测需要的内部字段。"""
    note_parts = [issue.reasoning.strip()]
    suggestion = issue.suggestion.strip()
    if suggestion:
        note_parts.append(f"Suggestion: {suggestion}")
    content = "\n\n".join(part for part in note_parts if part)

    return {
        "path": issue.file,
        "start_line": issue.line,
        "end_line": issue.end_line if issue.end_line is not None else issue.line,
        "content": content,
        # 以下字段 AACR 不读取，仅用于本地确定性指标 / 人工分析。
        "severity": issue.severity,
        "category": issue.category,
        "confidence": issue.confidence,
        "source_aspects": list(issue.source_aspects),
        "evidence": issue.evidence,
        "suggested_patch": issue.suggested_patch,
    }


def _token_counts(result: ReviewResult) -> tuple[int, int]:
    usage = result.metrics.get("llm_usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    if input_tokens == 0:
        budget = result.metrics.get("budget") or {}
        input_tokens = int(budget.get("used_input_tokens") or 0)
    return input_tokens, output_tokens


def build_result_payload(
    instance: Dict[str, Any],
    result: ReviewResult,
    *,
    duration_seconds: float,
    started_at: Optional[str] = None,
    status: str = "ok",
    error: Optional[str] = None,
) -> Dict[str, Any]:
    input_tokens, output_tokens = _token_counts(result)
    comments = [issue_comment(issue) for issue in result.potential_issues]
    review = {
        "comments": comments,
        # AACR 的 OCR usage 解析路径读取这两个字段。
        "summary": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "llm_calls": int(result.metrics.get("llm_calls") or 0),
        },
        "pr_summary": result.summary,
        "risk_level": result.risk_level,
        "risk_reason": result.risk_reason,
        "final_decision": result.final_decision,
        "final_summary": result.final_summary,
        "review_notes": list(result.review_notes),
        "metrics": result.metrics,
    }
    payload: Dict[str, Any] = {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "base_commit": instance["base_commit"],
        "head_commit": instance["head_commit"],
        "reviewer": REVIEWER_NAME,
        "status": status,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration_seconds, 2),
        "review": review,
        "token_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
    if error:
        payload["error"] = error
    return payload


def write_result(results_dir: Path, payload: Dict[str, Any]) -> Path:
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    path = result_path(results_dir, payload["instance_id"])
    tmp = path.with_suffix(".json.part")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def run_reviews(
    instances: List[Dict[str, Any]],
    *,
    repo_cache: Path,
    results_dir: Path,
    pipeline: Any,
    keep_worktrees: bool = False,
    fail_fast: bool = False,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """逐条调用 ``pipeline.run_local`` 并把结果落盘。

    这里刻意串行：同一 repo 的多条样本要反复创建 worktree，并发收益有限，
    但更容易把机器和临时目录打爆。需要并发时可以按 repo 分组后外层并行。
    """
    from evaluation.repos import ensure_repo

    rows: List[Dict[str, Any]] = []
    selected = instances[:limit] if limit is not None else instances
    for index, instance in enumerate(selected, start=1):
        instance_id = instance["instance_id"]
        repo = instance["repo"]
        owner, repo_name = repo.split("/", 1)
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        print(f"[{index}/{len(selected)}] {instance_id} ({instance['split']})")

        try:
            repo_path = ensure_repo(
                repo_cache,
                repo,
                instance.get("clone_url") or f"https://github.com/{repo}.git",
                instance["base_commit"],
                instance["head_commit"],
            )
            pipeline.run_local(
                repo_path,
                instance["base_commit"],
                instance["head_commit"],
                Path(results_dir) / "reports" / safe_id(instance_id),
                owner=owner,
                repo=repo_name,
                pr_number=int(instance.get("pr_number") or 0),
                title=f"AACR {instance_id}",
                pr_url=instance.get("github_pr_url"),
                keep_worktrees=keep_worktrees,
            )
            if pipeline.last_result is None:
                raise RuntimeError("run_local 结束但没有 last_result")
            payload = build_result_payload(
                instance,
                pipeline.last_result,
                duration_seconds=time.monotonic() - started,
                started_at=started_at,
            )
        except Exception as error:  # noqa: BLE001 - 单条失败不应中断整个评测集
            payload = {
                "instance_id": instance_id,
                "repo": repo,
                "base_commit": instance["base_commit"],
                "head_commit": instance["head_commit"],
                "reviewer": REVIEWER_NAME,
                "status": "error",
                "started_at": started_at,
                "duration_seconds": round(time.monotonic() - started, 2),
                "error": f"{type(error).__name__}: {error}",
                "review": {"comments": [], "summary": {}, "metrics": {}},
            }
            if fail_fast:
                raise
            print(f"  ! {payload['error']}")

        out_path = write_result(results_dir, payload)
        rows.append(
            {
                "instance_id": instance_id,
                "split": instance["split"],
                "status": payload["status"],
                "duration_seconds": payload["duration_seconds"],
                "issues": len(payload["review"].get("comments") or []),
                "result_path": str(out_path),
                "error": payload.get("error"),
            }
        )

    summary_path = Path(results_dir) / f"summary_{REVIEWER_NAME}.json"
    summary_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    succeeded = sum(1 for row in rows if row["status"] == "ok")
    print(f"[done] {succeeded}/{len(rows)} ok; summary -> {summary_path}")
    return rows
