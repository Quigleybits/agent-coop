"""Product-mode rules: kickoff+end human, peer Q&A, agent recovery, mid-run human gate."""

from __future__ import annotations

import unittest
from unittest import mock

from agent_coop import cli as coopcli
from agent_coop import coop_autonomous
from agent_coop import coop_watcher
from agent_coop import coopdb
from agent_coop.coop_errors import InvalidTransition, UnsafeReclaim
from tests.test_claims import ClaimBoard


class PeerOnlyNeedsInput(ClaimBoard):
    def test_needs_input_refuses_human_target(self):
        item = self.make_item()
        sid = self.make_session("alice", sid="s-a")
        claim = self.claim(item, "alice", sid)
        with self.assertRaisesRegex(InvalidTransition, "peer agent"):
            coopdb.needs_input(
                self.conn, claim_id=claim["claim_id"], session_id=sid,
                to_agent="human", question="blocked on human?")

    def test_needs_input_peer_ok(self):
        item = self.make_item()
        sid = self.make_session("alice", sid="s-a2")
        self.make_session("bob", sid="s-b2")
        claim = self.claim(item, "alice", sid)
        qid = coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"], session_id=sid,
            to_agent="bob", question="which path?")
        self.assertIsInstance(qid, int)


class AgentWedgeRecovery(ClaimBoard):
    def test_peer_releases_abandoned_stale_claim(self):
        item = self.make_item()
        # Owner session with last_seen in the past (abandonable).
        owner_sid = self.make_session("alice", sid="s-owner")
        claim = self.claim(item, "alice", owner_sid)
        # Expire the claim lease.
        self.conn.execute(
            "UPDATE claims SET lease_expires_at=? WHERE claim_id=?",
            ("2000-01-01T00:00:00", claim["claim_id"]))
        # Backdate session last_seen so abandon horizon is satisfied.
        self.conn.execute(
            "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
            ("2000-01-01T00:00:00", owner_sid))
        self.conn.commit()
        peer = self.make_session("bob", sid="s-peer")
        result = coopdb.agent_release_wedge(
            self.conn, claim_id=claim["claim_id"], session_id=peer,
            reason="peer abandoned; unwedge", abandon_after_s=60)
        self.assertEqual(result["released"], claim["claim_id"])
        row = self.conn.execute(
            "SELECT status FROM claims WHERE claim_id=?",
            (claim["claim_id"],)).fetchone()
        self.assertEqual(row["status"], "released")
        sess = self.conn.execute(
            "SELECT status, termination_reason FROM sessions "
            "WHERE session_id=?", (owner_sid,)).fetchone()
        self.assertEqual(sess["status"], "exited")
        self.assertEqual(sess["termination_reason"], "abandoned")

    def test_fresh_running_session_still_refuses(self):
        item = self.make_item()
        owner_sid = self.make_session("alice", sid="s-own2")
        claim = self.claim(item, "alice", owner_sid)
        self.conn.execute(
            "UPDATE claims SET lease_expires_at=? WHERE claim_id=?",
            ("2000-01-01T00:00:00", claim["claim_id"]))
        self.conn.commit()
        # last_seen is recent (session insert uses now) — refuse.
        peer = self.make_session("bob", sid="s-peer2")
        with self.assertRaises(UnsafeReclaim):
            coopdb.agent_release_wedge(
                self.conn, claim_id=claim["claim_id"], session_id=peer,
                reason="too soon", abandon_after_s=3600)


class MidRunHumanGate(ClaimBoard):
    def test_human_events_after_start_are_counted(self):
        self.make_session("alice", sid="s-audit-a")
        mark = coopdb.mark_autonomous_run(
            self.conn, phase="started", agent_id="alice",
            session_id="s-audit-a", payload={"t": 1})
        # Human mutation after start (illegal mid-run).
        self.make_item(title="mid-run-illegal")
        report = coopdb.mid_run_human_mutations(
            self.conn, after_event_id=mark["event_id"])
        self.assertGreaterEqual(report["count"], 1)

    def test_pre_start_human_not_counted(self):
        # Human creates item before mark — kickoff.
        item = self.make_item(title="seeded-at-kickoff")
        self.make_session("alice", sid="s-audit-b")
        mark = coopdb.mark_autonomous_run(
            self.conn, phase="started", agent_id="alice",
            session_id="s-audit-b", payload={"t": 2})
        report = coopdb.mid_run_human_mutations(
            self.conn, after_event_id=mark["event_id"])
        self.assertEqual(report["count"], 0)
        self.assertIsNotNone(item)


class RenewTouchesLastSeen(ClaimBoard):
    def test_renew_claims_updates_session_last_seen(self):
        item = self.make_item()
        sid = self.make_session("alice", sid="s-ls")
        self.claim(item, "alice", sid)
        self.conn.execute(
            "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
            ("2000-01-01T00:00:00", sid))
        coopdb.renew_claims(self.conn, session_id=sid, lease_seconds=3600)
        row = self.conn.execute(
            "SELECT last_seen_at FROM sessions WHERE session_id=?",
            (sid,)).fetchone()
        self.assertNotEqual(row["last_seen_at"], "2000-01-01T00:00:00")


class CanonicalProductRunner(unittest.TestCase):
    def test_coop_start_delegates_to_wake_driven_runner(self):
        args = coopcli.build_parser().parse_args([
            "start", "--board", "C:/work/board.db", "--item", "24",
            "--agents", "claude,codex,grok", "--interval", "1.5",
            "--timeout-seconds", "90", "--max-turns", "12",
            "--max-idle-rounds", "8", "--max-noop-cycles", "2",
            "--persistent-provider", "codex",
            "--persistent-provider", "grok",
            "--dry-run",
        ])
        with mock.patch.object(coop_autonomous, "main", return_value=0) as run:
            coopcli.cmd_start(None, args)
        run.assert_called_once_with([
            "--db", "C:/work/board.db", "--item", "24",
            "--agents", "claude,codex,grok",
            "--interval", "1.5", "--timeout", "90.0",
            "--max-turns", "12", "--max-idle-rounds", "8",
            "--max-noop-cycles", "2",
            "--persistent-provider", "codex",
            "--persistent-provider", "grok",
            "--dry-run",
        ])

    def test_coop_start_propagates_product_gate_exit_code(self):
        args = coopcli.build_parser().parse_args([
            "start", "--board", "C:/work/board.db", "--all",
        ])
        with mock.patch.object(coop_autonomous, "main", return_value=4):
            with self.assertRaisesRegex(SystemExit, "4"):
                coopcli.cmd_start(None, args)

    def test_coop_start_forwards_token_efficient_opt_in(self):
        args = coopcli.build_parser().parse_args([
            "start", "--board", "C:/work/board.db", "--item", "24",
            "--token-efficient",
        ])
        with mock.patch.object(coop_autonomous, "main", return_value=0) as run:
            coopcli.cmd_start(None, args)

        forwarded = run.call_args.args[0]
        self.assertEqual(forwarded.count("--token-efficient"), 1)


class FullWatcherRead(unittest.TestCase):
    def test_next_action_returns_full_envelope_and_closes_board(self):
        conn = mock.Mock()
        action = {
            "kind": "claim_task",
            "item_id": 25,
            "target_id": 25,
            "command": [*coopdb.CLI_ARGV, "item", "claim", "25"],
        }
        with mock.patch.object(
                coopdb, "connect", return_value=conn), \
             mock.patch.object(
                 coopdb, "now", return_value="2026-07-23T20:00:00+00:00"), \
             mock.patch.object(
                 coopdb, "_derive_next_action",
                 return_value=(action, [])) as derive:
            result = coop_watcher.next_action(
                "board.db", "codex", lease_seconds=777, item_id=25)

        self.assertIs(result, action)
        derive.assert_called_once_with(
            conn,
            "codex",
            "2026-07-23T20:00:00+00:00",
            lease_seconds=777,
            item_id=25,
        )
        conn.close.assert_called_once_with()

    def test_public_actionable_hint_remains_kind_only(self):
        action = {
            "kind": "review_task",
            "item_id": 25,
            "command": ["sensitive", "board", "content"],
        }
        with mock.patch.object(
                coop_watcher, "next_action", return_value=action) as read:
            hint = coop_watcher.actionable(
                "board.db", "codex", lease_seconds=321, item_id=25)

        self.assertEqual(hint, "review_task")
        self.assertNotIn("sensitive", hint)
        read.assert_called_once_with(
            "board.db",
            "codex",
            lease_seconds=321,
            item_id=25,
        )


if __name__ == "__main__":
    unittest.main()
