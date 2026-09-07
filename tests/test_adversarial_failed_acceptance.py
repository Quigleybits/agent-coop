"""Adversarial trial — failure class: failed acceptance command.

Scripted trial for the "Failed acceptance command" gate class: the
contract's ``done_when`` names a concrete acceptance command and that
command exits non-zero against the submitted evidence. The item must not
complete as success; the honest exits (rework until green, or
needs_input) stay open.

SCOPE NOTE — acceptance execution is convention, not code. No surface in
``agent_coop`` runs an acceptance command (there is no acceptance_command
field or executor); the protocol makes the REVIEWER re-run the
command named in the contract. The protocol gate under test is therefore
review + guarded completion refusing an unaccepted result: the reviewer
leg of this trial ACTUALLY executes the named command via subprocess,
observes exit 1, and files verdict='changes' — after which
``complete_item`` must fail closed until the command really exits 0 under
a fresh receipt and a fresh approval. Mechanical acceptance execution
would be a future source feature; this trial needs no source change.
"""

import json
import subprocess
import sys
import unittest

from agent_coop import coopdb
from agent_coop.coop_errors import ReceiptMissing, ReviewMissing, StaleClaim
from tests.test_completion import CompletionBoard


def acceptance_command(evidence_path):
    """A deterministic acceptance command over one evidence file:
    exit 0 iff the file's stripped bytes equal ``b'ok'``, else exit 1."""
    return [
        sys.executable,
        "-c",
        "import pathlib, sys; sys.exit(0 if pathlib.Path("
        + repr(str(evidence_path))
        + ").read_bytes().strip() == b'ok' else 1)",
    ]


class FailedAcceptanceCommand(CompletionBoard):
    def seed_trial(self):
        """Working item whose contract names a real, runnable acceptance
        command, with evidence seeded so that command exits 1."""
        evidence = self.evidence_file("acceptance_evidence.txt", b"broken\n")
        command = acceptance_command(evidence)
        item, sid, cid = self.make_working(
            "alice",
            done_when="acceptance command exits 0: " + " ".join(command))
        return item, sid, cid, evidence, command

    def run_acceptance(self, command):
        return subprocess.run(command, capture_output=True).returncode

    def review_red(self, cid, sid, command):
        """Owner submits + requests review; the reviewer ACTUALLY runs the
        named acceptance command, observes exit 1, and files 'changes' with
        the exit code in the reason. Returns the reviewer's session."""
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        bob_sid = self.make_session("bob")
        claim, _packet = self.claim_rev(review_id, bob_sid)
        exit_code = self.run_acceptance(command)
        self.assertEqual(exit_code, 1)
        coopdb.submit_verdict(
            self.conn, claim_id=claim["claim_id"], session_id=bob_sid,
            actor="bob", verdict="changes",
            body=f"acceptance command exited {exit_code}; done_when not met")
        return bob_sid

    def test_red_acceptance_blocks_completion_until_green(self):
        item, sid, cid, evidence, command = self.seed_trial()
        receipt_id = self.submit(cid, sid, path=evidence)
        bob_sid = self.review_red(cid, sid, command)

        # The changes verdict returns the item to working and supersedes
        # the reviewed receipt — the unaccepted result is dead capital.
        self.assertEqual(self.item_row(item)["status"], "working")
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT superseded_at FROM receipts WHERE receipt_id=?",
                (receipt_id,)).fetchone()["superseded_at"])

        # Completion fails closed: no current receipt after the supersede...
        with self.assertRaises(ReceiptMissing):
            self.complete(cid, sid)
        # ...and resubmitting a receipt over the SAME failing evidence has
        # no qualifying approval, so completion still refuses.
        self.submit(cid, sid, path=evidence)
        with self.assertRaises(ReviewMissing) as caught:
            self.complete(cid, sid)
        self.assertEqual(caught.exception.reason_code, "review_missing")
        self.assertEqual(self.events(item, "item_completed"), [])
        self.assertNotEqual(self.item_row(item)["status"], "done")

        # Honest exit A — rework until the named command is really green,
        # then fresh receipt + fresh approval completes.
        evidence.write_bytes(b"ok\n")
        self.assertEqual(self.run_acceptance(command), 0)
        fresh_receipt = self.submit(cid, sid, path=evidence)
        review2 = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        claim2, _packet = self.claim_rev(review2, bob_sid)
        self.assertEqual(self.run_acceptance(command), 0)  # reviewer re-run
        coopdb.submit_verdict(
            self.conn, claim_id=claim2["claim_id"], session_id=bob_sid,
            actor="bob", verdict="approve")
        result = self.complete(cid, sid)
        self.assertEqual(result["completed"], item)
        self.assertEqual(self.item_row(item)["status"], "done")
        done_events = self.events(item, "item_completed")
        self.assertEqual(len(done_events), 1)
        self.assertEqual(
            json.loads(done_events[0]["payload_json"])["receipt_id"],
            fresh_receipt)

    def test_owner_honest_stop_instead_of_false_completion(self):
        item, sid, cid, evidence, command = self.seed_trial()
        self.submit(cid, sid, path=evidence)
        self.review_red(cid, sid, command)

        # Honest exit B — the owner routes the red acceptance to a peer
        # instead of claiming success.
        question_id = coopdb.needs_input(
            self.conn, claim_id=cid, session_id=sid, to_agent="bob",
            question="acceptance command exits 1 against the evidence; "
                     "is the fixture or the contract wrong?")
        self.assertIsInstance(question_id, int)
        self.assertEqual(self.item_row(item)["status"], "needs_input")

        # needs_input closed the implementation lane; the closed claim
        # cannot complete, and nothing was recorded as done.
        with self.assertRaises(StaleClaim):
            self.complete(cid, sid)
        self.assertEqual(self.events(item, "item_completed"), [])
        self.assertNotEqual(self.item_row(item)["status"], "done")


if __name__ == "__main__":
    unittest.main()
