"""Tool-turn provider children get an allowlisted environment (security #5).

Every provider child runs with permission checks off inside a workspace whose
instruction files may be hostile, so one ``printenv`` exposes every secret in
the operator's shell. The runner now builds each child's environment from an
allowlist (platform + provider + ``COOP_*``); ``coop start --inherit-env``
restores full inheritance.
"""

from __future__ import annotations

import argparse
import os
import pathlib
from types import SimpleNamespace
from unittest import mock

import pytest

from agent_coop import cli as coopcli
from agent_coop import coop_autonomous
from agent_coop import coop_runtime
from agent_coop import coop_start
from agent_coop.coop_capabilities import (
    MANIFESTS,
    CapabilityManifest,
    ExternalServer,
)
from tests.test_speed_structured import _claude_envelope, _grok_envelope

SECRETS = {
    "GITHUB_TOKEN": "ghp_leak",
    "GH_TOKEN": "gh_leak",
    "NPM_TOKEN": "npm_leak",
    "NODE_AUTH_TOKEN": "node_leak",
    "AWS_SECRET_ACCESS_KEY": "aws_leak",
    "AWS_ACCESS_KEY_ID": "aws_id_leak",
    "DEPLOY_HOST_TOKEN": "deploy_leak",
    "DB_SERVICE_ROLE_KEY": "db_role_leak",
    "DB_HOST_URL": "https://example.invalid",
    "STRIPE_SECRET_KEY": "stripe_leak",
    "DATABASE_URL": "postgres://user:pw@host/db",
    "npm_config_authtoken": "npm_cfg_leak",
    "npm_config__auth": "npm_auth_leak",
    "GIT_TOKEN": "git_leak",
    "NODE_PASSWORD": "node_pw_leak",
    "PYTHON_SECRET": "py_leak",
    "SOME_RANDOM_APP_SETTING": "unrelated",
}
KEEP = {
    "PATH": "provider-path",
    "PATHEXT": ".EXE;.CMD",
    "SystemRoot": r"C:\Windows",
    "SystemDrive": "C:",
    "ComSpec": r"C:\Windows\system32\cmd.exe",
    "windir": r"C:\Windows",
    "TEMP": r"C:\t",
    "TMP": r"C:\t",
    "TMPDIR": "/tmp",
    "APPDATA": r"C:\Users\me\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
    "USERPROFILE": r"C:\Users\me",
    "HOME": "/home/me",
    "HOMEDRIVE": "C:",
    "HOMEPATH": r"\Users\me",
    "USERNAME": "me",
    "USER": "me",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "ProgramW6432": r"C:\Program Files",
    "ProgramData": r"C:\ProgramData",
    "LANG": "en_GB.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "NO_COLOR": "1",
    "HTTP_PROXY": "http://proxy",
    "HTTPS_PROXY": "http://proxy",
    "NO_PROXY": "localhost",
    "http_proxy": "http://proxy",
    "https_proxy": "http://proxy",
    "no_proxy": "localhost",
    "SSL_CERT_FILE": "/etc/ssl/cert.pem",
    "REQUESTS_CA_BUNDLE": "/etc/ssl/cert.pem",
    "NODE_EXTRA_CA_CERTS": "/etc/ssl/cert.pem",
    "NODE_OPTIONS": "--max-old-space-size=4096",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    # No drive letter or backslash: the value must survive os.pathsep on every OS.
    "PYTHONPATH": "parent-extra-path",
    "CI": "true",
    "RUST_LOG": "info",
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
    "GIT_AUTHOR_NAME": "me",
    "XDG_CONFIG_HOME": "/home/me/.config",
    "DISABLE_AUTOUPDATER": "1",
    "COOP_RUN_TRACE_PATH": r"C:\runs\trace.jsonl",
    "COOP_DB": r"C:\work\board.db",
    "OPENAI_API_KEY": "sk-openai",
    "OPENAI_BASE_URL": "https://api.openai.example",
    "CODEX_HOME": r"C:\Users\me\.codex",
    "XAI_API_KEY": "xai-key",
    "GROK_HOME": r"C:\Users\me\.grok",
    "ANTHROPIC_API_KEY": "sk-ant",
    "ANTHROPIC_BASE_URL": "https://api.anthropic.example",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
    "CLAUDE_CONFIG_DIR": r"C:\Users\me\.claude",
}


def _parent_env(**extra):
    return {**KEEP, **SECRETS, **extra}


def _isolated_parent_env(tmp_path, **extra):
    runtime_root = tmp_path.parent / f"{tmp_path.name}-private-runtime"
    return _parent_env(
        COOP_RUNTIME_ROOT=str(runtime_root),
        **extra,
    )


PROVIDER_ENV_NAMES = {
    "claude": frozenset({
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
    }),
    "codex": frozenset({
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
    }),
    "grok": frozenset({
        "XAI_API_KEY",
        "GROK_HOME",
    }),
}


def _assert_only_provider_credentials(env, provider):
    normalized = _upper(env)
    for candidate, names in PROVIDER_ENV_NAMES.items():
        for name in names:
            if candidate == provider:
                assert name in normalized
                if not name.endswith(("_HOME", "_CONFIG_DIR")):
                    assert normalized[name] == _upper(KEEP)[name]
            else:
                assert name not in normalized


# ---- the allowlist itself ----------------------------------------------------

def test_tool_turn_env_keeps_platform_and_provider_drops_secrets():
    child = coop_start.tool_turn_env(_parent_env(), provider="claude")

    for name, value in KEEP.items():
        if name in PROVIDER_ENV_NAMES["claude"]:
            assert child.get(name) == value, name
        elif any(name in names for names in PROVIDER_ENV_NAMES.values()):
            assert name not in child, name
        else:
            assert child.get(name) == value, name
    for name in SECRETS:
        assert name not in child, name


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_tool_turn_env_keeps_only_the_selected_provider_credentials(provider):
    child = coop_start.tool_turn_env(_parent_env(), provider=provider)

    assert child["PATH"] == "provider-path"
    _assert_only_provider_credentials(child, provider)


def test_tool_turn_env_without_a_provider_keeps_no_provider_credentials():
    child = _upper(coop_start.tool_turn_env(_parent_env()))

    assert child["PATH"] == "provider-path"
    assert not any(
        name in child
        for names in PROVIDER_ENV_NAMES.values()
        for name in names
    )


def test_tool_turn_env_defaults_to_the_process_environment():
    with mock.patch.dict(os.environ, _parent_env(), clear=True):
        child = coop_start.tool_turn_env()

    assert child["PATH"] == "provider-path"
    assert "GITHUB_TOKEN" not in child


def test_cloud_backend_credentials_pass_only_when_claude_opts_in():
    source = _parent_env(
        AWS_REGION="eu-west-2",
        GOOGLE_APPLICATION_CREDENTIALS="/keys/sa.json",
        AZURE_CLIENT_SECRET="azure_leak",
        CLOUD_ML_REGION="europe-west1",
    )
    default = coop_start.tool_turn_env(source, provider="claude")
    assert not any(name.startswith("AWS_") for name in default)
    assert not any(name.startswith("GOOGLE_") for name in default)
    assert not any(name.startswith("AZURE_") for name in default)

    bedrock = coop_start.tool_turn_env(
        {**source, "CLAUDE_CODE_USE_BEDROCK": "1"},
        provider="claude",
    )
    assert bedrock["AWS_SECRET_ACCESS_KEY"] == "aws_leak"
    assert bedrock["AWS_REGION"] == "eu-west-2"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in bedrock

    vertex = coop_start.tool_turn_env(
        {**source, "CLAUDE_CODE_USE_VERTEX": "true"},
        provider="claude",
    )
    assert vertex["GOOGLE_APPLICATION_CREDENTIALS"] == "/keys/sa.json"
    assert vertex["CLOUD_ML_REGION"] == "europe-west1"
    assert "AWS_SECRET_ACCESS_KEY" not in vertex

    foundry = coop_start.tool_turn_env(
        {**source, "CLAUDE_CODE_USE_FOUNDRY": "1"},
        provider="claude",
    )
    assert foundry["AZURE_CLIENT_SECRET"] == "azure_leak"

    off = coop_start.tool_turn_env(
        {**source, "CLAUDE_CODE_USE_BEDROCK": "0"},
        provider="claude",
    )
    assert "AWS_SECRET_ACCESS_KEY" not in off

    codex = coop_start.tool_turn_env(
        {**source, "CLAUDE_CODE_USE_BEDROCK": "1"},
        provider="codex",
    )
    assert "AWS_SECRET_ACCESS_KEY" not in codex
    assert "AWS_REGION" not in codex


def test_inherit_restores_the_whole_parent_environment():
    source = _parent_env()

    child = coop_start.tool_turn_env(source, inherit=True)

    assert child == source
    assert child is not source


def test_allowlist_is_case_insensitive_and_stringifies():
    child = coop_start.tool_turn_env({"path": "p", "Systemroot": "s"})

    assert child == {"path": "p", "Systemroot": "s"}


# ---- launch paths -------------------------------------------------------------

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


def _cold_turn_env(
        tmp_path, *, provider="claude", capability_manifest=None,
        capability_config=None, **kwargs):
    captured = {}

    def tree_factory(argv, **spawn):
        captured["argv"] = list(argv)
        captured["env"] = dict(spawn["env"])
        return _Prepared()

    with mock.patch.dict(
            os.environ, _isolated_parent_env(tmp_path), clear=True):
        result = coop_start.invoke_turn(
            provider=provider,
            prompt="hi",
            session_id="s-1",
            agent_id=provider,
            board_path=str(tmp_path / "board.db"),
            cwd=str(tmp_path),
            timeout_s=5,
            run_dir=tmp_path / "run",
            tree_factory=tree_factory,
            resolve=lambda name: rf"C:\native\{name}.exe",
            capability_manifest=(
                capability_manifest or MANIFESTS["board_core"]
            ),
            capability_config=capability_config,
            **kwargs,
        )
    assert result["ok"] is True
    return captured["env"]


def test_cold_turn_child_gets_the_allowlist(tmp_path):
    env = _cold_turn_env(tmp_path)

    for name in SECRETS:
        assert name not in env, name
    assert env["PATH"] == "provider-path"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant"
    assert "OPENAI_API_KEY" not in env
    assert "XAI_API_KEY" not in env
    assert env["COOP_SESSION_ID"] == "s-1"
    assert env["COOP_DB"] == str(tmp_path / "board.db")
    assert env["PYTHONPATH"].split(os.pathsep)[0] == (
        coop_start._INSTALL_ROOT
    )
    assert "parent-extra-path" in env["PYTHONPATH"].split(os.pathsep)


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_cold_turn_keeps_only_the_selected_provider_credentials(
        provider, tmp_path):
    env = _cold_turn_env(tmp_path, provider=provider)

    _assert_only_provider_credentials(env, provider)


def test_mcp_turn_adds_only_selected_server_credentials_to_provider_env(
        tmp_path):
    manifest = CapabilityManifest(
        name="knowledge_recall",
        builtin_tools=("board", "local_files", "local_shell"),
        external_servers=("selected",),
    )
    server = ExternalServer(
        name="selected",
        transport="stdio",
        command="selected-mcp",
        args=(),
        url=None,
        env_values=(("SELECTED_MCP_TOKEN", "selected-token"),),
        bearer_token=None,
    )

    env = _cold_turn_env(
        tmp_path,
        provider="claude",
        capability_manifest=manifest,
        capability_config={"servers": {"selected": server}},
    )

    assert env["SELECTED_MCP_TOKEN"] == "selected-token"
    _assert_only_provider_credentials(env, "claude")
    assert "GITHUB_TOKEN" not in env


def test_grok_prompt_rejects_a_runtime_root_inside_the_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(
            coop_runtime.RuntimeSecurityError,
            match="outside the workspace"):
        coop_start.open_prompt_delivery(
            "grok",
            ["grok", "-p"],
            "prompt",
            workspace=workspace,
            env={"COOP_RUNTIME_ROOT": str(workspace / "cache")},
        )

    assert not (workspace / "cache").exists()


def _upper(env):
    # os.environ upper-cases keys on Windows; compare case-insensitively.
    return {str(key).upper(): value for key, value in env.items()}


def test_cold_turn_inherit_env_restores_everything(tmp_path):
    env = _upper(_cold_turn_env(tmp_path, inherit_env=True))

    for name, value in SECRETS.items():
        assert env[name.upper()] == value, name
    assert env["COOP_SESSION_ID"] == "s-1"


def _persistent_worker_envs(tmp_path, **kwargs):
    builds = {}

    def build_profile(provider, argv, manifest, **profile_kwargs):
        builds[provider] = dict(profile_kwargs["env"])
        return SimpleNamespace(
            argv=list(argv),
            env=dict(profile_kwargs["env"]),
            cleanup=lambda: None,
        )

    with mock.patch.dict(
            os.environ, _isolated_parent_env(tmp_path), clear=True):
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
                resolve_argv=lambda provider: [f"{provider}.exe"],
                build_profile=build_profile,
                **kwargs,
            )
        )
    assert unavailable == {}
    pool.stop_all()
    for cleanup in cleanups:
        cleanup()
    return builds


def test_persistent_workers_get_the_allowlist(tmp_path):
    builds = _persistent_worker_envs(tmp_path)

    assert set(builds) == {"claude", "codex", "grok"}
    for provider, env in builds.items():
        for name in SECRETS:
            assert name not in env, (provider, name)
        assert env["PATH"] == "provider-path"
        _assert_only_provider_credentials(env, provider)
        assert env["COOP_SESSION_ID"] == f"coop-{provider}"
        assert env["COOP_ITEM_ID"] == "25"
        assert env["PYTHONPATH"].split(os.pathsep)[0] == (
            coop_start._INSTALL_ROOT
        )


def test_persistent_workers_keep_only_their_provider_credentials(tmp_path):
    builds = _persistent_worker_envs(tmp_path)

    for provider, env in builds.items():
        _assert_only_provider_credentials(env, provider)


def test_persistent_workers_inherit_env_restores_everything(tmp_path):
    builds = _persistent_worker_envs(tmp_path, inherit_env=True)

    for provider, env in builds.items():
        env = _upper(env)
        for name, value in SECRETS.items():
            assert env[name.upper()] == value, (provider, name)


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_structured_answer_child_gets_the_allowlist(provider, tmp_path):
    calls = {}
    envelope = (
        _claude_envelope() if provider == "claude" else _grok_envelope()
    )

    def runner(argv, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=envelope.encode("utf-8"),
        )

    with mock.patch.dict(
            os.environ, _isolated_parent_env(tmp_path), clear=True):
        answer = coop_start.invoke_structured_answer(
            provider=provider,
            prompt="P",
            expected_question_id=9,
            cwd=str(tmp_path),
            runner=runner,
            resolve=lambda name: name,
        )

    assert answer == "pong"
    env = calls["env"]
    for name in SECRETS:
        assert name not in env, name
    assert env["PATH"] == "provider-path"
    _assert_only_provider_credentials(env, provider)
    assert "COOP_DB" not in env
    assert "PYTHONPATH" not in env


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_structured_answer_keeps_only_the_selected_provider_credentials(
        provider, tmp_path):
    calls = {}
    envelope = (
        _claude_envelope() if provider == "claude" else _grok_envelope()
    )

    def runner(argv, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=envelope.encode("utf-8"),
        )

    with mock.patch.dict(
            os.environ, _isolated_parent_env(tmp_path), clear=True):
        answer = coop_start.invoke_structured_answer(
            provider=provider,
            prompt="P",
            expected_question_id=9,
            cwd=str(tmp_path),
            runner=runner,
            resolve=lambda name: name,
        )

    assert answer == "pong"
    _assert_only_provider_credentials(calls["env"], provider)


def test_structured_answer_inherit_env_restores_everything(tmp_path):
    calls = {}

    def runner(argv, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=_grok_envelope().encode("utf-8"),
        )

    with mock.patch.dict(
            os.environ, _isolated_parent_env(tmp_path), clear=True):
        coop_start.invoke_structured_answer(
            provider="grok",
            prompt="P",
            expected_question_id=9,
            cwd=str(tmp_path),
            runner=runner,
            resolve=lambda name: name,
            inherit_env=True,
        )

    assert _upper(calls["env"])["GITHUB_TOKEN"] == "ghp_leak"


def test_runner_board_commands_get_the_allowlist(tmp_path):
    with mock.patch.dict(os.environ, _parent_env(), clear=True):
        env = coop_autonomous.agent_action_env(
            "codex",
            session_id="coop-codex",
            board_path=tmp_path / "board.db",
            lease_seconds=900,
            item_id=25,
        )
        inherited = coop_autonomous.agent_action_env(
            "codex",
            session_id="coop-codex",
            board_path=tmp_path / "board.db",
            lease_seconds=900,
            item_id=None,
            inherit=True,
        )

    for name in SECRETS:
        assert name not in env, name
    assert env["PATH"] == "provider-path"
    assert env["COOP_SESSION_ID"] == "coop-codex"
    assert env["COOP_AGENT"] == "codex"
    assert env["COOP_PROVIDER"] == "codex"
    assert env["COOP_DB"] == str(tmp_path / "board.db")
    assert env["COOP_ACTION_LEASE_SECONDS"] == "900"
    assert env["COOP_ITEM_ID"] == "25"
    assert env["PYTHONPATH"].split(os.pathsep)[0] == (
        coop_start._INSTALL_ROOT
    )
    assert _upper(inherited)["GITHUB_TOKEN"] == "ghp_leak"
    assert "COOP_ITEM_ID" not in inherited


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_runner_board_commands_keep_only_the_routed_provider_credentials(
        provider, tmp_path):
    with mock.patch.dict(os.environ, _parent_env(), clear=True):
        env = coop_autonomous.agent_action_env(
            provider,
            session_id=f"coop-{provider}",
            board_path=tmp_path / "board.db",
            lease_seconds=900,
        )

    _assert_only_provider_credentials(env, provider)


# ---- the opt-out flag ---------------------------------------------------------

def test_public_start_forwards_inherit_env(monkeypatch):
    captured = []
    monkeypatch.setattr(
        coop_autonomous,
        "main",
        lambda argv: captured.append(argv) or 0,
    )
    args = coopcli.build_parser().parse_args([
        "start", "--item", "25", "--inherit-env",
    ])
    args.fn(None, args)
    assert "--inherit-env" in captured[0]

    captured.clear()
    args = coopcli.build_parser().parse_args(["start", "--item", "25"])
    args.fn(None, args)
    assert "--inherit-env" not in captured[0]


def test_runner_parser_accepts_the_hidden_inherit_env_flag():
    assert coop_autonomous.main(["--inherit-env", "--selftest"]) == 0
    with pytest.raises(SystemExit):
        coop_autonomous.main(["--inherit-env=maybe", "--selftest"])


def test_start_help_documents_the_opt_out():
    parser = coopcli.build_parser()
    start = next(
        action for action in parser._subparsers._group_actions
    ).choices["start"]
    option = next(
        action for action in start._actions
        if "--inherit-env" in action.option_strings
    )
    assert isinstance(option, argparse._StoreTrueAction)
    assert option.help and option.help != argparse.SUPPRESS


def test_runner_action_env_handles_a_pathlib_board(tmp_path):
    env = coop_autonomous.agent_action_env(
        "claude",
        session_id="coop-claude",
        board_path=pathlib.Path(tmp_path, "board.db"),
        lease_seconds=1,
        item_id=None,
        source={"PATH": "p"},
    )

    assert env["COOP_DB"] == str(tmp_path / "board.db")
    assert env["PATH"] == "p"
