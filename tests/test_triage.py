from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.review.triage import ALL_ASPECTS, TEST_ONLY_ASPECTS, triage


def _file(filename: str, **kwargs) -> ParsedChangedFile:
    options = {
        "filename": filename,
        "status": "modified",
        "patch": "@@ -0,0 +1,1 @@\n+x = 1\n",
    }
    options.update(kwargs)
    return ParsedChangedFile(**options)


def _diff(*files: ParsedChangedFile) -> ParsedDiff:
    return ParsedDiff(files=list(files))


def test_docs_only_pr_skips_llm():
    decision = triage(_diff(_file("README.md", is_docs_file=True)))

    assert decision.skip_llm is True
    assert decision.aspects == []
    assert "文档" in decision.reason


def test_config_only_pr_skips_llm():
    decision = triage(_diff(_file("pyproject.toml", is_config_file=True)))

    assert decision.skip_llm is True
    assert decision.aspects == []


def test_empty_diff_skips_llm():
    decision = triage(_diff())

    assert decision.skip_llm is True
    assert decision.aspects == []


def test_test_only_pr_runs_narrowed_aspects():
    decision = triage(_diff(_file("tests/test_math.py", is_test_file=True)))

    assert decision.skip_llm is False
    assert decision.aspects == TEST_ONLY_ASPECTS


def test_regular_code_runs_all_aspects():
    decision = triage(_diff(_file("src/math.py")))

    assert decision.skip_llm is False
    assert decision.aspects == ALL_ASPECTS
