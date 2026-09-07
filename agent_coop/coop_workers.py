"""Run-scoped provider worker contracts.

Provider adapters normalize onto this module; it owns no board state and
imports no provider SDK.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Protocol


class WorkerError(RuntimeError):
    pass


class WorkerBusy(WorkerError):
    pass


class WorkerStopped(WorkerError):
    pass


class WorkerUnavailable(WorkerError):
    pass


class WorkerCleanupError(WorkerError):
    """Sanitized aggregate while failed resources remain owned for retry."""

    def __init__(self, failures):
        self.failures = tuple(
            (str(owner), str(error_class))
            for owner, error_class in failures
        )
        super().__init__(
            f"worker_cleanup_failed ({len(self.failures)})"
        )


class RunCleanupRegistry:
    """Run-owned cleanup callbacks retained until they succeed."""

    def __init__(self):
        self._entries = {}
        self._next_token = 0
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._entries)

    def retain(self, owner, cleanup) -> int:
        if not callable(cleanup):
            raise TypeError("cleanup must be callable")
        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._entries[token] = (str(owner), cleanup)
        return token

    def drain(self) -> None:
        """Attempt every callback newest-first, retaining only failures."""
        with self._drain_lock:
            with self._lock:
                entries = list(reversed(tuple(self._entries.items())))
            succeeded = []
            failures = []
            for token, (owner, cleanup) in entries:
                try:
                    cleanup()
                except Exception as exc:
                    failures.append((owner, type(exc).__name__))
                else:
                    succeeded.append(token)
            if succeeded:
                with self._lock:
                    for token in succeeded:
                        self._entries.pop(token, None)
            if failures:
                raise WorkerCleanupError(failures)


@dataclasses.dataclass(frozen=True)
class WorkerTurn:
    turn_id: str
    provider: str
    agent_id: str
    prompt: str
    session_id: str
    board_path: str
    cwd: str
    action: Mapping[str, Any]
    invoke_kwargs: Mapping[str, Any] = dataclasses.field(
        default_factory=dict,
    )

    def __post_init__(self):
        object.__setattr__(
            self,
            "action",
            MappingProxyType(dict(self.action)),
        )
        object.__setattr__(
            self,
            "invoke_kwargs",
            MappingProxyType(dict(self.invoke_kwargs)),
        )

    def cold_kwargs(self, *, timeout_s: float) -> dict[str, Any]:
        kwargs = dict(self.invoke_kwargs)
        kwargs.update({
            "provider": self.provider,
            "prompt": self.prompt,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "board_path": self.board_path,
            "cwd": self.cwd,
            "timeout_s": float(timeout_s),
            "action": dict(self.action),
            "turn_id": self.turn_id,
        })
        return kwargs


@dataclasses.dataclass(frozen=True)
class WorkerHealth:
    provider: str
    mode: str
    state: str
    process_starts: int
    turns_submitted: int


@dataclasses.dataclass(frozen=True)
class TurnResult:
    agent: str
    provider: str
    ok: bool
    exit: int | None
    note: str
    tree_empty: bool | None = None
    classification: str | None = None
    retryable: bool | None = None
    process_started: bool | None = None
    session_created: bool | None = None
    extra: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self,
            "extra",
            MappingProxyType(dict(self.extra)),
        )

    @classmethod
    def from_mapping(
        cls,
        value,
        *,
        default_agent: str,
        default_provider: str,
    ) -> "TurnResult":
        payload = dict(value) if isinstance(value, Mapping) else {}
        known = {
            "agent",
            "provider",
            "ok",
            "exit",
            "note",
            "tree_empty",
            "classification",
            "retryable",
            "process_started",
            "session_created",
        }
        return cls(
            agent=str(payload.get("agent") or default_agent),
            provider=str(payload.get("provider") or default_provider),
            ok=bool(payload.get("ok")),
            exit=payload.get("exit"),
            note=str(payload.get("note") or ""),
            tree_empty=payload.get("tree_empty"),
            classification=payload.get("classification"),
            retryable=payload.get("retryable"),
            process_started=payload.get("process_started"),
            session_created=payload.get("session_created"),
            extra={
                key: item
                for key, item in payload.items()
                if key not in known
            },
        )

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "agent": self.agent,
            "provider": self.provider,
            "ok": self.ok,
            "exit": self.exit,
            "note": self.note,
        }
        for key in (
                "tree_empty",
                "classification",
                "retryable",
                "process_started",
                "session_created"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        payload.update(self.extra)
        return payload



PROVIDER_ERROR_MAX_CHARS = 400


def provider_error_text(error) -> str:
    """One bounded line of provider error text, or "" when there is none."""
    if isinstance(error, Mapping):
        parts = [
            str(error.get(key)).strip()
            for key in ("message", "additionalDetails", "code")
            if error.get(key)
        ]
        text = " | ".join(part for part in parts if part)
    elif error is None:
        text = ""
    else:
        text = str(error).strip()
    text = " ".join(text.split())
    if len(text) > PROVIDER_ERROR_MAX_CHARS:
        text = text[:PROVIDER_ERROR_MAX_CHARS - 1] + "…"
    return text


class ProviderWorker(Protocol):
    provider: str
    mode: str

    def start(self, core_profile, *, trace=None) -> WorkerHealth:
        ...

    def submit(
        self,
        turn: WorkerTurn,
        *,
        timeout_s: float,
    ) -> TurnResult:
        ...

    def interrupt(self, turn_id: str) -> None:
        ...

    def health(self) -> WorkerHealth:
        ...

    def stop(self) -> None:
        ...


class ColdCliWorker:
    mode = "cold"

    def __init__(self, provider, *, invoke):
        self.provider = str(provider)
        self._invoke = invoke
        self._state = "new"
        self._process_starts = 0
        self._turns_submitted = 0
        self._state_lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._trace = None

    def start(self, core_profile, *, trace=None) -> WorkerHealth:
        del core_profile
        with self._state_lock:
            if self._state == "stopped":
                raise WorkerStopped(self.provider)
            if self._state == "unhealthy":
                raise WorkerUnavailable(self.provider)
            self._trace = trace
            self._state = "ready"
            return self._health_unlocked()

    def submit(
        self,
        turn: WorkerTurn,
        *,
        timeout_s: float,
    ) -> TurnResult:
        if turn.provider != self.provider:
            raise WorkerUnavailable(
                f"{self.provider} cannot run {turn.provider}",
            )
        if not self._submit_lock.acquire(blocking=False):
            raise WorkerBusy(self.provider)
        try:
            with self._state_lock:
                if self._state == "stopped":
                    raise WorkerStopped(self.provider)
                if self._state != "ready":
                    raise WorkerUnavailable(self.provider)
                self._turns_submitted += 1
                details = self._lifecycle_details_unlocked()
            self._emit(
                "worker_turn_submitted",
                turn=turn,
                details=details,
            )
            raw = self._invoke(**turn.cold_kwargs(timeout_s=timeout_s))
            result = TurnResult.from_mapping(
                raw,
                default_agent=turn.agent_id,
                default_provider=self.provider,
            )
            with self._state_lock:
                if result.process_started is True:
                    self._process_starts += 1
                details = self._lifecycle_details_unlocked()
            self._emit(
                "worker_turn_completed",
                turn=turn,
                details=details,
            )
            return result
        finally:
            self._submit_lock.release()

    def interrupt(self, turn_id: str) -> None:
        self._emit(
            "worker_interrupt_requested",
            turn_id=turn_id,
            details=self._lifecycle_details(),
        )

    def health(self) -> WorkerHealth:
        with self._state_lock:
            return self._health_unlocked()

    def _health_unlocked(self) -> WorkerHealth:
        return WorkerHealth(
            provider=self.provider,
            mode=self.mode,
            state=self._state,
            process_starts=self._process_starts,
            turns_submitted=self._turns_submitted,
        )

    def _lifecycle_details(self) -> dict[str, Any]:
        with self._state_lock:
            return self._lifecycle_details_unlocked()

    def _lifecycle_details_unlocked(self) -> dict[str, Any]:
        return {
            "worker_mode": self.mode,
            "process_starts": self._process_starts,
            "worker_reuse_count": 0,
        }

    def _emit(
        self,
        event: str,
        *,
        turn: WorkerTurn | None = None,
        turn_id: str | None = None,
        details=None,
    ) -> None:
        if self._trace is None:
            return
        fields = {
            "turn_id": turn.turn_id if turn is not None else turn_id,
            "agent": turn.agent_id if turn is not None else None,
            "provider": self.provider,
            "action": (
                turn.action.get("kind")
                if turn is not None
                else None
            ),
            "details": details or self._lifecycle_details(),
        }
        capability = (
            turn.invoke_kwargs.get("capability_manifest")
            if turn is not None
            else None
        )
        if capability is not None:
            fields["capability_set"] = getattr(
                capability,
                "name",
                None,
            )
        try:
            self._trace.emit(
                event,
                **{
                    key: value
                    for key, value in fields.items()
                    if value is not None
                },
            )
        except Exception:
            pass

    def stop(self) -> None:
        with self._state_lock:
            self._state = "stopped"


class WorkerPool:
    def __init__(self, workers):
        self._workers = dict(workers)
        for provider, worker in self._workers.items():
            if provider != worker.provider:
                raise ValueError(
                    f"worker key {provider!r} does not match "
                    f"{worker.provider!r}",
                )
        self._started: list[str] = []
        self._stopped = False
        self._lock = threading.Lock()
        self._start_locks = {
            provider: threading.Lock()
            for provider in self._workers
        }

    def worker(self, provider):
        """Read-only access to one prepared worker (or None)."""
        return self._workers.get(provider)

    def start(
        self,
        provider: str,
        *,
        core_profile=None,
        trace=None,
    ) -> WorkerHealth:
        """Start one provider on first use; repeated starts are idempotent."""
        worker = self._workers.get(provider)
        start_lock = self._start_locks.get(provider)
        if worker is None or start_lock is None:
            raise WorkerUnavailable(provider)
        with start_lock:
            with self._lock:
                if self._stopped:
                    raise WorkerStopped("pool")
                if provider in self._started:
                    health = worker.health()
                    if health.state != "ready":
                        raise WorkerUnavailable(provider)
                    return health
            try:
                health = worker.start(
                    core_profile or {},
                    trace=trace,
                )
            except Exception:
                try:
                    worker.stop()
                except Exception as cleanup_exc:
                    with self._lock:
                        if provider not in self._started:
                            self._started.append(provider)
                    if isinstance(cleanup_exc, WorkerCleanupError):
                        raise
                    raise WorkerCleanupError((
                        (provider, type(cleanup_exc).__name__),
                    )) from cleanup_exc
                raise
            with self._lock:
                if self._stopped:
                    try:
                        worker.stop()
                    except Exception as cleanup_exc:
                        if provider not in self._started:
                            self._started.append(provider)
                        self._stopped = False
                        if isinstance(
                            cleanup_exc,
                            WorkerCleanupError,
                        ):
                            raise cleanup_exc
                        raise WorkerCleanupError((
                            (provider, type(cleanup_exc).__name__),
                        )) from cleanup_exc
                    raise WorkerStopped("pool")
                self._started.append(provider)
            return health

    def start_all(self, *, core_profiles, trace=None) -> dict[str, WorkerHealth]:
        health = {}
        try:
            for provider in self._workers:
                health[provider] = self.start(
                    provider,
                    core_profile=core_profiles.get(provider, {}),
                    trace=trace,
                )
            return health
        except Exception:
            with self._lock:
                self._stop_started_unlocked()
            raise

    def submit(
        self,
        provider: str,
        turn: WorkerTurn,
        *,
        timeout_s: float,
    ) -> TurnResult:
        with self._lock:
            if self._stopped:
                raise WorkerStopped("pool")
            worker = self._workers.get(provider)
            if worker is None or provider not in self._started:
                raise WorkerUnavailable(provider)
            health = worker.health()
            if health.state != "ready":
                raise WorkerUnavailable(provider)
        return worker.submit(turn, timeout_s=timeout_s)

    def interrupt(self, provider: str, turn_id: str) -> None:
        worker = self._workers.get(provider)
        if worker is None:
            raise WorkerUnavailable(provider)
        worker.interrupt(turn_id)

    def health(self) -> dict[str, WorkerHealth]:
        return {
            provider: worker.health()
            for provider, worker in self._workers.items()
        }

    def stop_all(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stop_started_unlocked()
            self._stopped = True

    def _stop_started_unlocked(self) -> None:
        failures = []
        stopped = []
        for provider in reversed(tuple(self._started)):
            try:
                self._workers[provider].stop()
                stopped.append(provider)
            except Exception as exc:
                failures.append((provider, type(exc).__name__))
        for provider in stopped:
            self._started.remove(provider)
        if failures:
            raise WorkerCleanupError(failures)
