"""Goal-tasks: draft claim-eligibility + the agent-lane `define_item`.

A draft = a task with a goal (title + objective) but an otherwise-incomplete
contract. It is claimable so the working agent completes the contract via
`define_item`, which fills EMPTY fields only and never overwrites a field the
human set. A goalless-incomplete legacy row stays unclaimable.
"""
import os
import unittest

from agent_coop import coopdb
from agent_coop import coop_errors
from agent_coop import coop_start
from tests.test_claims import ClaimBoard, snapshot


def goal_item(conn, title="Explain the thing", objective="Explain X clearly"):
    return coopdb.create_item(conn, actor="human", session_id=None,
                              title=title, objective=objective)


def goalless_item(conn):
    coopdb.register_agent(conn, "human")

    def _seed(c):
        return c.execute(
            "INSERT INTO items(title,objective,status,created_by,"
            "created_at,updated_at) VALUES ('legacy','','todo','human',?,?)",
            (coopdb.now(), coopdb.now())).lastrowid
    return coopdb.mutate(conn, _seed)


class DraftEligibility(ClaimBoard):
    def test_goal_only_create_is_a_draft_and_labeled(self):
        iid = goal_item(self.conn)
        row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        self.assertTrue(coopdb.is_draft(row))
        self.assertTrue(coopdb.contract_incomplete(row))
        self.assertIn("draft", coopdb.task_rows(self.conn)[0]["labels"])

    def test_goalless_incomplete_is_not_a_draft(self):
        iid = goalless_item(self.conn)
        row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        self.assertFalse(coopdb.is_draft(row))
        self.assertTrue(coopdb.contract_incomplete(row))
        labels = coopdb.task_rows(self.conn)[0]["labels"]
        self.assertIn("contract_incomplete", labels)
        self.assertNotIn("draft", labels)

    def test_draft_claimable_goalless_refused(self):
        draft = goal_item(self.conn)
        goalless = goalless_item(self.conn)
        sid = self.make_session("codex", provider="codex")
        claim = coopdb.claim_item(self.conn, item_id=draft, actor="codex",
                                  session_id=sid, intent="define + do")
        self.assertTrue(claim["claim_id"])
        with self.assertRaises(coop_errors.IncompleteContract):
            coopdb.claim_item(self.conn, item_id=goalless, actor="codex",
                              session_id=sid, intent="nope")


class DefineItem(ClaimBoard):
    def _claimed_draft(self, agent="codex"):
        draft = goal_item(self.conn)
        sid = self.make_session(agent, provider=agent)
        claim = coopdb.claim_item(self.conn, item_id=draft, actor=agent,
                                  session_id=sid, intent="define + do")
        return draft, sid, claim["claim_id"]

    def test_define_fills_empty_fields_and_completes_contract(self):
        draft, sid, claim_id = self._claimed_draft()
        result = coopdb.define_item(
            self.conn, claim_id=claim_id, session_id=sid, actor="codex",
            fields={"scope": "the parser", "done_when": "tests green",
                    "output_contract": "out.md", "context": "demo",
                    "allowed_actions": ["edit"], "stop_conditions": ["stop"]})
        self.assertTrue(result["contract_complete"])
        row = self.conn.execute(
            "SELECT * FROM items WHERE id=?", (draft,)).fetchone()
        self.assertFalse(coopdb.contract_incomplete(row))
        self.assertEqual(row["scope"], "the parser")
        events = [r["event_type"] for r in self.conn.execute(
            "SELECT event_type FROM events WHERE item_id=?", (draft,))]
        self.assertIn("item_defined", events)

    def test_define_refuses_to_overwrite_a_set_field(self):
        # Create a task whose scope the human already set; the agent may not
        # overwrite it (field_already_set), and nothing changes (rollback).
        item = coopdb.create_item(
            self.conn, actor="human", session_id=None, title="t",
            objective="o", scope="HUMAN SET THIS")
        sid = self.make_session("codex", provider="codex")
        claim = coopdb.claim_item(self.conn, item_id=item, actor="codex",
                                  session_id=sid, intent="x")
        before = snapshot(self.conn)
        with self.assertRaises(coop_errors.FieldAlreadySet):
            coopdb.define_item(
                self.conn, claim_id=claim["claim_id"], session_id=sid,
                actor="codex",
                fields={"done_when": "ok", "scope": "agent overwrite"})
        self.assertEqual(snapshot(self.conn), before)  # full rollback

    def test_define_requires_the_claim_holder(self):
        draft, sid, claim_id = self._claimed_draft("codex")
        other = self.make_session("claude", provider="claude")
        with self.assertRaises(coop_errors.CoopError):
            coopdb.define_item(
                self.conn, claim_id=claim_id, session_id=other,
                actor="claude", fields={"scope": "sneaky"})

    def test_define_rejects_title_objective_and_unknown_fields(self):
        draft, sid, claim_id = self._claimed_draft()
        for bad in ({"title": "x"}, {"objective": "x"}, {"bogus": "x"}):
            with self.subTest(fields=bad):
                with self.assertRaises(coop_errors.IncompleteContract):
                    coopdb.define_item(
                        self.conn, claim_id=claim_id, session_id=sid,
                        actor="codex", fields=bad)


class FullGoalTaskCycle(ClaimBoard):
    def test_claim_draft_define_receipt_review_complete(self):
        draft = goal_item(self.conn, title="Write EXPLAINER",
                          objective="Explain coop in 5 bullets")
        codex = self.make_session("codex", provider="codex")
        claude = self.make_session("claude", provider="claude")
        claim = coopdb.claim_item(self.conn, item_id=draft, actor="codex",
                                  session_id=codex, intent="do it")
        cid = claim["claim_id"]
        coopdb.define_item(
            self.conn, claim_id=cid, session_id=codex, actor="codex",
            fields={"scope": "one md file", "done_when": "5 bullets + review",
                    "output_contract": "EXPLAINER.md", "context": "demo",
                    "allowed_actions": ["write"], "stop_conditions": ["scope"]})
        huddle = coopdb.open_contract_huddle(
            self.conn, claim_id=cid, session_id=codex, actor="codex")
        coopdb.post_huddle(
            self.conn, huddle_id=huddle["huddle_id"], session_id=codex,
            actor="codex", stance="proposal", body="Use the five-bullet contract")
        coopdb.post_huddle(
            self.conn, huddle_id=huddle["huddle_id"], session_id=claude,
            actor="claude", stance="support", body="Bounded and testable")
        coopdb.close_huddle(
            self.conn, huddle_id=huddle["huddle_id"], session_id=claude,
            actor="claude", outcome="accepted",
            summary="Peer accepts the contract before execution")
        evidence = os.path.join(self.tmp.name, "EXPLAINER.md")
        with open(evidence, "w", encoding="utf-8") as fh:
            fh.write("- one\n- two\n- three\n- four\n- five\n")
        coopdb.submit_receipt(
            self.conn, claim_id=cid, session_id=codex, actor="codex",
            path=evidence, summary="5 bullets written",
            proof="file on disk", proof_refs=[f"file:{evidence}"])
        review = coopdb.request_review(
            self.conn, claim_id=cid, session_id=codex, actor="codex",
            reviewer="claude")
        rclaim, _ = coopdb.claim_review(
            self.conn, review_id=review, session_id=claude,
            intent="review the bullets")
        coopdb.submit_verdict(
            self.conn, claim_id=rclaim["claim_id"], session_id=claude,
            actor="claude", verdict="approve")
        result = coopdb.complete_item(
            self.conn, claim_id=cid, session_id=codex, actor="codex")
        self.assertEqual(result.get("completed"), draft)
        row = self.conn.execute(
            "SELECT status FROM items WHERE id=?", (draft,)).fetchone()
        self.assertEqual(row["status"], "done")


class PromptFidelity(unittest.TestCase):
    def test_prompt_names_define_and_cross_provider_review(self):
        text = coop_start.compose_prompt(
            agent="codex", provider="codex", board_path="/r/coop/board.db",
            guide_path="/r/coop/COOP_GUIDE.md",
            tasks=[{"item_id": 4, "status": "todo", "title": "Draft"}],
            round_no=1, propose_mode=False, task=4)
        self.assertIn("item define", text)
        self.assertIn("DRAFT", text)
        self.assertIn("DIFFERENT provider", text)
        self.assertIn("review claim", text)   # take an open review first


if __name__ == "__main__":
    unittest.main()
