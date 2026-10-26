"""Single-shot baseline：用一次 LLM 调用完成整份 PR review。

评测里需要一个“更简单但同口径”的对照，否则很难回答“多维度并行 +
verifier + 检索到底带来了什么”。这个 baseline 刻意保留：

- 同一套 ``PromptBuilder`` 的 patch/context 预算与不可信输入隔离；
- 同一套 ``OutputParser`` / ``FindingValidator`` / 测试缺口分析；
- 同一个 ``ReviewResult`` 与结果 JSON 格式。

它只去掉 multi-aspect 并行、triage、impact 和 verifier。这样 full vs
baseline 的差异主要来自审查策略，而不是后处理或落盘格式。
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
from openreview.services.review_service import ReviewPipeline

SINGLE_SHOT_INSTRUCTION = """### 【一次性审查任务】
请在一次输出中覆盖以下四类风险，并在每个 issue 的 category 中标明类别：

- bugs：逻辑错误、边界条件、空值/异常分支
- security：注入、硬编码密钥、权限校验缺失
- performance：不必要的重复计算、N+1、内存拷贝
- clean_code：明显的可维护性问题

只报告输入中有直接证据的问题；没有发现时 issues 返回空列表。
行号必须来自 Patch 左侧标注的新文件行号，evidence 必须逐字引用 Patch 或 Context。
"""


class SingleShotReviewer(ReviewOrchestrator):
    """用一个 LLM 调用替代多维度并行审查的 ablation baseline。"""

    def review(
        self,
        pr_info: PullRequestInfo,
        parsed_diff: ParsedDiff,
        contexts: List[CodeContext],
        repo_path=None,
        aspects: Optional[List[str]] = None,
    ) -> ReviewResult:
        # aspects 只用于 full pipeline 的维度消融；single-shot 一次性覆盖全部类别。
        del aspects
        user_prompt = (
            self.prompt_builder.build_user_prompt(pr_info, parsed_diff, contexts)
            + "\n\n"
            + SINGLE_SHOT_INSTRUCTION
        )
        raw_data = self.llm_client.generate_structured(
            system_prompt=self.prompt_builder.build_system_prompt(),
            user_prompt=user_prompt,
            response_schema=AspectReviewOutputSchema,
        )
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


def build_baseline_pipeline(
    *, mock_llm: bool = False, **pipeline_kwargs
) -> ReviewPipeline:
    """构造 baseline pipeline，并保持 ``ReviewPipeline`` 的生命周期接口。"""
    pipeline_kwargs.setdefault("triage_enabled", False)
    pipeline_kwargs.setdefault("impact_enabled", False)
    pipeline_kwargs.setdefault("verifier_enabled", False)
    pipeline = ReviewPipeline(mock_llm=mock_llm, **pipeline_kwargs)

    orchestrator = pipeline.reviewer
    if not isinstance(orchestrator, ReviewOrchestrator):
        pipeline.close()
        raise TypeError("baseline expects ReviewPipeline.reviewer to be an orchestrator")

    pipeline.reviewer = SingleShotReviewer(
        prompt_builder=orchestrator.prompt_builder,
        llm_client=pipeline.llm_client,
        output_parser=orchestrator.output_parser,
        validator=orchestrator.validator,
    )
    return pipeline


__all__ = ["SINGLE_SHOT_INSTRUCTION", "SingleShotReviewer", "build_baseline_pipeline"]
