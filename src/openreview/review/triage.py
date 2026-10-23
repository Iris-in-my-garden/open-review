"""规则驱动的 review triage。

图工作流的第一个条件边：不是所有 PR 都值得跑完整的多维度 LLM 审查。
这里只用确定性规则，不调用模型，因此 triage 本身没有成本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from openreview.github.client import PullRequestInfo
from openreview.repo.diff_parser import ParsedDiff
from openreview.review.report_writer import ReviewResult

ALL_ASPECTS = ["clean_code", "bugs", "performance", "security"]
TEST_ONLY_ASPECTS = ["clean_code", "bugs"]

SECURITY_PATH_MARKERS = (
    "auth",
    "security",
    "crypto",
    "permission",
    "acl",
    "token",
    "secret",
    "password",
    "oauth",
    "jwt",
    "session",
)
SECURITY_TEXT_MARKERS = (
    "password",
    "secret",
    "token",
    "auth",
    "permission",
    "sql",
    "exec(",
    "eval(",
    "subprocess",
    "pickle",
)


@dataclass(frozen=True)
class TriageDecision:
    aspects: List[str]
    skip_llm: bool
    reason: str


def triage(parsed_diff: ParsedDiff) -> TriageDecision:
    files = parsed_diff.files
    if not files:
        return TriageDecision([], True, "没有可审查的文件")

    code_files = [
        item
        for item in files
        if not item.is_docs_file and not item.is_config_file
    ]
    if not code_files:
        return TriageDecision([], True, "仅包含文档/配置变更，跳过 LLM 审查")

    if all(item.is_test_file for item in code_files):
        return TriageDecision(
            list(TEST_ONLY_ASPECTS),
            False,
            "仅包含测试变更，关闭 performance/security 维度",
        )

    selected = list(ALL_ASPECTS)
    sensitive = any(
        marker in item.filename.lower()
        for item in code_files
        for marker in SECURITY_PATH_MARKERS
    )
    if not sensitive:
        lowered_patches = "\n".join(item.patch or "" for item in code_files).lower()
        sensitive = any(marker in lowered_patches for marker in SECURITY_TEXT_MARKERS)

    # security 始终保留：漏掉安全问题的代价通常高于一次安全审查调用。
    if sensitive and "security" not in selected:
        selected.append("security")

    return TriageDecision(selected, False, "常规代码变更，运行多维度审查")


def build_skipped_result(
    pr_info: PullRequestInfo,
    parsed_diff: ParsedDiff,
    decision: TriageDecision,
) -> ReviewResult:
    """为 docs-only / 空变更生成一个零 LLM 调用的轻量结果。"""
    return ReviewResult(
        owner=pr_info.owner,
        repo=pr_info.repo,
        pr_number=pr_info.pr_number,
        summary=f"本次 PR 未运行 LLM 审查：{decision.reason}。",
        risk_level="LOW",
        risk_reason=decision.reason,
        key_changes=[
            f"共 {len(parsed_diff.files)} 个文件变更，"
            f"+{parsed_diff.total_additions}/-{parsed_diff.total_deletions}。"
        ],
        potential_issues=[],
        missing_test_suggestions=[],
        evidence=[],
        final_decision="APPROVE",
        final_summary="根据 triage 规则，本次变更不需要 LLM 审查。",
        review_notes=[f"triage: {decision.reason}"],
    )
