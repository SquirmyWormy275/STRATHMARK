from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

import strathmark.v3.infrastructure.v2_import as v2_import
from strathmark.ledger import _IMMUTABILITY_TRIGGERS as V2_LEDGER_TRIGGERS
from strathmark.ledger import _SCHEMA as V2_LEDGER_SCHEMA
from strathmark.store import _CREATE_EVIDENCE_SCHEMA_SQL as V2_EVIDENCE_SCHEMA
from strathmark.store import _CREATE_INDEX_SQL as V2_RESULTS_INDEX
from strathmark.store import _CREATE_TABLE_SQL as V2_RESULTS_SCHEMA
from strathmark.v3.contracts.commands import BlobReference, CommandKind, InlinePayload
from strathmark.v3.contracts.events import EventEnvelope, EventKind
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.infrastructure.sqlite.connection import (
    SQLiteDeadlineExceeded,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection
from strathmark.v3.infrastructure.v2_import import (
    V2ImportError,
    V2ImportPathConflictError,
    V2SourceChangedError,
    V2SourceIntegrityError,
    V2SourceSchemaError,
    import_v2_snapshot,
    open_v2_readonly,
)


@contextmanager
def _sqlite(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _create_current_ledger(path: Path, *, upgraded: bool = False) -> None:
    with _sqlite(path) as connection:
        if upgraded:
            legacy_schema = V2_LEDGER_SCHEMA.replace(
                "    hash_algorithm TEXT NOT NULL DEFAULT 'active-v2'\n"
                "        CHECK(hash_algorithm IN ('raw-v1', 'active-v2')),\n",
                "",
            )
            connection.executescript(legacy_schema)
            connection.execute(
                "ALTER TABLE prediction_requests ADD COLUMN hash_algorithm "
                "TEXT NOT NULL DEFAULT 'raw-v1' "
                "CHECK(hash_algorithm IN ('raw-v1', 'active-v2'))"
            )
        else:
            connection.executescript(V2_LEDGER_SCHEMA)
        connection.executescript(V2_LEDGER_TRIGGERS)


def _create_legacy_ledger(path: Path, *, outbox: bool) -> None:
    legacy_schema = V2_LEDGER_SCHEMA.replace(
        "    hash_algorithm TEXT NOT NULL DEFAULT 'active-v2'\n"
        "        CHECK(hash_algorithm IN ('raw-v1', 'active-v2')),\n",
        "",
    )
    boundary = (
        "CREATE TABLE IF NOT EXISTS shadow_receipts"
        if outbox
        else "CREATE TABLE IF NOT EXISTS prediction_mirror_outbox"
    )
    tables = legacy_schema.split(boundary, 1)[0]
    indexes = """
    CREATE INDEX idx_ledger_predictions_competitor
        ON ledger_predictions(competitor_id, event_code);
    CREATE INDEX idx_prediction_settlements_prediction
        ON prediction_settlements(prediction_id, revision DESC);
    """
    triggers = V2_LEDGER_TRIGGERS.split(
        "CREATE TRIGGER IF NOT EXISTS shadow_receipts_no_update", 1
    )[0]
    with _sqlite(path) as connection:
        connection.executescript(tables + indexes + triggers)


def _create_legacy_results(path: Path) -> None:
    with _sqlite(path) as connection:
        connection.executescript(
            """
            CREATE TABLE results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competitor_name TEXT NOT NULL,
                event_code TEXT NOT NULL,
                time_seconds REAL NOT NULL,
                species TEXT NOT NULL,
                diameter_mm REAL NOT NULL,
                quality INTEGER NOT NULL,
                heat_id TEXT NOT NULL DEFAULT '',
                result_date TEXT,
                recorded_at TEXT NOT NULL,
                UNIQUE(competitor_name, heat_id, event_code, time_seconds)
            );
            CREATE INDEX idx_results_competitor ON results(competitor_name, event_code);
            """
        )


def _create_evidence_source(path: Path, *, row_count: int = 1) -> tuple[str, str]:
    with _sqlite(path) as connection:
        connection.executescript(V2_RESULTS_SCHEMA)
        connection.executescript(V2_RESULTS_INDEX)
        connection.executescript(V2_EVIDENCE_SCHEMA)
        row_values = [
            {
                "schema_version": "strathmark.evidence-history-row.v1",
                "competitor_id": f"competitor:opaque-{ordinal + 1}",
                "event_code": "UH",
                "time_seconds": 31.25 + ordinal / 10_000,
                "species": "aspen",
                "diameter_mm": 300.0,
                "quality": 3,
                "competition_id": "competition:show-1",
                "heat_id": f"heat:{ordinal + 1}",
                "result_date": "2026-08-20",
            }
            for ordinal in range(row_count)
        ]
        row_digests = [
            hashlib.sha256(
                json.dumps(
                    row_value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode()
            ).hexdigest()
            for row_value in row_values
        ]
        snapshot_core = {
            "schema_version": "strathmark.evidence-snapshot.v1",
            "source_schema_version": "strathmark.evidence-snapshot-source.v1",
            "source_id": "source:test",
            "source_digest": "a" * 64,
            "cutoff": "2026-08-22",
            "cutoff_semantics": "exclusive-utc-date",
            "captured_at": "2026-08-22T00:00:00+00:00",
            "completeness": "full",
            "supplied_row_count": row_count,
            "accepted_row_count": row_count,
            "rejected_row_count": 0,
            "diagnostics": {},
            "rows": row_values,
        }
        snapshot_json = json.dumps(
            snapshot_core, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        snapshot_digest = hashlib.sha256(snapshot_json.encode()).hexdigest()
        connection.execute(
            "INSERT INTO evidence_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, 'full', ?, ?, 0, "
            "'{}', ?, '2026-08-22T00:00:00+00:00')",
            (
                snapshot_digest,
                "strathmark.evidence-snapshot.v1",
                "strathmark.evidence-snapshot-source.v1",
                "source:test",
                "a" * 64,
                "2026-08-22",
                "2026-08-22T00:00:00+00:00",
                row_count,
                row_count,
                snapshot_json,
            ),
        )
        connection.executemany(
            "INSERT INTO evidence_snapshot_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    snapshot_digest,
                    ordinal,
                    row_digests[ordinal],
                    row_value["competitor_id"],
                    row_value["event_code"],
                    row_value["time_seconds"],
                    row_value["species"],
                    row_value["diameter_mm"],
                    row_value["quality"],
                    row_value["competition_id"],
                    row_value["heat_id"],
                    row_value["result_date"],
                )
                for ordinal, row_value in enumerate(row_values)
            ],
        )
        activation_core = {
            "schema_version": "strathmark.evidence-snapshot-activation.v1",
            "revision": 1,
            "snapshot_digest": snapshot_digest,
            "previous_activation_id": None,
            "previous_snapshot_digest": None,
            "activated_at": "2026-08-22T00:00:00+00:00",
        }
        activation_json = json.dumps(
            activation_core, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        activation_id = hashlib.sha256(activation_json.encode()).hexdigest()
        connection.execute(
            "INSERT INTO evidence_snapshot_activations VALUES (?, ?, 1, ?, NULL, NULL, ?, ?)",
            (
                activation_id,
                "strathmark.evidence-snapshot-activation.v1",
                snapshot_digest,
                "2026-08-22T00:00:00+00:00",
                activation_json,
            ),
        )
    return snapshot_digest, row_digests[0]


def _insert_valid_shadow_and_correction(path: Path) -> None:
    active_material = {"caller_input": {"competitors": [{"competitor_id": "competitor:opaque"}]}}
    active_fingerprint = hashlib.sha256(
        json.dumps(active_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    projection_material = {"field": "field:1"}
    projection_fingerprint = hashlib.sha256(
        json.dumps(projection_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    core = {
        "schema_version": "strathmark.shadow-receipt-core.v1",
        "consumer_id": "caller:service",
        "request_id": "request:1",
        "active_input": {**active_material, "fingerprint": active_fingerprint},
        "request_projection": {**projection_material, "fingerprint": projection_fingerprint},
        "ledger": {"request_hash": "a" * 64, "hash_algorithm": "active-v2"},
        "predictions": [{"prediction_id": "prediction:1"}],
    }
    core_json = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with _sqlite(path) as connection:
        connection.execute(
            "INSERT INTO prediction_requests VALUES "
            "('request-row:1', 'caller:service', 'request:1', ?, 'active-v2', 'SB', "
            "'2026-08-20', '2026-08-20T00:00:00+00:00')",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO ledger_predictions(prediction_id, ledger_request_id, competitor_id, "
            "ordinal, event_code, median_seconds, assigned_mark, source, training_eligible, "
            "ignored_factors_json, warnings_json, optimizer_metadata_json, created_at) VALUES "
            "('prediction:1', 'request-row:1', 'competitor:opaque', 0, 'SB', 30, 3, "
            "'baseline', 0, '[]', '[]', '{}', '2026-08-20T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO shadow_receipts VALUES "
            "('request-row:1', 'caller:service', 'request:1', ?, "
            "'strathmark.shadow-receipt-core.v1', ?, '2026-08-20T00:00:00+00:00')",
            (active_fingerprint, core_json),
        )
        connection.execute(
            "INSERT INTO prediction_settlements VALUES "
            "('settlement:1', 'prediction:1', 1, 'competitor:opaque', 'SB', 31, 1, "
            "'actor:redacted', NULL, ?, NULL, '2026-08-21T00:00:00+00:00')",
            ("b" * 64,),
        )
        connection.execute(
            "INSERT INTO numeric_outcome_revisions VALUES "
            "('field-revision:1', 'outcome:1', 'request-row:1', 'caller:service', ?, "
            "'actor:service', 'corrected_time', '2026-08-21T01:00:00+00:00')",
            ("c" * 64,),
        )
        connection.execute(
            "INSERT INTO numeric_settlement_revisions VALUES "
            "('revision:2', 'field-revision:1', 'prediction:1', 2, 'competitor:opaque', "
            "'SB', 'settle', 30.5, 0.5, 'settlement:1', '2026-08-21T01:00:00+00:00')"
        )


def test_empty_v2_source_import_is_repeatable_and_ineligible(tmp_path: Path) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    source.parent.mkdir()
    source.touch()
    before = v2_import._source_file_manifest(source)
    first = import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    second = import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    assert first == second
    assert first.profile_ids == ("v2-empty-v1",)
    assert first.imported_row_count == 0
    assert first.eligible is False
    assert "cannot authenticate source actor or original write" in first.limitations
    assert v2_import._source_file_manifest(source) == before
    with open_v3_connection(destination, read_only=True) as connection:
        row = connection.execute(
            "SELECT eligible, cutover_manifest_digest FROM v3_historical_imports"
        ).fetchone()
        assert tuple(row) == (0, None)
        assert connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0] == 1
        assert tuple(
            connection.execute(
                "SELECT aggregate_kind, aggregate_id, aggregate_version, event_digest "
                "FROM v3_aggregate_heads"
            ).fetchone()
        ) == (
            "system",
            "system:v2-history",
            1,
            connection.execute("SELECT event_digest FROM v3_events").fetchone()[0],
        )
        idempotency = connection.execute(
            "SELECT principal_id, idempotency_key, command_digest, result_schema_version, "
            "result_json, result_digest, first_global_sequence, last_global_sequence, "
            "event_set_digest FROM v3_idempotency_records"
        ).fetchone()
        assert idempotency is not None
        assert tuple(idempotency[:2]) == (
            "actor:v2-readonly-import",
            f"command:{first.source_tip_digest}",
        )
        assert tuple(idempotency[6:8]) == (1, 1)
        result_value = json.loads(idempotency[4])
        assert result_value["source_tip_digest"] == first.source_tip_digest
        assert result_value["eligible"] is False
        assert hashlib.sha256(idempotency[4].encode()).hexdigest() == idempotency[5]


def test_source_and_destination_must_be_distinct(tmp_path: Path) -> None:
    database = tmp_path / "same.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection)
    with pytest.raises(V2ImportPathConflictError):
        import_v2_snapshot(database, database, cutoff="2026-08-22T00:00:00.000Z")


def test_v2_source_is_opened_with_true_read_only_policy(tmp_path: Path) -> None:
    source = tmp_path / "v2.sqlite3"
    source.touch()
    with open_v2_readonly(source) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        with pytest.raises(Exception):
            connection.execute("CREATE TABLE forbidden(value INTEGER)")


def test_readonly_open_deadline_missing_file_and_samefile_alias_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(FileNotFoundError):
        with open_v2_readonly(tmp_path):
            pass
    source = tmp_path / "source.sqlite3"
    source.touch()
    deadline = v2_import.SQLiteDeadline(timeout_seconds=1)
    with open_v2_readonly(source, deadline=deadline) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    alias = tmp_path / "alias.sqlite3"
    os.link(source, alias)
    with pytest.raises(V2ImportPathConflictError, match="same file"):
        import_v2_snapshot(source, alias, cutoff="2026-08-22T00:00:00.000Z")
    monkeypatch.setattr(Path, "samefile", lambda *_args: (_ for _ in ()).throw(OSError("no id")))
    with pytest.raises(V2ImportPathConflictError, match="identity"):
        v2_import._reject_same_file(source, alias)


def test_imported_event_round_trips_u2_contract_and_only_safe_evidence_persists(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    _create_evidence_source(source)
    result = import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    assert result.profile_ids == ("v2-results-current-v1", "v2-evidence-snapshot-v1")
    assert result.imported_row_count == 1
    with open_v3_connection(destination, read_only=True) as connection:
        event_json = connection.execute("SELECT envelope_json FROM v3_events").fetchone()[0]
        event = EventEnvelope.from_dict(json.loads(event_json))
        assert event.kind is EventKind.HISTORY_IMPORTED
        stored = connection.execute(
            "SELECT canonical_json, eligible FROM v3_historical_import_rows"
        ).fetchone()
        assert json.loads(stored[0])["competitor_id"] == "competitor:opaque-1"
        assert tuple(stored)[1] == 0


def test_large_released_evidence_profile_uses_bounded_deterministic_source_commitment(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    row_count = 1_200
    assert row_count * 64 > 65_536
    _create_evidence_source(source, row_count=row_count)
    before = v2_import._source_file_manifest(source)
    first_snapshot = v2_import._read_source_snapshot(
        source, cutoff="2026-08-22T00:00:00.000Z", deadline=None
    )
    second_snapshot = v2_import._read_source_snapshot(
        source, cutoff="2026-08-22T00:00:00.000Z", deadline=None
    )
    assert first_snapshot.source_tip_digest == second_snapshot.source_tip_digest
    assert first_snapshot.manifest == second_snapshot.manifest
    assert first_snapshot.manifest["schema_version"] == "strathmark-v3-v2-source-tip-v2"
    canonical = json.dumps(first_snapshot.manifest, sort_keys=True, separators=(",", ":")).encode()
    assert len(canonical) < 65_536
    summaries = first_snapshot.manifest["rows"]
    assert all("row_digests" not in summary for summary in summaries)
    evidence_rows = next(
        summary for summary in summaries if summary["table"] == "evidence_snapshot_rows"
    )
    assert evidence_rows["row_count"] == row_count
    assert evidence_rows["profile_ids"] == [
        "v2-results-current-v1",
        "v2-evidence-snapshot-v1",
    ]

    first = import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    second = import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    assert first == second
    assert first.source_tip_digest == first_snapshot.source_tip_digest
    assert first.imported_row_count == row_count
    assert v2_import._source_file_manifest(source) == before
    with open_v3_connection(destination, read_only=True) as connection:
        event_json = connection.execute("SELECT envelope_json FROM v3_events").fetchone()[0]
        event = EventEnvelope.from_dict(json.loads(event_json))
        assert len(event.command.payload.canonical_json.encode()) == len(canonical)
        assert len(event_json.encode()) < 1_048_576
        assert (
            connection.execute("SELECT COUNT(*) FROM v3_historical_import_rows").fetchone()[0]
            == row_count
        )

    with _sqlite(source) as connection:
        connection.execute("DROP TRIGGER evidence_snapshot_rows_no_update")
        connection.execute(
            "UPDATE evidence_snapshot_rows SET time_seconds=time_seconds+1 WHERE ordinal=1199"
        )
        connection.execute(
            "CREATE TRIGGER evidence_snapshot_rows_no_update "
            "BEFORE UPDATE ON evidence_snapshot_rows "
            "BEGIN SELECT RAISE(ABORT, 'append-only table'); END"
        )
    with pytest.raises(V2SourceIntegrityError):
        import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")


@pytest.mark.parametrize("upgraded", [False, True])
def test_current_fresh_and_upgraded_raw_v1_ledger_profiles_are_recognized(
    tmp_path: Path, upgraded: bool
) -> None:
    source = tmp_path / f"source-{upgraded}" / "v2.sqlite3"
    destination = tmp_path / f"destination-{upgraded}" / "v3.sqlite3"
    _create_current_ledger(source, upgraded=upgraded)
    result = import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    assert result.profile_ids == (
        "v2-shadow-ledger-current-upgraded-raw-v1"
        if upgraded
        else "v2-shadow-ledger-current-fresh-v1",
    )


@pytest.mark.parametrize(
    ("outbox", "profile"),
    [
        (False, "v2-ledger-core-no-hash-v1"),
        (True, "v2-ledger-outbox-no-hash-v1"),
    ],
)
def test_released_legacy_ledger_profiles_are_recognized(
    tmp_path: Path, outbox: bool, profile: str
) -> None:
    source = tmp_path / f"source-{outbox}" / "v2.sqlite3"
    _create_legacy_ledger(source, outbox=outbox)
    result = import_v2_snapshot(
        source,
        tmp_path / f"destination-{outbox}" / "v3.sqlite3",
        cutoff="2026-08-22T00:00:00.000Z",
    )
    assert result.profile_ids == (profile,)


def test_legacy_mutable_results_shape_is_recognized_but_never_imported(tmp_path: Path) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    _create_legacy_results(source)
    with _sqlite(source) as connection:
        connection.execute(
            "INSERT INTO results(competitor_name, event_code, time_seconds, species, diameter_mm, "
            "quality, heat_id, result_date, recorded_at) VALUES "
            "('Private Name', 'SB', 30, 'pine', 300, 3, 'heat-1', '2026-08-20', 'now')"
        )
    result = import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    assert result.profile_ids == ("v2-results-legacy-v1",)
    assert result.imported_row_count == 0
    with open_v3_connection(destination, read_only=True) as connection:
        manifest = connection.execute(
            "SELECT source_manifest_json FROM v3_historical_imports"
        ).fetchone()[0]
        assert "Private Name" not in manifest


def test_ledger_actor_reason_and_caller_rows_are_hashed_but_never_persisted(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    _create_current_ledger(source)
    with _sqlite(source) as connection:
        connection.execute(
            "INSERT INTO prediction_requests VALUES (?, ?, ?, ?, 'active-v2', 'SB', ?, ?)",
            (
                "request:1",
                "caller:private",
                "request:private",
                "a" * 64,
                "2026-08-20",
                "2026-08-20T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO ledger_predictions(prediction_id, ledger_request_id, competitor_id, "
            "ordinal, event_code, median_seconds, assigned_mark, source, training_eligible, "
            "ignored_factors_json, warnings_json, optimizer_metadata_json, created_at) "
            "VALUES ('prediction:1', 'request:1', 'competitor:opaque', 0, 'SB', 30, 3, "
            "'baseline', 0, '[]', '[]', '{}', '2026-08-20T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO prediction_settlements VALUES ('settlement:1', 'prediction:1', 1, "
            "'competitor:opaque', 'SB', 31, 1, 'actor:private-human', 'private free text', ?, "
            "NULL, '2026-08-21T00:00:00+00:00')",
            ("b" * 64,),
        )
    import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    with open_v3_connection(destination, read_only=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM v3_historical_import_rows").fetchone()[0] == 0
        )
        persisted = connection.execute(
            "SELECT source_manifest_json FROM v3_historical_imports"
        ).fetchone()[0]
        assert "private-human" not in persisted
        assert "private free text" not in persisted
        assert "caller:private" not in persisted


def test_valid_shadow_receipt_and_settlement_supersession_chain_is_verified(tmp_path: Path) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    _create_current_ledger(source)
    _insert_valid_shadow_and_correction(source)
    result = import_v2_snapshot(
        source,
        tmp_path / "destination" / "v3.sqlite3",
        cutoff="2026-08-22T00:00:00.000Z",
    )
    assert result.profile_ids == ("v2-shadow-ledger-current-fresh-v1",)
    assert result.imported_row_count == 0


def test_tampered_trigger_body_and_partial_trusted_group_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "tampered" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    _create_current_ledger(source)
    with _sqlite(source) as connection:
        connection.execute("DROP TRIGGER prediction_requests_no_update")
        connection.execute(
            "CREATE TRIGGER prediction_requests_no_update BEFORE UPDATE ON prediction_requests "
            "BEGIN SELECT RAISE(ABORT, 'append-only table'); SELECT 2; END"
        )
    with pytest.raises(V2SourceSchemaError, match="trigger"):
        import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")

    partial = tmp_path / "partial" / "v2.sqlite3"
    with _sqlite(partial) as connection:
        connection.execute("CREATE TABLE prediction_requests(ledger_request_id TEXT PRIMARY KEY)")
    with pytest.raises(V2SourceSchemaError, match="partial"):
        import_v2_snapshot(
            partial,
            tmp_path / "partial-destination" / "v3.sqlite3",
            cutoff="2026-08-22T00:00:00.000Z",
        )


def test_unknown_column_index_nonce_trigger_and_intermediate_profile_fail_closed(
    tmp_path: Path,
) -> None:
    cases: list[tuple[str, callable]] = []

    unknown = tmp_path / "unknown" / "v2.sqlite3"
    with _sqlite(unknown) as connection:
        connection.execute("CREATE TABLE invented(id INTEGER PRIMARY KEY)")
    cases.append(
        (
            "unsupported V2 tables",
            lambda: import_v2_snapshot(
                unknown, tmp_path / "d1" / "v3.sqlite3", cutoff="2026-08-22T00:00:00.000Z"
            ),
        )
    )

    column = tmp_path / "column" / "v2.sqlite3"
    _create_current_ledger(column)
    with _sqlite(column) as connection:
        connection.execute("ALTER TABLE ledger_predictions ADD COLUMN injected TEXT")
    cases.append(
        (
            "semantics drifted",
            lambda: import_v2_snapshot(
                column, tmp_path / "d2" / "v3.sqlite3", cutoff="2026-08-22T00:00:00.000Z"
            ),
        )
    )

    index = tmp_path / "index" / "v2.sqlite3"
    _create_current_ledger(index)
    with _sqlite(index) as connection:
        connection.execute("CREATE INDEX invented_index ON prediction_requests(created_at)")
    cases.append(
        (
            "index semantics",
            lambda: import_v2_snapshot(
                index, tmp_path / "d3" / "v3.sqlite3", cutoff="2026-08-22T00:00:00.000Z"
            ),
        )
    )

    nonce = tmp_path / "nonce" / "v2.sqlite3"
    _create_current_ledger(nonce)
    with _sqlite(nonce) as connection:
        connection.execute("DROP TRIGGER actor_attestation_nonce_claims_no_update")
    cases.append(
        (
            "nonce",
            lambda: import_v2_snapshot(
                nonce, tmp_path / "d4" / "v3.sqlite3", cutoff="2026-08-22T00:00:00.000Z"
            ),
        )
    )

    intermediate = tmp_path / "intermediate" / "v2.sqlite3"
    _create_current_ledger(intermediate)
    with _sqlite(intermediate) as connection:
        connection.execute("DROP TABLE actor_attestation_nonce_claims")
    cases.append(
        (
            "partial or unsupported",
            lambda: import_v2_snapshot(
                intermediate, tmp_path / "d5" / "v3.sqlite3", cutoff="2026-08-22T00:00:00.000Z"
            ),
        )
    )

    for message, action in cases:
        with pytest.raises(V2SourceSchemaError, match=message):
            action()


def test_malformed_trigger_parser_and_typed_row_boundaries(tmp_path: Path) -> None:
    with pytest.raises(V2SourceSchemaError, match="malformed"):
        v2_import._trigger_body("SELECT 1")
    blob = v2_import._typed_value(b"secret")
    assert blob["type"] == "blob-sha256" and blob["bytes"] == 6
    with pytest.raises(V2SourceIntegrityError, match="unsupported SQLite value"):
        v2_import._typed_value(object())
    database = tmp_path / "no-pk.sqlite3"
    with _sqlite(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE prediction_features(value TEXT)")
        with pytest.raises(V2SourceSchemaError, match="primary key"):
            v2_import._build_row_manifests(connection, {"prediction_features"})


@pytest.mark.parametrize(
    "mutation",
    [
        "snapshot-malformed",
        "snapshot-noncanonical",
        "snapshot-digest",
        "row-orphan",
        "row-ordinal",
        "row-digest",
        "row-material",
        "accepted-count",
        "diagnostics-malformed",
        "scalar-source",
        "scalar-diagnostics",
        "scalar-diagnostics-canonical",
        "scalar-source-digest",
        "scalar-count-sum",
        "activation-malformed",
        "activation-core",
        "activation-digest",
        "activation-revision",
        "activation-previous-id",
        "activation-previous-snapshot",
        "activation-snapshot",
    ],
)
def test_every_v2_evidence_digest_and_activation_failure_is_rejected(
    tmp_path: Path, mutation: str
) -> None:
    source = tmp_path / mutation / "v2.sqlite3"
    _create_evidence_source(source)
    with _sqlite(source) as connection:
        connection.row_factory = sqlite3.Row
        for table in (
            "evidence_snapshots",
            "evidence_snapshot_rows",
            "evidence_snapshot_activations",
        ):
            connection.execute(f"DROP TRIGGER {table}_no_update")
        if mutation == "snapshot-malformed":
            connection.execute("UPDATE evidence_snapshots SET canonical_json='{'")
        elif mutation == "snapshot-noncanonical":
            core = json.loads(
                connection.execute("SELECT canonical_json FROM evidence_snapshots").fetchone()[0]
            )
            connection.execute(
                "UPDATE evidence_snapshots SET canonical_json=?", (json.dumps(core, indent=2),)
            )
        elif mutation == "snapshot-digest":
            core = json.loads(
                connection.execute("SELECT canonical_json FROM evidence_snapshots").fetchone()[0]
            )
            core["source_id"] = "source:different"
            connection.execute(
                "UPDATE evidence_snapshots SET canonical_json=?",
                (json.dumps(core, sort_keys=True, separators=(",", ":")),),
            )
        elif mutation == "row-orphan":
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("UPDATE evidence_snapshot_rows SET snapshot_digest=?", ("f" * 64,))
        elif mutation == "row-ordinal":
            connection.execute("UPDATE evidence_snapshot_rows SET ordinal=1")
        elif mutation == "row-digest":
            connection.execute("UPDATE evidence_snapshot_rows SET row_digest=?", ("f" * 64,))
        elif mutation == "row-material":
            row = dict(connection.execute("SELECT * FROM evidence_snapshot_rows").fetchone())
            projected = {
                "schema_version": "strathmark.evidence-history-row.v1",
                "competitor_id": row["competitor_id"],
                "event_code": row["event_code"],
                "time_seconds": 32.0,
                "species": row["species"],
                "diameter_mm": row["diameter_mm"],
                "quality": row["quality"],
                "competition_id": row["competition_id"],
                "heat_id": row["heat_id"],
                "result_date": row["result_date"],
            }
            digest = hashlib.sha256(
                json.dumps(projected, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            connection.execute(
                "UPDATE evidence_snapshot_rows SET time_seconds=32, row_digest=?", (digest,)
            )
        elif mutation == "accepted-count":
            row = connection.execute("SELECT * FROM evidence_snapshots").fetchone()
            core = json.loads(row["canonical_json"])
            core["accepted_row_count"] = 2
            canonical = json.dumps(core, sort_keys=True, separators=(",", ":"))
            new_digest = hashlib.sha256(canonical.encode()).hexdigest()
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "UPDATE evidence_snapshots SET snapshot_digest=?, accepted_row_count=2, "
                "canonical_json=?",
                (new_digest, canonical),
            )
            connection.execute("UPDATE evidence_snapshot_rows SET snapshot_digest=?", (new_digest,))
            activation = json.loads(
                connection.execute(
                    "SELECT canonical_json FROM evidence_snapshot_activations"
                ).fetchone()[0]
            )
            activation["snapshot_digest"] = new_digest
            activation_json = json.dumps(activation, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "UPDATE evidence_snapshot_activations SET activation_id=?, snapshot_digest=?, "
                "canonical_json=?",
                (hashlib.sha256(activation_json.encode()).hexdigest(), new_digest, activation_json),
            )
        elif mutation == "diagnostics-malformed":
            connection.execute("UPDATE evidence_snapshots SET diagnostics_json='{'")
        elif mutation == "scalar-source":
            connection.execute("UPDATE evidence_snapshots SET source_id='source:different'")
        elif mutation == "scalar-diagnostics":
            connection.execute("UPDATE evidence_snapshots SET diagnostics_json='{\"x\":1}'")
        elif mutation == "scalar-diagnostics-canonical":
            connection.execute("UPDATE evidence_snapshots SET diagnostics_json='{ \"x\": 1 }'")
        elif mutation == "scalar-source-digest":
            connection.execute("UPDATE evidence_snapshots SET source_digest='bad'")
        elif mutation == "scalar-count-sum":
            connection.execute("UPDATE evidence_snapshots SET supplied_row_count=2")
        elif mutation == "activation-malformed":
            connection.execute("UPDATE evidence_snapshot_activations SET canonical_json='{'")
        else:
            if mutation in {
                "activation-previous-id",
                "activation-previous-snapshot",
                "activation-snapshot",
            }:
                connection.execute("PRAGMA foreign_keys=OFF")
            row = connection.execute("SELECT * FROM evidence_snapshot_activations").fetchone()
            core = json.loads(row["canonical_json"])
            if mutation == "activation-core":
                core["activated_at"] = "different"
                canonical = json.dumps(core, sort_keys=True, separators=(",", ":"))
                connection.execute(
                    "UPDATE evidence_snapshot_activations SET canonical_json=?, activation_id=?",
                    (canonical, hashlib.sha256(canonical.encode()).hexdigest()),
                )
            elif mutation == "activation-digest":
                connection.execute(
                    "UPDATE evidence_snapshot_activations SET activation_id=?", ("f" * 64,)
                )
            elif mutation == "activation-revision":
                connection.execute("UPDATE evidence_snapshot_activations SET revision=2")
            elif mutation == "activation-previous-id":
                connection.execute(
                    "UPDATE evidence_snapshot_activations SET previous_activation_id='event:x'"
                )
            elif mutation == "activation-previous-snapshot":
                connection.execute(
                    "UPDATE evidence_snapshot_activations SET previous_snapshot_digest=?",
                    ("f" * 64,),
                )
            else:
                connection.execute(
                    "UPDATE evidence_snapshot_activations SET snapshot_digest=?", ("f" * 64,)
                )
        with pytest.raises(V2SourceIntegrityError):
            v2_import._verify_evidence(connection, set(v2_import._EVIDENCE_GROUP))


@pytest.mark.parametrize(
    "mutation",
    [
        "request-hash",
        "receipt-malformed",
        "receipt-noncanonical",
        "receipt-identity",
        "receipt-fingerprint",
        "receipt-proof-missing",
        "receipt-proof-digest",
        "receipt-predictions",
        "settlement-hash",
        "settlement-identity",
        "numeric-identity",
        "supersession",
        "outcome-hash",
        "outcome-caller",
    ],
)
def test_every_v2_shadow_and_settlement_integrity_failure_is_rejected(
    tmp_path: Path, mutation: str
) -> None:
    source = tmp_path / mutation / "v2.sqlite3"
    _create_current_ledger(source)
    _insert_valid_shadow_and_correction(source)
    with _sqlite(source) as connection:
        connection.row_factory = sqlite3.Row
        for table in (
            "prediction_requests",
            "shadow_receipts",
            "prediction_settlements",
            "numeric_outcome_revisions",
            "numeric_settlement_revisions",
        ):
            connection.execute(f"DROP TRIGGER {table}_no_update")
        if mutation == "request-hash":
            connection.execute("UPDATE prediction_requests SET request_hash='bad'")
        elif mutation == "receipt-malformed":
            connection.execute("UPDATE shadow_receipts SET core_json='{'")
        elif mutation == "receipt-noncanonical":
            core = json.loads(
                connection.execute("SELECT core_json FROM shadow_receipts").fetchone()[0]
            )
            connection.execute(
                "UPDATE shadow_receipts SET core_json=?", (json.dumps(core, indent=2),)
            )
        elif mutation == "receipt-identity":
            connection.execute("UPDATE shadow_receipts SET caller_id='caller:different'")
        elif mutation == "receipt-fingerprint":
            connection.execute("UPDATE shadow_receipts SET active_input_fingerprint='bad'")
        elif mutation in {
            "receipt-proof-missing",
            "receipt-proof-digest",
            "receipt-predictions",
        }:
            core = json.loads(
                connection.execute("SELECT core_json FROM shadow_receipts").fetchone()[0]
            )
            if mutation == "receipt-proof-missing":
                del core["active_input"]
            elif mutation == "receipt-proof-digest":
                core["active_input"]["fingerprint"] = "f" * 64
                connection.execute(
                    "UPDATE shadow_receipts SET active_input_fingerprint=?", ("f" * 64,)
                )
            else:
                core["predictions"] = []
            connection.execute(
                "UPDATE shadow_receipts SET core_json=?",
                (json.dumps(core, sort_keys=True, separators=(",", ":")),),
            )
        elif mutation == "settlement-hash":
            connection.execute("UPDATE prediction_settlements SET payload_hash='bad'")
        elif mutation == "settlement-identity":
            connection.execute(
                "UPDATE prediction_settlements SET competitor_id='competitor:different'"
            )
        elif mutation == "numeric-identity":
            connection.execute(
                "UPDATE numeric_settlement_revisions SET competitor_id='competitor:different'"
            )
        elif mutation == "supersession":
            connection.execute(
                "UPDATE numeric_settlement_revisions SET supersedes_revision_id=NULL"
            )
        elif mutation == "outcome-hash":
            connection.execute("UPDATE numeric_outcome_revisions SET payload_hash='bad'")
        else:
            connection.execute("UPDATE numeric_outcome_revisions SET caller_id='caller:different'")
        with pytest.raises(V2SourceIntegrityError):
            v2_import._verify_shadow_and_settlements(connection, set(v2_import._LEDGER_GROUP))


def test_source_mutation_at_commit_rolls_back_event_and_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    source.parent.mkdir()
    source.touch()

    def mutate(path: Path) -> None:
        path.write_bytes(path.read_bytes() + b"changed")

    monkeypatch.setattr(v2_import, "_before_import_commit_check", mutate)
    with pytest.raises(V2SourceChangedError):
        import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    with open_v3_connection(destination, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM v3_historical_imports").fetchone()[0] == 0


@pytest.mark.parametrize(
    "tamper",
    [
        "metadata",
        "event-missing",
        "event-corrupt",
        "event-projection",
        "row",
        "idempotency-missing",
        "idempotency-command",
        "idempotency-result",
        "head",
    ],
)
def test_exact_retry_reverifies_persisted_import_authority(tmp_path: Path, tamper: str) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    _create_evidence_source(source)
    import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    with open_v3_connection(destination) as connection:
        if tamper == "metadata":
            connection.execute("DROP TRIGGER v3_historical_imports_no_update")
            connection.execute(
                "UPDATE v3_historical_imports SET source_catalog_digest=?", ("f" * 64,)
            )
            connection.execute(
                "CREATE TRIGGER v3_historical_imports_no_update BEFORE UPDATE ON "
                "v3_historical_imports BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
        elif tamper == "event-missing":
            connection.execute("DROP TRIGGER v3_events_no_delete")
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DELETE FROM v3_events")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "CREATE TRIGGER v3_events_no_delete BEFORE DELETE ON v3_events "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
        elif tamper == "event-corrupt":
            connection.execute("DROP TRIGGER v3_events_no_update")
            connection.execute("UPDATE v3_events SET envelope_json='{'")
            connection.execute(
                "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON v3_events "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
        elif tamper == "event-projection":
            connection.execute("DROP TRIGGER v3_events_no_update")
            connection.execute("UPDATE v3_events SET command_id='command:different'")
            connection.execute(
                "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON v3_events "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
        elif tamper == "row":
            connection.execute("DROP TRIGGER v3_historical_import_rows_no_update")
            connection.execute("UPDATE v3_historical_import_rows SET row_digest=?", ("f" * 64,))
            connection.execute(
                "CREATE TRIGGER v3_historical_import_rows_no_update BEFORE UPDATE ON "
                "v3_historical_import_rows "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
        elif tamper == "idempotency-missing":
            connection.execute("DROP TRIGGER v3_idempotency_records_no_delete")
            connection.execute("DELETE FROM v3_idempotency_records")
            connection.execute(
                "CREATE TRIGGER v3_idempotency_records_no_delete BEFORE DELETE ON "
                "v3_idempotency_records "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
        elif tamper in {"idempotency-command", "idempotency-result"}:
            connection.execute("DROP TRIGGER v3_idempotency_records_no_update")
            column = "command_digest" if tamper == "idempotency-command" else "result_digest"
            connection.execute(
                f"UPDATE v3_idempotency_records SET {column}=?",  # noqa: S608 - fixed test column
                ("f" * 64,),
            )
            connection.execute(
                "CREATE TRIGGER v3_idempotency_records_no_update BEFORE UPDATE ON "
                "v3_idempotency_records "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
        else:
            connection.execute(
                "UPDATE v3_aggregate_heads SET event_digest=? WHERE aggregate_id=?",
                ("f" * 64, "system:v2-history"),
            )
    with pytest.raises(V2SourceIntegrityError, match="existing V3"):
        import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")


def test_new_import_rejects_stale_aggregate_head_and_rolls_back(tmp_path: Path) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    with _sqlite(source):
        pass
    import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    with open_v3_connection(destination) as connection:
        connection.execute(
            "UPDATE v3_aggregate_heads SET aggregate_version=0, event_digest=NULL "
            "WHERE aggregate_id='system:v2-history'"
        )
    with pytest.raises(V2SourceIntegrityError, match="aggregate head"):
        import_v2_snapshot(source, destination, cutoff="2026-08-23T00:00:00.000Z")
    with open_v3_connection(destination, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM v3_historical_imports").fetchone()[0] == 1


def test_concurrent_distinct_imports_serialize_sequences_heads_and_exact_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_source = tmp_path / "first.sqlite3"
    second_source = tmp_path / "second.sqlite3"
    with _sqlite(first_source):
        pass
    with _sqlite(second_source) as connection:
        connection.executescript(V2_RESULTS_SCHEMA)
        connection.executescript(V2_RESULTS_INDEX)
    destination = tmp_path / "v3.sqlite3"
    with open_v3_connection(destination) as connection:
        migrate_connection(connection)

    rendezvous = Barrier(2)
    original_transaction = v2_import.immediate_transaction

    @contextmanager
    def synchronized_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        rendezvous.wait(timeout=5)
        with original_transaction(connection) as transaction:
            yield transaction

    monkeypatch.setattr(v2_import, "immediate_transaction", synchronized_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(
                import_v2_snapshot,
                first_source,
                destination,
                cutoff="2026-08-22T00:00:00.000Z",
            ),
            pool.submit(
                import_v2_snapshot,
                second_source,
                destination,
                cutoff="2026-08-22T00:00:00.000Z",
            ),
        )
        results = tuple(future.result(timeout=10) for future in futures)
    monkeypatch.undo()

    with open_v3_connection(destination, read_only=True) as connection:
        events = connection.execute(
            "SELECT global_sequence, aggregate_version, event_digest, prior_global_digest, "
            "prior_aggregate_digest FROM v3_events ORDER BY global_sequence"
        ).fetchall()
        assert [tuple(row[:2]) for row in events] == [(1, 1), (2, 2)]
        assert events[1][3] == events[0][2]
        assert events[1][4] == events[0][2]
        assert tuple(
            connection.execute(
                "SELECT aggregate_version, event_digest FROM v3_aggregate_heads "
                "WHERE aggregate_kind='system' AND aggregate_id='system:v2-history'"
            ).fetchone()
        ) == (2, events[1][2])
        assert connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0] == 2

    assert (
        import_v2_snapshot(first_source, destination, cutoff="2026-08-22T00:00:00.000Z")
        == results[0]
    )
    assert (
        import_v2_snapshot(second_source, destination, cutoff="2026-08-22T00:00:00.000Z")
        == results[1]
    )


def test_concurrent_same_import_resolves_one_exact_idempotent_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.sqlite3"
    with _sqlite(source):
        pass
    destination = tmp_path / "v3.sqlite3"
    with open_v3_connection(destination) as connection:
        migrate_connection(connection)

    rendezvous = Barrier(2)
    original_transaction = v2_import.immediate_transaction

    @contextmanager
    def synchronized_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        rendezvous.wait(timeout=5)
        with original_transaction(connection) as transaction:
            yield transaction

    monkeypatch.setattr(v2_import, "immediate_transaction", synchronized_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(
            pool.submit(
                import_v2_snapshot,
                source,
                destination,
                cutoff="2026-08-22T00:00:00.000Z",
            )
            for _ in range(2)
        )
        results = tuple(future.result(timeout=10) for future in futures)
    monkeypatch.undo()
    assert results[0] == results[1]
    with open_v3_connection(destination, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0] == 1

    snapshot = v2_import._read_source_snapshot(
        source, cutoff="2026-08-22T00:00:00.000Z", deadline=None
    )
    with open_v3_connection(destination) as connection:
        with pytest.raises(V2SourceChangedError, match="exact retry resolution"):
            v2_import._persist_snapshot(
                connection,
                snapshot,
                source=source,
                expected_source_state=(),
            )


@pytest.mark.parametrize(
    "cutoff",
    ["2026-02-30T00:00:00.000Z", "2026-08-22T25:00:00.000Z", "2026-08-22"],
)
def test_import_cutoff_rejects_impossible_or_noncanonical_instants(
    tmp_path: Path, cutoff: str
) -> None:
    source = tmp_path / "v2.sqlite3"
    source.touch()
    with pytest.raises(V2ImportError, match="valid canonical"):
        import_v2_snapshot(source, tmp_path / "v3.sqlite3", cutoff=cutoff)


def test_v2_deadline_type_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "v2.sqlite3"
    with _sqlite(source):
        pass
    with pytest.raises(V2ImportError, match="SQLiteDeadline"):
        with open_v2_readonly(source, deadline=True):  # type: ignore[arg-type]
            pass


def test_cancelled_import_and_schema_rejection_leave_source_bytes_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "v2.sqlite3"
    with _sqlite(source) as connection:
        connection.execute("CREATE TABLE invented(id INTEGER PRIMARY KEY)")
    before = v2_import._source_file_manifest(source)
    deadline = v2_import.SQLiteDeadline(timeout_seconds=1)
    deadline.cancel()
    with pytest.raises(SQLiteDeadlineExceeded):
        import_v2_snapshot(
            source,
            tmp_path / "cancelled-v3.sqlite3",
            cutoff="2026-08-22T00:00:00.000Z",
            deadline=deadline,
        )
    assert v2_import._source_file_manifest(source) == before
    assert not (tmp_path / "cancelled-v3.sqlite3").exists()

    with pytest.raises(V2SourceSchemaError):
        import_v2_snapshot(
            source, tmp_path / "rejected-v3.sqlite3", cutoff="2026-08-22T00:00:00.000Z"
        )
    assert v2_import._source_file_manifest(source) == before
    assert not (tmp_path / "rejected-v3.sqlite3").exists()


def test_source_lock_fails_closed_and_leaves_both_databases_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "v2.sqlite3"
    with _sqlite(source):
        pass
    blocker = sqlite3.connect(source, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        before = v2_import._source_file_manifest(source)
        destination = tmp_path / "v3.sqlite3"
        with pytest.raises(V2SourceChangedError, match="checkpointed and closed"):
            import_v2_snapshot(
                source,
                destination,
                cutoff="2026-08-22T00:00:00.000Z",
                deadline=v2_import.SQLiteDeadline(timeout_seconds=0.05),
            )
        assert v2_import._source_file_manifest(source) == before
        assert not destination.exists()
    finally:
        blocker.rollback()
        blocker.close()


def test_active_wal_source_fails_closed_without_touching_source_or_v3(tmp_path: Path) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    source.parent.mkdir(parents=True)
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.executescript(V2_RESULTS_SCHEMA)
        writer.executescript(V2_RESULTS_INDEX)
        writer.commit()
        before = v2_import._source_file_manifest(source)
        assert before[1][1] is not None
        destination = tmp_path / "v3.sqlite3"
        with pytest.raises(V2SourceChangedError, match="checkpointed and closed"):
            import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
        assert v2_import._source_file_manifest(source) == before
        assert not destination.exists()
    finally:
        writer.close()


def test_snapshot_pragmas_fail_closed_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Rows:
        def __init__(self, values: list[tuple[object, ...]]) -> None:
            self.values = values

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.values

        def fetchone(self) -> tuple[object, ...] | None:
            return self.values[0] if self.values else None

    class Proxy:
        def __init__(self, connection: sqlite3.Connection, mode: str) -> None:
            self.connection = connection
            self.mode = mode
            self.versions = iter((1, 2))
            self.rollback_called = False

        @property
        def in_transaction(self) -> bool:
            return self.connection.in_transaction

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> object:
            if sql == "PRAGMA integrity_check" and self.mode == "integrity":
                return Rows([("corrupt",)])
            if sql == "PRAGMA integrity_check" and self.mode == "database-error":
                raise sqlite3.OperationalError("failed read")
            if sql == "PRAGMA integrity_check" and self.mode == "cancelled":
                cancelled.cancel()
                raise sqlite3.OperationalError("interrupted")
            if sql == "PRAGMA data_version" and self.mode == "version":
                return Rows([(next(self.versions),)])
            return self.connection.execute(sql, parameters)

        def commit(self) -> None:
            self.connection.commit()

        def rollback(self) -> None:
            self.rollback_called = True
            self.connection.rollback()

    proxies: list[Proxy] = []
    mode = "integrity"
    cancelled = v2_import.SQLiteDeadline(timeout_seconds=1)

    @contextmanager
    def fake_open(*_args: object, **_kwargs: object) -> Iterator[Proxy]:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        proxy = Proxy(connection, mode)
        proxies.append(proxy)
        try:
            yield proxy
        finally:
            connection.close()

    monkeypatch.setattr(v2_import, "open_v2_readonly", fake_open)
    with pytest.raises(V2SourceIntegrityError, match="integrity_check"):
        v2_import._read_source_snapshot(
            tmp_path / "unused.sqlite3", cutoff="2026-08-22T00:00:00.000Z", deadline=None
        )
    assert proxies[-1].rollback_called is True

    mode = "database-error"
    with pytest.raises(V2ImportError, match="locked past deadline"):
        v2_import._read_source_snapshot(
            tmp_path / "unused.sqlite3", cutoff="2026-08-22T00:00:00.000Z", deadline=None
        )
    assert proxies[-1].rollback_called is True

    mode = "cancelled"
    with pytest.raises(SQLiteDeadlineExceeded, match="cancelled"):
        v2_import._read_source_snapshot(
            tmp_path / "unused.sqlite3",
            cutoff="2026-08-22T00:00:00.000Z",
            deadline=cancelled,
        )
    assert proxies[-1].rollback_called is True

    mode = "version"
    with pytest.raises(V2SourceChangedError, match="data_version"):
        v2_import._read_source_snapshot(
            tmp_path / "unused.sqlite3", cutoff="2026-08-22T00:00:00.000Z", deadline=None
        )
    assert proxies[-1].rollback_called is True


def test_foreign_key_check_rejects_orphaned_v2_rows(tmp_path: Path) -> None:
    source = tmp_path / "v2.sqlite3"
    _create_current_ledger(source)
    with _sqlite(source) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO ledger_predictions(prediction_id, ledger_request_id, competitor_id, "
            "ordinal, event_code, median_seconds, assigned_mark, source, training_eligible, "
            "ignored_factors_json, warnings_json, optimizer_metadata_json, created_at) VALUES "
            "('prediction:orphan', 'request:missing', 'competitor:x', 0, 'SB', 30, 3, "
            "'baseline', 0, '[]', '[]', '{}', '2026-08-20T00:00:00+00:00')"
        )
    with pytest.raises(V2SourceIntegrityError, match="foreign_key_check"):
        import_v2_snapshot(source, tmp_path / "v3.sqlite3", cutoff="2026-08-22T00:00:00.000Z")


def test_double_read_and_exact_lookup_source_change_paths_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "v2.sqlite3"
    destination = tmp_path / "v3.sqlite3"
    with _sqlite(source):
        pass
    snapshot = v2_import._read_source_snapshot(
        source, cutoff="2026-08-22T00:00:00.000Z", deadline=None
    )
    snapshots = iter((snapshot, replace(snapshot, source_tip_digest="f" * 64)))
    monkeypatch.setattr(
        v2_import, "_read_source_snapshot", lambda *_args, **_kwargs: next(snapshots)
    )
    with pytest.raises(V2SourceChangedError, match="between consistent"):
        import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    assert not destination.exists()

    monkeypatch.undo()
    import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    stable = v2_import._source_file_manifest(source)
    changed = ((source.name, 1, "f" * 64),)
    observed = iter((stable, stable, changed, stable))
    monkeypatch.setattr(v2_import, "_source_file_manifest", lambda _source: next(observed))
    with pytest.raises(V2SourceChangedError, match="exact import lookup"):
        import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")


def test_profile_shape_catalog_and_request_variant_rejections(tmp_path: Path) -> None:
    malformed_results = tmp_path / "malformed-results.sqlite3"
    with _sqlite(malformed_results) as connection:
        connection.execute("CREATE TABLE results(id INTEGER PRIMARY KEY, unexpected TEXT)")
    with pytest.raises(V2SourceSchemaError, match="table semantics drifted: results"):
        import_v2_snapshot(
            malformed_results, tmp_path / "d1.sqlite3", cutoff="2026-08-22T00:00:00.000Z"
        )

    partial_evidence = tmp_path / "partial-evidence.sqlite3"
    _create_evidence_source(partial_evidence)
    with _sqlite(partial_evidence) as connection:
        connection.execute("DROP TABLE evidence_snapshot_activations")
        connection.execute("DROP TABLE evidence_snapshot_rows")
    with pytest.raises(V2SourceSchemaError, match="partial v2-evidence"):
        import_v2_snapshot(
            partial_evidence, tmp_path / "d2.sqlite3", cutoff="2026-08-22T00:00:00.000Z"
        )

    catalog = tmp_path / "catalog.sqlite3"
    with _sqlite(catalog) as connection:
        connection.executescript(
            V2_RESULTS_SCHEMA.replace(
                "UNIQUE(competitor_name, competition_id, heat_id, event_code, time_seconds)",
                "UNIQUE(competitor_name, event_code, competition_id, heat_id, time_seconds)",
            )
        )
        connection.executescript(V2_RESULTS_INDEX)
    with pytest.raises(V2SourceSchemaError, match="catalog semantics drifted"):
        import_v2_snapshot(catalog, tmp_path / "d3.sqlite3", cutoff="2026-08-22T00:00:00.000Z")

    core_hash = tmp_path / "core-hash.sqlite3"
    _create_legacy_ledger(core_hash, outbox=False)
    with _sqlite(core_hash) as connection:
        connection.execute(
            "ALTER TABLE prediction_requests ADD COLUMN hash_algorithm "
            "TEXT NOT NULL DEFAULT 'raw-v1' CHECK(hash_algorithm IN ('raw-v1', 'active-v2'))"
        )
        with pytest.raises(V2SourceSchemaError, match="not valid for this profile"):
            v2_import._request_variant(connection, allow_current=False)

    fresh_default = tmp_path / "fresh-default.sqlite3"
    _create_current_ledger(fresh_default)
    with _sqlite(fresh_default) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='prediction_requests'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='prediction_requests'",
            (sql.replace("DEFAULT 'active-v2'", "DEFAULT 'raw-v1'"),),
        )
        connection.execute("PRAGMA writable_schema=OFF")
    with _sqlite(fresh_default) as connection:
        with pytest.raises(V2SourceSchemaError, match="fresh V2 hash_algorithm default"):
            v2_import._request_variant(connection, allow_current=True)

    upgraded_default = tmp_path / "upgraded-default.sqlite3"
    _create_current_ledger(upgraded_default, upgraded=True)
    with _sqlite(upgraded_default) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='prediction_requests'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='prediction_requests'",
            (sql.replace("DEFAULT 'raw-v1'", "DEFAULT 'active-v2'"),),
        )
        connection.execute("PRAGMA writable_schema=OFF")
    with _sqlite(upgraded_default) as connection:
        with pytest.raises(V2SourceSchemaError, match="upgraded V2 hash_algorithm default"):
            v2_import._request_variant(connection, allow_current=True)

    unsupported = tmp_path / "unsupported-request.sqlite3"
    _create_current_ledger(unsupported, upgraded=True)
    with _sqlite(unsupported) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='prediction_requests'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='prediction_requests'",
            (sql.replace("hash_algorithm TEXT", "hash_algorithm INTEGER"),),
        )
        connection.execute("PRAGMA writable_schema=OFF")
    with _sqlite(unsupported) as connection:
        with pytest.raises(V2SourceSchemaError, match="unsupported"):
            v2_import._request_variant(connection, allow_current=True)


@pytest.mark.parametrize("tamper", ["kind", "command", "blob", "payload"])
def test_exact_retry_rejects_valid_event_that_does_not_bind_source_tip(
    tmp_path: Path, tamper: str
) -> None:
    source = tmp_path / "source" / "v2.sqlite3"
    destination = tmp_path / "destination" / "v3.sqlite3"
    _create_evidence_source(source)
    import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
    with open_v3_connection(destination) as connection:
        row = connection.execute("SELECT envelope_json FROM v3_events").fetchone()
        event = EventEnvelope.from_dict(json.loads(row[0]))
        command = event.command
        kind = event.kind
        if tamper == "kind":
            kind = EventKind.CHECKPOINT_ANCHORED
        elif tamper == "command":
            command = replace(command, kind=CommandKind.EMERGENCY_STOP)
        elif tamper == "blob":
            command = replace(
                command,
                payload=BlobReference(
                    blob_id=StableIdentifier("blob:wrong"),
                    digest="f" * 64,
                    byte_count=65_537,
                    media_type="application/json",
                ),
            )
        else:
            command = replace(command, payload=InlinePayload.from_value({"wrong": True}))
        wrong = EventEnvelope.create(
            event_id=event.event_id,
            kind=kind,
            aggregate_kind=event.aggregate_kind,
            aggregate_id=event.aggregate_id,
            aggregate_version=event.aggregate_version,
            global_sequence=event.global_sequence,
            prior_global_digest=event.prior_global_digest,
            prior_aggregate_digest=event.prior_aggregate_digest,
            occurred_at_utc=event.occurred_at_utc,
            monotonic_elapsed_ms=event.monotonic_elapsed_ms,
            command=command,
        )
        connection.execute("DROP TRIGGER v3_events_no_update")
        connection.execute(
            "UPDATE v3_events SET envelope_json=?, event_digest=?",
            (
                json.dumps(wrong.to_dict(), sort_keys=True, separators=(",", ":")),
                wrong.event_digest,
            ),
        )
        connection.execute(
            "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON v3_events "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
        )
    with pytest.raises(V2SourceIntegrityError, match="does not bind source tip"):
        import_v2_snapshot(source, destination, cutoff="2026-08-22T00:00:00.000Z")
