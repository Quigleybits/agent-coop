"""Pure action-scoped capability selection.

This module decides the smallest capability manifest for one structured board
action. It does not choose a provider, inspect prose, start tools, or load MCP
configuration.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType

from agent_coop import coop_runtime


CAPABILITY_NAMES = (
    "board_core",
    "local_code",
    "deep_review",
    "research_web",
    "browser_interactive",
    "knowledge_recall",
)

BOARD_ACTIONS = frozenset({
    "huddle_post",
    "huddle_close",
    "respond_handoff",
    "open_huddle",
    "request_review",
    "complete_task",
})

DEEP_REVIEW_ACTIONS = frozenset({"review_task"})

LOCAL_CODE_ACTIONS = frozenset({
    "claim_task",
    "recover_claim",
    "resume_task",
    "continue_task",
    "define_contract",
    "refine_contract",
    "answer_question",
})

_EXTERNAL_CAPABILITIES = frozenset({
    "research_web",
    "browser_interactive",
    "knowledge_recall",
})

_DENIED_NEXT_ACTION = (
    "revise the item contract to name one supported capability"
)

_SAFE_SERVER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONFIG_TOP_LEVEL_KEYS = frozenset({
    "version",
    "capabilities",
    "servers",
})
_SERVER_KEYS = frozenset({
    "transport",
    "command",
    "args",
    "env_from",
    "url_from",
    "bearer_token_from",
})
_LITERAL_SECRET_FIELDS = frozenset({
    "url",
    "token",
    "headers",
    "env",
})


class CapabilityConfigError(ValueError):
    """Local capability configuration is unsafe or malformed."""


class CapabilityActivationError(RuntimeError):
    """A provider cannot enforce the requested capability boundary."""


@dataclasses.dataclass(frozen=True)
class CapabilityManifest:
    name: str
    builtin_tools: tuple[str, ...]
    external_servers: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    manifest: CapabilityManifest | None
    classification: str | None = None
    legal_next_action: str | None = None


@dataclasses.dataclass(frozen=True, repr=False)
class ExternalServer:
    """Resolved server launch data whose representation never exposes values."""

    name: str
    transport: str
    command: str | None
    args: tuple[str, ...]
    url: str | None
    env_values: tuple[tuple[str, str], ...]
    bearer_token: str | None


@dataclasses.dataclass(repr=False)
class LaunchProfile:
    """One isolated provider invocation plus its private artifact cleanup."""

    argv: list[str]
    env: dict[str, str]
    external_server_names: tuple[str, ...]
    cleanup: Callable[[], None]


MANIFESTS = {
    "board_core": CapabilityManifest(
        name="board_core",
        builtin_tools=("board",),
    ),
    "local_code": CapabilityManifest(
        name="local_code",
        builtin_tools=("board", "local_files", "local_shell"),
    ),
    "deep_review": CapabilityManifest(
        name="deep_review",
        builtin_tools=("board", "local_files", "local_shell"),
    ),
    "research_web": CapabilityManifest(
        name="research_web",
        builtin_tools=(
            "board",
            "local_files",
            "local_shell",
            "web_search",
            "web_fetch",
        ),
    ),
    "browser_interactive": CapabilityManifest(
        name="browser_interactive",
        builtin_tools=(
            "board",
            "local_files",
            "local_shell",
            "browser",
        ),
    ),
    "knowledge_recall": CapabilityManifest(
        name="knowledge_recall",
        builtin_tools=(
            "board",
            "local_files",
            "local_shell",
            "knowledge_recall",
        ),
    ),
}

PROVIDER_TOOLS = MappingProxyType({
    "claude": MappingProxyType({
        "board_core": ("Bash", "Read", "Glob", "Grep"),
        "local_code": (
            "Bash",
            "Read",
            "Edit",
            "Write",
            "Glob",
            "Grep",
        ),
        "deep_review": ("Bash", "Read", "Glob", "Grep"),
        "research_web": (
            "Bash",
            "Read",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
        ),
    }),
    "grok": MappingProxyType({
        "board_core": (
            "bash",
            "read_file",
            "grep_search",
            "list_dir",
        ),
        "local_code": (
            "bash",
            "read_file",
            "search_replace",
            "grep_search",
            "list_dir",
        ),
        "deep_review": (
            "bash",
            "read_file",
            "grep_search",
            "list_dir",
        ),
        "research_web": (
            "bash",
            "read_file",
            "grep_search",
            "list_dir",
            "web_search",
            "web_fetch",
        ),
    }),
})

_GROK_ALWAYS_DENIED = (
    "task",
    "memory_search",
    "memory_get",
)
_GROK_EXTERNAL_TOOLS = ("search_tool", "use_tool")


def _configured_manifest(
        manifest_name: str,
        config) -> CapabilityManifest:
    manifest = MANIFESTS[manifest_name]
    if manifest_name not in _EXTERNAL_CAPABILITIES:
        return manifest
    if not isinstance(config, Mapping):
        return manifest
    capabilities = config.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return manifest
    external_servers = capabilities.get(manifest_name, ())
    if not isinstance(external_servers, tuple):
        return manifest
    return dataclasses.replace(
        manifest,
        external_servers=external_servers,
    )


def _allowed(manifest_name: str, config=None) -> CapabilityDecision:
    return CapabilityDecision(
        allowed=True,
        manifest=_configured_manifest(manifest_name, config),
    )


def _denied() -> CapabilityDecision:
    return CapabilityDecision(
        allowed=False,
        manifest=None,
        classification="capability_denied",
        legal_next_action=_DENIED_NEXT_ACTION,
    )


def _capability_tags(item) -> tuple[str, ...]:
    if not isinstance(item, Mapping):
        return ()
    values = item.get("allowed_actions", ())
    if isinstance(values, str):
        values = (values,)
    if values is None:
        return ()
    try:
        tags = (
            value.removeprefix("capability:")
            for value in values
            if isinstance(value, str)
            and value.startswith("capability:")
        )
        return tuple(dict.fromkeys(tags))
    except TypeError:
        return ()


def _config_error(message: str) -> CapabilityConfigError:
    return CapabilityConfigError(
        f"invalid local capability configuration: {message}"
    )


def _mapping(value, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise _config_error(f"{label} must be an object")
    return value


def _validate_env_name(value, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ENV_NAME.fullmatch(value):
        raise _config_error(
            f"{label} must name a valid environment variable"
        )
    return value


def _validate_string_list(value, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
            not isinstance(entry, str) for entry in value):
        raise _config_error(f"{label} must be an array of strings")
    return tuple(value)


def _validate_server_schema(name: str, raw_spec) -> dict:
    if not isinstance(name, str) or not _SAFE_SERVER_NAME.fullmatch(name):
        raise _config_error(f"unsafe server name {name!r}")
    spec = _mapping(raw_spec, f"server {name!r}")

    literal_fields = sorted(_LITERAL_SECRET_FIELDS.intersection(spec))
    if literal_fields:
        raise _config_error(
            f"server {name!r} contains forbidden literal field "
            f"{literal_fields[0]!r}"
        )
    unknown = sorted(set(spec).difference(_SERVER_KEYS))
    if unknown:
        raise _config_error(
            f"server {name!r} contains unknown field {unknown[0]!r}"
        )

    has_command = "command" in spec
    has_url_reference = "url_from" in spec
    if has_command and has_url_reference:
        raise _config_error(
            f"server {name!r} cannot define both command and url_from"
        )

    transport = spec.get("transport")
    if transport not in {"stdio", "http"}:
        raise _config_error(
            f"server {name!r} transport must be 'stdio' or 'http'"
        )

    if transport == "stdio":
        command = spec.get("command")
        if not isinstance(command, str) or not command.strip():
            raise _config_error(
                f"stdio server {name!r} requires a command"
            )
        forbidden = sorted(
            {"url_from", "bearer_token_from"}.intersection(spec)
        )
        if forbidden:
            raise _config_error(
                f"stdio server {name!r} cannot define {forbidden[0]}"
            )
        args = _validate_string_list(
            spec.get("args", []),
            f"server {name!r} args",
        )
        env_from = _validate_string_list(
            spec.get("env_from", []),
            f"server {name!r} env_from",
        )
        env_names = tuple(
            _validate_env_name(
                env_name,
                f"server {name!r} environment variable",
            )
            for env_name in env_from
        )
        return {
            "transport": transport,
            "command": command,
            "args": args,
            "env_from": env_names,
            "url_from": None,
            "bearer_token_from": None,
        }

    forbidden = sorted({"command", "args", "env_from"}.intersection(spec))
    if forbidden:
        raise _config_error(
            f"http server {name!r} cannot define {forbidden[0]}"
        )
    url_from = _validate_env_name(
        spec.get("url_from"),
        f"server {name!r} url_from",
    )
    bearer_token_from = spec.get("bearer_token_from")
    if bearer_token_from is not None:
        bearer_token_from = _validate_env_name(
            bearer_token_from,
            f"server {name!r} bearer_token_from",
        )
    return {
        "transport": transport,
        "command": None,
        "args": (),
        "env_from": (),
        "url_from": url_from,
        "bearer_token_from": bearer_token_from,
    }


def _resolve_environment(
        name: str,
        spec: Mapping,
        environ: Mapping) -> ExternalServer:
    references = list(spec["env_from"])
    if spec["url_from"] is not None:
        references.append(spec["url_from"])
    if spec["bearer_token_from"] is not None:
        references.append(spec["bearer_token_from"])
    for reference in references:
        if (
                reference not in environ
                or not isinstance(environ[reference], str)
                or not environ[reference]
        ):
            raise _config_error(
                f"required environment variable {reference!r} is missing"
            )

    return ExternalServer(
        name=name,
        transport=spec["transport"],
        command=spec["command"],
        args=spec["args"],
        url=(
            environ[spec["url_from"]]
            if spec["url_from"] is not None
            else None
        ),
        env_values=tuple(
            (env_name, environ[env_name])
            for env_name in spec["env_from"]
        ),
        bearer_token=(
            environ[spec["bearer_token_from"]]
            if spec["bearer_token_from"] is not None
            else None
        ),
    )


def load_local_config(path, environ=None) -> dict:
    """Load an optional strict config, resolving only environment references."""
    config_path = Path(path)
    if not config_path.exists():
        return {
            "version": 1,
            "capabilities": {},
            "servers": {},
        }

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise _config_error(
            f"could not read {config_path.name!r}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise _config_error(
            f"{config_path.name!r} is not valid JSON"
        ) from exc

    root = _mapping(raw, "root")
    missing = sorted(_CONFIG_TOP_LEVEL_KEYS.difference(root))
    if missing:
        raise _config_error(
            f"missing top-level field {missing[0]!r}"
        )
    unknown = sorted(set(root).difference(_CONFIG_TOP_LEVEL_KEYS))
    if unknown:
        raise _config_error(
            f"unknown top-level field {unknown[0]!r}"
        )
    if root["version"] != 1 or isinstance(root["version"], bool):
        raise _config_error("unsupported version; expected integer 1")

    raw_capabilities = _mapping(
        root["capabilities"],
        "capabilities",
    )
    raw_servers = _mapping(root["servers"], "servers")

    normalized_servers = {
        name: _validate_server_schema(name, spec)
        for name, spec in raw_servers.items()
    }

    normalized_capabilities = {}
    for capability, server_names in raw_capabilities.items():
        if capability not in _EXTERNAL_CAPABILITIES:
            raise _config_error(
                f"unknown or core capability {capability!r}"
            )
        names = _validate_string_list(
            server_names,
            f"capability {capability!r}",
        )
        unique_names = tuple(dict.fromkeys(names))
        for server_name in unique_names:
            if not _SAFE_SERVER_NAME.fullmatch(server_name):
                raise _config_error(
                    f"unsafe server name {server_name!r}"
                )
            if server_name not in normalized_servers:
                raise _config_error(
                    f"capability {capability!r} references missing "
                    f"server {server_name!r}"
                )
        normalized_capabilities[capability] = unique_names

    environment = os.environ if environ is None else environ
    if not isinstance(environment, Mapping):
        raise _config_error("environ must be a mapping")
    resolved_servers = {
        name: _resolve_environment(name, spec, environment)
        for name, spec in normalized_servers.items()
    }
    return {
        "version": 1,
        "capabilities": normalized_capabilities,
        "servers": resolved_servers,
    }


class _RunArtifacts:
    """Private artifacts rooted outside the selected workspace."""

    def __init__(self, workspace, *, env=None):
        self.workspace = Path(workspace).resolve()
        self._owned = coop_runtime.create_private_directory(
            self.workspace,
            "launch-profile",
            env=env,
        )
        self.root = self._owned.path
        self._cleaned = False

    def directory(self, name: str) -> Path:
        return self._owned.directory(name)

    def write_text(self, name: str, content: str) -> Path:
        return self.write_bytes(name, content.encode("utf-8"))

    def write_bytes(self, name: str, content: bytes) -> Path:
        return self._owned.write_bytes(name, content)

    def write_home_text(
            self,
            home: Path,
            name: str,
            content: str) -> Path:
        path = home / name
        return coop_runtime._write_new_file(
            path,
            content.encode("utf-8"),
        )

    def replace_home_text(
            self,
            home: Path,
            name: str,
            content: str) -> Path:
        path = home / name
        temporary = home / f".{name}.{uuid.uuid4().hex}.tmp"
        coop_runtime._write_new_file(
            temporary,
            content.encode("utf-8"),
        )
        try:
            os.replace(temporary, path)
            coop_runtime._secure_permissions(path, directory=False)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return path

    def copy_private_file(
            self,
            source: Path,
            destination: Path) -> None:
        coop_runtime._write_new_file(destination, source.read_bytes())

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._owned.cleanup()
        self._cleaned = True


def _activation_error(message: str) -> CapabilityActivationError:
    return CapabilityActivationError(
        f"capability activation failed: {message}"
    )


def _tools_for(provider: str, manifest: CapabilityManifest) -> tuple[str, ...]:
    provider_tools = PROVIDER_TOOLS[provider]
    manifest_name = (
        manifest.name
        if manifest.name in provider_tools
        else "local_code"
    )
    return provider_tools[manifest_name]


def _selected_servers(
        manifest: CapabilityManifest,
        servers) -> tuple[ExternalServer, ...]:
    names = manifest.external_servers
    if not names:
        return ()
    if not isinstance(servers, Mapping):
        raise _activation_error(
            "resolved external server registry is unavailable"
        )
    selected = []
    for name in names:
        server = servers.get(name)
        if not isinstance(server, ExternalServer) or server.name != name:
            raise _activation_error(
                f"requested server {name!r} is missing"
            )
        selected.append(server)
    return tuple(selected)


def _set_server_environment(
        environment: dict[str, str],
        selected: tuple[ExternalServer, ...]) -> None:
    for server in selected:
        for name, value in server.env_values:
            environment[name] = value


def _copy_optional_auth(
        artifacts: _RunArtifacts,
        source_home: Path,
        isolated_home: Path,
        *,
        api_key_present: bool) -> None:
    if api_key_present:
        return
    source = source_home / "auth.json"
    if not source.is_file():
        return
    try:
        artifacts.copy_private_file(
            source,
            isolated_home / "auth.json",
        )
    except OSError as exc:
        raise _activation_error(
            "could not prepare isolated provider authentication"
        ) from exc


def _reserve_mcp_environment(
        environment: dict[str, str],
        server_name: str,
        suffix: str,
        value: str) -> str:
    encoded_name = server_name.encode("utf-8").hex().upper()
    base = f"COOP_MCP_{encoded_name}{suffix}"
    occupied = {name.upper() for name in environment}
    name = base
    counter = 2
    while name.upper() in occupied:
        name = f"{base}_{counter}"
        counter += 1
    environment[name] = value
    return name


def _claude_server_spec(
        server: ExternalServer,
        environment: dict[str, str]) -> dict:
    if server.transport == "stdio":
        spec = {
            "command": server.command,
            "args": list(server.args),
        }
        if server.env_values:
            spec["env"] = {
                name: "${" + name + "}"
                for name, _value in server.env_values
            }
        return spec
    url_name = _reserve_mcp_environment(
        environment,
        server.name,
        "_URL",
        server.url,
    )
    spec = {
        "type": "http",
        "url": "${" + url_name + "}",
    }
    if server.bearer_token is not None:
        token_name = _reserve_mcp_environment(
            environment,
            server.name,
            "_BEARER_TOKEN",
            server.bearer_token,
        )
        spec["headers"] = {
            "Authorization": "Bearer ${" + token_name + "}",
        }
    return spec


def _build_claude_profile(
        argv: list[str],
        environment: dict[str, str],
        manifest: CapabilityManifest,
        selected: tuple[ExternalServer, ...],
        artifacts_factory) -> None:
    argv.extend([
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
        "--tools",
        ",".join(_tools_for("claude", manifest)),
        "--strict-mcp-config",
    ])
    if not selected:
        return
    artifacts = artifacts_factory()
    payload = {
        "mcpServers": {
            server.name: _claude_server_spec(server, environment)
            for server in selected
        },
    }
    path = artifacts.write_text(
        f"claude-mcp-{uuid.uuid4().hex}.json",
        json.dumps(payload, separators=(",", ":")),
    )
    argv.extend(["--mcp-config", str(path)])


def _codex_override(key: str, value) -> list[str]:
    rendered = (
        value
        if isinstance(value, str) and value in {"true", "false"}
        else json.dumps(value, separators=(",", ":"))
    )
    return ["-c", f"{key}={rendered}"]


def _build_codex_profile(
        argv: list[str],
        environment: dict[str, str],
        manifest: CapabilityManifest,
        selected: tuple[ExternalServer, ...],
        artifacts_factory) -> None:
    artifacts = artifacts_factory()
    source_home = Path(
        environment.get("CODEX_HOME")
        or (Path.home() / ".codex")
    )
    isolated_home = artifacts.directory("codex-home")
    _copy_optional_auth(
        artifacts,
        source_home,
        isolated_home,
        api_key_present=bool(environment.get("OPENAI_API_KEY")),
    )
    environment["CODEX_HOME"] = str(isolated_home)

    # Current Codex has no ignore-user-config flag. A fresh CODEX_HOME is the
    # supported isolation boundary; strict config makes malformed overrides
    # fail instead of silently widening the turn.
    argv[1:1] = ["--strict-config"]
    if manifest.name == "research_web":
        argv[2:2] = ["--search"]
    else:
        argv.extend(_codex_override("web_search", "disabled"))
    argv.extend(_codex_override("features.hooks", "false"))
    argv.extend(_codex_override("features.apps", "false"))
    argv.extend(_codex_override("features.plugins", "false"))
    argv.extend(_codex_override("agents.enabled", "false"))

    for server in selected:
        prefix = f"mcp_servers.{server.name}"
        argv.extend(_codex_override(f"{prefix}.required", "true"))
        if server.transport == "stdio":
            argv.extend(
                _codex_override(f"{prefix}.command", server.command)
            )
            argv.extend(
                _codex_override(f"{prefix}.args", list(server.args))
            )
            if server.env_values:
                argv.extend(
                    _codex_override(
                        f"{prefix}.env_vars",
                        [name for name, _value in server.env_values],
                    )
                )
        else:
            argv.extend(_codex_override(f"{prefix}.url", server.url))
            if server.bearer_token is not None:
                env_name = _reserve_mcp_environment(
                    environment,
                    server.name,
                    "_BEARER_TOKEN",
                    server.bearer_token,
                )
                argv.extend(
                    _codex_override(
                        f"{prefix}.bearer_token_env_var",
                        env_name,
                    )
                )


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _grok_config(
        selected: tuple[ExternalServer, ...],
        environment: dict[str, str]) -> str:
    lines = [
        "disable_plugins = true",
        "",
        "[subagents]",
        "enabled = false",
    ]
    for vendor in ("cursor", "claude"):
        lines.extend([
            "",
            f"[compat.{vendor}]",
            "skills = false",
            "rules = false",
            "agents = false",
            "mcps = false",
            "hooks = false",
            "sessions = false",
        ])
    for server in selected:
        lines.extend(["", f"[mcp_servers.{server.name}]"])
        if server.transport == "stdio":
            lines.append(f"command = {_toml_string(server.command)}")
            lines.append(
                "args = "
                + json.dumps(
                    list(server.args),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            if server.env_values:
                entries = []
                for name, _value in server.env_values:
                    entries.append(
                        f"{name} = {_toml_string('${' + name + '}')}"
                    )
                lines.append("env = { " + ", ".join(entries) + " }")
        else:
            lines.append(f"url = {_toml_string(server.url)}")
            if server.bearer_token is not None:
                env_name = _reserve_mcp_environment(
                    environment,
                    server.name,
                    "_BEARER_TOKEN",
                    server.bearer_token,
                )
                authorization = f"Bearer ${{{env_name}}}"
                lines.append(
                    "headers = { Authorization = "
                    + _toml_string(authorization)
                    + " }"
                )
        lines.append("enabled = true")
    return "\n".join(lines) + "\n"


def _grok_agent_profile(
        tools: tuple[str, ...],
        denied: tuple[str, ...]) -> str:
    lines = [
        "---",
        "name: coop-action",
        "description: Action-scoped Agent Co-op worker.",
        "prompt_mode: full",
        "permission_mode: default",
        "agents_md: false",
        "tools:",
    ]
    lines.extend(f"  - {tool}" for tool in tools)
    lines.append("disallowedTools:")
    lines.extend(f"  - {tool}" for tool in denied)
    lines.extend([
        "---",
        "",
        "Follow only the supplied Agent Co-op turn prompt.",
        "",
    ])
    return "\n".join(lines)


def _prefer_native_grok(
        argv: list[str],
        source_home: Path) -> None:
    executable = Path(argv[0])
    if os.name != "nt":
        return
    if executable.suffix.lower() not in {"", ".cmd", ".bat"}:
        return
    native = source_home / "bin" / "grok.exe"
    if not native.is_file():
        raise _activation_error(
            "the Windows Grok shim cannot preserve run-local isolation"
        )
    argv[0] = str(native)


def _insert_before_single_prompt(
        argv: list[str],
        arguments: list[str]) -> None:
    try:
        position = argv.index("-p")
    except ValueError:
        try:
            position = argv.index("--single")
        except ValueError:
            position = len(argv)
    argv[position:position] = arguments


def _build_grok_profile(
        argv: list[str],
        environment: dict[str, str],
        manifest: CapabilityManifest,
        selected: tuple[ExternalServer, ...],
        artifacts_factory,
        provider_state_dir=None) -> None:
    artifacts = artifacts_factory()
    source_home = Path(
        environment.get("GROK_HOME")
        or (Path.home() / ".grok")
    )
    _prefer_native_grok(argv, source_home)
    isolated_home = (
        Path(provider_state_dir).resolve()
        if provider_state_dir is not None
        else artifacts.directory("grok-home")
    )
    if not isolated_home.is_dir():
        raise _activation_error("provider state directory is unavailable")
    if not (isolated_home / "auth.json").exists():
        _copy_optional_auth(
            artifacts,
            source_home,
            isolated_home,
            api_key_present=bool(environment.get("XAI_API_KEY")),
        )
    environment.update({
        "GROK_HOME": str(isolated_home),
        "GROK_MEMORY": "0",
        "GROK_SUBAGENTS": "0",
        "GROK_WORKFLOWS": "0",
        "GROK_DISABLE_AUTOUPDATER": "1",
        "GROK_CURSOR_SKILLS_ENABLED": "0",
        "GROK_CURSOR_RULES_ENABLED": "0",
        "GROK_CURSOR_AGENTS_ENABLED": "0",
        "GROK_CURSOR_MCPS_ENABLED": "0",
        "GROK_CURSOR_HOOKS_ENABLED": "0",
        "GROK_CLAUDE_SKILLS_ENABLED": "0",
        "GROK_CLAUDE_RULES_ENABLED": "0",
        "GROK_CLAUDE_AGENTS_ENABLED": "0",
        "GROK_CLAUDE_MCPS_ENABLED": "0",
        "GROK_CLAUDE_HOOKS_ENABLED": "0",
    })

    tools = _tools_for("grok", manifest)
    denied = list(_GROK_ALWAYS_DENIED)
    if selected:
        tools = tools + _GROK_EXTERNAL_TOOLS
    else:
        denied.extend(_GROK_EXTERNAL_TOOLS)
    if manifest.name != "research_web":
        denied.extend(("web_search", "web_fetch"))

    config = _grok_config(selected, environment)
    config_path = isolated_home / "config.toml"
    try:
        if config_path.exists():
            artifacts.replace_home_text(
                isolated_home,
                "config.toml",
                config,
            )
        else:
            artifacts.write_home_text(
                isolated_home,
                "config.toml",
                config,
            )
        if config_path.read_text(encoding="utf-8") != config:
            raise _activation_error(
                "could not establish run-scoped Grok configuration"
            )
    except CapabilityActivationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _activation_error(
            "could not establish run-scoped Grok configuration"
        ) from exc
    agent_path = artifacts.write_text(
        f"grok-agent-{uuid.uuid4().hex}.md",
        _grok_agent_profile(tools, tuple(denied)),
    )
    if "agent" in argv:
        # ACP agent options belong after the `agent` subcommand and before
        # the transport name.  The one-shot `--agent/--tools` flags below are
        # a different CLI surface and are rejected by `grok agent stdio`.
        agent_position = argv.index("agent")
        transport_modes = {"stdio", "serve", "headless", "leader"}
        transport_positions = [
            index
            for index, value in enumerate(argv)
            if index > agent_position and value in transport_modes
        ]
        if not transport_positions:
            raise _activation_error(
                "Grok agent transport is unavailable"
            )
        transport_position = min(transport_positions)
        argv[transport_position:transport_position] = [
            "--always-approve",
            "--no-leader",
            "--agent-profile",
            str(agent_path),
        ]
        return
    arguments = [
        "--agent",
        str(agent_path),
        "--tools",
        ",".join(tools),
        "--disallowed-tools",
        ",".join(denied),
        "--no-memory",
        "--no-subagents",
        "--no-auto-update",
    ]
    if manifest.name != "research_web":
        arguments.append("--disable-web-search")
    _insert_before_single_prompt(argv, arguments)


def build_launch_profile(
        provider,
        base_argv,
        manifest,
        *,
        env,
        workspace,
        run_dir,
        servers=None,
        provider_state_dir=None) -> LaunchProfile:
    """Construct a minimal, action-scoped provider invocation.

    Resolved server values stay in the child environment or owner-private
    run files. The returned profile exposes only selected server names.
    """
    if provider not in {"claude", "codex", "grok"}:
        raise _activation_error(f"unknown provider {provider!r}")
    if not isinstance(manifest, CapabilityManifest):
        raise _activation_error("capability manifest is unavailable")
    if not isinstance(env, Mapping):
        raise _activation_error("provider environment is unavailable")
    if provider_state_dir is not None:
        state_path = Path(provider_state_dir).resolve()
        if (
            provider != "grok"
            or not coop_runtime.is_owned_runtime_directory(
                state_path,
                workspace,
                "worker-state-grok",
                env=env,
            )
        ):
            raise _activation_error(
                "provider state directory is not owned private runtime state"
            )
    argv = [str(value) for value in base_argv]
    if not argv:
        raise _activation_error("provider command is empty")
    environment = {
        str(name): str(value)
        for name, value in env.items()
    }
    selected = _selected_servers(manifest, servers)
    _set_server_environment(environment, selected)

    artifacts = None

    def artifacts_factory():
        nonlocal artifacts
        if artifacts is None:
            try:
                artifacts = _RunArtifacts(workspace, env=environment)
            except (OSError, coop_runtime.RuntimeSecurityError) as exc:
                raise _activation_error(
                    "could not create private launch artifacts"
                ) from exc
        return artifacts

    def cleanup():
        if artifacts is not None:
            artifacts.cleanup()

    try:
        if provider == "claude":
            _build_claude_profile(
                argv,
                environment,
                manifest,
                selected,
                artifacts_factory,
            )
        elif provider == "codex":
            _build_codex_profile(
                argv,
                environment,
                manifest,
                selected,
                artifacts_factory,
            )
        else:
            _build_grok_profile(
                argv,
                environment,
                manifest,
                selected,
                artifacts_factory,
                provider_state_dir=provider_state_dir,
            )
    except Exception:
        cleanup()
        raise

    return LaunchProfile(
        argv=argv,
        env=environment,
        external_server_names=tuple(
            server.name for server in selected
        ),
        cleanup=cleanup,
    )


def resolve_capability(action, item, config=None) -> CapabilityDecision:
    """Resolve one structured action without inspecting task prose or roles."""
    kind = action.get("kind") if isinstance(action, Mapping) else None

    if kind == "idle" or kind in BOARD_ACTIONS:
        return _allowed("board_core", config)
    if kind in DEEP_REVIEW_ACTIONS:
        return _allowed("deep_review", config)
    if kind not in LOCAL_CODE_ACTIONS:
        return _denied()

    tags = _capability_tags(item)
    if any(tag not in CAPABILITY_NAMES for tag in tags):
        return _denied()
    external = tuple(
        tag for tag in tags if tag in _EXTERNAL_CAPABILITIES)
    if len(external) > 1:
        return _denied()
    if external:
        return _allowed(external[0], config)
    return _allowed("local_code", config)


__all__ = [
    "BOARD_ACTIONS",
    "CAPABILITY_NAMES",
    "DEEP_REVIEW_ACTIONS",
    "LOCAL_CODE_ACTIONS",
    "MANIFESTS",
    "PROVIDER_TOOLS",
    "CapabilityActivationError",
    "CapabilityConfigError",
    "CapabilityDecision",
    "CapabilityManifest",
    "ExternalServer",
    "LaunchProfile",
    "build_launch_profile",
    "load_local_config",
    "resolve_capability",
]
