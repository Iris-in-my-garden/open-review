# tests/test_report_writer.py
import json
from pathlib import Path

from openreview.review.report_writer import (
    PotentialIssue,
    ReportWriter,
    ReviewResult,
)


def test_write_report_creates_file_and_directory(tmp_path: Path):
    """测试自动创建输出目录并写入符合规范命名的文件"""
    output_dir = tmp_path / "nested" / "reports"

    result = ReviewResult(
        owner="octocat",
        repo="Hello-World",
        pr_number=42,
        summary="Add new greeting feature.",
        risk_level="Low",
        risk_reason="Minor addition with low impact.",
        key_changes=["Added hello() method in main.py"],
        final_decision="Approve",
        final_summary="Looks good to merge.",
    )

    written_path = ReportWriter.write(result, output_dir)

    expected_filename = "octocat_Hello-World_pr_42_review.md"
    assert written_path.name == expected_filename
    assert written_path.exists()
    assert written_path.is_file()


def test_write_report_without_findings(tmp_path: Path):
    """测试当没有 potential_issues 和测试建议时，仍能正确输出完整结构"""
    result = ReviewResult(
        owner="foo",
        repo="bar",
        pr_number=1,
        summary="Refactor internal helper.",
        risk_level="Low",
        risk_reason="Pure internal refactoring.",
        final_decision="Approve",
        final_summary="Clean changes.",
    )

    written_path = ReportWriter.write(result, tmp_path)
    content = written_path.read_text(encoding="utf-8")

    # 校验各主要 Heading 均被保留
    assert "# PR Review Report" in content
    assert "## 1. PR Summary" in content
    assert "Refactor internal helper." in content
    assert "## 2. Risk Level" in content
    assert "- **Risk Level**: Low" in content
    assert "## 4. Potential Issues" in content
    assert "No high-confidence issues found." in content
    assert "## 7. Final Recommendation" in content
    assert "- **Decision**: Approve" in content


def test_write_report_with_findings(tmp_path: Path):
    """测试包含完整的 Potential Issues 和 Evidence 时的格式渲染"""
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.95,
        reasoning="Hardcoded secret key detected.",
        suggestion="Use environment variables instead.",
        line=45,
        evidence="secret = '123456'",
        suggested_patch="-secret = '123456'\n+secret = os.environ['SECRET']",
    )

    result = ReviewResult(
        owner="org",
        repo="project",
        pr_number=100,
        summary="Updated authentication logic.",
        risk_level="High",
        risk_reason="Security sensitive area.",
        key_changes=["Modified token validation in auth.py"],
        potential_issues=[issue],
        missing_test_suggestions=["Add unit tests for missing token cases."],
        evidence=["Line 45 in src/auth.py: secret = '123456'"],
        final_decision="Request Changes",
        final_summary="Please fix the security issue before merging.",
        review_notes=["batch 1/2"],
    )

    written_path = ReportWriter.write(result, tmp_path)
    content = written_path.read_text(encoding="utf-8")

    assert "### Issue 1" in content
    assert "- **file**: `src/auth.py:45`" in content
    assert "- **severity**: high" in content
    assert "- **category**: Security" in content
    assert "- **confidence**: 0.95" in content
    assert "Hardcoded secret key detected." in content
    assert "Use environment variables instead." in content
    assert "- **evidence**: `secret = '123456'`" in content
    assert "-secret = '123456'" in content
    assert "- Add unit tests for missing token cases." in content
    assert "- Line 45 in src/auth.py: secret = '123456'" in content
    assert "- **Decision**: Request Changes" in content
    assert "## 8. Review Notes" in content
    assert "- batch 1/2" in content

def test_severity_is_normalized_to_literal_values():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="HIGH",
        category="Security",
        confidence=0.9,
        reasoning="Issue.",
        suggestion="Fix it.",
    )
    assert issue.severity == "high"

    chinese = PotentialIssue(
        file="src/auth.py",
        severity="严重",
        category="Security",
        confidence=0.9,
        reasoning="Issue.",
        suggestion="Fix it.",
    )
    assert chinese.severity == "critical"


def test_run_metrics_are_rendered(tmp_path: Path):
    result = ReviewResult(
        owner="org",
        repo="project",
        pr_number=1,
        summary="Summary.",
        risk_level="LOW",
        risk_reason="Reason.",
        final_decision="APPROVE",
        final_summary="LGTM.",
        metrics={
            "stages_seconds": {"fetch_pr": 0.25, "retrieve_and_review": 1.5},
            "files": 2,
            "batches": 1,
            "aspects": 4,
            "llm_calls": 4,
            "estimated_patch_tokens": 120,
            "estimated_context_tokens": 80,
            "total_seconds": 1.75,
        },
    )

    content = ReportWriter.render_markdown(result)

    assert "### Run Metrics" in content
    assert "**Stages**: fetch_pr=0.25s, retrieve_and_review=1.50s" in content
    assert "**LLM Calls**: 4" in content
    assert "**Estimated Patch Tokens**: 120" in content


def test_write_json_includes_source_aspects(tmp_path: Path):
    issue = PotentialIssue(
        file="src/auth.py",
        severity="high",
        category="bugs",
        confidence=0.9,
        reasoning="Missing guard.",
        suggestion="Add a guard.",
        source_aspects=["bugs", "security"],
    )
    result = ReviewResult(
        owner="org",
        repo="project",
        pr_number=1,
        summary="Summary.",
        risk_level="HIGH",
        risk_reason="Reason.",
        potential_issues=[issue],
        final_decision="REQUEST_CHANGES",
        final_summary="Fix it.",
    )

    path = ReportWriter.write_json(result, tmp_path / "nested" / "result.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["potential_issues"][0]["source_aspects"] == ["bugs", "security"]
    assert payload["final_decision"] == "REQUEST_CHANGES"
