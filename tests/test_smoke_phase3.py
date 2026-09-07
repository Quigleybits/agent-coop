"""The end-to-end protocol smoke — the protocol closes.

One deterministic two-session walk over the entire protocol on a
temporary board, every transition legal under the state machine:
seed → claim → decision → needs-input → answer → resume →
receipt (typed refs) → review request → second-session review claim →
changes (receipt superseded, item working) → handoff create → decline →
resume under grace → fresh receipt → re-request → review claim → approve →
complete — then the entire run is reconstructed from board rows alone and
the event chain is asserted gapless.  This is the mechanical form of the
live evidence sweep, with harmless Python children instead of
real providers.  The authorization matrix lives in test_smoke_phase2 —
never here.
"""

import json
import pathlib
import sys
import threading
import unittest

from agent_coop import coop_supervisor
from agent_coop import coopdb
from agent_coop.coop_errors import StaleClaim
from tests.test_claims import contract_kwargs
from tests.test_smoke_phase2 import SmokeBase, SLEEPER_CODE, wait_until
from tests.test_status import _scan_for_token


class Phase3ProtocolSmoke(SmokeBase):
    """The full protocol walk, driven through coopdb over two live sessions."""

    def _cursor(self, agent):
        rows = self.query(
            "SELECT * FROM inbox_offsets WHERE agent_id=?", (agent,))
        return rows[0]["last_entry_id"] if rows else 0

    def test_the_protocol_closes_end_to_end(self):
        sups, threads = {}, {}
        for agent, provider in (("alpha", "claude"), ("beta", "codex")):
            sups[agent] = coop_supervisor.Supervisor(
                self.db, provider=provider, agent_id=agent,
                argv=[sys.executable, "-c", SLEEPER_CODE],
                cwd=self.tmp.name,
                timings=coop_supervisor.Timings(
                    shutdown_grace_s=2, max_runtime_s=300))
            threads[agent] = threading.Thread(target=sups[agent].run)
            threads[agent].start()
        try:
            self.assertTrue(wait_until(lambda: len(self.query(
                "SELECT 1 FROM sessions WHERE status='running'")) == 2))
            sid_a = sups["alpha"].session_id
            sid_b = sups["beta"].session_id

            result_file = self.root / "results.md"
            result_file.write_text("evidence v1\n", encoding="utf-8")

            conn = coopdb.connect(self.db)
            try:
                # 1 seed — complete contract, review required by default.
                item = coopdb.create_item(
                    conn, actor="human", session_id=None,
                    **contract_kwargs(title="phase3-walk", owner="alpha"))
                # 2 claim.
                claim1 = coopdb.claim_item(
                    conn, item_id=item, actor="alpha", session_id=sid_a,
                    intent="walk the whole protocol")
                # 3 binding decision through the live claim.
                decision = coopdb.record_decision(
                    conn, claim_id=claim1["claim_id"], session_id=sid_a,
                    actor="alpha", text="serialize results as markdown",
                    rationale="the walk needs a binding decision")
                # 4 needs-input → 5 exact answer through beta's claim.
                qid = coopdb.needs_input(
                    conn, claim_id=claim1["claim_id"], session_id=sid_a,
                    to_agent="beta", question="which fixture encoding?")
                response = coopdb.claim_question(
                    conn, question_id=qid, session_id=sid_b,
                    intent="answering the walk question")
                coopdb.answer_question(
                    conn, claim_id=response["claim_id"], session_id=sid_b,
                    answer="utf-8")
                # 6 resume under a fresh claim; the old id is dead forever.
                claim2 = coopdb.claim_item(
                    conn, item_id=item, actor="alpha", session_id=sid_a,
                    intent="resuming with the answer",
                    reclaim_reason="question answered")
                with self.assertRaises(StaleClaim):
                    coopdb.checkpoint(
                        conn, ctype="step", claim_id=claim1["claim_id"],
                        actor="alpha", session_id=sid_a)
                # 7 receipt with typed refs — a file and the decision.
                receipt1 = coopdb.submit_receipt(
                    conn, claim_id=claim2["claim_id"], session_id=sid_a,
                    actor="alpha", path=str(result_file),
                    summary="first evidence", proof="walk proof v1",
                    proof_refs=[f"file:{result_file}",
                                f"decision:{decision}"])
                # 8 review request (unnamed — queue-discoverable).
                review1 = coopdb.request_review(
                    conn, claim_id=claim2["claim_id"], session_id=sid_a,
                    actor="alpha")
                # 9 second-session review claim: packet returned, canonical
                # cursor untouched, decisions included (the watermark base).
                before_offset = self._cursor("beta")
                rclaim1, packet = coopdb.claim_review(
                    conn, review_id=review1, session_id=sid_b,
                    intent="reviewing the first evidence")
                self.assertEqual(self._cursor("beta"), before_offset)
                self.assertTrue(any(
                    d.get("decision_id") == decision or
                    d.get("id") == decision
                    for d in packet.get("decisions", [])))
                # 10 changes — receipt superseded, item back to working.
                coopdb.submit_verdict(
                    conn, claim_id=rclaim1["claim_id"], session_id=sid_b,
                    actor="beta", verdict="changes",
                    body="needs the encoding note")
                self.assertEqual(self.query(
                    "SELECT status FROM items WHERE id=?",
                    (item,))[0]["status"], "working")
                self.assertIsNotNone(self.query(
                    "SELECT superseded_at FROM receipts WHERE receipt_id=?",
                    (receipt1,))[0]["superseded_at"])
                # 11 handoff to beta → 12 declined back with grace.
                handoff = coopdb.create_handoff(
                    conn, claim_id=claim2["claim_id"], session_id=sid_a,
                    actor="alpha", to_agent="beta",
                    reason="second pair of eyes",
                    summary="walk in progress", completed="v1 evidence",
                    remaining="the encoding note", risks="fixture drift",
                    next_action="add the note and re-request",
                    proof_refs=[f"decision:{decision}"])
                coopdb.decline_handoff(
                    conn, handoff_id=handoff["handoff_id"],
                    session_id=sid_b, actor="beta",
                    reason="owner is closer to it")
                # 13 resume under the decline grace.
                claim3 = coopdb.claim_item(
                    conn, item_id=item, actor="alpha", session_id=sid_a,
                    intent="resuming after the decline",
                    reclaim_reason="decline grace resume")
                # 14 fresh receipt → 15 re-request → 16 review claim.
                result_file.write_text(
                    "evidence v2 — encoding: utf-8\n", encoding="utf-8")
                coopdb.submit_receipt(
                    conn, claim_id=claim3["claim_id"], session_id=sid_a,
                    actor="alpha", path=str(result_file),
                    summary="second evidence", proof="walk proof v2",
                    proof_refs=[f"file:{result_file}",
                                f"decision:{decision}"])
                review2 = coopdb.request_review(
                    conn, claim_id=claim3["claim_id"], session_id=sid_a,
                    actor="alpha")
                rclaim2, _ = coopdb.claim_review(
                    conn, review_id=review2, session_id=sid_b,
                    intent="re-reviewing the fresh evidence")
                # 17 approve → 18 guarded completion.
                coopdb.submit_verdict(
                    conn, claim_id=rclaim2["claim_id"], session_id=sid_b,
                    actor="beta", verdict="approve", body="clean now")
                outcome = coopdb.complete_item(
                    conn, claim_id=claim3["claim_id"], session_id=sid_a,
                    actor="alpha")
                self.assertEqual(outcome.get("completed"), item)

                # ---- Reconstruction from board rows alone (the evidence
                # sweep): every protocol object typed-readable. ----
                history = coopdb.item_show(conn, item, history=True)
                blob = json.dumps(history)
                self.assertEqual(history["item"]["status"], "done")
                self.assertEqual(len(history["receipts"]), 2)
                self.assertEqual(
                    [bool(r["superseded_at"]) for r in history["receipts"]],
                    [True, False])
                self.assertEqual(len(history["reviews"]), 2)
                self.assertEqual(
                    sorted(r["status"] for r in history["reviews"]),
                    ["approved", "changes"])
                self.assertIn("serialize results as markdown", blob)
                self.assertIn("which fixture encoding?", blob)
                self.assertIn("utf-8", blob)
                self.assertIn("owner is closer to it", blob)
                _scan_for_token(history)
                status_a = coopdb.status(conn, "alpha", session_id=sid_a)
                status_b = coopdb.status(conn, "beta", session_id=sid_b)
                _scan_for_token(status_a)
                _scan_for_token(status_b)
                self.assertEqual(
                    coopdb.queue(conn, for_agent="beta"), [])
            finally:
                conn.close()

            # ---- The event chain has no gaps: the item's ordered event
            # types are exactly the walk, one event per transition. ----
            events = [r["event_type"] for r in self.query(
                "SELECT event_type FROM events WHERE item_id=? "
                "ORDER BY event_id", (item,))]
            self.assertEqual(events, [
                "item_created",
                "claim_acquired",
                "decision_recorded",
                "needs_input",
                "question_claimed",
                "question_answered",
                "claim_acquired",       # resume mints a fresh claim
                "receipt_submitted",
                "review_requested",
                "review_claimed",
                "receipt_superseded",   # changes supersedes in-transaction
                "review_resolved",
                "handoff_created",
                "handoff_declined",
                "claim_acquired",       # decline-grace resume
                "receipt_submitted",
                "review_requested",
                "review_claimed",
                "review_resolved",
                "item_completed",
            ])
            ids = [r["event_id"] for r in self.query(
                "SELECT event_id FROM events WHERE item_id=? "
                "ORDER BY event_id", (item,))]
            self.assertEqual(ids, sorted(set(ids)))  # strict, no repeats

            # No side channel anywhere in the run directory: no prompt
            # files, machine-written projections only.
            files = [p for p in pathlib.Path(self.tmp.name).rglob("*")
                     if p.is_file()]
            self.assertFalse(
                [p for p in files if "prompt" in p.name.lower()])
            # Every markdown file is either the contract's declared result
            # artifact (the frozen-mutation-set rule) or a machine-written
            # projection — nothing else.
            for md in pathlib.Path(self.tmp.name).rglob("*.md"):
                if md == result_file:
                    continue
                self.assertTrue(md.read_text(
                    encoding="utf-8").startswith("# Inbox"))
        finally:
            for sup in sups.values():
                sup.cancel()
            for t in threads.values():
                t.join(timeout=120)


if __name__ == "__main__":
    unittest.main()
