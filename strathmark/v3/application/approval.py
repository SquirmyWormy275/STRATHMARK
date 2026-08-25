"""Exception-first, deterministic approval projection contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import (
    require_idempotency_key,
    require_identifier,
)
from strathmark.v3.domain.disagreement import ConsequenceColor

_VERIFIED_RECEIPT_AUTHORITY = object()


class ApprovalError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalConflictChange:
    field_id: str
    prior_receipt_id: str | None
    replacement_receipt_id: str | None
    replacement_revision: int | None
    reason_code: str


class ApprovalConflict(ApprovalError):
    def __init__(
        self,
        message: str,
        replacements: tuple[tuple[str, str, int], ...] = (),
        *,
        changes: tuple[ApprovalConflictChange, ...] = (),
        total_changes: int | None = None,
    ) -> None:
        super().__init__(message)
        if total_changes is not None and (
            isinstance(total_changes, bool)
            or not isinstance(total_changes, int)
            or total_changes < len(changes)
        ):
            raise ApprovalError("approval conflict change count is invalid")
        self.replacements = replacements
        self.changes = changes
        self.total_changes = len(changes) if total_changes is None else total_changes
        self.changes_truncated = self.total_changes > len(changes)


class IntegrityState(str, Enum):
    VERIFIED = "verified"
    BLOCKED = "blocked"


class FreshnessState(str, Enum):
    CURRENT = "current"
    STALE = "stale"


class AvailabilityMode(str, Enum):
    NORMAL_THREE = "normal_three"
    DEGRADED_TWO = "degraded_two"
    MANUAL_SINGLE = "manual_single"
    MANUAL_ZERO = "manual_zero"


class ApprovalLane(str, Enum):
    NORMAL_GREEN = "normal_green"
    NORMAL_AMBER = "normal_amber"
    DEGRADED_TWO = "degraded_two"
    DEGRADED_COUNCIL = "degraded_council"
    RED = "red"
    ZERO_HISTORY = "zero_history"
    MANUAL_SINGLE = "manual_single"
    MANUAL_COMPLETE = "manual_complete"
    MANUAL_ZERO = "manual_zero"
    STALE = "stale"
    INTEGRITY_BLOCKED = "integrity_blocked"


class DecisionState(str, Enum):
    UNDECIDED = "undecided"
    ACCEPTED = "accepted"
    OVERRIDE_SUBMITTED = "override-submitted"
    EXCLUDED = "excluded"
    DEFERRED = "deferred"
    BLOCKED = "blocked"


class ApprovalManualMode(str, Enum):
    NONE = "none"
    EXACT_SINGLE_SURVIVOR = "exact_single_survivor"
    COMPLETE_EXPECTED_TIME = "complete_expected_time"


class ApprovalDecisionAction(str, Enum):
    ORDINARY_BATCH_ACCEPT = "ordinary_batch_accept"
    DEGRADED_BATCH_ACCEPT = "degraded_batch_accept"
    INDIVIDUAL_ACCEPT = "individual_accept"
    OVERRIDE_SUBMITTED = "override_submitted"
    EXCLUDE = "exclude"
    DEFER = "defer"


class QueueEmptyReason(str, Enum):
    NO_SCHEDULED_FIELDS = "no_scheduled_fields"
    STILL_PREPARING = "still_preparing"
    NO_BATCH_ELIGIBLE_FIELDS = "no_batch_eligible_fields"
    ALL_ISSUED = "all_issued"
    ALL_BLOCKED = "all_blocked"
    TOURNAMENT_CLOSED = "tournament_closed"


@dataclass(frozen=True, slots=True)
class ApprovalDecisionSelection:
    field_id: str
    receipt_id: str
    receipt_revision: int
    upstream_field_revision: int
    row_digest: str
    call_order: int

    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        require_identifier(self.receipt_id, expected_namespace="receipt")
        if (
            isinstance(self.receipt_revision, bool)
            or not isinstance(self.receipt_revision, int)
            or self.receipt_revision <= 0
            or isinstance(self.upstream_field_revision, bool)
            or not isinstance(self.upstream_field_revision, int)
            or self.upstream_field_revision <= 0
        ):
            raise ApprovalError("decision field revision must be positive")
        _digest(self.row_digest)
        if (
            isinstance(self.call_order, bool)
            or not isinstance(self.call_order, int)
            or self.call_order < 0
        ):
            raise ApprovalError("decision call order must be nonnegative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "receipt_id": self.receipt_id,
            "receipt_revision": self.receipt_revision,
            "upstream_field_revision": self.upstream_field_revision,
            "row_digest": self.row_digest,
            "call_order": self.call_order,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ApprovalDecisionSelection:
        if set(value) != {
            "field_id",
            "receipt_id",
            "receipt_revision",
            "upstream_field_revision",
            "row_digest",
            "call_order",
        }:
            raise ApprovalError("decision selection fields differ")
        return cls(
            value["field_id"],
            value["receipt_id"],
            value["receipt_revision"],
            value["upstream_field_revision"],
            value["row_digest"],
            value["call_order"],
        )


def _selection_order(
    item: ApprovalDecisionSelection,
) -> tuple[int, str, str]:
    return item.call_order, item.field_id, item.receipt_id


@dataclass(frozen=True, slots=True)
class ApprovalDecisionCommand:
    caller_namespace: str
    request_identity: str
    tournament_id: str
    snapshot_id: str
    action: ApprovalDecisionAction
    selected: tuple[ApprovalDecisionSelection, ...]
    excluded: tuple[ApprovalDecisionSelection, ...]
    actor_id: str
    actor_metadata_json: str
    actor_metadata_digest: str
    reason_code: str
    superseded_receipt_id: str | None
    submitted_at: str
    command_digest: str

    def __post_init__(self) -> None:
        if not _bounded_token(self.caller_namespace, maximum=64):
            raise ApprovalError("decision caller namespace must be canonical")
        require_idempotency_key(self.request_identity)
        require_identifier(self.tournament_id, expected_namespace="tournament")
        if not _valid_snapshot_id(self.snapshot_id):
            raise ApprovalError("decision approval snapshot is invalid")
        if not isinstance(self.action, ApprovalDecisionAction):
            raise ApprovalError("decision action must use the closed vocabulary")
        if not isinstance(self.selected, tuple) or not isinstance(self.excluded, tuple):
            raise ApprovalError("decision selections must be immutable tuples")
        selections = (*self.selected, *self.excluded)
        if (
            not selections
            or not all(isinstance(item, ApprovalDecisionSelection) for item in selections)
            or len(selections) > 100
        ):
            raise ApprovalError("decision requires a typed selection")
        if self.selected != tuple(
            sorted(self.selected, key=_selection_order)
        ) or self.excluded != tuple(sorted(self.excluded, key=_selection_order)):
            raise ApprovalError("decision selections must be canonically sorted")
        selected_keys = tuple((item.field_id, item.receipt_id) for item in self.selected)
        excluded_keys = tuple((item.field_id, item.receipt_id) for item in self.excluded)
        if len(selected_keys) != len(set(selected_keys)) or len(excluded_keys) != len(
            set(excluded_keys)
        ):
            raise ApprovalError("decision selection contains a duplicate")
        if set(selected_keys) & set(excluded_keys):
            raise ApprovalError("decision selected and excluded material overlap")
        if self.action in {
            ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT,
            ApprovalDecisionAction.DEGRADED_BATCH_ACCEPT,
        }:
            if not self.selected:
                raise ApprovalError("batch acceptance requires a selected receipt")
        elif len(self.selected) != 1 or self.excluded:
            raise ApprovalError("individual decision requires exactly one selection")
        require_identifier(self.actor_id, expected_namespace="actor")
        if not isinstance(self.actor_metadata_json, str):
            raise ApprovalError("decision actor metadata must be canonical JSON")
        try:
            actor_metadata = json.loads(self.actor_metadata_json)
            encoded_metadata = canonical_bytes(actor_metadata, max_bytes=4_096)
        except Exception as exc:
            raise ApprovalError("decision actor metadata is invalid or oversized") from exc
        if encoded_metadata.decode() != self.actor_metadata_json or not isinstance(
            actor_metadata, dict
        ):
            raise ApprovalError("decision actor metadata must be a canonical object")
        _digest(self.actor_metadata_digest)
        if self.actor_metadata_digest != canonical_digest(actor_metadata):
            raise ApprovalError("decision actor metadata digest differs")
        if not _bounded_token(self.reason_code, maximum=128):
            raise ApprovalError("decision reason code must be canonical and bounded")
        if self.action is ApprovalDecisionAction.OVERRIDE_SUBMITTED:
            if self.superseded_receipt_id is None:
                raise ApprovalError("override decision requires its superseded receipt")
            require_identifier(self.superseded_receipt_id, expected_namespace="receipt")
            if self.superseded_receipt_id == self.selected[0].receipt_id:
                raise ApprovalError("override must bind a distinct predecessor receipt")
        elif self.superseded_receipt_id is not None:
            raise ApprovalError("only override decisions bind a superseded receipt")
        require_utc_milliseconds(self.submitted_at)
        _digest(self.command_digest)
        if self.command_digest != canonical_digest(self.content_value()):
            raise ApprovalError("decision command digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        caller_namespace: str,
        request_identity: str,
        tournament_id: str,
        snapshot_id: str,
        action: ApprovalDecisionAction,
        selected: tuple[ApprovalDecisionSelection, ...],
        excluded: tuple[ApprovalDecisionSelection, ...],
        actor_id: str,
        actor_metadata: Mapping[str, Any],
        reason_code: str,
        superseded_receipt_id: str | None = None,
        submitted_at: str,
    ) -> ApprovalDecisionCommand:
        if not isinstance(actor_metadata, Mapping):
            raise ApprovalError("decision actor metadata must be a mapping")
        metadata_bytes = canonical_bytes(actor_metadata, max_bytes=4_096)
        ordered_selected = tuple(sorted(selected, key=_selection_order))
        ordered_excluded = tuple(sorted(excluded, key=_selection_order))
        values = {
            "caller_namespace": caller_namespace,
            "request_identity": request_identity,
            "tournament_id": tournament_id,
            "snapshot_id": snapshot_id,
            "action": action,
            "selected": ordered_selected,
            "excluded": ordered_excluded,
            "actor_id": actor_id,
            "actor_metadata_json": metadata_bytes.decode(),
            "actor_metadata_digest": canonical_digest(actor_metadata),
            "reason_code": reason_code,
            "superseded_receipt_id": superseded_receipt_id,
            "submitted_at": submitted_at,
        }
        content = {
            "schema_version": "strathmark-v3-approval-decision-command-v1",
            "caller_namespace": caller_namespace,
            "request_identity": request_identity,
            "tournament_id": tournament_id,
            "snapshot_id": snapshot_id,
            "action": (action.value if isinstance(action, ApprovalDecisionAction) else action),
            "selected": [item.to_dict() for item in ordered_selected],
            "excluded": [item.to_dict() for item in ordered_excluded],
            "actor_id": actor_id,
            "actor_metadata": actor_metadata,
            "actor_metadata_digest": values["actor_metadata_digest"],
            "reason_code": reason_code,
            "superseded_receipt_id": superseded_receipt_id,
            "submitted_at": submitted_at,
        }
        return cls(**values, command_digest=canonical_digest(content))

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-approval-decision-command-v1",
            "caller_namespace": self.caller_namespace,
            "request_identity": self.request_identity,
            "tournament_id": self.tournament_id,
            "snapshot_id": self.snapshot_id,
            "action": self.action.value,
            "selected": [item.to_dict() for item in self.selected],
            "excluded": [item.to_dict() for item in self.excluded],
            "actor_id": self.actor_id,
            "actor_metadata": json.loads(self.actor_metadata_json),
            "actor_metadata_digest": self.actor_metadata_digest,
            "reason_code": self.reason_code,
            "superseded_receipt_id": self.superseded_receipt_id,
            "submitted_at": self.submitted_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "command_digest": self.command_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ApprovalDecisionCommand:
        expected = {
            "schema_version",
            "caller_namespace",
            "request_identity",
            "tournament_id",
            "snapshot_id",
            "action",
            "selected",
            "excluded",
            "actor_id",
            "actor_metadata",
            "actor_metadata_digest",
            "reason_code",
            "superseded_receipt_id",
            "submitted_at",
            "command_digest",
        }
        if set(value) != expected or value.get("schema_version") != (
            "strathmark-v3-approval-decision-command-v1"
        ):
            raise ApprovalError("decision command fields or schema differ")
        try:
            selected = tuple(
                ApprovalDecisionSelection.from_dict(item) for item in value["selected"]
            )
            excluded = tuple(
                ApprovalDecisionSelection.from_dict(item) for item in value["excluded"]
            )
            metadata = value["actor_metadata"]
            metadata_bytes = canonical_bytes(metadata, max_bytes=4_096)
            return cls(
                value["caller_namespace"],
                value["request_identity"],
                value["tournament_id"],
                value["snapshot_id"],
                ApprovalDecisionAction(value["action"]),
                selected,
                excluded,
                value["actor_id"],
                metadata_bytes.decode(),
                value["actor_metadata_digest"],
                value["reason_code"],
                value["superseded_receipt_id"],
                value["submitted_at"],
                value["command_digest"],
            )
        except (TypeError, ValueError) as exc:
            raise ApprovalError("decision command encoded values are invalid") from exc


@dataclass(frozen=True, slots=True)
class ApprovalDecisionReceipt:
    caller_namespace: str
    request_identity: str
    tournament_id: str
    command_digest: str
    snapshot_id: str
    action: ApprovalDecisionAction
    decisions: tuple[tuple[str, DecisionState], ...]
    decided_at: str
    decision_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.caller_namespace, str) or not self.caller_namespace:
            raise ApprovalError("decision receipt caller namespace is invalid")
        require_idempotency_key(self.request_identity)
        require_identifier(self.tournament_id, expected_namespace="tournament")
        _digest(self.command_digest)
        if not _valid_snapshot_id(self.snapshot_id):
            raise ApprovalError("decision receipt snapshot is invalid")
        if not isinstance(self.action, ApprovalDecisionAction):
            raise ApprovalError("decision receipt action is invalid")
        if not isinstance(self.decisions, tuple) or not self.decisions:
            raise ApprovalError("decision receipt requires immutable results")
        if self.decisions != _decision_results_for_action(self.action, self.decisions):
            raise ApprovalError("decision receipt states differ from its action")
        seen: set[str] = set()
        for receipt_id, state in self.decisions:
            require_identifier(receipt_id, expected_namespace="receipt")
            if receipt_id in seen or state not in {
                DecisionState.ACCEPTED,
                DecisionState.OVERRIDE_SUBMITTED,
                DecisionState.EXCLUDED,
                DecisionState.DEFERRED,
            }:
                raise ApprovalError("decision receipt result is duplicate or invalid")
            seen.add(receipt_id)
        require_utc_milliseconds(self.decided_at)
        _digest(self.decision_digest)
        if self.decision_digest != canonical_digest(self.content_value()):
            raise ApprovalError("decision receipt digest mismatch")

    @classmethod
    def create(
        cls,
        command: ApprovalDecisionCommand,
    ) -> ApprovalDecisionReceipt:
        if not isinstance(command, ApprovalDecisionCommand):
            raise ApprovalError("decision receipt requires a typed command")
        values = {
            "caller_namespace": command.caller_namespace,
            "request_identity": command.request_identity,
            "tournament_id": command.tournament_id,
            "command_digest": command.command_digest,
            "snapshot_id": command.snapshot_id,
            "action": command.action,
            "decisions": _command_decision_results(command),
            "decided_at": command.submitted_at,
        }
        content = _decision_receipt_value(values)
        return cls(**values, decision_digest=canonical_digest(content))

    def content_value(self) -> dict[str, Any]:
        return _decision_receipt_value(
            {
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "decision_digest"
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "decision_digest": self.decision_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ApprovalDecisionReceipt:
        expected = {
            "schema_version",
            "caller_namespace",
            "request_identity",
            "tournament_id",
            "command_digest",
            "snapshot_id",
            "action",
            "decisions",
            "decided_at",
            "decision_digest",
        }
        if set(value) != expected or value.get("schema_version") != (
            "strathmark-v3-approval-decision-receipt-v1"
        ):
            raise ApprovalError("decision receipt fields or schema differ")
        try:
            return cls(
                value["caller_namespace"],
                value["request_identity"],
                value["tournament_id"],
                value["command_digest"],
                value["snapshot_id"],
                ApprovalDecisionAction(value["action"]),
                tuple((item[0], DecisionState(item[1])) for item in value["decisions"]),
                value["decided_at"],
                value["decision_digest"],
            )
        except (TypeError, ValueError, IndexError) as exc:
            raise ApprovalError("decision receipt encoded values are invalid") from exc


def _decision_receipt_value(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-approval-decision-receipt-v1",
        "caller_namespace": values["caller_namespace"],
        "request_identity": values["request_identity"],
        "tournament_id": values["tournament_id"],
        "command_digest": values["command_digest"],
        "snapshot_id": values["snapshot_id"],
        "action": values["action"].value,
        "decisions": [[receipt_id, state.value] for receipt_id, state in values["decisions"]],
        "decided_at": values["decided_at"],
    }


def _command_decision_results(
    command: ApprovalDecisionCommand,
) -> tuple[tuple[str, DecisionState], ...]:
    selected_state = {
        ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT: DecisionState.ACCEPTED,
        ApprovalDecisionAction.DEGRADED_BATCH_ACCEPT: DecisionState.ACCEPTED,
        ApprovalDecisionAction.INDIVIDUAL_ACCEPT: DecisionState.ACCEPTED,
        ApprovalDecisionAction.OVERRIDE_SUBMITTED: DecisionState.OVERRIDE_SUBMITTED,
        ApprovalDecisionAction.EXCLUDE: DecisionState.EXCLUDED,
        ApprovalDecisionAction.DEFER: DecisionState.DEFERRED,
    }[command.action]
    return tuple((item.receipt_id, selected_state) for item in command.selected) + tuple(
        (item.receipt_id, DecisionState.EXCLUDED) for item in command.excluded
    )


def _decision_results_for_action(
    action: ApprovalDecisionAction,
    decisions: tuple[tuple[str, DecisionState], ...],
) -> tuple[tuple[str, DecisionState], ...]:
    allowed = {
        ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT: {
            DecisionState.ACCEPTED,
            DecisionState.EXCLUDED,
        },
        ApprovalDecisionAction.DEGRADED_BATCH_ACCEPT: {
            DecisionState.ACCEPTED,
            DecisionState.EXCLUDED,
        },
        ApprovalDecisionAction.INDIVIDUAL_ACCEPT: {DecisionState.ACCEPTED},
        ApprovalDecisionAction.OVERRIDE_SUBMITTED: {DecisionState.OVERRIDE_SUBMITTED},
        ApprovalDecisionAction.EXCLUDE: {DecisionState.EXCLUDED},
        ApprovalDecisionAction.DEFER: {DecisionState.DEFERRED},
    }[action]
    if any(state not in allowed for _receipt, state in decisions):
        return ()
    if action in {
        ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT,
        ApprovalDecisionAction.DEGRADED_BATCH_ACCEPT,
    }:
        accepted = tuple(item for item in decisions if item[1] is DecisionState.ACCEPTED)
        excluded = tuple(item for item in decisions if item[1] is DecisionState.EXCLUDED)
        if not accepted or decisions != (*accepted, *excluded):
            return ()
    elif len(decisions) != 1:
        return ()
    return decisions


def _bounded_token(value: object, *, maximum: int) -> bool:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_.:"
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(character in allowed for character in value)
    )


def _valid_snapshot_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("approval_snapshot:")
        and len(value) == len("approval_snapshot:") + 64
        and all(character in "0123456789abcdef" for character in value.split(":", 1)[1])
    )


@dataclass(frozen=True, slots=True)
class ApprovalFacts:
    integrity: IntegrityState
    freshness: FreshnessState
    availability: AvailabilityMode
    consequence: ConsequenceColor
    zero_history: bool
    council_degraded: bool
    manual_construction: bool
    reason_codes: tuple[str, ...]
    availability_counts: tuple[tuple[str, int], ...] = ()
    manual_mode: ApprovalManualMode = ApprovalManualMode.NONE
    flagged_competitor_ids: tuple[str, ...] = ()
    flag_reason_tokens: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, expected)
            for value, expected in (
                (self.integrity, IntegrityState),
                (self.freshness, FreshnessState),
                (self.availability, AvailabilityMode),
                (self.consequence, ConsequenceColor),
                (self.manual_mode, ApprovalManualMode),
            )
        ):
            raise ApprovalError("approval facts require closed typed states")
        if not all(
            isinstance(value, bool)
            for value in (
                self.zero_history,
                self.council_degraded,
                self.manual_construction,
            )
        ):
            raise ApprovalError("approval fact flags must be explicit booleans")
        if not isinstance(self.reason_codes, tuple) or self.reason_codes != tuple(
            sorted(set(self.reason_codes))
        ):
            raise ApprovalError("approval reasons must be unique and sorted")
        if not isinstance(self.availability_counts, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or isinstance(item[1], bool)
            or not isinstance(item[1], int)
            or item[1] not in {0, 1, 2, 3}
            for item in self.availability_counts
        ):
            raise ApprovalError("approval availability counts must be typed")
        if self.availability_counts and self.availability_counts != tuple(
            sorted(self.availability_counts)
        ):
            raise ApprovalError("approval availability counts must be canonical")
        if not isinstance(
            self.flagged_competitor_ids, tuple
        ) or self.flagged_competitor_ids != tuple(sorted(set(self.flagged_competitor_ids))):
            raise ApprovalError("flagged competitor identities must be canonical")
        for competitor_id in self.flagged_competitor_ids:
            require_identifier(competitor_id, expected_namespace="competitor")
        if (
            not isinstance(self.flag_reason_tokens, tuple)
            or self.flag_reason_tokens
            != tuple(sorted(self.flag_reason_tokens, key=lambda item: item[0]))
            or tuple(item[0] for item in self.flag_reason_tokens) != self.flagged_competitor_ids
        ):
            raise ApprovalError("flag reason rows must match flagged competitors")
        for competitor_id, tokens in self.flag_reason_tokens:
            require_identifier(competitor_id, expected_namespace="competitor")
            if (
                not isinstance(tokens, tuple)
                or tokens != tuple(sorted(set(tokens)))
                or not tokens
                or not all(_bounded_token(token, maximum=96) for token in tokens)
            ):
                raise ApprovalError("flag reason tokens must be bounded and canonical")
        if self.manual_construction is not (self.manual_mode is not ApprovalManualMode.NONE):
            raise ApprovalError("manual construction must agree with availability")
        if (
            self.availability
            in {
                AvailabilityMode.MANUAL_SINGLE,
                AvailabilityMode.MANUAL_ZERO,
            }
            and self.manual_mode is ApprovalManualMode.NONE
        ):
            raise ApprovalError("manual construction must agree with availability")


def derive_approval_lane(facts: ApprovalFacts) -> ApprovalLane:
    """Apply KTD10 precedence and never invent an exceptional default."""

    if facts.integrity is IntegrityState.BLOCKED:
        return ApprovalLane.INTEGRITY_BLOCKED
    if facts.freshness is FreshnessState.STALE:
        return ApprovalLane.STALE
    if (
        facts.manual_mode is ApprovalManualMode.COMPLETE_EXPECTED_TIME
        and facts.availability is not AvailabilityMode.MANUAL_ZERO
    ):
        return ApprovalLane.MANUAL_COMPLETE
    if facts.availability is AvailabilityMode.MANUAL_ZERO:
        return ApprovalLane.MANUAL_ZERO
    if facts.availability is AvailabilityMode.MANUAL_SINGLE:
        return ApprovalLane.MANUAL_SINGLE
    if facts.zero_history:
        return ApprovalLane.ZERO_HISTORY
    if facts.consequence is ConsequenceColor.RED:
        return ApprovalLane.RED
    if facts.availability is AvailabilityMode.DEGRADED_TWO:
        return ApprovalLane.DEGRADED_TWO
    if facts.council_degraded:
        return ApprovalLane.DEGRADED_COUNCIL
    if facts.consequence is ConsequenceColor.AMBER:
        return ApprovalLane.NORMAL_AMBER
    return ApprovalLane.NORMAL_GREEN


def derive_receipt_approval_facts(
    receipt: Any, *, u5_current: bool, integrity_verified: bool
) -> ApprovalFacts:
    """Derive KTD10 facts from closed receipt sections, never caller claims."""

    from strathmark.v3.contracts.commands import InlinePayload
    from strathmark.v3.contracts.receipts import FieldReceipt, ReceiptSectionKind

    if not isinstance(receipt, FieldReceipt):
        raise ApprovalError("approval facts require a typed field receipt")
    if not isinstance(u5_current, bool) or not isinstance(integrity_verified, bool):
        raise ApprovalError("approval fact authority flags must be explicit")
    reasons = set(receipt.warning_codes)
    if not integrity_verified:
        reasons.add("receipt_integrity_unverified")
        return ApprovalFacts(
            IntegrityState.BLOCKED,
            FreshnessState.CURRENT if u5_current else FreshnessState.STALE,
            AvailabilityMode.NORMAL_THREE,
            ConsequenceColor.RED,
            "zero_history" in reasons,
            False,
            False,
            tuple(sorted(reasons)),
        )
    try:
        sections = {item.kind: item for item in receipt.sections}
        component_payload = sections[ReceiptSectionKind.COMPONENT_OUTPUTS].payload
        pool_payload = sections[ReceiptSectionKind.POOLED_DISTRIBUTION].payload
        member_payload = sections[ReceiptSectionKind.MEMBER_OUTPUTS].payload
        validation_payload = sections[ReceiptSectionKind.VALIDATIONS].payload
        disagreement_payload = sections[ReceiptSectionKind.DISAGREEMENT].payload
        if not all(
            isinstance(item, InlinePayload)
            for item in (
                component_payload,
                pool_payload,
                member_payload,
                validation_payload,
                disagreement_payload,
            )
        ):
            raise ApprovalError("approval receipt sections must be inline and closed")
        components = component_payload.to_value()
        pools = pool_payload.to_value()
        members = member_payload.to_value()
        validations = validation_payload.to_value()
        disagreement = disagreement_payload.to_value()
        if (
            components.get("schema_version") != "strathmark-v3-receipt-components-v1"
            or pools.get("schema_version") != "strathmark-v3-receipt-prediction-distributions-v1"
            or members.get("schema_version") != "strathmark-v3-receipt-members-v1"
            or validations.get("schema_version") != "strathmark-v3-receipt-validations-v1"
            or disagreement.get("schema_version") != "strathmark-v3-receipt-disagreement-v1"
        ):
            raise ApprovalError("approval receipt section schema is unsupported")
        component_rows = components.get("competitors")
        basis_rows = pools.get("bases")
        if (
            not isinstance(component_rows, list)
            or not component_rows
            or not isinstance(basis_rows, list)
            or len(basis_rows) != len(component_rows)
        ):
            raise ApprovalError("approval receipt has no typed prediction availability")
        counts: list[int] = []
        availability_counts: list[tuple[str, int]] = []
        component_ids: list[str] = []
        for row in component_rows:
            if (
                not isinstance(row, Mapping)
                or not isinstance(row.get("competitor_id"), str)
                or not isinstance(row.get("components"), list)
            ):
                raise ApprovalError("approval component availability row is malformed")
            competitor_id = str(
                require_identifier(row["competitor_id"], expected_namespace="competitor")
            )
            forecasts = row["components"]
            assessor_states: dict[str, str] = {}
            for forecast in forecasts:
                if (
                    not isinstance(forecast, Mapping)
                    or forecast.get("assessor") not in {"formula", "ml", "llm_council"}
                    or forecast.get("state") not in {"committed", "abstained"}
                    or forecast["assessor"] in assessor_states
                ):
                    raise ApprovalError("approval component availability is inconsistent")
                assessor_states[str(forecast["assessor"])] = str(forecast["state"])
            if set(assessor_states) != {"formula", "ml", "llm_council"}:
                raise ApprovalError("approval component roster is incomplete")
            sources = tuple(
                source
                for source in ("formula", "ml", "llm_council")
                if assessor_states[source] == "committed"
            )
            count = len(sources)
            component_ids.append(competitor_id)
            counts.append(count)
            availability_counts.append((competitor_id, count))
        if tuple(component_ids) != tuple(str(item) for item in receipt.ordered_competitor_ids):
            raise ApprovalError("approval component roster differs from receipt")
        try:
            basis_ids = tuple(
                str(require_identifier(row[0], expected_namespace="competitor"))
                for row in basis_rows
                if isinstance(row, list) and len(row) == 2 and isinstance(row[1], Mapping)
            )
        except (TypeError, ValueError) as exc:
            raise ApprovalError("approval prediction bases are malformed") from exc
        if basis_ids != tuple(component_ids) or len(basis_ids) != len(basis_rows):
            raise ApprovalError("approval prediction bases differ from receipt")
        available_count = min(counts)
        availability = {
            3: AvailabilityMode.NORMAL_THREE,
            2: AvailabilityMode.DEGRADED_TWO,
            1: AvailabilityMode.MANUAL_SINGLE,
            0: AvailabilityMode.MANUAL_ZERO,
        }[available_count]
        manual_candidate = validations.get("manual_authority")
        manual_mode = ApprovalManualMode.NONE
        if isinstance(manual_candidate, Mapping):
            manual_mode = ApprovalManualMode(manual_candidate.get("mode"))
        flagged_competitor_ids, flag_reason_tokens = _validation_flags(validations, receipt)
        audit = members.get("council_audit")
        council_degraded = False
        if audit is not None:
            if not isinstance(audit, Mapping) or not isinstance(audit.get("members"), list):
                raise ApprovalError("approval council audit is malformed")
            outcomes = audit["members"]
            if len(outcomes) != 3 or any(
                not isinstance(item, Mapping)
                or item.get("status") not in {"valid", "failed", "invalid"}
                for item in outcomes
            ):
                raise ApprovalError("approval council audit outcomes are invalid")
            valid_count = sum(item["status"] == "valid" for item in outcomes)
            council_degraded = valid_count == 2
            if valid_count < 2 and available_count == 3:
                raise ApprovalError("available council lacks two valid members")
        if available_count < 2 or manual_mode is not ApprovalManualMode.NONE:
            if manual_mode is ApprovalManualMode.NONE:
                raise ApprovalError("manual availability lacks construction authority")
            _verify_manual_authority(
                manual_candidate,
                validations.get("field_revision_digest"),
                receipt,
                manual_mode,
            )
            consequence = ConsequenceColor.RED
        else:
            decision = disagreement.get("decision")
            operational = disagreement.get("operational_receipt")
            if decision is None or operational is None:
                reasons.add("operational_disagreement_unavailable")
                return ApprovalFacts(
                    IntegrityState.BLOCKED,
                    FreshnessState.CURRENT if u5_current else FreshnessState.STALE,
                    availability,
                    ConsequenceColor.RED,
                    "zero_history" in reasons,
                    council_degraded,
                    False,
                    tuple(sorted(reasons)),
                    tuple(sorted(availability_counts)),
                    ApprovalManualMode.NONE,
                )
            consequence = _verified_operational_color(
                decision=decision,
                operational=operational,
                validations=validations,
            )
    except (ApprovalError, KeyError, TypeError, ValueError):
        reasons.add("receipt_typed_facts_invalid")
        return ApprovalFacts(
            IntegrityState.BLOCKED,
            FreshnessState.CURRENT if u5_current else FreshnessState.STALE,
            AvailabilityMode.NORMAL_THREE,
            ConsequenceColor.RED,
            "zero_history" in reasons,
            False,
            False,
            tuple(sorted(reasons)),
        )
    return ApprovalFacts(
        IntegrityState.VERIFIED,
        FreshnessState.CURRENT if u5_current else FreshnessState.STALE,
        availability,
        consequence,
        "zero_history" in reasons,
        council_degraded,
        manual_mode is not ApprovalManualMode.NONE,
        tuple(sorted(reasons)),
        tuple(sorted(availability_counts)),
        manual_mode,
        flagged_competitor_ids,
        flag_reason_tokens,
    )


def _verified_operational_color(
    *, decision: object, operational: object, validations: Mapping[str, Any]
) -> ConsequenceColor:
    if not isinstance(decision, Mapping) or not isinstance(operational, Mapping):
        raise ApprovalError("operational disagreement authority is malformed")
    if (
        decision.get("schema_version") != "strathmark-v3-disagreement-decision-v1"
        or decision.get("operational_status") != "pending_u14_verifier"
        or decision.get("manual_review_required") is not True
    ):
        raise ApprovalError("raw disagreement decision must remain pending U14")
    operational_fields = {
        "schema_version",
        "field_revision_digest",
        "decision_digest",
        "color",
        "policy_digest",
        "pooled_optimizer_verification_digest",
        "component_optimizer_verification_digests",
        "component_joint_draw_digests",
        "policy_manifest_digest",
        "council_manifest_digest",
        "verification_status",
        "receipt_digest",
    }
    if (
        set(operational) != operational_fields
        or operational.get("schema_version") != "strathmark-v3-operational-disagreement-receipt-v1"
        or operational.get("verification_status") != "verified"
    ):
        raise ApprovalError("operational disagreement receipt is not verified")
    for value in (
        decision.get("decision_digest"),
        decision.get("policy_digest"),
        operational.get("field_revision_digest"),
        operational.get("decision_digest"),
        operational.get("policy_digest"),
        operational.get("pooled_optimizer_verification_digest"),
        operational.get("policy_manifest_digest"),
        operational.get("receipt_digest"),
        validations.get("field_revision_digest"),
        validations.get("optimizer_verification_digest"),
    ):
        _digest(value)
    try:
        decision_color = ConsequenceColor(decision.get("color"))
        operational_color = ConsequenceColor(operational.get("color"))
    except (TypeError, ValueError) as exc:
        raise ApprovalError("operational disagreement color is invalid") from exc
    component_sheets = decision.get("component_sheets")
    component_verifiers = operational.get("component_optimizer_verification_digests")
    component_draws = operational.get("component_joint_draw_digests")
    if (
        not isinstance(component_sheets, list)
        or not isinstance(component_verifiers, list)
        or not isinstance(component_draws, list)
    ):
        raise ApprovalError("operational component verification is malformed")
    try:
        decision_sources = tuple(item["source"] for item in component_sheets)
        verifier_sources = tuple(item[0] for item in component_verifiers)
        verifier_digests = tuple(item[1] for item in component_verifiers)
        draw_sources = tuple(item[0] for item in component_draws)
        draw_digests = tuple(item[1] for item in component_draws)
        decision_draw_digests = tuple(item["joint_draw_digest"] for item in component_sheets)
    except (KeyError, IndexError, TypeError) as exc:
        raise ApprovalError("operational component verification is malformed") from exc
    allowed_sources = ("formula", "ml", "llm_council")
    if (
        not 2 <= len(decision_sources) <= 3
        or decision_sources != tuple(item for item in allowed_sources if item in decision_sources)
        or len(set(decision_sources)) != len(decision_sources)
        or verifier_sources != decision_sources
        or draw_sources != decision_sources
        or draw_digests != decision_draw_digests
    ):
        raise ApprovalError("operational component sources differ")
    for digest in (*verifier_digests, *draw_digests):
        _digest(digest)
    council_manifest_digest = operational.get("council_manifest_digest")
    council_present = decision.get("council_audit") is not None
    if council_present:
        _digest(council_manifest_digest)
    elif council_manifest_digest is not None:
        raise ApprovalError("unavailable council carries manifest authority")
    content = {
        key: operational[key]
        for key in operational_fields - {"verification_status", "receipt_digest"}
    }
    if (
        decision_color is not operational_color
        or operational["decision_digest"] != decision["decision_digest"]
        or operational["policy_digest"] != decision["policy_digest"]
        or operational["field_revision_digest"] != validations["field_revision_digest"]
        or operational["pooled_optimizer_verification_digest"]
        != validations["optimizer_verification_digest"]
        or operational["receipt_digest"] != canonical_digest(content)
    ):
        raise ApprovalError("operational disagreement authority differs")
    return operational_color


def _verify_manual_authority(
    authority: object,
    field_revision_digest: object,
    receipt: Any,
    manual_mode: ApprovalManualMode,
) -> None:
    if not isinstance(authority, Mapping):
        raise ApprovalError("manual construction authority is missing")
    if manual_mode is ApprovalManualMode.NONE:
        raise ApprovalError("manual construction mode is missing")
    expected_mode = manual_mode.value
    expected_reason = {
        ApprovalManualMode.EXACT_SINGLE_SURVIVOR: "judge_single_survivor_acceptance",
        ApprovalManualMode.COMPLETE_EXPECTED_TIME: "judge_complete_expected_time_construction",
    }[manual_mode]
    expected_fields = {
        "schema_version",
        "mode",
        "field_revision_digest",
        "estimates",
        "actor_id",
        "reason_code",
        "scope",
        "created_at",
        "authority_digest",
    }
    if (
        set(authority) != expected_fields
        or authority.get("schema_version") != "strathmark-v3-manual-field-authority-v1"
        or authority.get("mode") != expected_mode
        or authority.get("reason_code") != expected_reason
        or authority.get("scope") != "upcoming_race"
        or authority.get("field_revision_digest") != field_revision_digest
    ):
        raise ApprovalError("manual construction authority differs")
    _digest(field_revision_digest)
    _digest(authority.get("authority_digest"))
    require_identifier(authority.get("actor_id"), expected_namespace="actor")
    require_utc_milliseconds(authority.get("created_at"))
    estimates = authority.get("estimates")
    if not isinstance(estimates, list):
        raise ApprovalError("manual construction estimates are malformed")
    identities = tuple(
        item.get("competitor_id") if isinstance(item, Mapping) else None for item in estimates
    )
    expected_identities = tuple(sorted(str(item) for item in receipt.ordered_competitor_ids))
    if identities != expected_identities:
        raise ApprovalError("manual construction roster differs")
    content = {key: authority[key] for key in expected_fields - {"authority_digest"}}
    if authority["authority_digest"] != canonical_digest(content):
        raise ApprovalError("manual construction authority digest differs")


def _flagged_competitors(receipt: Any, facts: ApprovalFacts) -> tuple[str, ...]:
    """Derive causal scan identities independently from integer mark changes."""

    roster = tuple(str(item) for item in receipt.ordered_competitor_ids)
    if facts.flagged_competitor_ids:
        return tuple(
            competitor for competitor in roster if competitor in facts.flagged_competitor_ids
        )
    flagged = {competitor for competitor, count in facts.availability_counts if count < 3}
    if (
        facts.consequence is ConsequenceColor.RED
        or facts.zero_history
        or facts.council_degraded
        or facts.manual_construction
    ):
        flagged.update(roster)
    return tuple(item for item in roster if item in flagged)


def _validation_flags(
    validations: Mapping[str, Any], receipt: Any
) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    flagged = validations.get("flagged_competitor_ids")
    reasons = validations.get("flag_reason_tokens")
    if not isinstance(flagged, list) or not isinstance(reasons, list):
        raise ApprovalError("approval validation causal flags are unavailable")
    roster = {str(item) for item in receipt.ordered_competitor_ids}
    flagged_ids = tuple(flagged)
    if any(
        not isinstance(item, str) or item not in roster for item in flagged_ids
    ) or flagged_ids != tuple(sorted(set(flagged_ids))):
        raise ApprovalError("approval validation flagged competitors differ")
    rows: list[tuple[str, tuple[str, ...]]] = []
    for item in reasons:
        if not isinstance(item, list) or len(item) != 2:
            raise ApprovalError("approval validation flag reason row is malformed")
        competitor_id, tokens = item
        if not isinstance(competitor_id, str) or not isinstance(tokens, list):
            raise ApprovalError("approval validation flag reason row is malformed")
        normalized_tokens = tuple(tokens)
        if (
            competitor_id not in roster
            or normalized_tokens != tuple(sorted(set(normalized_tokens)))
            or not normalized_tokens
            or not all(_bounded_token(token, maximum=96) for token in normalized_tokens)
        ):
            raise ApprovalError("approval validation flag reasons differ")
        rows.append((competitor_id, normalized_tokens))
    normalized_rows = tuple(rows)
    if (
        normalized_rows != tuple(sorted(normalized_rows, key=lambda row: row[0]))
        or tuple(row[0] for row in normalized_rows) != flagged_ids
    ):
        raise ApprovalError("approval validation flags and reasons differ")
    return flagged_ids, normalized_rows


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    field_id: str
    receipt_revision: int
    upstream_field_revision: int
    receipt_id: str
    call_order: int
    deadline_at: str
    target_context_digest: str
    receipt_content_digest: str
    prior_receipt_id: str | None
    prior_receipt_content_digest: str | None
    component_outputs_digest: str
    consequence_detail_digest: str
    validation_digest: str
    causal_rule_codes: tuple[str, ...]
    proposed_marks: tuple[tuple[str, int], ...]
    changed_marks: tuple[tuple[str, int | None, int | None], ...]
    affected_competitors: tuple[str, ...]
    facts: ApprovalFacts
    lane: ApprovalLane
    ordinary_batch_eligible: bool
    degraded_batch_eligible: bool
    decision_state: DecisionState
    row_digest: str
    _authority: object = dataclass_field(repr=False, compare=False)

    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        require_identifier(self.receipt_id, expected_namespace="receipt")
        if (
            isinstance(self.receipt_revision, bool)
            or not isinstance(self.receipt_revision, int)
            or self.receipt_revision <= 0
            or isinstance(self.upstream_field_revision, bool)
            or not isinstance(self.upstream_field_revision, int)
            or self.upstream_field_revision <= 0
            or isinstance(self.call_order, bool)
            or not isinstance(self.call_order, int)
            or self.call_order < 0
        ):
            raise ApprovalError("approval row revision/order is invalid")
        require_utc_milliseconds(self.deadline_at)
        _digest(self.target_context_digest)
        for value in (
            self.receipt_content_digest,
            self.component_outputs_digest,
            self.consequence_detail_digest,
            self.validation_digest,
        ):
            _digest(value)
        if (self.prior_receipt_id is None) is not (self.prior_receipt_content_digest is None):
            raise ApprovalError("prior receipt identity and digest must be bound together")
        if self.prior_receipt_id is not None:
            require_identifier(self.prior_receipt_id, expected_namespace="receipt")
            _digest(self.prior_receipt_content_digest)
        if self.causal_rule_codes != tuple(sorted(set(self.causal_rule_codes))):
            raise ApprovalError("causal rule codes must be unique and sorted")
        if self._authority is not _VERIFIED_RECEIPT_AUTHORITY:
            raise ApprovalError("approval row lacks verified receipt authority")
        if not isinstance(self.facts, ApprovalFacts):
            raise ApprovalError("approval row facts must be typed")
        if self.lane is not derive_approval_lane(self.facts):
            raise ApprovalError("approval lane differs from derived precedence")
        if not isinstance(self.decision_state, DecisionState):
            raise ApprovalError("approval decision state must be typed")
        ordinary = self.decision_state is DecisionState.UNDECIDED and self.lane in {
            ApprovalLane.NORMAL_GREEN,
            ApprovalLane.NORMAL_AMBER,
        }
        degraded = self.decision_state is DecisionState.UNDECIDED and self.lane in {
            ApprovalLane.DEGRADED_TWO,
            ApprovalLane.DEGRADED_COUNCIL,
        }
        if (
            self.ordinary_batch_eligible is not ordinary
            or self.degraded_batch_eligible is not degraded
        ):
            raise ApprovalError("approval eligibility or decision state is not derived")
        if self.lane is ApprovalLane.INTEGRITY_BLOCKED and self.decision_state not in {
            DecisionState.BLOCKED,
            DecisionState.UNDECIDED,
        }:
            raise ApprovalError("integrity-blocked material cannot carry a decision")
        proposed_ids: list[str] = []
        for competitor, mark in self.proposed_marks:
            proposed_ids.append(
                str(require_identifier(competitor, expected_namespace="competitor"))
            )
            if isinstance(mark, bool) or not isinstance(mark, int) or not 3 <= mark <= 183:
                raise ApprovalError("approval proposed mark is outside 3..183")
        if len(set(proposed_ids)) != len(proposed_ids):
            raise ApprovalError("approval proposed marks cannot repeat a competitor")
        changed_ids: list[str] = []
        for competitor, before, after in self.changed_marks:
            changed_ids.append(str(require_identifier(competitor, expected_namespace="competitor")))
            if (
                before == after
                or not all(
                    item is None
                    or (isinstance(item, int) and not isinstance(item, bool) and 3 <= item <= 183)
                    for item in (before, after)
                )
                or (before is None and after is None)
            ):
                raise ApprovalError("changed marks require distinct legal before/after values")
        if len(set(changed_ids)) != len(changed_ids):
            raise ApprovalError("changed marks cannot repeat a competitor")
        affected_ids = tuple(
            str(require_identifier(item, expected_namespace="competitor"))
            for item in self.affected_competitors
        )
        if (
            affected_ids != self.affected_competitors
            or len(set(affected_ids)) != len(affected_ids)
            or any(item not in proposed_ids for item in affected_ids)
        ):
            raise ApprovalError("affected competitors must be a unique current subset")
        proposed_by_id = dict(self.proposed_marks)
        if any(
            (after is None and competitor in proposed_by_id)
            or (after is not None and proposed_by_id.get(competitor) != after)
            for competitor, _before, after in self.changed_marks
        ):
            raise ApprovalError("changed marks must end at the proposed roster marks")
        _digest(self.row_digest)
        if self.row_digest != canonical_digest(_row_value(self)):
            raise ApprovalError("approval row digest mismatch")

    @classmethod
    def from_verified_receipt(
        cls,
        *,
        store: Any,
        receipt_id: str,
        call_order: int,
        deadline_at: str,
    ) -> ApprovalRow:
        loader = getattr(store, "verified_receipt", None)
        fact_loader = getattr(store, "approval_facts", None)
        if (
            not callable(loader)
            or not callable(fact_loader)
            or getattr(store, "_approval_authority_token", None) is not _VERIFIED_RECEIPT_AUTHORITY
        ):
            raise ApprovalError("approval row requires a verified receipt store")
        receipt = loader(receipt_id)
        facts = fact_loader(receipt_id)
        from strathmark.v3.contracts.receipts import FieldReceipt

        if not isinstance(receipt, FieldReceipt) or str(receipt.receipt_id) != receipt_id:
            raise ApprovalError("approval receipt authority returned different material")
        if not isinstance(facts, ApprovalFacts):
            raise ApprovalError("approval facts must derive from verified receipt authority")
        prior = (
            None
            if receipt.supersedes_receipt_id is None
            else loader(str(receipt.supersedes_receipt_id))
        )
        if prior is not None and (
            not isinstance(prior, FieldReceipt)
            or prior.field_id != receipt.field_id
            or prior.receipt_revision + 1 != receipt.receipt_revision
        ):
            raise ApprovalError("approval predecessor receipt chain is invalid")
        return cls._from_verified_material(
            receipt=receipt,
            prior=prior,
            facts=facts,
            call_order=call_order,
            deadline_at=deadline_at,
            _authority=_VERIFIED_RECEIPT_AUTHORITY,
        )

    @classmethod
    def _from_verified_material(
        cls,
        *,
        receipt: Any,
        prior: Any | None,
        facts: ApprovalFacts,
        call_order: int,
        deadline_at: str,
        _authority: object,
    ) -> ApprovalRow:
        """Build one compact row after its caller verifies authority exactly once."""

        from strathmark.v3.contracts.receipts import FieldReceipt, ReceiptSectionKind

        if _authority is not _VERIFIED_RECEIPT_AUTHORITY:
            raise ApprovalError("approval material lacks store verification authority")
        if not isinstance(receipt, FieldReceipt) or (
            prior is not None
            and (
                not isinstance(prior, FieldReceipt)
                or prior.field_id != receipt.field_id
                or prior.receipt_revision + 1 != receipt.receipt_revision
            )
        ):
            raise ApprovalError("approval material is not a verified receipt chain")
        if not isinstance(facts, ApprovalFacts):
            raise ApprovalError("approval material facts must be typed")
        sections = {item.kind: item for item in receipt.sections}
        proposed = tuple((str(item.competitor_id), item.mark) for item in receipt.marks)
        before = (
            {} if prior is None else {str(item.competitor_id): item.mark for item in prior.marks}
        )
        after = dict(proposed)
        union = tuple(after) + tuple(competitor for competitor in before if competitor not in after)
        changed = tuple(
            (competitor, before.get(competitor), after.get(competitor))
            for competitor in union
            if before.get(competitor) != after.get(competitor)
        )
        values: dict[str, Any] = {
            "field_id": str(receipt.field_id),
            "receipt_revision": receipt.receipt_revision,
            "upstream_field_revision": receipt.upstream_field_revision,
            "receipt_id": str(receipt.receipt_id),
            "call_order": call_order,
            "deadline_at": deadline_at,
            "target_context_digest": receipt.target_context_digest,
            "receipt_content_digest": receipt.content_digest,
            "prior_receipt_id": None if prior is None else str(prior.receipt_id),
            "prior_receipt_content_digest": (None if prior is None else prior.content_digest),
            "component_outputs_digest": canonical_digest(
                sections[ReceiptSectionKind.COMPONENT_OUTPUTS].to_dict()
            ),
            "consequence_detail_digest": canonical_digest(
                sections[ReceiptSectionKind.DISAGREEMENT].to_dict()
            ),
            "validation_digest": canonical_digest(
                sections[ReceiptSectionKind.VALIDATIONS].to_dict()
            ),
            "causal_rule_codes": tuple(sorted(set((*receipt.warning_codes, *facts.reason_codes)))),
            "proposed_marks": proposed,
            "changed_marks": changed,
            "affected_competitors": _flagged_competitors(receipt, facts),
            "facts": facts,
        }
        field_id = str(require_identifier(values["field_id"], expected_namespace="field"))
        receipt_id = str(require_identifier(values["receipt_id"], expected_namespace="receipt"))
        revision = values["receipt_revision"]
        upstream_revision = values["upstream_field_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ApprovalError("approval field revision must be positive")
        if (
            isinstance(values["call_order"], bool)
            or not isinstance(values["call_order"], int)
            or values["call_order"] < 0
        ):
            raise ApprovalError("approval call order must be nonnegative")
        require_utc_milliseconds(values["deadline_at"])
        _digest(values["target_context_digest"])
        proposed = tuple(values["proposed_marks"])
        changed = tuple(values["changed_marks"])
        affected = tuple(values["affected_competitors"])
        for competitor, mark in proposed:
            require_identifier(competitor, expected_namespace="competitor")
            if isinstance(mark, bool) or not isinstance(mark, int) or not 3 <= mark <= 183:
                raise ApprovalError("approval proposed mark is outside 3..183")
        for competitor, before, after in changed:
            require_identifier(competitor, expected_namespace="competitor")
            if (
                before == after
                or not all(
                    item is None
                    or (isinstance(item, int) and not isinstance(item, bool) and 3 <= item <= 183)
                    for item in (before, after)
                )
                or (before is None and after is None)
            ):
                raise ApprovalError("changed marks require distinct legal before/after values")
        if len(set(affected)) != len(affected):
            raise ApprovalError("affected competitors must be unique")
        facts = values["facts"]
        if not isinstance(facts, ApprovalFacts):
            raise ApprovalError("approval row facts must be typed")
        lane = derive_approval_lane(facts)
        state = (
            DecisionState.BLOCKED
            if lane is ApprovalLane.INTEGRITY_BLOCKED
            else DecisionState.UNDECIDED
        )
        ordinary = state is DecisionState.UNDECIDED and lane in {
            ApprovalLane.NORMAL_GREEN,
            ApprovalLane.NORMAL_AMBER,
        }
        degraded = state is DecisionState.UNDECIDED and lane in {
            ApprovalLane.DEGRADED_TWO,
            ApprovalLane.DEGRADED_COUNCIL,
        }
        content = {
            "schema_version": "strathmark-v3-approval-row-v1",
            "field_id": field_id,
            "receipt_revision": revision,
            "upstream_field_revision": upstream_revision,
            "receipt_id": receipt_id,
            "call_order": values["call_order"],
            "deadline_at": values["deadline_at"],
            "target_context_digest": values["target_context_digest"],
            "receipt_content_digest": values["receipt_content_digest"],
            "prior_receipt_id": values["prior_receipt_id"],
            "prior_receipt_content_digest": values["prior_receipt_content_digest"],
            "component_outputs_digest": values["component_outputs_digest"],
            "consequence_detail_digest": values["consequence_detail_digest"],
            "validation_digest": values["validation_digest"],
            "causal_rule_codes": values["causal_rule_codes"],
            "proposed_marks": proposed,
            "changed_marks": changed,
            "affected_competitors": affected,
            "facts": _facts_value(facts),
            "lane": lane.value,
            "ordinary_batch_eligible": ordinary,
            "degraded_batch_eligible": degraded,
            "decision_state": state.value,
        }
        return cls(
            field_id,
            revision,
            upstream_revision,
            receipt_id,
            values["call_order"],
            values["deadline_at"],
            values["target_context_digest"],
            values["receipt_content_digest"],
            values["prior_receipt_id"],
            values["prior_receipt_content_digest"],
            values["component_outputs_digest"],
            values["consequence_detail_digest"],
            values["validation_digest"],
            values["causal_rule_codes"],
            proposed,
            changed,
            affected,
            facts,
            lane,
            ordinary,
            degraded,
            state,
            canonical_digest(content),
            _VERIFIED_RECEIPT_AUTHORITY,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-approval-row-v1",
            "field_id": self.field_id,
            "receipt_revision": self.receipt_revision,
            "upstream_field_revision": self.upstream_field_revision,
            "receipt_id": self.receipt_id,
            "call_order": self.call_order,
            "deadline_at": self.deadline_at,
            "target_context_digest": self.target_context_digest,
            "receipt_content_digest": self.receipt_content_digest,
            "prior_receipt_id": self.prior_receipt_id,
            "prior_receipt_content_digest": self.prior_receipt_content_digest,
            "component_outputs_digest": self.component_outputs_digest,
            "consequence_detail_digest": self.consequence_detail_digest,
            "validation_digest": self.validation_digest,
            "causal_rule_codes": list(self.causal_rule_codes),
            "proposed_marks": [list(item) for item in self.proposed_marks],
            "changed_marks": [list(item) for item in self.changed_marks],
            "affected_competitors": list(self.affected_competitors),
            "facts": _facts_value(self.facts),
            "lane": self.lane.value,
            "ordinary_batch_eligible": self.ordinary_batch_eligible,
            "degraded_batch_eligible": self.degraded_batch_eligible,
            "decision_state": self.decision_state.value,
            "row_digest": self.row_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, _authority: object) -> ApprovalRow:
        if _authority is not _VERIFIED_RECEIPT_AUTHORITY:
            raise ApprovalError("approval projection decode lacks store authority")
        expected = {
            "schema_version",
            "field_id",
            "receipt_revision",
            "upstream_field_revision",
            "receipt_id",
            "call_order",
            "deadline_at",
            "target_context_digest",
            "receipt_content_digest",
            "prior_receipt_id",
            "prior_receipt_content_digest",
            "component_outputs_digest",
            "consequence_detail_digest",
            "validation_digest",
            "causal_rule_codes",
            "proposed_marks",
            "changed_marks",
            "affected_competitors",
            "facts",
            "lane",
            "ordinary_batch_eligible",
            "degraded_batch_eligible",
            "decision_state",
            "row_digest",
        }
        if set(value) != expected or value.get("schema_version") != (
            "strathmark-v3-approval-row-v1"
        ):
            raise ApprovalError("approval row fields or schema differ")
        facts_value = value["facts"]
        if not isinstance(facts_value, Mapping):
            raise ApprovalError("approval row facts are invalid")
        try:
            facts = ApprovalFacts(
                IntegrityState(facts_value["integrity"]),
                FreshnessState(facts_value["freshness"]),
                AvailabilityMode(facts_value["availability"]),
                ConsequenceColor(facts_value["consequence"]),
                facts_value["zero_history"],
                facts_value["council_degraded"],
                facts_value["manual_construction"],
                tuple(facts_value["reason_codes"]),
                tuple((item[0], item[1]) for item in facts_value["availability_counts"]),
                ApprovalManualMode(facts_value["manual_mode"]),
                tuple(facts_value["flagged_competitor_ids"]),
                tuple((item[0], tuple(item[1])) for item in facts_value["flag_reason_tokens"]),
            )
            lane = ApprovalLane(value["lane"])
            state = DecisionState(value["decision_state"])
            proposed = tuple((item[0], item[1]) for item in value["proposed_marks"])
            changed = tuple((item[0], item[1], item[2]) for item in value["changed_marks"])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ApprovalError("approval row encoded values are invalid") from exc
        return cls(
            value["field_id"],
            value["receipt_revision"],
            value["upstream_field_revision"],
            value["receipt_id"],
            value["call_order"],
            value["deadline_at"],
            value["target_context_digest"],
            value["receipt_content_digest"],
            value["prior_receipt_id"],
            value["prior_receipt_content_digest"],
            value["component_outputs_digest"],
            value["consequence_detail_digest"],
            value["validation_digest"],
            tuple(value["causal_rule_codes"]),
            proposed,
            changed,
            tuple(value["affected_competitors"]),
            facts,
            lane,
            value["ordinary_batch_eligible"],
            value["degraded_batch_eligible"],
            state,
            value["row_digest"],
            _VERIFIED_RECEIPT_AUTHORITY,
        )

    def _with_decision(self, state: DecisionState, *, _authority: object) -> ApprovalRow:
        if _authority is not _VERIFIED_RECEIPT_AUTHORITY:
            raise ApprovalError("approval decision lacks store authority")
        if not isinstance(state, DecisionState) or state in {
            DecisionState.UNDECIDED,
            DecisionState.BLOCKED,
        }:
            raise ApprovalError("approval decision transition is invalid")
        if self.decision_state is not DecisionState.UNDECIDED:
            raise ApprovalError("approval row already has a terminal decision")
        value = self.to_dict()
        value["ordinary_batch_eligible"] = False
        value["degraded_batch_eligible"] = False
        value["decision_state"] = state.value
        value.pop("row_digest")
        value["row_digest"] = canonical_digest(value)
        return ApprovalRow.from_dict(value, _authority=_VERIFIED_RECEIPT_AUTHORITY)


@dataclass(frozen=True, slots=True)
class ApprovalPage:
    tournament_id: str
    snapshot_id: str
    offset: int
    limit: int
    total: int
    rows: tuple[ApprovalRow, ...]
    lifecycle_state: str
    preparation_completed: int
    preparation_total: int
    preparing_count: int
    ready_count: int
    blocked_count: int
    issued_count: int
    projection_current: bool
    empty_reason: QueueEmptyReason | None
    source_global_sequence: int
    decision_global_sequence: int
    lane_counts: tuple[tuple[str, int], ...]
    earliest_deadline_at: str | None
    retry_guidance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchAcknowledgment:
    snapshot_id: str
    included_receipts: tuple[str, ...]
    excluded_receipts: tuple[str, ...]
    actor_metadata_digest: str
    decided_at: str
    degraded: bool
    acknowledgment_digest: str

    def __post_init__(self) -> None:
        if not self.snapshot_id.startswith("approval_snapshot:"):
            raise ApprovalError("batch acknowledgment snapshot is invalid")
        if not isinstance(self.degraded, bool):
            raise ApprovalError("batch degraded flag must be explicit")
        require_utc_milliseconds(self.decided_at)
        for group in (self.included_receipts, self.excluded_receipts):
            if not isinstance(group, tuple) or len(set(group)) != len(group):
                raise ApprovalError("batch receipt identities must be unique tuples")
            for receipt in group:
                require_identifier(receipt, expected_namespace="receipt")
        if set(self.included_receipts) & set(self.excluded_receipts):
            raise ApprovalError("batch included and excluded receipts overlap")
        _digest(self.actor_metadata_digest)
        _digest(self.acknowledgment_digest)
        if self.acknowledgment_digest != canonical_digest(_ack_value(self)):
            raise ApprovalError("batch acknowledgment digest mismatch")


@dataclass(frozen=True, slots=True)
class ApprovalProjection:
    tournament_id: str
    rows: tuple[ApprovalRow, ...]
    global_sequence: int
    lifecycle_state: str
    preparation_completed: int
    preparation_total: int
    projection_current: bool
    snapshot_id: str
    empty_reason: QueueEmptyReason | None

    def __post_init__(self) -> None:
        require_identifier(self.tournament_id, expected_namespace="tournament")
        if not isinstance(self.rows, tuple) or not all(
            isinstance(item, ApprovalRow) for item in self.rows
        ):
            raise ApprovalError("projection rows must be typed")
        if self.rows != tuple(sorted(self.rows, key=lambda item: (item.call_order, item.field_id))):
            raise ApprovalError("projection rows must be in canonical call order")
        if len({item.field_id for item in self.rows}) != len(self.rows):
            raise ApprovalError("approval projection cannot repeat a field")
        if (
            any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in (
                    self.global_sequence,
                    self.preparation_completed,
                    self.preparation_total,
                )
            )
            or self.preparation_completed > self.preparation_total
        ):
            raise ApprovalError("projection sequence/progress is invalid")
        if not isinstance(self.projection_current, bool):
            raise ApprovalError("projection currency must be explicit")
        expected = f"approval_snapshot:{canonical_digest(_projection_value(self))}"
        if self.snapshot_id != expected:
            raise ApprovalError("approval projection snapshot digest mismatch")
        if self.empty_reason is not _empty_reason(
            self.rows,
            self.preparation_completed,
            self.preparation_total,
            self.lifecycle_state,
        ):
            raise ApprovalError("approval projection empty reason is not derived")

    @classmethod
    def create(cls, **values: Any) -> ApprovalProjection:
        rows = tuple(values["rows"])
        if not all(isinstance(item, ApprovalRow) for item in rows):
            raise ApprovalError("projection rows must be typed")
        rows = tuple(sorted(rows, key=lambda item: (item.call_order, item.field_id)))
        if len({item.field_id for item in rows}) != len(rows):
            raise ApprovalError("approval projection cannot repeat a field")
        global_sequence = values["global_sequence"]
        completed = values["preparation_completed"]
        total = values["preparation_total"]
        if (
            any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in (global_sequence, completed, total)
            )
            or completed > total
        ):
            raise ApprovalError("projection sequence/progress is invalid")
        if not isinstance(values["projection_current"], bool):
            raise ApprovalError("projection currency must be explicit")
        content = {
            "schema_version": "strathmark-v3-approval-snapshot-v1",
            "tournament_id": values["tournament_id"],
            "row_digests": [item.row_digest for item in rows],
            "global_sequence": global_sequence,
            "lifecycle_state": values["lifecycle_state"],
            "preparation_completed": completed,
            "preparation_total": total,
            "projection_current": values["projection_current"],
        }
        reason = _empty_reason(rows, completed, total, values["lifecycle_state"])
        return cls(
            values["tournament_id"],
            rows,
            global_sequence,
            values["lifecycle_state"],
            completed,
            total,
            values["projection_current"],
            f"approval_snapshot:{canonical_digest(content)}",
            reason,
        )

    def page(self, *, offset: int, limit: int) -> ApprovalPage:
        if (
            any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in (offset, limit)
            )
            or not 1 <= limit <= 100
        ):
            raise ApprovalError("approval page must use offset >=0 and limit 1..100")
        return ApprovalPage(
            self.tournament_id,
            self.snapshot_id,
            offset,
            limit,
            len(self.rows),
            self.rows[offset : offset + limit],
            self.lifecycle_state,
            self.preparation_completed,
            self.preparation_total,
            self.preparation_total - self.preparation_completed,
            sum(item.lane is not ApprovalLane.INTEGRITY_BLOCKED for item in self.rows),
            sum(item.lane is ApprovalLane.INTEGRITY_BLOCKED for item in self.rows),
            max(0, self.preparation_completed - len(self.rows)),
            self.projection_current,
            self.empty_reason,
            self.global_sequence,
            self.global_sequence,
            tuple(
                sorted(
                    {
                        lane.value: sum(item.lane is lane for item in self.rows)
                        for lane in ApprovalLane
                        if any(item.lane is lane for item in self.rows)
                    }.items()
                )
            ),
            min((item.deadline_at for item in self.rows), default=None),
            (
                "reload_snapshot_on_conflict",
                "poll_while_preparing",
                "open_verified_detail_for_exceptions",
            ),
        )

    def acknowledge_batch(
        self,
        *,
        snapshot_id: str,
        included: tuple[tuple[str, int], ...],
        excluded: tuple[tuple[str, int], ...],
        actor_metadata: Mapping[str, Any],
        decided_at: str,
        degraded: bool,
    ) -> BatchAcknowledgment:
        require_utc_milliseconds(decided_at)
        if not isinstance(degraded, bool) or not isinstance(actor_metadata, Mapping):
            raise ApprovalError("batch decision inputs are invalid")
        current = {item.receipt_id: item for item in self.rows}
        if not self.projection_current:
            raise ApprovalConflict("approval projection is not current", ())
        if not isinstance(included, tuple) or not isinstance(excluded, tuple):
            raise ApprovalError("batch selections must be immutable tuples")
        selections = (*included, *excluded)
        if not all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], int)
            and not isinstance(item[1], bool)
            for item in selections
        ):
            raise ApprovalError("batch selections must contain receipt/revision pairs")
        included_ids = tuple(item[0] for item in included)
        excluded_ids = tuple(item[0] for item in excluded)
        if len(set(included_ids)) != len(included_ids) or len(set(excluded_ids)) != len(
            excluded_ids
        ):
            raise ApprovalConflict("batch selection contains a duplicate receipt", ())
        if set(included_ids) & set(excluded_ids):
            raise ApprovalConflict("batch included/excluded receipts overlap", ())
        supplied = {receipt: revision for receipt, revision in selections}
        replacements: list[tuple[str, str, int]] = []
        if snapshot_id != self.snapshot_id:
            for row in self.rows:
                if supplied.get(row.receipt_id) != row.receipt_revision:
                    replacements.append((row.field_id, row.receipt_id, row.receipt_revision))
            raise ApprovalConflict("approval snapshot is stale", tuple(replacements))
        if set(supplied) != set(current) or any(
            supplied[receipt] != row.receipt_revision for receipt, row in current.items()
        ):
            raise ApprovalConflict("batch must bind every included/excluded revision", ())
        for receipt, _revision in included:
            row = current[receipt]
            eligible = row.degraded_batch_eligible if degraded else row.ordinary_batch_eligible
            if not eligible:
                raise ApprovalConflict("batch lane contains an ineligible receipt", ())
        values = {
            "snapshot_id": self.snapshot_id,
            "included_receipts": tuple(receipt for receipt, _ in included),
            "excluded_receipts": tuple(receipt for receipt, _ in excluded),
            "actor_metadata_digest": canonical_digest(actor_metadata),
            "decided_at": decided_at,
            "degraded": degraded,
        }
        return BatchAcknowledgment(**values, acknowledgment_digest=canonical_digest(values))


def _empty_reason(
    rows: tuple[ApprovalRow, ...], completed: int, total: int, lifecycle: str
) -> QueueEmptyReason | None:
    if lifecycle == "closed":
        return QueueEmptyReason.TOURNAMENT_CLOSED
    if lifecycle == "all_issued":
        return QueueEmptyReason.ALL_ISSUED
    if completed < total:
        return QueueEmptyReason.STILL_PREPARING
    if not rows and total == 0:
        return QueueEmptyReason.NO_SCHEDULED_FIELDS
    if rows and all(item.lane is ApprovalLane.INTEGRITY_BLOCKED for item in rows):
        return QueueEmptyReason.ALL_BLOCKED
    if not any(item.ordinary_batch_eligible or item.degraded_batch_eligible for item in rows):
        return QueueEmptyReason.NO_BATCH_ELIGIBLE_FIELDS
    return None


def _facts_value(facts: ApprovalFacts) -> dict[str, Any]:
    return {
        "integrity": facts.integrity.value,
        "freshness": facts.freshness.value,
        "availability": facts.availability.value,
        "consequence": facts.consequence.value,
        "zero_history": facts.zero_history,
        "council_degraded": facts.council_degraded,
        "manual_construction": facts.manual_construction,
        "reason_codes": list(facts.reason_codes),
        "availability_counts": [list(item) for item in facts.availability_counts],
        "manual_mode": facts.manual_mode.value,
        "flagged_competitor_ids": list(facts.flagged_competitor_ids),
        "flag_reason_tokens": [
            [competitor_id, list(tokens)] for competitor_id, tokens in facts.flag_reason_tokens
        ],
    }


def _row_value(row: ApprovalRow) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-approval-row-v1",
        "field_id": row.field_id,
        "receipt_revision": row.receipt_revision,
        "upstream_field_revision": row.upstream_field_revision,
        "receipt_id": row.receipt_id,
        "call_order": row.call_order,
        "deadline_at": row.deadline_at,
        "target_context_digest": row.target_context_digest,
        "receipt_content_digest": row.receipt_content_digest,
        "prior_receipt_id": row.prior_receipt_id,
        "prior_receipt_content_digest": row.prior_receipt_content_digest,
        "component_outputs_digest": row.component_outputs_digest,
        "consequence_detail_digest": row.consequence_detail_digest,
        "validation_digest": row.validation_digest,
        "causal_rule_codes": row.causal_rule_codes,
        "proposed_marks": row.proposed_marks,
        "changed_marks": row.changed_marks,
        "affected_competitors": row.affected_competitors,
        "facts": _facts_value(row.facts),
        "lane": row.lane.value,
        "ordinary_batch_eligible": row.ordinary_batch_eligible,
        "degraded_batch_eligible": row.degraded_batch_eligible,
        "decision_state": row.decision_state.value,
    }


def _projection_value(projection: ApprovalProjection) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-approval-snapshot-v1",
        "tournament_id": projection.tournament_id,
        "row_digests": [item.row_digest for item in projection.rows],
        "global_sequence": projection.global_sequence,
        "lifecycle_state": projection.lifecycle_state,
        "preparation_completed": projection.preparation_completed,
        "preparation_total": projection.preparation_total,
        "projection_current": projection.projection_current,
    }


def _ack_value(acknowledgment: BatchAcknowledgment) -> dict[str, Any]:
    return {
        "snapshot_id": acknowledgment.snapshot_id,
        "included_receipts": acknowledgment.included_receipts,
        "excluded_receipts": acknowledgment.excluded_receipts,
        "actor_metadata_digest": acknowledgment.actor_metadata_digest,
        "decided_at": acknowledgment.decided_at,
        "degraded": acknowledgment.degraded,
    }


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ApprovalError("value must be a lower-case SHA-256 digest")
    return value


__all__ = [
    "ApprovalConflict",
    "ApprovalConflictChange",
    "ApprovalDecisionAction",
    "ApprovalDecisionCommand",
    "ApprovalDecisionReceipt",
    "ApprovalDecisionSelection",
    "ApprovalFacts",
    "ApprovalLane",
    "ApprovalManualMode",
    "ApprovalPage",
    "ApprovalProjection",
    "ApprovalRow",
    "AvailabilityMode",
    "BatchAcknowledgment",
    "DecisionState",
    "FreshnessState",
    "IntegrityState",
    "QueueEmptyReason",
    "derive_approval_lane",
    "derive_receipt_approval_facts",
]
