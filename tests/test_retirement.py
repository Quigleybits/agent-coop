"""The pre-release mutation surface is retired on version 4 boards.

The v4 binary operates only on v4 boards, so the pre-release protocol does not
need to keep working — it needs to stop existing as a mutation surface.
Reads over legacy history survive."""

import argparse
import contextlib
import pathlib
import sqlite3
import subprocess
import sys
import unittest

from agent_coop import cli as coopcli
from agent_coop import coopdb
from tests.test_schema_migration import V3Board

COOP_DIR = pathlib.Path(__file__).resolve().parents[1]

SURVIVING_COMMANDS = {
    "init", "migrate", "say", "inbox", "status", "agents", "monitor",
    "item", "queue",  # full-contract item family + deterministic queue
    "claim", "admin",  # voluntary release + operator recovery lanes
    "checkpoint",  # claim-bound, packet-returning (not message-kind)
    "needs-input", "question",  # exact questions + addressed answers
    "session",  # the sole supported supervised launch path
    "smoke",  # offline preflight (temp board, tree, CLIs)
    "herdr",  # optional read-only trace mirror; no board mutation
    "envelope",  # read-only projection over events
    "receipt",  # hashed receipts (deliberate extension)
    "review",   # claim-bound request/claim replace the
                # retired pre-release review commands (deliberate extension)
    "decision",  # claim-bound append-only record replaces
                 # the retired debate-driven pre-release (deliberate extension)
    "handoff",  # structured create/accept/decline —
                # transfer only on acceptance (deliberate extension)
    "huddle",  # bounded peer deliberation; legacy open-form debate stays retired
    "board", "tasks", "task",  # operator read-only views (`task` is the
                               # friendly alias of `tasks`)
    "start",  # convene the agents through the canonical wake-driven runtime
    "recover",  # agent wedge release + mid-run human audit (product recovery)
}
RETIRED_COMMANDS = ("watch", "assign", "debate")
# create_item is not listed: the keyword-only full-contract
# version replaced the pre-release build's (conn, title, created_by, body) form.
# claim_item is not listed: the keyword-only fenced-lane claim replaced
# the pre-release build's assignment-based (conn, item_id, agent) form.
# checkpoint is not listed: the claim-bound guarded form replaced the
# pre-release build's message-kind (conn, agent, ctype, item_id, note) form.
RETIRED_CALLABLES = (
    "update_item",
    "done_item",
    "claim_next_item",
    "assign_item",
    # request_review is not listed — it is live again
    # as the claim-bound, receipt-requiring form (the pre-release build's
    # assignment-shaped version stays gone). record_decision is not listed
    # for the same reason (claim-bound, append-only; the debate-driven
    # pre-release form stays gone). submit_review is not listed: the
    # claim-bound `submit_verdict` (watermarked, derivation-checked)
    # replaced the pre-release build's assignment-shaped verdict for good.
    "item_has_open_review",
    "start_debate",
    "reply_debate",
    "close_debate",
)


def run_coop(*args):
    return subprocess.run(
        [sys.executable, "coop.py", *(str(arg) for arg in args)],
        cwd=COOP_DIR,
        capture_output=True,
        text=True,
    )


class TestParserSurface(unittest.TestCase):
    def _choices(self):
        parser = coopcli.build_parser()
        return set(
            next(
                action.choices
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            )
        )

    def test_surviving_command_set_is_exact(self):
        self.assertEqual(self._choices(), SURVIVING_COMMANDS)

    def test_retired_subcommands_are_rejected_by_the_parser(self):
        for name in RETIRED_COMMANDS:
            with self.subTest(command=name):
                with self.assertRaises(SystemExit) as caught:
                    coopcli.build_parser().parse_args([name])
                self.assertEqual(caught.exception.code, 2)

    def test_retired_subcommand_exits_two_end_to_end(self):
        result = run_coop("assign", "1", "--to", "codex")
        self.assertEqual(result.returncode, 2)

    def test_help_omits_retired_commands(self):
        # checkpoint and decision are not listed — each is live again as
        # its claim-bound form; the golden RETIRED_COMMANDS tuple is the
        # single source of truth.
        help_text = coopcli.build_parser().format_help()
        for name in RETIRED_COMMANDS:
            self.assertNotIn(name, help_text)


class TestNoDoneWritePath(unittest.TestCase):
    def test_retired_callables_are_gone(self):
        for name in RETIRED_CALLABLES:
            with self.subTest(callable=name):
                self.assertFalse(
                    hasattr(coopdb, name),
                    f"coopdb.{name} must be retired on version 4",
                )

    def test_no_coopdb_source_writes_done_status(self):
        # The guarded completion transition now
        # exists, and it is the ONLY licensed `done` writer. Every
        # occurrence of a done-status write in coopdb.py must sit inside
        # the complete_item function body; any other writer fails here.
        source = (COOP_DIR / "agent_coop" / "coopdb.py").read_text(
            encoding="utf-8")
        start = source.index("\ndef complete_item(")
        tail = source[start + 1:]
        end = tail.index("\ndef ", tail.index("\n"))
        span = (start + 1, start + 1 + end)
        writes = []
        offset = 0
        while True:
            hit = source.find("status='done'", offset)
            if hit == -1:
                break
            writes.append(hit)
            offset = hit + 1
        self.assertTrue(
            writes,
            "complete_item must write items.status='done'; the guarded "
            "transition is licensed and expected",
        )
        for hit in writes:
            self.assertTrue(
                span[0] <= hit < span[1],
                "a done-status write exists outside complete_item; "
                "complete_item is the sole licensed writer",
            )


class TestLegacyHistoryStaysReadable(unittest.TestCase):
    def test_migrated_board_reads_and_status_renders(self):
        with V3Board() as board:
            coopdb.migrate_db(board.path, confirm_legacy_clients_stopped=True)
            result = run_coop("--db", board.path, "--as", "claude", "status")
            self.assertEqual(result.returncode, 0, result.stderr)
            # The boot panel: the migrated legacy item renders in its
            # owner's owned-work section.
            self.assertIn("next:", result.stdout)
            self.assertIn("Blocked on a question", result.stdout)
            with contextlib.closing(sqlite3.connect(board.path)) as conn:
                self.assertEqual(
                    conn.execute("SELECT topic FROM debates").fetchone()[0],
                    "Topic",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT body FROM messages WHERE from_agent='claude'"
                    ).fetchone()[0],
                    "hello",
                )


class TestRunnerDeleted(unittest.TestCase):
    def test_provider_specific_runner_is_gone(self):
        self.assertFalse(
            (COOP_DIR / "runners" / "codex.sh").exists(),
            "runners/codex.sh violates the sole-launch-path decision",
        )


if __name__ == "__main__":
    unittest.main()
