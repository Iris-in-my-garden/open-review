# openreview/cli.py
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
        help="GitHub Personal Access Token",
    ),
):
    def print_progress(step: int, msg: str):
        typer.secho(f"--> [{step}/7] {msg}", fg=typer.colors.CYAN)

    try:
        pipeline = ReviewPipeline(github_token=github_token)
        report_path = pipeline.run(
            pr_url, output, progress_cb=print_progress
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
        raise typer.Exit(code=1)
    except Exception as e:
        err_msg = str(e)
        if github_token and github_token in err_msg:
            err_msg = err_msg.replace(github_token, "***")

        typer.secho(
            f"\n❌ Review failed: {err_msg}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)


def main():
    """CLI 启动入口函数"""
    app()

if __name__ == "__main__":
    main()