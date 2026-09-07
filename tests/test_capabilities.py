"""Pure action-to-capability routing contracts."""

import dataclasses
import json
from pathlib import Path

import pytest

from agent_coop import coop_capabilities, coop_runtime
from agent_coop.coop_capabilities import (
    CAPABILITY_NAMES,
    MANIFESTS,
    CapabilityActivationError,
    CapabilityConfigError,
    CapabilityDecision,
    CapabilityManifest,
    ExternalServer,
    LaunchProfile,
    build_launch_profile,
    load_local_config,
    resolve_capability,
)


DENIED = CapabilityDecision(
    allowed=False,
    manifest=None,
    classification="capability_denied",
    legal_next_action=(
        "revise the item contract to name one supported capability"
    ),
)


@pytest.mark.parametrize(
    ("kind", "manifest_name"),
    [
        ("idle", "board_core"),
        ("huddle_post", "board_core"),
        ("huddle_close", "board_core"),
        ("respond_handoff", "board_core"),
        ("open_huddle", "board_core"),
        ("request_review", "board_core"),
        ("complete_task", "board_core"),
        ("review_task", "deep_review"),
        ("claim_task", "local_code"),
        ("recover_claim", "local_code"),
        ("resume_task", "local_code"),
        ("continue_task", "local_code"),
        ("define_contract", "local_code"),
        ("refine_contract", "local_code"),
        ("answer_question", "local_code"),
    ],
)
def test_every_current_next_action_kind_has_a_base_manifest(
        kind, manifest_name):
    decision = resolve_capability(
        {"kind": kind, "item_id": 25},
        {"allowed_actions": []},
    )

    assert decision.allowed
    assert isinstance(decision.manifest, CapabilityManifest)
    assert decision.manifest.name == manifest_name
    assert isinstance(decision.manifest.builtin_tools, tuple)
    assert decision.manifest.external_servers == ()
    assert decision.classification is None
    assert decision.legal_next_action is None


def test_capability_names_are_stable():
    assert CAPABILITY_NAMES == (
        "board_core",
        "local_code",
        "deep_review",
        "research_web",
        "browser_interactive",
        "knowledge_recall",
    )


def test_manifests_and_decisions_are_frozen():
    manifest = resolve_capability(
        {"kind": "continue_task"},
        {"allowed_actions": []},
    ).manifest

    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.name = "research_web"


def test_board_action_never_inherits_research_tag():
    item = {"allowed_actions": ["capability:research_web"]}
    decision = resolve_capability(
        {"kind": "huddle_post", "item_id": 25}, item)

    assert decision.allowed
    assert decision.manifest.name == "board_core"
    assert decision.manifest.external_servers == ()


def test_board_action_ignores_ambiguous_external_tags():
    item = {
        "allowed_actions": [
            "capability:research_web",
            "capability:browser_interactive",
        ]
    }
    decision = resolve_capability(
        {"kind": "complete_task", "item_id": 25}, item)

    assert decision.allowed
    assert decision.manifest.name == "board_core"


def test_deep_review_does_not_inherit_substantive_research_tag():
    item = {"allowed_actions": ["capability:research_web"]}
    decision = resolve_capability(
        {"kind": "review_task", "item_id": 25}, item)

    assert decision.allowed
    assert decision.manifest.name == "deep_review"


@pytest.mark.parametrize(
    "name",
    ["research_web", "browser_interactive", "knowledge_recall"],
)
def test_substantive_action_uses_exact_external_capability_tag(name):
    item = {"allowed_actions": [f"capability:{name}"]}
    decision = resolve_capability(
        {"kind": "continue_task", "item_id": 25}, item)

    assert decision.allowed
    assert decision.manifest.name == name
    assert decision.manifest.external_servers == ()


def test_non_exact_capability_like_text_does_not_upgrade():
    item = {
        "allowed_actions": [
            "research_web",
            "use capability:research_web when useful",
            "capability :research_web",
        ]
    }
    decision = resolve_capability(
        {"kind": "continue_task", "item_id": 25}, item)

    assert decision.allowed
    assert decision.manifest.name == "local_code"


def test_unknown_capability_tag_is_denied_with_legal_next_action():
    item = {"allowed_actions": ["capability:unknown_tool"]}
    decision = resolve_capability(
        {"kind": "continue_task", "item_id": 25}, item)

    assert decision == DENIED


def test_multiple_external_capability_tags_are_denied_as_ambiguous():
    item = {
        "allowed_actions": [
            "capability:knowledge_recall",
            "capability:research_web",
        ]
    }
    decision = resolve_capability(
        {"kind": "continue_task", "item_id": 25}, item)

    assert decision == DENIED


def test_duplicate_external_tag_is_not_ambiguous():
    item = {
        "allowed_actions": [
            "capability:research_web",
            "capability:research_web",
        ]
    }
    decision = resolve_capability(
        {"kind": "continue_task", "item_id": 25}, item)

    assert decision.allowed
    assert decision.manifest.name == "research_web"


def test_unknown_action_kind_is_denied():
    assert resolve_capability(
        {"kind": "invent_workflow"},
        {"allowed_actions": []},
    ) == DENIED


def test_titles_roles_and_provider_fields_do_not_influence_resolution():
    action = {
        "kind": "continue_task",
        "provider": "grok",
        "role": "research",
    }
    item = {
        "title": "Research the web with a browser",
        "objective": "Use every available search tool",
        "allowed_actions": ["read repository"],
    }

    decision = resolve_capability(action, item)
    assert decision.manifest.name == "local_code"


def _write_config(tmp_path, payload):
    path = tmp_path / "coop-capabilities.local.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_config():
    return {
        "version": 1,
        "capabilities": {
            "browser_interactive": ["browser"],
            "knowledge_recall": ["knowledge"],
        },
        "servers": {
            "browser": {
                "transport": "stdio",
                "command": "browser-mcp",
                "args": ["--headless"],
                "env_from": ["BROWSER_MCP_TOKEN"],
            },
            "knowledge": {
                "transport": "http",
                "url_from": "KNOWLEDGE_MCP_URL",
                "bearer_token_from": "KNOWLEDGE_MCP_TOKEN",
            },
        },
    }


def _valid_environment():
    return {
        "BROWSER_MCP_TOKEN": "browser-secret-value",
        "KNOWLEDGE_MCP_URL": "https://knowledge.invalid/mcp",
        "KNOWLEDGE_MCP_TOKEN": "knowledge-secret-value",
    }


def test_missing_local_config_is_an_empty_optional_configuration(tmp_path):
    config = load_local_config(
        tmp_path / "missing.json",
        environ={},
    )

    assert config == {
        "version": 1,
        "capabilities": {},
        "servers": {},
    }


def test_valid_local_config_resolves_env_after_validation_without_leaks(
        tmp_path):
    config = load_local_config(
        _write_config(tmp_path, _valid_config()),
        environ=_valid_environment(),
    )

    browser = config["servers"]["browser"]
    knowledge = config["servers"]["knowledge"]
    assert isinstance(browser, ExternalServer)
    assert browser.command == "browser-mcp"
    assert browser.args == ("--headless",)
    assert browser.env_values == (
        ("BROWSER_MCP_TOKEN", "browser-secret-value"),
    )
    assert knowledge.url == "https://knowledge.invalid/mcp"
    assert knowledge.bearer_token == "knowledge-secret-value"
    assert "browser-secret-value" not in repr(browser)
    assert "knowledge-secret-value" not in repr(knowledge)

    decision = resolve_capability(
        {"kind": "continue_task", "item_id": 25},
        {"allowed_actions": ["capability:browser_interactive"]},
        config,
    )
    assert decision.manifest.external_servers == ("browser",)
    assert "browser-secret-value" not in repr(decision.manifest)


@pytest.mark.parametrize(
    "extra",
    [
        {"unexpected": True},
        {"credentials": {"token": "literal-secret"}},
    ],
)
def test_unknown_top_level_config_keys_are_rejected(tmp_path, extra):
    payload = _valid_config()
    payload.update(extra)

    with pytest.raises(CapabilityConfigError):
        load_local_config(
            _write_config(tmp_path, payload),
            environ=_valid_environment(),
        )


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_unsupported_config_versions_are_rejected(tmp_path, version):
    payload = _valid_config()
    payload["version"] = version

    with pytest.raises(CapabilityConfigError, match="version"):
        load_local_config(
            _write_config(tmp_path, payload),
            environ=_valid_environment(),
        )


@pytest.mark.parametrize(
    "capability",
    ["unknown", "board_core", "local_code", "deep_review"],
)
def test_unknown_or_core_capability_mappings_are_rejected(
        tmp_path, capability):
    payload = _valid_config()
    payload["capabilities"] = {capability: ["browser"]}

    with pytest.raises(CapabilityConfigError, match="capability"):
        load_local_config(
            _write_config(tmp_path, payload),
            environ=_valid_environment(),
        )


def test_missing_environment_variable_names_are_reported_without_values(
        tmp_path):
    payload = _valid_config()
    environment = _valid_environment()
    environment.pop("KNOWLEDGE_MCP_TOKEN")

    with pytest.raises(
            CapabilityConfigError,
            match="KNOWLEDGE_MCP_TOKEN",
    ) as exc:
        load_local_config(
            _write_config(tmp_path, payload),
            environ=environment,
        )

    assert "browser-secret-value" not in str(exc.value)
    assert "https://knowledge.invalid/mcp" not in str(exc.value)


@pytest.mark.parametrize(
    "server_name",
    ["", "../browser", "browser.config", "browser name", "-browser"],
)
def test_unsafe_server_names_are_rejected(tmp_path, server_name):
    payload = _valid_config()
    payload["capabilities"] = {"browser_interactive": [server_name]}
    payload["servers"] = {
        server_name: payload["servers"]["browser"],
    }

    with pytest.raises(CapabilityConfigError, match="server name"):
        load_local_config(
            _write_config(tmp_path, payload),
            environ=_valid_environment(),
        )


def test_simultaneous_command_and_url_reference_is_rejected(tmp_path):
    payload = _valid_config()
    payload["servers"]["browser"]["url_from"] = "KNOWLEDGE_MCP_URL"

    with pytest.raises(CapabilityConfigError, match="command.*url_from"):
        load_local_config(
            _write_config(tmp_path, payload),
            environ=_valid_environment(),
        )


@pytest.mark.parametrize("field", ["url", "token", "headers", "env"])
def test_literal_secret_bearing_server_fields_are_rejected(
        tmp_path, field):
    payload = _valid_config()
    payload["servers"]["knowledge"][field] = "do-not-serialize-me"

    with pytest.raises(CapabilityConfigError) as exc:
        load_local_config(
            _write_config(tmp_path, payload),
            environ=_valid_environment(),
        )

    assert field in str(exc.value)
    assert "do-not-serialize-me" not in str(exc.value)


def test_referenced_server_must_be_declared(tmp_path):
    payload = _valid_config()
    payload["capabilities"]["browser_interactive"] = ["missing"]

    with pytest.raises(CapabilityConfigError, match="missing"):
        load_local_config(
            _write_config(tmp_path, payload),
            environ=_valid_environment(),
        )


@pytest.mark.parametrize(
    ("server_name", "replacement", "message"),
    [
        (
            "browser",
            {"transport": "stdio", "args": []},
            "command",
        ),
        (
            "knowledge",
            {"transport": "http"},
            "url_from",
        ),
        (
            "browser",
            {
                "transport": "stdio",
                "command": "browser-mcp",
                "args": [],
                "env_from": ["NOT-AN-ENV-NAME"],
            },
            "environment variable",
        ),
    ],
)
def test_transport_and_environment_reference_schema_is_strict(
        tmp_path, server_name, replacement, message):
    payload = _valid_config()
    payload["servers"][server_name] = replacement

    with pytest.raises(CapabilityConfigError, match=message):
        load_local_config(
            _write_config(tmp_path, payload),
            environ=_valid_environment(),
        )


def test_committed_example_is_a_valid_non_secret_template():
    path = Path(__file__).parents[1] / "coop-capabilities.example.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    environment = {
        name: f"resolved-{name}"
        for spec in payload["servers"].values()
        for key in ("env_from", "url_from", "bearer_token_from")
        for name in (
            spec.get(key, ())
            if isinstance(spec.get(key, ()), list)
            else (spec.get(key),)
        )
        if name
    }

    config = load_local_config(path, environ=environment)

    assert config["version"] == 1
    assert "https://" not in serialized
    assert '"url"' not in serialized
    assert '"token"' not in serialized
    assert payload["servers"]["browser"]["command"].startswith("REPLACE_")


BASE_ARGV = {
    "claude": [
        "claude.exe",
        "-p",
        "--dangerously-skip-permissions",
    ],
    "codex": [
        "codex.cmd",
        "exec",
        "--sandbox",
        "danger-full-access",
    ],
    "grok": [
        "grok.exe",
        "--always-approve",
        "-p",
    ],
}


def _profile_env(tmp_path, provider):
    source_home = tmp_path / f"{provider}-source"
    source_home.mkdir(parents=True)
    (source_home / "auth.json").write_text(
        f"{provider}-fake-auth-secret",
        encoding="utf-8",
    )
    key = {
        "codex": "CODEX_HOME",
        "grok": "GROK_HOME",
    }.get(provider)
    environment = {
        "COOP_RUNTIME_ROOT": str(tmp_path.parent / "private-runtime"),
    }
    if key:
        environment[key] = str(source_home)
    return environment, source_home


def _argument_value(argv, flag):
    return argv[argv.index(flag) + 1]


@pytest.mark.parametrize(
    ("provider", "required"),
    [
        (
            "claude",
            {
                "--setting-sources",
                "--disable-slash-commands",
                "--no-chrome",
                "--no-session-persistence",
                "--tools",
                "--strict-mcp-config",
            },
        ),
        ("codex", {"--strict-config"}),
        ("grok", {"--tools", "--disable-web-search"}),
    ],
)
def test_core_launch_profile_isolated_with_supported_cli_surfaces(
        tmp_path, provider, required):
    environment, source_home = _profile_env(tmp_path, provider)
    profile = build_launch_profile(
        provider,
        BASE_ARGV[provider],
        MANIFESTS["board_core"],
        env=environment,
        workspace=tmp_path,
        run_dir=tmp_path / "run",
    )
    try:
        assert isinstance(profile, LaunchProfile)
        assert required <= set(profile.argv)
        assert profile.external_server_names == ()
        if provider == "claude":
            assert "--safe-mode" not in profile.argv
            assert "--bare" not in profile.argv
            assert _argument_value(
                profile.argv, "--setting-sources"
            ) == ""
            assert "--mcp-config" not in profile.argv
        if provider == "codex":
            assert profile.env["CODEX_HOME"] != str(source_home)
            assert (
                Path(profile.env["CODEX_HOME"]) / "auth.json"
            ).read_text(encoding="utf-8") == "codex-fake-auth-secret"
            assert "-c" in profile.argv
            assert "web_search=\"disabled\"" in profile.argv
            assert "features.apps=false" in profile.argv
            assert "features.plugins=false" in profile.argv
        if provider == "grok":
            assert profile.env["GROK_HOME"] != str(source_home)
            assert profile.env["GROK_MEMORY"] == "0"
            assert profile.env["GROK_SUBAGENTS"] == "0"
            assert "--agent" in profile.argv
            assert "--agent-profile" not in profile.argv
    finally:
        isolated_home = profile.env.get(
            "CODEX_HOME",
            profile.env.get("GROK_HOME"),
        )
        profile.cleanup()
    if isolated_home:
        assert not Path(isolated_home).exists()


def _resident_surface(provider, manifest, home_root, run_dir, state_dir=None):
    """Return everything a built resident profile derives from a manifest."""
    environment, _source_home = _profile_env(home_root, provider)
    base_argv = (
        [BASE_ARGV[provider][0], "agent", "stdio"]
        if provider == "grok"
        else BASE_ARGV[provider]
    )
    profile = build_launch_profile(
        provider,
        base_argv,
        manifest,
        env=environment,
        workspace=home_root,
        run_dir=run_dir,
        **({"provider_state_dir": state_dir} if state_dir else {}),
    )
    try:
        argv = list(profile.argv)
        agent_profile = None
        if "--agent-profile" in argv:
            position = argv.index("--agent-profile") + 1
            agent_profile = Path(argv[position]).read_text(encoding="utf-8")
            argv[position] = "<agent-profile>"
        return argv, agent_profile, dict(profile.env)
    finally:
        profile.cleanup()


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_resident_board_and_review_surfaces_stay_identical(
        tmp_path, provider):
    # The claude/grok residents freeze one process at board_core and serve
    # deep_review turns from it. Any divergence between the two manifests
    # would silently change what a review turn can do, so it must fail here.
    run_dir = tmp_path / "run"
    state_dir = None
    state = None
    if provider == "grok":
        state = coop_runtime.create_private_directory(
            tmp_path / "board",
            "worker-state-grok",
            env={"COOP_RUNTIME_ROOT": str(tmp_path / "private-runtime")},
        )
        state_dir = state.path

    try:
        board = _resident_surface(
            provider,
            MANIFESTS["board_core"],
            tmp_path / "board",
            run_dir,
            state_dir=state_dir,
        )
        review = _resident_surface(
            provider,
            MANIFESTS["deep_review"],
            tmp_path / "review",
            run_dir,
            state_dir=state_dir,
        )
    finally:
        if state is not None:
            state.cleanup()

    assert board == review
    assert (
        MANIFESTS["board_core"].external_servers
        == MANIFESTS["deep_review"].external_servers
        == ()
    )
    provider_tools = coop_capabilities.PROVIDER_TOOLS[provider]
    assert provider_tools["board_core"] == provider_tools["deep_review"]
    assert not set(provider_tools["deep_review"]) & {
        "Edit",
        "Write",
        "search_replace",
    }
    surface = " ".join(board[0]) + (board[1] or "")
    for tool in provider_tools["deep_review"]:
        assert tool in surface


def test_core_grok_profile_excludes_slow_or_external_tools(tmp_path):
    environment, _source_home = _profile_env(tmp_path, "grok")
    profile = build_launch_profile(
        "grok",
        BASE_ARGV["grok"],
        MANIFESTS["board_core"],
        env=environment,
        workspace=tmp_path,
        run_dir=tmp_path / "run",
    )
    try:
        allowed = set(
            _argument_value(profile.argv, "--tools").split(",")
        )
        denied = set(
            _argument_value(
                profile.argv,
                "--disallowed-tools",
            ).split(",")
        )
        assert allowed == {
            "bash",
            "read_file",
            "grep_search",
            "list_dir",
        }
        assert {
            "web_search",
            "web_fetch",
            "search_tool",
            "use_tool",
            "task",
            "memory_search",
            "memory_get",
        } <= denied
    finally:
        profile.cleanup()


def test_grok_acp_profile_places_agent_options_before_stdio(tmp_path):
    environment, _source_home = _profile_env(tmp_path, "grok")
    profile = build_launch_profile(
        "grok",
        [BASE_ARGV["grok"][0], "agent", "stdio"],
        MANIFESTS["local_code"],
        env=environment,
        workspace=tmp_path,
        run_dir=tmp_path / "run",
    )
    try:
        agent_position = profile.argv.index("agent")
        stdio_position = profile.argv.index("stdio")
        profile_position = profile.argv.index("--agent-profile")
        assert agent_position < profile_position < stdio_position
        assert "--no-leader" in profile.argv[agent_position:stdio_position]
        assert "--always-approve" in profile.argv[
            agent_position:stdio_position
        ]
        assert "--agent" not in profile.argv
        assert "--tools" not in profile.argv
        assert "--disallowed-tools" not in profile.argv
    finally:
        profile.cleanup()


def test_research_web_uses_provider_native_search_without_mcp(tmp_path):
    for provider in ("claude", "codex", "grok"):
        environment, _source_home = _profile_env(
            tmp_path / provider,
            provider,
        )
        profile = build_launch_profile(
            provider,
            BASE_ARGV[provider],
            MANIFESTS["research_web"],
            env=environment,
            workspace=tmp_path / provider,
            run_dir=tmp_path / provider / "run",
        )
        try:
            assert profile.external_server_names == ()
            if provider == "claude":
                assert {"WebSearch", "WebFetch"} <= set(
                    _argument_value(
                        profile.argv,
                        "--tools",
                    ).split(",")
                )
            elif provider == "codex":
                assert "--search" in profile.argv
                assert "web_search=\"disabled\"" not in profile.argv
            else:
                assert "--disable-web-search" not in profile.argv
                assert {"web_search", "web_fetch"} <= set(
                    _argument_value(
                        profile.argv,
                        "--tools",
                    ).split(",")
                )
        finally:
            profile.cleanup()


def _selected_external_servers():
    return {
        "selected": ExternalServer(
            name="selected",
            transport="http",
            command=None,
            args=(),
            url="https://selected.invalid/mcp",
            env_values=(),
            bearer_token="selected-token-secret",
        ),
        "ambient": ExternalServer(
            name="ambient",
            transport="stdio",
            command="ambient-mcp",
            args=("--slow",),
            url=None,
            env_values=(("AMBIENT_TOKEN", "ambient-token-secret"),),
            bearer_token=None,
        ),
    }


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_explicit_external_profile_renders_only_requested_server(
        tmp_path, provider):
    manifest = dataclasses.replace(
        MANIFESTS["knowledge_recall"],
        external_servers=("selected",),
    )
    environment, _source_home = _profile_env(tmp_path, provider)
    profile = build_launch_profile(
        provider,
        BASE_ARGV[provider],
        manifest,
        env=environment,
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        servers=_selected_external_servers(),
    )
    try:
        assert profile.external_server_names == ("selected",)
        rendered_argv = " ".join(profile.argv)
        assert "ambient" not in rendered_argv
        assert "ambient-token-secret" not in rendered_argv
        assert "selected-token-secret" not in rendered_argv
        assert "selected-token-secret" not in repr(profile)

        if provider == "claude":
            config_path = Path(
                _argument_value(profile.argv, "--mcp-config")
            )
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            assert set(payload["mcpServers"]) == {"selected"}
            assert "--bare" not in profile.argv
            assert "--safe-mode" not in profile.argv
            assert _argument_value(
                profile.argv, "--setting-sources"
            ) == ""
            assert "--disable-slash-commands" in profile.argv
            assert "--strict-mcp-config" in profile.argv
        elif provider == "codex":
            assert "mcp_servers.selected.required=true" in profile.argv
            assert not any(
                "mcp_servers.ambient" in value
                for value in profile.argv
            )
            token_env_name = next(
                json.loads(value.split("=", 1)[1])
                for value in profile.argv
                if value.startswith(
                    "mcp_servers.selected.bearer_token_env_var="
                )
            )
            assert (
                profile.env[token_env_name]
                == "selected-token-secret"
            )
        else:
            config_path = Path(profile.env["GROK_HOME"]) / "config.toml"
            text = config_path.read_text(encoding="utf-8")
            assert "[mcp_servers.selected]" in text
            assert "mcp_servers.ambient" not in text
            assert {"search_tool", "use_tool"} <= set(
                _argument_value(
                    profile.argv,
                    "--tools",
                ).split(",")
            )
    finally:
        profile.cleanup()


def test_grok_profiles_can_reuse_one_run_scoped_isolated_session_home(
    tmp_path,
):
    environment, _source_home = _profile_env(tmp_path, "grok")
    state = coop_runtime.create_private_directory(
        tmp_path,
        "worker-state-grok",
        env=environment,
    )
    state_dir = state.path

    try:
        first = build_launch_profile(
            "grok",
            BASE_ARGV["grok"],
            MANIFESTS["board_core"],
            env=environment,
            workspace=tmp_path,
            run_dir=tmp_path / "run",
            provider_state_dir=state_dir,
        )
        first.cleanup()
        assert Path(first.env["GROK_HOME"]) == state_dir
        assert (state_dir / "config.toml").is_file()

        second = build_launch_profile(
            "grok",
            BASE_ARGV["grok"],
            MANIFESTS["local_code"],
            env=environment,
            workspace=tmp_path,
            run_dir=tmp_path / "run",
            provider_state_dir=state_dir,
        )
        second.cleanup()
        assert Path(second.env["GROK_HOME"]) == state_dir
        assert (state_dir / "config.toml").is_file()
    finally:
        state.cleanup()


def test_grok_profile_reasserts_boundary_after_cli_mutates_config(
    tmp_path,
):
    environment, _source_home = _profile_env(tmp_path, "grok")
    state = coop_runtime.create_private_directory(
        tmp_path,
        "worker-state-grok",
        env=environment,
    )
    state_dir = state.path

    first = build_launch_profile(
        "grok",
        BASE_ARGV["grok"],
        MANIFESTS["board_core"],
        env=environment,
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        provider_state_dir=state_dir,
    )
    try:
        first.cleanup()
        config_path = state_dir / "config.toml"
        with config_path.open(
                "a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                "\n[marketplace]\n"
                "default_skills_installs_purged = true\n"
                "official_marketplace_auto_installed = true\n"
                "\n[[marketplace.sources]]\n"
                'name = "xAI Official"\n'
                'git = "https://github.com/xai-org/plugin-marketplace.git"\n'
            )

        second = build_launch_profile(
            "grok",
            BASE_ARGV["grok"],
            MANIFESTS["local_code"],
            env=environment,
            workspace=tmp_path,
            run_dir=tmp_path / "run",
            provider_state_dir=state_dir,
        )
        restored = config_path.read_text(encoding="utf-8")
        assert "disable_plugins = true" in restored
        assert "[subagents]\nenabled = false" in restored
        assert "[marketplace]" not in restored
        assert "[[marketplace.sources]]" not in restored
        assert "mcp_servers." not in restored
    finally:
        if "second" in locals():
            second.cleanup()
        state.cleanup()


def test_codex_stdio_profile_forwards_named_environment_without_values(
        tmp_path):
    manifest = dataclasses.replace(
        MANIFESTS["board_core"],
        external_servers=("selected",),
    )
    server = ExternalServer(
        name="selected",
        transport="stdio",
        command="selected-mcp",
        args=(),
        url=None,
        env_values=(("SELECTED_TOKEN", "selected-token-secret"),),
        bearer_token=None,
    )
    environment, _source_home = _profile_env(tmp_path, "codex")
    profile = build_launch_profile(
        "codex",
        BASE_ARGV["codex"],
        manifest,
        env=environment,
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        servers={"selected": server},
    )
    try:
        assert (
            'mcp_servers.selected.env_vars=["SELECTED_TOKEN"]'
            in profile.argv
        )
        assert profile.env["SELECTED_TOKEN"] == "selected-token-secret"
        assert "selected-token-secret" not in " ".join(profile.argv)
        assert "selected-token-secret" not in repr(profile)
    finally:
        profile.cleanup()


def test_external_manifest_without_resolved_server_fails_closed(tmp_path):
    manifest = dataclasses.replace(
        MANIFESTS["knowledge_recall"],
        external_servers=("missing",),
    )

    with pytest.raises(CapabilityActivationError, match="missing"):
        build_launch_profile(
            "claude",
            BASE_ARGV["claude"],
            manifest,
            env={},
            workspace=tmp_path,
            run_dir=tmp_path,
            servers={},
        )


def test_unknown_provider_profile_fails_closed(tmp_path):
    with pytest.raises(CapabilityActivationError, match="provider"):
        build_launch_profile(
            "unknown",
            ["unknown"],
            MANIFESTS["board_core"],
            env={},
            workspace=tmp_path,
            run_dir=tmp_path,
        )


def test_launch_profile_cleanup_can_retry_after_filesystem_failure(
    tmp_path,
    monkeypatch,
):
    run_dir = tmp_path / "run"
    profile = build_launch_profile(
        "codex",
        BASE_ARGV["codex"],
        MANIFESTS["board_core"],
        env={
            "CODEX_HOME": str(tmp_path / "source-codex-home"),
            "COOP_RUNTIME_ROOT": str(tmp_path.parent / "private-runtime"),
        },
        workspace=tmp_path,
        run_dir=run_dir,
    )
    runtime_root = tmp_path.parent / "private-runtime"
    real_rmtree = coop_runtime.shutil.rmtree
    attempts = []

    def flaky_rmtree(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError("private profile path")
        return real_rmtree(path)

    monkeypatch.setattr(
        coop_runtime.shutil,
        "rmtree",
        flaky_rmtree,
    )

    with pytest.raises(OSError):
        profile.cleanup()
    assert any(runtime_root.glob("launch-profile-*"))

    profile.cleanup()
    assert not any(runtime_root.glob("launch-profile-*"))
    assert len(attempts) == 2


@pytest.mark.parametrize("provider", ["codex", "grok"])
def test_copied_auth_is_kept_outside_the_workspace(tmp_path, provider):
    """A provider auth copy must not become a repository run artifact."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_root = tmp_path / "private-runtime"
    environment, _source_home = _profile_env(tmp_path, provider)
    environment["COOP_RUNTIME_ROOT"] = str(private_root)

    profile = build_launch_profile(
        provider,
        BASE_ARGV[provider],
        MANIFESTS["board_core"],
        env=environment,
        workspace=workspace,
        run_dir=workspace / ".coop" / ".coop-runs",
    )
    isolated_home = Path(
        profile.env["CODEX_HOME" if provider == "codex" else "GROK_HOME"]
    )
    try:
        assert private_root.resolve() in isolated_home.parents
        assert workspace.resolve() not in isolated_home.parents
        assert (isolated_home / "auth.json").read_text(encoding="utf-8") == (
            f"{provider}-fake-auth-secret"
        )
        assert not list(workspace.rglob("*auth.json"))
        assert not list(workspace.rglob("launch-profile-*"))
    finally:
        profile.cleanup()
    assert not isolated_home.exists()


def test_claude_mcp_config_references_secrets_from_the_environment(tmp_path):
    """Claude's temporary MCP JSON must not serialize selected credentials."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_root = tmp_path / "private-runtime"
    manifest = dataclasses.replace(
        MANIFESTS["knowledge_recall"], external_servers=("selected",)
    )
    profile = build_launch_profile(
        "claude",
        BASE_ARGV["claude"],
        manifest,
        env={"COOP_RUNTIME_ROOT": str(private_root)},
        workspace=workspace,
        run_dir=workspace / ".coop" / ".coop-runs",
        servers=_selected_external_servers(),
    )
    config_path = Path(_argument_value(profile.argv, "--mcp-config"))
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        selected = payload["mcpServers"]["selected"]
        url_name = selected["url"][2:-1]
        token_name = selected["headers"]["Authorization"][9:-1]
        assert selected["url"] == "${" + url_name + "}"
        assert selected["headers"] == {
            "Authorization": "Bearer ${" + token_name + "}"
        }
        assert profile.env[url_name] == (
            "https://selected.invalid/mcp"
        )
        assert profile.env[token_name] == (
            "selected-token-secret"
        )
        assert "selected-token-secret" not in config_path.read_text(
            encoding="utf-8"
        )
        assert private_root.resolve() in config_path.parents
        assert workspace.resolve() not in config_path.parents
    finally:
        profile.cleanup()


def test_claude_mcp_environment_names_are_collision_free(tmp_path):
    """Similar server names and existing variables must stay independent."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_root = tmp_path / "private-runtime"
    sentinel_name = "COOP_MCP_612D62_URL"
    environment = {
        "COOP_RUNTIME_ROOT": str(private_root),
        sentinel_name: "preserve-existing-value",
    }
    servers = {
        name: ExternalServer(
            name=name,
            transport="http",
            command=None,
            args=(),
            url=f"https://{number}.invalid/mcp",
            env_values=(),
            bearer_token=f"token-{number}",
        )
        for name, number in (("a-b", "one"), ("a_b", "two"))
    }
    manifest = dataclasses.replace(
        MANIFESTS["knowledge_recall"],
        external_servers=tuple(servers),
    )

    profile = build_launch_profile(
        "claude",
        BASE_ARGV["claude"],
        manifest,
        env=environment,
        workspace=workspace,
        run_dir=workspace / ".coop" / ".coop-runs",
        servers=servers,
    )
    config_path = Path(_argument_value(profile.argv, "--mcp-config"))
    try:
        specs = json.loads(config_path.read_text(encoding="utf-8"))[
            "mcpServers"
        ]
        references = {}
        for name, spec in specs.items():
            url_name = spec["url"][2:-1]
            token_name = spec["headers"]["Authorization"][9:-1]
            references[name] = (url_name, token_name)
            assert profile.env[url_name] == servers[name].url
            assert profile.env[token_name] == servers[name].bearer_token
        assert len({value for pair in references.values() for value in pair}) == 4
        assert profile.env[sentinel_name] == "preserve-existing-value"
        assert references["a-b"][0] != sentinel_name
    finally:
        profile.cleanup()


@pytest.mark.parametrize("provider", ["codex", "grok"])
def test_provider_mcp_token_environment_names_are_collision_free(
        tmp_path, provider):
    """Generated HTTP secrets must not collide with each other or stdio."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment, _source_home = _profile_env(tmp_path, provider)
    stdio_name = "COOP_MCP_A_B_BEARER_TOKEN"
    environment[stdio_name] = "ambient-value"
    servers = {
        "stdio": ExternalServer(
            name="stdio",
            transport="stdio",
            command="mcp-server",
            args=(),
            url=None,
            env_values=((stdio_name, "stdio-secret"),),
            bearer_token=None,
        ),
        **{
            name: ExternalServer(
                name=name,
                transport="http",
                command=None,
                args=(),
                url=f"https://{number}.invalid/mcp",
                env_values=(),
                bearer_token=f"token-{number}",
            )
            for name, number in (("a-b", "one"), ("a_b", "two"))
        },
    }
    manifest = dataclasses.replace(
        MANIFESTS["knowledge_recall"],
        external_servers=tuple(servers),
    )

    profile = build_launch_profile(
        provider,
        BASE_ARGV[provider],
        manifest,
        env=environment,
        workspace=workspace,
        run_dir=workspace / ".coop" / ".coop-runs",
        servers=servers,
    )
    try:
        references = {}
        if provider == "codex":
            overrides = [
                profile.argv[index + 1]
                for index, value in enumerate(profile.argv[:-1])
                if value == "-c"
            ]
            for name in ("a-b", "a_b"):
                prefix = f"mcp_servers.{name}.bearer_token_env_var="
                rendered = next(
                    value for value in overrides if value.startswith(prefix)
                )
                references[name] = json.loads(rendered[len(prefix):])
        else:
            config = (
                Path(profile.env["GROK_HOME"]) / "config.toml"
            ).read_text(encoding="utf-8")
            for name in ("a-b", "a_b"):
                section = config.split(
                    f"[mcp_servers.{name}]\n", 1
                )[1].split("\n[mcp_servers.", 1)[0]
                references[name] = section.split(
                    "Bearer ${", 1
                )[1].split("}", 1)[0]

        assert references["a-b"] != references["a_b"]
        assert stdio_name not in references.values()
        assert profile.env[stdio_name] == "stdio-secret"
        assert profile.env[references["a-b"]] == "token-one"
        assert profile.env[references["a_b"]] == "token-two"
    finally:
        profile.cleanup()


def test_grok_accepts_only_owned_external_runtime_state(tmp_path):
    """Persistent state is valid only when marked under the private root."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {"COOP_RUNTIME_ROOT": str(tmp_path / "private-runtime")}
    source_home = tmp_path / "grok-source"
    source_home.mkdir()
    env["GROK_HOME"] = str(source_home)
    state = coop_runtime.create_private_directory(
        workspace, "worker-state-grok", env=env
    )
    profile = build_launch_profile(
        "grok",
        BASE_ARGV["grok"],
        MANIFESTS["board_core"],
        env=env,
        workspace=workspace,
        run_dir=workspace / ".coop" / ".coop-runs",
        provider_state_dir=state.path,
    )
    try:
        assert Path(profile.env["GROK_HOME"]) == state.path
    finally:
        profile.cleanup()
        state.cleanup()


def test_runtime_override_inside_workspace_fails_with_nested_run_dir(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text("fake-auth", encoding="utf-8")
    inside_workspace = workspace / "private-cache"

    with pytest.raises(
        CapabilityActivationError,
        match="private launch artifacts",
    ):
        build_launch_profile(
            "codex",
            BASE_ARGV["codex"],
            MANIFESTS["board_core"],
            env={
                "CODEX_HOME": str(source_home),
                "COOP_RUNTIME_ROOT": str(inside_workspace),
            },
            workspace=workspace,
            run_dir=workspace / ".coop" / ".coop-runs",
        )
    assert not inside_workspace.exists()
