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
EXPECTED_SCHEMA_DIGEST = "be612a3d1f5e7d19e1f9c5ad6f556c8db005927112301f98e9cc99c725d7c85b"


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

    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
        ORDER BY type, name
        """
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
