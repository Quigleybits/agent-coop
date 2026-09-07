"""Reviews — request, designation, and the claim that observes.

`request_review` is legal from `working` only, binds the current receipt, and
carries an immutable request-time designation (`reviews.reviewer`); the
replacement-during-review transaction ships in the same commit that makes
`review` reachable; `claim_review` runs the review lane through the shared
lane-history guard, enforces designation and reviewer ≠ owner, stamps the
informational `reviewer_agent_id`, and returns the compact packet — decisions
included — without advancing the inbox cursor.
"""

import contextlib
import io
import json
import threading
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop import projection
from agent_coop.coop_errors import (
    AddressedTargetMismatch,
    ClaimCollision,
    DecisionUnobserved,
    HumanLaneViolation,
    IncompleteContract,
    InvalidTransition,
    NotFound,
    ReceiptMissing,
    ReviewStale,
    SelfReview,
    SessionMismatch,
    StaleClaim,
    UnsafeReclaim,
)
from tests.test_claims import LEASE
from tests.test_receipts import ReceiptBoard

SNAP_TABLES = (
    "items", "claims", "receipts", "reviews", "events", "inbox_entries",
    "inbox_offsets",
)

REVIEW_SLOT_KEYS = {
    "review_id", "status", "reviewer", "reviewer_agent_id", "requested_by",
    "receipt_id", "contract_version", "claim",
}


def snap(conn):
    return {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
        for t in SNAP_TABLES
    }


class ReviewBoard(ReceiptBoard):
    def make_working(
            self, agent="alice", ensure_review_provider=True, **item_over):
        """Working item with realistic review capacity by default.

        Review protocol tests normally exercise a claimable review. Tests of
        the fail-closed zero-provider state opt out explicitly.
        """
        item, sid, cid = super().make_working(agent, **item_over)
        if ensure_review_provider:
            capacity = self.conn.execute(
                "SELECT 1 FROM agents WHERE name NOT IN (?, 'human') AND "
                "provider IS NOT NULL AND TRIM(provider)!='' LIMIT 1",
                (agent,),
            ).fetchone()
            if capacity is None:
                coopdb.register_or_bind_agent(
                    self.conn, agent_id="review-peer",
                    provider="review-peer")
        return item, sid, cid

    def make_reviewed(self, agent="alice", reviewer=None, **item_over):
        """working item + current receipt + requested review."""
        item, sid, cid = self.make_working(agent, **item_over)
        receipt_id = self.submit(cid, sid, agent=agent)
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor=agent,
            reviewer=reviewer)
        return item, sid, cid, receipt_id, review_id

    def review_row(self, review_id):
        return self.conn.execute(
            "SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()

    def review_lane(self, review_id):
        return self.conn.execute(
            "SELECT * FROM claims WHERE lane_key=? ORDER BY claim_id",
            (f"review:{review_id}",)).fetchall()

    def claim_rev(self, review_id, sid, intent="review it", reason=None):
        return coopdb.claim_review(
            self.conn, review_id=review_id, session_id=sid, intent=intent,
            reclaim_reason=reason)

    def offset_of(self, agent):
        row = self.conn.execute(
            "SELECT last_consumed_entry_id FROM inbox_offsets WHERE "
            "agent_id=?", (agent,)).fetchone()
        return row["last_consumed_entry_id"] if row else None

    def seed_decision(self, item, text="use sqlite"):
        def _seed(conn):
            conn.execute(
                "INSERT INTO decisions(item_id,text,rationale,decided_by,"
                "decided_by_agent,legacy,created_at) VALUES (?,?,?,?,?,1,?)",
                (item, text, "seeded", "human", "human", coopdb.now()))
        coopdb.mutate(self.conn, _seed)

    def seed_legacy_review(self, item, *, status="requested", reviewer=None,
                           resolved=False):
        """A migration-shaped legacy row: null receipt/version, backfilled
        reviewer_agent_id, legacy=1 — pre-release data describing no claim."""
        def _seed(conn):
            coopdb.register_agent(conn, "alice")
            if reviewer:
                coopdb.register_agent(conn, reviewer)
            cur = conn.execute(
                "INSERT INTO reviews(item_id,receipt_id,contract_version,"
                "requested_by,requested_by_agent,reviewer,reviewer_agent_id,"
                "status,legacy,created_at,resolved_at) "
                "VALUES (?,NULL,NULL,?,?,?,?,?,1,?,?)",
                (item, "alice", "alice", reviewer, reviewer, status,
                 coopdb.now(), coopdb.now() if resolved else None))
            return cur.lastrowid
        return coopdb.mutate(self.conn, _seed)


class RequestValidation(ReviewBoard):
    def test_request_without_current_receipt_refuses_receipt_missing(self):
        item, sid, cid = self.make_working()
        before = snap(self.conn)
        with self.assertRaises(ReceiptMissing) as caught:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(caught.exception.reason_code, "receipt_missing")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["claim_id"], cid)
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "current_receipt_required",
        )

    def test_request_binds_current_receipt_and_flips_item_to_review(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        row = self.review_row(review_id)
        self.assertEqual(row["status"], "requested")
        self.assertEqual(row["receipt_id"], receipt_id)
        self.assertEqual(row["contract_version"], 1)
        self.assertEqual(row["requested_by"], "alice")
        self.assertEqual(row["requested_by_agent"], "alice")
        self.assertIsNone(row["reviewer"])
        self.assertIsNone(row["reviewer_agent_id"])
        self.assertEqual(row["legacy"], 0)
        self.assertIsNone(row["resolved_at"])
        item_row = self.conn.execute(
            "SELECT status, owner_agent_id FROM items WHERE id=?",
            (item,)).fetchone()
        self.assertEqual(item_row["status"], "review")
        self.assertEqual(item_row["owner_agent_id"], "alice")
        # The implementation claim stays open — the owner keeps it current.
        impl = self.conn.execute(
            "SELECT status FROM claims WHERE claim_id=?", (cid,)).fetchone()
        self.assertEqual(impl["status"], "active")
        events = self.events_of("review_requested")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["review_id"], review_id)
        self.assertEqual(payload["receipt_id"], receipt_id)
        # Unnamed review: no recipient entry — queue-discoverable.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM inbox_entries WHERE "
            "category='review_request'").fetchone()[0], 0)

    def test_named_reviewer_gets_the_single_delivery(self):
        self.make_session("bob")
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            reviewer="bob")
        row = self.review_row(review_id)
        self.assertEqual(row["reviewer"], "bob")
        self.assertIsNone(row["reviewer_agent_id"])
        entries = self.conn.execute(
            "SELECT recipient_agent_id, category, payload_json FROM "
            "inbox_entries WHERE category='review_request'").fetchall()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["recipient_agent_id"], "bob")
        payload = json.loads(entries[0]["payload_json"])
        self.assertEqual(payload["review_id"], review_id)

    def test_unregistered_named_reviewer_refused_before_any_row(self):
        item, sid, cid = self.make_working()
        self.submit(cid, sid)
        before = snap(self.conn)
        with self.assertRaises(NotFound) as caught:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=sid, actor="alice",
                reviewer="nobody")
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(caught.exception.reason_code, "target_not_found")
        self.assertEqual(
            caught.exception.evidence["target_agent_id"], "nobody")

    def test_named_human_and_named_owner_refused_before_any_row(self):
        item, sid, cid = self.make_working()
        self.submit(cid, sid)
        for reviewer, exc in (("human", HumanLaneViolation),
                              ("alice", SelfReview)):
            with self.subTest(reviewer=reviewer):
                before = snap(self.conn)
                with self.assertRaises(exc) as caught:
                    coopdb.request_review(
                        self.conn, claim_id=cid, session_id=sid,
                        actor="alice", reviewer=reviewer)
                self.assertEqual(before, snap(self.conn))
                if reviewer == "alice":
                    self.assertEqual(
                        caught.exception.reason_code, "reviewer_is_owner")
                    self.assertEqual(
                        caught.exception.evidence["item_id"], item)
                    self.assertEqual(
                        caught.exception.evidence["actor_agent_id"], "alice")
                    self.assertEqual(
                        caught.exception.evidence["owner_agent_id"], "alice")

    def test_duplicate_live_review_refuses_naming_it(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        with self.assertRaises(InvalidTransition) as ctx:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.assertIn(str(review_id), str(ctx.exception))

    def test_replacement_escape_then_re_request_from_working(self):
        # `review request` is working-only (the matrix admits no request
        # from `review`); the documented route out is a replacement receipt.
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        replacement = self.submit(
            cid, sid, path=self.evidence_file("v2.md", b"new evidence\n"))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id=?", (item,)).fetchone()[0],
            "working")
        second = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.assertNotEqual(second, review_id)
        rows = self.conn.execute(
            "SELECT id, receipt_id FROM reviews WHERE item_id=? ORDER BY id",
            (item,)).fetchall()
        # Append-only history: the dead row stays, the new row binds the
        # replacement receipt.
        self.assertEqual([r["id"] for r in rows], [review_id, second])
        self.assertEqual(rows[1]["receipt_id"], replacement)

    def test_voluntary_request_on_waived_item_is_legal(self):
        item = self.make_item(review_waiver_reason="tiny docs-only change")
        sid = self.make_session("alice")
        cid = self.claim(item, "alice", sid)["claim_id"]
        self.submit(cid, sid)
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        self.assertEqual(self.review_row(review_id)["status"], "requested")


class WaiverPairing(ReceiptBoard):
    def test_core_pairing_waiver_iff_reason(self):
        waived = self.make_item(review_waiver_reason="docs only")
        row = self.conn.execute(
            "SELECT review_required, review_waiver_reason FROM items "
            "WHERE id=?", (waived,)).fetchone()
        self.assertEqual(tuple(row), (0, "docs only"))
        plain = self.make_item()
        row = self.conn.execute(
            "SELECT review_required, review_waiver_reason FROM items "
            "WHERE id=?", (plain,)).fetchone()
        self.assertEqual(tuple(row), (1, None))
        for bad in ("", "   "):
            with self.subTest(reason=repr(bad)):
                with self.assertRaises(IncompleteContract):
                    self.make_item(review_waiver_reason=bad)

    def _run(self, argv, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {"COOP_DB": self.db, **(env or {})}
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

    def _create_argv(self, *extra):
        return ["--json", "item", "create", "--title", "T", "--objective",
                "O", "--scope", "S", "--done-when", "D",
                "--output-contract", "OC", "--context", "C",
                "--allowed-action", "read", "--stop-condition", "stop",
                *extra]

    def test_cli_waiver_flag_and_contract_file_parity(self):
        code, out, err = self._run(
            self._create_argv("--review-waiver", "docs only"))
        self.assertEqual(code, 0, err)
        first = json.loads(out)["item_id"]
        contract = self.workdir / "contract.json"
        contract.write_text(
            json.dumps({"review_waiver": "docs only"}), encoding="utf-8")
        code, out, err = self._run(
            self._create_argv("--contract", str(contract)))
        self.assertEqual(code, 0, err)
        second = json.loads(out)["item_id"]
        rows = self.conn.execute(
            "SELECT review_required, review_waiver_reason FROM items "
            "WHERE id IN (?,?) ORDER BY id", (first, second)).fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [(0, "docs only"), (0, "docs only")])

    def test_cli_require_review_stays_the_default_and_conflicts(self):
        code, out, err = self._run(self._create_argv("--require-review"))
        self.assertEqual(code, 0, err)
        item = json.loads(out)["item_id"]
        row = self.conn.execute(
            "SELECT review_required, review_waiver_reason FROM items "
            "WHERE id=?", (item,)).fetchone()
        self.assertEqual(tuple(row), (1, None))
        code, out, err = self._run(self._create_argv(
            "--require-review", "--review-waiver", "r"))
        self.assertNotEqual(code, 0)


class ReplacementDuringReview(ReviewBoard):
    def test_replacement_transaction_row_by_row(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        bob = self.make_session("bob")
        review_claim = self.claim_rev(review_id, bob)[0]
        replacement = self.submit(
            cid, sid, path=self.evidence_file("v2.md", b"new evidence\n"))
        # 1. supersede: exactly one current receipt, the old one stamped.
        rows = self.receipt_rows(item)
        self.assertIsNotNone(rows[0]["superseded_at"])
        self.assertIsNone(rows[1]["superseded_at"])
        self.assertEqual(rows[1]["receipt_id"], replacement)
        # 2. status reset review -> working.
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id=?", (item,)).fetchone()[0],
            "working")
        # 3. the active review-lane claim closes with the pinned reason.
        lane = self.review_lane(review_id)
        self.assertEqual(
            [(r["status"], r["close_reason"]) for r in lane],
            [("closed", "receipt_superseded")])
        # 4. one review-death delivery to the claimant.
        entries = self.conn.execute(
            "SELECT recipient_agent_id, payload_json FROM inbox_entries "
            "WHERE category='review_death'").fetchall()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["recipient_agent_id"], "bob")
        payload = json.loads(entries[0]["payload_json"])
        self.assertEqual(payload["review_id"], review_id)
        self.assertEqual(payload["superseded_receipt_id"], receipt_id)
        # The one receipt_submitted event carries the death in its payload.
        event = json.loads(
            self.events_of("receipt_submitted")[-1]["payload_json"])
        self.assertEqual(event["review_death"], review_id)
        self.assertEqual(event["closed_review_claim_id"],
                         review_claim["claim_id"])
        # The review row itself: append-only, unresolved, dead by derivation.
        row = self.review_row(review_id)
        self.assertEqual(row["status"], "requested")
        self.assertIsNone(row["resolved_at"])

    def test_replacement_notifies_named_reviewer_when_unclaimed(self):
        self.make_session("bob")
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            reviewer="bob")
        self.submit(cid, sid, path=self.evidence_file("v2.md", b"n\n"))
        deaths = self.conn.execute(
            "SELECT recipient_agent_id FROM inbox_entries WHERE "
            "category='review_death'").fetchall()
        self.assertEqual([r[0] for r in deaths], ["bob"])

    def test_replacement_is_one_transaction(self):
        # Force the last leg (the review-death delivery) to fail: the
        # supersede, the status reset, and the claim close all roll back
        # with it — nothing about the replacement is half-visible.
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        bob = self.make_session("bob")
        self.claim_rev(review_id, bob)
        before = snap(self.conn)
        real_deliver = coopdb.deliver

        def failing_deliver(conn, **kwargs):
            if kwargs.get("category") == "review_death":
                raise RuntimeError("injected delivery failure")
            return real_deliver(conn, **kwargs)

        with unittest.mock.patch.object(coopdb, "deliver", failing_deliver):
            with self.assertRaises(RuntimeError):
                self.submit(
                    cid, sid,
                    path=self.evidence_file("v2.md", b"new evidence\n"))
        self.assertEqual(before, snap(self.conn))


class ClaimEligibility(ReviewBoard):
    def test_claim_returns_packet_with_decisions_and_cursor_untouched(self):
        self.make_session("bob")
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            reviewer="bob")
        self.seed_decision(item, text="use sqlite")
        bob_sid = self.conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id='bob'"
        ).fetchone()["session_id"]
        unread_before = len(coopdb.read_inbox(self.conn, "bob", peek=True))
        self.assertGreaterEqual(unread_before, 1)  # the review_request
        claim, packet = self.claim_rev(review_id, bob_sid)
        self.assertEqual(claim["review_id"], review_id)
        self.assertEqual(claim["lane"], f"review:{review_id}")
        self.assertEqual(
            [d["text"] for d in packet["decisions"]], ["use sqlite"])
        # The canonical inbox offset is unchanged and nothing was consumed.
        self.assertIsNone(self.offset_of("bob"))
        self.assertEqual(
            len(coopdb.read_inbox(self.conn, "bob", peek=True)),
            unread_before)
        # The informational stamp records the claimant.
        self.assertEqual(
            self.review_row(review_id)["reviewer_agent_id"], "bob")

    def test_review_claim_long_lease_outlasts_the_default(self):
        self.make_session("bob")
        bob_sid = self.conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id='bob'"
        ).fetchone()["session_id"]
        *_, rev_default = self.make_reviewed(agent="alice", reviewer="bob")
        *_, rev_long = self.make_reviewed(agent="carol", reviewer="bob")
        d, _ = coopdb.claim_review(
            self.conn, review_id=rev_default, session_id=bob_sid,
            intent="default lease")
        long_claim, _ = coopdb.claim_review(
            self.conn, review_id=rev_long, session_id=bob_sid,
            intent="thorough review", lease_seconds=7200)
        # same (frozen) claim instant → the long lease expires strictly later.
        self.assertGreater(
            long_claim["lease_expires_at"], d["lease_expires_at"])

    def test_owner_as_claimant_refused_self_review(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        before = snap(self.conn)
        with self.assertRaises(SelfReview) as caught:
            self.claim_rev(review_id, sid)
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(caught.exception.reason_code, "reviewer_is_owner")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["review_id"], review_id)
        self.assertEqual(caught.exception.evidence["actor_agent_id"], "alice")
        self.assertEqual(caught.exception.evidence["owner_agent_id"], "alice")

    def test_dead_review_refused_review_stale_at_first_claim(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        self.submit(cid, sid, path=self.evidence_file("v2.md", b"n\n"))
        bob = self.make_session("bob")
        with self.assertRaises(ReviewStale) as caught:
            self.claim_rev(review_id, bob)
        self.assertEqual(caught.exception.reason_code, "review_stale")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["review_id"], review_id)
        self.assertEqual(
            caught.exception.evidence["current_status"], "requested")
        legacy = self.seed_legacy_review(item, status="requested")
        with self.assertRaises(ReviewStale) as caught:
            self.claim_rev(legacy, bob)
        self.assertEqual(caught.exception.reason_code, "review_stale")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["review_id"], legacy)

    def test_named_designation_enforced(self):
        self.make_session("bob")
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            reviewer="bob")
        carol = self.make_session("carol")
        before = snap(self.conn)
        with self.assertRaises(AddressedTargetMismatch):
            self.claim_rev(review_id, carol)
        self.assertEqual(before, snap(self.conn))
        bob_sid = self.conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id='bob'"
        ).fetchone()["session_id"]
        claim, _ = self.claim_rev(review_id, bob_sid)
        self.assertEqual(self.review_lane(review_id)[-1]["status"], "active")

    def test_unnamed_claim_refuses_provider_already_approved_receipt(self):
        bob_sid = self.make_session("bob", provider="codex")
        bob2_sid = self.make_session("bob2", provider="codex")
        carol_sid = self.make_session("carol", provider="grok")
        item, alice_sid, cid, receipt_id, first_review = self.make_reviewed(
            review_quorum=2)
        first_claim, _ = self.claim_rev(first_review, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id=bob_sid, actor="bob", verdict="approve")
        second_review = coopdb.request_review(
            self.conn, claim_id=cid, session_id=alice_sid, actor="alice")

        before = snap(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            self.claim_rev(second_review, bob2_sid)
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(
            caught.exception.reason_code,
            "review_provider_already_approved")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["review_id"], second_review)
        self.assertEqual(
            caught.exception.evidence["receipt_id"], receipt_id)
        self.assertEqual(caught.exception.evidence["provider"], "codex")

        claim, _ = self.claim_rev(second_review, carol_sid)
        self.assertEqual(claim["review_id"], second_review)

    def test_first_review_refuses_when_no_provider_is_registered(self):
        """A binding review is not opened without any provider that can
        claim it; registration happens before retrying the request."""
        item, alice_sid, cid = self.make_working(
            review_quorum=2, ensure_review_provider=False)
        receipt_id = self.submit(cid, alice_sid, agent="alice")

        before = snap(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=alice_sid, actor="alice")
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(
            caught.exception.reason_code, "second_reviewer_not_selected")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["receipt_id"], receipt_id)
        self.assertEqual(
            list(caught.exception.evidence["approved_providers"]), [])
        self.assertEqual(
            list(caught.exception.evidence["remaining_providers"]), [])

    def test_null_provider_is_not_remaining_review_capacity(self):
        """An unbound agent cannot prove a distinct provider is available."""
        bob_sid = self.make_session("bob", provider="codex")
        item, alice_sid, cid, _receipt_id, first_review = self.make_reviewed(
            review_quorum=2)
        first_claim, _ = self.claim_rev(first_review, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id=bob_sid, actor="bob", verdict="approve")
        coopdb.register_agent(self.conn, "carol")  # provider remains NULL

        block = coopdb.second_reviewer_blocked(self.conn, item)
        self.assertTrue(block["blocked"], block)
        self.assertEqual(block["remaining_providers"], [])
        before = snap(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=alice_sid, actor="alice")
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(
            caught.exception.reason_code, "second_reviewer_not_selected")

    def test_named_request_refuses_unbound_reviewer(self):
        """Other board capacity cannot make an unbound designation safe."""
        self.make_session("bob", provider="codex")
        coopdb.register_agent(self.conn, "carol")  # provider remains NULL
        item, alice_sid, cid = self.make_working(review_quorum=2)
        receipt_id = self.submit(cid, alice_sid, agent="alice")

        before = snap(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=alice_sid, actor="alice",
                reviewer="carol")
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(
            caught.exception.reason_code, "second_reviewer_not_selected")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["receipt_id"], receipt_id)
        self.assertEqual(
            caught.exception.evidence["required_agent_id"], "carol")
        self.assertEqual(
            caught.exception.evidence["constraint"],
            "bound_review_provider_required")

    def test_request_review_refuses_when_no_remaining_distinct_provider(self):
        """P1: cannot open another review if no provider can expand quorum."""
        bob_sid = self.make_session("bob", provider="codex")
        # No third-provider agent registered.
        item, alice_sid, cid, receipt_id, first_review = self.make_reviewed(
            review_quorum=2)
        first_claim, _ = self.claim_rev(first_review, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id=bob_sid, actor="bob", verdict="approve")
        before = snap(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=alice_sid, actor="alice")
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(
            caught.exception.reason_code, "second_reviewer_not_selected")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["receipt_id"], receipt_id)
        self.assertEqual(caught.exception.evidence["required_count"], 1)
        self.assertEqual(
            list(caught.exception.evidence["approved_providers"]), ["codex"])
        self.assertEqual(
            list(caught.exception.evidence["remaining_providers"]), [])

    def test_named_request_refuses_provider_already_approved_receipt(self):
        bob_sid = self.make_session("bob", provider="codex")
        self.make_session("bob2", provider="codex")
        self.make_session("carol", provider="grok")
        item, alice_sid, cid, receipt_id, first_review = self.make_reviewed(
            review_quorum=2)
        first_claim, _ = self.claim_rev(first_review, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id=bob_sid, actor="bob", verdict="approve")

        before = snap(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.request_review(
                self.conn, claim_id=cid, session_id=alice_sid, actor="alice",
                reviewer="bob2")
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(
            caught.exception.reason_code,
            "review_provider_already_approved")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(
            caught.exception.evidence["receipt_id"], receipt_id)
        self.assertEqual(caught.exception.evidence["provider"], "codex")

    def test_named_claim_refuses_provider_already_approved_receipt(self):
        """The claim guard also protects review rows created before the
        request-time provider check existed."""
        bob_sid = self.make_session("bob", provider="codex")
        bob2_sid = self.make_session("bob2", provider="codex")
        self.make_session("carol", provider="grok")
        item, alice_sid, _cid, receipt_id, first_review = self.make_reviewed(
            review_quorum=2)
        first_claim, _ = self.claim_rev(first_review, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id=bob_sid, actor="bob", verdict="approve")
        stamp = coopdb.now()
        cur = self.conn.execute(
            "INSERT INTO reviews(item_id,receipt_id,contract_version,"
            "requested_by,requested_by_agent,reviewer,reviewer_agent_id,"
            "status,legacy,created_at) VALUES (?,?,?,?,?,?,NULL,"
            "'requested',0,?)",
            (item, receipt_id, 1, "alice", "alice", "bob2", stamp))
        second_review = cur.lastrowid
        self.conn.execute(
            "UPDATE items SET status='review', updated_at=? WHERE id=?",
            (stamp, item))
        self.conn.commit()

        before = snap(self.conn)
        with self.assertRaises(InvalidTransition) as caught:
            self.claim_rev(second_review, bob2_sid)
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(
            caught.exception.reason_code,
            "review_provider_already_approved")
        self.assertEqual(caught.exception.evidence["provider"], "codex")

    def test_named_verdict_refuses_second_approve_from_same_provider(self):
        """Verdict guard protects a claim created before P1 claim checks."""
        bob_sid = self.make_session("bob", provider="codex")
        bob2_sid = self.make_session("bob2", provider="codex")
        self.make_session("carol", provider="grok")
        item, alice_sid, cid, receipt_id, first_review = self.make_reviewed(
            review_quorum=2)
        first_claim, _ = self.claim_rev(first_review, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=first_claim["claim_id"],
            session_id=bob_sid, actor="bob", verdict="approve")
        stamp = coopdb.now()
        cur = self.conn.execute(
            "INSERT INTO reviews(item_id,receipt_id,contract_version,"
            "requested_by,requested_by_agent,reviewer,reviewer_agent_id,"
            "status,legacy,created_at) VALUES (?,?,?,?,?,?,NULL,"
            "'requested',0,?)",
            (item, receipt_id, 1, "alice", "alice", "bob2", stamp))
        second_review = cur.lastrowid
        self.conn.execute(
            "UPDATE items SET status='review', updated_at=? WHERE id=?",
            (stamp, item))
        self.conn.commit()
        # Simulate a historical claim that predates the claim-time guard.
        with unittest.mock.patch(
                "agent_coop.coopdb._current_approval_providers",
                return_value=set()):
            second_claim, _ = self.claim_rev(second_review, bob2_sid)
        with self.assertRaises(InvalidTransition) as caught:
            coopdb.submit_verdict(
                self.conn, claim_id=second_claim["claim_id"],
                session_id=bob2_sid, actor="bob2", verdict="approve")
        self.assertEqual(
            caught.exception.reason_code,
            "review_provider_already_approved")
        self.assertEqual(caught.exception.evidence["provider"], "codex")

    def test_designation_immutable_across_generations_and_reopen(self):
        # Unnamed review: B claims, goes safely stale; C reclaims; reopen the
        # database; C goes safely stale; D reclaims. reviewer stays NULL the
        # whole way; reviewer_agent_id tracks the latest claimant; the claims
        # lane is the authority.
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        holders = []
        for agent in ("bob", "carol", "dave"):
            agent_sid = self.make_session(agent)
            claim, _ = self.claim_rev(
                review_id, agent_sid,
                reason=None if agent == "bob" else f"{agent} takes over")
            holders.append(claim["claim_id"])
            row = self.review_row(review_id)
            self.assertIsNone(row["reviewer"])
            self.assertEqual(row["reviewer_agent_id"], agent)
            if agent == "dave":
                break
            self.clock.advance(LEASE + 1)
            coopdb.sweep_expired(self.conn)
            coopdb.finish_session(
                self.conn, agent_sid, status="exited", reason="child_exit",
                exit_code=0)
            if agent == "bob":
                # Survives a database reopen mid-lifecycle.
                self.conn.close()
                self.conn = coopdb.connect(self.db)
                self.addCleanup(self.conn.close)
        lane = self.review_lane(review_id)
        self.assertEqual([r["claim_id"] for r in lane], holders)
        self.assertEqual([r["status"] for r in lane],
                         ["stale", "stale", "active"])
        tokens = [r["fencing_token"] for r in lane]
        self.assertEqual(tokens, sorted(tokens))
        self.assertLess(tokens[0], tokens[1])
        self.assertLess(tokens[1], tokens[2])

    def test_named_review_refuses_others_forever(self):
        self.make_session("bob")
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            reviewer="bob")
        bob_sid = self.conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id='bob'"
        ).fetchone()["session_id"]
        self.claim_rev(review_id, bob_sid)
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        coopdb.finish_session(
            self.conn, bob_sid, status="exited", reason="child_exit",
            exit_code=0)
        carol = self.make_session("carol")
        # Safely stale — but the designation still refuses carol, and does
        # so across a database reopen.
        with self.assertRaises(AddressedTargetMismatch):
            self.claim_rev(review_id, carol, reason="bob is gone")
        self.conn.close()
        self.conn = coopdb.connect(self.db)
        self.addCleanup(self.conn.close)
        with self.assertRaises(AddressedTargetMismatch):
            self.claim_rev(review_id, carol, reason="bob is still gone")
        self.assertEqual(self.review_row(review_id)["reviewer"], "bob")


class ReviewLaneLifecycle(ReviewBoard):
    def _staled_lane(self):
        """Bob claims the review, his lease lapses, the sweep flips it while
        his session still reports running."""
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        bob_sid = self.make_session("bob")
        claim, _ = self.claim_rev(review_id, bob_sid)
        self.clock.advance(LEASE + 1)
        flipped = coopdb.sweep_expired(self.conn)
        self.assertIn(claim["claim_id"], flipped)
        return item, review_id, bob_sid, claim

    def test_swept_stale_running_predecessor_refused(self):
        # The lane-history guard regression in the review lane (third consumer).
        item, review_id, bob_sid, claim = self._staled_lane()
        carol = self.make_session("carol")
        before = snap(self.conn)
        with self.assertRaises(UnsafeReclaim):
            self.claim_rev(review_id, carol, reason="seize it")
        self.assertEqual(before, snap(self.conn))

    def test_terminal_predecessor_reclaims_with_greater_generation(self):
        item, review_id, bob_sid, claim = self._staled_lane()
        coopdb.finish_session(
            self.conn, bob_sid, status="exited", reason="child_exit",
            exit_code=0)
        carol = self.make_session("carol")
        fresh, _ = self.claim_rev(
            review_id, carol, reason="predecessor exited")
        lane = self.review_lane(review_id)
        self.assertEqual([r["status"] for r in lane], ["stale", "active"])
        self.assertGreater(lane[1]["fencing_token"], lane[0]["fencing_token"])

    def test_released_lane_reclaims_while_releaser_still_runs(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        bob_sid = self.make_session("bob")
        claim, _ = self.claim_rev(review_id, bob_sid)
        coopdb.release_claim(
            self.conn, claim_id=claim["claim_id"], actor="bob",
            session_id=bob_sid, reason="stepping away")
        carol = self.make_session("carol")
        fresh, _ = self.claim_rev(review_id, carol, reason="picking it up")
        self.assertEqual(self.review_lane(review_id)[-1]["status"], "active")
        self.assertEqual(
            self.review_row(review_id)["reviewer_agent_id"], "carol")

    def test_active_lane_collides_and_reasonless_history_refused(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        bob_sid = self.make_session("bob")
        claim, _ = self.claim_rev(review_id, bob_sid)
        carol = self.make_session("carol")
        with self.assertRaises(ClaimCollision):
            self.claim_rev(review_id, carol)
        coopdb.release_claim(
            self.conn, claim_id=claim["claim_id"], actor="bob",
            session_id=bob_sid, reason="done for now")
        with self.assertRaises(InvalidTransition):
            self.claim_rev(review_id, carol)  # history demands a reason


class PacketRendering(ReviewBoard):
    def test_dual_claim_packet_tokenless(self):
        self.make_session("bob")
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            reviewer="bob")
        bob_sid = self.conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id='bob'"
        ).fetchone()["session_id"]
        claim, packet = self.claim_rev(review_id, bob_sid)
        self.assertEqual(set(packet["review"]), REVIEW_SLOT_KEYS)
        review = packet["review"]
        self.assertEqual(review["review_id"], review_id)
        self.assertEqual(review["status"], "requested")
        self.assertEqual(review["reviewer"], "bob")
        self.assertEqual(review["reviewer_agent_id"], "bob")
        self.assertEqual(review["requested_by"], "alice")
        self.assertEqual(review["receipt_id"], receipt_id)
        self.assertEqual(review["contract_version"], 1)
        # Dual rendering: the implementation claim slot is alice's, the
        # review slot carries bob's claim beside it.
        self.assertEqual(packet["claim"]["agent"], "alice")
        self.assertEqual(review["claim"]["agent"], "bob")
        self.assertEqual(review["claim"]["claim_id"], claim["claim_id"])
        self.assertEqual(review["claim"]["session_id"], bob_sid[:8])
        self.assertNotIn("fencing_token", json.dumps(packet))
        rendered = projection.render_inbox(self.conn, "bob")
        self.assertNotIn("fencing_token", rendered)

    def test_legacy_reviews_render_without_fabricated_claim_state(self):
        item, sid, cid = self.make_working()
        requested = self.seed_legacy_review(
            item, status="requested", reviewer="bob")
        resolved = self.seed_legacy_review(
            item, status="approved", reviewer=None, resolved=True)
        packet = coopdb.item_show(self.conn, item, packet=True)
        # The packet's current-review pick is the latest open row; its claim
        # slot is None because no claims-lane row exists — nothing invented.
        self.assertEqual(packet["review"]["review_id"], requested)
        self.assertIsNone(packet["review"]["claim"])
        self.assertIsNone(packet["review"]["receipt_id"])
        history = coopdb.item_show(self.conn, item, history=True)
        self.assertEqual(
            [r["id"] for r in history["reviews"]], [requested, resolved])
        self.assertEqual(
            [r["legacy"] for r in history["reviews"]], [1, 1])
        self.assertNotIn("fencing_token", json.dumps(history["reviews"]))


class ReviewCLI(ReviewBoard):
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

    def test_cli_request_then_claim_happy_json(self):
        self.make_session("bob")
        item, sid, cid = self.make_working()
        self.submit(cid, sid)
        code, out, err = self._run(
            ["--json", "review", "request", "--claim", str(cid),
             "--reviewer", "bob"],
            {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"})
        self.assertEqual(code, 0, err)
        review_id = json.loads(out)["review_id"]
        bob_sid = self.conn.execute(
            "SELECT session_id FROM sessions WHERE agent_id='bob'"
        ).fetchone()["session_id"]
        code, out, err = self._run(
            ["--json", "review", "claim", str(review_id),
             "--intent", "review it"],
            {"COOP_SESSION_ID": bob_sid, "COOP_AGENT": "bob"})
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["review_id"], review_id)
        self.assertEqual(data["packet"]["review"]["reviewer_agent_id"], "bob")
        self.assertNotIn("fencing_token", out)

    def test_cli_claim_pairing_validation(self):
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        bob_sid = self.make_session("bob")
        env = {"COOP_SESSION_ID": bob_sid, "COOP_AGENT": "bob"}
        code, out, err = self._run(
            ["review", "claim", str(review_id), "--intent", "i",
             "--reclaim"], env)
        self.assertEqual(code, 1)
        self.assertIn("invalid_transition", err)
        code, out, err = self._run(
            ["review", "claim", str(review_id), "--intent", "i",
             "--reason", "r"], env)
        self.assertEqual(code, 1)
        self.assertIn("invalid_transition", err)


class VerdictBoard(ReviewBoard):
    """Fixture: a requested review claimed by a non-owner reviewer."""

    def make_claimed_review(self, agent="alice", reviewer_agent="bob",
                            **item_over):
        item, sid, cid, receipt_id, review_id = self.make_reviewed(
            agent, **item_over)
        rev_sid = self.make_session(reviewer_agent)
        claim, packet = self.claim_rev(review_id, rev_sid)
        return {"item": item, "owner_sid": sid, "impl_cid": cid,
                "receipt_id": receipt_id, "review_id": review_id,
                "rev_sid": rev_sid, "rev_cid": claim["claim_id"],
                "packet": packet}

    def verdict(self, claim_id, sid, actor="bob", verdict="approve",
                body=None, conn=None):
        return coopdb.submit_verdict(
            conn or self.conn, claim_id=claim_id, session_id=sid,
            actor=actor, verdict=verdict, body=body)

    def decide(self, cid, sid, agent="alice", text="use sqlite"):
        return coopdb.record_decision(
            self.conn, claim_id=cid, session_id=sid, actor=agent, text=text)

    def observe(self, rev_cid, rev_sid, actor="bob"):
        return coopdb.checkpoint(
            self.conn, ctype="step", claim_id=rev_cid, actor=actor,
            session_id=rev_sid, note="observed")

    def item_row(self, item):
        return self.conn.execute(
            "SELECT * FROM items WHERE id=?", (item,)).fetchone()

    def claim_row(self, cid):
        return self.conn.execute(
            "SELECT * FROM claims WHERE claim_id=?", (cid,)).fetchone()


class VerdictResolution(VerdictBoard):
    def test_approve_resolves_with_owner_delivery(self):
        f = self.make_claimed_review()
        self.verdict(f["rev_cid"], f["rev_sid"], verdict="approve",
                     body="looks right")
        rev = self.review_row(f["review_id"])
        self.assertEqual(rev["status"], "approved")
        self.assertIsNotNone(rev["resolved_at"])
        # Approve keeps the item in review (review -> done goes
        # through completion only); the review claim is completed.
        self.assertEqual(self.item_row(f["item"])["status"], "review")
        rclaim = self.claim_row(f["rev_cid"])
        self.assertEqual(rclaim["status"], "completed")
        events = self.conn.execute(
            "SELECT * FROM events WHERE event_type='review_resolved'").fetchall()
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload_json"])
        self.assertEqual(payload["verdict"], "approve")
        self.assertNotIn("fencing_token", payload)
        entries = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='alice' "
            "AND category='review_verdict'").fetchall()
        self.assertEqual(len(entries), 1)
        self.assertNotIn("fencing_token", entries[0]["payload_json"])

    def test_changes_resolves_supersedes_and_reopens_working(self):
        f = self.make_claimed_review()
        self.verdict(f["rev_cid"], f["rev_sid"], verdict="changes",
                     body="missing edge case")
        rev = self.review_row(f["review_id"])
        self.assertEqual(rev["status"], "changes")
        self.assertIsNotNone(rev["resolved_at"])
        self.assertEqual(self.item_row(f["item"])["status"], "working")
        receipt = self.conn.execute(
            "SELECT * FROM receipts WHERE receipt_id=?",
            (f["receipt_id"],)).fetchone()
        self.assertIsNotNone(receipt["superseded_at"])
        superseded = self.conn.execute(
            "SELECT * FROM events WHERE event_type='receipt_superseded'"
        ).fetchall()
        self.assertEqual(len(superseded), 1)
        self.assertEqual(
            json.loads(superseded[0]["payload_json"])["reason"], "changes")
        # Completion groundwork sees no current receipt, and a same-receipt
        # re-request is impossible by construction.
        self.assertIsNone(coopdb._current_receipt(self.conn, f["item"]))
        with self.assertRaises(ReceiptMissing):
            coopdb.request_review(
                self.conn, claim_id=f["impl_cid"], session_id=f["owner_sid"],
                actor="alice")
        # Delivery reached the requesting owner.
        entries = self.conn.execute(
            "SELECT * FROM inbox_entries WHERE recipient_agent_id='alice' "
            "AND category='review_verdict'").fetchall()
        self.assertEqual(len(entries), 1)

    def test_verdict_value_validated(self):
        f = self.make_claimed_review()
        with self.assertRaises(InvalidTransition):
            self.verdict(f["rev_cid"], f["rev_sid"], verdict="maybe")


class ObservationWatermark(VerdictBoard):
    def test_decision_before_claim_verdicts_immediately(self):
        # The claim returned the packet with the decision — the claim event
        # is the initial watermark, and nothing is newer.
        item, sid, cid = self.make_working()
        self.submit(cid, sid)
        self.decide(cid, sid, text="pre-claim decision")
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        rev_sid = self.make_session("bob")
        claim, packet = self.claim_rev(review_id, rev_sid)
        self.assertEqual(len(packet["decisions"]), 1)
        self.verdict(claim["claim_id"], rev_sid, verdict="approve")
        self.assertEqual(self.review_row(review_id)["status"], "approved")

    def test_unobserved_decision_refuses_then_one_checkpoint_unblocks(self):
        f = self.make_claimed_review()
        decision_id = self.decide(
            f["impl_cid"], f["owner_sid"], text="mid-review pivot")
        before = snap(self.conn)
        with self.assertRaises(DecisionUnobserved) as caught:
            self.verdict(f["rev_cid"], f["rev_sid"], verdict="approve")
        # The refusal is side-effect-free: the claim stays live and the
        # review unresolved, so one checkpoint unblocks.
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(caught.exception.reason_code, "decision_unobserved")
        self.assertEqual(caught.exception.evidence["item_id"], f["item"])
        self.assertEqual(
            caught.exception.evidence["review_id"], f["review_id"])
        self.assertEqual(
            caught.exception.evidence["decision_id"], decision_id)
        self.assertEqual(self.claim_row(f["rev_cid"])["status"], "active")
        self.assertEqual(self.review_row(f["review_id"])["status"],
                         "requested")
        self.observe(f["rev_cid"], f["rev_sid"])
        self.verdict(f["rev_cid"], f["rev_sid"], verdict="approve")
        self.assertEqual(self.review_row(f["review_id"])["status"],
                         "approved")

    def test_decision_then_checkpoint_then_approve_succeeds(self):
        f = self.make_claimed_review()
        self.decide(f["impl_cid"], f["owner_sid"], text="observed pivot")
        self.observe(f["rev_cid"], f["rev_sid"])
        self.verdict(f["rev_cid"], f["rev_sid"], verdict="approve")
        self.assertEqual(self.review_row(f["review_id"])["status"],
                         "approved")

    def test_decision_vs_approve_race_no_blind_approval(self):
        # Real two-connection race: BEGIN IMMEDIATE serializes on the write
        # lock. Decision-first must refuse the verdict; verdict-first must
        # commit an approval the later decision kills by event order. In no
        # interleaving does an approval survive with an unobserved-or-later
        # decision standing.
        f = self.make_claimed_review()
        barrier = threading.Barrier(2)
        results = [None, None]

        def race(i, fn):
            conn = coopdb.connect(self.db)
            try:
                barrier.wait(timeout=10)
                try:
                    results[i] = ("ok", fn(conn))
                except coopdb.CoopError as exc:
                    results[i] = ("err", exc)
            finally:
                conn.close()

        def do_decide(conn):
            return coopdb.record_decision(
                conn, claim_id=f["impl_cid"], session_id=f["owner_sid"],
                actor="alice", text="racing pivot")

        def do_verdict(conn):
            return coopdb.submit_verdict(
                conn, claim_id=f["rev_cid"], session_id=f["rev_sid"],
                actor="bob", verdict="approve")

        threads = [threading.Thread(target=race, args=(0, do_decide)),
                   threading.Thread(target=race, args=(1, do_verdict))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "race thread deadlocked")

        self.assertEqual(results[0][0], "ok",
                         f"decision must always commit: {results[0]!r}")
        decision_event = self.conn.execute(
            "SELECT MAX(event_id) AS m FROM events WHERE "
            "event_type='decision_recorded'").fetchone()["m"]
        rev = self.review_row(f["review_id"])
        if results[1][0] == "err":
            # Decision won the lock: the verdict saw it unobserved.
            self.assertIsInstance(results[1][1], DecisionUnobserved)
            self.assertEqual(rev["status"], "requested")
            self.assertEqual(self.claim_row(f["rev_cid"])["status"],
                             "active")
        else:
            # Verdict won: the approval committed, and the later decision
            # kills it by event order (the completion gate's derivation).
            self.assertEqual(rev["status"], "approved")
            resolve_event = self.conn.execute(
                "SELECT MAX(event_id) AS m FROM events WHERE "
                "event_type='review_resolved'").fetchone()["m"]
            self.assertIsNotNone(resolve_event)
            self.assertGreater(decision_event, resolve_event)


class ReviewStateClosure(VerdictBoard):
    def test_needs_input_and_blocked_refuse_from_review(self):
        f = self.make_claimed_review()
        for label, op in (
                ("needs_input", lambda: coopdb.needs_input(
                    self.conn, claim_id=f["impl_cid"],
                    session_id=f["owner_sid"], to_agent="bob",
                    question="may I?")),
                ("blocked", lambda: coopdb.checkpoint(
                    self.conn, ctype="blocked", claim_id=f["impl_cid"],
                    actor="alice", session_id=f["owner_sid"],
                    note="stuck"))):
            with self.subTest(transition=label):
                before = snap(self.conn)
                with self.assertRaises(InvalidTransition):
                    op()
                self.assertEqual(before, snap(self.conn))
        # The non-transition checkpoint types stay legal from review — they
        # keep the retained implementation claim fresh.
        coopdb.checkpoint(
            self.conn, ctype="step", claim_id=f["impl_cid"], actor="alice",
            session_id=f["owner_sid"], note="still here")
        self.assertEqual(self.item_row(f["item"])["status"], "review")

    def test_handoff_create_guard_refuses_from_review(self):
        # Handoff wiring point: create_handoff routes through this guard;
        # the guard itself is the review-state closure.
        f = self.make_claimed_review()
        row = self.item_row(f["item"])
        with self.assertRaises(InvalidTransition):
            coopdb._refuse_from_review(row, "handoff create")
        working = self.make_item(title="open")
        sid2 = self.make_session("carol")
        self.claim(working, "carol", sid2)
        self.assertIsNone(coopdb._refuse_from_review(
            self.item_row(working), "handoff create"))

    def test_reclaim_during_review_preserves_review_and_pending_review(self):
        # A naive reclaim write would reset status unconditionally; the
        # carve-out preserves review — reclaim re-arms the lane, it does
        # not rewind the state machine.
        f = self.make_claimed_review()
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        coopdb.finish_session(
            self.conn, f["owner_sid"], status="exited", reason="child_exit",
            exit_code=0)
        carol = self.make_session("carol")
        self.claim(f["item"], "carol", carol,
                   reclaim_reason="owner session died")
        row = self.item_row(f["item"])
        self.assertEqual(row["status"], "review")
        self.assertEqual(row["owner_agent_id"], "carol")
        rev = self.review_row(f["review_id"])
        self.assertEqual(rev["status"], "requested")
        self.assertIsNone(rev["resolved_at"])

    def test_takeover_mid_review_verdict_refused_self_review(self):
        # Bob claims the review, then takes over the implementation lane —
        # becoming the owner — and his verdict must be refused at verdict
        # time (the claim-time check cannot see the future takeover).
        item, sid, cid, receipt_id, review_id = self.make_reviewed()
        self.clock.advance(10)
        bob_sid = self.make_session("bob")
        claim, _ = self.claim_rev(review_id, bob_sid)
        self.clock.advance(LEASE - 10 + 1)  # alice expired; bob still live
        coopdb.sweep_expired(self.conn)
        coopdb.finish_session(
            self.conn, sid, status="exited", reason="child_exit",
            exit_code=0)
        self.claim(item, "bob", bob_sid, reclaim_reason="taking it over")
        self.assertEqual(self.item_row(item)["owner_agent_id"], "bob")
        self.assertEqual(self.item_row(item)["status"], "review")
        before = snap(self.conn)
        with self.assertRaises(SelfReview) as caught:
            self.verdict(claim["claim_id"], bob_sid, verdict="approve")
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(caught.exception.reason_code, "reviewer_is_owner")
        self.assertEqual(caught.exception.evidence["item_id"], item)
        self.assertEqual(caught.exception.evidence["review_id"], review_id)
        self.assertEqual(caught.exception.evidence["actor_agent_id"], "bob")
        self.assertEqual(caught.exception.evidence["owner_agent_id"], "bob")


class VerdictSlugPrecedence(VerdictBoard):
    def test_verdict_via_replacement_closed_claim_is_stale_claim(self):
        # Claim validation runs first: a review claim closed by the
        # replacement transaction refuses stale_claim before any
        # derivation.
        f = self.make_claimed_review()
        self.submit(f["impl_cid"], f["owner_sid"],
                    path=self.evidence_file("fresh.md", b"fresh bytes\n"))
        with self.assertRaises(StaleClaim):
            self.verdict(f["rev_cid"], f["rev_sid"], verdict="approve")

    def test_live_claim_on_dead_review_is_review_stale(self):
        # Seeded divergence: a future supersede path (the
        # completion-time file failure) retires the receipt without closing
        # the review claim; the verdict's own derivation must refuse.
        f = self.make_claimed_review()
        self.conn.execute(
            "UPDATE receipts SET superseded_at=? WHERE receipt_id=?",
            (coopdb.now(), f["receipt_id"]))
        self.conn.commit()
        self.assertEqual(self.claim_row(f["rev_cid"])["status"], "active")
        with self.assertRaises(ReviewStale) as caught:
            self.verdict(f["rev_cid"], f["rev_sid"], verdict="approve")
        self.assertEqual(caught.exception.reason_code, "review_stale")
        self.assertEqual(caught.exception.evidence["item_id"], f["item"])
        self.assertEqual(
            caught.exception.evidence["review_id"], f["review_id"])
        self.assertEqual(
            caught.exception.evidence["current_status"], "requested")


class VerdictCLI(VerdictBoard):
    _run = ReviewCLI._run

    def test_cli_submit_happy_and_json(self):
        f = self.make_claimed_review()
        env = {"COOP_SESSION_ID": f["rev_sid"], "COOP_AGENT": "bob"}
        code, out, err = self._run(
            ["--json", "review", "submit", "--claim", str(f["rev_cid"]),
             "--verdict", "approve", "--body", "solid"], env)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["review_id"], f["review_id"])
        self.assertEqual(data["verdict"], "approve")
        self.assertNotIn("fencing_token", out)
        self.assertEqual(self.review_row(f["review_id"])["status"],
                         "approved")

    def test_cli_submit_requires_known_verdict(self):
        f = self.make_claimed_review()
        env = {"COOP_SESSION_ID": f["rev_sid"], "COOP_AGENT": "bob"}
        code, out, err = self._run(
            ["review", "submit", "--claim", str(f["rev_cid"]),
             "--verdict", "shrug"], env)
        self.assertEqual(code, 2)  # argparse choices rejection


if __name__ == "__main__":
    unittest.main()
