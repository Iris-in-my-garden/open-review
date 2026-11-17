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


def test_collect_referenced_fields_handles_non_python():
    # 非 Python/Go 文件直接跳过，交给其他检索机制。
    assert collect_referenced_fields("func run() {}", ".rb", 1, 1) == []
