"""Closed official-result vocabulary and one raw-time admission rule."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from strathmark.v3.contracts.errors import ContractError

STATUS_SCHEMA_VERSION = "strathmark-v3-official-result-v1"
LIFECYCLE_SCHEMA_VERSION = "strathmark-v3-aggregate-lifecycle-v1"


class ResultStatus(str, Enum):
    """Version-one official outcome states; values are persisted verbatim."""

    COMPLETION = "completion"
    DNF = "dnf"
    DQ = "dq"
    DNS = "dns"
    VOID = "void"
    PENALTY = "penalty"


class LifecycleAggregateKind(str, Enum):
    TOURNAMENT_INGRESS = "tournament_ingress"
    ROUND_INGRESS = "round_ingress"
    FIELD_INGRESS = "field_ingress"
    RESULT = "result"
    SETTLEMENT = "settlement"
    EPOCH = "epoch"
    REACTION = "reaction"
    DERIVATION = "derivation"
    TOURNAMENT = "tournament"
    ROUND = "round"
    FIELD = "field"
    JOB = "job"
    BUNDLE = "bundle"
    ISSUE_BATCH = "issue_batch"
    COMPETITOR = "competitor"


class LifecycleStatus(str, Enum):
    TOURNAMENT_INGRESS_CURRENT = "tournament_ingress_current"
    ROUND_INGRESS_CURRENT = "round_ingress_current"
    FIELD_INGRESS_CURRENT = "field_ingress_current"
    RESULT_ACTIVE = "result_active"
    SETTLEMENT_RECORDED = "settlement_recorded"
    EPOCH_FROZEN = "epoch_frozen"
    REACTION_COMPLETED = "reaction_completed"
    DERIVATION_COMPLETED = "derivation_completed"
    TOURNAMENT_CONFIGURED = "tournament_configured"
    TOURNAMENT_OPEN = "tournament_open"
    TOURNAMENT_CLOSED = "tournament_closed"
    ROUND_CONFIGURED = "round_configured"
    ROUND_FROZEN = "round_frozen"
    ROUND_CLOSING = "round_closing"
    ROUND_CLOSED = "round_closed"
    FIELD_PREPARED = "field_prepared"
    FIELD_ISSUED = "field_issued"
    FIELD_SETTLED = "field_settled"
    FIELD_SUPERSEDED = "field_superseded"
    JOB_QUEUED = "job_queued"
    JOB_LEASED = "job_leased"
    JOB_SUCCEEDED = "job_succeeded"
    JOB_INVALID = "job_invalid"
    JOB_RETRYABLE_FAILED = "job_retryable_failed"
    JOB_STALE = "job_stale"
    JOB_PERMANENT_FAILED = "job_permanent_failed"
    JOB_CANCELLED = "job_cancelled"
    BUNDLE_CANDIDATE = "bundle_candidate"
    BUNDLE_PROMOTED = "bundle_promoted"
    BUNDLE_ROLLED_BACK = "bundle_rolled_back"
    ISSUE_BATCH_ISSUED = "issue_batch_issued"
    COMPETITOR_CAPABILITY_CURRENT = "competitor_capability_current"


_LIFECYCLE_TRANSITIONS: dict[LifecycleStatus, frozenset[LifecycleStatus]] = {
    LifecycleStatus.TOURNAMENT_INGRESS_CURRENT: frozenset(
        {LifecycleStatus.TOURNAMENT_INGRESS_CURRENT}
    ),
    LifecycleStatus.ROUND_INGRESS_CURRENT: frozenset({LifecycleStatus.ROUND_INGRESS_CURRENT}),
    LifecycleStatus.FIELD_INGRESS_CURRENT: frozenset({LifecycleStatus.FIELD_INGRESS_CURRENT}),
    LifecycleStatus.RESULT_ACTIVE: frozenset({LifecycleStatus.RESULT_ACTIVE}),
    LifecycleStatus.SETTLEMENT_RECORDED: frozenset(),
    LifecycleStatus.EPOCH_FROZEN: frozenset(),
    LifecycleStatus.REACTION_COMPLETED: frozenset(),
    LifecycleStatus.DERIVATION_COMPLETED: frozenset(),
    LifecycleStatus.TOURNAMENT_CONFIGURED: frozenset({LifecycleStatus.TOURNAMENT_OPEN}),
    LifecycleStatus.TOURNAMENT_OPEN: frozenset({LifecycleStatus.TOURNAMENT_CLOSED}),
    LifecycleStatus.TOURNAMENT_CLOSED: frozenset(),
    LifecycleStatus.ROUND_CONFIGURED: frozenset({LifecycleStatus.ROUND_FROZEN}),
    LifecycleStatus.ROUND_FROZEN: frozenset({LifecycleStatus.ROUND_CLOSING}),
    LifecycleStatus.ROUND_CLOSING: frozenset({LifecycleStatus.ROUND_CLOSED}),
    LifecycleStatus.ROUND_CLOSED: frozenset(),
    LifecycleStatus.FIELD_PREPARED: frozenset(
        {LifecycleStatus.FIELD_ISSUED, LifecycleStatus.FIELD_SUPERSEDED}
    ),
    LifecycleStatus.FIELD_SUPERSEDED: frozenset({LifecycleStatus.FIELD_PREPARED}),
    LifecycleStatus.FIELD_ISSUED: frozenset({LifecycleStatus.FIELD_SETTLED}),
    LifecycleStatus.FIELD_SETTLED: frozenset(),
    LifecycleStatus.JOB_QUEUED: frozenset(
        {LifecycleStatus.JOB_LEASED, LifecycleStatus.JOB_CANCELLED}
    ),
    LifecycleStatus.JOB_LEASED: frozenset(
        {
            LifecycleStatus.JOB_SUCCEEDED,
            LifecycleStatus.JOB_INVALID,
            LifecycleStatus.JOB_RETRYABLE_FAILED,
            LifecycleStatus.JOB_STALE,
            LifecycleStatus.JOB_PERMANENT_FAILED,
        }
    ),
    LifecycleStatus.JOB_RETRYABLE_FAILED: frozenset({LifecycleStatus.JOB_QUEUED}),
    LifecycleStatus.JOB_SUCCEEDED: frozenset(),
    LifecycleStatus.JOB_INVALID: frozenset(),
    LifecycleStatus.JOB_STALE: frozenset(),
    LifecycleStatus.JOB_PERMANENT_FAILED: frozenset(),
    LifecycleStatus.JOB_CANCELLED: frozenset(),
    LifecycleStatus.BUNDLE_CANDIDATE: frozenset({LifecycleStatus.BUNDLE_PROMOTED}),
    LifecycleStatus.BUNDLE_PROMOTED: frozenset({LifecycleStatus.BUNDLE_ROLLED_BACK}),
    LifecycleStatus.BUNDLE_ROLLED_BACK: frozenset(),
    LifecycleStatus.ISSUE_BATCH_ISSUED: frozenset(),
    LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT: frozenset(
        {LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT}
    ),
}


@dataclass(frozen=True, slots=True)
class AggregateLifecycle:
    """One closed aggregate state plus its legal transition table."""

    aggregate_kind: LifecycleAggregateKind
    status: LifecycleStatus

    def __post_init__(self) -> None:
        if not isinstance(self.aggregate_kind, LifecycleAggregateKind):
            raise ContractError("aggregate_kind must be a LifecycleAggregateKind")
        if not isinstance(self.status, LifecycleStatus):
            raise ContractError("status must be a LifecycleStatus")
        if not self.status.value.startswith(f"{self.aggregate_kind.value}_"):
            raise ContractError("lifecycle status does not belong to aggregate kind")

    def transition_to(self, next_status: LifecycleStatus) -> AggregateLifecycle:
        if not isinstance(next_status, LifecycleStatus):
            raise ContractError("next status must be a LifecycleStatus")
        if next_status not in _LIFECYCLE_TRANSITIONS[self.status]:
            raise ContractError(
                f"illegal lifecycle transition {self.status.value} -> {next_status.value}"
            )
        return AggregateLifecycle(self.aggregate_kind, next_status)

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "aggregate_kind": self.aggregate_kind.value,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AggregateLifecycle:
        _require_fields(value, {"schema_version", "aggregate_kind", "status"})
        _require_schema(value["schema_version"], LIFECYCLE_SCHEMA_VERSION)
        try:
            return cls(
                LifecycleAggregateKind(value["aggregate_kind"]),
                LifecycleStatus(value["status"]),
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("unknown aggregate lifecycle kind or status") from exc


@dataclass(frozen=True, slots=True)
class AdmittedCompletion:
    """Raw performance admitted by the evidence governor."""

    raw_time_ms: int
    source_revision: int

    def __post_init__(self) -> None:
        _require_positive_int(self.raw_time_ms, "raw_time_ms")
        _require_positive_int(self.source_revision, "source_revision")


@dataclass(frozen=True, slots=True)
class OfficialResult:
    """One immutable official result revision before evidence admission.

    Penalty rows retain the observed raw time and penalty amount for audit, but
    neither an adjusted time nor the raw time is admitted as numeric model
    evidence by this version.  Nonfinish and void states cannot carry a raw time.
    """

    status: ResultStatus
    raw_time_ms: int | None
    penalty_ms: int | None
    revision: int
    supersedes_revision: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ResultStatus):
            raise ContractError("status must be a ResultStatus value")
        _require_positive_int(self.revision, "revision")
        if self.revision == 1 and self.supersedes_revision is not None:
            raise ContractError("revision 1 cannot supersede an earlier revision")
        if self.supersedes_revision is not None:
            _require_positive_int(self.supersedes_revision, "supersedes_revision")
            if self.supersedes_revision >= self.revision:
                raise ContractError("supersedes_revision must be lower than revision")

        if self.status is ResultStatus.COMPLETION:
            _require_positive_int(self.raw_time_ms, "raw_time_ms")
            if self.penalty_ms is not None:
                raise ContractError("penalty_ms is only valid for penalty status")
        elif self.status is ResultStatus.PENALTY:
            _require_positive_int(self.raw_time_ms, "raw_time_ms")
            _require_positive_int(self.penalty_ms, "penalty_ms")
        else:
            if self.raw_time_ms is not None:
                raise ContractError(f"raw_time_ms must be absent for {self.status.value}")
            if self.penalty_ms is not None:
                raise ContractError("penalty_ms is only valid for penalty status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "status": self.status.value,
            "raw_time_ms": self.raw_time_ms,
            "penalty_ms": self.penalty_ms,
            "revision": self.revision,
            "supersedes_revision": self.supersedes_revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OfficialResult:
        _require_fields(
            value,
            {
                "schema_version",
                "status",
                "raw_time_ms",
                "penalty_ms",
                "revision",
                "supersedes_revision",
            },
        )
        _require_schema(value["schema_version"], STATUS_SCHEMA_VERSION)
        try:
            status = ResultStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise ContractError("unknown official result status code") from exc
        return cls(
            status=status,
            raw_time_ms=value["raw_time_ms"],
            penalty_ms=value["penalty_ms"],
            revision=value["revision"],
            supersedes_revision=value["supersedes_revision"],
        )


def admit_raw_completion(result: OfficialResult) -> AdmittedCompletion | None:
    """Return eligible raw time once, without creating adjusted completions."""

    if not isinstance(result, OfficialResult):
        raise ContractError("raw-time admission requires an OfficialResult")
    if result.status is not ResultStatus.COMPLETION:
        return None
    assert result.raw_time_ms is not None
    return AdmittedCompletion(result.raw_time_ms, result.revision)


def _require_fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ContractError("contract fields are missing, unknown, or extra")


def _require_schema(actual: object, expected: str) -> None:
    if actual != expected:
        raise ContractError(f"unsupported schema version; expected {expected}")


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{label} must be a non-negative integer")
    return value


__all__ = [
    "LIFECYCLE_SCHEMA_VERSION",
    "STATUS_SCHEMA_VERSION",
    "AdmittedCompletion",
    "AggregateLifecycle",
    "LifecycleAggregateKind",
    "LifecycleStatus",
    "OfficialResult",
    "ResultStatus",
    "admit_raw_completion",
]
