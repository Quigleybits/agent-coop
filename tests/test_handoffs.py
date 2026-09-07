"""Structured handoffs — transfer only on acceptance.

`create_handoff` closes the originator's implementation claim, freezes the
lane while the handoff is pending (every claim path refuses in-transaction),
and delivers the six-field structured transfer with at least one linted
proof reference. `accept_handoff` mints the fresh implementation claim
through the shared lane-history guard (the acceptor's intent is the guard's
reclaim reason) and transfers ownership; `decline_handoff` hands the item
back to the owner under the answered-question grace machinery. Nothing
times a pending handoff out — an unresponsive target is a visible wedge.
"""

import contextlib
import io
import json
import pathlib
import threading
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop.coop_errors import (
    AddressedTargetMismatch,
    ClaimCollision,
    CoopError,
    HumanLaneViolation,
    InvalidTransition,
    NotFound,
    ProofReferenceInvalid,
    StaleClaim,
)
from tests.test_claims import LEASE
from tests.test_reviews import SNAP_TABLES, ReviewBoard

GRACE = coopdb.DEFAULT_RESUME_GRACE_SECONDS
FILE_REF = f"file:{pathlib.Path(__file__).resolve()}"

HSNAP_TABLES = SNAP_TABLES + ("handoffs", "questions")


def hsnap(conn):
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
        for t in HSNAP_TABLES
    }


class HandoffBoard(ReviewBoard):
    def transfer_kwargs(self, **over):
        base = dict(
            reason="context switch", summary="half done",
            completed="parser built", remaining="serializer missing",
            risks="fixture drift", next_action="finish the serializer",
            proof_refs=[FILE_REF])
        base.update(over)
        return base

    def create_h(self, cid, sid, agent="alice", to_agent="bob", **over):
        coopdb.register_agent(self.conn, to_agent)
        return coopdb.create_handoff(
            self.conn, claim_id=cid, session_id=sid, actor=agent,
            to_agent=to_agent, **self.transfer_kwargs(**over))

    def make_pending(self, agent="alice", to_agent="bob", **item_over):
        item, sid, cid = self.make_working(agent, **item_over)
        result = self.create_h(cid, sid, agent=agent, to_agent=to_agent)
        return item, sid, cid, result["handoff_id"]

    def handoff_row(self, hid):
        return self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id=?", (hid,)).fetchone()

    def active_claims(self, item):
        return self.conn.execute(
            "SELECT * FROM claims WHERE item_id=? AND status='active'",
            (item,)).fetchall()


class CreateValidation(HandoffBoard):
    def test_create_closes_claim_freezes_item_delivers_transfer(self):
        item, sid, cid = self.make_working()
        result = self.create_h(cid, sid)
        hid = result["handoff_id"]
        row = self.handoff_row(hid)
        claim = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (cid,)).fetchone()
        self.assertEqual(claim["status"], "closed")
        self.assertEqual(claim["close_reason"], "handoff")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["from_agent"], "alice")
        self.assertEqual(row["from_session"], sid)
        self.assertEqual(row["to_agent"], "bob")
        self.assertEqual(
            row["execution_fencing_token"], claim["fencing_token"])
        refs = json.loads(row["proof_references"])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["type"], "file")
        self.assertIn("sha256", refs[0])
        item_row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(item_row["status"], "handoff")
        self.assertEqual(item_row["owner_agent_id"], "alice")
        self.assertEqual(item_row["next_actor_agent_id"], "bob")
        events = self.events_of("handoff_created")
        self.assertEqual(len(events), 1)
        self.assertNotIn(
            "fencing_token", events[0]["payload_json"])
        delivered = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='bob' AND "
            "category='handoff'").fetchall()
        self.assertEqual(len(delivered), 1)
        self.assertNotIn("fencing_token", delivered[0]["payload_json"])

    def test_create_refuses_from_review(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        before = hsnap(self.conn)
        with self.assertRaises(InvalidTransition) as ctx:
            self.create_h(cid, sid)
        self.assertIn("review", str(ctx.exception))
        self.assertEqual(ctx.exception.reason_code, "transition_not_available")
        self.assertEqual(ctx.exception.evidence["item_id"], item)
        self.assertEqual(ctx.exception.evidence["current_state"], "review")
        self.assertEqual(hsnap(self.conn), before)

    def test_create_refuses_without_live_claim(self):
        # needs_input and blocked both close the implementation claim, so
        # handoff creation from either state dies at the claim chokepoint.
        for state in ("needs_input", "blocked"):
            with self.subTest(state=state):
                agent = f"al-{state.replace('_', '-')}"
                item, sid, cid = self.make_working(
                    agent=agent, title=f"t-{state}")
                if state == "needs_input":
                    coopdb.register_agent(self.conn, "bob")
                    self.make_session("bob", sid=f"s-bob-{state}")
                    coopdb.needs_input(
                        self.conn, claim_id=cid, session_id=sid,
                        to_agent="bob", question="blocked?")
                else:
                    coopdb.checkpoint(
                        self.conn, ctype="blocked", claim_id=cid,
                        actor=agent, session_id=sid, note="stop condition")
                before = hsnap(self.conn)
                with self.assertRaises(StaleClaim):
                    self.create_h(cid, sid, agent=agent)
                self.assertEqual(hsnap(self.conn), before)

    def test_create_empty_fields_refuse(self):
        item, sid, cid = self.make_working()
        for field in ("reason", "summary", "completed", "remaining",
                      "risks", "next_action"):
            with self.subTest(field=field):
                before = hsnap(self.conn)
                with self.assertRaises(InvalidTransition):
                    self.create_h(cid, sid, **{field: "  "})
                self.assertEqual(hsnap(self.conn), before)

    def test_create_zero_proof_refs_refuse(self):
        item, sid, cid = self.make_working()
        for refs in ([], None):
            with self.subTest(refs=refs):
                before = hsnap(self.conn)
                with self.assertRaises(ProofReferenceInvalid) as caught:
                    self.create_h(cid, sid, proof_refs=refs)
                self.assertEqual(caught.exception.reason_code, "proof_invalid")
                self.assertEqual(
                    caught.exception.evidence,
                    {"constraint": "proof_reference_required"},
                )
                self.assertEqual(hsnap(self.conn), before)

    def test_create_dead_reference_refuses(self):
        item, sid, cid = self.make_working()
        before = hsnap(self.conn)
        with self.assertRaises(ProofReferenceInvalid):
            self.create_h(cid, sid, proof_refs=["decision:999"])
        self.assertEqual(hsnap(self.conn), before)

    def test_create_bad_targets_rollback_row_by_row(self):
        item, sid, cid = self.make_working()
        cases = (
            ("human", HumanLaneViolation),
            ("ghost-target", NotFound),
            ("alice", InvalidTransition),
        )
        for target, exc in cases:
            with self.subTest(target=target):
                before = hsnap(self.conn)
                with self.assertRaises(exc):
                    coopdb.create_handoff(
                        self.conn, claim_id=cid, session_id=sid,
                        actor="alice", to_agent=target,
                        **self.transfer_kwargs())
                self.assertEqual(hsnap(self.conn), before)


class Freeze(HandoffBoard):
    def test_pending_freezes_every_claim_path(self):
        item, sid, cid, hid = self.make_pending()
        coopdb.register_agent(self.conn, "carol")
        csid = self.make_session("carol")
        for agent, s, reason in (
                ("carol", csid, None),
                ("carol", csid, "third-party reclaim"),
                ("alice", sid, "owner reclaim")):
            with self.subTest(agent=agent, reason=reason):
                with self.assertRaises(InvalidTransition) as ctx:
                    coopdb.claim_item(
                        self.conn, item_id=item, actor=agent, session_id=s,
                        intent="steal", reclaim_reason=reason)
                self.assertIn("handoff", str(ctx.exception))
                self.assertEqual(
                    ctx.exception.reason_code, "blocking_work_open")
                self.assertEqual(ctx.exception.evidence["item_id"], item)
                self.assertEqual(
                    ctx.exception.evidence["blocking_object_type"], "handoff")
                self.assertEqual(
                    ctx.exception.evidence["blocking_ids"], (hid,))
        self.assertEqual(self.active_claims(item), [])

    def test_respond_asserts_no_active_claim(self):
        # Defensive: a manufactured active claim beside a pending handoff
        # is a corrupt state accept/decline must refuse to build on.
        item, sid, cid, hid = self.make_pending()
        self.conn.execute(
            "UPDATE claims SET status='active' WHERE claim_id=?", (cid,))
        self.conn.commit()
        bsid = self.make_session("bob")
        with self.assertRaises(InvalidTransition):
            coopdb.accept_handoff(
                self.conn, handoff_id=hid, session_id=bsid, actor="bob",
                intent="take it")
        with self.assertRaises(InvalidTransition):
            coopdb.decline_handoff(
                self.conn, handoff_id=hid, session_id=bsid, actor="bob",
                reason="busy")


class Respond(HandoffBoard):
    def test_accept_transfers_ownership_with_greater_generation(self):
        item, sid, cid, hid = self.make_pending()
        old_token = self.conn.execute(
            "SELECT fencing_token FROM claims WHERE claim_id=?",
            (cid,)).fetchone()["fencing_token"]
        bsid = self.make_session("bob")
        result = coopdb.accept_handoff(
            self.conn, handoff_id=hid, session_id=bsid, actor="bob",
            intent="picking up the serializer")
        new_claim = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?",
            (result["claim_id"],)).fetchone()
        self.assertGreater(new_claim["fencing_token"], old_token)
        self.assertEqual(new_claim["claimed_by_agent"], "bob")
        row = self.handoff_row(hid)
        self.assertEqual(row["status"], "accepted")
        self.assertIsNotNone(row["resolved_at"])
        item_row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(item_row["status"], "working")
        self.assertEqual(item_row["owner_agent_id"], "bob")
        events = self.events_of("handoff_accepted")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["via"], "handoff_accept")
        self.assertEqual(payload["handoff_id"], hid)
        delivered = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='alice' "
            "AND category='handoff_accepted'").fetchall()
        self.assertEqual(len(delivered), 1)
        # The acceptor's packet carries the full structured transfer.
        packet = coopdb.item_show(self.conn, item, packet=True)
        slot = packet["handoff"]
        self.assertEqual(slot["status"], "accepted")
        self.assertEqual(slot["summary"], "half done")
        self.assertEqual(slot["remaining_work"], "serializer missing")
        self.assertEqual(slot["suggested_next_action"],
                         "finish the serializer")
        self.assertNotIn("fencing_token", json.dumps(packet))

    def test_accept_uses_explicit_runner_sized_lease(self):
        _item, _sid, _cid, hid = self.make_pending()
        bsid = self.make_session("bob")
        expected = coopdb._ts(3600)
        result = coopdb.accept_handoff(
            self.conn, handoff_id=hid, session_id=bsid, actor="bob",
            intent="take it", lease_seconds=3600)
        self.assertEqual(result["lease_expires_at"], expected)

    def test_handoff_enables_peer_to_author_a_routed_question(self):
        item, _alice_sid, _alice_cid, hid = self.make_pending(
            to_agent="bob")
        bob_sid = self.make_session("bob", sid="s-bob")
        carol_sid = self.make_session("carol", sid="s-carol")
        accepted = coopdb.accept_handoff(
            self.conn, handoff_id=hid, session_id=bob_sid, actor="bob",
            intent="author my required peer question")

        question_id = coopdb.needs_input(
            self.conn, claim_id=accepted["claim_id"],
            session_id=bob_sid, to_agent="carol",
            question="Which recovery invariant matters most?")
        question = self.conn.execute(
            "SELECT * FROM questions WHERE question_id=?",
            (question_id,)).fetchone()
        self.assertEqual(question["asked_by_agent"], "bob")
        self.assertEqual(question["assigned_to_agent"], "carol")
        action = coopdb.status(
            self.conn, "carol", session_id=carol_sid,
            item_id=item)["next_action"]
        self.assertEqual(action["kind"], "answer_question")
        self.assertEqual(action["target_id"], question_id)

        response = coopdb.claim_question(
            self.conn, question_id=question_id, session_id=carol_sid,
            intent="answer Bob's question")
        coopdb.answer_question(
            self.conn, claim_id=response["claim_id"],
            session_id=carol_sid, answer="Lease ownership must stay live.")
        resume = coopdb.status(
            self.conn, "bob", session_id=bob_sid,
            item_id=item)["next_action"]
        self.assertEqual(resume["kind"], "resume_task")
        coopdb.claim_item(
            self.conn, item_id=item, actor="bob", session_id=bob_sid,
            intent="continue after Carol's answer",
            reclaim_reason="addressed answer received")
        item_row = self.conn.execute(
            "SELECT status, owner_agent_id, next_actor_agent_id FROM items "
            "WHERE id=?", (item,)).fetchone()
        self.assertEqual(dict(item_row), {
            "status": "working",
            "owner_agent_id": "bob",
            "next_actor_agent_id": "bob",
        })

    def test_nontarget_respond_refuses_with_zero_mutation(self):
        item, sid, cid, hid = self.make_pending()
        coopdb.register_agent(self.conn, "carol")
        csid = self.make_session("carol")
        for op in ("accept", "decline"):
            with self.subTest(op=op):
                before = hsnap(self.conn)
                with self.assertRaises(AddressedTargetMismatch) as caught:
                    if op == "accept":
                        coopdb.accept_handoff(
                            self.conn, handoff_id=hid, session_id=csid,
                            actor="carol", intent="mine now")
                    else:
                        coopdb.decline_handoff(
                            self.conn, handoff_id=hid, session_id=csid,
                            actor="carol", reason="not mine")
                self.assertEqual(
                    caught.exception.reason_code,
                    "addressed_target_mismatch",
                )
                self.assertEqual(caught.exception.evidence["handoff_id"], hid)
                self.assertEqual(
                    caught.exception.evidence["actor_agent_id"], "carol")
                self.assertEqual(
                    caught.exception.evidence["required_agent_id"], "bob")
                self.assertEqual(hsnap(self.conn), before)

    def test_respond_requires_text_and_pending_row(self):
        item, sid, cid, hid = self.make_pending()
        bsid = self.make_session("bob")
        with self.assertRaises(InvalidTransition):
            coopdb.accept_handoff(
                self.conn, handoff_id=hid, session_id=bsid, actor="bob",
                intent="  ")
        with self.assertRaises(InvalidTransition):
            coopdb.decline_handoff(
                self.conn, handoff_id=hid, session_id=bsid, actor="bob",
                reason="")
        with self.assertRaises(NotFound) as caught:
            coopdb.accept_handoff(
                self.conn, handoff_id=999, session_id=bsid, actor="bob",
                intent="ghost")
        self.assertEqual(caught.exception.reason_code, "target_not_found")
        self.assertEqual(caught.exception.evidence["handoff_id"], 999)
        coopdb.decline_handoff(
            self.conn, handoff_id=hid, session_id=bsid, actor="bob",
            reason="busy")
        with self.assertRaises(InvalidTransition):
            coopdb.accept_handoff(
                self.conn, handoff_id=hid, session_id=bsid, actor="bob",
                intent="changed my mind")

    def test_human_interface_cannot_respond(self):
        item, sid, cid, hid = self.make_pending()
        with self.assertRaises(HumanLaneViolation):
            coopdb.accept_handoff(
                self.conn, handoff_id=hid, session_id=None, actor="human",
                intent="human grab")
        with self.assertRaises(HumanLaneViolation):
            coopdb.decline_handoff(
                self.conn, handoff_id=hid, session_id=None, actor="human",
                reason="human decline")

    def test_decline_starts_grace_and_owner_resumes(self):
        item, sid, cid, hid = self.make_pending()
        bsid = self.make_session("bob")
        result = coopdb.decline_handoff(
            self.conn, handoff_id=hid, session_id=bsid, actor="bob",
            reason="over capacity")
        row = self.handoff_row(hid)
        self.assertEqual(row["status"], "declined")
        self.assertIsNotNone(row["resolved_at"])
        item_row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(item_row["status"], "working")
        self.assertEqual(item_row["owner_agent_id"], "alice")
        self.assertEqual(item_row["next_actor_agent_id"], "alice")
        self.assertEqual(
            item_row["preferred_resume_owner_agent_id"], "alice")
        self.assertIsNotNone(item_row["resume_grace_started_at"])
        self.assertEqual(
            item_row["resume_grace_expires_at"],
            result["grace_expires_at"])
        self.assertEqual(self.active_claims(item), [])
        delivered = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='alice' "
            "AND category='handoff_declined'").fetchall()
        self.assertEqual(len(delivered), 1)
        # The owner resumes with a fresh claim through the still-running
        # original session; the claim clears grace.
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="alice", session_id=sid,
            intent="resuming", reclaim_reason="handoff declined, resuming")
        item_row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()
        self.assertIsNone(item_row["resume_grace_expires_at"])
        self.assertIsNone(item_row["preferred_resume_owner_agent_id"])
        self.assertEqual(item_row["owner_agent_id"], "alice")

    def test_decline_grace_blocks_third_party_until_expiry(self):
        item, sid, cid, hid = self.make_pending()
        bsid = self.make_session("bob")
        coopdb.decline_handoff(
            self.conn, handoff_id=hid, session_id=bsid, actor="bob",
            reason="busy")
        coopdb.register_agent(self.conn, "carol")
        csid = self.make_session("carol")
        with self.assertRaises(InvalidTransition) as ctx:
            coopdb.claim_item(
                self.conn, item_id=item, actor="carol", session_id=csid,
                intent="takeover", reclaim_reason="impatient")
        self.assertIn("grace", str(ctx.exception))
        self.clock.advance(GRACE + 1)
        # Post-expiry controlled takeover: reason required, ownership moves.
        with self.assertRaises(InvalidTransition):
            coopdb.claim_item(
                self.conn, item_id=item, actor="carol", session_id=csid,
                intent="takeover")
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="carol", session_id=csid,
            intent="takeover", reclaim_reason="grace expired, taking over")
        item_row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(item_row["owner_agent_id"], "carol")


class Wedge(HandoffBoard):
    def test_unresponsive_target_is_a_visible_wedge(self):
        item, sid, cid, hid = self.make_pending()
        self.clock.advance(LEASE * 1000)
        coopdb.sweep_expired(self.conn)
        row = self.handoff_row(hid)
        self.assertEqual(row["status"], "pending")  # nothing times it out
        item_row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(item_row["status"], "handoff")
        for agent in ("alice", "bob"):
            with self.subTest(agent=agent):
                st = coopdb.status(self.conn, agent)
                wedged = [i for i in st["owned_items"]
                          if i["item_id"] == item]
                self.assertEqual(len(wedged), 1)
                self.assertEqual(wedged[0]["status"], "handoff")
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(packet["handoff"]["handoff_id"], hid)
        self.assertEqual(packet["handoff"]["to_agent"], "bob")


class EndToEnd(HandoffBoard):
    def test_completion_blocked_while_pending_end_to_end(self):
        # The gate's seeded-row proof lives in test_completion; this is the real
        # path: after create_handoff the originator's claim is closed, so
        # completion dies at the claim chokepoint and the item never
        # reaches done.
        item, sid, cid = self.make_working()
        self.submit(cid, sid)
        self.create_h(cid, sid)
        with self.assertRaises(StaleClaim):
            coopdb.complete_item(
                self.conn, claim_id=cid, session_id=sid, actor="alice")
        status = self.conn.execute(
            "SELECT status FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(status["status"], "handoff")


class HandoffRaces(HandoffBoard):
    def _race(self, fns):
        barrier = threading.Barrier(len(fns))
        results = [None] * len(fns)

        def run(i, fn):
            conn = coopdb.connect(self.db)
            try:
                barrier.wait(timeout=10)
                try:
                    results[i] = ("ok", fn(conn))
                except CoopError as exc:
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

    def test_claim_vs_accept_race(self):
        item, sid, cid, hid = self.make_pending()
        bsid = self.make_session("bob")
        coopdb.register_agent(self.conn, "carol")
        csid = self.make_session("carol")

        def do_claim(conn):
            return coopdb.claim_item(
                conn, item_id=item, actor="carol", session_id=csid,
                intent="steal", reclaim_reason="racing")

        def do_accept(conn):
            return coopdb.accept_handoff(
                conn, handoff_id=hid, session_id=bsid, actor="bob",
                intent="taking it")

        results = self._race([do_claim, do_accept])
        self.assertEqual(results[1][0], "ok", results[1][1])
        self.assertEqual(results[0][0], "err")
        self.assertIsInstance(
            results[0][1], (InvalidTransition, ClaimCollision))
        active = self.active_claims(item)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["claimed_by_agent"], "bob")
        self.assertEqual(self.handoff_row(hid)["status"], "accepted")

    def test_claim_vs_decline_race(self):
        item, sid, cid, hid = self.make_pending()
        bsid = self.make_session("bob")
        coopdb.register_agent(self.conn, "carol")
        csid = self.make_session("carol")

        def do_claim(conn):
            return coopdb.claim_item(
                conn, item_id=item, actor="carol", session_id=csid,
                intent="steal", reclaim_reason="racing")

        def do_decline(conn):
            return coopdb.decline_handoff(
                conn, handoff_id=hid, session_id=bsid, actor="bob",
                reason="busy")

        results = self._race([do_claim, do_decline])
        self.assertEqual(results[1][0], "ok", results[1][1])
        self.assertEqual(results[0][0], "err")
        self.assertIsInstance(results[0][1], InvalidTransition)
        self.assertEqual(self.handoff_row(hid)["status"], "declined")
        self.assertEqual(self.active_claims(item), [])
        item_row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(
            item_row["preferred_resume_owner_agent_id"], "alice")


class HandoffCLI(HandoffBoard):
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

    def test_cli_full_cycle_json(self):
        item, sid, cid = self.make_working()
        coopdb.register_agent(self.conn, "bob")
        code, out, err = self._run(
            ["--json", "handoff", "create", "--claim", str(cid),
             "--to", "bob", "--reason", "r", "--summary", "s",
             "--completed", "c", "--remaining", "w", "--risks", "k",
             "--next-action", "n", "--proof-ref", FILE_REF],
            {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"})
        self.assertEqual(code, 0, err)
        hid = json.loads(out)["handoff_id"]
        self.assertNotIn("fencing_token", out)
        bsid = self.make_session("bob")
        code, out, err = self._run(
            ["--json", "handoff", "accept", "--id", str(hid),
             "--intent", "on it"],
            {"COOP_SESSION_ID": bsid, "COOP_AGENT": "bob"})
        self.assertEqual(code, 0, err)
        accepted = json.loads(out)
        self.assertEqual(accepted["handoff_id"], hid)
        self.assertIn("claim_id", accepted)
        self.assertNotIn("fencing_token", out)

    def test_cli_decline(self):
        item, sid, cid, hid = self.make_pending()
        bsid = self.make_session("bob")
        code, out, err = self._run(
            ["--json", "handoff", "decline", "--id", str(hid),
             "--reason", "over capacity"],
            {"COOP_SESSION_ID": bsid, "COOP_AGENT": "bob"})
        self.assertEqual(code, 0, err)
        declined = json.loads(out)
        self.assertEqual(declined["handoff_id"], hid)
        self.assertIn("grace_expires_at", declined)
        self.assertNotIn("fencing_token", out)


class TargetLivenessSignal(HandoffBoard):
    """R9 soft peer-online signal: informative field, never a guard."""

    def test_offline_target_reports_not_live_and_still_creates(self):
        item, sid, cid = self.make_working()
        result = self.create_h(cid, sid)  # bob registered, no session
        self.assertFalse(result["target_session_live"])
        self.assertIsNotNone(self.handoff_row(result["handoff_id"]))

    def test_running_target_reports_live(self):
        item, sid, cid = self.make_working()
        self.make_session("bob")
        result = self.create_h(cid, sid)
        self.assertTrue(result["target_session_live"])


if __name__ == "__main__":
    unittest.main()
