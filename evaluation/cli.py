"""OpenReview 离线评测 CLI。

示例（在项目根目录运行）::

    # 1) 生成固定 manifest（只需一次）
    python -m evaluation.cli prepare --source /path/to/positive_samples.json

    # 2) 在 dev 子集上跑 mock review，验证链路
    python -m evaluation.cli review --split dev --run-id dev-mock --mock-llm

    # 3) 零成本确定性指标
    python -m evaluation.cli metrics --split dev --run-id dev-mock

    # 4) 可选：用 AACR 官方 judge 算语义 P/R（需要 AACR checkout + JUDGE_*）
    python -m evaluation.cli semantic --split dev --run-id dev \
        --aacr-eval-dir /path/to/aacr-bench/evaluation
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

    review_cache_dir = args.review_cache_dir
    if args.review_cache and not review_cache_dir:
        review_cache_dir = EVAL_ROOT / ".cache" / "review"
    pipeline = ReviewPipeline(
        mock_llm=args.mock_llm,
        llm_model=args.model,
        llm_base_url=args.base_url,
        max_workers=args.max_workers,
        triage_enabled=not args.no_triage,
        impact_enabled=not args.no_impact,
        verifier_enabled=not args.no_verifier,
        hybrid_retrieval_enabled=not args.no_hybrid_retrieval,
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
        )
    finally:
        pipeline.close()

    ok = sum(1 for row in rows if row["status"] == "ok")
    print(f"\nreview done: {ok}/{len(rows)} ok")
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
    review.add_argument("--mock-llm", action="store_true", help="零成本 mock，仅验证链路")
    review.add_argument("--model", default=None)
    review.add_argument("--base-url", default=None)
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
    review.add_argument("--keep-worktrees", action="store_true")
    review.add_argument("--fail-fast", action="store_true")
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

    metrics = sub.add_parser("metrics", parents=[common], help="零成本确定性指标")
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
