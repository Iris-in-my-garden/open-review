from pathlib import Path

from openreview.review.static_analysis import (
    StaticAnalysisReport,
    StaticAnalysisRunner,
    StaticFinding,
    _as_repo_relative,
    _filter_to_changed_lines,
)


def test_static_report_contexts_are_filtered_by_file(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("bad = 1\n", encoding="utf-8")
    report = StaticAnalysisReport(
        findings=[
            StaticFinding(tool="ruff", file="a.py", line=2, code="F841", message="unused"),
            StaticFinding(tool="ruff", file="b.py", line=1, code="F401", message="import"),
        ]
    )

    contexts = report.to_contexts(tmp_path, filenames={"a.py"})

    assert len(contexts) == 1
    assert contexts[0].file_path == "a.py"
    assert contexts[0].reason == "static:ruff:F841"
    assert "2: y = 2" in contexts[0].snippet


def test_static_report_notes_are_capped(tmp_path: Path):
    report = StaticAnalysisReport(
        findings=[
            StaticFinding(
                tool="ruff",
                file=f"f{index}.py",
                line=1,
                code="F401",
                message="unused import",
            )
            for index in range(12)
        ]
    )

    notes = report.to_notes()

    assert notes[0].startswith("[static]")
    assert any("其余 2 条" in note for note in notes)


def test_runner_with_unknown_tools_is_noop(tmp_path: Path):
    runner = StaticAnalysisRunner(tools=["unknown"])
    report = runner.run(tmp_path, parsed_diff=_empty_diff())

    assert report.findings == []
    assert report.errors == []


def test_as_repo_relative_normalizes_absolute_paths(tmp_path: Path):
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")

    assert _as_repo_relative(tmp_path, str(target)) == "src/a.py"
    assert _as_repo_relative(tmp_path, "src/a.py") == "src/a.py"


def _empty_diff():
    from openreview.repo.diff_parser import ParsedDiff

    return ParsedDiff()


def test_static_findings_are_filtered_to_diff_visible_lines():
    from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff

    parsed = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="a.py",
                status="modified",
                visible_lines=[10, 11, 12],
            )
        ]
    )
    findings = [
        StaticFinding(tool="ruff", file="a.py", line=11, code="F841", message="new"),
        StaticFinding(tool="ruff", file="a.py", line=3, code="F401", message="old"),
    ]

    filtered = _filter_to_changed_lines(findings, parsed)

    assert [item.line for item in filtered] == [11]

