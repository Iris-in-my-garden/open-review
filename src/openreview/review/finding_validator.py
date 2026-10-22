# openreview/finding_validator.py
from openreview.repo.diff_parser import ParsedDiff
from openreview.review.assessment import (
    assess_risk,
    decide,
    deduplicate_issues,
    normalize_severity,
    unique_files,
)
from openreview.review.report_writer import ReviewResult


class FindingValidator:
    """对 LLM 产出做后置校验，并基于“过滤后”的问题重新计算结论。"""

    def __init__(self, min_confidence: float = 0.6):
        self.min_confidence = min_confidence

    def validate(self, result: ReviewResult, parsed_diff: ParsedDiff) -> ReviewResult:
        valid_files = {f.filename for f in parsed_diff.files}

        filtered_issues = []
        for issue in result.potential_issues:
            # 规则 1: 过滤低置信度
            if issue.confidence < self.min_confidence:
                continue
            # 规则 2: 防止文件路径幻觉（确保 Issue 指向的文件在 Diff 中存在）
            if issue.file and issue.file not in valid_files:
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
