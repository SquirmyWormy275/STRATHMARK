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
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

_ENV_VAR = "STRATHMARK_DB_PATH"
_DEFAULT_PATH = Path.home() / ".strathmark" / "results.db"

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


@dataclass(frozen=True)
class LedgerWriteResult:
    recorded: bool
    status: str
    prediction_ids: tuple[str, ...]
    request_hash: str
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
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.executescript(_IMMUTABILITY_TRIGGERS)

    def record_field(
        self,
        caller_id: str,
        request_id: str,
        request_payload: Mapping[str, Any],
        predictions: Sequence[LedgerPrediction],
    ) -> LedgerWriteResult:
        """Atomically append one complete field or return its original IDs."""

        caller = _identifier(caller_id, "caller_id")
        idempotency_key = _identifier(request_id, "request_id")
        if not predictions:
            raise ValueError("predictions must not be empty")
        digest = canonical_hash(request_payload)
        validated = [self._validate_prediction(item) for item in predictions]
        if len({item["competitor_id"] for item in validated}) != len(validated):
            raise ValueError("competitor_id values must be unique within a field")

        event_code = _event(request_payload.get("event_code"))
        prediction_as_of = str(request_payload.get("prediction_as_of") or "").strip()
        try:
            date.fromisoformat(prediction_as_of)
        except ValueError as exc:
            raise ValueError("prediction_as_of must be an ISO date") from exc

        request_row_id = str(uuid.uuid4())
        timestamp = _now()
        prediction_ids: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    """
                    SELECT ledger_request_id, request_hash
                    FROM prediction_requests
                    WHERE caller_id = ? AND request_id = ?
                    """,
                    (caller, idempotency_key),
                ).fetchone()
                if existing is not None:
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
                    conn.commit()
                    return LedgerWriteResult(
                        recorded=True,
                        status="duplicate",
                        prediction_ids=tuple(row["prediction_id"] for row in rows),
                        request_hash=digest,
                    )

                conn.execute(
                    """
                    INSERT INTO prediction_requests (
                        ledger_request_id, caller_id, request_id, request_hash,
                        event_code, prediction_as_of, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_row_id,
                        caller,
                        idempotency_key,
                        digest,
                        event_code,
                        prediction_as_of,
                        timestamp,
                    ),
                )
                for ordinal, item in enumerate(validated):
                    prediction_id = str(uuid.uuid4())
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
                            int(item["source"] != "manual"),
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
                                str(uuid.uuid4()),
                                prediction_id,
                                feature_name,
                                numeric_value,
                                timestamp,
                            ),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        cloud_payload = self._cloud_field_payload(
            request_row_id,
            digest,
            event_code,
            prediction_as_of,
            prediction_ids,
            validated,
            timestamp,
        )
        cloud_status = self._try_mirror(cloud_payload)
        status = "recorded_cloud_failed" if cloud_status == "failed" else "recorded"
        return LedgerWriteResult(
            recorded=True,
            status=status,
            prediction_ids=tuple(prediction_ids),
            request_hash=digest,
            cloud_status=cloud_status,
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
                    return self._settlement_from_row(duplicate, status="duplicate")

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
                settlement_id = str(uuid.uuid4())
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
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        cloud_status = self._try_mirror(
            {
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
        )
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
        event_code: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return current settled model predictions; manual rows never qualify."""

        conditions = ["p.training_eligible = 1"]
        parameters: list[Any] = []
        if since is not None:
            since_value = since.isoformat() if hasattr(since, "isoformat") else str(since)
            conditions.append("s.settled_at >= ?")
            parameters.append(since_value)
        if model_version is not None:
            conditions.append("p.model_version = ?")
            parameters.append(str(model_version))
        if event_code is not None:
            conditions.append("p.event_code = ?")
            parameters.append(_event(event_code))
        where = " AND ".join(conditions)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    p.prediction_id, p.competitor_id, p.event_code,
                    p.median_seconds AS predicted_time, p.source,
                    p.engine_version, p.model_version, p.calibration_version,
                    p.evidence_cutoff, s.actual_time, s.residual, s.settled_at,
                    s.revision
                FROM ledger_predictions p
                JOIN prediction_settlements s ON s.prediction_id = p.prediction_id
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
        return [dict(row) for row in rows]

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
        item["evidence_cutoff"] = (
            item["evidence_cutoff"].isoformat()
            if hasattr(item["evidence_cutoff"], "isoformat")
            else item["evidence_cutoff"]
        )
        for key in ("interval_lower", "interval_upper", "interval_coverage"):
            if item[key] is not None:
                item[key] = _finite(item[key], key, positive=True)
        if item["interval_lower"] is not None and item["interval_upper"] is not None:
            if item["interval_lower"] > item["interval_upper"]:
                raise ValueError("interval lower bound must not exceed upper bound")

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
    def _cloud_field_payload(
        request_row_id: str,
        request_hash: str,
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
                    "training_eligible": item["source"] != "manual",
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
                        "feature_snapshot_id": str(uuid.uuid4()),
                        "prediction_id": prediction_id,
                        "feature_name": name,
                        "numeric_value": value,
                        "created_at": timestamp,
                    }
                )
        return {
            "request": {
                "ledger_request_id": request_row_id,
                "request_hash": request_hash,
                "event_code": event_code,
                "prediction_as_of": prediction_as_of,
                "created_at": timestamp,
            },
            "predictions": prediction_rows,
            "features": feature_rows,
        }

    def _try_mirror(self, payload: Mapping[str, Any]) -> str:
        if self._mirror is None:
            return "not_configured"
        try:
            self._mirror(payload)
        except Exception:
            return "failed"
        return "recorded"

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
