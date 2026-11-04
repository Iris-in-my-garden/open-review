"""OpenReview 离线评测 CLI。

示例（在项目根目录运行）::

    # 1) 生成固定 manifest（只需一次）
    python -m evaluation.cli prepare --source /path/to/positive_samples.json

    # 2) 在 dev 子集上跑 mock review，验证链路
    python -m evaluation.cli review --split dev --run-id dev-mock --mock-llm

    # 3) 确定性指标（不调用 LLM）
    python -m evaluation.cli metrics --split dev --run-id dev-mock

    # 4) 可选：用 AACR 官方 judge 算语义 P/R（需要 AACR checkout + JUDGE_*）
    python -m evaluation.cli semantic --split dev --run-id dev \
        --aacr-eval-dir /path/to/aacr-bench/evaluation

    # 5) 汇总多次 run 为 Markdown 表格
    python -m evaluation.cli report \
        --run single=evaluation/metrics/aacr-pygo-v1/dev-single \
        --run multi_aspect=evaluation/metrics/aacr-pygo-v1/dev-multi
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from evaluation.aacr import (
    DEFAULT_SEED,
    ManifestError,
    build_manifest,
    ensure_source,
    instances_for_split,
    load_manifest,
    load_records,
    manifest_summary,
    sha256_file,
    write_manifest,
)
from evaluation.deterministic import (
    compute_deterministic_metrics,
    write_metrics,
)
from evaluation.deterministic import (
    print_summary as print_deterministic_summary,
)
from evaluation.retrieval import (
    compute_retrieval_metrics,
    print_retrieval_summary,
    write_retrieval_metrics,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = PROJECT_ROOT / "evaluation"
DEFAULT_MANIFEST = EVAL_ROOT / "manifests" / "aacr_pygo_v1.json"
DEFAULT_SOURCE = EVAL_ROOT / ".cache" / "aacr" / "positive_samples.json"
DEFAULT_REPO_CACHE = EVAL_ROOT / ".cache" / "repos"
DEFAULT_RESULTS_ROOT = EVAL_ROOT / "results"
DEFAULT_METRICS_ROOT = EVAL_ROOT / "metrics"


def _default_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _resolve_run_dir(root: Path, version: str, run_id: str) -> Path:
    return root / version / run_id


def _load_dataset(args: argparse.Namespace):
    manifest = load_manifest(args.manifest)
    source = ensure_source(
        args.source, verify=getattr(args, "verify_source", True)
    )
    records = load_records(source)
    instances = instances_for_split(manifest, records, args.split)
    if args.limit is not None:
        instances = instances[: args.limit]
    return manifest, instances


def _restrict_to_actual_run(
    instances: list[dict], results_dir: Path
) -> list[dict]:
    """metrics/semantic 默认只评估本次实际跑过的实例。

    否则 ``review --limit 1`` 之后跑 ``metrics`` 会把另外 9 条算成 missing，
    指标看起来像大面积失败。summary 是 review 阶段写出的权威实例清单。
    """
    summary_path = Path(results_dir) / "summary_openreview.json"
    if not summary_path.exists():
        return instances
    try:
        rows = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return instances
    if not isinstance(rows, list):
        return instances
    actual_ids = {
        str(row.get("instance_id"))
        for row in rows
        if isinstance(row, dict) and row.get("instance_id")
    }
    if not actual_ids:
        return instances
    selected = [
        instance
        for instance in instances
        if instance["instance_id"] in actual_ids
    ]
    if not selected:
        return instances
    if len(selected) != len(instances):
        print(
            f"[metrics] 按 review summary 评估 {len(selected)} 个实例"
            f"（split 中还有 {len(instances) - len(selected)} 个未运行）"
        )
    return selected


def _estimate_aacr_review(
    instances: list[dict],
    *,
    verifier: bool = True,
    reviewer_mode: str = "single",
) -> dict:
    """AACR review 的保守 token preflight。

    不 clone 仓库，只看 manifest 里的 change_line_count / reference_comments。
    真实 prompt 还受上下文检索和 token 预算影响，因此这里明确标注为估算。
    """
    calls_per_review = 4 if reviewer_mode == "multi_aspect" else 1
    prompt_tokens = 0
    completion_tokens = 0
    calls = 0
    for instance in instances:
        lines = max(1, int(instance.get("change_line_count") or 0))
        per_review_prompt = 5000 + lines * 12
        prompt_tokens += per_review_prompt * calls_per_review
        completion_tokens += 450 * calls_per_review
        calls += calls_per_review
        if verifier:
            prompt_tokens += 1500 + int(instance.get("reference_comments") or 1) * 250
            completion_tokens += 300
            calls += 1
    mode_label = "4 aspects" if reviewer_mode == "multi_aspect" else "single-pass"
    return {
        "instances": len(instances),
        "llm_calls": calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "assumption": (
            f"{mode_label} + verifier; ~12 tokens/change line + base context"
        ),
    }


def cmd_prepare(args: argparse.Namespace) -> int:
    source = ensure_source(
        args.source,
        download=args.download,
        cache_dir=DEFAULT_SOURCE.parent,
        verify=args.verify_source,
    )
    records = load_records(source)
    manifest = build_manifest(
        records,
        seed=args.seed,
        source_sha256=sha256_file(source),
        source_name="AACR-Bench" if args.verify_source else "AACR-Bench (custom)",
    )
    path = write_manifest(manifest, args.out)
    print(f"manifest -> {path}")
    print(manifest_summary(manifest))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from evaluation.results import run_reviews
    from openreview.services.review_service import ReviewPipeline

    manifest, instances = _load_dataset(args)
    run_id = args.run_id or _default_run_id()
    results_dir = args.results_dir or _resolve_run_dir(
        DEFAULT_RESULTS_ROOT, manifest["version"], run_id
    )

    if args.estimate_only or args.budget_tokens is not None:
        estimate = _estimate_aacr_review(
            instances,
            verifier=not args.no_verifier,
            reviewer_mode=args.reviewer_mode,
        )
        print(
            "[estimate] instances={instances} calls={llm_calls} "
            "prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} "
            "total_tokens={total_tokens}".format(**estimate)
        )
        print(f"[estimate] assumption: {estimate['assumption']}")
        if args.estimate_only:
            return 0
        if estimate["total_tokens"] > args.budget_tokens:
            print(
                "error: estimated {} tokens exceeds --budget-tokens {}; "
                "abort before any LLM call.".format(
                    estimate["total_tokens"], args.budget_tokens
                ),
                file=sys.stderr,
            )
            return 2

    review_cache_dir = args.review_cache_dir
    if args.review_cache and not review_cache_dir:
        review_cache_dir = EVAL_ROOT / ".cache" / "review"
    pipeline = ReviewPipeline(
        mock_llm=args.mock_llm,
        llm_model=args.model,
        llm_base_url=args.base_url,
        reviewer_mode=args.reviewer_mode,
        max_workers=args.max_workers,
        triage_enabled=not args.no_triage,
        impact_enabled=not args.no_impact,
        verifier_enabled=not args.no_verifier,
        hybrid_retrieval_enabled=not args.no_hybrid_retrieval,
        graph_retrieval_enabled=not args.no_graph_retrieval,
        budget_calls=args.budget_calls,
        review_cache_enabled=bool(review_cache_dir),
        review_cache_dir=str(review_cache_dir) if review_cache_dir else None,
        llm_cache_dir=args.llm_cache_dir,
        llm_cache_mode=args.llm_cache_mode,
    )
    try:
        rows = run_reviews(
            instances,
            repo_cache=args.repo_cache,
            results_dir=results_dir,
            pipeline=pipeline,
            keep_worktrees=args.keep_worktrees,
            fail_fast=args.fail_fast,
            limit=None,
            resume=args.resume,
        )
    finally:
        pipeline.close()

    ok = sum(1 for row in rows if row["status"] == "ok")
    resumed = sum(1 for row in rows if row.get("resumed"))
    print(f"\nreview done: {ok}/{len(rows)} ok; resumed={resumed}")
    print(f"results -> {results_dir}")
    print(f"next: python -m evaluation.cli metrics --run-id {run_id} --split {args.split}")
    return 0 if ok == len(rows) else 1


def cmd_metrics(args: argparse.Namespace) -> int:
    manifest, instances = _load_dataset(args)
    run_id = args.run_id or _default_run_id()
    results_dir = args.results_dir or _resolve_run_dir(
        DEFAULT_RESULTS_ROOT, manifest["version"], run_id
    )
    if not results_dir.exists():
        raise ManifestError(f"结果目录不存在: {results_dir}")
    instances = _restrict_to_actual_run(instances, results_dir)

    metrics = compute_deterministic_metrics(
        instances,
        results_dir=results_dir,
        repo_cache=args.repo_cache,
        run_id=run_id,
        split=args.split,
    )
    metrics_dir = args.metrics_dir or _resolve_run_dir(
        DEFAULT_METRICS_ROOT, manifest["version"], run_id
    )
    path = write_metrics(metrics, metrics_dir, run_id=run_id)
    print_deterministic_summary(metrics)

    retrieval_metrics = compute_retrieval_metrics(
        instances,
        results_dir=results_dir,
        run_id=run_id,
        split=args.split,
    )
    retrieval_path = write_retrieval_metrics(
        retrieval_metrics, metrics_dir, run_id=run_id
    )
    print_retrieval_summary(retrieval_metrics)

    print(f"metrics -> {path}")
    print(f"retrieval metrics -> {retrieval_path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from evaluation.report import (
        RunSpec,
        build_row,
        load_run,
        parse_run_spec,
        render_report,
        write_report,
    )

    try:
        specs = [parse_run_spec(raw) for raw in args.run]
        labels = {spec.label for spec in specs}
        if len(labels) != len(specs):
            raise ValueError("--run 的 label 不能重复")
        semantic_paths = {
            spec.label: spec.path
            for spec in (
                parse_run_spec(raw, option="--semantic")
                for raw in (args.semantic or [])
            )
        }
        unknown = sorted(set(semantic_paths) - labels)
        if unknown:
            raise ValueError(
                "--semantic 的 label 不在 --run 中: " + ", ".join(unknown)
            )

        rows = []
        for spec in specs:
            spec = RunSpec(
                label=spec.label,
                path=spec.path,
                semantic_path=semantic_paths.get(spec.label),
            )
            deterministic, retrieval, semantic = load_run(spec)
            rows.append(
                build_row(
                    spec.label,
                    deterministic,
                    retrieval,
                    semantic,
                )
            )
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    text = render_report(specs, rows, title=args.title)
    if args.out:
        path = write_report(text, args.out)
        print(f"report -> {path}")
    else:
        print(text)
    return 0


def cmd_semantic(args: argparse.Namespace) -> int:
    from evaluation.semantic import run_semantic_eval

    manifest, instances = _load_dataset(args)
    run_id = args.run_id or _default_run_id()
    results_dir = args.results_dir or _resolve_run_dir(
        DEFAULT_RESULTS_ROOT, manifest["version"], run_id
    )
    if not results_dir.exists():
        raise ManifestError(f"结果目录不存在: {results_dir}")
    instances = _restrict_to_actual_run(instances, results_dir)
    metrics_dir = args.metrics_dir or _resolve_run_dir(
        DEFAULT_METRICS_ROOT, manifest["version"], run_id
    )
    run_semantic_eval(
        instances,
        results_dir=results_dir,
        metrics_dir=metrics_dir,
        aacr_eval_dir=args.aacr_eval_dir,
        line_k=args.line_k,
        use_deprecated_score=args.use_deprecated_score,
    )
    return 0


def cmd_synthetic(args: argparse.Namespace) -> int:
    from evaluation.synthetic import (
        DEFAULT_CASES,
        estimate_synthetic_run,
        run_synthetic_benchmark,
    )

    cases = list(DEFAULT_CASES)
    if args.limit is not None:
        cases = cases[: args.limit]
    modes = ["full", "baseline"] if args.mode == "both" else [args.mode]

    estimates = [
        estimate_synthetic_run(cases, mode=mode) for mode in modes
    ]
    if args.estimate_only or args.budget_tokens is not None:
        for estimate in estimates:
            print(
                "[estimate] mode={mode} cases={cases} calls={llm_calls} "
                "prompt_tokens={prompt_tokens} "
                "completion_tokens={completion_tokens} "
                "total_tokens={total_tokens}".format(**estimate)
            )
            print(f"[estimate] assumption: {estimate['assumption']}")
        if args.estimate_only:
            return 0
        total_estimate = sum(item["total_tokens"] for item in estimates)
        if total_estimate > args.budget_tokens:
            print(
                "error: estimated {} tokens exceeds --budget-tokens {}; "
                "abort before creating repositories or calling LLMs.".format(
                    total_estimate, args.budget_tokens
                ),
                file=sys.stderr,
            )
            return 2

    run_id = args.run_id or _default_run_id()
    run_root = args.results_dir or (EVAL_ROOT / "results" / "synthetic" / run_id)
    work_root = args.work_dir or (EVAL_ROOT / ".cache" / "synthetic" / run_id)

    summaries = []
    for mode in modes:
        summary = run_synthetic_benchmark(
            cases,
            mode=mode,
            results_dir=run_root / mode,
            work_dir=work_root,
            mock_llm=args.mock_llm,
            resume=args.resume,
            keep_worktrees=args.keep_worktrees,
            budget_tokens=args.budget_tokens,
            hybrid_retrieval_enabled=args.hybrid_retrieval,
            verifier_enabled=not args.no_verifier,
            fail_fast=args.fail_fast,
        )
        summaries.append(summary)

    if len(summaries) > 1:
        print("\nSynthetic comparison")
        for summary in summaries:
            usage = summary["cost"]
            total_tokens = usage["input_tokens"] + usage["output_tokens"]
            print(
                f"- {summary['mode']}: bug_recall={summary['bug_recall']} "
                f"precision={summary['finding_precision']} "
                f"clean_fp={summary['clean_case_false_positive_rate']} "
                f"llm_calls={usage['llm_calls']} "
                f"total_tokens={total_tokens}"
            )

    failed = any(
        summary["cases_error"] > 0 or summary["stopped_by_budget"]
        for summary in summaries
    )
    print(f"\nsynthetic results -> {run_root}")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.cli",
        description="OpenReview 离线评测：AACR-Bench Python/Go 子集",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="生成固定 split manifest")
    prepare.add_argument("--source", type=Path, default=None, help="AACR positive_samples.json")
    prepare.add_argument("--download", action="store_true", help="本地缺失时下载官方数据")
    prepare.add_argument("--out", type=Path, default=DEFAULT_MANIFEST)
    prepare.add_argument("--seed", type=int, default=DEFAULT_SEED)
    prepare.add_argument(
        "--no-verify-source",
        dest="verify_source",
        action="store_false",
        help="跳过 SHA256 校验（仅用于本地 fixture / 镜像数据）",
    )
    prepare.set_defaults(func=cmd_prepare)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    common.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    common.add_argument("--split", choices=["all", "dev", "test", "holdout"], default="dev")
    common.add_argument("--limit", type=int, default=None)
    common.add_argument("--run-id", default=None)
    common.add_argument("--repo-cache", type=Path, default=DEFAULT_REPO_CACHE)
    common.add_argument(
        "--no-verify-source",
        dest="verify_source",
        action="store_false",
        help="跳过 SHA256 校验（仅用于本地 fixture / 镜像数据）",
    )

    review = sub.add_parser("review", parents=[common], help="调用 run_local 跑评测")
    review.add_argument("--results-dir", type=Path, default=None)
    review.add_argument(
        "--mock-llm", action="store_true", help="mock LLM，仅验证链路"
    )
    review.add_argument("--model", default=None)
    review.add_argument("--base-url", default=None)
    review.add_argument(
        "--reviewer-mode",
        choices=["single", "multi_aspect"],
        default="single",
        help="single=每个 batch 一次调用；multi_aspect=四维度并行（高召回/消融）",
    )
    review.add_argument("--max-workers", type=int, default=4)
    review.add_argument("--budget-calls", type=int, default=0)
    review.add_argument("--no-triage", action="store_true")
    review.add_argument("--no-impact", action="store_true")
    review.add_argument("--no-verifier", action="store_true")
    review.add_argument(
        "--no-hybrid-retrieval",
        action="store_true",
        help="关闭 FTS5 符号索引，仅用 tree-sitter AST 检索（消融用）",
    )
    review.add_argument(
        "--no-graph-retrieval",
        action="store_true",
        help="关闭 import/call graph 扩展，用于 +FTS vs +graph 消融",
    )
    review.add_argument("--keep-worktrees", action="store_true")
    review.add_argument("--fail-fast", action="store_true")
    review.add_argument(
        "--resume",
        action="store_true",
        help="跳过已有 status=ok 的结果；失败样本会重跑",
    )
    review.add_argument(
        "--estimate-only",
        action="store_true",
        help="只打印 token 估算，不 clone、不调用 LLM",
    )
    review.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="preflight 估算的批次总 token 超过该值时退出",
    )
    review.add_argument(
        "--review-cache",
        action="store_true",
        help="复用相同 PR head / 模型 / 开关的完整 review 结果",
    )
    review.add_argument(
        "--review-cache-dir",
        type=Path,
        default=None,
        help="review cache 目录（隐含启用 --review-cache）",
    )
    review.add_argument(
        "--llm-cache-dir",
        type=Path,
        default=None,
        help="LLM record/replay 缓存目录；不给则每次真实调用",
    )
    review.add_argument(
        "--llm-cache-mode",
        choices=["auto", "record", "replay"],
        default="auto",
        help="auto=有缓存则读、否则写入；record=强制重录；replay=缺缓存即失败",
    )
    review.set_defaults(func=cmd_review)

    synthetic = sub.add_parser(
        "synthetic", help="合成 bug / clean-control benchmark（可先用 mock）"
    )
    synthetic.add_argument(
        "--mode",
        choices=["full", "baseline", "both"],
        default="both",
        help="full=single-pass+检索+verifier；baseline=无 triage/impact/verifier；both=同批对照",
    )
    synthetic.add_argument("--limit", type=int, default=None)
    synthetic.add_argument("--run-id", default=None)
    synthetic.add_argument("--results-dir", type=Path, default=None)
    synthetic.add_argument("--work-dir", type=Path, default=None)
    synthetic.add_argument("--mock-llm", action="store_true")
    synthetic.add_argument("--resume", action="store_true")
    synthetic.add_argument("--keep-worktrees", action="store_true")
    synthetic.add_argument("--fail-fast", action="store_true")
    synthetic.add_argument(
        "--hybrid-retrieval",
        action="store_true",
        help="启用 FTS5 符号索引；默认关闭以保持合成 benchmark 轻量",
    )
    synthetic.add_argument(
        "--no-verifier",
        action="store_true",
        help="full 模式下关闭 verifier（消融用）",
    )
    synthetic.add_argument(
        "--estimate-only",
        action="store_true",
        help="只打印 preflight token 估算，不创建仓库、不调用 LLM",
    )
    synthetic.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="preflight 估算或运行累计 token 超过该值时退出",
    )
    synthetic.set_defaults(func=cmd_synthetic)

    metrics = sub.add_parser(
        "metrics", parents=[common], help="确定性指标（不调用 LLM）"
    )
    metrics.add_argument("--results-dir", type=Path, default=None)
    metrics.add_argument("--metrics-dir", type=Path, default=None)
    metrics.set_defaults(func=cmd_metrics)

    semantic = sub.add_parser(
        "semantic", parents=[common], help="可选的 AACR 官方语义 judge"
    )
    semantic.add_argument("--results-dir", type=Path, default=None)
    semantic.add_argument("--metrics-dir", type=Path, default=None)
    semantic.add_argument("--aacr-eval-dir", type=Path, required=True)
    semantic.add_argument("--line-k", type=int, default=1)
    semantic.add_argument("--use-deprecated-score", action="store_true")
    semantic.set_defaults(func=cmd_semantic)

    report = sub.add_parser(
        "report", help="把多次 run 的 metrics 汇总成 Markdown 表格"
    )
    report.add_argument(
        "--run",
        action="append",
        required=True,
        metavar="LABEL=METRICS_DIR",
        help="一次 run，例如 single=evaluation/metrics/aacr-pygo-v1/dev-single",
    )
    report.add_argument(
        "--semantic",
        action="append",
        metavar="LABEL=JSON",
        help="显式指定 AACR semantic metrics JSON；默认自动查找 metrics 目录",
    )
    report.add_argument("--title", default="OpenReview 评测结果")
    report.add_argument("--out", type=Path, default=None)
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ManifestError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
