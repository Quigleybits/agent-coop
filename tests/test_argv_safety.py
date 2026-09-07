"""Board-derived text must never travel in provider argv (security #1).

Windows resolves the provider CLIs through npm ``.cmd`` shims, so every argv
element passes through cmd.exe, which does not honour Python's ``\\"``
escaping (BatBadBut / CVE-2024-24576 class). Three layers keep board text out
of that command line: structured one-shots deliver the prompt off argv, warm
session ids are shape-validated before they reach argv, and the spawn choke
point refuses cmd.exe metacharacters behind a ``.cmd``/``.bat`` launcher.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from agent_coop import coop_autonomous
from agent_coop import coop_decisions
from agent_coop import coop_start
from agent_coop.coop_capabilities import MANIFESTS
from tests.test_speed_structured import _claude_envelope, _grok_envelope

# A board item title crafted by a peer agent: closes the quoted argument,
# runs a command, and comments out the rest of the shim's command line.
HOSTILE_TEXT = 'Fix login" & echo INJECTED & rem '
FINGERPRINT = "b" * 64


def _request(prompt):
    return coop_decisions.make_decision_request(
        decision_kind="respond_handoff",
        provider="claude",
        agent_id="claude",
        item_id=39,
        action_fingerprint=FINGERPRINT,
        prompt=prompt,
        handoff_id=12,
    )


def _capturing_runner(calls, stdout_text):
    def runner(argv, **kwargs):
        calls["argv"] = [str(value) for value in argv]
        calls.update(kwargs)
        stdin = kwargs.get("stdin")
        if stdin in (None, subprocess.DEVNULL):
            calls["stdin_text"] = None
        else:
            calls["stdin_text"] = stdin.read().decode("utf-8")
        if "--prompt-file" in argv:
            path = pathlib.Path(argv[argv.index("--prompt-file") + 1])
            calls["prompt_file"] = path
            calls["prompt_file_text"] = path.read_text(encoding="utf-8")
        calls["cwd_existed"] = pathlib.Path(kwargs["cwd"]).is_dir()
        return SimpleNamespace(
            returncode=0,
            stdout=stdout_text.encode("utf-8"),
        )

    return runner


# ---- structured one-shots: prompt off argv -----------------------------------

@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_structured_answer_argv_has_no_prompt_slot(provider):
    argv = coop_start.structured_answer_argv(
        provider,
        resolve=lambda name: f"/bin/{name}",
    )

    assert argv[0] == f"/bin/{provider}"
    assert "-p" in argv
    assert "--json-schema" in argv
    assert coop_start.STRUCTURED_ANSWER_SCHEMA in argv
    # Nothing in argv is free text: every element is a flag, a code constant,
    # or the resolved launcher.
    for value in argv:
        assert (
            value == argv[0]
            or value.startswith("-")
            or value in ("", "json")
            or json.loads(value) is not None
        ), value


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_structured_answer_prompt_travels_off_argv(provider, tmp_path):
    calls = {}
    private_runtime = tmp_path.parent / f"{tmp_path.name}-private-runtime"
    envelope = (
        _claude_envelope() if provider == "claude" else _grok_envelope()
    )

    answer = coop_start.invoke_structured_answer(
        provider=provider,
        prompt=HOSTILE_TEXT,
        expected_question_id=9,
        cwd=str(tmp_path),
        timeout_s=30,
        runner=_capturing_runner(calls, envelope),
        resolve=lambda name: rf"C:\shims\{name}.cmd",
        base_env={
            "PATH": "provider-path",
            "COOP_DB": "must-not-pass",
            "COOP_RUNTIME_ROOT": str(private_runtime),
            "GITHUB_TOKEN": "must-not-pass",
        },
    )

    assert answer == "pong"
    assert not any(HOSTILE_TEXT in value for value in calls["argv"])
    assert not any("INJECTED" in value for value in calls["argv"])
    if provider == "claude":
        assert calls["stdin_text"] == HOSTILE_TEXT
        assert "--prompt-file" not in calls["argv"]
    else:
        assert calls["stdin_text"] is None
        assert calls["prompt_file_text"] == HOSTILE_TEXT
        assert private_runtime in calls["prompt_file"].parents
        assert pathlib.Path(calls["cwd"]) not in calls["prompt_file"].parents
        assert not calls["prompt_file"].exists()
    # Both providers run in an isolated cwd with the allowlisted env: the
    # grok branch used to run in the workspace with the full environment.
    assert calls["cwd_existed"] is True
    assert pathlib.Path(calls["cwd"]).resolve() != tmp_path.resolve()
    assert not pathlib.Path(calls["cwd"]).exists()
    assert calls["env"] == {"PATH": "provider-path"}


def test_structured_decision_prompt_travels_off_argv(tmp_path):
    request = _request(HOSTILE_TEXT)
    argv = coop_start.structured_decision_argv(
        request,
        resolve=lambda name: f"/bin/{name}",
    )
    assert argv[:2] == ["/bin/claude", "-p"]
    assert HOSTILE_TEXT not in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == (
        request.json_schema
    )

    calls = {}
    value = {"handoff_id": 12, "response": "accept", "reason": ""}
    stdout_text = json.dumps({
        "type": "result",
        "structured_output": value,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    result = coop_start.invoke_structured_decision(
        request=request,
        cwd=str(tmp_path),
        timeout_s=30,
        runner=_capturing_runner(calls, stdout_text),
        resolve=lambda name: rf"C:\shims\{name}.cmd",
        base_env={
            "PATH": "provider-path",
            "ANTHROPIC_API_KEY": "claude-token",
            "OPENAI_API_KEY": "foreign-token",
            "XAI_API_KEY": "foreign-token",
            "COOP_DB": "must-not-pass",
        },
    )

    assert dict(result.value) == value
    assert not any(HOSTILE_TEXT in value for value in calls["argv"])
    assert calls["stdin_text"] == HOSTILE_TEXT
    assert calls["env"] == {
        "PATH": "provider-path",
        "ANTHROPIC_API_KEY": "claude-token",
    }


def test_structured_prompt_file_is_released_when_the_runner_fails(tmp_path):
    seen = {}

    def runner(argv, **kwargs):
        seen["prompt_file"] = pathlib.Path(
            argv[argv.index("--prompt-file") + 1]
        )
        raise OSError("spawn refused")

    assert coop_start.invoke_structured_answer(
        provider="grok",
        prompt="P",
        expected_question_id=9,
        cwd=str(tmp_path),
        runner=runner,
        resolve=lambda name: name,
    ) is None
    assert not seen["prompt_file"].exists()


# ---- warm session ids: shape-validated before argv ---------------------------

@pytest.mark.parametrize("hostile", [
    HOSTILE_TEXT,
    "sess-abc",
    "--dangerously-skip-permissions",
    "12345678-1234-1234-1234-123456789abc extra",
    "",
])
def test_warm_session_loader_treats_non_uuid_ids_as_no_session(
        tmp_path, hostile):
    path = tmp_path / "warm-claude-session.json"
    path.write_text(json.dumps({
        "provider_session_id": hostile,
        "saved_at_epoch": 1000.0,
    }), encoding="utf-8")

    assert coop_autonomous.load_warm_claude_session(
        tmp_path,
        now_epoch=2000.0,
    ) is None


def test_warm_session_loader_accepts_a_uuid(tmp_path):
    session_id = str(uuid.uuid4())
    coop_autonomous.save_warm_claude_session(
        tmp_path,
        session_id,
        now_epoch=1000.0,
    )

    assert coop_autonomous.load_warm_claude_session(
        tmp_path,
        now_epoch=2000.0,
    ) == session_id
    # Case is not part of the shape.
    coop_autonomous.save_warm_claude_session(
        tmp_path,
        session_id.upper(),
        now_epoch=1000.0,
    )
    assert coop_autonomous.load_warm_claude_session(
        tmp_path,
        now_epoch=2000.0,
    ) == session_id.upper()


@pytest.mark.parametrize("bad", [
    HOSTILE_TEXT,
    "a b",
    'x"y',
    "x&y",
    "x\ny",
    "x" * 200,
])
def test_apply_provider_session_rejects_unshaped_ids(bad):
    with pytest.raises(ValueError):
        coop_start.apply_provider_session(
            ["claude", "-p"],
            provider="claude",
            provider_session_id=bad,
            resume=False,
        )


def test_apply_provider_session_accepts_uuid_and_plain_ids():
    for good in (str(uuid.uuid4()), "session-1", "provider.session_2"):
        argv = coop_start.apply_provider_session(
            ["claude", "-p"],
            provider="claude",
            provider_session_id=good,
            resume=False,
        )
        assert argv == ["claude", "--session-id", good, "-p"]


# ---- spawn choke point: cmd.exe metacharacters behind a .cmd/.bat shim -------

@pytest.mark.parametrize("launcher", [
    r"C:\Users\me\AppData\Roaming\npm\claude.cmd",
    r"C:\Users\me\AppData\Roaming\npm\CODEX.CMD",
    r"C:\tools\grok.bat",
    r"C:\tools\grok.BAT",
])
@pytest.mark.parametrize("hostile", [
    HOSTILE_TEXT,
    "a|b",
    "a<b",
    "a>b",
    "a^b",
    "%PATH%",
    "!X!",
    "a\nb",
    "a\rb",
    'plain"quote',
    '{"title":"x\\"y"}',
    'mcp_servers.x.command="a\\"b"',
    '{"title":"x\\" & calc"}',
    '{"args":["ok","bad & worse"]}',
    'shell_environment_policy.set.COOP_DB="C:\\\\a & b\\\\board.db"',
])
def test_cmd_launcher_refuses_metacharacters_anywhere_in_argv(
        launcher, hostile):
    argv = [launcher, "-p", "--output-format", "json", hostile]

    with pytest.raises(coop_start.LauncherArgvRejected) as caught:
        coop_start.check_launcher_argv(argv)
    message = str(caught.value)
    assert "cmd.exe" in message
    # The message never echoes the hostile text itself.
    assert "INJECTED" not in message
    assert "calc" not in message


@pytest.mark.parametrize("token", [
    "-p",
    "--dangerously-skip-permissions",
    "",
    "-",
    str(uuid.uuid4()),
    "coop-auto-claude-0123456789ab",
    r"C:\Users\me\repo\.coop\.coop-runs\coop-prompt-abc.txt",
    "/tmp/coop-prompt-abc.txt",
    coop_start.STRUCTURED_ANSWER_SCHEMA,
    '{"mcpServers":{}}',
    'web_search="disabled"',
    "features.hooks=false",
    'mcp_servers.docs.command="npx"',
    'mcp_servers.docs.args=["-y","@scope/server"]',
    'shell_environment_policy.set.COOP_DB="C:\\\\Users\\\\me\\\\board.db"',
    'shell_environment_policy.set.COOP_ITEM_ID="25"',
    "Bash,Read,Edit",
])
def test_cmd_launcher_accepts_code_built_tokens(token):
    argv = [r"C:\npm\codex.cmd", "exec", "-c", token]

    coop_start.check_launcher_argv(argv)


def test_native_launchers_are_not_second_guessed():
    for launcher in (
            r"C:\Users\me\.local\bin\claude.exe",
            "/usr/local/bin/claude",
            r"C:\grok\bin\grok.exe",
    ):
        coop_start.check_launcher_argv([launcher, "-p", HOSTILE_TEXT])


def test_launcher_rejection_is_a_typed_launch_error():
    assert issubclass(coop_start.LauncherArgvRejected, ValueError)


def test_guarded_prepare_tree_refuses_before_spawning(monkeypatch):
    spawned = []
    monkeypatch.setattr(
        coop_start.coop_process,
        "prepare_tree",
        lambda argv, **kwargs: spawned.append(list(argv)) or "prepared",
    )

    with pytest.raises(coop_start.LauncherArgvRejected):
        coop_start.guarded_prepare_tree(
            [r"C:\npm\claude.cmd", "-p", HOSTILE_TEXT],
            session_id="s",
        )
    assert spawned == []

    assert coop_start.guarded_prepare_tree(
        [r"C:\npm\claude.cmd", "-p"],
        session_id="s",
    ) == "prepared"
    assert spawned == [[r"C:\npm\claude.cmd", "-p"]]


def test_invoke_turn_surfaces_a_rejected_launcher_argv_as_a_failed_turn(
        tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setitem(
        coop_start.PROVIDER_INVOKE,
        "claude",
        ["claude", "-p", "--dangerously-skip-permissions", HOSTILE_TEXT],
    )

    def tree_factory(argv, **kwargs):
        spawned.append(list(argv))
        raise AssertionError("must not spawn")

    result = coop_start.invoke_turn(
        provider="claude",
        prompt="hi",
        session_id="s",
        agent_id="claude",
        board_path=str(tmp_path / "board.db"),
        cwd=str(tmp_path),
        timeout_s=5,
        run_dir=tmp_path / "run",
        tree_factory=tree_factory,
        resolve=lambda name: rf"C:\shims\{name}.cmd",
        capability_manifest=MANIFESTS["board_core"],
    )

    assert spawned == []
    assert result["ok"] is False
    assert result["process_started"] is False
    assert result["classification"] == "worker_start_failed"
    assert result["retryable"] is False
    assert result["note"].startswith("launcher_argv_rejected")
    assert "INJECTED" not in result["note"]


def test_invoke_turn_guard_ignores_native_launchers(tmp_path, monkeypatch):
    monkeypatch.setitem(
        coop_start.PROVIDER_INVOKE,
        "claude",
        ["claude", "-p", "--dangerously-skip-permissions", HOSTILE_TEXT],
    )

    class _Tree:
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

    class _Prepared:
        def release(self):
            return _Tree()

    result = coop_start.invoke_turn(
        provider="claude",
        prompt="hi",
        session_id="s",
        agent_id="claude",
        board_path=str(tmp_path / "board.db"),
        cwd=str(tmp_path),
        timeout_s=5,
        run_dir=tmp_path / "run",
        tree_factory=lambda argv, **kwargs: _Prepared(),
        resolve=lambda name: rf"C:\native\{name}.exe",
        capability_manifest=MANIFESTS["board_core"],
    )

    assert result["ok"] is True


def test_resident_workers_spawn_through_the_guarded_tree_factory(
        tmp_path, monkeypatch):
    constructed = {}

    class _Recording:
        def __init__(self, provider):
            self.provider = provider

        def __call__(self, *args, **kwargs):
            constructed[self.provider] = kwargs
            return SimpleNamespace(
                provider=self.provider,
                health=lambda: None,
            )

    monkeypatch.setattr(
        coop_autonomous.coop_resident_workers,
        "ClaudeStreamWorker",
        _Recording("claude"),
    )
    monkeypatch.setattr(
        coop_autonomous.coop_resident_workers,
        "GrokAcpWorker",
        _Recording("grok"),
    )
    monkeypatch.setattr(
        coop_autonomous.coop_provider_workers,
        "CodexAppServerWorker",
        _Recording("codex"),
    )
    monkeypatch.setattr(
        coop_autonomous.coop_provider_workers,
        "prepare_resumed_state",
        lambda workspace, provider, *, env=None: (
            tmp_path / "state", lambda: None
        ),
    )

    def build_profile(provider, argv, manifest, **kwargs):
        return SimpleNamespace(
            argv=list(argv),
            env=dict(kwargs["env"]),
            cleanup=lambda: None,
        )

    pool, cleanups, unavailable = (
        coop_autonomous.prepare_opt_in_worker_pool(
            {"claude", "codex", "grok"},
            sessions={
                "claude": "coop-claude",
                "codex": "coop-codex",
                "grok": "coop-grok",
            },
            board_path=tmp_path / "board.db",
            cwd=tmp_path,
            run_dir=tmp_path / "run",
            action_lease_seconds=900,
            item_id=25,
            resolve_argv=lambda provider: [rf"C:\npm\{provider}.cmd"],
            build_profile=build_profile,
        )
    )

    assert unavailable == {}
    assert set(constructed) == {"claude", "codex", "grok"}
    for provider, kwargs in constructed.items():
        assert kwargs["tree_factory"] is coop_start.guarded_prepare_tree, (
            provider
        )
