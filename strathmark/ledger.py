"""Append-only trusted prediction ledger for Prediction Engine V2.

The local SQLite database is the authoritative race-day write target.  A whole
field is committed in one transaction.  Optional cloud mirroring runs only
after that transaction and is deliberately best-effort.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from strathmark.provenance import ENGINE_VERSION, is_v2_training_source

_ENV_VAR = "STRATHMARK_DB_PATH"
_DEFAULT_PATH = Path.home() / ".strathmark" / "results.db"
_LEDGER_NAMESPACE = uuid.UUID("2f08f564-cae9-54bf-b488-7d5a19831f80")
_RAW_HASH_ALGORITHM = "raw-v1"
_ACTIVE_HASH_ALGORITHM = "active-v2"
MAX_MIRROR_QUEUE = 1024
MAX_NUMERIC_SETTLEMENTS_PER_REVISION = 512
MAX_NUMERIC_RAW_TIME_SECONDS = 300.0
MAX_ACTIVE_ATTESTATION_NONCES_PER_CONSUMER = 4096
NUMERIC_OUTCOME_REASON_CODES = frozenset(
    {
        "corrected_time",
        "retract_invalid_numeric_evidence",
        "valid_replacement",
    }
)
LEGACY_SETTLEMENT_REASON_CODES = frozenset(
    {
        "corrected_time",
        "corrected_transcription",
        "timing_review",
    }
)
_LEGACY_SETTLEMENT_REASON_ALIASES = {
    "corrected transcription": "corrected_transcription",
    "timing review": "timing_review",
}
_LEGACY_REDACTED_ACTOR = "legacy:redacted"
_LEGACY_REDACTED_REASON = "legacy_redacted"
_SAFE_LEGACY_SETTLEMENT_ACTOR_IDS = frozenset(
    {
        "api",
        "api-official",
        "chief-handicapper",
        "legacy-official",
        "official",
    }
)

_NAMESPACED_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")

_NUMERIC_FEATURES = frozenset(
    {
        "diameter_mm",
        "janka_hardness",
        "specific_gravity",
        "crush_strength",
        "shear_strength",
        "modulus_of_rupture",
        "modulus_of_elasticity",
        "species_missing",
        "gender_f",
        "gender_missing",
        "log_diameter_ratio",
        "history_count",
        "effective_history_weight",
        "same_event_state",
        "trend_projection",
        "cross_event_state",
        "core_log_location",
        "posterior_log_location",
        "posterior_log_scale",
        "shared_log_scale",
        "performance_std_dev",
        "calibration_sample_count",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_requests (
    ledger_request_id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    hash_algorithm TEXT NOT NULL DEFAULT 'active-v2'
        CHECK(hash_algorithm IN ('raw-v1', 'active-v2')),
    event_code TEXT NOT NULL CHECK(event_code IN ('SB', 'UH')),
    prediction_as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(caller_id, request_id)
);

CREATE TABLE IF NOT EXISTS ledger_predictions (
    prediction_id TEXT PRIMARY KEY,
    ledger_request_id TEXT NOT NULL REFERENCES prediction_requests(ledger_request_id),
    competitor_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    event_code TEXT NOT NULL CHECK(event_code IN ('SB', 'UH')),
    median_seconds REAL NOT NULL CHECK(median_seconds > 0),
    assigned_mark INTEGER NOT NULL CHECK(assigned_mark >= 3),
    source TEXT NOT NULL,
    training_eligible INTEGER NOT NULL CHECK(training_eligible IN (0, 1)),
    engine_version TEXT,
    model_version TEXT,
    calibration_version TEXT,
    evidence_cutoff TEXT,
    interval_lower REAL,
    interval_upper REAL,
    interval_coverage REAL,
    interval_state TEXT,
    interval_scope TEXT,
    ignored_factors_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    optimizer TEXT,
    optimizer_metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(ledger_request_id, competitor_id)
);

CREATE TABLE IF NOT EXISTS prediction_features (
    feature_snapshot_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL REFERENCES ledger_predictions(prediction_id),
    feature_name TEXT NOT NULL,
    numeric_value REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(prediction_id, feature_name)
);

CREATE TABLE IF NOT EXISTS prediction_settlements (
    settlement_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL REFERENCES ledger_predictions(prediction_id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    competitor_id TEXT NOT NULL,
    event_code TEXT NOT NULL CHECK(event_code IN ('SB', 'UH')),
    actual_time REAL NOT NULL CHECK(actual_time > 0),
    residual REAL NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    payload_hash TEXT NOT NULL,
    supersedes_settlement_id TEXT REFERENCES prediction_settlements(settlement_id),
    settled_at TEXT NOT NULL,
    UNIQUE(prediction_id, revision),
    UNIQUE(prediction_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS prediction_mirror_outbox (
    outbox_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('field', 'settlement')),
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, entity_id)
);

CREATE TABLE IF NOT EXISTS prediction_mirror_delivery (
    outbox_id TEXT PRIMARY KEY REFERENCES prediction_mirror_outbox(outbox_id),
    attempts INTEGER NOT NULL CHECK(attempts >= 1),
    status TEXT NOT NULL CHECK(status IN ('failed', 'recorded')),
    last_attempt_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_receipts (
    ledger_request_id TEXT PRIMARY KEY REFERENCES prediction_requests(ledger_request_id),
    caller_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    active_input_fingerprint TEXT NOT NULL,
    core_schema_version TEXT NOT NULL,
    core_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(caller_id, request_id)
);

CREATE TABLE IF NOT EXISTS numeric_outcome_revisions (
    field_revision_id TEXT PRIMARY KEY,
    outcome_revision_id TEXT NOT NULL UNIQUE,
    ledger_request_id TEXT NOT NULL REFERENCES prediction_requests(ledger_request_id),
    caller_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason_code TEXT CHECK(reason_code IN (
        'corrected_time',
        'retract_invalid_numeric_evidence',
        'valid_replacement'
    )),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS numeric_settlement_revisions (
    revision_id TEXT PRIMARY KEY,
    field_revision_id TEXT NOT NULL
        REFERENCES numeric_outcome_revisions(field_revision_id),
    prediction_id TEXT NOT NULL REFERENCES ledger_predictions(prediction_id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    competitor_id TEXT NOT NULL,
    event_code TEXT NOT NULL CHECK(event_code IN ('SB', 'UH')),
    action TEXT NOT NULL CHECK(action IN ('settle', 'void')),
    actual_time REAL,
    residual REAL,
    supersedes_revision_id TEXT,
    created_at TEXT NOT NULL,
    CHECK(
        (action = 'settle' AND actual_time > 0 AND actual_time <= 300.0
            AND residual IS NOT NULL)
        OR (action = 'void' AND actual_time IS NULL AND residual IS NULL)
    ),
    UNIQUE(prediction_id, revision),
    UNIQUE(field_revision_id, prediction_id)
);

CREATE TABLE IF NOT EXISTS actor_attestation_nonce_claims (
    consumer_id TEXT NOT NULL,
    nonce_hash TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    subject_revision TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(consumer_id, nonce_hash)
);

CREATE INDEX IF NOT EXISTS idx_ledger_predictions_competitor
    ON ledger_predictions(competitor_id, event_code);
CREATE INDEX IF NOT EXISTS idx_prediction_settlements_prediction
    ON prediction_settlements(prediction_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_numeric_settlement_revisions_prediction
    ON numeric_settlement_revisions(prediction_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_actor_attestation_nonce_expiry
    ON actor_attestation_nonce_claims(expires_at);
"""

_IMMUTABILITY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS prediction_requests_no_update
BEFORE UPDATE ON prediction_requests BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS prediction_requests_no_delete
BEFORE DELETE ON prediction_requests BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS ledger_predictions_no_update
BEFORE UPDATE ON ledger_predictions BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS ledger_predictions_no_delete
BEFORE DELETE ON ledger_predictions BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS prediction_features_no_update
BEFORE UPDATE ON prediction_features BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS prediction_features_no_delete
BEFORE DELETE ON prediction_features BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS prediction_settlements_no_update
BEFORE UPDATE ON prediction_settlements BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS prediction_settlements_no_delete
BEFORE DELETE ON prediction_settlements BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS shadow_receipts_no_update
BEFORE UPDATE ON shadow_receipts BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS shadow_receipts_no_delete
BEFORE DELETE ON shadow_receipts BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS numeric_outcome_revisions_no_update
BEFORE UPDATE ON numeric_outcome_revisions BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS numeric_outcome_revisions_no_delete
BEFORE DELETE ON numeric_outcome_revisions BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS numeric_settlement_revisions_no_update
BEFORE UPDATE ON numeric_settlement_revisions BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS numeric_settlement_revisions_no_delete
BEFORE DELETE ON numeric_settlement_revisions BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS actor_attestation_nonce_claims_no_update
BEFORE UPDATE ON actor_attestation_nonce_claims BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
"""


class LedgerConflictError(ValueError):
    """An idempotency key was reused for a different canonical request."""


class SettlementConflictError(ValueError):
    """A settlement does not match its prediction or lacks correction data."""


class LedgerQueryTimeoutError(TimeoutError):
    """A bounded SQLite read was cooperatively interrupted at its deadline."""


class SQLiteQueryDeadline:
    """Thread-safe cancellation/deadline state for SQLite progress handlers."""

    def __init__(self, *, timeout_seconds: float) -> None:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise ValueError("timeout_seconds must be finite, positive, and at most 60")
        self._deadline = time.monotonic() + timeout
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        if time.monotonic() >= self._deadline:
            self._cancelled.set()
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    def remaining_milliseconds(self) -> int:
        return max(1, min(30_000, int((self._deadline - time.monotonic()) * 1000)))

    def progress_handler(self) -> int:
        return 1 if self.cancelled else 0

    def raise_if_expired(self) -> None:
        if self.cancelled:
            raise LedgerQueryTimeoutError("bounded SQLite read exceeded its deadline")


@dataclass(frozen=True)
class LedgerPrediction:
    """Sanitized immutable prediction payload stored for one competitor."""

    competitor_id: str
    event_code: str
    median_seconds: float
    assigned_mark: int
    source: str
    engine_version: Optional[str] = None
    model_version: Optional[str] = None
    calibration_version: Optional[str] = None
    evidence_cutoff: Optional[date] = None
    interval_lower: Optional[float] = None
    interval_upper: Optional[float] = None
    interval_coverage: Optional[float] = None
    interval_state: Optional[str] = None
    interval_scope: Optional[str] = None
    ignored_factors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    optimizer: Optional[str] = None
    optimizer_metadata: Mapping[str, Any] = field(default_factory=dict)
    feature_snapshot: Mapping[str, float] = field(default_factory=dict)
    training_eligible: bool = False
    degraded: bool = False


@dataclass(frozen=True)
class LedgerWriteResult:
    recorded: bool
    status: str
    prediction_ids: tuple[str, ...]
    request_hash: str
    hash_algorithm: str = _ACTIVE_HASH_ALGORITHM
    cloud_status: str = "not_configured"


@dataclass(frozen=True)
class SettlementResult:
    settlement_id: str
    prediction_id: str
    revision: int
    actual_time: float
    residual: float
    actor: str
    reason: Optional[str]
    supersedes_settlement_id: Optional[str]
    settled_at: str
    status: str = "recorded"
    cloud_status: str = "not_configured"


@dataclass(frozen=True)
class NumericSettlementRevision:
    """One strictly numeric Missoula outcome projection for a prediction."""

    prediction_id: str
    competitor_id: str
    event_code: str
    action: str
    actual_time: Optional[float]
    expected_revision: int


@dataclass(frozen=True)
class NumericSettlementRevisionResult:
    """One append-only numeric settlement or void revision."""

    revision_id: str
    prediction_id: str
    revision: int
    competitor_id: str
    event_code: str
    action: str
    actual_time: Optional[float]
    residual: Optional[float]
    supersedes_revision_id: Optional[str]
    created_at: str


@dataclass(frozen=True)
class NumericOutcomeRevisionResult:
    """Field-atomic result linked to one authoritative Missoula revision ID."""

    outcome_revision_id: str
    ledger_request_id: str
    caller_id: str
    revisions: tuple[NumericSettlementRevisionResult, ...]
    actor: str
    reason_code: Optional[str]
    created_at: str
    status: str = "recorded"
    cloud_status: str = "not_configured"


@dataclass(frozen=True)
class LedgerMonitoringStatus:
    """Payload-free operational facts derived from ledger and outbox rows."""

    mirror: str
    mirror_pending_count: int
    mirror_oldest_pending_at: Optional[str]
    mirror_last_attempt_at: Optional[str]
    local_trust: str
    receipt_freshness: str
    receipt_readiness: str
    numeric_mirror: str
    numeric_mirror_backlog_count: int
    numeric_mirror_oldest_pending_at: Optional[str]
    numeric_mirror_last_attempt_at: Optional[str]
    numeric_revision_count: int
    active_numeric_settlement_count: int
    voided_prediction_count: int
    evidence_sample_count: int
    evidence_status: str
    drift_calibration_advisory: str


Mirror = Callable[[Mapping[str, Any]], Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    if len(text) > 128:
        raise ValueError(f"{label} must be at most 128 characters")
    return text


def _legacy_settlement_reason_code(value: Any) -> Optional[str]:
    """Return a bounded code while accepting two fixed historical aliases."""

    text = str(value or "").strip()
    if not text:
        return None
    candidate = _LEGACY_SETTLEMENT_REASON_ALIASES.get(text.casefold(), text.casefold())
    if candidate not in LEGACY_SETTLEMENT_REASON_CODES:
        allowed = ", ".join(sorted(LEGACY_SETTLEMENT_REASON_CODES))
        raise ValueError(f"reason must be one of: {allowed}")
    return candidate


def _legacy_settlement_actor_id(value: Any) -> str:
    """Preserve fixed safe IDs and pseudonymize every legacy free-form value."""

    text = _identifier(value, "actor")
    if _NAMESPACED_ID.fullmatch(text) or text in _SAFE_LEGACY_SETTLEMENT_ACTOR_IDS:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"legacy:actor-{digest}"


def _legacy_egress_actor(value: Any) -> str:
    try:
        return _legacy_settlement_actor_id(value)
    except ValueError:
        return _LEGACY_REDACTED_ACTOR


def _legacy_egress_reason(value: Any) -> Optional[str]:
    try:
        return _legacy_settlement_reason_code(value)
    except ValueError:
        return _LEGACY_REDACTED_REASON


def _sanitize_settlement_mirror_payload(payload: Any) -> Mapping[str, Any]:
    """Redact historical legacy narrative at egress without mutating evidence."""

    if not isinstance(payload, Mapping):
        raise ValueError("settlement mirror payload must be an object")
    if "settlement" not in payload:
        # Numeric outcome payloads are already validated at their write boundary.
        return payload
    settlement = payload.get("settlement")
    if not isinstance(settlement, Mapping):
        raise ValueError("legacy settlement mirror payload must contain an object")
    sanitized = dict(payload)
    sanitized_settlement = dict(settlement)
    sanitized_settlement["actor"] = _legacy_egress_actor(settlement.get("actor"))
    sanitized_settlement["reason"] = _legacy_egress_reason(settlement.get("reason"))
    sanitized["settlement"] = sanitized_settlement
    return sanitized


def _namespaced_identifier(value: Any, label: str) -> str:
    text = _identifier(value, label)
    if not _NAMESPACED_ID.fullmatch(text):
        raise ValueError(f"{label} must be namespaced as 'namespace:value'")
    return text


def _identifier_namespace(value: str) -> str:
    """Return the authority prefix from a validated namespaced identifier."""

    return value.split(":", 1)[0]


def _event(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text not in {"SB", "UH"}:
        raise ValueError("event_code must be 'SB' or 'UH'")
    return text


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be numeric and finite") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{label} must be numeric and finite")
    return number


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical request payload contains a non-finite number")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError(f"canonical request payload contains unsupported type {type(value).__name__}")


def canonical_hash(payload: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 digest without retaining the request payload."""

    encoded = json.dumps(
        _json_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PredictionLedger:
    """SQLite-backed append-only prediction and settlement ledger."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        mirror: Optional[Mirror] = None,
    ) -> None:
        if db_path is None:
            db_path = os.environ.get(_ENV_VAR) or _DEFAULT_PATH
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mirror = mirror
        self._delivery_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._delivery_queue: deque[tuple[str, str]] = deque()
        self._delivery_in_flight: set[tuple[str, str]] = set()
        self._mirror_worker: Optional[threading.Thread] = None
        self._init_schema()
        if self._mirror is not None and self._has_pending_delivery():
            with self._worker_lock:
                self._start_mirror_worker_locked()

    def _connect(
        self, *, query_deadline: Optional[SQLiteQueryDeadline] = None
    ) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if query_deadline is not None:
            query_deadline.raise_if_expired()
            conn.execute(f"PRAGMA busy_timeout = {query_deadline.remaining_milliseconds()}")
            conn.set_progress_handler(query_deadline.progress_handler, 100)
        else:
            conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            request_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(prediction_requests)").fetchall()
            }
            if "hash_algorithm" not in request_columns:
                # Rows written before active-v2 hashed raw request history.  The
                # additive column labels them without rewriting immutable digests.
                conn.execute(
                    "ALTER TABLE prediction_requests ADD COLUMN hash_algorithm "
                    "TEXT NOT NULL DEFAULT 'raw-v1' "
                    "CHECK(hash_algorithm IN ('raw-v1', 'active-v2'))"
                )
            conn.execute("DROP TRIGGER IF EXISTS actor_attestation_nonce_claims_no_delete")
            conn.executescript(_IMMUTABILITY_TRIGGERS)

    def record_field(
        self,
        caller_id: str,
        request_id: str,
        request_payload: Mapping[str, Any],
        predictions: Sequence[LedgerPrediction],
        *,
        legacy_request_payload: Optional[Mapping[str, Any]] = None,
        receipt_metadata: Optional[Mapping[str, Any]] = None,
    ) -> LedgerWriteResult:
        """Atomically append one complete field or return its original IDs."""

        caller = _identifier(caller_id, "caller_id")
        idempotency_key = _identifier(request_id, "request_id")
        if not predictions:
            raise ValueError("predictions must not be empty")
        event_code = _event(request_payload.get("event_code"))
        prediction_as_of = str(request_payload.get("prediction_as_of") or "").strip()
        try:
            request_cutoff = date.fromisoformat(prediction_as_of)
        except ValueError as exc:
            raise ValueError("prediction_as_of must be an ISO date") from exc
        validated = [self._validate_prediction(item) for item in predictions]
        if len({item["competitor_id"] for item in validated}) != len(validated):
            raise ValueError("competitor_id values must be unique within a field")
        legacy_validated = [dict(item) for item in validated]
        for item in legacy_validated:
            # raw-v1 predates the hardening release's search-strategy field.
            # Preserve its exact serialized prediction shape for old retries.
            legacy_optimizer = dict(item["optimizer_metadata"])
            legacy_optimizer.pop("search_strategy", None)
            item["optimizer_metadata"] = legacy_optimizer
            if (
                item["degraded"]
                or item["engine_version"] != ENGINE_VERSION
                or item["source"] not in {"baseline", "ml"}
            ):
                item["training_eligible"] = False
        for item in validated:
            item["training_eligible"] = self._training_eligible(item, request_cutoff)

        request_row_id = str(uuid.uuid5(_LEDGER_NAMESPACE, f"request:{caller}:{idempotency_key}"))
        timestamp = _now()
        prediction_ids: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT ledger_request_id, request_hash, hash_algorithm
                    FROM prediction_requests
                    WHERE caller_id = ? AND request_id = ?
                    """,
                    (caller, idempotency_key),
                ).fetchone()
                if existing is not None:
                    algorithm = str(existing["hash_algorithm"])
                    digest_payload = (
                        legacy_request_payload
                        if algorithm == _RAW_HASH_ALGORITHM and legacy_request_payload is not None
                        else request_payload
                    )
                    digest = canonical_hash(
                        {
                            "request": digest_payload,
                            "predictions": (
                                legacy_validated if algorithm == _RAW_HASH_ALGORITHM else validated
                            ),
                        }
                    )
                    if existing["request_hash"] != digest:
                        raise LedgerConflictError(
                            "request_id was already used by this caller for a different payload"
                        )
                    rows = conn.execute(
                        """
                        SELECT prediction_id FROM ledger_predictions
                        WHERE ledger_request_id = ? ORDER BY ordinal
                        """,
                        (existing["ledger_request_id"],),
                    ).fetchall()
                    existing_request_id = str(existing["ledger_request_id"])
                    conn.commit()
                    cloud_status = self._schedule_delivery("field", existing_request_id)
                    status = "duplicate_cloud_pending" if cloud_status == "pending" else "duplicate"
                    return LedgerWriteResult(
                        recorded=True,
                        status=status,
                        prediction_ids=tuple(row["prediction_id"] for row in rows),
                        request_hash=digest,
                        hash_algorithm=algorithm,
                        cloud_status=cloud_status,
                    )

                algorithm = _ACTIVE_HASH_ALGORITHM
                digest = canonical_hash({"request": request_payload, "predictions": validated})
                conn.execute(
                    """
                    INSERT INTO prediction_requests (
                        ledger_request_id, caller_id, request_id, request_hash, hash_algorithm,
                        event_code, prediction_as_of, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_row_id,
                        caller,
                        idempotency_key,
                        digest,
                        algorithm,
                        event_code,
                        prediction_as_of,
                        timestamp,
                    ),
                )
                for ordinal, item in enumerate(validated):
                    prediction_id = str(
                        uuid.uuid5(
                            _LEDGER_NAMESPACE,
                            f"prediction:{request_row_id}:{item['competitor_id']}",
                        )
                    )
                    prediction_ids.append(prediction_id)
                    conn.execute(
                        """
                        INSERT INTO ledger_predictions (
                            prediction_id, ledger_request_id, competitor_id, ordinal,
                            event_code, median_seconds, assigned_mark, source,
                            training_eligible, engine_version, model_version,
                            calibration_version, evidence_cutoff, interval_lower,
                            interval_upper, interval_coverage, interval_state,
                            interval_scope, ignored_factors_json, warnings_json,
                            optimizer, optimizer_metadata_json, created_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            prediction_id,
                            request_row_id,
                            item["competitor_id"],
                            ordinal,
                            item["event_code"],
                            item["median_seconds"],
                            item["assigned_mark"],
                            item["source"],
                            int(item["training_eligible"]),
                            item["engine_version"],
                            item["model_version"],
                            item["calibration_version"],
                            item["evidence_cutoff"],
                            item["interval_lower"],
                            item["interval_upper"],
                            item["interval_coverage"],
                            item["interval_state"],
                            item["interval_scope"],
                            json.dumps(item["ignored_factors"], separators=(",", ":")),
                            json.dumps(item["warnings"], separators=(",", ":")),
                            item["optimizer"],
                            json.dumps(item["optimizer_metadata"], sort_keys=True),
                            timestamp,
                        ),
                    )
                    for feature_name, numeric_value in item["feature_snapshot"].items():
                        conn.execute(
                            """
                            INSERT INTO prediction_features (
                                feature_snapshot_id, prediction_id, feature_name,
                                numeric_value, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                str(
                                    uuid.uuid5(
                                        _LEDGER_NAMESPACE,
                                        f"feature:{prediction_id}:{feature_name}",
                                    )
                                ),
                                prediction_id,
                                feature_name,
                                numeric_value,
                                timestamp,
                            ),
                        )
                if receipt_metadata is not None:
                    receipt_core = self._shadow_receipt_core(
                        receipt_metadata,
                        request_hash=digest,
                        hash_algorithm=algorithm,
                        predictions=validated,
                        prediction_ids=prediction_ids,
                        timestamp=timestamp,
                    )
                    active_input = receipt_core.get("active_input")
                    if not isinstance(active_input, Mapping):
                        raise ValueError("receipt active_input must be an object")
                    active_fingerprint = str(active_input.get("fingerprint") or "").strip()
                    if len(active_fingerprint) != 64:
                        raise ValueError("receipt active input fingerprint must be SHA-256")
                    core_schema_version = str(receipt_core.get("schema_version") or "").strip()
                    if not core_schema_version:
                        raise ValueError("receipt schema_version must not be empty")
                    core_json = self._canonical_json(receipt_core)
                    conn.execute(
                        """
                        INSERT INTO shadow_receipts (
                            ledger_request_id, caller_id, request_id,
                            active_input_fingerprint, core_schema_version,
                            core_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request_row_id,
                            caller,
                            idempotency_key,
                            active_fingerprint,
                            core_schema_version,
                            core_json,
                            timestamp,
                        ),
                    )
                cloud_payload = self._cloud_field_payload(
                    request_row_id,
                    caller,
                    idempotency_key,
                    digest,
                    algorithm,
                    event_code,
                    prediction_as_of,
                    prediction_ids,
                    validated,
                    timestamp,
                )
                self._append_outbox(
                    conn,
                    kind="field",
                    entity_id=request_row_id,
                    payload=cloud_payload,
                    timestamp=timestamp,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        cloud_status = self._schedule_delivery("field", request_row_id)
        status = "recorded_cloud_pending" if cloud_status == "pending" else "recorded"
        return LedgerWriteResult(
            recorded=True,
            status=status,
            prediction_ids=tuple(prediction_ids),
            request_hash=digest,
            hash_algorithm=algorithm,
            cloud_status=cloud_status,
        )

    def claim_actor_attestation_nonce(
        self,
        *,
        consumer_id: str,
        nonce: str,
        actor_id: str,
        action: str,
        subject_revision: str,
        expires_at: int,
    ) -> bool:
        """Atomically claim a signed actor nonce in the durable local authority.

        Only a SHA-256 digest of the nonce is retained.  Active claims survive
        service restarts and block replay until their signed expiry; expired
        claims are transactionally purged so security state remains bounded.
        """

        consumer = _namespaced_identifier(consumer_id, "consumer_id")
        actor = _namespaced_identifier(actor_id, "actor_id")
        revision = _namespaced_identifier(subject_revision, "subject_revision")
        action_value = _identifier(action, "action")
        nonce_value = str(nonce or "")
        if not 16 <= len(nonce_value) <= 128:
            raise ValueError("nonce must contain between 16 and 128 characters")
        now_epoch = int(time.time())
        if (
            not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or expires_at <= now_epoch
        ):
            raise ValueError("expires_at must be a future integer timestamp")
        nonce_hash = hashlib.sha256(nonce_value.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM actor_attestation_nonce_claims WHERE expires_at <= ?",
                    (now_epoch,),
                )
                existing = conn.execute(
                    """
                    SELECT 1 FROM actor_attestation_nonce_claims
                    WHERE consumer_id = ? AND nonce_hash = ?
                    """,
                    (consumer, nonce_hash),
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    return False
                active_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM actor_attestation_nonce_claims WHERE consumer_id = ?",
                        (consumer,),
                    ).fetchone()[0]
                )
                if active_count >= MAX_ACTIVE_ATTESTATION_NONCES_PER_CONSUMER:
                    raise RuntimeError("active actor attestation nonce capacity is exhausted")
                conn.execute(
                    """
                    INSERT INTO actor_attestation_nonce_claims (
                        consumer_id, nonce_hash, actor_id, action,
                        subject_revision, expires_at, claimed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        consumer,
                        nonce_hash,
                        actor,
                        action_value,
                        revision,
                        expires_at,
                        _now(),
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            except RuntimeError:
                conn.rollback()
                raise

    def get_shadow_receipt(
        self,
        caller_id: str,
        request_id: str,
        *,
        current_active_fingerprint: Optional[str] = None,
        expected_run_revision: Optional[str] = None,
        query_deadline: Optional[SQLiteQueryDeadline] = None,
    ) -> Optional[Any]:
        """Return the exact immutable core plus a freshly derived status view."""

        caller = _identifier(caller_id, "caller_id")
        idempotency_key = _identifier(request_id, "request_id")
        with self._connect(query_deadline=query_deadline) as conn:
            row = conn.execute(
                """
                SELECT q.ledger_request_id, q.request_hash, q.hash_algorithm,
                       r.core_json, r.active_input_fingerprint, r.core_schema_version,
                       d.status AS delivery_status
                FROM prediction_requests q
                LEFT JOIN shadow_receipts r
                  ON r.ledger_request_id = q.ledger_request_id
                LEFT JOIN prediction_mirror_outbox o
                  ON o.kind = 'field' AND o.entity_id = q.ledger_request_id
                LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                WHERE q.caller_id = ? AND q.request_id = ?
                """,
                (caller, idempotency_key),
            ).fetchone()
            child_rows = (
                []
                if row is None
                else conn.execute(
                    """
                    SELECT prediction_id, competitor_id, ordinal, event_code,
                           median_seconds, assigned_mark, source, training_eligible,
                           engine_version, model_version, calibration_version,
                           evidence_cutoff, interval_lower, interval_upper,
                           interval_coverage, interval_state, interval_scope,
                           ignored_factors_json, warnings_json, optimizer,
                           optimizer_metadata_json
                    FROM ledger_predictions
                    WHERE ledger_request_id = ? ORDER BY ordinal
                    """,
                    (row["ledger_request_id"],),
                ).fetchall()
            )
        if row is None:
            return None
        if row["core_json"] is None:
            raise LedgerConflictError(
                "request_id already has an incomplete legacy ledger field without a receipt"
            )

        from strathmark.shadow import (
            RECEIPT_CORE_SCHEMA_VERSION,
            REQUEST_PROJECTION_SCHEMA_VERSION,
            ShadowLiveStatus,
            ShadowReceipt,
            ShadowReceiptCorruptionError,
        )

        core_json = str(row["core_json"])
        try:
            core = json.loads(core_json)
        except (TypeError, ValueError) as exc:
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt contains malformed JSON"
            ) from exc
        if not isinstance(core, Mapping):
            raise ShadowReceiptCorruptionError("persisted shadow receipt core is not an object")
        if self._canonical_json(core) != core_json:
            raise ShadowReceiptCorruptionError("persisted shadow receipt JSON is not canonical")
        if (
            core.get("schema_version") != RECEIPT_CORE_SCHEMA_VERSION
            or row["core_schema_version"] != RECEIPT_CORE_SCHEMA_VERSION
        ):
            raise ShadowReceiptCorruptionError("persisted shadow receipt schema is unsupported")
        if core.get("consumer_id") != caller or core.get("request_id") != idempotency_key:
            raise ShadowReceiptCorruptionError("persisted shadow receipt identity is inconsistent")
        request_projection = core.get("request_projection")
        if not isinstance(request_projection, Mapping):
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt request projection is missing"
            )
        projection_without_fingerprint = dict(request_projection)
        projection_fingerprint = str(projection_without_fingerprint.pop("fingerprint", ""))
        observation = core.get("observation")
        if (
            projection_without_fingerprint.get("schema_version")
            != REQUEST_PROJECTION_SCHEMA_VERSION
            or canonical_hash(projection_without_fingerprint) != projection_fingerprint
            or projection_without_fingerprint.get("consumer_id") != caller
            or projection_without_fingerprint.get("request_id") != idempotency_key
            or projection_without_fingerprint.get("run_revision") != core.get("run_revision")
            or projection_without_fingerprint.get("tournament_id") != core.get("tournament_id")
            or projection_without_fingerprint.get("event_occurrence_id")
            != core.get("event_occurrence_id")
            or projection_without_fingerprint.get("field_run_id") != core.get("field_run_id")
            or projection_without_fingerprint.get("operator_id") != core.get("operator_id")
            or projection_without_fingerprint.get("event_code") != core.get("event_code")
            or projection_without_fingerprint.get("target_contract") != core.get("target_contract")
            or projection_without_fingerprint.get("prediction_as_of")
            != core.get("prediction_as_of")
            or not isinstance(observation, Mapping)
            or projection_without_fingerprint.get("observation_schema_version")
            != observation.get("schema_version")
            or projection_without_fingerprint.get("observation_fingerprint")
            != observation.get("fingerprint")
        ):
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt request projection is inconsistent"
            )
        try:
            recorded_run_revision = _namespaced_identifier(
                core.get("run_revision"), "persisted run_revision"
            )
        except ValueError as exc:
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt run revision is invalid"
            ) from exc
        if expected_run_revision is not None:
            requested_run_revision = _namespaced_identifier(
                expected_run_revision, "expected_run_revision"
            )
            if recorded_run_revision != requested_run_revision:
                raise LedgerConflictError(
                    "run_revision does not match the immutable shadow receipt"
                )
        ledger_core = core.get("ledger")
        if not isinstance(ledger_core, Mapping) or (
            ledger_core.get("request_hash") != row["request_hash"]
            or ledger_core.get("hash_algorithm") != row["hash_algorithm"]
        ):
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt ledger identity is inconsistent"
            )
        active_input = core.get("active_input")
        if not isinstance(active_input, Mapping):
            raise ShadowReceiptCorruptionError("persisted shadow receipt active input is missing")
        active_without_fingerprint = dict(active_input)
        embedded_fingerprint = str(active_without_fingerprint.pop("fingerprint", ""))
        recorded_fingerprint = str(row["active_input_fingerprint"])
        if (
            embedded_fingerprint != recorded_fingerprint
            or canonical_hash(active_without_fingerprint) != recorded_fingerprint
        ):
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt active fingerprint is inconsistent"
            )
        predictions = core.get("predictions")
        if not isinstance(predictions, list) or len(predictions) != len(child_rows):
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt prediction set is incomplete"
            )
        for ordinal, (item, child) in enumerate(zip(predictions, child_rows, strict=True)):
            if not isinstance(item, Mapping) or (
                item.get("ordinal") != ordinal
                or item.get("prediction_id") != child["prediction_id"]
                or item.get("competitor_id") != child["competitor_id"]
                or item.get("event_code") != child["event_code"]
                or child["ordinal"] != ordinal
            ):
                raise ShadowReceiptCorruptionError(
                    "persisted shadow receipt prediction identity is inconsistent"
                )
            try:
                ignored_factors = json.loads(str(child["ignored_factors_json"]))
                warnings = json.loads(str(child["warnings_json"]))
                optimizer_metadata = json.loads(str(child["optimizer_metadata_json"]))
                persisted_projection = self._shadow_prediction_projection(
                    {
                        **dict(child),
                        "training_eligible": bool(child["training_eligible"]),
                        "ignored_factors": ignored_factors,
                        "warnings": warnings,
                        "optimizer_metadata": optimizer_metadata,
                    },
                    prediction_id=str(child["prediction_id"]),
                    ordinal=ordinal,
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ShadowReceiptCorruptionError(
                    "persisted ledger prediction projection is malformed"
                ) from exc
            if self._canonical_json(dict(item)) != self._canonical_json(persisted_projection):
                raise ShadowReceiptCorruptionError(
                    "persisted shadow receipt prediction payload is inconsistent"
                )
        caller_input = active_input.get("caller_input")
        entrants = caller_input.get("competitors") if isinstance(caller_input, Mapping) else None
        request_entrants = request_projection.get("competitors")
        if not isinstance(entrants, list) or {
            item.get("competitor_id") for item in entrants if isinstance(item, Mapping)
        } != {child["competitor_id"] for child in child_rows}:
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt entrant set is inconsistent"
            )
        if not isinstance(request_entrants, list) or [
            item.get("competitor_id") for item in request_entrants if isinstance(item, Mapping)
        ] != [item.get("competitor_id") for item in entrants if isinstance(item, Mapping)]:
            raise ShadowReceiptCorruptionError(
                "persisted request projection entrant order is inconsistent"
            )

        freshness = (
            "current"
            if current_active_fingerprint is None
            or str(current_active_fingerprint) == recorded_fingerprint
            else "stale"
        )
        delivery_status = row["delivery_status"]
        if self._mirror is None:
            mirror = "not-configured"
        elif delivery_status == "recorded":
            mirror = "recorded"
        elif delivery_status == "failed":
            mirror = "retryable-failed"
        else:
            mirror = "pending"
        status = ShadowLiveStatus(
            trust="recorded",
            mirror=mirror,
            freshness=freshness,
            ready_for_review=freshness == "current",
        )
        return ShadowReceipt(
            core_json=core_json,
            core=core,
            status=status,
        )

    def apply_numeric_outcome_revision(
        self,
        outcome_revision_id: str,
        revisions: Sequence[NumericSettlementRevision | Mapping[str, Any]],
        *,
        caller_id: str,
        request_id: str,
        run_revision: str,
        actor: str,
        reason_code: Optional[str] = None,
    ) -> NumericOutcomeRevisionResult:
        """Atomically append eligible numeric settlements or retraction voids.

        Missoula remains authoritative for finish/nonfinish classification and
        operational outcome history.  This boundary deliberately accepts only
        positive raw elapsed times or voids linked to a stable outcome revision.
        The actor is namespace-bound here, but is not authenticated here; the U4
        transport must verify its signed actor/action attestation before calling
        this storage method.
        """

        outcome_key = _namespaced_identifier(outcome_revision_id, "outcome_revision_id")
        requested_caller = _namespaced_identifier(caller_id, "caller_id")
        requested_request = _namespaced_identifier(request_id, "request_id")
        requested_run = _namespaced_identifier(run_revision, "run_revision")
        actor_value = _namespaced_identifier(actor, "actor")
        reason_code_value = str(reason_code or "").strip() or None
        if reason_code_value is not None and reason_code_value not in NUMERIC_OUTCOME_REASON_CODES:
            allowed = ", ".join(sorted(NUMERIC_OUTCOME_REASON_CODES))
            raise ValueError(f"reason_code must be one of: {allowed}")
        if not revisions:
            raise ValueError("revisions must not be empty")
        if len(revisions) > MAX_NUMERIC_SETTLEMENTS_PER_REVISION:
            raise ValueError(
                "revisions must contain at most "
                f"{MAX_NUMERIC_SETTLEMENTS_PER_REVISION} numeric projections"
            )

        validated = [self._validate_numeric_revision(item) for item in revisions]
        if len({item["prediction_id"] for item in validated}) != len(validated):
            raise ValueError("prediction_id values must be unique within an outcome revision")
        validated.sort(key=lambda item: item["prediction_id"])
        payload_digest = canonical_hash(
            {
                "outcome_revision_id": outcome_key,
                "caller_id": requested_caller,
                "request_id": requested_request,
                "run_revision": requested_run,
                "actor": actor_value,
                "reason_code": reason_code_value,
                "revisions": validated,
            }
        )
        field_revision_id = str(uuid.uuid5(_LEDGER_NAMESPACE, f"numeric-outcome:{outcome_key}"))
        timestamp = _now()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                placeholders = ",".join("?" for _ in validated)
                prediction_rows = conn.execute(
                    f"""
                    SELECT p.prediction_id, p.competitor_id, p.event_code,
                           p.median_seconds, p.ledger_request_id,
                           r.caller_id, r.request_id,
                           sr.caller_id AS receipt_caller_id,
                           sr.request_id AS receipt_request_id,
                           sr.core_json AS receipt_core_json
                    FROM ledger_predictions p
                    JOIN prediction_requests r
                      ON r.ledger_request_id = p.ledger_request_id
                    LEFT JOIN shadow_receipts sr
                      ON sr.ledger_request_id = p.ledger_request_id
                    WHERE p.prediction_id IN ({placeholders})
                    """,
                    [item["prediction_id"] for item in validated],
                ).fetchall()
                predictions_by_id = {str(row["prediction_id"]): row for row in prediction_rows}
                if len(predictions_by_id) != len(validated):
                    raise SettlementConflictError("prediction_id was not found")
                for item in validated:
                    prediction = predictions_by_id[item["prediction_id"]]
                    if prediction["competitor_id"] != item["competitor_id"]:
                        raise SettlementConflictError("competitor_id does not match prediction")
                    if prediction["event_code"] != item["event_code"]:
                        raise SettlementConflictError("event_code does not match prediction")

                bound_callers = {str(row["caller_id"]) for row in prediction_rows}
                if len(bound_callers) != 1:
                    raise SettlementConflictError(
                        "one numeric outcome revision must contain predictions from one caller_id"
                    )
                if bound_callers != {requested_caller}:
                    raise SettlementConflictError(
                        "numeric outcome predictions do not belong to the authenticated caller"
                    )
                ledger_request_ids = {str(row["ledger_request_id"]) for row in prediction_rows}
                if len(ledger_request_ids) != 1:
                    raise SettlementConflictError(
                        "one numeric outcome revision must contain predictions from one "
                        "ledger_request_id"
                    )
                ledger_request_id = next(iter(ledger_request_ids))
                if {str(row["request_id"]) for row in prediction_rows} != {requested_request}:
                    raise SettlementConflictError(
                        "numeric outcome predictions do not belong to request_id"
                    )
                receipt_json_values = {row["receipt_core_json"] for row in prediction_rows}
                if None in receipt_json_values or len(receipt_json_values) != 1:
                    raise SettlementConflictError(
                        "numeric outcome requires one immutable shadow receipt"
                    )
                receipt_json = str(next(iter(receipt_json_values)))
                try:
                    receipt_core = json.loads(receipt_json)
                except (TypeError, ValueError) as exc:
                    raise SettlementConflictError("immutable shadow receipt is malformed") from exc
                if (
                    not isinstance(receipt_core, Mapping)
                    or self._canonical_json(receipt_core) != receipt_json
                    or receipt_core.get("consumer_id") != requested_caller
                    or receipt_core.get("request_id") != requested_request
                    or {str(row["receipt_caller_id"]) for row in prediction_rows}
                    != {requested_caller}
                    or {str(row["receipt_request_id"]) for row in prediction_rows}
                    != {requested_request}
                ):
                    raise SettlementConflictError(
                        "immutable shadow receipt does not match caller/request_id"
                    )
                try:
                    recorded_run = _namespaced_identifier(
                        receipt_core.get("run_revision"),
                        "persisted run_revision",
                    )
                except ValueError as exc:
                    raise SettlementConflictError(
                        "immutable shadow receipt run_revision is invalid"
                    ) from exc
                if recorded_run != requested_run:
                    raise SettlementConflictError(
                        "run_revision does not match the immutable shadow receipt"
                    )
                if _identifier_namespace(outcome_key) != _identifier_namespace(requested_caller):
                    raise SettlementConflictError(
                        "outcome_revision_id namespace must match the caller_id namespace"
                    )
                if _identifier_namespace(actor_value) != _identifier_namespace(requested_caller):
                    raise SettlementConflictError(
                        "actor namespace must match the caller_id namespace"
                    )

                existing = conn.execute(
                    "SELECT * FROM numeric_outcome_revisions WHERE outcome_revision_id = ?",
                    (outcome_key,),
                ).fetchone()
                if existing is not None:
                    if existing["caller_id"] != requested_caller:
                        raise SettlementConflictError(
                            "outcome_revision_id does not belong to the authenticated caller"
                        )
                    if existing["ledger_request_id"] != ledger_request_id:
                        raise SettlementConflictError(
                            "outcome_revision_id does not belong to request_id"
                        )
                    if existing["payload_hash"] != payload_digest:
                        raise SettlementConflictError(
                            "outcome_revision_id was already used for a different payload"
                        )
                    result = self._numeric_outcome_result(conn, existing, status="duplicate")
                    conn.commit()
                    cloud_status = self._schedule_delivery(
                        "settlement", str(existing["field_revision_id"])
                    )
                    return replace(result, cloud_status=cloud_status)

                prepared: list[dict[str, Any]] = []
                any_correction = False
                for item in validated:
                    prediction = predictions_by_id[item["prediction_id"]]

                    latest = conn.execute(
                        """
                        SELECT revision, revision_id, action
                        FROM (
                            SELECT revision, settlement_id AS revision_id,
                                   'settle' AS action, 0 AS source_priority,
                                   settled_at AS authority_timestamp
                            FROM prediction_settlements
                            WHERE prediction_id = ?
                            UNION ALL
                            SELECT revision, revision_id, action, 1 AS source_priority,
                                   created_at AS authority_timestamp
                            FROM numeric_settlement_revisions
                            WHERE prediction_id = ?
                        )
                        ORDER BY revision DESC, source_priority DESC,
                                 authority_timestamp DESC, revision_id DESC
                        LIMIT 1
                        """,
                        (item["prediction_id"], item["prediction_id"]),
                    ).fetchone()
                    latest_revision = 0 if latest is None else int(latest["revision"])
                    if item["expected_revision"] != latest_revision:
                        raise SettlementConflictError(
                            f"expected revision {item['expected_revision']} but latest is "
                            f"{latest_revision} for prediction_id {item['prediction_id']}"
                        )
                    if item["action"] == "void" and (latest is None or latest["action"] == "void"):
                        raise SettlementConflictError(
                            "void requires a currently active numeric settlement"
                        )
                    if latest is not None:
                        any_correction = True

                    revision_number = latest_revision + 1
                    revision_id = str(
                        uuid.uuid5(
                            _LEDGER_NAMESPACE,
                            f"numeric-settlement:{outcome_key}:{item['prediction_id']}",
                        )
                    )
                    actual_time = item["actual_time"]
                    residual = (
                        None
                        if item["action"] == "void"
                        else float(actual_time) - float(prediction["median_seconds"])
                    )
                    prepared.append(
                        {
                            **item,
                            "revision_id": revision_id,
                            "revision": revision_number,
                            "residual": residual,
                            "supersedes_revision_id": (
                                None if latest is None else str(latest["revision_id"])
                            ),
                            "ledger_request_id": str(prediction["ledger_request_id"]),
                            "caller_id": str(prediction["caller_id"]),
                        }
                    )

                caller_ids = {item["caller_id"] for item in prepared}
                if len(caller_ids) != 1:
                    raise SettlementConflictError(
                        "one numeric outcome revision must contain predictions from one caller_id"
                    )
                caller_id = next(iter(caller_ids))
                if caller_id != requested_caller:
                    raise SettlementConflictError(
                        "numeric outcome predictions do not belong to the authenticated caller"
                    )
                if (any_correction or any(item["action"] == "void" for item in prepared)) and (
                    reason_code_value is None
                ):
                    raise SettlementConflictError(
                        "a numeric correction or void requires a reason_code"
                    )

                conn.execute(
                    """
                    INSERT INTO numeric_outcome_revisions (
                        field_revision_id, outcome_revision_id, ledger_request_id,
                        caller_id, payload_hash, actor, reason_code, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        field_revision_id,
                        outcome_key,
                        ledger_request_id,
                        caller_id,
                        payload_digest,
                        actor_value,
                        reason_code_value,
                        timestamp,
                    ),
                )
                for item in prepared:
                    conn.execute(
                        """
                        INSERT INTO numeric_settlement_revisions (
                            revision_id, field_revision_id, prediction_id, revision,
                            competitor_id, event_code, action, actual_time, residual,
                            supersedes_revision_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["revision_id"],
                            field_revision_id,
                            item["prediction_id"],
                            item["revision"],
                            item["competitor_id"],
                            item["event_code"],
                            item["action"],
                            item["actual_time"],
                            item["residual"],
                            item["supersedes_revision_id"],
                            timestamp,
                        ),
                    )

                cloud_payload = {
                    "numeric_outcome_revision": {
                        "outcome_revision_id": outcome_key,
                        "ledger_request_id": ledger_request_id,
                        "caller_id": caller_id,
                        "actor": actor_value,
                        "reason_code": reason_code_value,
                        "created_at": timestamp,
                        "revisions": [
                            {
                                "revision_id": item["revision_id"],
                                "prediction_id": item["prediction_id"],
                                "revision": item["revision"],
                                "competitor_id": item["competitor_id"],
                                "event_code": item["event_code"],
                                "action": item["action"],
                                "actual_time": item["actual_time"],
                                "residual": item["residual"],
                                "supersedes_revision_id": item["supersedes_revision_id"],
                            }
                            for item in prepared
                        ],
                    }
                }
                self._append_outbox(
                    conn,
                    kind="settlement",
                    entity_id=field_revision_id,
                    payload=cloud_payload,
                    timestamp=timestamp,
                )
                outcome_row = conn.execute(
                    "SELECT * FROM numeric_outcome_revisions WHERE field_revision_id = ?",
                    (field_revision_id,),
                ).fetchone()
                result = self._numeric_outcome_result(conn, outcome_row)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        cloud_status = self._schedule_delivery("settlement", field_revision_id)
        return replace(result, cloud_status=cloud_status)

    def get_numeric_outcome_revision(
        self, outcome_revision_id: str
    ) -> Optional[NumericOutcomeRevisionResult]:
        """Return a numeric revision and its current derived mirror state."""

        outcome_key = _namespaced_identifier(outcome_revision_id, "outcome_revision_id")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.*, d.status AS delivery_status
                FROM numeric_outcome_revisions r
                LEFT JOIN prediction_mirror_outbox o
                  ON o.kind = 'settlement' AND o.entity_id = r.field_revision_id
                LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                WHERE r.outcome_revision_id = ?
                """,
                (outcome_key,),
            ).fetchone()
            if row is None:
                return None
            if self._mirror is None:
                cloud_status = "not_configured"
            elif row["delivery_status"] == "recorded":
                cloud_status = "recorded"
            elif row["delivery_status"] == "failed":
                cloud_status = "retryable-failed"
            else:
                cloud_status = "pending"
            return self._numeric_outcome_result(
                conn,
                row,
                cloud_status=cloud_status,
            )

    def get_monitoring_status(
        self,
        *,
        model_version: Optional[str] = None,
        caller_id: Optional[str] = None,
        request_id: Optional[str] = None,
        current_active_fingerprint: Optional[str] = None,
        expected_run_revision: Optional[str] = None,
        query_deadline: Optional[SQLiteQueryDeadline] = None,
    ) -> LedgerMonitoringStatus:
        """Derive payload-free mirror and numeric evidence monitoring facts."""

        if (
            (caller_id is None) != (request_id is None)
            or (current_active_fingerprint is not None and caller_id is None)
            or (expected_run_revision is not None and caller_id is None)
        ):
            raise ValueError(
                "caller_id and request_id are required together for receipt monitoring"
            )

        scoped_caller = (
            None if caller_id is None else _namespaced_identifier(caller_id, "caller_id")
        )
        with self._connect(query_deadline=query_deadline) as conn:
            mirror_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS pending_count,
                    MIN(o.created_at) AS oldest_pending_at,
                    MAX(d.last_attempt_at) AS last_attempt_at,
                    SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                FROM prediction_mirror_outbox o
                JOIN prediction_requests q
                  ON o.kind = 'field' AND q.ledger_request_id = o.entity_id
                LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                WHERE (d.status IS NULL OR d.status != 'recorded')
                  AND (? IS NULL OR q.caller_id = ?)
                """,
                (scoped_caller, scoped_caller),
            ).fetchone()
            numeric_mirror_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS pending_count,
                    MIN(o.created_at) AS oldest_pending_at,
                    MAX(d.last_attempt_at) AS last_attempt_at,
                    SUM(CASE WHEN d.status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                FROM numeric_outcome_revisions r
                JOIN prediction_mirror_outbox o
                  ON o.kind = 'settlement' AND o.entity_id = r.field_revision_id
                LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                WHERE (d.status IS NULL OR d.status != 'recorded')
                  AND (? IS NULL OR r.caller_id = ?)
                """,
                (scoped_caller, scoped_caller),
            ).fetchone()
            revision_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM numeric_settlement_revisions s
                    JOIN numeric_outcome_revisions r
                      ON r.field_revision_id = s.field_revision_id
                    WHERE ? IS NULL OR r.caller_id = ?
                    """,
                    (scoped_caller, scoped_caller),
                ).fetchone()[0]
            )
            latest_rows = conn.execute(
                """
                SELECT current.action
                FROM numeric_settlement_revisions current
                JOIN numeric_outcome_revisions r
                  ON r.field_revision_id = current.field_revision_id
                WHERE current.revision = (
                    SELECT MAX(candidate.revision)
                    FROM numeric_settlement_revisions candidate
                    WHERE candidate.prediction_id = current.prediction_id
                )
                  AND (? IS NULL OR r.caller_id = ?)
                """,
                (scoped_caller, scoped_caller),
            ).fetchall()

        pending_count = int(mirror_row["pending_count"] or 0)
        if self._mirror is None:
            mirror = "not-configured"
        elif int(mirror_row["failed_count"] or 0) > 0:
            mirror = "retryable-failed"
        elif pending_count > 0:
            mirror = "pending"
        else:
            mirror = "recorded"
        numeric_pending_count = int(numeric_mirror_row["pending_count"] or 0)
        if self._mirror is None:
            numeric_mirror = "not-configured"
        elif int(numeric_mirror_row["failed_count"] or 0) > 0:
            numeric_mirror = "retryable-failed"
        elif numeric_pending_count > 0:
            numeric_mirror = "pending"
        else:
            numeric_mirror = "recorded"

        local_trust = "unavailable"
        receipt_freshness = "unavailable"
        receipt_readiness = "unavailable"
        if caller_id is not None and request_id is not None:
            from strathmark.shadow import ShadowReceiptCorruptionError

            try:
                receipt = self.get_shadow_receipt(
                    caller_id,
                    request_id,
                    current_active_fingerprint=current_active_fingerprint,
                    expected_run_revision=expected_run_revision,
                    query_deadline=query_deadline,
                )
            except LedgerConflictError:
                if expected_run_revision is not None:
                    raise
                local_trust = "invalid"
                receipt_readiness = "not-ready"
            except ShadowReceiptCorruptionError:
                local_trust = "invalid"
                receipt_readiness = "not-ready"
            else:
                if receipt is None:
                    local_trust = "missing"
                    receipt_freshness = "missing"
                    receipt_readiness = "not-ready"
                else:
                    local_trust = str(receipt.status.trust)
                    receipt_freshness = str(receipt.status.freshness)
                    receipt_readiness = "ready" if receipt.status.ready_for_review else "not-ready"

        evidence_count = self.count_training_rows(
            model_version=model_version,
            caller_id=scoped_caller,
            query_deadline=query_deadline,
        )
        from strathmark.drift import MIN_RECENT_SAMPLES

        evidence_floor_met = evidence_count >= MIN_RECENT_SAMPLES
        evidence_status = (
            "minimum-sample-available" if evidence_floor_met else "insufficient-evidence"
        )
        drift_calibration_advisory = (
            "not-evaluated" if evidence_floor_met else "insufficient-evidence"
        )

        return LedgerMonitoringStatus(
            mirror=mirror,
            mirror_pending_count=pending_count,
            mirror_oldest_pending_at=mirror_row["oldest_pending_at"],
            mirror_last_attempt_at=mirror_row["last_attempt_at"],
            local_trust=local_trust,
            receipt_freshness=receipt_freshness,
            receipt_readiness=receipt_readiness,
            numeric_mirror=numeric_mirror,
            numeric_mirror_backlog_count=numeric_pending_count,
            numeric_mirror_oldest_pending_at=numeric_mirror_row["oldest_pending_at"],
            numeric_mirror_last_attempt_at=numeric_mirror_row["last_attempt_at"],
            numeric_revision_count=revision_count,
            active_numeric_settlement_count=sum(row["action"] == "settle" for row in latest_rows),
            voided_prediction_count=sum(row["action"] == "void" for row in latest_rows),
            evidence_sample_count=evidence_count,
            evidence_status=evidence_status,
            drift_calibration_advisory=drift_calibration_advisory,
        )

    def settle(
        self,
        prediction_id: str,
        competitor_id: str,
        event_code: str,
        actual_time: float,
        actor: str,
        reason: Optional[str] = None,
    ) -> SettlementResult:
        """Append an idempotent settlement or an attributed correction."""

        prediction_key = _identifier(prediction_id, "prediction_id")
        competitor_key = _identifier(competitor_id, "competitor_id")
        event = _event(event_code)
        actual = _finite(actual_time, "actual_time", positive=True)
        submitted_actor = _identifier(actor, "actor")
        actor_value = _legacy_settlement_actor_id(submitted_actor)
        submitted_reason = str(reason or "").strip() or None
        reason_error: Optional[ValueError] = None
        try:
            reason_value = _legacy_settlement_reason_code(submitted_reason)
        except ValueError as exc:
            reason_error = exc
            reason_value = submitted_reason

        actor_candidates = (submitted_actor, actor_value)
        reason_candidates = [submitted_reason]
        if reason_error is None and reason_value != submitted_reason:
            reason_candidates.append(reason_value)
        candidate_digests = list(
            dict.fromkeys(
                canonical_hash(
                    {
                        "prediction_id": prediction_key,
                        "competitor_id": competitor_key,
                        "event_code": event,
                        "actual_time": actual,
                        "actor": candidate_actor,
                        "reason": candidate_reason,
                    }
                )
                for candidate_actor in actor_candidates
                for candidate_reason in reason_candidates
            )
        )
        normalized_payload_digest = (
            None
            if reason_error is not None
            else canonical_hash(
                {
                    "prediction_id": prediction_key,
                    "competitor_id": competitor_key,
                    "event_code": event,
                    "actual_time": actual,
                    "actor": actor_value,
                    "reason": reason_value,
                }
            )
        )
        timestamp = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prediction = conn.execute(
                    """
                    SELECT competitor_id, event_code, median_seconds
                    FROM ledger_predictions WHERE prediction_id = ?
                    """,
                    (prediction_key,),
                ).fetchone()
                if prediction is None:
                    raise SettlementConflictError("prediction_id was not found")
                if prediction["competitor_id"] != competitor_key:
                    raise SettlementConflictError("competitor_id does not match prediction")
                if prediction["event_code"] != event:
                    raise SettlementConflictError("event_code does not match prediction")

                duplicate = None
                for candidate_digest in candidate_digests:
                    duplicate = conn.execute(
                        """
                        SELECT * FROM prediction_settlements
                        WHERE prediction_id = ? AND payload_hash = ?
                        """,
                        (prediction_key, candidate_digest),
                    ).fetchone()
                    if duplicate is not None:
                        break
                if duplicate is not None:
                    conn.commit()
                    result = self._settlement_from_row(duplicate, status="duplicate")
                    cloud_status = self._schedule_delivery(
                        "settlement", str(duplicate["settlement_id"])
                    )
                    return replace(result, cloud_status=cloud_status)

                numeric_authority = conn.execute(
                    """
                    SELECT 1 FROM numeric_settlement_revisions
                    WHERE prediction_id = ? LIMIT 1
                    """,
                    (prediction_key,),
                ).fetchone()
                if numeric_authority is not None:
                    raise SettlementConflictError(
                        "numeric settlement revisions are authoritative for this prediction; "
                        "legacy settle() is closed"
                    )
                if reason_error is not None:
                    raise reason_error
                assert normalized_payload_digest is not None
                payload_digest = normalized_payload_digest

                latest = conn.execute(
                    """
                    SELECT * FROM prediction_settlements
                    WHERE prediction_id = ? ORDER BY revision DESC LIMIT 1
                    """,
                    (prediction_key,),
                ).fetchone()
                if latest is not None and reason_value is None:
                    raise SettlementConflictError(
                        "a correction requires a non-empty reason and actor"
                    )

                revision = 1 if latest is None else int(latest["revision"]) + 1
                supersedes = None if latest is None else str(latest["settlement_id"])
                settlement_id = str(
                    uuid.uuid5(
                        _LEDGER_NAMESPACE,
                        f"settlement:{prediction_key}:{payload_digest}",
                    )
                )
                residual = actual - float(prediction["median_seconds"])
                conn.execute(
                    """
                    INSERT INTO prediction_settlements (
                        settlement_id, prediction_id, revision, competitor_id,
                        event_code, actual_time, residual, actor, reason,
                        payload_hash, supersedes_settlement_id, settled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        settlement_id,
                        prediction_key,
                        revision,
                        competitor_key,
                        event,
                        actual,
                        residual,
                        actor_value,
                        reason_value,
                        payload_digest,
                        supersedes,
                        timestamp,
                    ),
                )
                cloud_payload = {
                    "settlement": {
                        "settlement_id": settlement_id,
                        "prediction_id": prediction_key,
                        "revision": revision,
                        "competitor_id": competitor_key,
                        "event_code": event,
                        "actual_time": actual,
                        "residual": residual,
                        "actor": actor_value,
                        "reason": reason_value,
                        "payload_hash": payload_digest,
                        "supersedes_settlement_id": supersedes,
                        "settled_at": timestamp,
                    }
                }
                self._append_outbox(
                    conn,
                    kind="settlement",
                    entity_id=settlement_id,
                    payload=cloud_payload,
                    timestamp=timestamp,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        cloud_status = self._schedule_delivery("settlement", settlement_id)
        return SettlementResult(
            settlement_id=settlement_id,
            prediction_id=prediction_key,
            revision=revision,
            actual_time=actual,
            residual=residual,
            actor=actor_value,
            reason=reason_value,
            supersedes_settlement_id=supersedes,
            settled_at=timestamp,
            cloud_status=cloud_status,
        )

    def get_settlements(self, prediction_id: str) -> list[SettlementResult]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM prediction_settlements
                WHERE prediction_id = ? ORDER BY revision
                """,
                (_identifier(prediction_id, "prediction_id"),),
            ).fetchall()
        return [self._settlement_from_row(row) for row in rows]

    def get_training_rows(
        self,
        *,
        since: Optional[date | datetime | str] = None,
        model_version: Optional[str] = None,
        calibration_version: Optional[str] = None,
        event_code: Optional[str] = None,
        history_band: Optional[str] = None,
        nominal_coverage: Optional[float] = None,
        interval_state: Optional[str] = None,
        interval_scope: Optional[str] = None,
        caller_id: Optional[str] = None,
        limit: Optional[int] = None,
        query_deadline: Optional[SQLiteQueryDeadline] = None,
    ) -> list[dict[str, Any]]:
        """Return current settled, explicitly eligible model predictions."""

        prediction_conditions = ["p.training_eligible = 1"]
        prediction_parameters: list[Any] = []
        if caller_id is not None:
            prediction_conditions.append("r.caller_id = ?")
            prediction_parameters.append(_namespaced_identifier(caller_id, "caller_id"))
        if model_version is not None:
            prediction_conditions.append("p.model_version = ?")
            prediction_parameters.append(str(model_version))
        if calibration_version is not None:
            prediction_conditions.append("p.calibration_version = ?")
            prediction_parameters.append(str(calibration_version))
        if event_code is not None:
            prediction_conditions.append("p.event_code = ?")
            prediction_parameters.append(_event(event_code))
        if nominal_coverage is not None:
            prediction_conditions.append("p.interval_coverage = ?")
            prediction_parameters.append(
                _finite(nominal_coverage, "nominal_coverage", positive=True)
            )
        if interval_state is not None:
            prediction_conditions.append("p.interval_state = ?")
            prediction_parameters.append(_identifier(interval_state, "interval_state"))
        if interval_scope is not None:
            prediction_conditions.append("p.interval_scope = ?")
            prediction_parameters.append(_identifier(interval_scope, "interval_scope"))
        post_conditions = ["s.action = 'settle'"]
        post_parameters: list[Any] = []
        if history_band is not None:
            band = str(history_band).strip()
            if band not in {"0", "1-3", "4+", "unavailable"}:
                raise ValueError("history_band must be '0', '1-3', '4+', or 'unavailable'")
            post_conditions.append(
                "CASE WHEN h.numeric_value IS NULL THEN 'unavailable' "
                "WHEN h.numeric_value < 1 THEN '0' "
                "WHEN h.numeric_value < 4 THEN '1-3' ELSE '4+' END = ?"
            )
            post_parameters.append(band)
        prediction_where = " AND ".join(prediction_conditions)
        post_where = " AND ".join(post_conditions)
        legacy_since_clause = ""
        numeric_since_clause = ""
        since_parameters: list[Any] = []
        if since is not None:
            since_value = since.isoformat() if hasattr(since, "isoformat") else str(since)
            legacy_since_clause = "WHERE source_settlement.settled_at >= ?"
            numeric_since_clause = "WHERE source_settlement.created_at >= ?"
            since_parameters = [since_value, since_value]
        limit_clause = ""
        limit_parameters: list[Any] = []
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
                raise ValueError("limit must be an integer between 1 and 10000")
            limit_clause = "LIMIT ?"
            limit_parameters.append(limit)
        parameters = prediction_parameters + since_parameters + post_parameters + limit_parameters
        try:
            with self._connect(query_deadline=query_deadline) as conn:
                rows = conn.execute(
                    f"""
                WITH filtered_predictions AS (
                    SELECT p.*, r.prediction_as_of
                    FROM ledger_predictions p
                    JOIN prediction_requests r
                      ON r.ledger_request_id = p.ledger_request_id
                    WHERE {prediction_where}
                ),
                settlement_history AS (
                    SELECT source_settlement.settlement_id AS revision_id,
                           source_settlement.prediction_id,
                           source_settlement.revision,
                           'settle' AS action,
                           source_settlement.actual_time,
                           source_settlement.residual,
                           source_settlement.settled_at,
                           0 AS source_priority
                    FROM prediction_settlements source_settlement
                    JOIN filtered_predictions filtered
                      ON filtered.prediction_id = source_settlement.prediction_id
                    {legacy_since_clause}
                    UNION ALL
                    SELECT source_settlement.revision_id,
                           source_settlement.prediction_id,
                           source_settlement.revision,
                           source_settlement.action,
                           source_settlement.actual_time,
                           source_settlement.residual,
                           source_settlement.created_at AS settled_at,
                           1 AS source_priority
                    FROM numeric_settlement_revisions source_settlement
                    JOIN filtered_predictions filtered
                      ON filtered.prediction_id = source_settlement.prediction_id
                    {numeric_since_clause}
                ),
                ranked_settlements AS (
                    SELECT candidate.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY candidate.prediction_id
                               ORDER BY candidate.revision DESC,
                                        candidate.source_priority DESC,
                                        candidate.settled_at DESC,
                                        candidate.revision_id DESC
                           ) AS authority_rank
                    FROM settlement_history candidate
                ),
                latest_settlements AS (
                    SELECT * FROM ranked_settlements WHERE authority_rank = 1
                )
                SELECT
                    p.prediction_id, p.competitor_id, p.event_code,
                    p.median_seconds AS predicted_time, p.source,
                    p.engine_version, p.model_version, p.calibration_version,
                    p.evidence_cutoff, p.prediction_as_of,
                    p.interval_lower, p.interval_upper,
                    p.interval_coverage, p.interval_coverage AS nominal_coverage,
                    p.interval_state, p.interval_scope,
                    h.numeric_value AS history_count,
                    CASE WHEN h.numeric_value IS NULL THEN 'unavailable'
                         WHEN h.numeric_value < 1 THEN '0'
                         WHEN h.numeric_value < 4 THEN '1-3'
                         ELSE '4+' END AS history_band,
                    s.actual_time, s.residual, s.settled_at, s.revision
                FROM filtered_predictions p
                JOIN latest_settlements s ON s.prediction_id = p.prediction_id
                LEFT JOIN prediction_features h
                  ON h.prediction_id = p.prediction_id
                 AND h.feature_name = 'history_count'
                WHERE {post_where}
                ORDER BY s.settled_at, p.prediction_id
                {limit_clause}
                """,
                    parameters,
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if query_deadline is not None and query_deadline.cancelled:
                raise LedgerQueryTimeoutError(
                    "bounded training-row query exceeded its deadline"
                ) from exc
            raise
        result = [dict(row) for row in rows]
        for row in result:
            if row["history_count"] is not None:
                row["history_count"] = int(float(row["history_count"]))
        return result

    def count_training_rows(
        self,
        *,
        since: Optional[date | datetime | str] = None,
        model_version: Optional[str] = None,
        caller_id: Optional[str] = None,
        query_deadline: Optional[SQLiteQueryDeadline] = None,
    ) -> int:
        """Count current eligible evidence with one bounded SQL aggregate."""

        conditions = ["p.training_eligible = 1"]
        prediction_parameters: list[Any] = []
        if model_version is not None:
            conditions.append("p.model_version = ?")
            prediction_parameters.append(str(model_version))
        if caller_id is not None:
            conditions.append("r.caller_id = ?")
            prediction_parameters.append(_namespaced_identifier(caller_id, "caller_id"))
        where = " AND ".join(conditions)
        legacy_since_clause = ""
        numeric_since_clause = ""
        since_parameters: list[Any] = []
        if since is not None:
            since_value = since.isoformat() if hasattr(since, "isoformat") else str(since)
            legacy_since_clause = "WHERE source_settlement.settled_at >= ?"
            numeric_since_clause = "WHERE source_settlement.created_at >= ?"
            since_parameters = [since_value, since_value]
        try:
            with self._connect(query_deadline=query_deadline) as conn:
                return int(
                    conn.execute(
                        f"""
                    WITH filtered_predictions AS (
                        SELECT p.prediction_id
                        FROM ledger_predictions p
                        JOIN prediction_requests r
                          ON r.ledger_request_id = p.ledger_request_id
                        WHERE {where}
                    ),
                    settlement_history AS (
                        SELECT source_settlement.settlement_id AS revision_id,
                               source_settlement.prediction_id,
                               source_settlement.revision,
                               'settle' AS action,
                               source_settlement.settled_at,
                               0 AS source_priority
                        FROM prediction_settlements source_settlement
                        JOIN filtered_predictions filtered
                          ON filtered.prediction_id = source_settlement.prediction_id
                        {legacy_since_clause}
                        UNION ALL
                        SELECT source_settlement.revision_id,
                               source_settlement.prediction_id,
                               source_settlement.revision,
                               source_settlement.action,
                               source_settlement.created_at AS settled_at,
                               1 AS source_priority
                        FROM numeric_settlement_revisions source_settlement
                        JOIN filtered_predictions filtered
                          ON filtered.prediction_id = source_settlement.prediction_id
                        {numeric_since_clause}
                    ),
                    ranked_settlements AS (
                        SELECT candidate.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY candidate.prediction_id
                                   ORDER BY candidate.revision DESC,
                                            candidate.source_priority DESC,
                                            candidate.settled_at DESC,
                                            candidate.revision_id DESC
                               ) AS authority_rank
                        FROM settlement_history candidate
                    ),
                    latest_settlements AS (
                        SELECT * FROM ranked_settlements WHERE authority_rank = 1
                    )
                    SELECT COUNT(*)
                    FROM filtered_predictions p
                    JOIN latest_settlements s ON s.prediction_id = p.prediction_id
                    WHERE s.action = 'settle'
                    """,
                        prediction_parameters + since_parameters,
                    ).fetchone()[0]
                )
        except sqlite3.OperationalError as exc:
            if query_deadline is not None and query_deadline.cancelled:
                raise LedgerQueryTimeoutError(
                    "bounded training-count query exceeded its deadline"
                ) from exc
            raise

    @staticmethod
    def _canonical_json(value: Mapping[str, Any]) -> str:
        """Serialize an immutable receipt with one stable byte representation."""

        return json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

    @classmethod
    def _shadow_receipt_core(
        cls,
        metadata: Mapping[str, Any],
        *,
        request_hash: str,
        hash_algorithm: str,
        predictions: Sequence[Mapping[str, Any]],
        prediction_ids: Sequence[str],
        timestamp: str,
    ) -> dict[str, Any]:
        """Complete caller metadata with ledger-owned prediction identities."""

        if len(predictions) != len(prediction_ids):
            raise ValueError("receipt prediction identities are incomplete")
        core = dict(_json_value(metadata))
        core["ledger"] = {
            "request_hash": request_hash,
            "hash_algorithm": hash_algorithm,
        }
        core["created_at"] = timestamp
        core["predictions"] = [
            cls._shadow_prediction_projection(
                item,
                prediction_id=prediction_id,
                ordinal=ordinal,
            )
            for ordinal, (prediction_id, item) in enumerate(
                zip(prediction_ids, predictions, strict=True)
            )
        ]
        return core

    @staticmethod
    def _shadow_prediction_projection(
        item: Mapping[str, Any],
        *,
        prediction_id: str,
        ordinal: int,
    ) -> dict[str, Any]:
        """Project persisted prediction evidence into the immutable receipt shape."""

        return {
            "ordinal": ordinal,
            "prediction_id": prediction_id,
            "competitor_id": item["competitor_id"],
            "event_code": item["event_code"],
            "median_seconds": item["median_seconds"],
            "assigned_mark": item["assigned_mark"],
            "source": item["source"],
            "training_eligible": item["training_eligible"],
            "versions": {
                "engine": item["engine_version"],
                "model": item["model_version"],
                "calibration": item["calibration_version"],
            },
            "evidence_cutoff": item["evidence_cutoff"],
            "interval": {
                "lower": item["interval_lower"],
                "upper": item["interval_upper"],
                "nominal_coverage": item["interval_coverage"],
                "calibration_state": item["interval_state"],
                "scope": item["interval_scope"],
            },
            "optimizer": item["optimizer"],
            "optimizer_metadata": _json_value(item["optimizer_metadata"]),
            "warnings": list(item["warnings"]),
            "ignored_factors": list(item["ignored_factors"]),
        }

    @staticmethod
    def _validate_prediction(prediction: LedgerPrediction) -> dict[str, Any]:
        item = asdict(prediction)
        item["competitor_id"] = _identifier(item["competitor_id"], "stable competitor_id")
        item["event_code"] = _event(item["event_code"])
        item["median_seconds"] = _finite(item["median_seconds"], "median_seconds", positive=True)
        item["assigned_mark"] = int(item["assigned_mark"])
        if item["assigned_mark"] < 3:
            raise ValueError("assigned_mark must be at least 3")
        item["source"] = _identifier(item["source"], "source").lower()
        if not isinstance(item["training_eligible"], bool):
            raise ValueError("training_eligible must be a boolean")
        if not isinstance(item["degraded"], bool):
            raise ValueError("degraded must be a boolean")
        item["evidence_cutoff"] = (
            item["evidence_cutoff"].isoformat()
            if hasattr(item["evidence_cutoff"], "isoformat")
            else item["evidence_cutoff"]
        )
        for key in ("engine_version", "model_version", "calibration_version"):
            item[key] = str(item[key]).strip() if item[key] is not None else None
        for key in ("interval_state", "interval_scope"):
            item[key] = str(item[key]).strip() if item[key] is not None else None
        for key in ("interval_lower", "interval_upper", "interval_coverage"):
            try:
                item[key] = None if item[key] is None else _finite(item[key], key)
            except ValueError:
                # Optional persistence never fails a field for incomplete issued
                # provenance.  Unrepresentable values become unavailable and the
                # authoritative eligibility derivation below fails closed.
                item[key] = None

        clean_features: dict[str, float] = {}
        for name, value in item["feature_snapshot"].items():
            feature_name = str(name)
            if feature_name not in _NUMERIC_FEATURES:
                raise ValueError("feature snapshots accept numeric allowlisted model inputs only")
            clean_features[feature_name] = _finite(value, f"feature {feature_name}")
        item["feature_snapshot"] = dict(sorted(clean_features.items()))
        item["ignored_factors"] = tuple(str(value) for value in item["ignored_factors"])
        item["warnings"] = tuple(str(value) for value in item["warnings"])
        item["optimizer_metadata"] = _json_value(item["optimizer_metadata"])
        return item

    @staticmethod
    def _training_eligible(item: Mapping[str, Any], request_cutoff: date) -> bool:
        """Derive eligibility from complete immutable provenance, never caller intent alone."""

        if not item["training_eligible"] or item["degraded"]:
            return False
        if item["engine_version"] != ENGINE_VERSION:
            return False
        if not is_v2_training_source(item["source"]):
            return False
        if not item["model_version"] or not item["calibration_version"]:
            return False
        try:
            evidence_cutoff = date.fromisoformat(str(item["evidence_cutoff"]))
        except (TypeError, ValueError):
            return False
        if evidence_cutoff != request_cutoff:
            return False
        lower = item["interval_lower"]
        upper = item["interval_upper"]
        coverage = item["interval_coverage"]
        if lower is None or upper is None or coverage is None:
            return False
        if not (0 < lower <= item["median_seconds"] <= upper and lower < upper):
            return False
        if not 0 < coverage < 1:
            return False
        return bool(item["interval_state"] and item["interval_scope"])

    @staticmethod
    def _cloud_field_payload(
        request_row_id: str,
        caller_id: str,
        request_id: str,
        request_hash: str,
        hash_algorithm: str,
        event_code: str,
        prediction_as_of: str,
        prediction_ids: Sequence[str],
        predictions: Sequence[Mapping[str, Any]],
        timestamp: str,
    ) -> dict[str, Any]:
        prediction_rows = []
        feature_rows = []
        for prediction_id, item in zip(prediction_ids, predictions, strict=True):
            prediction_rows.append(
                {
                    "prediction_id": prediction_id,
                    "ledger_request_id": request_row_id,
                    "competitor_id": item["competitor_id"],
                    "event_code": item["event_code"],
                    "median_seconds": item["median_seconds"],
                    "assigned_mark": item["assigned_mark"],
                    "source": item["source"],
                    "training_eligible": item["training_eligible"],
                    "engine_version": item["engine_version"],
                    "model_version": item["model_version"],
                    "calibration_version": item["calibration_version"],
                    "evidence_cutoff": item["evidence_cutoff"],
                    "interval_lower": item["interval_lower"],
                    "interval_upper": item["interval_upper"],
                    "interval_coverage": item["interval_coverage"],
                    "interval_state": item["interval_state"],
                    "interval_scope": item["interval_scope"],
                    "ignored_factors": list(item["ignored_factors"]),
                    "warnings": list(item["warnings"]),
                    "optimizer": item["optimizer"],
                    "optimizer_metadata": item["optimizer_metadata"],
                    "created_at": timestamp,
                }
            )
            for name, value in item["feature_snapshot"].items():
                feature_rows.append(
                    {
                        "feature_snapshot_id": str(
                            uuid.uuid5(
                                _LEDGER_NAMESPACE,
                                f"feature:{prediction_id}:{name}",
                            )
                        ),
                        "prediction_id": prediction_id,
                        "feature_name": name,
                        "numeric_value": value,
                        "created_at": timestamp,
                    }
                )
        return {
            "request": {
                "ledger_request_id": request_row_id,
                "caller_id": caller_id,
                "request_id": request_id,
                "request_hash": request_hash,
                "hash_algorithm": hash_algorithm,
                "event_code": event_code,
                "prediction_as_of": prediction_as_of,
                "created_at": timestamp,
            },
            "predictions": prediction_rows,
            "features": feature_rows,
        }

    @staticmethod
    def _append_outbox(
        conn: sqlite3.Connection,
        *,
        kind: str,
        entity_id: str,
        payload: Mapping[str, Any],
        timestamp: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO prediction_mirror_outbox (
                outbox_id, kind, entity_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                kind,
                entity_id,
                json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":")),
                timestamp,
            ),
        )

    def _deliver_pending(self, kind: str, entity_id: str) -> str:
        with self._delivery_lock:
            return self._deliver_pending_locked(kind, entity_id)

    def _deliver_pending_locked(self, kind: str, entity_id: str) -> str:
        if self._mirror is None:
            return "not_configured"
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT o.outbox_id, o.payload_json, d.status
                    FROM prediction_mirror_outbox o
                    LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                    WHERE o.kind = ? AND o.entity_id = ?
                    """,
                    (kind, entity_id),
                ).fetchone()
        except sqlite3.Error:
            return "failed"
        if row is None:
            return "not_configured"
        if row["status"] == "recorded":
            return "recorded"
        try:
            payload = json.loads(row["payload_json"])
            if kind == "settlement":
                payload = _sanitize_settlement_mirror_payload(payload)
            self._mirror(payload)
        except Exception:
            status = "failed"
        else:
            status = "recorded"
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO prediction_mirror_delivery (
                        outbox_id, attempts, status, last_attempt_at
                    ) VALUES (?, 1, ?, ?)
                    ON CONFLICT(outbox_id) DO UPDATE SET
                        attempts = attempts + 1,
                        status = excluded.status,
                        last_attempt_at = excluded.last_attempt_at
                    """,
                    (row["outbox_id"], status, _now()),
                )
        except sqlite3.Error:
            # The authoritative ledger and outbox were already committed.  A
            # delivery-status failure must never relabel that durable write as
            # a local ledger failure; an identical retry can replay the outbox.
            return "failed"
        return status

    def _schedule_delivery(self, kind: str, entity_id: str) -> str:
        """Queue best-effort delivery on the ledger's sole daemon worker."""

        if self._mirror is None:
            return "not_configured"
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT d.status
                    FROM prediction_mirror_outbox o
                    LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                    WHERE o.kind = ? AND o.entity_id = ?
                    """,
                    (kind, entity_id),
                ).fetchone()
        except sqlite3.Error:
            return "pending"
        if row is not None and row["status"] == "recorded":
            return "recorded"
        key = (kind, entity_id)
        with self._worker_lock:
            if key not in self._delivery_in_flight and len(self._delivery_queue) < MAX_MIRROR_QUEUE:
                self._delivery_in_flight.add(key)
                self._delivery_queue.append(key)
            self._start_mirror_worker_locked()
        return "pending"

    def _start_mirror_worker_locked(self) -> None:
        if self._mirror_worker is not None and self._mirror_worker.is_alive():
            return
        worker = threading.Thread(
            target=self._mirror_worker_loop,
            name=f"strathmark-ledger-mirror-{id(self)}",
            daemon=True,
        )
        self._mirror_worker = worker
        worker.start()

    def _has_pending_delivery(self) -> bool:
        try:
            with self._connect() as conn:
                return (
                    conn.execute("""
                        SELECT 1
                        FROM prediction_mirror_outbox o
                        LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                        WHERE d.status IS NULL OR d.status != 'recorded'
                        LIMIT 1
                        """).fetchone()
                    is not None
                )
        except sqlite3.Error:
            return False

    def _claim_pending_delivery(self, attempted: set[tuple[str, str]]) -> Optional[tuple[str, str]]:
        """Claim one durable row not already attempted by this worker run."""

        try:
            with self._connect() as conn:
                rows = conn.execute("""
                    SELECT o.kind, o.entity_id
                    FROM prediction_mirror_outbox o
                    LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                    WHERE d.status IS NULL OR d.status != 'recorded'
                    ORDER BY o.created_at, o.outbox_id
                    """).fetchall()
        except sqlite3.Error:
            return None
        with self._worker_lock:
            for row in rows:
                key = (str(row["kind"]), str(row["entity_id"]))
                if key in attempted or key in self._delivery_in_flight:
                    continue
                self._delivery_in_flight.add(key)
                return key
        return None

    def _mirror_worker_loop(self) -> None:
        """Drain scheduled entities serially while durable outbox rows remain authority."""

        attempted: set[tuple[str, str]] = set()
        while True:
            with self._worker_lock:
                key = self._delivery_queue.popleft() if self._delivery_queue else None
            if key is None:
                key = self._claim_pending_delivery(attempted)
            if key is None:
                with self._worker_lock:
                    if self._delivery_queue:
                        continue
                    self._mirror_worker = None
                    return
            attempted.add(key)
            try:
                self._deliver_pending(*key)
            finally:
                with self._worker_lock:
                    self._delivery_in_flight.discard(key)

    def flush_mirror_outbox(
        self, *, limit: int = 100, caller_id: Optional[str] = None
    ) -> dict[str, int]:
        """Synchronously retry a bounded number of pending mirror payloads off-path."""

        bounded = max(1, min(int(limit), 1000))
        scoped_caller = (
            None if caller_id is None else _namespaced_identifier(caller_id, "caller_id")
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT o.kind, o.entity_id
                FROM prediction_mirror_outbox o
                LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                WHERE (d.status IS NULL OR d.status != 'recorded')
                  AND (
                    ? IS NULL
                    OR (
                      o.kind = 'field' AND EXISTS (
                        SELECT 1 FROM prediction_requests q
                        WHERE q.ledger_request_id = o.entity_id AND q.caller_id = ?
                      )
                    )
                    OR (
                      o.kind = 'settlement' AND EXISTS (
                        SELECT 1 FROM numeric_outcome_revisions r
                        WHERE r.field_revision_id = o.entity_id AND r.caller_id = ?
                      )
                    )
                  )
                ORDER BY o.created_at, o.outbox_id
                LIMIT ?
                """,
                (scoped_caller, scoped_caller, scoped_caller, bounded),
            ).fetchall()
        summary = {"recorded": 0, "failed": 0, "not_configured": 0}
        for row in rows:
            status = self._deliver_pending(str(row["kind"]), str(row["entity_id"]))
            summary[status] = summary.get(status, 0) + 1
        return summary

    @staticmethod
    def _validate_numeric_revision(
        value: NumericSettlementRevision | Mapping[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "prediction_id",
            "competitor_id",
            "event_code",
            "action",
            "actual_time",
            "expected_revision",
        }
        if isinstance(value, NumericSettlementRevision):
            raw = asdict(value)
        elif isinstance(value, Mapping):
            raw = dict(value)
            unknown = sorted(str(key) for key in raw if key not in allowed)
            if unknown:
                raise ValueError(f"unknown properties: {', '.join(unknown)}")
        else:
            raise ValueError("each revision must be a NumericSettlementRevision or object")

        prediction_id = _identifier(raw.get("prediction_id"), "prediction_id")
        competitor_id = _namespaced_identifier(raw.get("competitor_id"), "competitor_id")
        event_code = _event(raw.get("event_code"))
        action = str(raw.get("action") or "").strip().lower()
        if action not in {"settle", "void"}:
            raise ValueError("action must be 'settle' or 'void'")
        expected_revision = raw.get("expected_revision")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        if action == "settle":
            actual_time: Optional[float] = _finite(
                raw.get("actual_time"), "actual_time", positive=True
            )
            if actual_time > MAX_NUMERIC_RAW_TIME_SECONDS:
                raise ValueError(
                    f"actual_time must be at most {MAX_NUMERIC_RAW_TIME_SECONDS:g} seconds"
                )
        else:
            if raw.get("actual_time") is not None:
                raise ValueError("actual_time must be omitted or null for a void")
            actual_time = None
        return {
            "prediction_id": prediction_id,
            "competitor_id": competitor_id,
            "event_code": event_code,
            "action": action,
            "actual_time": actual_time,
            "expected_revision": expected_revision,
        }

    @staticmethod
    def _numeric_outcome_result(
        conn: sqlite3.Connection,
        outcome_row: sqlite3.Row,
        *,
        status: str = "recorded",
        cloud_status: str = "not_configured",
    ) -> NumericOutcomeRevisionResult:
        rows = conn.execute(
            """
            SELECT * FROM numeric_settlement_revisions
            WHERE field_revision_id = ?
            ORDER BY prediction_id
            """,
            (outcome_row["field_revision_id"],),
        ).fetchall()
        return NumericOutcomeRevisionResult(
            outcome_revision_id=str(outcome_row["outcome_revision_id"]),
            ledger_request_id=str(outcome_row["ledger_request_id"]),
            caller_id=str(outcome_row["caller_id"]),
            revisions=tuple(
                NumericSettlementRevisionResult(
                    revision_id=str(row["revision_id"]),
                    prediction_id=str(row["prediction_id"]),
                    revision=int(row["revision"]),
                    competitor_id=str(row["competitor_id"]),
                    event_code=str(row["event_code"]),
                    action=str(row["action"]),
                    actual_time=(None if row["actual_time"] is None else float(row["actual_time"])),
                    residual=None if row["residual"] is None else float(row["residual"]),
                    supersedes_revision_id=row["supersedes_revision_id"],
                    created_at=str(row["created_at"]),
                )
                for row in rows
            ),
            actor=str(outcome_row["actor"]),
            reason_code=outcome_row["reason_code"],
            created_at=str(outcome_row["created_at"]),
            status=status,
            cloud_status=cloud_status,
        )

    @staticmethod
    def _settlement_from_row(row: sqlite3.Row, *, status: str = "recorded") -> SettlementResult:
        return SettlementResult(
            settlement_id=str(row["settlement_id"]),
            prediction_id=str(row["prediction_id"]),
            revision=int(row["revision"]),
            actual_time=float(row["actual_time"]),
            residual=float(row["residual"]),
            actor=_legacy_egress_actor(row["actor"]),
            reason=_legacy_egress_reason(row["reason"]),
            supersedes_settlement_id=row["supersedes_settlement_id"],
            settled_at=str(row["settled_at"]),
            status=status,
        )
