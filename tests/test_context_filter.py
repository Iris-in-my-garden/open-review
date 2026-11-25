from openreview.repo.diff_parser import DiffParser
from openreview.retriever.code_context import CodeContext, ContextPriority
from openreview.review.context_filter import (
    filter_redundant_base_contexts,
    is_base_definition_covered_by_deleted_lines,
    is_base_definition_visible_in_patch,
)


def _base_context(start: int, end: int) -> CodeContext:
    return CodeContext(
        file_path="src/utils.py",
        reason="[base revision] removed definition",
        snippet="\n".join(f"line {line}" for line in range(start, end + 1)),
        score=0.8,
        revision="base",
        kind="base_definition",
        priority=ContextPriority.DEFINITION,
        symbol="old_func",
        start_line=start,
        end_line=end,
    )


def _diff(patch: str):
    return DiffParser.parse_diff(
        [
            {
                "filename": "src/utils.py",
                "status": "modified",
                "additions": 0,
                "deletions": 2,
                "patch": patch,
            }
        ]
    )


def test_fully_deleted_definition_is_redundant():
    diff = _diff("@@ -10,2 +9,0 @@\n-def old_func():\n-    return 0\n")
    context = _base_context(10, 11)
    file_diff = diff.files[0]

    assert is_base_definition_covered_by_deleted_lines(context, file_diff) is True

    kept, dropped = filter_redundant_base_contexts(
        [context], diff, fully_visible_files={"src/utils.py"}
    )

    assert kept == []
    assert dropped == [context]


def test_definition_fully_visible_as_context_lines_is_redundant():
    """Base definitions are also redundant when the patch shows them as context."""
    diff = _diff(
        "@@ -10,3 +10,3 @@\n"
        " def old_func():\n"
        "-    return 0\n"
        "+    return 1\n"
    )
    context = _base_context(10, 11)
    file_diff = diff.files[0]

    assert is_base_definition_visible_in_patch(context, file_diff) is True

    kept, dropped = filter_redundant_base_contexts(
        [context], diff, fully_visible_files={"src/utils.py"}
    )

    assert kept == []
    assert dropped == [context]


def test_partially_deleted_definition_is_kept():
    diff = _diff("@@ -11,1 +11,1 @@\n-    return 0\n+    return 1\n")
    context = _base_context(10, 11)

    assert (
        is_base_definition_covered_by_deleted_lines(context, diff.files[0])
        is False
    )

    kept, dropped = filter_redundant_base_contexts(
        [context], diff, fully_visible_files={"src/utils.py"}
    )

    assert kept == [context]
    assert dropped == []


def test_patch_visibility_guard_keeps_context_when_file_is_truncated():
    diff = _diff("@@ -10,2 +9,0 @@\n-def old_func():\n-    return 0\n")
    context = _base_context(10, 11)

    kept, dropped = filter_redundant_base_contexts(
        [context], diff, fully_visible_files=set()
    )

    assert kept == [context]
    assert dropped == []


def test_without_visibility_information_filter_is_a_noop():
    diff = _diff("@@ -10,2 +9,0 @@\n-def old_func():\n-    return 0\n")
    context = _base_context(10, 11)

    kept, dropped = filter_redundant_base_contexts([context], diff)

    assert kept == [context]
    assert dropped == []


def test_head_revision_context_is_never_treated_as_base_definition():
    diff = _diff("@@ -10,2 +9,0 @@\n-def old_func():\n-    return 0\n")
    context = _base_context(10, 11).model_copy(
        update={"revision": "head", "kind": "enclosing"}
    )

    assert (
        is_base_definition_covered_by_deleted_lines(context, diff.files[0])
        is False
    )
