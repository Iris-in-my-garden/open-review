"""集中管理 review 的行为配置。

``ReviewConfig`` 只描述“会影响输出”的配置，不负责构造客户端或执行工作流。
这样做有两个目的：

- ``ReviewPipeline`` 不再把 30+ 个参数散落在构造函数里；
- review cache key 可以从同一份配置生成，避免“改了 batch/context/工具配置
  但缓存 key 没变”导致误命中。

检索与后处理通过固定的四档消融配置控制（见 :mod:`openreview.review.ablation`），
不再暴露多个重叠的检索 / 审查开关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from openreview.domain.versions import REVIEW_CACHE_SCHEMA_VERSION
from openreview.review.ablation import (
    ABLATION_LEVELS,
    RETRIEVAL_MODES,
    resolve_ablation,
)
from openreview.review.file_filter import FILE_FILTER_POLICY_VERSION
from openreview.review.llm_client import REASONING_EFFORTS
from openreview.review.token_budget import (
    DEFAULT_HARD_PROMPT_TOKENS,
    DEFAULT_SOFT_PROMPT_TOKENS,
)

LLM_CACHE_MODES = frozenset({"auto", "record", "replay"})

__all__ = [
    "ABLATION_LEVELS",
    "LLM_CACHE_MODES",
    "RETRIEVAL_MODES",
    "REVIEW_CACHE_SCHEMA_VERSION",
    "ReviewConfig",
]


@dataclass(frozen=True)
class ReviewConfig:
    """一次 review 的不可变配置。"""

    mock_llm: bool = False

    # 凭证与模型连接
    github_token: Optional[str] = None
    llm_api_key: Optional[str] = None
    # review 主模型；None 表示沿用 OPENAI_MODEL / 项目默认。
    llm_model: Optional[str] = None
    # verifier 可独立选便宜模型；None 表示 DeepSeek 用 flash，其他沿用主模型。
    verifier_llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_timeout: float = 60.0
    llm_max_retries: int = 0
    temperature: float = 0.0
    seed: Optional[int] = None
    # review 默认开启 thinking（DeepSeek）；verifier 默认关闭以控制成本。
    llm_thinking: bool = True
    verifier_llm_thinking: bool = False
    llm_reasoning_effort: Optional[str] = "high"
    # Per-call output cap. Thinking 模式要把 reasoning token 算进去，默认给足余量。
    max_completion_tokens: int = 16384

    # batching 与 prompt 预算
    # 单批原始 Diff 目标 8k；硬熔断 48k 只用于无法继续拆分的单个超大 hunk。
    max_batch_tokens: int = 12000
    max_patch_tokens: int = 8192
    hard_patch_tokens: int = 48000
    # P2-P6 共享的上下文扩展预算。
    max_context_tokens: int = 8192
    # 内部 prompt 预算策略（不暴露 CLI）：
    # soft 用于把超限内容打包成多个调用，hard 是触发拆分的上限。
    soft_prompt_tokens: int = DEFAULT_SOFT_PROMPT_TOKENS
    hard_prompt_tokens: int = DEFAULT_HARD_PROMPT_TOKENS

    # 固定消融阶梯；None 表示生产默认（图谱检索 + triage + impact + verifier）。
    ablation: Optional[str] = None
    # none | ast | graph
    retrieval_mode: str = "graph"

    # 检索与证据增强
    index_cache_dir: Optional[str] = None
    triage_enabled: bool = True
    # 在 triage / batching 前剔除 lockfile、图片、生成代码、纯数据/文档。
    file_filter_enabled: bool = True
    impact_enabled: bool = True
    static_analysis_enabled: bool = False
    static_tools: Sequence[str] = ("ruff",)
    verifier_enabled: bool = True
    verify_severities: Sequence[str] = ("critical", "high")
    verify_confidence_threshold: float = 0.75

    # 预算
    budget_calls: int = 0
    budget_tokens: int = 0
    budget_output_tokens: int = 0
    budget_seconds: float = 0.0

    # 缓存
    review_cache_enabled: bool = False
    review_cache_dir: Optional[str] = None
    llm_cache_dir: Optional[str] = None
    llm_cache_mode: str = "auto"

    # 调试产物目录；None 表示不写。它不影响 review 输出，因此不进 cache key。
    debug_dir: Optional[str] = None

    cache_schema_version: int = REVIEW_CACHE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.ablation is not None:
            spec = resolve_ablation(self.ablation)
            object.__setattr__(self, "ablation", spec.name)
            object.__setattr__(self, "retrieval_mode", spec.retrieval_mode)
            object.__setattr__(self, "verifier_enabled", spec.verifier_enabled)
            object.__setattr__(self, "triage_enabled", spec.triage_enabled)
            object.__setattr__(self, "impact_enabled", spec.impact_enabled)
            object.__setattr__(
                self, "static_analysis_enabled", spec.static_analysis_enabled
            )

        normalized_retrieval = self.retrieval_mode.strip().lower()
        if normalized_retrieval not in RETRIEVAL_MODES:
            choices = ", ".join(RETRIEVAL_MODES)
            raise ValueError(
                f"retrieval_mode must be one of {choices}, "
                f"got {self.retrieval_mode!r}"
            )
        object.__setattr__(self, "retrieval_mode", normalized_retrieval)

        if self.soft_prompt_tokens <= 0 or self.hard_prompt_tokens <= 0:
            raise ValueError(
                "soft_prompt_tokens and hard_prompt_tokens must be positive"
            )
        if self.soft_prompt_tokens > self.hard_prompt_tokens:
            raise ValueError(
                "soft_prompt_tokens must be <= hard_prompt_tokens, "
                f"got {self.soft_prompt_tokens} > {self.hard_prompt_tokens}"
            )

        normalized_cache_mode = self.llm_cache_mode.strip().lower()
        if normalized_cache_mode not in LLM_CACHE_MODES:
            raise ValueError(
                "llm_cache_mode must be 'auto', 'record' or 'replay', "
                f"got {self.llm_cache_mode!r}"
            )
        object.__setattr__(self, "llm_cache_mode", normalized_cache_mode)

        if self.llm_reasoning_effort is not None:
            normalized_effort = self.llm_reasoning_effort.strip().lower()
            if normalized_effort not in REASONING_EFFORTS:
                raise ValueError(
                    "llm_reasoning_effort must be one of "
                    f"{', '.join(sorted(REASONING_EFFORTS))}, "
                    f"got {self.llm_reasoning_effort!r}"
                )
            object.__setattr__(self, "llm_reasoning_effort", normalized_effort)

        if self.max_completion_tokens < 0:
            raise ValueError("max_completion_tokens must be >= 0")

        object.__setattr__(
            self,
            "static_tools",
            tuple(
                str(tool).strip()
                for tool in self.static_tools
                if str(tool).strip()
            ),
        )
        object.__setattr__(
            self,
            "verify_severities",
            tuple(
                str(severity).strip().lower()
                for severity in self.verify_severities
                if str(severity).strip()
            ),
        )
        if not 0.0 <= self.verify_confidence_threshold <= 1.0:
            raise ValueError(
                "verify_confidence_threshold must be between 0 and 1"
            )

    def cache_key_parts(self) -> List[str]:
        """所有会影响 review 输出的配置。

        模型名 / temperature / seed 由调用方从实际客户端读取，因为测试或
        自定义 reviewer 可能替换 ``llm_client``；这里只负责其余配置。
        """
        return [
            f"schema={self.cache_schema_version}",
            f"ablation={self.ablation or 'custom'}",
            f"retrieval={self.retrieval_mode}",
            f"triage={int(self.triage_enabled)}",
            f"file_filter={int(self.file_filter_enabled)}",
            f"file_filter_policy={FILE_FILTER_POLICY_VERSION}",
            f"impact={int(self.impact_enabled)}",
            f"static={int(self.static_analysis_enabled)}",
            f"static_tools={','.join(self.static_tools)}",
            f"verifier={int(self.verifier_enabled)}",
            f"verify_severities={','.join(self.verify_severities)}",
            f"verify_confidence_threshold={self.verify_confidence_threshold}",
            f"max_batch_tokens={self.max_batch_tokens}",
            f"max_patch_tokens={self.max_patch_tokens}",
            f"hard_patch_tokens={self.hard_patch_tokens}",
            f"max_context_tokens={self.max_context_tokens}",
            f"soft_prompt_tokens={self.soft_prompt_tokens}",
            f"hard_prompt_tokens={self.hard_prompt_tokens}",
            f"llm_timeout={self.llm_timeout}",
            f"llm_max_retries={self.llm_max_retries}",
            f"verifier_model={self.verifier_llm_model or 'default'}",
            f"thinking={int(self.llm_thinking)}",
            f"verifier_thinking={int(self.verifier_llm_thinking)}",
            f"reasoning_effort={self.llm_reasoning_effort or 'default'}",
            f"max_completion_tokens={self.max_completion_tokens}",
            f"mock={int(self.mock_llm)}",
        ]
