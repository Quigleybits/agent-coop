"""The atomic, non-consuming inbox projection.

Markdown is a disposable view: rendered from peek reads only, published by
temp-file + atomic replace, refused for hostile agent names at the sink,
and failing as a returned warning — never an exception into the
supervisor's maintenance loop.
"""

import os
import pathlib
import unittest
import unittest.mock

from agent_coop import coopdb
from agent_coop import projection
from tests.test_claims import ClaimBoard, LEASE


class ProjectionBoard(ClaimBoard):
    def seed_full_inbox(self):
        """One delivery of every category for bob, plus live work state."""
        self.make_session("alice", sid="s-alice")
        self.make_session("bob", sid="s-bob")
        self.make_session("carol", sid="s-carol")
        # assignment: seeding with an explicit owner delivers to bob.
        self.make_item(title="assigned-to-bob", owner="bob")
        # ownership_transfer: alice claims an item seeded as bob's.
        transfer_item = self.make_item(title="taken-from-bob", owner="bob")
        self.claim(transfer_item, "alice", "s-alice")
        # stale_warning: bob's claim lapses and the sweep flips it.
        stale_item = self.make_item(title="bob-went-quiet")
        self.claim(stale_item, "bob", "s-bob")
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        # question: alice needs input from bob.
        q_item = self.make_item(title="alice-asks-bob")
        q_claim = self.claim(q_item, "alice", "s-alice")
        coopdb.needs_input(
            self.conn, claim_id=q_claim["claim_id"], session_id="s-alice",
            to_agent="bob", question="which schema version?")
        # answer: bob owns work, asks carol, carol answers -> bob notified.
        a_item = self.make_item(title="bob-asks-carol")
        a_claim = self.claim(a_item, "bob", "s-bob")
        qid = coopdb.needs_input(
            self.conn, claim_id=a_claim["claim_id"], session_id="s-bob",
            to_agent="carol", question="which region?")
        response = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-carol", intent="easy")
        coopdb.answer_question(
            self.conn, claim_id=response["claim_id"], session_id="s-carol",
            answer="eu-west-2")
        # message: a plain addressed say.
        coopdb.say(self.conn, session_id="s-alice", body="fyi bob",
                   to_agent="bob")


class RenderSemantics(ProjectionBoard):
    def test_render_is_pure_and_cursor_neutral(self):
        self.seed_full_inbox()
        offsets_before = self.conn.execute(
            "SELECT * FROM inbox_offsets").fetchall()
        first = projection.render_inbox(self.conn, "bob")
        second = projection.render_inbox(self.conn, "bob")
        self.assertEqual(first, second)
        offsets_after = self.conn.execute(
            "SELECT * FROM inbox_offsets").fetchall()
        self.assertEqual(
            [tuple(r) for r in offsets_before],
            [tuple(r) for r in offsets_after])
        # The agent's own consume still sees everything afterwards.
        consumed = coopdb.read_inbox(self.conn, "bob")
        self.assertGreater(len(consumed), 0)
        emptied = projection.render_inbox(self.conn, "bob")
        self.assertIn("## Unread (0)", emptied)

    def test_render_covers_every_delivery_category(self):
        self.seed_full_inbox()
        md = projection.render_inbox(self.conn, "bob")
        for category in ("assignment", "ownership_transfer", "stale_warning",
                         "question", "answer", "message"):
            self.assertIn(f"[{category}]", md)

    def test_render_shows_work_questions_and_stale_sections(self):
        self.seed_full_inbox()
        md = projection.render_inbox(self.conn, "bob")
        self.assertIn("## Work", md)
        self.assertIn("assigned-to-bob", md)
        self.assertIn("## Questions for you", md)
        self.assertIn("which schema version?", md)
        self.assertIn("## Stale claims", md)

    def test_reviews_render_only_when_migrated_rows_exist(self):
        self.seed_full_inbox()
        md = projection.render_inbox(self.conn, "bob")
        self.assertNotIn("Review requests", md)  # never invented
        self.conn.execute(
            "INSERT INTO reviews(item_id,requested_by,reviewer_agent_id,"
            "status,legacy,created_at) VALUES (1,'human','bob','requested',"
            "1,?)", (coopdb.now(),))
        self.conn.commit()
        md = projection.render_inbox(self.conn, "bob")
        self.assertIn("## Review requests (1)", md)

    def test_packets_render_for_active_work_and_stay_tokenless(self):
        item = self.make_item(title="active-work")
        sid = self.make_session("bob")
        self.claim(item, "bob", sid)
        md = projection.render_inbox(self.conn, "bob")
        self.assertIn(f"## Packet — item {item}", md)
        self.assertIn("active-work", md)
        self.assertNotIn("fencing_token", md)


class AtomicPublication(ProjectionBoard):
    def _inbox_dir(self):
        return pathlib.Path(self.tmp.name) / "inbox"

    def test_write_inbox_replaces_atomically_and_cleans_temps(self):
        target = self._inbox_dir() / "bob.md"
        projection.write_inbox(target, "first\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "first\n")
        projection.write_inbox(target, "second\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "second\n")
        leftovers = [p for p in self._inbox_dir().iterdir()
                     if p.name != "bob.md"]
        self.assertEqual(leftovers, [])

    def test_failed_write_leaves_previous_file_and_no_temp(self):
        target = self._inbox_dir() / "bob.md"
        projection.write_inbox(target, "intact\n")
        with unittest.mock.patch.object(
                projection.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                projection.write_inbox(target, "doomed\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "intact\n")
        leftovers = [p for p in self._inbox_dir().iterdir()
                     if p.name != "bob.md"]
        self.assertEqual(leftovers, [])

    def test_refresh_writes_the_rendered_file(self):
        self.seed_full_inbox()
        warning = projection.refresh_inbox(
            self.conn, "bob", out_dir=str(self._inbox_dir()))
        self.assertIsNone(warning)
        content = (self._inbox_dir() / "bob.md").read_text(encoding="utf-8")
        self.assertEqual(content, projection.render_inbox(self.conn, "bob"))

    def test_refresh_refuses_hostile_agent_names_at_the_sink(self):
        for hostile in ("../evil", "/abs/path", "a/b", "a\\b", "..", ""):
            with self.subTest(agent=hostile):
                warning = projection.refresh_inbox(
                    self.conn, hostile, out_dir=str(self._inbox_dir()))
                self.assertIsNotNone(warning)
                self.assertIn("invalid agent name", warning)
        self.assertFalse(self._inbox_dir().exists())  # nothing was written

    def test_refresh_returns_a_warning_never_raises(self):
        with unittest.mock.patch.object(
                projection, "render_inbox",
                side_effect=RuntimeError("render exploded")):
            warning = projection.refresh_inbox(
                self.conn, "bob", out_dir=str(self._inbox_dir()))
        self.assertIn("projection failed", warning)
        self.assertIn("render exploded", warning)


if __name__ == "__main__":
    unittest.main()
