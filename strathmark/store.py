"""
Result Store
=============

SQLite-backed persistence layer for tournament results.

Results accumulate across competitions so predictions grow more accurate
over time. Multiple projects (STRATHEX, future tournament software) can
share the same store by pointing at the same database path.

Default path: ~/.strathmark/results.db
Override:     Set the STRATHMARK_DB_PATH environment variable.

Public API:
    ResultStore(db_path=None)                       -- open/create the store
    .record_result(...)                             -- append one result
    .import_from_dataframe(df, skip_duplicates=True)-- bulk import from DataFrame
    .get_competitor_history(name, event_code=None)  -- List[HistoricalResult]
    .get_all_as_dataframe()                         -- full table as DataFrame
    .get_competitors()                              -- List[str] of all names
    .count()                                        -- total row count

Schema (table: results):
    id              INTEGER PRIMARY KEY AUTOINCREMENT
    competitor_name TEXT NOT NULL
    event_code      TEXT NOT NULL     ('SB' or 'UH')
    time_seconds    REAL NOT NULL
    species         TEXT NOT NULL
    diameter_mm     REAL NOT NULL
    quality         INTEGER NOT NULL
    competition_id  TEXT NOT NULL              (stable show/source identity)
    heat_id         TEXT NOT NULL DEFAULT ''  (empty string, never NULL)
    result_date     TEXT              (ISO 8601 date, e.g. '2025-06-14', nullable)
    recorded_at     TEXT NOT NULL     (ISO 8601 datetime of when row was inserted)

Unique constraint: (competitor_name, competition_id, heat_id, event_code,
time_seconds). A competition identity keeps the same heat label in two shows
from being treated as a duplicate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional, Protocol, Sequence

import pandas as pd

from strathmark.config import data_req, events, is_valid_event, rules
from strathmark.predictor import HistoricalResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENV_VAR = "STRATHMARK_DB_PATH"
_DEFAULT_DB_DIR = Path.home() / ".strathmark"
_DEFAULT_DB_NAME = "results.db"

EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION = "strathmark.evidence-snapshot-source.v1"
EVIDENCE_SNAPSHOT_SCHEMA_VERSION = "strathmark.evidence-snapshot.v1"
EVIDENCE_HISTORY_ROW_SCHEMA_VERSION = "strathmark.evidence-history-row.v1"
EVIDENCE_ACTIVATION_SCHEMA_VERSION = "strathmark.evidence-snapshot-activation.v1"
DEFAULT_MAX_SNAPSHOT_AGE_DAYS = 7
MAX_CAPTURE_CLOCK_SKEW_SECONDS = 300
MAX_EVIDENCE_SNAPSHOT_ROWS = 100_000
MAX_EVIDENCE_SOURCE_STRING_LENGTH = 512
MAX_EVIDENCE_SOURCE_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_SOURCE_NESTING = 4
MAX_EVIDENCE_SOURCE_NODES = 1_000_000

_NAMESPACED_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$")
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_ROW_FIELDS = frozenset(
    {
        "schema_version",
        "competitor_id",
        "event_code",
        "time_seconds",
        "species",
        "diameter_mm",
        "quality",
        "competition_id",
        "heat_id",
        "result_date",
    }
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_name TEXT NOT NULL,
    event_code      TEXT NOT NULL,
    time_seconds    REAL NOT NULL,
    species         TEXT NOT NULL,
    diameter_mm     REAL NOT NULL,
    quality         INTEGER NOT NULL,
    competition_id  TEXT NOT NULL,
    heat_id         TEXT NOT NULL DEFAULT '',
    result_date     TEXT,
    recorded_at     TEXT NOT NULL,
    UNIQUE(competitor_name, competition_id, heat_id, event_code, time_seconds)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_results_competitor
    ON results(competitor_name, event_code);
"""

_CREATE_EVIDENCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evidence_snapshots (
    snapshot_digest      TEXT PRIMARY KEY,
    schema_version       TEXT NOT NULL,
    source_schema_version TEXT NOT NULL,
    source_id            TEXT NOT NULL,
    source_digest        TEXT NOT NULL,
    cutoff               TEXT NOT NULL,
    captured_at          TEXT NOT NULL,
    completeness         TEXT NOT NULL,
    supplied_row_count   INTEGER NOT NULL,
    accepted_row_count   INTEGER NOT NULL,
    rejected_row_count   INTEGER NOT NULL,
    diagnostics_json     TEXT NOT NULL,
    canonical_json       TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    CHECK (completeness IN ('full', 'partial', 'empty')),
    CHECK (supplied_row_count >= 0),
    CHECK (accepted_row_count >= 0),
    CHECK (rejected_row_count >= 0)
);

CREATE TABLE IF NOT EXISTS evidence_snapshot_rows (
    snapshot_digest TEXT NOT NULL REFERENCES evidence_snapshots(snapshot_digest),
    ordinal         INTEGER NOT NULL,
    row_digest      TEXT NOT NULL,
    competitor_id   TEXT NOT NULL,
    event_code      TEXT NOT NULL,
    time_seconds    REAL NOT NULL,
    species         TEXT NOT NULL,
    diameter_mm     REAL NOT NULL,
    quality         INTEGER NOT NULL,
    competition_id  TEXT NOT NULL,
    heat_id         TEXT NOT NULL,
    result_date     TEXT NOT NULL,
    PRIMARY KEY (snapshot_digest, ordinal),
    UNIQUE (snapshot_digest, row_digest)
);

CREATE INDEX IF NOT EXISTS idx_evidence_snapshot_competitor
    ON evidence_snapshot_rows(snapshot_digest, competitor_id, event_code, result_date);

CREATE TABLE IF NOT EXISTS evidence_snapshot_activations (
    activation_id             TEXT PRIMARY KEY,
    schema_version            TEXT NOT NULL,
    revision                  INTEGER NOT NULL UNIQUE,
    snapshot_digest           TEXT NOT NULL REFERENCES evidence_snapshots(snapshot_digest),
    previous_activation_id    TEXT REFERENCES evidence_snapshot_activations(activation_id),
    previous_snapshot_digest  TEXT REFERENCES evidence_snapshots(snapshot_digest),
    activated_at              TEXT NOT NULL,
    canonical_json            TEXT NOT NULL,
    CHECK (revision > 0)
);

CREATE INDEX IF NOT EXISTS idx_evidence_snapshot_activations_revision
    ON evidence_snapshot_activations(revision);

CREATE TRIGGER IF NOT EXISTS evidence_snapshots_no_update
BEFORE UPDATE ON evidence_snapshots BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS evidence_snapshots_no_delete
BEFORE DELETE ON evidence_snapshots BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS evidence_snapshot_rows_no_update
BEFORE UPDATE ON evidence_snapshot_rows BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS evidence_snapshot_rows_no_delete
BEFORE DELETE ON evidence_snapshot_rows BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS evidence_snapshot_activations_no_update
BEFORE UPDATE ON evidence_snapshot_activations
BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
CREATE TRIGGER IF NOT EXISTS evidence_snapshot_activations_no_delete
BEFORE DELETE ON evidence_snapshot_activations
BEGIN SELECT RAISE(ABORT, 'append-only table'); END;
"""


class EvidenceSnapshotIntegrityError(RuntimeError):
    """A source or persisted evidence snapshot failed digest verification."""


class EvidenceSnapshotConflictError(RuntimeError):
    """A refresh lost the active-snapshot compare-and-swap race."""


@dataclass(frozen=True)
class EvidenceSnapshotPayload:
    """Versioned local source-adapter result used by an explicit operator refresh."""

    schema_version: str
    source_id: str
    cutoff: date
    captured_at: datetime
    rows: Sequence[Mapping[str, Any]]
    source_digest: str


class EvidenceSnapshotSource(Protocol):
    """Side-effect boundary invoked only by ``refresh_evidence_snapshot``."""

    def load_snapshot(self, *, cutoff: date) -> EvidenceSnapshotPayload: ...


@dataclass(frozen=True)
class EvidenceSnapshotStatus:
    """Verified active snapshot metadata plus an offline-only freshness view."""

    schema_version: str
    snapshot_digest: str
    source_schema_version: str
    source_id: str
    source_digest: str
    cutoff: date
    captured_at: datetime
    activated_at: datetime
    activation_id: str
    activation_revision: int
    previous_activation_id: Optional[str]
    supersedes_snapshot_digest: Optional[str]
    completeness: str
    supplied_row_count: int
    accepted_row_count: int
    rejected_row_count: int
    diagnostics: Mapping[str, int]
    age_days: int
    freshness: str
    integrity: str
    ready_for_offline: bool

    def input_projection(self) -> dict[str, Any]:
        """Stable subset that belongs in the active calculation fingerprint."""

        return {
            "schema_version": self.schema_version,
            "snapshot_digest": self.snapshot_digest,
            "source_schema_version": self.source_schema_version,
            "source_id": self.source_id,
            "source_digest": self.source_digest,
            "cutoff": self.cutoff.isoformat(),
            "cutoff_semantics": "exclusive-utc-date",
            "captured_at": _utc_iso(self.captured_at),
            "activation_id": self.activation_id,
            "activation_revision": self.activation_revision,
            "previous_activation_id": self.previous_activation_id,
            "supersedes_snapshot_digest": self.supersedes_snapshot_digest,
            "completeness": self.completeness,
            "supplied_row_count": self.supplied_row_count,
            "accepted_row_count": self.accepted_row_count,
            "rejected_row_count": self.rejected_row_count,
            "diagnostics": dict(sorted(self.diagnostics.items())),
        }

    def receipt_projection(self) -> dict[str, Any]:
        """Immutable at receipt creation while explicitly exposing readiness facts."""

        return {
            **self.input_projection(),
            "activated_at": _utc_iso(self.activated_at),
            "age_days_at_calculation": self.age_days,
            "freshness_at_calculation": self.freshness,
            "integrity": self.integrity,
            "ready_for_offline_at_calculation": self.ready_for_offline,
        }


@dataclass(frozen=True)
class EvidenceSnapshotSelection:
    """One verified active snapshot and field-wide histories loaded from its digest."""

    status: EvidenceSnapshotStatus
    histories: Mapping[str, tuple[HistoricalResult, ...]]


class _RejectedEvidenceRow(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _date_value(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{label} must be a date without a time")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _namespaced_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > 128 or not _NAMESPACED_ID.fullmatch(text):
        raise ValueError(f"{label} must be a bounded namespaced identifier")
    return text


def _raw_json_value(
    value: Any,
    *,
    depth: int = 0,
    _seen: Optional[set[int]] = None,
    _nodes: Optional[list[int]] = None,
) -> Any:
    """Project adapter material into strict deterministic JSON for verification."""

    seen = set() if _seen is None else _seen
    nodes = [0] if _nodes is None else _nodes
    nodes[0] += 1
    if nodes[0] > MAX_EVIDENCE_SOURCE_NODES:
        raise ValueError("snapshot source exceeds the maximum nodes")
    if depth > MAX_EVIDENCE_SOURCE_NESTING:
        raise ValueError("snapshot source exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_EVIDENCE_SOURCE_STRING_LENGTH:
            raise ValueError("snapshot source contains an oversized string")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("snapshot source contains a non-finite number")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("snapshot source contains a naive datetime")
        return _utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError("snapshot source contains a cycle")
        seen.add(identity)
        try:
            projected = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("snapshot source mappings require string keys")
                projected[key] = _raw_json_value(
                    item,
                    depth=depth + 1,
                    _seen=seen,
                    _nodes=nodes,
                )
            return projected
        finally:
            seen.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        identity = id(value)
        if identity in seen:
            raise ValueError("snapshot source contains a cycle")
        seen.add(identity)
        try:
            return [
                _raw_json_value(
                    item,
                    depth=depth + 1,
                    _seen=seen,
                    _nodes=nodes,
                )
                for item in value
            ]
        finally:
            seen.remove(identity)
    raise ValueError(f"snapshot source contains unsupported value type {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _raw_json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except RecursionError as exc:
        raise ValueError("snapshot source exceeds safe recursion bounds") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _freeze_evidence_source_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
    """Traverse an adapter sequence once into a bounded immutable JSON projection."""

    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
        raise ValueError("rows must be a bounded sequence of objects")
    frozen: list[Any] = []
    nodes = [0]
    try:
        for ordinal, row in enumerate(rows):
            if ordinal >= MAX_EVIDENCE_SNAPSHOT_ROWS:
                raise ValueError(f"snapshot rows must not exceed {MAX_EVIDENCE_SNAPSHOT_ROWS}")
            frozen.append(_raw_json_value(row, depth=2, _nodes=nodes))
    except RecursionError as exc:
        raise ValueError("snapshot source exceeds safe recursion bounds") from exc
    return tuple(frozen)


def _canonical_evidence_source_digest_from_frozen(
    *,
    source_id: str,
    cutoff: date,
    captured_at: datetime,
    frozen_rows: tuple[Any, ...],
) -> str:
    material = {
        "schema_version": EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        "source_id": source_id,
        "cutoff": cutoff.isoformat(),
        "cutoff_semantics": "exclusive-utc-date",
        "captured_at": _utc_iso(captured_at),
        "rows": frozen_rows,
    }
    serialized = _canonical_json(material)
    if len(serialized.encode("utf-8")) > MAX_EVIDENCE_SOURCE_BYTES:
        raise ValueError(f"snapshot source must not exceed {MAX_EVIDENCE_SOURCE_BYTES} bytes")
    return _sha256(serialized)


def canonical_evidence_source_digest(
    *,
    source_id: str,
    cutoff: date,
    captured_at: datetime,
    rows: Sequence[Mapping[str, Any]],
) -> str:
    """Digest the exact versioned source envelope before row-level filtering."""

    source = _namespaced_id(source_id, "source_id")
    cutoff_date = _date_value(cutoff, "cutoff")
    captured = _aware_utc_datetime(captured_at, "captured_at")
    frozen_rows = _freeze_evidence_source_rows(rows)
    return _canonical_evidence_source_digest_from_frozen(
        source_id=source,
        cutoff=cutoff_date,
        captured_at=captured,
        frozen_rows=frozen_rows,
    )


# ---------------------------------------------------------------------------
# ResultStore
# ---------------------------------------------------------------------------


class ResultStore:
    """
    Persistent store for woodchopping tournament results.

    Thread-safety: uses ``check_same_thread=False`` so a single instance can be
    shared across threads, but each operation acquires/releases the connection
    internally via context managers.

    Args:
        db_path: Explicit path to the SQLite file. If None, reads the
                 STRATHMARK_DB_PATH environment variable; falls back to
                 ~/.strathmark/results.db.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is not None:
            self._path = Path(db_path)
        elif _ENV_VAR in os.environ:
            self._path = Path(os.environ[_ENV_VAR])
        else:
            self._path = _DEFAULT_DB_DIR / _DEFAULT_DB_NAME

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @property
    def path(self) -> Path:
        """Resolved local database path (useful for additive local stores)."""

        return self._path

    def prediction_ledger(self, mirror: Optional[Any] = None):
        """Return a trusted ledger sharing this store's isolated SQLite file."""

        from strathmark.ledger import PredictionLedger

        return PredictionLedger(self._path, mirror=mirror)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(results)").fetchall()}
            if columns and "competition_id" not in columns:
                self._migrate_results_schema(conn)
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.executescript(_CREATE_EVIDENCE_SCHEMA_SQL)
            conn.commit()

    @staticmethod
    def _migrate_results_schema(conn: sqlite3.Connection) -> None:
        """Rebuild the legacy table with competition-aware deduplication."""
        conn.execute("DROP INDEX IF EXISTS idx_results_competitor")
        conn.execute("ALTER TABLE results RENAME TO results_legacy")
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(
            """
            INSERT INTO results (
                id, competitor_name, event_code, time_seconds, species,
                diameter_mm, quality, competition_id, heat_id, result_date, recorded_at
            )
            SELECT
                id, competitor_name, event_code, time_seconds, species,
                diameter_mm, quality,
                CASE
                    WHEN result_date IS NOT NULL AND TRIM(result_date) != ''
                    THEN 'legacy:' || result_date
                    ELSE 'legacy:unknown'
                END,
                heat_id, result_date, recorded_at
            FROM results_legacy
            """
        )
        conn.execute("DROP TABLE results_legacy")

    # ------------------------------------------------------------------
    # Deliberate offline evidence snapshot refresh/read boundary
    # ------------------------------------------------------------------

    def refresh_evidence_snapshot(
        self,
        source: EvidenceSnapshotSource,
        *,
        cutoff: date,
        expected_active_snapshot_digest: Optional[str] = None,
    ) -> EvidenceSnapshotStatus:
        """Verify, persist, and atomically activate one local evidence snapshot.

        The source adapter is invoked only here.  Prediction and freshness reads
        never retain or call it, which makes race-day operation cloud-independent.
        Invalid individual rows are counted and excluded.  Envelope, digest,
        adapter, or transaction failures leave the previous active pointer intact.
        """

        requested_cutoff = _date_value(cutoff, "cutoff")
        payload = source.load_snapshot(cutoff=requested_cutoff)
        if not isinstance(payload, EvidenceSnapshotPayload):
            raise ValueError("snapshot source must return EvidenceSnapshotPayload")
        if payload.schema_version != EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION:
            raise ValueError("unsupported evidence snapshot source schema_version")
        source_id = _namespaced_id(payload.source_id, "source_id")
        payload_cutoff = _date_value(payload.cutoff, "payload cutoff")
        if payload_cutoff != requested_cutoff:
            raise ValueError("snapshot source cutoff does not match the requested cutoff")
        captured_at = _aware_utc_datetime(payload.captured_at, "captured_at")
        observed_at = _utc_now()
        if captured_at > observed_at + timedelta(seconds=MAX_CAPTURE_CLOCK_SKEW_SECONDS):
            raise ValueError(
                f"captured_at is too far in the future; maximum UTC skew is "
                f"{MAX_CAPTURE_CLOCK_SKEW_SECONDS} seconds"
            )
        frozen_rows = _freeze_evidence_source_rows(payload.rows)
        declared_source_digest = str(payload.source_digest or "").strip().lower()
        if not _SHA256.fullmatch(declared_source_digest):
            raise EvidenceSnapshotIntegrityError("source digest must be lowercase SHA-256")
        actual_source_digest = _canonical_evidence_source_digest_from_frozen(
            source_id=source_id,
            cutoff=payload_cutoff,
            captured_at=captured_at,
            frozen_rows=frozen_rows,
        )
        if actual_source_digest != declared_source_digest:
            raise EvidenceSnapshotIntegrityError("source digest verification failed")
        expected_digest = None
        if expected_active_snapshot_digest is not None:
            expected_digest = str(expected_active_snapshot_digest).strip().lower()
            if not _SHA256.fullmatch(expected_digest):
                raise ValueError("expected_active_snapshot_digest must be lowercase SHA-256")

        accepted: list[dict[str, Any]] = []
        diagnostics: dict[str, int] = {}
        row_digests: set[str] = set()
        for candidate in frozen_rows:
            try:
                normalized = self._normalize_evidence_row(
                    candidate,
                    cutoff=payload_cutoff,
                    captured_at=captured_at,
                )
                row_digest = _sha256(_canonical_json(normalized))
                if row_digest in row_digests:
                    raise _RejectedEvidenceRow("duplicate")
                row_digests.add(row_digest)
                normalized["row_digest"] = row_digest
                accepted.append(normalized)
            except _RejectedEvidenceRow as exc:
                diagnostics[exc.reason] = diagnostics.get(exc.reason, 0) + 1

        accepted.sort(key=lambda item: item["row_digest"])
        supplied_count = len(frozen_rows)
        accepted_count = len(accepted)
        rejected_count = supplied_count - accepted_count
        if accepted_count == 0:
            completeness = "empty"
        elif rejected_count:
            completeness = "partial"
        else:
            completeness = "full"

        canonical_rows = [
            {key: value for key, value in item.items() if key != "row_digest"} for item in accepted
        ]
        core = {
            "schema_version": EVIDENCE_SNAPSHOT_SCHEMA_VERSION,
            "source_schema_version": EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
            "source_id": source_id,
            "source_digest": declared_source_digest,
            "cutoff": payload_cutoff.isoformat(),
            "cutoff_semantics": "exclusive-utc-date",
            "captured_at": _utc_iso(captured_at),
            "completeness": completeness,
            "supplied_row_count": supplied_count,
            "accepted_row_count": accepted_count,
            "rejected_row_count": rejected_count,
            "diagnostics": dict(sorted(diagnostics.items())),
            "rows": canonical_rows,
        }
        canonical_json = _canonical_json(core)
        snapshot_digest = _sha256(canonical_json)
        activated_at = _utc_now()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._verify_activation_chain(conn)
            current_digest = None if current is None else str(current["snapshot_digest"])
            existing = conn.execute(
                "SELECT canonical_json FROM evidence_snapshots WHERE snapshot_digest = ?",
                (snapshot_digest,),
            ).fetchone()
            if existing is not None and str(existing["canonical_json"]) != canonical_json:
                raise EvidenceSnapshotIntegrityError("snapshot digest collision detected")
            if current_digest == snapshot_digest:
                conn.commit()
            else:
                if expected_digest != current_digest:
                    raise EvidenceSnapshotConflictError(
                        "active snapshot changed; refresh the preflight view and retry"
                    )
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO evidence_snapshots (
                            snapshot_digest, schema_version, source_schema_version,
                            source_id, source_digest, cutoff, captured_at, completeness,
                            supplied_row_count, accepted_row_count, rejected_row_count,
                            diagnostics_json, canonical_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_digest,
                            EVIDENCE_SNAPSHOT_SCHEMA_VERSION,
                            EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
                            source_id,
                            declared_source_digest,
                            payload_cutoff.isoformat(),
                            _utc_iso(captured_at),
                            completeness,
                            supplied_count,
                            accepted_count,
                            rejected_count,
                            _canonical_json(dict(sorted(diagnostics.items()))),
                            canonical_json,
                            _utc_iso(activated_at),
                        ),
                    )
                    conn.executemany(
                        """
                        INSERT INTO evidence_snapshot_rows (
                            snapshot_digest, ordinal, row_digest, competitor_id,
                            event_code, time_seconds, species, diameter_mm, quality,
                            competition_id, heat_id, result_date
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                snapshot_digest,
                                ordinal,
                                item["row_digest"],
                                item["competitor_id"],
                                item["event_code"],
                                item["time_seconds"],
                                item["species"],
                                item["diameter_mm"],
                                item["quality"],
                                item["competition_id"],
                                item["heat_id"],
                                item["result_date"],
                            )
                            for ordinal, item in enumerate(accepted)
                        ],
                    )
                revision = 1 if current is None else int(current["revision"]) + 1
                previous_activation_id = None if current is None else str(current["activation_id"])
                activation_core = {
                    "schema_version": EVIDENCE_ACTIVATION_SCHEMA_VERSION,
                    "revision": revision,
                    "snapshot_digest": snapshot_digest,
                    "previous_activation_id": previous_activation_id,
                    "previous_snapshot_digest": current_digest,
                    "activated_at": _utc_iso(activated_at),
                }
                activation_json = _canonical_json(activation_core)
                activation_id = _sha256(activation_json)
                conn.execute(
                    """
                    INSERT INTO evidence_snapshot_activations (
                        activation_id, schema_version, revision, snapshot_digest,
                        previous_activation_id, previous_snapshot_digest,
                        activated_at, canonical_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        activation_id,
                        EVIDENCE_ACTIVATION_SCHEMA_VERSION,
                        revision,
                        snapshot_digest,
                        previous_activation_id,
                        current_digest,
                        _utc_iso(activated_at),
                        activation_json,
                    ),
                )
                conn.commit()

        status = self.get_evidence_snapshot_status()
        if status is None:  # pragma: no cover - transaction invariant
            raise EvidenceSnapshotIntegrityError("activated snapshot disappeared")
        return status

    @classmethod
    def _normalize_evidence_row(
        cls,
        candidate: Any,
        *,
        cutoff: date,
        captured_at: datetime,
    ) -> dict[str, Any]:
        if not isinstance(candidate, Mapping):
            raise _RejectedEvidenceRow("not_an_object")
        fields = {str(key) for key in candidate}
        required = _EVIDENCE_ROW_FIELDS - {"heat_id"}
        if not required.issubset(fields) or not fields.issubset(_EVIDENCE_ROW_FIELDS):
            raise _RejectedEvidenceRow("invalid_schema")
        if candidate.get("schema_version") != EVIDENCE_HISTORY_ROW_SCHEMA_VERSION:
            raise _RejectedEvidenceRow("invalid_schema")
        try:
            competitor_id = _namespaced_id(candidate.get("competitor_id"), "competitor_id")
            competition_id = _namespaced_id(candidate.get("competition_id"), "competition_id")
            heat_raw = str(candidate.get("heat_id") or "").strip()
            heat_id = _namespaced_id(heat_raw, "heat_id") if heat_raw else ""
        except ValueError as exc:
            raise _RejectedEvidenceRow("invalid_identity") from exc

        result_raw = candidate.get("result_date")
        if result_raw is None or not str(result_raw).strip():
            raise _RejectedEvidenceRow("undated")
        try:
            result_date = _date_value(result_raw, "result_date")
        except ValueError as exc:
            raise _RejectedEvidenceRow("invalid_date") from exc
        if result_date >= cutoff:
            raise _RejectedEvidenceRow("on_or_after_cutoff")
        if result_date > captured_at.date():
            raise _RejectedEvidenceRow("future_result_date")

        event_code = str(candidate.get("event_code") or "").strip().upper()
        if not is_valid_event(event_code):
            raise _RejectedEvidenceRow("invalid_event")
        species = str(candidate.get("species") or "").strip()
        if not _CODE.fullmatch(species):
            raise _RejectedEvidenceRow("invalid_species")
        try:
            if isinstance(candidate.get("time_seconds"), bool) or isinstance(
                candidate.get("diameter_mm"), bool
            ):
                raise ValueError
            time_seconds = float(candidate.get("time_seconds"))
            diameter_mm = float(candidate.get("diameter_mm"))
            quality_raw = candidate.get("quality")
            if isinstance(quality_raw, bool) or float(quality_raw) != int(quality_raw):
                raise ValueError
            quality = int(quality_raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _RejectedEvidenceRow("invalid_numeric") from exc
        if not math.isfinite(time_seconds) or not (
            rules.MIN_MARK_SECONDS <= time_seconds <= rules.MAX_TIME_LIMIT_SECONDS
        ):
            raise _RejectedEvidenceRow("invalid_numeric")
        if not math.isfinite(diameter_mm) or not (
            data_req.MIN_DIAMETER_MM <= diameter_mm <= data_req.MAX_DIAMETER_MM
        ):
            raise _RejectedEvidenceRow("invalid_numeric")
        if not 1 <= quality <= 10:
            raise _RejectedEvidenceRow("invalid_numeric")
        return {
            "schema_version": EVIDENCE_HISTORY_ROW_SCHEMA_VERSION,
            "competitor_id": competitor_id,
            "event_code": event_code,
            "time_seconds": time_seconds,
            "species": species,
            "diameter_mm": diameter_mm,
            "quality": quality,
            "competition_id": competition_id,
            "heat_id": heat_id,
            "result_date": result_date.isoformat(),
        }

    @staticmethod
    def _verify_activation_chain(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        """Verify the complete append-only activation hash chain and return its tip."""

        rows = conn.execute(
            """
            SELECT activation_id, schema_version, revision, snapshot_digest,
                   previous_activation_id, previous_snapshot_digest,
                   activated_at, canonical_json
            FROM evidence_snapshot_activations ORDER BY revision
            """
        ).fetchall()
        previous_activation_id = None
        previous_snapshot_digest = None
        tip = None
        for expected_revision, row in enumerate(rows, start=1):
            activation_id = str(row["activation_id"])
            canonical_json = str(row["canonical_json"])
            try:
                core = json.loads(canonical_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise EvidenceSnapshotIntegrityError(
                    "persisted activation JSON is malformed"
                ) from exc
            if not isinstance(core, Mapping) or _canonical_json(core) != canonical_json:
                raise EvidenceSnapshotIntegrityError("persisted activation JSON is not canonical")
            projected = {
                "schema_version": EVIDENCE_ACTIVATION_SCHEMA_VERSION,
                "revision": int(row["revision"]),
                "snapshot_digest": str(row["snapshot_digest"]),
                "previous_activation_id": (
                    str(row["previous_activation_id"])
                    if row["previous_activation_id"] is not None
                    else None
                ),
                "previous_snapshot_digest": (
                    str(row["previous_snapshot_digest"])
                    if row["previous_snapshot_digest"] is not None
                    else None
                ),
                "activated_at": str(row["activated_at"]),
            }
            if (
                row["schema_version"] != EVIDENCE_ACTIVATION_SCHEMA_VERSION
                or int(row["revision"]) != expected_revision
                or projected != core
                or _sha256(canonical_json) != activation_id
                or projected["previous_activation_id"] != previous_activation_id
                or projected["previous_snapshot_digest"] != previous_snapshot_digest
            ):
                raise EvidenceSnapshotIntegrityError(
                    "persisted activation hash chain is inconsistent"
                )
            try:
                _aware_utc_datetime(
                    datetime.fromisoformat(projected["activated_at"]),
                    "persisted activation timestamp",
                )
            except (TypeError, ValueError) as exc:
                raise EvidenceSnapshotIntegrityError(
                    "persisted activation timestamp is invalid"
                ) from exc
            previous_activation_id = activation_id
            previous_snapshot_digest = projected["snapshot_digest"]
            tip = {**projected, "activation_id": activation_id}
        return tip

    def get_evidence_snapshot_status(
        self,
        *,
        as_of: Optional[datetime] = None,
        max_age_days: int = DEFAULT_MAX_SNAPSHOT_AGE_DAYS,
    ) -> Optional[EvidenceSnapshotStatus]:
        """Verify and describe the active snapshot using local SQLite only."""

        if (
            not isinstance(max_age_days, int)
            or isinstance(max_age_days, bool)
            or not (0 <= max_age_days <= 3650)
        ):
            raise ValueError("max_age_days must be an integer between 0 and 3650")
        observed_at = _utc_now() if as_of is None else _aware_utc_datetime(as_of, "as_of")
        with self._connect() as conn:
            activation = self._verify_activation_chain(conn)
            if activation is None:
                return None
            record = conn.execute(
                "SELECT * FROM evidence_snapshots WHERE snapshot_digest = ?",
                (activation["snapshot_digest"],),
            ).fetchone()
            if record is None:
                raise EvidenceSnapshotIntegrityError(
                    "active activation references a missing snapshot"
                )
            rows = conn.execute(
                """
                SELECT ordinal, row_digest, competitor_id, event_code, time_seconds,
                       species, diameter_mm, quality, competition_id, heat_id, result_date
                FROM evidence_snapshot_rows
                WHERE snapshot_digest = ? ORDER BY ordinal
                """,
                (record["snapshot_digest"],),
            ).fetchall()

        canonical_json = str(record["canonical_json"])
        try:
            core = json.loads(canonical_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise EvidenceSnapshotIntegrityError("persisted snapshot JSON is malformed") from exc
        if not isinstance(core, Mapping) or _canonical_json(core) != canonical_json:
            raise EvidenceSnapshotIntegrityError("persisted snapshot JSON is not canonical")
        snapshot_digest = str(record["snapshot_digest"])
        if _sha256(canonical_json) != snapshot_digest:
            raise EvidenceSnapshotIntegrityError("persisted snapshot digest verification failed")
        projected_rows = []
        for ordinal, row in enumerate(rows):
            if int(row["ordinal"]) != ordinal:
                raise EvidenceSnapshotIntegrityError("persisted snapshot row order is invalid")
            projected = {
                "schema_version": EVIDENCE_HISTORY_ROW_SCHEMA_VERSION,
                "competitor_id": str(row["competitor_id"]),
                "event_code": str(row["event_code"]),
                "time_seconds": float(row["time_seconds"]),
                "species": str(row["species"]),
                "diameter_mm": float(row["diameter_mm"]),
                "quality": int(row["quality"]),
                "competition_id": str(row["competition_id"]),
                "heat_id": str(row["heat_id"]),
                "result_date": str(row["result_date"]),
            }
            if _sha256(_canonical_json(projected)) != str(row["row_digest"]):
                raise EvidenceSnapshotIntegrityError("persisted snapshot row digest is invalid")
            projected_rows.append(projected)
        if core.get("rows") != projected_rows:
            raise EvidenceSnapshotIntegrityError("persisted snapshot rows do not match digest core")

        diagnostics = core.get("diagnostics")
        if not isinstance(diagnostics, Mapping) or _canonical_json(diagnostics) != str(
            record["diagnostics_json"]
        ):
            raise EvidenceSnapshotIntegrityError("persisted snapshot diagnostics are inconsistent")
        scalar_pairs = {
            "schema_version": EVIDENCE_SNAPSHOT_SCHEMA_VERSION,
            "source_schema_version": EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
            "source_id": str(record["source_id"]),
            "source_digest": str(record["source_digest"]),
            "cutoff": str(record["cutoff"]),
            "captured_at": str(record["captured_at"]),
            "completeness": str(record["completeness"]),
            "supplied_row_count": int(record["supplied_row_count"]),
            "accepted_row_count": int(record["accepted_row_count"]),
            "rejected_row_count": int(record["rejected_row_count"]),
        }
        if any(core.get(key) != value for key, value in scalar_pairs.items()):
            raise EvidenceSnapshotIntegrityError("persisted snapshot metadata is inconsistent")
        if scalar_pairs["accepted_row_count"] != len(projected_rows):
            raise EvidenceSnapshotIntegrityError("persisted snapshot row count is inconsistent")

        try:
            captured_at = _aware_utc_datetime(
                datetime.fromisoformat(str(record["captured_at"])), "persisted captured_at"
            )
            activated_at = _aware_utc_datetime(
                datetime.fromisoformat(str(activation["activated_at"])),
                "persisted activated_at",
            )
            cutoff = date.fromisoformat(str(record["cutoff"]))
        except (TypeError, ValueError) as exc:
            raise EvidenceSnapshotIntegrityError("persisted snapshot dates are invalid") from exc
        if captured_at > observed_at + timedelta(seconds=MAX_CAPTURE_CLOCK_SKEW_SECONDS):
            raise EvidenceSnapshotIntegrityError("persisted captured_at is too far in the future")
        age_days = max(0, (observed_at.date() - captured_at.date()).days)
        freshness = "current" if age_days <= max_age_days else "stale"
        return EvidenceSnapshotStatus(
            schema_version=EVIDENCE_SNAPSHOT_SCHEMA_VERSION,
            snapshot_digest=snapshot_digest,
            source_schema_version=EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
            source_id=str(record["source_id"]),
            source_digest=str(record["source_digest"]),
            cutoff=cutoff,
            captured_at=captured_at,
            activated_at=activated_at,
            activation_id=str(activation["activation_id"]),
            activation_revision=int(activation["revision"]),
            previous_activation_id=activation["previous_activation_id"],
            supersedes_snapshot_digest=activation["previous_snapshot_digest"],
            completeness=str(record["completeness"]),
            supplied_row_count=int(record["supplied_row_count"]),
            accepted_row_count=int(record["accepted_row_count"]),
            rejected_row_count=int(record["rejected_row_count"]),
            diagnostics={str(key): int(value) for key, value in diagnostics.items()},
            age_days=age_days,
            freshness=freshness,
            integrity="verified",
            ready_for_offline=freshness == "current",
        )

    def get_evidence_history(
        self,
        competitor_id: str,
        event_code: Optional[str] = None,
    ) -> List[HistoricalResult]:
        """Return only verified rows from the currently active local snapshot."""

        identity = _namespaced_id(competitor_id, "competitor_id")
        selection = self.load_evidence_for_competitors([identity], event_code=event_code)
        if selection is None:
            return []
        return list(selection.histories[identity])

    def load_evidence_for_competitors(
        self,
        competitor_ids: Sequence[str],
        *,
        event_code: Optional[str] = None,
    ) -> Optional[EvidenceSnapshotSelection]:
        """Verify once, then bulk-load a field from that exact immutable digest."""

        if isinstance(competitor_ids, (str, bytes, bytearray)) or not isinstance(
            competitor_ids, Sequence
        ):
            raise ValueError("competitor_ids must be a bounded sequence")
        if not competitor_ids or len(competitor_ids) > 64:
            raise ValueError("competitor_ids must contain between 1 and 64 identities")
        identities = [_namespaced_id(value, "competitor_id") for value in competitor_ids]
        if len(set(identities)) != len(identities):
            raise ValueError("competitor_ids must not contain duplicates")
        event = None
        if event_code is not None:
            event = str(event_code).strip().upper()
            if not is_valid_event(event):
                raise ValueError(f"event_code must be one of {events.VALID_EVENTS}")
        status = self.get_evidence_snapshot_status()
        if status is None:
            return None
        placeholders = ",".join("?" for _ in identities)
        params: list[Any] = [status.snapshot_digest, *identities]
        sql = (
            "SELECT competitor_id, event_code, time_seconds, species, diameter_mm, quality, "
            "result_date, heat_id, ordinal FROM evidence_snapshot_rows "
            f"WHERE snapshot_digest = ? AND competitor_id IN ({placeholders}) "
        )
        if event is not None:
            sql += "AND event_code = ? "
            params.append(event)
        sql += "ORDER BY competitor_id, result_date, ordinal"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        histories: dict[str, list[HistoricalResult]] = {identity: [] for identity in identities}
        for row in rows:
            histories[str(row["competitor_id"])].append(
                HistoricalResult(
                    event_code=str(row["event_code"]),
                    time_seconds=float(row["time_seconds"]),
                    species=str(row["species"]),
                    diameter_mm=float(row["diameter_mm"]),
                    quality=int(row["quality"]),
                    result_date=date.fromisoformat(str(row["result_date"])),
                    heat_id=str(row["heat_id"]) or None,
                )
            )
        return EvidenceSnapshotSelection(
            status=status,
            histories={identity: tuple(histories[identity]) for identity in identities},
        )

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def record_result(
        self,
        competitor_name: str,
        event_code: str,
        time_seconds: float,
        species: str,
        diameter_mm: float,
        quality: int,
        heat_id: Optional[str] = None,
        result_date: Optional[date] = None,
        competition_id: Optional[str] = None,
    ) -> bool:
        """
        Append a single tournament result to the store.

        Duplicate results (same competitor_name + competition_id + heat_id +
        event_code + time_seconds) are silently ignored via INSERT OR IGNORE.

        Args:
            competitor_name: Competitor display name.
            event_code: 'SB' or 'UH'.
            time_seconds: Raw cutting time in seconds.
            species: Wood species code/name.
            diameter_mm: Block diameter.
            quality: Wood quality (1-10).
            heat_id: Optional heat/round identifier (e.g. 'SB-225mmSB-Heat1').
            result_date: Date of competition. None if unknown.
            competition_id: Stable show or source identifier. New callers should
                always provide it. Legacy callers fall back to a date-derived key.

        Returns:
            True if a new row was inserted, False if it was a duplicate.
        """
        (
            _competitor_name,
            _event_code,
            _time_seconds,
            _species,
            _diameter_mm,
            _quality,
        ) = self._validate_result_fields(
            competitor_name,
            event_code,
            time_seconds,
            species,
            diameter_mm,
            quality,
        )
        _heat_id = str(heat_id or "").strip()
        _result_date = result_date.isoformat() if result_date is not None else None
        _competition_id = self._competition_key(competition_id, _result_date)
        _recorded_at = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO results
                    (competitor_name, event_code, time_seconds, species,
                     diameter_mm, quality, competition_id, heat_id, result_date, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _competitor_name,
                    _event_code,
                    _time_seconds,
                    _species,
                    _diameter_mm,
                    _quality,
                    _competition_id,
                    _heat_id,
                    _result_date,
                    _recorded_at,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _competition_key(competition_id: Optional[str], result_date: Optional[str]) -> str:
        """Return a stable key while retaining safe legacy-call behavior."""
        if competition_id is not None:
            try:
                if pd.isna(competition_id):
                    competition_id = None
            except (TypeError, ValueError):
                pass
        key = str(competition_id).strip() if competition_id is not None else ""
        if key:
            return key
        return f"legacy:{result_date or 'unknown'}"

    @staticmethod
    def _validate_result_fields(
        competitor_name: str,
        event_code: str,
        time_seconds: float,
        species: str,
        diameter_mm: float,
        quality: int,
    ) -> tuple[str, str, float, str, float, int]:
        """Validate raw result data before it can affect future predictions."""
        name = str(competitor_name or "").strip()
        if not name:
            raise ValueError("competitor_name must not be empty")

        event = str(event_code or "").strip().upper()
        if not is_valid_event(event):
            raise ValueError(f"event_code must be one of {events.VALID_EVENTS}")

        try:
            time_value = float(time_seconds)
            diameter_value = float(diameter_mm)
            quality_value = int(quality)
        except (TypeError, ValueError) as exc:
            raise ValueError("time_seconds, diameter_mm, and quality must be numeric") from exc

        if (
            not math.isfinite(time_value)
            or not rules.MIN_MARK_SECONDS <= time_value <= rules.MAX_TIME_LIMIT_SECONDS
        ):
            raise ValueError(
                f"time_seconds must be between {rules.MIN_MARK_SECONDS} and "
                f"{rules.MAX_TIME_LIMIT_SECONDS}"
            )
        if not math.isfinite(diameter_value) or not (
            data_req.MIN_DIAMETER_MM <= diameter_value <= data_req.MAX_DIAMETER_MM
        ):
            raise ValueError(
                f"diameter_mm must be between {data_req.MIN_DIAMETER_MM} and "
                f"{data_req.MAX_DIAMETER_MM}"
            )
        if not 1 <= quality_value <= 10:
            raise ValueError("quality must be between 1 and 10")

        species_value = str(species or "").strip()
        if not species_value:
            raise ValueError("species must not be empty")

        return name, event, time_value, species_value, diameter_value, quality_value

    def import_from_dataframe(
        self,
        df: pd.DataFrame,
        skip_duplicates: bool = True,
    ) -> int:
        """
        Bulk-import results from a DataFrame.

        Expects columns matching the STRATHEX results_df format:
            competitor_name, event (or event_code), raw_time (or time_seconds),
            species, size_mm (or diameter_mm), quality, heat_id, date (or result_date).

        Missing columns are tolerated (heat_id and date default to empty/'').

        Args:
            df: DataFrame of historical results.
            skip_duplicates: If True (default), existing rows are silently skipped.

        Returns:
            Number of rows actually inserted.
        """
        if df is None or df.empty:
            return 0

        # Normalize column names
        df = df.copy()
        df.columns = [str(c).strip().lower() for c in df.columns]
        col_map = {
            "event": "event_code",
            "raw_time": "time_seconds",
            "size_mm": "diameter_mm",
            "date": "result_date",
            "show_id": "competition_id",
            "tournament_id": "competition_id",
        }
        df.rename(columns=col_map, inplace=True)

        required = [
            "competitor_name",
            "event_code",
            "time_seconds",
            "species",
            "diameter_mm",
            "quality",
        ]
        for col in required:
            if col not in df.columns:
                return 0

        # Fill optional columns
        if "heat_id" not in df.columns:
            df["heat_id"] = ""
        else:
            df["heat_id"] = df["heat_id"].fillna("").astype(str)
        if "result_date" not in df.columns:
            df["result_date"] = None
        if "competition_id" not in df.columns:
            df["competition_id"] = ""
        else:
            df["competition_id"] = df["competition_id"].fillna("").astype(str)

        _recorded_at = datetime.now(timezone.utc).isoformat()
        insert_sql = (
            (
                "INSERT OR IGNORE INTO results "
                "(competitor_name, event_code, time_seconds, species, "
                "diameter_mm, quality, competition_id, heat_id, result_date, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            if skip_duplicates
            else (
                "INSERT INTO results "
                "(competitor_name, event_code, time_seconds, species, "
                "diameter_mm, quality, competition_id, heat_id, result_date, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
        )

        inserted = 0
        with self._connect() as conn:
            for _, row in df.iterrows():
                try:
                    # Parse result_date
                    rd = row.get("result_date")
                    if (
                        pd.isna(rd)
                        if hasattr(rd, "__class__") and rd.__class__.__name__ in ("float", "NaT")
                        else False
                    ):
                        rd = None
                    if rd is not None:
                        try:
                            if hasattr(rd, "isoformat"):
                                rd = rd.date().isoformat() if hasattr(rd, "date") else str(rd)[:10]
                            else:
                                rd = str(rd)[:10]
                        except Exception:
                            rd = None

                    (
                        competitor_name,
                        event_code,
                        time_val,
                        species,
                        diameter_val,
                        quality_val,
                    ) = self._validate_result_fields(
                        row["competitor_name"],
                        row["event_code"],
                        row["time_seconds"],
                        row["species"],
                        row["diameter_mm"],
                        row["quality"],
                    )
                    heat_id = str(row.get("heat_id", "") or "").strip()
                    competition_id = self._competition_key(row.get("competition_id"), rd)

                    cursor = conn.execute(
                        insert_sql,
                        (
                            competitor_name,
                            event_code,
                            time_val,
                            species,
                            diameter_val,
                            quality_val,
                            competition_id,
                            heat_id,
                            rd,
                            _recorded_at,
                        ),
                    )
                    inserted += cursor.rowcount
                except Exception:
                    continue
            conn.commit()

        return inserted

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_competitor_history(
        self,
        competitor_name: str,
        event_code: Optional[str] = None,
    ) -> List[HistoricalResult]:
        """
        Return all stored results for a competitor as HistoricalResult objects.

        Args:
            competitor_name: Exact competitor name (case-insensitive match).
            event_code: Optional filter ('SB' or 'UH'). None returns all events.

        Returns:
            List of HistoricalResult, ordered oldest-first.
        """
        params: list = [competitor_name.strip().lower()]
        sql = (
            "SELECT event_code, time_seconds, species, diameter_mm, quality, "
            "result_date, heat_id "
            "FROM results "
            "WHERE LOWER(TRIM(competitor_name)) = ? "
        )
        if event_code is not None:
            sql += "AND event_code = ? "
            params.append(event_code.strip().upper())
        sql += "ORDER BY result_date ASC NULLS LAST, recorded_at ASC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            rd = None
            if row["result_date"]:
                try:
                    rd = date.fromisoformat(row["result_date"])
                except (ValueError, TypeError):
                    rd = None
            results.append(
                HistoricalResult(
                    event_code=row["event_code"],
                    time_seconds=float(row["time_seconds"]),
                    species=row["species"],
                    diameter_mm=float(row["diameter_mm"]),
                    quality=int(row["quality"]),
                    result_date=rd,
                    heat_id=row["heat_id"] or None,
                )
            )
        return results

    def get_all_as_dataframe(self) -> pd.DataFrame:
        """
        Return the full results table as a pandas DataFrame.

        Column names match the STRATHEX results_df format:
            competitor_name, event_code, raw_time, species, size_mm, quality,
            competition_id, heat_id, result_date, recorded_at.
        """
        with self._connect() as conn:
            df = pd.read_sql_query(
                "SELECT competitor_name, event_code, time_seconds AS raw_time, "
                "species, diameter_mm AS size_mm, quality, competition_id, heat_id, "
                "result_date, recorded_at FROM results "
                "ORDER BY result_date ASC, recorded_at ASC",
                conn,
            )
        # Normalize event column alias for STRATHEX compatibility
        if "event_code" in df.columns:
            df = df.rename(columns={"event_code": "event"})
        return df

    def get_competitors(self) -> List[str]:
        """Return a sorted list of all distinct competitor names in the store."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT competitor_name FROM results ORDER BY competitor_name"
            ).fetchall()
        return [r["competitor_name"] for r in rows]

    def count(self) -> int:
        """Return the total number of result rows in the store."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM results").fetchone()
        return int(row["n"])

    def __repr__(self) -> str:
        return f"ResultStore(path={self._path!r}, rows={self.count()})"
