"""A board provisioned in a repo that is not Co-op.

The end-to-end check this feature exists for — `status --json` run from
inside a foreign repo must return an executable `next_action.command`.
Workspace bootstrap installs only harness adapters, never checkout runtime
files. Offline; no provider, no quota.
"""
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from agent_coop import coop_adapters
from agent_coop import coop_start
from agent_coop import coopdb
from tests.test_claims import contract_kwargs

INSTALL_ROOT = pathlib.Path(__file__).resolve().parents[1]


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


class ForeignWorkspace(unittest.TestCase):
    """Everything here runs against a throwaway git repo, never this one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "not-coop"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "thing.py").write_text("x = 1\n", encoding="utf-8")
        if git("init", "-q", cwd=self.repo).returncode != 0:
            self.skipTest("git unavailable")
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "t", cwd=self.repo)
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "seed", cwd=self.repo)

    def clean_env(self):
        """No live operator session leaks in, and no test ever writes the
        real ~/.coop switcher registry or the real ~/.claude, ~/.codex and
        ~/.grok skill trees (`init` installs the launch skill there)."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("COOP_")}
        env["COOP_BOARDS_REGISTRY"] = str(
            pathlib.Path(self.tmp.name) / "boards.json")
        env["COOP_GLOBAL_SKILLS_HOME"] = str(
            pathlib.Path(self.tmp.name) / "skillhome")
        return env

    def init_workspace(self, *extra):
        env = self.clean_env()
        env["PYTHONPATH"] = str(INSTALL_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "agent_coop", "init",
             "--workspace", str(self.repo), *extra],
            capture_output=True, text=True, env=env, cwd=str(self.repo))

    def test_init_creates_a_board_and_hides_it_from_git(self):
        result = self.init_workspace()
        self.assertEqual(result.returncode, 0, result.stderr)

        board = self.repo / ".coop" / "board.db"
        self.assertTrue(board.is_file(), result.stdout)
        self.assertIn(".coop", result.stdout)
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertTrue((self.repo / relative).is_file(), result.stdout)
        self.assertIn("adapter installed", result.stdout)

        # The repo must be clean — local state and adapters are invisible to
        # git, and the tracked .gitignore was never touched.
        self.assertEqual(git("status", "--porcelain", cwd=self.repo).stdout, "")
        self.assertFalse((self.repo / ".gitignore").exists())

    def test_init_rejects_adapter_escape_before_creating_board(self):
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.mkdir()
        try:
            (self.repo / ".claude").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")

        result = self.init_workspace()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("adapter_path_invalid", result.stderr)
        self.assertFalse((self.repo / ".coop" / "board.db").exists())
        self.assertFalse((outside / "skills" / "coop" / "SKILL.md").exists())

    def test_init_is_idempotent(self):
        self.assertEqual(self.init_workspace().returncode, 0)
        second = self.init_workspace()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already excluded", second.stdout)

        exclude = (self.repo / ".git" / "info" / "exclude").read_text(
            encoding="utf-8")
        for expected in coop_adapters.git_exclude_entries():
            self.assertEqual(
                [ln for ln in exclude.splitlines()
                 if ln.strip() == expected],
                [expected],
            )
        self.assertIn("adapter current", second.stdout)
        self.assertEqual(git("status", "--porcelain", cwd=self.repo).stdout, "")

    def test_init_registers_the_board_in_the_switcher(self):
        # Creation must pin the board so the dashboard switcher lists it
        # immediately (human or agent `coop init`). Tests redirect the
        # registry via COOP_BOARDS_REGISTRY so the real ~/.coop is untouched.
        registry = pathlib.Path(self.tmp.name) / "boards.json"
        self.assertEqual(self.init_workspace().returncode, 0)
        self.assertTrue(registry.is_file(), "init should write the switcher registry")
        data = json.loads(registry.read_text(encoding="utf-8"))
        board = str((self.repo / ".coop" / "board.db").resolve())
        self.assertIn(board, data)

    def test_init_does_not_touch_the_operator_home_registry(self):
        # Isolation regression: without COOP_BOARDS_REGISTRY,
        # every `coop init --workspace` leaked temp boards into real
        # ~/.coop/boards.json. clean_env always redirects; prove home is stable.
        home_registry = pathlib.Path.home() / ".coop" / "boards.json"
        before = (home_registry.read_text(encoding="utf-8")
                  if home_registry.is_file() else None)
        self.assertEqual(self.init_workspace().returncode, 0)
        after = (home_registry.read_text(encoding="utf-8")
                 if home_registry.is_file() else None)
        self.assertEqual(before, after)

    def test_init_refuses_the_home_directory_as_a_workspace(self):
        # A board in home is discovered by every repository below it. The
        # child sees a throwaway HOME/USERPROFILE so a failed guard could
        # only ever write into the temp directory.
        fake_home = pathlib.Path(self.tmp.name) / "fakehome"
        fake_home.mkdir()
        env = self.clean_env()
        env["PYTHONPATH"] = str(INSTALL_ROOT)
        env["HOME"] = env["USERPROFILE"] = str(fake_home)
        result = subprocess.run(
            [sys.executable, "-m", "agent_coop", "init",
             "--workspace", str(fake_home)],
            capture_output=True, text=True, env=env, cwd=str(self.repo))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("refusing to create a board", result.stderr)
        self.assertIn("home directory", result.stderr)
        self.assertEqual(sorted(p.name for p in fake_home.iterdir()), [])
        # The repository itself is still an ordinary workspace.
        self.assertEqual(self.init_workspace().returncode, 0)

    def test_a_non_git_workspace_is_legal(self):
        plain = pathlib.Path(self.tmp.name) / "plain"
        plain.mkdir()
        env = self.clean_env()
        env["PYTHONPATH"] = str(INSTALL_ROOT)
        result = subprocess.run(
            [sys.executable, "-m", "agent_coop", "init",
             "--workspace", str(plain)],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((plain / ".coop" / "board.db").is_file())
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertTrue((plain / relative).is_file())
        self.assertIn("not a git repo", result.stdout)

    def test_the_routed_command_runs_inside_the_foreign_repo(self):
        """The point of the whole feature.

        Discover the board by walking up from the workspace, read the routed
        command, and execute it verbatim inside a repo that holds no Co-op
        runtime files — exactly what a provider turn does.
        """
        self.assertEqual(self.init_workspace().returncode, 0)
        board = str(self.repo / ".coop" / "board.db")

        # Runtime policy and checkout files stay absent. Only standalone
        # harness adapters are installed into their discovery layers.
        self.assertFalse((self.repo / "coop.py").exists())
        self.assertFalse((self.repo / "SKILL.md").exists())
        self.assertFalse((self.repo / "COOP_GUIDE.md").exists())
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertTrue((self.repo / relative).is_file())

        # Walk-up discovery finds it from a nested source directory.
        self.assertEqual(
            coopdb.discover_board(str(self.repo / "src")),
            str(pathlib.Path(board).resolve()))

        with contextlib.closing(coopdb.connect(board)) as conn:
            coopdb.register_agent(conn, "claude")
            item = coopdb.create_item(
                conn, actor="human", session_id=None,
                **contract_kwargs(title="make the acceptance command pass"))
            coopdb.insert_session(
                conn, session_id="s-foreign", agent_id="claude",
                provider="claude", command=[sys.executable, "-c", "pass"],
                cwd=str(self.repo), max_runtime_s=28800, grace_s=10)

        # Exactly the bindings a provider turn is launched with.
        env = self.clean_env()
        env["PYTHONPATH"] = coop_start.pythonpath_with_install_root(env)
        env.update({"COOP_SESSION_ID": "s-foreign", "COOP_AGENT": "claude",
                    "COOP_AGENT_ID": "claude"})

        status = subprocess.run(
            [sys.executable, "-m", "agent_coop", "status", "--json"],
            capture_output=True, text=True, env=env, cwd=str(self.repo))
        self.assertEqual(status.returncode, 0, status.stderr)
        command = json.loads(status.stdout)["next_action"]["command"]
        self.assertIsNotNone(command, "expected a routed command")

        # It must be the cwd-independent form, not a bare relative coop.py.
        self.assertEqual(tuple(command[:3]), coopdb.CLI_ARGV)
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(pathlib.Path(command[0]).is_absolute())
        self.assertNotEqual(command[0], "python")
        self.assertNotIn("coop.py", command)

        self.assertIn(str(item), command)

        # And it must actually execute there. `python` in the routed command
        # is the agent's interpreter; this process's is the faithful stand-in.
        routed = subprocess.run(
            [sys.executable, *command[1:]],
            capture_output=True, text=True, env=env, cwd=str(self.repo))
        self.assertEqual(routed.returncode, 0,
                         f"routed command failed in the workspace: "
                         f"{routed.stderr}")

        # The claim landed on the board — proof it ran against the right one.
        with contextlib.closing(coopdb.connect(board)) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM items WHERE id=?",
                             (item,)).fetchone()[0],
                "working")


class InstallRootBinding(unittest.TestCase):
    def test_install_root_leads_the_pythonpath(self):
        built = coop_start.pythonpath_with_install_root({})
        self.assertEqual(built, str(INSTALL_ROOT))

    def test_existing_pythonpath_is_preserved_after_it(self):
        built = coop_start.pythonpath_with_install_root(
            {"PYTHONPATH": "/somewhere/else"})
        self.assertEqual(
            built.split(os.pathsep), [str(INSTALL_ROOT), "/somewhere/else"])

    def test_the_install_root_is_never_duplicated(self):
        built = coop_start.pythonpath_with_install_root(
            {"PYTHONPATH": str(INSTALL_ROOT)})
        self.assertEqual(built.split(os.pathsep), [str(INSTALL_ROOT)])


if __name__ == "__main__":
    unittest.main()
