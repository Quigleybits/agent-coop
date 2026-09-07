"""Strict, workspace-free bounded coordination decision contracts."""

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent_coop import coop_decisions
from agent_coop import coop_autonomous
from agent_coop import coop_action_scheduler
from agent_coop import coop_prompt_cache
from agent_coop import coopdb
from agent_coop import coop_start


FINGERPRINT = "a" * 64


def request(kind, **expected):
    return coop_decisions.make_decision_request(
        decision_kind=kind,
        provider="claude",
        agent_id="claude",
        item_id=39,
        action_fingerprint=FINGERPRINT,
        prompt="Return only the bounded decision.",
        **expected,
    )


def envelope(value, *, usage=None):
    return json.dumps({
        "type": "result",
        "structured_output": value,
        "usage": usage or {
            "input_tokens": 80,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 400,
            "output_tokens": 10,
        },
    })


def test_request_is_frozen_and_binds_exact_schema_identity():
    result = request("answer_questions", question_ids=(7, 9))

    assert result.decision_kind == "answer_questions"
    assert result.provider == "claude"
    assert result.agent_id == "claude"
    assert result.item_id == 39
    assert result.action_fingerprint == FINGERPRINT
    assert result.session_class == "isolated_no_workspace"
    assert result.json_schema["additionalProperties"] is False
    items = result.json_schema["properties"]["answers"]["items"]
    assert items["additionalProperties"] is False
    assert items["properties"]["question_id"]["enum"] == [7, 9]
    with pytest.raises(FrozenInstanceError):
        result.provider = "grok"


@pytest.mark.parametrize("kwargs", [
    {"decision_kind": "unknown"},
    {"provider": "unknown"},
    {"item_id": True},
    {"item_id": 0},
    {"action_fingerprint": "short"},
    {"prompt": ""},
])
def test_request_rejects_invalid_identity(kwargs):
    values = {
        "decision_kind": "answer_questions",
        "provider": "claude",
        "agent_id": "claude",
        "item_id": 39,
        "action_fingerprint": FINGERPRINT,
        "prompt": "bounded",
        "question_ids": (7,),
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        coop_decisions.make_decision_request(**values)


def test_answer_questions_requires_exact_order_ids_and_bounded_text():
    req = request("answer_questions", question_ids=(7, 9))
    valid = {
        "answers": [
            {"question_id": 7, "answer": "one"},
            {"question_id": 9, "answer": "two"},
        ],
    }
    assert coop_decisions.validate_decision_value(req, valid) == valid

    invalid = [
        {**valid, "command": ["coop", "question", "answer"]},
        {"answers": list(reversed(valid["answers"]))},
        {"answers": [{"question_id": 7, "answer": "one"}]},
        {"answers": [
            {"question_id": True, "answer": "one"},
            {"question_id": 9, "answer": "two"},
        ]},
        {"answers": [
            {"question_id": 7, "answer": ""},
            {"question_id": 9, "answer": "two"},
        ]},
        {"answers": [
            {"question_id": 7, "answer": "x" * 4097},
            {"question_id": 9, "answer": "two"},
        ]},
        {"answers": [
            {"question_id": 7, "answer": "one", "path": "x"},
            {"question_id": 9, "answer": "two"},
        ]},
    ]
    assert all(
        coop_decisions.validate_decision_value(req, value) is None
        for value in invalid
    )


def test_handoff_response_enforces_id_choice_and_conditional_reason():
    req = request("respond_handoff", handoff_id=12)
    accept = {"handoff_id": 12, "response": "accept", "reason": ""}
    decline = {
        "handoff_id": 12,
        "response": "decline",
        "reason": "I cannot preserve the requested boundary.",
    }
    assert coop_decisions.validate_decision_value(req, accept) == accept
    assert coop_decisions.validate_decision_value(req, decline) == decline
    for value in (
        {"handoff_id": 13, "response": "accept", "reason": ""},
        {"handoff_id": True, "response": "accept", "reason": ""},
        {"handoff_id": 12, "response": "maybe", "reason": ""},
        {"handoff_id": 12, "response": "decline", "reason": ""},
        {"handoff_id": 12, "response": "accept", "reason": "because"},
        {"handoff_id": 12, "response": "decline",
         "reason": "x" * 1025},
        {"handoff_id": 12, "response": "accept", "reason": "",
         "proof_ref": "event:1"},
    ):
        assert coop_decisions.validate_decision_value(req, value) is None


def test_mesh_questions_require_exact_runner_supplied_recipients():
    req = request(
        "compose_mesh_questions", recipients=("codex", "grok"))
    valid = {"questions": [
        {"recipient": "codex", "question": "Return one exact pong."},
        {"recipient": "grok", "question": "Return one exact pong."},
    ]}
    assert coop_decisions.validate_decision_value(req, valid) == valid
    for value in (
        {"questions": list(reversed(valid["questions"]))},
        {"questions": [valid["questions"][0]]},
        {"questions": [
            {"recipient": "codex", "question": "q"},
            {"recipient": "human", "question": "q"},
        ]},
        {"questions": [
            {"recipient": "codex", "question": "q", "command": "x"},
            {"recipient": "grok", "question": "q"},
        ]},
    ):
        assert coop_decisions.validate_decision_value(req, value) is None


def test_report_and_review_values_are_strict_and_bounded():
    report_req = request("compose_mesh_report_sections")
    report = {
        "what_was_asked": "A bounded connectivity mesh.",
        "method": "Each ordered pair used one board question.",
        "stop_boundaries": ["No research or repository edits."],
        "what_this_proves": "The six directed lanes completed.",
        "what_this_does_not_prove": "It does not benchmark quality.",
        "receipt_summary": (
            "Done: six lanes. Not done: research. "
            "Stop boundaries: connectivity only."
        ),
    }
    assert coop_decisions.validate_decision_value(report_req, report) == report
    assert coop_decisions.validate_decision_value(
        report_req, {**report, "path": "report.md"}) is None
    assert coop_decisions.validate_decision_value(
        report_req, {**report, "stop_boundaries": []}) is None
    assert coop_decisions.validate_decision_value(
        report_req, {**report, "method": "x" * 4097}) is None

    review_req = request("review_mesh_report", review_id=4)
    approve = {"review_id": 4, "verdict": "approve", "body": ""}
    changes = {
        "review_id": 4,
        "verdict": "changes",
        "body": "The Grok to Claude evidence cell is missing.",
    }
    assert coop_decisions.validate_decision_value(review_req, approve) == approve
    assert coop_decisions.validate_decision_value(review_req, changes) == changes
    for value in (
        {"review_id": 5, "verdict": "approve", "body": ""},
        {"review_id": 4, "verdict": "changes", "body": ""},
        {"review_id": 4, "verdict": "reject", "body": "no"},
        {"review_id": 4, "verdict": "approve", "body": "",
         "receipt_path": "x"},
    ):
        assert coop_decisions.validate_decision_value(review_req, value) is None


def test_generic_parser_accepts_only_valid_structured_payload():
    req = request("respond_handoff", handoff_id=12)
    valid = {"handoff_id": 12, "response": "accept", "reason": ""}
    assert coop_start.parse_structured_decision(
        envelope(valid), request=req) == valid
    assert coop_start.parse_structured_decision(
        json.dumps({"structuredOutput": valid}), request=req) == valid
    assert coop_start.parse_structured_decision(
        envelope({**valid, "path": "x"}), request=req) is None
    assert coop_start.parse_structured_decision("not json", request=req) is None


def test_generic_argv_promotes_only_claude_and_embeds_exact_schema():
    req = request("respond_handoff", handoff_id=12)
    argv = coop_start.structured_decision_argv(
        req, resolve=lambda name: f"/bin/{name}")
    # -p takes no positional: the prompt is delivered on stdin, never argv.
    assert argv[:2] == ["/bin/claude", "-p"]
    assert req.prompt not in argv
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema == req.json_schema
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv

    for provider in ("codex", "grok"):
        disabled = coop_decisions.make_decision_request(
            decision_kind="respond_handoff",
            provider=provider,
            agent_id=provider,
            item_id=39,
            action_fingerprint=FINGERPRINT,
            prompt="bounded",
            handoff_id=12,
        )
        assert coop_start.structured_decision_argv(
            disabled, resolve=lambda name: name) is None


def test_generic_invoke_isolated_claude_returns_value_and_usage(tmp_path):
    req = request("respond_handoff", handoff_id=12)
    value = {"handoff_id": 12, "response": "accept", "reason": ""}
    calls = {}
    observed_usage = []

    def runner(argv, *, cwd, capture_output, timeout, env, stdin):
        calls.update({
            "argv": argv,
            "cwd": cwd,
            "timeout": timeout,
            "env": env,
            "stdin": stdin.read().decode("utf-8"),
        })
        assert tmp_path.resolve() != type(tmp_path)(cwd).resolve()
        return SimpleNamespace(
            returncode=0,
            stdout=envelope(value).encode("utf-8"),
        )

    result = coop_start.invoke_structured_decision(
        request=req,
        cwd=str(tmp_path),
        timeout_s=30,
        runner=runner,
        resolve=lambda name: name,
        base_env={
            "PATH": "provider-path",
            "ANTHROPIC_API_KEY": "auth",
            "COOP_DB": "must-not-pass",
        },
        usage_callback=observed_usage.append,
    )

    assert dict(result.value) == value
    assert result.usage["uncached_input_tokens"] == 80
    assert result.usage["cache_write_input_tokens"] == 20
    assert result.usage["cached_input_tokens"] == 400
    assert result.usage["output_tokens"] == 10
    assert observed_usage == [dict(result.usage)]
    assert calls["timeout"] == 30.0
    assert calls["stdin"] == req.prompt
    assert req.prompt not in calls["argv"]
    assert calls["env"] == {
        "PATH": "provider-path",
        "ANTHROPIC_API_KEY": "auth",
    }
    assert not type(tmp_path)(calls["cwd"]).exists()


def test_generic_invoke_fails_soft_and_reports_unobserved_usage(tmp_path):
    req = request("respond_handoff", handoff_id=12)
    observed_usage = []
    assert coop_start.invoke_structured_decision(
        request=req,
        cwd=str(tmp_path),
        runner=lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout=b"{}"),
        resolve=lambda name: name,
        usage_callback=observed_usage.append,
    ) is None
    assert observed_usage == [{"usage_observation": "unobserved"}]


def handoff_action(handoff_id=12, item_id=39):
    return {
        "kind": "respond_handoff",
        "target_type": "handoff",
        "target_id": handoff_id,
        "item_id": item_id,
        "claim_id": None,
        "lease_seconds": 3600,
        "command": None,
        "required_inputs": ["handoff_response"],
        "choices": [
            {
                "kind": "accept_handoff",
                "command": [
                    *coopdb.CLI_ARGV,
                    "handoff", "accept", "--id", str(handoff_id),
                    "--intent", f"accept handoff {handoff_id}",
                    "--lease-seconds", "3600",
                ],
                "required_inputs": [],
            },
            {
                "kind": "decline_handoff",
                "command": [
                    *coopdb.CLI_ARGV,
                    "handoff", "decline", "--id", str(handoff_id),
                    "--reason", "{reason}",
                ],
                "required_inputs": ["reason"],
            },
        ],
    }


def test_structured_action_eligibility_rejects_malformed_command_grammar():
    answer = {
        "kind": "answer_question",
        "target_type": "question",
        "target_id": 7,
        "item_id": 39,
        "claim_id": 17,
        "command": [
            *coopdb.CLI_ARGV,
            "question", "answer", "--claim", "17",
            "--answer", "{answer}",
        ],
        "required_inputs": ["answer"],
        "choices": [],
    }
    assert coop_autonomous.structured_decision_kind(answer) == (
        "answer_questions")
    assert coop_autonomous.structured_decision_kind(handoff_action()) == (
        "respond_handoff")

    malformed = handoff_action()
    malformed["choices"][1]["command"][-1] = "model-supplied"
    assert coop_autonomous.structured_decision_kind(malformed) is None
    malformed = handoff_action()
    malformed["choices"][0]["command"][
        malformed["choices"][0]["command"].index("12")
    ] = "13"
    assert coop_autonomous.structured_decision_kind(malformed) is None
    malformed = handoff_action()
    malformed["choices"].append({
        "kind": "invented",
        "command": ["delete", "everything"],
    })
    assert coop_autonomous.structured_decision_kind(malformed) is None


def test_decision_prompts_carry_only_bounded_board_facts():
    answer_prompt = coop_prompt_cache.structured_questions_decision_prompt(
        [{
            "question_id": 7,
            "exact_question": "Return the exact pong.",
            "asked_by_agent": "codex",
            "assigned_to_agent": "claude",
        }],
        {"id": 39, "title": "mesh"},
    )
    assert "Return the exact pong." in answer_prompt
    assert "question_id" in answer_prompt
    assert "shell" not in answer_prompt.lower()

    handoff_prompt = coop_prompt_cache.structured_handoff_decision_prompt(
        {
            "handoff_id": 12,
            "item_id": 39,
            "from_agent": "codex",
            "to_agent": "claude",
            "reason": "rotate",
            "summary": "two pings complete",
            "completed_work": "codex pairs",
            "remaining_work": "claude pairs",
            "risks": "none",
            "suggested_next_action": "continue bounded mesh",
            "proof_references": '[{"type":"event","id":7}]',
            "claim_id": 999,
            "from_session": "secret-session",
        },
        {"id": 39, "title": "mesh"},
    )
    assert "two pings complete" in handoff_prompt
    assert "secret-session" not in handoff_prompt
    assert '"claim_id"' not in handoff_prompt
    assert "accept" in handoff_prompt and "decline" in handoff_prompt

    mesh_prompt = coop_prompt_cache.structured_mesh_questions_decision_prompt(
        sender="claude",
        recipients=("codex", "grok"),
        item={"id": 39, "title": "mesh"},
    )
    assert '"sender": "claude"' in mesh_prompt
    assert '"recipients": ["codex", "grok"]' in mesh_prompt
    assert "one sentence" in mesh_prompt
    assert "claim_id" not in mesh_prompt
    assert "session" not in mesh_prompt.lower()

    report_prompt = coop_prompt_cache.structured_mesh_report_decision_prompt(
        sender="grok",
        exchanges=({
            "sender": "claude",
            "recipient": "codex",
            "question": "Ping Codex.",
            "answer": "Acknowledged.",
        },),
        item={"id": 39, "title": "mesh"},
    )
    assert "Ping Codex." in report_prompt
    assert "Acknowledged." in report_prompt
    assert "what_this_does_not_prove" in report_prompt
    assert "report_path" not in report_prompt
    assert "proof_ref" not in report_prompt
    assert "claim_id" not in report_prompt
    assert "session" not in report_prompt.lower()


def test_dispatch_promotes_claude_decisions_only_when_opted_in():
    action = handoff_action()
    candidate = coop_action_scheduler.ActionCandidate(
        agent="claude", hint="respond_handoff", action=action)
    default = coop_autonomous.dispatch_profile(candidate)
    promoted = coop_autonomous.dispatch_profile(
        candidate, structured_decisions=True)
    assert (default.workspace_surface, default.execution_mode) == (
        "read", "tool_turn")
    assert (promoted.workspace_surface, promoted.execution_mode) == (
        "none", "isolated_structured_decision")

    for provider in ("codex", "grok"):
        other = coop_action_scheduler.ActionCandidate(
            agent=provider, hint="respond_handoff", action=action)
        profile = coop_autonomous.dispatch_profile(
            other, structured_decisions=True)
        assert (profile.workspace_surface, profile.execution_mode) == (
            "read", "tool_turn")
    failed = coop_autonomous.dispatch_profile(
        candidate,
        structured_decisions=True,
        failed_isolated={("claude", promoted.action_fingerprint)},
    )
    assert (failed.workspace_surface, failed.execution_mode) == (
        "read", "tool_turn")


def test_runner_renders_only_action_owned_answer_and_handoff_commands():
    answer_action = {
        "kind": "answer_question",
        "target_type": "question",
        "target_id": 7,
        "item_id": 39,
        "claim_id": 17,
        "command": [
            *coopdb.CLI_ARGV,
            "question", "answer", "--claim", "17",
            "--answer", "{answer}",
        ],
        "required_inputs": ["answer"],
        "choices": [],
    }
    answer_request = coop_decisions.make_decision_request(
        decision_kind="answer_questions",
        provider="claude",
        agent_id="claude",
        item_id=39,
        action_fingerprint=coop_action_scheduler.action_fingerprint(
            answer_action),
        prompt="bounded",
        question_ids=(7,),
    )
    answer = {"answers": [{"question_id": 7, "answer": "pong"}]}
    rendered = coop_autonomous.structured_decision_command(
        answer_action, answer_request, answer)
    assert rendered[-2:] == ["--answer", "pong"]

    action = handoff_action()
    handoff_request = coop_decisions.make_decision_request(
        decision_kind="respond_handoff",
        provider="claude",
        agent_id="claude",
        item_id=39,
        action_fingerprint=coop_action_scheduler.action_fingerprint(action),
        prompt="bounded",
        handoff_id=12,
    )
    accepted = coop_autonomous.structured_decision_command(
        action,
        handoff_request,
        {"handoff_id": 12, "response": "accept", "reason": ""},
    )
    declined = coop_autonomous.structured_decision_command(
        action,
        handoff_request,
        {"handoff_id": 12, "response": "decline", "reason": "outside scope"},
    )
    assert accepted[-6:] == [
        "--id", "12", "--intent", "accept handoff 12",
        "--lease-seconds", "3600",
    ]
    assert declined[-4:] == ["--id", "12", "--reason", "outside scope"]

    stale = coop_decisions.make_decision_request(
        decision_kind="respond_handoff",
        provider="claude",
        agent_id="claude",
        item_id=39,
        action_fingerprint="f" * 64,
        prompt="bounded",
        handoff_id=12,
    )
    assert coop_autonomous.structured_decision_command(
        action,
        stale,
        {"handoff_id": 12, "response": "accept", "reason": ""},
    ) is None
