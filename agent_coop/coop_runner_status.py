"""Structured, runner-only status sidecars for detached Co-op runs.

The sidecar is operational telemetry, never a coordination channel.  Its
schema deliberately cannot carry task, chat, huddle, review, or deliverable
content.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import uuid
from collections.abc import Mapping

from agent_coop.coop_errors import RUNNER_DETAIL_CODES, evidence_dict, normalize_evidence


TERMINAL_PHASES = frozenset({"failed", "finished", "stalled", "stopped"})
_STATUS_FIELDS = (
    "phase", "started_at", "updated_at", "agent", "action", "reason",
    "reason_code", "evidence", "turns", "herdr",
)
_HERDR_UNSET = object()
_HERDR_CALLER_FIELDS = frozenset({"caller_pane"})
_HERDR_MIRROR_FIELDS = frozenset({"workspace", "panes"})
# A jump target must stay copy-pastable, so IDs are never abbreviated.  When
# the exact segment cannot fit the one-line dashboard notice, the renderer
# falls back to the owning workspace, then omits the segment entirely.
HERDR_JUMP_MAX_CHARS = 96


def canonical_herdr_id(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127
               for character in value)
    ):
        return None
    return value


def caller_pane_from_env(environ=None) -> str | None:
    """Return only a canonical ``HERDR_PANE_ID``, preserving it exactly."""
    source = os.environ if environ is None else environ
    return canonical_herdr_id(source.get("HERDR_PANE_ID"))


def canonical_herdr_metadata(
    *,
    workspace=None,
    panes=None,
    caller_pane=None,
):
    """Build the sole nonempty Herdr sidecar shapes.

    No arguments means no metadata.  A caller pane may stand alone; mirror
    metadata must always carry one workspace and at least one provider pane.
    Invalid programmatic values raise ``ValueError`` so a status write can
    fail without replacing its prior sidecar.
    """
    canonical_caller = None
    if caller_pane is not None:
        canonical_caller = canonical_herdr_id(caller_pane)
        if canonical_caller is None:
            raise ValueError("caller_pane must be a canonical opaque ID")

    mirror_supplied = workspace is not None or panes is not None
    if not mirror_supplied:
        if canonical_caller is None:
            return None
        return {"caller_pane": canonical_caller}

    canonical_workspace = canonical_herdr_id(workspace)
    if canonical_workspace is None:
        raise ValueError("workspace must be a canonical opaque ID")
    if not isinstance(panes, Mapping) or not panes:
        raise ValueError("panes must be a nonempty provider map")
    canonical_panes = {}
    for provider, pane_id in panes.items():
        if (
            not isinstance(provider, str)
            or not provider
            or len(provider) > 128
            or provider != provider.strip()
            or any(ord(character) < 32 or ord(character) == 127
                   for character in provider)
        ):
            raise ValueError("pane provider must be a canonical name")
        canonical_pane = canonical_herdr_id(pane_id)
        if canonical_pane is None:
            raise ValueError("pane ID must be a canonical opaque ID")
        canonical_panes[provider] = canonical_pane

    metadata = {
        "workspace": canonical_workspace,
        "panes": canonical_panes,
    }
    if canonical_caller is not None:
        metadata["caller_pane"] = canonical_caller
    return metadata


def _canonical_programmatic_herdr(value):
    if not isinstance(value, Mapping):
        raise ValueError("herdr metadata must be an object")
    fields = frozenset(value)
    if fields == _HERDR_CALLER_FIELDS:
        caller_pane = canonical_herdr_id(value["caller_pane"])
        if caller_pane is None:
            raise ValueError("caller_pane must be a canonical opaque ID")
        return canonical_herdr_metadata(caller_pane=caller_pane)
    if fields in (
        _HERDR_MIRROR_FIELDS,
        _HERDR_MIRROR_FIELDS | _HERDR_CALLER_FIELDS,
    ):
        caller_pane = value.get("caller_pane")
        if (
            "caller_pane" in fields
            and canonical_herdr_id(caller_pane) is None
        ):
            raise ValueError("caller_pane must be a canonical opaque ID")
        return canonical_herdr_metadata(
            workspace=value["workspace"],
            panes=value["panes"],
            caller_pane=caller_pane,
        )
    raise ValueError("herdr metadata has a noncanonical shape")


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def status_path_for_log(log_path) -> str:
    path = pathlib.Path(log_path)
    return str(path.with_suffix(".status.json"))


def status_path_for_run_artifact(path) -> str:
    """Map a run log or trace path onto its sibling status sidecar.

    Dashboard launches use ``run-….log``; CLI/default traces use
    ``run-….trace.jsonl``. Both must resolve to ``run-….status.json``.
    """
    target = pathlib.Path(path)
    name = target.name
    for suffix in (".trace.jsonl", ".status.json", ".log"):
        if name.endswith(suffix):
            return str(target.with_name(name[: -len(suffix)] + ".status.json"))
    return str(target.with_suffix(".status.json"))


def runs_dir_for_board(board_path) -> pathlib.Path:
    return pathlib.Path(board_path).resolve().parent / ".coop-runs"


def latest_status_path(runs_dir) -> str | None:
    """Return the newest ``*.status.json`` under a board's run directory.

    Used by the dashboard so a later CLI or /coop launch replaces a stuck
    terminal line from an earlier finished run.
    """
    root = pathlib.Path(runs_dir)
    try:
        candidates = list(root.glob("*.status.json"))
    except OSError:
        return None
    best_path = None
    best_mtime = -1
    for candidate in candidates:
        try:
            mtime = candidate.stat().st_mtime_ns
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best_path = candidate
    return str(best_path) if best_path is not None else None


def write_status(path, *, phase, started_at, agent=None, action=None,
                 reason=None, reason_code=None, evidence=None, turns=None,
                 now=None, herdr=_HERDR_UNSET) -> bool:
    """Atomically publish one bounded operational status object.

    Status reporting must never interrupt a product run, so filesystem and
    serialization failures are reported as ``False`` rather than raised.
    """
    target = pathlib.Path(path)
    temp = target.with_name(
        f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        payload = {
            "phase": str(phase),
            "started_at": str(started_at),
            "updated_at": str(now or globals()["now"]()),
        }
        optional = {
            "agent": agent,
            "action": action,
            "reason": reason,
            "turns": turns,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value
        if herdr is not _HERDR_UNSET:
            payload["herdr"] = _canonical_programmatic_herdr(herdr)
        if reason_code is not None:
            if reason_code not in RUNNER_DETAIL_CODES:
                return False
            payload["reason_code"] = reason_code
            payload["evidence"] = evidence_dict(normalize_evidence(evidence))
        payload = {
            key: payload[key] for key in _STATUS_FIELDS if key in payload
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        return True
    except (OSError, TypeError, ValueError, RuntimeError):
        try:
            temp.unlink()
        except OSError:
            pass
        return False


def read_status(path):
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def status_token(path):
    """Small change token for dashboard repaint detection."""
    if not path:
        return None
    target = pathlib.Path(path)
    try:
        stat = target.stat()
        digest = hashlib.blake2b(target.read_bytes(), digest_size=8).digest()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, digest


def is_terminal(status) -> bool:
    return bool(status) and status.get("phase") in TERMINAL_PHASES


def _parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed(started_at, now_value):
    start = _parse_timestamp(started_at)
    end = _parse_timestamp(now_value)
    if start is None or end is None:
        return None
    seconds = max(0, int((end - start).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    return f"{minutes:02}:{seconds:02}"


def _turn_count(value) -> str:
    try:
        turns = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{turns} turn" if turns == 1 else f"{turns} turns"


def format_herdr_jump(status) -> str:
    """Render the newest sidecar's Herdr mirror IDs as a jump target only.

    The segment carries opaque Herdr IDs and nothing else: no task, chat,
    provider action, or other board content.  Only mirror panes are offered,
    because the caller pane is the one the human already occupies.  Returns
    ``""`` whenever the sidecar has no mirror metadata.
    """
    if not isinstance(status, Mapping):
        return ""
    herdr = status.get("herdr")
    if not isinstance(herdr, Mapping):
        return ""
    workspace = canonical_herdr_id(herdr.get("workspace"))
    panes = herdr.get("panes")
    if workspace is None or not isinstance(panes, Mapping) or not panes:
        return ""
    rendered = []
    for provider in sorted(panes):
        pane_id = canonical_herdr_id(panes.get(provider))
        if pane_id is None or not isinstance(provider, str) or not provider:
            # A noncanonical entry can never be a usable jump target, and a
            # partial list would mislead.  Drop the whole segment.
            return ""
        rendered.append(f"{provider}={pane_id}")
    panes_segment = "herdr " + " ".join(rendered)
    if len(panes_segment) <= HERDR_JUMP_MAX_CHARS:
        return panes_segment
    workspace_segment = f"herdr ws={workspace}"
    if len(workspace_segment) <= HERDR_JUMP_MAX_CHARS:
        return workspace_segment
    return ""


def format_status(status, *, now=None) -> str:
    """Render only operational fields from a sidecar."""
    if not isinstance(status, dict):
        return ""
    phase = str(status.get("phase") or "unknown")
    if phase in TERMINAL_PHASES:
        parts = ["runner:", phase]
        reason_code = str(status.get("reason_code") or "")
        if reason_code:
            parts.extend(["·", reason_code])
        reason = str(status.get("reason") or "")
        if reason and reason != phase:
            parts.extend(["·", reason])
        count = _turn_count(status.get("turns"))
        if count:
            parts.extend(["·", count])
        return " ".join(parts)

    if phase == "turn":
        agent = str(status.get("agent") or "agent")
        action = str(status.get("action") or "working")
        label = f"runner: {agent} · {action}"
    elif phase == "checking" and status.get("agent"):
        label = f"runner: checking {status['agent']}"
    else:
        label = f"runner: {phase}"

    elapsed = _elapsed(status.get("started_at"), now or globals()["now"]())
    return f"{label} · {elapsed}" if elapsed else label
