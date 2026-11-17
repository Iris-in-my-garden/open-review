"""P5：提取被当前方法引用的类/文件级字段定义。

只看“方法里真的用到了哪些全局字段”，避免把整个模块的常量表塞进 prompt。
Python 用 ``ast`` 精确区分局部变量、参数和真正的模块/类属性；Go 暂不启用。
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass
from typing import List, Set

_BUILTIN_NAMES = set(dir(builtins))


@dataclass(frozen=True)
class FieldDefinition:
    name: str
    start_line: int
    end_line: int
    source: str


def collect_referenced_fields(
    text: str,
    suffix: str,
    start_line: int,
    end_line: int,
) -> List[FieldDefinition]:
    """返回被 ``start_line``–``end_line`` 方法引用的模块/类级字段。"""
    if suffix != ".py" or not text:
        return []

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    function = _find_function(tree, start_line, end_line)
    if function is None:
        return []

    enclosing_class = _find_enclosing_class(tree, start_line, end_line)
    class_name = enclosing_class.name if enclosing_class is not None else ""

    references = _collect_references(function, class_name)
    if not references:
        return []

    lines = text.splitlines()
    definitions: List[FieldDefinition] = []

    # 类字段优先：self.x / Cls.x 通常比模块常量更贴近当前方法。
    if enclosing_class is not None:
        for statement in enclosing_class.body:
            for name in _assigned_names(statement):
                if name in references:
                    definitions.append(
                        _to_definition(name, statement, lines)
                    )

    for statement in tree.body:
        for name in _assigned_names(statement):
            if name in references:
                definitions.append(_to_definition(name, statement, lines))

    # 同名只保留第一条（类字段优先于模块字段）。
    seen: Set[str] = set()
    result: List[FieldDefinition] = []
    for item in definitions:
        if item.name in seen:
            continue
        seen.add(item.name)
        result.append(item)
    return result


def _find_function(
    tree: ast.AST, start_line: int, end_line: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    best_span: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        node_start = node.lineno
        node_end = getattr(node, "end_lineno", node_start)
        if node_start <= start_line and end_line <= node_end:
            span = node_end - node_start
            if best_span is None or span < best_span:
                best = node
                best_span = span
    return best


def _find_enclosing_class(
    tree: ast.AST, start_line: int, end_line: int
) -> ast.ClassDef | None:
    best: ast.ClassDef | None = None
    best_span: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        node_start = node.lineno
        node_end = getattr(node, "end_lineno", node_start)
        if node_start <= start_line and end_line <= node_end:
            span = node_end - node_start
            if best_span is None or span < best_span:
                best = node
                best_span = span
    return best


def _collect_references(
    function: ast.FunctionDef | ast.AsyncFunctionDef, class_name: str
) -> Set[str]:
    local_names = _local_names(function)
    references: Set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id not in local_names:
                references.add(node.id)
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            root: ast.AST = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and (
                root.id == "self" or (class_name and root.id == class_name)
            ):
                references.add(node.attr)
            self.generic_visit(node)

    Visitor().visit(function)
    references.difference_update(_BUILTIN_NAMES)
    return references


def _local_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Set[str]:
    names: Set[str] = set()

    args = function.args
    for arg in (
        list(args.args)
        + list(args.kwonlyargs)
        + list(getattr(args, "posonlyargs", []))
    ):
        names.add(arg.arg)
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)

    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not function:
            names.add(node.name)
        elif isinstance(node, ast.ClassDef) and node is not function:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _assigned_names(statement: ast.AST) -> List[str]:
    if isinstance(statement, ast.Assign):
        names: List[str] = []
        for target in statement.targets:
            names.extend(_target_names(target))
        return names
    if isinstance(statement, ast.AnnAssign):
        return _target_names(statement.target)
    return []


def _target_names(target: ast.AST) -> List[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: List[str] = []
        for element in target.elts:
            names.extend(_target_names(element))
        return names
    return []


def _to_definition(
    name: str, statement: ast.stmt, lines: List[str]
) -> FieldDefinition:
    start = statement.lineno
    end = getattr(statement, "end_lineno", start)
    return FieldDefinition(
        name=name,
        start_line=start,
        end_line=end,
        source="\n".join(lines[start - 1 : end]),
    )
