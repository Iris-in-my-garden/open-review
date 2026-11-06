"""集中管理 review 的行为配置。

``ReviewConfig`` 只描述“会影响输出”的配置，不负责构造客户端或执行工作流。
这样做有两个目的：

- ``ReviewPipeline`` 不再把 30+ 个参数散落在构造函数里；
- review cache key 可以从同一份配置生成，避免“改了 batch/context/工具配置
  但缓存 key 没变”导致误命中。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

REVIEWER_MODES = frozenset({"single", "multi_aspect"})
LLM_CACHE_MODES = frozenset({"auto", "record", "replay"})
REVIEW_CACHE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ReviewConfig:
    """一次 review 的不可变配置。"""

    mock_llm: bool = False

    # 凭证与模型连接
    github_token: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_timeout: float = 60.0
    llm_max_retries: int = 0
    temperature: float = 0.0
    seed: Optional[int] = None

    # batching 与 prompt 预算
    max_batch_tokens: int = 60000
    max_patch_tokens: int = 48000
    max_context_tokens: int = 16000

    # reviewer / 并发
    reviewer_mode: str = "single"
    max_workers: int = 4
    aspect_timeout: float = 90.0

    # 检索与证据增强
    hybrid_retrieval_enabled: bool = True
    graph_retrieval_enabled: bool = True
    index_cache_dir: Optional[str] = None
    triage_enabled: bool = True
    impact_enabled: bool = True
    static_analysis_enabled: bool = False
    static_tools: Sequence[str] = ("ruff",)
    verifier_enabled: bool = True
    verify_severities: Sequence[str] = ("critical", "high")

    # 预算
    budget_calls: int = 0
    budget_tokens: int = 0
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
        normalized_mode = self.reviewer_mode.strip().lower().replace("-", "_")
        if normalized_mode not in REVIEWER_MODES:
            raise ValueError(
                "reviewer_mode must be 'single' or 'multi_aspect', "
                f"got {self.reviewer_mode!r}"
            )
        object.__setattr__(self, "reviewer_mode", normalized_mode)

        normalized_cache_mode = self.llm_cache_mode.strip().lower()
        if normalized_cache_mode not in LLM_CACHE_MODES:
            raise ValueError(
                "llm_cache_mode must be 'auto', 'record' or 'replay', "
                f"got {self.llm_cache_mode!r}"
            )
        object.__setattr__(self, "llm_cache_mode", normalized_cache_mode)

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

    def cache_key_parts(self) -> List[str]:
        """所有会影响 review 输出的配置。

        模型名 / temperature / seed 由调用方从实际客户端读取，因为测试或
        自定义 reviewer 可能替换 ``llm_client``；这里只负责其余配置。
        """
        return [
            f"schema={self.cache_schema_version}",
            f"reviewer={self.reviewer_mode}",
            f"triage={int(self.triage_enabled)}",
            f"impact={int(self.impact_enabled)}",
            f"static={int(self.static_analysis_enabled)}",
            f"static_tools={','.join(self.static_tools)}",
            f"verifier={int(self.verifier_enabled)}",
            f"verify_severities={','.join(self.verify_severities)}",
            f"hybrid={int(self.hybrid_retrieval_enabled)}",
            f"graph={int(self.graph_retrieval_enabled)}",
            f"max_batch_tokens={self.max_batch_tokens}",
            f"max_patch_tokens={self.max_patch_tokens}",
            f"max_context_tokens={self.max_context_tokens}",
            f"max_workers={self.max_workers}",
            f"aspect_timeout={self.aspect_timeout}",
            f"llm_timeout={self.llm_timeout}",
            f"llm_max_retries={self.llm_max_retries}",
            f"mock={int(self.mock_llm)}",
        ]
