# openreview/finding_validator.py
from openreview.repo.diff_parser import ParsedDiff
from openreview.review.report_writer import ReviewResult


class FindingValidator:
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
            filtered_issues.append(issue)

        result.potential_issues = filtered_issues
        return result