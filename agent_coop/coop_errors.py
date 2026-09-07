"""Central typed error taxonomy for Agent Co-op.

Every domain rejection is a CoopError subclass carrying a stable lowercase
slug in `.type`. The CLI renders `error: <type>: <message>` with exit code 1
(JSON mode: {"error": {"type": ..., "message": ...}}). The list is
extensible, not exhaustive.
"""

import copy
import types


ERROR_REASON_CODES = frozenset({
    "coop_error",
    "input_invalid",
    "target_not_found",
    "transition_not_available",
    "field_already_set",
    "blocking_work_open",
    "session_unavailable",
    "actor_mismatch",
    "provider_conflict",
    "active_session_conflict",
    "human_lane_forbidden",
    "addressed_target_mismatch",
    "peer_unavailable",
    "awaiting_peer",
    "contract_incomplete",
    "contract_acceptance_required",
    "claim_conflict",
    "claim_expired",
    "claim_not_current",
    "claim_lane_mismatch",
    "unsafe_reclaim",
    "receipt_missing",
    "receipt_invalid",
    "receipt_stale",
    "receipt_hash_mismatch",
    "proof_invalid",
    "reviewer_is_owner",
    "review_provider_already_approved",
    "second_reviewer_not_selected",
    "review_missing",
    "review_stale",
    "decision_unobserved",
    "schema_mismatch",
    "invalid_agent_name",
    "invalid_timing",
    "projection_path_invalid",
    "adapter_path_invalid",
    "process_tree_unavailable",
    "launch_failed",
    "migration_failed",
})

RUNNER_DETAIL_CODES = frozenset({
    "no_actionable_participant",
    "actionable_no_board_progress",
    "turn_budget_exhausted",
})

EVIDENCE_KEYS = frozenset({
    "item_id", "claim_id", "question_id", "handoff_id", "review_id",
    "receipt_id", "huddle_id", "decision_id", "actor_agent_id",
    "owner_agent_id", "target_agent_id", "required_agent_id", "provider",
    "current_state", "required_state", "allowed_states", "current_status",
    "required_status", "claim_kind", "required_claim_kind", "lane",
    "contract_version", "required_contract_version", "required_count",
    "observed_count", "constraint", "field", "blocking_object_type",
    "blocking_ids", "session_status", "lease_expired", "lease_expires_at",
    "actionable_agents", "action_kinds", "idle_rounds", "noop_cycles",
    "turns", "max_turns", "approved_providers", "remaining_providers",
})

_MAX_EVIDENCE_STRING = 256
_MAX_EVIDENCE_LIST = 32


def _evidence_scalar(value):
    return value is None or isinstance(value, (str, int, bool))


def normalize_evidence(evidence):
    if evidence is None:
        return types.MappingProxyType({})
    if not isinstance(evidence, dict):
        raise TypeError("error evidence must be a mapping")
    normalized = {}
    for key, value in evidence.items():
        if key not in EVIDENCE_KEYS:
            raise ValueError(f"unsafe error evidence key: {key}")
        if isinstance(value, str) and len(value) > _MAX_EVIDENCE_STRING:
            raise ValueError(f"error evidence value too long: {key}")
        if isinstance(value, (list, tuple)):
            if len(value) > _MAX_EVIDENCE_LIST:
                raise ValueError(f"error evidence list too long: {key}")
            if not all(_evidence_scalar(entry) for entry in value):
                raise TypeError(f"error evidence list must be scalar: {key}")
            if any(
                isinstance(entry, str)
                and len(entry) > _MAX_EVIDENCE_STRING
                for entry in value
            ):
                raise ValueError(f"error evidence value too long: {key}")
            normalized[key] = tuple(value)
        elif _evidence_scalar(value):
            normalized[key] = value
        else:
            raise TypeError(f"error evidence value must be scalar: {key}")
    return types.MappingProxyType(normalized)


def evidence_dict(evidence):
    return {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in evidence.items()
    }


class CoopError(Exception):
    type = "coop_error"
    default_reason_code = "coop_error"

    def __init__(self, message, *, reason_code=None, evidence=None):
        code = self.default_reason_code if reason_code is None else reason_code
        if not isinstance(code, str) or code not in ERROR_REASON_CODES:
            raise ValueError(f"unknown error reason code: {code!r}")
        super().__init__(message)
        self.reason_code = code
        self._evidence = normalize_evidence(evidence)
        self._legal_next_actions = ()

    @property
    def evidence(self):
        return self._evidence

    @property
    def legal_next_actions(self):
        return copy.deepcopy(list(self._legal_next_actions))

    def attach_legal_next_actions(self, actions):
        copied = copy.deepcopy(list(actions or ()))
        self._legal_next_actions = tuple(copied)
        return self

    def as_dict(self):
        return {
            "type": self.type,
            "message": str(self),
            "reason_code": self.reason_code,
            "evidence": evidence_dict(self._evidence),
            "legal_next_actions": self.legal_next_actions,
        }


class SchemaMismatch(CoopError):
    type = "schema_mismatch"
    default_reason_code = "schema_mismatch"


class AdapterPathInvalid(CoopError):
    type = "adapter_path_invalid"
    default_reason_code = "adapter_path_invalid"


class SessionMismatch(CoopError):
    type = "session_mismatch"
    default_reason_code = "session_unavailable"


class ProviderConflict(CoopError):
    type = "provider_conflict"
    default_reason_code = "provider_conflict"


class ActiveSessionConflict(CoopError):
    type = "active_session_conflict"
    default_reason_code = "active_session_conflict"


class ClaimCollision(CoopError):
    type = "claim_collision"
    default_reason_code = "claim_conflict"


class StaleClaim(CoopError):
    type = "stale_claim"
    default_reason_code = "claim_not_current"


class AddressedTargetMismatch(CoopError):
    type = "addressed_target_mismatch"
    default_reason_code = "addressed_target_mismatch"


class IncompleteContract(CoopError):
    type = "incomplete_contract"
    default_reason_code = "contract_incomplete"


class UnsafeReclaim(CoopError):
    type = "unsafe_reclaim"
    default_reason_code = "unsafe_reclaim"


class InvalidTransition(CoopError):
    type = "invalid_transition"
    default_reason_code = "transition_not_available"


class NotFound(CoopError):
    type = "not_found"
    default_reason_code = "target_not_found"


class HumanLaneViolation(CoopError):
    type = "human_lane_violation"
    default_reason_code = "human_lane_forbidden"


class InvalidAgentName(CoopError):
    # Agent names double as inbox filenames, so a hostile name is an
    # arbitrary-file-write attempt, not a mere transition error.
    type = "invalid_agent_name"
    default_reason_code = "invalid_agent_name"


class ReceiptInvalid(CoopError):
    # The receipt file itself is unusable as evidence —
    # missing, empty, or unreadable at submission time.
    type = "receipt_invalid"
    default_reason_code = "receipt_invalid"


class ProofReferenceInvalid(CoopError):
    # A typed proof reference failed mechanical
    # verification — bad grammar, a board row that does not exist or does
    # not attach to the item, or an unresolvable file reference.
    type = "proof_reference_invalid"
    default_reason_code = "proof_invalid"


class ReceiptMissing(CoopError):
    # No current unsuperseded receipt where one is
    # required (review request and completion).
    type = "receipt_missing"
    default_reason_code = "receipt_missing"


class SelfReview(CoopError):
    # The reviewer would equal the item's durable owner —
    # at request-naming, claim, verdict, or completion's approval-author
    # check.
    type = "self_review"
    default_reason_code = "reviewer_is_owner"


class ReviewStale(CoopError):
    # A claim or verdict attempted
    # against a review dead by derivation — its receipt or contract
    # version is no longer the item's current one, or it is resolved.
    type = "review_stale"
    default_reason_code = "review_stale"


class DecisionUnobserved(CoopError):
    # The observation watermark — a binding decision event
    # is newer than the review claim's newest observation event (claim
    # creation or a later checkpoint on that claim). The claim is left
    # live; one checkpoint returns the decision and unblocks the verdict.
    type = "decision_unobserved"
    default_reason_code = "decision_unobserved"


class ReceiptStale(CoopError):
    # The current receipt's contract version no longer
    # matches the item's — the version-tamper guard. No legal path can
    # produce it before completion checks it (revision supersedes).
    type = "receipt_stale"
    default_reason_code = "receipt_stale"


class ReviewMissing(CoopError):
    # Completion requires a qualifying approval on the
    # item's current receipt and none qualifies — never requested,
    # unresolved, `changes`, derivation-dead, or fenced by a post-verdict
    # decision. Only the reviewer-authored case is `self_review` instead.
    type = "review_missing"
    default_reason_code = "review_missing"


class ReceiptHashMismatch(CoopError):
    # Completion-time file evidence changed — the primary
    # receipt file or a file: reference missing or hash-mismatched. The
    # supersede and its event COMMIT first; the core returns a failed
    # outcome and only the command layer raises this.
    type = "receipt_hash_mismatch"
    default_reason_code = "receipt_hash_mismatch"


class InvalidTiming(CoopError):
    # A CLI timing flag outside 1..31_536_000 seconds,
    # refused at the conversion chokepoint before any database or process
    # work — a typed exit instead of an OverflowError traceback.
    type = "invalid_timing"
    default_reason_code = "invalid_timing"


class ProjectionPathInvalid(CoopError):
    # The resolved projection out-dir escapes the
    # resolved launch working directory. Raised only by the session-start
    # preflight — before any process preparation, so it can never fire
    # while a child tree is alive. The runtime recheck warns instead.
    type = "projection_path_invalid"
    default_reason_code = "projection_path_invalid"


class ProcessTreeUnavailable(CoopError):
    type = "process_tree_unavailable"
    default_reason_code = "process_tree_unavailable"


class LaunchFailed(CoopError):
    type = "launch_failed"
    default_reason_code = "launch_failed"


class MigrationFailed(CoopError):
    type = "migration_failed"
    default_reason_code = "migration_failed"


class FieldAlreadySet(CoopError):
    # Goal-tasks: `item define` fills EMPTY contract fields only. Refusing to
    # overwrite a field the human already set — their intent is immutable
    # from the agent lane; the human-lane `revise` changes a set field.
    type = "field_already_set"
    default_reason_code = "field_already_set"
