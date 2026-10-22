from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.review.token_budget import (
    TRUNCATION_MARKER,
    estimate_tokens,
    split_parsed_diff,
    truncate_to_tokens,
)


def _file(name: str, lines: int) -> ParsedChangedFile:
    patch = "@@ -0,0 +1,%d @@\n" % lines + "\n".join(
        f"+line_{index} = {index}" for index in range(lines)
    )
    return ParsedChangedFile(
        filename=name,
        status="modified",
        patch=patch,
        additions=lines,
    )


def test_estimate_tokens_is_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 2
    assert estimate_tokens("a" * 300) > estimate_tokens("a" * 30)


def test_truncate_to_tokens_adds_marker():
    truncated, changed = truncate_to_tokens("x\n" * 1000, 20)

    assert changed is True
    assert truncated.endswith(TRUNCATION_MARKER)
    assert estimate_tokens(truncated) < 200


def test_split_parsed_diff_keeps_files_whole_and_within_budget():
    diff = ParsedDiff(
        files=[_file(f"src/file_{index}.py", 60) for index in range(5)]
    )

    batches = split_parsed_diff(diff, max_tokens_per_batch=300)

    assert len(batches) > 1
    # 所有文件都保留，且没有文件被拆到两个批次
    flattened = [file.filename for batch in batches for file in batch.files]
    assert flattened == [file.filename for file in diff.files]
    assert len(set(flattened)) == len(flattened)


def test_split_parsed_diff_handles_oversized_single_file():
    diff = ParsedDiff(files=[_file("src/huge.py", 2000), _file("src/small.py", 5)])

    batches = split_parsed_diff(diff, max_tokens_per_batch=100)

    assert len(batches) == 2
    assert [file.filename for file in batches[0].files] == ["src/huge.py"]
    assert [file.filename for file in batches[1].files] == ["src/small.py"]
