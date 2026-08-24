"""Validated command intents prepared before the SQLite writer lock."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier

MAX_COMMAND_RESULT_BYTES = 1_048_576

_COMMAND_EVENT: dict[CommandKind, tuple[AggregateKind, EventKind]] = {
    CommandKind.REVISE_TOURNAMENT_SNAPSHOT: (
        AggregateKind.TOURNAMENT_INGRESS,
        EventKind.TOURNAMENT_SNAPSHOT_REVISED,
    ),
    CommandKind.REVISE_ROUND_SNAPSHOT: (
        AggregateKind.ROUND_INGRESS,
        EventKind.ROUND_SNAPSHOT_REVISED,
    ),
    CommandKind.REVISE_FIELD_ROSTER: (
        AggregateKind.FIELD_INGRESS,
        EventKind.FIELD_ROSTER_REVISED,
    ),
    CommandKind.RECORD_RESULT: (AggregateKind.RESULT, EventKind.RESULT_RECORDED),
    CommandKind.CORRECT_RESULT: (AggregateKind.RESULT, EventKind.RESULT_SUPERSEDED),
    CommandKind.VOID_RESULT: (AggregateKind.RESULT, EventKind.RESULT_SUPERSEDED),
    CommandKind.SETTLE_LIVE_RACE: (AggregateKind.SETTLEMENT, EventKind.LIVE_RACE_SETTLED),
    CommandKind.FREEZE_EVIDENCE_EPOCH: (AggregateKind.EPOCH, EventKind.ROUND_EPOCH_FROZEN),
    CommandKind.COMPLETE_DERIVATION_REACTION: (
        AggregateKind.REACTION,
        EventKind.DERIVATION_REACTION_COMPLETED,
    ),
    CommandKind.COMPLETE_DERIVATION_SEQUENCE: (
        AggregateKind.DERIVATION,
        EventKind.DERIVATION_SEQUENCE_COMPLETED,
    ),
    CommandKind.CONFIGURE_TOURNAMENT: (AggregateKind.TOURNAMENT, EventKind.TOURNAMENT_CONFIGURED),
    CommandKind.OPEN_TOURNAMENT: (AggregateKind.TOURNAMENT, EventKind.TOURNAMENT_OPENED),
    CommandKind.CLOSE_TOURNAMENT: (AggregateKind.TOURNAMENT, EventKind.TOURNAMENT_CLOSED),
    CommandKind.CONFIGURE_ROUND: (AggregateKind.ROUND, EventKind.ROUND_CONFIGURED),
    CommandKind.FREEZE_ROUND: (AggregateKind.ROUND, EventKind.ROUND_FROZEN),
    CommandKind.BEGIN_ROUND_CLOSING: (AggregateKind.ROUND, EventKind.ROUND_CLOSING_STARTED),
    CommandKind.CLOSE_ROUND: (AggregateKind.ROUND, EventKind.ROUND_CLOSED),
    CommandKind.OPTIMIZE_FIELD: (AggregateKind.FIELD, EventKind.FIELD_OPTIMIZED),
    CommandKind.SUPERSEDE_FIELD: (AggregateKind.FIELD, EventKind.FIELD_SUPERSEDED),
    CommandKind.REGENERATE_FIELD: (AggregateKind.FIELD, EventKind.FIELD_REGENERATED),
    CommandKind.ACKNOWLEDGE_ISSUE: (AggregateKind.FIELD, EventKind.FIELD_ISSUED),
    CommandKind.SETTLE_FIELD: (AggregateKind.FIELD, EventKind.FIELD_SETTLED),
    CommandKind.QUEUE_JOB: (AggregateKind.JOB, EventKind.JOB_QUEUED),
    CommandKind.LEASE_JOB: (AggregateKind.JOB, EventKind.JOB_LEASED),
    CommandKind.SUCCEED_JOB: (AggregateKind.JOB, EventKind.JOB_SUCCEEDED),
    CommandKind.INVALIDATE_JOB: (AggregateKind.JOB, EventKind.JOB_INVALID),
    CommandKind.RECORD_RETRYABLE_JOB_FAILURE: (
        AggregateKind.JOB,
        EventKind.JOB_RETRYABLE_FAILED,
    ),
    CommandKind.REQUEUE_JOB: (AggregateKind.JOB, EventKind.JOB_REQUEUED),
    CommandKind.MARK_JOB_STALE: (AggregateKind.JOB, EventKind.JOB_STALE),
    CommandKind.RECORD_PERMANENT_JOB_FAILURE: (
        AggregateKind.JOB,
        EventKind.JOB_PERMANENT_FAILED,
    ),
    CommandKind.CANCEL_JOB: (AggregateKind.JOB, EventKind.JOB_CANCELLED),
    CommandKind.CREATE_MODEL_CANDIDATE: (
        AggregateKind.BUNDLE,
        EventKind.MODEL_CANDIDATE_CREATED,
    ),
    CommandKind.PROMOTE_BUNDLE: (AggregateKind.BUNDLE, EventKind.BUNDLE_PROMOTED),
    CommandKind.ROLLBACK_BUNDLE: (AggregateKind.BUNDLE, EventKind.BUNDLE_ROLLED_BACK),
    CommandKind.RECORD_CAPABILITY_UPDATE: (
        AggregateKind.COMPETITOR,
        EventKind.CAPABILITY_UPDATED,
    ),
    CommandKind.REBASE_CAPABILITY_STATE: (
        AggregateKind.COMPETITOR,
        EventKind.CAPABILITY_STATE_REBASED,
    ),
    CommandKind.CHANGE_WEIGHTS: (AggregateKind.WEIGHTS, EventKind.WEIGHTS_CHANGED),
    CommandKind.SUSPEND_LIVE: (AggregateKind.WEIGHTS, EventKind.LIVE_SUSPENDED),
    CommandKind.RESUME_LIVE: (AggregateKind.WEIGHTS, EventKind.LIVE_RESUMED),
    CommandKind.EMERGENCY_STOP: (AggregateKind.WEIGHTS, EventKind.EMERGENCY_STOPPED),
}


@dataclass(frozen=True, slots=True)
class EventIntent:
    """One already-computed event append, without sequence/hash values."""

    aggregate_kind: AggregateKind
    aggregate_id: StableIdentifier
    event_kind: EventKind

    def __post_init__(self) -> None:
        if not isinstance(self.aggregate_kind, AggregateKind):
            raise ContractError("event intent aggregate_kind must be an AggregateKind")
        if not isinstance(self.aggregate_id, StableIdentifier):
            raise ContractError("event intent aggregate_id must be a StableIdentifier")
        require_identifier(self.aggregate_id, expected_namespace=self.aggregate_kind.value)
        if not isinstance(self.event_kind, EventKind):
            raise ContractError("event intent event_kind must be an EventKind")


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """Credential-bound, bounded work ready for one short atomic commit."""

    principal_id: StableIdentifier
    command: CommandEnvelope
    events: tuple[EventIntent, ...]
    result_schema_version: str
    result: Mapping[str, Any]
    occurred_at_utc: str
    monotonic_elapsed_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.principal_id, StableIdentifier):
            raise ContractError("principal_id must be a StableIdentifier")
        require_identifier(self.principal_id, expected_namespace="actor")
        if not isinstance(self.command, CommandEnvelope):
            raise ContractError("command request requires a CommandEnvelope")
        if self.command.actor_id != self.principal_id:
            raise ContractError("command actor must be the credential-derived principal")
        if not isinstance(self.events, tuple) or not self.events:
            raise ContractError("command request requires an immutable nonempty event tuple")
        if any(not isinstance(item, EventIntent) for item in self.events):
            raise ContractError("command request events must be EventIntent values")
        identities = tuple(str(item.aggregate_id) for item in self.events)
        if len(set(identities)) != len(identities):
            raise ContractError("one command can append at most one event per aggregate")
        expected = tuple(aggregate_id for aggregate_id, _version in self.command.expected_versions)
        if tuple(sorted(identities)) != expected:
            raise ContractError(
                "event aggregates must exactly match the sorted expected-version map"
            )
        if not isinstance(self.result_schema_version, str) or not self.result_schema_version:
            raise ContractError("result_schema_version must be a nonempty string")
        if not isinstance(self.result, Mapping):
            raise ContractError("command result must be a mapping")
        encoded_result = canonical_bytes(self.result, max_bytes=MAX_COMMAND_RESULT_BYTES)
        object.__setattr__(self, "result", _deep_freeze(json.loads(encoded_result)))
        require_utc_milliseconds(self.occurred_at_utc)
        if (
            isinstance(self.monotonic_elapsed_ms, bool)
            or not isinstance(self.monotonic_elapsed_ms, int)
            or self.monotonic_elapsed_ms < 0
        ):
            raise ContractError("monotonic_elapsed_ms must be a non-negative integer")
        validate_command_event_intents(self.command, self.events)


def validate_command_event_intents(
    command: CommandEnvelope, events: tuple[EventIntent, ...]
) -> None:
    """Bind a closed command kind to its exact aggregate/event intent set."""

    if command.kind is CommandKind.COMMIT_FORECAST:
        if (
            len(events) != 1
            or events[0].aggregate_kind is not AggregateKind.FORECAST
            or events[0].event_kind
            not in {
                EventKind.COMPONENT_FORECAST_COMMITTED,
                EventKind.COMPONENT_FORECAST_REJECTED,
            }
            or events[0].aggregate_id != command.target_aggregate
        ):
            raise ContractError("forecast commit requires one sealed forecast event")
        return
    if command.kind is CommandKind.RECORD_SCORE:
        if (
            len(events) != 1
            or events[0].aggregate_kind is not AggregateKind.SCORE
            or events[0].event_kind not in {EventKind.SCORE_RECORDED, EventKind.SCORE_REVERSED}
            or events[0].aggregate_id != command.target_aggregate
        ):
            raise ContractError("score command requires one append-only score event")
        return
    if command.kind is CommandKind.FREEZE_EVIDENCE_EPOCH:
        epochs = [item for item in events if item.aggregate_kind is AggregateKind.EPOCH]
        rounds = [item for item in events if item.aggregate_kind is AggregateKind.ROUND]
        if (
            len(epochs) != 1
            or epochs[0].event_kind is not EventKind.ROUND_EPOCH_FROZEN
            or epochs[0].aggregate_id != command.target_aggregate
            or len(rounds) != 1
            or rounds[0].event_kind is not EventKind.ROUND_FROZEN
            or len(events) != 2
        ):
            raise ContractError(
                "evidence epoch freeze requires one epoch event and its round freeze"
            )
        return
    if command.kind is CommandKind.SETTLE_LIVE_RACE:
        settlements = [item for item in events if item.aggregate_kind is AggregateKind.SETTLEMENT]
        fields = [item for item in events if item.aggregate_kind is AggregateKind.FIELD]
        if (
            len(settlements) != 1
            or settlements[0].event_kind is not EventKind.LIVE_RACE_SETTLED
            or settlements[0].aggregate_id != command.target_aggregate
            or len(fields) != 1
            or fields[0].event_kind is not EventKind.FIELD_SETTLED
            or len(events) != 2
        ):
            raise ContractError(
                "live settlement requires one settlement event and its field settlement"
            )
        return
    if command.kind is CommandKind.SUPERSEDE_AND_SETTLE_RESULT:
        results = [item for item in events if item.aggregate_kind is AggregateKind.RESULT]
        settlements = [item for item in events if item.aggregate_kind is AggregateKind.SETTLEMENT]
        fields = [item for item in events if item.aggregate_kind is AggregateKind.FIELD]
        if (
            len(results) != 1
            or results[0].event_kind is not EventKind.RESULT_SUPERSEDED
            or results[0].aggregate_id != command.target_aggregate
            or len(settlements) != 1
            or settlements[0].event_kind is not EventKind.LIVE_RACE_SETTLED
            or any(item.event_kind is not EventKind.FIELD_SUPERSEDED for item in fields)
            or len(results) + len(settlements) + len(fields) != len(events)
        ):
            raise ContractError(
                "atomic correction requires one result supersession and one live settlement"
            )
        return
    if command.kind is CommandKind.REVISE_FIELD_ROSTER:
        ingress = [item for item in events if item.aggregate_kind is AggregateKind.FIELD_INGRESS]
        fields = [item for item in events if item.aggregate_kind is AggregateKind.FIELD]
        if (
            len(ingress) != 1
            or ingress[0].event_kind is not EventKind.FIELD_ROSTER_REVISED
            or ingress[0].aggregate_id != command.target_aggregate
            or len(fields) > 1
            or any(item.event_kind is not EventKind.FIELD_SUPERSEDED for item in fields)
            or len(ingress) + len(fields) != len(events)
        ):
            raise ContractError(
                "field roster revision requires ingress and at most one prepared-field supersession"
            )
        return
    if command.kind is CommandKind.ACKNOWLEDGE_BATCH_ISSUE:
        if command.target_aggregate.namespace != AggregateKind.ISSUE_BATCH.value:
            raise ContractError("batch issue must target an issue_batch aggregate")
        batch = [item for item in events if item.aggregate_kind is AggregateKind.ISSUE_BATCH]
        fields = [item for item in events if item.aggregate_kind is AggregateKind.FIELD]
        if (
            len(batch) != 1
            or batch[0].aggregate_id != command.target_aggregate
            or batch[0].event_kind is not EventKind.ISSUE_BATCH_ISSUED
            or not fields
            or any(item.event_kind is not EventKind.FIELD_ISSUED for item in fields)
        ):
            raise ContractError("batch issue requires one batch event and one or more field issues")
        return
    try:
        expected_kind, expected_event = _COMMAND_EVENT[command.kind]
    except KeyError as exc:
        raise ContractError("command kind is not supported by the lifecycle boundary") from exc
    if len(events) != 1:
        raise ContractError("lifecycle commands append exactly one aggregate event")
    event = events[0]
    if (
        event.aggregate_kind is not expected_kind
        or event.event_kind is not expected_event
        or event.aggregate_id != command.target_aggregate
    ):
        raise ContractError("command, target aggregate, and event kind do not match")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


__all__ = [
    "MAX_COMMAND_RESULT_BYTES",
    "CommandRequest",
    "EventIntent",
    "validate_command_event_intents",
]
