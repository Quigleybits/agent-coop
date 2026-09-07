"""Supervised session identity and lifecycle records.

Provider binds once at launch; one running session per agent; terminal
transitions map exactly and happen exactly once; the CLI env chokepoint
resolves canonical and alias variables identically."""

import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from agent_coop import coop_errors
from agent_coop import coopdb
from tests.test_claims import Clock
from tests.test_contract import contract_kwargs

COOP_DIR = pathlib.Path(__file__).resolve().parents[1]

TERMINAL_MAP = {
    "child_exit": "exited",
    "cancelled": "cancelled",
    "max_runtime": "timed_out",
    "checkpoint_timeout": "timed_out",
    "launch_failed": "exited",
}


def run_coop(*args, env=None):
    import os

    merged = dict(os.environ)
    for key in ("COOP_SESSION_ID", "COOP_AGENT", "COOP_AGENT_ID",
                "COOP_DB", "COOP_DB_PATH"):
        merged.pop(key, None)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "coop.py", *(str(arg) for arg in args)],
        cwd=COOP_DIR,
        capture_output=True,
        text=True,
        env=merged,
    )


class SessionBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = pathlib.Path(self._dir.name) / "board.db"
        self.conn = coopdb.connect(self.db)
        self.addCleanup(self.conn.close)
        coopdb.init_db(self.conn)

    def start(self, session_id, agent, provider=None, command=("codex", "exec")):
        return coopdb.insert_session(
            self.conn,
            session_id=session_id,
            agent_id=agent,
            provider=provider or agent,
            command=list(command),
            cwd="C:/work/repo",
            max_runtime_s=28800,
            grace_s=10,
        )


class TestRegisterOrBind(SessionBase):
    def test_fresh_agent_registers_with_its_provider(self):
        coopdb.register_or_bind_agent(
            self.conn, agent_id="backend-reviewer", provider="codex"
        )
        row = self.conn.execute(
            "SELECT provider FROM agents WHERE name='backend-reviewer'"
        ).fetchone()
        self.assertEqual(row["provider"], "codex")

    def test_null_provider_agent_binds_once_on_first_launch(self):
        # Item seeding auto-registers routed agents with a NULL provider;
        # the first supervised launch binds it atomically (closes that loop).
        coopdb.create_item(self.conn, **contract_kwargs(owner="routed"))
        before = self.conn.execute(
            "SELECT provider FROM agents WHERE name='routed'"
        ).fetchone()
        self.assertIsNone(before["provider"])
        coopdb.register_or_bind_agent(
            self.conn, agent_id="routed", provider="grok"
        )
        after = self.conn.execute(
            "SELECT provider FROM agents WHERE name='routed'"
        ).fetchone()
        self.assertEqual(after["provider"], "grok")

    def test_same_provider_relaunch_is_idempotent(self):
        coopdb.register_or_bind_agent(self.conn, agent_id="codex", provider="codex")
        coopdb.register_or_bind_agent(self.conn, agent_id="codex", provider="codex")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM agents WHERE name='codex'"
            ).fetchone()[0],
            1,
        )

    def test_different_provider_is_a_conflict_with_name_hint(self):
        coopdb.register_or_bind_agent(self.conn, agent_id="codex", provider="codex")
        with self.assertRaises(coop_errors.ProviderConflict) as caught:
            coopdb.register_or_bind_agent(
                self.conn, agent_id="codex", provider="claude"
            )
        self.assertIn("--name", str(caught.exception))

    def test_reserved_human_cannot_be_launched(self):
        with self.assertRaises(coop_errors.HumanLaneViolation):
            coopdb.register_or_bind_agent(
                self.conn, agent_id="human", provider="claude"
            )
        with self.assertRaises(coop_errors.HumanLaneViolation):
            self.start("sess-h", "human", provider="claude")

    def test_invalid_names_rejected(self):
        with self.assertRaises(coop_errors.InvalidAgentName):
            coopdb.register_or_bind_agent(
                self.conn, agent_id="../evil", provider="codex"
            )
        with self.assertRaises(coop_errors.InvalidAgentName):
            coopdb.register_or_bind_agent(
                self.conn, agent_id="fine", provider="/bad/provider"
            )


class TestInsertSession(SessionBase):
    def test_insert_registers_binds_and_records_the_launch(self):
        self.start("sess-1", "fresh-agent", provider="codex")
        agent = self.conn.execute(
            "SELECT provider FROM agents WHERE name='fresh-agent'"
        ).fetchone()
        self.assertEqual(agent["provider"], "codex")
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id='sess-1'"
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["command_json"], '["codex","exec"]')
        self.assertEqual(row["working_directory"], "C:/work/repo")
        self.assertEqual(row["max_runtime_seconds"], 28800)
        self.assertEqual(row["shutdown_grace_seconds"], 10)
        event = self.conn.execute(
            "SELECT event_type, actor_agent_id, actor_session_id, item_id "
            "FROM events WHERE event_type='session_started'"
        ).fetchone()
        self.assertEqual(event["actor_agent_id"], "fresh-agent")
        self.assertEqual(event["actor_session_id"], "sess-1")
        self.assertIsNone(event["item_id"])

    def test_second_running_session_conflicts_with_name_hint(self):
        self.start("sess-1", "codex")
        with self.assertRaises(coop_errors.ActiveSessionConflict) as caught:
            self.start("sess-2", "codex")
        self.assertIn("--name", str(caught.exception))
        self.start("sess-3", "claude")  # other agents unaffected
        coopdb.finish_session(
            self.conn, "sess-1", status="exited", reason="child_exit",
            exit_code=0,
        )
        self.start("sess-4", "codex")  # terminal predecessor frees the lane

    def test_duplicate_session_id_is_not_masked_as_a_conflict(self):
        self.start("sess-1", "codex")
        with self.assertRaises(sqlite3.IntegrityError):
            self.start("sess-1", "claude")

    def test_empty_command_rejected(self):
        with self.assertRaises(coop_errors.InvalidTransition):
            self.start("sess-1", "codex", command=())


class TestActorEventProbe(SessionBase):
    def setUp(self):
        super().setUp()
        self.start("sess-a", "claude", provider="claude")
        self.start("sess-b", "codex", provider="codex")
        self.item = coopdb.create_item(
            self.conn,
            **contract_kwargs(),
        )

    def test_targeted_probe_credits_only_the_authoring_session(self):
        a_before = coopdb.actor_event_probe(
            self.conn,
            session_id="sess-a",
            item_id=self.item,
        )
        b_before = coopdb.actor_event_probe(
            self.conn,
            session_id="sess-b",
            item_id=self.item,
        )

        coopdb.say(
            self.conn,
            session_id="sess-a",
            body="actor-owned progress",
            item_id=self.item,
        )

        a_after = coopdb.actor_event_probe(
            self.conn,
            session_id="sess-a",
            item_id=self.item,
        )
        b_after = coopdb.actor_event_probe(
            self.conn,
            session_id="sess-b",
            item_id=self.item,
        )
        self.assertGreater(a_after[0], a_before[0])
        self.assertEqual(a_after[1] - a_before[1], 1)
        self.assertEqual(b_after, b_before)

    def test_all_item_probe_keeps_session_baseline_actor_scoped(self):
        a_before = coopdb.actor_event_probe(
            self.conn,
            session_id="sess-a",
            item_id=None,
        )
        b_before = coopdb.actor_event_probe(
            self.conn,
            session_id="sess-b",
            item_id=None,
        )
        self.assertGreaterEqual(a_before[1], 1)
        self.assertGreaterEqual(b_before[1], 1)

        coopdb.say(
            self.conn,
            session_id="sess-a",
            body="all-scope actor progress",
            item_id=self.item,
        )

        a_after = coopdb.actor_event_probe(
            self.conn,
            session_id="sess-a",
            item_id=None,
        )
        b_after = coopdb.actor_event_probe(
            self.conn,
            session_id="sess-b",
            item_id=None,
        )
        self.assertEqual(a_after[1] - a_before[1], 1)
        self.assertEqual(b_after, b_before)


class TestFinishSession(SessionBase):
    def test_terminal_mapping_records_exactly_once(self):
        for index, (reason, status) in enumerate(sorted(TERMINAL_MAP.items())):
            with self.subTest(reason=reason):
                session_id = f"sess-{index}"
                agent = f"agent-{index}"
                self.start(session_id, agent)
                exit_code = 0 if status == "exited" else None
                coopdb.finish_session(
                    self.conn, session_id, status=status, reason=reason,
                    exit_code=exit_code,
                )
                row = self.conn.execute(
                    "SELECT status, termination_reason, exit_code, exited_at "
                    "FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                self.assertEqual(row["status"], status)
                self.assertEqual(row["termination_reason"], reason)
                self.assertEqual(row["exit_code"], exit_code)
                self.assertIsNotNone(row["exited_at"])
                with self.assertRaises(coop_errors.InvalidTransition):
                    coopdb.finish_session(
                        self.conn, session_id, status=status, reason=reason,
                        exit_code=exit_code,
                    )

    def test_status_must_match_the_reason(self):
        self.start("sess-1", "codex")
        with self.assertRaises(coop_errors.InvalidTransition):
            coopdb.finish_session(
                self.conn, "sess-1", status="exited", reason="max_runtime",
                exit_code=None,
            )

    def test_unknown_reason_rejected(self):
        self.start("sess-1", "codex")
        with self.assertRaises(coop_errors.InvalidTransition):
            coopdb.finish_session(
                self.conn, "sess-1", status="exited", reason="rapture",
                exit_code=None,
            )

    def test_unknown_session_is_a_mismatch(self):
        with self.assertRaises(coop_errors.SessionMismatch):
            coopdb.finish_session(
                self.conn, "no-such", status="exited", reason="child_exit",
                exit_code=0,
            )

    def test_finish_appends_a_session_finished_event(self):
        self.start("sess-1", "codex")
        coopdb.finish_session(
            self.conn, "sess-1", status="cancelled", reason="cancelled",
            exit_code=None,
        )
        event = self.conn.execute(
            "SELECT actor_session_id, payload_json FROM events "
            "WHERE event_type='session_finished'"
        ).fetchone()
        self.assertEqual(event["actor_session_id"], "sess-1")
        self.assertIn('"reason":"cancelled"', event["payload_json"])


class TestResolveActorLegs(SessionBase):
    def test_all_four_session_mismatch_legs_and_the_happy_path(self):
        self.assertEqual(
            coopdb.resolve_actor(self.conn, None), ("human", None)
        )
        with self.assertRaises(coop_errors.SessionMismatch) as unknown:
            coopdb.resolve_actor(self.conn, "no-such-session")
        self.assertEqual(unknown.exception.reason_code, "session_unavailable")
        self.assertNotIn("item_id", unknown.exception.evidence)
        self.assertNotIn("session_status", unknown.exception.evidence)
        self.start("sess-dead", "codex")
        coopdb.finish_session(
            self.conn, "sess-dead", status="exited", reason="child_exit",
            exit_code=0,
        )
        with self.assertRaises(coop_errors.SessionMismatch) as dead:
            coopdb.resolve_actor(self.conn, "sess-dead")
        self.assertEqual(dead.exception.reason_code, "session_unavailable")
        self.assertEqual(dead.exception.evidence["session_status"], "exited")
        self.start("sess-live", "codex")
        with self.assertRaises(coop_errors.SessionMismatch) as mismatch:
            coopdb.resolve_actor(self.conn, "sess-live", claimed_agent="claude")
        self.assertEqual(mismatch.exception.reason_code, "actor_mismatch")
        self.assertEqual(
            dict(mismatch.exception.evidence),
            {"actor_agent_id": "claude", "required_agent_id": "codex"},
        )
        self.assertEqual(
            coopdb.resolve_actor(self.conn, "sess-live", claimed_agent="codex"),
            ("codex", "sess-live"),
        )


class TestEnvChokepoint(SessionBase):
    def test_alias_envs_resolve_identically_to_canonical(self):
        self.start("sess-live", "codex")
        flags = [
            "item", "create",
            "--title", "T", "--objective", "O", "--scope", "S",
            "--done-when", "D", "--output-contract", "OC", "--context", "C",
            "--allowed-action", "a", "--stop-condition", "s",
        ]
        canonical = run_coop(
            *flags,
            env={
                "COOP_DB": str(self.db),
                "COOP_SESSION_ID": "sess-live",
                "COOP_AGENT": "codex",
            },
        )
        self.assertEqual(canonical.returncode, 0, canonical.stderr)
        alias = run_coop(
            *flags,
            env={
                "COOP_DB_PATH": str(self.db),
                "COOP_SESSION_ID": "sess-live",
                "COOP_AGENT_ID": "codex",
            },
        )
        self.assertEqual(alias.returncode, 0, alias.stderr)
        creators = [
            row[0]
            for row in self.conn.execute(
                "SELECT created_by FROM items ORDER BY id"
            )
        ]
        self.assertEqual(creators, ["codex", "codex"])

    def test_exited_session_env_is_refused(self):
        self.start("sess-dead", "codex")
        coopdb.finish_session(
            self.conn, "sess-dead", status="exited", reason="child_exit",
            exit_code=0,
        )
        result = run_coop(
            "item", "create", "--title", "T",
            env={
                "COOP_DB": str(self.db),
                "COOP_SESSION_ID": "sess-dead",
                "COOP_AGENT": "codex",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: session_mismatch:", result.stderr)


class SessionSweep(unittest.TestCase):
    """The deterministic stale-session sweep: a running session past its
    max_runtime self-heals (mirrors the claim sweep), so an orphaned session
    left by a dead launcher no longer wedges a fresh launch forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(pathlib.Path(self.tmp.name) / "board.db")
        self.clock = Clock()
        self._orig = coopdb._clock
        coopdb._clock = self.clock
        self.addCleanup(lambda: setattr(coopdb, "_clock", self._orig))
        self.conn = coopdb.connect(self.db)
        coopdb.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def _insert(self, agent, runtime, sid=None):
        sid = sid or f"s-{agent}-{runtime}"
        return coopdb.insert_session(
            self.conn, session_id=sid, agent_id=agent, provider=agent,
            command=["x"], cwd=".", max_runtime_s=runtime, grace_s=5)

    def _status(self, sid):
        return self.conn.execute(
            "SELECT status FROM sessions WHERE session_id=?",
            (sid,)).fetchone()["status"]

    def test_expired_session_swept_with_event_and_idempotent(self):
        sid = self._insert("claude", 60)
        self.clock.advance(61)
        self.assertEqual(coopdb.sweep_expired_sessions(self.conn), 1)
        self.assertEqual(self._status(sid), "timed_out")
        ev = self.conn.execute(
            "SELECT payload_json FROM events WHERE "
            "event_type='session_finished' AND actor_session_id=?",
            (sid,)).fetchone()
        self.assertIsNotNone(ev)
        self.assertIn("max_runtime", ev["payload_json"])
        self.assertEqual(coopdb.sweep_expired_sessions(self.conn), 0)

    def test_within_runtime_not_swept_and_still_conflicts(self):
        self._insert("codex", 3600)
        self.clock.advance(60)
        self.assertEqual(coopdb.sweep_expired_sessions(self.conn), 0)
        with self.assertRaises(coop_errors.ActiveSessionConflict):
            self._insert("codex", 3600, sid="s-codex-second")

    def test_insert_auto_heals_over_an_expired_prior_session(self):
        old = self._insert("grok", 60)
        self.clock.advance(120)
        self._insert("grok", 3600, sid="s-grok-new")   # would 've conflicted
        self.assertEqual(self._status(old), "timed_out")
        self.assertEqual(self._status("s-grok-new"), "running")

    def test_agent_id_scopes_the_sweep(self):
        a = self._insert("claude", 60)
        b = self._insert("codex", 60)
        self.clock.advance(120)
        self.assertEqual(
            coopdb.sweep_expired_sessions(self.conn, agent_id="claude"), 1)
        self.assertEqual(self._status(a), "timed_out")
        self.assertEqual(self._status(b), "running")


if __name__ == "__main__":
    unittest.main()
