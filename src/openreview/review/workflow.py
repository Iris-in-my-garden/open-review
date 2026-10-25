"""有界工作流的状态、预算与 trace。

这里不引入 LangGraph 之类的重框架：PR Review 的步骤是稳定的，真正需要
动态决策的只有 triage、维度选择、verifier 回炉和失败兜底。用一个轻量
状态对象把预算、耗时和中间结果显式记录下来，既方便测试，也方便后续做
评测与消融。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from openreview.github.client import ChangedFile, PullRequestInfo
from openreview.github.pr_url import GitHubPRRef
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.report_writer import ReviewResult

if TYPE_CHECKING:
    from openreview.repo.local import LocalReviewInput, LocalWorkspace


class BudgetExceeded(RuntimeError):
    """LLM 调用或 token 预算不足时抛出。"""


@dataclass
class ReviewBudget:
    """单次 review 的硬预算。

    0 表示不限制。图工作流在任何新增 LLM 调用前都应该先检查 allow()，
    避免 verifier / retry 把成本放大到不可控。
    """

    max_llm_calls: int = 0
    max_input_tokens: int = 0
    max_seconds: float = 0.0
    used_llm_calls: int = 0
    used_input_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining_calls(self) -> Optional[int]:
        if self.max_llm_calls <= 0:
            return None
        return max(0, self.max_llm_calls - self.used_llm_calls)

    def remaining_tokens(self) -> Optional[int]:
        if self.max_input_tokens <= 0:
            return None
        return max(0, self.max_input_tokens - self.used_input_tokens)

    def allow(self, calls: int = 1, tokens: int = 0) -> bool:
        if self.max_seconds > 0 and self.elapsed() >= self.max_seconds:
            return False
        if (
            self.max_llm_calls > 0
            and self.used_llm_calls + max(0, calls) > self.max_llm_calls
        ):
            return False
        if (
            self.max_input_tokens > 0
            and self.used_input_tokens + max(0, tokens) > self.max_input_tokens
        ):
            return False
        return True

    def consume(self, calls: int = 0, tokens: int = 0) -> None:
        self.used_llm_calls += max(0, calls)
        self.used_input_tokens += max(0, tokens)

    def require(self, calls: int = 1, tokens: int = 0) -> None:
        if not self.allow(calls, tokens):
            raise BudgetExceeded(
                f"Review budget exceeded: calls={self.used_llm_calls}/"
                f"{self.max_llm_calls or 'unlimited'}, "
                f"tokens={self.used_input_tokens}/"
                f"{self.max_input_tokens or 'unlimited'}, "
                f"elapsed={self.elapsed():.1f}s"
            )


@dataclass
class TraceEvent:
    node: str
    elapsed_ms: float
    status: str = "ok"
    detail: str = ""


@dataclass
class ReviewState:
    pr_url: str
    output_dir: Path
    post_comments: bool = False
    budget: ReviewBudget = field(default_factory=ReviewBudget)
    progress_cb: Optional[Callable[[int, str], None]] = None

    # local_mode=True 时跳过 GitHub API，改用 repo_path + base/head SHA。
    local_mode: bool = False
    local_input: Optional["LocalReviewInput"] = None
    local_workspace: Optional["LocalWorkspace"] = None

    pr_ref: Optional[GitHubPRRef] = None
    pr_info: Optional[PullRequestInfo] = None
    raw_files: List[ChangedFile] = field(default_factory=list)
    parsed_diff: Optional[ParsedDiff] = None
    local_repo_path: Optional[Path] = None
    base_path: Optional[Path] = None
    base_error: Optional[str] = None
    batches: List[ParsedDiff] = field(default_factory=list)
    batch_results: List[ReviewResult] = field(default_factory=list)
    all_contexts: List[CodeContext] = field(default_factory=list)

    selected_aspects: List[str] = field(default_factory=list)
    skipped_llm: bool = False
    triage_reason: str = ""

    impact_findings: List[str] = field(default_factory=list)
    static_findings: List[str] = field(default_factory=list)
    impact_count: int = 0
    static_count: int = 0
    validation_stats: Dict[str, int] = field(default_factory=dict)
    retrieval_stats: Dict[str, float] = field(default_factory=dict)
    verifier_stats: Dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hit: bool = False
    cache_key: str = ""

    review_result: Optional[ReviewResult] = None
    report_path: Optional[Path] = None
    notes: List[str] = field(default_factory=list)

    stages: Dict[str, float] = field(default_factory=dict)
    trace: List[TraceEvent] = field(default_factory=list)

    def add_note(self, note: str) -> None:
        if note and note not in self.notes:
            self.notes.append(note)

    def trace_node(self, node: str, elapsed_ms: float, status: str = "ok", detail: str = "") -> None:
        self.trace.append(
            TraceEvent(node=node, elapsed_ms=elapsed_ms, status=status, detail=detail)
        )

    def metrics(self) -> Dict[str, Any]:
        return {
            "stages_seconds": dict(self.stages),
            "trace": [
                {
                    "node": event.node,
                    "elapsed_ms": event.elapsed_ms,
                    "status": event.status,
                    "detail": event.detail,
                }
                for event in self.trace
            ],
            "budget": {
                "max_llm_calls": self.budget.max_llm_calls,
                "used_llm_calls": self.budget.used_llm_calls,
                "max_input_tokens": self.budget.max_input_tokens,
                "used_input_tokens": self.budget.used_input_tokens,
                "elapsed_seconds": round(self.budget.elapsed(), 3),
            },
            "selected_aspects": list(self.selected_aspects),
            "skipped_llm": self.skipped_llm,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "validation_stats": dict(self.validation_stats),
            "retrieval_stats": dict(self.retrieval_stats),
            "verifier_stats": dict(self.verifier_stats),
        }


Node = Callable[[ReviewState], None]


@dataclass
class WorkflowNode:
    name: str
    run: Node
    enabled: Optional[Callable[[ReviewState], bool]] = None


class BoundedWorkflow:
    """顺序执行节点，但允许节点级条件跳过。

    这不是自由 Agent：没有模型自主规划、没有无限循环。所有条件边都由代码
    显式定义，并且共享同一个 ReviewBudget。
    """

    def __init__(self, nodes: List[WorkflowNode], max_steps: int = 32):
        self.nodes = nodes
        self.max_steps = max_steps

    def run(self, state: ReviewState) -> ReviewState:
        if len(self.nodes) > self.max_steps:
            raise RuntimeError(
                f"Workflow has {len(self.nodes)} nodes, exceeds max_steps={self.max_steps}"
            )

        for node in self.nodes:
            if node.enabled is not None and not node.enabled(state):
                state.trace_node(node.name, 0.0, status="skipped")
                continue

            started = time.perf_counter()
            try:
                node.run(state)
            except Exception as exc:
                state.trace_node(
                    node.name,
                    round((time.perf_counter() - started) * 1000, 2),
                    status="error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
                raise
            state.trace_node(
                node.name,
                round((time.perf_counter() - started) * 1000, 2),
            )
        return state
