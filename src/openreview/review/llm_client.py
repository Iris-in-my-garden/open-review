import os
import re
from typing import Optional, Type, TypeVar, Protocol
from pydantic import BaseModel
from openai import OpenAI

from openreview.review.output_parser import ReviewResultSchema, PotentialIssueSchema

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
    """真实的 OpenAI 客户端。

    配置与 GitHub Token 完全解耦：
    - api_key:OPENAI_API_KEY
    - model: OPENAI_MODEL，默认 gpt-4o
    - base_url: OPENAI_BASE_URL（用于兼容 OpenAI 协议的第三方服务）
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "缺少 OpenAI API Key。请设置环境变量 OPENAI_API_KEY，"
                "或使用 `openreview review <PR_URL> --mock_llm` 进行离线演示。"
            )

        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")

        client_kwargs = {
            "api_key": self.api_key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self._client = OpenAI(**client_kwargs)

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
    """专门用于离线开发和测试的 Mock 客户端（支持根据 Schema 动态生成对应的 Mock 数据）"""

    def __init__(self, custom_schema: Optional[BaseModel] = None):
        self.custom_schema = custom_schema

    @staticmethod
    def _extract_anchor(user_prompt: str, filename: str) -> tuple[Optional[int], Optional[str]]:
        """从带行号的 Patch 中取第一处新增/上下文行，作为 line + evidence。"""
        pattern = rf"^### {re.escape(filename)}\n```diff\n(.*?)\n```"
        block = re.search(pattern, user_prompt, flags=re.MULTILINE | re.DOTALL)
        if not block:
            return None, None

        for raw_line in block.group(1).splitlines():
            added = re.match(r"^\s*(\d+) \| \+(.*)$", raw_line)
            if added and added.group(2).strip():
                return int(added.group(1)), added.group(2).strip()
            context = re.match(r"^\s*(\d+) \|  (.*)$", raw_line)
            if context and context.group(2).strip():
                return int(context.group(1)), context.group(2).strip()
        return None, None

    def generate_structured(
            self,
            system_prompt: str,
            user_prompt: str,
            response_schema: Type[T],
    ) -> T:
        # 1. 如果用户自定义了匹配的实例，优先返回
        if self.custom_schema and isinstance(self.custom_schema, response_schema):
            return self.custom_schema

        schema_name = response_schema.__name__

        # 2. 针对单维度审查的 Schema (AspectReviewOutputSchema)
        if "AspectReviewOutputSchema" in schema_name:
            # 从 User Prompt 的 Patch 区块中取出真实文件名，
            # 避免 Mock 问题因“文件不在 Diff 中”被校验器全部过滤掉。
            changed_files = re.findall(
                r"^### (.+?)\n```diff", user_prompt, flags=re.MULTILINE
            )
            primary_file = changed_files[0] if changed_files else "src/example.py"
            secondary_file = (
                changed_files[1] if len(changed_files) > 1 else primary_file
            )
            primary_line, primary_evidence = self._extract_anchor(
                user_prompt, primary_file
            )
            secondary_line, secondary_evidence = self._extract_anchor(
                user_prompt, secondary_file
            )

            # 依据不同情况塞入模拟的 PotentialIssueSchema 列表
            mock_issues = [
                PotentialIssueSchema(
                    file=primary_file,
                    severity="high",
                    category="security",
                    confidence=0.92,
                    reasoning="发现硬编码的 JWT 密钥或敏感凭证，存在严重的安全隐患。",
                    suggestion="请将密钥移除，并改用环境变量（如 os.environ.get('JWT_SECRET')）进行加载。",
                    line=primary_line,
                    evidence=primary_evidence,
                ),
                PotentialIssueSchema(
                    file=secondary_file,
                    severity="medium",
                    category="clean_code",
                    confidence=0.85,
                    reasoning="函数体过长且嵌套层级较深，可读性较差。",
                    suggestion="建议将核心逻辑拆分为独立的小函数，遵循单一职责原则（SRP）。",
                    line=secondary_line,
                    evidence=secondary_evidence,
                )
            ]

            return response_schema(
                aspect_summary="[MOCK] 该维度的代码检查完成，发现了一些需要注意的潜在问题。",
                issues=mock_issues
            )

        # 3. 针对全局总表的 Schema (ReviewResultSchema)
        return response_schema(
            summary="[MOCK] 这是一个测试 PR Review 总结",
            risk_level="MEDIUM",
            risk_reason="[MOCK] 发现少量高风险或中风险问题，建议修复后再合并。",
            key_changes=["[MOCK] 调整了认证逻辑与参数传递"],
            potential_issues=[],
            missing_test_suggestions=["[MOCK] 建议针对修改后的认证流程补充单元测试。"],
            evidence=["src/auth/service.py"],
            final_decision="REQUEST_CHANGES",
            final_summary="[MOCK] 多维度审查结束。最终决策: REQUEST_CHANGES (风险等级: MEDIUM)"
        )