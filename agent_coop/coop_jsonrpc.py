"""Small, bounded JSON-lines RPC client for owned provider processes."""

from __future__ import annotations

import collections
import json
import queue
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


class JsonRpcError(RuntimeError):
    """Base class for stable provider-protocol failures."""


class JsonRpcProtocolError(JsonRpcError):
    pass


class JsonRpcTimeout(JsonRpcError):
    pass


class JsonRpcRemoteError(JsonRpcError):
    def __init__(self, code=None):
        super().__init__("json_rpc_remote_error")
        self.code = code


class JsonLineRpcClient:
    """Threaded JSON-RPC client over binary line-oriented streams."""

    def __init__(
        self,
        stdin,
        stdout,
        stderr=None,
        *,
        server_request_handler: Callable[[str, Any], Any] | None = None,
        stderr_limit=4096,
        notification_limit=256,
        notification_filter: Callable[[str], bool] | None = None,
        notification_observer: Callable[[str, Any], None] | None = None,
    ):
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._server_request_handler = server_request_handler
        self._stderr_limit = max(0, int(stderr_limit))
        self._notification_limit = max(0, int(notification_limit))
        self._notification_filter = notification_filter
        self._notification_observer = notification_observer
        self._stderr_tail = collections.deque()
        self._stderr_chars = 0
        self._pending: dict[object, queue.Queue] = {}
        self._notifications = collections.deque(
            maxlen=self._notification_limit,
        )
        self._next_id = 1
        self._failure: JsonRpcError | None = None
        self._closed = False
        self._started = False
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._notification_condition = threading.Condition(
            self._state_lock,
        )
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            if self._closed:
                raise JsonRpcProtocolError("json_rpc_closed")
            self._started = True
        reader = threading.Thread(
            target=self._read_stdout,
            name="coop-jsonrpc-stdout",
            daemon=True,
        )
        self._threads.append(reader)
        reader.start()
        if self._stderr is not None:
            stderr_reader = threading.Thread(
                target=self._read_stderr,
                name="coop-jsonrpc-stderr",
                daemon=True,
            )
            self._threads.append(stderr_reader)
            stderr_reader.start()

    def request(
        self,
        method,
        params=None,
        *,
        timeout_s=30,
        on_submitted: Callable[[], None] | None = None,
    ):
        with self._state_lock:
            self._raise_failure_unlocked()
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": str(method),
        }
        if params is not None:
            payload["params"] = params
        try:
            self._send(payload, on_submitted=on_submitted)
            try:
                response = response_queue.get(
                    timeout=max(0.0, float(timeout_s)),
                )
            except queue.Empty as exc:
                raise JsonRpcTimeout("json_rpc_timeout") from exc
            if isinstance(response, BaseException):
                raise response
            if "error" in response:
                error = response.get("error")
                code = (
                    error.get("code")
                    if isinstance(error, Mapping)
                    else None
                )
                raise JsonRpcRemoteError(code)
            return response.get("result")
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def notify(self, method, params=None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": str(method),
        }
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def wait_notification(
        self,
        method,
        *,
        predicate: Callable[[Any], bool] | None = None,
        timeout_s=30,
    ):
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._notification_condition:
            while True:
                self._raise_failure_unlocked()
                for index, notification in enumerate(self._notifications):
                    if notification.get("method") != method:
                        continue
                    params = notification.get("params")
                    if predicate is not None and not predicate(params):
                        continue
                    del self._notifications[index]
                    return params
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JsonRpcTimeout("json_rpc_notification_timeout")
                self._notification_condition.wait(remaining)

    @property
    def stderr_tail(self) -> str:
        with self._state_lock:
            return "".join(self._stderr_tail)[-self._stderr_limit:]

    @property
    def notification_backlog(self) -> int:
        with self._state_lock:
            return len(self._notifications)

    def close(self) -> None:
        with self._notification_condition:
            if self._closed:
                return
            self._closed = True
            failure = JsonRpcProtocolError("json_rpc_closed")
            if self._failure is None:
                self._failure = failure
            else:
                failure = self._failure
            pending = list(self._pending.values())
            self._notification_condition.notify_all()
        for target in pending:
            try:
                target.put_nowait(failure)
            except queue.Full:
                pass
        try:
            self._stdin.close()
        except (AttributeError, OSError, ValueError):
            pass
        for thread in self._threads:
            thread.join(timeout=0.2)

    def _send(self, payload, *, on_submitted=None) -> None:
        data = (
            json.dumps(payload, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self._write_lock:
            with self._state_lock:
                self._raise_failure_unlocked()
                if self._closed:
                    raise JsonRpcProtocolError("json_rpc_closed")
            try:
                self._stdin.write(data)
                if on_submitted is not None:
                    try:
                        on_submitted()
                    except Exception:
                        pass
                self._stdin.flush()
            except (OSError, ValueError) as exc:
                failure = JsonRpcProtocolError("json_rpc_write_failed")
                self._fail(failure)
                raise failure from exc

    def _read_stdout(self) -> None:
        while True:
            try:
                raw = self._stdout.readline()
            except (OSError, ValueError):
                self._fail(
                    JsonRpcProtocolError("json_rpc_read_failed"),
                )
                return
            if not raw:
                with self._state_lock:
                    closed = self._closed
                if not closed:
                    self._fail(
                        JsonRpcProtocolError("json_rpc_eof"),
                    )
                return
            try:
                text = (
                    raw.decode("utf-8")
                    if isinstance(raw, bytes)
                    else str(raw)
                )
                message = json.loads(text)
                if not isinstance(message, dict):
                    raise ValueError
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self._fail(
                    JsonRpcProtocolError("json_rpc_malformed"),
                )
                return
            self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and method is None:
            with self._state_lock:
                target = self._pending.get(request_id)
            if target is not None:
                try:
                    target.put_nowait(message)
                except queue.Full:
                    pass
            return
        if isinstance(method, str) and request_id is None:
            observer = self._notification_observer
            if observer is not None:
                try:
                    observer(method, message.get("params"))
                except Exception:
                    pass
            notification_filter = self._notification_filter
            if (
                notification_filter is not None
                and not notification_filter(method)
            ):
                return
            with self._notification_condition:
                self._notifications.append(message)
                self._notification_condition.notify_all()
            return
        if isinstance(method, str) and request_id is not None:
            self._handle_server_request(request_id, method, message.get(
                "params",
            ))
            return
        self._fail(JsonRpcProtocolError("json_rpc_invalid_message"))

    def _handle_server_request(self, request_id, method, params) -> None:
        try:
            if self._server_request_handler is None:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "method not supported",
                    },
                }
            else:
                result = self._server_request_handler(method, params)
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": result,
                }
            self._send(response)
        except JsonRpcError:
            return

    def _read_stderr(self) -> None:
        while True:
            try:
                chunk = self._stderr.read(4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            text = (
                chunk.decode("utf-8", errors="replace")
                if isinstance(chunk, bytes)
                else str(chunk)
            )
            if not self._stderr_limit:
                continue
            with self._state_lock:
                self._stderr_tail.append(text)
                self._stderr_chars += len(text)
                while (
                    self._stderr_tail
                    and self._stderr_chars > self._stderr_limit * 2
                ):
                    removed = self._stderr_tail.popleft()
                    self._stderr_chars -= len(removed)

    def _fail(self, failure: JsonRpcError) -> None:
        with self._notification_condition:
            if self._failure is None and not self._closed:
                self._failure = failure
            pending = list(self._pending.values())
            self._notification_condition.notify_all()
        for target in pending:
            try:
                target.put_nowait(failure)
            except queue.Full:
                pass

    def _raise_failure_unlocked(self) -> None:
        if self._failure is not None:
            raise self._failure


__all__ = [
    "JsonLineRpcClient",
    "JsonRpcError",
    "JsonRpcProtocolError",
    "JsonRpcRemoteError",
    "JsonRpcTimeout",
]
