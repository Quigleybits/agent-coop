"""Adversarial trial — failure class: same-reviewer livelock.

Replays the livelock shape — the same provider re-claiming and
re-approving the same review, cycle after cycle, until the turn budget
runs out at `turn_budget_exhausted` — against the fixed protocol.

Board leg: with quorum 2 and one codex approval banked, three
consecutive attempts of the livelock loop — same-provider re-claim,
same-provider re-approve, owner re-request — are each refused typed
(`review_provider_already_approved`, duplicate-live-review,
`second_reviewer_not_selected`), the distinct-approver count never
moves, the owner's next_action collapses to idle naming
`second_reviewer_blocked`, and completion still refuses. Only a
distinct-provider approve unlocks `done`.

Runner leg: the scheduler (`run_autonomous`) and the full runner
(`coop_autonomous.main`) terminate in a TYPED stall — idle
(`no_actionable_participant`) or no-progress
(`actionable_no_board_progress`) — in a handful of turns with
`--max-turns` pinned at a generous 200, never spinning to
`turn_budget_exhausted`. These trials cover the protocol + scheduler
layer beneath any operator-side turn alarm.
"""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from agent_coop import coop_autonomous
from agent_coop import coop_runner_status as runner_status
from agent_coop import coop_start
from agent_coop import coop_turn_trace
from agent_coop import coopdb
from agent_coop.coop_errors import InvalidTransition, ReviewMissing
from tests.test_completion import CompletionBoard
from tests.test_reviews import snap


class LivelockBoard(CompletionBoard):
    """quorum-2 item mid-run with one codex approval banked and the
    owner's implementation claim still live (the Trial-2 shape)."""

    def bind_peers(self, carol_provider="grok"):
        bob_sid = self.make_session("bob", provider="codex")
        bob2_sid = self.make_session("bob2", provider="codex")
        carol_sid = self.make_session("carol", provider=carol_provider)
        return bob_sid, bob2_sid, carol_sid

    def banked_one_of_two(self, carol_provider="grok"):
        bob_sid, bob2_sid, carol_sid = self.bind_peers(carol_provider)
        item, sid, cid = self.make_working("alice", review_quorum=2)
        receipt_id = self.submit(cid, sid)
        first_review = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        claim, _packet = self.claim_rev(first_review, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=claim["claim_id"], session_id=bob_sid,
            actor="bob", verdict="approve")
        self.assertEqual(coopdb.approvals_still_needed(self.conn, item), 1)
        self.assertEqual(
            coopdb._current_approval_providers(self.conn, item), {"codex"})
        return {
            "item": item, "sid": sid, "cid": cid, "receipt_id": receipt_id,
            "bob_sid": bob_sid, "bob2_sid": bob2_sid, "carol_sid": carol_sid,
        }

    def status_of(self, agent, sid, item):
        return coopdb.status(
            self.conn, agent, session_id=sid, item_id=item)


class SameReviewerLivelockReplay(LivelockBoard):
    def test_three_consecutive_reapproval_attempts_all_refuse_typed(self):
        f = self.banked_one_of_two()
        # The second unnamed review opens (grok remains) and is the only
        # legal continuation; the attackers then hammer it three times.
        second_review = coopdb.request_review(
            self.conn, claim_id=f["cid"], session_id=f["sid"], actor="alice")
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                # Same-provider re-claim: the original approver…
                before = snap(self.conn)
                with self.assertRaises(InvalidTransition) as caught:
                    self.claim_rev(second_review, f["bob_sid"])
                self.assertEqual(before, snap(self.conn))
                self.assertEqual(
                    caught.exception.reason_code,
                    "review_provider_already_approved")
                self.assertEqual(
                    caught.exception.evidence["provider"], "codex")
                # …and a second agent from the same provider bucket.
                before = snap(self.conn)
                with self.assertRaises(InvalidTransition) as caught:
                    self.claim_rev(second_review, f["bob2_sid"])
                self.assertEqual(before, snap(self.conn))
                self.assertEqual(
                    caught.exception.reason_code,
                    "review_provider_already_approved")
                # The owner cannot churn a replacement request either.
                before = snap(self.conn)
                with self.assertRaises(InvalidTransition) as caught:
                    coopdb.request_review(
                        self.conn, claim_id=f["cid"], session_id=f["sid"],
                        actor="alice")
                self.assertEqual(before, snap(self.conn))
                self.assertIn(str(second_review), str(caught.exception))
                # The quorum arithmetic never moved.
                self.assertEqual(
                    coopdb.approvals_still_needed(self.conn, f["item"]), 1)
                self.assertEqual(
                    coopdb._current_approval_providers(
                        self.conn, f["item"]),
                    {"codex"})
        with self.assertRaises(ReviewMissing) as caught:
            self.complete(f["cid"], f["sid"])
        self.assertEqual(caught.exception.evidence["required_count"], 2)
        self.assertEqual(caught.exception.evidence["observed_count"], 1)
        # Recovery: the distinct provider approves and completion opens.
        claim, _ = self.claim_rev(second_review, f["carol_sid"])
        coopdb.submit_verdict(
            self.conn, claim_id=claim["claim_id"],
            session_id=f["carol_sid"], actor="carol", verdict="approve")
        outcome = self.complete(f["cid"], f["sid"])
        self.assertEqual(outcome["completed"], f["item"])
        self.assertEqual(
            coopdb._current_approval_providers(self.conn, f["item"]),
            {"codex", "grok"})

    def test_verdict_layer_refuses_the_same_provider_re_approve(self):
        f = self.banked_one_of_two()
        second_review = coopdb.request_review(
            self.conn, claim_id=f["cid"], session_id=f["sid"], actor="alice")
        # A claim that predates the claim-time provider guard: sneak it
        # through, then the verdict itself must still refuse.
        with mock.patch(
                "agent_coop.coopdb._current_approval_providers",
                return_value=set()):
            sneaked, _ = self.claim_rev(second_review, f["bob2_sid"])
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.submit_verdict(
                self.conn, claim_id=sneaked["claim_id"],
                session_id=f["bob2_sid"], actor="bob2", verdict="approve")
        self.assertEqual(
            caught.exception.reason_code,
            "review_provider_already_approved")
        self.assertEqual(
            coopdb.approvals_still_needed(self.conn, f["item"]), 1)
        with self.assertRaises(ReviewMissing):
            self.complete(f["cid"], f["sid"])
        # The sneaked claim is not a permanent wedge: release, distinct
        # provider takes over, completion opens.
        coopdb.release_claim(
            self.conn, claim_id=sneaked["claim_id"], actor="bob2",
            session_id=f["bob2_sid"], reason="refused verdict")
        claim, _ = self.claim_rev(
            second_review, f["carol_sid"], reason="distinct provider")
        coopdb.submit_verdict(
            self.conn, claim_id=claim["claim_id"],
            session_id=f["carol_sid"], actor="carol", verdict="approve")
        self.assertEqual(
            self.complete(f["cid"], f["sid"])["completed"], f["item"])


class FailClosedWedge(LivelockBoard):
    """No distinct provider remains (carol shares codex's bucket)."""

    def test_re_request_fails_closed_and_owner_idles_naming_the_wedge(self):
        f = self.banked_one_of_two(carol_provider="codex")
        block = coopdb.second_reviewer_blocked(self.conn, f["item"])
        self.assertTrue(block["blocked"], block)
        self.assertEqual(
            block["reason_code"], "second_reviewer_not_selected")
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                before = snap(self.conn)
                with self.assertRaises(InvalidTransition) as caught:
                    coopdb.request_review(
                        self.conn, claim_id=f["cid"], session_id=f["sid"],
                        actor="alice")
                self.assertEqual(before, snap(self.conn))
                self.assertEqual(
                    caught.exception.reason_code,
                    "second_reviewer_not_selected")
                self.assertEqual(
                    list(caught.exception.evidence["approved_providers"]),
                    ["codex"])
                self.assertEqual(
                    list(caught.exception.evidence["remaining_providers"]),
                    [])
        owner = self.status_of("alice", f["sid"], f["item"])
        self.assertEqual(owner["next_action"]["kind"], "idle")
        self.assertTrue(
            any("second_reviewer_not_selected" in w
                for w in owner["warnings"]),
            owner["warnings"])
        self.assertTrue(
            any("second_reviewer_blocked" in (i.get("labels") or [])
                for i in owner["owned_items"]
                if i["item_id"] == f["item"]),
            owner["owned_items"])
        with self.assertRaises(ReviewMissing):
            self.complete(f["cid"], f["sid"])


class SchedulerHaltsOnWedge(LivelockBoard):
    """run_autonomous against the real wedged board: typed stalls in a
    handful of turns with max_turns pinned at a generous 200."""

    LIVELOCK_MAX_TURNS = 200

    def _run_scheduler(self, f, take_turn, **overrides):
        sids = {"alice": f["sid"], "bob": f["bob_sid"],
                "carol": f["carol_sid"]}
        statuses = []
        kwargs = dict(
            actionable_fn=lambda agent: self.status_of(
                agent, sids[agent], f["item"])["next_action"],
            take_turn=take_turn,
            all_done=lambda: coop_autonomous.board_all_done(
                self.conn, item_id=f["item"]),
            max_turns=self.LIVELOCK_MAX_TURNS,
            progress_fn=lambda: coopdb.item_board_probe(
                self.conn, f["item"]),
            status_fn=lambda phase, **fields: statuses.append(
                (phase, fields)),
            sleep=lambda _seconds: None,
        )
        kwargs.update(overrides)
        reason, turns, log = coop_autonomous.run_autonomous(
            ["alice", "bob", "carol"], **kwargs)
        terminal = [fields for phase, fields in statuses
                    if phase == "terminal_detail"]
        return reason, turns, log, terminal

    def test_idle_wedge_terminates_typed_not_turn_budget_exhausted(self):
        f = self.banked_one_of_two(carol_provider="codex")
        dispatched = []
        reason, turns, _log, terminal = self._run_scheduler(
            f,
            take_turn=lambda agent, hint: dispatched.append(
                (agent, hint)) or {"ok": True},
            max_idle_rounds=2,
        )
        self.assertEqual((reason, turns), ("stalled", 0))
        self.assertEqual(dispatched, [])
        self.assertEqual(len(terminal), 1)
        self.assertEqual(
            terminal[0]["reason_code"], "no_actionable_participant")
        self.assertEqual(terminal[0]["evidence"]["turns"], 0)
        self.assertEqual(terminal[0]["evidence"]["action_kinds"], ["idle"])
        # The livelock's failure mode is structurally impossible here.
        self.assertNotEqual(reason, "max_turns")
        self.assertLess(turns, self.LIVELOCK_MAX_TURNS)

    def test_attacker_spin_stalls_within_the_noop_budget(self):
        f = self.banked_one_of_two(carol_provider="grok")
        owner = self.status_of("alice", f["sid"], f["item"])
        self.assertEqual(owner["next_action"]["kind"], "request_review")
        refusals = []

        def attacker_turn(agent, hint):
            # The owner keeps trying to hand the second slot back to the
            # provider that already approved, never to grok.
            self.assertEqual((agent, hint), ("alice", "request_review"))
            conn = coopdb.connect(self.db)
            try:
                coopdb.request_review(
                    conn, claim_id=f["cid"], session_id=f["sid"],
                    actor="alice", reviewer="bob2")
                refusals.append("unexpected_success")
            except InvalidTransition as exc:
                refusals.append(exc.reason_code)
            finally:
                conn.close()
            return {"ok": True}

        reason, turns, _log, terminal = self._run_scheduler(
            f, take_turn=attacker_turn, max_noop_cycles=3)
        self.assertEqual((reason, turns), ("stalled", 3))
        self.assertEqual(
            refusals, ["review_provider_already_approved"] * 3)
        self.assertEqual(len(terminal), 1)
        self.assertEqual(
            terminal[0]["reason_code"], "actionable_no_board_progress")
        self.assertEqual(terminal[0]["evidence"]["noop_cycles"], 3)
        self.assertEqual(
            terminal[0]["evidence"]["actionable_agents"], ["alice"])
        self.assertEqual(
            terminal[0]["evidence"]["action_kinds"], ["request_review"])
        self.assertNotEqual(reason, "max_turns")
        self.assertLess(turns, self.LIVELOCK_MAX_TURNS)
        # The board never moved: quorum still open, item never done.
        self.assertEqual(
            coopdb.approvals_still_needed(self.conn, f["item"]), 1)
        with self.assertRaises(ReviewMissing):
            self.complete(f["cid"], f["sid"])


class RunnerMainWedgeTrial(unittest.TestCase):
    """coop_autonomous.main on a seeded wedged-review board with no-op
    provider turns: the full runner halts typed within the noop budget,
    publishing the stall to the status sidecar — never 200 turns."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.board = str(root / "board.db")
        self.status_path = root / "run.status.json"
        self.trace_path = root / "run.trace.jsonl"
        conn = coopdb.connect(self.board)
        try:
            coopdb.init_db(conn)
            self.item = coopdb.create_item(
                conn, actor="human", session_id=None,
                title="wedged review", objective="O", scope="S",
                done_when="D", output_contract="OC", context="C",
                allowed_actions=["read"], stop_conditions=["stop"],
                review_quorum=2)
            # Seed the Trial-2 shape: claude owns and files evidence,
            # codex banks the only approval, quorum still needs one.
            claude_sid = "seed-claude"
            codex_sid = "seed-codex"
            coopdb.insert_session(
                conn, session_id=claude_sid, agent_id="claude",
                provider="claude", command=["claude"], cwd=self.tmp.name,
                max_runtime_s=3600, grace_s=10)
            coopdb.insert_session(
                conn, session_id=codex_sid, agent_id="codex",
                provider="codex", command=["codex"], cwd=self.tmp.name,
                max_runtime_s=3600, grace_s=10)
            coopdb.register_or_bind_agent(
                conn, agent_id="grok", provider="grok")
            cid = coopdb.claim_item(
                conn, item_id=self.item, actor="claude",
                session_id=claude_sid, intent="do the work",
                lease_seconds=3600)["claim_id"]
            evidence = root / "receipt.md"
            evidence.write_bytes(b"seeded evidence\n")
            coopdb.submit_receipt(
                conn, claim_id=cid, session_id=claude_sid, actor="claude",
                path=str(evidence), summary="did the work",
                proof="see file")
            review_id = coopdb.request_review(
                conn, claim_id=cid, session_id=claude_sid, actor="claude")
            rclaim, _ = coopdb.claim_review(
                conn, review_id=review_id, session_id=codex_sid,
                intent="first review")
            coopdb.submit_verdict(
                conn, claim_id=rclaim["claim_id"], session_id=codex_sid,
                actor="codex", verdict="approve")
            self.assertEqual(
                coopdb.approvals_still_needed(conn, self.item), 1)
            # The seed harnesses exit; the runner will bind fresh ones.
            coopdb.finish_session(
                conn, claude_sid, status="exited", reason="child_exit",
                exit_code=0)
            coopdb.finish_session(
                conn, codex_sid, status="exited", reason="child_exit",
                exit_code=0)
        finally:
            conn.close()

    def test_main_stalls_typed_within_noop_budget_not_200_turns(self):
        calls = []

        def noop_invoke(**kwargs):
            calls.append(kwargs["agent_id"])
            return {
                "agent": kwargs["agent_id"],
                "provider": kwargs["provider"],
                "ok": True,
                "exit": 0,
                "tree_empty": True,
                "note": "",
            }

        trace = mock.Mock()
        trace.emit.side_effect = lambda event, **fields: True
        with mock.patch.dict(
                os.environ,
                {
                    "COOP_RUN_STATUS_PATH": str(self.status_path),
                    "COOP_RUN_TRACE_PATH": str(self.trace_path),
                }), \
             mock.patch.object(
                 coop_turn_trace, "TurnTrace", return_value=trace), \
             mock.patch.object(
                 coop_start, "resolve_participants",
                 return_value={
                     "available": ["claude", "codex", "grok"],
                     "skipped": [],
                 }), \
             mock.patch.object(
                 coop_start, "resolved_provider_argv",
                 side_effect=lambda name: [name]), \
             mock.patch.object(
                 coop_start, "invoke_turn", side_effect=noop_invoke), \
             mock.patch.object(
                 coop_autonomous.pathlib.Path, "exists",
                 return_value=False):
            code = coop_autonomous.main([
                "--db", self.board,
                "--item", str(self.item),
                "--max-turns", "200",
                "--max-noop-cycles", "3",
                "--interval", "0.05",
                "--no-persistent-workers",
                "--no-mechanical-precommit",
                "--no-prompt-hydration",
            ])

        self.assertEqual(code, 3)
        status = runner_status.read_status(self.status_path)
        self.assertEqual(status["phase"], "stalled")
        self.assertEqual(status["reason"], "stalled")
        self.assertEqual(
            status["reason_code"], "actionable_no_board_progress")
        self.assertGreaterEqual(status["evidence"]["noop_cycles"], 3)
        self.assertEqual(
            status["evidence"]["actionable_agents"], ["claude"])
        # An unfixed livelock burns all 200 turns; the fixed runner stops
        # typed within the noop budget.
        self.assertLessEqual(status["turns"], 6)
        self.assertLess(status["turns"], 200)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(set(calls), {"claude"})

        conn = coopdb.connect(self.board, require_current=True)
        try:
            row = conn.execute(
                "SELECT status FROM items WHERE id=?",
                (self.item,)).fetchone()
            self.assertEqual(row["status"], "review")
            self.assertEqual(
                coopdb.approvals_still_needed(conn, self.item), 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE "
                "event_type='item_completed'").fetchone()["n"], 0)
            finished = conn.execute(
                "SELECT payload_json FROM events WHERE "
                "event_type='autonomous_run_finished' "
                "ORDER BY event_id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        payload = json.loads(finished["payload_json"])
        self.assertEqual(payload["reason"], "stalled")
        self.assertEqual(
            payload["reason_code"], "actionable_no_board_progress")


if __name__ == "__main__":
    unittest.main()
