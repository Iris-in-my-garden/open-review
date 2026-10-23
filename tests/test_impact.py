from pathlib import Path

from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.review.impact import ImpactAnalyzer


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_impact_finds_cross_file_references(tmp_path: Path):
    _write(
        tmp_path / "src" / "core.py",
        "def helper(value):\n    return value\n",
    )
    _write(
        tmp_path / "src" / "caller.py",
        "from core import helper\n\nresult = helper(1)\n",
    )

    parsed = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/core.py",
                status="modified",
                patch="@@ -1,2 +1,2 @@\n-def helper(value):\n+def helper(value):\n",
                changed_symbols=["helper"],
            )
        ]
    )

    report = ImpactAnalyzer().analyze(parsed, tmp_path)

    assert report.changed_symbols == ["helper"]
    assert any(item.file == "src/caller.py" for item in report.findings)
    contexts = report.to_contexts(tmp_path)
    assert contexts
    assert "helper" in contexts[0].snippet
    assert contexts[0].reason.startswith("impact:")


def test_impact_skips_common_symbol_names(tmp_path: Path):
    _write(tmp_path / "src" / "a.py", "def run():\n    return 1\n")

    parsed = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/a.py",
                status="modified",
                changed_symbols=["run"],
            )
        ]
    )

    report = ImpactAnalyzer().analyze(parsed, tmp_path)

    assert report.findings == []
    assert report.changed_symbols == []


def test_impact_returns_empty_for_missing_repo(tmp_path: Path):
    parsed = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/a.py",
                status="modified",
                changed_symbols=["calculate_total"],
            )
        ]
    )

    report = ImpactAnalyzer().analyze(parsed, tmp_path / "missing")

    assert report.findings == []
