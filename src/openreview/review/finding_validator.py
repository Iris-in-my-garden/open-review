# openreview/finding_validator.py
import logging
import re
from pathlib import Path
from typing import List, Optional, Sequence

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.assessment import (
    assess_risk,
    decide,
    deduplicate_issues,
    normalize_severity,
    unique_files,
)
from openreview.review.report_writer import PotentialIssue, ReviewResult

logger = logging.getLogger(__name__)

MAX_GROUNDING_FILE_SIZE = 500 * 1024
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")


class FindingValidator:
    """对 LLM 产出做后置校验，并基于“过滤后”的问题重新计算结论。

    校验不只过滤低置信度，还会用真实代码做证据核对（evidence grounding）：
    - 行号必须落在该文件 Patch 的新文件侧可见行内
    - evidence 必须在 Patch / 检索上下文 / 本地源码中能找到
    - 通过证据校验加分，找不到证据减分
    """

    def __init__(
        self,
        min_confidence: float = 0.6,
        invalid_line_penalty: float = 0.15,
        missing_line_penalty: float = 0.10,
        missing_evidence_penalty: float = 0.05,
        ungrounded_evidence_penalty: float = 0.20,
        grounded_evidence_bonus: float = 0.05,
    ):
        self.min_confidence = min_confidence
        self.invalid_line_penalty = invalid_line_penalty
        self.missing_line_penalty = missing_line_penalty
        self.missing_evidence_penalty = missing_evidence_penalty
        self.ungrounded_evidence_penalty = ungrounded_evidence_penalty
        self.grounded_evidence_bonus = grounded_evidence_bonus

    def validate(
        self,
        result: ReviewResult,
        parsed_diff: ParsedDiff,
        contexts: Optional[Sequence[CodeContext]] = None,
        repo_path: Optional[Path] = None,
    ) -> ReviewResult:
        files_by_name = {file.filename: file for file in parsed_diff.files}
        contexts = contexts or []

        filtered_issues: List[PotentialIssue] = []
        for issue in result.potential_issues:
            file_diff = files_by_name.get(issue.file)
            # 规则 1: 防止文件路径幻觉（确保 Issue 指向的文件在 Diff 中存在）
            if file_diff is None:
                continue

            confidence = issue.confidence

            # 规则 2: 行号必须落在 Patch 的新文件侧可见行内
            if issue.line is not None:
                if issue.line not in file_diff.visible_lines:
                    issue.line = None
                    issue.end_line = None
                    confidence -= self.invalid_line_penalty
                elif issue.end_line is not None:
                    if issue.end_line < issue.line:
                        issue.end_line = None
                    elif issue.end_line not in file_diff.visible_lines:
                        issue.end_line = None
            elif file_diff.visible_lines:
                confidence -= self.missing_line_penalty

            # 规则 3: evidence 必须能在真实代码中找到
            grounding = self._check_evidence(
                issue, file_diff, contexts, repo_path
            )
            if grounding is None:
                confidence -= self.missing_evidence_penalty
            elif grounding:
                confidence += self.grounded_evidence_bonus
            else:
                confidence -= self.ungrounded_evidence_penalty
                logger.debug(
                    "Ungrounded evidence for %s:%s: %r",
                    issue.file,
                    issue.line,
                    issue.evidence,
                )

            issue.confidence = round(max(0.0, min(1.0, confidence)), 2)
            # 规则 4: 过滤低置信度
            if issue.confidence < self.min_confidence:
                continue

            issue.severity = normalize_severity(issue.severity)
            filtered_issues.append(issue)

        # 规则过滤会改变问题集合，风险等级 / 决策 / 证据必须同步重算，
        # 否则报告会出现 “HIGH + 8 issues” 但正文 “No issues found” 的矛盾。
        # 同时再做一次跨维度去重，覆盖不经过 OutputParser.aggregate_aspects 的单 Reviewer 路径。
        filtered_issues = deduplicate_issues(filtered_issues)
        result.potential_issues = filtered_issues
        result.risk_level, result.risk_reason = assess_risk(filtered_issues)
        result.final_decision = decide(result.risk_level)
        result.evidence = unique_files(filtered_issues)
        result.final_summary = (
            f"校验后保留 {len(filtered_issues)} 个问题。"
            f"最终决策: {result.final_decision} (风险等级: {result.risk_level})"
        )
        return result

    def _check_evidence(
        self,
        issue: PotentialIssue,
        file_diff: ParsedChangedFile,
        contexts: Sequence[CodeContext],
        repo_path: Optional[Path],
    ) -> Optional[bool]:
        """返回 True=证据可核实，False=证据与代码不符，None=未提供证据。"""
        evidence = (issue.evidence or "").strip()
        if len(evidence) < 6:
            return None

        haystack_parts = [file_diff.patch or ""]
        haystack_parts.extend(
            ctx.snippet for ctx in contexts if ctx.file_path == issue.file
        )

        if repo_path is not None:
            source = self._read_source_for_issue(repo_path, issue)
            if source:
                haystack_parts.append(source)

        haystack = "\n".join(part for part in haystack_parts if part)
        if not haystack:
            return None

        evidence_tokens = set(TOKEN_PATTERN.findall(evidence.lower()))
        if len(evidence_tokens) < 3:
            normalized_evidence = " ".join(evidence.lower().split())
            normalized_haystack = " ".join(haystack.lower().split())
            return normalized_evidence in normalized_haystack

        haystack_tokens = set(TOKEN_PATTERN.findall(haystack.lower()))
        overlap = len(evidence_tokens & haystack_tokens) / len(evidence_tokens)
        return overlap >= 0.8

    @staticmethod
    def _read_source_for_issue(
        repo_path: Path, issue: PotentialIssue
    ) -> Optional[str]:
        repo_path = Path(repo_path).resolve()
        candidate = (repo_path / issue.file).resolve()
        try:
            candidate.relative_to(repo_path)
        except ValueError:
            return None

        try:
            if not candidate.is_file() or candidate.stat().st_size > MAX_GROUNDING_FILE_SIZE:
                return None
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

        if issue.line is None:
            return text

        lines = text.splitlines()
        start = max(0, issue.line - 6)
        end = min(len(lines), issue.line + 5)
        return "\n".join(lines[start:end])
