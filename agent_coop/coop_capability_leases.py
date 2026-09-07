"""Run-scoped leases for dynamically attachable external capabilities.

The public support matrix remains conservative until live provider smokes
prove dynamic attachment. Unsupported providers use the existing isolated
turn-lazy launch profile instead.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from collections.abc import Mapping
from typing import Any


DYNAMIC_ATTACHMENT_SUPPORT = {
    "claude": False,
    "codex": False,
    "grok": False,
}


class CapabilityLeaseError(RuntimeError):
    pass


class CapabilityAttachmentUnsupported(CapabilityLeaseError):
    pass


class CapabilityLeasePoolStopped(CapabilityLeaseError):
    pass


class CapabilityCleanupError(CapabilityLeaseError):
    def __init__(self, failures):
        self.failures = tuple(str(value) for value in failures)
        super().__init__(
            f"capability_cleanup_failed ({len(self.failures)})"
        )


@dataclasses.dataclass(frozen=True)
class CapabilityLeaseKey:
    run_id: str
    provider: str
    config_hash: str


@dataclasses.dataclass
class _Entry:
    resource: Any
    in_use: int
    last_used: float


class CapabilityLease:
    def __init__(self, pool, key, resource):
        self.key = key
        self.resource = resource
        self._pool = pool
        self._released = False
        self._lock = threading.Lock()

    def release(self):
        with self._lock:
            if self._released:
                return
            self._released = True
        self._pool.release(self.key)

    def __enter__(self):
        return self.resource

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False


def configuration_hash(server_config: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(server_config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CapabilityLeasePool:
    def __init__(
        self,
        *,
        activator,
        deactivator,
        dynamic_support=None,
        idle_ttl_s=60,
        clock=None,
    ):
        self._activator = activator
        self._deactivator = deactivator
        self._dynamic_support = dict(
            DYNAMIC_ATTACHMENT_SUPPORT
            if dynamic_support is None
            else dynamic_support
        )
        self._idle_ttl_s = float(idle_ttl_s)
        self._clock = clock or time.monotonic
        self._entries: dict[CapabilityLeaseKey, _Entry] = {}
        self._activation_order: list[CapabilityLeaseKey] = []
        self._starts = 0
        self._stops = 0
        self._stopped = False
        self._stop_requested = False
        self._lock = threading.RLock()

    def attachment_mode(self, provider: str) -> str:
        return (
            "call_lazy"
            if self._dynamic_support.get(str(provider), False)
            else "turn_lazy"
        )

    def acquire(
        self,
        *,
        run_id: str,
        provider: str,
        server_config: Mapping[str, Any],
    ) -> CapabilityLease:
        provider = str(provider)
        if not self._dynamic_support.get(provider, False):
            raise CapabilityAttachmentUnsupported(provider)
        config = dict(server_config)
        key = CapabilityLeaseKey(
            run_id=str(run_id),
            provider=provider,
            config_hash=configuration_hash(config),
        )
        with self._lock:
            if self._stopped or self._stop_requested:
                raise CapabilityLeasePoolStopped("capability lease pool")
            entry = self._entries.get(key)
            if entry is None:
                resource = self._activator(key, config)
                entry = _Entry(
                    resource=resource,
                    in_use=0,
                    last_used=self._clock(),
                )
                self._entries[key] = entry
                self._activation_order.append(key)
                self._starts += 1
            entry.in_use += 1
            return CapabilityLease(self, key, entry.resource)

    def release(self, key: CapabilityLeaseKey) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.in_use <= 0:
                return
            entry.in_use -= 1
            entry.last_used = self._clock()

    def evict_idle(self, *, now=None) -> int:
        current = self._clock() if now is None else float(now)
        failures = []
        stopped = 0
        with self._lock:
            if self._stopped:
                return 0
            for key in tuple(self._activation_order):
                entry = self._entries.get(key)
                if (
                    entry is None
                    or entry.in_use
                    or current - entry.last_used < self._idle_ttl_s
                ):
                    continue
                try:
                    self._deactivator(entry.resource)
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    continue
                del self._entries[key]
                self._activation_order.remove(key)
                self._stops += 1
                stopped += 1
        if failures:
            raise CapabilityCleanupError(failures)
        return stopped

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "starts": self._starts,
                "stops": self._stops,
                "active": len(self._entries),
                "in_use": sum(
                    entry.in_use for entry in self._entries.values()
                ),
            }

    def stop_all(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stop_requested = True
            failures = []
            for key in reversed(tuple(self._activation_order)):
                entry = self._entries.get(key)
                if entry is None:
                    continue
                try:
                    self._deactivator(entry.resource)
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    continue
                del self._entries[key]
                self._activation_order.remove(key)
                self._stops += 1
            if failures:
                raise CapabilityCleanupError(failures)
            self._stopped = True


__all__ = [
    "DYNAMIC_ATTACHMENT_SUPPORT",
    "CapabilityAttachmentUnsupported",
    "CapabilityCleanupError",
    "CapabilityLease",
    "CapabilityLeaseError",
    "CapabilityLeaseKey",
    "CapabilityLeasePool",
    "CapabilityLeasePoolStopped",
    "configuration_hash",
]
