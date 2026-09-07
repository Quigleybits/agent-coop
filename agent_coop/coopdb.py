"""Agent_coop core: deterministic SQLite state transitions. Stdlib only."""
import hashlib, json, os, pathlib, sqlite3, datetime, re, sys, tempfile

from agent_coop import coop_schema
# The taxonomy base is the one CoopError: typed subclasses raised anywhere in
# the core (e.g. MigrationFailed) cross the CLI boundary intact.
from agent_coop.coop_errors import (
    ActiveSessionConflict,
    AddressedTargetMismatch,
    ClaimCollision,
    CoopError,
    DecisionUnobserved,
    FieldAlreadySet,
    HumanLaneViolation,
    IncompleteContract,
    InvalidAgentName,
    InvalidTiming,
    InvalidTransition,
    MigrationFailed,
    NotFound,
    ProjectionPathInvalid,
    ProofReferenceInvalid,
    ProviderConflict,
    ReceiptInvalid,
    ReceiptMissing,
    ReceiptStale,
    ReviewMissing,
    ReviewStale,
    SchemaMismatch,
    SelfReview,
    SessionMismatch,
    StaleClaim,
    UnsafeReclaim,
)

class BoardMissing(CoopError):
    """A read command found no board and must not create one."""
    type = "board_missing"
    default_reason_code = "target_not_found"


# Agent names double as inbox filenames (projection.watch writes inbox/<name>.md), so a
# name is only valid as a plain identifier — this blocks path traversal / absolute-path
# writes via `--as ../x` or `--to /abs/path`.
_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

def valid_agent_name(name):
    return isinstance(name, str) and _AGENT_RE.match(name) is not None

def _default_clock():
    return datetime.datetime.now(datetime.timezone.utc)

# Injectable module clock: tests replace coopdb._clock; production never does.
_clock = _default_clock

def now():
    return _clock().isoformat(timespec="seconds")

# Six missed five-second maintenance polls before a claim lease lapses.
DEFAULT_LEASE_SECONDS = 30
# The preferred owner's fifteen-minute resume window after an answer.
DEFAULT_RESUME_GRACE_SECONDS = 900
MAX_QUESTION_BATCH_SIZE = 8
MAX_QUESTION_BATCH_BYTES = 8192
MAX_QUESTION_TEXT_BYTES = 4096
# The substantive-checkpoint limit: progress_stale beyond this age.
DEFAULT_CHECKPOINT_LIMIT_SECONDS = 900
# A deterministic next-action command must carry a lease long enough for a
# normal headless turn.  The autonomous runner overrides this value when its
# configured turn timeout needs a longer lease.
DEFAULT_ACTION_LEASE_SECONDS = 3600

def _ts(seconds_from_now):
    return (_clock() + datetime.timedelta(seconds=seconds_from_now)
            ).isoformat(timespec="seconds")

# --- the deterministic spine ------------------------------------------------
# One transaction per protocol mutation: state change, its event, and every
# addressed delivery commit together or not at all.

def mutate(conn, fn):
    """Run fn(conn) under BEGIN IMMEDIATE; commit its whole unit or roll the
    whole unit back on any exception and re-raise. Returns fn's result."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(conn)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return result

def _in_mutate(conn, fn):
    """Compose: run fn inside the caller's open transaction, or give it its
    own one-transaction envelope when called at the top level."""
    if conn.in_transaction:
        return fn(conn)
    return mutate(conn, fn)

def _payload_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))

def append_event(conn, *, item_id, event_type, actor_agent_id,
                 actor_session_id, claim_id=None, fencing_token=None, payload):
    """Append one protocol event (same transaction as its state change).
    Cardinality contract: one event per singular domain transition, one per
    affected row in a batch, zero on a no-op."""
    cur = conn.execute(
        "INSERT INTO events(item_id,event_type,actor_agent_id,actor_session_id,"
        "claim_id,fencing_token,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (item_id, event_type, actor_agent_id, actor_session_id,
         claim_id, fencing_token, _payload_json(payload), now()))
    return cur.lastrowid

def deliver(conn, *, recipient, source_event_id, item_id, category, payload):
    """Append one addressed inbox entry for one recipient, in the same
    transaction as its source event. Broadcasts make zero deliver calls."""
    cur = conn.execute(
        "INSERT INTO inbox_entries(recipient_agent_id,source_event_id,item_id,"
        "category,payload_json,created_at) VALUES (?,?,?,?,?,?)",
        (recipient, source_event_id, item_id, category,
         _payload_json(payload), now()))
    return cur.lastrowid

def _unconsumed_entries(conn, agent, item_id=None):
    item_clause = "" if item_id is None else " AND item_id=?"
    params = (agent, agent) if item_id is None else (agent, agent, item_id)
    return conn.execute(
        "SELECT * FROM inbox_entries WHERE recipient_agent_id=? AND "
        "inbox_entry_id > COALESCE((SELECT last_consumed_entry_id "
        "FROM inbox_offsets WHERE agent_id=?),0)" + item_clause +
        " ORDER BY inbox_entry_id", params).fetchall()

def read_inbox(conn, agent, *, peek=False, item_id=None):
    """The canonical addressed-delivery cursor over inbox_entries (global id
    order). Consume advances inbox_offsets exactly through the returned ids,
    atomically with the read — inside the caller's open transaction when one
    exists (checkpoints consume this way), else in its own. Peek never
    writes; projection, status, and packet reads use peek only. Item-scoped
    reads are always peek-only because the offset is board-global: consuming
    a filtered range could silently skip delivery for an unrelated item."""
    if peek or item_id is not None:
        return _unconsumed_entries(conn, agent, item_id=item_id)
    def _consume(conn):
        rows = _unconsumed_entries(conn, agent)
        if rows:
            conn.execute(
                "INSERT INTO inbox_offsets(agent_id,last_consumed_entry_id) "
                "VALUES (?,?) ON CONFLICT(agent_id) DO UPDATE SET "
                "last_consumed_entry_id=excluded.last_consumed_entry_id",
                (agent, rows[-1]["inbox_entry_id"]))
        return rows
    return _in_mutate(conn, _consume)

def connect(db_path, *, require_current=False):
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        if require_current:
            require_current_schema(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except BaseException:
        conn.close()
        raise

def init_db(conn):
    try:
        coop_schema.initialize(conn)
    except coop_schema.SchemaError as exc:
        raise SchemaMismatch(str(exc)) from exc

def migrate_db(
    db_path, backup_path=None, confirm_legacy_clients_stopped=False
):
    try:
        return coop_schema.migrate(
            db_path,
            backup_path=backup_path,
            confirm_legacy_clients_stopped=confirm_legacy_clients_stopped,
        )
    except coop_schema.SchemaError as exc:
        raise MigrationFailed(str(exc)) from exc

def require_current_schema(conn):
    try:
        coop_schema.require_current(conn)
    except coop_schema.SchemaError as exc:
        raise SchemaMismatch(str(exc)) from exc

def register_agent(conn, name, kind=None):
    if not valid_agent_name(name):
        raise InvalidAgentName(f"invalid agent name: {name!r} (letters/digits/._- only, max 64, no path separators)")
    def _register(conn):
        conn.execute("INSERT OR IGNORE INTO agents(name,kind,registered_at) VALUES (?,?,?)",
                     (name, kind, now()))
    _in_mutate(conn, _register)

def list_agents(conn):
    return conn.execute(
        "SELECT name,kind,provider,registered_at FROM agents "
        "ORDER BY registered_at,name"
    ).fetchall()

def _ensure_room(conn, name):
    conn.execute("INSERT OR IGNORE INTO rooms(name,created_at) VALUES (?,?)", (name, now()))
    return conn.execute("SELECT id FROM rooms WHERE name=?", (name,)).fetchone()["id"]

def post_message(conn, from_agent, body, kind="chat", room="#general",
                 to_agent=None, item_id=None, checkpoint=None):
    def _post(conn):
        register_agent(conn, from_agent)
        if to_agent:
            register_agent(conn, to_agent)
        room_id = _ensure_room(conn, room)
        cur = conn.execute(
            "INSERT INTO messages(room_id,item_id,from_agent,to_agent,kind,checkpoint,body,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (room_id, item_id, from_agent, to_agent, kind, checkpoint, body, now()))
        return cur.lastrowid
    return _in_mutate(conn, _post)

# --- non-binding messages, version 4 ----------------------------------------
# Messages inform. They never answer a structured question, transfer
# ownership, approve work, amend a contract, or complete anything — no code
# path from say touches item or claim state.

def say(conn, *, session_id, body, to_agent=None, item_id=None,
        room="#general"):
    """Post one non-binding board message. Agent-authored requires a live
    session (no claim — a message cannot change active protocol state); a
    null session is the trusted-local human. Addressed messages append the
    message row, its event, and exactly one recipient inbox entry
    atomically; broadcasts append no recipient entries."""
    if not isinstance(body, str) or not body.strip():
        raise InvalidTransition("a message needs a non-empty body")
    # An unset COOP_SESSION_ID reaches the CLI as "" — normalize so the
    # human path never writes an empty-string session foreign key.
    session_id = session_id or None

    def _say(conn):
        actor = _session_actor(conn, session_id) if session_id else "human"
        if to_agent is not None:
            target = conn.execute(
                "SELECT name FROM agents WHERE name=?",
                (to_agent,)).fetchone()
            if target is None:
                raise NotFound(
                    f"unknown recipient {to_agent!r}; messages address "
                    "registered agents")
        if item_id is not None:
            _item_row(conn, item_id)
        room_id = _ensure_room(conn, room or "#general")
        cur = conn.execute(
            "INSERT INTO messages(room_id,item_id,from_agent,to_agent,kind,"
            "body,created_at) VALUES (?,?,?,?,'chat',?,?)",
            (room_id, item_id, actor, to_agent, body.strip(), now()))
        message_id = cur.lastrowid
        payload = {"message_id": message_id}
        if to_agent:
            payload["to"] = to_agent
        if item_id:
            payload["item_id"] = item_id
        event_id = append_event(
            conn, item_id=item_id, event_type="message_posted",
            actor_agent_id=actor, actor_session_id=session_id,
            payload=payload)
        if to_agent is not None:
            deliver(conn, recipient=to_agent, source_event_id=event_id,
                    item_id=item_id, category="message",
                    payload={"message_id": message_id, "from": actor,
                             "body": body.strip(),
                             **({"item_id": item_id} if item_id else {})})
        return message_id

    return mutate(conn, _say)

# The legacy message-offset cursor (get_inbox/peek_inbox and helpers) was
# deleted: inbox_entries + read_inbox is the one canonical
# delivery cursor. The agent_offsets TABLE stays as preserved history per
# the migration rules; nothing reads it as a cursor.

# --- pre-release mutation surface retired -------------------------------------
# checkpoint (message-kind), update_item, done_item, claim_item,
# claim_next_item, assign_item, request_review, submit_review, debate writes,
# and record_decision are gone: the version 4 protocol replaces them with
# claim-bound guarded transitions (Tasks 5-9). Reads over legacy history stay.

def item_owner(conn, item_id):
    """Legacy READ: the pre-release build's assignment-derived owner label, used only
    by the status render over migrated history. Never an authority."""
    row = conn.execute(
        "SELECT agent FROM assignments WHERE item_id=? AND role='owner' AND state='active'",
        (item_id,)).fetchone()
    return row["agent"] if row else None

# --- sessions and identity --------------------------------------------------
# Provider is observable launch metadata bound once per agent identity; it is
# never a routing or eligibility policy. Session lifecycle ops are the DB legs
# the supervisor drives; nothing here touches processes.

SESSION_TERMINAL_MAP = {
    "child_exit": "exited",
    "cancelled": "cancelled",
    "max_runtime": "timed_out",
    "checkpoint_timeout": "timed_out",
    "launch_failed": "exited",
    # Peer agent recovery: abandoned session (no recent liveness) so a
    # wedge can be released without the human mid-run.
    "abandoned": "exited",
}

# Seconds without session last_seen before a peer may abandon that session
# and release its stale claim (agent recovery, not human admin release).
DEFAULT_ABANDON_SECONDS = 180

def register_or_bind_agent(conn, *, agent_id, provider):
    """Register a launching agent, binding its provider exactly once. A
    null-provider identity (migrated, or auto-registered by task seeding)
    binds atomically on its first supervised launch; a later launch under a
    different provider is refused — identity continuity, not routing."""
    if agent_id == "human":
        raise HumanLaneViolation(
            "the reserved human actor may never own a supervised session; "
            "choose another --name")
    if not valid_agent_name(agent_id):
        raise InvalidAgentName(f"invalid agent name: {agent_id!r}")
    if not valid_agent_name(provider):
        raise InvalidAgentName(f"invalid provider name: {provider!r}")

    def _bind(conn):
        row = conn.execute(
            "SELECT provider FROM agents WHERE name=?", (agent_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO agents(name,provider,registered_at) "
                "VALUES (?,?,?)", (agent_id, provider, now()))
            return
        bound = row["provider"]
        if bound is None:
            conn.execute(
                "UPDATE agents SET provider=? WHERE name=? AND provider IS NULL",
                (provider, agent_id))
            return
        if bound != provider:
            raise ProviderConflict(
                f"agent {agent_id!r} is bound to provider {bound!r}; launch "
                f"the {provider!r} harness with a different --name")

    return _in_mutate(conn, _bind)

def insert_session(conn, *, session_id, agent_id, provider, command, cwd,
                   max_runtime_s, grace_s, stdin_isatty=None):
    """Insert one running supervised-session row (the supervisor
    calls this between preparing and releasing the process tree).
    stdin_isatty is recorded verbatim in the session_started payload:
    true/false from a caller that observed the launch TTY (the supervisor),
    null from one that did not (direct seeding) — mechanical
    evidence, the three states deliberately distinguishable."""
    if not isinstance(command, (list, tuple)) or not command:
        raise InvalidTransition("a session needs a non-empty opaque command")

    def _insert(conn):
        # Self-heal first: an orphaned running session for this agent that is
        # already past its max_runtime is finished here, so this insert is not
        # wedged by a launcher that died without finish_session. A session
        # still within its runtime remains a genuine active_session_conflict.
        sweep_expired_sessions(conn, agent_id=agent_id)
        register_or_bind_agent(conn, agent_id=agent_id, provider=provider)
        try:
            conn.execute(
                "INSERT INTO sessions(session_id,agent_id,provider,status,"
                "command_json,working_directory,started_at,last_seen_at,"
                "max_runtime_seconds,shutdown_grace_seconds) "
                "VALUES (?,?,?,'running',?,?,?,?,?,?)",
                (session_id, agent_id, provider,
                 json.dumps(list(command), separators=(",", ":")),
                 str(cwd), now(), now(), max_runtime_s, grace_s))
        except sqlite3.IntegrityError as exc:
            # ux_sessions_one_running violations surface as the column text.
            if "sessions.agent_id" in str(exc):
                raise ActiveSessionConflict(
                    f"agent {agent_id!r} already has a running session; "
                    f"launch with a distinct --name") from exc
            raise
        append_event(
            conn, item_id=None, event_type="session_started",
            actor_agent_id=agent_id, actor_session_id=session_id,
            payload={"provider": provider, "command": list(command),
                     "working_directory": str(cwd),
                     "stdin_isatty": stdin_isatty})
        return session_id

    return mutate(conn, _insert)

def finish_session(conn, session_id, *, status, reason, exit_code=None):
    """Record one terminal session transition, exactly once. The reason is
    authoritative and the status must agree with the design's mapping."""
    expected = SESSION_TERMINAL_MAP.get(reason)
    if expected is None:
        raise InvalidTransition(
            f"unknown termination reason {reason!r} "
            f"(want one of {sorted(SESSION_TERMINAL_MAP)})")
    if status != expected:
        raise InvalidTransition(
            f"termination reason {reason!r} maps to status {expected!r}, "
            f"not {status!r}")

    def _finish(conn):
        row = conn.execute(
            "SELECT agent_id, status FROM sessions WHERE session_id=?",
            (session_id,)).fetchone()
        if row is None:
            raise SessionMismatch(f"unknown session {session_id!r}")
        if row["status"] != "running":
            raise InvalidTransition(
                f"session {session_id!r} is already terminal "
                f"({row['status']}); terminal transitions happen exactly once")
        stamp = now()
        conn.execute(
            "UPDATE sessions SET status=?, termination_reason=?, exit_code=?, "
            "exited_at=?, last_seen_at=? WHERE session_id=? AND status='running'",
            (status, reason, exit_code, stamp, stamp, session_id))
        append_event(
            conn, item_id=None, event_type="session_finished",
            actor_agent_id=row["agent_id"], actor_session_id=session_id,
            payload={"status": status, "reason": reason,
                     "exit_code": exit_code})

    return mutate(conn, _finish)

def sweep_expired_sessions(conn, *, agent_id=None, now=None):
    """Finish every running session past its max_runtime, idempotently — the
    session-level mirror of sweep_expired (claims). An orphaned session whose
    launcher died without calling finish_session self-heals here, so a fresh
    launch for that agent no longer wedges forever on active_session_conflict.
    Zero schema delta: expiry is derived from started_at + max_runtime_seconds
    (no expires column). A second sweep of the same rows is a no-op — they are
    already terminal. Optional agent_id scopes the sweep. Returns the count."""
    def _sweep(conn):
        cutoff = _clock() if now is None else now
        stamp = cutoff.isoformat(timespec="seconds")
        query = ("SELECT session_id, agent_id, started_at, max_runtime_seconds"
                 " FROM sessions WHERE status='running'")
        params = ()
        if agent_id is not None:
            query += " AND agent_id=?"
            params = (agent_id,)
        swept = 0
        for row in conn.execute(query, params).fetchall():
            started = datetime.datetime.fromisoformat(row["started_at"])
            expires = started + datetime.timedelta(
                seconds=row["max_runtime_seconds"])
            if expires > cutoff:
                continue
            conn.execute(
                "UPDATE sessions SET status='timed_out', "
                "termination_reason='max_runtime', exited_at=?, last_seen_at=? "
                "WHERE session_id=? AND status='running'",
                (stamp, stamp, row["session_id"]))
            append_event(
                conn, item_id=None, event_type="session_finished",
                actor_agent_id=row["agent_id"],
                actor_session_id=row["session_id"],
                payload={"status": "timed_out", "reason": "max_runtime",
                         "exit_code": None, "swept": True})
            swept += 1
        return swept
    return _in_mutate(conn, _sweep)

# --- task contracts and the read path ---------------------------------------

CONTRACT_TEXT_FIELDS = (
    "title", "objective", "scope", "done_when", "output_contract", "context")
CONTRACT_LIST_FIELDS = ("allowed_actions", "stop_conditions")
CONTRACT_FIELDS = CONTRACT_TEXT_FIELDS + CONTRACT_LIST_FIELDS

# Serializers redact internal generations everywhere an agent can read.
_REDACTED_COLUMNS = frozenset({"fencing_token", "execution_fencing_token"})

# Session references are shortened to a display prefix everywhere an agent
# can read them. Identity is cooperative: `COOP_SESSION_ID` is self-asserted,
# so a full peer session id would let a misbehaving process act as that peer.
# The prefix still lets a human correlate rows with the dashboard's sessions
# strip, which shows the same eight characters.
_SESSION_REF_COLUMNS = frozenset({
    "owner_session_id", "actor_session_id", "asked_by_session",
    "answered_by_session", "submitted_by_session", "from_session",
    "decided_by_session",
})
SESSION_DISPLAY_CHARS = 8


def short_session_id(value):
    """Display prefix of a session id; ``None`` stays ``None``."""
    if value is None:
        return None
    return str(value)[:SESSION_DISPLAY_CHARS]

def resolve_actor(conn, session_id, claimed_agent=None):
    """CLI-boundary actor resolution: no session means the reserved human
    actor; with a session, the row must exist, be running, and match any
    claimed agent. Returns (actor, session_id)."""
    if not session_id:
        return "human", None
    row = conn.execute(
        "SELECT agent_id, status FROM sessions WHERE session_id=?",
        (session_id,)).fetchone()
    if row is None:
        raise SessionMismatch(
            f"unknown session {session_id!r}",
            reason_code="session_unavailable",
            evidence={},
        )
    if row["status"] != "running":
        raise SessionMismatch(
            f"session {session_id!r} is {row['status']}, not running",
            reason_code="session_unavailable",
            evidence={
                "session_status": row["status"],
                "required_status": "running",
            },
        )
    if claimed_agent and claimed_agent != row["agent_id"]:
        raise SessionMismatch(
            f"session {session_id!r} belongs to {row['agent_id']!r}, "
            f"not {claimed_agent!r}",
            reason_code="actor_mismatch",
            evidence={
                "actor_agent_id": claimed_agent,
                "required_agent_id": row["agent_id"],
            },
        )
    return row["agent_id"], session_id

def _require_live_session(conn, session_id, actor):
    """In-transaction session guard for agent mutations. The human actor
    never has a session; an agent always needs a live matching one."""
    if session_id is None:
        if actor == "human":
            return None
        raise SessionMismatch(
            f"agent {actor!r} may mutate only inside a supervised session",
            reason_code="session_unavailable",
            evidence={
                "actor_agent_id": actor,
                "required_status": "running",
            },
        )
    if actor == "human":
        required = conn.execute(
            "SELECT agent_id FROM sessions WHERE session_id=?",
            (session_id,)).fetchone()
        evidence = {"actor_agent_id": actor}
        if required is not None:
            evidence["required_agent_id"] = required["agent_id"]
        raise SessionMismatch(
            "the reserved human actor never has a supervised session",
            reason_code="actor_mismatch",
            evidence=evidence,
        )
    row = conn.execute(
        "SELECT session_id, agent_id, status FROM sessions WHERE session_id=?",
        (session_id,)).fetchone()
    if row is None:
        raise SessionMismatch(
            f"unknown session {session_id!r}",
            reason_code="session_unavailable",
            evidence={},
        )
    if row["status"] != "running":
        raise SessionMismatch(
            f"session {session_id!r} is {row['status']}, not running",
            reason_code="session_unavailable",
            evidence={
                "session_status": row["status"],
                "required_status": "running",
            },
        )
    if row["agent_id"] != actor:
        raise SessionMismatch(
            f"session {session_id!r} belongs to {row['agent_id']!r}, "
            f"not {actor!r}",
            reason_code="actor_mismatch",
            evidence={
                "actor_agent_id": actor,
                "required_agent_id": row["agent_id"],
            },
        )
    return row

def _validated_contract(fields):
    contract = {}
    for field in CONTRACT_TEXT_FIELDS:
        value = fields.get(field)
        if not isinstance(value, str) or not value.strip():
            raise IncompleteContract(
                f"task contract requires a non-empty {field}")
        contract[field] = value.strip()
    for field in CONTRACT_LIST_FIELDS:
        value = fields.get(field)
        if not isinstance(value, (list, tuple)) or not value:
            raise IncompleteContract(
                f"task contract requires a non-empty {field} list")
        cleaned = []
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                raise IncompleteContract(
                    f"{field} entries must be non-empty strings")
            cleaned.append(entry.strip())
        contract[field] = cleaned
    return contract

def _goal_contract(fields):
    """Validate a task contract that may be a goal-only DRAFT: title and
    objective (the goal) are required; the other contract fields may be
    empty/omitted (stored empty). A full contract validates every field the
    same as before — an empty non-goal field simply yields a draft."""
    contract = {}
    for field in CONTRACT_TEXT_FIELDS:
        value = fields.get(field)
        if field in ("title", "objective"):
            if not isinstance(value, str) or not value.strip():
                raise IncompleteContract(
                    f"a task goal requires a non-empty {field}")
            contract[field] = value.strip()
        else:
            contract[field] = value.strip() \
                if isinstance(value, str) and value.strip() else ""
    for field in CONTRACT_LIST_FIELDS:
        value = fields.get(field)
        if isinstance(value, (list, tuple)) and value:
            cleaned = []
            for entry in value:
                if not isinstance(entry, str) or not entry.strip():
                    raise IncompleteContract(
                        f"{field} entries must be non-empty strings")
                cleaned.append(entry.strip())
            contract[field] = cleaned
        else:
            contract[field] = []
    return contract


def contract_fingerprint(fields):
    """Hash only the normalized public contract fields.

    Accepts rendered dictionaries, item-show dictionaries, or SQLite rows.
    List fields may therefore arrive either as lists or deterministic JSON.
    Unknown fields (ownership, timestamps, status) never influence the hash.
    """
    normalized = {}
    for field in CONTRACT_TEXT_FIELDS:
        value = fields[field]
        if not isinstance(value, str):
            raise TypeError(f"{field} must be text")
        normalized[field] = value.strip()
    for field in CONTRACT_LIST_FIELDS:
        value = fields[field]
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"{field} must be a list")
        if any(not isinstance(entry, str) for entry in value):
            raise TypeError(f"{field} entries must be text")
        normalized[field] = [entry.strip() for entry in value]
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _template_provenance(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
            "name", "version", "contract_fingerprint"}:
        raise IncompleteContract("invalid template provenance")
    name = value["name"]
    version = value["version"]
    fingerprint = value["contract_fingerprint"]
    if (
            not isinstance(name, str)
            or not name.strip()
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None):
        raise IncompleteContract("invalid template provenance")
    return {
        "name": name.strip(),
        "version": version,
        "contract_fingerprint": fingerprint,
    }

def create_item(conn, *, actor, session_id, title, objective, scope="",
                done_when="", output_contract="", context="",
                allowed_actions=(), stop_conditions=(), owner=None,
                next_actor=None, review_waiver_reason=None,
                review_quorum=None, template_provenance=None):
    """Create one complete task contract. Every required field must be
    non-empty; list fields are stored as deterministic JSON arrays. The
    review-waiver pairing rule: review_required=0 iff a
    non-empty waiver reason — the reason's presence is the single source."""
    if not valid_agent_name(actor):
        raise InvalidAgentName(f"invalid actor name: {actor!r}")
    if review_waiver_reason is not None and (
            not isinstance(review_waiver_reason, str)
            or not review_waiver_reason.strip()):
        raise IncompleteContract(
            "a review waiver requires a non-empty reason")
    waiver = (review_waiver_reason.strip()
              if review_waiver_reason is not None else None)
    if waiver is not None and review_quorum is not None:
        raise IncompleteContract(
            "review_quorum and a review waiver are mutually exclusive")
    quorum = 0 if waiver is not None else _normalize_review_quorum(
        review_quorum, default=1)
    contract = _goal_contract({
        "title": title, "objective": objective, "scope": scope,
        "done_when": done_when, "output_contract": output_contract,
        "context": context, "allowed_actions": allowed_actions,
        "stop_conditions": stop_conditions,
    })
    provenance = _template_provenance(template_provenance)
    for label, agent in (("owner", owner), ("next_actor", next_actor)):
        if agent is None:
            continue
        if agent == "human":
            raise HumanLaneViolation(
                f"the reserved human actor cannot be a task {label}")
        if not valid_agent_name(agent):
            raise InvalidAgentName(f"invalid {label} name: {agent!r}")

    def _create(conn):
        _require_live_session(conn, session_id, actor)
        register_agent(conn, actor)
        for agent in dict.fromkeys(a for a in (owner, next_actor) if a):
            register_agent(conn, agent)
        cur = conn.execute(
            "INSERT INTO items(title,objective,scope,done_when,"
            "output_contract,context,allowed_actions,stop_conditions,status,"
            "owner_agent_id,next_actor_agent_id,contract_version,created_by,"
            "review_required,review_waiver_reason,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'todo',?,?,1,?,?,?,?,?)",
            (contract["title"], contract["objective"], contract["scope"],
             contract["done_when"], contract["output_contract"],
             contract["context"],
             json.dumps(contract["allowed_actions"], separators=(",", ":")),
             json.dumps(contract["stop_conditions"], separators=(",", ":")),
             owner, next_actor, actor,
             0 if waiver else 1, waiver, now(), now()))
        item_id = cur.lastrowid
        event_id = append_event(
            conn, item_id=item_id, event_type="item_created",
            actor_agent_id=actor, actor_session_id=session_id,
            payload={
                "item_id": item_id,
                "title": contract["title"],
                "review_quorum": quorum,
                **({"template": provenance} if provenance is not None else {}),
            })
        for recipient in dict.fromkeys(
                a for a in (owner, next_actor) if a and a != actor):
            deliver(conn, recipient=recipient, source_event_id=event_id,
                    item_id=item_id, category="assignment",
                    payload={"item_id": item_id, "title": contract["title"]})
        return item_id

    return mutate(conn, _create)

DEFINABLE_FIELDS = ("scope", "done_when", "output_contract", "context",
                    "allowed_actions", "stop_conditions")

def define_item(conn, *, claim_id, session_id, actor, fields):
    """Agent-lane contract authoring for a goal-task. The implementation-claim
    holder FILLS the item's EMPTY contract fields — never title/objective (the
    human's goal) and never a field already set (`field_already_set`; the human
    lane revises a set field). Fills any of scope/done_when/output_contract/
    context/allowed_actions/stop_conditions; once every required field is
    present the contract is complete and normal work proceeds. One
    `item_defined` event carries the delta; no delivery. Full rollback on any
    refusal (the whole call is one transaction)."""
    fields = dict(fields or {})
    unknown = sorted(set(fields) - set(DEFINABLE_FIELDS))
    if unknown:
        raise IncompleteContract(
            "define fills only the non-goal contract fields; unknown or "
            f"non-definable field(s): {', '.join(unknown)} — title/objective "
            "are the human's goal (change them via the human-lane revise)")

    def _define(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        if item["status"] == "done":
            raise InvalidTransition(
                f"item {item['id']} is done; its contract is immutable")
        delta = {}
        for field in DEFINABLE_FIELDS:
            if field not in fields:
                continue
            value = fields[field]
            if field in CONTRACT_LIST_FIELDS:
                cleaned = [e.strip() for e in value
                           if isinstance(e, str) and e.strip()] \
                    if isinstance(value, (list, tuple)) else []
                if not cleaned:
                    continue
                if bool(_parse_list(item[field])):
                    raise FieldAlreadySet(
                        f"{field} is already set; the agent lane only fills "
                        "empty fields (the human lane revises a set one)")
                delta[field] = cleaned
            else:
                new = value.strip() if isinstance(value, str) else ""
                if not new:
                    continue
                current = item[field]
                if current is not None and str(current).strip():
                    raise FieldAlreadySet(
                        f"{field} is already set; the agent lane only fills "
                        "empty fields (the human lane revises a set one)")
                delta[field] = new
        if not delta:
            raise IncompleteContract(
                "define changed nothing; pass at least one empty field to fill")
        stamp = now()
        assignments = ", ".join(f"{f}=?" for f in delta)
        params = [json.dumps(v, separators=(",", ":"))
                  if f in CONTRACT_LIST_FIELDS else v
                  for f, v in delta.items()]
        conn.execute(
            f"UPDATE items SET {assignments}, updated_at=? WHERE id=?",
            (*params, stamp, item["id"]))
        event_id = append_event(
            conn, item_id=item["id"], event_type="item_defined",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload={"item_id": item["id"], "fields": sorted(delta),
                     "defined": delta})
        filled = _item_row(conn, item["id"])
        if is_draft(item) and not contract_incomplete(filled):
            append_event(
                conn, item_id=item["id"],
                event_type="contract_acceptance_required",
                actor_agent_id=actor, actor_session_id=session_id,
                claim_id=claim_id, fencing_token=claim["fencing_token"],
                payload={
                    "item_id": item["id"],
                    "contract_version": filled["contract_version"],
                    "contract_author": actor,
                    "trigger": "agent_defined_goal_contract",
                    "max_rounds": 2,
                })
        return {"item_id": item["id"], "defined": sorted(delta),
                "contract_complete": not contract_incomplete(filled),
                "contract_acceptance": contract_acceptance(
                    conn, item["id"]),
                "event_id": event_id}

    return mutate(conn, _define)


HUDDLE_STANCES = ("proposal", "concern", "support", "revision")
HUDDLE_OUTCOMES = ("accepted", "changes")


def _event_payload(row):
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def contract_acceptance(conn, item_id):
    """Derived acceptance state for the item's current contract version.

    Human-authored complete contracts and legacy items need no extra gate.
    An agent-authored goal contract emits `contract_acceptance_required` and
    stays non-executable until a separate-provider peer closes its bounded
    huddle as accepted.
    """
    item = _item_row(conn, item_id)
    version = item["contract_version"]
    required = None
    for row in conn.execute(
            "SELECT event_id, payload_json FROM events WHERE item_id=? AND "
            "event_type='contract_acceptance_required' ORDER BY event_id DESC",
            (item_id,)):
        payload = _event_payload(row)
        if payload.get("contract_version") == version:
            required = (row["event_id"], payload)
            break
    if required is None:
        return {"required": False, "state": "not_required",
                "contract_version": version, "huddle_id": None}
    state, huddle_id = "pending", None
    for row in conn.execute(
            "SELECT event_type, payload_json FROM events WHERE item_id=? AND "
            "event_id>? AND event_type IN "
            "('huddle_opened','contract_accepted',"
            "'contract_changes_requested') ORDER BY event_id",
            (item_id, required[0])):
        payload = _event_payload(row)
        if payload.get("contract_version") != version:
            continue
        if row["event_type"] == "huddle_opened":
            huddle_id = payload.get("huddle_id")
        elif row["event_type"] == "contract_accepted":
            state = "accepted"
        elif row["event_type"] == "contract_changes_requested":
            state = "changes"
    return {"required": True, "state": state,
            "contract_version": version, "huddle_id": huddle_id,
            "contract_author": required[1].get("contract_author"),
            "max_rounds": required[1].get("max_rounds", 2)}


def _huddle(conn, huddle_id):
    row = conn.execute(
        "SELECT * FROM debates WHERE id=?", (huddle_id,)).fetchone()
    if row is None:
        raise NotFound(
            f"no huddle {huddle_id}",
            reason_code="target_not_found",
            evidence={"huddle_id": huddle_id},
        )
    opened = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='huddle_opened' "
        "AND json_extract(payload_json, '$.huddle_id')=? "
        "ORDER BY event_id DESC LIMIT 1", (huddle_id,)).fetchone()
    if opened is None:
        raise InvalidTransition(
            f"debate {huddle_id} is legacy history, not a live huddle",
            reason_code="transition_not_available",
            evidence={
                "item_id": row["item_id"],
                "huddle_id": huddle_id,
                "current_state": "legacy",
                "required_state": "live_huddle",
            },
        )
    return row, _event_payload(opened)


def _open_contract_huddle(conn, *, claim, session_id, actor, item):
    state = contract_acceptance(conn, item["id"])
    if not state["required"] or state["state"] != "pending":
        raise InvalidTransition(
            f"item {item['id']} contract acceptance is {state['state']}; "
            "no contract huddle can open",
            reason_code="transition_not_available",
            evidence={
                "item_id": item["id"],
                "current_state": state["state"],
                "required_state": "pending",
            },
        )
    existing = conn.execute(
        "SELECT d.id FROM debates d JOIN events e ON e.item_id=d.item_id "
        "AND e.event_type='huddle_opened' "
        "WHERE d.item_id=? AND d.status='open' AND "
        "json_extract(e.payload_json, '$.huddle_id')=d.id AND "
        "json_extract(e.payload_json, '$.contract_version')=? "
        "ORDER BY d.id DESC LIMIT 1",
        (item["id"], item["contract_version"])).fetchone()
    if existing is not None:
        raise InvalidTransition(
            f"item {item['id']} already has open huddle {existing['id']}",
            reason_code="transition_not_available",
            evidence={
                "item_id": item["id"],
                "huddle_id": existing["id"],
                "current_state": "open",
                "constraint": "single_open_contract_huddle",
            },
        )
    owner_provider = _agent_provider_bucket(conn, actor)
    peers = []
    seen_providers = {owner_provider}
    for row in conn.execute(
            "SELECT agent_id, provider, started_at FROM sessions WHERE "
            "status='running' AND agent_id NOT IN (?, 'human') "
            "ORDER BY started_at, agent_id", (actor,)):
        provider = row["provider"] or _agent_provider_bucket(
            conn, row["agent_id"])
        if provider in seen_providers:
            continue
        seen_providers.add(provider)
        peers.append(row["agent_id"])
        if len(peers) == 2:
            break
    if not peers:
        raise InvalidTransition(
            "contract acceptance huddle requires a live peer from a "
            "different provider",
            reason_code="peer_unavailable",
            evidence={
                "item_id": item["id"],
                "session_status": "missing",
                "constraint": "different_provider_peer_required",
            },
        )
    participants = [actor, *peers]
    stamp = now()
    cur = conn.execute(
        "INSERT INTO debates(item_id,topic,status,judge,created_by,created_at) "
        "VALUES (?,?,'open',?,?,?)",
        (item["id"],
         f"contract acceptance v{item['contract_version']}",
         peers[0], actor, stamp))
    huddle_id = cur.lastrowid
    payload = {
        "huddle_id": huddle_id, "item_id": item["id"],
        "kind": "contract_acceptance",
        "trigger": "agent_defined_goal_contract",
        "contract_version": item["contract_version"],
        "owner": actor, "participants": participants, "max_rounds": 2,
    }
    event_id = append_event(
        conn, item_id=item["id"], event_type="huddle_opened",
        actor_agent_id=actor, actor_session_id=session_id,
        claim_id=claim["claim_id"], fencing_token=claim["fencing_token"],
        payload=payload)
    for peer in peers:
        deliver(conn, recipient=peer, source_event_id=event_id,
                item_id=item["id"], category="huddle",
                payload={"huddle_id": huddle_id, "item_id": item["id"],
                         "kind": "contract_acceptance"})
    return {**payload, "event_id": event_id}


def open_contract_huddle(conn, *, claim_id, session_id, actor):
    """Open the one bounded peer huddle required by a goal contract."""
    def _open(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        if contract_incomplete(item):
            raise IncompleteContract(
                f"item {item['id']} contract is incomplete; define it first",
                reason_code="contract_incomplete",
                evidence={
                    "item_id": item["id"],
                    "constraint": "executable_contract_required",
                },
            )
        return _open_contract_huddle(
            conn, claim=claim, session_id=session_id, actor=actor, item=item)
    return mutate(conn, _open)


def _open_plan_huddle(conn, *, claim, session_id, actor, item, proposal_ref):
    """Post-acceptance multi-writer plan critique. Advisory only.

    Distinct from contract_acceptance: never emits contract_accepted /
    contract_changes_requested and never mutates contract_version.
    """
    state = contract_acceptance(conn, item["id"])
    if state["required"] and state["state"] != "accepted":
        raise InvalidTransition(
            f"item {item['id']} contract acceptance is {state['state']}; "
            "plan huddle requires an accepted (or not-required) contract",
            reason_code="contract_acceptance_required",
            evidence={
                "item_id": item["id"],
                "current_state": state["state"],
                "required_state": "accepted",
            },
        )
    if contract_incomplete(item):
        raise IncompleteContract(
            f"item {item['id']} contract is incomplete; define it first",
            reason_code="contract_incomplete",
            evidence={
                "item_id": item["id"],
                "constraint": "executable_contract_required",
            },
        )
    existing = conn.execute(
        "SELECT d.id FROM debates d JOIN events e ON e.item_id=d.item_id "
        "AND e.event_type='huddle_opened' "
        "WHERE d.item_id=? AND d.status='open' AND "
        "json_extract(e.payload_json, '$.huddle_id')=d.id AND "
        "json_extract(e.payload_json, '$.kind')='implementation_plan' "
        "ORDER BY d.id DESC LIMIT 1", (item["id"],)).fetchone()
    if existing is not None:
        raise InvalidTransition(
            f"item {item['id']} already has open plan huddle {existing['id']}",
            reason_code="transition_not_available",
            evidence={
                "item_id": item["id"],
                "huddle_id": existing["id"],
                "current_state": "open",
                "constraint": "single_open_plan_huddle",
            },
        )
    owner_provider = _agent_provider_bucket(conn, actor)
    peers = []
    seen_providers = {owner_provider}
    for row in conn.execute(
            "SELECT agent_id, provider, started_at FROM sessions WHERE "
            "status='running' AND agent_id NOT IN (?, 'human') "
            "ORDER BY started_at, agent_id", (actor,)):
        provider = row["provider"] or _agent_provider_bucket(
            conn, row["agent_id"])
        if provider in seen_providers:
            continue
        seen_providers.add(provider)
        peers.append(row["agent_id"])
        if len(peers) == 2:
            break
    if not peers:
        raise InvalidTransition(
            "plan huddle requires a live peer from a different provider",
            reason_code="peer_unavailable",
            evidence={
                "item_id": item["id"],
                "session_status": "missing",
                "constraint": "different_provider_peer_required",
            },
        )
    participants = [actor, *peers]
    stamp = now()
    cur = conn.execute(
        "INSERT INTO debates(item_id,topic,status,judge,created_by,created_at) "
        "VALUES (?,?,'open',?,?,?)",
        (item["id"], "implementation plan critique", peers[0], actor, stamp))
    huddle_id = cur.lastrowid
    payload = {
        "huddle_id": huddle_id, "item_id": item["id"],
        "kind": "implementation_plan",
        "trigger": "post_acceptance_plan_critique",
        "contract_version": item["contract_version"],
        "proposal_ref": proposal_ref,
        "owner": actor, "participants": participants, "max_rounds": 2,
    }
    event_id = append_event(
        conn, item_id=item["id"], event_type="huddle_opened",
        actor_agent_id=actor, actor_session_id=session_id,
        claim_id=claim["claim_id"], fencing_token=claim["fencing_token"],
        payload=payload)
    for peer in peers:
        deliver(conn, recipient=peer, source_event_id=event_id,
                item_id=item["id"], category="huddle",
                payload={"huddle_id": huddle_id, "item_id": item["id"],
                         "kind": "implementation_plan"})
    return {**payload, "event_id": event_id}


def open_plan_huddle(conn, *, claim_id, session_id, actor, proposal_ref=None):
    """Open a bounded post-acceptance plan huddle under an implementation claim.

    Opt-in only (CLI / explicit agent choice). Never forced by
    ``_derive_next_action``: complete human contracts retain the fast path
    claim → work → receipt → review without a plan-huddle gate.
    """
    ref = (proposal_ref or "").strip() or "implementation plan"

    def _open(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        return _open_plan_huddle(
            conn, claim=claim, session_id=session_id, actor=actor, item=item,
            proposal_ref=ref)
    return mutate(conn, _open)


def post_huddle(conn, *, huddle_id, session_id, actor, stance, body):
    """Post once per participant per round; rounds advance only together."""
    if stance not in HUDDLE_STANCES:
        raise InvalidTransition(
            f"huddle stance must be one of: {', '.join(HUDDLE_STANCES)}",
            reason_code="input_invalid",
            evidence={
                "huddle_id": huddle_id,
                "constraint": "valid_huddle_stance",
            },
        )
    if not isinstance(body, str) or not body.strip():
        raise InvalidTransition(
            "a huddle post requires a non-empty body",
            reason_code="input_invalid",
            evidence={
                "huddle_id": huddle_id,
                "constraint": "non_empty_huddle_body",
            },
        )

    def _post(conn):
        _require_live_session(conn, session_id, actor)
        row, meta = _huddle(conn, huddle_id)
        if row["status"] != "open":
            raise InvalidTransition(
                f"huddle {huddle_id} is {row['status']}, not open",
                reason_code="transition_not_available",
                evidence={
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "current_status": row["status"],
                    "required_status": "open",
                },
            )
        participants = meta.get("participants") or []
        if actor not in participants:
            raise AddressedTargetMismatch(
                f"{actor!r} is not a participant in huddle {huddle_id}",
                reason_code="addressed_target_mismatch",
                evidence={
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "actor_agent_id": actor,
                    "actionable_agents": participants,
                },
            )
        prior = conn.execute(
            "SELECT round FROM debate_posts WHERE debate_id=? AND agent=? "
            "ORDER BY round", (huddle_id, actor)).fetchall()
        round_no = len(prior) + 1
        max_rounds = int(meta.get("max_rounds", 2))
        if round_no > max_rounds:
            raise InvalidTransition(
                f"huddle {huddle_id} is bounded to {max_rounds} rounds",
                reason_code="transition_not_available",
                evidence={
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "required_count": max_rounds,
                    "observed_count": round_no,
                    "constraint": "huddle_round_limit",
                },
            )
        if round_no > 1:
            missing = [p for p in participants if conn.execute(
                "SELECT 1 FROM debate_posts WHERE debate_id=? AND agent=? "
                "AND round=?", (huddle_id, p, round_no - 1)).fetchone()
                is None]
            if missing:
                evidence = {
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "actionable_agents": missing,
                    "constraint": "prior_round_peer_posts_required",
                }
                if len(missing) == 1:
                    evidence["target_agent_id"] = missing[0]
                raise InvalidTransition(
                    f"huddle {huddle_id} round {round_no - 1} awaits: "
                    + ", ".join(missing),
                    reason_code="awaiting_peer",
                    evidence=evidence,
                )
        stamp = now()
        cur = conn.execute(
            "INSERT INTO debate_posts(debate_id,round,agent,body,created_at) "
            "VALUES (?,?,?,?,?)",
            (huddle_id, round_no, actor,
             _payload_json({"stance": stance, "body": body.strip()}), stamp))
        event_id = append_event(
            conn, item_id=row["item_id"], event_type="huddle_posted",
            actor_agent_id=actor, actor_session_id=session_id,
            payload={"huddle_id": huddle_id, "item_id": row["item_id"],
                     "post_id": cur.lastrowid, "round": round_no,
                     "stance": stance})
        for peer in participants:
            if peer != actor:
                deliver(conn, recipient=peer, source_event_id=event_id,
                        item_id=row["item_id"], category="huddle",
                        payload={"huddle_id": huddle_id,
                                 "item_id": row["item_id"],
                                 "round": round_no, "from": actor,
                                 "stance": stance})
        return {"huddle_id": huddle_id, "post_id": cur.lastrowid,
                "round": round_no, "stance": stance,
                "event_id": event_id}
    return mutate(conn, _post)


def _huddle_latest_posts(conn, huddle_id):
    latest = {}
    for row in conn.execute(
            "SELECT * FROM debate_posts WHERE debate_id=? ORDER BY id",
            (huddle_id,)):
        try:
            payload = json.loads(row["body"])
        except (TypeError, ValueError):
            payload = {"stance": None, "body": row["body"]}
        latest[row["agent"]] = {
            "round": row["round"], "stance": payload.get("stance"),
            "body": payload.get("body"), "post_id": row["id"]}
    return latest


def close_huddle(conn, *, huddle_id, session_id, actor, outcome, summary):
    """Close a contract or plan huddle with a structured peer outcome.

    Contract huddles emit contract_accepted / contract_changes_requested.
    Plan huddles (kind=implementation_plan) emit plan_huddle_concurred /
    plan_huddle_changes_requested and never touch contract finality.
    """
    if outcome not in HUDDLE_OUTCOMES:
        raise InvalidTransition(
            f"contract huddle outcome must be one of: "
            f"{', '.join(HUDDLE_OUTCOMES)}",
            reason_code="input_invalid",
            evidence={
                "huddle_id": huddle_id,
                "constraint": "valid_huddle_outcome",
            },
        )
    if not isinstance(summary, str) or not summary.strip():
        raise InvalidTransition(
            "closing a huddle requires a non-empty summary",
            reason_code="input_invalid",
            evidence={
                "huddle_id": huddle_id,
                "constraint": "non_empty_huddle_summary",
            },
        )

    def _close(conn):
        _require_live_session(conn, session_id, actor)
        row, meta = _huddle(conn, huddle_id)
        if row["status"] != "open":
            raise InvalidTransition(
                f"huddle {huddle_id} is {row['status']}, not open",
                reason_code="transition_not_available",
                evidence={
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "current_status": row["status"],
                    "required_status": "open",
                },
            )
        participants = meta.get("participants") or []
        if actor not in participants:
            raise AddressedTargetMismatch(
                f"{actor!r} is not a participant in huddle {huddle_id}",
                reason_code="addressed_target_mismatch",
                evidence={
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "actor_agent_id": actor,
                    "actionable_agents": participants,
                },
            )
        owner = meta.get("owner")
        kind = meta.get("kind") or "contract_acceptance"
        is_plan = kind == "implementation_plan"
        if actor == owner:
            raise SelfReview(
                "the huddle owner cannot close their own "
                + ("plan huddle" if is_plan else
                   "agent-authored contract huddle"),
                reason_code="reviewer_is_owner",
                evidence={
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "actor_agent_id": actor,
                    "owner_agent_id": owner,
                },
            )
        if _agent_provider_bucket(conn, actor) == _agent_provider_bucket(
                conn, owner):
            raise SelfReview(
                "huddle close requires a different provider from the owner",
                reason_code="reviewer_is_owner",
                evidence={
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "actor_agent_id": actor,
                    "owner_agent_id": owner,
                    "constraint": "different_provider_reviewer_required",
                },
            )
        latest = _huddle_latest_posts(conn, huddle_id)
        missing = [p for p in participants if p not in latest]
        if missing:
            evidence = {
                "item_id": row["item_id"],
                "huddle_id": huddle_id,
                "actionable_agents": missing,
                "constraint": "first_huddle_post_required",
            }
            if len(missing) == 1:
                evidence["target_agent_id"] = missing[0]
            raise InvalidTransition(
                f"huddle {huddle_id} awaits a first post from: "
                + ", ".join(missing),
                reason_code="awaiting_peer",
                evidence=evidence,
            )
        peers = [p for p in participants if p != owner]
        if outcome == "accepted":
            concerns = [p for p in peers
                        if latest[p]["stance"] != "support"]
            if concerns:
                raise InvalidTransition(
                    ("plan" if is_plan else "contract")
                    + " cannot be accepted while peer concerns remain: "
                    + ", ".join(concerns),
                    reason_code="transition_not_available",
                    evidence={
                        "item_id": row["item_id"],
                        "huddle_id": huddle_id,
                        "current_status": row["status"],
                        "actionable_agents": concerns,
                        "constraint": "peer_support_required",
                    },
                )
        elif latest[actor]["stance"] != "concern":
            raise InvalidTransition(
                "a changes outcome requires the closing peer's latest stance "
                "to be concern",
                reason_code="transition_not_available",
                evidence={
                    "item_id": row["item_id"],
                    "huddle_id": huddle_id,
                    "constraint": "closing_peer_concern_required",
                },
            )
        stamp = now()
        conn.execute(
            "UPDATE debates SET status=?, closed_at=? WHERE id=?",
            (outcome, stamp, huddle_id))
        if is_plan:
            event_type = ("plan_huddle_concurred" if outcome == "accepted" else
                          "plan_huddle_changes_requested")
        else:
            event_type = ("contract_accepted" if outcome == "accepted" else
                          "contract_changes_requested")
        payload = {
            "huddle_id": huddle_id, "item_id": row["item_id"],
            "kind": kind,
            "contract_version": meta.get("contract_version"),
            "outcome": outcome, "summary": summary.strip(),
            "participants": participants,
        }
        event_id = append_event(
            conn, item_id=row["item_id"], event_type=event_type,
            actor_agent_id=actor, actor_session_id=session_id,
            payload=payload)
        if owner != actor:
            deliver(conn, recipient=owner, source_event_id=event_id,
                    item_id=row["item_id"], category="huddle_outcome",
                    payload=payload)
        return {**payload, "event_id": event_id}
    return mutate(conn, _close)


def huddle_show(conn, huddle_id):
    row, meta = _huddle(conn, huddle_id)
    posts = []
    for post in conn.execute(
            "SELECT id, round, agent, body, created_at FROM debate_posts "
            "WHERE debate_id=? ORDER BY id", (huddle_id,)):
        try:
            content = json.loads(post["body"])
        except (TypeError, ValueError):
            content = {"stance": None, "body": post["body"]}
        posts.append({"post_id": post["id"], "round": post["round"],
                      "agent": post["agent"],
                      "stance": content.get("stance"),
                      "body": content.get("body"),
                      "created_at": post["created_at"]})
    return {"huddle_id": row["id"], "item_id": row["item_id"],
            "topic": row["topic"], "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"], "closed_at": row["closed_at"],
            **meta, "posts": posts}


def refine_item(conn, *, claim_id, session_id, actor, fields):
    """Amend only agent-authored contract fields after peer changes."""
    fields = dict(fields or {})
    unknown = sorted(set(fields) - set(DEFINABLE_FIELDS))
    if unknown:
        raise IncompleteContract(
            "refine changes only agent-authored non-goal fields; unknown: "
            + ", ".join(unknown),
            reason_code="input_invalid",
            evidence={"constraint": "refinable_contract_fields_only"},
        )

    def _refine(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        acceptance = contract_acceptance(conn, item["id"])
        if acceptance["state"] != "changes":
            raise InvalidTransition(
                f"item {item['id']} contract acceptance is "
                f"{acceptance['state']}; refine requires peer-requested changes",
                reason_code=(
                    "contract_acceptance_required"
                    if acceptance["state"] == "pending"
                    else "transition_not_available"
                ),
                evidence={
                    "item_id": item["id"],
                    "current_state": acceptance["state"],
                    "required_state": "changes",
                },
            )
        authored = set()
        for event in conn.execute(
                "SELECT payload_json FROM events WHERE item_id=? AND "
                "event_type IN ('item_defined','item_refined')",
                (item["id"],)):
            authored.update(_event_payload(event).get("fields") or [])
        protected = sorted(set(fields) - authored)
        if protected:
            evidence = {
                "item_id": item["id"],
                "constraint": "agent_authored_field_required",
            }
            if len(protected) == 1:
                evidence["field"] = protected[0]
            raise FieldAlreadySet(
                "the agent lane cannot refine human-authored fields: "
                + ", ".join(protected),
                evidence=evidence,
            )
        current = {f: item[f] for f in CONTRACT_TEXT_FIELDS}
        for field in CONTRACT_LIST_FIELDS:
            current[field] = _parse_list(item[field])
        cleaned = {}
        for field, value in fields.items():
            if field in CONTRACT_LIST_FIELDS:
                if not isinstance(value, (list, tuple)) or not value or any(
                        not isinstance(v, str) or not v.strip() for v in value):
                    raise IncompleteContract(
                        f"{field} must be a non-empty string list",
                        reason_code="input_invalid",
                        evidence={
                            "item_id": item["id"],
                            "field": field,
                            "constraint": "non_empty_string_list",
                        },
                    )
                cleaned[field] = [v.strip() for v in value]
            else:
                if not isinstance(value, str) or not value.strip():
                    raise IncompleteContract(
                        f"{field} must be non-empty",
                        reason_code="input_invalid",
                        evidence={
                            "item_id": item["id"],
                            "field": field,
                            "constraint": "non_empty_contract_field",
                        },
                    )
                cleaned[field] = value.strip()
        if not cleaned:
            raise IncompleteContract(
                "refine changed nothing",
                reason_code="input_invalid",
                evidence={
                    "item_id": item["id"],
                    "constraint": "contract_refinement_required",
                },
            )
        merged = {**current, **cleaned}
        contract = _validated_contract(merged)
        version = item["contract_version"] + 1
        assignments = ", ".join(f"{field}=?" for field in cleaned)
        values = [json.dumps(contract[field], separators=(",", ":"))
                  if field in CONTRACT_LIST_FIELDS else contract[field]
                  for field in cleaned]
        conn.execute(
            f"UPDATE items SET {assignments}, contract_version=?, "
            "updated_at=? WHERE id=?",
            (*values, version, now(), item["id"]))
        event_id = append_event(
            conn, item_id=item["id"], event_type="item_refined",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload={"item_id": item["id"], "fields": sorted(cleaned),
                     "refined": cleaned, "contract_version": version})
        append_event(
            conn, item_id=item["id"],
            event_type="contract_acceptance_required",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload={"item_id": item["id"], "contract_version": version,
                     "contract_author": actor,
                     "trigger": "agent_refined_goal_contract",
                     "max_rounds": 2})
        return {"item_id": item["id"], "contract_version": version,
                "refined": sorted(cleaned), "event_id": event_id,
                "contract_acceptance": contract_acceptance(
                    conn, item["id"])}
    return mutate(conn, _refine)

def _parse_list(value):
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return value  # legacy free text stays readable as-is
    return parsed if isinstance(parsed, list) else value

def contract_incomplete(row):
    """Derived label (never stored): does this item lack any required
    contract field? Migrated legacy rows are readable but unclaimable."""
    for field in CONTRACT_TEXT_FIELDS:
        value = row[field]
        if value is None or not str(value).strip():
            return True
    for field in CONTRACT_LIST_FIELDS:
        parsed = _parse_list(row[field])
        if not isinstance(parsed, list) or not parsed:
            return True
    return False

def is_draft(row):
    """A goal-task: a non-empty goal (objective) with an otherwise-incomplete
    contract. Distinct from a fully-empty legacy row (no goal) — a draft is
    claimable so the working agent completes it via `define_item`, while a
    goalless-incomplete row stays unclaimable."""
    objective = row["objective"]
    has_goal = objective is not None and str(objective).strip() != ""
    return has_goal and contract_incomplete(row)

def _row_to_dict(row):
    return {key: (short_session_id(row[key])
                  if key in _SESSION_REF_COLUMNS else row[key])
            for key in row.keys()
            if key not in _REDACTED_COLUMNS}

def _item_row(conn, item_id):
    row = conn.execute(
        "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise NotFound(
            f"no item {item_id}",
            reason_code="target_not_found",
            evidence={"item_id": item_id},
        )
    return row

def _agent_ref(conn, agent_id):
    if agent_id is None:
        return None
    row = conn.execute(
        "SELECT provider FROM agents WHERE name=?", (agent_id,)).fetchone()
    return {"agent_id": agent_id,
            "provider": row["provider"] if row else None}

def _packet(conn, row):
    item_id = row["id"]
    claim_row = conn.execute(
        "SELECT claim_id, claimed_by_agent, owner_session_id, intent_note, "
        "lease_expires_at FROM claims WHERE lane_key=? AND status='active'",
        (f"implementation:item:{item_id}",)).fetchone()
    claim = session = None
    if claim_row:
        # The packet is what every peer reads. It carries the holder's
        # session as a display prefix plus liveness — never the full id
        # (self-asserted identity), never the harness argv or cwd.
        session_row = conn.execute(
            "SELECT provider, status FROM sessions "
            "WHERE session_id=?", (claim_row["owner_session_id"],)).fetchone()
        provider = session_row["provider"] if session_row else None
        claim = {
            "claim_id": claim_row["claim_id"],
            "agent": claim_row["claimed_by_agent"],
            "provider": provider,
            "session_id": short_session_id(claim_row["owner_session_id"]),
            "intent": claim_row["intent_note"],
            "lease_expires_at": claim_row["lease_expires_at"],
        }
        if session_row:
            session = {
                "provider": provider,
                "status": session_row["status"],
            }
    question_row = conn.execute(
        "SELECT question_id, exact_question, asked_by_agent, "
        "assigned_to_agent, status FROM questions WHERE item_id=? AND "
        "status='open' ORDER BY question_id DESC LIMIT 1",
        (item_id,)).fetchone()
    review_row = conn.execute(
        "SELECT id, status, reviewer, reviewer_agent_id, "
        "requested_by_agent, receipt_id, contract_version FROM "
        "reviews WHERE item_id=? AND status IN ('requested','changes') "
        "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    review_claim = None
    if review_row:
        rc = conn.execute(
            "SELECT claim_id, claimed_by_agent, owner_session_id, "
            "intent_note, lease_expires_at FROM claims WHERE lane_key=? "
            "AND status='active'", (f"review:{review_row['id']}",)).fetchone()
        if rc:
            rc_session = conn.execute(
                "SELECT provider FROM sessions WHERE session_id=?",
                (rc["owner_session_id"],)).fetchone()
            review_claim = {
                "claim_id": rc["claim_id"],
                "agent": rc["claimed_by_agent"],
                "provider": rc_session["provider"] if rc_session else None,
                "session_id": short_session_id(rc["owner_session_id"]),
                "intent": rc["intent_note"],
                "lease_expires_at": rc["lease_expires_at"],
            }
    # Handoff slot: pending else latest-accepted — declined/withdrawn rows never
    # render a slot; the execution token is never selected outward.
    handoff_row = conn.execute(
        "SELECT * FROM handoffs WHERE item_id=? AND status='pending' "
        "ORDER BY handoff_id DESC LIMIT 1", (item_id,)).fetchone()
    if handoff_row is None:
        handoff_row = conn.execute(
            "SELECT * FROM handoffs WHERE item_id=? AND status='accepted' "
            "ORDER BY handoff_id DESC LIMIT 1", (item_id,)).fetchone()
    receipt_row = _current_receipt(conn, item_id)
    # Live rows join the slot beside migrated legacy rows —
    # legacy keeps its preserved debate_id with null claim data, live rows
    # carry their claim_id; nothing fabricates the other lineage.
    decisions = [
        {"decision_id": r["id"], "text": r["text"], "rationale": r["rationale"],
         "decided_by": r["decided_by_agent"] or r["decided_by"],
         "debate_id": r["debate_id"], "claim_id": r["claim_id"],
         "legacy": bool(r["legacy"])}
        for r in conn.execute(
            "SELECT id, text, rationale, decided_by, decided_by_agent, "
            "debate_id, claim_id, legacy FROM "
            "decisions WHERE item_id=? ORDER BY id", (item_id,))]
    events = [
        {"event_id": r["event_id"], "event_type": r["event_type"],
         "actor_agent_id": r["actor_agent_id"], "created_at": r["created_at"]}
        for r in reversed(conn.execute(
            "SELECT event_id, event_type, actor_agent_id, created_at FROM "
            "events WHERE item_id=? ORDER BY event_id DESC LIMIT 10",
            (item_id,)).fetchall())]
    return {
        "item_id": item_id,
        "contract_version": row["contract_version"],
        "title": row["title"],
        "objective": row["objective"],
        "scope": row["scope"],
        "done_when": row["done_when"],
        "output_contract": row["output_contract"],
        "context": row["context"],
        "allowed_actions": _parse_list(row["allowed_actions"]),
        "stop_conditions": _parse_list(row["stop_conditions"]),
        "review_quorum": review_quorum(conn, item_id),
        "contract_acceptance": contract_acceptance(conn, item_id),
        "status": row["status"],
        "owner": _agent_ref(conn, row["owner_agent_id"]),
        "next_actor": _agent_ref(conn, row["next_actor_agent_id"]),
        "claim": claim,
        "session": session,
        "question": ({
            "question_id": question_row["question_id"],
            "exact_question": question_row["exact_question"],
            "asked_by": question_row["asked_by_agent"],
            "assigned_to": question_row["assigned_to_agent"],
            "status": question_row["status"],
        } if question_row else None),
        # The review slot's dual facts: `reviewer` is the
        # immutable request-time designation; `reviewer_agent_id` is the
        # informational latest claimant; the review-lane claim beside the
        # implementation claim is the authority for the current holder.
        "review": ({
            "review_id": review_row["id"],
            "status": review_row["status"],
            "reviewer": review_row["reviewer"],
            "reviewer_agent_id": review_row["reviewer_agent_id"],
            "requested_by": review_row["requested_by_agent"],
            "receipt_id": review_row["receipt_id"],
            "contract_version": review_row["contract_version"],
            "claim": review_claim,
        } if review_row else None),
        "handoff": ({
            "handoff_id": handoff_row["handoff_id"],
            "from_agent": handoff_row["from_agent"],
            "to_agent": handoff_row["to_agent"],
            "status": handoff_row["status"],
            "reason": handoff_row["reason"],
            "summary": handoff_row["summary"],
            "completed_work": handoff_row["completed_work"],
            "remaining_work": handoff_row["remaining_work"],
            "risks": handoff_row["risks"],
            "suggested_next_action": handoff_row["suggested_next_action"],
            "proof_references": json.loads(handoff_row["proof_references"]),
            "created_at": handoff_row["created_at"],
            "resolved_at": handoff_row["resolved_at"],
        } if handoff_row else None),
        "receipt": ({
            "receipt_id": receipt_row["receipt_id"],
            "summary": receipt_row["summary"],
            "sha256_short": receipt_row["sha256"][:12],
            "reference_count": len(
                json.loads(receipt_row["proof_references_json"])),
            "created_at": receipt_row["created_at"],
            "marker": receipt_marker(receipt_row),
        } if receipt_row else None),
        "decisions": decisions,
        "events": events,
    }

def item_show(conn, item_id, *, packet=False, history=False):
    """The first-class task read path: base contract view, compact packet,
    or complete history. Every serialization redacts internal generations."""
    row = _item_row(conn, item_id)
    if packet:
        return _packet(conn, row)
    base = _row_to_dict(row)
    base["item_id"] = row["id"]
    base["allowed_actions"] = _parse_list(row["allowed_actions"])
    base["stop_conditions"] = _parse_list(row["stop_conditions"])
    base["contract_incomplete"] = contract_incomplete(row)
    base["draft"] = is_draft(row)
    base["review_quorum"] = review_quorum(conn, item_id)
    base["contract_acceptance"] = contract_acceptance(conn, item_id)
    if not history:
        return base
    def dump(query, params=(item_id,)):
        return [_row_to_dict(r) for r in conn.execute(query, params)]
    return {
        "item": base,
        "events": dump("SELECT * FROM events WHERE item_id=? ORDER BY event_id"),
        "messages": dump("SELECT * FROM messages WHERE item_id=? ORDER BY id"),
        "claims": dump("SELECT * FROM claims WHERE item_id=? ORDER BY claim_id"),
        "questions": dump(
            "SELECT * FROM questions WHERE item_id=? ORDER BY question_id"),
        "reviews": dump("SELECT * FROM reviews WHERE item_id=? ORDER BY id"),
        "handoffs": dump(
            "SELECT * FROM handoffs WHERE item_id=? ORDER BY handoff_id"),
        "decisions": dump("SELECT * FROM decisions WHERE item_id=? ORDER BY id"),
        # Every receipt row carries the opportunistic
        # display-only evidence marker (missing|changed|None); tokens are
        # already stripped by _row_to_dict.
        "receipts": [
            dict(_row_to_dict(r), evidence_marker=receipt_marker(r))
            for r in conn.execute(
                "SELECT * FROM receipts WHERE item_id=? ORDER BY receipt_id",
                (item_id,))],
    }

# --- operator read surface (presentation queries; no protocol authority) ----

def task_rows(conn):
    """Operator task list: durable owner/next + live implementation claim.
    SELECT * so contract_incomplete() can read the contract fields; the
    row dict below carries only the operator-view keys (no token columns
    exist on items)."""
    running = {r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE status='running'")}
    rows = []
    for r in conn.execute("SELECT * FROM items ORDER BY id"):
        claim = conn.execute(
            "SELECT claim_id, claimed_by_agent, status, intent_note, "
            "owner_session_id "
            "FROM claims WHERE item_id=? AND claim_kind='implementation' "
            "AND status='active' ORDER BY claim_id DESC LIMIT 1",
            (r["id"],)).fetchone()
        labels = []
        if is_draft(r):
            labels.append("draft")
        elif contract_incomplete(r):
            labels.append("contract_incomplete")
        acceptance = contract_acceptance(conn, r["id"])
        if acceptance["required"] and acceptance["state"] != "accepted":
            labels.append(f"contract_{acceptance['state']}")
        rows.append({
            "item_id": r["id"],
            "status": r["status"],
            "title": r["title"],
            "owner": r["owner_agent_id"] or item_owner(conn, r["id"]),
            "next_actor": r["next_actor_agent_id"],
            "labels": labels,
            "claim": ({
                "claim_id": claim["claim_id"],
                "agent": claim["claimed_by_agent"],
                "status": claim["status"],
                "intent": claim["intent_note"],
                "session_live": claim["owner_session_id"] in running,
            } if claim else None),
        })
    return rows

def message_rows(conn, limit=50):
    return [dict(r) for r in conn.execute(
        "SELECT id, from_agent, to_agent, body, created_at, item_id "
        "FROM messages ORDER BY id DESC LIMIT ?", (limit,))][::-1]

def session_rows(conn):
    """Operator sessions strip (display only): ids are the eight-character
    prefix the dashboard renders, never the full self-asserted id."""
    rows = []
    for r in conn.execute(
            "SELECT session_id, agent_id, provider, status, started_at, "
            "last_seen_at FROM sessions WHERE status='running' "
            "ORDER BY started_at"):
        row = dict(r)
        row["session_id"] = short_session_id(row["session_id"])
        rows.append(row)
    return rows

def board_probe(conn):
    """Cheap change fingerprint for repaint-on-change monitors: broadcast
    messages write no event row, so their max id is a separate component."""
    return (
        conn.execute(
            "SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0],
        conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0],
        conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE status='running'"
        ).fetchone()[0],
    )


def actor_event_probe(conn, *, session_id, item_id=None):
    """Return the latest event id and count authored by one session."""
    clause = "" if item_id is None else " AND item_id=?"
    params = (
        (session_id,)
        if item_id is None
        else (session_id, item_id)
    )
    row = conn.execute(
        "SELECT COALESCE(MAX(event_id),0), COUNT(*) FROM events "
        "WHERE actor_session_id=?" + clause,
        params,
    ).fetchone()
    return int(row[0]), int(row[1])


def item_board_probe(conn, item_id):
    """Change fingerprint scoped to one item for targeted autonomous runs."""
    item = _item_row(conn, item_id)
    return (
        conn.execute(
            "SELECT COALESCE(MAX(event_id),0) FROM events WHERE item_id=?",
            (item_id,)).fetchone()[0],
        conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM messages WHERE item_id=?",
            (item_id,)).fetchone()[0],
        item["status"],
        item["updated_at"],
    )

def discover_board(start_cwd):
    """Find the nearest board without crossing the current Git boundary.

    Three shapes, nearest ancestor wins, newest first.
    `<workspace>/.coop/board.db` is the layout `coop init --workspace` creates
    and the only one that works on a repo that is not Co-op;
    `<repo>/board.db` and `<repo>/coop/board.db` are two older board layouts
    that remain discoverable.
    Both older shapes stay supported — no board needs migrating. A `.git`
    directory or linked-worktree `.git` file is the last ancestor searched.
    Outside Git, only `start_cwd` is searched."""
    path = pathlib.Path(start_cwd).resolve()
    ancestors = (path, *path.parents)
    boundary = next(
        (
            index
            for index, candidate in enumerate(ancestors)
            if (
                (candidate / ".git").is_dir()
                or (candidate / ".git").is_file()
            )
        ),
        None,
    )
    candidates = ancestors[:boundary + 1] if boundary is not None else (path,)
    for candidate in candidates:
        for board in (candidate / ".coop" / "board.db",
                      candidate / "board.db",
                      candidate / "coop" / "board.db"):
            if board.is_file():
                return str(board)
    return None

def _boards_registry_path():
    override = os.environ.get("COOP_BOARDS_REGISTRY")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".coop" / "boards.json"


def board_workspace(db_path) -> pathlib.Path:
    """The folder a board belongs to: the parent of `.coop`/`coop`, else the
    board file's own folder (a bare `board.db` next to the code)."""
    parent = pathlib.Path(db_path).resolve(strict=False).parent
    if parent.name in (".coop", "coop") and parent.parent != parent:
        return parent.parent
    return parent


def _temp_roots() -> list[pathlib.Path]:
    roots = [pathlib.Path(tempfile.gettempdir())]
    for raw in ("/tmp", "/var/tmp", "/private/tmp"):
        candidate = pathlib.Path(raw)
        if candidate.exists():
            roots.append(candidate)
    out = []
    for root in roots:
        try:
            out.append(root.resolve(strict=False))
        except Exception:
            pass
    return out


def is_temp_path(path) -> bool:
    """True when `path` sits under the OS temp directory. Scratch boards
    (tests, benchmarks, exports, throwaway workspaces) live there; a user's
    real repositories do not, so the switcher registry never lists them."""
    try:
        resolved = pathlib.Path(path).resolve(strict=False)
    except Exception:
        return False
    for root in _temp_roots():
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _home_relative(path: pathlib.Path) -> str:
    try:
        home = pathlib.Path.home().resolve(strict=False)
        rel = path.relative_to(home)
        return "~" if str(rel) == "." else "~" + os.sep + str(rel)
    except Exception:
        return str(path)


def git_worktree_main(workspace) -> tuple[pathlib.Path, str] | None:
    """(main repository, worktree name) when `workspace` is a linked git
    worktree, else None. A linked worktree carries a `.git` *file* whose
    `gitdir:` line points inside `<main>/.git/worktrees/<name>`."""
    dotgit = pathlib.Path(workspace) / ".git"
    try:
        if not dotgit.is_file():
            return None
        text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = pathlib.Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = (pathlib.Path(workspace) / gitdir).resolve(strict=False)
    parts = gitdir.parts
    # .../<main>/.git/worktrees/<name>
    if len(parts) >= 3 and parts[-2] == "worktrees" and parts[-3] == ".git":
        return pathlib.Path(*parts[:-3]).resolve(strict=False), parts[-1]
    return None


def display_board_path(db_path) -> str:
    """Home-relative workspace label for the switcher: `~/projects/app`;
    a linked git worktree reads `<repo> · worktree:<name>`."""
    workspace = board_workspace(db_path)
    linked = git_worktree_main(workspace)
    if linked is not None:
        main_repo, name = linked
        return f"{_home_relative(main_repo)} · worktree:{name}"
    return _home_relative(workspace)


def _read_registry(registry: pathlib.Path) -> dict:
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _live_entries(data: dict) -> dict:
    """Entries worth listing. A vanished folder has nothing to open and
    nothing to create, so it leaves the registry; in the operator's home
    registry a temp-directory board leaves too (older entries recorded
    before the temp rule, or scratch that outlived its run)."""
    sandbox = bool(os.environ.get("COOP_BOARDS_REGISTRY"))
    return {
        path: meta for path, meta in data.items()
        if board_workspace(path).is_dir()
        and (sandbox or not is_temp_path(path))
    }


def record_board(db_path):
    """Best-effort boards registry ({path: {last_opened}}); silent on any
    failure — the registry is a convenience, never a dependency.

    The operator's home registry never receives a board under the OS temp
    directory (tests, benchmarks, exports, throwaway workspaces). A registry
    redirected with COOP_BOARDS_REGISTRY is a deliberate sandbox and records
    whatever it is told. Entries whose workspace folder no longer exists are
    dropped while the file is open."""
    try:
        if is_temp_path(db_path) and not os.environ.get("COOP_BOARDS_REGISTRY"):
            return
        registry = _boards_registry_path()
        registry.parent.mkdir(parents=True, exist_ok=True)
        data = _live_entries(_read_registry(registry))
        data[str(pathlib.Path(db_path).resolve())] = {"last_opened": now()}
        registry.write_text(json.dumps(data, sort_keys=True),
                            encoding="utf-8")
    except Exception:
        pass

def known_boards():
    """Registry paths whose workspace folder still exists, freshest-opened
    first; [] on any trouble. A folder without a board is listed: opening it
    from the switcher creates the board there."""
    try:
        data = _live_entries(_read_registry(_boards_registry_path()))
        return sorted(
            data,
            key=lambda k: str((data[k] or {}).get("last_opened", "")),
            reverse=True)
    except Exception:
        return []

_QUEUE_KIND_RANK = {"task": 0, "review": 1, "handoff": 2}

def _lane_state(conn, lane, now_ts):
    """Classify the lane's newest claim for discovery (read-time mirror of
    the claim guard): (claimable_now, unsafe, row). Never-claimed and
    deliberately-ended lanes are claimable; an expired-active row counts
    as stale; a stale row is claimable only under a terminal session and
    unsafe while its session still reports running."""
    row = conn.execute(
        "SELECT * FROM claims WHERE lane_key=? ORDER BY claim_id DESC "
        "LIMIT 1", (lane,)).fetchone()
    if row is None:
        return True, False, None
    if row["status"] == "active" and row["lease_expires_at"] > now_ts:
        if _session_terminal(conn, row["owner_session_id"]):
            return True, False, row
        return False, False, row
    if row["status"] in ("active", "stale"):
        if _session_terminal(conn, row["owner_session_id"]):
            return True, False, row
        return False, True, row
    return True, False, row  # released / closed / completed: reason only

def _queue_reviews(conn, for_agent, now_ts, item_id=None):
    """Current claimable reviews for the agent: live by
    derivation, owner excluded, designation honored, and the review lane
    claimable right now — unsafe lanes never surface.  An unnamed review
    also excludes providers whose approval already qualifies on the current
    receipt; an explicit reviewer designation remains authoritative."""
    rows = []
    approval_providers = {}
    item_clause = "" if item_id is None else " AND r.item_id=?"
    params = () if item_id is None else (item_id,)
    for r in conn.execute(
            "SELECT r.*, i.title AS item_title, i.owner_agent_id AS owner, "
            "i.contract_version AS item_version FROM reviews r JOIN items i "
            "ON i.id=r.item_id WHERE r.status='requested' AND "
            "r.resolved_at IS NULL" + item_clause + " ORDER BY r.id",
            params):
        if r["owner"] == for_agent:
            continue
        if r["reviewer"] is not None and r["reviewer"] != for_agent:
            continue
        if r["reviewer"] is None:
            if r["item_id"] not in approval_providers:
                approval_providers[r["item_id"]] = \
                    _current_approval_providers(conn, r["item_id"])
            providers = approval_providers[r["item_id"]]
            if _agent_provider_bucket(conn, for_agent) in providers:
                continue
        current = _current_receipt(conn, r["item_id"])
        if (current is None or r["receipt_id"] != current["receipt_id"]
                or r["contract_version"] != r["item_version"]):
            continue  # dead by derivation
        claimable, _unsafe, _row = _lane_state(
            conn, f"review:{r['id']}", now_ts)
        if not claimable:
            continue
        rows.append((r["created_at"], r["id"], {
            "kind": "review", "review_id": r["id"],
            "item_id": r["item_id"], "title": r["item_title"],
            "designated_reviewer": r["reviewer"],
            "requested_at": r["created_at"],
        }))
    return rows

def queue(conn, for_agent=None, item_id=None):
    """Discriminated discovery rows in the pinned total order
    (created_at, entity_id, kind): claimable todo tasks always; plus, for
    `--for <agent>`, claimable-now reviews and pending handoffs addressed
    to them. `kind` breaks a tie only when timestamp AND entity id both
    collide (task < review < handoff, stable serializer only) — never a
    category priority. Incomplete legacy contracts stay excluded."""
    if for_agent is not None and not valid_agent_name(for_agent):
        raise InvalidAgentName(f"invalid agent name: {for_agent!r}")
    keyed = []
    item_clause = "" if item_id is None else " AND id=?"
    item_params = () if item_id is None else (item_id,)
    for row in conn.execute(
            "SELECT * FROM items WHERE status='todo'" + item_clause +
            " ORDER BY created_at, id", item_params):
        if contract_incomplete(row) and not is_draft(row):
            continue  # goalless-incomplete legacy rows stay hidden; drafts show
        if for_agent is not None:
            addressed = for_agent in (
                row["owner_agent_id"], row["next_actor_agent_id"])
            unassigned = (row["owner_agent_id"] is None
                          and row["next_actor_agent_id"] is None)
            if not (addressed or unassigned):
                continue
        keyed.append((row["created_at"], row["id"], {
            "kind": "task",
            "item_id": row["id"], "title": row["title"],
            "objective": row["objective"], "created_at": row["created_at"],
            "owner": row["owner_agent_id"],
            "next_actor": row["next_actor_agent_id"],
        }))
    if for_agent is not None and for_agent != "human":
        now_ts = now()
        keyed.extend(_queue_reviews(
            conn, for_agent, now_ts, item_id=item_id))
        handoff_clause = "" if item_id is None else " AND h.item_id=?"
        handoff_params = ((for_agent,) if item_id is None
                          else (for_agent, item_id))
        for h in conn.execute(
                "SELECT h.handoff_id, h.item_id, h.from_agent, h.created_at, "
                "i.title FROM handoffs h JOIN items i ON i.id=h.item_id "
                "WHERE h.status='pending' AND h.to_agent=? "
                + handoff_clause + " ORDER BY h.handoff_id",
                handoff_params):
            keyed.append((h["created_at"], h["handoff_id"], {
                "kind": "handoff", "handoff_id": h["handoff_id"],
                "item_id": h["item_id"], "title": h["title"],
                "from_agent": h["from_agent"],
                "created_at": h["created_at"],
            }))
    keyed.sort(key=lambda k: (k[0], k[1], _QUEUE_KIND_RANK[k[2]["kind"]]))
    return [entry for _, _, entry in keyed]

# --- claims, leases, fencing, controlled reclaim ----------------------------
# A claim is exclusive protocol authority for one canonical lane. Fencing
# tokens are per-lane generations stored for audit and never serialized;
# agents carry exactly one handle, the claim id.

def _implementation_lane(item_id):
    return f"implementation:item:{item_id}"

def _flip_stale(conn, claim_row):
    """Flip one lapsed active claim to stale — exactly once. Cardinality:
    one event and one addressed stale warning per row actually flipped."""
    cur = conn.execute(
        "UPDATE claims SET status='stale' WHERE claim_id=? AND status='active'",
        (claim_row["claim_id"],))
    if cur.rowcount != 1:
        return False
    event_id = append_event(
        conn, item_id=claim_row["item_id"], event_type="claim_stale",
        actor_agent_id=claim_row["claimed_by_agent"],
        actor_session_id=claim_row["owner_session_id"],
        claim_id=claim_row["claim_id"],
        fencing_token=claim_row["fencing_token"],
        payload={"claim_id": claim_row["claim_id"],
                 "lane": claim_row["lane_key"]})
    deliver(conn, recipient=claim_row["claimed_by_agent"],
            source_event_id=event_id, item_id=claim_row["item_id"],
            category="stale_warning",
            payload={"claim_id": claim_row["claim_id"],
                     "item_id": claim_row["item_id"],
                     "lane": claim_row["lane_key"]})
    return True

def _session_is_running(conn, session_id):
    row = conn.execute(
        "SELECT status FROM sessions WHERE session_id=?",
        (session_id,)).fetchone()
    return row is not None and row["status"] == "running"

def _session_actor(conn, session_id):
    """Derive the acting agent from its live session — for operations whose
    pinned signatures carry no explicit actor."""
    if not session_id:
        raise SessionMismatch(
            "this operation requires a live supervised session",
            reason_code="session_unavailable",
            evidence={"required_status": "running"},
        )
    row = conn.execute(
        "SELECT agent_id, status FROM sessions WHERE session_id=?",
        (session_id,)).fetchone()
    if row is None:
        raise SessionMismatch(
            f"unknown session {session_id!r}",
            reason_code="session_unavailable",
            evidence={},
        )
    if row["status"] != "running":
        raise SessionMismatch(
            f"session {session_id!r} is {row['status']}, not running",
            reason_code="session_unavailable",
            evidence={
                "session_status": row["status"],
                "required_status": "running",
            },
        )
    return row["agent_id"]

def _lane_history_guard(conn, *, lane, reclaim_reason):
    """One lane-safety classifier for every claim acquisition:
    classifies the newest claim row for the lane regardless of status and
    answers exactly one question — is the lane safe to re-enter. Subject
    eligibility stays with each caller. Returns the newest row, or None
    when the lane has no history. Refusals raise inside the caller's
    transaction, so a rejected attempt is side-effect-free (an in-guard
    stale flip rolls back with it)."""
    newest = conn.execute(
        "SELECT * FROM claims WHERE lane_key=? ORDER BY claim_id DESC "
        "LIMIT 1", (lane,)).fetchone()
    if newest is None:
        return None
    status = newest["status"]
    if status == "active":
        if (newest["lease_expires_at"] > now()
                and _session_is_running(conn, newest["owner_session_id"])):
            raise ClaimCollision(
                f"lane {lane} is claimed by "
                f"{newest['claimed_by_agent']!r} (claim "
                f"{newest['claim_id']}, lease live); claim other work",
                reason_code="claim_conflict",
                evidence={
                    "item_id": newest["item_id"],
                    "claim_id": newest["claim_id"],
                    "lane": lane,
                    "owner_agent_id": newest["claimed_by_agent"],
                    "current_status": status,
                },
            )
        # An expired claim, or any claim whose owning session has confirmed
        # terminal exit, is safe to classify stale immediately. A refusal
        # below still rolls the flip back with the whole attempt.
        _flip_stale(conn, newest)
        status = "stale"
    if not reclaim_reason:
        raise InvalidTransition(
            f"lane {lane} has prior claim history (latest: {status}); "
            "pass --reclaim --reason <text>",
            reason_code="transition_not_available",
            evidence={
                "item_id": newest["item_id"],
                "claim_id": newest["claim_id"],
                "lane": lane,
                "current_status": status,
                "constraint": "reclaim_reason_required",
            },
        )
    if status == "stale" and _session_is_running(
            conn, newest["owner_session_id"]):
        # The repaired branch: stale demands predecessor-
        # session evidence — terminal exit or a prior operator release
        # (which lands the lane in 'released') — a reason alone never
        # seizes a still-running session's work.
        raise UnsafeReclaim(
            f"claim {newest['claim_id']} is stale but session "
            f"{newest['owner_session_id']!r} still reports running; wait "
            "for abandon horizon then `coop recover wedge {newest['claim_id']}` "
            "(agent recovery) or confirmed exit",
            reason_code="unsafe_reclaim",
            evidence={
                "item_id": newest["item_id"],
                "claim_id": newest["claim_id"],
                "session_status": "running",
                "constraint": "predecessor_session_exit_required",
            },
        )
    # released / closed / completed: deliberate ends — reason only.
    return newest

def _insert_claim(conn, *, item_id, kind, subject_id, lane, actor,
                  session_id, intent, lease):
    """Allocate the per-lane generation and insert one active claim; the
    insert stamps the initial start checkpoint."""
    stamp = now()
    token = conn.execute(
        "SELECT COALESCE(MAX(fencing_token),0)+1 AS t FROM claims "
        "WHERE lane_key=?", (lane,)).fetchone()["t"]
    expires = _ts(lease)
    cur = conn.execute(
        "INSERT INTO claims(item_id,claim_kind,subject_id,lane_key,"
        "claimed_by_agent,owner_session_id,status,intent_note,"
        "fencing_token,claimed_at,last_renewed_at,lease_expires_at,"
        "last_checkpoint_at) VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?)",
        (item_id, kind, subject_id, lane, actor, session_id,
         intent, token, stamp, stamp, expires, stamp))
    return cur.lastrowid, token, expires

def claim_item(conn, *, item_id, actor, session_id, intent,
               reclaim_reason=None, lease_seconds=None):
    """Acquire the implementation claim for one item under the full guard
    ladder. A rejected attempt is side-effect-free."""
    if not isinstance(intent, str) or not intent.strip():
        raise InvalidTransition(
            "a claim requires a non-empty --intent",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_claim_intent"},
        )
    lease = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds
    reason = reclaim_reason.strip() if isinstance(reclaim_reason, str) else None

    def _claim(conn):
        session = _require_live_session(conn, session_id, actor)
        if session is None:
            raise HumanLaneViolation(
                "the human interface cannot claim agent work",
                reason_code="human_lane_forbidden",
                evidence={"constraint": "agent_session_required"},
            )
        row = _item_row(conn, item_id)
        if row["status"] == "done":
            raise InvalidTransition(
                f"item {item_id} is done; follow-up work is a new task",
                reason_code="transition_not_available",
                evidence={
                    "item_id": item_id,
                    "current_state": row["status"],
                    "allowed_states": ["todo", "working", "needs_input",
                                       "blocked", "review"],
                },
            )
        if contract_incomplete(row) and not is_draft(row):
            raise IncompleteContract(
                f"item {item_id} has no goal and an incomplete contract; a "
                "human must give it a goal before it can be claimed",
                reason_code="contract_incomplete",
                evidence={
                    "item_id": item_id,
                    "constraint": "executable_contract_required",
                },
            )
        open_q = conn.execute(
            "SELECT question_id, assigned_to_agent FROM questions "
            "WHERE item_id=? AND status='open' "
            "ORDER BY question_id DESC LIMIT 1", (item_id,)).fetchone()
        if open_q is not None:
            raise InvalidTransition(
                f"item {item_id} is waiting on open question "
                f"{open_q['question_id']} (addressed to "
                f"{open_q['assigned_to_agent']!r}); work resumes only "
                "after an answer",
                reason_code="blocking_work_open",
                evidence={
                    "item_id": item_id,
                    "target_agent_id": open_q["assigned_to_agent"],
                    "blocking_object_type": "question",
                    "blocking_ids": [open_q["question_id"]],
                },
            )
        # Handoff freeze: a pending handoff freezes the lane —
        # every claim branch, reclaim included, waits for accept/decline.
        pending_h = conn.execute(
            "SELECT handoff_id, to_agent FROM handoffs WHERE item_id=? "
            "AND status='pending' ORDER BY handoff_id DESC LIMIT 1",
            (item_id,)).fetchone()
        if pending_h is not None:
            raise InvalidTransition(
                f"item {item_id} has pending handoff "
                f"{pending_h['handoff_id']} addressed to "
                f"{pending_h['to_agent']!r}; the lane is frozen until it "
                "is accepted or declined",
                reason_code="blocking_work_open",
                evidence={
                    "item_id": item_id,
                    "target_agent_id": pending_h["to_agent"],
                    "blocking_object_type": "handoff",
                    "blocking_ids": [pending_h["handoff_id"]],
                },
            )
        if (row["resume_grace_expires_at"] is not None
                and now() < row["resume_grace_expires_at"]
                and actor != row["preferred_resume_owner_agent_id"]):
            raise InvalidTransition(
                f"the resume grace window for "
                f"{row['preferred_resume_owner_agent_id']!r} runs until "
                f"{row['resume_grace_expires_at']}; a controlled takeover "
                "is allowed only after it expires",
                reason_code="awaiting_peer",
                evidence={
                    "item_id": item_id,
                    "target_agent_id":
                        row["preferred_resume_owner_agent_id"],
                    "current_state": row["status"],
                    "constraint": "resume_grace_owner_priority",
                },
            )
        lane = _implementation_lane(item_id)
        stamp = now()
        history = _lane_history_guard(conn, lane=lane, reclaim_reason=reason)
        if history is None and row["status"] != "todo":
            if reason is None or not reason:
                raise InvalidTransition(
                    f"item {item_id} is {row['status']}; claiming it "
                    "requires --reclaim --reason <text>",
                    reason_code="transition_not_available",
                    evidence={
                        "item_id": item_id,
                        "current_state": row["status"],
                        "constraint": "reclaim_reason_required",
                    },
                )
        claim_id, token, expires = _insert_claim(
            conn, item_id=item_id, kind="implementation", subject_id=None,
            lane=lane, actor=actor, session_id=session_id,
            intent=intent.strip(), lease=lease)
        previous_owner = row["owner_agent_id"]
        # Reclaim during review re-arms the lane, it
        # does not rewind the state machine — review is preserved.
        new_status = "review" if row["status"] == "review" else "working"
        conn.execute(
            "UPDATE items SET status=?, owner_agent_id=?, "
            "next_actor_agent_id=?, preferred_resume_owner_agent_id=NULL, "
            "resume_grace_started_at=NULL, resume_grace_expires_at=NULL, "
            "updated_at=? WHERE id=?",
            (new_status, actor, actor, stamp, item_id))
        payload = {"item_id": item_id, "claim_id": claim_id, "lane": lane}
        if reason:
            payload["reclaim_reason"] = reason
        event_id = append_event(
            conn, item_id=item_id, event_type="claim_acquired",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=token, payload=payload)
        if previous_owner and previous_owner != actor:
            deliver(conn, recipient=previous_owner,
                    source_event_id=event_id, item_id=item_id,
                    category="ownership_transfer",
                    payload={"item_id": item_id, "claim_id": claim_id,
                             "new_owner": actor})
        return {"claim_id": claim_id, "item_id": item_id, "lane": lane,
                "lease_expires_at": expires}

    return mutate(conn, _claim)

def validate_claim(conn, *, claim_id, session_id, actor, expected_kind=None,
                   subject_id=None):
    """The single in-transaction chokepoint every claim-bound mutation calls:
    the claim belongs to this live session and agent, is active and
    unexpired, is the newest row for its lane, and matches the expected kind
    and subject. Returns the row (its token stays internal)."""
    _require_live_session(conn, session_id, actor)
    row = conn.execute(
        "SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    if row is None:
        raise NotFound(
            f"no claim {claim_id}",
            reason_code="target_not_found",
            evidence={"claim_id": claim_id},
        )
    if row["owner_session_id"] != session_id or \
            row["claimed_by_agent"] != actor:
        reason_code = (
            "session_unavailable" if session_id is None else "actor_mismatch")
        evidence = {
            "item_id": row["item_id"],
            "claim_id": claim_id,
            "actor_agent_id": actor,
            "required_agent_id": row["claimed_by_agent"],
        }
        if session_id is None:
            evidence["required_status"] = "running"
        raise SessionMismatch(
            f"claim {claim_id} belongs to {row['claimed_by_agent']!r} "
            f"(session {row['owner_session_id']!r}), not {actor!r}",
            reason_code=reason_code,
            evidence=evidence,
        )
    if row["status"] != "active":
        raise StaleClaim(
            f"claim {claim_id} is {row['status']}; acquire a fresh claim",
            reason_code="claim_not_current",
            evidence={
                "item_id": row["item_id"],
                "claim_id": claim_id,
                "current_status": row["status"],
                "lane": row["lane_key"],
            },
        )
    if row["lease_expires_at"] <= now():
        raise StaleClaim(
            f"claim {claim_id} lease expired; it is stale pending sweep",
            reason_code="claim_expired",
            evidence={
                "item_id": row["item_id"],
                "claim_id": claim_id,
                "current_status": row["status"],
                "lease_expired": True,
                "lease_expires_at": row["lease_expires_at"],
            },
        )
    newest = conn.execute(
        "SELECT MAX(claim_id) AS m FROM claims WHERE lane_key=?",
        (row["lane_key"],)).fetchone()["m"]
    if newest != claim_id:
        raise StaleClaim(
            f"claim {claim_id} is superseded on lane {row['lane_key']}",
            reason_code="claim_not_current",
            evidence={
                "item_id": row["item_id"],
                "claim_id": claim_id,
                "current_status": "superseded",
                "lane": row["lane_key"],
            },
        )
    if expected_kind is not None and row["claim_kind"] != expected_kind:
        raise InvalidTransition(
            f"claim {claim_id} is a {row['claim_kind']} claim, "
            f"not {expected_kind}",
            reason_code="claim_lane_mismatch",
            evidence={
                "item_id": row["item_id"],
                "claim_id": claim_id,
                "claim_kind": row["claim_kind"],
                "required_claim_kind": expected_kind,
            },
        )
    if subject_id is not None and row["subject_id"] != subject_id:
        raise InvalidTransition(
            f"claim {claim_id} targets subject {row['subject_id']!r}, "
            f"not {subject_id!r}",
            reason_code="claim_lane_mismatch",
            evidence={
                "item_id": row["item_id"],
                "claim_id": claim_id,
                "claim_kind": row["claim_kind"],
                "required_claim_kind": (
                    expected_kind
                    if expected_kind is not None
                    else row["claim_kind"]
                ),
                "constraint": "claim_subject_matches_required_subject",
            },
        )
    return row

def release_claim(conn, *, claim_id, actor, session_id, reason):
    """Voluntary release: relinquish authority, preserve durable ownership
    and item state (owned but unclaimed). Later writes through it refuse."""
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidTransition(
            "release requires a non-empty --reason",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_release_reason"},
        )

    def _release(conn):
        row = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor)
        stamp = now()
        conn.execute(
            "UPDATE claims SET status='released', closed_at=?, "
            "close_reason=? WHERE claim_id=?",
            (stamp, reason.strip(), claim_id))
        append_event(
            conn, item_id=row["item_id"], event_type="claim_released",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=row["fencing_token"],
            payload={"claim_id": claim_id, "reason": reason.strip()})

    return mutate(conn, _release)

def sweep_expired(conn):
    """Flip every lapsed active claim to stale, idempotently. Composes
    inside the supervisor's maintenance transaction; a concurrent sweep of
    the same rows is a no-op (zero events). Returns flipped claim ids."""
    def _sweep(conn):
        rows = conn.execute(
            "SELECT * FROM claims WHERE status='active' AND "
            "lease_expires_at <= ?", (now(),)).fetchall()
        return [r["claim_id"] for r in rows if _flip_stale(conn, r)]
    return _in_mutate(conn, _sweep)

def renew_claims(conn, *, session_id, lease_seconds=None):
    """Extend only a running session's active, still-unexpired claims. An
    already-expired claim and a claim owned by a terminal session are never
    revived or extended. Deliberate cardinality exception: renewal is
    five-second maintenance, not a domain transition — last_renewed_at is its
    audit record, no event is appended. Also stamps sessions.last_seen_at so
    peer abandon recovery can detect live agents."""
    lease = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds
    def _renew(conn):
        stamp = now()
        heartbeat = conn.execute(
            "UPDATE sessions SET last_seen_at=? WHERE session_id=? AND "
            "status='running'", (stamp, session_id))
        if heartbeat.rowcount != 1:
            return 0
        cur = conn.execute(
            "UPDATE claims SET last_renewed_at=?, lease_expires_at=? "
            "WHERE owner_session_id=? AND status='active' AND "
            "lease_expires_at > ?",
            (stamp, _ts(lease), session_id, stamp))
        return cur.rowcount
    return _in_mutate(conn, _renew)

# --- substantive checkpoints and the blocked transition ---------------------
# Checkpoints are where the agent reasons about protocol state; the
# supervisor never substitutes for them. Freshness is kind-agnostic;
# `blocked` is also the guarded working->blocked transition and is
# implementation-only.

CHECKPOINT_TYPES = ("start", "step", "blocked", "risky", "predone")

def _refuse_from_review(item, operation):
    """The review state is closed — the machine permits only
    review -> working | done. needs_input and the blocked checkpoint call
    this; handoff creation goes through the same guard. The
    two legal exits are a replacement receipt or a changes verdict."""
    if item["status"] == "review":
        raise InvalidTransition(
            f"item {item['id']} is in review; {operation} does not exist "
            "from review — exit via a replacement receipt or a changes "
            "verdict first",
            reason_code="transition_not_available",
            evidence={
                "item_id": item["id"],
                "current_state": item["status"],
                "constraint": "review_state_closed",
            },
        )

def checkpoint(conn, *, ctype, claim_id, actor, session_id, note=None):
    """Record one substantive checkpoint through the claim chokepoint and
    return the compact packet plus the unread inbox range, consumed in the
    same transaction. For `blocked`, the note is the exact stop-condition
    reason (the pinned CLI carries it as --note): the implementation claim
    closes, next_actor clears, ownership is preserved, the item blocks."""
    if ctype not in CHECKPOINT_TYPES:
        raise InvalidTransition(
            f"unknown checkpoint type {ctype!r} "
            f"(want one of {', '.join(CHECKPOINT_TYPES)})",
            reason_code="input_invalid",
            evidence={
                "allowed_states": list(CHECKPOINT_TYPES),
                "constraint": "valid_checkpoint_type",
            },
        )

    def _checkpoint(conn):
        row = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor)
        stamp = now()
        if ctype == "blocked":
            if row["claim_kind"] != "implementation":
                raise InvalidTransition(
                    "blocked is the working->blocked transition; claim "
                    f"{claim_id} is a {row['claim_kind']} claim",
                    reason_code="claim_lane_mismatch",
                    evidence={
                        "item_id": row["item_id"],
                        "claim_id": claim_id,
                        "claim_kind": row["claim_kind"],
                        "required_claim_kind": "implementation",
                    },
                )
            if not isinstance(note, str) or not note.strip():
                raise InvalidTransition(
                    "blocked requires --note naming the exact stop condition",
                    reason_code="input_invalid",
                    evidence={
                        "item_id": row["item_id"],
                        "claim_id": claim_id,
                        "constraint": "blocked_stop_condition_required",
                    },
                )
            _refuse_from_review(
                _item_row(conn, row["item_id"]), "checkpoint blocked")
            conn.execute(
                "UPDATE claims SET status='closed', closed_at=?, "
                "close_reason='blocked', last_checkpoint_at=? "
                "WHERE claim_id=?", (stamp, stamp, claim_id))
            conn.execute(
                "UPDATE items SET status='blocked', "
                "next_actor_agent_id=NULL, updated_at=? WHERE id=?",
                (stamp, row["item_id"]))
            append_event(
                conn, item_id=row["item_id"], event_type="item_blocked",
                actor_agent_id=actor, actor_session_id=session_id,
                claim_id=claim_id, fencing_token=row["fencing_token"],
                payload={"type": "blocked", "reason": note.strip(),
                         "claim_id": claim_id})
        else:
            conn.execute(
                "UPDATE claims SET last_checkpoint_at=? WHERE claim_id=?",
                (stamp, claim_id))
            payload = {"type": ctype, "claim_id": claim_id}
            if isinstance(note, str) and note.strip():
                payload["note"] = note.strip()
            append_event(
                conn, item_id=row["item_id"], event_type="checkpoint",
                actor_agent_id=actor, actor_session_id=session_id,
                claim_id=claim_id, fencing_token=row["fencing_token"],
                payload=payload)
        packet = _packet(conn, _item_row(conn, row["item_id"]))
        inbox = [dict(entry) for entry in read_inbox(conn, actor)]
        return {"packet": packet, "inbox": inbox}

    return mutate(conn, _checkpoint)

def find_overdue(conn, *, session_id, checkpoint_limit_seconds):
    """Active claims of this session whose checkpoint age has reached the
    substantive-checkpoint limit — the supervisor's checkpoint-timeout feed
    Read-only; the shutdown itself is the supervisor's."""
    horizon = _ts(-checkpoint_limit_seconds)
    return [
        {"claim_id": r["claim_id"], "item_id": r["item_id"],
         "lane": r["lane_key"], "last_checkpoint_at": r["last_checkpoint_at"]}
        for r in conn.execute(
            "SELECT * FROM claims WHERE owner_session_id=? AND "
            "status='active' AND last_checkpoint_at <= ? ORDER BY claim_id",
            (session_id, horizon))]

# --- needs-input, exact questions, answers, resume --------------------------
# Blocking uncertainty is a first-class state: the question closes execution
# authority; only the addressed agent (or the audited human lane) answers;
# the answer opens the preferred-owner grace window on the item alone.


def _normalized_question_batch(questions):
    try:
        entries = list(questions)
    except TypeError as exc:
        raise InvalidTransition(
            "needs-input batch requires 1-8 --question pairs",
            reason_code="input_invalid",
            evidence={"constraint": "bounded_question_batch_required"},
        ) from exc
    if not 1 <= len(entries) <= MAX_QUESTION_BATCH_SIZE:
        raise InvalidTransition(
            "needs-input batch requires 1-8 --question pairs",
            reason_code="input_invalid",
            evidence={"constraint": "bounded_question_batch_required"},
        )
    normalized = []
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise InvalidTransition(
                "each batch question must be an (agent, exact text) pair",
                reason_code="input_invalid",
                evidence={"constraint": "question_pair_required"},
            )
        to_agent, question = entry
        if not isinstance(to_agent, str) or not to_agent.strip():
            raise InvalidTransition(
                "each batch question requires a target agent",
                reason_code="input_invalid",
                evidence={"constraint": "peer_agent_target_required"},
            )
        to_agent = to_agent.strip()
        if to_agent == "human":
            raise InvalidTransition(
                "needs-input must address peer agents, not human",
                reason_code="human_lane_forbidden",
                evidence={
                    "target_agent_id": "human",
                    "constraint": "peer_agent_target_required",
                },
            )
        if not isinstance(question, str) or not question.strip():
            raise InvalidTransition(
                "each batch question must be exact and non-empty",
                reason_code="input_invalid",
                evidence={"constraint": "non_empty_exact_question"},
            )
        question = question.strip()
        try:
            question_bytes = len(question.encode("utf-8"))
            total_bytes += question_bytes + len(to_agent.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidTransition(
                "batch questions must be valid UTF-8 text",
                reason_code="input_invalid",
                evidence={"constraint": "valid_utf8_question_required"},
            ) from exc
        if question_bytes > MAX_QUESTION_TEXT_BYTES:
            raise InvalidTransition(
                "one batch question exceeds the text budget",
                reason_code="input_invalid",
                evidence={"constraint": "bounded_exact_question_required"},
            )
        normalized.append((to_agent, question))
    if total_bytes > MAX_QUESTION_BATCH_BYTES:
        raise InvalidTransition(
            "needs-input batch exceeds the total text budget",
            reason_code="input_invalid",
            evidence={"constraint": "bounded_question_batch_required"},
        )
    return tuple(normalized)


def _positive_id_batch(values, *, label):
    try:
        entries = tuple(values)
    except TypeError as exc:
        raise InvalidTransition(
            f"{label} requires 1-{MAX_QUESTION_BATCH_SIZE} ids",
            reason_code="input_invalid",
            evidence={"constraint": "bounded_question_batch_required"},
        ) from exc
    if not 1 <= len(entries) <= MAX_QUESTION_BATCH_SIZE:
        raise InvalidTransition(
            f"{label} requires 1-{MAX_QUESTION_BATCH_SIZE} ids",
            reason_code="input_invalid",
            evidence={"constraint": "bounded_question_batch_required"},
        )
    if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in entries):
        raise InvalidTransition(
            f"{label} requires positive integer ids",
            reason_code="input_invalid",
            evidence={"constraint": "positive_question_ids_required"},
        )
    if len(set(entries)) != len(entries):
        raise InvalidTransition(
            f"{label} requires unique ids",
            reason_code="input_invalid",
            evidence={"constraint": "unique_question_ids_required"},
        )
    return entries


def needs_input_batch(conn, *, claim_id, session_id, questions):
    """Atomically post 1-8 ordinary questions from one implementation claim."""
    normalized = _normalized_question_batch(questions)

    def _needs(conn):
        actor = _session_actor(conn, session_id)
        claim = validate_claim(
            conn,
            claim_id=claim_id,
            session_id=session_id,
            actor=actor,
            expected_kind="implementation",
        )
        _refuse_from_review(
            _item_row(conn, claim["item_id"]),
            "needs-input batch",
        )
        for to_agent, _question in normalized:
            target = conn.execute(
                "SELECT name FROM agents WHERE name=?",
                (to_agent,),
            ).fetchone()
            if target is None:
                raise NotFound(
                    f"unknown target agent {to_agent!r}",
                    reason_code="target_not_found",
                    evidence={
                        "item_id": claim["item_id"],
                        "target_agent_id": to_agent,
                    },
                )
            live = conn.execute(
                "SELECT 1 FROM sessions WHERE agent_id=? AND "
                "status='running' LIMIT 1",
                (to_agent,),
            ).fetchone()
            if live is None:
                latest = conn.execute(
                    "SELECT status FROM sessions WHERE agent_id=? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (to_agent,),
                ).fetchone()
                raise InvalidTransition(
                    f"target agent {to_agent!r} has no running session",
                    reason_code="peer_unavailable",
                    evidence={
                        "item_id": claim["item_id"],
                        "target_agent_id": to_agent,
                        "session_status": (
                            latest["status"]
                            if latest is not None
                            else "missing"
                        ),
                        "required_status": "running",
                    },
                )
        stamp = now()
        owner = conn.execute(
            "SELECT owner_agent_id FROM items WHERE id=?",
            (claim["item_id"],),
        ).fetchone()["owner_agent_id"]
        question_ids = []
        for to_agent, question in normalized:
            cur = conn.execute(
                "INSERT INTO questions(item_id,exact_question,"
                "asked_by_agent,asked_by_session,assigned_to_agent,status,"
                "asked_at) VALUES (?,?,?,?,?,'open',?)",
                (
                    claim["item_id"],
                    question,
                    actor,
                    session_id,
                    to_agent,
                    stamp,
                ),
            )
            question_id = cur.lastrowid
            question_ids.append(question_id)
            event_id = append_event(
                conn,
                item_id=claim["item_id"],
                event_type="needs_input",
                actor_agent_id=actor,
                actor_session_id=session_id,
                claim_id=claim_id,
                fencing_token=claim["fencing_token"],
                payload={
                    "question_id": question_id,
                    "item_id": claim["item_id"],
                    "to": to_agent,
                    "question": question,
                },
            )
            deliver(
                conn,
                recipient=to_agent,
                source_event_id=event_id,
                item_id=claim["item_id"],
                category="question",
                payload={
                    "question_id": question_id,
                    "item_id": claim["item_id"],
                    "question": question,
                },
            )
        conn.execute(
            "UPDATE claims SET status='closed', closed_at=?, "
            "close_reason='needs_input' WHERE claim_id=?",
            (stamp, claim_id),
        )
        conn.execute(
            "UPDATE items SET status='needs_input', "
            "preferred_resume_owner_agent_id=?, next_actor_agent_id=?, "
            "resume_grace_started_at=NULL, resume_grace_expires_at=NULL, "
            "updated_at=? WHERE id=?",
            (owner, normalized[0][0], stamp, claim["item_id"]),
        )
        return question_ids

    return _in_mutate(conn, _needs)

def needs_input(conn, *, claim_id, session_id, to_agent, question):
    """The working->needs_input transition, one transaction.

    Product mode (kickoff + end review only): questions must address a
    **peer agent**, never the reserved `human` actor. Mid-run human Q&A is
    a stall of the happy path, not a protocol feature.
    """
    if not isinstance(question, str) or not question.strip():
        raise InvalidTransition(
            "needs-input requires one exact non-empty --question",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_exact_question"},
        )
    if to_agent == "human":
        raise InvalidTransition(
            "needs-input must address a peer agent (claude/codex/grok), "
            "not human — product happy path has no mid-run human judge; "
            "peers answer or the run stalls for agent recovery",
            reason_code="human_lane_forbidden",
            evidence={
                "target_agent_id": "human",
                "constraint": "peer_agent_target_required",
            },
        )

    def _needs(conn):
        actor = _session_actor(conn, session_id)
        row = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        _refuse_from_review(_item_row(conn, row["item_id"]), "needs-input")
        target = conn.execute(
            "SELECT name FROM agents WHERE name=?", (to_agent,)).fetchone()
        if target is None:
            raise NotFound(
                f"unknown target agent {to_agent!r}; the question must be "
                "addressed to a registered agent",
                reason_code="target_not_found",
                evidence={
                    "item_id": row["item_id"],
                    "target_agent_id": to_agent,
                },
            )
        # Item-25: registration alone is not liveness — an open question to an
        # offline peer never becomes answer_question until they join.
        live_target = conn.execute(
            "SELECT 1 FROM sessions WHERE agent_id=? AND status='running' "
            "LIMIT 1", (to_agent,)).fetchone()
        if live_target is None:
            latest = conn.execute(
                "SELECT status FROM sessions WHERE agent_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (to_agent,),
            ).fetchone()
            raise InvalidTransition(
                f"target agent {to_agent!r} has no running session; "
                "address a live peer (status=running) so the autonomous "
                "loop can emit answer_question",
                reason_code="peer_unavailable",
                evidence={
                    "item_id": row["item_id"],
                    "target_agent_id": to_agent,
                    "session_status": (
                        latest["status"] if latest is not None else "missing"
                    ),
                    "required_status": "running",
                },
            )
        stamp = now()
        conn.execute(
            "UPDATE claims SET status='closed', closed_at=?, "
            "close_reason='needs_input' WHERE claim_id=?", (stamp, claim_id))
        cur = conn.execute(
            "INSERT INTO questions(item_id,exact_question,asked_by_agent,"
            "asked_by_session,assigned_to_agent,status,asked_at) "
            "VALUES (?,?,?,?,?,'open',?)",
            (row["item_id"], question.strip(), actor, session_id,
             to_agent, stamp))
        question_id = cur.lastrowid
        owner = conn.execute(
            "SELECT owner_agent_id FROM items WHERE id=?",
            (row["item_id"],)).fetchone()["owner_agent_id"]
        conn.execute(
            "UPDATE items SET status='needs_input', "
            "preferred_resume_owner_agent_id=?, next_actor_agent_id=?, "
            "updated_at=? WHERE id=?",
            (owner, to_agent, stamp, row["item_id"]))
        event_id = append_event(
            conn, item_id=row["item_id"], event_type="needs_input",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=row["fencing_token"],
            payload={"question_id": question_id, "item_id": row["item_id"],
                     "to": to_agent, "question": question.strip()})
        deliver(conn, recipient=to_agent, source_event_id=event_id,
                item_id=row["item_id"], category="question",
                payload={"question_id": question_id,
                         "item_id": row["item_id"],
                         "question": question.strip()})
        return question_id

    return mutate(conn, _needs)

def claim_question(conn, *, question_id, session_id, intent,
                   reclaim_reason=None, lease_seconds=None):
    """Acquire the question-response lane — only a live session whose agent
    is the addressed target; any other actor causes zero mutation. The
    subject checks (question still open, caller the addressed target)
    refuse before any lane logic, so an answered question's lane is
    unreachable regardless of its claim history."""
    if not isinstance(intent, str) or not intent.strip():
        raise InvalidTransition(
            "a claim requires a non-empty --intent",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_claim_intent"},
        )
    reason = reclaim_reason.strip() if isinstance(reclaim_reason, str) else None
    lease = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds

    def _claim(conn):
        actor = _session_actor(conn, session_id)
        q = conn.execute(
            "SELECT * FROM questions WHERE question_id=?",
            (question_id,)).fetchone()
        if q is None:
            raise NotFound(
                f"no question {question_id}",
                reason_code="target_not_found",
                evidence={"question_id": question_id},
            )
        if q["status"] != "open":
            raise InvalidTransition(
                f"question {question_id} is {q['status']}; only open "
                "questions can be claimed",
                reason_code="transition_not_available",
                evidence={
                    "item_id": q["item_id"],
                    "question_id": question_id,
                    "current_status": q["status"],
                    "required_status": "open",
                },
            )
        if q["assigned_to_agent"] != actor:
            raise AddressedTargetMismatch(
                f"question {question_id} is addressed to "
                f"{q['assigned_to_agent']!r}, not {actor!r}",
                reason_code="addressed_target_mismatch",
                evidence={
                    "item_id": q["item_id"],
                    "question_id": question_id,
                    "actor_agent_id": actor,
                    "required_agent_id": q["assigned_to_agent"],
                },
            )
        lane = f"question_response:{question_id}"
        _lane_history_guard(conn, lane=lane, reclaim_reason=reason)
        claim_id, token, expires = _insert_claim(
            conn, item_id=q["item_id"], kind="question_response",
            subject_id=question_id, lane=lane, actor=actor,
            session_id=session_id, intent=intent.strip(),
            lease=lease)
        payload = {"question_id": question_id, "claim_id": claim_id}
        if reason:
            payload["reclaim_reason"] = reason
        append_event(
            conn, item_id=q["item_id"], event_type="question_claimed",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=token, payload=payload)
        return {"claim_id": claim_id, "question_id": question_id,
                "lane": lane, "lease_expires_at": expires}

    return mutate(conn, _claim)


def claim_questions_batch(conn, *, question_ids, session_id, intent,
                          lease_seconds=None):
    """Atomically acquire ordinary response claims for one addressed group."""
    ids = _positive_id_batch(question_ids, label="question claim batch")
    if not isinstance(intent, str) or not intent.strip():
        raise InvalidTransition(
            "a question claim batch requires a non-empty intent",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_claim_intent"},
        )
    lease = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds
    if (
            isinstance(lease, bool)
            or not isinstance(lease, (int, float))
            or lease <= 0):
        raise InvalidTiming("question claim batch lease_seconds must be positive")

    def _claim(conn):
        actor = _session_actor(conn, session_id)
        questions = []
        for question_id in ids:
            question = conn.execute(
                "SELECT * FROM questions WHERE question_id=?",
                (question_id,),
            ).fetchone()
            if question is None:
                raise NotFound(
                    f"no question {question_id}",
                    reason_code="target_not_found",
                    evidence={"question_id": question_id},
                )
            if question["status"] != "open":
                raise InvalidTransition(
                    f"question {question_id} is {question['status']}",
                    reason_code="transition_not_available",
                    evidence={
                        "item_id": question["item_id"],
                        "question_id": question_id,
                        "current_status": question["status"],
                        "required_status": "open",
                    },
                )
            if question["assigned_to_agent"] != actor:
                raise AddressedTargetMismatch(
                    f"question {question_id} is addressed to "
                    f"{question['assigned_to_agent']!r}, not {actor!r}",
                    reason_code="addressed_target_mismatch",
                    evidence={
                        "item_id": question["item_id"],
                        "question_id": question_id,
                        "actor_agent_id": actor,
                        "required_agent_id": question["assigned_to_agent"],
                    },
                )
            questions.append(question)
        item_ids = {question["item_id"] for question in questions}
        if len(item_ids) != 1:
            raise InvalidTransition(
                "a question claim batch must belong to one item",
                reason_code="input_invalid",
                evidence={"constraint": "single_item_question_batch_required"},
            )
        for question in questions:
            _lane_history_guard(
                conn,
                lane=f"question_response:{question['question_id']}",
                reclaim_reason=None,
            )
        results = []
        for question in questions:
            question_id = question["question_id"]
            lane = f"question_response:{question_id}"
            claim_id, token, expires = _insert_claim(
                conn,
                item_id=question["item_id"],
                kind="question_response",
                subject_id=question_id,
                lane=lane,
                actor=actor,
                session_id=session_id,
                intent=intent.strip(),
                lease=lease,
            )
            append_event(
                conn,
                item_id=question["item_id"],
                event_type="question_claimed",
                actor_agent_id=actor,
                actor_session_id=session_id,
                claim_id=claim_id,
                fencing_token=token,
                payload={
                    "question_id": question_id,
                    "claim_id": claim_id,
                },
            )
            results.append({
                "claim_id": claim_id,
                "question_id": question_id,
                "lane": lane,
                "lease_expires_at": expires,
            })
        return results

    return mutate(conn, _claim)

def _apply_answer(conn, q, *, answer, answered_by_agent, answered_by_session,
                  resume_grace_seconds):
    """The shared answer transition: question answered, item stays
    needs_input and unclaimed, next actor back to the preferred owner, and
    the grace window recorded ON THE ITEM ONLY (the legacy question grace
    columns stay null — the item row is the sole current routing state)."""
    stamp = now()
    conn.execute(
        "UPDATE questions SET status='answered', answer=?, "
        "answered_by_agent=?, answered_by_session=?, answered_at=? "
        "WHERE question_id=?",
        (answer, answered_by_agent, answered_by_session, stamp,
         q["question_id"]))
    remaining = conn.execute(
        "SELECT question_id, assigned_to_agent FROM questions WHERE "
        "item_id=? AND status='open' ORDER BY question_id",
        (q["item_id"],),
    ).fetchall()
    if remaining:
        conn.execute(
            "UPDATE items SET next_actor_agent_id=?, "
            "resume_grace_started_at=NULL, resume_grace_expires_at=NULL, "
            "updated_at=? WHERE id=?",
            (remaining[0]["assigned_to_agent"], stamp, q["item_id"]),
        )
        return None, None
    grace = (DEFAULT_RESUME_GRACE_SECONDS if resume_grace_seconds is None
             else resume_grace_seconds)
    expires = _ts(grace)
    owner = conn.execute(
        "SELECT preferred_resume_owner_agent_id FROM items WHERE id=?",
        (q["item_id"],)).fetchone()["preferred_resume_owner_agent_id"]
    conn.execute(
        "UPDATE items SET next_actor_agent_id=?, resume_grace_started_at=?, "
        "resume_grace_expires_at=?, updated_at=? WHERE id=?",
        (owner, stamp, expires, stamp, q["item_id"]))
    return owner, expires

def answer_question(conn, *, claim_id, session_id, answer,
                    resume_grace_seconds=None):
    """Answer through the addressed agent's question-response claim."""
    if not isinstance(answer, str) or not answer.strip():
        raise InvalidTransition(
            "an answer must be non-empty",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_answer"},
        )

    def _answer(conn):
        actor = _session_actor(conn, session_id)
        row = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="question_response")
        q = conn.execute(
            "SELECT * FROM questions WHERE question_id=?",
            (row["subject_id"],)).fetchone()
        if q is None:
            raise InvalidTransition(
                f"question {row['subject_id']} is not open",
                reason_code="target_not_found",
                evidence={
                    "item_id": row["item_id"],
                    "question_id": row["subject_id"],
                },
            )
        if q["status"] != "open":
            raise InvalidTransition(
                f"question {row['subject_id']} is not open",
                reason_code="transition_not_available",
                evidence={
                    "item_id": row["item_id"],
                    "question_id": row["subject_id"],
                    "current_status": q["status"],
                    "required_status": "open",
                },
            )
        stamp = now()
        conn.execute(
            "UPDATE claims SET status='completed', closed_at=?, "
            "close_reason='answered' WHERE claim_id=?", (stamp, claim_id))
        owner, expires = _apply_answer(
            conn, q, answer=answer.strip(), answered_by_agent=actor,
            answered_by_session=session_id,
            resume_grace_seconds=resume_grace_seconds)
        event_id = append_event(
            conn, item_id=q["item_id"], event_type="question_answered",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=row["fencing_token"],
            payload={"question_id": q["question_id"],
                     "item_id": q["item_id"], "resume_owner": owner,
                     "grace_expires_at": expires})
        recipient = owner or q["asked_by_agent"]
        if recipient:
            deliver(conn, recipient=recipient, source_event_id=event_id,
                    item_id=q["item_id"], category="answer",
                    payload={"question_id": q["question_id"],
                             "item_id": q["item_id"],
                             "answer": answer.strip(),
                             "grace_expires_at": expires})

    return mutate(conn, _answer)


def _normalized_answer_batch(answers):
    try:
        entries = list(answers)
    except TypeError as exc:
        raise InvalidTransition(
            "answer batch requires 1-8 (claim, answer) pairs",
            reason_code="input_invalid",
            evidence={"constraint": "bounded_answer_batch_required"},
        ) from exc
    if not 1 <= len(entries) <= MAX_QUESTION_BATCH_SIZE:
        raise InvalidTransition(
            "answer batch requires 1-8 (claim, answer) pairs",
            reason_code="input_invalid",
            evidence={"constraint": "bounded_answer_batch_required"},
        )
    normalized = []
    claim_ids = []
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise InvalidTransition(
                "each answer needs a claim id and exact text",
                reason_code="input_invalid",
                evidence={"constraint": "claim_answer_pair_required"},
            )
        claim_id, answer = entry
        if (
                isinstance(claim_id, bool)
                or not isinstance(claim_id, int)
                or claim_id <= 0):
            raise InvalidTransition(
                "answer claim ids must be positive integers",
                reason_code="input_invalid",
                evidence={"constraint": "positive_claim_ids_required"},
            )
        if not isinstance(answer, str) or not answer.strip():
            raise InvalidTransition(
                "every answer in a batch must be non-empty",
                reason_code="input_invalid",
                evidence={"constraint": "non_empty_answer"},
            )
        answer = answer.strip()
        try:
            answer_bytes = len(answer.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidTransition(
                "batch answers must be valid UTF-8 text",
                reason_code="input_invalid",
                evidence={"constraint": "valid_utf8_answer_required"},
            ) from exc
        if answer_bytes > MAX_QUESTION_TEXT_BYTES:
            raise InvalidTransition(
                "one batch answer exceeds the text budget",
                reason_code="input_invalid",
                evidence={"constraint": "bounded_answer_required"},
            )
        total_bytes += answer_bytes
        claim_ids.append(claim_id)
        normalized.append((claim_id, answer))
    if len(set(claim_ids)) != len(claim_ids):
        raise InvalidTransition(
            "answer batch claim ids must be unique",
            reason_code="input_invalid",
            evidence={"constraint": "unique_claim_ids_required"},
        )
    if total_bytes > MAX_QUESTION_BATCH_BYTES:
        raise InvalidTransition(
            "answer batch exceeds the total text budget",
            reason_code="input_invalid",
            evidence={"constraint": "bounded_answer_batch_required"},
        )
    return tuple(normalized)


def answer_questions_batch(conn, *, session_id, answers,
                           resume_grace_seconds=None):
    """Atomically answer separately claimed question-response lanes."""
    normalized = _normalized_answer_batch(answers)
    if (
            resume_grace_seconds is not None
            and (
                isinstance(resume_grace_seconds, bool)
                or not isinstance(resume_grace_seconds, (int, float))
                or resume_grace_seconds <= 0
            )):
        raise InvalidTiming("resume_grace_seconds must be positive")

    def _answer(conn):
        actor = _session_actor(conn, session_id)
        validated = []
        for claim_id, answer in normalized:
            claim = validate_claim(
                conn,
                claim_id=claim_id,
                session_id=session_id,
                actor=actor,
                expected_kind="question_response",
            )
            question = conn.execute(
                "SELECT * FROM questions WHERE question_id=?",
                (claim["subject_id"],),
            ).fetchone()
            if question is None:
                raise NotFound(
                    f"no question {claim['subject_id']}",
                    reason_code="target_not_found",
                    evidence={"question_id": claim["subject_id"]},
                )
            if question["status"] != "open":
                raise InvalidTransition(
                    f"question {question['question_id']} is not open",
                    reason_code="transition_not_available",
                    evidence={
                        "item_id": question["item_id"],
                        "question_id": question["question_id"],
                        "current_status": question["status"],
                        "required_status": "open",
                    },
                )
            if (
                    question["assigned_to_agent"] != actor
                    or question["item_id"] != claim["item_id"]):
                raise AddressedTargetMismatch(
                    f"question {question['question_id']} is not assigned "
                    f"to {actor!r}",
                    reason_code="addressed_target_mismatch",
                    evidence={
                        "item_id": question["item_id"],
                        "question_id": question["question_id"],
                        "actor_agent_id": actor,
                        "required_agent_id": question[
                            "assigned_to_agent"
                        ],
                    },
                )
            validated.append((claim, question, answer))
        item_ids = {question["item_id"] for _claim, question, _answer in validated}
        question_ids = [
            question["question_id"]
            for _claim, question, _answer in validated
        ]
        if len(item_ids) != 1 or len(set(question_ids)) != len(question_ids):
            raise InvalidTransition(
                "an answer batch must contain unique questions on one item",
                reason_code="input_invalid",
                evidence={"constraint": "single_item_question_batch_required"},
            )
        item_id = next(iter(item_ids))
        stamp = now()
        for claim, question, answer in validated:
            conn.execute(
                "UPDATE claims SET status='completed', closed_at=?, "
                "close_reason='answered' WHERE claim_id=?",
                (stamp, claim["claim_id"]),
            )
            conn.execute(
                "UPDATE questions SET status='answered', answer=?, "
                "answered_by_agent=?, answered_by_session=?, answered_at=? "
                "WHERE question_id=?",
                (
                    answer,
                    actor,
                    session_id,
                    stamp,
                    question["question_id"],
                ),
            )
        remaining = conn.execute(
            "SELECT question_id, assigned_to_agent FROM questions WHERE "
            "item_id=? AND status='open' ORDER BY question_id",
            (item_id,),
        ).fetchall()
        owner = conn.execute(
            "SELECT preferred_resume_owner_agent_id FROM items WHERE id=?",
            (item_id,),
        ).fetchone()["preferred_resume_owner_agent_id"]
        if remaining:
            resume_owner = None
            expires = None
            conn.execute(
                "UPDATE items SET next_actor_agent_id=?, "
                "resume_grace_started_at=NULL, "
                "resume_grace_expires_at=NULL, updated_at=? WHERE id=?",
                (remaining[0]["assigned_to_agent"], stamp, item_id),
            )
        else:
            resume_owner = owner
            grace = (
                DEFAULT_RESUME_GRACE_SECONDS
                if resume_grace_seconds is None
                else resume_grace_seconds
            )
            expires = _ts(grace)
            conn.execute(
                "UPDATE items SET next_actor_agent_id=?, "
                "resume_grace_started_at=?, resume_grace_expires_at=?, "
                "updated_at=? WHERE id=?",
                (resume_owner, stamp, expires, stamp, item_id),
            )
        for claim, question, answer in validated:
            event_id = append_event(
                conn,
                item_id=item_id,
                event_type="question_answered",
                actor_agent_id=actor,
                actor_session_id=session_id,
                claim_id=claim["claim_id"],
                fencing_token=claim["fencing_token"],
                payload={
                    "question_id": question["question_id"],
                    "item_id": item_id,
                    "resume_owner": resume_owner,
                    "grace_expires_at": expires,
                },
            )
            recipient = resume_owner or question["asked_by_agent"]
            if recipient:
                deliver(
                    conn,
                    recipient=recipient,
                    source_event_id=event_id,
                    item_id=item_id,
                    category="answer",
                    payload={
                        "question_id": question["question_id"],
                        "item_id": item_id,
                        "answer": answer,
                        "grace_expires_at": expires,
                    },
                )
        return {
            "question_ids": question_ids,
            "remaining_open_question_ids": [
                row["question_id"] for row in remaining
            ],
            "resume_owner": resume_owner,
            "grace_expires_at": expires,
        }

    return mutate(conn, _answer)

def admin_answer(conn, *, question_id, answer, reason,
                 resume_grace_seconds=None):
    """The audited trusted-local human answer lane: no session,
    reason required, supersedes any active response claim so a late agent
    answer is fenced out, then performs the identical answer transition."""
    if os.environ.get("COOP_SESSION_ID"):
        raise HumanLaneViolation(
            "the human answer lane runs outside supervised sessions; "
            "unset COOP_SESSION_ID",
            reason_code="human_lane_forbidden",
            evidence={"constraint": "offline_human_lane_required"},
        )
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidTransition(
            "admin answer requires --reason",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_admin_answer_reason"},
        )
    if not isinstance(answer, str) or not answer.strip():
        raise InvalidTransition(
            "an answer must be non-empty",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_answer"},
        )

    def _answer(conn):
        q = conn.execute(
            "SELECT * FROM questions WHERE question_id=?",
            (question_id,)).fetchone()
        if q is None:
            raise NotFound(
                f"no question {question_id}",
                reason_code="target_not_found",
                evidence={"question_id": question_id},
            )
        if q["status"] != "open":
            raise InvalidTransition(
                f"question {question_id} is {q['status']}, not open",
                reason_code="transition_not_available",
                evidence={
                    "item_id": q["item_id"],
                    "question_id": question_id,
                    "current_status": q["status"],
                    "required_status": "open",
                },
            )
        stamp = now()
        superseded = conn.execute(
            "SELECT * FROM claims WHERE lane_key=? AND status='active'",
            (f"question_response:{question_id}",)).fetchone()
        superseded_id = None
        if superseded is not None:
            conn.execute(
                "UPDATE claims SET status='closed', closed_at=?, "
                "close_reason='superseded_by_human' WHERE claim_id=?",
                (stamp, superseded["claim_id"]))
            superseded_id = superseded["claim_id"]
        owner, expires = _apply_answer(
            conn, q, answer=answer.strip(), answered_by_agent="human",
            answered_by_session=None,
            resume_grace_seconds=resume_grace_seconds)
        event_id = append_event(
            conn, item_id=q["item_id"], event_type="question_answered",
            actor_agent_id="human", actor_session_id=None,
            payload={"question_id": question_id, "item_id": q["item_id"],
                     "by": "human", "reason": reason.strip(),
                     "original_target": q["assigned_to_agent"],
                     "superseded_claim_id": superseded_id,
                     "resume_owner": owner, "grace_expires_at": expires})
        recipient = owner or q["asked_by_agent"]
        if recipient:
            deliver(conn, recipient=recipient, source_event_id=event_id,
                    item_id=q["item_id"], category="answer",
                    payload={"question_id": question_id,
                             "item_id": q["item_id"],
                             "answer": answer.strip(),
                             "grace_expires_at": expires})

    return mutate(conn, _answer)

# --- hashed receipts and the mechanical linter ------------------------------
# A receipt is immutable evidence metadata: the file's bytes hashed at
# submission, typed references mechanically verified, one current receipt
# per item. Reads re-hash opportunistically and only mark;
# the authoritative re-hash gate is completion.

_REF_TABLES = {
    "debate": ("debates", "id"),
    "decision": ("decisions", "id"),
    "event": ("events", "event_id"),
}

def _proof_error_evidence(item_id):
    evidence = {"constraint": "proof_reference_invalid"}
    if item_id is not None:
        evidence["item_id"] = item_id
    return evidence


def normalize_proof_refs(refs, *, item_id=None):
    """Pure grammar pass over typed proof references — exactly
    debate:<int> | decision:<int> | event:<int> | file:<absolute-path>.
    Never touches the database or the filesystem; the value after the
    first colon may itself contain colons (Windows drive paths)."""
    out = []
    for raw in refs or []:
        if not isinstance(raw, str) or ":" not in raw:
            raise ProofReferenceInvalid(
                f"malformed proof reference {raw!r}; expected <type>:<value>",
                reason_code="proof_invalid",
                evidence=_proof_error_evidence(item_id),
            )
        rtype, value = raw.split(":", 1)
        if rtype in _REF_TABLES:
            if not value.isdigit():
                raise ProofReferenceInvalid(
                    f"proof reference {raw!r} needs an integer id",
                    reason_code="proof_invalid",
                    evidence=_proof_error_evidence(item_id),
                )
            out.append({"type": rtype, "id": int(value)})
        elif rtype == "file":
            if not pathlib.Path(value).is_absolute():
                raise ProofReferenceInvalid(
                    f"file reference {value!r} must be an absolute path",
                    reason_code="proof_invalid",
                    evidence=_proof_error_evidence(item_id),
                )
            out.append({"type": "file", "path": value})
        else:
            raise ProofReferenceInvalid(
                f"unknown proof reference type {rtype!r}",
                reason_code="proof_invalid",
                evidence=_proof_error_evidence(item_id),
            )
    return out

def lint_proof_refs(conn, *, item_id, refs, phase="submit"):
    """Mechanical verification: board rows must exist AND attach
    to the item (a null-item legacy row attaches to nothing); file refs
    resolve and each carries its own sha256. Returns the normalized list.
    The linter never reads prose and never judges sufficiency. The
    completion-phase split semantics (file failures vs board anomalies)
    belong to completion; this verification pass itself is phase-agnostic."""
    out = []
    for ref in normalize_proof_refs(refs, item_id=item_id):
        if ref["type"] == "file":
            source = pathlib.Path(ref["path"])
            try:
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
            except OSError as exc:
                raise ProofReferenceInvalid(
                    f"file reference {ref['path']!r} is not readable: {exc}",
                    reason_code="proof_invalid",
                    evidence=_proof_error_evidence(item_id),
                )
            out.append({"type": "file", "path": str(source.resolve()),
                        "sha256": digest})
        else:
            table, pk = _REF_TABLES[ref["type"]]
            row = conn.execute(
                f"SELECT item_id FROM {table} WHERE {pk}=?",
                (ref["id"],)).fetchone()
            if row is None:
                raise ProofReferenceInvalid(
                    f"{ref['type']} {ref['id']} does not exist on this board",
                    reason_code="proof_invalid",
                    evidence=_proof_error_evidence(item_id),
                )
            if row["item_id"] != item_id:
                raise ProofReferenceInvalid(
                    f"{ref['type']} {ref['id']} is not attached to item "
                    f"{item_id} (attached: {row['item_id']!r})",
                    reason_code="proof_invalid",
                    evidence=_proof_error_evidence(item_id),
                )
            out.append(dict(ref))
    return out

def submit_receipt(conn, *, claim_id, session_id, actor, path, summary,
                   proof, proof_refs=None):
    """One-transaction receipt submission: validate the
    implementation claim, hash the evidence file, lint every reference,
    supersede any prior unsuperseded receipt for the item, append the
    receipt_submitted event. No inbox delivery — submission is the owner's
    own act, visible through packet, status, and projection."""
    if not isinstance(summary, str) or not summary.strip():
        raise InvalidTransition(
            "a receipt requires a non-empty --summary",
            reason_code="input_invalid",
            evidence={
                "field": "summary",
                "constraint": "non_empty_receipt_field",
            },
        )
    if not isinstance(proof, str) or not proof.strip():
        raise InvalidTransition(
            "a receipt requires a non-empty --proof",
            reason_code="input_invalid",
            evidence={
                "field": "proof",
                "constraint": "non_empty_receipt_field",
            },
        )

    def _submit(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        if item["status"] not in ("working", "review"):
            raise InvalidTransition(
                f"item {item['id']} is {item['status']}; a receipt is "
                "submitted while the item is working (or as the "
                "replacement path while it is in review)",
                reason_code="transition_not_available",
                evidence={
                    "item_id": item["id"],
                    "current_state": item["status"],
                    "allowed_states": ["working", "review"],
                },
            )
        acceptance = contract_acceptance(conn, item["id"])
        if acceptance["required"] and acceptance["state"] != "accepted":
            raise InvalidTransition(
                f"item {item['id']} agent-authored contract is "
                f"{acceptance['state']}; a peer huddle must accept it before "
                "execution evidence can be submitted",
                reason_code="contract_acceptance_required",
                evidence={
                    "item_id": item["id"],
                    "current_state": acceptance["state"],
                    "required_state": "accepted",
                },
            )
        source = pathlib.Path(path)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise ReceiptInvalid(
                f"receipt file {str(source)!r} is not readable: {exc}",
                reason_code="receipt_invalid",
                evidence={
                    "item_id": item["id"],
                    "claim_id": claim_id,
                    "constraint": "receipt_source_readable",
                },
            )
        if not data:
            raise ReceiptInvalid(
                f"receipt file {str(source)!r} is empty",
                reason_code="receipt_invalid",
                evidence={
                    "item_id": item["id"],
                    "claim_id": claim_id,
                    "constraint": "receipt_source_non_empty",
                },
            )
        refs = lint_proof_refs(
            conn, item_id=item["id"], refs=proof_refs, phase="submit")
        stamp = now()
        resolved = str(source.resolve())
        digest = hashlib.sha256(data).hexdigest()
        prior = conn.execute(
            "SELECT receipt_id FROM receipts WHERE item_id=? AND "
            "superseded_at IS NULL", (item["id"],)).fetchone()
        if prior is not None:
            conn.execute(
                "UPDATE receipts SET superseded_at=? WHERE receipt_id=?",
                (stamp, prior["receipt_id"]))
        cur = conn.execute(
            "INSERT INTO receipts(item_id,claim_id,fencing_token,"
            "contract_version,submitted_by_agent,submitted_by_session,"
            "summary,proof,proof_references_json,source_path,sha256,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (item["id"], claim_id, claim["fencing_token"],
             item["contract_version"], actor, session_id, summary.strip(),
             proof.strip(), _payload_json(refs), resolved, digest, stamp))
        receipt_id = cur.lastrowid
        payload = {"item_id": item["id"], "receipt_id": receipt_id,
                   "source_path": resolved, "sha256": digest,
                   "reference_count": len(refs)}
        if prior is not None:
            payload["superseded_receipt_id"] = prior["receipt_id"]
        # The replacement-during-review path: superseding the
        # receipt must not leave a silently dead review standing — the same
        # transaction returns the item to working, fences any active review
        # claim (closed, reason receipt_superseded — the review-lane
        # analogue of a claim superseded by an audited human answer), and
        # notifies the claimant or named reviewer. The review row itself is
        # append-only history, dead by derivation.
        death_recipient = None
        death_receipt_id = prior["receipt_id"] if prior is not None else None
        if item["status"] == "review":
            # The newest unresolved review row — normally bound to the
            # receipt superseded just above; after a completion-time
            # evidence failure the item can sit in review with NO
            # current receipt, and the replacement must still fence any
            # doomed review claim and exit to working.
            dead = conn.execute(
                "SELECT * FROM reviews WHERE item_id=? AND "
                "status='requested' AND resolved_at IS NULL "
                "ORDER BY id DESC LIMIT 1", (item["id"],)).fetchone()
            if dead is not None:
                payload["review_death"] = dead["id"]
                if death_receipt_id is None:
                    death_receipt_id = dead["receipt_id"]
                rclaim = conn.execute(
                    "SELECT * FROM claims WHERE lane_key=? AND "
                    "status='active'", (f"review:{dead['id']}",)).fetchone()
                if rclaim is not None:
                    conn.execute(
                        "UPDATE claims SET status='closed', closed_at=?, "
                        "close_reason='receipt_superseded' WHERE claim_id=?",
                        (stamp, rclaim["claim_id"]))
                    payload["closed_review_claim_id"] = rclaim["claim_id"]
                    death_recipient = rclaim["claimed_by_agent"]
                else:
                    payload["closed_review_claim_id"] = None
                    death_recipient = dead["reviewer"]
            conn.execute(
                "UPDATE items SET status='working', updated_at=? "
                "WHERE id=?", (stamp, item["id"]))
        event_id = append_event(
            conn, item_id=item["id"], event_type="receipt_submitted",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload=payload)
        if death_recipient is not None and death_recipient != actor:
            deliver(conn, recipient=death_recipient,
                    source_event_id=event_id, item_id=item["id"],
                    category="review_death",
                    payload={"review_id": payload["review_death"],
                             "item_id": item["id"],
                             "superseded_receipt_id": death_receipt_id,
                             "new_receipt_id": receipt_id})
        return receipt_id

    return _in_mutate(conn, _submit)

def receipt_marker(row):
    """Opportunistic display-only re-hash: 'missing' when the
    source no longer reads, 'changed' when its bytes hash differently,
    None when the evidence still matches. Never mutates, never raises."""
    try:
        data = pathlib.Path(row["source_path"]).read_bytes()
    except OSError:
        return "missing"
    return None if hashlib.sha256(data).hexdigest() == row["sha256"] else "changed"

def _current_receipt(conn, item_id):
    return conn.execute(
        "SELECT * FROM receipts WHERE item_id=? AND superseded_at IS NULL "
        "ORDER BY receipt_id DESC LIMIT 1", (item_id,)).fetchone()

# --- reviews: request, designation, and the claim that observes -------------
# `reviews.reviewer` is the immutable request-time designation (null =
# unnamed; legacy semantics preserved); `reviewer_agent_id` is the
# informational latest claimant; the active row on the review:<id> claims
# lane is the sole authority for the current holder.

def _live_review(conn, item_id):
    """The item's current live review by derivation: unresolved and bound
    to the item's current receipt and contract version. Legacy rows (null
    receipt) never qualify — nothing fabricates claim state for them."""
    current = _current_receipt(conn, item_id)
    if current is None:
        return None
    item = _item_row(conn, item_id)
    return conn.execute(
        "SELECT * FROM reviews WHERE item_id=? AND status='requested' AND "
        "resolved_at IS NULL AND receipt_id=? AND contract_version=? "
        "ORDER BY id DESC LIMIT 1",
        (item_id, current["receipt_id"], item["contract_version"])).fetchone()

def request_review(conn, *, claim_id, session_id, actor, reviewer=None):
    """One-transaction review request: validate the
    implementation claim, require a current receipt, refuse a duplicate
    live review, refuse any non-`working` status (the review-state-closed
    matrix admits no request from `review` — the escape is a replacement
    receipt), guard the designation before any write, then insert the
    append-only review row and move the item working -> review. The
    implementation claim stays open — the owner keeps it current."""

    def _request(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        receipt = _current_receipt(conn, item["id"])
        if receipt is None:
            raise ReceiptMissing(
                f"item {item['id']} has no current receipt; submit one "
                "before requesting review",
                reason_code="receipt_missing",
                evidence={
                    "item_id": item["id"],
                    "claim_id": claim_id,
                    "constraint": "current_receipt_required",
                },
            )
        live = _live_review(conn, item["id"])
        if live is not None:
            raise InvalidTransition(
                f"review {live['id']} is already live for item "
                f"{item['id']}; await its verdict or replace the receipt",
                reason_code="transition_not_available",
                evidence={
                    "item_id": item["id"],
                    "review_id": live["id"],
                    "current_status": live["status"],
                },
            )
        # working: first (or post-changes) request. review: second (or Nth)
        # independent review after a prior approve while the item remains
        # in review (binding-second-review plan — no dummy receipt flip).
        if item["status"] not in ("working", "review"):
            raise InvalidTransition(
                f"item {item['id']} is {item['status']}; a review is "
                "requested from working or review (no live review) only",
                reason_code="transition_not_available",
                evidence={
                    "item_id": item["id"],
                    "current_state": item["status"],
                    "allowed_states": ["working", "review"],
                },
            )
        if reviewer is not None:
            if reviewer == "human":
                raise HumanLaneViolation(
                    "the reserved human actor can never hold a session or "
                    "claim, so a review named to human could never be "
                    "verdicted; name an agent or leave the review unnamed",
                    reason_code="human_lane_forbidden",
                    evidence={
                        "target_agent_id": reviewer,
                        "constraint": "agent_review_lane_required",
                    },
                )
            named = conn.execute(
                "SELECT name, provider FROM agents WHERE name=?",
                (reviewer,)).fetchone()
            if named is None:
                raise NotFound(
                    f"unknown reviewer {reviewer!r}; a named reviewer must "
                    "be a registered agent",
                    reason_code="target_not_found",
                    evidence={"target_agent_id": reviewer},
                )
            if reviewer == item["owner_agent_id"]:
                raise SelfReview(
                    f"{reviewer!r} owns item {item['id']}; the reviewer "
                    "must differ from the durable owner",
                    reason_code="reviewer_is_owner",
                    evidence={
                        "item_id": item["id"],
                        "actor_agent_id": reviewer,
                        "owner_agent_id": item["owner_agent_id"],
                    },
                )
            provider = (
                named["provider"].strip()
                if isinstance(named["provider"], str)
                and named["provider"].strip()
                else None
            )
            if provider is None:
                raise InvalidTransition(
                    f"named reviewer {reviewer!r} has no bound provider; "
                    "launch or bind that agent before requesting its review",
                    reason_code="second_reviewer_not_selected",
                    evidence={
                        "item_id": item["id"],
                        "receipt_id": receipt["receipt_id"],
                        "required_agent_id": reviewer,
                        "constraint": "bound_review_provider_required",
                    },
                )
            if provider in _current_approval_providers(conn, item["id"]):
                raise InvalidTransition(
                    f"provider {provider!r} already has a qualifying "
                    f"approval on receipt {receipt['receipt_id']}; name a "
                    "reviewer from a provider not yet represented",
                    reason_code="review_provider_already_approved",
                    evidence={
                        "item_id": item["id"],
                        "receipt_id": receipt["receipt_id"],
                        "required_agent_id": reviewer,
                        "provider": provider,
                        "constraint":
                            "new_provider_required_for_named_review",
                    },
                )
        # Do not open a review when no registered, provider-bound non-owner
        # can expand the approval set. This applies to the first request too:
        # an unclaimable row is a latent wedge, not useful progress.
        block = second_reviewer_blocked(conn, item["id"])
        if block["blocked"]:
            raise InvalidTransition(
                f"item {item['id']} still needs "
                f"{block['still_needed']} distinct-provider approve(s) but "
                f"no remaining registered provider can expand the set "
                f"(approved={block['approved_providers'] or '[]'}); "
                "register another provider or revise the quorum — do not "
                "re-request review",
                reason_code="second_reviewer_not_selected",
                evidence={
                    "item_id": item["id"],
                    "receipt_id": receipt["receipt_id"],
                    "required_count": block["still_needed"],
                    "approved_providers": block["approved_providers"],
                    "remaining_providers": block["remaining_providers"],
                    "constraint": "remaining_distinct_provider_required",
                },
            )
        stamp = now()
        cur = conn.execute(
            "INSERT INTO reviews(item_id,receipt_id,contract_version,"
            "requested_by,requested_by_agent,reviewer,reviewer_agent_id,"
            "status,legacy,created_at) VALUES (?,?,?,?,?,?,NULL,"
            "'requested',0,?)",
            (item["id"], receipt["receipt_id"], item["contract_version"],
             actor, actor, reviewer, stamp))
        review_id = cur.lastrowid
        conn.execute(
            "UPDATE items SET status='review', updated_at=? WHERE id=?",
            (stamp, item["id"]))
        payload = {"item_id": item["id"], "review_id": review_id,
                   "receipt_id": receipt["receipt_id"],
                   "contract_version": item["contract_version"]}
        if reviewer is not None:
            payload["reviewer"] = reviewer
        event_id = append_event(
            conn, item_id=item["id"], event_type="review_requested",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload=payload)
        if reviewer is not None:
            deliver(conn, recipient=reviewer, source_event_id=event_id,
                    item_id=item["id"], category="review_request",
                    payload={"review_id": review_id, "item_id": item["id"],
                             "receipt_id": receipt["receipt_id"]})
        return review_id

    return _in_mutate(conn, _request)

def claim_review(conn, *, review_id, session_id, intent,
                 reclaim_reason=None, lease_seconds=None):
    """Acquire the review:<id> lane through the shared claim guard.
    Subject eligibility first: the review must be live by derivation
    (`review_stale` otherwise, including at first claim); the claimant must
    differ from the durable owner (`self_review`); a named review admits
    only its designated reviewer. Returns (claim, packet) — the packet,
    current decisions included, WITHOUT advancing the inbox cursor: cursor
    advancement stays reserved to checkpoints and `coop inbox`; the claim
    event is the initial observation watermark."""
    if not isinstance(intent, str) or not intent.strip():
        raise InvalidTransition(
            "a claim requires a non-empty --intent",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_claim_intent"},
        )
    reason = reclaim_reason.strip() if isinstance(reclaim_reason, str) else None
    lease = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds

    def _claim(conn):
        actor = _session_actor(conn, session_id)
        rev = conn.execute(
            "SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if rev is None:
            raise NotFound(
                f"no review {review_id}",
                reason_code="target_not_found",
                evidence={"review_id": review_id},
            )
        item = _item_row(conn, rev["item_id"])
        current = _current_receipt(conn, item["id"])
        if (rev["status"] != "requested" or rev["resolved_at"] is not None
                or current is None
                or rev["receipt_id"] != current["receipt_id"]
                or rev["contract_version"] != item["contract_version"]):
            raise ReviewStale(
                f"review {review_id} is dead by derivation (it does not "
                "bind the item's current receipt and contract version); "
                "re-request against the current receipt",
                reason_code="review_stale",
                evidence={
                    "item_id": item["id"],
                    "review_id": review_id,
                    "current_status": rev["status"],
                },
            )
        if actor == item["owner_agent_id"]:
            raise SelfReview(
                f"{actor!r} owns item {item['id']}; the reviewer must "
                "differ from the durable owner",
                reason_code="reviewer_is_owner",
                evidence={
                    "item_id": item["id"],
                    "review_id": review_id,
                    "actor_agent_id": actor,
                    "owner_agent_id": item["owner_agent_id"],
                },
            )
        if rev["reviewer"] is not None and rev["reviewer"] != actor:
            raise AddressedTargetMismatch(
                f"review {review_id} is designated to "
                f"{rev['reviewer']!r}, not {actor!r}",
                reason_code="addressed_target_mismatch",
                evidence={
                    "item_id": item["id"],
                    "review_id": review_id,
                    "actor_agent_id": actor,
                    "required_agent_id": rev["reviewer"],
                },
            )
        provider = _agent_provider_bucket(conn, actor)
        if provider in _current_approval_providers(conn, item["id"]):
            raise InvalidTransition(
                f"provider {provider!r} already has a qualifying approval "
                f"on receipt {current['receipt_id']}; reviews require a "
                "provider not yet represented",
                reason_code="review_provider_already_approved",
                evidence={
                    "item_id": item["id"],
                    "review_id": review_id,
                    "receipt_id": current["receipt_id"],
                    "actor_agent_id": actor,
                    "provider": provider,
                    "constraint": "new_provider_required_for_review",
                },
            )
        lane = f"review:{review_id}"
        _lane_history_guard(conn, lane=lane, reclaim_reason=reason)
        claim_id, token, expires = _insert_claim(
            conn, item_id=item["id"], kind="review", subject_id=review_id,
            lane=lane, actor=actor, session_id=session_id,
            intent=intent.strip(), lease=lease)
        conn.execute(
            "UPDATE reviews SET reviewer_agent_id=? WHERE id=?",
            (actor, review_id))
        payload = {"review_id": review_id, "item_id": item["id"],
                   "claim_id": claim_id}
        if reason:
            payload["reclaim_reason"] = reason
        append_event(
            conn, item_id=item["id"], event_type="review_claimed",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=token, payload=payload)
        packet = _packet(conn, _item_row(conn, item["id"]))
        claim = {"claim_id": claim_id, "review_id": review_id,
                 "lane": lane, "lease_expires_at": expires}
        return claim, packet

    return mutate(conn, _claim)

def submit_verdict(conn, *, claim_id, session_id, actor, verdict, body=None):
    """One-transaction verdict through the review claim: claim
    validation first (a replacement-closed claim refuses stale_claim before
    any derivation), then liveness re-derivation (`review_stale`), the
    verdict-time owner re-check (`self_review` — takeover mid-review), and
    the observation watermark (`decision_unobserved` — the claim is left
    live; one checkpoint unblocks). `approve` resolves the review and keeps
    the item in review; `changes` resolves, returns the item to working,
    and supersedes the reviewed receipt (reason `changes`). Either verdict
    completes the review claim and delivers to the requesting owner."""
    if verdict not in ("approve", "changes"):
        raise InvalidTransition(
            "verdict must be approve or changes",
            reason_code="input_invalid",
            evidence={
                "field": "verdict",
                "constraint": "valid_review_verdict",
            },
        )
    note = body.strip() if isinstance(body, str) and body.strip() else None
    # REVIEW-INTEGRITY: a rejection the owner cannot act on is a stall (item 19,
    # run3). A 'changes' verdict must carry a reason — the mirror of the
    # receipt-must-cite-evidence rule for the review side.
    if verdict == "changes" and note is None:
        raise InvalidTransition(
            "a 'changes' verdict must include a reason (--body): name what to "
            "fix so the owner can rework, or approve instead",
            reason_code="input_invalid",
            evidence={
                "field": "body",
                "constraint": "changes_reason_required",
            },
        )

    def _submit(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="review")
        review_id = claim["subject_id"]
        rev = conn.execute(
            "SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        item = _item_row(conn, rev["item_id"])
        current = _current_receipt(conn, item["id"])
        if (rev["status"] != "requested" or rev["resolved_at"] is not None
                or current is None
                or rev["receipt_id"] != current["receipt_id"]
                or rev["contract_version"] != item["contract_version"]):
            raise ReviewStale(
                f"review {review_id} is dead by derivation; a verdict can "
                "only resolve a review bound to the item's current receipt "
                "and contract version",
                reason_code="review_stale",
                evidence={
                    "item_id": item["id"],
                    "review_id": review_id,
                    "current_status": rev["status"],
                },
            )
        if actor == item["owner_agent_id"]:
            raise SelfReview(
                f"{actor!r} now owns item {item['id']}; ownership changed "
                "since the claim, and the reviewer must differ from the "
                "durable owner at verdict time",
                reason_code="reviewer_is_owner",
                evidence={
                    "item_id": item["id"],
                    "review_id": review_id,
                    "actor_agent_id": actor,
                    "owner_agent_id": item["owner_agent_id"],
                },
            )
        # Named-designation loophole: even if claim slipped through, a second
        # approve from an already-represented provider cannot expand quorum.
        if verdict == "approve":
            provider = _agent_provider_bucket(conn, actor)
            if provider in _current_approval_providers(conn, item["id"]):
                raise InvalidTransition(
                    f"provider {provider!r} already has a qualifying "
                    f"approval on receipt {current['receipt_id']}; another "
                    "approve from the same provider does not count toward "
                    "review_quorum",
                    reason_code="review_provider_already_approved",
                    evidence={
                        "item_id": item["id"],
                        "review_id": review_id,
                        "receipt_id": current["receipt_id"],
                        "actor_agent_id": actor,
                        "provider": provider,
                        "constraint":
                            "new_provider_required_for_additional_approve",
                    },
                )
        newest_decision_row = conn.execute(
            "SELECT event_id, payload_json FROM events WHERE item_id=? AND "
            "event_type='decision_recorded' ORDER BY event_id DESC LIMIT 1",
            (item["id"],),
        ).fetchone()
        newest_decision = (
            newest_decision_row["event_id"]
            if newest_decision_row is not None else None
        )
        newest_observation = conn.execute(
            "SELECT MAX(event_id) AS m FROM events WHERE claim_id=? AND "
            "event_type IN ('review_claimed','checkpoint')",
            (claim_id,)).fetchone()["m"]
        if newest_decision is not None and (
                newest_observation is None
                or newest_decision > newest_observation):
            decision_evidence = {
                "item_id": item["id"],
                "review_id": review_id,
            }
            try:
                decision_id = json.loads(
                    newest_decision_row["payload_json"]).get("decision_id")
            except (TypeError, ValueError, AttributeError):
                decision_id = None
            if isinstance(decision_id, int):
                decision_evidence["decision_id"] = decision_id
            raise DecisionUnobserved(
                f"decision event {newest_decision} postdates this claim's "
                "newest observation; checkpoint once to observe it, then "
                "verdict again",
                reason_code="decision_unobserved",
                evidence=decision_evidence,
            )
        stamp = now()
        resolved_status = "approved" if verdict == "approve" else "changes"
        conn.execute(
            "UPDATE reviews SET status=?, resolved_at=? WHERE id=?",
            (resolved_status, stamp, review_id))
        conn.execute(
            "UPDATE claims SET status='completed', closed_at=?, "
            "close_reason='verdict' WHERE claim_id=?", (stamp, claim_id))
        payload = {"review_id": review_id, "item_id": item["id"],
                   "verdict": verdict, "receipt_id": rev["receipt_id"]}
        if note:
            payload["body"] = note
        if verdict == "changes":
            conn.execute(
                "UPDATE receipts SET superseded_at=? WHERE receipt_id=?",
                (stamp, rev["receipt_id"]))
            conn.execute(
                "UPDATE items SET status='working', updated_at=? "
                "WHERE id=?", (stamp, item["id"]))
            append_event(
                conn, item_id=item["id"], event_type="receipt_superseded",
                actor_agent_id=actor, actor_session_id=session_id,
                claim_id=claim_id, fencing_token=claim["fencing_token"],
                payload={"receipt_id": rev["receipt_id"],
                         "item_id": item["id"], "review_id": review_id,
                         "reason": "changes"})
        event_id = append_event(
            conn, item_id=item["id"], event_type="review_resolved",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload=payload)
        owner = item["owner_agent_id"]
        if owner and owner != actor:
            deliver(conn, recipient=owner, source_event_id=event_id,
                    item_id=item["id"], category="review_verdict",
                    payload={"review_id": review_id, "item_id": item["id"],
                             "verdict": verdict,
                             **({"body": note} if note else {})})
        return {"review_id": review_id, "item_id": item["id"],
                "verdict": verdict}

    return _in_mutate(conn, _submit)

def record_decision(conn, *, claim_id, session_id, actor, text,
                    rationale=None):
    """Claim-bound append-only decision: the validated
    implementation claim resolves the item — the CLI takes no item id —
    and the row is immutable once written. Legal while the item is working
    or review. Delivery goes to the live review's claimant-or-designated
    reviewer when one exists and differs from the actor; otherwise the
    event is the whole record (the decider is the owner by construction)."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidTransition(
            "a decision requires non-empty --text",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_decision_text"},
        )
    note = rationale.strip() if isinstance(rationale, str) and \
        rationale.strip() else None

    def _record(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        if item["status"] not in ("working", "review"):
            raise InvalidTransition(
                f"item {item['id']} is {item['status']}; decisions are "
                "recorded while working or under review only",
                reason_code="transition_not_available",
                evidence={
                    "item_id": item["id"],
                    "current_state": item["status"],
                    "allowed_states": ["working", "review"],
                },
            )
        stamp = now()
        cur = conn.execute(
            "INSERT INTO decisions(item_id,text,rationale,decided_by,"
            "decided_by_agent,decided_by_session,claim_id,fencing_token,"
            "legacy,created_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
            (item["id"], text.strip(), note, actor, actor, session_id,
             claim_id, claim["fencing_token"], stamp))
        decision_id = cur.lastrowid
        event_id = append_event(
            conn, item_id=item["id"], event_type="decision_recorded",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload={"decision_id": decision_id, "item_id": item["id"],
                     "text": text.strip()})
        live = _live_review(conn, item["id"])
        if live is not None:
            holder = conn.execute(
                "SELECT claimed_by_agent FROM claims WHERE lane_key=? AND "
                "status='active'", (f"review:{live['id']}",)).fetchone()
            recipient = holder["claimed_by_agent"] if holder \
                else live["reviewer"]
            if recipient is not None and recipient != actor:
                deliver(conn, recipient=recipient, source_event_id=event_id,
                        item_id=item["id"], category="decision",
                        payload={"decision_id": decision_id,
                                 "item_id": item["id"],
                                 "review_id": live["id"],
                                 "text": text.strip()})
        return decision_id

    return mutate(conn, _record)

# --- guarded completion -----------------------------------------------------
# The only licensed `done` writer. Eight steps, one transaction; file
# evidence failures take the recorded path — supersede + event COMMIT, the
# core RETURNS a failed outcome, and only the command layer raises
# `receipt_hash_mismatch`. Everything else is full rollback.

def _completion_evidence_failures(conn, item, receipt):
    """Collect-then-classify: re-hash the primary
    receipt file, re-check every board reference, re-hash every file
    reference. Returns ALL failures of both kinds; mutates nothing."""
    failures = []

    def _file_check(path, expected, role):
        try:
            data = pathlib.Path(path).read_bytes()
        except OSError:
            failures.append({
                "kind": "file", "failure": "missing", "path": path,
                "role": role,
                "detail": f"{role} file {path!r} is no longer readable"})
            return
        if hashlib.sha256(data).hexdigest() != expected:
            failures.append({
                "kind": "file", "failure": "changed", "path": path,
                "role": role,
                "detail": f"{role} file {path!r} no longer matches its "
                          "recorded hash"})

    _file_check(receipt["source_path"], receipt["sha256"], "receipt")
    for ref in json.loads(receipt["proof_references_json"]):
        if ref["type"] == "file":
            _file_check(ref["path"], ref["sha256"], "reference")
            continue
        table, pk = _REF_TABLES[ref["type"]]
        row = conn.execute(
            f"SELECT item_id FROM {table} WHERE {pk}=?",
            (ref["id"],)).fetchone()
        label = f"{ref['type']}:{ref['id']}"
        if row is None:
            failures.append({
                "kind": "board", "failure": "missing", "ref": label,
                "detail": f"{label} no longer exists on this board"})
        elif row["item_id"] != item["id"]:
            failures.append({
                "kind": "board", "failure": "unattached", "ref": label,
                "detail": f"{label} is no longer attached to item "
                          f"{item['id']}"})
    return failures

def _approval_dead_by_decision(conn, item_id, review_id):
    """Derivation at completion: an approval is fenced dead when
    any binding decision event postdates its resolving verdict event, by
    event order (`events.event_id`, the board's global sequencer)."""
    verdict_eid = None
    for row in conn.execute(
            "SELECT event_id, payload_json FROM events WHERE item_id=? AND "
            "event_type='review_resolved'", (item_id,)):
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            continue
        if payload.get("review_id") == review_id:
            verdict_eid = row["event_id"] if verdict_eid is None \
                else max(verdict_eid, row["event_id"])
    if verdict_eid is None:
        return False
    newest_decision = conn.execute(
        "SELECT MAX(event_id) AS m FROM events WHERE item_id=? AND "
        "event_type='decision_recorded'", (item_id,)).fetchone()["m"]
    return newest_decision is not None and newest_decision > verdict_eid

def _agent_provider_bucket(conn, agent_id):
    """Provider identity for independence checks. Unknown/null provider
    falls back to the agent_id so two unbound agents never count as one
    provider by accident (binding-second-review plan D1)."""
    row = conn.execute(
        "SELECT provider FROM agents WHERE name=?", (agent_id,)).fetchone()
    if row is not None and isinstance(row["provider"], str) \
            and row["provider"].strip():
        return row["provider"].strip()
    return agent_id

def _distinct_board_providers(conn):
    """Registered non-human provider identities (diagnostic only).

    Provider topology no longer changes an item's acceptance contract.  Keep
    this read helper for diagnostics and compatibility with older callers.
    """
    return {
        r["provider"] for r in conn.execute(
            "SELECT DISTINCT provider FROM agents WHERE name!='human' AND "
            "provider IS NOT NULL AND TRIM(provider)!=''")
    }

def _normalize_review_quorum(value, *, default=None):
    """Validate the deliberately small binding-review policy surface."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) \
            or value not in (1, 2):
        raise IncompleteContract(
            "review_quorum must be 1 or 2",
            reason_code="input_invalid",
            evidence={
                "field": "review_quorum",
                "constraint": "valid_review_quorum",
            },
        )
    return value


def review_quorum(conn, item_id):
    """Return the item's explicit binding approval count.

    The policy is carried by the existing append-only item-created/revised
    events, avoiding a schema migration.  Legacy required-review items that
    predate this field get the stable product default of one.  A recorded
    waiver is zero.  Provider registration never silently changes the result.
    """
    item = _item_row(conn, item_id)
    if item["review_required"] != 1:
        return 0
    for row in conn.execute(
            "SELECT payload_json FROM events WHERE item_id=? AND "
            "event_type IN ('item_created','item_revised') "
            "ORDER BY event_id DESC", (item_id,)):
        try:
            value = json.loads(row["payload_json"]).get("review_quorum")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(value, int) and not isinstance(value, bool) \
                and value in (1, 2):
            return value
    return 1


def min_approving_providers(conn, item_id=None):
    """Compatibility name for the old topology-derived policy.

    New code should call :func:`review_quorum` with an item.  With no item,
    return the stable default instead of inspecting how many providers happen
    to be registered.
    """
    return review_quorum(conn, item_id) if item_id is not None else 1

def _qualifying_approvals(conn, item, current, completing_actor):
    """Approved reviews on the current receipt + contract_version that are
    not dead-by-decision. Returns (independent_rows, provider_set,
    self_authored_rows)."""
    rows = conn.execute(
        "SELECT * FROM reviews WHERE item_id=? AND status='approved' AND "
        "receipt_id=? AND contract_version=? ORDER BY id",
        (item["id"], current["receipt_id"],
         item["contract_version"])).fetchall()
    live = [
        r for r in rows
        if not _approval_dead_by_decision(conn, item["id"], r["id"])
    ]
    self_authored = [
        r for r in live if r["reviewer_agent_id"] == completing_actor]
    independent = [
        r for r in live if r["reviewer_agent_id"] != completing_actor]
    providers = {
        _agent_provider_bucket(conn, r["reviewer_agent_id"])
        for r in independent
        if r["reviewer_agent_id"]
    }
    return independent, providers, self_authored

def _current_approval_providers(conn, item_id):
    """Provider buckets already represented by qualifying approvals on the
    item's current receipt and contract version."""
    item = _item_row(conn, item_id)
    current = _current_receipt(conn, item_id)
    if current is None:
        return set()
    _independent, providers, _self_authored = _qualifying_approvals(
        conn, item, current, item["owner_agent_id"] or "")
    return providers

def _remaining_review_provider_buckets(conn, item_id):
    """Reachable provider buckets among registered non-owner agents.

    Only provider-bound identities are capacity. An unbound agent may later
    become capacity when launched, but must not make a review look claimable
    before that happens.
    """
    item = _item_row(conn, item_id)
    owner = item["owner_agent_id"]
    already = _current_approval_providers(conn, item_id)
    remaining = set()
    for row in conn.execute("SELECT name, provider FROM agents"):
        name = row["name"]
        if name == owner or name == "human":
            continue
        bucket = (
            row["provider"].strip()
            if isinstance(row["provider"], str) and row["provider"].strip()
            else None
        )
        if bucket and bucket not in already:
            remaining.add(bucket)
    return remaining


def second_reviewer_blocked(conn, item_id):
    """Whether further review requests cannot expand the approval set.

    True when the current receipt still needs distinct-provider approvals but
    no registered, provider-bound non-owner bucket remains unused.
    """
    still = approvals_still_needed(conn, item_id)
    if still <= 0:
        return {
            "blocked": False,
            "reason_code": None,
            "still_needed": 0,
            "approved_providers": sorted(_current_approval_providers(conn, item_id)),
            "remaining_providers": sorted(
                _remaining_review_provider_buckets(conn, item_id)),
        }
    approved = _current_approval_providers(conn, item_id)
    remaining = _remaining_review_provider_buckets(conn, item_id)
    if not remaining:
        return {
            "blocked": True,
            "reason_code": "second_reviewer_not_selected",
            "still_needed": still,
            "approved_providers": sorted(approved),
            "remaining_providers": [],
        }
    return {
        "blocked": False,
        "reason_code": None,
        "still_needed": still,
        "approved_providers": sorted(approved),
        "remaining_providers": sorted(remaining),
    }


def approvals_still_needed(conn, item_id):
    """How many additional distinct reviewing providers are still required
    for the item's current receipt (0 when met or review waived). Used by
    status/next_action and prompts — never mutates."""
    item = _item_row(conn, item_id)
    if item["review_required"] != 1 and item["status"] != "review":
        return 0
    needed = review_quorum(conn, item_id)
    # Asking for a voluntary review makes that one review binding even when
    # the item was originally waived.
    if item["status"] == "review" and needed == 0:
        needed = 1
    current = _current_receipt(conn, item_id)
    if current is None:
        return needed
    owner = item["owner_agent_id"]
    _ind, providers, _self = _qualifying_approvals(
        conn, item, current, completing_actor=owner or "")
    return max(0, needed - len(providers))

def complete_item(conn, *, claim_id, session_id, actor):
    """The completion gate, one transaction: validate the
    implementation claim -> complete contract -> current receipt at the
    item's contract version -> collect-then-classify evidence pass before
    any mutation -> qualifying approval(s) (or the recorded waiver from
    working) -> no open question, no pending handoff -> claim completed,
    item done, event, delivery to the creator. Returns
    {"completed": item_id, "event_id": id} or the recorded failure
    {"failed": "receipt_hash_mismatch", ...} — only coop.py raises it.

    The item's explicit review_quorum controls how many distinct providers
    must approve the current receipt; provider registration never changes the
    contract implicitly.  No approval may be authored by the completing
    owner."""
    def _complete(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        if contract_incomplete(item):
            raise IncompleteContract(
                f"item {item['id']} has an incomplete contract; completion "
                "requires every contract field (revise the contract first)",
                reason_code="contract_incomplete",
                evidence={
                    "item_id": item["id"],
                    "constraint": "complete_contract_required",
                },
            )
        acceptance = contract_acceptance(conn, item["id"])
        if acceptance["required"] and acceptance["state"] != "accepted":
            raise InvalidTransition(
                f"item {item['id']} agent-authored contract is "
                f"{acceptance['state']}; peer acceptance is required",
                reason_code="contract_acceptance_required",
                evidence={
                    "item_id": item["id"],
                    "current_state": acceptance["state"],
                    "required_state": "accepted",
                },
            )
        current = _current_receipt(conn, item["id"])
        if current is None:
            raise ReceiptMissing(
                f"item {item['id']} has no current receipt; submit evidence "
                "before completing",
                reason_code="receipt_missing",
                evidence={
                    "item_id": item["id"],
                    "claim_id": claim_id,
                    "constraint": "current_receipt_required",
                },
            )
        if current["contract_version"] != item["contract_version"]:
            raise ReceiptStale(
                f"receipt {current['receipt_id']} binds contract version "
                f"{current['contract_version']}; the item is at version "
                f"{item['contract_version']}",
                reason_code="receipt_stale",
                evidence={
                    "item_id": item["id"],
                    "receipt_id": current["receipt_id"],
                    "contract_version": current["contract_version"],
                    "required_contract_version": item["contract_version"],
                },
            )
        failures = _completion_evidence_failures(conn, item, current)
        if any(f["kind"] == "file" for f in failures):
            # The recorded path: file evidence changed after submission is
            # a protocol fact the audit trail carries — supersede + event
            # commit, the failure returns (never raises inside mutate).
            stamp = now()
            conn.execute(
                "UPDATE receipts SET superseded_at=? WHERE receipt_id=?",
                (stamp, current["receipt_id"]))
            append_event(
                conn, item_id=item["id"], event_type="receipt_superseded",
                actor_agent_id=actor, actor_session_id=session_id,
                claim_id=claim_id, fencing_token=claim["fencing_token"],
                payload={"receipt_id": current["receipt_id"],
                         "item_id": item["id"],
                         "reason": "receipt_hash_mismatch",
                         "failures": failures})
            return {"failed": "receipt_hash_mismatch",
                    "completed_item_id": item["id"],
                    "superseded_receipt_id": current["receipt_id"],
                    "failures": failures}
        if failures:
            raise ProofReferenceInvalid(
                "completion re-lint failed: "
                + "; ".join(f["detail"] for f in failures),
                reason_code="proof_invalid",
                evidence={
                    "item_id": item["id"],
                    "receipt_id": current["receipt_id"],
                    "constraint": "proof_reference_invalid",
                },
            )
        if item["review_required"] == 1 or item["status"] == "review":
            _independent, providers, self_authored = _qualifying_approvals(
                conn, item, current, actor)
            if self_authored:
                self_authored_review = self_authored[0]
                raise SelfReview(
                    f"a qualifying approval on item {item['id']} was "
                    f"authored by {actor!r}; the completing actor must "
                    "differ from every approving reviewer",
                    reason_code="reviewer_is_owner",
                    evidence={
                        "item_id": item["id"],
                        "review_id": self_authored_review["id"],
                        "actor_agent_id": actor,
                        "owner_agent_id": item["owner_agent_id"],
                    },
                )
            needed = review_quorum(conn, item["id"])
            if item["status"] == "review" and needed == 0:
                needed = 1
            if len(providers) < needed:
                have = len(providers)
                who = ", ".join(sorted(providers)) if providers else "none"
                raise ReviewMissing(
                    f"item {item['id']} needs {needed} approve(s) from "
                    f"distinct providers on its current receipt; have "
                    f"{have} ({who}); request another review from a "
                    "different provider",
                    reason_code="review_missing",
                    evidence={
                        "item_id": item["id"],
                        "required_count": needed,
                        "observed_count": have,
                    },
                )
        else:
            waiver = item["review_waiver_reason"]
            if not isinstance(waiver, str) or not waiver.strip():
                raise ReviewMissing(
                    f"item {item['id']} waives review but records no "
                    "waiver reason; completion requires the recorded "
                    "reason",
                    reason_code="review_missing",
                    evidence={
                        "item_id": item["id"],
                        "constraint": "review_waiver_reason_required",
                    },
                )
        open_q = conn.execute(
            "SELECT question_id FROM questions WHERE item_id=? AND "
            "status='open' ORDER BY question_id LIMIT 1",
            (item["id"],)).fetchone()
        if open_q is not None:
            raise InvalidTransition(
                f"item {item['id']} has open question "
                f"{open_q['question_id']}; completion waits for the answer",
                reason_code="blocking_work_open",
                evidence={
                    "item_id": item["id"],
                    "blocking_object_type": "question",
                    "blocking_ids": [open_q["question_id"]],
                },
            )
        pending = conn.execute(
            "SELECT handoff_id FROM handoffs WHERE item_id=? AND "
            "status='pending' ORDER BY handoff_id LIMIT 1",
            (item["id"],)).fetchone()
        if pending is not None:
            raise InvalidTransition(
                f"item {item['id']} has pending handoff "
                f"{pending['handoff_id']}; completion waits for accept or "
                "decline",
                reason_code="blocking_work_open",
                evidence={
                    "item_id": item["id"],
                    "blocking_object_type": "handoff",
                    "blocking_ids": [pending["handoff_id"]],
                },
            )
        stamp = now()
        conn.execute(
            "UPDATE claims SET status='completed', closed_at=?, "
            "close_reason='completion' WHERE claim_id=?", (stamp, claim_id))
        conn.execute(
            "UPDATE items SET status='done', updated_at=? WHERE id=?",
            (stamp, item["id"]))
        event_id = append_event(
            conn, item_id=item["id"], event_type="item_completed",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload={"item_id": item["id"],
                     "receipt_id": current["receipt_id"]})
        creator = item["created_by"]
        if creator and creator != actor:
            deliver(conn, recipient=creator, source_event_id=event_id,
                    item_id=item["id"], category="completion",
                    payload={"item_id": item["id"],
                             "receipt_id": current["receipt_id"]})
        return {"completed": item["id"], "event_id": event_id}

    return mutate(conn, _complete)

# --- structured handoffs ----------------------------------------------------
# Transfer only on acceptance: creation closes the originator's claim and
# freezes the lane; acceptance mints the fresh claim through the shared
# lane-history guard (the acceptor's intent is the guard's reclaim reason);
# decline hands the item back under the answered-question grace machinery.
# Nothing times a pending handoff out — an unresponsive target is a
# visible wedge, not a silent transfer.

def _handoff_row(conn, handoff_id):
    row = conn.execute(
        "SELECT * FROM handoffs WHERE handoff_id=?",
        (handoff_id,)).fetchone()
    if row is None:
        raise NotFound(
            f"no handoff {handoff_id}",
            reason_code="target_not_found",
            evidence={"handoff_id": handoff_id},
        )
    return row

def _respond_guard(conn, *, handoff_id, session_id, actor, verb):
    """Shared accept/decline entry: live agent session, pending row,
    addressed target only, and no active implementation claim beside the
    frozen lane (a corrupt state responses refuse to build on)."""
    session = _require_live_session(conn, session_id, actor)
    if session is None:
        raise HumanLaneViolation(
            f"the human interface cannot {verb} a handoff; the addressed "
            "agent responds from its own session",
            reason_code="human_lane_forbidden",
            evidence={"constraint": "addressed_agent_session_required"},
        )
    h = _handoff_row(conn, handoff_id)
    if h["status"] != "pending":
        raise InvalidTransition(
            f"handoff {handoff_id} is {h['status']}; only a pending "
            "handoff can be responded to",
            reason_code="transition_not_available",
            evidence={
                "item_id": h["item_id"],
                "handoff_id": handoff_id,
                "current_status": h["status"],
                "required_status": "pending",
            },
        )
    if actor != h["to_agent"]:
        raise AddressedTargetMismatch(
            f"handoff {handoff_id} is addressed to {h['to_agent']!r}, "
            f"not {actor!r}",
            reason_code="addressed_target_mismatch",
            evidence={
                "item_id": h["item_id"],
                "handoff_id": handoff_id,
                "actor_agent_id": actor,
                "required_agent_id": h["to_agent"],
            },
        )
    active = conn.execute(
        "SELECT claim_id FROM claims WHERE lane_key=? AND status='active' "
        "ORDER BY claim_id DESC LIMIT 1",
        (_implementation_lane(h["item_id"]),)).fetchone()
    if active is not None:
        raise InvalidTransition(
            f"item {h['item_id']} has active claim {active['claim_id']} "
            f"beside pending handoff {handoff_id}; run `coop admin "
            "release` to repair the lane first",
            reason_code="blocking_work_open",
            evidence={
                "item_id": h["item_id"],
                "handoff_id": handoff_id,
                "blocking_object_type": "claim",
                "blocking_ids": [active["claim_id"]],
            },
        )
    return h

def create_handoff(conn, *, claim_id, session_id, actor, to_agent, reason,
                   summary, completed, remaining, risks, next_action,
                   proof_refs):
    """One-transaction structured handoff: validate the implementation
    claim, refuse outside `working` (the review-state guard first), guard
    the target and the six text fields before any write, lint the
    mandatory proof references, then close the claim, insert the pending
    transfer, freeze the item, and deliver to the target. The owner is
    preserved — ownership moves only on acceptance."""
    fields = {"reason": reason, "summary": summary, "completed": completed,
              "remaining": remaining, "risks": risks,
              "next_action": next_action}
    for flag, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            raise InvalidTransition(
                "a handoff requires a non-empty "
                f"--{flag.replace('_', '-')}",
                reason_code="input_invalid",
                evidence={
                    "field": flag,
                    "constraint": "non_empty_handoff_field",
                },
            )
    refs = normalize_proof_refs(proof_refs)
    if not refs:
        raise ProofReferenceInvalid(
            "a handoff requires at least one proof reference "
            "(event:<id> is always available)",
            reason_code="proof_invalid",
            evidence={"constraint": "proof_reference_required"},
        )

    def _create(conn):
        claim = validate_claim(
            conn, claim_id=claim_id, session_id=session_id, actor=actor,
            expected_kind="implementation")
        item = _item_row(conn, claim["item_id"])
        _refuse_from_review(item, "handoff create")
        if item["status"] != "working":
            raise InvalidTransition(
                f"item {item['id']} is {item['status']}; a handoff leaves "
                "working only",
                reason_code="transition_not_available",
                evidence={
                    "item_id": item["id"],
                    "current_state": item["status"],
                    "required_state": "working",
                },
            )
        if to_agent == "human":
            raise HumanLaneViolation(
                "the reserved human actor can never hold a session, so a "
                "handoff to human could never be accepted; use needs-input "
                "to ask the human instead",
                reason_code="human_lane_forbidden",
                evidence={
                    "item_id": item["id"],
                    "target_agent_id": to_agent,
                    "constraint": "agent_handoff_target_required",
                },
            )
        if to_agent == actor:
            raise InvalidTransition(
                "a handoff cannot target its own author",
                reason_code="input_invalid",
                evidence={
                    "item_id": item["id"],
                    "actor_agent_id": actor,
                    "target_agent_id": to_agent,
                    "constraint": "distinct_handoff_target_required",
                },
            )
        if conn.execute("SELECT name FROM agents WHERE name=?",
                        (to_agent,)).fetchone() is None:
            raise NotFound(
                f"unknown handoff target {to_agent!r}; the target must be "
                "a registered agent",
                reason_code="target_not_found",
                evidence={
                    "item_id": item["id"],
                    "target_agent_id": to_agent,
                },
            )
        linted = lint_proof_refs(
            conn, item_id=item["id"], refs=proof_refs, phase="handoff")
        stamp = now()
        conn.execute(
            "UPDATE claims SET status='closed', closed_at=?, "
            "close_reason='handoff' WHERE claim_id=?", (stamp, claim_id))
        cur = conn.execute(
            "INSERT INTO handoffs(item_id,claim_id,from_agent,from_session,"
            "execution_fencing_token,to_agent,reason,summary,"
            "completed_work,remaining_work,risks,proof_references,"
            "suggested_next_action,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
            (item["id"], claim_id, actor, session_id,
             claim["fencing_token"], to_agent, reason.strip(),
             summary.strip(), completed.strip(), remaining.strip(),
             risks.strip(), json.dumps(linted), next_action.strip(),
             stamp))
        handoff_id = cur.lastrowid
        conn.execute(
            "UPDATE items SET status='handoff', next_actor_agent_id=?, "
            "updated_at=? WHERE id=?", (to_agent, stamp, item["id"]))
        event_id = append_event(
            conn, item_id=item["id"], event_type="handoff_created",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=claim["fencing_token"],
            payload={"item_id": item["id"], "handoff_id": handoff_id,
                     "to_agent": to_agent, "reason": reason.strip()})
        deliver(conn, recipient=to_agent, source_event_id=event_id,
                item_id=item["id"], category="handoff",
                payload={"handoff_id": handoff_id, "item_id": item["id"],
                         "from_agent": actor, "summary": summary.strip()})
        # Soft peer-online signal: a sleeping autonomous
        # peer keeps a running session, so False means the target has no
        # session at all and the handoff waits until one binds. Never a
        # guard — creation stays legal either way.
        target_live = conn.execute(
            "SELECT 1 FROM sessions WHERE agent_id=? AND status='running' "
            "LIMIT 1", (to_agent,)).fetchone() is not None
        return {"handoff_id": handoff_id, "item_id": item["id"],
                "target_session_live": target_live}

    return _in_mutate(conn, _create)

def accept_handoff(conn, *, handoff_id, session_id, actor, intent,
                   lease_seconds=None):
    """Acceptance is the transfer: the fresh implementation claim is
    minted through the shared lane-history guard — the lane's newest claim
    is closed/handoff, so the acceptor's intent doubles as the guard's
    reclaim reason (no second flag) — then ownership moves, the item
    returns to working, and the acquisition event records the route."""
    if not isinstance(intent, str) or not intent.strip():
        raise InvalidTransition(
            "accepting a handoff requires a non-empty --intent",
            reason_code="input_invalid",
            evidence={
                "handoff_id": handoff_id,
                "constraint": "non_empty_handoff_intent",
            },
        )
    lease = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds

    def _accept(conn):
        h = _respond_guard(conn, handoff_id=handoff_id,
                           session_id=session_id, actor=actor,
                           verb="accept")
        item = _item_row(conn, h["item_id"])
        lane = _implementation_lane(item["id"])
        _lane_history_guard(conn, lane=lane, reclaim_reason=intent.strip())
        claim_id, token, expires = _insert_claim(
            conn, item_id=item["id"], kind="implementation",
            subject_id=None, lane=lane, actor=actor,
            session_id=session_id, intent=intent.strip(),
            lease=lease)
        stamp = now()
        conn.execute(
            "UPDATE handoffs SET status='accepted', resolved_at=? "
            "WHERE handoff_id=?", (stamp, handoff_id))
        conn.execute(
            "UPDATE items SET status='working', owner_agent_id=?, "
            "next_actor_agent_id=?, preferred_resume_owner_agent_id=NULL, "
            "resume_grace_started_at=NULL, resume_grace_expires_at=NULL, "
            "updated_at=? WHERE id=?", (actor, actor, stamp, item["id"]))
        event_id = append_event(
            conn, item_id=item["id"], event_type="handoff_accepted",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=token,
            payload={"item_id": item["id"], "handoff_id": handoff_id,
                     "claim_id": claim_id, "lane": lane,
                     "via": "handoff_accept",
                     "reclaim_reason": intent.strip()})
        if h["from_agent"] != actor:
            deliver(conn, recipient=h["from_agent"],
                    source_event_id=event_id, item_id=item["id"],
                    category="handoff_accepted",
                    payload={"handoff_id": handoff_id,
                             "item_id": item["id"], "new_owner": actor})
        return {"handoff_id": handoff_id, "item_id": item["id"],
                "claim_id": claim_id, "lane": lane,
                "lease_expires_at": expires}

    return mutate(conn, _accept)

def decline_handoff(conn, *, handoff_id, session_id, actor, reason):
    """Decline hands the item back: the preserved owner becomes the
    preferred resume owner under the answered-question grace machinery,
    the item returns to working unclaimed, and resume or post-expiry
    takeover ride the ordinary claim paths."""
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidTransition(
            "declining a handoff requires a non-empty --reason",
            reason_code="input_invalid",
            evidence={
                "handoff_id": handoff_id,
                "constraint": "non_empty_handoff_decline_reason",
            },
        )

    def _decline(conn):
        h = _respond_guard(conn, handoff_id=handoff_id,
                           session_id=session_id, actor=actor,
                           verb="decline")
        item = _item_row(conn, h["item_id"])
        owner = item["owner_agent_id"]
        stamp = now()
        expires = _ts(DEFAULT_RESUME_GRACE_SECONDS)
        conn.execute(
            "UPDATE handoffs SET status='declined', resolved_at=? "
            "WHERE handoff_id=?", (stamp, handoff_id))
        conn.execute(
            "UPDATE items SET status='working', next_actor_agent_id=?, "
            "preferred_resume_owner_agent_id=?, resume_grace_started_at=?, "
            "resume_grace_expires_at=?, updated_at=? WHERE id=?",
            (owner, owner, stamp, expires, stamp, item["id"]))
        event_id = append_event(
            conn, item_id=item["id"], event_type="handoff_declined",
            actor_agent_id=actor, actor_session_id=session_id,
            payload={"item_id": item["id"], "handoff_id": handoff_id,
                     "reason": reason.strip(), "resume_owner": owner,
                     "grace_expires_at": expires})
        if owner and owner != actor:
            deliver(conn, recipient=owner, source_event_id=event_id,
                    item_id=item["id"], category="handoff_declined",
                    payload={"handoff_id": handoff_id,
                             "item_id": item["id"], "declined_by": actor,
                             "reason": reason.strip(),
                             "grace_expires_at": expires})
        return {"handoff_id": handoff_id, "item_id": item["id"],
                "resume_owner": owner, "grace_expires_at": expires}

    return mutate(conn, _decline)

# --- contract revision: the human lane --------------------------------------
# Revision is the trusted-local operator surface:
# it merges changed fields over the current contract, re-validates the
# MERGED result (the legacy unlock), bumps contract_version, and
# supersedes the current receipt so every in-flight evidence chain dies
# by derivation. It requires a quiescent item — live or unsafely-stale
# lanes refuse through the lane classifier.

_REVISE_FILE_KEYS = frozenset(CONTRACT_FIELDS) | {
    "review_waiver", "review_quorum"}

def _load_revision_file(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise IncompleteContract(f"could not read contract file: {exc}")
    except ValueError as exc:
        raise IncompleteContract(f"contract file is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise IncompleteContract("contract file must hold a JSON object")
    unknown = sorted(set(data) - _REVISE_FILE_KEYS)
    if unknown:
        raise IncompleteContract(
            "only contract fields are revisable; unknown or non-revisable "
            f"field(s): {', '.join(unknown)}")
    return data

def revise_item(conn, *, item_id, reason, fields, contract_path=None):
    """Revise one item's contract from the human lane. `fields` carries
    flag-level overrides (contract fields plus `review_waiver`,
    `review_quorum`, or `require_review`); `contract_path` carries file-level ones — flags
    win over the file, the file over the stored contract, matching
    `item create`'s precedence."""
    if os.environ.get("COOP_SESSION_ID"):
        raise HumanLaneViolation(
            "contract revision is the human lane and runs outside "
            "supervised sessions; unset COOP_SESSION_ID")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidTransition("item revise requires --reason")
    overrides = dict(_load_revision_file(contract_path)) if contract_path \
        else {}
    fields = dict(fields or {})
    require_review = bool(fields.pop("require_review", False))
    unknown = sorted(set(fields) - _REVISE_FILE_KEYS)
    if unknown:
        raise IncompleteContract(
            "only contract fields are revisable; unknown or non-revisable "
            f"field(s): {', '.join(unknown)}")
    overrides.update(fields)
    waiver_given = "review_waiver" in overrides
    waiver_reason = overrides.pop("review_waiver", None)
    quorum_given = "review_quorum" in overrides
    quorum_override = overrides.pop("review_quorum", None)
    if require_review and (waiver_given or quorum_given):
        raise IncompleteContract(
            "--require-review is mutually exclusive with a review waiver "
            "or explicit review_quorum")
    if waiver_given and quorum_given:
        raise IncompleteContract(
            "review_quorum and a review waiver are mutually exclusive")
    if waiver_given and (not isinstance(waiver_reason, str)
                         or not waiver_reason.strip()):
        raise IncompleteContract(
            "a review waiver requires a non-empty reason")
    if quorum_given:
        quorum_override = _normalize_review_quorum(quorum_override)

    def _revise(conn):
        row = _item_row(conn, item_id)
        if row["status"] == "done":
            raise InvalidTransition(
                f"item {item_id} is done; contracts of finished work are "
                "immutable — follow-up work is a new task")
        open_q = conn.execute(
            "SELECT question_id FROM questions WHERE item_id=? AND "
            "status='open' ORDER BY question_id DESC LIMIT 1",
            (item_id,)).fetchone()
        if open_q is not None:
            raise InvalidTransition(
                f"item {item_id} is waiting on open question "
                f"{open_q['question_id']}; answer or supersede it before "
                "revising the contract")
        pending_h = conn.execute(
            "SELECT handoff_id FROM handoffs WHERE item_id=? AND "
            "status='pending' ORDER BY handoff_id DESC LIMIT 1",
            (item_id,)).fetchone()
        if pending_h is not None:
            raise InvalidTransition(
                f"item {item_id} has pending handoff "
                f"{pending_h['handoff_id']}; the lane is frozen until it "
                "is accepted or declined")
        # Every lane with history goes through the lane classifier: a
        # live claim collides, an unsafely-stale lane refuses, deliberate
        # ends pass on the operator's reason.
        lanes = conn.execute(
            "SELECT DISTINCT lane_key FROM claims WHERE item_id=? "
            "ORDER BY lane_key", (item_id,)).fetchall()
        for lane in lanes:
            _lane_history_guard(conn, lane=lane["lane_key"],
                                reclaim_reason=reason.strip())
        current = {f: row[f] for f in CONTRACT_TEXT_FIELDS}
        for f in CONTRACT_LIST_FIELDS:
            parsed = _parse_list(row[f])
            current[f] = parsed if isinstance(parsed, list) else (
                [] if row[f] is None else [str(row[f])])
        merged = dict(current)
        merged.update(overrides)
        contract = _validated_contract(merged)
        old_version = row["contract_version"]
        new_version = old_version + 1
        delta = {}
        for f in CONTRACT_FIELDS:
            if contract[f] != current[f]:
                delta[f] = {"old": current[f], "new": contract[f]}
        new_required, new_waiver = row["review_required"], \
            row["review_waiver_reason"]
        old_quorum = review_quorum(conn, item_id)
        new_quorum = old_quorum
        if require_review:
            new_required, new_waiver = 1, None
            new_quorum = 1
        elif waiver_given:
            new_required, new_waiver = 0, waiver_reason.strip()
            new_quorum = 0
        elif quorum_given:
            new_required, new_waiver = 1, None
            new_quorum = quorum_override
        if (new_required, new_waiver) != (
                row["review_required"], row["review_waiver_reason"]):
            delta["review_waiver"] = {
                "old": row["review_waiver_reason"],
                "new": new_waiver}
        if new_quorum != old_quorum:
            delta["review_quorum"] = {
                "old": old_quorum, "new": new_quorum}
        stamp = now()
        receipt = _current_receipt(conn, item_id)
        if receipt is not None:
            conn.execute(
                "UPDATE receipts SET superseded_at=? WHERE receipt_id=?",
                (stamp, receipt["receipt_id"]))
            append_event(
                conn, item_id=item_id, event_type="receipt_superseded",
                actor_agent_id="human", actor_session_id=None,
                payload={"receipt_id": receipt["receipt_id"],
                         "item_id": item_id, "reason": "revision"})
        new_status = "working" if row["status"] == "review" else \
            row["status"]
        conn.execute(
            "UPDATE items SET title=?, objective=?, scope=?, done_when=?, "
            "output_contract=?, context=?, allowed_actions=?, "
            "stop_conditions=?, review_required=?, review_waiver_reason=?, "
            "contract_version=?, status=?, updated_at=? WHERE id=?",
            (contract["title"], contract["objective"], contract["scope"],
             contract["done_when"], contract["output_contract"],
             contract["context"],
             json.dumps(contract["allowed_actions"], separators=(",", ":")),
             json.dumps(contract["stop_conditions"], separators=(",", ":")),
             new_required, new_waiver, new_version, new_status, stamp,
             item_id))
        event_id = append_event(
            conn, item_id=item_id, event_type="item_revised",
            actor_agent_id="human", actor_session_id=None,
            payload={"item_id": item_id, "old_version": old_version,
                      "new_version": new_version, "reason": reason.strip(),
                      "delta": delta, "review_quorum": new_quorum})
        if row["owner_agent_id"]:
            deliver(conn, recipient=row["owner_agent_id"],
                    source_event_id=event_id, item_id=item_id,
                    category="revision",
                    payload={"item_id": item_id,
                             "new_version": new_version,
                             "reason": reason.strip()})
        return {"item_id": item_id, "contract_version": new_version}

    return mutate(conn, _revise)

# --- boot-panel status and executable next action ---------------------------
# The status surface is a deterministic query, not an LLM. Labels are derived
# at read time; next_action selects the exact legal target/command while the
# external watcher decides only when to wake a provider.

def _newest_lane_stale(conn, agent, item_id=None):
    item_clause = "" if item_id is None else " AND c.item_id=?"
    params = (agent,) if item_id is None else (agent, item_id)
    return conn.execute(
        "SELECT * FROM claims c WHERE claimed_by_agent=? AND "
        "(status='stale' OR (status='active' AND EXISTS(SELECT 1 FROM "
        "sessions s WHERE s.session_id=c.owner_session_id AND "
        "s.status!='running'))) AND claim_id=(SELECT MAX(claim_id) FROM claims "
        "WHERE lane_key=c.lane_key)" + item_clause +
        " ORDER BY claim_id", params).fetchall()

def _session_terminal(conn, session_id):
    row = conn.execute(
        "SELECT status FROM sessions WHERE session_id=?",
        (session_id,)).fetchone()
    return row is not None and row["status"] != "running"

def _lane_subject_reclaimable(conn, row, agent):
    """Reclaiming a lane is only 'actual' when its subject still admits
    the claim: items always route through claim_item's own checks; an
    answered question refuses resurrection; a dead-by-derivation review
    (or one the agent is no longer eligible for) refuses review_stale."""
    lane = row["lane_key"]
    if lane.startswith("question_response:"):
        q = conn.execute(
            "SELECT status, assigned_to_agent FROM questions WHERE "
            "question_id=?", (int(lane.split(":", 1)[1]),)).fetchone()
        return (q is not None and q["status"] == "open"
                and q["assigned_to_agent"] == agent)
    if lane.startswith("review:"):
        review_id = int(lane.split(":", 1)[1])
        r = conn.execute(
            "SELECT r.*, i.owner_agent_id AS owner, i.contract_version AS "
            "item_version FROM reviews r JOIN items i ON i.id=r.item_id "
            "WHERE r.id=?", (review_id,)).fetchone()
        if (r is None or r["status"] != "requested"
                or r["resolved_at"] is not None or r["owner"] == agent
                or (r["reviewer"] is not None and r["reviewer"] != agent)):
            return False
        current = _current_receipt(conn, r["item_id"])
        return (current is not None
                and r["receipt_id"] == current["receipt_id"]
                and r["contract_version"] == r["item_version"])
    return True

# A routed command is executed verbatim by an agent whose cwd is the
# WORKSPACE, which on a foreign repo holds no Co-op files. `-m` resolves
# wherever agent_coop is importable, which coop_start guarantees by binding
# the install root into each turn's PYTHONPATH. A bare relative `coop.py`
# only worked while the workspace and the install directory were the same
# path.
# Board-routed commands pin the interpreter that loaded Agent Co-op. This is
# reliable in pipx environments and on Linux hosts that provide ``python3``
# but no bare ``python`` alias.
CLI_ARGV = (sys.executable, "-m", "agent_coop")

def _action(kind, *, target_type=None, target_id=None, item_id=None,
            claim_id=None, lease_seconds=None, command=None,
            required_inputs=(), choices=()):
    """Stable executable next-action envelope.

    `command` is argv, never prose.  A placeholder in a command is named in
    `required_inputs`; `choices` enumerates the few transitions that genuinely
    require agent judgment (for example accepting versus declining a handoff).
    """
    return {
        "kind": kind,
        "target_type": target_type,
        "target_id": target_id,
        "item_id": item_id,
        "claim_id": claim_id,
        "lease_seconds": lease_seconds,
        "command": command,
        "required_inputs": list(required_inputs),
        "choices": list(choices),
    }


def _claim_command(noun, target_id, *, intent, lease_seconds, history=None):
    command = [*CLI_ARGV, noun, "claim", str(target_id),
               "--intent", intent]
    if history is not None:
        command.extend([
            "--reclaim", "--reason",
            f"resume safe prior {history['claim_id']} on this lane",
        ])
    command.extend(["--lease-seconds", str(lease_seconds)])
    return command


def _recovery_action(row, lease_seconds):
    if row["claim_kind"] == "implementation":
        noun, subject = "item", row["item_id"]
    elif row["claim_kind"] == "review":
        noun, subject = "review", row["subject_id"]
    else:
        noun, subject = "question", row["subject_id"]
    return _action(
        "recover_claim", target_type="claim", target_id=row["claim_id"],
        item_id=row["item_id"], claim_id=row["claim_id"],
        lease_seconds=lease_seconds,
        command=_claim_command(
            noun, subject, intent=f"recover {noun} {subject}",
            lease_seconds=lease_seconds, history=row))


def _next_huddle_action(conn, agent, item_id=None):
    """Return this participant's next bounded huddle transition, if any."""
    item_clause = "" if item_id is None else " AND item_id=?"
    params = () if item_id is None else (item_id,)
    for row in conn.execute(
            "SELECT id, item_id FROM debates WHERE status='open'" +
            item_clause + " ORDER BY id", params):
        _debate, meta = _huddle(conn, row["id"])
        participants = meta.get("participants") or []
        if agent not in participants:
            continue
        latest = _huddle_latest_posts(conn, row["id"])
        current_round = max(
            (post["round"] for post in latest.values()), default=1)
        posted_current = {
            name for name, post in latest.items()
            if post["round"] == current_round}
        all_current = all(name in posted_current for name in participants)
        owner = meta.get("owner")
        peers = [name for name in participants if name != owner]

        kind = meta.get("kind") or "contract_acceptance"
        accept_summary = (
            "peer concerns resolved; plan concurred"
            if kind == "implementation_plan" else
            "peer concerns resolved; contract accepted")
        if all_current and all(
                latest[name]["stance"] == "support" for name in peers):
            if agent != owner and latest[agent]["stance"] == "support":
                return _action(
                    "huddle_close", target_type="huddle",
                    target_id=row["id"], item_id=row["item_id"],
                    command=[
                        *CLI_ARGV, "huddle", "close",
                        str(row["id"]), "--outcome", "accepted",
                        "--summary", accept_summary,
                    ])
            continue

        if all_current and current_round >= int(meta.get("max_rounds", 2)):
            if agent != owner and latest[agent]["stance"] == "concern":
                return _action(
                    "huddle_close", target_type="huddle",
                    target_id=row["id"], item_id=row["item_id"],
                    command=[
                        *CLI_ARGV, "huddle", "close",
                        str(row["id"]), "--outcome", "changes",
                        "--summary", "{summary}",
                    ], required_inputs=("summary",))
            continue

        next_round = current_round + 1 if all_current else current_round
        already_posted = conn.execute(
            "SELECT 1 FROM debate_posts WHERE debate_id=? AND agent=? AND "
            "round=?", (row["id"], agent, next_round)).fetchone()
        if already_posted is None:
            return _action(
                "huddle_post", target_type="huddle", target_id=row["id"],
                item_id=row["item_id"],
                command=[
                    *CLI_ARGV, "huddle", "post", str(row["id"]),
                    "--stance", "{stance}", "--body", "{body}",
                ], required_inputs=("stance", "body"))
    return None


def _derive_next_action(conn, agent, now_ts,
                        lease_seconds=DEFAULT_ACTION_LEASE_SECONDS,
                        item_id=None):
    """Return one blocking-first executable protocol action plus warnings.

    Code chooses the target, legal transition, and lease.  The agent supplies
    only irreducible judgment inputs.  Unsafe lanes demote to warnings and are
    never recommended.
    """
    if isinstance(lease_seconds, bool) or not isinstance(
            lease_seconds, (int, float)) or lease_seconds <= 0:
        raise InvalidTiming("next-action lease_seconds must be positive")
    lease = max(1, int(lease_seconds))
    warnings = []

    def _effectively_stale(row):
        # An expired-active row classifies as stale regardless of whether
        # a sweep ran (the flip-then-classify rule).
        return row is not None and (
            row["status"] == "stale"
            or (row["status"] == "active"
                and row["lease_expires_at"] <= now_ts))

    answer = None
    question_clause = "" if item_id is None else " AND item_id=?"
    question_params = ((agent,) if item_id is None
                       else (agent, item_id))
    for q in conn.execute(
            "SELECT question_id, item_id FROM questions WHERE assigned_to_agent=? "
            "AND status='open'" + question_clause +
            " ORDER BY question_id", question_params):
        claimable, unsafe, row = _lane_state(
            conn, f"question_response:{q['question_id']}", now_ts)
        if claimable and not (_effectively_stale(row)
                              and row["claimed_by_agent"] == agent):
            # never claimed, deliberately ended, or another agent's safe
            # history — answering is claimable right now
            answer = (q, row, False)
            break
        elif (row is not None and row["status"] == "active"
                and row["lease_expires_at"] > now_ts
                and row["claimed_by_agent"] == agent):
            answer = (q, row, True)
            break
        elif unsafe:
            warnings.append(
                f"question {q['question_id']} awaits you but its response "
                f"lane is unsafely stale (session "
                f"{row['owner_session_id']!r} still reports running); "
                "wait for confirmed exit or use peer recovery after the "
                "abandon horizon")
        # my own safely-stale lane falls through to recover_claim
    if answer is not None:
        q, history, already_held = answer
        if already_held:
            return _action(
                "answer_question", target_type="question",
                target_id=q["question_id"], item_id=q["item_id"],
                claim_id=history["claim_id"],
                command=[*CLI_ARGV, "question", "answer",
                         "--claim", str(history["claim_id"]),
                         "--answer", "{answer}"],
                required_inputs=("answer",)), warnings
        return _action(
            "answer_question", target_type="question",
            target_id=q["question_id"], item_id=q["item_id"],
            lease_seconds=lease,
            command=_claim_command(
                "question", q["question_id"],
                intent=f"answer question {q['question_id']}",
                lease_seconds=lease, history=history)), warnings
    handoff_clause = "" if item_id is None else " AND item_id=?"
    handoff_params = ((agent,) if item_id is None
                      else (agent, item_id))
    pending_handoff = conn.execute(
        "SELECT handoff_id, item_id FROM handoffs WHERE status='pending' "
        "AND to_agent=?" + handoff_clause +
        " ORDER BY handoff_id LIMIT 1", handoff_params).fetchone()
    review_entry = None
    for _, review_id, entry in _queue_reviews(
            conn, agent, now_ts, item_id=item_id):
        _, _, newest = _lane_state(conn, f"review:{review_id}", now_ts)
        if _effectively_stale(newest) \
                and newest["claimed_by_agent"] == agent:
            continue  # my own safely-stale lane is recover_claim's
        review_entry = (entry, newest)
        break
    unsafe_review_clause = "" if item_id is None else " AND r.item_id=?"
    unsafe_review_params = ((agent, agent) if item_id is None
                            else (agent, agent, item_id))
    for r in conn.execute(
            "SELECT r.id FROM reviews r JOIN items i ON i.id=r.item_id "
            "WHERE r.status='requested' AND r.resolved_at IS NULL AND "
            "i.owner_agent_id != ? AND (r.reviewer IS NULL OR r.reviewer=?) "
            + unsafe_review_clause + " ORDER BY r.id",
            unsafe_review_params):
        _, unsafe, row = _lane_state(conn, f"review:{r['id']}", now_ts)
        if unsafe and _lane_subject_reclaimable(
                conn, {"lane_key": f"review:{r['id']}"}, agent):
            warnings.append(
                f"review {r['id']} awaits you but its lane is unsafely "
                f"stale (session {row['owner_session_id']!r} still reports "
                "running); it is a warning, not an action")
    if pending_handoff:
        handoff_id = pending_handoff["handoff_id"]
        handoff_item_id = pending_handoff["item_id"]
        return _action(
            "respond_handoff", target_type="handoff",
            target_id=handoff_id, item_id=handoff_item_id,
            required_inputs=("handoff_response",), choices=(
                {
                    "kind": "accept_handoff",
                    "command": [
                        *CLI_ARGV, "handoff", "accept", "--id",
                        str(handoff_id), "--intent", f"accept handoff {handoff_id}",
                        "--lease-seconds", str(lease),
                    ],
                    "required_inputs": [],
                },
                {
                    "kind": "decline_handoff",
                    "command": [
                        *CLI_ARGV, "handoff", "decline", "--id",
                        str(handoff_id), "--reason", "{reason}",
                    ],
                    "required_inputs": ["reason"],
                },
            )), warnings
    huddle_action = _next_huddle_action(conn, agent, item_id=item_id)
    if huddle_action is not None:
        return huddle_action, warnings
    if review_entry is not None:
        entry, history = review_entry
        return _action(
            "review_task", target_type="review",
            target_id=entry["review_id"], item_id=entry["item_id"],
            lease_seconds=lease,
            command=_claim_command(
                "review", entry["review_id"],
                intent=f"review item {entry['item_id']}",
                lease_seconds=lease, history=history)), warnings
    resume_clause = "" if item_id is None else " AND id=?"
    resume_params = ((agent, now_ts) if item_id is None
                     else (agent, now_ts, item_id))
    resume = conn.execute(
            "SELECT id FROM items i WHERE preferred_resume_owner_agent_id=? "
            "AND status IN ('needs_input','working') AND "
            "resume_grace_expires_at > ? AND "
            "NOT EXISTS(SELECT 1 FROM questions q WHERE q.item_id=i.id AND "
            "q.status='open')" + resume_clause +
            " ORDER BY id LIMIT 1", resume_params).fetchone()
    if resume is not None:
        lane = _implementation_lane(resume["id"])
        _claimable, _unsafe, history = _lane_state(conn, lane, now_ts)
        return _action(
            "resume_task", target_type="item", target_id=resume["id"],
            item_id=resume["id"], lease_seconds=lease,
            command=_claim_command(
                "item", resume["id"], intent=f"resume item {resume['id']}",
                lease_seconds=lease, history=history)), warnings
    # Item-25 P0: after resume grace expires, claim_item still allows reasoned
    # reclaim, but routing used to emit idle for everyone. Split preferred
    # resume from peer reclaim so a lower-id foreign expired item never steals
    # precedence from this agent's own preferred resume_task.
    # Live grace is unchanged; grace columns clear only via claim_item.
    expired_scope = "" if item_id is None else " AND id=?"
    # 1) Preferred owner: post-expiry resume_task for *their* items only.
    expired_pref_params = ((agent, now_ts) if item_id is None
                           else (agent, now_ts, item_id))
    expired_pref = conn.execute(
            "SELECT id FROM items i WHERE preferred_resume_owner_agent_id=? "
            "AND status IN ('needs_input','working') AND "
            "resume_grace_expires_at IS NOT NULL AND "
            "resume_grace_expires_at <= ? AND "
            "NOT EXISTS(SELECT 1 FROM questions q WHERE q.item_id=i.id AND "
            "q.status='open')" + expired_scope +
            " ORDER BY id LIMIT 1", expired_pref_params).fetchone()
    if expired_pref is not None:
        lane = _implementation_lane(expired_pref["id"])
        _claimable, _unsafe, history = _lane_state(conn, lane, now_ts)
        return _action(
            "resume_task", target_type="item", target_id=expired_pref["id"],
            item_id=expired_pref["id"], lease_seconds=lease,
            command=_claim_command(
                "item", expired_pref["id"],
                intent=f"resume item {expired_pref['id']}",
                lease_seconds=lease, history=history)), warnings
    # Peer reclaim of *foreign* expired grace is deferred until after this
    # agent's own active claim work (continue/define/review/complete).
    active_clause = "" if item_id is None else " AND c.item_id=?"
    active_params = ((agent, now_ts) if item_id is None
                     else (agent, now_ts, item_id))
    active = conn.execute(
            "SELECT c.* FROM claims c WHERE claimed_by_agent=? AND "
            "c.status='active' AND "
            "c.claim_kind IN ('implementation','review') AND "
            "c.lease_expires_at > ? AND EXISTS(SELECT 1 FROM sessions s "
            "WHERE s.session_id=c.owner_session_id AND s.status='running')" +
            active_clause +
            " ORDER BY claim_id LIMIT 1", active_params).fetchone()
    if active is not None:
        if active["claim_kind"] == "review":
            review_id = active["subject_id"]
            return _action(
                "continue_task", target_type="review",
                target_id=review_id, item_id=active["item_id"],
                claim_id=active["claim_id"],
                required_inputs=("review_verdict",), choices=(
                    {
                        "kind": "approve_review",
                        "command": [
                            *CLI_ARGV, "review", "submit",
                            "--claim", str(active["claim_id"]),
                            "--verdict", "approve",
                        ],
                        "required_inputs": [],
                    },
                    {
                        "kind": "request_changes",
                        "command": [
                            *CLI_ARGV, "review", "submit",
                            "--claim", str(active["claim_id"]),
                            "--verdict", "changes", "--body", "{body}",
                        ],
                        "required_inputs": ["body"],
                    },
                )), warnings
        item = _item_row(conn, active["item_id"])
        if contract_incomplete(item):
            return _action(
                "define_contract", target_type="item",
                target_id=item["id"], item_id=item["id"],
                claim_id=active["claim_id"],
                command=[
                    *CLI_ARGV, "item", "define",
                    "--claim", str(active["claim_id"]),
                    "--scope", "{scope}", "--done-when", "{done_when}",
                    "--output-contract", "{output_contract}",
                    "--context", "{context}",
                    "--allowed-action", "{allowed_action}",
                    "--stop-condition", "{stop_condition}",
                ], required_inputs=(
                    "scope", "done_when", "output_contract", "context",
                    "allowed_action", "stop_condition")), warnings
        acceptance = contract_acceptance(conn, active["item_id"])
        if acceptance["required"] and acceptance["state"] != "accepted":
            if acceptance["state"] == "changes":
                return _action(
                    "refine_contract", target_type="item",
                    target_id=active["item_id"], item_id=active["item_id"],
                    claim_id=active["claim_id"],
                    command=[
                        *CLI_ARGV, "item", "refine",
                        "--claim", str(active["claim_id"]),
                        "--contract", "{contract_path}",
                    ], required_inputs=("contract_path",)), warnings
            if acceptance["huddle_id"] is None:
                return _action(
                    "open_huddle", target_type="item",
                    target_id=active["item_id"], item_id=active["item_id"],
                    claim_id=active["claim_id"],
                    command=[
                        *CLI_ARGV, "huddle", "open",
                        "--claim", str(active["claim_id"]),
                    ]), warnings
            # The owner has already contributed and now waits for peer posts or
            # closure.  Do not wake it into a no-op turn.
            return _action("idle"), warnings
        receipt = _current_receipt(conn, item["id"])
        if receipt is None:
            return _action(
                "continue_task", target_type="claim",
                target_id=active["claim_id"], item_id=item["id"],
                claim_id=active["claim_id"],
                command=[
                    *CLI_ARGV, "receipt", "submit",
                    "--claim", str(active["claim_id"]),
                    "--path", "{path}", "--summary", "{summary}",
                    "--proof", "{proof}", "--proof-ref", "{proof_ref}",
                ], required_inputs=(
                    "contract_work", "path", "summary", "proof",
                    "proof_ref")), warnings
        still_needed = approvals_still_needed(conn, item["id"])
        if still_needed > 0:
            block = second_reviewer_blocked(conn, item["id"])
            if block["blocked"]:
                warnings.append(
                    f"item {item['id']} needs {still_needed} more "
                    "distinct-provider approve(s) but no remaining "
                    "registered provider can expand the set "
                    f"(approved={block['approved_providers'] or []}); "
                    "idle — do not re-request review "
                    f"({block['reason_code']})")
                return _action("idle"), warnings
            if _live_review(conn, item["id"]) is None:
                return _action(
                    "request_review", target_type="item", target_id=item["id"],
                    item_id=item["id"], claim_id=active["claim_id"],
                    command=[
                        *CLI_ARGV, "review", "request",
                        "--claim", str(active["claim_id"]),
                    ]), warnings
            return _action("idle"), warnings
        return _action(
            "complete_task", target_type="item", target_id=item["id"],
            item_id=item["id"], claim_id=active["claim_id"],
            command=[*CLI_ARGV, "item", "complete", "--claim",
                     str(active["claim_id"])]), warnings
    for row in _newest_lane_stale(conn, agent, item_id=item_id):
        if _session_terminal(conn, row["owner_session_id"]) \
                and _lane_subject_reclaimable(conn, row, agent):
            return _recovery_action(row, lease), warnings
    released_clause = "" if item_id is None else " AND c.item_id=?"
    released_params = ((agent,) if item_id is None
                       else (agent, item_id))
    for row in conn.execute(
            "SELECT c.* FROM claims c JOIN events e ON e.claim_id=c.claim_id "
            "AND e.event_type='operator_release' WHERE "
            "c.claimed_by_agent=? AND c.status='released' AND "
            "c.claim_id=(SELECT MAX(claim_id) FROM claims WHERE "
            "lane_key=c.lane_key)" + released_clause,
            released_params):
        if _lane_subject_reclaimable(conn, row, agent):
            return _recovery_action(row, lease), warnings
    # Compatibility path for imported/migrated states that have an owner but
    # no live implementation claim. Normal product routing handles this above.
    owned_review_clause = "" if item_id is None else " AND id=?"
    owned_review_params = ((agent,) if item_id is None
                           else (agent, item_id))
    for row in conn.execute(
            "SELECT id FROM items WHERE owner_agent_id=? AND status='review' "
            + owned_review_clause + " ORDER BY id", owned_review_params):
        if approvals_still_needed(conn, row["id"]) > 0:
            block = second_reviewer_blocked(conn, row["id"])
            if block["blocked"]:
                warnings.append(
                    f"item {row['id']} needs "
                    f"{block['still_needed']} more distinct-provider "
                    "approve(s) but no remaining registered provider can "
                    f"expand the set (approved="
                    f"{block['approved_providers'] or []}); idle — do not "
                    f"re-request review ({block['reason_code']})")
                return _action("idle"), warnings
            if _live_review(conn, row["id"]) is not None:
                continue
            claim = conn.execute(
                "SELECT claim_id FROM claims WHERE item_id=? AND "
                "claim_kind='implementation' AND claimed_by_agent=? AND "
                "status='active' ORDER BY claim_id DESC LIMIT 1",
                (row["id"], agent)).fetchone()
            claim_id = claim["claim_id"] if claim else None
            return _action(
                "request_review", target_type="item", target_id=row["id"],
                item_id=row["id"], claim_id=claim_id,
                command=([*CLI_ARGV, "review", "request",
                          "--claim", str(claim_id)] if claim_id else None),
                required_inputs=(() if claim_id else
                                 ("active_implementation_claim",))), warnings
    # Ordinary claim_task tier: todo queue *and* non-owner expired-grace
    # takeovers compete at the same priority (lowest item id wins). Elevated
    # only after recover_claim / own active work / preferred resume.
    claim_candidates = []
    task = next((entry for entry in queue(
                     conn, for_agent=agent, item_id=item_id)
                 if entry["kind"] == "task"), None)
    if task is not None:
        claim_candidates.append(("todo", task["item_id"], task))
    if agent != "human":
        expired_peer_params = ((agent, now_ts) if item_id is None
                               else (agent, now_ts, item_id))
        expired_peer = conn.execute(
            "SELECT id FROM items i WHERE "
            "preferred_resume_owner_agent_id IS NOT NULL AND "
            "preferred_resume_owner_agent_id != ? AND "
            "status IN ('needs_input','working') AND "
            "resume_grace_expires_at IS NOT NULL AND "
            "resume_grace_expires_at <= ? AND "
            "NOT EXISTS(SELECT 1 FROM questions q WHERE q.item_id=i.id AND "
            "q.status='open')" + expired_scope +
            " ORDER BY id LIMIT 1", expired_peer_params).fetchone()
        if expired_peer is not None:
            claim_candidates.append(
                ("reclaim", expired_peer["id"], expired_peer))
    if claim_candidates:
        claim_candidates.sort(key=lambda c: c[1])  # ordinary claim by item id
        kind, task_item_id, payload = claim_candidates[0]
        if kind == "reclaim":
            cmd = [
                *CLI_ARGV, "item", "claim", str(task_item_id),
                "--intent",
                f"take over item {task_item_id} after resume grace",
                "--reclaim", "--reason", "{reason}",
                "--lease-seconds", str(lease),
            ]
            return _action(
                "claim_task", target_type="item", target_id=task_item_id,
                item_id=task_item_id, lease_seconds=lease,
                command=cmd, required_inputs=("reason",)), warnings
        _claimable, _unsafe, history = _lane_state(
            conn, _implementation_lane(task_item_id), now_ts)
        return _action(
            "claim_task", target_type="item", target_id=task_item_id,
            item_id=task_item_id, lease_seconds=lease,
            command=_claim_command(
                "item", task_item_id, intent=f"claim item {task_item_id}",
                lease_seconds=lease, history=history)), warnings
    return _action("idle"), warnings


def rejection_actions(conn, agent, *, item_id, lease_seconds=None):
    if item_id is None:
        return []
    lease = (
        DEFAULT_ACTION_LEASE_SECONDS
        if lease_seconds is None
        else lease_seconds
    )
    action, _warnings = _derive_next_action(
        conn,
        agent,
        now(),
        lease_seconds=lease,
        item_id=int(item_id),
    )
    return [action]


def status(conn, agent, *, session_id=None, checkpoint_limit_seconds=None,
           action_lease_seconds=None, item_id=None):
    """The boot panel: identity + liveness, live claims with checkpoint
    freshness, owned/addressed work with derived labels, unread counts by
    category (peek — the cursor is never consumed), grace state, stale
    visibility, warnings, and the executable next_action object."""
    if not valid_agent_name(agent):
        raise InvalidAgentName(f"invalid agent name: {agent!r}")
    session_id = session_id or None  # env empty-string normalization
    limit = (DEFAULT_CHECKPOINT_LIMIT_SECONDS
             if checkpoint_limit_seconds is None else checkpoint_limit_seconds)
    now_ts = now()
    horizon = _ts(-limit)
    warnings = []

    session = None
    if session_id:
        row = conn.execute(
            "SELECT session_id, status, provider, started_at, last_seen_at "
            "FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row:
            session = dict(row)

    claims = []
    claim_clause = "" if item_id is None else " AND item_id=?"
    claim_params = (agent,) if item_id is None else (agent, item_id)
    for row in conn.execute(
            "SELECT * FROM claims WHERE claimed_by_agent=? AND "
            "status='active'" + claim_clause +
            " ORDER BY claim_id", claim_params):
        progress_stale = row["last_checkpoint_at"] <= horizon
        claims.append({
            "claim_id": row["claim_id"], "item_id": row["item_id"],
            "lane": row["lane_key"], "intent": row["intent_note"],
            "lease_expires_at": row["lease_expires_at"],
            "last_checkpoint_at": row["last_checkpoint_at"],
            "progress_stale": progress_stale,
        })
        if progress_stale:
            warnings.append(
                f"claim {row['claim_id']} is progress_stale (no substantive "
                f"checkpoint since {row['last_checkpoint_at']}); the "
                "supervisor terminates quiet sessions — checkpoint now")

    stale = []
    for row in _newest_lane_stale(conn, agent, item_id=item_id):
        reclaimable = _session_terminal(conn, row["owner_session_id"])
        stale.append({
            "claim_id": row["claim_id"], "item_id": row["item_id"],
            "lane": row["lane_key"],
            "lease_expired_at": row["lease_expires_at"],
            "reclaimable": reclaimable,
        })
        if not reclaimable:
            warnings.append(
                f"stale claim {row['claim_id']} is not yet reclaimable: "
                f"session {row['owner_session_id']!r} still reports running "
                "(wait for confirmed exit or peer recovery after the abandon "
                "horizon)")

    owned = []
    owned_clause = "" if item_id is None else " AND id=?"
    owned_params = ((agent, agent) if item_id is None
                    else (agent, agent, item_id))
    for row in conn.execute(
            "SELECT * FROM items WHERE (owner_agent_id=? OR "
            "next_actor_agent_id=?) AND status!='done'" + owned_clause +
            " ORDER BY id", owned_params):
        labels = []
        if is_draft(row):
            labels.append("draft")
        elif contract_incomplete(row):
            labels.append("contract_incomplete")
        acceptance = contract_acceptance(conn, row["id"])
        if acceptance["required"] and acceptance["state"] != "accepted":
            labels.append(f"contract_{acceptance['state']}")
        # The grace label keys on the grace state itself, whatever its
        # source: answered questions leave needs_input, a declined handoff
        # leaves working.
        if (row["status"] in ("needs_input", "working")
                and row["resume_grace_expires_at"] is not None
                and row["resume_grace_expires_at"] <= now_ts
                and conn.execute(
                    "SELECT 1 FROM questions WHERE item_id=? AND "
                    "status='open' LIMIT 1", (row["id"],)).fetchone() is None):
            labels.append("resume_stale")
        still_needed = approvals_still_needed(conn, row["id"])
        if still_needed > 0:
            labels.append("approvals_needed")
            block = second_reviewer_blocked(conn, row["id"])
            if block["blocked"]:
                labels.append("second_reviewer_blocked")
            if agent == row["owner_agent_id"]:
                if block["blocked"]:
                    warnings.append(
                        f"item {row['id']} needs {still_needed} more "
                        "approve(s) from distinct providers but no remaining "
                        "registered provider can expand the set "
                        f"(approved={block['approved_providers'] or []}); "
                        "do not re-request review "
                        f"({block['reason_code']})")
                elif _live_review(conn, row["id"]) is None:
                    warnings.append(
                        f"item {row['id']} needs {still_needed} more "
                        "approve(s) from distinct providers on its current "
                        "receipt; request another review "
                        "(`coop review request --claim …`)")
        # The dual-claim review picture: the current review
        # row with the claims-lane holder beside it; `reviewer` stays the
        # immutable designation, `reviewer_agent_id` the informational
        # latest claimant.
        review = None
        review_row = conn.execute(
            "SELECT id, status, reviewer, reviewer_agent_id FROM reviews "
            "WHERE item_id=? AND status IN ('requested','changes') "
            "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        if review_row:
            holder = conn.execute(
                "SELECT claim_id, claimed_by_agent, lease_expires_at FROM "
                "claims WHERE lane_key=? AND status='active'",
                (f"review:{review_row['id']}",)).fetchone()
            review = {
                "review_id": review_row["id"],
                "status": review_row["status"],
                "reviewer": review_row["reviewer"],
                "reviewer_agent_id": review_row["reviewer_agent_id"],
                "claim": ({
                    "claim_id": holder["claim_id"],
                    "agent": holder["claimed_by_agent"],
                    "lease_expires_at": holder["lease_expires_at"],
                } if holder else None),
            }
        owned.append({
            "item_id": row["id"], "title": row["title"],
            "status": row["status"], "owner": row["owner_agent_id"],
            "next_actor": row["next_actor_agent_id"], "labels": labels,
            "review": review,
        })

    unread = {}
    for entry in read_inbox(
            conn, agent, peek=True, item_id=item_id):
        unread[entry["category"]] = unread.get(entry["category"], 0) + 1

    grace_clause = "" if item_id is None else " AND id=?"
    grace_params = ((agent, now_ts) if item_id is None
                    else (agent, now_ts, item_id))
    resume_grace = [
        {"item_id": r["id"], "expires_at": r["resume_grace_expires_at"]}
        for r in conn.execute(
            "SELECT id, resume_grace_expires_at FROM items WHERE "
            "preferred_resume_owner_agent_id=? AND "
            "resume_grace_expires_at > ?" + grace_clause +
            " ORDER BY id", grace_params)]

    action_lease = (DEFAULT_ACTION_LEASE_SECONDS
                    if action_lease_seconds is None else action_lease_seconds)
    next_action, lane_warnings = _derive_next_action(
        conn, agent, now_ts, lease_seconds=action_lease,
        item_id=item_id)
    warnings.extend(lane_warnings)

    return {
        "agent": _agent_ref(conn, agent) or
                 {"agent_id": agent, "provider": None},
        "session": session,
        "claims": claims,
        "owned_items": owned,
        "unread": unread,
        "resume_grace": resume_grace,
        "stale": stale,
        "warnings": warnings,
        "next_action": next_action,
    }

def admin_release(conn, *, claim_id, reason, confirm_process_stopped=False):
    """End-of-run / offline operator recovery only.

    Product happy path uses **agent** recovery (`agent_release_wedge`), not
    mid-run human release. Prefer unset COOP_SESSION_ID offline after the
    autonomous run has stopped.
    """
    if os.environ.get("COOP_SESSION_ID"):
        raise HumanLaneViolation(
            "operator recovery runs outside supervised sessions; "
            "unset COOP_SESSION_ID (or use `coop recover wedge` as a peer "
            "agent mid-run)",
            reason_code="human_lane_forbidden",
            evidence={"constraint": "offline_human_lane_required"},
        )
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidTransition(
            "operator release requires --reason",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_operator_release_reason"},
        )

    def _release(conn):
        row = conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound(
                f"no claim {claim_id}",
                reason_code="target_not_found",
                evidence={"claim_id": claim_id},
            )
        stamp = now()
        if row["status"] == "active":
            if row["lease_expires_at"] > stamp:
                raise InvalidTransition(
                    f"claim {claim_id} is live and unexpired; operator "
                    "release accepts only stale claims",
                    reason_code="transition_not_available",
                    evidence={
                        "item_id": row["item_id"],
                        "claim_id": claim_id,
                        "current_status": row["status"],
                        "required_status": "stale",
                        "lease_expired": False,
                        "lease_expires_at": row["lease_expires_at"],
                    },
                )
            _flip_stale(conn, row)
        elif row["status"] != "stale":
            raise InvalidTransition(
                f"claim {claim_id} is {row['status']}; operator release "
                "accepts only stale claims",
                reason_code="transition_not_available",
                evidence={
                    "item_id": row["item_id"],
                    "claim_id": claim_id,
                    "current_status": row["status"],
                    "required_status": "stale",
                },
            )
        session_running = _session_is_running(conn, row["owner_session_id"])
        if session_running and not confirm_process_stopped:
            raise UnsafeReclaim(
                f"session {row['owner_session_id']!r} still reports running "
                "with no confirmed process-tree exit; check the local "
                "process state and pass --confirm-process-stopped "
                "(or use peer `coop recover wedge` after abandon horizon)",
                reason_code="unsafe_reclaim",
                evidence={
                    "item_id": row["item_id"],
                    "claim_id": claim_id,
                    "session_status": "running",
                    "constraint": "process_tree_exit_confirmation_required",
                },
            )
        conn.execute(
            "UPDATE claims SET status='released', closed_at=?, "
            "close_reason=? WHERE claim_id=?",
            (stamp, reason.strip(), claim_id))
        append_event(
            conn, item_id=row["item_id"], event_type="operator_release",
            actor_agent_id="human", actor_session_id=None,
            claim_id=claim_id, fencing_token=row["fencing_token"],
            payload={"claim_id": claim_id, "reason": reason.strip(),
                     "process_exit_confirmed": bool(
                         confirm_process_stopped or not session_running)})

    return mutate(conn, _release)


def _session_last_seen_age_seconds(conn, session_id, stamp_iso):
    """Age of last_seen_at vs stamp; None if session missing."""
    row = conn.execute(
        "SELECT last_seen_at, status FROM sessions WHERE session_id=?",
        (session_id,)).fetchone()
    if row is None or not row["last_seen_at"]:
        return None
    try:
        last = datetime.datetime.fromisoformat(row["last_seen_at"])
        now_dt = datetime.datetime.fromisoformat(stamp_iso)
        # Normalize tz for boards that mix naive/aware ISO stamps.
        if last.tzinfo is not None and now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=last.tzinfo)
        elif now_dt.tzinfo is not None and last.tzinfo is None:
            last = last.replace(tzinfo=now_dt.tzinfo)
    except ValueError:
        return None
    return max(0.0, (now_dt - last).total_seconds())


def agent_release_wedge(conn, *, claim_id, session_id, reason,
                        abandon_after_s=None):
    """Peer (or self) agent recovery: release a stale claim without human.

    If the predecessor session still reports running but has not been seen
    within `abandon_after_s` (default DEFAULT_ABANDON_SECONDS), it is
    finished as `abandoned` so the claim can be released and reclaimed.
    Live fresh sessions still refuse (UnsafeReclaim).
    """
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidTransition(
            "agent wedge release requires --reason",
            reason_code="input_invalid",
            evidence={"constraint": "non_empty_agent_release_reason"},
        )
    horizon = (DEFAULT_ABANDON_SECONDS if abandon_after_s is None
               else int(abandon_after_s))
    if horizon < 1:
        raise InvalidTransition(
            "abandon horizon must be >= 1 second",
            reason_code="input_invalid",
            evidence={"constraint": "positive_abandon_horizon"},
        )

    # Sweep outside the write transaction (no nested mutate).
    sweep_expired_sessions(conn)

    def _release(conn):
        actor = _session_actor(conn, session_id)
        if actor is None:
            raise HumanLaneViolation(
                "agent wedge release requires a live supervised session",
                reason_code="human_lane_forbidden",
                evidence={"constraint": "agent_session_required"},
            )
        row = conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound(
                f"no claim {claim_id}",
                reason_code="target_not_found",
                evidence={"claim_id": claim_id},
            )
        stamp = now()
        if row["status"] == "released":
            return {"released": claim_id, "already": True}
        if row["status"] == "active":
            if row["lease_expires_at"] > stamp:
                raise InvalidTransition(
                    f"claim {claim_id} is live and unexpired; wait for "
                    "lease expiry or the owner to release",
                    reason_code="transition_not_available",
                    evidence={
                        "item_id": row["item_id"],
                        "claim_id": claim_id,
                        "current_status": row["status"],
                        "required_status": "stale",
                        "lease_expired": False,
                        "lease_expires_at": row["lease_expires_at"],
                    },
                )
            _flip_stale(conn, row)
            row = conn.execute(
                "SELECT * FROM claims WHERE claim_id=?",
                (claim_id,)).fetchone()
        elif row["status"] != "stale":
            raise InvalidTransition(
                f"claim {claim_id} is {row['status']}; wedge release "
                "accepts only stale (or expired-active) claims",
                reason_code="transition_not_available",
                evidence={
                    "item_id": row["item_id"],
                    "claim_id": claim_id,
                    "current_status": row["status"],
                    "required_status": "stale",
                },
            )
        owner_sid = row["owner_session_id"]
        if _session_is_running(conn, owner_sid):
            age = _session_last_seen_age_seconds(conn, owner_sid, stamp)
            if age is None or age < horizon:
                raise UnsafeReclaim(
                    f"session {owner_sid!r} still reports running "
                    f"(last_seen age {age!r}s < abandon {horizon}s); "
                    "wait for abandon horizon or peer liveness to drop",
                    reason_code="unsafe_reclaim",
                    evidence={
                        "item_id": row["item_id"],
                        "claim_id": claim_id,
                        "session_status": "running",
                        "constraint":
                            "abandon_horizon_or_session_exit_required",
                    },
                )
            # Abandon zombie session so reclaim is safe (agent recovery).
            conn.execute(
                "UPDATE sessions SET status='exited', "
                "termination_reason='abandoned', exited_at=?, last_seen_at=? "
                "WHERE session_id=? AND status='running'",
                (stamp, stamp, owner_sid))
            append_event(
                conn, item_id=None, event_type="session_finished",
                actor_agent_id=actor, actor_session_id=session_id,
                payload={"status": "exited", "reason": "abandoned",
                         "exit_code": None, "abandoned_session": owner_sid,
                         "by_agent": actor})
        conn.execute(
            "UPDATE claims SET status='released', closed_at=?, "
            "close_reason=? WHERE claim_id=?",
            (stamp, reason.strip(), claim_id))
        append_event(
            conn, item_id=row["item_id"], event_type="agent_release",
            actor_agent_id=actor, actor_session_id=session_id,
            claim_id=claim_id, fencing_token=row["fencing_token"],
            payload={"claim_id": claim_id, "reason": reason.strip(),
                     "recovery": "agent_wedge"})
        return {"released": claim_id, "already": False}

    return mutate(conn, _release)


def mark_autonomous_run(conn, *, phase, session_id=None, agent_id=None,
                        payload=None):
    """Board marker for autonomous run start/end (audit + mid-run human gate).

    phase is 'started' or 'finished'. Prefer agent_id of the runner host
    session when available so markers are not attributed to human.
    """
    if phase not in ("started", "finished"):
        raise InvalidTransition("run marker phase must be started|finished")
    body = dict(payload or {})
    body["phase"] = phase
    actor = agent_id
    sid = session_id
    if sid and not actor:
        actor = _session_actor(conn, sid)
    if not actor:
        row = conn.execute(
            "SELECT name FROM agents WHERE name!='human' "
            "ORDER BY name LIMIT 1").fetchone()
        if row is None:
            # Outside mutate: register host attribution agent.
            register_or_bind_agent(conn, agent_id="claude", provider="claude")
            actor = "claude"
        else:
            actor = row["name"]

    def _mark(conn):
        eid = append_event(
            conn, item_id=None,
            event_type=f"autonomous_run_{phase}",
            actor_agent_id=actor, actor_session_id=sid,
            payload=body)
        return {"event_id": eid, "phase": phase, "at": now()}

    return mutate(conn, _mark)


def mark_workflow_recipe_promotion(
        conn, *,
        item_id,
        run_started_event_id,
        session_id,
        agent_id,
        previous,
        current,
        reason):
    """Record one runner-owned quick-two -> standard-three promotion."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise InvalidTransition("workflow recipes must be payload objects")
    if (
            previous.get("name") != "quick_two"
            or current.get("name") != "standard_three"):
        raise InvalidTransition(
            "workflow promotion must be quick_two -> standard_three"
        )
    if not isinstance(reason, str) or not reason:
        raise InvalidTransition("workflow promotion requires a reason")

    def _mark(conn):
        _require_live_session(conn, session_id, agent_id)
        item = _item_row(conn, item_id)
        started = conn.execute(
            "SELECT event_id FROM events "
            "WHERE event_id=? AND event_type='autonomous_run_started'",
            (run_started_event_id,),
        ).fetchone()
        if started is None:
            raise InvalidTransition(
                "workflow promotion requires its run-start marker"
            )
        duplicate = conn.execute(
            "SELECT event_id FROM events WHERE item_id=? "
            "AND event_type='workflow_recipe_promoted' "
            "AND json_extract(payload_json, '$.run_started_event_id')=? "
            "LIMIT 1",
            (item_id, run_started_event_id),
        ).fetchone()
        if duplicate is not None:
            raise InvalidTransition(
                "workflow recipe was already promoted for this run"
            )
        payload = {
            "item_id": item_id,
            "run_started_event_id": run_started_event_id,
            "from": previous["name"],
            "to": current["name"],
            "reason": reason,
            "selected_contract_version": previous.get(
                "contract_version"
            ),
            "current_contract_version": item["contract_version"],
            "active_participants": current.get(
                "active_participants",
                [],
            ),
        }
        event_id = append_event(
            conn,
            item_id=item_id,
            event_type="workflow_recipe_promoted",
            actor_agent_id=agent_id,
            actor_session_id=session_id,
            payload=payload,
        )
        return {"event_id": event_id, **payload}

    return mutate(conn, _mark)


def mid_run_human_mutations(conn, *, since_iso=None, until_iso=None,
                            after_event_id=None):
    """Detect human-lane board mutations after autonomous run start.

    Prefer ``after_event_id`` (the autonomous_run_started event_id) so
    same-second kickoff seeds are not false positives. Falls back to
    ``since_iso`` timestamp comparison when no event id is supplied.
    """
    until = until_iso or now()
    if after_event_id is not None:
        events = [dict(r) for r in conn.execute(
            "SELECT event_id, event_type, created_at, payload_json FROM events "
            "WHERE actor_agent_id='human' AND event_id > ? AND created_at <= ? "
            "ORDER BY event_id", (after_event_id, until))]
        # Messages have no event_id link; use started marker's created_at.
        start = conn.execute(
            "SELECT created_at FROM events WHERE event_id=?",
            (after_event_id,)).fetchone()
        since = start["created_at"] if start else (since_iso or "")
        messages = [dict(r) for r in conn.execute(
            "SELECT id, body, created_at, item_id FROM messages "
            "WHERE from_agent='human' AND created_at > ? AND created_at <= ? "
            "ORDER BY id", (since, until))] if since else []
    else:
        since = since_iso or ""
        events = [dict(r) for r in conn.execute(
            "SELECT event_id, event_type, created_at, payload_json FROM events "
            "WHERE actor_agent_id='human' AND created_at > ? AND created_at <= ? "
            "ORDER BY event_id", (since, until))]
        messages = [dict(r) for r in conn.execute(
            "SELECT id, body, created_at, item_id FROM messages "
            "WHERE from_agent='human' AND created_at > ? AND created_at <= ? "
            "ORDER BY id", (since, until))]
    return {
        "since": since_iso,
        "after_event_id": after_event_id,
        "until": until,
        "events": events,
        "messages": messages,
        "count": len(events) + len(messages),
    }
