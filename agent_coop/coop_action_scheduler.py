"""Pure, conservative batching for board-derived Co-op actions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


MAX_TURN_CONCURRENCY = 3

ACTION_IDENTITY_FIELDS = (
    "kind",
    "target_type",
    "target_id",
    "item_id",
    "claim_id",
    "lease_seconds",
    "command",
    "required_inputs",
    "choices",
)


@dataclass(frozen=True)
class ActionCandidate:
    agent: str
    hint: str
    action: dict


@dataclass(frozen=True)
class DispatchProfile:
    lane: str | None
    workspace_surface: Literal["none", "read", "write"]
    execution_mode: Literal[
        "compiled_mesh_report",
        "compiled_mesh_review_request",
        "compiled_mesh_transfer",
        "isolated_structured_decision",
        "isolated_structured_answer",
        "isolated_structured_mesh_questions",
        "isolated_structured_mesh_report",
        "isolated_structured_mesh_review",
        "structured_answer",
        "tool_turn",
    ]
    action_fingerprint: str


def action_fingerprint(action: Mapping) -> str:
    """SHA-256 identity of the complete executable action envelope."""
    if not isinstance(action, Mapping):
        raise TypeError("action must be a mapping")
    envelope = {
        field: action.get(field)
        for field in ACTION_IDENTITY_FIELDS
    }
    payload = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def actions_equivalent(left, right) -> bool:
    """Return whether two actions have the same executable identity."""
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    try:
        return action_fingerprint(left) == action_fingerprint(right)
    except (TypeError, ValueError):
        return False


def _target_id(candidate: ActionCandidate) -> int | None:
    if not isinstance(candidate.action, Mapping):
        return None
    if candidate.action.get("kind") != candidate.hint:
        return None
    target_id = candidate.action.get("target_id")
    if (
            isinstance(target_id, bool)
            or not isinstance(target_id, int)
            or target_id <= 0):
        return None
    return target_id


def action_lane(candidate: ActionCandidate) -> str | None:
    """Return a parallel-safe lane, or None to force serialization."""
    target_id = _target_id(candidate)
    if target_id is None:
        return None
    if candidate.hint == "huddle_post":
        if not isinstance(candidate.agent, str) or not candidate.agent:
            return None
        return f"huddle:{target_id}:{candidate.agent}"
    if candidate.hint == "answer_question":
        return f"question_response:{target_id}"
    if candidate.hint == "review_task":
        return f"review:{target_id}"
    if (
            candidate.hint == "continue_task"
            and candidate.action.get("target_type") == "review"):
        return f"review:{target_id}"
    if candidate.hint == "respond_handoff":
        # Item-scoped: cross-item handoff responses overlap; two handoffs
        # on the same item still serialize (an accept mutates item state).
        item_id = candidate.action.get("item_id")
        if (
                isinstance(item_id, bool)
                or not isinstance(item_id, int)
                or item_id <= 0):
            return None
        return f"handoff:{item_id}"
    return None


def actions_independent(
        left: ActionCandidate,
        right: ActionCandidate) -> bool:
    """True only for different providers on distinct allowlisted lanes."""
    left_lane = action_lane(left)
    right_lane = action_lane(right)
    return (
        bool(left_lane)
        and bool(right_lane)
        and left.agent != right.agent
        and left_lane != right_lane
    )


def select_action_batch(
        candidates,
        *,
        limit=MAX_TURN_CONCURRENCY) -> tuple[ActionCandidate, ...]:
    """Select a stable prefix without crossing a serialization barrier."""
    ordered = tuple(candidates)
    if not ordered:
        return ()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return ()
    cap = min(MAX_TURN_CONCURRENCY, limit)
    selected = [ordered[0]]
    if action_lane(ordered[0]) is None:
        return tuple(selected)
    for candidate in ordered[1:]:
        if len(selected) >= cap:
            break
        if not all(
                actions_independent(candidate, prior)
                for prior in selected):
            break
        selected.append(candidate)
    return tuple(selected)


__all__ = [
    "MAX_TURN_CONCURRENCY",
    "ACTION_IDENTITY_FIELDS",
    "ActionCandidate",
    "DispatchProfile",
    "action_fingerprint",
    "action_lane",
    "actions_equivalent",
    "actions_independent",
    "select_action_batch",
]
