#!/usr/bin/env python3
"""coop_watcher - deterministic, content-free wake watcher (NO LLM).

An interactive agent cannot "monitor the board
between turns": between turns it is *suspended* (blocked on stdin), not a
running loop, and a within-turn `while sleep; poll` loop just blocks the agent
dumbly and burns tokens. So the wake must be EXTERNAL deterministic code that
watches the board and wakes an idle agent when it has actionable work -
carrying NO coordination content (the wake says only "you have work:
<category>"; every contract / handoff / review / decision stays on the board).

This reuses coopdb._derive_next_action - the exact executable action object the
`status` command already ships. `kind=idle` means nothing to do; every other
kind wakes the agent. The wake emits only the kind, while target/argv remain on
the board for the bound turn to read.

    python -m agent_coop.coop_watcher --as claude --db board.db  # block until actionable, print hint
    python -m agent_coop.coop_watcher --as claude --once         # check once (exit 0 actionable / 3 idle)
    python -m agent_coop.coop_watcher --selftest                 # runnable check (no board needed)

Why content-free matters: it is what keeps the board the operative channel
rather than a mirror of coordination happening in an orchestrator's injected
prompts. A wake that carries only a *category* (not the contract/handoff
body) cannot stand in for a protocol object - the agent still has to read the
board for every operative fact.
"""
import argparse
import time

from agent_coop import coopdb

IDLE = "idle"


def next_action(db_path, agent,
                lease_seconds=coopdb.DEFAULT_ACTION_LEASE_SECONDS,
                item_id=None):
    """Return the full executable next-action envelope for internal use."""
    conn = coopdb.connect(db_path)
    try:
        action, _warnings = coopdb._derive_next_action(
            conn, agent, coopdb.now(), lease_seconds=lease_seconds,
            item_id=item_id)
        return action
    finally:
        conn.close()


def actionable(db_path, agent, lease_seconds=coopdb.DEFAULT_ACTION_LEASE_SECONDS,
               item_id=None):
    """Current content-free next-action kind (IDLE if nothing)."""
    return next_action(
        db_path,
        agent,
        lease_seconds=lease_seconds,
        item_id=item_id,
    )["kind"]


def wait_for_action(check, *, interval=2.0, timeout=None,
                    sleep=time.sleep, clock=time.monotonic):
    """Poll check() until it returns a non-idle hint, or timeout elapses.

    check() -> hint str. Returns the actionable hint, or None on timeout.
    Pure loop (check/sleep/clock injected) so it is testable without a board.
    """
    start = clock()
    while True:
        hint = check()
        if hint and hint != IDLE:
            return hint
        if timeout is not None and (clock() - start) >= timeout:
            return None
        sleep(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description="content-free board wake watcher")
    ap.add_argument("--as", dest="agent", help="agent id to watch")
    ap.add_argument("--db", default="board.db")
    ap.add_argument("--item", type=int, default=None,
                    help="limit actionable work to one item")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="poll interval seconds (default 2)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="give up after N seconds idle (default: wait forever)")
    ap.add_argument("--once", action="store_true",
                    help="check once and exit (0 actionable / 3 idle)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        selftest()
        print("coop_watcher selftest: OK")
        return 0
    if not args.agent:
        ap.error("--as <agent> is required")

    if args.item is not None and args.item <= 0:
        ap.error("--item must be a positive integer")
    check = lambda: actionable(  # noqa: E731
        args.db, args.agent, item_id=args.item)
    if args.once:
        hint = check()
        print(hint)
        return 0 if hint != IDLE else 3
    hint = wait_for_action(check, interval=args.interval, timeout=args.timeout)
    if hint is None:
        print(IDLE)
        return 2  # timed out still idle
    print(hint)  # content-free: a category, never the board's content
    return 0


def selftest():
    # 1. blocks while idle, returns on the first actionable poll
    seq = iter(["idle", "idle", "respond_handoff", "idle"])
    slept = []
    hint = wait_for_action(lambda: next(seq), interval=0.0,
                           sleep=lambda s: slept.append(s), clock=lambda: 0.0)
    assert hint == "respond_handoff", hint
    assert len(slept) == 2, slept  # two idle polls, then woke on the third

    # 2. timeout returns None when the agent never has work
    ticks = [0.0]

    def clk():
        ticks[0] += 5.0
        return ticks[0]

    out = wait_for_action(lambda: "idle", interval=0.0, timeout=3.0,
                          sleep=lambda s: None, clock=clk)
    assert out is None, out


if __name__ == "__main__":
    raise SystemExit(main())
