"""合成 bug / clean-control benchmark：零成本先验证评测口径。

真实 AACR-Bench 很贵，而且 reference comment 稀疏。这个 benchmark 用一组
可解释的小 PR 回答三个问题：

1. 注入的 bug 有没有被检出（bug recall）；
2. clean / no-op 控制组会不会被强行找茬（clean false positive）；
3. 检出同样数量的 bug 时，full pipeline 是否比 single-shot baseline 更贵。

它不替代真实数据，只作为 CI 里的低成本回归和面试时可复现的 demo。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional, Sequence, Tuple

from evaluation.baseline import build_baseline_pipeline
from evaluation.cost import estimate_usd
from evaluation.results import issue_comment, safe_id
from openreview.review.report_writer import PotentialIssue, ReviewResult
from openreview.review.token_budget import estimate_tokens
from openreview.services.review_service import ReviewPipeline

LINE_TOLERANCE = 2


@dataclass(frozen=True)
class SyntheticBug:
    bug_id: str
    kind: str
    file: str
    marker: str
    description: str
    severity: str = "high"


@dataclass(frozen=True)
class SyntheticCase:
    case_id: str
    base_files: Dict[str, str]
    head_files: Dict[str, str]
    bugs: Tuple[SyntheticBug, ...] = ()
    description: str = ""
    split: str = "synthetic"

    @property
    def is_control(self) -> bool:
        return not self.bugs


@dataclass(frozen=True)
class MaterializedCase:
    case: SyntheticCase
    repo_path: Path
    base_sha: str
    head_sha: str


def _source(text: str) -> str:
    return dedent(text).lstrip("\n")


def _case(
    case_id: str,
    base_files: Dict[str, str],
    head_files: Dict[str, str],
    *,
    bugs: Sequence[SyntheticBug] = (),
    description: str = "",
) -> SyntheticCase:
    return SyntheticCase(
        case_id=case_id,
        base_files=base_files,
        head_files=head_files,
        bugs=tuple(bugs),
        description=description,
    )


DEFAULT_CASES: Tuple[SyntheticCase, ...] = (
    _case(
        "off_by_one_last_item",
        {
            "pkg/stats.py": _source(
                """
                def find_max(values):
                    if not values:
                        raise ValueError("values must not be empty")
                    best = values[0]
                    for value in values:
                        if value > best:
                            best = value
                    return best
                """
            )
        },
        {
            "pkg/stats.py": _source(
                """
                def find_max(values):
                    if not values:
                        raise ValueError("values must not be empty")
                    best = values[0]
                    for value in values[:-1]:
                        if value > best:
                            best = value
                    return best
                """
            )
        },
        bugs=[
            SyntheticBug(
                bug_id="off_by_one_last_item",
                kind="off_by_one",
                file="pkg/stats.py",
                marker="for value in values[:-1]:",
                description=(
                    "values[:-1] silently ignores the last item, so find_max "
                    "can return a non-maximum value."
                ),
            )
        ],
        description="Off-by-one in find_max: the last element is skipped.",
    ),
    _case(
        "missing_none_guard",
        {
            "pkg/users.py": _source(
                """
                def display_name(user):
                    if user is None:
                        return "anonymous"
                    return user.name
                """
            )
        },
        {
            "pkg/users.py": _source(
                """
                def display_name(user):
                    return user.name
                """
            )
        },
        bugs=[
            SyntheticBug(
                bug_id="missing_none_guard",
                kind="null_dereference",
                file="pkg/users.py",
                marker="return user.name",
                description="display_name no longer handles user=None.",
            )
        ],
        description="Removed the None guard before dereferencing user.name.",
    ),
    _case(
        "sql_injection",
        {
            "pkg/db.py": _source(
                """
                def find_user(connection, username):
                    query = "SELECT id, name FROM users WHERE username = ?"
                    return connection.execute(query, (username,)).fetchone()
                """
            )
        },
        {
            "pkg/db.py": _source(
                """
                def find_user(connection, username):
                    query = f"SELECT id, name FROM users WHERE username = '{username}'"
                    return connection.execute(query).fetchone()
                """
            )
        },
        bugs=[
            SyntheticBug(
                bug_id="sql_injection",
                kind="sql_injection",
                file="pkg/db.py",
                marker="query = f\"SELECT id, name FROM users WHERE username = '{username}'\"",
                description="User input is interpolated directly into SQL.",
                severity="critical",
            )
        ],
        description="SQL injection introduced by f-string query construction.",
    ),
    _case(
        "file_handle_not_closed",
        {
            "pkg/config.py": _source(
                """
                def read_config(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        return handle.read()
                """
            )
        },
        {
            "pkg/config.py": _source(
                """
                def read_config(path):
                    handle = open(path, "r", encoding="utf-8")
                    return handle.read()
                """
            )
        },
        bugs=[
            SyntheticBug(
                bug_id="file_handle_not_closed",
                kind="resource_leak",
                file="pkg/config.py",
                marker='handle = open(path, "r", encoding="utf-8")',
                description="The file handle is no longer closed deterministically.",
            )
        ],
        description="A with-block was replaced by a bare open().",
    ),
    _case(
        "division_by_zero",
        {
            "pkg/math_utils.py": _source(
                """
                def average(values):
                    if not values:
                        return 0.0
                    return sum(values) / len(values)
                """
            )
        },
        {
            "pkg/math_utils.py": _source(
                """
                def average(values):
                    return sum(values) / len(values)
                """
            )
        },
        bugs=[
            SyntheticBug(
                bug_id="division_by_zero",
                kind="unchecked_division",
                file="pkg/math_utils.py",
                marker="return sum(values) / len(values)",
                description="average([]) now raises ZeroDivisionError.",
            )
        ],
        description="Removed the empty-list guard before division.",
    ),
    _case(
        "hardcoded_secret",
        {
            "pkg/settings.py": _source(
                """
                import os

                API_KEY = os.environ["API_KEY"]
                """
            )
        },
        {
            "pkg/settings.py": _source(
                """
                API_KEY = "sk-live-1234567890abcdef"
                """
            )
        },
        bugs=[
            SyntheticBug(
                bug_id="hardcoded_secret",
                kind="hardcoded_secret",
                file="pkg/settings.py",
                marker='API_KEY = "sk-live-1234567890abcdef"',
                description="A live-looking API key is committed to the source.",
                severity="critical",
            )
        ],
        description="Environment-based secret replaced with a hardcoded key.",
    ),
    _case(
        "clean_local_rename",
        {
            "pkg/calculator.py": _source(
                """
                def add(a, b):
                    result = a + b
                    return result
                """
            )
        },
        {
            "pkg/calculator.py": _source(
                """
                def add(a, b):
                    total = a + b
                    return total
                """
            )
        },
        description="Benign local rename; should produce no findings.",
    ),
    _case(
        "clean_guarded_division",
        {
            "pkg/safe_math.py": _source(
                """
                def safe_average(values):
                    return sum(values) / len(values) if values else 0.0
                """
            )
        },
        {
            "pkg/safe_math.py": _source(
                """
                def safe_average(values):
                    total = sum(values)
                    return total / len(values) if values else 0.0
                """
            )
        },
        description="Guarded division looks suspicious but handles the empty list.",
    ),
    _case(
        "clean_docstring",
        {
            "pkg/text.py": _source(
                """
                def normalize(value):
                    # Collapse whitespace.
                    return " ".join(value.split())
                """
            )
        },
        {
            "pkg/text.py": _source(
                """
                def normalize(value):
                    \"\"\"Collapse runs of whitespace into a single space.\"\"\"
                    return " ".join(value.split())
                """
            )
        },
        description="Comment-only change; should produce no findings.",
    ),
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


def _write_snapshot(repo: Path, files: Dict[str, str]) -> None:
    wanted = set(files)
    for path in repo.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        relative = str(path.relative_to(repo))
        if relative not in wanted:
            path.unlink()

    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def materialize_case(case: SyntheticCase, root: Path) -> MaterializedCase:
    """把一个 case 落地成 base/head 两个 commit，供 run_local 复用。"""
    repo = Path(root) / case.case_id
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", str(repo)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    _git(repo, "config", "user.email", "synthetic@openreview.local")
    _git(repo, "config", "user.name", "OpenReview Synthetic Benchmark")

    _write_snapshot(repo, case.base_files)
    base_sha = _commit(repo, "base")
    _write_snapshot(repo, case.head_files)
    head_sha = _commit(repo, "head")
    return MaterializedCase(
        case=case,
        repo_path=repo,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def bug_line(case: SyntheticCase, bug: SyntheticBug) -> Optional[int]:
    content = case.head_files.get(bug.file)
    if content is None:
        return None
    for line_number, line in enumerate(content.splitlines(), start=1):
        if bug.marker in line:
            return line_number
    return None


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def finding_matches_bug(
    finding: PotentialIssue,
    bug: SyntheticBug,
    case: SyntheticCase,
    tolerance: int = LINE_TOLERANCE,
) -> bool:
    if _normalize_path(finding.file) != _normalize_path(bug.file):
        return False
    expected_line = bug_line(case, bug)
    if expected_line is None or finding.line is None:
        return False
    return abs(int(finding.line) - expected_line) <= tolerance


def evaluate_case(
    case: SyntheticCase,
    result: ReviewResult,
    *,
    duration_seconds: float = 0.0,
) -> Dict[str, Any]:
    """把一次 review 结果转成可聚合的 case row。"""
    findings = list(result.potential_issues)
    matched_indexes: set[int] = set()
    detected: List[str] = []

    for bug in case.bugs:
        for index, finding in enumerate(findings):
            if index in matched_indexes:
                continue
            if finding_matches_bug(finding, bug, case):
                matched_indexes.add(index)
                detected.append(bug.bug_id)
                break

    false_positive_count = len(findings) - len(matched_indexes)
    recall = len(detected) / len(case.bugs) if case.bugs else None
    precision = (
        len(matched_indexes) / len(findings)
        if findings
        else (None if case.bugs else 1.0)
    )
    usage = result.metrics.get("llm_usage") or {}
    return {
        "case_id": case.case_id,
        "split": case.split,
        "description": case.description,
        "is_control": case.is_control,
        "status": "ok",
        "bugs": [
            {
                "bug_id": bug.bug_id,
                "kind": bug.kind,
                "file": bug.file,
                "line": bug_line(case, bug),
                "severity": bug.severity,
                "description": bug.description,
            }
            for bug in case.bugs
        ],
        "detected_bug_ids": detected,
        "missed_bug_ids": [
            bug.bug_id for bug in case.bugs if bug.bug_id not in detected
        ],
        "findings": [issue_comment(issue) for issue in findings],
        "findings_count": len(findings),
        "false_positive_findings": false_positive_count,
        "recall": None if recall is None else round(recall, 3),
        "precision": None if precision is None else round(precision, 3),
        "llm_calls": int(result.metrics.get("llm_calls") or usage.get("calls") or 0),
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "duration_seconds": round(duration_seconds, 2),
    }


def _aggregate_rows(
    rows: List[Dict[str, Any]],
    *,
    mode: str,
    input_price: float,
    output_price: float,
    cases_requested: int,
) -> Dict[str, Any]:
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    bug_rows = [row for row in ok_rows if not row.get("is_control")]
    control_rows = [row for row in ok_rows if row.get("is_control")]

    bugs_total = sum(len(row.get("bugs") or []) for row in bug_rows)
    bugs_detected = sum(len(row.get("detected_bug_ids") or []) for row in bug_rows)
    findings_total = sum(int(row.get("findings_count") or 0) for row in ok_rows)
    matched_total = sum(
        int(row.get("findings_count") or 0)
        - int(row.get("false_positive_findings") or 0)
        for row in ok_rows
    )
    control_with_findings = sum(
        1 for row in control_rows if int(row.get("findings_count") or 0) > 0
    )
    input_tokens = sum(int(row.get("input_tokens") or 0) for row in ok_rows)
    output_tokens = sum(int(row.get("output_tokens") or 0) for row in ok_rows)
    llm_calls = sum(int(row.get("llm_calls") or 0) for row in ok_rows)
    duration = sum(float(row.get("duration_seconds") or 0) for row in ok_rows)
    estimated_usd = estimate_usd(
        input_tokens, output_tokens, input_price, output_price
    )

    def _rate(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator, 3) if denominator else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "cases_requested": cases_requested,
        "cases": len(rows),
        "cases_ok": len(ok_rows),
        "cases_error": len(rows) - len(ok_rows),
        "bug_cases": len(bug_rows),
        "control_cases": len(control_rows),
        "bugs_total": bugs_total,
        "bugs_detected": bugs_detected,
        "bug_recall": _rate(bugs_detected, bugs_total),
        "findings_total": findings_total,
        "matched_findings": matched_total,
        "false_positive_findings": findings_total - matched_total,
        "finding_precision": _rate(matched_total, findings_total),
        "control_cases_with_findings": control_with_findings,
        "clean_case_false_positive_rate": _rate(
            control_with_findings, len(control_rows)
        ),
        "cost": {
            "llm_calls": llm_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_seconds": round(duration, 2),
            "estimated_usd": estimated_usd,
            "input_price_per_million": input_price,
            "output_price_per_million": output_price,
        },
        "per_case": rows,
    }


def estimate_synthetic_run(
    cases: Sequence[SyntheticCase],
    *,
    mode: str,
    input_price: float,
    output_price: float,
) -> Dict[str, Any]:
    """不调用 LLM 的保守 preflight，用于 --estimate-only / --budget-usd。"""
    if mode not in {"full", "baseline"}:
        raise ValueError(f"unknown mode: {mode}")

    per_case_prompt = []
    for case in cases:
        payload = json.dumps(
            {"base": case.base_files, "head": case.head_files},
            ensure_ascii=False,
        )
        per_case_prompt.append(estimate_tokens(payload) + 1200)

    count = len(cases)
    if mode == "baseline":
        calls = count
        prompt_tokens = sum(per_case_prompt)
        completion_tokens = count * 600
        assumption = "1 次全量 review / case"
    else:
        calls = count * 5
        # 4 个 aspect 各发送一次 patch/context；verifier 只发送候选问题。
        prompt_tokens = sum(per_case_prompt) * 4 + count * 800
        completion_tokens = count * 1600
        assumption = "4 个 aspect + 1 次 verifier（上界）/ case"

    return {
        "mode": mode,
        "cases": count,
        "llm_calls": calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_usd": estimate_usd(
            prompt_tokens, completion_tokens, input_price, output_price
        ),
        "assumption": assumption,
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_synthetic_benchmark(
    cases: Sequence[SyntheticCase],
    *,
    mode: str,
    results_dir: Path,
    work_dir: Path,
    mock_llm: bool = False,
    input_price: float = 0.0,
    output_price: float = 0.0,
    limit: Optional[int] = None,
    resume: bool = False,
    keep_worktrees: bool = False,
    budget_usd: Optional[float] = None,
    hybrid_retrieval_enabled: bool = False,
    verifier_enabled: bool = True,
    fail_fast: bool = False,
) -> Dict[str, Any]:
    if mode not in {"full", "baseline"}:
        raise ValueError(f"unknown mode: {mode}")

    selected = list(cases[:limit] if limit is not None else cases)
    results_dir = Path(results_dir)
    work_dir = Path(work_dir) / mode
    results_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    pipeline_kwargs: Dict[str, Any] = {
        "mock_llm": mock_llm,
        "hybrid_retrieval_enabled": hybrid_retrieval_enabled,
        "review_cache_enabled": False,
    }
    if mode == "full":
        pipeline_kwargs["verifier_enabled"] = verifier_enabled
    pipeline = (
        build_baseline_pipeline(**pipeline_kwargs)
        if mode == "baseline"
        else ReviewPipeline(**pipeline_kwargs)
    )

    rows: List[Dict[str, Any]] = []
    resumed = 0
    stopped_by_budget = False
    cumulative_usd = 0.0

    try:
        for index, case in enumerate(selected, start=1):
            result_path = results_dir / f"{safe_id(case.case_id)}.json"
            if resume and result_path.exists():
                try:
                    existing = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = None
                if isinstance(existing, dict) and existing.get("status") == "ok":
                    existing["resumed"] = True
                    rows.append(existing)
                    resumed += 1
                    print(f"[{mode}] [{index}/{len(selected)}] {case.case_id} (resume)")
                    continue

            kind = "control" if case.is_control else f"{len(case.bugs)} bug(s)"
            print(f"[{mode}] [{index}/{len(selected)}] {case.case_id} ({kind})")
            started = time.monotonic()
            try:
                materialized = materialize_case(case, work_dir)
                pipeline.run_local(
                    materialized.repo_path,
                    materialized.base_sha,
                    materialized.head_sha,
                    results_dir / "reports" / mode / safe_id(case.case_id),
                    owner="synthetic",
                    repo=case.case_id,
                    pr_number=index,
                    title=case.description,
                    keep_worktrees=keep_worktrees,
                )
                if pipeline.last_result is None:
                    raise RuntimeError("run_local completed without last_result")
                row = evaluate_case(
                    case,
                    pipeline.last_result,
                    duration_seconds=time.monotonic() - started,
                )
            except Exception as error:  # noqa: BLE001 - 单 case 失败不应中断整批
                row = {
                    "case_id": case.case_id,
                    "split": case.split,
                    "description": case.description,
                    "is_control": case.is_control,
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}",
                    "bugs": [
                        {
                            "bug_id": bug.bug_id,
                            "kind": bug.kind,
                            "file": bug.file,
                            "line": bug_line(case, bug),
                        }
                        for bug in case.bugs
                    ],
                    "findings": [],
                    "findings_count": 0,
                    "duration_seconds": round(time.monotonic() - started, 2),
                }
                if fail_fast:
                    raise
                print(f"  ! {row['error']}")

            row["mode"] = mode
            row["resumed"] = False
            _write_json(result_path, row)
            row["result_path"] = str(result_path)
            rows.append(row)

            cumulative_usd += estimate_usd(
                int(row.get("input_tokens") or 0),
                int(row.get("output_tokens") or 0),
                input_price,
                output_price,
            )
            if budget_usd is not None and cumulative_usd > budget_usd:
                print(
                    f"[budget] estimated ${cumulative_usd:.4f} exceeds "
                    f"--budget-usd {budget_usd:.4f}; stopping before remaining cases."
                )
                stopped_by_budget = True
                break
    finally:
        pipeline.close()

    summary = _aggregate_rows(
        rows,
        mode=mode,
        input_price=input_price,
        output_price=output_price,
        cases_requested=len(selected),
    )
    summary["resumed"] = resumed
    summary["stopped_by_budget"] = stopped_by_budget
    summary["results_dir"] = str(results_dir)
    summary["work_dir"] = str(work_dir)
    _write_json(results_dir / f"summary_{mode}.json", summary)
    print_synthetic_summary(summary)
    return summary


def print_synthetic_summary(summary: Dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print(
        f"Synthetic benchmark | mode={summary['mode']} "
        f"cases={summary['cases_ok']}/{summary['cases_requested']}"
    )
    print("=" * 60)
    print(
        f"bug recall: {summary['bug_recall']} "
        f"({summary['bugs_detected']}/{summary['bugs_total']})"
    )
    print(
        f"finding precision: {summary['finding_precision']} "
        f"(false positives: {summary['false_positive_findings']})"
    )
    print(
        "clean-case false positive rate: "
        f"{summary['clean_case_false_positive_rate']} "
        f"({summary['control_cases_with_findings']}/{summary['control_cases']})"
    )
    cost = summary["cost"]
    print(
        f"cost: llm_calls={cost['llm_calls']} "
        f"input_tokens={cost['input_tokens']} "
        f"output_tokens={cost['output_tokens']} "
        f"estimated_usd={cost['estimated_usd']} "
        f"duration={cost['duration_seconds']}s"
    )
    print("=" * 60)


__all__ = [
    "DEFAULT_CASES",
    "LINE_TOLERANCE",
    "MaterializedCase",
    "SyntheticBug",
    "SyntheticCase",
    "bug_line",
    "estimate_synthetic_run",
    "evaluate_case",
    "finding_matches_bug",
    "materialize_case",
    "print_synthetic_summary",
    "run_synthetic_benchmark",
]
