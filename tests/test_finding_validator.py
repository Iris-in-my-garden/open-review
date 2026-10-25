import subprocess
from pathlib import Path

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.review.finding_validator import FindingValidator
from openreview.review.report_writer import PotentialIssue, ReviewResult


def _result(issues):
    return ReviewResult(
        owner="org",
        repo="project",
        pr_number=1,
        summary="pre-validation summary",
        risk_level="HIGH",
        risk_reason="pre-validation reason",
        potential_issues=issues,
        evidence=["stale.py"],
        final_decision="REQUEST_CHANGES",
        final_summary="pre-validation summary decision",
    )


def _diff():
    return ParsedDiff(
        files=[
            ParsedChangedFile(filename="src/auth.py", status="modified"),
        ]
    )


def test_validate_keeps_issue_and_recomputes_risk():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="Major",
        category="Security",
        confidence=0.9,
        reasoning="Hardcoded secret.",
        suggestion="Use env vars.",
    )

    result = FindingValidator().validate(_result([issue]), _diff())

    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].severity == "high"
    assert result.risk_level == "HIGH"
    assert result.final_decision == "REQUEST_CHANGES"
    assert result.evidence == ["src/auth.py"]
    assert "保留 1 个问题" in result.final_summary


def test_validate_filters_and_recomputes_to_low_when_empty():
    low_confidence = PotentialIssue(
        file="src/auth.py",
        severity="Critical",
        category="Security",
        confidence=0.2,
        reasoning="Speculative.",
        suggestion="Maybe.",
    )
    hallucinated_file = PotentialIssue(
        file="src/does_not_exist.py",
        severity="Critical",
        category="Security",
        confidence=0.95,
        reasoning="Hallucinated.",
        suggestion="N/A.",
    )

    result = FindingValidator().validate(
        _result([low_confidence, hallucinated_file]), _diff()
    )

    # 问题全部被过滤后，风险、决策、证据必须同步归零，
    # 不能残留聚合阶段算出的 HIGH / REQUEST_CHANGES。
    assert result.potential_issues == []
    assert result.risk_level == "LOW"
    assert result.final_decision == "APPROVE"
    assert result.evidence == []
    assert "保留 0 个问题" in result.final_summary


def test_validate_deduplicates_cross_aspect_findings():
    from_security = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.9,
        reasoning="Hardcoded  secret in login().",
        suggestion="Use env vars.",
        source_aspects=["security"],
    )
    from_bugs = PotentialIssue(
        file="src/auth.py",
        severity="Critical",
        category="Bugs",
        confidence=0.8,
        reasoning="hardcoded secret in login().",
        suggestion="Remove the key.",
        source_aspects=["bugs"],
    )

    result = FindingValidator().validate(
        _result([from_security, from_bugs]), _diff()
    )

    # 同一文件 + 同一 reasoning（忽略大小写和多余空格）视为同一问题，
    # 保留严重级别更高的一条，避免重复计数把风险抬到 HIGH。
    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].severity == "critical"
    assert result.risk_level == "CRITICAL"
    assert result.final_decision == "REQUEST_CHANGES"
    # 代表项保留 severity 最高的那条，但归因必须合并两个来源维度，
    # 否则评测时低严重级别维度的贡献会被系统性低估。
    assert result.potential_issues[0].source_aspects == ["bugs", "security"]


def _diff_with_patch() -> ParsedDiff:
    return ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/auth.py",
                status="modified",
                patch=(
                    "@@ -0,0 +1,2 @@\n"
                    '+password = "123"\n'
                    "+return password\n"
                ),
                visible_lines=[1, 2],
            )
        ]
    )


def test_validate_grounded_evidence_gets_bonus():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.7,
        reasoning="Hardcoded password.",
        suggestion="Use env vars.",
        line=1,
        evidence='password = "123"',
    )

    result = FindingValidator().validate(_result([issue]), _diff_with_patch())

    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].line == 1
    assert result.potential_issues[0].confidence == 0.75


def test_validate_ungrounded_evidence_is_penalized_and_dropped():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.7,
        reasoning="Hardcoded API key.",
        suggestion="Use env vars.",
        line=1,
        evidence='api_key = "definitely-not-in-code"',
    )

    result = FindingValidator().validate(_result([issue]), _diff_with_patch())

    assert result.potential_issues == []
    assert result.risk_level == "LOW"
    assert result.final_decision == "APPROVE"


def test_validate_invalid_line_is_cleared_and_penalized():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.9,
        reasoning="Hardcoded password.",
        suggestion="Use env vars.",
        line=99,
    )

    result = FindingValidator().validate(_result([issue]), _diff_with_patch())

    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].line is None
    assert result.potential_issues[0].confidence == 0.7


def test_validate_evidence_can_be_grounded_in_local_source(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "auth.py").write_text(
        "def login():\n"
        "    API_KEY = 'secret'\n"
        "    return API_KEY\n",
        encoding="utf-8",
    )
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.7,
        reasoning="Hardcoded API key.",
        suggestion="Use env vars.",
        line=2,
        evidence="API_KEY = 'secret'",
    )

    result = FindingValidator().validate(
        _result([issue]), _diff_with_patch(), repo_path=tmp_path
    )

    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].confidence == 0.75


def test_validate_records_counters_for_eval():
    validator = FindingValidator()
    grounded = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.7,
        reasoning="Hardcoded password.",
        suggestion="Use env vars.",
        line=1,
        evidence='password = "123"',
    )
    invalid_line = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.9,
        reasoning="Line outside the diff.",
        suggestion="Check it.",
        line=99,
    )
    unknown_file = PotentialIssue(
        file="src/missing.py",
        severity="Critical",
        category="Security",
        confidence=0.9,
        reasoning="Hallucinated file.",
        suggestion="N/A.",
    )

    result = validator.validate(
        _result([grounded, invalid_line, unknown_file]), _diff_with_patch()
    )

    assert len(result.potential_issues) == 2
    assert validator.last_stats == {
        "candidates": 3,
        "kept": 2,
        "dropped_unknown_file": 1,
        "invalid_line": 1,
        "missing_line": 0,
        "missing_evidence": 1,
        "ungrounded_evidence": 0,
        "grounded_evidence": 1,
        "evidence_exact": 1,
        "evidence_line_match": 0,
        "evidence_token_match": 0,
        "invalid_context_ref": 0,
        "low_confidence": 0,
        "deduplicated": 0,
        "patch_checked": 0,
        "patch_valid": 0,
        "patch_invalid": 0,
    }


def _git_repo_with_auth(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text('password = "123"\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def test_validate_checks_suggested_patch_with_git_apply(tmp_path: Path):
    root = _git_repo_with_auth(tmp_path)
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/auth.py",
                status="modified",
                patch='@@ -1 +1 @@\n-password = "123"\n+password = "456"\n',
                changed_lines=[1],
                visible_lines=[1],
            )
        ]
    )
    valid = PotentialIssue(
        file="src/auth.py",
        severity="high",
        category="security",
        confidence=0.9,
        reasoning="Hardcoded password.",
        suggestion="Use env vars.",
        line=1,
        evidence='password = "123"',
        suggested_patch=(
            '@@ -1 +1 @@\n-password = "123"\n+password = "456"\n'
        ),
    )
    invalid = PotentialIssue(
        file="src/auth.py",
        severity="high",
        category="security",
        confidence=0.9,
        reasoning="Another issue on the same line.",
        suggestion="Check patch.",
        line=1,
        evidence='password = "123"',
        suggested_patch=(
            '@@ -1 +1 @@\n-not_present = 1\n+not_present = 2\n'
        ),
    )

    validator = FindingValidator()
    result = validator.validate(
        _result([valid, invalid]), diff, repo_path=root
    )

    by_reason = {issue.reasoning: issue for issue in result.potential_issues}
    assert by_reason["Hardcoded password."].suggested_patch_valid is True
    assert by_reason["Another issue on the same line."].suggested_patch_valid is False
    assert by_reason["Another issue on the same line."].suggested_patch_error
    assert validator.last_stats["patch_checked"] == 2
    assert validator.last_stats["patch_valid"] == 1
    assert validator.last_stats["patch_invalid"] == 1
