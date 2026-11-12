"""Evidence enhancement pipeline (impact analysis + static analysis)."""

from __future__ import annotations

import time
from typing import List

from openreview.retriever.code_context import CodeContext
from openreview.review.config import ReviewConfig
from openreview.review.workflow import ReviewState


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
    ) -> List[CodeContext]:
        batch_files = {file.filename for file in batch.files}
        # impact 指向的是“变更文件之外”的引用，全部保留；
        # static 信号只取当前批次涉及的文件，避免重复注入。
        extras = list(impact_contexts)
        extras.extend(
            context
            for context in static_contexts
            if context.file_path in batch_files
        )
        return extras
