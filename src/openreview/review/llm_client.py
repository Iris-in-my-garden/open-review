import os
from typing import Optional, Type, TypeVar, Protocol
from pydantic import BaseModel
from openai import OpenAI

from openreview.review.output_parser import ReviewResultSchema

T = TypeVar("T", bound=BaseModel)


class AbstractLLMClient(Protocol):
    """LLM 客户端统一接口规范"""
    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        ...


class LLMClient:
    """真实的 OpenAI 客户端"""
    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "mock-key")
        self.model = model
        self._client = OpenAI(api_key=self.api_key)

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        response = self._client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format=response_schema,
        )

        message = response.choices[0].message

        # 检查模型是否拒绝了请求
        if message.refusal:
            raise ValueError(f"Model refused to respond: {message.refusal}")

        # 检查解析结果是否存在
        if message.parsed is None:
            raise RuntimeError("Failed to parse structured output from model response.")

        return message.parsed


class MockLLMClient:
    """专门用于离线开发和测试的 Mock 客户端"""
    def __init__(self, custom_schema: Optional[BaseModel] = None):
        self.custom_schema = custom_schema

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        if self.custom_schema and isinstance(self.custom_schema, response_schema):
            return self.custom_schema

        # 默认生成的静态 Mock 数据
        return ReviewResultSchema(
            summary="[MOCK] 这是一个测试 PR Review 总结",
            risk_level="LOW",
            risk_reason="[MOCK] 修改量较小，无敏感模块变更",
            key_changes=["[MOCK] 调整了代码结构与参数传递"],
            potential_issues=[],
            missing_test_suggestions=["[MOCK] 建议补充单元测试"],
            evidence=[],
            final_decision="APPROVE",
            final_summary="[MOCK] 代码质量良好，通过审查",
        )