"""Prompt pre-hydration + mechanical-precommit eligibility."""
import hashlib
import json
import pathlib

from agent_coop import coop_autonomous
from agent_coop import coop_prompt_cache
from agent_coop import coopdb
from tests.test_questions import QuestionBoard


def _claim_action(**overrides):
    action = {
        "kind": "claim_task",
        "target_type": "item",
        "target_id": 26,
        "item_id": 26,
        "claim_id": None,
        "lease_seconds": 3600,
        "command": ["python", "-m", "agent_coop", "item", "claim", "26",
                    "--intent", "claim item 26", "--lease-seconds", "3600"],
        "required_inputs": [],
        "choices": [],
    }
    action.update(overrides)
    return action


# ---- hydrated_prompt --------------------------------------------------------

def test_hydrated_prompt_is_prefix_anchored():
    prompt = coop_prompt_cache.hydrated_prompt(
        action=_claim_action(), item={"id": 26, "title": "t"})
    assert prompt.startswith(
        coop_prompt_cache.PROMPT_PREFIX
        + coop_prompt_cache.HYDRATION_HEADER)
    assert '"kind": "claim_task"' in prompt
    assert '"title": "t"' in prompt
    assert "python coop.py" not in prompt
    assert "coop status --json" in prompt
    assert "python -m agent_coop status --json" not in prompt


def test_hydrated_prompt_without_content_is_bare_prefix():
    assert coop_prompt_cache.hydrated_prompt() \
        == coop_prompt_cache.PROMPT_PREFIX
    assert coop_prompt_cache.hydrated_prompt(action=None, item=None) \
        == coop_prompt_cache.PROMPT_PREFIX


def test_oversized_section_is_omitted_whole_never_sliced():
    item = {"id": 26, "body": "x" * (2 * coop_prompt_cache.HYDRATION_MAX_BYTES)}
    prompt = coop_prompt_cache.hydrated_prompt(item=item)
    assert "[item omitted - exceeded its snapshot budget" in prompt
    assert "coop item show" in prompt
    assert '"body"' not in prompt  # dropped whole, not byte-sliced
    assert len(prompt.encode("utf-8")) < (
        coop_prompt_cache.HYDRATION_MAX_BYTES + 2048)


def test_oversized_judgment_packets_use_valid_item_packet_readback():
    huge = "x" * (2 * coop_prompt_cache.HYDRATION_MAX_BYTES)
    prompt = coop_prompt_cache.hydrated_prompt(
        action=_claim_action(),
        handoff={"body": huge},
        review={"body": huge},
    )

    assert "coop handoff show" not in prompt
    assert "coop review show" not in prompt
    assert "coop item show 26 --packet --json" in prompt


def test_oversized_optional_context_cannot_remove_the_action_envelope():
    # Acceptance case: arbitrarily large status and
    # item payloads must never displace or invalidate the mandatory
    # next_action envelope or the exact addressed question.
    action = _claim_action()
    huge = "x" * (3 * coop_prompt_cache.HYDRATION_MAX_BYTES)
    prompt = coop_prompt_cache.hydrated_prompt(
        action=action,
        status={"noise": huge, "next_action": action},
        item={"id": 26, "body": huge},
        questions=[{"question_id": 3, "item_id": 26,
                    "exact_question": "the exact addressed question",
                    "asked_by_agent": "codex"}])
    body = prompt.split(coop_prompt_cache.HYDRATION_HEADER, 1)[1]
    sections = body.split("\n\n")
    action_sections = [
        s for s in sections
        if s.startswith("next_action (already derived")]
    assert len(action_sections) == 1
    assert json.loads(action_sections[0].split("\n", 1)[1]) == action
    assert sections[1].startswith("next_action ")  # action leads (after note)
    assert "the exact addressed question" in prompt
    assert "[status omitted" in prompt and "[item omitted" in prompt
    assert len(prompt.encode("utf-8")) < (
        coop_prompt_cache.HYDRATION_MAX_BYTES + 2048)


def test_action_envelope_present_even_with_status():
    # v2.1: status no longer displaces the action (the model executes the
    # envelope; the status summary is context, not the carrier).
    prompt = coop_prompt_cache.hydrated_prompt(
        action=_claim_action(),
        status={"next_action": _claim_action(), "unread": {"message": 2}})
    assert "next_action (already derived for this turn" in prompt
    assert "status --json (already run for this turn):" in prompt
    assert '"message": 2' in prompt


def test_default_hydration_bytes_remain_certified_snapshot():
    action = _claim_action()
    prompt = coop_prompt_cache.hydrated_prompt(
        action=action,
        status={"next_action": action, "unread": {"message": 2}},
        item={"id": 26, "title": "t"},
    )

    # v4 keeps the autonomous policy self-contained and uses the installed
    # console script for shell fallbacks.
    assert len(prompt.encode("utf-8")) == 2455
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        "ac6685aa230fb73337537c998e0cdca5d6a90721b58099d02fd7376a1089d29b"
    )
    assert prompt == coop_prompt_cache.hydrated_prompt(
        action=action,
        status={"next_action": action, "unread": {"message": 2}},
        item={"id": 26, "title": "t"},
        token_efficient=False,
    )


def test_token_efficient_crib_has_exact_choices_and_receipt_grammar():
    action = _claim_action(
        kind="continue_task",
        target_type="claim",
        target_id=9,
        item_id=26,
        claim_id=9,
        command=[
            "python", "-m", "agent_coop", "receipt", "submit",
            "--claim", "9", "--path", "{path}",
            "--summary", "{summary}", "--proof", "{proof}",
            "--proof-ref", "{proof_ref}",
        ],
        required_inputs=[
            "contract_work", "path", "summary", "proof", "proof_ref",
        ],
    )
    evidence = [
        {"question_id": 2, "answer": "pong", "proof_ref": "event:41"},
        {"question_id": 3, "answer": "ack", "proof_ref": "event:43"},
    ]

    crib = coop_prompt_cache.command_crib(
        action,
        peer_agents=["grok", "claude"],
        receipt_evidence=evidence,
    )

    assert crib["kind"] == "continue_task"
    assert crib["argv"] == action["command"]
    assert crib["required_inputs"] == action["required_inputs"]
    assert crib["peer_agents"] == ["claude", "grok"]
    assert crib["proof_references"] == ["event:41", "event:43"]
    assert crib["proof_ref_rule"] == (
        "repeat --proof-ref once per value; use only listed event refs or "
        "file:<absolute-path>"
    )
    assert crib["follow_on"]["needs_input"] == [
        *coopdb.CLI_ARGV, "needs-input", "--claim", "9", "--to",
        "{peer_agent}", "--question", "{exact_question}",
    ]
    assert crib["follow_on"]["handoff_create"][:8] == [
        *coopdb.CLI_ARGV, "handoff", "create", "--claim", "9", "--to",
    ]
    assert len(json.dumps(
        crib,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")) <= coop_prompt_cache.COMMAND_CRIB_MAX_BYTES


def test_token_efficient_crib_fits_budget_with_max_path_interpreter(monkeypatch):
    # Board-routed argv arrays embed the absolute interpreter path
    # (coopdb.CLI_ARGV) up to three times per crib: the routed command plus
    # the needs-input and handoff follow-ons. The budget must hold for a
    # MAX_PATH-length interpreter, or every turn on a normal pipx install
    # loses the crib and falls back to a board read.
    interpreter = "\\".join([
        "C:", "Users", "u" * 180, "AppData", "Local", "pipx", "venvs",
        "agent-coop", "Scripts", "python.exe",
    ])
    assert len(interpreter) >= 240
    cli_argv = (interpreter, "-m", "agent_coop")
    monkeypatch.setattr(coopdb, "CLI_ARGV", cli_argv)
    action = _claim_action(
        kind="continue_task",
        target_type="claim",
        target_id=9,
        item_id=26,
        claim_id=9,
        command=[
            *cli_argv, "receipt", "submit",
            "--claim", "9", "--path", "{path}",
            "--summary", "{summary}", "--proof", "{proof}",
            "--proof-ref", "{proof_ref}",
        ],
        required_inputs=[
            "contract_work", "path", "summary", "proof", "proof_ref",
        ],
    )
    evidence = [
        {"question_id": 2, "answer": "pong", "proof_ref": "event:41"},
        {"question_id": 3, "answer": "ack", "proof_ref": "event:43"},
    ]

    crib = coop_prompt_cache.command_crib(
        action,
        peer_agents=["grok", "claude"],
        receipt_evidence=evidence,
    )

    assert crib["argv"][0] == interpreter
    assert crib["follow_on"]["needs_input"][0] == interpreter
    assert crib["follow_on"]["handoff_create"][0] == interpreter
    assert len(json.dumps(
        crib,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")) <= coop_prompt_cache.COMMAND_CRIB_MAX_BYTES


def test_token_efficient_choice_crib_uses_only_enumerated_argv():
    action = _claim_action(
        kind="respond_handoff",
        target_type="handoff",
        target_id=7,
        item_id=26,
        command=None,
        required_inputs=["handoff_response"],
        choices=[
            {
                "kind": "accept_handoff",
                "command": [
                    "python", "coop.py", "handoff", "accept", "--id", "7",
                ],
                "required_inputs": [],
            },
            {
                "kind": "decline_handoff",
                "command": [
                    "python", "coop.py", "handoff", "decline", "--id", "7",
                    "--reason", "{reason}",
                ],
                "required_inputs": ["reason"],
            },
        ],
    )

    crib = coop_prompt_cache.command_crib(action)

    assert crib == {
        "kind": "respond_handoff",
        "required_inputs": ["handoff_response"],
        "choices": [
            {
                "kind": "accept_handoff",
                "argv": action["choices"][0]["command"],
                "required_inputs": [],
            },
            {
                "kind": "decline_handoff",
                "argv": action["choices"][1]["command"],
                "required_inputs": ["reason"],
            },
        ],
    }


def test_token_efficient_hydration_is_compact_ordered_and_bounded(monkeypatch):
    action = _claim_action()
    prompt = coop_prompt_cache.hydrated_prompt(
        action=action,
        status={
            "agent": {"agent_id": "claude", "provider": "claude"},
            "session": {"session_id": "s", "status": "running"},
            "claims": [{
                "claim_id": 9,
                "item_id": 26,
                "lane": "implementation:26",
                "lease_expires_at": "far away",
                "last_checkpoint_at": "recent",
                "progress_stale": False,
            }],
            "owned_items": [{
                "item_id": 26,
                "title": "t",
                "status": "working",
                "owner": "claude",
                "next_actor": "claude",
                "labels": [],
                "review": None,
            }],
            "unread": {"answer": 2},
            "warnings": [],
            "next_action": action,
            "noise": "must disappear",
        },
        item={"id": 26, "context": "x" * 7_900},
        token_efficient=True,
    )

    assert prompt.startswith(
        coop_prompt_cache.TOKEN_EFFICIENT_PROMPT_PREFIX
        + coop_prompt_cache.HYDRATION_HEADER
    )
    body = prompt.split(coop_prompt_cache.HYDRATION_HEADER, 1)[1]
    assert body.index("next_action (already derived") \
        < body.index("command_crib (exact argv; do not run help)")
    status_json = body.split(
        "compact status projection (next_action intentionally omitted):\n",
        1,
    )[1]
    status = json.loads(status_json)
    assert "next_action" not in status
    assert "noise" not in status
    assert "lease_expires_at" not in status["claims"][0]
    assert '"context": "' + ("x" * 100) in prompt
    assert len(prompt.encode("utf-8")) <= coop_prompt_cache.HYDRATION_MAX_BYTES

    monkeypatch.setattr(coop_prompt_cache, "COMMAND_CRIB_MAX_BYTES", 10)
    omitted = coop_prompt_cache.hydrated_prompt(
        action=action,
        token_efficient=True,
    )
    assert "[command_crib omitted - exceeded its snapshot budget" in omitted
    assert "command_crib (exact argv; do not run help):" not in omitted


def test_token_efficient_total_budget_omits_sections_whole():
    action = _claim_action()
    prompt = coop_prompt_cache.hydrated_prompt(
        action=action,
        item={"id": 26, "context": "i" * 8_000},
        questions=[{"question_id": 1, "exact_question": "q" * 2_800}],
        answered_questions=[{"question_id": 2, "answer": "a" * 2_800}],
        handoff={"handoff_id": 3, "summary": "h" * 2_800},
        review={"id": 4, "body": "r" * 4_800},
        receipt_evidence=[{"question_id": 5, "answer": "e" * 4_800}],
        token_efficient=True,
    )

    assert len(prompt.encode("utf-8")) <= coop_prompt_cache.HYDRATION_MAX_BYTES
    assert " omitted - exceeded its snapshot budget" in prompt
    assert "next_action (already derived" in prompt
    assert "command_crib (exact argv; do not run help)" in prompt


# ---- prompt_trace_details on hydrated prompts -------------------------------

def test_trace_details_recognize_hydrated_prompt():
    prompt = coop_prompt_cache.hydrated_prompt(action=_claim_action())
    details = coop_prompt_cache.prompt_trace_details(
        prompt, provider="claude", provider_hint=True)
    assert details["prompt_cache_mode"] == "provider_hint"
    assert details["prompt_prefix_sha256"] \
        == coop_prompt_cache.PROMPT_PREFIX_SHA256
    assert details["prompt_hydration_bytes"] > 0


def test_trace_details_bare_prefix_unchanged():
    details = coop_prompt_cache.prompt_trace_details(
        coop_prompt_cache.PROMPT_PREFIX, provider="claude")
    assert details["prompt_cache_mode"] == "provider_managed"
    assert "prompt_hydration_bytes" not in details


def test_trace_details_unknown_prompt_still_unobserved():
    details = coop_prompt_cache.prompt_trace_details(
        "something else entirely", provider="claude")
    assert details == {"prompt_cache_mode": "unobserved"}


# ---- mechanical_precommit_eligible ------------------------------------------

def test_eligible_plain_claim():
    assert coop_autonomous.mechanical_precommit_eligible(_claim_action())


def test_eligible_reclaim_with_prefilled_reason():
    action = _claim_action(command=[
        "python", "-m", "agent_coop", "item", "claim", "26",
        "--intent", "resume item 26", "--reclaim", "--reason",
        "resume safe prior 7 on this lane", "--lease-seconds", "3600"])
    assert coop_autonomous.mechanical_precommit_eligible(action)


def test_not_eligible_placeholder_command():
    action = _claim_action(
        kind="answer_question",
        command=["python", "-m", "agent_coop", "question", "answer",
                 "--claim", "5", "--answer", "{answer}"],
        required_inputs=["answer"])
    assert not coop_autonomous.mechanical_precommit_eligible(action)


def test_not_eligible_choices_or_inputs():
    assert not coop_autonomous.mechanical_precommit_eligible(
        _claim_action(kind="respond_handoff",
                      command=None,
                      required_inputs=["handoff_response"],
                      choices=[{"kind": "accept_handoff"}]))
    assert not coop_autonomous.mechanical_precommit_eligible(
        _claim_action(required_inputs=["answer"]))


def test_not_eligible_unlisted_kind_or_shape():
    assert not coop_autonomous.mechanical_precommit_eligible(
        _claim_action(kind="respond_handoff"))
    assert not coop_autonomous.mechanical_precommit_eligible(
        _claim_action(command=None))
    assert not coop_autonomous.mechanical_precommit_eligible(None)
    assert not coop_autonomous.mechanical_precommit_eligible("claim_task")


def test_hydrated_snapshot_json_round_trips():
    prompt = coop_prompt_cache.hydrated_prompt(action=_claim_action())
    payload = prompt.split(
        "next_action (already derived for this turn - execute this):\n",
        1)[1]
    assert json.loads(payload)["item_id"] == 26


# ---- defaults, completion precommit, tiering, status ------------------------

def test_action_leads_the_packet():
    prompt = coop_prompt_cache.hydrated_prompt(
        action=_claim_action(),
        status={"unread": {"message": 2}})
    body = prompt.split(coop_prompt_cache.HYDRATION_HEADER, 1)[1]
    assert body.index("next_action (already derived") \
        < body.index("status --json (already run")


def test_complete_task_is_precommit_eligible():
    action = _claim_action(
        kind="complete_task",
        command=["python", "-m", "agent_coop", "item", "complete",
                 "--claim", "9"])
    assert coop_autonomous.mechanical_precommit_eligible(action)


def test_persistent_defaults_cover_core_providers():
    assert coop_autonomous.default_persistent_providers(
        ["claude", "codex", "grok"]) == ["claude", "codex", "grok"]
    assert coop_autonomous.default_persistent_providers(
        ["claude", "codex"]) == ["claude", "codex"]
    assert coop_autonomous.default_prompt_cache_providers(
        ["claude", "codex", "grok"]) == ["claude"]
    assert coop_autonomous.default_prompt_cache_providers(["codex"]) == []


def test_handoff_responses_overlap_across_items_only():
    from agent_coop.coop_action_scheduler import (
        ActionCandidate, action_lane, actions_independent)

    def hoff(agent, handoff_id, item_id):
        return ActionCandidate(agent=agent, hint="respond_handoff", action={
            "kind": "respond_handoff", "target_id": handoff_id,
            "item_id": item_id})

    cross_a = hoff("codex", 5, 31)
    cross_b = hoff("grok", 6, 32)
    same_item = hoff("grok", 7, 31)
    assert action_lane(cross_a) == "handoff:31"
    assert actions_independent(cross_a, cross_b)
    assert not actions_independent(cross_a, same_item)


def test_questions_hydration_section():
    prompt = coop_prompt_cache.hydrated_prompt(questions=[
        {"question_id": 3, "item_id": 29,
         "exact_question": "ping from codex", "asked_by_agent": "codex"}])
    assert "open questions addressed to you" in prompt
    assert '"exact_question": "ping from codex"' in prompt
    assert coop_prompt_cache.prompt_trace_details(
        prompt, provider="claude")["prompt_hydration_bytes"] > 0


def test_prefix_v4_says_execute_now_not_reread():
    prefix = coop_prompt_cache.PROMPT_PREFIX
    assert coop_prompt_cache.PROMPT_PREFIX_VERSION == "coop-bootstrap-v4"
    assert "execute `next_action.command` now" in prefix
    assert "Repeatedly run" not in prefix


def test_causal_sections_render():
    prompt = coop_prompt_cache.hydrated_prompt(
        answered_questions=[
            {"question_id": 1, "item_id": 29, "exact_question": "ping?",
             "answer": "pong", "answered_by_agent": "grok"}],
        handoff={"handoff_id": 4, "reason": "r", "summary": "s"},
        review={"id": 7, "receipt": {"receipt_id": 3, "summary": "done"}})
    assert "questions you asked that are now answered" in prompt
    assert '"answer": "pong"' in prompt
    assert "pending handoff addressed to you" in prompt
    assert "review packet" in prompt
    assert prompt.startswith(
        coop_prompt_cache.PROMPT_PREFIX + coop_prompt_cache.HYDRATION_HEADER)


class CausalHydrationRows(QuestionBoard):
    """causal_hydration_rows against a real board (hydration v2)."""

    STAMP = "2026-07-27T00:00:00+00:00"

    def test_answered_own_questions_hydrate_for_the_asker_only(self):
        item, claim, alice, bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        self.clock.advance(5)   # the answer commits after the suspension
        response = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="on it")
        coopdb.answer_question(
            self.conn, claim_id=response["claim_id"], session_id="s-bob",
            answer="42")
        answered, handoff, review = coop_autonomous.causal_hydration_rows(
            self.conn, "alice",
            {"kind": "resume_task", "target_id": item}, item)
        assert answered == [{
            "question_id": qid, "item_id": item, "exact_question": "exact?",
            "answer": "42", "answered_by_agent": "bob"}]
        assert handoff is None and review is None
        bob_answered, _, _ = coop_autonomous.causal_hydration_rows(
            self.conn, "bob", {"kind": "claim_task", "target_id": item},
            item)
        assert bob_answered is None

    def test_receipt_evidence_uses_exact_question_answer_event_refs(self):
        item, claim, _alice, _bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        response = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob", intent="ping")
        coopdb.answer_question(
            self.conn,
            claim_id=response["claim_id"],
            session_id="s-bob",
            answer="pong",
        )
        event = self.conn.execute(
            "SELECT event_id, payload_json FROM events "
            "WHERE item_id=? AND event_type='question_answered' "
            "ORDER BY event_id DESC LIMIT 1",
            (item,),
        ).fetchone()
        assert json.loads(event["payload_json"])["question_id"] == qid

        evidence = coop_autonomous.receipt_hydration_rows(self.conn, item)

        assert evidence == [{
            "question_id": qid,
            "item_id": item,
            "exact_question": "exact?",
            "answer": "pong",
            "answered_by_agent": "bob",
            "proof_ref": f"event:{event['event_id']}",
        }]

    def test_receipt_evidence_omits_rows_without_an_exact_event(self):
        item = self.make_item()
        self.make_session("alice", sid="s-a")
        self.make_session("bob", sid="s-b")
        stamp = coopdb.now()
        self.conn.execute(
            "INSERT INTO questions(item_id, exact_question, "
            "asked_by_agent, asked_by_session, assigned_to_agent, status, "
            "answer, answered_by_agent, answered_by_session, asked_at, "
            "answered_at) VALUES (?,?,?,?,?,'answered',?,?,?,?,?)",
            (
                item, "orphan?", "alice", "s-a", "bob", "orphan",
                "bob", "s-b", stamp, stamp,
            ),
        )
        self.conn.commit()

        assert coop_autonomous.receipt_hydration_rows(self.conn, item) is None

    def test_second_suspension_excludes_the_consumed_first_answer(self):
        # Acceptance case: two question/resume
        # cycles; the second wake packet carries ONLY the answer that
        # caused it, not the answer already consumed by the first resume.
        item, claim, alice, bob = self.working_pair()
        q1 = self.needs_input(claim["claim_id"], "s-alice")
        self.clock.advance(5)
        response = coopdb.claim_question(
            self.conn, question_id=q1, session_id="s-bob", intent="on it")
        coopdb.answer_question(
            self.conn, claim_id=response["claim_id"], session_id="s-bob",
            answer="first")
        self.clock.advance(5)
        resumed = self.claim(item, "alice", "s-alice",
                             intent="resume after first answer",
                             reclaim_reason="resume: q1 answered")
        self.clock.advance(5)
        q2 = self.needs_input(resumed["claim_id"], "s-alice",
                              question="second?")
        self.clock.advance(5)
        response2 = coopdb.claim_question(
            self.conn, question_id=q2, session_id="s-bob", intent="again")
        coopdb.answer_question(
            self.conn, claim_id=response2["claim_id"], session_id="s-bob",
            answer="second")
        answered, _, _ = coop_autonomous.causal_hydration_rows(
            self.conn, "alice",
            {"kind": "resume_task", "target_id": item}, item)
        assert [row["question_id"] for row in answered] == [q2]
        assert answered[0]["answer"] == "second"

    def test_handoff_and_review_targets_hydrate(self):
        item, claim, alice, bob = self.working_pair()
        self.conn.execute(
            "INSERT INTO handoffs (item_id, claim_id, from_agent, "
            "from_session, execution_fencing_token, to_agent, reason, "
            "summary, completed_work, remaining_work, risks, "
            "proof_references, suggested_next_action, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item, claim["claim_id"], "alice", "s-alice", 1, "bob", "why",
             "sum", "done", "left", "risk", "[]", "next", "pending",
             self.STAMP))
        hid = self.conn.execute(
            "SELECT handoff_id FROM handoffs").fetchone()["handoff_id"]
        _, handoff, _ = coop_autonomous.causal_hydration_rows(
            self.conn, "bob",
            {"kind": "respond_handoff", "target_id": hid}, item)
        assert handoff["reason"] == "why" and handoff["to_agent"] == "bob"
        self.conn.execute(
            "INSERT INTO receipts (item_id, claim_id, fencing_token, "
            "contract_version, submitted_by_agent, submitted_by_session, "
            "summary, proof, proof_references_json, source_path, sha256, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item, claim["claim_id"], 1, 1, "alice", "s-alice", "did it",
             "proof", "[]", "p.md", "0" * 64, self.STAMP))
        receipt_id = self.conn.execute(
            "SELECT receipt_id FROM receipts").fetchone()["receipt_id"]
        self.conn.execute(
            "INSERT INTO reviews (item_id, receipt_id, requested_by, "
            "status, created_at) VALUES (?,?,?,?,?)",
            (item, receipt_id, "alice", "requested", self.STAMP))
        review_id = self.conn.execute(
            "SELECT id FROM reviews").fetchone()["id"]
        for act in (
                {"kind": "review_task", "target_id": review_id},
                {"kind": "continue_task", "target_type": "review",
                 "target_id": review_id}):
            _, _, review = coop_autonomous.causal_hydration_rows(
                self.conn, "bob", act, item)
            assert review["receipt"]["summary"] == "did it", act
        _, no_handoff, no_review = coop_autonomous.causal_hydration_rows(
            self.conn, "bob", {"kind": "claim_task", "target_id": item},
            item)
        assert no_handoff is None and no_review is None


class PostwriteActionSatisfied(QuestionBoard):
    """Exact terminal-write proof for the token-efficient exit grace."""

    LEASE = 3600

    def _action(self, agent, session_id, item):
        return coopdb.status(
            self.conn,
            agent,
            session_id=session_id,
            action_lease_seconds=self.LEASE,
            item_id=item,
        )["next_action"]

    def _before(self, session_id, item):
        return coopdb.actor_event_probe(
            self.conn,
            session_id=session_id,
            item_id=item,
        )

    def _satisfied(self, agent, session_id, action, before):
        return coop_autonomous.postwrite_action_satisfied(
            self.conn,
            agent=agent,
            session_id=session_id,
            action=action,
            progress_before=before,
            lease_seconds=self.LEASE,
        )

    def _working_pair_named(self, suffix):
        item = self.make_item(title=f"item-{suffix}")
        owner = f"alice-{suffix}"
        peer = f"bob-{suffix}"
        owner_sid = self.make_session(
            owner,
            sid=f"s-{owner}",
            provider="claude",
        )
        peer_sid = self.make_session(
            peer,
            sid=f"s-{peer}",
            provider="codex",
        )
        claim = self.claim(item, owner, owner_sid)
        return item, claim, owner, owner_sid, peer, peer_sid

    def _continue_pair(self, suffix):
        item, claim, owner, owner_sid, peer, peer_sid = (
            self._working_pair_named(suffix)
        )
        action = self._action(owner, owner_sid, item)
        assert action["kind"] == "continue_task"
        assert action["target_type"] == "claim"
        return item, claim, action, owner, owner_sid, peer, peer_sid

    def test_held_question_answer_is_terminal(self):
        item, claim, _alice, _bob = self.working_pair()
        qid = self.needs_input(claim["claim_id"], "s-alice")
        response = coopdb.claim_question(
            self.conn,
            question_id=qid,
            session_id="s-bob",
            intent="answer",
        )
        action = self._action("bob", "s-bob", item)
        before = self._before("s-bob", item)

        coopdb.answer_question(
            self.conn,
            claim_id=response["claim_id"],
            session_id="s-bob",
            answer="pong",
        )

        assert action["kind"] == "answer_question"
        assert self._satisfied("bob", "s-bob", action, before)

    def test_handoff_accept_and_decline_are_terminal(self):
        for response in ("accept", "decline"):
            with self.subTest(response=response):
                item, claim, owner, owner_sid, peer, peer_sid = (
                    self._working_pair_named(response)
                )
                proof_event = self.conn.execute(
                    "SELECT event_id FROM events WHERE item_id=? AND "
                    "claim_id=? ORDER BY event_id LIMIT 1",
                    (item, claim["claim_id"]),
                ).fetchone()["event_id"]
                handoff = coopdb.create_handoff(
                    self.conn,
                    claim_id=claim["claim_id"],
                    session_id=owner_sid,
                    actor=owner,
                    to_agent=peer,
                    reason="rotate",
                    summary="ready",
                    completed="setup",
                    remaining="finish",
                    risks="none",
                    next_action="continue",
                    proof_refs=[f"event:{proof_event}"],
                )
                action = self._action(peer, peer_sid, item)
                before = self._before(peer_sid, item)

                if response == "accept":
                    coopdb.accept_handoff(
                        self.conn,
                        handoff_id=handoff["handoff_id"],
                        session_id=peer_sid,
                        actor=peer,
                        intent="accept transfer",
                    )
                else:
                    coopdb.decline_handoff(
                        self.conn,
                        handoff_id=handoff["handoff_id"],
                        session_id=peer_sid,
                        actor=peer,
                        reason="cannot take it",
                    )

                assert action["kind"] == "respond_handoff"
                assert self._satisfied(peer, peer_sid, action, before)

    def test_review_verdict_is_terminal(self):
        item = self.make_item()
        alice = self.make_session("alice", sid="s-review-owner")
        bob = self.make_session(
            "bob", sid="s-reviewer", provider="codex")
        claim = self.claim(item, "alice", alice)
        evidence = pathlib.Path(self.tmp.name) / "review-evidence.md"
        evidence.write_text("verified", encoding="utf-8")
        coopdb.submit_receipt(
            self.conn,
            claim_id=claim["claim_id"],
            session_id=alice,
            actor="alice",
            path=str(evidence),
            summary="done",
            proof="focused test",
            proof_refs=[],
        )
        review_id = coopdb.request_review(
            self.conn,
            claim_id=claim["claim_id"],
            session_id=alice,
            actor="alice",
            reviewer="bob",
        )
        review_claim, _packet = coopdb.claim_review(
            self.conn,
            review_id=review_id,
            session_id=bob,
            intent="review",
        )
        action = self._action("bob", bob, item)
        before = self._before(bob, item)

        coopdb.submit_verdict(
            self.conn,
            claim_id=review_claim["claim_id"],
            session_id=bob,
            actor="bob",
            verdict="approve",
        )

        assert action["kind"] == "continue_task"
        assert action["target_type"] == "review"
        assert self._satisfied("bob", bob, action, before)

    def test_receipt_needs_input_handoff_and_block_are_terminal(self):
        transitions = ("receipt", "needs_input", "handoff", "blocked")
        for transition in transitions:
            with self.subTest(transition=transition):
                (
                    item,
                    claim,
                    action,
                    owner,
                    owner_sid,
                    peer,
                    _peer_sid,
                ) = self._continue_pair(transition)
                before = self._before(owner_sid, item)
                if transition == "receipt":
                    evidence = (
                        pathlib.Path(self.tmp.name)
                        / f"receipt-{item}.md"
                    )
                    evidence.write_text("verified", encoding="utf-8")
                    coopdb.submit_receipt(
                        self.conn,
                        claim_id=claim["claim_id"],
                        session_id=owner_sid,
                        actor=owner,
                        path=str(evidence),
                        summary="done",
                        proof="focused test",
                        proof_refs=[],
                    )
                elif transition == "needs_input":
                    coopdb.needs_input(
                        self.conn,
                        claim_id=claim["claim_id"],
                        session_id=owner_sid,
                        to_agent=peer,
                        question="ping?",
                    )
                elif transition == "handoff":
                    proof_event = self.conn.execute(
                        "SELECT event_id FROM events WHERE item_id=? AND "
                        "claim_id=? ORDER BY event_id LIMIT 1",
                        (item, claim["claim_id"]),
                    ).fetchone()["event_id"]
                    coopdb.create_handoff(
                        self.conn,
                        claim_id=claim["claim_id"],
                        session_id=owner_sid,
                        actor=owner,
                        to_agent=peer,
                        reason="rotate",
                        summary="ready",
                        completed="setup",
                        remaining="finish",
                        risks="none",
                        next_action="continue",
                        proof_refs=[f"event:{proof_event}"],
                    )
                else:
                    coopdb.checkpoint(
                        self.conn,
                        ctype="blocked",
                        claim_id=claim["claim_id"],
                        actor=owner,
                        session_id=owner_sid,
                        note="stop condition reached",
                    )

                assert self._satisfied(
                    owner, owner_sid, action, before
                )

    def test_checkpoint_and_spoofed_terminal_event_fail_closed(self):
        item, claim, action, owner, owner_sid, _peer, _peer_sid = (
            self._continue_pair("spoof")
        )
        before = self._before(owner_sid, item)
        coopdb.checkpoint(
            self.conn,
            ctype="step",
            claim_id=claim["claim_id"],
            actor=owner,
            session_id=owner_sid,
            note="still working",
        )
        assert not self._satisfied(owner, owner_sid, action, before)

        before = self._before(owner_sid, item)
        coopdb.append_event(
            self.conn,
            item_id=item,
            event_type="receipt_submitted",
            actor_agent_id=owner,
            actor_session_id=owner_sid,
            claim_id=claim["claim_id"],
            payload={"item_id": item, "receipt_id": 999999},
        )
        assert not self._satisfied(owner, owner_sid, action, before)

    def test_sibling_wrong_session_wrong_target_and_kind_fail_closed(self):
        item, claim, action, owner, owner_sid, peer, peer_sid = (
            self._continue_pair("adversarial")
        )
        sibling = self.make_item(title="sibling")
        before = self._before(owner_sid, item)
        coopdb.append_event(
            self.conn,
            item_id=sibling,
            event_type="receipt_submitted",
            actor_agent_id=owner,
            actor_session_id=owner_sid,
            claim_id=claim["claim_id"],
            payload={"item_id": sibling, "receipt_id": 1},
        )
        assert not self._satisfied(owner, owner_sid, action, before)

        before = self._before(owner_sid, item)
        coopdb.append_event(
            self.conn,
            item_id=item,
            event_type="receipt_submitted",
            actor_agent_id=peer,
            actor_session_id=peer_sid,
            claim_id=claim["claim_id"],
            payload={"item_id": item, "receipt_id": 1},
        )
        assert not self._satisfied(owner, owner_sid, action, before)
        self.conn.commit()

        qid = coopdb.needs_input(
            self.conn,
            claim_id=claim["claim_id"],
            session_id=owner_sid,
            to_agent=peer,
            question="exact?",
        )
        response = coopdb.claim_question(
            self.conn,
            question_id=qid,
            session_id=peer_sid,
            intent="answer",
        )
        held = self._action(peer, peer_sid, item)
        wrong_target = dict(held, target_id=qid + 1000)
        before = self._before(peer_sid, item)
        coopdb.answer_question(
            self.conn,
            claim_id=response["claim_id"],
            session_id=peer_sid,
            answer="pong",
        )
        assert not self._satisfied(
            peer, peer_sid, wrong_target, before
        )
        assert not self._satisfied(
            peer,
            peer_sid,
            dict(held, kind="claim_task"),
            before,
        )


# Provider session ids are uuid4; the warm record accepts only that shape.
WARM_SID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_warm_session_round_trip(tmp_path):
    assert coop_autonomous.load_warm_claude_session(tmp_path) is None
    coop_autonomous.save_warm_claude_session(
        tmp_path, WARM_SID, now_epoch=1000.0)
    assert coop_autonomous.load_warm_claude_session(
        tmp_path, now_epoch=2000.0) == WARM_SID
    stale = 1000.0 + coop_autonomous.WARM_SESSION_MAX_AGE_S + 1
    assert coop_autonomous.load_warm_claude_session(
        tmp_path, now_epoch=stale) is None
    coop_autonomous.save_warm_claude_session(tmp_path, None)  # no-op
    assert coop_autonomous.load_warm_claude_session(
        tmp_path, now_epoch=2000.0) == WARM_SID
    assert not (tmp_path / "warm-claude-session.json.tmp").exists()
    # The record is writable by every agent turn: an id that is not
    # UUID-shaped is "no warm session", never an argv value.
    coop_autonomous.save_warm_claude_session(
        tmp_path, "sess-abc", now_epoch=1000.0)
    assert coop_autonomous.load_warm_claude_session(
        tmp_path, now_epoch=2000.0) is None


def test_warm_session_rejects_a_prefix_version_mismatch(tmp_path):
    # A prefix bump changes what the resumed conversation was taught;
    # resuming it would replay stale instructions - load must miss.
    coop_autonomous.save_warm_claude_session(
        tmp_path, WARM_SID, now_epoch=1000.0)
    record = json.loads(
        (tmp_path / "warm-claude-session.json").read_text())
    assert record["prompt_prefix_version"] \
        == coop_prompt_cache.PROMPT_PREFIX_VERSION
    record["prompt_prefix_version"] = "coop-bootstrap-v0"
    (tmp_path / "warm-claude-session.json").write_text(json.dumps(record))
    assert coop_autonomous.load_warm_claude_session(
        tmp_path, now_epoch=2000.0) is None
    # Records written before the field existed stay trusted (back-compat).
    del record["prompt_prefix_version"]
    (tmp_path / "warm-claude-session.json").write_text(json.dumps(record))
    assert coop_autonomous.load_warm_claude_session(
        tmp_path, now_epoch=2000.0) == WARM_SID


def test_first_then_fresh_session_ids():
    factory = coop_autonomous.first_then_fresh_session_ids("warm-1")
    assert factory() == "warm-1"
    fresh = factory()
    assert fresh != "warm-1" and fresh != factory()
