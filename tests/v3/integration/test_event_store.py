from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

import strathmark.v3.infrastructure.sqlite.event_store as event_store_module
from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import (
    MAX_INLINE_PAYLOAD_BYTES,
    BlobReference,
    CommandEnvelope,
    CommandKind,
    InlinePayload,
)
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import (
    AuthorityAnchor,
    EventStoreConflict,
    EventStoreIntegrityError,
    InjectedEventStoreFailure,
    SQLiteEventStore,
    StoredCommandResult,
)
from strathmark.v3.infrastructure.v2_import import import_v2_snapshot

NOW = "2026-08-22T18:00:00.000Z"


def _request(
    *,
    command_id: str = "command:configure-show",
    actor: str = "actor:judge",
    command_kind: CommandKind = CommandKind.CONFIGURE_TOURNAMENT,
    aggregate_kind: AggregateKind = AggregateKind.TOURNAMENT,
    aggregate_id: str = "tournament:show",
    expected: int = 0,
    event_kind: EventKind = EventKind.TOURNAMENT_CONFIGURED,
    result: dict[str, object] | None = None,
) -> CommandRequest:
    identifier = StableIdentifier(aggregate_id)
    command = CommandEnvelope(
        kind=command_kind,
        command_id=IdempotencyKey(command_id),
        target_aggregate=identifier,
        expected_versions=((aggregate_id, expected),),
        actor_id=StableIdentifier(actor),
        payload=InlinePayload.from_value({"aggregate_id": aggregate_id, "step": expected + 1}),
    )
    return CommandRequest(
        principal_id=StableIdentifier(actor),
        command=command,
        events=(EventIntent(aggregate_kind, identifier, event_kind),),
        result_schema_version="strathmark-v3-test-result-v1",
        result=result or {"accepted": True, "aggregate_id": aggregate_id},
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=expected + 1,
    )


def _batch_request(*, field_b_version: int = 1) -> CommandRequest:
    ids = ("field:a", "field:b", "issue_batch:round-1")
    command = CommandEnvelope(
        kind=CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
        command_id=IdempotencyKey("command:issue-round-1"),
        target_aggregate=StableIdentifier("issue_batch:round-1"),
        expected_versions=((ids[0], 1), (ids[1], field_b_version), (ids[2], 0)),
        actor_id=StableIdentifier("actor:judge"),
        payload=InlinePayload.from_value({"snapshot": "snapshot:sealed-1"}),
    )
    return CommandRequest(
        principal_id=StableIdentifier("actor:judge"),
        command=command,
        events=(
            EventIntent(AggregateKind.FIELD, StableIdentifier("field:a"), EventKind.FIELD_ISSUED),
            EventIntent(AggregateKind.FIELD, StableIdentifier("field:b"), EventKind.FIELD_ISSUED),
            EventIntent(
                AggregateKind.ISSUE_BATCH,
                StableIdentifier("issue_batch:round-1"),
                EventKind.ISSUE_BATCH_ISSUED,
            ),
        ),
        result_schema_version="strathmark-v3-batch-result-v1",
        result={"issued": ["field:a", "field:b"], "snapshot": "snapshot:sealed-1"},
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=10,
    )


def _prepare_field(store: SQLiteEventStore, field_id: str) -> None:
    store.execute(
        _request(
            command_id=f"command:prepare-{field_id.split(':')[1]}",
            command_kind=CommandKind.OPTIMIZE_FIELD,
            aggregate_kind=AggregateKind.FIELD,
            aggregate_id=field_id,
            event_kind=EventKind.FIELD_OPTIMIZED,
        )
    )


def test_exact_retry_returns_original_bytes_and_changed_command_conflicts(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "authority.sqlite3")
    request = _request(result={"z": 3, "a": "exact"})
    first = store.execute(request)
    second = store.execute(request)
    assert first is not second
    assert first.result_bytes == second.result_bytes == b'{"a":"exact","z":3}'
    assert first.event_ids == second.event_ids
    assert first.first_global_sequence == second.first_global_sequence == 1
    assert first.value() == {"a": "exact", "z": 3}

    changed = replace(
        request,
        command=replace(request.command, payload=InlinePayload.from_value({"changed": True})),
    )
    with pytest.raises(EventStoreConflict, match="different command"):
        store.execute(changed)

    other_actor_command = replace(
        request.command,
        actor_id=StableIdentifier("actor:other"),
    )
    other_actor = replace(
        request,
        principal_id=StableIdentifier("actor:other"),
        command=other_actor_command,
    )
    with pytest.raises(EventStoreConflict, match="claimed by another principal"):
        store.execute(other_actor)


def test_nested_result_snapshot_cannot_drift_before_execute(tmp_path: Path) -> None:
    original: dict[str, object] = {
        "receipt": {"revision": 1},
        "fields": ["field:a"],
    }
    request = replace(_request(), result=original)
    receipt = original["receipt"]
    fields = original["fields"]
    assert isinstance(receipt, dict)
    assert isinstance(fields, list)
    receipt["revision"] = 2
    fields.append("field:b")
    stored = SQLiteEventStore(tmp_path / "immutable-result.sqlite3").execute(request)
    assert stored.result_bytes == b'{"fields":["field:a"],"receipt":{"revision":1}}'


def test_lifecycle_expected_version_and_illegal_transition_fail_closed(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "authority.sqlite3")
    store.execute(_request())
    with pytest.raises(EventStoreConflict, match="expected version"):
        store.execute(_request(command_id="command:stale"))
    illegal = _request(
        command_id="command:close-too-soon",
        command_kind=CommandKind.CLOSE_TOURNAMENT,
        expected=1,
        event_kind=EventKind.TOURNAMENT_CLOSED,
    )
    with pytest.raises(EventStoreConflict, match="illegal"):
        store.execute(illegal)
    assert store.event_count() == 1


def test_each_writer_revalidates_global_and_aggregate_tips_inside_its_lock(
    tmp_path: Path,
) -> None:
    head_database = tmp_path / "head-race.sqlite3"
    head_store = SQLiteEventStore(head_database)
    head_store.execute(_request())
    with open_v3_connection(head_database) as connection:
        connection.execute("UPDATE v3_aggregate_heads SET event_digest=?", ("f" * 64,))
    with pytest.raises(EventStoreIntegrityError, match="head does not match"):
        head_store.execute(
            _request(
                command_id="command:open-after-head-tamper",
                command_kind=CommandKind.OPEN_TOURNAMENT,
                expected=1,
                event_kind=EventKind.TOURNAMENT_OPENED,
            )
        )

    global_database = tmp_path / "global-race.sqlite3"
    global_store = SQLiteEventStore(global_database)
    global_store.execute(_request())
    global_store.execute(_request(command_id="command:other", aggregate_id="tournament:other"))
    with open_v3_connection(global_database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TRIGGER v3_events_no_delete")
        connection.execute("DROP TRIGGER v3_idempotency_records_no_delete")
        connection.execute(
            "DELETE FROM v3_idempotency_records WHERE idempotency_key='command:configure-show'"
        )
        connection.execute("DELETE FROM v3_events WHERE global_sequence=1")
        connection.execute("DELETE FROM v3_aggregate_heads WHERE aggregate_id='tournament:show'")
    with pytest.raises(EventStoreIntegrityError, match="global sequence"):
        global_store.execute(_request(command_id="command:third", aggregate_id="tournament:third"))


def test_concurrent_duplicate_commits_once_and_distinct_aggregates_get_consecutive_global_versions(
    tmp_path: Path,
) -> None:
    store = SQLiteEventStore(tmp_path / "authority.sqlite3")
    duplicate = _request()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: store.execute(duplicate), range(16)))
    assert {item.result_bytes for item in results} == {results[0].result_bytes}
    assert {item.event_ids for item in results} == {results[0].event_ids}
    assert store.event_count() == 1

    requests = [
        _request(
            command_id=f"command:configure-{index}",
            aggregate_id=f"tournament:show-{index}",
        )
        for index in range(12)
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(store.execute, requests))
    assert store.global_sequences() == tuple(range(1, 14))
    store.verify()


def test_batch_issue_is_one_atomic_multi_aggregate_command(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "authority.sqlite3")
    _prepare_field(store, "field:a")
    _prepare_field(store, "field:b")
    result = store.execute(_batch_request())
    assert result.last_global_sequence - result.first_global_sequence == 2
    assert len(result.event_ids) == 3
    assert store.aggregate_head("field:a")[0] == 2
    assert store.aggregate_head("field:b")[0] == 2
    assert store.aggregate_head("issue_batch:round-1")[0] == 1

    stale = _batch_request(field_b_version=0)
    stale = replace(
        stale,
        command=replace(stale.command, command_id=IdempotencyKey("command:stale-batch")),
    )
    with pytest.raises(EventStoreConflict, match="expected version"):
        store.execute(stale)
    assert store.event_count() == 5


def test_independent_anchor_detects_coherent_tail_truncation(tmp_path: Path) -> None:
    database = tmp_path / "coherent-tail.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(_request())
    store.execute(
        _request(
            command_id="command:open-show",
            command_kind=CommandKind.OPEN_TOURNAMENT,
            expected=1,
            event_kind=EventKind.TOURNAMENT_OPENED,
        )
    )
    trusted = store.current_anchor()
    with open_v3_connection(database) as connection:
        first = connection.execute(
            "SELECT event_digest FROM v3_events WHERE global_sequence=1"
        ).fetchone()
        assert first is not None
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TRIGGER v3_events_no_delete")
        connection.execute("DROP TRIGGER v3_idempotency_records_no_delete")
        connection.execute(
            "DELETE FROM v3_idempotency_records WHERE idempotency_key='command:open-show'"
        )
        connection.execute("DELETE FROM v3_events WHERE global_sequence=2")
        connection.execute(
            "UPDATE v3_aggregate_heads SET aggregate_version=1, event_digest=?",
            (str(first[0]),),
        )
        connection.execute(
            "CREATE TRIGGER v3_events_no_delete BEFORE DELETE ON v3_events "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
        )
        connection.execute(
            "CREATE TRIGGER v3_idempotency_records_no_delete "
            "BEFORE DELETE ON v3_idempotency_records "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
        )

    # SQLite-only replay is internally coherent after a total tail erasure.
    # The independent U6 checkpoint boundary is therefore essential.
    SQLiteEventStore(database).verify()
    with pytest.raises(EventStoreIntegrityError, match="trusted checkpoint"):
        SQLiteEventStore(database, trusted_anchor=trusted)


def test_lagging_independent_anchor_proves_prefix_without_blocking_valid_tail(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lagging-anchor.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(_request())
    checkpoint = store.current_anchor()
    store.execute(
        _request(
            command_id="command:open-after-checkpoint",
            command_kind=CommandKind.OPEN_TOURNAMENT,
            expected=1,
            event_kind=EventKind.TOURNAMENT_OPENED,
        )
    )
    verified = SQLiteEventStore(database, trusted_anchor=checkpoint)
    assert verified.current_anchor().global_sequence == 2
    SQLiteEventStore(database, trusted_anchor=AuthorityAnchor(0, "0" * 64))
    with pytest.raises(EventStoreIntegrityError, match="trusted checkpoint"):
        SQLiteEventStore(database, trusted_anchor=AuthorityAnchor(1, "f" * 64))


def test_issue_replays_both_chains_under_writer_lock_after_post_startup_tamper(
    tmp_path: Path,
) -> None:
    database = tmp_path / "issue-time-integrity.sqlite3"
    store = SQLiteEventStore(database)
    _prepare_field(store, "field:a")
    before = store.event_count()
    _mutate_event(
        database,
        lambda connection: connection.execute(
            "UPDATE v3_events SET envelope_json='{' WHERE global_sequence=1"
        ),
    )
    issue = _request(
        command_id="command:issue-after-tamper",
        command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
        aggregate_kind=AggregateKind.FIELD,
        aggregate_id="field:a",
        expected=1,
        event_kind=EventKind.FIELD_ISSUED,
    )
    with pytest.raises(EventStoreIntegrityError):
        store.execute(issue)
    assert store.event_count() == before


def test_exact_issue_retry_replays_both_chains_before_returning_stored_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "issue-retry-integrity.sqlite3"
    store = SQLiteEventStore(database)
    _prepare_field(store, "field:a")
    issue = _request(
        command_id="command:issue-exact-retry",
        command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
        aggregate_kind=AggregateKind.FIELD,
        aggregate_id="field:a",
        expected=1,
        event_kind=EventKind.FIELD_ISSUED,
    )
    first = store.execute(issue)
    before = store.event_count()
    _mutate_event(
        database,
        lambda connection: connection.execute(
            "UPDATE v3_events SET envelope_json='{' WHERE global_sequence=1"
        ),
    )
    with pytest.raises(EventStoreIntegrityError):
        store.lookup_exact_retry(
            principal_id="actor:judge",
            idempotency_key="command:issue-exact-retry",
            command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
            target_aggregate="field:a",
            payload_digest=issue.command.payload_digest,
        )
    with pytest.raises(EventStoreIntegrityError):
        store.execute(issue)
    assert store.event_count() == before
    assert first.first_global_sequence == 2


@pytest.mark.parametrize(
    "point",
    [
        "before_event:0",
        "after_event:0",
        "after_head:0",
        "before_event:1",
        "after_event:1",
        "after_head:1",
        "before_event:2",
        "after_event:2",
        "after_head:2",
        "before_result",
        "after_result",
    ],
)
def test_injected_failure_at_each_append_head_and_result_point_rolls_back_everything(
    tmp_path: Path, point: str
) -> None:
    store = SQLiteEventStore(tmp_path / f"failure-{point.replace(':', '-')}.sqlite3")
    _prepare_field(store, "field:a")
    _prepare_field(store, "field:b")
    before = store.event_count()

    def fail(observed: str) -> None:
        if observed == point:
            raise InjectedEventStoreFailure(point)

    with pytest.raises(InjectedEventStoreFailure):
        store.execute(_batch_request(), fault_hook=fail)
    assert store.event_count() == before
    assert store.aggregate_head("field:a")[0] == 1
    assert store.aggregate_head("field:b")[0] == 1
    assert store.aggregate_head("issue_batch:round-1") is None


def _mutate_event(database: Path, operation: Callable[[sqlite3.Connection], None]) -> None:
    with open_v3_connection(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TRIGGER v3_events_no_update")
        connection.execute("DROP TRIGGER v3_events_no_delete")
        operation(connection)
        connection.execute(
            "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON v3_events "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
        )
        connection.execute(
            "CREATE TRIGGER v3_events_no_delete BEFORE DELETE ON v3_events "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "malformed-envelope",
        "noncanonical-envelope",
        "column-mismatch",
        "event-digest",
        "event-identity",
        "global-gap",
        "global-reorder",
        "global-prior",
        "stream-version",
        "stream-prior",
        "head-missing",
        "head-wrong",
        "idempotency-missing",
        "idempotency-result",
        "idempotency-event-set",
        "idempotency-command",
        "unsupported-kind",
        "illegal-lifecycle",
        "unsupported-aggregate",
        "training-flag",
        "ordinary-import-provenance",
        "tail-delete",
        "idempotency-result-json",
        "idempotency-schema",
        "idempotency-key",
        "idempotency-principal",
        "idempotency-created",
        "idempotency-range",
        "idempotency-duplicate-key",
    ],
)
def test_startup_verification_fails_closed_on_every_authority_tamper(
    tmp_path: Path, tamper: str
) -> None:
    database = tmp_path / f"tamper-{tamper}.sqlite3"
    store = SQLiteEventStore(database)
    store.execute(_request())
    store.execute(
        _request(
            command_id="command:open-show",
            command_kind=CommandKind.OPEN_TOURNAMENT,
            expected=1,
            event_kind=EventKind.TOURNAMENT_OPENED,
        )
    )
    if tamper == "head-missing":
        with open_v3_connection(database) as connection:
            connection.execute("DELETE FROM v3_aggregate_heads")
    elif tamper == "head-wrong":
        with open_v3_connection(database) as connection:
            connection.execute("UPDATE v3_aggregate_heads SET aggregate_version=1")
    elif tamper.startswith("idempotency-"):
        with open_v3_connection(database) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TRIGGER v3_idempotency_records_no_update")
            connection.execute("DROP TRIGGER v3_idempotency_records_no_delete")
            if tamper == "idempotency-missing":
                connection.execute(
                    "DELETE FROM v3_idempotency_records WHERE idempotency_key='command:open-show'"
                )
            elif tamper in {
                "idempotency-result",
                "idempotency-event-set",
                "idempotency-command",
            }:
                column = {
                    "idempotency-result": "result_digest",
                    "idempotency-event-set": "event_set_digest",
                    "idempotency-command": "command_digest",
                }[tamper]
                connection.execute(
                    f"UPDATE v3_idempotency_records SET {column}=? "  # noqa: S608 - fixed test map
                    "WHERE idempotency_key='command:open-show'",
                    ("f" * 64,),
                )
            elif tamper == "idempotency-result-json":
                connection.execute(
                    "UPDATE v3_idempotency_records SET result_json=? "
                    "WHERE idempotency_key='command:open-show'",
                    ('{"z":1, "a":2}',),
                )
            elif tamper == "idempotency-schema":
                connection.execute(
                    "UPDATE v3_idempotency_records SET result_schema_version='' "
                    "WHERE idempotency_key='command:open-show'"
                )
            elif tamper == "idempotency-key":
                connection.execute(
                    "UPDATE v3_idempotency_records SET idempotency_key='command:changed' "
                    "WHERE idempotency_key='command:open-show'"
                )
            elif tamper == "idempotency-principal":
                connection.execute(
                    "UPDATE v3_idempotency_records SET principal_id='actor:other' "
                    "WHERE idempotency_key='command:open-show'"
                )
            elif tamper == "idempotency-created":
                connection.execute(
                    "UPDATE v3_idempotency_records SET created_at='not-utc' "
                    "WHERE idempotency_key='command:open-show'"
                )
            elif tamper == "idempotency-range":
                connection.execute(
                    "UPDATE v3_idempotency_records SET last_global_sequence=99 "
                    "WHERE idempotency_key='command:open-show'"
                )
            else:
                connection.execute(
                    "INSERT INTO v3_idempotency_records SELECT 'actor:other', "
                    "idempotency_key, command_digest, result_schema_version, result_json, "
                    "result_digest, first_global_sequence, last_global_sequence, "
                    "event_set_digest, created_at FROM v3_idempotency_records "
                    "WHERE idempotency_key='command:open-show'"
                )
            connection.execute(
                "CREATE TRIGGER v3_idempotency_records_no_update "
                "BEFORE UPDATE ON v3_idempotency_records "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
            connection.execute(
                "CREATE TRIGGER v3_idempotency_records_no_delete "
                "BEFORE DELETE ON v3_idempotency_records "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
    else:

        def operation(connection: sqlite3.Connection) -> None:
            if tamper == "malformed-envelope":
                connection.execute("UPDATE v3_events SET envelope_json='{' WHERE global_sequence=2")
            elif tamper == "noncanonical-envelope":
                row = connection.execute(
                    "SELECT envelope_json FROM v3_events WHERE global_sequence=2"
                ).fetchone()
                assert row is not None
                connection.execute(
                    "UPDATE v3_events SET envelope_json=? WHERE global_sequence=2",
                    (json.dumps(json.loads(str(row[0])), sort_keys=True),),
                )
            elif tamper == "column-mismatch":
                connection.execute(
                    "UPDATE v3_events SET command_id='command:different' WHERE global_sequence=2"
                )
            elif tamper == "tail-delete":
                connection.execute("DELETE FROM v3_events WHERE global_sequence=2")
            elif tamper == "training-flag":
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(
                    "UPDATE v3_events SET training_eligible=2 WHERE global_sequence=2"
                )
            elif tamper == "ordinary-import-provenance":
                connection.execute(
                    "UPDATE v3_events SET source_import_id='v2import:invented' "
                    "WHERE global_sequence=2"
                )
            elif tamper == "global-reorder":
                rows = connection.execute(
                    "SELECT global_sequence, envelope_json FROM v3_events ORDER BY global_sequence"
                ).fetchall()
                values = [json.loads(str(row[1])) for row in rows]
                values[0]["global_sequence"] = 2
                values[1]["global_sequence"] = 1
                for value in values:
                    value["event_digest"] = canonical_digest(
                        {key: item for key, item in value.items() if key != "event_digest"}
                    )
                connection.execute(
                    "UPDATE v3_events SET global_sequence=99 WHERE global_sequence=1"
                )
                connection.execute("UPDATE v3_events SET global_sequence=1 WHERE global_sequence=2")
                connection.execute(
                    "UPDATE v3_events SET global_sequence=2 WHERE global_sequence=99"
                )
                for value in values:
                    connection.execute(
                        "UPDATE v3_events SET envelope_json=?, event_digest=? "
                        "WHERE global_sequence=?",
                        (
                            canonical_bytes(value).decode(),
                            value["event_digest"],
                            value["global_sequence"],
                        ),
                    )
            else:
                row = connection.execute(
                    "SELECT envelope_json FROM v3_events WHERE global_sequence=2"
                ).fetchone()
                assert row is not None
                value = json.loads(str(row[0]))
                if tamper == "event-digest":
                    value["event_digest"] = "f" * 64
                elif tamper == "event-identity":
                    value["event_id"] = "event:coherently-rewritten"
                    connection.execute(
                        "UPDATE v3_events SET event_id=? WHERE global_sequence=2",
                        (value["event_id"],),
                    )
                elif tamper == "global-gap":
                    value["global_sequence"] = 4
                    connection.execute(
                        "UPDATE v3_events SET global_sequence=4 WHERE global_sequence=2"
                    )
                elif tamper == "global-prior":
                    value["prior_global_digest"] = "f" * 64
                elif tamper == "stream-version":
                    value["aggregate_version"] = 4
                    value["command"]["expected_versions"] = [["tournament:show", 3]]
                    connection.execute(
                        "UPDATE v3_events SET aggregate_version=4 WHERE global_sequence=2"
                    )
                elif tamper == "stream-prior":
                    value["prior_aggregate_digest"] = "f" * 64
                elif tamper == "illegal-lifecycle":
                    value["kind"] = EventKind.TOURNAMENT_CLOSED.value
                    connection.execute(
                        "UPDATE v3_events SET event_kind=? WHERE global_sequence=2",
                        (value["kind"],),
                    )
                elif tamper == "unsupported-aggregate":
                    value["kind"] = EventKind.CHECKPOINT_ANCHORED.value
                    value["aggregate_kind"] = AggregateKind.SYSTEM.value
                    value["aggregate_id"] = "system:unsupported"
                    value["aggregate_version"] = 1
                    value["prior_aggregate_digest"] = "0" * 64
                    value["command"]["target_aggregate"] = "system:unsupported"
                    value["command"]["expected_versions"] = [["system:unsupported", 0]]
                    connection.execute(
                        "UPDATE v3_events SET aggregate_kind='system', "
                        "aggregate_id='system:unsupported', aggregate_version=1, "
                        "event_kind='checkpoint_anchored', prior_aggregate_digest=? "
                        "WHERE global_sequence=2",
                        ("0" * 64,),
                    )
                else:
                    value["kind"] = "future_event"
                if tamper in {"stream-version", "illegal-lifecycle", "unsupported-aggregate"}:
                    value["event_id"] = str(
                        deterministic_identifier(
                            "event",
                            {
                                "command_digest": canonical_digest(value["command"]),
                                "aggregate_id": value["aggregate_id"],
                                "aggregate_version": value["aggregate_version"],
                                "event_kind": value["kind"],
                            },
                        )
                    )
                    connection.execute(
                        "UPDATE v3_events SET event_id=? WHERE global_sequence IN (2, 4)",
                        (value["event_id"],),
                    )
                if tamper != "unsupported-kind" and tamper != "event-digest":
                    value["event_digest"] = canonical_digest(
                        {key: item for key, item in value.items() if key != "event_digest"}
                    )
                    updates = {
                        "global-prior": ("prior_global_digest", value["prior_global_digest"]),
                        "stream-prior": (
                            "prior_aggregate_digest",
                            value["prior_aggregate_digest"],
                        ),
                    }
                    if tamper in updates:
                        column, column_value = updates[tamper]
                        connection.execute(
                            f"UPDATE v3_events SET {column}=? WHERE global_sequence IN (2, 4)",  # noqa: S608
                            (column_value,),
                        )
                    connection.execute(
                        "UPDATE v3_events SET event_digest=? WHERE global_sequence IN (2, 4)",
                        (value["event_digest"],),
                    )
                connection.execute(
                    "UPDATE v3_events SET envelope_json=? WHERE global_sequence IN (2, 4)",
                    (canonical_bytes(value).decode(),),
                )

        _mutate_event(database, operation)
    with pytest.raises(EventStoreIntegrityError):
        SQLiteEventStore(database)


def test_u3_history_import_exact_retry_and_linkage_survive_general_store_replay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-v2.sqlite3"
    destination = tmp_path / "authority.sqlite3"
    with sqlite3.connect(source) as connection:
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
    first = import_v2_snapshot(source, destination, cutoff=NOW)
    second = import_v2_snapshot(source, destination, cutoff=NOW)
    assert first == second
    store = SQLiteEventStore(destination)
    assert store.event_count() == 1
    store.verify()
    assert store.current_anchor().global_sequence == 1


def test_history_import_missing_event_provenance_fails_general_replay(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    destination = tmp_path / "authority.sqlite3"
    with sqlite3.connect(source) as connection:
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
    import_v2_snapshot(source, destination, cutoff=NOW)
    _mutate_event(
        destination,
        lambda connection: connection.execute(
            "UPDATE v3_events SET source_import_id=NULL WHERE global_sequence=1"
        ),
    )
    with pytest.raises(EventStoreIntegrityError):
        SQLiteEventStore(destination)


def test_persisted_command_event_pair_mismatch_fails_after_valid_hash_rewrite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pair-mismatch.sqlite3"
    SQLiteEventStore(database).execute(_request())
    with open_v3_connection(database) as connection:
        connection.execute("DROP TRIGGER v3_events_no_update")
        connection.execute("DROP TRIGGER v3_idempotency_records_no_update")
        row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE global_sequence=1"
        ).fetchone()
        assert row is not None
        value = json.loads(str(row[0]))
        value["command"]["kind"] = CommandKind.OPEN_TOURNAMENT.value
        value["event_id"] = str(
            deterministic_identifier(
                "event",
                {
                    "command_digest": canonical_digest(value["command"]),
                    "aggregate_id": value["aggregate_id"],
                    "aggregate_version": value["aggregate_version"],
                    "event_kind": value["kind"],
                },
            )
        )
        value["event_digest"] = canonical_digest(
            {key: item for key, item in value.items() if key != "event_digest"}
        )
        envelope_json = canonical_bytes(value).decode()
        event_set_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-event-set-v1",
                "events": [
                    {
                        "global_sequence": 1,
                        "event_id": value["event_id"],
                        "event_digest": value["event_digest"],
                    }
                ],
            }
        )
        connection.execute(
            "UPDATE v3_events SET event_id=?, envelope_json=?, event_digest=? "
            "WHERE global_sequence=1",
            (value["event_id"], envelope_json, value["event_digest"]),
        )
        connection.execute(
            "UPDATE v3_aggregate_heads SET event_digest=?",
            (value["event_digest"],),
        )
        connection.execute(
            "UPDATE v3_idempotency_records SET command_digest=?, event_set_digest=?",
            (canonical_digest(value["command"]), event_set_digest),
        )
        connection.execute(
            "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON v3_events "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
        )
        connection.execute(
            "CREATE TRIGGER v3_idempotency_records_no_update "
            "BEFORE UPDATE ON v3_idempotency_records "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
        )
    with pytest.raises(EventStoreIntegrityError, match="command/event"):
        SQLiteEventStore(database)


def test_anchor_contract_is_closed() -> None:
    assert AuthorityAnchor(0, "0" * 64).global_sequence == 0
    for args in ((True, "0" * 64), (1, "0" * 64), (0, "f" * 64), (1, "X" * 64)):
        with pytest.raises(Exception, match="anchor"):
            AuthorityAnchor(*args)  # type: ignore[arg-type]


def test_stored_result_and_head_write_defenses_execute_with_controlled_fakes() -> None:
    malformed = StoredCommandResult(
        result_schema_version="result:v1",
        result_bytes=b"[]",
        result_digest="a" * 64,
        first_global_sequence=1,
        last_global_sequence=1,
        event_set_digest="b" * 64,
        event_ids=("event:one",),
    )
    with pytest.raises(EventStoreIntegrityError, match="not an object"):
        malformed.value()
    event_store_module._require_head_advance(SimpleNamespace(rowcount=1))
    with pytest.raises(EventStoreConflict, match="head changed"):
        event_store_module._require_head_advance(SimpleNamespace(rowcount=0))


def test_empty_anchor_properties_and_public_argument_guards(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite3"
    store = SQLiteEventStore(database)
    assert store.database_path == database.resolve()
    assert store.current_anchor() == AuthorityAnchor(0, "0" * 64)
    SQLiteEventStore(database, trusted_anchor=AuthorityAnchor(0, "0" * 64))
    for invalid in (True, object()):
        with pytest.raises(Exception, match="filesystem"):
            SQLiteEventStore(invalid)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="trusted_anchor"):
        SQLiteEventStore(database, trusted_anchor=object())  # type: ignore[arg-type]
    with pytest.raises(Exception, match="CommandRequest"):
        store.execute(object())  # type: ignore[arg-type]
    with pytest.raises(Exception, match="callable"):
        store.execute(_request(), fault_hook=object())  # type: ignore[arg-type]
    result = store.execute(_request())
    SQLiteEventStore(database, trusted_anchor=store.current_anchor())
    assert result.value()["accepted"] is True


def test_error_translation_and_defensive_verifier_wrappers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "wrappers.sqlite3"
    store = SQLiteEventStore(database)

    def sqlite_failure(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise sqlite3.IntegrityError("injected")

    monkeypatch.setattr(store, "_append_events", sqlite_failure)
    with pytest.raises(EventStoreConflict, match="concurrent authority"):
        store.execute(_request())

    def contract_failure(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise ContractError("injected contract")

    monkeypatch.setattr(store, "_append_events", contract_failure)
    with pytest.raises(EventStoreConflict, match="injected contract"):
        store.execute(_request(command_id="command:contract"))

    def verification_failure(_connection: object) -> None:
        raise RuntimeError("injected verification")

    monkeypatch.setattr(store, "_verify_events", verification_failure)
    with open_v3_connection(database) as connection:
        with pytest.raises(EventStoreIntegrityError, match="replay"):
            store._verify_connection(connection)

    def integrity_failure(_connection: object) -> None:
        raise EventStoreIntegrityError("injected integrity")

    monkeypatch.setattr(store, "_verify_connection", integrity_failure)
    with pytest.raises(EventStoreIntegrityError, match="injected integrity"):
        store.verify()


def test_startup_and_explicit_verify_wrap_nonintegrity_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "startup-wrap.sqlite3"
    store = SQLiteEventStore(database)

    def broken_migration(_connection: object) -> None:
        raise RuntimeError("broken migration")

    monkeypatch.setattr(event_store_module, "migrate_connection", broken_migration)
    with pytest.raises(EventStoreIntegrityError, match="startup"):
        SQLiteEventStore(tmp_path / "new.sqlite3")
    with pytest.raises(EventStoreIntegrityError, match="verification failed"):
        store.verify()


def test_historical_link_verifier_rejects_set_and_command_mismatch(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "links.sqlite3")

    class LinkConnection:
        @staticmethod
        def execute(sql: str):
            return [("v2import:only-event",)] if "source_import_id" in sql else []

    with pytest.raises(EventStoreIntegrityError, match="do not reconcile"):
        store._verify_historical_import_links(LinkConnection(), {})  # type: ignore[arg-type]

    wrong = SimpleNamespace(
        kind=EventKind.HISTORY_IMPORTED,
        command=SimpleNamespace(kind=CommandKind.CONFIGURE_TOURNAMENT),
    )
    with pytest.raises(EventStoreIntegrityError, match="wrong command"):
        store._verify_historical_import_links(
            SimpleNamespace(execute=lambda _sql: []),  # type: ignore[arg-type]
            {1: wrong},  # type: ignore[dict-item]
        )


def test_history_event_identity_requires_inline_manifest_and_valid_tip() -> None:
    aggregate = StableIdentifier("system:v2-history")
    base = CommandEnvelope(
        kind=CommandKind.IMPORT_HISTORY,
        command_id=IdempotencyKey("command:history-contract"),
        target_aggregate=aggregate,
        expected_versions=((str(aggregate), 0),),
        actor_id=StableIdentifier("actor:v2-readonly-import"),
        payload=InlinePayload.from_value({}),
    )

    def event(command: CommandEnvelope) -> EventEnvelope:
        return EventEnvelope.create(
            event_id=StableIdentifier("event:history-contract"),
            kind=EventKind.HISTORY_IMPORTED,
            aggregate_kind=AggregateKind.SYSTEM,
            aggregate_id=aggregate,
            aggregate_version=1,
            global_sequence=1,
            prior_global_digest="0" * 64,
            prior_aggregate_digest="0" * 64,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=0,
            command=command,
        )

    with pytest.raises(EventStoreIntegrityError, match="source tip"):
        event_store_module._expected_event_id(event(base))
    blob = BlobReference(
        StableIdentifier("blob:history"),
        "a" * 64,
        MAX_INLINE_PAYLOAD_BYTES + 1,
        "application/json",
    )
    with pytest.raises(EventStoreIntegrityError, match="inline"):
        event_store_module._expected_event_id(event(replace(base, payload=blob)))


def test_command_boundary_rejects_spoofed_principal_unsorted_scope_and_wrong_event() -> None:
    request = _request()
    with pytest.raises(Exception, match="credential-derived"):
        replace(request, principal_id=StableIdentifier("actor:spoofed"))
    with pytest.raises(Exception, match="do not match"):
        replace(
            request,
            events=(
                EventIntent(
                    AggregateKind.TOURNAMENT,
                    StableIdentifier("tournament:show"),
                    EventKind.TOURNAMENT_CLOSED,
                ),
            ),
        )
