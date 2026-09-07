"""Strict value-only contracts for bounded coordination decisions.

This module is deliberately board- and provider-process-free.  It describes
the only values a schema-constrained provider may choose; callers retain every
identifier, command, path, proof reference, and canonical mutation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


DECISION_KINDS = frozenset({
    "answer_questions",
    "respond_handoff",
    "compose_mesh_questions",
    "compose_mesh_report_sections",
    "review_mesh_report",
})
DECISION_SESSION_CLASSES = {
    "claude": "isolated_no_workspace",
    "codex": "isolated_no_workspace",
    "grok": "run_scoped_read",
}

MAX_ANSWER_CHARS = 4096
MAX_HANDOFF_REASON_CHARS = 1024
MAX_QUESTION_CHARS = 2048
MAX_REPORT_SECTION_CHARS = 4096
MAX_STOP_BOUNDARY_CHARS = 1024
MAX_STOP_BOUNDARIES = 8
MAX_RECEIPT_SUMMARY_CHARS = 2048
MAX_REVIEW_BODY_CHARS = 4096
MAX_DECISION_PROMPT_CHARS = 24000
MAX_BATCH_SIZE = 8

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DecisionRequest:
    decision_kind: str
    provider: str
    agent_id: str
    item_id: int
    action_fingerprint: str
    prompt: str
    json_schema: Mapping[str, object]
    session_class: str

    def __post_init__(self):
        if self.decision_kind not in DECISION_KINDS:
            raise ValueError("unknown decision kind")
        if self.provider not in DECISION_SESSION_CLASSES:
            raise ValueError("unknown decision provider")
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id must be non-empty")
        if len(self.agent_id) > 64:
            raise ValueError("agent_id is too long")
        if (
                isinstance(self.item_id, bool)
                or not isinstance(self.item_id, int)
                or self.item_id <= 0):
            raise ValueError("item_id must be a positive integer")
        if (
                not isinstance(self.action_fingerprint, str)
                or _FINGERPRINT_RE.fullmatch(
                    self.action_fingerprint
                ) is None):
            raise ValueError("action_fingerprint must be a SHA-256 hex value")
        if (
                not isinstance(self.prompt, str)
                or not self.prompt.strip()
                or len(self.prompt) > MAX_DECISION_PROMPT_CHARS):
            raise ValueError("prompt must be non-empty and bounded")
        if not isinstance(self.json_schema, Mapping):
            raise ValueError("json_schema must be a mapping")
        expected_class = DECISION_SESSION_CLASSES[self.provider]
        if self.session_class != expected_class:
            raise ValueError("session_class does not match provider")


@dataclass(frozen=True)
class DecisionResult:
    value: Mapping[str, object]
    usage: Mapping[str, object]

    def __post_init__(self):
        if not isinstance(self.value, Mapping):
            raise ValueError("decision value must be a mapping")
        if not isinstance(self.usage, Mapping):
            raise ValueError("decision usage must be a mapping")
        object.__setattr__(self, "value", MappingProxyType(dict(self.value)))
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


def _positive_ids(values, *, name):
    try:
        normalized = tuple(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be an iterable") from exc
    if not 1 <= len(normalized) <= MAX_BATCH_SIZE:
        raise ValueError(f"{name} must contain 1-{MAX_BATCH_SIZE} values")
    if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in normalized):
        raise ValueError(f"{name} must contain positive integers")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} must be unique")
    return normalized


def _recipients(values):
    try:
        normalized = tuple(values)
    except TypeError as exc:
        raise ValueError("recipients must be an iterable") from exc
    if not 1 <= len(normalized) <= MAX_BATCH_SIZE:
        raise ValueError(
            f"recipients must contain 1-{MAX_BATCH_SIZE} values")
    if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 64
            for value in normalized):
        raise ValueError("recipients must contain bounded agent IDs")
    if len(set(normalized)) != len(normalized):
        raise ValueError("recipients must be unique")
    return normalized


def _positive_id(value, *, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _object_schema(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def decision_schema(
        decision_kind,
        *,
        question_ids=(),
        handoff_id=None,
        recipients=(),
        review_id=None):
    """Return the exact JSON schema for one runner-bound decision."""
    if decision_kind == "answer_questions":
        ids = _positive_ids(question_ids, name="question_ids")
        answer = _object_schema({
            "question_id": {"type": "integer", "enum": list(ids)},
            "answer": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_ANSWER_CHARS,
            },
        }, ("question_id", "answer"))
        return _object_schema({
            "answers": {
                "type": "array",
                "items": answer,
                "minItems": len(ids),
                "maxItems": len(ids),
            },
        }, ("answers",))
    if decision_kind == "respond_handoff":
        target_id = _positive_id(handoff_id, name="handoff_id")
        return _object_schema({
            "handoff_id": {"type": "integer", "const": target_id},
            "response": {
                "type": "string",
                "enum": ["accept", "decline"],
            },
            "reason": {
                "type": "string",
                "maxLength": MAX_HANDOFF_REASON_CHARS,
            },
        }, ("handoff_id", "response", "reason"))
    if decision_kind == "compose_mesh_questions":
        peers = _recipients(recipients)
        question = _object_schema({
            "recipient": {"type": "string", "enum": list(peers)},
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUESTION_CHARS,
            },
        }, ("recipient", "question"))
        return _object_schema({
            "questions": {
                "type": "array",
                "items": question,
                "minItems": len(peers),
                "maxItems": len(peers),
            },
        }, ("questions",))
    if decision_kind == "compose_mesh_report_sections":
        text = {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_REPORT_SECTION_CHARS,
        }
        return _object_schema({
            "what_was_asked": dict(text),
            "method": dict(text),
            "stop_boundaries": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_STOP_BOUNDARY_CHARS,
                },
                "minItems": 1,
                "maxItems": MAX_STOP_BOUNDARIES,
            },
            "what_this_proves": dict(text),
            "what_this_does_not_prove": dict(text),
            "receipt_summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_RECEIPT_SUMMARY_CHARS,
            },
        }, (
            "what_was_asked",
            "method",
            "stop_boundaries",
            "what_this_proves",
            "what_this_does_not_prove",
            "receipt_summary",
        ))
    if decision_kind == "review_mesh_report":
        target_id = _positive_id(review_id, name="review_id")
        return _object_schema({
            "review_id": {"type": "integer", "const": target_id},
            "verdict": {
                "type": "string",
                "enum": ["approve", "changes"],
            },
            "body": {
                "type": "string",
                "maxLength": MAX_REVIEW_BODY_CHARS,
            },
        }, ("review_id", "verdict", "body"))
    raise ValueError("unknown decision kind")


def make_decision_request(
        *,
        decision_kind,
        provider,
        agent_id,
        item_id,
        action_fingerprint,
        prompt,
        question_ids=(),
        handoff_id=None,
        recipients=(),
        review_id=None):
    if provider not in DECISION_SESSION_CLASSES:
        raise ValueError("unknown decision provider")
    schema = decision_schema(
        decision_kind,
        question_ids=question_ids,
        handoff_id=handoff_id,
        recipients=recipients,
        review_id=review_id,
    )
    return DecisionRequest(
        decision_kind=decision_kind,
        provider=provider,
        agent_id=agent_id,
        item_id=item_id,
        action_fingerprint=action_fingerprint,
        prompt=prompt,
        json_schema=schema,
        session_class=DECISION_SESSION_CLASSES[provider],
    )


def _exact_keys(value, expected):
    return isinstance(value, Mapping) and set(value) == set(expected)


def _integer(value):
    return not isinstance(value, bool) and isinstance(value, int)


def _text(value, max_chars, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > max_chars:
        return None
    normalized = value.strip()
    if not normalized and not allow_empty:
        return None
    return normalized


def _schema_const(request, field):
    try:
        return request.json_schema["properties"][field]["const"]
    except (KeyError, TypeError):
        return None


def _schema_enum(request, container, field):
    try:
        properties = request.json_schema["properties"]
        if container is not None:
            properties = properties[container]["items"]["properties"]
        return tuple(properties[field]["enum"])
    except (KeyError, TypeError):
        return ()


def _validate_answers(request, value):
    if not _exact_keys(value, ("answers",)):
        return None
    answers = value.get("answers")
    expected = _schema_enum(request, "answers", "question_id")
    if not isinstance(answers, list) or len(answers) != len(expected):
        return None
    normalized = []
    for entry, question_id in zip(answers, expected):
        if not _exact_keys(entry, ("question_id", "answer")):
            return None
        if not _integer(entry["question_id"]) or entry["question_id"] != question_id:
            return None
        answer = _text(entry["answer"], MAX_ANSWER_CHARS)
        if answer is None:
            return None
        normalized.append({"question_id": question_id, "answer": answer})
    return {"answers": normalized}


def _validate_handoff(request, value):
    if not _exact_keys(value, ("handoff_id", "response", "reason")):
        return None
    target_id = _schema_const(request, "handoff_id")
    if not _integer(value["handoff_id"]) or value["handoff_id"] != target_id:
        return None
    response = value["response"]
    if response not in ("accept", "decline"):
        return None
    reason = _text(
        value["reason"],
        MAX_HANDOFF_REASON_CHARS,
        allow_empty=response == "accept",
    )
    if reason is None:
        return None
    if response == "accept" and reason:
        return None
    return {
        "handoff_id": target_id,
        "response": response,
        "reason": reason,
    }


def _validate_questions(request, value):
    if not _exact_keys(value, ("questions",)):
        return None
    questions = value.get("questions")
    expected = _schema_enum(request, "questions", "recipient")
    if not isinstance(questions, list) or len(questions) != len(expected):
        return None
    normalized = []
    for entry, recipient in zip(questions, expected):
        if not _exact_keys(entry, ("recipient", "question")):
            return None
        if entry["recipient"] != recipient:
            return None
        question = _text(entry["question"], MAX_QUESTION_CHARS)
        if question is None:
            return None
        normalized.append({"recipient": recipient, "question": question})
    return {"questions": normalized}


_REPORT_FIELDS = (
    "what_was_asked",
    "method",
    "stop_boundaries",
    "what_this_proves",
    "what_this_does_not_prove",
    "receipt_summary",
)


def _valid_receipt_summary(value):
    done = value.find("Done:")
    not_done = value.find("Not done:")
    boundaries = value.find("Stop boundaries:")
    return done == 0 and done < not_done < boundaries


def _validate_report(value):
    if not _exact_keys(value, _REPORT_FIELDS):
        return None
    normalized = {}
    for field in (
            "what_was_asked",
            "method",
            "what_this_proves",
            "what_this_does_not_prove"):
        text = _text(value[field], MAX_REPORT_SECTION_CHARS)
        if text is None:
            return None
        normalized[field] = text
    boundaries = value["stop_boundaries"]
    if (
            not isinstance(boundaries, list)
            or not 1 <= len(boundaries) <= MAX_STOP_BOUNDARIES):
        return None
    normalized_boundaries = []
    for boundary in boundaries:
        text = _text(boundary, MAX_STOP_BOUNDARY_CHARS)
        if text is None:
            return None
        normalized_boundaries.append(text)
    summary = _text(value["receipt_summary"], MAX_RECEIPT_SUMMARY_CHARS)
    if summary is None or not _valid_receipt_summary(summary):
        return None
    normalized["stop_boundaries"] = normalized_boundaries
    normalized["receipt_summary"] = summary
    return {field: normalized[field] for field in _REPORT_FIELDS}


def _validate_review(request, value):
    if not _exact_keys(value, ("review_id", "verdict", "body")):
        return None
    target_id = _schema_const(request, "review_id")
    if not _integer(value["review_id"]) or value["review_id"] != target_id:
        return None
    verdict = value["verdict"]
    if verdict not in ("approve", "changes"):
        return None
    body = _text(
        value["body"],
        MAX_REVIEW_BODY_CHARS,
        allow_empty=verdict == "approve",
    )
    if body is None:
        return None
    return {"review_id": target_id, "verdict": verdict, "body": body}


def validate_decision_value(request, value):
    """Return a normalized exact value, or ``None`` on any mismatch."""
    if not isinstance(request, DecisionRequest) or not isinstance(value, Mapping):
        return None
    if request.decision_kind == "answer_questions":
        return _validate_answers(request, value)
    if request.decision_kind == "respond_handoff":
        return _validate_handoff(request, value)
    if request.decision_kind == "compose_mesh_questions":
        return _validate_questions(request, value)
    if request.decision_kind == "compose_mesh_report_sections":
        return _validate_report(value)
    if request.decision_kind == "review_mesh_report":
        return _validate_review(request, value)
    return None


__all__ = [
    "DECISION_KINDS",
    "DECISION_SESSION_CLASSES",
    "DecisionRequest",
    "DecisionResult",
    "decision_schema",
    "make_decision_request",
    "validate_decision_value",
]
