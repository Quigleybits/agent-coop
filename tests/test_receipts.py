"""Hashed receipts with the mechanical typed-reference linter.

Receipts are immutable evidence metadata: the file's bytes are hashed at
submission, every proof reference is mechanically verified (board rows must
exist AND attach to the item; file refs resolve and carry their own hash),
and a replacement submission supersedes the prior receipt in the same
transaction — one current receipt per item. Reads re-hash opportunistically
and mark missing|changed evidence without mutating anything.
"""

import contextlib
import hashlib
import io
import json
import pathlib
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop import projection
from agent_coop.coop_errors import (
    InvalidTransition,
    ProofReferenceInvalid,
    ReceiptInvalid,
    SessionMismatch,
    StaleClaim,
)
from tests.test_claims import ClaimBoard, LEASE

SNAP_TABLES = (
    "items", "claims", "receipts", "events", "inbox_entries", "inbox_offsets",
)


def snap(conn):
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
        for t in SNAP_TABLES
    }


class ReceiptBoard(ClaimBoard):
    def setUp(self):
        super().setUp()
        self.workdir = pathlib.Path(self.tmp.name)

    def make_working(self, agent="alice", **item_over):
        item = self.make_item(**item_over)
        sid = self.make_session(agent)
        claim = self.claim(item, agent, sid)
        return item, sid, claim["claim_id"]

    def evidence_file(self, name="receipt.md", data=b"evidence bytes\n"):
        path = self.workdir / name
        path.write_bytes(data)
        return path

    def submit(self, claim_id, sid, agent="alice", path=None, refs=None,
               summary="did the work", proof="see refs"):
        if path is None:
            path = self.evidence_file()
        return coopdb.submit_receipt(
            self.conn, claim_id=claim_id, session_id=sid, actor=agent,
            path=str(path), summary=summary, proof=proof, proof_refs=refs)

    def receipt_rows(self, item):
        return self.conn.execute(
            "SELECT * FROM receipts WHERE item_id=? ORDER BY receipt_id",
            (item,)).fetchall()


class FileValidation(ReceiptBoard):
    def test_missing_empty_unreadable_file_refused(self):
        item, sid, cid = self.make_working()
        missing = self.workdir / "nope.md"
        empty = self.evidence_file("empty.md", b"")
        directory = self.workdir / "adir"
        directory.mkdir()
        for label, path in (("missing", missing), ("empty", empty),
                            ("unreadable", directory)):
            with self.subTest(case=label):
                before = snap(self.conn)
                with self.assertRaises(ReceiptInvalid) as caught:
                    self.submit(cid, sid, path=path)
                self.assertEqual(before, snap(self.conn))
                self.assertEqual(
                    caught.exception.reason_code, "receipt_invalid")
                self.assertEqual(caught.exception.evidence["item_id"], item)
                self.assertEqual(caught.exception.evidence["claim_id"], cid)
                self.assertEqual(
                    caught.exception.evidence["constraint"],
                    "receipt_source_non_empty"
                    if label == "empty" else "receipt_source_readable",
                )

    def test_hash_and_resolved_path_recorded(self):
        data = b"the exact bytes\n"
        item, sid, cid = self.make_working()
        source = self.evidence_file("proof.md", data)
        rid = self.submit(cid, sid, path=source)
        row = self.receipt_rows(item)[0]
        self.assertEqual(row["receipt_id"], rid)
        self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(row["source_path"], str(source.resolve()))
        self.assertEqual(row["contract_version"],
                         self.conn.execute(
                             "SELECT contract_version FROM items WHERE id=?",
                             (item,)).fetchone()["contract_version"])
        claim_row = self.conn.execute(
            "SELECT fencing_token FROM claims WHERE claim_id=?",
            (cid,)).fetchone()
        self.assertEqual(row["fencing_token"], claim_row["fencing_token"])
        self.assertIsNone(row["superseded_at"])
        events = self.events_of("receipt_submitted")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["receipt_id"], rid)
        self.assertEqual(payload["sha256"], row["sha256"])
        self.assertNotIn("fencing_token", payload)


class LinterValidation(ReceiptBoard):
    def test_malformed_reference_classes_rejected_with_rollback(self):
        item, sid, cid = self.make_working()
        source = self.evidence_file()
        missing_abs = str((self.workdir / "ghost.txt").resolve())
        cases = {
            "unknown_type": "commit:5",
            "non_integer_id": "decision:abc",
            "relative_path": "file:relative/path.txt",
            "unreadable_file": f"file:{missing_abs}",
            "no_separator": "garbage",
        }
        for label, ref in cases.items():
            with self.subTest(case=label):
                before = snap(self.conn)
                with self.assertRaises(ProofReferenceInvalid) as caught:
                    self.submit(cid, sid, path=source, refs=[ref])
                self.assertEqual(before, snap(self.conn))
                self.assertEqual(caught.exception.reason_code, "proof_invalid")
                self.assertEqual(caught.exception.evidence["item_id"], item)
                self.assertEqual(
                    caught.exception.evidence["constraint"],
                    "proof_reference_invalid",
                )

    def test_board_row_references_must_attach(self):
        item, sid, cid = self.make_working()
        other, other_sid, other_cid = self.make_working(
            agent="bob", title="other item")
        source = self.evidence_file()

        def seed(table, columns, values):
            def _ins(conn):
                cur = conn.execute(
                    f"INSERT INTO {table}({columns}) VALUES ({values[0]})",
                    values[1])
                return cur.lastrowid
            return coopdb.mutate(self.conn, _ins)

        stamp = coopdb.now()
        wrong_debate = seed(
            "debates", "item_id,topic,created_by,created_at",
            ("?,?,?,?", (other, "t", "human", stamp)))
        null_debate = seed(
            "debates", "item_id,topic,created_by,created_at",
            ("?,?,?,?", (None, "legacy", "human", stamp)))
        wrong_decision = seed(
            "decisions", "item_id,text,decided_by,created_at",
            ("?,?,?,?", (other, "d", "human", stamp)))
        null_decision = seed(
            "decisions", "item_id,text,decided_by,created_at",
            ("?,?,?,?", (None, "legacy d", "human", stamp)))
        wrong_event = self.conn.execute(
            "SELECT event_id FROM events WHERE item_id=? ORDER BY event_id",
            (other,)).fetchone()["event_id"]
        null_event = seed(
            "events", "item_id,event_type,payload_json,created_at",
            ("?,?,?,?", (None, "legacy_note", "{}", stamp)))
        cases = {
            "missing_debate": "debate:9999",
            "missing_decision": "decision:9999",
            "missing_event": "event:9999",
            "wrong_item_debate": f"debate:{wrong_debate}",
            "wrong_item_decision": f"decision:{wrong_decision}",
            "wrong_item_event": f"event:{wrong_event}",
            "null_item_debate": f"debate:{null_debate}",
            "null_item_decision": f"decision:{null_decision}",
            "null_item_event": f"event:{null_event}",
        }
        for label, ref in cases.items():
            with self.subTest(case=label):
                before = snap(self.conn)
                with self.assertRaises(ProofReferenceInvalid) as caught:
                    self.submit(cid, sid, path=source, refs=[ref])
                self.assertEqual(before, snap(self.conn))
                self.assertEqual(caught.exception.reason_code, "proof_invalid")
                self.assertEqual(caught.exception.evidence["item_id"], item)
                self.assertEqual(
                    caught.exception.evidence["constraint"],
                    "proof_reference_invalid",
                )

    def test_valid_references_stored_normalized_with_file_hashes(self):
        item, sid, cid = self.make_working()
        stamp = coopdb.now()

        def seed(sql, params):
            def _ins(conn):
                return conn.execute(sql, params).lastrowid
            return coopdb.mutate(self.conn, _ins)

        debate = seed(
            "INSERT INTO debates(item_id,topic,created_by,created_at) "
            "VALUES (?,?,?,?)", (item, "t", "human", stamp))
        decision = seed(
            "INSERT INTO decisions(item_id,text,decided_by,created_at) "
            "VALUES (?,?,?,?)", (item, "d", "human", stamp))
        event = self.conn.execute(
            "SELECT event_id FROM events WHERE item_id=? ORDER BY event_id",
            (item,)).fetchone()["event_id"]
        cited_data = b"cited artifact\n"
        cited = self.workdir / "artifact.txt"
        cited.write_bytes(cited_data)
        source = self.evidence_file()
        rid = self.submit(
            cid, sid, path=source,
            refs=[f"debate:{debate}", f"decision:{decision}",
                  f"event:{event}", f"file:{cited}"])
        stored = json.loads(self.receipt_rows(item)[0]["proof_references_json"])
        self.assertEqual(stored, [
            {"type": "debate", "id": debate},
            {"type": "decision", "id": decision},
            {"type": "event", "id": event},
            {"type": "file", "path": str(cited.resolve()),
             "sha256": hashlib.sha256(cited_data).hexdigest()},
        ])
        payload = json.loads(
            self.events_of("receipt_submitted")[0]["payload_json"])
        self.assertEqual(payload["reference_count"], 4)


class Supersession(ReceiptBoard):
    def test_replacement_supersedes_in_one_transaction(self):
        item, sid, cid = self.make_working()
        first = self.submit(cid, sid, path=self.evidence_file("a.md", b"one\n"))
        second = self.submit(cid, sid, path=self.evidence_file("b.md", b"two\n"))
        rows = self.receipt_rows(item)
        self.assertEqual([r["receipt_id"] for r in rows], [first, second])
        self.assertIsNotNone(rows[0]["superseded_at"])
        self.assertIsNone(rows[1]["superseded_at"])
        current = self.conn.execute(
            "SELECT COUNT(*) AS n FROM receipts WHERE item_id=? AND "
            "superseded_at IS NULL", (item,)).fetchone()["n"]
        self.assertEqual(current, 1)
        events = self.events_of("receipt_submitted")
        self.assertEqual(len(events), 2)
        replacement_payload = json.loads(events[1]["payload_json"])
        self.assertEqual(replacement_payload["superseded_receipt_id"], first)
        history = coopdb.item_show(self.conn, item, history=True)
        self.assertEqual(
            [r["receipt_id"] for r in history["receipts"]], [first, second])


class ClaimBoundary(ReceiptBoard):
    def test_claim_boundary_cells_refuse(self):
        item, sid, cid = self.make_working()
        source = self.evidence_file()
        self.make_session("bob", sid="s-bob")
        q_item, q_sid, q_cid = self.make_working(agent="carol", title="asker")
        qid = coopdb.needs_input(
            self.conn, claim_id=q_cid, session_id=q_sid, to_agent="alice",
            question="which flavor?")
        q_claim = coopdb.claim_question(
            self.conn, question_id=qid, session_id=sid, intent="answering")
        cells = {
            "no_session_human": dict(claim_id=cid, session_id=None,
                                     actor="human", exc=SessionMismatch),
            "wrong_session": dict(claim_id=cid, session_id="s-bob",
                                  actor="bob", exc=SessionMismatch),
            "wrong_kind_question_claim": dict(
                claim_id=q_claim["claim_id"], session_id=sid, actor="alice",
                exc=InvalidTransition),
        }
        for label, cell in cells.items():
            with self.subTest(cell=label):
                before = snap(self.conn)
                with self.assertRaises(cell["exc"]):
                    coopdb.submit_receipt(
                        self.conn, claim_id=cell["claim_id"],
                        session_id=cell["session_id"], actor=cell["actor"],
                        path=str(source), summary="s", proof="p",
                        proof_refs=None)
                self.assertEqual(before, snap(self.conn))
        # Superseded claim: release, reclaim, then write through the old claim.
        coopdb.release_claim(
            self.conn, claim_id=cid, actor="alice", session_id=sid,
            reason="rotating")
        self.claim(item, "alice", sid, reclaim_reason="fresh claim")
        before = snap(self.conn)
        with self.assertRaises(StaleClaim):
            coopdb.submit_receipt(
                self.conn, claim_id=cid, session_id=sid, actor="alice",
                path=str(source), summary="s", proof="p", proof_refs=None)
        self.assertEqual(before, snap(self.conn))


class OpportunisticMarker(ReceiptBoard):
    def test_marker_flips_on_byte_change_without_mutating_rows(self):
        item, sid, cid = self.make_working()
        source = self.evidence_file("live.md", b"v1\n")
        rid = self.submit(cid, sid, path=source)
        packet = coopdb.item_show(self.conn, item, packet=True)
        slot = packet["receipt"]
        self.assertEqual(slot["receipt_id"], rid)
        self.assertEqual(slot["reference_count"], 0)
        self.assertIsNone(slot["marker"])
        stored = snap(self.conn)["receipts"]
        source.write_bytes(b"v2 tampered\n")
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(packet["receipt"]["marker"], "changed")
        history = coopdb.item_show(self.conn, item, history=True)
        self.assertEqual(history["receipts"][0]["evidence_marker"], "changed")
        source.unlink()
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(packet["receipt"]["marker"], "missing")
        self.assertEqual(snap(self.conn)["receipts"], stored)


class TokenAbsence(ReceiptBoard):
    def test_token_absent_from_every_new_surface(self):
        item, sid, cid = self.make_working()
        rid = self.submit(cid, sid, path=self.evidence_file())
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertNotIn("fencing_token", json.dumps(packet))
        history = coopdb.item_show(self.conn, item, history=True)
        self.assertNotIn("fencing_token", json.dumps(history["receipts"]))
        payload = self.events_of("receipt_submitted")[0]["payload_json"]
        self.assertNotIn("fencing_token", payload)
        rendered = projection.render_inbox(self.conn, "alice")
        self.assertIn(f"receipt {rid}", rendered)
        self.assertNotIn("fencing_token", rendered)


class ReceiptCLI(ReceiptBoard):
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

    def test_cli_submit_happy_path_json(self):
        item, sid, cid = self.make_working()
        source = self.evidence_file()
        event = self.conn.execute(
            "SELECT event_id FROM events WHERE item_id=? ORDER BY event_id",
            (item,)).fetchone()["event_id"]
        code, out, err = self._run(
            ["--json", "receipt", "submit", "--claim", str(cid),
             "--path", str(source), "--summary", "done", "--proof", "see ref",
             "--proof-ref", f"event:{event}"],
            {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"})
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"receipt_id": 1})
        self.assertNotIn("fencing_token", out)


if __name__ == "__main__":
    unittest.main()
