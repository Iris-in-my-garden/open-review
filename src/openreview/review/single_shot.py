"""单遍审查（single-pass）实现。

每个 batch 用一次结构化 LLM 调用覆盖五类风险；大 PR 仍由
``plan_review_batches`` 按内部 prompt 预算拆成多个 batch，因此
“single-shot”指每个 batch 一次调用，而不是整个 PR 只调用一次。

原 multi-aspect 并行审查已移除：在没有证据表明四倍调用能带来成比例收益前，
只保留单遍路径，降低成本和评测维度。
"""

from __future__ import annotations

from typing import List, Optional

from openreview.domain.models import PullRequestInfo, ReviewResult
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.finalize import finalize_review_result
from openreview.review.finding_validator import FindingValidator
from openreview.review.llm_client import AbstractLLMClient, LLMClient
from openreview.review.output_parser import (
    AspectReviewOutputSchema,
    AspectReviewTaskResult,
    OutputParser,
)
from openreview.review.prompt_builder import PromptBuilder

SINGLE_SHOT_INSTRUCTION = """### 【一次性审查任务】
请在一次输出中覆盖以下五类风险，并在每个 issue 的 category 中标明类别：

- bugs：逻辑错误、边界条件、空值/异常分支
- security：注入、硬编码密钥、权限校验缺失
- performance：不必要的重复计算、N+1、内存拷贝
- clean_code：结构性可维护性问题（重复代码、职责过多、魔法数字、隐藏耦合）
- test_gap：本次改动缺少必要的测试覆盖

**Finding Gate（硬门槛）**
只报告能完整证明以下四段因果链的问题：触发条件、执行路径、实际结果、可观察影响。
任一环节缺少直接证据、依赖猜测或需要用截断/省略代码补全，直接不报告。

**test_gap 判定标准**
只有能指出“缺失的具体分支或输入、对应变更行、现有测试为何不覆盖、可执行的测试用例”
四项时才报告 test_gap。不要仅因改动没有伴随测试，或无法看到测试文件就报告；
若现有测试已覆盖触发条件和影响路径，也不要重复报告。

**clean_code 判定标准**
只报告能证实会直接影响 bug 修复、数据一致性、并发正确性或高频变更风险的问题。
禁止纯风格偏好、局部重复、命名偏好、格式问题及缺少具体维护影响的主观重构建议。

**置信度分档**
- 0.90–1.00：完整代码链与直接证据
- 0.75–0.89：核心链完整，缺少次要上下文
- 0.60–0.74：触发条件或影响路径存在局限
- 低于 0.60：不报告
不得因潜在影响严重而上调置信度。

只报告本次 diff 引入（或本次改动直接使其变得危险）的问题，scope 填 introduced。
未改动代码里的历史问题默认不要报告；如果确实相关，必须把 scope 填为 pre_existing，
系统默认会过滤这类 finding。
只报告输入中有直接证据的问题；没有发现时 issues 返回空列表。
新增/上下文行使用 Patch 标注的新文件行号并设 side=RIGHT；删除行使用 `L` 标注的
旧文件行号并设 side=LEFT。evidence 只填代码本身，不要带行号或 `|` 前缀。
每个 issue 另提供 short_claim：一句独立、可验证的缺陷断言，最多 180 个字符，
只说明可观察的问题，不包含长背景推理或修复建议。
"""


class SingleShotReviewer:
    """每个 batch 用一次 LLM 调用完成审查。"""

    # 供 ReviewPipeline 做预算预检：单遍审查每个 batch 固定 1 次调用。
    calls_per_review = 1
    # 预算不足时整批跳过，不存在“只跑部分维度”的中间态。
    supports_aspect_narrowing = False

    def __init__(
        self,
        prompt_builder: Optional[PromptBuilder] = None,
        llm_client: Optional[AbstractLLMClient] = None,
        output_parser: Optional[OutputParser] = None,
        validator: Optional[FindingValidator] = None,
        debug_sink=None,
    ):
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.llm_client = llm_client or LLMClient()
        self.output_parser = output_parser or OutputParser()
        self.validator = validator or FindingValidator()
        self.debug_sink = debug_sink

    def review(
        self,
        pr_info: PullRequestInfo,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext],
        repo_path=None,
        aspects: Optional[List[str]] = None,
    ) -> ReviewResult:
        # aspects 是 triage 给出的“审查重点”，不是调用次数。
        focus = self._focus_instruction(aspects)
        system_prompt = self.prompt_builder.build_system_prompt()
        user_prompt = self.prompt_builder.build_user_prompt(
            pr_info,
            parsed_diff,
            contexts,
            task_instruction=SINGLE_SHOT_INSTRUCTION + focus,
        )
        if self.debug_sink is not None:
            self.debug_sink.record_prompt("single_shot", system_prompt, user_prompt)
        raw_data = self.llm_client.generate_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_schema=AspectReviewOutputSchema,
        )
        if self.debug_sink is not None:
            self.debug_sink.record_llm_response("single_shot", raw_data)
        return finalize_review_result(
            prompt_builder=self.prompt_builder,
            output_parser=self.output_parser,
            validator=self.validator,
            pr_info=pr_info,
            parsed_diff=parsed_diff,
            contexts=contexts,
            repo_path=repo_path,
            aspect_results=[
                AspectReviewTaskResult(aspect="single_shot", data=raw_data)
            ],
            failures=[],
        )

    @staticmethod
    def _focus_instruction(aspects: Optional[List[str]]) -> str:
        """triage 收窄审查范围时，把重点写进 prompt，而不是减少调用次数。"""
        if not aspects:
            return ""
        labels = {
            "clean_code": "代码规范与可维护性",
            "bugs": "逻辑错误与边界条件",
            "performance": "性能与资源使用",
            "security": "安全漏洞与权限校验",
        }
        names = [labels.get(aspect, aspect) for aspect in aspects]
        return (
            "\n\n### 【本次 triage 重点】\n"
            + "、".join(names)
            + "\n仍然只报告有直接证据的问题；未列出的类别如果发现高置信度问题也可以报告。"
        )
