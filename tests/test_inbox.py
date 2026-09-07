"""Non-binding messages and the addressed inbox cursor.

Messages inform; they never authorize, transfer, approve, or complete.
An agent-authored say requires a live session but no claim; the human says
with a null session. Addressed messages deliver exactly once; broadcasts
deliver to nobody. `coop inbox` is the CLI face of the spine cursor.
"""

import io
import contextlib
import json
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coop_repl
from agent_coop import coopdb
from agent_coop.coop_errors import (
    InvalidTransition,
    NotFound,
    SessionMismatch,
)
from tests.test_claims import ClaimBoard, snapshot


class SayBoard(ClaimBoard):
    def say(self, body, sid=None, **kw):
        return coopdb.say(self.conn, session_id=sid, body=body, **kw)

    def messages(self):
        return self.conn.execute(
            "SELECT * FROM messages ORDER BY id").fetchall()

    def entries_for(self, agent):
        return self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id=? "
            "ORDER BY inbox_entry_id", (agent,)).fetchall()


class SaySemantics(SayBoard):
    def test_agent_say_requires_a_live_session_but_no_claim(self):
        with self.assertRaises(SessionMismatch):
            self.say("hello", sid="no-such-session")
        sid = self.make_session("alice")
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit", exit_code=0)
        with self.assertRaises(SessionMismatch):
            self.say("hello", sid=sid)
        live = self.make_session("bob")
        mid = self.say("claim-free chatter", sid=live)  # bob holds no claim
        self.assertEqual(self.messages()[-1]["from_agent"], "bob")
        self.assertIsNotNone(mid)

    def test_human_say_has_the_human_actor_and_null_session(self):
        self.say("operator note")
        row = self.messages()[-1]
        self.assertEqual(row["from_agent"], "human")
        event = self.events_of("message_posted")[-1]
        self.assertEqual(event["actor_agent_id"], "human")
        self.assertIsNone(event["actor_session_id"])

    def test_addressed_delivers_exactly_once_broadcast_never(self):
        sid = self.make_session("alice")
        coopdb.register_agent(self.conn, "bob")
        self.say("for bob", sid=sid, to_agent="bob")
        self.say("for everyone", sid=sid)
        bob_entries = self.entries_for("bob")
        self.assertEqual(len(bob_entries), 1)
        self.assertEqual(bob_entries[0]["category"], "message")
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM inbox_entries").fetchone()["n"]
        self.assertEqual(total, 1)  # the broadcast delivered to nobody
        self.assertEqual(len(self.events_of("message_posted")), 2)
        self.assertEqual(len(self.messages()), 2)

    def test_say_commits_message_event_and_entry_together(self):
        sid = self.make_session("alice")
        coopdb.register_agent(self.conn, "bob")
        real_deliver = coopdb.deliver

        def sabotage(conn, **kw):
            if kw.get("category") == "message":
                raise RuntimeError("forced")
            return real_deliver(conn, **kw)

        with unittest.mock.patch.object(coopdb, "deliver", sabotage):
            with self.assertRaises(RuntimeError):
                self.say("doomed", sid=sid, to_agent="bob")
        self.assertEqual(len(self.messages()), 0)
        self.assertEqual(len(self.events_of("message_posted")), 0)

    def test_addressed_to_unregistered_is_refused_clean(self):
        sid = self.make_session("alice")
        before = snapshot(self.conn)
        with self.assertRaises(NotFound):
            self.say("typo", sid=sid, to_agent="bobb")
        self.assertEqual(before, snapshot(self.conn))

    def test_item_reference_must_exist(self):
        sid = self.make_session("alice")
        with self.assertRaises(NotFound):
            self.say("about nothing", sid=sid, item_id=999)

    def test_say_never_changes_item_or_claim_state(self):
        item = self.make_item()
        sid = self.make_session("alice")
        self.claim(item, "alice", sid)
        coopdb.register_agent(self.conn, "bob")
        watched = {
            t: [tuple(r) for r in self.conn.execute(
                f"SELECT * FROM {t} ORDER BY rowid")]
            for t in ("items", "claims")}
        self.say("status ping about the item", sid=sid, to_agent="bob",
                 item_id=item)
        after = {
            t: [tuple(r) for r in self.conn.execute(
                f"SELECT * FROM {t} ORDER BY rowid")]
            for t in ("items", "claims")}
        self.assertEqual(watched, after)

    def test_empty_body_is_refused(self):
        with self.assertRaises(InvalidTransition):
            self.say("   ")


class InboxCli(SayBoard):
    def _run(self, argv, env):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"COOP_DB": self.db, **env}
        code = 0
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                try:
                    coopcli.main(argv)
                except SystemExit as exc:
                    if isinstance(exc.code, int):
                        code = exc.code
                    else:
                        code = 1
                        if exc.code:
                            stderr.write(str(exc.code))
        return code, stdout.getvalue(), stderr.getvalue()

    def _seed_bob_inbox(self):
        """Two deliveries for bob: a question and an addressed message."""
        item = self.make_item()
        self.make_session("alice", sid="s-alice")
        self.make_session("bob", sid="s-bob")
        claim = self.claim(item, "alice", "s-alice")
        coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"], session_id="s-alice",
            to_agent="bob", question="which db?")
        coopdb.say(self.conn, session_id="s-alice", body="context for you",
                   to_agent="bob")

    def test_cli_peek_never_writes_consume_advances_exactly_once(self):
        self._seed_bob_inbox()
        env = {"COOP_SESSION_ID": "s-bob", "COOP_AGENT": "bob"}
        code, out1, err = self._run(["--json", "inbox", "--peek"], env)
        self.assertEqual(code, 0, err)
        code, out2, err = self._run(["--json", "inbox", "--peek"], env)
        self.assertEqual(json.loads(out1), json.loads(out2))
        self.assertEqual(len(json.loads(out1)), 2)
        offset = self.conn.execute(
            "SELECT * FROM inbox_offsets WHERE agent_id='bob'").fetchone()
        self.assertIsNone(offset)  # peek wrote nothing
        code, out3, err = self._run(["--json", "inbox"], env)
        self.assertEqual(len(json.loads(out3)), 2)
        code, out4, err = self._run(["--json", "inbox"], env)
        self.assertEqual(json.loads(out4), [])  # consumed exactly once
        categories = [e["category"] for e in json.loads(out3)]
        self.assertEqual(categories, ["question", "message"])

    def test_human_inbox_reads_are_peek_only(self):
        self._seed_bob_inbox()
        env = {"COOP_SESSION_ID": "", "COOP_AGENT": ""}
        code, out, err = self._run(["--as", "bob", "inbox"], env)
        self.assertEqual(code, 1)
        self.assertIn("invalid_transition", err)
        code, out, err = self._run(
            ["--json", "--as", "bob", "inbox", "--peek"], env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)), 2)
        offset = self.conn.execute(
            "SELECT * FROM inbox_offsets WHERE agent_id='bob'").fetchone()
        self.assertIsNone(offset)  # bob's cursor is untouched

    def test_cli_say_resolves_actor_from_the_session(self):
        self.make_session("alice", sid="s-alice")
        coopdb.register_agent(self.conn, "bob")
        env = {"COOP_SESSION_ID": "s-alice", "COOP_AGENT": "alice"}
        code, out, err = self._run(
            ["say", "hi bob", "--to", "bob"], env)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.messages()[-1]["from_agent"], "alice")
        code, out, err = self._run(
            ["say", "operator broadcast"],
            {"COOP_SESSION_ID": "", "COOP_AGENT": ""})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.messages()[-1]["from_agent"], "human")


class ReplHumanLane(SayBoard):
    def test_bare_text_is_a_human_broadcast_and_never_a_task(self):
        items_before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        out, err = io.StringIO(), io.StringIO()
        keep_going = coop_repl.dispatch_line(
            "please remember the staging db is frozen",
            self.db, "human", out, err)
        self.assertTrue(keep_going)
        self.assertIn("posted message #", out.getvalue())
        row = self.messages()[-1]
        self.assertEqual(row["from_agent"], "human")
        self.assertIsNone(row["to_agent"])  # broadcast
        event = self.events_of("message_posted")[-1]
        self.assertEqual(event["actor_agent_id"], "human")
        self.assertIsNone(event["actor_session_id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM inbox_entries").fetchone()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM items").fetchone()["n"], items_before)


if __name__ == "__main__":
    unittest.main()
