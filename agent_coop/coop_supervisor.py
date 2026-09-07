"""The supervised session loop.

Deterministic policy over proven primitives: prepare→insert→release
start ordering, a fixed five-second maintenance cadence on monotonic
deadlines, stale sweeps and lease renewal scoped to this exact session,
checkpoint-timeout shutdown, and a graceful→grace→force ladder that
records the terminal state only after the process tree is confirmed
empty. The loop never reasons, never injects text into the child, and
prefers a visible wedge (a `running` row it could not finish) to two
silent workspace writers.
"""
import os
import sys
import threading
import uuid
from dataclasses import dataclass

from agent_coop import coop_process
from agent_coop import coopdb
from agent_coop import projection

# The exact platform baselines — not indicative. PATH sits
# in both because the opaque argv must still resolve; nothing else is
# inferred on the harness's behalf.
WINDOWS_ENV_BASELINE = ("SystemRoot", "SystemDrive", "ComSpec",
                        "PATH", "PATHEXT", "TEMP", "TMP")
POSIX_ENV_BASELINE = ("PATH", "HOME", "TMPDIR", "LANG", "TERM")

_MISSING = object()


def build_child_env(parent, allowlist, coop_pairs, *, windows):
    """The three-stage allowlisted child environment, later stages
    winning every collision: (1) the platform baseline, (2) the named
    parent variables that exist, (3) the COOP_* set, always last. Windows
    names match and deduplicate case-insensitively (within a stage the
    last writer wins); POSIX names are case-sensitive. Returns
    (env, skipped_names) — skipped are allowlist names absent from the
    parent, for the supervisor's single warning line."""
    result, casemap = {}, {}

    def put(name, value):
        if windows:
            prior = casemap.get(name.upper())
            if prior is not None and prior != name:
                del result[prior]
            casemap[name.upper()] = name
        result[name] = value

    def lookup(name):
        if windows:
            hit = _MISSING
            for key, value in parent.items():  # last writer wins in-stage
                if key.upper() == name.upper():
                    hit = (key, value)
            return hit
        return (name, parent[name]) if name in parent else _MISSING

    baseline = WINDOWS_ENV_BASELINE if windows else POSIX_ENV_BASELINE
    for name in baseline:
        hit = lookup(name)
        if hit is not _MISSING:
            put(*hit)
    skipped = []
    for name in allowlist or ():
        hit = lookup(name)
        if hit is _MISSING:
            skipped.append(name)
        else:
            put(*hit)
    for name, value in coop_pairs.items():
        put(name, value)
    return result, skipped


@dataclass
class Timings:
    """Production defaults, verbatim from the design's timing table."""
    poll_interval_s: float = 5
    lease_s: float = coopdb.DEFAULT_LEASE_SECONDS          # 30
    checkpoint_limit_s: float = coopdb.DEFAULT_CHECKPOINT_LIMIT_SECONDS  # 900
    resume_grace_s: float = coopdb.DEFAULT_RESUME_GRACE_SECONDS  # 900
    max_runtime_s: float = 8 * 3600
    shutdown_grace_s: float = 10


class Supervisor:
    def __init__(self, db_path, *, provider, agent_id=None, argv, cwd=None,
                 timings=None, clock=None, monotonic=None, waiter=None,
                 tree_factory=None, projector=None, env=None,
                 env_allowlist=None):
        import time
        self.db_path = str(db_path)
        self.provider = provider
        self.agent_id = agent_id or provider
        self.argv = list(argv)
        self.cwd = str(cwd or os.getcwd())
        self.timings = timings or Timings()
        self.clock = clock or coopdb._clock  # stamps the warning log only
        self.monotonic = monotonic or time.monotonic
        self._cancel = threading.Event()
        self.waiter = waiter or self._cancel.wait
        self.tree_factory = tree_factory or coop_process.prepare_tree
        self.projector = projector or (
            lambda conn, agent, out_dir: projection.refresh_inbox(
                conn, agent, out_dir=out_dir, contain_within=self.cwd))
        self._base_env = env
        self.env_allowlist = env_allowlist
        self.out_dir = os.path.join(self.cwd, "inbox")
        self.session_id = None
        self.exit_code = None
        self.warnings = []

    # -- lifecycle ----------------------------------------------------------

    def cancel(self):
        """Request a graceful stop; the loop notices before its next tick."""
        self._cancel.set()

    def run(self):
        """Start, supervise, shut down. Returns the child's exit code for a
        natural exit; 1 for cancelled/timed-out runs. Start failures raise
        their typed errors after cleaning up (never an unowned launch)."""
        conn = coopdb.connect(self.db_path, require_current=True)
        try:
            tree = self._start(conn)
            try:
                reason = self._loop(conn, tree)
            except KeyboardInterrupt:
                reason = "cancelled"
            if reason is not None:
                self._shutdown(conn, tree, reason)
            else:
                tree.close()  # someone else finished the session row
            return self.exit_code if reason == "child_exit" else 1
        finally:
            conn.close()

    # -- start sequence ------------------------------------------------------

    def _child_env(self):
        parent = dict(self._base_env if self._base_env is not None
                      else os.environ)
        pairs = {
            "COOP_AGENT": self.agent_id,
            "COOP_AGENT_ID": self.agent_id,
            "COOP_PROVIDER": self.provider,
            "COOP_SESSION_ID": self.session_id,
            "COOP_DB": self.db_path,
            "COOP_DB_PATH": self.db_path,
        }
        for key, value in pairs.items():
            if not value:  # never export empty strings
                raise coopdb.InvalidTransition(
                    f"internal: refusing to export empty {key}")
        if self.env_allowlist is None:
            # Trusted-local default: full inheritance plus COOP_*.
            parent.update(pairs)
            return parent
        env, skipped = build_child_env(
            parent, self.env_allowlist, pairs, windows=os.name == "nt")
        if skipped:
            self._warn("env allowlist skipped absent names: "
                       + ", ".join(skipped))
        return env

    def _start(self, conn):
        if not projection.contained(self.cwd, self.out_dir):
            # Containment preflight: refuse the entire launch before any process
            # preparation — this typed error can never fire while a child
            # tree is alive.
            raise coopdb.ProjectionPathInvalid(
                f"projection out-dir {self.out_dir!r} escapes the launch "
                f"working directory {self.cwd!r}")
        self.session_id = uuid.uuid4().hex
        prepared = self.tree_factory(
            self.argv, cwd=self.cwd, env=self._child_env(),
            session_id=self.session_id)
        try:
            coopdb.insert_session(
                conn, session_id=self.session_id, agent_id=self.agent_id,
                provider=self.provider, command=self.argv, cwd=self.cwd,
                max_runtime_s=self.timings.max_runtime_s,
                grace_s=self.timings.shutdown_grace_s,
                stdin_isatty=sys.stdin.isatty())
        except BaseException:
            prepared.abort()  # the harness never launched; no session row
            raise
        try:
            return prepared.release()
        except coop_process.LaunchFailed:
            coopdb.finish_session(
                conn, self.session_id, status="exited",
                reason="launch_failed", exit_code=None)
            raise

    # -- maintenance loop -----------------------------------------------------

    def _tick(self, conn):
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id=?",
            (self.session_id,)).fetchone()
        if row is None or row["status"] != "running":
            return "session_terminal"
        coopdb.sweep_expired(conn)
        overdue = coopdb.find_overdue(
            conn, session_id=self.session_id,
            checkpoint_limit_seconds=self.timings.checkpoint_limit_s)
        if overdue:
            return "checkpoint_timeout"
        stamp = coopdb.now()
        conn.execute(
            "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
            (stamp, self.session_id))
        conn.execute(
            "UPDATE agents SET last_seen_at=? WHERE name=?",
            (stamp, self.agent_id))
        coopdb.renew_claims(conn, session_id=self.session_id,
                            lease_seconds=self.timings.lease_s)
        return None

    def _warn(self, message):
        self.warnings.append(f"{self.clock().isoformat()} {message}")

    def _loop(self, conn, tree):
        started = self.monotonic()
        deadline = started + self.timings.poll_interval_s
        while True:
            if self._cancel.is_set():
                return "cancelled"  # renew nothing more, stop first
            if self.monotonic() - started >= self.timings.max_runtime_s:
                return "max_runtime"
            root = tree.poll_root()
            if root is not None:
                self.exit_code = root
                return "child_exit"  # renewal stops now; drain in shutdown
            try:
                instruction = coopdb.mutate(conn, self._tick)
            except Exception as exc:
                # No renewal happened; leases keep draining toward stale.
                self._warn(f"maintenance transaction failed: {exc}")
                instruction = None
            if instruction == "checkpoint_timeout":
                return "checkpoint_timeout"
            if instruction == "session_terminal":
                return None
            warning = self.projector(conn, self.agent_id, self.out_dir)
            if warning:
                self._warn(warning)
            remaining = max(0.0, deadline - self.monotonic())
            self.waiter(remaining)
            deadline += self.timings.poll_interval_s

    # -- shutdown -------------------------------------------------------------

    def _await_empty(self, tree, timeout_s):
        deadline = self.monotonic() + timeout_s
        while not tree.is_empty():
            if self.monotonic() >= deadline:
                return False
            self.waiter(0.1)
        return True

    def _shutdown(self, conn, tree, reason):
        grace = self.timings.shutdown_grace_s
        if reason != "child_exit":
            try:
                tree.graceful_stop()
            except Exception as exc:
                self._warn(f"graceful stop failed: {exc}")
        if not self._await_empty(tree, grace):
            tree.force_stop()
            self._await_empty(tree, grace)
        try:
            coopdb.finish_session(
                conn, self.session_id,
                status=coopdb.SESSION_TERMINAL_MAP[reason],
                reason=reason, exit_code=self.exit_code)
            warning = self.projector(conn, self.agent_id, self.out_dir)
            if warning:
                self._warn(warning)
        finally:
            tree.close()
