"""Operator read-only views: `coop tasks`/`task`, `coop board`, monitor frame.

Presentation only — no protocol authority. Pins the row shapes, the
contract_incomplete label derivation (a narrow SELECT must not starve
contract_incomplete of its contract fields and crash on the first row),
token absence, and the JSON surfaces.
"""
import contextlib
import io
import json
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop import coop_ui
from tests.test_claims import ClaimBoard


class TaskRows(ClaimBoard):
    def test_full_contract_item_row_shape(self):
        item = self.make_item(title="op-view item")
        rows = coopcli._task_rows(self.conn)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            set(row), {"item_id", "status", "title", "owner", "next_actor",
                       "labels", "claim"})
        self.assertEqual(row["item_id"], item)
        self.assertEqual(row["status"], "todo")
        self.assertEqual(row["labels"], [])
        self.assertIsNone(row["claim"])

    def test_incomplete_legacy_item_gets_label_not_crash(self):
        # A narrow 7-column SELECT would starve contract_incomplete of the
        # contract fields — this row would raise IndexError there.
        coopdb.register_agent(self.conn, "claude")
        def _seed(c):
            return c.execute(
                "INSERT INTO items(title,status,created_by,created_at,"
                "updated_at) VALUES ('legacy','todo','claude',?,?)",
                (coopdb.now(), coopdb.now())).lastrowid
        coopdb.mutate(self.conn, _seed)
        rows = coopcli._task_rows(self.conn)
        self.assertEqual(rows[0]["labels"], ["contract_incomplete"])

    def test_live_claim_subdict_carries_no_token(self):
        item = self.make_item(title="claimed")
        sid = self.make_session("codex", provider="codex")
        self.claim(item, "codex", sid, intent="take it")
        row = coopcli._task_rows(self.conn)[0]
        claim = row["claim"]
        self.assertEqual(
            set(claim),
            {"claim_id", "agent", "status", "intent", "session_live"})
        self.assertTrue(claim["session_live"])  # owning session is running
        self.assertEqual(claim["agent"], "codex")
        self.assertEqual(claim["status"], "active")
        self.assertEqual(claim["intent"], "take it")
        self.assertEqual(row["owner"], "codex")
        self.assertNotIn("fencing", json.dumps(coopcli._task_rows(self.conn)))

    def test_zombie_claim_reports_session_not_live(self):
        # A claim stays active after its owning session exits (the item-21
        # wedge). session_live must flip False so the dashboard stops
        # animating it as active work.
        item = self.make_item(title="claimed")
        sid = self.make_session("codex", provider="codex")
        self.claim(item, "codex", sid, intent="take it")
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit", exit_code=0)
        claim = coopcli._task_rows(self.conn)[0]["claim"]
        self.assertIsNotNone(claim)            # still an active claim on board
        self.assertEqual(claim["status"], "active")
        self.assertFalse(claim["session_live"])


class CommandSurfaces(ClaimBoard):
    def _run(self, argv):
        stdout = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(stdout):
            try:
                coopcli.main(["--db", self.db] + argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        self.assertEqual(code, 0)
        return stdout.getvalue()

    def test_tasks_json_and_status_filter(self):
        self.make_item(title="first")
        item = self.make_item(title="second")
        sid = self.make_session("codex", provider="codex")
        self.claim(item, "codex", sid, intent="work")
        rows = json.loads(self._run(["tasks", "--json"]))
        self.assertEqual([r["title"] for r in rows], ["first", "second"])
        working = json.loads(
            self._run(["tasks", "--json", "--status", "working"]))
        self.assertEqual([r["title"] for r in working], ["second"])
        self.assertNotIn("fencing", self._run(["tasks", "--json"]))
        alias = json.loads(self._run(["task", "--json"]))
        self.assertEqual(len(alias), 2)

    def test_board_json_oldest_first_with_limit(self):
        for n in range(3):
            coopdb.post_message(self.conn, "codex", f"m{n}")
        rows = json.loads(self._run(["board", "--json", "--limit", "2"]))
        self.assertEqual([r["body"] for r in rows], ["m1", "m2"])
        self.assertEqual(
            set(rows[0]), {"id", "from_agent", "to_agent", "body",
                           "created_at", "item_id"})


class Renderers(ClaimBoard):
    def test_plain_render_has_sections_and_no_ansi(self):
        self.make_item(title="rendered")
        board = coopcli.render_status(self.conn, color=False)
        for section in ("== tasks ==", "== board ==", "== sessions =="):
            self.assertIn(section, board)
        self.assertIn("rendered", board)
        self.assertNotIn("\033[", board)

    def test_monitor_frame_empty_states(self):
        frame = coop_ui.render_monitor_frame(
            tasks=[], messages=[], sessions=[], color=False,
            width=80, interval=2, stamp="T", include_logo=False)
        self.assertIn("(no tasks)", frame)
        self.assertIn("(no messages)", frame)
        self.assertIn("(no live sessions)", frame)
        self.assertIn("refresh 2s", frame)

    def test_running_session_appears_in_strip(self):
        self.make_session("claude", sid="s-live")
        board = coopcli.render_status(self.conn, color=False)
        self.assertIn("provider=claude", board)
        self.assertIn("session=s-live"[:16], board)


if __name__ == "__main__":
    unittest.main()
