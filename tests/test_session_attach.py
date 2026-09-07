"""Foreign-harness session attach: `coop session start/end/renew`.

The bug this closes: an agent in its own harness ran `coop` with no session
and was attributed to the trusted-local `human`, so it could not claim /
receipt / review. `session start` mints a session for the current process so
its actions are attributed to the provider instead.
"""
import contextlib
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb


class Attach(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "board.db")
        conn = coopdb.connect(self.db)
        coopdb.init_db(conn)
        conn.close()

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        base = {"COOP_DB": self.db}
        base.update(env or {})
        with unittest.mock.patch.dict(os.environ, base, clear=False):
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                try:
                    coopcli.main(argv)
                except SystemExit as exc:
                    if isinstance(exc.code, int):
                        code = exc.code
                    else:
                        code = 1
                        if exc.code:
                            err.write(str(exc.code))
        return code, out.getvalue(), err.getvalue()

    def _ro(self):
        c = sqlite3.connect("file:" + self.db + "?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    def _start(self, provider="grok"):
        code, out, err = self._run(
            ["--db", self.db, "session", "start", "--as", provider, "--json"])
        self.assertEqual(code, 0, err)
        return json.loads(out)["session_id"]

    def _seed_claimable(self):
        conn = coopdb.connect(self.db, require_current=True)
        item = coopdb.create_item(
            conn, actor="human", session_id=None, title="do a thing",
            objective="o", scope="s", done_when="d", output_contract="x.md",
            context="c", allowed_actions=["edit"], stop_conditions=["stop"])
        conn.close()
        return item

    def test_start_mints_a_provider_session_not_human(self):
        code, out, err = self._run(
            ["--db", self.db, "session", "start", "--as", "grok", "--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["agent"], "grok")
        self.assertEqual(payload["provider"], "grok")
        self.assertTrue(payload["session_id"].startswith("coopstart-grok-"))
        self.assertEqual(payload["env"]["COOP_SESSION_ID"],
                         payload["session_id"])
        c = self._ro()
        row = c.execute(
            "SELECT agent_id, provider, status FROM sessions WHERE session_id=?",
            (payload["session_id"],)).fetchone()
        self.assertEqual((row["agent_id"], row["provider"], row["status"]),
                         ("grok", "grok", "running"))
        ev = c.execute(
            "SELECT payload_json FROM events WHERE event_type='session_started' "
            "ORDER BY event_id DESC LIMIT 1").fetchone()
        self.assertIn("stdin_isatty", json.loads(ev["payload_json"]))
        c.close()

    def test_export_mode_prints_only_export_lines(self):
        code, out, err = self._run(
            ["--db", self.db, "session", "start", "--as", "codex", "--export"])
        self.assertEqual(code, 0, err)
        lines = [ln for ln in out.strip().splitlines() if ln]
        self.assertTrue(all(ln.startswith("export ") for ln in lines), out)
        self.assertTrue(any("COOP_SESSION_ID=" in ln for ln in lines))
        self.assertTrue(any('COOP_AGENT="codex"' in ln for ln in lines))

    def test_second_start_for_same_agent_conflicts(self):
        self._start("grok")
        code, out, err = self._run(
            ["--db", self.db, "session", "start", "--as", "grok", "--json"])
        self.assertEqual(code, 1)
        self.assertIn("active_session_conflict", (out + err).lower())

    def test_claim_under_started_session_is_attributed_to_provider(self):
        item = self._seed_claimable()
        sid = self._start("grok")
        code, cout, cerr = self._run(
            ["--db", self.db, "item", "claim", str(item), "--intent", "mine"],
            env={"COOP_SESSION_ID": sid, "COOP_AGENT": "grok"})
        self.assertEqual(code, 0, cerr)
        c = self._ro()
        who = c.execute(
            "SELECT claimed_by_agent FROM claims WHERE item_id=?",
            (item,)).fetchone()["claimed_by_agent"]
        c.close()
        self.assertEqual(who, "grok")  # NOT "human"

    def test_end_finishes_the_session(self):
        sid = self._start("grok")
        code, _, err = self._run(
            ["--db", self.db, "session", "end", "--session", sid])
        self.assertEqual(code, 0, err)
        c = self._ro()
        st = c.execute("SELECT status FROM sessions WHERE session_id=?",
                       (sid,)).fetchone()["status"]
        c.close()
        self.assertNotEqual(st, "running")

    def test_renew_extends_an_active_claim(self):
        item = self._seed_claimable()
        sid = self._start("grok")
        self._run(
            ["--db", self.db, "item", "claim", str(item), "--intent", "m"],
            env={"COOP_SESSION_ID": sid, "COOP_AGENT": "grok"})
        c = self._ro()
        before = c.execute(
            "SELECT lease_expires_at FROM claims WHERE item_id=?",
            (item,)).fetchone()["lease_expires_at"]
        c.close()
        code, rout, rerr = self._run(
            ["--db", self.db, "session", "renew", "--session", sid])
        self.assertEqual(code, 0, rerr)
        c = self._ro()
        after = c.execute(
            "SELECT lease_expires_at FROM claims WHERE item_id=?",
            (item,)).fetchone()["lease_expires_at"]
        c.close()
        self.assertGreaterEqual(after, before)

    def test_end_without_a_session_errors(self):
        code, _, _ = self._run(["--db", self.db, "session", "end"])
        self.assertEqual(code, 1)


class SkillDiscoverability(unittest.TestCase):
    def skill_texts(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        for relative in (
            ".claude/skills/coop/SKILL.md",
            ".agents/skills/coop/SKILL.md",
        ):
            skill = root / relative
            self.assertTrue(skill.exists(), f"missing {skill}")
            yield skill.read_text(encoding="utf-8")

    def test_harness_skill_files_are_valid(self):
        for text in self.skill_texts():
            self.assertTrue(text.startswith("---"))
            self.assertIn("name:", text)
            self.assertIn("description:", text)

    def test_harness_skills_do_not_auto_launch(self):
        for text in self.skill_texts():
            self.assertIn(
                "Loading this skill does not start provider processes",
                text,
            )
            self.assertIn("Never infer launch authorization", text)
            self.assertIn("coop start --item <id>", text)
            self.assertIn("coop start --all", text)
            self.assertIn(
                'coop item create --title "<goal>" --objective "<goal>"',
                text,
            )
            self.assertIn("COOP_SESSION_ID", text)
            self.assertIn("runner prompt is self-contained", text)


if __name__ == "__main__":
    unittest.main()
