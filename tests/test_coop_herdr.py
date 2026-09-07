"""Herdr adapter and read-only mirror contracts."""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

from agent_coop import cli, coop_herdr, coop_start


INSTALL_ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_HERDR_LAUNCHER = str(
    (pathlib.Path(tempfile.gettempdir()) / "agent-coop-test-herdr").resolve()
)


@pytest.fixture(autouse=True)
def _resolve_synthetic_herdr(monkeypatch):
    monkeypatch.setattr(
        coop_herdr,
        "_resolve_herdr_launcher",
        lambda name, *, workspace: TEST_HERDR_LAUNCHER,
    )


def _response(result, *, returncode=0, stdout=None, stderr=""):
    if stdout is None:
        stdout = json.dumps({"id": "opaque-request", "result": result})
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class ScriptedRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.raw_calls = []

    def __call__(self, argv, **kwargs):
        raw_argv = list(argv)
        self.raw_calls.append((raw_argv, kwargs))
        normalized = ["herdr", *raw_argv[1:]] if raw_argv else []
        self.calls.append((normalized, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected Herdr call: {argv!r}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _created_workspace():
    return _response({
        "type": "workspace_created",
        "workspace": {"workspace_id": "workspace opaque/7"},
        "tab": {"tab_id": "tab opaque/11"},
        "root_pane": {"pane_id": "pane opaque/13"},
    })


def _created_tab():
    return _response({
        "type": "tab_created",
        "tab": {"tab_id": "tab opaque/17"},
        "root_pane": {"pane_id": "pane opaque/19"},
    })


def _ok():
    return _response({"type": "ok"})


def _option_values(argv, option):
    return [
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == option
    ]


def _spawn(adapter, run_id, provider, cwd):
    workspace = pathlib.Path(cwd).resolve()
    return adapter.spawn_mirror(
        run_id,
        provider,
        cwd=workspace,
        trace_path=workspace / f"{run_id}.trace.jsonl",
        board_path=workspace / ".coop" / "board.db",
        environ={},
    )


def _creation_context(cwd, run_id):
    workspace = pathlib.Path(cwd).resolve()
    return [
        "--cwd", str(workspace),
        "--env", f"COOP_RUN_TRACE_PATH={workspace / f'{run_id}.trace.jsonl'}",
        "--env", f"COOP_DB={workspace / '.coop' / 'board.db'}",
        "--env", f"PYTHONPATH={INSTALL_ROOT}",
    ]


def _fake_launcher(directory):
    directory.mkdir(parents=True, exist_ok=True)
    name = "herdr.exe" if os.name == "nt" else "herdr"
    launcher = directory / name
    launcher.write_text("synthetic launcher", encoding="utf-8")
    if os.name != "nt":
        launcher.chmod(0o700)
    return launcher.resolve()


def test_adapter_resolution_rejects_a_launcher_inside_the_selected_workspace(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    shadow = _fake_launcher(workspace)
    trusted = _fake_launcher(tmp_path / "trusted-bin")
    source = {
        "PATH": os.pathsep.join((str(shadow.parent), str(trusted.parent))),
        "PATHEXT": ".EXE;.CMD",
        "SystemRoot": r"C:\Windows",
    }
    runner = ScriptedRunner(_response({
        "type": "session_snapshot",
        "snapshot": {"version": "0.8.0", "protocol": 20},
    }))

    with monkeypatch.context() as patch:
        patch.setattr(coop_herdr.os, "environ", source)
        adapter = coop_herdr.HerdrAdapter(
            runner=runner,
            workspace=workspace,
            environ=source,
            resolve=coop_start.resolve_cli,
        )
        assert adapter.available() is True

    assert runner.raw_calls[0][0][0] == str(trusted)
    assert runner.raw_calls[0][0][0] != str(shadow)
    assert runner.raw_calls[0][1]["env"]["PATH"] == str(trusted.parent)


def test_adapter_pins_one_absolute_launcher_for_every_command(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _fake_launcher(tmp_path / "first-bin")
    second = _fake_launcher(tmp_path / "second-bin")
    resolutions = []

    def resolve(name, *, workspace=None):
        resolutions.append((name, pathlib.Path(workspace).resolve()))
        return str(first if len(resolutions) == 1 else second)

    runner = ScriptedRunner(
        _response({
            "type": "session_snapshot",
            "snapshot": {"version": "0.8.0", "protocol": 20},
        }),
        _response({
            "type": "session_snapshot",
            "snapshot": {"version": "0.8.0", "protocol": 20},
        }),
    )
    adapter = coop_herdr.HerdrAdapter(
        runner=runner,
        workspace=workspace,
        environ={"PATH": str(first.parent)},
        resolve=resolve,
    )

    assert adapter.available() is True
    assert adapter.available() is True

    assert resolutions == [("herdr", workspace.resolve())]
    assert [call[0][0] for call in runner.raw_calls] == [
        str(first), str(first),
    ]
    assert first.is_absolute()


@pytest.mark.skipif(os.name != "nt", reason="Windows batch lookup semantics")
def test_batch_launcher_resolves_dependencies_from_trusted_directory(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    trusted = tmp_path / "trusted-bin"
    workspace.mkdir()
    trusted.mkdir()
    shadow_marker = tmp_path / "workspace-shadow.txt"
    trusted_marker = tmp_path / "trusted-node.txt"
    launcher = trusted / "herdr.cmd"
    launcher.write_bytes(b"@call node %*\r\n")
    (workspace / "node.cmd").write_bytes(
        (
            f'@echo shadow>"{shadow_marker}"\r\n'
            '@echo {"result":{"type":"session_snapshot",'
            '"snapshot":{"source":"shadow"}}}\r\n'
        ).encode("utf-8")
    )
    (trusted / "node.cmd").write_bytes(
        (
            f'@echo trusted>"{trusted_marker}"\r\n'
            '@echo {"result":{"type":"session_snapshot",'
            '"snapshot":{"source":"trusted"}}}\r\n'
        ).encode("utf-8")
    )
    source = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP")
        if name in os.environ
    }
    source.update({"PATH": str(trusted), "PATHEXT": ".CMD;.EXE"})
    monkeypatch.chdir(workspace)
    adapter = coop_herdr.HerdrAdapter(
        workspace=workspace,
        environ=source,
        resolve=lambda name, *, workspace=None: str(launcher.resolve()),
    )

    assert adapter.available() is True
    assert trusted_marker.is_file()
    assert not shadow_marker.exists()


@pytest.mark.parametrize("pane_id", [
    "pane & echo unsafe",
    'pane "quoted"',
])
def test_batch_launcher_rejects_unsafe_opaque_ids_before_spawn(
        pane_id, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = tmp_path / "trusted-bin" / "herdr.cmd"
    launcher.parent.mkdir()
    launcher.write_text("@exit /b 0\n", encoding="utf-8")
    runner = ScriptedRunner(
        _response({
            "type": "workspace_created",
            "workspace": {"workspace_id": "workspace-safe"},
            "tab": {"tab_id": "tab-safe"},
            "root_pane": {"pane_id": pane_id},
        }),
        _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(
        runner=runner,
        workspace=workspace,
        environ={"PATH": str(launcher.parent), "PATHEXT": ".CMD"},
        resolve=lambda name, *, workspace=None: str(launcher.resolve()),
    )

    with pytest.raises(
            coop_herdr.HerdrInputError,
            match="unsafe value"):
        _spawn(adapter, "run-safe", "codex", workspace)

    assert [call[0][1:3] for call in runner.raw_calls] == [
        ["workspace", "create"],
        ["workspace", "close"],
    ]
    assert all(pane_id not in call[0] for call in runner.raw_calls)


def test_batch_launcher_accepts_only_the_internal_mirror_command(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = tmp_path / "trusted-bin" / "herdr.cmd"
    launcher.parent.mkdir()
    launcher.write_text("@exit /b 0\n", encoding="utf-8")
    runner = ScriptedRunner(_created_workspace(), _ok(), _ok())
    adapter = coop_herdr.HerdrAdapter(
        runner=runner,
        workspace=workspace,
        environ={"PATH": str(launcher.parent), "PATHEXT": ".CMD"},
        resolve=lambda name, *, workspace=None: str(launcher.resolve()),
    )

    assert _spawn(adapter, "run-safe", "codex", workspace) == (
        "pane opaque/13"
    )
    mirror_command = runner.raw_calls[2][0][-1]
    assert 'run_module' in mirror_command

    untrusted = ScriptedRunner()
    plain_adapter = coop_herdr.HerdrAdapter(
        runner=untrusted,
        workspace=workspace,
        environ={"PATH": str(launcher.parent), "PATHEXT": ".CMD"},
        resolve=lambda name, *, workspace=None: str(launcher.resolve()),
    )
    with pytest.raises(coop_herdr.HerdrInputError):
        plain_adapter._invoke([
            "herdr", "pane", "run", "pane-safe", str(mirror_command),
        ])
    assert untrusted.raw_calls == []


@pytest.mark.parametrize(("setting", "unsafe"), [
    ("python", r"C:\tools&run\python.exe"),
    ("install_root", r"C:\trusted%TEMP%\agent-coop"),
])
@pytest.mark.skipif(
    os.name != "nt",
    reason="cmd.exe metacharacter rules apply only on Windows",
)
def test_internal_mirror_command_rejects_batch_metacharacters(
        setting, unsafe, tmp_path, monkeypatch):
    if setting == "python":
        monkeypatch.setattr(coop_herdr.sys, "executable", unsafe)
    else:
        monkeypatch.setattr(coop_herdr, "_INSTALL_ROOT", unsafe)

    command = coop_herdr._mirror_shell_command("run-safe", "codex")
    launcher = tmp_path / "trusted-bin" / "herdr.cmd"
    launcher.parent.mkdir()
    launcher.write_text("@exit /b 0\n", encoding="utf-8")
    runner = ScriptedRunner()
    adapter = coop_herdr.HerdrAdapter(
        runner=runner,
        workspace=tmp_path / "workspace",
        resolve=lambda name, *, workspace=None: str(launcher.resolve()),
    )

    with pytest.raises(
            coop_herdr.HerdrInputError,
            match="unsafe for a Windows batch launcher"):
        adapter._invoke([
            "herdr", "pane", "run", "pane-safe", command,
        ])
    assert runner.raw_calls == []


def test_native_launcher_accepts_shell_quoted_install_paths(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        coop_herdr,
        "_INSTALL_ROOT",
        str(tmp_path / "trusted&tools"),
    )
    command = coop_herdr._mirror_shell_command("run-safe", "codex")
    launcher = tmp_path / "trusted-bin" / "herdr.exe"
    runner = ScriptedRunner(_ok())
    adapter = coop_herdr.HerdrAdapter(
        runner=runner,
        workspace=tmp_path / "workspace",
        resolve=lambda name, *, workspace=None: str(launcher.resolve()),
    )

    completed = adapter._invoke([
        "herdr", "pane", "run", "pane-safe", command,
    ])

    assert completed.returncode == 0
    assert runner.raw_calls[0][0][-1] == str(command)


def test_cmd_suffix_uses_posix_quoting_outside_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(
        coop_herdr,
        "_INSTALL_ROOT",
        str(tmp_path / "trusted&tools"),
    )
    command = coop_herdr._mirror_shell_command("run-safe", "codex")
    launcher = tmp_path / "trusted-bin" / "herdr.cmd"
    runner = ScriptedRunner(_ok())
    adapter = coop_herdr.HerdrAdapter(
        runner=runner,
        workspace=tmp_path / "workspace",
        resolve=lambda name, *, workspace=None: str(launcher.resolve()),
    )
    adapter._pinned_launcher()
    platform = type("Platform", (), {"name": "posix"})()
    monkeypatch.setattr(coop_herdr, "os", platform)

    completed = adapter._invoke([
        "herdr", "pane", "run", "pane-safe", command,
    ])

    assert completed.returncode == 0
    assert runner.raw_calls[0][0][-1] == str(command)


def test_every_herdr_call_gets_only_platform_and_session_environment(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = _fake_launcher(tmp_path / "trusted-bin")
    source = {
        "PATH": str(launcher.parent),
        "PATHEXT": ".EXE;.CMD",
        "SystemRoot": r"C:\Windows",
        "TEMP": str(tmp_path / "temp"),
        "HERDR_ENV": "1",
        "HERDR_WORKSPACE_ID": "outer-workspace",
        "HERDR_TAB_ID": "outer-tab",
        "HERDR_PANE_ID": "outer-pane",
        "ANTHROPIC_API_KEY": "foreign-provider-token",
        "OPENAI_API_KEY": "foreign-provider-token",
        "XAI_API_KEY": "foreign-provider-token",
        "GITHUB_TOKEN": "unrelated-token",
        "COOP_PRIVATE_VALUE": "unrelated-value",
    }
    expected = {
        "PATH": str(launcher.parent),
        "PATHEXT": ".EXE;.CMD",
        "SystemRoot": r"C:\Windows",
        "TEMP": str(tmp_path / "temp"),
        "HERDR_ENV": "1",
        "HERDR_WORKSPACE_ID": "outer-workspace",
        "HERDR_TAB_ID": "outer-tab",
        "HERDR_PANE_ID": "outer-pane",
    }
    pane = {
        "pane_id": "pane opaque/13",
        "workspace_id": "workspace opaque/7",
        "tab_id": "tab opaque/11",
        "agent_status": "unknown",
    }
    runner = ScriptedRunner(
        _response({
            "type": "session_snapshot",
            "snapshot": {"version": "0.8.0", "protocol": 20},
        }),
        _created_workspace(), _ok(), _ok(),
        _response({
            "type": "pane_read",
            "read": {"pane_id": "pane opaque/13", "text": "safe\n"},
        }),
        _response({"type": "pane_info", "pane": pane}),
        _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(
        runner=runner,
        workspace=workspace,
        environ=source,
        resolve=lambda name, *, workspace=None: str(launcher),
    )

    assert adapter.available() is True
    assert _spawn(adapter, "run-filtered", "codex", workspace) == (
        "pane opaque/13"
    )
    assert adapter.read("codex") == "safe\n"
    assert adapter.state("codex") == pane
    adapter.teardown(["codex"])

    assert runner.calls
    assert all(call[0][0] == str(launcher) for call in runner.raw_calls)
    assert all(call[1]["env"] == expected for call in runner.raw_calls)


def test_available_probes_the_live_api_without_leaking_probe_output(capsys):
    runner = ScriptedRunner(_response({
        "type": "session_snapshot",
        "snapshot": {"version": "0.8.0", "protocol": 20},
    }))

    assert coop_herdr.HerdrAdapter(
        runner=runner,
        environ={
            "PATH": str(pathlib.Path(TEST_HERDR_LAUNCHER).parent),
            "GITHUB_TOKEN": "must-not-pass",
        },
    ).available() is True

    assert runner.calls == [(
        ["herdr", "api", "snapshot"],
        {
            "capture_output": True,
            "text": True,
                "check": False,
                "timeout": coop_herdr.COMMAND_TIMEOUT_SECONDS,
                "env": {"PATH": str(pathlib.Path(TEST_HERDR_LAUNCHER).parent)},
                "cwd": str(pathlib.Path(TEST_HERDR_LAUNCHER).parent),
        },
    )]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", [
    _response(
        {"type": "session_snapshot", "snapshot": {}},
        returncode=1,
        stdout="secret stdout",
        stderr="secret stderr",
    ),
    _response({}, stdout="not json", stderr="also secret"),
    FileNotFoundError("herdr missing"),
    subprocess.TimeoutExpired(["herdr"], timeout=1, output="secret"),
])
def test_available_fails_closed_and_silent(failure, capsys):
    runner = ScriptedRunner(failure)

    assert coop_herdr.HerdrAdapter(runner=runner).available() is False
    assert capsys.readouterr() == ("", "")


def test_spawn_mirror_uses_argv_lists_and_parses_opaque_json_ids(tmp_path):
    runner = ScriptedRunner(_created_workspace(), _ok(), _ok())
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    pane_id = _spawn(adapter, "run-opaque_42", "codex", tmp_path)

    assert pane_id == "pane opaque/13"
    assert [call[0] for call in runner.calls] == [
        [
            "herdr", "workspace", "create",
            *_creation_context(tmp_path, "run-opaque_42"),
            "--label", "coop-run-run-opaque_42",
            "--no-focus",
        ],
        ["herdr", "pane", "rename", "pane opaque/13", "coop-codex"],
            [
                "herdr", "pane", "run", "pane opaque/13",
                coop_herdr._mirror_shell_command(
                    "run-opaque_42", "codex",
                ),
        ],
    ]
    assert all(isinstance(call[0], list) for call in runner.calls)
    assert all("shell" not in call[1] for call in runner.calls)


def test_spawn_mirror_uses_one_path_free_shell_command(monkeypatch):
    monkeypatch.setattr(
        sys,
        "executable",
        r"C:\Program Files\Python 3\python.exe",
    )
    runner = ScriptedRunner(_created_workspace(), _ok(), _ok())
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    _spawn(adapter, "run-command_42", "codex", pathlib.Path.cwd())

    pane_run_argv = runner.calls[2][0]
    assert pane_run_argv[:4] == [
        "herdr", "pane", "run", "pane opaque/13",
    ]
    assert pane_run_argv[4:] == [
        coop_herdr._mirror_shell_command("run-command_42", "codex"),
    ]


def test_mirror_python_entrypoint_ignores_a_workspace_package_shadow(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    trusted_root = tmp_path / "trusted-root"
    shadow_marker = tmp_path / "workspace-module.txt"
    trusted_marker = tmp_path / "trusted-module.txt"
    for root, marker in (
            (workspace, shadow_marker),
            (trusted_root, trusted_marker)):
        package = root / "agent_coop"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(coop_herdr, "_INSTALL_ROOT", str(trusted_root))
    runner = ScriptedRunner(_created_workspace(), _ok(), _ok())
    adapter = coop_herdr.HerdrAdapter(runner=runner, workspace=workspace)

    _spawn(adapter, "run-module-shadow", "codex", workspace)
    mirror_command = runner.calls[2][0][-1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(trusted_root)
    completed = subprocess.run(
        mirror_command,
        cwd=workspace,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert trusted_marker.is_file()
    assert not shadow_marker.exists()


def test_mirror_bootstrap_drops_inherited_credentials_before_import(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    package = tmp_path / "trusted-root" / "agent_coop"
    observed = tmp_path / "observed-environment.json"
    workspace.mkdir()
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        f"Path({str(observed)!r}).write_text("
        "json.dumps(dict(os.environ)), encoding='utf-8')\n",
        encoding="utf-8",
    )
    trusted_root = package.parent
    monkeypatch.setattr(coop_herdr, "_INSTALL_ROOT", str(trusted_root))
    environment = dict(os.environ)
    environment.update({
        "COOP_RUN_TRACE_PATH": str(tmp_path / "trace.jsonl"),
        "COOP_DB": str(workspace / ".coop" / "board.db"),
        "OPENAI_API_KEY": "foreign-provider-token",
        "GITHUB_TOKEN": "unrelated-token",
        "UNRELATED_SECRET": "unrelated-value",
    })

    completed = subprocess.run(
        str(coop_herdr._mirror_shell_command("run-env", "codex")),
        cwd=workspace,
        env=environment,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    child = json.loads(observed.read_text(encoding="utf-8"))
    assert child["COOP_RUN_TRACE_PATH"] == str(tmp_path / "trace.jsonl")
    assert child["COOP_DB"] == str(workspace / ".coop" / "board.db")
    assert "OPENAI_API_KEY" not in child
    assert "GITHUB_TOKEN" not in child
    assert "UNRELATED_SECRET" not in child


def test_second_mirror_gets_a_created_tab_in_the_same_workspace(tmp_path):
    runner = ScriptedRunner(
        _created_workspace(), _ok(), _ok(),
        _created_tab(), _ok(), _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)
    _spawn(adapter, "run-shared", "claude", tmp_path)

    pane_id = _spawn(adapter, "run-shared", "grok", tmp_path)

    assert pane_id == "pane opaque/19"
    assert runner.calls[3][0] == [
        "herdr", "tab", "create",
        "--workspace", "workspace opaque/7",
        *_creation_context(tmp_path, "run-shared"),
        "--label", "coop-grok",
        "--no-focus",
    ]


def test_workspace_id_accessor_preserves_opaque_id_without_a_cli_call(
        tmp_path, monkeypatch):
    runner = ScriptedRunner(_created_workspace(), _ok(), _ok())
    adapter = coop_herdr.HerdrAdapter(runner=runner)
    _spawn(adapter, "run-workspace-accessor", "codex", tmp_path)
    calls_after_spawn = list(runner.calls)

    assert adapter.workspace_id("run-workspace-accessor") == (
        "workspace opaque/7"
    )
    assert adapter.workspace_id("run-not-owned") is None
    assert runner.calls == calls_after_spawn

    monkeypatch.setattr(coop_herdr, "_DEFAULT_ADAPTER", adapter)
    assert coop_herdr.workspace_id("run-workspace-accessor") == (
        "workspace opaque/7"
    )
    assert runner.calls == calls_after_spawn

    with pytest.raises(coop_herdr.HerdrInputError):
        adapter.workspace_id("../unsafe")
    assert runner.calls == calls_after_spawn


@pytest.mark.parametrize("identifier", [
    "",
    " workspace opaque/7",
    "workspace opaque/7 ",
    "workspace\x00opaque/7",
    "workspace\nopaque/7",
    "x" * 1025,
])
def test_creation_rejects_noncanonical_opaque_ids_before_ownership(
        identifier, tmp_path):
    runner = ScriptedRunner(
        _response({
            "type": "workspace_created",
            "workspace": {"workspace_id": identifier},
            "tab": {"tab_id": "tab opaque/11"},
            "root_pane": {"pane_id": "pane opaque/13"},
        }),
        _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    with pytest.raises(coop_herdr.HerdrProtocolError):
        _spawn(adapter, "run-invalid-opaque", "codex", tmp_path)

    assert runner.calls[-1][0] == [
        "herdr", "tab", "close", "tab opaque/11",
    ]
    assert adapter.workspace_id("run-invalid-opaque") is None


def test_workspace_and_tab_creation_receive_authoritative_run_context(
        tmp_path):
    workspace = tmp_path / "foreign workspace"
    trace = workspace / ".coop-runs" / "benchmark-context.trace.jsonl"
    board = workspace / ".coop" / "board.db"
    inherited = str(tmp_path / "existing-pythonpath")
    runner = ScriptedRunner(
        _created_workspace(), _ok(), _ok(),
        _created_tab(), _ok(), _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    for provider in ("claude", "codex"):
        adapter.spawn_mirror(
            "benchmark-context",
            provider,
            cwd=workspace,
            trace_path=trace,
            board_path=board,
            environ={"PYTHONPATH": inherited},
        )

    expected_env = [
        f"COOP_RUN_TRACE_PATH={trace.resolve()}",
        f"COOP_DB={board.resolve()}",
        f"PYTHONPATH={INSTALL_ROOT}{os.pathsep}{inherited}",
    ]
    workspace_create = runner.calls[0][0]
    tab_create = runner.calls[3][0]
    assert _option_values(workspace_create, "--cwd") == [
        str(workspace.resolve())
    ]
    assert _option_values(tab_create, "--cwd") == [
        str(workspace.resolve())
    ]
    assert _option_values(workspace_create, "--env") == expected_env
    assert _option_values(tab_create, "--env") == expected_env


def test_created_mirror_command_resolves_agent_coop_from_foreign_cwd(
        tmp_path):
    workspace = tmp_path / "foreign-repo"
    trace = workspace / "artifacts" / "benchmark-subprocess.trace.jsonl"
    board = workspace / ".coop" / "board.db"
    trace.parent.mkdir(parents=True)
    trace.write_text(
        json.dumps({
            "run_id": "benchmark-subprocess",
            "event": "run_finished",
            "elapsed_ms": 1,
        }) + "\n",
        encoding="utf-8",
    )
    runner = ScriptedRunner(_created_workspace(), _ok(), _ok())
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    adapter.spawn_mirror(
        "benchmark-subprocess",
        "codex",
        cwd=workspace,
        trace_path=trace,
        board_path=board,
        environ={"PYTHONPATH": str(tmp_path / "preserved")},
    )

    create_argv = runner.calls[0][0]
    child_env = {
        key: value
        for key, value in (
            binding.split("=", 1)
            for binding in _option_values(create_argv, "--env")
        )
    }
    process_env = dict(os.environ)
    process_env.update(child_env)
    pane_run_argv = runner.calls[2][0]
    completed = subprocess.run(
        pane_run_argv[4],
        cwd=_option_values(create_argv, "--cwd")[0],
        env=process_env,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert "benchmark-subprocess" in completed.stdout
    assert "run finished" in completed.stdout


def test_read_and_state_use_only_the_owned_pane_and_parse_exact_results(
        tmp_path):
    pane = {
        "pane_id": "pane opaque/13",
        "workspace_id": "workspace opaque/7",
        "tab_id": "tab opaque/11",
        "agent_status": "unknown",
    }
    runner = ScriptedRunner(
        _created_workspace(), _ok(), _ok(),
        _response({
            "type": "pane_read",
            "read": {
                "pane_id": "pane opaque/13",
                "text": "mirror output\n",
            },
        }),
        _response({"type": "pane_info", "pane": pane}),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)
    _spawn(adapter, "run-read", "codex", tmp_path)

    assert adapter.read("codex") == "mirror output\n"
    assert adapter.state("codex") == pane
    assert runner.calls[3][0] == [
        "herdr", "pane", "read", "pane opaque/13",
        "--source", "recent-unwrapped", "--lines", "120",
    ]
    assert runner.calls[4][0] == [
        "herdr", "pane", "get", "pane opaque/13"
    ]


def test_teardown_closes_only_tabs_and_workspaces_created_by_this_adapter(
        tmp_path):
    runner = ScriptedRunner(
        _created_workspace(), _ok(), _ok(),
        _created_tab(), _ok(), _ok(),
        _ok(), _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)
    _spawn(adapter, "run-clean", "claude", tmp_path)
    _spawn(adapter, "run-clean", "codex", tmp_path)

    adapter.teardown(["codex", "unowned-pane"])
    adapter.teardown(["claude"])

    assert [call[0] for call in runner.calls[-2:]] == [
        ["herdr", "tab", "close", "tab opaque/17"],
        ["herdr", "workspace", "close", "workspace opaque/7"],
    ]


def test_malformed_creation_rolls_back_the_created_workspace(tmp_path):
    runner = ScriptedRunner(
        _response({
            "type": "workspace_created",
            "workspace": {"workspace_id": "workspace opaque/23"},
            "tab": {"tab_id": "tab opaque/29"},
        }),
        _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    with pytest.raises(coop_herdr.HerdrProtocolError):
        _spawn(adapter, "run-malformed", "codex", tmp_path)

    assert [call[0] for call in runner.calls] == [
        [
            "herdr", "workspace", "create",
            *_creation_context(tmp_path, "run-malformed"),
            "--label", "coop-run-run-malformed",
            "--no-focus",
        ],
        ["herdr", "workspace", "close", "workspace opaque/23"],
    ]


def test_malformed_later_creation_rolls_back_only_its_created_tab(tmp_path):
    runner = ScriptedRunner(
        _created_workspace(), _ok(), _ok(),
        _response({
            "type": "tab_created",
            "tab": {"tab_id": "tab opaque/31"},
        }),
        _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)
    _spawn(adapter, "run-malformed-tab", "claude", tmp_path)

    with pytest.raises(coop_herdr.HerdrProtocolError):
        _spawn(adapter, "run-malformed-tab", "grok", tmp_path)

    assert [call[0] for call in runner.calls[-2:]] == [
        [
            "herdr", "tab", "create",
            "--workspace", "workspace opaque/7",
            *_creation_context(tmp_path, "run-malformed-tab"),
            "--label", "coop-grok",
            "--no-focus",
        ],
        ["herdr", "tab", "close", "tab opaque/31"],
    ]


def test_failed_workspace_rollback_remains_owned_and_teardown_retries(
        tmp_path):
    failure = _response({}, returncode=1)
    runner = ScriptedRunner(
        _created_workspace(),
        failure,
        failure,
        failure,
        _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    with pytest.raises(coop_herdr.HerdrCommandError):
        _spawn(adapter, "run-pending-workspace", "codex", tmp_path)
    with pytest.raises(coop_herdr.HerdrCommandError):
        adapter.teardown([])
    adapter.teardown([])

    assert [call[0] for call in runner.calls[-3:]] == [
        ["herdr", "workspace", "close", "workspace opaque/7"],
        ["herdr", "workspace", "close", "workspace opaque/7"],
        ["herdr", "workspace", "close", "workspace opaque/7"],
    ]


def test_failed_tab_rollback_remains_owned_for_later_teardown(tmp_path):
    failure = _response({}, returncode=1)
    runner = ScriptedRunner(
        _created_workspace(), _ok(), _ok(),
        _created_tab(), failure, failure,
        _ok(), _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)
    _spawn(adapter, "run-pending-tab", "claude", tmp_path)

    with pytest.raises(coop_herdr.HerdrCommandError):
        _spawn(adapter, "run-pending-tab", "grok", tmp_path)
    adapter.teardown([])
    adapter.teardown(["claude"])

    assert [call[0] for call in runner.calls[-3:]] == [
        ["herdr", "tab", "close", "tab opaque/17"],
        ["herdr", "tab", "close", "tab opaque/17"],
        ["herdr", "workspace", "close", "workspace opaque/7"],
    ]


def test_missing_tab_id_closes_only_the_known_created_pane(tmp_path):
    runner = ScriptedRunner(
        _created_workspace(), _ok(), _ok(),
        _response({
            "type": "tab_created",
            "tab": {},
            "root_pane": {"pane_id": "pane opaque/37"},
        }),
        _ok(),
    )
    adapter = coop_herdr.HerdrAdapter(runner=runner)
    _spawn(adapter, "run-missing-tab", "claude", tmp_path)

    with pytest.raises(coop_herdr.HerdrProtocolError):
        _spawn(adapter, "run-missing-tab", "grok", tmp_path)

    assert runner.calls[-1][0] == [
        "herdr", "pane", "close", "pane opaque/37"
    ]


@pytest.mark.parametrize("run_id", [
    "../run-escape",
    "run/escape",
    r"run\escape",
    "--run-bad",
    ".hidden",
    "",
])
def test_unsafe_run_ids_are_rejected_before_any_herdr_call(run_id, tmp_path):
    runner = ScriptedRunner()
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    with pytest.raises(coop_herdr.HerdrInputError):
        adapter.spawn_mirror(run_id, "codex", cwd=tmp_path)

    assert runner.calls == []


@pytest.mark.parametrize("provider", ["codex;remove", "../grok", "other"])
def test_non_core_providers_are_rejected_before_any_herdr_call(
        provider, tmp_path):
    runner = ScriptedRunner()
    adapter = coop_herdr.HerdrAdapter(runner=runner)

    with pytest.raises(coop_herdr.HerdrInputError):
        adapter.spawn_mirror("run-safe", provider, cwd=tmp_path)

    assert runner.calls == []


def test_resolve_trace_path_uses_the_existing_run_trace_source(tmp_path):
    trace = tmp_path / ".coop-runs" / "run-source.trace.jsonl"

    assert coop_herdr.resolve_trace_path(
        "run-source",
        environ={"COOP_RUN_TRACE_PATH": str(trace)},
    ) == trace.resolve()

    assert coop_herdr.resolve_trace_path(
        "run-other",
        environ={"COOP_RUN_TRACE_PATH": str(trace)},
    ) == trace.resolve()


def test_benchmark_run_id_uses_explicit_trace_path(tmp_path):
    trace = tmp_path / "benchmark-1234abcd.trace.jsonl"

    assert coop_herdr.resolve_trace_path(
        "benchmark-1234abcd",
        environ={"COOP_RUN_TRACE_PATH": str(trace.resolve())},
    ) == trace.resolve()


@pytest.mark.parametrize("environ", [
    {},
    {"COOP_RUN_TRACE_PATH": "run-relative.trace.jsonl"},
])
def test_trace_resolution_requires_absolute_explicit_authority(
        environ, tmp_path):
    with pytest.raises(coop_herdr.HerdrInputError):
        coop_herdr.resolve_trace_path(
            "run-relative",
            environ=environ,
        )


def test_mirror_incrementally_renders_only_provider_and_run_events(tmp_path):
    run_id = "run-render_1"
    trace = tmp_path / f"{run_id}.trace.jsonl"
    initial = [
        {"run_id": run_id, "event": "run_started", "elapsed_ms": 0},
        {
            "run_id": run_id,
            "event": "action_eligible",
            "elapsed_ms": 20,
            "provider": "claude",
            "action": "claim_task",
        },
        {
            "run_id": run_id,
            "event": "action_eligible",
            "elapsed_ms": 25,
            "provider": "codex",
            "action": "review_task",
            "turn_id": "turn-1",
            "details": {"worker_mode": "persistent"},
        },
    ]
    trace.write_text(
        "\n".join(json.dumps(event) for event in initial)
        + "\n{malformed}\n"
        + json.dumps({
            "run_id": run_id,
            "event": "prompt_submitted",
            "elapsed_ms": 30,
            "provider": "codex",
            "action": "review_task",
            "turn_id": "turn-1",
        }),
        encoding="utf-8",
    )
    sleeps = []

    def append_terminal_event(seconds):
        sleeps.append(seconds)
        with trace.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n")
            handle.write(json.dumps({
                "run_id": run_id,
                "event": "run_finished",
                "elapsed_ms": 40,
                "details": {"classification": "all_done"},
            }) + "\n")

    output = io.StringIO()
    result = coop_herdr.render_mirror(
        run_id,
        "codex",
        trace_path=trace,
        output=output,
        poll_interval=0.01,
        max_idle_polls=3,
        sleep=append_terminal_event,
    )

    assert result == "finished"
    assert sleeps == [0.01]
    assert output.getvalue().splitlines() == [
        "Co-op mirror · run-render_1 · codex",
        "[00:00.000] run · run started",
        (
            "[00:00.025] codex · action eligible · action=review_task "
            "· turn=turn-1 · worker=persistent"
        ),
        (
            "[00:00.030] codex · prompt submitted · action=review_task "
            "· turn=turn-1"
        ),
        "[00:00.040] run · run finished · classification=all_done",
    ]


def test_mirror_has_pollable_stop_and_bounded_idle_wait(tmp_path):
    output = io.StringIO()
    stop_checks = iter([False, True])
    sleeps = []

    stopped = coop_herdr.render_mirror(
        "run-stop",
        "grok",
        trace_path=tmp_path / "missing.trace.jsonl",
        output=output,
        stop_requested=lambda: next(stop_checks),
        poll_interval=0.01,
        max_idle_polls=5,
        sleep=sleeps.append,
    )

    assert stopped == "stopped"
    assert sleeps == [0.01]

    timed_out = coop_herdr.render_mirror(
        "run-timeout",
        "grok",
        trace_path=tmp_path / "still-missing.trace.jsonl",
        output=io.StringIO(),
        poll_interval=0.01,
        max_idle_polls=2,
        sleep=lambda _seconds: None,
    )
    assert timed_out == "idle_timeout"


def test_mirror_default_has_no_fixed_idle_cutoff(tmp_path):
    checks = 0

    def stop_after_old_cutoff():
        nonlocal checks
        checks += 1
        return checks > 3600

    result = coop_herdr.render_mirror(
        "run-long",
        "codex",
        trace_path=tmp_path / "missing.trace.jsonl",
        output=io.StringIO(),
        poll_interval=0,
        stop_requested=stop_after_old_cutoff,
        sleep=lambda _seconds: None,
    )

    assert result == "stopped"


@pytest.mark.parametrize("max_idle_polls", [True, 0, -1, 1.5, "2"])
def test_mirror_rejects_invalid_optional_idle_bounds(
        max_idle_polls, tmp_path):
    with pytest.raises(coop_herdr.HerdrInputError):
        coop_herdr.render_mirror(
            "run-bound",
            "codex",
            trace_path=tmp_path / "missing.trace.jsonl",
            output=io.StringIO(),
            max_idle_polls=max_idle_polls,
        )


def test_cli_routes_herdr_mirror_without_opening_a_board(monkeypatch):
    seen = []
    monkeypatch.setattr(
        coop_herdr,
        "render_mirror",
        lambda run_id, provider, **_kwargs: (
            seen.append((run_id, provider)) or "finished"
        ),
    )
    monkeypatch.setattr(
        cli.coopdb,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("mirror must not open the board"),
    )

    cli.main(["herdr", "mirror", "run-cli", "claude"])

    assert seen == [("run-cli", "claude")]


@pytest.mark.parametrize("argv, expected", [
    (["herdr", "mirror", "run-default", "codex"], None),
    (
        [
            "herdr", "mirror", "run-bounded", "codex",
            "--max-idle-polls", "7",
        ],
        7,
    ),
])
def test_cli_idle_bound_is_optional_and_explicit(monkeypatch, argv, expected):
    seen = []
    monkeypatch.setattr(
        coop_herdr,
        "render_mirror",
        lambda run_id, provider, **kwargs: (
            seen.append((run_id, provider, kwargs)) or "finished"
        ),
    )

    cli.main(argv)

    assert seen == [(
        argv[2], argv[3], {"max_idle_polls": expected}
    )]
