from __future__ import annotations

import pytest

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.application.factory import (
    FactoryError,
    FactoryService,
    MonitoringObservation,
)
from strathmark.v3.application.lifecycle import (
    LifecycleService,
    SnapshotKind,
    UpstreamSnapshot,
)
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
)
from strathmark.v3.factory.evaluator import (
    AuditGenerationRegistry,
    EvaluationGate,
    FrozenEvaluationHarness,
    FrozenEvaluator,
)
from strathmark.v3.infrastructure.artifacts import (
    ArtifactError,
    BundleRepository,
    FactoryTrustPolicy,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
)
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
from tests.v3.evals.test_factory_audit_isolation import DIGESTS, _candidate
from tests.v3.evals.test_promotion_calibration_authority import _evidence

NOW = "2026-08-25T10:00:00.000Z"
ACTOR = StableIdentifier("actor:factory-service")
ZERO = "0" * 64


def _report(tmp_path, candidate, signer, *, generation: str, passed: bool = True):
    harness = FrozenEvaluationHarness.create(
        generation_id=generation,
        audit_snapshot_digest=DIGESTS[22],
        harness_code_digest=DIGESTS[23],
        precommit_digest=DIGESTS[24],
        gates=(EvaluationGate("normalized_crps", "lte", 0.25),),
        frozen_at=NOW,
    )
    return FrozenEvaluator(
        harness,
        AuditGenerationRegistry(tmp_path / f"audit-{generation}"),
        signer=signer,
    ).evaluate(
        candidate,
        metrics={"normalized_crps": 0.20 if passed else 0.30},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at=NOW,
        promotion_evidence=_evidence(candidate.candidate_digest),
    )


def _append_tournament_state(database, tournament_id, bundle_digest):
    name = tournament_id.value.split(":")[1]
    root_round = StableIdentifier(f"round:{name}-root")
    lifecycle = LifecycleService(database)
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament_id,
            1,
            tournament_id,
            None,
            {
                "bundle_id": f"bundle:{bundle_digest}",
                "historical_cutoff_key": "history:prior",
            },
        ),
        command_id=IdempotencyKey(f"command:snapshot-{name}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            root_round,
            1,
            tournament_id,
            root_round,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        command_id=IdempotencyKey(f"command:round-snapshot-{name}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    store = SQLiteEventStore(database)
    configured = InlinePayload.from_value(
        {"schema_version": "test-configure-v1", "configured": True}
    )
    command = CommandEnvelope(
        CommandKind.CONFIGURE_TOURNAMENT,
        IdempotencyKey(f"command:configure-{name}"),
        tournament_id,
        ((str(tournament_id), 0),),
        ACTOR,
        configured,
    )
    store.execute(
        CommandRequest(
            ACTOR,
            command,
            (
                EventIntent(
                    AggregateKind.TOURNAMENT,
                    tournament_id,
                    EventKind.TOURNAMENT_CONFIGURED,
                ),
            ),
            "test-result-v1",
            {"accepted": True},
            NOW,
            1,
        )
    )
    LifecycleService(database).open_tournament(
        tournament_id,
        bundle_id=StableIdentifier(f"bundle:{bundle_digest}"),
        historical_cutoff_key="history:prior",
        root_round_ids=(root_round,),
        command_id=IdempotencyKey(f"command:open-{name}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )


def _service(tmp_path):
    bundle_signer = P256EphemeralSigner.generate("integrity-key:factory-bundle")
    evaluator_signer = P256EphemeralSigner.generate("integrity-key:factory-evaluator")
    policy = FactoryTrustPolicy(
        bundle_trust_store=IntegrityTrustStore((bundle_signer.identity,)),
        evaluator_trust_store=IntegrityTrustStore((evaluator_signer.identity,)),
    )
    repository = BundleRepository(tmp_path / "bundles", trust_policy=policy)
    database = tmp_path / "authority.sqlite3"
    return (
        FactoryService(database, repository=repository),
        repository,
        bundle_signer,
        evaluator_signer,
        database,
    )


def _register_evaluate_promote(
    service,
    repository,
    candidate,
    report,
    bundle_signer,
    *,
    key: str,
):
    service.register_candidate(
        candidate,
        command_id=IdempotencyKey(f"command:{key}-register"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    service.record_evaluation(
        candidate,
        report,
        command_id=IdempotencyKey(f"command:{key}-evaluate"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    installed = repository.publish(candidate, report, signer=bundle_signer, created_at=NOW)
    receipt = service.promote(
        candidate,
        installed,
        command_id=IdempotencyKey(f"command:{key}-promote"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    return installed, receipt


def test_exact_pass_promotes_once_failed_gate_leaves_champion_and_retry_is_exact(tmp_path) -> None:
    service, repository, bundle_signer, evaluator_signer, _database = _service(tmp_path)
    champion = _candidate(name="champion", rollback_parent_digest=ZERO)
    report = _report(tmp_path, champion, evaluator_signer, generation="audit-champion")
    outcome = service.run_candidate(
        champion,
        report,
        signer=bundle_signer,
        request_identity="run-champion",
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    retry = service.run_candidate(
        champion,
        report,
        signer=bundle_signer,
        request_identity="run-champion",
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    assert retry == outcome
    assert outcome.promoted is True
    assert outcome.installed is not None
    installed = outcome.installed
    assert service.active_bundle_digest() == installed.bundle_digest

    loser = _candidate(
        name="failed-candidate",
        dependency_digest=DIGESTS[26],
        rollback_parent_digest=installed.bundle_digest,
    )
    failed = _report(
        tmp_path,
        loser,
        evaluator_signer,
        generation="audit-failed",
        passed=False,
    )
    failed_outcome = service.run_candidate(
        loser,
        failed,
        signer=bundle_signer,
        request_identity="run-loser",
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    assert failed_outcome.promoted is False
    assert failed_outcome.installed is None
    with pytest.raises(ArtifactError, match="failed"):
        repository.publish(loser, failed, signer=bundle_signer, created_at=NOW)
    assert service.active_bundle_digest() == installed.bundle_digest


def test_passing_diagnostic_report_cannot_cross_manual_promotion_boundary(tmp_path) -> None:
    service, repository, bundle_signer, evaluator_signer, _database = _service(tmp_path)
    candidate = _candidate(name="diagnostic-only", rollback_parent_digest=ZERO)
    harness = FrozenEvaluationHarness.create(
        generation_id="audit:diagnostic-only",
        audit_snapshot_digest=DIGESTS[22],
        harness_code_digest=DIGESTS[23],
        precommit_digest=DIGESTS[24],
        gates=(EvaluationGate("normalized_crps", "lte", 0.25),),
        frozen_at=NOW,
    )
    report = FrozenEvaluator(
        harness,
        AuditGenerationRegistry(tmp_path / "diagnostic-audit"),
        signer=evaluator_signer,
    ).evaluate(
        candidate,
        metrics={"normalized_crps": 0.2},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at=NOW,
    )
    service.register_candidate(
        candidate,
        command_id=IdempotencyKey("command:diagnostic-register"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    service.record_evaluation(
        candidate,
        report,
        command_id=IdempotencyKey("command:diagnostic-evaluate"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    installed = repository.publish(candidate, report, signer=bundle_signer, created_at=NOW)

    with pytest.raises(FactoryError, match="calibration authority"):
        service.promote(
            candidate,
            installed,
            command_id=IdempotencyKey("command:diagnostic-promote"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=3,
        )
    run_candidate = _candidate(
        name="diagnostic-run-only",
        dependency_digest=DIGESTS[30],
        rollback_parent_digest=ZERO,
    )
    run_harness = FrozenEvaluationHarness.create(
        generation_id="audit:diagnostic-run-only",
        audit_snapshot_digest=DIGESTS[22],
        harness_code_digest=DIGESTS[23],
        precommit_digest=DIGESTS[24],
        gates=(EvaluationGate("normalized_crps", "lte", 0.25),),
        frozen_at=NOW,
    )
    run_report = FrozenEvaluator(
        run_harness,
        AuditGenerationRegistry(tmp_path / "diagnostic-run-audit"),
        signer=evaluator_signer,
    ).evaluate(
        run_candidate,
        metrics={"normalized_crps": 0.2},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at=NOW,
    )
    with pytest.raises(FactoryError, match="diagnostic report"):
        service.run_candidate(
            run_candidate,
            run_report,
            signer=bundle_signer,
            request_identity="diagnostic-run",
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=4,
        )


def test_new_promotion_and_health_rollback_are_future_only_for_open_tournaments(tmp_path) -> None:
    service, repository, bundle_signer, evaluator_signer, database = _service(tmp_path)
    first = _candidate(name="first", rollback_parent_digest=ZERO)
    first_report = _report(tmp_path, first, evaluator_signer, generation="audit-first")
    first_installed, _ = _register_evaluate_promote(
        service, repository, first, first_report, bundle_signer, key="first"
    )
    open_on_first = StableIdentifier("tournament:open-first")
    _append_tournament_state(database, open_on_first, first_installed.bundle_digest)

    second = _candidate(
        name="second",
        dependency_digest=DIGESTS[27],
        rollback_parent_digest=first_installed.bundle_digest,
    )
    second_report = _report(tmp_path, second, evaluator_signer, generation="audit-second")
    second_installed, _ = _register_evaluate_promote(
        service, repository, second, second_report, bundle_signer, key="second"
    )
    open_on_second = StableIdentifier("tournament:open-second")
    _append_tournament_state(database, open_on_second, second_installed.bundle_digest)

    assert (
        service.bundle_for_tournament(open_on_first).bundle_digest == first_installed.bundle_digest
    )
    assert (
        service.bundle_for_tournament(open_on_second).bundle_digest
        == second_installed.bundle_digest
    )
    assert (
        service.bundle_for_tournament(StableIdentifier("tournament:not-open-yet")).bundle_digest
        == second_installed.bundle_digest
    )

    monitoring = MonitoringObservation.create(
        window_id="health-window-1",
        bundle_digest=second_installed.bundle_digest,
        settled_evidence_digest=DIGESTS[28],
        policy_digest=DIGESTS[29],
        gates=(EvaluationGate("normalized_crps", "lte", 0.25),),
        metrics={"normalized_crps": 0.40},
    )
    rollback = service.record_monitoring(
        monitoring,
        command_id=IdempotencyKey("command:monitor-second"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=6,
    )
    assert rollback.rolled_back is True
    assert rollback.active_bundle_digest == first_installed.bundle_digest
    assert (
        service.bundle_for_tournament(open_on_first).bundle_digest == first_installed.bundle_digest
    )
    assert (
        service.bundle_for_tournament(open_on_second).bundle_digest
        == second_installed.bundle_digest
    )
    assert (
        service.bundle_for_tournament(StableIdentifier("tournament:future")).bundle_digest
        == first_installed.bundle_digest
    )
    restarted = FactoryService(database, repository=repository)
    restarted.event_store.verify()
    assert restarted.active_bundle_digest() == first_installed.bundle_digest
    assert (
        restarted.bundle_for_tournament(open_on_second).bundle_digest
        == second_installed.bundle_digest
    )


def test_parent_substitution_and_pin_mismatch_fail_closed(tmp_path) -> None:
    service, repository, bundle_signer, evaluator_signer, database = _service(tmp_path)
    first = _candidate(name="first-parent", rollback_parent_digest=ZERO)
    report = _report(tmp_path, first, evaluator_signer, generation="audit-parent")
    installed, _ = _register_evaluate_promote(
        service, repository, first, report, bundle_signer, key="parent"
    )
    wrong_parent = _candidate(
        name="wrong-parent",
        dependency_digest=DIGESTS[30],
        rollback_parent_digest=DIGESTS[31],
    )
    wrong_report = _report(
        tmp_path, wrong_parent, evaluator_signer, generation="audit-wrong-parent"
    )
    service.register_candidate(
        wrong_parent,
        command_id=IdempotencyKey("command:wrong-register"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=7,
    )
    service.record_evaluation(
        wrong_parent,
        wrong_report,
        command_id=IdempotencyKey("command:wrong-evaluate"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=8,
    )
    wrong_installed = repository.publish(
        wrong_parent, wrong_report, signer=bundle_signer, created_at=NOW
    )
    with pytest.raises(FactoryError, match="rollback parent"):
        service.promote(
            wrong_parent,
            wrong_installed,
            command_id=IdempotencyKey("command:wrong-promote"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=9,
        )

    mismatched = StableIdentifier("tournament:mismatched-pin")
    _append_tournament_state(database, mismatched, DIGESTS[32])
    with pytest.raises(FactoryError, match="installed"):
        service.bundle_for_tournament(mismatched)
    assert service.active_bundle_digest() == installed.bundle_digest
