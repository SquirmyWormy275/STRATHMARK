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
import sqlite3
import threading
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

CREATE INDEX IF NOT EXISTS idx_ledger_predictions_competitor
    ON ledger_predictions(competitor_id, event_code);
CREATE INDEX IF NOT EXISTS idx_prediction_settlements_prediction
    ON prediction_settlements(prediction_id, revision DESC);
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
"""


class LedgerConflictError(ValueError):
    """An idempotency key was reused for a different canonical request."""


class SettlementConflictError(ValueError):
    """A settlement does not match its prediction or lacks correction data."""


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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

    def get_shadow_receipt(
        self,
        caller_id: str,
        request_id: str,
        *,
        current_active_fingerprint: Optional[str] = None,
    ) -> Optional[Any]:
        """Return the exact immutable core plus a freshly derived status view."""

        caller = _identifier(caller_id, "caller_id")
        idempotency_key = _identifier(request_id, "request_id")
        with self._connect() as conn:
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
        if not isinstance(entrants, list) or {
            item.get("competitor_id") for item in entrants if isinstance(item, Mapping)
        } != {child["competitor_id"] for child in child_rows}:
            raise ShadowReceiptCorruptionError(
                "persisted shadow receipt entrant set is inconsistent"
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
        actor_value = _identifier(actor, "actor")
        reason_value = str(reason or "").strip() or None
        if reason_value is not None and len(reason_value) > 500:
            raise ValueError("reason must be at most 500 characters")

        payload_digest = canonical_hash(
            {
                "prediction_id": prediction_key,
                "competitor_id": competitor_key,
                "event_code": event,
                "actual_time": actual,
                "actor": actor_value,
                "reason": reason_value,
            }
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

                duplicate = conn.execute(
                    """
                    SELECT * FROM prediction_settlements
                    WHERE prediction_id = ? AND payload_hash = ?
                    """,
                    (prediction_key, payload_digest),
                ).fetchone()
                if duplicate is not None:
                    conn.commit()
                    result = self._settlement_from_row(duplicate, status="duplicate")
                    cloud_status = self._schedule_delivery(
                        "settlement", str(duplicate["settlement_id"])
                    )
                    return replace(result, cloud_status=cloud_status)

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
    ) -> list[dict[str, Any]]:
        """Return current settled, explicitly eligible model predictions."""

        conditions = ["p.training_eligible = 1"]
        parameters: list[Any] = []
        if since is not None:
            since_value = since.isoformat() if hasattr(since, "isoformat") else str(since)
            conditions.append("s.settled_at >= ?")
            parameters.append(since_value)
        if model_version is not None:
            conditions.append("p.model_version = ?")
            parameters.append(str(model_version))
        if calibration_version is not None:
            conditions.append("p.calibration_version = ?")
            parameters.append(str(calibration_version))
        if event_code is not None:
            conditions.append("p.event_code = ?")
            parameters.append(_event(event_code))
        if nominal_coverage is not None:
            conditions.append("p.interval_coverage = ?")
            parameters.append(_finite(nominal_coverage, "nominal_coverage", positive=True))
        if interval_state is not None:
            conditions.append("p.interval_state = ?")
            parameters.append(_identifier(interval_state, "interval_state"))
        if interval_scope is not None:
            conditions.append("p.interval_scope = ?")
            parameters.append(_identifier(interval_scope, "interval_scope"))
        if history_band is not None:
            band = str(history_band).strip()
            if band not in {"0", "1-3", "4+", "unavailable"}:
                raise ValueError("history_band must be '0', '1-3', '4+', or 'unavailable'")
            conditions.append(
                "CASE WHEN h.numeric_value IS NULL THEN 'unavailable' "
                "WHEN h.numeric_value < 1 THEN '0' "
                "WHEN h.numeric_value < 4 THEN '1-3' ELSE '4+' END = ?"
            )
            parameters.append(band)
        where = " AND ".join(conditions)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    p.prediction_id, p.competitor_id, p.event_code,
                    p.median_seconds AS predicted_time, p.source,
                    p.engine_version, p.model_version, p.calibration_version,
                    p.evidence_cutoff, r.prediction_as_of,
                    p.interval_lower, p.interval_upper,
                    p.interval_coverage, p.interval_coverage AS nominal_coverage,
                    p.interval_state, p.interval_scope,
                    h.numeric_value AS history_count,
                    CASE WHEN h.numeric_value IS NULL THEN 'unavailable'
                         WHEN h.numeric_value < 1 THEN '0'
                         WHEN h.numeric_value < 4 THEN '1-3'
                         ELSE '4+' END AS history_band,
                    s.actual_time, s.residual, s.settled_at, s.revision
                FROM ledger_predictions p
                JOIN prediction_requests r ON r.ledger_request_id = p.ledger_request_id
                JOIN prediction_settlements s ON s.prediction_id = p.prediction_id
                LEFT JOIN prediction_features h
                  ON h.prediction_id = p.prediction_id
                 AND h.feature_name = 'history_count'
                WHERE {where}
                  AND s.revision = (
                    SELECT MAX(current.revision)
                    FROM prediction_settlements current
                    WHERE current.prediction_id = p.prediction_id
                  )
                ORDER BY s.settled_at, p.prediction_id
                """,
                parameters,
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            if row["history_count"] is not None:
                row["history_count"] = int(float(row["history_count"]))
        return result

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
            self._mirror(json.loads(row["payload_json"]))
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

    def flush_mirror_outbox(self, *, limit: int = 100) -> dict[str, int]:
        """Synchronously retry a bounded number of pending mirror payloads off-path."""

        bounded = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT o.kind, o.entity_id
                FROM prediction_mirror_outbox o
                LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
                WHERE d.status IS NULL OR d.status != 'recorded'
                ORDER BY o.created_at, o.outbox_id
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        summary = {"recorded": 0, "failed": 0, "not_configured": 0}
        for row in rows:
            status = self._deliver_pending(str(row["kind"]), str(row["entity_id"]))
            summary[status] = summary.get(status, 0) + 1
        return summary

    @staticmethod
    def _settlement_from_row(row: sqlite3.Row, *, status: str = "recorded") -> SettlementResult:
        return SettlementResult(
            settlement_id=str(row["settlement_id"]),
            prediction_id=str(row["prediction_id"]),
            revision=int(row["revision"]),
            actual_time=float(row["actual_time"]),
            residual=float(row["residual"]),
            actor=str(row["actor"]),
            reason=row["reason"],
            supersedes_settlement_id=row["supersedes_settlement_id"],
            settled_at=str(row["settled_at"]),
            status=status,
        )
