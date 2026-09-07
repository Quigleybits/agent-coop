"""Needs-input, question claims, answers, resume, takeover.

Blocking uncertainty is a first-class state: the exact question closes
execution authority, only the addressed agent (or the audited human lane)
answers, the answer opens the preferred-owner grace window on the item
alone, and resumption is always a fresh reasoned claim.
"""

import io
import contextlib
import json
import threading
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop.coop_errors import (
    AddressedTargetMismatch,
    ClaimCollision,
    HumanLaneViolation,
    InvalidTransition,
    NotFound,
    SessionMismatch,
    StaleClaim,
    UnsafeReclaim,
)
from tests.test_claims import ClaimBoard, snapshot

GRACE = 900


def batch_snapshot(conn):
    return {
        table: [
            tuple(row)
            for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid")
        ]
        for table in (
            "items", "claims", "questions", "events", "inbox_entries"
        )
    }


class QuestionBoard(ClaimBoard):
    def needs_input(self, claim_id, sid, to_agent="bob", question="exact?"):
        return coopdb.needs_input(
            self.conn, claim_id=claim_id, session_id=sid,
            to_agent=to_agent, question=question)

    def working_pair(self):
        """Item claimed by alice, bob registered and running — the standard
        needs-input stage."""
        item = self.make_item()
        alice = self.make_session("alice", sid="s-alice")
        bob = self.make_session("bob", sid="s-bob")
        claim = self.claim(item, "alice", "s-alice")
        return item, claim, alice, bob

    def question_row(self, question_id):
        return self.conn.execute(
            "SELECT * FROM questions WHERE question_id=?",
            (question_id,)).fetchone()

    def item_row(self, item_id):
        return self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


class NeedsInput(QuestionBoard):
    def test_nine_step_transition_is_atomic_and_exact(self):
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        crow = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?",
            (claim["claim_id"],)).fetchone()
        self.assertEqual(crow["status"], "closed")
        self.assertEqual(crow["close_reason"], "needs_input")
        irow = self.item_row(item)
        self.assertEqual(irow["status"], "needs_input")
        self.assertEqual(irow["owner_agent_id"], "alice")  # preserved
        self.assertEqual(irow["preferred_resume_owner_agent_id"], "alice")
        self.assertEqual(irow["next_actor_agent_id"], "bob")
        self.assertIsNone(irow["resume_grace_started_at"])  # starts at answer
        qrow = self.question_row(qid)
        self.assertEqual(qrow["status"], "open")
        self.assertEqual(qrow["exact_question"], "exact?")
        self.assertEqual(qrow["asked_by_agent"], "alice")
        self.assertEqual(qrow["asked_by_session"], "s-alice")
        self.assertEqual(qrow["assigned_to_agent"], "bob")
        self.assertEqual(len(self.events_of("needs_input")), 1)
        deliveries = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='bob' "
            "AND category='question'").fetchall()
        self.assertEqual(len(deliveries), 1)

    def test_needs_input_rolls_back_whole(self):
        item, claim, alice, bob = self.working_pair()
        real_append = coopdb.append_event

        def sabotage(conn, **kw):
            if kw.get("event_type") == "needs_input":
                raise RuntimeError("forced")
            return real_append(conn, **kw)

        with unittest.mock.patch.object(coopdb, "append_event", sabotage):
            with self.assertRaises(RuntimeError):
                self.needs_input(claim["claim_id"], "s-alice")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM questions").fetchone()["n"], 0)
        crow = self.conn.execute(
            "SELECT status FROM claims WHERE claim_id=?",
            (claim["claim_id"],)).fetchone()
        self.assertEqual(crow["status"], "active")
        self.assertEqual(self.item_row(item)["status"], "working")

    def test_question_must_be_exact_and_target_registered(self):
        item, claim, alice, bob = self.working_pair()
        for bad in (None, "", "   "):
            with self.subTest(question=bad):
                with self.assertRaises(InvalidTransition) as caught:
                    self.needs_input(
                        claim["claim_id"], "s-alice", question=bad)
                self.assertEqual(caught.exception.reason_code, "input_invalid")
        with self.assertRaises(NotFound) as missing:
            self.needs_input(claim["claim_id"], "s-alice",
                             to_agent="nobody-registered")
        self.assertEqual(missing.exception.reason_code, "target_not_found")
        self.assertEqual(
            missing.exception.evidence["target_agent_id"],
            "nobody-registered",
        )
        coopdb.register_agent(self.conn, "offline-peer")
        with self.assertRaises(InvalidTransition) as offline:
            self.needs_input(
                claim["claim_id"], "s-alice", to_agent="offline-peer")
        self.assertEqual(offline.exception.reason_code, "peer_unavailable")
        self.assertEqual(offline.exception.evidence["item_id"], item)
        self.assertEqual(
            offline.exception.evidence["target_agent_id"], "offline-peer")
        self.assertEqual(
            offline.exception.evidence["session_status"], "missing")
        self.assertEqual(self.item_row(item)["status"], "working")

    def test_no_lease_renewable_while_needs_input(self):
        item, claim, alice, bob = self.working_pair()
        self.needs_input(claim["claim_id"], "s-alice")
        self.assertEqual(
            coopdb.renew_claims(self.conn, session_id="s-alice"), 0)

    def test_open_question_blocks_fresh_implementation_claims(self):
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        with self.assertRaises(InvalidTransition) as caught:
            self.claim(item, "alice", "s-alice",
                       reclaim_reason="jumping the question")
        self.assertEqual(caught.exception.reason_code, "blocking_work_open")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["blocking_object_type"], "question")
        self.assertEqual(caught.exception.evidence["blocking_ids"], (qid,))


class ClaimQuestion(QuestionBoard):
    def _stage(self):
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        return item, qid

    def test_only_the_addressed_agent_may_claim(self):
        item, qid = self._stage()
        carol = self.make_session("carol")
        before = snapshot(self.conn)
        with self.assertRaises(AddressedTargetMismatch) as caught:
            coopdb.claim_question(
                self.conn, question_id=qid, session_id=carol,
                intent="not mine")
        self.assertEqual(
            caught.exception.reason_code, "addressed_target_mismatch")
        self.assertEqual(caught.exception.evidence["question_id"], qid)
        self.assertEqual(caught.exception.evidence["actor_agent_id"], "carol")
        self.assertEqual(caught.exception.evidence["required_agent_id"], "bob")
        self.assertEqual(before, snapshot(self.conn))

    def test_addressed_claim_takes_the_question_lane(self):
        item, qid = self._stage()
        result = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it")
        row = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?",
            (result["claim_id"],)).fetchone()
        self.assertEqual(row["claim_kind"], "question_response")
        self.assertEqual(row["lane_key"], f"question_response:{qid}")
        self.assertEqual(row["subject_id"], qid)
        self.assertEqual(self.item_row(item)["status"], "needs_input")
        with self.assertRaises(ClaimCollision):
            coopdb.claim_question(
                self.conn, question_id=qid, session_id="s-bob",
                intent="again")

    def test_question_claim_accepts_runner_sized_lease(self):
        _item, qid = self._stage()
        expected = coopdb._ts(3600)
        result = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it",
            lease_seconds=3600)
        self.assertEqual(result["lease_expires_at"], expected)

    def test_missing_or_answered_questions_are_refused(self):
        item, qid = self._stage()
        with self.assertRaises(NotFound) as caught:
            coopdb.claim_question(
                self.conn, question_id=999, session_id="s-bob", intent="x")
        self.assertEqual(caught.exception.reason_code, "target_not_found")
        self.assertEqual(caught.exception.evidence["question_id"], 999)
        result = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it")
        coopdb.answer_question(
            self.conn, claim_id=result["claim_id"], session_id="s-bob",
            answer="42")
        with self.assertRaises(InvalidTransition):
            coopdb.claim_question(
                self.conn, question_id=qid, session_id="s-bob",
                intent="too late")


class AnswerQuestion(QuestionBoard):
    def _claimed(self):
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        response = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it")
        return item, qid, response, claim

    def test_answer_transition_is_exact(self):
        item, qid, response, pre_claim = self._claimed()
        coopdb.answer_question(
            self.conn, claim_id=response["claim_id"], session_id="s-bob",
            answer="use the staging key")
        crow = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?",
            (response["claim_id"],)).fetchone()
        self.assertEqual(crow["status"], "completed")
        qrow = self.question_row(qid)
        self.assertEqual(qrow["status"], "answered")
        self.assertEqual(qrow["answer"], "use the staging key")
        self.assertEqual(qrow["answered_by_agent"], "bob")
        self.assertEqual(qrow["answered_by_session"], "s-bob")
        # Grace lives on the item ONLY; the legacy question columns stay null.
        self.assertIsNone(qrow["preferred_resume_owner_agent_id"])
        self.assertIsNone(qrow["resume_grace_expires_at"])
        irow = self.item_row(item)
        self.assertEqual(irow["status"], "needs_input")  # still, unclaimed
        self.assertEqual(irow["next_actor_agent_id"], "alice")  # back to owner
        self.assertEqual(irow["preferred_resume_owner_agent_id"], "alice")
        self.assertIsNotNone(irow["resume_grace_started_at"])
        self.assertIsNotNone(irow["resume_grace_expires_at"])
        deliveries = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='alice' "
            "AND category='answer'").fetchall()
        self.assertEqual(len(deliveries), 1)

    def test_answer_must_be_non_empty(self):
        item, qid, response, _ = self._claimed()
        with self.assertRaises(InvalidTransition):
            coopdb.answer_question(
                self.conn, claim_id=response["claim_id"],
                session_id="s-bob", answer="  ")


class AtomicQuestionBatches(QuestionBoard):
    def _batch_stage(self, recipients=("bob", "carol")):
        item = self.make_item()
        self.make_session("alice", sid="s-alice")
        for recipient in dict.fromkeys(recipients):
            self.make_session(recipient, sid=f"s-{recipient}")
        claim = self.claim(item, "alice", "s-alice")
        questions = [
            (recipient, f"exact question for {recipient} #{index}")
            for index, recipient in enumerate(recipients, start=1)
        ]
        return item, claim, questions

    def test_outbound_batch_keeps_ordinary_rows_events_and_deliveries(self):
        item, claim, questions = self._batch_stage()

        ids = coopdb.needs_input_batch(
            self.conn,
            claim_id=claim["claim_id"],
            session_id="s-alice",
            questions=questions,
        )

        self.assertEqual(len(ids), 2)
        rows = self.conn.execute(
            "SELECT * FROM questions ORDER BY question_id"
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["question_id"],
                    row["asked_by_agent"],
                    row["asked_by_session"],
                    row["assigned_to_agent"],
                    row["exact_question"],
                    row["status"],
                )
                for row in rows
            ],
            [
                (ids[0], "alice", "s-alice", "bob", questions[0][1], "open"),
                (ids[1], "alice", "s-alice", "carol", questions[1][1], "open"),
            ],
        )
        implementation = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?",
            (claim["claim_id"],),
        ).fetchone()
        self.assertEqual(
            (implementation["status"], implementation["close_reason"]),
            ("closed", "needs_input"),
        )
        item_row = self.item_row(item)
        self.assertEqual(item_row["status"], "needs_input")
        self.assertEqual(item_row["owner_agent_id"], "alice")
        self.assertEqual(
            item_row["preferred_resume_owner_agent_id"], "alice")
        self.assertEqual(item_row["next_actor_agent_id"], "bob")
        events = self.events_of("needs_input")
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [json.loads(event["payload_json"])["question_id"]
             for event in events],
            ids,
        )
        deliveries = self.conn.execute(
            "SELECT recipient_agent_id, source_event_id FROM inbox_entries "
            "WHERE category='question' ORDER BY inbox_entry_id"
        ).fetchall()
        self.assertEqual(
            [row["recipient_agent_id"] for row in deliveries],
            ["bob", "carol"],
        )
        self.assertEqual(
            [row["source_event_id"] for row in deliveries],
            [event["event_id"] for event in events],
        )

    def test_outbound_batch_prevalidates_every_input_and_target(self):
        item, claim, questions = self._batch_stage()
        cases = [
            ([], "input_invalid"),
            ([("bob", "")], "input_invalid"),
            ([("human", "q")], "human_lane_forbidden"),
            ([("missing", "q")], "target_not_found"),
            ([("bob", "x" * 9000)], "input_invalid"),
        ]
        for values, reason_code in cases:
            with self.subTest(values=values[:1], reason_code=reason_code):
                before = batch_snapshot(self.conn)
                with self.assertRaises(coopdb.CoopError) as caught:
                    coopdb.needs_input_batch(
                        self.conn,
                        claim_id=claim["claim_id"],
                        session_id="s-alice",
                        questions=values,
                    )
                self.assertEqual(caught.exception.reason_code, reason_code)
                self.assertEqual(batch_snapshot(self.conn), before)

        coopdb.register_agent(self.conn, "offline")
        before = batch_snapshot(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.needs_input_batch(
                self.conn,
                claim_id=claim["claim_id"],
                session_id="s-alice",
                questions=[("bob", "valid"), ("offline", "not routable")],
            )
        self.assertEqual(caught.exception.reason_code, "peer_unavailable")
        self.assertEqual(batch_snapshot(self.conn), before)

        peers = []
        for index in range(coopdb.MAX_QUESTION_BATCH_SIZE + 1):
            name = f"peer-{index}"
            self.make_session(name, sid=f"s-{name}")
            peers.append((name, "q"))
        before = batch_snapshot(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.needs_input_batch(
                self.conn,
                claim_id=claim["claim_id"],
                session_id="s-alice",
                questions=peers,
            )
        self.assertEqual(caught.exception.reason_code, "input_invalid")
        self.assertEqual(batch_snapshot(self.conn), before)

    def test_outbound_batch_rolls_back_after_a_late_write_failure(self):
        item, claim, questions = self._batch_stage()
        before = batch_snapshot(self.conn)
        real_append = coopdb.append_event
        seen = {"needs_input": 0}

        def sabotage(conn, **kwargs):
            if kwargs.get("event_type") == "needs_input":
                seen["needs_input"] += 1
                if seen["needs_input"] == 2:
                    raise RuntimeError("forced second event failure")
            return real_append(conn, **kwargs)

        with unittest.mock.patch.object(coopdb, "append_event", sabotage):
            with self.assertRaises(RuntimeError):
                coopdb.needs_input_batch(
                    self.conn,
                    claim_id=claim["claim_id"],
                    session_id="s-alice",
                    questions=questions,
                )
        self.assertEqual(batch_snapshot(self.conn), before)

    def test_claim_and_answer_batch_preserve_each_response_lane(self):
        item, claim, questions = self._batch_stage(("bob", "bob"))
        question_ids = coopdb.needs_input_batch(
            self.conn,
            claim_id=claim["claim_id"],
            session_id="s-alice",
            questions=questions,
        )
        claims = coopdb.claim_questions_batch(
            self.conn,
            question_ids=question_ids,
            session_id="s-bob",
            intent="answer bounded batch",
            lease_seconds=3600,
        )

        self.assertEqual(len(claims), 2)
        self.assertEqual(
            [claim["question_id"] for claim in claims], question_ids)
        self.assertEqual(
            [claim["lane"] for claim in claims],
            [f"question_response:{qid}" for qid in question_ids],
        )
        self.assertEqual(len(self.events_of("question_claimed")), 2)

        answers = [
            (claims[0]["claim_id"], "first exact answer"),
            (claims[1]["claim_id"], "second exact answer"),
        ]
        result = coopdb.answer_questions_batch(
            self.conn,
            session_id="s-bob",
            answers=answers,
        )

        self.assertEqual(result["question_ids"], question_ids)
        self.assertEqual(result["remaining_open_question_ids"], [])
        rows = self.conn.execute(
            "SELECT * FROM questions ORDER BY question_id"
        ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["answered"] * 2)
        self.assertEqual(
            [row["answer"] for row in rows],
            ["first exact answer", "second exact answer"],
        )
        self.assertEqual(
            [row["answered_by_agent"] for row in rows], ["bob", "bob"])
        claim_rows = self.conn.execute(
            "SELECT * FROM claims WHERE claim_kind='question_response' "
            "ORDER BY claim_id"
        ).fetchall()
        self.assertEqual(
            [(row["status"], row["close_reason"]) for row in claim_rows],
            [("completed", "answered"), ("completed", "answered")],
        )
        self.assertEqual(len(self.events_of("question_answered")), 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM inbox_entries WHERE category='answer'"
            ).fetchone()[0],
            2,
        )
        item_row = self.item_row(item)
        self.assertEqual(item_row["next_actor_agent_id"], "alice")
        self.assertIsNotNone(item_row["resume_grace_expires_at"])

    def test_partial_answer_batch_keeps_next_open_recipient_actionable(self):
        item, claim, questions = self._batch_stage(("bob", "carol"))
        question_ids = coopdb.needs_input_batch(
            self.conn,
            claim_id=claim["claim_id"],
            session_id="s-alice",
            questions=questions,
        )
        bob_claim = coopdb.claim_questions_batch(
            self.conn,
            question_ids=(question_ids[0],),
            session_id="s-bob",
            intent="answer mine",
        )[0]
        result = coopdb.answer_questions_batch(
            self.conn,
            session_id="s-bob",
            answers=[(bob_claim["claim_id"], "bob answer")],
        )
        self.assertEqual(
            result["remaining_open_question_ids"], [question_ids[1]])
        item_row = self.item_row(item)
        self.assertEqual(item_row["next_actor_agent_id"], "carol")
        self.assertIsNone(item_row["resume_grace_started_at"])
        self.assertIsNone(item_row["resume_grace_expires_at"])
        self.assertEqual(
            coopdb.status(self.conn, "carol", item_id=item)["next_action"][
                "target_id"],
            question_ids[1],
        )

        carol_claim = coopdb.claim_questions_batch(
            self.conn,
            question_ids=(question_ids[1],),
            session_id="s-carol",
            intent="answer mine",
        )[0]
        coopdb.answer_questions_batch(
            self.conn,
            session_id="s-carol",
            answers=[(carol_claim["claim_id"], "carol answer")],
        )
        item_row = self.item_row(item)
        self.assertEqual(item_row["next_actor_agent_id"], "alice")
        self.assertIsNotNone(item_row["resume_grace_expires_at"])

    def test_claim_and_answer_batches_fail_atomically(self):
        item, claim, questions = self._batch_stage(("bob", "bob"))
        question_ids = coopdb.needs_input_batch(
            self.conn,
            claim_id=claim["claim_id"],
            session_id="s-alice",
            questions=questions,
        )
        before = batch_snapshot(self.conn)
        with self.assertRaises(NotFound):
            coopdb.claim_questions_batch(
                self.conn,
                question_ids=(question_ids[0], 999999),
                session_id="s-bob",
                intent="invalid group",
            )
        self.assertEqual(batch_snapshot(self.conn), before)

        claims = coopdb.claim_questions_batch(
            self.conn,
            question_ids=question_ids,
            session_id="s-bob",
            intent="valid group",
        )
        before = batch_snapshot(self.conn)
        with self.assertRaises(InvalidTransition):
            coopdb.answer_questions_batch(
                self.conn,
                session_id="s-bob",
                answers=[
                    (claims[0]["claim_id"], "valid"),
                    (claims[1]["claim_id"], ""),
                ],
            )
        self.assertEqual(batch_snapshot(self.conn), before)

        with self.assertRaises(NotFound):
            coopdb.answer_questions_batch(
                self.conn,
                session_id="s-bob",
                answers=[
                    (claims[0]["claim_id"], "valid"),
                    (999999, "missing"),
                ],
            )
        self.assertEqual(batch_snapshot(self.conn), before)

    def test_concurrent_batch_claims_have_one_complete_winner(self):
        item, claim, questions = self._batch_stage(("bob", "bob"))
        question_ids = coopdb.needs_input_batch(
            self.conn,
            claim_id=claim["claim_id"],
            session_id="s-alice",
            questions=questions,
        )
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def compete(label):
            conn = coopdb.connect(self.db, require_current=True)
            try:
                barrier.wait(timeout=2)
                try:
                    value = coopdb.claim_questions_batch(
                        conn,
                        question_ids=question_ids,
                        session_id="s-bob",
                        intent=f"batch contender {label}",
                    )
                    outcome = ("ok", value)
                except coopdb.CoopError as exc:
                    outcome = ("error", exc.reason_code)
                with lock:
                    results.append(outcome)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=compete, args=(label,))
            for label in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([kind for kind, _value in results].count("ok"), 1)
        self.assertEqual([kind for kind, _value in results].count("error"), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM claims WHERE "
                "claim_kind='question_response'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(len(self.events_of("question_claimed")), 2)


class AdminAnswer(QuestionBoard):
    def _open_question(self, with_response_claim=False):
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        response = None
        if with_response_claim:
            response = coopdb.claim_question(
                self.conn, question_id=qid, session_id="s-bob",
                intent="slow")
        return item, qid, response

    def test_rejected_inside_a_session(self):
        item, qid, _ = self._open_question()
        with unittest.mock.patch.dict(
                "os.environ", {"COOP_SESSION_ID": "s-bob"}):
            with self.assertRaises(HumanLaneViolation):
                coopdb.admin_answer(
                    self.conn, question_id=qid, answer="a", reason="r")

    def test_reason_and_answer_are_mandatory(self):
        item, qid, _ = self._open_question()
        with self.assertRaises(InvalidTransition):
            coopdb.admin_answer(
                self.conn, question_id=qid, answer="a", reason="  ")
        with self.assertRaises(InvalidTransition):
            coopdb.admin_answer(
                self.conn, question_id=qid, answer="", reason="r")

    def test_supersedes_the_active_response_claim(self):
        item, qid, response = self._open_question(with_response_claim=True)
        coopdb.admin_answer(
            self.conn, question_id=qid, answer="operator says staging",
            reason="agent stalled")
        crow = self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?",
            (response["claim_id"],)).fetchone()
        self.assertEqual(crow["status"], "closed")
        self.assertEqual(crow["close_reason"], "superseded_by_human")
        qrow = self.question_row(qid)
        self.assertEqual(qrow["status"], "answered")
        self.assertEqual(qrow["answered_by_agent"], "human")
        self.assertIsNone(qrow["answered_by_session"])
        event = self.events_of("question_answered")[-1]
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["superseded_claim_id"],
                         response["claim_id"])
        self.assertEqual(payload["original_target"], "bob")
        self.assertEqual(payload["reason"], "agent stalled")
        self.assertEqual(event["actor_agent_id"], "human")
        self.assertIsNone(event["actor_session_id"])
        # The late answer through the superseded claim is fenced out.
        with self.assertRaises(StaleClaim):
            coopdb.answer_question(
                self.conn, claim_id=response["claim_id"],
                session_id="s-bob", answer="too late")
        # Grace opened on the item exactly as the agent path does.
        irow = self.item_row(item)
        self.assertEqual(irow["next_actor_agent_id"], "alice")
        self.assertIsNotNone(irow["resume_grace_expires_at"])

    def test_answers_without_any_response_claim(self):
        item, qid, _ = self._open_question(with_response_claim=False)
        coopdb.admin_answer(
            self.conn, question_id=qid, answer="just do it",
            reason="no agent picked it up")
        self.assertEqual(self.question_row(qid)["status"], "answered")

    def test_answered_questions_are_refused(self):
        item, qid, _ = self._open_question()
        coopdb.admin_answer(
            self.conn, question_id=qid, answer="a", reason="r")
        with self.assertRaises(InvalidTransition):
            coopdb.admin_answer(
                self.conn, question_id=qid, answer="b", reason="again")


class ResumeAndTakeover(QuestionBoard):
    def _answered(self):
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        response = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it")
        coopdb.answer_question(
            self.conn, claim_id=response["claim_id"], session_id="s-bob",
            answer="answered")
        return item, claim

    def test_owner_resume_clears_all_three_grace_fields(self):
        item, old_claim = self._answered()
        result = self.claim(item, "alice", "s-alice",
                            reclaim_reason="resuming after answer")
        irow = self.item_row(item)
        self.assertEqual(irow["status"], "working")
        self.assertEqual(irow["owner_agent_id"], "alice")
        self.assertIsNone(irow["preferred_resume_owner_agent_id"])
        self.assertIsNone(irow["resume_grace_started_at"])
        self.assertIsNone(irow["resume_grace_expires_at"])
        self.assertNotEqual(result["claim_id"], old_claim["claim_id"])

    def test_non_owner_is_refused_while_grace_runs(self):
        item, _ = self._answered()
        carol = self.make_session("carol")
        with self.assertRaises(InvalidTransition):
            coopdb.claim_item(
                self.conn, item_id=item, actor="carol", session_id=carol,
                intent="jump", reclaim_reason="impatient")

    def test_takeover_after_expiry_transfers_and_clears(self):
        item, _ = self._answered()
        stored_before = tuple(self.item_row(item))
        self.clock.advance(GRACE)  # at expiry: the window is over
        # resume_stale is derived, never stored: the row is unchanged.
        self.assertEqual(tuple(self.item_row(item)), stored_before)
        carol = self.make_session("carol")
        coopdb.claim_item(
            self.conn, item_id=item, actor="carol", session_id=carol,
            intent="rescue", reclaim_reason="grace expired, taking over")
        irow = self.item_row(item)
        self.assertEqual(irow["owner_agent_id"], "carol")
        self.assertEqual(irow["status"], "working")
        self.assertIsNone(irow["preferred_resume_owner_agent_id"])
        self.assertIsNone(irow["resume_grace_expires_at"])
        transfers = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='alice' "
            "AND category='ownership_transfer'").fetchall()
        self.assertEqual(len(transfers), 1)

    def test_delayed_pre_question_claim_writes_are_refused(self):
        item, old_claim = self._answered()
        self.claim(item, "alice", "s-alice", reclaim_reason="resuming")
        with self.assertRaises(StaleClaim):
            coopdb.checkpoint(
                self.conn, ctype="step", claim_id=old_claim["claim_id"],
                actor="alice", session_id="s-alice")
        with self.assertRaises(StaleClaim):
            coopdb.needs_input(
                self.conn, claim_id=old_claim["claim_id"],
                session_id="s-alice", to_agent="bob", question="again?")

    def test_expired_grace_routing_three_case_matrix(self):
        """Item-25 P0: post-expiry next_action without weakening live grace.

        1. During grace: preferred resume_task; non-preferred no reclaim action
           and claim_item refused.
        2. After grace: preferred still resume_task; non-preferred claim_task
           with --reclaim; reclaim succeeds and clears grace fields.
        3. During grace (negative): non-preferred never gets reclaim next_action.
        """
        item, _ = self._answered()
        self.make_session("carol", sid="s-carol")

        # --- Case 1 + 3: live grace ---
        alice_na = coopdb.status(self.conn, "alice")["next_action"]
        bob_na = coopdb.status(self.conn, "bob")["next_action"]
        carol_na = coopdb.status(self.conn, "carol")["next_action"]
        self.assertEqual(alice_na["kind"], "resume_task")
        self.assertNotEqual(bob_na["kind"], "resume_task")
        self.assertNotIn("--reclaim", " ".join(bob_na.get("command") or []))
        self.assertNotEqual(carol_na["kind"], "resume_task")
        self.assertNotIn("--reclaim", " ".join(carol_na.get("command") or []))
        with self.assertRaises(InvalidTransition):
            coopdb.claim_item(
                self.conn, item_id=item, actor="carol", session_id="s-carol",
                intent="jump", reclaim_reason="impatient")

        # --- Case 2: post-expiry ---
        self.clock.advance(GRACE + 1)
        alice_na = coopdb.status(self.conn, "alice")["next_action"]
        carol_na = coopdb.status(self.conn, "carol")["next_action"]
        self.assertEqual(alice_na["kind"], "resume_task")
        self.assertEqual(carol_na["kind"], "claim_task")
        cmd = " ".join(carol_na.get("command") or [])
        self.assertIn("--reclaim", cmd)
        self.assertIn("{reason}", cmd)
        self.assertIn("reason", carol_na.get("required_inputs") or [])
        coopdb.claim_item(
            self.conn, item_id=item, actor="carol", session_id="s-carol",
            intent="rescue", reclaim_reason="grace expired, taking over")
        irow = self.item_row(item)
        self.assertEqual(irow["owner_agent_id"], "carol")
        self.assertIsNone(irow["preferred_resume_owner_agent_id"])
        self.assertIsNone(irow["resume_grace_expires_at"])

    def test_preferred_expired_resume_beats_foreign_expired_reclaim(self):
        """Preferred owner's own expired resume stays resume_task priority.

        Regression: a global ORDER BY id LIMIT 1 on any expired-grace item
        could surface a foreign reclaim-shaped claim_task for the preferred
        owner and hide their own resume_task.
        """
        self.make_session("alice", sid="s-alice-f")
        self.make_session("bob", sid="s-bob-f")

        # Lower-id item: bob is preferred (foreign to alice).
        foreign = self.make_item(title="foreign-expired")
        f_claim = self.claim(foreign, "bob", "s-bob-f")
        fq = self.needs_input(f_claim["claim_id"], "s-bob-f", to_agent="alice")
        fr = coopdb.claim_question(
            self.conn, question_id=fq, session_id="s-alice-f", intent="a")
        coopdb.answer_question(
            self.conn, claim_id=fr["claim_id"], session_id="s-alice-f",
            answer="foreign answered")

        # Higher-id item: alice is preferred.
        mine = self.make_item(title="mine-expired")
        m_claim = self.claim(mine, "alice", "s-alice-f")
        mq = self.needs_input(m_claim["claim_id"], "s-alice-f", to_agent="bob")
        mr = coopdb.claim_question(
            self.conn, question_id=mq, session_id="s-bob-f", intent="b")
        coopdb.answer_question(
            self.conn, claim_id=mr["claim_id"], session_id="s-bob-f",
            answer="mine answered")

        self.assertLess(foreign, mine)  # foreign id precedes mine
        self.clock.advance(GRACE + 1)

        alice_na = coopdb.status(self.conn, "alice")["next_action"]
        # Must be preferred resume on *mine*, not reclaim-shaped claim_task
        # on the lower-id foreign item.
        self.assertEqual(alice_na["kind"], "resume_task", alice_na)
        self.assertEqual(alice_na["item_id"], mine)
        self.assertEqual(alice_na["target_id"], mine)

        # Bob (preferred on foreign) still resumes foreign first.
        bob_na = coopdb.status(self.conn, "bob")["next_action"]
        self.assertEqual(bob_na["kind"], "resume_task", bob_na)
        self.assertEqual(bob_na["item_id"], foreign)

    def test_active_claim_beats_foreign_expired_reclaim(self):
        """Own live claim work stays ahead of reclaiming somebody else's item."""
        self.make_session("alice", sid="s-alice-a")
        self.make_session("bob", sid="s-bob-a")
        self.make_session("carol", sid="s-carol-a")

        # Somebody else's expired resume-grace item (alice preferred).
        foreign = self.make_item(title="foreign-grace")
        f_claim = self.claim(foreign, "alice", "s-alice-a")
        fq = self.needs_input(f_claim["claim_id"], "s-alice-a", to_agent="bob")
        fr = coopdb.claim_question(
            self.conn, question_id=fq, session_id="s-bob-a", intent="a")
        coopdb.answer_question(
            self.conn, claim_id=fr["claim_id"], session_id="s-bob-a",
            answer="answered")
        self.clock.advance(GRACE + 1)

        # Carol has her own active implementation claim.
        mine = self.make_item(title="carol-live-work")
        mine_claim = self.claim(mine, "carol", "s-carol-a")

        carol_na = coopdb.status(self.conn, "carol")["next_action"]
        # Must continue own work, not reclaim alice's expired grace item.
        self.assertIn(carol_na["kind"], (
            "continue_task", "define_contract", "open_huddle",
            "request_review", "complete_task", "idle"), carol_na)
        self.assertNotEqual(carol_na["kind"], "claim_task")
        if carol_na.get("item_id") is not None:
            self.assertEqual(carol_na["item_id"], mine)
        if carol_na.get("claim_id") is not None:
            self.assertEqual(carol_na["claim_id"], mine_claim["claim_id"])

    def test_recover_claim_beats_foreign_expired_grace_reclaim(self):
        """Non-owner grace takeover is ordinary claim_task tier, under recover."""
        self.make_session("alice", sid="s-alice-r")
        self.make_session("bob", sid="s-bob-r")
        self.make_session("carol", sid="s-carol-r")
        # Foreign expired grace (alice preferred); carol answers then has no claim.
        foreign = self.make_item(title="grace-for-recover-test")
        f_claim = self.claim(foreign, "alice", "s-alice-r")
        fq = self.needs_input(
            f_claim["claim_id"], "s-alice-r", to_agent="carol")
        fr = coopdb.claim_question(
            self.conn, question_id=fq, session_id="s-carol-r", intent="a")
        coopdb.answer_question(
            self.conn, claim_id=fr["claim_id"], session_id="s-carol-r",
            answer="done")
        self.clock.advance(GRACE + 1)
        # Bob has a recoverable stale claim of his own (separate from answerer).
        stale_item = self.make_item(title="bob-stale")
        self.claim(stale_item, "bob", "s-bob-r")
        self.clock.advance(3600 + 1)
        coopdb.sweep_expired(self.conn)
        coopdb.finish_session(
            self.conn, "s-bob-r", status="exited", reason="child_exit",
            exit_code=0)
        self.make_session("bob", sid="s-bob-live")
        bob_na = coopdb.status(self.conn, "bob")["next_action"]
        self.assertEqual(bob_na["kind"], "recover_claim", bob_na)

    def test_foreign_expired_grace_reclaim_is_ordinary_claim_task_tier(self):
        """With no own work, non-owner takeover competes as claim_task by id."""
        self.make_session("alice", sid="s-alice-q")
        self.make_session("carol", sid="s-carol-q")
        # Lower-id todo (ordinary claim) vs higher-id expired grace reclaim.
        todo = self.make_item(title="todo-first")
        foreign = self.make_item(title="grace-second")
        self.assertLess(todo, foreign)
        f_claim = self.claim(foreign, "alice", "s-alice-q")
        fq = self.needs_input(
            f_claim["claim_id"], "s-alice-q", to_agent="carol")
        # Need a live peer to answer — bob
        self.make_session("bob", sid="s-bob-q")
        # re-open: needs_input already targeted carol; carol must answer
        fr = coopdb.claim_question(
            self.conn, question_id=fq, session_id="s-carol-q", intent="a")
        coopdb.answer_question(
            self.conn, claim_id=fr["claim_id"], session_id="s-carol-q",
            answer="answered")
        self.clock.advance(GRACE + 1)
        # Carol has no active claim; both todo and reclaim available.
        # Lower item id (todo) wins at ordinary claim_task priority.
        carol_na = coopdb.status(self.conn, "carol")["next_action"]
        self.assertEqual(carol_na["kind"], "claim_task", carol_na)
        self.assertEqual(carol_na["item_id"], todo)
        cmd = " ".join(carol_na.get("command") or [])
        self.assertNotIn("--reclaim", cmd)

        # Without the todo, reclaim surfaces as claim_task with --reclaim.
        self.conn.execute("UPDATE items SET status='done' WHERE id=?", (todo,))
        self.conn.commit()
        carol_na = coopdb.status(self.conn, "carol")["next_action"]
        self.assertEqual(carol_na["kind"], "claim_task", carol_na)
        self.assertEqual(carol_na["item_id"], foreign)
        self.assertIn("--reclaim", " ".join(carol_na.get("command") or []))


class CliQuestions(QuestionBoard):
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

    def test_cli_full_cycle_is_tokenless(self):
        item, claim, alice, bob = self.working_pair()
        alice_env = {"COOP_SESSION_ID": "s-alice", "COOP_AGENT": "alice"}
        bob_env = {"COOP_SESSION_ID": "s-bob", "COOP_AGENT": "bob"}
        code, out, err = self._run(
            ["needs-input", "--claim", str(claim["claim_id"]),
             "--to", "bob", "--question", "which db?"], alice_env)
        self.assertEqual(code, 0, err)
        qid = self.conn.execute(
            "SELECT question_id FROM questions").fetchone()["question_id"]
        code, out, err = self._run(
            ["question", "claim", str(qid), "--intent", "answering"],
            bob_env)
        self.assertEqual(code, 0, err)
        self.assertNotIn("fencing_token", out + err)
        response_id = self.conn.execute(
            "SELECT claim_id FROM claims WHERE claim_kind="
            "'question_response'").fetchone()["claim_id"]
        code, out, err = self._run(
            ["question", "answer", "--claim", str(response_id),
             "--answer", "staging"], bob_env)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.question_row(qid)["status"], "answered")

    def test_cli_outbound_batch_keeps_legacy_single_form(self):
        item, claim, alice, bob = self.working_pair()
        self.make_session("carol", sid="s-carol")
        alice_env = {"COOP_SESSION_ID": "s-alice", "COOP_AGENT": "alice"}

        code, out, err = self._run(
            [
                "needs-input", "batch",
                "--claim", str(claim["claim_id"]),
                "--question", "bob", "bob exact?",
                "--question", "carol", "carol exact?",
            ],
            alice_env,
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(len(payload["question_ids"]), 2)
        rows = self.conn.execute(
            "SELECT assigned_to_agent, exact_question FROM questions "
            "ORDER BY question_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("bob", "bob exact?"), ("carol", "carol exact?")],
        )

        # A fresh item proves the original no-subcommand syntax still works.
        item2 = self.make_item(title="legacy-single")
        claim2 = self.claim(item2, "alice", "s-alice")
        code, out, err = self._run(
            [
                "needs-input",
                "--claim", str(claim2["claim_id"]),
                "--to", "bob",
                "--question", "legacy exact?",
            ],
            alice_env,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("question", out)

    def test_cli_admin_answer_env_guard_and_happy_path(self):
        item, claim, alice, bob = self.working_pair()
        self.needs_input(claim["claim_id"], "s-alice")
        qid = self.conn.execute(
            "SELECT question_id FROM questions").fetchone()["question_id"]
        code, out, err = self._run(
            ["admin", "answer", str(qid), "--answer", "a", "--reason", "r"],
            {"COOP_SESSION_ID": "s-alice", "COOP_AGENT": "alice"})
        self.assertEqual(code, 1)
        self.assertIn("human_lane_violation", err)
        code, out, err = self._run(
            ["admin", "answer", str(qid), "--answer", "use prod",
             "--reason", "operator call"],
            {"COOP_SESSION_ID": "", "COOP_AGENT": ""})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.question_row(qid)["status"], "answered")

    def test_cli_question_claim_reclaim_pairing_and_path(self):
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it")
        self.clock.advance(30 + 1)
        coopdb.sweep_expired(self.conn)
        coopdb.finish_session(
            self.conn, "s-bob", status="exited", reason="child_exit",
            exit_code=0)
        self.make_session("bob", sid="s-bob2")
        env = {"COOP_SESSION_ID": "s-bob2", "COOP_AGENT": "bob"}
        code, out, err = self._run(
            ["question", "claim", str(qid), "--intent", "x", "--reclaim"],
            env)
        self.assertEqual(code, 1)
        self.assertIn("invalid_transition", err)
        code, out, err = self._run(
            ["question", "claim", str(qid), "--intent", "x",
             "--reason", "orphaned"], env)
        self.assertEqual(code, 1)
        self.assertIn("invalid_transition", err)
        code, out, err = self._run(
            ["question", "claim", str(qid), "--intent", "resume",
             "--reclaim", "--reason", "fresh session"], env)
        self.assertEqual(code, 0, err)
        self.assertNotIn("fencing_token", out + err)
        rows = self.conn.execute(
            "SELECT status FROM claims WHERE lane_key=? ORDER BY claim_id",
            (f"question_response:{qid}",)).fetchall()
        self.assertEqual([r["status"] for r in rows], ["stale", "active"])


class QuestionLaneGuard(QuestionBoard):
    """The question-response lane rides the shared
    lane-history guard — the swept-stale hole closes here too, and the
    lane gains its first reclaim surface."""

    def _swept_response_lane(self):
        """Bob's response claim expires and the maintenance sweep flips it
        stale while bob's session still reports running."""
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        first = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it")
        self.clock.advance(30 + 1)
        flipped = coopdb.sweep_expired(self.conn)
        self.assertIn(first["claim_id"], flipped)
        return item, qid, first

    def _response_rows(self, qid):
        return self.conn.execute(
            "SELECT * FROM claims WHERE lane_key=? ORDER BY claim_id",
            (f"question_response:{qid}",)).fetchall()

    def test_swept_stale_response_reclaim_refused_while_owner_runs(self):
        item, qid, first = self._swept_response_lane()
        before = snapshot(self.conn)
        with self.assertRaises(UnsafeReclaim):
            coopdb.claim_question(
                self.conn, question_id=qid, session_id="s-bob",
                intent="retry", reclaim_reason="it was mine")
        self.assertEqual(before, snapshot(self.conn))

    def test_target_reclaims_safely_stale_response_with_reason(self):
        item, qid, first = self._swept_response_lane()
        coopdb.finish_session(
            self.conn, "s-bob", status="exited", reason="child_exit",
            exit_code=0)
        self.make_session("bob", sid="s-bob2")
        with self.assertRaises(InvalidTransition):
            coopdb.claim_question(
                self.conn, question_id=qid, session_id="s-bob2",
                intent="resume")  # reason-less on a lane with history
        result = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob2",
            intent="resume", reclaim_reason="fresh session after crash")
        rows = self._response_rows(qid)
        self.assertEqual([r["status"] for r in rows], ["stale", "active"])
        self.assertGreater(rows[1]["fencing_token"], rows[0]["fencing_token"])
        self.assertEqual(rows[1]["claim_id"], result["claim_id"])

    def test_answered_question_lane_refuses_resurrection(self):
        # Subject eligibility fires before lane logic: an answered
        # question refuses regardless of history shape or reason.
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        first = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it")
        coopdb.answer_question(
            self.conn, claim_id=first["claim_id"], session_id="s-bob",
            answer="42")
        before = snapshot(self.conn)
        for reason in (None, "resurrect it"):
            with self.subTest(reason=reason):
                with self.assertRaises(InvalidTransition):
                    coopdb.claim_question(
                        self.conn, question_id=qid, session_id="s-bob",
                        intent="again", reclaim_reason=reason)
        self.assertEqual(before, snapshot(self.conn))

    def test_non_target_reclaim_of_stale_lane_is_addressed_mismatch(self):
        item, qid, first = self._swept_response_lane()
        coopdb.finish_session(
            self.conn, "s-bob", status="exited", reason="child_exit",
            exit_code=0)
        carol = self.make_session("carol")
        before = snapshot(self.conn)
        with self.assertRaises(AddressedTargetMismatch):
            coopdb.claim_question(
                self.conn, question_id=qid, session_id=carol,
                intent="steal", reclaim_reason="mine now")
        self.assertEqual(before, snapshot(self.conn))


class QuestionLaneAware(QuestionBoard):
    """Question-response next_action across the five
    lane states — unsafe stale demotes to a warning, my own safely stale
    lane surfaces recover_claim, answered questions surface nothing."""

    def _stale_response_lane(self):
        """Bob claims his question's response lane; the lease dies while
        his session still runs."""
        from tests.test_claims import LEASE
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob",
            intent="answering")
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        return qid

    def test_unsafe_response_lane_demotes_to_warning(self):
        self._stale_response_lane()
        status = coopdb.status(self.conn, "bob")
        self.assertNotEqual(status["next_action"]["kind"], "answer_question")
        self.assertTrue(any("question" in w for w in status["warnings"]),
                        status["warnings"])
        coopdb.finish_session(
            self.conn, "s-bob", status="exited", reason="child_exit",
            exit_code=0)
        status = coopdb.status(self.conn, "bob")
        self.assertEqual(status["next_action"]["kind"], "recover_claim")

    def test_answered_question_surfaces_nothing(self):
        qid = self._stale_response_lane()
        coopdb.finish_session(
            self.conn, "s-bob", status="exited", reason="child_exit",
            exit_code=0)
        coopdb.admin_answer(
            self.conn, question_id=qid, answer="handled by the operator",
            reason="agent lane died")
        status = coopdb.status(self.conn, "bob")
        self.assertNotEqual(status["next_action"]["kind"], "answer_question")
        self.assertNotEqual(status["next_action"]["kind"], "recover_claim")

    def test_open_unclaimed_question_still_hints_answer(self):
        item, claim, alice, bob = self.working_pair()
        self.needs_input(claim["claim_id"], "s-alice")
        status = coopdb.status(self.conn, "bob")
        self.assertEqual(status["next_action"]["kind"], "answer_question")


if __name__ == "__main__":
    unittest.main()
