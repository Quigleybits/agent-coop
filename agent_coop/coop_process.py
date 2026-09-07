"""Process-tree ownership for Agent Co-op.

One contract, two platforms:

    prepared = prepare_tree(argv, session_id=..., cwd=..., env=...)
    # ... insert the session row ...
    tree = prepared.release()   # starts the opaque command under ownership
    prepared.abort()            # OR: abandon it — the harness never runs

Launch failure is classified at release() time (LaunchFailed), never by
exit code: an opaque child that legitimately exits 3 is a clean root exit.

POSIX owns the tree with a process group created at spawn, so the spawn is
simply deferred to release() — no gate machinery exists or is needed.

Windows owns the tree with a Job Object (kill-on-close). Because a process
cannot be created inside a Job atomically, prepare() spawns the gated
coop_bootstrap.py, assigns IT to the Job, and release() signals the gate;
the bootstrap launches the exact opaque argv and signals the ack event.
A pre-existing gate/ack name fails closed. Emptiness is read from
JobObjectBasicAccountingInformation.ActiveProcesses — a fixed-size struct,
deliberately not the variable-length PID list.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from agent_coop.coop_errors import LaunchFailed, ProcessTreeUnavailable

_BOOTSTRAP = Path(__file__).resolve().with_name("coop_bootstrap.py")

# ---------------------------------------------------------------------------
# Windows Job Object plumbing (ctypes; loaded lazily so POSIX never touches it)
# ---------------------------------------------------------------------------

if os.name == "nt":
    import ctypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_LIMIT_KILL_ON_CLOSE = 0x2000
    _JobObjectBasicAccountingInformation = 1
    _JobObjectExtendedLimitInformation = 9
    _ERROR_ALREADY_EXISTS = 183
    _CTRL_BREAK_EVENT = 1
    _WAIT_OBJECT_0 = 0

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", ctypes.c_uint32),
            ("TotalProcesses", ctypes.c_uint32),
            ("ActiveProcesses", ctypes.c_uint32),
            ("TotalTerminatedProcesses", ctypes.c_uint32),
        ]

    _k32.CreateJobObjectW.restype = ctypes.c_void_p
    _k32.CreateEventW.restype = ctypes.c_void_p

    def _create_kill_on_close_job():
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            raise ProcessTreeUnavailable(
                f"CreateJobObjectW failed: {ctypes.get_last_error()}"
            )
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_CLOSE
        ok = _k32.SetInformationJobObject(
            ctypes.c_void_p(job),
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.get_last_error()
            _k32.CloseHandle(ctypes.c_void_p(job))
            raise ProcessTreeUnavailable(
                f"SetInformationJobObject failed: {err}"
            )
        return job

    def _assign_to_job(job, process_handle):
        ok = _k32.AssignProcessToJobObject(
            ctypes.c_void_p(job), ctypes.c_void_p(int(process_handle))
        )
        if not ok:
            raise OSError(
                f"AssignProcessToJobObject failed: {ctypes.get_last_error()}"
            )

    def _terminate_job(job):
        _k32.TerminateJobObject(ctypes.c_void_p(job), 1)

    def _close_job(job):
        _terminate_job(job)
        _k32.CloseHandle(ctypes.c_void_p(job))

    def _close_job_handle(job):
        _k32.CloseHandle(ctypes.c_void_p(job))

    def _job_active_processes(job):
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        ok = _k32.QueryInformationJobObject(
            ctypes.c_void_p(job),
            _JobObjectBasicAccountingInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not ok:
            raise ProcessTreeUnavailable(
                f"QueryInformationJobObject failed: {ctypes.get_last_error()}"
            )
        return info.ActiveProcesses

    def _create_event(name):
        handle = _k32.CreateEventW(None, True, False, name)
        err = ctypes.get_last_error()
        if not handle:
            raise ProcessTreeUnavailable(f"CreateEventW failed: {err}")
        if err == _ERROR_ALREADY_EXISTS:
            _k32.CloseHandle(ctypes.c_void_p(handle))
            raise ProcessTreeUnavailable(
                f"event name already exists (refused, fail closed): {name}"
            )
        return handle

    def _set_event(handle):
        return bool(_k32.SetEvent(ctypes.c_void_p(handle)))

    def _event_is_set(handle):
        return _k32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == _WAIT_OBJECT_0

    def _close_handle(handle):
        _k32.CloseHandle(ctypes.c_void_p(handle))

    def _send_console_break(group_pid):
        return bool(_k32.GenerateConsoleCtrlEvent(_CTRL_BREAK_EVENT, group_pid))


# ---------------------------------------------------------------------------
# Shared contract
# ---------------------------------------------------------------------------


class ProcessTree:
    """A running, owned process tree. Platform subclasses implement the
    five operations; the supervisor owns the graceful→grace→force ladder."""

    pid = None

    @property
    def stdin(self):
        return self._proc.stdin

    @property
    def stdout(self):
        return self._proc.stdout

    @property
    def stderr(self):
        return self._proc.stderr

    def poll_root(self):
        raise NotImplementedError

    def graceful_stop(self):
        raise NotImplementedError

    def force_stop(self):
        raise NotImplementedError

    def is_empty(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class _PreparedBase:
    def __init__(self):
        self._consumed = None  # None | "released" | "aborted"

    def _consume(self, how):
        if self._consumed is not None:
            raise RuntimeError(
                f"PreparedTree already {self._consumed}; "
                "release()/abort() are one-shot"
            )
        self._consumed = how


# ---------------------------------------------------------------------------
# POSIX: deferred spawn — release() IS the Popen(start_new_session=True)
# ---------------------------------------------------------------------------


class _PosixPrepared(_PreparedBase):
    def __init__(self, argv, cwd, env, stdin, stdout, stderr):
        super().__init__()
        self._argv, self._cwd, self._env = argv, cwd, env
        self._stdin, self._stdout, self._stderr = stdin, stdout, stderr

    def release(self):
        self._consume("released")
        try:
            proc = subprocess.Popen(
                self._argv,
                shell=False,
                start_new_session=True,
                cwd=self._cwd,
                env=self._env,
                stdin=self._stdin,
                stdout=self._stdout,
                stderr=self._stderr,
            )
        except OSError as exc:
            raise LaunchFailed(f"could not start {self._argv[0]!r}: {exc}")
        return _PosixTree(proc)

    def abort(self):
        self._consume("aborted")  # nothing was spawned; nothing to kill


class _PosixTree(ProcessTree):
    def __init__(self, proc):
        self._proc = proc
        self.pid = proc.pid
        self._pgid = proc.pid  # start_new_session=True: pgid == pid

    def poll_root(self):
        return self._proc.poll()

    def graceful_stop(self):
        try:
            os.killpg(self._pgid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return True  # nothing left to stop
        except PermissionError:
            return False

    def force_stop(self):
        try:
            os.killpg(self._pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._proc.poll()

    def is_empty(self):
        self._proc.poll()  # reap a zombie root so it stops holding the group
        try:
            os.killpg(self._pgid, 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False  # someone in the group is alive (not ours to signal)

    def close(self):
        self.force_stop()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # Waiting on the root only reaps the root. SIGKILL is asynchronous and
        # an owned descendant that outlives the root is reparented away from
        # us, so nothing here reaps it — it stays in the process group for a
        # few more milliseconds. Drain the group before returning, the same
        # contract _WindowsTree.close() gets from its Job Object: when close()
        # returns, the tree is empty.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.is_empty():
                break
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# Windows: Job Object + gated bootstrap + launched-acknowledgement
# ---------------------------------------------------------------------------


class _WindowsPrepared(_PreparedBase):
    def __init__(self, argv, cwd, env, session_id, gate_timeout_s,
                 stdin, stdout, stderr):
        super().__init__()
        self._timeout_s = gate_timeout_s
        gate_name = f"coop-gate-{session_id}"
        ack_name = f"coop-ack-{session_id}"

        self._gate = _create_event(gate_name)
        try:
            self._ack = _create_event(ack_name)
        except ProcessTreeUnavailable:
            _close_handle(self._gate)
            raise
        try:
            self._job = _create_kill_on_close_job()
        except ProcessTreeUnavailable:
            self._close_events()
            raise

        try:
            self._proc = subprocess.Popen(
                [
                    sys.executable,
                    str(_BOOTSTRAP),
                    gate_name,
                    ack_name,
                    str(gate_timeout_s),
                    "--",
                    *argv,
                ],
                shell=False,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ),
            )
        except OSError as exc:
            self._close_all()
            raise ProcessTreeUnavailable(f"could not spawn bootstrap: {exc}")

        try:
            _assign_to_job(self._job, self._proc._handle)
        except OSError as exc:
            self._proc.terminate()
            self._proc.wait(timeout=5)
            self._close_all()
            raise ProcessTreeUnavailable(
                f"Job assignment failed; refusing an unowned launch: {exc}"
            )

    def _close_events(self):
        _close_handle(self._gate)
        _close_handle(self._ack)

    def _close_all(self):
        self._close_events()
        _close_job(self._job)

    def release(self):
        self._consume("released")
        if not _set_event(self._gate):
            self._teardown()
            raise LaunchFailed("could not signal the launch gate")
        deadline = time.monotonic() + self._timeout_s
        while time.monotonic() < deadline:
            if _event_is_set(self._ack):
                self._close_events()
                return _WindowsTree(self._job, self._proc)
            if self._proc.poll() is not None:
                break  # bootstrap died without acking — launch never happened
            time.sleep(0.05)
        self._teardown()
        raise LaunchFailed(
            "the opaque command did not start (no launch acknowledgement)"
        )

    def abort(self):
        self._consume("aborted")
        self._teardown()

    def _teardown(self):
        _terminate_job(self._job)
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self._close_all()


class _WindowsTree(ProcessTree):
    # `pid` is the bootstrap's — the signalable group root that mirrors the
    # harness's exit code. The opaque command's own pid is deliberately not
    # surfaced; the Job, not a pid, is the ownership identity.
    def __init__(self, job, bootstrap_proc):
        self._job = job
        self._proc = bootstrap_proc
        self.pid = bootstrap_proc.pid
        self._closed = False

    def poll_root(self):
        return self._proc.poll()

    def graceful_stop(self):
        # CREATE_NO_WINDOW is the headless product contract. It deliberately
        # removes the shared console GenerateConsoleCtrlEvent needs, so do not
        # claim a CTRL_BREAK was delivered. The supervisor's bounded grace then
        # falls through to Job Object termination, which still drains the tree.
        return False

    def force_stop(self):
        _terminate_job(self._job)

    def is_empty(self):
        if self._closed:
            return True  # close() verified the job drained before returning
        return _job_active_processes(self._job) == 0

    def close(self):
        if self._closed:
            return
        _terminate_job(self._job)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if _job_active_processes(self._job) == 0:
                    break
            except ProcessTreeUnavailable:
                break
            time.sleep(0.05)
        self._closed = True
        _close_job_handle(self._job)  # kill-on-close backs any straggler
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def prepare_tree(argv, *, session_id, cwd=None, env=None, gate_timeout_s=30,
                 stdin=None, stdout=None, stderr=None):
    """Prepare platform process-tree ownership WITHOUT running the opaque
    command. The caller inserts its session row between prepare and
    release() — abort() abandons the prepared state unlaunched."""
    if not argv:
        raise ProcessTreeUnavailable("empty argv")
    if os.name == "nt":
        return _WindowsPrepared(
            list(argv), cwd, env, session_id, gate_timeout_s,
            stdin, stdout, stderr)
    return _PosixPrepared(list(argv), cwd, env, stdin, stdout, stderr)
