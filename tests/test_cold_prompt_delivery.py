"""A cold turn's prompt never rides argv.

Windows resolves codex and grok through PATHEXT ``.cmd`` launchers, so their
spawn passes through cmd.exe and its ~8191-character command line. A cold
codex turn carrying a hydrated payload on argv dies with "The command line is
too long", and the runner then typed-stalls. These tests pin the delivery
contract offline: no live provider is invoked anywhere here.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_coop import coop_process
from agent_coop import coop_start
from agent_coop import coop_workers


# Comfortably past the cmd.exe limit and past the 4.7-6.2 KB hydrated
# payloads the failing trials carried.
LONG_PROMPT = "hydrated-turn-payload " * 900
WINDOWS_COMMAND_LINE_LIMIT = 8191


def _prompt_context(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    private_runtime = tmp_path / "private-runtime"
    return (
        workspace,
        private_runtime,
        {"COOP_RUNTIME_ROOT": str(private_runtime)},
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_stdin_prompt_backing_storage_uses_private_runtime(
        provider, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    unsafe_temp = workspace / "temp"
    private_runtime = tmp_path / "private-runtime"
    unsafe_temp.mkdir(parents=True)
    monkeypatch.setattr(coop_start.tempfile, "tempdir", str(unsafe_temp))

    delivery = coop_start.open_prompt_delivery(
        provider,
        [provider],
        LONG_PROMPT,
        workspace=workspace,
        env={"COOP_RUNTIME_ROOT": str(private_runtime)},
    )
    try:
        assert private_runtime.is_dir()
        assert len(list(private_runtime.glob("prompt-*"))) == 1
    finally:
        delivery.close()

    assert not list(private_runtime.glob("prompt-*"))


class _FakeTree:
    def __init__(self):
        self.pid = 4242

    def poll_root(self):
        return 0

    def graceful_stop(self):
        return True

    def force_stop(self):
        return None

    def is_empty(self):
        return True

    def close(self):
        return None


class _FakePrepared:
    def __init__(self, tree):
        self._tree = tree

    def release(self):
        return self._tree

    def abort(self):
        return None


def _spawn_capture(captured):
    """A tree factory that records argv and drains the delivered channel."""

    def tree_factory(argv, **kwargs):
        captured["argv"] = list(argv)
        stdin = kwargs["stdin"]
        captured["stdin_is_devnull"] = stdin == subprocess.DEVNULL
        captured["stdin"] = (
            None
            if stdin == subprocess.DEVNULL
            else stdin.read().decode("utf-8")
        )
        prompt_file = None
        if "--prompt-file" in argv:
            prompt_file = pathlib.Path(argv[argv.index("--prompt-file") + 1])
            captured["prompt_file"] = prompt_file
            captured["prompt_file_text"] = prompt_file.read_text(
                encoding="utf-8",
            )
        return _FakePrepared(_FakeTree())

    return tree_factory


def _stub_grok_home(monkeypatch, tmp_path):
    """Manufacture the Grok install these turns resolve against.

    ``resolve`` below models the Windows PATHEXT ``.cmd`` shim, and the launch
    profile refuses that shim unless it can point argv[0] at the native
    ``<GROK_HOME>/bin/grok.exe`` instead (coop_capabilities._prefer_native
    _grok). Reading the developer's real ``~/.grok`` for that binary makes the
    test pass only on a machine with Grok installed, so build both halves in
    the temp directory. Nothing here is executed: the tree factory is a fake.
    """
    home = tmp_path / "grok-home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "grok.exe").write_bytes(b"")
    monkeypatch.setenv("GROK_HOME", str(home))
    return home


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_long_prompt_is_delivered_off_argv(provider, tmp_path):
    workspace, _private_runtime, env = _prompt_context(tmp_path)
    delivery = coop_start.open_prompt_delivery(
        provider,
        coop_start.resolved_provider_argv(provider),
        LONG_PROMPT,
        workspace=workspace,
        env=env,
    )
    try:
        assert len(LONG_PROMPT) > WINDOWS_COMMAND_LINE_LIMIT
        assert LONG_PROMPT not in delivery.argv
        assert not any(LONG_PROMPT[:200] in value for value in delivery.argv)
        # The measure that matters is the command line Windows builds.
        command_line = subprocess.list2cmdline(delivery.argv)
        assert len(command_line) < 1000
        assert len(command_line) < WINDOWS_COMMAND_LINE_LIMIT

        if delivery.channel == "stdin":
            assert delivery.path is None
            assert delivery.stdin.read().decode("utf-8") == LONG_PROMPT
        else:
            assert delivery.stdin == subprocess.DEVNULL
            assert pathlib.Path(delivery.path).read_text(
                encoding="utf-8",
            ) == LONG_PROMPT
    finally:
        delivery.close()


def test_stdin_providers_signal_the_channel_the_cli_documents(tmp_path):
    workspace, _private_runtime, env = _prompt_context(tmp_path)
    claude = coop_start.open_prompt_delivery(
        "claude", ["claude", "-p"], "x", workspace=workspace, env=env,
    )
    codex = coop_start.open_prompt_delivery(
        "codex", ["codex", "exec"], "x", workspace=workspace, env=env,
    )
    try:
        # `claude -p` with no positional prompt reads stdin; `codex exec -`
        # is codex's explicit stdin form.
        assert claude.argv == ["claude", "-p"]
        assert codex.argv == ["codex", "exec", "-"]
    finally:
        claude.close()
        codex.close()


def test_grok_file_channel_replaces_the_value_taking_prompt_flag(tmp_path):
    workspace, private_runtime, env = _prompt_context(tmp_path)
    delivery = coop_start.open_prompt_delivery(
        "grok",
        ["grok", "--always-approve", "--tools", "read_file", "-p"],
        LONG_PROMPT,
        workspace=workspace,
        env=env,
    )
    try:
        assert "-p" not in delivery.argv
        assert "--single" not in delivery.argv
        assert delivery.argv[-2] == "--prompt-file"
        prompt_path = pathlib.Path(delivery.path)
        assert private_runtime in prompt_path.parents
        assert workspace not in prompt_path.parents
    finally:
        delivery.close()


def test_release_removes_the_prompt_file(tmp_path):
    workspace, private_runtime, env = _prompt_context(tmp_path)
    delivery = coop_start.open_prompt_delivery(
        "grok",
        ["grok", "-p"],
        LONG_PROMPT,
        workspace=workspace,
        env=env,
    )
    path = pathlib.Path(delivery.path)
    assert path.is_file()

    delivery.close()

    assert not path.exists()
    assert not list(private_runtime.glob("prompt-*"))
    delivery.close()  # releasing twice is not an error


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_invoke_turn_keeps_a_hydrated_prompt_off_the_command_line(
        provider, tmp_path, monkeypatch):
    captured = {}
    _stub_grok_home(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(
        "COOP_RUNTIME_ROOT",
        str(tmp_path / "private-runtime"),
    )

    result = coop_start.invoke_turn(
        provider=provider,
        prompt=LONG_PROMPT,
        session_id="s-f1",
        agent_id=provider,
        board_path=str(workspace / "board.db"),
        cwd=str(workspace),
        timeout_s=5,
        run_dir=workspace / "run",
        tree_factory=_spawn_capture(captured),
        resolve=lambda name: rf"C:\shims\{name}.cmd",
    )

    assert result["ok"] is True
    argv = captured["argv"]
    assert LONG_PROMPT not in argv
    assert not any(LONG_PROMPT[:200] in value for value in argv)
    assert len(subprocess.list2cmdline(argv)) < WINDOWS_COMMAND_LINE_LIMIT

    if provider == "grok":
        assert captured["stdin_is_devnull"] is True
        assert captured["prompt_file_text"] == LONG_PROMPT
        # The turn owns the file for exactly its own lifetime.
        assert not captured["prompt_file"].exists()
    else:
        assert captured["stdin"] == LONG_PROMPT


def test_failed_turn_still_releases_the_prompt_file(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / "run"
    _stub_grok_home(monkeypatch, tmp_path)
    private_runtime = tmp_path / "private-runtime"
    monkeypatch.setenv("COOP_RUNTIME_ROOT", str(private_runtime))

    def tree_factory(argv, **_kwargs):
        raise OSError("spawn refused")

    result = coop_start.invoke_turn(
        provider="grok",
        prompt=LONG_PROMPT,
        session_id="s-f1-fail",
        agent_id="grok",
        board_path=str(workspace / "board.db"),
        cwd=str(workspace),
        timeout_s=5,
        run_dir=run_dir,
        tree_factory=tree_factory,
        resolve=lambda name: rf"C:\shims\{name}.cmd",
    )

    assert result["ok"] is False
    assert result["classification"] == "worker_start_failed"
    assert not list(private_runtime.glob("prompt-*"))


def test_turn_retains_prompt_cleanup_until_it_succeeds(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt_path = tmp_path / "owned-prompt.txt"
    prompt_path.write_text(LONG_PROMPT, encoding="utf-8")
    attempts = []

    class OwnedPrompt:
        path = prompt_path

        @staticmethod
        def cleanup():
            attempts.append("cleanup")
            if len(attempts) < 3:
                raise OSError("synthetic cleanup failure")
            prompt_path.unlink(missing_ok=True)

    monkeypatch.setattr(
        coop_start.coop_runtime,
        "create_private_file",
        lambda *args, **kwargs: OwnedPrompt(),
    )
    monkeypatch.setattr(
        coop_start,
        "build_launch_profile",
        lambda *args, **kwargs: SimpleNamespace(
            argv=["claude.exe", "-p"],
            env={},
            cleanup=lambda: None,
        ),
    )
    registry = coop_workers.RunCleanupRegistry()

    result = coop_start.invoke_turn(
        provider="claude",
        prompt=LONG_PROMPT,
        session_id="s-cleanup",
        agent_id="claude",
        board_path=str(workspace / "board.db"),
        cwd=str(workspace),
        timeout_s=5,
        tree_factory=_spawn_capture({}),
        resolve=lambda name: "claude.exe",
        cleanup_registry=registry,
    )

    assert result["classification"] == "capability_shutdown_failed"
    assert result["cleanup_retained"] is True
    assert registry.pending == 1
    registry.drain()
    assert not prompt_path.exists()


def test_owned_process_tree_delivers_a_long_prompt_on_stdin(tmp_path):
    """The platform half: a file handle survives the ownership bootstrap.

    On Windows the prompt handle is inherited by the gated bootstrap and
    re-passed to the real provider, so the size only proves anything when a
    genuine process tree reads it.
    """
    workspace, _private_runtime, env = _prompt_context(tmp_path)
    delivery = coop_start.open_prompt_delivery(
        "codex",
        [
            sys.executable,
            "-c",
            (
                "import hashlib,sys;"
                "data=sys.stdin.buffer.read();"
                "sys.stdout.buffer.write("
                "str(len(data)).encode()+b' '"
                "+hashlib.sha256(data).hexdigest().encode())"
            ),
        ],
        LONG_PROMPT,
        workspace=workspace,
        env=env,
    )
    argv = [value for value in delivery.argv if value != "-"]
    expected = LONG_PROMPT.encode("utf-8")

    with open(tmp_path / "out.txt", "w+b") as stdout:
        tree = None
        try:
            prepared = coop_process.prepare_tree(
                argv,
                session_id=uuid.uuid4().hex,
                cwd=str(workspace),
                stdin=delivery.stdin,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
            )
            tree = prepared.release()
            deadline = time.monotonic() + 60
            while tree.poll_root() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert tree.poll_root() == 0
        finally:
            if tree is not None:
                tree.close()
            delivery.close()
        stdout.seek(0)
        observed = stdout.read().decode("utf-8").split()

    assert observed == [
        str(len(expected)),
        hashlib.sha256(expected).hexdigest(),
    ]
