"""Adversarial trial — failure class: raw ``status=done`` completion bypass.

Three legs. (a) CLI surface enumeration: every plausible raw-status
mutator spelling is rejected by argparse with zero board mutation —
the bypass has no surface at all. (b) Source path enumeration ("grep
every writer to status='done'"): the ONLY writer of
``items.status='done'`` in the package is the guarded transaction inside
``coopdb.complete_item``; parameterized and dynamic item-status writers
are pinned so any new write path lands red. (c) End-to-end bypass via
the real CLI: ``item complete`` without a receipt, then with a receipt
but no review, both refuse with typed errors and leave the board
byte-identical.

Fixtures: tests/test_completion.py CompletionBoard (board + CLI ``_run``)
and tests/test_reviews.py ``snap`` full-table snapshots.
"""

import json
import pathlib
import re
import unittest

from agent_coop import coopdb
from tests.test_completion import CompletionBoard
from tests.test_reviews import snap

PACKAGE_DIR = pathlib.Path(coopdb.__file__).resolve().parent

# A literal SQL assignment of done — Python comparisons (`== "done"`)
# do not match because `==` breaks the assignment shape.
DONE_ASSIGNMENT = re.compile(r"status\s*=\s*['\"]done['\"]")


def _item_status_writers():
    """Every ``UPDATE items SET`` in the package that touches ``status``.

    Returns (file, enclosing top-level function, statuses, dynamic) per
    statement, where statuses are the literal or ``?`` assignments found
    between SET and WHERE, and dynamic marks f-string ``{assignments}``
    writers.
    """
    writers = []
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        def_positions = [
            (m.start(), m.group(1))
            for m in re.finditer(r"^def (\w+)", text, re.MULTILINE)
        ]
        for match in re.finditer(r"UPDATE items SET", text):
            end = text.find("WHERE", match.end())
            if end == -1:
                end = match.end() + 400
            window = text[match.end():end]
            enclosing = None
            for pos, name in def_positions:
                if pos < match.start():
                    enclosing = name
                else:
                    break
            writers.append({
                "file": path.name,
                "fn": enclosing,
                "statuses": re.findall(
                    r"status\s*=\s*(\?|'[a-z_]+')", window),
                "dynamic": "{assignments}" in window,
            })
    return writers


class RawStatusCliSurface(CompletionBoard):
    """Leg (a): no CLI spelling can mutate item status raw."""

    def test_every_raw_status_spelling_is_rejected_with_no_board_write(self):
        item, sid, cid = self.make_working()
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}
        before = snap(self.conn)
        spellings = [
            ["item", "update", str(item), "--status", "done"],
            ["item", "update", "--id", str(item), "--status", "done"],
            ["item", "set-status", str(item), "done"],
            ["item", "status", str(item), "--status", "done"],
            ["item", "done", str(item)],
            ["item", "edit", str(item), "--status", "done"],
            ["item", "show", str(item), "--status", "done"],
            ["item", "revise", str(item), "--reason", "r",
             "--status", "done"],
            ["item", "claim", str(item), "--intent", "i",
             "--status", "done"],
            ["item", "complete", "--claim", str(cid), "--status", "done"],
            ["item", "create", "--title", "t", "--status", "done"],
            ["update", "item", str(item), "--status", "done"],
        ]
        for argv in spellings:
            code, out, err = self._run(argv, env)
            self.assertEqual(
                code, 2,
                f"argparse must reject {argv!r}; got exit {code}: "
                f"{err or out}")
        # Zero board mutation across every rejected spelling.
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(self.item_row(item)["status"], "working")
        self.assertEqual(self.events(item, "item_completed"), [])


class DoneWriterPathEnumeration(unittest.TestCase):
    """Leg (b): exhaustive writer enumeration, red on new paths."""

    def test_only_complete_item_writes_status_done(self):
        hits = []
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in DONE_ASSIGNMENT.finditer(text):
                hits.append((path.name, match.start()))
        self.assertEqual(
            [name for name, _ in hits], ["coopdb.py"],
            f"new status='done' writer(s) landed: {hits} — every new "
            "write path needs a red adversarial test first")
        text = (PACKAGE_DIR / "coopdb.py").read_text(encoding="utf-8")
        offset = hits[0][1]
        # The single assignment is the guarded UPDATE on items ...
        line = text[text.rfind("\n", 0, offset) + 1:
                    text.index("\n", offset)]
        self.assertIn("UPDATE items SET status='done'", line)
        # ... inside complete_item's transaction, not anywhere else.
        fn_start = text.index("\ndef complete_item(")
        next_def = text.index("\ndef ", fn_start + 1)
        self.assertTrue(
            fn_start < offset < next_def,
            "the status='done' writer moved outside complete_item")

    def test_non_literal_item_status_writers_are_pinned(self):
        writers = _item_status_writers()
        # Parameterized status writes: only the two state-machine moves
        # whose ternaries cannot yield 'done' (claim re-arm, human revise).
        self.assertEqual(
            sorted(
                (w["file"], w["fn"]) for w in writers
                if "?" in w["statuses"]
            ),
            [("coopdb.py", "claim_item"), ("coopdb.py", "revise_item")],
        )
        # Literal 'done' cross-check against the writer map.
        self.assertEqual(
            sorted(
                (w["file"], w["fn"]) for w in writers
                if "'done'" in w["statuses"]
            ),
            [("coopdb.py", "complete_item")],
        )
        # Dynamic {assignments} writers draw their columns from
        # DEFINABLE_FIELDS, which must never include status.
        self.assertEqual(
            sorted(
                (w["file"], w["fn"]) for w in writers if w["dynamic"]
            ),
            [("coopdb.py", "define_item"), ("coopdb.py", "refine_item")],
        )
        self.assertNotIn("status", coopdb.DEFINABLE_FIELDS)


class CompletionBypassEndToEnd(CompletionBoard):
    """Leg (c): the real CLI refuses completion without receipt/review."""

    def test_cli_complete_refuses_typed_without_receipt_then_review(self):
        item, sid, cid = self.make_working()
        env = {"COOP_SESSION_ID": sid, "COOP_AGENT": "alice"}

        # Attempt 1: live claim, no receipt -> typed receipt_missing,
        # zero board mutation.
        before = snap(self.conn)
        code, _out, err = self._run(
            ["--json", "item", "complete", "--claim", str(cid)], env)
        self.assertEqual(code, 1)
        error = json.loads(err)["error"]
        self.assertEqual(error["reason_code"], "receipt_missing")
        self.assertEqual(snap(self.conn), before)
        self.assertEqual(self.item_row(item)["status"], "working")

        # Attempt 2: receipt submitted but review never requested ->
        # typed review_missing, zero board mutation beyond the legal
        # receipt submit.
        self.submit(cid, sid)
        before = snap(self.conn)
        code, _out, err = self._run(
            ["--json", "item", "complete", "--claim", str(cid)], env)
        self.assertEqual(code, 1)
        error = json.loads(err)["error"]
        self.assertEqual(error["reason_code"], "review_missing")
        self.assertEqual(snap(self.conn), before)

        # The item never left working and nothing ever completed.
        self.assertEqual(self.item_row(item)["status"], "working")
        self.assertEqual(self.events(item, "item_completed"), [])


if __name__ == "__main__":
    unittest.main()
