"""Opt-in live ``coop start --herdr`` integration against real Herdr.

This is the only test in the suite that runs the real ``herdr`` binary, and it
is the only test that is allowed to skip.  It stays off unless the operator
sets ``COOP_HERDR_LIVE=1``, because it needs a reachable live Herdr session
that no hermetic runner can provide.

The test drives the real public runner path with ``--herdr`` and the real
``HerdrAdapter``.  It stubs only the turn loop, so it spends no provider
quota while it still exercises recipe preflight, mirror setup in frozen
session order, sidecar publication, and the run finalizers.

Run it with, for example::

    COOP_HERDR_LIVE=1 python -m pytest tests/test_herdr_live.py -q -s
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import time
import uuid
from unittest import mock

import pytest

from agent_coop import (
    coop_autonomous,
    coop_herdr,
    coop_runner_status,
    coop_start,
    coopdb,
)


LIVE_ENV_FLAG = "COOP_HERDR_LIVE"
LIVE_SKIP_REASON = (
    "opt-in live Herdr test: set COOP_HERDR_LIVE=1 and run it inside a "
    "reachable live Herdr session"
)
# One mirror per provider is the product default.  Two providers prove frozen
# session order and multi-pane teardown without opening a pane the operator
# has to watch three of.
LIVE_PROVIDERS = ("claude", "codex")
MIRROR_READY_TIMEOUT_SECONDS = 30.0
MIRROR_POLL_SECONDS = 0.5


def _herdr_stdout(*argv):
    """Run one read-only Herdr command from the test, never from a product
    module.  Only ``coop_herdr`` may call the CLI in product code."""
    completed = subprocess.run(
        ["herdr", *argv],
        capture_output=True,
        text=True,
        check=False,
        timeout=coop_herdr.COMMAND_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, (
        f"herdr {' '.join(argv)} exited {completed.returncode}"
    )
    return completed.stdout


def _strings(value):
    """Collect every string in a decoded payload, whatever its schema.

    ``herdr pane list`` output shape is not part of Co-op's contract and can
    change across Herdr versions, so membership is checked structurally.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _listed_pane_ids():
    stdout = _herdr_stdout("pane", "list")
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None, stdout
    return set(_strings(payload)), stdout


def _pane_is_listed(pane_id):
    listed, raw = _listed_pane_ids()
    return pane_id in listed if listed is not None else pane_id in raw


def _live_board(tmp_path):
    workspace = tmp_path / "live-workspace"
    board = workspace / ".coop" / "board.db"
    board.parent.mkdir(parents=True)
    conn = coopdb.connect(str(board))
    coopdb.init_db(conn)
    item_id = coopdb.create_item(
        conn,
        actor="human",
        session_id=None,
        title="herdr live target",
        objective="Exercise live Herdr mirror setup",
    )
    conn.close()
    return board.resolve(), item_id


def _wait_for_mirror_header(adapter, pane_id, run_id):
    """Poll an owned pane until the renderer's own first line appears."""
    deadline = time.monotonic() + MIRROR_READY_TIMEOUT_SECONDS
    last = ""
    while time.monotonic() < deadline:
        try:
            last = adapter.read(pane_id)
        except coop_herdr.HerdrError:
            last = ""
        if "Co-op mirror" in last and run_id in last:
            return last
        time.sleep(MIRROR_POLL_SECONDS)
    return last


def test_live_coop_start_herdr_opens_mirrors_and_publishes_pane_ids(tmp_path):
    if os.environ.get(LIVE_ENV_FLAG) != "1":
        pytest.skip(LIVE_SKIP_REASON)

    adapter = coop_herdr.HerdrAdapter()
    assert adapter.available(), (
        "COOP_HERDR_LIVE=1 was set, but the herdr binary did not reach a live "
        "session. Start Herdr, then run this test again."
    )

    board, item_id = _live_board(tmp_path)
    # A throwaway, uniquely named run: the adapter labels its workspace
    # `coop-run-<run_id>`, so this run owns an identifiable Herdr workspace
    # that the teardown below closes.
    run_id = f"coop-live-{uuid.uuid4().hex[:12]}"
    trace_path = tmp_path / f"{run_id}.trace.jsonl"
    status_path = pathlib.Path(
        coop_runner_status.status_path_for_run_artifact(str(trace_path)))

    environment = dict(os.environ)
    environment.pop("COOP_RUN_STATUS_PATH", None)
    environment.pop("HERDR_PANE_ID", None)
    environment["COOP_RUN_TRACE_PATH"] = str(trace_path)
    # Standing repo rule: a test board never reaches the operator registry.
    environment["COOP_BOARDS_REGISTRY"] = str(tmp_path / "boards.json")

    argv = [
        "--db", str(board),
        "--item", str(item_id),
        "--herdr",
        "--fresh-sessions",
    ]
    for provider in LIVE_PROVIDERS:
        argv += ["--persistent-provider", provider]

    owned_panes = ()
    try:
        with mock.patch.dict(os.environ, environment, clear=True), \
             mock.patch.object(
                 coop_start,
                 "resolve_participants",
                 return_value={
                     "available": list(LIVE_PROVIDERS), "skipped": []},
             ), \
             mock.patch.object(
                 coop_autonomous,
                 "prepare_opt_in_worker_pool",
                 mock.Mock(return_value=(None, (), {})),
             ), \
             mock.patch.object(
                 coop_autonomous,
                 "run_autonomous",
                 mock.Mock(return_value=("stopped", 0, [])),
             ), \
             contextlib.redirect_stdout(io.StringIO()):
            coop_autonomous.main(argv, herdr_adapter=adapter)

        status = coop_runner_status.read_status(str(status_path))
        assert status is not None, "the run published no status sidecar"
        assert status.get("phase") in coop_runner_status.TERMINAL_PHASES, (
            f"the run did not reach a terminal phase: {status.get('phase')}"
        )

        # The sidecar carries the pane IDs.
        herdr_metadata = status.get("herdr")
        assert isinstance(herdr_metadata, dict), "sidecar carried no herdr key"
        panes = herdr_metadata.get("panes")
        assert isinstance(panes, dict)
        assert sorted(panes) == sorted(LIVE_PROVIDERS)
        assert coop_runner_status.canonical_herdr_id(
            herdr_metadata.get("workspace"))
        owned_panes = tuple(panes[provider] for provider in LIVE_PROVIDERS)

        # The dashboard offers exactly those IDs as a jump target.
        jump = coop_runner_status.format_herdr_jump(status)
        assert jump.startswith("herdr ")
        for provider, pane_id in panes.items():
            assert f"{provider}={pane_id}" in jump or jump.startswith(
                "herdr ws="), jump

        # The mirrors are real Herdr panes.
        for pane_id in owned_panes:
            assert _pane_is_listed(pane_id), (
                f"pane {pane_id} is absent from `herdr pane list`")

        # Each pane runs the read-only renderer, not a worker.
        for provider, pane_id in panes.items():
            text = _wait_for_mirror_header(adapter, pane_id, run_id)
            assert "Co-op mirror" in text, (
                f"the {provider} pane never rendered the mirror header: "
                f"{text!r}"
            )
            assert provider in text
    finally:
        if owned_panes:
            adapter.teardown(owned_panes)

    for pane_id in owned_panes:
        assert not _pane_is_listed(pane_id), (
            f"pane {pane_id} survived teardown")
