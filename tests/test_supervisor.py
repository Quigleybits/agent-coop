"""The supervised session loop.

Everything injectable, nothing real: fake prepared trees, an injected
monotonic driven by a recording waiter, the board clock frozen via
coopdb._clock, and a scriptable projector. The supervisor is policy over
already-proven primitives — these tests pin the policy.
"""

import unittest
import unittest.mock
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_coop import coop_supervisor
from agent_coop import coopdb
from agent_coop.coop_errors import ActiveSessionConflict, LaunchFailed
from tests.test_claims import Clock, contract_kwargs


class FakeMonotonic:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeTree:
    def __init__(self):
        self.root_code = None
        self.empty = False
        self.calls = []

    def poll_root(self):
        return self.root_code

    def graceful_stop(self):
        self.calls.append("graceful")
        return True

    def force_stop(self):
        self.calls.append("force")
        self.empty = True  # a forced job kill drains the tree

    def is_empty(self):
        return self.empty

    def close(self):
        self.calls.append("close")
        self.empty = True


class FakePrepared:
    def __init__(self, tree, *, release_error=None, on_release=None):
        self.tree = tree
        self.release_error = release_error
        self.on_release = on_release
        self.calls = []

    def release(self):
        self.calls.append("release")
        if self.on_release:
            self.on_release()
        if self.release_error:
            raise self.release_error
        return self.tree

    def abort(self):
        self.calls.append("abort")


class ScriptedWaiter:
    """The loop's only sleep: each call advances fake monotonic time (and
    optionally the board clock) and fires tick-indexed script actions."""

    def __init__(self, monotonic, board_clock=None, script=None,
                 advance_board=True):
        self.monotonic = monotonic
        self.board_clock = board_clock
        self.script = script or {}
        self.advance_board = advance_board
        self.waits = []

    def __call__(self, seconds):
        tick = len(self.waits)
        self.waits.append(seconds)
        self.monotonic.advance(seconds)
        if self.board_clock is not None and self.advance_board:
            self.board_clock.advance(seconds)
        action = self.script.get(tick)
        if action:
            action()


class SupervisorBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "board.db")
        self.clock = Clock()
        self._orig = coopdb._clock
        coopdb._clock = self.clock
        self.addCleanup(self._restore)
        conn = coopdb.connect(self.db)
        coopdb.init_db(conn)
        conn.close()
        self.tree = FakeTree()
        self.prepared = FakePrepared(self.tree)
        self.monotonic = FakeMonotonic()
        self.projector_calls = []

    def _restore(self):
        coopdb._clock = self._orig

    def projector(self, conn, agent, out_dir):
        self.projector_calls.append(agent)
        return None

    def build(self, *, script=None, timings=None, prepared=None,
              projector=None, provider="codex", name=None):
        waiter = ScriptedWaiter(self.monotonic, self.clock, script)
        self.waiter = waiter
        return coop_supervisor.Supervisor(
            self.db, provider=provider, agent_id=name,
            argv=["python", "-c", "pass"], cwd=self.tmp.name,
            timings=timings or coop_supervisor.Timings(),
            monotonic=self.monotonic, waiter=waiter,
            tree_factory=lambda argv, **kw: prepared or self.prepared,
            projector=projector or self.projector)

    def session_row(self, sid):
        conn = coopdb.connect(self.db)
        try:
            return conn.execute(
                "SELECT * FROM sessions WHERE session_id=?",
                (sid,)).fetchone()
        finally:
            conn.close()

    def sessions_count(self):
        conn = coopdb.connect(self.db)
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        finally:
            conn.close()

    def stop_root_at(self, tick, code=0, empty=True):
        def action():
            self.tree.root_code = code
            self.tree.empty = empty
        return {tick: action}


class ProductionDefaults(unittest.TestCase):
    def test_production_defaults_verbatim(self):
        t = coop_supervisor.Timings()
        self.assertEqual(t.poll_interval_s, 5)
        self.assertEqual(t.lease_s, 30)
        self.assertEqual(t.checkpoint_limit_s, 900)
        self.assertEqual(t.resume_grace_s, 900)
        self.assertEqual(t.max_runtime_s, 8 * 3600)
        self.assertEqual(t.shutdown_grace_s, 10)


class StartSequence(SupervisorBoard):
    def test_session_row_exists_before_release(self):
        observed = {}

        def on_release():
            sup_sid = sup.session_id
            row = self.session_row(sup_sid)
            observed["row_at_release"] = None if row is None else row["status"]
            self.tree.root_code = 0
            self.tree.empty = True

        prepared = FakePrepared(self.tree, on_release=on_release)
        sup = self.build(prepared=prepared)
        sup.run()
        self.assertEqual(observed["row_at_release"], "running")
        self.assertEqual(prepared.calls, ["release"])

    def test_insert_failure_aborts_and_leaves_no_new_row(self):
        conn = coopdb.connect(self.db)
        coopdb.insert_session(
            conn, session_id="occupied", agent_id="codex", provider="codex",
            command=["x"], cwd=".", max_runtime_s=10, grace_s=1)
        conn.close()
        sup = self.build()  # same default agent name: codex
        with self.assertRaises(ActiveSessionConflict):
            sup.run()
        self.assertEqual(self.prepared.calls, ["abort"])
        self.assertEqual(self.sessions_count(), 1)  # only the occupier

    def test_launch_failed_records_exited_launch_failed(self):
        prepared = FakePrepared(self.tree, release_error=LaunchFailed("no ack"))
        sup = self.build(prepared=prepared)
        with self.assertRaises(LaunchFailed):
            sup.run()
        row = self.session_row(sup.session_id)
        self.assertEqual(row["status"], "exited")
        self.assertEqual(row["termination_reason"], "launch_failed")

    def test_child_env_carries_canonical_and_alias_names(self):
        captured = {}

        def factory(argv, *, cwd, env, session_id, **kw):
            captured.update(env)
            captured["_sid"] = session_id
            return self.prepared

        waiter = ScriptedWaiter(self.monotonic, self.clock,
                                self.stop_root_at(0))
        sup = coop_supervisor.Supervisor(
            self.db, provider="codex", agent_id="backend",
            argv=["python", "-c", "pass"], cwd=self.tmp.name,
            timings=coop_supervisor.Timings(), monotonic=self.monotonic,
            waiter=waiter, tree_factory=factory, projector=self.projector)
        sup.run()
        self.assertEqual(captured["COOP_AGENT"], "backend")
        self.assertEqual(captured["COOP_AGENT_ID"], "backend")
        self.assertEqual(captured["COOP_PROVIDER"], "codex")
        self.assertEqual(captured["COOP_SESSION_ID"], captured["_sid"])
        self.assertEqual(captured["COOP_DB"], captured["COOP_DB_PATH"])
        for key in ("COOP_AGENT", "COOP_PROVIDER", "COOP_SESSION_ID",
                    "COOP_DB"):
            self.assertTrue(captured[key])  # never empty strings


class MaintenanceLoop(SupervisorBoard):
    def test_fixed_five_second_monotonic_schedule(self):
        sup = self.build(script=self.stop_root_at(3))
        sup.run()
        self.assertEqual(self.waiter.waits[:4], [5, 5, 5, 5])
        self.assertEqual(self.monotonic.t, 20.0)

    def test_renewal_scoped_to_own_unexpired_claims(self):
        state = {}

        def claim_now():
            conn = coopdb.connect(self.db)
            try:
                item = coopdb.create_item(
                    conn, actor="human", session_id=None, **contract_kwargs())
                state["claim"] = coopdb.claim_item(
                    conn, item_id=item, actor="codex",
                    session_id=sup.session_id, intent="supervised work")
            finally:
                conn.close()

        def read_expiry():
            conn = coopdb.connect(self.db)
            try:
                state["expiry"] = conn.execute(
                    "SELECT lease_expires_at FROM claims WHERE claim_id=?",
                    (state["claim"]["claim_id"],)).fetchone()[0]
            finally:
                conn.close()

        def finish():
            read_expiry()
            self.tree.root_code = 0
            self.tree.empty = True

        sup = self.build(script={0: claim_now, 2: finish})
        sup.run()
        first = state["claim"]["lease_expires_at"]
        self.assertGreater(state["expiry"], first)  # renewed by the loop

    def test_expired_claim_never_revived_after_a_stall(self):
        state = {}

        def claim_then_stall():
            conn = coopdb.connect(self.db)
            try:
                item = coopdb.create_item(
                    conn, actor="human", session_id=None, **contract_kwargs())
                state["claim"] = coopdb.claim_item(
                    conn, item_id=item, actor="codex",
                    session_id=sup.session_id, intent="about to nap")
            finally:
                conn.close()
            self.clock.advance(40)  # laptop sleep: board time jumps
            self.monotonic.advance(40)

        def finish():
            self.tree.root_code = 0
            self.tree.empty = True

        sup = self.build(script={0: claim_then_stall, 1: finish})
        sup.run()
        conn = coopdb.connect(self.db)
        try:
            row = conn.execute(
                "SELECT status, lease_expires_at FROM claims WHERE "
                "claim_id=?", (state["claim"]["claim_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "stale")  # swept, never revived
        self.assertEqual(row["lease_expires_at"],
                         state["claim"]["lease_expires_at"])

    def test_projection_failure_is_logged_and_retried(self):
        outcomes = iter(["disk exploded", None, None, None])

        def flaky(conn, agent, out_dir):
            self.projector_calls.append(agent)
            return next(outcomes, None)

        sup = self.build(script=self.stop_root_at(2), projector=flaky)
        sup.run()
        self.assertTrue(any("disk exploded" in w for w in sup.warnings))
        self.assertGreaterEqual(len(self.projector_calls), 2)  # retried
        row = self.session_row(sup.session_id)
        self.assertEqual(row["status"], "exited")  # loop was untouched


class TerminalPaths(SupervisorBoard):
    def test_root_exit_stops_renewal_then_records_after_drain(self):
        def root_exits_tree_lingers():
            self.tree.root_code = 7
            self.tree.empty = False  # grandchild lingers

        sup = self.build(script={1: root_exits_tree_lingers})
        code = sup.run()
        self.assertEqual(code, 7)
        row = self.session_row(sup.session_id)
        self.assertEqual((row["status"], row["termination_reason"]),
                         ("exited", "child_exit"))
        self.assertEqual(row["exit_code"], 7)
        # The lingering tree was forced before the terminal write.
        self.assertIn("force", self.tree.calls)

    def test_cancellation_stops_renewal_before_anything_else(self):
        state = {}

        def claim_then_cancel():
            conn = coopdb.connect(self.db)
            try:
                item = coopdb.create_item(
                    conn, actor="human", session_id=None, **contract_kwargs())
                state["claim"] = coopdb.claim_item(
                    conn, item_id=item, actor="codex",
                    session_id=sup.session_id, intent="will be cancelled")
            finally:
                conn.close()
            sup.cancel()

        sup = self.build(script={0: claim_then_cancel})
        sup.run()
        row = self.session_row(sup.session_id)
        self.assertEqual((row["status"], row["termination_reason"]),
                         ("cancelled", "cancelled"))
        conn = coopdb.connect(self.db)
        try:
            expiry = conn.execute(
                "SELECT lease_expires_at FROM claims WHERE claim_id=?",
                (state["claim"]["claim_id"],)).fetchone()[0]
        finally:
            conn.close()
        # The cancel tick renewed nothing: the lease is the claim-time one.
        self.assertEqual(expiry, state["claim"]["lease_expires_at"])
        self.assertEqual(self.tree.calls[0], "graceful")

    def test_max_runtime_times_out(self):
        sup = self.build(
            timings=coop_supervisor.Timings(max_runtime_s=12))
        code = sup.run()
        row = self.session_row(sup.session_id)
        self.assertEqual((row["status"], row["termination_reason"]),
                         ("timed_out", "max_runtime"))
        self.assertEqual(code, 1)

    def test_checkpoint_timeout_renews_nothing_and_terminates_the_tree(self):
        state = {}

        def claim_now():
            conn = coopdb.connect(self.db)
            try:
                item = coopdb.create_item(
                    conn, actor="human", session_id=None, **contract_kwargs())
                state["claim"] = coopdb.claim_item(
                    conn, item_id=item, actor="codex",
                    session_id=sup.session_id, intent="will go quiet")
            finally:
                conn.close()

        def read_expiry():
            conn = coopdb.connect(self.db)
            try:
                return conn.execute(
                    "SELECT lease_expires_at FROM claims WHERE claim_id=?",
                    (state["claim"]["claim_id"],)).fetchone()[0]

            finally:
                conn.close()

        sup = self.build(
            script={0: claim_now,
                    4: lambda: state.update(before=read_expiry())},
            timings=coop_supervisor.Timings(checkpoint_limit_s=18))
        sup.run()
        row = self.session_row(sup.session_id)
        self.assertEqual((row["status"], row["termination_reason"]),
                         ("timed_out", "checkpoint_timeout"))
        self.assertEqual(read_expiry(), state["before"])  # no renewal after
        self.assertEqual(self.tree.calls[0], "graceful")
        self.assertIn("force", self.tree.calls)  # stubborn fake tree

    def test_failed_final_write_leaves_the_visible_wedge(self):
        sup = self.build(script=self.stop_root_at(1))
        with unittest.mock.patch.object(
                coop_supervisor.coopdb, "finish_session",
                side_effect=RuntimeError("db gone")):
            with self.assertRaises(RuntimeError):
                sup.run()
        row = self.session_row(sup.session_id)
        self.assertEqual(row["status"], "running")  # the documented wedge
        self.assertIn("close", self.tree.calls)  # the tree was still closed


if __name__ == "__main__":
    unittest.main()
