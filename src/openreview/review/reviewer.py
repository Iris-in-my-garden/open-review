# openreview/reviewer.py
import os
from typing import List, Optional
from pydantic import BaseModel
from openai import OpenAI

from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.grep_retriever import CodeContext
from openreview.review.report_writer import PotentialIssue, ReviewResult
# openreview/reviewer.py
from typing import List, Optional
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.grep_retriever import CodeContext
from openreview.review.report_writer import ReviewResult
from prompt_builder import PromptBuilder
from llm_client import LLMClient, AbstractLLMClient
from output_parser import OutputParser
from finding_validator import FindingValidator

class Reviewer:
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
        pr_info: dict,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext],
    ) -> ReviewResult:
        # 1. 组装 Prompt
        system_prompt = self.prompt_builder.build_system_prompt()
        user_prompt = self.prompt_builder.build_user_prompt(pr_info, parsed_diff, contexts)

        # 2. 调用 LLM
        raw_output = self.llm_client.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=self.output_parser.schema,
        )

        # 3. 解析与格式转换
        metadata = {
            "owner": pr_info.get("base", {}).get("repo", {}).get("owner", {}).get("login", "unknown"),
            "repo": pr_info.get("base", {}).get("repo", {}).get("name", "unknown"),
            "pr_number": pr_info.get("number", 0),
        }
        review_result = self.output_parser.parse(raw_output, metadata)

        # 4. 后置校验与过滤
        validated_result = self.validator.validate(review_result, parsed_diff)

        return validated_result