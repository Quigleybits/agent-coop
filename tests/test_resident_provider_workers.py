"""Offline protocol tests for resident Claude and Grok workers."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from agent_coop import coop_jsonrpc
from agent_coop import coop_resident_workers
from agent_coop import coop_workers


class _RecordingTrace:
    def __init__(self):
        self.records = []

    def emit(self, event, **fields):
        self.records.append({"event": event, **fields})
        return True


def _turn(turn_id, provider):
    return coop_workers.WorkerTurn(
        turn_id=turn_id,
        provider=provider,
        agent_id=provider,
        prompt=f"act for {turn_id}",
        session_id=f"coop-auto-{provider}-test",
        board_path="board.db",
        cwd=".",
        action={"kind": "review_task", "item_id": 25},
    )


def _fake_claude_stream_argv(
    marker: pathlib.Path,
    *,
    crash_once=None,
    crash_always=False,
    rotate=None,
):
    code = r'''
import json
import pathlib
import sys

marker = pathlib.Path(sys.argv[1])
crash_once = pathlib.Path(sys.argv[2]) if sys.argv[2] != "-" else None
crash_always = sys.argv[3] == "1"
rotate = sys.argv[4]
marker.parent.mkdir(parents=True, exist_ok=True)
with marker.open("a", encoding="utf-8") as stream:
    stream.write("process-start\n")

def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()

initialized = False
answered = 0
for line in sys.stdin:
    message = json.loads(line)
    answered += 1
    with marker.open("a", encoding="utf-8") as stream:
        stream.write("prompt:" + message["message"]["content"] + "\n")
        stream.write("session:" + str(message["session_id"]) + "\n")
    if crash_always or (
        crash_once is not None and not crash_once.exists()
    ):
        crash_once.write_text("crashed", encoding="utf-8")
        sys.exit(17)
    session_id = message["session_id"]
    if rotate == "fixed":
        session_id = "claude-rotated"
    elif rotate == "each":
        session_id = "claude-rotated-" + str(answered)
    result_session_id = session_id
    if rotate == "split":
        session_id = "claude-rotated-init"
        result_session_id = "claude-rotated-result"
    if not initialized:
        send({
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
        })
        initialized = True
    send({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "discard me"}],
        },
        "session_id": session_id,
    })
    send({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "discard me too",
        "session_id": result_session_id,
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 6,
            "output_tokens": 2,
        },
        "num_turns": 1,
    })
'''
    return [
        sys.executable,
        "-u",
        "-c",
        code,
        str(marker),
        str(crash_once) if crash_once is not None else "-",
        "1" if crash_always else "0",
        rotate or "-",
    ]


def _fake_grok_acp_argv(
    marker: pathlib.Path,
    *,
    crash_once=None,
    crash_always=False,
    load_session=True,
    load_error=False,
):
    code = r'''
import json
import pathlib
import sys

marker = pathlib.Path(sys.argv[1])
crash_once = pathlib.Path(sys.argv[2]) if sys.argv[2] != "-" else None
crash_always = sys.argv[3] == "1"
load_session = sys.argv[4] == "1"
load_error = sys.argv[5] == "1"
marker.parent.mkdir(parents=True, exist_ok=True)
with marker.open("a", encoding="utf-8") as stream:
    stream.write("process-start\n")

def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    with marker.open("a", encoding="utf-8") as stream:
        stream.write(str(method) + "\n")
    request_id = request.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": 1,
            "agentCapabilities": (
                {"loadSession": True} if load_session else {}
            ),
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "sessionId": "grok-session-1",
        }})
    elif method == "session/load":
        if load_error:
            send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32601,
                "message": "session/load is not supported",
            }})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/prompt":
        if crash_always or (
            crash_once is not None and not crash_once.exists()
        ):
            crash_once.write_text("crashed", encoding="utf-8")
            sys.exit(19)
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": request["params"]["sessionId"],
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "discard me"},
                },
            },
        })
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "stopReason": "end_turn",
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }})
    elif method == "session/cancel":
        continue
'''
    return [
        sys.executable,
        "-u",
        "-c",
        code,
        str(marker),
        str(crash_once) if crash_once is not None else "-",
        "1" if crash_always else "0",
        "1" if load_session else "0",
        "1" if load_error else "0",
    ]


def test_claude_stream_worker_reuses_one_process_for_two_turns(tmp_path):
    marker = tmp_path / "claude.log"
    trace = _RecordingTrace()
    argv = _fake_claude_stream_argv(marker)
    worker = coop_resident_workers.ClaudeStreamWorker(
        argv_factory=lambda _session_id, _resume: argv,
        cwd=tmp_path,
        env=dict(os.environ),
        provider_session_id_factory=lambda: "claude-session-1",
        handshake_timeout_s=5,
    )

    worker.start({"trace_turn": _turn("turn-1", "claude")}, trace=trace)
    first = worker.submit(_turn("turn-1", "claude"), timeout_s=5)
    second = worker.submit(_turn("turn-2", "claude"), timeout_s=5)
    health = worker.health()
    worker.stop()

    assert first.ok is True
    assert second.ok is True
    assert first.note == "persistent_turn_completed"
    assert health == coop_workers.WorkerHealth(
        provider="claude",
        mode="persistent",
        state="ready",
        process_starts=1,
        turns_submitted=2,
    )
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines.count("process-start") == 1
    assert lines.count("prompt:act for turn-1") == 1
    assert lines.count("prompt:act for turn-2") == 1
    assert worker.validated_provider_session_id == "claude-session-1"
    assert dict(first.extra)["usage_observation"] == "partial"
    assert "discard me" not in repr(trace.records)
    assert any(
        row["event"] == "worker_handshake_completed"
        for row in trace.records
    )


def test_grok_acp_worker_reuses_one_process_and_session_for_two_turns(
    tmp_path,
):
    marker = tmp_path / "grok.log"
    trace = _RecordingTrace()
    worker = coop_resident_workers.GrokAcpWorker(
        argv=_fake_grok_acp_argv(marker),
        cwd=tmp_path,
        env=dict(os.environ),
        handshake_timeout_s=5,
    )

    worker.start({"trace_turn": _turn("turn-1", "grok")}, trace=trace)
    first = worker.submit(_turn("turn-1", "grok"), timeout_s=5)
    second = worker.submit(_turn("turn-2", "grok"), timeout_s=5)
    health = worker.health()
    worker.stop()

    assert first.ok is True
    assert second.ok is True
    assert health == coop_workers.WorkerHealth(
        provider="grok",
        mode="persistent",
        state="ready",
        process_starts=1,
        turns_submitted=2,
    )
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines.count("process-start") == 1
    assert lines.count("initialize") == 1
    assert lines.count("session/new") == 1
    assert lines.count("session/prompt") == 2
    assert lines.count("session/load") == 0
    assert dict(first.extra)["usage_observation"] == "partial"
    assert "discard me" not in repr(trace.records)


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_resident_worker_restarts_once_without_replaying_failed_turn(
    tmp_path,
    provider,
):
    marker = tmp_path / f"{provider}.log"
    crash_once = tmp_path / f"{provider}.crashed"
    if provider == "claude":
        argv = _fake_claude_stream_argv(marker, crash_once=crash_once)
        worker = coop_resident_workers.ClaudeStreamWorker(
            argv_factory=lambda _session_id, _resume: argv,
            cwd=tmp_path,
            env=dict(os.environ),
            provider_session_id_factory=lambda: "claude-session-1",
            handshake_timeout_s=5,
        )
    else:
        worker = coop_resident_workers.GrokAcpWorker(
            argv=_fake_grok_acp_argv(marker, crash_once=crash_once),
            cwd=tmp_path,
            env=dict(os.environ),
            handshake_timeout_s=5,
        )

    worker.start({})
    failed = worker.submit(_turn("turn-1", provider), timeout_s=5)
    recovered = worker.submit(_turn("turn-2", provider), timeout_s=5)
    health = worker.health()
    worker.stop()

    assert failed.ok is False
    assert failed.classification == "worker_protocol_failed"
    assert failed.retryable is True
    assert recovered.ok is True
    assert health.process_starts == 2
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines.count("process-start") == 2
    if provider == "grok":
        assert lines.count("session/new") == 1
        assert lines.count("session/load") == 1
    else:
        # The failed prompt is written exactly once; the worker never replays it.
        assert lines.count("prompt:act for turn-1") == 1
        assert lines.count("prompt:act for turn-2") == 1


def _claude_worker(tmp_path, argv, **kwargs):
    return coop_resident_workers.ClaudeStreamWorker(
        argv_factory=lambda _session_id, _resume: argv,
        cwd=tmp_path,
        env=dict(os.environ),
        handshake_timeout_s=5,
        **kwargs,
    )


def _submitted_sessions(marker: pathlib.Path):
    return [
        line.split(":", 1)[1]
        for line in marker.read_text(encoding="utf-8").splitlines()
        if line.startswith("session:")
    ]


def test_resumed_claude_worker_adopts_the_session_id_the_cli_reports(tmp_path):
    marker = tmp_path / "claude.log"
    worker = _claude_worker(
        tmp_path,
        _fake_claude_stream_argv(marker, rotate="fixed"),
        provider_session_id_factory=lambda: "claude-warm-1",
        resume_initial=True,
    )

    worker.start({})
    first = worker.submit(_turn("turn-1", "claude"), timeout_s=5)
    second = worker.submit(_turn("turn-2", "claude"), timeout_s=5)
    health = worker.health()
    worker.stop()

    # A resumed launch passes --resume only, so the CLI may answer with its
    # own identity. Adopting it keeps the warm run alive.
    assert first.ok is True
    assert second.ok is True
    assert health.process_starts == 1
    assert worker.validated_provider_session_id == "claude-rotated"
    assert _submitted_sessions(marker) == [
        "claude-warm-1",
        "claude-rotated",
    ]


def test_claude_worker_fails_a_session_id_change_after_confirmation(tmp_path):
    marker = tmp_path / "claude.log"
    worker = _claude_worker(
        tmp_path,
        _fake_claude_stream_argv(marker, rotate="each"),
        provider_session_id_factory=lambda: "claude-warm-1",
        resume_initial=True,
    )

    worker.start({})
    first = worker.submit(_turn("turn-1", "claude"), timeout_s=5)
    second = worker.submit(_turn("turn-2", "claude"), timeout_s=5)
    health = worker.health()
    worker.stop()

    assert first.ok is True
    assert worker.provider_session_id != "claude-warm-1"
    assert second.ok is False
    assert second.classification == "worker_protocol_failed"
    assert health.process_starts == 2


def test_claude_worker_adopts_a_resumed_session_id_only_once(tmp_path):
    marker = tmp_path / "claude.log"
    worker = _claude_worker(
        tmp_path,
        _fake_claude_stream_argv(marker, rotate="split"),
        provider_session_id_factory=lambda: "claude-warm-1",
        resume_initial=True,
    )

    worker.start({})
    # The handshake adopts one identity; a second identity in the same turn
    # is a genuine mismatch, not another adoption.
    result = worker.submit(_turn("turn-1", "claude"), timeout_s=5)
    worker.stop()

    assert result.ok is False
    assert result.classification == "worker_protocol_failed"


def test_claude_worker_ignores_an_interrupt_with_no_matching_turn(tmp_path):
    marker = tmp_path / "claude.log"
    worker = _claude_worker(
        tmp_path,
        _fake_claude_stream_argv(marker),
        provider_session_id_factory=lambda: "claude-session-1",
    )

    worker.start({})
    worker.interrupt("turn-1")
    worker.interrupt("turn-does-not-exist")
    result = worker.submit(_turn("turn-1", "claude"), timeout_s=5)
    health = worker.health()
    worker.stop()

    # Closing a healthy transport would spend the one-shot restart budget on
    # a turn that was never in flight.
    assert result.ok is True
    assert health.process_starts == 1


class _ShutdownRacingStreamClient:
    """Transport whose send races a stop(), as a real closed stream would."""

    def __init__(self, worker, *_args, **_kwargs):
        self._worker = worker

    def start(self):
        return None

    def send(self, _payload, *, on_submitted=None):
        with self._worker._state_lock:
            self._worker._state = "stopping"
        raise coop_jsonrpc.JsonRpcProtocolError("json_stream_closed")

    def close(self):
        return None


def test_resident_worker_never_relaunches_after_shutdown_begins(tmp_path):
    marker = tmp_path / "claude.log"
    worker = None

    def stream_client_factory(*args, **kwargs):
        return _ShutdownRacingStreamClient(worker, *args, **kwargs)

    worker = _claude_worker(
        tmp_path,
        _fake_claude_stream_argv(marker),
        provider_session_id_factory=lambda: "claude-session-1",
        stream_client_factory=stream_client_factory,
    )

    worker.start({})
    result = worker.submit(_turn("turn-1", "claude"), timeout_s=5)
    health = worker.health()
    worker.stop()

    assert result.ok is False
    assert result.retryable is False
    assert result.classification == "worker_protocol_failed"
    # _start_tree owns this counter, so it proves no replacement was launched
    # without racing the fake CLI's own start-up write.
    assert health.process_starts == 1
    assert health.state == "stopping"


def test_grok_recovery_uses_a_new_session_without_advertised_load_support(
    tmp_path,
):
    marker = tmp_path / "grok.log"
    crash_once = tmp_path / "grok.crashed"
    worker = coop_resident_workers.GrokAcpWorker(
        argv=_fake_grok_acp_argv(
            marker,
            crash_once=crash_once,
            load_session=False,
        ),
        cwd=tmp_path,
        env=dict(os.environ),
        handshake_timeout_s=5,
    )

    worker.start({})
    failed = worker.submit(_turn("turn-1", "grok"), timeout_s=5)
    recovered = worker.submit(_turn("turn-2", "grok"), timeout_s=5)
    health = worker.health()
    worker.stop()

    assert failed.ok is False
    assert recovered.ok is True
    assert health.state == "ready"
    assert health.process_starts == 2
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines.count("session/load") == 0
    assert lines.count("session/new") == 2


def test_grok_recovery_falls_back_when_session_load_is_refused(tmp_path):
    marker = tmp_path / "grok.log"
    crash_once = tmp_path / "grok.crashed"
    worker = coop_resident_workers.GrokAcpWorker(
        argv=_fake_grok_acp_argv(
            marker,
            crash_once=crash_once,
            load_error=True,
        ),
        cwd=tmp_path,
        env=dict(os.environ),
        handshake_timeout_s=5,
    )

    worker.start({})
    failed = worker.submit(_turn("turn-1", "grok"), timeout_s=5)
    recovered = worker.submit(_turn("turn-2", "grok"), timeout_s=5)
    health = worker.health()
    worker.stop()

    assert failed.ok is False
    assert recovered.ok is True
    assert health.state == "ready"
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines.count("session/load") == 1
    assert lines.count("session/new") == 2


def test_claude_stream_argv_has_bidirectional_json_and_explicit_session():
    argv = coop_resident_workers.claude_stream_argv(
        [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
        ],
        provider_session_id="session-1",
        resume=False,
    )

    assert "--no-session-persistence" not in argv
    assert argv[argv.index("--session-id") + 1] == "session-1"
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv.count("-p") == 1


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_second_protocol_failure_exhausts_restart_budget(tmp_path, provider):
    marker = tmp_path / f"{provider}.log"
    if provider == "claude":
        argv = _fake_claude_stream_argv(marker, crash_always=True)
        worker = coop_resident_workers.ClaudeStreamWorker(
            argv_factory=lambda _session_id, _resume: argv,
            cwd=tmp_path,
            env=dict(os.environ),
            provider_session_id_factory=lambda: "claude-session-1",
            handshake_timeout_s=5,
        )
    else:
        worker = coop_resident_workers.GrokAcpWorker(
            argv=_fake_grok_acp_argv(marker, crash_always=True),
            cwd=tmp_path,
            env=dict(os.environ),
            handshake_timeout_s=5,
        )

    worker.start({})
    first = worker.submit(_turn("turn-1", provider), timeout_s=5)
    second = worker.submit(_turn("turn-2", provider), timeout_s=5)

    assert first.retryable is True
    assert second.retryable is False
    assert second.classification == "worker_protocol_failed"
    assert worker.health() == coop_workers.WorkerHealth(
        provider=provider,
        mode="persistent",
        state="unhealthy",
        process_starts=2,
        turns_submitted=2,
    )
    with pytest.raises(coop_workers.WorkerUnavailable):
        worker.submit(_turn("turn-3", provider), timeout_s=5)
    worker.stop()
