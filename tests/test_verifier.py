from typing import Type, TypeVar

from pydantic import BaseModel

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.review.report_writer import PotentialIssue, ReviewResult
from openreview.review.verifier import (
    VERIFIER_SYSTEM_PROMPT,
    FindingVerifier,
    VerifierResult,
)

T = TypeVar("T", bound=BaseModel)


class ScriptedVerifierLLM:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.calls = 0

    def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Type[T],
    ) -> T:
        self.calls += 1
        return response_schema(
            results=[
                VerifierResult(
                    issue_id=issue_id,
                    verdict=verdict,
                    confidence=0.9 if verdict == "verified" else 0.5,
                    reason=f"{verdict} reason",
                    counter_argument=(
                        "The surrounding code validates the value."
                        if verdict == "verified"
                        else ""
                    ),
                )
                for issue_id, verdict in self.verdicts.items()
            ]
        )


def test_verifier_prompt_requires_evidence_gate_and_category_rules():
    assert "完整因果链" in VERIFIER_SYSTEM_PROMPT
    assert "test_gap" in VERIFIER_SYSTEM_PROMPT
    assert "clean_code" in VERIFIER_SYSTEM_PROMPT
    assert "环境变量、配置、跨文件调用" in VERIFIER_SYSTEM_PROMPT
    assert "不能只有“可能无风险”" in VERIFIER_SYSTEM_PROMPT
    assert "medium 级别也必须经过同一套证据门槛" in VERIFIER_SYSTEM_PROMPT


def _issue(line: int, severity: str = "high", confidence: float = 0.8) -> PotentialIssue:
    return PotentialIssue(
        file="src/a.py",
        severity=severity,
        category="bugs",
        confidence=confidence,
        reasoning=f"issue at {line}",
        suggestion="fix it",
        line=line,
    )


def _result(issues) -> ReviewResult:
    return ReviewResult(
        owner="org",
        repo="repo",
        pr_number=1,
        summary="summary",
        risk_level="HIGH",
        risk_reason="reason",
        potential_issues=list(issues),
        final_decision="REQUEST_CHANGES",
        final_summary="summary",
    )


def _diff() -> ParsedDiff:
    return ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/a.py",
                status="modified",
                patch="@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n",
                changed_lines=[1, 2],
            )
        ]
    )


def test_verifier_rejects_findings_and_recomputes_risk():
    llm = ScriptedVerifierLLM({"issue-1": "rejected", "issue-2": "verified"})
    verifier = FindingVerifier(llm_client=llm, batch_size=10, max_attempts=1)

    result, stats = verifier.verify(
        _result([_issue(1), _issue(2)]), _diff(), contexts=[]
    )

    assert llm.calls == 1
    assert stats.candidates == 2
    assert stats.rejected == 1
    assert stats.verified == 1
    assert len(result.potential_issues) == 1
    assert result.potential_issues[0].line == 2
    assert result.risk_level == "HIGH"
    assert any("Verifier 拒绝" in note for note in result.review_notes)


def test_verifier_lowers_confidence_when_context_is_insufficient():
    llm = ScriptedVerifierLLM({"issue-1": "needs_more_context"})
    verifier = FindingVerifier(llm_client=llm, batch_size=10, max_attempts=1)

    result, stats = verifier.verify(_result([_issue(1)]), _diff(), contexts=[])

    assert stats.needs_more_context == 1
    assert stats.dropped_low_confidence == 0
    assert result.potential_issues[0].confidence == 0.7


def test_verifier_drops_findings_below_threshold_after_downgrade():
    llm = ScriptedVerifierLLM({"issue-1": "needs_more_context"})
    verifier = FindingVerifier(llm_client=llm, batch_size=10, max_attempts=1)

    result, stats = verifier.verify(
        _result([_issue(1, confidence=0.65)]), _diff(), contexts=[]
    )

    # 0.65 - 0.1 = 0.55 < 0.6，不能在报告里留下低于阈值的 finding。
    assert stats.needs_more_context == 1
    assert stats.dropped_low_confidence == 1
    assert result.potential_issues == []
    assert result.risk_level == "LOW"
    assert any("已丢弃" in note for note in result.review_notes)


class LowConfidenceVerifiedLLM:
    def generate_structured(self, system_prompt, user_prompt, response_schema):
        return response_schema(
            results=[
                VerifierResult(
                    issue_id="issue-1",
                    verdict="verified",
                    confidence=0.2,
                    reason="looks plausible",
                    counter_argument="The surrounding code validates the value.",
                )
            ]
        )


def test_verifier_does_not_accept_low_confidence_verified():
    verifier = FindingVerifier(
        llm_client=LowConfidenceVerifiedLLM(), batch_size=10, max_attempts=1
    )

    result, stats = verifier.verify(_result([_issue(1)]), _diff(), contexts=[])

    assert stats.verified == 0
    assert stats.needs_more_context == 1
    assert stats.downgraded_low_confidence == 1
    assert result.potential_issues[0].confidence == 0.7


def test_verifier_treats_unknown_verdict_as_needs_more_context():
    llm = ScriptedVerifierLLM({"issue-1": "definitely-not-a-verdict"})
    verifier = FindingVerifier(llm_client=llm, batch_size=10, max_attempts=1)

    result, stats = verifier.verify(_result([_issue(1)]), _diff(), contexts=[])

    # 未知 verdict 绝不能默认成 verified；应降级为需要更多上下文。
    assert stats.verified == 0
    assert stats.needs_more_context == 1
    assert result.potential_issues[0].confidence == 0.7


def test_verifier_includes_low_confidence_medium_findings():
    llm = ScriptedVerifierLLM(
        {
            "issue-1": "verified",
            "issue-4": "verified",
        }
    )
    issues = [
        _issue(1, severity="medium", confidence=0.74),
        _issue(2, severity="medium", confidence=0.75),
        _issue(3, severity="low", confidence=0.7),
        _issue(4, severity="critical", confidence=0.9),
    ]

    result, stats = FindingVerifier(llm).verify(_result(issues), _diff(), [])

    assert stats.candidates == 2
    assert stats.verified == 2
    assert len(result.potential_issues) == 4


def test_verifier_requires_counter_argument_for_verified_result(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text(
        "\n".join(f"value_{index} = {index}" for index in range(1, 21)),
        encoding="utf-8",
    )

    class MissingCounterArgumentLLM:
        def __init__(self):
            self.prompts = []

        def generate_structured(self, system_prompt, user_prompt, response_schema):
            self.prompts.append(user_prompt)
            return response_schema(
                results=[
                    VerifierResult(
                        issue_id="issue-1",
                        verdict="verified",
                        confidence=0.9,
                        reason="supported",
                    )
                ]
            )

    llm = MissingCounterArgumentLLM()
    result, stats = FindingVerifier(llm).verify(
        _result([_issue(1)]), _diff(), [], repo_path=tmp_path
    )

    assert stats.missing_counter_arguments == 2
    assert stats.verified == 0
    assert stats.needs_more_context == 1
    assert stats.symbol_searches == 0
    assert "counter_argument 不足" in llm.prompts[1]
    assert "16: " not in llm.prompts[1]
    assert result.potential_issues[0].confidence == 0.7


def test_verifier_prompt_prefers_short_claim_not_reviewer_reasoning():
    issue = _issue(1)
    issue.reasoning = "A concise claim. " + ("Long reviewer chain. " * 30)
    issue.short_claim = "Expired tokens are accepted without signature validation."
    verifier = FindingVerifier(ScriptedVerifierLLM({}))

    prompt = verifier._build_prompt(
        [("issue-1", issue)],
        _diff(),
        [],
        None,
        expanded=False,
    )

    assert (
        '"claim": "Expired tokens are accepted without signature validation."'
        in prompt
    )
    assert '"reasoning"' not in prompt
    assert '"suggestion"' not in prompt
    assert "Long reviewer chain." not in prompt


def test_verifier_retrieves_missing_symbol_before_expanding_source(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("value = 1\n", encoding="utf-8")
    (source_dir / "helper.py").write_text(
        "def load_user(user_id):\n"
        "    return lookup(user_id)\n",
        encoding="utf-8",
    )

    class MissingSymbolLLM:
        def __init__(self):
            self.prompts = []

        def generate_structured(self, system_prompt, user_prompt, response_schema):
            self.prompts.append(user_prompt)
            if len(self.prompts) == 1:
                return response_schema(
                    results=[
                        VerifierResult(
                            issue_id="issue-1",
                            verdict="needs_more_context",
                            confidence=0.5,
                            missing_symbol="load_user",
                        )
                    ]
                )
            return response_schema(
                results=[
                    VerifierResult(
                        issue_id="issue-1",
                        verdict="verified",
                        confidence=0.9,
                        counter_argument=(
                            "The helper receives a validated user identifier."
                        ),
                    )
                ]
            )

    llm = MissingSymbolLLM()
    result, stats = FindingVerifier(llm).verify(
        _result([_issue(1)]), _diff(), [], repo_path=tmp_path
    )

    assert stats.symbol_searches == 1
    assert stats.symbol_contexts == 1
    assert "symbol search: src/helper.py:1" in llm.prompts[1]
    assert "def load_user(user_id):" in llm.prompts[1]
    assert len(result.potential_issues) == 1


def test_verifier_expands_source_when_missing_symbol_has_no_definition(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    source_lines = ["value = 1"] + [f"value_{i} = {i}" for i in range(2, 20)]
    (source_dir / "a.py").write_text(
        "\n".join(source_lines) + "\n", encoding="utf-8"
    )

    class MissingSymbolLLM:
        def __init__(self):
            self.prompts = []

        def generate_structured(self, system_prompt, user_prompt, response_schema):
            self.prompts.append(user_prompt)
            verdict = "needs_more_context" if len(self.prompts) == 1 else "rejected"
            return response_schema(
                results=[
                    VerifierResult(
                        issue_id="issue-1",
                        verdict=verdict,
                        confidence=0.8,
                        missing_symbol="not_defined_anywhere",
                    )
                ]
            )

    llm = MissingSymbolLLM()
    FindingVerifier(llm).verify(
        _result([_issue(1)]), _diff(), [], repo_path=tmp_path
    )

    assert "8: value_8 = 8" not in llm.prompts[0]
    assert "8: value_8 = 8" in llm.prompts[1]


def test_verifier_rejects_unsafe_missing_symbol_names(tmp_path):
    assert FindingVerifier._search_symbol_contexts(
        tmp_path,
        "name; touch /tmp/pwned",
        target_file="src/a.py",
        deadline=10**10,
    ) == []


def test_verifier_symbol_search_ranks_local_source_definitions(tmp_path):
    local = tmp_path / "src" / "feature"
    local.mkdir(parents=True)
    tests = tmp_path / "tests"
    tests.mkdir()
    (local / "helper.py").write_text(
        "def load_user(user_id):\n    return user_id\n",
        encoding="utf-8",
    )
    (tests / "mock.py").write_text(
        "def load_user(user_id):\n    return None\n",
        encoding="utf-8",
    )

    snippets = FindingVerifier._search_symbol_contexts(
        tmp_path,
        "load_user",
        target_file="src/feature/review.py",
        deadline=10**10,
    )

    assert snippets
    assert "src/feature/helper.py" in snippets[0]


def test_verifier_symbol_search_accepts_one_level_qualified_name(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "service.py").write_text(
        "class UserService:\n"
        "    def update(self, user):\n"
        "        return user\n",
        encoding="utf-8",
    )

    snippets = FindingVerifier._search_symbol_contexts(
        tmp_path,
        "UserService.update",
        target_file="src/review.py",
        deadline=10**10,
    )

    assert snippets
    assert "src/service.py" in snippets[0]
    assert "def update(self, user):" in snippets[0]
