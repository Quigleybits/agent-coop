"""Bounded fake-process tests for the JSON-lines RPC transport."""

from __future__ import annotations

import concurrent.futures
import io
import subprocess
import sys
import threading

import pytest

from agent_coop import coop_jsonrpc


def _server_request_process():
    code = r"""
import json
import sys

request = json.loads(sys.stdin.buffer.readline())
server_request = {
    "jsonrpc": "2.0",
    "id": 91,
    "method": "approval/request",
    "params": {"must": "not leak"},
}
sys.stdout.buffer.write(
    (json.dumps(server_request, separators=(",", ":")) + "\n").encode()
)
sys.stdout.buffer.flush()
server_response = json.loads(sys.stdin.buffer.readline())
result = {
    "serverError": server_response["error"]["code"],
    "requestMethod": request["method"],
}
sys.stdout.buffer.write(
    (json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": result,
    }, separators=(",", ":")) + "\n").encode()
)
sys.stdout.buffer.flush()
"""
    return subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_default_server_request_handler_denies_unknown_method_without_deadlock():
    process = _server_request_process()
    client = coop_jsonrpc.JsonLineRpcClient(
        process.stdin,
        process.stdout,
        process.stderr,
    )
    try:
        client.start()
        result = client.request("turn/start", {}, timeout_s=5)
        assert result == {
            "serverError": -32601,
            "requestMethod": "turn/start",
        }
        assert process.wait(timeout=5) == 0
    finally:
        client.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_notification_backlog_is_bounded_and_old_payloads_are_discarded():
    client = coop_jsonrpc.JsonLineRpcClient(
        io.BytesIO(),
        io.BytesIO(),
        notification_limit=3,
    )
    for sequence in range(5):
        client._dispatch({
            "jsonrpc": "2.0",
            "method": "item/delta",
            "params": {
                "sequence": sequence,
                "payload": "must not be retained without a bound",
            },
        })

    assert client.notification_backlog == 3
    with pytest.raises(coop_jsonrpc.JsonRpcTimeout):
        client.wait_notification(
            "item/delta",
            predicate=lambda params: params["sequence"] == 0,
            timeout_s=0,
        )
    assert client.wait_notification(
        "item/delta",
        predicate=lambda params: params["sequence"] == 2,
        timeout_s=0,
    )["sequence"] == 2


def test_close_wakes_pending_requests_without_waiting_for_timeout():
    wrote = threading.Event()
    release_reader = threading.Event()

    class SignallingInput(io.BytesIO):
        def write(self, value):
            result = super().write(value)
            wrote.set()
            return result

    class BlockingOutput:
        def readline(self):
            release_reader.wait(timeout=5)
            return b""

    client = coop_jsonrpc.JsonLineRpcClient(
        SignallingInput(),
        BlockingOutput(),
    )
    client.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.request,
            "turn/start",
            {},
            timeout_s=30,
        )
        assert wrote.wait(timeout=2)
        client.close()
        with pytest.raises(coop_jsonrpc.JsonRpcProtocolError):
            future.result(timeout=2)
    release_reader.set()
