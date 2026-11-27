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


def test_base_revision_context_ref_is_rejected():
    """base 旧代码只能解释删除内容，不能作为当前 finding 的 context_ref。"""
    from openreview.retriever.code_context import CodeContext, ContextPriority

    base = CodeContext(
        file_path="src/auth.py",
        reason="base definition",
        snippet='password = "old"',
        score=0.8,
        revision="base",
        kind="base_definition",
        priority=ContextPriority.DEFINITION,
        start_line=1,
        end_line=1,
    )
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.7,
        reasoning="Hardcoded password.",
        suggestion="Use env vars.",
        line=1,
        evidence='password = "123"',
        context_refs=[base.ensure_context_id()],
    )
    validator = FindingValidator()

    result = validator.validate(
        _result([issue]), _diff_with_patch(), contexts=[base]
    )

    assert result.potential_issues[0].context_refs == []
    assert validator.last_stats["base_context_ref"] == 1
    assert validator.last_stats["invalid_context_ref"] == 0


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


def test_validate_invalid_line_is_dropped_instead_of_becoming_file_level():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.9,
        reasoning="Hardcoded password.",
        suggestion="Use env vars.",
        line=99,
    )

    validator = FindingValidator()
    result = validator.validate(_result([issue]), _diff_with_patch())

    # 幻觉行号不能降级成文件级问题继续留在报告里，否则会把证据读取
    # 退化成文件头部，削弱 grounding 的意义。
    assert result.potential_issues == []
    assert validator.last_stats["invalid_line"] == 1
    assert result.risk_level == "LOW"


def test_validate_relocates_nearby_invalid_line_from_unique_evidence():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.7,
        reasoning="Hardcoded password.",
        suggestion="Use env vars.",
        line=3,
        end_line=3,
        evidence='password = "123"',
    )
    validator = FindingValidator()

    result = validator.validate(_result([issue]), _diff_with_patch())

    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].line == 1
    assert result.potential_issues[0].end_line == 1
    assert validator.last_stats["line_relocated"] == 1
    assert validator.last_stats["invalid_line"] == 0


def test_validate_does_not_relocate_ambiguous_evidence():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.9,
        reasoning="Ambiguous evidence.",
        suggestion="Check it.",
        line=4,
        evidence="return password",
    )
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/auth.py",
                status="modified",
                patch=(
                    "@@ -0,0 +1,3 @@\n"
                    "+return password\n"
                    "+password = '123'\n"
                    "+return password\n"
                ),
                visible_lines=[1, 2, 3],
            )
        ]
    )
    validator = FindingValidator()

    result = validator.validate(_result([issue]), diff)

    assert result.potential_issues == []
    assert validator.last_stats["line_relocated"] == 0
    assert validator.last_stats["invalid_line"] == 1


def test_validate_drops_pre_existing_issues_by_default():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.9,
        reasoning="Legacy hardcoded password.",
        suggestion="Use env vars.",
        line=1,
        evidence='password = "123"',
        scope="pre_existing",
    )

    validator = FindingValidator()
    result = validator.validate(_result([issue]), _diff_with_patch())

    assert result.potential_issues == []
    assert validator.last_stats["dropped_pre_existing"] == 1


def _diff_with_deleted_line() -> ParsedDiff:
    return ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/auth.py",
                status="modified",
                patch=(
                    "@@ -7,2 +7,2 @@\n"
                    '-    check_password(password)\n'
                    "+    return password\n"
                ),
                visible_lines=[7, 8],
                old_visible_lines=[7, 8],
                deleted_lines=[7],
            )
        ]
    )


def test_validate_accepts_left_side_deleted_lines():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.8,
        reasoning="Deleted line removed a required validation.",
        suggestion="Restore the check.",
        line=7,
        side="LEFT",
        evidence="check_password(password)",
    )

    result = FindingValidator().validate(
        _result([issue]), _diff_with_deleted_line()
    )

    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].side == "LEFT"


def test_validate_rejects_left_side_line_on_right_side_only_patch():
    issue = PotentialIssue(
        file="src/auth.py",
        severity="High",
        category="Security",
        confidence=0.9,
        reasoning="Wrong-side line.",
        suggestion="N/A.",
        line=1,
        side="LEFT",
        evidence='password = "123"',
    )

    result = FindingValidator().validate(_result([issue]), _diff_with_patch())

    assert result.potential_issues == []


def test_validate_extends_end_line_to_cover_multiline_evidence(tmp_path):
    from evaluation.deterministic import evidence_is_grounded

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    source_lines = [
        "def f():",
        "    a = 1",
        "    b = 2",
        "    return a + b",
    ]
    (repo / "src" / "calc.py").write_text(
        "\n".join(source_lines) + "\n", encoding="utf-8"
    )
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/calc.py",
                status="modified",
                patch=(
                    "@@ -1,4 +1,4 @@\n"
                    " def f():\n"
                    "-    a = 0\n"
                    "+    a = 1\n"
                    "     b = 2\n"
                    "     return a + b\n"
                ),
                visible_lines=[1, 2, 3, 4],
            )
        ]
    )
    issue = PotentialIssue(
        file="src/calc.py",
        severity="High",
        category="Bugs",
        confidence=0.9,
        reasoning="a and b are related.",
        suggestion="Check it.",
        line=2,
        evidence="a = 1\nb = 2",
    )

    result = FindingValidator().validate(
        _result([issue]), diff, repo_path=repo
    )

    kept = result.potential_issues[0]
    assert kept.line == 2
    assert kept.end_line == 3
    assert kept.evidence == "    a = 1\n    b = 2"
    assert evidence_is_grounded(
        kept.evidence or "",
        "\n".join(source_lines[kept.line - 1 : kept.end_line]),
    )


def test_validate_canonicalizes_ellipsis_evidence(tmp_path):
    from evaluation.deterministic import evidence_is_grounded

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    source_lines = [
        'log.Printf("first: %s", value)',
        "middle()",
        'log.Printf("second: %s", value)',
    ]
    (repo / "src" / "log.go").write_text(
        "\n".join(source_lines) + "\n", encoding="utf-8"
    )
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/log.go",
                status="modified",
                patch=(
                    "@@ -1,3 +1,3 @@\n"
                    '-log.Printf("first")\n'
                    '+log.Printf("first: %s", value)\n'
                    " middle()\n"
                    '-log.Printf("second")\n'
                    '+log.Printf("second: %s", value)\n'
                ),
                visible_lines=[1, 2, 3],
            )
        ]
    )
    issue = PotentialIssue(
        file="src/log.go",
        severity="Low",
        category="Bugs",
        confidence=0.9,
        reasoning="Both log lines are wrong.",
        suggestion="Remove them.",
        line=1,
        evidence=(
            'log.Printf("first: %s", value)\n'
            "...\n"
            'log.Printf("second: %s", value)'
        ),
    )

    result = FindingValidator().validate(
        _result([issue]), diff, repo_path=repo
    )

    kept = result.potential_issues[0]
    assert kept.evidence in {
        'log.Printf("first: %s", value)',
        'log.Printf("second: %s", value)',
    }
    assert evidence_is_grounded(
        kept.evidence or "",
        "\n".join(
            source_lines[kept.line - 1 : (kept.end_line or kept.line)]
        ),
    )


def test_validate_uses_visible_part_of_partially_visible_evidence(tmp_path):
    from evaluation.deterministic import evidence_is_grounded

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    source_lines = [
        "func render() {",
        "\tfor i := range min(len(states), height-1) {",
        "\t\tfmt.Fprint(w, states[i])",
        "\t}",
        "\tp.pos = len(states)",
    ]
    (repo / "src" / "progress.go").write_text(
        "\n".join(source_lines) + "\n", encoding="utf-8"
    )
    # 只有第一行在 diff 可见行内，其余是未展示的上下文。
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/progress.go",
                status="modified",
                patch=(
                    "@@ -1,5 +1,5 @@\n"
                    " func render() {\n"
                    "-\tfor i := range states {\n"
                    "+\tfor i := range min(len(states), height-1) {\n"
                    " \t\tfmt.Fprint(w, states[i])\n"
                    " \t}\n"
                    " \tp.pos = len(states)\n"
                ),
                visible_lines=[2],
            )
        ]
    )
    issue = PotentialIssue(
        file="src/progress.go",
        severity="Medium",
        category="Bugs",
        confidence=0.9,
        reasoning="Cursor accounting is wrong.",
        suggestion="Use the rendered count.",
        line=2,
        evidence=(
            "\tfor i := range min(len(states), height-1) {\n"
            "\t\tfmt.Fprint(w, states[i])\n"
            "\t}\n"
            "\n"
            "\tp.pos = len(states)"
        ),
    )

    result = FindingValidator().validate(
        _result([issue]), diff, repo_path=repo
    )

    kept = result.potential_issues[0]
    assert kept.line == 2
    assert kept.end_line is None
    assert kept.evidence == "\tfor i := range min(len(states), height-1) {"
    assert evidence_is_grounded(kept.evidence or "", source_lines[1])


def test_validate_canonicalizes_indentation_to_source_text(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "calc.py").write_text(
        "def f():\n\tvalue = 1\n", encoding="utf-8"
    )
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/calc.py",
                status="modified",
                patch=(
                    "@@ -1,2 +1,2 @@\n"
                    " def f():\n"
                    "-\tvalue = 0\n"
                    "+\tvalue = 1\n"
                ),
                visible_lines=[1, 2],
            )
        ]
    )
    issue = PotentialIssue(
        file="src/calc.py",
        severity="Medium",
        category="Bugs",
        confidence=0.9,
        reasoning="Value changed.",
        suggestion="Check it.",
        line=2,
        evidence="    value = 1",
    )

    result = FindingValidator().validate(
        _result([issue]), diff, repo_path=repo
    )

    kept = result.potential_issues[0]
    assert kept.evidence == "\tvalue = 1"


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

    assert len(result.potential_issues) == 1
    assert validator.last_stats == {
        "candidates": 3,
        "kept": 1,
        "dropped_unknown_file": 1,
        "dropped_pre_existing": 0,
        "invalid_line": 1,
        "line_relocated": 0,
        "missing_line": 0,
        "missing_evidence": 0,
        "ungrounded_evidence": 0,
        "grounded_evidence": 1,
        "evidence_exact": 1,
        "evidence_line_match": 0,
        "evidence_token_match": 0,
        "invalid_context_ref": 0,
        "base_context_ref": 0,
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
    assert by_reason["Another issue on the same line."].suggested_patch is None
    assert by_reason["Another issue on the same line."].confidence == 0.95
    assert validator.last_stats["patch_checked"] == 2
    assert validator.last_stats["patch_valid"] == 1
    assert validator.last_stats["patch_invalid"] == 1


def test_validate_retries_malformed_suggested_patch_with_recount(tmp_path: Path):
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
    issue = PotentialIssue(
        file="src/auth.py",
        severity="high",
        category="security",
        confidence=0.9,
        reasoning="Hardcoded password.",
        suggestion="Change the password.",
        line=1,
        evidence='password = "123"',
        suggested_patch=(
            '@@ -1 +1,2 @@\n-password = "123"\n+password = "456"\n'
        ),
    )

    result = FindingValidator().validate(
        _result([issue]), diff, repo_path=root
    )

    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].suggested_patch_valid is True
    assert result.potential_issues[0].suggested_patch is not None


def test_validate_redacts_secrets_but_still_grounds_evidence():
    """模型引用被 mask 的密钥行时，证据匹配必须使用同一脱敏视图。"""
    from openreview.repo.diff_parser import DiffParser

    diff = DiffParser.parse_diff(
        [
            {
                "filename": "src/config.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "patch": (
                    "@@ -0,0 +1,1 @@\n"
                    "+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
                ),
            }
        ]
    )
    issue = PotentialIssue(
        file="src/config.py",
        severity="critical",
        category="security",
        confidence=0.9,
        reasoning="Leaked key AKIAIOSFODNN7EXAMPLE",
        suggestion="Revoke AKIAIOSFODNN7EXAMPLE",
        line=1,
        evidence="AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'",
    )

    validator = FindingValidator()
    result = validator.validate(_result([issue]), diff)

    assert len(result.potential_issues) == 1
    kept = result.potential_issues[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in kept.reasoning
    assert "AKIAIOSFODNN7EXAMPLE" not in kept.suggestion
    assert "AKIAIOSFODNN7EXAMPLE" not in kept.evidence
    assert validator.last_stats["grounded_evidence"] == 1


def test_validate_redaction_preserves_missing_optional_fields():
    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/auth.py",
                status="modified",
                visible_lines=[1],
            )
        ]
    )
    issue = PotentialIssue(
        file="src/auth.py",
        severity="medium",
        category="clean_code",
        confidence=0.9,
        reasoning="Naming issue.",
        suggestion="Rename.",
        line=1,
    )

    result = FindingValidator().validate(_result([issue]), diff)

    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].evidence is None
    assert result.potential_issues[0].suggested_patch is None
