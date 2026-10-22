# test_manager.py
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openreview.repo.manager import RepoCommandError, RepoManager, default_cache_dir


@pytest.fixture
def manager():
    return RepoManager(cache_dir=".cache/repos", timeout=30, max_cache_size_gb=0)


@patch("subprocess.run")
@patch.object(Path, "exists")
def test_prepare_pr_repo_clone_new(mock_exists, mock_run, manager):
    """测试当仓库本地不存在时，执行 clone -> fetch PR -> checkout 的流程"""
    mock_exists.return_value = False
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    result_path = manager.prepare_pr_repo("owner_a", "repo_b", 123)

    assert result_path == Path(".cache/repos/owner_a__repo_b").resolve()

    # 验证 git 命令调用顺序
    expected_calls = [
        # Clone
        (
            (
                [
                    "git",
                    "clone",
                    "--no-tags",
                    "--filter=blob:none",
                    "https://github.com/owner_a/repo_b.git",
                    str(Path(".cache/repos/owner_a__repo_b").resolve()),
                ],
            ),
            {"cwd": None},
        ),
        # 清理工作区
        (
            (["git", "reset", "--hard"],),
            {"cwd": Path(".cache/repos/owner_a__repo_b").resolve()},
        ),
        # Fetch PR HEAD
        (
            (["git", "fetch", "origin", "pull/123/head"],),
            {"cwd": Path(".cache/repos/owner_a__repo_b").resolve()},
        ),
        # Checkout PR Branch
        (
            (["git", "checkout", "-B", "pr-123", "FETCH_HEAD"],),
            {"cwd": Path(".cache/repos/owner_a__repo_b").resolve()},
        ),
    ]

    assert mock_run.call_count == len(expected_calls)
    for idx, (args, kwargs) in enumerate(expected_calls):
        call_args = mock_run.call_args_list[idx]
        assert call_args.args[0] == args[0]
        assert call_args.kwargs.get("cwd") == kwargs.get("cwd")
        assert call_args.kwargs.get("timeout") == 30


@patch("subprocess.run")
@patch.object(Path, "exists")
def test_prepare_pr_repo_fetch_existing(mock_exists, mock_run, manager):
    """测试当仓库本地已存在时，执行 fetch origin -> fetch PR -> checkout 的流程"""
    mock_exists.return_value = True
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    manager.prepare_pr_repo("owner_a", "repo_b", 456)

    # 验证 git 命令调用顺序
    assert mock_run.call_count == 3
    assert mock_run.call_args_list[0].args[0] == ["git", "reset", "--hard"]
    assert mock_run.call_args_list[1].args[0] == [
        "git",
        "fetch",
        "origin",
        "pull/456/head",
    ]
    assert mock_run.call_args_list[2].args[0] == [
        "git",
        "checkout",
        "-B",
        "pr-456",
        "FETCH_HEAD",
    ]


@patch("subprocess.run")
@patch.object(Path, "exists")
def test_command_error_raises_exception(mock_exists, mock_run, manager):
    """测试 git 命令失败时抛出 RepoCommandError 且包含 stderr"""
    mock_exists.return_value = False
    mock_run.return_value = MagicMock(
        returncode=1, stderr="Fatal: repository not found"
    )

    with pytest.raises(RepoCommandError) as exc_info:
        manager.prepare_pr_repo("invalid", "repo", 1)

    assert exc_info.value.returncode == 1
    assert "Fatal: repository not found" in exc_info.value.stderr
    assert exc_info.value.stderr in str(exc_info.value)


@patch.object(RepoManager, "_run_git_cmd")
@patch.object(Path, "exists")
def test_prepare_base_worktree_adds_detached_worktree(mock_exists, mock_run, manager):
    mock_exists.side_effect = [True, False]  # repo 存在，base worktree 不存在
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    result = manager.prepare_base_worktree("owner_a", "repo_b", "base-sha")

    expected_base = Path(".cache/repos/owner_a__repo_b__base").resolve()
    assert result == expected_base
    commands = [call.args[0] for call in mock_run.call_args_list]
    assert commands[0] == ["cat-file", "-e", "base-sha^{commit}"]
    assert commands[-1] == [
        "worktree",
        "add",
        "--detach",
        str(expected_base),
        "base-sha",
    ]


@patch.object(RepoManager, "_run_git_cmd")
@patch.object(Path, "exists")
def test_prepare_base_worktree_replaces_stale_directory(
    mock_exists, mock_run, manager
):
    mock_exists.side_effect = [True, True]
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    manager.prepare_base_worktree("owner_a", "repo_b", "base-sha")

    commands = [call.args[0] for call in mock_run.call_args_list]
    expected_base = Path(".cache/repos/owner_a__repo_b__base").resolve()
    assert ["worktree", "remove", "--force", str(expected_base)] in commands
    assert commands[-1] == [
        "worktree",
        "add",
        "--detach",
        str(expected_base),
        "base-sha",
    ]

def test_default_cache_dir_respects_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENREVIEW_CACHE_DIR", str(tmp_path / "cache"))
    assert default_cache_dir() == (tmp_path / "cache").resolve()


def test_prune_cache_evicts_oldest_repository(tmp_path: Path):
    manager = RepoManager(cache_dir=tmp_path, max_cache_size_gb=0.000001)

    old = tmp_path / "old__repo"
    new = tmp_path / "new__repo"
    old.mkdir()
    new.mkdir()
    (old / "blob").write_bytes(b"x" * 900)
    (new / "blob").write_bytes(b"x" * 900)
    os.utime(old, (1, 1))

    manager._prune_cache()

    assert not old.exists()
    assert new.exists()


@patch("subprocess.run")
def test_clone_falls_back_to_full_clone(mock_run, tmp_path: Path):
    manager = RepoManager(cache_dir=tmp_path, max_cache_size_gb=0)
    mock_run.side_effect = [
        MagicMock(returncode=1, stderr="filter unsupported", stdout=""),
        MagicMock(returncode=0, stderr="", stdout=""),
    ]

    manager._clone_repo("https://github.com/org/repo.git", tmp_path / "repo")

    first = mock_run.call_args_list[0].args[0]
    second = mock_run.call_args_list[1].args[0]
    assert "--filter=blob:none" in first
    assert "--filter=blob:none" not in second
