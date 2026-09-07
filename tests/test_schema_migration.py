import contextlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_coop import coop_errors
from agent_coop import coop_schema
from agent_coop import coopdb


LEGACY_SCHEMA = """
CREATE TABLE agents (
  name TEXT PRIMARY KEY, kind TEXT, registered_at TEXT NOT NULL);
CREATE TABLE rooms (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT,
  status TEXT NOT NULL DEFAULT 'todo',
  created_by TEXT NOT NULL REFERENCES agents(name),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER REFERENCES rooms(id),
  item_id INTEGER REFERENCES items(id),
  from_agent TEXT NOT NULL REFERENCES agents(name),
  to_agent TEXT REFERENCES agents(name), kind TEXT NOT NULL DEFAULT 'chat',
  checkpoint TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE agent_offsets (
  agent TEXT PRIMARY KEY REFERENCES agents(name),
  last_message_id INTEGER NOT NULL DEFAULT 0);
CREATE TABLE assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  agent TEXT NOT NULL REFERENCES agents(name), role TEXT NOT NULL DEFAULT 'owner',
  state TEXT NOT NULL DEFAULT 'active', claimed_at TEXT NOT NULL);
CREATE UNIQUE INDEX ux_one_owner ON assignments(item_id)
  WHERE role='owner' AND state='active';
CREATE TABLE reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  requested_by TEXT NOT NULL REFERENCES agents(name),
  reviewer TEXT REFERENCES agents(name),
  status TEXT NOT NULL DEFAULT 'requested', body TEXT,
  created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE debates (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER REFERENCES items(id),
  topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  judge TEXT REFERENCES agents(name),
  created_by TEXT NOT NULL REFERENCES agents(name),
  created_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE debate_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  debate_id INTEGER NOT NULL REFERENCES debates(id),
  round INTEGER NOT NULL DEFAULT 1,
  agent TEXT NOT NULL REFERENCES agents(name),
  body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER REFERENCES items(id),
  debate_id INTEGER REFERENCES debates(id), text TEXT NOT NULL, rationale TEXT,
  decided_by TEXT NOT NULL REFERENCES agents(name), created_at TEXT NOT NULL);
"""

LOOKALIKE_SCHEMA = """
CREATE TABLE agents (name TEXT PRIMARY KEY);
CREATE TABLE rooms (id INTEGER PRIMARY KEY);
CREATE TABLE items (id INTEGER PRIMARY KEY, body TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY);
CREATE TABLE agent_offsets (agent TEXT PRIMARY KEY);
CREATE TABLE assignments (
  id INTEGER PRIMARY KEY, item_id INTEGER, agent TEXT, role TEXT,
  state TEXT, claimed_at TEXT);
CREATE UNIQUE INDEX ux_one_owner ON assignments(item_id)
  WHERE role='owner' AND state='active';
CREATE TABLE reviews (
  id INTEGER PRIMARY KEY, requested_by TEXT, reviewer TEXT, body TEXT);
CREATE TABLE debates (id INTEGER PRIMARY KEY);
CREATE TABLE debate_posts (id INTEGER PRIMARY KEY);
CREATE TABLE decisions (id INTEGER PRIMARY KEY, decided_by TEXT);
"""

LEGACY_TABLES = (
    "agent_offsets",
    "agents",
    "assignments",
    "debate_posts",
    "debates",
    "decisions",
    "items",
    "messages",
    "reviews",
    "rooms",
)


def database_snapshot(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return coop_schema.identity(conn), tuple(conn.iterdump())


class TemporaryBoard:
    def __enter__(self):
        self._directory = tempfile.TemporaryDirectory()
        # migrate() resolves every path it accepts or returns, so the board
        # path a test compares against must be resolved too. TEMP holds an 8.3
        # short name on some Windows hosts (a GitHub runner has
        # C:\Users\RUNNER~1\...), which makes the raw temp path and the
        # resolved product path two spellings of the same file.
        self.path = pathlib.Path(self._directory.name).resolve() / "board.db"
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._directory.cleanup()


class LegacyBoard(TemporaryBoard):
    def __enter__(self):
        super().__enter__()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(LEGACY_SCHEMA)
            conn.executemany(
                "INSERT INTO agents(name,kind,registered_at) VALUES (?,?,?)",
                (
                    ("claude", "reasoner", "2026-07-15T08:00:00+00:00"),
                    ("codex", "implementer", "2026-07-15T08:01:00+00:00"),
                    ("grok", "reviewer", "2026-07-15T08:02:00+00:00"),
                    ("custom-agent", "specialist", "2026-07-15T08:03:00+00:00"),
                ),
            )
            conn.execute(
                "INSERT INTO rooms(name,created_at) VALUES (?,?)",
                ("#general", "2026-07-15T08:04:00+00:00"),
            )
            conn.executemany(
                "INSERT INTO items(title,body,status,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    (
                        "Completed pre-release task",
                        "legacy body",
                        "done",
                        "claude",
                        "2026-07-15T08:05:00+00:00",
                        "2026-07-15T08:06:00+00:00",
                    ),
                    (
                        "Classifier pre-release task",
                        "classification context",
                        "todo",
                        "claude",
                        "2026-07-15T08:07:00+00:00",
                        "2026-07-15T08:07:00+00:00",
                    ),
                ),
            )
            conn.executemany(
                "INSERT INTO assignments(item_id,agent,role,state,claimed_at) "
                "VALUES (?,?,?,'active',?)",
                (
                    (1, "codex", "owner", "2026-07-15T08:08:00+00:00"),
                    (2, "custom-agent", "classifier", "2026-07-15T08:09:00+00:00"),
                ),
            )
            conn.execute(
                "INSERT INTO messages(room_id,item_id,from_agent,to_agent,kind,body,created_at) "
                "VALUES (1,1,'claude','codex','chat','legacy message',?)",
                ("2026-07-15T08:10:00+00:00",),
            )
            conn.execute(
                "INSERT INTO agent_offsets(agent,last_message_id) VALUES ('codex',1)"
            )
            conn.execute(
                "INSERT INTO reviews(item_id,requested_by,reviewer,status,body,created_at,resolved_at) "
                "VALUES (1,'codex','grok','approved','legacy verdict',?,?)",
                (
                    "2026-07-15T08:11:00+00:00",
                    "2026-07-15T08:12:00+00:00",
                ),
            )
            conn.execute(
                "INSERT INTO debates(item_id,topic,status,judge,created_by,created_at,closed_at) "
                "VALUES (1,'Legacy migration topic','closed','grok','claude',?,?)",
                (
                    "2026-07-15T08:13:00+00:00",
                    "2026-07-15T08:16:00+00:00",
                ),
            )
            conn.execute(
                "INSERT INTO debate_posts(debate_id,round,agent,body,created_at) "
                "VALUES (1,1,'codex','legacy debate post',?)",
                ("2026-07-15T08:14:00+00:00",),
            )
            conn.execute(
                "INSERT INTO decisions(item_id,debate_id,text,rationale,decided_by,created_at) "
                "VALUES (1,1,'Keep the pre-release history','Migration evidence','grok',?)",
                ("2026-07-15T08:15:00+00:00",),
            )
            conn.commit()
        self.expected_counts = self.row_counts()
        return self

    def row_counts(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in LEGACY_TABLES
            }

    def source_snapshot(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            schema = tuple(
                conn.execute(
                    "SELECT type,name,sql FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )
            )
            rows = {
                table: tuple(conn.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in LEGACY_TABLES
            }
            return coop_schema.identity(conn), schema, rows


def run_coop(*args):
    coop_dir = pathlib.Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "coop.py", *(str(arg) for arg in args)],
        cwd=coop_dir,
        capture_output=True,
        text=True,
    )


# The exact version 3 DDL as it shipped (the pre-version-4 schema.sql).
# Frozen here as a fixture: version 3 boards in the wild have this shape, and
# the fixture must not drift when schema.sql moves to version 4.
V3_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY,
  kind TEXT,
  provider TEXT,
  display_name TEXT,
  registered_at TEXT NOT NULL,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT,
  objective TEXT,
  scope TEXT,
  done_when TEXT,
  context TEXT,
  allowed_actions TEXT,
  stop_conditions TEXT,
  status TEXT NOT NULL DEFAULT 'todo',
  owner_agent_id TEXT REFERENCES agents(name),
  next_actor_agent_id TEXT REFERENCES agents(name),
  preferred_resume_owner_agent_id TEXT REFERENCES agents(name),
  review_required INTEGER NOT NULL DEFAULT 1 CHECK (review_required IN (0, 1)),
  review_waiver_reason TEXT,
  contract_version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL REFERENCES agents(name),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  answered_at TEXT,
  resume_grace_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(name),
  provider TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('running', 'exited', 'cancelled', 'timed_out')
  ),
  command_json TEXT NOT NULL,
  working_directory TEXT NOT NULL,
  started_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  exited_at TEXT,
  exit_code INTEGER,
  termination_reason TEXT,
  max_runtime_seconds INTEGER NOT NULL,
  shutdown_grace_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  claim_kind TEXT NOT NULL,
  subject_id INTEGER,
  lane_key TEXT NOT NULL,
  claimed_by_agent TEXT NOT NULL REFERENCES agents(name),
  owner_session_id TEXT NOT NULL REFERENCES sessions(session_id),
  status TEXT NOT NULL DEFAULT 'active' CHECK (
    status IN ('active', 'stale', 'released', 'completed', 'closed')
  ),
  intent_note TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  claimed_at TEXT NOT NULL,
  last_renewed_at TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  last_checkpoint_at TEXT NOT NULL,
  closed_at TEXT,
  close_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_one_active_lane
ON claims(lane_key) WHERE status='active';

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER REFERENCES rooms(id),
  item_id INTEGER REFERENCES items(id),
  from_agent TEXT NOT NULL REFERENCES agents(name),
  to_agent TEXT REFERENCES agents(name),
  kind TEXT NOT NULL DEFAULT 'chat',
  checkpoint TEXT,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_offsets (
  agent TEXT PRIMARY KEY REFERENCES agents(name),
  last_message_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  agent TEXT NOT NULL REFERENCES agents(name),
  agent_id TEXT REFERENCES agents(name),
  role TEXT NOT NULL DEFAULT 'owner',
  state TEXT NOT NULL DEFAULT 'active',
  assigned_by TEXT REFERENCES agents(name),
  claimed_at TEXT NOT NULL,
  assigned_at TEXT,
  released_at TEXT,
  release_reason TEXT,
  legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_one_owner ON assignments(item_id)
WHERE role='owner' AND state='active';

CREATE TABLE IF NOT EXISTS receipts (
  receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  claim_id INTEGER NOT NULL REFERENCES claims(claim_id),
  fencing_token INTEGER NOT NULL,
  contract_version INTEGER NOT NULL,
  submitted_by_agent TEXT NOT NULL REFERENCES agents(name),
  submitted_by_session TEXT NOT NULL REFERENCES sessions(session_id),
  summary TEXT NOT NULL,
  proof TEXT NOT NULL,
  proof_references_json TEXT NOT NULL,
  source_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  superseded_at TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  receipt_id INTEGER REFERENCES receipts(receipt_id),
  contract_version INTEGER,
  requested_by TEXT NOT NULL REFERENCES agents(name),
  requested_by_agent TEXT REFERENCES agents(name),
  reviewer TEXT REFERENCES agents(name),
  reviewer_agent_id TEXT REFERENCES agents(name),
  status TEXT NOT NULL DEFAULT 'requested',
  body TEXT,
  verdict_body TEXT,
  legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1)),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS questions (
  question_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  exact_question TEXT NOT NULL,
  asked_by_agent TEXT NOT NULL REFERENCES agents(name),
  asked_by_session TEXT NOT NULL REFERENCES sessions(session_id),
  assigned_to_agent TEXT NOT NULL REFERENCES agents(name),
  status TEXT NOT NULL DEFAULT 'open' CHECK (
    status IN ('open', 'answered', 'withdrawn')
  ),
  answer TEXT,
  answered_by_agent TEXT REFERENCES agents(name),
  answered_by_session TEXT REFERENCES sessions(session_id),
  asked_at TEXT NOT NULL,
  answered_at TEXT,
  preferred_resume_owner_agent_id TEXT REFERENCES agents(name),
  resume_grace_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS handoffs (
  handoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  claim_id INTEGER NOT NULL REFERENCES claims(claim_id),
  from_agent TEXT NOT NULL REFERENCES agents(name),
  from_session TEXT NOT NULL REFERENCES sessions(session_id),
  execution_fencing_token INTEGER NOT NULL,
  to_agent TEXT NOT NULL REFERENCES agents(name),
  reason TEXT NOT NULL,
  summary TEXT NOT NULL,
  completed_work TEXT NOT NULL,
  remaining_work TEXT NOT NULL,
  risks TEXT NOT NULL,
  proof_references TEXT NOT NULL,
  suggested_next_action TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN ('pending', 'accepted', 'declined', 'withdrawn')
  ),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS debates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER REFERENCES items(id),
  topic TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  judge TEXT REFERENCES agents(name),
  created_by TEXT NOT NULL REFERENCES agents(name),
  created_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS debate_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  debate_id INTEGER NOT NULL REFERENCES debates(id),
  round INTEGER NOT NULL DEFAULT 1,
  agent TEXT NOT NULL REFERENCES agents(name),
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER REFERENCES items(id),
  debate_id INTEGER REFERENCES debates(id),
  text TEXT NOT NULL,
  rationale TEXT,
  decided_by TEXT NOT NULL REFERENCES agents(name),
  decided_by_agent TEXT REFERENCES agents(name),
  decided_by_session TEXT REFERENCES sessions(session_id),
  claim_id INTEGER REFERENCES claims(claim_id),
  fencing_token INTEGER,
  legacy INTEGER NOT NULL DEFAULT 0 CHECK (legacy IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER REFERENCES items(id),
  event_type TEXT NOT NULL,
  actor_agent_id TEXT REFERENCES agents(name),
  actor_session_id TEXT REFERENCES sessions(session_id),
  claim_id INTEGER REFERENCES claims(claim_id),
  fencing_token INTEGER,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_entries (
  inbox_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient_agent_id TEXT NOT NULL REFERENCES agents(name),
  source_event_id INTEGER NOT NULL REFERENCES events(event_id),
  item_id INTEGER REFERENCES items(id),
  category TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_offsets (
  agent_id TEXT PRIMARY KEY REFERENCES agents(name),
  last_consumed_entry_id INTEGER NOT NULL DEFAULT 0
);
"""

V3_TABLES = (
    "agent_offsets",
    "agents",
    "assignments",
    "claims",
    "debate_posts",
    "debates",
    "decisions",
    "events",
    "handoffs",
    "inbox_entries",
    "inbox_offsets",
    "items",
    "messages",
    "questions",
    "receipts",
    "reviews",
    "rooms",
    "sessions",
)


class V3Board(TemporaryBoard):
    """A populated version 3 board: every table holds at least one row, the
    session is terminal, item 1 carries the legacy 'needs-input' status, and
    no reserved human actor exists (unless human=True)."""

    def __init__(self, *, human=False, running=False):
        self._with_human = human
        self._running = running

    def __enter__(self):
        super().__enter__()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(V3_SCHEMA)
            t = "2026-07-15T09:{:02d}:00+00:00".format
            conn.executemany(
                "INSERT INTO agents(name,kind,provider,registered_at) "
                "VALUES (?,?,?,?)",
                (
                    ("claude", None, "claude", t(0)),
                    ("codex", None, "codex", t(1)),
                ),
            )
            if self._with_human:
                conn.execute(
                    "INSERT INTO agents(name,provider,registered_at) "
                    "VALUES ('human',NULL,?)",
                    (t(2),),
                )
            conn.execute(
                "INSERT INTO rooms(name,created_at) VALUES ('#general',?)", (t(3),)
            )
            conn.executemany(
                "INSERT INTO items(title,objective,scope,done_when,context,"
                "allowed_actions,stop_conditions,status,owner_agent_id,"
                "next_actor_agent_id,preferred_resume_owner_agent_id,"
                "created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        "Blocked on a question",
                        "obj",
                        "scope",
                        "dw",
                        "ctx",
                        "[]",
                        "[]",
                        "needs-input",
                        "claude",
                        "codex",
                        "claude",
                        "claude",
                        t(4),
                        t(5),
                    ),
                    (
                        "Queued work",
                        None,
                        None,
                        None,
                        "legacy context",
                        None,
                        None,
                        "todo",
                        None,
                        None,
                        None,
                        "codex",
                        t(6),
                        t(6),
                    ),
                ),
            )
            session_status = "running" if self._running else "exited"
            conn.execute(
                "INSERT INTO sessions(session_id,agent_id,provider,status,"
                "command_json,working_directory,started_at,last_seen_at,"
                "exited_at,exit_code,termination_reason,max_runtime_seconds,"
                "shutdown_grace_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "sess-1",
                    "claude",
                    "claude",
                    session_status,
                    '["claude"]',
                    "/work",
                    t(7),
                    t(8),
                    None if self._running else t(9),
                    None if self._running else 0,
                    None if self._running else "child_exit",
                    28800,
                    10,
                ),
            )
            conn.execute(
                "INSERT INTO claims(item_id,claim_kind,subject_id,lane_key,"
                "claimed_by_agent,owner_session_id,status,intent_note,"
                "fencing_token,claimed_at,last_renewed_at,lease_expires_at,"
                "last_checkpoint_at,closed_at,close_reason) "
                "VALUES (1,'implementation',NULL,'implementation:item:1',"
                "'claude','sess-1','closed','work item 1',1,?,?,?,?,?,"
                "'needs_input')",
                (t(10), t(11), t(12), t(11), t(13)),
            )
            conn.execute(
                "INSERT INTO messages(room_id,item_id,from_agent,to_agent,kind,"
                "body,created_at) VALUES (1,1,'claude','codex','chat','hello',?)",
                (t(14),),
            )
            conn.execute(
                "INSERT INTO agent_offsets(agent,last_message_id) "
                "VALUES ('codex',1)"
            )
            conn.execute(
                "INSERT INTO assignments(item_id,agent,agent_id,role,state,"
                "assigned_by,claimed_at,assigned_at,legacy) "
                "VALUES (1,'claude','claude','owner','active',NULL,?,?,1)",
                (t(15), t(15)),
            )
            conn.execute(
                "INSERT INTO receipts(item_id,claim_id,fencing_token,"
                "contract_version,submitted_by_agent,submitted_by_session,"
                "summary,proof,proof_references_json,source_path,sha256,"
                "created_at) VALUES (1,1,1,1,'claude','sess-1','sum','proof',"
                "'[]','/work/receipt.md','deadbeef',?)",
                (t(16),),
            )
            conn.execute(
                "INSERT INTO reviews(item_id,requested_by,requested_by_agent,"
                "reviewer,reviewer_agent_id,status,body,verdict_body,legacy,"
                "created_at) VALUES (1,'claude','claude','codex','codex',"
                "'requested','b','b',1,?)",
                (t(17),),
            )
            conn.execute(
                "INSERT INTO questions(item_id,exact_question,asked_by_agent,"
                "asked_by_session,assigned_to_agent,status,asked_at) "
                "VALUES (1,'What next?','claude','sess-1','codex','open',?)",
                (t(18),),
            )
            conn.execute(
                "INSERT INTO handoffs(item_id,claim_id,from_agent,from_session,"
                "execution_fencing_token,to_agent,reason,summary,completed_work,"
                "remaining_work,risks,proof_references,suggested_next_action,"
                "status,created_at,resolved_at) VALUES (1,1,'claude','sess-1',"
                "1,'codex','r','s','c','rw','ri','[]','next','declined',?,?)",
                (t(19), t(20)),
            )
            conn.execute(
                "INSERT INTO debates(item_id,topic,status,judge,created_by,"
                "created_at,closed_at) VALUES (1,'Topic','closed','codex',"
                "'claude',?,?)",
                (t(21), t(22)),
            )
            conn.execute(
                "INSERT INTO debate_posts(debate_id,round,agent,body,created_at) "
                "VALUES (1,1,'codex','post',?)",
                (t(23),),
            )
            conn.execute(
                "INSERT INTO decisions(item_id,debate_id,text,rationale,"
                "decided_by,decided_by_agent,legacy,created_at) "
                "VALUES (1,1,'ruling','because','codex','codex',1,?)",
                (t(24),),
            )
            conn.execute(
                "INSERT INTO events(item_id,event_type,actor_agent_id,"
                "actor_session_id,claim_id,payload_json,created_at) "
                "VALUES (1,'item_claimed','claude','sess-1',1,'{}',?)",
                (t(25),),
            )
            conn.execute(
                "INSERT INTO inbox_entries(recipient_agent_id,source_event_id,"
                "item_id,category,payload_json,created_at) "
                "VALUES ('codex',1,1,'assignment','{}',?)",
                (t(26),),
            )
            conn.execute(
                "INSERT INTO inbox_offsets(agent_id,last_consumed_entry_id) "
                "VALUES ('codex',0)"
            )
            conn.execute(f"PRAGMA application_id = {coop_schema.APPLICATION_ID}")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        self.expected_counts = self.row_counts()
        return self

    def row_counts(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in V3_TABLES
            }

    def dump_tables(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return {
                table: tuple(
                    conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
                )
                for table in V3_TABLES
            }

    def source_snapshot(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return coop_schema.identity(conn), self.dump_tables()


class TestMigrationCLI(unittest.TestCase):
    def test_migrate_command_requires_confirmation(self):
        with LegacyBoard() as board:
            result = run_coop("--db", board.path, "migrate")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("legacy clients", result.stderr)

    def test_migrate_command_prints_verified_backup_path(self):
        with LegacyBoard() as board:
            result = run_coop(
                "--db",
                board.path,
                "migrate",
                "--confirm-legacy-clients-stopped",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            prefix = f"migrated schema 0 -> {coop_schema.SCHEMA_VERSION} backup="
            output = result.stdout.rstrip("\n")
            self.assertTrue(output.startswith(prefix), output)
            self.assertNotIn("\n", output)
            backup = pathlib.Path(output.removeprefix(prefix))
            self.assertTrue(backup.is_file(), backup)
            with contextlib.closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(
                    saved.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(coop_schema.identity(saved), (0, 0))

    def test_normal_command_rejects_legacy_schema(self):
        with LegacyBoard() as board:
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertEqual(
                    conn.execute("PRAGMA journal_mode").fetchone()[0], "delete"
                )
            result = run_coop("--db", board.path, "status")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("coop migrate", result.stderr)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertEqual(
                    conn.execute("PRAGMA journal_mode").fetchone()[0], "delete"
                )
            self.assertFalse(pathlib.Path(f"{board.path}-wal").exists())


class TestCurrentSchema(unittest.TestCase):
    def test_fresh_init_sets_identity_and_creates_phase_one_tables(self):
        with TemporaryBoard() as board, contextlib.closing(
            coopdb.connect(board.path)
        ) as conn:
            coopdb.init_db(conn)
            self.assertEqual(
                conn.execute("PRAGMA application_id").fetchone()[0], 0x434F4F50
            )
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                coop_schema.SCHEMA_VERSION,
            )
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
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
                <= names
            )

    def test_normal_guard_rejects_uninitialized_database(self):
        with TemporaryBoard() as board, contextlib.closing(
            coopdb.connect(board.path)
        ) as conn:
            guard = getattr(coopdb, "require_current_schema", None)
            self.assertIsNotNone(guard, "require_current_schema is not implemented")
            with self.assertRaisesRegex(coopdb.CoopError, "coop init|coop migrate"):
                guard(conn)

    def test_guarded_connect_closes_on_schema_guard_failure(self):
        connection = mock.Mock()
        with (
            mock.patch.object(
                coopdb.sqlite3, "connect", return_value=connection
            ),
            mock.patch.object(
                coopdb,
                "require_current_schema",
                side_effect=coopdb.CoopError("board schema is not current"),
            ),
        ):
            with self.assertRaisesRegex(coopdb.CoopError, "not current"):
                coopdb.connect("legacy.db", require_current=True)

        self.assertEqual(
            connection.execute.call_args_list,
            [
                mock.call("PRAGMA busy_timeout=5000"),
                mock.call("PRAGMA foreign_keys=ON"),
            ],
        )
        connection.close.assert_called_once_with()

    def test_init_refuses_a_legacy_database(self):
        with LegacyBoard() as board, contextlib.closing(
            coopdb.connect(board.path)
        ) as conn:
            with self.assertRaisesRegex(coopdb.CoopError, "coop migrate"):
                coopdb.init_db(conn)

    def test_init_refuses_an_unknown_nonempty_database(self):
        with TemporaryBoard() as board, contextlib.closing(
            coopdb.connect(board.path)
        ) as conn:
            conn.execute("CREATE VIEW marker AS SELECT 1")
            conn.commit()
            with self.assertRaisesRegex(coopdb.CoopError, "not an empty"):
                coopdb.init_db(conn)

    def test_init_serializes_classification_against_competing_schema_write(self):
        with (
            TemporaryBoard() as board,
            contextlib.closing(coopdb.connect(board.path)) as initializer,
            contextlib.closing(coopdb.connect(board.path)) as competitor,
        ):
            competitor.execute("PRAGMA busy_timeout=0")
            original_classify = coop_schema._user_schema_objects
            competing_outcomes = []

            def classify_with_competing_write(conn):
                schema_objects = original_classify(conn)
                self.assertEqual(schema_objects, set())
                try:
                    competitor.execute("CREATE TABLE intruder(x)")
                    competitor.commit()
                except sqlite3.OperationalError as exc:
                    competitor.rollback()
                    if "locked" not in str(exc).lower():
                        raise
                    competing_outcomes.append("locked")
                else:
                    competing_outcomes.append("committed")
                return schema_objects

            with mock.patch.object(
                coop_schema,
                "_user_schema_objects",
                side_effect=classify_with_competing_write,
            ):
                coopdb.init_db(initializer)

            names = {
                row[0]
                for row in initializer.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
            mixed_schema_was_stamped_current = (
                "intruder" in names
                and coop_schema.identity(initializer)
                == (coop_schema.APPLICATION_ID, coop_schema.SCHEMA_VERSION)
            )
            self.assertFalse(
                mixed_schema_was_stamped_current,
                "a competing unknown table was stamped as part of the current schema",
            )
            self.assertEqual(competing_outcomes, ["locked"])


class TestLegacyMigration(unittest.TestCase):
    def _require_migrate(self):
        migrate = getattr(coopdb, "migrate_db", None)
        self.assertIsNotNone(migrate, "migrate_db is not implemented")
        return migrate

    def test_migration_requires_explicit_quiescence_acknowledgement(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            with self.assertRaisesRegex(coopdb.CoopError, "legacy clients"):
                migrate(board.path)

    def test_migration_backs_up_and_preserves_legacy_rows(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            backup = migrate(
                board.path, confirm_legacy_clients_stopped=True
            )
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.parent, board.path.parent)

            with contextlib.closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(
                    saved.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(coop_schema.identity(saved), (0, 0))
                self.assertEqual(
                    {
                        table: saved.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        for table in LEGACY_TABLES
                    },
                    board.expected_counts,
                )

            with contextlib.closing(coopdb.connect(board.path)) as conn:
                coopdb.require_current_schema(conn)
                # Version 4 seeds the reserved human actor: agents gains
                # exactly one row; every other preserved count is unchanged.
                expected = dict(board.expected_counts)
                expected["agents"] += 1
                self.assertEqual(
                    {
                        table: conn.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        for table in LEGACY_TABLES
                    },
                    expected,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM agents WHERE name='human' "
                        "AND provider IS NULL"
                    ).fetchone()[0],
                    1,
                )
                for table in (
                    "sessions",
                    "claims",
                    "questions",
                    "receipts",
                    "handoffs",
                    "inbox_entries",
                    "inbox_offsets",
                ):
                    self.assertEqual(
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                        table,
                    )
                events = conn.execute("SELECT * FROM events").fetchall()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_type"], "schema_migrated")
                self.assertIsNone(events[0]["actor_agent_id"])
                self.assertIsNone(events[0]["actor_session_id"])
                self.assertIsNone(events[0]["claim_id"])
                payload = json.loads(events[0]["payload_json"])
                self.assertEqual(payload["source_schema_version"], 0)
                self.assertEqual(
                    payload["target_schema_version"], coop_schema.SCHEMA_VERSION
                )
                self.assertEqual(
                    pathlib.Path(payload["backup_path"]), backup.resolve()
                )
                self.assertEqual(
                    payload["preserved_row_counts"], board.expected_counts
                )
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )

    def test_migration_backfills_only_an_exact_owner(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            migrate(board.path, confirm_legacy_clients_stopped=True)
            with contextlib.closing(coopdb.connect(board.path)) as conn:
                rows = conn.execute(
                    "SELECT id,status,owner_agent_id,context,objective "
                    "FROM items ORDER BY id"
                ).fetchall()
                self.assertEqual(rows[0]["status"], "done")
                self.assertEqual(rows[0]["owner_agent_id"], "codex")
                self.assertEqual(rows[0]["context"], "legacy body")
                self.assertIsNone(rows[0]["objective"])
                self.assertIsNone(rows[1]["owner_agent_id"])
                self.assertEqual(rows[1]["context"], "classification context")

    def test_migration_maps_legacy_compatibility_fields_without_new_authority(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            migrate(board.path, confirm_legacy_clients_stopped=True)
            with contextlib.closing(coopdb.connect(board.path)) as conn:
                providers = {
                    row["name"]: row["provider"]
                    for row in conn.execute(
                        "SELECT name,provider FROM agents ORDER BY name"
                    )
                }
                self.assertEqual(providers["claude"], "claude")
                self.assertEqual(providers["codex"], "codex")
                self.assertEqual(providers["grok"], "grok")
                self.assertIsNone(providers["custom-agent"])

                assignments = conn.execute(
                    "SELECT agent,agent_id,role,state,claimed_at,assigned_at,legacy "
                    "FROM assignments ORDER BY id"
                ).fetchall()
                self.assertEqual(
                    [row["agent_id"] for row in assignments],
                    ["codex", "custom-agent"],
                )
                self.assertEqual(
                    [row["assigned_at"] for row in assignments],
                    [row["claimed_at"] for row in assignments],
                )
                self.assertEqual(assignments[1]["role"], "classifier")
                self.assertEqual(assignments[1]["state"], "active")
                self.assertEqual([row["legacy"] for row in assignments], [1, 1])

                review = conn.execute("SELECT * FROM reviews").fetchone()
                self.assertEqual(review["requested_by_agent"], "codex")
                self.assertEqual(review["reviewer_agent_id"], "grok")
                self.assertEqual(review["verdict_body"], "legacy verdict")
                self.assertEqual(review["legacy"], 1)
                self.assertIsNone(review["receipt_id"])
                self.assertIsNone(review["contract_version"])

                decision = conn.execute("SELECT * FROM decisions").fetchone()
                self.assertEqual(decision["decided_by_agent"], "grok")
                self.assertEqual(decision["legacy"], 1)
                self.assertIsNone(decision["decided_by_session"])
                self.assertIsNone(decision["claim_id"])
                self.assertIsNone(decision["fencing_token"])

                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_schema "
                        "WHERE type='index' AND name='ux_one_owner'"
                    ).fetchone()
                )

    def test_migration_rejects_an_unknown_nonzero_application_id(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                conn.execute("PRAGMA application_id = 12345")
                conn.commit()
            with self.assertRaisesRegex(coopdb.CoopError, "application ID"):
                migrate(board.path, confirm_legacy_clients_stopped=True)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertEqual(coop_schema.identity(conn), (12345, 0))

    def test_migration_rejects_a_running_recorded_session(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                conn.execute("CREATE TABLE sessions(status TEXT NOT NULL)")
                conn.execute("INSERT INTO sessions(status) VALUES ('running')")
                conn.commit()
            with self.assertRaisesRegex(coopdb.CoopError, "running session"):
                migrate(board.path, confirm_legacy_clients_stopped=True)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertEqual(coop_schema.identity(conn), (0, 0))
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1
                )

    def test_migration_refuses_an_existing_backup_target(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            backup = board.path.with_name("existing-backup.db")
            backup.write_bytes(b"do not overwrite")
            before = board.source_snapshot()
            with self.assertRaisesRegex(coopdb.CoopError, "backup target.*exists"):
                migrate(
                    board.path,
                    backup_path=backup,
                    confirm_legacy_clients_stopped=True,
                )
            self.assertEqual(backup.read_bytes(), b"do not overwrite")
            self.assertEqual(board.source_snapshot(), before)

    def test_backup_verification_failure_leaves_source_identity_and_data_unchanged(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            backup = board.path.with_name("unverified-backup.db")
            before = board.source_snapshot()
            with mock.patch.object(
                coop_schema,
                "_verify_backup",
                side_effect=coop_schema.SchemaError(
                    "backup integrity check failed"
                ),
                create=True,
            ):
                with self.assertRaisesRegex(
                    coopdb.CoopError, "backup integrity check failed"
                ):
                    migrate(
                        board.path,
                        backup_path=backup,
                        confirm_legacy_clients_stopped=True,
                    )
            self.assertEqual(board.source_snapshot(), before)

    def test_missing_backup_during_real_verification_is_not_recreated(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            backup = board.path.with_name("removed-before-verification.db")
            before = board.source_snapshot()
            real_verify = coop_schema._verify_backup
            missing_at_verification = []

            def remove_then_verify(path, *args, **kwargs):
                path.unlink()
                missing_at_verification.append(not path.exists())
                return real_verify(path, *args, **kwargs)

            with mock.patch.object(
                coop_schema,
                "_verify_backup",
                side_effect=remove_then_verify,
            ):
                with self.assertRaisesRegex(
                    coopdb.CoopError,
                    "backup.*(missing|open|verification|integrity)",
                ):
                    migrate(
                        board.path,
                        backup_path=backup,
                        confirm_legacy_clients_stopped=True,
                    )

            self.assertEqual(missing_at_verification, [True])
            self.assertFalse(backup.exists())
            self.assertEqual(board.source_snapshot(), before)

    def test_same_shape_decoy_backup_is_rejected_before_source_mutation(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board, LegacyBoard() as decoy:
            backup = board.path.with_name("same-shape-decoy.db")
            before = board.source_snapshot()
            with contextlib.closing(sqlite3.connect(decoy.path)) as conn:
                conn.execute(
                    "UPDATE items SET title='DECOY CONTENT' WHERE id=1"
                )
                conn.commit()
            real_verify = coop_schema._verify_backup
            substituted = []

            def substitute_then_verify(path, *args, **kwargs):
                shutil.copyfile(decoy.path, path)
                substituted.append(path)
                return real_verify(path, *args, **kwargs)

            with mock.patch.object(
                coop_schema,
                "_verify_backup",
                side_effect=substitute_then_verify,
            ):
                with self.assertRaisesRegex(
                    coopdb.CoopError,
                    "backup verification.*(content|source)",
                ):
                    migrate(
                        board.path,
                        backup_path=backup,
                        confirm_legacy_clients_stopped=True,
                    )

            self.assertEqual(len(substituted), 1)
            self.assertFalse(backup.exists())
            self.assertEqual(board.source_snapshot(), before)

    def test_explicit_backup_target_race_does_not_clobber_new_file(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            backup = board.path.with_name("racing-backup.db")
            before = board.source_snapshot()
            real_create = coop_schema._create_verified_backup

            def create_sentinel_then_backup(source, path, *args, **kwargs):
                with contextlib.closing(sqlite3.connect(path)) as sentinel:
                    sentinel.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
                    sentinel.execute(
                        "INSERT INTO sentinel(value) VALUES ('preserve me')"
                    )
                    sentinel.commit()
                return real_create(source, path, *args, **kwargs)

            with mock.patch.object(
                coop_schema,
                "_create_verified_backup",
                side_effect=create_sentinel_then_backup,
            ):
                with self.assertRaisesRegex(
                    coopdb.CoopError, "backup target.*exists"
                ):
                    migrate(
                        board.path,
                        backup_path=backup,
                        confirm_legacy_clients_stopped=True,
                    )

            with contextlib.closing(sqlite3.connect(backup)) as sentinel:
                self.assertEqual(
                    sentinel.execute("SELECT value FROM sentinel").fetchone()[0],
                    "preserve me",
                )
            self.assertEqual(board.source_snapshot(), before)

    def test_backup_target_substitution_before_publish_is_not_clobbered(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            backup = board.path.with_name("post-reservation-race.db")
            before = board.source_snapshot()
            real_connect = sqlite3.connect
            real_link = os.link
            final_uri = backup.resolve().as_uri()
            final_path_open_races = []
            publication_races = []

            def replace_with_sentinel(path):
                path.unlink(missing_ok=True)
                with contextlib.closing(real_connect(path)) as sentinel:
                    sentinel.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
                    sentinel.execute(
                        "INSERT INTO sentinel(value) VALUES ('preserve me')"
                    )
                    sentinel.commit()

            def connect_after_target_substitution(database, *args, **kwargs):
                if (
                    isinstance(database, str)
                    and database.startswith(final_uri)
                    and "mode=rw" in database
                ):
                    replace_with_sentinel(backup)
                    final_path_open_races.append(backup)
                return real_connect(database, *args, **kwargs)

            def link_after_target_substitution(source, target, *args, **kwargs):
                target = pathlib.Path(target)
                replace_with_sentinel(target)
                publication_races.append(target)
                return real_link(source, target, *args, **kwargs)

            with (
                mock.patch.object(
                    coop_schema.sqlite3,
                    "connect",
                    side_effect=connect_after_target_substitution,
                ),
                mock.patch("os.link", side_effect=link_after_target_substitution),
            ):
                with self.assertRaisesRegex(
                    coopdb.CoopError, "backup target.*exists"
                ):
                    migrate(
                        board.path,
                        backup_path=backup,
                        confirm_legacy_clients_stopped=True,
                    )

            self.assertEqual(final_path_open_races, [])
            self.assertEqual(publication_races, [backup])
            with contextlib.closing(real_connect(backup)) as sentinel:
                self.assertEqual(
                    sentinel.execute("SELECT value FROM sentinel").fetchone()[0],
                    "preserve me",
                )
            self.assertEqual(board.source_snapshot(), before)

    def test_migration_rejects_schema_incomplete_lookalike(self):
        migrate = self._require_migrate()
        with TemporaryBoard() as board:
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                conn.executescript(LOOKALIKE_SCHEMA)
                conn.commit()
            before = database_snapshot(board.path)

            with self.assertRaisesRegex(coopdb.CoopError, "pre-release schema"):
                migrate(board.path, confirm_legacy_clients_stopped=True)

            self.assertEqual(database_snapshot(board.path), before)

    def test_mutation_failure_rolls_back_source_changes(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            before = board.source_snapshot()

            def fail_after_source_write(conn, *args, **kwargs):
                conn.execute("UPDATE items SET title='must roll back' WHERE id=1")
                raise coop_schema.SchemaError("injected migration failure")

            with mock.patch.object(
                coop_schema,
                "_apply_legacy_migration",
                side_effect=fail_after_source_write,
                create=True,
            ):
                with self.assertRaisesRegex(
                    coopdb.CoopError, "injected migration failure"
                ):
                    migrate(board.path, confirm_legacy_clients_stopped=True)
            self.assertEqual(board.source_snapshot(), before)

    def test_foreign_key_failure_rolls_back_before_identity_stamp(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                conn.execute(
                    "INSERT INTO messages(room_id,from_agent,kind,body,created_at) "
                    "VALUES (1,'missing-agent','chat','invalid legacy row',?)",
                    ("2026-07-15T08:17:00+00:00",),
                )
                conn.commit()
            before = board.source_snapshot()

            with self.assertRaisesRegex(coopdb.CoopError, "foreign key check"):
                migrate(board.path, confirm_legacy_clients_stopped=True)

            self.assertEqual(board.source_snapshot(), before)

    def test_post_commit_verification_failure_clears_current_identity(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board:
            with mock.patch.object(
                coop_schema,
                "_post_commit_checks",
                side_effect=coop_schema.SchemaError(
                    "injected post-commit integrity check failure"
                ),
                create=True,
            ):
                with self.assertRaisesRegex(
                    coopdb.CoopError,
                    "injected post-commit integrity check failure",
                ):
                    migrate(board.path, confirm_legacy_clients_stopped=True)

            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertNotEqual(
                    coop_schema.identity(conn),
                    (coop_schema.APPLICATION_ID, coop_schema.SCHEMA_VERSION),
                )

    def test_migration_serializes_classification_under_an_exclusive_lock(self):
        migrate = self._require_migrate()
        with LegacyBoard() as board, contextlib.closing(
            sqlite3.connect(board.path)
        ) as competitor:
            competitor.execute("PRAGMA busy_timeout=0")
            original_classify = coop_schema._user_schema_objects
            competing_outcomes = []

            def classify_with_competing_write(conn):
                schema_objects = original_classify(conn)
                try:
                    competitor.execute("CREATE TABLE intruder(x)")
                    competitor.commit()
                except sqlite3.OperationalError as exc:
                    competitor.rollback()
                    if "locked" not in str(exc).lower():
                        raise
                    competing_outcomes.append("locked")
                else:
                    competing_outcomes.append("committed")
                return schema_objects

            with mock.patch.object(
                coop_schema,
                "_user_schema_objects",
                side_effect=classify_with_competing_write,
            ):
                migrate(board.path, confirm_legacy_clients_stopped=True)

            self.assertEqual(competing_outcomes, ["locked"])
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name='intruder'"
                    ).fetchone()
                )
                self.assertEqual(
                    coop_schema.identity(conn),
                    (coop_schema.APPLICATION_ID, coop_schema.SCHEMA_VERSION),
                )


class TestV4Migration(unittest.TestCase):
    def _migrate(self, board, **kw):
        kw.setdefault("confirm_legacy_clients_stopped", True)
        return coopdb.migrate_db(board.path, **kw)

    def _fresh_v4_fingerprint(self):
        with TemporaryBoard() as board, contextlib.closing(
            coopdb.connect(board.path)
        ) as conn:
            coopdb.init_db(conn)
            return coop_schema.semantic_fingerprint(conn)

    def test_fresh_v4_fingerprint_equals_migrated_v3_fingerprint(self):
        fresh = self._fresh_v4_fingerprint()
        with V3Board() as board:
            self._migrate(board)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertEqual(coop_schema.semantic_fingerprint(conn), fresh)

    def test_fresh_v4_fingerprint_equals_migrated_v0_fingerprint(self):
        fresh = self._fresh_v4_fingerprint()
        with LegacyBoard() as board:
            self._migrate(board)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertEqual(coop_schema.semantic_fingerprint(conn), fresh)

    def test_v3_migration_delta_seeds_exactly_one_human_agent(self):
        with V3Board() as board:
            before = board.dump_tables()
            self.assertNotIn(
                "human", [row[0] for row in before["agents"]]
            )
            self._migrate(board)
            after = board.dump_tables()
            self.assertEqual(
                len(after["agents"]), len(before["agents"]) + 1
            )
            human = [row for row in after["agents"] if row[0] == "human"]
            self.assertEqual(len(human), 1)
            self.assertIsNone(human[0][2], "human provider must be NULL")
            self.assertEqual(
                [row for row in after["agents"] if row[0] != "human"],
                list(before["agents"]),
                "pre-existing agent rows must be byte-identical",
            )

    def test_v3_migration_delta_appends_exactly_one_migration_event(self):
        with V3Board() as board:
            before = board.dump_tables()
            backup = self._migrate(board)
            after = board.dump_tables()
            self.assertEqual(len(after["events"]), len(before["events"]) + 1)
            self.assertEqual(
                list(after["events"][: len(before["events"])]),
                list(before["events"]),
                "pre-existing event rows must be byte-identical",
            )
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                conn.row_factory = sqlite3.Row
                event = conn.execute(
                    "SELECT * FROM events WHERE event_type='schema_migrated'"
                ).fetchone()
                payload = json.loads(event["payload_json"])
                self.assertEqual(payload["source_schema_version"], 3)
                self.assertEqual(payload["target_schema_version"], 4)
                self.assertEqual(
                    pathlib.Path(payload["backup_path"]), backup.resolve()
                )
                self.assertEqual(
                    payload["preserved_row_counts"], board.expected_counts
                )

    def test_v3_migration_delta_normalizes_needs_input_status(self):
        with V3Board() as board:
            before = board.dump_tables()
            self._migrate(board)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                conn.row_factory = sqlite3.Row
                statuses = {
                    row["id"]: row["status"]
                    for row in conn.execute("SELECT id,status FROM items")
                }
                self.assertEqual(statuses[1], "needs_input")
                self.assertEqual(statuses[2], "todo")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM items WHERE status='needs-input'"
                    ).fetchone()[0],
                    0,
                )
                first = conn.execute(
                    "SELECT * FROM items WHERE id=1"
                ).fetchone()
                original = dict(
                    zip([d[0] for d in conn.execute(
                        "SELECT * FROM items LIMIT 0"
                    ).description], before["items"][0])
                )
                for key in original:
                    if key in ("status", "output_contract",
                               "resume_grace_started_at"):
                        continue
                    self.assertEqual(
                        first[key], original[key],
                        f"items.{key} must survive normalization untouched",
                    )

    def test_v3_migration_preserves_every_other_row_byte_identical(self):
        # agents (+human), events (+migration event), and items (status
        # normalization + two new NULL columns) are the ONLY permitted deltas;
        # every other table's rows must come through byte-identical.
        with V3Board() as board:
            before = board.dump_tables()
            self._migrate(board)
            after = board.dump_tables()
            untouched = set(V3_TABLES) - {"agents", "events", "items"}
            for table in sorted(untouched):
                self.assertEqual(
                    after[table], before[table],
                    f"{table} rows must be byte-identical after migration",
                )
            # items: identical apart from the normalized status and the two
            # appended NULL columns.
            for old_row, new_row in zip(before["items"], after["items"]):
                self.assertEqual(new_row[-2:], (None, None))
                trimmed = new_row[:-2]
                if old_row[9] == "needs-input":  # status column position
                    self.assertEqual(trimmed[9], "needs_input")
                    self.assertEqual(trimmed[:9], old_row[:9])
                    self.assertEqual(trimmed[10:], old_row[10:])
                else:
                    self.assertEqual(trimmed, old_row)

    def test_v3_migration_human_seed_is_idempotent(self):
        with V3Board(human=True) as board:
            agents_before = board.expected_counts["agents"]
            self._migrate(board)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
                    agents_before,
                    "a pre-existing human row must not be duplicated",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM agents WHERE name='human'"
                    ).fetchone()[0],
                    1,
                )

    def test_v3_migration_requires_quiescence_acknowledgement(self):
        with V3Board() as board:
            before = board.source_snapshot()
            with self.assertRaisesRegex(coopdb.CoopError, "legacy clients"):
                coopdb.migrate_db(board.path)
            self.assertEqual(board.source_snapshot(), before)

    def test_v3_migration_refuses_running_session(self):
        with V3Board(running=True) as board:
            before = board.source_snapshot()
            with self.assertRaisesRegex(coopdb.CoopError, "running session"):
                self._migrate(board)
            self.assertEqual(board.source_snapshot(), before)
            self.assertEqual(
                before[0], (coop_schema.APPLICATION_ID, 3)
            )

    def test_v3_migration_creates_verified_backup_before_mutation(self):
        with V3Board() as board:
            backup = self._migrate(board)
            self.assertTrue(backup.is_file())
            with contextlib.closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(
                    saved.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    coop_schema.identity(saved),
                    (coop_schema.APPLICATION_ID, 3),
                    "the backup must be the untouched version 3 source",
                )
                self.assertEqual(
                    {
                        table: saved.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        for table in V3_TABLES
                    },
                    board.expected_counts,
                )

    def test_v4_index_swap_on_fresh_and_migrated_boards(self):
        def assert_v4_indexes(conn):
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE type='index' AND name='ux_one_owner'"
                ).fetchone(),
                "ux_one_owner must be dropped on version 4",
            )
            row = conn.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='index' AND name='ux_sessions_one_running'"
            ).fetchone()
            self.assertIsNotNone(row, "ux_sessions_one_running must exist")
            normalized = " ".join(row[0].lower().split())
            self.assertIn("where status='running'", normalized)

        with TemporaryBoard() as board, contextlib.closing(
            coopdb.connect(board.path)
        ) as conn:
            coopdb.init_db(conn)
            assert_v4_indexes(conn)
            conn.execute(
                "INSERT INTO agents(name,registered_at) VALUES ('claude','t')"
            )
            conn.execute(
                "INSERT INTO sessions(session_id,agent_id,provider,status,"
                "command_json,working_directory,started_at,last_seen_at,"
                "max_runtime_seconds,shutdown_grace_seconds) "
                "VALUES ('s1','claude','claude','running','[]','/w','t','t',1,1)"
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sessions(session_id,agent_id,provider,status,"
                    "command_json,working_directory,started_at,last_seen_at,"
                    "max_runtime_seconds,shutdown_grace_seconds) "
                    "VALUES ('s2','claude','claude','running','[]','/w','t','t',1,1)"
                )
            conn.rollback()
            conn.execute(
                "INSERT INTO sessions(session_id,agent_id,provider,status,"
                "command_json,working_directory,started_at,last_seen_at,"
                "max_runtime_seconds,shutdown_grace_seconds) "
                "VALUES ('s3','claude','claude','exited','[]','/w','t','t',1,1)"
            )
            conn.commit()

        with V3Board() as board:
            self._migrate(board)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                assert_v4_indexes(conn)

    def test_identity_stamp_rolls_back_with_the_migration_transaction(self):
        with V3Board() as board:
            before = board.source_snapshot()
            real_stamp = coop_schema._stamp_current_identity

            def stamp_then_crash(conn):
                real_stamp(conn)
                raise coop_schema.SchemaError("injected crash after stamp")

            with mock.patch.object(
                coop_schema,
                "_stamp_current_identity",
                side_effect=stamp_then_crash,
            ):
                with self.assertRaisesRegex(
                    coopdb.CoopError, "injected crash after stamp"
                ):
                    self._migrate(board)
            self.assertEqual(
                board.source_snapshot(),
                before,
                "a crash before commit must leave the source untouched at "
                "its old version, stamp included",
            )

    def test_post_commit_failure_is_migration_failed_naming_backup(self):
        with V3Board() as board:
            backup = board.path.with_name("v3-verified-backup.db")
            with mock.patch.object(
                coop_schema,
                "_post_commit_checks",
                side_effect=coop_schema.SchemaError(
                    "injected post-commit verification failure"
                ),
            ):
                with self.assertRaises(coop_errors.MigrationFailed) as caught:
                    self._migrate(board, backup_path=backup)
            self.assertIsInstance(caught.exception, coopdb.CoopError)
            self.assertIn(str(backup.resolve()), str(caught.exception))
            self.assertIn(
                "injected post-commit verification failure",
                str(caught.exception),
            )
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertNotEqual(
                    coop_schema.identity(conn),
                    (coop_schema.APPLICATION_ID, coop_schema.SCHEMA_VERSION),
                )

    def test_normal_clients_reject_v3_with_migrate_instruction(self):
        with V3Board() as board:
            with self.assertRaisesRegex(coopdb.CoopError, "coop migrate"):
                coopdb.connect(board.path, require_current=True)
            result = run_coop("--db", board.path, "status")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("coop migrate", result.stderr)

    def test_migrate_rejects_current_v4_board(self):
        with TemporaryBoard() as board:
            with contextlib.closing(coopdb.connect(board.path)) as conn:
                coopdb.init_db(conn)
            with self.assertRaisesRegex(coopdb.CoopError, "already current"):
                coopdb.migrate_db(
                    board.path, confirm_legacy_clients_stopped=True
                )

    def test_v0_chain_migrates_directly_to_v4(self):
        with LegacyBoard() as board:
            backup = self._migrate(board)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                conn.row_factory = sqlite3.Row
                self.assertEqual(
                    coop_schema.identity(conn),
                    (coop_schema.APPLICATION_ID, 4),
                )
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(items)")
                }
                self.assertIn("output_contract", columns)
                self.assertIn("resume_grace_started_at", columns)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM agents WHERE name='human' "
                        "AND provider IS NULL"
                    ).fetchone()[0],
                    1,
                )
                event = conn.execute(
                    "SELECT payload_json FROM events "
                    "WHERE event_type='schema_migrated'"
                ).fetchone()
                payload = json.loads(event["payload_json"])
                self.assertEqual(payload["source_schema_version"], 0)
                self.assertEqual(payload["target_schema_version"], 4)
            with contextlib.closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(coop_schema.identity(saved), (0, 0))

    def test_migrate_command_prints_v3_source_version(self):
        with V3Board() as board:
            result = run_coop(
                "--db",
                board.path,
                "migrate",
                "--confirm-legacy-clients-stopped",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                result.stdout.startswith("migrated schema 3 -> 4 backup="),
                result.stdout,
            )


if __name__ == "__main__":
    unittest.main()
