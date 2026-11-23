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

    # 5) 固定四档消融
    python -m evaluation.cli review --split dev --run-id dev-diff --ablation diff
    python -m evaluation.cli review --split dev --run-id dev-ast --ablation ast
    python -m evaluation.cli review --split dev --run-id dev-graph --ablation graph
    python -m evaluation.cli review --split dev --run-id dev-verifier --ablation verifier

    # 6) 汇总多次 run 为 Markdown 表格
    python -m evaluation.cli report \
        --run diff=evaluation/metrics/aacr-pygo-v3/dev-diff \
        --run graph=evaluation/metrics/aacr-pygo-v3/dev-graph
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
DEFAULT_MANIFEST = EVAL_ROOT / "manifests" / "aacr_pygo_v3.json"
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


def _load_manifest_instances(args: argparse.Namespace):
    """Dry-run only needs pinned repo/commit metadata, not reference comments."""
    manifest = load_manifest(args.manifest)
    instances = [
        item
        for item in manifest["instances"]
        if args.split == "all" or item["split"] == args.split
    ]
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
    ablation: str | None = None,
) -> dict:
    """AACR review 的保守 token preflight。

    不 clone 仓库，只看 manifest 里的 change_line_count / reference_comments。
    检索上下文按 ablation 档位做粗估：``diff`` 不注入上下文，``ast`` 只注入
    单文件折叠，``graph`` 额外注入跨文件图。真实 prompt 还受 batching、
    ``max_context_tokens`` 与重试影响，因此这里明确标注为 lower bound。
    """
    from openreview.review.ablation import resolve_ablation

    spec = resolve_ablation(ablation) if ablation else None
    verifier = spec.verifier_enabled if spec else True
    mode_label = spec.label if spec else "production"
    retrieval_mode = spec.retrieval_mode if spec else "graph"

    prompt_tokens = 0
    completion_tokens = 0
    retrieval_tokens = 0
    calls = 0
    for instance in instances:
        lines = max(1, int(instance.get("change_line_count") or 0))
        per_review_prompt = 5000 + lines * 12
        prompt_tokens += per_review_prompt
        completion_tokens += 450
        calls += 1
        # 上下文粗估：AST 只注入变更所在函数/类，graph 还会注入 caller/callee/
        # import 等跨文件证据。系数是启发式的，只用于“开跑前拦截明显超支”。
        if retrieval_mode == "ast":
            context = min(3000, 256 + lines * 4)
        elif retrieval_mode == "graph":
            context = min(6000, 512 + lines * 8)
        else:
            context = 0
        if spec is None:
            # 生产默认还开启 impact/static，留一点余量。
            context += 1000
        prompt_tokens += context
        retrieval_tokens += context
        if verifier:
            prompt_tokens += 1500 + int(instance.get("reference_comments") or 1) * 250
            completion_tokens += 300
            calls += 1
    return {
        "instances": len(instances),
        "llm_calls": calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "retrieval_tokens": retrieval_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "assumption": (
            f"{mode_label}; patch ~12 tokens/change line + retrieval "
            f"~{retrieval_tokens} tokens; lower bound (batching/retries excluded)"
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


def cmd_prepare_repos(args: argparse.Namespace) -> int:
    from evaluation.repos import prepare_repositories

    _, instances = _load_manifest_instances(args)
    count = prepare_repositories(instances, cache_dir=args.repo_cache)
    print(
        f"[repo-cache] prepared {count} unique repositories "
        f"for {len(instances)} instances"
    )
    return 0


def cmd_check_run(args: argparse.Namespace) -> int:
    from evaluation.results import has_reusable_results
    from evaluation.run_config import expected_run_config

    manifest, instances = _load_manifest_instances(args)
    results_dir = args.results_dir or _resolve_run_dir(
        DEFAULT_RESULTS_ROOT, manifest["version"], args.run_id
    )
    config = expected_run_config(
        manifest_path=args.manifest,
        split=args.split,
        ablation=args.ablation,
        model=args.model,
        base_url=args.base_url,
    )
    if has_reusable_results(
        instances,
        results_dir=results_dir,
        evaluation_config=config,
    ):
        print(f"[reuse] complete compatible results found: {results_dir}")
        return 0
    print(f"[reuse] compatible complete results not found: {results_dir}")
    return 1


def cmd_review(args: argparse.Namespace) -> int:
    from evaluation.results import run_reviews
    from openreview.services.review_service import ReviewPipeline

    manifest, instances = (
        _load_manifest_instances(args) if args.dry_run else _load_dataset(args)
    )
    run_id = args.run_id or _default_run_id()
    results_dir = args.results_dir or _resolve_run_dir(
        DEFAULT_RESULTS_ROOT, manifest["version"], run_id
    )

    if args.preflight_tokens is not None:
        estimate = _estimate_aacr_review(
            instances,
            ablation=args.ablation,
        )
        print(
            "[estimate] instances={instances} calls={llm_calls} "
            "prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} "
            "retrieval_tokens={retrieval_tokens} "
            "total_tokens={total_tokens}".format(**estimate)
        )
        print(f"[estimate] assumption: {estimate['assumption']}")
        if estimate["total_tokens"] > args.preflight_tokens:
            print(
                "error: estimated {} tokens exceeds --preflight-tokens {}; "
                "abort before any LLM call.".format(
                    estimate["total_tokens"], args.preflight_tokens
                ),
                file=sys.stderr,
            )
            return 2

    if args.dry_run:
        import json

        from evaluation.context_dry_run import build_context_dry_run
        from evaluation.repos import ensure_repo

        pipeline = ReviewPipeline(
            mock_llm=True,
            ablation=args.ablation,
        )
        try:
            reports = []
            for index, instance in enumerate(instances, start=1):
                repo = ensure_repo(
                    args.repo_cache,
                    instance["repo"],
                    instance.get("clone_url")
                    or f"https://github.com/{instance['repo']}.git",
                    instance["base_commit"],
                    instance["head_commit"],
                )
                print(
                    f"[dry-run {index}/{len(instances)}] "
                    f"{instance['instance_id']}",
                    file=sys.stderr,
                )
                reports.append(
                    build_context_dry_run(
                        instance,
                        repo_path=repo,
                        pipeline=pipeline,
                    )
                )
        finally:
            pipeline.close()

        report = {
            "llm_called": False,
            "token_estimator": (
                "openreview.review.token_budget.estimate_tokens "
                "(heuristic, not provider tokenizer)"
            ),
            "instances": reports,
            "final_prompt_tokens_estimate": sum(
                item["final_prompt_tokens_estimate"] for item in reports
            ),
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.dry_run_output:
            args.dry_run_output.parent.mkdir(parents=True, exist_ok=True)
            args.dry_run_output.write_text(rendered, encoding="utf-8")
            print(f"[dry-run] detail report -> {args.dry_run_output}")
        else:
            print(rendered, end="")
        return 0

    review_cache_dir = args.review_cache_dir
    if args.review_cache and not review_cache_dir:
        review_cache_dir = EVAL_ROOT / ".cache" / "review"
    from evaluation.run_config import expected_run_config

    evaluation_config = expected_run_config(
        manifest_path=args.manifest,
        split=args.split,
        ablation=args.ablation,
        model=args.model,
        base_url=args.base_url,
        mock_llm=args.mock_llm,
    )
    pipeline = ReviewPipeline(
        mock_llm=args.mock_llm,
        llm_model=args.model,
        llm_base_url=args.base_url,
        ablation=args.ablation,
        budget_calls=args.budget_calls,
        budget_tokens=args.run_budget_tokens,
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
            evaluation_config=evaluation_config,
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


def cmd_gate(args: argparse.Namespace) -> int:
    from evaluation.gate import (
        GateThresholds,
        evaluate_gate,
        load_metrics,
        print_gate_result,
    )

    thresholds = GateThresholds(
        min_results_ok_rate=args.min_results_ok_rate,
        max_unknown_file_rate=args.max_unknown_file_rate,
        max_invalid_line_rate=args.max_invalid_line_rate,
        max_ungrounded_rate=args.max_ungrounded_rate,
        min_evidence_coverage=args.min_evidence_coverage,
        min_evidence_evaluable_rate=args.min_evidence_evaluable_rate,
        min_attribution_coverage=args.min_attribution_coverage,
    )
    try:
        metrics = load_metrics(args.metrics)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    result = evaluate_gate(metrics, thresholds)
    print_gate_result(result, args.metrics)
    return 0 if result.passed else 1


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
    if args.dry_run or args.preflight_tokens is not None:
        for estimate in estimates:
            print(
                "[estimate] mode={mode} cases={cases} calls={llm_calls} "
                "prompt_tokens={prompt_tokens} "
                "completion_tokens={completion_tokens} "
                "total_tokens={total_tokens}".format(**estimate)
            )
            print(f"[estimate] assumption: {estimate['assumption']}")
        if args.dry_run:
            return 0
        total_estimate = sum(item["total_tokens"] for item in estimates)
        if total_estimate > args.preflight_tokens:
            print(
                "error: estimated {} tokens exceeds --preflight-tokens {}; "
                "abort before creating repositories or calling LLMs.".format(
                    total_estimate, args.preflight_tokens
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
            budget_tokens=args.run_budget_tokens,
            ablation=args.ablation,
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

    prepare_repos = sub.add_parser(
        "prepare-repos",
        parents=[common],
        help="预热评测仓库缓存并确保所需 base/head commits 可用",
    )
    prepare_repos.set_defaults(func=cmd_prepare_repos)

    check_run = sub.add_parser(
        "check-run", parents=[common], help=argparse.SUPPRESS
    )
    check_run.add_argument("--results-dir", type=Path, default=None)
    check_run.add_argument("--ablation", choices=["diff", "ast", "graph", "verifier"])
    check_run.add_argument("--model", default=None)
    check_run.add_argument("--base-url", default=None)
    check_run.set_defaults(func=cmd_check_run)

    review = sub.add_parser("review", parents=[common], help="调用 run_local 跑评测")
    review.add_argument("--results-dir", type=Path, default=None)
    review.add_argument(
        "--mock-llm", action="store_true", help="mock LLM，仅验证链路"
    )
    review.add_argument("--model", default=None)
    review.add_argument("--base-url", default=None)
    review.add_argument(
        "--ablation",
        choices=["diff", "ast", "graph", "verifier"],
        default=None,
        help=(
            "固定消融档位：diff=纯 Diff；ast=+AST Folding；"
            "graph=+代码图谱；verifier=+Verifier。缺省跑生产默认。"
        ),
    )
    review.add_argument("--budget-calls", type=int, default=0)
    review.add_argument("--keep-worktrees", action="store_true")
    review.add_argument("--fail-fast", action="store_true")
    review.add_argument(
        "--resume",
        action="store_true",
        help="跳过已有 status=ok 的结果；失败样本会重跑",
    )
    review.add_argument(
        "--dry-run",
        action="store_true",
        help="准备真实仓库并检索上下文，输出逐段文本和 prompt 估算；不调用 LLM",
    )
    review.add_argument(
        "--dry-run-output",
        type=Path,
        default=None,
        help="dry-run JSON 详细报告路径；缺省输出到 stdout",
    )
    review.add_argument(
        "--preflight-tokens",
        type=int,
        default=None,
        help="按 manifest 估算整批 token；超过该值则在任何 LLM 调用前退出",
    )
    review.add_argument(
        "--run-budget-tokens",
        type=int,
        default=0,
        help="单个 PR run 的运行时输入 token 上限；0 表示不限制",
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
        "--ablation",
        choices=["diff", "ast", "graph", "verifier"],
        default=None,
        help=(
            "覆盖 full/baseline 的默认档位；缺省 full=verifier、baseline=ast。"
        ),
    )
    synthetic.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印 preflight token 估算，不创建仓库、不调用 LLM",
    )
    synthetic.add_argument(
        "--preflight-tokens",
        type=int,
        default=None,
        help="按 case 估算整批 token；超过该值则在创建仓库前退出",
    )
    synthetic.add_argument(
        "--run-budget-tokens",
        type=int,
        default=None,
        help="运行期累计 token 上限；超过后停止剩余 case；缺省不限制",
    )
    synthetic.set_defaults(func=cmd_synthetic)

    metrics = sub.add_parser(
        "metrics", parents=[common], help="确定性指标（不调用 LLM）"
    )
    metrics.add_argument("--results-dir", type=Path, default=None)
    metrics.add_argument("--metrics-dir", type=Path, default=None)
    metrics.set_defaults(func=cmd_metrics)

    gate = sub.add_parser(
        "gate",
        help="确定性指标硬阈值门禁（不调用 LLM，适合 CI）",
    )
    gate.add_argument(
        "--metrics",
        type=Path,
        required=True,
        help="metrics 命令产出的 deterministic_<run>.json",
    )
    gate.add_argument("--min-results-ok-rate", type=float, default=1.0)
    gate.add_argument("--max-unknown-file-rate", type=float, default=0.0)
    gate.add_argument("--max-invalid-line-rate", type=float, default=0.0)
    gate.add_argument("--max-ungrounded-rate", type=float, default=0.0)
    gate.add_argument("--min-evidence-coverage", type=float, default=0.8)
    gate.add_argument("--min-evidence-evaluable-rate", type=float, default=0.8)
    gate.add_argument("--min-attribution-coverage", type=float, default=0.9)
    gate.set_defaults(func=cmd_gate)

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
        help="一次 run，例如 single=evaluation/metrics/aacr-pygo-v3/dev-single",
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
