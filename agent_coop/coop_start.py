"""Agent Co-op orchestrator — CORE (pure, injectable).

This module holds the deterministic heart of `coop start`: resolve which
providers can participate, compose each agent's bootstrap prompt, and drive
the bounded round-robin over injected callables.

Deliberately inert: nothing here launches an agent, opens a board, inserts a
session, or touches the dashboard. The real turn runner (session insert +
headless CLI invoke) and the `/coop` slash-command are separate modules.
Every function is pure or pure-over-injected-callables so the whole core is
unit-tested without a single real process.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
# resolve_cli never calls shutil.which (it searches cwd first on Windows);
# the module stays importable as coop_start.shutil for existing test seams.
import shutil  # noqa: F401
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping

from agent_coop import coop_decisions
from agent_coop import coop_process
from agent_coop import coop_prompt_cache
from agent_coop import coop_provider_failures
from agent_coop import coop_runtime
from agent_coop import coop_workers
from agent_coop import coopdb
from agent_coop.coop_capabilities import (
    MANIFESTS,
    build_launch_profile,
)

# Headless invocation templates for the LATER real runner (step 4). The turn
# runner appends the composed prompt string; nothing in this module calls
# these — they are data, kept here so the provider set has one home.
# argv[0] is a bare name; resolve_cli() pins the full path at probe/invoke
# time (ENV/PATH — shell-independent, including Windows .cmd shims).
PROVIDER_INVOKE = {
    "claude": ["claude", "-p", "--dangerously-skip-permissions"],
    # --sandbox danger-full-access (was workspace-write): codex's OS sandbox
    # cannot initialize when codex is spawned NESTED inside another sandbox
    # (autonomous runner launched from Claude Code) — "every shell command fails
    # during sandbox initialization". danger-full-access skips codex's own
    # sandbox so it runs nested. Tradeoff: codex gets full shell access;
    # acceptable on a trusted local repo. --skip-git-repo-check lets it run in
    # a non-git dir. (workspace-write works when codex is not nested.)
    # invoke_turn also supplies run-local shell_environment_policy.set.COOP_*
    # overrides so a user's inherit="core" policy cannot strip board identity
    # from model-generated shell commands.
    "codex": ["codex", "exec", "--sandbox", "danger-full-access",
              "--skip-git-repo-check"],
    "grok": ["grok", "--always-approve", "-p"],
}

DEFAULT_AGENTS = ["claude", "codex", "grok"]
POST_WRITE_EXIT_GRACE_S = 20.0

# Structured answers: the model
# returns a schema-constrained {question_id, answer} value and the RUNNER
# performs the canonical `question answer` postcommit under the agent's
# bound session. answer_question only; codex keeps tool turns until its
# app-server strict-schema support is verified.
STRUCTURED_ANSWER_PROVIDERS = frozenset({"claude", "grok"})
STRUCTURED_DECISION_PROVIDERS = frozenset({"claude"})
STRUCTURED_ANSWER_SCHEMA = (
    '{"type":"object","properties":{"question_id":{"type":"integer"},'
    '"answer":{"type":"string","minLength":1}},'
    '"required":["question_id","answer"],"additionalProperties":false}'
)

STRUCTURED_ENV_EXACT = frozenset({
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "USERNAME",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
})
PROVIDER_ENV_PREFIXES = {
    "claude": ("ANTHROPIC_", "CLAUDE_"),
    "codex": ("OPENAI_", "CODEX_"),
    "grok": ("XAI_", "GROK_"),
}
PROVIDER_ENV_EXACT = {
    "claude": frozenset({
        "CLOUD_ML_REGION",
        "MAX_THINKING_TOKENS",
        "MCP_TIMEOUT",
        "MCP_TOOL_TIMEOUT",
        "MAX_MCP_OUTPUT_TOKENS",
        "BASH_DEFAULT_TIMEOUT_MS",
        "BASH_MAX_TIMEOUT_MS",
        "BASH_MAX_OUTPUT_LENGTH",
        "API_TIMEOUT_MS",
        "USE_BUILTIN_RIPGREP",
    }),
    "codex": frozenset(),
    "grok": frozenset(),
}
# Cloud backends for Claude: those credentials pass only when Claude has
# been told to use that backend, so an idle AWS/GCP/Azure profile in the
# operator's shell never reaches a provider child.
CLOUD_BACKEND_ENV_PREFIXES = (
    ("CLAUDE_CODE_USE_BEDROCK", ("AWS_",)),
    ("CLAUDE_CODE_USE_VERTEX", ("GOOGLE_", "GCLOUD_", "CLOUDSDK_")),
    ("CLAUDE_CODE_USE_FOUNDRY", ("AZURE_",)),
)


def _flag_enabled(value):
    return str(value or "").strip().lower() not in {
        "", "0", "false", "no", "off",
    }


def _provider_env_key(provider):
    return provider if isinstance(provider, str) else None


def _cloud_backend_prefixes(source, provider=None):
    if _provider_env_key(provider) != "claude":
        return ()
    return tuple(
        prefix
        for flag, prefixes in CLOUD_BACKEND_ENV_PREFIXES
        if _flag_enabled(source.get(flag))
        for prefix in prefixes
    )


def structured_answer_env(source=None, *, provider=None):
    """Minimal selected-provider environment without Co-op workspace state.

    An omitted or unknown provider keeps platform/runtime values only.
    """
    source = os.environ if source is None else source
    provider = _provider_env_key(provider)
    prefixes = (
        PROVIDER_ENV_PREFIXES.get(provider, ())
        + _cloud_backend_prefixes(source, provider)
    )
    exact = STRUCTURED_ENV_EXACT | PROVIDER_ENV_EXACT.get(
        provider,
        frozenset(),
    )
    isolated = {}
    for key, value in source.items():
        normalized = str(key).upper()
        if (
                normalized in exact
                or normalized.startswith(prefixes)):
            isolated[str(key)] = str(value)
    return isolated


# Tool turns run with permission checks off inside a workspace whose
# instruction files may be hostile, so the operator's shell secrets must not
# be one `printenv` away from the model or from any MCP server it spawns.
# Platform and provider configuration passes; credentials pass only under a
# provider's own prefix (the provider cannot run without them). Names are
# compared upper-cased.
TOOL_TURN_ENV_EXACT = STRUCTURED_ENV_EXACT | frozenset({
    # Windows platform
    "SYSTEMDRIVE", "ALLUSERSPROFILE", "PUBLIC", "COMPUTERNAME", "USERDOMAIN",
    "USERDOMAIN_ROAMINGPROFILE", "LOGONSERVER", "HOMESHARE", "SESSIONNAME",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "OS", "PSMODULEPATH",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "DRIVERDATA",
    "NODEFAULTCURRENTDIRECTORYINEXEPATH", "MSYSTEM", "MSYS", "MINGW_PREFIX",
    # POSIX platform
    "USER", "SHELL", "TZ", "TMPDIR", "LANGUAGE", "HOSTNAME",
    "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "DISPLAY", "WAYLAND_DISPLAY",
    "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK",
    "SSH_AGENT_PID", "GPG_TTY",
    # terminal / tooling conventions
    "COLORTERM", "NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE",
    "WT_SESSION", "WT_PROFILE_ID", "EDITOR", "VISUAL", "PAGER", "BROWSER",
    "CI",
    # TLS trust
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # runtimes the three CLIs and the routed board commands run on
    "NODE_OPTIONS", "NODE_PATH", "NODE_ENV", "NODE_TLS_REJECT_UNAUTHORIZED",
    "NODE_NO_WARNINGS", "UV_THREADPOOL_SIZE",
    "PYTHONPATH", "PYTHONHOME", "PYTHONIOENCODING", "PYTHONUTF8",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE",
    "VIRTUAL_ENV", "RUST_LOG", "RUST_BACKTRACE",
})
# Configuration namespaces: pass unless the name is credential-shaped.
TOOL_TURN_CONFIG_PREFIXES = (
    "LC_", "XDG_", "GIT_", "NODE_", "PYTHON", "DISABLE_", "RUST_", "TERM_",
)
_CREDENTIAL_SHAPES = (
    "TOKEN", "SECRET", "PASSW", "CREDENTIAL", "API_KEY", "APIKEY",
    "ACCESS_KEY", "PRIVATE_KEY",
)


def _credential_shaped(name):
    return any(shape in name for shape in _CREDENTIAL_SHAPES) or \
        name.endswith("_KEY")


def tool_turn_env(source=None, *, provider=None, inherit=False):
    """The environment a tool-enabled provider child (or a routed board
    command) starts from. ``inherit=True`` restores full inheritance
    (``coop start --inherit-env``). An omitted or unknown provider carries no
    provider credential namespace."""
    source = os.environ if source is None else source
    if inherit:
        return {str(key): str(value) for key, value in source.items()}
    provider = _provider_env_key(provider)
    provider_prefixes = PROVIDER_ENV_PREFIXES.get(provider, ())
    provider_exact = PROVIDER_ENV_EXACT.get(provider, frozenset())
    cloud_prefixes = _cloud_backend_prefixes(source, provider)
    child = {}
    for key, value in source.items():
        name = str(key).upper()
        if name in TOOL_TURN_ENV_EXACT or name in provider_exact:
            pass
        elif name.startswith(("COOP_",)):
            pass
        elif provider_prefixes and name.startswith(provider_prefixes):
            pass
        elif cloud_prefixes and name.startswith(cloud_prefixes):
            pass
        elif (
                name.startswith(TOOL_TURN_CONFIG_PREFIXES)
                and not _credential_shaped(name)):
            pass
        else:
            continue
        child[str(key)] = str(value)
    return child


def _structured_child_env(base_env, inherit, provider):
    if inherit:
        source = os.environ if base_env is None else base_env
        return {str(key): str(value) for key, value in source.items()}
    return structured_answer_env(base_env, provider=provider)


def _prompt_runtime_env(source=None):
    """Use ambient private storage plus an explicit runtime-root override."""
    environment = dict(os.environ)
    if source is not None and "COOP_RUNTIME_ROOT" in source:
        environment["COOP_RUNTIME_ROOT"] = str(source["COOP_RUNTIME_ROOT"])
    return environment


def structured_decision_argv(request, *, resolve=None):
    """Workspace-free argv for an explicitly promoted bounded decision."""
    if (
            not isinstance(request, coop_decisions.DecisionRequest)
            or request.provider not in STRUCTURED_DECISION_PROVIDERS
            or request.session_class != "isolated_no_workspace"):
        return None
    resolve = resolve or resolve_cli
    resolved = resolve(PROVIDER_INVOKE[request.provider][0])
    if resolved is None:
        return None
    schema = json.dumps(
        request.json_schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # The prompt is NOT here: it travels on stdin (open_prompt_delivery), so
    # board-derived text never reaches a cmd.exe launcher's command line.
    return [
        resolved,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--tools",
        "",
        "--setting-sources",
        "",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--strict-mcp-config",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
    ]


def _structured_payload(stdout_text):
    try:
        envelope = json.loads((stdout_text or "").strip())
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    payload = envelope.get("structured_output")
    if not isinstance(payload, dict):
        payload = envelope.get("structuredOutput")
    return payload if isinstance(payload, dict) else None


def parse_structured_decision(stdout_text, *, request):
    """Validated bounded provider value, or ``None`` on any mismatch."""
    payload = _structured_payload(stdout_text)
    if payload is None:
        return None
    return coop_decisions.validate_decision_value(request, payload)


def invoke_structured_decision(
        *,
        request,
        cwd,
        timeout_s=120.0,
        runner=None,
        resolve=None,
        base_env=None,
        usage_callback=None,
        inherit_env=False):
    """Invoke one workspace-free decision without board identity or tools."""
    usage = {"usage_observation": "unobserved"}

    def report_usage():
        if not callable(usage_callback):
            return
        try:
            usage_callback(dict(usage))
        except Exception:
            pass

    try:
        runner = runner or subprocess.run
        argv = structured_decision_argv(request, resolve=resolve)
        if argv is None:
            return None
        child_env = _structured_child_env(
            base_env,
            inherit_env,
            request.provider,
        )
        try:
            with tempfile.TemporaryDirectory(
                    prefix="coop-structured-decision-") as isolated_cwd:
                delivery = open_prompt_delivery(
                    request.provider,
                    argv,
                    request.prompt,
                    workspace=cwd,
                    env=_prompt_runtime_env(base_env),
                )
                try:
                    check_launcher_argv(delivery.argv)
                    proc = runner(
                        delivery.argv,
                        cwd=isolated_cwd,
                        stdin=delivery.stdin,
                        capture_output=True,
                        timeout=max(1.0, float(timeout_s)),
                        env=child_env,
                    )
                finally:
                    delivery.close()
        except Exception:
            return None
        stdout_text = decode_agent_bytes(getattr(proc, "stdout", b""))
        _final_text, parsed_usage = parse_cli_result(
            request.provider,
            stdout_text,
        )
        usage = parsed_usage
        if getattr(proc, "returncode", 1) != 0:
            return None
        value = parse_structured_decision(stdout_text, request=request)
        if value is None:
            return None
        return coop_decisions.DecisionResult(value=value, usage=usage)
    finally:
        report_usage()


def structured_answer_argv(provider, *, resolve=None):
    """One-shot schema-constrained argv: no session resume, no board work.

    The prompt is not part of argv: ``open_prompt_delivery`` attaches it on
    stdin (claude) or through ``--prompt-file`` (grok), so a peer-authored
    item title or question never reaches a cmd.exe launcher's command line.
    """
    if provider not in STRUCTURED_ANSWER_PROVIDERS:
        return None
    resolve = resolve or resolve_cli
    resolved = resolve(PROVIDER_INVOKE[provider][0])
    if resolved is None:
        return None
    if provider == "claude":
        return [resolved, "-p", "--output-format", "json",
                "--json-schema", STRUCTURED_ANSWER_SCHEMA,
                "--tools", "",
                "--setting-sources", "",
                "--mcp-config", '{"mcpServers":{}}',
                "--strict-mcp-config",
                "--safe-mode",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--no-chrome"]
    return [resolved, "--always-approve", "-p",
            "--json-schema", STRUCTURED_ANSWER_SCHEMA]


def parse_structured_answer(stdout_text, *, expected_question_id):
    """Validated answer string from a provider JSON envelope, else None.

    claude nests the object under ``structured_output``, grok under
    ``structuredOutput``.
    """
    try:
        envelope = json.loads((stdout_text or "").strip())
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    payload = envelope.get("structured_output")
    if not isinstance(payload, dict):
        payload = envelope.get("structuredOutput")
    if not isinstance(payload, dict):
        return None
    answer = payload.get("answer")
    question_id = payload.get("question_id")
    if not isinstance(answer, str) or not answer.strip():
        return None
    if (isinstance(question_id, bool) or not isinstance(question_id, int)
            or question_id != expected_question_id):
        return None
    return answer.strip()


def invoke_structured_answer(*, provider, prompt, expected_question_id,
                             cwd, timeout_s=120.0, runner=None,
                             resolve=None, base_env=None,
                             usage_callback=None, inherit_env=False):
    """Run one schema-constrained one-shot; return the answer or None.

    Best-effort by design: every failure (missing CLI, non-zero exit,
    timeout, schema/envelope mismatch, wrong question) returns None. The
    caller decides whether fallback is safe in the admitted dispatch slot.

    Both providers run in a private temporary cwd with the structured
    allowlist environment. ``cwd`` identifies the selected workspace only
    for validating owner-private runtime storage.
    """
    usage = {"usage_observation": "unobserved"}

    def report_usage():
        if not callable(usage_callback):
            return
        try:
            usage_callback(dict(usage))
        except Exception:
            pass

    try:
        runner = runner or subprocess.run
        argv = structured_answer_argv(provider, resolve=resolve)
        if argv is None:
            return None
        child_env = _structured_child_env(
            base_env,
            inherit_env,
            provider,
        )
        try:
            with tempfile.TemporaryDirectory(
                    prefix="coop-structured-answer-") as isolated_cwd:
                delivery = open_prompt_delivery(
                    provider,
                    argv,
                    prompt,
                    workspace=cwd,
                    env=_prompt_runtime_env(base_env),
                )
                try:
                    check_launcher_argv(delivery.argv)
                    proc = runner(
                        delivery.argv,
                        cwd=isolated_cwd,
                        stdin=delivery.stdin,
                        capture_output=True,
                        timeout=max(1.0, float(timeout_s)),
                        env=child_env,
                    )
                finally:
                    delivery.close()
        except Exception:
            return None
        stdout_text = decode_agent_bytes(getattr(proc, "stdout", b""))
        _final_text, parsed_usage = parse_cli_result(
            provider,
            stdout_text,
        )
        usage = parsed_usage
        if getattr(proc, "returncode", 1) != 0:
            return None
        return parse_structured_answer(
            stdout_text,
            expected_question_id=expected_question_id,
        )
    finally:
        report_usage()

_CODEX_SHELL_ENV_KEYS = (
    "COOP_SESSION_ID", "COOP_AGENT", "COOP_AGENT_ID", "COOP_PROVIDER",
    "COOP_DB", "COOP_ACTION_LEASE_SECONDS", "COOP_ITEM_ID",
    # PYTHONPATH keeps module fallbacks importable from a workspace holding
    # no Co-op files. Board commands pin the runner's interpreter. Codex shell
    # tools do not inherit the outer environment under `inherit = "core"`, so
    # the override must still carry the installed package root.
    "PYTHONPATH",
)

# The install root — resolved from this module, never from cwd, because cwd is
# the agent's workspace. Bound into every turn so the routed CLI is importable
# there.
_INSTALL_ROOT = str(pathlib.Path(__file__).resolve().parents[1])


def pythonpath_with_install_root(env=None):
    """Co-op's install root, prepended to any inherited PYTHONPATH."""
    source = os.environ if env is None else env
    existing = source.get("PYTHONPATH") or ""
    parts = [p for p in existing.split(os.pathsep) if p and p != _INSTALL_ROOT]
    return os.pathsep.join([_INSTALL_ROOT, *parts])

# Tests and embedders may override this tuple. ``None`` selects the lazy
# defaults so importing Co-op never requires HOME/USERPROFILE to exist.
_CLI_EXTRA_DIRS = None


def _default_cli_extra_dirs():
    try:
        home = pathlib.Path.home()
    except (OSError, RuntimeError):
        home = None
    directories = [
        pathlib.Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "nodejs",
        pathlib.Path("/usr/local/bin"),
    ]
    if home is not None:
        directories.extend([
            home / "AppData" / "Roaming" / "npm",
            home / "AppData" / "Local" / "Programs" / "nodejs",
            home / ".local" / "bin",
            home / ".npm-global" / "bin",
        ])
    return tuple(directories)


def _configured_cli_extra_dirs():
    return (
        _default_cli_extra_dirs()
        if _CLI_EXTRA_DIRS is None
        else _CLI_EXTRA_DIRS
    )


def _windows_launcher_extensions():
    raw = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return tuple(
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in raw.split(";") if ext)


def _windows_launcher(path):
    return pathlib.Path(path).suffix.lower() in _windows_launcher_extensions()


def _workspace_root(workspace):
    try:
        return pathlib.Path(
            os.getcwd() if workspace is None else workspace
        ).resolve()
    except (OSError, RuntimeError):
        return None


def _inside_workspace(path, root):
    """True when ``path`` is the workspace or anything below it.

    An unresolvable path is treated as inside: a launcher whose location
    cannot be established is never the one to run.
    """
    if root is None:
        return False
    try:
        resolved = pathlib.Path(path).resolve()
    except (OSError, RuntimeError):
        return True
    return resolved == root or root in resolved.parents


def _launcher_filenames(name):
    if os.name != "nt":
        return (name,)
    suffix = pathlib.Path(name).suffix.lower()
    if suffix in _windows_launcher_extensions():
        return (name,)
    return tuple(f"{name}{ext}" for ext in _windows_launcher_extensions())


def _is_launcher_file(path):
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    if os.name == "nt":
        return _windows_launcher(path)
    return os.access(path, os.X_OK)


def _path_directories():
    """PATH as the operator set it: no empty entries and never ``.``."""
    directories = []
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        if not entry or entry == os.curdir:
            continue
        directories.append(pathlib.Path(entry))
    return directories


def _resolve_cli_uncached(name, *, workspace=None):
    name = str(name)
    root = _workspace_root(workspace)
    if os.path.dirname(name):
        # An explicit path is the caller's own choice; only PATHEXT applies.
        for filename in _launcher_filenames(name):
            candidate = pathlib.Path(filename)
            if _is_launcher_file(candidate):
                try:
                    return str(candidate.resolve())
                except OSError:
                    return None
        return None
    for directory in [*_path_directories(), *_configured_cli_extra_dirs()]:
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        if _inside_workspace(directory, root):
            continue
        for filename in _launcher_filenames(name):
            candidate = directory / filename
            if not _is_launcher_file(candidate):
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if _inside_workspace(resolved.parent, root):
                continue  # a link that leads back into the workspace
            return str(resolved)
    return None


# One run resolves each provider once (pin_cli_resolution) so a launcher
# dropped into PATH or the repo mid-run cannot change what the next turn
# spawns. ``None`` means no run has pinned anything: resolve live.
_PINNED_CLI = None


def pin_cli_resolution(names=None, *, workspace=None):
    """Resolve ``names`` now, outside ``workspace``, and freeze the answer."""
    global _PINNED_CLI
    names = list(DEFAULT_AGENTS if names is None else names)
    pinned = {
        name: _resolve_cli_uncached(name, workspace=workspace)
        for name in names
    }
    _PINNED_CLI = dict(pinned)
    return pinned


def release_cli_resolution():
    """Forget the run's pins (the runner's exit; tests)."""
    global _PINNED_CLI
    _PINNED_CLI = None


def resolve_cli(name, *, workspace=None):
    """Return an absolute path to a provider CLI, or None.

    Walks PATH itself (never ``shutil.which``, which searches the current
    directory first on Windows), skipping empty entries, ``.``, and any
    directory inside the workspace (default: the cwd ``coop start`` runs
    in), then the known install dirs. On Windows only a PATHEXT launcher
    counts; on POSIX the file must be executable. A run that has pinned its
    providers gets the pinned answer.
    """
    pinned = _PINNED_CLI
    if pinned is not None and name in pinned:
        return pinned[name]
    return _resolve_cli_uncached(name, workspace=workspace)


def resolved_provider_argv(provider):
    """PROVIDER_INVOKE[provider] with argv[0] pinned to a full path when found."""
    template = list(PROVIDER_INVOKE[provider])
    resolved = resolve_cli(template[0])
    if resolved:
        template[0] = resolved
    return template


def _codex_shell_environment_args(env):
    """Inject only Co-op bindings into Codex-generated shell commands.

    A user's Codex config may intentionally use
    ``shell_environment_policy.inherit = "core"``. The outer Codex process
    still receives our environment, but its shell tools do not unless these
    run-local values are explicit config overrides.
    """
    args = []
    for key in _CODEX_SHELL_ENV_KEYS:
        if key in env:
            args.extend([
                "-c",
                f"shell_environment_policy.set.{key}="
                f"{json.dumps(str(env[key]))}",
            ])
    return args


class LauncherArgvRejected(ValueError):
    """argv[0] is a cmd.exe launcher and another element could escape its
    quoting. Raised before anything is spawned; surfaced as a failed turn."""


# cmd.exe re-parses the command line a `.cmd`/`.bat` shim receives and does
# not honour the `\"` escaping CPython's list2cmdline emits (BatBadBut /
# CVE-2024-24576 class), so these characters must never reach it from a
# value that is not code-built. Quotes are allowed only as JSON syntax: the
# schema, the MCP config, and codex `-c key=<json>` overrides are all JSON
# built by this package, and a JSON string value may not itself contain one.
_CMD_METACHARACTERS = frozenset('&|<>^%!\r\n')
_CODEX_OVERRIDE_KEY = re.compile(r"[A-Za-z0-9_.\-]+")


def _json_string_scalars(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _json_string_scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_string_scalars(item)


def _cmd_token_problem(token):
    if any(char in _CMD_METACHARACTERS for char in token):
        return "a cmd.exe metacharacter"
    if '"' not in token:
        return None
    candidates = [token]
    key, separator, rest = token.partition("=")
    if separator and _CODEX_OVERRIDE_KEY.fullmatch(key):
        candidates.append(rest)
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        if any('"' in text for text in _json_string_scalars(decoded)):
            return "a quote inside a JSON string value"
        return None
    return "a quote outside JSON syntax"


def check_launcher_argv(argv):
    """Refuse to hand cmd.exe an argument it could execute.

    Applies only when argv[0] ends in ``.cmd``/``.bat`` (case-insensitive);
    native ``.exe`` and POSIX launchers receive argv verbatim. The message
    names the offending position, never its content.
    """
    if not argv:
        return
    launcher = str(argv[0])
    if not launcher.lower().endswith((".cmd", ".bat")):
        return
    for index, value in enumerate(argv[1:], 1):
        problem = _cmd_token_problem(str(value))
        if problem is not None:
            raise LauncherArgvRejected(
                f"refusing to spawn {pathlib.Path(launcher).name} through "
                f"cmd.exe: argv[{index}] contains {problem}"
            )


def guarded_prepare_tree(argv, **kwargs):
    """``coop_process.prepare_tree`` behind ``check_launcher_argv``: the
    tree factory every resident provider worker spawns through."""
    check_launcher_argv(argv)
    return coop_process.prepare_tree(argv, **kwargs)


def _guarded_tree_factory(factory):
    def guarded(argv, **kwargs):
        check_launcher_argv(argv)
        return factory(argv, **kwargs)

    return guarded


# A cold turn NEVER carries its prompt in argv. Windows resolves codex and
# grok through PATHEXT `.cmd` launchers, so those spawns pass through cmd.exe
# and its ~8191-character command line; one hydrated prompt is several
# kilobytes on its own, so a cold codex turn with the prompt in argv dies
# with "The command line is too long". Per-CLI prompt channels: claude reads
# the prompt from stdin under `-p`, codex reads stdin when its prompt
# argument is `-`, and grok takes a path via `--prompt-file`.
PROMPT_FILE_PROVIDERS = frozenset({"grok"})
# grok's `-p/--single` carries the prompt as an argv VALUE, so the file
# channel replaces the flag instead of joining it.
PROMPT_FILE_REPLACED_FLAGS = frozenset({"-p", "--single"})
PROMPT_STDIN_ARGUMENT = {"codex": "-"}


class PromptDelivery:
    """One turn's prompt, attached off argv and released with the turn."""

    def __init__(self, argv, stdin, *, channel, path=None, cleanup=None):
        self.argv = argv
        self.stdin = stdin
        self.channel = channel
        self.path = path
        self._cleanup = cleanup

    def close(self):
        """Best effort: a failed turn must not leave its prompt on disk."""
        close = getattr(self.stdin, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass
        cleanup = self._cleanup
        if callable(cleanup):
            cleanup_error = None
            for _attempt in range(2):
                try:
                    cleanup()
                    self._cleanup = None
                    cleanup_error = None
                    break
                except Exception as exc:
                    cleanup_error = exc
            if cleanup_error is not None:
                raise cleanup_error


def open_prompt_delivery(provider, argv, prompt, *, workspace=None, env=None):
    """Attach `prompt` to one invocation without placing it in argv.

    The returned delivery owns a stdin stream or a private prompt file for
    the length of the turn; the caller closes it in a finally so a failed
    turn releases the same files a successful one does.
    """
    text = ("" if prompt is None else str(prompt)).encode("utf-8")
    result = [str(value) for value in argv]
    if workspace is None:
        raise ValueError("workspace is required for prompt delivery")
    owned_file = coop_runtime.create_private_file(
        workspace,
        prefix="coop-prompt-",
        suffix=".txt",
        content=text,
        env=env,
    )
    if provider in PROMPT_FILE_PROVIDERS:
        result = [
            value
            for value in result
            if value not in PROMPT_FILE_REPLACED_FLAGS
        ]
        path = str(owned_file.path)
        result.extend(["--prompt-file", path])
        return PromptDelivery(
            result,
            subprocess.DEVNULL,
            channel="prompt_file",
            path=path,
            cleanup=owned_file.cleanup,
        )
    try:
        stream = owned_file.path.open("rb")
    except Exception:
        owned_file.cleanup()
        raise
    argument = PROMPT_STDIN_ARGUMENT.get(provider)
    if argument is not None:
        result.append(argument)
    return PromptDelivery(
        result,
        stream,
        channel="stdin",
        cleanup=owned_file.cleanup,
    )


def apply_prompt_cache_hint(argv, *, provider, enabled):
    """Apply one explicit provider cache hint without changing defaults."""
    result = list(argv)
    if not enabled:
        return result
    if provider != "claude":
        raise ValueError("prompt-cache hint is supported only for claude")
    flag = "--exclude-dynamic-system-prompt-sections"
    if flag in result:
        return result
    positions = [
        result.index(print_flag)
        for print_flag in ("-p", "--print")
        if print_flag in result
    ]
    position = min(positions) if positions else len(result)
    result.insert(position, flag)
    return result


def apply_usage_output(argv, *, provider):
    """Request one machine-readable result envelope where supported."""
    result = list(argv)
    if provider not in {"claude", "grok"}:
        return result
    for index, value in enumerate(result):
        if value == "--output-format":
            if index + 1 < len(result):
                result[index + 1] = "json"
            else:
                result.append("json")
            return result
        if value.startswith("--output-format="):
            result[index] = "--output-format=json"
            return result
    positions = [
        result.index(flag)
        for flag in ("-p", "--print", "--single")
        if flag in result
    ]
    position = min(positions) if positions else len(result)
    result[position:position] = ["--output-format", "json"]
    return result


def _result_text(envelope):
    for key in ("result", "text", "content"):
        value = envelope.get(key)
        if isinstance(value, str):
            return value
    message = envelope.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
    return None


def parse_cli_result(provider, stdout_text):
    """Return final provider text and bounded usage from one JSON result."""
    original = stdout_text if isinstance(stdout_text, str) else ""
    unobserved = {"usage_observation": "unobserved"}
    if provider not in {"claude", "grok"}:
        return original, unobserved
    try:
        envelope = json.loads(original.strip())
    except (TypeError, ValueError):
        return original, unobserved
    if not isinstance(envelope, Mapping):
        return original, unobserved
    usage = coop_prompt_cache.normalize_cli_usage(provider, envelope)
    if not usage:
        usage = unobserved
    final_text = _result_text(envelope)
    return (
        final_text if final_text is not None else original,
        usage,
    )


# The only shape a session id may have before it is placed in argv. Worker
# ids are uuid4; the on-disk warm record is held to the stricter UUID shape
# by load_warm_claude_session before it gets this far.
_PROVIDER_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def apply_provider_session(
        argv,
        *,
        provider,
        provider_session_id,
        resume):
    """Apply one explicit Claude/Grok session without ambient lookup.

    Raises ``ValueError`` for an id outside ``_PROVIDER_SESSION_ID``: an
    unvalidated id never reaches argv.
    """
    if provider not in {"claude", "grok"}:
        raise ValueError("explicit CLI resume is supported for claude or grok")
    session_id = str(provider_session_id or "")
    if not session_id:
        raise ValueError("provider session id is required")
    if not _PROVIDER_SESSION_ID.fullmatch(session_id):
        raise ValueError("provider session id has an unexpected shape")
    result = list(argv)
    result = [
        value
        for value in result
        if value != "--no-session-persistence"
    ]
    for flag in ("--session-id", "--resume"):
        while flag in result:
            position = result.index(flag)
            del result[position:position + 2]
    session_args = [
        "--resume" if resume else "--session-id",
        session_id,
    ]
    positions = [
        result.index(flag)
        for flag in ("-p", "--print", "--single")
        if flag in result
    ]
    position = min(positions) if positions else len(result)
    result[position:position] = session_args
    return result


def _provider_session_option_unsupported(output, *, resume):
    """Recognize only an exact CLI parser rejection before model execution."""
    flag = "--resume" if resume else "--session-id"
    quoted_flags = (flag, f"'{flag}'", f'"{flag}"')
    accepted = set()
    for quoted in quoted_flags:
        accepted.update({
            f"error: unknown option {quoted}",
            f"error: unrecognized option {quoted}",
            f"error: unexpected argument {quoted}",
            f"error: unrecognized arguments: {quoted}",
            f"unknown option: {quoted}",
            f"unrecognized option: {quoted}",
            f"unexpected argument: {quoted}",
        })
    return any(
        " ".join(line.lower().split()).rstrip(".") in accepted
        for line in str(output or "").splitlines()
    )


# A timed-out turn escalates that agent's next-turn budget (×factor, capped) —
# a research-heavy read often just needs more time on the retry.
_TIMEOUT_ESCALATION_FACTOR = 2
_TIMEOUT_CAP_SECONDS = 1800.0


def resolve_participants(requested, probe):
    """Split `requested` providers into available vs skipped, preserving order.

    `probe(name) -> (ok: bool, reason: str)` is injected: in real use it
    checks the CLI is on PATH and a cheap liveness/402 probe passes; here it
    is supplied so the resolution logic is testable without real CLIs. A
    provider not in PROVIDER_INVOKE is skipped as "unknown provider" without
    ever calling `probe`.
    """
    available = []
    skipped = []
    for name in requested:
        if name not in PROVIDER_INVOKE:
            skipped.append({"agent": name, "reason": "unknown provider"})
            continue
        ok, reason = probe(name)
        if ok:
            available.append(name)
        else:
            skipped.append({"agent": name, "reason": reason})
    return {"available": available, "skipped": skipped}


def _task_line(task):
    return (f"  #{task.get('id', task.get('item_id', '?'))} "
            f"[{task.get('status', '?')}] "
            f"{' '.join(str(task.get('title') or '').split())}")


def compose_prompt(*, agent, provider, board_path, guide_path, tasks,
                   round_no, propose_mode, task=None, phase="execute",
                   lease_seconds=None):
    """Build one agent's turn prompt. Pure and deterministic — same inputs
    always yield the same string (no timestamps, no randomness, no ANSI).
    `task` focuses the agent; `phase` is "plan" (a short says-only huddle) or
    "execute" (the full claim→…→complete cycle); `lease_seconds` is the long
    claim lease the agent must use so its claim survives the whole turn."""
    lease = int(lease_seconds) if lease_seconds else 3600
    lines = []
    if round_no > 1:
        lines.append(
            f"Continue the coop session — you are {agent} (provider "
            f"{provider}). Here is the current board; take your next turn.")
    else:
        lines.append(
            f"You are {agent}, provider {provider}, in an Agent Co-op "
            f"collaboration with the other agents on a shared board.")
    lines.append(f"Board: {board_path}")
    lines.append(
        "This is an interactive/debug turn. Use "
        "`coop --help` for exact command flags."
    )
    lines.append("")

    if propose_mode:
        lines.append(
            "There are no claimable tasks yet. Propose 2-3 concrete, "
            "repo-relevant tasks — each a title plus a one-line objective — "
            "by posting them with `coop say`, then stop so the operator can "
            "turn one into a real contract.")
        return "\n".join(lines)

    if task is not None:
        lines.append(f"Focus on task #{task} specifically.")
    lines.append("Open tasks:")
    if tasks:
        lines.extend(_task_line(item) for item in tasks)
    else:
        lines.append("  (none listed)")
    lines.append("")

    if phase == "plan":
        lines.append(
            "PLANNING HUDDLE — do NOT claim, define, or do heavy work yet. "
            "In ONE or TWO short sentences, post your proposed approach for "
            "the task with `coop say --item <id> \"...\"`. If another agent "
            "already posted an approach, STRESS-TEST it rather than agreeing — "
            "name a risk, a missing case, or a simpler path. Echoing a peer "
            "adds nothing; a different provider's doubt is the whole point of "
            "a heterogeneous trio. Keep it short — take ONE short turn, then "
            "stop.")
        return "\n".join(lines)

    if phase == "recover":
        focus = f" #{task}" if task is not None else ""
        lines.append(
            f"TIMEOUT RECOVERY HUDDLE — a turn just timed out on task{focus}. "
            "Do NOT do heavy work now. In ONE or TWO short sentences via "
            "`coop say --item <id>`, briefly DIAGNOSE why it timed out (too "
            "much to read? scope too broad? one model too slow for this?) and "
            "agree ONE ADAPTATION, then act on it: (a) SPLIT the task into "
            "smaller goal-tasks with `coop item create` that the three of you "
            "claim in parallel; (b) REASSIGN the slow chunk to a faster/free "
            "peer; (c) NARROW the scope (a tighter contract, or a `changes` "
            "verdict that shrinks it). The huddle must END WITH A CONCRETE "
            "ADAPTATION — a new sub-task, a reassignment, or a narrowed "
            "contract — not open-ended talk. Take ONE short turn, then stop.")
        return "\n".join(lines)

    lines.append(
        "ALWAYS scope task talk to the task: `coop say --item <id> "
        "\"...\"` (never a board-wide `coop say` for task work). Read the "
        "CLI help for exact flags.")
    lines.append(
        "NARRATE as you go — post a short `coop say --item <id>` line when you "
        "claim, define, finish the work, and request or approve a review, plus "
        "a one-line reply to what your peers said. Protocol actions alone show "
        "as terse milestones; your `say` lines are what make this a readable "
        "conversation the human can follow. Your turn, in order:")
    lines.append(
        "1. `coop status` — find your next action (or the focus task).")
    lines.append(
        "2. If you already OWN a claim that has gone stale (status shows "
        "`recover_claim`, or a claim you made is now stale), RECLAIM it and "
        f"continue from where you left off: `coop item claim <id> --intent "
        f"\"resume my work\" --reclaim --reason \"my claim went stale\" "
        f"--lease-seconds {lease}`. NEVER "
        "declare a human-lane unblock for your own stale claim — reclaiming "
        "is your agent-side recovery.")
    lines.append(
        "3. If your next action is a REVIEW to claim, do that WITH A LONG "
        f"LEASE (a thorough review outlasts the default): `coop review claim "
        f"<id> --lease-seconds {lease}` then `coop review submit --claim "
        "<review-claim> --verdict approve` (or changes). A cross-provider "
        "review is how peers join a task — take it before claiming new work.")
    lines.append(
        "4. Otherwise claim a task WITH A LONG LEASE so it survives your "
        f"whole turn: `coop item claim <id> --intent \"...\" --lease-seconds "
        f"{lease}`.")
    lines.append(
        "5. If it is a DRAFT (a `draft` label / empty contract fields), "
        "your FIRST action is to author the contract from the goal: "
        "`coop item define --claim <claim> --scope ... --done-when ... "
        "--output-contract ... --allowed-action ... --stop-condition ...`, "
        "then post the drafted contract to the task thread with `coop say "
        "--item <id>`.")
    lines.append(
        "6. Do the work; submit a receipt with the produced file as proof "
        "(`coop receipt submit --claim <claim> --path <file> ...`).")
    lines.append(
        "7. Request a review from a DIFFERENT provider — name the other "
        "agent (`coop review request --claim <claim> --reviewer "
        "<other-agent>`). This opens a review a peer claims in step 3.")
    lines.append(
        "8. On a board with three or more providers, completion needs TWO "
        "approves from DISTINCT providers (neither is the owner). After the "
        "first approve, if status/packet still shows approvals needed, "
        "request a SECOND review naming a different provider "
        "(`coop review request --claim <claim> --reviewer <third-agent>`) — "
        "do not complete yet. If you are the third provider and a second "
        "review is open, CLAIM AND VERDICT it — that is gate work, not "
        "optional chat. Two-provider boards still complete with one approve.")
    lines.append(
        "9. Once enough peers have approved, complete: `coop item complete "
        "--claim <claim>`.")
    lines.append(
        "10. If `coop status` shows you are IDLE — nothing to claim, no "
        "review to take (including no second-review slot), no stale claim of "
        "your own to recover — do NOT just stop. Take a CRITIQUE turn: you "
        "are the third voice, the skeptic. Pick the most active in-progress "
        "task, read its contract + plan + work, and post ONE short, pointed "
        "`coop say --item <id>` that CHALLENGES it — name a concrete gap, "
        "risk, conflict, or edge case, or ask the single sharpest question "
        "that would improve the outcome. Be a skeptic, not a cheerleader; "
        "never rubber-stamp. Prefer a second-review claim over critique when "
        "both are available.")
    lines.append("Take ONE turn, then stop.")
    return "\n".join(lines)


def run_collaboration(*, participants, rounds, run_turn, observe,
                      journal_sink=None):
    """Drive the bounded round-robin over injected callables.

    `run_turn(agent, round_no) -> dict` performs one agent turn (injected —
    no real invocation here). `observe() -> {"change_token", "all_done"}` is
    the board snapshot used for stop conditions; it is read once before the
    run and again after every full round. Sequence is participant-major
    within a round, rounds ascending. Stops at `rounds`, or early when a full
    round leaves the board unchanged ("idle") or `all_done` flips true.
    """
    if not participants:
        journal = {"participants": [], "rounds_run": 0, "turns": [],
                   "stopped_reason": "no_participants", "skipped": []}
        if journal_sink:
            journal_sink(journal)
        return journal

    turns = []
    rounds_run = 0
    stopped_reason = "rounds_exhausted"
    round_start_token = observe().get("change_token")

    for round_no in range(1, rounds + 1):
        rounds_run = round_no
        round_results = [run_turn(agent, round_no) for agent in participants]
        turns.extend(round_results)
        # Timeout recovery: if any turn in this round timed out, run ONE bounded
        # recovery huddle (diagnose why + agree an adaptation: split/reassign/
        # narrow) before the next execution round. Fires once per timeout round;
        # the short recover turns are not themselves checked for timeouts.
        if any(isinstance(r, dict) and r.get("note") == "timeout"
               for r in round_results):
            for agent in participants:
                turns.append(run_turn(agent, round_no, phase="recover"))
        post = observe()
        if post.get("all_done"):
            stopped_reason = "all_done"
            break
        if post.get("change_token") == round_start_token:
            stopped_reason = "idle"
            break
        round_start_token = post.get("change_token")

    journal = {"participants": list(participants), "rounds_run": rounds_run,
               "turns": turns, "stopped_reason": stopped_reason, "skipped": []}
    if journal_sink:
        journal_sink(journal)
    return journal


# ── Step 4: the real turn runner (the only functions that touch a real CLI) ──

def probe_agent(name):
    """Cheap availability check: known provider + its CLI on PATH (or known
    install dirs). A quota (402) or auth failure surfaces later as a failed
    turn, not here."""
    if name not in PROVIDER_INVOKE:
        return (False, "unknown provider")
    cli = PROVIDER_INVOKE[name][0]
    if resolve_cli(cli) is None:
        return (False, f"cli not found: {cli}")
    return (True, "ok")


def decode_agent_bytes(raw):
    """ENC: decode untrusted agent stdout/stderr as utf-8 with replace.

    Never use locale/cp1252 default decode on agent output (Windows).
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")


def invoke_turn(*, provider, prompt, session_id, agent_id, board_path, cwd,
                timeout_s, shutdown_grace_s=10, tree_factory=None,
                monotonic=None, sleep=None, resolve=None,
                action_lease_seconds=None, item_id=None,
                capability_manifest=None, capability_config=None,
                run_dir=None, trace=None, progress_probe=None,
                progress_before=None, action=None, turn_id=None,
                workflow_recipe=None, provider_session_id=None,
                resume_provider_session=False, worker_mode="cold",
                provider_state_dir=None, cleanup_registry=None,
                prompt_cache_hint=False, completion_probe=None,
                post_write_exit_grace_s=None, inherit_env=False):
    """Run one headless turn inside an owned process tree.

    The provider root and every descendant are drained before this function
    returns. Every failure is a result dictionary; an unowned or surviving
    process is never treated as a successful turn.

    The child starts from ``tool_turn_env`` (allowlist) unless
    ``inherit_env`` is set; argv is checked by ``check_launcher_argv`` at
    the spawn.
    """
    tree_factory = _guarded_tree_factory(
        tree_factory or coop_process.prepare_tree
    )
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    resolve = resolve or resolve_cli
    if post_write_exit_grace_s is None:
        postwrite_grace_s = POST_WRITE_EXIT_GRACE_S
    else:
        try:
            postwrite_grace_s = max(
                0.0,
                float(post_write_exit_grace_s),
            )
        except (TypeError, ValueError):
            postwrite_grace_s = POST_WRITE_EXIT_GRACE_S
    capability_manifest = capability_manifest or MANIFESTS["local_code"]
    action_kind = (
        action.get("kind")
        if isinstance(action, dict)
        else None
    )
    turn_id = turn_id or uuid.uuid4().hex
    emitted = set()
    provider_usage = {"usage_observation": "unobserved"}
    provider_output_text = ""

    def emit(event, details=None):
        if trace is None or event in emitted:
            return
        emitted.add(event)
        fields = {
            "turn_id": turn_id,
            "agent": agent_id,
            "provider": provider,
            "action": action_kind,
            "capability_set": capability_manifest.name,
        }
        bounded_details = dict(details or {})
        if workflow_recipe is not None:
            bounded_details["workflow_recipe"] = workflow_recipe
        if bounded_details:
            fields["details"] = bounded_details
        try:
            trace.emit(event, **fields)
        except Exception:
            pass

    external_names = list(capability_manifest.external_servers)
    emit(
        "worker_start_requested",
        details={"worker_mode": worker_mode},
    )
    emit(
        "capability_activation_requested",
        details={"external_mcp": external_names},
    )
    bare = PROVIDER_INVOKE[provider][0]
    resolved = resolve(bare)
    if resolved is None:
        emit(
            "provider_result_received",
            details={
                "exit_code": None,
                "timed_out": False,
                "error_class": "cli_not_found",
                **provider_usage,
            },
        )
        emit("worker_idle")
        emit(
            "capability_shutdown",
            details={"external_mcp": external_names},
        )
        emit("worker_shutdown")
        return {
            "agent": agent_id,
            "provider": provider,
            "ok": False,
            "exit": None,
            "tree_empty": True,
            "note": f"cli not found on PATH: {bare}",
            "classification": "worker_start_failed",
            "retryable": False,
            "process_started": False,
            "session_created": False,
        }
    env = tool_turn_env(
        os.environ,
        provider=provider,
        inherit=bool(inherit_env),
    )
    env.update({"COOP_SESSION_ID": session_id, "COOP_AGENT": agent_id,
                "COOP_AGENT_ID": agent_id, "COOP_PROVIDER": provider,
                "COOP_DB": str(board_path),
                "PYTHONPATH": pythonpath_with_install_root(env)})
    if action_lease_seconds is not None:
        env["COOP_ACTION_LEASE_SECONDS"] = str(int(action_lease_seconds))
    if item_id is not None:
        env["COOP_ITEM_ID"] = str(int(item_id))
    base_argv = [resolved] + list(PROVIDER_INVOKE[provider][1:])
    if provider == "codex":
        base_argv.extend(_codex_shell_environment_args(env))
    base_argv = apply_prompt_cache_hint(
        base_argv,
        provider=provider,
        enabled=bool(prompt_cache_hint),
    )
    base_argv = apply_usage_output(base_argv, provider=provider)
    capability_servers = (
        capability_config.get("servers")
        if isinstance(capability_config, dict)
        else None
    )
    profile_run_dir = (
        pathlib.Path(run_dir)
        if run_dir is not None
        else pathlib.Path(board_path).resolve().parent / ".coop-runs"
    )
    try:
        profile_kwargs = {
            "env": env,
            "workspace": cwd,
            "run_dir": profile_run_dir,
            "servers": capability_servers,
        }
        if provider_state_dir is not None:
            profile_kwargs["provider_state_dir"] = provider_state_dir
        profile = build_launch_profile(
            provider,
            base_argv,
            capability_manifest,
            **profile_kwargs,
        )
    except Exception as exc:
        emit(
            "provider_result_received",
            details={
                "exit_code": None,
                "timed_out": False,
                "error_class": type(exc).__name__,
                **provider_usage,
            },
        )
        emit("worker_idle")
        emit(
            "capability_shutdown",
            details={
                "external_mcp": external_names,
                "error_class": type(exc).__name__,
            },
        )
        emit("worker_shutdown")
        note = f"capability_activation_failed: {exc}"[:200]
        return {
            "agent": agent_id,
            "provider": provider,
            "ok": False,
            "exit": None,
            "tree_empty": True,
            "note": note,
            "classification": "capability_activation_failed",
            "retryable": False,
            "process_started": False,
            "session_created": False,
        }
    argv = list(profile.argv)
    env = dict(profile.env)
    if provider_session_id is not None:
        argv = apply_provider_session(
            argv,
            provider=provider,
            provider_session_id=provider_session_id,
            resume=bool(resume_provider_session),
        )
    delivery = None
    tree = None
    exit_code = None
    timed_out = False
    postwrite_grace_expired = False
    postwrite_grace_deadline = None
    turn_error = None
    tree_empty = True
    cleanup_errors = []
    tree_cleanup_failed = False
    cleanup_retained = False
    capability_cleanup_failed = False
    capability_cleanup_error = None
    prompt_cleanup_failed = False
    prompt_cleanup_error = None
    tail = ""
    stdout_text = ""
    stderr_text = ""
    saw_output = False
    saw_board_write = False

    if progress_probe is not None and progress_before is None:
        try:
            progress_before = progress_probe()
        except Exception:
            progress_before = None

    last_progress = progress_before
    last_mutation_monotonic = None

    def observe_progress(stdout, stderr):
        nonlocal saw_output, saw_board_write, last_progress
        nonlocal last_mutation_monotonic
        if not saw_output:
            try:
                saw_output = bool(
                    os.fstat(stdout.fileno()).st_size
                    or os.fstat(stderr.fileno()).st_size
                )
            except (OSError, ValueError):
                saw_output = False
            if saw_output:
                emit("first_provider_output")
        if progress_probe is not None:
            try:
                current_progress = progress_probe()
            except Exception:
                current_progress = last_progress
            if current_progress != last_progress:
                # Track EVERY committed change: the gap between the last
                # one and provider exit is the disposable turn tail.
                last_progress = current_progress
                last_mutation_monotonic = monotonic()
                if not saw_board_write:
                    saw_board_write = True
                    emit("first_board_mutation")

    # Files avoid PIPE deadlocks when a provider or descendant emits heavily.
    # The owned bootstrap inherits these handles; decode only after the whole
    # tree has been drained and the handles can no longer be written.
    try:
        with (
            tempfile.TemporaryFile() as stdout,
            tempfile.TemporaryFile() as stderr,
        ):
            try:
                delivery = open_prompt_delivery(
                    provider,
                    argv,
                    prompt,
                    workspace=cwd,
                    env=env,
                )
                prepared = tree_factory(
                    delivery.argv,
                    session_id=uuid.uuid4().hex,
                    cwd=cwd,
                    env=env,
                    stdin=delivery.stdin,
                    stdout=stdout,
                    stderr=stderr,
                )
                emit("process_tree_prepared")
                emit(
                    "capability_activation_completed",
                    details={"external_mcp": external_names},
                )
                tree = prepared.release()
                emit("provider_process_started")
                emit(
                    "prompt_submitted",
                    details=coop_prompt_cache.prompt_trace_details(
                        prompt,
                        provider=provider,
                        provider_hint=bool(prompt_cache_hint),
                    ),
                )
                deadline = monotonic() + max(0.0, float(timeout_s))
                while True:
                    exit_code = tree.poll_root()
                    observe_progress(stdout, stderr)
                    if exit_code is not None:
                        break
                    if (
                            completion_probe is not None
                            and saw_board_write
                            and postwrite_grace_deadline is None):
                        try:
                            completed_write = bool(completion_probe())
                        except Exception:
                            completed_write = False
                        if completed_write:
                            postwrite_grace_deadline = (
                                monotonic() + postwrite_grace_s
                            )
                    if (
                            postwrite_grace_deadline is not None
                            and monotonic() >= postwrite_grace_deadline):
                        postwrite_grace_expired = True
                        emit("postwrite_grace_expired")
                        break
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    sleep_for = min(0.1, remaining)
                    if postwrite_grace_deadline is not None:
                        sleep_for = min(
                            sleep_for,
                            max(
                                0.0,
                                postwrite_grace_deadline - monotonic(),
                            ),
                        )
                    sleep(sleep_for)
                observe_progress(stdout, stderr)
            except Exception as exc:
                turn_error = exc

            if tree is not None and (timed_out or postwrite_grace_expired):
                try:
                    tree.graceful_stop()
                except Exception as exc:
                    cleanup_errors.append(
                        f"graceful_stop: {type(exc).__name__}"
                    )
                grace_deadline = monotonic() + max(
                    0.0,
                    float(shutdown_grace_s),
                )
                while True:
                    try:
                        if tree.is_empty():
                            break
                    except Exception as exc:
                        cleanup_errors.append(
                            f"is_empty: {type(exc).__name__}"
                        )
                        break
                    if monotonic() >= grace_deadline:
                        try:
                            tree.force_stop()
                        except Exception as exc:
                            cleanup_errors.append(
                                f"force_stop: {type(exc).__name__}"
                            )
                        break
                    sleep(
                        min(
                            0.1,
                            max(0.0, grace_deadline - monotonic()),
                        )
                    )

            if tree is not None:
                close_error = None
                for _attempt in range(2):
                    try:
                        tree.close()
                        close_error = None
                        break
                    except Exception as exc:
                        close_error = exc
                if close_error is not None:
                    tree_cleanup_failed = True
                    cleanup_errors.append(
                        f"close: {type(close_error).__name__}"
                    )
                try:
                    tree_empty = bool(tree.is_empty())
                except Exception as exc:
                    tree_empty = False
                    tree_cleanup_failed = True
                    cleanup_errors.append(
                        f"final is_empty: {type(exc).__name__}"
                    )
                if (
                    cleanup_registry is not None
                    and (tree_cleanup_failed or not tree_empty)
                ):
                    def retry_tree_cleanup(owned_tree=tree):
                        owned_tree.close()
                        try:
                            empty = bool(owned_tree.is_empty())
                        except Exception as exc:
                            raise coop_workers.WorkerError(
                                "process tree state unavailable"
                            ) from exc
                        if not empty:
                            raise coop_workers.WorkerError(
                                "process tree not empty"
                            )

                    cleanup_registry.retain(
                        f"{provider}:process_tree:{turn_id}",
                        retry_tree_cleanup,
                    )
                    cleanup_retained = True

            observe_progress(stdout, stderr)
            if postwrite_grace_expired and completion_probe is not None:
                # Re-check only as a consistency observation after owned
                # cleanup. The result never converts this failed provider
                # turn into success and never writes to the board.
                try:
                    completion_probe()
                except Exception:
                    pass
            stdout.seek(0)
            stderr.seek(0)
            stdout_text = decode_agent_bytes(stdout.read())
            stderr_text = decode_agent_bytes(stderr.read())
            provider_output_text, provider_usage = parse_cli_result(
                provider,
                stdout_text,
            )
            tail = (provider_output_text or stderr_text or "").strip()
    except Exception as exc:
        if turn_error is None:
            turn_error = exc
    finally:
        emit(
            "provider_result_received",
            details={
                "exit_code": exit_code,
                "timed_out": timed_out,
                **({
                    "actor_last_mutation_to_exit_ms": int(
                        (monotonic() - last_mutation_monotonic) * 1000),
                } if last_mutation_monotonic is not None else {}),
                **({
                    "error_class": type(turn_error).__name__,
                } if turn_error is not None else {}),
                **{
                    key: value
                    for key, value in provider_usage.items()
                    if key in coop_prompt_cache.USAGE_TRACE_FIELDS
                },
            },
        )
        emit("worker_idle")
        if delivery is not None:
            # The tree is drained by here, so nothing still holds the prompt.
            try:
                delivery.close()
            except Exception as exc:
                prompt_cleanup_failed = True
                prompt_cleanup_error = exc
                if cleanup_registry is not None:
                    cleanup_registry.retain(
                        f"{provider}:prompt:{turn_id}",
                        delivery.close,
                    )
                    cleanup_retained = True
        for _attempt in range(2):
            try:
                profile.cleanup()
                capability_cleanup_failed = False
                capability_cleanup_error = None
                break
            except Exception as exc:
                capability_cleanup_failed = True
                capability_cleanup_error = exc
        if capability_cleanup_failed and cleanup_registry is not None:
            cleanup_registry.retain(
                f"{provider}:capability_profile:{turn_id}",
                profile.cleanup,
            )
            cleanup_retained = True
        emit(
            "capability_shutdown",
            details={
                "external_mcp": external_names,
                **({
                    "error_class": type(
                        capability_cleanup_error or prompt_cleanup_error
                    ).__name__,
                } if (
                    capability_cleanup_error is not None
                    or prompt_cleanup_error is not None
                ) else {}),
            },
        )
        emit(
            "worker_shutdown",
            details={
                **({
                    "error_class": "process_tree_cleanup_failed",
                } if cleanup_errors or not tree_empty else {}),
            },
        )

    process_started = tree is not None
    session_created = bool(
        provider_session_id is not None
        and process_started
        and exit_code == 0
    )
    base = {
        "agent": agent_id,
        "provider": provider,
        "exit": exit_code,
        "tree_empty": tree_empty,
        "process_started": process_started,
        "session_created": session_created,
        "usage": dict(provider_usage),
        **({"cleanup_retained": True} if cleanup_retained else {}),
    }
    if cleanup_errors or not tree_empty:
        detail = "; ".join(cleanup_errors) or "tree not empty"
        return {
            **base,
            "ok": False,
            "note": f"process_tree_cleanup_failed: {detail}"[:200],
            "classification": "process_tree_cleanup_failed",
            "retryable": False,
        }
    if capability_cleanup_failed or prompt_cleanup_failed:
        return {
            **base,
            "ok": False,
            "note": "capability_shutdown_failed",
            "classification": "capability_shutdown_failed",
            "retryable": False,
        }
    if isinstance(turn_error, LauncherArgvRejected):
        # The same argv would be refused again: terminal, not transient.
        return {
            **base,
            "ok": False,
            "note": f"launcher_argv_rejected: {turn_error}"[:200],
            "classification": "worker_start_failed",
            "retryable": False,
        }
    if turn_error is not None:
        classification = (
            "worker_protocol_failed"
            if process_started
            else "worker_start_failed"
        )
        note = f"{classification}: {turn_error}"[:200]
        return {
            **base,
            "ok": False,
            "note": note,
            "classification": classification,
            "retryable": True,
        }
    if timed_out:
        return {
            **base,
            "ok": False,
            "exit": None,
            "note": "timeout",
            "classification": "turn_timeout",
            "retryable": True,
            "session_created": False,
        }
    if postwrite_grace_expired:
        return {
            **base,
            "ok": False,
            "exit": None,
            "note": "postwrite_exit_timeout",
            "classification": "postwrite_exit_timeout",
            "retryable": False,
            "session_created": False,
        }
    if (
        provider_session_id is not None
        and exit_code not in {None, 0}
        and _provider_session_option_unsupported(
            stderr_text,
            resume=bool(resume_provider_session),
        )
    ):
        return {
            **base,
            "ok": False,
            "note": "provider_session_unsupported",
            "classification": "provider_session_unsupported",
            "retryable": False,
            "cold_fallback_safe": True,
            "session_created": False,
        }
    provider_failure = coop_provider_failures.classify_provider_failure(
        provider,
        exit_code=exit_code,
        output=f"{provider_output_text}\n{stderr_text}",
    )
    if provider_failure is not None:
        return {
            **base,
            "ok": False,
            "note": provider_failure.classification,
            "classification": provider_failure.classification,
            "retryable": provider_failure.retryable,
        }
    return {**base, "ok": exit_code == 0,
            "note": " ".join(tail.split())[:200]}


def _board_dir(board_path):
    return pathlib.Path(board_path).resolve().parent


def stop_flag_path(board_path):
    return str(_board_dir(board_path) / ".coop-start-stop")


def _write_journal(board_path, journal, resolution):
    path = _board_dir(board_path) / "coop-start-journal.md"
    parts = journal.get("participants") or []
    lines = ["# coop start — run journal", "",
             f"participants: {', '.join(parts) or '(none)'}"]
    skipped = resolution.get("skipped") or []
    if skipped:
        lines.append("skipped: " + "; ".join(
            f"{s['agent']} ({s['reason']})" for s in skipped))
    lines += [f"rounds run: {journal.get('rounds_run')}",
              f"stopped: {journal.get('stopped_reason')}", "", "## turns"]
    for turn in journal.get("turns") or []:
        note = " ".join(str(turn.get("note") or "").split())[:120]
        phase = turn.get("phase", "execute")
        lines.append(f"- [{phase}] r{turn.get('round', '?')} "
                     f"{turn.get('agent', '?')}: ok={turn.get('ok')} "
                     f"exit={turn.get('exit')} {note}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def start_collaboration(*, board_path, requested=None, rounds=2, plan_rounds=1,
                        task=None, cwd=None, guide_path=None, timeout_s=180.0,
                        turn_runner=None, probe=None, stop_flag=None):
    """Convene the available agents on `board_path` for a bounded round-robin.

    Option (a): one supervised session per agent for the whole run — inserted
    here (the orchestrator manages sessions directly; it is not an agent),
    kept running across the agent's turns, finished at the end. Each turn is
    invoked headless with COOP_SESSION_ID set so the agent's `coop` commands
    resolve to that persistent session. `turn_runner`/`probe` are injectable
    so the whole orchestration is unit-tested without a real CLI.
    """
    requested = list(requested) if requested else list(DEFAULT_AGENTS)
    probe = probe or probe_agent
    resolution = resolve_participants(requested, probe)
    available = resolution["available"]

    board_path = str(board_path)
    if cwd is None:
        cwd = str(_board_dir(board_path).parent)
    # ``guide_path`` stays in this compatibility API. Interactive prompts use
    # installed CLI help and do not require a repository document.
    if stop_flag is None:
        stop_flag = stop_flag_path(board_path)
    try:                                   # a stale flag must not pre-stop us
        pathlib.Path(stop_flag).unlink()
    except OSError:
        pass

    # TIMING: claim lease must always be ≥ per-turn timeout (self-lock risk).
    long_lease = max(int(timeout_s), 3600, int(timeout_s) * (rounds + 2))
    # Bounded (not 86400): generous enough that no genuine turn expires, small
    # enough that an orphaned session self-heals within a sane window via
    # coopdb.sweep_expired_sessions (which insert_session now calls).
    session_runtime = max(3600, int(timeout_s) * rounds * 2)
    conn = coopdb.connect(board_path, require_current=True)
    sessions = {}
    try:
        for name in available:
            sid = f"coopstart-{name}-{uuid.uuid4().hex[:12]}"
            coopdb.insert_session(
                conn, session_id=sid, agent_id=name, provider=name,
                command=resolved_provider_argv(name), cwd=cwd,
                max_runtime_s=session_runtime, grace_s=10, stdin_isatty=False)
            sessions[name] = sid

        budgets = {name: float(timeout_s) for name in available}

        def _run_one(**kw):
            if turn_runner is not None:
                return turn_runner(**kw)
            return invoke_turn(provider=kw["provider"], prompt=kw["prompt"],
                               session_id=kw["session_id"],
                               agent_id=kw["agent"], board_path=board_path,
                               cwd=cwd, timeout_s=kw.get("timeout_s", timeout_s))

        def run_turn(agent, round_no, phase="execute"):
            try:            # extend this agent's surviving claims before it acts
                coopdb.renew_claims(conn, session_id=sessions[agent],
                                    lease_seconds=long_lease)
            except (coopdb.InvalidTransition, coopdb.SessionMismatch):
                pass
            except Exception as exc:
                # API-CONTRACT: surface unexpected renew failures (don't hide)
                return {"agent": agent, "ok": False, "round": round_no,
                        "phase": phase,
                        "note": f"renew_claims: {type(exc).__name__}: {exc}"[:160]}
            tasks = coopdb.task_rows(conn)
            propose = not any(
                t.get("status") in ("todo", "working", "review")
                for t in tasks)
            prompt = compose_prompt(
                agent=agent, provider=agent, board_path=board_path,
                guide_path=guide_path, tasks=tasks, round_no=round_no,
                propose_mode=propose, task=task, phase=phase,
                lease_seconds=long_lease)
            try:
                result = _run_one(agent=agent, provider=agent, round_no=round_no,
                                  prompt=prompt, session_id=sessions[agent],
                                  timeout_s=budgets[agent], phase=phase)
            except Exception as exc:  # FAULT-ISOLATION: one turn never kills run
                result = {"agent": agent, "ok": False,
                          "note": f"turn error: {exc}"[:160]}
            if isinstance(result, dict):
                result = {**result, "round": round_no, "phase": phase}
                if result.get("note") == "timeout":
                    budgets[agent] = min(
                        _TIMEOUT_CAP_SECONDS,
                        budgets[agent] * _TIMEOUT_ESCALATION_FACTOR)
            # Renew again AFTER the turn regardless of its outcome (a timed-out
            # turn still returns a result dict, so this always runs); it cannot
            # revive an already-expired claim, which is why the agent claims
            # with the long lease above.
            try:
                coopdb.renew_claims(conn, session_id=sessions[agent],
                                    lease_seconds=long_lease)
            except (coopdb.InvalidTransition, coopdb.SessionMismatch):
                pass
            except Exception as exc:
                if isinstance(result, dict):
                    result = {**result, "ok": False,
                              "note": (result.get("note") or "") +
                              f" | post-renew: {type(exc).__name__}"}
            return result

        plan_turns = []
        for plan_no in range(1, max(0, plan_rounds) + 1):
            if pathlib.Path(stop_flag).exists():
                break
            for agent in available:      # a short consensus huddle, then act
                plan_turns.append(run_turn(agent, plan_no, phase="plan"))

        def observe():
            if pathlib.Path(stop_flag).exists():
                return {"change_token": None, "all_done": True}
            tasks = coopdb.task_rows(conn)
            all_done = bool(tasks) and all(
                t.get("status") == "done" for t in tasks)
            return {"change_token": coopdb.board_probe(conn),
                    "all_done": all_done}

        journal = run_collaboration(
            participants=available, rounds=rounds, run_turn=run_turn,
            observe=observe,
            journal_sink=lambda j: _write_journal(board_path, j, resolution))
        # Prepend the planning huddle so the journal shows plan → execute.
        journal["turns"] = plan_turns + journal.get("turns", [])
        journal["plan_rounds"] = max(0, plan_rounds)
        journal["skipped"] = resolution["skipped"]
        if pathlib.Path(stop_flag).exists():
            journal["stopped_reason"] = "stopped"
        _write_journal(board_path, journal, resolution)  # rewrite with plan turns
        return journal
    finally:
        for sid in sessions.values():
            try:
                coopdb.finish_session(conn, sid, status="exited",
                                      reason="child_exit")
            except (coopdb.InvalidTransition, coopdb.SessionMismatch):
                pass  # already terminal / gone — fine
            except Exception as exc:
                # API-CONTRACT: never silent-pass an unexpected finish failure
                # (swallowed finish_session enums left stale sessions in run0).
                print(f"warn: finish_session {sid}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
        conn.close()
