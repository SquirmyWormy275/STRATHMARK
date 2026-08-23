"""Application-owned contracts for durable scheduling and publication."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol

from strathmark.v3.application.capacity import JobLane

_TOKEN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
MANDATORY_EXTERNAL_FIELD_DEPENDENCIES = (
    "event_integrity",
    "projection_currency",
    "writer_latency",
    "disk_reserve",
    "issue_recovery_path",
)


class DurableJobError(RuntimeError):
    """Base durable-job failure shared by application and repository adapters."""


class JobAdmissionRejected(DurableJobError):
    """Capacity or deadline policy rejected work before dispatch."""


class JobConflict(DurableJobError):
    """An identity, revision, state, or fencing precondition conflicted."""


class JobDeadlineExceeded(JobConflict):
    """A lease or hard deadline has elapsed."""


class FailureKind(str, Enum):
    SCHEMA = "schema"
    TRANSPORT = "transport"
    PROCESS = "process"
    VALIDATION = "validation"
    PERMANENT = "permanent"


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
    "QueueHealthPort",
    "ReadinessDependencySnapshot",
    "ReadinessProbePort",
    "RetryPolicy",
]
