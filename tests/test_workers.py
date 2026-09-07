"""Provider-independent persistent worker contracts."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import threading

import pytest

from agent_coop import coop_workers


def _turn(provider="codex", turn_id="turn-1"):
    return coop_workers.WorkerTurn(
        turn_id=turn_id,
        provider=provider,
        agent_id=provider,
        prompt="follow the board",
        session_id=f"session-{provider}",
        board_path="board.db",
        cwd=".",
        action={"kind": "review_task", "item_id": 25},
        invoke_kwargs={"workflow_recipe": "standard_three"},
    )


def test_worker_turn_and_health_are_immutable_and_cold_kwargs_are_copies():
    turn = _turn()
    health = coop_workers.WorkerHealth(
        provider="codex",
        mode="cold",
        state="ready",
        process_starts=0,
        turns_submitted=0,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        turn.provider = "grok"
    with pytest.raises(TypeError):
        turn.action["kind"] = "claim_task"
    with pytest.raises(dataclasses.FrozenInstanceError):
        health.state = "stopped"

    kwargs = turn.cold_kwargs(timeout_s=17)
    assert kwargs == {
        "provider": "codex",
        "prompt": "follow the board",
        "session_id": "session-codex",
        "agent_id": "codex",
        "board_path": "board.db",
        "cwd": ".",
        "timeout_s": 17.0,
        "action": {"kind": "review_task", "item_id": 25},
        "turn_id": "turn-1",
        "workflow_recipe": "standard_three",
    }
    kwargs["action"]["kind"] = "mutated-copy"
    assert turn.action["kind"] == "review_task"


def test_turn_result_preserves_bounded_existing_result_fields():
    result = coop_workers.TurnResult.from_mapping(
        {
            "agent": "codex",
            "provider": "codex",
            "ok": False,
            "exit": 1,
            "note": "provider_quota_exhausted",
            "tree_empty": True,
            "classification": "provider_quota_exhausted",
            "retryable": False,
            "process_started": True,
            "session_created": False,
            "legal_next_action": "restore provider allowance",
        },
        default_agent="ignored",
        default_provider="ignored",
    )

    assert result.as_dict() == {
        "agent": "codex",
        "provider": "codex",
        "ok": False,
        "exit": 1,
        "note": "provider_quota_exhausted",
        "tree_empty": True,
        "classification": "provider_quota_exhausted",
        "retryable": False,
        "process_started": True,
        "session_created": False,
        "legal_next_action": "restore provider allowance",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = True


def test_run_cleanup_registry_retries_failures_in_reverse_ownership_order():
    registry = coop_workers.RunCleanupRegistry()
    events = []
    attempts = {"profile": 0}

    def cleanup_tree():
        events.append("tree")

    def cleanup_profile():
        attempts["profile"] += 1
        events.append(f"profile:{attempts['profile']}")
        if attempts["profile"] == 1:
            raise OSError("private profile path")

    registry.retain("tree:turn-1", cleanup_tree)
    registry.retain("profile:turn-1", cleanup_profile)

    with pytest.raises(coop_workers.WorkerCleanupError) as first:
        registry.drain()

    assert "private profile path" not in str(first.value)
    assert events == ["profile:1", "tree"]
    assert registry.pending == 1

    registry.drain()
    assert events == ["profile:1", "tree", "profile:2"]
    assert registry.pending == 0
    registry.drain()


def test_cold_worker_preserves_invocation_contract_and_counts_processes():
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        return {
            "agent": kwargs["agent_id"],
            "provider": kwargs["provider"],
            "ok": True,
            "exit": 0,
            "note": "done",
            "tree_empty": True,
            "process_started": True,
        }

    worker = coop_workers.ColdCliWorker("codex", invoke=invoke)
    assert worker.health().state == "new"
    started = worker.start({"name": "core"})
    result = worker.submit(_turn(), timeout_s=9)

    assert started.state == "ready"
    assert result.as_dict()["ok"] is True
    assert calls[0]["timeout_s"] == 9.0
    assert worker.health() == coop_workers.WorkerHealth(
        provider="codex",
        mode="cold",
        state="ready",
        process_starts=1,
        turns_submitted=1,
    )
    worker.stop()
    worker.stop()
    assert worker.health().state == "stopped"
    with pytest.raises(coop_workers.WorkerStopped):
        worker.submit(_turn(turn_id="turn-2"), timeout_s=9)


def test_one_provider_rejects_overlapping_submit_while_other_providers_overlap():
    entered = {"codex": threading.Event(), "grok": threading.Event()}
    release = threading.Event()

    def invoke(**kwargs):
        provider = kwargs["provider"]
        entered[provider].set()
        assert release.wait(timeout=5)
        return {
            "agent": provider,
            "provider": provider,
            "ok": True,
            "exit": 0,
            "note": "done",
            "tree_empty": True,
            "process_started": True,
        }

    pool = coop_workers.WorkerPool({
        "codex": coop_workers.ColdCliWorker("codex", invoke=invoke),
        "grok": coop_workers.ColdCliWorker("grok", invoke=invoke),
    })
    pool.start_all(core_profiles={"codex": {}, "grok": {}})

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        codex_future = executor.submit(
            pool.submit,
            "codex",
            _turn("codex"),
            timeout_s=5,
        )
        grok_future = executor.submit(
            pool.submit,
            "grok",
            _turn("grok"),
            timeout_s=5,
        )
        assert entered["codex"].wait(timeout=2)
        assert entered["grok"].wait(timeout=2)
        with pytest.raises(coop_workers.WorkerBusy):
            pool.submit(
                "codex",
                _turn("codex", "turn-2"),
                timeout_s=5,
            )
        release.set()
        assert codex_future.result(timeout=2).ok is True
        assert grok_future.result(timeout=2).ok is True


class _RecordingWorker:
    def __init__(self, provider, events, *, state="new"):
        self.provider = provider
        self.mode = "persistent"
        self.events = events
        self.state = state

    def start(self, core_profile, *, trace=None):
        del core_profile, trace
        self.state = "ready"
        self.events.append(f"start:{self.provider}")
        return self.health()

    def submit(self, turn, *, timeout_s):
        del turn, timeout_s
        if self.state != "ready":
            raise coop_workers.WorkerUnavailable(self.provider)
        return coop_workers.TurnResult.from_mapping(
            {"ok": True},
            default_agent=self.provider,
            default_provider=self.provider,
        )

    def interrupt(self, turn_id):
        self.events.append(f"interrupt:{self.provider}:{turn_id}")

    def health(self):
        return coop_workers.WorkerHealth(
            provider=self.provider,
            mode=self.mode,
            state=self.state,
            process_starts=1 if self.state != "new" else 0,
            turns_submitted=0,
        )

    def stop(self):
        if self.state != "stopped":
            self.events.append(f"stop:{self.provider}")
            self.state = "stopped"


def test_pool_starts_in_order_stops_in_reverse_and_refuses_unhealthy_worker():
    events = []
    codex = _RecordingWorker("codex", events)
    grok = _RecordingWorker("grok", events)
    pool = coop_workers.WorkerPool({"codex": codex, "grok": grok})

    pool.start_all(core_profiles={"codex": {}, "grok": {}})
    grok.state = "unhealthy"
    with pytest.raises(coop_workers.WorkerUnavailable):
        pool.submit("grok", _turn("grok"), timeout_s=5)
    pool.stop_all()
    pool.stop_all()

    assert events == [
        "start:codex",
        "start:grok",
        "stop:grok",
        "stop:codex",
    ]


def test_pool_retains_failed_cleanup_ownership_for_retry():
    events = []

    class FlakyStopWorker(_RecordingWorker):
        def __init__(self, provider, events):
            super().__init__(provider, events)
            self.stop_attempts = 0

        def stop(self):
            self.stop_attempts += 1
            self.events.append(
                f"stop:{self.provider}:{self.stop_attempts}"
            )
            if self.stop_attempts == 1:
                raise RuntimeError("private cleanup detail")
            self.state = "stopped"

    codex = _RecordingWorker("codex", events)
    grok = FlakyStopWorker("grok", events)
    pool = coop_workers.WorkerPool({"codex": codex, "grok": grok})
    pool.start_all(core_profiles={"codex": {}, "grok": {}})

    with pytest.raises(coop_workers.WorkerCleanupError) as first:
        pool.stop_all()

    assert "private cleanup detail" not in str(first.value)
    assert codex.state == "stopped"
    assert grok.state != "stopped"

    pool.stop_all()
    pool.stop_all()
    assert grok.state == "stopped"
    assert events[-3:] == [
        "stop:grok:1",
        "stop:codex",
        "stop:grok:2",
    ]


def test_pool_can_start_only_the_first_actionable_provider():
    events = []
    codex = _RecordingWorker("codex", events)
    grok = _RecordingWorker("grok", events)
    pool = coop_workers.WorkerPool({"codex": codex, "grok": grok})

    first = pool.start("grok", core_profile={"name": "core"})
    repeated = pool.start("grok", core_profile={"name": "ignored"})

    assert first.state == "ready"
    assert repeated.state == "ready"
    assert events == ["start:grok"]
    assert codex.health().state == "new"
    pool.stop_all()
    assert events == ["start:grok", "stop:grok"]


def test_different_provider_handshakes_can_start_concurrently():
    entered = {
        "codex": threading.Event(),
        "grok": threading.Event(),
    }
    release = threading.Event()

    class BlockingStartWorker(_RecordingWorker):
        def start(self, core_profile, *, trace=None):
            del core_profile, trace
            entered[self.provider].set()
            assert release.wait(timeout=5)
            self.state = "ready"
            self.events.append(f"start:{self.provider}")
            return self.health()

    events = []
    pool = coop_workers.WorkerPool({
        provider: BlockingStartWorker(provider, events)
        for provider in ("codex", "grok")
    })

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                pool.start,
                provider,
                core_profile={},
            )
            for provider in ("codex", "grok")
        ]
        both_entered = (
            entered["codex"].wait(timeout=1)
            and entered["grok"].wait(timeout=1)
        )
        release.set()
        for future in futures:
            assert future.result(timeout=2).state == "ready"

    assert both_entered
    pool.stop_all()


class _RecordingTrace:
    def __init__(self):
        self.records = []

    def emit(self, event, **fields):
        self.records.append({"event": event, **fields})
        return True


def test_cold_worker_emits_bounded_turn_lifecycle_without_prompt_content():
    trace = _RecordingTrace()
    worker = coop_workers.ColdCliWorker(
        "codex",
        invoke=lambda **kwargs: {
            "agent": kwargs["agent_id"],
            "provider": kwargs["provider"],
            "ok": True,
            "exit": 0,
            "note": "provider output must not enter telemetry",
            "process_started": True,
        },
    )
    worker.start({}, trace=trace)

    worker.submit(_turn(), timeout_s=5)
    worker.submit(_turn(turn_id="turn-2"), timeout_s=5)
    worker.interrupt("turn-2")
    worker.stop()

    assert [row["event"] for row in trace.records] == [
        "worker_turn_submitted",
        "worker_turn_completed",
        "worker_turn_submitted",
        "worker_turn_completed",
        "worker_interrupt_requested",
    ]
    assert trace.records[0]["details"] == {
        "worker_mode": "cold",
        "process_starts": 0,
        "worker_reuse_count": 0,
    }
    assert trace.records[3]["details"] == {
        "worker_mode": "cold",
        "process_starts": 2,
        "worker_reuse_count": 0,
    }
    serialized = repr(trace.records)
    assert "follow the board" not in serialized
    assert "provider output" not in serialized
