from __future__ import annotations

import sqlite3

import pytest

from strathmark.v3.application.factory import FactoryService
from strathmark.v3.application.lifecycle import LifecycleService, SnapshotKind, UpstreamSnapshot
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.infrastructure.integrity import (
    CheckpointRegistry,
    P256EphemeralSigner,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
from strathmark.v3.infrastructure.sqlite.projections import (
    ProjectionError,
    SQLiteProjectionStore,
)
from tests.v3.system.test_promotion_rollback import (
    ACTOR,
    NOW,
    ZERO,
    _candidate,
    _register_evaluate_promote,
    _report,
    _service,
)


def test_factory_model_status_reads_verified_projection_without_event_scan(
    tmp_path, monkeypatch
) -> None:
    service, repository, bundle_signer, evaluator_signer, database = _service(tmp_path)
    candidate = _candidate(name="projected-champion", rollback_parent_digest=ZERO)
    report = _report(tmp_path, candidate, evaluator_signer, generation="projected-audit")
    installed, _receipt = _register_evaluate_promote(
        service,
        repository,
        candidate,
        report,
        bundle_signer,
        key="projected",
    )

    def forbid_lifetime_scan(_self):
        raise AssertionError("model status must not scan the full event ledger")

    monkeypatch.setattr(SQLiteEventStore, "events", forbid_lifetime_scan)
    restarted = FactoryService(database, repository=repository)

    assert restarted.active_bundle_digest() == installed.bundle_digest
    with open_v3_connection(database, read_only=True) as connection:
        incremental_digest = SQLiteProjectionStore.model_status_digest(connection)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT candidate_id FROM v3_model_candidates "
            "WHERE promoted_bundle_digest=?",
            (installed.bundle_digest,),
        ).fetchall()
        head_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT global_sequence,event_digest FROM v3_events "
            "WHERE event_kind=? ORDER BY global_sequence DESC LIMIT 1",
            ("bundle_promoted",),
        ).fetchall()
    assert any("v3_model_candidates_bundle_idx" in str(tuple(row)) for row in plan)
    assert any("v3_events_model_status_relevant_idx" in str(tuple(row)) for row in head_plan)
    assert SQLiteProjectionStore(database).rebuild_model_status_projection() == incremental_digest
    assert restarted.active_bundle_digest() == installed.bundle_digest


def test_model_projection_tamper_fails_closed(tmp_path) -> None:
    service, repository, bundle_signer, evaluator_signer, database = _service(tmp_path)
    candidate = _candidate(name="tamper-champion", rollback_parent_digest=ZERO)
    report = _report(tmp_path, candidate, evaluator_signer, generation="tamper-audit")
    _register_evaluate_promote(
        service,
        repository,
        candidate,
        report,
        bundle_signer,
        key="tamper",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE v3_model_status SET active_bundle_digest=? WHERE singleton=1",
            ("f" * 64,),
        )

    with pytest.raises(ProjectionError, match="model status.*digest"):
        SQLiteProjectionStore(database).active_model_bundle_digest()


def _ingest_tournament_snapshot(database, *, revision: int) -> None:
    tournament_id = StableIdentifier("tournament:checkpoint-restore")
    LifecycleService(database).ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament_id,
            revision,
            tournament_id,
            None,
            {
                "bundle_id": f"bundle:{'a' * 64}",
                "historical_cutoff_key": f"history:checkpoint-{revision}",
            },
        ),
        command_id=IdempotencyKey(f"command:checkpoint-snapshot-{revision}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=revision,
    )


def test_verified_checkpoint_restore_matches_genesis_and_replays_only_suffix(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "authority.sqlite3"
    _ingest_tournament_snapshot(database, revision=1)
    projections = SQLiteProjectionStore(database)
    checkpoint_digest = projections.capture_projection_checkpoint()
    signer = P256EphemeralSigner.generate("integrity-key:projection-checkpoint")
    registry = CheckpointRegistry(
        tmp_path / "checkpoint-registry", bootstrap_identity=signer.identity
    )
    checkpoint = registry.create_checkpoint(database, signer=signer, created_at=NOW)
    assert checkpoint.projection_digest == checkpoint_digest

    _ingest_tournament_snapshot(database, revision=2)
    genesis_digest = projections.rebuild_reaction_projection()
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM v3_ingress_snapshots")

    def forbid_genesis(_self):
        raise AssertionError("verified checkpoint restore must not replay from genesis")

    monkeypatch.setattr(SQLiteProjectionStore, "rebuild_reaction_projection", forbid_genesis)
    restored_digest = projections.rebuild_from_checkpoint_registry(registry)

    assert restored_digest == genesis_digest


def test_verified_checkpoint_restore_rejects_tampered_snapshot(tmp_path) -> None:
    database = tmp_path / "authority.sqlite3"
    _ingest_tournament_snapshot(database, revision=1)
    projections = SQLiteProjectionStore(database)
    digest = projections.capture_projection_checkpoint()
    signer = P256EphemeralSigner.generate("integrity-key:tampered-projection-checkpoint")
    registry = CheckpointRegistry(
        tmp_path / "checkpoint-registry", bootstrap_identity=signer.identity
    )
    registry.create_checkpoint(database, signer=signer, created_at=NOW)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE v3_projection_restore_snapshots SET snapshot_json=? WHERE projection_digest=?",
            ("{}", digest),
        )

    with pytest.raises(ProjectionError, match="snapshot.*digest|canonical"):
        projections.rebuild_from_checkpoint_registry(registry)
