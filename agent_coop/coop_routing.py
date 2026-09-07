"""Soft provider profiles for initial Agent Co-op ownership.

Profiles affect only ordering among providers that are already available.
They never decide eligibility or override board-derived ``next_action``.
"""

from __future__ import annotations

from collections.abc import Mapping


PROVIDER_PROFILES = {
    "claude": {
        "role": "orchestration",
        "keywords": (
            "orchestration", "orchestrate", "coordinate", "coordination",
            "multi-agent", "multi-provider", "huddle", "handoff",
            "consensus", "synthesize", "synthesis",
        ),
    },
    "codex": {
        "role": "code review and deep dives",
        "keywords": (
            "code review", "review", "audit", "deep dive", "deep-dive",
            "debug", "diagnose", "architecture", "analysis", "analyze",
            "inspect",
        ),
    },
    "grok": {
        "role": "fast tasks and research",
        "keywords": (
            "fast", "quick", "research", "survey", "compare", "lookup",
            "search", "explore", "investigate",
        ),
    },
}

# Completion/review boilerplate is intentionally excluded: it describes the
# protocol gate, not the substantive work whose initial owner we are choosing.
_TASK_TEXT_FIELDS = ("title", "objective", "scope", "context")


def _task_text(task) -> str:
    if not isinstance(task, Mapping):
        return ""
    values = []
    for field in _TASK_TEXT_FIELDS:
        value = task.get(field)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(part) for part in value)
        elif value is not None:
            values.append(str(value))
    return " ".join(" ".join(values).lower().split())


def _profile_score(provider: str, text: str) -> int:
    profile = PROVIDER_PROFILES.get(provider)
    if not profile or not text:
        return 0
    return sum(text.count(keyword) for keyword in profile["keywords"])


def soft_order_participants(participants, task=None) -> list[str]:
    """Prefer a task-fit provider while retaining every incoming participant.

    Equal and zero scores preserve caller order, making the profiles a
    deterministic hint rather than an eligibility or authority mechanism.
    """
    original = list(participants)
    text = _task_text(task)
    if not text:
        return original
    scored = [
        (-_profile_score(name, text), index, name)
        for index, name in enumerate(original)
    ]
    return [name for _score, _index, name in sorted(scored)]
