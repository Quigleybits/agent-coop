"""Windows launch-gate bootstrap for Agent Co-op.

Runs as a supervised child, never imported by coop modules. The supervisor
assigns THIS process to the Job Object, inserts the session row, and only
then signals the gate event; we launch the exact opaque argv (inherited
stdio/env/cwd), signal the ack event, and mirror the harness's exit code.

Fail-closed contract: any failure before the opaque command starts means we
exit WITHOUT launching. The launched-acknowledgement — not our exit code —
is what tells the supervisor the harness started; exit codes are never used
to classify launch failure (an opaque child may legitimately exit 3).

Usage: coop_bootstrap.py <gate-name> <ack-name> <timeout-s> -- <argv...>
"""

import ctypes
import signal
import subprocess
import sys

FAIL_CLOSED_EXIT = 3

SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0x00000000
INFINITE_GUARD_MS = 0x7FFFFFFF


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def main(argv):
    try:
        sep = argv.index("--")
    except ValueError:
        return FAIL_CLOSED_EXIT
    gate_name, ack_name, timeout_s = argv[:sep]
    opaque = argv[sep + 1 :]
    if not opaque:
        return FAIL_CLOSED_EXIT

    # Survive the console break aimed at the harness: the supervisor signals
    # our process group so the harness sees CTRL_BREAK; we must live on to
    # mirror its exit code.
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)

    k32 = _kernel32()
    k32.OpenEventW.restype = ctypes.c_void_p
    gate = k32.OpenEventW(SYNCHRONIZE, False, gate_name)
    if not gate:
        return FAIL_CLOSED_EXIT
    timeout_ms = min(int(float(timeout_s) * 1000), INFINITE_GUARD_MS)
    waited = k32.WaitForSingleObject(ctypes.c_void_p(gate), timeout_ms)
    k32.CloseHandle(ctypes.c_void_p(gate))
    if waited != WAIT_OBJECT_0:
        return FAIL_CLOSED_EXIT  # gate never opened — never launch

    try:
        child = subprocess.Popen(
            opaque, shell=False,
            stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return FAIL_CLOSED_EXIT  # no ack was sent — release() sees LaunchFailed

    ack = k32.OpenEventW(EVENT_MODIFY_STATE, False, ack_name)
    if ack:
        k32.SetEvent(ctypes.c_void_p(ack))
        k32.CloseHandle(ctypes.c_void_p(ack))
    # A missing ack fails closed on the supervisor side (LaunchFailed +
    # job termination); nothing useful to do here either way.

    return child.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
