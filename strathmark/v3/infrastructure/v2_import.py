"""Explicit, fail-closed, read-only V2 snapshot ingress for V3.

V2 is inspected directly through SQLite ``mode=ro``.  This adapter never
constructs a V2 store/ledger, never attaches the source to V3, never changes
its journaling mode, and never treats mutable named ``results`` as evidence.
The resulting historical rows remain ineligible until a later signed cutover
manifest binds the exact synthetic source tip.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    DEFAULT_CONNECTION_POLICY,
    SQLiteDeadline,
    SQLiteDeadlineExceeded,
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection


class V2ImportError(RuntimeError):
    """Base class for a rejected V2 historical snapshot import."""


class V2ImportPathConflictError(V2ImportError):
    """V2 source and V3 authority resolve to the same file."""


class V2SourceChangedError(V2ImportError):
    """The V2 source changed while it was inspected or imported."""


class V2SourceSchemaError(V2ImportError):
    """The V2 source does not exactly match a supported released profile."""


class V2SourceIntegrityError(V2ImportError):
    """V2 rows, chains, or canonical payloads failed consistency checks."""


@dataclass(frozen=True, slots=True)
class V2ImportResult:
    """Deterministic receipt for one ineligible historical ingress."""

    import_id: str
    source_tip_digest: str
    source_catalog_digest: str
    profile_ids: tuple[str, ...]
    imported_row_count: int
    cutoff: str
    eligible: bool
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    source_tip_digest: str
    catalog_digest: str
    profile_ids: tuple[str, ...]
    cutoff: str
    importable_rows: tuple[tuple[str, str, int, str, str], ...]
    manifest: Mapping[str, Any]


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LIMITATIONS = (
    "cannot authenticate source actor or original write",
    "cannot prove host/key non-compromise from SQLite bytes alone",
    "legacy request and settlement hashes may prove consistency but not external provenance",
    "mutable results and operational outbox/nonces are excluded from authoritative evidence",
)

# Exact current released V2 table shapes.  Profiles compose: a source can carry
# results, evidence snapshots, and the shadow ledger in the same SQLite file.
_TABLE_COLUMNS: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "results": (
        ("id", "INTEGER", 0, 1),
        ("competitor_name", "TEXT", 1, 0),
        ("event_code", "TEXT", 1, 0),
        ("time_seconds", "REAL", 1, 0),
        ("species", "TEXT", 1, 0),
        ("diameter_mm", "REAL", 1, 0),
        ("quality", "INTEGER", 1, 0),
        ("competition_id", "TEXT", 1, 0),
        ("heat_id", "TEXT", 1, 0),
        ("result_date", "TEXT", 0, 0),
        ("recorded_at", "TEXT", 1, 0),
    ),
    "evidence_snapshots": (
        ("snapshot_digest", "TEXT", 0, 1),
        ("schema_version", "TEXT", 1, 0),
        ("source_schema_version", "TEXT", 1, 0),
        ("source_id", "TEXT", 1, 0),
        ("source_digest", "TEXT", 1, 0),
        ("cutoff", "TEXT", 1, 0),
        ("captured_at", "TEXT", 1, 0),
        ("completeness", "TEXT", 1, 0),
        ("supplied_row_count", "INTEGER", 1, 0),
        ("accepted_row_count", "INTEGER", 1, 0),
        ("rejected_row_count", "INTEGER", 1, 0),
        ("diagnostics_json", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "evidence_snapshot_rows": (
        ("snapshot_digest", "TEXT", 1, 1),
        ("ordinal", "INTEGER", 1, 2),
        ("row_digest", "TEXT", 1, 0),
        ("competitor_id", "TEXT", 1, 0),
        ("event_code", "TEXT", 1, 0),
        ("time_seconds", "REAL", 1, 0),
        ("species", "TEXT", 1, 0),
        ("diameter_mm", "REAL", 1, 0),
        ("quality", "INTEGER", 1, 0),
        ("competition_id", "TEXT", 1, 0),
        ("heat_id", "TEXT", 1, 0),
        ("result_date", "TEXT", 1, 0),
    ),
    "evidence_snapshot_activations": (
        ("activation_id", "TEXT", 0, 1),
        ("schema_version", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("snapshot_digest", "TEXT", 1, 0),
        ("previous_activation_id", "TEXT", 0, 0),
        ("previous_snapshot_digest", "TEXT", 0, 0),
        ("activated_at", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "prediction_requests": (
        ("ledger_request_id", "TEXT", 0, 1),
        ("caller_id", "TEXT", 1, 0),
        ("request_id", "TEXT", 1, 0),
        ("request_hash", "TEXT", 1, 0),
        ("hash_algorithm", "TEXT", 1, 0),
        ("event_code", "TEXT", 1, 0),
        ("prediction_as_of", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "ledger_predictions": (
        ("prediction_id", "TEXT", 0, 1),
        ("ledger_request_id", "TEXT", 1, 0),
        ("competitor_id", "TEXT", 1, 0),
        ("ordinal", "INTEGER", 1, 0),
        ("event_code", "TEXT", 1, 0),
        ("median_seconds", "REAL", 1, 0),
        ("assigned_mark", "INTEGER", 1, 0),
        ("source", "TEXT", 1, 0),
        ("training_eligible", "INTEGER", 1, 0),
        ("engine_version", "TEXT", 0, 0),
        ("model_version", "TEXT", 0, 0),
        ("calibration_version", "TEXT", 0, 0),
        ("evidence_cutoff", "TEXT", 0, 0),
        ("interval_lower", "REAL", 0, 0),
        ("interval_upper", "REAL", 0, 0),
        ("interval_coverage", "REAL", 0, 0),
        ("interval_state", "TEXT", 0, 0),
        ("interval_scope", "TEXT", 0, 0),
        ("ignored_factors_json", "TEXT", 1, 0),
        ("warnings_json", "TEXT", 1, 0),
        ("optimizer", "TEXT", 0, 0),
        ("optimizer_metadata_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "prediction_features": (
        ("feature_snapshot_id", "TEXT", 0, 1),
        ("prediction_id", "TEXT", 1, 0),
        ("feature_name", "TEXT", 1, 0),
        ("numeric_value", "REAL", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "prediction_settlements": (
        ("settlement_id", "TEXT", 0, 1),
        ("prediction_id", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("competitor_id", "TEXT", 1, 0),
        ("event_code", "TEXT", 1, 0),
        ("actual_time", "REAL", 1, 0),
        ("residual", "REAL", 1, 0),
        ("actor", "TEXT", 1, 0),
        ("reason", "TEXT", 0, 0),
        ("payload_hash", "TEXT", 1, 0),
        ("supersedes_settlement_id", "TEXT", 0, 0),
        ("settled_at", "TEXT", 1, 0),
    ),
    "prediction_mirror_outbox": (
        ("outbox_id", "TEXT", 0, 1),
        ("kind", "TEXT", 1, 0),
        ("entity_id", "TEXT", 1, 0),
        ("payload_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "prediction_mirror_delivery": (
        ("outbox_id", "TEXT", 0, 1),
        ("attempts", "INTEGER", 1, 0),
        ("status", "TEXT", 1, 0),
        ("last_attempt_at", "TEXT", 1, 0),
    ),
    "shadow_receipts": (
        ("ledger_request_id", "TEXT", 0, 1),
        ("caller_id", "TEXT", 1, 0),
        ("request_id", "TEXT", 1, 0),
        ("active_input_fingerprint", "TEXT", 1, 0),
        ("core_schema_version", "TEXT", 1, 0),
        ("core_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "numeric_outcome_revisions": (
        ("field_revision_id", "TEXT", 0, 1),
        ("outcome_revision_id", "TEXT", 1, 0),
        ("ledger_request_id", "TEXT", 1, 0),
        ("caller_id", "TEXT", 1, 0),
        ("payload_hash", "TEXT", 1, 0),
        ("actor", "TEXT", 1, 0),
        ("reason_code", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "numeric_settlement_revisions": (
        ("revision_id", "TEXT", 0, 1),
        ("field_revision_id", "TEXT", 1, 0),
        ("prediction_id", "TEXT", 1, 0),
        ("revision", "INTEGER", 1, 0),
        ("competitor_id", "TEXT", 1, 0),
        ("event_code", "TEXT", 1, 0),
        ("action", "TEXT", 1, 0),
        ("actual_time", "REAL", 0, 0),
        ("residual", "REAL", 0, 0),
        ("supersedes_revision_id", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "actor_attestation_nonce_claims": (
        ("consumer_id", "TEXT", 1, 1),
        ("nonce_hash", "TEXT", 1, 2),
        ("actor_id", "TEXT", 1, 0),
        ("action", "TEXT", 1, 0),
        ("subject_revision", "TEXT", 1, 0),
        ("expires_at", "INTEGER", 1, 0),
        ("claimed_at", "TEXT", 1, 0),
    ),
}

_RESULTS_GROUP = frozenset({"results"})
_EVIDENCE_GROUP = frozenset(
    {"evidence_snapshots", "evidence_snapshot_rows", "evidence_snapshot_activations"}
)
_LEDGER_GROUP = frozenset(
    {
        "prediction_requests",
        "ledger_predictions",
        "prediction_features",
        "prediction_settlements",
        "prediction_mirror_outbox",
        "prediction_mirror_delivery",
        "shadow_receipts",
        "numeric_outcome_revisions",
        "numeric_settlement_revisions",
        "actor_attestation_nonce_claims",
    }
)
_LEDGER_CORE_GROUP = frozenset(
    {"prediction_requests", "ledger_predictions", "prediction_features", "prediction_settlements"}
)
_LEDGER_OUTBOX_GROUP = _LEDGER_CORE_GROUP | frozenset(
    {"prediction_mirror_outbox", "prediction_mirror_delivery"}
)
_IMMUTABLE_TABLES = (_EVIDENCE_GROUP | _LEDGER_GROUP) - {
    "prediction_mirror_outbox",
    "prediction_mirror_delivery",
    "actor_attestation_nonce_claims",
}
_EXCLUDED_OPERATIONAL = frozenset(
    {
        "results",
        "prediction_mirror_outbox",
        "prediction_mirror_delivery",
        "actor_attestation_nonce_claims",
    }
)
_GROUP_FOR_TABLE = {
    **{name: "evidence-snapshot" for name in _EVIDENCE_GROUP},
    **{name: "shadow-ledger" for name in _LEDGER_GROUP - _EXCLUDED_OPERATIONAL},
}
_LEGACY_RESULTS_COLUMNS = tuple(
    item for item in _TABLE_COLUMNS["results"] if item[0] != "competition_id"
)
_LEGACY_REQUEST_COLUMNS = tuple(
    item for item in _TABLE_COLUMNS["prediction_requests"] if item[0] != "hash_algorithm"
)
_UPGRADED_REQUEST_COLUMNS = _LEGACY_REQUEST_COLUMNS + (("hash_algorithm", "TEXT", 1, 0),)

_EXPECTED_INDEX_SQL = {
    "idx_results_competitor": "CREATE INDEX idx_results_competitor ON results(competitor_name, event_code)",
    "idx_evidence_snapshot_competitor": (
        "CREATE INDEX idx_evidence_snapshot_competitor ON evidence_snapshot_rows"
        "(snapshot_digest, competitor_id, event_code, result_date)"
    ),
    "idx_evidence_snapshot_activations_revision": (
        "CREATE INDEX idx_evidence_snapshot_activations_revision "
        "ON evidence_snapshot_activations(revision)"
    ),
    "idx_ledger_predictions_competitor": (
        "CREATE INDEX idx_ledger_predictions_competitor "
        "ON ledger_predictions(competitor_id, event_code)"
    ),
    "idx_prediction_settlements_prediction": (
        "CREATE INDEX idx_prediction_settlements_prediction "
        "ON prediction_settlements(prediction_id, revision DESC)"
    ),
    "idx_numeric_settlement_revisions_prediction": (
        "CREATE INDEX idx_numeric_settlement_revisions_prediction "
        "ON numeric_settlement_revisions(prediction_id, revision DESC)"
    ),
    "idx_prediction_mirror_outbox_pending_scan": (
        "CREATE INDEX idx_prediction_mirror_outbox_pending_scan "
        "ON prediction_mirror_outbox(created_at, outbox_id, kind, entity_id)"
    ),
    "idx_prediction_mirror_delivery_status": (
        "CREATE INDEX idx_prediction_mirror_delivery_status "
        "ON prediction_mirror_delivery(status, outbox_id)"
    ),
    "idx_actor_attestation_nonce_expiry": (
        "CREATE INDEX idx_actor_attestation_nonce_expiry "
        "ON actor_attestation_nonce_claims(expires_at)"
    ),
}
_TABLE_SQL_DIGESTS = {
    "actor_attestation_nonce_claims": "925e547305f29c3931f8ed227c1cf543f5c44590bb82687a4e9712c43ab20706",
    "evidence_snapshot_activations": "da26a3b0e25173ee48cc85601987375d19f781e221b9ffade72bd0023fa6f30c",
    "evidence_snapshot_rows": "1477f5087aa467cdb3bd1e88b6fcc2f135ef703d7e7e0b8c6bfee7c401640ea1",
    "evidence_snapshots": "862f2ff7f36621e8782dc92eb800bca087f0cbf090d5d5692987c3cea8e6d619",
    "ledger_predictions": "eb9f48fd178f01f290daef1dc720564728dc0083faa62ad9032735664e0c2bcd",
    "numeric_outcome_revisions": "8e4a31047b3009e8d6c0e56e1dee29ad6de70ce1f9257684deb298f0655928ba",
    "numeric_settlement_revisions": "952c3ab71739fe20c07aba8762f2e88eb888502d64edbb71a5de79b7bdd5e6af",
    "prediction_features": "423952a8ee5cb168693bfac772b7a6b3ddaab35e8d6197373d9c6580e223f223",
    "prediction_mirror_delivery": "fd2799eae2baf7b02bafe5bd810211c4efb09dcb50aa7ef5c45da4e28394b087",
    "prediction_mirror_outbox": "83728cbd0503925b6fe668afeb77c4018f1ec1843362bc7aeeffe43ef76ae05e",
    "prediction_settlements": "e65b908ad159abe4dc161fac133aded56da997f41e56c4d29219e130d9525db8",
    "shadow_receipts": "a2f12d3ab554722ced735a8f9a81fc55621a07c8217a6f125ebdafb5c6d9bf4f",
}
_RESULTS_SQL_DIGESTS = {
    "v2-results-current-v1": "fa9512e4935e718d7f27a8f482ed373a685459f72e8e63f44360acec4bca95a4",
    "v2-results-legacy-v1": "eb626d288947122fe7c0587419102ce96b891eedbdd501cb2b5bb02ccdf67f5d",
}
_REQUEST_SQL_DIGESTS = {
    "legacy-no-hash": "01f95e8a2e7d52a5601fed36b6352374197ae361855c698a91383339395f98ca",
    "fresh-active-v2": "dfdc84e02718533aedae92f063dab50faa0563915fe01338bce7189a347fa890",
    "upgraded-raw-v1": "40369952ca7d5b4007c5add9ee1edd933518ebeaa7dae3fc2771413646113b64",
}


@contextmanager
def open_v2_readonly(
    source_path: Path | str,
    *,
    deadline: SQLiteDeadline | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open V2 with SQLite-enforced read-only and query-only policy."""

    source = Path(source_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError(source)
    if deadline is not None and not isinstance(deadline, SQLiteDeadline):
        raise V2ImportError("deadline must be a SQLiteDeadline")
    if deadline is not None:
        deadline.raise_if_expired()
        timeout_ms = min(
            DEFAULT_CONNECTION_POLICY.busy_timeout_ms, deadline.remaining_milliseconds()
        )
    else:
        timeout_ms = DEFAULT_CONNECTION_POLICY.busy_timeout_ms
    connection = sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=timeout_ms / 1000,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        connection.execute("PRAGMA query_only = ON")
        if deadline is not None:
            connection.set_progress_handler(
                deadline.progress_handler, DEFAULT_CONNECTION_POLICY.progress_opcode_interval
            )
            deadline.raise_if_expired()
        yield connection
    finally:
        connection.close()


def import_v2_snapshot(
    source_path: Path | str,
    destination_path: Path | str,
    *,
    cutoff: str,
    deadline: SQLiteDeadline | None = None,
) -> V2ImportResult:
    """Verify one stable V2 snapshot and append its ineligible V3 import event."""

    source = Path(source_path).expanduser().resolve(strict=True)
    destination = Path(destination_path).expanduser().resolve(strict=False)
    _reject_same_file(source, destination)
    normalized_cutoff = _require_cutoff(cutoff)
    before = _source_file_manifest(source)
    if any(entry[1] is not None for entry in before[1:]):
        raise V2SourceChangedError(
            "active V2 WAL/shared-memory/journal state must be checkpointed and closed before import"
        )
    before_state = _source_file_state(source)
    primary_error: BaseException | None = None
    completed = False
    try:
        snapshot = _read_source_snapshot(source, cutoff=normalized_cutoff, deadline=deadline)
        repeated = _read_source_snapshot(source, cutoff=normalized_cutoff, deadline=deadline)
        if snapshot != repeated or _source_file_manifest(source) != before:
            raise V2SourceChangedError("V2 source changed between consistent snapshot reads")
        with open_v3_connection(destination) as v3:
            migrate_connection(v3)
            existing = v3.execute(
                "SELECT * FROM v3_historical_imports WHERE source_tip_digest = ?",
                (snapshot.source_tip_digest,),
            ).fetchone()
            if existing is None:
                concurrent = _persist_snapshot(
                    v3,
                    snapshot,
                    source=source,
                    expected_source_state=before_state,
                )
                if concurrent is not None:
                    _verify_existing_import(v3, concurrent, snapshot)
            else:
                _verify_existing_import(v3, existing, snapshot)
                if _source_file_manifest(source) != before:
                    raise V2SourceChangedError("V2 source changed during exact import lookup")
        completed = True
        return _result_from_snapshot(snapshot)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if not completed and before != _source_file_manifest(source):
            changed = V2SourceChangedError(
                "V2 source database/WAL files changed during read-only import"
            )
            raise changed from primary_error


def _reject_same_file(source: Path, destination: Path) -> None:
    if str(source).casefold() == str(destination).casefold():
        raise V2ImportPathConflictError("V2 source and V3 destination must be distinct")
    if destination.exists():
        try:
            if source.samefile(destination):
                raise V2ImportPathConflictError("V2 source and V3 destination are the same file")
        except OSError as exc:
            raise V2ImportPathConflictError(
                "cannot establish distinct source/destination identity"
            ) from exc


def _require_cutoff(value: str) -> str:
    try:
        return require_utc_milliseconds(value)
    except Exception as exc:
        raise V2ImportError("cutoff must be a valid canonical UTC instant") from exc


def _source_file_manifest(
    source: Path,
) -> tuple[tuple[str, int, str] | tuple[str, None, None], ...]:
    observed: list[tuple[str, int, str] | tuple[str, None, None]] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{source}{suffix}")
        if not candidate.exists():
            observed.append((candidate.name, None, None))
            continue
        digest = hashlib.sha256()
        size = 0
        with candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        observed.append((candidate.name, size, digest.hexdigest()))
    return tuple(observed)


def _source_file_state(
    source: Path,
) -> tuple[tuple[str, int, int, int, int] | tuple[str, None, None, None, None], ...]:
    observed: list[tuple[str, int, int, int, int] | tuple[str, None, None, None, None]] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{source}{suffix}")
        try:
            stat = candidate.stat()
        except OSError:
            observed.append((candidate.name, None, None, None, None))
        else:
            observed.append(
                (candidate.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
            )
    return tuple(observed)


def _read_source_snapshot(
    source: Path, *, cutoff: str, deadline: SQLiteDeadline | None
) -> _SourceSnapshot:
    with open_v2_readonly(source, deadline=deadline) as connection:
        connection.execute("BEGIN")
        try:
            start_data_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if [str(row[0]) for row in integrity] != ["ok"]:
                raise V2SourceIntegrityError("V2 SQLite integrity_check failed")
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise V2SourceIntegrityError("V2 SQLite foreign_key_check failed")
            catalog, table_names = _catalog(connection)
            profile_ids = _validate_profiles(connection, table_names)
            _verify_evidence(connection, table_names)
            _verify_shadow_and_settlements(connection, table_names)
            row_summary = _build_row_manifests(connection, table_names, profile_ids=profile_ids)
            importable_rows = _build_importable_evidence(connection, table_names)
            chain_tips = _available_chain_tips(connection, table_names)
            end_data_version = int(connection.execute("PRAGMA data_version").fetchone()[0])
            if start_data_version != end_data_version:
                raise V2SourceChangedError("V2 data_version changed during snapshot read")
            connection.commit()
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            if deadline is not None and deadline.cancelled:
                raise SQLiteDeadlineExceeded(
                    "V2 snapshot read deadline expired or was cancelled"
                ) from exc
            raise V2ImportError("V2 snapshot read failed or remained locked past deadline") from exc
        except BaseException:
            connection.rollback()
            raise
    catalog_digest = canonical_digest({"catalog": catalog})
    tip_material = {
        "schema_version": "strathmark-v3-v2-source-tip-v2",
        "profiles": list(profile_ids),
        "catalog_digest": catalog_digest,
        "rows": list(row_summary),
        "chain_tips": chain_tips,
        "cutoff": cutoff,
        "excluded_tables": sorted(table_names.intersection(_EXCLUDED_OPERATIONAL)),
        "limitations": list(_LIMITATIONS),
    }
    tip = canonical_digest(tip_material)
    manifest = {**tip_material, "source_tip_digest": tip}
    return _SourceSnapshot(
        source_tip_digest=tip,
        catalog_digest=catalog_digest,
        profile_ids=profile_ids,
        cutoff=cutoff,
        importable_rows=importable_rows,
        manifest=manifest,
    )


def _catalog(connection: sqlite3.Connection) -> tuple[list[dict[str, str]], set[str]]:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type, name"
    ).fetchall()
    catalog = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": " ".join(str(row[3]).split()),
        }
        for row in rows
    ]
    return catalog, {item["name"] for item in catalog if item["type"] == "table"}


def _validate_profiles(connection: sqlite3.Connection, tables: set[str]) -> tuple[str, ...]:
    unknown = tables - set(_TABLE_COLUMNS)
    if unknown:
        raise V2SourceSchemaError(f"unsupported V2 tables: {', '.join(sorted(unknown))}")
    profiles: list[str] = []
    if "results" in tables:
        result_columns = _column_signature(connection, "results")
        if result_columns == _TABLE_COLUMNS["results"]:
            profiles.append("v2-results-current-v1")
        elif result_columns == _LEGACY_RESULTS_COLUMNS:
            profiles.append("v2-results-legacy-v1")
        else:
            raise V2SourceSchemaError("V2 table semantics drifted: results")
    evidence_present = tables.intersection(_EVIDENCE_GROUP)
    if evidence_present and evidence_present != _EVIDENCE_GROUP:
        missing = ", ".join(sorted(_EVIDENCE_GROUP - evidence_present))
        raise V2SourceSchemaError(f"partial v2-evidence-snapshot-v1 schema; missing {missing}")
    if evidence_present:
        profiles.append("v2-evidence-snapshot-v1")
    ledger_present = tables.intersection(_LEDGER_GROUP)
    request_variant: str | None = None
    if ledger_present:
        if ledger_present == _LEDGER_CORE_GROUP:
            request_variant = _request_variant(connection, allow_current=False)
            profiles.append("v2-ledger-core-no-hash-v1")
        elif ledger_present == _LEDGER_OUTBOX_GROUP:
            request_variant = _request_variant(connection, allow_current=False)
            profiles.append("v2-ledger-outbox-no-hash-v1")
        elif ledger_present == _LEDGER_GROUP:
            request_variant = _request_variant(connection, allow_current=True)
            profiles.append(
                "v2-shadow-ledger-current-fresh-v1"
                if request_variant == "fresh-active-v2"
                else "v2-shadow-ledger-current-upgraded-raw-v1"
            )
        else:
            raise V2SourceSchemaError("partial or unsupported released V2 ledger profile")
    if not profiles:
        profiles.append("v2-empty-v1")
    for table in sorted(tables):
        if table in {"results", "prediction_requests"}:
            continue
        if _column_signature(connection, table) != _TABLE_COLUMNS[table]:
            raise V2SourceSchemaError(f"V2 table semantics drifted: {table}")
    _require_exact_table_sql(connection, tables, tuple(profiles), request_variant)
    for table in sorted(tables.intersection(_IMMUTABLE_TABLES)):
        _require_immutable_triggers(connection, table)
    if "actor_attestation_nonce_claims" in tables:
        _require_nonce_update_trigger(connection)
    _require_exact_indexes(connection, tables, tuple(profiles))
    return tuple(profiles)


def _require_exact_table_sql(
    connection: sqlite3.Connection,
    tables: set[str],
    profiles: tuple[str, ...],
    request_variant: str | None,
) -> None:
    expected = {name: digest for name, digest in _TABLE_SQL_DIGESTS.items() if name in tables}
    if "results" in tables:
        result_profile = next(profile for profile in profiles if profile.startswith("v2-results-"))
        expected["results"] = _RESULTS_SQL_DIGESTS[result_profile]
    if "prediction_requests" in tables:
        assert request_variant is not None
        expected["prediction_requests"] = _REQUEST_SQL_DIGESTS[request_variant]
    for table, digest in expected.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert row is not None
        observed = hashlib.sha256(_normalize_sql(str(row[0])).encode()).hexdigest()
        if observed != digest:
            raise V2SourceSchemaError(f"V2 table catalog semantics drifted: {table}")


def _column_signature(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, str, int, int], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    )


def _request_variant(connection: sqlite3.Connection, *, allow_current: bool) -> str:
    rows = connection.execute('PRAGMA table_info("prediction_requests")').fetchall()
    signature = tuple((str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows)
    if signature == _LEGACY_REQUEST_COLUMNS:
        return "legacy-no-hash"
    if not allow_current:
        raise V2SourceSchemaError("V2 prediction_requests shape is not valid for this profile")
    hash_rows = [row for row in rows if str(row[1]) == "hash_algorithm"]
    if signature == _TABLE_COLUMNS["prediction_requests"] and len(hash_rows) == 1:
        if str(hash_rows[0][4]) != "'active-v2'":
            raise V2SourceSchemaError("fresh V2 hash_algorithm default drifted")
        return "fresh-active-v2"
    if signature == _UPGRADED_REQUEST_COLUMNS and len(hash_rows) == 1:
        if str(hash_rows[0][4]) != "'raw-v1'":
            raise V2SourceSchemaError("upgraded V2 hash_algorithm default drifted")
        return "upgraded-raw-v1"
    raise V2SourceSchemaError("unsupported V2 prediction_requests catalog variant")


def _normalize_sql(value: str) -> str:
    normalized = " ".join(value.casefold().replace(" if not exists", "").split())
    normalized = re.sub(r"\s*([(),;])\s*", r"\1", normalized).rstrip(";")
    return normalized


def _require_immutable_triggers(connection: sqlite3.Connection, table: str) -> None:
    rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()
    observed = {_trigger_body(str(row[0])) for row in rows}
    expected = {
        _normalize_sql(
            f"BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
        ),
        _normalize_sql(
            f"BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
        ),
    }
    if observed != expected:
        raise V2SourceSchemaError(f"immutable trigger semantics missing for {table}")


def _require_nonce_update_trigger(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='actor_attestation_nonce_claims'"
    ).fetchall()
    expected = {
        _normalize_sql(
            "BEFORE UPDATE ON actor_attestation_nonce_claims "
            "BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
        )
    }
    if {_trigger_body(str(row[0])) for row in rows} != expected:
        raise V2SourceSchemaError("nonce no-update trigger semantics drifted")


def _trigger_body(sql: str) -> str:
    normalized = _normalize_sql(sql)
    match = re.fullmatch(r"create trigger [^ ]+ (.+)", normalized)
    if match is None:
        raise V2SourceSchemaError("malformed V2 trigger definition")
    return match.group(1)


def _require_exact_indexes(
    connection: sqlite3.Connection, tables: set[str], profiles: tuple[str, ...]
) -> None:
    names: set[str] = set()
    if "results" in tables:
        names.add("idx_results_competitor")
    if _EVIDENCE_GROUP.issubset(tables):
        names.update(
            {"idx_evidence_snapshot_competitor", "idx_evidence_snapshot_activations_revision"}
        )
    if _LEDGER_CORE_GROUP.issubset(tables):
        names.update({"idx_ledger_predictions_competitor", "idx_prediction_settlements_prediction"})
    if _LEDGER_GROUP.issubset(tables):
        names.update(
            {
                "idx_numeric_settlement_revisions_prediction",
                "idx_prediction_mirror_outbox_pending_scan",
                "idx_prediction_mirror_delivery_status",
                "idx_actor_attestation_nonce_expiry",
            }
        )
    observed_rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ).fetchall()
    observed = {str(row[0]): _normalize_sql(str(row[1])) for row in observed_rows}
    expected = {name: _normalize_sql(_EXPECTED_INDEX_SQL[name]) for name in names}
    if observed != expected:
        raise V2SourceSchemaError(f"V2 index semantics drifted for profiles {', '.join(profiles)}")


def _v2_canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _verify_evidence(connection: sqlite3.Connection, tables: set[str]) -> None:
    if not _EVIDENCE_GROUP.issubset(tables):
        return
    snapshots: dict[str, Mapping[str, Any]] = {}
    rows_by_snapshot: dict[str, list[Mapping[str, Any]]] = {}
    for row in connection.execute("SELECT * FROM evidence_snapshots ORDER BY snapshot_digest"):
        canonical_json = str(row["canonical_json"])
        try:
            core = json.loads(canonical_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise V2SourceIntegrityError("malformed V2 evidence snapshot JSON") from exc
        if not isinstance(core, Mapping) or _v2_canonical_json(core) != canonical_json:
            raise V2SourceIntegrityError("noncanonical V2 evidence snapshot JSON")
        digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        if digest != str(row["snapshot_digest"]):
            raise V2SourceIntegrityError("V2 evidence snapshot digest mismatch")
        snapshots[digest] = core
        rows_by_snapshot[digest] = []
    for row in connection.execute(
        "SELECT * FROM evidence_snapshot_rows ORDER BY snapshot_digest, ordinal"
    ):
        snapshot_digest = str(row["snapshot_digest"])
        if snapshot_digest not in snapshots:
            raise V2SourceIntegrityError("V2 evidence row references missing snapshot")
        projected = {
            "schema_version": "strathmark.evidence-history-row.v1",
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
        if int(row["ordinal"]) != len(rows_by_snapshot[snapshot_digest]):
            raise V2SourceIntegrityError("V2 evidence row ordinals are not consecutive")
        if hashlib.sha256(_v2_canonical_json(projected).encode()).hexdigest() != str(
            row["row_digest"]
        ):
            raise V2SourceIntegrityError("V2 evidence row digest mismatch")
        rows_by_snapshot[snapshot_digest].append(projected)
    for digest, core in snapshots.items():
        expected_rows = core.get("rows")
        if expected_rows != rows_by_snapshot[digest]:
            raise V2SourceIntegrityError("V2 snapshot row material does not match canonical core")
        if int(core.get("accepted_row_count", -1)) != len(expected_rows):
            raise V2SourceIntegrityError("V2 snapshot accepted row count is inconsistent")
        record = connection.execute(
            "SELECT * FROM evidence_snapshots WHERE snapshot_digest=?", (digest,)
        ).fetchone()
        assert record is not None
        try:
            diagnostics = json.loads(str(record["diagnostics_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise V2SourceIntegrityError("V2 snapshot diagnostics are malformed") from exc
        scalar = {
            "schema_version": "strathmark.evidence-snapshot.v1",
            "source_schema_version": "strathmark.evidence-snapshot-source.v1",
            "source_id": str(record["source_id"]),
            "source_digest": str(record["source_digest"]),
            "cutoff": str(record["cutoff"]),
            "captured_at": str(record["captured_at"]),
            "completeness": str(record["completeness"]),
            "supplied_row_count": int(record["supplied_row_count"]),
            "accepted_row_count": int(record["accepted_row_count"]),
            "rejected_row_count": int(record["rejected_row_count"]),
        }
        if (
            any(core.get(key) != value for key, value in scalar.items())
            or core.get("diagnostics") != diagnostics
            or _v2_canonical_json(diagnostics) != str(record["diagnostics_json"])
            or not _SHA256.fullmatch(scalar["source_digest"])
            or scalar["supplied_row_count"]
            != scalar["accepted_row_count"] + scalar["rejected_row_count"]
        ):
            raise V2SourceIntegrityError("V2 snapshot scalar provenance is inconsistent")
    previous_id: str | None = None
    previous_snapshot: str | None = None
    for expected_revision, row in enumerate(
        connection.execute("SELECT * FROM evidence_snapshot_activations ORDER BY revision"), start=1
    ):
        canonical_json = str(row["canonical_json"])
        try:
            core = json.loads(canonical_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise V2SourceIntegrityError("malformed V2 activation JSON") from exc
        projected = {
            "schema_version": "strathmark.evidence-snapshot-activation.v1",
            "revision": int(row["revision"]),
            "snapshot_digest": str(row["snapshot_digest"]),
            "previous_activation_id": row["previous_activation_id"],
            "previous_snapshot_digest": row["previous_snapshot_digest"],
            "activated_at": str(row["activated_at"]),
        }
        activation_id = hashlib.sha256(canonical_json.encode()).hexdigest()
        if (
            core != projected
            or _v2_canonical_json(core) != canonical_json
            or activation_id != str(row["activation_id"])
            or int(row["revision"]) != expected_revision
            or row["previous_activation_id"] != previous_id
            or row["previous_snapshot_digest"] != previous_snapshot
            or str(row["snapshot_digest"]) not in snapshots
        ):
            raise V2SourceIntegrityError("V2 evidence activation chain is inconsistent")
        previous_id = activation_id
        previous_snapshot = str(row["snapshot_digest"])


def _verify_shadow_and_settlements(connection: sqlite3.Connection, tables: set[str]) -> None:
    if not _LEDGER_CORE_GROUP.issubset(tables):
        return
    for row in connection.execute("SELECT request_hash FROM prediction_requests"):
        if not _SHA256.fullmatch(str(row[0])):
            raise V2SourceIntegrityError("V2 request hash is malformed")
    shadow_rows = (
        connection.execute("SELECT * FROM shadow_receipts ORDER BY ledger_request_id")
        if "shadow_receipts" in tables
        else ()
    )
    for row in shadow_rows:
        canonical_json = str(row["core_json"])
        try:
            core = json.loads(canonical_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise V2SourceIntegrityError("malformed V2 shadow receipt JSON") from exc
        if not isinstance(core, Mapping) or _v2_canonical_json(core) != canonical_json:
            raise V2SourceIntegrityError("noncanonical V2 shadow receipt JSON")
        request = connection.execute(
            "SELECT caller_id, request_id FROM prediction_requests WHERE ledger_request_id=?",
            (row["ledger_request_id"],),
        ).fetchone()
        if request is None or (
            str(request["caller_id"]) != str(row["caller_id"])
            or str(request["request_id"]) != str(row["request_id"])
            or core.get("consumer_id") != str(row["caller_id"])
            or core.get("request_id") != str(row["request_id"])
            or core.get("schema_version") != str(row["core_schema_version"])
        ):
            raise V2SourceIntegrityError("V2 shadow receipt identity is inconsistent")
        if not _SHA256.fullmatch(str(row["active_input_fingerprint"])):
            raise V2SourceIntegrityError("V2 shadow receipt fingerprint is malformed")
        active_input = core.get("active_input")
        request_projection = core.get("request_projection")
        ledger = core.get("ledger")
        if not all(
            isinstance(item, Mapping) for item in (active_input, request_projection, ledger)
        ):
            raise V2SourceIntegrityError("V2 shadow receipt proof sections are missing")
        assert isinstance(active_input, Mapping)
        assert isinstance(request_projection, Mapping)
        assert isinstance(ledger, Mapping)
        active_material = dict(active_input)
        active_fingerprint = str(active_material.pop("fingerprint", ""))
        projection_material = dict(request_projection)
        projection_fingerprint = str(projection_material.pop("fingerprint", ""))
        request_row = connection.execute(
            "SELECT request_hash, hash_algorithm FROM prediction_requests "
            "WHERE ledger_request_id=?",
            (row["ledger_request_id"],),
        ).fetchone()
        assert request_row is not None
        if (
            active_fingerprint != str(row["active_input_fingerprint"])
            or hashlib.sha256(_v2_canonical_json(active_material).encode()).hexdigest()
            != active_fingerprint
            or hashlib.sha256(_v2_canonical_json(projection_material).encode()).hexdigest()
            != projection_fingerprint
            or ledger.get("request_hash") != str(request_row["request_hash"])
            or ledger.get("hash_algorithm") != str(request_row["hash_algorithm"])
        ):
            raise V2SourceIntegrityError("V2 shadow receipt proof digests are inconsistent")
        predictions = core.get("predictions")
        child_ids = [
            str(item[0])
            for item in connection.execute(
                "SELECT prediction_id FROM ledger_predictions WHERE ledger_request_id=? ORDER BY ordinal",
                (row["ledger_request_id"],),
            )
        ]
        if (
            not isinstance(predictions, list)
            or [
                item.get("prediction_id") if isinstance(item, Mapping) else None
                for item in predictions
            ]
            != child_ids
        ):
            raise V2SourceIntegrityError("V2 shadow receipt prediction set is incomplete")
    histories: dict[str, list[tuple[int, str, str | None]]] = {}
    for row in connection.execute(
        "SELECT * FROM prediction_settlements ORDER BY prediction_id, revision"
    ):
        if not _SHA256.fullmatch(str(row["payload_hash"])):
            raise V2SourceIntegrityError("V2 settlement payload hash is malformed")
        histories.setdefault(str(row["prediction_id"]), []).append(
            (int(row["revision"]), str(row["settlement_id"]), row["supersedes_settlement_id"])
        )
        prediction = connection.execute(
            "SELECT competitor_id, event_code FROM ledger_predictions WHERE prediction_id=?",
            (row["prediction_id"],),
        ).fetchone()
        if prediction is None or (
            str(prediction["competitor_id"]) != str(row["competitor_id"])
            or str(prediction["event_code"]) != str(row["event_code"])
        ):
            raise V2SourceIntegrityError("V2 settlement identity is inconsistent")
    numeric_rows = (
        connection.execute(
            "SELECT * FROM numeric_settlement_revisions ORDER BY prediction_id, revision"
        )
        if "numeric_settlement_revisions" in tables
        else ()
    )
    for row in numeric_rows:
        histories.setdefault(str(row["prediction_id"]), []).append(
            (int(row["revision"]), str(row["revision_id"]), row["supersedes_revision_id"])
        )
        prediction = connection.execute(
            "SELECT competitor_id, event_code FROM ledger_predictions WHERE prediction_id=?",
            (row["prediction_id"],),
        ).fetchone()
        if prediction is None or (
            str(prediction["competitor_id"]) != str(row["competitor_id"])
            or str(prediction["event_code"]) != str(row["event_code"])
        ):
            raise V2SourceIntegrityError("V2 numeric settlement identity is inconsistent")
    for history in histories.values():
        history.sort(key=lambda item: item[0])
        previous: str | None = None
        for expected_revision, (revision, revision_id, supersedes) in enumerate(history, start=1):
            if revision != expected_revision or supersedes != previous:
                raise V2SourceIntegrityError("V2 settlement supersession chain is inconsistent")
            previous = revision_id
    outcome_rows = (
        connection.execute("SELECT * FROM numeric_outcome_revisions")
        if "numeric_outcome_revisions" in tables
        else ()
    )
    for row in outcome_rows:
        if not _SHA256.fullmatch(str(row["payload_hash"])):
            raise V2SourceIntegrityError("V2 numeric outcome payload hash is malformed")
        request = connection.execute(
            "SELECT caller_id FROM prediction_requests WHERE ledger_request_id=?",
            (row["ledger_request_id"],),
        ).fetchone()
        if request is None or str(request["caller_id"]) != str(row["caller_id"]):
            raise V2SourceIntegrityError("V2 numeric outcome caller is inconsistent")


def _primary_key_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [
        str(row[1])
        for row in sorted((row for row in rows if int(row[5]) > 0), key=lambda row: int(row[5]))
    ]


def _typed_value(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        return {"type": "real", "value": value}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, bytes):
        return {
            "type": "blob-sha256",
            "value": hashlib.sha256(value).hexdigest(),
            "bytes": len(value),
        }
    raise V2SourceIntegrityError(f"unsupported SQLite value type {type(value).__name__}")


def _build_row_manifests(
    connection: sqlite3.Connection,
    tables: set[str],
    *,
    profile_ids: tuple[str, ...] = (),
) -> tuple[Mapping[str, Any], ...]:
    summaries: list[Mapping[str, Any]] = []
    for table in sorted(tables - _EXCLUDED_OPERATIONAL):
        primary_key = _primary_key_columns(connection, table)
        if not primary_key:
            raise V2SourceSchemaError(f"trusted V2 table lacks deterministic primary key: {table}")
        order = ", ".join(f'"{name}"' for name in primary_key)
        row_count = 0
        ordered_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-v2-row-chain-v1",
                "table": table,
                "empty": True,
            }
        )
        for ordinal, row in enumerate(
            connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
        ):
            material = {
                "schema_version": "strathmark-v3-v2-typed-row-v1",
                "table": table,
                "values": [{"column": key, **_typed_value(row[key])} for key in row.keys()],
            }
            encoded = canonical_bytes(material).decode("utf-8")
            ordered_digest = canonical_digest(
                {
                    "schema_version": "strathmark-v3-v2-row-chain-node-v1",
                    "table": table,
                    "ordinal": ordinal,
                    "prior_digest": ordered_digest,
                    "row_digest": hashlib.sha256(encoded.encode()).hexdigest(),
                }
            )
            row_count += 1
        summaries.append(
            {
                "group": _GROUP_FOR_TABLE[table],
                "table": table,
                "profile_ids": list(profile_ids),
                "row_count": row_count,
                "ordered_row_manifest_digest": ordered_digest,
                "aggregation": "sha256-chain-v1",
            }
        )
    return tuple(summaries)


def _build_importable_evidence(
    connection: sqlite3.Connection, tables: set[str]
) -> tuple[tuple[str, str, int, str, str], ...]:
    """Project only pseudonymous numeric evidence; retain no legacy actor/free text."""

    if not _EVIDENCE_GROUP.issubset(tables):
        return ()
    projected: list[tuple[str, str, int, str, str]] = []
    rows = connection.execute(
        "SELECT snapshot_digest, ordinal, row_digest, competitor_id, event_code, "
        "time_seconds, species, diameter_mm, quality, competition_id, heat_id, result_date "
        "FROM evidence_snapshot_rows ORDER BY snapshot_digest, ordinal"
    ).fetchall()
    for ordinal, row in enumerate(rows):
        value = {
            "schema_version": "strathmark-v3-imported-v2-evidence-row-v1",
            "source_snapshot_digest": str(row["snapshot_digest"]),
            "source_ordinal": int(row["ordinal"]),
            "source_row_digest": str(row["row_digest"]),
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
        encoded = canonical_bytes(value).decode("utf-8")
        projected.append(
            (
                "evidence-snapshot",
                "evidence_snapshot_rows",
                ordinal,
                hashlib.sha256(encoded.encode()).hexdigest(),
                encoded,
            )
        )
    return tuple(projected)


def _available_chain_tips(connection: sqlite3.Connection, tables: set[str]) -> Mapping[str, Any]:
    tips: dict[str, Any] = {}
    if _EVIDENCE_GROUP.issubset(tables):
        row = connection.execute(
            "SELECT activation_id, revision, snapshot_digest FROM evidence_snapshot_activations "
            "ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        tips["evidence_activation"] = (
            None
            if row is None
            else {
                "activation_id": str(row[0]),
                "revision": int(row[1]),
                "snapshot_digest": str(row[2]),
            }
        )
    if _LEDGER_GROUP.issubset(tables):
        tips["ledger"] = {
            "request_count": int(
                connection.execute("SELECT COUNT(*) FROM prediction_requests").fetchone()[0]
            ),
            "receipt_count": int(
                connection.execute("SELECT COUNT(*) FROM shadow_receipts").fetchone()[0]
            ),
            "outcome_count": int(
                connection.execute("SELECT COUNT(*) FROM numeric_outcome_revisions").fetchone()[0]
            ),
            "cryptographic_chain": None,
        }
    return tips


def _import_result_value(snapshot: _SourceSnapshot) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-v2-import-result-v1",
        "import_id": f"v2import:{snapshot.source_tip_digest}",
        "source_tip_digest": snapshot.source_tip_digest,
        "source_catalog_digest": snapshot.catalog_digest,
        "profile_ids": list(snapshot.profile_ids),
        "imported_row_count": len(snapshot.importable_rows),
        "cutoff": snapshot.cutoff,
        "eligible": False,
        "limitations": list(_LIMITATIONS),
    }


def _single_event_set_digest(event: EventEnvelope) -> str:
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-event-set-v1",
            "events": [
                {
                    "global_sequence": event.global_sequence,
                    "event_id": str(event.event_id),
                    "event_digest": event.event_digest,
                }
            ],
        }
    )


def _persist_snapshot(
    connection: sqlite3.Connection,
    snapshot: _SourceSnapshot,
    *,
    source: Path,
    expected_source_state: tuple[
        tuple[str, int, int, int, int] | tuple[str, None, None, None, None], ...
    ],
) -> sqlite3.Row | None:
    import_id = f"v2import:{snapshot.source_tip_digest}"
    aggregate_id = StableIdentifier("system:v2-history")
    actor_id = StableIdentifier("actor:v2-readonly-import")
    command_id = IdempotencyKey(f"command:{snapshot.source_tip_digest}")
    command_payload = InlinePayload.from_value(snapshot.manifest)
    payload_json = canonical_bytes(snapshot.manifest).decode("utf-8")
    result_value = _import_result_value(snapshot)
    result_json = canonical_bytes(result_value).decode("utf-8")
    result_digest = canonical_digest(result_value)
    with immediate_transaction(connection):
        concurrent = connection.execute(
            "SELECT * FROM v3_historical_imports WHERE source_tip_digest=?",
            (snapshot.source_tip_digest,),
        ).fetchone()
        if concurrent is not None:
            if _source_file_state(source) != expected_source_state:
                raise V2SourceChangedError("V2 source changed before exact retry resolution")
            return concurrent
        previous_global_row = connection.execute(
            "SELECT global_sequence, event_digest FROM v3_events "
            "ORDER BY global_sequence DESC LIMIT 1"
        ).fetchone()
        global_sequence = 1 if previous_global_row is None else int(previous_global_row[0]) + 1
        prior_global = "0" * 64 if previous_global_row is None else str(previous_global_row[1])
        previous_aggregate_row = connection.execute(
            "SELECT aggregate_version, event_digest FROM v3_events "
            "WHERE aggregate_kind=? AND aggregate_id=? ORDER BY aggregate_version DESC LIMIT 1",
            (AggregateKind.SYSTEM.value, str(aggregate_id)),
        ).fetchone()
        aggregate_version = (
            1 if previous_aggregate_row is None else int(previous_aggregate_row[0]) + 1
        )
        prior_aggregate = (
            "0" * 64 if previous_aggregate_row is None else str(previous_aggregate_row[1])
        )
        command = CommandEnvelope(
            kind=CommandKind.IMPORT_HISTORY,
            command_id=command_id,
            target_aggregate=aggregate_id,
            expected_versions=((str(aggregate_id), aggregate_version - 1),),
            actor_id=actor_id,
            payload=command_payload,
        )
        event = EventEnvelope.create(
            event_id=deterministic_identifier(
                "event",
                {"kind": EventKind.HISTORY_IMPORTED.value, "tip": snapshot.source_tip_digest},
            ),
            kind=EventKind.HISTORY_IMPORTED,
            aggregate_kind=AggregateKind.SYSTEM,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            global_sequence=global_sequence,
            prior_global_digest=prior_global,
            prior_aggregate_digest=prior_aggregate,
            occurred_at_utc=snapshot.cutoff,
            monotonic_elapsed_ms=0,
            command=command,
        )
        envelope_json = canonical_bytes(event.to_dict()).decode("utf-8")
        command_digest = canonical_digest(command.to_dict())
        event_set_digest = _single_event_set_digest(event)
        connection.execute(
            "INSERT INTO v3_events(global_sequence, event_id, aggregate_kind, aggregate_id, "
            "aggregate_version, event_kind, envelope_json, event_digest, prior_global_digest, "
            "prior_aggregate_digest, occurred_at_utc, command_id, source_import_id, "
            "training_eligible) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                event.global_sequence,
                str(event.event_id),
                event.aggregate_kind.value,
                str(event.aggregate_id),
                event.aggregate_version,
                event.kind.value,
                envelope_json,
                event.event_digest,
                event.prior_global_digest,
                event.prior_aggregate_digest,
                event.occurred_at_utc,
                str(event.command.command_id),
                import_id,
            ),
        )
        head_write = connection.execute(
            "INSERT INTO v3_aggregate_heads(aggregate_kind, aggregate_id, aggregate_version, "
            "event_digest) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(aggregate_kind, aggregate_id) DO UPDATE SET "
            "aggregate_version=excluded.aggregate_version, event_digest=excluded.event_digest "
            "WHERE v3_aggregate_heads.aggregate_version=excluded.aggregate_version-1 "
            "AND v3_aggregate_heads.event_digest=?",
            (
                event.aggregate_kind.value,
                str(event.aggregate_id),
                event.aggregate_version,
                event.event_digest,
                event.prior_aggregate_digest,
            ),
        )
        if head_write.rowcount != 1:
            raise V2SourceIntegrityError("V3 aggregate head conflicts with imported history event")
        connection.execute(
            "INSERT INTO v3_idempotency_records(principal_id, idempotency_key, command_digest, "
            "result_schema_version, result_json, result_digest, first_global_sequence, "
            "last_global_sequence, event_set_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(actor_id),
                str(command_id),
                command_digest,
                "strathmark-v3-v2-import-result-v1",
                result_json,
                result_digest,
                event.global_sequence,
                event.global_sequence,
                event_set_digest,
                snapshot.cutoff,
            ),
        )
        connection.execute(
            "INSERT INTO v3_historical_imports(import_id, source_profile_json, "
            "source_catalog_digest, source_tip_digest, source_cutoff, source_manifest_json, "
            "imported_row_count, imported_at, cutover_manifest_digest, eligible) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
            (
                import_id,
                canonical_bytes(list(snapshot.profile_ids)).decode(),
                snapshot.catalog_digest,
                snapshot.source_tip_digest,
                snapshot.cutoff,
                payload_json,
                len(snapshot.importable_rows),
                snapshot.cutoff,
            ),
        )
        connection.executemany(
            "INSERT INTO v3_historical_import_rows(import_id, source_group, source_table, "
            "ordinal, row_digest, canonical_json, eligible) VALUES (?, ?, ?, ?, ?, ?, 0)",
            [
                (import_id, group, table, ordinal, digest, row_json)
                for group, table, ordinal, digest, row_json in snapshot.importable_rows
            ],
        )
        _before_import_commit_check(source)
        if _source_file_state(source) != expected_source_state:
            raise V2SourceChangedError("V2 source changed before V3 import commit")
    return None


def _before_import_commit_check(source: Path) -> None:
    """Deterministic fault-injection seam; production behavior is intentionally inert."""

    del source


def _verify_existing_import(
    connection: sqlite3.Connection, row: sqlite3.Row, snapshot: _SourceSnapshot
) -> None:
    expected = {
        "import_id": f"v2import:{snapshot.source_tip_digest}",
        "source_profile_json": canonical_bytes(list(snapshot.profile_ids)).decode(),
        "source_catalog_digest": snapshot.catalog_digest,
        "source_cutoff": snapshot.cutoff,
        "source_manifest_json": canonical_bytes(snapshot.manifest).decode(),
        "imported_row_count": len(snapshot.importable_rows),
        "eligible": 0,
        "cutover_manifest_digest": None,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise V2SourceIntegrityError("existing V3 historical import conflicts with source tip")
    event_row = connection.execute(
        "SELECT global_sequence, event_id, aggregate_kind, aggregate_id, aggregate_version, "
        "event_kind, envelope_json, event_digest, prior_global_digest, "
        "prior_aggregate_digest, occurred_at_utc, command_id, source_import_id, "
        "training_eligible FROM v3_events WHERE source_import_id=?",
        (expected["import_id"],),
    ).fetchone()
    if event_row is None:
        raise V2SourceIntegrityError("existing V3 historical import event is missing")
    try:
        event = EventEnvelope.from_dict(json.loads(str(event_row[6])))
    except Exception as exc:
        raise V2SourceIntegrityError("existing V3 historical import event is corrupt") from exc
    if (
        event.kind is not EventKind.HISTORY_IMPORTED
        or event.aggregate_kind is not AggregateKind.SYSTEM
        or str(event.aggregate_id) != "system:v2-history"
        or event.command.kind is not CommandKind.IMPORT_HISTORY
        or str(event.command.command_id) != f"command:{snapshot.source_tip_digest}"
        or str(event.command.actor_id) != "actor:v2-readonly-import"
        or event.command.target_aggregate != event.aggregate_id
        or event.command.expected_versions
        != ((str(event.aggregate_id), event.aggregate_version - 1),)
        or not isinstance(event.command.payload, InlinePayload)
        or event.command.payload.to_value() != snapshot.manifest
    ):
        raise V2SourceIntegrityError("existing V3 historical import event does not bind source tip")
    persisted_event = (
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
        expected["import_id"],
        0,
    )
    if tuple(event_row) != persisted_event:
        raise V2SourceIntegrityError("existing V3 historical import event projection is corrupt")

    latest_event = connection.execute(
        "SELECT aggregate_version, event_digest FROM v3_events "
        "WHERE aggregate_kind=? AND aggregate_id=? ORDER BY aggregate_version DESC LIMIT 1",
        (AggregateKind.SYSTEM.value, "system:v2-history"),
    ).fetchone()
    aggregate_head = connection.execute(
        "SELECT aggregate_version, event_digest FROM v3_aggregate_heads "
        "WHERE aggregate_kind=? AND aggregate_id=?",
        (AggregateKind.SYSTEM.value, "system:v2-history"),
    ).fetchone()
    if (
        latest_event is None
        or aggregate_head is None
        or tuple(aggregate_head) != tuple(latest_event)
    ):
        raise V2SourceIntegrityError("existing V3 aggregate head is missing or corrupt")

    result_value = _import_result_value(snapshot)
    result_json = canonical_bytes(result_value).decode("utf-8")
    idempotency = connection.execute(
        "SELECT command_digest, result_schema_version, result_json, result_digest, "
        "first_global_sequence, last_global_sequence, event_set_digest "
        "FROM v3_idempotency_records WHERE principal_id=? AND idempotency_key=?",
        ("actor:v2-readonly-import", f"command:{snapshot.source_tip_digest}"),
    ).fetchone()
    expected_idempotency = (
        canonical_digest(event.command.to_dict()),
        "strathmark-v3-v2-import-result-v1",
        result_json,
        canonical_digest(result_value),
        event.global_sequence,
        event.global_sequence,
        _single_event_set_digest(event),
    )
    if idempotency is None or tuple(idempotency) != expected_idempotency:
        raise V2SourceIntegrityError("existing V3 import idempotency result is missing or corrupt")
    persisted_rows = connection.execute(
        "SELECT source_group, source_table, ordinal, row_digest, canonical_json, eligible "
        "FROM v3_historical_import_rows WHERE import_id=? ORDER BY source_table, ordinal",
        (expected["import_id"],),
    ).fetchall()
    observed_rows = tuple(
        (str(item[0]), str(item[1]), int(item[2]), str(item[3]), str(item[4]))
        for item in persisted_rows
    )
    if (
        observed_rows != snapshot.importable_rows
        or any(int(item[5]) != 0 for item in persisted_rows)
        or any(
            hashlib.sha256(str(item[4]).encode()).hexdigest() != str(item[3])
            for item in persisted_rows
        )
    ):
        raise V2SourceIntegrityError("existing V3 imported evidence rows are corrupt")


def _result_from_snapshot(snapshot: _SourceSnapshot) -> V2ImportResult:
    return V2ImportResult(
        import_id=f"v2import:{snapshot.source_tip_digest}",
        source_tip_digest=snapshot.source_tip_digest,
        source_catalog_digest=snapshot.catalog_digest,
        profile_ids=snapshot.profile_ids,
        imported_row_count=len(snapshot.importable_rows),
        cutoff=snapshot.cutoff,
        eligible=False,
        limitations=_LIMITATIONS,
    )


__all__ = [
    "V2ImportError",
    "V2ImportPathConflictError",
    "V2ImportResult",
    "V2SourceChangedError",
    "V2SourceIntegrityError",
    "V2SourceSchemaError",
    "import_v2_snapshot",
    "open_v2_readonly",
]
