# openreview/services/review_service.py
"""ReviewPipeline：构造 review 运行时，并把执行委托给 ReviewExecutor。

这个类现在只负责：

- 把 kwargs 收敛成不可变的 :class:`ReviewConfig`；
- 构造 GitHub client、retriever、LLM client、reviewer、verifier 和缓存；
- 提供 ``run`` / ``run_local`` / ``close`` 生命周期；
- 保留最近一次 ``last_state`` / ``last_result`` / ``last_outcome``。

真正的节点逻辑在 :mod:`openreview.services.review_executor`，配置校验和
cache key 在 :mod:`openreview.review.config`。这样 pipeline 不再是 god
object，测试也可以单独构造 executor。
"""

import logging
from pathlib import Path
from typing import Callable, Optional, Sequence

from openreview.github.client import GitHubClient
from openreview.repo.diff_parser import DiffParser
from openreview.repo.local import LocalReviewInput
from openreview.repo.manager import RepoManager
from openreview.retriever.base import Retriever
from openreview.retriever.hybrid import HybridRetriever
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.cache import ReviewCache
from openreview.review.config import ReviewConfig
from openreview.review.finding_validator import FindingValidator
from openreview.review.llm_cache import FileLLMCacheClient
from openreview.review.llm_client import (
    AbstractLLMClient,
    BudgetedLLMClient,
    LLMClient,
    MockLLMClient,
)
from openreview.review.output_parser import OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.report_writer import ReviewResult
from openreview.review.single_shot import SingleShotReviewer
from openreview.review.verifier import FindingVerifier
from openreview.review.workflow import ReviewOutcome, ReviewState
from openreview.services.review_executor import ReviewExecutor

logger = logging.getLogger(__name__)


class ReviewPipeline:
    """Review 的 facade / 组合根。

    默认使用 single-pass reviewer（每个 batch 一次 LLM 调用），
    ``reviewer_mode="multi_aspect"`` 可切回四维度并行，作为高召回模式和评测
    消融。这里刻意不引入“自主 Agent”：PR Review 的步骤是稳定的，真正需要
    动态决策的只有 triage、证据增强、verifier 回炉和失败兜底。
    """

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
        reviewer_mode: str = "single",
        max_workers: int = 4,
        aspect_timeout: float = 90.0,
        llm_timeout: float = 60.0,
        llm_max_retries: int = 0,
        temperature: float = 0.0,
        seed: Optional[int] = None,
        hybrid_retrieval_enabled: bool = True,
        graph_retrieval_enabled: bool = True,
        index_cache_dir: Optional[str] = None,
        triage_enabled: bool = True,
        impact_enabled: bool = True,
        static_analysis_enabled: bool = False,
        static_tools: Sequence[str] = ("ruff",),
        verifier_enabled: bool = True,
        verify_severities: Sequence[str] = ("critical", "high"),
        budget_calls: int = 0,
        budget_tokens: int = 0,
        budget_seconds: float = 0.0,
        review_cache_enabled: bool = False,
        review_cache_dir: Optional[str] = None,
        llm_cache_dir: Optional[str] = None,
        llm_cache_mode: str = "auto",
    ):
        self.config = ReviewConfig(
            mock_llm=mock_llm,
            github_token=github_token,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            max_batch_tokens=max_batch_tokens,
            max_patch_tokens=max_patch_tokens,
            max_context_tokens=max_context_tokens,
            reviewer_mode=reviewer_mode,
            max_workers=max_workers,
            aspect_timeout=aspect_timeout,
            llm_timeout=llm_timeout,
            llm_max_retries=llm_max_retries,
            temperature=temperature,
            seed=seed,
            hybrid_retrieval_enabled=hybrid_retrieval_enabled,
            graph_retrieval_enabled=graph_retrieval_enabled,
            index_cache_dir=index_cache_dir,
            triage_enabled=triage_enabled,
            impact_enabled=impact_enabled,
            static_analysis_enabled=static_analysis_enabled,
            static_tools=static_tools,
            verifier_enabled=verifier_enabled,
            verify_severities=verify_severities,
            budget_calls=budget_calls,
            budget_tokens=budget_tokens,
            budget_seconds=budget_seconds,
            review_cache_enabled=review_cache_enabled,
            review_cache_dir=review_cache_dir,
            llm_cache_dir=llm_cache_dir,
            llm_cache_mode=llm_cache_mode,
        )

        self.gh_client = GitHubClient(token=github_token)
        self.repo_mgr = RepoManager()
        self.diff_parser = DiffParser()
        # HybridRetriever 以 tree-sitter AST 为主路径，FTS5 符号索引只做增强；
        # 关闭时行为与旧版 TreeSitterLocalRetriever 完全一致，方便做消融。
        if hybrid_retrieval_enabled:
            self.retriever: Retriever = HybridRetriever(
                index_cache_dir=index_cache_dir,
                graph_enabled=graph_retrieval_enabled,
            )
        else:
            self.retriever = TreeSitterLocalRetriever()

        # 根据标志直接切换实例，不需要用 MagicMock 动态拼装。
        # 注意：GitHub Token 与 LLM API Key 是两套完全独立的凭证。
        effective_llm_timeout = (
            min(llm_timeout, aspect_timeout) if aspect_timeout > 0 else llm_timeout
        )
        base_client: AbstractLLMClient = (
            MockLLMClient()
            if mock_llm
            else LLMClient(
                api_key=llm_api_key,
                model=llm_model,
                base_url=llm_base_url,
                timeout=effective_llm_timeout,
                max_retries=llm_max_retries,
                temperature=temperature,
                seed=seed,
            )
        )
        # 预算包在真实客户端外面：aspect / verifier / retry 的每次调用都扣账。
        self._budgeted_client = BudgetedLLMClient(base_client)
        client: AbstractLLMClient = self._budgeted_client
        # record/replay 放在预算外层：cache hit 不消耗真实调用预算。
        if llm_cache_dir:
            client = FileLLMCacheClient(
                self._budgeted_client, llm_cache_dir, llm_cache_mode  # type: ignore[arg-type]
            )
        self.llm_client = client

        prompt_builder = PromptBuilder(
            max_patch_tokens=max_patch_tokens,
            max_context_tokens=max_context_tokens,
        )
        output_parser = OutputParser()
        validator = FindingValidator()
        self.reviewer: ReviewOrchestrator
        if self.config.reviewer_mode == "single":
            self.reviewer = SingleShotReviewer(
                prompt_builder=prompt_builder,
                llm_client=self.llm_client,
                output_parser=output_parser,
                validator=validator,
                max_workers=max_workers,
                aspect_timeout=aspect_timeout,
            )
        else:
            self.reviewer = ReviewOrchestrator(
                prompt_builder=prompt_builder,
                llm_client=self.llm_client,
                output_parser=output_parser,
                validator=validator,
                max_workers=max_workers,
                aspect_timeout=aspect_timeout,
            )
        self.verifier = (
            FindingVerifier(
                self.llm_client,
                verify_severities=self.config.verify_severities,
            )
            if verifier_enabled
            else None
        )
        self.review_cache = ReviewCache(
            review_cache_dir, enabled=review_cache_enabled
        )

        # run()/run_local() 结束后保留最后一次结果，供 CLI 导出 JSON 或评测读取。
        self.last_result: Optional[ReviewResult] = None
        self.last_state: Optional[ReviewState] = None
        self.last_outcome: Optional[ReviewOutcome] = None

    # ------------------------------------------------------------------
    # 兼容属性：调用方仍然可以读取 pipeline.max_batch_tokens 等旧字段。
    # ------------------------------------------------------------------
    @property
    def max_batch_tokens(self) -> int:
        return self.config.max_batch_tokens

    @property
    def reviewer_mode(self) -> str:
        return self.config.reviewer_mode

    @property
    def triage_enabled(self) -> bool:
        return self.config.triage_enabled

    @property
    def impact_enabled(self) -> bool:
        return self.config.impact_enabled

    @property
    def static_analysis_enabled(self) -> bool:
        return self.config.static_analysis_enabled

    @property
    def static_tools(self) -> tuple[str, ...]:
        return tuple(self.config.static_tools)

    @property
    def verifier_enabled(self) -> bool:
        return self.config.verifier_enabled

    @property
    def verify_severities(self) -> tuple[str, ...]:
        return tuple(self.config.verify_severities)

    @property
    def budget_calls(self) -> int:
        return self.config.budget_calls

    @property
    def budget_tokens(self) -> int:
        return self.config.budget_tokens

    @property
    def budget_seconds(self) -> float:
        return self.config.budget_seconds

    @property
    def hybrid_retrieval_enabled(self) -> bool:
        return self.config.hybrid_retrieval_enabled

    @property
    def graph_retrieval_enabled(self) -> bool:
        return self.config.graph_retrieval_enabled

    def close(self) -> None:
        self.gh_client.close()
        close_retriever = getattr(self.retriever, "close", None)
        if callable(close_retriever):
            close_retriever()

    def __enter__(self) -> "ReviewPipeline":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------
    def _executor(self) -> ReviewExecutor:
        """每次 run 新建 executor，捕获调用方当前替换过的组件。"""
        return ReviewExecutor(
            config=self.config,
            gh_client=self.gh_client,
            repo_mgr=self.repo_mgr,
            diff_parser=self.diff_parser,
            retriever=self.retriever,
            reviewer=self.reviewer,
            verifier=self.verifier,
            review_cache=self.review_cache,
            llm_client=self.llm_client,
            budgeted_client=self._budgeted_client,
        )

    def _execute(self, executor: ReviewExecutor, state: ReviewState) -> Path:
        try:
            return executor.execute(state)
        finally:
            self.last_state = executor.last_state
            self.last_result = executor.last_result
            self.last_outcome = executor.last_outcome

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
        executor = self._executor()
        state = executor.new_state(
            pr_url=pr_url,
            output_dir=output_dir,
            progress_cb=progress_cb,
            post_comments=post_comments,
        )
        return self._execute(executor, state)

    def run_local(
        self,
        repo_path: str | Path,
        base_sha: str,
        head_sha: str,
        output_dir: Optional[Path] = None,
        *,
        owner: str = "local",
        repo: str = "repo",
        pr_number: int = 0,
        title: Optional[str] = None,
        body: Optional[str] = None,
        pr_url: Optional[str] = None,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        keep_worktrees: bool = False,
    ) -> Path:
        """不依赖 GitHub API，在本地仓库上跑完整 review。

        适用于代码评测（AACR-Bench 等）和离线回放：输入是仓库路径 +
        base/head commit，pipeline 会创建临时 detached worktree，结束后清理。
        需要 GitHub 写操作的 ``post_comments`` 在本地模式下不生效。
        """
        resolved_repo = Path(repo_path).expanduser().resolve()
        base = base_sha.strip()
        head = head_sha.strip()
        if not base or not head:
            raise ValueError("base_sha and head_sha are required")
        if base == head:
            raise ValueError("base_sha and head_sha are identical; nothing to review")

        synthetic_url = pr_url or (
            f"local://{resolved_repo}@{base[:7]}..{head[:7]}"
        )
        local_input = LocalReviewInput(
            repo_path=resolved_repo,
            base_sha=base,
            head_sha=head,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            title=title,
            body=body,
            pr_url=pr_url,
            keep_worktrees=keep_worktrees,
        )
        executor = self._executor()
        state = executor.new_state(
            pr_url=synthetic_url,
            output_dir=output_dir or Path("reports"),
            progress_cb=progress_cb,
            local_mode=True,
            local_input=local_input,
        )
        try:
            return self._execute(executor, state)
        finally:
            if state.local_workspace is not None:
                state.local_workspace.cleanup()
