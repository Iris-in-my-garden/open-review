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


def test_changed_lines_and_removed_symbols():
    patch = """@@ -10,3 +10,4 @@ def existing_func():
 context
-def old_func():
+def new_func():
+    pass
"""
    parsed = DiffParser.parse_file(
        {
            "filename": "src/utils.py",
            "status": "modified",
            "additions": 2,
            "deletions": 1,
            "patch": patch,
        }
    )

    assert parsed.changed_symbols == ["new_func"]
    assert parsed.removed_symbols == ["old_func"]
    # context 行对应新文件第 10 行，两个新增行是 11、12
    assert parsed.changed_lines == [11, 12]


def test_pure_deletion_hunk_falls_back_to_hunk_start():
    patch = """@@ -5,2 +5,0 @@
-def gone():
-    return 1
"""
    parsed = DiffParser.parse_file(
        {
            "filename": "src/legacy.py",
            "status": "modified",
            "additions": 0,
            "deletions": 2,
            "patch": patch,
        }
    )

    # 没有新增行时，用新文件中的 Hunk 起始行作为定位锚点
    assert parsed.changed_lines == [5]
    assert parsed.removed_symbols == ["gone"]
    assert parsed.changed_symbols == []