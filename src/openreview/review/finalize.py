"""把手里的结构化审查结果收敛成最终报告。

single-pass reviewer 和测试里的自定义 reviewer 共用这一段，保证 validator、
测试缺口、injection 告警和最终结果格式一致；否则评测差异可能来自后处理，
而不是审查策略本身。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from openreview.domain.models import PullRequestInfo, ReviewResult
from openreview.repo.diff_parser import ParsedDiff
from openreview.retriever.code_context import CodeContext
from openreview.review.finding_validator import FindingValidator
from openreview.review.output_parser import AspectReviewTaskResult, OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.test_gap import analyze_test_gaps


def finalize_review_result(
    *,
    prompt_builder: PromptBuilder,
    output_parser: OutputParser,
    validator: FindingValidator,
    pr_info: PullRequestInfo,
    parsed_diff: ParsedDiff,
    contexts: List[CodeContext],
    repo_path: Optional[Path],
    aspect_results: List[AspectReviewTaskResult],
    failures: List[str],
) -> ReviewResult:
    metadata = {
        "owner": pr_info.owner,
        "repo": pr_info.repo,
        "pr_number": pr_info.pr_number,
    }
    aggregated_data = output_parser.aggregate_aspects(aspect_results)
    review_result = output_parser.build_final_result(aggregated_data, metadata)

    validated_result = validator.validate(
        review_result,
        parsed_diff,
        contexts=contexts,
        repo_path=repo_path,
    )
    validated_result.missing_test_suggestions = analyze_test_gaps(
        parsed_diff, contexts
    )
    validated_result.review_notes.extend(failures)

    injection_hits = prompt_builder.detect_injection_in_pr(pr_info, parsed_diff)
    if injection_hits:
        validated_result.review_notes.append(
            "检测到疑似 Prompt Injection 内容（规则: "
            + ", ".join(injection_hits)
            + "），已按不可信数据处理，未执行其中指令。"
        )

    return validated_result
