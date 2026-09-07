"""Envelope projection: the seven-part test plan.

Unit tests drive synthetic event rows (the projection's contract is the
events stream); one integration test drives the real question flow through
coopdb so an event_type rename cannot silently break the registry.
"""
import json
import unittest
import uuid

from agent_coop import coop_envelope
from agent_coop import coopdb
from tests.test_claims import ClaimBoard, contract_kwargs


def _insert_event(conn, event_type, payload, *, item_id=None, actor=None):
    conn.execute(
        "INSERT INTO events (item_id, event_type, actor_agent_id, "
        "payload_json, created_at) VALUES (?,?,?,?,?)",
        (item_id, event_type, actor, json.dumps(payload), coopdb.now()))
    conn.commit()
    return conn.execute("SELECT max(event_id) FROM events").fetchone()[0]


class EnvelopeProjection(ClaimBoard):
    def test_registry_closure_unmapped_types_do_not_project(self):
        for etype in coop_envelope.TYPE_REGISTRY:
            self.assertRegex(
                coop_envelope.TYPE_REGISTRY[etype][0],
                r"^[a-z]+\.(request|response|record|post|outcome)$")
        _insert_event(self.conn, "claim_acquired", {"claim_id": 1})
        _insert_event(self.conn, "checkpoint", {"note": "x"})
        self.assertEqual(coop_envelope.project(self.conn), [])

    def test_correlation_per_family(self):
        cases = [
            ("needs_input", "question_answered", "question_id", 11),
            ("handoff_created", "handoff_accepted", "handoff_id", 21),
            ("handoff_created", "handoff_declined", "handoff_id", 22),
            ("review_requested", "review_resolved", "review_id", 31),
            ("huddle_opened", "huddle_posted", "huddle_id", 41),
            ("huddle_opened", "contract_accepted", "huddle_id", 42),
            ("huddle_opened", "contract_changes_requested", "huddle_id", 43),
            ("huddle_opened", "plan_huddle_concurred", "huddle_id", 44),
            ("huddle_opened", "plan_huddle_changes_requested", "huddle_id", 45),
        ]
        for request_type, response_type, key, oid in cases:
            with self.subTest(family=key, response=response_type):
                root = _insert_event(self.conn, request_type, {key: oid})
                resp = _insert_event(self.conn, response_type, {key: oid})
                by_id = {e["id"]: e for e in coop_envelope.project(self.conn)}
                self.assertIsNone(by_id[root]["correlation_id"])
                self.assertEqual(by_id[resp]["correlation_id"], root)

    def test_motivating_failure_concurrent_questions_cross_wise(self):
        # two questions open at once, answered in reverse order: each
        # response must correlate to ITS OWN root, never the other.
        root_a = _insert_event(self.conn, "needs_input", {"question_id": 7})
        root_b = _insert_event(self.conn, "needs_input", {"question_id": 8})
        resp_b = _insert_event(
            self.conn, "question_answered", {"question_id": 8})
        resp_a = _insert_event(
            self.conn, "question_answered", {"question_id": 7})
        by_id = {e["id"]: e for e in coop_envelope.project(self.conn)}
        self.assertEqual(by_id[resp_a]["correlation_id"], root_a)
        self.assertEqual(by_id[resp_b]["correlation_id"], root_b)

    def test_one_to_many_responses_share_the_root(self):
        root = _insert_event(self.conn, "huddle_opened", {"huddle_id": 5})
        posts = [
            _insert_event(self.conn, "huddle_posted",
                          {"huddle_id": 5, "post_id": n})
            for n in (1, 2, 3)
        ]
        by_id = {e["id"]: e for e in coop_envelope.project(self.conn)}
        self.assertEqual({by_id[p]["correlation_id"] for p in posts}, {root})

    def test_legacy_linkless_rows_project_null_never_invented(self):
        resp = _insert_event(self.conn, "question_answered", {"other": 1})
        orphan = _insert_event(
            self.conn, "question_answered", {"question_id": 99})
        _insert_event(self.conn, "message_posted", {"message_id": 3})
        _insert_event(self.conn, "decision_recorded", {"decision_id": 4})
        by_id = {e["id"]: e for e in coop_envelope.project(self.conn)}
        self.assertIsNone(by_id[resp]["correlation_id"])
        self.assertIsNone(by_id[orphan]["correlation_id"])
        for env in by_id.values():
            if env["type"] in ("message.post", "decision.record"):
                self.assertIsNone(env["correlation_id"])

    def test_since_bounds_emission_but_not_root_discovery(self):
        root = _insert_event(self.conn, "needs_input", {"question_id": 1})
        resp = _insert_event(
            self.conn, "question_answered", {"question_id": 1})
        late = coop_envelope.project(self.conn, since=root)
        self.assertEqual([e["id"] for e in late], [resp])
        self.assertEqual(late[0]["correlation_id"], root)

    def test_duplicate_delivery_dedups_by_stable_id(self):
        _insert_event(self.conn, "needs_input", {"question_id": 1})
        first = coop_envelope.project(self.conn)
        second = coop_envelope.project(self.conn)
        self.assertEqual([e["id"] for e in first], [e["id"] for e in second])
        seen = {e["id"] for e in first}
        self.assertEqual(
            [e for e in second if e["id"] not in seen], [])

    def test_projection_performs_zero_writes(self):
        _insert_event(self.conn, "needs_input", {"question_id": 1})
        counts_before = {
            t: self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("events", "items", "claims", "questions")}
        changes_before = self.conn.total_changes
        coop_envelope.project(self.conn)
        coop_envelope.show(self.conn, 1)
        self.assertEqual(self.conn.total_changes, changes_before)
        for table, count in counts_before.items():
            self.assertEqual(
                self.conn.execute(
                    f"SELECT count(*) FROM {table}").fetchone()[0],
                count, table)

    def test_payload_is_reference_plus_passthrough(self):
        eid = _insert_event(
            self.conn, "needs_input",
            {"question_id": 9, "item_id": 1, "to": "codex", "question": "q?"})
        env = coop_envelope.show(self.conn, eid)
        self.assertEqual(env["envelope_version"], 1)
        self.assertEqual(env["payload"]["object_type"], "question")
        self.assertEqual(env["payload"]["object_id"], 9)
        self.assertEqual(env["payload"]["event_payload"]["to"], "codex")

    def test_integration_real_question_flow_correlates(self):
        # real coopdb emitters, not synthetic rows: an event_type rename in
        # the core must break this test, not silently empty the projection.
        item = coopdb.create_item(
            self.conn, actor="human", session_id=None, **contract_kwargs())
        coopdb.register_agent(self.conn, "asker")
        coopdb.register_agent(self.conn, "peer")
        asker = self.make_session("asker")
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="asker", session_id=asker,
            intent="work then ask")
        peer = self.make_session("peer")
        coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"], session_id=asker,
            to_agent="peer", question="exact question?")
        question_id = self.conn.execute(
            "SELECT question_id FROM questions").fetchone()[0]
        response_claim = coopdb.claim_question(
            self.conn, question_id=question_id, session_id=peer,
            intent="answering")
        coopdb.answer_question(
            self.conn, claim_id=response_claim["claim_id"], session_id=peer,
            answer="the answer")
        envelopes = coop_envelope.project(self.conn, item_id=item)
        requests = [e for e in envelopes if e["type"] == "question.request"]
        responses = [e for e in envelopes if e["type"] == "question.response"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(responses), 1)
        self.assertEqual(
            responses[0]["correlation_id"], requests[0]["id"])
        self.assertEqual(
            responses[0]["payload"]["object_id"],
            requests[0]["payload"]["object_id"])


class RenderLines(unittest.TestCase):
    def test_compact_line_shape(self):
        lines = coop_envelope.render_lines([{
            "envelope_version": 1, "id": 12, "type": "question.response",
            "item_id": 3, "actor": "codex", "created_at": "t",
            "payload": {}, "correlation_id": 7,
        }])
        self.assertEqual(lines, ["#12 question.response item:3 codex corr:#7"])


if __name__ == "__main__":
    unittest.main()
