# test_manager.py
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from openreview.repo.manager import RepoCommandError, RepoManager


@pytest.fixture
def manager():
    return RepoManager(cache_dir=".cache/repos", timeout=30)


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
                    "https://github.com/owner_a/repo_b.git",
                    str(Path(".cache/repos/owner_a__repo_b")),
                ],
            ),
            {"cwd": None},
        ),
        # 清理工作区
        (
            (["git", "reset", "--hard"],),
            {"cwd": Path(".cache/repos/owner_a__repo_b")},
        ),
        # Fetch PR HEAD
        (
            (["git", "fetch", "origin", "pull/123/head"],),
            {"cwd": Path(".cache/repos/owner_a__repo_b")},
        ),
        # Checkout PR Branch
        (
            (["git", "checkout", "-B", "pr-123", "FETCH_HEAD"],),
            {"cwd": Path(".cache/repos/owner_a__repo_b")},
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