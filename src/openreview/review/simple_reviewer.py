# openreview/simple_reviewer.py
from pathlib import Path
from typing import List, Optional

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.finding_validator import FindingValidator
from openreview.review.llm_client import AbstractLLMClient, LLMClient
from openreview.review.output_parser import OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.report_writer import ReviewResult
from openreview.review.test_gap import analyze_test_gaps


class Reviewer:
    aspects = ["single_prompt"]

    def __init__(
        self,
        prompt_builder: Optional[PromptBuilder] = None,
        llm_client: Optional[AbstractLLMClient] = None,
        output_parser: Optional[OutputParser] = None,
        validator: Optional[FindingValidator] = None,
    ):
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.llm_client = llm_client or LLMClient()
        self.output_parser = output_parser or OutputParser()
        self.validator = validator or FindingValidator()

    def review(
        self,
        pr_info: PullRequestInfo,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext],
        repo_path: Optional[Path] = None,
        aspects: Optional[List[str]] = None,
    ) -> ReviewResult:
        _ = aspects  # 单 Prompt 模式不使用维度选择，保持与其他 Reviewer 一致的接口
        # 1. 组装 Prompt
        system_prompt = self.prompt_builder.build_system_prompt()
        user_prompt = self.prompt_builder.build_user_prompt(pr_info, parsed_diff, contexts)

        # 2. 调用 LLM
        review_data = self.llm_client.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=self.output_parser.schema,
        )

        # 3. 解析与格式转换
        metadata = {
            "owner": pr_info.owner,
            "repo": pr_info.repo,
            "pr_number": pr_info.pr_number
        }
        review_result = self.output_parser.build_final_result(review_data, metadata)

        # 4. 后置校验与过滤（带真实上下文做证据核对）
        validated_result = self.validator.validate(
            review_result,
            parsed_diff,
            contexts=contexts,
            repo_path=repo_path,
        )
        validated_result.missing_test_suggestions = analyze_test_gaps(
            parsed_diff, contexts
        )

        return validated_result