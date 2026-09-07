"""Boot-panel status with a derived next action.

The status skill is a compact deterministic query, not an LLM: identity,
claims, owned work, unread counts, grace state, derived labels — and one
`next_action` hint ordered blocking-first. Unsafe states are warnings,
never recommendations. Plus the CLI parity walk and the
central exit-code contract.
"""

import io
import contextlib
import json
import unittest
import unittest.mock
from pathlib import Path

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop import projection
from tests.test_claims import ClaimBoard, LEASE, contract_kwargs

GOLDEN_KEYS = {
    "agent", "session", "claims", "owned_items", "unread",
    "resume_grace", "stale", "warnings", "next_action",
}


def _scan_for_token(value):
    if isinstance(value, dict):
        assert "fencing_token" not in value, value
        for v in value.values():
            _scan_for_token(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _scan_for_token(v)


def next_kind(status):
    return status["next_action"]["kind"]


class StatusBoard(ClaimBoard):
    def status(self, agent, sid=None):
        return coopdb.status(self.conn, agent, session_id=sid)

    def open_question_for(self, target="bob"):
        item = self.make_item(title=f"q-for-{target}")
        self.make_session("asker", sid="s-asker")
        claim = self.claim(item, "asker", "s-asker")
        return coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"], session_id="s-asker",
            to_agent=target, question="blocking?")

    def answered_grace_for(self, owner="bob", owner_sid="s-bob"):
        """Item owned by `owner`, question answered, grace running."""
        item = self.make_item(title=f"grace-{owner}")
        claim = self.claim(item, owner, owner_sid)
        qid = coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"], session_id=owner_sid,
            to_agent="helper", question="grace?")
        response = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-helper",
            intent="answering")
        coopdb.answer_question(
            self.conn, claim_id=response["claim_id"],
            session_id="s-helper", answer="here you go")
        return item

    def recoverable_stale_for(self, agent="bob"):
        """A stale newest-of-lane claim whose session is terminal."""
        item = self.make_item(title=f"stale-{agent}")
        sid = self.make_session(agent, sid=f"s-stale-{agent}")
        self.claim(item, agent, sid)
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        self.clock.advance(-(LEASE + 1))
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit",
            exit_code=0)
        return item

    def receipt_for(self, claim, sid, actor="alice"):
        path = Path(self.tmp.name) / f"r-{claim['claim_id']}.txt"
        path.write_text("evidence", encoding="utf-8")
        return coopdb.submit_receipt(
            self.conn, claim_id=claim["claim_id"], session_id=sid,
            actor=actor, path=str(path), summary="did it",
            proof="see the file", proof_refs=[])

    def live_review_for(self, reviewer=None, owner="alice",
                        owner_sid="s-alice", title="rev-item",
                        review_quorum=None):
        """Item owned+claimed by `owner` (long lease, so review-lane clock
        games never kill the owner's claim), receipt current, review
        live."""
        item_overrides = {"title": title, "owner": owner}
        if review_quorum is not None:
            item_overrides["review_quorum"] = review_quorum
        item = self.make_item(**item_overrides)
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor=owner, session_id=owner_sid,
            intent="work", lease_seconds=3600)
        self.receipt_for(claim, owner_sid, owner)
        review_id = coopdb.request_review(
            self.conn, claim_id=claim["claim_id"], session_id=owner_sid,
            actor=owner, reviewer=reviewer)
        return item, review_id, claim

    def pending_handoff_for(self, to="bob", owner="alice",
                            owner_sid="s-alice", title="hand-item"):
        item = self.make_item(title=title, owner=owner)
        claim = self.claim(item, owner, owner_sid)
        ev = self.conn.execute(
            "SELECT event_id FROM events WHERE item_id=? ORDER BY event_id "
            "LIMIT 1", (item,)).fetchone()["event_id"]
        result = coopdb.create_handoff(
            self.conn, claim_id=claim["claim_id"], session_id=owner_sid,
            actor=owner, to_agent=to, reason="rotating off",
            summary="half done", completed="the parser",
            remaining="the renderer", risks="none known",
            next_action="wire the renderer", proof_refs=[f"event:{ev}"])
        return item, result["handoff_id"]


class NextActionPrecedence(StatusBoard):
    def setUp(self):
        super().setUp()
        self.make_session("helper", sid="s-helper")

    def test_answer_question_beats_resume_task(self):
        self.make_session("bob", sid="s-bob")
        self.answered_grace_for("bob", "s-bob")
        self.open_question_for("bob")
        self.assertEqual(next_kind(self.status("bob")),
                         "answer_question")

    def test_resume_task_beats_continue_task(self):
        self.make_session("bob", sid="s-bob")
        active = self.make_item(title="live-work")
        self.claim(active, "bob", "s-bob")
        self.answered_grace_for("bob", "s-bob")
        self.assertEqual(next_kind(self.status("bob")), "resume_task")

    def test_continue_task_beats_recover_claim(self):
        self.recoverable_stale_for("bob")
        sid = self.make_session("bob", sid="s-bob-2")
        active = self.make_item(title="live-work")
        self.claim(active, "bob", sid)
        self.assertEqual(next_kind(self.status("bob")), "continue_task")

    def test_recover_claim_beats_claim_task(self):
        self.recoverable_stale_for("bob")
        self.make_item(title="queued-work")  # claimable queue non-empty
        self.assertEqual(next_kind(self.status("bob")), "recover_claim")

    def test_claim_task_beats_idle_and_idle_is_the_floor(self):
        coopdb.register_agent(self.conn, "bob")
        self.make_item(title="queued-work")
        self.assertEqual(next_kind(self.status("bob")), "claim_task")
        empty_status = self.status("helper")
        # helper sees the same queue — consume it to prove the idle floor
        conn = self.conn
        conn.execute("UPDATE items SET status='done'")
        conn.commit()
        self.assertEqual(next_kind(self.status("bob")), "idle")

    def test_progress_stale_is_a_warning_with_continue_recommended(self):
        item = self.make_item()
        sid = self.make_session("bob")
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="bob", session_id=sid,
            intent="quiet work", lease_seconds=3600)
        self.clock.advance(901)  # past the checkpoint limit, lease alive
        status = self.status("bob")
        self.assertEqual(next_kind(status), "continue_task")
        self.assertTrue(any("progress_stale" in w for w in
                            status["warnings"]))
        claims = status["claims"]
        self.assertTrue(claims[0]["progress_stale"])

    def test_unsafely_stale_claim_warns_and_never_recommends_recovery(self):
        item = self.make_item()
        sid = self.make_session("bob")
        self.claim(item, "bob", sid)
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)  # stale while the session runs on
        status = self.status("bob")
        self.assertNotEqual(next_kind(status), "recover_claim")
        self.assertTrue(any("not yet reclaimable" in w for w in
                            status["warnings"]))


class StatusAssembly(StatusBoard):
    def test_next_action_is_an_executable_protocol_object(self):
        coopdb.register_agent(self.conn, "bob")
        item = self.make_item(title="queued")
        action = self.status("bob")["next_action"]
        self.assertEqual(action, {
            "kind": "claim_task",
            "target_type": "item",
            "target_id": item,
            "item_id": item,
            "claim_id": None,
            "lease_seconds": 3600,
            "command": [
                *coopdb.CLI_ARGV, "item", "claim", str(item),
                "--intent", f"claim item {item}",
                "--lease-seconds", "3600",
            ],
            "required_inputs": [],
            "choices": [],
        })

    def test_claimable_review_action_names_target_and_command(self):
        self.make_session("alice", sid="s-alice")
        self.make_session("bob", sid="s-bob")
        item, review_id, _claim = self.live_review_for()
        action = self.status("bob", "s-bob")["next_action"]
        self.assertEqual(action["kind"], "review_task")
        self.assertEqual(action["target_type"], "review")
        self.assertEqual(action["target_id"], review_id)
        self.assertEqual(action["item_id"], item)
        self.assertEqual(action["lease_seconds"], 3600)
        self.assertEqual(action["command"], [
            *coopdb.CLI_ARGV, "review", "claim", str(review_id),
            "--intent", f"review item {item}",
            "--lease-seconds", "3600",
        ])
        self.assertEqual(action["required_inputs"], [])

    def test_owner_with_receipt_gets_exact_review_request(self):
        self.make_session("alice", sid="s-alice")
        coopdb.register_or_bind_agent(
            self.conn, agent_id="bob", provider="codex")
        item = self.make_item(owner="alice")
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="alice", session_id="s-alice",
            intent="work", lease_seconds=3600)
        self.receipt_for(claim, "s-alice", "alice")
        action = self.status("alice", "s-alice")["next_action"]
        self.assertEqual(action["kind"], "request_review")
        self.assertEqual(action["command"], [
            *coopdb.CLI_ARGV, "review", "request",
            "--claim", str(claim["claim_id"]),
        ])
        self.assertEqual(action["required_inputs"], [])

    def test_owner_with_approval_gets_exact_completion(self):
        self.make_session("alice", sid="s-alice")
        self.make_session("bob", sid="s-bob", provider="codex")
        item, review_id, claim = self.live_review_for(reviewer="bob")
        review_claim, _packet = coopdb.claim_review(
            self.conn, review_id=review_id, session_id="s-bob",
            intent="review")
        coopdb.submit_verdict(
            self.conn, claim_id=review_claim["claim_id"],
            session_id="s-bob", actor="bob", verdict="approve")
        action = self.status("alice", "s-alice")["next_action"]
        self.assertEqual(action["kind"], "complete_task")
        self.assertEqual(action["item_id"], item)
        self.assertEqual(action["command"], [
            *coopdb.CLI_ARGV, "item", "complete",
            "--claim", str(claim["claim_id"]),
        ])

    def test_recovery_action_carries_safe_reclaim_flags_and_lease(self):
        item = self.recoverable_stale_for("bob")
        action = self.status("bob")["next_action"]
        self.assertEqual(action["kind"], "recover_claim")
        self.assertEqual(action["item_id"], item)
        self.assertIn("--reclaim", action["command"])
        self.assertIn("--reason", action["command"])
        self.assertEqual(action["command"][-2:],
                         ["--lease-seconds", "3600"])

    def test_golden_json_key_set_and_tokenless(self):
        self.make_session("helper", sid="s-helper")
        self.make_session("bob", sid="s-bob")
        active = self.make_item(title="live")
        self.claim(active, "bob", "s-bob")
        self.answered_grace_for("bob", "s-bob")
        status = self.status("bob", sid="s-bob")
        self.assertEqual(set(status), GOLDEN_KEYS)
        _scan_for_token(status)
        json.dumps(status)  # stable/serializable

    def test_session_identity_and_liveness(self):
        sid = self.make_session("bob", provider="codex")
        status = self.status("bob", sid=sid)
        self.assertEqual(status["agent"]["agent_id"], "bob")
        self.assertEqual(status["agent"]["provider"], "codex")
        self.assertEqual(status["session"]["session_id"], sid)
        self.assertEqual(status["session"]["status"], "running")

    def test_owned_items_carry_derived_labels(self):
        self.make_session("helper", sid="s-helper")
        self.make_session("bob", sid="s-bob")
        item = self.answered_grace_for("bob", "s-bob")
        self.clock.advance(coopdb.DEFAULT_RESUME_GRACE_SECONDS)  # expire it
        status = self.status("bob")
        entry = next(i for i in status["owned_items"]
                     if i["item_id"] == item)
        self.assertIn("resume_stale", entry["labels"])
        # incomplete legacy row: readable, labeled, never claimable
        self.conn.execute(
            "INSERT INTO items(title,status,contract_version,created_by,"
            "created_at,updated_at,owner_agent_id) VALUES "
            "('legacy','todo',1,'human',?,?,'bob')",
            (coopdb.now(), coopdb.now()))
        self.conn.commit()
        status = self.status("bob")
        legacy = next(i for i in status["owned_items"]
                      if i["title"] == "legacy")
        self.assertIn("contract_incomplete", legacy["labels"])

    def test_unread_counts_by_category_via_peek(self):
        self.make_session("alice", sid="s-alice")
        coopdb.register_agent(self.conn, "bob")
        coopdb.say(self.conn, session_id="s-alice", body="one",
                   to_agent="bob")
        coopdb.say(self.conn, session_id="s-alice", body="two",
                   to_agent="bob")
        status = self.status("bob")
        self.assertEqual(status["unread"], {"message": 2})
        offset = self.conn.execute(
            "SELECT * FROM inbox_offsets WHERE agent_id='bob'").fetchone()
        self.assertIsNone(offset)  # status never consumed the cursor

    def test_empty_session_id_normalizes_to_human_view(self):
        coopdb.register_agent(self.conn, "bob")
        status = coopdb.status(self.conn, "bob", session_id="")
        self.assertIsNone(status["session"])


class CliContract(StatusBoard):
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

    # Every documented command line, with placeholder values.
    PARITY = [
        ["init"],
        ["migrate", "--backup", "b.db", "--confirm-legacy-clients-stopped"],
        ["session", "run", "--as", "codex", "--name", "x", "--", "cmd"],
        ["status", "--compact"],
        ["status", "--json"],
        ["queue", "--for", "bob", "--json"],
        ["inbox", "--peek", "--json"],
        ["say", "hello", "--to", "bob", "--item", "1", "--room", "#general"],
        ["item", "create", "--title", "t", "--objective", "o",
         "--scope", "s", "--done-when", "d", "--output-contract", "oc",
         "--context", "c", "--allowed-action", "a", "--stop-condition", "s",
         "--contract", "c.json", "--owner", "bob", "--next-actor", "bob"],
        ["item", "show", "1", "--packet", "--json"],
        ["item", "show", "1", "--history"],
        ["item", "claim", "1", "--intent", "i", "--reclaim", "--reason", "r"],
        ["claim", "release", "--claim", "1", "--reason", "r"],
        ["checkpoint", "step", "--claim", "1", "--note", "n"],
        ["needs-input", "--claim", "1", "--to", "bob", "--question", "q"],
        ["question", "claim", "1", "--intent", "i"],
        ["question", "answer", "--claim", "1", "--answer", "a"],
        ["admin", "answer", "1", "--answer", "a", "--reason", "r"],
        ["admin", "release", "1", "--reason", "r",
         "--confirm-process-stopped"],
    ]

    def test_design_13_surface_parses_end_to_end(self):
        parser = coopcli.build_parser()
        for argv in self.PARITY:
            with self.subTest(argv=" ".join(argv)):
                parser.parse_args(argv)  # argparse SystemExit = failure

    def test_exit_code_contract_is_central(self):
        code, out, err = self._run(["status"], {"COOP_AGENT": "human"})
        self.assertEqual(code, 0)  # success
        sid = self.make_session("bob")
        code, out, err = self._run(
            ["item", "claim", "999", "--intent", "x"],
            {"COOP_SESSION_ID": sid, "COOP_AGENT": "bob"})
        self.assertEqual(code, 1)  # typed domain rejection
        self.assertIn("not_found", err)
        code, out, err = self._run(["no-such-command"])
        self.assertEqual(code, 2)  # usage

    def test_cli_status_json_is_the_core_dict(self):
        coopdb.register_agent(self.conn, "bob")
        self.make_item(title="queued")
        code, out, err = self._run(
            ["--json", "status"], {"COOP_AGENT": "bob"})
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(set(data), GOLDEN_KEYS)
        self.assertEqual(next_kind(data), "claim_task")
        self.assertNotIn("fencing_token", out)

    def test_cli_compact_form_names_the_next_action(self):
        coopdb.register_agent(self.conn, "bob")
        code, out, err = self._run(
            ["status", "--compact"], {"COOP_AGENT": "bob"})
        self.assertEqual(code, 0, err)
        self.assertIn("next: idle", out)


class LaneAwareHints(StatusBoard):
    """The widened blocking-first ladder — respond_handoff and
    review_task join it; continue/recover generalize across claim kinds;
    resume keys on grace regardless of source; unsafe lanes warn, never
    recommend."""

    def setUp(self):
        super().setUp()
        self.make_session("alice", sid="s-alice")
        self.make_session("bob", sid="s-bob")
        coopdb.register_or_bind_agent(
            self.conn, agent_id="carol", provider="grok")

    def bob(self):
        return self.status("bob")

    def test_respond_handoff_sits_between_answer_and_review(self):
        self.live_review_for(title="needs-eyes")
        self.pending_handoff_for(to="bob", title="take-this")
        self.assertEqual(next_kind(self.bob()), "respond_handoff")
        self.open_question_for("bob")
        self.assertEqual(next_kind(self.bob()), "answer_question")

    def test_review_task_beats_resume_task(self):
        self.make_session("helper", sid="s-helper")
        self.answered_grace_for("bob", "s-bob")
        self.live_review_for(title="needs-eyes")
        self.assertEqual(next_kind(self.bob()), "review_task")

    def test_resume_covers_declined_handoff_grace_both_sides_of_expiry(self):
        self.make_session("carol", sid="s-carol", provider="grok")
        item, hid = self.pending_handoff_for(
            to="carol", owner="bob", owner_sid="s-bob", title="boomerang")
        coopdb.decline_handoff(
            self.conn, handoff_id=hid, session_id="s-carol", actor="carol",
            reason="no capacity")
        row = self.conn.execute(
            "SELECT status, resume_grace_expires_at FROM items WHERE id=?",
            (item,)).fetchone()
        self.assertEqual(row["status"], "working")
        self.assertIsNotNone(row["resume_grace_expires_at"])
        self.assertEqual(next_kind(self.bob()), "resume_task")
        self.clock.advance(coopdb.DEFAULT_RESUME_GRACE_SECONDS + 1)
        status = self.bob()
        # Item-25 P0: preferred owner still gets resume_task after grace
        # expiry (post-expiry sibling branch); resume_stale remains a label.
        self.assertEqual(next_kind(status), "resume_task")
        entry = next(i for i in status["owned_items"]
                     if i["item_id"] == item)
        self.assertIn("resume_stale", entry["labels"])
        # Non-preferred peers get reclaim-shaped claim_task, not idle.
        carol = self.status("carol")
        self.assertEqual(next_kind(carol), "claim_task")
        self.assertIn("--reclaim", " ".join(
            carol["next_action"].get("command") or []))

    def test_continue_task_covers_live_review_claim(self):
        item, rid, _ = self.live_review_for(title="claimed-by-bob")
        coopdb.claim_review(
            self.conn, review_id=rid, session_id="s-bob", intent="reviewing")
        self.assertEqual(next_kind(self.bob()), "continue_task")

    def test_review_task_requires_eligibility(self):
        self.live_review_for(reviewer="carol", title="carols-review")
        self.assertNotEqual(next_kind(self.bob()), "review_task")
        self.assertNotEqual(next_kind(self.status("alice")),
                            "review_task")
        self.assertEqual(next_kind(self.status("carol")), "review_task")

    def test_second_review_request_surfaces_review_task_for_third_provider(self):
        """Binding second review: after first approve + second request,
        the third provider sees review_task."""
        # Explicit quorum two; provider bindings prove the approvals are
        # independent, but topology itself does not select the quorum.
        self.conn.execute(
            "UPDATE agents SET provider='claude' WHERE name='alice'")
        self.conn.execute(
            "UPDATE agents SET provider='codex' WHERE name='bob'")
        self.conn.execute(
            "UPDATE agents SET provider='grok' WHERE name='carol'")
        self.conn.commit()
        self.make_session("carol", sid="s-carol", provider="grok")
        item, rid, claim = self.live_review_for(
            reviewer="bob", title="needs-two", review_quorum=2)
        # Reuse setUp's s-bob (one running session per agent).
        rev_claim, _ = coopdb.claim_review(
            self.conn, review_id=rid, session_id="s-bob", intent="first look")
        coopdb.submit_verdict(
            self.conn, claim_id=rev_claim["claim_id"], session_id="s-bob",
            actor="bob", verdict="approve")
        self.assertEqual(
            coopdb.approvals_still_needed(self.conn, item), 1)
        owner_status = self.status("alice", "s-alice")
        self.assertTrue(
            any("approvals_needed" in (i.get("labels") or [])
                for i in owner_status["owned_items"] if i["item_id"] == item),
            owner_status["owned_items"])
        self.assertTrue(
            any("needs 1 more approve" in w for w in owner_status["warnings"]),
            owner_status["warnings"])
        # Owner opens the second review for carol.
        coopdb.request_review(
            self.conn, claim_id=claim["claim_id"], session_id="s-alice",
            actor="alice", reviewer="carol")
        self.assertEqual(next_kind(self.status("carol", "s-carol")),
                         "review_task")
        self.assertNotEqual(next_kind(self.bob()), "review_task")

    def test_unnamed_second_review_excludes_already_approved_provider(self):
        """An unnamed quorum review routes to a missing provider, not back
        to the provider whose approval already counts."""
        self.conn.execute(
            "UPDATE agents SET provider='claude' WHERE name='alice'")
        self.conn.execute(
            "UPDATE agents SET provider='codex' WHERE name='bob'")
        self.conn.commit()
        self.make_session("bob2", sid="s-bob2", provider="codex")
        self.make_session("carol", sid="s-carol", provider="grok")
        item, first_review, claim = self.live_review_for(
            title="unnamed-needs-two", review_quorum=2)
        first_claim, _ = coopdb.claim_review(
            self.conn, review_id=first_review, session_id="s-bob",
            intent="first provider review")
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id="s-bob", actor="bob", verdict="approve")
        second_review = coopdb.request_review(
            self.conn, claim_id=claim["claim_id"], session_id="s-alice",
            actor="alice")

        carol_action = self.status("carol", "s-carol")["next_action"]
        self.assertEqual(carol_action["kind"], "review_task")
        self.assertEqual(carol_action["target_id"], second_review)
        self.assertNotEqual(
            next_kind(self.status("bob", "s-bob")), "review_task")
        self.assertNotEqual(
            next_kind(self.status("bob2", "s-bob2")), "review_task")

    def test_owner_idles_when_no_remaining_second_reviewer(self):
        """P1: quorum still open but no unused provider bucket left → idle,
        not infinite request_review (same-reviewer livelock class)."""
        # LaneAwareHints setUp registers carol without a provider (bucket=carol).
        # Pin every non-owner to the first approver's bucket so none remain.
        self.conn.execute(
            "UPDATE agents SET provider='claude' WHERE name='alice'")
        self.conn.execute(
            "UPDATE agents SET provider='codex' WHERE name!='alice' "
            "AND name!='human'")
        self.conn.commit()
        item, first_review, claim = self.live_review_for(
            title="quorum-two-no-peer", review_quorum=2)
        first_claim, _ = coopdb.claim_review(
            self.conn, review_id=first_review, session_id="s-bob",
            intent="only provider review")
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id="s-bob", actor="bob", verdict="approve")
        self.assertEqual(coopdb.approvals_still_needed(self.conn, item), 1)
        block = coopdb.second_reviewer_blocked(self.conn, item)
        self.assertTrue(block["blocked"], block)
        self.assertEqual(block["reason_code"], "second_reviewer_not_selected")

        owner = self.status("alice", "s-alice")
        self.assertEqual(next_kind(owner), "idle")
        self.assertTrue(
            any("second_reviewer_not_selected" in w
                or "no remaining" in w for w in owner["warnings"]),
            owner["warnings"])
        self.assertTrue(
            any("second_reviewer_blocked" in (i.get("labels") or [])
                for i in owner["owned_items"] if i["item_id"] == item),
            owner["owned_items"])

    def test_owner_warns_when_blocked_review_is_already_live(self):
        """A pre-fix stranded review stays idle but names the terminal
        provider-capacity reason instead of becoming an opaque wait."""
        self.conn.execute(
            "UPDATE agents SET provider='claude' WHERE name='alice'")
        self.conn.execute(
            "UPDATE agents SET provider='codex' WHERE name!='alice' "
            "AND name!='human'")
        self.conn.commit()
        item, first_review, _claim = self.live_review_for(
            title="blocked-live-review", review_quorum=2)
        first_claim, _ = coopdb.claim_review(
            self.conn, review_id=first_review, session_id="s-bob",
            intent="only provider review")
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id="s-bob", actor="bob", verdict="approve")
        receipt = self.conn.execute(
            "SELECT receipt_id FROM receipts WHERE item_id=? AND "
            "superseded_at IS NULL", (item,)).fetchone()
        stamp = coopdb.now()
        self.conn.execute(
            "INSERT INTO reviews(item_id,receipt_id,contract_version,"
            "requested_by,requested_by_agent,reviewer,reviewer_agent_id,"
            "status,legacy,created_at) VALUES (?,?,?,?,?,NULL,NULL,"
            "'requested',0,?)",
            (item, receipt["receipt_id"], 1, "alice", "alice", stamp))
        self.conn.commit()

        owner = self.status("alice", "s-alice")
        self.assertEqual(next_kind(owner), "idle")
        self.assertTrue(
            any("second_reviewer_not_selected" in warning
                for warning in owner["warnings"]),
            owner["warnings"])

    def test_review_lane_five_states_route_disjointly(self):
        item, rid, _ = self.live_review_for(title="lifecycle")
        self.assertEqual(next_kind(self.bob()), "review_task")
        claim = coopdb.claim_review(
            self.conn, review_id=rid, session_id="s-bob",
            intent="reviewing")[0]
        self.assertEqual(next_kind(self.bob()), "continue_task")
        self.make_session("carol", sid="s-carol", provider="grok")
        self.assertNotEqual(next_kind(self.status("carol")),
                            "review_task")
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        carol = self.status("carol")
        self.assertNotEqual(next_kind(carol), "review_task")
        self.assertTrue(any("review" in w and "unsafe" in w
                            for w in carol["warnings"]), carol["warnings"])
        coopdb.finish_session(
            self.conn, "s-bob", status="exited", reason="child_exit",
            exit_code=0)
        self.assertEqual(next_kind(self.bob()), "recover_claim")
        self.assertEqual(next_kind(self.status("carol")), "review_task")
        coopdb.admin_release(
            self.conn, claim_id=claim["claim_id"], reason="cleanup")
        self.assertEqual(next_kind(self.status("carol")), "review_task")

    def test_dead_review_never_hints_or_queues(self):
        item, rid, claim = self.live_review_for(title="will-die")
        self.receipt_for(claim, "s-alice", "alice")
        self.assertNotEqual(next_kind(self.bob()), "review_task")
        rows = coopdb.queue(self.conn, for_agent="bob")
        self.assertEqual([r for r in rows if r["kind"] == "review"], [])

    def test_recover_claim_skips_dead_subjects(self):
        # bob's safely-stale REVIEW lane on a review that then dies —
        # not actually reclaimable, so no recover hint survives.
        item, rid, claim = self.live_review_for(title="stale-then-dead")
        coopdb.claim_review(
            self.conn, review_id=rid, session_id="s-bob", intent="reviewing")
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        coopdb.finish_session(
            self.conn, "s-bob", status="exited", reason="child_exit",
            exit_code=0)
        self.assertEqual(next_kind(self.bob()), "recover_claim")
        self.receipt_for(claim, "s-alice", "alice")  # review dies
        self.assertNotEqual(next_kind(self.bob()), "recover_claim")


class ReviewReadSurfaces(StatusBoard):
    """The implementation claim and the active review claim
    side by side across status, the packet, JSON, and the projection —
    named and unnamed, before and after claiming; the claims lane is the
    authority, `reviewer_agent_id` informational."""

    REVIEW_SLOT_KEYS = {
        "review_id", "status", "reviewer", "reviewer_agent_id",
        "requested_by", "receipt_id", "contract_version", "claim",
    }

    def setUp(self):
        super().setUp()
        self.make_session("alice", sid="s-alice")
        self.make_session("bob", sid="s-bob")
        coopdb.register_or_bind_agent(
            self.conn, agent_id="carol", provider="grok")

    def _status_entry(self, item):
        status = self.status("alice")
        _scan_for_token(status)
        return next(i for i in status["owned_items"]
                    if i["item_id"] == item)

    def test_unnamed_review_before_and_after_claiming(self):
        item, rid, claim = self.live_review_for(title="unnamed-flow")
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(set(packet["review"]), self.REVIEW_SLOT_KEYS)
        self.assertIsNone(packet["review"]["reviewer"])
        self.assertIsNone(packet["review"]["reviewer_agent_id"])
        self.assertIsNone(packet["review"]["claim"])
        entry = self._status_entry(item)
        self.assertEqual(entry["review"]["review_id"], rid)
        self.assertIsNone(entry["review"]["claim"])
        coopdb.claim_review(
            self.conn, review_id=rid, session_id="s-bob", intent="reviewing")
        packet = coopdb.item_show(self.conn, item, packet=True)
        # dual claims side by side: alice's implementation, bob's review
        self.assertEqual(packet["claim"]["agent"], "alice")
        self.assertEqual(packet["review"]["claim"]["agent"], "bob")
        self.assertIsNone(packet["review"]["reviewer"])  # still unnamed
        self.assertEqual(packet["review"]["reviewer_agent_id"], "bob")
        entry = self._status_entry(item)
        self.assertEqual(entry["review"]["claim"]["agent"], "bob")
        json.dumps(entry)  # JSON surface, serializable
        rendered = projection.render_inbox(self.conn, "bob")
        self.assertIn("holder=bob", rendered)
        self.assertNotIn("fencing", rendered)

    def test_named_review_renders_designation_before_any_claim(self):
        item, rid, _ = self.live_review_for(
            reviewer="carol", title="named-flow")
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(packet["review"]["reviewer"], "carol")
        self.assertIsNone(packet["review"]["claim"])
        rendered = projection.render_inbox(self.conn, "carol")
        self.assertIn("designated=carol", rendered)
        self.assertIn("holder=-", rendered)
        self.make_session("carol", sid="s-carol", provider="grok")
        coopdb.claim_review(
            self.conn, review_id=rid, session_id="s-carol",
            intent="reviewing")
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(packet["review"]["reviewer"], "carol")
        self.assertEqual(packet["review"]["claim"]["agent"], "carol")

    def test_claims_lane_is_the_authority_over_reviewer_agent_id(self):
        item, rid, claim = self.live_review_for(title="authority")
        coopdb.claim_review(
            self.conn, review_id=rid, session_id="s-bob", intent="reviewing")
        self.receipt_for(claim, "s-alice", "alice")  # closes the lane
        packet = coopdb.item_show(self.conn, item, packet=True)
        self.assertEqual(packet["review"]["reviewer_agent_id"], "bob")
        self.assertIsNone(packet["review"]["claim"])  # lane says nobody


if __name__ == "__main__":
    unittest.main()
