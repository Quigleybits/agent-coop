"""Bounded first-class huddles and peer acceptance of agent-authored contracts."""

import contextlib
import io
import json
import os
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop.coop_errors import InvalidTransition, SelfReview
from tests.test_claims import ClaimBoard


FULL_DEFINITION = {
    "scope": "one markdown file",
    "done_when": "the file exists and matches the goal",
    "output_contract": "OUT.md",
    "context": "goal-task contract",
    "allowed_actions": ["write the output"],
    "stop_conditions": ["stop if the goal is ambiguous"],
}


class HuddleBoard(ClaimBoard):
    def setUp(self):
        super().setUp()
        self.owner_sid = self.make_session("codex", sid="s-codex",
                                           provider="codex")
        self.peer_sid = self.make_session("claude", sid="s-claude",
                                          provider="claude")
        self.item = coopdb.create_item(
            self.conn, actor="human", session_id=None,
            title="Write OUT", objective="Explain the result clearly")
        self.claim = coopdb.claim_item(
            self.conn, item_id=self.item, actor="codex",
            session_id=self.owner_sid, intent="define and implement",
            lease_seconds=3600)
        coopdb.define_item(
            self.conn, claim_id=self.claim["claim_id"],
            session_id=self.owner_sid, actor="codex",
            fields=FULL_DEFINITION)

    def evidence(self):
        path = os.path.join(self.tmp.name, "OUT.md")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("result\n")
        return path

    def open(self):
        return coopdb.open_contract_huddle(
            self.conn, claim_id=self.claim["claim_id"],
            session_id=self.owner_sid, actor="codex")

    def post(self, huddle_id, actor, sid, stance, body):
        return coopdb.post_huddle(
            self.conn, huddle_id=huddle_id, session_id=sid, actor=actor,
            stance=stance, body=body)

    def cli(self, argv, *, actor="", session_id=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"COOP_DB": self.db, "COOP_AGENT": actor,
               "COOP_SESSION_ID": session_id}
        code = 0
        with unittest.mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            try:
                coopcli.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()


class ContractAcceptance(HuddleBoard):
    def test_agent_authored_contract_cannot_execute_before_peer_acceptance(self):
        state = coopdb.contract_acceptance(self.conn, self.item)
        self.assertEqual(state["state"], "pending")
        action = coopdb.status(
            self.conn, "codex", session_id=self.owner_sid)["next_action"]
        self.assertEqual(action["kind"], "open_huddle")
        self.assertEqual(action["command"], [
            *coopdb.CLI_ARGV, "huddle", "open",
            "--claim", str(self.claim["claim_id"]),
        ])
        path = self.evidence()
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.submit_receipt(
                self.conn, claim_id=self.claim["claim_id"],
                session_id=self.owner_sid, actor="codex", path=path,
                summary="done", proof="file", proof_refs=[f"file:{path}"])
        self.assertEqual(
            caught.exception.reason_code, "contract_acceptance_required")
        self.assertEqual(caught.exception.evidence["item_id"], self.item)
        self.assertEqual(caught.exception.evidence["current_state"], "pending")

    def test_one_round_peer_support_accepts_and_unlocks_execution(self):
        opened = self.open()
        hid = opened["huddle_id"]
        self.post(hid, "codex", self.owner_sid, "proposal",
                  "Use the narrow one-file contract as written.")
        with self.assertRaises(InvalidTransition) as caught:
            self.post(hid, "codex", self.owner_sid, "revision",
                      "A second post cannot skip the peer's turn.")
        self.assertEqual(caught.exception.reason_code, "awaiting_peer")
        self.assertEqual(caught.exception.evidence["item_id"], self.item)
        self.assertEqual(caught.exception.evidence["huddle_id"], hid)
        self.assertEqual(caught.exception.evidence["target_agent_id"], "claude")
        self.post(hid, "claude", self.peer_sid, "support",
                  "Scope and done condition are bounded and testable.")
        result = coopdb.close_huddle(
            self.conn, huddle_id=hid, session_id=self.peer_sid,
            actor="claude", outcome="accepted",
            summary="Peer accepts the executable contract.")
        self.assertEqual(result["outcome"], "accepted")
        self.assertEqual(
            coopdb.contract_acceptance(self.conn, self.item)["state"],
            "accepted")
        path = self.evidence()
        receipt = coopdb.submit_receipt(
            self.conn, claim_id=self.claim["claim_id"],
            session_id=self.owner_sid, actor="codex", path=path,
            summary="done", proof="file", proof_refs=[f"file:{path}"])
        self.assertTrue(receipt)

    def test_contract_author_cannot_accept_own_huddle(self):
        hid = self.open()["huddle_id"]
        self.post(hid, "codex", self.owner_sid, "proposal", "Bounded plan")
        self.post(hid, "claude", self.peer_sid, "support", "Looks sound")
        with self.assertRaises(SelfReview) as caught:
            coopdb.close_huddle(
                self.conn, huddle_id=hid, session_id=self.owner_sid,
                actor="codex", outcome="accepted", summary="self accept")
        self.assertEqual(caught.exception.reason_code, "reviewer_is_owner")
        self.assertEqual(caught.exception.evidence["item_id"], self.item)
        self.assertEqual(caught.exception.evidence["huddle_id"], hid)
        self.assertEqual(caught.exception.evidence["owner_agent_id"], "codex")

    def test_peer_receives_structured_huddle_post_action(self):
        hid = self.open()["huddle_id"]
        action = coopdb.status(
            self.conn, "claude", session_id=self.peer_sid)["next_action"]
        self.assertEqual(action["kind"], "huddle_post")
        self.assertEqual(action["target_type"], "huddle")
        self.assertEqual(action["target_id"], hid)
        self.assertEqual(action["required_inputs"], ["stance", "body"])
        self.assertEqual(action["command"], [
            *coopdb.CLI_ARGV, "huddle", "post", str(hid),
            "--stance", "{stance}", "--body", "{body}",
        ])

    def test_cli_open_post_close_and_show(self):
        code, out, err = self.cli(
            ["--json", "huddle", "open", "--claim",
             str(self.claim["claim_id"])],
            actor="codex", session_id=self.owner_sid)
        self.assertEqual(code, 0, err)
        hid = json.loads(out)["huddle_id"]
        for actor, sid, stance, body in (
                ("codex", self.owner_sid, "proposal", "bounded plan"),
                ("claude", self.peer_sid, "support", "peer accepts")):
            code, _out, err = self.cli(
                ["huddle", "post", str(hid), "--stance", stance,
                 "--body", body], actor=actor, session_id=sid)
            self.assertEqual(code, 0, err)
        code, _out, err = self.cli(
            ["huddle", "close", str(hid), "--outcome", "accepted",
             "--summary", "ready"], actor="claude",
            session_id=self.peer_sid)
        self.assertEqual(code, 0, err)
        code, out, err = self.cli(
            ["--json", "huddle", "show", str(hid)])
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown["status"], "accepted")
        self.assertEqual(len(shown["posts"]), 2)


class ContractRefinement(HuddleBoard):
    def test_concern_closes_for_changes_and_owner_refines_authored_fields(self):
        hid = self.open()["huddle_id"]
        self.post(hid, "codex", self.owner_sid, "proposal", "Original scope")
        self.post(hid, "claude", self.peer_sid, "concern",
                  "The scope does not name the audience.")
        coopdb.close_huddle(
            self.conn, huddle_id=hid, session_id=self.peer_sid,
            actor="claude", outcome="changes",
            summary="Name the audience before execution.")
        action = coopdb.status(
            self.conn, "codex", session_id=self.owner_sid)["next_action"]
        self.assertEqual(action["kind"], "refine_contract")
        self.assertEqual(action["required_inputs"], ["contract_path"])
        self.assertEqual(action["command"], [
            *coopdb.CLI_ARGV, "item", "refine",
            "--claim", str(self.claim["claim_id"]),
            "--contract", "{contract_path}",
        ])
        patch_path = os.path.join(self.tmp.name, "contract-patch.json")
        with open(patch_path, "w", encoding="utf-8") as stream:
            json.dump({"scope": "one markdown file for a new user"}, stream)
        code, out, err = self.cli(
            ["--json", "item", "refine", "--claim",
             str(self.claim["claim_id"]), "--contract", patch_path],
            actor="codex", session_id=self.owner_sid)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["contract_version"], 2)
        self.assertEqual(
            coopdb.contract_acceptance(self.conn, self.item)["state"],
            "pending")
        packet = coopdb.item_show(self.conn, self.item, packet=True)
        self.assertEqual(packet["scope"], "one markdown file for a new user")
        self.assertEqual(packet["contract_acceptance"]["state"], "pending")
