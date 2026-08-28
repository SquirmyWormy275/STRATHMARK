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
            "wal_autocheckpoint": connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0],
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


@pytest.mark.parametrize("prior_version", range(len(DEFAULT_MIGRATIONS)))
def test_every_supported_prior_schema_upgrades_repeatably_to_canonical(
    tmp_path: Path,
    prior_version: int,
) -> None:
    database = tmp_path / f"prior-{prior_version}" / "authority.sqlite3"
    with open_v3_connection(database) as connection:
        assert (
            migrate_connection(connection, migrations=DEFAULT_MIGRATIONS[:prior_version])
            == prior_version
        )
        assert current_schema_version(connection) == prior_version
        assert migrate_connection(connection) == len(DEFAULT_MIGRATIONS) - prior_version
        assert canonical_schema_digest(connection) == EXPECTED_SCHEMA_DIGEST
        assert migrate_connection(connection) == 0
        assert current_schema_version(connection) == len(DEFAULT_MIGRATIONS)
        assert [row[1] for row in connection.execute("PRAGMA table_info(v3_aggregate_heads)")] == [
            "aggregate_kind",
            "aggregate_id",
            "aggregate_version",
            "event_digest",
            "lifecycle_status",
            "head_digest",
        ]


def test_populated_0005_to_0006_upgrade_preserves_import_rows_and_rolls_back(
    tmp_path: Path,
) -> None:
    database = tmp_path / "populated-0005.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection, migrations=DEFAULT_MIGRATIONS[:5])
        connection.execute(
            "INSERT INTO v3_historical_imports("
            "import_id,source_profile_json,source_catalog_digest,source_tip_digest,"
            "source_cutoff,source_manifest_json,imported_row_count,imported_at,"
            "cutover_manifest_digest,eligible) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "import:migration-0006",
                '{"schema_version":"test-source-v1"}',
                "a" * 64,
                "b" * 64,
                "2026-08-22T00:00:00.000Z",
                '{"schema_version":"test-manifest-v1"}',
                1,
                "2026-08-22T00:00:01.000Z",
                None,
                0,
            ),
        )
        connection.execute(
            "INSERT INTO v3_historical_import_rows("
            "import_id,source_group,source_table,ordinal,row_digest,canonical_json,eligible) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "import:migration-0006",
                "results",
                "prediction_results",
                0,
                "c" * 64,
                '{"row":"preserved"}',
                0,
            ),
        )
        expected_import = tuple(
            connection.execute(
                "SELECT * FROM v3_historical_imports WHERE import_id=?",
                ("import:migration-0006",),
            ).fetchone()
        )
        expected_row = tuple(
            connection.execute(
                "SELECT * FROM v3_historical_import_rows WHERE import_id=?",
                ("import:migration-0006",),
            ).fetchone()
        )

        connection.execute("CREATE TABLE v3_historical_cutovers(decoy INTEGER)")
        with pytest.raises(MigrationStateError, match="0006_signed_historical_cutover"):
            migrate_connection(connection)
        assert current_schema_version(connection) == 5
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_historical_imports WHERE import_id=?",
                    ("import:migration-0006",),
                ).fetchone()
            )
            == expected_import
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_historical_import_rows WHERE import_id=?",
                    ("import:migration-0006",),
                ).fetchone()
            )
            == expected_row
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='v3_historical_imports_no_update'"
            ).fetchone()
            is not None
        )

        connection.execute("DROP TABLE v3_historical_cutovers")
        assert migrate_connection(connection) == len(DEFAULT_MIGRATIONS) - 5
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_historical_imports WHERE import_id=?",
                    ("import:migration-0006",),
                ).fetchone()
            )
            == expected_import
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_historical_import_rows WHERE import_id=?",
                    ("import:migration-0006",),
                ).fetchone()
            )
            == expected_row
        )
        assert canonical_schema_digest(connection) == EXPECTED_SCHEMA_DIGEST
        assert migrate_connection(connection) == 0


def test_populated_0009_to_0010_upgrade_backfills_receipts_and_rolls_back(
    tmp_path: Path,
) -> None:
    database = tmp_path / "populated-0009.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection, migrations=DEFAULT_MIGRATIONS[:9])
        connection.execute(
            "INSERT INTO v3_events("
            "global_sequence,event_id,aggregate_kind,aggregate_id,aggregate_version,event_kind,"
            "envelope_json,event_digest,prior_global_digest,prior_aggregate_digest,"
            "occurred_at_utc,command_id,source_import_id,training_eligible) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                "event:migration-0010",
                "field",
                "field:migration-0010",
                1,
                "field.prepared",
                "{}",
                "a" * 64,
                "0" * 64,
                "0" * 64,
                "2026-08-22T00:00:00.000Z",
                "command:migration-0010",
                None,
                0,
            ),
        )
        connection.execute(
            "INSERT INTO v3_field_receipts("
            "receipt_id,field_id,field_revision,supersedes_receipt_id,caller_namespace,"
            "request_identity,field_revision_digest,pipeline_digest,receipt_json,receipt_digest,"
            "crn_assignments_json,source_global_sequence,superseded_by_sequence,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "receipt:migration-0010",
                "field:migration-0010",
                3,
                None,
                "caller:test",
                "request:migration-0010",
                "b" * 64,
                "c" * 64,
                "{}",
                "d" * 64,
                "[]",
                1,
                None,
                "2026-08-22T00:00:01.000Z",
            ),
        )
        expected_receipt = tuple(
            connection.execute(
                "SELECT * FROM v3_field_receipts WHERE receipt_id=?",
                ("receipt:migration-0010",),
            ).fetchone()
        )

        connection.execute("CREATE TABLE v3_field_capacity_authorities(decoy INTEGER)")
        with pytest.raises(
            MigrationStateError,
            match="0010_field_capacity_and_disagreement_authority",
        ):
            migrate_connection(connection)
        assert current_schema_version(connection) == 9
        assert [row[1] for row in connection.execute("PRAGMA table_info(v3_field_receipts)")] == [
            "receipt_id",
            "field_id",
            "field_revision",
            "supersedes_receipt_id",
            "caller_namespace",
            "request_identity",
            "field_revision_digest",
            "pipeline_digest",
            "receipt_json",
            "receipt_digest",
            "crn_assignments_json",
            "source_global_sequence",
            "superseded_by_sequence",
            "created_at",
        ]
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_field_receipts WHERE receipt_id=?",
                    ("receipt:migration-0010",),
                ).fetchone()
            )
            == expected_receipt
        )

        connection.execute("DROP TABLE v3_field_capacity_authorities")
        assert migrate_connection(connection) == len(DEFAULT_MIGRATIONS) - 9
        upgraded_receipt = tuple(
            connection.execute(
                "SELECT * FROM v3_field_receipts WHERE receipt_id=?",
                ("receipt:migration-0010",),
            ).fetchone()
        )
        assert upgraded_receipt[:-1] == expected_receipt
        assert upgraded_receipt[-1] == 3
        assert tuple(
            connection.execute(
                "SELECT receipt_revision,upstream_field_revision FROM v3_field_receipts "
                "WHERE receipt_id=?",
                ("receipt:migration-0010",),
            ).fetchone()
        ) == (3, 3)
        assert canonical_schema_digest(connection) == EXPECTED_SCHEMA_DIGEST
        assert migrate_connection(connection) == 0


def test_populated_0012_to_0013_upgrade_backfills_authority_links_and_rolls_back(
    tmp_path: Path,
) -> None:
    database = tmp_path / "populated-0012.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection, migrations=DEFAULT_MIGRATIONS[:12])
        connection.execute(
            "INSERT INTO v3_jobs("
            "job_id,job_revision,idempotency_key,job_kind,lane,resource_class,base_priority,"
            "capacity_use_json,payload_json,payload_digest,evidence_digest,bundle_digest,"
            "retry_policy_version,state,attempt_count,max_attempts,initial_not_before_at,"
            "not_before_at,hard_deadline_at,lease_owner,lease_acquired_at,lease_expires_at,"
            "fencing_token,terminal_reason,result_digest,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "job:migration-0013",
                1,
                "job-request:migration-0013",
                "formula_card",
                "inference",
                "local_cpu",
                100,
                "{}",
                '{"schema_version":"test-job-v1"}',
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "retry.v1",
                "queued",
                0,
                2,
                "2026-08-22T00:00:00.000Z",
                "2026-08-22T00:00:00.000Z",
                "2026-08-22T00:10:00.000Z",
                None,
                None,
                None,
                0,
                None,
                None,
                "2026-08-22T00:00:00.000Z",
                "2026-08-22T00:00:00.000Z",
            ),
        )
        connection.execute(
            "INSERT INTO v3_job_history("
            "history_sequence,transition_id,job_id,job_revision,operation_kind,from_state,"
            "result_state,attempt_count,fencing_token,lease_owner,lease_acquired_at,"
            "lease_expires_at,not_before_at,terminal_reason,result_digest,observed_at,"
            "prior_history_digest,history_digest,job_material_digest,auth_body_json,"
            "auth_body_digest,auth_key_id,auth_signature_der_b64) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                "transition:migration-0013",
                "job:migration-0013",
                1,
                "queued",
                None,
                "queued",
                0,
                0,
                None,
                None,
                None,
                "2026-08-22T00:00:00.000Z",
                None,
                None,
                "2026-08-22T00:00:00.000Z",
                "0" * 64,
                "d" * 64,
                "e" * 64,
                "{}",
                "f" * 64,
                "integrity-key:migration-0013",
                "test-signature",
            ),
        )
        connection.execute(
            "INSERT INTO v3_rolling_restart_checkpoints("
            "checkpoint_sequence,prior_checkpoint_digest,capacity_manifest_digest,"
            "source_global_sequence,source_event_digest,aggregate_heads_json,"
            "aggregate_head_count,aggregate_heads_digest,reaction_cursor_digest,"
            "reaction_cursor_revision,reaction_relevant_command_count,"
            "reaction_latest_reaction_id,job_history_sequence,job_history_digest,"
            "status_sequence,status_digest,current_subjects_json,current_subject_count,"
            "current_subject_digest,active_job_count,active_job_digest,pending_reactions_json,"
            "pending_reaction_count,pending_reaction_digest,checkpoint_digest,"
            "checkpoint_manifest_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                "0" * 64,
                "1" * 64,
                0,
                "0" * 64,
                "[]",
                0,
                "2" * 64,
                "3" * 64,
                0,
                0,
                "0" * 64,
                1,
                "d" * 64,
                0,
                "0" * 64,
                "[]",
                0,
                "4" * 64,
                1,
                "5" * 64,
                "[]",
                0,
                "6" * 64,
                "7" * 64,
                "{}",
                "2026-08-22T00:00:01.000Z",
            ),
        )
        expected_job = tuple(
            connection.execute(
                "SELECT * FROM v3_jobs WHERE job_id=? AND job_revision=?",
                ("job:migration-0013", 1),
            ).fetchone()
        )
        expected_history = tuple(
            connection.execute("SELECT * FROM v3_job_history WHERE history_sequence=1").fetchone()
        )
        expected_checkpoint = tuple(
            connection.execute(
                "SELECT * FROM v3_rolling_restart_checkpoints WHERE checkpoint_sequence=1"
            ).fetchone()
        )

        connection.execute("CREATE TABLE v3_rolling_restart_deltas(decoy INTEGER)")
        with pytest.raises(
            MigrationStateError,
            match="0013_rolling_delta_and_job_spec_authority",
        ):
            migrate_connection(connection)
        assert current_schema_version(connection) == 12
        assert "job_spec_digest" not in {
            row[1] for row in connection.execute("PRAGMA table_info(v3_job_history)")
        }
        checkpoint_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(v3_rolling_restart_checkpoints)")
        }
        assert "absorbed_delta_sequence" not in checkpoint_columns
        assert "absorbed_delta_digest" not in checkpoint_columns
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='v3_jobs_no_delete'"
            ).fetchone()
            is not None
        )
        assert (
            connection.execute(
                "SELECT history_digest FROM v3_job_history WHERE history_sequence=1"
            ).fetchone()[0]
            == "d" * 64
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_jobs WHERE job_id=? AND job_revision=?",
                    ("job:migration-0013", 1),
                ).fetchone()
            )
            == expected_job
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_job_history WHERE history_sequence=1"
                ).fetchone()
            )
            == expected_history
        )
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_rolling_restart_checkpoints WHERE checkpoint_sequence=1"
                ).fetchone()
            )
            == expected_checkpoint
        )

        connection.execute("DROP TABLE v3_rolling_restart_deltas")
        assert migrate_connection(connection) == len(DEFAULT_MIGRATIONS) - 12
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM v3_jobs WHERE job_id=? AND job_revision=?",
                    ("job:migration-0013", 1),
                ).fetchone()
            )
            == expected_job
        )
        upgraded_history = tuple(
            connection.execute("SELECT * FROM v3_job_history WHERE history_sequence=1").fetchone()
        )
        assert upgraded_history[:-1] == expected_history
        assert upgraded_history[-1] == "0" * 64
        upgraded_checkpoint = tuple(
            connection.execute(
                "SELECT * FROM v3_rolling_restart_checkpoints WHERE checkpoint_sequence=1"
            ).fetchone()
        )
        assert upgraded_checkpoint[:-2] == expected_checkpoint
        assert upgraded_checkpoint[-2:] == (0, "0" * 64)
        assert canonical_schema_digest(connection) == EXPECTED_SCHEMA_DIGEST
        assert migrate_connection(connection) == 0


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
        assert current_schema_version(connection) == len(DEFAULT_MIGRATIONS)
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
        connection.execute("CREATE TABLE v3_schema_migrations(version INTEGER PRIMARY KEY)")
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
    monkeypatch.setattr(sqlite_connection.sqlite3, "connect", lambda *args, **kwargs: refused)
    with pytest.raises(SQLitePolicyError, match="refused"):
        open_v3_connection(tmp_path / "refused.sqlite3")
    assert refused.closed is True


def test_deadline_connection_nested_transaction_and_checkpoint_guards(
    tmp_path: Path,
) -> None:
    deadline = SQLiteDeadline(timeout_seconds=1)
    with open_v3_connection(tmp_path / "deadline.sqlite3", deadline=deadline) as connection:
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
    monkeypatch.setattr(migration_module, "_PINNED_CHECKSUMS", {"0001_valid.sql": "0" * 64})
    with pytest.raises(MigrationChecksumError, match="checksum drift"):
        migration_module._load_default_migrations()

    sql = "SELECT 1;"
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    valid = Migration(1, "0001_valid.sql", checksum, sql)
    with pytest.raises(MigrationStateError, match="consecutive"):
        migration_module._validate_catalog((Migration(2, "0002_gap.sql", checksum, sql),))
    with pytest.raises(MigrationStateError, match="unique"):
        migration_module._validate_catalog((valid, Migration(2, valid.name, checksum, sql)))
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
