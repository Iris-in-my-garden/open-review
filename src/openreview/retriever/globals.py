"""P5：提取被当前方法引用的类/结构体字段与模块常量。

统一使用 Tree-sitter，覆盖 Python 和 Go，避免 Python ast / Go 文本匹配两套
口径。只看“方法里真的用到了哪些字段”，避免把整个模块的常量表塞进 prompt。

- Python：``self.x`` / ``Cls.x`` 以及模块级赋值常量。
- Go：selector 访问的 struct 字段，以及包级 ``const`` / ``var``。

P0/P1 的约束：

- 只在 changed lines 附近的窗口里收集引用，避免整个函数体把无关字段带进来；
- ``func_literal`` 也作为作用域边界，防止匿名函数逃逸到外层测试函数；
- 字段候选带 ``parent_type``，按 ``(parent_type, field_name)`` 匹配；
- 超大包级 var/const 只保留类型签名，避免把测试表等大对象整段注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from tree_sitter import Node, Tree

from openreview.retriever.ast_utils import node_text, parse_text
from openreview.review.token_budget import estimate_tokens

# 字段引用只在 changed lines 周围扫描。P5 是补充证据，宁缺毋滥。
DEFAULT_FIELD_WINDOW_RADIUS = 3
MAX_FIELD_SOURCE_LINES = 15
MAX_FIELD_SOURCE_TOKENS = 150

SCOPE_TYPES = {
    "function_definition",
    "class_definition",
    "function_declaration",
    "method_declaration",
    "type_declaration",
    "func_literal",
}


@dataclass(frozen=True)
class FieldDefinition:
    name: str
    start_line: int
    end_line: int
    source: str
    parent_type: str = ""
    kind: str = "field"


def collect_referenced_fields(
    text: str,
    suffix: str,
    start_line: int,
    end_line: int,
    *,
    changed_lines: Optional[Sequence[int]] = None,
    window_radius: int = DEFAULT_FIELD_WINDOW_RADIUS,
) -> List[FieldDefinition]:
    """返回被 ``start_line``–``end_line`` 方法引用的类/模块级字段。"""
    parsed = parse_text(text, suffix)
    if parsed is None:
        return []
    parsed_text, source, tree = parsed
    return collect_referenced_fields_from_tree(
        parsed_text,
        source,
        tree,
        suffix,
        start_line,
        end_line,
        changed_lines=changed_lines,
        window_radius=window_radius,
    )


def collect_referenced_fields_from_tree(
    text: str,
    source: bytes,
    tree: Tree,
    suffix: str,
    start_line: int,
    end_line: int,
    *,
    changed_lines: Optional[Sequence[int]] = None,
    window_radius: int = DEFAULT_FIELD_WINDOW_RADIUS,
    cache: Optional[Dict[str, Any]] = None,
) -> List[FieldDefinition]:
    """复用已解析的 tree，避免每个方法重复 parse 同一文件。

    ``cache`` 由同一个文件的多个作用域共享，用于复用整棵树的 Go 类型/字段
    扫描结果；不同文件必须使用独立缓存。
    """
    if not text:
        return []
    function = _find_enclosing_scope(tree, max(0, start_line - 1))
    if function is None:
        return []
    if function.end_point[0] + 1 < end_line:
        return []

    lines = text.splitlines()
    if suffix == ".py":
        candidates = _python_field_candidates(tree, function, source, lines)
    elif suffix == ".go":
        candidates = _go_field_candidates(tree, function, source, lines, cache)
    else:
        return []

    scan_ranges = _scan_ranges(function, changed_lines, window_radius)
    references = _collect_scoped_references(
        function, source, suffix, scan_ranges, cache
    )
    if not references:
        return []

    return _match_candidates(candidates, references)


def _find_enclosing_scope(tree: Tree, row: int) -> Optional[Node]:
    """返回包含 row 的最小作用域；匿名函数也算边界。"""
    best: Optional[Node] = None
    stack: List[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.start_point[0] <= row <= node.end_point[0]:
            if node.type in SCOPE_TYPES:
                best = node
            stack.extend(node.children)
    return best


def _scan_ranges(
    scope: Node,
    changed_lines: Optional[Sequence[int]],
    window_radius: int,
) -> List[Tuple[int, int]]:
    """1-based inclusive 扫描窗口；没有 changed_lines 时退回整个作用域。"""
    start = scope.start_point[0] + 1
    end = scope.end_point[0] + 1
    if not changed_lines:
        return [(start, end)]

    radius = max(0, window_radius)
    ranges: List[Tuple[int, int]] = []
    for line in changed_lines:
        lo = max(start, line - radius)
        hi = min(end, line + radius)
        if lo <= hi:
            ranges.append((lo, hi))
    return _merge_ranges(ranges)


def _merge_ranges(ranges: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlaps(node: Node, ranges: Sequence[Tuple[int, int]]) -> bool:
    start = node.start_point[0] + 1
    end = node.end_point[0] + 1
    return any(start <= hi and end >= lo for lo, hi in ranges)


def _match_candidates(
    candidates: Sequence[FieldDefinition],
    references: Dict[str, Set[Optional[str]]],
) -> List[FieldDefinition]:
    by_name: Dict[str, List[FieldDefinition]] = {}
    for candidate in candidates:
        by_name.setdefault(candidate.name, []).append(candidate)

    result: List[FieldDefinition] = []
    seen: Set[Tuple[str, str, int, int]] = set()
    for candidate in candidates:
        key = (
            candidate.parent_type,
            candidate.name,
            candidate.start_line,
            candidate.end_line,
        )
        if key in seen:
            continue
        reference_types = references.get(candidate.name)
        if not reference_types:
            continue
        if not _candidate_matches(
            candidate, reference_types, len(by_name[candidate.name])
        ):
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _candidate_matches(
    candidate: FieldDefinition,
    reference_types: Set[Optional[str]],
    same_name_candidates: int,
) -> bool:
    for parent_type in reference_types:
        if parent_type is None:
            # 类型推断不出来时，只允许包级变量/常量，或文件中唯一同名候选。
            if candidate.parent_type == "" or same_name_candidates == 1:
                return True
        elif parent_type == candidate.parent_type:
            return True
    return False


# ----------------------------------------------------------------------
# References
# ----------------------------------------------------------------------
def _collect_scoped_references(
    scope: Node,
    source: bytes,
    suffix: str,
    scan_ranges: Sequence[Tuple[int, int]],
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Set[Optional[str]]]:
    if suffix == ".py":
        return _python_references(scope, source, scan_ranges)
    if suffix == ".go":
        return _go_references(scope, source, scan_ranges, cache)
    return {}


def _add_reference(
    references: Dict[str, Set[Optional[str]]],
    name: str,
    parent_type: Optional[str],
) -> None:
    if not name or name == "_":
        return
    references.setdefault(name, set()).add(parent_type)


def _python_references(
    function: Node,
    source: bytes,
    scan_ranges: Sequence[Tuple[int, int]],
) -> Dict[str, Set[Optional[str]]]:
    local_names = _python_local_names(function, source)
    class_name = _enclosing_class_name(function, source)
    references: Dict[str, Set[Optional[str]]] = {}

    stack = [function]
    while stack:
        node = stack.pop()
        if not _overlaps(node, scan_ranges):
            continue
        if node is not function and node.type in {
            "function_definition",
            "class_definition",
        }:
            continue
        if node.type == "attribute":
            target = node.child_by_field_name("object")
            attribute = node.child_by_field_name("attribute")
            if target is not None and attribute is not None:
                _add_reference(
                    references,
                    node_text(attribute, source),
                    _python_parent_type(target, source, class_name),
                )
                stack.append(target)
            continue
        if node.type == "identifier":
            name = node_text(node, source)
            if name not in local_names:
                _add_reference(references, name, "")
            continue
        stack.extend(reversed(node.children))
    return references


def _python_parent_type(
    target: Node,
    source: bytes,
    class_name: str,
) -> Optional[str]:
    text = node_text(target, source)
    if text in {"self", "cls"} and class_name:
        return class_name
    if target.type == "identifier" and text[:1].isupper():
        return text
    return None


def _enclosing_class_name(function: Node, source: bytes) -> str:
    current = function.parent
    while current is not None:
        if current.type == "class_definition":
            name = current.child_by_field_name("name")
            return node_text(name, source) if name is not None else ""
        current = current.parent
    return ""


def _go_references(
    function: Node,
    source: bytes,
    scan_ranges: Sequence[Tuple[int, int]],
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Set[Optional[str]]]:
    local_names = _go_local_names(function, source)
    type_env, struct_fields, package_types = _go_type_context(
        function, source, cache
    )
    references: Dict[str, Set[Optional[str]]] = {}

    stack = [function]
    while stack:
        node = stack.pop()
        if not _overlaps(node, scan_ranges):
            continue
        if node is not function and node.type in {
            "function_declaration",
            "method_declaration",
        }:
            continue
        if node.type == "selector_expression":
            field = node.child_by_field_name("field")
            operand = node.child_by_field_name("operand")
            if field is not None and operand is not None:
                parent_type = _go_base_type(
                    _go_expr_type(
                        operand, type_env, struct_fields, package_types, source
                    )
                )
                _add_reference(
                    references,
                    node_text(field, source),
                    parent_type or None,
                )
                stack.append(operand)
            continue
        if node.type == "keyed_element":
            key = node.child_by_field_name("key")
            parent_type = _go_composite_parent_type(node, source)
            key_name = _first_identifier_text(key, source) if key is not None else ""
            if key_name:
                _add_reference(references, key_name, parent_type or None)
            for child in node.children:
                if key is not None and child == key:
                    continue
                stack.append(child)
            continue
        if node.type == "identifier":
            name = node_text(node, source)
            if name not in local_names:
                _add_reference(references, name, "")
            continue
        stack.extend(reversed(node.children))
    return references


def _first_identifier_text(node: Node, source: bytes) -> str:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "identifier":
            return node_text(current, source)
        stack.extend(reversed(current.children))
    return ""


# ----------------------------------------------------------------------
# Python candidates
# ----------------------------------------------------------------------
def _python_field_candidates(
    tree: Tree,
    function: Node,
    source: bytes,
    lines: List[str],
) -> List[FieldDefinition]:
    candidates: List[FieldDefinition] = []

    class_node = _enclosing_class(function)
    class_name = ""
    if class_node is not None:
        name_node = class_node.child_by_field_name("name")
        class_name = node_text(name_node, source) if name_node is not None else ""
        body = class_node.child_by_field_name("body")
        if body is not None:
            for statement in _assignment_statements(body):
                candidates.extend(
                    _statement_fields(
                        statement,
                        source,
                        lines,
                        parent_type=class_name,
                        kind="field",
                    )
                )

    for statement in _assignment_statements(tree.root_node):
        candidates.extend(
            _statement_fields(
                statement,
                source,
                lines,
                parent_type="",
                kind="module",
            )
        )
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
    *,
    parent_type: str,
    kind: str,
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
            parent_type=parent_type,
            kind=kind,
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
# Go candidates
# ----------------------------------------------------------------------
def _go_field_candidates(
    tree: Tree,
    function: Node,
    source: bytes,
    lines: List[str],
    cache: Optional[Dict[str, Any]] = None,
) -> List[FieldDefinition]:
    if cache is not None:
        cached = cache.get("go_field_candidates")
        if cached is not None:
            return cached

    candidates: List[FieldDefinition] = []

    for type_decl in _walk_type(tree.root_node, "type_declaration"):
        for type_spec in type_decl.children:
            if type_spec.type != "type_spec":
                continue
            name_node = type_spec.child_by_field_name("name")
            struct_type = type_spec.child_by_field_name("type")
            if (
                name_node is None
                or struct_type is None
                or struct_type.type != "struct_type"
            ):
                continue
            parent_type = node_text(name_node, source)
            for field in _walk_type(struct_type, "field_declaration"):
                start = field.start_point[0]
                end = field.end_point[0]
                snippet = "\n".join(lines[start : end + 1])
                for identifier in field.children:
                    if identifier.type != "field_identifier":
                        continue
                    candidates.append(
                        FieldDefinition(
                            name=node_text(identifier, source),
                            start_line=start + 1,
                            end_line=end + 1,
                            source=snippet,
                            parent_type=parent_type,
                            kind="field",
                        )
                    )

    for child in tree.root_node.children:
        if child.type not in {"const_declaration", "var_declaration"}:
            continue
        kind = "const" if child.type == "const_declaration" else "var"
        spec_type = "const_spec" if kind == "const" else "var_spec"
        for spec in child.children:
            if spec.type != spec_type:
                continue
            names = [
                node_text(identifier, source)
                for identifier in spec.children
                if identifier.type == "identifier"
            ]
            if not names:
                continue
            start = spec.start_point[0]
            end = spec.end_point[0]
            snippet = _package_decl_source(
                spec, kind, names, source, lines
            )
            for name in names:
                candidates.append(
                    FieldDefinition(
                        name=name,
                        start_line=start + 1,
                        end_line=end + 1,
                        source=snippet,
                        parent_type="",
                        kind=kind,
                    )
                )
    if cache is not None:
        cache["go_field_candidates"] = candidates
    return candidates


def _package_decl_source(
    spec: Node,
    kind: str,
    names: Sequence[str],
    source: bytes,
    lines: List[str],
) -> str:
    start = spec.start_point[0]
    end = spec.end_point[0]
    raw = "\n".join(lines[start : end + 1])
    if (
        end - start + 1 <= MAX_FIELD_SOURCE_LINES
        and estimate_tokens(raw) <= MAX_FIELD_SOURCE_TOKENS
    ):
        return raw

    joined = ", ".join(names)
    type_node = spec.child_by_field_name("type")
    type_text = (
        node_text(type_node, source)
        if type_node is not None
        else _go_decl_initializer_type(spec, source)
    )
    if kind == "const":
        if type_text:
            return f"const {joined} {type_text} // value omitted"
        return f"const {joined} = ... // value omitted"
    if type_text:
        return f"var {joined} {type_text} // initializer omitted"
    return f"var {joined} = ... // initializer omitted"


def _go_decl_initializer_type(spec: Node, source: bytes) -> str:
    value = spec.child_by_field_name("value")
    if value is None:
        return ""
    return _go_first_expr_type_text(value, source)


# ----------------------------------------------------------------------
# Go type inference (best-effort, no full type checker)
# ----------------------------------------------------------------------
def _go_type_context(
    function: Node,
    source: bytes,
    cache: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]], Dict[str, str]]:
    root = function
    while root.parent is not None:
        root = root.parent

    cached_types = cache.get("go_type_context") if cache is not None else None
    if cached_types is None:
        struct_fields: Dict[str, Dict[str, str]] = {}
        package_types: Dict[str, str] = {}

        for type_decl in _walk_type(root, "type_declaration"):
            for type_spec in type_decl.children:
                if type_spec.type != "type_spec":
                    continue
                name_node = type_spec.child_by_field_name("name")
                struct_type = type_spec.child_by_field_name("type")
                if (
                    name_node is None
                    or struct_type is None
                    or struct_type.type != "struct_type"
                ):
                    continue
                fields: Dict[str, str] = {}
                for field in _walk_type(struct_type, "field_declaration"):
                    field_type = field.child_by_field_name("type")
                    if field_type is None:
                        continue
                    raw_type = node_text(field_type, source)
                    for identifier in field.children:
                        if identifier.type == "field_identifier":
                            fields[node_text(identifier, source)] = raw_type
                struct_fields[node_text(name_node, source)] = fields

        for child in root.children:
            if child.type not in {"const_declaration", "var_declaration"}:
                continue
            for spec in child.children:
                if spec.type not in {"const_spec", "var_spec"}:
                    continue
                type_node = spec.child_by_field_name("type")
                raw_type = (
                    node_text(type_node, source)
                    if type_node is not None
                    else _go_decl_initializer_type(spec, source)
                )
                for identifier in spec.children:
                    if identifier.type == "identifier":
                        package_types[node_text(identifier, source)] = raw_type

        if cache is not None:
            cache["go_type_context"] = (struct_fields, package_types)
    else:
        struct_fields, package_types = cached_types

    type_env: Dict[str, str] = {}
    for parameters_field in ("receiver", "parameters"):
        parameters = function.child_by_field_name(parameters_field)
        if parameters is None:
            continue
        for parameter in _walk_type(parameters, "parameter_declaration"):
            type_node = parameter.child_by_field_name("type")
            raw_type = (
                node_text(type_node, source) if type_node is not None else ""
            )
            for identifier in parameter.children:
                if identifier.type == "identifier":
                    type_env[node_text(identifier, source)] = raw_type

    stack = [function]
    while stack:
        node = stack.pop()
        if node is not function and node.type in {
            "function_declaration",
            "method_declaration",
        }:
            continue
        if node.type == "var_declaration":
            for spec in node.children:
                if spec.type != "var_spec":
                    continue
                type_node = spec.child_by_field_name("type")
                raw_type = (
                    node_text(type_node, source)
                    if type_node is not None
                    else _go_decl_initializer_type(spec, source)
                )
                for identifier in spec.children:
                    if identifier.type == "identifier":
                        type_env[node_text(identifier, source)] = raw_type
        elif node.type == "short_var_declaration":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            raw_type = _go_first_expr_type_text(right, source)
            if left is not None:
                for identifier in _walk_type(left, "identifier"):
                    type_env[node_text(identifier, source)] = raw_type
        elif node.type == "range_clause":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            raw_type = (
                _go_expr_type(
                    right, type_env, struct_fields, package_types, source
                )
                if right is not None
                else ""
            )
            element_type = _go_element_type(raw_type) or raw_type
            if left is not None:
                for identifier in _walk_type(left, "identifier"):
                    name = node_text(identifier, source)
                    if name != "_":
                        type_env[name] = element_type
        stack.extend(reversed(node.children))

    return type_env, struct_fields, package_types


def _go_expr_type(
    node: Node,
    type_env: Dict[str, str],
    struct_fields: Dict[str, Dict[str, str]],
    package_types: Dict[str, str],
    source: bytes,
) -> str:
    if node.type == "expression_list":
        for child in node.children:
            if child.is_named:
                return _go_expr_type(
                    child, type_env, struct_fields, package_types, source
                )
        return ""
    if node.type == "identifier":
        text = node_text(node, source)
        return type_env.get(text, "") or package_types.get(text, "")
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        field = node.child_by_field_name("field")
        if operand is not None and field is not None:
            operand_type = _go_expr_type(
                operand, type_env, struct_fields, package_types, source
            )
            base = _go_base_type(operand_type)
            return struct_fields.get(base, {}).get(
                node_text(field, source), ""
            )
        return ""
    if node.type == "composite_literal":
        type_node = node.child_by_field_name("type")
        return node_text(type_node, source) if type_node else ""
    if node.type == "unary_expression":
        text = node_text(node, source)
        unary_child = next(
            (item for item in node.children if item.is_named), None
        )
        if unary_child is None:
            return ""
        child_type = _go_expr_type(
            unary_child, type_env, struct_fields, package_types, source
        )
        return f"*{child_type}" if text.startswith("&") else child_type
    if node.type == "parenthesized_expression":
        parenthesized_child = next(
            (item for item in node.children if item.is_named), None
        )
        return (
            _go_expr_type(
                parenthesized_child,
                type_env,
                struct_fields,
                package_types,
                source,
            )
            if parenthesized_child is not None
            else ""
        )
    if node.type == "call_expression":
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is not None and arguments is not None:
            function_name = node_text(function, source)
            first = next(
                (item for item in arguments.children if item.is_named), None
            )
            if first is not None:
                raw = node_text(first, source)
                if function_name == "make":
                    return raw
                if function_name == "new":
                    return f"*{raw}"
        return ""
    if node.type in {
        "slice_type",
        "array_type",
        "map_type",
        "pointer_type",
        "type_identifier",
        "qualified_type",
    }:
        return node_text(node, source)
    return ""


def _go_first_expr_type_text(node: Optional[Node], source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "expression_list":
        first = next((child for child in node.children if child.is_named), None)
        if first is None:
            return ""
        return _go_first_expr_type_text(first, source)
    if node.type == "composite_literal":
        type_node = node.child_by_field_name("type")
        return node_text(type_node, source) if type_node is not None else ""
    if node.type in {
        "slice_type",
        "array_type",
        "map_type",
        "pointer_type",
        "type_identifier",
        "qualified_type",
    }:
        return node_text(node, source)
    if node.type == "unary_expression":
        text = node_text(node, source)
        if text.startswith("&"):
            child = next((item for item in node.children if item.is_named), None)
            return f"*{_go_first_expr_type_text(child, source)}"
    if node.type == "int_literal":
        return "int"
    if node.type in {"float_literal"}:
        return "float64"
    if node.type in {"string", "raw_string_literal"}:
        return "string"
    if node.type in {"true", "false"}:
        return "bool"
    if node.type == "call_expression":
        return _go_expr_type(node, {}, {}, {}, source)
    return ""


def _go_element_type(raw_type: str) -> str:
    text = raw_type.strip()
    while text.startswith("*"):
        text = text[1:].strip()
    if text.startswith("[]"):
        return text[2:].strip()
    if text.startswith("[") and "]" in text:
        return text.split("]", 1)[1].strip()
    if text.startswith("map["):
        remainder = _drop_bracketed_prefix(text)
        return remainder.strip()
    return ""


def _go_base_type(raw_type: str) -> str:
    text = raw_type.strip()
    while text.startswith("*"):
        text = text[1:].strip()
    if text.startswith("[]"):
        text = text[2:].strip()
    elif text.startswith("[") and "]" in text:
        text = text.split("]", 1)[1].strip()
    elif text.startswith("map["):
        text = _drop_bracketed_prefix(text).strip()
    if text.startswith("map["):
        text = _drop_bracketed_prefix(text).strip()
    return text.split(".")[-1].strip()


def _drop_bracketed_prefix(text: str) -> str:
    if not text.startswith("map["):
        return text
    depth = 0
    for index, char in enumerate(text[4:], start=4):
        if char == "[":
            depth += 1
        elif char == "]":
            if depth == 0:
                return text[index + 1 :]
            depth -= 1
    return text


def _go_composite_parent_type(keyed_element: Node, source: bytes) -> str:
    current = keyed_element.parent
    while current is not None:
        if current.type == "composite_literal":
            type_node = current.child_by_field_name("type")
            if type_node is None:
                return ""
            raw_type = node_text(type_node, source)
            if raw_type.strip().startswith("map["):
                return ""
            element_type = _go_element_type(raw_type) or raw_type
            return _go_base_type(element_type)
        current = current.parent
    return ""


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


def _identifier_texts(node: Node, source: bytes) -> Set[str]:
    return {
        node_text(item, source)
        for item in _walk_type(node, "identifier")
        if item is not node
    }


def _walk_type(root: Node, node_type: str) -> Iterable[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == node_type:
            yield node
        stack.extend(reversed(node.children))
