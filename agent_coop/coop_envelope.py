"""Communication envelope — a read-model projection over the events stream.

The envelope is a pure projection: it performs zero writes, adds no schema,
and never becomes a second source of domain truth.

- id             = event_id (unique, ordered, the inbox provenance key)
- type           = closed object.direction registry below
- payload        = a reference (object_type/object_id/item_id) plus the
                   event's own payload passed through unmodified — no new
                   content surface, no duplicated domain truth
- correlation_id = the root request's event_id, walked from the object id
                   already present in the event payload; null when no link
                   exists. Never inferred.

Event types outside the registry do not project: the envelope covers the
agent-to-agent request/response surface, not every lifecycle event.
"""
import json

ENVELOPE_VERSION = 1

# event_type -> (envelope type, object_type, payload object-id key)
TYPE_REGISTRY = {
    "needs_input":       ("question.request",  "question", "question_id"),
    "question_answered": ("question.response", "question", "question_id"),
    "handoff_created":   ("handoff.request",   "handoff",  "handoff_id"),
    "handoff_accepted":  ("handoff.response",  "handoff",  "handoff_id"),
    "handoff_declined":  ("handoff.response",  "handoff",  "handoff_id"),
    "review_requested":  ("review.request",    "review",   "review_id"),
    "review_resolved":   ("review.response",   "review",   "review_id"),
    "huddle_opened":     ("huddle.request",    "huddle",   "huddle_id"),
    "huddle_posted":     ("huddle.response",   "huddle",   "huddle_id"),
    # close_huddle emits one of four outcome types, all carrying huddle_id
    "contract_accepted":              ("huddle.outcome", "huddle", "huddle_id"),
    "contract_changes_requested":     ("huddle.outcome", "huddle", "huddle_id"),
    "plan_huddle_concurred":          ("huddle.outcome", "huddle", "huddle_id"),
    "plan_huddle_changes_requested":  ("huddle.outcome", "huddle", "huddle_id"),
    "decision_recorded": ("decision.record",   "decision", "decision_id"),
    "message_posted":    ("message.post",      "message",  "message_id"),
}

REQUEST_TYPES = frozenset({
    "question.request", "handoff.request", "review.request", "huddle.request",
})


def _event_payload(raw):
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def project(conn, *, item_id=None, since=None):
    """Project envelopes from committed events. Read-only by construction.

    `since` bounds emission (event_id > since) but never root discovery, so a
    response after `since` still correlates to its earlier request.
    """
    # Full family scan per call — fine at local board scale; add an
    # incremental root cache only if a real board makes this measurably slow.
    marks = ",".join("?" for _ in TYPE_REGISTRY)
    sql = (f"SELECT event_id, item_id, event_type, actor_agent_id, "
           f"created_at, payload_json FROM events "
           f"WHERE event_type IN ({marks})")
    params = list(TYPE_REGISTRY)
    if item_id is not None:
        sql += " AND item_id = ?"
        params.append(item_id)
    sql += " ORDER BY event_id"
    rows = conn.execute(sql, params).fetchall()

    roots = {}
    parsed_rows = []
    for (event_id, row_item, event_type, actor, created_at, raw) in rows:
        etype, object_type, id_key = TYPE_REGISTRY[event_type]
        payload = _event_payload(raw)
        object_id = payload.get(id_key)
        if etype in REQUEST_TYPES and object_id is not None:
            roots.setdefault((object_type, object_id), event_id)
        parsed_rows.append(
            (event_id, row_item, etype, object_type, object_id, actor,
             created_at, payload))

    envelopes = []
    for (event_id, row_item, etype, object_type, object_id, actor,
         created_at, payload) in parsed_rows:
        if since is not None and event_id <= since:
            continue
        if etype in REQUEST_TYPES:
            correlation = None
        else:
            root = roots.get((object_type, object_id))
            # a response is never its own root; absent/self links stay null
            correlation = root if (root is not None and root != event_id) \
                else None
        envelopes.append({
            "envelope_version": ENVELOPE_VERSION,
            "id": event_id,
            "type": etype,
            "item_id": row_item,
            "actor": actor,
            "created_at": created_at,
            "payload": {
                "object_type": object_type,
                "object_id": object_id,
                "item_id": row_item,
                "event_payload": payload,
            },
            "correlation_id": correlation,
        })
    return envelopes


def show(conn, event_id):
    """One envelope by id, or None if that event does not project."""
    for envelope in project(conn):
        if envelope["id"] == event_id:
            return envelope
    return None


def render_lines(envelopes):
    lines = []
    for env in envelopes:
        corr = f"corr:#{env['correlation_id']}" if env["correlation_id"] \
            else "corr:-"
        item = f"item:{env['item_id']}" if env["item_id"] else "item:-"
        actor = env["actor"] or "-"
        lines.append(
            f"#{env['id']} {env['type']} {item} {actor} {corr}")
    return lines
