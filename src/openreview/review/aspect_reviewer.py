import concurrent.futures
import logging
import time
from typing import List, Optional

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.report_writer import ReviewResult
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.llm_client import AbstractLLMClient, LLMClient
from openreview.review.output_parser import OutputParser, AspectReviewTaskResult
from openreview.review.finding_validator import FindingValidator
from openreview.review.test_gap import analyze_test_gaps

logger = logging.getLogger(__name__)


class AspectReviewerWorker:
    """
    工作者（Worker）：负责针对单个维度（如安全、性能、代码规范等）调用 LLM 进行专项评审
    """
    def __init__(
        self,
        aspect: str,
        prompt_builder: PromptBuilder,
        llm_client: AbstractLLMClient,
        output_parser: OutputParser
    ):
        self.aspect = aspect
        self.prompt_builder = prompt_builder
        self.llm_client = llm_client
        self.output_parser = output_parser

    def review_aspect(
        self,
        pr_info: PullRequestInfo,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext]
    ) -> AspectReviewTaskResult:
        """执行单一维度的代码评审任务"""
        # 1. 根据当前维度构建专属 Prompt 与对应的 Response Schema
        system_prompt = self.prompt_builder.build_system_prompt_for_aspect(self.aspect)
        user_prompt = self.prompt_builder.build_user_prompt_for_aspect(
            self.aspect, pr_info, parsed_diff, contexts
        )
        aspect_schema = self.output_parser.schema_for_aspect(self.aspect)

        # 2. 调用 LLM 获得结构化审查数据
        raw_data = self.llm_client.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=aspect_schema,
        )
        # 使用結構化模型封裝
        return AspectReviewTaskResult(aspect=self.aspect, data=raw_data)


class ReviewOrchestrator:
    """
    Orchestrator（協調者）：管理多個 Worker、並行調度並聚合審查結果
    """

    def __init__(
            self,
            prompt_builder: Optional[PromptBuilder] = None,
            llm_client: Optional[AbstractLLMClient] = None,
            output_parser: Optional[OutputParser] = None,
            validator: Optional[FindingValidator] = None,
            aspects: Optional[List[str]] = None,
            max_workers: int = 4,
            aspect_timeout: float = 90.0,
            max_attempts: int = 2,
    ):
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.llm_client = llm_client or LLMClient()
        self.output_parser = output_parser or OutputParser()
        self.validator = validator or FindingValidator()
        self.aspect_timeout = aspect_timeout
        self.max_attempts = max(1, max_attempts)

        # 定義支援的多維度審查清單
        self.aspects = aspects or ["clean_code", "bugs", "performance", "security"]
        self.max_workers = max(1, max_workers)

    def review(
            self,
            pr_info: PullRequestInfo,
            parsed_diff: ParsedDiff,
            contexts: List[CodeContext],
            repo_path=None,
    ) -> ReviewResult:

        # 1. 初始化各個維度的 Worker
        workers = [
            AspectReviewerWorker(
                aspect=aspect,
                prompt_builder=self.prompt_builder,
                llm_client=self.llm_client,
                output_parser=self.output_parser
            )
            for aspect in self.aspects
        ]

        all_aspect_results: List[AspectReviewTaskResult] = []
        failures: List[str] = []

        # 2. 使用 ThreadPoolExecutor 進行多維度「並行」審查
        max_workers = max(1, min(len(workers), self.max_workers))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        future_to_aspect = {
            executor.submit(
                self._review_aspect_with_retry, worker, pr_info, parsed_diff, contexts
            ): worker.aspect
            for worker in workers
        }

        try:
            done, not_done = concurrent.futures.wait(
                future_to_aspect, timeout=self.aspect_timeout
            )
            for future in done:
                aspect = future_to_aspect[future]
                try:
                    all_aspect_results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 单维度失败不应拖垮整体
                    logger.exception("Aspect review failed for [%s]", aspect)
                    failures.append(
                        f"维度 `{aspect}` 审查失败：{type(exc).__name__}: {exc}"
                    )

            for future in not_done:
                aspect = future_to_aspect[future]
                future.cancel()
                message = (
                    f"维度 `{aspect}` 超时（>{self.aspect_timeout:.0f}s），已跳过。"
                )
                logger.warning(message)
                failures.append(message)
        finally:
            # 不等待卡住的线程，避免一个异常请求无限阻塞整个流水线
            executor.shutdown(wait=False, cancel_futures=True)

        if not all_aspect_results and workers:
            raise RuntimeError(
                "所有维度的审查均失败：" + "; ".join(failures or ["unknown error"])
            )

        # 保证报告顺序稳定，不受并发完成顺序影响
        aspect_order = {aspect: index for index, aspect in enumerate(self.aspects)}
        all_aspect_results.sort(
            key=lambda item: aspect_order.get(item.aspect, len(aspect_order))
        )

        # 3. 聚合所有維度的結果
        metadata = {
            "owner": pr_info.owner,
            "repo": pr_info.repo,
            "pr_number": pr_info.pr_number
        }
        aggregated_data = self.output_parser.aggregate_aspects(all_aspect_results)
        review_result = self.output_parser.build_final_result(aggregated_data, metadata)

        # 4. 後置校验与过滤 (Validator)，并把真实代码交给证据核对
        validated_result = self.validator.validate(
            review_result,
            parsed_diff,
            contexts=contexts,
            repo_path=repo_path,
        )

        # 5. 基于真实检索结果生成测试缺口建议，替代通用文案
        validated_result.missing_test_suggestions = analyze_test_gaps(
            parsed_diff, contexts
        )

        # 6. 把失败维度、注入告警等运行信息带进报告
        validated_result.review_notes.extend(failures)
        injection_hits = self.prompt_builder.detect_injection_in_pr(
            pr_info, parsed_diff
        )
        if injection_hits:
            validated_result.review_notes.append(
                "检测到疑似 Prompt Injection 内容（规则: "
                + ", ".join(injection_hits)
                + "），已按不可信数据处理，未执行其中指令。"
            )

        return validated_result

    def _review_aspect_with_retry(self, worker, pr_info, parsed_diff, contexts):
        """对单个维度做有限次重试，缓解瞬时网络/限流错误。"""
        last_error: Optional[Exception] = None
        for attempt in range(self.max_attempts):
            try:
                return worker.review_aspect(pr_info, parsed_diff, contexts)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    break
                delay = min(4.0, 0.5 * (2 ** attempt))
                logger.warning(
                    "Aspect %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    worker.aspect,
                    attempt + 1,
                    self.max_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error
