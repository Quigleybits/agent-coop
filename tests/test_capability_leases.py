"""Offline call-lazy capability lease tests."""

from __future__ import annotations

import dataclasses

import pytest

from agent_coop import coop_capability_leases


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def test_supported_capability_activates_on_first_acquire_and_reuses():
    clock = _Clock()
    starts = []
    stops = []
    pool = coop_capability_leases.CapabilityLeasePool(
        activator=lambda key, config: starts.append((key, config)) or {
            "tree": len(starts),
        },
        deactivator=lambda resource: stops.append(resource),
        dynamic_support={"codex": True},
        clock=clock,
    )
    config = {
        "name": "research",
        "command": "fake-mcp",
        "token": "must-not-appear-in-key",
    }

    assert starts == []
    first = pool.acquire(
        run_id="run-1",
        provider="codex",
        server_config=config,
    )
    first.release()
    clock.value += 10
    second = pool.acquire(
        run_id="run-1",
        provider="codex",
        server_config=config,
    )

    assert len(starts) == 1
    assert first.resource is second.resource
    assert first.key.config_hash == second.key.config_hash
    assert "must-not-appear" not in repr(first.key)
    assert pool.stats()["starts"] == 1
    second.release()
    pool.stop_all()
    assert stops == [{"tree": 1}]


def test_in_use_lease_survives_idle_eviction_then_expires_at_sixty_seconds():
    clock = _Clock()
    stopped = []
    pool = coop_capability_leases.CapabilityLeasePool(
        activator=lambda key, config: object(),
        deactivator=stopped.append,
        dynamic_support={"codex": True},
        idle_ttl_s=60,
        clock=clock,
    )
    lease = pool.acquire(
        run_id="run-1",
        provider="codex",
        server_config={"name": "fake"},
    )
    resource = lease.resource

    clock.value += 120
    assert pool.evict_idle() == 0
    lease.release()
    clock.value += 59.9
    assert pool.evict_idle() == 0
    clock.value += 0.1
    assert pool.evict_idle() == 1
    assert stopped == [resource]


def test_unsupported_provider_is_explicit_turn_lazy_fallback():
    pool = coop_capability_leases.CapabilityLeasePool(
        activator=lambda key, config: pytest.fail("must not activate"),
        deactivator=lambda resource: pytest.fail("must not deactivate"),
        dynamic_support=coop_capability_leases.DYNAMIC_ATTACHMENT_SUPPORT,
    )

    for provider in ("claude", "codex", "grok"):
        assert pool.attachment_mode(provider) == "turn_lazy"
        with pytest.raises(
            coop_capability_leases.CapabilityAttachmentUnsupported,
        ):
            pool.acquire(
                run_id="run-1",
                provider=provider,
                server_config={"name": "fake"},
            )


def test_leases_are_scoped_by_run_provider_and_configuration_hash():
    starts = []
    pool = coop_capability_leases.CapabilityLeasePool(
        activator=lambda key, config: starts.append(key) or object(),
        deactivator=lambda resource: None,
        dynamic_support={"codex": True, "grok": True},
    )
    handles = [
        pool.acquire(
            run_id=run_id,
            provider=provider,
            server_config={"name": config_name},
        )
        for run_id, provider, config_name in (
            ("run-1", "codex", "a"),
            ("run-2", "codex", "a"),
            ("run-1", "grok", "a"),
            ("run-1", "codex", "b"),
        )
    ]

    assert len(starts) == 4
    assert len(set(starts)) == 4
    assert all(dataclasses.is_dataclass(key) for key in starts)
    for handle in handles:
        handle.release()
    pool.stop_all()


def test_double_release_and_stop_are_idempotent_and_cleanup_is_reverse_order():
    stopped = []
    pool = coop_capability_leases.CapabilityLeasePool(
        activator=lambda key, config: config["name"],
        deactivator=stopped.append,
        dynamic_support={"codex": True},
    )
    first = pool.acquire(
        run_id="run-1",
        provider="codex",
        server_config={"name": "first"},
    )
    second = pool.acquire(
        run_id="run-1",
        provider="codex",
        server_config={"name": "second"},
    )

    first.release()
    first.release()
    second.release()
    pool.stop_all()
    pool.stop_all()

    assert stopped == ["second", "first"]


def test_failed_deactivation_retains_lease_for_retry_and_counts_only_success():
    attempts = []

    def deactivate(resource):
        attempts.append(resource)
        if len(attempts) == 1:
            raise OSError("private capability detail")

    pool = coop_capability_leases.CapabilityLeasePool(
        activator=lambda key, config: config["name"],
        deactivator=deactivate,
        dynamic_support={"codex": True},
    )
    lease = pool.acquire(
        run_id="run-1",
        provider="codex",
        server_config={"name": "research"},
    )
    lease.release()

    with pytest.raises(
        coop_capability_leases.CapabilityCleanupError,
    ) as first:
        pool.stop_all()

    assert "private capability detail" not in str(first.value)
    assert pool.stats() == {
        "starts": 1,
        "stops": 0,
        "active": 1,
        "in_use": 0,
    }

    pool.stop_all()
    assert attempts == ["research", "research"]
    assert pool.stats() == {
        "starts": 1,
        "stops": 1,
        "active": 0,
        "in_use": 0,
    }
