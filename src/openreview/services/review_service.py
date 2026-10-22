# openreview/services/review_service.py
from pathlib import Path
from typing import Callable, Optional

from openreview.repo.diff_parser import DiffParser
from openreview.github.client import GitHubClient
from openreview.github.pr_url import  parse_pr_url
from openreview.repo.manager import RepoManager
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.finding_validator import FindingValidator
from openreview.review.output_parser import OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.report_writer import ReportWriter
from openreview.review.llm_client import LLMClient, MockLLMClient


class ReviewPipeline:

    def __init__(
        self,
        mock_llm: bool = False,
        github_token: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_base_url: Optional[str] = None,
    ):
        self.gh_client = GitHubClient(token=github_token)
        self.repo_mgr = RepoManager()
        self.diff_parser = DiffParser()
        # self.retriever = GrepRetriever()
        self.retriever = TreeSitterLocalRetriever()

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
        self.reviewer = ReviewOrchestrator(prompt_builder=PromptBuilder(), llm_client=llm_client, output_parser=OutputParser(),
                                 validator=FindingValidator())

    def run(
        self,
        pr_url: str,
        output_dir: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Path:
        """运行完整的 PR Review 流程。

        :param progress_cb: 可选的回调函数，用于向外推送进度提示，形如 cb(step_number,
        message)
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

        # 4. 解析 Diff
        log(4, "Parsing diffs and extracting symbols...")
        parsed_diff = self.diff_parser.parse_diff([f.model_dump() for f in raw_files])

        # 5. 上下文检索
        log(5, "Retrieving relevant code context...")
        contexts = self.retriever.retrieve(local_repo_path, parsed_diff)

        # 6. 执行 LLM Review
        log(6, "Running AI Code Reviewer...")
        review_result = self.reviewer.review(
            pr_info=pr_info, parsed_diff=parsed_diff, contexts=contexts
        )

        # 7. 生成报告
        log(7, "Writing markdown report...")
        report_path = ReportWriter.write(review_result, output_dir)

        return report_path