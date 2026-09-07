"""Orchestrator (coop_start) — pure/injected unit tests.

Nothing here launches a real agent CLI or spawns a detached process; the
turn runner, the participant probe, and the dashboard spawn are all injected
or monkeypatched. `invoke_turn` (the one real-CLI function) is never called.
"""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_coop import coop_prompt_cache
from agent_coop import coop_start
from agent_coop import coop_workers
from agent_coop import coopdb
from agent_coop import coop_runner_status
from agent_coop.coop_capabilities import (
    MANIFESTS,
    CapabilityActivationError,
    LaunchProfile,
)


def _fresh_board():
    tmp = tempfile.TemporaryDirectory()
    board = str(pathlib.Path(tmp.name) / "coop" / "board.db")
    pathlib.Path(board).parent.mkdir(parents=True, exist_ok=True)
    conn = coopdb.connect(board)
    coopdb.init_db(conn)
    conn.close()
    return tmp, board


def _seed_done_item(board):
    conn = coopdb.connect(board, require_current=True)
    try:
        def _seed(c):
            c.execute("INSERT OR IGNORE INTO agents(name, registered_at) "
                      "VALUES ('human', ?)", (coopdb.now(),))
            return c.execute(
                "INSERT INTO items(title,status,created_by,created_at,"
                "updated_at) VALUES ('done one','done','human',?,?)",
                (coopdb.now(), coopdb.now())).lastrowid
        coopdb.mutate(conn, _seed)
    finally:
        conn.close()


class ResolveParticipants(unittest.TestCase):
    def test_all_ok_passes_through_in_order(self):
        out = coop_start.resolve_participants(
            ["claude", "codex", "grok"], probe=lambda n: (True, ""))
        self.assertEqual(out["available"], ["claude", "codex", "grok"])
        self.assertEqual(out["skipped"], [])

    def test_skips_carry_reasons_and_preserve_order(self):
        def probe(name):
            return (False, "usage balance exhausted") if name == "grok" \
                else (True, "")
        out = coop_start.resolve_participants(
            ["claude", "grok", "codex"], probe=probe)
        self.assertEqual(out["available"], ["claude", "codex"])
        self.assertEqual(
            out["skipped"], [{"agent": "grok",
                              "reason": "usage balance exhausted"}])

    def test_unknown_provider_skipped_without_probing(self):
        called = []

        def probe(name):
            called.append(name)
            return True, ""
        out = coop_start.resolve_participants(
            ["claude", "gemini"], probe=probe)
        self.assertEqual(out["available"], ["claude"])
        self.assertEqual(
            out["skipped"], [{"agent": "gemini", "reason": "unknown provider"}])
        self.assertNotIn("gemini", called)   # unknown never probed

    def test_402_reason_surfaces(self):
        out = coop_start.resolve_participants(
            ["grok"], probe=lambda n: (False, "API error 402 Payment Required"))
        self.assertEqual(out["available"], [])
        self.assertIn("402", out["skipped"][0]["reason"])

    def test_provider_invoke_map_is_stable(self):
        self.assertEqual(set(coop_start.PROVIDER_INVOKE),
                         {"claude", "codex", "grok"})
        self.assertEqual(coop_start.PROVIDER_INVOKE["grok"][0], "grok")


TASKS = [
    {"id": 7, "status": "todo", "title": "fix the parser"},
    {"id": 9, "status": "working", "title": "add   --json  flag"},
]


class ComposePrompt(unittest.TestCase):
    def _normal(self, **over):
        kw = dict(agent="codex", provider="codex", board_path="/r/coop/board.db",
                  guide_path="COOP_GUIDE.md", tasks=TASKS, round_no=1,
                  propose_mode=False)
        kw.update(over)
        return coop_start.compose_prompt(**kw)

    def test_identity_board_and_self_contained_help_present(self):
        text = self._normal()
        self.assertIn("codex", text)
        self.assertIn("/r/coop/board.db", text)
        self.assertIn("coop --help", text)
        self.assertNotIn("python -m agent_coop --help", text)
        self.assertNotIn("COOP_GUIDE.md", text)

    def test_normal_lists_tasks_and_names_the_verbs(self):
        text = self._normal()
        self.assertIn("#7", text)
        self.assertIn("fix the parser", text)
        self.assertIn("add --json flag", text)   # title whitespace normalised
        for verb in ("coop say", "claim", "review", "complete"):
            self.assertIn(verb, text)

    def test_propose_mode_omits_tasks_and_asks_for_suggestions(self):
        text = self._normal(tasks=[], propose_mode=True)
        self.assertNotIn("#7", text)
        self.assertIn("2-3", text)
        self.assertIn("coop say", text)

    def test_round_two_uses_continue_framing(self):
        first = self._normal(round_no=1)
        later = self._normal(round_no=2)
        self.assertIn("Continue", later)
        self.assertNotIn("Continue", first)

    def test_deterministic_no_ansi(self):
        a = self._normal()
        b = self._normal()
        self.assertEqual(a, b)
        self.assertNotIn("\033[", a)

    def test_plan_phase_is_short_says_only(self):
        text = self._normal(phase="plan")
        self.assertIn("PLANNING HUDDLE", text)
        self.assertIn("coop say", text)
        for absent in ("item claim", "item define", "receipt submit"):
            self.assertNotIn(absent, text)

    def test_execute_phase_has_reclaim_long_lease_and_cross_provider(self):
        text = self._normal(phase="execute", lease_seconds=7200)
        self.assertIn("RECLAIM", text)
        self.assertIn("--lease-seconds 7200", text)
        self.assertIn("DIFFERENT provider", text)

    def test_execute_phase_gives_an_idle_agent_a_critique_turn(self):
        text = self._normal(phase="execute")
        self.assertIn("CRITIQUE", text)
        self.assertIn("IDLE", text)
        self.assertIn("skeptic", text)
        self.assertIn("never rubber-stamp", text)

    def test_execute_phase_requires_second_distinct_provider_approve(self):
        text = self._normal(phase="execute")
        self.assertIn("TWO", text)
        self.assertIn("DISTINCT providers", text)
        self.assertIn("SECOND review", text)
        self.assertIn("Prefer a second-review claim over critique", text)

    def test_plan_phase_stress_tests_rather_than_echoes(self):
        text = self._normal(phase="plan")
        self.assertIn("STRESS-TEST", text)
        self.assertIn("Echoing a peer adds nothing", text)

    def test_lease_seconds_flag_parses_and_passes_through(self):
        from agent_coop import cli as coopcli
        args = coopcli.build_parser().parse_args(
            ["item", "claim", "7", "--intent", "x", "--lease-seconds", "7200"])
        self.assertEqual(args.lease_seconds, 7200.0)

    def test_review_claim_takes_lease_seconds(self):
        from agent_coop import cli as coopcli
        args = coopcli.build_parser().parse_args(
            ["review", "claim", "3", "--intent", "x", "--lease-seconds", "7200"])
        self.assertEqual(args.lease_seconds, 7200.0)

    def test_execute_phase_claims_reviews_with_a_long_lease(self):
        text = self._normal(phase="execute", lease_seconds=7200)
        self.assertIn("coop review claim <id> --lease-seconds 7200", text)

    def test_recover_phase_diagnoses_and_adapts(self):
        text = self._normal(phase="recover", task=1)
        upper = text.upper()
        self.assertIn("RECOVERY", upper)
        self.assertIn("#1", text)                 # names the timed-out task
        for word in ("DIAGNOSE", "SPLIT", "REASSIGN", "NARROW"):
            self.assertIn(word, upper)
        for absent in ("receipt submit", "item complete", "review submit"):
            self.assertNotIn(absent, text)        # short/says-only, no heavy work


class RunCollaboration(unittest.TestCase):
    def _changing_observe(self):
        """Board keeps changing every round (never idle)."""
        counter = {"n": 0}

        def observe():
            counter["n"] += 1
            return {"change_token": counter["n"], "all_done": False}
        return observe

    def test_order_is_participant_major_then_rounds(self):
        seq = []
        journal = coop_start.run_collaboration(
            participants=["claude", "codex"], rounds=2,
            run_turn=lambda a, r: (seq.append((a, r)) or {"agent": a, "round": r}),
            observe=self._changing_observe())
        self.assertEqual(
            seq, [("claude", 1), ("codex", 1), ("claude", 2), ("codex", 2)])
        self.assertEqual(journal["rounds_run"], 2)
        self.assertEqual(journal["stopped_reason"], "rounds_exhausted")
        self.assertEqual(len(journal["turns"]), 4)

    def test_stops_at_rounds_when_board_keeps_changing(self):
        journal = coop_start.run_collaboration(
            participants=["claude"], rounds=3,
            run_turn=lambda a, r: {"agent": a, "round": r},
            observe=self._changing_observe())
        self.assertEqual(journal["rounds_run"], 3)
        self.assertEqual(journal["stopped_reason"], "rounds_exhausted")

    def test_stops_idle_when_change_token_is_constant(self):
        journal = coop_start.run_collaboration(
            participants=["claude", "codex"], rounds=5,
            run_turn=lambda a, r: {"agent": a, "round": r},
            observe=lambda: {"change_token": "frozen", "all_done": False})
        self.assertEqual(journal["stopped_reason"], "idle")
        self.assertEqual(journal["rounds_run"], 1)   # one full round, no change

    def test_stops_all_done_when_observe_flips(self):
        state = {"done": False, "n": 0}

        def observe():
            state["n"] += 1
            return {"change_token": state["n"], "all_done": state["done"]}

        def run_turn(agent, round_no):
            if round_no == 2:
                state["done"] = True
            return {"agent": agent, "round": round_no}
        journal = coop_start.run_collaboration(
            participants=["claude"], rounds=9, run_turn=run_turn,
            observe=observe)
        self.assertEqual(journal["stopped_reason"], "all_done")
        self.assertEqual(journal["rounds_run"], 2)

    def test_empty_participants_short_circuits(self):
        seen = []
        journal = coop_start.run_collaboration(
            participants=[], rounds=3,
            run_turn=lambda a, r: seen.append((a, r)),
            observe=lambda: {"change_token": 0, "all_done": False})
        self.assertEqual(journal["stopped_reason"], "no_participants")
        self.assertEqual(journal["rounds_run"], 0)
        self.assertEqual(seen, [])   # no turns run

    def test_journal_sink_receives_the_journal(self):
        got = []
        journal = coop_start.run_collaboration(
            participants=["claude"], rounds=1,
            run_turn=lambda a, r: {"agent": a, "round": r},
            observe=self._changing_observe(), journal_sink=got.append)
        self.assertEqual(got, [journal])
        self.assertEqual(got[0]["participants"], ["claude"])

    def test_recovery_huddle_fires_once_after_a_timeout(self):
        calls = []

        def run_turn(agent, round_no, phase="execute"):
            calls.append((agent, round_no, phase))
            if agent == "claude" and round_no == 1 and phase == "execute":
                return {"agent": agent, "round": round_no, "note": "timeout"}
            return {"agent": agent, "round": round_no}

        coop_start.run_collaboration(
            participants=["claude", "codex"], rounds=1, run_turn=run_turn,
            observe=self._changing_observe())
        recover = [(a, r) for a, r, p in calls if p == "recover"]
        # one recovery round, every participant, after the timed-out round
        self.assertEqual(recover, [("claude", 1), ("codex", 1)])

    def test_no_recovery_round_without_a_timeout(self):
        phases = []
        coop_start.run_collaboration(
            participants=["claude"], rounds=2,
            run_turn=lambda a, r, phase="execute": (
                phases.append(phase) or {"agent": a, "round": r}),
            observe=self._changing_observe())
        self.assertNotIn("recover", phases)


class ProbeAgent(unittest.TestCase):
    def test_unknown_provider(self):
        self.assertEqual(coop_start.probe_agent("gemini"),
                         (False, "unknown provider"))

    def test_cli_absent_and_present(self):
        with mock.patch.object(coop_start, "resolve_cli", return_value=None):
            ok, reason = coop_start.probe_agent("claude")
        self.assertFalse(ok)
        self.assertIn("cli not found: claude", reason)
        with mock.patch.object(coop_start, "resolve_cli",
                               return_value="C:/x/claude.exe"):
            self.assertEqual(coop_start.probe_agent("claude"), (True, "ok"))


class StartCollaboration(unittest.TestCase):
    def setUp(self):
        self.tmp, self.board = _fresh_board()
        self.addCleanup(self.tmp.cleanup)

    def _sessions(self):
        conn = coopdb.connect(self.board, require_current=True)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT agent_id, status FROM sessions ORDER BY agent_id")]
        finally:
            conn.close()

    def _run(self, *, turn_runner, probe=lambda n: (True, "ok"),
             requested=("claude", "codex", "grok"), rounds=2, plan_rounds=0,
             stop_flag=None):
        return coop_start.start_collaboration(
            board_path=self.board, requested=list(requested), rounds=rounds,
            plan_rounds=plan_rounds, turn_runner=turn_runner, probe=probe,
            stop_flag=stop_flag)

    def test_sessions_opened_for_available_and_all_finished(self):
        skip_grok = lambda n: (False, "balance") if n == "grok" else (True, "ok")
        journal = self._run(turn_runner=lambda **k: {"ok": True},
                            probe=skip_grok)
        self.assertEqual(journal["participants"], ["claude", "codex"])
        self.assertEqual(journal["skipped"],
                         [{"agent": "grok", "reason": "balance"}])
        rows = self._sessions()
        self.assertEqual({r["agent_id"] for r in rows}, {"claude", "codex"})
        self.assertTrue(all(r["status"] != "running" for r in rows),
                        "every orchestrator session must be finished")
        self.assertTrue(pathlib.Path(self.board).parent.joinpath(
            "coop-start-journal.md").exists())

    def test_participant_major_ordering_and_rounds_exhausted(self):
        seq = []

        def runner(**k):
            seq.append((k["agent"], k["round_no"]))
            # change the board each turn so the loop never goes idle
            c = coopdb.connect(self.board, require_current=True)
            try:
                coopdb.post_message(c, k["agent"], f"tick {k['round_no']}")
            finally:
                c.close()
            return {"ok": True}
        journal = self._run(turn_runner=runner, rounds=2)
        self.assertEqual(seq, [("claude", 1), ("codex", 1), ("grok", 1),
                               ("claude", 2), ("codex", 2), ("grok", 2)])
        self.assertEqual(journal["stopped_reason"], "rounds_exhausted")
        self.assertEqual(journal["rounds_run"], 2)

    def test_idle_stops_early_when_board_unchanged(self):
        journal = self._run(turn_runner=lambda **k: {"ok": True}, rounds=5)
        self.assertEqual(journal["stopped_reason"], "idle")
        self.assertEqual(journal["rounds_run"], 1)

    def test_all_done_stops_early(self):
        _seed_done_item(self.board)
        journal = self._run(turn_runner=lambda **k: {"ok": True}, rounds=5)
        self.assertEqual(journal["stopped_reason"], "all_done")

    def test_timed_out_agent_gets_an_escalated_next_turn_budget(self):
        budgets = []

        def runner(**k):
            budgets.append((k["agent"], k["timeout_s"]))
            c = coopdb.connect(self.board, require_current=True)
            try:                              # keep the board changing
                coopdb.post_message(c, k["agent"], f"t{k['round_no']}")
            finally:
                c.close()
            if k["agent"] == "claude" and k["round_no"] == 1:
                return {"ok": False, "note": "timeout"}
            return {"ok": True}

        self._run(turn_runner=runner, requested=("claude", "codex"), rounds=2)
        claude = [t for a, t in budgets if a == "claude"]
        codex = [t for a, t in budgets if a == "codex"]
        self.assertEqual(claude[0], 180.0)                 # base budget
        self.assertIn(360.0, claude)                       # ×2 after the timeout
        self.assertTrue(all(t == 180.0 for t in codex))    # codex not escalated

    def test_stop_flag_halts_at_boundary(self):
        flag = str(pathlib.Path(self.board).parent / "stopme")

        def runner(**k):
            pathlib.Path(flag).write_text("stop", encoding="utf-8")
            return {"ok": True}
        journal = self._run(turn_runner=runner, rounds=5, stop_flag=flag)
        self.assertEqual(journal["stopped_reason"], "stopped")

    def test_planning_phase_runs_before_execution(self):
        seq = []

        def runner(**k):
            seq.append((k["agent"], k["round_no"]))
            c = coopdb.connect(self.board, require_current=True)
            try:                       # change the board so execution isn't idle
                coopdb.post_message(c, k["agent"], "tick")
            finally:
                c.close()
            return {"ok": True}
        journal = self._run(turn_runner=runner, requested=("claude", "codex"),
                            rounds=1, plan_rounds=1)
        phases = [t.get("phase") for t in journal["turns"]]
        # both planning turns precede any execution turn
        self.assertEqual(phases, ["plan", "plan", "execute", "execute"])
        self.assertEqual(journal["plan_rounds"], 1)

    def test_renew_runs_after_a_timed_out_turn(self):
        conn = coopdb.connect(self.board, require_current=True)
        item = coopdb.create_item(
            conn, actor="human", session_id=None, title="t", objective="o",
            scope="s", done_when="d", output_contract="oc", context="c",
            allowed_actions=["x"], stop_conditions=["y"])
        conn.close()
        seen = {}

        def runner(**k):
            if k.get("phase") == "recover":   # the recovery huddle re-invokes;
                return {"ok": True}           # do not re-claim on a recover turn
            c = coopdb.connect(self.board, require_current=True)
            try:            # claim under the orchestrator session, short lease
                res = coopdb.claim_item(
                    c, item_id=item, actor=k["agent"],
                    session_id=k["session_id"], intent="x", lease_seconds=1)
                seen["before"] = res["lease_expires_at"]
            finally:
                c.close()
            return {"ok": False, "note": "timeout"}   # a timed-out turn
        self._run(turn_runner=runner, requested=("claude",), rounds=1)
        conn = coopdb.connect(self.board, require_current=True)
        after = conn.execute(
            "SELECT lease_expires_at FROM claims WHERE item_id=? "
            "ORDER BY claim_id DESC LIMIT 1", (item,)).fetchone()[0]
        conn.close()
        self.assertGreater(after, seen["before"],
                           "the after-turn renew must extend the claim even "
                           "when the turn timed out")

    def test_invoke_turn_is_never_called_here(self):
        # Guard: the injected runner path must not touch the real CLI.
        with mock.patch.object(coop_start, "invoke_turn",
                               side_effect=AssertionError("real CLI launched")):
            self._run(turn_runner=lambda **k: {"ok": True})


class DashboardSlashCommand(unittest.TestCase):
    def setUp(self):
        self.tmp, self.board = _fresh_board()
        self.addCleanup(self.tmp.cleanup)
        from agent_coop import coop_monitor
        self.mon = coop_monitor

    def test_slash_coop_spawns_detached_and_posts_no_say(self):
        spawned = []
        launches = []

        def spawn(db, args):
            spawned.append((db, args))
            root = pathlib.Path(self.tmp.name) / ".coop-runs"
            return self.mon.DetachedRun(
                log_path=str(root / "run.log"),
                status_path=str(root / "run.status.json"),
                trace_path=str(root / "run.trace.jsonl"),
            )

        with mock.patch.object(self.mon, "_spawn_detached",
                               side_effect=spawn), \
             mock.patch.object(coop_start, "probe_agent",
                               return_value=(True, "ok")), \
             mock.patch.object(coopdb, "say",
                               side_effect=AssertionError("say called")):
            notice = self.mon.submit_line(
                self.board, "/coop", 23, launch_sink=launches.append)
        self.assertEqual(spawned, [(self.board, ["--item", "23"])])
        self.assertEqual(notice, "runner: starting")
        self.assertEqual(len(launches), 1)
        self.assertTrue(launches[0].log_path.endswith("run.log"))
        self.assertNotIn("claude", notice)
        self.assertNotIn("log=", notice)

    @unittest.skipUnless(os.name == "nt", "Windows background launch contract")
    def test_detached_launch_hides_console_and_captures_log(self):
        with mock.patch.object(self.mon.subprocess, "Popen") as popen:
            run = self.mon._spawn_detached(
                self.board, ["--item", "24"])
        self.assertIsNotNone(run)
        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        flags = kwargs["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertFalse(flags & getattr(subprocess, "DETACHED_PROCESS", 0))
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(pathlib.Path(kwargs["stdout"].name),
                         pathlib.Path(run.log_path))
        self.assertTrue(kwargs["stdout"].closed)
        self.assertIn("-u", argv)
        self.assertEqual(argv[-2:], ["--item", "24"])
        self.assertEqual(
            kwargs["env"]["COOP_RUN_STATUS_PATH"], run.status_path)
        self.assertEqual(
            kwargs["env"]["COOP_RUN_TRACE_PATH"], run.trace_path)
        self.assertEqual(
            pathlib.Path(run.trace_path).parent,
            pathlib.Path(run.log_path).parent,
        )
        self.assertTrue(
            pathlib.Path(run.trace_path).name.endswith(".trace.jsonl"))
        self.assertEqual(
            coop_runner_status.read_status(run.status_path)["phase"],
            "starting")
        self.assertEqual(
            pathlib.Path(run.status_path).suffixes[-2:],
            [".status", ".json"])

    def test_detached_starting_status_carries_only_caller_pane_and_preserves_env(
            self):
        inherited = {
            "HERDR_PANE_ID": "caller pane/parent",
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "workspace must pass but be ignored",
            "HERDR_TAB_ID": "tab must pass but be ignored",
            "P1_4_ENV_SENTINEL": "preserved exactly",
        }
        with mock.patch.dict(os.environ, inherited, clear=True), \
             mock.patch.object(self.mon.subprocess, "Popen") as popen:
            run = self.mon._spawn_detached(
                self.board,
                ["--item", "24"],
            )

        status = coop_runner_status.read_status(run.status_path)
        self.assertEqual(status["phase"], "starting")
        self.assertEqual(status["herdr"], {
            "caller_pane": "caller pane/parent",
        })
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(
            {key: child_env[key] for key in inherited},
            inherited,
        )

    def test_detached_launch_failure_retains_caller_only_metadata_and_env(self):
        inherited = {
            "HERDR_PANE_ID": "caller pane/launch-failed",
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "ignored metadata source",
            "HERDR_TAB_ID": "ignored metadata source",
            "P1_4_ENV_SENTINEL": "preserved exactly",
        }
        captured = {}

        def fail_launch(_argv, **kwargs):
            captured["env"] = kwargs["env"]
            raise OSError("expected launch failure")

        with mock.patch.dict(os.environ, inherited, clear=True), \
             mock.patch.object(
                 self.mon.subprocess,
                 "Popen",
                 side_effect=fail_launch,
             ), self.assertRaisesRegex(OSError, "expected launch failure"):
            self.mon._spawn_detached(self.board, ["--item", "24"])

        status_path = coop_runner_status.latest_status_path(
            coop_runner_status.runs_dir_for_board(self.board)
        )
        status = coop_runner_status.read_status(status_path)
        self.assertEqual(
            (status["phase"], status["reason"], status["turns"]),
            ("failed", "launch_failed", 0),
        )
        self.assertEqual(status["herdr"], {
            "caller_pane": "caller pane/launch-failed",
        })
        self.assertEqual(
            {key: captured["env"][key] for key in inherited},
            inherited,
        )

    def test_slash_coop_stop_touches_flag(self):
        from agent_coop import coop_autonomous
        with mock.patch.object(coopdb, "say",
                               side_effect=AssertionError("say called")):
            notice = self.mon.submit_line(self.board, "/coop stop", None)
        self.assertIn("stop requested", notice)
        self.assertTrue(pathlib.Path(
            coop_autonomous.stop_flag_path(self.board)).exists())

    def test_unknown_slash_command(self):
        notice = self.mon.submit_line(self.board, "/coopx now", None)
        self.assertEqual(notice, "unknown command: /coopx")

    def test_plain_text_still_posts_a_say(self):
        notice = self.mon.submit_line(self.board, "hello team", None)
        self.assertIn("posted message", notice)


class _FakeTree:
    def __init__(self, *, root_exit=0, events=None):
        self.root_exit = root_exit
        self.events = events if events is not None else []
        self.empty = False

    def poll_root(self):
        self.events.append("poll")
        return self.root_exit

    def graceful_stop(self):
        self.events.append("graceful")
        return True

    def force_stop(self):
        self.events.append("force")
        self.empty = True

    def is_empty(self):
        self.events.append("empty")
        return self.empty

    def close(self):
        self.events.append("close")
        self.empty = True


class _FakePrepared:
    def __init__(self, tree, events):
        self.tree = tree
        self.events = events

    def release(self):
        self.events.append("release")
        return self.tree


class _RecordingTrace:
    def __init__(self, *, fail=False):
        self.records = []
        self.fail = fail

    def emit(self, event, **fields):
        if self.fail:
            raise OSError("trace unavailable")
        self.records.append({"event": event, **fields})
        return True


class InvokeTurnExecResolution(unittest.TestCase):
    """FIX 1: invoke_turn resolves the CLI via resolve_cli so Windows .cmd
    launcher shims are found (subprocess wouldn't find them by bare name)."""

    def test_claude_prompt_cache_hint_is_opt_in_and_idempotent(self):
        base = ["claude.exe", "-p", "--dangerously-skip-permissions"]

        self.assertEqual(
            coop_start.apply_prompt_cache_hint(
                base,
                provider="claude",
                enabled=False,
            ),
            base,
        )
        enabled = coop_start.apply_prompt_cache_hint(
            base,
            provider="claude",
            enabled=True,
        )
        self.assertEqual(
            enabled,
            [
                "claude.exe",
                "--exclude-dynamic-system-prompt-sections",
                "-p",
                "--dangerously-skip-permissions",
            ],
        )
        self.assertEqual(
            coop_start.apply_prompt_cache_hint(
                enabled,
                provider="claude",
                enabled=True,
            ),
            enabled,
        )

        with self.assertRaises(ValueError):
            coop_start.apply_prompt_cache_hint(
                ["codex", "exec"],
                provider="codex",
                enabled=True,
            )

    def test_machine_usage_output_is_provider_scoped_and_idempotent(self):
        claude = ["claude.exe", "-p", "--dangerously-skip-permissions"]
        expected_claude = [
            "claude.exe",
            "--output-format",
            "json",
            "-p",
            "--dangerously-skip-permissions",
        ]
        self.assertEqual(
            coop_start.apply_usage_output(claude, provider="claude"),
            expected_claude,
        )
        self.assertEqual(
            coop_start.apply_usage_output(
                expected_claude,
                provider="claude",
            ),
            expected_claude,
        )
        self.assertEqual(
            coop_start.apply_usage_output(
                ["grok.exe", "--always-approve", "-p"],
                provider="grok",
            ),
            [
                "grok.exe",
                "--always-approve",
                "--output-format",
                "json",
                "-p",
            ],
        )
        self.assertEqual(
            coop_start.apply_usage_output(
                ["codex.exe", "exec"],
                provider="codex",
            ),
            ["codex.exe", "exec"],
        )

    def test_machine_result_parser_does_not_unwrap_unsupported_provider(self):
        raw = '{"result":"agent-authored json"}'
        self.assertEqual(
            coop_start.parse_cli_result("codex", raw),
            (raw, {"usage_observation": "unobserved"}),
        )

    def test_claude_json_result_is_unwrapped_and_usage_is_traced(self):
        captured = {}
        events = []
        trace = _RecordingTrace()
        envelope = json.dumps({
            "type": "result",
            "result": "board write complete",
            "num_turns": 3,
            "modelUsage": {"claude-opus-5": {}},
            "usage": {
                "input_tokens": 80,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 400,
                "output_tokens": 10,
            },
        }).encode("utf-8")

        def tree_factory(argv, **kwargs):
            captured["argv"] = argv
            kwargs["stdout"].write(envelope)
            kwargs["stdout"].flush()
            return _FakePrepared(_FakeTree(events=events), events)

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=tree_factory,
            resolve=lambda _name: "claude.exe",
            trace=trace,
            action={"kind": "continue_task", "item_id": 25},
            turn_id="turn-usage",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["note"], "board write complete")
        self.assertEqual(captured["argv"].count("--output-format"), 1)
        self.assertEqual(
            captured["argv"][captured["argv"].index("--output-format") + 1],
            "json",
        )
        received = next(
            row for row in trace.records
            if row["event"] == "provider_result_received"
        )
        self.assertEqual(received["details"]["model_id"], "claude-opus-5")
        self.assertEqual(received["details"]["uncached_input_tokens"], 80)
        self.assertEqual(received["details"]["cached_input_tokens"], 400)
        self.assertEqual(received["details"]["cache_write_input_tokens"], 20)
        self.assertEqual(received["details"]["output_tokens"], 10)
        self.assertEqual(received["details"]["usage_observation"], "complete")
        self.assertNotIn("board write complete", repr(trace.records))
        self.assertNotIn("modelUsage", repr(trace.records))

    def test_grok_json_result_is_unwrapped_and_usage_is_traced(self):
        events = []
        trace = _RecordingTrace()
        envelope = json.dumps({
            "result": "grok board write complete",
            "model": "grok-code-fast-1",
            "num_turns": 1,
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 8,
                "total_tokens": 108,
                "cache_write_input_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 25},
            },
        }).encode("utf-8")

        def tree_factory(_argv, **kwargs):
            kwargs["stdout"].write(envelope)
            kwargs["stdout"].flush()
            return _FakePrepared(_FakeTree(events=events), events)

        result = coop_start.invoke_turn(
            provider="grok",
            prompt="work",
            session_id="s",
            agent_id="grok",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=tree_factory,
            resolve=lambda _name: "grok.exe",
            trace=trace,
            action={"kind": "continue_task", "item_id": 25},
            turn_id="turn-grok-usage",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["note"], "grok board write complete")
        received = next(
            row for row in trace.records
            if row["event"] == "provider_result_received"
        )
        self.assertEqual(received["details"]["model_id"], "grok-code-fast-1")
        self.assertEqual(received["details"]["uncached_input_tokens"], 75)
        self.assertEqual(received["details"]["usage_observation"], "complete")
        self.assertNotIn("grok board write complete", repr(trace.records))

    def test_plain_provider_output_remains_compatible_but_unobserved(self):
        events = []
        trace = _RecordingTrace()

        def tree_factory(_argv, **kwargs):
            kwargs["stdout"].write(b"plain completion")
            kwargs["stdout"].flush()
            return _FakePrepared(_FakeTree(events=events), events)

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=tree_factory,
            resolve=lambda _name: "claude.exe",
            trace=trace,
            action={"kind": "continue_task", "item_id": 25},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["note"], "plain completion")
        received = next(
            row for row in trace.records
            if row["event"] == "provider_result_received"
        )
        self.assertEqual(
            received["details"]["usage_observation"],
            "unobserved",
        )

    def test_opt_in_claude_hint_reaches_final_argv_before_print_flag(self):
        captured = {}
        events = []

        def tree_factory(argv, **kwargs):
            captured["argv"] = argv
            # Read at spawn time: the turn closes the channel when it ends.
            captured["stdin"] = kwargs["stdin"].read().decode("utf-8")
            return _FakePrepared(_FakeTree(events=events), events)

        result = coop_start.invoke_turn(
            provider="claude",
            prompt=coop_prompt_cache.PROMPT_PREFIX,
            session_id="cache-session",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=tree_factory,
            resolve=lambda _name: "claude.exe",
            prompt_cache_hint=True,
        )

        self.assertTrue(result["ok"])
        argv = captured["argv"]
        hint = "--exclude-dynamic-system-prompt-sections"
        self.assertEqual(argv.count(hint), 1)
        self.assertLess(argv.index(hint), argv.index("-p"))
        # The prompt travels on stdin, never in argv.
        self.assertNotIn(coop_prompt_cache.PROMPT_PREFIX, argv)
        self.assertEqual(captured["stdin"], coop_prompt_cache.PROMPT_PREFIX)

    def test_resolves_full_shim_path_as_argv0(self):
        captured = {}
        events = []
        tree = _FakeTree(events=events)

        def tree_factory(argv, **kw):
            captured["argv"] = argv
            captured["env"] = kw.get("env")
            captured["stdin"] = kw["stdin"].read().decode("utf-8")
            captured["stdout"] = kw.get("stdout")
            captured["stderr"] = kw.get("stderr")
            return _FakePrepared(tree, events)

        with mock.patch.object(coop_start, "resolve_cli",
                               return_value=r"C:\shims\claude.cmd"):
            result = coop_start.invoke_turn(
                provider="claude", prompt="hello", session_id="s1",
                agent_id="claude", board_path="b.db", cwd=".", timeout_s=5,
                tree_factory=tree_factory)
        self.assertTrue(result["ok"])
        self.assertTrue(result["tree_empty"])
        self.assertEqual(captured["argv"][0], r"C:\shims\claude.cmd")
        self.assertIn("-p", captured["argv"])          # template tail kept
        # A .cmd shim spawns through cmd.exe, so the prompt stays off the
        # command line entirely and arrives on stdin.
        self.assertNotIn("hello", captured["argv"])
        self.assertEqual(captured["stdin"], "hello")
        self.assertEqual(captured["env"]["COOP_SESSION_ID"], "s1")
        # Routed commands pin this interpreter. PYTHONPATH also keeps module
        # fallbacks importable from the foreign workspace.
        install_root = str(pathlib.Path(coop_start.__file__).resolve().parents[1])
        self.assertEqual(
            captured["env"]["PYTHONPATH"].split(os.pathsep)[0], install_root)
        self.assertIsNotNone(captured["stdout"])
        self.assertIsNotNone(captured["stderr"])
        self.assertLess(events.index("release"), events.index("poll"))
        self.assertLess(events.index("poll"), events.index("close"))
        self.assertLess(events.index("close"), events.index("empty"))

    def test_codex_injects_only_required_coop_binding_into_shell_policy(self):
        captured = {}
        events = []

        def tree_factory(argv, **kw):
            captured["argv"] = argv
            captured["env"] = kw["env"]
            captured["stdin"] = kw["stdin"].read().decode("utf-8")
            return _FakePrepared(_FakeTree(events=events), events)

        result = coop_start.invoke_turn(
            provider="codex", prompt="work", session_id="s-codex",
            agent_id="codex", board_path=r"C:\repo\coop\board.db",
            cwd=".", timeout_s=5, action_lease_seconds=3600, item_id=24,
            tree_factory=tree_factory,
            resolve=lambda _name: r"C:\shims\codex.cmd")

        self.assertTrue(result["ok"])
        overrides = [
            captured["argv"][i + 1]
            for i, value in enumerate(captured["argv"][:-1])
            if value == "-c"
        ]
        for key in (
                "COOP_SESSION_ID", "COOP_AGENT", "COOP_AGENT_ID",
                "COOP_PROVIDER", "COOP_DB", "COOP_ACTION_LEASE_SECONDS",
                "COOP_ITEM_ID",
                # Without this one, Codex shell tools cannot import module
                # fallbacks because `inherit = "core"` drops the outer env.
                "PYTHONPATH"):
            self.assertTrue(any(
                value.startswith(f"shell_environment_policy.set.{key}=")
                for value in overrides), key)
        self.assertFalse(any(
            "shell_environment_policy.inherit=all" in value
            for value in overrides))
        # codex.cmd spawns through cmd.exe, so the prompt is read from stdin
        # and argv ends with codex's `-` stdin marker.
        self.assertNotIn("work", captured["argv"])
        self.assertEqual(captured["argv"][-1], "-")
        self.assertEqual(captured["stdin"], "work")

    def test_timeout_drains_owned_tree_before_returning(self):
        events = []
        tree = _FakeTree(root_exit=None, events=events)
        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += max(float(seconds), 1.0)

        result = coop_start.invoke_turn(
            provider="claude", prompt="hello", session_id="s-timeout",
            agent_id="claude", board_path="b.db", cwd=".", timeout_s=1,
            shutdown_grace_s=0, tree_factory=lambda *a, **k: _FakePrepared(
                tree, events), monotonic=monotonic, sleep=sleep,
            resolve=lambda _name: r"C:\shims\claude.cmd")

        self.assertFalse(result["ok"])
        self.assertEqual(result["note"], "timeout")
        self.assertEqual(result["classification"], "turn_timeout")
        self.assertIs(result["retryable"], True)
        self.assertIs(result["process_started"], True)
        self.assertIs(result["session_created"], False)
        self.assertTrue(result["tree_empty"])
        self.assertLess(events.index("graceful"), events.index("force"))
        self.assertLess(events.index("force"), events.index("close"))
        final_empty = len(events) - 1 - events[::-1].index("empty")
        self.assertLess(events.index("close"), final_empty)

    def test_exact_postwrite_completion_exits_normally_inside_grace(self):
        events = []
        trace = _RecordingTrace()
        clock = {"now": 0.0}
        completion_calls = []

        class TimedExitTree(_FakeTree):
            def poll_root(inner_self):
                events.append("poll")
                return 0 if clock["now"] >= 1.5 else None

        tree = TimedExitTree(root_exit=None, events=events)

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += float(seconds)

        def completion_probe():
            completion_calls.append(clock["now"])
            return clock["now"] >= 0.5

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s-postwrite-normal",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=10,
            tree_factory=lambda *a, **k: _FakePrepared(tree, events),
            monotonic=monotonic,
            sleep=sleep,
            resolve=lambda _name: "claude.exe",
            trace=trace,
            progress_probe=lambda: 1 if clock["now"] >= 0.5 else 0,
            progress_before=0,
            completion_probe=completion_probe,
            post_write_exit_grace_s=2.0,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["exit"], 0)
        self.assertGreaterEqual(completion_calls[0], 0.5)
        self.assertNotIn("graceful", events)
        self.assertNotIn(
            "postwrite_grace_expired",
            [row["event"] for row in trace.records],
        )

    def test_postwrite_grace_expiry_uses_owned_cleanup_and_fails_typed(self):
        events = []
        trace = _RecordingTrace()
        tree = _FakeTree(root_exit=None, events=events)
        clock = {"now": 0.0}
        completion_calls = []

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += float(seconds)

        def completion_probe():
            completion_calls.append(clock["now"])
            return True

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s-postwrite-expired",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=10,
            shutdown_grace_s=0,
            tree_factory=lambda *a, **k: _FakePrepared(tree, events),
            monotonic=monotonic,
            sleep=sleep,
            resolve=lambda _name: "claude.exe",
            trace=trace,
            progress_probe=lambda: 1,
            progress_before=0,
            completion_probe=completion_probe,
            post_write_exit_grace_s=0.25,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "postwrite_exit_timeout")
        self.assertEqual(result["note"], "postwrite_exit_timeout")
        self.assertIs(result["retryable"], False)
        self.assertIs(result["session_created"], False)
        self.assertEqual(events.count("graceful"), 1)
        self.assertEqual(events.count("force"), 1)
        self.assertLess(events.index("graceful"), events.index("force"))
        self.assertGreaterEqual(clock["now"], 0.25)
        self.assertGreaterEqual(len(completion_calls), 2)
        names = [row["event"] for row in trace.records]
        self.assertEqual(names.count("postwrite_grace_expired"), 1)
        terminal = next(
            row for row in trace.records
            if row["event"] == "provider_result_received"
        )
        self.assertIs(terminal["details"]["timed_out"], False)

    def test_completion_probe_cannot_bypass_progress_or_timeout(self):
        for case in ("no_board_write", "false_predicate"):
            with self.subTest(case=case):
                events = []
                trace = _RecordingTrace()
                tree = _FakeTree(root_exit=None, events=events)
                clock = {"now": 0.0}
                calls = []

                def monotonic():
                    return clock["now"]

                def sleep(seconds):
                    clock["now"] += max(float(seconds), 0.1)

                def completion_probe():
                    calls.append(clock["now"])
                    return case == "no_board_write"

                result = coop_start.invoke_turn(
                    provider="claude",
                    prompt="work",
                    session_id=f"s-{case}",
                    agent_id="claude",
                    board_path="b.db",
                    cwd=".",
                    timeout_s=0.3,
                    shutdown_grace_s=0,
                    tree_factory=lambda *a, **k: _FakePrepared(tree, events),
                    monotonic=monotonic,
                    sleep=sleep,
                    resolve=lambda _name: "claude.exe",
                    trace=trace,
                    progress_probe=(
                        (lambda: 0)
                        if case == "no_board_write"
                        else (lambda: 1)
                    ),
                    progress_before=0,
                    completion_probe=completion_probe,
                    post_write_exit_grace_s=0,
                )

                self.assertEqual(result["classification"], "turn_timeout")
                self.assertIs(result["retryable"], True)
                names = [row["event"] for row in trace.records]
                self.assertNotIn("postwrite_grace_expired", names)
                if case == "no_board_write":
                    self.assertEqual(calls, [])
                else:
                    self.assertGreater(len(calls), 0)

    def test_timeout_cleanup_failure_is_authoritative_and_retained(self):
        events = []
        registry = coop_workers.RunCleanupRegistry()
        clock = {"now": 0.0}

        class RetainedTree(_FakeTree):
            def __init__(self):
                super().__init__(root_exit=None, events=events)
                self.allow_cleanup = False

            def force_stop(self):
                events.append("force")

            def close(self):
                events.append("close")
                if not self.allow_cleanup:
                    raise OSError("private process path")
                self.empty = True

        tree = RetainedTree()

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += max(float(seconds), 1.0)

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="hello",
            session_id="s-timeout",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=1,
            shutdown_grace_s=0,
            tree_factory=lambda *a, **k: _FakePrepared(tree, events),
            monotonic=monotonic,
            sleep=sleep,
            resolve=lambda _name: r"C:\shims\claude.cmd",
            cleanup_registry=registry,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["classification"],
            "process_tree_cleanup_failed",
        )
        self.assertIs(result["retryable"], False)
        self.assertIs(result["cleanup_retained"], True)
        self.assertNotIn("private process path", result["note"])
        self.assertEqual(registry.pending, 1)

        tree.allow_cleanup = True
        registry.drain()
        self.assertEqual(registry.pending, 0)
        self.assertTrue(tree.empty)

    def test_missing_cli_returns_typed_failure_and_never_spawns(self):
        def must_not_prepare(*_args, **_kwargs):
            raise AssertionError("must not prepare")

        with mock.patch.object(coop_start, "resolve_cli",
                               return_value=None):
            result = coop_start.invoke_turn(
                provider="grok", prompt="hi", session_id="s", agent_id="grok",
                board_path="b", cwd=".", timeout_s=5,
                tree_factory=must_not_prepare)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["exit"])
        self.assertEqual(result["note"], "cli not found on PATH: grok")
        self.assertEqual(result["classification"], "worker_start_failed")
        self.assertIs(result["retryable"], False)
        self.assertIs(result["process_started"], False)
        self.assertIs(result["session_created"], False)

    def test_profile_construction_failure_never_spawns(self):
        def must_not_prepare(*_args, **_kwargs):
            raise AssertionError("must not prepare")

        with mock.patch.object(
                coop_start,
                "build_launch_profile",
                side_effect=CapabilityActivationError(
                    "unsupported isolation contract",
                )):
            result = coop_start.invoke_turn(
                provider="claude",
                prompt="hi",
                session_id="s",
                agent_id="claude",
                board_path="b",
                cwd=".",
                timeout_s=5,
                tree_factory=must_not_prepare,
                resolve=lambda _name: "claude.exe",
                capability_manifest=MANIFESTS["board_core"],
            )

        self.assertFalse(result["ok"])
        self.assertIsNone(result["exit"])
        self.assertTrue(
            result["note"].startswith("capability_activation_failed:")
        )
        self.assertEqual(
            result["classification"],
            "capability_activation_failed",
        )
        self.assertIs(result["retryable"], False)
        self.assertIs(result["process_started"], False)
        self.assertIs(result["session_created"], False)

    def test_profile_cleanup_runs_once_after_nonzero_exit_and_tree_drain(self):
        events = []
        cleanup = mock.Mock(side_effect=lambda: events.append("profile-cleanup"))
        profile = LaunchProfile(
            argv=["provider.exe"],
            env={},
            external_server_names=(),
            cleanup=cleanup,
        )
        tree = _FakeTree(root_exit=7, events=events)

        with mock.patch.object(
                coop_start,
                "build_launch_profile",
                return_value=profile,
        ):
            result = coop_start.invoke_turn(
                provider="claude",
                prompt="hi",
                session_id="s",
                agent_id="claude",
                board_path="b",
                cwd=".",
                timeout_s=5,
                tree_factory=lambda *a, **k: _FakePrepared(tree, events),
                resolve=lambda _name: "claude.exe",
                capability_manifest=MANIFESTS["board_core"],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["exit"], 7)
        self.assertTrue(result["tree_empty"])
        cleanup.assert_called_once_with()
        final_empty = len(events) - 1 - events[::-1].index("empty")
        self.assertLess(final_empty, events.index("profile-cleanup"))

    def test_monthly_limit_returns_redacted_non_retryable_failure(self):
        events = []
        tree = _FakeTree(root_exit=1, events=events)

        def tree_factory(*_args, **kwargs):
            kwargs["stderr"].write(
                b"You've hit your monthly spend limit - raise it in settings"
            )
            kwargs["stderr"].flush()
            return _FakePrepared(tree, events)

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="hi",
            session_id="s",
            agent_id="claude",
            board_path="b",
            cwd=".",
            timeout_s=5,
            tree_factory=tree_factory,
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["exit"], 1)
        self.assertTrue(result["tree_empty"])
        self.assertEqual(
            result["classification"],
            "provider_quota_exhausted",
        )
        self.assertIs(result["retryable"], False)
        self.assertEqual(result["note"], "provider_quota_exhausted")
        self.assertNotIn("monthly spend limit", result["note"])

    def test_profile_cleanup_runs_once_after_worker_start_failure(self):
        events = []
        cleanup = mock.Mock(side_effect=lambda: events.append("profile-cleanup"))
        profile = LaunchProfile(
            argv=["provider.exe"],
            env={},
            external_server_names=(),
            cleanup=cleanup,
        )

        def fail_to_prepare(*_args, **_kwargs):
            raise RuntimeError("forced launch failure")

        with mock.patch.object(
                coop_start,
                "build_launch_profile",
                return_value=profile,
        ):
            result = coop_start.invoke_turn(
                provider="claude",
                prompt="hi",
                session_id="s",
                agent_id="claude",
                board_path="b",
                cwd=".",
                timeout_s=5,
                tree_factory=fail_to_prepare,
                resolve=lambda _name: "claude.exe",
                capability_manifest=MANIFESTS["board_core"],
            )

        self.assertFalse(result["ok"])
        self.assertTrue(
            result["note"].startswith("worker_start_failed:")
        )
        self.assertEqual(result["classification"], "worker_start_failed")
        self.assertIs(result["retryable"], True)
        self.assertIs(result["process_started"], False)
        self.assertIs(result["session_created"], False)
        cleanup.assert_called_once_with()
        self.assertEqual(events, ["profile-cleanup"])

    def test_process_tree_cleanup_retries_before_reporting_success(self):
        events = []

        class FlakyCloseTree(_FakeTree):
            def __init__(self):
                super().__init__(events=events)
                self.close_attempts = 0

            def close(self):
                self.close_attempts += 1
                events.append("close")
                if self.close_attempts == 1:
                    raise OSError("private process path")
                self.empty = True

        tree = FlakyCloseTree()
        result = coop_start.invoke_turn(
            provider="claude",
            prompt="hi",
            session_id="s",
            agent_id="claude",
            board_path="b",
            cwd=".",
            timeout_s=5,
            tree_factory=lambda *a, **k: _FakePrepared(tree, events),
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["tree_empty"])
        self.assertEqual(tree.close_attempts, 2)
        self.assertNotIn("private process path", result["note"])

    def test_successful_explicit_provider_session_is_confirmed(self):
        events = []
        captured = {}

        def tree_factory(argv, **_kwargs):
            captured["argv"] = argv
            return _FakePrepared(
                _FakeTree(events=events),
                events,
            )

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="hi",
            session_id="s",
            agent_id="claude",
            board_path="b",
            cwd=".",
            timeout_s=5,
            tree_factory=tree_factory,
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
            provider_session_id="provider-session",
        )

        self.assertTrue(result["ok"])
        self.assertIs(result["process_started"], True)
        self.assertIs(result["session_created"], True)
        self.assertIn("--session-id", captured["argv"])
        self.assertNotIn("--resume", captured["argv"])

    def test_unsupported_session_flag_is_safe_for_cold_fallback(self):
        events = []

        def tree_factory(_argv, **kwargs):
            kwargs["stderr"].write(
                b"error: unknown option '--session-id'\n"
            )
            kwargs["stderr"].flush()
            return _FakePrepared(
                _FakeTree(root_exit=2, events=events),
                events,
            )

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="hi",
            session_id="s",
            agent_id="claude",
            board_path="b",
            cwd=".",
            timeout_s=5,
            tree_factory=tree_factory,
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
            provider_session_id="provider-session",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["classification"],
            "provider_session_unsupported",
        )
        self.assertIs(result["retryable"], False)
        self.assertIs(result["cold_fallback_safe"], True)
        self.assertIs(result["session_created"], False)
        self.assertEqual(result["note"], "provider_session_unsupported")
        self.assertNotIn("unknown option", result["note"])

    def test_profile_cleanup_runs_once_after_timeout(self):
        events = []
        cleanup = mock.Mock(side_effect=lambda: events.append("profile-cleanup"))
        profile = LaunchProfile(
            argv=["provider.exe"],
            env={},
            external_server_names=(),
            cleanup=cleanup,
        )
        tree = _FakeTree(root_exit=None, events=events)
        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += max(float(seconds), 1.0)

        with mock.patch.object(
                coop_start,
                "build_launch_profile",
                return_value=profile,
        ):
            result = coop_start.invoke_turn(
                provider="claude",
                prompt="hi",
                session_id="s",
                agent_id="claude",
                board_path="b",
                cwd=".",
                timeout_s=1,
                shutdown_grace_s=0,
                tree_factory=lambda *a, **k: _FakePrepared(tree, events),
                monotonic=monotonic,
                sleep=sleep,
                resolve=lambda _name: "claude.exe",
                capability_manifest=MANIFESTS["board_core"],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["note"], "timeout")
        cleanup.assert_called_once_with()
        final_empty = len(events) - 1 - events[::-1].index("empty")
        self.assertLess(final_empty, events.index("profile-cleanup"))

    def test_successful_cold_turn_emits_ordered_milestones_once(self):
        events = []
        progress = {"value": 0}
        trace = _RecordingTrace()

        class ProgressTree(_FakeTree):
            def poll_root(self):
                progress["value"] = 1
                return super().poll_root()

        tree = ProgressTree(events=events)

        class Prepared(_FakePrepared):
            def release(self):
                names = [record["event"] for record in trace.records]
                self.assertions(names)
                return super().release()

            @staticmethod
            def assertions(names):
                assert names[-2:] == [
                    "process_tree_prepared",
                    "capability_activation_completed",
                ]
                assert "provider_process_started" not in names

        def tree_factory(_argv, **kwargs):
            kwargs["stdout"].write(b"provider-started")
            kwargs["stdout"].flush()
            return Prepared(tree, events)

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=tree_factory,
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["local_code"],
            trace=trace,
            turn_id="turn-1",
            action={"kind": "continue_task", "item_id": 25},
            workflow_recipe="standard_three",
            progress_probe=lambda: progress["value"],
            progress_before=0,
        )

        self.assertTrue(result["ok"])
        self.assertIs(result["process_started"], True)
        self.assertIs(result["session_created"], False)
        names = [record["event"] for record in trace.records]
        self.assertEqual(names, [
            "worker_start_requested",
            "capability_activation_requested",
            "process_tree_prepared",
            "capability_activation_completed",
            "provider_process_started",
            "prompt_submitted",
            "first_provider_output",
            "first_board_mutation",
            "provider_result_received",
            "worker_idle",
            "capability_shutdown",
            "worker_shutdown",
        ])
        self.assertEqual(len(names), len(set(names)))
        terminal = next(
            record
            for record in trace.records
            if record["event"] == "provider_result_received"
        )
        self.assertIn(
            "actor_last_mutation_to_exit_ms",
            terminal["details"],
        )
        self.assertNotIn("satisfied_to_exit_ms", terminal["details"])
        for record in trace.records:
            self.assertEqual(record["turn_id"], "turn-1")
            self.assertEqual(record["agent"], "claude")
            self.assertEqual(record["provider"], "claude")
            self.assertEqual(record["action"], "continue_task")
            self.assertEqual(record["capability_set"], "local_code")
            self.assertEqual(
                record["details"]["workflow_recipe"],
                "standard_three",
            )

    def test_release_failure_never_claims_provider_process_started(self):
        trace = _RecordingTrace()

        class FailingPrepared:
            def release(self):
                raise RuntimeError("release failed")

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=lambda *_args, **_kwargs: FailingPrepared(),
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
            trace=trace,
            action={"kind": "huddle_post", "item_id": 25},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "worker_start_failed")
        self.assertIs(result["process_started"], False)
        names = [record["event"] for record in trace.records]
        self.assertIn("process_tree_prepared", names)
        self.assertNotIn("provider_process_started", names)

    def test_no_output_or_board_write_omits_optional_milestones(self):
        events = []
        trace = _RecordingTrace()
        tree = _FakeTree(events=events)

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=lambda *a, **k: _FakePrepared(tree, events),
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
            trace=trace,
            action={"kind": "huddle_post", "item_id": 25},
            progress_probe=lambda: 0,
            progress_before=0,
        )

        self.assertTrue(result["ok"])
        names = [record["event"] for record in trace.records]
        self.assertNotIn("first_provider_output", names)
        self.assertNotIn("first_board_mutation", names)
        self.assertEqual(len(names), len(set(names)))

    def test_launch_failure_emits_bounded_terminal_milestones(self):
        trace = _RecordingTrace()

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=lambda *a, **k: (
                (_ for _ in ()).throw(RuntimeError("forced"))
            ),
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
            trace=trace,
            action={"kind": "huddle_post", "item_id": 25},
        )

        self.assertFalse(result["ok"])
        names = [record["event"] for record in trace.records]
        self.assertEqual(names, [
            "worker_start_requested",
            "capability_activation_requested",
            "provider_result_received",
            "worker_idle",
            "capability_shutdown",
            "worker_shutdown",
        ])
        self.assertEqual(len(names), len(set(names)))

    def test_timeout_milestones_are_emitted_at_most_once(self):
        trace = _RecordingTrace()
        events = []
        tree = _FakeTree(root_exit=None, events=events)
        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        def sleep(seconds):
            clock["now"] += max(float(seconds), 1.0)

        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=1,
            shutdown_grace_s=0,
            tree_factory=lambda *a, **k: _FakePrepared(tree, events),
            monotonic=monotonic,
            sleep=sleep,
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
            trace=trace,
            action={"kind": "huddle_post", "item_id": 25},
        )

        self.assertFalse(result["ok"])
        names = [record["event"] for record in trace.records]
        self.assertEqual(len(names), len(set(names)))
        terminal = next(
            record
            for record in trace.records
            if record["event"] == "provider_result_received"
        )
        self.assertTrue(terminal["details"]["timed_out"])

    def test_capability_cleanup_failure_is_traced_once_and_fails_turn(self):
        trace = _RecordingTrace()
        events = []
        registry = coop_workers.RunCleanupRegistry()
        cleanup_allowed = {"value": False}

        def cleanup():
            if not cleanup_allowed["value"]:
                raise RuntimeError("forced cleanup failure")

        profile = LaunchProfile(
            argv=["provider.exe"],
            env={},
            external_server_names=(),
            cleanup=mock.Mock(side_effect=cleanup),
        )
        with mock.patch.object(
                coop_start,
                "build_launch_profile",
                return_value=profile,
        ):
            result = coop_start.invoke_turn(
                provider="claude",
                prompt="work",
                session_id="s",
                agent_id="claude",
                board_path="b.db",
                cwd=".",
                timeout_s=5,
                tree_factory=lambda *a, **k: _FakePrepared(
                    _FakeTree(events=events),
                    events,
                ),
                resolve=lambda _name: "claude.exe",
                capability_manifest=MANIFESTS["board_core"],
                trace=trace,
                action={"kind": "huddle_post", "item_id": 25},
                cleanup_registry=registry,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["note"], "capability_shutdown_failed")
        self.assertIs(result["retryable"], False)
        self.assertIs(result["cleanup_retained"], True)
        self.assertEqual(registry.pending, 1)
        names = [record["event"] for record in trace.records]
        self.assertEqual(names.count("capability_shutdown"), 1)
        shutdown = next(
            record
            for record in trace.records
            if record["event"] == "capability_shutdown"
        )
        self.assertEqual(
            shutdown["details"]["error_class"],
            "RuntimeError",
        )
        cleanup_allowed["value"] = True
        registry.drain()
        self.assertEqual(registry.pending, 0)

    def test_trace_failure_is_fail_soft_for_turn_result(self):
        events = []
        result = coop_start.invoke_turn(
            provider="claude",
            prompt="work",
            session_id="s",
            agent_id="claude",
            board_path="b.db",
            cwd=".",
            timeout_s=5,
            tree_factory=lambda *a, **k: _FakePrepared(
                _FakeTree(events=events),
                events,
            ),
            resolve=lambda _name: "claude.exe",
            capability_manifest=MANIFESTS["board_core"],
            trace=_RecordingTrace(fail=True),
            action={"kind": "huddle_post", "item_id": 25},
        )

        self.assertTrue(result["ok"])


class ComposePromptScoping(unittest.TestCase):
    """FIX 4: the prompt tells agents to scope messages to the task and to
    focus on a specific task when one is named."""

    def test_task_focus_and_scoped_say(self):
        prompt = coop_start.compose_prompt(
            agent="codex", provider="codex", board_path="b", guide_path="g",
            tasks=[{"item_id": 4, "status": "todo", "title": "X"}],
            round_no=1, propose_mode=False, task=4)
        self.assertIn("Focus on task #4", prompt)
        self.assertIn("coop say --item", prompt)

    def test_no_task_still_scopes_say(self):
        prompt = coop_start.compose_prompt(
            agent="codex", provider="codex", board_path="b", guide_path="g",
            tasks=[], round_no=1, propose_mode=False, task=None)
        self.assertIn("coop say --item", prompt)
        self.assertNotIn("Focus on task #", prompt)


if __name__ == "__main__":
    unittest.main()
