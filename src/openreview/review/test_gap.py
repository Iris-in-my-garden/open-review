"""基于真实测试文件与检索上下文生成测试缺口建议，替代写死的通用文案。"""

from typing import List, Sequence

from openreview.repo.diff_parser import DiffParser, ParsedDiff
from openreview.retriever.code_context import CodeContext

MAX_TEST_SUGGESTIONS = 6


def _is_test_path(path: str) -> bool:
    return DiffParser.is_test_path(path)


def _is_linked_test(source_file: str, test_path: str) -> bool:
    return DiffParser.is_linked_test(source_file, test_path)


def analyze_test_gaps(
    parsed_diff: ParsedDiff, contexts: Sequence[CodeContext]
) -> List[str]:
    """根据检索到的测试文件判断测试缺口。

    这是一个保守实现：只有当检索上下文里确实没有关联测试文件时，才提示“未发现”；
    如果测试文件存在但没有出现变更符号，则提示补充具体符号的用例。
    """
    test_contexts = [ctx for ctx in contexts if _is_test_path(ctx.file_path)]
    suggestions: List[str] = []

    for file_diff in parsed_diff.files:
        if file_diff.is_test_file or file_diff.is_docs_file or file_diff.is_config_file:
            continue
        if not file_diff.changed_symbols and not file_diff.changed_lines:
            continue

        linked = [
            ctx
            for ctx in test_contexts
            if _is_linked_test(file_diff.filename, ctx.file_path)
        ]

        if not linked:
            suggestions.append(
                f"未发现与 `{file_diff.filename}` 关联的测试文件，"
                "建议补充覆盖本次变更逻辑的单元测试或集成测试。"
            )
            continue

        test_snippets = "\n".join(ctx.snippet for ctx in linked)
        uncovered = [
            symbol
            for symbol in file_diff.changed_symbols
            if symbol not in test_snippets
        ]
        if file_diff.changed_symbols and len(uncovered) == len(file_diff.changed_symbols):
            test_files = ", ".join(sorted({ctx.file_path for ctx in linked}))
            suggestions.append(
                f"`{test_files}` 中未出现变更符号（{', '.join(uncovered)}），"
                f"建议为 `{file_diff.filename}` 的改动补充针对性用例。"
            )

    # 去重并限制条数
    seen = set()
    unique: List[str] = []
    for item in suggestions:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:MAX_TEST_SUGGESTIONS]
