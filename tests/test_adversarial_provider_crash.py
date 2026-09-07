"""Adversarial trial — failure class: provider crash mid-turn.

Scripted trial for the "Provider crash mid-turn" gate class: a provider
process dies while HOLDING a
live implementation claim. Expected behaviour is "typed cleanup; board
consistent; optional resume" — stitched here into one trial across the
three legs prior tests only covered piecewise:

* the run terminates with the typed classification in the status sidecar,
  the run trace, and the ``autonomous_run_finished`` board event (no fake
  ``all_done``);
* the crashed agent's session row is finished exactly once (terminal);
* the abandoned claim fails closed but stays recoverable: renewal by the
  dead session is refused, a reason-less peer claim is refused, and a peer
  reclaim WITH a reason transfers ownership;
* the item never reaches ``done`` and no ``item_completed`` event exists.

The harness mirrors AutonomousRunnerSidecar (tests/test_runner_status.py):
the real ``coop_autonomous.main`` runs against a real board with only the
provider transport (``coop_start.invoke_turn``) and routing
(``coop_watcher.next_action``) faked. The faked turn 1 performs a REAL
``claim_item`` as claude before returning a crash-shaped retryable result;
turn 2 crashes non-retryably, producing the typed terminal stop.
"""

import json
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
from agent_coop.coop_errors import InvalidTransition


class ProviderCrashMidTurn(unittest.TestCase):
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
            title="crash target",
            objective="survive a mid-turn provider crash",
            scope="bounded adversarial trial",
            done_when="never during this trial",
            output_contract="no deliverable; the crash is the subject",
            context="adversarial trial, provider-crash class",
            allowed_actions=["read"],
            stop_conditions=["stop on doubt"])
        conn.close()

    def test_crash_while_holding_claim_types_cleanup_and_recovers(self):
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

        def route(_board, agent, lease_seconds=None, item_id=None):
            del lease_seconds, item_id
            if agent == "claude":
                return dict(action)
            return {"kind": "idle"}

        observed = {"invokes": []}

        def invoke(**kwargs):
            observed["invokes"].append(kwargs["agent_id"])
            crash = {
                "agent": kwargs["agent_id"],
                "provider": kwargs["provider"],
                "ok": False,
                "exit": 137,
                "tree_empty": True,
                "note": "worker_protocol_failed",
                "classification": "worker_protocol_failed",
            }
            if len(observed["invokes"]) == 1:
                # The provider does real board work — acquires the
                # implementation claim — and THEN its process dies. The
                # first crash is restartable (retryable), so the runner
                # keeps going instead of stopping on one bad turn.
                conn = coopdb.connect(self.board, require_current=True)
                try:
                    observed["claim"] = coopdb.claim_item(
                        conn, item_id=self.item, actor="claude",
                        session_id=kwargs["session_id"],
                        intent="implement the crash target",
                        lease_seconds=3600)
                    observed["crashed_session"] = kwargs["session_id"]
                finally:
                    conn.close()
                crash["retryable"] = True
            else:
                # The restarted worker crashes again, this time terminally:
                # the run must stop with the typed classification.
                crash["retryable"] = False
            return crash

        with mock.patch.dict(
                "os.environ",
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
                 coop_watcher, "next_action", side_effect=route), \
             mock.patch.object(
                 coop_start, "invoke_turn", side_effect=invoke), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path, "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db", self.board,
                "--item", str(self.item),
                "--max-turns", "6",
                "--no-persistent-workers",
                "--no-mechanical-precommit",
            ])

        # Leg 1 — typed classification, everywhere it must appear.
        self.assertEqual(code, 3)
        self.assertEqual(observed["invokes"], ["claude", "claude"])
        status = runner_status.read_status(self.status_path)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("failed", "worker_protocol_failed", 2),
        )
        self.assertEqual(trace_events[-1]["event"], "run_finished")
        self.assertEqual(
            trace_events[-1]["details"]["classification"],
            "worker_protocol_failed",
        )

        conn = coopdb.connect(self.board, require_current=True)
        self.addCleanup(conn.close)
        finished = conn.execute(
            "SELECT payload_json FROM events "
            "WHERE event_type='autonomous_run_finished' "
            "ORDER BY event_id DESC LIMIT 1").fetchone()
        payload = json.loads(finished["payload_json"])
        self.assertEqual(payload["reason"], "worker_protocol_failed")
        self.assertEqual(payload["turns"], 2)

        # Leg 2 — typed cleanup: the crashed agent's session is terminal.
        crashed_sid = observed["crashed_session"]
        sess = conn.execute(
            "SELECT status, termination_reason FROM sessions "
            "WHERE session_id=?", (crashed_sid,)).fetchone()
        self.assertEqual(sess["status"], "exited")
        self.assertEqual(sess["termination_reason"], "child_exit")

        # Leg 3 — board consistent: the crash left the claim held and the
        # item mid-flight, never done.
        claim_id = observed["claim"]["claim_id"]
        self.assertEqual(
            conn.execute(
                "SELECT status FROM claims WHERE claim_id=?",
                (claim_id,)).fetchone()["status"],
            "active")
        item_row = conn.execute(
            "SELECT status, owner_agent_id FROM items WHERE id=?",
            (self.item,)).fetchone()
        self.assertEqual(
            (item_row["status"], item_row["owner_agent_id"]),
            ("working", "claude"))

        # The dead session cannot extend its own lease.
        self.assertEqual(
            coopdb.renew_claims(conn, session_id=crashed_sid), 0)

        # Leg 4 — recover path: a peer cannot silently seize the lane...
        peer_sid = "recovery-codex-session"
        coopdb.insert_session(
            conn, session_id=peer_sid, agent_id="codex", provider="codex",
            command=["codex"], cwd=".", max_runtime_s=3600, grace_s=10)
        with self.assertRaises(InvalidTransition) as refused:
            coopdb.claim_item(
                conn, item_id=self.item, actor="codex",
                session_id=peer_sid, intent="recover the crashed lane")
        self.assertEqual(
            refused.exception.evidence["constraint"],
            "reclaim_reason_required")

        # ...but a reasoned reclaim transfers ownership. No lease/last_seen
        # backdating is needed: the runner's finish_session recorded the
        # predecessor's terminal exit, which is exactly the evidence the
        # stale-flip branch demands.
        reclaim = coopdb.claim_item(
            conn, item_id=self.item, actor="codex", session_id=peer_sid,
            intent="recover the crashed lane",
            reclaim_reason="owner session crashed mid-turn; exit confirmed")
        self.assertNotEqual(reclaim["claim_id"], claim_id)
        self.assertEqual(
            conn.execute(
                "SELECT status FROM claims WHERE claim_id=?",
                (claim_id,)).fetchone()["status"],
            "stale")
        after = conn.execute(
            "SELECT status, owner_agent_id FROM items WHERE id=?",
            (self.item,)).fetchone()
        self.assertEqual(
            (after["status"], after["owner_agent_id"]),
            ("working", "codex"))

        # Leg 5 — no false done, at any point in the trial.
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) AS c FROM events "
                "WHERE event_type='item_completed'").fetchone()["c"],
            0)


if __name__ == "__main__":
    unittest.main()
