#!/usr/bin/env python3
"""coop_autonomous - wake-driven autonomous runner (the product).

Agents coordinate through the board without a human driving each turn. The
watcher sends a content-free wake category. The runner supplies a stable
operating loop and can append a snapshot copied from the board. It injects no
off-board coordination content. The human seeds a goal or complete contract,
launches the run, and returns for end review. Peers answer questions and recover
wedges during the run.

How it differs from the round-robin helpers in coop_start:
  * WAKE-DRIVEN - an agent takes a turn only when the board says it has
    actionable work (coop_watcher / coopdb._derive_next_action != 'idle'),
    not every round regardless.
  * CONTENT-FREE wake - the wake signal carries no task instruction. Each turn
    receives the stable loop plus an optional board snapshot. Every operative
    fact and routine transition stays on-board.

Reuses coop_start.invoke_turn (process-tree-owned provider turn),
coopdb.insert_session (bind), and coop_watcher.actionable (wake). The scheduler
is pure / injectable, so it is unit-tested (selftest) without launching a real
CLI.

    coop start --item 24          # one-item runner (board discovered from cwd)
    coop start --all              # explicit backlog drain
    coop start --item 24 --dry-run # resolve only
    python -m agent_coop.coop_autonomous --selftest          # runnable check (no board, no CLI)
    touch <board-dir>/.coop-auto-stop                        # ask it to stop after the current turn

Failure handling:
  * CODEX-HEADLESS: ok=True with no board write → failed turn; no progress
    across a full cycle of actionable agents → stalled.
  * FAULT-ISOLATION: per-turn exceptions never kill the loop.
  * TIMING: claim lease always ≥ turn timeout (passed through the environment
    into status.next_action, never taught through prompt prose).

The scheduler is sparse-concurrent: it fans out only board actions whose
allowlisted lanes are independent, then fans results in deterministic
participant order. Unknown and implementation-lane work remains serialized.
"""
import argparse
import concurrent.futures
import hashlib
import inspect
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Mapping

from agent_coop import coop_action_scheduler
from agent_coop import coop_capabilities
from agent_coop import coop_decisions
from agent_coop import coopdb
from agent_coop import coop_mesh
from agent_coop import coop_provider_workers
from agent_coop import coop_provider_failures
from agent_coop import coop_prompt_cache
from agent_coop import coop_recipes
from agent_coop import coop_resident_workers
from agent_coop import coop_routing
from agent_coop import coop_runner_status
from agent_coop import coop_start
from agent_coop import coop_turn_trace
from agent_coop import coop_watcher
from agent_coop import coop_workers

# Preserve the established injected-transport seam used by offline and
# adversarial callers. Normal runs retain this exact function object and use
# resident transports; deliberate substitutes keep their fake/cold protocol
# instead of accidentally launching a real provider CLI.
_NATIVE_INVOKE_TURN = coop_start.invoke_turn

IDLE = coop_watcher.IDLE

ACTIVE_STATUSES = ("todo", "working", "review", "needs_input", "handoff")
PERSISTENT_PROVIDERS = frozenset({"claude", "codex", "grok"})
PROMPT_CACHE_HINT_PROVIDERS = frozenset({"claude"})
# The claude/grok residents are frozen on the read-only board/deep-review core
# surface, so a review turn can never edit the files under review. local_code
# turns need Edit/Write and therefore stay on the per-turn cold path; warming
# them needs a write-surface resident plus a per-turn containment answer, which
# is deferred. The codex app-server keeps its established three-manifest reuse.
_STREAMING_RESIDENT_PROVIDERS = frozenset({"claude", "grok"})
_RESIDENT_MANIFESTS = {
    "claude": frozenset({"board_core", "deep_review"}),
    "codex": frozenset({"board_core", "local_code", "deep_review"}),
    "grok": frozenset({"board_core", "deep_review"}),
}
# Deliberately empty: Grok owns the fixed mesh-v2 composition phase, and its
# prior structured lane regressed. A one-token preflight plus matched A/B must
# explicitly promote this action/provider arm in a later, separately approved
# live change.
STRUCTURED_MESH_REPORT_PROVIDERS = frozenset()


def content_free_turn(lease_seconds, *, token_efficient=False):
    """Thin bootstrap: deterministic status now owns all routine mechanics."""
    del lease_seconds  # carried in COOP_ACTION_LEASE_SECONDS, not prompt prose
    return (
        coop_prompt_cache.TOKEN_EFFICIENT_PROMPT_PREFIX
        if token_efficient
        else coop_prompt_cache.PROMPT_PREFIX
    )


MECHANICAL_PRECOMMIT_KINDS = frozenset({
    "claim_task", "resume_task", "review_task", "answer_question",
    # Completion is only routed once receipt + review gates have passed;
    # confirming it is mechanical, so the runner may do it in milliseconds.
    "complete_task",
})

# Per-turn --model switching is deliberately absent: changing model on a
# resumed session invalidates the provider-side cache. Model tiering, if
# ever, belongs in a dedicated per-tier persistent session.

WARM_SESSION_MAX_AGE_S = 24 * 3600
_WARM_SESSION_ID = re.compile(r"[0-9a-fA-F-]{36}")


def _warm_session_path(run_dir):
    return pathlib.Path(run_dir) / "warm-claude-session.json"


def load_warm_claude_session(run_dir, *, now_epoch=None):
    """Cross-run warmth: reuse claude's provider conversation from the last
    run on this board when fresh AND recorded under the current prompt
    prefix version (a prefix bump changes what the resumed conversation
    was taught - resume cold instead). Best-effort — a stale or broken id
    degrades through the worker's resume_failed recovery (fresh id, then
    create). Records written before the version field are trusted for
    back-compat."""
    import time as _time
    try:
        data = json.loads(
            _warm_session_path(run_dir).read_text(encoding="utf-8"))
        sid = data.get("provider_session_id")
        saved = float(data.get("saved_at_epoch", 0))
        version = data.get("prompt_prefix_version")
        if version is not None and version != \
                coop_prompt_cache.PROMPT_PREFIX_VERSION:
            return None
        age = (now_epoch if now_epoch is not None else _time.time()) - saved
        # The record is writable by every agent turn; only a UUID-shaped id
        # is ever placed in argv (anything else is "no warm session").
        sid = str(sid or "")
        if not _WARM_SESSION_ID.fullmatch(sid):
            return None
        if 0 <= age < WARM_SESSION_MAX_AGE_S:
            return sid
    except Exception:
        pass
    return None


def save_warm_claude_session(run_dir, provider_session_id, *,
                             now_epoch=None):
    """Persist a VALIDATED session id atomically (tmp + replace).

    Callers pass ``worker.validated_provider_session_id`` — an id that
    never carried a successful create/resume must not be written (a
    poisoned warm record guarantees the next run a failed opening
    attempt)."""
    import time as _time
    if not provider_session_id:
        return
    try:
        path = _warm_session_path(run_dir)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "provider_session_id": str(provider_session_id),
            "saved_at_epoch": (
                now_epoch if now_epoch is not None else _time.time()
            ),
            "prompt_prefix_version":
                coop_prompt_cache.PROMPT_PREFIX_VERSION,
        }), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def first_then_fresh_session_ids(stored):
    """Factory yielding the warm id once, then fresh ids for recovery."""
    state = {"used": False}

    def factory():
        if not state["used"]:
            state["used"] = True
            return str(stored)
        return str(uuid.uuid4())

    return factory


def print_run_speed_summary(trace_path):
    """Every run reports its own speed: per-provider first-write and
    turn-total medians read back from this run's trace."""
    import statistics
    from datetime import datetime
    turns = {}
    try:
        with open(trace_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                turn_id = event.get("turn_id")
                if not turn_id:
                    continue
                turn = turns.setdefault(
                    turn_id, {"agent": event.get("agent")})
                stamp = datetime.fromisoformat(event["at"])
                name = event.get("event")
                if name == "action_eligible":
                    turn["start"] = stamp
                elif name == "first_board_mutation":
                    turn.setdefault("first_write", stamp)
                elif name == "final_classification":
                    turn["end"] = stamp
    except Exception:
        return
    by_agent = {}
    for turn in turns.values():
        if "start" not in turn or "end" not in turn:
            continue
        entry = by_agent.setdefault(turn["agent"], {"fw": [], "total": []})
        entry["total"].append((turn["end"] - turn["start"]).total_seconds())
        if "first_write" in turn:
            entry["fw"].append(
                (turn["first_write"] - turn["start"]).total_seconds())
    if not by_agent:
        return
    print("  speed: agent  turns  med-first-write  med-turn-total")
    for agent in sorted(by_agent):
        entry = by_agent[agent]
        fw = (f"{statistics.median(entry['fw']):.0f}s"
              if entry["fw"] else "-")
        total = (f"{statistics.median(entry['total']):.0f}s"
                 if entry["total"] else "-")
        print(f"  speed: {agent:<7}{len(entry['total']):>4}"
              f"  {fw:>14}  {total:>13}")


def default_persistent_providers(participants):
    """Persistent workers are the default; cold turns are the opt-out."""
    return sorted(PERSISTENT_PROVIDERS & set(participants))


def default_prompt_cache_providers(participants):
    return sorted(PROMPT_CACHE_HINT_PROVIDERS & set(participants))


def causal_hydration_rows(conn, agent, action, item_id):
    """Board rows that explain THIS wake (hydration v2).

    Returns (answered_questions, handoff, review): answers to the agent's
    own questions committed SINCE ITS IMPLEMENTATION LANE LAST CLOSED (the
    suspension watermark - a needs_input closes the lane, so answers newer
    than the close are exactly the rows that wake the paused owner;
    recent history is not wake causality), plus the full
    handoff or review/receipt row the routed action targets. Read-only and
    best-effort per section — a failed query hydrates as None and the model
    falls back to reading the board.
    """
    answered = None
    try:
        clause = "" if item_id is None else " AND q.item_id=?"
        params = ((agent, agent) if item_id is None
                  else (agent, agent, item_id))
        rows = conn.execute(
            "SELECT q.question_id, q.item_id, q.exact_question, q.answer, "
            "q.answered_by_agent FROM questions q WHERE q.asked_by_agent=? "
            "AND q.status='answered' AND q.answered_at > COALESCE("
            "(SELECT MAX(c.closed_at) FROM claims c WHERE "
            "c.item_id=q.item_id AND c.claim_kind='implementation' AND "
            "c.claimed_by_agent=? AND c.closed_at IS NOT NULL), '')"
            + clause + " ORDER BY q.question_id DESC LIMIT 8",
            params).fetchall()
        answered = [dict(row) for row in reversed(rows)] or None
    except Exception:
        answered = None
    kind = action.get("kind") if isinstance(action, dict) else None
    target_id = action.get("target_id") if isinstance(action, dict) else None
    handoff = None
    try:
        if kind == "respond_handoff" and target_id is not None:
            row = conn.execute(
                "SELECT * FROM handoffs WHERE handoff_id=?",
                (target_id,)).fetchone()
            handoff = dict(row) if row is not None else None
    except Exception:
        handoff = None
    review = None
    try:
        if target_id is not None and (
                kind == "review_task"
                or (kind == "continue_task"
                    and action.get("target_type") == "review")):
            row = conn.execute(
                "SELECT * FROM reviews WHERE id=?", (target_id,)).fetchone()
            if row is not None:
                review = dict(row)
                if review.get("receipt_id") is not None:
                    receipt = conn.execute(
                        "SELECT * FROM receipts WHERE receipt_id=?",
                        (review["receipt_id"],)).fetchone()
                    if receipt is not None:
                        review["receipt"] = dict(receipt)
    except Exception:
        review = None
    return answered, handoff, review


def receipt_hydration_rows(conn, item_id, *, limit=8):
    """Return bounded answered-question evidence with exact event refs.

    A question row is usable as receipt evidence only when the same item has
    a canonical ``question_answered`` event whose decoded payload names that
    exact question. This is a read-only, best-effort hydration helper; any
    malformed/missing evidence is omitted rather than guessed.
    """
    if (
            not isinstance(item_id, int)
            or isinstance(item_id, bool)
            or item_id <= 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0):
        return None
    try:
        rows = conn.execute(
            "SELECT q.question_id, q.item_id, q.exact_question, q.answer, "
            "q.answered_by_agent, (SELECT e.event_id FROM events e WHERE "
            "e.item_id=q.item_id AND e.event_type='question_answered' AND "
            "json_extract(e.payload_json, '$.question_id')=q.question_id "
            "ORDER BY e.event_id DESC LIMIT 1) AS answer_event_id FROM "
            "(SELECT question_id, item_id, exact_question, answer, "
            "answered_by_agent FROM questions WHERE item_id=? AND "
            "status='answered' ORDER BY question_id DESC LIMIT ?) q "
            "ORDER BY q.question_id",
            (item_id, limit),
        ).fetchall()
    except Exception:
        return None
    evidence = []
    for row in rows:
        event_id = row["answer_event_id"]
        if (
                not isinstance(event_id, int)
                or isinstance(event_id, bool)
                or event_id <= 0):
            continue
        evidence.append({
            "question_id": row["question_id"],
            "item_id": row["item_id"],
            "exact_question": row["exact_question"],
            "answer": row["answer"],
            "answered_by_agent": row["answered_by_agent"],
            "proof_ref": f"event:{event_id}",
        })
    return evidence or None


def _positive_id(value):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def postwrite_completion_eligible(action):
    """Whether an action has an exact allowlisted terminal predicate."""
    if not isinstance(action, Mapping):
        return False
    item_id = action.get("item_id")
    target_id = action.get("target_id")
    claim_id = action.get("claim_id")
    if not _positive_id(item_id) or not _positive_id(target_id):
        return False
    kind = action.get("kind")
    target_type = action.get("target_type")
    if kind == "answer_question":
        return (
            target_type == "question"
            and _positive_id(claim_id)
        )
    if kind == "respond_handoff":
        return target_type == "handoff"
    if kind != "continue_task" or not _positive_id(claim_id):
        return False
    if target_type == "review":
        return True
    return target_type == "claim" and target_id == claim_id


def postwrite_action_satisfied(
        conn, *, agent, session_id, action, progress_before,
        lease_seconds):
    """Prove that this session completed its exact routed board action.

    This predicate is intentionally stricter than generic board progress. It
    requires an allowlisted post-baseline event, exact actor/session/item and
    target attribution, the corresponding canonical row transition, and a
    newly derived action whose executable fingerprint differs. Any malformed
    input or read failure returns ``False``.
    """
    if (
            not postwrite_completion_eligible(action)
            or not isinstance(agent, str)
            or not agent
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(progress_before, (tuple, list))
            or len(progress_before) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                and value >= 0
                for value in progress_before
            )
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or lease_seconds <= 0):
        return False
    item_id = action["item_id"]
    target_id = action["target_id"]
    claim_id = action.get("claim_id")
    before_event_id, before_count = progress_before

    try:
        session = conn.execute(
            "SELECT agent_id, status FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if (
                session is None
                or session["agent_id"] != agent
                or session["status"] != "running"):
            return False
        current_event_id, current_count = coopdb.actor_event_probe(
            conn,
            session_id=session_id,
            item_id=item_id,
        )
        if (
                current_event_id <= before_event_id
                or current_count <= before_count):
            return False
        events = conn.execute(
            "SELECT event_id, event_type, actor_agent_id, "
            "actor_session_id, claim_id, payload_json FROM events WHERE "
            "item_id=? AND actor_session_id=? AND event_id>? "
            "ORDER BY event_id",
            (item_id, session_id, before_event_id),
        ).fetchall()

        def payload_for(event):
            try:
                payload = json.loads(event["payload_json"])
            except (TypeError, ValueError):
                return None
            return payload if isinstance(payload, Mapping) else None

        def claim_row(expected_kind, subject_id=None):
            row = conn.execute(
                "SELECT * FROM claims WHERE claim_id=?",
                (claim_id,),
            ).fetchone()
            if (
                    row is None
                    or row["item_id"] != item_id
                    or row["claim_kind"] != expected_kind
                    or row["claimed_by_agent"] != agent
                    or row["owner_session_id"] != session_id):
                return None
            if subject_id is not None and row["subject_id"] != subject_id:
                return None
            return row

        matched = False
        for event in events:
            if event["actor_agent_id"] != agent:
                continue
            payload = payload_for(event)
            if payload is None:
                continue
            event_type = event["event_type"]
            kind = action["kind"]
            target_type = action.get("target_type")

            if kind == "answer_question":
                if (
                        event_type != "question_answered"
                        or event["claim_id"] != claim_id
                        or payload.get("question_id") != target_id):
                    continue
                claim = claim_row("question_response", target_id)
                question = conn.execute(
                    "SELECT * FROM questions WHERE question_id=? AND "
                    "item_id=?",
                    (target_id, item_id),
                ).fetchone()
                matched = bool(
                    claim is not None
                    and claim["status"] == "completed"
                    and claim["close_reason"] == "answered"
                    and question is not None
                    and question["status"] == "answered"
                    and question["answered_by_agent"] == agent
                    and question["answered_by_session"] == session_id
                )
            elif kind == "respond_handoff":
                states = {
                    "handoff_accepted": "accepted",
                    "handoff_declined": "declined",
                }
                expected_status = states.get(event_type)
                if (
                        expected_status is None
                        or payload.get("handoff_id") != target_id):
                    continue
                handoff = conn.execute(
                    "SELECT * FROM handoffs WHERE handoff_id=? AND item_id=?",
                    (target_id, item_id),
                ).fetchone()
                matched = bool(
                    handoff is not None
                    and handoff["to_agent"] == agent
                    and handoff["status"] == expected_status
                    and handoff["resolved_at"] is not None
                )
                if matched and event_type == "handoff_accepted":
                    accepted_claim_id = payload.get("claim_id")
                    accepted = (
                        conn.execute(
                            "SELECT * FROM claims WHERE claim_id=?",
                            (accepted_claim_id,),
                        ).fetchone()
                        if _positive_id(accepted_claim_id)
                        else None
                    )
                    matched = bool(
                        accepted is not None
                        and event["claim_id"] == accepted_claim_id
                        and accepted["item_id"] == item_id
                        and accepted["claim_kind"] == "implementation"
                        and accepted["claimed_by_agent"] == agent
                        and accepted["owner_session_id"] == session_id
                        and accepted["status"] == "active"
                    )
            elif target_type == "review":
                if (
                        event_type != "review_resolved"
                        or event["claim_id"] != claim_id
                        or payload.get("review_id") != target_id):
                    continue
                claim = claim_row("review", target_id)
                review = conn.execute(
                    "SELECT * FROM reviews WHERE id=? AND item_id=?",
                    (target_id, item_id),
                ).fetchone()
                matched = bool(
                    claim is not None
                    and claim["status"] == "completed"
                    and claim["close_reason"] == "verdict"
                    and review is not None
                    and review["status"] in {"approved", "changes"}
                    and review["resolved_at"] is not None
                    and review["reviewer_agent_id"] == agent
                )
            else:
                if event["claim_id"] != claim_id:
                    continue
                claim = claim_row("implementation")
                if claim is None:
                    continue
                if event_type == "receipt_submitted":
                    receipt_id = payload.get("receipt_id")
                    receipt = (
                        conn.execute(
                            "SELECT * FROM receipts WHERE receipt_id=?",
                            (receipt_id,),
                        ).fetchone()
                        if _positive_id(receipt_id)
                        else None
                    )
                    matched = bool(
                        claim["status"] == "active"
                        and receipt is not None
                        and receipt["item_id"] == item_id
                        and receipt["claim_id"] == claim_id
                        and receipt["submitted_by_agent"] == agent
                        and receipt["submitted_by_session"] == session_id
                        and receipt["superseded_at"] is None
                    )
                elif event_type == "needs_input":
                    question_id = payload.get("question_id")
                    question = (
                        conn.execute(
                            "SELECT * FROM questions WHERE question_id=?",
                            (question_id,),
                        ).fetchone()
                        if _positive_id(question_id)
                        else None
                    )
                    matched = bool(
                        claim["status"] == "closed"
                        and claim["close_reason"] == "needs_input"
                        and question is not None
                        and question["item_id"] == item_id
                        and question["asked_by_agent"] == agent
                        and question["asked_by_session"] == session_id
                        and question["status"] == "open"
                        and question["assigned_to_agent"] == payload.get("to")
                        and question["exact_question"] == payload.get("question")
                    )
                elif event_type == "handoff_created":
                    handoff_id = payload.get("handoff_id")
                    handoff = (
                        conn.execute(
                            "SELECT * FROM handoffs WHERE handoff_id=?",
                            (handoff_id,),
                        ).fetchone()
                        if _positive_id(handoff_id)
                        else None
                    )
                    matched = bool(
                        claim["status"] == "closed"
                        and claim["close_reason"] == "handoff"
                        and handoff is not None
                        and handoff["item_id"] == item_id
                        and handoff["claim_id"] == claim_id
                        and handoff["from_agent"] == agent
                        and handoff["from_session"] == session_id
                        and handoff["status"] == "pending"
                        and handoff["to_agent"] == payload.get("to_agent")
                    )
                elif event_type == "item_blocked":
                    item = conn.execute(
                        "SELECT status FROM items WHERE id=?",
                        (item_id,),
                    ).fetchone()
                    matched = bool(
                        payload.get("claim_id") == claim_id
                        and payload.get("type") == "blocked"
                        and claim["status"] == "closed"
                        and claim["close_reason"] == "blocked"
                        and item is not None
                        and item["status"] == "blocked"
                    )
            if matched:
                break
        if not matched:
            return False
        refreshed = coopdb.status(
            conn,
            agent,
            session_id=session_id,
            action_lease_seconds=lease_seconds,
            item_id=item_id,
        ).get("next_action")
        return not coop_action_scheduler.actions_equivalent(
            action,
            refreshed,
        )
    except Exception:
        return False


def mechanical_precommit_eligible(action):
    """True only for a fully code-authored, judgment-free claim command.

    Code already chose the target, transition, lease, and intent for these
    envelopes; the agent's judgment starts after the claim. Anything with
    required inputs, choices, or an unresolved {placeholder} stays with the
    model (handoff accept/decline and answer content are judgment per
    SKILL.md).
    """
    if not isinstance(action, dict):
        return False
    if action.get("kind") not in MECHANICAL_PRECOMMIT_KINDS:
        return False
    command = action.get("command")
    if not isinstance(command, (list, tuple)) or not command:
        return False
    if action.get("required_inputs") or action.get("choices"):
        return False
    return not any("{" in str(token) for token in command)


# Back-compat name used by older call sites / docs.
CONTENT_FREE_TURN = content_free_turn(3600)


# ---- pure scheduler (testable without a board or a CLI) ---------------------

def structured_answer_eligible(action):
    """True only for a held question and its canonical answer placeholder."""
    return (
        isinstance(action, dict)
        and action.get("kind") == "answer_question"
        and action.get("claim_id") is not None
        and isinstance(action.get("command"), (list, tuple))
        and any(str(token) == "{answer}" for token in action["command"])
    )


def _exact_command(value, expected):
    return (
        isinstance(value, (list, tuple))
        and [str(token) for token in value]
        == [str(token) for token in expected]
    )


def structured_decision_kind(action):
    """Return a bounded decision kind only for exact canonical grammar."""
    if not isinstance(action, Mapping):
        return None
    kind = action.get("kind")
    target_id = action.get("target_id")
    item_id = action.get("item_id")
    if not _positive_id(target_id) or not _positive_id(item_id):
        return None
    if kind == "answer_question":
        claim_id = action.get("claim_id")
        expected = [
            *coopdb.CLI_ARGV,
            "question",
            "answer",
            "--claim",
            str(claim_id),
            "--answer",
            "{answer}",
        ]
        if (
                action.get("target_type") == "question"
                and _positive_id(claim_id)
                and _exact_command(action.get("command"), expected)
                and list(action.get("required_inputs") or ()) == ["answer"]
                and not action.get("choices")):
            return "answer_questions"
        return None
    if kind != "respond_handoff":
        return None
    choices = action.get("choices")
    if (
            action.get("target_type") != "handoff"
            or action.get("claim_id") is not None
            or action.get("command") is not None
            or list(action.get("required_inputs") or ())
            != ["handoff_response"]
            or not isinstance(choices, (list, tuple))
            or len(choices) != 2):
        return None
    accept, decline = choices
    if not isinstance(accept, Mapping) or not isinstance(decline, Mapping):
        return None
    lease_command = accept.get("command")
    if not isinstance(lease_command, (list, tuple)) or not lease_command:
        return None
    try:
        lease_value = lease_command[-1]
        lease = float(lease_value)
    except (TypeError, ValueError):
        return None
    if not (lease > 0 and lease < float("inf")):
        return None
    expected_accept = [
        *coopdb.CLI_ARGV,
        "handoff",
        "accept",
        "--id",
        str(target_id),
        "--intent",
        f"accept handoff {target_id}",
        "--lease-seconds",
        str(lease_value),
    ]
    expected_decline = [
        *coopdb.CLI_ARGV,
        "handoff",
        "decline",
        "--id",
        str(target_id),
        "--reason",
        "{reason}",
    ]
    if (
            set(accept) != {"kind", "command", "required_inputs"}
            or accept.get("kind") != "accept_handoff"
            or accept.get("required_inputs") not in ([], ())
            or not _exact_command(lease_command, expected_accept)
            or set(decline) != {"kind", "command", "required_inputs"}
            or decline.get("kind") != "decline_handoff"
            or list(decline.get("required_inputs") or ()) != ["reason"]
            or not _exact_command(decline.get("command"), expected_decline)):
        return None
    return "respond_handoff"


def structured_action_still_current(
        conn, *, agent, session_id, action, lease_seconds):
    """Re-derive the exact executable action immediately before postcommit."""
    if structured_decision_kind(action) is None:
        return False
    try:
        current = coopdb.status(
            conn,
            agent,
            session_id=session_id,
            action_lease_seconds=lease_seconds,
            item_id=action["item_id"],
        ).get("next_action")
    except Exception:
        return False
    return coop_action_scheduler.actions_equivalent(action, current)


def structured_decision_request(
        conn, *, provider, agent, action, item):
    """Build one isolated request from exact current board rows, or None."""
    kind = structured_decision_kind(action)
    if kind is None:
        return None
    try:
        if kind == "answer_questions":
            row = conn.execute(
                "SELECT question_id, item_id, exact_question, "
                "asked_by_agent, assigned_to_agent FROM questions WHERE "
                "question_id=? AND item_id=? AND status='open' AND "
                "assigned_to_agent=?",
                (action["target_id"], action["item_id"], agent),
            ).fetchone()
            if row is None:
                return None
            question = dict(row)
            prompt = coop_prompt_cache.structured_questions_decision_prompt(
                [question], item)
            expected = {"question_ids": (action["target_id"],)}
        else:
            row = conn.execute(
                "SELECT handoff_id, item_id, from_agent, to_agent, reason, "
                "summary, completed_work, remaining_work, risks, "
                "suggested_next_action, proof_references FROM handoffs "
                "WHERE handoff_id=? AND item_id=? AND status='pending' AND "
                "to_agent=?",
                (action["target_id"], action["item_id"], agent),
            ).fetchone()
            if row is None:
                return None
            handoff = dict(row)
            prompt = coop_prompt_cache.structured_handoff_decision_prompt(
                handoff, item)
            expected = {"handoff_id": action["target_id"]}
        return coop_decisions.make_decision_request(
            decision_kind=kind,
            provider=provider,
            agent_id=agent,
            item_id=action["item_id"],
            action_fingerprint=(
                coop_action_scheduler.action_fingerprint(action)
            ),
            prompt=prompt,
            **expected,
        )
    except Exception:
        return None


def structured_decision_command(action, request, value):
    """Render only the action-owned canonical argv for a validated value."""
    if (
            not isinstance(request, coop_decisions.DecisionRequest)
            or structured_decision_kind(action) != request.decision_kind
            or coop_action_scheduler.action_fingerprint(action)
            != request.action_fingerprint):
        return None
    if request.decision_kind == "answer_questions":
        answers = value.get("answers") if isinstance(value, Mapping) else None
        if not isinstance(answers, (list, tuple)) or len(answers) != 1:
            return None
        entry = answers[0]
        answer = entry.get("answer") if isinstance(entry, Mapping) else None
        template = action.get("command")
        substitutions = {"{answer}": answer}
    elif request.decision_kind == "respond_handoff":
        response = value.get("response") if isinstance(value, Mapping) else None
        choice_kind = (
            "accept_handoff" if response == "accept"
            else "decline_handoff" if response == "decline"
            else None
        )
        choice = next(
            (
                entry
                for entry in action.get("choices") or ()
                if entry.get("kind") == choice_kind
            ),
            None,
        )
        if choice is None:
            return None
        template = choice.get("command")
        substitutions = {"{reason}": value.get("reason")}
    else:
        return None
    command = []
    for token in template or ():
        text = str(token)
        if text == "python":
            command.append(sys.executable)
        elif text in substitutions:
            replacement = substitutions[text]
            if not isinstance(replacement, str):
                return None
            command.append(replacement)
        elif "{" in text or "}" in text:
            return None
        else:
            command.append(text)
    return command or None


def dispatch_profile(
        candidate,
        *,
        structured_answers=True,
        structured_decisions=False,
        failed_isolated=frozenset()):
    """Prepare one immutable execution and workspace-admission profile."""
    lane = coop_action_scheduler.action_lane(candidate)
    fingerprint = coop_action_scheduler.action_fingerprint(candidate.action)
    decision_kind = (
        structured_decision_kind(candidate.action)
        if structured_decisions
        else None
    )
    if (
            decision_kind is not None
            and (decision_kind != "answer_questions" or structured_answers)):
        failure_key = (candidate.agent, fingerprint)
        if (
                candidate.agent == "claude"
                and failure_key not in failed_isolated):
            return coop_action_scheduler.DispatchProfile(
                lane=lane,
                workspace_surface="none",
                execution_mode="isolated_structured_decision",
                action_fingerprint=fingerprint,
            )
    if structured_answers and structured_answer_eligible(candidate.action):
        failure_key = (candidate.agent, fingerprint)
        if (
                candidate.agent == "claude"
                and failure_key not in failed_isolated):
            return coop_action_scheduler.DispatchProfile(
                lane=lane,
                workspace_surface="none",
                execution_mode="isolated_structured_answer",
                action_fingerprint=fingerprint,
            )
    return coop_action_scheduler.DispatchProfile(
        lane=lane,
        workspace_surface="write" if lane is None else "read",
        execution_mode="tool_turn",
        action_fingerprint=fingerprint,
    )


def admission_conflicts(candidate_profile, in_flight_profile):
    """Return stable reasons this pair cannot overlap."""
    conflicts = []
    if (
            candidate_profile.workspace_surface == "write"
            and in_flight_profile.workspace_surface in {"read", "write"}):
        conflicts.append("candidate_write_blocked_by_workspace_user")
    if (
            candidate_profile.workspace_surface == "read"
            and in_flight_profile.workspace_surface == "write"):
        conflicts.append("candidate_read_blocked_by_workspace_writer")
    if (
            candidate_profile.lane is not None
            and candidate_profile.lane == in_flight_profile.lane):
        conflicts.append("same_lane")
    return tuple(conflicts)


def admit_candidate(profile, in_flight_profiles):
    """Continuous-dispatch admission rule.

    - a write candidate admits only when no read or write turn is in flight;
    - a read candidate admits only when no write turn is in flight;
    - a workspace-free candidate may overlap either surface;
    - equal lanes never overlap. One-turn-per-provider is the caller's
      invariant.
    """
    return not any(
        admission_conflicts(profile, other)
        for other in in_flight_profiles
    )


def profile_refinement_allowed(admitted, effective):
    """Whether post-precommit execution narrows the admitted workspace scope."""
    surface_rank = {"none": 0, "read": 1, "write": 2}
    return (
        effective.lane == admitted.lane
        and surface_rank.get(effective.workspace_surface, 3)
        <= surface_rank.get(admitted.workspace_surface, -1)
    )


def actions_equivalent(left, right):
    """Compatibility delegate for complete executable action identity."""
    return coop_action_scheduler.actions_equivalent(left, right)


def _take_turn_accepts_profile(take_turn):
    """Whether an injected callback accepts the new third profile argument."""
    try:
        inspect.signature(take_turn).bind(None, None, None)
    except (TypeError, ValueError):
        return False
    return True


def _wrap_take_turn(take_turn, agent, hint, profile, accepts_profile):
    try:
        if accepts_profile:
            return take_turn(agent, hint, profile)
        return take_turn(agent, hint)
    except Exception as exc:
        return {"ok": False, "note": f"turn error: {type(exc).__name__}"}


def run_autonomous(participants, *, actionable_fn, take_turn, all_done,
                   stopped=lambda: False, interval=2.0, max_turns=200,
                   max_idle_rounds=None, max_noop_cycles=None,
                   progress_fn=None, status_fn=None, prepare_cycle=None,
                   profile_fn=None, scheduler_event_fn=None, sleep=None,
                   continuous=True, dispatch_tick=0.5):
    """Wake-driven scheduler. Continuous mode (default) reacts to committed
    board progress with at most one turn in flight per provider; barriered
    mode (continuous=False) is the batch scheduler kept as the
    opt-out.

    Shared contract: actionable_fn(agent) -> action dict or IDLE;
    take_turn(agent, hint, profile) -> result dict (legacy two-argument
    injected callbacks remain supported; ok=False turns still count);
    progress_fn() -> hashable token that changes only on real board
    progress; prepare_cycle() may activate a frozen recipe edge only while
    nothing is in flight; a non-empty return value is a terminal run
    reason. Returns (reason, turns_taken, log).
    """
    kwargs = dict(
        actionable_fn=actionable_fn, take_turn=take_turn, all_done=all_done,
        stopped=stopped, interval=interval, max_turns=max_turns,
        max_idle_rounds=max_idle_rounds, max_noop_cycles=max_noop_cycles,
        progress_fn=progress_fn, status_fn=status_fn,
        prepare_cycle=prepare_cycle,
        profile_fn=profile_fn or dispatch_profile,
        scheduler_event_fn=scheduler_event_fn,
        sleep=sleep)
    if continuous:
        return _run_autonomous_continuous(
            participants, dispatch_tick=dispatch_tick, **kwargs)
    return _run_autonomous_barriered(participants, **kwargs)


def _run_autonomous_continuous(participants, *, actionable_fn, take_turn,
                               all_done, stopped, interval, max_turns,
                               max_idle_rounds, max_noop_cycles, progress_fn,
                               status_fn, prepare_cycle, profile_fn,
                               scheduler_event_fn, sleep, dispatch_tick=0.5):
    """Dispatch from committed board writes instead of provider exit.

    Noop accounting consumes each turn's actor-attributed result fields.
    The global board token remains only a wake signal, so a still-running
    sibling's write cannot misclassify a no-op turn as productive.
    """
    import time
    sleep = sleep or time.sleep
    status_fn = status_fn or (lambda phase, **fields: None)

    def report(phase, **fields):
        try:
            status_fn(phase, **fields)
        except Exception:
            pass  # operational telemetry must never stop coordination

    def report_terminal(reason, reason_code, evidence):
        report(
            "terminal_detail",
            reason=reason,
            reason_code=reason_code,
            evidence=evidence,
        )

    def emit_scheduler_event(event_name, **fields):
        try:
            scheduler_event_fn(event_name, **fields)
        except Exception:
            pass  # operational telemetry must never stop coordination

    def participant_order(agents):
        ordered = [a for a in tuple(participants) if a in agents]
        return ordered + [a for a in agents if a not in ordered]

    turns, idle_rounds, noop_streak = 0, 0, 0
    streak_agents, streak_kinds = set(), set()
    log = []
    last_polled_action_kinds = []
    in_flight = {}
    last_token = progress_fn() if progress_fn else None
    poll_needed = True
    take_turn_accepts_profile = _take_turn_accepts_profile(take_turn)
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(8, len(tuple(participants)) or 1),
        thread_name_prefix="coop-turn")

    def collect(agent):
        nonlocal turns
        entry = in_flight.pop(agent)
        result = entry["future"].result()
        log.append({
            "agent": agent,
            "hint": entry["hint"],
            "result": result,
        })
        turns += 1
        return entry, result

    def drain_in_flight():
        while in_flight:
            concurrent.futures.wait(
                [entry["future"] for entry in in_flight.values()])
            for agent in participant_order([
                    a for a in in_flight
                    if in_flight[a]["future"].done()]):
                collect(agent)

    try:
        while True:
            if stopped():
                drain_in_flight()
                return "stopped", turns, log
            if all_done():
                drain_in_flight()
                return "all_done", turns, log
            if turns >= max_turns:
                report_terminal(
                    "max_turns",
                    "turn_budget_exhausted",
                    {"turns": turns, "max_turns": max_turns},
                )
                drain_in_flight()
                return "max_turns", turns, log
            if prepare_cycle is not None and not in_flight:
                prepare_reason = prepare_cycle()
                if prepare_reason:
                    return prepare_reason, turns, log
            done_now = participant_order([
                a for a in in_flight if in_flight[a]["future"].done()])
            if done_now:
                harvested = []
                for agent in done_now:
                    entry, result = collect(agent)
                    harvested.append(result)
                    poll_needed = True
                    if max_noop_cycles is None:
                        continue
                    if (isinstance(result, dict)
                            and result.get("retry_after_board_refresh")
                            is True):
                        pass
                    elif (isinstance(result, dict)
                          and result.get("made_board_progress") is True):
                        noop_streak = 0
                        streak_agents.clear()
                        streak_kinds.clear()
                    else:
                        noop_streak += 1
                        streak_agents.add(agent)
                        streak_kinds.add(entry["hint"])
                if all_done():
                    drain_in_flight()
                    return "all_done", turns, log
                for result in harvested:
                    terminal_reason = coop_provider_failures.terminal_reason(
                        result
                    )
                    if terminal_reason is not None:
                        drain_in_flight()
                        return terminal_reason, turns, log
                if (max_noop_cycles is not None
                        and noop_streak >= max_noop_cycles):
                    report_terminal(
                        "stalled",
                        "actionable_no_board_progress",
                        {
                            "noop_cycles": noop_streak,
                            "turns": turns,
                            "actionable_agents": sorted(streak_agents),
                            "action_kinds": sorted(streak_kinds),
                        },
                    )
                    drain_in_flight()
                    return "stalled", turns, log
                if turns >= max_turns:
                    report_terminal(
                        "max_turns",
                        "turn_budget_exhausted",
                        {"turns": turns, "max_turns": max_turns},
                    )
                    drain_in_flight()
                    return "max_turns", turns, log
                idle_rounds = 0
            if progress_fn is not None:
                token = progress_fn()
                if token != last_token:
                    last_token = token
                    poll_needed = True
            else:
                poll_needed = True
            candidates = []
            if poll_needed:
                poll_needed = False
                polled_kinds = []
                for agent in tuple(participants):
                    if agent in in_flight:
                        continue
                    if stopped():
                        drain_in_flight()
                        return "stopped", turns, log
                    report("checking", agent=agent)
                    action = actionable_fn(agent)
                    hint = (
                        action.get("kind")
                        if isinstance(action, dict)
                        else action
                    )
                    polled_kinds.append(str(hint or IDLE))
                    if hint and hint != IDLE:
                        candidate = coop_action_scheduler.ActionCandidate(
                                agent=agent,
                                hint=hint,
                                action=(
                                    action if isinstance(action, dict)
                                    else {}
                                ),
                            )
                        candidates.append((candidate, profile_fn(candidate)))
                if not in_flight:
                    last_polled_action_kinds = polled_kinds
            for candidate, profile in candidates:
                if turns + len(in_flight) >= max_turns:
                    break
                if len(in_flight) >= \
                        coop_action_scheduler.MAX_TURN_CONCURRENCY:
                    break
                if candidate.agent in in_flight:
                    continue
                ordered_in_flight = [
                    (agent, in_flight[agent]["profile"])
                    for agent in participant_order(in_flight)
                ]
                if not admit_candidate(
                        profile,
                        [other for _agent, other in ordered_in_flight]):
                    if scheduler_event_fn is not None:
                        comparisons = []
                        admission_reasons = []
                        for agent, other in ordered_in_flight:
                            conflicts = admission_conflicts(profile, other)
                            comparisons.append({
                                "agent": agent,
                                "lane": other.lane,
                                "workspace_surface": other.workspace_surface,
                                "execution_mode": other.execution_mode,
                                "action_fingerprint":
                                    other.action_fingerprint,
                                "conflicts": list(conflicts),
                            })
                            for conflict in conflicts:
                                if conflict not in admission_reasons:
                                    admission_reasons.append(conflict)
                        emit_scheduler_event(
                            "dispatch_admission_skipped",
                            agent=candidate.agent,
                            action=candidate.hint,
                            details={
                                "dispatch_lane": profile.lane,
                                "workspace_surface":
                                    profile.workspace_surface,
                                "execution_mode": profile.execution_mode,
                                "action_fingerprint":
                                    profile.action_fingerprint,
                                "admission_reasons": admission_reasons,
                                "in_flight_profiles": comparisons,
                            },
                        )
                    continue  # stays on the board; re-derived next tick
                report("turn", agent=candidate.agent, action=candidate.hint)
                in_flight[candidate.agent] = {
                    "hint": candidate.hint,
                    "profile": profile,
                    "future": executor.submit(
                        _wrap_take_turn,
                        take_turn,
                        candidate.agent,
                        candidate.hint,
                        profile,
                        take_turn_accepts_profile,
                    ),
                }
                idle_rounds = 0
            if in_flight:
                concurrent.futures.wait(
                    [entry["future"] for entry in in_flight.values()],
                    timeout=dispatch_tick,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                continue
            if not candidates:
                idle_rounds += 1
                report("waiting")
                if (max_idle_rounds is not None
                        and idle_rounds >= max_idle_rounds):
                    report_terminal(
                        "stalled",
                        "no_actionable_participant",
                        {
                            "idle_rounds": idle_rounds,
                            "turns": turns,
                            "actionable_agents": [],
                            "action_kinds": (
                                sorted(set(last_polled_action_kinds))
                                or [IDLE]
                            ),
                        },
                    )
                    return "stalled", turns, log
                sleep(interval)
                poll_needed = True
    finally:
        executor.shutdown(wait=True)


def _run_autonomous_barriered(participants, *, actionable_fn, take_turn,
                              all_done, stopped=lambda: False, interval=2.0,
                              max_turns=200, max_idle_rounds=None,
                              max_noop_cycles=None, progress_fn=None,
                              status_fn=None, prepare_cycle=None,
                              profile_fn=dispatch_profile,
                              scheduler_event_fn=None, sleep=None):
    """The batch scheduler: select a lane-independent batch, wait
    for every future, then re-poll (the `--no-continuous-dispatch` opt-out).
    """
    import time
    sleep = sleep or time.sleep
    status_fn = status_fn or (lambda phase, **fields: None)

    def report(phase, **fields):
        try:
            status_fn(phase, **fields)
        except Exception:
            pass  # operational telemetry must never stop coordination

    def report_terminal(reason, reason_code, evidence):
        report(
            "terminal_detail",
            reason=reason,
            reason_code=reason_code,
            evidence=evidence,
        )

    turns, idle_rounds, noop_streak, log = 0, 0, 0, []
    streak_agents, streak_kinds = set(), set()
    take_turn_accepts_profile = _take_turn_accepts_profile(take_turn)
    last_polled_action_kinds = []
    while True:
        if stopped():
            return "stopped", turns, log
        if all_done():
            return "all_done", turns, log
        if turns >= max_turns:
            report_terminal(
                "max_turns",
                "turn_budget_exhausted",
                {"turns": turns, "max_turns": max_turns},
            )
            return "max_turns", turns, log
        if prepare_cycle is not None:
            prepare_reason = prepare_cycle()
            if prepare_reason:
                return prepare_reason, turns, log
        candidates = []
        profiles = {}
        polled_action_kinds = []
        for agent in tuple(participants):
            if stopped():
                return "stopped", turns, log
            report("checking", agent=agent)
            action = actionable_fn(agent)
            hint = (
                action.get("kind")
                if isinstance(action, dict)
                else action
            )
            polled_action_kinds.append(str(hint or IDLE))
            if hint and hint != IDLE:
                candidate = coop_action_scheduler.ActionCandidate(
                        agent=agent,
                        hint=hint,
                        action=action if isinstance(action, dict) else {},
                    )
                candidates.append(candidate)
                profiles[agent] = profile_fn(candidate)
        last_polled_action_kinds = polled_action_kinds
        if candidates:
            remaining = max_turns - turns
            batch = coop_action_scheduler.select_action_batch(
                candidates,
                limit=remaining,
            )
            for candidate in batch:
                report(
                    "turn",
                    agent=candidate.agent,
                    action=candidate.hint,
                )

            def run_candidate(candidate):
                return _wrap_take_turn(
                    take_turn,
                    candidate.agent,
                    candidate.hint,
                    profiles[candidate.agent],
                    take_turn_accepts_profile,
                )

            if len(batch) == 1:
                results = [run_candidate(batch[0])]
            else:
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=len(batch),
                        thread_name_prefix="coop-turn") as executor:
                    futures = [
                        executor.submit(run_candidate, candidate)
                        for candidate in batch
                    ]
                    results = [future.result() for future in futures]

            for candidate, result in zip(batch, results):
                log.append({
                    "agent": candidate.agent,
                    "hint": candidate.hint,
                    "result": result,
                })
            turns += len(batch)
            if all_done():
                return "all_done", turns, log
            for result in results:
                terminal_reason = coop_provider_failures.terminal_reason(
                    result
                )
                if terminal_reason is not None:
                    return terminal_reason, turns, log
            if turns >= max_turns:
                report_terminal(
                    "max_turns",
                    "turn_budget_exhausted",
                    {"turns": turns, "max_turns": max_turns},
                )
                return "max_turns", turns, log
            idle_rounds = 0
            if max_noop_cycles is not None:
                for candidate, result in zip(batch, results):
                    if (isinstance(result, dict)
                            and result.get("retry_after_board_refresh")
                            is True):
                        continue
                    if (isinstance(result, dict)
                            and result.get("made_board_progress") is True):
                        noop_streak = 0
                        streak_agents.clear()
                        streak_kinds.clear()
                        continue
                    noop_streak += 1
                    streak_agents.add(candidate.agent)
                    streak_kinds.add(candidate.hint)
                    if noop_streak >= max_noop_cycles:
                        report_terminal(
                            "stalled",
                            "actionable_no_board_progress",
                            {
                                "noop_cycles": noop_streak,
                                "turns": turns,
                                "actionable_agents": sorted(streak_agents),
                                "action_kinds": sorted(streak_kinds),
                            },
                        )
                        return "stalled", turns, log
        else:
            idle_rounds += 1
            if max_idle_rounds is not None and idle_rounds >= max_idle_rounds:
                report("waiting")
                report_terminal(
                    "stalled",
                    "no_actionable_participant",
                    {
                        "idle_rounds": idle_rounds,
                        "turns": turns,
                        "actionable_agents": [],
                        "action_kinds": (
                            sorted(set(last_polled_action_kinds))
                            or [IDLE]
                        ),
                    },
                )
                return "stalled", turns, log
            report("waiting")
            sleep(interval)


def classify_turn_result(
        result,
        *,
        made_board_progress,
        actor_board_events):
    """CODEX-HEADLESS / no-progress: ok=True with no board write is a failure.

    Pure helper — used by the real take_turn wrapper and unit-tested without
    a board.
    """
    out = dict(result or {})
    out["made_board_progress"] = bool(made_board_progress)
    out["actor_board_events"] = max(0, int(actor_board_events))
    if out.get("ok") and not made_board_progress:
        out["ok"] = False
        note = (out.get("note") or "").strip()
        tag = "no_board_progress"
        out["note"] = f"{tag}: {note}" if note else tag
    return out


def _turn_classification(result, *, made_board_progress):
    explicit = result.get("classification")
    if explicit:
        return explicit
    note = str(result.get("note") or "")
    if note.startswith("capability_denied:"):
        return "capability_denied"
    if note.startswith("timeout"):
        return "timeout"
    if note.startswith("no_board_progress"):
        return "no_board_progress"
    if result.get("ok") and made_board_progress:
        return "board_progress"
    return "failed"


def _emit_trace(trace, event, **fields):
    try:
        trace.emit(event, **fields)
    except Exception:
        pass


def lease_for_timeout(timeout_s):
    """TIMING invariant: claim lease must always be ≥ turn timeout."""
    t = max(1, int(timeout_s))
    return max(t, t * 6, 3600)


# ---- real board wiring ------------------------------------------------------

def board_all_done(conn, item_id=None):
    """True when no item is in an active (non-terminal) status."""
    if item_id is not None:
        row = conn.execute(
            "SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
        return row is not None and row["status"] == "done"
    q = "SELECT 1 FROM items WHERE status IN (%s) LIMIT 1" % \
        ",".join("?" * len(ACTIVE_STATUSES))
    return conn.execute(q, ACTIVE_STATUSES).fetchone() is None


def _board_dir(board_path):
    return pathlib.Path(board_path).resolve().parent


def workspace_dir(board_path):
    """The directory agents work in — the cwd of every provider turn.

    A board under `.coop/` names its parent as the workspace (B4: one board
    per working directory, so one per git worktree). Every older layout is
    its own workspace, unchanged. Board-adjacent runtime state keeps using
    `_board_dir`, which is what puts it under `.coop/` for free.

    Kept separate from `_board_dir` on purpose: the workspace is a parameter
    the turn is given, not a fact derived at the call site. That is the seam
    a future session-level workspace would need, and it costs nothing now."""
    board_dir = _board_dir(board_path)
    return board_dir.parent if board_dir.name == ".coop" else board_dir


def stop_flag_path(board_path):
    return str(_board_dir(board_path) / ".coop-auto-stop")


def normalize_persistent_providers(values, *, participants):
    selected = {str(value) for value in (values or ())}
    unknown = selected - PERSISTENT_PROVIDERS
    if unknown:
        raise ValueError(
            "unknown persistent provider: "
            + ", ".join(sorted(unknown))
        )
    outside = selected - set(participants)
    if outside:
        raise ValueError(
            "persistent provider is not a run participant: "
            + ", ".join(sorted(outside))
        )
    return selected


def normalize_prompt_cache_providers(values, *, participants):
    selected = {str(value) for value in (values or ())}
    unknown = selected - PROMPT_CACHE_HINT_PROVIDERS
    if unknown:
        raise ValueError(
            "unknown prompt-cache provider: "
            + ", ".join(sorted(unknown))
        )
    outside = selected - set(participants)
    if outside:
        raise ValueError(
            "prompt-cache provider is not a run participant: "
            + ", ".join(sorted(outside))
        )
    return selected


def worker_mode_for(provider, persistent_providers, manifest):
    if provider not in persistent_providers or manifest is None:
        return "cold"
    if manifest is not None and manifest.external_servers:
        # Dynamic MCP attachment is not promoted until provider smokes pass.
        return "cold"
    if manifest.name not in _RESIDENT_MANIFESTS.get(provider, ()):
        # Resident transports are started with one frozen core tool surface.
        # Native research/browser turns keep their action-specific cold path.
        return "cold"
    return "persistent"


def resident_completion_probe_mode(
        worker_mode, *, provider, completion_probe_attached):
    """Demote a resident turn that carries a post-write completion probe."""
    if (
        worker_mode == "persistent"
        and completion_probe_attached
        and provider in _STREAMING_RESIDENT_PROVIDERS
    ):
        # Resident stream/ACP transports cannot early-exit a streaming turn,
        # so the probe would be inert and a slow final turn would burn the
        # restart budget on a turn timeout. The codex app-server honors it.
        return "cold"
    return worker_mode


def agent_action_env(
        agent,
        *,
        session_id,
        board_path,
        lease_seconds,
        item_id=None,
        source=None,
        inherit=False):
    """The bound-session environment for a runner-side board command.

    Built from the same allowlist as a provider child (``inherit`` restores
    full inheritance): the routed CLI needs Python, PATH and ``COOP_*``.
    """
    env = coop_start.tool_turn_env(
        source,
        provider=str(agent),
        inherit=bool(inherit),
    )
    env["PYTHONPATH"] = coop_start.pythonpath_with_install_root(env)
    env.update({
        "COOP_SESSION_ID": str(session_id),
        "COOP_AGENT": str(agent),
        "COOP_AGENT_ID": str(agent),
        "COOP_PROVIDER": str(agent),
        "COOP_DB": str(board_path),
        "COOP_ACTION_LEASE_SECONDS": str(int(lease_seconds)),
    })
    if item_id is not None:
        env["COOP_ITEM_ID"] = str(int(item_id))
    return env


def prepare_opt_in_worker_pool(
        providers,
        *,
        sessions,
        board_path,
        cwd,
        run_dir,
        action_lease_seconds,
        item_id,
        invoke_turn=None,
        resolve_argv=None,
        build_profile=None,
        fresh_sessions=False,
        prompt_cache_providers=frozenset(),
        inherit_env=False):
    """Prepare opt-in workers without starting a provider process.

    Every resident worker starts from ``coop_start.tool_turn_env`` (unless
    ``inherit_env``) and spawns through ``coop_start.guarded_prepare_tree``.
    """
    invoke_turn = invoke_turn or coop_start.invoke_turn
    resolve_argv = resolve_argv or coop_start.resolved_provider_argv
    build_profile = build_profile or coop_capabilities.build_launch_profile
    workspace = coopdb.board_workspace(board_path)
    workers = {}
    cleanups = []
    unavailable = {}

    def worker_environment(provider):
        environment = coop_start.tool_turn_env(
            os.environ,
            provider=provider,
            inherit=bool(inherit_env),
        )
        environment.update({
            "COOP_SESSION_ID": sessions[provider],
            "COOP_AGENT": provider,
            "COOP_AGENT_ID": provider,
            "COOP_PROVIDER": provider,
            "COOP_DB": str(board_path),
            "COOP_ACTION_LEASE_SECONDS": str(
                int(action_lease_seconds)
            ),
            "PYTHONPATH": coop_start.pythonpath_with_install_root(
                environment
            ),
        })
        if item_id is not None:
            environment["COOP_ITEM_ID"] = str(int(item_id))
        return environment

    for provider in sorted(set(providers)):
        if provider not in {"claude", "codex", "grok"}:
            unavailable[provider] = "unsupported_provider"
            continue
        if (
            provider in {"claude", "grok"}
            and invoke_turn is not _NATIVE_INVOKE_TURN
        ):
            try:
                environment = worker_environment(provider)
                state_factory = (
                    (
                        lambda workspace=workspace, provider=provider,
                        environment=dict(environment):
                        coop_provider_workers.prepare_resumed_state(
                            workspace,
                            provider,
                            env=environment,
                        )
                    )
                    if provider == "grok"
                    else None
                )
                warm_id = (
                    load_warm_claude_session(run_dir)
                    if provider == "claude" and not fresh_sessions
                    else None
                )
                worker_kwargs = {}
                if warm_id:
                    worker_kwargs = {
                        "provider_session_id_factory":
                            first_then_fresh_session_ids(warm_id),
                        "resume_initial": True,
                    }
                workers[provider] = (
                    coop_provider_workers.ResumedCliWorker(
                        provider,
                        invoke=invoke_turn,
                        provider_state_factory=state_factory,
                        **worker_kwargs,
                    )
                )
            except Exception as exc:
                unavailable[provider] = type(exc).__name__
            continue
        profile = None
        state_cleanup = None
        try:
            resolved = resolve_argv(provider)
            if not resolved:
                raise ValueError("provider command is unavailable")
            environment = worker_environment(provider)
            if provider == "claude":
                # The resident argv must carry the same cache hint the run
                # traces, otherwise the trace records a configuration the
                # provider never received.
                base_argv = coop_start.apply_prompt_cache_hint(
                    [
                        resolved[0],
                        "-p",
                        "--dangerously-skip-permissions",
                    ],
                    provider="claude",
                    enabled="claude" in prompt_cache_providers,
                )
                profile = build_profile(
                    provider,
                    base_argv,
                    coop_capabilities.MANIFESTS["board_core"],
                    env=environment,
                    workspace=workspace,
                    run_dir=run_dir,
                    servers={},
                )
                warm_id = (
                    load_warm_claude_session(run_dir)
                    if not fresh_sessions
                    else None
                )
                session_id_factory = (
                    first_then_fresh_session_ids(warm_id)
                    if warm_id
                    else None
                )
                frozen_argv = tuple(profile.argv)
                workers[provider] = (
                    coop_resident_workers.ClaudeStreamWorker(
                        argv_factory=(
                            lambda session_id, resume,
                            base=frozen_argv:
                            coop_resident_workers.claude_stream_argv(
                                base,
                                provider_session_id=session_id,
                                resume=resume,
                            )
                        ),
                        cwd=cwd,
                        env=profile.env,
                        provider_session_id_factory=session_id_factory,
                        resume_initial=bool(warm_id),
                        tree_factory=coop_start.guarded_prepare_tree,
                    )
                )
            elif provider == "grok":
                state_dir, state_cleanup = (
                    coop_provider_workers.prepare_resumed_state(
                        workspace,
                        provider,
                        env=environment,
                    )
                )
                profile = build_profile(
                    provider,
                    [resolved[0], "agent", "stdio"],
                    coop_capabilities.MANIFESTS["board_core"],
                    env=environment,
                    workspace=workspace,
                    run_dir=run_dir,
                    servers={},
                    provider_state_dir=state_dir,
                )
                workers[provider] = (
                    coop_resident_workers.GrokAcpWorker(
                        argv=profile.argv,
                        cwd=cwd,
                        env=profile.env,
                        tree_factory=coop_start.guarded_prepare_tree,
                    )
                )
            else:
                base_argv = [resolved[0], "app-server"]
                base_argv.extend(
                    coop_start._codex_shell_environment_args(environment)
                )
                profile = build_profile(
                    provider,
                    base_argv,
                    coop_capabilities.MANIFESTS["board_core"],
                    env=environment,
                    workspace=workspace,
                    run_dir=run_dir,
                    servers={},
                )
                workers[provider] = (
                    coop_provider_workers.CodexAppServerWorker(
                        argv=profile.argv,
                        cwd=cwd,
                        env=profile.env,
                        tree_factory=coop_start.guarded_prepare_tree,
                    )
                )
            cleanups.append(profile.cleanup)
            if state_cleanup is not None:
                cleanups.append(state_cleanup)
        except Exception as exc:
            if profile is not None:
                try:
                    profile.cleanup()
                except Exception:
                    cleanups.append(profile.cleanup)
            if state_cleanup is not None:
                try:
                    state_cleanup()
                except Exception:
                    cleanups.append(state_cleanup)
            workers.pop(provider, None)
            unavailable[provider] = type(exc).__name__
    pool = coop_workers.WorkerPool(workers) if workers else None
    return pool, tuple(cleanups), unavailable


class HerdrMirrorSetupError(RuntimeError):
    """Fail-closed mirror setup result retained for runner finalization."""

    def __init__(self, *, cleanup_failed):
        self.reason = (
            "herdr_cleanup_failed"
            if cleanup_failed
            else "herdr_setup_failed"
        )
        super().__init__(self.reason)


class HerdrMirrorCleanupError(RuntimeError):
    """An owned provider mirror could not be closed before demotion."""

    reason = "herdr_cleanup_failed"

    def __init__(self, provider):
        self.provider = provider
        super().__init__(f"{self.reason}:{provider}")


class _HerdrMirrorOwnership:
    """Locked ownership known to one autonomous runner.

    Snapshots are detached copies so concurrent status publication never sees
    a workspace without its pane map, or a pane map without its workspace.
    """

    def __init__(self):
        self._workspace = None
        self._panes = {}
        self._lock = threading.RLock()

    def snapshot(self):
        with self._lock:
            return self._workspace, dict(self._panes)

    def record(self, provider, pane_id, *, workspace_id):
        workspace_id = coop_runner_status.canonical_herdr_id(workspace_id)
        pane_id = coop_runner_status.canonical_herdr_id(pane_id)
        if workspace_id is None or pane_id is None:
            raise ValueError("Herdr ownership requires canonical opaque IDs")
        if not isinstance(provider, str) or not provider:
            raise ValueError("Herdr ownership requires a provider")
        with self._lock:
            if (
                self._workspace is not None
                and self._workspace != workspace_id
            ):
                raise ValueError("Herdr ownership workspace changed")
            self._workspace = workspace_id
            self._panes[provider] = pane_id

    def pane_for(self, provider):
        with self._lock:
            return self._panes.get(provider)

    def pane_ids(self):
        with self._lock:
            return tuple(self._panes.values())

    def forget(self, provider, *, pane_id=None):
        with self._lock:
            current = self._panes.get(provider)
            if current is None or (
                pane_id is not None and current != pane_id
            ):
                return False
            self._panes.pop(provider)
            if not self._panes:
                self._workspace = None
            return True

    def forget_all(self):
        with self._lock:
            self._panes.clear()
            self._workspace = None


def teardown_owned_herdr_mirror(ownership, provider, *, adapter):
    """Close only ``provider``'s mirror, retaining ownership on failure."""
    pane_id = ownership.pane_for(provider)
    if pane_id is None:
        return False
    cleanup_error = None
    for _attempt in range(2):
        try:
            adapter.teardown((pane_id,))
            cleanup_error = None
            break
        except Exception as exc:
            cleanup_error = exc
    if cleanup_error is not None:
        raise HerdrMirrorCleanupError(provider) from cleanup_error
    ownership.forget(provider, pane_id=pane_id)
    return True


def prepare_opt_in_herdr_mirrors(
    enabled,
    effective_persistent_providers,
    *,
    session_participants,
    run_id,
    trace_path,
    cwd,
    board_path,
    environ,
    adapter=None,
    ownership=None,
):
    """Atomically open persistent-provider mirrors in frozen session order."""
    ownership = ownership or _HerdrMirrorOwnership()
    ordered_providers = tuple(
        provider
        for provider in session_participants
        if provider in effective_persistent_providers
    )
    if not enabled or not ordered_providers:
        return ownership, adapter

    try:
        from agent_coop import coop_herdr
    except Exception as exc:
        raise HerdrMirrorSetupError(cleanup_failed=False) from exc
    herdr_env = coop_herdr.herdr_cli_env(environ)
    if adapter is None:
        try:
            adapter = coop_herdr.HerdrAdapter(
                workspace=cwd,
                environ=herdr_env,
            )
        except Exception as exc:
            raise HerdrMirrorSetupError(cleanup_failed=False) from exc

    spawned_panes = []
    try:
        for provider in ordered_providers:
            pane_id = adapter.spawn_mirror(
                run_id,
                provider,
                cwd=cwd,
                trace_path=trace_path,
                board_path=board_path,
                environ=herdr_env,
            )
            spawned_panes.append(pane_id)
            workspace_id, _panes = ownership.snapshot()
            if workspace_id is None:
                workspace_id = adapter.workspace_id(run_id)
            ownership.record(
                provider,
                pane_id,
                workspace_id=workspace_id,
            )
    except BaseException as setup_error:
        cleanup_error = None
        # Teardown also retries adapter-owned pending cleanup when no
        # pane ID was returned.  Passing only returned IDs preserves its
        # explicit ownership boundary.
        for _attempt in range(2):
            try:
                adapter.teardown(tuple(spawned_panes))
                cleanup_error = None
                break
            except BaseException as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise HerdrMirrorSetupError(cleanup_failed=True) from cleanup_error
        ownership.forget_all()
        if isinstance(setup_error, (KeyboardInterrupt, SystemExit)):
            raise
        if not isinstance(setup_error, Exception):
            raise
        raise HerdrMirrorSetupError(cleanup_failed=False) from setup_error
    return ownership, adapter


def _build_main_parser_base():
    """Build the stable first half of the autonomous-runner CLI parser."""
    ap = argparse.ArgumentParser(description="wake-driven content-free coop runner")
    ap.add_argument("--db", default="board.db")
    ap.add_argument("--agents", help="comma list (default: probe claude,codex,grok)")
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--item", type=int, help="run exactly this item id")
    target.add_argument("--all", action="store_true", dest="all_items",
                        help="explicitly drain all active board items")
    ap.add_argument("--interval", type=float, default=3.0, help="idle poll seconds")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="per-turn seconds (a real agent turn: read board + act + write)")
    ap.add_argument("--max-turns", type=int, default=200)
    ap.add_argument("--max-idle-rounds", type=int, default=40,
                    help="idle polls with active work before declaring 'stalled' (needs human)")
    ap.add_argument("--max-noop-cycles", type=int, default=3,
                    help="full cycles with actionable agents but zero board progress "
                         "before 'stalled' (broken provider / no-op spin)")
    ap.add_argument(
        "--persistent-provider",
        action="append",
        choices=tuple(sorted(PERSISTENT_PROVIDERS)),
        default=[],
        help="opt in one provider to persistent worker reuse (repeatable)",
    )
    ap.add_argument(
        "--prompt-cache-provider",
        action="append",
        choices=tuple(sorted(PROMPT_CACHE_HINT_PROVIDERS)),
        default=[],
        help="opt in one provider to a prompt-cache reuse hint",
    )
    ap.add_argument(
        "--no-prompt-hydration", action="store_true",
        help="spawn turns with the bare content-free prefix instead of "
             "appending the spawn-time board snapshot",
    )
    return ap


def compiled_mesh_profile(
        admitted, plan, *, agent, failed_isolated=frozenset(),
        structured_report_providers=frozenset()):
    """Narrow an already-safe admission for one compiled mesh phase.

    Outbound text is promoted only for Claude, the one adapter verified for
    isolated schema output. Code-rendered transfers require no provider
    call. Codex and Grok outbound turns remain ordinary until their schema
    output is verified.
    """
    if not isinstance(admitted, coop_action_scheduler.DispatchProfile):
        return admitted
    if (
            not isinstance(
                plan,
                (
                    coop_mesh.MeshOutboundPlan,
                    coop_mesh.MeshTransferPlan,
                    coop_mesh.MeshComposePlan,
                ),
            )
            or plan.sender != agent
            or plan.action_fingerprint != admitted.action_fingerprint
            or (agent, plan.action_fingerprint) in failed_isolated):
        return admitted
    if isinstance(plan, coop_mesh.MeshComposePlan):
        mode = (
            "isolated_structured_mesh_report"
            if agent in structured_report_providers
            else "compiled_mesh_report"
        )
        surface = "write"
    elif isinstance(plan, coop_mesh.MeshOutboundPlan):
        if agent != "claude":
            return admitted
        mode = "isolated_structured_mesh_questions"
        surface = "none"
    else:
        mode = "compiled_mesh_transfer"
        surface = "none"
    refined = coop_action_scheduler.DispatchProfile(
        lane=admitted.lane,
        workspace_surface=surface,
        execution_mode=mode,
        action_fingerprint=admitted.action_fingerprint,
    )
    return refined if profile_refinement_allowed(admitted, refined) else admitted


def compiled_mesh_review_profile(
        admitted, plan, *, agent, failed_isolated=frozenset()):
    """Narrow validated mesh review mechanics without widening admission."""
    if not isinstance(admitted, coop_action_scheduler.DispatchProfile):
        return admitted
    if (
            not isinstance(
                plan,
                (
                    coop_mesh.MeshReviewRequestPlan,
                    coop_mesh.MeshReviewPlan,
                ),
            )
            or plan.action_fingerprint != admitted.action_fingerprint
            or (agent, plan.action_fingerprint) in failed_isolated):
        return admitted
    if isinstance(plan, coop_mesh.MeshReviewRequestPlan):
        if plan.owner != agent:
            return admitted
        mode = "compiled_mesh_review_request"
    else:
        if plan.reviewer != agent or agent != "claude":
            return admitted
        mode = "isolated_structured_mesh_review"
    refined = coop_action_scheduler.DispatchProfile(
        lane=admitted.lane,
        workspace_surface="read",
        execution_mode=mode,
        action_fingerprint=admitted.action_fingerprint,
    )
    return refined if profile_refinement_allowed(admitted, refined) else admitted


def mesh_questions_decision_request(plan, *, item, provider):
    """Build a text-only request; recipients and authority stay runner-owned."""
    if (
            not isinstance(plan, coop_mesh.MeshOutboundPlan)
            or provider != plan.sender):
        return None
    try:
        return coop_decisions.make_decision_request(
            decision_kind="compose_mesh_questions",
            provider=provider,
            agent_id=plan.sender,
            item_id=plan.item_id,
            action_fingerprint=plan.action_fingerprint,
            prompt=(
                coop_prompt_cache.structured_mesh_questions_decision_prompt(
                    sender=plan.sender,
                    recipients=plan.recipients,
                    item=item,
                )
            ),
            recipients=plan.recipients,
        )
    except Exception:
        return None


def mesh_report_decision_request(plan, *, item, provider):
    """Build a narrative-only request without paths, refs, or claim secrets."""
    if (
            not isinstance(plan, coop_mesh.MeshComposePlan)
            or provider != plan.sender):
        return None
    exchanges = tuple({
        "sender": entry.sender,
        "recipient": entry.recipient,
        "question": entry.question,
        "answer": entry.answer,
    } for entry in plan.exchanges)
    try:
        return coop_decisions.make_decision_request(
            decision_kind="compose_mesh_report_sections",
            provider=provider,
            agent_id=plan.sender,
            item_id=plan.item_id,
            action_fingerprint=plan.action_fingerprint,
            prompt=coop_prompt_cache.structured_mesh_report_decision_prompt(
                sender=plan.sender,
                exchanges=exchanges,
                item=item,
            ),
        )
    except Exception:
        return None


def mesh_review_decision_request(plan, *, item, provider):
    """Build the one bounded independent verdict with no board authority."""
    if (
            not isinstance(plan, coop_mesh.MeshReviewPlan)
            or provider != plan.reviewer):
        return None
    try:
        return coop_decisions.make_decision_request(
            decision_kind="review_mesh_report",
            provider=provider,
            agent_id=plan.reviewer,
            item_id=plan.item_id,
            action_fingerprint=plan.action_fingerprint,
            prompt=coop_prompt_cache.structured_mesh_review_decision_prompt(
                reviewer=plan.reviewer,
                report_text=plan.evidence.report_text,
                item=item,
            ),
            review_id=plan.review_id,
        )
    except Exception:
        return None


def mesh_plan_still_current(
        conn, *, plan, agent, session_id, lease_seconds):
    """Recompile every contract, identity, row, event, and action invariant."""
    try:
        action = coopdb.status(
            conn,
            agent,
            session_id=session_id,
            action_lease_seconds=lease_seconds,
            item_id=plan.item_id,
        )["next_action"]
        if (
                coop_action_scheduler.action_fingerprint(action)
                != plan.action_fingerprint):
            return False
        current = coop_mesh.compile_mesh_phase(
            conn,
            item=dict(coopdb.item_show(conn, plan.item_id)),
            action=action,
            agent=agent,
        )
        return current == plan
    except Exception:
        return False


def mesh_review_request_still_current(
        conn, *, plan, agent, session_id, lease_seconds, workspace):
    """Recompile a named review request immediately before its board write."""
    try:
        action = coopdb.status(
            conn,
            agent,
            session_id=session_id,
            action_lease_seconds=lease_seconds,
            item_id=plan.item_id,
        )["next_action"]
        if (
                coop_action_scheduler.action_fingerprint(action)
                != plan.action_fingerprint):
            return False
        current = coop_mesh.compile_mesh_review_request(
            conn,
            item=dict(coopdb.item_show(conn, plan.item_id)),
            action=action,
            agent=agent,
            workspace=workspace,
        )
        return current == plan
    except Exception:
        return False


def mesh_review_still_current(
        conn, *, plan, agent, session_id, lease_seconds, workspace):
    """Recompile review evidence, identity, claim, and action before verdict."""
    try:
        action = coopdb.status(
            conn,
            agent,
            session_id=session_id,
            action_lease_seconds=lease_seconds,
            item_id=plan.item_id,
        )["next_action"]
        if (
                coop_action_scheduler.action_fingerprint(action)
                != plan.action_fingerprint):
            return False
        current = coop_mesh.compile_mesh_review(
            conn,
            item=dict(coopdb.item_show(conn, plan.item_id)),
            action=action,
            agent=agent,
            workspace=workspace,
        )
        return current == plan
    except Exception:
        return False


def _mesh_question_pairs(plan, value):
    if (
            not isinstance(plan, coop_mesh.MeshOutboundPlan)
            or not isinstance(value, Mapping)
            or set(value) != {"questions"}
            or not isinstance(value["questions"], list)
            or len(value["questions"]) != len(plan.recipients)):
        return None
    pairs = []
    for entry, recipient in zip(value["questions"], plan.recipients):
        if (
                not isinstance(entry, Mapping)
                or set(entry) != {"recipient", "question"}
                or entry["recipient"] != recipient
                or not isinstance(entry["question"], str)):
            return None
        question = entry["question"].strip()
        try:
            question_bytes = len(question.encode("utf-8"))
        except UnicodeEncodeError:
            return None
        if (
                not question
                or len(question) > coop_decisions.MAX_QUESTION_CHARS
                or question_bytes > coopdb.MAX_QUESTION_TEXT_BYTES):
            return None
        pairs.append((recipient, question))
    return tuple(pairs)


def postcommit_mesh_questions(
        conn, *, plan, value, agent, session_id, lease_seconds):
    """Atomically recompile and post the runner-fixed recipient batch."""
    pairs = _mesh_question_pairs(plan, value)
    if pairs is None or plan.sender != agent:
        return None

    def _commit(current):
        if not mesh_plan_still_current(
                current,
                plan=plan,
                agent=agent,
                session_id=session_id,
                lease_seconds=lease_seconds):
            return None
        return coopdb.needs_input_batch(
            current,
            claim_id=plan.claim_id,
            session_id=session_id,
            questions=pairs,
        )

    try:
        return coopdb.mutate(conn, _commit)
    except Exception:
        return None


def postcommit_mesh_transfer(
        conn, *, plan, agent, session_id, lease_seconds):
    """Atomically recompile and create one entirely code-rendered handoff."""
    if not isinstance(plan, coop_mesh.MeshTransferPlan) or plan.sender != agent:
        return None

    def _commit(current):
        if not mesh_plan_still_current(
                current,
                plan=plan,
                agent=agent,
                session_id=session_id,
                lease_seconds=lease_seconds):
            return None
        return coopdb.create_handoff(
            current,
            claim_id=plan.claim_id,
            session_id=session_id,
            actor=agent,
            to_agent=plan.to_agent,
            reason=plan.reason,
            summary=plan.summary,
            completed=plan.completed,
            remaining=plan.remaining,
            risks=plan.risks,
            next_action=plan.next_action,
            proof_refs=plan.proof_refs,
        )

    try:
        return coopdb.mutate(conn, _commit)
    except Exception:
        return None


def postcommit_mesh_report(
        conn, *, plan, value, agent, session_id, lease_seconds, workspace):
    """Recompile, exclusively publish the report, and submit its receipt."""
    if not isinstance(plan, coop_mesh.MeshComposePlan) or plan.sender != agent:
        return None
    try:
        rendered = coop_mesh.render_mesh_report(plan, value)
        report_bytes = rendered.encode("utf-8")
        sections = coop_mesh.normalize_mesh_report_value(value)
        target = coop_mesh.resolve_mesh_report_target(
            workspace, plan.report_path)
    except (UnicodeEncodeError, ValueError):
        return None
    if target is None:
        return None
    created = False

    def _commit(current):
        nonlocal created
        if not mesh_plan_still_current(
                current,
                plan=plan,
                agent=agent,
                session_id=session_id,
                lease_seconds=lease_seconds):
            return None
        if coop_mesh.resolve_mesh_report_target(
                workspace, plan.report_path) != target:
            return None
        if not coop_mesh.write_mesh_report_exclusive(target, report_bytes):
            return None
        created = True
        proof_refs = tuple(
            f"event:{exchange.answer_event_id}"
            for exchange in plan.exchanges
        )
        receipt_id = coopdb.submit_receipt(
            current,
            claim_id=plan.claim_id,
            session_id=session_id,
            actor=agent,
            path=str(target),
            summary=sections["receipt_summary"],
            proof=coop_mesh.MESH_RECEIPT_PROOF,
            proof_refs=proof_refs,
        )
        return {"receipt_id": receipt_id, "path": str(target)}

    try:
        result = coopdb.mutate(conn, _commit)
    except Exception:
        result = None
    if result is None and created:
        try:
            target.unlink()
        except OSError:
            pass
    return result


def postcommit_mesh_review_request(
        conn, *, plan, agent, session_id, lease_seconds, workspace):
    """Atomically revalidate evidence and request the named Claude review."""
    if (
            not isinstance(plan, coop_mesh.MeshReviewRequestPlan)
            or plan.owner != agent):
        return None

    def _commit(current):
        if not mesh_review_request_still_current(
                current,
                plan=plan,
                agent=agent,
                session_id=session_id,
                lease_seconds=lease_seconds,
                workspace=workspace):
            return None
        review_id = coopdb.request_review(
            current,
            claim_id=plan.claim_id,
            session_id=session_id,
            actor=agent,
            reviewer=plan.reviewer,
        )
        return {"review_id": review_id, "receipt_id": plan.receipt_id}

    try:
        return coopdb.mutate(conn, _commit)
    except Exception:
        return None


def postcommit_mesh_review(
        conn, *, plan, value, agent, session_id, lease_seconds, workspace):
    """Atomically revalidate the evidence packet and submit one verdict."""
    if (
            not isinstance(plan, coop_mesh.MeshReviewPlan)
            or plan.reviewer != agent):
        return None
    try:
        item = dict(coopdb.item_show(conn, plan.item_id))
    except Exception:
        return None
    request = mesh_review_decision_request(
        plan, item=item, provider=agent)
    normalized = coop_decisions.validate_decision_value(request, value)
    if normalized is None:
        return None

    def _commit(current):
        if not mesh_review_still_current(
                current,
                plan=plan,
                agent=agent,
                session_id=session_id,
                lease_seconds=lease_seconds,
                workspace=workspace):
            return None
        target = coop_mesh.resolve_existing_mesh_report_target(
            workspace, plan.report_path)
        if target is None:
            return None
        before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if before_hash != plan.evidence.report_sha256:
            return None
        result = coopdb.submit_verdict(
            current,
            claim_id=plan.claim_id,
            session_id=session_id,
            actor=agent,
            verdict=normalized["verdict"],
            body=normalized["body"],
        )
        after_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if after_hash != before_hash:
            raise RuntimeError("mesh evidence changed during verdict")
        return result

    try:
        return coopdb.mutate(conn, _commit)
    except Exception:
        return None


def main(argv=None, *, herdr_adapter=None):
    ap = _build_main_parser_base()
    ap.add_argument(
        "--token-efficient", action="store_true",
        help="opt in to the bounded action grammar and prompt-level "
             "post-write exit contract",
    )
    ap.add_argument(
        "--no-mechanical-precommit", action="store_true",
        help="do not let the runner pre-execute judgment-free claim "
             "commands before spawning the turn",
    )
    ap.add_argument(
        "--no-persistent-workers", action="store_true",
        help="cold provider turns every time (debug mode; persistent "
             "workers are the default)",
    )
    ap.add_argument(
        "--no-prompt-cache", action="store_true",
        help="disable the default Claude prompt-cache reuse hint",
    )
    ap.add_argument(
        "--fresh-sessions", action="store_true",
        help="do not resume claude's provider conversation from the "
             "previous run on this board",
    )
    # Handoff contract: cmd_start has already run the silent adapter
    # preflight.  The mirror path consumes this boolean to create mirrors;
    # it must not change worker, board, sidecar, or dashboard behavior.
    ap.add_argument("--herdr", action="store_true", help=argparse.SUPPRESS)
    # Forwarded from `coop start --inherit-env`: provider children and the
    # runner's own board commands get the full parent environment instead
    # of coop_start.tool_turn_env's allowlist.
    ap.add_argument(
        "--inherit-env", action="store_true", help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--no-continuous-dispatch", action="store_true",
        help="restore the barriered batch scheduler (wait for every "
             "in-flight turn before re-polling the board)",
    )
    ap.add_argument(
        "--no-structured-answers", action="store_true",
        help="answer questions through full tool-enabled turns instead of "
             "schema-constrained one-shots with a runner postcommit",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        selftest()
        print("coop_autonomous selftest: OK")
        return 0

    if args.item is None and not args.all_items:
        ap.error("one of --item or --all is required")
    if args.item is not None and args.item <= 0:
        ap.error("--item must be a positive integer")

    board_path = str(pathlib.Path(args.db).resolve())
    trace_path = (
        os.environ.get("COOP_RUN_TRACE_PATH")
        or coop_turn_trace.default_trace_path(board_path)
    )
    owned_herdr_mirrors = _HerdrMirrorOwnership()
    frozen_caller_pane = coop_runner_status.caller_pane_from_env(os.environ)

    def herdr_status_fields():
        workspace_id, panes = owned_herdr_mirrors.snapshot()
        metadata = coop_runner_status.canonical_herdr_metadata(
            workspace=workspace_id,
            panes=panes if workspace_id is not None else None,
            caller_pane=frozen_caller_pane,
        )
        return {"herdr": metadata} if metadata is not None else {}

    # Always publish a status sidecar. Dashboard launches set
    # COOP_RUN_STATUS_PATH; bare `coop start` derives one beside the trace so
    # the dashboard can discover and refresh past a prior finished run.
    status_path = (
        os.environ.get("COOP_RUN_STATUS_PATH")
        or coop_runner_status.status_path_for_run_artifact(trace_path)
    )
    existing_status = coop_runner_status.read_status(status_path)
    status_started_at = (
        (existing_status or {}).get("started_at")
        or coop_runner_status.now())
    if existing_status is None:
        coop_runner_status.write_status(
            status_path,
            phase="starting",
            started_at=status_started_at,
            **herdr_status_fields(),
        )
    terminal_detail = {}

    def publish_status(phase, **fields):
        if phase == "terminal_detail":
            terminal_detail.clear()
            terminal_detail.update(fields)
            return
        if status_path:
            coop_runner_status.write_status(
                status_path, phase=phase, started_at=status_started_at,
                **fields, **herdr_status_fields())
    run_id = pathlib.Path(trace_path).name.removesuffix(".trace.jsonl")
    trace = coop_turn_trace.TurnTrace(
        trace_path, run_id=run_id, item_id=args.item)
    cwd = str(workspace_dir(board_path))         # what agents edit; NOT the install dir
    try:
        # Board-adjacent, not workspace-adjacent: capability config is
        # per-board operator state, and a target repo must not be asked to
        # carry Co-op config files in its root.
        capability_config = coop_capabilities.load_local_config(
            _board_dir(board_path) / "coop-capabilities.local.json"
        )
    except coop_capabilities.CapabilityConfigError as exc:
        publish_status(
            "failed",
            reason="capability_config_invalid",
            turns=0,
        )
        print(f"invalid capability configuration: {exc}")
        return 2
    requested = args.agents.split(",") if args.agents else list(coop_start.DEFAULT_AGENTS)
    resolution = coop_start.resolve_participants(requested, coop_start.probe_agent)
    available = resolution["available"]
    if len(available) < 2:
        publish_status(
            "failed", reason="insufficient_providers", turns=0)
        print("multi-provider co-op requires at least two available agents; found",
              len(available), "— probe results:",
              [s["reason"] for s in resolution.get("skipped", [])])
        return 2

    conn = coopdb.connect(board_path, require_current=True)
    recipe_item = {}
    if args.item is not None:
        target_row = conn.execute(
            "SELECT * FROM items WHERE id=?", (args.item,)).fetchone()
        if target_row is None:
            publish_status("failed", reason="item_not_found", turns=0)
            print(f"no item {args.item}")
            conn.close()
            return 2
        if target_row["status"] == "done":
            publish_status("failed", reason="item_already_done", turns=0)
            print(f"item {args.item} is already done")
            conn.close()
            return 2
        available = coop_routing.soft_order_participants(
            available, dict(target_row))
        recipe_item = dict(coopdb.item_show(conn, args.item))
    try:
        recipe = coop_recipes.compile_recipe(recipe_item, available)
    except coop_recipes.RecipeBlocked as exc:
        publish_status("failed", reason="recipe_blocked", turns=0)
        print(f"workflow recipe blocked: {exc.reason}")
        conn.close()
        return 2
    active_participants = list(recipe.active_participants)
    session_participants = list(
        recipe.active_participants + recipe.reserve_participants
    )
    persistent_requested = (
        [] if args.no_persistent_workers
        else (args.persistent_provider
              or default_persistent_providers(session_participants))
    )
    prompt_cache_requested = (
        [] if args.no_prompt_cache
        else (args.prompt_cache_provider
              or default_prompt_cache_providers(session_participants))
    )
    try:
        persistent_providers = normalize_persistent_providers(
            persistent_requested,
            participants=session_participants,
        )
    except ValueError as exc:
        publish_status(
            "failed",
            reason="persistent_provider_invalid",
            turns=0,
        )
        print(str(exc))
        conn.close()
        return 2
    try:
        prompt_cache_providers = normalize_prompt_cache_providers(
            prompt_cache_requested,
            participants=session_participants,
        )
    except ValueError as exc:
        publish_status(
            "failed",
            reason="prompt_cache_provider_invalid",
            turns=0,
        )
        print(str(exc))
        conn.close()
        return 2
    if args.dry_run:
        print("participants:", ", ".join(active_participants))
        if recipe.reserve_participants:
            print("reserve:", ", ".join(recipe.reserve_participants))
        print("recipe:", recipe.name)
        if persistent_providers:
            print(
                "persistent providers:",
                ", ".join(sorted(persistent_providers)),
            )
        if prompt_cache_providers:
            print(
                "prompt-cache providers:",
                ", ".join(sorted(prompt_cache_providers)),
            )
        print("target:", f"item {args.item}" if args.item is not None else "all")
        print("all_done:", board_all_done(conn, item_id=args.item))
        publish_status("finished", reason="dry_run", turns=0)
        conn.close()
        return 0

    stop_flag = stop_flag_path(board_path)
    try:
        pathlib.Path(stop_flag).unlink()         # a stale flag must not pre-stop us
    except OSError:
        pass

    long_lease = lease_for_timeout(args.timeout)
    assert long_lease >= int(args.timeout), (long_lease, args.timeout)
    session_runtime = max(3600, int(args.timeout) * max(1, args.max_turns))
    # Resolve every provider launcher once, outside the workspace, and hold
    # that answer for the whole run (released in the run's finally).
    coop_start.pin_cli_resolution(workspace=cwd)
    sessions = {}
    for name in session_participants:
        sid = f"coop-auto-{name}-{uuid.uuid4().hex[:12]}"
        argv = coop_start.resolved_provider_argv(name)
        coopdb.insert_session(
            conn, session_id=sid, agent_id=name, provider=name,
            command=argv, cwd=cwd,
            max_runtime_s=session_runtime, grace_s=10, stdin_isatty=False)
        sessions[name] = sid

    # Kickoff already done by human (items seeded). Mark run start for the
    # mid-run human-mutation gate (happy path: zero human events after this).
    host = recipe.lead
    run_mark = coopdb.mark_autonomous_run(
        conn, phase="started", session_id=sessions[host], agent_id=host,
        payload={
            "agents": active_participants,
            "reserve_agents": list(recipe.reserve_participants),
            "board": board_path,
            "item_id": args.item,
            "workflow_recipe": recipe.as_payload(),
            "persistent_providers_requested": sorted(
                persistent_providers
            ),
            "prompt_cache_providers_requested": sorted(
                prompt_cache_providers
            ),
            "continuous_dispatch": not args.no_continuous_dispatch,
            "structured_answers": not args.no_structured_answers,
            **({"token_efficient": True} if args.token_efficient else {}),
            "child_env": "inherit" if args.inherit_env else "allowlist",
        })
    run_started_at = run_mark["at"]
    run_started_event_id = run_mark["event_id"]
    worker_pool, worker_cleanups, worker_unavailable = (
        prepare_opt_in_worker_pool(
            persistent_providers,
            sessions=sessions,
            board_path=board_path,
            cwd=cwd,
            run_dir=pathlib.Path(trace_path).parent,
            action_lease_seconds=long_lease,
            item_id=args.item,
            fresh_sessions=args.fresh_sessions,
            prompt_cache_providers=prompt_cache_providers,
            inherit_env=args.inherit_env,
        )
    )
    effective_persistent_providers = (
        persistent_providers - set(worker_unavailable)
    )
    persistent_providers_lock = threading.Lock()
    turn_cleanup_registry = coop_workers.RunCleanupRegistry()
    for provider, reason in sorted(worker_unavailable.items()):
        print(
            f"  warn: {provider} worker unavailable ({reason}); "
            "using cold turns"
        )
    active_herdr_adapter = herdr_adapter
    trace.emit(
        "run_started",
        details={
            "workflow_recipe": recipe.name,
            "persistent_providers": sorted(
                effective_persistent_providers
            ),
        },
    )
    print(f"  run_started_at={run_started_at} event_id={run_started_event_id} "
          f"(mid-run human mutations fail gate)")

    def prepare_recipe_cycle():
        nonlocal recipe
        if recipe.name != "quick_two":
            return None
        if args.item is None or not recipe.reserve_participants:
            return "recipe_blocked"
        reserve = recipe.reserve_participants[0]
        promotion_conn = coopdb.connect(
            board_path,
            require_current=True,
        )
        try:
            item = dict(coopdb.item_show(promotion_conn, args.item))
            rows = promotion_conn.execute(
                "SELECT event_id, event_type, payload_json FROM events "
                "WHERE item_id=? AND event_id>? ORDER BY event_id",
                (args.item, run_started_event_id),
            ).fetchall()
            events = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, ValueError):
                    payload = {}
                events.append({
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "payload": payload,
                })
            reserve_action = coop_watcher.next_action(
                board_path,
                reserve,
                lease_seconds=long_lease,
                item_id=args.item,
            )
            reason = coop_recipes.detect_promotion_reason(
                recipe,
                current_contract_version=item["contract_version"],
                events=events,
                reserve_action=reserve_action,
            )
            if reason is None:
                return None
            promoted = coop_recipes.promote_to_standard_three(
                recipe,
                reason,
            )
            coopdb.mark_workflow_recipe_promotion(
                promotion_conn,
                item_id=args.item,
                run_started_event_id=run_started_event_id,
                session_id=sessions[host],
                agent_id=host,
                previous=recipe.as_payload(),
                current=promoted.as_payload(),
                reason=reason,
            )
            recipe = promoted
            active_participants[:] = list(
                promoted.active_participants
            )
            print(f"  recipe promoted to standard_three: {reason}")
            return None
        except Exception as exc:
            print(
                "  workflow recipe promotion failed: "
                f"{type(exc).__name__}"
            )
            return "recipe_blocked"
        finally:
            promotion_conn.close()

    prompt = content_free_turn(
        long_lease,
        token_efficient=args.token_efficient,
    )
    pending_actions = {}
    ready_observed = {}
    pending_actions_lock = threading.Lock()
    isolated_decision_failures = set()
    isolated_decision_failures_lock = threading.Lock()

    def profile_fn(candidate):
        with isolated_decision_failures_lock:
            failed = frozenset(isolated_decision_failures)
        return dispatch_profile(
            candidate,
            structured_answers=not args.no_structured_answers,
            structured_decisions=args.token_efficient,
            failed_isolated=failed,
        )

    def remember_isolated_decision_failure(agent, fingerprint):
        with isolated_decision_failures_lock:
            isolated_decision_failures.add((agent, fingerprint))

    def actionable_fn(agent):
        action = coop_watcher.next_action(
            board_path, agent, lease_seconds=long_lease,
            item_id=args.item)
        kind = action.get("kind") if isinstance(action, dict) else IDLE
        if kind and kind != IDLE:
            with pending_actions_lock:
                pending_actions[agent] = action
                # Critical-path instrumentation: first observation of this
                # complete action becoming actionable. ready -> dispatch is
                # the scheduler delay the trace could not previously see.
                ready_key = coop_action_scheduler.action_fingerprint(action)
                if ready_observed.get(agent) != ready_key:
                    ready_observed[agent] = ready_key
                    _emit_trace(
                        trace,
                        "action_became_ready",
                        agent=agent,
                        provider=agent,
                        action=kind,
                        details={
                            "target_id": action.get("target_id"),
                            "action_fingerprint": ready_key,
                        },
                    )
            return action
        with pending_actions_lock:
            pending_actions.pop(agent, None)
            ready_observed.pop(agent, None)
        return IDLE

    def progress_on(connection):
        if args.item is not None:
            return coopdb.item_board_probe(connection, args.item)
        return coopdb.board_probe(connection)

    def progress_fn():
        return progress_on(conn)

    def _agent_action_env(agent, action):
        """The routed agent's bound-session environment for a runner-side
        command (mechanical precommit / structured postcommit)."""
        env_item = (
            action.get("item_id") if isinstance(action, dict) else None
        )
        if env_item is None:
            env_item = args.item
        return agent_action_env(
            agent,
            session_id=sessions[agent],
            board_path=board_path,
            lease_seconds=long_lease,
            item_id=env_item,
            inherit=args.inherit_env,
        )

    def _mechanical_precommit(agent, action, turn_id):
        """Execute one judgment-free command as the routed agent before its
        turn spawns, so the model wakes already holding the lane.

        Returns ("advanced", refreshed_action) when follow-on work exists,
        ("satisfied", None) when the command was the whole turn (nothing
        left to judge - the spawn can be skipped), ("stale", None) when the
        board moved between derivation and launch, or ("failed", None) to
        run the original turn unchanged; failure here is never fatal, the
        model handles the original action through the normal rejection
        reflex.
        """
        command = [
            sys.executable if str(tok) == "python" else str(tok)
            for tok in action["command"]
        ]
        env = _agent_action_env(agent, action)
        try:
            proc = subprocess.run(
                command, env=env, cwd=cwd, capture_output=True,
                text=True, timeout=60,
            )
        except Exception as exc:
            print(f"  warn: precommit {agent}: {type(exc).__name__}")
            return "failed", None
        _emit_trace(
            trace,
            "mechanical_precommit",
            turn_id=turn_id,
            agent=agent,
            provider=agent,
            action=action.get("kind"),
            details={"exit_code": proc.returncode},
        )
        refreshed = coop_watcher.next_action(
            board_path, agent, lease_seconds=long_lease,
            item_id=args.item)
        if proc.returncode != 0:
            if (isinstance(refreshed, dict)
                    and not actions_equivalent(action, refreshed)):
                # The board moved between derivation and launch; do not
                # spend a model turn proving it - the scheduler
                # re-derives next tick.
                return "stale", None
            return "failed", None
        kind = (
            refreshed.get("kind")
            if isinstance(refreshed, dict)
            else None
        )
        if kind and kind != IDLE:
            return "advanced", refreshed
        return "satisfied", None

    def _take_turn_with_conn(agent, hint, action, profile, turn_conn):
        if profile is None:
            profile = profile_fn(
                coop_action_scheduler.ActionCandidate(
                    agent=agent,
                    hint=hint,
                    action=action if isinstance(action, dict) else {},
                )
            )
        effective_profile = profile
        actor_probe_state = {
            "failed": False,
            "last": None,
            "warned": False,
            "live_mutation_observed": False,
        }

        def actor_progress():
            """Fail-closed, turn-local progress attributed to this session."""
            if actor_probe_state["failed"]:
                return actor_probe_state["last"]
            try:
                token = coopdb.actor_event_probe(
                    turn_conn,
                    session_id=sessions[agent],
                    # A targeted product run stays item-scoped. An explicit
                    # --all run attributes any event from this unique session.
                    item_id=args.item,
                )
            except Exception as exc:
                actor_probe_state["failed"] = True
                if not actor_probe_state["warned"]:
                    actor_probe_state["warned"] = True
                    print(
                        f"  warn: actor progress probe {agent}: "
                        f"{type(exc).__name__}; failing closed for this turn"
                    )
                return actor_probe_state["last"]
            actor_probe_state["last"] = token
            return token

        before = actor_progress()

        def provider_progress_probe():
            """Actor probe wrapper that records live first-write observation."""
            token = actor_progress()
            if (
                    not actor_probe_state["failed"]
                    and before is not None
                    and token is not None
                    and token != before):
                actor_probe_state["live_mutation_observed"] = True
            return token

        turn_id = uuid.uuid4().hex
        precommit_satisfied = False
        precommit_stale = False
        precommit_profile_refresh = False
        if (not args.no_mechanical_precommit
                and isinstance(action, dict)
                and action.get("kind") == hint
                and mechanical_precommit_eligible(action)):
            state, refreshed = _mechanical_precommit(agent, action, turn_id)
            if state == "advanced":
                # The runner holds the lane now; the turn wakes on the
                # follow-on action. Re-snapshot progress so the precommit
                # write never masks a no-op model turn.
                action = refreshed
                hint = action.get("kind") or hint
                before = actor_progress()
                actor_probe_state["live_mutation_observed"] = False
                refreshed_profile = profile_fn(
                    coop_action_scheduler.ActionCandidate(
                        agent=agent,
                        hint=hint,
                        action=action,
                    )
                )
                if profile_refinement_allowed(
                        profile,
                        refreshed_profile):
                    effective_profile = refreshed_profile
                else:
                    precommit_profile_refresh = True
            elif state == "satisfied":
                # The command was the whole turn (e.g. gated completion);
                # nothing is left to judge, so no model spawns. The
                # precommit write itself is this turn's board progress.
                precommit_satisfied = True
            elif state == "stale":
                precommit_stale = True
        turn_item_id = (
            action.get("item_id")
            if isinstance(action, dict)
            else None
        )
        if turn_item_id is None:
            turn_item_id = args.item
        decision = None
        item = None
        mesh_plan = None
        mesh_review_plan = None
        resolution_error = None
        if not isinstance(action, dict) or action.get("kind") != hint:
            resolution_error = "cached actionable envelope is unavailable"
        elif turn_item_id is None:
            resolution_error = "action does not identify an item"
        else:
            try:
                item = dict(coopdb.item_show(turn_conn, turn_item_id))
                decision = coop_capabilities.resolve_capability(
                    action,
                    item,
                    capability_config,
                )
            except Exception as exc:
                resolution_error = (
                    "capability resolution failed: "
                    f"{type(exc).__name__}"
                )

        if (
                args.token_efficient
                and resolution_error is None
                and item is not None
                and isinstance(action, dict)):
            mesh_plan = coop_mesh.compile_mesh_phase(
                turn_conn,
                item=item,
                action=action,
                agent=agent,
            )
            if mesh_plan is None:
                mesh_review_plan = coop_mesh.compile_mesh_review_request(
                    turn_conn,
                    item=item,
                    action=action,
                    agent=agent,
                    workspace=cwd,
                )
            if mesh_plan is None and mesh_review_plan is None:
                mesh_review_plan = coop_mesh.compile_mesh_review(
                    turn_conn,
                    item=item,
                    action=action,
                    agent=agent,
                    workspace=cwd,
                )
            with isolated_decision_failures_lock:
                failed_mesh = frozenset(isolated_decision_failures)
            refined = compiled_mesh_profile(
                effective_profile,
                mesh_plan,
                agent=agent,
                failed_isolated=failed_mesh,
                structured_report_providers=(
                    STRUCTURED_MESH_REPORT_PROVIDERS
                ),
            )
            refined = compiled_mesh_review_profile(
                refined,
                mesh_review_plan,
                agent=agent,
                failed_isolated=failed_mesh,
            )
            if profile_refinement_allowed(effective_profile, refined):
                effective_profile = refined
            else:
                mesh_plan = None
                mesh_review_plan = None

        manifest = decision.manifest if decision is not None else None
        completion_probe_attached = bool(
            args.token_efficient
            and postwrite_completion_eligible(action)
        )
        with persistent_providers_lock:
            worker_mode = worker_mode_for(
                agent,
                effective_persistent_providers,
                manifest,
            )
        # Decided before the eligibility trace so the recorded mode is the
        # mode this turn actually runs.
        worker_mode = resident_completion_probe_mode(
            worker_mode,
            provider=agent,
            completion_probe_attached=completion_probe_attached,
        )
        capability_activation_mode = (
            "turn_lazy"
            if manifest is not None and manifest.external_servers
            else "none"
        )
        _emit_trace(
            trace,
            "action_eligible",
            turn_id=turn_id,
            agent=agent,
            provider=agent,
            action=hint,
            capability_set=manifest.name if manifest is not None else None,
            details={
                "worker_mode": worker_mode,
                "external_mcp": (
                    list(manifest.external_servers)
                    if manifest is not None
                    else []
                ),
                "capability_activation_mode": (
                    capability_activation_mode
                ),
                "workflow_recipe": recipe.name,
                "workspace_surface": effective_profile.workspace_surface,
                "execution_mode": effective_profile.execution_mode,
                "action_fingerprint": (
                    effective_profile.action_fingerprint
                ),
            },
        )

        if precommit_profile_refresh:
            res = {
                "agent": agent,
                "provider": agent,
                "ok": False,
                "exit": None,
                "note": "dispatch_profile_refresh_requeued",
                "classification": "dispatch_profile_refresh_requeued",
                "retry_after_board_refresh": True,
            }
        elif precommit_stale:
            res = {
                "agent": agent,
                "provider": agent,
                "ok": False,
                "exit": None,
                "note": "stale_action_requeued",
                "classification": "stale_action_requeued",
                "retry_after_board_refresh": True,
            }
        elif precommit_satisfied:
            res = {
                "agent": agent,
                "provider": agent,
                "ok": True,
                "exit": 0,
                "note": "mechanical_precommit_satisfied",
            }
        elif resolution_error is not None:
            res = {
                "agent": agent,
                "provider": agent,
                "ok": False,
                "exit": None,
                "note": f"capability_activation_failed: {resolution_error}",
                "classification": "capability_activation_failed",
                "retryable": False,
            }
        elif not decision.allowed:
            res = {
                "agent": agent,
                "provider": agent,
                "ok": False,
                "exit": None,
                "note": (
                    "capability_denied: "
                    f"{decision.legal_next_action}"
                ),
                "classification": decision.classification,
                "legal_next_action": decision.legal_next_action,
            }
        else:
            try:                                 # extend surviving claims first
                coopdb.renew_claims(
                    turn_conn,
                    session_id=sessions[agent],
                    lease_seconds=long_lease,
                )
            except (
                    coopdb.InvalidTransition,
                    coopdb.SessionMismatch,
            ) as exc:
                print(f"  warn: renew_claims {agent}: {exc}")
            except Exception as exc:
                # API-CONTRACT: never swallow unknown renew failures silently
                print(
                    f"  warn: renew_claims unexpected {agent}: "
                    f"{type(exc).__name__}: {exc}"
                )
            structured_res = None
            structured_deferred = None
            structured_mode = effective_profile.execution_mode
            def record_structured_usage(usage):
                bounded_usage = {
                    key: usage[key]
                    for key in coop_prompt_cache.USAGE_TRACE_FIELDS
                    if isinstance(usage, dict) and key in usage
                }
                _emit_trace(
                    trace,
                    "provider_result_received",
                    turn_id=turn_id,
                    agent=agent,
                    provider=agent,
                    action=hint,
                    capability_set=(
                        manifest.name if manifest is not None else None
                    ),
                    details={
                        "workflow_recipe": recipe.name,
                        "execution_mode": structured_mode,
                        **bounded_usage,
                    },
                )

            if structured_mode == "compiled_mesh_review_request":
                review_request_result = postcommit_mesh_review_request(
                    turn_conn,
                    plan=mesh_review_plan,
                    agent=agent,
                    session_id=sessions[agent],
                    lease_seconds=long_lease,
                    workspace=cwd,
                )
                if review_request_result is not None:
                    structured_res = {
                        "agent": agent,
                        "provider": agent,
                        "ok": True,
                        "exit": 0,
                        "note": "compiled_mesh_review_request_postcommit",
                    }
                else:
                    remember_isolated_decision_failure(
                        agent,
                        effective_profile.action_fingerprint,
                    )
                    structured_deferred = {
                        "agent": agent,
                        "provider": agent,
                        "ok": False,
                        "exit": None,
                        "note": "compiled_mesh_review_request_deferred_fallback",
                        "classification": (
                            "structured_decision_deferred_fallback"
                        ),
                        "retry_after_board_refresh": True,
                    }
            elif structured_mode == "isolated_structured_mesh_review":
                request = mesh_review_decision_request(
                    mesh_review_plan,
                    item=item,
                    provider=agent,
                )
                decision_result = None
                if (
                        request is not None
                        and request.action_fingerprint
                        == effective_profile.action_fingerprint):
                    decision_result = coop_start.invoke_structured_decision(
                        request=request,
                        cwd=cwd,
                        timeout_s=min(float(args.timeout), 120.0),
                        usage_callback=record_structured_usage,
                        inherit_env=args.inherit_env,
                    )
                value = (
                    coop_decisions.validate_decision_value(
                        request,
                        decision_result.value,
                    )
                    if request is not None and decision_result is not None
                    else None
                )
                review_result = (
                    postcommit_mesh_review(
                        turn_conn,
                        plan=mesh_review_plan,
                        value=value,
                        agent=agent,
                        session_id=sessions[agent],
                        lease_seconds=long_lease,
                        workspace=cwd,
                    )
                    if value is not None
                    else None
                )
                if review_result is not None:
                    structured_res = {
                        "agent": agent,
                        "provider": agent,
                        "ok": True,
                        "exit": 0,
                        "note": "structured_mesh_review_postcommit",
                    }
                else:
                    remember_isolated_decision_failure(
                        agent,
                        effective_profile.action_fingerprint,
                    )
                    structured_deferred = {
                        "agent": agent,
                        "provider": agent,
                        "ok": False,
                        "exit": None,
                        "note": "structured_mesh_review_deferred_fallback",
                        "classification": (
                            "structured_decision_deferred_fallback"
                        ),
                        "retry_after_board_refresh": True,
                    }
            elif structured_mode == "isolated_structured_mesh_questions":
                request = mesh_questions_decision_request(
                    mesh_plan,
                    item=item,
                    provider=agent,
                )
                decision_result = None
                if (
                        request is not None
                        and request.action_fingerprint
                        == effective_profile.action_fingerprint):
                    decision_result = coop_start.invoke_structured_decision(
                        request=request,
                        cwd=cwd,
                        timeout_s=min(float(args.timeout), 120.0),
                        usage_callback=record_structured_usage,
                        inherit_env=args.inherit_env,
                    )
                value = (
                    coop_decisions.validate_decision_value(
                        request,
                        decision_result.value,
                    )
                    if request is not None and decision_result is not None
                    else None
                )
                question_ids = (
                    postcommit_mesh_questions(
                        turn_conn,
                        plan=mesh_plan,
                        value=value,
                        agent=agent,
                        session_id=sessions[agent],
                        lease_seconds=long_lease,
                    )
                    if value is not None
                    else None
                )
                if question_ids:
                    structured_res = {
                        "agent": agent,
                        "provider": agent,
                        "ok": True,
                        "exit": 0,
                        "note": "structured_mesh_questions_postcommit",
                    }
                else:
                    remember_isolated_decision_failure(
                        agent,
                        effective_profile.action_fingerprint,
                    )
                    structured_deferred = {
                        "agent": agent,
                        "provider": agent,
                        "ok": False,
                        "exit": None,
                        "note": "structured_mesh_questions_deferred_fallback",
                        "classification": (
                            "structured_decision_deferred_fallback"
                        ),
                        "retry_after_board_refresh": True,
                    }
            elif structured_mode == "compiled_mesh_report":
                report_result = postcommit_mesh_report(
                    turn_conn,
                    plan=mesh_plan,
                    value=coop_mesh.default_mesh_report_value(),
                    agent=agent,
                    session_id=sessions[agent],
                    lease_seconds=long_lease,
                    workspace=cwd,
                )
                if report_result is not None:
                    structured_res = {
                        "agent": agent,
                        "provider": agent,
                        "ok": True,
                        "exit": 0,
                        "note": "compiled_mesh_report_postcommit",
                    }
                else:
                    remember_isolated_decision_failure(
                        agent,
                        effective_profile.action_fingerprint,
                    )
                    structured_deferred = {
                        "agent": agent,
                        "provider": agent,
                        "ok": False,
                        "exit": None,
                        "note": "compiled_mesh_report_deferred_fallback",
                        "classification": (
                            "structured_decision_deferred_fallback"
                        ),
                        "retry_after_board_refresh": True,
                    }
            elif structured_mode == "isolated_structured_mesh_report":
                request = mesh_report_decision_request(
                    mesh_plan,
                    item=item,
                    provider=agent,
                )
                decision_result = None
                if (
                        request is not None
                        and request.action_fingerprint
                        == effective_profile.action_fingerprint):
                    decision_result = coop_start.invoke_structured_decision(
                        request=request,
                        cwd=cwd,
                        timeout_s=min(float(args.timeout), 120.0),
                        usage_callback=record_structured_usage,
                        inherit_env=args.inherit_env,
                    )
                value = (
                    coop_decisions.validate_decision_value(
                        request,
                        decision_result.value,
                    )
                    if request is not None and decision_result is not None
                    else None
                )
                report_result = (
                    postcommit_mesh_report(
                        turn_conn,
                        plan=mesh_plan,
                        value=value,
                        agent=agent,
                        session_id=sessions[agent],
                        lease_seconds=long_lease,
                        workspace=cwd,
                    )
                    if value is not None
                    else None
                )
                if report_result is not None:
                    structured_res = {
                        "agent": agent,
                        "provider": agent,
                        "ok": True,
                        "exit": 0,
                        "note": "structured_mesh_report_postcommit",
                    }
                else:
                    remember_isolated_decision_failure(
                        agent,
                        effective_profile.action_fingerprint,
                    )
                    structured_deferred = {
                        "agent": agent,
                        "provider": agent,
                        "ok": False,
                        "exit": None,
                        "note": "structured_mesh_report_deferred_fallback",
                        "classification": (
                            "structured_decision_deferred_fallback"
                        ),
                        "retry_after_board_refresh": True,
                    }
            elif structured_mode == "compiled_mesh_transfer":
                transfer = postcommit_mesh_transfer(
                    turn_conn,
                    plan=mesh_plan,
                    agent=agent,
                    session_id=sessions[agent],
                    lease_seconds=long_lease,
                )
                if transfer is not None:
                    structured_res = {
                        "agent": agent,
                        "provider": agent,
                        "ok": True,
                        "exit": 0,
                        "note": "compiled_mesh_transfer_postcommit",
                    }
                else:
                    remember_isolated_decision_failure(
                        agent,
                        effective_profile.action_fingerprint,
                    )
                    structured_deferred = {
                        "agent": agent,
                        "provider": agent,
                        "ok": False,
                        "exit": None,
                        "note": "compiled_mesh_transfer_deferred_fallback",
                        "classification": (
                            "structured_decision_deferred_fallback"
                        ),
                        "retry_after_board_refresh": True,
                    }
            elif structured_mode == "isolated_structured_decision":
                request = structured_decision_request(
                    turn_conn,
                    provider=agent,
                    agent=agent,
                    action=action,
                    item=item,
                )
                decision_result = None
                if (
                        request is not None
                        and request.action_fingerprint
                        == effective_profile.action_fingerprint):
                    decision_result = coop_start.invoke_structured_decision(
                        request=request,
                        cwd=cwd,
                        timeout_s=min(float(args.timeout), 120.0),
                        usage_callback=record_structured_usage,
                        inherit_env=args.inherit_env,
                    )
                value = (
                    coop_decisions.validate_decision_value(
                        request,
                        decision_result.value,
                    )
                    if request is not None and decision_result is not None
                    else None
                )
                command = (
                    structured_decision_command(action, request, value)
                    if value is not None
                    else None
                )
                if command is not None and structured_action_still_current(
                        turn_conn,
                        agent=agent,
                        session_id=sessions[agent],
                        action=action,
                        lease_seconds=long_lease):
                    postcommit = None
                    try:
                        postcommit = subprocess.run(
                            command,
                            env=_agent_action_env(agent, action),
                            cwd=cwd,
                            capture_output=True,
                            text=True,
                            timeout=60,
                        )
                    except Exception as exc:
                        print(
                            f"  warn: structured postcommit {agent}: "
                            f"{type(exc).__name__}"
                        )
                    if postcommit is not None and postcommit.returncode == 0:
                        structured_res = {
                            "agent": agent,
                            "provider": agent,
                            "ok": True,
                            "exit": 0,
                            "note": "structured_decision_postcommit",
                        }
                        if request.decision_kind == "answer_questions":
                            answer = value["answers"][0]["answer"]
                            structured_res["structured_answer_sha256"] = (
                                hashlib.sha256(
                                    answer.encode("utf-8")
                                ).hexdigest()
                            )
                    elif postcommit is not None:
                        print(
                            f"  warn: structured postcommit {agent} exit "
                            f"{postcommit.returncode}"
                        )
                if structured_res is None:
                    remember_isolated_decision_failure(
                        agent,
                        effective_profile.action_fingerprint,
                    )
                    structured_deferred = {
                        "agent": agent,
                        "provider": agent,
                        "ok": False,
                        "exit": None,
                        "note": "structured_decision_deferred_fallback",
                        "classification": (
                            "structured_decision_deferred_fallback"
                        ),
                        "retry_after_board_refresh": True,
                    }
            elif structured_mode in {
                    "isolated_structured_answer",
                    "structured_answer"}:
                target_qid = action.get("target_id")
                question_row = None
                try:
                    row = turn_conn.execute(
                        "SELECT question_id, item_id, exact_question, "
                        "asked_by_agent, assigned_to_agent FROM questions "
                        "WHERE question_id=? AND status='open' AND "
                        "assigned_to_agent=?",
                        (target_qid, agent)).fetchone()
                    question_row = dict(row) if row is not None else None
                except Exception:
                    question_row = None
                answer = None
                if question_row is not None:
                    answer = coop_start.invoke_structured_answer(
                        provider=agent,
                        prompt=coop_prompt_cache.structured_answer_prompt(
                            question_row, item),
                        expected_question_id=target_qid,
                        cwd=cwd,
                        timeout_s=min(float(args.timeout), 120.0),
                        usage_callback=record_structured_usage,
                        inherit_env=args.inherit_env,
                    )
                if answer:
                    command = [
                        sys.executable if str(tok) == "python"
                        else answer if str(tok) == "{answer}"
                        else str(tok)
                        for tok in action["command"]
                    ]
                    postcommit = None
                    try:
                        postcommit = subprocess.run(
                            command,
                            env=_agent_action_env(agent, action),
                            cwd=cwd, capture_output=True, text=True,
                            timeout=60,
                        )
                    except Exception as exc:
                        print(
                            f"  warn: structured postcommit {agent}: "
                            f"{type(exc).__name__}"
                        )
                    if postcommit is not None and postcommit.returncode == 0:
                        structured_sha = hashlib.sha256(
                            answer.encode("utf-8")).hexdigest()
                        structured_res = {
                            "agent": agent,
                            "provider": agent,
                            "ok": True,
                            "exit": 0,
                            "note": "structured_answer_postcommit",
                            "structured_answer_sha256": structured_sha,
                        }
                    elif postcommit is not None:
                        print(
                            f"  warn: structured postcommit {agent} exit "
                            f"{postcommit.returncode}"
                        )
                if (
                        structured_mode == "isolated_structured_answer"
                        and structured_res is None):
                    remember_isolated_decision_failure(
                        agent,
                        effective_profile.action_fingerprint,
                    )
                    structured_deferred = {
                        "agent": agent,
                        "provider": agent,
                        "ok": False,
                        "exit": None,
                        "note": "structured_answer_deferred_fallback",
                        "classification": (
                            "structured_answer_deferred_fallback"
                        ),
                        "retry_after_board_refresh": True,
                    }
            turn_prompt = prompt
            if not args.no_prompt_hydration:
                # Spawn-time cache of board-derived state the runner already
                # holds; board stays canonical (see hydrated_prompt). Every
                # section is scoped to the TURN's item, not the run target -
                # under --all an unscoped status/question sweep was the
                # packet's biggest and least causal payload.
                status_payload = None
                try:
                    status_payload = coopdb.status(
                        turn_conn, agent,
                        session_id=sessions[agent],
                        action_lease_seconds=long_lease,
                        item_id=turn_item_id,
                    )
                except Exception:
                    pass  # action-envelope hydration still applies
                open_questions = None
                try:
                    q_clause = ("" if turn_item_id is None
                                else " AND item_id=?")
                    q_params = ((agent,) if turn_item_id is None
                                else (agent, turn_item_id))
                    open_questions = [
                        dict(row) for row in turn_conn.execute(
                            "SELECT question_id, item_id, exact_question, "
                            "asked_by_agent FROM questions WHERE "
                            "assigned_to_agent=? AND status='open'"
                            + q_clause + " ORDER BY question_id",
                            q_params)
                    ] or None
                except Exception:
                    open_questions = None
                answered_questions, pending_handoff, pending_review = (
                    causal_hydration_rows(
                        turn_conn, agent, action, turn_item_id,
                    )
                )
                receipt_evidence = None
                if (
                        args.token_efficient
                        and isinstance(action, dict)
                        and action.get("kind") == "continue_task"
                        and action.get("target_type") == "claim"):
                    receipt_evidence = receipt_hydration_rows(
                        turn_conn,
                        turn_item_id,
                    )
                turn_prompt = coop_prompt_cache.hydrated_prompt(
                    action=action if isinstance(action, dict) else None,
                    item=item,
                    status=status_payload,
                    questions=open_questions,
                    answered_questions=answered_questions,
                    handoff=pending_handoff,
                    review=pending_review,
                    receipt_evidence=receipt_evidence,
                    peer_agents=(
                        [
                            peer for peer in session_participants
                            if peer != agent
                        ]
                        if args.token_efficient
                        else ()
                    ),
                    token_efficient=args.token_efficient,
                )
            invoke_kwargs = {
                "action_lease_seconds": long_lease,
                "item_id": turn_item_id,
                "capability_manifest": manifest,
                "capability_config": capability_config,
                "run_dir": pathlib.Path(trace_path).parent,
                "trace": trace,
                "progress_probe": provider_progress_probe,
                "progress_before": before,
                "action": action,
                "turn_id": turn_id,
                "workflow_recipe": recipe.name,
                "worker_mode": worker_mode,
                "cleanup_registry": turn_cleanup_registry,
                "prompt_cache_hint": (
                    agent in prompt_cache_providers
                ),
                "inherit_env": args.inherit_env,
            }
            if completion_probe_attached:
                try:
                    completion_before = coopdb.actor_event_probe(
                        turn_conn,
                        session_id=sessions[agent],
                        item_id=turn_item_id,
                    )
                except Exception:
                    completion_before = None
                terminal_action = action

                def completion_probe(
                        *, _action=terminal_action,
                        _before=completion_before):
                    return postwrite_action_satisfied(
                        turn_conn,
                        agent=agent,
                        session_id=sessions[agent],
                        action=_action,
                        progress_before=_before,
                        lease_seconds=long_lease,
                    )

                invoke_kwargs["completion_probe"] = completion_probe
            try:
                if structured_deferred is not None:
                    res = structured_deferred
                elif structured_res is not None:
                    res = structured_res
                elif worker_mode == "cold":
                    res = coop_start.invoke_turn(
                        provider=agent,
                        prompt=turn_prompt,
                        session_id=sessions[agent],
                        agent_id=agent,
                        board_path=board_path,
                        cwd=cwd,
                        timeout_s=args.timeout,
                        **invoke_kwargs,
                    )
                else:
                    turn = coop_workers.WorkerTurn(
                        turn_id=turn_id,
                        provider=agent,
                        agent_id=agent,
                        prompt=turn_prompt,
                        session_id=sessions[agent],
                        board_path=board_path,
                        cwd=cwd,
                        action=action,
                        invoke_kwargs=invoke_kwargs,
                    )
                    try:
                        worker_pool.start(
                            agent,
                            core_profile={
                                "approval_policy": "never",
                                "sandbox": "danger-full-access",
                                "trace_turn": turn,
                            },
                            trace=trace,
                        )
                    except coop_workers.WorkerCleanupError:
                        raise
                    except Exception as exc:
                        teardown_owned_herdr_mirror(
                            owned_herdr_mirrors,
                            agent,
                            adapter=active_herdr_adapter,
                        )
                        with persistent_providers_lock:
                            effective_persistent_providers.discard(agent)
                        worker_mode = "cold"
                        invoke_kwargs["worker_mode"] = "cold"
                        print(
                            f"  warn: {agent} worker start failed "
                            f"({type(exc).__name__}); using cold turns"
                        )
                        res = coop_start.invoke_turn(
                            provider=agent,
                            prompt=turn_prompt,
                            session_id=sessions[agent],
                            agent_id=agent,
                            board_path=board_path,
                            cwd=cwd,
                            timeout_s=args.timeout,
                            **invoke_kwargs,
                        )
                    else:
                        res = worker_pool.submit(
                            agent,
                            turn,
                            timeout_s=args.timeout,
                        ).as_dict()
                        if (
                            res.get("classification")
                            == "provider_session_unsupported"
                            and res.get("cold_fallback_safe") is True
                            and res.get("tree_empty") is True
                        ):
                            # An exact CLI parser rejection proves the prompt
                            # never reached the model. Demote now, then return
                            # to the scheduler so board action and capability
                            # selection are both refreshed before cold work.
                            teardown_owned_herdr_mirror(
                                owned_herdr_mirrors,
                                agent,
                                adapter=active_herdr_adapter,
                            )
                            with persistent_providers_lock:
                                effective_persistent_providers.discard(
                                    agent
                                )
                            worker_mode = "cold"
                            invoke_kwargs["worker_mode"] = "cold"
                            print(
                                f"  warn: {agent} session flags "
                                "unsupported; retrying from fresh "
                                "board state with cold turns"
                            )
                            res["retry_after_board_refresh"] = True
            except Exception as exc:      # one bad turn must never kill the run
                cleanup_failed = isinstance(
                    exc,
                    coop_workers.WorkerCleanupError,
                )
                herdr_cleanup_failed = isinstance(
                    exc,
                    HerdrMirrorCleanupError,
                )
                res = {
                    "agent": agent,
                    "provider": agent,
                    "ok": False,
                    "note": (
                        "herdr_cleanup_failed"
                        if herdr_cleanup_failed
                        else "worker_cleanup_failed"
                        if cleanup_failed
                        else "worker_protocol_failed"
                        if worker_mode != "cold"
                        else f"turn error: {type(exc).__name__}"
                    )[:120],
                    **({
                        "classification": (
                            "herdr_cleanup_failed"
                            if herdr_cleanup_failed
                            else "worker_cleanup_failed"
                            if cleanup_failed
                            else "worker_protocol_failed"
                        ),
                        "retryable": False,
                    } if (
                        worker_mode != "cold"
                        or cleanup_failed
                        or herdr_cleanup_failed
                    ) else {}),
                }
        after = actor_progress()
        if (
                actor_probe_state["failed"]
                or before is None
                or after is None):
            actor_board_events = 0
            made_board_progress = False
        else:
            actor_board_events = max(0, int(after[1]) - int(before[1]))
            made_board_progress = actor_board_events > 0
        if (
                made_board_progress
                and not actor_probe_state["live_mutation_observed"]):
            # Persistent workers and runner-side structured/mechanical paths
            # do not poll during execution. A mocked cold transport may also
            # omit polling. Record the completion-time observation once.
            _emit_trace(
                trace,
                "first_board_mutation",
                turn_id=turn_id,
                agent=agent,
                provider=agent,
                action=hint,
                capability_set=(
                    manifest.name if manifest is not None else None
                ),
                details={"workflow_recipe": recipe.name},
            )
        res = classify_turn_result(
            res,
            made_board_progress=made_board_progress,
            actor_board_events=actor_board_events,
        )
        classification = _turn_classification(
            res,
            made_board_progress=made_board_progress,
        )
        res["classification"] = classification
        note = str(res.get("note") or "")
        _emit_trace(
            trace,
            "final_classification",
            turn_id=turn_id,
            agent=agent,
            provider=agent,
            action=hint,
            capability_set=manifest.name if manifest is not None else None,
            details={
                "classification": classification,
                "board_mutations": actor_board_events,
                "timed_out": note.startswith("timeout"),
                "exit_code": res.get("exit"),
                "workflow_recipe": recipe.name,
                "workspace_surface": effective_profile.workspace_surface,
                "execution_mode": effective_profile.execution_mode,
                "action_fingerprint": (
                    effective_profile.action_fingerprint
                ),
                **({
                    "structured_answer_sha256":
                        res["structured_answer_sha256"],
                } if res.get("structured_answer_sha256") else {}),
            },
        )
        provider_error = res.get("provider_error")
        print(f"  turn: {agent} <- {hint}: ok={res.get('ok')} "
              f"{res.get('note', '')}"
              + (f" — {provider_error}" if provider_error else ""))
        return res

    def take_turn(agent, hint, profile=None):
        with pending_actions_lock:
            action = pending_actions.pop(agent, None)
        turn_conn = coopdb.connect(board_path, require_current=True)
        try:
            return _take_turn_with_conn(
                agent,
                hint,
                action,
                profile,
                turn_conn,
            )
        finally:
            turn_conn.close()

    def scheduler_event(event_name, *, agent, action, details):
        _emit_trace(
            trace,
            event_name,
            agent=agent,
            provider=agent,
            action=action,
            details=details,
        )

    print(
        f"autonomous run: {', '.join(active_participants)} "
        f"| recipe={recipe.name} | board={board_path}"
    )
    print(f"  lease={long_lease}s timeout={int(args.timeout)}s "
          f"(lease>=timeout)")
    print(f"  stop with: touch {stop_flag}")
    print(
        "  child env: "
        + ("inherit (--inherit-env)" if args.inherit_env else "allowlist")
    )
    publish_status("checking")
    reason, turns = "error", 0
    human_mid = {"count": 0, "events": [], "messages": []}
    try:
        try:
            owned_herdr_mirrors, active_herdr_adapter = (
                prepare_opt_in_herdr_mirrors(
                    args.herdr,
                    effective_persistent_providers,
                    session_participants=session_participants,
                    run_id=run_id,
                    trace_path=trace_path,
                    cwd=cwd,
                    board_path=board_path,
                    environ=os.environ,
                    adapter=herdr_adapter,
                    ownership=owned_herdr_mirrors,
                )
            )
        except HerdrMirrorSetupError as exc:
            reason = exc.reason
            print(f"  Herdr mirror setup failed: {exc.reason}")
        else:
            publish_status("checking")
            reason, turns, _log = run_autonomous(
                active_participants,
                actionable_fn=actionable_fn,
                take_turn=take_turn,
                all_done=lambda: board_all_done(conn, item_id=args.item),
                stopped=lambda: pathlib.Path(stop_flag).exists(),
                interval=args.interval, max_turns=args.max_turns,
                max_idle_rounds=args.max_idle_rounds,
                max_noop_cycles=args.max_noop_cycles,
                progress_fn=progress_fn,
                profile_fn=profile_fn,
                status_fn=publish_status,
                scheduler_event_fn=scheduler_event,
                prepare_cycle=prepare_recipe_cycle,
                continuous=not args.no_continuous_dispatch)
    finally:
        cleanup_failures = []
        turn_cleanup_error = None
        for _attempt in range(2):
            try:
                turn_cleanup_registry.drain()
                turn_cleanup_error = None
                break
            except Exception as exc:
                turn_cleanup_error = exc
        if (
            turn_cleanup_error is not None
            or turn_cleanup_registry.pending
        ):
            cleanup_failures.append((
                "turn_resources",
                (
                    type(turn_cleanup_error).__name__
                    if turn_cleanup_error is not None
                    else "WorkerCleanupError"
                ),
            ))
            print(
                "  warn: turn resource cleanup failed: "
                f"{type(turn_cleanup_error).__name__}"
                if turn_cleanup_error is not None
                else "  warn: turn resource cleanup remains pending"
            )
        if worker_pool is not None:
            worker_cleanup_error = None
            for _attempt in range(2):
                try:
                    worker_pool.stop_all()
                    worker_cleanup_error = None
                    break
                except Exception as exc:
                    worker_cleanup_error = exc
            if worker_cleanup_error is not None:
                cleanup_failures.append(
                    (
                        "worker_pool",
                        type(worker_cleanup_error).__name__,
                    )
                )
                print(
                    "  warn: worker cleanup failed: "
                    f"{type(worker_cleanup_error).__name__}"
                )
        for cleanup in reversed(worker_cleanups):
            profile_cleanup_error = None
            for _attempt in range(2):
                try:
                    cleanup()
                    profile_cleanup_error = None
                    break
                except Exception as exc:
                    profile_cleanup_error = exc
            if profile_cleanup_error is not None:
                cleanup_failures.append(
                    (
                        "worker_profile",
                        type(profile_cleanup_error).__name__,
                    )
                )
                print(
                    "  warn: worker profile cleanup: "
                    f"{type(profile_cleanup_error).__name__}"
                )
        if cleanup_failures:
            reason = "worker_cleanup_failed"
            terminal_detail.clear()
        for name, sid in sessions.items():
            try:
                coopdb.finish_session(conn, sid, status="exited",
                                      reason="child_exit", exit_code=0)
            except (coopdb.InvalidTransition, coopdb.SessionMismatch) as exc:
                print(f"  warn: finish_session {name}: {exc}")
            except Exception as exc:
                print(f"  warn: finish_session unexpected {name}: "
                      f"{type(exc).__name__}: {exc}")
        human_mid = coopdb.mid_run_human_mutations(
            conn, since_iso=run_started_at,
            after_event_id=run_started_event_id)
        if human_mid["count"]:
            print(f"GATE FAIL: mid-run human mutations: {human_mid['count']} "
                  f"(happy path is kickoff+end review only)")
            for ev in human_mid["events"][:10]:
                print(f"  human event {ev.get('event_type')} @ "
                      f"{ev.get('created_at')}")
            for msg in human_mid["messages"][:5]:
                print(f"  human message id={msg.get('id')} @ "
                      f"{msg.get('created_at')}")
        final_reason = (
            "human_mutation_gate" if human_mid["count"] else reason)
        if terminal_detail.get("reason") != final_reason:
            terminal_detail.clear()
        terminal_fields = {
            key: terminal_detail[key]
            for key in ("reason_code", "evidence")
            if key in terminal_detail
        }
        try:
            coopdb.mark_autonomous_run(
                conn, phase="finished", session_id=sessions.get(host),
                agent_id=host,
                payload={
                    "reason": final_reason,
                    "turns": turns,
                    "item_id": args.item,
                    "workflow_recipe": recipe.as_payload(),
                    **terminal_fields,
                })
        except Exception as exc:
            print(f"  warn: run finished marker: {exc}")
        final_phase = {
            "all_done": "finished",
            "stalled": "stalled",
            "stopped": "stopped",
        }.get(final_reason, "failed")
        trace.emit(
            "run_finished",
            details={
                "classification": final_reason,
                "workflow_recipe": recipe.name,
                **terminal_fields,
            },
        )
        publish_status(
            final_phase,
            reason=final_reason,
            turns=turns,
            **terminal_fields,
        )
        reason = final_reason
        conn.close()
        if worker_pool is not None and not args.fresh_sessions:
            worker_getter = getattr(worker_pool, "worker", None)
            claude_worker = (
                worker_getter("claude") if callable(worker_getter) else None
            )
            save_warm_claude_session(
                pathlib.Path(trace_path).parent,
                getattr(
                    claude_worker,
                    "validated_provider_session_id",
                    None,
                ),
            )
        print_run_speed_summary(trace_path)
        coop_start.release_cli_resolution()
    print(f"stopped: {reason} after {turns} turn(s)")
    if human_mid["count"]:
        return 4  # mid-run human — product gate fail
    return 0 if reason in ("all_done", "stopped") else 3


def _selftest_scheduler(continuous):
    # 1. one actionable agent, then all_done flips -> reason all_done, 1 turn
    st = {"acted": False, "done": False}

    def actionable_fn(agent):
        return "review_task" if (agent == "a" and not st["acted"]) else IDLE

    def take_turn(agent, hint):
        st["acted"] = True
        st["done"] = True
        return {"ok": True}

    reason, turns, log = run_autonomous(
        ["a", "b"], actionable_fn=actionable_fn, take_turn=take_turn,
        all_done=lambda: st["done"], sleep=lambda s: None,
        continuous=continuous)
    assert reason == "all_done" and turns == 1, (reason, turns)
    assert log[0]["agent"] == "a" and log[0]["hint"] == "review_task", log

    # 2. nobody actionable, never done -> 'stalled' after max_idle_rounds
    slept = []
    reason2, turns2, _ = run_autonomous(
        ["a"], actionable_fn=lambda a: IDLE, take_turn=lambda a, h: None,
        all_done=lambda: False, max_idle_rounds=3,
        sleep=lambda s: slept.append(s), continuous=continuous)
    assert reason2 == "stalled" and turns2 == 0, (reason2, turns2)
    assert len(slept) == 2, slept          # idle rounds 1,2 slept; round 3 stalls

    # 3. stop flag wins immediately
    reason3, turns3, _ = run_autonomous(
        ["a"], actionable_fn=lambda a: "claim_task", take_turn=lambda a, h: None,
        all_done=lambda: False, stopped=lambda: True, sleep=lambda s: None,
        continuous=continuous)
    assert reason3 == "stopped" and turns3 == 0, (reason3, turns3)

    # 5. no-progress across cycles of actionable agents → stalled
    prog = {"t": 0}

    def always_actionable(a):
        return "review_task"

    def noop_turn(a, h):
        return {"ok": False, "note": "no_board_progress"}

    reason5, turns5, _ = run_autonomous(
        ["a"], actionable_fn=always_actionable, take_turn=noop_turn,
        all_done=lambda: False, max_noop_cycles=3,
        progress_fn=lambda: prog["t"], max_idle_rounds=None,
        sleep=lambda s: None, max_turns=20, continuous=continuous)
    assert reason5 == "stalled" and turns5 == 3, (reason5, turns5)

    # 6. FAULT-ISOLATION: failure notes flow through; recovery completes
    n = {"i": 0}

    def sometimes_raises(a, h):
        n["i"] += 1
        if n["i"] == 1:
            return {"ok": False, "note": "turn error: 402 Payment Required"}
        if n["i"] == 2:
            return {"ok": False, "note": "cli not found on PATH: codex"}
        if n["i"] == 3:
            return {"ok": False, "note": "timeout"}
        prog["t"] = 1
        return {"ok": True, "note": "recovered"}

    reason6, turns6, log6 = run_autonomous(
        ["a"], actionable_fn=lambda a: "claim_task" if n["i"] < 4 else IDLE,
        take_turn=sometimes_raises, all_done=lambda: n["i"] >= 4,
        progress_fn=lambda: prog["t"], max_noop_cycles=10,
        sleep=lambda s: None, max_turns=10, continuous=continuous)
    assert reason6 == "all_done" and turns6 == 4, (reason6, turns6, log6)
    notes = [e["result"].get("note", "") for e in log6]
    assert any("402" in n for n in notes), notes
    assert any("timeout" in n for n in notes), notes
    assert any("cli not found" in n or "launch" in n.lower() for n in notes), notes


def _selftest_continuous_overlap():
    # Only a held, workspace-isolated Claude answer may overlap an exclusive
    # turn's tail. The owner finishes only after that answer runs.
    answered = threading.Event()
    state = {"answer_ready": False, "done": False, "order": []}

    def actionable_fn(agent):
        if agent == "codex" and not state["order"]:
            return {"kind": "claim_task", "target_type": "item",
                    "target_id": 7, "item_id": 7}
        if agent == "claude" and state["answer_ready"] and \
                "claude" not in [o[0] for o in state["order"]]:
            return {"kind": "answer_question", "target_type": "question",
                    "target_id": 9, "item_id": 7, "claim_id": 12,
                    "command": [
                        *coopdb.CLI_ARGV, "question", "answer", "--claim",
                        "12", "--answer", "{answer}",
                    ]}
        return IDLE

    def take_turn(agent, hint):
        state["order"].append((agent, hint))
        if agent == "codex":
            state["answer_ready"] = True     # the committed needs_input
            assert answered.wait(timeout=10), "answer never overlapped"
            state["done"] = True
            return {
                "ok": True,
                "made_board_progress": True,
                "actor_board_events": 1,
            }
        answered.set()
        return {
            "ok": True,
            "made_board_progress": True,
            "actor_board_events": 1,
        }

    reason, turns, _log = run_autonomous(
        ["codex", "claude"],
        actionable_fn=actionable_fn,
        take_turn=take_turn,
        all_done=lambda: state["done"],
        progress_fn=lambda: (len(state["order"]), state["answer_ready"]),
        max_noop_cycles=5, sleep=lambda s: None, max_turns=10,
        dispatch_tick=0.05)
    assert reason == "all_done" and turns == 2, (reason, turns)
    assert state["order"][0][0] == "codex", state["order"]


def selftest():
    for continuous in (True, False):
        _selftest_scheduler(continuous)
    _selftest_continuous_overlap()

    # 4. CODEX-HEADLESS: ok=True + no board write → classify as failed
    failed = classify_turn_result(
        {"ok": True, "note": "sandbox init failed"},
        made_board_progress=False,
        actor_board_events=0,
    )
    assert failed["ok"] is False and "no_board_progress" in failed["note"], failed
    ok_write = classify_turn_result(
        {"ok": True, "note": "posted"},
        made_board_progress=True,
        actor_board_events=1,
    )
    assert ok_write["ok"] is True, ok_write

    # 7. TIMING: lease always >= timeout
    assert lease_for_timeout(120) >= 120
    assert lease_for_timeout(600) >= 600
    assert lease_for_timeout(600) == 3600  # 600*6 = 3600 floor


if __name__ == "__main__":
    raise SystemExit(main())
