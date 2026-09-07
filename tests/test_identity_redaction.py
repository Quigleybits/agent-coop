"""Session ids are display prefixes everywhere a peer can read them.

Identity is cooperative: `COOP_SESSION_ID` is self-asserted, so a full peer
session id in the packet, the history dump or the sessions strip would let a
misbehaving process act as that peer. Every serialized read shortens session
references to the eight-character prefix the dashboard renders. The board
rows themselves keep the full id — the protocol still joins on it.
"""
import json
import unittest

from agent_coop import coopdb
from tests.test_claims import ClaimBoard
from tests.test_reviews import ReviewBoard

FULL = "0123456789abcdef0123456789abcdef"
SHORT = FULL[:coopdb.SESSION_DISPLAY_CHARS]


class ShortSessionId(unittest.TestCase):
    def test_prefix_and_none(self):
        self.assertEqual(coopdb.short_session_id(FULL), SHORT)
        self.assertEqual(len(SHORT), 8)
        self.assertIsNone(coopdb.short_session_id(None))
        self.assertEqual(coopdb.short_session_id("s-a"), "s-a")


class PacketRedaction(ClaimBoard):
    def test_packet_carries_no_full_session_id_argv_or_cwd(self):
        item = self.make_item()
        sid = self.make_session("alice", sid=FULL)
        coopdb.insert_session(
            self.conn, session_id="peer-full-id-000000", agent_id="bob",
            provider="codex", command=["codex", "--token", "SECRET-ARGV"],
            cwd="C:/private/repo", max_runtime_s=28800, grace_s=10)
        self.claim(item, "alice", sid)

        packet = coopdb.item_show(self.conn, item, packet=True)

        self.assertEqual(packet["claim"]["session_id"], SHORT)
        self.assertEqual(packet["session"],
                         {"provider": "claude", "status": "running"})
        flat = json.dumps(packet)
        self.assertNotIn(FULL, flat)
        self.assertNotIn("SECRET-ARGV", flat)
        self.assertNotIn("C:/private/repo", flat)
        self.assertNotIn("command", packet["session"])
        self.assertNotIn("working_directory", packet["session"])



class ReviewClaimRedaction(ReviewBoard):
    def test_review_claim_session_is_a_prefix_too(self):
        self.make_session("bob")
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            reviewer="bob")
        bob_sid = self.conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id='bob'"
        ).fetchone()["session_id"]
        self.assertGreater(len(bob_sid), coopdb.SESSION_DISPLAY_CHARS)

        _claim, packet = self.claim_rev(review_id, bob_sid)

        self.assertEqual(packet["review"]["claim"]["session_id"],
                         bob_sid[:coopdb.SESSION_DISPLAY_CHARS])
        flat = json.dumps(packet)
        self.assertNotIn(bob_sid, flat)
        self.assertNotIn(sid, flat)


class HistoryRedaction(ClaimBoard):
    def test_history_dump_shortens_every_session_reference(self):
        item = self.make_item()
        sid = self.make_session("alice", sid=FULL)
        self.claim(item, "alice", sid)

        history = coopdb.item_show(self.conn, item, history=True)

        flat = json.dumps(history)
        self.assertNotIn(FULL, flat)
        self.assertIn(SHORT, flat)
        claim = history["claims"][0]
        self.assertEqual(claim["owner_session_id"], SHORT)
        with_session = [e["actor_session_id"] for e in history["events"]
                        if e["actor_session_id"] is not None]
        self.assertTrue(with_session)
        self.assertEqual(set(with_session), {SHORT})
        # Human actions keep a null session reference.
        created = [e for e in history["events"]
                   if e["event_type"] == "item_created"]
        self.assertIsNone(created[0]["actor_session_id"])

    def test_the_board_row_keeps_the_full_id_for_the_protocol(self):
        item = self.make_item()
        sid = self.make_session("alice", sid=FULL)
        self.claim(item, "alice", sid)
        row = self.conn.execute(
            "SELECT owner_session_id FROM claims WHERE item_id=?",
            (item,)).fetchone()
        self.assertEqual(row["owner_session_id"], FULL)


class SessionsStrip(ClaimBoard):
    def test_session_rows_are_display_prefixes(self):
        self.make_session("alice", sid=FULL)
        rows = coopdb.session_rows(self.conn)
        self.assertEqual([r["session_id"] for r in rows], [SHORT])
        self.assertEqual(rows[0]["agent_id"], "alice")

    def test_task_rows_still_detect_liveness_from_the_full_id(self):
        item = self.make_item()
        sid = self.make_session("alice", sid=FULL)
        self.claim(item, "alice", sid)
        task = next(t for t in coopdb.task_rows(self.conn)
                    if t["item_id"] == item)
        self.assertTrue(task["claim"]["session_live"])


if __name__ == "__main__":
    unittest.main()
