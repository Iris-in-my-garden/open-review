"""单遍审查（single-pass）实现。

默认生产路径使用一次结构化 LLM 调用覆盖四类风险；大 PR 仍由
``plan_review_batches`` 按内部 prompt 预算拆成多个 batch，因此
“single-shot”指每个 batch 一次调用，而不是整个 PR 只调用一次。

多维度并行审查保留在 ``aspect_reviewer.py``，作为高召回模式和消融配置。
"""

from __future__ import annotations

from typing import List, Optional

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.aspect_reviewer import ReviewOrchestrator, finalize_review_result
from openreview.review.output_parser import (
    AspectReviewOutputSchema,
    AspectReviewTaskResult,
)
from openreview.review.report_writer import ReviewResult

SINGLE_SHOT_INSTRUCTION = """### 【一次性审查任务】
请在一次输出中覆盖以下五类风险，并在每个 issue 的 category 中标明类别：

- bugs：逻辑错误、边界条件、空值/异常分支
- security：注入、硬编码密钥、权限校验缺失
- performance：不必要的重复计算、N+1、内存拷贝
- clean_code：结构性可维护性问题（重复代码、职责过多、魔法数字、隐藏耦合）
- test_gap：本次改动缺少必要的测试覆盖

只报告本次 diff 引入（或本次改动直接使其变得危险）的问题，scope 填 introduced。
未改动代码里的历史问题默认不要报告；如果确实相关，必须把 scope 填为 pre_existing，
系统默认会过滤这类 finding。
只报告输入中有直接证据的问题；没有发现时 issues 返回空列表。
新增/上下文行使用 Patch 标注的新文件行号并设 side=RIGHT；删除行使用 `L` 标注的
旧文件行号并设 side=LEFT。evidence 只填代码本身，不要带行号或 `|` 前缀。
"""


class SingleShotReviewer(ReviewOrchestrator):
    """每个 batch 用一次 LLM 调用完成审查。

    继续继承 ``ReviewOrchestrator``，是为了复用同一套 prompt、schema、
    validator 和结果聚合逻辑；区别只在于不创建 4 个 aspect worker。
    """

    # 供 ReviewPipeline 做预算预检：单遍审查每个 batch 固定 1 次调用。
    calls_per_review = 1
    # 单遍审查不能像多维度那样“只跑剩余的几个维度”，预算不足时整批跳过。
    supports_aspect_narrowing = False

    def review(
        self,
        pr_info: PullRequestInfo,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext],
        repo_path=None,
        aspects: Optional[List[str]] = None,
    ) -> ReviewResult:
        # aspects 在单遍模式下是 triage 给出的“审查重点”，不是调用次数。
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
