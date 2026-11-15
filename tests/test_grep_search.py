from __future__ import annotations

import subprocess
from pathlib import Path

from openreview.retriever import grep as grep_module
from openreview.retriever.grep import search_symbols


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


def _write_repo(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "auth.py").write_text(
        "def login(user):\n"
        "    return user\n"
        "\n"
        "login_admin = login\n"
        "login_admin = 1\n",
        encoding="utf-8",
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.auth import login\n"
        "\n"
        "def handle(user):\n"
        "    return login(user)\n",
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "conf.py").write_text("login = None\n", encoding="utf-8")
    (root / "pkg" / "notes.md").write_text("login\n", encoding="utf-8")
    _init_repo(root)


def test_search_finds_whole_word_hits_and_skips_excluded_paths(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)

    result = search_symbols(root, ["login"])

    paths = {path for path, _, _ in result.hits}
    assert paths == {"pkg/auth.py", "pkg/api.py"}
    # `login_admin` 单独出现时不是整词命中；`docs/` 与 `.md` 不在检索面内。
    assert all(code.strip() != "login_admin = 1" for _, _, code in result.hits)


def test_search_chunks_git_grep_when_rg_is_missing(tmp_path: Path, monkeypatch):
    """没有 ripgrep 时按小批次调用 git grep，结果必须与单次检索一致。"""
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(grep_module, "GIT_GREP_CHUNK_SIZE", 1)

    result = search_symbols(root, ["login", "handle", "user"])

    assert result.engine == "git-grep"
    assert result.unavailable is False
    paths = {path for path, _, _ in result.hits}
    assert {"pkg/auth.py", "pkg/api.py"} <= paths


def test_search_reports_truncation(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)

    result = search_symbols(root, ["user"], max_hits=1)

    assert len(result.hits) == 1
    assert result.truncated is True


def test_search_timeout_degrades_without_rescanning(tmp_path: Path, monkeypatch):
    """超时是降级信号，不应该再触发一次全仓 Python 扫描。"""

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(grep_module.subprocess, "run", _timeout)

    result = search_symbols(root, ["login"])

    assert result.hits == []
    assert result.timeouts >= 1
    assert result.engine == "git-grep"


def test_search_falls_back_to_python_when_git_is_unusable(tmp_path: Path, monkeypatch):
    """git grep 完全不可用（例如非 git 目录）时才做有界 Python 扫描。"""

    class _Broken:
        returncode = 128
        stdout = ""
        stderr = "not a git repository"

    root = tmp_path / "repo"
    root.mkdir()
    _write_repo(root)
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        grep_module.subprocess, "run", lambda *args, **kwargs: _Broken()
    )

    result = search_symbols(root, ["login"])

    assert result.engine == "python"
    assert result.hits
    assert all(path.endswith(".py") for path, _, _ in result.hits)
