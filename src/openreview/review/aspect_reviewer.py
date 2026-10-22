from typing import List, Optional, Dict, Any
import concurrent.futures
from pydantic import BaseModel, Field

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.grep_retriever import CodeContext
from openreview.review.report_writer import ReviewResult
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.llm_client import AbstractLLMClient, LLMClient
from openreview.review.output_parser import OutputParser, AspectReviewTaskResult
from openreview.review.finding_validator import FindingValidator

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
    ):
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.llm_client = llm_client or LLMClient()
        self.output_parser = output_parser or OutputParser()
        self.validator = validator or FindingValidator()

        # 定義支援的多維度審查清單
        self.aspects = aspects or ["clean_code", "bugs", "performance", "security"]

    def review(
            self,
            pr_info: PullRequestInfo,
            parsed_diff: ParsedDiff,
            contexts: List[CodeContext],
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

        all_aspect_results = []

        # 2. 使用 ThreadPoolExecutor 進行多維度「並行」審查，大幅提升執行效率
        max_workers = min(len(self.aspects), 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有 Worker 任務
            future_to_aspect = {
                executor.submit(worker.review_aspect, pr_info, parsed_diff, contexts): worker.aspect
                for worker in workers
            }

            # 收集並行執行的返回結果
            for future in concurrent.futures.as_completed(future_to_aspect):
                aspect = future_to_aspect[future]
                try:
                    result = future.result()
                    all_aspect_results.append(result)
                except Exception as exc:
                    # 實務上可加上 Log 記錄，避免單一維度報錯導致整個 PR Review 失敗
                    print(f"Aspect review failed for [{aspect}]: {exc}")

        # 3. 聚合所有維度的結果
        metadata = {
            "owner": pr_info.owner,
            "repo": pr_info.repo,
            "pr_number": pr_info.pr_number
        }
        aggregated_data = self.output_parser.aggregate_aspects(all_aspect_results)
        review_result = self.output_parser.build_final_result(aggregated_data, metadata)

        # 4. 後置校验与过滤 (Validator)
        validated_result = self.validator.validate(review_result, parsed_diff)

        return validated_result