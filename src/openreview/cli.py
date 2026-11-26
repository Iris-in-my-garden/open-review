# openreview/cli.py
import logging
from pathlib import Path
from typing import Optional

import typer

from openreview.repo.manager import RepoCommandError
from openreview.review.report_writer import ReportWriter
from openreview.review.workflow import WORKFLOW_NODE_COUNT, ReviewStatus
from openreview.services.review_service import ReviewPipeline

# 创建应用对象
app = typer.Typer(
    help="OpenReview CLI: Automated GitHub PR Code Review Tool",
)

@app.callback()
def callback():
    """pr 审查"""
    pass

@app.command()
def review(
    pr_url: str = typer.Argument(
        ...,
        help="GitHub PR URL (e.g., https://github.com/owner/repo/pull/123)",
    ),
    output: Path = typer.Option(
        Path("reports"),
        "--output",
        "-o",
        help="Directory to save the review markdown report",
    ),
    json_out: Optional[Path] = typer.Option(
        None,
        "--json-out",
        help="Also export the structured result (JSON, incl. source_aspects/metrics)",
    ),
    github_token: str = typer.Option(
        None,
        "--token",
        envvar="GITHUB_TOKEN",
        help="GitHub Personal Access Token (仅用于访问 GitHub API)",
    ),
    llm_model: str = typer.Option(
        None,
        "--model",
        envvar="OPENAI_MODEL",
        help="Review 主模型（默认 deepseek-v4-pro）",
    ),
    verifier_llm_model: str = typer.Option(
        None,
        "--verifier-model",
        envvar="OPENREVIEW_VERIFIER_MODEL",
        help="Verifier 模型（默认 DeepSeek 下用 deepseek-flash）",
    ),
    llm_base_url: str = typer.Option(
        None,
        "--base-url",
        envvar="OPENAI_BASE_URL",
        help="OpenAI 兼容 API base URL（DeepSeek 模型默认官方地址）",
    ),
    thinking: bool = typer.Option(
        True,
        "--thinking/--no-thinking",
        help="Review 是否启用 thinking 模式（仅 DeepSeek 发送该参数）",
    ),
    verifier_thinking: bool = typer.Option(
        False,
        "--verifier-thinking/--no-verifier-thinking",
        help="Verifier 是否启用 thinking 模式（默认关闭以节省成本）",
    ),
    reasoning_effort: str = typer.Option(
        "high",
        "--reasoning-effort",
        help="Thinking 强度：none | low | high | max",
    ),
    mock_llm: bool = typer.Option(
        False,
        "--mock_llm",
        help = "use mock llm",
    ),
    post_comment: bool = typer.Option(
        False,
        "--post-comment",
        help="Post findings back to the PR as GitHub inline review comments",
    ),
    max_batch_tokens: int = typer.Option(
        12000,
        "--max-batch-tokens",
        help=(
            "Optional extra upper bound on estimated patch tokens per batch; "
            "0 removes this cap, but the internal prompt budget still applies"
        ),
    ),
    ablation: Optional[str] = typer.Option(
        None,
        "--ablation",
        help=(
            "Fixed ablation level: diff (patch only) | ast (+AST folding) | "
            "graph (+code graph) | verifier (+post-hoc verifier). "
            "Omit to run the production pipeline."
        ),
    ),
    budget_calls: int = typer.Option(
        0,
        "--budget-calls",
        help="Hard cap on LLM calls per review (0 = unlimited)",
    ),
    budget_tokens: int = typer.Option(
        0,
        "--budget-tokens",
        help=(
            "Preflight cap on estimated input tokens per review (0 = unlimited); "
            "provider usage is reconciled after each response"
        ),
    ),
    budget_output_tokens: int = typer.Option(
        0,
        "--budget-output-tokens",
        help=(
            "Hard cap on provider output tokens; reserved per request and applied "
            "as max_completion_tokens (0 = unlimited)"
        ),
    ),
    budget_seconds: float = typer.Option(
        0.0,
        "--budget-seconds",
        help=(
            "LLM request time budget; each request timeout is capped by the "
            "remaining time (0 = unlimited)"
        ),
    ),
    cache_review: bool = typer.Option(
        False,
        "--cache-review/--no-cache-review",
        help="Reuse a previous review for the same PR head SHA",
    ),
    cache_dir: str = typer.Option(
        None,
        "--cache-dir",
        help="Review cache directory (default: platform cache dir)",
    ),
    llm_cache_dir: str = typer.Option(
        None,
        "--llm-cache-dir",
        help="Record/replay LLM responses into this directory",
    ),
    llm_cache_mode: str = typer.Option(
        "auto",
        "--llm-cache-mode",
        help="LLM cache mode: auto | record | replay",
    ),
    llm_max_retries: int = typer.Option(
        0,
        "--llm-max-retries",
        help="Retries for transient LLM API failures (default: 0)",
    ),
    max_completion_tokens: int = typer.Option(
        16384,
        "--max-completion-tokens",
        help=(
            "Per-call output token cap (thinking 模式需要为 reasoning 预留空间; "
            "0 disables the cap)."
        ),
    ),
    dump_intermediates: Optional[Path] = typer.Option(
        None,
        "--dump-intermediates",
        help=(
            "Write prompts, parsed LLM responses, per-finding validation/verifier "
            "decisions, and final state to this directory"
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable debug logging",
    ),
):
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("openreview").setLevel(
        logging.DEBUG if verbose else logging.INFO
    )

    def print_progress(step: int, msg: str):
        typer.secho(
            f"--> [{step}/{WORKFLOW_NODE_COUNT}] {msg}", fg=typer.colors.CYAN
        )

    pipeline = None

    def show_failure_metrics() -> None:
        failed_state = pipeline.last_state if pipeline is not None else None
        if failed_state is None or not failed_state.metrics_snapshot:
            return
        metrics_path = (
            failed_state.output_dir / f"run_{failed_state.run_id}_metrics.json"
        )
        if metrics_path.exists():
            typer.secho(
                f"Failure metrics saved to: {metrics_path.resolve()}",
                fg=typer.colors.YELLOW,
                err=True,
            )

    try:
        pipeline = ReviewPipeline(
            github_token=github_token,
            llm_model=llm_model,
            verifier_llm_model=verifier_llm_model,
            llm_base_url=llm_base_url,
            llm_thinking=thinking,
            verifier_llm_thinking=verifier_thinking,
            llm_reasoning_effort=reasoning_effort,
            mock_llm=mock_llm,
            max_batch_tokens=max_batch_tokens,
            ablation=ablation,
            budget_calls=budget_calls,
            budget_tokens=budget_tokens,
            budget_output_tokens=budget_output_tokens,
            budget_seconds=budget_seconds,
            review_cache_enabled=cache_review,
            review_cache_dir=cache_dir,
            llm_cache_dir=llm_cache_dir,
            llm_cache_mode=llm_cache_mode,
            llm_max_retries=llm_max_retries,
            max_completion_tokens=max_completion_tokens,
            debug_dir=str(dump_intermediates) if dump_intermediates else None,
        )
        report_path = pipeline.run(
            pr_url,
            output,
            progress_cb=print_progress,
            post_comments=post_comment,
        )

        json_path = None
        if json_out is not None:
            if pipeline.last_result is None:
                raise RuntimeError("Review finished without a result to export.")
            json_path = ReportWriter.write_json(pipeline.last_result, json_out)

        outcome = pipeline.last_outcome
        if outcome is not None and outcome.status == ReviewStatus.PARTIAL:
            # 有批次失败 / 预算耗尽时仍会生成报告，但不能伪装成完整成功。
            typer.secho(
                "\n⚠️  Review finished with partial results. "
                f"Report saved to:\n{report_path.resolve()}",
                fg=typer.colors.YELLOW,
                bold=True,
                err=True,
            )
            for reason in outcome.degraded_reasons:
                typer.secho(f"  - {reason}", fg=typer.colors.YELLOW, err=True)
            if json_path is not None:
                typer.secho(
                    f"Structured result saved to: {json_path.resolve()}",
                    fg=typer.colors.YELLOW,
                )
            if dump_intermediates is not None:
                typer.secho(
                    "Intermediate artifacts: "
                    f"{dump_intermediates.resolve()}",
                    fg=typer.colors.YELLOW,
                )
            raise typer.Exit(code=outcome.exit_code)

        typer.secho(
            f"\n✨ Review completed successfully! Report saved to:\n{report_path.resolve()}",
            fg=typer.colors.GREEN,
            bold=True,
        )
        if json_path is not None:
            typer.secho(
                f"Structured result saved to: {json_path.resolve()}",
                fg=typer.colors.GREEN,
            )
        if dump_intermediates is not None:
            typer.secho(
                f"Intermediate artifacts: {dump_intermediates.resolve()}",
                fg=typer.colors.GREEN,
            )

    except typer.Exit:
        raise
    except RepoCommandError as e:
        typer.secho(
            f"\n❌ Git Command Error: {e.stderr}", fg=typer.colors.RED, err=True
        )
        show_failure_metrics()
        raise typer.Exit(code=1) from e
    except Exception as e:
        err_msg = str(e)
        if github_token and github_token in err_msg:
            err_msg = err_msg.replace(github_token, "***")

        typer.secho(
            f"\n❌ Review failed: {err_msg}", fg=typer.colors.RED, err=True
        )
        show_failure_metrics()
        raise typer.Exit(code=1) from e
    finally:
        if pipeline is not None:
            pipeline.close()


def main():
    """CLI 启动入口函数"""
    app()

if __name__ == "__main__":
    main()