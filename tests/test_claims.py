"""Fenced claim lanes with leases and controlled reclaim.

The concurrency suite is the heart: real two-connection races behind a
threading.Barrier, loser zero-effect proven by full-table snapshots, the
exact-expiry boundary where renewal loses in BOTH serialization orders, and
a cumulative superseded-claim matrix that later lanes extend.
"""

import datetime
import io
import contextlib
import threading
import unittest
import unittest.mock
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop.coop_errors import (
    ClaimCollision,
    HumanLaneViolation,
    IncompleteContract,
    InvalidTransition,
    NotFound,
    SessionMismatch,
    StaleClaim,
    UnsafeReclaim,
)

SNAPSHOT_TABLES = (
    "items", "claims", "assignments", "events", "inbox_entries",
    "inbox_offsets",
)

LEASE = 30


class Clock:
    def __init__(self, start="2026-07-17T10:00:00+00:00"):
        self.t = datetime.datetime.fromisoformat(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += datetime.timedelta(seconds=seconds)


def contract_kwargs(**over):
    base = dict(
        title="T", objective="O", scope="S", done_when="D",
        output_contract="OC", context="C",
        allowed_actions=["read"], stop_conditions=["stop on doubt"],
    )
    base.update(over)
    return base


def snapshot(conn):
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
        for t in SNAPSHOT_TABLES
    }


class ClaimBoard(unittest.TestCase):
    """Fixture: fresh v4 board, frozen injected clock, real sessions."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "board.db")
        self.clock = Clock()
        self._orig_clock = coopdb._clock
        coopdb._clock = self.clock
        self.addCleanup(self._restore_clock)
        self.conn = coopdb.connect(self.db)
        self.addCleanup(self.conn.close)
        coopdb.init_db(self.conn)

    def _restore_clock(self):
        coopdb._clock = self._orig_clock

    def make_item(self, **over):
        return coopdb.create_item(
            self.conn, actor="human", session_id=None,
            **contract_kwargs(**over))

    def make_session(self, agent, sid=None, provider="claude"):
        sid = sid or uuid.uuid4().hex
        coopdb.insert_session(
            self.conn, session_id=sid, agent_id=agent, provider=provider,
            command=["python", "-c", "pass"], cwd=".",
            max_runtime_s=28800, grace_s=10)
        return sid

    def claim(self, item_id, agent, sid, conn=None, intent="work",
              reclaim_reason=None):
        return coopdb.claim_item(
            conn or self.conn, item_id=item_id, actor=agent, session_id=sid,
            intent=intent, reclaim_reason=reclaim_reason)

    def lane_rows(self, item_id):
        return self.conn.execute(
            "SELECT * FROM claims WHERE lane_key=? ORDER BY claim_id",
            (f"implementation:item:{item_id}",)).fetchall()

    def events_of(self, event_type):
        return self.conn.execute(
            "SELECT * FROM events WHERE event_type=? ORDER BY event_id",
            (event_type,)).fetchall()


class GuardLadder(ClaimBoard):
    def test_fresh_todo_claim_acquires_ownership_and_moves_to_working(self):
        item = self.make_item()
        sid = self.make_session("alice")
        result = self.claim(item, "alice", sid)
        row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["owner_agent_id"], "alice")
        self.assertEqual(row["next_actor_agent_id"], "alice")
        claims = self.lane_rows(item)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["status"], "active")
        self.assertEqual(claims[0]["claim_id"], result["claim_id"])
        self.assertNotIn("fencing_token", result)
        self.assertEqual(len(self.events_of("claim_acquired")), 1)

    def test_unexpired_claim_can_be_reasoned_reclaimed_after_session_exit(self):
        item = self.make_item(title="resume after stopped run")
        old_sid = self.make_session("alice", sid="s-alice-old")
        old_claim = coopdb.claim_item(
            self.conn, item_id=item, actor="alice", session_id=old_sid,
            intent="first run", lease_seconds=3600)
        coopdb.finish_session(
            self.conn, old_sid, status="exited", reason="child_exit",
            exit_code=0)
        new_sid = self.make_session("alice", sid="s-alice-new")

        action = coopdb.status(
            self.conn, "alice", session_id=new_sid,
            item_id=item)["next_action"]
        self.assertEqual(action["kind"], "recover_claim")
        self.assertEqual(action["target_id"], old_claim["claim_id"])
        recovered = coopdb.claim_item(
            self.conn, item_id=item, actor="alice", session_id=new_sid,
            intent="resume stopped run", reclaim_reason="prior session exited",
            lease_seconds=3600)
        rows = self.lane_rows(item)
        self.assertEqual([row["status"] for row in rows],
                         ["stale", "active"])
        self.assertEqual(recovered["claim_id"], rows[-1]["claim_id"])

    def test_claim_requires_intent(self):
        item = self.make_item()
        sid = self.make_session("alice")
        with self.assertRaises(InvalidTransition) as caught:
            self.claim(item, "alice", sid, intent="   ")
        self.assertEqual(caught.exception.reason_code, "input_invalid")
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "non_empty_claim_intent",
        )

    def test_human_cannot_claim(self):
        item = self.make_item()
        with self.assertRaises(HumanLaneViolation) as caught:
            coopdb.claim_item(
                self.conn, item_id=item, actor="human", session_id=None,
                intent="nope")
        self.assertEqual(caught.exception.reason_code, "human_lane_forbidden")
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "agent_session_required",
        )

    def test_claim_requires_live_matching_session(self):
        item = self.make_item()
        with self.assertRaises(SessionMismatch):
            self.claim(item, "alice", "no-such-session")
        sid = self.make_session("alice")
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit", exit_code=0)
        with self.assertRaises(SessionMismatch):
            self.claim(item, "alice", sid)

    def test_incomplete_contract_is_unclaimable(self):
        self.conn.execute(
            "INSERT INTO items(title,status,contract_version,created_by,"
            "created_at,updated_at) VALUES ('legacy','todo',1,'human',?,?)",
            (coopdb.now(), coopdb.now()))
        self.conn.commit()
        item = self.conn.execute(
            "SELECT id FROM items WHERE title='legacy'").fetchone()["id"]
        sid = self.make_session("alice")
        with self.assertRaises(IncompleteContract) as caught:
            self.claim(item, "alice", sid)
        self.assertEqual(caught.exception.reason_code, "contract_incomplete")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "executable_contract_required",
        )

    def test_done_item_is_unclaimable(self):
        item = self.make_item()
        self.conn.execute(
            "UPDATE items SET status='done' WHERE id=?", (item,))
        self.conn.commit()
        sid = self.make_session("alice")
        with self.assertRaises(InvalidTransition):
            self.claim(item, "alice", sid, reclaim_reason="even with reason")

    def test_missing_item_is_not_found(self):
        sid = self.make_session("alice")
        with self.assertRaises(NotFound):
            self.claim(999, "alice", sid)

    def test_reclaim_of_lane_history_requires_reason(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        coopdb.release_claim(
            self.conn, claim_id=first["claim_id"], actor="alice",
            session_id=sid, reason="pausing")
        with self.assertRaises(InvalidTransition):
            self.claim(item, "alice", sid)  # no reason supplied

    def test_expired_predecessor_with_running_session_is_unsafe(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        self.clock.advance(LEASE + 1)
        bob = self.make_session("bob")
        with self.assertRaises(UnsafeReclaim) as caught:
            self.claim(item, "bob", bob, reclaim_reason="takeover")
        self.assertEqual(caught.exception.reason_code, "unsafe_reclaim")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["claim_id"], first["claim_id"])
        self.assertEqual(caught.exception.evidence["session_status"], "running")
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "predecessor_session_exit_required",
        )
        # The rejection left the expired claim untouched (still active) —
        # rejected, not marked, while exit is unconfirmed.
        self.assertEqual(self.lane_rows(item)[0]["status"], "active")

    def test_reclaim_after_terminal_predecessor_transfers_ownership(self):
        item = self.make_item()
        sid = self.make_session("alice")
        self.claim(item, "alice", sid)
        self.clock.advance(LEASE + 1)
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit", exit_code=0)
        bob = self.make_session("bob")
        result = self.claim(item, "bob", bob, reclaim_reason="takeover")
        rows = self.lane_rows(item)
        self.assertEqual([r["status"] for r in rows], ["stale", "active"])
        owner = self.conn.execute(
            "SELECT owner_agent_id FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(owner["owner_agent_id"], "bob")
        # Ownership transfer away from alice is an addressed delivery.
        entries = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='alice' "
            "AND category='ownership_transfer'").fetchall()
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(self.events_of("claim_stale")), 1)
        self.assertIsNotNone(result["claim_id"])

    def test_same_agent_resume_preserves_ownership(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        coopdb.release_claim(
            self.conn, claim_id=first["claim_id"], actor="alice",
            session_id=sid, reason="pausing")
        self.claim(item, "alice", sid, reclaim_reason="resuming")
        owner = self.conn.execute(
            "SELECT owner_agent_id, status FROM items WHERE id=?",
            (item,)).fetchone()
        self.assertEqual(owner["owner_agent_id"], "alice")
        self.assertEqual(owner["status"], "working")
        transfers = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE category='ownership_transfer'"
        ).fetchall()
        self.assertEqual(transfers, [])

    def test_voluntary_release_preserves_item_state_and_ownership(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        coopdb.release_claim(
            self.conn, claim_id=first["claim_id"], actor="alice",
            session_id=sid, reason="stepping away")
        row = self.conn.execute(
            "SELECT status, owner_agent_id FROM items WHERE id=?",
            (item,)).fetchone()
        self.assertEqual(row["status"], "working")  # owned but unclaimed
        self.assertEqual(row["owner_agent_id"], "alice")
        claim = self.lane_rows(item)[0]
        self.assertEqual(claim["status"], "released")
        self.assertEqual(claim["close_reason"], "stepping away")
        self.assertEqual(len(self.events_of("claim_released")), 1)

    def test_release_requires_owning_session(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        bob = self.make_session("bob")
        with self.assertRaises(SessionMismatch) as caught:
            coopdb.release_claim(
                self.conn, claim_id=first["claim_id"], actor="bob",
                session_id=bob, reason="not mine")
        self.assertEqual(caught.exception.reason_code, "actor_mismatch")
        self.assertEqual(caught.exception.evidence["actor_agent_id"], "bob")
        self.assertEqual(caught.exception.evidence["required_agent_id"], "alice")


class FencingAndLeases(ClaimBoard):
    def test_per_lane_token_monotonicity(self):
        item1, item2 = self.make_item(), self.make_item(title="T2")
        alice = self.make_session("alice")
        bob = self.make_session("bob")
        first = self.claim(item1, "alice", alice)
        self.claim(item2, "bob", bob)
        tokens = {
            r["lane_key"]: r["fencing_token"]
            for r in self.conn.execute("SELECT lane_key, fencing_token FROM claims")}
        # Per-lane counters: two fresh lanes both start at 1, not a global 1,2.
        self.assertEqual(set(tokens.values()), {1})
        coopdb.release_claim(
            self.conn, claim_id=first["claim_id"], actor="alice",
            session_id=alice, reason="pausing")
        self.claim(item1, "alice", alice, reclaim_reason="resuming")
        lane1 = f"implementation:item:{item1}"
        top = self.conn.execute(
            "SELECT MAX(fencing_token) AS t FROM claims WHERE lane_key=?",
            (lane1,)).fetchone()["t"]
        self.assertEqual(top, 2)
        lane2 = f"implementation:item:{item2}"
        top2 = self.conn.execute(
            "SELECT MAX(fencing_token) AS t FROM claims WHERE lane_key=?",
            (lane2,)).fetchone()["t"]
        self.assertEqual(top2, 1)

    def test_renewal_extends_only_live_claims_of_that_session(self):
        item1, item2 = self.make_item(), self.make_item(title="T2")
        alice = self.make_session("alice")
        bob = self.make_session("bob")
        self.claim(item1, "alice", alice)
        self.claim(item2, "bob", bob)
        self.clock.advance(10)
        renewed = coopdb.renew_claims(self.conn, session_id=alice)
        self.assertEqual(renewed, 1)
        rows = {r["claimed_by_agent"]: r["lease_expires_at"]
                for r in self.conn.execute("SELECT * FROM claims")}
        self.assertGreater(rows["alice"], rows["bob"])

    def test_terminal_session_cannot_renew_an_unexpired_claim(self):
        item = self.make_item()
        sid = self.make_session("alice")
        claim = self.claim(item, "alice", sid)
        before = self.conn.execute(
            "SELECT last_renewed_at, lease_expires_at FROM claims "
            "WHERE claim_id=?", (claim["claim_id"],)).fetchone()
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit",
            exit_code=0)
        self.clock.advance(10)

        self.assertEqual(coopdb.renew_claims(
            self.conn, session_id=sid), 0)
        after = self.conn.execute(
            "SELECT last_renewed_at, lease_expires_at FROM claims "
            "WHERE claim_id=?", (claim["claim_id"],)).fetchone()
        self.assertEqual(dict(after), dict(before))

    def test_expired_claim_is_never_revived_after_a_clock_jump(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        before = self.lane_rows(item)[0]["lease_expires_at"]
        self.clock.advance(LEASE + 10)  # laptop-sleep style stall
        renewed = coopdb.renew_claims(self.conn, session_id=sid)
        self.assertEqual(renewed, 0)
        self.assertEqual(self.lane_rows(item)[0]["lease_expires_at"], before)
        flipped = coopdb.sweep_expired(self.conn)
        self.assertEqual(flipped, [first["claim_id"]])
        self.assertEqual(self.lane_rows(item)[0]["status"], "stale")
        # And renewal of a stale claim is a no-op forever after.
        self.assertEqual(coopdb.renew_claims(self.conn, session_id=sid), 0)

    def test_validate_claim_refuses_expired_but_unswept(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        self.clock.advance(LEASE)  # exactly at expiry: no longer unexpired
        with self.assertRaises(StaleClaim) as caught:
            coopdb.validate_claim(
                self.conn, claim_id=first["claim_id"], session_id=sid,
                actor="alice")
        self.assertEqual(caught.exception.reason_code, "claim_expired")
        self.assertEqual(caught.exception.evidence["claim_id"],
                         first["claim_id"])
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertTrue(caught.exception.evidence["lease_expired"])

    def test_sweep_is_idempotent_with_exact_cardinality(self):
        item = self.make_item()
        sid = self.make_session("alice")
        self.claim(item, "alice", sid)
        self.clock.advance(LEASE + 1)
        flipped = coopdb.sweep_expired(self.conn)
        self.assertEqual(len(flipped), 1)
        self.assertEqual(len(self.events_of("claim_stale")), 1)
        warnings = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE category='stale_warning'"
        ).fetchall()
        self.assertEqual(len(warnings), 1)
        self.assertEqual(coopdb.sweep_expired(self.conn), [])  # no-op: zero new
        self.assertEqual(len(self.events_of("claim_stale")), 1)


class ConcurrencySuite(ClaimBoard):
    def _race(self, fns):
        barrier = threading.Barrier(len(fns))
        results = [None] * len(fns)

        def run(i, fn):
            conn = coopdb.connect(self.db)
            try:
                barrier.wait(timeout=10)
                try:
                    results[i] = ("ok", fn(conn))
                except coopdb.CoopError as exc:
                    results[i] = ("err", exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(i, fn))
                   for i, fn in enumerate(fns)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "race thread deadlocked")
        return results

    def test_two_session_race_single_winner(self):
        item1 = self.make_item()
        item2 = self.make_item(title="T2")
        s_alice = self.make_session("alice", sid="s-alice")
        s_bob = self.make_session("bob", sid="s-bob")

        def attempt(agent, sid):
            return lambda conn: coopdb.claim_item(
                conn, item_id=item1, actor=agent, session_id=sid,
                intent="race")

        results = self._race(
            [attempt("alice", "s-alice"), attempt("bob", "s-bob")])
        outcomes = {kind for kind, _ in results}
        self.assertEqual(outcomes, {"ok", "err"})
        loser_exc = next(v for k, v in results if k == "err")
        self.assertIsInstance(loser_exc, ClaimCollision)
        winner = next(v for k, v in results if k == "ok")
        winner_agent = self.conn.execute(
            "SELECT claimed_by_agent FROM claims WHERE claim_id=?",
            (winner["claim_id"],)).fetchone()["claimed_by_agent"]
        loser_agent = "bob" if winner_agent == "alice" else "alice"
        loser_sid = f"s-{loser_agent}"

        # Non-poisoning: the loser immediately claims different work.
        self.claim(item2, loser_agent, loser_sid, conn=self.conn)

        # Loser zero-effect: the whole database equals a control board where
        # the same two claims simply happened with no collision at all.
        race_state = snapshot(self.conn)
        with TemporaryDirectory() as ctrl_dir:
            ctrl_db = str(Path(ctrl_dir) / "board.db")
            ctrl = coopdb.connect(ctrl_db)
            try:
                coopdb.init_db(ctrl)
                c1 = coopdb.create_item(
                    ctrl, actor="human", session_id=None, **contract_kwargs())
                c2 = coopdb.create_item(
                    ctrl, actor="human", session_id=None,
                    **contract_kwargs(title="T2"))
                self.assertEqual((c1, c2), (item1, item2))
                for agent, sid in (("alice", "s-alice"), ("bob", "s-bob")):
                    coopdb.insert_session(
                        ctrl, session_id=sid, agent_id=agent,
                        provider="claude", command=["python", "-c", "pass"],
                        cwd=".", max_runtime_s=28800, grace_s=10)
                coopdb.claim_item(
                    ctrl, item_id=item1, actor=winner_agent,
                    session_id=f"s-{winner_agent}", intent="race")
                coopdb.claim_item(
                    ctrl, item_id=item2, actor=loser_agent,
                    session_id=loser_sid, intent="work")
                self.assertEqual(race_state, snapshot(ctrl))
            finally:
                ctrl.close()

    def test_concurrent_sweeps_transition_once(self):
        item = self.make_item()
        sid = self.make_session("alice")
        self.claim(item, "alice", sid)
        self.clock.advance(LEASE + 1)
        results = self._race(
            [lambda conn: coopdb.sweep_expired(conn)] * 2)
        flipped = [v for k, v in results if k == "ok"]
        self.assertEqual(sorted(len(f) for f in flipped), [0, 1])
        self.assertEqual(len(self.events_of("claim_stale")), 1)
        warnings = self.conn.execute(
            "SELECT COUNT(*) AS n FROM inbox_entries "
            "WHERE category='stale_warning'").fetchone()["n"]
        self.assertEqual(warnings, 1)
        self.assertEqual(self.lane_rows(item)[0]["status"], "stale")

    def test_renew_vs_sweep_at_expiry_boundary(self):
        # At now >= lease_expires_at the claim is no longer "still-unexpired",
        # so renewal loses in BOTH serialization orders.
        for order in ("renew_first", "sweep_first"):
            with self.subTest(order=order):
                item = self.make_item(title=f"boundary-{order}")
                sid = self.make_session(f"agent-{order}")
                self.claim(item, f"agent-{order}", sid)
                self.clock.advance(LEASE)  # exactly at expiry
                if order == "renew_first":
                    self.assertEqual(
                        coopdb.renew_claims(self.conn, session_id=sid), 0)
                    self.assertEqual(len(coopdb.sweep_expired(self.conn)), 1)
                else:
                    self.assertEqual(len(coopdb.sweep_expired(self.conn)), 1)
                    self.assertEqual(
                        coopdb.renew_claims(self.conn, session_id=sid), 0)
                self.assertEqual(
                    self.lane_rows(item)[0]["status"], "stale",
                    "the claim must end stale whichever transaction ran first")
                self.clock.advance(-LEASE)  # reset for the next subtest lane

    def test_renewal_wins_before_expiry_and_sweep_finds_nothing(self):
        item = self.make_item()
        sid = self.make_session("alice")
        self.claim(item, "alice", sid)
        self.clock.advance(LEASE - 1)  # one tick before expiry
        self.assertEqual(coopdb.renew_claims(self.conn, session_id=sid), 1)
        self.assertEqual(coopdb.sweep_expired(self.conn), [])
        self.assertEqual(self.lane_rows(item)[0]["status"], "active")

    def test_two_reclaimers_one_winner(self):
        item = self.make_item()
        sid = self.make_session("alice", sid="s-alice")
        self.claim(item, "alice", "s-alice")
        self.clock.advance(LEASE + 1)
        coopdb.finish_session(
            self.conn, "s-alice", status="exited", reason="child_exit",
            exit_code=0)
        self.make_session("bob", sid="s-bob")
        self.make_session("carol", sid="s-carol")

        def attempt(agent, sid):
            return lambda conn: coopdb.claim_item(
                conn, item_id=item, actor=agent, session_id=sid,
                intent="rescue", reclaim_reason="predecessor exited")

        results = self._race(
            [attempt("bob", "s-bob"), attempt("carol", "s-carol")])
        kinds = sorted(k for k, _ in results)
        self.assertEqual(kinds, ["err", "ok"])
        loser_exc = next(v for k, v in results if k == "err")
        self.assertIsInstance(loser_exc, ClaimCollision)
        rows = self.lane_rows(item)
        self.assertEqual([r["status"] for r in rows], ["stale", "active"])
        self.assertEqual(rows[-1]["fencing_token"], 2)  # one new generation
        self.assertEqual(len(self.events_of("claim_stale")), 1)
        self.assertEqual(len(self.events_of("claim_acquired")), 2)
        winner_agent = rows[-1]["claimed_by_agent"]
        owner = self.conn.execute(
            "SELECT owner_agent_id FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(owner["owner_agent_id"], winner_agent)

    def test_same_session_reacquisition_rejects_old_claim(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        # Stub of the needs-input close: the claim ends deliberately.
        coopdb.release_claim(
            self.conn, claim_id=first["claim_id"], actor="alice",
            session_id=sid, reason="needs-input stub")
        second = self.claim(item, "alice", sid, reclaim_reason="resuming")
        self.assertNotEqual(first["claim_id"], second["claim_id"])
        before = snapshot(self.conn)
        with self.assertRaises(StaleClaim) as caught:
            coopdb.validate_claim(
                self.conn, claim_id=first["claim_id"], session_id=sid,
                actor="alice")
        self.assertEqual(caught.exception.reason_code, "claim_not_current")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["claim_id"],
                         first["claim_id"])
        self.assertEqual(caught.exception.evidence["current_status"],
                         "released")
        with self.assertRaises(StaleClaim):
            coopdb.release_claim(
                self.conn, claim_id=first["claim_id"], actor="alice",
                session_id=sid, reason="late write")
        self.assertEqual(before, snapshot(self.conn))
        current = coopdb.validate_claim(
            self.conn, claim_id=second["claim_id"], session_id=sid,
            actor="alice")
        self.assertEqual(current["claim_id"], second["claim_id"])

    def test_superseded_claim_rejected_everywhere(self):
        # CUMULATIVE matrix: seeded with the claim core (voluntary
        # release + the validate_claim chokepoint), then extended with
        # checkpoints, needs-input, question answers, and every later lane.
        def induce(state, item, sid, agent):
            claim = self.claim(item, agent, sid)
            cid = claim["claim_id"]
            if state == "stale":
                self.clock.advance(LEASE + 1)
                coopdb.sweep_expired(self.conn)
                self.clock.advance(-(LEASE + 1))
            elif state == "released":
                coopdb.release_claim(
                    self.conn, claim_id=cid, actor=agent, session_id=sid,
                    reason="matrix")
            else:  # completed / closed have no direct inducer; manufacture.
                self.conn.execute(
                    "UPDATE claims SET status=?, closed_at=?, close_reason="
                    "'matrix' WHERE claim_id=?", (state, coopdb.now(), cid))
                self.conn.commit()
            return cid

        def checkpoint_op(ctype):
            return lambda cid, sid, agent: coopdb.checkpoint(
                self.conn, ctype=ctype, claim_id=cid, actor=agent,
                session_id=sid, note="matrix stop condition")

        def needs_peer(cid, sid, agent=None):
            # Product mode: needs-input is peer-only (never human).
            peer = f"peer{cid}"
            coopdb.register_agent(self.conn, peer)
            return coopdb.needs_input(
                self.conn, claim_id=cid, session_id=sid, to_agent=peer,
                question="matrix?")

        mutations = {
            "validate_claim": lambda cid, sid, agent: coopdb.validate_claim(
                self.conn, claim_id=cid, session_id=sid, actor=agent),
            "release_claim": lambda cid, sid, agent: coopdb.release_claim(
                self.conn, claim_id=cid, actor=agent, session_id=sid,
                reason="late"),
            # Checkpoints: every checkpoint type refuses a dead claim.
            **{f"checkpoint_{t}": checkpoint_op(t)
               for t in ("start", "step", "blocked", "risky", "predone")},
            # Questions: the needs-input transition and the answer
            # path refuse a dead claim the same way.
            "needs_input": needs_peer,
            "question_answer": lambda cid, sid, agent: (
                coopdb.answer_question(
                    self.conn, claim_id=cid, session_id=sid,
                    answer="matrix")),
            # Receipts: receipt submission refuses a dead
            # claim at the validate_claim chokepoint, before any file read
            # (the path below is real so only the claim boundary can fire).
            "receipt_submit": lambda cid, sid, agent: coopdb.submit_receipt(
                self.conn, claim_id=cid, session_id=sid, actor=agent,
                path=str(Path(__file__)), summary="matrix", proof="matrix",
                proof_refs=None),
            # Reviews: a review request through a dead
            # implementation claim refuses at the same chokepoint, before
            # any receipt or review derivation runs.
            "review_request": lambda cid, sid, agent: coopdb.request_review(
                self.conn, claim_id=cid, session_id=sid, actor=agent),
            # Decisions: a decision through a dead
            # implementation claim refuses at the chokepoint too — the
            # append-only ledger records only claim-validated calls.
            "decision_record": lambda cid, sid, agent: (
                coopdb.record_decision(
                    self.conn, claim_id=cid, session_id=sid, actor=agent,
                    text="matrix")),
            # Verdicts: a verdict through a dead claim
            # refuses at the chokepoint — status precedes the kind check,
            # so the dead implementation claim never reaches derivation.
            "review_submit": lambda cid, sid, agent: coopdb.submit_verdict(
                self.conn, claim_id=cid, session_id=sid, actor=agent,
                verdict="approve"),
            # Completion: completion through a dead claim
            # refuses at the chokepoint before any gate step runs — no
            # receipt read, no evidence re-hash, no supersede.
            "item_complete": lambda cid, sid, agent: coopdb.complete_item(
                self.conn, claim_id=cid, session_id=sid, actor=agent),
            # Handoffs: handoff creation through a dead
            # claim refuses at the chokepoint — real target and real file
            # ref, so only the claim boundary can fire.
            "handoff_create": lambda cid, sid, agent: coopdb.create_handoff(
                self.conn, claim_id=cid, session_id=sid, actor=agent,
                to_agent="handoff-target", reason="m", summary="m",
                completed="m", remaining="m", risks="m", next_action="m",
                proof_refs=[f"file:{Path(__file__)}"]),
        }
        coopdb.register_agent(self.conn, "handoff-target")
        for state in ("stale", "released", "completed", "closed"):
            for name, op in mutations.items():
                with self.subTest(state=state, mutation=name):
                    agent = f"a-{state}-{name}".replace("_", "-")
                    item = self.make_item(title=f"{state}-{name}")
                    sid = self.make_session(agent)
                    cid = induce(state, item, sid, agent)
                    before = snapshot(self.conn)
                    with self.assertRaises(StaleClaim):
                        op(cid, sid, agent)
                    self.assertEqual(before, snapshot(self.conn))


class OperatorRecovery(ClaimBoard):
    def test_admin_release_rejected_inside_a_session(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        with unittest.mock.patch.dict(
                "os.environ", {"COOP_SESSION_ID": sid}):
            with self.assertRaises(HumanLaneViolation):
                coopdb.admin_release(
                    self.conn, claim_id=first["claim_id"], reason="nope")

    def test_admin_release_refuses_live_unexpired_claims(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        with self.assertRaises(InvalidTransition):
            coopdb.admin_release(
                self.conn, claim_id=first["claim_id"], reason="too alive")

    def test_admin_release_requires_confirmation_while_session_runs(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        self.clock.advance(LEASE + 1)
        with self.assertRaises(UnsafeReclaim):
            coopdb.admin_release(
                self.conn, claim_id=first["claim_id"], reason="wedge")
        coopdb.admin_release(
            self.conn, claim_id=first["claim_id"], reason="wedge",
            confirm_process_stopped=True)
        row = self.lane_rows(item)[0]
        self.assertEqual(row["status"], "released")
        events = self.events_of("operator_release")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor_agent_id"], "human")
        self.assertIsNone(events[0]["actor_session_id"])

    def test_admin_release_cannot_create_transfer_or_complete(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        self.clock.advance(LEASE + 1)
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit", exit_code=0)
        before = self.conn.execute(
            "SELECT status, owner_agent_id FROM items WHERE id=?",
            (item,)).fetchone()
        n_claims = self.conn.execute(
            "SELECT COUNT(*) AS n FROM claims").fetchone()["n"]
        coopdb.admin_release(
            self.conn, claim_id=first["claim_id"], reason="crash cleanup")
        after = self.conn.execute(
            "SELECT status, owner_agent_id FROM items WHERE id=?",
            (item,)).fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"],
            n_claims)
        # The lane is now eligible for a normal reasoned reclaim.
        bob = self.make_session("bob")
        self.claim(item, "bob", bob, reclaim_reason="after operator release")

    def test_admin_release_accepts_only_stale_family(self):
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        coopdb.release_claim(
            self.conn, claim_id=first["claim_id"], actor="alice",
            session_id=sid, reason="voluntary")
        with self.assertRaises(InvalidTransition):
            coopdb.admin_release(
                self.conn, claim_id=first["claim_id"], reason="already over")


class CliSurface(ClaimBoard):
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
                    # A string exit code is what the interpreter prints to
                    # stderr before exiting 1 — mirror that faithfully.
                    if isinstance(exc.code, int):
                        code = exc.code
                    else:
                        code = 1
                        if exc.code:
                            stderr.write(str(exc.code))
        return code, stdout.getvalue(), stderr.getvalue()

    def _clean_env(self):
        # Ensure no ambient session leaks into human-lane CLI tests.
        return {"COOP_SESSION_ID": "", "COOP_AGENT": "", "COOP_AGENT_ID": ""}

    def test_cli_claim_release_happy_path_is_tokenless(self):
        item = self.make_item()
        sid = self.make_session("alice")
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}
        code, out, err = self._run(
            ["item", "claim", str(item), "--intent", "cli work"], env)
        self.assertEqual(code, 0, err)
        self.assertNotIn("fencing_token", out + err)
        claim_id = self.lane_rows(item)[0]["claim_id"]
        code, out, err = self._run(
            ["claim", "release", "--claim", str(claim_id),
             "--reason", "cli done"], env)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.lane_rows(item)[0]["status"], "released")

    def test_cli_reclaim_requires_reason(self):
        item = self.make_item()
        sid = self.make_session("alice")
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}
        self._run(["item", "claim", str(item), "--intent", "w"], env)
        self._run(["claim", "release", "--claim",
                   str(self.lane_rows(item)[0]["claim_id"]),
                   "--reason", "pause"], env)
        code, out, err = self._run(
            ["item", "claim", str(item), "--intent", "again", "--reclaim"],
            env)
        self.assertEqual(code, 1)
        self.assertIn("invalid_transition", err)

    def test_cli_admin_release_rejected_inside_session(self):
        item = self.make_item()
        sid = self.make_session("alice")
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}
        self._run(["item", "claim", str(item), "--intent", "w"], env)
        claim_id = self.lane_rows(item)[0]["claim_id"]
        code, out, err = self._run(
            ["admin", "release", str(claim_id), "--reason", "no"], env)
        self.assertEqual(code, 1)
        self.assertIn("human_lane_violation", err)

    def test_cli_admin_release_happy_path(self):
        item = self.make_item()
        sid = self.make_session("alice")
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}
        self._run(["item", "claim", str(item), "--intent", "w"], env)
        claim_id = self.lane_rows(item)[0]["claim_id"]
        self.clock.advance(LEASE + 1)
        code, out, err = self._run(
            ["admin", "release", str(claim_id), "--reason", "op cleanup",
             "--confirm-process-stopped"], self._clean_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.lane_rows(item)[0]["status"], "released")

    def test_packet_claim_block_reads_the_implementation_lane(self):
        item = self.make_item()
        sid = self.make_session("alice")
        self.claim(item, "alice", sid)
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(packet["claim"]["agent"], "alice")
        self.assertEqual(packet["claim"]["session_id"], sid[:8])
        self.assertNotIn("command", packet["session"])
        flat = repr(packet)
        self.assertNotIn("fencing_token", flat)


class LaneHistoryGuard(ClaimBoard):
    """One shared lane-history guard for every claim kind —
    the swept-stale reclaim bypass closes. Lane
    safety is the guard's only question; subject eligibility stays with
    each claim path."""

    def _swept_stale_lane(self):
        """Alice's claim expires and another session's maintenance sweep
        flips it stale while alice's session still reports running."""
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        self.clock.advance(LEASE + 1)
        flipped = coopdb.sweep_expired(self.conn)
        self.assertEqual(flipped, [first["claim_id"]])
        self.assertEqual(self.lane_rows(item)[0]["status"], "stale")
        return item, sid, first

    def test_swept_stale_with_running_predecessor_is_refused(self):
        # The repaired branch: once the sweep has run, reclaim must still
        # demand predecessor-session evidence — a reason alone no longer
        # seizes a still-running session's work.
        item, sid, first = self._swept_stale_lane()
        bob = self.make_session("bob")
        before = snapshot(self.conn)
        with self.assertRaises(UnsafeReclaim):
            self.claim(item, "bob", bob, reclaim_reason="seize it")
        self.assertEqual(before, snapshot(self.conn))

    def test_swept_stale_reclaim_succeeds_after_terminal_predecessor(self):
        item, sid, first = self._swept_stale_lane()
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit", exit_code=0)
        bob = self.make_session("bob")
        result = self.claim(
            item, "bob", bob, reclaim_reason="predecessor exited")
        rows = self.lane_rows(item)
        self.assertEqual([r["status"] for r in rows], ["stale", "active"])
        self.assertGreater(rows[1]["fencing_token"], rows[0]["fencing_token"])
        self.assertEqual(rows[1]["claim_id"], result["claim_id"])
        owner = self.conn.execute(
            "SELECT owner_agent_id FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(owner["owner_agent_id"], "bob")

    def test_released_lane_reclaims_while_releaser_still_runs(self):
        # A release is a deliberate end: reason required, no session test —
        # even though the releasing session still reports running.
        item = self.make_item()
        sid = self.make_session("alice")
        first = self.claim(item, "alice", sid)
        coopdb.release_claim(
            self.conn, claim_id=first["claim_id"], actor="alice",
            session_id=sid, reason="pausing")
        self.assertTrue(coopdb._session_is_running(self.conn, sid))
        bob = self.make_session("bob")
        result = self.claim(
            item, "bob", bob, reclaim_reason="continuing paused work")
        rows = self.lane_rows(item)
        self.assertEqual([r["status"] for r in rows], ["released", "active"])
        self.assertEqual(rows[1]["claim_id"], result["claim_id"])

    def test_reasonless_attempt_on_any_history_shape_is_invalid_transition(self):
        # released lane, reason-less.
        item_r = self.make_item()
        sid_r = self.make_session("alice")
        first_r = self.claim(item_r, "alice", sid_r)
        coopdb.release_claim(
            self.conn, claim_id=first_r["claim_id"], actor="alice",
            session_id=sid_r, reason="pausing")
        with self.subTest(shape="released"):
            before = snapshot(self.conn)
            with self.assertRaises(InvalidTransition):
                self.claim(item_r, "alice", sid_r)
            self.assertEqual(before, snapshot(self.conn))
        # safely-stale lane (terminal predecessor), reason-less.
        item_s = self.make_item()
        sid_s = self.make_session("bob")
        self.claim(item_s, "bob", sid_s)
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        coopdb.finish_session(
            self.conn, sid_s, status="exited", reason="child_exit",
            exit_code=0)
        carol = self.make_session("carol")
        with self.subTest(shape="stale_terminal"):
            before = snapshot(self.conn)
            with self.assertRaises(InvalidTransition):
                self.claim(item_s, "carol", carol)
            self.assertEqual(before, snapshot(self.conn))

    def test_lane_history_guard_classifier(self):
        """Drive _lane_history_guard directly through every classification
        branch — the proven contract the third (review) consumer plugs
        into."""
        guard = lambda lane, reason: coopdb._in_mutate(
            self.conn, lambda c: coopdb._lane_history_guard(
                c, lane=lane, reclaim_reason=reason))
        # No history: proceed, with or without a reason.
        self.assertIsNone(guard("implementation:item:999", None))
        self.assertIsNone(guard("implementation:item:999", "why"))
        # Active unexpired: collision, reason irrelevant.
        item_b = self.make_item()
        sid_b = self.make_session("dave")
        first_b = self.claim(item_b, "dave", sid_b)
        lane_b = f"implementation:item:{item_b}"
        for reason in (None, "reason"):
            with self.assertRaises(ClaimCollision) as caught:
                guard(lane_b, reason)
            self.assertEqual(caught.exception.reason_code, "claim_conflict")
            self.assertEqual(caught.exception.evidence["item_id"], item_b)
            self.assertEqual(caught.exception.evidence["lane"], lane_b)
            self.assertEqual(
                caught.exception.evidence["owner_agent_id"], "dave")
        self.assertEqual(self.lane_rows(item_b)[0]["status"], "active")
        # Active expired, session running: reason-less stays
        # invalid_transition; reasoned is unsafe_reclaim. Both refusals
        # roll back the in-guard stale flip — the row stays active.
        self.clock.advance(LEASE + 1)
        with self.assertRaises(InvalidTransition):
            guard(lane_b, None)
        self.assertEqual(self.lane_rows(item_b)[0]["status"], "active")
        with self.assertRaises(UnsafeReclaim):
            guard(lane_b, "seize")
        self.assertEqual(self.lane_rows(item_b)[0]["status"], "active")
        # Active expired, terminal session, reasoned: flips stale in place
        # (committed) and returns the newest row.
        coopdb.finish_session(
            self.conn, sid_b, status="exited", reason="child_exit",
            exit_code=0)
        row = guard(lane_b, "recover")
        self.assertEqual(row["claim_id"], first_b["claim_id"])
        self.assertEqual(self.lane_rows(item_b)[0]["status"], "stale")
        self.assertEqual(len(self.events_of("claim_stale")), 1)
        # Already-swept stale, running session: reason-less invalid,
        # reasoned unsafe; terminal session with reason proceeds.
        item_e = self.make_item()
        sid_e = self.make_session("erin")
        self.claim(item_e, "erin", sid_e)
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        lane_e = f"implementation:item:{item_e}"
        with self.assertRaises(InvalidTransition):
            guard(lane_e, None)
        with self.assertRaises(UnsafeReclaim):
            guard(lane_e, "grab")
        coopdb.finish_session(
            self.conn, sid_e, status="exited", reason="child_exit",
            exit_code=0)
        self.assertIsNotNone(guard(lane_e, "safe now"))
        # Released while its session still runs: reason only.
        item_f = self.make_item()
        sid_f = self.make_session("frank")
        first_f = self.claim(item_f, "frank", sid_f)
        coopdb.release_claim(
            self.conn, claim_id=first_f["claim_id"], actor="frank",
            session_id=sid_f, reason="pausing")
        lane_f = f"implementation:item:{item_f}"
        with self.assertRaises(InvalidTransition):
            guard(lane_f, None)
        self.assertIsNotNone(guard(lane_f, "resume"))
        # Closed (deliberate end): reason only.
        self.conn.execute(
            "UPDATE claims SET status='closed', close_reason='needs_input' "
            "WHERE claim_id=?", (first_f["claim_id"],))
        self.conn.commit()
        with self.assertRaises(InvalidTransition):
            guard(lane_f, None)
        self.assertIsNotNone(guard(lane_f, "resume"))
        # Completed (deliberate end): reason only.
        self.conn.execute(
            "UPDATE claims SET status='completed', close_reason='answered' "
            "WHERE claim_id=?", (first_f["claim_id"],))
        self.conn.commit()
        with self.assertRaises(InvalidTransition):
            guard(lane_f, None)
        self.assertIsNotNone(guard(lane_f, "resume"))


if __name__ == "__main__":
    unittest.main()
