"""Process-tree ownership contract tests.

Real subprocesses, harmless `sys.executable -c` children only. These tests
are the oracle for the pinned platform decisions: prepare/release/abort
semantics, launch-failure classification at release() time (never by exit
code), Job Object ownership on Windows, process-group ownership on POSIX.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_coop import coop_process
from agent_coop import coop_bootstrap
from agent_coop import coop_start
from agent_coop.coop_capabilities import LaunchProfile
from agent_coop.coop_errors import LaunchFailed, ProcessTreeUnavailable

IS_WINDOWS = os.name == "nt"
IS_POSIX = os.name == "posix"

COOP_DIR = Path(__file__).resolve().parents[1]


def _poll_until(predicate, timeout_s=60.0, interval_s=0.05):
    """Poll until true. These tests spawn real interpreters, so the
    ceiling has to cover process creation on a loaded hosted runner.
    A predicate that becomes true returns at once, so a generous
    ceiling costs a healthy run nothing."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _sentinel_argv(sentinel):
    code = (
        "import pathlib,sys;"
        f"pathlib.Path({str(sentinel)!r}).write_text('ran');"
        "sys.exit(0)"
    )
    return [sys.executable, "-c", code]


def _exit_code_argv(code):
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def _spawn_grandchild_and_exit_argv():
    code = (
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        "sys.exit(0)"
    )
    return [sys.executable, "-c", code]


def _cooperative_child_argv():
    if IS_WINDOWS:
        code = (
            "import signal,sys,time\n"
            "signal.signal(signal.SIGBREAK, lambda *a: sys.exit(0))\n"
            "while True:\n"
            "    time.sleep(0.05)\n"
        )
    else:
        code = (
            "import signal,sys,time\n"
            "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
            "while True:\n"
            "    time.sleep(0.05)\n"
        )
    return [sys.executable, "-c", code]


def _stubborn_child_argv():
    if IS_WINDOWS:
        code = (
            "import signal,time\n"
            "signal.signal(signal.SIGBREAK, signal.SIG_IGN)\n"
            "while True:\n"
            "    time.sleep(0.05)\n"
        )
    else:
        code = (
            "import signal,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n"
            "    time.sleep(0.05)\n"
        )
    return [sys.executable, "-c", code]


class PreparedReleaseContract(unittest.TestCase):
    """Shared semantics: sentinel only after release(), abort never launches,
    LaunchFailed classification, exit-code neutrality."""

    def setUp(self):
        self.tmp = Path(self._make_tmpdir())
        self.trees = []

    def _make_tmpdir(self):
        import tempfile

        d = tempfile.mkdtemp(prefix="coop-tree-")
        self.addCleanup(lambda: None)
        return d

    def tearDown(self):
        for tree in self.trees:
            try:
                tree.close()
            except Exception:
                pass

    def _prepare(self, argv, **kw):
        kw.setdefault("session_id", uuid.uuid4().hex)
        return coop_process.prepare_tree(argv, **kw)

    def test_release_starts_the_opaque_command(self):
        sentinel = self.tmp / "started.txt"
        prepared = self._prepare(_sentinel_argv(sentinel))
        try:
            time.sleep(0.5)
            self.assertFalse(
                sentinel.exists(),
                "opaque command ran before release() — the gate is broken",
            )
        except Exception:
            prepared.abort()
            raise
        tree = prepared.release()
        self.trees.append(tree)
        self.assertTrue(
            _poll_until(sentinel.exists),
            "opaque command did not run after release()",
        )
        self.assertTrue(_poll_until(lambda: tree.poll_root() is not None))
        self.assertEqual(tree.poll_root(), 0)

    def test_release_can_capture_opaque_stdout(self):
        with tempfile.TemporaryFile() as stdout:
            prepared = self._prepare(
                [sys.executable, "-c", "print('owned-output')"],
                stdout=stdout,
            )
            tree = prepared.release()
            self.trees.append(tree)
            self.assertTrue(_poll_until(lambda: tree.poll_root() is not None))
            tree.close()
            stdout.seek(0)
            self.assertIn(b"owned-output", stdout.read())

    def test_released_tree_exposes_owned_protocol_streams(self):
        prepared = self._prepare(
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import sys;"
                    "line=sys.stdin.buffer.readline();"
                    "sys.stdout.buffer.write(b'ack:'+line);"
                    "sys.stdout.buffer.flush()"
                ),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tree = prepared.release()
        self.trees.append(tree)

        self.assertIsNotNone(tree.stdin)
        self.assertIsNotNone(tree.stdout)
        self.assertIsNotNone(tree.stderr)
        tree.stdin.write(b"hello\n")
        tree.stdin.flush()
        self.assertEqual(tree.stdout.readline(), b"ack:hello\n")
        self.assertTrue(_poll_until(lambda: tree.poll_root() is not None))

    def test_unreleased_prepare_never_launches(self):
        sentinel = self.tmp / "never.txt"
        prepared = self._prepare(
            _sentinel_argv(sentinel), gate_timeout_s=1
        )
        time.sleep(2.5)
        self.assertFalse(
            sentinel.exists(), "opaque command ran without release()"
        )
        prepared.abort()
        time.sleep(0.3)
        self.assertFalse(sentinel.exists())

    def test_abort_never_launches(self):
        sentinel = self.tmp / "aborted.txt"
        prepared = self._prepare(_sentinel_argv(sentinel))
        prepared.abort()
        time.sleep(0.8)
        self.assertFalse(
            sentinel.exists(), "abort() must prevent the launch entirely"
        )

    def test_release_of_unrunnable_argv_raises_launch_failed(self):
        bogus = str(self.tmp / "definitely-not-a-real-binary")
        prepared = self._prepare([bogus], gate_timeout_s=5)
        with self.assertRaises(LaunchFailed):
            tree = prepared.release()
            self.trees.append(tree)

    def test_child_exit_code_3_is_not_launch_failure(self):
        prepared = self._prepare(_exit_code_argv(3))
        tree = prepared.release()  # must NOT raise — launch succeeded
        self.trees.append(tree)
        self.assertTrue(_poll_until(lambda: tree.poll_root() is not None))
        self.assertEqual(
            tree.poll_root(),
            3,
            "a launched child's own exit code must pass through untouched",
        )

    def test_root_exit_leaves_grandchild_owned(self):
        prepared = self._prepare(_spawn_grandchild_and_exit_argv())
        tree = prepared.release()
        self.trees.append(tree)
        self.assertTrue(_poll_until(lambda: tree.poll_root() is not None))
        self.assertFalse(
            tree.is_empty(),
            "grandchild must remain owned by the tree after root exit",
        )
        tree.close()
        self.assertTrue(
            _poll_until(tree.is_empty),
            "close() must terminate every owned descendant",
        )

    def test_turn_profile_cleanup_follows_owned_descendant_drain(self):
        cleanup_calls = []

        def profile_builder(_provider, _base_argv, _manifest, **kwargs):
            return LaunchProfile(
                argv=_spawn_grandchild_and_exit_argv(),
                env=kwargs["env"],
                external_server_names=(),
                cleanup=lambda: cleanup_calls.append("cleanup"),
            )

        with unittest.mock.patch.object(
                coop_start,
                "build_launch_profile",
                side_effect=profile_builder,
        ):
            result = coop_start.invoke_turn(
                provider="claude",
                prompt="ignored-extra-argv",
                session_id="owned-cleanup",
                agent_id="claude",
                board_path=self.tmp / "board.db",
                cwd=self.tmp,
                # Generous: this test is about cleanup ordering after a
                # descendant drain, not about the turn budget. At 10s a
                # slow Windows runner exhausted it and reported only
                # ok=False, which is indistinguishable from a real fault.
                timeout_s=120,
                resolve=lambda _name: sys.executable,
            )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["tree_empty"], result)
        self.assertEqual(cleanup_calls, ["cleanup"], result)

    def test_force_stop_empties_tree(self):
        prepared = self._prepare(_stubborn_child_argv())
        tree = prepared.release()
        self.trees.append(tree)
        time.sleep(0.4)
        self.assertFalse(tree.is_empty())
        tree.force_stop()
        self.assertTrue(_poll_until(tree.is_empty))

    def test_graceful_stop_cooperative_child(self):
        prepared = self._prepare(_cooperative_child_argv())
        tree = prepared.release()
        self.trees.append(tree)
        time.sleep(0.6)  # let the child install its handler
        delivered = tree.graceful_stop()
        if not delivered:
            self.skipTest(
                "console signal could not be delivered in this environment"
            )
        self.assertTrue(
            _poll_until(tree.is_empty, timeout_s=60.0),
            "cooperative child should exit on the graceful signal alone",
        )
        self.assertEqual(
            tree.poll_root(),
            0,
            "cooperative child must exit cleanly via its handler",
        )

    def test_grace_expiry_forces_stubborn_child(self):
        prepared = self._prepare(_stubborn_child_argv())
        tree = prepared.release()
        self.trees.append(tree)
        time.sleep(0.6)
        tree.graceful_stop()
        time.sleep(1.0)  # the grace interval — child ignores the signal
        self.assertFalse(
            tree.is_empty(), "stubborn child must survive the graceful signal"
        )
        tree.force_stop()
        self.assertTrue(_poll_until(tree.is_empty))


@unittest.skipUnless(IS_WINDOWS, "Windows Job Object contract")
class WindowsJobContract(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="coop-win-"))

    def test_event_name_collision_fails_closed(self):
        session_id = uuid.uuid4().hex
        gate_name = f"coop-gate-{session_id}"
        handle = coop_process._create_event(gate_name)  # occupy the name
        try:
            with self.assertRaises(ProcessTreeUnavailable):
                coop_process.prepare_tree(
                    _exit_code_argv(0), session_id=session_id
                )
        finally:
            coop_process._close_handle(handle)

    def test_assignment_failure_refuses_launch(self):
        sentinel = self.tmp / "assign-fail.txt"
        with unittest.mock.patch.object(
            coop_process, "_assign_to_job", side_effect=OSError("forced")
        ):
            with self.assertRaises(ProcessTreeUnavailable):
                coop_process.prepare_tree(
                    _sentinel_argv(sentinel), session_id=uuid.uuid4().hex
                )
        time.sleep(0.8)
        self.assertFalse(
            sentinel.exists(),
            "assignment failure must terminate the bootstrap unlaunched",
        )

    def test_bootstrap_uses_hidden_console_process_group(self):
        class FakeProcess:
            _handle = 123

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        with unittest.mock.patch.object(
                coop_process, "_create_event", side_effect=[11, 12]), \
             unittest.mock.patch.object(
                 coop_process, "_create_kill_on_close_job", return_value=13), \
             unittest.mock.patch.object(coop_process, "_assign_to_job"), \
             unittest.mock.patch.object(coop_process, "_terminate_job"), \
             unittest.mock.patch.object(coop_process, "_close_handle"), \
             unittest.mock.patch.object(coop_process, "_close_job"), \
             unittest.mock.patch.object(
                 coop_process.subprocess, "Popen", return_value=FakeProcess()) as popen:
            prepared = coop_process.prepare_tree(
                _exit_code_argv(0), session_id=uuid.uuid4().hex)
            flags = popen.call_args.kwargs["creationflags"]
            prepared.abort()
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)

    def test_provider_child_explicitly_inherits_headless_launch(self):
        kernel32 = unittest.mock.MagicMock()
        kernel32.OpenEventW.side_effect = [11, 12]
        kernel32.WaitForSingleObject.return_value = (
            coop_bootstrap.WAIT_OBJECT_0)
        child = unittest.mock.MagicMock()
        child.wait.return_value = 0
        with unittest.mock.patch.object(
                coop_bootstrap, "_kernel32", return_value=kernel32), \
             unittest.mock.patch.object(coop_bootstrap.signal, "signal"), \
             unittest.mock.patch.object(
                 coop_bootstrap.subprocess, "Popen",
                 return_value=child) as popen:
            code = coop_bootstrap.main([
                "gate", "ack", "1", "--", "provider.cmd"])
        self.assertEqual(code, 0)
        self.assertTrue(
            popen.call_args.kwargs["creationflags"]
            & subprocess.CREATE_NO_WINDOW)

    def test_nested_job_assignment_works_here(self):
        """Prove nested-Job support on the supported machine: a child that is
        already inside a Job can itself prepare/release an owned tree."""
        sentinel = self.tmp / "nested.txt"
        outer = coop_process._create_kill_on_close_job()
        try:
            harness_code = (
                f"import pathlib; pathlib.Path({str(sentinel)!r})"
                ".write_text('ran')"
            )
            script = (
                "import sys, time, uuid\n"
                f"sys.path.insert(0, {str(COOP_DIR)!r})\n"
                "from agent_coop import coop_process\n"
                "prepared = coop_process.prepare_tree(\n"
                f"    [sys.executable, '-c', {harness_code!r}],\n"
                "    session_id=uuid.uuid4().hex)\n"
                "tree = prepared.release()\n"
                "while tree.poll_root() is None: time.sleep(0.01)\n"
                "tree.close()\n"
                "print('NESTED-OK')\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            coop_process._assign_to_job(outer, child._handle)
            out, err = child.communicate(timeout=60)
            self.assertEqual(
                child.returncode, 0, f"nested prepare/release failed: {err}"
            )
            self.assertIn("NESTED-OK", out)
            self.assertTrue(sentinel.exists())
        finally:
            coop_process._close_job(outer)


class OneShotSemantics(unittest.TestCase):
    def test_release_after_abort_is_an_error(self):
        prepared = coop_process.prepare_tree(
            _exit_code_argv(0), session_id=uuid.uuid4().hex
        )
        prepared.abort()
        with self.assertRaises(RuntimeError):
            prepared.release()

    def test_double_release_is_an_error(self):
        prepared = coop_process.prepare_tree(
            _exit_code_argv(0), session_id=uuid.uuid4().hex
        )
        tree = prepared.release()
        try:
            with self.assertRaises(RuntimeError):
                prepared.release()
        finally:
            tree.close()


if __name__ == "__main__":
    unittest.main()
