"""Content-free prompt identity and provider cache-usage normalization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from agent_coop import coopdb


PROMPT_PREFIX_VERSION = "coop-bootstrap-v4"
PROMPT_PREFIX = (
    "You are bound to an Agent Co-op autonomous session. "
    "The board is the only operative channel. "
    "Do not start or bind another session. "
    "When `COOP_ITEM_ID` is set, do not act on another item. "
    "Your prompt usually ends with a hydrated board snapshot. "
    "It contains this turn's `status --json` payload, `next_action`, target "
    "item, and required packets. "
    "When the snapshot is present, execute `next_action.command` now. "
    "Do not rerun status, reread the item, or sweep the inbox first. "
    "Supply only the named `required_inputs` or select one listed choice. "
    "Read only the named board object when judgment is required. "
    "Perform substantive work only while you hold the routed claim. "
    "Use board commands for every contract, question, handoff, decision, "
    "receipt, review, and completion. "
    "Never author or decide for a named peer. "
    "Name known limitations and stop boundaries in receipts and reviews. "
    "After a refusal, read `reason_code`, `evidence`, and "
    "`legal_next_actions`. "
    "Do not repeat the refused command unchanged. "
    "If the offered action is `idle`, stop the turn. "
    "Otherwise, execute one offered repair. "
    "If the same reason repeats, route a `needs-input` question to the named "
    "peer or open a plan huddle. "
    "If no repair exists, run `coop status --json`. "
    "When no snapshot is present, run that status command first. "
    "After a truncation note, read the named board object. "
    "Never ask `human` or use `admin` during a run. "
    "Stop when `next_action.kind` is `idle`."
)
_PROMPT_PREFIX_BYTES = PROMPT_PREFIX.encode("utf-8")
PROMPT_PREFIX_SHA256 = hashlib.sha256(_PROMPT_PREFIX_BYTES).hexdigest()
TOKEN_EFFICIENT_PROMPT_PREFIX_VERSION = "coop-bootstrap-v4-token-efficient-v1"
TOKEN_EFFICIENT_EXIT_INSTRUCTION = (
    "After your final canonical board write succeeds, do not run status, "
    "help, inbox, or history and do not produce a narrative summary. Return "
    "the shortest provider completion marker and exit so the runner can "
    "finalize the session."
)
TOKEN_EFFICIENT_PROMPT_PREFIX = (
    PROMPT_PREFIX + " " + TOKEN_EFFICIENT_EXIT_INSTRUCTION
)
_TOKEN_EFFICIENT_PROMPT_PREFIX_BYTES = (
    TOKEN_EFFICIENT_PROMPT_PREFIX.encode("utf-8")
)
TOKEN_EFFICIENT_PROMPT_PREFIX_SHA256 = hashlib.sha256(
    _TOKEN_EFFICIENT_PROMPT_PREFIX_BYTES
).hexdigest()

HYDRATION_HEADER = "\n\n--- BOARD SNAPSHOT (spawn-time cache) ---\n"
HYDRATION_NOTE = (
    "Snapshot taken when this turn was spawned; the board may have "
    "advanced since. The board stays canonical and CLI rejections are "
    "authoritative. After a refusal, follow its `legal_next_actions`. If no "
    "repair exists, run `coop status --json`.\n"
)
HYDRATION_MAX_BYTES = 24000
# The crib embeds the absolute interpreter path (coopdb.CLI_ARGV) up to
# three times: routed argv plus the needs-input and handoff follow-ons. A
# MAX_PATH-length interpreter costs about 800 bytes of that, so 1024 was
# only enough for a short `python` prefix and dropped the crib on ordinary
# pipx installs (tests/test_speed_hydration.py pins the MAX_PATH case).
COMMAND_CRIB_MAX_BYTES = 2048

USAGE_COUNTER_FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "model_calls",
)
WEIGHTED_USAGE_FIELDS = (
    "uncached_input_tokens",
    "cache_write_input_tokens",
    "cached_input_tokens",
    "output_tokens",
)
USAGE_TRACE_FIELDS = (
    "model_id",
    *USAGE_COUNTER_FIELDS,
    "usage_observation",
)

# Per-section byte budgets (rule: never truncate the serialized packet
# mid-section - an oversized OPTIONAL section is omitted whole, with
# a note naming the board read that recovers it; the mandatory action
# envelope is never dropped). Budgets sum comfortably under
# HYDRATION_MAX_BYTES, which survives as the total-size contract.
HYDRATION_SECTION_BUDGETS = {
    "action": None,          # mandatory - envelopes are code-authored/small
    "answered": 3000,
    "questions": 3000,
    "handoff": 3000,
    "review": 5000,
    "item": 4000,
    "status": 5000,
}
TOKEN_EFFICIENT_SECTION_BUDGETS = {
    "action": None,
    "command_crib": COMMAND_CRIB_MAX_BYTES,
    "answered": 3000,
    "questions": 3000,
    "handoff": 3000,
    "review": 5000,
    "receipt_evidence": 5000,
    "item": 8192,
    "status": 2500,
}
_SECTION_OMITTED = (
    "[%s omitted - exceeded its snapshot budget; read it from the board "
    "with %s]"
)


def _valid_event_proof_ref(value):
    if not isinstance(value, str):
        return False
    prefix, separator, raw_id = value.partition(":")
    return (
        separator == ":"
        and prefix == "event"
        and raw_id.isdigit()
        and int(raw_id) > 0
    )


def command_crib(action, *, peer_agents=(), receipt_evidence=None):
    """Render exact board-derived argv grammar for one routed action."""
    if not isinstance(action, Mapping):
        return None
    kind = action.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    crib = {"kind": kind}
    command = action.get("command")
    if isinstance(command, (list, tuple)) and command:
        crib["argv"] = list(command)
    required_inputs = action.get("required_inputs")
    if isinstance(required_inputs, (list, tuple)) and required_inputs:
        crib["required_inputs"] = list(required_inputs)
    choices = []
    for choice in action.get("choices") or ():
        if not isinstance(choice, Mapping):
            continue
        choice_kind = choice.get("kind")
        choice_command = choice.get("command")
        if (
            not isinstance(choice_kind, str)
            or not choice_kind
            or not isinstance(choice_command, (list, tuple))
            or not choice_command
        ):
            continue
        choices.append({
            "kind": choice_kind,
            "argv": list(choice_command),
            "required_inputs": list(choice.get("required_inputs") or ()),
        })
    if choices:
        crib["choices"] = choices

    claim_id = action.get("claim_id")
    if (
        kind == "continue_task"
        and action.get("target_type") == "claim"
        and isinstance(claim_id, int)
        and not isinstance(claim_id, bool)
        and claim_id > 0
    ):
        peers = sorted({
            peer.strip()
            for peer in peer_agents or ()
            if isinstance(peer, str) and peer.strip()
        })
        if peers:
            crib["peer_agents"] = peers
        claim = str(claim_id)
        crib["follow_on"] = {
            "needs_input": [
                *coopdb.CLI_ARGV, "needs-input", "--claim", claim,
                "--to", "{peer_agent}",
                "--question", "{exact_question}",
            ],
            "handoff_create": [
                *coopdb.CLI_ARGV, "handoff", "create", "--claim", claim,
                "--to", "{peer_agent}",
                "--reason", "{reason}",
                "--summary", "{summary}",
                "--completed", "{completed_work}",
                "--remaining", "{remaining_work}",
                "--risks", "{risks}",
                "--next-action", "{suggested_next_action}",
                "--proof-ref", "{proof_ref}",
            ],
        }
        refs = sorted({
            row.get("proof_ref")
            for row in receipt_evidence or ()
            if isinstance(row, Mapping)
            and _valid_event_proof_ref(row.get("proof_ref"))
        })
        crib["proof_references"] = refs
        crib["proof_ref_rule"] = (
            "repeat --proof-ref once per value; use only listed event refs "
            "or file:<absolute-path>"
        )
    return crib


def _compact_status(status, action):
    if not isinstance(status, Mapping):
        return None
    item_id = action.get("item_id") if isinstance(action, Mapping) else None

    def project_rows(rows, fields):
        result = []
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            if (
                item_id is not None
                and row.get("item_id") not in {None, item_id}
            ):
                continue
            result.append({
                field: row.get(field)
                for field in fields
                if row.get(field) is not None
            })
        return result

    agent = status.get("agent")
    session = status.get("session")
    projection = {
        "agent": ({
            field: agent.get(field)
            for field in ("agent_id", "provider")
            if isinstance(agent, Mapping) and agent.get(field) is not None
        } if isinstance(agent, Mapping) else agent),
        "session": ({
            field: session.get(field)
            for field in ("session_id", "status", "provider")
            if isinstance(session, Mapping) and session.get(field) is not None
        } if isinstance(session, Mapping) else session),
        "claims": project_rows(status.get("claims"), (
            "claim_id", "item_id", "lane", "intent", "progress_stale",
        )),
        "owned_items": project_rows(status.get("owned_items"), (
            "item_id", "status", "owner", "next_actor", "labels", "review",
        )),
        "unread": status.get("unread") or {},
        "resume_grace": project_rows(status.get("resume_grace"), (
            "item_id", "expires_at",
        )),
        "stale": project_rows(status.get("stale"), (
            "claim_id", "item_id", "lane", "reclaimable",
        )),
        "warnings": list(status.get("warnings") or ()),
    }
    return {
        key: value
        for key, value in projection.items()
        if value not in (None, [], {})
    }


def _token_efficient_hydrated_prompt(
        *, action, item, status, questions, answered_questions,
        handoff, review, receipt_evidence, peer_agents):
    crib = command_crib(
        action,
        peer_agents=peer_agents,
        receipt_evidence=receipt_evidence,
    )
    compact_status = _compact_status(status, action)
    item_id = action.get("item_id") if isinstance(action, Mapping) else None
    if item_id is None and isinstance(item, Mapping):
        item_id = item.get("id")
    item_read = (
        f"coop item show {item_id} --packet --json"
        if item_id is not None
        else "coop status --json"
    )
    ordered = (
        ("action", "next_action (already derived for this turn - execute "
         "this):", action if isinstance(action, dict) else None,
         "coop status --json", False),
        ("command_crib", "command_crib (exact argv; do not run help):",
         crib, "coop status --json", True),
        ("answered", "questions you asked that are now answered since your "
         "lane last closed (use the answers directly, no inbox or history "
         "reads):", list(answered_questions) if answered_questions else None,
         "coop inbox", False),
        ("questions", "open questions addressed to you (answer content is "
         "your judgment; one turn may answer all of them):",
         list(questions) if questions else None,
         "coop inbox", False),
        ("handoff", "pending handoff addressed to you (full handoff row - "
         "decide accept or decline from this):",
         handoff if isinstance(handoff, dict) else None,
         item_read, False),
        ("review", "review packet (review row plus its receipt - the "
         "verdict is your judgment):",
         review if isinstance(review, dict) else None,
         item_read, False),
        ("receipt_evidence", "receipt evidence (answered question rows with "
         "valid proof references):",
         list(receipt_evidence) if receipt_evidence else None,
         item_read, False),
        ("item", "target item:", item if isinstance(item, dict) else None,
         item_read, False),
        ("status", "compact status projection (next_action intentionally "
         "omitted):", compact_status,
         "coop status --json", False),
    )
    sections = []
    prefix = TOKEN_EFFICIENT_PROMPT_PREFIX

    def fits(candidate_sections):
        body = HYDRATION_NOTE + "\n" + "\n\n".join(candidate_sections)
        candidate = prefix + HYDRATION_HEADER + body
        return len(candidate.encode("utf-8")) <= HYDRATION_MAX_BYTES

    for key, label, payload, read_back, compact in ordered:
        if payload is None:
            continue
        rendered = json.dumps(
            payload,
            default=str,
            sort_keys=True,
            **({"separators": (",", ":")} if compact else {}),
        )
        budget = (
            COMMAND_CRIB_MAX_BYTES
            if key == "command_crib"
            else TOKEN_EFFICIENT_SECTION_BUDGETS[key]
        )
        omitted = _SECTION_OMITTED % (key, read_back)
        if budget is not None and len(rendered.encode("utf-8")) > budget:
            if fits([*sections, omitted]):
                sections.append(omitted)
            continue
        section = label + "\n" + rendered
        if fits([*sections, section]):
            sections.append(section)
        elif key == "action":
            # The complete code-authored action is the execution authority and
            # is never truncated. Ordinary envelopes are bounded well below
            # the total limit; an impossible oversized envelope stays visible.
            sections.append(section)
        elif fits([*sections, omitted]):
            sections.append(omitted)
    if not sections:
        return prefix
    body = HYDRATION_NOTE + "\n" + "\n\n".join(sections)
    return prefix + HYDRATION_HEADER + body


def hydrated_prompt(*, action=None, item=None, status=None, questions=None,
                    answered_questions=None, handoff=None, review=None,
                    receipt_evidence=None, peer_agents=(),
                    token_efficient=False):
    """Content-free prefix plus a bounded spawn-time board snapshot.

    Assembly is action-first and per-section budgeted: the routed
    ``next_action`` envelope always leads and is never truncated, causal
    wake rows (answers to this agent's own questions, addressed open
    questions, the targeted handoff or review+receipt row) come next, and
    the bulky item/status context comes last. Every section is serialized
    independently - an oversized optional section is dropped whole with a
    read-back note, so the packet never contains sliced/invalid JSON. It
    carries no operative content that is not on the board.
    """
    if token_efficient:
        return _token_efficient_hydrated_prompt(
            action=action,
            item=item,
            status=status,
            questions=questions,
            answered_questions=answered_questions,
            handoff=handoff,
            review=review,
            receipt_evidence=receipt_evidence,
            peer_agents=peer_agents,
        )
    item_id = action.get("item_id") if isinstance(action, Mapping) else None
    if item_id is None and isinstance(item, Mapping):
        item_id = item.get("id")
    item_read = (
        f"coop item show {item_id} --packet --json"
        if item_id is not None
        else "coop status --json"
    )
    ordered = (
        ("action", "next_action (already derived for this turn - execute "
         "this):", action if isinstance(action, dict) else None,
         "coop status --json"),
        ("answered", "questions you asked that are now answered since your "
         "lane last closed (use the answers directly, no inbox or history "
         "reads):", list(answered_questions) if answered_questions else None,
         "coop inbox"),
        ("questions", "open questions addressed to you (answer content is "
         "your judgment; one turn may answer all of them):",
         list(questions) if questions else None,
         "coop inbox"),
        ("handoff", "pending handoff addressed to you (full handoff row - "
         "decide accept or decline from this):",
         handoff if isinstance(handoff, dict) else None,
         item_read),
        ("review", "review packet (review row plus its receipt - the "
         "verdict is your judgment):",
         review if isinstance(review, dict) else None,
         item_read),
        ("item", "target item:", item if isinstance(item, dict) else None,
         item_read),
        ("status", "status --json (already run for this turn):",
         status if isinstance(status, dict) else None,
         "coop status --json"),
    )
    sections = []
    for key, label, payload, read_back in ordered:
        if payload is None:
            continue
        rendered = json.dumps(payload, default=str, sort_keys=True)
        budget = HYDRATION_SECTION_BUDGETS[key]
        if budget is not None and len(rendered.encode("utf-8")) > budget:
            sections.append(_SECTION_OMITTED % (key, read_back))
            continue
        sections.append(label + "\n" + rendered)
    if not sections:
        return PROMPT_PREFIX
    body = HYDRATION_NOTE + "\n" + "\n\n".join(sections)
    return PROMPT_PREFIX + HYDRATION_HEADER + body


def structured_answer_prompt(question, item=None):
    """One-shot instruction for a schema-constrained answer (no tools).

    Board-derived facts only: the routed question row plus item identity.
    The answer content stays the model's judgment.
    """
    where = ""
    if isinstance(item, dict):
        where = " on work item %s (%r)" % (
            item.get("id"),
            str(item.get("title") or ""),
        )
    return (
        "You are agent %s in an Agent Co-op multi-agent session. Peer "
        "agent %s asked you this exact question%s:\n\n%s\n\n"
        "Return only the JSON answer object for question_id %s. The "
        "answer text is your judgment as the addressed peer - answer "
        "the question directly and exactly."
        % (
            question.get("assigned_to_agent"),
            question.get("asked_by_agent"),
            where,
            question.get("exact_question"),
            question.get("question_id"),
        )
    )


def prompt_trace_details(
    prompt,
    *,
    provider,
    provider_hint=False,
):
    """Return bounded metadata only when ``prompt`` is prefix-anchored."""
    identity = None
    for prefix, version, digest, byte_count in (
        (
            TOKEN_EFFICIENT_PROMPT_PREFIX,
            TOKEN_EFFICIENT_PROMPT_PREFIX_VERSION,
            TOKEN_EFFICIENT_PROMPT_PREFIX_SHA256,
            len(_TOKEN_EFFICIENT_PROMPT_PREFIX_BYTES),
        ),
        (
            PROMPT_PREFIX,
            PROMPT_PREFIX_VERSION,
            PROMPT_PREFIX_SHA256,
            len(_PROMPT_PREFIX_BYTES),
        ),
    ):
        if prompt == prefix:
            identity = (prefix, version, digest, byte_count, 0)
            break
        if isinstance(prompt, str) and prompt.startswith(
                prefix + HYDRATION_HEADER):
            identity = (
                prefix,
                version,
                digest,
                byte_count,
                len(prompt[len(prefix):].encode("utf-8")),
            )
            break
    if identity is None:
        return {"prompt_cache_mode": "unobserved"}
    _prefix, version, digest, byte_count, hydration_bytes = identity
    mode = (
        "provider_hint"
        if provider == "claude" and provider_hint
        else "provider_managed"
    )
    details = {
        "prompt_prefix_version": version,
        "prompt_prefix_sha256": digest,
        "prompt_prefix_bytes": byte_count,
        "prompt_cache_mode": mode,
    }
    if hydration_bytes:
        details["prompt_hydration_bytes"] = hydration_bytes
    return details


def _decision_item_identity(item):
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    if isinstance(item_id, bool) or not isinstance(item_id, int):
        return None
    return {
        "item_id": item_id,
        "title": str(item.get("title") or ""),
    }


def structured_questions_decision_prompt(questions, item=None):
    """Bounded facts for one or more addressed question answers."""
    rows = []
    for question in questions or ():
        if not isinstance(question, Mapping):
            continue
        rows.append({
            "question_id": question.get("question_id"),
            "asked_by_agent": question.get("asked_by_agent"),
            "assigned_to_agent": question.get("assigned_to_agent"),
            "exact_question": question.get("exact_question"),
        })
    packet = {
        "decision_kind": "answer_questions",
        "item": _decision_item_identity(item),
        "questions": rows,
    }
    return (
        "You are the addressed peer in an Agent Co-op session. Answer each "
        "exact question directly using your judgment. Preserve the supplied "
        "question order and IDs. Return only the schema-constrained JSON "
        "value; do not add identifiers or fields.\n\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )


def structured_handoff_decision_prompt(handoff, item=None):
    """Bounded handoff evidence for an accept-or-decline judgment."""
    if not isinstance(handoff, Mapping):
        packet_handoff = None
    else:
        packet_handoff = {
            field: handoff.get(field)
            for field in (
                "handoff_id",
                "item_id",
                "from_agent",
                "to_agent",
                "reason",
                "summary",
                "completed_work",
                "remaining_work",
                "risks",
                "suggested_next_action",
                "proof_references",
            )
        }
    packet = {
        "decision_kind": "respond_handoff",
        "item": _decision_item_identity(item),
        "handoff": packet_handoff,
    }
    return (
        "You are the named handoff recipient in an Agent Co-op session. "
        "Choose accept when you can continue within the stated contract, or "
        "decline with one concise reason when you cannot. For accept, return "
        "an empty reason. Return only the schema-constrained JSON value.\n\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )


def structured_mesh_questions_decision_prompt(
        *, sender, recipients, item=None):
    """Minimal facts for one sender's fixed-recipient mesh pings."""
    packet = {
        "decision_kind": "compose_mesh_questions",
        "item": _decision_item_identity(item),
        "sender": str(sender),
        "recipients": [str(recipient) for recipient in recipients],
    }
    return (
        "Write one direct ping of one sentence from the named sender to each "
        "recipient, in the supplied recipient order. Each ping should ask "
        "only for a concise acknowledgement identifying both endpoints; "
        "do not research, inspect files, or discuss orchestration. Return "
        "only the schema-constrained JSON value.\n\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )


def structured_mesh_report_decision_prompt(
        *, sender, exchanges, item=None):
    """Evidence-only facts for bounded narrative report sections."""
    rows = []
    for exchange in exchanges or ():
        if not isinstance(exchange, Mapping):
            continue
        rows.append({
            key: exchange.get(key)
            for key in ("sender", "recipient", "question", "answer")
        })
    packet = {
        "decision_kind": "compose_mesh_report_sections",
        "item": _decision_item_identity(item),
        "sender": str(sender),
        "exchanges": rows,
        "required_sections": [
            "what_was_asked",
            "method",
            "stop_boundaries",
            "what_this_proves",
            "what_this_does_not_prove",
            "receipt_summary",
        ],
    }
    return (
        "Summarize only the supplied completed exchanges. The runner owns "
        "the report structure, table, path, hashes, proof references, and "
        "board writes. Since every supplied pair is answered, return exactly "
        "['none'] for stop_boundaries and state none for both Not done and "
        "Stop boundaries in the receipt summary. Do not claim a substantive "
        "work benchmark. Return only the schema-constrained JSON value.\n\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )


def structured_mesh_review_decision_prompt(
        *, reviewer, report_text, item=None):
    """Bounded, authority-free packet for the independent mesh reviewer."""
    if not isinstance(report_text, str) or not report_text.strip():
        raise ValueError("mesh review requires report text")
    packet = {
        "decision_kind": "review_mesh_report",
        "item": _decision_item_identity(item),
        "reviewer": str(reviewer),
        "mechanical_validation": (
            "passed: six peer-authored exchanges, two accepted handoffs, "
            "report hash, receipt, proof references, and board events agree"
        ),
        "report": report_text,
    }
    return (
        "Independently review only the supplied validated liveness report. "
        "Approve when its narrative accurately describes the six completed "
        "routes and stays within the stated non-benchmark scope. Request "
        "changes only for a concrete unsupported or misleading claim, with "
        "one concise actionable reason. The runner owns identifiers and all "
        "board writes. Return only the schema-constrained JSON value.\n\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    )


def _counter(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        return None
    return value


def _model_id(value):
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _finish_usage(result, *, model_id=None, model_calls=None):
    normalized = dict(result)
    model = _model_id(model_id)
    if model is not None:
        normalized["model_id"] = model
    calls = _counter(model_calls)
    if calls is not None:
        normalized["model_calls"] = calls
    observed = any(
        field in normalized
        for field in USAGE_COUNTER_FIELDS
        if field != "model_calls"
    )
    if not observed:
        observation = "unobserved"
    elif (
        _model_id(normalized.get("model_id")) is not None
        and all(field in normalized for field in WEIGHTED_USAGE_FIELDS)
    ):
        observation = "complete"
    else:
        observation = "partial"
    normalized["usage_observation"] = observation
    return normalized


def _derive_uncached(result):
    logical = result.get("input_tokens")
    cached = result.get("cached_input_tokens")
    cache_write = result.get("cache_write_input_tokens")
    if not all(
        _counter(value) is not None
        for value in (logical, cached, cache_write)
    ):
        return
    uncached = logical - cached - cache_write
    if uncached >= 0:
        result["uncached_input_tokens"] = uncached


def normalize_codex_usage(value, *, model_id=None, model_calls=None):
    """Normalize one Codex app-server cumulative usage breakdown."""
    if not isinstance(value, Mapping):
        return {}
    fields = {
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens",
        "totalTokens": "total_tokens",
    }
    result = {}
    for source, target in fields.items():
        counter = _counter(value.get(source))
        if counter is not None:
            result[target] = counter
    if "cacheWriteInputTokens" not in value and result:
        # Codex app-server's v2 schema defines an omitted cache-write field
        # as zero. Other providers do not inherit this default.
        result["cache_write_input_tokens"] = 0
    elif "cacheWriteInputTokens" in value:
        cache_write = _counter(value.get("cacheWriteInputTokens"))
        if cache_write is not None:
            result["cache_write_input_tokens"] = cache_write
    _derive_uncached(result)
    if not result:
        return {}
    return _finish_usage(
        result,
        model_id=model_id,
        model_calls=model_calls,
    )


def _single_model_id(envelope):
    direct = _model_id(envelope.get("model"))
    if direct is not None:
        return direct
    for key in ("modelUsage", "model_usage"):
        per_model = envelope.get(key)
        if not isinstance(per_model, Mapping):
            continue
        models = [
            model
            for raw in per_model
            if (model := _model_id(raw)) is not None
        ]
        if len(models) == 1:
            return models[0]
    return None


def _normalize_claude_usage(envelope):
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    result = {}
    uncached = _counter(usage.get("input_tokens"))
    cache_write = _counter(usage.get("cache_creation_input_tokens"))
    cached = _counter(usage.get("cache_read_input_tokens"))
    output = _counter(usage.get("output_tokens"))
    for key, value in (
        ("uncached_input_tokens", uncached),
        ("cache_write_input_tokens", cache_write),
        ("cached_input_tokens", cached),
        ("output_tokens", output),
    ):
        if value is not None:
            result[key] = value
    if all(value is not None for value in (uncached, cache_write, cached)):
        result["input_tokens"] = uncached + cache_write + cached
    total = _counter(usage.get("total_tokens"))
    if total is not None:
        result["total_tokens"] = total
    elif result.get("input_tokens") is not None and output is not None:
        result["total_tokens"] = result["input_tokens"] + output
    return _finish_usage(
        result,
        model_id=_single_model_id(envelope),
        model_calls=envelope.get("num_turns"),
    )


def _first_counter(mapping, *keys):
    for key in keys:
        if key not in mapping:
            continue
        value = _counter(mapping.get(key))
        if value is not None:
            return value
    return None


def _normalize_grok_usage(envelope):
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    result = {}
    logical = _first_counter(usage, "input_tokens", "prompt_tokens")
    output = _first_counter(
        usage,
        "output_tokens",
        "completion_tokens",
    )
    cached = _first_counter(
        usage,
        "cached_input_tokens",
        "cache_read_input_tokens",
    )
    prompt_details = usage.get("prompt_tokens_details")
    if cached is None and isinstance(prompt_details, Mapping):
        cached = _counter(prompt_details.get("cached_tokens"))
    cache_write = _first_counter(
        usage,
        "cache_write_input_tokens",
        "cache_creation_input_tokens",
    )
    for key, value in (
        ("input_tokens", logical),
        ("cached_input_tokens", cached),
        ("cache_write_input_tokens", cache_write),
        ("output_tokens", output),
    ):
        if value is not None:
            result[key] = value
    explicit_uncached = _counter(usage.get("uncached_input_tokens"))
    if explicit_uncached is not None:
        result["uncached_input_tokens"] = explicit_uncached
    else:
        _derive_uncached(result)
    total = _counter(usage.get("total_tokens"))
    if total is not None:
        result["total_tokens"] = total
    elif logical is not None and output is not None:
        result["total_tokens"] = logical + output
    reasoning = _counter(usage.get("reasoning_output_tokens"))
    if reasoning is not None:
        result["reasoning_output_tokens"] = reasoning
    return _finish_usage(
        result,
        model_id=_single_model_id(envelope),
        model_calls=(
            envelope.get("model_calls")
            if "model_calls" in envelope
            else envelope.get("num_turns")
        ),
    )


def normalize_cli_usage(provider, envelope):
    """Normalize one Claude/Grok headless JSON result envelope."""
    if not isinstance(envelope, Mapping):
        return {}
    if provider == "claude":
        return _normalize_claude_usage(envelope)
    if provider == "grok":
        return _normalize_grok_usage(envelope)
    return {}


def codex_usage_delta(total, baseline):
    """Return one turn's usage from two observed cumulative totals."""
    if not isinstance(total, Mapping) or not isinstance(baseline, Mapping):
        return {}
    result = {}
    for key in USAGE_COUNTER_FIELDS:
        current = total.get(key)
        previous = baseline.get(key)
        if (
            _counter(current) is None
            or _counter(previous) is None
        ):
            continue
        if current < previous:
            return {}
        result[key] = current - previous
    if not result:
        return {}
    current_model = _model_id(total.get("model_id"))
    previous_model = _model_id(baseline.get("model_id"))
    if (
        current_model is not None
        and previous_model is not None
        and current_model != previous_model
    ):
        return {}
    return _finish_usage(
        result,
        model_id=current_model or previous_model,
    )


__all__ = [
    "COMMAND_CRIB_MAX_BYTES",
    "HYDRATION_HEADER",
    "HYDRATION_MAX_BYTES",
    "PROMPT_PREFIX",
    "PROMPT_PREFIX_SHA256",
    "PROMPT_PREFIX_VERSION",
    "TOKEN_EFFICIENT_EXIT_INSTRUCTION",
    "TOKEN_EFFICIENT_PROMPT_PREFIX",
    "TOKEN_EFFICIENT_PROMPT_PREFIX_SHA256",
    "TOKEN_EFFICIENT_PROMPT_PREFIX_VERSION",
    "USAGE_COUNTER_FIELDS",
    "USAGE_TRACE_FIELDS",
    "WEIGHTED_USAGE_FIELDS",
    "codex_usage_delta",
    "command_crib",
    "hydrated_prompt",
    "normalize_cli_usage",
    "normalize_codex_usage",
    "prompt_trace_details",
    "structured_answer_prompt",
    "structured_handoff_decision_prompt",
    "structured_mesh_questions_decision_prompt",
    "structured_mesh_report_decision_prompt",
    "structured_mesh_review_decision_prompt",
    "structured_questions_decision_prompt",
]
