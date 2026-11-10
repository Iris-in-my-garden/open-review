"""本地 TP/FP/FN 回归夹具。

这些用例不依赖 LLM，直接锁定“什么问题应该进入最终报告”的确定性行为：
- 本次引入且证据可定位 -> 保留
- 历史问题 / 幻觉行号 / 编造证据 -> 丢弃
- 删除行 -> LEFT 侧保留
"""

import json
from pathlib import Path

import pytest

from openreview.repo.diff_parser import DiffParser
from openreview.review.finding_validator import FindingValidator
from openreview.review.report_writer import PotentialIssue, ReviewResult

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "evaluation"
    / "fixtures"
    / "accuracy_cases.json"
)


def _load_cases():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: case["name"])
def test_accuracy_fixture(case):
    parsed = DiffParser.parse_diff(
        [
            {
                "filename": "src/auth.py",
                "status": "modified",
                "patch": case["patch"],
            }
        ]
    )
    issues = [PotentialIssue(**payload) for payload in case["issues"]]
    result = ReviewResult(
        owner="org",
        repo="repo",
        pr_number=1,
        summary="fixture",
        risk_level="HIGH",
        risk_reason="fixture",
        potential_issues=issues,
        final_decision="REQUEST_CHANGES",
        final_summary="fixture",
    )

    validated = FindingValidator().validate(result, parsed)

    assert len(validated.potential_issues) == case["expected_kept"]
