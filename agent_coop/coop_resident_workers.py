"""Resident provider transports for Claude stream JSON and Grok ACP.

These adapters own one provider process for a Co-op run.  They deliberately
discard model payload text, expose only lifecycle/usage metadata, and never
replay a failed turn automatically.  The scheduler may retry after refreshing
the canonical board state.
"""

from __future__ import annotations

import collections
import json
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping

from agent_coop import coop_jsonrpc
from agent_coop import coop_process
from agent_coop import coop_prompt_cache
from agent_coop import coop_start
from agent_coop import coop_workers


def _strip_option(argv, flag, *, takes_value=True):
    result = list(argv)
    while flag in result:
        position = result.index(flag)
        width = 2 if takes_value and position + 1 < len(result) else 1
        del result[position:position + width]
    prefix = flag + "="
    result = [value for value in result if not value.startswith(prefix)]
    return result


def claude_stream_argv(
    base_argv,
    *,
    provider_session_id,
    resume,
):
    """Freeze Claude's bidirectional JSON transport and explicit session."""
    argv = list(base_argv)
    if not any(flag in argv for flag in ("-p", "--print")):
        raise ValueError("Claude stream transport requires print mode")
    argv = _strip_option(argv, "--input-format")
    argv = _strip_option(argv, "--output-format")
    argv = coop_start.apply_provider_session(
        argv,
        provider="claude",
        provider_session_id=provider_session_id,
        resume=resume,
    )
    positions = [
        argv.index(flag)
        for flag in ("-p", "--print")
        if flag in argv
    ]
    position = min(positions)
    stream_flags = [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]
    if "--verbose" not in argv:
        stream_flags.append("--verbose")
    argv[position:position] = stream_flags
    return argv


class JsonLineMessageClient:
    """Bounded JSON-lines stream used by Claude's bidirectional print mode."""

    def __init__(
        self,
        stdin,
        stdout,
        stderr=None,
        *,
        message_filter: Callable[[Mapping], bool] | None = None,
        message_observer: Callable[[Mapping], None] | None = None,
        message_limit=32,
        stderr_limit=4096,
    ):
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._message_filter = message_filter
        self._message_observer = message_observer
        self._messages = collections.deque(maxlen=max(1, int(message_limit)))
        self._stderr_tail = collections.deque()
        self._stderr_chars = 0
        self._stderr_limit = max(0, int(stderr_limit))
        self._failure = None
        self._closed = False
        self._started = False
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._threads = []

    def start(self):
        with self._condition:
            if self._started:
                return
            if self._closed:
                raise coop_jsonrpc.JsonRpcProtocolError(
                    "json_stream_closed"
                )
            self._started = True
        reader = threading.Thread(
            target=self._read_stdout,
            name="coop-claude-stream-stdout",
            daemon=True,
        )
        self._threads.append(reader)
        reader.start()
        if self._stderr is not None:
            stderr_reader = threading.Thread(
                target=self._read_stderr,
                name="coop-claude-stream-stderr",
                daemon=True,
            )
            self._threads.append(stderr_reader)
            stderr_reader.start()

    def send(self, payload, *, on_submitted=None):
        data = (
            json.dumps(payload, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self._write_lock:
            with self._condition:
                self._raise_failure_unlocked()
                if self._closed:
                    raise coop_jsonrpc.JsonRpcProtocolError(
                        "json_stream_closed"
                    )
            try:
                self._stdin.write(data)
                self._stdin.flush()
            except (OSError, ValueError) as exc:
                failure = coop_jsonrpc.JsonRpcProtocolError(
                    "json_stream_write_failed"
                )
                self._fail(failure)
                raise failure from exc
            if on_submitted is not None:
                try:
                    on_submitted()
                except Exception:
                    pass

    def wait_message(self, *, timeout_s=30):
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            while True:
                if self._messages:
                    return self._messages.popleft()
                self._raise_failure_unlocked()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise coop_jsonrpc.JsonRpcTimeout(
                        "json_stream_timeout"
                    )
                self._condition.wait(remaining)

    @property
    def stderr_tail(self):
        with self._condition:
            return "".join(self._stderr_tail)[-self._stderr_limit:]

    @property
    def message_backlog(self):
        with self._condition:
            return len(self._messages)

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            if self._failure is None:
                self._failure = coop_jsonrpc.JsonRpcProtocolError(
                    "json_stream_closed"
                )
            self._condition.notify_all()
        try:
            self._stdin.close()
        except (AttributeError, OSError, ValueError):
            pass
        for thread in self._threads:
            thread.join(timeout=0.2)

    def _read_stdout(self):
        while True:
            try:
                raw = self._stdout.readline()
            except (OSError, ValueError):
                self._fail(coop_jsonrpc.JsonRpcProtocolError(
                    "json_stream_read_failed"
                ))
                return
            if not raw:
                with self._condition:
                    closed = self._closed
                if not closed:
                    self._fail(coop_jsonrpc.JsonRpcProtocolError(
                        "json_stream_eof"
                    ))
                return
            try:
                text = (
                    raw.decode("utf-8")
                    if isinstance(raw, bytes)
                    else str(raw)
                )
                message = json.loads(text)
                if not isinstance(message, Mapping):
                    raise ValueError
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self._fail(coop_jsonrpc.JsonRpcProtocolError(
                    "json_stream_malformed"
                ))
                return
            observer = self._message_observer
            if observer is not None:
                try:
                    observer(message)
                except Exception:
                    pass
            message_filter = self._message_filter
            if message_filter is not None and not message_filter(message):
                continue
            with self._condition:
                self._messages.append(dict(message))
                self._condition.notify_all()

    def _read_stderr(self):
        while True:
            try:
                chunk = self._stderr.read(4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if not self._stderr_limit:
                continue
            text = (
                chunk.decode("utf-8", errors="replace")
                if isinstance(chunk, bytes)
                else str(chunk)
            )
            with self._condition:
                self._stderr_tail.append(text)
                self._stderr_chars += len(text)
                while (
                    self._stderr_tail
                    and self._stderr_chars > self._stderr_limit * 2
                ):
                    removed = self._stderr_tail.popleft()
                    self._stderr_chars -= len(removed)

    def _fail(self, failure):
        with self._condition:
            if self._failure is None and not self._closed:
                self._failure = failure
            self._condition.notify_all()

    def _raise_failure_unlocked(self):
        if self._failure is not None:
            raise self._failure


class _ResidentWorkerBase:
    mode = "persistent"
    provider = None

    def __init__(
        self,
        *,
        cwd,
        env,
        handshake_timeout_s=30,
        tree_factory=None,
    ):
        self.cwd = str(cwd)
        self.env = dict(env)
        self.handshake_timeout_s = float(handshake_timeout_s)
        self._tree_factory = tree_factory or coop_process.prepare_tree
        self._state = "new"
        self._process_starts = 0
        self._turns_submitted = 0
        self._restart_used = False
        self._tree = None
        self._transport = None
        self._trace = None
        self._core_profile = {}
        self._active_trace_turn = None
        self._first_output_emitted = False
        self._state_lock = threading.Lock()
        self._submit_lock = threading.Lock()

    def start(self, core_profile, *, trace=None):
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
            self._launch_initial(turn=trace_turn)
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
                self._turns_submitted += 1
                self._active_trace_turn = turn
                self._first_output_emitted = False
                details = self._details_unlocked()
            self._emit("worker_turn_submitted", turn=turn, details=details)
            try:
                result = self._submit_turn(turn, timeout_s=float(timeout_s))
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
            self._emit_turn_result(turn, result)
            return result
        finally:
            with self._state_lock:
                self._active_trace_turn = None
                self._first_output_emitted = False
            self._submit_lock.release()

    def health(self):
        with self._state_lock:
            return self._health_unlocked()

    def stop(self):
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
        self._emit("worker_shutdown", details=self._details())

    def _start_tree(self, argv, *, turn=None):
        prepared = self._tree_factory(
            list(argv),
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
        return tree

    def _protocol_failure(self, turn, *, timed_out=False):
        with self._state_lock:
            shutting_down = self._state in {"stopping", "stopped"}
        if shutting_down:
            # stop() closes the transport outside the lock and owns teardown.
            # A submit thread woken by that close must never relaunch.
            return self._failed_protocol_result(
                turn,
                retryable=False,
                timed_out=timed_out,
            )
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
                )
            try:
                self._launch_recovery(turn=turn)
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
                    )
                with self._state_lock:
                    self._state = "unhealthy"
            else:
                with self._state_lock:
                    shutting_down = self._state in {"stopping", "stopped"}
                    if not shutting_down:
                        self._state = "ready"
                        retryable = True
                if shutting_down:
                    # Shutdown began while the replacement was starting; drop
                    # it rather than going ready after stop().
                    try:
                        self._cleanup_current()
                    except coop_workers.WorkerCleanupError:
                        with self._state_lock:
                            self._state = "cleanup_failed"
                    return self._failed_protocol_result(
                        turn,
                        retryable=False,
                        timed_out=timed_out,
                    )
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
                )
            with self._state_lock:
                self._state = "unhealthy"
        return self._failed_protocol_result(
            turn,
            retryable=retryable,
            timed_out=timed_out,
        )

    def _failed_protocol_result(self, turn, *, retryable, timed_out):
        classification = (
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
            retryable=bool(retryable),
        )
        self._emit_turn_result(turn, result, timed_out=timed_out)
        return result

    def _cleanup_current(self):
        transport = self._transport
        tree = self._tree
        failures = []
        # Close the tree first. Reader threads parked in readline() then see
        # EOF at once, so the transport close does not wait out a join timeout
        # per reader on every stop, restart, and interrupt.
        if tree is not None:
            try:
                tree.close()
            except Exception as exc:
                failures.append(("tree", type(exc).__name__))
            else:
                self._tree = None
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                failures.append(("transport", type(exc).__name__))
            else:
                self._transport = None
        if failures:
            raise coop_workers.WorkerCleanupError((
                (f"{self.provider}:{owner}", error_class)
                for owner, error_class in failures
            ))

    def _observe_output(self, *_args):
        with self._state_lock:
            turn = self._active_trace_turn
            if turn is None or self._first_output_emitted:
                return
            self._first_output_emitted = True
        self._emit(
            "first_provider_output",
            turn=turn,
            details=self._details(),
        )

    def _emit_turn_result(self, turn, result, *, timed_out=False):
        details = self._details()
        received = {
            **details,
            "exit_code": result.exit,
            "timed_out": bool(timed_out),
        }
        if result.classification is not None:
            received["classification"] = result.classification
        for key in coop_prompt_cache.USAGE_TRACE_FIELDS:
            value = result.extra.get(key)
            if value is not None:
                received[key] = value
        self._emit(
            "provider_result_received",
            turn=turn,
            details=received,
        )
        self._emit("worker_turn_completed", turn=turn, details=details)
        self._emit("worker_idle", turn=turn, details=details)

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
                turn.action.get("kind") if turn is not None else None
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


class ClaudeStreamWorker(_ResidentWorkerBase):
    """One bidirectional Claude stream-JSON process per Co-op run."""

    provider = "claude"

    def __init__(
        self,
        *,
        argv_factory,
        cwd,
        env,
        provider_session_id_factory=None,
        resume_initial=False,
        handshake_timeout_s=30,
        tree_factory=None,
        stream_client_factory=None,
    ):
        super().__init__(
            cwd=cwd,
            env=env,
            handshake_timeout_s=handshake_timeout_s,
            tree_factory=tree_factory,
        )
        self._argv_factory = argv_factory
        self._provider_session_id_factory = (
            provider_session_id_factory or (lambda: str(uuid.uuid4()))
        )
        self._provider_session_id = None
        self._resume_initial = bool(resume_initial)
        self._session_created = False
        self._pending_session_adoption = False
        self._handshake_emitted_for_start = 0
        self._stream_client_factory = (
            stream_client_factory or JsonLineMessageClient
        )

    @property
    def provider_session_id(self):
        return self._provider_session_id

    @property
    def validated_provider_session_id(self):
        with self._state_lock:
            if self._session_created:
                return self._provider_session_id
        return None

    @property
    def stderr_tail(self):
        transport = self._transport
        return transport.stderr_tail if transport is not None else ""

    @property
    def message_backlog(self):
        transport = self._transport
        return transport.message_backlog if transport is not None else 0

    def _launch_initial(self, *, turn=None):
        self._provider_session_id = str(
            self._provider_session_id_factory()
        )
        if not self._provider_session_id:
            raise coop_workers.WorkerUnavailable(self.provider)
        self._launch(resume=self._resume_initial, turn=turn)

    def _launch_recovery(self, *, turn=None):
        if not self._session_created:
            self._provider_session_id = str(
                self._provider_session_id_factory()
            )
            self._resume_initial = False
            resume = False
        else:
            resume = True
        self._launch(resume=resume, turn=turn)

    def _launch(self, *, resume, turn=None):
        argv = self._argv_factory(self._provider_session_id, bool(resume))
        with self._state_lock:
            # A resumed launch passes --resume only. If the installed CLI
            # answers with its own id, the first message of this process
            # adopts it instead of failing the turn.
            self._pending_session_adoption = bool(resume)
        tree = self._start_tree(argv, turn=turn)
        transport = self._stream_client_factory(
            tree.stdin,
            tree.stdout,
            tree.stderr,
            message_limit=16,
            message_filter=lambda message: (
                message.get("type") in {"system", "result"}
            ),
            message_observer=self._observe_message,
        )
        self._transport = transport
        transport.start()

    def _submit_turn(self, turn, *, timeout_s):
        deadline = time.monotonic() + max(0.0, timeout_s)
        payload = {
            "type": "user",
            "message": {
                "role": "user",
                "content": turn.prompt,
            },
            "parent_tool_use_id": None,
            "session_id": self._provider_session_id,
        }
        self._transport.send(
            payload,
            on_submitted=lambda: self._emit(
                "prompt_submitted",
                turn=turn,
                details={
                    **self._details(),
                    **coop_prompt_cache.prompt_trace_details(
                        turn.prompt,
                        provider=self.provider,
                        provider_hint=bool(
                            turn.invoke_kwargs.get("prompt_cache_hint")
                        ),
                    ),
                },
            ),
        )
        while True:
            message = self._transport.wait_message(
                timeout_s=self._remaining(deadline)
            )
            self._validate_session(message)
            if (
                message.get("type") == "system"
                and message.get("subtype") == "init"
            ):
                self._emit_handshake_once(turn)
                continue
            if message.get("type") != "result":
                continue
            successful = (
                message.get("is_error") is not True
                and message.get("subtype")
                not in {"error", "error_max_turns", "error_during_execution"}
            )
            usage = coop_prompt_cache.normalize_cli_usage(
                self.provider,
                message,
            ) or {"usage_observation": "unobserved"}
            if successful:
                with self._state_lock:
                    self._session_created = True
                    self._resume_initial = False
                self._emit_handshake_once(turn)
                return coop_workers.TurnResult(
                    agent=turn.agent_id,
                    provider=self.provider,
                    ok=True,
                    exit=None,
                    note="persistent_turn_completed",
                    tree_empty=False,
                    extra=usage,
                )
            error_text = coop_workers.provider_error_text(
                message.get("result") or message.get("error")
                or message.get("subtype")
            )
            return coop_workers.TurnResult(
                agent=turn.agent_id,
                provider=self.provider,
                ok=False,
                exit=None,
                note="persistent_turn_failed",
                tree_empty=False,
                classification="provider_turn_failed",
                retryable=False,
                extra={
                    **usage,
                    **({"provider_error": error_text} if error_text else {}),
                },
            )

    def interrupt(self, turn_id):
        with self._state_lock:
            active = self._active_trace_turn
            if active is None or active.turn_id != turn_id:
                # No matching turn is in flight. Closing a healthy transport
                # here would spend the one-shot restart on nothing.
                return
        self._emit(
            "worker_interrupt_requested",
            turn_id=turn_id,
            details=self._details(),
        )
        # Stream JSON has no version-stable cancellation handshake. Closing
        # the owned transport wakes the pending turn; the normal bounded
        # restart path then restores the explicit session without replay.
        transport = self._transport
        if transport is not None:
            transport.close()

    def _observe_message(self, message):
        if message.get("type") not in {"system", "result"}:
            self._observe_output()

    def _validate_session(self, message):
        observed = message.get("session_id")
        if observed is None:
            return
        observed = str(observed)
        with self._state_lock:
            if observed == self._provider_session_id:
                self._pending_session_adoption = False
                return
            if not self._pending_session_adoption:
                raise coop_jsonrpc.JsonRpcProtocolError(
                    "claude_session_mismatch"
                )
            # Adopt once per resumed process. Every later change of identity
            # is a real mismatch and fails the turn.
            self._pending_session_adoption = False
            self._provider_session_id = observed

    def _emit_handshake_once(self, turn):
        with self._state_lock:
            process_start = self._process_starts
            if self._handshake_emitted_for_start == process_start:
                return
            self._handshake_emitted_for_start = process_start
        self._emit(
            "worker_handshake_completed",
            turn=turn,
            details=self._details(),
        )


class GrokAcpWorker(_ResidentWorkerBase):
    """One run-owned Grok ACP stdio process and session."""

    provider = "grok"

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
        super().__init__(
            cwd=cwd,
            env=env,
            handshake_timeout_s=handshake_timeout_s,
            tree_factory=tree_factory,
        )
        self.argv = list(argv)
        self._session_id = None
        self._rpc_client_factory = (
            rpc_client_factory or coop_jsonrpc.JsonLineRpcClient
        )

    @property
    def stderr_tail(self):
        transport = self._transport
        return transport.stderr_tail if transport is not None else ""

    @property
    def notification_backlog(self):
        transport = self._transport
        return (
            transport.notification_backlog
            if transport is not None
            else 0
        )

    def _launch_initial(self, *, turn=None):
        self._launch(resume=False, turn=turn)

    def _launch_recovery(self, *, turn=None):
        self._launch(resume=self._session_id is not None, turn=turn)

    def _launch(self, *, resume, turn=None):
        tree = self._start_tree(self.argv, turn=turn)
        rpc = self._rpc_client_factory(
            tree.stdin,
            tree.stdout,
            tree.stderr,
            notification_limit=1,
            notification_filter=lambda _method: False,
            notification_observer=self._observe_notification,
        )
        self._transport = rpc
        rpc.start()
        initialized = rpc.request(
            "initialize",
            {
                "protocolVersion": 1,
                # Never advertise client filesystem/terminal services that
                # Co-op does not implement. Grok's isolated profile owns its
                # permitted tools in-process.
                "clientCapabilities": {},
            },
            timeout_s=self.handshake_timeout_s,
        )
        capabilities = (
            initialized.get("agentCapabilities")
            if isinstance(initialized, Mapping)
            else None
        )
        load_advertised = bool(
            capabilities.get("loadSession")
            if isinstance(capabilities, Mapping)
            else False
        )
        session_params = {
            "cwd": self.cwd,
            "mcpServers": [],
            "_meta": {"yoloMode": True},
        }
        resumed = False
        if resume and self._session_id is not None and load_advertised:
            try:
                rpc.request(
                    "session/load",
                    {"sessionId": self._session_id, **session_params},
                    timeout_s=self.handshake_timeout_s,
                )
            except coop_jsonrpc.JsonRpcError:
                # session/load is optional and may still refuse this session.
                # A fresh session keeps the worker usable instead of demoting
                # every remaining turn to the cold path.
                resumed = False
            else:
                resumed = True
        if not resumed:
            response = rpc.request(
                "session/new",
                session_params,
                timeout_s=self.handshake_timeout_s,
            )
            session_id = (
                response.get("sessionId")
                if isinstance(response, Mapping)
                else None
            )
            if not session_id:
                raise coop_jsonrpc.JsonRpcProtocolError(
                    "grok_session_id_missing"
                )
            self._session_id = str(session_id)
        self._emit(
            "worker_handshake_completed",
            turn=turn,
            details={
                **self._details(),
                "classification": (
                    "grok_session_loaded"
                    if resumed
                    else "grok_session_new"
                ),
            },
        )

    def _submit_turn(self, turn, *, timeout_s):
        response = self._transport.request(
            "session/prompt",
            {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": turn.prompt}],
            },
            timeout_s=timeout_s,
            on_submitted=lambda: self._emit(
                "prompt_submitted",
                turn=turn,
                details={
                    **self._details(),
                    **coop_prompt_cache.prompt_trace_details(
                        turn.prompt,
                        provider=self.provider,
                    ),
                },
            ),
        )
        response = response if isinstance(response, Mapping) else {}
        usage = coop_prompt_cache.normalize_cli_usage(
            self.provider,
            response,
        ) or {"usage_observation": "unobserved"}
        stop_reason = response.get("stopReason")
        if stop_reason in {"cancelled", "refusal"}:
            return coop_workers.TurnResult(
                agent=turn.agent_id,
                provider=self.provider,
                ok=False,
                exit=None,
                note="persistent_turn_failed",
                tree_empty=False,
                classification="provider_turn_failed",
                retryable=False,
                extra={**usage, "provider_turn_status": stop_reason},
            )
        return coop_workers.TurnResult(
            agent=turn.agent_id,
            provider=self.provider,
            ok=True,
            exit=None,
            note="persistent_turn_completed",
            tree_empty=False,
            extra=usage,
        )

    def interrupt(self, turn_id):
        with self._state_lock:
            active = self._active_trace_turn
            if active is None or active.turn_id != turn_id:
                # No matching turn is in flight; cancelling now would only
                # risk the healthy session and the one-shot restart.
                return
        self._emit(
            "worker_interrupt_requested",
            turn_id=turn_id,
            details=self._details(),
        )
        transport = self._transport
        if transport is None or self._session_id is None:
            return
        try:
            transport.notify(
                "session/cancel",
                {"sessionId": self._session_id},
            )
        except coop_jsonrpc.JsonRpcError:
            pass

    def _observe_notification(self, method, params):
        if method != "session/update" or not isinstance(params, Mapping):
            return
        if params.get("sessionId") != self._session_id:
            return
        self._observe_output()


__all__ = [
    "ClaudeStreamWorker",
    "GrokAcpWorker",
    "JsonLineMessageClient",
    "claude_stream_argv",
]
