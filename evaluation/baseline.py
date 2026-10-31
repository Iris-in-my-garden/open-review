"""评测用的简化 baseline。

评测里需要一个“更简单但同口径”的对照，否则很难回答“检索、impact、verifier
到底带来了什么”。这个 baseline 刻意保留：

- 同一套 ``PromptBuilder`` 的 patch/context 预算与不可信输入隔离；
- 同一套 ``OutputParser`` / ``FindingValidator`` / 测试缺口分析；
- 同一个 ``ReviewResult`` 与结果 JSON 格式；
- 同一个 single-pass reviewer。

它只去掉 triage、impact 和 verifier。这样 full vs baseline 的差异主要来自
证据增强和后处理，而不是落盘格式或 reviewer 实现。

多维度并行审查（``reviewer_mode="multi_aspect"``）是另一条消融轴，默认不启用。
"""

from __future__ import annotations

from openreview.review.single_shot import (  # re-export for compatibility
    SINGLE_SHOT_INSTRUCTION,
    SingleShotReviewer,
)
from openreview.services.review_service import ReviewPipeline

__all__ = [
    "SINGLE_SHOT_INSTRUCTION",
    "SingleShotReviewer",
    "build_baseline_pipeline",
]


def build_baseline_pipeline(
    *, mock_llm: bool = False, **pipeline_kwargs
) -> ReviewPipeline:
    """构造 baseline pipeline，并保持 ``ReviewPipeline`` 的生命周期接口。

    默认就是 single-pass，因此这里只需要关掉证据增强与后处理。
    """
    pipeline_kwargs.setdefault("reviewer_mode", "single")
    pipeline_kwargs.setdefault("triage_enabled", False)
    pipeline_kwargs.setdefault("impact_enabled", False)
    pipeline_kwargs.setdefault("verifier_enabled", False)
    return ReviewPipeline(mock_llm=mock_llm, **pipeline_kwargs)
