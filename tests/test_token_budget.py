from openreview.repo.diff_parser import DiffParser, ParsedChangedFile, ParsedDiff
from openreview.review.token_budget import (
    TRUNCATION_MARKER,
    context_reserve_tokens_for,
    estimate_tokens,
    file_patch_cost,
    plan_review_batches,
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


def _multi_hunk_file(
    name: str,
    hunk_count: int,
    lines_per_hunk: int,
) -> ParsedChangedFile:
    parts = []
    new_line = 1
    for hunk_index in range(hunk_count):
        parts.append(
            f"@@ -{new_line},{lines_per_hunk} "
            f"+{new_line},{lines_per_hunk} @@\n"
        )
        for line_index in range(lines_per_hunk):
            parts.append(
                f"+line_{hunk_index}_{line_index} = {line_index}\n"
            )
        new_line += lines_per_hunk
    return ParsedChangedFile(
        filename=name,
        status="modified",
        patch="".join(parts),
        additions=hunk_count * lines_per_hunk,
    )


def test_estimate_tokens_is_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 2
    assert estimate_tokens("a" * 300) > estimate_tokens("a" * 30)


def test_estimate_tokens_does_not_underestimate_cjk():
    """CJK 约 1-1.5 字符/token，旧的 len/3 会低估 2-3 倍并突破硬预算。"""
    chinese = "这是一个用于测试中文 token 估算的句子" * 10
    ascii_text = "a" * len(chinese)

    assert estimate_tokens(chinese) > estimate_tokens(ascii_text) * 1.5



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


def test_split_patch_into_hunks_preserves_each_hunk():
    file = _multi_hunk_file("src/a.py", hunk_count=3, lines_per_hunk=2)

    hunks = DiffParser.split_patch_into_hunks(file.patch)

    assert len(hunks) == 3
    assert all(hunk.startswith("@@") for hunk in hunks)
    assert "".join(hunks) == file.patch


def test_slice_changed_file_recomputes_line_numbers():
    file = _multi_hunk_file("src/a.py", hunk_count=3, lines_per_hunk=2)
    hunks = DiffParser.split_patch_into_hunks(file.patch)

    sliced = DiffParser.slice_changed_file(file, [hunks[1]])

    assert sliced.filename == "src/a.py"
    assert sliced.additions == 2
    assert sliced.changed_lines == [3, 4]
    assert sliced.visible_lines == [3, 4]


def test_context_reserve_scales_with_soft_budget():
    assert context_reserve_tokens_for(10_000) == 2_500
    assert context_reserve_tokens_for(4_000) == 2_000


def test_plan_review_batches_keeps_small_diff_in_one_batch():
    diff = ParsedDiff(
        files=[_file("src/a.py", 10), _file("src/b.py", 10)]
    )

    batches = plan_review_batches(diff)

    assert len(batches) == 1
    assert [file.filename for file in batches[0].files] == [
        "src/a.py",
        "src/b.py",
    ]


def test_plan_review_batches_splits_only_above_hard_cap():
    diff = ParsedDiff(
        files=[
            _multi_hunk_file(
                "src/a.py", hunk_count=8, lines_per_hunk=20
            )
        ]
    )
    total = sum(file_patch_cost(file) for file in diff.files)

    one = plan_review_batches(
        diff,
        soft_prompt_tokens=total,
        hard_prompt_tokens=total + 1,
        prompt_overhead_tokens=0,
        context_reserve_tokens=0,
    )
    many = plan_review_batches(
        diff,
        soft_prompt_tokens=total // 2,
        hard_prompt_tokens=total // 2,
        prompt_overhead_tokens=0,
        context_reserve_tokens=0,
    )

    assert len(one) == 1
    assert len(many) > 1


def test_plan_review_batches_preserves_all_hunks():
    file = _multi_hunk_file("src/a.py", hunk_count=6, lines_per_hunk=20)
    diff = ParsedDiff(files=[file])
    expected_headers = [
        hunk.splitlines()[0]
        for hunk in DiffParser.split_patch_into_hunks(file.patch)
    ]

    batches = plan_review_batches(
        diff,
        soft_prompt_tokens=300,
        hard_prompt_tokens=400,
        prompt_overhead_tokens=0,
        context_reserve_tokens=0,
    )

    assert len(batches) > 1
    actual_headers = [
        hunk.splitlines()[0]
        for batch in batches
        for file_diff in batch.files
        for hunk in DiffParser.split_patch_into_hunks(file_diff.patch)
    ]
    assert actual_headers == expected_headers


def test_dependency_ordering_keeps_related_files_together():
    diff = ParsedDiff(
        files=[
            _file("src/a.py", 10),
            _file("src/b.py", 10),
            _file("src/c.py", 10),
            _file("src/d.py", 10),
        ]
    )
    total = sum(file_patch_cost(file) for file in diff.files)

    batches = plan_review_batches(
        diff,
        soft_prompt_tokens=total // 2,
        hard_prompt_tokens=total // 2,
        prompt_overhead_tokens=0,
        context_reserve_tokens=0,
        dependencies={"src/a.py": {"src/d.py"}},
    )

    groups = [{file.filename for file in batch.files} for batch in batches]
    assert any({"src/a.py", "src/d.py"} <= group for group in groups)
    # 所有文件仍然只出现一次。
    flattened = [
        file.filename for batch in batches for file in batch.files
    ]
    assert sorted(flattened) == ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]


def test_plan_review_batches_respects_legacy_max_batch_tokens():
    diff = ParsedDiff(
        files=[_file("src/a.py", 2), _file("src/b.py", 2)]
    )

    batches = plan_review_batches(
        diff,
        soft_prompt_tokens=10_000,
        hard_prompt_tokens=14_000,
        max_batch_tokens=1,
    )

    assert len(batches) == 2
    assert [
        file.filename for batch in batches for file in batch.files
    ] == ["src/a.py", "src/b.py"]


def test_plan_review_batches_merges_same_file_units_in_one_batch():
    # 一个文件被切成多个 hunk unit；同一批里的 unit 必须合回一个文件，
    # 否则 validator 的 {filename: file} 索引会丢掉前面 unit 的可见行。
    file = _multi_hunk_file("src/a.py", hunk_count=10, lines_per_hunk=20)
    diff = ParsedDiff(files=[file])

    batches = plan_review_batches(
        diff,
        soft_prompt_tokens=600,
        hard_prompt_tokens=700,
        prompt_overhead_tokens=0,
        context_reserve_tokens=0,
    )

    assert len(batches) > 1
    for batch in batches:
        names = [file_diff.filename for file_diff in batch.files]
        assert names == ["src/a.py"]
        assert batch.files[0].visible_lines

    # 所有可见行仍然覆盖原文件，没有因为合并而丢 hunk。
    original_lines = set(DiffParser.extract_visible_lines(file.patch))
    batched_lines = {
        line
        for batch in batches
        for file_diff in batch.files
        for line in file_diff.visible_lines
    }
    assert batched_lines == original_lines
