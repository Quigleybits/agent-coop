"""Session-protocol smokes — the loop closes end to end.

Real stack, no fakes: real supervisors owning real process trees over a
real board, three provider labels with two sessions concurrent behind a
barrier, the full two-session protocol dance through SQLite alone, the
authorization-boundary matrix over the mutation surface, and the
guide-parity walk. No real model CLIs — harmless Python harnesses only.
"""

import io
import contextlib
import json
import pathlib
import re
import shlex
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock
from tempfile import TemporaryDirectory

from agent_coop import cli as coopcli
from agent_coop import coop_supervisor
from agent_coop import coopdb
from agent_coop.coop_errors import StaleClaim
from tests.test_claims import contract_kwargs

COOP_DIR = pathlib.Path(__file__).resolve().parents[1]

ENV_KEYS = ("COOP_AGENT", "COOP_AGENT_ID", "COOP_PROVIDER",
            "COOP_SESSION_ID", "COOP_DB", "COOP_DB_PATH")

EVIDENCE_CODE = (
    "import json,os,sys,time;"
    "keys=['COOP_AGENT','COOP_AGENT_ID','COOP_PROVIDER','COOP_SESSION_ID',"
    "'COOP_DB','COOP_DB_PATH'];"
    "open(sys.argv[1],'w').write(json.dumps({'argv':sys.argv[1:],"
    "'cwd':os.getcwd(),'env':{k:os.environ.get(k) for k in keys}}));"
    "time.sleep(6)"
)

SLEEPER_CODE = "import time; time.sleep(60)"


def wait_until(predicate, timeout_s=120.0, interval_s=0.1):
    """Poll until true. The default budget covers spawning real child
    interpreters on a loaded machine, where process creation competes
    with on-access virus scanning; a passing predicate still returns
    immediately, so a generous ceiling costs a fast run nothing."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def fresh_board(path):
    conn = coopdb.connect(path)
    coopdb.init_db(conn)
    conn.close()


class SmokeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.db = str(self.root / "board.db")
        fresh_board(self.db)

    def query(self, sql, params=()):
        conn = coopdb.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def evidence_argv(self, out_file, *extra):
        return [sys.executable, "-c", EVIDENCE_CODE, str(out_file), *extra]


class ThreeProviderSmoke(SmokeBase):
    def check_evidence(self, path, *, agent, provider, extra):
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        self.assertEqual(data["argv"], [str(path), *extra])  # exact order
        env = data["env"]
        for key in ENV_KEYS:
            self.assertTrue(env[key], f"{key} must never be empty")
        self.assertEqual(env["COOP_AGENT"], agent)
        self.assertEqual(env["COOP_AGENT"], env["COOP_AGENT_ID"])
        self.assertEqual(env["COOP_PROVIDER"], provider)
        self.assertEqual(env["COOP_DB"], env["COOP_DB_PATH"])
        self.assertEqual(env["COOP_DB"], self.db)
        return data, env

    def check_session(self, agent, provider):
        rows = self.query(
            "SELECT * FROM sessions WHERE agent_id=?", (agent,))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider"], provider)
        self.assertEqual(row["status"], "exited")
        self.assertEqual(row["termination_reason"], "child_exit")
        self.assertEqual(row["exit_code"], 0)
        self.assertGreater(row["last_seen_at"], row["started_at"])  # liveness
        return row

    def test_claude_runs_through_the_cli_end_to_end(self):
        evidence = self.root / "claude.json"
        result = subprocess.run(
            [sys.executable, "coop.py", "--db", self.db, "session", "run",
             "--as", "claude", "--shutdown-grace-seconds", "2", "--",
             *self.evidence_argv(evidence, "alpha", "beta")],
            cwd=COOP_DIR, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)  # mirrored
        data, env = self.check_evidence(
            evidence, agent="claude", provider="claude",
            extra=["alpha", "beta"])
        self.assertEqual(pathlib.Path(data["cwd"]).resolve(), COOP_DIR)
        self.check_session("claude", "claude")
        # Provider visibility through status JSON.
        status = subprocess.run(
            [sys.executable, "coop.py", "--db", self.db, "--as", "claude",
             "--json", "status"],
            cwd=COOP_DIR, capture_output=True, text=True, timeout=60)
        self.assertEqual(status.returncode, 0, status.stderr)
        panel = json.loads(status.stdout)
        self.assertEqual(panel["agent"]["provider"], "claude")

    def test_codex_and_grok_run_concurrently_behind_a_barrier(self):
        barrier = threading.Barrier(2)
        errors = []

        def run(provider):
            evidence = self.root / f"{provider}.json"
            sup = coop_supervisor.Supervisor(
                self.db, provider=provider,
                argv=self.evidence_argv(evidence, provider),
                cwd=self.tmp.name,
                timings=coop_supervisor.Timings(
                    shutdown_grace_s=2, max_runtime_s=120))
            try:
                barrier.wait(timeout=30)
                code = sup.run()
                if code != 0:
                    errors.append(f"{provider} exited {code}")
            except Exception as exc:  # noqa: BLE001 - smoke surface
                errors.append(f"{provider}: {exc!r}")

        threads = [threading.Thread(target=run, args=(p,))
                   for p in ("codex", "grok")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
            self.assertFalse(t.is_alive(), "supervisor thread hung")
        self.assertEqual(errors, [])
        for provider in ("codex", "grok"):
            self.check_evidence(self.root / f"{provider}.json",
                                agent=provider, provider=provider,
                                extra=[provider])
            self.check_session(provider, provider)
        # Concurrency: the two supervised windows overlapped.
        rows = {r["agent_id"]: r for r in self.query(
            "SELECT agent_id, started_at, exited_at FROM sessions")}
        latest_start = max(rows["codex"]["started_at"],
                          rows["grok"]["started_at"])
        earliest_exit = min(rows["codex"]["exited_at"],
                            rows["grok"]["exited_at"])
        self.assertLess(latest_start, earliest_exit)


class TwoSessionIntegrationSmoke(SmokeBase):
    def test_the_full_cycle_through_sqlite_alone(self):
        sups = {}
        threads = {}
        for agent, provider in (("alpha", "claude"), ("beta", "codex")):
            sups[agent] = coop_supervisor.Supervisor(
                self.db, provider=provider, agent_id=agent,
                argv=[sys.executable, "-c", SLEEPER_CODE],
                cwd=self.tmp.name,
                timings=coop_supervisor.Timings(
                    shutdown_grace_s=2, max_runtime_s=300))
            threads[agent] = threading.Thread(target=sups[agent].run)
            threads[agent].start()
        try:
            self.assertTrue(wait_until(lambda: bool(
                self.query("SELECT 1 FROM sessions WHERE status='running'")
                and len(self.query(
                    "SELECT 1 FROM sessions WHERE status='running'")) == 2)))
            sid_a = sups["alpha"].session_id
            sid_b = sups["beta"].session_id

            conn = coopdb.connect(self.db)
            try:
                # Human seeds the complete contract; assignment delivered.
                item = coopdb.create_item(
                    conn, actor="human", session_id=None,
                    **contract_kwargs(title="integration-item",
                                      owner="alpha"))
                # A claims, checkpoints (packet + consumed inbox).
                claim_a = coopdb.claim_item(
                    conn, item_id=item, actor="alpha", session_id=sid_a,
                    intent="drive the integration smoke")
                result = coopdb.checkpoint(
                    conn, ctype="start", claim_id=claim_a["claim_id"],
                    actor="alpha", session_id=sid_a)
                self.assertEqual(result["packet"]["item_id"], item)
                self.assertIn("assignment",
                              [e["category"] for e in result["inbox"]])
                # A blocks on B's exact answer.
                qid = coopdb.needs_input(
                    conn, claim_id=claim_a["claim_id"], session_id=sid_a,
                    to_agent="beta", question="which region?")
                response = coopdb.claim_question(
                    conn, question_id=qid, session_id=sid_b,
                    intent="answering the smoke question")
                coopdb.answer_question(
                    conn, claim_id=response["claim_id"], session_id=sid_b,
                    answer="eu-west-2")
                grace = conn.execute(
                    "SELECT resume_grace_expires_at FROM items WHERE id=?",
                    (item,)).fetchone()[0]
                self.assertIsNotNone(grace)  # the window opened
                # A resumes under a fresh claim; the old id is dead.
                claim_a2 = coopdb.claim_item(
                    conn, item_id=item, actor="alpha", session_id=sid_a,
                    intent="resuming", reclaim_reason="answer arrived")
                self.assertNotEqual(claim_a2["claim_id"],
                                    claim_a["claim_id"])
                with self.assertRaises(StaleClaim):
                    coopdb.checkpoint(
                        conn, ctype="step", claim_id=claim_a["claim_id"],
                        actor="alpha", session_id=sid_a)
                # Packet carries provider + launch command visibility.
                packet = coopdb.item_show(conn, item, packet=True)
                self.assertEqual(packet["claim"]["agent"], "alpha")
                self.assertEqual(packet["claim"]["provider"], "claude")
                self.assertIn("session", packet)
                self.assertNotIn("fencing_token", repr(packet))
            finally:
                conn.close()

            # The projection reflects the run (written by the supervisor).
            inbox_file = pathlib.Path(self.tmp.name) / "inbox" / "alpha.md"

            def projected():
                return (inbox_file.exists() and "integration-item" in
                        inbox_file.read_text(encoding="utf-8"))

            self.assertTrue(wait_until(projected, timeout_s=15))
            content = inbox_file.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("# Inbox — alpha"))
            self.assertIn("integration-item", content)

            # No side channel: no prompt files anywhere, no contract text
            # in any launch command, machine-written projections only.
            files = [p for p in pathlib.Path(self.tmp.name).rglob("*")
                     if p.is_file()]
            self.assertFalse(
                [p for p in files if "prompt" in p.name.lower()])
            for row in self.query("SELECT command_json FROM sessions"):
                self.assertNotIn("objective", row["command_json"])
            for md in pathlib.Path(self.tmp.name).rglob("*.md"):
                self.assertTrue(md.read_text(
                    encoding="utf-8").startswith("# Inbox"))
        finally:
            for sup in sups.values():
                sup.cancel()
            for t in threads.values():
                t.join(timeout=120)
        for agent in ("alpha", "beta"):
            row = self.query(
                "SELECT status, termination_reason FROM sessions WHERE "
                "agent_id=?", (agent,))[0]
            self.assertEqual((row["status"], row["termination_reason"]),
                             ("cancelled", "cancelled"))


class AuthorizationBoundaryMatrix(SmokeBase):
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

    def setUp(self):
        super().setUp()
        conn = coopdb.connect(self.db)
        try:
            self.item = coopdb.create_item(
                conn, actor="human", session_id=None, **contract_kwargs())
            coopdb.insert_session(
                conn, session_id="s-live", agent_id="worker",
                provider="codex", command=["x"], cwd=".",
                max_runtime_s=600, grace_s=5)
            self.claim = coopdb.claim_item(
                conn, item_id=self.item, actor="worker",
                session_id="s-live", intent="matrix target")
            self.q_item = coopdb.create_item(
                conn, actor="human", session_id=None,
                **contract_kwargs(title="q-item"))
            q_claim = coopdb.claim_item(
                conn, item_id=self.q_item, actor="worker",
                session_id="s-live", intent="asker")
            self.qid = coopdb.needs_input(
                conn, claim_id=q_claim["claim_id"], session_id="s-live",
                to_agent="worker", question="matrix?")
            # A quiescent item — no claims on any lane — for the revision
            # cells (revision refuses live claims).
            self.revisable = coopdb.create_item(
                conn, actor="human", session_id=None,
                **contract_kwargs(title="revisable"))
            # The published-table fixtures.
            # A wrong-kind claim: worker claims its own addressed question.
            self.qr_claim = coopdb.claim_question(
                conn, question_id=self.qid, session_id="s-live",
                intent="wrong-kind fixture")
            # Two more agents for the wrong-agent column.
            for sid, agent in (("s-owner2", "owner2"), ("s-third", "third")):
                coopdb.insert_session(
                    conn, session_id=sid, agent_id=agent,
                    provider="codex", command=["x"], cwd=".",
                    max_runtime_s=600, grace_s=5)
            # A live review NAMED to worker, owned by owner2: the owner's
            # claim attempt is self_review, a third agent's is
            # addressed_target_mismatch.
            item2 = coopdb.create_item(
                conn, actor="human", session_id=None,
                **contract_kwargs(title="named-review", owner="owner2"))
            claim2 = coopdb.claim_item(
                conn, item_id=item2, actor="owner2",
                session_id="s-owner2", intent="named-review fixture")
            coopdb.submit_receipt(
                conn, claim_id=claim2["claim_id"], session_id="s-owner2",
                actor="owner2", path=str(pathlib.Path(__file__)),
                summary="fixture", proof="fixture", proof_refs=None)
            self.named_review = coopdb.request_review(
                conn, claim_id=claim2["claim_id"], session_id="s-owner2",
                actor="owner2", reviewer="worker")
            # A pending handoff addressed to worker: non-target responses
            # refuse addressed_target_mismatch.
            item3 = coopdb.create_item(
                conn, actor="human", session_id=None,
                **contract_kwargs(title="pending-handoff", owner="owner2"))
            claim3 = coopdb.claim_item(
                conn, item_id=item3, actor="owner2",
                session_id="s-owner2", intent="handoff fixture")
            self.pending_handoff = coopdb.create_handoff(
                conn, claim_id=claim3["claim_id"], session_id="s-owner2",
                actor="owner2", to_agent="worker", reason="f",
                summary="f", completed="f", remaining="f", risks="f",
                next_action="f",
                proof_refs=[f"file:{pathlib.Path(__file__)}"])["handoff_id"]
        finally:
            conn.close()

    NO_SESSION = {"COOP_SESSION_ID": "", "COOP_AGENT": ""}
    LIVE = {"COOP_SESSION_ID": "s-live", "COOP_AGENT": "worker"}

    def test_every_boundary_refuses_the_wrong_caller(self):
        cid = str(self.claim["claim_id"])
        rows = [
            # session-bound agent mutations refused without a session
            (["item", "claim", str(self.item), "--intent", "i"],
             self.NO_SESSION, "human_lane_violation",
             "human_lane_forbidden"),
            (["claim", "release", "--claim", cid, "--reason", "r"],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["checkpoint", "step", "--claim", cid],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["needs-input", "--claim", cid, "--to", "worker",
              "--question", "q"], self.NO_SESSION, "session_mismatch",
             "session_unavailable"),
            (["question", "claim", str(self.qid), "--intent", "i"],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["question", "answer", "--claim", cid, "--answer", "a"],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            # Receipt submission is claim-bound — refused
            # without a session and under an unknown session (real file, so
            # only the claim boundary can fire).
            (["receipt", "submit", "--claim", cid, "--path",
              str(pathlib.Path(__file__)), "--summary", "s", "--proof", "p"],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["receipt", "submit", "--claim", cid, "--path",
             str(pathlib.Path(__file__)), "--summary", "s", "--proof", "p"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            # Review request/claim are session-bound — the
            # request through the claim chokepoint, the claim through the
            # session-derived actor.
            (["review", "request", "--claim", cid],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["review", "request", "--claim", cid],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            (["review", "claim", "1", "--intent", "i"],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["review", "claim", "1", "--intent", "i"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            # Decisions are claim-bound — refused without a
            # session and under an unknown session at the same chokepoint.
            (["decision", "record", "--claim", cid, "--text", "t"],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["decision", "record", "--claim", cid, "--text", "t"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            # Verdicts are claim-bound — the same session
            # boundary fires before any review derivation.
            (["review", "submit", "--claim", cid, "--verdict", "approve"],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["review", "submit", "--claim", cid, "--verdict", "approve"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            # Completion is claim-bound — refused without a
            # session and under an unknown session at the same chokepoint,
            # before any gate step runs.
            (["item", "complete", "--claim", cid],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["item", "complete", "--claim", cid],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            # Handoff create/accept/decline are
            # session-bound — refused without a session and under an
            # unknown session before any transfer state exists.
            (["handoff", "create", "--claim", cid, "--to", "worker",
              "--reason", "r", "--summary", "s", "--completed", "c",
              "--remaining", "w", "--risks", "k", "--next-action", "n",
              "--proof-ref", f"file:{pathlib.Path(__file__)}"],
             self.NO_SESSION, "session_mismatch", "session_unavailable"),
            (["handoff", "create", "--claim", cid, "--to", "worker",
              "--reason", "r", "--summary", "s", "--completed", "c",
              "--remaining", "w", "--risks", "k", "--next-action", "n",
              "--proof-ref", f"file:{pathlib.Path(__file__)}"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            # accept/decline from the sessionless human interface land on
            # the human-lane guard (the item-claim precedent); unknown
            # sessions land on the session boundary.
            (["handoff", "accept", "--id", "1", "--intent", "i"],
             self.NO_SESSION, "human_lane_violation",
             "human_lane_forbidden"),
            (["handoff", "accept", "--id", "1", "--intent", "i"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            (["handoff", "decline", "--id", "1", "--reason", "r"],
             self.NO_SESSION, "human_lane_violation",
             "human_lane_forbidden"),
            (["handoff", "decline", "--id", "1", "--reason", "r"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            (["--as", "worker", "inbox"],
             self.NO_SESSION, "invalid_transition",
             "transition_not_available"),  # human consume
            # unknown session ids are refused everywhere
            (["item", "claim", str(self.item), "--intent", "i"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            (["say", "hi", "--to", "worker"],
             {"COOP_SESSION_ID": "ghost", "COOP_AGENT": "worker"},
             "session_mismatch", "session_unavailable"),
            # human-only lanes refused inside a session
            (["admin", "release", cid, "--reason", "r"],
             self.LIVE, "human_lane_violation", "human_lane_forbidden"),
            (["admin", "answer", str(self.qid), "--answer", "a",
              "--reason", "r"], self.LIVE, "human_lane_violation",
             "human_lane_forbidden"),
            # Contract revision is the human lane — any
            # live session refuses before a row is read.
            (["item", "revise", str(self.revisable), "--reason", "r",
              "--objective", "in-session"],
             self.LIVE, "human_lane_violation", "human_lane_forbidden"),
            # The wrong-agent column's live cells of the published table.
            (["review", "claim", str(self.named_review),
              "--intent", "i"],
             {"COOP_SESSION_ID": "s-third", "COOP_AGENT": "third"},
             "addressed_target_mismatch", "addressed_target_mismatch"),
            (["review", "claim", str(self.named_review),
              "--intent", "i"],
             {"COOP_SESSION_ID": "s-owner2", "COOP_AGENT": "owner2"},
             "self_review", "reviewer_is_owner"),
            (["handoff", "accept", "--id", str(self.pending_handoff),
              "--intent", "i"],
             {"COOP_SESSION_ID": "s-third", "COOP_AGENT": "third"},
             "addressed_target_mismatch", "addressed_target_mismatch"),
            (["handoff", "decline", "--id", str(self.pending_handoff),
              "--reason", "r"],
             {"COOP_SESSION_ID": "s-third", "COOP_AGENT": "third"},
             "addressed_target_mismatch", "addressed_target_mismatch"),
            # The wrong-claim-kind column's live cells:
            # a question-response claim presented to each claim-bound
            # mutation, and the implementation claim presented to the
            # review verdict.
            (["receipt", "submit",
              "--claim", str(self.qr_claim["claim_id"]),
              "--path", str(pathlib.Path(__file__)),
              "--summary", "s", "--proof", "p"],
             self.LIVE, "invalid_transition", "claim_lane_mismatch"),
            (["review", "request",
              "--claim", str(self.qr_claim["claim_id"])],
             self.LIVE, "invalid_transition", "claim_lane_mismatch"),
            (["review", "submit", "--claim", cid,
              "--verdict", "approve"],
             self.LIVE, "invalid_transition", "claim_lane_mismatch"),
            (["decision", "record",
              "--claim", str(self.qr_claim["claim_id"]),
              "--text", "t"],
             self.LIVE, "invalid_transition", "claim_lane_mismatch"),
            (["item", "complete",
              "--claim", str(self.qr_claim["claim_id"])],
             self.LIVE, "invalid_transition", "claim_lane_mismatch"),
            (["handoff", "create",
              "--claim", str(self.qr_claim["claim_id"]),
              "--to", "worker", "--reason", "r", "--summary", "s",
              "--completed", "c", "--remaining", "w", "--risks", "k",
              "--next-action", "n",
              "--proof-ref", f"file:{pathlib.Path(__file__)}"],
             self.LIVE, "invalid_transition", "claim_lane_mismatch"),
        ]
        for argv, env, error_type, reason_code in rows:
            with self.subTest(
                    argv=" ".join(argv), error_type=error_type,
                    reason_code=reason_code):
                code, out, err = self._run(["--json", *argv], env)
                self.assertEqual(code, 1, f"{argv} -> {out or err}")
                payload = json.loads(err)["error"]
                self.assertEqual(payload["type"], error_type)
                self.assertEqual(payload["reason_code"], reason_code)
        # And the read/no-session-legal surface succeeds — including the
        # matrix's only required-success mutation cell: sessionless
        # revision.
        for argv in (["status"], ["queue"],
                     ["--as", "worker", "inbox", "--peek"],
                     ["item", "show", str(self.item), "--packet"],
                     ["say", "human broadcast"],
                     ["item", "revise", str(self.revisable), "--reason",
                      "matrix required-success cell", "--context",
                      "revised without a session"]):
            with self.subTest(argv=" ".join(argv)):
                code, out, err = self._run(argv, self.NO_SESSION)
                self.assertEqual(code, 0, err)

    # ------------------------------------------------------------------
    # The published authorization table, finalized per cell.  Every
    # cell is live (asserted by a row in the walk above), delegated (the
    # named test asserts it — existence machine-checked below), or a
    # structural N/A with its reason.  No silent cells.
    # ------------------------------------------------------------------
    SUPERSEDED = ("delegated",
                  "tests.test_claims.ConcurrencySuite."
                  "test_superseded_claim_rejected_everywhere")
    PUBLISHED_MATRIX = {
        "receipt submit": {
            "no_session": ("live", "session_mismatch"),
            "wrong_session": ("live", "session_mismatch"),
            "wrong_agent": ("na", "identity rides the claim's session"),
            "superseded_claim": SUPERSEDED,
            "wrong_claim_kind": ("live", "invalid_transition"),
        },
        "review request": {
            "no_session": ("live", "session_mismatch"),
            "wrong_session": ("live", "session_mismatch"),
            "wrong_agent": ("na", "identity rides the claim's session"),
            "superseded_claim": SUPERSEDED,
            "wrong_claim_kind": ("live", "invalid_transition"),
        },
        "review claim": {
            "no_session": ("live", "session_mismatch"),
            "wrong_session": ("na", "mints its own claim — unknown-session"
                                    " hardening asserted anyway"),
            "wrong_agent": ("live", "addressed_target_mismatch|self_review"),
            "superseded_claim": ("na", "no claim presented"),
            "wrong_claim_kind": ("na", "no claim presented"),
        },
        "review submit": {
            "no_session": ("live", "session_mismatch"),
            "wrong_session": ("live", "session_mismatch"),
            "wrong_agent": ("delegated",
                            "tests.test_reviews.ReviewStateClosure."
                            "test_takeover_mid_review_verdict_refused_"
                            "self_review"),
            "superseded_claim": ("delegated",
                                 "tests.test_reviews.VerdictSlugPrecedence."
                                 "test_verdict_via_replacement_closed_"
                                 "claim_is_stale_claim"),
            "wrong_claim_kind": ("live", "invalid_transition"),
        },
        "handoff create": {
            "no_session": ("live", "session_mismatch"),
            "wrong_session": ("live", "session_mismatch"),
            "wrong_agent": ("na", "identity rides the claim's session"),
            "superseded_claim": SUPERSEDED,
            "wrong_claim_kind": ("live", "invalid_transition"),
        },
        "handoff accept": {
            "no_session": ("live", "human_lane_violation"),
            "wrong_session": ("na", "mints its own claim — unknown-session"
                                    " hardening asserted anyway"),
            "wrong_agent": ("live", "addressed_target_mismatch"),
            "superseded_claim": ("na", "no claim presented"),
            "wrong_claim_kind": ("na", "no claim presented"),
        },
        "handoff decline": {
            "no_session": ("live", "human_lane_violation"),
            "wrong_session": ("na", "no claim presented — unknown-session"
                                    " hardening asserted anyway"),
            "wrong_agent": ("live", "addressed_target_mismatch"),
            "superseded_claim": ("na", "no claim presented"),
            "wrong_claim_kind": ("na", "no claim presented"),
        },
        "decision record": {
            "no_session": ("live", "session_mismatch"),
            "wrong_session": ("live", "session_mismatch"),
            "wrong_agent": ("na", "identity rides the claim's session"),
            "superseded_claim": SUPERSEDED,
            "wrong_claim_kind": ("live", "invalid_transition"),
        },
        "item complete": {
            "no_session": ("live", "session_mismatch"),
            "wrong_session": ("live", "session_mismatch"),
            "wrong_agent": ("delegated",
                            "tests.test_completion.ApprovalLeg."
                            "test_reviewer_reclaims_then_self_completes_"
                            "refused"),
            "superseded_claim": SUPERSEDED,
            "wrong_claim_kind": ("live", "invalid_transition"),
        },
        "item revise": {
            "no_session": ("live", "REQUIRED SUCCESS"),
            "wrong_session": ("live", "human_lane_violation"),
            "wrong_agent": ("na", "the human lane has no agent identity"),
            "superseded_claim": ("na", "no claim involved"),
            "wrong_claim_kind": ("na", "no claim involved"),
        },
    }

    BOUNDARIES = ("no_session", "wrong_session", "wrong_agent",
                  "superseded_claim", "wrong_claim_kind")

    def test_published_table_is_complete_and_delegations_resolve(self):
        import importlib
        self.assertEqual(len(self.PUBLISHED_MATRIX), 10)
        for mutation, cells in self.PUBLISHED_MATRIX.items():
            self.assertEqual(tuple(cells), self.BOUNDARIES, mutation)
            for boundary, (kind, detail) in cells.items():
                with self.subTest(mutation=mutation, boundary=boundary):
                    self.assertIn(kind, ("live", "delegated", "na"))
                    self.assertTrue(detail)
                    if kind == "delegated":
                        mod_path, cls_name, test_name = detail.rsplit(
                            ".", 2)
                        mod = importlib.import_module(mod_path)
                        self.assertTrue(
                            hasattr(getattr(mod, cls_name), test_name),
                            detail)

    def test_admin_release_powers_stay_bounded(self):
        # Operator recovery can release a stale claim with a reason, but cannot
        # create a claim, change ownership, or complete a task — the
        # completion gate is unreachable from the recovery lane.
        conn = coopdb.connect(self.db)
        try:
            item = coopdb.create_item(
                conn, actor="human", session_id=None,
                **contract_kwargs(title="release-bounds"))
            claim = coopdb.claim_item(
                conn, item_id=item, actor="worker", session_id="s-live",
                intent="release fixture")
            cid = claim["claim_id"]
            conn.execute(
                "UPDATE claims SET status='stale' WHERE claim_id=?",
                (cid,))
            conn.commit()
            before_claims = conn.execute(
                "SELECT COUNT(*) FROM claims").fetchone()[0]
            before_owner = conn.execute(
                "SELECT owner_agent_id FROM items WHERE id=?",
                (item,)).fetchone()[0]
            coopdb.admin_release(
                conn, claim_id=cid, reason="release-bounds fixture",
                confirm_process_stopped=True)
            after = conn.execute(
                "SELECT status, close_reason FROM claims WHERE claim_id=?",
                (cid,)).fetchone()
            self.assertEqual(after["status"], "released")
            # No claim created, no ownership change, no completion.
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM claims").fetchone()[0], before_claims)
            self.assertEqual(conn.execute(
                "SELECT owner_agent_id FROM items WHERE id=?",
                (item,)).fetchone()[0], before_owner)
            self.assertNotEqual(conn.execute(
                "SELECT status FROM items WHERE id=?",
                (item,)).fetchone()[0], "done")
            with self.assertRaises(StaleClaim):
                coopdb.complete_item(
                    conn, claim_id=cid, session_id="s-live",
                    actor="worker")
            self.assertNotEqual(conn.execute(
                "SELECT status FROM items WHERE id=?",
                (item,)).fetchone()[0], "done")
        finally:
            conn.close()


class GuideParity(unittest.TestCase):
    GUIDE = COOP_DIR / "COOP_GUIDE.md"

    def _guide_commands(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        blocks = re.findall(r"```\n(.*?)```", text, flags=re.S)
        for block in blocks:
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("coop "):
                    yield line
        self.text = text

    def test_every_guide_example_parses_against_the_real_cli(self):
        parser = coopcli.build_parser()
        commands = list(self._guide_commands())
        self.assertGreaterEqual(len(commands), 15)  # the guide is worked
        for line in commands:
            with self.subTest(command=line):
                parser.parse_args(shlex.split(line)[1:])

    def test_prohibited_actions_and_protocol_lanes_present(self):
        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertIn("## Prohibited actions", text)
        for phrase in (
                "Never write SQLite directly",
                "Never edit a projection",
                "Never run an agent mutation outside `coop session run`",
                "Never continue under a stale, released, completed, or "
                "closed claim",
                "Never use prompt files, stdin contracts, external chat",
                "Commands from pre-release builds"):
            self.assertIn(phrase, text)
        lowered = text.lower()
        for lane in ("receipts", "reviews", "handoffs", "decisions",
                     "completion"):
            self.assertIn(lane, lowered)  # the guide covers every lane


if __name__ == "__main__":
    unittest.main()
