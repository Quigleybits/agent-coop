"""Claim-bound append-only decisions.

`record_decision` resolves the item from the validated implementation claim
(the CLI takes no item id), is legal while the item is `working` or
`review`, and appends an immutable row — nothing edits or retracts.
Delivery reaches the live review's claimant-or-designated-reviewer when one
exists and differs from the actor; otherwise the event is the whole record.
Migrated legacy decisions render with their preserved `debate_id` and null
claim data, fabricating nothing.
"""

import contextlib
import io
import json
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop import projection
from agent_coop.coop_errors import InvalidTransition
from tests.test_contract import DECISION_ENTRY_KEYS
from tests.test_reviews import ReviewBoard, snap


class DecisionBoard(ReviewBoard):
    def decide(self, cid, sid, agent="alice", text="use sqlite",
               rationale=None):
        return coopdb.record_decision(
            self.conn, claim_id=cid, session_id=sid, actor=agent, text=text,
            rationale=rationale)

    def decision_rows(self, item):
        return self.conn.execute(
            "SELECT * FROM decisions WHERE item_id=? ORDER BY id",
            (item,)).fetchall()

    def decision_entries(self):
        return self.conn.execute(
            "SELECT * FROM inbox_entries WHERE category='decision' "
            "ORDER BY inbox_entry_id").fetchall()

    def seed_legacy_debate_decision(self, item, text="pre-release call"):
        """A migration-shaped legacy row: preserved debate lineage, no
        claim, no session — pre-release data describing no claim state."""
        def _seed(conn):
            coopdb.register_agent(conn, "alice")
            cur = conn.execute(
                "INSERT INTO debates(item_id,topic,status,created_by,"
                "created_at) VALUES (?,?,'closed','alice',?)",
                (item, "which db", coopdb.now()))
            debate_id = cur.lastrowid
            conn.execute(
                "INSERT INTO decisions(item_id,debate_id,text,rationale,"
                "decided_by,decided_by_agent,legacy,created_at) "
                "VALUES (?,?,?,NULL,'human','human',1,?)",
                (item, debate_id, text, coopdb.now()))
            return debate_id
        return coopdb.mutate(self.conn, _seed)


class RecordValidation(DecisionBoard):
    def test_decision_resolves_item_from_claim_appended_event_only(self):
        item, sid, cid = self.make_working()
        decision_id = self.decide(cid, sid)
        rows = self.decision_rows(item)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], decision_id)
        self.assertEqual(row["item_id"], item)
        self.assertEqual(row["legacy"], 0)
        self.assertIsNone(row["debate_id"])
        self.assertEqual(row["decided_by"], "alice")
        self.assertEqual(row["decided_by_agent"], "alice")
        self.assertEqual(row["decided_by_session"], sid)
        self.assertEqual(row["claim_id"], cid)
        claim = self.conn.execute(
            "SELECT fencing_token FROM claims WHERE claim_id=?",
            (cid,)).fetchone()
        self.assertEqual(row["fencing_token"], claim["fencing_token"])
        events = self.events_of("decision_recorded")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["claim_id"], cid)
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["decision_id"], decision_id)
        self.assertEqual(payload["item_id"], item)
        # No live review -> event-only: zero addressed deliveries.
        self.assertEqual(self.decision_entries(), [])

    def test_rationale_optional_and_stored(self):
        item, sid, cid = self.make_working()
        with_r = self.decide(cid, sid, text="split it",
                             rationale="two concerns")
        without = self.decide(cid, sid, text="ship it")
        rows = {r["id"]: r for r in self.decision_rows(item)}
        self.assertEqual(rows[with_r]["rationale"], "two concerns")
        self.assertIsNone(rows[without]["rationale"])

    def test_empty_text_refused(self):
        item, sid, cid = self.make_working()
        before = snap(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            self.decide(cid, sid, text="   ")
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(caught.exception.reason_code, "input_invalid")
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "non_empty_decision_text",
        )

    def test_append_only_no_edit_or_retract_surface(self):
        for name in ("update_decision", "edit_decision", "delete_decision",
                     "retract_decision"):
            self.assertFalse(
                hasattr(coopdb, name),
                f"decisions are append-only; coopdb.{name} must not exist")

    def test_question_and_review_claims_refuse_wrong_kind(self):
        item, sid, cid = self.make_working()
        bob_sid = self.make_session("bob")
        qid = coopdb.needs_input(
            self.conn, claim_id=cid, session_id=sid, to_agent="bob",
            question="which db?")
        qclaim = coopdb.claim_question(
            self.conn, question_id=qid, session_id=bob_sid, intent="answer")
        before = snap(self.conn)
        with self.assertRaises(InvalidTransition):
            self.decide(qclaim["claim_id"], bob_sid, agent="bob")
        self.assertEqual(snap(self.conn), before)

        item2, sid2, cid2, receipt_id, review_id = self.make_reviewed(
            agent="carol")
        rclaim, _packet = self.claim_rev(review_id, bob_sid)
        before = snap(self.conn)
        with self.assertRaises(InvalidTransition):
            self.decide(rclaim["claim_id"], bob_sid, agent="bob")
        self.assertEqual(snap(self.conn), before)

    def test_legal_in_working_and_review(self):
        # working covered above; the review-status leg: request_review
        # keeps the implementation claim open and the owner keeps deciding.
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        status = self.conn.execute(
            "SELECT status FROM items WHERE id=?", (item,)).fetchone()
        self.assertEqual(status["status"], "review")
        decision_id = self.decide(cid, sid, text="decided mid-review")
        self.assertEqual(
            self.decision_rows(item)[-1]["id"], decision_id)


class Delivery(DecisionBoard):
    def test_review_claimant_gets_the_single_delivery(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        bob_sid = self.make_session("bob")
        self.claim_rev(review_id, bob_sid)
        decision_id = self.decide(cid, sid, text="mid-review call")
        entries = self.decision_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["recipient_agent_id"], "bob")
        payload = json.loads(entries[0]["payload_json"])
        self.assertEqual(payload["decision_id"], decision_id)
        self.assertEqual(payload["item_id"], item)
        self.assertEqual(payload["review_id"], review_id)
        event = self.events_of("decision_recorded")[0]
        self.assertEqual(entries[0]["source_event_id"], event["event_id"])
        self.assertNotIn("fencing_token", entries[0]["payload_json"])

    def test_designated_unclaimed_reviewer_gets_delivery(self):
        self.make_session("bob")
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            reviewer="bob")
        self.decide(cid, sid, text="named, unclaimed")
        entries = self.decision_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["recipient_agent_id"], "bob")

    def test_unnamed_unclaimed_review_is_event_only(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        self.decide(cid, sid, text="nobody to notify yet")
        self.assertEqual(self.decision_entries(), [])
        self.assertEqual(len(self.events_of("decision_recorded")), 1)


class LegacyAndReadPath(DecisionBoard):
    def test_legacy_decision_renders_preserved_debate_null_claim(self):
        item = self.make_item()
        debate_id = self.seed_legacy_debate_decision(item)
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(len(packet["decisions"]), 1)
        entry = packet["decisions"][0]
        self.assertEqual(set(entry), DECISION_ENTRY_KEYS)
        self.assertEqual(entry["debate_id"], debate_id)
        self.assertIsNone(entry["claim_id"])
        self.assertTrue(entry["legacy"])
        self.assertEqual(entry["decided_by"], "human")
        self.assertNotIn("fencing_token", json.dumps(packet))
        history = coopdb.item_show(self.conn, item, history=True)
        row = history["decisions"][0]
        self.assertEqual(row["debate_id"], debate_id)
        self.assertIsNone(row["decided_by_session"])
        self.assertIsNone(row["claim_id"])

    def test_live_row_packet_shape(self):
        item, sid, cid = self.make_working()
        decision_id = self.decide(cid, sid, rationale="why not")
        entry = coopdb.item_show(
            self.conn, item, packet=True)["decisions"][0]
        self.assertEqual(set(entry), DECISION_ENTRY_KEYS)
        self.assertEqual(entry["decision_id"], decision_id)
        self.assertEqual(entry["claim_id"], cid)
        self.assertIsNone(entry["debate_id"])
        self.assertFalse(entry["legacy"])
        self.assertEqual(entry["decided_by"], "alice")

    def test_projection_renders_latest_decision_token_free(self):
        item, sid, cid = self.make_working()
        self.decide(cid, sid, text="first")
        latest = self.decide(cid, sid, text="latest call")
        rendered = projection.render_inbox(self.conn, "alice")
        self.assertIn(f"decision {latest} by alice: latest call", rendered)
        self.assertNotIn("fencing_token", rendered)


class DecisionCLI(DecisionBoard):
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

    def test_cli_record_happy_and_json(self):
        item, sid, cid = self.make_working()
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}
        code, out, err = self._run(
            ["decision", "record", "--claim", str(cid), "--text",
             "use sqlite"], env)
        self.assertEqual(code, 0, err)
        self.assertIn("decision", out)
        code, out, err = self._run(
            ["--json", "decision", "record", "--claim", str(cid), "--text",
             "and wal mode", "--rationale", "durability"], env)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        row = self.decision_rows(item)[-1]
        self.assertEqual(data["decision_id"], row["id"])
        self.assertEqual(row["rationale"], "durability")
        self.assertNotIn("fencing_token", out)


if __name__ == "__main__":
    unittest.main()
