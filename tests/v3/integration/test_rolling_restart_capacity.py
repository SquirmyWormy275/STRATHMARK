from __future__ import annotations

import ctypes
import json
import os
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import strathmark.v3.infrastructure.sqlite.event_store as event_store_module
import strathmark.v3.infrastructure.sqlite.jobs as jobs_module
import strathmark.v3.infrastructure.sqlite.rolling_restart as rolling_restart_module
from strathmark.v3.application.capacity import (
    CapacityManifest,
    CapacityUse,
    JobKind,
    JobLane,
    JobPriority,
    LaneCapacity,
)
from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.application.coordinator import (
    DurableRollingPreparationCoordinator,
)
from strathmark.v3.application.job_ports import (
    DurableJobError,
    RollingRestartExpectedHead,
    RollingRestartTrust,
    RollingRestartTrustMode,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import (
    CommandEnvelope,
    CommandKind,
    InlinePayload,
)
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    bounded_checkpoint,
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
from strathmark.v3.infrastructure.sqlite.jobs import (
    DurableJobRepository,
    JobRecord,
    JobRequest,
    JobState,
)
from strathmark.v3.infrastructure.sqlite.migrations import DEFAULT_MIGRATIONS, migrate_connection
from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore
from strathmark.v3.infrastructure.sqlite.rolling_restart import (
    RollingRestartIntegrityError,
    reset_rolling_reaction_cursor,
)

NOW = "2026-08-24T18:00:00.000Z"


def _capacity() -> CapacityManifest:
    return CapacityManifest(
        schema_version="strathmark-v3-job-capacity-v1",
        max_open_tournaments=1,
        max_round_entrants=48,
        max_field_entrants=12,
        max_plausible_qualifiers=48,
        max_context_cards=48,
        max_queued_jobs=16,
        max_receipt_bytes=1_048_576,
        max_blob_bytes=16_777_216,
        max_api_page_size=100,
        reserved_imminent_jobs=1,
        reserved_recovery_jobs=1,
        aging_interval_ms=1_000,
        aging_increment=125,
        lanes=(
            LaneCapacity(JobLane.HOT_FIELD, 4, 2),
            LaneCapacity(JobLane.INFERENCE, 12, 4),
            LaneCapacity(JobLane.LOOKUP_RECOVERY, 4, 2),
            LaneCapacity(JobLane.MAINTENANCE, 4, 1),
        ),
    )


def _repository(database: Path) -> tuple[DurableJobRepository, P256EphemeralSigner]:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-restart")
    repository = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    return repository, signer


def _job_request(ordinal: int = 1) -> JobRequest:
    return JobRequest.create(
        job_id=f"job:restart-{ordinal}",
        job_revision=1,
        idempotency_key=f"job_request:restart-{ordinal}",
        job_kind=JobKind.FORMULA_CARD,
        lane=JobLane.INFERENCE,
        priority=JobPriority.IMMINENT_FIELD,
        capacity_use=CapacityUse(0, 0, 0, 0, 0, 0, 0, 0),
        payload={"schema_version": "test-job-v1", "ordinal": ordinal},
        evidence_digest="a" * 64,
        bundle_digest="b" * 64,
        retry_policy_version="retry.v1",
        created_at=NOW,
        not_before_at=NOW,
        hard_deadline_at="2026-08-24T19:00:00.000Z",
        max_attempts=2,
    )


def _install_populated_v12_job(database: Path, signer: P256EphemeralSigner) -> JobRecord:
    with open_v3_connection(database) as connection:
        migrate_connection(connection, migrations=DEFAULT_MIGRATIONS[:12])
        request = _job_request()
        record = JobRecord(
            job_id=str(request.job_id),
            job_revision=request.job_revision,
            idempotency_key=str(request.idempotency_key),
            job_kind=request.job_kind,
            lane=request.lane,
            resource_class=request.resource_class,
            priority=request.priority,
            capacity_use_json=request.capacity_use_json,
            payload_json=request.payload_json,
            payload_digest=request.payload_digest,
            evidence_digest=request.evidence_digest,
            bundle_digest=request.bundle_digest,
            retry_policy_version=request.retry_policy_version,
            state=JobState.QUEUED,
            attempt_count=0,
            max_attempts=request.max_attempts,
            initial_not_before_at=request.not_before_at,
            not_before_at=request.not_before_at,
            hard_deadline_at=request.hard_deadline_at,
            lease_owner=None,
            lease_acquired_at=None,
            lease_expires_at=None,
            fencing_token=0,
            terminal_reason=None,
            result_digest=None,
            created_at=request.created_at,
            updated_at=request.created_at,
        )
        connection.execute(
            "INSERT INTO v3_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            jobs_module._record_storage_values(record),
        )
        history = {
            "schema_version": jobs_module.JOB_RESULT_SCHEMA_VERSION,
            "history_sequence": 1,
            "job_id": record.job_id,
            "job_revision": record.job_revision,
            "operation_kind": "queued",
            "from_state": None,
            "result_state": "queued",
            "attempt_count": 0,
            "fencing_token": 0,
            "lease_owner": None,
            "lease_acquired_at": None,
            "lease_expires_at": None,
            "not_before_at": record.not_before_at,
            "terminal_reason": None,
            "result_digest": None,
            "observed_at": record.created_at,
            "prior_history_digest": "0" * 64,
            "job_material_digest": jobs_module._record_material_digest(record),
        }
        history_digest = canonical_digest(history)
        authority = sign_manifest(
            "job_transition", history, signer=signer, created_at=record.created_at
        )
        connection.execute(
            "INSERT INTO v3_job_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                f"job_transition:{history_digest}",
                record.job_id,
                record.job_revision,
                "queued",
                None,
                "queued",
                0,
                0,
                None,
                None,
                None,
                record.not_before_at,
                None,
                None,
                record.created_at,
                "0" * 64,
                history_digest,
                history["job_material_digest"],
                authority.body_json,
                authority.body_digest,
                authority.key_id,
                authority.signature_der_b64,
            ),
        )
    return record


def _append_test_checkpoints(
    repository: DurableJobRepository, database: Path
) -> tuple[RollingRestartExpectedHead, RollingRestartExpectedHead]:
    with open_v3_connection(database) as connection:
        with immediate_transaction(connection):
            second = repository._append_rolling_restart_checkpoint(connection, NOW)
            third = repository._append_rolling_restart_checkpoint(connection, NOW)
    return (
        RollingRestartExpectedHead(second.checkpoint_sequence, second.checkpoint_digest),
        RollingRestartExpectedHead(third.checkpoint_sequence, third.checkpoint_digest),
    )


def _append_tail_event(
    database: Path,
    ordinal: int = 0,
    *,
    event_store: SQLiteEventStore | None = None,
) -> None:
    suffix = "tail-test" if ordinal == 0 else f"tail-test-{ordinal}"
    aggregate_id = StableIdentifier(f"tournament:{suffix}")
    command = CommandEnvelope(
        kind=CommandKind.CONFIGURE_TOURNAMENT,
        command_id=IdempotencyKey(f"command:{suffix}"),
        target_aggregate=aggregate_id,
        expected_versions=((str(aggregate_id), 0),),
        actor_id=StableIdentifier("actor:judge"),
        payload=InlinePayload.from_value({"tournament_id": str(aggregate_id)}),
    )
    (SQLiteEventStore(database) if event_store is None else event_store).execute(
        CommandRequest(
            principal_id=StableIdentifier("actor:judge"),
            command=command,
            events=(
                EventIntent(
                    AggregateKind.TOURNAMENT,
                    aggregate_id,
                    EventKind.TOURNAMENT_CONFIGURED,
                ),
            ),
            result_schema_version="strathmark-v3-tail-test-v1",
            result={"accepted": True},
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
    )


def test_rolling_restart_migration_adds_checkpoint_tip_and_hot_indexes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "migration.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('v3_rolling_restart_checkpoints',"
                "'v3_rolling_restart_tip','v3_rolling_reaction_cursor')"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'v3_%rolling%idx'"
            )
        }
    assert tables == {
        "v3_rolling_restart_checkpoints",
        "v3_rolling_restart_tip",
        "v3_rolling_reaction_cursor",
    }
    assert {
        "v3_jobs_rolling_card_component_idx",
        "v3_jobs_rolling_subject_revision_idx",
        "v3_jobs_rolling_epoch_state_idx",
        "v3_jobs_rolling_recombination_epoch_state_idx",
        "v3_rolling_status_publication_idx",
    } <= indexes


def test_rolling_reaction_cursor_helper_requires_atomic_writer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cursor-transaction.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection)
        with pytest.raises(RollingRestartIntegrityError, match="writer transaction"):
            reset_rolling_reaction_cursor(connection)


def test_populated_upgrade_requires_projection_cutover_before_job_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "reverse-order-cutover.sqlite3"
    with open_v3_connection(database) as connection:
        migrate_connection(connection, migrations=DEFAULT_MIGRATIONS[:11])
    original_migrate = event_store_module.migrate_connection
    original_advance = rolling_restart_module.advance_rolling_reaction_cursor
    original_require = rolling_restart_module.require_rolling_reaction_cursor_at_event_head
    monkeypatch.setattr(event_store_module, "migrate_connection", lambda _connection: 0)
    monkeypatch.setattr(
        rolling_restart_module,
        "advance_rolling_reaction_cursor",
        lambda _connection, _events: None,
    )
    monkeypatch.setattr(
        rolling_restart_module,
        "require_rolling_reaction_cursor_at_event_head",
        lambda _connection: None,
    )
    _append_tail_event(database)
    monkeypatch.setattr(event_store_module, "migrate_connection", original_migrate)
    monkeypatch.setattr(
        rolling_restart_module,
        "advance_rolling_reaction_cursor",
        original_advance,
    )
    monkeypatch.setattr(
        rolling_restart_module,
        "require_rolling_reaction_cursor_at_event_head",
        original_require,
    )
    with open_v3_connection(database) as connection:
        migrate_connection(connection)

    signer = P256EphemeralSigner.generate("integrity-key:reverse-cutover")
    trust = IntegrityTrustStore((signer.identity,))
    with pytest.raises(DurableJobError, match="projection cursor cutover"):
        DurableJobRepository(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=trust,
        )
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT through_global_sequence FROM v3_rolling_reaction_cursor"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM v3_rolling_restart_checkpoints").fetchone()[0]
            == 0
        )

    projections = SQLiteProjectionStore(database)
    assert projections.bootstrap_rolling_reaction_cursor_cutover() == 1
    repository = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=trust,
    )
    assert repository.recover_rolling_restart().source_global_sequence == 1


@pytest.mark.parametrize(
    ("tamper_sql", "drop_trigger", "restore_trigger"),
    (
        (
            "DELETE FROM v3_idempotency_records WHERE idempotency_key='command:tail-test'",
            "DROP TRIGGER v3_idempotency_records_no_delete",
            "CREATE TRIGGER v3_idempotency_records_no_delete "
            "BEFORE DELETE ON v3_idempotency_records "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END",
        ),
        (
            "UPDATE v3_events SET prior_global_digest='" + "f" * 64 + "' WHERE global_sequence=1",
            "DROP TRIGGER v3_events_no_update",
            "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON v3_events "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END",
        ),
        (
            "UPDATE v3_events SET occurred_at_utc='2026-08-24T18:00:00.001Z' "
            "WHERE global_sequence=1",
            "DROP TRIGGER v3_events_no_update",
            "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON v3_events "
            "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END",
        ),
    ),
)
def test_first_populated_checkpoint_requires_verified_event_authority(
    tmp_path: Path,
    tamper_sql: str,
    drop_trigger: str,
    restore_trigger: str,
) -> None:
    database = tmp_path / "first-populated-checkpoint.sqlite3"
    _append_tail_event(database)
    signer = P256EphemeralSigner.generate("integrity-key:first-checkpoint")
    with open_v3_connection(database) as connection:
        connection.execute(drop_trigger)
        connection.execute(tamper_sql)
        connection.execute(restore_trigger)
    with pytest.raises(DurableJobError, match="event authority"):
        DurableJobRepository(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
        )
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM v3_rolling_restart_checkpoints").fetchone()[0]
            == 0
        )


def test_coordinator_restart_uses_bounded_checkpoint_not_full_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, signer = _repository(tmp_path / "restart.sqlite3")
    receipt = repository.recover_rolling_restart()
    assert receipt.current_subject_count == 0
    assert receipt.active_job_count == 0
    assert receipt.pending_reaction_count == 0

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("critical restart called a lifetime-history operation")

    bounded_repository = DurableJobRepository(
        repository.database_path,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        restart_trust=RollingRestartTrust.externally_anchored(
            RollingRestartExpectedHead(receipt.checkpoint_sequence, receipt.checkpoint_digest)
        ),
    )
    for name in (
        "verify",
        "verify_rolling_storage",
        "rolling_publication_rows",
        "_verify_connection",
        "_verify_rolling_storage_connection",
        "_verify_rolling_restart_checkpoint_history_connection",
    ):
        monkeypatch.setattr(bounded_repository, name, forbidden)
    coordinator = DurableRollingPreparationCoordinator(
        bounded_repository,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert coordinator.readiness((), observed_at=NOW).total_cards == 0


def test_externally_anchored_restart_rejects_coherent_local_head_rollback(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rollback.sqlite3"
    repository, signer = _repository(database)
    second, third = _append_test_checkpoints(repository, database)
    with open_v3_connection(database) as connection:
        connection.execute("DROP TRIGGER v3_rolling_restart_checkpoints_no_delete")
        connection.execute(
            "UPDATE v3_rolling_restart_tip SET checkpoint_sequence=?,checkpoint_digest=? "
            "WHERE singleton=1",
            (second.checkpoint_sequence, second.checkpoint_digest),
        )
        connection.execute(
            "DELETE FROM v3_rolling_restart_checkpoints WHERE checkpoint_sequence=?",
            (third.checkpoint_sequence,),
        )
        connection.execute(
            "CREATE TRIGGER v3_rolling_restart_checkpoints_no_delete "
            "BEFORE DELETE ON v3_rolling_restart_checkpoints BEGIN "
            "SELECT RAISE(ABORT, 'rolling restart checkpoints are immutable'); END"
        )
    with pytest.raises(DurableJobError, match="external rolling head rolled back"):
        DurableJobRepository(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            restart_trust=RollingRestartTrust.externally_anchored(third),
        )


def test_local_corruption_deep_audit_rejects_deleted_checkpoint_prefix(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prefix.sqlite3"
    repository, signer = _repository(database)
    _append_test_checkpoints(repository, database)
    with open_v3_connection(database) as connection:
        connection.execute("DROP TRIGGER v3_rolling_restart_checkpoints_no_delete")
        connection.execute("DELETE FROM v3_rolling_restart_checkpoints WHERE checkpoint_sequence=1")
        connection.execute(
            "CREATE TRIGGER v3_rolling_restart_checkpoints_no_delete "
            "BEFORE DELETE ON v3_rolling_restart_checkpoints BEGIN "
            "SELECT RAISE(ABORT, 'rolling restart checkpoints are immutable'); END"
        )
    with pytest.raises(DurableJobError, match="checkpoint history has a gap"):
        DurableJobRepository(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
            restart_trust=RollingRestartTrust.local_corruption_only(),
        )


def test_restart_receipt_names_local_corruption_only_trust(tmp_path: Path) -> None:
    repository, _signer = _repository(tmp_path / "local-mode.sqlite3")
    receipt = repository.recover_rolling_restart()
    assert receipt.trust_mode is RollingRestartTrustMode.LOCAL_CORRUPTION_ONLY


def test_bounded_authenticated_event_tail_appends_delta(tmp_path: Path) -> None:
    database = tmp_path / "tail-refresh.sqlite3"
    repository, _signer = _repository(database)
    before = repository.recover_rolling_restart()
    _append_tail_event(database)
    after = repository.recover_rolling_restart()
    assert after.source_global_sequence == 1
    assert after.checkpoint_sequence == before.checkpoint_sequence
    with open_v3_connection(database, read_only=True) as connection:
        delta = connection.execute(
            "SELECT operation_kind,authority_kind,authority_sequence,authority_digest "
            "FROM v3_rolling_restart_deltas ORDER BY delta_sequence DESC LIMIT 1"
        ).fetchone()
        assert tuple(delta[:3]) == ("event_tail_verified", "event_tail", 1)
        event_digest = connection.execute(
            "SELECT event_digest FROM v3_events WHERE global_sequence=1"
        ).fetchone()[0]
        assert str(delta[3]) == str(event_digest)


def test_event_tail_over_signed_capacity_requires_explicit_deep_audit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tail-capacity.sqlite3"
    capacity = replace(
        _capacity(),
        max_round_entrants=1,
        max_field_entrants=1,
        max_plausible_qualifiers=1,
        max_context_cards=1,
    )
    signer = P256EphemeralSigner.generate("integrity-key:tail-capacity")
    repository = DurableJobRepository(
        database,
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    for ordinal in range(1, 23):
        _append_tail_event(database, ordinal)
    with pytest.raises(DurableJobError, match="authenticated deep audit"):
        repository.recover_rolling_restart()
    recovered = repository.recover_rolling_restart_deep_audit()
    assert recovered.source_global_sequence == 22
    restarted = DurableJobRepository(
        database,
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert restarted.recover_rolling_restart().checkpoint_digest == (recovered.checkpoint_digest)


def test_deep_recovery_rejects_concurrent_event_head_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "deep-recovery-race.sqlite3"
    capacity = replace(
        _capacity(),
        max_round_entrants=1,
        max_field_entrants=1,
        max_plausible_qualifiers=1,
        max_context_cards=1,
    )
    signer = P256EphemeralSigner.generate("integrity-key:deep-recovery-race")
    repository = DurableJobRepository(
        database,
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    for ordinal in range(1, 23):
        _append_tail_event(database, ordinal)
    monkeypatch.setattr(
        repository,
        "_before_deep_rolling_recovery_commit",
        lambda: _append_tail_event(database, 99),
    )
    with pytest.raises(DurableJobError, match="authority changed before commit"):
        repository.recover_rolling_restart_deep_audit()
    monkeypatch.setattr(repository, "_before_deep_rolling_recovery_commit", lambda: None)
    recovered = repository.recover_rolling_restart_deep_audit()
    assert recovered.source_global_sequence == 23


def test_deep_recovery_rejects_u5_projection_change_after_offline_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.v3.integration.test_field_receipts import _bootstrap

    database = tmp_path / "deep-recovery-u5-race.sqlite3"
    store, _field, _build, _lifecycle = _bootstrap(database)
    repository = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=store._signer,
        trust_store=store._trust_store,
    )
    with open_v3_connection(database, read_only=True) as connection:
        checkpoint_count = int(
            connection.execute("SELECT COUNT(*) FROM v3_rolling_restart_checkpoints").fetchone()[0]
        )

    def forge_current_field_projection() -> None:
        with open_v3_connection(database) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' ORDER BY source_global_sequence DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            value = json.loads(str(row[0]))
            value["call_order"] = int(value["call_order"]) + 1
            encoded = canonical_bytes(value).decode("utf-8")
            connection.execute(
                "UPDATE v3_ingress_snapshots SET snapshot_json=?,snapshot_digest=? "
                "WHERE entity_kind='field'",
                (encoded, canonical_digest(value)),
            )

    monkeypatch.setattr(
        repository,
        "_before_deep_rolling_recovery_commit",
        forge_current_field_projection,
    )
    with pytest.raises(DurableJobError, match="projection material changed"):
        repository.recover_rolling_restart_deep_audit()
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_rolling_restart_checkpoints"
                ).fetchone()[0]
            )
            == checkpoint_count
        )


@pytest.mark.parametrize(
    ("table", "mutation"),
    (
        (
            "v3_evidence_epochs",
            "UPDATE v3_evidence_epochs SET historical_cutoff_key='history:forged'",
        ),
        (
            "v3_result_revisions",
            "UPDATE v3_result_revisions SET admission_reason='forged'",
        ),
        (
            "v3_prepared_field_dependencies",
            "UPDATE v3_prepared_field_dependencies SET round_id='round:forged'",
        ),
    ),
)
def test_deep_recovery_rejects_each_u5_projection_family_changed_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    mutation: str,
) -> None:
    from strathmark.v3.contracts.statuses import ResultStatus
    from tests.v3.integration.test_derivation_barrier import (
        _bootstrap as bootstrap_lifecycle,
    )
    from tests.v3.integration.test_derivation_barrier import (
        _submission,
    )

    service, _round_id, field_id = bootstrap_lifecycle(tmp_path / table)
    service.record_live_result(
        _submission(field_id, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey(f"command:{table}-result"),
        actor_id=StableIdentifier("actor:tournament-manager"),
        occurred_at_utc="2026-08-22T01:02:03.004Z",
        monotonic_elapsed_ms=4,
    )
    database = service.projections.database_path
    signer = P256EphemeralSigner.generate(f"integrity-key:{table}")
    repository = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    with open_v3_connection(database, read_only=True) as connection:
        assert int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) > 0
        checkpoint_count = int(
            connection.execute("SELECT COUNT(*) FROM v3_rolling_restart_checkpoints").fetchone()[0]
        )

    def forge_projection() -> None:
        with open_v3_connection(database) as connection:
            connection.execute(mutation)

    monkeypatch.setattr(repository, "_before_deep_rolling_recovery_commit", forge_projection)
    with pytest.raises(DurableJobError, match="projection material changed"):
        repository.recover_rolling_restart_deep_audit()
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_rolling_restart_checkpoints"
                ).fetchone()[0]
            )
            == checkpoint_count
        )


@pytest.mark.parametrize(
    ("tamper_sql", "message"),
    (
        (
            "UPDATE v3_events SET prior_aggregate_digest='" + "f" * 64 + "' "
            "WHERE global_sequence=1",
            "event tail integrity",
        ),
        (
            "DELETE FROM v3_aggregate_heads WHERE aggregate_id='tournament:tail-test'",
            "aggregate heads differ",
        ),
        (
            "DELETE FROM v3_idempotency_records WHERE idempotency_key='command:tail-test'",
            "tail idempotency differs",
        ),
    ),
)
def test_bounded_tail_rejects_wrong_prior_missing_head_and_missing_idempotency(
    tmp_path: Path, tamper_sql: str, message: str
) -> None:
    database = tmp_path / f"tail-tamper-{canonical_name(message)}.sqlite3"
    repository, _signer = _repository(database)
    _append_tail_event(database)
    with open_v3_connection(database) as connection:
        if tamper_sql.startswith("UPDATE v3_events"):
            connection.execute("DROP TRIGGER v3_events_no_update")
        elif tamper_sql.startswith("DELETE FROM v3_idempotency_records"):
            connection.execute("DROP TRIGGER v3_idempotency_records_no_delete")
        connection.execute(tamper_sql)
        if tamper_sql.startswith("UPDATE v3_events"):
            connection.execute(
                "CREATE TRIGGER v3_events_no_update BEFORE UPDATE ON v3_events "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
        elif tamper_sql.startswith("DELETE FROM v3_idempotency_records"):
            connection.execute(
                "CREATE TRIGGER v3_idempotency_records_no_delete "
                "BEFORE DELETE ON v3_idempotency_records "
                "BEGIN SELECT RAISE(ABORT, 'append-only authority'); END"
            )
    with pytest.raises(DurableJobError, match=message):
        repository.recover_rolling_restart()


def canonical_name(value: str) -> str:
    return value.replace(" ", "-")


def _windows_rss_bytes() -> int:
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = (
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        )

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    process = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.working_set_size)


def _sqlite_material_bytes(database: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (database, Path(f"{database}-wal"))
        if candidate.exists()
    )


def test_declared_capacity_checkpoint_writer_p99_rss_and_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "declared-capacity-checkpoint.sqlite3"
    capacity = CapacityManifest.load("benchmarks/v3/job_capacity_manifest.json")
    signer = P256EphemeralSigner.generate("integrity-key:declared-capacity")
    repository = DurableJobRepository(
        database,
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    checkpoint_before_fixture = repository.recover_rolling_restart()
    original_checkpoint = repository._append_rolling_restart_checkpoint
    original_delta = repository._append_rolling_restart_delta
    monkeypatch.setattr(
        repository,
        "_append_rolling_restart_checkpoint",
        lambda *_args, **_kwargs: checkpoint_before_fixture,
    )
    monkeypatch.setattr(
        repository,
        "_append_rolling_restart_delta",
        lambda *_args, **_kwargs: "0" * 64,
    )
    zero_use = CapacityUse(0, 0, 0, 0, 0, 0, 0, 0)
    ordinal = 0
    for kind, count, priority in (
        (JobKind.FORMULA_CARD, 256, JobPriority.IMMINENT_FIELD),
        (JobKind.HOT_FIELD_ASSEMBLY, 64, JobPriority.IMMINENT_FIELD),
        (JobKind.RECEIPT_LOOKUP, 64, JobPriority.RECOVERY),
    ):
        for _ in range(count):
            ordinal += 1
            repository.enqueue(
                JobRequest.create(
                    job_id=f"job:capacity-{ordinal:03d}",
                    job_revision=1,
                    idempotency_key=f"job_request:capacity-{ordinal:03d}",
                    job_kind=kind,
                    lane=kind.lane,
                    priority=priority,
                    capacity_use=zero_use,
                    payload={
                        "schema_version": "strathmark-v3-rolling-component-job-v1",
                        "card_key": {"tournament_epoch_id": "epoch:capacity"},
                    },
                    evidence_digest="a" * 64,
                    bundle_digest="b" * 64,
                    retry_policy_version="retry.v1",
                    created_at=NOW,
                    not_before_at=NOW,
                    hard_deadline_at="2026-08-24T19:00:00.000Z",
                    max_attempts=2,
                )
            )
    monkeypatch.setattr(repository, "_append_rolling_restart_checkpoint", original_checkpoint)
    monkeypatch.setattr(repository, "_append_rolling_restart_delta", original_delta)
    event_store = SQLiteEventStore(database)
    for event_ordinal in range(1, 1_025):
        _append_tail_event(
            database,
            10_000 + event_ordinal,
            event_store=event_store,
        )
    repository.recover_rolling_restart_deep_audit()
    with open_v3_connection(database) as connection:
        checkpoint_result = bounded_checkpoint(connection)
        assert checkpoint_result.busy == 0
    claimed = repository.claim(
        JobLane.INFERENCE,
        worker_id="worker:capacity",
        clock=lambda: "2026-08-24T18:00:01.000Z",
        lease_duration_ms=300_000,
    )
    assert claimed is not None

    writer_ms: list[float] = []
    writer_service_ms: list[float] = []
    hot_writer_ms: list[float] = []
    hot_writer_service_ms: list[float] = []
    end_to_end_ms: list[float] = []
    end_to_end_service_ms: list[float] = []
    original_transaction = jobs_module.immediate_transaction

    @contextmanager
    def measured_transaction(connection: object):
        started = time.perf_counter_ns()
        service_started = time.thread_time_ns()
        try:
            with original_transaction(connection):
                yield
        finally:
            writer_service_ms.append((time.thread_time_ns() - service_started) / 1_000_000)
            writer_ms.append((time.perf_counter_ns() - started) / 1_000_000)

    monkeypatch.setattr(jobs_module, "immediate_transaction", measured_transaction)
    rss_before = _windows_rss_bytes()
    bytes_before = _sqlite_material_bytes(database)
    compacted = None
    for sample in range(200):
        offset = sample + 2
        observed_at = f"2026-08-24T18:{offset // 60:02d}:{offset % 60:02d}.000Z"
        started = time.perf_counter_ns()
        service_started = time.thread_time_ns()
        repository.heartbeat(
            claimed.job_id,
            claimed.job_revision,
            worker_id="worker:capacity",
            fencing_token=claimed.fencing_token,
            observed_at=observed_at,
            extend_ms=300_000,
        )
        end_to_end_service_ms.append((time.thread_time_ns() - service_started) / 1_000_000)
        end_to_end_ms.append((time.perf_counter_ns() - started) / 1_000_000)
        hot_writer_ms.append(writer_ms[-1])
        hot_writer_service_ms.append(writer_service_ms[-1])
        if (sample + 1) % 40 == 0:
            compacted = repository.refresh_rolling_restart_checkpoint_if_due(
                observed_at=observed_at,
                delta_threshold=40,
            )
            assert compacted is not None
            with open_v3_connection(database) as connection:
                checkpoint_result = bounded_checkpoint(connection)
                assert checkpoint_result.busy == 0
    rss_growth = max(0, _windows_rss_bytes() - rss_before)
    database_growth = _sqlite_material_bytes(database) - bytes_before
    with open_v3_connection(database, read_only=True) as connection:
        latest_checkpoint_bytes = int(
            connection.execute(
                "SELECT length(CAST(checkpoint_manifest_json AS BLOB))+"
                "length(CAST(aggregate_heads_json AS BLOB))+"
                "length(CAST(current_subjects_json AS BLOB))+"
                "length(CAST(pending_reactions_json AS BLOB)) "
                "FROM v3_rolling_restart_checkpoints "
                "ORDER BY checkpoint_sequence DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_jobs WHERE state IN ('queued','leased','retryable-failed')"
            ).fetchone()[0]
            == 384
        )
    ordered_writer = sorted(hot_writer_ms)
    ordered_writer_service = sorted(hot_writer_service_ms)
    ordered_total = sorted(end_to_end_ms)
    ordered_service = sorted(end_to_end_service_ms)
    p99_index = int(len(end_to_end_ms) * 0.99) - 1
    metrics = {
        "aggregate_heads": 1_024,
        "active_jobs": 384,
        "samples": len(end_to_end_ms),
        "writer_wall_p99_ms": round(ordered_writer[p99_index], 3),
        "writer_service_p99_ms": round(ordered_writer_service[p99_index], 3),
        "caller_wall_p99_ms": round(ordered_total[p99_index], 3),
        "service_active_p99_ms": round(ordered_service[p99_index], 3),
        "rss_growth_bytes": rss_growth,
        "latest_checkpoint_bytes": latest_checkpoint_bytes,
        "database_growth_per_transition": database_growth // len(end_to_end_ms),
    }
    assert compacted is not None
    with open_v3_connection(database) as connection:
        connection.execute(
            "UPDATE v3_jobs SET base_priority=base_priority+1 WHERE job_id=? AND job_revision=?",
            (claimed.job_id, claimed.job_revision),
        )
    rebuild_started = time.perf_counter()
    assert repository.rebuild_job_projection() == 1
    metrics["job_projection_rebuild_seconds"] = round(time.perf_counter() - rebuild_started, 3)
    restart_started = time.perf_counter()
    restarted = DurableJobRepository(
        database,
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        restart_trust=RollingRestartTrust.externally_anchored(
            RollingRestartExpectedHead(compacted.checkpoint_sequence, compacted.checkpoint_digest)
        ),
    )
    restarted.recover_rolling_restart()
    metrics["critical_restart_seconds"] = round(time.perf_counter() - restart_started, 3)
    controlled_profile = os.environ.get("STRATHMARK_V3_CONTROLLED_PERF_GATE") == "1"
    metrics["controlled_profile_wall_limit_ms"] = 100
    controlled_wall_passed = (
        metrics["writer_wall_p99_ms"] <= 100 and metrics["caller_wall_p99_ms"] <= 100
    )
    metrics["controlled_profile_wall_gate"] = (
        "passed"
        if controlled_profile and controlled_wall_passed
        else "failed"
        if controlled_profile
        else "not_evaluated_shared_host"
    )
    metrics["release_capacity_manifest"] = (
        "certified_green"
        if controlled_profile and controlled_wall_passed
        else "failed"
        if controlled_profile
        else "red_not_certified"
    )
    print(json.dumps(metrics, sort_keys=True))
    assert metrics["writer_service_p99_ms"] <= 100
    assert metrics["service_active_p99_ms"] <= 100
    if controlled_profile:
        assert metrics["writer_wall_p99_ms"] <= 100
        assert metrics["caller_wall_p99_ms"] <= 100
    assert metrics["rss_growth_bytes"] <= 256 * 1024 * 1024
    assert metrics["critical_restart_seconds"] <= 5
    assert metrics["job_projection_rebuild_seconds"] <= 30


def test_restart_checkpoint_tamper_fails_before_current_recovery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tamper.sqlite3"
    repository, signer = _repository(database)
    repository.recover_rolling_restart()
    with open_v3_connection(database) as connection:
        connection.execute("DROP TRIGGER v3_rolling_restart_checkpoints_no_update")
        row = connection.execute(
            "SELECT checkpoint_sequence,checkpoint_manifest_json "
            "FROM v3_rolling_restart_checkpoints ORDER BY checkpoint_sequence DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        manifest = json.loads(str(row[1]))
        manifest["body_digest"] = "f" * 64
        connection.execute(
            "UPDATE v3_rolling_restart_checkpoints SET checkpoint_manifest_json=? "
            "WHERE checkpoint_sequence=?",
            (json.dumps(manifest, separators=(",", ":")), int(row[0])),
        )
        connection.execute(
            "CREATE TRIGGER v3_rolling_restart_checkpoints_no_update "
            "BEFORE UPDATE ON v3_rolling_restart_checkpoints BEGIN "
            "SELECT RAISE(ABORT, 'rolling restart checkpoints are immutable'); END"
        )
    with pytest.raises(DurableJobError, match="restart checkpoint"):
        DurableJobRepository(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
        )


@pytest.mark.parametrize(
    "damage", ("tip_missing", "tip_corrupt", "cursor_missing", "cursor_corrupt")
)
def test_restart_repairs_disposable_tip_and_cursor_then_restarts_cleanly(
    tmp_path: Path, damage: str
) -> None:
    database = tmp_path / f"repair-{damage}.sqlite3"
    repository, signer = _repository(database)
    expected = repository.recover_rolling_restart()
    with open_v3_connection(database) as connection:
        if damage == "tip_missing":
            connection.execute("DELETE FROM v3_rolling_restart_tip")
        elif damage == "tip_corrupt":
            connection.execute(
                "UPDATE v3_rolling_restart_tip SET checkpoint_digest=? WHERE singleton=1",
                ("f" * 64,),
            )
        elif damage == "cursor_missing":
            connection.execute("DELETE FROM v3_rolling_reaction_cursor")
        else:
            connection.execute(
                "UPDATE v3_rolling_reaction_cursor SET cursor_digest=? WHERE singleton=1",
                ("f" * 64,),
            )
    repaired = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert repaired.recover_rolling_restart().checkpoint_digest == expected.checkpoint_digest
    restarted = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert restarted.recover_rolling_restart().checkpoint_digest == expected.checkpoint_digest


@pytest.mark.parametrize("damage", ("missing", "corrupt"))
def test_restart_repairs_cursor_after_verified_bounded_event_tail(
    tmp_path: Path, damage: str
) -> None:
    database = tmp_path / f"repair-tail-cursor-{damage}.sqlite3"
    repository, signer = _repository(database)
    _append_tail_event(database)
    with open_v3_connection(database) as connection:
        if damage == "missing":
            connection.execute("DELETE FROM v3_rolling_reaction_cursor")
        else:
            connection.execute(
                "UPDATE v3_rolling_reaction_cursor SET cursor_digest=? WHERE singleton=1",
                ("f" * 64,),
            )
    repaired = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert repaired.recover_rolling_restart().source_global_sequence == 1


@pytest.mark.parametrize("damage", ("mutated", "deleted"))
def test_job_projection_rebuilds_exactly_from_signed_spec_and_history(
    tmp_path: Path, damage: str
) -> None:
    database = tmp_path / f"job-projection-{damage}.sqlite3"
    repository, _signer = _repository(database)
    expected = repository.enqueue(_job_request())
    with open_v3_connection(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        spec = connection.execute(
            "SELECT spec_digest,spec_manifest_json FROM v3_job_specs "
            "WHERE job_id=? AND job_revision=?",
            (expected.job_id, expected.job_revision),
        ).fetchone()
        assert spec is not None
        if damage == "deleted":
            connection.execute(
                "DELETE FROM v3_jobs WHERE job_id=? AND job_revision=?",
                (expected.job_id, expected.job_revision),
            )
        else:
            forged_payload = {"schema_version": "forged-job-v1"}
            forged_capacity = CapacityUse(0, 0, 0, 0, 0, 0, 0, 1)
            connection.execute(
                "UPDATE v3_jobs SET job_id='job:forged',job_revision=2,"
                "idempotency_key='job_request:forged',job_kind='ml_card',"
                "lane='inference',resource_class='local_cpu',base_priority=300,"
                "capacity_use_json=?,payload_json=?,payload_digest=?,evidence_digest=?,"
                "bundle_digest=?,retry_policy_version='retry.v2',state='leased',"
                "attempt_count=7,max_attempts=3,initial_not_before_at=?,"
                "not_before_at=NULL,lease_owner='worker:forged',lease_acquired_at=?,"
                "lease_expires_at=?,fencing_token=8,terminal_reason=NULL,"
                "result_digest=NULL,created_at=?,hard_deadline_at=?,updated_at=? "
                "WHERE job_id=? AND job_revision=?",
                (
                    canonical_bytes(forged_capacity.to_dict()).decode("utf-8"),
                    canonical_bytes(forged_payload).decode("utf-8"),
                    canonical_digest(forged_payload),
                    "c" * 64,
                    "d" * 64,
                    "2026-08-24T18:00:00.001Z",
                    "2026-08-24T18:00:01.000Z",
                    "2026-08-24T18:01:01.000Z",
                    "2026-08-24T18:00:00.001Z",
                    "2026-08-24T20:00:00.000Z",
                    "2026-08-24T18:00:01.000Z",
                    expected.job_id,
                    expected.job_revision,
                ),
            )
    assert repository.rebuild_job_projection() >= 1
    assert repository.get(expected.job_id, expected.job_revision) == expected
    assert repository.rebuild_job_projection() == 0


@pytest.mark.parametrize("damage", ("spec", "history"))
def test_job_projection_rebuild_rejects_damaged_immutable_authority(
    tmp_path: Path, damage: str
) -> None:
    database = tmp_path / f"job-authority-{damage}.sqlite3"
    repository, _signer = _repository(database)
    expected = repository.enqueue(_job_request())
    with open_v3_connection(database) as connection:
        if damage == "spec":
            connection.execute("DROP TRIGGER v3_job_specs_no_update")
            connection.execute(
                "UPDATE v3_job_specs SET spec_digest=? WHERE job_id=? AND job_revision=?",
                ("f" * 64, expected.job_id, expected.job_revision),
            )
            connection.execute(
                "CREATE TRIGGER v3_job_specs_no_update BEFORE UPDATE ON v3_job_specs "
                "BEGIN SELECT RAISE(ABORT, 'job specs are immutable'); END"
            )
        else:
            connection.execute("DROP TRIGGER v3_job_history_no_update")
            connection.execute(
                "UPDATE v3_job_history SET history_digest=? WHERE job_id=? AND job_revision=?",
                ("f" * 64, expected.job_id, expected.job_revision),
            )
            connection.execute(
                "CREATE TRIGGER v3_job_history_no_update BEFORE UPDATE ON v3_job_history "
                "BEGIN SELECT RAISE(ABORT, 'append-only history'); END"
            )
    with pytest.raises(DurableJobError, match="job (spec|history)"):
        repository.rebuild_job_projection()


def test_populated_v12_jobs_require_explicit_signed_spec_cutover(
    tmp_path: Path,
) -> None:
    database = tmp_path / "populated-v12-jobs.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:job-spec-cutover")
    expected = _install_populated_v12_job(database, signer)
    trust_store = IntegrityTrustStore((signer.identity,))
    with open_v3_connection(database) as connection:
        migrate_connection(connection)

    with pytest.raises(DurableJobError, match="job spec cutover is required"):
        DurableJobRepository(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=trust_store,
        )
    assert (
        DurableJobRepository.bootstrap_job_spec_authority_cutover(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=trust_store,
        )
        == 1
    )
    repository = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=trust_store,
    )
    assert repository.get(expected.job_id, expected.job_revision) == expected
    leased = repository.claim(
        JobLane.INFERENCE,
        worker_id="worker:cutover",
        clock=lambda: "2026-08-24T18:00:01.000Z",
        lease_duration_ms=60_000,
    )
    assert leased is not None
    with open_v3_connection(database, read_only=True) as connection:
        assert tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT job_spec_digest FROM v3_job_history ORDER BY history_sequence"
            )
        ) == ("0" * 64, canonical_digest(jobs_module._job_spec_value(expected)))
    assert repository.rebuild_job_projection() == 0
    assert (
        DurableJobRepository.bootstrap_job_spec_authority_cutover(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=trust_store,
        )
        == 0
    )


def test_populated_job_spec_cutover_rejects_partial_state(tmp_path: Path) -> None:
    database = tmp_path / "partial-job-spec-cutover.sqlite3"
    signer = P256EphemeralSigner.generate("integrity-key:partial-job-spec-cutover")
    expected = _install_populated_v12_job(database, signer)
    trust_store = IntegrityTrustStore((signer.identity,))
    with open_v3_connection(database) as connection:
        migrate_connection(connection)
        value = jobs_module._job_spec_value(expected)
        manifest = sign_manifest("job_spec", value, signer=signer, created_at=expected.created_at)
        connection.execute(
            "INSERT INTO v3_job_specs VALUES (?,?,?,?,?,?)",
            (
                expected.job_id,
                expected.job_revision,
                canonical_bytes(value).decode("utf-8"),
                canonical_digest(value),
                canonical_bytes(manifest.to_dict()).decode("utf-8"),
                expected.created_at,
            ),
        )
    with pytest.raises(DurableJobError, match="partial job spec cutover"):
        DurableJobRepository.bootstrap_job_spec_authority_cutover(
            database,
            capacity=_capacity(),
            signer=signer,
            trust_store=trust_store,
        )


def test_hot_job_transitions_append_bounded_deltas_not_full_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "hot-deltas.sqlite3"
    repository, signer = _repository(database)

    def forbidden_full_checkpoint(*args: object, **kwargs: object) -> None:
        raise AssertionError("ordinary hot paths must not append full checkpoints")

    monkeypatch.setattr(
        repository,
        "_append_rolling_restart_checkpoint",
        forbidden_full_checkpoint,
    )
    queued = repository.enqueue(_job_request())
    leased = repository.claim(
        JobLane.INFERENCE,
        worker_id="worker:delta",
        clock=lambda: "2026-08-24T18:00:01.000Z",
        lease_duration_ms=60_000,
    )
    assert leased is not None
    repository.heartbeat(
        queued.job_id,
        queued.job_revision,
        worker_id="worker:delta",
        fencing_token=leased.fencing_token,
        observed_at="2026-08-24T18:00:02.000Z",
        extend_ms=60_000,
    )
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM v3_rolling_restart_checkpoints").fetchone()[0]
            == 1
        )
        deltas = tuple(
            connection.execute("SELECT * FROM v3_rolling_restart_deltas ORDER BY delta_sequence")
        )
        assert len(deltas) == 3
        assert all(len(str(row["delta_manifest_json"]).encode("utf-8")) < 16_384 for row in deltas)
    restarted = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert restarted.get(queued.job_id, queued.job_revision).state.value == "leased"


def test_periodic_refresh_absorbs_exact_delta_tip_and_rebases_suffix(
    tmp_path: Path,
) -> None:
    database = tmp_path / "periodic-refresh.sqlite3"
    repository, signer = _repository(database)
    queued = repository.enqueue(_job_request())
    leased = repository.claim(
        JobLane.INFERENCE,
        worker_id="worker:refresh",
        clock=lambda: "2026-08-24T18:00:01.000Z",
        lease_duration_ms=60_000,
    )
    assert leased is not None
    repository.heartbeat(
        queued.job_id,
        queued.job_revision,
        worker_id="worker:refresh",
        fencing_token=leased.fencing_token,
        observed_at="2026-08-24T18:00:02.000Z",
        extend_ms=60_000,
    )
    before_status = repository.rolling_restart_suffix_status()
    assert before_status.checkpoint_sequence == 1
    assert before_status.delta_suffix_count == 3
    assert before_status.delta_tip_sequence == 3
    with open_v3_connection(database, read_only=True) as connection:
        delta_tip = tuple(
            connection.execute(
                "SELECT delta_sequence,delta_digest FROM "
                "v3_rolling_restart_delta_tip WHERE singleton=1"
            ).fetchone()
        )

    refreshed = repository.refresh_rolling_restart_checkpoint_if_due(
        observed_at="2026-08-24T18:00:03.000Z",
        delta_threshold=3,
    )
    assert refreshed is not None
    assert refreshed.checkpoint_sequence == 2
    after_status = repository.rolling_restart_suffix_status()
    assert after_status.checkpoint_sequence == 2
    assert after_status.delta_suffix_count == 0
    assert after_status.absorbed_delta_sequence == 3
    assert after_status.absorbed_delta_digest == after_status.delta_tip_digest
    with open_v3_connection(database, read_only=True) as connection:
        checkpoint = connection.execute(
            "SELECT absorbed_delta_sequence,absorbed_delta_digest FROM "
            "v3_rolling_restart_checkpoints WHERE checkpoint_sequence=2"
        ).fetchone()
        assert tuple(checkpoint) == delta_tip

    repository.heartbeat(
        queued.job_id,
        queued.job_revision,
        worker_id="worker:refresh",
        fencing_token=leased.fencing_token,
        observed_at="2026-08-24T18:00:04.000Z",
        extend_ms=60_000,
    )
    with open_v3_connection(database, read_only=True) as connection:
        next_delta = connection.execute(
            "SELECT prior_delta_digest,base_checkpoint_sequence FROM "
            "v3_rolling_restart_deltas WHERE delta_sequence=?",
            (int(delta_tip[0]) + 1,),
        ).fetchone()
        assert tuple(next_delta) == (str(delta_tip[1]), 2)

    restarted = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    assert restarted.recover_rolling_restart().checkpoint_sequence == 2


def test_periodic_refresh_cas_rejects_concurrent_job_and_delta_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "periodic-refresh-cas.sqlite3"
    repository, _signer = _repository(database)
    queued = repository.enqueue(_job_request())
    leased = repository.claim(
        JobLane.INFERENCE,
        worker_id="worker:refresh-cas",
        clock=lambda: "2026-08-24T18:00:01.000Z",
        lease_duration_ms=60_000,
    )
    assert leased is not None

    def concurrent_mutation() -> None:
        repository.heartbeat(
            queued.job_id,
            queued.job_revision,
            worker_id="worker:refresh-cas",
            fencing_token=leased.fencing_token,
            observed_at="2026-08-24T18:00:02.000Z",
            extend_ms=60_000,
        )

    monkeypatch.setattr(repository, "_before_rolling_restart_refresh_commit", concurrent_mutation)
    with pytest.raises(DurableJobError, match="authority changed before commit"):
        repository.refresh_rolling_restart_checkpoint_if_due(
            observed_at="2026-08-24T18:00:03.000Z",
            delta_threshold=2,
        )
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM v3_rolling_restart_checkpoints").fetchone()[0]
            == 1
        )


def test_periodic_refresh_rejects_time_before_latest_checkpoint(tmp_path: Path) -> None:
    repository, _signer = _repository(tmp_path / "refresh-time.sqlite3")
    with pytest.raises(DurableJobError, match="refresh time precedes checkpoint"):
        repository.refresh_rolling_restart_checkpoint_if_due(
            observed_at="1969-12-31T23:59:59.999Z",
            delta_threshold=3,
        )


def test_external_anchor_seeds_absorbed_delta_lineage_and_deep_audit_keeps_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "anchored-absorbed-lineage.sqlite3"
    repository, signer = _repository(database)
    queued = repository.enqueue(_job_request())
    leased = repository.claim(
        JobLane.INFERENCE,
        worker_id="worker:anchor",
        clock=lambda: "2026-08-24T18:00:01.000Z",
        lease_duration_ms=60_000,
    )
    assert leased is not None
    ordinal = 2
    for checkpoint_sequence in (2, 3):
        for _ in range(40):
            repository.heartbeat(
                queued.job_id,
                queued.job_revision,
                worker_id="worker:anchor",
                fencing_token=leased.fencing_token,
                observed_at=(f"2026-08-24T18:{ordinal // 60:02d}:{ordinal % 60:02d}.000Z"),
                extend_ms=60_000,
            )
            ordinal += 1
        refreshed = repository.refresh_rolling_restart_checkpoint_if_due(
            observed_at=(f"2026-08-24T18:{ordinal // 60:02d}:{ordinal % 60:02d}.000Z"),
            delta_threshold=40,
        )
        ordinal += 1
        assert refreshed is not None
        assert refreshed.checkpoint_sequence == checkpoint_sequence

    anchor = repository.recover_rolling_restart_deep_audit()
    assert anchor.checkpoint_sequence == 4
    with open_v3_connection(database) as connection:
        connection.execute("DROP TRIGGER v3_rolling_restart_deltas_no_update")
        connection.execute(
            "UPDATE v3_rolling_restart_deltas SET delta_digest=? WHERE delta_sequence=1",
            ("f" * 64,),
        )
        connection.execute(
            "CREATE TRIGGER v3_rolling_restart_deltas_no_update "
            "BEFORE UPDATE ON v3_rolling_restart_deltas BEGIN "
            "SELECT RAISE(ABORT, 'rolling restart deltas are immutable'); END"
        )
    with pytest.raises(DurableJobError, match="delta lineage"):
        repository.recover_rolling_restart_deep_audit()

    externally_anchored = DurableJobRepository(
        database,
        capacity=_capacity(),
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
        restart_trust=RollingRestartTrust.externally_anchored(
            RollingRestartExpectedHead(anchor.checkpoint_sequence, anchor.checkpoint_digest)
        ),
    )
    assert externally_anchored.recover_rolling_restart() == replace(
        anchor, trust_mode=RollingRestartTrustMode.EXTERNALLY_ANCHORED
    )
