# openreview/cli.py
import logging
from pathlib import Path

import typer

from openreview.repo.manager import RepoCommandError
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
        help="LLM model name (default: gpt-4o)",
    ),
    llm_base_url: str = typer.Option(
        None,
        "--base-url",
        envvar="OPENAI_BASE_URL",
        help="OpenAI-compatible API base URL",
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
        60000,
        "--max-batch-tokens",
        help="Max estimated patch tokens per review batch (0 disables batching)",
    ),
    max_workers: int = typer.Option(
        4,
        "--max-workers",
        help="Max parallel aspect reviewers",
    ),
    retriever: str = typer.Option(
        "tree_sitter",
        "--retriever",
        help="Context retriever: tree_sitter (default) or grep (fallback)",
    ),
    reviewer_mode: str = typer.Option(
        "multi",
        "--reviewer",
        help="Reviewer mode: multi (4 aspects, default) or single (low-cost mode)",
    ),
    triage_enabled: bool = typer.Option(
        True,
        "--triage/--no-triage",
        help="Enable rule-based triage (skip docs-only PRs, narrow aspects)",
    ),
    impact_enabled: bool = typer.Option(
        True,
        "--impact/--no-impact",
        help="Inject cross-file references of changed symbols as context",
    ),
    static_analysis_enabled: bool = typer.Option(
        False,
        "--static-analysis/--no-static-analysis",
        help="Run ruff/mypy on changed files and inject findings (executes local tools)",
    ),
    static_tools: str = typer.Option(
        "ruff",
        "--static-tools",
        help="Comma separated static analysis tools: ruff,mypy",
    ),
    verifier_enabled: bool = typer.Option(
        True,
        "--verifier/--no-verifier",
        help="Run a second-pass semantic verifier on critical/high findings",
    ),
    budget_calls: int = typer.Option(
        0,
        "--budget-calls",
        help="Hard cap on LLM calls per review (0 = unlimited)",
    ),
    budget_tokens: int = typer.Option(
        0,
        "--budget-tokens",
        help="Hard cap on estimated input tokens per review (0 = unlimited)",
    ),
    budget_seconds: float = typer.Option(
        0.0,
        "--budget-seconds",
        help="Hard wall-clock budget for LLM work (0 = unlimited)",
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
        typer.secho(f"--> [{step}/8] {msg}", fg=typer.colors.CYAN)

    pipeline = None
    try:
        pipeline = ReviewPipeline(
            github_token=github_token,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            mock_llm=mock_llm,
            max_batch_tokens=max_batch_tokens,
            max_workers=max_workers,
            retriever=retriever,
            reviewer_mode=reviewer_mode,
            triage_enabled=triage_enabled,
            impact_enabled=impact_enabled,
            static_analysis_enabled=static_analysis_enabled,
            static_tools=tuple(
                tool.strip() for tool in static_tools.split(",") if tool.strip()
            ),
            verifier_enabled=verifier_enabled,
            budget_calls=budget_calls,
            budget_tokens=budget_tokens,
            budget_seconds=budget_seconds,
            review_cache_enabled=cache_review,
            review_cache_dir=cache_dir,
            llm_cache_dir=llm_cache_dir,
            llm_cache_mode=llm_cache_mode,
        )
        report_path = pipeline.run(
            pr_url,
            output,
            progress_cb=print_progress,
            post_comments=post_comment,
        )

        typer.secho(
            f"\n✨ Review completed successfully! Report saved to:\n{report_path.resolve()}",
            fg=typer.colors.GREEN,
            bold=True,
        )

    except RepoCommandError as e:
        typer.secho(
            f"\n❌ Git Command Error: {e.stderr}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1) from e
    except Exception as e:
        err_msg = str(e)
        if github_token and github_token in err_msg:
            err_msg = err_msg.replace(github_token, "***")

        typer.secho(
            f"\n❌ Review failed: {err_msg}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1) from e
    finally:
        if pipeline is not None:
            pipeline.close()


def main():
    """CLI 启动入口函数"""
    app()

if __name__ == "__main__":
    main()