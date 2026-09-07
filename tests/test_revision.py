"""Contract revision — the human lane.

`revise_item` is the trusted-local operator surface: no
session, `human` actor, rejected inside a supervised session. It merges
changed fields over the current contract, re-validates the MERGED result
(the legacy unlock), bumps `contract_version`, supersedes any
current receipt, and never touches grace routing. The by-status table is
exact: todo/working/blocked keep status, answered-`needs_input` keeps its
grace, `review` resets to `working`; open questions, pending handoffs,
`done`, live claims, and stale-running lanes refuse.
"""

import json
import unittest.mock

from agent_coop import coopdb
from agent_coop.coop_errors import (
    ClaimCollision,
    HumanLaneViolation,
    IncompleteContract,
    InvalidTransition,
    NotFound,
    UnsafeReclaim,
)
from tests.test_claims import LEASE
from tests.test_completion import CompletionBoard
from tests.test_reviews import snap


class RevisionBoard(CompletionBoard):
    def revise(self, item, reason="operator revision", fields=None,
               contract_path=None):
        return coopdb.revise_item(
            self.conn, item_id=item, reason=reason, fields=fields or {},
            contract_path=contract_path)

    def release(self, cid, sid, agent="alice"):
        coopdb.release_claim(
            self.conn, claim_id=cid, actor=agent, session_id=sid,
            reason="stepping away")

    def quiet_review(self, agent="alice", reviewer=None):
        """Item in `review` with a live requested review and NO active
        claim on any lane."""
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            agent, reviewer=reviewer)
        self.release(cid, sid, agent)
        return item, sid, receipt_id, review_id

    def seed_legacy_item(self, title="legacy import"):
        """A migration-shaped row: contract fields null, unclaimable until
        revision fills every one."""
        def _seed(conn):
            coopdb.register_agent(conn, "human")
            cur = conn.execute(
                "INSERT INTO items(title,status,contract_version,"
                "created_by,review_required,created_at,updated_at) "
                "VALUES (?,'todo',1,'human',1,?,?)",
                (title, coopdb.now(), coopdb.now()))
            return cur.lastrowid
        return coopdb.mutate(self.conn, _seed)


class ByStatusTable(RevisionBoard):
    def test_todo_keeps_status_and_bumps_version(self):
        item = self.make_item()
        result = self.revise(item, fields={"objective": "sharper objective"})
        row = self.item_row(item)
        self.assertEqual(row["status"], "todo")
        self.assertEqual(row["contract_version"], 2)
        self.assertEqual(row["objective"], "sharper objective")
        self.assertEqual(result["contract_version"], 2)

    def test_working_after_release_keeps_status(self):
        item, sid, cid = self.make_working()
        self.release(cid, sid)
        self.revise(item, fields={"scope": "narrower"})
        row = self.item_row(item)
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["contract_version"], 2)

    def test_working_declined_handoff_grace_survives(self):
        item, sid, cid = self.make_working()
        self.submit(cid, sid)
        bob_sid = self.make_session("bob")
        h = coopdb.create_handoff(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            to_agent="bob", reason="r", summary="s", completed="c",
            remaining="w", risks="k", next_action="n",
            proof_refs=[f"event:{self.events(item)[0]['event_id']}"])
        coopdb.decline_handoff(
            self.conn, handoff_id=h["handoff_id"], session_id=bob_sid,
            actor="bob", reason="busy")
        before = self.item_row(item)
        self.assertIsNotNone(before["resume_grace_expires_at"])
        self.revise(item, fields={"context": "revised mid-grace"})
        after = self.item_row(item)
        self.assertEqual(after["status"], "working")
        self.assertEqual(after["resume_grace_started_at"],
                         before["resume_grace_started_at"])
        self.assertEqual(after["resume_grace_expires_at"],
                         before["resume_grace_expires_at"])
        self.assertEqual(after["preferred_resume_owner_agent_id"],
                         before["preferred_resume_owner_agent_id"])

    def test_blocked_keeps_status(self):
        item, sid, cid = self.make_working()
        coopdb.checkpoint(
            self.conn, ctype="blocked", claim_id=cid, actor="alice",
            session_id=sid, note="stuck on env")
        self.assertEqual(self.item_row(item)["status"], "blocked")
        self.revise(item, fields={"done_when": "clearer done"})
        row = self.item_row(item)
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["contract_version"], 2)

    def test_answered_needs_input_keeps_grace_then_owner_resumes(self):
        item, sid, cid = self.make_working()
        coopdb.register_agent(self.conn, "bob")
        bob = self.make_session("bob", sid="s-bob-rev")
        qid = coopdb.needs_input(
            self.conn, claim_id=cid, session_id=sid, to_agent="bob",
            question="which flavor?")
        qclaim = coopdb.claim_question(
            self.conn, question_id=qid, session_id=bob,
            intent="answer flavor")
        coopdb.answer_question(
            self.conn, claim_id=qclaim["claim_id"], session_id=bob,
            answer="vanilla")
        before = self.item_row(item)
        self.assertEqual(before["status"], "needs_input")
        self.assertIsNotNone(before["resume_grace_expires_at"])
        self.revise(item, fields={"context": "now with the answer"})
        after = self.item_row(item)
        self.assertEqual(after["status"], "needs_input")
        self.assertEqual(after["contract_version"], 2)
        self.assertEqual(after["resume_grace_started_at"],
                         before["resume_grace_started_at"])
        self.assertEqual(after["resume_grace_expires_at"],
                         before["resume_grace_expires_at"])
        self.assertEqual(after["preferred_resume_owner_agent_id"], "alice")
        # The needs-input -> answer -> revise -> resume path: the owner
        # claims back into the NEW contract version inside the grace window.
        resumed = self.claim(item, "alice", sid, intent="resume",
                             reclaim_reason="resuming after answer")
        self.assertIsNotNone(resumed["claim_id"])
        final = self.item_row(item)
        self.assertEqual(final["status"], "working")
        self.assertEqual(final["contract_version"], 2)

    def test_review_resets_to_working_and_supersedes_receipt(self):
        item, sid, receipt_id, review_id = self.quiet_review()
        self.revise(item, fields={"objective": "reviewed differently"})
        row = self.item_row(item)
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["contract_version"], 2)
        receipt = self.conn.execute(
            "SELECT * FROM receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
        self.assertIsNotNone(receipt["superseded_at"])
        superseded = self.events(item, "receipt_superseded")
        self.assertEqual(len(superseded), 1)
        payload = json.loads(superseded[0]["payload_json"])
        self.assertEqual(payload["reason"], "revision")
        self.assertEqual(payload["receipt_id"], receipt_id)
        # The requested review is dead by derivation now.
        self.assertIsNone(coopdb._live_review(self.conn, item))

    def test_done_refuses(self):
        item, sid, cid, receipt_id, review_id = self.make_approved()
        self.complete(cid, sid)
        before = snap(self.conn)
        with self.assertRaisesRegex(InvalidTransition, "done"):
            self.revise(item, fields={"objective": "too late"})
        self.assertEqual(before, snap(self.conn))

    def test_open_question_refuses(self):
        item, sid, cid = self.make_working()
        coopdb.register_agent(self.conn, "bob")
        self.make_session("bob", sid="s-bob-open-q")
        qid = coopdb.needs_input(
            self.conn, claim_id=cid, session_id=sid, to_agent="bob",
            question="open?")
        before = snap(self.conn)
        with self.assertRaisesRegex(InvalidTransition, f"question {qid}"):
            self.revise(item, fields={"objective": "while open"})
        self.assertEqual(before, snap(self.conn))

    def test_pending_handoff_refuses(self):
        item, sid, cid = self.make_working()
        self.submit(cid, sid)
        coopdb.register_agent(self.conn, "bob")
        h = coopdb.create_handoff(
            self.conn, claim_id=cid, session_id=sid, actor="alice",
            to_agent="bob", reason="r", summary="s", completed="c",
            remaining="w", risks="k", next_action="n",
            proof_refs=[f"event:{self.events(item)[0]['event_id']}"])
        before = snap(self.conn)
        with self.assertRaisesRegex(
                InvalidTransition, f"handoff {h['handoff_id']}"):
            self.revise(item, fields={"objective": "while frozen"})
        self.assertEqual(before, snap(self.conn))


class ClaimSafety(RevisionBoard):
    def test_live_implementation_claim_refuses(self):
        item, sid, cid = self.make_working()
        before = snap(self.conn)
        with self.assertRaises(ClaimCollision):
            self.revise(item, fields={"objective": "under their feet"})
        self.assertEqual(before, snap(self.conn))

    def test_live_review_claim_refuses(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        self.release(cid, sid)
        bob_sid = self.make_session("bob")
        self.claim_rev(review_id, bob_sid)
        before = snap(self.conn)
        with self.assertRaises(ClaimCollision):
            self.revise(item, fields={"objective": "mid-review"})
        self.assertEqual(before, snap(self.conn))

    def test_stale_running_refuses_then_terminal_allows(self):
        item, sid, cid = self.make_working()
        self.clock.advance(LEASE + 1)
        sweeper_sid = self.make_session("carol")
        coopdb.sweep_expired(self.conn)
        before = snap(self.conn)
        with self.assertRaises(UnsafeReclaim):
            self.revise(item, fields={"objective": "seized"})
        self.assertEqual(before, snap(self.conn))
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit")
        self.revise(item, fields={"objective": "safely revised"})
        self.assertEqual(self.item_row(item)["contract_version"], 2)

    def test_unswept_expired_claim_same_refusal(self):
        item, sid, cid = self.make_working()
        self.clock.advance(LEASE + 1)
        with self.assertRaises(UnsafeReclaim):
            self.revise(item, fields={"objective": "pre-sweep seize"})
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit")
        self.revise(item, fields={"objective": "post-exit fine"})
        self.assertEqual(self.item_row(item)["contract_version"], 2)


class HumanLane(RevisionBoard):
    def test_rejected_inside_a_session(self):
        item = self.make_item()
        with unittest.mock.patch.dict(
                "os.environ", {"COOP_SESSION_ID": "s-live"}, clear=False):
            with self.assertRaises(HumanLaneViolation):
                self.revise(item, fields={"objective": "from a session"})

    def test_reason_required(self):
        item = self.make_item()
        with self.assertRaisesRegex(InvalidTransition, "reason"):
            self.revise(item, reason="   ",
                        fields={"objective": "no reason"})

    def test_unknown_item_refuses(self):
        with self.assertRaises(NotFound):
            self.revise(99999, fields={"objective": "ghost"})


class MergeAndValidation(RevisionBoard):
    def test_merged_result_must_be_complete(self):
        legacy = self.seed_legacy_item()
        self.assertTrue(
            coopdb.contract_incomplete(self.item_row(legacy)))
        with self.assertRaises(IncompleteContract):
            self.revise(legacy, fields={"objective": "only one field"})
        # A refused partial revision changes nothing.
        self.assertTrue(
            coopdb.contract_incomplete(self.item_row(legacy)))

    def test_legacy_unlock_end_to_end(self):
        legacy = self.seed_legacy_item()
        sid = self.make_session("alice")
        with self.assertRaises(IncompleteContract):
            self.claim(legacy, "alice", sid)
        self.revise(legacy, reason="unlock the import", fields={
            "objective": "o", "scope": "s", "done_when": "d",
            "output_contract": "out", "context": "c",
            "allowed_actions": ["read"], "stop_conditions": ["never"]})
        row = self.item_row(legacy)
        self.assertFalse(coopdb.contract_incomplete(row))
        self.assertEqual(row["contract_version"], 2)
        self.assertEqual(row["review_required"], 1)
        result = self.claim(legacy, "alice", sid)
        self.assertIsNotNone(result["claim_id"])

    def test_contract_file_then_field_precedence(self):
        item = self.make_item()
        path = self.workdir / "rev.json"
        path.write_text(json.dumps(
            {"objective": "from file", "scope": "file scope"}),
            encoding="utf-8")
        self.revise(item, fields={"scope": "flag scope"},
                    contract_path=str(path))
        row = self.item_row(item)
        self.assertEqual(row["objective"], "from file")
        self.assertEqual(row["scope"], "flag scope")

    def test_unknown_file_key_refuses(self):
        item = self.make_item()
        path = self.workdir / "bad.json"
        path.write_text(json.dumps({"owner": "bob"}), encoding="utf-8")
        with self.assertRaises(IncompleteContract):
            self.revise(item, contract_path=str(path))

    def test_version_bump_and_delta_event(self):
        item = self.make_item()
        self.revise(item, fields={"objective": "new objective",
                                  "stop_conditions": ["halt on red"]})
        events = self.events(item, "item_revised")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["old_version"], 1)
        self.assertEqual(payload["new_version"], 2)
        self.assertEqual(payload["reason"], "operator revision")
        delta = payload["delta"]
        self.assertEqual(set(delta), {"objective", "stop_conditions"})
        self.assertEqual(delta["objective"]["new"], "new objective")
        self.assertEqual(delta["stop_conditions"]["new"], ["halt on red"])
        self.assertNotIn("fencing_token", json.dumps(payload))
        self.assertEqual(events[0]["actor_agent_id"], "human")
        self.assertIsNone(events[0]["actor_session_id"])

    def test_second_revision_reaches_version_three(self):
        item = self.make_item()
        self.revise(item, fields={"objective": "two"})
        self.revise(item, fields={"objective": "three"})
        self.assertEqual(self.item_row(item)["contract_version"], 3)


class WaiverPairing(RevisionBoard):
    def test_waiver_set_and_cleared(self):
        item = self.make_item()
        self.revise(item, fields={"review_waiver": "trivial docs tweak"})
        row = self.item_row(item)
        self.assertEqual(row["review_required"], 0)
        self.assertEqual(row["review_waiver_reason"], "trivial docs tweak")
        self.revise(item, fields={"require_review": True})
        row = self.item_row(item)
        self.assertEqual(row["review_required"], 1)
        self.assertIsNone(row["review_waiver_reason"])

    def test_empty_waiver_reason_refuses(self):
        item = self.make_item()
        with self.assertRaises(IncompleteContract):
            self.revise(item, fields={"review_waiver": "   "})

    def test_untouched_waiver_survives_revision(self):
        item = self.make_item(review_waiver_reason="already waived")
        self.revise(item, fields={"objective": "changed"})
        row = self.item_row(item)
        self.assertEqual(row["review_required"], 0)
        self.assertEqual(row["review_waiver_reason"], "already waived")

    def test_explicit_quorum_is_revisioned_and_visible(self):
        item = self.make_item()
        self.assertEqual(coopdb.review_quorum(self.conn, item), 1)
        self.revise(item, fields={"review_quorum": 2})
        self.assertEqual(coopdb.review_quorum(self.conn, item), 2)
        self.assertEqual(
            coopdb.item_show(self.conn, item, packet=True)["review_quorum"], 2)

    def test_quorum_and_waiver_are_mutually_exclusive(self):
        item = self.make_item()
        with self.assertRaises(IncompleteContract):
            self.revise(item, fields={
                "review_quorum": 2, "review_waiver": "not both"})


class EvidenceInvalidation(RevisionBoard):
    def test_in_flight_approval_dies_and_fresh_cycle_completes(self):
        item, sid, cid, receipt_id, review_id = self.make_approved()
        self.release(cid, sid)
        self.revise(item, fields={"objective": "moved the goalposts"})
        row = self.item_row(item)
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["contract_version"], 2)
        old_receipt = self.conn.execute(
            "SELECT superseded_at FROM receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
        self.assertIsNotNone(old_receipt["superseded_at"])
        # Fresh cycle against v2 completes without the old approval leaking.
        cid2 = self.claim(item, "alice", sid, intent="rework",
                          reclaim_reason="resume after revision")["claim_id"]
        self.submit(cid2, sid, path=self.evidence_file("v2.md", b"v2\n"))
        rev2 = coopdb.request_review(
            self.conn, claim_id=cid2, session_id=sid, actor="alice")
        self.approve(rev2, reviewer="carol")
        outcome = self.complete(cid2, sid)
        self.assertEqual(outcome["completed"], item)
        completion_reviews = self.conn.execute(
            "SELECT id, contract_version FROM reviews WHERE item_id=? "
            "AND status='approved' ORDER BY id", (item,)).fetchall()
        self.assertEqual([r["contract_version"] for r in completion_reviews],
                         [1, 2])


class OwnerDelivery(RevisionBoard):
    def test_durable_owner_notified(self):
        item, sid, cid = self.make_working()
        self.release(cid, sid)
        self.revise(item, fields={"objective": "heads up"})
        entries = self.deliveries("alice")
        revisions = [e for e in entries if e["category"] == "revision"]
        self.assertEqual(len(revisions), 1)
        payload = json.loads(revisions[0]["payload_json"])
        self.assertEqual(payload["item_id"], item)
        self.assertEqual(payload["new_version"], 2)

    def test_unowned_item_no_delivery(self):
        item = self.make_item()
        self.revise(item, fields={"objective": "quiet"})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM inbox_entries").fetchone()["n"], 0)


class RevisionCli(RevisionBoard):
    def test_happy_path_with_flags(self):
        item = self.make_item()
        code, out, err = self._run(
            ["item", "revise", str(item), "--reason", "cli test",
             "--objective", "cli objective"],
            env={"COOP_SESSION_ID": "", "COOP_AGENT": ""})
        self.assertEqual(code, 0, err)
        row = self.item_row(item)
        self.assertEqual(row["objective"], "cli objective")
        self.assertEqual(row["contract_version"], 2)

    def test_json_output(self):
        item = self.make_item()
        code, out, err = self._run(
            ["--json", "item", "revise", str(item), "--reason", "r",
             "--context", "c2"],
            env={"COOP_SESSION_ID": "", "COOP_AGENT": ""})
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["item_id"], item)
        self.assertEqual(data["contract_version"], 2)

    def test_contract_file_flag(self):
        item = self.make_item()
        path = self.workdir / "cli.json"
        path.write_text(json.dumps({"scope": "file-driven"}),
                        encoding="utf-8")
        code, out, err = self._run(
            ["item", "revise", str(item), "--reason", "r",
             "--contract", str(path)],
            env={"COOP_SESSION_ID": "", "COOP_AGENT": ""})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.item_row(item)["scope"], "file-driven")

    def test_in_session_exit_one(self):
        item, sid, cid = self.make_working()
        self.release(cid, sid)
        code, out, err = self._run(
            ["item", "revise", str(item), "--reason", "r",
             "--objective", "sneaky"],
            env={"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"})
        self.assertEqual(code, 1)
        self.assertIn("human_lane_violation", err)

    def test_waiver_flags_mutually_exclusive(self):
        item = self.make_item()
        code, out, err = self._run(
            ["item", "revise", str(item), "--reason", "r",
             "--review-waiver", "w", "--require-review"],
            env={"COOP_SESSION_ID": "", "COOP_AGENT": ""})
        self.assertNotEqual(code, 0)

    def test_review_quorum_flag(self):
        item = self.make_item()
        code, out, err = self._run(
            ["item", "revise", str(item), "--reason", "high risk",
             "--review-quorum", "2"],
            env={"COOP_SESSION_ID": "", "COOP_AGENT": ""})
        self.assertEqual(code, 0, err)
        self.assertEqual(coopdb.review_quorum(self.conn, item), 2)


if __name__ == "__main__":
    unittest.main()
