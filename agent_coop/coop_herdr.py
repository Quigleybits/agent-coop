"""Herdr CLI adapter and read-only Co-op turn-trace mirror.

Herdr is an optional observability transport.  This module is the only product
code allowed to execute the ``herdr`` CLI; it never reads pane output back into
the board protocol.  Persistent provider workers keep their existing PIPE
stdio and the panes created here run only the trace renderer below.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, TextIO

from agent_coop import coop_runner_status, coopdb
from agent_coop.coop_errors import CoopError


COMMAND_TIMEOUT_SECONDS = 10.0
MIRROR_POLL_INTERVAL_SECONDS = 0.25
CORE_PROVIDERS = frozenset({"claude", "codex", "grok"})
HERDR_CLI_ENV_EXACT = frozenset({
    # Process launch and platform runtime.
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "HOMEDRIVE", "HOMEPATH",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    # Locale and terminal presentation.
    "LANG", "LANGUAGE", "LC_ALL", "TERM", "COLORTERM", "NO_COLOR",
    "FORCE_COLOR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
    # Herdr's current-session selectors. These are opaque routing values, not
    # pane output, and must survive when the client talks to its live session.
    "HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID",
})
MIRROR_ENV_EXACT = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "HOMEDRIVE", "HOMEPATH",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
    "NO_COLOR", "FORCE_COLOR",
    "COOP_RUN_TRACE_PATH", "COOP_DB",
})


def herdr_cli_env(source=None) -> dict[str, str]:
    """Return only the platform and session values the Herdr client needs."""
    source = os.environ if source is None else source
    return {
        str(key): str(value)
        for key, value in source.items()
        if str(key).upper() in HERDR_CLI_ENV_EXACT
    }


def _resolve_herdr_launcher(name, *, workspace):
    # Local import keeps the adapter import-light and avoids module-init cycles.
    from agent_coop import coop_start

    return coop_start.resolve_cli(name, workspace=workspace)


def _launcher_path_environment(raw_path, *, workspace, launcher) -> str:
    """Absolute PATH entries outside the selected workspace, launcher first."""
    candidates = [launcher.parent]
    candidates.extend(
        pathlib.Path(entry)
        for entry in str(raw_path or "").split(os.pathsep)
        if entry and entry != os.curdir
    )
    safe = []
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == workspace or workspace in resolved.parents:
            continue
        identity = os.path.normcase(str(resolved))
        if identity in seen:
            continue
        seen.add(identity)
        safe.append(str(resolved))
    return os.pathsep.join(safe)


def _shell_command(argv):
    values = [str(value) for value in argv]
    return (
        subprocess.list2cmdline(values)
        if os.name == "nt"
        else shlex.join(values)
    )


_INSTALL_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_GLOBAL_EVENTS = frozenset({"run_started", "run_finished"})
_BATCH_COMMAND_METACHARACTERS = frozenset("&|<>^%!\r\n")


class HerdrError(CoopError):
    """Base for bounded adapter failures rendered by the normal Co-op CLI."""

    type = "herdr_error"
    default_reason_code = "launch_failed"


class HerdrUnavailable(HerdrError):
    type = "herdr_unavailable"
    default_reason_code = "session_unavailable"


class HerdrCommandError(HerdrError):
    type = "herdr_command_failed"


class HerdrProtocolError(HerdrError):
    type = "herdr_protocol_error"


class HerdrInputError(HerdrError):
    type = "invalid_herdr_target"
    default_reason_code = "input_invalid"


class HerdrMirrorTimeout(HerdrError):
    type = "herdr_mirror_timeout"


@dataclass(frozen=True)
class _MirrorResource:
    provider: str
    pane_id: str
    tab_id: str


@dataclass
class _WorkspaceResource:
    workspace_id: str
    mirrors: dict[str, _MirrorResource] = field(default_factory=dict)


@dataclass(frozen=True)
class _PendingCleanup:
    close_argv: tuple[str, ...]
    workspace_id: str | None


def _validate_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise HerdrInputError(
            "run id must be a 1-128 character path-safe basename using "
            "letters, digits, underscores, or hyphens"
        )
    return run_id


def _validate_provider(provider: object) -> str:
    if not isinstance(provider, str) or provider not in CORE_PROVIDERS:
        raise HerdrInputError(
            "provider must be one of claude, codex, or grok"
        )
    return provider


def _decode_payload(stdout: object) -> dict | None:
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _returned_id(result: Mapping, object_key: str, id_key: str) -> str | None:
    value = result.get(object_key)
    identifier = value.get(id_key) if isinstance(value, Mapping) else None
    # IDs are deliberately opaque.  Preserve valid values exactly; reject only
    # the bounded transport hazards shared with sidecar metadata.
    return coop_runner_status.canonical_herdr_id(identifier)


def _pythonpath_with_install_root(env: Mapping[str, str]) -> str:
    """Dependency-safe twin of coop_start's foreign-workspace binding."""
    existing = env.get("PYTHONPATH") or ""
    parts = [
        part
        for part in existing.split(os.pathsep)
        if part and part != _INSTALL_ROOT
    ]
    return os.pathsep.join([_INSTALL_ROOT, *parts])


def _mirror_argv(run_id: str, provider: str) -> list[str]:
    bootstrap = (
        "import os,runpy,sys;"
        f"_names={tuple(sorted(MIRROR_ENV_EXACT))!r};"
        "_env={k:os.environ[k] for k in _names if k in os.environ};"
        "os.environ.clear();os.environ.update(_env);"
        f"sys.path.insert(0,{json.dumps(_INSTALL_ROOT)});"
        "runpy.run_module('agent_coop',run_name='__main__')"
    )
    return [
        sys.executable,
        "-I",
        "-c",
        bootstrap,
        "herdr",
        "mirror",
        run_id,
        provider,
    ]


class _InternalMirrorCommand(str):
    """A shell command assembled only from pinned code and validated IDs."""


def _mirror_shell_command(run_id: str, provider: str) -> _InternalMirrorCommand:
    run_id = _validate_run_id(run_id)
    provider = _validate_provider(provider)
    command = _shell_command(_mirror_argv(run_id, provider))
    return _InternalMirrorCommand(command)


def _absolute_path(value: object, *, setting: str) -> pathlib.Path:
    if not isinstance(value, (str, os.PathLike)):
        raise HerdrInputError(f"{setting} must be an absolute path")
    candidate = pathlib.Path(value).expanduser()
    if not candidate.is_absolute():
        raise HerdrInputError(f"{setting} must be an absolute path")
    return candidate.resolve()


class HerdrAdapter:
    """Own Herdr mirror resources created by one Co-op runner process."""

    def __init__(
            self, *, runner=None, workspace=None, environ=None,
            resolve=None):
        self._runner = runner
        try:
            self._workspace = pathlib.Path(
                os.getcwd() if workspace is None else workspace
            ).resolve()
        except (OSError, RuntimeError) as exc:
            raise HerdrInputError(
                "Herdr workspace could not be resolved"
            ) from exc
        self._environment = herdr_cli_env(environ)
        self._resolve = resolve or _resolve_herdr_launcher
        self._launcher: str | None = None
        self._workspaces: dict[str, _WorkspaceResource] = {}
        self._pending: list[_PendingCleanup] = []
        self._lock = threading.RLock()

    def _pinned_launcher(self) -> str:
        with self._lock:
            if self._launcher is not None:
                return self._launcher
            try:
                value = self._resolve("herdr", workspace=self._workspace)
                candidate = pathlib.Path(value) if value else None
                if candidate is None or not candidate.is_absolute():
                    raise ValueError("launcher is unavailable")
                resolved = candidate.resolve()
                if (
                        resolved == self._workspace
                        or self._workspace in resolved.parents):
                    raise ValueError("launcher is inside workspace")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise HerdrUnavailable(
                    "Herdr launcher is unavailable outside the workspace"
                ) from exc
            self._launcher = str(resolved)
            self._environment["PATH"] = _launcher_path_environment(
                self._environment.get("PATH"),
                workspace=self._workspace,
                launcher=resolved,
            )
            return self._launcher

    def _invoke(self, argv: list[str]):
        from agent_coop import coop_start

        runner = self._runner or subprocess.run
        launcher = pathlib.Path(self._pinned_launcher())
        raw_arguments = list(argv[1:])
        command = [str(launcher), *map(str, raw_arguments)]
        guarded_command = list(command)
        batch_launcher = (
            os.name == "nt"
            and launcher.suffix.lower() in {".cmd", ".bat"}
        )
        for index, value in enumerate(raw_arguments, 1):
            if isinstance(value, _InternalMirrorCommand):
                if batch_launcher and any(
                        character in value
                        for character in _BATCH_COMMAND_METACHARACTERS):
                    raise HerdrInputError(
                        "internal mirror command is unsafe for a Windows "
                        "batch launcher"
                    )
                guarded_command[index] = "agent-coop-internal-mirror-command"
        try:
            coop_start.check_launcher_argv(guarded_command)
        except coop_start.LauncherArgvRejected as exc:
            raise HerdrInputError(
                "Herdr command contains an unsafe value for the selected "
                "launcher"
            ) from exc
        try:
            return runner(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
                env=dict(self._environment),
                cwd=str(launcher.parent),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HerdrCommandError(
                f"Herdr command could not run: {' '.join(argv[:3])}"
            ) from exc

    def _result(self, argv: list[str], expected_type: str) -> dict:
        completed = self._invoke(argv)
        if completed.returncode != 0:
            # Pane output and CLI diagnostics may contain run content.  Keep
            # them captured but never splice them into the error message.
            raise HerdrCommandError(
                f"Herdr command failed: {' '.join(argv[:3])}"
            )
        payload = _decode_payload(completed.stdout)
        result = payload.get("result") if payload is not None else None
        if (
            not isinstance(result, dict)
            or result.get("type") != expected_type
        ):
            raise HerdrProtocolError(
                "Herdr returned an unexpected response for "
                + " ".join(argv[:3])
            )
        return result

    def available(self) -> bool:
        """Fail closed unless the CLI reaches the current live session.

        ``api snapshot`` exercises both binary resolution and the session
        socket.  All output is captured and every execution/protocol failure
        becomes ``False`` without emitting either stream.
        """
        try:
            completed = self._invoke(["herdr", "api", "snapshot"])
        except HerdrError:
            return False
        if completed.returncode != 0:
            return False
        payload = _decode_payload(completed.stdout)
        result = payload.get("result") if payload is not None else None
        return bool(
            isinstance(result, dict)
            and result.get("type") == "session_snapshot"
            and isinstance(result.get("snapshot"), dict)
        )

    @staticmethod
    def _creation_argv(
        prefix: list[str],
        cwd: pathlib.Path,
        bindings: Mapping[str, str],
        label: str,
    ) -> list[str]:
        argv = [*prefix, "--cwd", str(cwd)]
        for key, value in bindings.items():
            argv.extend(["--env", f"{key}={value}"])
        argv.extend(["--label", label, "--no-focus"])
        return argv

    def _rollback_created(
        self,
        *,
        workspace_id: str | None,
        tab_id: str | None,
        pane_id: str | None,
        created_workspace: bool,
    ) -> None:
        candidates = (
            (("workspace", workspace_id), ("tab", tab_id), ("pane", pane_id))
            if created_workspace
            else (("tab", tab_id), ("pane", pane_id))
        )
        target = next(
            ((kind, identifier) for kind, identifier in candidates if identifier),
            None,
        )
        if target is None:
            return
        kind, identifier = target
        argv = ("herdr", kind, "close", identifier)
        try:
            self._result(list(argv), "ok")
        except HerdrError:
            self._pending.append(_PendingCleanup(
                close_argv=argv,
                workspace_id=workspace_id,
            ))

    @staticmethod
    def _created_ids(result: Mapping):
        return (
            _returned_id(result, "workspace", "workspace_id"),
            _returned_id(result, "tab", "tab_id"),
            _returned_id(result, "root_pane", "pane_id"),
        )

    def _created_target(
        self,
        run_id: str,
        provider: str,
        cwd: pathlib.Path,
        bindings: Mapping[str, str],
    ):
        workspace = self._workspaces.get(run_id)
        if workspace is None:
            result = self._result(
                self._creation_argv(
                    ["herdr", "workspace", "create"],
                    cwd,
                    bindings,
                    f"coop-run-{run_id}",
                ),
                "workspace_created",
            )
            workspace_id, tab_id, pane_id = self._created_ids(result)
            missing = next((
                name
                for name, value in (
                    ("workspace.workspace_id", workspace_id),
                    ("tab.tab_id", tab_id),
                    ("root_pane.pane_id", pane_id),
                )
                if value is None
            ), None)
            if missing is not None:
                self._rollback_created(
                    workspace_id=workspace_id,
                    tab_id=tab_id,
                    pane_id=pane_id,
                    created_workspace=True,
                )
                raise HerdrProtocolError(f"Herdr response omitted {missing}")
            workspace = _WorkspaceResource(workspace_id=workspace_id)
            self._workspaces[run_id] = workspace
            return workspace, pane_id, tab_id, True

        result = self._result(
            self._creation_argv(
                [
                    "herdr", "tab", "create",
                    "--workspace", workspace.workspace_id,
                ],
                cwd,
                bindings,
                f"coop-{provider}",
            ),
            "tab_created",
        )
        _ignored_workspace, tab_id, pane_id = self._created_ids(result)
        missing = next((
            name
            for name, value in (
                ("tab.tab_id", tab_id),
                ("root_pane.pane_id", pane_id),
            )
            if value is None
        ), None)
        if missing is not None:
            self._rollback_created(
                workspace_id=workspace.workspace_id,
                tab_id=tab_id,
                pane_id=pane_id,
                created_workspace=False,
            )
            raise HerdrProtocolError(f"Herdr response omitted {missing}")
        return workspace, pane_id, tab_id, False

    def _best_effort_close_created(
        self,
        run_id: str,
        workspace: _WorkspaceResource,
        tab_id: str,
        pane_id: str,
        created_workspace: bool,
    ) -> None:
        self._rollback_created(
            workspace_id=workspace.workspace_id,
            tab_id=tab_id,
            pane_id=pane_id,
            created_workspace=created_workspace,
        )
        if created_workspace:
            self._workspaces.pop(run_id, None)

    def spawn_mirror(
        self,
        run_id,
        provider,
        *,
        cwd=None,
        trace_path=None,
        board_path=None,
        environ: Mapping[str, str] | None = None,
    ) -> str:
        """Create a read-only provider mirror and return its opaque pane ID."""
        run_id = _validate_run_id(run_id)
        provider = _validate_provider(provider)
        target_cwd = pathlib.Path(cwd or os.getcwd()).resolve()
        env = os.environ if environ is None else environ
        resolved_trace = (
            _absolute_path(trace_path, setting="COOP_RUN_TRACE_PATH")
            if trace_path is not None
            else resolve_trace_path(run_id, environ=env)
        )
        raw_board = (
            board_path
            or env.get("COOP_DB")
            or env.get("COOP_DB_PATH")
            or coopdb.discover_board(target_cwd)
        )
        bindings = {"COOP_RUN_TRACE_PATH": str(resolved_trace)}
        if raw_board:
            board = pathlib.Path(raw_board).expanduser()
            if not board.is_absolute():
                board = target_cwd / board
            bindings["COOP_DB"] = str(board.resolve())
        bindings["PYTHONPATH"] = _pythonpath_with_install_root(env)
        with self._lock:
            if self._pending:
                raise HerdrInputError(
                    "Herdr cleanup is pending; call teardown before spawning"
                )
            if self._workspaces and run_id not in self._workspaces:
                raise HerdrInputError(
                    "one HerdrAdapter instance may own only one run"
                )
            workspace = self._workspaces.get(run_id)
            if workspace is not None and provider in workspace.mirrors:
                return workspace.mirrors[provider].pane_id

            workspace, pane_id, tab_id, created_workspace = (
                self._created_target(
                    run_id, provider, target_cwd, bindings
                )
            )
            try:
                self._result(
                    ["herdr", "pane", "rename", pane_id, f"coop-{provider}"],
                    "ok",
                )
                self._result(
                    [
                        "herdr", "pane", "run", pane_id,
                        _mirror_shell_command(run_id, provider),
                    ],
                    "ok",
                )
            except HerdrError:
                self._best_effort_close_created(
                    run_id,
                    workspace,
                    tab_id,
                    pane_id,
                    created_workspace,
                )
                raise
            workspace.mirrors[provider] = _MirrorResource(
                provider=provider,
                pane_id=pane_id,
                tab_id=tab_id,
            )
            return pane_id

    def _owned_mirror(self, name: object) -> _MirrorResource:
        if not isinstance(name, str) or not name:
            raise HerdrInputError("mirror name must be a non-empty string")
        matches = [
            mirror
            for workspace in self._workspaces.values()
            for mirror in workspace.mirrors.values()
            if name in (mirror.provider, mirror.pane_id)
        ]
        if len(matches) != 1:
            raise HerdrInputError("mirror is not owned by this adapter")
        return matches[0]

    def workspace_id(self, run_id) -> str | None:
        """Return this adapter's owned workspace ID without a Herdr call."""
        run_id = _validate_run_id(run_id)
        with self._lock:
            workspace = self._workspaces.get(run_id)
            return workspace.workspace_id if workspace is not None else None

    def read(self, name) -> str:
        """Return recent unwrapped text from an adapter-owned mirror pane."""
        with self._lock:
            mirror = self._owned_mirror(name)
            result = self._result(
                [
                    "herdr", "pane", "read", mirror.pane_id,
                    "--source", "recent-unwrapped", "--lines", "120",
                ],
                "pane_read",
            )
        read_result = result.get("read")
        text = (
            read_result.get("text")
            if isinstance(read_result, Mapping)
            else None
        )
        if not isinstance(text, str):
            raise HerdrProtocolError("Herdr pane read response omitted text")
        return text

    def state(self, name) -> dict:
        """Return Herdr's structured state for an adapter-owned mirror pane."""
        with self._lock:
            mirror = self._owned_mirror(name)
            result = self._result(
                ["herdr", "pane", "get", mirror.pane_id],
                "pane_info",
            )
        pane = result.get("pane")
        if not isinstance(pane, dict):
            raise HerdrProtocolError("Herdr pane info response omitted pane")
        return dict(pane)

    def teardown(self, names: Iterable[str]) -> None:
        """Close only adapter-owned resources named by ``names``.

        Unknown names are ignored.  When every remaining mirror in an owned
        workspace is selected, closing that owned workspace is the smallest
        complete cleanup; otherwise only the selected owned tabs are closed.
        """
        requested = {names} if isinstance(names, str) else set(names)
        with self._lock:
            for pending in list(self._pending):
                self._result(list(pending.close_argv), "ok")
                self._pending.remove(pending)
            for run_id, workspace in list(self._workspaces.items()):
                selected = {
                    provider: mirror
                    for provider, mirror in workspace.mirrors.items()
                    if (
                        provider in requested
                        or mirror.pane_id in requested
                    )
                }
                if not selected:
                    continue
                if len(selected) == len(workspace.mirrors):
                    self._result(
                        [
                            "herdr", "workspace", "close",
                            workspace.workspace_id,
                        ],
                        "ok",
                    )
                    self._workspaces.pop(run_id, None)
                    self._pending = [
                        pending
                        for pending in self._pending
                        if pending.workspace_id != workspace.workspace_id
                    ]
                    continue
                for provider, mirror in selected.items():
                    self._result(
                        ["herdr", "tab", "close", mirror.tab_id],
                        "ok",
                    )
                    workspace.mirrors.pop(provider, None)


def resolve_trace_path(
    run_id,
    *,
    environ: Mapping[str, str] | None = None,
) -> pathlib.Path:
    """Return the runner-published absolute trace path for ``run_id``."""
    _validate_run_id(run_id)
    env = os.environ if environ is None else environ
    explicit_trace = env.get("COOP_RUN_TRACE_PATH")
    if not explicit_trace:
        raise HerdrInputError(
            "COOP_RUN_TRACE_PATH must name the mirror's absolute trace path"
        )
    return _absolute_path(explicit_trace, setting="COOP_RUN_TRACE_PATH")


def _display(value: object, *, limit=96) -> str:
    text = str(value)
    safe = "".join(
        character if character.isprintable() and character != "\x1b" else "?"
        for character in text
    )
    return safe[:limit]


def _elapsed_label(value: object) -> str:
    if isinstance(value, bool):
        milliseconds = 0
    else:
        try:
            milliseconds = max(0, int(value))
        except (TypeError, ValueError):
            milliseconds = 0
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"
    return f"{minutes:02}:{seconds:02}.{milliseconds:03}"


def format_mirror_event(event: Mapping) -> str:
    """Render one bounded trace object without dumping arbitrary JSON."""
    event_name = _display(event.get("event", "unknown")).replace("_", " ")
    actor = event.get("provider") or event.get("agent") or "run"
    parts = [
        f"[{_elapsed_label(event.get('elapsed_ms'))}]",
        _display(actor),
        event_name,
    ]
    fields = []
    if event.get("action") is not None:
        fields.append(("action", event["action"]))
    if event.get("turn_id") is not None:
        fields.append(("turn", event["turn_id"]))
    details = event.get("details")
    if isinstance(details, Mapping):
        for key, label in (
            ("worker_mode", "worker"),
            ("classification", "classification"),
            ("board_mutations", "board-mutations"),
            ("exit_code", "exit"),
            ("timed_out", "timed-out"),
            ("error_class", "error"),
        ):
            if key in details:
                fields.append((label, details[key]))
    body = " · ".join(parts[1:] + [
        f"{label}={_display(value)}" for label, value in fields
    ])
    return f"{parts[0]} {body}"


def _read_complete_lines(path: pathlib.Path, position: int):
    records = []
    advanced = False
    try:
        if path.stat().st_size < position:
            position = 0
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(position)
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    position = line_start
                    break
                position = handle.tell()
                advanced = True
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except (OSError, UnicodeError, ValueError):
        return [], position, False
    return records, position, advanced


def render_mirror(
    run_id,
    provider,
    *,
    trace_path=None,
    output: TextIO | None = None,
    poll_interval: float = MIRROR_POLL_INTERVAL_SECONDS,
    max_idle_polls: int | None = None,
    stop_requested: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Incrementally render provider events until terminal, stop, or bound."""
    run_id = _validate_run_id(run_id)
    provider = _validate_provider(provider)
    if max_idle_polls is not None and (
        isinstance(max_idle_polls, bool)
        or not isinstance(max_idle_polls, int)
        or max_idle_polls <= 0
    ):
        raise HerdrInputError("max_idle_polls must be a positive integer")
    try:
        poll_interval = float(poll_interval)
    except (TypeError, ValueError) as exc:
        raise HerdrInputError("poll_interval must be non-negative") from exc
    if poll_interval < 0:
        raise HerdrInputError("poll_interval must be non-negative")

    path = (
        pathlib.Path(trace_path).resolve()
        if trace_path is not None
        else resolve_trace_path(run_id)
    )
    stream = output or sys.stdout
    stopper = stop_requested or (lambda: False)
    print(f"Co-op mirror · {run_id} · {provider}", file=stream, flush=True)
    position = 0
    idle_polls = 0

    try:
        while True:
            if stopper():
                return "stopped"
            records, position, advanced = _read_complete_lines(path, position)
            if advanced:
                idle_polls = 0
            else:
                idle_polls += 1
            for event in records:
                if event.get("run_id") != run_id:
                    continue
                event_name = event.get("event")
                if (
                    event_name in _GLOBAL_EVENTS
                    or event.get("provider") == provider
                    or event.get("agent") == provider
                ):
                    print(format_mirror_event(event), file=stream, flush=True)
                if event_name == "run_finished":
                    return "finished"
            if (
                max_idle_polls is not None
                and idle_polls >= max_idle_polls
            ):
                return "idle_timeout"
            sleep(poll_interval)
    except KeyboardInterrupt:
        return "stopped"


_DEFAULT_ADAPTER = HerdrAdapter()


def available() -> bool:
    return _DEFAULT_ADAPTER.available()


def spawn_mirror(run_id, provider, **kwargs) -> str:
    return _DEFAULT_ADAPTER.spawn_mirror(run_id, provider, **kwargs)


def workspace_id(run_id) -> str | None:
    return _DEFAULT_ADAPTER.workspace_id(run_id)


def read(name) -> str:
    return _DEFAULT_ADAPTER.read(name)


def state(name) -> dict:
    return _DEFAULT_ADAPTER.state(name)


def teardown(names) -> None:
    _DEFAULT_ADAPTER.teardown(names)


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "CORE_PROVIDERS",
    "HerdrAdapter",
    "HerdrCommandError",
    "HerdrError",
    "HerdrInputError",
    "HerdrMirrorTimeout",
    "HerdrProtocolError",
    "HerdrUnavailable",
    "available",
    "format_mirror_event",
    "herdr_cli_env",
    "read",
    "render_mirror",
    "resolve_trace_path",
    "spawn_mirror",
    "state",
    "teardown",
    "workspace_id",
]
