"""Application-owned contracts for durable scheduling and publication."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Protocol

from strathmark.v3.application.capacity import JobLane
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest

_TOKEN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
MANDATORY_EXTERNAL_FIELD_DEPENDENCIES = (
    "event_integrity",
    "projection_currency",
    "writer_latency",
    "disk_reserve",
    "issue_recovery_path",
)


class RollingRestartTrustMode(str, Enum):
    """Names the evidence available to a critical rolling restart."""

    LOCAL_CORRUPTION_ONLY = "local_corruption_only"
    EXTERNALLY_ANCHORED = "externally_anchored"


@dataclass(frozen=True, slots=True)
class RollingRestartExpectedHead:
    """A rolling checkpoint head retained independently from the SQLite file."""

    checkpoint_sequence: int
    checkpoint_digest: str

    def __post_init__(self) -> None:
        _positive(self.checkpoint_sequence, "expected rolling checkpoint sequence")
        _digest(self.checkpoint_digest, "expected rolling checkpoint")


@dataclass(frozen=True, slots=True)
class RollingRestartTrust:
    """Explicitly separates local corruption checks from rollback protection."""

    mode: RollingRestartTrustMode
    expected_head: RollingRestartExpectedHead | None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RollingRestartTrustMode):
            raise DurableJobError("rolling restart trust mode is invalid")
        if self.mode is RollingRestartTrustMode.EXTERNALLY_ANCHORED:
            if not isinstance(self.expected_head, RollingRestartExpectedHead):
                raise DurableJobError(
                    "externally anchored restart requires an expected rolling head"
                )
        elif self.expected_head is not None:
            raise DurableJobError("local corruption verification cannot claim an external head")

    @classmethod
    def local_corruption_only(cls) -> RollingRestartTrust:
        return cls(RollingRestartTrustMode.LOCAL_CORRUPTION_ONLY, None)

    @classmethod
    def externally_anchored(cls, expected_head: RollingRestartExpectedHead) -> RollingRestartTrust:
        return cls(RollingRestartTrustMode.EXTERNALLY_ANCHORED, expected_head)


class DurableJobError(RuntimeError):
    """Base durable-job failure shared by application and repository adapters."""


class JobAdmissionRejected(DurableJobError):
    """Capacity or deadline policy rejected work before dispatch."""


class JobConflict(DurableJobError):
    """An identity, revision, state, or fencing precondition conflicted."""


class JobDeadlineExceeded(JobConflict):
    """A lease or hard deadline has elapsed."""


@dataclass(frozen=True, slots=True)
class RollingRestartReceipt:
    """Bounded authenticated material recovered for the critical rolling path."""

    checkpoint_sequence: int
    checkpoint_digest: str
    source_global_sequence: int
    current_subject_count: int
    active_job_count: int
    pending_reaction_count: int
    trust_mode: RollingRestartTrustMode = RollingRestartTrustMode.LOCAL_CORRUPTION_ONLY

    def __post_init__(self) -> None:
        _positive(self.checkpoint_sequence, "rolling restart checkpoint sequence")
        _digest(self.checkpoint_digest, "rolling restart checkpoint")
        _nonnegative(self.source_global_sequence, "rolling restart source sequence")
        _nonnegative(self.current_subject_count, "rolling restart current subjects")
        _nonnegative(self.active_job_count, "rolling restart active jobs")
        _nonnegative(self.pending_reaction_count, "rolling restart pending reactions")
        if not isinstance(self.trust_mode, RollingRestartTrustMode):
            raise DurableJobError("rolling restart receipt trust mode is invalid")


@dataclass(frozen=True, slots=True)
class RollingRestartSuffixStatus:
    """Verified local checkpoint and bounded uncompacted delta suffix."""

    checkpoint_sequence: int
    checkpoint_digest: str
    checkpoint_created_at: str
    absorbed_delta_sequence: int
    absorbed_delta_digest: str
    delta_suffix_count: int
    delta_tip_sequence: int
    delta_tip_digest: str

    def __post_init__(self) -> None:
        _positive(self.checkpoint_sequence, "rolling restart checkpoint sequence")
        _digest(self.checkpoint_digest, "rolling restart checkpoint")
        if not isinstance(self.checkpoint_created_at, str) or not self.checkpoint_created_at:
            raise DurableJobError("rolling restart checkpoint time is invalid")
        _nonnegative(self.absorbed_delta_sequence, "absorbed delta sequence")
        _digest(self.absorbed_delta_digest, "absorbed delta")
        _nonnegative(self.delta_suffix_count, "rolling restart delta suffix")
        _nonnegative(self.delta_tip_sequence, "rolling restart delta tip")
        _digest(self.delta_tip_digest, "rolling restart delta tip")
        if self.delta_tip_sequence < self.absorbed_delta_sequence:
            raise DurableJobError("rolling restart delta tip precedes checkpoint")
        if self.delta_tip_sequence - self.absorbed_delta_sequence != self.delta_suffix_count:
            raise DurableJobError("rolling restart delta suffix count differs")


class FailureKind(str, Enum):
    SCHEMA = "schema"
    TRANSPORT = "transport"
    PROCESS = "process"
    VALIDATION = "validation"
    PERMANENT = "permanent"


@dataclass(frozen=True, slots=True)
class ProviderStorageAudit:
    raw_digest: str
    byte_count: int
    reference_json: str
    reference_digest: str

    @classmethod
    def create(cls, reference: Any) -> ProviderStorageAudit:
        to_dict = getattr(reference, "to_dict", None)
        if not callable(to_dict):
            raise DurableJobError("provider storage audit requires a canonical reference")
        value = to_dict()
        encoded = canonical_bytes(value)
        return cls(
            value.get("raw_digest"),
            value.get("byte_count"),
            encoded.decode("utf-8"),
            canonical_digest(value),
        )

    def __post_init__(self) -> None:
        _digest(self.raw_digest, "provider storage raw digest")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise DurableJobError("provider storage byte count must be non-negative")
        _canonical_json(self.reference_json, self.reference_digest, "provider storage reference")


@dataclass(frozen=True, slots=True)
class ProviderAttemptAudit:
    ordinal: int
    raw_digest: str
    validator_code: str
    accepted: bool
    storage_reference: ProviderStorageAudit

    def __post_init__(self) -> None:
        _positive(self.ordinal, "provider attempt ordinal")
        _digest(self.raw_digest, "provider attempt raw digest")
        if (
            not isinstance(self.validator_code, str)
            or _TOKEN.fullmatch(self.validator_code) is None
        ):
            raise DurableJobError("provider attempt validator code must be a machine token")
        if not isinstance(self.accepted, bool):
            raise DurableJobError("provider attempt acceptance must be explicit")
        if not isinstance(self.storage_reference, ProviderStorageAudit):
            raise DurableJobError("provider attempt requires one durable storage reference")
        if self.storage_reference.raw_digest != self.raw_digest:
            raise DurableJobError("provider attempt storage digest differs")


@dataclass(frozen=True, slots=True)
class ProviderExecutionAudit:
    provider_id: str
    member_id: str
    member_pin_json: str
    member_pin_digest: str
    status: str
    reason: str | None
    attempts: tuple[ProviderAttemptAudit, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.provider_id, "provider execution provider id"),
            (self.member_id, "provider execution member id"),
            (self.status, "provider execution status"),
        ):
            if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
                raise DurableJobError(f"{label} must be a machine token")
        if self.status not in {"succeeded", "failed"}:
            raise DurableJobError("provider execution status is closed")
        if (self.status == "succeeded") != (self.reason is None):
            raise DurableJobError("provider execution reason differs from status")
        if self.reason is not None and _TOKEN.fullmatch(self.reason) is None:
            raise DurableJobError("provider execution reason must be a machine token")
        _canonical_json(self.member_pin_json, self.member_pin_digest, "provider member pin")
        if not isinstance(self.attempts, tuple):
            raise DurableJobError("provider execution attempts must be immutable")
        if self.status == "succeeded" and not self.attempts:
            raise DurableJobError("successful provider execution requires a retained attempt")
        if any(not isinstance(item, ProviderAttemptAudit) for item in self.attempts):
            raise DurableJobError("provider execution attempts must be typed")
        if tuple(item.ordinal for item in self.attempts) != tuple(range(1, len(self.attempts) + 1)):
            raise DurableJobError("provider execution attempt ordinals must be consecutive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-provider-execution-audit-v1",
            "provider_id": self.provider_id,
            "member_id": self.member_id,
            "member_pin": json.loads(self.member_pin_json),
            "status": self.status,
            "reason": self.reason,
            "attempts": [
                {
                    "ordinal": item.ordinal,
                    "raw_digest": item.raw_digest,
                    "validator_code": item.validator_code,
                    "accepted": item.accepted,
                    "storage_reference": json.loads(item.storage_reference.reference_json),
                }
                for item in self.attempts
            ],
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> ProviderExecutionAudit:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "provider_id",
            "member_id",
            "member_pin",
            "status",
            "reason",
            "attempts",
        }:
            raise DurableJobError("provider execution audit fields differ")
        if value["schema_version"] != "strathmark-v3-provider-execution-audit-v1":
            raise DurableJobError("provider execution audit schema differs")
        attempts_value = value["attempts"]
        if not isinstance(attempts_value, list):
            raise DurableJobError("provider execution attempts must be a list")
        attempts = []
        for item in attempts_value:
            if not isinstance(item, dict) or set(item) != {
                "ordinal",
                "raw_digest",
                "validator_code",
                "accepted",
                "storage_reference",
            }:
                raise DurableJobError("provider attempt audit fields differ")
            reference = item["storage_reference"]
            encoded_reference = canonical_bytes(reference).decode("utf-8")
            storage = ProviderStorageAudit(
                reference.get("raw_digest") if isinstance(reference, dict) else None,
                reference.get("byte_count") if isinstance(reference, dict) else None,
                encoded_reference,
                canonical_digest(reference),
            )
            attempts.append(
                ProviderAttemptAudit(
                    item["ordinal"],
                    item["raw_digest"],
                    item["validator_code"],
                    item["accepted"],
                    storage,
                )
            )
        pin_json = canonical_bytes(value["member_pin"]).decode("utf-8")
        return cls(
            value["provider_id"],
            value["member_id"],
            pin_json,
            canonical_digest(value["member_pin"]),
            value["status"],
            value["reason"],
            tuple(attempts),
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    version: str
    base_delay_ms: int = 250
    maximum_delay_ms: int = 10_000
    schema_retry_limit: int = 1
    transport_attempt_limit: int = 4
    process_attempt_limit: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or _TOKEN.fullmatch(self.version) is None:
            raise DurableJobError("retry policy version must be a bounded machine token")
        _positive(self.base_delay_ms, "base delay")
        _positive(self.maximum_delay_ms, "maximum delay")
        if self.maximum_delay_ms < self.base_delay_ms:
            raise DurableJobError("maximum retry delay cannot be below the base")
        _nonnegative(self.schema_retry_limit, "schema retry limit")
        _positive(self.transport_attempt_limit, "transport attempt limit")
        _positive(self.process_attempt_limit, "process attempt limit")


class JobRecordPort(Protocol):
    job_id: str
    job_revision: int
    evidence_digest: str
    bundle_digest: str
    fencing_token: int


class QueueHealthPort(Protocol):
    observed_at: str
    field_ready: bool


@dataclass(frozen=True, slots=True)
class ReadinessDependencySnapshot:
    """Request-scoped truth for every non-queue R23.6 readiness dimension."""

    event_integrity: bool
    projection_currency: bool
    blob_integrity: bool
    pinned_bundle: bool
    formula: bool
    ml: bool
    llm_members: tuple[tuple[str, bool], ...]
    pool_degradation_mode: bool
    writer_latency: bool
    disk_reserve: bool
    backup_age: bool
    issue_recovery_path: bool
    cloud_consent: bool
    required_for_field: tuple[str, ...]

    def __post_init__(self) -> None:
        scalar_names = (
            "event_integrity",
            "projection_currency",
            "blob_integrity",
            "pinned_bundle",
            "formula",
            "ml",
            "pool_degradation_mode",
            "writer_latency",
            "disk_reserve",
            "backup_age",
            "issue_recovery_path",
            "cloud_consent",
        )
        if any(not isinstance(getattr(self, name), bool) for name in scalar_names):
            raise DurableJobError("readiness dimensions must be explicit booleans")
        if not isinstance(self.llm_members, tuple) or not self.llm_members:
            raise DurableJobError("readiness requires a nonempty immutable LLM member set")
        names: list[str] = []
        for item in self.llm_members:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or _TOKEN.fullmatch(item[0]) is None
                or not isinstance(item[1], bool)
            ):
                raise DurableJobError("LLM readiness members must be typed name/boolean pairs")
            names.append(item[0])
        if len(names) != len(set(names)):
            raise DurableJobError("LLM readiness member names must be unique")
        if not isinstance(self.required_for_field, tuple) or not self.required_for_field:
            raise DurableJobError("field readiness requires a nonempty immutable dependency set")
        if any(not isinstance(name, str) for name in self.required_for_field):
            raise DurableJobError("field readiness dependency names must be strings")
        if len(self.required_for_field) != len(set(self.required_for_field)):
            raise DurableJobError("field readiness dependencies must be unique")
        known = {name for name, _ready in self.dimensions()}
        if not set(self.required_for_field) <= known:
            raise DurableJobError("field readiness contains an unknown dependency")
        if not set(MANDATORY_EXTERNAL_FIELD_DEPENDENCIES) <= set(self.required_for_field):
            raise DurableJobError("field readiness cannot omit mandatory integrity dependencies")

    @classmethod
    def all_ready(
        cls,
        *,
        llm_members: tuple[str, ...],
        required_for_field: tuple[str, ...],
    ) -> ReadinessDependencySnapshot:
        if not isinstance(llm_members, tuple):
            raise DurableJobError("LLM member names must be an immutable tuple")
        return cls(
            True,
            True,
            True,
            True,
            True,
            True,
            tuple((name, True) for name in llm_members),
            True,
            True,
            True,
            True,
            True,
            True,
            required_for_field,
        )

    def dimensions(self) -> tuple[tuple[str, bool], ...]:
        return (
            ("event_integrity", self.event_integrity),
            ("projection_currency", self.projection_currency),
            ("blob_integrity", self.blob_integrity),
            ("pinned_bundle", self.pinned_bundle),
            ("formula", self.formula),
            ("ml", self.ml),
            *((f"llm:{name}", ready) for name, ready in self.llm_members),
            ("pool_degradation_mode", self.pool_degradation_mode),
            ("writer_latency", self.writer_latency),
            ("disk_reserve", self.disk_reserve),
            ("backup_age", self.backup_age),
            ("issue_recovery_path", self.issue_recovery_path),
            ("cloud_consent", self.cloud_consent),
        )

    def with_dimension(self, name: str, ready: bool) -> ReadinessDependencySnapshot:
        if not isinstance(ready, bool):
            raise DurableJobError("readiness override must be an explicit boolean")
        if name.startswith("llm:"):
            member = name.removeprefix("llm:")
            updated = tuple(
                (candidate, ready if candidate == member else current)
                for candidate, current in self.llm_members
            )
            if updated == self.llm_members and not any(
                candidate == member for candidate, _current in self.llm_members
            ):
                raise DurableJobError("unknown readiness dimension")
            return replace(self, llm_members=updated)
        if name not in {dimension for dimension, _ready in self.dimensions()}:
            raise DurableJobError("unknown readiness dimension")
        return replace(self, **{name: ready})


class ReadinessProbePort(Protocol):
    def __call__(self, observed_at: str) -> ReadinessDependencySnapshot: ...


class JobRepositoryPort(Protocol):
    """Durable scheduling port implemented by SQLite or another repository adapter."""

    def claim(self, lane: JobLane, **kwargs: Any) -> JobRecordPort | None: ...

    def record_failure(self, job_id: str, job_revision: int, **kwargs: Any) -> JobRecordPort: ...

    def mark_stale(self, job_id: str, job_revision: int, **kwargs: Any) -> JobRecordPort: ...

    def commit_success(self, job_id: str, job_revision: int, **kwargs: Any) -> JobRecordPort: ...

    def health(self, **kwargs: Any) -> QueueHealthPort: ...


class RollingJobRepositoryPort(Protocol):
    """Application boundary for durable rolling-card orchestration."""

    capacity: Any

    def verify(self) -> None: ...

    def recover_rolling_restart(self) -> RollingRestartReceipt: ...

    def recover_rolling_restart_deep_audit(self) -> RollingRestartReceipt: ...

    def refresh_rolling_restart_checkpoint_if_due(
        self,
        *,
        observed_at: str,
        delta_threshold: int = 48,
        max_elapsed_ms: int = 300_000,
    ) -> RollingRestartReceipt | None: ...

    def rolling_restart_suffix_status(self) -> RollingRestartSuffixStatus: ...

    def rebuild_job_projection(self) -> int: ...

    def enqueue_rolling_job(self, **values: Any) -> JobRecordPort: ...

    def records_for_card(self, card_digest: str) -> tuple[JobRecordPort, ...]: ...

    def current_rolling_card_key(
        self, competitor_id: str, target_context_digest: str
    ) -> dict[str, Any] | None: ...

    def rolling_card_keys_for_epoch(self, epoch_id: str) -> tuple[dict[str, Any], ...]: ...

    def rolling_epoch_closed(self, epoch_id: str) -> bool: ...

    def cancel_closed_rolling_jobs(self) -> tuple[JobRecordPort, ...]: ...

    def supersede_closed_rolling_publications(self) -> tuple[str, ...]: ...

    def install_rolling_council_authority(self, *args: Any, **kwargs: Any) -> str: ...

    def rolling_council_authority(self, digest: str) -> tuple[str, Any]: ...

    def rolling_publication_row(self, **lookup: Any) -> dict[str, Any] | None: ...

    def rolling_publication_rows(self) -> tuple[dict[str, Any], ...]: ...

    def rolling_current_rows(self) -> tuple[dict[str, Any], ...]: ...

    def verify_rolling_storage(self, *, allow_closed_current: bool = False) -> None: ...

    def rebuild_rolling_current_projection(self) -> int: ...

    def commit_rolling_publication(
        self,
        row: Mapping[str, Any],
        *,
        expected_jobs: tuple[JobRecordPort, ...],
        observed_at: str,
    ) -> dict[str, Any]: ...

    def supersede_rolling_publication(self, **values: Any) -> None: ...

    def close_rolling_epoch(self, epoch_id: str, event: Any) -> None: ...

    def pending_rolling_reactions(self, *, limit: int) -> tuple[dict[str, Any], ...]: ...

    def complete_rolling_reaction(
        self,
        reaction_id: str,
        *,
        plan_digest: str,
        completed_at: str,
    ) -> None: ...

    def cancel(self, job_id: str, job_revision: int, **values: Any) -> JobRecordPort: ...

    def get(self, job_id: str, job_revision: int) -> JobRecordPort: ...


class PublicationPort(Protocol):
    """Application publication callback; no database connection is exposed."""

    def __call__(self, job: JobRecordPort, response: Any) -> None: ...


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DurableJobError(f"{label} must be a positive integer")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DurableJobError(f"{label} must be a non-negative integer")
    return value


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DurableJobError(f"{label} must be a lower-case SHA-256 digest")
    return value


def _canonical_json(value: object, digest: object, label: str) -> None:
    _digest(digest, f"{label} digest")
    if not isinstance(value, str):
        raise DurableJobError(f"{label} must be canonical JSON")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise DurableJobError(f"{label} must be canonical JSON") from exc
    if canonical_bytes(decoded).decode("utf-8") != value or canonical_digest(decoded) != digest:
        raise DurableJobError(f"{label} canonical identity differs")


__all__ = [
    "DurableJobError",
    "FailureKind",
    "JobAdmissionRejected",
    "JobConflict",
    "JobDeadlineExceeded",
    "JobRecordPort",
    "JobRepositoryPort",
    "MANDATORY_EXTERNAL_FIELD_DEPENDENCIES",
    "PublicationPort",
    "ProviderAttemptAudit",
    "ProviderExecutionAudit",
    "ProviderStorageAudit",
    "QueueHealthPort",
    "ReadinessDependencySnapshot",
    "ReadinessProbePort",
    "RetryPolicy",
    "RollingRestartReceipt",
    "RollingRestartSuffixStatus",
    "RollingRestartExpectedHead",
    "RollingRestartTrust",
    "RollingRestartTrustMode",
]
