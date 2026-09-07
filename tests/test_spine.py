"""Spine contract: one-transaction mutation, event + delivery
atomicity, event cardinality, the canonical inbox cursor, the injectable
clock, and typed errors crossing the CLI boundary."""

import contextlib
import datetime
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_coop import coop_errors
from agent_coop import coopdb


def run_coop(*args):
    coop_dir = pathlib.Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "coop.py", *(str(arg) for arg in args)],
        cwd=coop_dir,
        capture_output=True,
        text=True,
    )


class SpineBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = pathlib.Path(self._dir.name) / "board.db"
        self.conn = coopdb.connect(self.db)
        self.addCleanup(self.conn.close)
        coopdb.init_db(self.conn)
        coopdb.register_agent(self.conn, "claude")
        coopdb.register_agent(self.conn, "codex")

    def counts(self):
        return {
            table: self.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in ("events", "inbox_entries", "inbox_offsets", "agents")
        }


class TestMutate(SpineBase):
    def test_mutate_commits_state_event_and_delivery_together(self):
        def fn(conn):
            event_id = coopdb.append_event(
                conn,
                item_id=None,
                event_type="spine_probe",
                actor_agent_id="claude",
                actor_session_id=None,
                payload={"k": "v"},
            )
            coopdb.deliver(
                conn,
                recipient="codex",
                source_event_id=event_id,
                item_id=None,
                category="probe",
                payload={"k": "v"},
            )
            return event_id

        event_id = coopdb.mutate(self.conn, fn)
        row = self.conn.execute(
            "SELECT event_type, payload_json FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual(row["event_type"], "spine_probe")
        self.assertEqual(json.loads(row["payload_json"]), {"k": "v"})
        entry = self.conn.execute(
            "SELECT recipient_agent_id, source_event_id FROM inbox_entries"
        ).fetchone()
        self.assertEqual(entry["recipient_agent_id"], "codex")
        self.assertEqual(entry["source_event_id"], event_id)

    def test_mutate_rolls_back_everything_on_failure(self):
        before = self.counts()

        def fn(conn):
            event_id = coopdb.append_event(
                conn,
                item_id=None,
                event_type="doomed",
                actor_agent_id="claude",
                actor_session_id=None,
                payload={},
            )
            coopdb.deliver(
                conn,
                recipient="codex",
                source_event_id=event_id,
                item_id=None,
                category="doomed",
                payload={},
            )
            raise coop_errors.InvalidTransition("injected failure")

        with self.assertRaisesRegex(coopdb.CoopError, "injected failure"):
            coopdb.mutate(self.conn, fn)
        self.assertEqual(
            self.counts(), before,
            "a failing fn must leave zero event and zero delivery rows",
        )

    def test_mutate_returns_fn_result(self):
        self.assertEqual(coopdb.mutate(self.conn, lambda conn: 42), 42)

    def test_delivery_requires_its_event(self):
        before = self.counts()

        def fn(conn):
            coopdb.deliver(
                conn,
                recipient="codex",
                source_event_id=99999,
                item_id=None,
                category="orphan",
                payload={},
            )

        with self.assertRaises(sqlite3.IntegrityError):
            coopdb.mutate(self.conn, fn)
        self.assertEqual(self.counts(), before)


class TestEventCardinality(SpineBase):
    def test_single_transition_appends_exactly_one_event(self):
        def fn(conn):
            coopdb.append_event(
                conn,
                item_id=None,
                event_type="single",
                actor_agent_id="claude",
                actor_session_id=None,
                payload={},
            )

        coopdb.mutate(self.conn, fn)
        self.assertEqual(self.counts()["events"], 1)
        self.assertEqual(self.counts()["inbox_entries"], 0)

    def test_batch_appends_one_event_and_delivery_per_affected_row(self):
        affected = ("a1", "a2", "a3")

        def fn(conn):
            for name in affected:
                coopdb.register_agent(conn, name)
                event_id = coopdb.append_event(
                    conn,
                    item_id=None,
                    event_type="batch_row",
                    actor_agent_id=None,
                    actor_session_id=None,
                    payload={"agent": name},
                )
                coopdb.deliver(
                    conn,
                    recipient=name,
                    source_event_id=event_id,
                    item_id=None,
                    category="batch",
                    payload={"agent": name},
                )

        coopdb.mutate(self.conn, fn)
        self.assertEqual(self.counts()["events"], len(affected))
        self.assertEqual(self.counts()["inbox_entries"], len(affected))
        pairs = self.conn.execute(
            "SELECT e.payload_json, i.recipient_agent_id FROM events e "
            "JOIN inbox_entries i ON i.source_event_id = e.event_id "
            "ORDER BY e.event_id"
        ).fetchall()
        self.assertEqual(
            [json.loads(row[0])["agent"] for row in pairs], list(affected)
        )
        self.assertEqual([row[1] for row in pairs], list(affected))

    def test_no_op_appends_zero_events(self):
        def fn(conn):
            if conn.execute(
                "SELECT 1 FROM items WHERE status='no-such-status'"
            ).fetchone():
                coopdb.append_event(
                    conn,
                    item_id=None,
                    event_type="never",
                    actor_agent_id=None,
                    actor_session_id=None,
                    payload={},
                )
            return "no-op"

        self.assertEqual(coopdb.mutate(self.conn, fn), "no-op")
        self.assertEqual(self.counts()["events"], 0)
        self.assertEqual(self.counts()["inbox_entries"], 0)


class TestInboxCursor(SpineBase):
    def _seed(self, recipient, n, category="probe"):
        def fn(conn):
            for index in range(n):
                event_id = coopdb.append_event(
                    conn,
                    item_id=None,
                    event_type=f"{category}_{index}",
                    actor_agent_id=None,
                    actor_session_id=None,
                    payload={"i": index},
                )
                coopdb.deliver(
                    conn,
                    recipient=recipient,
                    source_event_id=event_id,
                    item_id=None,
                    category=category,
                    payload={"i": index},
                )

        coopdb.mutate(self.conn, fn)

    def test_consume_returns_global_id_order_and_advances_exactly(self):
        # Interleave two recipients so codex's ids are non-contiguous.
        self._seed("codex", 1, "first")
        self._seed("claude", 1, "noise")
        self._seed("codex", 1, "second")

        rows = coopdb.read_inbox(self.conn, "codex")
        ids = [row["inbox_entry_id"] for row in rows]
        self.assertEqual(len(rows), 2)
        self.assertEqual(ids, sorted(ids), "entries must arrive in id order")
        offset = self.conn.execute(
            "SELECT last_consumed_entry_id FROM inbox_offsets "
            "WHERE agent_id='codex'"
        ).fetchone()[0]
        self.assertEqual(
            offset, ids[-1],
            "consume must advance exactly through the returned ids",
        )
        self.assertEqual(coopdb.read_inbox(self.conn, "codex"), [])
        # New entries after the consume are picked up next time.
        self._seed("codex", 1, "third")
        again = coopdb.read_inbox(self.conn, "codex")
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["category"], "third")

    def test_peek_never_writes(self):
        self._seed("codex", 2)
        first = coopdb.read_inbox(self.conn, "codex", peek=True)
        second = coopdb.read_inbox(self.conn, "codex", peek=True)
        self.assertEqual(len(first), 2)
        self.assertEqual(
            [row["inbox_entry_id"] for row in first],
            [row["inbox_entry_id"] for row in second],
            "peek must be repeatable",
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM inbox_offsets WHERE agent_id='codex'"
            ).fetchone(),
            "peek must never create or advance an offset",
        )

    def test_consume_inside_mutate_rolls_back_with_the_transaction(self):
        self._seed("codex", 2)

        def fn(conn):
            rows = coopdb.read_inbox(conn, "codex")
            self.assertEqual(len(rows), 2)
            raise coop_errors.InvalidTransition("checkpoint failed late")

        with self.assertRaisesRegex(coopdb.CoopError, "failed late"):
            coopdb.mutate(self.conn, fn)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM inbox_offsets WHERE agent_id='codex'"
            ).fetchone(),
            "a rolled-back consume must not advance the cursor",
        )
        self.assertEqual(
            len(coopdb.read_inbox(self.conn, "codex", peek=True)), 2
        )


class TestClockAndRouting(SpineBase):
    def test_now_reads_the_injectable_module_clock(self):
        frozen = datetime.datetime(
            2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc
        )
        with mock.patch.object(coopdb, "_clock", lambda: frozen):
            self.assertEqual(coopdb.now(), "2026-01-02T03:04:05+00:00")

    def test_register_agent_and_post_message_route_through_mutate(self):
        with mock.patch.object(
            coopdb, "mutate", wraps=coopdb.mutate
        ) as spy:
            coopdb.register_agent(self.conn, "spy-agent")
            self.assertGreaterEqual(spy.call_count, 1)
            spy.reset_mock()
            coopdb.post_message(self.conn, "claude", "hello", to_agent="codex")
            self.assertGreaterEqual(spy.call_count, 1)

    def test_register_agent_composes_inside_an_open_mutate(self):
        def fn(conn):
            coopdb.register_agent(conn, "nested-agent")
            raise coop_errors.InvalidTransition("outer failure wins")

        with self.assertRaisesRegex(coopdb.CoopError, "outer failure"):
            coopdb.mutate(self.conn, fn)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM agents WHERE name='nested-agent'"
            ).fetchone(),
            "a nested register must roll back with the outer transaction",
        )


class TestTypedErrors(SpineBase):
    def test_coopdb_raises_specific_taxonomy_classes(self):
        # The pre-release raise-sites are retired; the surviving coopdb
        # surface carries these typed classes (later lanes add the rest).
        with self.assertRaises(coop_errors.InvalidAgentName):
            coopdb.register_agent(self.conn, "../evil")
        with self.assertRaises(coop_errors.MigrationFailed):
            coopdb.migrate_db(self.db)

    def test_schema_guard_raises_schema_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            empty = pathlib.Path(d) / "empty.db"
            with contextlib.closing(coopdb.connect(empty)) as conn:
                with self.assertRaises(coop_errors.SchemaMismatch):
                    coopdb.require_current_schema(conn)

    def test_cli_renders_type_message_and_json_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            empty = pathlib.Path(d) / "empty.db"
            text = run_coop("--db", empty, "status")
            self.assertEqual(text.returncode, 1)
            self.assertTrue(
                text.stderr.startswith("error: schema_mismatch: "),
                text.stderr,
            )
            self.assertTrue(
                text.stderr.splitlines()[0].startswith(
                    "error: schema_mismatch: "
                )
            )
            self.assertIn("reason: schema_mismatch", text.stderr)
            self.assertIn("evidence: {}", text.stderr)
            self.assertIn("legal-next-actions: []", text.stderr)
            as_json = run_coop("--db", empty, "--json", "status")
            self.assertEqual(as_json.returncode, 1)
            envelope = json.loads(as_json.stderr)
            self.assertEqual(envelope["error"]["type"], "schema_mismatch")
            self.assertIn("coop", envelope["error"]["message"])
            self.assertEqual(
                set(envelope["error"]),
                {
                    "type",
                    "message",
                    "reason_code",
                    "evidence",
                    "legal_next_actions",
                },
            )


if __name__ == "__main__":
    unittest.main()
