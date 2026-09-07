"""Security dispositions + the stdin-TTY evidence bit.

Timing bounds are pinned at the CLI conversion chokepoint — refusals are
pre-database (no board file may exist afterward) and pre-process. The
child environment is pinned to exact platform baselines and a three-stage
build, both platforms driven directly through the pure builder. The launch
preflight (typed refusal before any process preparation) and the
never-raising runtime recheck in refresh_inbox are pinned too. The
stdin_isatty keyword is recorded verbatim in the session_started payload:
true/false/null, null distinguishable from both booleans.
"""
import argparse
import contextlib
import io
import json
import os
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_coop import cli as coopcli
from agent_coop import coop_supervisor
from agent_coop import coopdb
from agent_coop import projection
from agent_coop.coop_errors import InvalidTiming, ProjectionPathInvalid
from tests.test_supervisor import FakePrepared, FakeTree


def run_cli(argv, env=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = 0
    with unittest.mock.patch.dict("os.environ", env or {}, clear=False):
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


COOP_PAIRS = {
    "COOP_AGENT": "codex", "COOP_AGENT_ID": "codex",
    "COOP_PROVIDER": "codex", "COOP_SESSION_ID": "sess-1",
    "COOP_DB": "board.db", "COOP_DB_PATH": "board.db",
}


class TimingBounds(unittest.TestCase):
    """Timing: 1 <= v <= 31_536_000 on every timing surface, refused before
    any database or process work."""

    SESSION_FLAGS = ("--lease-seconds", "--max-runtime-seconds",
                     "--checkpoint-limit-seconds", "--shutdown-grace-seconds")

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "board.db")

    def test_session_flags_refused_pre_database(self):
        for flag in self.SESSION_FLAGS:
            for bad in ("0", "0.5", "31536001", "1e300"):
                with self.subTest(flag=flag, value=bad):
                    code, _, err = run_cli(
                        ["--db", self.db, "session", "run", "--as", "codex",
                         flag, bad, "--", "harness"])
                    self.assertEqual(code, 1)
                    self.assertIn("invalid_timing", err)
                    self.assertFalse(os.path.exists(self.db),
                                     "a refused timing must not create the board")

    def test_monitor_interval_refused_pre_database(self):
        for bad in ("0", "31536001", "99999999999999999999"):
            with self.subTest(value=bad):
                code, _, err = run_cli(["--db", self.db, "monitor",
                                        "--interval", bad])
                self.assertEqual(code, 1)
                self.assertIn("invalid_timing", err)
                self.assertFalse(os.path.exists(self.db))

    def test_bounds_accept_legal_values(self):
        legal = argparse.Namespace(
            max_runtime_seconds=1, shutdown_grace_seconds=31_536_000.0,
            lease_seconds=1.0, checkpoint_limit_seconds=12.5, interval=2)
        coopcli._enforce_timing_bounds(legal)  # must not raise
        coopcli._enforce_timing_bounds(argparse.Namespace())  # nothing set
        coopcli._enforce_timing_bounds(argparse.Namespace(
            lease_seconds=None, interval=None))

    def test_nan_refused(self):
        with self.assertRaises(InvalidTiming):
            coopcli._enforce_timing_bounds(
                argparse.Namespace(lease_seconds=float("nan")))

    def test_refusal_names_the_flag(self):
        with self.assertRaises(InvalidTiming) as caught:
            coopcli._enforce_timing_bounds(
                argparse.Namespace(checkpoint_limit_seconds=0))
        self.assertIn("checkpoint-limit-seconds", str(caught.exception))


class EnvAllowlist(unittest.TestCase):
    """Child environment: three stages, later stages win; exact baselines;
    case rules per platform; absent allowlist names skipped with one
    warning line."""

    def test_windows_baseline_exact_set(self):
        parent = {"SystemRoot": "C:\\Windows", "SystemDrive": "C:",
                  "ComSpec": "cmd.exe", "Path": "C:\\bin",
                  "PATHEXT": ".EXE", "TEMP": "C:\\t", "TMP": "C:\\t",
                  "APPDATA": "C:\\a", "SECRET_TOKEN": "leak-me-not"}
        env, skipped = coop_supervisor.build_child_env(
            parent, ["APPDATA", "MISSING_ONE"], COOP_PAIRS, windows=True)
        expected = {"SystemRoot", "SystemDrive", "ComSpec", "Path",
                    "PATHEXT", "TEMP", "TMP", "APPDATA", *COOP_PAIRS}
        self.assertEqual(set(env), expected)
        self.assertEqual(env["Path"], "C:\\bin")  # matched case-insensitively
        self.assertNotIn("SECRET_TOKEN", env)
        self.assertEqual(skipped, ["MISSING_ONE"])

    def test_posix_baseline_case_sensitive(self):
        parent = {"PATH": "/bin", "HOME": "/home/a", "LANG": "C",
                  "path": "/evil", "EXTRA": "kept"}
        env, skipped = coop_supervisor.build_child_env(
            parent, ["EXTRA", "home"], COOP_PAIRS, windows=False)
        expected = {"PATH", "HOME", "LANG", "EXTRA", *COOP_PAIRS}
        self.assertEqual(set(env), expected)  # lowercase 'path' never enters
        self.assertEqual(skipped, ["home"])   # case-sensitive: no match

    def test_windows_case_collision_last_writer_wins(self):
        parent = {"PATH": "first", "Path": "second", "SystemRoot": "C:\\W"}
        env, _ = coop_supervisor.build_child_env(
            parent, [], COOP_PAIRS, windows=True)
        path_keys = [k for k in env if k.upper() == "PATH"]
        self.assertEqual(len(path_keys), 1)
        self.assertEqual(env[path_keys[0]], "second")

    def test_coop_wins_shadowed_name(self):
        parent = {"PATH": "/bin", "COOP_SESSION_ID": "forged"}
        env, skipped = coop_supervisor.build_child_env(
            parent, ["COOP_SESSION_ID"], COOP_PAIRS, windows=False)
        self.assertEqual(env["COOP_SESSION_ID"], "sess-1")
        self.assertEqual(skipped, [])

    def test_default_full_inheritance_unchanged(self):
        parent = {"ANYTHING": "stays", "PATH": "/bin"}
        with TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "b.db")
            sup = coop_supervisor.Supervisor(
                db, provider="codex", argv=["x"], env=parent)
            sup.session_id = "sess-1"
            env = sup._child_env()
        self.assertEqual(env["ANYTHING"], "stays")
        for key in ("COOP_AGENT", "COOP_SESSION_ID", "COOP_DB"):
            self.assertIn(key, env)

    def test_allowlist_child_env_and_one_warning_line(self):
        baseline = (coop_supervisor.WINDOWS_ENV_BASELINE if os.name == "nt"
                    else coop_supervisor.POSIX_ENV_BASELINE)
        parent = {name: f"v-{name}" for name in baseline}
        parent.update({"KEEP_ME": "yes", "DROP_ME": "no"})
        with TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "b.db")
            sup = coop_supervisor.Supervisor(
                db, provider="codex", argv=["x"], env=parent,
                env_allowlist=["KEEP_ME", "GONE1", "GONE2"])
            sup.session_id = "sess-1"
            env = sup._child_env()
            expected = {*baseline, "KEEP_ME",
                        "COOP_AGENT", "COOP_AGENT_ID", "COOP_PROVIDER",
                        "COOP_SESSION_ID", "COOP_DB", "COOP_DB_PATH"}
            self.assertEqual(set(env), expected)
            self.assertNotIn("DROP_ME", env)
            self.assertEqual(len(sup.warnings), 1)
            self.assertIn("GONE1", sup.warnings[0])
            self.assertIn("GONE2", sup.warnings[0])

    def test_parser_repeatable_and_default(self):
        args = coopcli.build_parser().parse_args(
            ["session", "run", "--as", "codex", "--env-allowlist", "A",
             "--env-allowlist", "B", "--", "x"])
        self.assertEqual(args.env_allowlist, ["A", "B"])
        args = coopcli.build_parser().parse_args(
            ["session", "run", "--as", "codex", "--", "x"])
        self.assertIsNone(args.env_allowlist)


class ProjectionContainment(unittest.TestCase):
    """Launch: preflight refusal before any process work; runtime recheck
    warns and skips, never raises."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.db = str(self.work / "board.db")
        conn = coopdb.connect(self.db)
        coopdb.init_db(conn)
        conn.close()

    def test_contained_truth_table(self):
        contained = projection.contained
        self.assertTrue(contained(str(self.work), str(self.work)))
        self.assertTrue(contained(str(self.work), str(self.work / "inbox")))
        self.assertTrue(contained(str(self.work), str(self.work / "a" / "b")))
        self.assertFalse(contained(str(self.work), str(self.outside)))
        self.assertFalse(contained(str(self.work), str(self.root)))
        trap = Path(str(self.work) + "x")  # prefix trap: 'workx' vs 'work'
        trap.mkdir()
        self.assertFalse(contained(str(self.work), str(trap)))

    def test_preflight_refuses_before_any_process_work(self):
        factory_calls = []

        def factory(*args, **kwargs):
            factory_calls.append((args, kwargs))
            raise AssertionError("tree_factory must never run on refusal")

        sup = coop_supervisor.Supervisor(
            self.db, provider="codex", argv=["x"], cwd=str(self.work),
            tree_factory=factory)
        sup.out_dir = str(self.outside / "inbox")
        with self.assertRaises(ProjectionPathInvalid):
            sup.run()
        self.assertEqual(factory_calls, [])
        conn = coopdb.connect(self.db)
        try:
            rows = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()
            self.assertEqual(rows["c"], 0, "no session row on refused launch")
        finally:
            conn.close()

    def test_runtime_recheck_warns_and_skips(self):
        conn = coopdb.connect(self.db)
        try:
            escaping = str(self.outside / "inbox")
            warning = projection.refresh_inbox(
                conn, "alice", out_dir=escaping,
                contain_within=str(self.work))
            self.assertIsNotNone(warning)
            self.assertIn("escapes", warning)
            self.assertFalse((self.outside / "inbox" / "alice.md").exists(),
                             "a refused refresh must write nothing")
            inside = str(self.work / "inbox")
            self.assertIsNone(projection.refresh_inbox(
                conn, "alice", out_dir=inside,
                contain_within=str(self.work)))
            self.assertTrue((self.work / "inbox" / "alice.md").exists())
        finally:
            conn.close()

    def test_default_projector_passes_containment(self):
        sup = coop_supervisor.Supervisor(
            self.db, provider="codex", argv=["x"], cwd=str(self.work))
        sup.out_dir = str(self.outside / "inbox")
        conn = coopdb.connect(self.db)
        try:
            warning = sup.projector(conn, "alice", sup.out_dir)
        finally:
            conn.close()
        self.assertIsNotNone(warning)
        self.assertIn("escapes", warning)
        self.assertFalse((self.outside / "inbox" / "alice.md").exists())


class StdinIsattyEvidence(unittest.TestCase):
    """Design 18: session_started records stdin_isatty verbatim —
    true/false from the supervisor, null from callers that did not
    observe."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "board.db")
        conn = coopdb.connect(self.db)
        coopdb.init_db(conn)
        conn.close()

    def _payload(self, conn):
        row = conn.execute(
            "SELECT payload_json FROM events "
            "WHERE event_type='session_started'").fetchone()
        return row["payload_json"], json.loads(row["payload_json"])

    def test_direct_insert_records_null(self):
        conn = coopdb.connect(self.db)
        try:
            coopdb.insert_session(
                conn, session_id="s1", agent_id="codex", provider="codex",
                command=["x"], cwd=".", max_runtime_s=60, grace_s=5)
            raw, payload = self._payload(conn)
        finally:
            conn.close()
        self.assertIn("stdin_isatty", payload)
        self.assertIsNone(payload["stdin_isatty"])
        self.assertIn('"stdin_isatty":null', raw)  # verbatim JSON null

    def test_explicit_false_distinguishable_from_null(self):
        conn = coopdb.connect(self.db)
        try:
            coopdb.insert_session(
                conn, session_id="s1", agent_id="codex", provider="codex",
                command=["x"], cwd=".", max_runtime_s=60, grace_s=5,
                stdin_isatty=False)
            raw, payload = self._payload(conn)
        finally:
            conn.close()
        self.assertIs(payload["stdin_isatty"], False)
        self.assertIn('"stdin_isatty":false', raw)

    def test_supervisor_records_boolean(self):
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        stub = unittest.mock.Mock()
        stub.isatty.return_value = True
        sup = coop_supervisor.Supervisor(
            self.db, provider="codex", argv=["x"], cwd=str(work),
            tree_factory=lambda argv, cwd, env, session_id:
                FakePrepared(FakeTree()))
        conn = coopdb.connect(self.db)
        try:
            with unittest.mock.patch.object(
                    coop_supervisor.sys, "stdin", stub):
                tree = sup._start(conn)
            tree.close()
            raw, payload = self._payload(conn)
        finally:
            conn.close()
        self.assertIs(payload["stdin_isatty"], True)
        self.assertIn('"stdin_isatty":true', raw)


if __name__ == "__main__":
    unittest.main()
