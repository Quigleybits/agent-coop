"""Regression: the cwd-default board auto-provision (cli `_db_meta` /
`_auto_provision`, `_reads_only`).

A write command run in a directory with no resolvable board creates one in
place and names every file first. A read command never creates anything: it
reports the missing board and exits non-zero. Only the `cwd_default`
resolution source is eligible at all. Every other source — `--db`,
`COOP_DB`, `COOP_DEFAULT_DB`, a discovered ancestor board — names a board
the caller meant, so its absence stays an error. The home directory, its
parents, a filesystem root and the temp directory are refused outright.

Opening the dashboard (`monitor`, bare `coop`) is the one non-write command
that creates: see tests/test_golden_path.py.

Offline; no provider, no quota. Every test redirects the switcher registry
with COOP_BOARDS_REGISTRY and the user-global launch skill with
COOP_GLOBAL_SKILLS_HOME, so the operator's real ~/.coop, ~/.claude, ~/.codex
and ~/.grok are never written, and the guard tests point HOME/USERPROFILE at
a throwaway directory so a failing guard can never touch the real home.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from agent_coop import coop_adapters
from agent_coop import coopdb

INSTALL_ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTICE = "auto-created board"
PLAN = "creating .coop/board.db"
NO_BOARD = "no board here"
REFUSED = "refusing to create a board"


class ProvisionCase(unittest.TestCase):
    """Every run happens in a throwaway directory, never in this repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = pathlib.Path(self.tmp.name) / "workdir"
        self.work.mkdir()
        # The walk-up is git-style, so a stray board above the temp root would
        # make `cwd_default` unreachable and quietly void these assertions.
        found = coopdb.discover_board(str(self.work))
        if found:
            self.skipTest(f"an ancestor of the temp dir holds a board: {found}")

    def clean_env(self, **extra):
        """No live operator session leaks in, and no test ever writes the
        real ~/.coop switcher registry."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("COOP_")}
        env["COOP_BOARDS_REGISTRY"] = str(self.registry)
        env["COOP_GLOBAL_SKILLS_HOME"] = str(self.skill_home)
        env["PYTHONPATH"] = str(INSTALL_ROOT)
        env.update(extra)
        return env

    @property
    def registry(self):
        return pathlib.Path(self.tmp.name) / "boards.json"

    @property
    def skill_home(self):
        return pathlib.Path(self.tmp.name) / "skillhome"

    def coop(self, *argv, cwd=None, **env_extra):
        return subprocess.run(
            [sys.executable, "-m", "agent_coop", *argv],
            capture_output=True, text=True, timeout=120,
            env=self.clean_env(**env_extra),
            cwd=str(cwd or self.work))

    @property
    def cwd_board(self):
        return self.work / ".coop" / "board.db"

    def assert_nothing_created(self, root=None):
        root = pathlib.Path(root or self.work)
        self.assertFalse((root / ".coop").exists(), list(root.iterdir()))
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertFalse((root / relative).exists())
        self.assertFalse((root / ".claude").exists())
        self.assertFalse((root / ".agents").exists())


class ReadCommandsNeverProvision(ProvisionCase):
    """(b) a read command with no board writes nothing and exits non-zero."""

    READ_COMMANDS = (
        ("status",),
        ("status", "--json"),
        ("--as", "alice", "inbox", "--peek"),
        ("tasks",),
        ("task",),
        ("queue",),
        ("board",),
        ("agents",),
        ("item", "show", "1"),
        ("envelope", "list"),
        ("envelope", "show", "1"),
        ("huddle", "show", "1"),
    )

    def test_monitor_is_not_a_read_command(self):
        # Opening the dashboard creates a missing board (golden path B);
        # the read list above must never grow it back.
        self.assertNotIn(("monitor",), self.READ_COMMANDS)

    def test_every_read_command_reports_the_missing_board(self):
        for argv in self.READ_COMMANDS:
            with self.subTest(argv=argv):
                result = self.coop(*argv)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(NO_BOARD, result.stderr)
                self.assertNotIn(NOTICE, result.stderr)
                self.assert_nothing_created()

    def test_the_text_error_is_one_line_with_the_fix(self):
        result = self.coop("status")

        self.assertEqual(result.returncode, 1)
        lines = [ln for ln in result.stderr.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn("coop init --workspace .", lines[0])
        self.assertEqual(result.stdout, "")

    def test_the_json_error_keeps_the_structured_shape(self):
        result = self.coop("status", "--json")

        self.assertEqual(result.returncode, 1)
        error = json.loads(result.stderr)["error"]
        self.assertEqual(error["type"], "board_missing")
        self.assertEqual(error["reason_code"], "target_not_found")
        self.assertIn(NO_BOARD, error["message"])
        self.assertEqual(result.stdout, "")

    def test_the_registry_is_untouched_by_a_refused_read(self):
        self.coop("status")
        self.assertFalse(self.registry.exists())


class WriteCommandsProvision(ProvisionCase):
    """(c) a write command still creates the board, and says so first."""

    def test_a_write_command_in_an_empty_cwd_creates_a_board(self):
        result = self.coop("say", "hello")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.cwd_board.is_file(), result.stderr)
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertTrue((self.work / relative).is_file())
        self.assertIn("message 1", result.stdout)

    def test_the_file_list_is_announced_before_any_write(self):
        result = self.coop("say", "hello")

        lines = result.stderr.splitlines()
        plan = [i for i, ln in enumerate(lines) if ln.startswith(PLAN)]
        created = [i for i, ln in enumerate(lines) if NOTICE in ln]
        self.assertEqual(len(plan), 1, result.stderr)
        self.assertTrue(created, result.stderr)
        self.assertLess(plan[0], created[0])
        for expected in (".coop/board.db", ".coop/harness-adapters.json",
                         ".claude/skills/coop/SKILL.md",
                         ".agents/skills/coop/SKILL.md"):
            self.assertIn(expected, lines[plan[0]])
        self.assertIn(str(self.cwd_board.resolve()), result.stderr)
        self.assertIn("adapter installed", result.stderr)

    def test_json_stdout_stays_machine_readable(self):
        result = self.coop("--json", "say", "hello")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"message_id": 1})
        self.assertIn(PLAN, result.stderr)
        self.assertIn(NOTICE, result.stderr)

    def test_the_second_run_reuses_the_board(self):
        self.assertEqual(self.coop("say", "one").returncode, 0)
        stamp = self.cwd_board.stat().st_mtime_ns

        second = self.coop("status", "--json")

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn(NOTICE, second.stderr)
        self.assertNotIn(PLAN, second.stderr)
        self.assertIn("next_action", json.loads(second.stdout))
        self.assertEqual(self.cwd_board.stat().st_mtime_ns, stamp)

    def test_adapter_escape_blocks_auto_provision_before_board_creation(self):
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.mkdir()
        try:
            (self.work / ".agents").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")

        result = self.coop("--json", "say", "hello")

        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stderr)
        self.assertEqual(
            payload["error"]["reason_code"], "adapter_path_invalid")
        self.assertNotIn(PLAN, result.stderr)
        self.assertFalse(self.cwd_board.exists())
        self.assertFalse((outside / "skills" / "coop" / "SKILL.md").exists())

    def test_auto_provision_leaves_the_operator_home_registry_alone(self):
        home_registry = pathlib.Path.home() / ".coop" / "boards.json"
        before = (home_registry.read_text(encoding="utf-8")
                  if home_registry.is_file() else None)

        self.assertEqual(self.coop("say", "hello").returncode, 0)

        after = (home_registry.read_text(encoding="utf-8")
                 if home_registry.is_file() else None)
        self.assertEqual(before, after)
        # It landed in the redirected registry instead.
        self.assertTrue(self.registry.is_file())
        self.assertIn(str(self.cwd_board.resolve()),
                      json.loads(self.registry.read_text(encoding="utf-8")))


class ExplicitTargetsStillError(ProvisionCase):
    """Every explicit target names a board the caller meant."""

    def test_an_explicit_db_flag_to_a_missing_file_errors(self):
        target = pathlib.Path(self.tmp.name) / "named.db"

        result = self.coop("--db", str(target), "say", "hello")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.cwd_board.exists(),
                         "an explicit --db must not auto-provision the cwd")
        self.assertNotIn(NOTICE, result.stderr)
        self.assertNotIn(NOTICE, result.stdout)

    def test_coop_db_in_the_environment_is_explicit_too(self):
        target = pathlib.Path(self.tmp.name) / "from-env.db"

        result = self.coop("say", "hello", COOP_DB=str(target))

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.cwd_board.exists())
        self.assertNotIn(NOTICE, result.stderr)

    def test_coop_default_db_is_not_auto_provisioned(self):
        target = pathlib.Path(self.tmp.name) / "fallback.db"

        result = self.coop("say", "hello", COOP_DEFAULT_DB=str(target))

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.cwd_board.exists())
        self.assertNotIn(NOTICE, result.stderr)

    def test_a_discovered_ancestor_board_is_never_shadowed(self):
        # Discovery walks up only inside a Git worktree; mark the root.
        (self.work / ".git").mkdir()
        self.assertEqual(
            self.coop("init", "--workspace", str(self.work)).returncode, 0)
        nested = self.work / "src" / "deep"
        nested.mkdir(parents=True)

        result = self.coop("say", "hello", cwd=nested)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(NOTICE, result.stderr)
        self.assertFalse((nested / ".coop").exists(),
                         "the ancestor board should have been discovered")


class RefusedWorkspaces(ProvisionCase):
    """(a) home, its parents and a root never receive a board.

    The child process sees a throwaway HOME/USERPROFILE, so even a broken
    guard would write into the temp directory, never the real home.
    """

    def setUp(self):
        super().setUp()
        self.fake_home = pathlib.Path(self.tmp.name) / "fakehome"
        self.fake_home.mkdir()

    def home_env(self):
        return {"HOME": str(self.fake_home),
                "USERPROFILE": str(self.fake_home)}

    def test_a_write_command_in_home_is_refused(self):
        result = self.coop("say", "hello", cwd=self.fake_home,
                           **self.home_env())

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(REFUSED, result.stderr)
        self.assertIn("home directory", result.stderr)
        self.assertIn("coop init --workspace <dir>", result.stderr)
        self.assertEqual(
            len([ln for ln in result.stderr.splitlines() if ln.strip()]), 1)
        self.assert_nothing_created(self.fake_home)

    def test_a_write_command_above_home_is_refused(self):
        parent = self.fake_home.parent
        result = self.coop("say", "hello", cwd=parent, **self.home_env())

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("contains your home directory", result.stderr)
        self.assert_nothing_created(parent)

    def test_the_refusal_is_structured_under_json(self):
        result = self.coop("--json", "say", "hello", cwd=self.fake_home,
                           **self.home_env())

        self.assertEqual(result.returncode, 1)
        error = json.loads(result.stderr)["error"]
        self.assertEqual(error["type"], "workspace_refused")
        self.assertEqual(error["reason_code"], "input_invalid")
        self.assert_nothing_created(self.fake_home)

    def test_init_in_home_is_refused(self):
        for argv in (("init",), ("init", "--workspace", str(self.fake_home)),
                     ("init", "--workspace", "~")):
            with self.subTest(argv=argv):
                result = self.coop(*argv, cwd=self.fake_home,
                                   **self.home_env())
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(REFUSED, result.stderr)
                self.assertIn("choose a project directory", result.stderr)
                self.assert_nothing_created(self.fake_home)

    def test_init_above_home_is_refused(self):
        parent = self.fake_home.parent
        result = self.coop("init", "--workspace", str(parent),
                           cwd=self.work, **self.home_env())

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("contains your home directory", result.stderr)
        self.assert_nothing_created(parent)

    def test_a_project_below_home_is_an_ordinary_workspace(self):
        project = self.fake_home / "projects" / "app"
        project.mkdir(parents=True)

        result = self.coop("say", "hello", cwd=project, **self.home_env())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((project / ".coop" / "board.db").is_file())
        self.assert_nothing_created(self.fake_home)

    def test_an_explicit_db_in_home_stays_the_callers_decision(self):
        target = self.fake_home / "named.db"
        result = self.coop("--db", str(target), "init",
                           cwd=self.fake_home, **self.home_env())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.is_file())
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertFalse((self.fake_home / relative).exists())


if __name__ == "__main__":
    unittest.main()
