"""P5：只收集被当前方法引用的类/模块级字段。"""

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


def test_collect_referenced_fields_filters_unused_and_locals():
    fields = collect_referenced_fields(SOURCE, ".py", 8, 10)

    assert {field.name for field in fields} == {"timeout", "RETRIES"}


def test_collect_referenced_fields_returns_source_snippets():
    fields = collect_referenced_fields(SOURCE, ".py", 8, 10)
    by_name = {field.name: field for field in fields}

    assert by_name["RETRIES"].source == "RETRIES = 3"
    assert "timeout = 10" in by_name["timeout"].source


def test_collect_referenced_fields_handles_non_python():
    assert collect_referenced_fields("func run() {}", ".go", 1, 1) == []
