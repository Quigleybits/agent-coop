"""Schema identity and fresh-database initialization for Agent Co-op."""

import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile

from agent_coop.coop_errors import CoopError, MigrationFailed


APPLICATION_ID = 0x434F4F50
SCHEMA_VERSION = 4
V3_SCHEMA_VERSION = 3
LEGACY_REQUIRED_TABLES = frozenset(
    {
        "agents",
        "rooms",
        "items",
        "messages",
        "agent_offsets",
        "assignments",
        "reviews",
        "debates",
        "debate_posts",
        "decisions",
    }
)
LEGACY_REQUIRED_SCHEMA_OBJECTS = frozenset(
    {("table", table) for table in LEGACY_REQUIRED_TABLES}
    | {("index", "ux_one_owner")}
)
LEGACY_ALLOWED_EXTRA_SCHEMA_OBJECTS = frozenset({("table", "sessions")})
V3_REQUIRED_TABLES = LEGACY_REQUIRED_TABLES | frozenset(
    {
        "sessions",
        "claims",
        "questions",
        "receipts",
        "handoffs",
        "events",
        "inbox_entries",
        "inbox_offsets",
    }
)
LEGACY_REQUIRED_COLUMNS = {
    "agents": frozenset({"name", "kind", "registered_at"}),
    "rooms": frozenset({"id", "name", "created_at"}),
    "items": frozenset(
        {"id", "title", "body", "status", "created_by", "created_at", "updated_at"}
    ),
    "messages": frozenset(
        {
            "id",
            "room_id",
            "item_id",
            "from_agent",
            "to_agent",
            "kind",
            "checkpoint",
            "body",
            "created_at",
        }
    ),
    "agent_offsets": frozenset({"agent", "last_message_id"}),
    "assignments": frozenset(
        {"id", "item_id", "agent", "role", "state", "claimed_at"}
    ),
    "reviews": frozenset(
        {
            "id",
            "item_id",
            "requested_by",
            "reviewer",
            "status",
            "body",
            "created_at",
            "resolved_at",
        }
    ),
    "debates": frozenset(
        {
            "id",
            "item_id",
            "topic",
            "status",
            "judge",
            "created_by",
            "created_at",
            "closed_at",
        }
    ),
    "debate_posts": frozenset(
        {"id", "debate_id", "round", "agent", "body", "created_at"}
    ),
    "decisions": frozenset(
        {"id", "item_id", "debate_id", "text", "rationale", "decided_by", "created_at"}
    ),
}
LEGACY_PRIMARY_KEYS = {
    "agents": ("name",),
    "rooms": ("id",),
    "items": ("id",),
    "messages": ("id",),
    "agent_offsets": ("agent",),
    "assignments": ("id",),
    "reviews": ("id",),
    "debates": ("id",),
    "debate_posts": ("id",),
    "decisions": ("id",),
}
LEGACY_REQUIRED_FOREIGN_KEYS = {
    "agents": frozenset(),
    "rooms": frozenset(),
    "items": frozenset({("created_by", "agents", "name")}),
    "messages": frozenset(
        {
            ("room_id", "rooms", "id"),
            ("item_id", "items", "id"),
            ("from_agent", "agents", "name"),
            ("to_agent", "agents", "name"),
        }
    ),
    "agent_offsets": frozenset({("agent", "agents", "name")}),
    "assignments": frozenset(
        {("item_id", "items", "id"), ("agent", "agents", "name")}
    ),
    "reviews": frozenset(
        {
            ("item_id", "items", "id"),
            ("requested_by", "agents", "name"),
            ("reviewer", "agents", "name"),
        }
    ),
    "debates": frozenset(
        {
            ("item_id", "items", "id"),
            ("judge", "agents", "name"),
            ("created_by", "agents", "name"),
        }
    ),
    "debate_posts": frozenset(
        {("debate_id", "debates", "id"), ("agent", "agents", "name")}
    ),
    "decisions": frozenset(
        {
            ("item_id", "items", "id"),
            ("debate_id", "debates", "id"),
            ("decided_by", "agents", "name"),
        }
    ),
}
LEGACY_COLUMN_ADDITIONS = {
    "agents": (
        ("provider", "TEXT"),
        ("display_name", "TEXT"),
        ("last_seen_at", "TEXT"),
    ),
    "items": (
        ("objective", "TEXT"),
        ("scope", "TEXT"),
        ("done_when", "TEXT"),
        ("output_contract", "TEXT"),
        ("context", "TEXT"),
        ("allowed_actions", "TEXT"),
        ("stop_conditions", "TEXT"),
        ("owner_agent_id", "TEXT REFERENCES agents(name)"),
        ("next_actor_agent_id", "TEXT REFERENCES agents(name)"),
        ("preferred_resume_owner_agent_id", "TEXT REFERENCES agents(name)"),
        (
            "review_required",
            "INTEGER NOT NULL DEFAULT 1 CHECK (review_required IN (0, 1))",
        ),
        ("review_waiver_reason", "TEXT"),
        ("contract_version", "INTEGER NOT NULL DEFAULT 1"),
        ("answered_at", "TEXT"),
        ("resume_grace_started_at", "TEXT"),
        ("resume_grace_expires_at", "TEXT"),
    ),
    "assignments": (
        ("agent_id", "TEXT REFERENCES agents(name)"),
        ("assigned_by", "TEXT REFERENCES agents(name)"),
        ("assigned_at", "TEXT"),
        ("released_at", "TEXT"),
        ("release_reason", "TEXT"),
        ("legacy", "INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1))"),
    ),
    "reviews": (
        ("receipt_id", "INTEGER REFERENCES receipts(receipt_id)"),
        ("contract_version", "INTEGER"),
        ("requested_by_agent", "TEXT REFERENCES agents(name)"),
        ("reviewer_agent_id", "TEXT REFERENCES agents(name)"),
        ("verdict_body", "TEXT"),
        ("legacy", "INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1))"),
    ),
    "decisions": (
        ("decided_by_agent", "TEXT REFERENCES agents(name)"),
        ("decided_by_session", "TEXT REFERENCES sessions(session_id)"),
        ("claim_id", "INTEGER REFERENCES claims(claim_id)"),
        ("fencing_token", "INTEGER"),
        ("legacy", "INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1))"),
    ),
}


class SchemaError(Exception):
    """Raised when a database cannot be used as the current Co-op schema."""


def identity(conn):
    """Return the SQLite application ID and user schema version."""
    return (
        conn.execute("PRAGMA application_id").fetchone()[0],
        conn.execute("PRAGMA user_version").fetchone()[0],
    )


def _schema_statements(schema):
    pending = []
    for line in schema.splitlines():
        pending.append(line)
        candidate = "\n".join(pending).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            pending.clear()
    if "\n".join(pending).strip():
        raise SchemaError("schema.sql ends with an incomplete SQL statement")


def _schema_objects(conn):
    return {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    }


def _user_schema_objects(conn):
    return _schema_objects(conn)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _default_backup_path(db_path):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    candidate = db_path.with_name(f"{db_path.name}.backup-{stamp}.bak")
    counter = 1
    while candidate.exists():
        candidate = db_path.with_name(
            f"{db_path.name}.backup-{stamp}-{counter}.bak"
        )
        counter += 1
    return candidate


def _sqlite_uri(path, mode):
    return f"{path.resolve().as_uri()}?mode={mode}"


def _logical_content_sha256(conn):
    digest = hashlib.sha256()
    for line in conn.iterdump():
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _backup_snapshot(conn):
    schema_objects = _schema_objects(conn)
    tables = {
        name for object_type, name in schema_objects if object_type == "table"
    }
    return {
        "identity": identity(conn),
        "schema_objects": frozenset(schema_objects),
        "legacy_row_counts": {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in sorted(LEGACY_REQUIRED_TABLES & tables)
        },
        "logical_content_sha256": _logical_content_sha256(conn),
    }


def _verify_backup(backup_path, expected_snapshot):
    try:
        with contextlib.closing(
            sqlite3.connect(_sqlite_uri(backup_path, "ro"), uri=True)
        ) as backup:
            result = backup.execute("PRAGMA integrity_check").fetchone()[0]
            actual_snapshot = _backup_snapshot(backup)
    except sqlite3.Error as exc:
        raise SchemaError(
            f"backup verification could not open the existing backup: {exc}"
        ) from exc
    if result != "ok":
        raise SchemaError(f"backup integrity check failed: {result}")
    for field in (
        "identity",
        "schema_objects",
        "legacy_row_counts",
        "logical_content_sha256",
    ):
        if actual_snapshot[field] != expected_snapshot[field]:
            raise SchemaError(
                f"backup verification failed: {field} does not match the source"
            )


def _create_verified_backup(source, backup_path, expected_snapshot):
    private_path = None
    try:
        descriptor, private_name = tempfile.mkstemp(
            prefix=f".{backup_path.name}.",
            suffix=".tmp",
            dir=backup_path.parent,
        )
        private_path = pathlib.Path(private_name)
        os.close(descriptor)
        with contextlib.closing(
            sqlite3.connect(_sqlite_uri(private_path, "rw"), uri=True)
        ) as backup:
            source.backup(backup)
        _verify_backup(private_path, expected_snapshot)
        try:
            os.link(private_path, backup_path)
        except FileExistsError as exc:
            raise SchemaError(
                f"backup target already exists: {backup_path}"
            ) from exc
        except OSError as exc:
            raise SchemaError(
                f"could not publish verified backup: {exc}"
            ) from exc
    finally:
        if private_path is not None:
            try:
                private_path.unlink(missing_ok=True)
            except OSError:
                pass


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_legacy_columns(conn):
    for table, additions in LEGACY_COLUMN_ADDITIONS.items():
        existing = _table_columns(conn, table)
        for name, definition in additions:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _require_legacy_schema_fingerprint(conn, schema_objects):
    problems = []
    unexpected_objects = sorted(
        schema_objects
        - LEGACY_REQUIRED_SCHEMA_OBJECTS
        - LEGACY_ALLOWED_EXTRA_SCHEMA_OBJECTS
    )
    if unexpected_objects:
        problems.append(f"unexpected schema objects {unexpected_objects!r}")

    for table in sorted(LEGACY_REQUIRED_TABLES):
        table_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row[1] for row in table_info}
        missing_columns = sorted(LEGACY_REQUIRED_COLUMNS[table] - columns)
        if missing_columns:
            problems.append(f"{table} missing columns {','.join(missing_columns)}")

        primary_key = tuple(
            row[1]
            for row in sorted(
                (row for row in table_info if row[5]), key=lambda row: row[5]
            )
        )
        if primary_key != LEGACY_PRIMARY_KEYS[table]:
            problems.append(f"{table} has primary key {primary_key!r}")

        foreign_keys = {
            (row[3], row[2], row[4])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        missing_foreign_keys = sorted(
            LEGACY_REQUIRED_FOREIGN_KEYS[table] - foreign_keys
        )
        if missing_foreign_keys:
            problems.append(
                f"{table} missing foreign keys {missing_foreign_keys!r}"
            )

    owner_index = next(
        (
            row
            for row in conn.execute("PRAGMA index_list(assignments)")
            if row[1] == "ux_one_owner"
        ),
        None,
    )
    owner_index_columns = tuple(
        row[2] for row in conn.execute("PRAGMA index_info('ux_one_owner')")
    )
    owner_index_row = conn.execute(
        "SELECT sql FROM sqlite_schema "
        "WHERE type='index' AND name='ux_one_owner' AND tbl_name='assignments'"
    ).fetchone()
    owner_index_sql = "" if owner_index_row is None else owner_index_row[0]
    normalized_owner_index = "".join(owner_index_sql.lower().split())
    expected_owner_index = (
        "createuniqueindexux_one_owneronassignments(item_id)"
        "whererole='owner'andstate='active'"
    )
    if (
        owner_index is None
        or owner_index[2] != 1
        or owner_index[4] != 1
        or owner_index_columns != ("item_id",)
        or normalized_owner_index != expected_owner_index
    ):
        problems.append("assignments has an invalid ux_one_owner partial index")

    if problems:
        raise SchemaError(
            "pre-release schema does not match the documented legacy structure: "
            + "; ".join(problems)
        )


def _classify_board_for_migration(conn):
    """Route a quiesced board to its supported migration path.

    Returns (source_version, row_counts): 0 for a verified pre-release board,
    3 for an Agent Co-op version 3 board. Everything else is refused."""
    application_id, source_version = identity(conn)
    if (application_id, source_version) == (APPLICATION_ID, SCHEMA_VERSION):
        raise SchemaError("board schema is already current; nothing to migrate")
    if application_id == APPLICATION_ID:
        if source_version == V3_SCHEMA_VERSION:
            return _classify_v3_board(conn)
        raise SchemaError(
            f"unsupported Agent Co-op schema version {source_version}; "
            "migration accepts a pre-release (version 0) or version 3 board"
        )
    if application_id != 0:
        raise SchemaError(
            f"unsupported Agent Co-op application ID {application_id}; "
            "migration accepts only an unstamped pre-release board or an "
            "Agent Co-op version 3 board"
        )
    if source_version != 0:
        raise SchemaError(
            f"unsupported pre-release schema version {source_version}; "
            "migration accepts only version 0"
        )
    return _classify_legacy_board(conn)


def _classify_v3_board(conn):
    tables = {
        name
        for object_type, name in _schema_objects(conn)
        if object_type == "table"
    }
    missing = sorted(V3_REQUIRED_TABLES - tables)
    if missing:
        raise SchemaError(
            "database is not a recognized version 3 board; missing tables: "
            + ", ".join(missing)
        )
    if conn.execute(
        "SELECT 1 FROM sessions WHERE status='running' LIMIT 1"
    ).fetchone():
        raise SchemaError(
            "cannot migrate while a recorded running session exists"
        )
    row_counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in sorted(V3_REQUIRED_TABLES)
    }
    return V3_SCHEMA_VERSION, row_counts


def _classify_legacy_board(conn):
    schema_objects = _user_schema_objects(conn)
    tables = {
        name for object_type, name in schema_objects if object_type == "table"
    }
    missing = sorted(LEGACY_REQUIRED_TABLES - tables)
    if missing:
        raise SchemaError(
            "database is not a recognized pre-release board; missing tables: "
            + ", ".join(missing)
        )
    _require_legacy_schema_fingerprint(conn, schema_objects)
    if "sessions" in tables:
        if conn.execute(
            "SELECT 1 FROM sessions WHERE status='running' LIMIT 1"
        ).fetchone():
            raise SchemaError(
                "cannot migrate while a recorded running session exists"
            )
        raise SchemaError(
            "pre-release schema contains an unsupported partial sessions table"
        )

    row_counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in sorted(LEGACY_REQUIRED_TABLES)
    }
    return 0, row_counts


def _apply_legacy_migration(conn):
    """The v0->v3 leg: pure transformation, no checks, no event, no stamp."""
    _add_legacy_columns(conn)

    schema_path = pathlib.Path(__file__).with_name("schema.sql")
    schema = schema_path.read_text(encoding="utf-8")
    for statement in _schema_statements(schema):
        conn.execute(statement)

    conn.execute(
        "UPDATE agents SET provider=name "
        "WHERE provider IS NULL AND name IN ('claude','codex','grok')"
    )
    conn.execute("UPDATE items SET context=body WHERE context IS NULL")
    conn.execute(
        "UPDATE items SET owner_agent_id=("
        "  SELECT MIN(assignments.agent) FROM assignments "
        "  WHERE assignments.item_id=items.id "
        "    AND assignments.role='owner' AND assignments.state='active'"
        ") WHERE owner_agent_id IS NULL AND 1=("
        "  SELECT COUNT(*) FROM assignments "
        "  WHERE assignments.item_id=items.id "
        "    AND assignments.role='owner' AND assignments.state='active'"
        ")"
    )
    conn.execute(
        "UPDATE assignments SET "
        "agent_id=COALESCE(agent_id,agent), "
        "assigned_at=COALESCE(assigned_at,claimed_at), legacy=1"
    )
    conn.execute(
        "UPDATE reviews SET "
        "requested_by_agent=COALESCE(requested_by_agent,requested_by), "
        "reviewer_agent_id=COALESCE(reviewer_agent_id,reviewer), "
        "verdict_body=COALESCE(verdict_body,body), legacy=1"
    )
    conn.execute(
        "UPDATE decisions SET "
        "decided_by_agent=COALESCE(decided_by_agent,decided_by), legacy=1"
    )


def _apply_v4_delta(conn):
    """The v3->v4 leg. Idempotent: also runs after the v0 leg, where the
    schema.sql pass and column additions have already done most of it."""
    existing = _table_columns(conn, "items")
    if "output_contract" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN output_contract TEXT")
    if "resume_grace_started_at" not in existing:
        conn.execute(
            "ALTER TABLE items ADD COLUMN resume_grace_started_at TEXT"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_one_running "
        "ON sessions(agent_id) WHERE status='running'"
    )
    conn.execute("DROP INDEX IF EXISTS ux_one_owner")
    conn.execute(
        "UPDATE items SET status='needs_input' WHERE status='needs-input'"
    )


def _seed_human_actor(conn):
    """Ensure the reserved trusted-local human actor exists. Returns the
    number of rows inserted (0 or 1)."""
    if conn.execute(
        "SELECT 1 FROM agents WHERE name='human' LIMIT 1"
    ).fetchone():
        return 0
    conn.execute(
        "INSERT INTO agents(name,provider,registered_at) "
        "VALUES ('human',NULL,?)",
        (_utc_now(),),
    )
    return 1


def _check_preserved_rows(conn, row_counts, human_added):
    """Every preserved count must match the source exactly, except agents,
    which may grow by exactly the seeded human row. The events count is
    checked BEFORE the migration event is appended, so it too is exact."""
    for table, expected in row_counts.items():
        if table == "agents":
            expected += human_added
        actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if actual != expected:
            raise SchemaError(
                f"migration changed the preserved {table} row count "
                f"from {expected} to {actual}"
            )


def _pre_commit_integrity(conn):
    foreign_key_failures = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_failures:
        raise SchemaError(
            "pre-commit foreign key check failed: "
            f"{foreign_key_failures!r}"
        )
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SchemaError(f"pre-commit integrity check failed: {integrity}")


def _append_migration_event(conn, source_version, row_counts, backup_path):
    payload = json.dumps(
        {
            "backup_path": str(backup_path.resolve()),
            "preserved_row_counts": row_counts,
            "source_schema_version": source_version,
            "target_schema_version": SCHEMA_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO events(event_type,payload_json,created_at) VALUES (?,?,?)",
        ("schema_migrated", payload, _utc_now()),
    )


def _stamp_current_identity(conn):
    """Stamp inside the exclusive migration transaction: a crash
    before commit rolls the stamp back with everything else, so a migrated
    shape can never sit under a stale version label."""
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _post_commit_checks(conn):
    foreign_key_failures = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_failures:
        raise SchemaError(
            "post-migration foreign key check failed: "
            f"{foreign_key_failures!r}"
        )
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SchemaError(f"post-migration integrity check failed: {integrity}")


def _clear_current_identity(conn):
    conn.execute("BEGIN EXCLUSIVE")
    try:
        conn.execute("PRAGMA application_id = 0")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def initialize(conn):
    """Initialize an empty database, refusing legacy or unknown schemas."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_identity = identity(conn)
        if current_identity == (APPLICATION_ID, SCHEMA_VERSION):
            conn.rollback()
            return

        schema_objects = _user_schema_objects(conn)
        tables = {
            name for object_type, name in schema_objects if object_type == "table"
        }
        if current_identity == (0, 0) and LEGACY_REQUIRED_TABLES <= tables:
            raise SchemaError("pre-release board requires migration; run coop migrate")
        if current_identity != (0, 0) or schema_objects:
            raise SchemaError(
                "database is not an empty Agent Co-op board; run coop init for a new "
                "board or coop migrate for a pre-release board"
            )

        schema_path = pathlib.Path(__file__).with_name("schema.sql")
        schema = schema_path.read_text(encoding="utf-8")
        for statement in _schema_statements(schema):
            conn.execute(statement)
        _seed_human_actor(conn)
        _stamp_current_identity(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate(
    db_path, backup_path=None, confirm_legacy_clients_stopped=False
):
    """Back up and migrate one quiesced pre-release board to the current schema."""
    if not confirm_legacy_clients_stopped:
        raise SchemaError(
            "migration requires confirmation that all legacy clients are stopped"
        )

    source_path = pathlib.Path(db_path).expanduser().resolve()
    if not source_path.is_file():
        raise SchemaError(f"pre-release board does not exist: {source_path}")

    if backup_path is None:
        saved_path = _default_backup_path(source_path)
    else:
        saved_path = pathlib.Path(backup_path).expanduser().resolve()
    if saved_path.exists():
        raise SchemaError(f"backup target already exists: {saved_path}")

    try:
        conn = sqlite3.connect(source_path)
    except sqlite3.Error as exc:
        raise SchemaError(f"could not open pre-release board: {exc}") from exc

    committed = False
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        expected_backup = _backup_snapshot(conn)
        _create_verified_backup(conn, saved_path, expected_backup)

        try:
            conn.execute("BEGIN EXCLUSIVE")
            source_version, row_counts = _classify_board_for_migration(conn)
            if source_version == 0:
                _apply_legacy_migration(conn)
            _apply_v4_delta(conn)
            human_added = _seed_human_actor(conn)
            _check_preserved_rows(conn, row_counts, human_added)
            _pre_commit_integrity(conn)
            _append_migration_event(
                conn, source_version, row_counts, saved_path
            )
            _stamp_current_identity(conn)
            conn.commit()
            committed = True
        except Exception:
            conn.rollback()
            raise

        try:
            _post_commit_checks(conn)
        except Exception as verification_error:
            try:
                _clear_current_identity(conn)
            except Exception as identity_error:
                raise MigrationFailed(
                    f"post-commit verification failed: {verification_error}; "
                    f"verified backup preserved at {saved_path}; failed to "
                    f"clear the current schema identity: {identity_error}"
                ) from verification_error
            raise MigrationFailed(
                f"post-commit verification failed: {verification_error}; "
                f"verified backup preserved at {saved_path}"
            ) from verification_error
    except (SchemaError, CoopError):
        raise
    except (OSError, sqlite3.Error) as exc:
        phase = "post-commit verification" if committed else "migration"
        raise SchemaError(f"{phase} failed: {exc}") from exc
    finally:
        conn.close()

    return saved_path


def require_current(conn):
    """Reject a connection whose schema identity is not exactly current."""
    application_id, version = identity(conn)
    if (application_id, version) == (APPLICATION_ID, SCHEMA_VERSION):
        return
    if application_id == APPLICATION_ID:
        raise SchemaError(
            f"board schema version {version} is no longer supported; "
            "run coop migrate to upgrade this board"
        )
    raise SchemaError(
        "board schema is not current; run coop init for a new board or "
        "coop migrate for a pre-release board"
    )


def semantic_fingerprint(conn):
    """Order-insensitive structural fingerprint used to prove that a fresh
    board and a migrated board are the same schema: per-table columns
    (name -> declared type, notnull, default, primary-key position), foreign
    keys, and the named index set including partial-index WHERE clauses.
    Raw sqlite_master SQL is deliberately excluded — additive ALTER TABLE
    makes it permanently unequal between fresh and migrated boards."""
    fingerprint = {"tables": {}, "indexes": {}}
    for object_type, name in sorted(_schema_objects(conn)):
        if object_type == "table":
            columns = {
                row[1]: (
                    " ".join(str(row[2]).upper().split()),
                    row[3],
                    row[4],
                    row[5],
                )
                for row in conn.execute(f"PRAGMA table_info({name})")
            }
            foreign_keys = frozenset(
                (row[3], row[2], row[4], row[5], row[6])
                for row in conn.execute(f"PRAGMA foreign_key_list({name})")
            )
            fingerprint["tables"][name] = {
                "columns": columns,
                "foreign_keys": foreign_keys,
            }
        elif object_type == "index":
            row = conn.execute(
                "SELECT tbl_name, sql FROM sqlite_schema "
                "WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
            normalized_sql = (
                "" if row is None or row[1] is None
                else " ".join(row[1].lower().split())
            )
            fingerprint["indexes"][name] = (row[0], normalized_sql)
    return fingerprint
