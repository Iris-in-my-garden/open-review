"""Tree-sitter 的轻量公共工具。

这里只放“多个模块都要用”的 AST 能力，避免 index / validator / retriever
各自维护一套 parser 和节点类型判断：

- 按后缀懒加载 parser
- 找某一行的最小包围定义（函数/类/方法）
- 读取定义名与 qualname
- 读取定义对应的源码范围

注意：解析失败必须降级为空结果，不能因为一个文件让整次 review 挂掉。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tree_sitter import Language, Node, Parser, Tree

DEFINITION_TYPES = {
    # Python
    "function_definition",
    "class_definition",
    # Go
    "function_declaration",
    "method_declaration",
    "type_declaration",
}

_PARSERS: Dict[str, Optional[Parser]] = {}
_PARSER_LOCK = threading.Lock()


def get_parser(suffix: str) -> Optional[Parser]:
    """按文件后缀返回 parser；依赖缺失时返回 None，由调用方降级。"""
    with _PARSER_LOCK:
        if suffix in _PARSERS:
            return _PARSERS[suffix]

        parser: Optional[Parser] = None
        try:
            if suffix == ".py":
                import tree_sitter_python as tspython

                parser = Parser(Language(tspython.language()))
            elif suffix == ".go":
                import tree_sitter_go as tsgo

                parser = Parser(Language(tsgo.language()))
        except ImportError:
            parser = None

        _PARSERS[suffix] = parser
        return parser


def parse_text(text: str, suffix: str) -> Optional[Tuple[str, bytes, Tree]]:
    """把文本解析成 (text, source_bytes, tree)；不支持的格式返回 None。"""
    parser = get_parser(suffix)
    if parser is None:
        return None
    source = text.encode("utf-8")
    try:
        return text, source, parser.parse(source)
    except Exception:  # noqa: BLE001 - parser 失败只影响增强上下文
        return None


def parse_file(path: Path) -> Optional[Tuple[str, bytes, Tree]]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return parse_text(text, path.suffix)


def find_enclosing_definition(tree: Tree, row: int) -> Optional[Node]:
    """返回包含 row（0-based）的最小定义节点。"""
    best: Optional[Node] = None
    stack: List[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.start_point[0] <= row <= node.end_point[0]:
            if node.type in DEFINITION_TYPES:
                best = node
            stack.extend(node.children)
    return best


def definition_name(node: Node, source: bytes) -> Optional[str]:
    """读取定义节点的名字；Go 的 type_declaration 需要下钻到 type_spec。"""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        for child in node.children:
            if child.type in {"type_spec", "type_alias"}:
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    break
    if name_node is None:
        return None
    return source[name_node.start_byte : name_node.end_byte].decode("utf-8", "ignore")


def definition_qualname(node: Node, source: bytes) -> str:
    """构造 module 内的 qualname，例如 `Service.handle`。"""
    parts: List[str] = []
    current: Optional[Node] = node
    while current is not None:
        if current.type in DEFINITION_TYPES:
            name = definition_name(current, source)
            if name:
                parts.append(name)
        current = current.parent
    return ".".join(reversed(parts))


def node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "ignore")


def node_span(node: Node) -> Tuple[int, int]:
    """返回 0-based 的 (start_row, end_row)。"""
    return node.start_point[0], node.end_point[0]


def iter_definitions(
    tree: Tree, source: bytes
) -> List[Tuple[str, str, str, int, int]]:
    """遍历所有定义，返回 (name, qualname, kind, start_line, end_line)。

    line 使用 1-based，方便直接写进数据库和报告。
    """
    result: List[Tuple[str, str, str, int, int]] = []
    stack: List[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in DEFINITION_TYPES:
            name = definition_name(node, source)
            if name:
                start_line, end_line = node_span(node)
                result.append(
                    (
                        name,
                        definition_qualname(node, source) or name,
                        node.type,
                        start_line + 1,
                        end_line + 1,
                    )
                )
        stack.extend(node.children)
    return result


def enclosing_definition_span(path: Path, line: int) -> Optional[Tuple[int, int]]:
    """返回 1-based 的 (start_line, end_line)，用于证据校验的 AST 窗口。"""
    parsed = parse_file(path)
    if parsed is None:
        return None
    _, _, tree = parsed
    node = find_enclosing_definition(tree, max(0, line - 1))
    if node is None:
        return None
    start_line, end_line = node_span(node)
    return start_line + 1, end_line + 1


def read_line_range(
    path: Path, start_line: int, end_line: int
) -> str:
    """读取 1-based、闭区间的源码行；越界时自动裁剪。"""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(0, start_line - 1)
    end = min(len(lines), end_line)
    return "\n".join(lines[start:end])
