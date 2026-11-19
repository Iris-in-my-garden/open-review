"""P5：只收集被当前方法引用的类/结构体字段与模块常量。"""

from openreview.retriever.globals import collect_referenced_fields

SOURCE = (
    "RETRIES = 3\n"
    "UNUSED = 99\n"
    "\n"
    "class Service:\n"
    "    timeout = 10\n"
    "    retries = RETRIES\n"
    "\n"
    "    def run(self, payload):\n"
    "        local = len(payload)\n"
    "        return self.timeout + RETRIES + local\n"
)

GO_SOURCE = (
    "package pkg\n"
    "\n"
    "const DefaultTimeout = 30\n"
    "var retries = 3\n"
    "\n"
    "type Service struct {\n"
    "    timeout int\n"
    "    unused  string\n"
    "}\n"
    "\n"
    "func (s *Service) Run() int {\n"
    "    return s.timeout + DefaultTimeout + retries\n"
    "}\n"
)


def test_collect_referenced_fields_filters_unused_and_locals():
    fields = collect_referenced_fields(SOURCE, ".py", 8, 10)

    assert {field.name for field in fields} == {"timeout", "RETRIES"}


def test_collect_referenced_fields_returns_source_snippets():
    fields = collect_referenced_fields(SOURCE, ".py", 8, 10)
    by_name = {field.name: field for field in fields}

    assert by_name["RETRIES"].source == "RETRIES = 3"
    assert "timeout = 10" in by_name["timeout"].source


def test_collect_referenced_fields_handles_go_structs_and_constants():
    fields = collect_referenced_fields(GO_SOURCE, ".go", 11, 13)
    by_name = {field.name: field for field in fields}

    assert set(by_name) == {"timeout", "DefaultTimeout", "retries"}
    assert "timeout int" in by_name["timeout"].source
    assert by_name["DefaultTimeout"].source == "const DefaultTimeout = 30"
    assert by_name["retries"].source == "var retries = 3"


GO_SCOPED = (
    "package pkg\n"
    "\n"
    "type Service struct {\n"
    "    target int\n"
    "}\n"
    "\n"
    "type Other struct {\n"
    "    target int\n"
    "}\n"
    "\n"
    "func (s *Service) Run() int {\n"
    "    return s.target\n"
    "}\n"
)

GO_WINDOW = (
    "package pkg\n"
    "\n"
    "type Service struct {\n"
    "    timeout int\n"
    "}\n"
    "\n"
    "func Run() int {\n"
    "    svc := Service{timeout: 1}\n"
    "    _ = svc\n"
    "    _ = svc\n"
    "    _ = svc\n"
    "    return 0\n"
    "}\n"
)


def test_collect_referenced_fields_handles_non_python():
    # 非 Python/Go 文件直接跳过，交给其他检索机制。
    assert collect_referenced_fields("func run() {}", ".rb", 1, 1) == []


def test_go_fields_are_scoped_by_parent_type():
    """同名字段只匹配 receiver 对应的 struct，不跨 struct 误召回。"""
    fields = collect_referenced_fields(
        GO_SCOPED, ".go", 12, 12, changed_lines=[12]
    )

    assert {(field.parent_type, field.name) for field in fields} == {
        ("Service", "target")
    }


def test_go_field_references_outside_window_are_ignored():
    """changed line 附近没有字段访问时，P5 不应扫描整个函数体。"""
    far_fields = collect_referenced_fields(
        GO_WINDOW, ".go", 12, 12, changed_lines=[12]
    )
    near_fields = collect_referenced_fields(
        GO_WINDOW, ".go", 8, 8, changed_lines=[8]
    )

    assert far_fields == []
    assert {(field.parent_type, field.name) for field in near_fields} == {
        ("Service", "timeout")
    }


def test_go_large_package_literal_is_truncated_to_type_signature():
    entries = "\n".join(
        f'    {{name: "file-{index}"}},' for index in range(20)
    )
    source = (
        "package pkg\n"
        "\n"
        "type testFile struct {\n"
        "    name string\n"
        "}\n"
        "\n"
        "var cases = []testFile{\n"
        f"{entries}\n"
        "}\n"
        "\n"
        "func Run() string {\n"
        "    for _, file := range cases {\n"
        "        return file.name\n"
        "    }\n"
        "    return \"\"\n"
        "}\n"
    )
    changed_line = source.splitlines().index("        return file.name") + 1

    fields = collect_referenced_fields(
        source, ".go", changed_line, changed_line, changed_lines=[changed_line]
    )
    by_name = {field.name: field for field in fields}

    assert by_name["cases"].source == (
        "var cases []testFile // initializer omitted"
    )
    assert any(
        field.parent_type == "testFile" and field.name == "name"
        for field in fields
    )
