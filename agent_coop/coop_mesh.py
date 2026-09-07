"""Read-only, fail-closed compiler for the explicit mesh-v2 contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import pathlib
import re
import tempfile
from typing import Mapping

from agent_coop import coop_action_scheduler
from agent_coop import coop_decisions
from agent_coop import coopdb


MESH_PARTICIPANTS = ("claude", "codex", "grok")
MESH_PAIRS = tuple(
    (sender, recipient)
    for sender in MESH_PARTICIPANTS
    for recipient in MESH_PARTICIPANTS
    if sender != recipient
)
_TEMPLATE_KEYS = {"name", "version", "contract_fingerprint"}
_REPORT_RE = re.compile(
    r"^Exactly one new file: "
    r"(docs/evidence/three-agent-ping-mesh-v2-[A-Za-z0-9._-]+\.md)\. "
    r"Required sections:"
)
_REPORT_PATH_RE = re.compile(
    r"^docs/evidence/three-agent-ping-mesh-v2-[A-Za-z0-9._-]+\.md$"
)
_REPORT_FIELDS = (
    "what_was_asked",
    "method",
    "stop_boundaries",
    "what_this_proves",
    "what_this_does_not_prove",
    "receipt_summary",
)
MAX_RENDERED_REPORT_BYTES = 131072
MESH_RECEIPT_PROOF = (
    "Canonical board evidence: six directed questions and six "
    "peer-authored answers, ordered by the compiled mesh-v2 contract."
)
MESH_REVIEWER = "claude"
_REPORT_TITLE = "# Three-agent ping mesh - results"
_REPORT_HEADINGS = (
    "## 1. What was asked",
    "## 2. Method",
    "## 3. Results",
    "## 4. Not completed / stop boundaries",
    "## 5. What this does and does not prove",
)


@dataclass(frozen=True)
class MeshExchange:
    sender: str
    recipient: str
    question_id: int
    question: str
    answer: str
    answer_event_id: int


@dataclass(frozen=True)
class MeshOutboundPlan:
    item_id: int
    claim_id: int
    sender: str
    recipients: tuple[str, ...]
    action_fingerprint: str


@dataclass(frozen=True)
class MeshTransferPlan:
    item_id: int
    claim_id: int
    sender: str
    to_agent: str
    reason: str
    summary: str
    completed: str
    remaining: str
    risks: str
    next_action: str
    proof_refs: tuple[str, ...]
    exchanges: tuple[MeshExchange, ...]
    action_fingerprint: str


@dataclass(frozen=True)
class MeshComposePlan:
    item_id: int
    claim_id: int
    sender: str
    report_path: str
    exchanges: tuple[MeshExchange, ...]
    action_fingerprint: str


@dataclass(frozen=True)
class MeshEvidencePacket:
    item_id: int
    owner: str
    receipt_id: int
    report_path: str
    report_sha256: str
    report_text: str
    exchanges: tuple[MeshExchange, ...]


@dataclass(frozen=True)
class MeshReviewRequestPlan:
    item_id: int
    claim_id: int
    owner: str
    reviewer: str
    receipt_id: int
    evidence: MeshEvidencePacket
    action_fingerprint: str

    @property
    def report_path(self):
        return self.evidence.report_path


@dataclass(frozen=True)
class MeshReviewPlan:
    item_id: int
    review_id: int
    claim_id: int
    owner: str
    reviewer: str
    receipt_id: int
    evidence: MeshEvidencePacket
    action_fingerprint: str

    @property
    def report_path(self):
        return self.evidence.report_path


def _bounded_text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("report text is missing or oversized")
    normalized = value.strip()
    if not normalized:
        raise ValueError("report text is empty")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("report text is not valid UTF-8") from exc
    return normalized


def normalize_mesh_report_value(value):
    """Strictly normalize the narrative-only mesh report decision."""
    if not isinstance(value, Mapping) or set(value) != set(_REPORT_FIELDS):
        raise ValueError("mesh report fields must be exact")
    normalized = {
        field: _bounded_text(value[field], coop_decisions.MAX_REPORT_SECTION_CHARS)
        for field in (
            "what_was_asked",
            "method",
            "what_this_proves",
            "what_this_does_not_prove",
        )
    }
    boundaries = value["stop_boundaries"]
    if (
            not isinstance(boundaries, list)
            or len(boundaries) != 1
            or not isinstance(boundaries[0], str)
            or boundaries[0].strip().lower() != "none"):
        raise ValueError("a complete mesh has exactly one 'none' boundary")
    normalized["stop_boundaries"] = ("none",)
    summary = _bounded_text(
        value["receipt_summary"],
        coop_decisions.MAX_RECEIPT_SUMMARY_CHARS,
    )
    lowered = summary.lower()
    if (
            not lowered.startswith("done:")
            or "not done: none" not in lowered
            or "stop boundaries: none" not in lowered):
        raise ValueError("receipt summary must record the complete stop state")
    normalized["receipt_summary"] = summary
    return {field: normalized[field] for field in _REPORT_FIELDS}


def default_mesh_report_value():
    """Code-authored narrative for the fixed six-ping liveness contract."""
    return {
        "what_was_asked": (
            "Exchange one directed ping on every ordered provider pair."
        ),
        "method": (
            "Each sender posted ordinary board questions to its two peers; "
            "each addressed peer authored its own answer."
        ),
        "stop_boundaries": ["none"],
        "what_this_proves": (
            "All six directed question and answer routes completed with "
            "canonical board evidence."
        ),
        "what_this_does_not_prove": (
            "This liveness check does not benchmark substantive agent work."
        ),
        "receipt_summary": (
            "Done: six directed pings and one evidence report. "
            "Not done: none. Stop boundaries: none."
        ),
    }


def _markdown_cell(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def _markdown_prose(value):
    collapsed = " ".join(str(value).replace("\r", "\n").splitlines())
    return (
        collapsed
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("#", "\\#")
        .replace("|", "\\|")
    )


def _mesh_result_rows(exchanges):
    rows = [
        "| from | to | ping text | response text | outcome |",
        "|---|---|---|---|---|",
    ]
    rows.extend(
        "| " + " | ".join((
            exchange.sender,
            exchange.recipient,
            _markdown_cell(exchange.question),
            _markdown_cell(exchange.answer),
            "answered",
        )) + " |"
        for exchange in exchanges
    )
    return rows


def render_mesh_report(plan, value):
    """Render immutable headings and the six canonical exchange rows."""
    if (
            not isinstance(plan, MeshComposePlan)
            or tuple((row.sender, row.recipient) for row in plan.exchanges)
            != MESH_PAIRS
            or any(not row.answer_event_id for row in plan.exchanges)):
        raise ValueError("a report requires all six canonical exchanges")
    sections = normalize_mesh_report_value(value)
    rows = _mesh_result_rows(plan.exchanges)
    report = (
        f"{_REPORT_TITLE}\n"
        f"{_REPORT_HEADINGS[0]}\n"
        f"{_markdown_prose(sections['what_was_asked'])}\n"
        f"{_REPORT_HEADINGS[1]}\n"
        f"{_markdown_prose(sections['method'])}\n"
        f"{_REPORT_HEADINGS[2]}\n"
        + "\n".join(rows)
        + f"\n{_REPORT_HEADINGS[3]}\n"
        + "\n".join(
            f"- {boundary}" for boundary in sections["stop_boundaries"]
        )
        + f"\n{_REPORT_HEADINGS[4]}\n"
        f"{_markdown_prose(sections['what_this_proves'])}\n\n"
        f"{_markdown_prose(sections['what_this_does_not_prove'])}\n"
    )
    if len(report.encode("utf-8")) > MAX_RENDERED_REPORT_BYTES:
        raise ValueError("rendered mesh report exceeds its byte budget")
    return report


def _resolve_mesh_report_path(workspace, report_path, *, must_exist):
    try:
        if (
                not isinstance(report_path, str)
                or _REPORT_PATH_RE.fullmatch(report_path) is None):
            return None
        root = pathlib.Path(workspace).resolve(strict=True)
        if not root.is_dir():
            return None
        pure = pathlib.PurePosixPath(report_path)
        if pure.is_absolute() or ".." in pure.parts:
            return None
        cursor = root
        for part in pure.parts[:-1]:
            cursor = cursor / part
            if cursor.is_symlink() or not cursor.is_dir():
                return None
        target = root.joinpath(*pure.parts)
        target.parent.resolve(strict=True).relative_to(root)
        if must_exist:
            if (
                    not os.path.lexists(target)
                    or target.is_symlink()
                    or not target.is_file()
                    or target.resolve(strict=True) != target):
                return None
        elif os.path.lexists(target):
            return None
        return target
    except (OSError, RuntimeError, ValueError):
        return None


def resolve_mesh_report_target(workspace, report_path):
    """Resolve a new versioned report path through real directories."""
    return _resolve_mesh_report_path(
        workspace, report_path, must_exist=False)


def resolve_existing_mesh_report_target(workspace, report_path):
    """Resolve an existing versioned report without following symlinks."""
    return _resolve_mesh_report_path(
        workspace, report_path, must_exist=True)


def write_mesh_report_exclusive(target, data):
    """Publish fully written bytes atomically without replacing a target."""
    if not isinstance(data, bytes) or not data:
        return False
    temp_path = None
    try:
        target = pathlib.Path(target)
        if os.path.lexists(target) or target.parent.is_symlink():
            return False
        handle, temp_path = tempfile.mkstemp(
            prefix=".coop-mesh-report-",
            suffix=".tmp",
            dir=str(target.parent),
        )
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp_path, target)
        return True
    except OSError:
        return False
    finally:
        if temp_path is not None:
            try:
                pathlib.Path(temp_path).unlink()
            except OSError:
                pass


def _json_object(value):
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("expected object")
    return parsed


def _session_matches(conn, session_id, agent, *, running=False):
    row = conn.execute(
        "SELECT agent_id, provider, status FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return bool(
        row is not None
        and row["agent_id"] == agent
        and row["provider"] == agent
        and (not running or row["status"] == "running")
    )


def _compiled_contract(conn, item):
    item_id = item.get("item_id", item.get("id"))
    if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
        return None
    current = dict(coopdb.item_show(conn, item_id))
    if (
            current["contract_version"] != 1
            or current["created_by"] != "human"
            or current["contract_incomplete"]
            or current["review_required"] != 1
            or current["review_waiver_reason"] is not None):
        return None
    for field in (*coopdb.CONTRACT_FIELDS, "contract_version"):
        if item.get(field) != current.get(field):
            return None
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE item_id=? AND "
        "event_type='item_created' ORDER BY event_id",
        (item_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    payload = _json_object(rows[0]["payload_json"])
    template = payload.get("template")
    if (
            not isinstance(template, dict)
            or set(template) != _TEMPLATE_KEYS
            or template.get("name") != "mesh-v2"
            or template.get("version") != 2
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(template.get("contract_fingerprint", "")),
            ) is None
            or coopdb.contract_fingerprint(current)
            != template["contract_fingerprint"]):
        return None
    path_match = _REPORT_RE.match(current["output_contract"])
    if path_match is None:
        return None
    report_path = path_match.group(1)
    pure = pathlib.PurePosixPath(report_path)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    for participant in MESH_PARTICIPANTS:
        live = conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id=? AND provider=? "
            "AND status='running' LIMIT 1",
            (participant, participant),
        ).fetchone()
        if live is None:
            return None
    return current, report_path


def _canonical_continue_claim(conn, item, action, agent):
    if not isinstance(action, Mapping):
        return None
    claim_id = action.get("claim_id")
    if (
            isinstance(claim_id, bool)
            or not isinstance(claim_id, int)
            or claim_id <= 0
            or action.get("kind") != "continue_task"
            or action.get("target_type") != "claim"
            or action.get("target_id") != claim_id
            or action.get("item_id") != item["item_id"]
            or action.get("choices") not in ([], ())
            or list(action.get("required_inputs") or ()) != [
                "contract_work", "path", "summary", "proof", "proof_ref"
            ]):
        return None
    expected_command = [
        *coopdb.CLI_ARGV,
        "receipt", "submit", "--claim", str(claim_id),
        "--path", "{path}", "--summary", "{summary}",
        "--proof", "{proof}", "--proof-ref", "{proof_ref}",
    ]
    if list(action.get("command") or ()) != expected_command:
        return None
    claim = conn.execute(
        "SELECT * FROM claims WHERE claim_id=? AND item_id=? AND "
        "claim_kind='implementation' AND claimed_by_agent=? AND "
        "status='active'",
        (claim_id, item["item_id"], agent),
    ).fetchone()
    if (
            claim is None
            or claim["owner_session_id"] is None
            or claim["lease_expires_at"] <= coopdb.now()
            or not _session_matches(
                conn, claim["owner_session_id"], agent, running=True
            )):
        return None
    active = conn.execute(
        "SELECT claim_id FROM claims WHERE item_id=? AND status='active'",
        (item["item_id"],),
    ).fetchall()
    if [row["claim_id"] for row in active] != [claim_id]:
        return None
    if (
            item["status"] != "working"
            or item["owner_agent_id"] != agent
            or item["next_actor_agent_id"] != agent):
        return None
    return claim


def _event_maps(conn, item_id):
    needs = {}
    answers = {}
    for row in conn.execute(
            "SELECT * FROM events WHERE item_id=? AND event_type IN "
            "('needs_input','question_answered') ORDER BY event_id",
            (item_id,)):
        payload = _json_object(row["payload_json"])
        question_id = payload.get("question_id")
        if isinstance(question_id, bool) or not isinstance(question_id, int):
            raise ValueError("invalid question event")
        target = needs if row["event_type"] == "needs_input" else answers
        target.setdefault(question_id, []).append(row)
    return needs, answers


def _exchanges(conn, item_id):
    needs_events, answer_events = _event_maps(conn, item_id)
    pair_rows = {}
    evidence = {}
    for question in conn.execute(
            "SELECT * FROM questions WHERE item_id=? ORDER BY question_id",
            (item_id,)):
        pair = (question["asked_by_agent"], question["assigned_to_agent"])
        if pair not in MESH_PAIRS or pair in pair_rows:
            raise ValueError("foreign or duplicate mesh pair")
        text = question["exact_question"]
        if (
                not isinstance(text, str)
                or not text.strip()
                or len(text.encode("utf-8")) > coopdb.MAX_QUESTION_TEXT_BYTES
                or not _session_matches(
                    conn,
                    question["asked_by_session"],
                    question["asked_by_agent"],
                )):
            raise ValueError("invalid question identity")
        created = needs_events.get(question["question_id"], ())
        if len(created) != 1:
            raise ValueError("missing or duplicate needs-input event")
        created_event = created[0]
        created_payload = _json_object(created_event["payload_json"])
        if (
                set(created_payload) != {"question_id", "item_id", "to", "question"}
                or created_payload["item_id"] != item_id
                or created_payload["to"] != question["assigned_to_agent"]
                or created_payload["question"] != text
                or created_event["actor_agent_id"] != question["asked_by_agent"]
                or created_event["actor_session_id"] != question["asked_by_session"]):
            raise ValueError("question creation evidence mismatch")
        answered = answer_events.get(question["question_id"], ())
        if question["status"] == "open":
            if (
                    answered
                    or any(question[key] is not None for key in (
                        "answer", "answered_by_agent", "answered_by_session",
                        "answered_at",
                    ))):
                raise ValueError("open question has answer evidence")
            evidence[pair] = None
        elif question["status"] == "answered":
            if (
                    len(answered) != 1
                    or not isinstance(question["answer"], str)
                    or not question["answer"].strip()
                    or len(question["answer"].encode("utf-8"))
                    > coopdb.MAX_QUESTION_TEXT_BYTES
                    or question["answered_by_agent"] != pair[1]
                    or not _session_matches(
                        conn, question["answered_by_session"], pair[1]
                    )
                    or not question["answered_at"]):
                raise ValueError("answer identity mismatch")
            answered_event = answered[0]
            answer_payload = _json_object(answered_event["payload_json"])
            if (
                    set(answer_payload) != {
                        "question_id", "item_id", "resume_owner",
                        "grace_expires_at",
                    }
                    or answer_payload["item_id"] != item_id
                    or answered_event["actor_agent_id"] != pair[1]
                    or answered_event["actor_session_id"]
                    != question["answered_by_session"]):
                raise ValueError("answer event mismatch")
            evidence[pair] = MeshExchange(
                sender=pair[0],
                recipient=pair[1],
                question_id=question["question_id"],
                question=text,
                answer=question["answer"],
                answer_event_id=answered_event["event_id"],
            )
        else:
            raise ValueError("withdrawn mesh question")
        pair_rows[pair] = question
    if set(needs_events) != {row["question_id"] for row in pair_rows.values()}:
        raise ValueError("orphan question creation event")
    if not set(answer_events).issubset(needs_events):
        raise ValueError("orphan question answer event")
    return pair_rows, evidence


def mesh_handoff_fields(sender, to_agent, exchanges):
    own = tuple(entry for entry in exchanges if entry.sender == sender)
    if len(own) != 2 or any(not entry.answer_event_id for entry in own):
        raise ValueError("handoff requires two answered outbound pairs")
    pairs = ", ".join(
        f"{entry.sender}->{entry.recipient} (question {entry.question_id})"
        for entry in own
    )
    return {
        "reason": f"Advance compiled mesh-v2 from {sender} to {to_agent}.",
        "summary": f"{sender} completed both of its outbound mesh-v2 pings.",
        "completed": f"Answered outbound pairs: {pairs}.",
        "remaining": (
            f"{to_agent} must author its two outbound pings and preserve "
            "peer-authored answers."
        ),
        "risks": "No inferred responses; canonical board rows remain authoritative.",
        "next_action": f"Accept this handoff and continue mesh-v2 as {to_agent}.",
        "proof_refs": tuple(f"event:{entry.answer_event_id}" for entry in own),
    }


def _handoffs_valid(conn, item_id, stage, evidence):
    rows = conn.execute(
        "SELECT * FROM handoffs WHERE item_id=? ORDER BY handoff_id",
        (item_id,),
    ).fetchall()
    expected_pairs = tuple(
        (MESH_PARTICIPANTS[index], MESH_PARTICIPANTS[index + 1])
        for index in range(stage)
    )
    if len(rows) != len(expected_pairs):
        return False
    exchanges = tuple(
        evidence[pair] for pair in MESH_PAIRS if evidence.get(pair) is not None
    )
    for row, (sender, recipient) in zip(rows, expected_pairs):
        expected = mesh_handoff_fields(sender, recipient, exchanges)
        try:
            proof_refs = json.loads(row["proof_references"])
        except (TypeError, json.JSONDecodeError):
            return False
        normalized = [
            {"type": "event", "id": int(ref.split(":", 1)[1])}
            for ref in expected["proof_refs"]
        ]
        if (
                row["from_agent"] != sender
                or row["to_agent"] != recipient
                or row["status"] != "accepted"
                or row["reason"] != expected["reason"]
                or row["summary"] != expected["summary"]
                or row["completed_work"] != expected["completed"]
                or row["remaining_work"] != expected["remaining"]
                or row["risks"] != expected["risks"]
                or row["suggested_next_action"] != expected["next_action"]
                or proof_refs != normalized
                or not _session_matches(conn, row["from_session"], sender)):
            return False
        created = conn.execute(
            "SELECT * FROM events WHERE item_id=? AND "
            "event_type='handoff_created' AND "
            "json_extract(payload_json, '$.handoff_id')=?",
            (item_id, row["handoff_id"]),
        ).fetchall()
        accepted = conn.execute(
            "SELECT * FROM events WHERE item_id=? AND "
            "event_type='handoff_accepted' AND "
            "json_extract(payload_json, '$.handoff_id')=?",
            (item_id, row["handoff_id"]),
        ).fetchall()
        if (
                len(created) != 1
                or created[0]["actor_agent_id"] != sender
                or len(accepted) != 1
                or accepted[0]["actor_agent_id"] != recipient):
            return False
    return True


def _valid_mesh_report_text(report_text, exchanges):
    if not isinstance(report_text, str) or not report_text.endswith("\n"):
        return False
    try:
        encoded = report_text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if not encoded or len(encoded) > MAX_RENDERED_REPORT_BYTES:
        return False
    lines = report_text.splitlines()
    if not lines or lines[0] != _REPORT_TITLE:
        return False
    if (
            lines.count(_REPORT_TITLE) != 1
            or any(lines.count(heading) != 1 for heading in _REPORT_HEADINGS)):
        return False
    allowed_headings = {_REPORT_TITLE, *_REPORT_HEADINGS}
    if any(
            line.lstrip().startswith("#") and line not in allowed_headings
            for line in lines):
        return False
    indices = tuple(lines.index(heading) for heading in _REPORT_HEADINGS)
    if indices != tuple(sorted(indices)) or indices[0] != 1:
        return False
    asked = lines[indices[0] + 1:indices[1]]
    method = lines[indices[1] + 1:indices[2]]
    results = lines[indices[2] + 1:indices[3]]
    boundaries = lines[indices[3] + 1:indices[4]]
    conclusion = lines[indices[4] + 1:]
    return bool(
        len(asked) == 1
        and asked[0].strip()
        and len(method) == 1
        and method[0].strip()
        and results == _mesh_result_rows(exchanges)
        and boundaries == ["- none"]
        and len(conclusion) == 3
        and conclusion[0].strip()
        and conclusion[1] == ""
        and conclusion[2].strip()
    )


def _receipt_summary_valid(value):
    if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > coop_decisions.MAX_RECEIPT_SUMMARY_CHARS):
        return False
    lowered = value.strip().lower()
    return re.fullmatch(
        r"done:\s*.+?\.\s*not done:\s*none\.\s*"
        r"stop boundaries:\s*none\.?",
        lowered,
        flags=re.DOTALL,
    ) is not None


def _active_implementation_claim(conn, packet):
    claim = conn.execute(
        "SELECT c.* FROM claims c JOIN receipts r "
        "ON r.claim_id=c.claim_id WHERE r.receipt_id=? AND c.item_id=? AND "
        "c.claim_kind='implementation' AND c.claimed_by_agent=? AND "
        "c.status='active'",
        (packet.receipt_id, packet.item_id, packet.owner),
    ).fetchone()
    if (
            claim is None
            or claim["owner_session_id"] is None
            or claim["lease_expires_at"] <= coopdb.now()
            or not _session_matches(
                conn,
                claim["owner_session_id"],
                packet.owner,
                running=True,
            )):
        return None
    return claim


def validate_mesh_evidence(conn, *, item_id, workspace):
    """Validate the entire immutable mesh-v2 evidence packet, or fail closed."""
    try:
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
            return None
        item = dict(coopdb.item_show(conn, item_id))
        compiled = _compiled_contract(conn, item)
        if compiled is None:
            return None
        current, report_path = compiled
        if (
                current["status"] not in ("working", "review")
                or current["owner_agent_id"] != "grok"
                or current["next_actor_agent_id"] != "grok"):
            return None
        pair_rows, evidence = _exchanges(conn, item_id)
        if set(pair_rows) != set(MESH_PAIRS):
            return None
        exchanges = tuple(evidence.get(pair) for pair in MESH_PAIRS)
        if any(exchange is None for exchange in exchanges):
            return None
        if not _handoffs_valid(conn, item_id, 2, evidence):
            return None

        receipts = conn.execute(
            "SELECT * FROM receipts WHERE item_id=? ORDER BY receipt_id",
            (item_id,),
        ).fetchall()
        if len(receipts) != 1 or receipts[0]["superseded_at"] is not None:
            return None
        receipt = receipts[0]
        target = resolve_existing_mesh_report_target(workspace, report_path)
        if target is None or receipt["source_path"] != str(target):
            return None
        stat = target.stat()
        if stat.st_size <= 0 or stat.st_size > MAX_RENDERED_REPORT_BYTES:
            return None
        report_bytes = target.read_bytes()
        if len(report_bytes) != stat.st_size:
            return None
        report_sha256 = hashlib.sha256(report_bytes).hexdigest()
        if receipt["sha256"] != report_sha256:
            return None
        report_text = report_bytes.decode("utf-8")
        if not _valid_mesh_report_text(report_text, exchanges):
            return None
        expected_refs = [
            {"type": "event", "id": exchange.answer_event_id}
            for exchange in exchanges
        ]
        if (
                receipt["contract_version"] != current["contract_version"]
                or receipt["submitted_by_agent"] != "grok"
                or not _session_matches(
                    conn,
                    receipt["submitted_by_session"],
                    "grok",
                    running=True,
                )
                or receipt["proof"] != MESH_RECEIPT_PROOF
                or not _receipt_summary_valid(receipt["summary"])
                or json.loads(receipt["proof_references_json"]) != expected_refs):
            return None
        claim = conn.execute(
            "SELECT * FROM claims WHERE claim_id=? AND item_id=? AND "
            "claim_kind='implementation' AND claimed_by_agent='grok' AND "
            "status='active'",
            (receipt["claim_id"], item_id),
        ).fetchone()
        if (
                claim is None
                or claim["fencing_token"] != receipt["fencing_token"]
                or claim["owner_session_id"] != receipt["submitted_by_session"]
                or claim["lease_expires_at"] <= coopdb.now()
                or not _session_matches(
                    conn, claim["owner_session_id"], "grok", running=True
                )):
            return None
        receipt_events = conn.execute(
            "SELECT * FROM events WHERE item_id=? AND "
            "event_type='receipt_submitted' ORDER BY event_id",
            (item_id,),
        ).fetchall()
        if len(receipt_events) != 1:
            return None
        event = receipt_events[0]
        payload = _json_object(event["payload_json"])
        expected_payload = {
            "item_id": item_id,
            "receipt_id": receipt["receipt_id"],
            "source_path": str(target),
            "sha256": report_sha256,
            "reference_count": len(expected_refs),
        }
        if (
                payload != expected_payload
                or event["actor_agent_id"] != "grok"
                or event["actor_session_id"] != claim["owner_session_id"]
                or event["claim_id"] != claim["claim_id"]
                or event["fencing_token"] != claim["fencing_token"]):
            return None
        return MeshEvidencePacket(
            item_id=item_id,
            owner="grok",
            receipt_id=receipt["receipt_id"],
            report_path=report_path,
            report_sha256=report_sha256,
            report_text=report_text,
            exchanges=exchanges,
        )
    except Exception:
        return None


def _request_review_action(conn, item, action, agent, packet):
    if not isinstance(action, Mapping) or set(action) != set(
            coop_action_scheduler.ACTION_IDENTITY_FIELDS):
        return None
    claim_id = action.get("claim_id")
    expected_command = [
        *coopdb.CLI_ARGV,
        "review", "request", "--claim", str(claim_id),
    ]
    if (
            agent != packet.owner
            or action.get("kind") != "request_review"
            or action.get("target_type") != "item"
            or action.get("target_id") != packet.item_id
            or action.get("item_id") != packet.item_id
            or action.get("lease_seconds") is not None
            or list(action.get("command") or ()) != expected_command
            or action.get("required_inputs") not in ([], ())
            or action.get("choices") not in ([], ())):
        return None
    claim = _active_implementation_claim(conn, packet)
    if claim is None or claim["claim_id"] != claim_id:
        return None
    if (
            item["status"] != "working"
            or item["owner_agent_id"] != packet.owner
            or item["next_actor_agent_id"] != packet.owner):
        return None
    return claim


def compile_mesh_review_request(
        conn, *, item, action, agent, workspace):
    """Compile the one named independent review request for mesh-v2."""
    try:
        if agent != "grok" or not isinstance(item, Mapping):
            return None
        packet = validate_mesh_evidence(
            conn, item_id=item.get("item_id", item.get("id")), workspace=workspace)
        if packet is None:
            return None
        current = dict(coopdb.item_show(conn, packet.item_id))
        for field in (*coopdb.CONTRACT_FIELDS, "contract_version"):
            if item.get(field) != current.get(field):
                return None
        claim = _request_review_action(conn, current, action, agent, packet)
        if claim is None:
            return None
        if conn.execute(
                "SELECT 1 FROM reviews WHERE item_id=? LIMIT 1",
                (packet.item_id,),
        ).fetchone() is not None:
            return None
        reviewer_session = conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id=? AND provider=? "
            "AND status='running'",
            (MESH_REVIEWER, MESH_REVIEWER),
        ).fetchone()
        if reviewer_session is None or MESH_REVIEWER == packet.owner:
            return None
        return MeshReviewRequestPlan(
            item_id=packet.item_id,
            claim_id=claim["claim_id"],
            owner=packet.owner,
            reviewer=MESH_REVIEWER,
            receipt_id=packet.receipt_id,
            evidence=packet,
            action_fingerprint=(
                coop_action_scheduler.action_fingerprint(action)
            ),
        )
    except Exception:
        return None


def _continue_review_action(action, *, item_id, review_id, claim_id):
    expected_choices = [
        {
            "kind": "approve_review",
            "command": [
                *coopdb.CLI_ARGV,
                "review", "submit", "--claim", str(claim_id),
                "--verdict", "approve",
            ],
            "required_inputs": [],
        },
        {
            "kind": "request_changes",
            "command": [
                *coopdb.CLI_ARGV,
                "review", "submit", "--claim", str(claim_id),
                "--verdict", "changes", "--body", "{body}",
            ],
            "required_inputs": ["body"],
        },
    ]
    return bool(
        isinstance(action, Mapping)
        and set(action) == set(coop_action_scheduler.ACTION_IDENTITY_FIELDS)
        and action.get("kind") == "continue_task"
        and action.get("target_type") == "review"
        and action.get("target_id") == review_id
        and action.get("item_id") == item_id
        and action.get("claim_id") == claim_id
        and action.get("lease_seconds") is None
        and action.get("command") is None
        and list(action.get("required_inputs") or ()) == ["review_verdict"]
        and list(action.get("choices") or ()) == expected_choices
    )


def compile_mesh_review(conn, *, item, action, agent, workspace):
    """Compile one bounded verdict only for the named independent reviewer."""
    try:
        if agent != MESH_REVIEWER or not isinstance(item, Mapping):
            return None
        packet = validate_mesh_evidence(
            conn, item_id=item.get("item_id", item.get("id")), workspace=workspace)
        if packet is None:
            return None
        current = dict(coopdb.item_show(conn, packet.item_id))
        for field in (*coopdb.CONTRACT_FIELDS, "contract_version"):
            if item.get(field) != current.get(field):
                return None
        if (
                current["status"] != "review"
                or current["owner_agent_id"] != packet.owner
                or current["next_actor_agent_id"] != packet.owner
                or agent == packet.owner):
            return None
        reviews = conn.execute(
            "SELECT * FROM reviews WHERE item_id=? ORDER BY id",
            (packet.item_id,),
        ).fetchall()
        if len(reviews) != 1:
            return None
        review = reviews[0]
        if (
                review["receipt_id"] != packet.receipt_id
                or review["contract_version"] != current["contract_version"]
                or review["requested_by"] != packet.owner
                or review["requested_by_agent"] != packet.owner
                or review["reviewer"] != agent
                or review["reviewer_agent_id"] != agent
                or review["status"] != "requested"
                or review["resolved_at"] is not None
                or review["legacy"] != 0):
            return None
        claim_id = action.get("claim_id") if isinstance(action, Mapping) else None
        if not _continue_review_action(
                action,
                item_id=packet.item_id,
                review_id=review["id"],
                claim_id=claim_id):
            return None
        review_claim = conn.execute(
            "SELECT * FROM claims WHERE claim_id=? AND item_id=? AND "
            "claim_kind='review' AND subject_id=? AND lane_key=? AND "
            "claimed_by_agent=? AND status='active'",
            (
                claim_id,
                packet.item_id,
                review["id"],
                f"review:{review['id']}",
                agent,
            ),
        ).fetchone()
        implementation_claim = _active_implementation_claim(conn, packet)
        if (
                review_claim is None
                or implementation_claim is None
                or review_claim["lease_expires_at"] <= coopdb.now()
                or not _session_matches(
                    conn, review_claim["owner_session_id"], agent, running=True
                )):
            return None
        active_claims = conn.execute(
            "SELECT claim_id FROM claims WHERE item_id=? AND status='active' "
            "ORDER BY claim_id",
            (packet.item_id,),
        ).fetchall()
        if [row["claim_id"] for row in active_claims] != sorted((
                implementation_claim["claim_id"], review_claim["claim_id"])):
            return None
        requested_events = conn.execute(
            "SELECT * FROM events WHERE item_id=? AND "
            "event_type='review_requested' ORDER BY event_id",
            (packet.item_id,),
        ).fetchall()
        claimed_events = conn.execute(
            "SELECT * FROM events WHERE item_id=? AND "
            "event_type='review_claimed' ORDER BY event_id",
            (packet.item_id,),
        ).fetchall()
        expected_requested = {
            "item_id": packet.item_id,
            "review_id": review["id"],
            "receipt_id": packet.receipt_id,
            "contract_version": current["contract_version"],
            "reviewer": agent,
        }
        expected_claimed = {
            "review_id": review["id"],
            "item_id": packet.item_id,
            "claim_id": review_claim["claim_id"],
        }
        if len(requested_events) != 1 or len(claimed_events) != 1:
            return None
        requested_event = requested_events[0]
        claimed_event = claimed_events[0]
        if (
                _json_object(requested_event["payload_json"])
                != expected_requested
                or requested_event["actor_agent_id"] != packet.owner
                or requested_event["actor_session_id"]
                != implementation_claim["owner_session_id"]
                or requested_event["claim_id"]
                != implementation_claim["claim_id"]
                or requested_event["fencing_token"]
                != implementation_claim["fencing_token"]
                or _json_object(claimed_event["payload_json"])
                != expected_claimed
                or claimed_event["actor_agent_id"] != agent
                or claimed_event["actor_session_id"]
                != review_claim["owner_session_id"]
                or claimed_event["claim_id"] != review_claim["claim_id"]
                or claimed_event["fencing_token"]
                != review_claim["fencing_token"]):
            return None
        return MeshReviewPlan(
            item_id=packet.item_id,
            review_id=review["id"],
            claim_id=review_claim["claim_id"],
            owner=packet.owner,
            reviewer=agent,
            receipt_id=packet.receipt_id,
            evidence=packet,
            action_fingerprint=(
                coop_action_scheduler.action_fingerprint(action)
            ),
        )
    except Exception:
        return None


def compile_mesh_phase(conn, *, item, action, agent):
    """Return one immutable legal phase, otherwise ``None`` with zero writes."""
    try:
        if agent not in MESH_PARTICIPANTS or not isinstance(item, Mapping):
            return None
        compiled = _compiled_contract(conn, item)
        if compiled is None:
            return None
        current, report_path = compiled
        claim = _canonical_continue_claim(conn, current, action, agent)
        if claim is None:
            return None
        stage = MESH_PARTICIPANTS.index(agent)
        pair_rows, evidence = _exchanges(conn, current["item_id"])
        allowed_senders = set(MESH_PARTICIPANTS[:stage + 1])
        if any(sender not in allowed_senders for sender, _recipient in pair_rows):
            return None
        for prior in MESH_PARTICIPANTS[:stage]:
            prior_pairs = [pair for pair in MESH_PAIRS if pair[0] == prior]
            if any(pair not in pair_rows or evidence.get(pair) is None
                   for pair in prior_pairs):
                return None
        if not _handoffs_valid(conn, current["item_id"], stage, evidence):
            return None
        own_pairs = tuple(pair for pair in MESH_PAIRS if pair[0] == agent)
        present = tuple(pair for pair in own_pairs if pair in pair_rows)
        fingerprint = coop_action_scheduler.action_fingerprint(action)
        if not present:
            return MeshOutboundPlan(
                item_id=current["item_id"],
                claim_id=claim["claim_id"],
                sender=agent,
                recipients=tuple(pair[1] for pair in own_pairs),
                action_fingerprint=fingerprint,
            )
        if present != own_pairs or any(evidence.get(pair) is None for pair in own_pairs):
            return None
        ordered_evidence = tuple(evidence[pair] for pair in MESH_PAIRS if pair in evidence)
        if stage < len(MESH_PARTICIPANTS) - 1:
            to_agent = MESH_PARTICIPANTS[stage + 1]
            fields = mesh_handoff_fields(agent, to_agent, ordered_evidence)
            return MeshTransferPlan(
                item_id=current["item_id"],
                claim_id=claim["claim_id"],
                sender=agent,
                to_agent=to_agent,
                exchanges=ordered_evidence,
                action_fingerprint=fingerprint,
                **fields,
            )
        if len(ordered_evidence) != len(MESH_PAIRS):
            return None
        return MeshComposePlan(
            item_id=current["item_id"],
            claim_id=claim["claim_id"],
            sender=agent,
            report_path=report_path,
            exchanges=ordered_evidence,
            action_fingerprint=fingerprint,
        )
    except Exception:
        return None


__all__ = [
    "MAX_RENDERED_REPORT_BYTES",
    "MESH_RECEIPT_PROOF",
    "MESH_PAIRS",
    "MESH_PARTICIPANTS",
    "MESH_REVIEWER",
    "MeshComposePlan",
    "MeshEvidencePacket",
    "MeshExchange",
    "MeshOutboundPlan",
    "MeshReviewPlan",
    "MeshReviewRequestPlan",
    "MeshTransferPlan",
    "compile_mesh_phase",
    "compile_mesh_review",
    "compile_mesh_review_request",
    "default_mesh_report_value",
    "mesh_handoff_fields",
    "normalize_mesh_report_value",
    "render_mesh_report",
    "resolve_existing_mesh_report_target",
    "resolve_mesh_report_target",
    "validate_mesh_evidence",
    "write_mesh_report_exclusive",
]
