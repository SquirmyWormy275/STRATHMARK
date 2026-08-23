"""Durable V3 work queue with bounded lanes and monotonic fencing leases."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from strathmark.v3.application.capacity import (
    CapacityManifest,
    CapacityUse,
    JobKind,
    JobLane,
    JobPriority,
    JobResourceClass,
    QueueLoad,
    decide_admission,
    validate_capacity_use,
)
from strathmark.v3.application.job_ports import (
    DurableJobError,
    FailureKind,
    JobAdmissionRejected,
    JobConflict,
    JobDeadlineExceeded,
    ReadinessDependencySnapshot,
    ReadinessProbePort,
    RetryPolicy,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    require_idempotency_key,
    require_identifier,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import immediate_transaction, open_v3_connection
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection

JOB_RESULT_SCHEMA_VERSION = "strathmark-v3-durable-job-v1"
ZERO_DIGEST = "0" * 64
MAX_JOB_PAYLOAD_BYTES = 1_048_576
MANDATORY_REPOSITORY_FIELD_DEPENDENCIES = (
    "durable_store_integrity",
    "queue_within_capacity",
    "hot_field_capacity",
    "recovery_capacity",
    "deadline_safe",
)
_TOKEN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class JobState(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    INVALID = "invalid"
    STALE = "stale"
    CANCELLED = "cancelled"
    RETRYABLE_FAILED = "retryable-failed"
    PERMANENT_FAILED = "permanent-failed"


@dataclass(frozen=True, slots=True)
class JobRequest:
    job_id: StableIdentifier
    job_revision: int
    idempotency_key: IdempotencyKey
    job_kind: JobKind
    lane: JobLane
    resource_class: JobResourceClass
    priority: JobPriority
    capacity_use_json: str
    payload_json: str
    payload_digest: str
    evidence_digest: str
    bundle_digest: str
    retry_policy_version: str
    created_at: str
    not_before_at: str
    hard_deadline_at: str
    max_attempts: int

    def __post_init__(self) -> None:
        require_identifier(self.job_id, expected_namespace="job")
        _positive(self.job_revision, "job revision")
        if not isinstance(self.idempotency_key, IdempotencyKey):
            raise DurableJobError("idempotency key must be typed")
        if not isinstance(self.job_kind, JobKind):
            raise DurableJobError("job kind must be a closed JobKind")
        if not isinstance(self.lane, JobLane) or not isinstance(self.priority, JobPriority):
            raise DurableJobError("job lane and priority must be typed")
        if not isinstance(self.resource_class, JobResourceClass):
            raise DurableJobError("job resource class must be typed")
        if (
            self.lane is not self.job_kind.lane
            or self.resource_class is not self.job_kind.resource_class
        ):
            raise DurableJobError("job kind, lane, and resource class mapping differs")
        try:
            capacity_use = json.loads(self.capacity_use_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableJobError("capacity use must be canonical JSON") from exc
        try:
            decoded_use = CapacityUse.from_dict(capacity_use)
        except (TypeError, ValueError) as exc:
            raise DurableJobError("capacity use is invalid") from exc
        if canonical_bytes(decoded_use.to_dict()).decode("utf-8") != self.capacity_use_json:
            raise DurableJobError("capacity use must be canonical JSON")
        try:
            payload = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableJobError("job payload must be canonical JSON") from exc
        if not isinstance(payload, dict):
            raise DurableJobError("job payload must be a JSON object")
        encoded = canonical_bytes(payload, max_bytes=MAX_JOB_PAYLOAD_BYTES).decode("utf-8")
        if encoded != self.payload_json or canonical_digest(payload) != self.payload_digest:
            raise DurableJobError("job payload bytes or digest differ")
        _digest(self.evidence_digest, "evidence digest")
        _digest(self.bundle_digest, "bundle digest")
        _require_token(self.retry_policy_version, "retry policy version")
        created = require_utc_milliseconds(self.created_at)
        not_before = require_utc_milliseconds(self.not_before_at)
        deadline = require_utc_milliseconds(self.hard_deadline_at)
        if not created <= not_before < deadline:
            raise DurableJobError("job timing must satisfy created <= not-before < deadline")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise DurableJobError("max attempts must be an integer")
        if not 1 <= self.max_attempts <= 32:
            raise DurableJobError("max attempts must be between 1 and 32")

    @classmethod
    def create(
        cls,
        *,
        job_id: str | StableIdentifier,
        job_revision: int,
        idempotency_key: str | IdempotencyKey,
        job_kind: JobKind,
        lane: JobLane,
        priority: JobPriority,
        capacity_use: CapacityUse,
        payload: Mapping[str, Any],
        evidence_digest: str,
        bundle_digest: str,
        retry_policy_version: str,
        created_at: str,
        not_before_at: str,
        hard_deadline_at: str,
        max_attempts: int,
    ) -> JobRequest:
        if not isinstance(payload, Mapping):
            raise DurableJobError("job payload must be a mapping")
        if not isinstance(job_kind, JobKind) or not isinstance(capacity_use, CapacityUse):
            raise DurableJobError("job creation requires typed kind and capacity use")
        encoded = canonical_bytes(payload, max_bytes=MAX_JOB_PAYLOAD_BYTES)
        return cls(
            require_identifier(job_id, expected_namespace="job"),
            job_revision,
            require_idempotency_key(idempotency_key),
            job_kind,
            lane,
            job_kind.resource_class,
            priority,
            canonical_bytes(capacity_use.to_dict()).decode("utf-8"),
            encoded.decode("utf-8"),
            canonical_digest(payload),
            evidence_digest,
            bundle_digest,
            retry_policy_version,
            created_at,
            not_before_at,
            hard_deadline_at,
            max_attempts,
        )

    @property
    def material_digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": JOB_RESULT_SCHEMA_VERSION,
                "job_id": str(self.job_id),
                "job_revision": self.job_revision,
                "idempotency_key": str(self.idempotency_key),
                "job_kind": self.job_kind.value,
                "lane": self.lane.value,
                "resource_class": self.resource_class.value,
                "priority": int(self.priority),
                "capacity_use": self.capacity_use().to_dict(),
                "payload_digest": self.payload_digest,
                "evidence_digest": self.evidence_digest,
                "bundle_digest": self.bundle_digest,
                "retry_policy_version": self.retry_policy_version,
                "created_at": self.created_at,
                "not_before_at": self.not_before_at,
                "hard_deadline_at": self.hard_deadline_at,
                "max_attempts": self.max_attempts,
            }
        )

    def capacity_use(self) -> CapacityUse:
        value = json.loads(self.capacity_use_json)
        if not isinstance(value, dict):
            raise DurableJobError("capacity use is not an object")
        return CapacityUse.from_dict(value)


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    job_revision: int
    idempotency_key: str
    job_kind: JobKind
    lane: JobLane
    resource_class: JobResourceClass
    priority: JobPriority
    capacity_use_json: str
    payload_json: str
    payload_digest: str
    evidence_digest: str
    bundle_digest: str
    retry_policy_version: str
    state: JobState
    attempt_count: int
    max_attempts: int
    initial_not_before_at: str
    not_before_at: str | None
    hard_deadline_at: str
    lease_owner: str | None
    lease_acquired_at: str | None
    lease_expires_at: str | None
    fencing_token: int
    terminal_reason: str | None
    result_digest: str | None
    created_at: str
    updated_at: str

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise DurableJobError("persisted job payload is not an object")
        return value

    def capacity_use(self) -> CapacityUse:
        value = json.loads(self.capacity_use_json)
        if not isinstance(value, dict):
            raise DurableJobError("persisted capacity use is not an object")
        return CapacityUse.from_dict(value)


@dataclass(frozen=True, slots=True)
class QueueHealth:
    observed_at: str
    depth_by_lane: tuple[tuple[str, int], ...]
    leased_by_lane: tuple[tuple[str, int], ...]
    oldest_job_at: str | None
    deadline_risk_count: int
    capacity_manifest_digest: str
    effective_expired_leases: int
    dependency_readiness: tuple[tuple[str, bool], ...]
    required_field_dependencies: tuple[str, ...]
    field_ready: bool


PublishHook = Callable[[sqlite3.Connection, JobRecord], None]
ContextHook = Callable[[sqlite3.Connection, JobRecord], tuple[str, str]]
ClockHook = Callable[[], str]


class DurableJobRepository:
    """One-operation-per-connection durable queue.

    Provider/model/network work is intentionally absent.  The only publication hook runs
    inside the same short writer transaction that fences and succeeds the current attempt.
    """

    def __init__(
        self,
        database_path: Path | str,
        *,
        capacity: CapacityManifest,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
    ) -> None:
        if isinstance(database_path, bool) or not isinstance(database_path, (Path, str)):
            raise DurableJobError("database path must be a filesystem path")
        if not isinstance(capacity, CapacityManifest):
            raise DurableJobError("durable jobs require a CapacityManifest")
        if not callable(getattr(signer, "sign", None)) or not hasattr(signer, "identity"):
            raise DurableJobError("durable jobs require a typed external signer")
        if not isinstance(trust_store, IntegrityTrustStore):
            raise DurableJobError("durable jobs require a typed external trust store")
        trust_store.identity(signer.identity.key_id)
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.capacity = capacity
        self._signer = signer
        self._trust_store = trust_store
        with open_v3_connection(self.database_path) as connection:
            migrate_connection(connection)
            self._verify_connection(connection)

    def enqueue(self, request: JobRequest, *, maintenance_suspended: bool = False) -> JobRecord:
        if not isinstance(request, JobRequest):
            raise DurableJobError("enqueue requires a JobRequest")
        if not isinstance(maintenance_suspended, bool):
            raise DurableJobError("maintenance_suspended must be an explicit boolean")
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                existing = connection.execute(
                    "SELECT * FROM v3_jobs WHERE idempotency_key=?",
                    (str(request.idempotency_key),),
                ).fetchone()
                if existing is not None:
                    record = _decode(existing)
                    if _record_material_digest(record) != request.material_digest:
                        raise JobConflict("idempotency key already binds different job material")
                    return record
                if (
                    connection.execute(
                        "SELECT 1 FROM v3_jobs WHERE job_id=? AND job_revision=?",
                        (str(request.job_id), request.job_revision),
                    ).fetchone()
                    is not None
                ):
                    raise JobConflict("job revision already exists under another idempotency key")
                load = self._load(connection, request.lane)
                operational = validate_capacity_use(self.capacity, request.capacity_use())
                if not operational.admitted:
                    raise JobAdmissionRejected(operational.reason)
                decision = decide_admission(
                    self.capacity,
                    request.lane,
                    request.priority,
                    load,
                    maintenance_suspended=maintenance_suspended,
                )
                if not decision.admitted:
                    raise JobAdmissionRejected(decision.reason)
                connection.execute(
                    "INSERT INTO v3_jobs(job_id, job_revision, idempotency_key, job_kind, lane, "
                    "resource_class, base_priority, capacity_use_json, payload_json, payload_digest, evidence_digest, bundle_digest, "
                    "retry_policy_version, state, attempt_count, max_attempts, initial_not_before_at, not_before_at, "
                    "hard_deadline_at, lease_owner, lease_acquired_at, lease_expires_at, "
                    "fencing_token, terminal_reason, result_digest, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, NULL, NULL, "
                    "NULL, 0, NULL, NULL, ?, ?)",
                    (
                        str(request.job_id),
                        request.job_revision,
                        str(request.idempotency_key),
                        request.job_kind.value,
                        request.lane.value,
                        request.resource_class.value,
                        int(request.priority),
                        request.capacity_use_json,
                        request.payload_json,
                        request.payload_digest,
                        request.evidence_digest,
                        request.bundle_digest,
                        request.retry_policy_version,
                        request.max_attempts,
                        request.not_before_at,
                        request.not_before_at,
                        request.hard_deadline_at,
                        request.created_at,
                        request.created_at,
                    ),
                )
                record = self._get_connection(connection, str(request.job_id), request.job_revision)
                self._append_history(connection, "queued", None, record, request.created_at)
                return record

    def get(self, job_id: str, job_revision: int) -> JobRecord:
        _job_identity(job_id, job_revision)
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM v3_jobs WHERE job_id=? AND job_revision=?",
                (job_id, job_revision),
            ).fetchone()
            if row is None:
                raise KeyError((job_id, job_revision))
            return _decode(row)

    def claim(
        self,
        lane: JobLane,
        *,
        worker_id: str,
        clock: ClockHook,
        lease_duration_ms: int,
    ) -> JobRecord | None:
        if not isinstance(lane, JobLane):
            raise DurableJobError("claim lane must be a JobLane")
        _require_token(worker_id, "worker id")
        if not callable(clock):
            raise DurableJobError("claim requires a trusted clock port")
        _bounded_duration(lease_duration_ms)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                now = require_utc_milliseconds(clock())
                self._reconcile_for_claim(connection, now)
                load = self._load(connection, lane)
                if not decide_admission(
                    self.capacity,
                    lane,
                    JobPriority.IMMINENT_FIELD,
                    load,
                    for_claim=True,
                ).admitted:
                    return None
                rows = connection.execute(
                    "SELECT * FROM v3_jobs WHERE lane=? AND state='queued' "
                    "AND not_before_at<=? AND hard_deadline_at>?",
                    (lane.value, now, now),
                ).fetchall()
                if not rows:
                    return None
                records = tuple(_decode(row) for row in rows)
                gpu_busy = bool(
                    connection.execute(
                        "SELECT 1 FROM v3_jobs WHERE state='leased' AND resource_class=? LIMIT 1",
                        (JobResourceClass.LOCAL_GPU.value,),
                    ).fetchone()
                )
                eligible = tuple(
                    item
                    for item in records
                    if not (gpu_busy and item.resource_class is JobResourceClass.LOCAL_GPU)
                )
                if not eligible:
                    return None
                record = min(
                    eligible,
                    key=lambda item: (
                        -self._effective_priority(item, now),
                        item.hard_deadline_at,
                        item.created_at,
                        item.job_id,
                        item.job_revision,
                    ),
                )
                expiry = min(_add_milliseconds(now, lease_duration_ms), record.hard_deadline_at)
                connection.execute(
                    "UPDATE v3_jobs SET state='leased', attempt_count=attempt_count+1, "
                    "fencing_token=fencing_token+1, not_before_at=NULL, lease_owner=?, "
                    "lease_acquired_at=?, lease_expires_at=?, terminal_reason=NULL, updated_at=? "
                    "WHERE job_id=? AND job_revision=? AND state='queued'",
                    (
                        worker_id,
                        now,
                        expiry,
                        now,
                        record.job_id,
                        record.job_revision,
                    ),
                )
                leased = self._get_connection(connection, record.job_id, record.job_revision)
                self._append_history(connection, "leased", record.state, leased, now)
                return leased

    def heartbeat(
        self,
        job_id: str,
        job_revision: int,
        *,
        worker_id: str,
        fencing_token: int,
        observed_at: str,
        extend_ms: int,
    ) -> JobRecord:
        now = require_utc_milliseconds(observed_at)
        _bounded_duration(extend_ms)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                current = self._require_lease(
                    connection, job_id, job_revision, worker_id, fencing_token, now
                )
                expiry = min(_add_milliseconds(now, extend_ms), current.hard_deadline_at)
                connection.execute(
                    "UPDATE v3_jobs SET lease_expires_at=?, updated_at=? "
                    "WHERE job_id=? AND job_revision=?",
                    (expiry, now, job_id, job_revision),
                )
                result = self._get_connection(connection, job_id, job_revision)
                self._append_history(connection, "heartbeat", current.state, result, now)
                return result

    def commit_success(
        self,
        job_id: str,
        job_revision: int,
        *,
        worker_id: str,
        fencing_token: int,
        result_digest: str,
        current_context: ContextHook,
        clock: ClockHook,
        publish: PublishHook | None = None,
    ) -> JobRecord:
        _digest(result_digest, "result digest")
        if not callable(current_context) or not callable(clock):
            raise DurableJobError("commit requires callable context and trusted clock ports")
        if publish is not None and not callable(publish):
            raise DurableJobError("publish hook must be callable")
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                current = self._get_connection(connection, job_id, job_revision)
                if current.state is JobState.SUCCEEDED:
                    if (
                        current.fencing_token == fencing_token
                        and current.result_digest == result_digest
                    ):
                        return current
                    raise JobConflict("job revision already has a different publication")
                context = current_context(connection, current)
                if not isinstance(context, tuple) or len(context) != 2:
                    raise DurableJobError("current context port must return two digests")
                current_evidence_digest = _digest(context[0], "current evidence digest")
                current_bundle_digest = _digest(context[1], "current bundle digest")
                now = require_utc_milliseconds(clock())
                current = self._require_lease(
                    connection, job_id, job_revision, worker_id, fencing_token, now
                )
                if (
                    current.evidence_digest != current_evidence_digest
                    or current.bundle_digest != current_bundle_digest
                ):
                    reason = (
                        "evidence_changed"
                        if current.evidence_digest != current_evidence_digest
                        else "bundle_changed"
                    )
                    return self._finish(connection, current, JobState.STALE, now, reason=reason)
                connection.execute(
                    "INSERT INTO v3_job_publications(job_id, job_revision, fencing_token, "
                    "result_digest, published_at, auth_body_json, auth_body_digest, auth_key_id, "
                    "auth_signature_der_b64) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._publication_values(
                        connection, current, result_digest=result_digest, published_at=now
                    ),
                )
                if publish is not None:
                    publish(connection, current)
                connection.execute(
                    "UPDATE v3_jobs SET state='succeeded', lease_owner=NULL, "
                    "lease_acquired_at=NULL, lease_expires_at=NULL, terminal_reason=NULL, "
                    "result_digest=?, updated_at=? WHERE job_id=? AND job_revision=?",
                    (result_digest, now, job_id, job_revision),
                )
                result = self._get_connection(connection, job_id, job_revision)
                self._append_history(connection, "succeeded", current.state, result, now)
                return result

    def record_failure(
        self,
        job_id: str,
        job_revision: int,
        *,
        worker_id: str,
        fencing_token: int,
        observed_at: str,
        failure_kind: FailureKind,
        reason: str,
        policy: RetryPolicy,
    ) -> JobRecord:
        if not isinstance(failure_kind, FailureKind) or not isinstance(policy, RetryPolicy):
            raise DurableJobError("failure handling requires typed kind and retry policy")
        _require_reason(reason)
        now = require_utc_milliseconds(observed_at)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                current = self._require_lease(
                    connection, job_id, job_revision, worker_id, fencing_token, now
                )
                if current.retry_policy_version != policy.version:
                    raise JobConflict("job retry policy version differs from worker policy")
                if failure_kind is FailureKind.VALIDATION:
                    return self._finish(connection, current, JobState.INVALID, now, reason=reason)
                if failure_kind is FailureKind.PERMANENT:
                    return self._finish(
                        connection, current, JobState.PERMANENT_FAILED, now, reason=reason
                    )
                limit = {
                    FailureKind.SCHEMA: policy.schema_retry_limit + 1,
                    FailureKind.TRANSPORT: policy.transport_attempt_limit,
                    FailureKind.PROCESS: policy.process_attempt_limit,
                }[failure_kind]
                if current.attempt_count >= min(current.max_attempts, limit):
                    return self._finish(
                        connection,
                        current,
                        JobState.PERMANENT_FAILED,
                        now,
                        reason="retry_exhausted",
                    )
                delay = self._retry_delay(current, failure_kind, reason, policy)
                not_before = _add_milliseconds(now, delay)
                if not_before >= current.hard_deadline_at:
                    return self._finish(
                        connection,
                        current,
                        JobState.PERMANENT_FAILED,
                        now,
                        reason="deadline_exceeded",
                    )
                connection.execute(
                    "UPDATE v3_jobs SET state='retryable-failed', not_before_at=?, "
                    "lease_owner=NULL, lease_acquired_at=NULL, lease_expires_at=NULL, "
                    "terminal_reason=?, updated_at=? WHERE job_id=? AND job_revision=?",
                    (not_before, reason, now, job_id, job_revision),
                )
                result = self._get_connection(connection, job_id, job_revision)
                self._append_history(connection, "retryable-failed", current.state, result, now)
                return result

    def mark_invalid(self, *args: Any, reason: str, **kwargs: Any) -> JobRecord:
        return self._terminal_from_lease(*args, target=JobState.INVALID, reason=reason, **kwargs)

    def mark_stale(self, *args: Any, reason: str, **kwargs: Any) -> JobRecord:
        return self._terminal_from_lease(*args, target=JobState.STALE, reason=reason, **kwargs)

    def mark_permanent_failure(self, *args: Any, reason: str, **kwargs: Any) -> JobRecord:
        return self._terminal_from_lease(
            *args, target=JobState.PERMANENT_FAILED, reason=reason, **kwargs
        )

    def cancel(self, job_id: str, job_revision: int, *, observed_at: str, reason: str) -> JobRecord:
        _require_reason(reason)
        now = require_utc_milliseconds(observed_at)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                current = self._get_connection(connection, job_id, job_revision)
                if current.state not in {
                    JobState.QUEUED,
                    JobState.LEASED,
                    JobState.RETRYABLE_FAILED,
                }:
                    raise JobConflict("only active work may be cancelled")
                connection.execute(
                    "UPDATE v3_jobs SET state='cancelled', not_before_at=NULL, "
                    "lease_owner=NULL, lease_acquired_at=NULL, lease_expires_at=NULL, "
                    "fencing_token=fencing_token+1, terminal_reason=?, updated_at=? "
                    "WHERE job_id=? AND job_revision=?",
                    (reason, now, job_id, job_revision),
                )
                result = self._get_connection(connection, job_id, job_revision)
                self._append_history(connection, "cancelled", current.state, result, now)
                return result

    def health(
        self,
        *,
        observed_at: str,
        dependency_probe: ReadinessProbePort,
        deadline_risk_window_ms: int = 120_000,
    ) -> QueueHealth:
        now = require_utc_milliseconds(observed_at)
        if not callable(dependency_probe):
            raise DurableJobError("health requires a request-scoped dependency probe")
        external = dependency_probe(now)
        if not isinstance(external, ReadinessDependencySnapshot):
            raise DurableJobError("dependency probe must return a typed readiness snapshot")
        _positive(deadline_risk_window_ms, "deadline risk window")
        risk_at = _add_milliseconds(now, deadline_risk_window_ms)
        integrity_ready = True
        active: list[JobRecord] = []
        effective_expired = 0
        try:
            with open_v3_connection(self.database_path, read_only=True) as connection:
                self._verify_connection(connection)
                rows = tuple(
                    _decode(row)
                    for row in connection.execute(
                        "SELECT * FROM v3_jobs "
                        "WHERE state IN ('queued','leased','retryable-failed')"
                    )
                )
                for record in rows:
                    expired_lease = (
                        record.state is JobState.LEASED
                        and record.lease_expires_at is not None
                        and record.lease_expires_at <= now
                    )
                    if expired_lease:
                        effective_expired += 1
                    exhausted = record.attempt_count >= record.max_attempts
                    if record.hard_deadline_at <= now or (expired_lease and exhausted):
                        continue
                    active.append(record)
        except Exception:
            integrity_ready = False
            active = []
            effective_expired = 0
        depths = tuple((lane.value, sum(item.lane is lane for item in active)) for lane in JobLane)
        leased = tuple(
            (
                lane.value,
                sum(
                    item.lane is lane
                    and item.state is JobState.LEASED
                    and item.lease_expires_at is not None
                    and item.lease_expires_at > now
                    for item in active
                ),
            )
            for lane in JobLane
        )
        oldest = min((item.created_at for item in active), default=None)
        risk = sum(item.hard_deadline_at <= risk_at for item in active)
        depth_map = dict(depths)
        leased_map = dict(leased)
        dependencies = (
            ("durable_store_integrity", integrity_ready),
            *external.dimensions(),
            (
                "queue_within_capacity",
                integrity_ready and len(active) <= self.capacity.max_queued_jobs,
            ),
            (
                "hot_field_capacity",
                integrity_ready
                and leased_map[JobLane.HOT_FIELD.value]
                < self.capacity.lane(JobLane.HOT_FIELD).max_leased,
            ),
            (
                "recovery_capacity",
                integrity_ready
                and depth_map[JobLane.LOOKUP_RECOVERY.value]
                < self.capacity.lane(JobLane.LOOKUP_RECOVERY).max_queued,
            ),
            ("deadline_safe", integrity_ready and risk == 0),
        )
        required = tuple(
            dict.fromkeys((*MANDATORY_REPOSITORY_FIELD_DEPENDENCIES, *external.required_for_field))
        )
        readiness = dict(dependencies)
        return QueueHealth(
            now,
            depths,
            leased,
            oldest,
            risk,
            self.capacity.digest,
            effective_expired,
            dependencies,
            required,
            all(readiness[name] for name in required),
        )

    def verify(self) -> None:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            self._verify_connection(connection)

    def _terminal_from_lease(
        self,
        job_id: str,
        job_revision: int,
        *,
        worker_id: str,
        fencing_token: int,
        observed_at: str,
        target: JobState,
        reason: str,
    ) -> JobRecord:
        _require_reason(reason)
        now = require_utc_milliseconds(observed_at)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                current = self._require_lease(
                    connection, job_id, job_revision, worker_id, fencing_token, now
                )
                return self._finish(connection, current, target, now, reason=reason)

    def _finish(
        self,
        connection: sqlite3.Connection,
        current: JobRecord,
        target: JobState,
        observed_at: str,
        *,
        reason: str,
    ) -> JobRecord:
        if target not in {JobState.INVALID, JobState.STALE, JobState.PERMANENT_FAILED}:
            raise DurableJobError("finish target must be a non-success terminal state")
        connection.execute(
            "UPDATE v3_jobs SET state=?, not_before_at=NULL, lease_owner=NULL, "
            "lease_acquired_at=NULL, lease_expires_at=NULL, terminal_reason=?, updated_at=? "
            "WHERE job_id=? AND job_revision=?",
            (target.value, reason, observed_at, current.job_id, current.job_revision),
        )
        result = self._get_connection(connection, current.job_id, current.job_revision)
        self._append_history(connection, target.value, current.state, result, observed_at)
        return result

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        job_revision: int,
        worker_id: str,
        fencing_token: int,
        observed_at: str,
    ) -> JobRecord:
        _job_identity(job_id, job_revision)
        _require_token(worker_id, "worker id")
        _positive(fencing_token, "fencing token")
        current = self._get_connection(connection, job_id, job_revision)
        if (
            current.state is not JobState.LEASED
            or current.lease_owner != worker_id
            or current.fencing_token != fencing_token
        ):
            raise JobConflict("worker does not hold the current fencing lease")
        assert current.lease_expires_at is not None
        if observed_at >= current.lease_expires_at or observed_at >= current.hard_deadline_at:
            raise JobDeadlineExceeded("lease or hard deadline expired before commit")
        return current

    def _reconcile_for_claim(self, connection: sqlite3.Connection, observed_at: str) -> None:
        rows = connection.execute(
            "SELECT * FROM v3_jobs WHERE state='leased' AND lease_expires_at<=?",
            (observed_at,),
        ).fetchall()
        for row in rows:
            current = _decode(row)
            exhausted = (
                current.attempt_count >= current.max_attempts
                or observed_at >= current.hard_deadline_at
            )
            if exhausted:
                reason = (
                    "deadline_exceeded"
                    if observed_at >= current.hard_deadline_at
                    else "lease_expired_attempts_exhausted"
                )
                self._finish(
                    connection,
                    current,
                    JobState.PERMANENT_FAILED,
                    observed_at,
                    reason=reason,
                )
            else:
                connection.execute(
                    "UPDATE v3_jobs SET state='queued', not_before_at=?, lease_owner=NULL, "
                    "lease_acquired_at=NULL, lease_expires_at=NULL, terminal_reason='lease_expired', "
                    "updated_at=? WHERE job_id=? AND job_revision=?",
                    (observed_at, observed_at, current.job_id, current.job_revision),
                )
                result = self._get_connection(connection, current.job_id, current.job_revision)
                self._append_history(
                    connection, "lease_expired", current.state, result, observed_at
                )
        retry_rows = connection.execute(
            "SELECT * FROM v3_jobs WHERE state='retryable-failed' AND not_before_at<=?",
            (observed_at,),
        ).fetchall()
        for row in retry_rows:
            current = _decode(row)
            if (
                current.attempt_count >= current.max_attempts
                or observed_at >= current.hard_deadline_at
            ):
                reason = (
                    "deadline_exceeded"
                    if observed_at >= current.hard_deadline_at
                    else "retry_exhausted"
                )
                self._finish(
                    connection,
                    current,
                    JobState.PERMANENT_FAILED,
                    observed_at,
                    reason=reason,
                )
            else:
                connection.execute(
                    "UPDATE v3_jobs SET state='queued', terminal_reason=NULL, updated_at=? "
                    "WHERE job_id=? AND job_revision=?",
                    (observed_at, current.job_id, current.job_revision),
                )
                result = self._get_connection(connection, current.job_id, current.job_revision)
                self._append_history(connection, "requeued", current.state, result, observed_at)
        expired_rows = connection.execute(
            "SELECT * FROM v3_jobs WHERE state='queued' AND hard_deadline_at<=?",
            (observed_at,),
        ).fetchall()
        for row in expired_rows:
            current = _decode(row)
            self._finish(
                connection,
                current,
                JobState.PERMANENT_FAILED,
                observed_at,
                reason="deadline_exceeded",
            )

    def _load(self, connection: sqlite3.Connection, lane: JobLane) -> QueueLoad:
        active = "('queued','leased','retryable-failed')"
        total = int(
            connection.execute(f"SELECT COUNT(*) FROM v3_jobs WHERE state IN {active}").fetchone()[
                0
            ]
        )
        lane_active = int(
            connection.execute(
                f"SELECT COUNT(*) FROM v3_jobs WHERE lane=? AND state IN {active}",
                (lane.value,),
            ).fetchone()[0]
        )
        lane_leased = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_jobs WHERE lane=? AND state='leased'", (lane.value,)
            ).fetchone()[0]
        )
        return QueueLoad(total, lane_active, lane_leased)

    def _effective_priority(self, record: JobRecord, observed_at: str) -> int:
        age = max(0, _milliseconds(observed_at) - _milliseconds(record.created_at))
        increments = age // self.capacity.aging_interval_ms
        within_class = min(99, increments * self.capacity.aging_increment)
        return int(record.priority) + within_class

    @staticmethod
    def _retry_delay(
        record: JobRecord, failure_kind: FailureKind, reason: str, policy: RetryPolicy
    ) -> int:
        exponential = policy.base_delay_ms * (2 ** min(record.attempt_count - 1, 20))
        bounded = min(policy.maximum_delay_ms, exponential)
        jitter_window = max(1, bounded // 4)
        material = f"{record.job_id}:{record.job_revision}:{record.attempt_count}:"
        material += f"{failure_kind.value}:{reason}"
        jitter = int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:8], 16) % jitter_window
        return min(policy.maximum_delay_ms, bounded + jitter)

    def _append_history(
        self,
        connection: sqlite3.Connection,
        operation_kind: str,
        from_state: JobState | None,
        result: JobRecord,
        observed_at: str,
    ) -> None:
        row = connection.execute(
            "SELECT history_sequence, history_digest FROM v3_job_history "
            "ORDER BY history_sequence DESC LIMIT 1"
        ).fetchone()
        sequence = 1 if row is None else int(row[0]) + 1
        prior = ZERO_DIGEST if row is None else str(row[1])
        value = {
            "schema_version": JOB_RESULT_SCHEMA_VERSION,
            "history_sequence": sequence,
            "job_id": result.job_id,
            "job_revision": result.job_revision,
            "operation_kind": operation_kind,
            "from_state": None if from_state is None else from_state.value,
            "result_state": result.state.value,
            "attempt_count": result.attempt_count,
            "fencing_token": result.fencing_token,
            "lease_owner": result.lease_owner,
            "lease_acquired_at": result.lease_acquired_at,
            "lease_expires_at": result.lease_expires_at,
            "not_before_at": result.not_before_at,
            "terminal_reason": result.terminal_reason,
            "result_digest": result.result_digest,
            "observed_at": observed_at,
            "prior_history_digest": prior,
            "job_material_digest": _record_material_digest(result),
        }
        digest = canonical_digest(value)
        transition_id = f"job_transition:{digest}"
        authority = sign_manifest(
            "job_transition", value, signer=self._signer, created_at=observed_at
        )
        connection.execute(
            "INSERT INTO v3_job_history(history_sequence, transition_id, job_id, job_revision, "
            "operation_kind, from_state, result_state, attempt_count, fencing_token, lease_owner, "
            "lease_acquired_at, lease_expires_at, not_before_at, terminal_reason, result_digest, observed_at, "
            "prior_history_digest, history_digest, job_material_digest, auth_body_json, "
            "auth_body_digest, auth_key_id, auth_signature_der_b64) VALUES (?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sequence,
                transition_id,
                result.job_id,
                result.job_revision,
                operation_kind,
                value["from_state"],
                result.state.value,
                result.attempt_count,
                result.fencing_token,
                result.lease_owner,
                result.lease_acquired_at,
                result.lease_expires_at,
                result.not_before_at,
                result.terminal_reason,
                result.result_digest,
                observed_at,
                prior,
                digest,
                value["job_material_digest"],
                authority.body_json,
                authority.body_digest,
                authority.key_id,
                authority.signature_der_b64,
            ),
        )

    def _publication_values(
        self,
        connection: sqlite3.Connection,
        current: JobRecord,
        *,
        result_digest: str,
        published_at: str,
    ) -> tuple[Any, ...]:
        tip_row = connection.execute(
            "SELECT history_digest FROM v3_job_history ORDER BY history_sequence DESC LIMIT 1"
        ).fetchone()
        authority_tip = ZERO_DIGEST if tip_row is None else str(tip_row[0])
        payload = {
            "schema_version": JOB_RESULT_SCHEMA_VERSION,
            "job_id": current.job_id,
            "job_revision": current.job_revision,
            "fencing_token": current.fencing_token,
            "result_digest": result_digest,
            "published_at": published_at,
            "authority_tip": authority_tip,
            "job_material_digest": _record_material_digest(current),
        }
        authority = sign_manifest(
            "job_publication", payload, signer=self._signer, created_at=published_at
        )
        return (
            current.job_id,
            current.job_revision,
            current.fencing_token,
            result_digest,
            published_at,
            authority.body_json,
            authority.body_digest,
            authority.key_id,
            authority.signature_der_b64,
        )

    def _verify_connection(self, connection: sqlite3.Connection) -> None:
        jobs = {
            (str(row["job_id"]), int(row["job_revision"])): _decode(row)
            for row in connection.execute("SELECT * FROM v3_jobs")
        }
        for record in jobs.values():
            payload = record.payload()
            capacity_use = record.capacity_use()
            if (
                canonical_bytes(payload, max_bytes=MAX_JOB_PAYLOAD_BYTES).decode("utf-8")
                != record.payload_json
                or canonical_digest(payload) != record.payload_digest
                or canonical_bytes(capacity_use.to_dict()).decode("utf-8")
                != record.capacity_use_json
            ):
                raise DurableJobError("persisted job payload is not canonical")
            if (
                record.lane is not record.job_kind.lane
                or record.resource_class is not record.job_kind.resource_class
            ):
                raise DurableJobError("persisted job kind mapping differs")
            if not validate_capacity_use(self.capacity, capacity_use).admitted:
                raise DurableJobError("persisted job exceeds operational capacity")
        latest: dict[tuple[str, int], tuple[Any, ...]] = {}
        prior = ZERO_DIGEST
        rows = connection.execute(
            "SELECT * FROM v3_job_history ORDER BY history_sequence"
        ).fetchall()
        for expected_sequence, row in enumerate(rows, start=1):
            if int(row["history_sequence"]) != expected_sequence:
                raise DurableJobError("job history sequence has a gap")
            value = _history_value(row)
            if str(row["prior_history_digest"]) != prior or str(
                row["history_digest"]
            ) != canonical_digest(value):
                raise DurableJobError("job history digest chain differs")
            authority = SignedManifest(
                "job_transition",
                str(row["auth_body_json"]),
                str(row["auth_body_digest"]),
                str(row["auth_key_id"]),
                str(row["auth_signature_der_b64"]),
            )
            if verify_manifest(authority, self._trust_store) != value:
                raise DurableJobError("job history signature payload differs")
            key = (str(row["job_id"]), int(row["job_revision"]))
            if key not in jobs:
                raise DurableJobError("job history references unknown work")
            if str(row["job_material_digest"]) != _record_material_digest(jobs[key]):
                raise DurableJobError("job history does not bind current job material")
            previous = latest.get(key)
            from_state = row["from_state"]
            if previous is None:
                if str(row["operation_kind"]) != "queued" or from_state is not None:
                    raise DurableJobError("job history does not begin with queued")
            elif from_state != previous[0]:
                raise DurableJobError("job history states are not contiguous")
            _verify_transition(row)
            latest[key] = _history_snapshot(row)
            prior = str(row["history_digest"])
        if set(latest) != set(jobs):
            raise DurableJobError("job projection lacks complete immutable history")
        for key, record in jobs.items():
            if latest[key] != _record_snapshot(record):
                raise DurableJobError("job projection differs from immutable history")
        publications = {
            (str(row["job_id"]), int(row["job_revision"])): row
            for row in connection.execute("SELECT * FROM v3_job_publications")
        }
        succeeded = {key for key, record in jobs.items() if record.state is JobState.SUCCEEDED}
        if set(publications) != succeeded:
            raise DurableJobError("job publications do not exactly match succeeded revisions")
        for key, row in publications.items():
            record = jobs[key]
            if (
                int(row["fencing_token"]) != record.fencing_token
                or str(row["result_digest"]) != record.result_digest
            ):
                raise DurableJobError("job publication differs from its succeeded projection")
            payload = _publication_value(row)
            authority = SignedManifest(
                "job_publication",
                str(row["auth_body_json"]),
                str(row["auth_body_digest"]),
                str(row["auth_key_id"]),
                str(row["auth_signature_der_b64"]),
            )
            verify_manifest(authority, self._trust_store)
            succeeded_row = connection.execute(
                "SELECT prior_history_digest FROM v3_job_history WHERE job_id=? "
                "AND job_revision=? AND operation_kind='succeeded'",
                key,
            ).fetchone()
            if (
                payload["job_material_digest"] != _record_material_digest(record)
                or succeeded_row is None
                or payload["authority_tip"] != str(succeeded_row[0])
            ):
                raise DurableJobError("job publication authority binding differs")

    @staticmethod
    def _get_connection(
        connection: sqlite3.Connection, job_id: str, job_revision: int
    ) -> JobRecord:
        _job_identity(job_id, job_revision)
        row = connection.execute(
            "SELECT * FROM v3_jobs WHERE job_id=? AND job_revision=?", (job_id, job_revision)
        ).fetchone()
        if row is None:
            raise KeyError((job_id, job_revision))
        return _decode(row)


def _decode(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=str(row["job_id"]),
        job_revision=int(row["job_revision"]),
        idempotency_key=str(row["idempotency_key"]),
        job_kind=JobKind(str(row["job_kind"])),
        lane=JobLane(str(row["lane"])),
        resource_class=JobResourceClass(str(row["resource_class"])),
        priority=JobPriority(int(row["base_priority"])),
        capacity_use_json=str(row["capacity_use_json"]),
        payload_json=str(row["payload_json"]),
        payload_digest=str(row["payload_digest"]),
        evidence_digest=str(row["evidence_digest"]),
        bundle_digest=str(row["bundle_digest"]),
        retry_policy_version=str(row["retry_policy_version"]),
        state=JobState(str(row["state"])),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        initial_not_before_at=str(row["initial_not_before_at"]),
        not_before_at=None if row["not_before_at"] is None else str(row["not_before_at"]),
        hard_deadline_at=str(row["hard_deadline_at"]),
        lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
        lease_acquired_at=(
            None if row["lease_acquired_at"] is None else str(row["lease_acquired_at"])
        ),
        lease_expires_at=(
            None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
        ),
        fencing_token=int(row["fencing_token"]),
        terminal_reason=(None if row["terminal_reason"] is None else str(row["terminal_reason"])),
        result_digest=None if row["result_digest"] is None else str(row["result_digest"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _record_material_digest(record: JobRecord) -> str:
    return canonical_digest(
        {
            "schema_version": JOB_RESULT_SCHEMA_VERSION,
            "job_id": record.job_id,
            "job_revision": record.job_revision,
            "idempotency_key": record.idempotency_key,
            "job_kind": record.job_kind.value,
            "lane": record.lane.value,
            "resource_class": record.resource_class.value,
            "priority": int(record.priority),
            "capacity_use": record.capacity_use().to_dict(),
            "payload_digest": record.payload_digest,
            "evidence_digest": record.evidence_digest,
            "bundle_digest": record.bundle_digest,
            "retry_policy_version": record.retry_policy_version,
            "created_at": record.created_at,
            "not_before_at": record.initial_not_before_at,
            "hard_deadline_at": record.hard_deadline_at,
            "max_attempts": record.max_attempts,
        }
    )


def _history_value(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": JOB_RESULT_SCHEMA_VERSION,
        "history_sequence": int(row["history_sequence"]),
        "job_id": str(row["job_id"]),
        "job_revision": int(row["job_revision"]),
        "operation_kind": str(row["operation_kind"]),
        "from_state": None if row["from_state"] is None else str(row["from_state"]),
        "result_state": str(row["result_state"]),
        "attempt_count": int(row["attempt_count"]),
        "fencing_token": int(row["fencing_token"]),
        "lease_owner": None if row["lease_owner"] is None else str(row["lease_owner"]),
        "lease_acquired_at": (
            None if row["lease_acquired_at"] is None else str(row["lease_acquired_at"])
        ),
        "lease_expires_at": (
            None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
        ),
        "not_before_at": None if row["not_before_at"] is None else str(row["not_before_at"]),
        "terminal_reason": (
            None if row["terminal_reason"] is None else str(row["terminal_reason"])
        ),
        "result_digest": None if row["result_digest"] is None else str(row["result_digest"]),
        "observed_at": str(row["observed_at"]),
        "prior_history_digest": str(row["prior_history_digest"]),
        "job_material_digest": str(row["job_material_digest"]),
    }


def _publication_value(row: sqlite3.Row) -> dict[str, Any]:
    body = SignedManifest(
        "job_publication",
        str(row["auth_body_json"]),
        str(row["auth_body_digest"]),
        str(row["auth_key_id"]),
        str(row["auth_signature_der_b64"]),
    ).body()
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise DurableJobError("job publication authority payload is not an object")
    expected = {
        "schema_version": JOB_RESULT_SCHEMA_VERSION,
        "job_id": str(row["job_id"]),
        "job_revision": int(row["job_revision"]),
        "fencing_token": int(row["fencing_token"]),
        "result_digest": str(row["result_digest"]),
        "published_at": str(row["published_at"]),
        "authority_tip": payload.get("authority_tip"),
        "job_material_digest": payload.get("job_material_digest"),
    }
    if payload != expected:
        raise DurableJobError("job publication authority fields differ")
    return expected


def _history_snapshot(row: sqlite3.Row) -> tuple[Any, ...]:
    return (
        str(row["result_state"]),
        int(row["attempt_count"]),
        int(row["fencing_token"]),
        None if row["lease_owner"] is None else str(row["lease_owner"]),
        None if row["lease_acquired_at"] is None else str(row["lease_acquired_at"]),
        None if row["lease_expires_at"] is None else str(row["lease_expires_at"]),
        None if row["not_before_at"] is None else str(row["not_before_at"]),
        None if row["terminal_reason"] is None else str(row["terminal_reason"]),
        None if row["result_digest"] is None else str(row["result_digest"]),
        str(row["observed_at"]),
    )


def _record_snapshot(record: JobRecord) -> tuple[Any, ...]:
    return (
        record.state.value,
        record.attempt_count,
        record.fencing_token,
        record.lease_owner,
        record.lease_acquired_at,
        record.lease_expires_at,
        record.not_before_at,
        record.terminal_reason,
        record.result_digest,
        record.updated_at,
    )


def _verify_transition(row: sqlite3.Row) -> None:
    operation = str(row["operation_kind"])
    source = None if row["from_state"] is None else str(row["from_state"])
    target = str(row["result_state"])
    allowed = {
        "queued": {(None, "queued")},
        "leased": {("queued", "leased")},
        "heartbeat": {("leased", "leased")},
        "lease_expired": {("leased", "queued")},
        "requeued": {("retryable-failed", "queued")},
        "succeeded": {("leased", "succeeded")},
        "invalid": {("leased", "invalid")},
        "stale": {("leased", "stale")},
        "cancelled": {
            ("queued", "cancelled"),
            ("leased", "cancelled"),
            ("retryable-failed", "cancelled"),
        },
        "retryable-failed": {("leased", "retryable-failed")},
        "permanent-failed": {
            ("queued", "permanent-failed"),
            ("leased", "permanent-failed"),
            ("retryable-failed", "permanent-failed"),
        },
    }
    if (source, target) not in allowed.get(operation, set()):
        raise DurableJobError("job history contains an illegal transition")


def _job_identity(job_id: str, job_revision: int) -> None:
    require_identifier(job_id, expected_namespace="job")
    _positive(job_revision, "job revision")


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DurableJobError(f"{label} must be a lower-case SHA-256 digest")
    return value


def _require_token(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise DurableJobError(f"{label} must be a bounded machine token")
    return value


def _require_reason(value: object) -> str:
    if not isinstance(value, str) or _REASON.fullmatch(value) is None:
        raise DurableJobError("reason must be a bounded machine token")
    return value


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DurableJobError(f"{label} must be a positive integer")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DurableJobError(f"{label} must be a non-negative integer")
    return value


def _bounded_duration(value: object) -> int:
    _positive(value, "lease duration")
    if int(value) > 600_000:
        raise DurableJobError("lease duration cannot exceed ten minutes")
    return int(value)


def _milliseconds(timestamp: str) -> int:
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _add_milliseconds(timestamp: str, milliseconds: int) -> str:
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return (moment + timedelta(milliseconds=milliseconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[
        :-3
    ] + "Z"


__all__ = [
    "JOB_RESULT_SCHEMA_VERSION",
    "DurableJobError",
    "DurableJobRepository",
    "FailureKind",
    "JobAdmissionRejected",
    "JobConflict",
    "JobDeadlineExceeded",
    "JobRecord",
    "JobRequest",
    "JobState",
    "QueueHealth",
    "RetryPolicy",
]
