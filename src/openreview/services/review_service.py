# openreview/services/review_service.py
import logging
from pathlib import Path
from typing import Callable, Optional

from openreview.repo.diff_parser import DiffParser
from openreview.github.client import GitHubClient
from openreview.github.pr_url import parse_pr_url
from openreview.repo.manager import RepoCommandError, RepoManager
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.comment_builder import (
    build_review_body,
    build_review_comments,
)
from openreview.review.finding_validator import FindingValidator
from openreview.review.output_parser import OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.report_writer import ReportWriter
from openreview.review.llm_client import LLMClient, MockLLMClient
from openreview.review.assessment import merge_review_results
from openreview.review.token_budget import split_parsed_diff

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
    ):
        self.gh_client = GitHubClient(token=github_token)
        self.repo_mgr = RepoManager()
        self.diff_parser = DiffParser()
        # self.retriever = GrepRetriever()
        self.retriever = TreeSitterLocalRetriever()
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
        self.reviewer = ReviewOrchestrator(
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

        def log(step: int, msg: str):
            if progress_cb:
                progress_cb(step, msg)

        # 1. 解析 PR URL
        log(1, "Parsing PR URL...")
        pr_ref = parse_pr_url(pr_url)

        # 2. 获取 PR 元数据与 Changed Files
        log(2, "Fetching PR metadata & changed files...")
        pr_info = self.gh_client.get_pull_request(pr_ref)
        raw_files = self.gh_client.list_changed_files(pr_ref)

        # 3. 准备本地 Git 仓库缓存
        log(3, "Preparing local repository cache...")
        local_repo_path = self.repo_mgr.prepare_pr_repo(
            pr_ref.owner, pr_ref.repo, pr_ref.pr_number
        )

        base_path: Optional[Path] = None
        base_error: Optional[str] = None
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
        for index, batch in enumerate(batches, start=1):
            log(
                6,
                f"Running AI Code Reviewer "
                f"(batch {index}/{len(batches)}, {len(batch.files)} files)...",
            )
            contexts = self.retriever.retrieve(
                local_repo_path, batch, base_path=base_path
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

        # 7. 生成报告
        log(7, "Writing markdown report...")
        report_path = ReportWriter.write(review_result, output_dir)

        # 8. 可选：把问题作为 GitHub 行内评论发回 PR
        if post_comments:
            log(8, "Posting review comments to GitHub...")
            comments = build_review_comments(review_result)
            self.gh_client.create_review(
                pr_ref,
                body=build_review_body(review_result),
                comments=comments,
                event="COMMENT",
                commit_id=pr_info.head_sha,
            )
            logger.info("Posted %d inline comments", len(comments))

        return report_path
