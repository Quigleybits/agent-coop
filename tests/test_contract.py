"""Complete task contracts with a first-class read path.

The write path validates every required field; the read path (item show /
packet / history / queue) is what makes the board the operative channel —
contract fields written by the schema become readable here, before any
protocol machinery consumes them."""

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
from tests.test_claims import ClaimBoard, LEASE
from tests.test_schema_migration import V3Board

COOP_DIR = pathlib.Path(__file__).resolve().parents[1]

PACKET_KEYS = {
    "item_id", "contract_version", "title", "objective", "scope",
    "done_when", "output_contract", "context", "allowed_actions",
    "stop_conditions", "review_quorum", "contract_acceptance", "status",
    "owner", "next_actor", "claim", "session",
    "question", "review", "handoff", "decisions", "events",
    # The live receipt slot replaces nothing — it is the first of the
    # live evidence slots (golden set extends in place).
    "receipt",
}

REQUIRED_TEXT_FIELDS = (
    "title", "objective", "scope", "done_when", "output_contract", "context",
)
LIST_FIELDS = ("allowed_actions", "stop_conditions")

# The review slot's own golden key set — designation
# (`reviewer`) and informational claimant (`reviewer_agent_id`) are distinct
# facts, and the slot carries the review-lane claim beside the
# implementation claim (dual-claim rendering).
REVIEW_SLOT_KEYS = {
    "review_id", "status", "reviewer", "reviewer_agent_id", "requested_by",
    "receipt_id", "contract_version", "claim",
}

# Each decisions entry renders live and legacy rows through
# one key set — legacy rows keep their preserved debate_id with null claim
# data, live rows carry their claim_id; nothing fabricates the other side.
DECISION_ENTRY_KEYS = {
    "decision_id", "text", "rationale", "decided_by", "debate_id",
    "claim_id", "legacy",
}

# The handoff slot carries the full structured transfer —
# pending else latest-accepted — with the execution fencing token redacted.
HANDOFF_SLOT_KEYS = {
    "handoff_id", "from_agent", "to_agent", "status", "reason", "summary",
    "completed_work", "remaining_work", "risks", "suggested_next_action",
    "proof_references", "created_at", "resolved_at",
}


def run_coop(*args, env=None):
    import os

    merged = dict(os.environ)
    merged.pop("COOP_SESSION_ID", None)
    merged.pop("COOP_AGENT", None)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "coop.py", *(str(arg) for arg in args)],
        cwd=COOP_DIR,
        capture_output=True,
        text=True,
        env=merged,
    )


def contract_kwargs(**overrides):
    kwargs = {
        "actor": "human",
        "session_id": None,
        "title": "Wire the login flow",
        "objective": "Users can sign in",
        "scope": "auth module only",
        "done_when": "login round-trips",
        "output_contract": "a passing e2e test",
        "context": "see docs/auth.md",
        "allowed_actions": ["edit auth/", "run tests"],
        "stop_conditions": ["schema change needed"],
    }
    kwargs.update(overrides)
    return kwargs


class ContractBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db = pathlib.Path(self._dir.name) / "board.db"
        self.conn = coopdb.connect(self.db)
        self.addCleanup(self.conn.close)
        coopdb.init_db(self.conn)
        coopdb.register_agent(self.conn, "claude")
        coopdb.register_agent(self.conn, "codex")

    def seed_session(self, session_id, agent, status="running"):
        # Sessions are seeded through the real ops, not a direct INSERT.
        coopdb.insert_session(
            self.conn, session_id=session_id, agent_id=agent, provider=agent,
            command=["codex", "exec"], cwd="C:/work/repo",
            max_runtime_s=28800, grace_s=10,
        )
        if status == "exited":
            coopdb.finish_session(
                self.conn, session_id, status="exited", reason="child_exit",
                exit_code=0,
            )
        elif status != "running":
            raise AssertionError(f"unsupported fixture status {status!r}")

    def seed_active_claim(self, item_id, agent, session_id, token=7):
        def _seed(conn):
            conn.execute(
                "INSERT INTO claims(item_id,claim_kind,subject_id,lane_key,"
                "claimed_by_agent,owner_session_id,status,intent_note,"
                "fencing_token,claimed_at,last_renewed_at,lease_expires_at,"
                "last_checkpoint_at) VALUES (?,?,NULL,?,?,?,'active',?,?,?,?,?,?)",
                (item_id, "implementation", f"implementation:item:{item_id}",
                 agent, session_id, "working on it", token, coopdb.now(),
                 coopdb.now(), coopdb.now(), coopdb.now()),
            )
        coopdb.mutate(self.conn, _seed)


class TestCreateValidation(ContractBase):
    def test_goal_required_other_fields_optional_yield_draft(self):
        # Goal-tasks: only title + objective (the goal) are required. An empty
        # non-goal field now yields a claimable DRAFT, not a rejection — the
        # working agent fills it via `item define`.
        for field in ("title", "objective"):
            for bad in ("", "   "):
                with self.subTest(goal_field=field, value=repr(bad)):
                    with self.assertRaises(coop_errors.IncompleteContract):
                        coopdb.create_item(
                            self.conn, **contract_kwargs(**{field: bad}))
        for field in ("scope", "done_when", "output_contract", "context"):
            with self.subTest(draft_field=field):
                item_id = coopdb.create_item(
                    self.conn, **contract_kwargs(**{field: ""}))
                row = self.conn.execute(
                    "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                self.assertTrue(coopdb.is_draft(row),
                                f"empty {field} should yield a draft")
        for field in LIST_FIELDS:
            with self.subTest(draft_field=field):
                item_id = coopdb.create_item(
                    self.conn, **contract_kwargs(**{field: []}))
                row = self.conn.execute(
                    "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                self.assertTrue(coopdb.is_draft(row))

    def test_list_fields_stored_as_deterministic_json(self):
        item_id = coopdb.create_item(self.conn, **contract_kwargs())
        row = self.conn.execute(
            "SELECT allowed_actions, stop_conditions FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        self.assertEqual(
            row["allowed_actions"], '["edit auth/","run tests"]'
        )
        self.assertEqual(row["stop_conditions"], '["schema change needed"]')

    def test_human_actor_seeds_outside_any_session(self):
        item_id = coopdb.create_item(self.conn, **contract_kwargs())
        row = self.conn.execute(
            "SELECT created_by, status, contract_version FROM items WHERE id=?",
            (item_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("human", "todo", 1))
        event = self.conn.execute(
            "SELECT event_type, actor_agent_id, actor_session_id FROM events"
        ).fetchone()
        self.assertEqual(event["event_type"], "item_created")
        self.assertEqual(event["actor_agent_id"], "human")
        self.assertIsNone(event["actor_session_id"])

    def test_agent_actor_requires_a_live_matching_session(self):
        with self.assertRaises(coop_errors.SessionMismatch):
            coopdb.create_item(
                self.conn, **contract_kwargs(actor="codex", session_id=None)
            )
        with self.assertRaises(coop_errors.SessionMismatch):
            coopdb.create_item(
                self.conn,
                **contract_kwargs(actor="codex", session_id="no-such-session"),
            )
        self.seed_session("sess-dead", "codex", status="exited")
        with self.assertRaises(coop_errors.SessionMismatch):
            coopdb.create_item(
                self.conn,
                **contract_kwargs(actor="codex", session_id="sess-dead"),
            )
        self.seed_session("sess-live", "codex")
        with self.assertRaises(coop_errors.SessionMismatch):
            coopdb.create_item(
                self.conn,
                **contract_kwargs(actor="claude", session_id="sess-live"),
            )
        item_id = coopdb.create_item(
            self.conn, **contract_kwargs(actor="codex", session_id="sess-live")
        )
        created_by = self.conn.execute(
            "SELECT created_by FROM items WHERE id=?", (item_id,)
        ).fetchone()[0]
        self.assertEqual(created_by, "codex")

    def test_seeded_owner_and_next_actor_get_assignment_deliveries(self):
        coopdb.create_item(
            self.conn, **contract_kwargs(owner="claude", next_actor="codex")
        )
        entries = self.conn.execute(
            "SELECT recipient_agent_id, category FROM inbox_entries "
            "ORDER BY inbox_entry_id"
        ).fetchall()
        self.assertEqual(
            [(row[0], row[1]) for row in entries],
            [("claude", "assignment"), ("codex", "assignment")],
        )

    def test_owner_or_next_actor_may_not_be_human(self):
        with self.assertRaises(coop_errors.HumanLaneViolation):
            coopdb.create_item(self.conn, **contract_kwargs(owner="human"))
        with self.assertRaises(coop_errors.HumanLaneViolation):
            coopdb.create_item(
                self.conn, **contract_kwargs(next_actor="human")
            )


class TestPacketAndHistory(ContractBase):
    def test_item_show_packet_key_set_is_exact_and_tokenless(self):
        item_id = coopdb.create_item(
            self.conn, **contract_kwargs(owner="codex")
        )
        packet = coopdb.item_show(self.conn, item_id, packet=True)
        self.assertEqual(set(packet), PACKET_KEYS)
        self.assertEqual(packet["owner"], {"agent_id": "codex", "provider": None})
        self.assertIsNone(packet["claim"])
        self.assertIsNone(packet["session"])
        self.assertIsNone(packet["question"])
        self.assertIsNone(packet["review"])
        self.assertIsNone(packet["handoff"])
        self.assertEqual(packet["allowed_actions"], ["edit auth/", "run tests"])
        self.assertNotIn("fencing_token", json.dumps(packet))

        self.seed_session("sess-live", "codex")
        self.seed_active_claim(item_id, "codex", "sess-live", token=7)
        packet = coopdb.item_show(self.conn, item_id, packet=True)
        self.assertEqual(
            packet["claim"],
            {
                "claim_id": 1,
                "agent": "codex",
                "provider": "codex",
                "session_id": "sess-liv",  # display prefix, never the full id
                "intent": "working on it",
                "lease_expires_at": packet["claim"]["lease_expires_at"],
            },
        )
        self.assertEqual(
            packet["session"],
            {"provider": "codex", "status": "running"},
        )
        self.assertNotIn("fencing_token", json.dumps(packet))
        self.assertEqual(len(packet["events"]), 1)  # item_created

    def test_packet_bounds_events_to_last_ten(self):
        item_id = coopdb.create_item(self.conn, **contract_kwargs())

        def _spam(conn):
            for index in range(15):
                coopdb.append_event(
                    conn, item_id=item_id, event_type=f"probe_{index}",
                    actor_agent_id=None, actor_session_id=None, payload={},
                )
        coopdb.mutate(self.conn, _spam)
        packet = coopdb.item_show(self.conn, item_id, packet=True)
        self.assertEqual(len(packet["events"]), 10)
        self.assertEqual(packet["events"][-1]["event_type"], "probe_14")

    def test_item_show_history_redacts_every_token(self):
        item_id = coopdb.create_item(self.conn, **contract_kwargs())
        self.seed_session("sess-live", "codex")
        self.seed_active_claim(item_id, "codex", "sess-live", token=42)

        def _seed_handoff(conn):
            conn.execute(
                "INSERT INTO handoffs(item_id,claim_id,from_agent,from_session,"
                "execution_fencing_token,to_agent,reason,summary,completed_work,"
                "remaining_work,risks,proof_references,suggested_next_action,"
                "status,created_at) VALUES (?,1,'codex','sess-live',42,'claude',"
                "'r','s','c','rw','ri','[]','next','pending',?)",
                (item_id, coopdb.now()),
            )
        coopdb.mutate(self.conn, _seed_handoff)

        history = coopdb.item_show(self.conn, item_id, history=True)
        rendered = json.dumps(history)
        self.assertNotIn("fencing_token", rendered)
        self.assertNotIn("execution_fencing_token", rendered)
        self.assertEqual(len(history["claims"]), 1)
        self.assertEqual(len(history["handoffs"]), 1)

    def test_packet_handoff_slot_full_transfer_pending_else_accepted(self):
        # The slot renders the pending transfer in full,
        # falls back to the latest accepted one, and stays None when the
        # only row is declined (the migrated-V3 golden case).
        item_id = coopdb.create_item(self.conn, **contract_kwargs())

        def _seed(conn):
            conn.execute(
                "INSERT INTO handoffs(item_id,claim_id,from_agent,"
                "from_session,execution_fencing_token,to_agent,reason,"
                "summary,completed_work,remaining_work,risks,"
                "proof_references,suggested_next_action,status,created_at) "
                "VALUES (?,1,'codex','sess-live',42,'claude','r','s','c',"
                "'rw','ri','[]','next','pending',?)",
                (item_id, coopdb.now()))
        self.seed_session("sess-live", "codex")
        self.seed_active_claim(item_id, "codex", "sess-live", token=42)
        coopdb.mutate(self.conn, _seed)

        packet = coopdb.item_show(self.conn, item_id, packet=True)
        slot = packet["handoff"]
        self.assertEqual(set(slot), HANDOFF_SLOT_KEYS)
        self.assertEqual(slot["status"], "pending")
        self.assertEqual(slot["proof_references"], [])
        self.assertNotIn("fencing_token", json.dumps(packet))

        def _resolve(conn):
            conn.execute(
                "UPDATE handoffs SET status='accepted', resolved_at=? "
                "WHERE item_id=?", (coopdb.now(), item_id))
        coopdb.mutate(self.conn, _resolve)
        packet = coopdb.item_show(self.conn, item_id, packet=True)
        self.assertEqual(packet["handoff"]["status"], "accepted")
        self.assertEqual(set(packet["handoff"]), HANDOFF_SLOT_KEYS)

        def _decline(conn):
            conn.execute(
                "UPDATE handoffs SET status='declined' WHERE item_id=?",
                (item_id,))
        coopdb.mutate(self.conn, _decline)
        packet = coopdb.item_show(self.conn, item_id, packet=True)
        self.assertIsNone(packet["handoff"])

    def test_item_show_missing_item_is_not_found(self):
        with self.assertRaises(coop_errors.NotFound) as caught:
            coopdb.item_show(self.conn, 999)
        self.assertEqual(caught.exception.reason_code, "target_not_found")
        self.assertEqual(caught.exception.evidence["item_id"], 999)

    def test_contract_incomplete_is_derived_never_stored(self):
        from tests.test_coop import seed_item

        incomplete = seed_item(self.conn, "legacy-ish row")
        shown = coopdb.item_show(self.conn, incomplete)
        self.assertTrue(shown["contract_incomplete"])
        complete = coopdb.create_item(self.conn, **contract_kwargs())
        self.assertFalse(
            coopdb.item_show(self.conn, complete)["contract_incomplete"]
        )
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(items)")
        }
        self.assertNotIn("contract_incomplete", columns)


class TestQueue(ContractBase):
    def test_queue_orders_by_created_at_then_id(self):
        clock_values = [
            datetime.datetime(2026, 1, 1, 10, 0, 0, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 1, 1, 9, 0, 0, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 1, 1, 10, 0, 0, tzinfo=datetime.timezone.utc),
        ]
        for stamp, title in zip(clock_values, ("late", "early", "late-tie")):
            with mock.patch.object(coopdb, "_clock", lambda s=stamp: s):
                coopdb.create_item(self.conn, **contract_kwargs(title=title))
        rows = coopdb.queue(self.conn)
        self.assertEqual(
            [row["title"] for row in rows], ["early", "late", "late-tie"]
        )

    def test_queue_excludes_incomplete_and_non_todo(self):
        from tests.test_coop import seed_item

        seed_item(self.conn, "incomplete legacy row")            # todo, no contract
        seed_item(self.conn, "already working", owner="codex")   # not todo
        complete = coopdb.create_item(self.conn, **contract_kwargs())
        rows = coopdb.queue(self.conn)
        self.assertEqual([row["item_id"] for row in rows], [complete])

    def test_queue_for_agent_filters_addressed_and_unassigned(self):
        unassigned = coopdb.create_item(self.conn, **contract_kwargs())
        for_codex = coopdb.create_item(
            self.conn, **contract_kwargs(next_actor="codex")
        )
        coopdb.create_item(self.conn, **contract_kwargs(owner="claude"))
        rows = coopdb.queue(self.conn, for_agent="codex")
        self.assertEqual(
            {row["item_id"] for row in rows}, {unassigned, for_codex}
        )


class TestMigratedBoardReads(unittest.TestCase):
    def test_v3_migrated_item_reads_through_the_new_path(self):
        with V3Board() as board:
            coopdb.migrate_db(board.path, confirm_legacy_clients_stopped=True)
            with contextlib.closing(coopdb.connect(board.path)) as conn:
                shown = coopdb.item_show(conn, 1)
                self.assertTrue(
                    shown["contract_incomplete"],
                    "a migrated v3 item lacks output_contract and must "
                    "derive contract_incomplete",
                )
                packet = coopdb.item_show(conn, 1, packet=True)
                self.assertEqual(set(packet), PACKET_KEYS)
                self.assertEqual(packet["question"]["exact_question"], "What next?")
                self.assertEqual(packet["review"]["status"], "requested")
                # The migrated legacy review renders through
                # the full slot key set with no fabricated claim state.
                self.assertEqual(set(packet["review"]), REVIEW_SLOT_KEYS)
                self.assertIsNone(packet["review"]["claim"])
                self.assertIsNone(packet["handoff"])  # declined, not pending
                self.assertEqual(coopdb.queue(conn), [])
                self.assertNotIn("fencing_token", json.dumps(packet))


class TestContractCLI(ContractBase):
    def _create_args(self, **extra):
        args = [
            "--db", self.db, "item", "create",
            "--title", "Wire the login flow",
            "--objective", "Users can sign in",
            "--scope", "auth module only",
            "--done-when", "login round-trips",
            "--output-contract", "a passing e2e test",
            "--context", "see docs/auth.md",
            "--allowed-action", "edit auth/",
            "--allowed-action", "run tests",
            "--stop-condition", "schema change needed",
        ]
        return args

    def test_cli_create_with_flags_then_show_json(self):
        result = run_coop(*self._create_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        item_id = int(result.stdout.strip())
        shown = run_coop("--db", self.db, "--json", "item", "show", item_id)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        payload = json.loads(shown.stdout)
        self.assertEqual(payload["title"], "Wire the login flow")
        self.assertEqual(payload["created_by"], "human")
        self.assertEqual(
            payload["allowed_actions"], ["edit auth/", "run tests"]
        )

    def test_cli_create_records_explicit_review_quorum(self):
        result = run_coop(*self._create_args(), "--review-quorum", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        item_id = int(result.stdout.strip())
        shown = json.loads(run_coop(
            "--db", self.db, "--json", "item", "show", item_id).stdout)
        self.assertEqual(shown["review_quorum"], 2)

    def test_cli_contract_json_merge_with_flag_override(self):
        contract = {
            "title": "From the file",
            "objective": "Users can sign in",
            "scope": "auth module only",
            "done_when": "login round-trips",
            "output_contract": "a passing e2e test",
            "context": "see docs/auth.md",
            "allowed_actions": ["edit auth/"],
            "stop_conditions": ["schema change needed"],
        }
        path = pathlib.Path(self._dir.name) / "contract.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        result = run_coop(
            "--db", self.db, "item", "create",
            "--contract", path, "--title", "Overridden title",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        item_id = int(result.stdout.strip())
        shown = json.loads(
            run_coop("--db", self.db, "--json", "item", "show", item_id).stdout
        )
        self.assertEqual(shown["title"], "Overridden title")
        self.assertEqual(shown["scope"], "auth module only")

    def test_cli_contract_json_unknown_key_rejected(self):
        path = pathlib.Path(self._dir.name) / "typo.json"
        path.write_text(
            json.dumps({"objectve": "typo", "title": "x"}), encoding="utf-8"
        )
        result = run_coop(
            "--db", self.db, "item", "create", "--contract", path
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: incomplete_contract:", result.stderr)
        self.assertIn("objectve", result.stderr)

    def test_cli_incomplete_goal_rejected_with_field_name(self):
        # Only the goal (title/objective) is required now; an empty objective
        # is still rejected with its name (an empty scope would be a draft).
        args = self._create_args()
        obj_index = args.index("--objective")
        args[obj_index + 1] = "   "
        result = run_coop(*args)
        self.assertEqual(result.returncode, 1)
        self.assertIn("error: incomplete_contract:", result.stderr)
        self.assertIn("objective", result.stderr)

    def test_cli_queue_json(self):
        item_id = coopdb.create_item(self.conn, **contract_kwargs())
        result = run_coop("--db", self.db, "--json", "queue")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual([row["item_id"] for row in rows], [item_id])


class TestQueueDiscriminated(ClaimBoard):
    """The discriminated queue row contract — a kind per row,
    the (created_at, entity_id, kind) total order with kind only breaking
    a full timestamp-and-ID collision, and per-kind visibility filters."""

    TASK_KEYS = {"kind", "item_id", "title", "objective", "created_at",
                 "owner", "next_actor"}
    REVIEW_KEYS = {"kind", "review_id", "item_id", "title",
                   "designated_reviewer", "requested_at"}
    HANDOFF_KEYS = {"kind", "handoff_id", "item_id", "title", "from_agent",
                    "created_at"}

    def setUp(self):
        super().setUp()
        self.make_session("alice", sid="s-alice")
        coopdb.register_or_bind_agent(
            self.conn, agent_id="bob", provider="claude")
        coopdb.register_or_bind_agent(
            self.conn, agent_id="carol", provider="claude")

    def _receipt(self, claim):
        path = pathlib.Path(self.tmp.name) / f"r-{claim['claim_id']}.txt"
        path.write_text("evidence", encoding="utf-8")
        return coopdb.submit_receipt(
            self.conn, claim_id=claim["claim_id"], session_id="s-alice",
            actor="alice", path=str(path), summary="done", proof="file",
            proof_refs=[])

    def _live_review(self, reviewer=None, title="rev-item"):
        item = coopdb.create_item(
            self.conn, **contract_kwargs(title=title, owner="alice"))
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="alice", session_id="s-alice",
            intent="work")
        self._receipt(claim)
        rid = coopdb.request_review(
            self.conn, claim_id=claim["claim_id"], session_id="s-alice",
            actor="alice", reviewer=reviewer)
        return item, rid, claim

    def _pending_handoff(self, to="bob", title="hand-item"):
        item = coopdb.create_item(
            self.conn, **contract_kwargs(title=title, owner="alice"))
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="alice", session_id="s-alice",
            intent="work")
        ev = self.conn.execute(
            "SELECT event_id FROM events WHERE item_id=? ORDER BY event_id "
            "LIMIT 1", (item,)).fetchone()["event_id"]
        result = coopdb.create_handoff(
            self.conn, claim_id=claim["claim_id"], session_id="s-alice",
            actor="alice", to_agent=to, reason="rotate", summary="s",
            completed="c", remaining="r", risks="none",
            next_action="continue", proof_refs=[f"event:{ev}"])
        return item, result["handoff_id"]

    def _kinds(self, agent="bob"):
        return [(r["kind"],
                 r.get("review_id") or r.get("handoff_id") or r["item_id"])
                for r in coopdb.queue(self.conn, for_agent=agent)]

    def test_task_rows_keep_phase2_shape_plus_kind(self):
        coopdb.create_item(self.conn, **contract_kwargs(title="plain"))
        rows = coopdb.queue(self.conn)
        self.assertEqual(set(rows[0]), self.TASK_KEYS)
        self.assertEqual(rows[0]["kind"], "task")

    def test_reviews_and_handoffs_require_for_agent(self):
        self._live_review(title="unnamed")
        self._pending_handoff(to="bob")
        kinds = {r["kind"] for r in coopdb.queue(self.conn)}
        self.assertEqual(kinds - {"task"}, set())

    def test_review_rows_shape_designation_and_owner_filter(self):
        item, rid, _ = self._live_review(title="unnamed")
        rows = [r for r in coopdb.queue(self.conn, for_agent="bob")
                if r["kind"] == "review"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), self.REVIEW_KEYS)
        self.assertEqual(rows[0]["review_id"], rid)
        self.assertIsNone(rows[0]["designated_reviewer"])
        self.assertEqual(
            [r for r in coopdb.queue(self.conn, for_agent="alice")
             if r["kind"] == "review"], [])
        named_item, named_rid, _ = self._live_review(
            reviewer="carol", title="named")
        bob_rows = [r["review_id"] for r in
                    coopdb.queue(self.conn, for_agent="bob")
                    if r["kind"] == "review"]
        carol_rows = [r["review_id"] for r in
                      coopdb.queue(self.conn, for_agent="carol")
                      if r["kind"] == "review"]
        self.assertNotIn(named_rid, bob_rows)
        self.assertIn(named_rid, carol_rows)

    def test_review_lane_states_gate_visibility(self):
        item, rid, _ = self._live_review(title="lifecycle")
        sid_bob = self.make_session("bob", sid="s-bob")
        coopdb.claim_review(
            self.conn, review_id=rid, session_id="s-bob", intent="review")
        self.assertEqual(  # active lane: nobody discovers it
            [r for r in coopdb.queue(self.conn, for_agent="carol")
             if r["kind"] == "review"], [])
        self.clock.advance(LEASE + 1)
        coopdb.sweep_expired(self.conn)
        self.assertEqual(  # unsafe stale: still hidden
            [r for r in coopdb.queue(self.conn, for_agent="carol")
             if r["kind"] == "review"], [])
        coopdb.finish_session(
            self.conn, "s-bob", status="exited", reason="child_exit",
            exit_code=0)
        self.assertEqual(  # safely stale: discoverable again
            [r["review_id"] for r in
             coopdb.queue(self.conn, for_agent="carol")
             if r["kind"] == "review"], [rid])

    def test_handoff_rows_pending_and_addressed_only(self):
        item, hid = self._pending_handoff(to="bob", title="mine")
        rows = [r for r in coopdb.queue(self.conn, for_agent="bob")
                if r["kind"] == "handoff"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), self.HANDOFF_KEYS)
        self.assertEqual(rows[0]["handoff_id"], hid)
        self.assertEqual(rows[0]["from_agent"], "alice")
        self.assertEqual(
            [r for r in coopdb.queue(self.conn, for_agent="carol")
             if r["kind"] == "handoff"], [])
        sid_bob = self.make_session("bob", sid="s-bob-h")
        coopdb.decline_handoff(
            self.conn, handoff_id=hid, session_id=sid_bob, actor="bob",
            reason="declining")
        self.assertEqual(
            [r for r in coopdb.queue(self.conn, for_agent="bob")
             if r["kind"] == "handoff"], [])

    def test_total_order_puts_entity_id_before_kind(self):
        # Frozen clock: identical created_at everywhere. review id 1 and
        # handoff id 1 collide on (time, id) → kind breaks the tie; the
        # todo task (item id 3) sorts LAST on its higher entity id — a
        # category priority would wrongly put it first.
        self._live_review(title="review-first")          # items 1, review 1
        self._pending_handoff(to="bob", title="hand")    # item 2, handoff 1
        coopdb.create_item(
            self.conn, **contract_kwargs(title="task-last"))  # item 3, todo
        self.assertEqual(
            self._kinds("bob"),
            [("review", 1), ("handoff", 1), ("task", 3)])

    def test_kind_breaks_only_the_full_collision(self):
        coopdb.create_item(
            self.conn, **contract_kwargs(title="task-one"))   # item 1, todo
        self._live_review(title="review-one")            # item 2, review 1
        self._pending_handoff(to="bob", title="hand")    # item 3, handoff 1
        self.assertEqual(
            self._kinds("bob"),
            [("task", 1), ("review", 1), ("handoff", 1)])


if __name__ == "__main__":
    unittest.main()
