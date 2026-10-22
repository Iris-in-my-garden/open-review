# openreview/services/review_service.py
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Optional

from openreview.github.client import GitHubClient
from openreview.github.pr_url import parse_pr_url
from openreview.repo.diff_parser import DiffParser
from openreview.repo.manager import RepoCommandError, RepoManager
from openreview.retriever.factory import build_retriever
from openreview.review.assessment import merge_review_results
from openreview.review.comment_builder import (
    build_review_body,
    build_review_comments,
)
from openreview.review.finding_validator import FindingValidator
from openreview.review.llm_client import LLMClient, MockLLMClient
from openreview.review.output_parser import OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.report_writer import ReportWriter
from openreview.review.reviewer_factory import build_reviewer
from openreview.review.token_budget import estimate_tokens, split_parsed_diff

logger = logging.getLogger(__name__)


class ReviewPipeline:

    def __init__(
        self,
        mock_llm: bool = False,
        github_token: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        max_batch_tokens: int = 60000,
        max_patch_tokens: int = 48000,
        max_context_tokens: int = 16000,
        max_workers: int = 4,
        aspect_timeout: float = 90.0,
        retriever: str = "tree_sitter",
        reviewer_mode: str = "multi",
    ):
        self.gh_client = GitHubClient(token=github_token)
        self.repo_mgr = RepoManager()
        self.diff_parser = DiffParser()
        self.retriever = build_retriever(retriever)
        self.max_batch_tokens = max_batch_tokens

        # 根据标志直接切换实例，不需要用 MagicMock 动态拼装。
        # 注意：GitHub Token 与 LLM API Key 是两套完全独立的凭证。
        llm_client = (
            MockLLMClient()
            if mock_llm
            else LLMClient(
                api_key=llm_api_key,
                model=llm_model,
                base_url=llm_base_url,
            )
        )
        self.reviewer = build_reviewer(
            reviewer_mode,
            prompt_builder=PromptBuilder(
                max_patch_tokens=max_patch_tokens,
                max_context_tokens=max_context_tokens,
            ),
            llm_client=llm_client,
            output_parser=OutputParser(),
            validator=FindingValidator(),
            max_workers=max_workers,
            aspect_timeout=aspect_timeout,
        )

    def close(self) -> None:
        self.gh_client.close()

    def __enter__(self) -> "ReviewPipeline":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def run(
        self,
        pr_url: str,
        output_dir: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        post_comments: bool = False,
    ) -> Path:
        """运行完整的 PR Review 流程。

        :param progress_cb: 可选的回调函数，用于向外推送进度提示，形如 cb(step_number,
        message)
        :param post_comments: 是否通过 GitHub Review API 发表行内评论
        """

        stages: Dict[str, float] = {}

        @contextmanager
        def timed(stage: str):
            start = time.perf_counter()
            try:
                yield
            finally:
                stages[stage] = round(time.perf_counter() - start, 3)

        def log(step: int, msg: str):
            if progress_cb:
                progress_cb(step, msg)

        # 1. 解析 PR URL
        log(1, "Parsing PR URL...")
        with timed("parse_url"):
            pr_ref = parse_pr_url(pr_url)

        # 2. 获取 PR 元数据与 Changed Files
        log(2, "Fetching PR metadata & changed files...")
        with timed("fetch_pr"):
            pr_info = self.gh_client.get_pull_request(pr_ref)
            raw_files = self.gh_client.list_changed_files(pr_ref)

        # 3. 准备本地 Git 仓库缓存（head + base）
        log(3, "Preparing local repository cache...")
        base_error: Optional[str] = None
        with timed("prepare_repo"):
            local_repo_path = self.repo_mgr.prepare_pr_repo(
                pr_ref.owner, pr_ref.repo, pr_ref.pr_number
            )

            base_path: Optional[Path] = None
            try:
                base_path = self.repo_mgr.prepare_base_worktree(
                    pr_ref.owner, pr_ref.repo, pr_info.base_sha
                )
            except RepoCommandError as exc:
                # base 侧只是增强上下文，失败不应阻断主流程
                base_error = exc.stderr
                logger.warning("Failed to prepare base worktree: %s", exc)

        # 4. 解析 Diff
        log(4, "Parsing diffs and extracting symbols...")
        with timed("parse_diff"):
            parsed_diff = self.diff_parser.parse_diff(
                [f.model_dump() for f in raw_files]
            )

        # 5. 大 PR 分批：按 patch token 预算拆分，避免一次塞爆上下文
        batches = split_parsed_diff(parsed_diff, self.max_batch_tokens)
        log(
            5,
            f"Retrieving code context and reviewing "
            f"{len(parsed_diff.files)} files in {len(batches)} batch(es)...",
        )

        # 6. 逐批检索上下文并执行多维度并行审查
        batch_results = []
        context_tokens = 0
        with timed("retrieve_and_review"):
            for index, batch in enumerate(batches, start=1):
                log(
                    6,
                    f"Running AI Code Reviewer "
                    f"(batch {index}/{len(batches)}, {len(batch.files)} files)...",
                )
                contexts = self.retriever.retrieve(
                    local_repo_path, batch, base_path=base_path
                )
                context_tokens += sum(
                    estimate_tokens(context.snippet) for context in contexts
                )
                batch_results.append(
                    self.reviewer.review(
                        pr_info=pr_info,
                        parsed_diff=batch,
                        contexts=contexts,
                        repo_path=local_repo_path,
                    )
                )

        review_result = (
            batch_results[0]
            if len(batch_results) == 1
            else merge_review_results(batch_results)
        )
        if len(batches) > 1:
            review_result.review_notes.append(
                f"PR 较大，已按 token 预算拆分为 {len(batches)} 个批次审查。"
            )
        if base_error:
            review_result.review_notes.append(
                f"base 版本上下文不可用（{base_error}），删除代码的上下文可能不完整。"
            )

        aspect_count = len(
            getattr(self.reviewer, "aspects", ["single_prompt"])
        )
        patch_tokens = sum(
            estimate_tokens(file_diff.patch or "")
            for file_diff in parsed_diff.files
        )
        review_result.metrics = {
            "stages_seconds": dict(stages),
            "files": len(parsed_diff.files),
            "batches": len(batches),
            "aspects": aspect_count,
            "llm_calls": len(batches) * aspect_count,
            "estimated_patch_tokens": patch_tokens,
            "estimated_context_tokens": context_tokens,
            "total_seconds": round(sum(stages.values()), 3),
        }
        logger.info("Review metrics: %s", review_result.metrics)

        # 7. 生成报告
        log(7, "Writing markdown report...")
        with timed("write_report"):
            report_path = ReportWriter.write(review_result, output_dir)

        # 8. 可选：把问题作为 GitHub 行内评论发回 PR
        if post_comments:
            log(8, "Posting review comments to GitHub...")
            with timed("post_comments"):
                comments = build_review_comments(review_result)
                self.gh_client.create_review(
                    pr_ref,
                    body=build_review_body(review_result),
                    comments=comments,
                    event="COMMENT",
                    commit_id=pr_info.head_sha,
                )
            logger.info("Posted %d inline comments", len(comments))

        # 报告首次写入时 write_report 尚未结束；这里补齐最终 metrics 后再落盘一次，
        # 让报告里的阶段耗时包含报告生成与（可选的）评论回写。
        review_result.metrics["stages_seconds"] = dict(stages)
        review_result.metrics["total_seconds"] = round(sum(stages.values()), 3)
        report_path.write_text(
            ReportWriter.render_markdown(review_result),
            encoding="utf-8",
        )

        logger.info("Stage timings: %s", stages)
        return report_path
