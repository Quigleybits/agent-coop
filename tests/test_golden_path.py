"""Golden path: one install, then it works.

A. Bare `coop` (no subcommand) opens the dashboard, exactly like
   `coop monitor`; `coop --help` and `coop --version` are unchanged.
B. Opening the dashboard in a folder with no board creates the board and
   the repo adapters (the switcher's create-on-open) and shows the notice
   on the dashboard notice line; the plain-redraw fallback prints it once
   to stderr. Home, parents of home, filesystem roots and the temp root are
   refused with the existing one-line message, exit 1, nothing written.
C. Dashboard open and `coop init` install the `/coop` launch skill
   user-globally (Claude Code, Codex, Grok); hash-managed, announced once,
   opt-out with `--no-global-skills` or `COOP_NO_GLOBAL_SKILLS=1`; a write
   failure is a one-line notice.

Offline; no provider. Every test redirects the switcher registry with
COOP_BOARDS_REGISTRY and the global skill root with COOP_GLOBAL_SKILLS_HOME,
so the operator's real ~/.coop, ~/.claude, ~/.codex and ~/.grok are never
written.
"""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_coop import cli as coopcli
from agent_coop import coop_adapters
from agent_coop import coop_monitor
from agent_coop import coopdb

INSTALL_ROOT = pathlib.Path(__file__).resolve().parents[1]
CREATED = "created board in "
SKILL_INSTALLED = ("installed /coop skill for Claude Code, Codex and Grok "
                   "(~/.claude/skills, ~/.codex/skills, ~/.grok/skills)")
SKILL_FAILED = "could not install /coop skill"
REFUSED = "refusing to create a board"
GLOBAL_FILES = (
    pathlib.Path(".claude/skills/coop/SKILL.md"),
    pathlib.Path(".codex/skills/coop/SKILL.md"),
    pathlib.Path(".grok/skills/coop/SKILL.md"),
)
GLOBAL_STATE = pathlib.Path(".coop/global-skills.json")


class GoldenPathCase(unittest.TestCase):
    """Every run happens in throwaway directories, never in this repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.work = self.root / "workdir"
        self.work.mkdir()
        self.skill_home = self.root / "skillhome"
        self.skill_home.mkdir()
        self.registry = self.root / "boards.json"
        found = coopdb.discover_board(str(self.work))
        if found:
            self.skipTest(f"an ancestor of the temp dir holds a board: {found}")

    @property
    def cwd_board(self):
        return self.work / ".coop" / "board.db"

    def sandbox_env(self, **extra):
        env = {"COOP_BOARDS_REGISTRY": str(self.registry),
               "COOP_GLOBAL_SKILLS_HOME": str(self.skill_home),
               "COOP_DB": "", "COOP_DB_PATH": "", "COOP_DEFAULT_DB": "",
               "COOP_NO_GLOBAL_SKILLS": ""}
        env.update(extra)
        return env

    @contextlib.contextmanager
    def chdir(self, cwd):
        """The cwd-default board path is relative (`.coop/board.db`), so the
        process must really be in the sandbox, not just report it."""
        previous = os.getcwd()
        os.chdir(str(cwd))
        try:
            yield
        finally:
            os.chdir(previous)

    @contextlib.contextmanager
    def in_process(self, cwd=None, **env_extra):
        """Run `coopcli.main` in this process with the dashboard stubbed
        out; yields the `run_dashboard` mock."""
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox_env(**env_extra)), \
                self.chdir(cwd or self.work), \
                mock.patch.object(coop_monitor, "dashboard_supported",
                                  return_value=True), \
                mock.patch.object(coop_monitor, "run_dashboard",
                                  return_value=True) as run, \
                contextlib.redirect_stderr(err):
            run.stderr = err
            yield run

    def notice_of(self, run):
        run.assert_called_once()
        return run.call_args.kwargs.get("notice", "")

    def assert_global_skills_installed(self, home=None):
        home = pathlib.Path(home or self.skill_home)
        bundled = coop_adapters.bundled_adapters()
        expected = {GLOBAL_FILES[0]: bundled["claude"],
                    GLOBAL_FILES[1]: bundled["agents"],
                    GLOBAL_FILES[2]: bundled["agents"]}
        for relative, text in expected.items():
            path = home / relative
            self.assertTrue(path.is_file(), path)
            self.assertFalse(path.is_symlink(), path)
            self.assertEqual(path.read_text(encoding="utf-8"), text, path)
        state = json.loads((home / GLOBAL_STATE).read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 1)
        self.assertEqual(
            set(state["managed_hashes"]),
            {relative.as_posix() for relative in GLOBAL_FILES})

    def assert_global_skills_absent(self, home=None):
        home = pathlib.Path(home or self.skill_home)
        self.assertEqual(sorted(p.name for p in home.iterdir()), [],
                         list(home.iterdir()))


class BareCoopIsTheDashboard(GoldenPathCase):
    """(A) `coop` == `coop monitor`, root options included."""

    def test_no_subcommand_routes_to_monitor(self):
        self.assertEqual(coopcli._default_argv([]), ["monitor"])

    def test_root_options_without_a_subcommand_still_route_to_monitor(self):
        self.assertEqual(coopcli._default_argv(["--db", "x.db"]),
                         ["--db", "x.db", "monitor"])
        self.assertEqual(coopcli._default_argv(["--json"]),
                         ["--json", "monitor"])
        self.assertEqual(coopcli._default_argv(["--no-global-skills"]),
                         ["--no-global-skills", "monitor"])

    def test_a_subcommand_passes_through_unchanged(self):
        self.assertEqual(coopcli._default_argv(["agents"]), ["agents"])
        self.assertEqual(coopcli._default_argv(["monitor", "--interval", "5"]),
                         ["monitor", "--interval", "5"])
        self.assertEqual(coopcli._default_argv(["--db", "x", "status"]),
                         ["--db", "x", "status"])

    def test_bare_coop_opens_the_dashboard_like_monitor(self):
        parser = coopcli.build_parser()
        args = parser.parse_args(coopcli._default_argv([], parser))
        self.assertEqual(args.cmd, "monitor")
        self.assertEqual(args.interval, 2)
        self.assertIs(args.fn, coopcli.cmd_monitor)
        # The bare form routes through the same lifecycle as `monitor`:
        # not a read command (it may create), and it owns its connection.
        self.assertFalse(coopcli._reads_only(args))
        explicit = parser.parse_args(["monitor"])
        self.assertEqual(vars(args), vars(explicit))

    def test_help_and_version_are_unchanged(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    [sys.executable, "-m", "agent_coop", flag],
                    capture_output=True, text=True, timeout=120,
                    env=dict(os.environ, PYTHONPATH=str(INSTALL_ROOT)),
                    cwd=str(self.work), stdin=subprocess.DEVNULL)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage: coop", result.stdout)
                self.assertIn("monitor", result.stdout)
                self.assertIn("--no-global-skills", result.stdout)
        result = subprocess.run(
            [sys.executable, "-m", "agent_coop", "--version"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONPATH=str(INSTALL_ROOT)),
            cwd=str(self.work), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("coop "), result.stdout)
        self.assertFalse((self.work / ".coop").exists())


class DashboardOpenCreatesTheBoard(GoldenPathCase):
    """(B) the dashboard creates a missing board and says so."""

    def test_monitor_in_an_empty_folder_creates_board_and_adapters(self):
        with self.in_process() as run:
            coopcli.main(["monitor"])
            # The cwd-default path is relative; resolve it while still there.
            opened = pathlib.Path(run.call_args.args[0]).resolve()
        self.assertTrue(self.cwd_board.is_file())
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertTrue((self.work / relative).is_file(), relative)
        self.assertTrue(
            (self.work / coop_adapters.ADAPTER_STATE_PATH).is_file())
        notice = self.notice_of(run)
        self.assertIn(CREATED, notice)
        self.assertIn(coopdb.display_board_path(self.cwd_board), notice)
        self.assertEqual(opened, self.cwd_board.resolve())
        self.assertEqual(run.call_args.kwargs["interval"], 2)
        # Board creation records the board like any dashboard open.
        recorded = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertIn(str(self.cwd_board.resolve()), recorded)

    def test_bare_coop_creates_the_board_too(self):
        with self.in_process() as run:
            coopcli.main([])
        self.assertTrue(self.cwd_board.is_file())
        self.assertIn(CREATED, self.notice_of(run))

    def test_an_existing_board_opens_without_a_creation_notice(self):
        with self.in_process():
            coopcli.main(["monitor"])
        stamp = self.cwd_board.stat().st_mtime_ns
        with self.in_process() as run:
            coopcli.main(["monitor"])
        self.assertNotIn(CREATED, self.notice_of(run))
        self.assertEqual(self.cwd_board.stat().st_mtime_ns, stamp)

    def test_the_plain_fallback_gets_the_same_notice(self):
        with mock.patch.dict(os.environ, self.sandbox_env()), \
                self.chdir(self.work), \
                mock.patch.object(coop_monitor, "dashboard_supported",
                                  return_value=False), \
                mock.patch.object(coopcli, "monitor") as plain:
            coopcli.main(["monitor"])
        self.assertTrue(self.cwd_board.is_file())
        plain.assert_called_once()
        self.assertIn(CREATED, plain.call_args.kwargs["notice"])

    def test_the_plain_loop_prints_the_notice_once_to_stderr(self):
        with self.in_process():
            coopcli.main(["monitor"])
        err = io.StringIO()
        out = io.StringIO()
        ticks = iter([None, None, KeyboardInterrupt()])

        def sleep(_seconds):
            tick = next(ticks)
            if isinstance(tick, BaseException):
                raise tick

        with mock.patch.object(coopcli.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(out):
            coopcli.monitor(str(self.cwd_board), interval=1,
                            notice="created board in ~/projects/app")
        self.assertEqual(err.getvalue().count("created board in"), 1)
        self.assertNotIn("created board in", out.getvalue())

    def test_an_explicit_missing_db_is_an_error_not_a_creation(self):
        target = self.root / "named.db"
        with self.in_process() as run, \
                self.assertRaises(SystemExit) as caught:
            coopcli.main(["--db", str(target), "monitor"])
        self.assertEqual(caught.exception.code, 1)
        run.assert_not_called()
        self.assertFalse(target.exists())
        self.assertFalse(self.cwd_board.exists())
        self.assertIn("no board", run.stderr.getvalue())


class RefusedDashboardOpens(GoldenPathCase):
    """(B) guards unchanged: home, above home, a root, the temp root."""

    def setUp(self):
        super().setUp()
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        self.home_patch = mock.patch.object(
            pathlib.Path, "home", classmethod(lambda cls: self.fake_home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def assert_refused(self, cwd, reason, argv=("monitor",)):
        with self.in_process(cwd=cwd) as run, \
                self.assertRaises(SystemExit) as caught:
            coopcli.main(list(argv))
        self.assertEqual(caught.exception.code, 1)
        run.assert_not_called()
        lines = [ln for ln in run.stderr.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, run.stderr.getvalue())
        self.assertIn(REFUSED, lines[0])
        self.assertIn(reason, lines[0])
        self.assertFalse((pathlib.Path(cwd) / ".coop").exists())
        self.assertFalse((pathlib.Path(cwd) / ".claude").exists())
        self.assertFalse((pathlib.Path(cwd) / ".agents").exists())
        self.assertFalse(self.registry.exists())
        # A refused open installs nothing user-global either.
        self.assert_global_skills_absent()

    def test_home_is_refused(self):
        self.assert_refused(self.fake_home, "home directory")

    def test_bare_coop_in_home_is_refused(self):
        self.assert_refused(self.fake_home, "home directory", argv=())

    def test_a_parent_of_home_is_refused(self):
        self.assert_refused(self.fake_home.parent,
                            "contains your home directory")

    def test_the_temp_root_is_refused(self):
        temp_root = pathlib.Path(tempfile.gettempdir())
        if coopdb.discover_board(str(temp_root)):
            self.skipTest("an ancestor of the temp root holds a board")
        # The fake home of this class sits under the temp directory, which
        # would trip the "contains your home" guard first; a home elsewhere
        # (it need not exist) lets the temp-root guard speak.
        elsewhere = pathlib.Path(temp_root.anchor) / "coop-test-home-absent"
        with mock.patch.object(pathlib.Path, "home",
                               classmethod(lambda cls: elsewhere)):
            self.assert_refused(temp_root, "temp directory")

    def test_a_project_below_home_is_an_ordinary_workspace(self):
        project = self.fake_home / "projects" / "app"
        project.mkdir(parents=True)
        with self.in_process(cwd=project) as run:
            coopcli.main(["monitor"])
        self.assertTrue((project / ".coop" / "board.db").is_file())
        self.assertIn(CREATED, self.notice_of(run))
        self.assertFalse((self.fake_home / ".coop").exists())


class GlobalSkillInstall(GoldenPathCase):
    """(C) `coop_adapters.install_global_skills`: hash-managed copies."""

    def test_writes_the_three_files_and_state_under_home(self):
        results = coop_adapters.install_global_skills(home=self.skill_home)
        self.assertEqual([r["status"] for r in results],
                         ["installed"] * 3)
        self.assertEqual([r["name"] for r in results],
                         ["claude", "codex", "grok"])
        self.assert_global_skills_installed()
        self.assertEqual(coop_adapters.global_skills_notice(results),
                         SKILL_INSTALLED)

    def test_the_second_run_is_idempotent_and_silent(self):
        coop_adapters.install_global_skills(home=self.skill_home)
        stamps = {relative: (self.skill_home / relative).stat().st_mtime_ns
                  for relative in GLOBAL_FILES}
        results = coop_adapters.install_global_skills(home=self.skill_home)
        self.assertEqual([r["status"] for r in results], ["current"] * 3)
        self.assertEqual(coop_adapters.global_skills_notice(results), "")
        for relative, stamp in stamps.items():
            self.assertEqual(
                (self.skill_home / relative).stat().st_mtime_ns, stamp)

    def test_a_local_edit_is_preserved(self):
        coop_adapters.install_global_skills(home=self.skill_home)
        edited = self.skill_home / GLOBAL_FILES[1]
        edited.write_text("# my own codex skill\n", encoding="utf-8")
        results = coop_adapters.install_global_skills(home=self.skill_home)
        by_name = {r["name"]: r["status"] for r in results}
        self.assertEqual(by_name, {"claude": "current", "codex": "preserved",
                                   "grok": "current"})
        self.assertEqual(edited.read_text(encoding="utf-8"),
                         "# my own codex skill\n")
        self.assertEqual(coop_adapters.global_skills_notice(results), "")

    def test_an_unchanged_managed_file_is_updated(self):
        old = {"claude": "# claude v1\n", "agents": "# agents v1\n"}
        coop_adapters.install_global_skills(home=self.skill_home,
                                            templates=old)
        results = coop_adapters.install_global_skills(home=self.skill_home)
        self.assertEqual([r["status"] for r in results], ["updated"] * 3)
        self.assert_global_skills_installed()
        self.assertTrue(
            coop_adapters.global_skills_notice(results).startswith(
                "updated /coop skill for Claude Code, Codex and Grok"))

    def test_a_symlinked_destination_is_never_written_through(self):
        outside = self.root / "outside.md"
        outside.write_text("keep me\n", encoding="utf-8")
        link = self.skill_home / GLOBAL_FILES[0]
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        results = coop_adapters.install_global_skills(home=self.skill_home)
        self.assertEqual(results[0]["status"], "skipped")
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue((self.skill_home / GLOBAL_FILES[1]).is_file())

    def test_an_unwritable_target_raises_os_error(self):
        # A file where the skills directory should be: mkdir fails.
        blocker = self.skill_home / ".claude"
        blocker.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaises(OSError):
            coop_adapters.install_global_skills(home=self.skill_home)

    def test_home_resolution_honours_the_seam_then_path_home(self):
        with mock.patch.dict(os.environ,
                             {"COOP_GLOBAL_SKILLS_HOME": str(self.skill_home)}):
            self.assertEqual(coop_adapters.global_skills_home(),
                             self.skill_home)
        with mock.patch.dict(os.environ, {"COOP_GLOBAL_SKILLS_HOME": ""}), \
                mock.patch.object(pathlib.Path, "home",
                                  classmethod(lambda cls: self.root)):
            self.assertEqual(coop_adapters.global_skills_home(), self.root)


class FirstRunInstallsTheSkill(GoldenPathCase):
    """(C) the CLI surfaces: dashboard open and `coop init`."""

    def test_dashboard_open_installs_and_announces_once(self):
        with self.in_process() as run:
            coopcli.main(["monitor"])
        self.assert_global_skills_installed()
        notice = self.notice_of(run)
        self.assertIn(SKILL_INSTALLED, notice)
        self.assertIn(CREATED, notice)
        with self.in_process() as run:
            coopcli.main(["monitor"])
        self.assertNotIn("/coop skill", self.notice_of(run))

    def test_init_installs_and_announces_once(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox_env()), \
                contextlib.redirect_stdout(out):
            coopcli.main(["init", "--workspace", str(self.work)])
        self.assert_global_skills_installed()
        self.assertEqual(out.getvalue().count(SKILL_INSTALLED), 1,
                         out.getvalue())
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox_env()), \
                contextlib.redirect_stdout(out):
            coopcli.main(["init", "--workspace", str(self.work)])
        self.assertNotIn("/coop skill", out.getvalue())

    def test_the_flag_skips_the_install(self):
        with self.in_process() as run:
            coopcli.main(["--no-global-skills", "monitor"])
        self.assert_global_skills_absent()
        self.assertNotIn("/coop skill", self.notice_of(run))
        self.assertTrue(self.cwd_board.is_file())
        with self.in_process() as run:
            coopcli.main(["--no-global-skills"])
        self.assert_global_skills_absent()
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox_env()), \
                contextlib.redirect_stdout(out):
            coopcli.main(["--no-global-skills", "init",
                          "--workspace", str(self.work)])
        self.assert_global_skills_absent()
        self.assertNotIn("/coop skill", out.getvalue())

    def test_the_environment_opt_out_skips_the_install(self):
        with self.in_process(COOP_NO_GLOBAL_SKILLS="1") as run:
            coopcli.main(["monitor"])
        self.assert_global_skills_absent()
        self.assertNotIn("/coop skill", self.notice_of(run))
        self.assertTrue(self.cwd_board.is_file())

    def test_a_write_failure_is_a_one_line_notice_not_an_error(self):
        blocker = self.skill_home / ".claude"
        blocker.write_text("not a directory\n", encoding="utf-8")
        with self.in_process() as run:
            coopcli.main(["monitor"])
        notice = self.notice_of(run)
        self.assertIn(SKILL_FAILED, notice)
        self.assertNotIn("Traceback", run.stderr.getvalue())
        self.assertTrue(self.cwd_board.is_file(),
                        "the board still opens when the skill cannot install")
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox_env()), \
                contextlib.redirect_stdout(out):
            coopcli.main(["init", "--workspace", str(self.work)])
        failures = [ln for ln in out.getvalue().splitlines()
                    if SKILL_FAILED in ln]
        self.assertEqual(len(failures), 1, out.getvalue())

    def test_a_missing_home_is_a_notice(self):
        def no_home(cls):
            raise RuntimeError("Could not determine home directory")

        with mock.patch.object(pathlib.Path, "home", classmethod(no_home)), \
                self.in_process(COOP_GLOBAL_SKILLS_HOME="") as run:
            coopcli.main(["monitor"])
        self.assertIn(SKILL_FAILED, self.notice_of(run))
        self.assertTrue(self.cwd_board.is_file())


class GoalFromTheShell(GoldenPathCase):
    """(F) `coop "<goal>"`: the dashboard's TASKS Enter from the shell.

    A goal is recognised in exactly two shapes: one positional argument
    that contains whitespace (the user quoted a sentence), or everything
    after `--`. A single word or several unquoted words are a usage error
    (exit 2) with a quoting hint, so a typo or a retired command can never
    launch a run. A goal is a write command: the workspace guards and
    auto-provision apply, then the launch skill installs. Works without a
    terminal, so it is the Linux one-liner.
    """

    GOAL_LINE = "created task #1 (draft) · runner: starting · watch with: coop"
    HINT = 'to run a goal, quote it: coop "…"  (or: coop -- …)'

    def split(self, argv):
        return coopcli._split_goal(argv, coopcli.build_parser())

    def test_one_quoted_argument_with_a_space_is_a_goal(self):
        self.assertEqual(self.split(["fix the build"]), ([], ["fix the build"]))
        self.assertEqual(self.split(["--json", "fix it"]),
                         (["--json"], ["fix it"]))
        self.assertEqual(self.split(["--db", "x.db", "fix it"]),
                         (["--db", "x.db"], ["fix it"]))
        self.assertEqual(self.split(["--no-global-skills", "fix it"]),
                         (["--no-global-skills"], ["fix it"]))
        # The quoted form may begin with a command word: one element.
        self.assertEqual(self.split(["status the build"]),
                         ([], ["status the build"]))
        self.assertEqual(self.split(["add\ta test"]), ([], ["add\ta test"]))

    def test_a_command_word_first_is_that_command(self):
        for argv in (["status"], ["status", "the", "build"], ["monitor"],
                     ["--json", "tasks"], ["init", "--workspace", "."]):
            with self.subTest(argv=argv):
                self.assertIsNone(self.split(argv))

    def test_unquoted_words_or_a_single_word_are_not_a_goal(self):
        for argv in (["fix", "the", "build"], ["fix"], ["add"],
                     ["assign", "1", "claude"], ["no-such-command"],
                     ["--json", "fix", "it"], ["fix", "the build"]):
            with self.subTest(argv=argv):
                self.assertIsNone(self.split(argv))

    def test_double_dash_makes_any_words_a_goal(self):
        self.assertEqual(self.split(["--", "status", "the", "build"]),
                         ([], ["status", "the", "build"]))
        self.assertEqual(self.split(["--json", "--", "monitor", "x"]),
                         (["--json"], ["monitor", "x"]))
        self.assertEqual(self.split(["--", "--weird", "goal"]),
                         ([], ["--weird", "goal"]))
        self.assertEqual(self.split(["--", "fix"]), ([], ["fix"]))

    def test_no_words_is_not_a_goal(self):
        for argv in ([], ["--db", "x.db"], ["--json"], ["--"],
                     ["--json", "--"]):
            with self.subTest(argv=argv):
                self.assertIsNone(self.split(argv))

    def test_an_unquoted_goal_is_a_usage_error_with_the_hint(self):
        for argv in (["fix", "the", "build"], ["add"],
                     ["assign", "1", "claude"], ["--json", "fix", "it"]):
            with self.subTest(argv=argv):
                with self.goal_run() as run, \
                        self.assertRaises(SystemExit) as caught:
                    coopcli.main(argv)
                self.assertEqual(caught.exception.code, 2)
                self.assertIn(self.HINT, run["err"].getvalue())
                self.assertEqual(run["out"].getvalue(), "")
                run["spawn"].assert_not_called()
                self.assertFalse(self.cwd_board.exists())
                self.assertFalse((self.work / ".coop").exists())
        self.assert_global_skills_absent()

    def test_an_unknown_option_before_the_goal_is_an_argparse_error(self):
        err = io.StringIO()
        with self.in_process(), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as caught:
            coopcli.main(["--bogus", "fix it"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--bogus", err.getvalue())
        self.assertFalse(self.cwd_board.exists())

    @contextlib.contextmanager
    def goal_run(self, cwd=None, spawn=None, **env_extra):
        """Run `coopcli.main` with the detached launch stubbed; yields a
        dict with the spawn mock and captured stdout/stderr."""
        out, err = io.StringIO(), io.StringIO()
        run = coop_monitor.DetachedRun(
            log_path=str(self.work / ".coop" / ".coop-runs"
                         / "run-20260830T120000Z-abcd1234.log"),
            status_path=str(self.work / ".coop" / ".coop-runs"
                            / "run-20260830T120000Z-abcd1234.status.json"),
            trace_path=str(self.work / ".coop" / ".coop-runs"
                           / "run-20260830T120000Z-abcd1234.trace.jsonl"))
        capture = {"out": out, "err": err}
        with mock.patch.dict(os.environ, self.sandbox_env(**env_extra)), \
                self.chdir(cwd or self.work), \
                mock.patch.object(coop_monitor, "_spawn_detached",
                                  side_effect=spawn or (lambda *a: run)) \
                as spawned, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            capture["spawn"] = spawned
            yield capture

    def item_titles(self):
        conn = coopdb.connect(str(self.cwd_board), require_current=True)
        try:
            return [row["title"] for row in conn.execute(
                "SELECT title FROM items ORDER BY id")]
        finally:
            conn.close()

    def test_a_goal_creates_the_board_the_item_and_launches(self):
        with self.goal_run() as run:
            coopcli.main(["fix the build"])
        self.assertEqual(run["out"].getvalue().strip(), self.GOAL_LINE)
        self.assertTrue(self.cwd_board.is_file())
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertTrue((self.work / relative).is_file(), relative)
        self.assertEqual(self.item_titles(), ["fix the build"])
        run["spawn"].assert_called_once()
        self.assertEqual(run["spawn"].call_args.args[1], ["--item", "1"])
        self.assertEqual(
            pathlib.Path(run["spawn"].call_args.args[0]).resolve(),
            self.cwd_board.resolve())
        # Auto-provision announced the file list first, on stderr.
        self.assertIn("creating .coop/board.db", run["err"].getvalue())
        self.assertIn("auto-created board", run["err"].getvalue())
        # The launch skill installed and was announced on stderr, once.
        self.assert_global_skills_installed()
        self.assertEqual(run["err"].getvalue().count(SKILL_INSTALLED), 1)

    def test_a_quoted_goal_may_begin_with_a_command_word(self):
        with self.goal_run() as run:
            coopcli.main(["status the build"])
        self.assertEqual(run["out"].getvalue().strip(), self.GOAL_LINE)
        self.assertEqual(self.item_titles(), ["status the build"])

    def test_json_shape(self):
        with self.goal_run() as run:
            coopcli.main(["--json", "fix the build"])
        payload = json.loads(run["out"].getvalue())
        self.assertEqual(payload, {"item_id": 1,
                                   "run": "run-20260830T120000Z-abcd1234",
                                   "launched": True})
        self.assertNotIn("creating", run["out"].getvalue())

    def test_a_failed_launch_keeps_the_item_and_says_so(self):
        def boom(*_args):
            raise RuntimeError("boom")

        with self.goal_run(spawn=boom) as run, \
                self.assertRaises(SystemExit) as caught:
            coopcli.main(["fix it"])
        self.assertEqual(caught.exception.code, 1)
        line = run["out"].getvalue().strip()
        self.assertTrue(line.startswith(
            "created task #1 (draft) · launch failed: RuntimeError: boom"),
            line)
        self.assertTrue(line.endswith(" · watch with: coop"), line)
        self.assertEqual(self.item_titles(), ["fix it"])
        with self.goal_run(spawn=boom) as run, \
                self.assertRaises(SystemExit):
            coopcli.main(["--json", "fix again"])
        self.assertEqual(json.loads(run["out"].getvalue()),
                         {"item_id": 2, "run": None, "launched": False})

    def test_double_dash_goal_beginning_with_a_command_word(self):
        with self.goal_run() as run:
            coopcli.main(["--", "status", "the", "build"])
        self.assertEqual(run["out"].getvalue().strip(), self.GOAL_LINE)
        self.assertEqual(self.item_titles(), ["status the build"])

    def test_status_stays_the_read_command(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox_env()), \
                self.chdir(self.work), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as caught:
            coopcli.main(["status", "the", "build"])
        self.assertEqual(caught.exception.code, 2)   # argparse: extra words
        self.assertNotIn(self.HINT, err.getvalue())  # a command, not a goal
        self.assertFalse(self.cwd_board.exists())
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.sandbox_env()), \
                self.chdir(self.work), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as caught:
            coopcli.main(["status"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("no board here", err.getvalue())
        self.assertFalse(self.cwd_board.exists())
        self.assert_global_skills_absent()

    def test_a_goal_in_home_is_refused_and_writes_nothing(self):
        fake_home = self.root / "home"
        fake_home.mkdir()
        with mock.patch.object(pathlib.Path, "home",
                               classmethod(lambda cls: fake_home)), \
                self.goal_run(cwd=fake_home) as run, \
                self.assertRaises(SystemExit) as caught:
            coopcli.main(["fix it"])
        self.assertEqual(caught.exception.code, 1)
        run["spawn"].assert_not_called()
        lines = [ln for ln in run["err"].getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, run["err"].getvalue())
        self.assertIn(REFUSED, lines[0])
        self.assertIn("home directory", lines[0])
        self.assertEqual(run["out"].getvalue(), "")
        self.assertEqual(sorted(p.name for p in fake_home.iterdir()), [])
        self.assert_global_skills_absent()

    def test_the_opt_out_flag_applies_to_a_goal(self):
        with self.goal_run() as run:
            coopcli.main(["--no-global-skills", "fix it"])
        self.assertEqual(run["out"].getvalue().strip(), self.GOAL_LINE)
        self.assert_global_skills_absent()

    def test_help_names_the_goal_form(self):
        result = subprocess.run(
            [sys.executable, "-m", "agent_coop", "--help"],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONPATH=str(INSTALL_ROOT)),
            cwd=str(self.work), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        flat = " ".join(result.stdout.split())   # argparse wraps the epilog
        self.assertIn('coop "<goal>"', flat)
        self.assertIn("coop -- <goal words>", flat)


class EndToEndPlainLoop(GoldenPathCase):
    """Bare `coop` in a real child process with no terminal: the plain loop
    creates the board, prints the notice once, and keeps redrawing."""

    def run_until_timeout(self, *argv, seconds=8, **env_extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("COOP_")}
        env.update(self.sandbox_env(**env_extra))
        env["PYTHONPATH"] = str(INSTALL_ROOT)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "agent_coop", *argv],
                capture_output=True, text=True, timeout=seconds, env=env,
                cwd=str(self.work), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as expired:
            stderr = expired.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            return None, stderr
        return result.returncode, result.stderr

    def test_bare_coop_creates_the_board_and_announces(self):
        code, stderr = self.run_until_timeout()
        self.assertIsNone(code, f"the dashboard loop exited early:\n{stderr}")
        self.assertTrue(self.cwd_board.is_file())
        for relative in coop_adapters.ADAPTER_DESTINATIONS.values():
            self.assertTrue((self.work / relative).is_file(), relative)
        self.assertEqual(stderr.count(CREATED), 1, stderr)
        self.assertEqual(stderr.count(SKILL_INSTALLED), 1, stderr)
        self.assert_global_skills_installed()

    def test_monitor_with_the_opt_out_flag(self):
        code, stderr = self.run_until_timeout(
            "monitor", "--interval", "1", COOP_NO_GLOBAL_SKILLS="1")
        self.assertIsNone(code, stderr)
        self.assertTrue(self.cwd_board.is_file())
        self.assertIn(CREATED, stderr)
        self.assertNotIn("/coop skill", stderr)
        self.assert_global_skills_absent()


if __name__ == "__main__":
    unittest.main()
