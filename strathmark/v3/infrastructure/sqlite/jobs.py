"""Durable V3 work queue with bounded lanes and monotonic fencing leases."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

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
from strathmark.v3.application.commands import (
    EventIntent,
    validate_command_event_intents,
)
from strathmark.v3.application.job_ports import (
    DurableJobError,
    FailureKind,
    JobAdmissionRejected,
    JobConflict,
    JobDeadlineExceeded,
    ProviderExecutionAudit,
    ReadinessDependencySnapshot,
    ReadinessProbePort,
    RetryPolicy,
    RollingRestartExpectedHead,
    RollingRestartReceipt,
    RollingRestartSuffixStatus,
    RollingRestartTrust,
    RollingRestartTrustMode,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    require_idempotency_key,
    require_identifier,
)
from strathmark.v3.contracts.statuses import LifecycleStatus
from strathmark.v3.domain.state_machines import transition
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection
from strathmark.v3.infrastructure.sqlite.rolling_restart import (
    RollingRestartIntegrityError,
    require_rolling_reaction_cursor_at_event_head,
    rolling_reaction_identity,
)

JOB_RESULT_SCHEMA_VERSION = "strathmark-v3-durable-job-v1"
ZERO_DIGEST = "0" * 64
MAX_JOB_PAYLOAD_BYTES = 1_048_576
MAX_ROLLING_RESTART_ANCHORED_SUFFIX = 256
MAX_ROLLING_RESTART_EVENT_TAIL = 256
MAX_ROLLING_RESTART_AGGREGATE_HEADS = 1_024
MAX_ROLLING_RESTART_DELTA_SUFFIX = 64
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
        if not isinstance(self.lane, JobLane) or not isinstance(
            self.priority, JobPriority
        ):
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
        if (
            canonical_bytes(decoded_use.to_dict()).decode("utf-8")
            != self.capacity_use_json
        ):
            raise DurableJobError("capacity use must be canonical JSON")
        try:
            payload = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableJobError("job payload must be canonical JSON") from exc
        if not isinstance(payload, dict):
            raise DurableJobError("job payload must be a JSON object")
        encoded = canonical_bytes(payload, max_bytes=MAX_JOB_PAYLOAD_BYTES).decode(
            "utf-8"
        )
        if (
            encoded != self.payload_json
            or canonical_digest(payload) != self.payload_digest
        ):
            raise DurableJobError("job payload bytes or digest differ")
        _digest(self.evidence_digest, "evidence digest")
        _digest(self.bundle_digest, "bundle digest")
        _require_token(self.retry_policy_version, "retry policy version")
        created = require_utc_milliseconds(self.created_at)
        not_before = require_utc_milliseconds(self.not_before_at)
        deadline = require_utc_milliseconds(self.hard_deadline_at)
        if not created <= not_before < deadline:
            raise DurableJobError(
                "job timing must satisfy created <= not-before < deadline"
            )
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int
        ):
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
        if not isinstance(job_kind, JobKind) or not isinstance(
            capacity_use, CapacityUse
        ):
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
        restart_trust: RollingRestartTrust | None = None,
    ) -> None:
        if isinstance(database_path, bool) or not isinstance(
            database_path, (Path, str)
        ):
            raise DurableJobError("database path must be a filesystem path")
        if not isinstance(capacity, CapacityManifest):
            raise DurableJobError("durable jobs require a CapacityManifest")
        if not callable(getattr(signer, "sign", None)) or not hasattr(
            signer, "identity"
        ):
            raise DurableJobError("durable jobs require a typed external signer")
        if not isinstance(trust_store, IntegrityTrustStore):
            raise DurableJobError("durable jobs require a typed external trust store")
        trust_store.identity(signer.identity.key_id)
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.capacity = capacity
        self._signer = signer
        self._trust_store = trust_store
        self._restart_trust = (
            RollingRestartTrust.local_corruption_only()
            if restart_trust is None
            else restart_trust
        )
        if not isinstance(self._restart_trust, RollingRestartTrust):
            raise DurableJobError("durable jobs require typed rolling restart trust")
        with open_v3_connection(self.database_path) as connection:
            migrate_connection(connection)
            legacy_history = connection.execute(
                "SELECT COUNT(*) FROM v3_job_history WHERE job_spec_digest=?",
                (ZERO_DIGEST,),
            ).fetchone()[0]
            cutover = connection.execute(
                "SELECT 1 FROM v3_job_spec_cutovers WHERE cutover_sequence=1"
            ).fetchone()
            if legacy_history and cutover is None:
                raise DurableJobError("job spec cutover is required")
            checkpoint = connection.execute(
                "SELECT 1 FROM v3_rolling_restart_checkpoints LIMIT 1"
            ).fetchone()
            if checkpoint is None:
                self._verify_connection(connection)
                self._verify_rolling_storage_connection(
                    connection, allow_closed_current=True
                )
                event_tip = connection.execute(
                    "SELECT global_sequence,event_digest FROM v3_events "
                    "ORDER BY global_sequence DESC LIMIT 1"
                ).fetchone()
                trusted_event_anchor: tuple[int, str] | None = None
                trusted_projection_digest: str | None = None
                if event_tip is not None:
                    from strathmark.v3.infrastructure.sqlite.event_store import (
                        SQLiteEventStore,
                    )
                    from strathmark.v3.infrastructure.sqlite.projections import (
                        SQLiteProjectionStore,
                    )

                    try:
                        event_store = SQLiteEventStore(self.database_path)
                        event_store.verify()
                        anchor = event_store.current_anchor()
                    except Exception as exc:
                        raise DurableJobError(
                            "rolling restart event authority verification failed"
                        ) from exc
                    trusted_event_anchor = (
                        anchor.global_sequence,
                        anchor.event_digest,
                    )
                    try:
                        trusted_projection_digest = SQLiteProjectionStore(
                            self.database_path
                        ).verify_rolling_reaction_projection(
                            anchor.global_sequence,
                            anchor.event_digest,
                        )
                    except Exception as exc:
                        raise DurableJobError(
                            "rolling restart projection authority verification failed"
                        ) from exc
                with immediate_transaction(connection):
                    if trusted_event_anchor is not None:
                        observed_tip = connection.execute(
                            "SELECT global_sequence,event_digest FROM v3_events "
                            "ORDER BY global_sequence DESC LIMIT 1"
                        ).fetchone()
                        if observed_tip is None or (
                            int(observed_tip[0]),
                            str(observed_tip[1]),
                        ) != trusted_event_anchor:
                            raise DurableJobError(
                                "rolling restart event authority changed before first checkpoint"
                            )
                        if (
                            trusted_projection_digest is None
                            or SQLiteProjectionStore.projection_digest(connection)
                            != trusted_projection_digest
                        ):
                            raise DurableJobError(
                                "rolling restart projection authority changed before first checkpoint"
                            )
                    try:
                        require_rolling_reaction_cursor_at_event_head(connection)
                    except RollingRestartIntegrityError as exc:
                        raise DurableJobError(
                            "rolling projection cursor cutover is required"
                        ) from exc
                    self._append_rolling_restart_checkpoint(
                        connection, "1970-01-01T00:00:00.000Z"
                    )
            else:
                with immediate_transaction(connection):
                    self._verify_connection(connection)
                    self._verify_rolling_restart_connection(
                        connection, repair_current=True
                    )
        self.rebuild_rolling_epoch_closures()

    @classmethod
    def bootstrap_job_spec_authority_cutover(
        cls,
        database_path: Path | str,
        *,
        capacity: CapacityManifest,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
    ) -> int:
        """Offline one-time bridge from verified pre-0013 jobs into signed specs."""

        instance = cls.__new__(cls)
        instance.database_path = Path(database_path).expanduser().resolve(strict=False)
        instance.capacity = capacity
        instance._signer = signer
        instance._trust_store = trust_store
        instance._restart_trust = RollingRestartTrust.local_corruption_only()
        if not isinstance(capacity, CapacityManifest):
            raise DurableJobError("durable jobs require a CapacityManifest")
        if not callable(getattr(signer, "sign", None)) or not hasattr(
            signer, "identity"
        ):
            raise DurableJobError("durable jobs require a typed external signer")
        if not isinstance(trust_store, IntegrityTrustStore):
            raise DurableJobError("durable jobs require a typed external trust store")
        trust_store.identity(signer.identity.key_id)
        with open_v3_connection(instance.database_path) as connection:
            migrate_connection(connection)
            existing_cutover = connection.execute(
                "SELECT 1 FROM v3_job_spec_cutovers WHERE cutover_sequence=1"
            ).fetchone()
            if existing_cutover is not None:
                instance._verify_job_spec_cutover(connection)
                instance._replay_job_projection_authority(connection)
                return 0
            history_count = int(
                connection.execute("SELECT COUNT(*) FROM v3_job_history").fetchone()[0]
            )
            if history_count == 0:
                if connection.execute("SELECT 1 FROM v3_job_specs LIMIT 1").fetchone():
                    raise DurableJobError("partial job spec cutover authority exists")
                return 0
            if connection.execute("SELECT 1 FROM v3_job_specs LIMIT 1").fetchone():
                raise DurableJobError("partial job spec cutover authority exists")
            records = instance._verify_legacy_job_projection_authority(connection)
            guard = instance._legacy_job_cutover_guard(connection)
            history_tip = connection.execute(
                "SELECT history_sequence,history_digest,observed_at FROM v3_job_history "
                "ORDER BY history_sequence DESC LIMIT 1"
            ).fetchone()
            assert history_tip is not None
            prepared: list[tuple[JobRecord, dict[str, Any], SignedManifest]] = []
            for key in sorted(records):
                record = records[key]
                value = _job_spec_value(record)
                prepared.append(
                    (
                        record,
                        value,
                        sign_manifest(
                            "job_spec",
                            value,
                            signer=signer,
                            created_at=record.created_at,
                        ),
                    )
                )
            spec_material = [
                {
                    "job_id": record.job_id,
                    "job_revision": record.job_revision,
                    "spec_digest": canonical_digest(value),
                }
                for record, value, _manifest in prepared
            ]
            cutover_value = {
                "schema_version": "strathmark-v3-job-spec-cutover-v1",
                "cutover_sequence": 1,
                "legacy_history_sequence": int(history_tip[0]),
                "legacy_history_digest": str(history_tip[1]),
                "job_spec_count": len(spec_material),
                "job_spec_root_digest": canonical_digest(spec_material),
                "created_at": str(history_tip[2]),
            }
            cutover_manifest = sign_manifest(
                "job_spec_cutover",
                cutover_value,
                signer=signer,
                created_at=str(history_tip[2]),
            )
            with immediate_transaction(connection):
                if instance._legacy_job_cutover_guard(connection) != guard:
                    raise DurableJobError(
                        "job spec cutover authority changed before commit"
                    )
                for record, value, manifest in prepared:
                    connection.execute(
                        "INSERT INTO v3_job_specs VALUES (?,?,?,?,?,?)",
                        (
                            record.job_id,
                            record.job_revision,
                            canonical_bytes(value).decode("utf-8"),
                            canonical_digest(value),
                            canonical_bytes(manifest.to_dict()).decode("utf-8"),
                            record.created_at,
                        ),
                    )
                connection.execute(
                    "INSERT INTO v3_job_spec_cutovers VALUES (?,?,?,?,?,?,?,?)",
                    (
                        1,
                        cutover_value["legacy_history_sequence"],
                        cutover_value["legacy_history_digest"],
                        cutover_value["job_spec_count"],
                        cutover_value["job_spec_root_digest"],
                        cutover_manifest.body_digest,
                        canonical_bytes(cutover_manifest.to_dict()).decode("utf-8"),
                        cutover_value["created_at"],
                    ),
                )
        return len(prepared)

    def enqueue(
        self, request: JobRequest, *, maintenance_suspended: bool = False
    ) -> JobRecord:
        if not isinstance(request, JobRequest):
            raise DurableJobError("enqueue requires a JobRequest")
        if not isinstance(maintenance_suspended, bool):
            raise DurableJobError("maintenance_suspended must be an explicit boolean")
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                payload = request.payload_json and json.loads(request.payload_json)
                epoch_id = _rolling_job_epoch_id(payload)
                if epoch_id is not None:
                    if (
                        connection.execute(
                            "SELECT 1 FROM v3_rolling_epoch_closures WHERE epoch_id=?",
                            (epoch_id,),
                        ).fetchone()
                        is not None
                    ):
                        raise JobAdmissionRejected("rolling_epoch_closed")
                if payload.get("schema_version") == (
                    "strathmark-v3-weight-only-recombination-v1"
                ):
                    self._verify_recombination_context(connection, payload)
                existing = connection.execute(
                    "SELECT * FROM v3_jobs WHERE idempotency_key=?",
                    (str(request.idempotency_key),),
                ).fetchone()
                if existing is not None:
                    record = _decode(existing)
                    if _record_material_digest(record) != request.material_digest:
                        raise JobConflict(
                            "idempotency key already binds different job material"
                        )
                    return record
                if (
                    connection.execute(
                        "SELECT 1 FROM v3_jobs WHERE job_id=? AND job_revision=?",
                        (str(request.job_id), request.job_revision),
                    ).fetchone()
                    is not None
                ):
                    raise JobConflict(
                        "job revision already exists under another idempotency key"
                    )
                load = self._load(connection, request.lane)
                operational = validate_capacity_use(
                    self.capacity, request.capacity_use()
                )
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
                record = self._get_connection(
                    connection, str(request.job_id), request.job_revision
                )
                self._install_job_spec(connection, record)
                self._append_history(
                    connection, "queued", None, record, request.created_at
                )
                return record

    def enqueue_rolling_job(self, **values: Any) -> JobRecord:
        """Build and enqueue one application-defined rolling job at the adapter edge."""

        return self.enqueue(JobRequest.create(**values))

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

    def records_for_card(self, card_digest: str) -> tuple[JobRecord, ...]:
        """Return only the exact verified component set for one causal card."""

        _digest(card_digest, "card digest")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM v3_jobs WHERE json_extract(payload_json, '$.schema_version')=? "
                "AND json_extract(payload_json, '$.card_key.card_digest')=? "
                "ORDER BY json_extract(payload_json, '$.component_ordinal'), job_id, job_revision "
                "LIMIT 7",
                ("strathmark-v3-rolling-component-job-v1", card_digest),
            ).fetchall()
            if len(rows) > 5:
                raise DurableJobError("rolling card has more than five component jobs")
            return self._verify_job_rows_local(connection, rows)

    def current_rolling_card_key(
        self, competitor_id: str, target_context_digest: str
    ) -> dict[str, Any] | None:
        require_identifier(competitor_id, expected_namespace="competitor")
        _digest(target_context_digest, "target context digest")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM v3_jobs "
                "WHERE json_extract(payload_json, '$.schema_version')=? "
                "AND json_extract(payload_json, '$.card_key.competitor_id')=? "
                "AND json_extract(payload_json, '$.card_key.target_context_digest')=? "
                "ORDER BY CAST(json_extract(payload_json, '$.card_key.dependency_revision') "
                "AS INTEGER) DESC LIMIT 6",
                (
                    "strathmark-v3-rolling-component-job-v1",
                    competitor_id,
                    target_context_digest,
                ),
            ).fetchall()
            records = self._verify_job_rows_local(connection, rows)
        if not rows:
            return None
        keys = tuple(record.payload()["card_key"] for record in records)
        maximum = keys[0]["dependency_revision"]
        current = tuple(item for item in keys if item["dependency_revision"] == maximum)
        if len({item["card_digest"] for item in current}) != 1:
            raise DurableJobError("current rolling dependency revision conflicts")
        return dict(current[0])

    def rolling_card_keys_for_epoch(self, epoch_id: str) -> tuple[dict[str, Any], ...]:
        require_identifier(epoch_id, expected_namespace="epoch")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM v3_jobs "
                "WHERE json_extract(payload_json, '$.schema_version')=? "
                "AND json_extract(payload_json, '$.card_key.tournament_epoch_id')=? "
                "ORDER BY json_extract(payload_json, '$.card_key.card_digest'),"
                "json_extract(payload_json, '$.component_ordinal') LIMIT ?",
                (
                    "strathmark-v3-rolling-component-job-v1",
                    epoch_id,
                    self.capacity.max_context_cards * 5 + 1,
                ),
            ).fetchall()
            records = self._verify_job_rows_local(connection, rows)
        keys_by_digest = {
            record.payload()["card_key"]["card_digest"]: record.payload()["card_key"]
            for record in records
        }
        if len(keys_by_digest) > self.capacity.max_context_cards:
            raise DurableJobError("rolling epoch exceeds installed card capacity")
        return tuple(keys_by_digest[digest] for digest in sorted(keys_by_digest))

    def close_rolling_epoch(self, epoch_id: str, event: Any) -> None:
        from strathmark.v3.contracts.events import EventEnvelope, EventKind

        epoch = require_identifier(epoch_id, expected_namespace="epoch")
        if not isinstance(event, EventEnvelope) or event.kind not in {
            EventKind.ROUND_CLOSED,
            EventKind.TOURNAMENT_CLOSED,
        }:
            raise DurableJobError(
                "rolling epoch closure requires canonical close event"
            )
        value = {
            "schema_version": "strathmark-v3-rolling-epoch-closure-v1",
            "epoch_id": str(epoch),
            "source_event_digest": event.event_digest,
            "source_global_sequence": event.global_sequence,
            "source_event_kind": event.kind.value,
            "closed_at": event.occurred_at_utc,
        }
        manifest = sign_manifest(
            "rolling_epoch_closure",
            value,
            signer=self._signer,
            created_at=event.occurred_at_utc,
        )
        encoded = canonical_bytes(manifest.to_dict()).decode("utf-8")
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                source = connection.execute(
                    "SELECT event_digest,envelope_json FROM v3_events WHERE global_sequence=?",
                    (event.global_sequence,),
                ).fetchone()
                if (
                    source is None
                    or str(source[0]) != event.event_digest
                    or json.loads(str(source[1])) != event.to_dict()
                ):
                    raise DurableJobError(
                        "rolling epoch closure event is not installed authority"
                    )
                if not self._rolling_close_lineage(connection, str(epoch), event):
                    raise DurableJobError(
                        "rolling epoch does not belong to close event lineage"
                    )
                existing = connection.execute(
                    "SELECT source_event_digest,source_global_sequence,source_event_kind,"
                    "closed_at,closure_manifest_json FROM v3_rolling_epoch_closures "
                    "WHERE epoch_id=?",
                    (str(epoch),),
                ).fetchone()
                material = (
                    event.event_digest,
                    event.global_sequence,
                    event.kind.value,
                    event.occurred_at_utc,
                    encoded,
                )
                if existing is not None:
                    stored_manifest = SignedManifest.from_dict(
                        json.loads(str(existing[4]))
                    )
                    stored_value = {
                        "schema_version": "strathmark-v3-rolling-epoch-closure-v1",
                        "epoch_id": str(epoch),
                        "source_event_digest": str(existing[0]),
                        "source_global_sequence": int(existing[1]),
                        "source_event_kind": str(existing[2]),
                        "closed_at": str(existing[3]),
                    }
                    if (
                        verify_manifest(stored_manifest, self._trust_store)
                        != stored_value
                    ):
                        raise DurableJobError("rolling epoch closure conflicts")
                    return
                connection.execute(
                    "INSERT INTO v3_rolling_epoch_closures VALUES (?,?,?,?,?,?)",
                    (str(epoch), *material[:4], encoded),
                )
                self._append_rolling_restart_delta(
                    connection,
                    operation_kind="epoch_closed",
                    authority_kind="epoch_closure",
                    authority_sequence=event.global_sequence,
                    authority_digest=manifest.body_digest,
                    observed_at=event.occurred_at_utc,
                )

    def rebuild_rolling_epoch_closures(self) -> int:
        """Replay canonical lifecycle close events into the durable rolling fence."""

        from strathmark.v3.contracts.events import EventEnvelope, EventKind
        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        pending: list[tuple[str, EventEnvelope]] = []
        with open_v3_connection(self.database_path, read_only=True) as connection:
            missing = connection.execute(
                "SELECT epoch.epoch_id,epoch.round_id,ingress.tournament_id "
                "FROM v3_evidence_epochs epoch "
                "JOIN v3_ingress_snapshots ingress ON ingress.entity_kind='round' "
                "AND ingress.entity_id=epoch.round_id "
                "LEFT JOIN v3_rolling_epoch_closures closure "
                "ON closure.epoch_id=epoch.epoch_id WHERE closure.epoch_id IS NULL "
                "AND ingress.upstream_revision=(SELECT MAX(current.upstream_revision) "
                "FROM v3_ingress_snapshots current WHERE current.entity_kind='round' "
                "AND current.entity_id=epoch.round_id) "
                "ORDER BY epoch.frozen_global_sequence DESC LIMIT ?",
                (self.capacity.max_context_cards + 1,),
            ).fetchall()
            if len(missing) > self.capacity.max_context_cards:
                raise DurableJobError(
                    "unreconciled epoch closures exceed installed capacity"
                )
            for row in missing:
                source = connection.execute(
                    "SELECT envelope_json FROM v3_events WHERE "
                    "(event_kind=? AND aggregate_id=?) OR "
                    "(event_kind=? AND aggregate_id=?) "
                    "ORDER BY global_sequence LIMIT 1",
                    (
                        EventKind.ROUND_CLOSED.value,
                        str(row[1]),
                        EventKind.TOURNAMENT_CLOSED.value,
                        str(row[2]),
                    ),
                ).fetchone()
                if source is not None:
                    pending.append(
                        (
                            str(row[0]),
                            EventEnvelope.from_dict(json.loads(str(source[0]))),
                        )
                    )
        if pending:
            SQLiteEventStore(self.database_path).verify()
        created = 0
        for epoch_id, event in pending:
            if not self.rolling_epoch_closed(epoch_id):
                self.close_rolling_epoch(epoch_id, event)
                created += 1
        return created

    def cancel_closed_rolling_jobs(self) -> tuple[JobRecord, ...]:
        """Idempotently sweep active work whose authoritative epoch is closed."""

        cancelled: list[JobRecord] = []
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                self._verify_rolling_status_tip(connection)
                rows = connection.execute(
                    "SELECT job.*,closure.closed_at FROM v3_jobs job "
                    "JOIN v3_rolling_epoch_closures closure "
                    "ON closure.epoch_id=CASE "
                    "WHEN json_extract(job.payload_json, '$.schema_version')=? "
                    "THEN json_extract(job.payload_json, '$.card_key.tournament_epoch_id') "
                    "WHEN json_extract(job.payload_json, '$.schema_version')=? "
                    "THEN json_extract(job.payload_json, '$.tournament_epoch_id') END "
                    "AND job.state IN ('queued','leased','retryable-failed') "
                    "ORDER BY job.created_at,job.job_id,job.job_revision",
                    (
                        "strathmark-v3-rolling-component-job-v1",
                        "strathmark-v3-weight-only-recombination-v1",
                    ),
                ).fetchall()
                for row in rows:
                    current = _decode(row)
                    closed_at = str(row["closed_at"])
                    connection.execute(
                        "UPDATE v3_jobs SET state='cancelled', not_before_at=NULL, "
                        "lease_owner=NULL, lease_acquired_at=NULL, lease_expires_at=NULL, "
                        "fencing_token=fencing_token+1, terminal_reason='epoch_closed', "
                        "updated_at=? WHERE job_id=? AND job_revision=?",
                        (closed_at, current.job_id, current.job_revision),
                    )
                    result = self._get_connection(
                        connection, current.job_id, current.job_revision
                    )
                    self._append_history(
                        connection, "cancelled", current.state, result, closed_at
                    )
                    cancelled.append(result)
        return tuple(cancelled)

    def supersede_closed_rolling_publications(self) -> tuple[str, ...]:
        """Remove closed-epoch cards from the current cache with an audit status."""

        superseded: list[str] = []
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                self._verify_rolling_status_tip(connection)
                rows = connection.execute(
                    "SELECT current.competitor_id,current.target_context_digest,"
                    "current.publication_digest,closure.closed_at "
                    "FROM v3_rolling_card_current current "
                    "JOIN v3_rolling_card_publications publication "
                    "ON publication.publication_digest=current.publication_digest "
                    "JOIN v3_rolling_epoch_closures closure "
                    "ON closure.epoch_id=publication.tournament_epoch_id "
                    "ORDER BY current.competitor_id,current.target_context_digest"
                ).fetchall()
                for row in rows:
                    publication_digest = str(row[2])
                    self._append_rolling_status(
                        connection,
                        publication_digest,
                        "superseded",
                        "epoch_closed",
                        str(row[3]),
                    )
                    connection.execute(
                        "DELETE FROM v3_rolling_card_current WHERE competitor_id=? "
                        "AND target_context_digest=?",
                        (str(row[0]), str(row[1])),
                    )
                    superseded.append(publication_digest)
        return tuple(superseded)

    def rolling_epoch_closed(self, epoch_id: str) -> bool:
        epoch = require_identifier(epoch_id, expected_namespace="epoch")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT closure_manifest_json FROM v3_rolling_epoch_closures WHERE epoch_id=?",
                (str(epoch),),
            ).fetchone()
        if row is None:
            return False
        manifest = SignedManifest.from_dict(json.loads(str(row[0])))
        payload = verify_manifest(manifest, self._trust_store)
        if payload.get("epoch_id") != str(epoch):
            raise DurableJobError("rolling epoch closure index differs")
        return True

    def pending_rolling_reactions(self, *, limit: int) -> tuple[dict[str, Any], ...]:
        _positive(limit, "rolling reaction page limit")
        if limit > self.capacity.max_context_cards:
            raise DurableJobError("rolling reaction page exceeds installed capacity")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT obligation.* FROM v3_rolling_reaction_obligations obligation "
                "LEFT JOIN v3_rolling_reaction_completions completion "
                "ON completion.reaction_id=obligation.reaction_id "
                "WHERE completion.reaction_id IS NULL "
                "ORDER BY first_global_sequence LIMIT ?",
                (limit,),
            ).fetchall()
            self._verify_rolling_reaction_completion_tip(
                connection,
                None if not rows else int(rows[0]["first_global_sequence"]),
            )
            return tuple(
                self._verify_rolling_reaction_row(connection, row) for row in rows
            )

    def _verify_rolling_reaction_completion_tip(
        self,
        connection: sqlite3.Connection,
        first_pending_sequence: int | None,
    ) -> None:
        """Verify the signed contiguous completion prefix before anti-join suppression."""

        if first_pending_sequence is not None:
            gap = connection.execute(
                "SELECT 1 FROM v3_rolling_reaction_completions completion JOIN "
                "v3_rolling_reaction_obligations obligation USING(reaction_id) "
                "WHERE obligation.first_global_sequence>? LIMIT 1",
                (first_pending_sequence,),
            ).fetchone()
            if gap is not None:
                raise DurableJobError("rolling reaction completion prefix has a gap")
            predicate = "WHERE obligation.first_global_sequence<?"
            parameters: tuple[Any, ...] = (first_pending_sequence,)
        else:
            predicate = ""
            parameters = ()
        row = connection.execute(
            "SELECT obligation.*,completion.plan_digest,completion.completed_at,"
            "completion.completion_digest,completion.completion_manifest_json "
            "FROM v3_rolling_reaction_completions completion JOIN "
            "v3_rolling_reaction_obligations obligation USING(reaction_id) "
            f"{predicate} ORDER BY obligation.first_global_sequence DESC LIMIT 1",
            parameters,
        ).fetchone()
        if row is None:
            return
        obligation = self._verify_rolling_reaction_row(connection, row)
        self._verify_rolling_reaction_completion(row, obligation)

    def complete_rolling_reaction(
        self,
        reaction_id: str,
        *,
        plan_digest: str,
        completed_at: str,
    ) -> None:
        _digest(reaction_id, "rolling reaction")
        _digest(plan_digest, "rolling reaction plan")
        timestamp = require_utc_milliseconds(completed_at)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                row = connection.execute(
                    "SELECT * FROM v3_rolling_reaction_obligations WHERE reaction_id=?",
                    (reaction_id,),
                ).fetchone()
                if row is None:
                    raise DurableJobError("rolling reaction obligation is missing")
                self._verify_rolling_reaction_row(connection, row)
                existing = connection.execute(
                    "SELECT completion.*,obligation.source_command_id,"
                    "obligation.event_set_digest FROM v3_rolling_reaction_completions completion "
                    "JOIN v3_rolling_reaction_obligations obligation USING(reaction_id) "
                    "WHERE reaction_id=?",
                    (reaction_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["plan_digest"]) != plan_digest:
                        raise DurableJobError("rolling reaction completion conflicts")
                    self._verify_rolling_reaction_completion(
                        existing,
                        {
                            "reaction_id": reaction_id,
                            "source_command_id": str(existing["source_command_id"]),
                            "event_set_digest": str(existing["event_set_digest"]),
                        },
                    )
                    return
                value = {
                    "schema_version": "strathmark-v3-rolling-reaction-completion-v1",
                    "reaction_id": reaction_id,
                    "source_command_id": str(row["source_command_id"]),
                    "event_set_digest": str(row["event_set_digest"]),
                    "plan_digest": plan_digest,
                    "completed_at": timestamp,
                }
                completion_digest = canonical_digest(value)
                manifest = sign_manifest(
                    "rolling_reaction_completion",
                    {**value, "completion_digest": completion_digest},
                    signer=self._signer,
                    created_at=timestamp,
                )
                connection.execute(
                    "INSERT INTO v3_rolling_reaction_completions VALUES (?,?,?,?,?)",
                    (
                        reaction_id,
                        plan_digest,
                        timestamp,
                        completion_digest,
                        canonical_bytes(manifest.to_dict()).decode("utf-8"),
                    ),
                )
                self._append_rolling_restart_delta(
                    connection,
                    operation_kind="reaction_completed",
                    authority_kind="reaction_completion",
                    authority_sequence=int(row["last_global_sequence"]),
                    authority_digest=completion_digest,
                    observed_at=timestamp,
                )

    @staticmethod
    def _verify_rolling_reaction_row(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        from strathmark.v3.contracts.events import EventEnvelope

        refs = json.loads(str(row["event_ids_json"]))
        if not isinstance(refs, list) or not refs:
            raise DurableJobError("rolling reaction event set is invalid")
        if any(
            not isinstance(ref, dict)
            or set(ref) != {"event_id", "event_digest", "global_sequence"}
            for ref in refs
        ):
            raise DurableJobError("rolling reaction event references differ")
        sequences = tuple(ref["global_sequence"] for ref in refs)
        if any(
            isinstance(item, bool) or not isinstance(item, int) for item in sequences
        ):
            raise DurableJobError("rolling reaction event sequence is invalid")
        if sequences != tuple(range(sequences[0], sequences[-1] + 1)):
            raise DurableJobError("rolling reaction event set is not contiguous")
        event_set_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-rolling-reaction-event-set-v1",
                "events": refs,
            }
        )
        reaction_id = canonical_digest(
            {
                "source_command_id": str(row["source_command_id"]),
                "event_set_digest": event_set_digest,
            }
        )
        if (
            reaction_id != str(row["reaction_id"])
            or event_set_digest != str(row["event_set_digest"])
            or int(row["first_global_sequence"]) != int(refs[0]["global_sequence"])
            or int(row["last_global_sequence"]) != int(refs[-1]["global_sequence"])
        ):
            raise DurableJobError("rolling reaction obligation integrity differs")
        for ref in refs:
            source = connection.execute(
                "SELECT event_id,event_digest,envelope_json FROM v3_events WHERE global_sequence=?",
                (ref["global_sequence"],),
            ).fetchone()
            if (
                source is None
                or str(source[0]) != ref["event_id"]
                or str(source[1]) != ref["event_digest"]
            ):
                raise DurableJobError("rolling reaction event authority differs")
            event = EventEnvelope.from_dict(json.loads(str(source[2])))
            if str(event.command.command_id) != str(row["source_command_id"]):
                raise DurableJobError("rolling reaction command authority differs")
        return {
            "reaction_id": reaction_id,
            "source_command_id": str(row["source_command_id"]),
            "event_set_digest": event_set_digest,
            "first_global_sequence": int(row["first_global_sequence"]),
            "last_global_sequence": int(row["last_global_sequence"]),
            "event_ids": tuple(str(ref["event_id"]) for ref in refs),
            "registered_at": str(row["registered_at"]),
        }

    def _verify_rolling_reaction_completion(
        self, row: sqlite3.Row, obligation: Mapping[str, Any]
    ) -> None:
        value = {
            "schema_version": "strathmark-v3-rolling-reaction-completion-v1",
            "reaction_id": obligation["reaction_id"],
            "source_command_id": obligation["source_command_id"],
            "event_set_digest": obligation["event_set_digest"],
            "plan_digest": str(row["plan_digest"]),
            "completed_at": str(row["completed_at"]),
        }
        completion_digest = canonical_digest(value)
        try:
            manifest = SignedManifest.from_dict(
                json.loads(str(row["completion_manifest_json"]))
            )
            verified = verify_manifest(manifest, self._trust_store)
        except (IntegrityError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DurableJobError(
                "rolling reaction completion integrity differs"
            ) from exc
        if (
            completion_digest != str(row["completion_digest"])
            or manifest.kind != "rolling_reaction_completion"
            or verified != {**value, "completion_digest": completion_digest}
        ):
            raise DurableJobError("rolling reaction completion integrity differs")

    def install_rolling_council_authority(
        self,
        manifest: SignedManifest,
        *,
        bundle_digest: str,
        installed_at: str,
    ) -> str:
        if not isinstance(manifest, SignedManifest):
            raise DurableJobError("rolling council authority must be signed")
        _digest(bundle_digest, "rolling council bundle")
        timestamp = require_utc_milliseconds(installed_at)
        payload = verify_manifest(manifest, self._trust_store)
        if payload.get("bundle_digest") != bundle_digest:
            raise DurableJobError("rolling council authority bundle differs")
        encoded = canonical_bytes(manifest.to_dict()).decode("utf-8")
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                row = connection.execute(
                    "SELECT bundle_digest,manifest_json FROM v3_rolling_council_authorities "
                    "WHERE manifest_digest=?",
                    (manifest.body_digest,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) != bundle_digest or str(row[1]) != encoded:
                        raise DurableJobError(
                            "rolling council authority digest conflicts"
                        )
                    return manifest.body_digest
                try:
                    connection.execute(
                        "INSERT INTO v3_rolling_council_authorities"
                        "(manifest_digest,bundle_digest,manifest_json,installed_at) "
                        "VALUES (?,?,?,?)",
                        (manifest.body_digest, bundle_digest, encoded, timestamp),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DurableJobError(
                        "one bundle cannot install multiple rolling council rosters"
                    ) from exc
        return manifest.body_digest

    def rolling_council_authority(self, digest: str) -> tuple[str, SignedManifest]:
        _digest(digest, "rolling council manifest")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT bundle_digest,manifest_json FROM v3_rolling_council_authorities "
                "WHERE manifest_digest=?",
                (digest,),
            ).fetchone()
        if row is None:
            raise DurableJobError("rolling council authority is not installed")
        manifest = SignedManifest.from_dict(json.loads(str(row[1])))
        if manifest.body_digest != digest:
            raise DurableJobError("rolling council authority index differs")
        verify_manifest(manifest, self._trust_store)
        return str(row[0]), manifest

    def rolling_publication_row(
        self, *, publication_digest: str | None = None, card_digest: str | None = None
    ) -> dict[str, Any] | None:
        if (publication_digest is None) == (card_digest is None):
            raise DurableJobError(
                "rolling publication lookup requires one exact digest"
            )
        column = (
            "publication_digest" if publication_digest is not None else "card_digest"
        )
        digest = publication_digest if publication_digest is not None else card_digest
        _digest(digest, column)
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                f"SELECT * FROM v3_rolling_card_publications WHERE {column}=?",
                (digest,),
            ).fetchone()
        return None if row is None else dict(row)

    def rolling_publication_rows(self) -> tuple[dict[str, Any], ...]:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM v3_rolling_card_publications ORDER BY sealed_at,publication_digest"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def rolling_current_rows(self) -> tuple[dict[str, Any], ...]:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM v3_rolling_card_current ORDER BY competitor_id,target_context_digest"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def recover_rolling_restart(self) -> RollingRestartReceipt:
        """Verify only bounded current rolling material against its signed checkpoint."""

        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                return self._verify_rolling_restart_connection(
                    connection, repair_current=True
                )

    def rolling_restart_suffix_status(self) -> RollingRestartSuffixStatus:
        """Return verified checkpoint and bounded uncompacted delta status."""

        receipt = self.recover_rolling_restart()
        with open_v3_connection(self.database_path, read_only=True) as connection:
            checkpoint = connection.execute(
                "SELECT checkpoint_sequence,checkpoint_digest,created_at,"
                "absorbed_delta_sequence,absorbed_delta_digest FROM "
                "v3_rolling_restart_checkpoints ORDER BY checkpoint_sequence DESC LIMIT 1"
            ).fetchone()
            if checkpoint is None or (
                int(checkpoint[0]),
                str(checkpoint[1]),
            ) != (receipt.checkpoint_sequence, receipt.checkpoint_digest):
                raise DurableJobError(
                    "rolling restart checkpoint changed during suffix read"
                )
            suffix_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_rolling_restart_deltas "
                    "WHERE base_checkpoint_sequence=?",
                    (receipt.checkpoint_sequence,),
                ).fetchone()[0]
            )
            tip = connection.execute(
                "SELECT delta_sequence,delta_digest FROM "
                "v3_rolling_restart_delta_tip WHERE singleton=1"
            ).fetchone()
        tip_sequence = int(checkpoint[3]) if tip is None else int(tip[0])
        tip_digest = str(checkpoint[4]) if tip is None else str(tip[1])
        return RollingRestartSuffixStatus(
            receipt.checkpoint_sequence,
            receipt.checkpoint_digest,
            str(checkpoint[2]),
            int(checkpoint[3]),
            str(checkpoint[4]),
            suffix_count,
            tip_sequence,
            tip_digest,
        )

    def refresh_rolling_restart_checkpoint_if_due(
        self,
        *,
        observed_at: str,
        delta_threshold: int = 48,
        max_elapsed_ms: int = 300_000,
    ) -> RollingRestartReceipt | None:
        """Prepare a compact checkpoint off-lock and publish it by exact CAS."""

        from datetime import datetime

        timestamp = require_utc_milliseconds(observed_at)
        if (
            isinstance(delta_threshold, bool)
            or not isinstance(delta_threshold, int)
            or not 0 < delta_threshold < MAX_ROLLING_RESTART_DELTA_SUFFIX
        ):
            raise DurableJobError("rolling restart refresh threshold is invalid")
        if (
            isinstance(max_elapsed_ms, bool)
            or not isinstance(max_elapsed_ms, int)
            or max_elapsed_ms <= 0
        ):
            raise DurableJobError("rolling restart refresh RPO is invalid")
        self.recover_rolling_restart()
        with open_v3_connection(self.database_path, read_only=True) as connection:
            checkpoint = connection.execute(
                "SELECT checkpoint_sequence,checkpoint_digest,created_at FROM "
                "v3_rolling_restart_checkpoints ORDER BY checkpoint_sequence DESC LIMIT 1"
            ).fetchone()
            delta_tip = connection.execute(
                "SELECT delta_sequence,delta_digest,base_checkpoint_sequence FROM "
                "v3_rolling_restart_delta_tip WHERE singleton=1"
            ).fetchone()
            if checkpoint is None:
                raise DurableJobError("rolling restart checkpoint is missing")
            suffix_count = 0 if delta_tip is None else int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_rolling_restart_deltas "
                    "WHERE base_checkpoint_sequence=?",
                    (int(checkpoint[0]),),
                ).fetchone()[0]
            )
            elapsed_ms = int(
                (
                    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    - datetime.fromisoformat(str(checkpoint[2]).replace("Z", "+00:00"))
                ).total_seconds()
                * 1000
            )
            if elapsed_ms < 0:
                raise DurableJobError(
                    "rolling restart refresh time precedes checkpoint"
                )
            if suffix_count < delta_threshold and elapsed_ms < max_elapsed_ms:
                return None
            self._verify_connection(connection)
            self._verify_rolling_storage_connection(
                connection, allow_closed_current=True
            )
            material = self._rolling_restart_material(connection)
            guard = self._rolling_restart_refresh_guard(connection)
        self._before_rolling_restart_refresh_commit()
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                if self._rolling_restart_refresh_guard(connection) != guard:
                    raise DurableJobError(
                        "rolling restart refresh authority changed before commit"
                    )
                return self._append_rolling_restart_checkpoint(
                    connection, timestamp, prepared_material=material
                )

    def _before_rolling_restart_refresh_commit(self) -> None:
        """Test seam after prepared verification and before the short CAS writer."""

    def rebuild_job_projection(self) -> int:
        """Rebuild mutable job rows from signed specs plus immutable transitions."""

        with open_v3_connection(self.database_path, read_only=True) as connection:
            rebuilt = self._replay_job_projection_authority(connection)
            guard = self._job_projection_authority_guard(connection)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                if self._job_projection_authority_guard(connection) != guard:
                    raise DurableJobError(
                        "job projection authority changed before rebuild"
                    )
                changed = 0
                expected = set(rebuilt)
                existing = {
                    (str(row["job_id"]), int(row["job_revision"])): row
                    for row in connection.execute("SELECT * FROM v3_jobs")
                }
                for key, record in rebuilt.items():
                    values = _record_storage_values(record)
                    current = existing.get(key)
                    if current is not None and tuple(current) == values:
                        continue
                    connection.execute(
                        "INSERT INTO v3_jobs VALUES ("
                        + ",".join("?" for _ in values)
                        + ") ON CONFLICT(job_id,job_revision) DO UPDATE SET "
                        "idempotency_key=excluded.idempotency_key,"
                        "job_kind=excluded.job_kind,lane=excluded.lane,"
                        "resource_class=excluded.resource_class,"
                        "base_priority=excluded.base_priority,"
                        "capacity_use_json=excluded.capacity_use_json,"
                        "payload_json=excluded.payload_json,"
                        "payload_digest=excluded.payload_digest,"
                        "evidence_digest=excluded.evidence_digest,"
                        "bundle_digest=excluded.bundle_digest,"
                        "retry_policy_version=excluded.retry_policy_version,"
                        "state=excluded.state,attempt_count=excluded.attempt_count,"
                        "max_attempts=excluded.max_attempts,"
                        "initial_not_before_at=excluded.initial_not_before_at,"
                        "not_before_at=excluded.not_before_at,"
                        "hard_deadline_at=excluded.hard_deadline_at,"
                        "lease_owner=excluded.lease_owner,"
                        "lease_acquired_at=excluded.lease_acquired_at,"
                        "lease_expires_at=excluded.lease_expires_at,"
                        "fencing_token=excluded.fencing_token,"
                        "terminal_reason=excluded.terminal_reason,"
                        "result_digest=excluded.result_digest,"
                        "created_at=excluded.created_at,updated_at=excluded.updated_at",
                        values,
                    )
                    changed += 1
                for key in set(existing) - expected:
                    connection.execute(
                        "DELETE FROM v3_jobs WHERE job_id=? AND job_revision=?", key
                    )
                    changed += 1
                self._verify_connection(connection)
                return changed

    def recover_rolling_restart_deep_audit(self) -> RollingRestartReceipt:
        """Offline/listener-stopped verification, then a short CAS checkpoint refresh."""

        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
        from strathmark.v3.infrastructure.sqlite.projections import (
            SQLiteProjectionStore,
        )

        try:
            event_store = SQLiteEventStore(self.database_path)
            preflight_anchor = event_store.current_anchor()
            projection_store = SQLiteProjectionStore(self.database_path)
            rebuilt_anchor = projection_store.rebuild_rolling_reaction_projection_offline(
                preflight_anchor.global_sequence,
                preflight_anchor.event_digest,
            )
            if rebuilt_anchor != (
                preflight_anchor.global_sequence,
                preflight_anchor.event_digest,
            ):
                raise DurableJobError(
                    "rolling restart offline projection rebuild changed authority"
                )
            trusted_anchor = event_store.current_anchor()
            if (
                trusted_anchor.global_sequence,
                trusted_anchor.event_digest,
            ) != rebuilt_anchor:
                raise DurableJobError(
                    "rolling restart authority changed after offline projection rebuild"
                )
            projection_digest = projection_store.verify_rolling_reaction_projection(
                trusted_anchor.global_sequence,
                trusted_anchor.event_digest,
            )
            with open_v3_connection(self.database_path, read_only=True) as connection:
                if (
                    SQLiteProjectionStore.projection_digest(connection)
                    != projection_digest
                ):
                    raise DurableJobError(
                        "rolling restart projection authority changed after verification"
                    )
                self._verify_connection(connection)
                self._verify_rolling_storage_connection(
                    connection, allow_closed_current=True
                )
                self._verify_rolling_restart_checkpoint_history_connection(
                    connection
                )
                aggregate_heads = self._replay_all_rolling_aggregate_heads(
                    connection
                )
                cursor_value = self._full_rolling_cursor_value(connection)
                guard = self._deep_rolling_recovery_guard(connection)
        except Exception as exc:
            if isinstance(exc, DurableJobError):
                raise
            raise DurableJobError(
                "rolling restart deep audit failed closed"
            ) from exc
        self._before_deep_rolling_recovery_commit()
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                event_tip = connection.execute(
                    "SELECT global_sequence,event_digest FROM v3_events "
                    "ORDER BY global_sequence DESC LIMIT 1"
                ).fetchone()
                observed_anchor = (
                    (0, ZERO_DIGEST)
                    if event_tip is None
                    else (int(event_tip[0]), str(event_tip[1]))
                )
                if observed_anchor != (
                    trusted_anchor.global_sequence,
                    trusted_anchor.event_digest,
                ):
                    raise DurableJobError(
                        "rolling restart deep audit authority changed before commit"
                    )
                if (
                    SQLiteProjectionStore.projection_digest(connection)
                    != projection_digest
                ):
                    raise DurableJobError(
                        "rolling restart deep audit projection material changed before commit"
                    )
                if self._deep_rolling_recovery_guard(connection) != guard:
                    raise DurableJobError(
                        "rolling restart deep audit material changed before commit"
                    )
                self._write_rolling_cursor_value(connection, cursor_value)
                receipt = self._append_rolling_restart_checkpoint(
                    connection,
                    cursor_value["updated_at"],
                    aggregate_heads_override=aggregate_heads,
                )
        return receipt

    def _before_deep_rolling_recovery_commit(self) -> None:
        """Test seam after lifetime read verification and before the short CAS writer."""

    def _replay_all_rolling_aggregate_heads(
        self, connection: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        heads: dict[tuple[str, str], dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
        ):
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            self._advance_rolling_aggregate_head(heads, event)
        material = [heads[key] for key in sorted(heads)]
        if len(material) > self._rolling_aggregate_head_capacity():
            raise DurableJobError(
                "rolling restart aggregate heads exceed bounded capacity"
            )
        return material

    @staticmethod
    def _full_rolling_cursor_value(
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        events = tuple(
            EventEnvelope.from_dict(json.loads(str(row[0])))
            for row in connection.execute(
                "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
            )
        )
        groups: list[tuple[EventEnvelope, ...]] = []
        for event in events:
            if not groups or groups[-1][0].command.command_id != event.command.command_id:
                groups.append((event,))
            else:
                groups[-1] = (*groups[-1], event)
        relevant = tuple(
            identity
            for identity in (rolling_reaction_identity(group) for group in groups)
            if identity is not None
        )
        last = None if not events else events[-1]
        return {
            "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
            "cursor_revision": len(groups),
            "through_global_sequence": 0 if last is None else last.global_sequence,
            "through_event_digest": ZERO_DIGEST if last is None else last.event_digest,
            "relevant_command_count": len(relevant),
            "latest_reaction_id": ZERO_DIGEST if not relevant else relevant[-1],
            "updated_at": (
                "1970-01-01T00:00:00.000Z"
                if last is None
                else last.occurred_at_utc
            ),
        }

    @staticmethod
    def _write_rolling_cursor_value(
        connection: sqlite3.Connection, value: Mapping[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO v3_rolling_reaction_cursor VALUES (1,?,?,?,?,?,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET "
            "cursor_revision=excluded.cursor_revision,"
            "through_global_sequence=excluded.through_global_sequence,"
            "through_event_digest=excluded.through_event_digest,"
            "relevant_command_count=excluded.relevant_command_count,"
            "latest_reaction_id=excluded.latest_reaction_id,"
            "cursor_digest=excluded.cursor_digest,updated_at=excluded.updated_at",
            (
                value["cursor_revision"],
                value["through_global_sequence"],
                value["through_event_digest"],
                value["relevant_command_count"],
                value["latest_reaction_id"],
                canonical_digest(value),
                value["updated_at"],
            ),
        )

    def _deep_rolling_recovery_guard(
        self, connection: sqlite3.Connection
    ) -> str:
        def rows(query: str, parameters: tuple[Any, ...] = ()) -> list[list[Any]]:
            return [list(row) for row in connection.execute(query, parameters)]

        return canonical_digest(
            {
                "job_history_tip": rows(
                    "SELECT history_sequence,history_digest FROM v3_job_history "
                    "ORDER BY history_sequence DESC LIMIT 1"
                ),
                "status_tip": rows(
                    "SELECT status_sequence,status_digest FROM "
                    "v3_rolling_card_status_history "
                    "ORDER BY status_sequence DESC LIMIT 1"
                ),
                "current": rows(
                    "SELECT * FROM v3_rolling_card_current "
                    "ORDER BY competitor_id,target_context_digest"
                ),
                "active_jobs": rows(
                    "SELECT * FROM v3_jobs WHERE state IN "
                    "('queued','leased','retryable-failed') AND "
                    "json_extract(payload_json, '$.schema_version') IN (?,?) "
                    "ORDER BY job_id,job_revision LIMIT ?",
                    (
                        "strathmark-v3-rolling-component-job-v1",
                        "strathmark-v3-weight-only-recombination-v1",
                        self.capacity.max_queued_jobs + 1,
                    ),
                ),
                "pending_reactions": self._rolling_pending_material(connection),
                "closure_count": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM v3_rolling_epoch_closures"
                    ).fetchone()[0]
                ),
            }
        )

    def _rolling_restart_refresh_guard(self, connection: sqlite3.Connection) -> str:
        from strathmark.v3.infrastructure.sqlite.projections import (
            SQLiteProjectionStore,
        )

        return canonical_digest(
            {
                "schema_version": "strathmark-v3-rolling-refresh-guard-v1",
                "material": self._deep_rolling_recovery_guard(connection),
                "event_tip": [
                    list(row)
                    for row in connection.execute(
                        "SELECT global_sequence,event_digest FROM v3_events "
                        "ORDER BY global_sequence DESC LIMIT 1"
                    )
                ],
                "checkpoint_tip": [
                    list(row)
                    for row in connection.execute(
                        "SELECT checkpoint_sequence,checkpoint_digest FROM "
                        "v3_rolling_restart_tip WHERE singleton=1"
                    )
                ],
                "delta_tip": [
                    list(row)
                    for row in connection.execute(
                        "SELECT delta_sequence,delta_digest,base_checkpoint_sequence "
                        "FROM v3_rolling_restart_delta_tip WHERE singleton=1"
                    )
                ],
                "reaction_cursor": [
                    list(row)
                    for row in connection.execute(
                        "SELECT * FROM v3_rolling_reaction_cursor WHERE singleton=1"
                    )
                ],
                "projection_digest": SQLiteProjectionStore.projection_digest(
                    connection
                ),
            }
        )

    def _verify_rolling_restart_connection(
        self, connection: sqlite3.Connection, *, repair_current: bool = False
    ) -> RollingRestartReceipt:
        rows = tuple(
            connection.execute(
                "SELECT * FROM v3_rolling_restart_checkpoints "
                "ORDER BY checkpoint_sequence DESC LIMIT 2"
            )
        )
        if not rows:
            raise DurableJobError("rolling restart checkpoint is missing")
        newest = rows[0]
        try:
            newest_value = self._rolling_restart_checkpoint_value(newest)
            newest_manifest = SignedManifest.from_dict(
                json.loads(str(newest["checkpoint_manifest_json"]))
            )
        except Exception as exc:
            raise DurableJobError(
                "rolling restart checkpoint integrity differs"
            ) from exc
        if (
            str(newest["checkpoint_digest"]) != newest_manifest.body_digest
            or verify_manifest(newest_manifest, self._trust_store) != newest_value
        ):
            raise DurableJobError("rolling restart checkpoint integrity differs")
        if self._restart_trust.mode is RollingRestartTrustMode.EXTERNALLY_ANCHORED:
            expected = self._restart_trust.expected_head
            if expected is None:
                raise DurableJobError("expected rolling restart head is missing")
            self._verify_rolling_restart_anchored_suffix(connection, expected, newest)
        else:
            self._verify_rolling_restart_checkpoint_history_connection(connection)
        if len(rows) == 1:
            if (
                int(newest["checkpoint_sequence"]) != 1
                or str(newest["prior_checkpoint_digest"]) != ZERO_DIGEST
            ):
                raise DurableJobError("rolling restart checkpoint lineage differs")
        else:
            prior = rows[1]
            try:
                prior_value = self._rolling_restart_checkpoint_value(prior)
                prior_manifest = SignedManifest.from_dict(
                    json.loads(str(prior["checkpoint_manifest_json"]))
                )
            except Exception as exc:
                raise DurableJobError(
                    "rolling restart checkpoint lineage differs"
                ) from exc
            if (
                int(newest["checkpoint_sequence"])
                != int(prior["checkpoint_sequence"]) + 1
                or str(newest["prior_checkpoint_digest"])
                != str(prior["checkpoint_digest"])
                or str(prior["checkpoint_digest"]) != prior_manifest.body_digest
                or verify_manifest(prior_manifest, self._trust_store) != prior_value
            ):
                raise DurableJobError("rolling restart checkpoint lineage differs")
        tip = connection.execute(
            "SELECT checkpoint_sequence,checkpoint_digest FROM v3_rolling_restart_tip "
            "WHERE singleton=1"
        ).fetchone()
        if tip is None or (int(tip[0]), str(tip[1])) != (
            int(newest["checkpoint_sequence"]),
            str(newest["checkpoint_digest"]),
        ):
            if not repair_current or not connection.in_transaction:
                raise DurableJobError("rolling restart materialized tip differs")
            connection.execute(
                "INSERT INTO v3_rolling_restart_tip VALUES (1,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "checkpoint_sequence=excluded.checkpoint_sequence,"
                "checkpoint_digest=excluded.checkpoint_digest,updated_at=excluded.updated_at",
                (
                    int(newest["checkpoint_sequence"]),
                    str(newest["checkpoint_digest"]),
                    str(newest_value["created_at"]),
                ),
            )
        if repair_current:
            event_tip = connection.execute(
                "SELECT global_sequence,event_digest FROM v3_events "
                "ORDER BY global_sequence DESC LIMIT 1"
            ).fetchone()
            observed_event_tip = (
                (0, ZERO_DIGEST)
                if event_tip is None
                else (int(event_tip[0]), str(event_tip[1]))
            )
            if observed_event_tip == (
                int(newest_value["source_global_sequence"]),
                str(newest_value["source_event_digest"]),
            ):
                self._repair_rolling_cursor_from_checkpoint(
                    connection, newest_value
                )
        delta_authorities = self._verify_rolling_restart_delta_suffix(
            connection, int(newest["checkpoint_sequence"])
        )
        if newest_value["schema_version"] == (
            "strathmark-v3-rolling-restart-checkpoint-v2"
        ):
            expected_current = self._current_subjects_after_delta_suffix(
                connection, int(newest["checkpoint_sequence"]), newest_value
            )
            observed_current = _rolling_current_material(
                connection.execute("SELECT * FROM v3_rolling_card_current")
            )
            expected_rows = tuple(
                (
                    item["competitor_id"],
                    item["target_context_digest"],
                    item["publication_digest"],
                    item["dependency_revision"],
                    item["status_digest"],
                    item["updated_at"],
                )
                for item in expected_current
            )
            if observed_current != expected_rows:
                if not repair_current or not connection.in_transaction:
                    raise DurableJobError(
                        "rolling current projection differs from delta authority"
                    )
                connection.execute("DELETE FROM v3_rolling_card_current")
                for row in expected_rows:
                    connection.execute(
                        "INSERT INTO v3_rolling_card_current VALUES (?,?,?,?,?,?)", row
                    )
        material = self._rolling_restart_material(
            connection, repair_cursor=repair_current
        )
        current_keys = (
            "current_subject_count",
            "current_subject_digest",
        )
        if any(newest_value[key] != material[key] for key in current_keys):
            status_authority = delta_authorities.get("rolling_status")
            if status_authority == (
                int(material["status_sequence"]),
                str(material["status_digest"]),
            ):
                self._verify_rolling_storage_connection(
                    connection, allow_closed_current=True
                )
            elif not repair_current or not connection.in_transaction:
                raise DurableJobError("rolling restart checkpoint material differs")
            elif newest_value["schema_version"] == (
                "strathmark-v3-rolling-restart-checkpoint-v2"
            ):
                status_tip = connection.execute(
                    "SELECT status_sequence,status_digest,observed_at "
                    "FROM v3_rolling_card_status_history "
                    "ORDER BY status_sequence DESC LIMIT 1"
                ).fetchone()
                if status_tip is None:
                    raise DurableJobError(
                        "compact rolling restart checkpoint requires authority rebuild"
                    )
                self._restore_rolling_current_from_status_authority(connection)
                self._append_rolling_restart_delta(
                    connection,
                    operation_kind="projection_rebuilt",
                    authority_kind="rolling_status",
                    authority_sequence=int(status_tip[0]),
                    authority_digest=str(status_tip[1]),
                    observed_at=str(status_tip[2]),
                )
                return self._verify_rolling_restart_connection(connection)
            else:
                self._restore_rolling_current_from_checkpoint(connection, newest_value)
                self._append_rolling_restart_checkpoint(
                    connection, str(newest_value["created_at"])
                )
                return self._verify_rolling_restart_connection(connection)
        exact_keys = ["capacity_manifest_digest"]
        status_authority = delta_authorities.get("rolling_status")
        if status_authority is None:
            exact_keys.extend(("status_sequence", "status_digest"))
        elif status_authority != (
            int(material["status_sequence"]),
            str(material["status_digest"]),
        ):
            raise DurableJobError("rolling restart status delta tip differs")
        job_history_authority = delta_authorities.get("job_history")
        if job_history_authority is None:
            exact_keys.extend(
                (
                    "job_history_sequence",
                    "job_history_digest",
                    "active_job_count",
                    "active_job_digest",
                )
            )
        elif job_history_authority != (
            int(material["job_history_sequence"]),
            str(material["job_history_digest"]),
        ):
            raise DurableJobError("rolling restart job delta tip differs")
        for key in exact_keys:
            if newest_value[key] != material[key]:
                raise DurableJobError("rolling restart checkpoint material differs")
        checkpoint_source = int(newest_value["source_global_sequence"])
        current_source = int(material["source_global_sequence"])
        if current_source == checkpoint_source:
            source_keys = [
                "source_event_digest",
                "aggregate_head_count",
                "aggregate_heads_digest",
                "reaction_cursor_digest",
                "reaction_cursor_revision",
                "reaction_relevant_command_count",
                "reaction_latest_reaction_id",
            ]
            if delta_authorities.get("reaction_completion") is None:
                source_keys.extend(
                    ("pending_reaction_count", "pending_reaction_digest")
                )
            else:
                self._verify_rolling_reactions(connection)
            for key in source_keys:
                if newest_value[key] != material[key]:
                    raise DurableJobError("rolling restart checkpoint material differs")
        elif current_source > checkpoint_source:
            if not connection.in_transaction:
                raise DurableJobError(
                    "rolling restart tail refresh requires writer transaction"
                )
            cursor = connection.execute(
                "SELECT updated_at FROM v3_rolling_reaction_cursor WHERE singleton=1"
            ).fetchone()
            if cursor is None:
                raise DurableJobError("rolling restart reaction cursor is missing")
            event_authority = (
                current_source,
                str(material["source_event_digest"]),
            )
            if delta_authorities.get("event_tail") != event_authority:
                self._append_rolling_restart_delta(
                    connection,
                    operation_kind="event_tail_verified",
                    authority_kind="event_tail",
                    authority_sequence=current_source,
                    authority_digest=event_authority[1],
                    observed_at=str(cursor[0]),
                )
        else:
            raise DurableJobError("rolling restart event cursor rolled back")
        return RollingRestartReceipt(
            int(newest["checkpoint_sequence"]),
            str(newest["checkpoint_digest"]),
            current_source,
            int(material["current_subject_count"]),
            int(material["active_job_count"]),
            int(material["pending_reaction_count"]),
            self._restart_trust.mode,
        )

    def _current_subjects_after_delta_suffix(
        self,
        connection: sqlite3.Connection,
        checkpoint_sequence: int,
        checkpoint: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        subjects = {
            (str(row[0]), str(row[1])): {
                "competitor_id": str(row[0]),
                "target_context_digest": str(row[1]),
                "tournament_epoch_id": str(row[2]),
                "publication_digest": str(row[3]),
                "dependency_revision": int(row[4]),
                "status_digest": str(row[5]),
                "updated_at": str(row[6]),
            }
            for row in connection.execute(
                "SELECT competitor_id,target_context_digest,tournament_epoch_id,"
                "publication_digest,dependency_revision,status_digest,updated_at "
                "FROM v3_rolling_restart_current_subjects WHERE checkpoint_sequence=? "
                "ORDER BY competitor_id,target_context_digest",
                (checkpoint_sequence,),
            )
        }
        initial = [subjects[key] for key in sorted(subjects)]
        if (
            len(initial) != int(checkpoint["current_subject_count"])
            or canonical_digest(initial) != checkpoint["current_subject_digest"]
        ):
            raise DurableJobError("rolling restart current snapshot differs")
        for delta in connection.execute(
            "SELECT authority_sequence FROM v3_rolling_restart_deltas "
            "WHERE base_checkpoint_sequence=? AND authority_kind='rolling_status' "
            "ORDER BY delta_sequence",
            (checkpoint_sequence,),
        ):
            status = connection.execute(
                "SELECT * FROM v3_rolling_card_status_history WHERE status_sequence=?",
                (int(delta[0]),),
            ).fetchone()
            if status is None:
                raise DurableJobError("rolling restart status delta authority is missing")
            publication = connection.execute(
                "SELECT * FROM v3_rolling_card_publications WHERE publication_digest=?",
                (str(status["publication_digest"]),),
            ).fetchone()
            if publication is None:
                raise DurableJobError("rolling restart status publication is missing")
            key = (
                str(publication["competitor_id"]),
                str(publication["target_context_digest"]),
            )
            if str(status["status"]) == "current":
                subjects[key] = {
                    "competitor_id": key[0],
                    "target_context_digest": key[1],
                    "tournament_epoch_id": str(publication["tournament_epoch_id"]),
                    "publication_digest": str(publication["publication_digest"]),
                    "dependency_revision": int(publication["dependency_revision"]),
                    "status_digest": str(status["status_digest"]),
                    "updated_at": str(status["observed_at"]),
                }
            elif subjects.get(key, {}).get("publication_digest") == str(
                publication["publication_digest"]
            ):
                subjects.pop(key)
        return [subjects[key] for key in sorted(subjects)]

    def _restore_rolling_current_from_status_authority(
        self, connection: sqlite3.Connection
    ) -> None:
        self._verify_rolling_closures(connection)
        self._verify_rolling_reactions(connection)
        expected = self._verified_rolling_current_rows(connection)
        connection.execute("DELETE FROM v3_rolling_card_current")
        for row in expected:
            connection.execute(
                "INSERT INTO v3_rolling_card_current VALUES (?,?,?,?,?,?)", tuple(row)
            )

    @staticmethod
    def _repair_rolling_cursor_from_checkpoint(
        connection: sqlite3.Connection, checkpoint: Mapping[str, Any]
    ) -> None:
        source_sequence = int(checkpoint["source_global_sequence"])
        if source_sequence == 0:
            updated_at = "1970-01-01T00:00:00.000Z"
        else:
            event = connection.execute(
                "SELECT event_digest,occurred_at_utc FROM v3_events "
                "WHERE global_sequence=?",
                (source_sequence,),
            ).fetchone()
            if event is None or str(event[0]) != checkpoint["source_event_digest"]:
                raise DurableJobError(
                    "rolling restart cursor authority differs from events"
                )
            updated_at = str(event[1])
        value = {
            "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
            "cursor_revision": int(checkpoint["reaction_cursor_revision"]),
            "through_global_sequence": source_sequence,
            "through_event_digest": str(checkpoint["source_event_digest"]),
            "relevant_command_count": int(
                checkpoint["reaction_relevant_command_count"]
            ),
            "latest_reaction_id": str(checkpoint["reaction_latest_reaction_id"]),
            "updated_at": updated_at,
        }
        if canonical_digest(value) != checkpoint["reaction_cursor_digest"]:
            raise DurableJobError("rolling restart signed cursor material differs")
        connection.execute(
            "INSERT INTO v3_rolling_reaction_cursor VALUES (1,?,?,?,?,?,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET "
            "cursor_revision=excluded.cursor_revision,"
            "through_global_sequence=excluded.through_global_sequence,"
            "through_event_digest=excluded.through_event_digest,"
            "relevant_command_count=excluded.relevant_command_count,"
            "latest_reaction_id=excluded.latest_reaction_id,"
            "cursor_digest=excluded.cursor_digest,updated_at=excluded.updated_at",
            (
                value["cursor_revision"],
                value["through_global_sequence"],
                value["through_event_digest"],
                value["relevant_command_count"],
                value["latest_reaction_id"],
                checkpoint["reaction_cursor_digest"],
                updated_at,
            ),
        )

    def _verify_rolling_restart_anchored_suffix(
        self,
        connection: sqlite3.Connection,
        expected: RollingRestartExpectedHead,
        newest: sqlite3.Row,
    ) -> None:
        newest_sequence = int(newest["checkpoint_sequence"])
        if newest_sequence < expected.checkpoint_sequence:
            raise DurableJobError("external rolling head rolled back")
        suffix_length = newest_sequence - expected.checkpoint_sequence
        if suffix_length > MAX_ROLLING_RESTART_ANCHORED_SUFFIX:
            raise DurableJobError(
                "rolling restart external anchor requires checkpoint refresh"
            )
        rows = tuple(
            connection.execute(
                "SELECT * FROM v3_rolling_restart_checkpoints "
                "WHERE checkpoint_sequence BETWEEN ? AND ? "
                "ORDER BY checkpoint_sequence",
                (expected.checkpoint_sequence, newest_sequence),
            )
        )
        if len(rows) != suffix_length + 1:
            raise DurableJobError("rolling restart anchored lineage has a gap")
        if (
            int(rows[0]["checkpoint_sequence"]) != expected.checkpoint_sequence
            or str(rows[0]["checkpoint_digest"]) != expected.checkpoint_digest
        ):
            raise DurableJobError("external rolling head differs")
        self._verify_rolling_restart_checkpoint_rows(
            connection, rows, require_genesis=False
        )

    def _verify_rolling_restart_checkpoint_history_connection(
        self, connection: sqlite3.Connection
    ) -> None:
        """Deep local corruption audit; intentionally not rollback-proof or bounded."""

        rows = tuple(
            connection.execute(
                "SELECT * FROM v3_rolling_restart_checkpoints "
                "ORDER BY checkpoint_sequence"
            )
        )
        self._verify_rolling_restart_checkpoint_rows(
            connection, rows, require_genesis=True
        )

    def _verify_rolling_restart_checkpoint_rows(
        self,
        connection: sqlite3.Connection,
        rows: tuple[sqlite3.Row, ...],
        *,
        require_genesis: bool,
    ) -> None:
        if not rows:
            raise DurableJobError("rolling restart checkpoint history is missing")
        prior_sequence = 0
        prior_digest = ZERO_DIGEST
        prior_absorbed_sequence = 0
        prior_absorbed_digest = ZERO_DIGEST
        if not require_genesis:
            prior_sequence = int(rows[0]["checkpoint_sequence"]) - 1
            prior_digest = str(rows[0]["prior_checkpoint_digest"])
        for row_index, row in enumerate(rows):
            try:
                value = self._rolling_restart_checkpoint_value(row)
                manifest = SignedManifest.from_dict(
                    json.loads(str(row["checkpoint_manifest_json"]))
                )
            except Exception as exc:
                raise DurableJobError(
                    "rolling restart checkpoint history integrity differs"
                ) from exc
            if int(row["checkpoint_sequence"]) != prior_sequence + 1:
                raise DurableJobError("rolling restart checkpoint history has a gap")
            if str(row["prior_checkpoint_digest"]) != prior_digest:
                raise DurableJobError(
                    "rolling restart checkpoint history lineage differs"
                )
            if (
                str(row["checkpoint_digest"]) != manifest.body_digest
                or verify_manifest(manifest, self._trust_store) != value
            ):
                raise DurableJobError(
                    "rolling restart checkpoint history integrity differs"
                )
            is_external_anchor = not require_genesis and row_index == 0
            if value["schema_version"] == (
                "strathmark-v3-rolling-restart-checkpoint-v2"
            ):
                absorbed_sequence = int(value["absorbed_delta_sequence"])
                absorbed_digest = str(value["absorbed_delta_digest"])
                if is_external_anchor:
                    prior_absorbed_sequence = absorbed_sequence
                    prior_absorbed_digest = absorbed_digest
                    prior_sequence = int(row["checkpoint_sequence"])
                    prior_digest = str(row["checkpoint_digest"])
                    continue
                deltas = tuple(
                    connection.execute(
                        "SELECT delta_sequence,prior_delta_digest,delta_digest FROM "
                        "v3_rolling_restart_deltas WHERE delta_sequence>? AND "
                        "delta_sequence<=? ORDER BY delta_sequence",
                        (prior_absorbed_sequence, absorbed_sequence),
                    )
                )
                if len(deltas) != absorbed_sequence - prior_absorbed_sequence:
                    raise DurableJobError(
                        "rolling restart checkpoint delta lineage has a gap"
                    )
                expected_delta_prior = prior_absorbed_digest
                for expected, delta in enumerate(
                    deltas, start=prior_absorbed_sequence + 1
                ):
                    if (
                        int(delta[0]) != expected
                        or str(delta[1]) != expected_delta_prior
                    ):
                        raise DurableJobError(
                            "rolling restart checkpoint delta lineage differs"
                        )
                    expected_delta_prior = str(delta[2])
                if expected_delta_prior != absorbed_digest:
                    raise DurableJobError(
                        "rolling restart checkpoint absorbed delta differs"
                    )
                prior_absorbed_sequence = absorbed_sequence
                prior_absorbed_digest = absorbed_digest
            prior_sequence = int(row["checkpoint_sequence"])
            prior_digest = str(row["checkpoint_digest"])

    @staticmethod
    def _rolling_restart_checkpoint_value(row: sqlite3.Row) -> dict[str, Any]:
        manifest = SignedManifest.from_dict(
            json.loads(str(row["checkpoint_manifest_json"]))
        )
        body = manifest.body()
        payload = body.get("payload")
        if isinstance(payload, dict) and payload.get("schema_version") == (
            "strathmark-v3-rolling-restart-checkpoint-v2"
        ):
            if any(
                str(row[column]) != "[]"
                for column in (
                    "aggregate_heads_json",
                    "current_subjects_json",
                    "pending_reactions_json",
                )
            ):
                raise DurableJobError(
                    "compact rolling restart checkpoint embeds projection rows"
                )
            return {
                "schema_version": "strathmark-v3-rolling-restart-checkpoint-v2",
                "checkpoint_sequence": int(row["checkpoint_sequence"]),
                "prior_checkpoint_digest": str(row["prior_checkpoint_digest"]),
                "capacity_manifest_digest": str(row["capacity_manifest_digest"]),
                "source_global_sequence": int(row["source_global_sequence"]),
                "source_event_digest": str(row["source_event_digest"]),
                "aggregate_head_count": int(row["aggregate_head_count"]),
                "aggregate_heads_digest": str(row["aggregate_heads_digest"]),
                "reaction_cursor_digest": str(row["reaction_cursor_digest"]),
                "reaction_cursor_revision": int(row["reaction_cursor_revision"]),
                "reaction_relevant_command_count": int(
                    row["reaction_relevant_command_count"]
                ),
                "reaction_latest_reaction_id": str(row["reaction_latest_reaction_id"]),
                "job_history_sequence": int(row["job_history_sequence"]),
                "job_history_digest": str(row["job_history_digest"]),
                "status_sequence": int(row["status_sequence"]),
                "status_digest": str(row["status_digest"]),
                "current_subject_count": int(row["current_subject_count"]),
                "current_subject_digest": str(row["current_subject_digest"]),
                "active_job_count": int(row["active_job_count"]),
                "active_job_digest": str(row["active_job_digest"]),
                "pending_reaction_count": int(row["pending_reaction_count"]),
                "pending_reaction_digest": str(row["pending_reaction_digest"]),
                "absorbed_delta_sequence": int(row["absorbed_delta_sequence"]),
                "absorbed_delta_digest": str(row["absorbed_delta_digest"]),
                "created_at": str(row["created_at"]),
            }
        current_subjects_json = str(row["current_subjects_json"])
        current_subjects = json.loads(current_subjects_json)
        if canonical_bytes(current_subjects).decode("utf-8") != current_subjects_json:
            raise DurableJobError(
                "rolling restart checkpoint current subjects are not canonical"
            )
        aggregate_heads_json = str(row["aggregate_heads_json"])
        aggregate_heads = json.loads(aggregate_heads_json)
        pending_reactions_json = str(row["pending_reactions_json"])
        pending_reactions = json.loads(pending_reactions_json)
        if (
            canonical_bytes(aggregate_heads).decode("utf-8") != aggregate_heads_json
            or canonical_bytes(pending_reactions).decode("utf-8")
            != pending_reactions_json
        ):
            raise DurableJobError(
                "rolling restart checkpoint bounded material is not canonical"
            )
        return {
            "schema_version": "strathmark-v3-rolling-restart-checkpoint-v1",
            "checkpoint_sequence": int(row["checkpoint_sequence"]),
            "prior_checkpoint_digest": str(row["prior_checkpoint_digest"]),
            "capacity_manifest_digest": str(row["capacity_manifest_digest"]),
            "source_global_sequence": int(row["source_global_sequence"]),
            "source_event_digest": str(row["source_event_digest"]),
            "aggregate_heads": aggregate_heads,
            "aggregate_head_count": int(row["aggregate_head_count"]),
            "aggregate_heads_digest": str(row["aggregate_heads_digest"]),
            "reaction_cursor_digest": str(row["reaction_cursor_digest"]),
            "reaction_cursor_revision": int(row["reaction_cursor_revision"]),
            "reaction_relevant_command_count": int(
                row["reaction_relevant_command_count"]
            ),
            "reaction_latest_reaction_id": str(row["reaction_latest_reaction_id"]),
            "job_history_sequence": int(row["job_history_sequence"]),
            "job_history_digest": str(row["job_history_digest"]),
            "status_sequence": int(row["status_sequence"]),
            "status_digest": str(row["status_digest"]),
            "current_subjects": current_subjects,
            "current_subject_count": int(row["current_subject_count"]),
            "current_subject_digest": str(row["current_subject_digest"]),
            "active_job_count": int(row["active_job_count"]),
            "active_job_digest": str(row["active_job_digest"]),
            "pending_reactions": pending_reactions,
            "pending_reaction_count": int(row["pending_reaction_count"]),
            "pending_reaction_digest": str(row["pending_reaction_digest"]),
            "created_at": str(row["created_at"]),
        }

    def _append_rolling_restart_checkpoint(
        self,
        connection: sqlite3.Connection,
        observed_at: str,
        *,
        aggregate_heads_override: list[dict[str, Any]] | None = None,
        prepared_material: dict[str, Any] | None = None,
    ) -> RollingRestartReceipt:
        if not connection.in_transaction:
            raise DurableJobError(
                "rolling restart checkpoint requires writer transaction"
            )
        created_at = require_utc_milliseconds(observed_at)
        material = (
            self._rolling_restart_material(
                connection, aggregate_heads_override=aggregate_heads_override
            )
            if prepared_material is None
            else prepared_material
        )
        compact_aggregate_heads = [
            {
                key: item[key]
                for key in (
                    "aggregate_kind",
                    "aggregate_id",
                    "aggregate_version",
                    "event_digest",
                )
            }
            for item in material["aggregate_heads"]
        ]
        material["aggregate_head_count"] = len(compact_aggregate_heads)
        material["aggregate_heads_digest"] = canonical_digest(compact_aggregate_heads)
        prior = connection.execute(
            "SELECT checkpoint_sequence,checkpoint_digest "
            "FROM v3_rolling_restart_checkpoints ORDER BY checkpoint_sequence DESC LIMIT 1"
        ).fetchone()
        sequence = 1 if prior is None else int(prior[0]) + 1
        prior_digest = ZERO_DIGEST if prior is None else str(prior[1])
        delta_tip = connection.execute(
            "SELECT delta_sequence,delta_digest FROM v3_rolling_restart_deltas "
            "ORDER BY delta_sequence DESC LIMIT 1"
        ).fetchone()
        absorbed_delta_sequence = 0 if delta_tip is None else int(delta_tip[0])
        absorbed_delta_digest = ZERO_DIGEST if delta_tip is None else str(delta_tip[1])
        value = {
            "schema_version": "strathmark-v3-rolling-restart-checkpoint-v2",
            "checkpoint_sequence": sequence,
            "prior_checkpoint_digest": prior_digest,
            **{
                key: value
                for key, value in material.items()
                if key
                not in {"aggregate_heads", "current_subjects", "pending_reactions"}
            },
            "absorbed_delta_sequence": absorbed_delta_sequence,
            "absorbed_delta_digest": absorbed_delta_digest,
            "created_at": created_at,
        }
        manifest = sign_manifest(
            "rolling_restart_checkpoint",
            value,
            signer=self._signer,
            created_at=created_at,
        )
        encoded = canonical_bytes(manifest.to_dict()).decode("utf-8")
        connection.execute(
            "INSERT INTO v3_rolling_restart_checkpoints VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sequence,
                prior_digest,
                material["capacity_manifest_digest"],
                material["source_global_sequence"],
                material["source_event_digest"],
                "[]",
                material["aggregate_head_count"],
                material["aggregate_heads_digest"],
                material["reaction_cursor_digest"],
                material["reaction_cursor_revision"],
                material["reaction_relevant_command_count"],
                material["reaction_latest_reaction_id"],
                material["job_history_sequence"],
                material["job_history_digest"],
                material["status_sequence"],
                material["status_digest"],
                "[]",
                material["current_subject_count"],
                material["current_subject_digest"],
                material["active_job_count"],
                material["active_job_digest"],
                "[]",
                material["pending_reaction_count"],
                material["pending_reaction_digest"],
                manifest.body_digest,
                encoded,
                created_at,
                absorbed_delta_sequence,
                absorbed_delta_digest,
            ),
        )
        for head in material["aggregate_heads"]:
            connection.execute(
                "INSERT INTO v3_rolling_restart_aggregate_heads VALUES (?,?,?,?,?,?)",
                (
                    sequence,
                    head["aggregate_kind"],
                    head["aggregate_id"],
                    head["aggregate_version"],
                    head["event_digest"],
                    head.get("lifecycle_status"),
                ),
            )
        for reaction in material["pending_reactions"]:
            connection.execute(
                "INSERT INTO v3_rolling_restart_pending_reactions VALUES (?,?,?,?,?)",
                (
                    sequence,
                    reaction["reaction_id"],
                    reaction["first_global_sequence"],
                    reaction["last_global_sequence"],
                    reaction["event_set_digest"],
                ),
            )
        for subject in material["current_subjects"]:
            connection.execute(
                "INSERT INTO v3_rolling_restart_current_subjects VALUES "
                "(?,?,?,?,?,?,?,?)",
                (
                    sequence,
                    subject["competitor_id"],
                    subject["target_context_digest"],
                    subject["tournament_epoch_id"],
                    subject["publication_digest"],
                    subject["dependency_revision"],
                    subject["status_digest"],
                    subject["updated_at"],
                ),
            )
        connection.execute(
            "INSERT INTO v3_rolling_restart_tip VALUES (1,?,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET "
            "checkpoint_sequence=excluded.checkpoint_sequence,"
            "checkpoint_digest=excluded.checkpoint_digest,updated_at=excluded.updated_at",
            (sequence, manifest.body_digest, created_at),
        )
        return RollingRestartReceipt(
            sequence,
            manifest.body_digest,
            int(material["source_global_sequence"]),
            int(material["current_subject_count"]),
            int(material["active_job_count"]),
            int(material["pending_reaction_count"]),
            self._restart_trust.mode,
        )

    def _append_rolling_restart_delta(
        self,
        connection: sqlite3.Connection,
        *,
        operation_kind: str,
        authority_kind: str,
        authority_sequence: int,
        authority_digest: str,
        observed_at: str,
    ) -> str:
        if not connection.in_transaction:
            raise DurableJobError("rolling restart delta requires writer transaction")
        _require_token(operation_kind, "rolling restart delta operation")
        _require_token(authority_kind, "rolling restart delta authority")
        _digest(authority_digest, "rolling restart delta authority")
        created_at = require_utc_milliseconds(observed_at)
        checkpoint = connection.execute(
            "SELECT checkpoint_sequence FROM v3_rolling_restart_checkpoints "
            "ORDER BY checkpoint_sequence DESC LIMIT 1"
        ).fetchone()
        if checkpoint is None:
            raise DurableJobError("rolling restart delta lacks a base checkpoint")
        suffix_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_rolling_restart_deltas "
                "WHERE base_checkpoint_sequence=?",
                (int(checkpoint[0]),),
            ).fetchone()[0]
        )
        if suffix_count >= MAX_ROLLING_RESTART_DELTA_SUFFIX:
            raise DurableJobError(
                "rolling restart delta suffix requires checkpoint refresh"
            )
        prior = connection.execute(
            "SELECT delta_sequence,delta_digest FROM v3_rolling_restart_deltas "
            "ORDER BY delta_sequence DESC LIMIT 1"
        ).fetchone()
        sequence = 1 if prior is None else int(prior[0]) + 1
        prior_digest = ZERO_DIGEST if prior is None else str(prior[1])
        value = {
            "schema_version": "strathmark-v3-rolling-restart-delta-v1",
            "delta_sequence": sequence,
            "prior_delta_digest": prior_digest,
            "base_checkpoint_sequence": int(checkpoint[0]),
            "operation_kind": operation_kind,
            "authority_kind": authority_kind,
            "authority_sequence": authority_sequence,
            "authority_digest": authority_digest,
            "created_at": created_at,
        }
        manifest = sign_manifest(
            "rolling_restart_delta", value, signer=self._signer, created_at=created_at
        )
        connection.execute(
            "INSERT INTO v3_rolling_restart_deltas VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sequence,
                prior_digest,
                int(checkpoint[0]),
                operation_kind,
                authority_kind,
                authority_sequence,
                authority_digest,
                manifest.body_digest,
                canonical_bytes(manifest.to_dict()).decode("utf-8"),
                created_at,
            ),
        )
        connection.execute(
            "INSERT INTO v3_rolling_restart_delta_tip VALUES (1,?,?,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET "
            "delta_sequence=excluded.delta_sequence,"
            "delta_digest=excluded.delta_digest,"
            "base_checkpoint_sequence=excluded.base_checkpoint_sequence,"
            "updated_at=excluded.updated_at",
            (sequence, manifest.body_digest, int(checkpoint[0]), created_at),
        )
        return manifest.body_digest

    def _verify_rolling_restart_delta_suffix(
        self, connection: sqlite3.Connection, base_checkpoint_sequence: int
    ) -> dict[str, tuple[int, str]]:
        rows = tuple(
            connection.execute(
                "SELECT * FROM v3_rolling_restart_deltas "
                "WHERE base_checkpoint_sequence=? ORDER BY delta_sequence LIMIT ?",
                (base_checkpoint_sequence, MAX_ROLLING_RESTART_DELTA_SUFFIX + 1),
            )
        )
        if len(rows) > MAX_ROLLING_RESTART_DELTA_SUFFIX:
            raise DurableJobError(
                "rolling restart delta suffix exceeds refresh threshold"
            )
        if not rows:
            return {}
        first_sequence = int(rows[0]["delta_sequence"])
        predecessor = (
            None
            if first_sequence == 1
            else connection.execute(
                "SELECT delta_digest FROM v3_rolling_restart_deltas "
                "WHERE delta_sequence=?",
                (first_sequence - 1,),
            ).fetchone()
        )
        prior = ZERO_DIGEST if predecessor is None else str(predecessor[0])
        latest: dict[str, tuple[int, str]] = {}
        for expected_sequence, row in enumerate(rows, start=first_sequence):
            value = {
                "schema_version": "strathmark-v3-rolling-restart-delta-v1",
                "delta_sequence": int(row["delta_sequence"]),
                "prior_delta_digest": str(row["prior_delta_digest"]),
                "base_checkpoint_sequence": int(row["base_checkpoint_sequence"]),
                "operation_kind": str(row["operation_kind"]),
                "authority_kind": str(row["authority_kind"]),
                "authority_sequence": int(row["authority_sequence"]),
                "authority_digest": str(row["authority_digest"]),
                "created_at": str(row["created_at"]),
            }
            try:
                manifest = SignedManifest.from_dict(
                    json.loads(str(row["delta_manifest_json"]))
                )
            except Exception as exc:
                raise DurableJobError("rolling restart delta integrity differs") from exc
            if (
                int(row["delta_sequence"]) != expected_sequence
                or str(row["prior_delta_digest"]) != prior
                or str(row["delta_digest"]) != manifest.body_digest
                or verify_manifest(manifest, self._trust_store) != value
            ):
                raise DurableJobError("rolling restart delta integrity differs")
            self._verify_rolling_restart_delta_authority(connection, value)
            latest[value["authority_kind"]] = (
                value["authority_sequence"],
                value["authority_digest"],
            )
            prior = str(row["delta_digest"])
        tip = connection.execute(
            "SELECT delta_sequence,delta_digest,base_checkpoint_sequence "
            "FROM v3_rolling_restart_delta_tip WHERE singleton=1"
        ).fetchone()
        if tip is None or (
            int(tip[0]),
            str(tip[1]),
            int(tip[2]),
        ) != (
            int(rows[-1]["delta_sequence"]),
            str(rows[-1]["delta_digest"]),
            base_checkpoint_sequence,
        ):
            raise DurableJobError("rolling restart delta tip differs")
        return latest

    def _verify_rolling_restart_delta_authority(
        self, connection: sqlite3.Connection, value: Mapping[str, Any]
    ) -> None:
        kind = value["authority_kind"]
        sequence = int(value["authority_sequence"])
        digest = str(value["authority_digest"])
        if kind == "job_history":
            row = connection.execute(
                "SELECT * FROM v3_job_history WHERE history_sequence=?", (sequence,)
            ).fetchone()
            if row is None or str(row["history_digest"]) != digest:
                raise DurableJobError("rolling restart delta authority differs")
            authority_value = _history_value(row)
            manifest = SignedManifest(
                "job_transition",
                str(row["auth_body_json"]),
                str(row["auth_body_digest"]),
                str(row["auth_key_id"]),
                str(row["auth_signature_der_b64"]),
            )
            if verify_manifest(manifest, self._trust_store) != authority_value:
                raise DurableJobError("rolling restart delta authority differs")
            return
        if kind == "rolling_status":
            row = connection.execute(
                "SELECT * FROM v3_rolling_card_status_history WHERE status_sequence=?",
                (sequence,),
            ).fetchone()
            if row is None or str(row["status_digest"]) != digest:
                raise DurableJobError("rolling status integrity differs")
            manifest = SignedManifest.from_dict(
                json.loads(str(row["status_manifest_json"]))
            )
            if verify_manifest(manifest, self._trust_store) != _rolling_status_value(row):
                raise DurableJobError("rolling status integrity differs")
            return
        if kind == "reaction_completion":
            row = connection.execute(
                "SELECT completion.*,obligation.* FROM v3_rolling_reaction_completions "
                "completion JOIN v3_rolling_reaction_obligations obligation "
                "USING(reaction_id) WHERE obligation.last_global_sequence=?",
                (sequence,),
            ).fetchone()
            if row is None or str(row["completion_digest"]) != digest:
                raise DurableJobError("rolling restart delta authority differs")
            obligation = self._verify_rolling_reaction_row(connection, row)
            self._verify_rolling_reaction_completion(row, obligation)
            return
        if kind == "epoch_closure":
            row = connection.execute(
                "SELECT * FROM v3_rolling_epoch_closures WHERE source_global_sequence=?",
                (sequence,),
            ).fetchone()
            if row is None:
                raise DurableJobError("rolling restart delta authority differs")
            manifest = SignedManifest.from_dict(
                json.loads(str(row["closure_manifest_json"]))
            )
            if manifest.body_digest != digest:
                raise DurableJobError("rolling restart delta authority differs")
            self._verify_rolling_closures(connection)
            return
        if kind == "event_tail":
            row = connection.execute(
                "SELECT event_digest,envelope_json FROM v3_events "
                "WHERE global_sequence=?",
                (sequence,),
            ).fetchone()
            if row is None or str(row[0]) != digest:
                raise DurableJobError("rolling restart delta authority differs")
            event = EventEnvelope.from_dict(json.loads(str(row[1])))
            if event.event_digest != digest:
                raise DurableJobError("rolling restart delta authority differs")
            return
        raise DurableJobError("rolling restart delta authority kind is unsupported")

    @staticmethod
    def _bootstrap_rolling_reaction_cursor(connection: sqlite3.Connection) -> None:
        """Install the one-time cursor after a successful genesis audit."""

        event = connection.execute(
            "SELECT global_sequence,event_digest,occurred_at_utc FROM v3_events "
            "ORDER BY global_sequence DESC LIMIT 1"
        ).fetchone()
        command_count = int(
            connection.execute(
                "SELECT COUNT(DISTINCT command_id) FROM v3_events"
            ).fetchone()[0]
        )
        reaction_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_rolling_reaction_obligations"
            ).fetchone()[0]
        )
        reaction = connection.execute(
            "SELECT reaction_id FROM v3_rolling_reaction_obligations "
            "ORDER BY first_global_sequence DESC LIMIT 1"
        ).fetchone()
        value = {
            "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
            "cursor_revision": command_count,
            "through_global_sequence": 0 if event is None else int(event[0]),
            "through_event_digest": ZERO_DIGEST if event is None else str(event[1]),
            "relevant_command_count": reaction_count,
            "latest_reaction_id": ZERO_DIGEST if reaction is None else str(reaction[0]),
            "updated_at": (
                "1970-01-01T00:00:00.000Z" if event is None else str(event[2])
            ),
        }
        connection.execute(
            "UPDATE v3_rolling_reaction_cursor SET cursor_revision=?,"
            "through_global_sequence=?,through_event_digest=?,relevant_command_count=?,"
            "latest_reaction_id=?,cursor_digest=?,updated_at=? WHERE singleton=1",
            (
                value["cursor_revision"],
                value["through_global_sequence"],
                value["through_event_digest"],
                value["relevant_command_count"],
                value["latest_reaction_id"],
                canonical_digest(value),
                value["updated_at"],
            ),
        )

    def _rolling_restart_material(
        self,
        connection: sqlite3.Connection,
        *,
        aggregate_heads_override: list[dict[str, Any]] | None = None,
        repair_cursor: bool = False,
    ) -> dict[str, Any]:
        event_row = connection.execute(
            "SELECT global_sequence,event_digest,envelope_json FROM v3_events "
            "ORDER BY global_sequence DESC LIMIT 1"
        ).fetchone()
        if event_row is None:
            source_sequence, source_digest = 0, ZERO_DIGEST
        else:
            event = EventEnvelope.from_dict(json.loads(str(event_row[2])))
            if event.global_sequence != int(event_row[0]) or event.event_digest != str(
                event_row[1]
            ):
                raise DurableJobError("rolling restart event tip differs")
            source_sequence, source_digest = event.global_sequence, event.event_digest
        if aggregate_heads_override is None:
            aggregate_heads = self._rolling_aggregate_heads_material(
                connection,
                source_sequence,
                source_digest,
                repair_cursor=repair_cursor,
            )
        else:
            aggregate_heads = self._verify_prepared_rolling_aggregate_heads(
                connection, aggregate_heads_override
            )

        cursor = connection.execute(
            "SELECT * FROM v3_rolling_reaction_cursor WHERE singleton=1"
        ).fetchone()
        if cursor is None:
            raise DurableJobError("rolling restart reaction cursor is missing")
        cursor_value = {
            "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
            "cursor_revision": int(cursor["cursor_revision"]),
            "through_global_sequence": int(cursor["through_global_sequence"]),
            "through_event_digest": str(cursor["through_event_digest"]),
            "relevant_command_count": int(cursor["relevant_command_count"]),
            "latest_reaction_id": str(cursor["latest_reaction_id"]),
            "updated_at": str(cursor["updated_at"]),
        }
        if (
            canonical_digest(cursor_value) != str(cursor["cursor_digest"])
            or int(cursor["through_global_sequence"]) != source_sequence
            or str(cursor["through_event_digest"]) != source_digest
        ):
            raise DurableJobError("rolling restart reaction cursor differs")

        history_rows = tuple(
            connection.execute(
                "SELECT * FROM v3_job_history ORDER BY history_sequence DESC LIMIT 2"
            )
        )
        self._verify_job_history_tip(history_rows)
        history_sequence = (
            0 if not history_rows else int(history_rows[0]["history_sequence"])
        )
        history_digest = (
            ZERO_DIGEST if not history_rows else str(history_rows[0]["history_digest"])
        )

        self._verify_rolling_status_tip(connection)
        status_row = connection.execute(
            "SELECT status_sequence,status_digest FROM v3_rolling_card_status_history "
            "ORDER BY status_sequence DESC LIMIT 1"
        ).fetchone()
        status_sequence = 0 if status_row is None else int(status_row[0])
        status_digest = ZERO_DIGEST if status_row is None else str(status_row[1])

        current_rows = tuple(
            connection.execute(
                "SELECT current.*,publication.tournament_epoch_id "
                "FROM v3_rolling_card_current current JOIN "
                "v3_rolling_card_publications publication USING(publication_digest) "
                "ORDER BY current.competitor_id,current.target_context_digest LIMIT ?",
                (self.capacity.max_context_cards + 1,),
            )
        )
        if len(current_rows) > self.capacity.max_context_cards:
            raise DurableJobError("rolling restart current subjects exceed capacity")
        current_material: list[dict[str, Any]] = []
        for row in current_rows:
            publication = connection.execute(
                "SELECT * FROM v3_rolling_card_publications WHERE publication_digest=?",
                (row["publication_digest"],),
            ).fetchone()
            status = connection.execute(
                "SELECT * FROM v3_rolling_card_status_history WHERE status_digest=?",
                (row["status_digest"],),
            ).fetchone()
            if publication is None or status is None:
                raise DurableJobError("rolling restart current authority is missing")
            self._verify_rolling_publication_material(publication)
            status_value = _rolling_status_value(status)
            status_manifest = SignedManifest.from_dict(
                json.loads(str(status["status_manifest_json"]))
            )
            if (
                str(status["status"]) != "current"
                or str(status["publication_digest"]) != str(row["publication_digest"])
                or canonical_digest(status_value) != str(status["status_digest"])
                or verify_manifest(status_manifest, self._trust_store) != status_value
            ):
                raise DurableJobError("rolling restart current authority differs")
            current_material.append(
                {
                    "competitor_id": str(row["competitor_id"]),
                    "target_context_digest": str(row["target_context_digest"]),
                    "tournament_epoch_id": str(row["tournament_epoch_id"]),
                    "publication_digest": str(row["publication_digest"]),
                    "dependency_revision": int(row["dependency_revision"]),
                    "status_digest": str(row["status_digest"]),
                    "updated_at": str(row["updated_at"]),
                }
            )

        active_rows = tuple(
            connection.execute(
                "SELECT * FROM v3_jobs WHERE state IN "
                "('queued','leased','retryable-failed') AND "
                "json_extract(payload_json, '$.schema_version') IN (?,?) "
                "ORDER BY job_id,job_revision LIMIT ?",
                (
                    "strathmark-v3-rolling-component-job-v1",
                    "strathmark-v3-weight-only-recombination-v1",
                    self.capacity.max_queued_jobs + 1,
                ),
            )
        )
        if len(active_rows) > self.capacity.max_queued_jobs:
            raise DurableJobError("rolling restart active jobs exceed capacity")
        active_records = self._verify_job_rows_local(connection, active_rows)
        active_material = [
            {
                "job_id": record.job_id,
                "job_revision": record.job_revision,
                "state": record.state.value,
                "fencing_token": record.fencing_token,
                "job_material_digest": _record_material_digest(record),
            }
            for record in active_records
        ]

        pending_material = self._rolling_pending_material(connection)
        checkpoint = connection.execute(
            "SELECT checkpoint_manifest_json FROM v3_rolling_restart_checkpoints "
            "ORDER BY checkpoint_sequence DESC LIMIT 1"
        ).fetchone()
        compact_heads = checkpoint is None
        if checkpoint is not None:
            try:
                payload = SignedManifest.from_dict(
                    json.loads(str(checkpoint[0]))
                ).body().get("payload")
                compact_heads = isinstance(payload, dict) and payload.get(
                    "schema_version"
                ) == "strathmark-v3-rolling-restart-checkpoint-v2"
            except Exception as exc:
                raise DurableJobError(
                    "rolling restart checkpoint integrity differs"
                ) from exc
        aggregate_digest_material = (
            [
                {
                    key: item[key]
                    for key in (
                        "aggregate_kind",
                        "aggregate_id",
                        "aggregate_version",
                        "event_digest",
                    )
                }
                for item in aggregate_heads
            ]
            if compact_heads
            else aggregate_heads
        )
        return {
            "capacity_manifest_digest": self.capacity.digest,
            "source_global_sequence": source_sequence,
            "source_event_digest": source_digest,
            "aggregate_heads": aggregate_heads,
            "aggregate_head_count": len(aggregate_heads),
            "aggregate_heads_digest": canonical_digest(aggregate_digest_material),
            "reaction_cursor_digest": str(cursor["cursor_digest"]),
            "reaction_cursor_revision": int(cursor["cursor_revision"]),
            "reaction_relevant_command_count": int(cursor["relevant_command_count"]),
            "reaction_latest_reaction_id": str(cursor["latest_reaction_id"]),
            "job_history_sequence": history_sequence,
            "job_history_digest": history_digest,
            "status_sequence": status_sequence,
            "status_digest": status_digest,
            "current_subjects": current_material,
            "current_subject_count": len(current_material),
            "current_subject_digest": canonical_digest(current_material),
            "active_job_count": len(active_material),
            "active_job_digest": canonical_digest(active_material),
            "pending_reactions": pending_material,
            "pending_reaction_count": len(pending_material),
            "pending_reaction_digest": canonical_digest(pending_material),
        }

    def _rolling_aggregate_heads_material(
        self,
        connection: sqlite3.Connection,
        source_sequence: int,
        source_digest: str,
        *,
        repair_cursor: bool = False,
    ) -> list[dict[str, Any]]:
        checkpoint = connection.execute(
            "SELECT * FROM v3_rolling_restart_checkpoints "
            "ORDER BY checkpoint_sequence DESC LIMIT 1"
        ).fetchone()
        if checkpoint is None:
            heads: dict[tuple[str, str], dict[str, Any]] = {}
            rows = tuple(
                connection.execute(
                    "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
                )
            )
            for row in rows:
                event = EventEnvelope.from_dict(json.loads(str(row[0])))
                self._advance_rolling_aggregate_head(heads, event)
        else:
            value = self._rolling_restart_checkpoint_value(checkpoint)
            checkpoint_source = int(value["source_global_sequence"])
            if source_sequence < checkpoint_source:
                raise DurableJobError("rolling restart event cursor rolled back")
            if value["schema_version"] == (
                "strathmark-v3-rolling-restart-checkpoint-v2"
            ):
                heads = self._rolling_checkpoint_aggregate_heads(
                    connection, int(checkpoint["checkpoint_sequence"]), value
                )
                if source_sequence > checkpoint_source:
                    heads = self._verify_rolling_event_tail(
                        connection,
                        value,
                        heads,
                        source_sequence,
                        source_digest,
                        repair_cursor=repair_cursor,
                    )
                elif source_digest != value["source_event_digest"]:
                    raise DurableJobError("rolling restart event tip differs")
                material = [heads[key] for key in sorted(heads)]
                return self._verify_prepared_rolling_aggregate_heads(
                    connection, material
                )
            heads = self._decode_rolling_aggregate_heads(value)
            if source_sequence > checkpoint_source:
                heads = self._verify_rolling_event_tail(
                    connection,
                    value,
                    heads,
                    source_sequence,
                    source_digest,
                    repair_cursor=repair_cursor,
                )
            elif source_digest != value["source_event_digest"]:
                raise DurableJobError("rolling restart event tip differs")
        material = [heads[key] for key in sorted(heads)]
        return self._verify_prepared_rolling_aggregate_heads(connection, material)

    def _compact_rolling_aggregate_heads(
        self, connection: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        capacity = self._rolling_aggregate_head_capacity()
        rows = tuple(
            connection.execute(
                "SELECT aggregate_kind,aggregate_id,aggregate_version,event_digest "
                "FROM v3_aggregate_heads ORDER BY aggregate_kind,aggregate_id LIMIT ?",
                (capacity + 1,),
            )
        )
        if len(rows) > capacity:
            raise DurableJobError(
                "rolling restart aggregate heads exceed bounded capacity"
            )
        return [
            {
                "aggregate_kind": str(row[0]),
                "aggregate_id": str(row[1]),
                "aggregate_version": int(row[2]),
                "event_digest": str(row[3]),
            }
            for row in rows
        ]

    def _rolling_checkpoint_aggregate_heads(
        self,
        connection: sqlite3.Connection,
        checkpoint_sequence: int,
        checkpoint: Mapping[str, Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        rows = tuple(
            connection.execute(
                "SELECT aggregate_kind,aggregate_id,aggregate_version,event_digest,"
                "lifecycle_status FROM v3_rolling_restart_aggregate_heads "
                "WHERE checkpoint_sequence=? ORDER BY aggregate_kind,aggregate_id",
                (checkpoint_sequence,),
            )
        )
        material = [
            {
                "aggregate_kind": str(row[0]),
                "aggregate_id": str(row[1]),
                "aggregate_version": int(row[2]),
                "event_digest": str(row[3]),
                "lifecycle_status": None if row[4] is None else str(row[4]),
            }
            for row in rows
        ]
        compact = [
            {key: item[key] for key in (
                "aggregate_kind",
                "aggregate_id",
                "aggregate_version",
                "event_digest",
            )}
            for item in material
        ]
        if (
            len(material) != int(checkpoint["aggregate_head_count"])
            or canonical_digest(compact) != checkpoint["aggregate_heads_digest"]
        ):
            raise DurableJobError("rolling restart aggregate-head snapshot differs")
        return self._decode_rolling_aggregate_heads(
            {
                "aggregate_heads": material,
                "aggregate_head_count": len(material),
                "aggregate_heads_digest": canonical_digest(material),
            }
        )

    def _verify_prepared_rolling_aggregate_heads(
        self,
        connection: sqlite3.Connection,
        material: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if material and set(material[0]) == {
            "aggregate_kind",
            "aggregate_id",
            "aggregate_version",
            "event_digest",
        }:
            if any(
                set(item)
                != {
                    "aggregate_kind",
                    "aggregate_id",
                    "aggregate_version",
                    "event_digest",
                }
                for item in material
            ):
                raise DurableJobError("rolling restart aggregate heads differ")
        else:
            self._decode_rolling_aggregate_heads(
                {
                    "aggregate_heads": material,
                    "aggregate_head_count": len(material),
                    "aggregate_heads_digest": canonical_digest(material),
                }
            )
        aggregate_head_capacity = self._rolling_aggregate_head_capacity()
        if len(material) > aggregate_head_capacity:
            raise DurableJobError(
                "rolling restart aggregate heads exceed bounded capacity"
            )
        observed = tuple(
            connection.execute(
                "SELECT aggregate_kind,aggregate_id,aggregate_version,event_digest "
                "FROM v3_aggregate_heads ORDER BY aggregate_kind,aggregate_id LIMIT ?",
                (aggregate_head_capacity + 1,),
            )
        )
        if len(observed) != len(material) or any(
            (
                str(row[0]),
                str(row[1]),
                int(row[2]),
                str(row[3]),
            )
            != (
                item["aggregate_kind"],
                item["aggregate_id"],
                item["aggregate_version"],
                item["event_digest"],
            )
            for row, item in zip(observed, material, strict=True)
        ):
            raise DurableJobError("rolling restart aggregate heads differ")
        return material

    def _decode_rolling_aggregate_heads(
        self,
        checkpoint: Mapping[str, Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        values = checkpoint.get("aggregate_heads")
        if (
            not isinstance(values, list)
            or len(values) != checkpoint.get("aggregate_head_count")
            or len(values) > self._rolling_aggregate_head_capacity()
            or canonical_digest(values) != checkpoint.get("aggregate_heads_digest")
        ):
            raise DurableJobError("rolling restart aggregate-head snapshot differs")
        heads: dict[tuple[str, str], dict[str, Any]] = {}
        for value in values:
            if not isinstance(value, dict) or set(value) != {
                "aggregate_kind",
                "aggregate_id",
                "aggregate_version",
                "event_digest",
                "lifecycle_status",
            }:
                raise DurableJobError("rolling restart aggregate-head snapshot differs")
            try:
                aggregate_kind = AggregateKind(value["aggregate_kind"])
                StableIdentifier(value["aggregate_id"])
                version = int(value["aggregate_version"])
            except (TypeError, ValueError) as exc:
                raise DurableJobError(
                    "rolling restart aggregate-head snapshot differs"
                ) from exc
            status = value["lifecycle_status"]
            if status is not None:
                try:
                    LifecycleStatus(status)
                except (TypeError, ValueError) as exc:
                    raise DurableJobError(
                        "rolling restart aggregate-head snapshot differs"
                    ) from exc
            key = (aggregate_kind.value, value["aggregate_id"])
            if key in heads or version <= 0:
                raise DurableJobError("rolling restart aggregate-head snapshot differs")
            _digest(value["event_digest"], "rolling aggregate head")
            heads[key] = value
        if [heads[key] for key in sorted(heads)] != values:
            raise DurableJobError(
                "rolling restart aggregate-head snapshot is not sorted"
            )
        return heads

    def _rolling_aggregate_head_capacity(self) -> int:
        return min(
            MAX_ROLLING_RESTART_AGGREGATE_HEADS,
            self.capacity.max_context_cards * 16 + 256,
        )

    def _rolling_event_tail_capacity(self) -> int:
        return min(
            MAX_ROLLING_RESTART_EVENT_TAIL,
            self.capacity.max_context_cards * 5 + 16,
        )

    @staticmethod
    def _advance_rolling_aggregate_head(
        heads: dict[tuple[str, str], dict[str, Any]], event: EventEnvelope
    ) -> None:
        key = (event.aggregate_kind.value, str(event.aggregate_id))
        prior = heads.get(key)
        version = 0 if prior is None else int(prior["aggregate_version"])
        digest = ZERO_DIGEST if prior is None else str(prior["event_digest"])
        status_value = None if prior is None else prior["lifecycle_status"]
        if (
            event.aggregate_version != version + 1
            or event.prior_aggregate_digest != digest
        ):
            raise DurableJobError("rolling restart aggregate tail differs")
        if event.kind is EventKind.HISTORY_IMPORTED:
            next_status = status_value
        else:
            try:
                current = (
                    None if status_value is None else LifecycleStatus(status_value)
                )
                next_status = transition(
                    event.aggregate_kind, current, event.kind
                ).value
            except ContractError as exc:
                raise DurableJobError(
                    "rolling restart event tail lifecycle is illegal"
                ) from exc
        heads[key] = {
            "aggregate_kind": event.aggregate_kind.value,
            "aggregate_id": str(event.aggregate_id),
            "aggregate_version": event.aggregate_version,
            "event_digest": event.event_digest,
            "lifecycle_status": next_status,
        }

    def _verify_rolling_event_tail(
        self,
        connection: sqlite3.Connection,
        checkpoint: Mapping[str, Any],
        heads: dict[tuple[str, str], dict[str, Any]],
        source_sequence: int,
        source_digest: str,
        *,
        repair_cursor: bool = False,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        first = int(checkpoint["source_global_sequence"]) + 1
        tail_length = source_sequence - first + 1
        if tail_length > self._rolling_event_tail_capacity():
            raise DurableJobError(
                "rolling restart event tail requires authenticated deep audit"
            )
        rows = tuple(
            connection.execute(
                "SELECT global_sequence,event_id,aggregate_kind,aggregate_id,"
                "aggregate_version,event_kind,envelope_json,event_digest,"
                "prior_global_digest,prior_aggregate_digest,occurred_at_utc,command_id,"
                "source_import_id,training_eligible FROM v3_events "
                "WHERE global_sequence BETWEEN ? AND ? ORDER BY global_sequence",
                (first, source_sequence),
            )
        )
        if len(rows) != tail_length:
            raise DurableJobError("rolling restart event tail has a gap")
        prior_global = str(checkpoint["source_event_digest"])
        events: list[EventEnvelope] = []
        for expected_sequence, row in enumerate(rows, start=first):
            try:
                raw = str(row[6])
                value = json.loads(raw)
                event = EventEnvelope.from_dict(value)
            except Exception as exc:
                raise DurableJobError(
                    "rolling restart event tail is malformed"
                ) from exc
            expected_event_id = deterministic_identifier(
                "event",
                {
                    "command_digest": canonical_digest(event.command.to_dict()),
                    "aggregate_id": str(event.aggregate_id),
                    "aggregate_version": event.aggregate_version,
                    "event_kind": event.kind.value,
                },
            )
            persisted = (
                event.global_sequence,
                str(event.event_id),
                event.aggregate_kind.value,
                str(event.aggregate_id),
                event.aggregate_version,
                event.kind.value,
                canonical_bytes(event.to_dict()).decode("utf-8"),
                event.event_digest,
                event.prior_global_digest,
                event.prior_aggregate_digest,
                event.occurred_at_utc,
                str(event.command.command_id),
            )
            if (
                event.global_sequence != expected_sequence
                or event.event_id != expected_event_id
                or tuple(row[:12]) != persisted
                or raw != persisted[6]
                or event.prior_global_digest != prior_global
                or (event.kind is EventKind.HISTORY_IMPORTED) != (row[12] is not None)
                or int(row[13]) not in (0, 1)
            ):
                raise DurableJobError("rolling restart event tail integrity differs")
            self._advance_rolling_aggregate_head(heads, event)
            prior_global = event.event_digest
            events.append(event)
        if prior_global != source_digest:
            raise DurableJobError("rolling restart event tail tip differs")
        groups: list[tuple[EventEnvelope, ...]] = []
        for event in events:
            if (
                not groups
                or groups[-1][0].command.command_id != event.command.command_id
            ):
                groups.append((event,))
            else:
                groups[-1] = (*groups[-1], event)
        for group in groups:
            self._verify_rolling_tail_idempotency(connection, group)
        overlapping = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_idempotency_records "
                "WHERE last_global_sequence>=? AND first_global_sequence<=?",
                (first, source_sequence),
            ).fetchone()[0]
        )
        if overlapping != len(groups):
            raise DurableJobError("rolling restart event tail idempotency differs")
        self._verify_rolling_tail_reactions(
            connection, checkpoint, groups, repair_cursor=repair_cursor
        )
        return heads

    @staticmethod
    def _verify_rolling_tail_idempotency(
        connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]
    ) -> None:
        command = events[0].command
        rows = tuple(
            connection.execute(
                "SELECT principal_id,idempotency_key,command_digest,result_schema_version,"
                "result_json,result_digest,first_global_sequence,last_global_sequence,"
                "event_set_digest,created_at FROM v3_idempotency_records "
                "WHERE idempotency_key=?",
                (str(command.command_id),),
            )
        )
        if len(rows) != 1:
            raise DurableJobError("rolling restart event tail idempotency differs")
        row = rows[0]
        try:
            result_json = str(row[4])
            result_value = json.loads(result_json)
            intents = tuple(
                EventIntent(event.aggregate_kind, event.aggregate_id, event.kind)
                for event in events
            )
            validate_command_event_intents(command, intents)
            require_utc_milliseconds(str(row[9]))
        except Exception as exc:
            raise DurableJobError(
                "rolling restart event tail idempotency differs"
            ) from exc
        event_set_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-event-set-v1",
                "events": [
                    {
                        "global_sequence": event.global_sequence,
                        "event_id": str(event.event_id),
                        "event_digest": event.event_digest,
                    }
                    for event in events
                ],
            }
        )
        if (
            str(row[0]) != str(command.actor_id)
            or str(row[1]) != str(command.command_id)
            or str(row[2]) != canonical_digest(command.to_dict())
            or not str(row[3])
            or canonical_bytes(result_value).decode("utf-8") != result_json
            or canonical_digest(result_value) != str(row[5])
            or int(row[6]) != events[0].global_sequence
            or int(row[7]) != events[-1].global_sequence
            or str(row[8]) != event_set_digest
            or any(event.command != command for event in events)
        ):
            raise DurableJobError("rolling restart event tail idempotency differs")

    def _verify_rolling_tail_reactions(
        self,
        connection: sqlite3.Connection,
        checkpoint: Mapping[str, Any],
        groups: list[tuple[EventEnvelope, ...]],
        *,
        repair_cursor: bool = False,
    ) -> None:
        prior_pending = checkpoint.get("pending_reactions")
        if checkpoint.get("schema_version") == (
            "strathmark-v3-rolling-restart-checkpoint-v2"
        ):
            prior_pending = [
                {
                    "reaction_id": str(row[0]),
                    "first_global_sequence": int(row[1]),
                    "last_global_sequence": int(row[2]),
                    "event_set_digest": str(row[3]),
                }
                for row in connection.execute(
                    "SELECT reaction_id,first_global_sequence,last_global_sequence,"
                    "event_set_digest FROM v3_rolling_restart_pending_reactions "
                    "WHERE checkpoint_sequence=? ORDER BY first_global_sequence",
                    (int(checkpoint["checkpoint_sequence"]),),
                )
            ]
        if (
            not isinstance(prior_pending, list)
            or len(prior_pending) != checkpoint.get("pending_reaction_count")
            or canonical_digest(prior_pending)
            != checkpoint.get("pending_reaction_digest")
        ):
            raise DurableJobError("rolling restart pending snapshot differs")
        expected_pending = []
        for item in prior_pending:
            completion = connection.execute(
                "SELECT completion.*,obligation.* FROM "
                "v3_rolling_reaction_completions completion JOIN "
                "v3_rolling_reaction_obligations obligation USING(reaction_id) "
                "WHERE reaction_id=?",
                (item["reaction_id"],),
            ).fetchone()
            if completion is None:
                expected_pending.append(item)
            else:
                obligation = self._verify_rolling_reaction_row(
                    connection, completion
                )
                self._verify_rolling_reaction_completion(completion, obligation)
        relevant_count = int(checkpoint["reaction_relevant_command_count"])
        latest_reaction_id = str(checkpoint["reaction_latest_reaction_id"])
        for group in groups:
            reaction_id = rolling_reaction_identity(group)
            if reaction_id is None:
                continue
            row = connection.execute(
                "SELECT * FROM v3_rolling_reaction_obligations WHERE reaction_id=?",
                (reaction_id,),
            ).fetchone()
            if row is None:
                raise DurableJobError("rolling restart reaction obligation is missing")
            verified = self._verify_rolling_reaction_row(connection, row)
            completion = connection.execute(
                "SELECT completion.*,obligation.* FROM "
                "v3_rolling_reaction_completions completion JOIN "
                "v3_rolling_reaction_obligations obligation USING(reaction_id) "
                "WHERE reaction_id=?",
                (reaction_id,),
            ).fetchone()
            if completion is None:
                expected_pending.append(
                    {
                        "reaction_id": reaction_id,
                        "first_global_sequence": verified["first_global_sequence"],
                        "last_global_sequence": verified["last_global_sequence"],
                        "event_set_digest": verified["event_set_digest"],
                    }
                )
            else:
                self._verify_rolling_reaction_completion(completion, verified)
            relevant_count += 1
            latest_reaction_id = reaction_id
        expected_pending.sort(key=lambda item: item["first_global_sequence"])
        if expected_pending != self._rolling_pending_material(connection):
            raise DurableJobError("rolling restart reaction obligations differ")
        cursor = connection.execute(
            "SELECT * FROM v3_rolling_reaction_cursor WHERE singleton=1"
        ).fetchone()
        expected_cursor = {
            "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
            "cursor_revision": int(checkpoint["reaction_cursor_revision"])
            + len(groups),
            "through_global_sequence": groups[-1][-1].global_sequence,
            "through_event_digest": groups[-1][-1].event_digest,
            "relevant_command_count": relevant_count,
            "latest_reaction_id": latest_reaction_id,
            "updated_at": groups[-1][-1].occurred_at_utc,
        }
        differs = cursor is None or (
            int(cursor["cursor_revision"]) != expected_cursor["cursor_revision"]
            or int(cursor["through_global_sequence"])
            != expected_cursor["through_global_sequence"]
            or str(cursor["through_event_digest"])
            != expected_cursor["through_event_digest"]
            or int(cursor["relevant_command_count"])
            != expected_cursor["relevant_command_count"]
            or str(cursor["latest_reaction_id"])
            != expected_cursor["latest_reaction_id"]
            or str(cursor["updated_at"]) != expected_cursor["updated_at"]
            or str(cursor["cursor_digest"]) != canonical_digest(expected_cursor)
        )
        if differs and repair_cursor and connection.in_transaction:
            self._write_rolling_cursor_value(connection, expected_cursor)
            return
        if differs:
            raise DurableJobError("rolling restart reaction cursor differs")

    def _restore_rolling_current_from_checkpoint(
        self, connection: sqlite3.Connection, checkpoint: Mapping[str, Any]
    ) -> None:
        subjects = checkpoint.get("current_subjects")
        if not isinstance(subjects, list) or len(subjects) != checkpoint.get(
            "current_subject_count"
        ):
            raise DurableJobError("rolling restart current snapshot differs")
        if len(subjects) > self.capacity.max_context_cards or canonical_digest(
            subjects
        ) != checkpoint.get("current_subject_digest"):
            raise DurableJobError("rolling restart current snapshot differs")
        restored: list[tuple[Any, ...]] = []
        for subject in subjects:
            if not isinstance(subject, dict) or set(subject) != {
                "competitor_id",
                "target_context_digest",
                "tournament_epoch_id",
                "publication_digest",
                "dependency_revision",
                "status_digest",
                "updated_at",
            }:
                raise DurableJobError("rolling restart current snapshot differs")
            publication = connection.execute(
                "SELECT * FROM v3_rolling_card_publications WHERE publication_digest=?",
                (subject["publication_digest"],),
            ).fetchone()
            status = connection.execute(
                "SELECT * FROM v3_rolling_card_status_history WHERE status_digest=?",
                (subject["status_digest"],),
            ).fetchone()
            if publication is None or status is None:
                raise DurableJobError(
                    "rolling restart current snapshot authority is missing"
                )
            self._verify_rolling_publication_material(publication)
            status_value = _rolling_status_value(status)
            status_manifest = SignedManifest.from_dict(
                json.loads(str(status["status_manifest_json"]))
            )
            if (
                str(publication["competitor_id"]) != subject["competitor_id"]
                or str(publication["target_context_digest"])
                != subject["target_context_digest"]
                or str(publication["tournament_epoch_id"])
                != subject["tournament_epoch_id"]
                or int(publication["dependency_revision"])
                != subject["dependency_revision"]
                or str(status["publication_digest"]) != subject["publication_digest"]
                or str(status["status"]) != "current"
                or canonical_digest(status_value) != subject["status_digest"]
                or verify_manifest(status_manifest, self._trust_store) != status_value
            ):
                raise DurableJobError(
                    "rolling restart current snapshot authority differs"
                )
            restored.append(
                (
                    subject["competitor_id"],
                    subject["target_context_digest"],
                    subject["publication_digest"],
                    subject["dependency_revision"],
                    subject["status_digest"],
                    subject["updated_at"],
                )
            )
        connection.execute("DELETE FROM v3_rolling_card_current")
        for row in restored:
            connection.execute(
                "INSERT INTO v3_rolling_card_current VALUES (?,?,?,?,?,?)", row
            )

    def _rolling_pending_material(
        self,
        connection: sqlite3.Connection,
        *,
        through_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> list[dict[str, Any]]:
        if through_sequence is not None and after_sequence is not None:
            raise DurableJobError("rolling pending range is ambiguous")
        predicate = ""
        parameters: list[Any] = []
        if through_sequence is not None:
            predicate = "AND obligation.first_global_sequence<=? "
            parameters.append(through_sequence)
        elif after_sequence is not None:
            predicate = "AND obligation.first_global_sequence>? "
            parameters.append(after_sequence)
        parameters.append(self.capacity.max_context_cards + 1)
        rows = tuple(
            connection.execute(
                "SELECT obligation.* FROM v3_rolling_reaction_obligations obligation "
                "LEFT JOIN v3_rolling_reaction_completions completion USING(reaction_id) "
                "WHERE completion.reaction_id IS NULL "
                f"{predicate}ORDER BY first_global_sequence LIMIT ?",
                tuple(parameters),
            )
        )
        if len(rows) > self.capacity.max_context_cards:
            raise DurableJobError("rolling restart pending reactions exceed capacity")
        material: list[dict[str, Any]] = []
        for row in rows:
            verified = self._verify_rolling_reaction_row(connection, row)
            material.append(
                {
                    "reaction_id": verified["reaction_id"],
                    "first_global_sequence": verified["first_global_sequence"],
                    "last_global_sequence": verified["last_global_sequence"],
                    "event_set_digest": verified["event_set_digest"],
                }
            )
        return material

    def _verify_job_history_tip(self, rows: tuple[sqlite3.Row, ...]) -> None:
        for row in rows:
            value = _history_value(row)
            manifest = SignedManifest(
                "job_transition",
                str(row["auth_body_json"]),
                str(row["auth_body_digest"]),
                str(row["auth_key_id"]),
                str(row["auth_signature_der_b64"]),
            )
            if (
                str(row["history_digest"]) != canonical_digest(value)
                or verify_manifest(manifest, self._trust_store) != value
            ):
                raise DurableJobError("rolling restart job-history tip differs")
        if len(rows) == 1 and (
            int(rows[0]["history_sequence"]) != 1
            or str(rows[0]["prior_history_digest"]) != ZERO_DIGEST
        ):
            raise DurableJobError("rolling restart job-history lineage differs")
        if len(rows) == 2 and (
            int(rows[0]["history_sequence"]) != int(rows[1]["history_sequence"]) + 1
            or str(rows[0]["prior_history_digest"]) != str(rows[1]["history_digest"])
        ):
            raise DurableJobError("rolling restart job-history lineage differs")

    def commit_rolling_publication(
        self,
        row: Mapping[str, Any],
        *,
        expected_jobs: tuple[JobRecord, ...],
        observed_at: str,
    ) -> dict[str, Any]:
        now = require_utc_milliseconds(observed_at)
        expected_columns = {
            "publication_digest",
            "card_digest",
            "competitor_id",
            "target_context_digest",
            "dependency_revision",
            "tournament_epoch_id",
            "bundle_digest",
            "evidence_digest",
            "hard_deadline_at",
            "sealed_at",
            "authority_json",
            "authority_digest",
            "component_refs_json",
            "component_refs_digest",
            "availability_json",
            "availability_digest",
            "council_manifest_digest",
            "council_aggregate_manifest_json",
            "publication_manifest_json",
        }
        if not isinstance(row, Mapping) or set(row) != expected_columns:
            raise DurableJobError("rolling publication storage fields differ")
        if not isinstance(expected_jobs, tuple) or len(expected_jobs) != 5:
            raise DurableJobError("rolling publication requires five durable jobs")
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                self._verify_rolling_status_tip(connection)
                current_jobs = tuple(
                    self._get_connection(connection, item.job_id, item.job_revision)
                    for item in expected_jobs
                )
                self._verify_job_records_local(connection, current_jobs)
                if current_jobs != expected_jobs:
                    raise DurableJobError(
                        "component authority changed before card publication"
                    )
                existing = connection.execute(
                    "SELECT * FROM v3_rolling_card_publications WHERE publication_digest=?",
                    (row["publication_digest"],),
                ).fetchone()
                if existing is not None:
                    stored = dict(existing)
                    if stored != dict(row):
                        raise DurableJobError("rolling publication digest conflicts")
                    return stored
                if (
                    connection.execute(
                        "SELECT 1 FROM v3_rolling_epoch_closures WHERE epoch_id=?",
                        (row["tournament_epoch_id"],),
                    ).fetchone()
                    is not None
                ):
                    raise DurableJobError("closed epoch card cannot be published")
                scheduled = connection.execute(
                    "SELECT payload_json FROM v3_jobs "
                    "WHERE json_extract(payload_json, '$.schema_version')=? "
                    "AND json_extract(payload_json, '$.card_key.competitor_id')=? "
                    "AND json_extract(payload_json, '$.card_key.target_context_digest')=? "
                    "ORDER BY CAST(json_extract(payload_json, "
                    "'$.card_key.dependency_revision') AS INTEGER) DESC LIMIT 6",
                    (
                        "strathmark-v3-rolling-component-job-v1",
                        row["competitor_id"],
                        row["target_context_digest"],
                    ),
                ).fetchall()
                if not scheduled:
                    raise DurableJobError(
                        "rolling publication has no scheduled authority"
                    )
                scheduled_keys = tuple(
                    json.loads(str(item[0]))["card_key"] for item in scheduled
                )
                maximum = scheduled_keys[0]["dependency_revision"]
                current = tuple(
                    item
                    for item in scheduled_keys
                    if item["dependency_revision"] == maximum
                )
                if (
                    len({item["card_digest"] for item in current}) != 1
                    or maximum != row["dependency_revision"]
                    or current[0]["card_digest"] != row["card_digest"]
                ):
                    raise DurableJobError(
                        "superseded card publication cannot become current"
                    )
                self._verify_rolling_restart_connection(connection, repair_current=True)
                prior = connection.execute(
                    "SELECT publication_digest,dependency_revision FROM v3_rolling_card_current "
                    "WHERE competitor_id=? AND target_context_digest=?",
                    (row["competitor_id"], row["target_context_digest"]),
                ).fetchone()
                if prior is not None and int(prior[1]) >= int(
                    row["dependency_revision"]
                ):
                    raise DurableJobError("rolling card publication is not monotonic")
                columns = tuple(row)
                connection.execute(
                    f"INSERT INTO v3_rolling_card_publications({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    tuple(row[name] for name in columns),
                )
                if prior is not None:
                    self._append_rolling_status(
                        connection,
                        str(prior[0]),
                        "superseded",
                        "dependency_superseded",
                        now,
                    )
                    connection.execute(
                        "DELETE FROM v3_rolling_card_current WHERE competitor_id=? "
                        "AND target_context_digest=?",
                        (row["competitor_id"], row["target_context_digest"]),
                    )
                status_digest = self._append_rolling_status(
                    connection,
                    str(row["publication_digest"]),
                    "current",
                    "card_sealed",
                    now,
                )
                connection.execute(
                    "INSERT INTO v3_rolling_card_current VALUES (?,?,?,?,?,?)",
                    (
                        row["competitor_id"],
                        row["target_context_digest"],
                        row["publication_digest"],
                        row["dependency_revision"],
                        status_digest,
                        now,
                    ),
                )
                stored = connection.execute(
                    "SELECT * FROM v3_rolling_card_publications WHERE publication_digest=?",
                    (row["publication_digest"],),
                ).fetchone()
                assert stored is not None
                return dict(stored)

    def supersede_rolling_publication(
        self,
        *,
        publication_digest: str,
        competitor_id: str,
        target_context_digest: str,
        observed_at: str,
        reason: str,
    ) -> None:
        _digest(publication_digest, "rolling publication")
        _require_reason(reason)
        now = require_utc_milliseconds(observed_at)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                self._verify_rolling_status_tip(connection)
                self._verify_rolling_restart_connection(connection, repair_current=True)
                row = connection.execute(
                    "SELECT publication_digest FROM v3_rolling_card_current "
                    "WHERE competitor_id=? AND target_context_digest=?",
                    (competitor_id, target_context_digest),
                ).fetchone()
                if row is None:
                    return
                if str(row[0]) != publication_digest:
                    raise DurableJobError(
                        "rolling supersession current pointer differs"
                    )
                self._append_rolling_status(
                    connection, publication_digest, "superseded", reason, now
                )
                connection.execute(
                    "DELETE FROM v3_rolling_card_current WHERE competitor_id=? "
                    "AND target_context_digest=?",
                    (competitor_id, target_context_digest),
                )

    def verify_rolling_storage(self, *, allow_closed_current: bool = False) -> None:
        if not isinstance(allow_closed_current, bool):
            raise DurableJobError("closed-current verification mode must be explicit")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            self._verify_rolling_storage_connection(
                connection, allow_closed_current=allow_closed_current
            )

    def rebuild_rolling_current_projection(self) -> int:
        """Restore the disposable current index from verified signed history."""

        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                self._verify_rolling_closures(connection)
                self._verify_rolling_reactions(connection)
                expected = self._verified_rolling_current_rows(connection)
                observed = connection.execute(
                    "SELECT * FROM v3_rolling_card_current"
                ).fetchall()
                if _rolling_current_material(observed) == expected:
                    return 0
                connection.execute("DELETE FROM v3_rolling_card_current")
                for row in expected:
                    connection.execute(
                        "INSERT INTO v3_rolling_card_current VALUES (?,?,?,?,?,?)",
                        tuple(row),
                    )
                status_tip = connection.execute(
                    "SELECT status_sequence,status_digest,observed_at FROM "
                    "v3_rolling_card_status_history ORDER BY status_sequence DESC LIMIT 1"
                ).fetchone()
                if status_tip is not None:
                    self._append_rolling_restart_delta(
                        connection,
                        operation_kind="projection_rebuilt",
                        authority_kind="rolling_status",
                        authority_sequence=int(status_tip[0]),
                        authority_digest=str(status_tip[1]),
                        observed_at=str(status_tip[2]),
                    )
                return 1

    def _verify_rolling_storage_connection(
        self,
        connection: sqlite3.Connection,
        *,
        allow_closed_current: bool = False,
    ) -> None:
        self._verify_rolling_closures(connection)
        self._verify_rolling_reactions(connection)
        expected_rows = self._verified_rolling_current_rows(connection)
        publications = {
            str(row["publication_digest"]): row
            for row in connection.execute("SELECT * FROM v3_rolling_card_publications")
        }
        current = connection.execute("SELECT * FROM v3_rolling_card_current").fetchall()
        if _rolling_current_material(current) != expected_rows:
            raise DurableJobError(
                "rolling current projection differs from status history"
            )
        for row in current:
            source = publications[str(row["publication_digest"])]
            closed = connection.execute(
                "SELECT 1 FROM v3_rolling_epoch_closures WHERE epoch_id=?",
                (source["tournament_epoch_id"],),
            ).fetchone()
            if closed is not None and not allow_closed_current:
                raise DurableJobError("closed epoch publication remains current")

    def _verified_rolling_current_rows(
        self, connection: sqlite3.Connection
    ) -> tuple[tuple[Any, ...], ...]:
        publications = {
            str(row["publication_digest"]): row
            for row in connection.execute("SELECT * FROM v3_rolling_card_publications")
        }
        for row in publications.values():
            self._verify_rolling_publication_material(row)
        prior = ZERO_DIGEST
        latest: dict[str, tuple[str, str, str]] = {}
        for expected, row in enumerate(
            connection.execute(
                "SELECT * FROM v3_rolling_card_status_history ORDER BY status_sequence"
            ),
            start=1,
        ):
            value = _rolling_status_value(row)
            manifest = SignedManifest.from_dict(
                json.loads(str(row["status_manifest_json"]))
            )
            if (
                int(row["status_sequence"]) != expected
                or str(row["publication_digest"]) not in publications
                or str(row["prior_status_digest"]) != prior
                or str(row["status_digest"]) != canonical_digest(value)
                or verify_manifest(manifest, self._trust_store) != value
            ):
                raise DurableJobError("rolling status history integrity differs")
            prior = str(row["status_digest"])
            latest[str(row["publication_digest"])] = (
                str(row["status"]),
                str(row["status_digest"]),
                str(row["observed_at"]),
            )
        rows = []
        subjects: set[tuple[str, str]] = set()
        for digest, (status, status_digest, observed_at) in latest.items():
            if status != "current":
                continue
            source = publications[digest]
            subject = (
                str(source["competitor_id"]),
                str(source["target_context_digest"]),
            )
            if subject in subjects:
                raise DurableJobError(
                    "rolling status history has two current authorities"
                )
            subjects.add(subject)
            rows.append(
                (
                    subject[0],
                    subject[1],
                    digest,
                    int(source["dependency_revision"]),
                    status_digest,
                    observed_at,
                )
            )
        return tuple(sorted(rows))

    def _verify_rolling_publication_material(self, row: sqlite3.Row) -> None:
        try:
            authority = json.loads(str(row["authority_json"]))
            components = json.loads(str(row["component_refs_json"]))
            availability = json.loads(str(row["availability_json"]))
            manifest = SignedManifest.from_dict(
                json.loads(str(row["publication_manifest_json"]))
            )
            body = verify_manifest(manifest, self._trust_store)
        except Exception as exc:
            raise DurableJobError("rolling publication authority is corrupt") from exc
        if (
            canonical_digest(authority) != str(row["authority_digest"])
            or canonical_digest(components) != str(row["component_refs_digest"])
            or canonical_digest(availability) != str(row["availability_digest"])
            or body.get("publication_digest") != str(row["publication_digest"])
            or body.get("card_key", {}).get("card_digest") != str(row["card_digest"])
        ):
            raise DurableJobError("rolling publication authority differs")

    def _verify_rolling_status_tip(self, connection: sqlite3.Connection) -> None:
        """Verify the authenticated status-chain tip before a bounded hot write."""

        rows = tuple(
            connection.execute(
                "SELECT * FROM v3_rolling_card_status_history "
                "ORDER BY status_sequence DESC LIMIT 2"
            )
        )
        if not rows:
            if connection.execute(
                "SELECT 1 FROM v3_rolling_card_current LIMIT 1"
            ).fetchone():
                raise DurableJobError(
                    "rolling current projection exists without status authority"
                )
            return
        newest = rows[0]
        newest_value = _rolling_status_value(newest)
        newest_manifest = SignedManifest.from_dict(
            json.loads(str(newest["status_manifest_json"]))
        )
        if (
            str(newest["status_digest"]) != canonical_digest(newest_value)
            or verify_manifest(newest_manifest, self._trust_store) != newest_value
        ):
            raise DurableJobError("rolling status tip integrity differs")
        if len(rows) == 1:
            if (
                int(newest["status_sequence"]) != 1
                or str(newest["prior_status_digest"]) != ZERO_DIGEST
            ):
                raise DurableJobError("rolling status tip lineage differs")
            return
        prior = rows[1]
        prior_value = _rolling_status_value(prior)
        prior_manifest = SignedManifest.from_dict(
            json.loads(str(prior["status_manifest_json"]))
        )
        if (
            int(newest["status_sequence"]) != int(prior["status_sequence"]) + 1
            or str(newest["prior_status_digest"]) != str(prior["status_digest"])
            or str(prior["status_digest"]) != canonical_digest(prior_value)
            or verify_manifest(prior_manifest, self._trust_store) != prior_value
        ):
            raise DurableJobError("rolling status tip lineage differs")

    def _repair_rolling_current_subject(
        self,
        connection: sqlite3.Connection,
        competitor_id: str,
        target_context_digest: str,
    ) -> None:
        """Compare one current pointer with its signed per-subject status authority."""

        publications = tuple(
            connection.execute(
                "SELECT * FROM v3_rolling_card_publications WHERE competitor_id=? "
                "AND target_context_digest=? ORDER BY dependency_revision",
                (competitor_id, target_context_digest),
            )
        )
        publication_by_digest = {
            str(row["publication_digest"]): row for row in publications
        }
        for publication in publications:
            self._verify_rolling_publication_material(publication)
        latest: dict[str, sqlite3.Row] = {}
        for status in connection.execute(
            "SELECT status.* FROM v3_rolling_card_status_history status "
            "JOIN v3_rolling_card_publications publication "
            "ON publication.publication_digest=status.publication_digest "
            "WHERE publication.competitor_id=? AND publication.target_context_digest=? "
            "ORDER BY status.status_sequence",
            (competitor_id, target_context_digest),
        ):
            value = _rolling_status_value(status)
            manifest = SignedManifest.from_dict(
                json.loads(str(status["status_manifest_json"]))
            )
            if (
                str(status["status_digest"]) != canonical_digest(value)
                or verify_manifest(manifest, self._trust_store) != value
            ):
                raise DurableJobError("rolling subject status authority differs")
            latest[str(status["publication_digest"])] = status
        current_statuses = tuple(
            status for status in latest.values() if str(status["status"]) == "current"
        )
        if len(current_statuses) > 1:
            raise DurableJobError("rolling subject has two current authorities")
        expected: tuple[Any, ...] | None = None
        if current_statuses:
            status = current_statuses[0]
            publication = publication_by_digest[str(status["publication_digest"])]
            expected = (
                competitor_id,
                target_context_digest,
                str(publication["publication_digest"]),
                int(publication["dependency_revision"]),
                str(status["status_digest"]),
                str(status["observed_at"]),
            )
        observed = connection.execute(
            "SELECT * FROM v3_rolling_card_current WHERE competitor_id=? "
            "AND target_context_digest=?",
            (competitor_id, target_context_digest),
        ).fetchone()
        observed_value = None if observed is None else tuple(observed)
        if observed_value == expected:
            return
        connection.execute(
            "DELETE FROM v3_rolling_card_current WHERE competitor_id=? "
            "AND target_context_digest=?",
            (competitor_id, target_context_digest),
        )
        if expected is not None:
            connection.execute(
                "INSERT INTO v3_rolling_card_current VALUES (?,?,?,?,?,?)", expected
            )

    def _verify_rolling_reactions(self, connection: sqlite3.Connection) -> None:
        from itertools import groupby

        from strathmark.v3.contracts.events import EventEnvelope, EventKind

        relevant = {
            EventKind.FIELD_ROSTER_REVISED,
            EventKind.FIELD_SUPERSEDED,
            EventKind.RESULT_RECORDED,
            EventKind.RESULT_SUPERSEDED,
            EventKind.ROUND_EPOCH_FROZEN,
            EventKind.ROUND_CLOSED,
            EventKind.TOURNAMENT_CLOSED,
        }
        events = tuple(
            EventEnvelope.from_dict(json.loads(str(row[0])))
            for row in connection.execute(
                "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
            )
        )
        expected: dict[str, tuple[EventEnvelope, ...]] = {}
        for command_id, grouped in groupby(
            events, key=lambda event: str(event.command.command_id)
        ):
            batch = tuple(grouped)
            if any(event.kind in relevant for event in batch):
                refs = [
                    {
                        "event_id": str(event.event_id),
                        "event_digest": event.event_digest,
                        "global_sequence": event.global_sequence,
                    }
                    for event in batch
                ]
                event_set_digest = canonical_digest(
                    {
                        "schema_version": "strathmark-v3-rolling-reaction-event-set-v1",
                        "events": refs,
                    }
                )
                expected[
                    canonical_digest(
                        {
                            "source_command_id": command_id,
                            "event_set_digest": event_set_digest,
                        }
                    )
                ] = batch
        rows = tuple(
            connection.execute(
                "SELECT obligation.*,completion.plan_digest,completion.completed_at,"
                "completion.completion_digest,completion.completion_manifest_json "
                "FROM v3_rolling_reaction_obligations obligation LEFT JOIN "
                "v3_rolling_reaction_completions completion USING(reaction_id) "
                "ORDER BY first_global_sequence"
            )
        )
        if {str(row["reaction_id"]) for row in rows} != set(expected):
            raise DurableJobError("rolling reaction obligation coverage differs")
        for row in rows:
            obligation = self._verify_rolling_reaction_row(connection, row)
            batch = expected[obligation["reaction_id"]]
            if (
                obligation["first_global_sequence"] != batch[0].global_sequence
                or obligation["last_global_sequence"] != batch[-1].global_sequence
                or obligation["event_ids"]
                != tuple(str(event.event_id) for event in batch)
            ):
                raise DurableJobError("rolling reaction obligation event set differs")
            if row["completion_digest"] is not None:
                self._verify_rolling_reaction_completion(row, obligation)

    def _verify_rolling_closures(self, connection: sqlite3.Connection) -> None:
        from strathmark.v3.contracts.events import EventEnvelope

        for row in connection.execute(
            "SELECT * FROM v3_rolling_epoch_closures ORDER BY epoch_id"
        ):
            source = connection.execute(
                "SELECT event_digest,envelope_json FROM v3_events WHERE global_sequence=?",
                (row["source_global_sequence"],),
            ).fetchone()
            if source is None:
                raise DurableJobError("rolling epoch closure source event is missing")
            event = EventEnvelope.from_dict(json.loads(str(source[1])))
            value = {
                "schema_version": "strathmark-v3-rolling-epoch-closure-v1",
                "epoch_id": str(row["epoch_id"]),
                "source_event_digest": str(row["source_event_digest"]),
                "source_global_sequence": int(row["source_global_sequence"]),
                "source_event_kind": str(row["source_event_kind"]),
                "closed_at": str(row["closed_at"]),
            }
            manifest = SignedManifest.from_dict(
                json.loads(str(row["closure_manifest_json"]))
            )
            if (
                str(source[0]) != event.event_digest
                or event.event_digest != value["source_event_digest"]
                or event.global_sequence != value["source_global_sequence"]
                or event.kind.value != value["source_event_kind"]
                or event.occurred_at_utc != value["closed_at"]
                or verify_manifest(manifest, self._trust_store) != value
                or not self._rolling_close_lineage(connection, value["epoch_id"], event)
            ):
                raise DurableJobError("rolling epoch closure authority differs")

    @staticmethod
    def _rolling_close_lineage(
        connection: sqlite3.Connection, epoch_id: str, event: Any
    ) -> bool:
        from strathmark.v3.contracts.events import EventKind

        lineage = connection.execute(
            "SELECT ingress.tournament_id,epoch.round_id FROM v3_evidence_epochs epoch "
            "JOIN v3_ingress_snapshots ingress ON ingress.entity_kind='round' "
            "AND ingress.entity_id=epoch.round_id WHERE epoch.epoch_id=? "
            "ORDER BY ingress.upstream_revision DESC LIMIT 1",
            (epoch_id,),
        ).fetchone()
        return lineage is not None and (
            (
                event.kind is EventKind.TOURNAMENT_CLOSED
                and str(event.aggregate_id) == str(lineage[0])
            )
            or (
                event.kind is EventKind.ROUND_CLOSED
                and str(event.aggregate_id) == str(lineage[1])
            )
        )

    def _append_rolling_status(
        self,
        connection: sqlite3.Connection,
        publication_digest: str,
        status: str,
        reason: str,
        observed_at: str,
    ) -> str:
        self._verify_rolling_status_tip(connection)
        row = connection.execute(
            "SELECT status_sequence,status_digest FROM v3_rolling_card_status_history "
            "ORDER BY status_sequence DESC LIMIT 1"
        ).fetchone()
        sequence = 1 if row is None else int(row[0]) + 1
        prior = ZERO_DIGEST if row is None else str(row[1])
        value = {
            "schema_version": "strathmark-v3-rolling-card-status-v1",
            "status_sequence": sequence,
            "publication_digest": publication_digest,
            "status": status,
            "reason_code": reason,
            "observed_at": observed_at,
            "prior_status_digest": prior,
        }
        digest = canonical_digest(value)
        manifest = sign_manifest(
            "rolling_card_status", value, signer=self._signer, created_at=observed_at
        )
        connection.execute(
            "INSERT INTO v3_rolling_card_status_history VALUES (?,?,?,?,?,?,?,?)",
            (
                sequence,
                publication_digest,
                status,
                reason,
                observed_at,
                prior,
                digest,
                canonical_bytes(manifest.to_dict()).decode("utf-8"),
            ),
        )
        self._append_rolling_restart_delta(
            connection,
            operation_kind=status,
            authority_kind="rolling_status",
            authority_sequence=sequence,
            authority_digest=digest,
            observed_at=observed_at,
        )
        return digest

    def provider_execution(
        self, job_id: str, job_revision: int, fencing_token: int
    ) -> ProviderExecutionAudit:
        _job_identity(job_id, job_revision)
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise DurableJobError("provider execution fencing token must be positive")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM v3_job_provider_executions WHERE job_id=? AND job_revision=? "
                "AND fencing_token=?",
                (job_id, job_revision, fencing_token),
            ).fetchone()
            if row is None:
                raise JobConflict("provider execution audit does not exist")
            return self._decode_provider_execution(connection, row)

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
                    "AND not_before_at<=? AND hard_deadline_at>? "
                    "AND NOT EXISTS (SELECT 1 FROM v3_rolling_epoch_closures closure "
                    "WHERE closure.epoch_id=CASE "
                    "WHEN json_extract(v3_jobs.payload_json, '$.schema_version')=? "
                    "THEN json_extract(v3_jobs.payload_json, '$.card_key.tournament_epoch_id') "
                    "WHEN json_extract(v3_jobs.payload_json, '$.schema_version')=? "
                    "THEN json_extract(v3_jobs.payload_json, '$.tournament_epoch_id') END)",
                    (
                        lane.value,
                        now,
                        now,
                        "strathmark-v3-rolling-component-job-v1",
                        "strathmark-v3-weight-only-recombination-v1",
                    ),
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
                current_records: list[JobRecord] = []
                for item in records:
                    if self._job_context_is_current(connection, item):
                        current_records.append(item)
                    else:
                        self._finish(
                            connection,
                            item,
                            JobState.STALE,
                            now,
                            reason="recombination_authority_changed",
                        )
                eligible = tuple(
                    item
                    for item in current_records
                    if not (
                        gpu_busy and item.resource_class is JobResourceClass.LOCAL_GPU
                    )
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
                expiry = min(
                    _add_milliseconds(now, lease_duration_ms), record.hard_deadline_at
                )
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
                leased = self._get_connection(
                    connection, record.job_id, record.job_revision
                )
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
                expiry = min(
                    _add_milliseconds(now, extend_ms), current.hard_deadline_at
                )
                connection.execute(
                    "UPDATE v3_jobs SET lease_expires_at=?, updated_at=? "
                    "WHERE job_id=? AND job_revision=?",
                    (expiry, now, job_id, job_revision),
                )
                result = self._get_connection(connection, job_id, job_revision)
                self._append_history(
                    connection, "heartbeat", current.state, result, now
                )
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
        provider_audit: ProviderExecutionAudit | None = None,
    ) -> JobRecord:
        _digest(result_digest, "result digest")
        if not callable(current_context) or not callable(clock):
            raise DurableJobError(
                "commit requires callable context and trusted clock ports"
            )
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
                    raise JobConflict(
                        "job revision already has a different publication"
                    )
                context = current_context(connection, current)
                if not isinstance(context, tuple) or len(context) != 2:
                    raise DurableJobError(
                        "current context port must return two digests"
                    )
                current_evidence_digest = _digest(context[0], "current evidence digest")
                current_bundle_digest = _digest(context[1], "current bundle digest")
                now = require_utc_milliseconds(clock())
                current = self._require_lease(
                    connection, job_id, job_revision, worker_id, fencing_token, now
                )
                payload = current.payload()
                epoch_id = _rolling_job_epoch_id(payload)
                if epoch_id is not None and (
                    connection.execute(
                        "SELECT 1 FROM v3_rolling_epoch_closures WHERE epoch_id=?",
                        (epoch_id,),
                    ).fetchone()
                    is not None
                ):
                    connection.execute(
                        "UPDATE v3_jobs SET state='cancelled', not_before_at=NULL, "
                        "lease_owner=NULL, lease_acquired_at=NULL, lease_expires_at=NULL, "
                        "fencing_token=fencing_token+1, terminal_reason='epoch_closed', "
                        "updated_at=? WHERE job_id=? AND job_revision=?",
                        (now, current.job_id, current.job_revision),
                    )
                    result = self._get_connection(
                        connection, current.job_id, current.job_revision
                    )
                    self._append_history(
                        connection, "cancelled", current.state, result, now
                    )
                    return result
                if (
                    current.evidence_digest != current_evidence_digest
                    or current.bundle_digest != current_bundle_digest
                ):
                    reason = (
                        "evidence_changed"
                        if current.evidence_digest != current_evidence_digest
                        else "bundle_changed"
                    )
                    return self._finish(
                        connection, current, JobState.STALE, now, reason=reason
                    )
                if not self._job_context_is_current(connection, current):
                    return self._finish(
                        connection,
                        current,
                        JobState.STALE,
                        now,
                        reason="recombination_authority_changed",
                    )
                self._persist_provider_execution(
                    connection, current, provider_audit, "succeeded", None, now
                )
                connection.execute(
                    "INSERT INTO v3_job_publications(job_id, job_revision, fencing_token, "
                    "result_digest, published_at, auth_body_json, auth_body_digest, auth_key_id, "
                    "auth_signature_der_b64) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._publication_values(
                        connection,
                        current,
                        result_digest=result_digest,
                        published_at=now,
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
                self._append_history(
                    connection, "succeeded", current.state, result, now
                )
                return result

    def _job_context_is_current(
        self, connection: sqlite3.Connection, record: JobRecord
    ) -> bool:
        payload = record.payload()
        if payload.get("schema_version") != (
            "strathmark-v3-weight-only-recombination-v1"
        ):
            return True
        try:
            self._verify_recombination_context(connection, payload)
        except DurableJobError:
            return False
        return True

    def _verify_recombination_context(
        self, connection: sqlite3.Connection, payload: Mapping[str, Any]
    ) -> None:
        from strathmark.v3.application.field_assembly import OperationalWeightAuthority

        field = payload.get("field_authority")
        weight_value = payload.get("weight_authority")
        if not isinstance(field, dict) or not isinstance(weight_value, dict):
            raise DurableJobError("recombination authority payload is incomplete")
        epoch_id = _rolling_job_epoch_id(payload)
        if (
            epoch_id is None
            or connection.execute(
                "SELECT 1 FROM v3_rolling_epoch_closures WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
            is not None
        ):
            raise DurableJobError("recombination epoch is closed")
        revision_digest = field.get("revision_digest")
        content = {
            name: value for name, value in field.items() if name != "revision_digest"
        }
        if revision_digest != canonical_digest(
            content
        ) or revision_digest != payload.get("field_revision_digest"):
            raise DurableJobError("recombination field authority digest differs")
        ingress = connection.execute(
            "SELECT upstream_revision,tournament_id,round_id,snapshot_json "
            "FROM v3_ingress_snapshots WHERE entity_kind='field' AND entity_id=? "
            "ORDER BY upstream_revision DESC LIMIT 1",
            (payload.get("field_id"),),
        ).fetchone()
        if ingress is None:
            raise DurableJobError("recombination field authority is not current")
        snapshot = json.loads(str(ingress[3]))
        assignments = field.get("assignments")
        if not isinstance(assignments, list) or any(
            not isinstance(item, dict) for item in assignments
        ):
            raise DurableJobError("recombination assignments are invalid")
        if (
            int(ingress[0]) != payload.get("upstream_field_revision")
            or str(ingress[1]) != payload.get("tournament_id")
            or str(ingress[2]) != payload.get("round_id")
            or snapshot.get("competitor_ids")
            != [item.get("competitor_id") for item in assignments]
            or snapshot.get("stand_ids")
            != [item.get("stand_id") for item in assignments]
            or snapshot.get("target_context") != field.get("target_context")
            or snapshot.get("capacity_authority_digest")
            != field.get("capacity_authority_digest")
            or snapshot.get("max_field_entrants") != field.get("max_field_entrants")
            or snapshot.get("call_order") != field.get("call_order")
            or snapshot.get("scheduled_at") != field.get("scheduled_at")
            or snapshot.get("deadline_at") != field.get("deadline_at")
        ):
            raise DurableJobError("recombination field authority is not current")
        epoch = connection.execute(
            "SELECT epoch_digest,maximum_tournament_sequence,historical_cutoff_key "
            "FROM v3_evidence_epochs WHERE epoch_id=? AND round_id=?",
            (payload.get("tournament_epoch_id"), payload.get("round_id")),
        ).fetchone()
        if (
            epoch is None
            or str(epoch[0]) != field.get("evidence_digest")
            or int(epoch[1]) != field.get("tournament_event_sequence")
            or str(epoch[2]) != field.get("historical_cutoff_key")
        ):
            raise DurableJobError("recombination epoch authority is not current")
        weight = OperationalWeightAuthority.from_dict(weight_value)
        row = connection.execute(
            "SELECT binding_json,manifest_json FROM v3_field_weight_authorities "
            "WHERE binding_digest=?",
            (payload.get("weight_authority_digest"),),
        ).fetchone()
        if (
            row is None
            or json.loads(str(row[0])) != weight_value
            or weight.authority_digest != payload.get("weight_authority_digest")
            or str(weight.tournament_id) != payload.get("tournament_id")
            or str(weight.round_id) != payload.get("round_id")
            or str(weight.epoch_id) != payload.get("tournament_epoch_id")
            or weight.epoch_digest != field.get("evidence_digest")
            or weight.frozen_tournament_sequence
            != field.get("tournament_event_sequence")
        ):
            raise DurableJobError("recombination weight authority is not current")
        authority_event = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE global_sequence=? AND event_digest=?",
            (weight.authority_event_sequence, weight.authority_event_digest),
        ).fetchone()
        if authority_event is None or json.loads(str(row[1])) != json.loads(
            str(authority_event[0])
        ):
            raise DurableJobError("recombination weight event authority differs")

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
        provider_audit: ProviderExecutionAudit | None = None,
    ) -> JobRecord:
        if not isinstance(failure_kind, FailureKind) or not isinstance(
            policy, RetryPolicy
        ):
            raise DurableJobError(
                "failure handling requires typed kind and retry policy"
            )
        _require_reason(reason)
        now = require_utc_milliseconds(observed_at)
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                current = self._require_lease(
                    connection, job_id, job_revision, worker_id, fencing_token, now
                )
                if current.retry_policy_version != policy.version:
                    raise JobConflict(
                        "job retry policy version differs from worker policy"
                    )
                self._persist_provider_execution(
                    connection, current, provider_audit, "failed", reason, now
                )
                if failure_kind is FailureKind.VALIDATION:
                    return self._finish(
                        connection, current, JobState.INVALID, now, reason=reason
                    )
                if failure_kind is FailureKind.PERMANENT:
                    return self._finish(
                        connection,
                        current,
                        JobState.PERMANENT_FAILED,
                        now,
                        reason=reason,
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
                self._append_history(
                    connection, "retryable-failed", current.state, result, now
                )
                return result

    def mark_invalid(self, *args: Any, reason: str, **kwargs: Any) -> JobRecord:
        return self._terminal_from_lease(
            *args, target=JobState.INVALID, reason=reason, **kwargs
        )

    def mark_stale(self, *args: Any, reason: str, **kwargs: Any) -> JobRecord:
        return self._terminal_from_lease(
            *args, target=JobState.STALE, reason=reason, **kwargs
        )

    def mark_permanent_failure(
        self, *args: Any, reason: str, **kwargs: Any
    ) -> JobRecord:
        return self._terminal_from_lease(
            *args, target=JobState.PERMANENT_FAILED, reason=reason, **kwargs
        )

    def cancel(
        self, job_id: str, job_revision: int, *, observed_at: str, reason: str
    ) -> JobRecord:
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
                self._append_history(
                    connection, "cancelled", current.state, result, now
                )
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
            raise DurableJobError(
                "dependency probe must return a typed readiness snapshot"
            )
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
        depths = tuple(
            (lane.value, sum(item.lane is lane for item in active)) for lane in JobLane
        )
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
            dict.fromkeys(
                (*MANDATORY_REPOSITORY_FIELD_DEPENDENCIES, *external.required_for_field)
            )
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
        self.verify_rolling_storage()

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
        self._append_history(
            connection, target.value, current.state, result, observed_at
        )
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
        if (
            observed_at >= current.lease_expires_at
            or observed_at >= current.hard_deadline_at
        ):
            raise JobDeadlineExceeded("lease or hard deadline expired before commit")
        return current

    def _reconcile_for_claim(
        self, connection: sqlite3.Connection, observed_at: str
    ) -> None:
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
                result = self._get_connection(
                    connection, current.job_id, current.job_revision
                )
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
                result = self._get_connection(
                    connection, current.job_id, current.job_revision
                )
                self._append_history(
                    connection, "requeued", current.state, result, observed_at
                )
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
            connection.execute(
                f"SELECT COUNT(*) FROM v3_jobs WHERE state IN {active}"
            ).fetchone()[0]
        )
        lane_active = int(
            connection.execute(
                f"SELECT COUNT(*) FROM v3_jobs WHERE lane=? AND state IN {active}",
                (lane.value,),
            ).fetchone()[0]
        )
        lane_leased = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_jobs WHERE lane=? AND state='leased'",
                (lane.value,),
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
        jitter = (
            int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:8], 16)
            % jitter_window
        )
        return min(policy.maximum_delay_ms, bounded + jitter)

    def _persist_provider_execution(
        self,
        connection: sqlite3.Connection,
        current: JobRecord,
        audit: ProviderExecutionAudit | None,
        status: str,
        reason: str | None,
        observed_at: str,
    ) -> None:
        if audit is None:
            return
        if not isinstance(audit, ProviderExecutionAudit):
            raise DurableJobError("provider execution audit must be typed")
        if audit.status != status or audit.reason != reason:
            raise JobConflict(
                "provider execution audit outcome differs from transition"
            )
        payload = current.payload()
        packet = payload.get("provider_packet")
        pin = json.loads(audit.member_pin_json)
        if (
            not isinstance(packet, Mapping)
            or packet.get("provider_id") != audit.provider_id
            or pin.get("member_manifest_digest")
            != payload.get("member_manifest_digest")
        ):
            raise JobConflict(
                "provider execution audit differs from persisted job pins"
            )
        execution = audit.to_dict()
        execution_json = canonical_bytes(execution).decode("utf-8")
        authority_payload = {
            "schema_version": "strathmark-v3-provider-execution-authority-v1",
            "job_id": current.job_id,
            "job_revision": current.job_revision,
            "fencing_token": current.fencing_token,
            "lease_owner": current.lease_owner,
            "execution": execution,
            "execution_digest": audit.digest,
            "observed_at": observed_at,
        }
        authority = sign_manifest(
            "provider_execution",
            authority_payload,
            signer=self._signer,
            created_at=observed_at,
        )
        connection.execute(
            "INSERT INTO v3_job_provider_executions(job_id, job_revision, fencing_token, "
            "lease_owner, provider_id, member_id, member_pin_json, member_pin_digest, status, "
            "reason, attempt_count, execution_json, execution_digest, observed_at, "
            "auth_body_json, auth_body_digest, auth_key_id, auth_signature_der_b64) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                current.job_id,
                current.job_revision,
                current.fencing_token,
                current.lease_owner,
                audit.provider_id,
                audit.member_id,
                audit.member_pin_json,
                audit.member_pin_digest,
                audit.status,
                audit.reason,
                len(audit.attempts),
                execution_json,
                audit.digest,
                observed_at,
                authority.body_json,
                authority.body_digest,
                authority.key_id,
                authority.signature_der_b64,
            ),
        )
        for attempt in audit.attempts:
            connection.execute(
                "INSERT INTO v3_job_provider_attempts(job_id, job_revision, fencing_token, "
                "attempt_ordinal, raw_digest, validator_code, accepted) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    current.job_id,
                    current.job_revision,
                    current.fencing_token,
                    attempt.ordinal,
                    attempt.raw_digest,
                    attempt.validator_code,
                    int(attempt.accepted),
                ),
            )
            storage = attempt.storage_reference
            connection.execute(
                "INSERT INTO v3_job_provider_storage_refs(job_id, job_revision, fencing_token, "
                "attempt_ordinal, raw_digest, byte_count, reference_json, reference_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    current.job_id,
                    current.job_revision,
                    current.fencing_token,
                    attempt.ordinal,
                    storage.raw_digest,
                    storage.byte_count,
                    storage.reference_json,
                    storage.reference_digest,
                ),
            )

    def _decode_provider_execution(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> ProviderExecutionAudit:
        try:
            value = json.loads(str(row["execution_json"]))
            audit = ProviderExecutionAudit.from_dict(value)
            authority = SignedManifest(
                "provider_execution",
                str(row["auth_body_json"]),
                str(row["auth_body_digest"]),
                str(row["auth_key_id"]),
                str(row["auth_signature_der_b64"]),
            )
            signed = verify_manifest(authority, self._trust_store)
        except Exception as exc:
            raise DurableJobError(
                "provider execution audit failed integrity verification"
            ) from exc
        expected_signed = {
            "schema_version": "strathmark-v3-provider-execution-authority-v1",
            "job_id": str(row["job_id"]),
            "job_revision": int(row["job_revision"]),
            "fencing_token": int(row["fencing_token"]),
            "lease_owner": str(row["lease_owner"]),
            "execution": value,
            "execution_digest": audit.digest,
            "observed_at": str(row["observed_at"]),
        }
        if signed != expected_signed or (
            row["provider_id"] != audit.provider_id
            or row["member_id"] != audit.member_id
            or row["member_pin_json"] != audit.member_pin_json
            or row["member_pin_digest"] != audit.member_pin_digest
            or row["status"] != audit.status
            or row["reason"] != audit.reason
            or int(row["attempt_count"]) != len(audit.attempts)
            or row["execution_json"] != canonical_bytes(value).decode("utf-8")
            or row["execution_digest"] != audit.digest
        ):
            raise DurableJobError("provider execution audit material differs")
        attempt_rows = connection.execute(
            "SELECT * FROM v3_job_provider_attempts WHERE job_id=? AND job_revision=? "
            "AND fencing_token=? ORDER BY attempt_ordinal",
            (row["job_id"], row["job_revision"], row["fencing_token"]),
        ).fetchall()
        storage_rows = connection.execute(
            "SELECT * FROM v3_job_provider_storage_refs WHERE job_id=? AND job_revision=? "
            "AND fencing_token=? ORDER BY attempt_ordinal",
            (row["job_id"], row["job_revision"], row["fencing_token"]),
        ).fetchall()
        if len(attempt_rows) != len(audit.attempts) or len(storage_rows) != len(
            audit.attempts
        ):
            raise DurableJobError("provider execution normalized audit rows differ")
        for expected, attempt_row, storage_row in zip(
            audit.attempts, attempt_rows, storage_rows
        ):
            storage = expected.storage_reference
            if (
                int(attempt_row["attempt_ordinal"]) != expected.ordinal
                or attempt_row["raw_digest"] != expected.raw_digest
                or attempt_row["validator_code"] != expected.validator_code
                or bool(attempt_row["accepted"]) is not expected.accepted
                or int(storage_row["attempt_ordinal"]) != expected.ordinal
                or storage_row["raw_digest"] != storage.raw_digest
                or int(storage_row["byte_count"]) != storage.byte_count
                or storage_row["reference_json"] != storage.reference_json
                or storage_row["reference_digest"] != storage.reference_digest
            ):
                raise DurableJobError(
                    "provider execution normalized audit material differs"
                )
        return audit

    def _install_job_spec(
        self, connection: sqlite3.Connection, record: JobRecord
    ) -> str:
        value = _job_spec_value(record)
        digest = canonical_digest(value)
        manifest = sign_manifest(
            "job_spec", value, signer=self._signer, created_at=record.created_at
        )
        connection.execute(
            "INSERT INTO v3_job_specs VALUES (?,?,?,?,?,?)",
            (
                record.job_id,
                record.job_revision,
                canonical_bytes(value).decode("utf-8"),
                digest,
                canonical_bytes(manifest.to_dict()).decode("utf-8"),
                record.created_at,
            ),
        )
        return digest

    def _job_projection_authority_guard(
        self, connection: sqlite3.Connection
    ) -> str:
        material = {}
        for table, ordering in (
            ("v3_job_specs", "job_id,job_revision"),
            ("v3_job_history", "history_sequence"),
            ("v3_job_publications", "job_id,job_revision"),
        ):
            digest = hashlib.sha256()
            count = 0
            for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY {ordering}"
            ):
                encoded = canonical_bytes(
                    list(row), max_bytes=4_194_304, max_items=4_096
                )
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                count += 1
            material[table] = {"count": count, "root_digest": digest.hexdigest()}
        return canonical_digest(
            {"schema_version": "strathmark-v3-job-projection-authority-v1", **material}
        )

    @staticmethod
    def _legacy_job_cutover_guard(connection: sqlite3.Connection) -> str:
        material = {
            table: [list(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("v3_jobs", "v3_job_history", "v3_job_publications")
        }
        return canonical_digest(
            {"schema_version": "strathmark-v3-legacy-job-cutover-guard-v1", **material}
        )

    def _verify_legacy_job_projection_authority(
        self, connection: sqlite3.Connection
    ) -> dict[tuple[str, int], JobRecord]:
        current = {
            (str(row["job_id"]), int(row["job_revision"])): _decode(row)
            for row in connection.execute("SELECT * FROM v3_jobs")
        }
        queued: dict[tuple[str, int], JobRecord] = {}
        latest: dict[tuple[str, int], tuple[Any, ...]] = {}
        prior = ZERO_DIGEST
        for expected_sequence, row in enumerate(
            connection.execute("SELECT * FROM v3_job_history ORDER BY history_sequence"),
            start=1,
        ):
            key = (str(row["job_id"]), int(row["job_revision"]))
            record = current.get(key)
            value = _history_value(row)
            authority = SignedManifest(
                "job_transition",
                str(row["auth_body_json"]),
                str(row["auth_body_digest"]),
                str(row["auth_key_id"]),
                str(row["auth_signature_der_b64"]),
            )
            if (
                record is None
                or int(row["history_sequence"]) != expected_sequence
                or str(row["prior_history_digest"]) != prior
                or str(row["history_digest"]) != canonical_digest(value)
                or verify_manifest(authority, self._trust_store) != value
                or str(row["job_spec_digest"]) != ZERO_DIGEST
                or str(row["job_material_digest"])
                != _record_material_digest(record)
            ):
                raise DurableJobError("legacy job history authority differs")
            previous = latest.get(key)
            if previous is None:
                if str(row["operation_kind"]) != "queued" or row["from_state"] is not None:
                    raise DurableJobError("legacy job history does not begin with queued")
                queued[key] = _record_with_history_snapshot(record, row)
            elif row["from_state"] != previous[0]:
                raise DurableJobError("legacy job history states are not contiguous")
            _verify_transition(row)
            latest[key] = _history_snapshot(row)
            prior = str(row["history_digest"])
        if set(latest) != set(current) or any(
            latest[key] != _record_snapshot(record)
            for key, record in current.items()
        ):
            raise DurableJobError("legacy job projection differs from signed history")
        self._verify_job_records_local(connection, tuple(current.values()))
        return queued

    def _verify_job_spec_cutover(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT * FROM v3_job_spec_cutovers WHERE cutover_sequence=1"
        ).fetchone()
        if row is None:
            return
        specs = [
            {
                "job_id": str(spec[0]),
                "job_revision": int(spec[1]),
                "spec_digest": str(spec[2]),
            }
            for spec in connection.execute(
                "SELECT job_id,job_revision,spec_digest FROM v3_job_specs "
                "ORDER BY job_id,job_revision"
            )
        ]
        value = {
            "schema_version": "strathmark-v3-job-spec-cutover-v1",
            "cutover_sequence": 1,
            "legacy_history_sequence": int(row["legacy_history_sequence"]),
            "legacy_history_digest": str(row["legacy_history_digest"]),
            "job_spec_count": int(row["job_spec_count"]),
            "job_spec_root_digest": str(row["job_spec_root_digest"]),
            "created_at": str(row["created_at"]),
        }
        try:
            manifest = SignedManifest.from_dict(
                json.loads(str(row["cutover_manifest_json"]))
            )
        except Exception as exc:
            raise DurableJobError("job spec cutover authority is invalid") from exc
        history_tip = connection.execute(
            "SELECT history_sequence,history_digest FROM v3_job_history "
            "WHERE history_sequence=?",
            (value["legacy_history_sequence"],),
        ).fetchone()
        if (
            history_tip is None
            or (int(history_tip[0]), str(history_tip[1]))
            != (value["legacy_history_sequence"], value["legacy_history_digest"])
            or len(specs) != value["job_spec_count"]
            or canonical_digest(specs) != value["job_spec_root_digest"]
            or str(row["cutover_digest"]) != manifest.body_digest
            or verify_manifest(manifest, self._trust_store) != value
        ):
            raise DurableJobError("job spec cutover authority differs")

    def _replay_job_projection_authority(
        self, connection: sqlite3.Connection
    ) -> dict[tuple[str, int], JobRecord]:
        records: dict[tuple[str, int], JobRecord] = {}
        spec_digests: dict[tuple[str, int], str] = {}
        self._verify_job_spec_cutover(connection)
        legacy_row = connection.execute(
            "SELECT legacy_history_sequence FROM v3_job_spec_cutovers "
            "WHERE cutover_sequence=1"
        ).fetchone()
        legacy_cutover_sequence = 0 if legacy_row is None else int(legacy_row[0])
        for row in connection.execute(
            "SELECT * FROM v3_job_specs ORDER BY job_id,job_revision"
        ):
            try:
                value = json.loads(str(row["spec_json"]))
                manifest = SignedManifest.from_dict(
                    json.loads(str(row["spec_manifest_json"]))
                )
                record = _job_record_from_spec(value)
            except Exception as exc:
                raise DurableJobError("job spec authority is invalid") from exc
            key = (record.job_id, record.job_revision)
            if (
                key != (str(row["job_id"]), int(row["job_revision"]))
                or canonical_bytes(value).decode("utf-8") != str(row["spec_json"])
                or canonical_digest(value) != str(row["spec_digest"])
                or verify_manifest(manifest, self._trust_store) != value
                or record.state is not JobState.QUEUED
            ):
                raise DurableJobError("job spec authority differs")
            records[key] = record
            spec_digests[key] = str(row["spec_digest"])
        prior = ZERO_DIGEST
        latest: dict[tuple[str, int], tuple[Any, ...]] = {}
        for expected_sequence, row in enumerate(
            connection.execute(
                "SELECT * FROM v3_job_history ORDER BY history_sequence"
            ),
            start=1,
        ):
            key = (str(row["job_id"]), int(row["job_revision"]))
            record = records.get(key)
            if record is None:
                raise DurableJobError("job history references unknown job spec")
            value = _history_value(row)
            authority = SignedManifest(
                "job_transition",
                str(row["auth_body_json"]),
                str(row["auth_body_digest"]),
                str(row["auth_key_id"]),
                str(row["auth_signature_der_b64"]),
            )
            if (
                int(row["history_sequence"]) != expected_sequence
                or str(row["prior_history_digest"]) != prior
                or str(row["history_digest"]) != canonical_digest(value)
                or verify_manifest(authority, self._trust_store) != value
                or (
                    str(row["job_spec_digest"]) != spec_digests[key]
                    and not (
                        int(row["history_sequence"]) <= legacy_cutover_sequence
                        and str(row["job_spec_digest"]) == ZERO_DIGEST
                    )
                )
                or str(row["job_material_digest"])
                != _record_material_digest(record)
            ):
                raise DurableJobError("job history authority differs")
            previous = latest.get(key)
            if previous is None:
                if str(row["operation_kind"]) != "queued" or row["from_state"] is not None:
                    raise DurableJobError("job history does not begin with queued")
            elif row["from_state"] != previous[0]:
                raise DurableJobError("job history states are not contiguous")
            _verify_transition(row)
            record = _record_with_history_snapshot(record, row)
            records[key] = record
            latest[key] = _history_snapshot(row)
            prior = str(row["history_digest"])
        if set(latest) != set(records):
            raise DurableJobError("job spec lacks signed queued history")
        return records

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
            "job_spec_digest": self._job_spec_digest(
                connection, result.job_id, result.job_revision
            ),
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
            "auth_body_digest, auth_key_id, auth_signature_der_b64, job_spec_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                value["job_spec_digest"],
            ),
        )
        self._append_rolling_restart_delta(
            connection,
            operation_kind=operation_kind,
            authority_kind="job_history",
            authority_sequence=sequence,
            authority_digest=digest,
            observed_at=observed_at,
        )

    @staticmethod
    def _job_spec_digest(
        connection: sqlite3.Connection, job_id: str, job_revision: int
    ) -> str:
        row = connection.execute(
            "SELECT spec_digest FROM v3_job_specs WHERE job_id=? AND job_revision=?",
            (job_id, job_revision),
        ).fetchone()
        if row is None:
            raise DurableJobError("job spec authority is missing")
        return str(row[0])

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

    def _verify_job_rows_local(
        self, connection: sqlite3.Connection, rows: Iterable[sqlite3.Row]
    ) -> tuple[JobRecord, ...]:
        records = tuple(_decode(row) for row in rows)
        self._verify_job_records_local(connection, records)
        return records

    def _verify_job_records_local(
        self, connection: sqlite3.Connection, records: tuple[JobRecord, ...]
    ) -> None:
        """Verify exact touched jobs against their signed latest transitions."""

        for record in records:
            payload = record.payload()
            capacity_use = record.capacity_use()
            if (
                canonical_bytes(payload, max_bytes=MAX_JOB_PAYLOAD_BYTES).decode(
                    "utf-8"
                )
                != record.payload_json
                or canonical_digest(payload) != record.payload_digest
                or canonical_bytes(capacity_use.to_dict()).decode("utf-8")
                != record.capacity_use_json
                or record.lane is not record.job_kind.lane
                or record.resource_class is not record.job_kind.resource_class
                or not validate_capacity_use(self.capacity, capacity_use).admitted
            ):
                raise DurableJobError("persisted job local authority differs")
            history = connection.execute(
                "SELECT * FROM v3_job_history WHERE job_id=? AND job_revision=? "
                "ORDER BY history_sequence DESC LIMIT 1",
                (record.job_id, record.job_revision),
            ).fetchone()
            if history is None:
                raise DurableJobError("job projection lacks signed history")
            value = _history_value(history)
            manifest = SignedManifest(
                "job_transition",
                str(history["auth_body_json"]),
                str(history["auth_body_digest"]),
                str(history["auth_key_id"]),
                str(history["auth_signature_der_b64"]),
            )
            sequence = int(history["history_sequence"])
            prior = (
                None
                if sequence == 1
                else connection.execute(
                    "SELECT history_digest FROM v3_job_history WHERE history_sequence=?",
                    (sequence - 1,),
                ).fetchone()
            )
            expected_prior = ZERO_DIGEST if prior is None else str(prior[0])
            if (
                str(history["prior_history_digest"]) != expected_prior
                or str(history["history_digest"]) != canonical_digest(value)
                or verify_manifest(manifest, self._trust_store) != value
                or str(history["job_material_digest"])
                != _record_material_digest(record)
                or _history_snapshot(history) != _record_snapshot(record)
            ):
                raise DurableJobError(
                    "job projection differs from signed local authority"
                )
            publication = connection.execute(
                "SELECT * FROM v3_job_publications WHERE job_id=? AND job_revision=?",
                (record.job_id, record.job_revision),
            ).fetchone()
            if (publication is not None) != (record.state is JobState.SUCCEEDED):
                raise DurableJobError(
                    "job publication differs from local succeeded state"
                )
            if publication is not None:
                publication_value = _publication_value(publication)
                publication_manifest = SignedManifest(
                    "job_publication",
                    str(publication["auth_body_json"]),
                    str(publication["auth_body_digest"]),
                    str(publication["auth_key_id"]),
                    str(publication["auth_signature_der_b64"]),
                )
                if (
                    int(publication["fencing_token"]) != record.fencing_token
                    or str(publication["result_digest"]) != record.result_digest
                    or publication_value["job_material_digest"]
                    != _record_material_digest(record)
                    or verify_manifest(publication_manifest, self._trust_store)
                    != publication_value
                ):
                    raise DurableJobError("job publication local authority differs")

    def _verify_connection(self, connection: sqlite3.Connection) -> None:
        jobs = {
            (str(row["job_id"]), int(row["job_revision"])): _decode(row)
            for row in connection.execute("SELECT * FROM v3_jobs")
        }
        if self._replay_job_projection_authority(connection) != jobs:
            raise DurableJobError(
                "job projection differs from signed spec and history authority"
            )
        for record in jobs.values():
            payload = record.payload()
            capacity_use = record.capacity_use()
            if (
                canonical_bytes(payload, max_bytes=MAX_JOB_PAYLOAD_BYTES).decode(
                    "utf-8"
                )
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
        succeeded = {
            key for key, record in jobs.items() if record.state is JobState.SUCCEEDED
        }
        if set(publications) != succeeded:
            raise DurableJobError(
                "job publications do not exactly match succeeded revisions"
            )
        for key, row in publications.items():
            record = jobs[key]
            if (
                int(row["fencing_token"]) != record.fencing_token
                or str(row["result_digest"]) != record.result_digest
            ):
                raise DurableJobError(
                    "job publication differs from its succeeded projection"
                )
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
        for row in connection.execute(
            "SELECT * FROM v3_job_provider_executions ORDER BY job_id, job_revision, fencing_token"
        ):
            key = (str(row["job_id"]), int(row["job_revision"]))
            if key not in jobs:
                raise DurableJobError(
                    "provider execution audit references unknown work"
                )
            transition = connection.execute(
                "SELECT operation_kind FROM v3_job_history WHERE job_id=? AND job_revision=? "
                "AND fencing_token=? AND operation_kind IN ('succeeded', 'invalid', "
                "'retryable-failed', 'permanent-failed')",
                (row["job_id"], row["job_revision"], row["fencing_token"]),
            ).fetchone()
            if transition is None:
                raise DurableJobError(
                    "provider execution audit lacks a terminal attempt transition"
                )
            if (row["status"] == "succeeded") != (transition[0] == "succeeded"):
                raise DurableJobError(
                    "provider execution audit status differs from job history"
                )
            self._decode_provider_execution(connection, row)

    @staticmethod
    def _get_connection(
        connection: sqlite3.Connection, job_id: str, job_revision: int
    ) -> JobRecord:
        _job_identity(job_id, job_revision)
        row = connection.execute(
            "SELECT * FROM v3_jobs WHERE job_id=? AND job_revision=?",
            (job_id, job_revision),
        ).fetchone()
        if row is None:
            raise KeyError((job_id, job_revision))
        return _decode(row)


def _rolling_job_epoch_id(payload: Mapping[str, Any]) -> str | None:
    schema = payload.get("schema_version")
    if schema == "strathmark-v3-rolling-component-job-v1":
        card_key = payload.get("card_key")
        value = (
            card_key.get("tournament_epoch_id") if isinstance(card_key, dict) else None
        )
    elif schema == "strathmark-v3-weight-only-recombination-v1":
        value = payload.get("tournament_epoch_id")
    else:
        return None
    try:
        return str(require_identifier(value, expected_namespace="epoch"))
    except Exception as exc:
        raise DurableJobError("rolling job epoch authority is invalid") from exc


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
        not_before_at=(
            None if row["not_before_at"] is None else str(row["not_before_at"])
        ),
        hard_deadline_at=str(row["hard_deadline_at"]),
        lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
        lease_acquired_at=(
            None if row["lease_acquired_at"] is None else str(row["lease_acquired_at"])
        ),
        lease_expires_at=(
            None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
        ),
        fencing_token=int(row["fencing_token"]),
        terminal_reason=(
            None if row["terminal_reason"] is None else str(row["terminal_reason"])
        ),
        result_digest=(
            None if row["result_digest"] is None else str(row["result_digest"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _rolling_status_value(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-rolling-card-status-v1",
        "status_sequence": int(row["status_sequence"]),
        "publication_digest": str(row["publication_digest"]),
        "status": str(row["status"]),
        "reason_code": str(row["reason_code"]),
        "observed_at": str(row["observed_at"]),
        "prior_status_digest": str(row["prior_status_digest"]),
    }


def _rolling_current_material(rows: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        sorted(
            (
                str(row["competitor_id"]),
                str(row["target_context_digest"]),
                str(row["publication_digest"]),
                int(row["dependency_revision"]),
                str(row["status_digest"]),
                str(row["updated_at"]),
            )
            for row in rows
        )
    )


def _job_spec_value(record: JobRecord) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-job-spec-v1",
        "job": {
            "job_id": record.job_id,
            "job_revision": record.job_revision,
            "idempotency_key": record.idempotency_key,
            "job_kind": record.job_kind.value,
            "lane": record.lane.value,
            "resource_class": record.resource_class.value,
            "base_priority": int(record.priority),
            "capacity_use_json": record.capacity_use_json,
            "payload_json": record.payload_json,
            "payload_digest": record.payload_digest,
            "evidence_digest": record.evidence_digest,
            "bundle_digest": record.bundle_digest,
            "retry_policy_version": record.retry_policy_version,
            "state": record.state.value,
            "attempt_count": record.attempt_count,
            "max_attempts": record.max_attempts,
            "initial_not_before_at": record.initial_not_before_at,
            "not_before_at": record.not_before_at,
            "hard_deadline_at": record.hard_deadline_at,
            "lease_owner": record.lease_owner,
            "lease_acquired_at": record.lease_acquired_at,
            "lease_expires_at": record.lease_expires_at,
            "fencing_token": record.fencing_token,
            "terminal_reason": record.terminal_reason,
            "result_digest": record.result_digest,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        },
    }


def _job_record_from_spec(value: object) -> JobRecord:
    if not isinstance(value, dict) or set(value) != {"schema_version", "job"}:
        raise DurableJobError("job spec fields differ")
    if value["schema_version"] != "strathmark-v3-job-spec-v1":
        raise DurableJobError("job spec schema differs")
    job = value["job"]
    expected = {
        "job_id",
        "job_revision",
        "idempotency_key",
        "job_kind",
        "lane",
        "resource_class",
        "base_priority",
        "capacity_use_json",
        "payload_json",
        "payload_digest",
        "evidence_digest",
        "bundle_digest",
        "retry_policy_version",
        "state",
        "attempt_count",
        "max_attempts",
        "initial_not_before_at",
        "not_before_at",
        "hard_deadline_at",
        "lease_owner",
        "lease_acquired_at",
        "lease_expires_at",
        "fencing_token",
        "terminal_reason",
        "result_digest",
        "created_at",
        "updated_at",
    }
    if not isinstance(job, dict) or set(job) != expected:
        raise DurableJobError("job spec job fields differ")
    return JobRecord(
        job_id=job["job_id"],
        job_revision=job["job_revision"],
        idempotency_key=job["idempotency_key"],
        job_kind=JobKind(job["job_kind"]),
        lane=JobLane(job["lane"]),
        resource_class=JobResourceClass(job["resource_class"]),
        priority=JobPriority(job["base_priority"]),
        capacity_use_json=job["capacity_use_json"],
        payload_json=job["payload_json"],
        payload_digest=job["payload_digest"],
        evidence_digest=job["evidence_digest"],
        bundle_digest=job["bundle_digest"],
        retry_policy_version=job["retry_policy_version"],
        state=JobState(job["state"]),
        attempt_count=job["attempt_count"],
        max_attempts=job["max_attempts"],
        initial_not_before_at=job["initial_not_before_at"],
        not_before_at=job["not_before_at"],
        hard_deadline_at=job["hard_deadline_at"],
        lease_owner=job["lease_owner"],
        lease_acquired_at=job["lease_acquired_at"],
        lease_expires_at=job["lease_expires_at"],
        fencing_token=job["fencing_token"],
        terminal_reason=job["terminal_reason"],
        result_digest=job["result_digest"],
        created_at=job["created_at"],
        updated_at=job["updated_at"],
    )


def _record_with_history_snapshot(record: JobRecord, row: sqlite3.Row) -> JobRecord:
    return replace(
        record,
        state=JobState(str(row["result_state"])),
        attempt_count=int(row["attempt_count"]),
        fencing_token=int(row["fencing_token"]),
        lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
        lease_acquired_at=(
            None
            if row["lease_acquired_at"] is None
            else str(row["lease_acquired_at"])
        ),
        lease_expires_at=(
            None if row["lease_expires_at"] is None else str(row["lease_expires_at"])
        ),
        not_before_at=(
            None if row["not_before_at"] is None else str(row["not_before_at"])
        ),
        terminal_reason=(
            None if row["terminal_reason"] is None else str(row["terminal_reason"])
        ),
        result_digest=(
            None if row["result_digest"] is None else str(row["result_digest"])
        ),
        updated_at=str(row["observed_at"]),
    )


def _record_storage_values(record: JobRecord) -> tuple[Any, ...]:
    return (
        record.job_id,
        record.job_revision,
        record.idempotency_key,
        record.job_kind.value,
        record.lane.value,
        record.resource_class.value,
        int(record.priority),
        record.capacity_use_json,
        record.payload_json,
        record.payload_digest,
        record.evidence_digest,
        record.bundle_digest,
        record.retry_policy_version,
        record.state.value,
        record.attempt_count,
        record.max_attempts,
        record.initial_not_before_at,
        record.not_before_at,
        record.hard_deadline_at,
        record.lease_owner,
        record.lease_acquired_at,
        record.lease_expires_at,
        record.fencing_token,
        record.terminal_reason,
        record.result_digest,
        record.created_at,
        record.updated_at,
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
    value = {
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
        "not_before_at": (
            None if row["not_before_at"] is None else str(row["not_before_at"])
        ),
        "terminal_reason": (
            None if row["terminal_reason"] is None else str(row["terminal_reason"])
        ),
        "result_digest": (
            None if row["result_digest"] is None else str(row["result_digest"])
        ),
        "observed_at": str(row["observed_at"]),
        "prior_history_digest": str(row["prior_history_digest"]),
        "job_material_digest": str(row["job_material_digest"]),
    }
    job_spec_digest = str(row["job_spec_digest"])
    if job_spec_digest != ZERO_DIGEST:
        value["job_spec_digest"] = job_spec_digest
    return value


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
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    return int(moment.timestamp() * 1000)


def _add_milliseconds(timestamp: str, milliseconds: int) -> str:
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    return (moment + timedelta(milliseconds=milliseconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


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
