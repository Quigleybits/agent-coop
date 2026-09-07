"""Explicit ``coop start --herdr`` preflight and mirror contracts."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import textwrap
import threading
from unittest import mock

import pytest

from agent_coop import (
    cli,
    coop_autonomous,
    coop_herdr,
    coop_runner_status,
    coop_start,
    coop_turn_trace,
    coopdb,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
QUICK_TWO_TAGS = (
    "recipe:quick-two",
    "quick:bounded",
    "quick:low-risk",
    "quick:reversible",
    "quick:no-research",
    "quick:no-three-party",
    "quick:no-high-authority",
)


class _RecordingHerdrAdapter:
    def __init__(self, *, spawn_failures=None, teardown_failures=()):
        self.spawn_failures = dict(spawn_failures or {})
        self.teardown_failures = list(teardown_failures)
        self.spawn_calls = []
        self.teardown_calls = []
        self.workspace_calls = []

    def spawn_mirror(self, run_id, provider, **kwargs):
        self.spawn_calls.append((run_id, provider, kwargs))
        failure = self.spawn_failures.get(provider)
        if failure is not None:
            raise failure
        return f"opaque-pane::{provider}"

    def workspace_id(self, run_id):
        self.workspace_calls.append(run_id)
        return f"opaque-workspace::{run_id}"

    def teardown(self, names):
        self.teardown_calls.append(tuple(names))
        if self.teardown_failures:
            raise self.teardown_failures.pop(0)


class _RejectingHerdrAdapter:
    def spawn_mirror(self, *_args, **_kwargs):
        raise AssertionError("mirror spawned")

    def teardown(self, *_args, **_kwargs):
        raise AssertionError("mirror teardown called")

    def workspace_id(self, *_args, **_kwargs):
        raise AssertionError("workspace accessor called")


class _RecordingPool:
    def __init__(self):
        self.stop_calls = 0

    def stop_all(self):
        self.stop_calls += 1

    def worker(self, _provider):
        return None


def _runner_board(tmp_path, *, quick_two=False):
    workspace = tmp_path / "target-workspace"
    board = workspace / ".coop" / "board.db"
    board.parent.mkdir(parents=True)
    conn = coopdb.connect(str(board))
    coopdb.init_db(conn)
    item_id = coopdb.create_item(
        conn,
        actor="human",
        session_id=None,
        title="herdr target",
        objective="Exercise mirror setup",
    )
    if quick_two:
        conn.execute(
            "UPDATE items SET allowed_actions=? WHERE id=?",
            (json.dumps(QUICK_TWO_TAGS), item_id),
        )
        conn.commit()
    conn.close()
    return workspace, board.resolve(), item_id


def _run_p13_main(
    tmp_path,
    adapter,
    *,
    participants=("claude", "codex", "grok"),
    unavailable=None,
    extra_argv=(),
    quick_two=False,
    trace_path=None,
    worker_pool=None,
    worker_cleanups=(),
    expected_exception=None,
    environment_overrides=None,
    run_side_effect=None,
):
    workspace, board, item_id = _runner_board(
        tmp_path,
        quick_two=quick_two,
    )
    runner = mock.Mock(
        return_value=("stopped", 0, []),
        side_effect=run_side_effect,
    )
    prepared = mock.Mock(return_value=(
        worker_pool,
        tuple(worker_cleanups),
        dict(unavailable or {}),
    ))
    environment = dict(os.environ)
    environment.pop("COOP_RUN_STATUS_PATH", None)
    environment.pop("COOP_RUN_TRACE_PATH", None)
    environment.pop("HERDR_PANE_ID", None)
    environment.update(environment_overrides or {})
    if trace_path is not None:
        environment["COOP_RUN_TRACE_PATH"] = str(trace_path)
    real_connect = coopdb.connect
    opened_connections = []

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened_connections.append(conn)
        return conn

    with mock.patch.dict(os.environ, environment, clear=True), \
         mock.patch.object(
             coop_start,
             "resolve_participants",
             return_value={"available": list(participants), "skipped": []},
         ), \
         mock.patch.object(
             coop_start,
             "resolved_provider_argv",
             side_effect=lambda provider: [provider],
         ), \
         mock.patch.object(
             coop_autonomous,
             "prepare_opt_in_worker_pool",
             prepared,
         ), \
         mock.patch.object(
             coop_autonomous.coopdb,
             "connect",
             side_effect=tracked_connect,
         ), \
         mock.patch.object(coop_autonomous, "run_autonomous", runner), \
         contextlib.redirect_stdout(io.StringIO()):
        argv = [
            "--db",
            str(board),
            "--item",
            str(item_id),
            "--herdr",
            "--fresh-sessions",
            *extra_argv,
        ]
        if expected_exception is None:
            code = coop_autonomous.main(argv, herdr_adapter=adapter)
            caught_exception = None
        else:
            with pytest.raises(expected_exception) as caught:
                coop_autonomous.main(argv, herdr_adapter=adapter)
            code = None
            caught_exception = caught.value

    if trace_path is not None:
        status_path = coop_runner_status.status_path_for_run_artifact(
            trace_path
        )
    else:
        status_path = coop_runner_status.latest_status_path(
            coop_runner_status.runs_dir_for_board(board)
        )
    return {
        "board": board,
        "code": code,
        "exception": caught_exception,
        "item_id": item_id,
        "opened_connections": opened_connections,
        "prepared": prepared,
        "runner": runner,
        "status_path": status_path,
        "workspace": workspace.resolve(),
    }


def _fresh_python(script, argv):
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _failed_probe(*, stdout="private stdout", stderr="private stderr"):
    return subprocess.CompletedProcess(
        args=["herdr", "api", "snapshot"],
        returncode=1,
        stdout=stdout,
        stderr=stderr,
    )


def _successful_probe():
    return subprocess.CompletedProcess(
        args=["herdr", "api", "snapshot"],
        returncode=0,
        stdout=json.dumps({
            "result": {
                "type": "session_snapshot",
                "snapshot": {"version": "synthetic"},
            },
        }),
        stderr="",
    )


def _probe_runner(response):
    def run(*_args, **_kwargs):
        if isinstance(response, BaseException):
            raise response
        return response

    return run


@pytest.mark.parametrize("target", [
    ["--item", "24"],
    ["--all"],
])
def test_public_start_parser_accepts_herdr_for_each_explicit_target(target):
    args = cli.build_parser().parse_args(["start", *target, "--herdr"])

    assert args.herdr is True


@pytest.mark.parametrize("entrypoint", [
    [sys.executable, "coop.py"],
    [sys.executable, "-m", "agent_coop"],
])
def test_both_public_entrypoints_recognize_herdr_before_target_validation(
        entrypoint):
    completed = subprocess.run(
        [*entrypoint, "start", "--herdr"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "one of the arguments --item --all is required" in completed.stderr
    assert "unrecognized arguments" not in completed.stderr


def test_fresh_start_without_herdr_never_imports_adapter_and_keeps_argv():
    argv = [
        "start", "--board", "C:/work/board.db", "--item", "24",
        "--agents", "claude,codex,grok", "--interval", "1.5",
        "--timeout-seconds", "90", "--max-turns", "12",
        "--max-idle-rounds", "8", "--max-noop-cycles", "2",
        "--persistent-provider", "codex",
        "--prompt-cache-provider", "claude",
        "--no-prompt-hydration", "--token-efficient",
        "--no-mechanical-precommit", "--no-prompt-cache",
        "--fresh-sessions", "--dry-run",
    ]
    completed = _fresh_python(
        """
        import builtins
        import json
        import sys

        from agent_coop import cli, coop_autonomous

        state = {
            "adapter_import_attempted": False,
            "runner_argv": None,
        }
        real_import = builtins.__import__

        def tracking_import(name, globals=None, locals=None, fromlist=(),
                            level=0):
            if name == "agent_coop" and "coop_herdr" in fromlist:
                state["adapter_import_attempted"] = True
            return real_import(name, globals, locals, fromlist, level)

        def runner(argv):
            state["runner_argv"] = argv
            return 0

        builtins.__import__ = tracking_import
        coop_autonomous.main = runner
        cli.main(sys.argv[1:])
        state["adapter_in_modules"] = (
            "agent_coop.coop_herdr" in sys.modules
        )
        print(json.dumps(state, sort_keys=True))
        """,
        argv,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "adapter_import_attempted": False,
        "adapter_in_modules": False,
        "runner_argv": [
            "--db", "C:/work/board.db", "--item", "24",
            "--agents", "claude,codex,grok",
            "--interval", "1.5", "--timeout", "90.0",
            "--max-turns", "12", "--max-idle-rounds", "8",
            "--max-noop-cycles", "2",
            "--persistent-provider", "codex",
            "--prompt-cache-provider", "claude",
            "--no-prompt-hydration", "--token-efficient",
            "--no-mechanical-precommit", "--no-prompt-cache",
            "--fresh-sessions", "--dry-run",
        ],
    }


@pytest.mark.parametrize("argv", [
    ["start", "--item", "0", "--herdr"],
    ["start", "--herdr", "--item", "0"],
    ["start", "--item", "-2", "--herdr"],
    ["start", "--herdr", "--item", "-2"],
])
def test_public_start_rejects_nonpositive_item_before_callback_and_herdr(argv):
    completed = _fresh_python(
        """
        import atexit
        import builtins
        import json
        import sys
        import types

        from agent_coop import cli, coop_autonomous

        state = {
            "adapter_import_attempted": False,
            "cmd_start_called": False,
            "herdr_probe_called": False,
            "runner_called": False,
        }
        real_import = builtins.__import__
        real_cmd_start = cli.cmd_start

        def herdr_probe():
            state["herdr_probe_called"] = True
            return True

        fake_adapter = types.SimpleNamespace(available=herdr_probe)
        fake_package = types.SimpleNamespace(coop_herdr=fake_adapter)

        def tracking_import(name, globals=None, locals=None, fromlist=(),
                            level=0):
            if name == "agent_coop" and "coop_herdr" in fromlist:
                state["adapter_import_attempted"] = True
                return fake_package
            return real_import(name, globals, locals, fromlist, level)

        def tracked_cmd_start(conn, args):
            state["cmd_start_called"] = True
            return real_cmd_start(conn, args)

        def runner(_argv):
            state["runner_called"] = True
            return 0

        def report():
            print(json.dumps(state, sort_keys=True))

        builtins.__import__ = tracking_import
        cli.cmd_start = tracked_cmd_start
        coop_autonomous.main = runner
        atexit.register(report)
        cli.main(sys.argv[1:])
        """,
        argv,
    )

    assert completed.returncode == 2
    assert "argument --item" in completed.stderr
    assert "positive integer" in completed.stderr
    assert json.loads(completed.stdout) == {
        "adapter_import_attempted": False,
        "cmd_start_called": False,
        "herdr_probe_called": False,
        "runner_called": False,
    }


def test_start_with_herdr_pins_preflight_to_selected_workspace_and_runner(
        tmp_path, monkeypatch):
    board = tmp_path / "selected-workspace" / ".coop" / "board.db"
    args = cli.build_parser().parse_args([
        "start", "--board", str(board), "--all", "--herdr",
    ])
    launcher = tmp_path / "trusted-bin" / "herdr"
    source = {
        "PATH": str(launcher.parent),
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "caller-pane",
        "OPENAI_API_KEY": "provider-token",
        "GITHUB_TOKEN": "unrelated-token",
    }
    resolutions = []
    invocations = []

    def resolve(name, *, workspace=None):
        resolutions.append((name, pathlib.Path(workspace).resolve()))
        return str(launcher.resolve())

    def invoke(argv, **kwargs):
        invocations.append((list(argv), kwargs))
        return _successful_probe()

    monkeypatch.setattr(coop_herdr, "_resolve_herdr_launcher", resolve)
    monkeypatch.setattr(coop_herdr.subprocess, "run", invoke)
    with mock.patch.dict(os.environ, source, clear=True), \
         mock.patch.object(coop_autonomous, "main", return_value=0) as run:
        cli.cmd_start(None, args)

    expected_workspace = (tmp_path / "selected-workspace").resolve()
    assert resolutions == [("herdr", expected_workspace)]
    assert len(invocations) == 1
    preflight_argv, preflight_kwargs = invocations[0]
    assert preflight_argv == [str(launcher.resolve()), "api", "snapshot"]
    assert preflight_kwargs["env"] == {
        "PATH": str(launcher.parent),
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "caller-pane",
    }
    forwarded = run.call_args.args[0]
    assert isinstance(
        run.call_args.kwargs["herdr_adapter"],
        coop_herdr.HerdrAdapter,
    )
    assert forwarded.count("--herdr") == 1
    assert forwarded == [
        "--db", str(board), "--all",
        "--interval", "3.0", "--timeout", "600.0",
        "--max-turns", "200", "--max-idle-rounds", "40",
        "--max-noop-cycles", "3", "--herdr",
    ]


@pytest.mark.parametrize("probe_failure", [
    FileNotFoundError("private missing-binary path"),
    _failed_probe(),
])
def test_herdr_preflight_failure_is_human_readable_silent_and_pre_run(
        monkeypatch, capsys, probe_failure):
    adapter = coop_herdr.HerdrAdapter(
        runner=_probe_runner(probe_failure),
        resolve=lambda name, *, workspace=None: str(
            REPO_ROOT.parent / "synthetic-herdr"
        ),
    )

    with mock.patch.object(
             coop_herdr, "HerdrAdapter", return_value=adapter), \
         mock.patch.object(
             coop_autonomous, "main",
            side_effect=AssertionError("runner reached")) as run, \
         mock.patch.object(
             coop_start, "probe_agent",
             side_effect=AssertionError("provider preflight reached")) as providers, \
         mock.patch.object(
             coopdb, "connect",
             side_effect=AssertionError("board mutation boundary reached")) as connect, \
         pytest.raises(SystemExit) as caught:
        cli.main([
            "start", "--board", "unused.db", "--item", "24", "--herdr",
        ])

    assert caught.value.code == 1
    assert capsys.readouterr() == (
        "",
        "error: herdr_unavailable: --herdr requires the Herdr binary and a "
        "reachable live Herdr session\n"
        "reason: session_unavailable\n"
        "evidence: {\"constraint\":\"herdr_binary_and_live_session_required\"}\n"
        "legal-next-actions: []\n",
    )
    run.assert_not_called()
    providers.assert_not_called()
    connect.assert_not_called()


def test_herdr_preflight_failure_uses_project_json_error_without_probe_output(
        monkeypatch, capsys):
    adapter = coop_herdr.HerdrAdapter(
        runner=_probe_runner(_failed_probe()),
        resolve=lambda name, *, workspace=None: str(
            REPO_ROOT.parent / "synthetic-herdr"
        ),
    )

    with mock.patch.object(
             coop_herdr, "HerdrAdapter", return_value=adapter), \
         mock.patch.object(
             coop_autonomous, "main",
            side_effect=AssertionError("runner reached")) as run, \
         pytest.raises(SystemExit) as caught:
        cli.main([
            "--json", "start", "--board", "unused.db", "--all", "--herdr",
        ])

    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {
        "error": {
            "type": "herdr_unavailable",
            "message": (
                "--herdr requires the Herdr binary and a reachable live "
                "Herdr session"
            ),
            "reason_code": "session_unavailable",
            "evidence": {
                "constraint": "herdr_binary_and_live_session_required",
            },
            "legal_next_actions": [],
        },
    }
    assert "private stdout" not in captured.err
    assert "private stderr" not in captured.err
    run.assert_not_called()


@pytest.mark.parametrize("argv", [
    ["start", "--herdr"],
    ["start", "--item", "24", "--all", "--herdr"],
])
def test_herdr_flag_does_not_bypass_existing_target_validation(argv):
    with mock.patch.object(coop_herdr, "available") as probe, \
         contextlib.redirect_stderr(io.StringIO()), \
         pytest.raises(SystemExit) as caught:
        cli.main(argv)

    assert caught.value.code == 2
    probe.assert_not_called()


def test_autonomous_runner_accepts_p13_herdr_handoff_without_side_effects():
    with contextlib.redirect_stdout(io.StringIO()):
        code = coop_autonomous.main(["--selftest", "--herdr"])

    assert code == 0


def test_herdr_mirrors_follow_frozen_quick_two_session_order_including_reserve(
        tmp_path):
    adapter = _RecordingHerdrAdapter()

    result = _run_p13_main(
        tmp_path,
        adapter,
        participants=("grok", "codex", "claude"),
        quick_two=True,
    )

    assert result["code"] == 0
    assert [provider for _run_id, provider, _kwargs in adapter.spawn_calls] == [
        "grok",
        "codex",
        "claude",
    ]
    assert result["runner"].call_args.args[0] == ["grok", "codex"]


def test_herdr_mirrors_only_the_explicit_persistent_provider(tmp_path):
    adapter = _RecordingHerdrAdapter()

    result = _run_p13_main(
        tmp_path,
        adapter,
        participants=("grok", "claude", "codex"),
        extra_argv=("--persistent-provider", "claude"),
    )

    assert result["code"] == 0
    assert [provider for _run_id, provider, _kwargs in adapter.spawn_calls] == [
        "claude",
    ]


def test_worker_unavailable_provider_gets_no_herdr_mirror(tmp_path):
    adapter = _RecordingHerdrAdapter()

    result = _run_p13_main(
        tmp_path,
        adapter,
        participants=("grok", "codex", "claude"),
        unavailable={"codex": "resident bootstrap failed"},
    )

    assert result["code"] == 0
    assert [provider for _run_id, provider, _kwargs in adapter.spawn_calls] == [
        "grok",
        "claude",
    ]


def test_no_persistent_workers_allows_herdr_with_zero_mirrors(tmp_path):
    adapter = _RejectingHerdrAdapter()

    result = _run_p13_main(
        tmp_path,
        adapter,
        extra_argv=("--no-persistent-workers",),
    )

    assert result["code"] == 0
    result["runner"].assert_called_once()
    assert "herdr" not in coop_runner_status.read_status(
        result["status_path"]
    )


def test_public_cold_workers_herdr_contract_requests_no_persistent_provider():
    args = cli.build_parser().parse_args([
        "start",
        "--board",
        "C:/work/board.db",
        "--item",
        "24",
        "--cold-workers",
        "--persistent-provider",
        "codex",
        "--herdr",
    ])
    adapter = mock.Mock()
    adapter.available.return_value = True
    with mock.patch.object(
             coop_herdr, "HerdrAdapter", return_value=adapter), \
         mock.patch.object(coop_autonomous, "main", return_value=0) as run:
        cli.cmd_start(None, args)

    forwarded = run.call_args.args[0]
    assert "--herdr" in forwarded
    assert "--no-persistent-workers" in forwarded
    assert "--persistent-provider" not in forwarded


def test_herdr_dry_run_creates_no_pool_mirror_or_scheduler_turn(tmp_path):
    adapter = _RejectingHerdrAdapter()

    result = _run_p13_main(
        tmp_path,
        adapter,
        extra_argv=("--dry-run",),
        environment_overrides={
            "HERDR_PANE_ID": "caller pane/dry",
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "ignored workspace",
            "HERDR_TAB_ID": "ignored tab",
        },
    )

    assert result["code"] == 0
    result["prepared"].assert_not_called()
    result["runner"].assert_not_called()
    assert coop_runner_status.read_status(result["status_path"])["herdr"] == {
        "caller_pane": "caller pane/dry",
    }


def test_bare_runner_publishes_caller_only_without_importing_adapter(tmp_path):
    _workspace, board, item_id = _runner_board(tmp_path)
    status_path = tmp_path / "bare-caller.status.json"
    trace_path = tmp_path / "bare-caller.trace.jsonl"
    completed = _fresh_python(
        """
        import builtins
        import contextlib
        import io
        import json
        import os
        import sys
        from unittest import mock

        from agent_coop import coop_autonomous, coop_runner_status, coop_start

        os.environ["COOP_RUN_STATUS_PATH"] = sys.argv[3]
        os.environ["COOP_RUN_TRACE_PATH"] = sys.argv[4]
        os.environ["HERDR_PANE_ID"] = "caller pane/bare"
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_WORKSPACE_ID"] = "ignored workspace"
        os.environ["HERDR_TAB_ID"] = "ignored tab"
        state = {"adapter_import_attempted": False}
        real_import = builtins.__import__

        def tracking_import(name, globals=None, locals=None, fromlist=(),
                            level=0):
            if name == "agent_coop" and "coop_herdr" in fromlist:
                state["adapter_import_attempted"] = True
                raise AssertionError("bare runner imported adapter")
            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = tracking_import
        with mock.patch.object(
                coop_start,
                "resolve_participants",
                return_value={
                    "available": ["claude", "codex", "grok"],
                    "skipped": [],
                }), mock.patch.object(
                    coop_start,
                    "resolved_provider_argv",
                    side_effect=lambda provider: [provider]), mock.patch.object(
                    coop_autonomous,
                    "prepare_opt_in_worker_pool",
                    return_value=(None, (), {})), mock.patch.object(
                    coop_autonomous,
                    "run_autonomous",
                    return_value=("stopped", 0, [])), contextlib.redirect_stdout(
                        io.StringIO()):
            code = coop_autonomous.main([
                "--db", sys.argv[1], "--item", sys.argv[2],
                "--fresh-sessions",
            ])
        state["adapter_in_modules"] = "agent_coop.coop_herdr" in sys.modules
        state["code"] = code
        state["herdr"] = coop_runner_status.read_status(sys.argv[3]).get(
            "herdr"
        )
        print(json.dumps(state, sort_keys=True))
        """,
        [str(board), str(item_id), str(status_path), str(trace_path)],
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "adapter_import_attempted": False,
        "adapter_in_modules": False,
        "code": 0,
        "herdr": {"caller_pane": "caller pane/bare"},
    }


@pytest.mark.parametrize("explicit_name", [None, "benchmark-explicit"])
def test_herdr_mirror_uses_runner_trace_run_id_workspace_board_and_environment(
        tmp_path, explicit_name):
    adapter = _RecordingHerdrAdapter()
    explicit_trace = (
        tmp_path / f"{explicit_name}.trace.jsonl"
        if explicit_name is not None
        else None
    )

    result = _run_p13_main(
        tmp_path,
        adapter,
        extra_argv=("--persistent-provider", "codex"),
        trace_path=explicit_trace,
        environment_overrides={
            "GITHUB_TOKEN": "must-not-pass",
            "OPENAI_API_KEY": "must-pass",
        },
    )

    assert result["code"] == 0
    assert len(adapter.spawn_calls) == 1
    run_id, provider, kwargs = adapter.spawn_calls[0]
    trace_path = pathlib.Path(kwargs["trace_path"])
    assert provider == "codex"
    assert trace_path.is_absolute()
    assert run_id == trace_path.name.removesuffix(".trace.jsonl")
    assert run_id == (explicit_name or run_id)
    if explicit_trace is None:
        assert run_id.startswith("run-")
        assert trace_path.parent == result["board"].parent / ".coop-runs"
    else:
        assert trace_path == explicit_trace.resolve()
    assert pathlib.Path(kwargs["cwd"]) == result["workspace"]
    assert pathlib.Path(kwargs["board_path"]) == result["board"]
    # Mirror creation receives the Herdr-only environment, not provider or
    # unrelated credentials from the runner's shell.
    environ = kwargs["environ"]
    assert environ is not os.environ
    assert "GITHUB_TOKEN" not in environ
    assert "OPENAI_API_KEY" not in environ
    assert environ["PATH"] == os.environ["PATH"]


def test_runner_pins_provider_resolution_to_the_workspace_for_the_run(
        tmp_path):
    adapter = _RecordingHerdrAdapter()
    real_pin = coop_start.pin_cli_resolution
    pins = []

    def recording_pin(*args, **kwargs):
        pins.append((args, kwargs))
        return real_pin(*args, **kwargs)

    with mock.patch.object(
            coop_start, "pin_cli_resolution", side_effect=recording_pin):
        result = _run_p13_main(tmp_path, adapter)

    assert result["code"] == 0
    assert len(pins) == 1
    assert pathlib.Path(pins[0][1]["workspace"]).resolve() == (
        result["workspace"]
    )
    # Released with the run: the next resolve in this process is live.
    assert coop_start._PINNED_CLI is None


def test_successful_setup_publishes_full_metadata_before_scheduler_and_freezes_caller(
        tmp_path):
    trace_path = tmp_path / "successful-status.trace.jsonl"
    adapter = _RecordingHerdrAdapter()
    observed = []

    def run(_participants, **kwargs):
        observed.append(coop_runner_status.read_status(
            coop_runner_status.status_path_for_run_artifact(trace_path)
        ))
        os.environ["HERDR_PANE_ID"] = "caller pane/changed"
        kwargs["status_fn"](
            "turn",
            agent="codex",
            action="review_task",
        )
        observed.append(coop_runner_status.read_status(
            coop_runner_status.status_path_for_run_artifact(trace_path)
        ))
        return "stopped", 1, []

    result = _run_p13_main(
        tmp_path,
        adapter,
        trace_path=trace_path,
        environment_overrides={
            "HERDR_PANE_ID": "caller pane/original",
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "ignored workspace",
            "HERDR_TAB_ID": "ignored tab",
        },
        run_side_effect=run,
    )

    expected = {
        "workspace": "opaque-workspace::successful-status",
        "panes": {
            "claude": "opaque-pane::claude",
            "codex": "opaque-pane::codex",
            "grok": "opaque-pane::grok",
        },
        "caller_pane": "caller pane/original",
    }
    assert result["code"] == 0
    assert adapter.workspace_calls == ["successful-status"]
    assert [status["herdr"] for status in observed] == [expected, expected]
    terminal = coop_runner_status.read_status(result["status_path"])
    assert (terminal["phase"], terminal["reason"], terminal["turns"]) == (
        "stopped",
        "stopped",
        1,
    )
    assert terminal["herdr"] == expected


def test_partial_herdr_setup_rolls_back_returned_panes_stops_scheduler_and_runs_finalizers(
        tmp_path):
    trace_path = tmp_path / "partial-failure.trace.jsonl"
    adapter = _RecordingHerdrAdapter(
        spawn_failures={"codex": RuntimeError("second mirror failed")},
    )
    pool = _RecordingPool()
    cleanup_calls = []

    result = _run_p13_main(
        tmp_path,
        adapter,
        trace_path=trace_path,
        worker_pool=pool,
        worker_cleanups=(lambda: cleanup_calls.append("profile"),),
        environment_overrides={"HERDR_PANE_ID": "caller pane/rollback"},
    )

    assert result["code"] == 3
    assert [provider for _run_id, provider, _kwargs in adapter.spawn_calls] == [
        "claude",
        "codex",
    ]
    assert adapter.teardown_calls == [("opaque-pane::claude",)]
    result["runner"].assert_not_called()
    assert pool.stop_calls == 1
    assert cleanup_calls == ["profile"]
    status = coop_runner_status.read_status(
        coop_runner_status.status_path_for_run_artifact(trace_path)
    )
    assert (status["phase"], status["reason"], status["turns"]) == (
        "failed",
        "herdr_setup_failed",
        0,
    )
    assert status["herdr"] == {"caller_pane": "caller pane/rollback"}
    trace_events = coop_turn_trace.read_events(trace_path)
    assert [event["event"] for event in trace_events] == [
        "run_started",
        "run_finished",
    ]
    assert trace_events[-1]["details"]["classification"] == (
        "herdr_setup_failed"
    )
    conn = coopdb.connect(str(result["board"]), require_current=True)
    try:
        session_statuses = {
            row["status"]
            for row in conn.execute(
                "SELECT status FROM sessions WHERE session_id LIKE 'coop-auto-%'"
            )
        }
    finally:
        conn.close()
    assert session_statuses == {"exited"}


def test_herdr_rollback_failure_retries_and_surfaces_without_cold_fallback(
        tmp_path):
    trace_path = tmp_path / "rollback-failure.trace.jsonl"
    adapter = _RecordingHerdrAdapter(
        spawn_failures={"codex": RuntimeError("second mirror failed")},
        teardown_failures=(
            RuntimeError("first cleanup failed"),
            RuntimeError("retry cleanup failed"),
        ),
    )

    result = _run_p13_main(
        tmp_path,
        adapter,
        trace_path=trace_path,
        environment_overrides={
            "HERDR_PANE_ID": "caller pane/rollback-failed",
        },
    )

    assert result["code"] == 3
    assert adapter.teardown_calls == [
        ("opaque-pane::claude",),
        ("opaque-pane::claude",),
    ]
    result["runner"].assert_not_called()
    status = coop_runner_status.read_status(
        coop_runner_status.status_path_for_run_artifact(trace_path)
    )
    assert status["reason"] == "herdr_cleanup_failed"
    assert status["herdr"] == {
        "workspace": "opaque-workspace::rollback-failure",
        "panes": {"claude": "opaque-pane::claude"},
        "caller_pane": "caller pane/rollback-failed",
    }
    assert coop_turn_trace.read_events(trace_path)[-1]["details"][
        "classification"
    ] == "herdr_cleanup_failed"


def test_partial_setup_cleanup_failure_chains_cleanup_to_setup_evidence(
        tmp_path):
    setup_error = RuntimeError("second mirror failed")
    cleanup_failures = (
        RuntimeError("first cleanup failed"),
        RuntimeError("retry cleanup failed"),
    )
    adapter = _RecordingHerdrAdapter(
        spawn_failures={"codex": setup_error},
        teardown_failures=cleanup_failures,
    )

    with pytest.raises(coop_autonomous.HerdrMirrorSetupError) as caught:
        coop_autonomous.prepare_opt_in_herdr_mirrors(
            True,
            {"claude", "codex"},
            session_participants=("claude", "codex", "grok"),
            run_id="chain-evidence",
            trace_path=tmp_path / "chain-evidence.trace.jsonl",
            cwd=tmp_path,
            board_path=tmp_path / "board.db",
            environ={},
            adapter=adapter,
        )

    assert caught.value.reason == "herdr_cleanup_failed"
    assert caught.value.__cause__ is cleanup_failures[-1]
    assert caught.value.__cause__.__context__ is setup_error
    assert adapter.teardown_calls == [
        ("opaque-pane::claude",),
        ("opaque-pane::claude",),
    ]


def test_owned_provider_mirror_cleanup_failure_retries_and_keeps_ownership():
    cleanup_failures = (
        RuntimeError("first provider close failed"),
        RuntimeError("retry provider close failed"),
    )
    adapter = _RecordingHerdrAdapter(teardown_failures=cleanup_failures)
    ownership = coop_autonomous._HerdrMirrorOwnership()
    ownership.record(
        "claude",
        "opaque-pane::claude",
        workspace_id="opaque-workspace::cleanup",
    )
    ownership.record(
        "codex",
        "opaque-pane::codex",
        workspace_id="opaque-workspace::cleanup",
    )

    with pytest.raises(coop_autonomous.HerdrMirrorCleanupError) as caught:
        coop_autonomous.teardown_owned_herdr_mirror(
            ownership,
            "codex",
            adapter=adapter,
        )

    assert adapter.teardown_calls == [
        ("opaque-pane::codex",),
        ("opaque-pane::codex",),
    ]
    assert ownership.snapshot() == (
        "opaque-workspace::cleanup",
        {
            "claude": "opaque-pane::claude",
            "codex": "opaque-pane::codex",
        },
    )
    assert caught.value.__cause__ is cleanup_failures[-1]


def test_last_successful_provider_teardown_clears_workspace_ownership():
    adapter = _RecordingHerdrAdapter()
    ownership = coop_autonomous._HerdrMirrorOwnership()
    ownership.record(
        "codex",
        "opaque-pane::codex",
        workspace_id="opaque-workspace::last",
    )

    assert coop_autonomous.teardown_owned_herdr_mirror(
        ownership,
        "codex",
        adapter=adapter,
    ) is True
    assert ownership.snapshot() == (None, {})


def test_ownership_snapshots_are_fresh_and_concurrency_safe():
    ownership = coop_autonomous._HerdrMirrorOwnership()
    workspace_id = "opaque-workspace::concurrent"
    invalid = []
    done = threading.Event()

    def writer():
        for index in range(500):
            ownership.record(
                "codex",
                f"opaque-pane::{index}",
                workspace_id=workspace_id,
            )
            ownership.forget("codex")
        done.set()

    def reader():
        while not done.is_set():
            workspace, panes = ownership.snapshot()
            if bool(workspace) != bool(panes):
                invalid.append((workspace, panes))
            elif workspace not in (None, workspace_id):
                invalid.append((workspace, panes))

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    writer_thread.join(timeout=5)
    for thread in readers:
        thread.join(timeout=5)

    assert not writer_thread.is_alive()
    assert all(not thread.is_alive() for thread in readers)
    assert invalid == []
    ownership.record(
        "codex",
        "opaque-pane::fresh",
        workspace_id=workspace_id,
    )
    _workspace, first = ownership.snapshot()
    first["codex"] = "mutated snapshot"
    assert ownership.snapshot() == (
        workspace_id,
        {"codex": "opaque-pane::fresh"},
    )


def test_second_herdr_spawn_keyboard_interrupt_rolls_back_and_finalizes(
        tmp_path):
    trace_path = tmp_path / "keyboard-interrupt.trace.jsonl"
    interrupt = KeyboardInterrupt("operator interrupt")
    adapter = _RecordingHerdrAdapter(
        spawn_failures={"codex": interrupt},
    )
    pool = _RecordingPool()
    cleanup_calls = []

    result = _run_p13_main(
        tmp_path,
        adapter,
        trace_path=trace_path,
        worker_pool=pool,
        worker_cleanups=(lambda: cleanup_calls.append("profile"),),
        expected_exception=KeyboardInterrupt,
    )

    assert result["exception"] is interrupt
    assert [provider for _run_id, provider, _kwargs in adapter.spawn_calls] == [
        "claude",
        "codex",
    ]
    assert adapter.teardown_calls == [("opaque-pane::claude",)]
    result["runner"].assert_not_called()
    assert pool.stop_calls == 1
    assert cleanup_calls == ["profile"]
    status = coop_runner_status.read_status(
        coop_runner_status.status_path_for_run_artifact(trace_path)
    )
    assert (status["phase"], status["reason"], status["turns"]) == (
        "failed",
        "error",
        0,
    )
    trace_events = coop_turn_trace.read_events(trace_path)
    assert [event["event"] for event in trace_events] == [
        "run_started",
        "run_finished",
    ]
    assert {event["run_id"] for event in trace_events} == {
        "keyboard-interrupt",
    }
    assert trace_events[-1]["details"]["classification"] == "error"
    conn = coopdb.connect(str(result["board"]), require_current=True)
    try:
        session_statuses = {
            row["status"]
            for row in conn.execute(
                "SELECT status FROM sessions WHERE session_id LIKE 'coop-auto-%'"
            )
        }
        finished = conn.execute(
            "SELECT payload_json FROM events "
            "WHERE event_type='autonomous_run_finished' "
            "ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert session_statuses == {"exited"}
    assert json.loads(finished["payload_json"])["reason"] == "error"
    assert len(result["opened_connections"]) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        result["opened_connections"][0].execute("SELECT 1")


def test_successful_herdr_mirrors_are_retained_after_run_for_scrollback(
        tmp_path):
    adapter = _RecordingHerdrAdapter()

    result = _run_p13_main(tmp_path, adapter)

    assert result["code"] == 0
    result["runner"].assert_called_once()
    assert len(adapter.spawn_calls) == 3
    assert adapter.teardown_calls == []
    status = coop_runner_status.read_status(result["status_path"])
    run_id = pathlib.Path(result["status_path"]).name.removesuffix(
        ".status.json"
    )
    assert status["herdr"] == {
        "workspace": f"opaque-workspace::{run_id}",
        "panes": {
            "claude": "opaque-pane::claude",
            "codex": "opaque-pane::codex",
            "grok": "opaque-pane::grok",
        },
    }
