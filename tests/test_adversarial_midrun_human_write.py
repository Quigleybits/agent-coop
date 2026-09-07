"""Adversarial trial — failure class: mid-run human write.

The detector unit (``coopdb.mid_run_human_mutations``) is covered in
tests/test_product_mode.py. These trials prove the RUNNER gate itself:
a genuine human-lane board write landing between ``run_started`` and the
terminal marker of a real ``coop_autonomous.main`` run flips the final
reason to ``human_mutation_gate`` on every protocol surface — exit code 4,
``GATE FAIL`` stdout, status sidecar, turn trace, and the
``autonomous_run_finished`` board event — masking the underlying stop
reason. The control arm proves a kickoff-only human seed (pre-start)
leaves the underlying reason untouched.

Chassis: the AutonomousRunnerSidecar shape from tests/test_runner_status.py
(real board.db + ``coop_autonomous.main`` with mocked participants,
watcher action, and ``invoke_turn``).
"""

import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from agent_coop import coop_autonomous
from agent_coop import coop_runner_status as runner_status
from agent_coop import coop_start
from agent_coop import coop_turn_trace
from agent_coop import coop_watcher
from agent_coop import coopdb


class MidRunHumanWriteGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.board = str(root / "board.db")
        self.status_path = root / "run.status.json"
        self.trace_path = root / "run.trace.jsonl"
        conn = coopdb.connect(self.board)
        coopdb.init_db(conn)
        # Pre-start human seed: the legal kickoff lane. It must never trip
        # the mid-run gate (control arm) — only writes AFTER run_started do.
        self.item = coopdb.create_item(
            conn, actor="human", session_id=None,
            title="target", objective="target")
        conn.close()

    def _claim_action(self):
        return {
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

    def _provider_result(self):
        # Non-retryable classification: ends the run after one turn without
        # needing board completion, so the gate's override is observable.
        return {
            "agent": "claude",
            "provider": "claude",
            "ok": False,
            "exit": 1,
            "tree_empty": True,
            "note": "provider_quota_exhausted",
            "classification": "provider_quota_exhausted",
            "retryable": False,
        }

    def _run_main(self, invoke_side_effect):
        trace_events = []
        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: (
            trace_events.append({"event": event, **fields}) or True)
        stdout = io.StringIO()
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
                 coop_watcher, "next_action",
                 return_value=self._claim_action()), \
             mock.patch.object(
                 coop_start, "invoke_turn",
                 side_effect=invoke_side_effect) as invoke_turn, \
             mock.patch.object(
                 coop_autonomous.pathlib.Path, "exists",
                 return_value=False), \
             contextlib.redirect_stdout(stdout):
            code = coop_autonomous.main([
                "--db", self.board, "--item", str(self.item),
                "--max-turns", "5",
            ])
        # Both arms take exactly one provider turn before the terminal.
        self.assertEqual(invoke_turn.call_count, 1)
        return code, stdout.getvalue(), trace_events

    def _finished_payload(self):
        conn = coopdb.connect(self.board, require_current=True)
        try:
            row = conn.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='autonomous_run_finished' "
                "ORDER BY event_id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return json.loads(row["payload_json"])

    def test_midrun_human_write_flips_reason_on_every_surface(self):
        def invoke(**kwargs):
            del kwargs
            # A genuine human-lane board mutation between run_started and
            # the terminal — the adversarial move under trial.
            conn = coopdb.connect(self.board, require_current=True)
            try:
                coopdb.create_item(
                    conn, actor="human", session_id=None,
                    title="mid-run interference",
                    objective="illegal human board write during a run")
            finally:
                conn.close()
            return self._provider_result()

        code, out, trace_events = self._run_main(invoke)

        # Protocol outcome 1: the product-gate exit code (4), not the
        # generic failure exit (3) the underlying reason would produce.
        self.assertEqual(code, 4)
        # Protocol outcome 2: the operator-visible GATE FAIL naming the
        # flagged human event (only events after run_started are flagged,
        # so this also proves ordering).
        self.assertIn("GATE FAIL: mid-run human mutations: 1", out)
        self.assertIn("human event item_created", out)
        # Protocol outcome 3: the status sidecar reflects the failed gate.
        status = runner_status.read_status(self.status_path)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("failed", "human_mutation_gate", 1),
        )
        # Protocol outcome 4: the turn trace classifies the run as gated.
        self.assertEqual(trace_events[-1]["event"], "run_finished")
        self.assertEqual(
            trace_events[-1]["details"]["classification"],
            "human_mutation_gate",
        )
        # Protocol outcome 5: the immutable board record carries the gate
        # reason, masking the underlying provider stop reason.
        self.assertEqual(
            self._finished_payload()["reason"], "human_mutation_gate")

    def test_pre_start_human_seed_alone_keeps_underlying_reason(self):
        def invoke(**kwargs):
            del kwargs
            return self._provider_result()

        code, out, trace_events = self._run_main(invoke)

        # Control arm: the kickoff seed (setUp) predates run_started, so
        # the gate stays silent and the underlying reason survives on
        # every surface.
        self.assertEqual(code, 3)
        self.assertNotIn("GATE FAIL", out)
        status = runner_status.read_status(self.status_path)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("failed", "provider_quota_exhausted", 1),
        )
        self.assertEqual(trace_events[-1]["event"], "run_finished")
        self.assertEqual(
            trace_events[-1]["details"]["classification"],
            "provider_quota_exhausted",
        )
        self.assertEqual(
            self._finished_payload()["reason"], "provider_quota_exhausted")


if __name__ == "__main__":
    unittest.main()
