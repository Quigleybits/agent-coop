"""Deterministic workflow recipes for one selected Co-op item."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


RECIPE_VERSION = 1
CORE_PROVIDERS = frozenset({"claude", "codex", "grok"})
QUICK_TWO_TAGS = frozenset({
    "recipe:quick-two",
    "quick:bounded",
    "quick:low-risk",
    "quick:reversible",
    "quick:no-research",
    "quick:no-three-party",
    "quick:no-high-authority",
})
PROMOTION_REASONS = frozenset({
    "needs_input",
    "review_changes",
    "contract_expanded",
    "reserve_directed",
})
DIRECTED_RESERVE_ACTIONS = frozenset({
    "answer_question",
    "respond_handoff",
    "huddle_post",
    "huddle_close",
    "review_task",
    "continue_task",
    "resume_task",
    "recover_claim",
})

STANDARD_THREE_STAGES = (
    "implementation",
    "independent_contributions",
    "synthesis",
    "independent_review",
)
QUICK_TWO_STAGES = (
    "implementation",
    "independent_review",
)


class RecipeBlocked(RuntimeError):
    """The deterministic recipe contract cannot be satisfied."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class WorkflowRecipe:
    version: int
    name: str
    contract_version: int
    lead: str
    active_participants: tuple[str, ...]
    reserve_participants: tuple[str, ...]
    stages: tuple[str, ...]
    promoted_from: str | None = None
    promotion_reason: str | None = None

    def as_payload(self) -> dict:
        """Return the complete non-secret, JSON-safe frozen recipe."""
        return {
            "version": self.version,
            "name": self.name,
            "contract_version": self.contract_version,
            "lead": self.lead,
            "active_participants": list(self.active_participants),
            "reserve_participants": list(self.reserve_participants),
            "stages": list(self.stages),
            "promoted_from": self.promoted_from,
            "promotion_reason": self.promotion_reason,
        }


def _participants(ordered_participants) -> tuple[str, ...]:
    try:
        participants = tuple(ordered_participants)
    except TypeError as exc:
        raise RecipeBlocked(
            "standard_three_requires_claude_codex_grok"
        ) from exc
    if (
            len(participants) != 3
            or len(set(participants)) != 3
            or set(participants) != CORE_PROVIDERS):
        raise RecipeBlocked(
            "standard_three_requires_claude_codex_grok"
        )
    return participants


def _contract_version(item) -> int:
    if not isinstance(item, Mapping):
        return 1
    value = item.get("contract_version", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _allowed_tags(item) -> frozenset[str]:
    if not isinstance(item, Mapping):
        return frozenset()
    values = item.get("allowed_actions", ())
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        value
        for value in values
        if isinstance(value, str) and value
    )


def _quick_two_allowed(item) -> bool:
    if not isinstance(item, Mapping):
        return False
    quorum = item.get("review_quorum")
    return (
        not isinstance(quorum, bool)
        and quorum == 1
        and QUICK_TWO_TAGS.issubset(_allowed_tags(item))
    )


def compile_recipe(item, ordered_participants) -> WorkflowRecipe:
    """Compile one immutable recipe without interpreting task prose."""
    participants = _participants(ordered_participants)
    common = {
        "version": RECIPE_VERSION,
        "contract_version": _contract_version(item),
        "lead": participants[0],
    }
    if _quick_two_allowed(item):
        return WorkflowRecipe(
            **common,
            name="quick_two",
            active_participants=participants[:2],
            reserve_participants=participants[2:],
            stages=QUICK_TWO_STAGES,
        )
    return WorkflowRecipe(
        **common,
        name="standard_three",
        active_participants=participants,
        reserve_participants=(),
        stages=STANDARD_THREE_STAGES,
    )


def promote_to_standard_three(
        recipe: WorkflowRecipe,
        reason: str) -> WorkflowRecipe:
    """Activate a quick-two reserve through one allowlisted edge."""
    if recipe.name != "quick_two":
        raise RecipeBlocked("recipe_already_standard_three")
    if reason not in PROMOTION_REASONS:
        raise RecipeBlocked("unsupported_promotion_reason")
    participants = (
        recipe.active_participants + recipe.reserve_participants
    )
    if (
            len(participants) != 3
            or set(participants) != CORE_PROVIDERS):
        raise RecipeBlocked(
            "standard_three_requires_claude_codex_grok"
        )
    return WorkflowRecipe(
        version=recipe.version,
        name="standard_three",
        contract_version=recipe.contract_version,
        lead=recipe.lead,
        active_participants=participants,
        reserve_participants=(),
        stages=STANDARD_THREE_STAGES,
        promoted_from="quick_two",
        promotion_reason=reason,
    )


def detect_promotion_reason(
        recipe: WorkflowRecipe,
        *,
        current_contract_version,
        events,
        reserve_action) -> str | None:
    """Map bounded board evidence to one precompiled promotion edge."""
    if recipe.name != "quick_two":
        return None
    if (
            isinstance(current_contract_version, int)
            and not isinstance(current_contract_version, bool)
            and current_contract_version > recipe.contract_version):
        return "contract_expanded"
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("event_type")
        if event_type == "needs_input":
            return "needs_input"
        payload = event.get("payload")
        if (
                event_type == "review_resolved"
                and isinstance(payload, Mapping)
                and payload.get("verdict") == "changes"):
            return "review_changes"
    if isinstance(reserve_action, Mapping):
        kind = reserve_action.get("kind")
        if kind in DIRECTED_RESERVE_ACTIONS:
            return "reserve_directed"
    return None


__all__ = [
    "CORE_PROVIDERS",
    "DIRECTED_RESERVE_ACTIONS",
    "PROMOTION_REASONS",
    "QUICK_TWO_TAGS",
    "RECIPE_VERSION",
    "RecipeBlocked",
    "WorkflowRecipe",
    "compile_recipe",
    "detect_promotion_reason",
    "promote_to_standard_three",
]
