# test_diff_parser.py
from openreview.repo.diff_parser import DiffParser


def test_python_symbol_extraction():
    patch = """@@ -10,3 +10,8 @@
 context
-def old_func():
+def new_func(a, b):
+    pass
+
+class NewClass(BaseModel):
+    def method_inside(self):
+        pass
"""
    file_data = {
        "filename": "src/utils.py",
        "status": "modified",
        "additions": 6,
        "deletions": 2,
        "patch": patch,
    }

    parsed = DiffParser.parse_file(file_data)
    assert parsed.language == "python"
    assert not parsed.is_test_file
    assert set(parsed.changed_symbols) == {"new_func", "NewClass", "method_inside"}


def test_go_symbol_extraction():
    patch = """@@ -5,2 +5,7 @@
 package main

+type User struct {}
+
+func NewUser() *User {
+    return &User{}
+}
+
+func (u *User) GetName() string {
+    return "test"
+}
"""
    file_data = {
        "filename": "pkg/user.go",
        "status": "modified",
        "additions": 9,
        "deletions": 0,
        "patch": patch,
    }

    parsed = DiffParser.parse_file(file_data)
    assert parsed.language == "go"
    assert parsed.changed_symbols == ["User", "NewUser", "GetName"]


def test_patch_is_none_handling():
    """测试二进制文件或无 patch 文件的边界情况"""
    file_data = {
        "filename": "assets/logo.png",
        "status": "added",
        "additions": 0,
        "deletions": 0,
        "patch": None,
    }

    parsed = DiffParser.parse_file(file_data)
    assert parsed.patch is None
    assert parsed.changed_symbols == []
    assert parsed.language is None


def test_file_type_classification():
    pytest_files = [
        ({"filename": "tests/test_main.py"}, True, False, False),
        ({"filename": "config.yaml"}, False, True, False),
        ({"filename": "README.md"}, False, False, True),
    ]

    for data, is_test, is_config, is_docs in pytest_files:
        parsed = DiffParser.parse_file(data)
        assert parsed.is_test_file == is_test
        assert parsed.is_config_file == is_config
        assert parsed.is_docs_file == is_docs