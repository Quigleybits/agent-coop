"""Runner-only status sidecars and autonomous status emission."""

import contextlib
import io
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from agent_coop import coop_action_scheduler
from agent_coop import coop_autonomous
from agent_coop import coop_decisions
from agent_coop import coop_monitor
from agent_coop import coop_mesh
from agent_coop import coop_templates
from agent_coop import coop_prompt_cache
from agent_coop import coop_runner_status as runner_status
from agent_coop import coop_start
from agent_coop import coop_turn_trace
from agent_coop import coop_watcher
from agent_coop import coop_workers
from agent_coop import coopdb


class _RunnerHerdrAdapter:
    def __init__(self, *, teardown_failures=()):
        self.teardown_failures = list(teardown_failures)
        self.spawn_calls = []
        self.teardown_calls = []
        self.workspace_calls = []
        self.events = []

    def spawn_mirror(self, run_id, provider, **kwargs):
        pane_id = f"opaque-pane::{provider}"
        self.spawn_calls.append((run_id, provider, kwargs))
        self.events.append(("spawn", provider, pane_id))
        return pane_id

    def workspace_id(self, run_id):
        self.workspace_calls.append(run_id)
        return f"opaque-workspace::{run_id}"

    def teardown(self, pane_ids):
        pane_ids = tuple(pane_ids)
        self.teardown_calls.append(pane_ids)
        self.events.append(("teardown", pane_ids))
        if self.teardown_failures:
            raise self.teardown_failures.pop(0)


class RunnerStatusSidecar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "run.status.json"

    def test_atomic_round_trip_keeps_only_operational_fields(self):
        written = runner_status.write_status(
            self.path,
            phase="turn",
            started_at="2026-07-23T16:00:00+00:00",
            agent="codex",
            action="review_task",
            turns=2,
            now="2026-07-23T16:01:05+00:00",
        )

        self.assertTrue(written)
        self.assertEqual(
            runner_status.read_status(self.path),
            {
                "phase": "turn",
                "started_at": "2026-07-23T16:00:00+00:00",
                "updated_at": "2026-07-23T16:01:05+00:00",
                "agent": "codex",
                "action": "review_task",
                "turns": 2,
            },
        )
        self.assertEqual(
            list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_herdr_metadata_has_only_canonical_nonempty_shapes(self):
        started_at = "2026-07-23T16:00:00+00:00"
        self.assertTrue(runner_status.write_status(
            self.path,
            phase="starting",
            started_at=started_at,
        ))
        self.assertNotIn("herdr", runner_status.read_status(self.path))

        caller_only = {"caller_pane": "caller pane/opaque"}
        self.assertTrue(runner_status.write_status(
            self.path,
            phase="checking",
            started_at=started_at,
            herdr=caller_only,
        ))
        self.assertEqual(
            runner_status.read_status(self.path)["herdr"],
            caller_only,
        )

        mirror = {
            "workspace": "workspace opaque/1",
            "panes": {
                "claude": "pane opaque/2",
                "codex": "pane opaque/3",
            },
            "caller_pane": "caller pane/opaque",
        }
        self.assertTrue(runner_status.write_status(
            self.path,
            phase="turn",
            started_at=started_at,
            agent="codex",
            action="review_task",
            herdr=mirror,
        ))
        self.assertEqual(
            runner_status.read_status(self.path)["herdr"],
            mirror,
        )

    def test_malformed_herdr_metadata_does_not_replace_prior_sidecar(self):
        started_at = "2026-07-23T16:00:00+00:00"
        self.assertTrue(runner_status.write_status(
            self.path,
            phase="checking",
            started_at=started_at,
            herdr={"caller_pane": "caller pane/opaque"},
        ))
        before = self.path.read_bytes()
        malformed = [
            None,
            {},
            {"caller_pane": None},
            {"caller_pane": ""},
            {"caller_pane": " caller"},
            {"caller_pane": "caller "},
            {"caller_pane": "caller\ncontrol"},
            {"caller_pane": "x" * 1025},
            {"workspace": "workspace opaque/1"},
            {"panes": {"codex": "pane opaque/3"}},
            {"workspace": "workspace opaque/1", "panes": {}},
            {
                "workspace": "workspace opaque/1",
                "panes": {"codex": None},
            },
            {
                "workspace": "workspace opaque/1",
                "panes": {"codex": "pane opaque/3"},
                "unknown": "field",
            },
        ]
        for value in malformed:
            with self.subTest(value=value):
                self.assertFalse(runner_status.write_status(
                    self.path,
                    phase="failed",
                    started_at=started_at,
                    reason="error",
                    turns=0,
                    herdr=value,
                ))
                self.assertEqual(self.path.read_bytes(), before)

    def test_shared_herdr_helpers_freeze_only_valid_caller_pane(self):
        env = {
            "HERDR_PANE_ID": "caller pane/opaque",
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "must be ignored",
            "HERDR_TAB_ID": "must be ignored",
        }
        self.assertEqual(
            runner_status.caller_pane_from_env(env),
            "caller pane/opaque",
        )
        self.assertIsNone(runner_status.caller_pane_from_env({
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "ignored workspace",
            "HERDR_TAB_ID": "ignored tab",
        }))
        for invalid in ("", " pane", "pane ", "pane\x00id", "x" * 1025):
            with self.subTest(invalid=invalid):
                self.assertIsNone(runner_status.caller_pane_from_env({
                    "HERDR_PANE_ID": invalid,
                }))

        self.assertIsNone(runner_status.canonical_herdr_metadata())
        self.assertEqual(
            runner_status.canonical_herdr_metadata(
                caller_pane="caller pane/opaque",
            ),
            {"caller_pane": "caller pane/opaque"},
        )
        self.assertEqual(
            runner_status.canonical_herdr_metadata(
                workspace="workspace opaque/1",
                panes={"codex": "pane opaque/3"},
                caller_pane="caller pane/opaque",
            ),
            {
                "workspace": "workspace opaque/1",
                "panes": {"codex": "pane opaque/3"},
                "caller_pane": "caller pane/opaque",
            },
        )

    def test_read_remains_permissive_for_malformed_herdr_metadata(self):
        payload = {
            "phase": "checking",
            "started_at": "2026-07-23T16:00:00+00:00",
            "updated_at": "2026-07-23T16:00:01+00:00",
            "herdr": {"workspace": None, "panes": {}},
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertEqual(runner_status.read_status(self.path), payload)

    def test_terminal_detail_round_trip_is_bounded_and_runner_only(self):
        self.assertTrue(runner_status.write_status(
            self.path,
            phase="stalled",
            started_at="2026-07-23T16:00:00+00:00",
            reason="stalled",
            reason_code="actionable_no_board_progress",
            evidence={
                "noop_cycles": 3,
                "turns": 6,
                "actionable_agents": ["claude", "codex"],
                "action_kinds": ["huddle_post"],
            },
            turns=6,
            now="2026-07-23T16:01:05+00:00",
        ))
        status = runner_status.read_status(self.path)
        self.assertEqual(
            status["reason_code"], "actionable_no_board_progress")
        self.assertEqual(status["evidence"]["noop_cycles"], 3)
        rendered = runner_status.format_status(status)
        self.assertEqual(
            rendered,
            "runner: stalled · actionable_no_board_progress · 6 turns",
        )
        self.assertNotIn("claude", rendered)
        self.assertNotIn("huddle_post", rendered)

        before = self.path.read_bytes()
        self.assertFalse(runner_status.write_status(
            self.path,
            phase="stalled",
            started_at="2026-07-23T16:00:00+00:00",
            reason="stalled",
            reason_code="actionable_no_board_progress",
            evidence={"noop_cycles": {"nested": 3}},
            turns=6,
        ))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_and_malformed_sidecars_are_ignored(self):
        self.assertIsNone(runner_status.read_status(self.path))
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(runner_status.read_status(self.path))
        self.path.write_text(json.dumps(["not", "an", "object"]),
                             encoding="utf-8")
        self.assertIsNone(runner_status.read_status(self.path))

    def test_operational_format_ignores_task_and_chat_fields(self):
        line = runner_status.format_status(
            {
                "phase": "turn",
                "started_at": "2026-07-23T16:00:00+00:00",
                "agent": "codex",
                "action": "review_task",
                "task_text": "must never render",
                "message": "must never render",
                "huddle": "must never render",
                "herdr": {
                    "workspace": "must never render",
                    "panes": {"codex": "must never render"},
                    "caller_pane": "must never render",
                },
            },
            now="2026-07-23T16:01:05+00:00",
        )

        self.assertEqual(line, "runner: codex · review_task · 01:05")
        self.assertNotIn("must never render", line)

    def test_formats_non_turn_and_terminal_phases(self):
        cases = [
            ({"phase": "starting"}, "runner: starting"),
            (
                {
                    "phase": "checking",
                    "started_at": "2026-07-23T16:00:00+00:00",
                    "agent": "claude",
                },
                "runner: checking claude · 00:04",
            ),
            (
                {
                    "phase": "waiting",
                    "started_at": "2026-07-23T16:00:00+00:00",
                },
                "runner: waiting · 00:04",
            ),
            (
                {
                    "phase": "finished",
                    "reason": "all_done",
                    "turns": 7,
                },
                "runner: finished · all_done · 7 turns",
            ),
            (
                {"phase": "stalled", "reason": "stalled", "turns": 6},
                "runner: stalled · 6 turns",
            ),
            (
                {
                    "phase": "failed",
                    "reason": "insufficient_providers",
                    "turns": 0,
                },
                "runner: failed · insufficient_providers · 0 turns",
            ),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(
                    runner_status.format_status(
                        status, now="2026-07-23T16:00:04+00:00"),
                    expected,
                )

    def test_status_token_changes_with_published_content(self):
        self.assertIsNone(runner_status.status_token(self.path))
        runner_status.write_status(
            self.path, phase="starting",
            started_at="2026-07-23T16:00:00+00:00",
            now="2026-07-23T16:00:00+00:00")
        first = runner_status.status_token(self.path)
        runner_status.write_status(
            self.path, phase="turn",
            started_at="2026-07-23T16:00:00+00:00",
            agent="grok", action="claim_task",
            now="2026-07-23T16:00:01+00:00")
        self.assertNotEqual(runner_status.status_token(self.path), first)

    def test_status_path_for_run_artifact_maps_log_and_trace(self):
        root = pathlib.Path(self.tmp.name)
        self.assertEqual(
            runner_status.status_path_for_run_artifact(
                root / "run-1.log"),
            str(root / "run-1.status.json"),
        )
        self.assertEqual(
            runner_status.status_path_for_run_artifact(
                root / "run-2.trace.jsonl"),
            str(root / "run-2.status.json"),
        )
        self.assertEqual(
            runner_status.status_path_for_log(root / "run-1.log"),
            runner_status.status_path_for_run_artifact(root / "run-1.log"),
        )

    def test_latest_status_path_picks_newest_mtime(self):
        runs = pathlib.Path(self.tmp.name) / ".coop-runs"
        runs.mkdir()
        older = runs / "run-old.status.json"
        newer = runs / "run-new.status.json"
        runner_status.write_status(
            older, phase="finished",
            started_at="2026-07-23T16:00:00+00:00",
            reason="all_done", turns=20,
            now="2026-07-23T16:30:00+00:00")
        runner_status.write_status(
            newer, phase="turn",
            started_at="2026-07-23T17:00:00+00:00",
            agent="claude", action="claim_task",
            now="2026-07-23T17:00:05+00:00")
        # Force mtime order in case the writes land in the same second.
        os.utime(older, (1_000_000_000, 1_000_000_000))
        os.utime(newer, (1_700_000_000, 1_700_000_000))
        self.assertEqual(
            runner_status.latest_status_path(runs), str(newer))
        self.assertIsNone(
            runner_status.latest_status_path(runs / "missing"))


class SchedulerStatusEmission(unittest.TestCase):
    def test_scheduler_reports_checks_turns_and_waiting(self):
        statuses = []
        checks = {"count": 0}

        def actionable(agent):
            checks["count"] += 1
            return "claim_task" if checks["count"] == 1 else "idle"

        reason, turns, _log = coop_autonomous.run_autonomous(
            ["claude"],
            actionable_fn=actionable,
            take_turn=lambda agent, hint: {"ok": True},
            all_done=lambda: False,
            max_idle_rounds=1,
            progress_fn=lambda: checks["count"],
            status_fn=lambda phase, **fields: statuses.append(
                (phase, fields)),
            sleep=lambda _: None,
        )

        self.assertEqual((reason, turns), ("stalled", 1))
        self.assertEqual(
            statuses,
            [
                ("checking", {"agent": "claude"}),
                ("turn", {"agent": "claude", "action": "claim_task"}),
                ("checking", {"agent": "claude"}),
                ("waiting", {}),
                (
                    "terminal_detail",
                    {
                        "reason": "stalled",
                        "reason_code": "no_actionable_participant",
                        "evidence": {
                            "idle_rounds": 1,
                            "turns": 1,
                            "actionable_agents": [],
                            "action_kinds": ["idle"],
                        },
                    },
                ),
            ],
        )

    def test_idle_stall_emits_precise_terminal_detail(self):
        statuses = []
        reason, turns, _log = coop_autonomous.run_autonomous(
            ["claude", "codex"],
            actionable_fn=lambda _agent: "idle",
            take_turn=lambda _agent, _hint: {"ok": True},
            all_done=lambda: False,
            max_idle_rounds=2,
            status_fn=lambda phase, **fields: statuses.append(
                (phase, fields)),
            sleep=lambda _seconds: None,
        )
        self.assertEqual((reason, turns), ("stalled", 0))
        terminal = [
            fields for phase, fields in statuses
            if phase == "terminal_detail"
        ]
        self.assertEqual(
            terminal,
            [{
                "reason": "stalled",
                "reason_code": "no_actionable_participant",
                "evidence": {
                    "idle_rounds": 2,
                    "turns": 0,
                    "actionable_agents": [],
                    "action_kinds": ["idle"],
                },
            }],
        )

    def test_no_progress_stall_names_agents_and_actions(self):
        statuses = []
        reason, turns, _log = coop_autonomous.run_autonomous(
            ["claude"],
            actionable_fn=lambda _agent: "review_task",
            take_turn=lambda _agent, _hint: {"ok": False},
            all_done=lambda: False,
            progress_fn=lambda: "unchanged",
            max_noop_cycles=2,
            status_fn=lambda phase, **fields: statuses.append(
                (phase, fields)),
            sleep=lambda _seconds: None,
        )
        self.assertEqual((reason, turns), ("stalled", 2))
        terminal = [
            fields for phase, fields in statuses
            if phase == "terminal_detail"
        ][0]
        self.assertEqual(
            terminal["reason_code"], "actionable_no_board_progress")
        self.assertEqual(terminal["evidence"]["noop_cycles"], 2)
        self.assertEqual(
            terminal["evidence"]["actionable_agents"], ["claude"])
        self.assertEqual(
            terminal["evidence"]["action_kinds"], ["review_task"])

    def test_turn_budget_emits_detail_without_changing_outer_reason(self):
        statuses = []
        reason, turns, _log = coop_autonomous.run_autonomous(
            ["claude"],
            actionable_fn=lambda _agent: "claim_task",
            take_turn=lambda _agent, _hint: {"ok": True},
            all_done=lambda: False,
            max_turns=1,
            status_fn=lambda phase, **fields: statuses.append(
                (phase, fields)),
            sleep=lambda _seconds: None,
        )
        self.assertEqual((reason, turns), ("max_turns", 1))
        terminal = [
            fields for phase, fields in statuses
            if phase == "terminal_detail"
        ][0]
        self.assertEqual(
            terminal["reason_code"], "turn_budget_exhausted")
        self.assertEqual(
            terminal["evidence"], {"turns": 1, "max_turns": 1})

    def test_board_refresh_retry_does_not_consume_noop_budget(self):
        state = {"turns": 0, "progress": 0, "done": False}

        def take(_agent, _hint):
            state["turns"] += 1
            if state["turns"] == 1:
                return {
                    "ok": False,
                    "classification": "provider_session_unsupported",
                    "retry_after_board_refresh": True,
                }
            state["progress"] += 1
            state["done"] = True
            return {"ok": True}

        reason, turns, _ = coop_autonomous.run_autonomous(
            ["claude"],
            actionable_fn=lambda _agent: "claim_task",
            take_turn=take,
            all_done=lambda: state["done"],
            progress_fn=lambda: state["progress"],
            max_noop_cycles=1,
            sleep=lambda _seconds: None,
            max_turns=5,
        )

        self.assertEqual(reason, "all_done")
        self.assertEqual(turns, 2)


class DashboardRunnerNotice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "run.status.json"

    def test_live_sidecar_replaces_static_launch_notice(self):
        runner_status.write_status(
            self.path, phase="turn",
            started_at="2026-07-23T16:00:00+00:00",
            agent="grok", action="research_task",
            now="2026-07-23T16:00:03+00:00")
        state = coop_monitor.ViewState(
            notice="runner: starting",
            runner_status_path=str(self.path),
        )

        self.assertEqual(
            coop_monitor._runner_notice(
                state, now="2026-07-23T16:00:05+00:00"),
            "runner: grok · research_task · 00:05",
        )

    def test_missing_sidecar_falls_back_to_plain_notice(self):
        state = coop_monitor.ViewState(
            notice="created task #25 (draft)",
            runner_status_path=str(self.path),
        )
        self.assertEqual(
            coop_monitor._runner_notice(state),
            "created task #25 (draft)",
        )

    def test_follow_latest_replaces_stuck_finished_run(self):
        """After task 1 finishes, task 2's sidecar must own the notice."""
        board = pathlib.Path(self.tmp.name) / "board.db"
        board.write_bytes(b"")
        runs = runner_status.runs_dir_for_board(board)
        runs.mkdir()
        finished = runs / "run-task1.status.json"
        live = runs / "run-task2.status.json"
        runner_status.write_status(
            finished, phase="finished",
            started_at="2026-07-23T16:00:00+00:00",
            reason="all_done", turns=20,
            now="2026-07-23T16:30:00+00:00")
        runner_status.write_status(
            live, phase="turn",
            started_at="2026-07-23T17:00:00+00:00",
            agent="codex", action="review_task",
            now="2026-07-23T17:00:10+00:00")
        os.utime(finished, (1_000_000_000, 1_000_000_000))
        os.utime(live, (1_700_000_000, 1_700_000_000))

        state = coop_monitor.ViewState(
            notice="runner: starting",
            runner_status_path=str(finished),
        )
        changed = coop_monitor._follow_latest_runner_status(
            state, str(board))
        self.assertTrue(changed)
        self.assertEqual(state.runner_status_path, str(live))
        self.assertEqual(
            coop_monitor._runner_notice(
                state, now="2026-07-23T17:00:15+00:00"),
            "runner: codex · review_task · 00:15",
        )
        self.assertFalse(
            coop_monitor._follow_latest_runner_status(state, str(board)))


class DashboardHerdrJumpTarget(unittest.TestCase):
    """The newest sidecar's mirror pane IDs become a jump target."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "run.status.json"

    def _write(self, herdr=None, **overrides):
        fields = {
            "phase": "turn",
            "started_at": "2026-08-24T09:00:00+00:00",
            "agent": "codex",
            "action": "review_task",
            "now": "2026-08-24T09:00:04+00:00",
        }
        fields.update(overrides)
        if herdr is not None:
            fields["herdr"] = herdr
        self.assertTrue(runner_status.write_status(self.path, **fields))

    def _notice(self, now="2026-08-24T09:00:07+00:00"):
        state = coop_monitor.ViewState(
            notice="runner: starting",
            runner_status_path=str(self.path),
        )
        return coop_monitor._runner_notice(state, now=now)

    def test_mirror_panes_append_exact_ids_to_the_runner_line(self):
        self._write({
            "workspace": "ws-1",
            "panes": {"codex": "pane-c", "claude": "pane-a"},
        })

        self.assertEqual(
            self._notice(),
            "runner: codex · review_task · 00:07"
            " · herdr claude=pane-a codex=pane-c",
        )

    def test_jump_target_carries_ids_only_and_no_board_content(self):
        self._write({"workspace": "ws-1", "panes": {"grok": "pane-g"}})
        jump = runner_status.format_herdr_jump(
            runner_status.read_status(self.path))

        self.assertEqual(jump, "herdr grok=pane-g")
        for leaked in ("review_task", "codex", "runner", "phase", "turn"):
            self.assertNotIn(leaked, jump)

    def test_caller_pane_alone_is_not_a_jump_target(self):
        """The human already occupies the caller pane; nothing to jump to."""
        self._write({"caller_pane": "pane-human"})

        self.assertEqual(self._notice(), "runner: codex · review_task · 00:07")

    def test_no_herdr_metadata_leaves_the_runner_line_untouched(self):
        self._write()

        self.assertEqual(self._notice(), "runner: codex · review_task · 00:07")

    def test_terminal_phase_still_publishes_the_jump_target(self):
        """Scrollback outlives the run, so a finished run keeps its panes."""
        self._write(
            {"workspace": "ws-1", "panes": {"claude": "pane-a"}},
            phase="finished", reason="all_done", turns=3,
            agent=None, action=None)

        self.assertEqual(
            self._notice(),
            "runner: finished · all_done · 3 turns · herdr claude=pane-a",
        )

    def test_oversized_pane_list_falls_back_to_the_workspace(self):
        long_pane = "p" * 60
        self._write({
            "workspace": "ws-1",
            "panes": {"claude": long_pane, "codex": long_pane},
        })

        self.assertEqual(
            self._notice(),
            "runner: codex · review_task · 00:07 · herdr ws=ws-1",
        )

    def test_oversized_workspace_omits_the_segment(self):
        long_id = "w" * 200
        self._write({"workspace": long_id, "panes": {"claude": long_id}})

        self.assertEqual(self._notice(), "runner: codex · review_task · 00:07")

    def test_noncanonical_metadata_never_renders_a_partial_list(self):
        for herdr in (
            {"workspace": "ws-1", "panes": {"claude": "pane\nbreak"}},
            {"workspace": "ws-1", "panes": {}},
            {"workspace": "", "panes": {"claude": "pane-a"}},
            {"panes": {"claude": "pane-a"}},
            {"workspace": "ws-1"},
            {"workspace": "ws-1", "panes": "pane-a"},
            "herdr",
            None,
        ):
            with self.subTest(herdr=herdr):
                self.assertEqual(
                    runner_status.format_herdr_jump({"herdr": herdr}), "")

    def test_absent_or_malformed_status_renders_nothing(self):
        for status in ({}, {"phase": "turn"}, None, "status", []):
            with self.subTest(status=status):
                self.assertEqual(runner_status.format_herdr_jump(status), "")


class AutonomousRunnerSidecar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.board = str(root / "board.db")
        self.status_path = root / "run.status.json"
        self.trace_path = root / "run.trace.jsonl"
        conn = coopdb.connect(self.board)
        coopdb.init_db(conn)
        self.item = coopdb.create_item(
            conn, actor="human", session_id=None,
            title="target", objective="target")
        conn.close()
        runner_status.write_status(
            self.status_path, phase="starting",
            started_at="2026-07-23T16:00:00+00:00",
            now="2026-07-23T16:00:00+00:00")

    def _seed_open_question(self, assigned_to="claude"):
        conn = coopdb.connect(self.board, require_current=True)
        try:
            coopdb.register_agent(conn, "asker")
            coopdb.register_agent(conn, assigned_to)
            coopdb.insert_session(
                conn,
                session_id="asker-session",
                agent_id="asker",
                provider="codex",
                command=["codex"],
                cwd=".",
                max_runtime_s=3600,
                grace_s=10,
            )
            cur = conn.execute(
                "INSERT INTO questions(item_id,exact_question,"
                "asked_by_agent,asked_by_session,assigned_to_agent,status,"
                "asked_at) VALUES (?,?,?,?,?,'open',?)",
                (
                    self.item,
                    "What is the bounded answer?",
                    "asker",
                    "asker-session",
                    assigned_to,
                    coopdb.now(),
                ),
            )
            question_id = cur.lastrowid
            conn.execute(
                "UPDATE items SET status='needs_input', "
                "next_actor_agent_id=? WHERE id=?",
                (assigned_to, self.item),
            )
            conn.commit()
            return question_id
        finally:
            conn.close()

    def _held_answer_action(self, question_id, agent="claude"):
        del agent
        claim_id = 9000 + question_id
        return {
            "kind": "answer_question",
            "target_type": "question",
            "target_id": question_id,
            "item_id": self.item,
            "claim_id": claim_id,
            "lease_seconds": 3600,
            "command": [
                *coopdb.CLI_ARGV,
                "question",
                "answer",
                "--claim",
                str(claim_id),
                "--answer",
                "{answer}",
            ],
            "required_inputs": ["answer"],
            "choices": [],
        }

    def _seed_pending_handoff(self, assigned_to="claude"):
        conn = coopdb.connect(self.board, require_current=True)
        try:
            coopdb.register_agent(conn, "asker")
            coopdb.register_agent(conn, assigned_to)
            coopdb.insert_session(
                conn,
                session_id="asker-handoff-session",
                agent_id="asker",
                provider="codex",
                command=["codex"],
                cwd=".",
                max_runtime_s=3600,
                grace_s=10,
            )
            claim = coopdb.claim_item(
                conn,
                item_id=self.item,
                actor="asker",
                session_id="asker-handoff-session",
                intent="prepare bounded transfer",
                lease_seconds=3600,
            )
            event_id = conn.execute(
                "SELECT MAX(event_id) AS event_id FROM events WHERE item_id=?",
                (self.item,),
            ).fetchone()["event_id"]
            result = coopdb.create_handoff(
                conn,
                claim_id=claim["claim_id"],
                session_id="asker-handoff-session",
                actor="asker",
                to_agent=assigned_to,
                reason="bounded mesh rotation",
                summary="the prior directed pairs are complete",
                completed="asker pairs",
                remaining="target pairs",
                risks="none beyond the stop boundary",
                next_action="continue the bounded mesh",
                proof_refs=[f"event:{event_id}"],
            )
            return result["handoff_id"]
        finally:
            conn.close()

    def _use_mesh_v2_item(self):
        conn = coopdb.connect(self.board, require_current=True)
        try:
            rendered = coop_templates.render_contract_template(
                "mesh-v2",
                goal="six directed pings",
                stamp="20260801-120000",
                nonce="abc123",
            )
            self.item = coopdb.create_item(
                conn,
                actor="human",
                session_id=None,
                owner="claude",
                next_actor="claude",
                template_provenance=(
                    coop_templates.contract_template_provenance(
                        "mesh-v2", rendered
                    )
                ),
                **rendered,
            )
            return self.item
        finally:
            conn.close()

    def _seed_mesh_v2_composition(self):
        conn = coopdb.connect(self.board, require_current=True)
        try:
            sessions = {
                row["agent_id"]: row["session_id"]
                for row in conn.execute(
                    "SELECT agent_id, session_id FROM sessions WHERE "
                    "status='running'"
                )
            }
            current_claim = coopdb.claim_item(
                conn,
                item_id=self.item,
                actor="claude",
                session_id=sessions["claude"],
                intent="start compiled mesh",
                lease_seconds=3600,
            )["claim_id"]
            for index, sender in enumerate(coop_mesh.MESH_PARTICIPANTS):
                action = coopdb.status(
                    conn,
                    sender,
                    session_id=sessions[sender],
                    item_id=self.item,
                )["next_action"]
                outbound = coop_mesh.compile_mesh_phase(
                    conn,
                    item=coopdb.item_show(conn, self.item),
                    action=action,
                    agent=sender,
                )
                question_ids = coopdb.needs_input_batch(
                    conn,
                    claim_id=current_claim,
                    session_id=sessions[sender],
                    questions=tuple(
                        (
                            recipient,
                            f"Ping from {sender} to {recipient}.",
                        )
                        for recipient in outbound.recipients
                    ),
                )
                for recipient, question_id in zip(
                        outbound.recipients, question_ids):
                    response = coopdb.claim_questions_batch(
                        conn,
                        question_ids=[question_id],
                        session_id=sessions[recipient],
                        intent=f"answer {question_id}",
                    )[0]
                    coopdb.answer_questions_batch(
                        conn,
                        session_id=sessions[recipient],
                        answers=[(
                            response["claim_id"],
                            f"Acknowledged by {recipient}.",
                        )],
                    )
                current_claim = coopdb.claim_item(
                    conn,
                    item_id=self.item,
                    actor=sender,
                    session_id=sessions[sender],
                    intent=f"resume {sender}",
                    reclaim_reason="both answers arrived",
                    lease_seconds=3600,
                )["claim_id"]
                action = coopdb.status(
                    conn,
                    sender,
                    session_id=sessions[sender],
                    item_id=self.item,
                )["next_action"]
                phase = coop_mesh.compile_mesh_phase(
                    conn,
                    item=coopdb.item_show(conn, self.item),
                    action=action,
                    agent=sender,
                )
                if index < 2:
                    created = coopdb.create_handoff(
                        conn,
                        claim_id=phase.claim_id,
                        session_id=sessions[sender],
                        actor=sender,
                        to_agent=phase.to_agent,
                        reason=phase.reason,
                        summary=phase.summary,
                        completed=phase.completed,
                        remaining=phase.remaining,
                        risks=phase.risks,
                        next_action=phase.next_action,
                        proof_refs=phase.proof_refs,
                    )
                    current_claim = coopdb.accept_handoff(
                        conn,
                        handoff_id=created["handoff_id"],
                        session_id=sessions[phase.to_agent],
                        actor=phase.to_agent,
                        intent=f"accept {created['handoff_id']}",
                        lease_seconds=3600,
                    )["claim_id"]
            return phase
        finally:
            conn.close()

    def test_cli_start_without_env_still_publishes_status_sidecar(self):
        """Bare ``coop start`` must write a discoverable status sidecar."""
        def run(_participants, **kwargs):
            kwargs["status_fn"](
                "turn", agent="claude", action="claim_task")
            return "all_done", 1, []

        env = {
            key: value for key, value in os.environ.items()
            if key not in ("COOP_RUN_STATUS_PATH", "COOP_RUN_TRACE_PATH")
        }
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(
                 coop_start, "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start, "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_autonomous, "run_autonomous", side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path, "exists", return_value=False):
            code = coop_autonomous.main([
                "--db", self.board, "--item", str(self.item)])

        self.assertEqual(code, 0)
        runs_dir = runner_status.runs_dir_for_board(self.board)
        discovered = runner_status.latest_status_path(runs_dir)
        self.assertIsNotNone(discovered)
        status = runner_status.read_status(discovered)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("finished", "all_done", 1),
        )

    def test_main_publishes_turn_updates_and_terminal_reason(self):
        runs = []
        trace_events = []
        trace = mock.Mock()

        def emit(event, **fields):
            trace_events.append({"event": event, **fields})
            return True

        trace.emit.side_effect = emit

        def run(participants, **kwargs):
            runs.append(tuple(participants))
            kwargs["status_fn"](
                "turn", agent="codex", action="review_task")
            kwargs["scheduler_event_fn"](
                "dispatch_admission_skipped",
                agent="codex",
                action="review_task",
                details={
                    "dispatch_lane": "review:9",
                    "workspace_surface": "read",
                    "execution_mode": "tool_turn",
                    "action_fingerprint": "a" * 64,
                    "admission_reasons": [
                        "candidate_read_blocked_by_workspace_writer",
                    ],
                    "in_flight_profiles": [],
                },
            )
            return "all_done", 2, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace, "TurnTrace",
                 return_value=trace) as trace_type, \
             mock.patch.object(
                 coop_start, "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start, "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_autonomous, "run_autonomous", side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path, "exists", return_value=False):
            code = coop_autonomous.main([
                "--db", self.board, "--item", str(self.item)])

        self.assertEqual(code, 0)
        self.assertEqual(runs, [("claude", "codex", "grok")])
        trace_type.assert_called_once_with(
            str(self.trace_path), run_id="run", item_id=self.item)
        self.assertEqual(trace_events[0]["event"], "run_started")
        self.assertEqual(
            trace_events[0]["details"]["workflow_recipe"],
            "standard_three",
        )
        self.assertEqual(trace_events[-1]["event"], "run_finished")
        self.assertEqual(
            trace_events[-1]["details"]["classification"], "all_done")
        status = runner_status.read_status(self.status_path)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("finished", "all_done", 2),
        )
        skip = next(
            event for event in trace_events
            if event["event"] == "dispatch_admission_skipped"
        )
        self.assertEqual(skip["agent"], "codex")
        self.assertEqual(skip["provider"], "codex")
        self.assertEqual(skip["action"], "review_task")
        self.assertEqual(skip["details"]["dispatch_lane"], "review:9")
        self.assertNotIn("dispatch_lane", status)
        self.assertNotIn("admission_reasons", status)
        conn = coopdb.connect(self.board, require_current=True)
        try:
            row = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='autonomous_run_started' "
                "ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        recipe = json.loads(row["payload_json"])["workflow_recipe"]
        self.assertEqual(recipe["name"], "standard_three")
        self.assertEqual(
            recipe["active_participants"],
            ["claude", "codex", "grok"],
        )
        self.assertEqual(recipe["reserve_participants"], [])

    def test_terminal_detail_reaches_status_trace_and_finish_event(self):
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        detail = {
            "reason": "stalled",
            "reason_code": "actionable_no_board_progress",
            "evidence": {
                "noop_cycles": 3,
                "turns": 6,
                "actionable_agents": ["claude", "codex"],
                "action_kinds": ["huddle_post"],
            },
        }

        def run(_participants, **kwargs):
            kwargs["status_fn"]("terminal_detail", **detail)
            return "stalled", 6, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace, "TurnTrace", return_value=trace), \
             mock.patch.object(
                 coop_start, "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start, "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_autonomous, "run_autonomous", side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path, "exists", return_value=False):
            code = coop_autonomous.main([
                "--db", self.board, "--item", str(self.item)])

        self.assertEqual(code, 3)
        status = runner_status.read_status(self.status_path)
        self.assertEqual(status["phase"], "stalled")
        self.assertEqual(
            status["reason_code"], detail["reason_code"])
        self.assertEqual(status["evidence"], detail["evidence"])
        self.assertEqual(
            trace_events[-1]["details"]["reason_code"],
            detail["reason_code"],
        )
        self.assertEqual(
            trace_events[-1]["details"]["evidence"], detail["evidence"])

        conn = coopdb.connect(self.board, require_current=True)
        try:
            row = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='autonomous_run_finished' "
                "ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["reason_code"], detail["reason_code"])
        self.assertEqual(payload["evidence"], detail["evidence"])

    def test_quick_two_polls_two_and_registers_dormant_reserve(self):
        quick_tags = [
            "recipe:quick-two",
            "quick:bounded",
            "quick:low-risk",
            "quick:reversible",
            "quick:no-research",
            "quick:no-three-party",
            "quick:no-high-authority",
        ]
        conn = coopdb.connect(self.board, require_current=True)
        try:
            conn.execute(
                "UPDATE items SET allowed_actions=? WHERE id=?",
                (json.dumps(quick_tags), self.item),
            )
            conn.commit()
        finally:
            conn.close()
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        observed = {}

        def run(participants, **_kwargs):
            observed["participants"] = tuple(participants)
            return "stopped", 0, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
            ])

        self.assertEqual(code, 0)
        self.assertEqual(observed["participants"], ("claude", "codex"))
        conn = coopdb.connect(self.board, require_current=True)
        try:
            sessions = conn.execute(
                "SELECT agent_id FROM sessions ORDER BY agent_id"
            ).fetchall()
            row = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='autonomous_run_started' "
                "ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            [session["agent_id"] for session in sessions],
            ["claude", "codex", "grok"],
        )
        recipe = json.loads(row["payload_json"])["workflow_recipe"]
        self.assertEqual(recipe["name"], "quick_two")
        self.assertEqual(
            recipe["active_participants"],
            ["claude", "codex"],
        )
        self.assertEqual(recipe["reserve_participants"], ["grok"])
        self.assertEqual(
            trace_events[0]["details"]["workflow_recipe"],
            "quick_two",
        )

    def test_quick_two_promotes_once_from_board_uncertainty(self):
        quick_tags = [
            "recipe:quick-two",
            "quick:bounded",
            "quick:low-risk",
            "quick:reversible",
            "quick:no-research",
            "quick:no-three-party",
            "quick:no-high-authority",
        ]
        conn = coopdb.connect(self.board, require_current=True)
        try:
            conn.execute(
                "UPDATE items SET allowed_actions=? WHERE id=?",
                (json.dumps(quick_tags), self.item),
            )
            conn.commit()
        finally:
            conn.close()
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        observed = {}

        def run(participants, **kwargs):
            observed["before"] = tuple(participants)
            conn = coopdb.connect(self.board, require_current=True)
            try:
                session = conn.execute(
                    "SELECT session_id FROM sessions "
                    "WHERE agent_id='claude'"
                ).fetchone()["session_id"]

                def write_uncertainty(current):
                    return coopdb.append_event(
                        current,
                        item_id=self.item,
                        event_type="needs_input",
                        actor_agent_id="claude",
                        actor_session_id=session,
                        payload={
                            "item_id": self.item,
                            "question": "bounded uncertainty",
                        },
                    )

                coopdb.mutate(conn, write_uncertainty)
            finally:
                conn.close()
            observed["prepare_reason"] = kwargs["prepare_cycle"]()
            observed["after"] = tuple(participants)
            return "stopped", 0, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value={"kind": "idle"}), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
            ])

        self.assertEqual(code, 0)
        self.assertEqual(observed["before"], ("claude", "codex"))
        self.assertIsNone(observed["prepare_reason"])
        self.assertEqual(
            observed["after"],
            ("claude", "codex", "grok"),
        )
        conn = coopdb.connect(self.board, require_current=True)
        try:
            rows = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='workflow_recipe_promoted' "
                "ORDER BY event_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["from"], "quick_two")
        self.assertEqual(payload["to"], "standard_three")
        self.assertEqual(payload["reason"], "needs_input")
        self.assertEqual(
            trace_events[-1]["details"]["workflow_recipe"],
            "standard_three",
        )

    def test_non_retryable_provider_reason_reaches_trace_board_and_status(self):
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        action = {
            "kind": "claim_task",
            "item_id": self.item,
            "target_id": self.item,
            "command": [
                *coopdb.CLI_ARGV,
                "item",
                "claim",
                str(self.item),
            ],
        }
        provider_result = {
            "agent": "claude",
            "provider": "claude",
            "ok": False,
            "exit": 1,
            "tree_empty": True,
            "note": "provider_quota_exhausted",
            "classification": "provider_quota_exhausted",
            "retryable": False,
        }

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 return_value=provider_result) as invoke_turn, \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--max-turns",
                "5",
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 3)
        invoke_turn.assert_called_once()
        status = runner_status.read_status(self.status_path)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("failed", "provider_quota_exhausted", 1),
        )
        self.assertEqual(
            trace_events[-1],
            {
                "event": "run_finished",
                "details": {
                    "classification": "provider_quota_exhausted",
                    "workflow_recipe": "standard_three",
                },
            },
        )
        conn = coopdb.connect(self.board, require_current=True)
        try:
            row = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='autonomous_run_finished' "
                "ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            json.loads(row["payload_json"])["reason"],
            "provider_quota_exhausted",
        )

    def test_trace_emit_does_not_advance_item_board_probe(self):
        conn = coopdb.connect(self.board, require_current=True)
        try:
            before = coopdb.item_board_probe(conn, self.item)
            trace = coop_turn_trace.TurnTrace(
                self.trace_path,
                run_id="run",
                item_id=self.item,
                monotonic=lambda: 1.0,
                wall_clock=lambda: "now",
            )
            self.assertTrue(
                trace.emit("action_eligible", action="claim_task"))
            after = coopdb.item_board_probe(conn, self.item)
        finally:
            conn.close()
        self.assertEqual(after, before)

    def test_full_action_selects_capability_and_traces_mutation_delta(self):
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        action = {
            "kind": "claim_task",
            "item_id": self.item,
            "target_id": self.item,
            "command": [
                *coopdb.CLI_ARGV,
                "item",
                "claim",
                str(self.item),
            ],
        }
        observed = {}

        def invoke(**kwargs):
            observed["invoke"] = kwargs
            conn = coopdb.connect(self.board, require_current=True)
            try:
                coopdb.say(
                    conn,
                    session_id=kwargs["session_id"],
                    body="provider board progress",
                    item_id=self.item,
                )
            finally:
                conn.close()
            return {
                "agent": kwargs["agent_id"],
                "provider": kwargs["provider"],
                "ok": True,
                "exit": 0,
                "tree_empty": True,
                "note": "",
            }

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("codex")
            hint = envelope["kind"]
            observed["hint"] = hint
            observed["result"] = kwargs["take_turn"]("codex", hint)
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action) as next_action, \
             mock.patch.object(
                 coop_watcher,
                 "actionable",
                 side_effect=AssertionError(
                     "kind-only watcher must not be reread",
                 )), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 side_effect=invoke) as invoke_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
            ])

        self.assertEqual(code, 0)
        self.assertEqual(observed["hint"], "claim_task")
        self.assertTrue(observed["result"]["ok"])
        self.assertIs(observed["result"]["made_board_progress"], True)
        self.assertEqual(observed["result"]["actor_board_events"], 1)
        # Two derives: the routed action plus the failed mechanical
        # precommit's stale-guard refresh (board unchanged -> model turn).
        self.assertEqual(next_action.call_count, 2)
        invoke_turn.assert_called_once()
        call = observed["invoke"]
        self.assertIs(call["action"], action)
        self.assertEqual(call["capability_manifest"].name, "local_code")
        self.assertEqual(call["capability_config"]["version"], 1)
        self.assertIs(call["trace"], trace)
        self.assertEqual(
            pathlib.Path(call["run_dir"]),
            self.trace_path.parent,
        )
        self.assertIsNotNone(call["progress_before"])
        self.assertNotIn("completion_probe", call)

        eligible = next(
            event
            for event in trace_events
            if event["event"] == "action_eligible"
        )
        self.assertEqual(eligible["action"], "claim_task")
        self.assertEqual(eligible["capability_set"], "local_code")
        final = next(
            event
            for event in trace_events
            if event["event"] == "final_classification"
        )
        self.assertEqual(
            final["details"],
            {
                "classification": "board_progress",
                "board_mutations": 1,
                "timed_out": False,
                "exit_code": 0,
                "workflow_recipe": "standard_three",
                "workspace_surface": "write",
                "execution_mode": "tool_turn",
                "action_fingerprint": (
                    coop_action_scheduler.action_fingerprint(action)
                ),
            },
        )

    def test_token_efficient_mode_reaches_the_cold_turn_prompt_and_run_mark(self):
        observed = {}
        real_next_action = coop_watcher.next_action
        conn = coopdb.connect(self.board, require_current=True)
        try:
            coopdb.revise_item(
                conn,
                item_id=self.item,
                reason="complete test contract before autonomous kickoff",
                fields={
                    "scope": "scope",
                    "done_when": "done",
                    "output_contract": "output",
                    "context": "context",
                    "allowed_actions": ["read", "write"],
                    "stop_conditions": ["stop on ambiguity"],
                },
            )
        finally:
            conn.close()

        def invoke(**kwargs):
            observed["invoke"] = kwargs
            conn = coopdb.connect(self.board, require_current=True)
            try:
                coopdb.say(
                    conn,
                    session_id=kwargs["session_id"],
                    body="token-efficient prompt observed",
                    item_id=self.item,
                )
            finally:
                conn.close()
            observed["probe_after_say"] = kwargs["completion_probe"]()
            return {
                "agent": kwargs["agent_id"],
                "provider": kwargs["provider"],
                "ok": True,
                "exit": 0,
                "tree_empty": True,
                "note": "done",
            }

        def run(_participants, **kwargs):
            conn = coopdb.connect(self.board, require_current=True)
            try:
                session_id = conn.execute(
                    "SELECT session_id FROM sessions WHERE agent_id='codex' "
                    "AND status='running' ORDER BY started_at DESC LIMIT 1"
                ).fetchone()["session_id"]
                coopdb.claim_item(
                    conn,
                    item_id=self.item,
                    actor="codex",
                    session_id=session_id,
                    intent="work",
                )
            finally:
                conn.close()
            envelope = kwargs["actionable_fn"]("codex")
            self.assertEqual(envelope["kind"], "continue_task")
            result = kwargs["take_turn"]("codex", envelope["kind"])
            self.assertTrue(result["ok"])
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_TRACE_PATH": str(self.trace_path)}), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 side_effect=real_next_action), \
             mock.patch.object(
                 coop_autonomous,
                 "prepare_opt_in_worker_pool",
                 return_value=(None, (), {})), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 side_effect=invoke), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db", self.board,
                "--item", str(self.item),
                "--no-persistent-workers",
                "--no-mechanical-precommit",
                "--token-efficient",
            ])

        self.assertEqual(code, 0)
        prompt = observed["invoke"]["prompt"]
        self.assertTrue(prompt.startswith(
            coop_prompt_cache.TOKEN_EFFICIENT_PROMPT_PREFIX
            + coop_prompt_cache.HYDRATION_HEADER
        ))
        self.assertIn("command_crib (exact argv; do not run help):", prompt)
        self.assertTrue(callable(observed["invoke"]["completion_probe"]))
        self.assertIs(observed["probe_after_say"], False)
        conn = coopdb.connect(self.board, require_current=True)
        try:
            row = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='autonomous_run_started' "
                "ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIs(json.loads(row["payload_json"])["token_efficient"], True)

    def test_opt_in_codex_turn_uses_lazy_worker_pool_not_cold_invoke(self):
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        action = {
            "kind": "claim_task",
            "item_id": self.item,
            "target_id": self.item,
            "command": [*coopdb.CLI_ARGV, "item", "claim", str(self.item)],
        }
        observed = {}

        class FakePool:
            def start(inner_self, provider, *, core_profile, trace):
                observed["start"] = (provider, core_profile, trace)
                return coop_workers.WorkerHealth(
                    provider=provider,
                    mode="persistent",
                    state="ready",
                    process_starts=1,
                    turns_submitted=0,
                )

            def submit(inner_self, provider, turn, *, timeout_s):
                observed["submit"] = (provider, turn, timeout_s)
                conn = coopdb.connect(self.board, require_current=True)
                try:
                    coopdb.say(
                        conn,
                        session_id=turn.session_id,
                        body="persistent provider board progress",
                        item_id=self.item,
                    )
                finally:
                    conn.close()
                return coop_workers.TurnResult(
                    agent=provider,
                    provider=provider,
                    ok=True,
                    exit=None,
                    note="persistent_turn_completed",
                    tree_empty=False,
                )

            def stop_all(inner_self):
                observed["stopped"] = True

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("codex")
            observed["result"] = kwargs["take_turn"](
                "codex",
                envelope["kind"],
            )
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_TRACE_PATH": str(self.trace_path)}), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action), \
             mock.patch.object(
                 coop_autonomous,
                 "prepare_opt_in_worker_pool",
                 return_value=(FakePool(), (), {})), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn") as cold_invoke, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--persistent-provider",
                "codex",
            ])

        self.assertEqual(code, 0)
        cold_invoke.assert_not_called()
        self.assertTrue(observed["result"]["ok"])
        self.assertEqual(observed["start"][0], "codex")
        provider, turn, timeout_s = observed["submit"]
        self.assertEqual(provider, "codex")
        self.assertEqual(turn.action["kind"], "claim_task")
        self.assertEqual(timeout_s, 600.0)
        self.assertTrue(observed["stopped"])
        eligible = next(
            event
            for event in trace_events
            if event["event"] == "action_eligible"
        )
        self.assertEqual(
            eligible["details"]["worker_mode"],
            "persistent",
        )
        self.assertTrue(
            any(
                event["event"] == "first_board_mutation"
                for event in trace_events
            )
        )

    def test_worker_start_failure_demotes_provider_to_cold_before_prompt(self):
        action = {
            "kind": "claim_task",
            "item_id": self.item,
            "target_id": self.item,
            "command": [*coopdb.CLI_ARGV, "item", "claim", str(self.item)],
        }
        observed = {}
        adapter = _RunnerHerdrAdapter(
            teardown_failures=(RuntimeError("transient pane close"),),
        )

        class FailingStartPool:
            def start(inner_self, provider, *, core_profile, trace):
                del core_profile, trace
                observed["start_provider"] = provider
                raise coop_workers.WorkerUnavailable("handshake")

            def submit(inner_self, *args, **kwargs):
                raise AssertionError("failed worker must not receive prompt")

            def stop_all(inner_self):
                observed["stopped"] = True

        def cold_invoke(**kwargs):
            adapter.events.append(("cold", kwargs["provider"]))
            observed["cold"] = kwargs
            conn = coopdb.connect(self.board, require_current=True)
            try:
                coopdb.say(
                    conn,
                    session_id=kwargs["session_id"],
                    body="cold fallback board progress",
                    item_id=self.item,
                )
            finally:
                conn.close()
            return {
                "agent": kwargs["agent_id"],
                "provider": kwargs["provider"],
                "ok": True,
                "exit": 0,
                "note": "done",
                "tree_empty": True,
                "process_started": True,
                "session_created": False,
            }

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("codex")
            observed["result"] = kwargs["take_turn"](
                "codex",
                envelope["kind"],
            )
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_TRACE_PATH": str(self.trace_path)}), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action), \
             mock.patch.object(
                 coop_autonomous,
                 "prepare_opt_in_worker_pool",
                 return_value=(FailingStartPool(), (), {})), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 side_effect=cold_invoke), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main(
                [
                    "--db",
                    self.board,
                    "--item",
                    str(self.item),
                    "--persistent-provider",
                    "codex",
                    "--herdr",
                ],
                herdr_adapter=adapter,
            )

        self.assertEqual(code, 0)
        self.assertEqual(observed["start_provider"], "codex")
        self.assertTrue(observed["result"]["ok"])
        self.assertEqual(observed["cold"]["worker_mode"], "cold")
        self.assertTrue(observed["stopped"])
        self.assertEqual(
            [provider for _run_id, provider, _kwargs in adapter.spawn_calls],
            ["codex"],
        )
        self.assertEqual(
            adapter.teardown_calls,
            [
                ("opaque-pane::codex",),
                ("opaque-pane::codex",),
            ],
        )
        self.assertEqual(
            adapter.events,
            [
                ("spawn", "codex", "opaque-pane::codex"),
                ("teardown", ("opaque-pane::codex",)),
                ("teardown", ("opaque-pane::codex",)),
                ("cold", "codex"),
            ],
        )
        self.assertNotIn(
            "herdr",
            runner_status.read_status(self.status_path),
        )

    def test_worker_start_demotion_cleanup_failure_blocks_cold_fallback(self):
        action = {
            "kind": "claim_task",
            "item_id": self.item,
            "target_id": self.item,
            "command": [*coopdb.CLI_ARGV, "item", "claim", str(self.item)],
        }
        observed = {}
        adapter = _RunnerHerdrAdapter(
            teardown_failures=(
                RuntimeError("first pane close failed"),
                RuntimeError("retry pane close failed"),
            ),
        )

        class FailingStartPool:
            def start(inner_self, provider, *, core_profile, trace):
                del core_profile, trace
                observed["start_provider"] = provider
                raise coop_workers.WorkerUnavailable("handshake")

            def submit(inner_self, *args, **kwargs):
                raise AssertionError("failed worker must not receive prompt")

            def stop_all(inner_self):
                observed["stopped"] = True

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("codex")
            observed["result"] = kwargs["take_turn"](
                "codex",
                envelope["kind"],
            )
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_TRACE_PATH": str(self.trace_path)}), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action), \
             mock.patch.object(
                 coop_autonomous,
                 "prepare_opt_in_worker_pool",
                 return_value=(FailingStartPool(), (), {})), \
             mock.patch.object(coop_start, "invoke_turn") as cold_invoke, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main(
                [
                    "--db",
                    self.board,
                    "--item",
                    str(self.item),
                    "--persistent-provider",
                    "claude",
                    "--persistent-provider",
                    "codex",
                    "--herdr",
                ],
                herdr_adapter=adapter,
            )

        self.assertEqual(code, 0)
        self.assertEqual(observed["start_provider"], "codex")
        self.assertEqual(
            observed["result"]["classification"],
            "herdr_cleanup_failed",
        )
        self.assertIs(observed["result"]["retryable"], False)
        cold_invoke.assert_not_called()
        self.assertTrue(observed["stopped"])
        self.assertEqual(
            [provider for _run_id, provider, _kwargs in adapter.spawn_calls],
            ["claude", "codex"],
        )
        self.assertEqual(
            adapter.teardown_calls,
            [
                ("opaque-pane::codex",),
                ("opaque-pane::codex",),
            ],
        )
        self.assertNotIn(
            ("teardown", ("opaque-pane::claude",)),
            adapter.events,
        )
        self.assertEqual(
            runner_status.read_status(self.status_path)["herdr"],
            {
                "workspace": "opaque-workspace::run",
                "panes": {
                    "claude": "opaque-pane::claude",
                    "codex": "opaque-pane::codex",
                },
            },
        )

    def test_unsupported_session_demotes_and_rereads_before_cold_turn(self):
        # Both actions are resident-eligible, so the second turn proves the
        # demotion rather than claude's ordinary cold path for local_code.
        first_action = {
            "kind": "review_task",
            "item_id": self.item,
            "target_id": 7,
            "command": [*coopdb.CLI_ARGV, "review", "submit", "7"],
        }
        second_action = {
            "kind": "review_task",
            "item_id": self.item,
            "target_id": 8,
            "command": [*coopdb.CLI_ARGV, "review", "submit", "8"],
        }
        observed = {}
        adapter = _RunnerHerdrAdapter()

        class UnsupportedSessionPool:
            def start(inner_self, provider, *, core_profile, trace):
                del core_profile, trace
                return coop_workers.WorkerHealth(
                    provider=provider,
                    mode="resumed",
                    state="ready",
                    process_starts=0,
                    turns_submitted=0,
                )

            def submit(inner_self, provider, turn, *, timeout_s):
                observed["submit"] = (provider, turn, timeout_s)
                return coop_workers.TurnResult(
                    agent=provider,
                    provider=provider,
                    ok=False,
                    exit=2,
                    note="provider_session_unsupported",
                    tree_empty=True,
                    classification="provider_session_unsupported",
                    retryable=False,
                    process_started=True,
                    session_created=False,
                    extra={"cold_fallback_safe": True},
                )

            def stop_all(inner_self):
                observed["stopped"] = True

        def cold_invoke(**kwargs):
            adapter.events.append(("cold", kwargs["provider"]))
            observed["cold"] = kwargs
            conn = coopdb.connect(self.board, require_current=True)
            try:
                coopdb.say(
                    conn,
                    session_id=kwargs["session_id"],
                    body="safe cold fallback board progress",
                    item_id=self.item,
                )
            finally:
                conn.close()
            return {
                "agent": kwargs["agent_id"],
                "provider": kwargs["provider"],
                "ok": True,
                "exit": 0,
                "note": "done",
                "tree_empty": True,
                "process_started": True,
                "session_created": False,
            }

        def run(_participants, **kwargs):
            first_envelope = kwargs["actionable_fn"]("claude")
            observed["first_result"] = kwargs["take_turn"](
                "claude",
                first_envelope["kind"],
            )
            second_envelope = kwargs["actionable_fn"]("claude")
            observed["second_result"] = kwargs["take_turn"](
                "claude",
                second_envelope["kind"],
            )
            return "stopped", 2, []

        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_TRACE_PATH": str(self.trace_path)}), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 # Each envelope twice: the derive plus the failed
                 # mechanical-precommit's stale-guard refresh (the board
                 # has not moved at either point).
                 side_effect=(
                     first_action, first_action,
                     second_action, second_action,
                 )) as next_action, \
             mock.patch.object(
                 coop_autonomous,
                 "prepare_opt_in_worker_pool",
                 return_value=(UnsupportedSessionPool(), (), {})), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 side_effect=cold_invoke), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main(
                [
                    "--db",
                    self.board,
                    "--item",
                    str(self.item),
                    "--persistent-provider",
                    "claude",
                    "--herdr",
                ],
                herdr_adapter=adapter,
            )

        self.assertEqual(code, 0)
        self.assertEqual(observed["submit"][0], "claude")
        self.assertEqual(
            observed["first_result"]["classification"],
            "provider_session_unsupported",
        )
        self.assertIs(
            observed["first_result"]["retry_after_board_refresh"],
            True,
        )
        self.assertTrue(observed["second_result"]["ok"])
        self.assertEqual(observed["cold"]["worker_mode"], "cold")
        self.assertIs(observed["cold"]["action"], second_action)
        self.assertEqual(
            observed["cold"]["capability_manifest"].name,
            "deep_review",
        )
        self.assertEqual(next_action.call_count, 4)
        self.assertTrue(observed["stopped"])
        self.assertEqual(
            [provider for _run_id, provider, _kwargs in adapter.spawn_calls],
            ["claude"],
        )
        self.assertEqual(
            adapter.teardown_calls,
            [("opaque-pane::claude",)],
        )
        self.assertEqual(
            adapter.events,
            [
                ("spawn", "claude", "opaque-pane::claude"),
                ("teardown", ("opaque-pane::claude",)),
                ("cold", "claude"),
            ],
        )
        self.assertNotIn(
            "herdr",
            runner_status.read_status(self.status_path),
        )

    def test_worker_cleanup_failure_changes_final_run_status(self):
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )

        class CleanupFailingPool:
            def stop_all(inner_self):
                raise coop_workers.WorkerCleanupError(
                    (("codex", "OSError"),)
                )

        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_TRACE_PATH": str(self.trace_path)}), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_autonomous,
                 "prepare_opt_in_worker_pool",
                 return_value=(CleanupFailingPool(), (), {})), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 return_value=("stopped", 0, [])), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--persistent-provider",
                "codex",
            ])

        self.assertEqual(code, 3)
        finished = next(
            event
            for event in trace_events
            if event["event"] == "run_finished"
        )
        self.assertEqual(
            finished["details"]["classification"],
            "worker_cleanup_failed",
        )

    def test_retained_turn_cleanup_failure_changes_final_run_status(self):
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )

        class RetainedRegistry:
            pending = 1

            def drain(inner_self):
                raise coop_workers.WorkerCleanupError(
                    (("claude:process_tree", "OSError"),)
                )

        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_TRACE_PATH": str(self.trace_path)}), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_autonomous,
                 "prepare_opt_in_worker_pool",
                 return_value=(None, (), {})), \
             mock.patch.object(
                 coop_workers,
                 "RunCleanupRegistry",
                 return_value=RetainedRegistry()), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 return_value=("stopped", 0, [])), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
            ])

        self.assertEqual(code, 3)
        finished = next(
            event
            for event in trace_events
            if event["event"] == "run_finished"
        )
        self.assertEqual(
            finished["details"]["classification"],
            "worker_cleanup_failed",
        )

    def test_capability_denial_does_not_start_provider(self):
        conn = coopdb.connect(self.board, require_current=True)
        try:
            conn.execute(
                "UPDATE items SET allowed_actions=? WHERE id=?",
                ('[\"capability:unknown_tool\"]', self.item),
            )
            conn.commit()
        finally:
            conn.close()
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        action = {
            "kind": "continue_task",
            "item_id": self.item,
            "target_id": self.item,
            "command": ["must", "remain", "on", "board"],
        }
        observed = {}

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("codex")
            hint = envelope["kind"]
            observed["result"] = kwargs["take_turn"]("codex", hint)
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_TRACE_PATH": str(self.trace_path)}), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn") as invoke_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
            ])

        self.assertEqual(code, 0)
        self.assertFalse(observed["result"]["ok"])
        self.assertTrue(
            observed["result"]["note"].startswith("capability_denied:")
        )
        invoke_turn.assert_not_called()
        final = next(
            event
            for event in trace_events
            if event["event"] == "final_classification"
        )
        self.assertEqual(
            final["details"]["classification"],
            "capability_denied",
        )

    def test_preflight_failure_replaces_starting_status(self):
        with mock.patch.dict(
                os.environ,
                {"COOP_RUN_STATUS_PATH": str(self.status_path)}), \
             mock.patch.object(
                 coop_start, "resolve_participants",
                 return_value={
                     "available": ["claude"],
                     "skipped": [],
                 }):
            code = coop_autonomous.main([
                "--db", self.board, "--item", str(self.item)])

        self.assertEqual(code, 2)
        status = runner_status.read_status(self.status_path)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("failed", "insufficient_providers", 0),
        )

    def test_two_provider_standard_recipe_fails_before_board_mutation(self):
        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv") as resolved_argv, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous") as run:
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
            ])

        self.assertEqual(code, 2)
        resolved_argv.assert_not_called()
        run.assert_not_called()
        status = runner_status.read_status(self.status_path)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("failed", "recipe_blocked", 0),
        )
        conn = coopdb.connect(self.board, require_current=True)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type LIKE 'autonomous_run_%'"
            ).fetchone()[0]
            sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)
        self.assertEqual(sessions, 0)

    def test_real_runner_overlaps_independent_actions_with_isolated_db_access(
            self):
        actions = {
            "claude": {
                "kind": "answer_question",
                "target_type": "question",
                "target_id": 101,
                "item_id": self.item,
                "command": ["board", "owned", "command", "101"],
            },
            "codex": {
                "kind": "answer_question",
                "target_type": "question",
                "target_id": 102,
                "item_id": self.item,
                "command": ["board", "owned", "command", "102"],
            },
            "grok": {"kind": "idle"},
        }
        started = []
        lock = threading.Lock()
        both_started = threading.Event()
        writer_finished = threading.Event()

        def invoke(**kwargs):
            with lock:
                started.append(kwargs["agent_id"])
                if len(started) == 2:
                    both_started.set()
            overlapped = both_started.wait(timeout=2)
            if kwargs["agent_id"] == "claude":
                conn = coopdb.connect(self.board, require_current=True)
                try:
                    coopdb.say(
                        conn,
                        session_id=kwargs["session_id"],
                        body="claude parallel progress",
                        item_id=self.item,
                    )
                finally:
                    conn.close()
                writer_finished.set()
            else:
                self.assertTrue(writer_finished.wait(timeout=2))
            return {
                "agent": kwargs["agent_id"],
                "provider": kwargs["provider"],
                "ok": overlapped,
                "exit": 0,
                "tree_empty": True,
                "note": "",
            }

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 side_effect=lambda _db, agent, **_kwargs: actions[agent]), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 side_effect=invoke):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--max-turns",
                "2",
                "--no-mechanical-precommit",
                "--no-persistent-workers",
                "--no-structured-answers",
            ])

        self.assertEqual(code, 3)
        self.assertCountEqual(started, ["claude", "codex"])
        events = coop_turn_trace.read_events(self.trace_path)
        finals = [
            event
            for event in events
            if event["event"] == "final_classification"
        ]
        self.assertEqual(len(finals), 2)
        self.assertCountEqual(
            [event["agent"] for event in finals],
            ["claude", "codex"],
        )
        by_agent = {
            event["agent"]: event["details"]
            for event in finals
        }
        self.assertEqual(
            (
                by_agent["claude"]["classification"],
                by_agent["claude"]["board_mutations"],
            ),
            ("board_progress", 1),
        )
        self.assertEqual(
            (
                by_agent["codex"]["classification"],
                by_agent["codex"]["board_mutations"],
            ),
            ("no_board_progress", 0),
        )
        first_mutations = [
            event["agent"]
            for event in events
            if event["event"] == "first_board_mutation"
        ]
        self.assertEqual(first_mutations, ["claude"])
        conn = coopdb.connect(self.board, require_current=True)
        try:
            messages = conn.execute(
                "SELECT body FROM messages WHERE item_id=? "
                "ORDER BY id",
                (self.item,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(messages), 1)

    def test_isolated_structured_failure_defers_without_tool_fallthrough(self):
        question_id = self._seed_open_question()
        action = self._held_answer_action(question_id)
        observed = {}

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("claude")
            candidate = coop_autonomous.coop_action_scheduler.ActionCandidate(
                agent="claude",
                hint=envelope["kind"],
                action=envelope,
            )
            profile = kwargs["profile_fn"](candidate)
            observed["first_profile"] = profile
            observed["result"] = kwargs["take_turn"](
                "claude",
                envelope["kind"],
                profile,
            )
            observed["retry_profile"] = kwargs["profile_fn"](candidate)
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_answer",
                 return_value=None), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 return_value={
                     "agent": "claude",
                     "provider": "claude",
                     "ok": False,
                     "exit": 1,
                     "note": "tool fallback launched",
                 }) as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(
            observed["first_profile"].workspace_surface,
            "none",
        )
        self.assertEqual(
            observed["result"]["classification"],
            "structured_answer_deferred_fallback",
        )
        self.assertIs(
            observed["result"]["retry_after_board_refresh"],
            True,
        )
        tool_turn.assert_not_called()
        self.assertEqual(
            (
                observed["retry_profile"].workspace_surface,
                observed["retry_profile"].execution_mode,
            ),
            ("read", "tool_turn"),
        )

    def test_isolated_postcommit_refusal_also_defers_fallback(self):
        question_id = self._seed_open_question()
        action = self._held_answer_action(question_id)
        observed = {}

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("claude")
            candidate = coop_autonomous.coop_action_scheduler.ActionCandidate(
                agent="claude",
                hint=envelope["kind"],
                action=envelope,
            )
            profile = kwargs["profile_fn"](candidate)
            observed["result"] = kwargs["take_turn"](
                "claude",
                envelope["kind"],
                profile,
            )
            observed["retry_profile"] = kwargs["profile_fn"](candidate)
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_answer",
                 return_value="bounded answer"), \
             mock.patch.object(
                 coop_autonomous.subprocess,
                 "run",
                 return_value=mock.Mock(returncode=2)), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 return_value={
                     "agent": "claude",
                     "provider": "claude",
                     "ok": False,
                     "exit": 1,
                     "note": "tool fallback launched",
                 }) as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(
            observed["result"]["classification"],
            "structured_answer_deferred_fallback",
        )
        self.assertIs(
            observed["result"]["retry_after_board_refresh"],
            True,
        )
        tool_turn.assert_not_called()
        self.assertEqual(
            (
                observed["retry_profile"].workspace_surface,
                observed["retry_profile"].execution_mode,
            ),
            ("read", "tool_turn"),
        )

    def test_advancing_precommit_resets_actor_baseline_before_model_noop(self):
        self._seed_open_question()
        observed = {}
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("claude")
            self.assertEqual(envelope["kind"], "answer_question")
            self.assertIsNone(envelope["claim_id"])
            observed["result"] = kwargs["take_turn"](
                "claude",
                envelope["kind"],
            )
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_answer",
                 return_value=None), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn") as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        tool_turn.assert_not_called()
        self.assertEqual(
            observed["result"]["classification"],
            "structured_answer_deferred_fallback",
        )
        self.assertIs(observed["result"]["made_board_progress"], False)
        self.assertEqual(observed["result"]["actor_board_events"], 0)
        self.assertFalse(
            any(
                event["event"] == "first_board_mutation"
                for event in trace_events
            )
        )
        final = next(
            event
            for event in trace_events
            if event["event"] == "final_classification"
        )
        self.assertEqual(final["details"]["board_mutations"], 0)

    def test_fully_satisfied_precommit_counts_its_actor_event(self):
        observed = {}
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        receipt_path = pathlib.Path(self.tmp.name) / "completion-receipt.txt"
        receipt_path.write_text("verified", encoding="utf-8")
        conn = coopdb.connect(self.board, require_current=True)
        try:
            coopdb.revise_item(
                conn,
                item_id=self.item,
                reason="complete the test contract",
                fields={
                    "scope": "bounded scope",
                    "done_when": "review approves",
                    "output_contract": "tested change",
                    "context": "runner integration",
                    "allowed_actions": ["read", "write"],
                    "stop_conditions": ["stop on failed verification"],
                },
            )
        finally:
            conn.close()

        def run(_participants, **kwargs):
            conn = coopdb.connect(self.board, require_current=True)
            try:
                session_rows = conn.execute(
                    "SELECT agent_id, session_id FROM sessions "
                    "WHERE status='running'"
                ).fetchall()
                sessions = {
                    row["agent_id"]: row["session_id"]
                    for row in session_rows
                }
                claim = coopdb.claim_item(
                    conn,
                    item_id=self.item,
                    actor="claude",
                    session_id=sessions["claude"],
                    intent="implement",
                    lease_seconds=3600,
                )
                coopdb.submit_receipt(
                    conn,
                    claim_id=claim["claim_id"],
                    session_id=sessions["claude"],
                    actor="claude",
                    path=str(receipt_path),
                    summary="implemented",
                    proof="verified",
                    proof_refs=[],
                )
                review_id = coopdb.request_review(
                    conn,
                    claim_id=claim["claim_id"],
                    session_id=sessions["claude"],
                    actor="claude",
                    reviewer="codex",
                )
                review_claim, _packet = coopdb.claim_review(
                    conn,
                    review_id=review_id,
                    session_id=sessions["codex"],
                    intent="review",
                )
                coopdb.submit_verdict(
                    conn,
                    claim_id=review_claim["claim_id"],
                    session_id=sessions["codex"],
                    actor="codex",
                    verdict="approve",
                )
            finally:
                conn.close()

            envelope = kwargs["actionable_fn"]("claude")
            self.assertEqual(envelope["kind"], "complete_task")
            observed["result"] = kwargs["take_turn"](
                "claude",
                envelope["kind"],
            )
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn") as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        tool_turn.assert_not_called()
        self.assertEqual(
            observed["result"]["note"],
            "mechanical_precommit_satisfied",
        )
        self.assertIs(observed["result"]["made_board_progress"], True)
        self.assertEqual(observed["result"]["actor_board_events"], 1)
        self.assertEqual(
            sum(
                event["event"] == "first_board_mutation"
                for event in trace_events
            ),
            1,
        )
        final = next(
            event
            for event in trace_events
            if event["event"] == "final_classification"
        )
        self.assertEqual(final["details"]["board_mutations"], 1)

    def test_actor_probe_failure_fails_closed_without_itemwide_fallback(self):
        action = {
            "kind": "claim_task",
            "target_type": "item",
            "target_id": self.item,
            "item_id": self.item,
            "command": [
                *coopdb.CLI_ARGV,
                "item",
                "claim",
                str(self.item),
            ],
        }
        observed = {}
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )

        def invoke(**kwargs):
            conn = coopdb.connect(self.board, require_current=True)
            try:
                coopdb.say(
                    conn,
                    session_id=kwargs["session_id"],
                    body="real write hidden by failed actor probe",
                    item_id=self.item,
                )
            finally:
                conn.close()
            return {
                "agent": kwargs["agent_id"],
                "provider": kwargs["provider"],
                "ok": True,
                "exit": 0,
                "note": "",
            }

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("codex")
            observed["result"] = kwargs["take_turn"](
                "codex",
                envelope["kind"],
            )
            return "stopped", 1, []

        output = io.StringIO()
        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_watcher,
                 "next_action",
                 return_value=action), \
             mock.patch.object(
                 coopdb,
                 "actor_event_probe",
                 side_effect=RuntimeError("probe unavailable")) as probe, \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 side_effect=invoke), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False), \
             contextlib.redirect_stdout(output):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--all",
                "--no-mechanical-precommit",
                "--no-persistent-workers",
                "--no-structured-answers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(probe.call_count, 1)
        self.assertIsNone(probe.call_args.kwargs["item_id"])
        self.assertEqual(output.getvalue().count("actor progress probe"), 1)
        self.assertIs(observed["result"]["made_board_progress"], False)
        self.assertEqual(observed["result"]["actor_board_events"], 0)
        self.assertEqual(
            observed["result"]["classification"],
            "no_board_progress",
        )
        self.assertFalse(
            any(
                event["event"] == "first_board_mutation"
                for event in trace_events
            )
        )
        final = next(
            event
            for event in trace_events
            if event["event"] == "final_classification"
        )
        self.assertEqual(final["details"]["board_mutations"], 0)

    def test_precommit_can_narrow_read_profile_to_isolated_answer(self):
        question_id = self._seed_open_question()
        observed = {}
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True
        )
        subprocess_results = []
        real_subprocess_run = coop_autonomous.subprocess.run

        def recording_subprocess_run(*args, **kwargs):
            proc = real_subprocess_run(*args, **kwargs)
            subprocess_results.append({
                "argv": args[0] if args else kwargs.get("args"),
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            })
            return proc

        def run(_participants, **kwargs):
            envelope = kwargs["actionable_fn"]("claude")
            self.assertIsNone(envelope["claim_id"])
            candidate = coop_autonomous.coop_action_scheduler.ActionCandidate(
                agent="claude",
                hint=envelope["kind"],
                action=envelope,
            )
            profile = kwargs["profile_fn"](candidate)
            observed["admitted_profile"] = profile
            observed["result"] = kwargs["take_turn"](
                "claude",
                envelope["kind"],
                profile,
            )
            return "stopped", 1, []

        def structured_answer(**kwargs):
            kwargs["usage_callback"]({
                "model_id": "claude-opus-5",
                "input_tokens": 500,
                "uncached_input_tokens": 80,
                "cached_input_tokens": 400,
                "cache_write_input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 510,
                "model_calls": 1,
                "usage_observation": "complete",
            })
            return "bounded answer"

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_turn_trace,
                 "TurnTrace",
                 return_value=trace), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_answer",
                 side_effect=structured_answer) as structured, \
             mock.patch.object(
                 coop_autonomous.subprocess,
                 "run",
                 side_effect=recording_subprocess_run), \
             mock.patch.object(
                 coop_start,
                 "invoke_turn",
                 return_value={
                     "agent": "claude",
                     "provider": "claude",
                     "ok": False,
                     "exit": 1,
                     "note": "tool fallback launched",
                 }) as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(
            (
                observed["admitted_profile"].workspace_surface,
                observed["admitted_profile"].execution_mode,
            ),
            ("read", "tool_turn"),
        )
        precommit = next(
            event
            for event in trace_events
            if event["event"] == "mechanical_precommit"
        )
        self.assertEqual(
            precommit["details"]["exit_code"],
            0,
            subprocess_results,
        )
        structured.assert_called_once()
        tool_turn.assert_not_called()
        self.assertEqual(
            observed["result"]["note"],
            "structured_answer_postcommit",
        )
        self.assertIs(observed["result"]["made_board_progress"], True)
        self.assertEqual(observed["result"]["actor_board_events"], 1)
        conn = coopdb.connect(self.board, require_current=True)
        try:
            question = conn.execute(
                "SELECT status, answer FROM questions WHERE question_id=?",
                (question_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(
            (question["status"], question["answer"]),
            ("answered", "bounded answer"),
        )
        provider_result = next(
            event
            for event in trace_events
            if event["event"] == "provider_result_received"
        )
        self.assertTrue(provider_result["turn_id"])
        self.assertEqual(provider_result["details"]["model_id"], "claude-opus-5")
        self.assertEqual(
            provider_result["details"]["uncached_input_tokens"],
            80,
        )
        self.assertEqual(
            provider_result["details"]["execution_mode"],
            "isolated_structured_answer",
        )
        self.assertNotIn("bounded answer", repr(provider_result))

    def test_mesh_v2_outbound_uses_one_isolated_decision_and_atomic_batch(self):
        self._use_mesh_v2_item()
        observed = {}
        usage = {
            "model_id": "claude-opus-5",
            "input_tokens": 120,
            "uncached_input_tokens": 80,
            "cached_input_tokens": 20,
            "cache_write_input_tokens": 20,
            "output_tokens": 12,
            "total_tokens": 132,
            "model_calls": 1,
            "usage_observation": "complete",
        }

        def run(_participants, **kwargs):
            conn = coopdb.connect(self.board, require_current=True)
            try:
                session_id = conn.execute(
                    "SELECT session_id FROM sessions WHERE agent_id='claude' "
                    "AND status='running'"
                ).fetchone()["session_id"]
                coopdb.claim_item(
                    conn,
                    item_id=self.item,
                    actor="claude",
                    session_id=session_id,
                    intent="start compiled mesh",
                    lease_seconds=3600,
                )
            finally:
                conn.close()
            action = kwargs["actionable_fn"]("claude")
            candidate = coop_action_scheduler.ActionCandidate(
                agent="claude", hint=action["kind"], action=action)
            observed["admitted"] = kwargs["profile_fn"](candidate)
            observed["result"] = kwargs["take_turn"](
                "claude", action["kind"], observed["admitted"])
            return "stopped", 1, []

        def decide(**kwargs):
            request = kwargs["request"]
            observed["request"] = request
            kwargs["usage_callback"](usage)
            return coop_decisions.DecisionResult(
                value={
                    "questions": [
                        {
                            "recipient": "codex",
                            "question": "Ping from Claude to Codex; acknowledge.",
                        },
                        {
                            "recipient": "grok",
                            "question": "Ping from Claude to Grok; acknowledge.",
                        },
                    ]
                },
                usage=usage,
            )

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_decision",
                 side_effect=decide) as structured, \
             mock.patch.object(coop_start, "invoke_turn") as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db", self.board,
                "--item", str(self.item),
                "--token-efficient",
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(
            (
                observed["admitted"].workspace_surface,
                observed["admitted"].execution_mode,
            ),
            ("write", "tool_turn"),
        )
        self.assertEqual(
            observed["request"].decision_kind,
            "compose_mesh_questions",
        )
        self.assertEqual(
            observed["result"]["note"],
            "structured_mesh_questions_postcommit",
        )
        structured.assert_called_once()
        tool_turn.assert_not_called()
        conn = coopdb.connect(self.board, require_current=True)
        try:
            rows = conn.execute(
                "SELECT asked_by_agent, assigned_to_agent, status FROM "
                "questions WHERE item_id=? ORDER BY question_id",
                (self.item,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("claude", "codex", "open"),
                ("claude", "grok", "open"),
            ],
        )
        final = next(
            event for event in coop_turn_trace.read_events(self.trace_path)
            if event["event"] == "final_classification"
        )
        self.assertEqual(
            (
                final["details"]["workspace_surface"],
                final["details"]["execution_mode"],
            ),
            ("none", "isolated_structured_mesh_questions"),
        )

    def test_mesh_v2_transfer_is_code_rendered_with_zero_model_calls(self):
        self._use_mesh_v2_item()
        observed = {}

        def run(_participants, **kwargs):
            conn = coopdb.connect(self.board, require_current=True)
            try:
                sessions = {
                    row["agent_id"]: row["session_id"]
                    for row in conn.execute(
                        "SELECT agent_id, session_id FROM sessions WHERE "
                        "status='running'"
                    )
                }
                claim = coopdb.claim_item(
                    conn,
                    item_id=self.item,
                    actor="claude",
                    session_id=sessions["claude"],
                    intent="start compiled mesh",
                    lease_seconds=3600,
                )
                question_ids = coopdb.needs_input_batch(
                    conn,
                    claim_id=claim["claim_id"],
                    session_id=sessions["claude"],
                    questions=(
                        ("codex", "Ping from Claude to Codex."),
                        ("grok", "Ping from Claude to Grok."),
                    ),
                )
                for recipient, question_id in zip(
                        ("codex", "grok"), question_ids):
                    response = coopdb.claim_questions_batch(
                        conn,
                        question_ids=[question_id],
                        session_id=sessions[recipient],
                        intent=f"answer {question_id}",
                    )[0]
                    coopdb.answer_questions_batch(
                        conn,
                        session_id=sessions[recipient],
                        answers=[(
                            response["claim_id"],
                            f"Acknowledged by {recipient}.",
                        )],
                    )
                coopdb.claim_item(
                    conn,
                    item_id=self.item,
                    actor="claude",
                    session_id=sessions["claude"],
                    intent="resume compiled mesh",
                    reclaim_reason="both answers arrived",
                    lease_seconds=3600,
                )
            finally:
                conn.close()
            action = kwargs["actionable_fn"]("claude")
            observed["result"] = kwargs["take_turn"](
                "claude", action["kind"])
            return "stopped", 1, []

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_decision") as structured, \
             mock.patch.object(coop_start, "invoke_turn") as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db", self.board,
                "--item", str(self.item),
                "--token-efficient",
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(
            observed["result"]["note"],
            "compiled_mesh_transfer_postcommit",
        )
        structured.assert_not_called()
        tool_turn.assert_not_called()
        conn = coopdb.connect(self.board, require_current=True)
        try:
            row = conn.execute(
                "SELECT from_agent, to_agent, status FROM handoffs WHERE "
                "item_id=?",
                (self.item,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(row), ("claude", "codex", "pending"))
        final = next(
            event for event in coop_turn_trace.read_events(self.trace_path)
            if event["event"] == "final_classification"
        )
        self.assertEqual(
            (
                final["details"]["workspace_surface"],
                final["details"]["execution_mode"],
            ),
            ("none", "compiled_mesh_transfer"),
        )

    def test_promoted_mesh_report_arm_renders_and_receipts_in_one_call(self):
        self._use_mesh_v2_item()
        (pathlib.Path(self.tmp.name) / "docs" / "evidence").mkdir(
            parents=True)
        observed = {}
        usage = {
            "model_id": "grok-test-model",
            "input_tokens": 240,
            "uncached_input_tokens": 200,
            "cached_input_tokens": 20,
            "cache_write_input_tokens": 20,
            "output_tokens": 40,
            "total_tokens": 280,
            "model_calls": 1,
            "usage_observation": "complete",
        }
        report_value = {
            "what_was_asked": "Exchange all six directed provider pings.",
            "method": "Each sender posted; each addressed peer answered.",
            "stop_boundaries": ["none"],
            "what_this_proves": "All six board routes completed.",
            "what_this_does_not_prove": "This is not a work benchmark.",
            "receipt_summary": (
                "Done: six pings and the evidence report. Not done: none. "
                "Stop boundaries: none."
            ),
        }

        def run(_participants, **kwargs):
            conn = coopdb.connect(self.board, require_current=True)
            try:
                sessions = {
                    row["agent_id"]: row["session_id"]
                    for row in conn.execute(
                        "SELECT agent_id, session_id FROM sessions WHERE "
                        "status='running'"
                    )
                }
                current_claim = coopdb.claim_item(
                    conn,
                    item_id=self.item,
                    actor="claude",
                    session_id=sessions["claude"],
                    intent="start compiled mesh",
                    lease_seconds=3600,
                )["claim_id"]
                for index, sender in enumerate(coop_mesh.MESH_PARTICIPANTS):
                    action = coopdb.status(
                        conn,
                        sender,
                        session_id=sessions[sender],
                        item_id=self.item,
                    )["next_action"]
                    outbound = coop_mesh.compile_mesh_phase(
                        conn,
                        item=coopdb.item_show(conn, self.item),
                        action=action,
                        agent=sender,
                    )
                    question_ids = coopdb.needs_input_batch(
                        conn,
                        claim_id=current_claim,
                        session_id=sessions[sender],
                        questions=tuple(
                            (
                                recipient,
                                f"Ping from {sender} to {recipient}.",
                            )
                            for recipient in outbound.recipients
                        ),
                    )
                    for recipient, question_id in zip(
                            outbound.recipients, question_ids):
                        response = coopdb.claim_questions_batch(
                            conn,
                            question_ids=[question_id],
                            session_id=sessions[recipient],
                            intent=f"answer {question_id}",
                        )[0]
                        coopdb.answer_questions_batch(
                            conn,
                            session_id=sessions[recipient],
                            answers=[(
                                response["claim_id"],
                                f"Acknowledged by {recipient}.",
                            )],
                        )
                    current_claim = coopdb.claim_item(
                        conn,
                        item_id=self.item,
                        actor=sender,
                        session_id=sessions[sender],
                        intent=f"resume {sender}",
                        reclaim_reason="both answers arrived",
                        lease_seconds=3600,
                    )["claim_id"]
                    action = coopdb.status(
                        conn,
                        sender,
                        session_id=sessions[sender],
                        item_id=self.item,
                    )["next_action"]
                    phase = coop_mesh.compile_mesh_phase(
                        conn,
                        item=coopdb.item_show(conn, self.item),
                        action=action,
                        agent=sender,
                    )
                    if index < 2:
                        created = coopdb.create_handoff(
                            conn,
                            claim_id=phase.claim_id,
                            session_id=sessions[sender],
                            actor=sender,
                            to_agent=phase.to_agent,
                            reason=phase.reason,
                            summary=phase.summary,
                            completed=phase.completed,
                            remaining=phase.remaining,
                            risks=phase.risks,
                            next_action=phase.next_action,
                            proof_refs=phase.proof_refs,
                        )
                        current_claim = coopdb.accept_handoff(
                            conn,
                            handoff_id=created["handoff_id"],
                            session_id=sessions[phase.to_agent],
                            actor=phase.to_agent,
                            intent=f"accept {created['handoff_id']}",
                            lease_seconds=3600,
                        )["claim_id"]
            finally:
                conn.close()
            action = kwargs["actionable_fn"]("grok")
            observed["result"] = kwargs["take_turn"](
                "grok", action["kind"])
            return "stopped", 1, []

        def decide(**kwargs):
            observed["request"] = kwargs["request"]
            kwargs["usage_callback"](usage)
            return coop_decisions.DecisionResult(
                value=report_value,
                usage=usage,
            )

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_autonomous,
                 "STRUCTURED_MESH_REPORT_PROVIDERS",
                 frozenset({"grok"})), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_decision",
                 side_effect=decide) as structured, \
             mock.patch.object(coop_start, "invoke_turn") as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db", self.board,
                "--item", str(self.item),
                "--token-efficient",
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(
            observed["request"].decision_kind,
            "compose_mesh_report_sections",
        )
        self.assertEqual(
            observed["result"]["note"],
            "structured_mesh_report_postcommit",
        )
        structured.assert_called_once()
        tool_turn.assert_not_called()
        conn = coopdb.connect(self.board, require_current=True)
        try:
            item = coopdb.item_show(conn, self.item)
            receipt = conn.execute(
                "SELECT source_path FROM receipts WHERE item_id=?",
                (self.item,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(receipt)
        self.assertTrue(pathlib.Path(receipt["source_path"]).is_file())
        self.assertEqual(item["status"], "working")
        final = next(
            event for event in coop_turn_trace.read_events(self.trace_path)
            if event["event"] == "final_classification"
        )
        self.assertEqual(
            (
                final["details"]["workspace_surface"],
                final["details"]["execution_mode"],
            ),
            ("write", "isolated_structured_mesh_report"),
        )

    def test_mesh_report_request_and_review_use_one_bounded_model_call(self):
        self._use_mesh_v2_item()
        (pathlib.Path(self.tmp.name) / "docs" / "evidence").mkdir(
            parents=True)
        observed = {"results": []}
        usage = {
            "model_id": "claude-opus-5",
            "input_tokens": 135,
            "uncached_input_tokens": 90,
            "cached_input_tokens": 30,
            "cache_write_input_tokens": 15,
            "output_tokens": 8,
            "total_tokens": 143,
            "model_calls": 1,
            "usage_observation": "complete",
        }

        def run(_participants, **kwargs):
            self._seed_mesh_v2_composition()

            def act(agent):
                action = kwargs["actionable_fn"](agent)
                candidate = coop_action_scheduler.ActionCandidate(
                    agent=agent,
                    hint=action["kind"],
                    action=action,
                )
                profile = kwargs["profile_fn"](candidate)
                result = kwargs["take_turn"](
                    agent, action["kind"], profile)
                observed["results"].append(result)
                return action

            self.assertEqual(act("grok")["kind"], "continue_task")
            self.assertEqual(act("grok")["kind"], "request_review")
            self.assertEqual(act("claude")["kind"], "review_task")
            observed["next_action"] = kwargs["actionable_fn"]("grok")
            return "stopped", 3, []

        def decide(**kwargs):
            request = kwargs["request"]
            observed["request"] = request
            kwargs["usage_callback"](usage)
            return coop_decisions.DecisionResult(
                value={
                    "review_id": request.json_schema[
                        "properties"
                    ]["review_id"]["const"],
                    "verdict": "approve",
                    "body": "",
                },
                usage=usage,
            )

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_decision",
                 side_effect=decide) as structured, \
             mock.patch.object(coop_start, "invoke_turn") as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db", self.board,
                "--item", str(self.item),
                "--token-efficient",
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(
            [result["note"] for result in observed["results"]],
            [
                "compiled_mesh_report_postcommit",
                "compiled_mesh_review_request_postcommit",
                "structured_mesh_review_postcommit",
            ],
        )
        self.assertEqual(
            observed["request"].decision_kind,
            "review_mesh_report",
        )
        structured.assert_called_once()
        tool_turn.assert_not_called()
        conn = coopdb.connect(self.board, require_current=True)
        try:
            review = conn.execute(
                "SELECT reviewer, status FROM reviews WHERE item_id=?",
                (self.item,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(review), ("claude", "approved"))
        self.assertEqual(observed["next_action"]["kind"], "complete_task")

        trace_events = coop_turn_trace.read_events(self.trace_path)
        provider_result = next(
            event for event in trace_events
            if event["event"] == "provider_result_received"
        )
        self.assertEqual(
            {
                field: provider_result["details"][field]
                for field in (
                    "uncached_input_tokens",
                    "cache_write_input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                )
            },
            {
                "uncached_input_tokens": 90,
                "cache_write_input_tokens": 15,
                "cached_input_tokens": 30,
                "output_tokens": 8,
            },
        )
        final_modes = [
            event["details"]["execution_mode"]
            for event in trace_events
            if event["event"] == "final_classification"
        ]
        self.assertEqual(final_modes, [
            "compiled_mesh_report",
            "compiled_mesh_review_request",
            "isolated_structured_mesh_review",
        ])

    def test_token_efficient_handoff_decision_postcommits_canonical_choice(self):
        handoff_id = self._seed_pending_handoff()
        observed = {}
        usage = {
            "model_id": "claude-opus-5",
            "input_tokens": 500,
            "uncached_input_tokens": 80,
            "cached_input_tokens": 400,
            "cache_write_input_tokens": 20,
            "output_tokens": 10,
            "total_tokens": 510,
            "model_calls": 1,
            "usage_observation": "complete",
        }

        def run(_participants, **kwargs):
            action = kwargs["actionable_fn"]("claude")
            self.assertEqual(action["kind"], "respond_handoff")
            candidate = coop_action_scheduler.ActionCandidate(
                agent="claude", hint=action["kind"], action=action)
            profile = kwargs["profile_fn"](candidate)
            observed["profile"] = profile
            observed["result"] = kwargs["take_turn"](
                "claude", action["kind"], profile)
            return "stopped", 1, []

        def decide(**kwargs):
            req = kwargs["request"]
            observed["request"] = req
            kwargs["usage_callback"](usage)
            return coop_decisions.DecisionResult(
                value={
                    "handoff_id": handoff_id,
                    "response": "accept",
                    "reason": "",
                },
                usage=usage,
            )

        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_decision",
                 side_effect=decide) as structured, \
             mock.patch.object(
                 coop_start,
                 "invoke_turn") as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--token-efficient",
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(
            (
                observed["profile"].workspace_surface,
                observed["profile"].execution_mode,
            ),
            ("none", "isolated_structured_decision"),
        )
        self.assertEqual(
            observed["request"].decision_kind,
            "respond_handoff",
        )
        self.assertEqual(
            observed["request"].action_fingerprint,
            observed["profile"].action_fingerprint,
        )
        self.assertEqual(
            observed["result"]["note"],
            "structured_decision_postcommit",
        )
        structured.assert_called_once()
        tool_turn.assert_not_called()
        conn = coopdb.connect(self.board, require_current=True)
        try:
            handoff = conn.execute(
                "SELECT status FROM handoffs WHERE handoff_id=?",
                (handoff_id,),
            ).fetchone()
            item = coopdb.item_show(conn, self.item)
        finally:
            conn.close()
        self.assertEqual(handoff["status"], "accepted")
        self.assertEqual(item["status"], "working")
        self.assertEqual(item["owner_agent_id"], "claude")
        provider_result = next(
            event
            for event in coop_turn_trace.read_events(self.trace_path)
            if event["event"] == "provider_result_received"
        )
        self.assertEqual(
            provider_result["details"]["execution_mode"],
            "isolated_structured_decision",
        )
        self.assertEqual(
            provider_result["details"]["uncached_input_tokens"],
            80,
        )
        self.assertNotIn("accept", repr(provider_result))

    def test_stale_handoff_decision_defers_without_postcommit_or_tool_turn(self):
        handoff_id = self._seed_pending_handoff()
        observed = {}

        def run(_participants, **kwargs):
            action = kwargs["actionable_fn"]("claude")
            candidate = coop_action_scheduler.ActionCandidate(
                agent="claude", hint=action["kind"], action=action)
            profile = kwargs["profile_fn"](candidate)
            observed["result"] = kwargs["take_turn"](
                "claude", action["kind"], profile)
            observed["retry_profile"] = kwargs["profile_fn"](candidate)
            return "stopped", 1, []

        decision = coop_decisions.DecisionResult(
            value={
                "handoff_id": handoff_id,
                "response": "accept",
                "reason": "",
            },
            usage={"usage_observation": "unobserved"},
        )
        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start,
                 "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_start,
                 "invoke_structured_decision",
                 return_value=decision), \
             mock.patch.object(
                 coop_autonomous,
                 "structured_action_still_current",
                 return_value=False), \
             mock.patch.object(
                 coop_autonomous.subprocess,
                 "run") as postcommit, \
             mock.patch.object(
                 coop_start,
                 "invoke_turn") as tool_turn, \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 side_effect=run), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path,
                 "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db",
                self.board,
                "--item",
                str(self.item),
                "--token-efficient",
                "--no-persistent-workers",
            ])

        self.assertEqual(code, 0)
        postcommit.assert_not_called()
        tool_turn.assert_not_called()
        self.assertEqual(
            observed["result"]["classification"],
            "structured_decision_deferred_fallback",
        )
        self.assertEqual(
            (
                observed["retry_profile"].workspace_surface,
                observed["retry_profile"].execution_mode,
            ),
            ("read", "tool_turn"),
        )
        conn = coopdb.connect(self.board, require_current=True)
        try:
            status = conn.execute(
                "SELECT status FROM handoffs WHERE handoff_id=?",
                (handoff_id,),
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(status, "pending")


if __name__ == "__main__":
    unittest.main()
