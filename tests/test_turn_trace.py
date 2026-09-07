"""Filesystem-only turn trace contracts."""

import json
import threading

from agent_coop.coop_turn_trace import (
    DETAIL_FIELDS,
    TRACE_EVENTS,
    TRACE_VERSION,
    TurnTrace,
    default_trace_path,
    read_events,
    trace_path_for_log,
)


EXPECTED_EVENTS = frozenset({
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
    # Accepted only so historical trace producers remain readable.
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

EXPECTED_DETAIL_FIELDS = frozenset({
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


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


def test_trace_is_monotonic_and_drops_unknown_detail_fields(tmp_path):
    clock = Clock()
    path = tmp_path / "run.trace.jsonl"
    trace = TurnTrace(
        path,
        run_id="run-1",
        item_id=25,
        monotonic=clock,
        wall_clock=lambda: "2026-07-23T20:00:00+00:00",
    )

    assert trace.emit(
        "action_eligible",
        turn_id="turn-1",
        agent="codex",
        provider="codex",
        action="review_task",
        capability_set="deep_review",
        details={
            "worker_mode": "cold",
            "workflow_recipe": "standard_three",
            "secret": "must-drop",
        },
    )
    clock.value = 10.125
    assert trace.emit("worker_start_requested", turn_id="turn-1")

    events = read_events(path)
    assert [event["event"] for event in events] == [
        "action_eligible",
        "worker_start_requested",
    ]
    assert [event["elapsed_ms"] for event in events] == [0, 125]
    assert events[0] == {
        "version": TRACE_VERSION,
        "run_id": "run-1",
        "event": "action_eligible",
        "at": "2026-07-23T20:00:00+00:00",
        "elapsed_ms": 0,
        "item_id": 25,
        "turn_id": "turn-1",
        "agent": "codex",
        "provider": "codex",
        "action": "review_task",
        "capability_set": "deep_review",
        "details": {
            "worker_mode": "cold",
            "workflow_recipe": "standard_three",
        },
    }
    assert events[1]["turn_id"] == "turn-1"
    assert "agent" not in events[1]
    assert "provider" not in events[1]
    assert "action" not in events[1]
    assert "capability_set" not in events[1]
    assert "details" not in events[1]


def test_elapsed_time_never_goes_negative(tmp_path):
    clock = Clock()
    trace = TurnTrace(
        tmp_path / "run.trace.jsonl",
        run_id="run-1",
        item_id=25,
        monotonic=clock,
        wall_clock=lambda: "now",
    )
    clock.value = 9.5

    assert trace.emit("run_started")
    assert read_events(trace.path)[0]["elapsed_ms"] == 0


def test_event_and_detail_schema_are_bounded(tmp_path):
    assert TRACE_VERSION == 2
    assert TRACE_EVENTS == EXPECTED_EVENTS
    assert DETAIL_FIELDS == EXPECTED_DETAIL_FIELDS
    path = tmp_path / "run.trace.jsonl"
    trace = TurnTrace(
        path,
        run_id="run-1",
        item_id=25,
        monotonic=lambda: 1.0,
        wall_clock=lambda: "now",
    )
    details = {
        "worker_mode": "warm",
        "external_mcp": "tavily",
        "exit_code": 0,
        "timed_out": False,
        "classification": "board_progress",
        "board_mutations": 2,
        "retry": True,
        "error_class": "none",
        "workflow_recipe": "quick_two",
        "process_starts": 1,
        "worker_reuse_count": 2,
        "capability_activation_mode": "turn_lazy",
        "persistent_providers": ["codex"],
        "prompt_prefix_version": "coop-bootstrap-v1",
        "prompt_prefix_sha256": "a" * 64,
        "prompt_prefix_bytes": 432,
        "prompt_cache_mode": "provider_hint",
        "model_id": "gpt-5.4",
        "input_tokens": 1200,
        "uncached_input_tokens": 236,
        "cached_input_tokens": 900,
        "cache_write_input_tokens": 64,
        "output_tokens": 33,
        "reasoning_output_tokens": 7,
        "total_tokens": 1233,
        "model_calls": 2,
        "usage_observation": "complete",
        "workspace_surface": "none",
        "execution_mode": "isolated_structured_answer",
        "action_fingerprint": "a" * 64,
        "actor_last_mutation_to_exit_ms": 12,
        "satisfied_to_exit_ms": 99,
        "task_text": "must-drop",
        "provider_output": "must-drop",
    }

    assert trace.emit("run_finished", details=details)
    assert not trace.emit("task_content", details={"worker_mode": "cold"})

    events = read_events(path)
    assert len(events) == 1
    assert events[0]["details"] == {
        key: details[key]
        for key in (
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
            "workspace_surface",
            "execution_mode",
            "action_fingerprint",
            "actor_last_mutation_to_exit_ms",
        )
    }
    assert "satisfied_to_exit_ms" not in events[0]["details"]


def test_admission_skip_comparison_round_trips_and_drops_unknown_field(
        tmp_path):
    path = tmp_path / "run.trace.jsonl"
    trace = TurnTrace(
        path,
        run_id="run-1",
        item_id=25,
        monotonic=lambda: 1.0,
        wall_clock=lambda: "now",
    )
    details = {
        "dispatch_lane": "review:9",
        "workspace_surface": "read",
        "execution_mode": "tool_turn",
        "action_fingerprint": "a" * 64,
        "admission_reasons": [
            "candidate_read_blocked_by_workspace_writer",
        ],
        "in_flight_profiles": [{
            "agent": "claude",
            "lane": None,
            "workspace_surface": "write",
            "execution_mode": "tool_turn",
            "action_fingerprint": "b" * 64,
            "conflicts": [
                "candidate_read_blocked_by_workspace_writer",
            ],
        }],
        "prompt": "must-drop",
    }

    assert trace.emit(
        "dispatch_admission_skipped",
        agent="codex",
        provider="codex",
        action="review_task",
        details=details,
    )

    event = read_events(path)[0]
    assert event["version"] == 2
    assert event["details"] == {
        key: value
        for key, value in details.items()
        if key != "prompt"
    }


def test_trace_paths_are_sibling_and_board_scoped(tmp_path):
    assert trace_path_for_log("run.log") == "run.trace.jsonl"
    log_path = tmp_path / ".coop-runs" / "run-123.log"
    assert trace_path_for_log(log_path) == str(
        log_path.with_name("run-123.trace.jsonl")
    )

    board_path = tmp_path / "nested" / "board.db"
    path = default_trace_path(
        board_path,
        stamp="20260723-200000",
        nonce="abc123",
    )
    trace_path = type(board_path)(path)
    assert trace_path.parent == board_path.parent / ".coop-runs"
    assert trace_path.name.endswith(".trace.jsonl")
    assert "20260723-200000" in trace_path.name
    assert "abc123" in trace_path.name


def test_reader_skips_blank_malformed_and_non_object_lines(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    first = {"event": "run_started", "elapsed_ms": 0}
    second = {"event": "run_finished", "elapsed_ms": 10}
    path.write_text(
        "\n".join([
            json.dumps(first),
            "",
            "{not-json",
            json.dumps(["not", "an", "object"]),
            json.dumps(second),
        ]),
        encoding="utf-8",
    )

    assert read_events(path) == [first, second]
    assert read_events(tmp_path / "missing.trace.jsonl") == []
    assert read_events(tmp_path) == []


def test_reader_preserves_legacy_satisfied_tail_field(tmp_path):
    path = tmp_path / "legacy.trace.jsonl"
    legacy = {
        "version": 2,
        "event": "provider_result_received",
        "details": {"satisfied_to_exit_ms": 47},
    }
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    assert read_events(path) == [legacy]


def test_writer_is_fail_soft_for_io_and_serialization_errors(tmp_path):
    io_trace = TurnTrace(
        tmp_path,
        run_id="run-io",
        item_id=25,
        monotonic=lambda: 1.0,
        wall_clock=lambda: "now",
    )
    assert not io_trace.emit("run_started")

    type_path = tmp_path / "type.trace.jsonl"
    type_trace = TurnTrace(
        type_path,
        run_id="run-type",
        item_id=25,
        monotonic=lambda: 1.0,
        wall_clock=lambda: "now",
    )
    assert not type_trace.emit("run_finished", details={"retry": object()})
    assert read_events(type_path) == []

    def bad_clock():
        raise ValueError("clock unavailable")

    value_trace = TurnTrace(
        tmp_path / "value.trace.jsonl",
        run_id="run-value",
        item_id=25,
        monotonic=lambda: 1.0,
        wall_clock=bad_clock,
    )
    assert not value_trace.emit("run_started")


def test_concurrent_emits_remain_complete_json_lines(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    trace = TurnTrace(
        path,
        run_id="run-threaded",
        item_id=25,
        monotonic=lambda: 1.0,
        wall_clock=lambda: "now",
    )

    def emit_many(agent):
        for index in range(20):
            assert trace.emit(
                "provider_result_received",
                turn_id=f"{agent}-{index}",
                agent=agent,
            )

    threads = [
        threading.Thread(target=emit_many, args=(agent,))
        for agent in ("claude", "codex", "grok", "codex-2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = read_events(path)
    assert len(events) == 80
    assert {event["agent"] for event in events} == {
        "claude",
        "codex",
        "grok",
        "codex-2",
    }
