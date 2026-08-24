from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import strathmark.v3.infrastructure.sqlite.connection as sqlite_connection
import strathmark.v3.infrastructure.sqlite.migrations as migration_module
from strathmark.v3.infrastructure.sqlite.connection import (
    DEFAULT_CONNECTION_POLICY,
    SQLiteConnectionPolicy,
    SQLiteDeadline,
    SQLiteDeadlineExceeded,
    SQLitePolicyError,
    bounded_checkpoint,
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import (
    DEFAULT_MIGRATIONS,
    EXPECTED_SCHEMA_DIGEST,
    Migration,
    MigrationChecksumError,
    MigrationStateError,
    UnsupportedSchemaError,
    canonical_schema_digest,
    current_schema_version,
    migrate_connection,
)


def test_connection_policy_is_explicit_and_write_transactions_are_short(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v3" / "authority.sqlite3"
    with open_v3_connection(database) as connection:
        observed = {
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
            "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
            "trusted_schema": connection.execute("PRAGMA trusted_schema").fetchone()[0],
            "wal_autocheckpoint": connection.execute(
                "PRAGMA wal_autocheckpoint"
            ).fetchone()[0],
        }
        assert observed == {
            "journal_mode": "wal",
            "foreign_keys": 1,
            "synchronous": 2,
            "busy_timeout": DEFAULT_CONNECTION_POLICY.busy_timeout_ms,
            "trusted_schema": 0,
            "wal_autocheckpoint": DEFAULT_CONNECTION_POLICY.wal_autocheckpoint_pages,
        }
        with immediate_transaction(connection):
            connection.execute("CREATE TABLE committed(value INTEGER NOT NULL)")
        with pytest.raises(RuntimeError, match="rollback"):
            with immediate_transaction(connection):
                connection.execute("INSERT INTO committed VALUES (1)")
                raise RuntimeError("rollback")
        assert connection.execute("SELECT COUNT(*) FROM committed").fetchone()[0] == 0


def test_deadline_and_checkpoint_are_bounded(tmp_path: Path) -> None:
    database = tmp_path / "v3" / "authority.sqlite3"
    with open_v3_connection(database) as connection:
        deadline = SQLiteDeadline(timeout_seconds=1)
        deadline.cancel()
        with pytest.raises(SQLiteDeadlineExceeded):
            deadline.raise_if_expired()
        result = bounded_checkpoint(connection)
        assert result.mode == "PASSIVE"
        assert result.busy in (0, 1)
        assert result.log_frames >= 0
        assert result.checkpointed_frames >= 0


def test_migrations_are_repeatable_checksum_pinned_and_canonical(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh" / "authority.sqlite3"
    prior = tmp_path / "prior" / "authority.sqlite3"
    with open_v3_connection(fresh) as connection:
        assert migrate_connection(connection) == len(DEFAULT_MIGRATIONS)
        fresh_digest = canonical_schema_digest(connection)
        assert migrate_connection(connection) == 0
        assert current_schema_version(connection) == len(DEFAULT_MIGRATIONS)
    with open_v3_connection(prior) as connection:
        assert migrate_connection(connection, migrations=DEFAULT_MIGRATIONS[:1]) == 1
        assert migrate_connection(connection) == len(DEFAULT_MIGRATIONS) - 1
        assert canonical_schema_digest(connection) == fresh_digest
        assert [
            row[1]
            for row in connection.execute("PRAGMA table_info(v3_aggregate_heads)")
        ] == [
            "aggregate_kind",
            "aggregate_id",
            "aggregate_version",
            "event_digest",
        ]


def test_rolling_card_publication_schema_is_forward_only_and_restart_safe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rolling" / "authority.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection, migrations=DEFAULT_MIGRATIONS[:12])
        assert current_schema_version(connection) == 12
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'v3_rolling_%'"
            )
        }
        assert tables == {
            "v3_rolling_council_authorities",
            "v3_rolling_card_publications",
            "v3_rolling_card_status_history",
            "v3_rolling_card_current",
            "v3_rolling_epoch_closures",
            "v3_rolling_reaction_obligations",
            "v3_rolling_reaction_completions",
            "v3_rolling_restart_checkpoints",
            "v3_rolling_restart_tip",
            "v3_rolling_reaction_cursor",
        }


def test_rolling_delta_and_job_spec_schema_is_forward_only_and_immutable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rolling-delta" / "authority.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection)
        assert current_schema_version(connection) == 13
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('v3_job_specs','v3_rolling_restart_deltas',"
                "'v3_rolling_restart_delta_tip')"
            )
        }
        assert tables == {
            "v3_job_specs",
            "v3_rolling_restart_deltas",
            "v3_rolling_restart_delta_tip",
        }
        for table in ("v3_job_specs", "v3_rolling_restart_deltas"):
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                (f"{table}_no_update",),
            ).fetchone()
            assert row is not None
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                (f"{table}_no_delete",),
            ).fetchone()
            assert row is not None


def test_migrations_reject_drift_partial_state_and_future_schema(
    tmp_path: Path,
) -> None:
    drift = tmp_path / "drift.sqlite3"
    with open_v3_connection(drift) as connection:
        migrate_connection(connection)
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE v3_schema_migrations SET checksum = ? WHERE version = 1",
            ("0" * 64,),
        )
        connection.execute("PRAGMA writable_schema = OFF")
        with pytest.raises(MigrationChecksumError):
            migrate_connection(connection)

    partial = tmp_path / "partial.sqlite3"
    with open_v3_connection(partial) as connection:
        connection.execute("CREATE TABLE v3_events(unexpected INTEGER)")
        with pytest.raises(MigrationStateError):
            migrate_connection(connection)

    future = tmp_path / "future.sqlite3"
    with open_v3_connection(future) as connection:
        migrate_connection(connection)
        connection.execute(
            "INSERT INTO v3_schema_migrations(version, name, checksum, applied_at) "
            "VALUES (?, 'future', ?, '2026-08-22T00:00:00.000Z')",
            (len(DEFAULT_MIGRATIONS) + 1, "f" * 64),
        )
        with pytest.raises(UnsupportedSchemaError):
            migrate_connection(connection)


def test_read_only_connection_cannot_mutate(tmp_path: Path) -> None:
    database = tmp_path / "authority.sqlite3"
    with open_v3_connection(database) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER)")
    with open_v3_connection(database, read_only=True) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO sample VALUES (1)")


def test_connection_policy_rejects_memory_bool_and_coerced_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for target in (":memory:", "file::memory:?cache=shared"):
        with pytest.raises(SQLitePolicyError, match="in-memory"):
            open_v3_connection(target)
    assert not (tmp_path / ":memory:").exists()
    with pytest.raises(SQLitePolicyError, match="read_only"):
        open_v3_connection(tmp_path / "db.sqlite3", read_only="yes")  # type: ignore[arg-type]
    with pytest.raises(SQLitePolicyError, match="numeric value"):
        SQLiteDeadline(timeout_seconds=True)  # type: ignore[arg-type]
    with pytest.raises(SQLitePolicyError, match="busy timeout"):
        SQLiteConnectionPolicy(busy_timeout_ms=True)  # type: ignore[arg-type]


def test_migrations_reject_superficial_metadata_and_catalog_tamper(
    tmp_path: Path,
) -> None:
    wrong_shape = tmp_path / "wrong-shape.sqlite3"
    with open_v3_connection(wrong_shape) as connection:
        connection.execute(
            "CREATE TABLE v3_schema_migrations(version INTEGER PRIMARY KEY)"
        )
        with pytest.raises(MigrationStateError, match="shape"):
            migrate_connection(connection)

    metadata = tmp_path / "metadata.sqlite3"
    with open_v3_connection(metadata) as connection:
        connection.execute(
            "CREATE TABLE v3_schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        with pytest.raises(MigrationStateError, match="metadata"):
            migrate_connection(connection)

    catalog = tmp_path / "catalog.sqlite3"
    with open_v3_connection(catalog) as connection:
        migrate_connection(connection)
        connection.execute("CREATE TABLE unauthorized_projection(value TEXT)")
        with pytest.raises(MigrationStateError, match="catalog"):
            migrate_connection(connection)


def test_migration_object_collision_rolls_back_all_trusted_objects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "collision.sqlite3"
    with open_v3_connection(database) as connection:
        connection.execute("CREATE TABLE decoy(value INTEGER)")
        connection.execute(
            "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON decoy BEGIN SELECT 1; END"
        )
        with pytest.raises(MigrationStateError):
            migrate_connection(connection)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v3_events'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "unknown"},
        {"journal_mode": "DELETE"},
        {"synchronous": "NORMAL"},
        {"busy_timeout_ms": 0},
        {"wal_autocheckpoint_pages": True},
        {"progress_opcode_interval": 100_001},
        {"checkpoint_mode": "TRUNCATE"},
    ],
)
def test_connection_policy_rejects_every_unversioned_or_unbounded_choice(
    changes: dict[str, object],
) -> None:
    with pytest.raises(SQLitePolicyError):
        SQLiteConnectionPolicy(**changes)  # type: ignore[arg-type]


def test_deadline_progress_remaining_and_expiration_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = SQLiteDeadline(timeout_seconds=1)
    assert 1 <= deadline.remaining_milliseconds() <= 1000
    assert deadline.progress_handler() == 0
    for invalid in (0, -1, 61, float("nan")):
        with pytest.raises(SQLitePolicyError, match="finite"):
            SQLiteDeadline(timeout_seconds=invalid)
    moments = iter((10.0, 11.5))
    monkeypatch.setattr(sqlite_connection.time, "monotonic", lambda: next(moments))
    expired = SQLiteDeadline(timeout_seconds=1)
    with pytest.raises(SQLiteDeadlineExceeded):
        expired.raise_if_expired()


def test_connection_rejects_bad_types_missing_read_target_and_closes_on_policy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SQLitePolicyError, match="policy"):
        open_v3_connection(tmp_path / "db.sqlite3", policy=object())  # type: ignore[arg-type]
    with pytest.raises(SQLitePolicyError, match="filesystem"):
        open_v3_connection(True)  # type: ignore[arg-type]
    with pytest.raises(SQLitePolicyError, match="deadline"):
        open_v3_connection(tmp_path / "db.sqlite3", deadline=object())  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        open_v3_connection(tmp_path / "missing.sqlite3", read_only=True)

    class RefusingConnection:
        row_factory = None
        closed = False

        def execute(self, sql: str):
            self.sql = sql
            return self

        def fetchone(self):
            return ("delete",)

        def close(self) -> None:
            self.closed = True

    refused = RefusingConnection()
    monkeypatch.setattr(
        sqlite_connection.sqlite3, "connect", lambda *args, **kwargs: refused
    )
    with pytest.raises(SQLitePolicyError, match="refused"):
        open_v3_connection(tmp_path / "refused.sqlite3")
    assert refused.closed is True


def test_deadline_connection_nested_transaction_and_checkpoint_guards(
    tmp_path: Path,
) -> None:
    deadline = SQLiteDeadline(timeout_seconds=1)
    with open_v3_connection(
        tmp_path / "deadline.sqlite3", deadline=deadline
    ) as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] <= 1000
        connection.execute("BEGIN")
        with pytest.raises(SQLitePolicyError, match="nested"):
            with immediate_transaction(connection):
                pass
        with pytest.raises(SQLitePolicyError, match="checkpoint"):
            bounded_checkpoint(connection)
        connection.rollback()

    class BadCheckpoint:
        in_transaction = False

        @staticmethod
        def execute(_sql: str):
            return BadCheckpoint()

        @staticmethod
        def fetchone():
            return None

    with pytest.raises(SQLitePolicyError, match="malformed"):
        bounded_checkpoint(BadCheckpoint())  # type: ignore[arg-type]


def test_migration_catalog_loader_and_value_validation_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migration_module, "_MIGRATION_ROOT", tmp_path)
    with pytest.raises(MigrationChecksumError, match="set"):
        migration_module._load_default_migrations()

    malformed = tmp_path / "bad.sql"
    malformed.write_text("SELECT 1;", encoding="utf-8")
    digest = hashlib.sha256(malformed.read_bytes()).hexdigest()
    monkeypatch.setattr(migration_module, "_PINNED_CHECKSUMS", {"bad.sql": digest})
    with pytest.raises(MigrationStateError, match="filename"):
        migration_module._load_default_migrations()

    malformed.rename(tmp_path / "0001_valid.sql")
    monkeypatch.setattr(
        migration_module, "_PINNED_CHECKSUMS", {"0001_valid.sql": "0" * 64}
    )
    with pytest.raises(MigrationChecksumError, match="checksum drift"):
        migration_module._load_default_migrations()

    sql = "SELECT 1;"
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    valid = Migration(1, "0001_valid.sql", checksum, sql)
    with pytest.raises(MigrationStateError, match="consecutive"):
        migration_module._validate_catalog(
            (Migration(2, "0002_gap.sql", checksum, sql),)
        )
    with pytest.raises(MigrationStateError, match="unique"):
        migration_module._validate_catalog(
            (valid, Migration(2, valid.name, checksum, sql))
        )
    with pytest.raises(MigrationChecksumError, match="bytes"):
        migration_module._validate_catalog((Migration(1, valid.name, "f" * 64, sql),))


def test_migration_timestamp_idle_state_version_gap_and_schema_digest_guards(
    tmp_path: Path,
) -> None:
    database = tmp_path / "guards.sqlite3"
    with open_v3_connection(database) as connection:
        assert current_schema_version(connection) == 0
        with pytest.raises(MigrationStateError, match="valid canonical"):
            migrate_connection(connection, applied_at="2026-02-30T00:00:00.000Z")
        connection.execute("BEGIN")
        with pytest.raises(MigrationStateError, match="idle"):
            migrate_connection(connection)
        connection.rollback()
        migrate_connection(connection)
        assert canonical_schema_digest(connection) == EXPECTED_SCHEMA_DIGEST

    gap = tmp_path / "gap.sqlite3"
    with open_v3_connection(gap) as connection:
        connection.execute(migration_module._METADATA_SQL)
        connection.execute(
            "INSERT INTO v3_schema_migrations VALUES (2, ?, ?, ?)",
            (
                DEFAULT_MIGRATIONS[1].name,
                DEFAULT_MIGRATIONS[1].checksum,
                "2026-08-22T00:00:00.000Z",
            ),
        )
        with pytest.raises(MigrationStateError, match="version gap"):
            migrate_connection(connection)

    unreadable = tmp_path / "unreadable.sqlite3"
    with open_v3_connection(unreadable) as connection:
        connection.execute("CREATE TABLE v3_schema_migrations(other INTEGER)")
        with pytest.raises(MigrationStateError, match="cannot be read"):
            current_schema_version(connection)
