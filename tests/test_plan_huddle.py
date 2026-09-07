"""Post-acceptance plan huddle: advisory multi-writer critique.

Contract finality stays one-shot; plan huddles emit plan_huddle_* events only.
Plan huddle is never forced globally — complete human contracts keep claim →
work → receipt without open_plan_huddle / open_huddle in next_action.
"""

import unittest

from agent_coop import coopdb
from agent_coop.coop_errors import InvalidTransition, SelfReview
from tests.test_claims import ClaimBoard
from tests.test_huddles import HuddleBoard, FULL_DEFINITION


class PlanHuddle(HuddleBoard):
    def _accept_contract(self):
        opened = self.open()
        hid = opened["huddle_id"]
        self.post(hid, "codex", self.owner_sid, "proposal", "bounded plan")
        self.post(hid, "claude", self.peer_sid, "support", "peer accepts")
        coopdb.close_huddle(
            self.conn, huddle_id=hid, session_id=self.peer_sid,
            actor="claude", outcome="accepted",
            summary="contract accepted for plan-huddle tests")
        self.assertEqual(
            coopdb.contract_acceptance(self.conn, self.item)["state"],
            "accepted")

    def test_contract_huddle_refused_after_acceptance(self):
        self._accept_contract()
        with self.assertRaises(InvalidTransition) as ctx:
            coopdb.open_contract_huddle(
                self.conn, claim_id=self.claim["claim_id"],
                session_id=self.owner_sid, actor="codex")
        self.assertIn("accepted", str(ctx.exception).lower())

    def test_plan_huddle_opens_posts_and_closes_without_contract_events(self):
        self._accept_contract()
        plan = coopdb.open_plan_huddle(
            self.conn, claim_id=self.claim["claim_id"],
            session_id=self.owner_sid, actor="codex",
            proposal_ref="OUT.md plan")
        self.assertEqual(plan["kind"], "implementation_plan")
        hid = plan["huddle_id"]
        self.post(hid, "codex", self.owner_sid, "proposal",
                  "Implement as one file.")
        self.post(hid, "claude", self.peer_sid, "support",
                  "Plan is sound.")
        result = coopdb.close_huddle(
            self.conn, huddle_id=hid, session_id=self.peer_sid,
            actor="claude", outcome="accepted",
            summary="plan concurred")
        self.assertEqual(result["outcome"], "accepted")
        self.assertEqual(result["kind"], "implementation_plan")
        types = [e["event_type"] for e in self.conn.execute(
            "SELECT event_type FROM events WHERE item_id=? "
            "ORDER BY event_id", (self.item,))]
        self.assertIn("plan_huddle_concurred", types)
        self.assertNotIn("contract_changes_requested", types)
        # Contract acceptance remains accepted (still one contract_accepted).
        self.assertEqual(
            coopdb.contract_acceptance(self.conn, self.item)["state"],
            "accepted")
        self.assertEqual(types.count("contract_accepted"), 1)

    def test_plan_huddle_owner_cannot_close(self):
        self._accept_contract()
        plan = coopdb.open_plan_huddle(
            self.conn, claim_id=self.claim["claim_id"],
            session_id=self.owner_sid, actor="codex")
        hid = plan["huddle_id"]
        self.post(hid, "codex", self.owner_sid, "proposal", "p")
        self.post(hid, "claude", self.peer_sid, "support", "ok")
        with self.assertRaises(SelfReview):
            coopdb.close_huddle(
                self.conn, huddle_id=hid, session_id=self.owner_sid,
                actor="codex", outcome="accepted", summary="self")

    def test_plan_huddle_refused_before_contract_acceptance(self):
        with self.assertRaises(InvalidTransition):
            coopdb.open_plan_huddle(
                self.conn, claim_id=self.claim["claim_id"],
                session_id=self.owner_sid, actor="codex")

    def test_cli_open_plan(self):
        self._accept_contract()
        code, out, err = self.cli(
            ["--json", "huddle", "open-plan", "--claim",
             str(self.claim["claim_id"]), "--proposal-ref", "v1"],
            actor="codex", session_id=self.owner_sid)
        self.assertEqual(code, 0, err)
        data = __import__("json").loads(out)
        self.assertEqual(data["kind"], "implementation_plan")
        self.assertEqual(data["proposal_ref"], "v1")


class OfflineNeedsInput(HuddleBoard):
    def test_needs_input_refuses_offline_registered_peer(self):
        """Item-25: registration alone is not liveness."""
        # Register offline agent without a running session.
        coopdb.register_or_bind_agent(
            self.conn, agent_id="grok", provider="grok")
        with self.assertRaises(InvalidTransition) as ctx:
            coopdb.needs_input(
                self.conn, claim_id=self.claim["claim_id"],
                session_id=self.owner_sid, to_agent="grok",
                question="are you online?")
        self.assertIn("no running session", str(ctx.exception))


class CompleteContractFastPath(ClaimBoard):
    """Plan huddle is opt-in; complete human contracts keep the fast path."""

    def test_complete_contract_never_forces_open_plan_or_contract_huddle(self):
        from tests.test_claims import contract_kwargs
        sid = self.make_session("codex", sid="s-codex-fast", provider="codex")
        # Peer online so a forced plan huddle *could* open if we wrongly required it.
        self.make_session("claude", sid="s-claude-fast", provider="claude")
        item = coopdb.create_item(
            self.conn, actor="human", session_id=None, **contract_kwargs(
                title="Complete at kickoff", objective="Ship one file"))
        state = coopdb.contract_acceptance(self.conn, item)
        self.assertFalse(state["required"])
        self.assertEqual(state["state"], "not_required")
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="codex", session_id=sid,
            intent="implement complete contract", lease_seconds=3600)
        action = coopdb.status(
            self.conn, "codex", session_id=sid)["next_action"]
        # Fast path: work/receipt, not ceremony.
        self.assertEqual(action["kind"], "continue_task", action)
        self.assertNotEqual(action["kind"], "open_huddle")
        cmd = " ".join(action.get("command") or [])
        self.assertNotIn("open-plan", cmd)
        self.assertNotIn("huddle open", cmd)
        self.assertIn("receipt", cmd)
        # Plan huddle remains available as opt-in CLI only.
        plan = coopdb.open_plan_huddle(
            self.conn, claim_id=claim["claim_id"], session_id=sid,
            actor="codex", proposal_ref="optional")
        self.assertEqual(plan["kind"], "implementation_plan")


if __name__ == "__main__":
    unittest.main()
