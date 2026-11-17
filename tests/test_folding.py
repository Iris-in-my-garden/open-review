"""AST 折叠必须压缩长方法，同时永远保留 changed lines。"""

from openreview.retriever.folding import fold_snippet
from openreview.review.token_budget import estimate_tokens


def _long_function(lines: int = 80) -> tuple[str, int]:
    body = [f"    value_{index} = {index}" for index in range(lines)]
    # 变更行放在中间，折叠时不能被吞掉。
    changed_index = lines // 2
    body[changed_index] = "    changed_value = compute()"
    text = "def target():\n" + "\n".join(body) + "\n    return changed_value\n"
    return text, changed_index + 2


def test_fold_snippet_preserves_changed_lines():
    text, changed_line = _long_function()
    total_lines = len(text.splitlines())

    folded, was_folded = fold_snippet(
        text,
        ".py",
        1,
        total_lines,
        [changed_line],
        max_tokens=120,
    )

    assert was_folded is True
    assert "changed_value = compute()" in folded
    assert "lines folded" in folded
    assert estimate_tokens(folded) < estimate_tokens(text)


def test_fold_snippet_keeps_multiple_changed_lines():
    text, changed_line = _long_function(60)
    second_changed = changed_line + 1

    folded, was_folded = fold_snippet(
        text,
        ".py",
        1,
        len(text.splitlines()),
        [changed_line, second_changed],
        max_tokens=150,
    )

    lines = folded.splitlines()
    assert was_folded is True
    assert any("changed_value" in line for line in lines)
    assert len(lines) < len(text.splitlines())


def test_fold_snippet_skips_short_functions():
    text = "def target(x):\n    return x + 1\n"

    folded, was_folded = fold_snippet(text, ".py", 1, 2, [2], max_tokens=100)

    assert was_folded is False
    assert folded == text.rstrip("\n")
