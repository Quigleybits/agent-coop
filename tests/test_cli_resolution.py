"""Provider CLI resolution never picks a launcher from the workspace
(security #2).

``shutil.which`` searches the current directory first on Windows (always on
3.10/3.11; on 3.12+ unless ``NoDefaultCurrentDirectoryInExePath`` is set), and
``coop start`` runs from the workspace, so a cloned repo carrying
``claude.cmd`` at its root would be launched with the orchestrator's
environment before any model turn. Co-op walks PATH itself, skips the
workspace, and pins the result once per run.
"""

from __future__ import annotations

import os
import pathlib
import stat
from unittest import mock

import pytest

from agent_coop import coop_start

_WINDOWS = os.name == "nt"


def _install(directory, name):
    """Drop a launcher the platform would accept for ``name``."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if _WINDOWS:
        path = directory / f"{name}.cmd"
        path.write_text("@echo off\r\n", encoding="utf-8")
    else:
        path = directory / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                   | stat.S_IXOTH)
    return path.resolve()


@pytest.fixture
def hermetic(monkeypatch):
    """No ambient PATH, no known install dirs, no cwd-search opt-out."""
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    monkeypatch.setattr(coop_start, "_CLI_EXTRA_DIRS", ())
    coop_start.release_cli_resolution()
    yield
    coop_start.release_cli_resolution()


def _path(*directories):
    return os.pathsep.join(str(directory) for directory in directories)


def test_workspace_launcher_is_never_resolved_from_cwd(
        hermetic, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    planted = _install(workspace, "claude")
    monkeypatch.chdir(workspace)

    assert coop_start.resolve_cli("claude") is None
    assert coop_start.resolve_cli("claude", workspace=workspace) is None
    # Even when the workspace is explicitly on PATH.
    monkeypatch.setenv("PATH", _path(workspace))
    assert coop_start.resolve_cli("claude") is None
    assert planted.is_file()


def test_path_launcher_wins_over_a_workspace_launcher(
        hermetic, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    tools = tmp_path / "tools"
    _install(workspace, "claude")
    genuine = _install(tools, "claude")
    monkeypatch.chdir(workspace)
    # Workspace first on PATH, cwd is the workspace: still the tools copy.
    monkeypatch.setenv("PATH", _path(workspace, tools))

    resolved = coop_start.resolve_cli("claude")

    assert resolved is not None
    assert pathlib.Path(resolved).resolve() == genuine
    assert pathlib.Path(resolved).is_absolute()


def test_workspace_subdirectories_on_path_are_skipped(
        hermetic, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    tools = tmp_path / "tools"
    _install(workspace / "node_modules" / ".bin", "codex")
    genuine = _install(tools, "codex")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv(
        "PATH",
        _path(workspace / "node_modules" / ".bin", "node_modules/.bin",
              ".", "", tools),
    )

    resolved = coop_start.resolve_cli("codex")

    assert pathlib.Path(resolved).resolve() == genuine


def test_empty_and_dot_path_entries_never_resolve(
        hermetic, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    _install(elsewhere, "grok")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", _path("", ".", ""))

    # cwd is not the workspace here, yet '.' and '' are still not PATH.
    assert coop_start.resolve_cli("grok", workspace=tmp_path / "w") is None


def test_explicit_workspace_beats_cwd(hermetic, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    _install(workspace, "claude")
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("PATH", _path(workspace))

    assert coop_start.resolve_cli("claude", workspace=workspace) is None


def test_known_install_dirs_still_back_stop_path(
        hermetic, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    known = tmp_path / "known"
    _install(workspace, "claude")
    genuine = _install(known, "claude")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(coop_start, "_CLI_EXTRA_DIRS", (known,))

    resolved = coop_start.resolve_cli("claude")

    assert pathlib.Path(resolved).resolve() == genuine


def test_known_install_dir_inside_the_workspace_is_skipped(
        hermetic, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    _install(workspace / "bin", "claude")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(coop_start, "_CLI_EXTRA_DIRS", (workspace / "bin",))

    assert coop_start.resolve_cli("claude") is None


@pytest.mark.skipif(not _WINDOWS, reason="PATHEXT launcher contract")
def test_windows_requires_a_pathext_launcher(hermetic, tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "codex").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("PATH", _path(tools))

    assert coop_start.resolve_cli("codex") is None

    launcher = tools / "codex.cmd"
    launcher.write_text("@echo off\r\n", encoding="utf-8")
    assert pathlib.Path(coop_start.resolve_cli("codex")).resolve() == (
        launcher.resolve()
    )
    # A name that already names a launcher resolves as-is.
    assert pathlib.Path(coop_start.resolve_cli("codex.cmd")).resolve() == (
        launcher.resolve()
    )


@pytest.mark.skipif(_WINDOWS, reason="POSIX executable-bit contract")
def test_posix_requires_the_executable_bit(hermetic, tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "codex").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("PATH", _path(tools))

    assert coop_start.resolve_cli("codex") is None

    _install(tools, "codex")
    assert pathlib.Path(coop_start.resolve_cli("codex")).resolve() == (
        (tools / "codex").resolve()
    )


def test_shutil_which_is_not_consulted(hermetic, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    planted = _install(workspace, "claude")
    monkeypatch.chdir(workspace)

    with mock.patch.object(
            coop_start.shutil, "which", return_value=str(planted)):
        assert coop_start.resolve_cli("claude") is None


def test_pinned_resolution_survives_a_mid_run_path_change(
        hermetic, tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    first = tmp_path / "first"
    second = tmp_path / "second"
    genuine = _install(first, "claude")
    impostor = _install(second, "claude")
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("PATH", _path(first))

    pinned = coop_start.pin_cli_resolution(
        ["claude", "codex", "grok"],
        workspace=workspace,
    )

    assert pathlib.Path(pinned["claude"]).resolve() == genuine
    assert pinned["codex"] is None
    assert pinned["grok"] is None
    # The launcher moves and an impostor appears mid-run: the pin holds.
    genuine.unlink()
    monkeypatch.setenv("PATH", _path(second))
    _install(workspace, "claude")
    assert pathlib.Path(coop_start.resolve_cli("claude")).resolve() == genuine
    assert coop_start.resolve_cli("codex") is None
    assert coop_start.resolved_provider_argv("claude")[0] == pinned["claude"]

    coop_start.release_cli_resolution()
    assert pathlib.Path(coop_start.resolve_cli("claude")).resolve() == (
        impostor
    )


def test_unpinned_names_still_resolve_live(hermetic, tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    genuine = _install(tools, "grok")
    monkeypatch.setenv("PATH", _path(tools))

    coop_start.pin_cli_resolution(["claude"], workspace=tmp_path / "repo")

    assert coop_start.resolve_cli("claude") is None
    assert pathlib.Path(coop_start.resolve_cli("grok")).resolve() == genuine


def test_default_pin_covers_every_provider(hermetic, tmp_path):
    pinned = coop_start.pin_cli_resolution(workspace=tmp_path)

    assert set(pinned) == set(coop_start.DEFAULT_AGENTS)
