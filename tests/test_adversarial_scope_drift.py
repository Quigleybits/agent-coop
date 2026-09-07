"""Adversarial trial — failure class: scope drift changing
freeze/acceptance semantics without revise + re-review.

Two legs: source-path enumeration and a live replay. Leg (a) enumerates
by source scan every writer of
the contract columns, `review_required`/`review_waiver_reason`, and the
event-carried `review_quorum` policy: only create/define/refine (draft
phase) and `revise_item` (the human lane) may write them — any new
writer turns these tests red before it ships. Leg (b) replays the
attack on a live board: a mid-run quorum downgrade or waiver injection
to dodge the second reviewer is refused under a live claim and from
inside any session; after an honest release the revise lands but kills
the banked receipt+approval, so completion still costs a fresh receipt
and a fresh review cycle under the new contract version.
"""

import ast
import inspect
import json
import pathlib
import unittest
import unittest.mock

from agent_coop import cli as coopcli
from agent_coop import coopdb
from agent_coop.coop_errors import (
    ClaimCollision,
    HumanLaneViolation,
    ReceiptMissing,
    ReviewMissing,
)
from tests.test_completion import CompletionBoard
from tests.test_reviews import snap

# The columns whose semantics define the frozen contract (acceptance,
# scope, and the binding-review policy). `status` is deliberately NOT
# here — many legal transitions write it.
GUARDED_ITEM_COLUMNS = frozenset(coopdb.CONTRACT_FIELDS) | {
    "review_required", "review_waiver_reason", "contract_version"}

# The complete legal writer set. create/define/refine are the draft
# phase (creation, agent fill of EMPTY fields, pre-acceptance refine of
# agent-authored fields); revise_item is the only post-draft writer.
ALLOWED_CONTRACT_WRITERS = frozenset({
    "create_item", "define_item", "refine_item", "revise_item"})

# review_quorum is carried on append-only events, never a column;
# review_quorum() derives it from exactly these event types.
QUORUM_EVENT_TYPES = frozenset({"item_created", "item_revised"})
ALLOWED_QUORUM_EVENT_WRITERS = {
    "create_item": {"item_created"},
    "revise_item": {"item_revised"},
}

_DYNAMIC = "\x00DYNAMIC\x00"


def _module_source(module):
    return pathlib.Path(module.__file__).read_text(encoding="utf-8")


def _iter_strings(node):
    """Every string literal under `node`; f-string interpolations are
    replaced with a marker so a dynamic SET clause stays visible."""
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(
                    value.value, str):
                parts.append(value.value)
            else:
                parts.append(_DYNAMIC)
        yield "".join(parts)
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
        return
    for child in ast.iter_child_nodes(node):
        yield from _iter_strings(child)


def _update_columns(sql):
    """(columns, dynamic) for one 'UPDATE items SET …' statement."""
    body = sql.split("UPDATE items SET", 1)[1].split(" WHERE", 1)[0]
    columns = set()
    for chunk in body.split(","):
        left = chunk.split("=", 1)[0].strip()
        if left and all(c.isalnum() or c == "_" for c in left):
            columns.add(left)
    return columns, _DYNAMIC in body


def _insert_columns(sql):
    body = sql.split("INSERT INTO items(", 1)[1].split(")", 1)[0]
    return {c.strip() for c in body.split(",")}, _DYNAMIC in body


def _item_writers(module):
    """[(top_level_function, kind, columns, dynamic)] for every items
    write statement that lives inside a top-level function."""
    tree = ast.parse(_module_source(module))
    writers = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sql in _iter_strings(node):
            if "UPDATE items SET" in sql:
                columns, dynamic = _update_columns(sql)
                writers.append((node.name, "update", columns, dynamic))
            if "INSERT INTO items(" in sql:
                columns, dynamic = _insert_columns(sql)
                writers.append((node.name, "insert", columns, dynamic))
    return writers


class ContractWritePathEnumeration(unittest.TestCase):
    """Exhaustive writer enumeration over prose confidence."""

    def test_guarded_columns_have_no_writer_outside_the_four_gates(self):
        offenders = []
        guarded_writers = set()
        for name, kind, columns, dynamic in _item_writers(coopdb):
            touches_guarded = dynamic or (columns & GUARDED_ITEM_COLUMNS)
            if not touches_guarded:
                continue
            guarded_writers.add(name)
            if name not in ALLOWED_CONTRACT_WRITERS:
                offenders.append((name, kind, sorted(columns), dynamic))
        self.assertEqual(
            offenders, [],
            "a new writer of contract/review-policy columns landed outside "
            "create/define/refine/revise — route it through revise_item or "
            "add its adversarial trial first")
        # Exact enumeration: the four gates all still exist and write.
        self.assertEqual(guarded_writers, set(ALLOWED_CONTRACT_WRITERS))

    def test_every_items_write_lives_in_a_named_top_level_function(self):
        # A writer hidden at module level or inside a class would evade
        # the per-function scan above; keep the totals equal.
        source = _module_source(coopdb)
        total = source.count("UPDATE items SET") + source.count(
            "INSERT INTO items(")
        scanned = len(_item_writers(coopdb))
        self.assertEqual(
            total, scanned,
            "an items write statement exists outside a top-level coopdb "
            "function — the path enumeration cannot attribute it")

    def test_cli_layer_holds_no_direct_item_sql(self):
        source = _module_source(coopcli)
        self.assertNotIn("UPDATE items", source)
        self.assertNotIn("INSERT INTO items", source)

    def test_review_quorum_events_are_written_only_by_create_and_revise(self):
        tree = ast.parse(_module_source(coopdb))
        found = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                callee = getattr(
                    sub.func, "id", getattr(sub.func, "attr", None))
                if callee != "append_event":
                    continue
                for keyword in sub.keywords:
                    if keyword.arg != "event_type":
                        continue
                    if isinstance(keyword.value, ast.Constant) and \
                            keyword.value.value in QUORUM_EVENT_TYPES:
                        found.setdefault(node.name, set()).add(
                            keyword.value.value)
        self.assertEqual(found, ALLOWED_QUORUM_EVENT_WRITERS)

    def test_review_quorum_derivation_reads_only_those_event_types(self):
        source = inspect.getsource(coopdb.review_quorum)
        self.assertIn("'item_created','item_revised'", source)

    def test_events_table_is_written_only_through_append_event(self):
        # The quorum enumeration above relies on append_event being the
        # single events writer; a raw insert elsewhere would evade it.
        tree = ast.parse(_module_source(coopdb))
        writers = set()
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for sql in _iter_strings(node):
                if "INSERT INTO events" in sql:
                    writers.add(node.name)
        self.assertEqual(writers, {"append_event"})


class DriftBoard(CompletionBoard):
    """quorum-2 item mid-run: receipt v1 filed, one codex approval
    banked, the owner's implementation claim still live."""

    def bind_trio(self):
        bob_sid = self.make_session("bob", provider="codex")
        bob2_sid = self.make_session("bob2", provider="codex")
        carol_sid = self.make_session("carol", provider="grok")
        return bob_sid, bob2_sid, carol_sid

    def banked_one_of_two(self):
        bob_sid, _bob2_sid, carol_sid = self.bind_trio()
        item, sid, cid = self.make_working("alice", review_quorum=2)
        receipt_id = self.submit(cid, sid)
        review_id = coopdb.request_review(
            self.conn, claim_id=cid, session_id=sid, actor="alice")
        claim, _packet = self.claim_rev(review_id, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=claim["claim_id"], session_id=bob_sid,
            actor="bob", verdict="approve")
        self.assertEqual(coopdb.approvals_still_needed(self.conn, item), 1)
        return item, sid, cid, receipt_id, bob_sid, carol_sid

    def revise(self, item, reason="operator revision", fields=None):
        return coopdb.revise_item(
            self.conn, item_id=item, reason=reason, fields=fields or {})


class QuorumDowngradeAttack(DriftBoard):
    def test_downgrade_under_the_live_claim_refuses(self):
        item, sid, cid, _receipt, _bob, _carol = self.banked_one_of_two()
        before = snap(self.conn)
        with self.assertRaises(ClaimCollision):
            self.revise(item, reason="attacker weakens quorum mid-run",
                        fields={"review_quorum": 1})
        self.assertEqual(before, snap(self.conn))
        # The semantics did not move: still 2, still one short, no done.
        self.assertEqual(coopdb.review_quorum(self.conn, item), 2)
        with self.assertRaises(ReviewMissing) as caught:
            self.complete(cid, sid)
        self.assertEqual(caught.exception.evidence["required_count"], 2)
        self.assertEqual(caught.exception.evidence["observed_count"], 1)

    def test_downgrade_from_inside_a_session_refuses_human_lane(self):
        item, sid, cid, _receipt, _bob, _carol = self.banked_one_of_two()
        self.release(cid, sid)
        before = snap(self.conn)
        with unittest.mock.patch.dict(
                "os.environ", {"COOP_SESSION_ID": sid}, clear=False):
            with self.assertRaises(HumanLaneViolation):
                self.revise(item, reason="agent-lane quorum downgrade",
                            fields={"review_quorum": 1})
        self.assertEqual(before, snap(self.conn))
        # The CLI surface refuses identically.
        code, _out, err = self._run(
            ["item", "revise", str(item), "--reason", "sneaky",
             "--review-quorum", "1"],
            env={"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"})
        self.assertEqual(code, 1)
        self.assertIn("human_lane_violation", err)
        self.assertEqual(coopdb.review_quorum(self.conn, item), 2)

    def release(self, cid, sid, agent="alice"):
        coopdb.release_claim(
            self.conn, claim_id=cid, actor=agent, session_id=sid,
            reason="stepping away")

    def test_downgrade_costs_the_banked_approval_and_a_fresh_cycle(self):
        item, sid, cid, receipt_id, bob_sid, _carol = self.banked_one_of_two()
        self.release(cid, sid)
        self.revise(item, reason="operator lowers the bar",
                    fields={"review_quorum": 1})
        row = self.item_row(item)
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["contract_version"], 2)
        self.assertEqual(coopdb.review_quorum(self.conn, item), 1)
        # The downgrade is on the audit trail, not silent.
        revised = self.events(item, "item_revised")
        self.assertEqual(len(revised), 1)
        payload = json.loads(revised[0]["payload_json"])
        self.assertEqual(payload["delta"]["review_quorum"],
                         {"old": 2, "new": 1})
        # The banked evidence chain died with the revise.
        old = self.conn.execute(
            "SELECT superseded_at FROM receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
        self.assertIsNotNone(old["superseded_at"])
        self.assertIsNone(coopdb._live_review(self.conn, item))
        # A reclaim cannot complete on the dead receipt…
        cid2 = self.claim(item, "alice", sid, intent="rework",
                          reclaim_reason="resume after revision")["claim_id"]
        with self.assertRaises(ReceiptMissing):
            self.complete(cid2, sid)
        # …nor on a fresh receipt with only the dead v1 approval banked.
        self.submit(cid2, sid, path=self.evidence_file("v2.md", b"v2\n"))
        with self.assertRaises(ReviewMissing) as caught:
            self.complete(cid2, sid)
        self.assertEqual(caught.exception.evidence["required_count"], 1)
        self.assertEqual(caught.exception.evidence["observed_count"], 0)
        # Even the SAME reviewer must re-review under contract v2.
        review2 = coopdb.request_review(
            self.conn, claim_id=cid2, session_id=sid, actor="alice")
        claim2, _ = self.claim_rev(review2, bob_sid)
        coopdb.submit_verdict(
            self.conn, claim_id=claim2["claim_id"], session_id=bob_sid,
            actor="bob", verdict="approve")
        outcome = self.complete(cid2, sid)
        self.assertEqual(outcome["completed"], item)
        versions = [r["contract_version"] for r in self.conn.execute(
            "SELECT contract_version FROM reviews WHERE item_id=? AND "
            "status='approved' ORDER BY id", (item,))]
        self.assertEqual(versions, [1, 2])


class WaiverInjectionAttack(DriftBoard):
    release = QuorumDowngradeAttack.release

    def test_injection_under_the_live_claim_refuses(self):
        item, sid, cid, _receipt, _bob, _carol = self.banked_one_of_two()
        before = snap(self.conn)
        with self.assertRaises(ClaimCollision):
            self.revise(item, reason="attacker waives review mid-run",
                        fields={"review_waiver": "looks fine to me"})
        self.assertEqual(before, snap(self.conn))
        self.assertEqual(self.item_row(item)["review_required"], 1)
        with self.assertRaises(ReviewMissing):
            self.complete(cid, sid)

    def test_injection_from_inside_a_session_refuses_human_lane(self):
        item, sid, cid, _receipt, _bob, _carol = self.banked_one_of_two()
        self.release(cid, sid)
        before = snap(self.conn)
        with unittest.mock.patch.dict(
                "os.environ", {"COOP_SESSION_ID": sid}, clear=False):
            with self.assertRaises(HumanLaneViolation):
                self.revise(item, reason="agent-lane waiver injection",
                            fields={"review_waiver": "self-certified"})
        self.assertEqual(before, snap(self.conn))

    def test_injection_costs_the_banked_receipt_and_reevidencing(self):
        item, sid, cid, receipt_id, _bob, _carol = self.banked_one_of_two()
        self.release(cid, sid)
        self.revise(item, reason="operator accepts the risk",
                    fields={"review_waiver": "operator accepts the risk"})
        row = self.item_row(item)
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["contract_version"], 2)
        self.assertEqual(row["review_required"], 0)
        self.assertEqual(coopdb.review_quorum(self.conn, item), 0)
        # The waiver did NOT resurrect the banked receipt: it died with
        # the revise, so completion still demands fresh v2 evidence.
        old = self.conn.execute(
            "SELECT superseded_at FROM receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()
        self.assertIsNotNone(old["superseded_at"])
        cid2 = self.claim(item, "alice", sid, intent="rework",
                          reclaim_reason="resume after waiver")["claim_id"]
        with self.assertRaises(ReceiptMissing):
            self.complete(cid2, sid)
        fresh = self.submit(
            cid2, sid, path=self.evidence_file("v2.md", b"waived v2\n"))
        outcome = self.complete(cid2, sid)
        self.assertEqual(outcome["completed"], item)
        current = self.conn.execute(
            "SELECT contract_version FROM receipts WHERE receipt_id=?",
            (fresh,)).fetchone()
        self.assertEqual(current["contract_version"], 2)
        # The waiver injection is on the audit trail.
        payload = json.loads(
            self.events(item, "item_revised")[0]["payload_json"])
        self.assertEqual(payload["delta"]["review_waiver"]["new"],
                         "operator accepts the risk")


if __name__ == "__main__":
    unittest.main()
