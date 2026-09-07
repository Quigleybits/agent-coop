"""The guarded completion gate.

`complete_item` runs eight steps in one transaction: claim ->
contract -> current receipt (version match) -> collect-then-classify
evidence re-hash/re-lint -> qualifying approval (or recorded waiver from
working) -> no open question, no pending handoff -> done. File-evidence
failures dominate and take the recorded path: the supersede and its event
COMMIT, the core RETURNS a failed outcome, and only the command layer
raises `receipt_hash_mismatch`. Board-row failures roll back clean.
"""

import contextlib
import io
import json
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop.coop_errors import (
    IncompleteContract,
    InvalidTransition,
    ProofReferenceInvalid,
    ReceiptMissing,
    ReceiptStale,
    ReviewMissing,
    SelfReview,
    StaleClaim,
)
from tests.test_claims import LEASE
from tests.test_reviews import ReviewBoard, snap


class CompletionBoard(ReviewBoard):
    def approve(self, review_id, reviewer="bob", rsid=None, provider=None):
        if rsid is None:
            # Honour an already-bound provider (binding-second-review
            # trio tests bind bob=codex etc. before make_session).
            row = self.conn.execute(
                "SELECT provider FROM agents WHERE name=?",
                (reviewer,)).fetchone()
            prov = provider or (
                row["provider"] if row and row["provider"] else "claude")
            rsid = self.make_session(reviewer, provider=prov)
        claim, _packet = self.claim_rev(review_id, rsid)
        coopdb.submit_verdict(
            self.conn, claim_id=claim["claim_id"], session_id=rsid,
            actor=reviewer, verdict="approve")
        return rsid

    def make_approved(self, agent="alice", reviewer="bob", refs=None,
                      path=None, **item_over):
        item, sid, cid = self.make_working(agent, **item_over)
        receipt_id = self.submit(cid, sid, agent=agent, path=path, refs=refs)
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor=agent)
        self.approve(review_id, reviewer=reviewer)
        return item, sid, cid, receipt_id, review_id

    def complete(self, cid, sid, agent="alice"):
        return coopdb.complete_item(
            self.conn, claim_id=cid, session_id=sid, actor=agent)

    def seed_open_question(self, item, agent="alice", sid=None):
        def _seed(conn):
            cur = conn.execute(
                "INSERT INTO questions(item_id,exact_question,asked_by_agent,"
                "asked_by_session,assigned_to_agent,status,asked_at) "
                "VALUES (?,?,?,?,?,'open',?)",
                (item, "blocked?", agent, sid, "human", coopdb.now()))
            return cur.lastrowid
        return coopdb.mutate(self.conn, _seed)

    def seed_pending_handoff(self, item, cid, sid, agent="alice",
                             to_agent="bob"):
        coopdb.register_agent(self.conn, to_agent)
        def _seed(conn):
            cur = conn.execute(
                "INSERT INTO handoffs(item_id,claim_id,from_agent,"
                "from_session,execution_fencing_token,to_agent,reason,"
                "summary,completed_work,remaining_work,risks,"
                "proof_references,suggested_next_action,status,created_at) "
                "VALUES (?,?,?,?,1,?,'r','s','c','w','k','[]','n',"
                "'pending',?)",
                (item, cid, agent, sid, to_agent, coopdb.now()))
            return cur.lastrowid
        return coopdb.mutate(self.conn, _seed)

    def item_row(self, item):
        return self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()

    def claim_row(self, cid):
        return self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (cid,)).fetchone()

    def events(self, item, etype=None):
        if etype is None:
            return self.conn.execute(
                "SELECT * FROM events WHERE item_id=? ORDER BY event_id",
                (item,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM events WHERE item_id=? AND event_type=? "
            "ORDER BY event_id", (item, etype)).fetchall()

    def deliveries(self, recipient):
        return self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id=? "
            "ORDER BY inbox_entry_id", (recipient,)).fetchall()

    def _run(self, argv, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"COOP_DB": self.db, **(env or {})}
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


class GateLadder(CompletionBoard):
    def test_happy_path_review_required_completes(self):
        item, sid, cid, receipt_id, _rev = self.make_approved()
        result = self.complete(cid, sid)
        self.assertEqual(result["completed"], item)
        self.assertIn("event_id", result)
        row = self.item_row(item)
        self.assertEqual(row["status"], "done")
        claim = self.claim_row(cid)
        self.assertEqual(claim["status"], "completed")
        self.assertEqual(claim["close_reason"], "completion")
        done_events = self.events(item, "item_completed")
        self.assertEqual(len(done_events), 1)
        payload = json.loads(done_events[0]["payload_json"])
        self.assertEqual(payload["receipt_id"], receipt_id)
        # the creator (human) is not the actor -> exactly one delivery
        rows = [r for r in self.deliveries("human")
                if r["category"] == "completion"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_id"], item)

    def test_incomplete_contract_refuses(self):
        item, sid, cid, _receipt, _rev = self.make_approved()
        self.conn.execute(
            "UPDATE items SET objective='' WHERE id=?", (item,))
        self.conn.commit()
        before = snap(self.conn)
        with self.assertRaises(IncompleteContract) as caught:
            self.complete(cid, sid)
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(caught.exception.reason_code, "contract_incomplete")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "complete_contract_required",
        )

    def test_no_current_receipt_refuses_receipt_missing(self):
        item, sid, cid = self.make_working()
        before = snap(self.conn)
        with self.assertRaises(ReceiptMissing) as caught:
            self.complete(cid, sid)
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(self.item_row(item)["status"], "working")
        self.assertEqual(caught.exception.reason_code, "receipt_missing")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["claim_id"], cid)
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "current_receipt_required",
        )

    def test_version_mismatch_refuses_receipt_stale(self):
        item, sid, cid, receipt_id, _rev = self.make_approved()
        self.conn.execute(
            "UPDATE items SET contract_version=2 WHERE id=?", (item,))
        self.conn.commit()
        before = snap(self.conn)
        with self.assertRaises(ReceiptStale) as caught:
            self.complete(cid, sid)
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(caught.exception.reason_code, "receipt_stale")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["receipt_id"], receipt_id)
        self.assertEqual(caught.exception.evidence["contract_version"], 1)
        self.assertEqual(
            caught.exception.evidence["required_contract_version"], 2)

    def test_waiver_path_completes_with_receipt_only(self):
        item, sid, cid = self.make_working(
            review_waiver_reason="docs only")
        self.submit(cid, sid)
        result = self.complete(cid, sid)
        self.assertEqual(result["completed"], item)
        self.assertEqual(self.item_row(item)["status"], "done")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM reviews WHERE item_id=?",
                (item,)).fetchone()["c"], 0)

    def test_waived_item_with_seeded_null_reason_refuses(self):
        item, sid, cid = self.make_working(
            review_waiver_reason="docs only")
        self.submit(cid, sid)
        self.conn.execute(
            "UPDATE items SET review_waiver_reason=NULL WHERE id=?", (item,))
        self.conn.commit()
        with self.assertRaises(ReviewMissing):
            self.complete(cid, sid)


class EvidencePass(CompletionBoard):
    def test_file_change_commits_supersede_and_returns_failed(self):
        path = self.evidence_file("primary.md", b"original\n")
        item, sid, cid, receipt_id, _rev = self.make_approved(path=path)
        path.write_bytes(b"tampered after approval\n")
        result = self.complete(cid, sid)
        self.assertEqual(result["failed"], "receipt_hash_mismatch")
        self.assertEqual(result["superseded_receipt_id"], receipt_id)
        kinds = {(f["kind"], f["failure"]) for f in result["failures"]}
        self.assertEqual(kinds, {("file", "changed")})
        # the supersede and its event are COMMITTED
        row = self.conn.execute(
            "SELECT superseded_at FROM receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
        self.assertIsNotNone(row["superseded_at"])
        sup = self.events(item, "receipt_superseded")
        self.assertEqual(len(sup), 1)
        payload = json.loads(sup[0]["payload_json"])
        self.assertEqual(payload["reason"], "receipt_hash_mismatch")
        self.assertEqual(len(payload["failures"]), 1)
        # nothing completed; the implementation claim stays live
        self.assertEqual(self.item_row(item)["status"], "review")
        self.assertEqual(self.claim_row(cid)["status"], "active")
        self.assertEqual(self.events(item, "item_completed"), [])

    def test_stale_approval_does_not_carry_to_replacement_receipt(self):
        path = self.evidence_file("primary.md", b"original\n")
        item, sid, cid, receipt_id, _rev = self.make_approved(path=path)
        path.write_bytes(b"tampered\n")
        self.complete(cid, sid)  # recorded failure, receipt superseded
        # the replacement path exits review even with no current receipt
        fresh = self.evidence_file("fresh.md", b"fresh evidence\n")
        new_receipt = self.submit(cid, sid, path=fresh)
        self.assertEqual(self.item_row(item)["status"], "working")
        # the old approval binds the superseded receipt: not qualifying
        with self.assertRaises(ReviewMissing):
            self.complete(cid, sid)
        self.assertNotEqual(new_receipt, receipt_id)

    def seed_attached_decision(self, item):
        def _seed(conn):
            cur = conn.execute(
                "INSERT INTO decisions(item_id,text,rationale,decided_by,"
                "decided_by_agent,legacy,created_at) VALUES "
                "(?,?,?,?,?,1,?)",
                (item, "seeded", None, "human", "human", coopdb.now()))
            return cur.lastrowid
        return coopdb.mutate(self.conn, _seed)

    def test_mixed_failure_lands_file_dominant_with_both_listed(self):
        item, sid, cid = self.make_working()
        did = self.seed_attached_decision(item)
        refpath = self.evidence_file("ref.md", b"ref bytes\n")
        primary = self.evidence_file("primary.md", b"primary\n")
        receipt_id = self.submit(
            cid, sid, path=primary,
            refs=[f"decision:{did}", f"file:{refpath}"])
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.approve(review_id)
        refpath.write_bytes(b"ref changed\n")
        other = self.make_item()
        self.conn.execute(
            "UPDATE decisions SET item_id=? WHERE id=?",
            (other, did))
        self.conn.commit()
        result = self.complete(cid, sid)
        self.assertEqual(result["failed"], "receipt_hash_mismatch")
        kinds = {(f["kind"], f["failure"]) for f in result["failures"]}
        self.assertIn(("file", "changed"), kinds)
        self.assertIn(("board", "unattached"), kinds)
        row = self.conn.execute(
            "SELECT superseded_at FROM receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
        self.assertIsNotNone(row["superseded_at"])

    def test_board_only_failure_rolls_back_clean(self):
        item, sid, cid = self.make_working()
        did = self.seed_attached_decision(item)
        receipt_id = self.submit(cid, sid, refs=[f"decision:{did}"])
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.approve(review_id)
        other = self.make_item()
        self.conn.execute(
            "UPDATE decisions SET item_id=? WHERE id=?",
            (other, did))
        self.conn.commit()
        before = snap(self.conn)
        with self.assertRaises(ProofReferenceInvalid) as caught:
            self.complete(cid, sid)
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(caught.exception.reason_code, "proof_invalid")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["receipt_id"], receipt_id)
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "proof_reference_invalid",
        )
        row = self.conn.execute(
            "SELECT superseded_at FROM receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
        self.assertIsNone(row["superseded_at"])

    def test_missing_primary_file_reports_missing(self):
        path = self.evidence_file("primary.md", b"original\n")
        item, sid, cid, receipt_id, _rev = self.make_approved(path=path)
        path.unlink()
        result = self.complete(cid, sid)
        self.assertEqual(result["failed"], "receipt_hash_mismatch")
        kinds = {(f["kind"], f["failure"]) for f in result["failures"]}
        self.assertEqual(kinds, {("file", "missing")})


class ApprovalLeg(CompletionBoard):
    def test_no_review_requested_refuses_review_missing(self):
        item, sid, cid = self.make_working()
        self.submit(cid, sid)
        before = snap(self.conn)
        with self.assertRaises(ReviewMissing) as caught:
            self.complete(cid, sid)
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(caught.exception.reason_code, "review_missing")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["required_count"], 1)
        self.assertEqual(caught.exception.evidence["observed_count"], 0)

    def test_changes_verdict_then_fresh_receipt_refuses(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        rsid = self.make_session("bob")
        claim, _ = self.claim_rev(review_id, rsid)
        coopdb.submit_verdict(
            self.conn, claim_id=claim["claim_id"], session_id=rsid,
            actor="bob", verdict="changes", body="needs a fresh receipt")
        self.assertEqual(self.item_row(item)["status"], "working")
        self.submit(cid, sid, path=self.evidence_file("fresh.md", b"f\n"))
        with self.assertRaises(ReviewMissing):
            self.complete(cid, sid)

    def test_changes_verdict_requires_a_reason(self):
        # REVIEW-INTEGRITY: an empty 'changes' rejection is un-actionable and
        # stalls the owner (item 19, run3). It must carry a reason.
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        rsid = self.make_session("bob")
        claim, _ = self.claim_rev(review_id, rsid)
        with self.assertRaises(coopdb.InvalidTransition):
            coopdb.submit_verdict(
                self.conn, claim_id=claim["claim_id"], session_id=rsid,
                actor="bob", verdict="changes")

    def test_post_verdict_decision_kills_the_approval(self):
        item, sid, cid, _receipt, _rev = self.make_approved()
        coopdb.record_decision(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            text="scope narrowed after approval")
        with self.assertRaises(ReviewMissing):
            self.complete(cid, sid)

    def test_voluntary_review_binds_on_waived_item(self):
        item, sid, cid = self.make_working(
            review_waiver_reason="docs only")
        self.submit(cid, sid)
        coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.assertEqual(self.item_row(item)["status"], "review")
        with self.assertRaises(ReviewMissing):
            self.complete(cid, sid)

    def test_reviewer_reclaims_then_self_completes_refused(self):
        item, sid, cid = self.make_working("alice")
        self.submit(cid, sid)
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        bsid = self.approve(review_id, reviewer="bob")
        # alice's claim lapses; the sweep flips it stale; alice's session
        # ends; bob (the approving reviewer, session still live) reclaims.
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        self.conn.execute(
            "UPDATE sessions SET status='exited' WHERE session_id=?", (sid,))
        self.conn.commit()
        reclaim = self.claim(item, "bob", bsid,
                             reclaim_reason="finishing the approved work")
        self.assertEqual(self.item_row(item)["owner_agent_id"], "bob")
        before = snap(self.conn)
        with self.assertRaises(SelfReview) as caught:
            self.complete(reclaim["claim_id"], bsid, agent="bob")
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(caught.exception.reason_code, "reviewer_is_owner")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["review_id"], review_id)
        self.assertEqual(caught.exception.evidence["actor_agent_id"], "bob")
        self.assertEqual(caught.exception.evidence["owner_agent_id"], "bob")

    def test_fresh_verdict_route_end_to_end(self):
        # approve -> decision kills it -> refused -> replacement receipt
        # from review -> re-request from working -> claim carries the
        # decision -> fresh approval -> complete. Matrix-legal throughout.
        item, sid, cid, _receipt, _rev = self.make_approved(reviewer="bob")
        coopdb.record_decision(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            text="binding decision after approval")
        with self.assertRaises(ReviewMissing):
            self.complete(cid, sid)
        self.assertEqual(self.item_row(item)["status"], "review")
        fresh = self.evidence_file("fresh.md", b"fresh\n")
        self.submit(cid, sid, path=fresh)
        self.assertEqual(self.item_row(item)["status"], "working")
        review2 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        rsid = self.make_session("carol")
        claim2, packet = self.claim_rev(review2, rsid)
        texts = [d.get("text") for d in packet["decisions"]]
        self.assertIn("binding decision after approval", texts)
        coopdb.submit_verdict(
            self.conn, claim_id=claim2["claim_id"], session_id=rsid,
            actor="carol", verdict="approve")
        result = self.complete(cid, sid)
        self.assertEqual(result["completed"], item)
        self.assertEqual(self.item_row(item)["status"], "done")


class Blockers(CompletionBoard):
    def test_open_question_blocks(self):
        item, sid, cid, _receipt, _rev = self.make_approved()
        qid = self.seed_open_question(item, sid=sid)
        with self.assertRaises(InvalidTransition) as ctx:
            self.complete(cid, sid)
        self.assertIn("question", str(ctx.exception))
        self.assertEqual(ctx.exception.reason_code, "blocking_work_open")
        self.assertEqual(ctx.exception.evidence["item_id"], item)
        self.assertEqual(
            ctx.exception.evidence["blocking_object_type"], "question")
        self.assertEqual(ctx.exception.evidence["blocking_ids"], (qid,))
        self.conn.execute(
            "UPDATE questions SET status='answered' WHERE question_id=?",
            (qid,))
        self.conn.commit()
        self.assertEqual(self.complete(cid, sid)["completed"], item)

    def test_pending_handoff_blocks(self):
        item, sid, cid, _receipt, _rev = self.make_approved()
        hid = self.seed_pending_handoff(item, cid, sid)
        with self.assertRaises(InvalidTransition) as ctx:
            self.complete(cid, sid)
        self.assertIn("handoff", str(ctx.exception))
        self.assertEqual(ctx.exception.reason_code, "blocking_work_open")
        self.assertEqual(ctx.exception.evidence["item_id"], item)
        self.assertEqual(
            ctx.exception.evidence["blocking_object_type"], "handoff")
        self.assertEqual(ctx.exception.evidence["blocking_ids"], (hid,))
        self.conn.execute(
            "UPDATE handoffs SET status='declined' WHERE handoff_id=?",
            (hid,))
        self.conn.commit()
        self.assertEqual(self.complete(cid, sid)["completed"], item)


class TokenAbsence(CompletionBoard):
    def test_no_token_on_any_completion_surface(self):
        path = self.evidence_file("primary.md", b"original\n")
        item, sid, cid, _receipt, _rev = self.make_approved(path=path)
        result = self.complete(cid, sid)
        self.assertNotIn("fencing_token", json.dumps(result))
        for row in self.events(item):
            self.assertNotIn("fencing_token", row["payload_json"])
        for row in self.deliveries("human"):
            self.assertNotIn("fencing_token", row["payload_json"])


class BindingSecondReview(CompletionBoard):
    """Binding quorum is explicit per item; independence unit is provider."""

    def _bind(self, agent, provider):
        """Ensure agent is registered under a distinct provider (sessions
        default every agent to 'claude' — override explicitly)."""
        row = self.conn.execute(
            "SELECT provider FROM agents WHERE name=?", (agent,)).fetchone()
        if row is None:
            coopdb.register_or_bind_agent(
                self.conn, agent_id=agent, provider=provider)
        elif row["provider"] != provider:
            # Tests that already bound via make_session(provider=claude)
            # need a clean re-bind only when the default collided.
            self.conn.execute(
                "UPDATE agents SET provider=? WHERE name=?",
                (provider, agent))
            self.conn.commit()

    def _trio(self):
        """alice=claude owner, bob=codex, carol=grok — three providers."""
        self._bind("alice", "claude")
        self._bind("bob", "codex")
        self._bind("carol", "grok")
        self.assertEqual(coopdb.min_approving_providers(self.conn), 1)

    def test_d9_two_provider_board_one_approve_still_completes(self):
        # Default make_session binds both alice and bob to claude → one
        # provider on the board → min=1 (today's rule).
        item, sid, cid, _r, _rev = self.make_approved()
        providers = coopdb._distinct_board_providers(self.conn)
        self.assertLess(len(providers), 3)
        self.assertEqual(coopdb.min_approving_providers(self.conn), 1)
        result = self.complete(cid, sid)
        self.assertEqual(result["completed"], item)

    def test_default_quorum_is_one_even_when_three_providers_are_present(self):
        self._bind("alice", "claude")
        self._bind("bob", "codex")
        self._bind("carol", "grok")
        item, sid, cid, _receipt, _review = self.make_approved(
            agent="alice", reviewer="bob")
        self.assertEqual(coopdb.review_quorum(self.conn, item), 1)
        self.assertEqual(self.complete(cid, sid, agent="alice")["completed"],
                         item)

    def test_explicit_two_review_quorum_refuses_one_approval(self):
        self._bind("alice", "claude")
        self._bind("bob", "codex")
        self._bind("carol", "grok")
        item, sid, cid = self.make_working(
            "alice", review_quorum=2)
        self.submit(cid, sid, agent="alice")
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.approve(review_id, reviewer="bob")
        self.assertEqual(coopdb.review_quorum(self.conn, item), 2)
        with self.assertRaises(ReviewMissing) as ctx:
            self.complete(cid, sid, agent="alice")
        self.assertIn("needs 2", str(ctx.exception))
        self.assertEqual(ctx.exception.reason_code, "review_missing")
        self.assertEqual(ctx.exception.evidence["item_id"], item)
        self.assertEqual(ctx.exception.evidence["required_count"], 2)
        self.assertEqual(ctx.exception.evidence["observed_count"], 1)

    def test_three_providers_one_approve_refuses(self):
        self._trio()
        item, sid, cid = self.make_working("alice", review_quorum=2)
        self.submit(cid, sid, agent="alice")
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.approve(review_id, reviewer="bob")
        with self.assertRaises(ReviewMissing) as ctx:
            self.complete(cid, sid, agent="alice")
        self.assertIn("needs 2", str(ctx.exception))
        self.assertIn("have 1", str(ctx.exception))

    def test_three_providers_two_distinct_approves_completes(self):
        self._trio()
        item, sid, cid = self.make_working("alice", review_quorum=2)
        self.submit(cid, sid, agent="alice")
        r1 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            reviewer="bob")
        self.approve(r1, reviewer="bob")
        # Second request from status=review (no live review).
        r2 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            reviewer="carol")
        self.approve(r2, reviewer="carol")
        result = self.complete(cid, sid, agent="alice")
        self.assertEqual(result["completed"], item)
        self.assertEqual(self.item_row(item)["status"], "done")

    def test_two_approves_same_provider_do_not_count_as_two(self):
        """Same provider cannot even open the redundant second review."""
        self._trio()
        self._bind("bob2", "codex")  # second codex agent
        item, sid, cid = self.make_working("alice", review_quorum=2)
        self.submit(cid, sid, agent="alice")
        r1 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            reviewer="bob")
        self.approve(r1, reviewer="bob")
        with self.assertRaises(InvalidTransition) as ctx:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=sid, actor="alice",
                reviewer="bob2")
        self.assertEqual(
            ctx.exception.reason_code, "review_provider_already_approved")
        self.assertEqual(coopdb.approvals_still_needed(self.conn, item), 1)
        with self.assertRaises(ReviewMissing) as complete_ctx:
            self.complete(cid, sid, agent="alice")
        self.assertIn("have 1", str(complete_ctx.exception))

    def test_second_request_allowed_from_review_status(self):
        self._trio()
        item, sid, cid = self.make_working("alice", review_quorum=2)
        self.submit(cid, sid, agent="alice")
        r1 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.approve(r1, reviewer="bob")
        self.assertEqual(self.item_row(item)["status"], "review")
        r2 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            reviewer="carol")
        row = self.conn.execute(
            "SELECT status, reviewer FROM reviews WHERE id=?",
            (r2,)).fetchone()
        self.assertEqual(row["status"], "requested")
        self.assertEqual(row["reviewer"], "carol")

    def test_approvals_still_needed_helper(self):
        self._trio()
        item, sid, cid = self.make_working("alice", review_quorum=2)
        self.submit(cid, sid, agent="alice")
        self.assertEqual(coopdb.approvals_still_needed(self.conn, item), 2)
        r1 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.approve(r1, reviewer="bob")
        self.assertEqual(coopdb.approvals_still_needed(self.conn, item), 1)
        r2 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            reviewer="carol")
        self.approve(r2, reviewer="carol")
        self.assertEqual(coopdb.approvals_still_needed(self.conn, item), 0)


class CompletionCli(CompletionBoard):
    def _env(self, sid, agent="alice"):
        return {"COOP_SESSION_ID": sid, "COOP_AGENT": agent}

    def test_cli_complete_happy_json(self):
        item, sid, cid, _receipt, _rev = self.make_approved()
        code, out, err = self._run(
            ["--json", "item", "complete", "--claim", str(cid)],
            self._env(sid))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["completed"], item)
        self.assertIn("event_id", data)

    def test_cli_failed_outcome_raises_after_commit(self):
        path = self.evidence_file("primary.md", b"original\n")
        item, sid, cid, receipt_id, _rev = self.make_approved(path=path)
        path.write_bytes(b"tampered\n")
        code, out, err = self._run(
            ["--json", "item", "complete", "--claim", str(cid)],
            self._env(sid))
        self.assertEqual(code, 1)
        error = json.loads(err)["error"]
        self.assertEqual(error["type"], "receipt_hash_mismatch")
        self.assertEqual(error["reason_code"], "receipt_hash_mismatch")
        self.assertEqual(
            error["evidence"],
            {
                "item_id": item,
                "receipt_id": receipt_id,
                "constraint": "receipt_bytes_changed",
            },
        )
        row = self.conn.execute(
            "SELECT superseded_at FROM receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
        self.assertIsNotNone(row["superseded_at"])
        self.assertEqual(len(self.events(item, "receipt_superseded")), 1)

    def test_cli_dead_claim_refuses_stale(self):
        item, sid, cid, _receipt, _rev = self.make_approved()
        self.conn.execute(
            "UPDATE claims SET status='closed', closed_at=?, "
            "close_reason='matrix' WHERE claim_id=?",
            (coopdb.now(), cid))
        self.conn.commit()
        with self.assertRaises(StaleClaim):
            self.complete(cid, sid)


if __name__ == "__main__":
    unittest.main()
