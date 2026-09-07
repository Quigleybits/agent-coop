"""Substantive checkpoints and the guarded blocked transition.

Checkpoints are where the agent reasons about protocol state: every type
updates freshness and returns the compact packet plus the consumed unread
inbox range, atomically. `blocked` is also the guarded working→blocked
transition. `find_overdue` is the supervisor's checkpoint-timeout feed.
"""

import io
import contextlib
import json
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop.coop_errors import (
    InvalidTransition,
    SessionMismatch,
    StaleClaim,
)
from tests.test_claims import ClaimBoard, LEASE

CHECKPOINT_LIMIT = 900  # the 15-minute default, injected per call


class CheckpointBoard(ClaimBoard):
    def checkpoint(self, ctype, claim_id, sid, agent, note=None):
        return coopdb.checkpoint(
            self.conn, ctype=ctype, claim_id=claim_id, actor=agent,
            session_id=sid, note=note)

    def claim_long(self, item, agent, sid):
        """A claim whose lease outlives the overdue horizons under test —
        checkpoint staleness and lease expiry are separate axes."""
        return coopdb.claim_item(
            self.conn, item_id=item, actor=agent, session_id=sid,
            intent="work", lease_seconds=3600)

    def claim_row(self, claim_id):
        return self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()

    def question_response_claim(self):
        """The organic needs-input flow: alice's implementation claim goes to
        needs-input addressed to bob; bob claims the question lane.
        Freshness is kind-agnostic, `blocked` is not."""
        item = self.make_item()
        self.make_session("alice", sid="s-alice")
        self.make_session("bob", sid="s-bob")
        claim = self.claim(item, "alice", "s-alice")
        qid = coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"], session_id="s-alice",
            to_agent="bob", question="which db?")
        result = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob",
            intent="answering")
        return result["claim_id"], "s-bob"


class Freshness(CheckpointBoard):
    def test_claim_creation_is_the_initial_start_checkpoint(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        row = self.claim_row(claim["claim_id"])
        self.assertEqual(row["last_checkpoint_at"], row["claimed_at"])

    def test_each_type_updates_freshness(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        for ctype in ("start", "step", "risky", "predone"):
            with self.subTest(ctype=ctype):
                before = self.claim_row(claim["claim_id"])["last_checkpoint_at"]
                self.clock.advance(5)  # 20s total: within the 30s lease
                self.checkpoint(ctype, claim["claim_id"], sid, "alice")
                after = self.claim_row(claim["claim_id"])["last_checkpoint_at"]
                self.assertGreater(after, before)

    def test_unknown_type_is_refused(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        with self.assertRaises(InvalidTransition):
            self.checkpoint("victory", claim["claim_id"], sid, "alice")

    def test_note_is_recorded_in_the_event_payload(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        self.checkpoint("step", claim["claim_id"], sid, "alice",
                        note="halfway there")
        event = self.events_of("checkpoint")[-1]
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["type"], "step")
        self.assertEqual(payload["note"], "halfway there")

    def test_checkpoint_requires_the_owning_live_session(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        bob = self.make_session("bob")
        with self.assertRaises(SessionMismatch):
            self.checkpoint("step", claim["claim_id"], bob, "bob")

    def test_freshness_is_kind_agnostic(self):
        qclaim, sid = self.question_response_claim()
        before = self.claim_row(qclaim)["last_checkpoint_at"]
        self.clock.advance(5)
        self.checkpoint("step", qclaim, sid, "bob")
        self.assertGreater(
            self.claim_row(qclaim)["last_checkpoint_at"], before)


class PacketAndInbox(CheckpointBoard):
    def test_checkpoint_returns_packet_and_consumes_inbox(self):
        item = self.make_item(owner="alice")  # seeds one assignment delivery
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        result = self.checkpoint("step", claim["claim_id"], sid, "alice")
        self.assertEqual(result["packet"]["item_id"], item)
        self.assertEqual(result["packet"]["claim"]["claim_id"],
                         claim["claim_id"])
        categories = [e["category"] for e in result["inbox"]]
        self.assertIn("assignment", categories)
        again = self.checkpoint("step", claim["claim_id"], sid, "alice")
        self.assertEqual(again["inbox"], [])  # consumed exactly once

    def test_checkpoint_consume_rolls_back_with_the_transaction(self):
        item = self.make_item(owner="alice")
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        real_append = coopdb.append_event

        def sabotage(conn, **kw):
            if kw.get("event_type") == "checkpoint":
                raise RuntimeError("forced failure after the consume")
            return real_append(conn, **kw)

        offset_before = self.conn.execute(
            "SELECT COALESCE((SELECT last_consumed_entry_id FROM "
            "inbox_offsets WHERE agent_id='alice'),0) AS o").fetchone()["o"]
        with unittest.mock.patch.object(coopdb, "append_event", sabotage):
            with self.assertRaises(RuntimeError):
                self.checkpoint("step", claim["claim_id"], sid, "alice")
        offset_after = self.conn.execute(
            "SELECT COALESCE((SELECT last_consumed_entry_id FROM "
            "inbox_offsets WHERE agent_id='alice'),0) AS o").fetchone()["o"]
        self.assertEqual(offset_before, offset_after)
        result = self.checkpoint("step", claim["claim_id"], sid, "alice")
        self.assertNotEqual(result["inbox"], [])  # nothing was lost


class BlockedTransition(CheckpointBoard):
    def test_blocked_requires_an_exact_stop_condition_note(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        for empty in (None, "", "   "):
            with self.subTest(note=empty):
                with self.assertRaises(InvalidTransition):
                    self.checkpoint(
                        "blocked", claim["claim_id"], sid, "alice",
                        note=empty)

    def test_blocked_is_implementation_only(self):
        qclaim, sid = self.question_response_claim()
        with self.assertRaises(InvalidTransition):
            self.checkpoint("blocked", qclaim, sid, "bob",
                            note="stop condition hit")

    def test_blocked_ladder_is_atomic_and_exact(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        self.checkpoint("blocked", claim["claim_id"], sid, "alice",
                        note="stop condition: migration touches prod")
        crow = self.claim_row(claim["claim_id"])
        self.assertEqual(crow["status"], "closed")
        self.assertEqual(crow["close_reason"], "blocked")
        irow = self.conn.execute(
            "SELECT status, owner_agent_id, next_actor_agent_id FROM items "
            "WHERE id=?", (item,)).fetchone()
        self.assertEqual(irow["status"], "blocked")
        self.assertEqual(irow["owner_agent_id"], "alice")  # preserved
        self.assertIsNone(irow["next_actor_agent_id"])  # cleared
        self.assertEqual(len(self.events_of("item_blocked")), 1)
        # The closed claim is beyond every lease: renewal finds nothing.
        self.assertEqual(coopdb.renew_claims(self.conn, session_id=sid), 0)

    def test_blocked_then_resume_round_trip(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        self.checkpoint("blocked", claim["claim_id"], sid, "alice",
                        note="blocked on missing credentials")
        with self.assertRaises(InvalidTransition):
            self.claim(item, "alice", sid)  # blocked resume needs a reason
        second = self.claim(item, "alice", sid,
                            reclaim_reason="credentials arrived")
        irow = self.conn.execute(
            "SELECT status, owner_agent_id FROM items WHERE id=?",
            (item,)).fetchone()
        self.assertEqual(irow["status"], "working")
        self.assertEqual(irow["owner_agent_id"], "alice")
        self.assertNotEqual(second["claim_id"], claim["claim_id"])
        with self.assertRaises(StaleClaim):
            self.checkpoint("step", claim["claim_id"], sid, "alice")


class OverdueDetection(CheckpointBoard):
    def test_find_overdue_with_the_injected_clock(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim_long(item, "alice", sid)
        self.clock.advance(CHECKPOINT_LIMIT - 1)
        self.assertEqual(coopdb.find_overdue(
            self.conn, session_id=sid,
            checkpoint_limit_seconds=CHECKPOINT_LIMIT), [])
        self.clock.advance(1)  # exactly at the limit: overdue
        overdue = coopdb.find_overdue(
            self.conn, session_id=sid,
            checkpoint_limit_seconds=CHECKPOINT_LIMIT)
        self.assertEqual([o["claim_id"] for o in overdue],
                         [claim["claim_id"]])

    def test_checkpoint_resets_the_overdue_horizon(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim_long(item, "alice", sid)
        self.clock.advance(CHECKPOINT_LIMIT - 10)
        self.checkpoint("step", claim["claim_id"], sid, "alice")
        self.clock.advance(11)
        self.assertEqual(coopdb.find_overdue(
            self.conn, session_id=sid,
            checkpoint_limit_seconds=CHECKPOINT_LIMIT), [])

    def test_find_overdue_scopes_to_session_and_active_claims(self):
        item1, item2 = self.make_item(), self.make_item(title="T2")
        alice = self.make_session("alice")
        bob = self.make_session("bob")
        claim1 = self.claim(item1, "alice", alice)
        self.claim_long(item2, "bob", bob)
        self.checkpoint("blocked", claim1["claim_id"], alice, "alice",
                        note="stop: waiting on review")  # closed, never overdue
        self.clock.advance(CHECKPOINT_LIMIT + 1)
        self.assertEqual(coopdb.find_overdue(
            self.conn, session_id=alice,
            checkpoint_limit_seconds=CHECKPOINT_LIMIT), [])
        overdue_bob = coopdb.find_overdue(
            self.conn, session_id=bob,
            checkpoint_limit_seconds=CHECKPOINT_LIMIT)
        self.assertEqual(len(overdue_bob), 1)


class CliCheckpoint(CheckpointBoard):
    def _run(self, argv, env):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"COOP_DB": self.db, **env}
        code = 0
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                try:
                    coopcli.main(argv)
                except SystemExit as exc:
                    if isinstance(exc.code, int):
                        code = exc.code
                    else:
                        code = 1
                        if exc.code:
                            stderr.write(str(exc.code))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_checkpoint_returns_packet_json_tokenless(self):
        item = self.make_item(owner="alice")
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}
        code, out, err = self._run(
            ["--json", "checkpoint", "step",
             "--claim", str(claim["claim_id"]), "--note", "cli step"], env)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["packet"]["item_id"], item)
        self.assertNotIn("fencing_token", out)

    def test_cli_blocked_carries_the_note_as_the_reason(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}
        code, out, err = self._run(
            ["checkpoint", "blocked", "--claim", str(claim["claim_id"]),
             "--note", "stop condition: prod migration"], env)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.claim_row(claim["claim_id"])["status"],
                         "closed")

    def test_cli_rejects_an_unknown_type_as_usage(self):
        code, out, err = self._run(
            ["checkpoint", "victory", "--claim", "1"], {})
        self.assertEqual(code, 2)  # argparse choices refuse it


if __name__ == "__main__":
    unittest.main()
