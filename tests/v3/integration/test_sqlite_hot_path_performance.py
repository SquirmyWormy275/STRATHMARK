from __future__ import annotations

from pathlib import Path

import pytest

from strathmark.v3.contracts.commands import CommandKind
from strathmark.v3.contracts.events import EventKind
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import (
    EventStoreIntegrityError,
    SQLiteEventStore,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection
from strathmark.v3.infrastructure.sqlite.outbox import OutboxError, OutboxRepository


def _plan(database: Path, sql: str, parameters: tuple[object, ...] = ()) -> str:
    with open_v3_connection(database) as connection:
        migrate_connection(connection)
        rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall()
    return " | ".join(str(row[3]) for row in rows)


def test_job_hot_path_queries_use_state_leading_indexes(tmp_path: Path) -> None:
    database = tmp_path / "job-query-plans.sqlite3"
    cases = (
        (
            "SELECT COUNT(*) FROM v3_jobs WHERE state IN ('queued','leased','retryable-failed')",
            (),
            "v3_jobs_active_state_idx",
        ),
        (
            "SELECT * FROM v3_jobs WHERE state='leased' AND lease_expires_at<=?",
            ("2026-08-25T00:00:00.000Z",),
            "v3_jobs_expired_lease_idx",
        ),
        (
            "SELECT * FROM v3_jobs WHERE state='retryable-failed' AND not_before_at<=?",
            ("2026-08-25T00:00:00.000Z",),
            "v3_jobs_retry_ready_idx",
        ),
        (
            "SELECT * FROM v3_jobs WHERE state='queued' AND hard_deadline_at<=?",
            ("2026-08-25T00:00:00.000Z",),
            "v3_jobs_queued_deadline_idx",
        ),
    )

    for sql, parameters, expected_index in cases:
        plan = _plan(database, sql, parameters)
        assert "SCAN v3_jobs" not in plan
        assert expected_index in plan


def test_job_hot_path_plan_is_invariant_to_large_terminal_history(tmp_path: Path) -> None:
    database = tmp_path / "job-history-invariance.sqlite3"
    sql = "SELECT * FROM v3_jobs WHERE state='queued' AND hard_deadline_at<=?"
    before = _plan(database, sql, ("2026-08-25T00:00:00.000Z",))
    with open_v3_connection(database) as connection:
        connection.execute(
            "WITH RECURSIVE n(value) AS (VALUES(1) UNION ALL "
            "SELECT value+1 FROM n WHERE value<10000) "
            "INSERT INTO v3_jobs(job_id,job_revision,idempotency_key,job_kind,lane,"
            "resource_class,base_priority,capacity_use_json,payload_json,payload_digest,"
            "evidence_digest,bundle_digest,retry_policy_version,state,attempt_count,"
            "max_attempts,initial_not_before_at,not_before_at,hard_deadline_at,lease_owner,"
            "lease_acquired_at,lease_expires_at,fencing_token,terminal_reason,result_digest,"
            "created_at,updated_at) SELECT 'job:terminal-'||value,1,"
            "'job_request:terminal-'||value,'maintenance','maintenance','storage_io',1,"
            "'{}','{}',?,?,?, 'retry.v1','succeeded',1,1,?,NULL,?,NULL,NULL,NULL,1,"
            "'complete',?, ?,? FROM n",
            (
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "2026-08-01T00:00:00.000Z",
                "2026-08-02T00:00:00.000Z",
                "e" * 64,
                "2026-08-01T00:00:00.000Z",
                "2026-08-01T00:00:01.000Z",
            ),
        )
        connection.execute("ANALYZE")
    after = _plan(database, sql, ("2026-08-25T00:00:00.000Z",))
    assert "v3_jobs_queued_deadline_idx" in before
    assert "v3_jobs_queued_deadline_idx" in after
    assert "SCAN v3_jobs" not in after


def test_event_append_uses_bounded_head_after_startup_deep_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.v3.integration.test_event_store import _request

    store = SQLiteEventStore(tmp_path / "event-bounded.sqlite3")
    store.execute(_request())

    def forbidden_deep_replay(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("normal append replayed event history from genesis")

    monkeypatch.setattr(store, "_verify_connection", forbidden_deep_replay)
    store.execute(
        _request(
            command_id="command:bounded-open",
            command_kind=CommandKind.OPEN_TOURNAMENT,
            expected=1,
            event_kind=EventKind.TOURNAMENT_OPENED,
        )
    )
    assert store.verify_bounded_head().global_sequence == 2

    with open_v3_connection(store.database_path) as connection:
        connection.execute(
            "UPDATE v3_event_authority_checkpoint SET checkpoint_digest=? WHERE singleton=1",
            ("f" * 64,),
        )
    with pytest.raises(EventStoreIntegrityError, match="checkpoint digest"):
        store.verify_bounded_head()


def test_outbox_due_targets_signed_selected_rows_without_genesis_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.sqlite.outbox as outbox_module

    database = tmp_path / "outbox-bounded.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:hotpath-outbox")
    repository = OutboxRepository(
        database,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        active_key_id=signer.identity.key_id,
    )
    repository.enqueue(
        outbox_id="outbox:bounded",
        destination="mirror",
        payload={"receipt_id": "receipt:bounded"},
        created_at="2026-08-25T00:00:00.000Z",
    )

    def forbidden_genesis(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("due polling replayed outbox history from genesis")

    monkeypatch.setattr(outbox_module, "verify_outbox_integrity", forbidden_genesis)
    assert len(repository.due("2026-08-25T00:00:00.000Z", limit=1)) == 1

    with open_v3_connection(database) as connection:
        connection.execute(
            "UPDATE v3_outbox_item_checkpoints SET item_digest=? WHERE outbox_id='outbox:bounded'",
            ("f" * 64,),
        )
    with pytest.raises(OutboxError, match="signed checkpoint"):
        repository.due("2026-08-25T00:00:00.000Z", limit=1)


def test_receipt_and_approval_reads_do_not_invoke_deep_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.infrastructure.sqlite.projections as projection_module
    from strathmark.v3.application.field_assembly import FieldAssemblyService
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    store, field, build, _lifecycle = _bootstrap(tmp_path / "projection-bounded.sqlite3")
    assembled = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:bounded-projection",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )

    def forbidden_deep_rebuild(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bounded projection read invoked a deep rebuild")

    monkeypatch.setattr(store, "verify", forbidden_deep_rebuild)
    monkeypatch.setattr(
        projection_module,
        "_verify_live_approval_projection_connection",
        forbidden_deep_rebuild,
    )
    assert store.verified_receipt(str(assembled.receipt.receipt_id)) == assembled.receipt
    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=1)
    assert tuple(row.receipt_id for row in page.rows) == (str(assembled.receipt.receipt_id),)
