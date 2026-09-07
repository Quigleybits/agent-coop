"""Persistent and resumed provider-worker adapters."""

from __future__ import annotations

import dataclasses
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from agent_coop import coop_jsonrpc
from agent_coop import coop_process
from agent_coop import coop_prompt_cache
from agent_coop import coop_runtime
from agent_coop import coop_start
from agent_coop import coop_workers


def prepare_resumed_state(workspace, provider, *, env=None):
    """Create one private provider-state directory outside the workspace."""
    provider = str(provider)
    if provider != "grok":
        raise ValueError("run-scoped state is currently required only by grok")
    owned = coop_runtime.create_private_directory(
        workspace,
        f"worker-state-{provider}",
        env=env,
    )
    return owned.path, owned.cleanup



class ResumedCliWorker:
    """Run one isolated CLI process per turn against one explicit session.

    This is deliberately called ``resumed``, not ``persistent``: it avoids
    rebuilding model conversation state but records every provider process
    start honestly.
    """

    mode = "resumed"

    @property
    def provider_session_id(self):
        return self._provider_session_id

    @property
    def validated_provider_session_id(self):
        """The session ID only after a successful create/resume proved it.

        A failed initial resume swaps in a fresh, never-attempted ID;
        persisting that for cross-run warmth guarantees the NEXT run a
        failed opening attempt, so warm-state writers must use this
        property, never the raw ID.
        """
        with self._state_lock:
            if self._session_created:
                return self._provider_session_id
        return None

    _RECOVERY_CLASSIFICATIONS = frozenset({
        "worker_start_failed",
        "worker_protocol_failed",
        "resume_failed",
        "turn_timeout",
    })

    def __init__(
        self,
        provider,
        *,
        invoke,
        provider_session_id_factory=None,
        provider_state_dir=None,
        state_cleanup=None,
        provider_state_factory=None,
        resume_initial=False,
    ):
        if provider not in {"claude", "grok"}:
            raise ValueError("resumed CLI is supported for claude or grok")
        # Cross-run warmth: when True, the FIRST submit resumes the supplied
        # provider session instead of creating one. A stale id degrades via
        # the normal resume_failed recovery (fresh id, then create).
        self._resume_initial = bool(resume_initial)
        self.provider = str(provider)
        self._invoke = invoke
        self._provider_session_id_factory = (
            provider_session_id_factory
            or (lambda: str(uuid.uuid4()))
        )
        self._provider_session_id = None
        self._provider_state_dir = (
            str(Path(provider_state_dir).resolve())
            if provider_state_dir is not None
            else None
        )
        self._state_cleanup = state_cleanup
        self._provider_state_factory = provider_state_factory
        self._state = "new"
        self._process_starts = 0
        self._turns_submitted = 0
        self._resume_submissions = 0
        self._session_created = False
        self._recovery_used = False
        self._trace = None
        self._state_lock = threading.Lock()
        self._submit_lock = threading.Lock()

    def start(self, core_profile, *, trace=None):
        del core_profile
        with self._state_lock:
            if self._state == "stopped":
                raise coop_workers.WorkerStopped(self.provider)
            if self._state in {
                "unhealthy",
                "cleanup_failed",
                "stopping",
            }:
                raise coop_workers.WorkerUnavailable(self.provider)
            if self._state == "ready":
                return self._health_unlocked()
            if (
                self._provider_state_dir is None
                and self._provider_state_factory is not None
            ):
                try:
                    state_dir, cleanup = self._provider_state_factory()
                    self._provider_state_dir = str(
                        Path(state_dir).resolve()
                    )
                    self._state_cleanup = cleanup
                except Exception as exc:
                    self._state = "unhealthy"
                    raise coop_workers.WorkerUnavailable(
                        self.provider
                    ) from exc
            self._provider_session_id = str(
                self._provider_session_id_factory()
            )
            if not self._provider_session_id:
                self._state = "unhealthy"
                raise coop_workers.WorkerUnavailable(self.provider)
            self._trace = trace
            self._state = "ready"
            health = self._health_unlocked()
        return health

    def submit(self, turn, *, timeout_s):
        if turn.provider != self.provider:
            raise coop_workers.WorkerUnavailable(turn.provider)
        if not self._submit_lock.acquire(blocking=False):
            raise coop_workers.WorkerBusy(self.provider)
        try:
            with self._state_lock:
                if self._state == "stopped":
                    raise coop_workers.WorkerStopped(self.provider)
                if self._state != "ready":
                    raise coop_workers.WorkerUnavailable(self.provider)
                resume = self._session_created or self._resume_initial
                self._turns_submitted += 1
                if resume:
                    self._resume_submissions += 1
                details = self._details_unlocked()
            self._emit(
                "worker_turn_submitted",
                turn=turn,
                details=details,
            )
            kwargs = turn.cold_kwargs(timeout_s=timeout_s)
            kwargs.update({
                "provider_session_id": self._provider_session_id,
                "resume_provider_session": resume,
                "worker_mode": self.mode,
            })
            if self._provider_state_dir is not None:
                kwargs["provider_state_dir"] = self._provider_state_dir
            raw = self._invoke(**kwargs)
            result = coop_workers.TurnResult.from_mapping(
                raw,
                default_agent=turn.agent_id,
                default_provider=self.provider,
            )
            with self._state_lock:
                if result.process_started is True:
                    self._process_starts += 1
                if result.session_created is True:
                    self._session_created = True
                elif result.ok and resume and self._resume_initial:
                    # The cross-run warm resume worked; the session now
                    # behaves exactly like one created this run.
                    self._session_created = True
                elif not self._session_created:
                    # Failed initial resume falls back to creating fresh.
                    self._resume_initial = False
                    self._provider_session_id = str(
                        self._provider_session_id_factory()
                    )
            if (
                not result.ok
                and result.classification is None
                and result.retryable is None
            ):
                retryable = not self._recovery_used
                classification = (
                    "worker_protocol_failed"
                    if retryable
                    else (
                        "resume_failed"
                        if resume
                        else "worker_protocol_failed"
                    )
                )
                result = coop_workers.TurnResult(
                    agent=result.agent,
                    provider=result.provider,
                    ok=False,
                    exit=result.exit,
                    note=classification,
                    tree_empty=result.tree_empty,
                    classification=classification,
                    retryable=retryable,
                    process_started=result.process_started,
                    session_created=result.session_created,
                    extra=result.extra,
                )
            if (
                not result.ok
                and result.classification
                in self._RECOVERY_CLASSIFICATIONS
                and result.retryable is True
            ):
                if self._recovery_used:
                    result = dataclasses.replace(
                        result,
                        retryable=False,
                    )
                    with self._state_lock:
                        self._state = "unhealthy"
                else:
                    self._recovery_used = True
                    self._emit(
                        "worker_restart_requested",
                        turn=turn,
                        details=self._details(),
                    )
            if (
                result.retryable is False
                and not result.ok
                and result.classification
                in self._RECOVERY_CLASSIFICATIONS
            ):
                with self._state_lock:
                    self._state = "unhealthy"
            self._emit(
                "worker_turn_completed",
                turn=turn,
                details=self._details(),
            )
            return result
        finally:
            self._submit_lock.release()

    def interrupt(self, turn_id):
        self._emit(
            "worker_interrupt_requested",
            turn_id=turn_id,
        )

    def health(self):
        with self._state_lock:
            return self._health_unlocked()

    def stop(self):
        with self._state_lock:
            if self._state == "stopped":
                return
            self._state = "stopping"
            cleanup = self._state_cleanup
        if cleanup is not None:
            try:
                cleanup()
            except Exception as exc:
                with self._state_lock:
                    self._state = "cleanup_failed"
                raise coop_workers.WorkerCleanupError((
                    (self.provider, type(exc).__name__),
                )) from exc
            with self._state_lock:
                self._state_cleanup = None
        with self._state_lock:
            self._state = "stopped"
        self._emit("worker_shutdown")

    def _health_unlocked(self):
        return coop_workers.WorkerHealth(
            provider=self.provider,
            mode=self.mode,
            state=self._state,
            process_starts=self._process_starts,
            turns_submitted=self._turns_submitted,
        )

    def _details(self):
        with self._state_lock:
            return self._details_unlocked()

    def _details_unlocked(self):
        return {
            "worker_mode": self.mode,
            "process_starts": self._process_starts,
            "worker_reuse_count": self._resume_submissions,
        }

    def _emit(
        self,
        event,
        *,
        turn=None,
        turn_id=None,
        details=None,
    ):
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
            "details": details or self._details(),
        }
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


class CodexAppServerWorker:
    provider = "codex"
    mode = "persistent"

    def __init__(
        self,
        *,
        argv,
        cwd,
        env,
        handshake_timeout_s=30,
        tree_factory=None,
        rpc_client_factory=None,
    ):
        self.argv = list(argv)
        self.cwd = str(cwd)
        self.env = dict(env)
        self.handshake_timeout_s = float(handshake_timeout_s)
        self._tree_factory = tree_factory or coop_process.prepare_tree
        self._rpc_client_factory = (
            rpc_client_factory
            or coop_jsonrpc.JsonLineRpcClient
        )
        self._state = "new"
        self._process_starts = 0
        self._turns_submitted = 0
        self._restart_used = False
        self._tree = None
        self._rpc = None
        self._thread_id = None
        self._active_provider_turn_id = None
        self._active_trace_turn = None
        self._first_output_emitted = False
        self._model_id = None
        self._thread_usage_total = None
        self._thread_model_calls_total = 0
        self._last_usage_signature = None
        self._active_usage_baseline = None
        self._usage_by_provider_turn = {}
        self._trace = None
        self._core_profile = {}
        self._state_lock = threading.Lock()
        self._submit_lock = threading.Lock()

    def start(self, core_profile, *, trace=None) -> coop_workers.WorkerHealth:
        with self._state_lock:
            if self._state == "stopped":
                raise coop_workers.WorkerStopped(self.provider)
            if self._state == "ready":
                return self._health_unlocked()
            if self._state in {
                "unhealthy",
                "cleanup_failed",
                "stopping",
            }:
                raise coop_workers.WorkerUnavailable(self.provider)
            self._core_profile = dict(core_profile or {})
            self._trace = trace
            self._state = "starting"
            trace_turn = self._core_profile.get("trace_turn")
        self._emit(
            "worker_start_requested",
            turn=trace_turn,
            details=self._details(),
        )
        try:
            self._launch(resume=False, turn=trace_turn)
        except Exception:
            try:
                self._cleanup_current()
            except coop_workers.WorkerCleanupError:
                with self._state_lock:
                    self._state = "cleanup_failed"
                raise
            with self._state_lock:
                self._state = "unhealthy"
            raise
        with self._state_lock:
            self._state = "ready"
            return self._health_unlocked()

    def submit(
        self,
        turn: coop_workers.WorkerTurn,
        *,
        timeout_s: float,
    ) -> coop_workers.TurnResult:
        if turn.provider != self.provider:
            raise coop_workers.WorkerUnavailable(turn.provider)
        if not self._submit_lock.acquire(blocking=False):
            raise coop_workers.WorkerBusy(self.provider)
        try:
            with self._state_lock:
                if self._state == "stopped":
                    raise coop_workers.WorkerStopped(self.provider)
                if self._state != "ready":
                    raise coop_workers.WorkerUnavailable(self.provider)
                self._turns_submitted += 1
                self._active_trace_turn = turn
                self._first_output_emitted = False
                self._active_usage_baseline = (
                    dict(self._thread_usage_total)
                    if isinstance(self._thread_usage_total, Mapping)
                    else None
                )
                details = self._details_unlocked()
            self._emit(
                "worker_turn_submitted",
                turn=turn,
                details=details,
            )
            deadline = time.monotonic() + max(0.0, float(timeout_s))
            try:
                response = self._rpc.request(
                    "turn/start",
                    {
                        "threadId": self._thread_id,
                        "input": [{
                            "type": "text",
                            "text": turn.prompt,
                        }],
                        "cwd": turn.cwd,
                        "approvalPolicy": self._core_profile.get(
                            "approval_policy",
                            "never",
                        ),
                    },
                    timeout_s=self._remaining(deadline),
                    on_submitted=lambda: self._emit(
                        "prompt_submitted",
                        turn=turn,
                        details={
                            **self._details(),
                            **coop_prompt_cache.prompt_trace_details(
                                turn.prompt,
                                provider=self.provider,
                                provider_hint=bool(
                                    turn.invoke_kwargs.get(
                                        "prompt_cache_hint",
                                    )
                                ),
                            ),
                        },
                    ),
                )
                provider_turn_id = self._turn_id_from(response)
                with self._state_lock:
                    self._active_provider_turn_id = provider_turn_id
                completion_probe = turn.invoke_kwargs.get(
                    "completion_probe"
                )
                if callable(completion_probe):
                    result = self._wait_with_postwrite_grace(
                        turn,
                        provider_turn_id,
                        deadline=deadline,
                        completion_probe=completion_probe,
                    )
                    if result is None:
                        return self._protocol_failure(
                            turn,
                            timed_out=False,
                            classification="postwrite_exit_timeout",
                        )
                else:
                    completed = self._rpc.wait_notification(
                        "turn/completed",
                        predicate=lambda params: self._is_completed_turn(
                            params,
                            provider_turn_id,
                        ),
                        timeout_s=self._remaining(deadline),
                    )
                    result = self._result_from_completion(turn, completed)
                self._emit_turn_result(turn, result)
                return result
            except (
                coop_jsonrpc.JsonRpcError,
                OSError,
                ValueError,
            ) as exc:
                return self._protocol_failure(
                    turn,
                    timed_out=isinstance(
                        exc,
                        coop_jsonrpc.JsonRpcTimeout,
                    ),
                )
            finally:
                with self._state_lock:
                    self._active_provider_turn_id = None
                    self._active_trace_turn = None
                    self._first_output_emitted = False
                    self._active_usage_baseline = None
        finally:
            self._submit_lock.release()

    def interrupt(self, turn_id: str) -> None:
        self._emit(
            "worker_interrupt_requested",
            turn_id=turn_id,
            details=self._details(),
        )
        provider_turn_id = self._active_provider_turn_id
        if provider_turn_id is None or self._rpc is None:
            return
        try:
            self._rpc.request(
                "turn/interrupt",
                {
                    "threadId": self._thread_id,
                    "turnId": provider_turn_id,
                },
                timeout_s=min(5.0, self.handshake_timeout_s),
            )
        except coop_jsonrpc.JsonRpcError:
            pass

    def _wait_with_postwrite_grace(
        self,
        turn,
        provider_turn_id,
        *,
        deadline,
        completion_probe,
    ):
        """Wait in bounded slices so an exact terminal write can start grace."""
        grace_s = self._duration(
            turn.invoke_kwargs.get("post_write_exit_grace_s"),
            coop_start.POST_WRITE_EXIT_GRACE_S,
        )
        shutdown_grace_s = self._duration(
            turn.invoke_kwargs.get("shutdown_grace_s"),
            10.0,
        )
        postwrite_deadline = None

        def wait_completed(timeout_s):
            return self._rpc.wait_notification(
                "turn/completed",
                predicate=lambda params: self._is_completed_turn(
                    params,
                    provider_turn_id,
                ),
                timeout_s=timeout_s,
            )

        while True:
            now = time.monotonic()
            if postwrite_deadline is None:
                try:
                    terminal = bool(completion_probe())
                except Exception:
                    terminal = False
                if terminal:
                    postwrite_deadline = now + grace_s
            if postwrite_deadline is not None and now >= postwrite_deadline:
                # Consume a completion already queued at the boundary before
                # interrupting; wait_notification scans before timing out.
                try:
                    completed = wait_completed(0)
                except coop_jsonrpc.JsonRpcTimeout:
                    completed = None
                if completed is not None:
                    return self._result_from_completion(turn, completed)
                self._emit(
                    "postwrite_grace_expired",
                    turn=turn,
                    details=self._details(),
                )
                self.interrupt(turn.turn_id)
                try:
                    completed = wait_completed(shutdown_grace_s)
                except (
                    coop_jsonrpc.JsonRpcError,
                    OSError,
                    ValueError,
                ):
                    return None
                observed = self._result_from_completion(turn, completed)
                try:
                    completion_probe()
                except Exception:
                    pass
                return coop_workers.TurnResult(
                    agent=turn.agent_id,
                    provider=self.provider,
                    ok=False,
                    exit=None,
                    note="postwrite_exit_timeout",
                    tree_empty=False,
                    classification="postwrite_exit_timeout",
                    retryable=False,
                    extra=observed.extra,
                )
            try:
                remaining = self._remaining(deadline)
            except coop_jsonrpc.JsonRpcTimeout:
                # Preserve the original wait's boundary behavior: a queued
                # completion wins over timeout.
                return self._result_from_completion(
                    turn,
                    wait_completed(0),
                )
            wait_s = min(0.1, remaining)
            if postwrite_deadline is not None:
                wait_s = min(
                    wait_s,
                    max(0.0, postwrite_deadline - time.monotonic()),
                )
            try:
                completed = wait_completed(wait_s)
            except coop_jsonrpc.JsonRpcTimeout:
                continue
            return self._result_from_completion(turn, completed)

    def health(self) -> coop_workers.WorkerHealth:
        with self._state_lock:
            return self._health_unlocked()

    @property
    def stderr_tail(self) -> str:
        rpc = self._rpc
        return rpc.stderr_tail if rpc is not None else ""

    @property
    def notification_backlog(self) -> int:
        rpc = self._rpc
        return rpc.notification_backlog if rpc is not None else 0

    def stop(self) -> None:
        with self._state_lock:
            if self._state == "stopped":
                return
            self._state = "stopping"
        try:
            self._cleanup_current()
        except coop_workers.WorkerCleanupError:
            with self._state_lock:
                self._state = "cleanup_failed"
            raise
        with self._state_lock:
            self._state = "stopped"
        self._emit(
            "worker_shutdown",
            details=self._details(),
        )

    def _launch(self, *, resume: bool, turn=None) -> None:
        prepared = self._tree_factory(
            self.argv,
            session_id=uuid.uuid4().hex,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._emit(
            "process_tree_prepared",
            turn=turn,
            details=self._details(),
        )
        try:
            tree = prepared.release()
        except Exception:
            try:
                prepared.abort()
            except Exception:
                pass
            raise
        self._tree = tree
        with self._state_lock:
            self._process_starts += 1
            details = self._details_unlocked()
        self._emit(
            "provider_process_started",
            turn=turn,
            details=details,
        )
        rpc = self._rpc_client_factory(
            tree.stdin,
            tree.stdout,
            tree.stderr,
            notification_limit=32,
            notification_filter=lambda method: (
                method == "turn/completed"
            ),
            notification_observer=self._observe_notification,
        )
        self._rpc = rpc
        rpc.start()
        rpc.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "agent-coop",
                    "version": "1",
                },
                "capabilities": {
                    "experimentalApi": False,
                },
            },
            timeout_s=self.handshake_timeout_s,
        )
        rpc.notify("initialized", {})
        if resume and self._thread_id:
            thread_response = rpc.request(
                "thread/resume",
                {
                    "threadId": self._thread_id,
                    "cwd": self.cwd,
                    "approvalPolicy": self._core_profile.get(
                        "approval_policy",
                        "never",
                    ),
                    "sandbox": self._core_profile.get(
                        "sandbox",
                        "danger-full-access",
                    ),
                },
                timeout_s=self.handshake_timeout_s,
            )
        else:
            thread_response = rpc.request(
                "thread/start",
                {
                    "cwd": self.cwd,
                    "approvalPolicy": self._core_profile.get(
                        "approval_policy",
                        "never",
                    ),
                    "sandbox": self._core_profile.get(
                        "sandbox",
                        "danger-full-access",
                    ),
                    # The isolated run-local CODEX_HOME is cleaned at run end,
                    # but a non-ephemeral thread can be resumed after one
                    # app-server crash inside that run.
                    "ephemeral": False,
                },
                timeout_s=self.handshake_timeout_s,
            )
        thread_id = self._thread_id_from(thread_response)
        if not thread_id:
            raise coop_jsonrpc.JsonRpcProtocolError(
                "codex_thread_id_missing",
            )
        self._thread_id = thread_id
        model_id = self._model_id_from(thread_response)
        if model_id is not None:
            self._model_id = model_id
        if not resume:
            with self._state_lock:
                self._thread_model_calls_total = 0
                self._last_usage_signature = None
                self._thread_usage_total = {
                    key: 0
                    for key in coop_prompt_cache.USAGE_COUNTER_FIELDS
                }
                if self._model_id is not None:
                    self._thread_usage_total["model_id"] = self._model_id
        self._emit(
            "worker_handshake_completed",
            turn=turn,
            details=self._details(),
        )

    def _protocol_failure(
        self,
        turn: coop_workers.WorkerTurn,
        *,
        timed_out=False,
        classification=None,
    ) -> coop_workers.TurnResult:
        with self._state_lock:
            self._thread_usage_total = None
            self._thread_model_calls_total = 0
            self._last_usage_signature = None
            self._active_usage_baseline = None
            self._usage_by_provider_turn.clear()
        retryable = False
        if not self._restart_used:
            self._restart_used = True
            self._emit(
                "worker_restart_requested",
                turn=turn,
                details=self._details(),
            )
            try:
                self._cleanup_current()
            except coop_workers.WorkerCleanupError:
                with self._state_lock:
                    self._state = "cleanup_failed"
                return self._failed_protocol_result(
                    turn,
                    retryable=False,
                    timed_out=timed_out,
                    classification=classification,
                )
            try:
                self._launch(resume=True, turn=turn)
                retryable = True
                with self._state_lock:
                    self._state = "ready"
            except Exception:
                try:
                    self._cleanup_current()
                except coop_workers.WorkerCleanupError:
                    with self._state_lock:
                        self._state = "cleanup_failed"
                    return self._failed_protocol_result(
                        turn,
                        retryable=False,
                        timed_out=timed_out,
                        classification=classification,
                    )
                with self._state_lock:
                    self._state = "unhealthy"
        else:
            try:
                self._cleanup_current()
            except coop_workers.WorkerCleanupError:
                with self._state_lock:
                    self._state = "cleanup_failed"
                return self._failed_protocol_result(
                    turn,
                    retryable=False,
                    timed_out=timed_out,
                    classification=classification,
                )
            with self._state_lock:
                self._state = "unhealthy"
        return self._failed_protocol_result(
            turn,
            retryable=(False if classification is not None else retryable),
            timed_out=timed_out,
            classification=classification,
        )

    def _failed_protocol_result(
        self,
        turn,
        *,
        retryable,
        timed_out,
        classification=None,
    ):
        classification = classification or (
            "turn_timeout" if timed_out else "worker_protocol_failed"
        )
        result = coop_workers.TurnResult(
            agent=turn.agent_id,
            provider=self.provider,
            ok=False,
            exit=None,
            note=classification,
            tree_empty=self._tree is None,
            classification=classification,
            retryable=retryable,
        )
        self._emit_turn_result(
            turn,
            result,
            timed_out=timed_out,
        )
        return result

    def _emit_turn_result(
        self,
        turn,
        result,
        *,
        timed_out=False,
    ) -> None:
        details = self._details()
        result_details = {
            **details,
            "exit_code": result.exit,
            "timed_out": bool(timed_out),
        }
        if result.classification is not None:
            result_details["classification"] = result.classification
        for key in ("provider_turn_status", "provider_error"):
            value = result.extra.get(key)
            if value:
                result_details[key] = value
        for key in coop_prompt_cache.USAGE_TRACE_FIELDS:
            value = result.extra.get(key)
            if value is not None:
                result_details[key] = value
        self._emit(
            "provider_result_received",
            turn=turn,
            details=result_details,
        )
        self._emit(
            "worker_turn_completed",
            turn=turn,
            details=details,
        )
        self._emit(
            "worker_idle",
            turn=turn,
            details=details,
        )

    def _result_from_completion(
        self,
        turn: coop_workers.WorkerTurn,
        completed,
    ) -> coop_workers.TurnResult:
        params = completed if isinstance(completed, Mapping) else {}
        provider_turn = params.get("turn")
        provider_turn = (
            provider_turn
            if isinstance(provider_turn, Mapping)
            else {}
        )
        status = provider_turn.get("status")
        provider_turn_id = provider_turn.get("id")
        with self._state_lock:
            usage_record = self._usage_by_provider_turn.pop(
                provider_turn_id,
                {},
            )
            usage = (
                usage_record.get("delta", {})
                if isinstance(usage_record, Mapping)
                else {}
            )
            total = (
                usage_record.get("total")
                if isinstance(usage_record, Mapping)
                else None
            )
            self._thread_usage_total = (
                dict(total)
                if isinstance(total, Mapping)
                else None
            )
        if status == "completed":
            return coop_workers.TurnResult(
                agent=turn.agent_id,
                provider=self.provider,
                ok=True,
                exit=None,
                note="persistent_turn_completed",
                tree_empty=False,
                extra=usage,
            )
        # A non-completed turn carries the app-server's reason. Keep it: a
        # quota or auth refusal is otherwise indistinguishable from a
        # protocol failure in the run log and trace.
        error_text = coop_workers.provider_error_text(
            provider_turn.get("error")
        )
        detail = {"provider_turn_status": status or "unknown"}
        if error_text:
            detail["provider_error"] = error_text
        return coop_workers.TurnResult(
            agent=turn.agent_id,
            provider=self.provider,
            ok=False,
            exit=None,
            note="persistent_turn_failed",
            tree_empty=False,
            classification="provider_turn_failed",
            retryable=False,
            extra={**usage, **detail},
        )

    def _cleanup_current(self) -> None:
        rpc = self._rpc
        tree = self._tree
        failures = []
        if rpc is not None:
            try:
                rpc.close()
            except Exception as exc:
                failures.append(("rpc", type(exc).__name__))
            else:
                self._rpc = None
        if tree is not None:
            try:
                tree.close()
            except Exception as exc:
                failures.append(("tree", type(exc).__name__))
            else:
                self._tree = None
        if failures:
            raise coop_workers.WorkerCleanupError((
                (f"{self.provider}:{owner}", error_class)
                for owner, error_class in failures
            ))

    def _observe_notification(self, method, params) -> None:
        if method == "thread/tokenUsage/updated":
            if not isinstance(params, Mapping):
                return
            if params.get("threadId") != self._thread_id:
                return
            provider_turn_id = params.get("turnId")
            token_usage = params.get("tokenUsage")
            total = (
                token_usage.get("total")
                if isinstance(token_usage, Mapping)
                else None
            )
            base_total = coop_prompt_cache.normalize_codex_usage(total)
            if not provider_turn_id or not base_total:
                return
            with self._state_lock:
                signature = tuple(
                    (key, base_total.get(key))
                    for key in coop_prompt_cache.USAGE_COUNTER_FIELDS
                    if key != "model_calls"
                )
                if signature != self._last_usage_signature:
                    self._thread_model_calls_total += 1
                    self._last_usage_signature = signature
                normalized_total = coop_prompt_cache.normalize_codex_usage(
                    total,
                    model_id=self._model_id,
                    model_calls=self._thread_model_calls_total,
                )
                delta = coop_prompt_cache.codex_usage_delta(
                    normalized_total,
                    self._active_usage_baseline,
                )
                self._usage_by_provider_turn[str(provider_turn_id)] = {
                    "delta": delta,
                    "total": normalized_total,
                }
                while len(self._usage_by_provider_turn) > 8:
                    oldest = next(iter(self._usage_by_provider_turn))
                    self._usage_by_provider_turn.pop(oldest, None)
            return
        if not (
            method == "turn/completed"
            or method.startswith("item/")
            or method.startswith("turn/")
        ):
            return
        with self._state_lock:
            turn = self._active_trace_turn
            if turn is None or self._first_output_emitted:
                return
            if isinstance(params, Mapping):
                thread_id = params.get("threadId")
                if thread_id is not None and thread_id != self._thread_id:
                    return
                provider_turn_id = self._active_provider_turn_id
                if provider_turn_id is not None:
                    event_turn_id = params.get("turnId")
                    nested_turn = params.get("turn")
                    if (
                        event_turn_id is None
                        and isinstance(nested_turn, Mapping)
                    ):
                        event_turn_id = nested_turn.get("id")
                    if (
                        event_turn_id is not None
                        and event_turn_id != provider_turn_id
                    ):
                        return
            self._first_output_emitted = True
        self._emit(
            "first_provider_output",
            turn=turn,
            details=self._details(),
        )

    def _health_unlocked(self) -> coop_workers.WorkerHealth:
        return coop_workers.WorkerHealth(
            provider=self.provider,
            mode=self.mode,
            state=self._state,
            process_starts=self._process_starts,
            turns_submitted=self._turns_submitted,
        )

    def _details(self):
        with self._state_lock:
            return self._details_unlocked()

    def _details_unlocked(self):
        return {
            "worker_mode": self.mode,
            "process_starts": self._process_starts,
            "worker_reuse_count": max(
                0,
                self._turns_submitted - self._process_starts,
            ),
        }

    def _emit(
        self,
        event,
        *,
        turn=None,
        turn_id=None,
        details=None,
    ):
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
            "details": details or self._details(),
        }
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

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise coop_jsonrpc.JsonRpcTimeout("worker_turn_timeout")
        return remaining

    @staticmethod
    def _duration(value, default):
        if value is None:
            return float(default)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _thread_id_from(response):
        if not isinstance(response, Mapping):
            return None
        thread = response.get("thread")
        if not isinstance(thread, Mapping):
            return None
        value = thread.get("id")
        return str(value) if value else None

    @staticmethod
    def _model_id_from(response):
        if not isinstance(response, Mapping):
            return None
        value = response.get("model")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    @staticmethod
    def _turn_id_from(response):
        if not isinstance(response, Mapping):
            raise coop_jsonrpc.JsonRpcProtocolError(
                "codex_turn_missing",
            )
        turn = response.get("turn")
        if not isinstance(turn, Mapping) or not turn.get("id"):
            raise coop_jsonrpc.JsonRpcProtocolError(
                "codex_turn_id_missing",
            )
        return str(turn["id"])

    def _is_completed_turn(self, params, provider_turn_id):
        if not isinstance(params, Mapping):
            return False
        if params.get("threadId") != self._thread_id:
            return False
        turn = params.get("turn")
        return (
            isinstance(turn, Mapping)
            and turn.get("id") == provider_turn_id
        )


__all__ = [
    "CodexAppServerWorker",
    "ResumedCliWorker",
    "prepare_resumed_state",
]
