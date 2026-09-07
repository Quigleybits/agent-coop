import argparse, io, unittest, tempfile, os, subprocess, sys
from unittest import mock
from agent_coop import coopdb

def seed_item(conn, title, created_by="claude", owner=None, status=None):
    """Direct-INSERT test fixture for item rows. The pre-release create/claim
    surface is retired and `item create` demands a full contract, so
    read-behavior tests seed rows here instead."""
    coopdb.register_agent(conn, created_by)
    if owner:
        coopdb.register_agent(conn, owner)
    def _seed(c):
        cur = c.execute(
            "INSERT INTO items(title,status,created_by,created_at,updated_at)"
            " VALUES (?,?,?,?,?)",
            (title, status or ("working" if owner else "todo"), created_by,
             coopdb.now(), coopdb.now()))
        iid = cur.lastrowid
        if owner:
            c.execute(
                "INSERT INTO assignments(item_id,agent,role,state,claimed_at)"
                " VALUES (?,?,'owner','active',?)", (iid, owner, coopdb.now()))
        return iid
    return coopdb.mutate(conn, _seed)

class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "board.db")
        self.conn = coopdb.connect(self.db)
        coopdb.init_db(self.conn)
    def tearDown(self):
        self.conn.close(); self.dir.cleanup()

class TestFoundation(Base):
    def test_schema_and_pragmas(self):
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"agents","rooms","messages","items","assignments",
                         "reviews","debates","debate_posts","decisions","agent_offsets"} <= names)
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

class TestMessages(Base):
    def test_post_is_monotonic_and_autocreates_room(self):
        a = coopdb.post_message(self.conn, "codex", "hello")
        b = coopdb.post_message(self.conn, "codex", "again")
        self.assertEqual(b, a + 1)
        rooms = {r["name"] for r in self.conn.execute("SELECT name FROM rooms")}
        self.assertIn("#general", rooms)
        agents = {r["name"] for r in self.conn.execute("SELECT name FROM agents")}
        self.assertIn("codex", agents)

# TestInbox and TestProjection live elsewhere: the legacy message-offset
# cursor (get_inbox/peek_inbox) and the watch loop are deleted — the spine's
# inbox_entries cursor is canonical (tests/test_inbox.py) and the atomic
# non-consuming projection has its own suite (tests/test_projection.py).

class TestCLI(Base):
    def cli(self, *a):
        env = dict(os.environ, COOP_AGENT="codex")
        return subprocess.run([sys.executable, "coop.py", "--db", self.db, *a],
                              capture_output=True, text=True, env=env)
    def test_init_say_status_survive(self):
        self.assertEqual(self.cli("init").returncode, 0)
        say = self.cli("say", "hello board"); self.assertEqual(say.returncode, 0, say.stderr)
        status = self.cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        # The boot panel replaced the legacy "== items ==" render.
        self.assertIn("next:", status.stdout)

class TestAgentNameValidation(Base):
    def test_traversal_and_absolute_names_rejected(self):
        for bad in ("../../etc/passwd", "/abs/path", "..", "a/b", "a\\b", ".hidden", ""):
            with self.assertRaises(coopdb.CoopError):
                coopdb.register_agent(self.conn, bad)
    def test_post_message_rejects_bad_from_agent(self):
        with self.assertRaises(coopdb.CoopError):
            coopdb.post_message(self.conn, "../evil", "hi")
    # test_watch_rejects_traversal_before_writing was retargeted:
    # the sink moved to projection.refresh_inbox — see
    # tests/test_projection.py::test_refresh_refuses_hostile_agent_names_at_the_sink.
    def test_plain_names_accepted(self):
        coopdb.register_agent(self.conn, "codex")
        coopdb.register_agent(self.conn, "grok-2.5_beta")

class TestSplashAndStatusRender(Base):
    def test_parser_does_not_offer_legacy_splash(self):
        from agent_coop import cli as coopcli
        parser = coopcli.build_parser()
        commands = next(
            action.choices
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertNotIn("splash", commands)
        self.assertFalse(hasattr(coopcli, "splash_text"))

    def test_monitor_frame_has_board_without_legacy_splash(self):
        from agent_coop import cli as coopcli
        out = io.StringIO()
        with (
            mock.patch.object(
                coopcli.time, "sleep", side_effect=KeyboardInterrupt
            ),
            mock.patch.object(coopcli.sys, "stdout", out),
        ):
            coopcli.monitor(self.db, interval=2)
        frame = out.getvalue()
        self.assertIn("== tasks ==", frame)
        self.assertIn("refresh 2s", frame)
        self.assertNotIn("AGENT", frame)
        self.assertNotIn("multi-agent coordination", frame)
        self.assertNotIn("SQLite-backed", frame)

    def test_monitor_guards_every_reopened_connection(self):
        from agent_coop import cli as coopcli
        connections = [mock.Mock(), mock.Mock()]
        out = io.StringIO()
        with (
            mock.patch.object(
                coopcli.coopdb, "connect", side_effect=connections
            ) as guarded_connect,
            mock.patch.object(coopcli, "_monitor_frame", return_value="board"),
            mock.patch.object(
                coopcli.time, "sleep", side_effect=[None, KeyboardInterrupt]
            ),
            mock.patch.object(coopcli.sys, "stdout", out),
        ):
            coopcli.monitor(self.db, interval=0)

        self.assertEqual(
            guarded_connect.call_args_list,
            [
                mock.call(self.db, require_current=True),
                mock.call(self.db, require_current=True),
            ],
        )
        for connection in connections:
            connection.close.assert_called_once_with()

    def test_render_status_lists_item_and_colors_owner(self):
        from agent_coop import cli as coopcli
        seed_item(self.conn, "demo task", created_by="claude", owner="codex")
        board = coopcli.render_status(self.conn, color=True)
        self.assertIn("demo task", board)
        self.assertIn("136;192;208", board)   # codex owner rendered blue
        plain = coopcli.render_status(self.conn, color=False)
        self.assertNotIn("\033[", plain)      # color=False → no ANSI

class TestAgents(Base):
    def test_list_agents(self):
        coopdb.register_agent(self.conn, "codex")
        coopdb.register_agent(self.conn, "claude")
        # Version 4 seeds the reserved trusted-local 'human' actor at init.
        self.assertEqual(
            {r["name"] for r in coopdb.list_agents(self.conn)},
            {"codex", "claude", "human"},
        )

class TestExpandFailsClosed(unittest.TestCase):
    """--expand failure must fail the kickoff, not silently create a bare
    item that restores the contract-fill + huddle turns."""

    def _fields(self):
        return {"title": "goal only"}

    def test_expand_failure_without_fallback_raises(self):
        from agent_coop import cli as coopcli
        from agent_coop.coop_errors import IncompleteContract
        with mock.patch.object(
                subprocess, "run",
                side_effect=OSError("no provider")):
            with self.assertRaises(IncompleteContract) as caught:
                coopcli._expand_goal_contract(self._fields())
        self.assertIn("no item was created", str(caught.exception))
        self.assertIn("--expand-fallback-bare", str(caught.exception))

    def test_expand_failure_with_fallback_returns_bare_fields(self):
        from agent_coop import cli as coopcli
        with mock.patch.object(
                subprocess, "run",
                side_effect=OSError("no provider")):
            fields = coopcli._expand_goal_contract(
                self._fields(), allow_bare_fallback=True)
        self.assertEqual(fields, {"title": "goal only"})


if __name__ == "__main__":
    unittest.main()
