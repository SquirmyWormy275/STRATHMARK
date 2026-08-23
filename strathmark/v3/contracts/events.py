"""Immutable typed event envelopes with global and aggregate hash chains."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import (
    _require_digest,
    _require_id,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.contracts.statuses import (
    _require_fields,
    _require_nonnegative_int,
    _require_positive_int,
    _require_schema,
)

EVENT_SCHEMA_VERSION = "strathmark-v3-event-envelope-v1"
MAX_EVENT_CANONICAL_BYTES = 1_048_576


class AggregateKind(str, Enum):
    TOURNAMENT = "tournament"
    ROUND = "round"
    FIELD = "field"
    COMPETITOR = "competitor"
    FORECAST = "forecast"
    JOB = "job"
    BUNDLE = "bundle"
    SCORE = "score"
    WEIGHTS = "weights"
    SYSTEM = "system"
    ISSUE_BATCH = "issue_batch"


class EventKind(str, Enum):
    TOURNAMENT_CONFIGURED = "tournament_configured"
    TOURNAMENT_OPENED = "tournament_opened"
    TOURNAMENT_CLOSED = "tournament_closed"
    ROUND_CONFIGURED = "round_configured"
    ROUND_FROZEN = "round_frozen"
    ROUND_CLOSING_STARTED = "round_closing_started"
    ROUND_CLOSED = "round_closed"
    RESULT_RECORDED = "result_recorded"
    RESULT_CORRECTED = "result_corrected"
    RESULT_VOIDED = "result_voided"
    COMPONENT_FORECAST_COMMITTED = "component_forecast_committed"
    COMPONENT_FORECAST_REJECTED = "component_forecast_rejected"
    COUNCIL_MEMBER_COMMITTED = "council_member_committed"
    CAPABILITY_UPDATED = "capability_updated"
    SCORE_RECORDED = "score_recorded"
    SCORE_REVERSED = "score_reversed"
    WEIGHTS_CHANGED = "weights_changed"
    FORECASTS_POOLED = "forecasts_pooled"
    DISAGREEMENT_CLASSIFIED = "disagreement_classified"
    OVERRIDE_RECORDED = "override_recorded"
    FIELD_OPTIMIZED = "field_optimized"
    FIELD_RECEIPT_PREPARED = "field_receipt_prepared"
    FIELD_SUPERSEDED = "field_superseded"
    FIELD_REGENERATED = "field_regenerated"
    FIELD_ISSUED = "field_issued"
    ISSUE_BATCH_ISSUED = "issue_batch_issued"
    FIELD_SETTLED = "field_settled"
    HISTORY_IMPORTED = "history_imported"
    JOB_QUEUED = "job_queued"
    JOB_LEASED = "job_leased"
    JOB_SUCCEEDED = "job_succeeded"
    JOB_INVALID = "job_invalid"
    JOB_RETRYABLE_FAILED = "job_retryable_failed"
    JOB_REQUEUED = "job_requeued"
    JOB_STALE = "job_stale"
    JOB_PERMANENT_FAILED = "job_permanent_failed"
    JOB_CANCELLED = "job_cancelled"
    MODEL_CANDIDATE_CREATED = "model_candidate_created"
    BUNDLE_PROMOTED = "bundle_promoted"
    BUNDLE_ROLLED_BACK = "bundle_rolled_back"
    MONITORING_RECORDED = "monitoring_recorded"
    LIVE_SUSPENDED = "live_suspended"
    LIVE_RESUMED = "live_resumed"
    EMERGENCY_STOPPED = "emergency_stopped"
    CHECKPOINT_ANCHORED = "checkpoint_anchored"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: StableIdentifier
    kind: EventKind
    aggregate_kind: AggregateKind
    aggregate_id: StableIdentifier
    aggregate_version: int
    global_sequence: int
    prior_global_digest: str
    prior_aggregate_digest: str
    occurred_at_utc: str
    monotonic_elapsed_ms: int
    command: CommandEnvelope
    event_digest: str
    schema_version: str = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, EVENT_SCHEMA_VERSION)
        _require_id(self.event_id, "event")
        if not isinstance(self.kind, EventKind):
            raise ContractError("event kind must be an EventKind value")
        if not isinstance(self.aggregate_kind, AggregateKind):
            raise ContractError("aggregate kind must be an AggregateKind value")
        if not isinstance(self.aggregate_id, StableIdentifier):
            raise ContractError("aggregate_id must be a StableIdentifier")
        require_identifier(self.aggregate_id, expected_namespace=self.aggregate_kind.value)
        _require_positive_int(self.aggregate_version, "aggregate_version")
        _require_positive_int(self.global_sequence, "global_sequence")
        _require_digest(self.prior_global_digest, "prior_global_digest")
        _require_digest(self.prior_aggregate_digest, "prior_aggregate_digest")
        require_utc_milliseconds(self.occurred_at_utc)
        _require_nonnegative_int(self.monotonic_elapsed_ms, "monotonic_elapsed_ms")
        if not isinstance(self.command, CommandEnvelope):
            raise ContractError("event command must be a CommandEnvelope")
        expected = dict(self.command.expected_versions).get(str(self.aggregate_id))
        if expected is None:
            raise ContractError("event aggregate must match a declared command target")
        if expected != self.aggregate_version - 1:
            raise ContractError("event aggregate version must follow the command expected version")
        _require_digest(self.event_digest, "event_digest")
        if self.event_digest != self.recompute_digest():
            raise ContractError("event digest mismatch")
        try:
            canonical_bytes(self.to_dict(), max_bytes=MAX_EVENT_CANONICAL_BYTES)
        except Exception as exc:
            raise ContractError("event exceeds the maximum canonical size") from exc

    @classmethod
    def create(cls, **arguments: Any) -> EventEnvelope:
        content = _event_content_value(**arguments)
        return cls(event_digest=canonical_digest(content), **arguments)

    def _content_value(self) -> dict[str, Any]:
        return _event_content_value(
            event_id=self.event_id,
            kind=self.kind,
            aggregate_kind=self.aggregate_kind,
            aggregate_id=self.aggregate_id,
            aggregate_version=self.aggregate_version,
            global_sequence=self.global_sequence,
            prior_global_digest=self.prior_global_digest,
            prior_aggregate_digest=self.prior_aggregate_digest,
            occurred_at_utc=self.occurred_at_utc,
            monotonic_elapsed_ms=self.monotonic_elapsed_ms,
            command=self.command,
        )

    def recompute_digest(self) -> str:
        return canonical_digest(self._content_value())

    def to_dict(self) -> dict[str, Any]:
        return {**self._content_value(), "event_digest": self.event_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EventEnvelope:
        expected = {
            "schema_version",
            "event_id",
            "kind",
            "aggregate_kind",
            "aggregate_id",
            "aggregate_version",
            "global_sequence",
            "prior_global_digest",
            "prior_aggregate_digest",
            "occurred_at_utc",
            "monotonic_elapsed_ms",
            "command",
            "event_digest",
        }
        _require_fields(value, expected)
        _require_schema(value["schema_version"], EVENT_SCHEMA_VERSION)
        try:
            kind = EventKind(value["kind"])
            aggregate_kind = AggregateKind(value["aggregate_kind"])
        except (TypeError, ValueError) as exc:
            raise ContractError("unknown event or aggregate kind") from exc
        return cls(
            event_id=require_identifier(value["event_id"], expected_namespace="event"),
            kind=kind,
            aggregate_kind=aggregate_kind,
            aggregate_id=require_identifier(value["aggregate_id"]),
            aggregate_version=value["aggregate_version"],
            global_sequence=value["global_sequence"],
            prior_global_digest=value["prior_global_digest"],
            prior_aggregate_digest=value["prior_aggregate_digest"],
            occurred_at_utc=value["occurred_at_utc"],
            monotonic_elapsed_ms=value["monotonic_elapsed_ms"],
            command=CommandEnvelope.from_dict(value["command"]),
            event_digest=value["event_digest"],
        )


def _event_content_value(**arguments: Any) -> dict[str, Any]:
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": str(arguments["event_id"]),
        "kind": arguments["kind"].value,
        "aggregate_kind": arguments["aggregate_kind"].value,
        "aggregate_id": str(arguments["aggregate_id"]),
        "aggregate_version": arguments["aggregate_version"],
        "global_sequence": arguments["global_sequence"],
        "prior_global_digest": arguments["prior_global_digest"],
        "prior_aggregate_digest": arguments["prior_aggregate_digest"],
        "occurred_at_utc": arguments["occurred_at_utc"],
        "monotonic_elapsed_ms": arguments["monotonic_elapsed_ms"],
        "command": arguments["command"].to_dict(),
    }


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "MAX_EVENT_CANONICAL_BYTES",
    "AggregateKind",
    "EventEnvelope",
    "EventKind",
]
