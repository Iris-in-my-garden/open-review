import json
import subprocess
from pathlib import Path

from openreview.repo.local import (
    collect_local_changes,
    prepare_local_workspace,
)
from openreview.review.report_writer import ReportWriter
from openreview.services.review_service import ReviewPipeline


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "tests" / "test_app.py").write_text(
        "from src.app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (repo / "old.py").write_text("VALUE = 1\n", encoding="utf-8")
    base = _commit(repo, "base")

    (repo / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (repo / "src" / "util.py").write_text(
        "def double(value):\n    return value * 2\n", encoding="utf-8"
    )
    (repo / "old.py").unlink()
    head = _commit(repo, "head")
    return repo, base, head


def test_collect_local_changes_builds_github_equivalent_files(tmp_path: Path):
    repo, base, head = _make_repo(tmp_path)

    files = collect_local_changes(repo, base, head)
    by_name = {file.filename: file for file in files}

    assert set(by_name) == {"src/app.py", "src/util.py", "old.py"}
    assert by_name["src/app.py"].status == "modified"
    assert by_name["src/util.py"].status == "added"
    assert by_name["old.py"].status == "removed"
    assert by_name["src/app.py"].additions == 1
    assert by_name["src/app.py"].deletions == 1
    assert "+    return a + b" in (by_name["src/app.py"].patch or "")


def test_prepare_local_workspace_isolates_head_and_base(tmp_path: Path):
    repo, base, head = _make_repo(tmp_path)

    workspace = prepare_local_workspace(repo, base, head)
    try:
        assert "a + b" in (workspace.head_path / "src" / "app.py").read_text()
        assert workspace.base_path is not None
        assert "a - b" in (workspace.base_path / "src" / "app.py").read_text()
        assert (workspace.head_path / "src" / "util.py").exists()
        assert not (workspace.base_path / "src" / "util.py").exists()
        # 调用方仓库的工作区不应被 checkout 改动（仍停在它自己的 HEAD）
        assert "a + b" in (repo / "src" / "app.py").read_text()
    finally:
        temp_root = workspace.temp_root
        workspace.cleanup()
        assert temp_root is not None
        assert not temp_root.exists()


def test_run_local_generates_report_and_json(tmp_path: Path):
    repo, base, head = _make_repo(tmp_path)
    pipeline = ReviewPipeline(mock_llm=True)
    try:
        output_dir = tmp_path / "out"
        report_path = pipeline.run_local(
            repo,
            base,
            head,
            output_dir,
            owner="acme",
            repo="demo",
            pr_number=7,
        )

        assert report_path.exists()
        assert pipeline.last_result is not None
        assert pipeline.last_result.owner == "acme"
        assert pipeline.last_result.pr_number == 7
        assert pipeline.last_state is not None
        assert pipeline.last_state.local_mode is True

        json_path = ReportWriter.write_json(
            pipeline.last_result, output_dir / "result.json"
        )
        payload = json.loads(json_path.read_text())
        assert payload["owner"] == "acme"
        assert payload["pr_number"] == 7
        assert "source_aspects" in payload["potential_issues"][0] or payload[
            "potential_issues"
        ] == []

        # 临时 worktree 已清理，不会在系统临时目录里堆积。
        workspace = pipeline.last_state.local_workspace
        assert workspace is not None
        assert workspace.temp_root is not None
        assert not workspace.temp_root.exists()
    finally:
        pipeline.close()
