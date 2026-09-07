"""Fail-soft filesystem telemetry for Co-op provider turns.

Trace files are operational evidence only. They never write to the board and
their failure must never interrupt a run.
"""

from __future__ import annotations

import datetime as _datetime
import json
import pathlib
import re
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable


TRACE_VERSION = 2

TRACE_EVENTS = frozenset({
    "run_started",
    "action_eligible",
    "worker_start_requested",
    "worker_handshake_completed",
    "worker_turn_submitted",
    "worker_turn_completed",
    "worker_interrupt_requested",
    "worker_restart_requested",
    "capability_activation_requested",
    "process_tree_prepared",
    "provider_process_started",
    # Legacy producer event. New turns use the two factual milestones above.
    "worker_ready",
    "capability_activation_completed",
    "prompt_submitted",
    "first_provider_output",
    "first_board_mutation",
    "provider_result_received",
    "worker_idle",
    "capability_shutdown",
    "worker_shutdown",
    "final_classification",
    "run_finished",
    "mechanical_precommit",
    "action_became_ready",
    "dispatch_admission_skipped",
    "postwrite_grace_expired",
})

# Canonical detail contract. Public because validators (the provider contract
# smoke) must assert against this set rather than keep a copy — a copied
# allowlist silently rejects fields added by a later batch.
DETAIL_FIELDS = frozenset({
    "worker_mode",
    "external_mcp",
    "exit_code",
    "timed_out",
    "classification",
    "board_mutations",
    "retry",
    "error_class",
    "workflow_recipe",
    "process_starts",
    "worker_reuse_count",
    "capability_activation_mode",
    "persistent_providers",
    "prompt_prefix_version",
    "prompt_prefix_sha256",
    "prompt_prefix_bytes",
    "prompt_cache_mode",
    "prompt_hydration_bytes",
    "workspace_surface",
    "execution_mode",
    "action_fingerprint",
    "actor_last_mutation_to_exit_ms",
    "target_id",
    "model_id",
    "input_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "model_calls",
    "usage_observation",
    "structured_answer_sha256",
    "dispatch_lane",
    "admission_reasons",
    "in_flight_profiles",
})

_IDENTITY_FIELDS = (
    "turn_id",
    "agent",
    "provider",
    "action",
    "capability_set",
)


def trace_path_for_log(log_path) -> str:
    """Return the JSONL trace path beside a human-readable run log."""
    return str(pathlib.Path(log_path).with_suffix(".trace.jsonl"))


def _safe_name_part(value: object) -> str:
    part = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-_")
    return part or "unknown"


def default_trace_path(board_path, *, stamp=None, nonce=None) -> str:
    """Return a unique ignored trace path scoped to the selected board."""
    board = pathlib.Path(board_path)
    if stamp is None:
        stamp = _datetime.datetime.now(
            _datetime.timezone.utc
        ).strftime("%Y%m%d-%H%M%S")
    if nonce is None:
        nonce = secrets.token_hex(4)
    name = (
        f"run-{_safe_name_part(stamp)}-{_safe_name_part(nonce)}"
        ".trace.jsonl"
    )
    return str(board.parent / ".coop-runs" / name)


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


class TurnTrace:
    """Append-only, per-run JSONL trace writer."""

    def __init__(
        self,
        path,
        *,
        run_id,
        item_id,
        monotonic: Callable[[], float] | None = None,
        wall_clock: Callable[[], str] | None = None,
    ):
        self.path = pathlib.Path(path)
        self.run_id = run_id
        self.item_id = item_id
        self.monotonic = monotonic or time.monotonic
        self.wall_clock = wall_clock or _utc_now
        self.started = self.monotonic()
        self._lock = threading.Lock()

    def emit(
        self,
        event,
        *,
        turn_id=None,
        agent=None,
        provider=None,
        action=None,
        capability_set=None,
        details=None,
    ) -> bool:
        """Append one bounded event, returning False on telemetry failure."""
        try:
            with self._lock:
                if event not in TRACE_EVENTS:
                    raise ValueError(f"unsupported trace event: {event!r}")
                if details is not None and not isinstance(details, Mapping):
                    raise TypeError("trace details must be a mapping")

                record: dict[str, Any] = {
                    "version": TRACE_VERSION,
                    "run_id": self.run_id,
                    "event": event,
                    "at": self.wall_clock(),
                    "elapsed_ms": max(
                        0,
                        int(round(
                            (self.monotonic() - self.started) * 1000
                        )),
                    ),
                    "item_id": self.item_id,
                }
                identities = {
                    "turn_id": turn_id,
                    "agent": agent,
                    "provider": provider,
                    "action": action,
                    "capability_set": capability_set,
                }
                for field in _IDENTITY_FIELDS:
                    if identities[field] is not None:
                        record[field] = identities[field]

                if details is not None:
                    filtered = {
                        key: value
                        for key, value in details.items()
                        if key in DETAIL_FIELDS
                    }
                    if filtered:
                        record["details"] = filtered

                payload = json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open(
                    "a",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    handle.write(payload)
                    handle.write("\n")
                    handle.flush()
            return True
        except (OSError, TypeError, ValueError):
            return False


def read_events(path) -> list[dict]:
    """Read valid JSON-object lines, ignoring malformed trace fragments."""
    events: list[dict] = []
    try:
        with pathlib.Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except (OSError, TypeError, ValueError):
        return events
    return events


__all__ = [
    "TRACE_EVENTS",
    "TRACE_VERSION",
    "TurnTrace",
    "default_trace_path",
    "read_events",
    "trace_path_for_log",
]
