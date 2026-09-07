import contextlib
import importlib
import importlib.util
import inspect
import io
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_coop import coopdb
from tests.test_coop import seed_item
from tests.test_schema_migration import LegacyBoard


def load_module(name):
    qualified = f"agent_coop.{name}"
    spec = importlib.util.find_spec(qualified)
    if spec is None:
        raise AssertionError(f"expected module {qualified!r} to exist")
    return importlib.import_module(qualified)


class TestRendering(unittest.TestCase):
    def test_splash_uses_approved_minimal_layout(self):
        coop_ui = load_module("coop_ui")
        text = coop_ui.splash_text(width=100, color=False)
        self.assertNotIn("welcome  |", text)
        self.assertNotIn("Build together", text)
        self.assertNotIn("SQLite-backed", text)
        self.assertIn("Welcome to the coop!", text)
        version_line = next(line for line in text.splitlines() if "v0.1.0" in line)
        # Half-size mark uses single-cell / half-block glyphs (not full ██ cells).
        self.assertTrue(
            any(ch in version_line for ch in ("█", "▀", "▄")),
            version_line)
        self.assertLessEqual(len(version_line) - len(version_line.lstrip()), 2)

    def test_prompt_is_only_violet_chevron(self):
        coop_ui = load_module("coop_ui")
        self.assertEqual("> ", coop_ui.prompt_text(color=False))
        visible = coop_ui.ANSI_PATTERN.sub("", coop_ui.prompt_text(color=True))
        self.assertEqual("> ", visible)
        # Same violet focus accent as TASKS / LIVE CHAT highlight.
        self.assertIn(coop_ui.VIOLET, coop_ui.prompt_text(color=True))

    def test_splash_color_is_optional(self):
        coop_ui = load_module("coop_ui")
        self.assertIn("\033[", coop_ui.splash_text(width=120, color=True))
        self.assertNotIn("\033[", coop_ui.splash_text(width=120, color=False))

    def test_compact_splash_paints_middle_os_grey_and_blue(self):
        coop_ui = load_module("coop_ui")
        text = coop_ui.splash_text(width=50, color=True)
        first = text.splitlines()[0]
        # C coral · O grey · O blue · P blue — same palette as the large mark.
        self.assertIn(f"{coop_ui.CORAL}C", first)
        self.assertIn(f"{coop_ui.GREY}O", first)
        self.assertIn(f"{coop_ui.BLUE}O", first)
        self.assertIn(f"{coop_ui.BLUE}P", first)
        plain = coop_ui.ANSI_PATTERN.sub("", first)
        self.assertTrue(plain.startswith("COOP"), plain)

    def test_dashboard_short_header_uses_same_compact_palette(self):
        """Dashboard compact path is height-gated (<28), not width — was all-coral."""
        coop_ui = load_module("coop_ui")
        frame = coop_ui.render_dashboard(
            tasks=[],
            conversation=[],
            selected=None,
            sessions=[],
            cursor=None,
            width=100,
            height=20,
            color=True,
            board_label="test",
            board_path="/tmp/board.db",
        )
        first = frame.splitlines()[0]
        self.assertIn(f"{coop_ui.CORAL}C", first)
        self.assertIn(f"{coop_ui.GREY}O", first)
        self.assertIn(f"{coop_ui.BLUE}O", first)
        self.assertIn(f"{coop_ui.BLUE}P", first)
        plain = coop_ui.ANSI_PATTERN.sub("", first)
        self.assertTrue(plain.startswith("COOP"), plain)

    def test_large_splash_does_not_wrap_at_seventy_two_columns(self):
        coop_ui = load_module("coop_ui")
        lines = coop_ui.splash_text(width=72, color=False).splitlines()
        self.assertLessEqual(max(map(len, lines)), 72)

    def test_footer_animates_only_active_agents(self):
        coop_ui = load_module("coop_ui")
        snap = coop_ui.BoardSnapshot(
            agents=(
                coop_ui.AgentActivity("claude", "working", 2, "auth"),
                coop_ui.AgentActivity("grok", "idle", None, None),
            ),
        )
        renderer = getattr(coop_ui, "render_footer", None)
        self.assertIsNotNone(renderer, "render_footer must be implemented")
        frame_zero = renderer(snap, frame=0, color=False)
        frame_one = renderer(snap, frame=1, color=False)
        self.assertNotEqual(frame_zero, frame_one)
        self.assertIn("#2 auth", frame_zero)
        self.assertIn("grok idle", frame_zero)
        self.assertIn("grok idle", frame_one)

    def test_footer_blinks_a_fixed_width_square_without_shifting_text(self):
        coop_ui = load_module("coop_ui")
        snap = coop_ui.BoardSnapshot(
            agents=(coop_ui.AgentActivity("claude", "working", 4, "Demo M0c"),),
        )

        square_on = coop_ui.render_footer(snap, frame=0, color=False)
        square_off = coop_ui.render_footer(snap, frame=1, color=False)

        self.assertIn("working ▪ #4 Demo M0c", square_on)
        self.assertIn("working   #4 Demo M0c", square_off)
        self.assertEqual(len(square_on), len(square_off))
        self.assertEqual(square_on.replace("▪", " "), square_off)
        self.assertNotIn("working.", square_on + square_off)

    def test_footer_has_three_agents_no_totals_and_stays_on_one_row(self):
        coop_ui = load_module("coop_ui")
        snap = coop_ui.BoardSnapshot(
            agents=tuple(
                coop_ui.AgentActivity(name, "working", index, "long active task title")
                for index, name in enumerate(("claude", "codex", "grok"), 1)
            ),
        )
        renderer = getattr(coop_ui, "render_footer", None)
        self.assertIsNotNone(renderer, "render_footer must be implemented")
        rendered = renderer(snap, color=True, width=80)
        visible = coop_ui.ANSI_PATTERN.sub("", rendered)
        self.assertLessEqual(len(visible), 80)
        self.assertIn("claude working", visible)
        self.assertIn("codex working", visible)
        self.assertIn("grok working", visible)
        self.assertNotIn("tasks", visible)
        self.assertNotIn("todo:", visible)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(pathlib.Path(self.tmp.name) / "board.db")
        conn = coopdb.connect(self.db)
        coopdb.init_db(conn)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()


class TestLegacySchemaGuard(unittest.TestCase):
    def _board_state(self, path):
        with contextlib.closing(sqlite3.connect(path)) as conn:
            return {
                "identity": (
                    conn.execute("PRAGMA application_id").fetchone()[0],
                    conn.execute("PRAGMA user_version").fetchone()[0],
                ),
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "item_count": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
            }

    def test_load_snapshot_rejects_legacy_before_wal_side_effects(self):
        coop_repl = load_module("coop_repl")
        with LegacyBoard() as board:
            before = self._board_state(board.path)
            rejection = None
            try:
                coop_repl.load_snapshot(str(board.path), human="human")
            except coopdb.CoopError as exc:
                rejection = exc
            after = self._board_state(board.path)

            self.assertEqual("delete", before["journal_mode"])
            self.assertEqual("delete", after["journal_mode"])
            self.assertEqual(before["identity"], after["identity"])
            self.assertEqual(before["item_count"], after["item_count"])
            self.assertFalse(pathlib.Path(f"{board.path}-wal").exists())
            self.assertFalse(pathlib.Path(f"{board.path}-shm").exists())
            self.assertIsNotNone(rejection)
            self.assertIn("coop migrate", str(rejection))

    def test_free_text_rejects_legacy_without_mutation_or_wal(self):
        coop_repl = load_module("coop_repl")
        out, err = io.StringIO(), io.StringIO()
        with LegacyBoard() as board:
            before = self._board_state(board.path)
            rejection = None
            try:
                coop_repl.dispatch_line(
                    "unguarded repl mutation",
                    str(board.path),
                    "claude",
                    out,
                    err,
                )
            except coopdb.CoopError as exc:
                rejection = exc
            after = self._board_state(board.path)

            self.assertEqual(before["item_count"], after["item_count"])
            self.assertEqual("delete", before["journal_mode"])
            self.assertEqual("delete", after["journal_mode"])
            self.assertEqual(before["identity"], after["identity"])
            self.assertFalse(pathlib.Path(f"{board.path}-wal").exists())
            self.assertFalse(pathlib.Path(f"{board.path}-shm").exists())
            self.assertIsNotNone(rejection)
            self.assertIn("coop migrate", str(rejection))
            self.assertEqual("", out.getvalue())
            self.assertEqual("", err.getvalue())


class TestNoPackagedBoardPath(unittest.TestCase):
    """The REPL is a library for the dashboard. It must not carry an entry
    point that derives a board path from its own install location — from a
    wheel that path is site-packages, and a board would be created there."""

    def test_module_has_no_entry_point(self):
        coop_repl = load_module("coop_repl")
        self.assertFalse(hasattr(coop_repl, "main"))

    def test_no_board_path_is_derived_from_the_module_file(self):
        coop_repl = load_module("coop_repl")
        source = inspect.getsource(coop_repl)
        self.assertNotIn("__file__", source)
        self.assertNotIn("parents[1]", source)
        self.assertNotIn("__main__", source)

    def test_monitor_only_needs_the_pure_helpers(self):
        coop_repl = load_module("coop_repl")
        for name in ("classify_line", "dispatch_line", "run_session"):
            self.assertTrue(callable(getattr(coop_repl, name)), name)


class TestDispatch(Base):
    def test_plain_text_posts_a_human_message(self):
        # Bare free text is a non-binding message from the reserved
        # human actor, never an item mutation.
        coop_repl = load_module("coop_repl")
        out, err = io.StringIO(), io.StringIO()
        self.assertTrue(
            coop_repl.dispatch_line(
                "Build the first real pilot", self.db, "human", out, err
            )
        )
        conn = coopdb.connect(self.db)
        row = conn.execute(
            "SELECT from_agent,body,kind FROM messages"
        ).fetchone()
        items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()
        self.assertEqual(
            tuple(row), ("human", "Build the first real pilot", "chat")
        )
        self.assertEqual(items, 0, "free text must never create an item")
        self.assertIn("message", out.getvalue())
        self.assertEqual("", err.getvalue())

    def test_explicit_command_uses_existing_parser(self):
        coop_repl = load_module("coop_repl")
        out, err = io.StringIO(), io.StringIO()
        self.assertTrue(
            coop_repl.dispatch_line("status", self.db, "human", out, err)
        )
        self.assertIn("next:", out.getvalue())  # boot panel
        self.assertEqual("", err.getvalue())

    def test_command_error_returns_to_session(self):
        coop_repl = load_module("coop_repl")
        out, err = io.StringIO(), io.StringIO()
        self.assertTrue(
            coop_repl.dispatch_line("migrate", self.db, "human", out, err)
        )
        self.assertIn("error: migration_failed:", err.getvalue())

    def test_quit_is_the_only_terminating_input(self):
        coop_repl = load_module("coop_repl")
        out, err = io.StringIO(), io.StringIO()
        self.assertFalse(coop_repl.dispatch_line("quit", self.db, "human", out, err))
        self.assertTrue(coop_repl.dispatch_line("", self.db, "human", out, err))


class TestSnapshot(Base):
    def test_empty_board_still_has_provider_slots(self):
        coop_repl = load_module("coop_repl")
        snapshot = coop_repl.load_snapshot(self.db, human="human")
        self.assertEqual(
            [(row.name, row.state) for row in snapshot.agents],
            [("claude", "idle"), ("codex", "idle"), ("grok", "idle")],
        )

    def test_snapshot_derives_work_review_and_idle(self):
        coop_repl = load_module("coop_repl")
        conn = coopdb.connect(self.db)
        for name in ("human", "claude", "codex", "grok"):
            coopdb.register_agent(conn, name)

        seed_item(conn, "Implement auth", created_by="human", owner="claude")
        review = seed_item(
            conn, "Review storage", created_by="human", owner="codex",
            status="review",
        )
        def _request_review(c):
            c.execute(
                "INSERT INTO reviews(item_id,requested_by,reviewer,status,"
                "created_at) VALUES (?,?,?,'requested',?)",
                (review, "codex", "grok", coopdb.now()))
        coopdb.mutate(conn, _request_review)
        conn.close()

        snapshot = coop_repl.load_snapshot(self.db, human="human")
        states = {agent.name: agent.state for agent in snapshot.agents}
        self.assertEqual(
            states,
            {"claude": "working", "codex": "working", "grok": "reviewing"},
        )
        self.assertNotIn("human", states)


class TestReadCommand(unittest.TestCase):
    def test_long_input_is_windowed_to_one_terminal_row(self):
        coop_ui = load_module("coop_ui")
        rendered = coop_ui._fit_input_line(
            "> ", "abcdefghijklmnopqrstuvwxyz", width=20
        )
        self.assertLessEqual(len(rendered), 19)
        self.assertTrue(rendered.endswith("z"))
        self.assertIn("…", rendered)

    def test_redirected_input_uses_blocking_fallback(self):
        coop_ui = load_module("coop_ui")
        stdin = io.StringIO("status\n")
        stdout = io.StringIO()
        line = coop_ui.read_command(
            "> ",
            lambda frame, width=None: f"agents idle frame={frame} width={width}",
            stdin=stdin,
            stdout=stdout,
        )
        self.assertEqual(line, "status")
        self.assertNotIn("agents idle", stdout.getvalue())
        self.assertIn("> ", stdout.getvalue())

    def test_windows_reader_preserves_buffer_and_backspace(self):
        coop_ui = load_module("coop_ui")

        class FakeKeys:
            def __init__(self):
                self.keys = iter(("h", "x", "\b", "i", "\r"))

            def kbhit(self):
                return True

            def getwch(self):
                return next(self.keys)

        stdout = io.StringIO()
        line = coop_ui._read_windows(
            "> ",
            lambda frame, width=None: f"working{frame}",
            stdout,
            FakeKeys(),
            refresh=60,
        )
        self.assertEqual(line, "hi")
        self.assertIn("working0", stdout.getvalue())

    def test_windows_reader_rebuilds_footer_on_resize_and_keeps_buffer(self):
        coop_ui = load_module("coop_ui")
        self.assertIn(
            "size_provider",
            inspect.signature(coop_ui._read_windows).parameters,
            "the Windows reader must accept an injectable terminal-size provider",
        )

        class FakeKeys:
            def __init__(self):
                self.keys = iter(("h", "i", "\r"))

            def kbhit(self):
                return True

            def getwch(self):
                return next(self.keys)

        sizes = iter((os.terminal_size((100, 24)), os.terminal_size((40, 12))))
        current = os.terminal_size((40, 12))

        def size_provider(_fallback=(100, 24)):
            nonlocal current
            current = next(sizes, current)
            return current

        widths = []
        stdout = io.StringIO()
        line = coop_ui._read_windows(
            "> ",
            lambda frame, width: widths.append(width)
            or "claude idle   codex idle   grok idle",
            stdout,
            FakeKeys(),
            refresh=60,
            size_provider=size_provider,
        )
        output = stdout.getvalue()
        self.assertEqual("hi", line)
        self.assertIn(100, widths)
        self.assertIn(40, widths)
        self.assertIn("\033[1;23r", output)
        self.assertIn("\033[1;11r", output)
        self.assertIn("\033[12;1H", output)
        self.assertIn("\033[5 q", output)
        self.assertIn("> hi", coop_ui.ANSI_PATTERN.sub("", output))

    def test_windows_reader_disables_footer_below_three_rows(self):
        coop_ui = load_module("coop_ui")
        self.assertIn(
            "size_provider", inspect.signature(coop_ui._read_windows).parameters
        )

        class FakeKeys:
            def __init__(self):
                self.keys = iter(("x", "\r"))

            def kbhit(self):
                return True

            def getwch(self):
                return next(self.keys)

        stdout = io.StringIO()
        line = coop_ui._read_windows(
            "> ",
            lambda frame, width: "claude idle   codex idle   grok idle",
            stdout,
            FakeKeys(),
            refresh=60,
            size_provider=lambda _fallback=(100, 24): os.terminal_size((40, 2)),
        )
        self.assertEqual("x", line)
        self.assertIn("\033[r", stdout.getvalue())
        self.assertNotIn("\033[1;1r", stdout.getvalue())

    def test_restore_terminal_resets_cursor_and_scroll_region(self):
        coop_ui = load_module("coop_ui")
        restore = getattr(coop_ui, "restore_terminal", None)
        self.assertIsNotNone(restore, "restore_terminal must be implemented")
        stdout = io.StringIO()
        restore(
            stdout,
            size_provider=lambda _fallback=(100, 24): os.terminal_size((40, 12)),
        )
        output = stdout.getvalue()
        self.assertIn("\033[0 q", output)
        self.assertIn("\033[r", output)
        self.assertIn("\033[12;1H", output)


class TestSession(Base):
    def test_session_persists_until_quit(self):
        coop_repl = load_module("coop_repl")
        lines = iter(["First pilot note", "status", "quit"])
        out, err = io.StringIO(), io.StringIO()
        code = coop_repl.run_session(
            self.db,
            reader=lambda _prompt, _status: next(lines),
            out=out,
            err=err,
            color=False,
        )
        transcript = out.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual("", err.getvalue())
        self.assertIn("posted message #1", transcript)
        self.assertIn("next:", transcript)  # boot panel
        self.assertIn("First pilot note", transcript)
        self.assertEqual(transcript.count("Welcome to the coop!"), 1)
        self.assertEqual(transcript.count("> "), 3)
        self.assertNotIn("Build together", transcript)
        self.assertNotIn("claude idle", transcript)

    def test_session_footer_provider_uses_current_width(self):
        coop_repl = load_module("coop_repl")
        coop_ui = load_module("coop_ui")
        observed = []

        def reader(_prompt, footer_provider):
            self.assertEqual(2, len(inspect.signature(footer_provider).parameters))
            footer = footer_provider(0, 40)
            observed.append(coop_ui.ANSI_PATTERN.sub("", footer))
            return "quit"

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            coop_repl.run_session(
                self.db, reader=reader, out=out, err=err, color=False
            ),
            0,
        )
        self.assertEqual(1, len(observed))
        self.assertLessEqual(len(observed[0]), 40)
        self.assertEqual("claude idle   codex idle   grok idle", observed[0])

    def test_interactive_session_always_restores_terminal(self):
        coop_repl = load_module("coop_repl")
        coop_ui = load_module("coop_ui")

        for command in ("quit", KeyboardInterrupt()):
            with self.subTest(command=type(command).__name__):
                out, err = io.StringIO(), io.StringIO()
                read_behavior = (
                    {"return_value": command}
                    if isinstance(command, str)
                    else {"side_effect": command}
                )
                with (
                    mock.patch.object(
                        coop_ui,
                        "live_footer_supported",
                        return_value=True,
                        create=True,
                    ),
                    mock.patch.object(
                        coop_ui, "read_command", **read_behavior
                    ),
                    mock.patch.object(coop_ui, "restore_terminal") as restore,
                ):
                    self.assertEqual(
                        coop_repl.run_session(
                            self.db, out=out, err=err, color=False
                        ),
                        0,
                    )
                restore.assert_called_once_with(out)

    def test_eof_exits_cleanly(self):
        coop_repl = load_module("coop_repl")

        def eof(_prompt, _status):
            raise EOFError

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            coop_repl.run_session(
                self.db, reader=eof, out=out, err=err, color=False
            ),
            0,
        )
        self.assertEqual("", err.getvalue())


if __name__ == "__main__":
    unittest.main()
