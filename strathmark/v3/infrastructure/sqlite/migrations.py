"""Forward-only, checksum-pinned SQLite migrations for V3."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.evidence import require_utc_milliseconds


class MigrationError(RuntimeError):
    """Base class for a V3 migration failure."""


class MigrationChecksumError(MigrationError):
    """Checked-in or already-applied migration bytes drifted."""


class MigrationStateError(MigrationError):
    """The database contains a partial or otherwise inconsistent schema state."""


class UnsupportedSchemaError(MigrationError):
    """The database schema is newer than this runtime or has an unknown version."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable checked-in migration."""

    version: int
    name: str
    checksum: str
    sql: str


_PINNED_CHECKSUMS: Final[dict[str, str]] = {
    "0001_event_authority.sql": "329a03d7bba29d5bcd37c5bb0a3711192b03c47d58c47f972738730e5b0db15f",
    "0002_historical_v2_import.sql": "421e37969130a7f6bfd4ac973f2a78d5da3cecbd12b364238586803791b363ab",
    "0003_ingress_epochs.sql": "dfffdc51298cd001d18b50a605ded4463ea9365133b7cc749ec6c1a010c53301",
    "0004_recovery_storage.sql": "99cc6226024380bc26ca98e7376f118ac188a4b31faa46424d87a5d6b57cfaee",
    "0005_durable_jobs.sql": "8df9584e7961afb96eb7d87e28d4180864ede7bb195d96a1535b9304823cbcb4",
    "0006_signed_historical_cutover.sql": "2694b757596ccce399b67057bfce4d93fed42819dfece0d01573f62f658e5121",
    "0007_provider_execution_audit.sql": "1cc0aa52c2625b58d3a2975d3913286ab637358fa104d8c9c3c1ec11d8aa6fa5",
    "0008_field_assembly_projection.sql": "347d5ebd91dc8efea7944b3289c4627360352c7f8f71968739f8154e0cd0c1a9",
    "0009_approval_projection.sql": "7fca0aabe3f26f4d422523179779eebe0407c8924310388e27eba1e0cdaadf88",
    "0010_field_capacity_and_disagreement_authority.sql": "610f8c85f421208982ba6af6796efa80d728a979cfec273db2685bcce1b2fc26",
    "0011_rolling_card_publications.sql": "b7ffa42aa46b86d051fb074c2b7291e89a01fec3401bceba85020e77b7d53089",
    "0012_rolling_restart_checkpoints.sql": "b11d1629fce282456d8bab3394037001bd4587c4274e37229cafa5154a59c942",
    "0013_rolling_delta_and_job_spec_authority.sql": "3ff5f1ba0465752d0e85930394b1763c9fa48dc5c71179e1813b20116bed01f5",
    "0014_manual_action_requirements.sql": "ba6933de10edfe427412eb1404e5cb66f316880af244fce4046ac8d1e3e3c2be",
    "0015_authenticated_hot_path_checkpoints.sql": "f2f1af7d1688b03190e58152b346744090a1971ba24b5cbfcd7d3c15bd33cba7",
    "0016_model_status_and_projection_restore.sql": "9eada7cfe4271334632006e6c45d57040a767f0a31b757d670d34ce6b907a082",
    "0017_expected_time_override_state.sql": "c456188b146bc3c67b0bfe9fe7a57a087761392516259f5c0976794e8617c2ac",
    "0018_pre_field_forecasts.sql": "3933663689c7a70557ab8bc54151fb54fde300867f23098bae0bdefd86f9ca37",
}
_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<label>[a-z0-9_]+)\.sql$")
_MIGRATION_ROOT = Path(__file__).resolve().parents[2] / "migrations"


def _load_default_migrations() -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    discovered = {path.name: path for path in _MIGRATION_ROOT.glob("*.sql")}
    if set(discovered) != set(_PINNED_CHECKSUMS):
        raise MigrationChecksumError("checked-in migration set differs from the pinned manifest")
    for filename in sorted(discovered):
        match = _MIGRATION_NAME.fullmatch(filename)
        if match is None:
            raise MigrationStateError(f"malformed migration filename: {filename}")
        raw = discovered[filename].read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum != _PINNED_CHECKSUMS[filename]:
            raise MigrationChecksumError(f"checked-in migration checksum drift: {filename}")
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=filename,
                checksum=checksum,
                sql=raw.decode("utf-8"),
            )
        )
    _validate_catalog(migrations)
    return tuple(migrations)


def _validate_catalog(migrations: Iterable[Migration]) -> None:
    material = tuple(migrations)
    expected = tuple(range(1, len(material) + 1))
    actual = tuple(item.version for item in material)
    if actual != expected:
        raise MigrationStateError("migration versions must be consecutive and start at one")
    if len({item.name for item in material}) != len(material):
        raise MigrationStateError("migration names must be unique")
    for item in material:
        if hashlib.sha256(item.sql.encode("utf-8")).hexdigest() != item.checksum:
            raise MigrationChecksumError(f"migration bytes do not match checksum: {item.name}")


DEFAULT_MIGRATIONS = _load_default_migrations()


_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS v3_schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at TEXT NOT NULL
)
"""
_METADATA_COLUMNS = (
    ("version", "INTEGER", 0, 1),
    ("name", "TEXT", 1, 0),
    ("checksum", "TEXT", 1, 0),
    ("applied_at", "TEXT", 1, 0),
)
EXPECTED_SCHEMA_DIGEST = "e2b9952a6d017df2a1239a698213cbfd73bab02aa5d7b9505e5652fa8f35d443"


def _ensure_metadata_table(connection: sqlite3.Connection) -> None:
    connection.execute(_METADATA_SQL)
    rows = connection.execute("PRAGMA table_info(v3_schema_migrations)").fetchall()
    signature = tuple((str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5])) for row in rows)
    if signature != _METADATA_COLUMNS:
        raise MigrationStateError("v3_schema_migrations has an unsupported shape")
    catalog_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='v3_schema_migrations'"
    ).fetchone()
    expected_sql = " ".join(_METADATA_SQL.replace(" IF NOT EXISTS", "").split()).rstrip(";")
    observed_sql = "" if catalog_row is None else " ".join(str(catalog_row[0]).split()).rstrip(";")
    if observed_sql != expected_sql:
        raise MigrationStateError("migration metadata catalog semantics are unsupported")


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Return the latest applied schema version, or zero before initialization."""

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v3_schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM v3_schema_migrations"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise MigrationStateError("migration metadata cannot be read") from exc
    return int(row[0])


def migrate_connection(
    connection: sqlite3.Connection,
    *,
    migrations: tuple[Migration, ...] = DEFAULT_MIGRATIONS,
    applied_at: str = "1970-01-01T00:00:00.000Z",
) -> int:
    """Verify history and transactionally apply all missing forward migrations."""

    _validate_catalog(migrations)
    try:
        require_utc_milliseconds(applied_at)
    except Exception as exc:
        raise MigrationStateError("applied_at must be a valid canonical UTC instant") from exc
    if connection.in_transaction:
        raise MigrationStateError("migrations require an idle SQLite connection")
    _ensure_metadata_table(connection)
    applied = connection.execute(
        "SELECT version, name, checksum FROM v3_schema_migrations ORDER BY version"
    ).fetchall()
    available = {item.version: item for item in migrations}
    if applied and int(applied[-1][0]) > len(migrations):
        raise UnsupportedSchemaError("database schema is newer than this V3 runtime")
    expected_applied_versions = tuple(range(1, len(applied) + 1))
    if tuple(int(row[0]) for row in applied) != expected_applied_versions:
        raise MigrationStateError("applied migration history contains a version gap")
    for row in applied:
        version = int(row[0])
        candidate = available[version]
        if str(row[1]) != candidate.name or str(row[2]) != candidate.checksum:
            raise MigrationChecksumError(f"applied migration metadata drifted at version {version}")

    applied_count = 0
    for migration in migrations[len(applied) :]:
        _apply_migration(connection, migration, applied_at=applied_at)
        applied_count += 1
    if migrations == DEFAULT_MIGRATIONS:
        observed_digest = canonical_schema_digest(connection)
        if observed_digest != EXPECTED_SCHEMA_DIGEST:
            raise MigrationStateError("V3 catalog does not match the pinned canonical schema")
    return applied_count


def _apply_migration(
    connection: sqlite3.Connection, migration: Migration, *, applied_at: str
) -> None:
    name = migration.name.replace("'", "''")
    checksum = migration.checksum.replace("'", "''")
    timestamp = applied_at.replace("'", "''")
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql.rstrip()}\n"
        "INSERT INTO v3_schema_migrations(version, name, checksum, applied_at) "
        f"VALUES ({migration.version}, '{name}', '{checksum}', '{timestamp}');\n"
        "COMMIT;"
    )
    try:
        connection.executescript(script)
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise MigrationStateError(
            f"migration {migration.name} failed without partial application"
        ) from exc


def canonical_schema_digest(connection: sqlite3.Connection) -> str:
    """Digest normalized catalog semantics, excluding SQLite's internal objects."""

    rows = connection.execute("""
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
        ORDER BY type, name
        """).fetchall()
    catalog = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": " ".join(str(row[3]).split()),
        }
        for row in rows
    ]
    return canonical_digest({"schema": catalog})


__all__ = [
    "DEFAULT_MIGRATIONS",
    "EXPECTED_SCHEMA_DIGEST",
    "Migration",
    "MigrationChecksumError",
    "MigrationError",
    "MigrationStateError",
    "UnsupportedSchemaError",
    "canonical_schema_digest",
    "current_schema_version",
    "migrate_connection",
]
