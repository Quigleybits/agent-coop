"""Repository-boundary regressions for implicit board discovery."""

from __future__ import annotations

from pathlib import Path

from agent_coop import coopdb


def _board(workspace: Path) -> Path:
    board = workspace / ".coop" / "board.db"
    board.parent.mkdir(parents=True, exist_ok=True)
    board.touch()
    return board.resolve()


def test_nested_git_repository_does_not_adopt_parent_board(tmp_path):
    """A nested repository must not mix its work with the containing board."""
    _board(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    source = workspace / "src"
    source.mkdir()

    assert coopdb.discover_board(source) is None


def test_worktree_git_file_stops_parent_board_discovery(tmp_path):
    """A linked worktree's .git file is the same isolation boundary as a dir."""
    _board(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / ".git").write_text(
        "gitdir: ../metadata/worktrees/app\n", encoding="utf-8"
    )
    source = workspace / "src"
    source.mkdir()

    assert coopdb.discover_board(source) is None


def test_non_git_directory_checks_only_the_starting_directory(tmp_path):
    """A plain directory must not inherit a board from an unrelated parent."""
    _board(tmp_path)
    workspace = tmp_path / "plain"
    workspace.mkdir()

    assert coopdb.discover_board(workspace) is None
    own_board = _board(workspace)
    assert coopdb.discover_board(workspace) == str(own_board)


def test_repository_board_remains_discoverable_from_nested_directory(tmp_path):
    """Walk-up remains available inside the nearest repository boundary."""
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    own_board = _board(workspace)
    source = workspace / "src" / "package"
    source.mkdir(parents=True)

    assert coopdb.discover_board(source) == str(own_board)
