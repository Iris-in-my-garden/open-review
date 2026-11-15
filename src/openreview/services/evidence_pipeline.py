"""Evidence enhancement pipeline (impact analysis + static analysis)."""

from __future__ import annotations

import time
from typing import List

from openreview.retriever.code_context import CodeContext
from openreview.review.config import ReviewConfig
from openreview.review.workflow import ReviewState

# 一个 batch 最多注入多少条跨文件影响证据；超过这个量只会挤占真正相关
# 的 diff 上下文。
MAX_IMPACT_PER_BATCH = 6
# 无法按符号归属时的保守回退：只让一个 batch 带少量全局 impact。
MAX_GLOBAL_IMPACT_FALLBACK = 3


class EvidencePipeline:
    def __init__(self, config: ReviewConfig):
        self.config = config

    def collect(self, state: ReviewState) -> tuple[List[CodeContext], List[CodeContext]]:
        return self._impact_contexts(state), self._static_contexts(state)

    def _impact_contexts(self, state: ReviewState) -> List[CodeContext]:
        if not self.config.impact_enabled or state.parsed_diff is None:
            return []
        if state.local_repo_path is None:
            return []

        # 延迟 import：impact analyzer 会加载 tree-sitter，仅需要时才付成本。
        from openreview.review.impact import ImpactAnalyzer

        started = time.perf_counter()
        try:
            report = ImpactAnalyzer().analyze(
                state.parsed_diff, state.local_repo_path
            )
            state.impact_findings = report.to_notes()
            state.impact_count = len(report.findings)
            if report.findings:
                state.add_note(
                    f"[impact] 发现 {len(report.findings)} 处跨文件引用，"
                    "已作为上下文注入。"
                )
            return report.to_contexts(state.local_repo_path)
        finally:
            state.stages["impact_analysis"] = round(
                time.perf_counter() - started, 3
            )

    def _static_contexts(self, state: ReviewState) -> List[CodeContext]:
        if not self.config.static_analysis_enabled or state.parsed_diff is None:
            return []
        if state.local_repo_path is None:
            return []

        from openreview.review.static_analysis import StaticAnalysisRunner

        started = time.perf_counter()
        try:
            report = StaticAnalysisRunner(tools=self.config.static_tools).run(
                state.local_repo_path, state.parsed_diff
            )
            state.static_findings = report.to_notes()
            state.static_count = len(report.findings)
            for error in report.errors:
                state.add_note(f"[static] {error}")
            if report.findings:
                state.add_note(
                    f"[static] 发现 {len(report.findings)} 个静态分析信号，"
                    "已作为上下文注入。"
                )
            return report.to_contexts(state.local_repo_path)
        finally:
            state.stages["static_analysis"] = round(
                time.perf_counter() - started, 3
            )

    @staticmethod
    def extra_contexts(
        batch,
        impact_contexts: List[CodeContext],
        static_contexts: List[CodeContext],
        *,
        include_global_impact: bool = False,
        max_impact_per_batch: int = MAX_IMPACT_PER_BATCH,
    ) -> List[CodeContext]:
        """只注入与当前 batch 有关的额外证据。

        impact 上下文指向变更文件之外的引用，以前会被原样注入每个 batch。
        多 batch 时同一段引用会重复占用每个 prompt 的上下文预算，实测一个
        PR 内跨 batch 重复 339 段、413 段被预算省略。现在按“batch 变更符号”
        过滤；只有在没有命中（符号抽取失败等）时才由第一个 batch 带上少量
        全局 impact，避免跨文件证据彻底消失。
        """
        batch_files = {file.filename for file in batch.files}
        batch_symbols: set[str] = set()
        for file in batch.files:
            batch_symbols.update(file.changed_symbols or [])
            batch_symbols.update(getattr(file, "removed_symbols", []) or [])
            batch_symbols.update(file.enclosing_contexts or [])

        relevant = [
            context
            for context in impact_contexts
            if context.symbol in batch_symbols or context.file_path in batch_files
        ]
        if not relevant and include_global_impact:
            relevant = sorted(
                impact_contexts, key=lambda item: item.score, reverse=True
            )[:MAX_GLOBAL_IMPACT_FALLBACK]

        extras = relevant[:max_impact_per_batch]
        # static 信号只取当前批次涉及的文件，避免重复注入。
        extras.extend(
            context
            for context in static_contexts
            if context.file_path in batch_files
        )
        return extras
