"""Co-op is unchanged when Herdr is absent or the flag is off.

The absent-Herdr half proves the herdr-less machine contract: with ``--herdr``
never passed and the ``herdr`` binary unresolvable on ``PATH``, the runner
behaves exactly as it does today, no product module reaches the Herdr CLI, and
no test needs a skip.

The launcher half proves the Windows launcher rule survives it: provider
command resolution still selects an executable PATHEXT launcher such as
``codex.cmd`` and never an extensionless POSIX shim.  A mirror pane cannot
change that, because it binds no ``PATH``/``PATHEXT`` and runs the read-only
renderer instead of a provider CLI.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
from unittest import mock

import pytest

from agent_coop import (
    coop_autonomous,
    coop_herdr,
    coop_runner_status,
    coop_start,
    coopdb,
)


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "agent_coop"
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:]+\+\d{2}:\d{2}")


def _package_sources():
    return sorted(PACKAGE_ROOT.glob("*.py"))


def _cli_argv_lines(source):
    """Report every literal argv sequence that opens with the herdr command.

    An AST walk, not a text search: ``payload["herdr"]`` and
    ``add_parser("herdr", …)`` name the flag or the sidecar key, and neither
    one runs the binary.  Only a list or tuple whose first element is the
    literal command is an invocation (D3).
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.List, ast.Tuple))
        and len(node.elts) > 1
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "herdr"
    ]


def _fresh_python(script, argv, *, env):
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _herdr_free_environment(tmp_path):
    """A child environment whose PATH cannot resolve the Herdr binary."""
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir(exist_ok=True)
    environment = dict(os.environ)
    environment["PATH"] = str(empty_path)
    environment.pop("HERDR_ENV", None)
    environment.pop("HERDR_PANE_ID", None)
    environment.pop("HERDR_WORKSPACE_ID", None)
    environment.pop("HERDR_TAB_ID", None)
    environment.pop("COOP_RUN_STATUS_PATH", None)
    environment.pop("COOP_RUN_TRACE_PATH", None)
    # Standing repo rule: a test board never reaches the operator registry.
    environment["COOP_BOARDS_REGISTRY"] = str(tmp_path / "boards.json")
    return environment


def _board(tmp_path):
    workspace = tmp_path / "hermetic-workspace"
    board = workspace / ".coop" / "board.db"
    board.parent.mkdir(parents=True)
    conn = coopdb.connect(str(board))
    coopdb.init_db(conn)
    item_id = coopdb.create_item(
        conn,
        actor="human",
        session_id=None,
        title="herdr target",
        objective="Exercise the herdr-less contract",
    )
    conn.close()
    return board.resolve(), item_id


_BARE_RUN_SCRIPT = """
import contextlib
import io
import json
import os
import sys
from unittest import mock

from agent_coop import coop_autonomous, coop_runner_status, coop_start

os.environ["COOP_RUN_STATUS_PATH"] = sys.argv[3]
os.environ["COOP_RUN_TRACE_PATH"] = sys.argv[4]

# Any attempt to reach the Herdr CLI must fail the run loudly rather than
# degrade quietly, so the assertion below cannot pass by accident.
import subprocess as _subprocess

_real_run = _subprocess.run
_real_popen = _subprocess.Popen


def _argv_head(command):
    if isinstance(command, (list, tuple)) and command:
        return str(command[0])
    return str(command)


def _guard(name, real):
    def guarded(command, *args, **kwargs):
        if "herdr" in _argv_head(command).lower():
            raise AssertionError("bare run invoked the herdr CLI: " + name)
        return real(command, *args, **kwargs)

    return guarded


_subprocess.run = _guard("run", _real_run)
_subprocess.Popen = _guard("Popen", _real_popen)

stdout = io.StringIO()
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
                stdout):
    code = coop_autonomous.main([
        "--db", sys.argv[1], "--item", sys.argv[2], "--fresh-sessions",
    ])

status = coop_runner_status.read_status(sys.argv[3])
print(json.dumps({
    "adapter_in_modules": "agent_coop.coop_herdr" in sys.modules,
    "code": code,
    "herdr_in_sidecar": "herdr" in (status or {}),
    "phase": (status or {}).get("phase"),
    "stdout": stdout.getvalue(),
}, sort_keys=True))
"""


class TestHerdrAbsent:
    """The flag is never passed and the binary is unresolvable."""

    def test_adapter_reports_unavailable_and_stays_silent(
            self, tmp_path, capfd):
        empty_path = tmp_path / "empty-path"
        empty_path.mkdir()
        adapter = coop_herdr.HerdrAdapter()

        with mock.patch.dict(os.environ, {"PATH": str(empty_path)}):
            assert adapter.available() is False

        captured = capfd.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_adapter_raises_nothing_when_the_binary_cannot_execute(self):
        """A blocked or missing binary is a bounded False, never a crash."""
        for failure in (
            FileNotFoundError("herdr"),
            PermissionError("herdr"),
            OSError(4551, "An Application Control policy has blocked this"),
            subprocess.TimeoutExpired(cmd=["herdr"], timeout=1.0),
        ):
            adapter = coop_herdr.HerdrAdapter(
                runner=mock.Mock(side_effect=failure))
            assert adapter.available() is False

    def test_bare_run_never_reaches_the_cli_and_publishes_no_herdr_key(
            self, tmp_path):
        board, item_id = _board(tmp_path)
        status_path = tmp_path / "hermetic.status.json"
        trace_path = tmp_path / "hermetic.trace.jsonl"

        completed = _fresh_python(
            _BARE_RUN_SCRIPT,
            [str(board), str(item_id), str(status_path), str(trace_path)],
            env=_herdr_free_environment(tmp_path),
        )

        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["adapter_in_modules"] is False
        assert result["code"] == 0
        assert result["herdr_in_sidecar"] is False
        assert result["phase"] in coop_runner_status.TERMINAL_PHASES
        assert "herdr" not in result["stdout"].lower()

    def test_bare_run_output_matches_with_and_without_herdr_on_path(
            self, tmp_path):
        """The herdr-less machine runs Co-op identically to a herdr machine."""
        outputs = []
        for index, environment in enumerate((
            dict(
                os.environ,
                COOP_BOARDS_REGISTRY=str(tmp_path / "boards.json"),
            ),
            _herdr_free_environment(tmp_path),
        )):
            run_root = tmp_path / f"run-{index}"
            run_root.mkdir()
            board, item_id = _board(run_root)
            completed = _fresh_python(
                _BARE_RUN_SCRIPT,
                [
                    str(board),
                    str(item_id),
                    str(run_root / "run.status.json"),
                    str(run_root / "run.trace.jsonl"),
                ],
                env=environment,
            )
            assert completed.returncode == 0, completed.stderr
            result = json.loads(completed.stdout)
            # Run-local paths and the wall clock are the only differences the
            # comparison must tolerate.
            text = result["stdout"].replace(str(run_root), "<root>")
            result["stdout"] = _TIMESTAMP.sub("<time>", text)
            outputs.append(result)

        assert outputs[0] == outputs[1]
        assert "<time>" in outputs[0]["stdout"]

    def test_only_the_adapter_module_invokes_the_herdr_cli(self):
        offenders = [
            f"{source.name}:{lineno}"
            for source in _package_sources()
            if source.name != "coop_herdr.py"
            for lineno in _cli_argv_lines(source)
        ]

        assert offenders == [], (
            "only agent_coop/coop_herdr.py may invoke the herdr CLI (D3)"
        )
        # The guard must be able to fail: the adapter still holds invocations.
        assert _cli_argv_lines(PACKAGE_ROOT / "coop_herdr.py")

    def test_no_module_imports_the_adapter_at_import_time(self):
        """Every adapter import is lazy, so a bare run never loads it."""
        offenders = []
        for source in _package_sources():
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in tree.body:
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [alias.name for alias in node.names]
                    names.append(node.module or "")
                if any("coop_herdr" in name for name in names):
                    offenders.append(f"{source.name}:{node.lineno}")

        assert offenders == []


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
class TestWindowsLauncherResolutionInsidePanes:
    """PATHEXT launcher selection is unchanged by the integration."""

    @staticmethod
    def _shim_fixture(root):
        (root / "codex").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "codex.cmd").write_text("@echo off\r\n", encoding="utf-8")
        return root

    def test_resolution_still_prefers_the_pathext_launcher(self, tmp_path):
        # The PATH walk (PATH holds only the fixture dir, no known install
        # dirs) selects the PATHEXT launcher over the POSIX shim beside it.
        root = self._shim_fixture(tmp_path)
        with mock.patch.object(coop_start, "_CLI_EXTRA_DIRS", ()), \
             mock.patch.dict(os.environ, {"PATH": str(root)}):
            resolved = coop_start.resolve_cli("codex")

        assert resolved == str((root / "codex.cmd").resolve())
        assert not resolved.endswith("codex")

    def test_mirror_panes_bind_no_path_or_pathext(self, tmp_path):
        """A pane cannot shadow the launcher rule it never rebinds."""
        from tests.test_coop_herdr import (  # noqa: PLC0415
            ScriptedRunner, _created_workspace, _ok,
        )

        runner = ScriptedRunner(_created_workspace(), _ok(), _ok())
        adapter = coop_herdr.HerdrAdapter(
            runner=runner,
            workspace=tmp_path,
            resolve=lambda name, *, workspace=None: str(
                tmp_path.parent / "trusted-herdr"
            ),
        )
        adapter.spawn_mirror(
            "p19-run",
            "codex",
            cwd=tmp_path,
            trace_path=tmp_path / "p19-run.trace.jsonl",
            board_path=tmp_path / ".coop" / "board.db",
            environ={"PATH": "C:\\decoy", "PATHEXT": ".DECOY"},
        )

        bound = [
            argv[index + 1]
            for argv, _kwargs in runner.calls
            for index, value in enumerate(argv[:-1])
            if value == "--env"
        ]
        assert bound, "the mirror bound no environment at all"
        for binding in bound:
            key = binding.split("=", 1)[0]
            assert key in ("COOP_RUN_TRACE_PATH", "COOP_DB", "PYTHONPATH")
        assert not any(
            binding.startswith(("PATH=", "PATHEXT=")) for binding in bound)

    def test_a_pane_runs_the_renderer_and_never_a_provider_cli(
            self, tmp_path):
        """D5: the pane hosts the mirror, so it resolves no provider at all."""
        from tests.test_coop_herdr import (  # noqa: PLC0415
            ScriptedRunner, _created_workspace, _ok,
        )

        runner = ScriptedRunner(_created_workspace(), _ok(), _ok())
        adapter = coop_herdr.HerdrAdapter(
            runner=runner,
            workspace=tmp_path,
            resolve=lambda name, *, workspace=None: str(
                tmp_path.parent / "trusted-herdr"
            ),
        )
        adapter.spawn_mirror(
            "p19-run",
            "codex",
            cwd=tmp_path,
            trace_path=tmp_path / "p19-run.trace.jsonl",
            board_path=tmp_path / ".coop" / "board.db",
            environ={},
        )

        commands = [
            argv[-1]
            for argv, _kwargs in runner.calls
            if argv[:3] == ["herdr", "pane", "run"]
        ]
        assert commands == [
            coop_herdr._mirror_shell_command("p19-run", "codex")
        ]
        for command in commands:
            assert "codex.cmd" not in command
            assert not command.startswith("codex")

    def test_mirror_setup_resolves_no_provider_and_mutates_no_environment(
            self, tmp_path):
        """Provider resolution before and after `--herdr` setup is identical."""
        before = coop_start.resolve_cli("codex")
        environment_before = dict(os.environ)

        spawned = []

        class _Adapter:
            def spawn_mirror(self, run_id, provider, **_kwargs):
                spawned.append((run_id, provider))
                return f"pane::{provider}"

            def workspace_id(self, run_id):
                return f"workspace::{run_id}"

            def teardown(self, names):
                raise AssertionError("teardown called on a clean setup")

        with mock.patch.object(
                coop_start, "resolve_cli",
                side_effect=AssertionError("mirror setup resolved a CLI")):
            ownership, adapter = coop_autonomous.prepare_opt_in_herdr_mirrors(
                True,
                {"codex", "claude"},
                session_participants=["claude", "codex"],
                run_id="p19-run",
                trace_path=str(tmp_path / "p19-run.trace.jsonl"),
                cwd=str(tmp_path),
                board_path=str(tmp_path / ".coop" / "board.db"),
                environ=dict(os.environ),
                adapter=_Adapter(),
            )

        assert spawned == [("p19-run", "claude"), ("p19-run", "codex")]
        assert ownership.pane_ids() == ("pane::claude", "pane::codex")
        assert dict(os.environ) == environment_before
        assert coop_start.resolve_cli("codex") == before
