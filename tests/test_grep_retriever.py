# test_grep_retriever.py
from pathlib import Path
import pytest
from openreview.repo.diff_parser import ParsedChangedFile, ParsedDiff
from openreview.retriever.grep_retriever import CodeContext, GrepRetriever


@pytest.fixture
def mock_repo(tmp_path: Path) -> Path:
    """在临时路径建立测试用的虚拟项目仓库结构"""
    # 源码文件
    src_dir = tmp_path / "src" / "foo"
    src_dir.mkdir(parents=True)
    (src_dir / "bar.py").write_text("def process_data():\n    return 42\n")

    # 测试目录与测试文件
    tests_dir = tmp_path / "tests" / "foo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_bar.py").write_text(
        "from src.foo.bar import process_data\ndef test_process():\n    assert process_data() == 42\n"
    )

    # 引用了 symbol 的其他模块
    service_dir = tmp_path / "src" / "services"
    service_dir.mkdir(parents=True)
    (service_dir / "runner.py").write_text(
        "from src.foo.bar import process_data\ndef run():\n    process_data()\n"
    )

    # 被排除的目录 (node_modules)
    node_dir = tmp_path / "node_modules" / "pkg"
    node_dir.mkdir(parents=True)
    (node_dir / "index.js").write_text("process_data()")

    # 超过 500KB 的大文件
    large_file = tmp_path / "src" / "large.py"
    large_file.write_text("process_data()\n" * 40000)  # > 500KB

    return tmp_path


def test_grep_retriever_full_flow(mock_repo: Path):
    retriever = GrepRetriever()

    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="src/foo/bar.py",
                status="modified",
                changed_symbols=["process_data"],
            )
        ]
    )

    results = retriever.retrieve(mock_repo, diff)

    # 校验返回值类型
    assert isinstance(results, list)
    assert all(isinstance(c, CodeContext) for c in results)

    # 提取所有找到的文件相对路径
    found_paths = [r.file_path for r in results]

    # 1. 验证匹配到了对应的测试文件 test_bar.py
    assert "tests/foo/test_bar.py" in found_paths or "tests\\foo\\test_bar.py" in found_paths

    # 2. 验证匹配到了包含 process_data 的引用位置 runner.py
    assert "src/services/runner.py" in found_paths or "src\\services\\runner.py" in found_paths

    # 3. 验证排除目录中的文件没有被检索到
    assert not any("node_modules" in p for p in found_paths)

    # 4. 验证过大的文件被正常跳过
    assert not any("large.py" in p for p in found_paths)


def test_snippet_line_limit(tmp_path: Path):
    """验证单个 snippet 行数限制"""
    repo = tmp_path
    long_file = repo / "long_script.py"
    long_file.write_text("my_symbol = 1\n" + "\n".join([f"# line {i}" for i in range(100)]))

    diff = ParsedDiff(
        files=[
            ParsedChangedFile(
                filename="source.py",
                status="modified",
                changed_symbols=["my_symbol"],
            )
        ]
    )

    retriever = GrepRetriever()
    results = retriever.retrieve(repo, diff)

    assert len(results) == 1
    snippet_lines = results[0].snippet.splitlines()
    assert len(snippet_lines) <= 40