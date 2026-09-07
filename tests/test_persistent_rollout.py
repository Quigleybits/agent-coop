"""Opt-in persistent-worker selection stays explicit and capability-safe."""

from __future__ import annotations

import dataclasses
import os
from types import SimpleNamespace
from unittest import mock

from agent_coop import cli as coopcli
from agent_coop import coop_autonomous
from agent_coop import coop_capabilities
from agent_coop import coop_resident_workers


PROMPT_CACHE_FLAG = "--exclude-dynamic-system-prompt-sections"


def test_worker_mode_is_provider_specific_and_external_capabilities_stay_cold():
    selected = {"claude", "codex", "grok"}
    board = coop_capabilities.MANIFESTS["board_core"]
    local = coop_capabilities.MANIFESTS["local_code"]
    review = coop_capabilities.MANIFESTS["deep_review"]
    native_research = coop_capabilities.MANIFESTS["research_web"]
    external = dataclasses.replace(
        coop_capabilities.MANIFESTS["research_web"],
        external_servers=("fake-search",),
    )

    # The codex app-server keeps its established three-manifest reuse.
    for manifest in (board, local, review):
        assert coop_autonomous.worker_mode_for(
            "codex",
            selected,
            manifest,
        ) == "persistent"
    # Claude/Grok residents are frozen on the read-only core surface, so
    # local_code (which needs Edit/Write) stays on the cold path.
    for provider in ("claude", "grok"):
        assert coop_autonomous.worker_mode_for(
            provider,
            selected,
            board,
        ) == "persistent"
        assert coop_autonomous.worker_mode_for(
            provider,
            selected,
            review,
        ) == "persistent"
        assert coop_autonomous.worker_mode_for(
            provider,
            selected,
            local,
        ) == "cold"
    assert coop_autonomous.worker_mode_for(
        "codex",
        selected,
        external,
    ) == "cold"
    assert coop_autonomous.worker_mode_for(
        "codex",
        selected,
        native_research,
    ) == "cold"
    assert coop_autonomous.worker_mode_for(
        "grok",
        selected,
        native_research,
    ) == "cold"
    assert coop_autonomous.worker_mode_for(
        "codex",
        set(),
        board,
    ) == "cold"


def test_token_efficient_completion_probe_demotes_only_stream_residents():
    # The probe's post-write early exit only exists on the codex app-server;
    # a resident stream/ACP turn would block to provider completion.
    for provider in ("claude", "grok"):
        assert coop_autonomous.resident_completion_probe_mode(
            "persistent",
            provider=provider,
            completion_probe_attached=True,
        ) == "cold"
        assert coop_autonomous.resident_completion_probe_mode(
            "persistent",
            provider=provider,
            completion_probe_attached=False,
        ) == "persistent"
    assert coop_autonomous.resident_completion_probe_mode(
        "persistent",
        provider="codex",
        completion_probe_attached=True,
    ) == "persistent"
    assert coop_autonomous.resident_completion_probe_mode(
        "cold",
        provider="claude",
        completion_probe_attached=True,
    ) == "cold"


def test_persistent_provider_flags_are_normalized_without_widening_membership():
    assert coop_autonomous.normalize_persistent_providers(
        ["codex", "codex", "grok"],
        participants=["claude", "codex", "grok"],
    ) == {"codex", "grok"}

    try:
        coop_autonomous.normalize_persistent_providers(
            ["grok"],
            participants=["claude", "codex"],
        )
    except ValueError as exc:
        assert str(exc) == "persistent provider is not a run participant: grok"
    else:
        raise AssertionError("non-participant must be rejected")


def test_public_start_forwards_opt_in_claude_prompt_cache_hint(monkeypatch):
    captured = []
    monkeypatch.setattr(
        coop_autonomous,
        "main",
        lambda argv: captured.append(argv) or 0,
    )
    args = coopcli.build_parser().parse_args([
        "start",
        "--item",
        "25",
        "--prompt-cache-provider",
        "claude",
    ])

    args.fn(None, args)

    assert args.prompt_cache_provider == ["claude"]
    position = captured[0].index("--prompt-cache-provider")
    assert captured[0][position + 1] == "claude"


def test_cold_workers_forwards_runner_opt_out(monkeypatch):
    captured = []
    monkeypatch.setattr(
        coop_autonomous,
        "main",
        lambda argv: captured.append(argv) or 0,
    )
    args = coopcli.build_parser().parse_args([
        "start", "--item", "25", "--cold-workers",
        "--persistent-provider", "claude",
    ])

    args.fn(None, args)

    # --cold-workers wins over any explicit opt-in and reaches the runner
    # as its --no-persistent-workers debug flag.
    assert "--no-persistent-workers" in captured[0]
    assert "--persistent-provider" not in captured[0]


def test_opt_in_pool_prepares_isolated_codex_app_server_without_starting_it(
    tmp_path, monkeypatch,
):
    builds = []
    cleaned = []
    runtime_root = tmp_path.parent / f"{tmp_path.name}-private-runtime"
    monkeypatch.setenv("COOP_RUNTIME_ROOT", str(runtime_root))

    def build_profile(provider, argv, manifest, **kwargs):
        builds.append((provider, argv, manifest, kwargs))
        return SimpleNamespace(
            argv=list(argv),
            env=dict(kwargs["env"]),
            cleanup=lambda provider=provider: cleaned.append(provider),
        )

    pool, cleanups, unavailable = (
        coop_autonomous.prepare_opt_in_worker_pool(
            {"claude", "codex", "grok"},
            sessions={
                "claude": "coop-claude",
                "codex": "coop-codex",
                "grok": "coop-grok",
            },
            board_path=tmp_path / "board.db",
            cwd=tmp_path,
            run_dir=tmp_path / "run",
            action_lease_seconds=900,
            item_id=25,
            resolve_argv=lambda provider: [f"{provider}.exe"],
            build_profile=build_profile,
        )
    )

    assert unavailable == {}
    assert pool.health() == {
        "claude": coop_autonomous.coop_workers.WorkerHealth(
            provider="claude",
            mode="persistent",
            state="new",
            process_starts=0,
            turns_submitted=0,
        ),
        "codex": coop_autonomous.coop_workers.WorkerHealth(
            provider="codex",
            mode="persistent",
            state="new",
            process_starts=0,
            turns_submitted=0,
        ),
        "grok": coop_autonomous.coop_workers.WorkerHealth(
            provider="grok",
            mode="persistent",
            state="new",
            process_starts=0,
            turns_submitted=0,
        ),
    }
    assert [build[0] for build in builds] == ["claude", "codex", "grok"]
    by_provider = {build[0]: build for build in builds}
    provider, argv, manifest, kwargs = by_provider["claude"]
    assert argv[:2] == ["claude.exe", "-p"]
    assert manifest.name == "board_core"
    assert kwargs["servers"] == {}
    assert kwargs["workspace"] == tmp_path.resolve()
    assert kwargs["env"]["COOP_SESSION_ID"] == "coop-claude"
    provider, argv, manifest, kwargs = by_provider["codex"]
    assert argv[:2] == ["codex.exe", "app-server"]
    assert manifest.name == "board_core"
    assert kwargs["servers"] == {}
    assert kwargs["workspace"] == tmp_path.resolve()
    assert kwargs["env"]["COOP_SESSION_ID"] == "coop-codex"
    assert kwargs["env"]["COOP_ITEM_ID"] == "25"
    provider, argv, manifest, kwargs = by_provider["grok"]
    assert argv[:3] == ["grok.exe", "agent", "stdio"]
    assert manifest.name == "board_core"
    assert kwargs["provider_state_dir"].name.startswith(
        "worker-state-grok-"
    )
    assert kwargs["workspace"] == tmp_path.resolve()
    assert cleaned == []
    assert len(list(runtime_root.glob("worker-state-grok-*"))) == 1
    pool.stop_all()
    for cleanup in cleanups:
        cleanup()
    assert cleaned == ["claude", "codex", "grok"]
    assert list(runtime_root.glob("worker-state-grok-*")) == []


def _prepared_base_argv(tmp_path, **kwargs):
    """Return the pre-profile argv each resident provider is built with."""
    builds = {}

    def build_profile(provider, argv, manifest, **profile_kwargs):
        builds[provider] = list(argv)
        return SimpleNamespace(
            argv=list(argv),
            env=dict(profile_kwargs["env"]),
            cleanup=lambda: None,
        )

    runtime_root = tmp_path.parent / f"{tmp_path.name}-private-runtime"
    with mock.patch.dict(
            os.environ,
            {"COOP_RUNTIME_ROOT": str(runtime_root)},
            clear=False):
        pool, cleanups, unavailable = (
            coop_autonomous.prepare_opt_in_worker_pool(
                {"claude", "codex", "grok"},
                sessions={
                    "claude": "coop-claude",
                    "codex": "coop-codex",
                    "grok": "coop-grok",
                },
                board_path=tmp_path / "board.db",
                cwd=tmp_path,
                run_dir=tmp_path / "run",
                action_lease_seconds=900,
                item_id=25,
                resolve_argv=lambda provider: [f"{provider}.exe"],
                build_profile=build_profile,
                **kwargs,
            )
        )

    assert unavailable == {}
    pool.stop_all()
    for cleanup in cleanups:
        cleanup()
    return builds


def test_resident_claude_argv_carries_the_opt_in_prompt_cache_hint(tmp_path):
    # A traced cache hint that never reaches the argv is false evidence.
    opted_in = _prepared_base_argv(
        tmp_path / "opted-in",
        prompt_cache_providers=frozenset({"claude"}),
    )
    default = _prepared_base_argv(tmp_path / "default")

    assert PROMPT_CACHE_FLAG in opted_in["claude"]
    assert PROMPT_CACHE_FLAG not in default["claude"]
    for provider in ("codex", "grok"):
        assert PROMPT_CACHE_FLAG not in opted_in[provider]
        assert PROMPT_CACHE_FLAG not in default[provider]

    # The hint and the stream transport both insert before -p.
    argv = coop_resident_workers.claude_stream_argv(
        opted_in["claude"],
        provider_session_id="claude-session-1",
        resume=False,
    )
    assert argv.count("-p") == 1
    assert argv.count(PROMPT_CACHE_FLAG) == 1
    assert argv.index(PROMPT_CACHE_FLAG) < argv.index("-p")
    assert argv.index("--session-id") < argv.index("-p")
    assert argv.index("--input-format") < argv.index("-p")


def test_injected_turn_transport_never_launches_real_resident_clis(tmp_path):
    builds = []

    pool, cleanups, unavailable = (
        coop_autonomous.prepare_opt_in_worker_pool(
            {"claude", "grok"},
            sessions={"claude": "coop-claude", "grok": "coop-grok"},
            board_path=tmp_path / "board.db",
            cwd=tmp_path,
            run_dir=tmp_path / "run",
            action_lease_seconds=900,
            item_id=25,
            invoke_turn=lambda **kwargs: kwargs,
            resolve_argv=lambda provider: [f"{provider}.exe"],
            build_profile=lambda *args, **kwargs: builds.append(
                (args, kwargs)
            ),
        )
    )

    assert unavailable == {}
    assert cleanups == ()
    assert builds == []
    assert {
        provider: health.mode
        for provider, health in pool.health().items()
    } == {"claude": "resumed", "grok": "resumed"}
