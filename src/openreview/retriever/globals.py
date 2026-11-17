"""P5：提取被当前方法引用的类/结构体字段与模块常量。

统一使用 Tree-sitter，覆盖 Python 和 Go，避免 Python ast / Go 文本匹配两套
口径。只看“方法里真的用到了哪些字段”，避免把整个模块的常量表塞进 prompt。

- Python：``self.x`` / ``Cls.x`` 以及模块级赋值常量。
- Go：selector 访问的 struct 字段，以及包级 ``const`` / ``var``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Set

from tree_sitter import Node, Tree

from openreview.retriever.ast_utils import (
    find_enclosing_definition,
    node_text,
    parse_text,
)


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
    """返回被 ``start_line``–``end_line`` 方法引用的类/模块级字段。"""
    parsed = parse_text(text, suffix)
    if parsed is None:
        return []
    parsed_text, source, tree = parsed
    return collect_referenced_fields_from_tree(
        parsed_text, source, tree, suffix, start_line, end_line
    )


def collect_referenced_fields_from_tree(
    text: str,
    source: bytes,
    tree: Tree,
    suffix: str,
    start_line: int,
    end_line: int,
) -> List[FieldDefinition]:
    """复用已解析的 tree，避免每个方法重复 parse 同一文件。"""
    if not text:
        return []
    function = find_enclosing_definition(tree, max(0, start_line - 1))
    if function is None:
        return []
    if function.end_point[0] + 1 < end_line:
        return []

    lines = text.splitlines()
    if suffix == ".py":
        candidates = _python_field_candidates(tree, function, source, lines)
    elif suffix == ".go":
        candidates = _go_field_candidates(tree, function, source, lines)
    else:
        return []

    references = _collect_references(function, source, suffix)
    if not references:
        return []

    result: List[FieldDefinition] = []
    seen: Set[str] = set()
    for definition in candidates:
        if definition.name in seen or definition.name not in references:
            continue
        seen.add(definition.name)
        result.append(definition)
    return result


# ----------------------------------------------------------------------
# Python
# ----------------------------------------------------------------------
def _python_field_candidates(
    tree: Tree,
    function: Node,
    source: bytes,
    lines: List[str],
) -> List[FieldDefinition]:
    candidates: List[FieldDefinition] = []

    class_node = _enclosing_class(function)
    if class_node is not None:
        body = class_node.child_by_field_name("body")
        if body is not None:
            for statement in _assignment_statements(body):
                candidates.extend(
                    _statement_fields(statement, source, lines)
                )

    for statement in _assignment_statements(tree.root_node):
        candidates.extend(_statement_fields(statement, source, lines))
    return candidates


def _enclosing_class(node: Node) -> Optional[Node]:
    current = node.parent
    while current is not None:
        if current.type == "class_definition":
            return current
        current = current.parent
    return None


def _assignment_statements(container: Node) -> Iterable[Node]:
    """读取直接子节点里的赋值语句，不递归进方法/嵌套类。"""
    for child in container.children:
        if child.type != "expression_statement":
            continue
        for statement in child.children:
            if statement.type in {
                "assignment",
                "annotated_assignment",
                "augmented_assignment",
            }:
                yield statement


def _statement_fields(
    statement: Node,
    source: bytes,
    lines: List[str],
) -> List[FieldDefinition]:
    target = statement.child_by_field_name("left")
    if target is None:
        return []
    names = _python_target_names(target, source)
    start = statement.start_point[0]
    end = statement.end_point[0]
    snippet = "\n".join(lines[start : end + 1])
    return [
        FieldDefinition(
            name=name,
            start_line=start + 1,
            end_line=end + 1,
            source=snippet,
        )
        for name in names
    ]


def _python_target_names(target: Node, source: bytes) -> List[str]:
    names: List[str] = []
    stack = [target]
    while stack:
        node = stack.pop()
        if node.type == "identifier":
            names.append(node_text(node, source))
            continue
        stack.extend(node.children)
    return names


def _python_local_names(function: Node, source: bytes) -> Set[str]:
    names: Set[str] = set()

    parameters = function.child_by_field_name("parameters")
    if parameters is not None:
        names.update(_identifier_texts(parameters, source))

    stack = [function]
    while stack:
        node = stack.pop()
        if node is not function and node.type in {
            "function_definition",
            "class_definition",
        }:
            name = node.child_by_field_name("name")
            if name is not None:
                names.add(node_text(name, source))
            continue
        if node.type in {
            "assignment",
            "annotated_assignment",
            "augmented_assignment",
            "for_statement",
            "named_expression",
        }:
            target = node.child_by_field_name("left")
            if target is None:
                target = node.child_by_field_name("name")
            if target is not None:
                names.update(_python_target_names(target, source))
        stack.extend(node.children)
    return names


# ----------------------------------------------------------------------
# Go
# ----------------------------------------------------------------------
def _go_field_candidates(
    tree: Tree,
    function: Node,
    source: bytes,
    lines: List[str],
) -> List[FieldDefinition]:
    candidates: List[FieldDefinition] = []

    for type_decl in _walk_type(tree.root_node, "type_declaration"):
        for type_spec in type_decl.children:
            if type_spec.type != "type_spec":
                continue
            struct_type = type_spec.child_by_field_name("type")
            if struct_type is None or struct_type.type != "struct_type":
                continue
            for field in _walk_type(struct_type, "field_declaration"):
                for identifier in field.children:
                    if identifier.type != "field_identifier":
                        continue
                    start = field.start_point[0]
                    end = field.end_point[0]
                    candidates.append(
                        FieldDefinition(
                            name=node_text(identifier, source),
                            start_line=start + 1,
                            end_line=end + 1,
                            source="\n".join(lines[start : end + 1]),
                        )
                    )

    for child in tree.root_node.children:
        if child.type not in {"const_declaration", "var_declaration"}:
            continue
        for spec in child.children:
            if spec.type not in {"const_spec", "var_spec"}:
                continue
            start = spec.start_point[0]
            end = spec.end_point[0]
            snippet = "\n".join(lines[start : end + 1])
            for identifier in spec.children:
                if identifier.type != "identifier":
                    continue
                candidates.append(
                    FieldDefinition(
                        name=node_text(identifier, source),
                        start_line=start + 1,
                        end_line=end + 1,
                        source=snippet,
                    )
                )
    return candidates


def _walk_type(root: Node, node_type: str) -> Iterable[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == node_type:
            yield node
        stack.extend(reversed(node.children))


def _go_local_names(function: Node, source: bytes) -> Set[str]:
    names: Set[str] = set()

    for field_name in ("receiver", "parameters"):
        parameters = function.child_by_field_name(field_name)
        if parameters is not None:
            names.update(_identifier_texts(parameters, source))

    stack = [function]
    while stack:
        node = stack.pop()
        if node is not function and node.type in {
            "function_declaration",
            "method_declaration",
        }:
            continue
        if node.type in {"short_var_declaration", "assignment_statement"}:
            left = node.child_by_field_name("left")
            if left is not None:
                names.update(_identifier_texts(left, source))
        elif node.type in {"var_declaration", "const_declaration"}:
            for spec in node.children:
                if spec.type not in {"var_spec", "const_spec"}:
                    continue
                for child in spec.children:
                    if child.type == "identifier":
                        names.add(node_text(child, source))
        elif node.type == "range_clause":
            left = node.child_by_field_name("left")
            if left is not None:
                names.update(_identifier_texts(left, source))
        stack.extend(node.children)
    return names


# ----------------------------------------------------------------------
# references shared
# ----------------------------------------------------------------------
def _collect_references(function: Node, source: bytes, suffix: str) -> Set[str]:
    if suffix == ".py":
        local_names = _python_local_names(function, source)
    else:
        local_names = _go_local_names(function, source)

    references: Set[str] = set()
    stack = [function]
    while stack:
        node = stack.pop()
        if node.type == "identifier":
            text = node_text(node, source)
            if text not in local_names:
                references.add(text)
        elif node.type in {"attribute", "selector_expression"}:
            target = node.child_by_field_name("attribute") or node.child_by_field_name(
                "field"
            )
            if target is not None:
                references.add(node_text(target, source))
        stack.extend(node.children)
    references.difference_update(local_names)
    return references


def _identifier_texts(node: Node, source: bytes) -> Set[str]:
    return {
        node_text(item, source)
        for item in _walk_type(node, "identifier")
        if item is not node
    }
