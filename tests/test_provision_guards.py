"""Unit coverage for the board-creation guards.

`coop_adapters.workspace_refusal` decides which directories never receive a
board; `cli._reads_only` decides which commands never create one. The
dashboard switcher and `provision_workspace_board` share the workspace
guard. Offline; no subprocess, no provider.
"""
import argparse
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from agent_coop import cli as coopcli
from agent_coop import coop_adapters
from agent_coop import coop_monitor
from agent_coop import coopdb


def _no_home(cls):
    raise RuntimeError("no home directory")


class WorkspaceRefusal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()

    def with_home(self, home):
        return mock.patch.object(
            pathlib.Path, "home", classmethod(lambda cls: home))

    def test_home_is_refused(self):
        with self.with_home(self.fake_home):
            reason = coop_adapters.workspace_refusal(self.fake_home)
        self.assertIn("home directory", reason)

    def test_every_parent_of_home_is_refused(self):
        with self.with_home(self.fake_home):
            for parent in self.fake_home.parents:
                with self.subTest(parent=parent):
                    reason = coop_adapters.workspace_refusal(parent)
                    self.assertIn("contains your home directory", reason)

    def test_a_directory_below_home_is_allowed(self):
        project = self.fake_home / "projects" / "app"
        project.mkdir(parents=True)
        with self.with_home(self.fake_home):
            self.assertIsNone(coop_adapters.workspace_refusal(project))

    def test_a_filesystem_root_is_refused_even_without_a_home(self):
        anchor = pathlib.Path(self.root.anchor)
        with mock.patch.object(pathlib.Path, "home", classmethod(_no_home)):
            reason = coop_adapters.workspace_refusal(anchor)
        self.assertIn("filesystem root", reason)

    def test_the_temp_root_is_refused_only_for_auto_provision(self):
        temp_root = pathlib.Path(tempfile.gettempdir())
        with mock.patch.object(pathlib.Path, "home", classmethod(_no_home)):
            self.assertIn(
                "temp directory",
                coop_adapters.workspace_refusal(
                    temp_root, allow_temp_root=False))
            self.assertIsNone(
                coop_adapters.workspace_refusal(
                    temp_root, allow_temp_root=True))
            self.assertIsNone(
                coop_adapters.workspace_refusal(
                    self.root, allow_temp_root=False))

    def test_home_comparison_ignores_case_on_the_same_path(self):
        upper = pathlib.Path(str(self.fake_home).upper())
        with self.with_home(self.fake_home):
            reason = coop_adapters.workspace_refusal(upper)
        if coop_adapters._same_path(upper, self.fake_home):
            self.assertIn("home directory", reason)
        else:  # case-sensitive filesystem: a different directory
            self.assertIsNone(reason)

    def test_refuse_raises_a_typed_error_with_the_hint(self):
        with self.with_home(self.fake_home):
            with self.assertRaises(coop_adapters.WorkspaceRefused) as caught:
                coop_adapters.refuse_unsafe_workspace(
                    self.fake_home, hint="choose a project directory")
        error = caught.exception
        self.assertIn("refusing to create a board", str(error))
        self.assertIn("every repository below it", str(error))
        self.assertTrue(str(error).endswith("choose a project directory"))
        self.assertEqual(error.type, "workspace_refused")
        self.assertEqual(error.reason_code, "input_invalid")
        self.assertIsInstance(error, coopdb.CoopError)

    def test_refuse_returns_the_resolved_path_when_allowed(self):
        project = self.root / "repo"
        project.mkdir()
        with self.with_home(self.fake_home):
            resolved = coop_adapters.refuse_unsafe_workspace(project)
        self.assertEqual(resolved, project.resolve())


class ProvisionPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        self.home_patch = mock.patch.object(
            pathlib.Path, "home", classmethod(lambda cls: self.fake_home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.registry_patch = mock.patch.dict(
            "os.environ", {"COOP_BOARDS_REGISTRY": str(self.root / "b.json")})
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)

    def assert_untouched(self, folder):
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()), [], list(folder.iterdir()))

    def test_provision_notice_names_every_file(self):
        notice = coop_adapters.provision_notice(
            self.root / "repo" / ".coop" / "board.db")
        self.assertTrue(notice.startswith("creating .coop/board.db, "))
        for expected in (".coop/harness-adapters.json",
                         ".claude/skills/coop/SKILL.md",
                         ".agents/skills/coop/SKILL.md",
                         str((self.root / "repo").resolve())):
            self.assertIn(expected, notice)
        self.assertEqual(len(notice.splitlines()), 1)

    def test_provision_workspace_board_refuses_home_before_any_write(self):
        board = self.fake_home / ".coop" / "board.db"
        with self.assertRaises(coop_adapters.WorkspaceRefused):
            coop_adapters.provision_workspace_board(str(board))
        self.assert_untouched(self.fake_home)

    def test_switcher_open_reports_the_refusal_and_writes_nothing(self):
        board = self.fake_home / ".coop" / "board.db"
        conn, notice = coop_monitor.open_or_create_board(str(board))
        self.assertIsNone(conn)
        self.assertIn("could not create board", notice)
        self.assertIn("home directory", notice)
        self.assert_untouched(self.fake_home)

    def test_switcher_open_still_creates_a_project_board(self):
        project = self.fake_home / "app"
        project.mkdir()
        conn, notice = coop_monitor.open_or_create_board(
            str(project / ".coop" / "board.db"))
        self.assertIsNotNone(conn)
        conn.close()
        self.assertIn("created board", notice)
        self.assertTrue((project / ".coop" / "board.db").is_file())


class ReadOnlyCommandTable(unittest.TestCase):
    READ = (
        ["status"], ["status", "--json"], ["--as", "x", "inbox"],
        ["tasks"], ["task"], ["queue"], ["board"], ["agents"],
        ["item", "show", "1"], ["envelope", "list"], ["envelope", "show", "1"],
        ["huddle", "show", "1"],
    )
    # `monitor` creates a missing board on open (dashboard create-on-open),
    # so it is not read-only; bare `coop` routes to it.
    WRITE = (
        ["init"], ["say", "hi"], ["monitor"],
        ["item", "claim", "1", "--intent", "x"],
        ["item", "complete", "--claim", "1"],
        ["claim", "release", "--claim", "1", "--reason", "x"],
        ["huddle", "open", "--claim", "1"],
        ["question", "claim", "1", "--intent", "x"],
        ["needs-input", "--claim", "1", "--question", "q"],
    )

    def parse(self, argv):
        return coopcli.build_parser().parse_args(argv)

    def test_read_commands_are_read_only(self):
        for argv in self.READ:
            with self.subTest(argv=argv):
                self.assertTrue(coopcli._reads_only(self.parse(argv)))

    def test_write_commands_are_not(self):
        for argv in self.WRITE:
            with self.subTest(argv=argv):
                self.assertFalse(coopcli._reads_only(self.parse(argv)))

    def test_bare_coop_routes_like_monitor(self):
        parser = coopcli.build_parser()
        bare = parser.parse_args(coopcli._default_argv([], parser))
        self.assertEqual(coopcli._command_key(bare), "monitor")
        self.assertFalse(coopcli._reads_only(bare))


class DashboardOpenGuards(unittest.TestCase):
    """`cli._dashboard_board`: the dashboard creates a missing cwd-default
    board, and refuses the same directories as every other creation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        self.previous_cwd = pathlib.Path.cwd()
        self.addCleanup(lambda: os.chdir(self.previous_cwd))
        self.home_patch = mock.patch.object(
            pathlib.Path, "home", classmethod(lambda cls: self.fake_home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.env_patch = mock.patch.dict(
            "os.environ", {"COOP_BOARDS_REGISTRY": str(self.root / "b.json"),
                           "COOP_DB": "", "COOP_DB_PATH": "",
                           "COOP_DEFAULT_DB": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def monitor_args(self, *extra):
        return coopcli.build_parser().parse_args(["monitor", *extra])

    def assert_untouched(self, folder):
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()), [], list(folder.iterdir()))

    def test_home_is_refused_before_any_write(self):
        os.chdir(self.fake_home)
        with self.assertRaises(coop_adapters.WorkspaceRefused) as caught:
            coopcli._dashboard_board(self.monitor_args())
        self.assertIn("home directory", str(caught.exception))
        self.assert_untouched(self.fake_home)

    def test_a_parent_of_home_is_refused(self):
        os.chdir(self.root)
        with self.assertRaises(coop_adapters.WorkspaceRefused) as caught:
            coopcli._dashboard_board(self.monitor_args())
        self.assertIn("contains your home directory", str(caught.exception))
        self.assertFalse((self.root / ".coop").exists())

    def test_the_temp_root_is_refused(self):
        temp_root = pathlib.Path(tempfile.gettempdir())
        if coopdb.discover_board(str(temp_root)):
            self.skipTest("an ancestor of the temp root holds a board")
        elsewhere = pathlib.Path(temp_root.anchor) / "coop-test-home-absent"
        os.chdir(temp_root)
        with mock.patch.object(pathlib.Path, "home",
                               classmethod(lambda cls: elsewhere)):
            with self.assertRaises(coop_adapters.WorkspaceRefused) as caught:
                coopcli._dashboard_board(self.monitor_args())
        self.assertIn("temp directory", str(caught.exception))

    def test_a_project_folder_gets_a_board_and_the_notice(self):
        project = self.fake_home / "projects" / "app"
        project.mkdir(parents=True)
        os.chdir(project)
        db, notice = coopcli._dashboard_board(self.monitor_args())
        self.assertEqual(pathlib.Path(db).resolve(),
                         (project / ".coop" / "board.db").resolve())
        self.assertTrue((project / ".coop" / "board.db").is_file())
        self.assertIn("created board in", notice)
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertTrue((project / relative).is_file(), relative)
        # Second open: the board exists, no creation notice.
        self.assertEqual(coopcli._dashboard_board(self.monitor_args())[1], "")

    def test_an_explicit_missing_target_is_an_error(self):
        target = self.root / "named.db"
        args = coopcli.build_parser().parse_args(
            ["--db", str(target), "monitor"])
        with self.assertRaises(coopdb.BoardMissing):
            coopcli._dashboard_board(args)
        self.assertFalse(target.exists())

    def test_every_table_entry_names_a_real_command(self):
        parser = coopcli.build_parser()
        root = next(a for a in parser._actions
                    if isinstance(a, argparse._SubParsersAction))
        for key in coopcli._READ_ONLY_COMMANDS:
            cmd, _, sub = key.partition(" ")
            with self.subTest(key=key):
                self.assertIn(cmd, root.choices)
                if sub:
                    nested = next(
                        a for a in root.choices[cmd]._actions
                        if isinstance(a, argparse._SubParsersAction))
                    self.assertIn(sub, nested.choices)


if __name__ == "__main__":
    unittest.main()
