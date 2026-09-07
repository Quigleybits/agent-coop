"""Bounded, non-secret classification of terminal provider failures."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderFailure:
    classification: str
    retryable: bool


_RULES = {
    "claude": (
        (
            "provider_quota_exhausted",
            ("you've hit your monthly spend limit",),
        ),
        (
            "provider_auth_required",
            ("not logged in", "run /login"),
        ),
    ),
    "codex": (
        (
            "provider_auth_required",
            ("not logged in", "codex login"),
        ),
    ),
    "grok": (
        (
            "provider_auth_required",
            ("authentication required", "grok login"),
        ),
    ),
}

TERMINAL_CLASSIFICATIONS = frozenset({
    "provider_quota_exhausted",
    "provider_auth_required",
    "worker_start_failed",
    "worker_protocol_failed",
    "resume_failed",
    "worker_cleanup_failed",
    "capability_activation_failed",
    "process_tree_cleanup_failed",
    "capability_shutdown_failed",
    "turn_timeout",
})


def classify_provider_failure(
        provider: str,
        *,
        exit_code: int | None,
        output: str) -> ProviderFailure | None:
    """Return a typed terminal failure for an exact provider signature.

    Generic payment, rate-limit, or authentication text deliberately remains
    unclassified so the existing transient-failure policy can retry it.
    """
    if exit_code is None or exit_code == 0:
        return None
    normalized = " ".join(str(output or "").lower().split())
    for classification, required_fragments in _RULES.get(provider, ()):
        if all(fragment in normalized for fragment in required_fragments):
            return ProviderFailure(
                classification=classification,
                retryable=False,
            )
    return None


def terminal_reason(result) -> str | None:
    """Extract only a known, explicitly non-retryable run reason."""
    if not isinstance(result, dict) or result.get("retryable") is not False:
        return None
    classification = result.get("classification")
    if classification in TERMINAL_CLASSIFICATIONS:
        return classification
    return None
