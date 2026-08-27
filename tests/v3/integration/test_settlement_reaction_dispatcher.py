from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from strathmark.v3.application.capability_reactions import (
    CapabilityAdmissionVerifier,
    CapabilityCapacityVerifier,
    CapabilityReactionService,
    seal_capability_capacity,
)
from strathmark.v3.application.capacity import CapacityUse, JobLane, LaneCapacity
from strathmark.v3.application.coordinator import (
    DurableRollingPreparationCoordinator,
    RollingLifecycleReactionService,
)
from strathmark.v3.application.credibility_reactions import (
    SQLiteCredibilityReactionService,
    seal_credibility_policy,
)
from strathmark.v3.application.field_assembly import seal_field_capacity_authority
from strathmark.v3.application.job_ports import RollingDerivationPending
from strathmark.v3.application.lifecycle import LifecycleService, SnapshotKind, UpstreamSnapshot
from strathmark.v3.application.settlement import SettlementCommand, SettlementService
from strathmark.v3.application.settlement_reactions import (
    SettlementCapabilityPolicy,
    SettlementReactionDispatcher,
    SettlementReactionError,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import CommandKind
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.evidence import TargetContext
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.capability import CapabilityCapacityEnvelope
from strathmark.v3.domain.credibility import CredibilityPolicy
from strathmark.v3.domain.epochs import MandatoryReaction
from strathmark.v3.factory.candidates import CandidateBuilder
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository
from strathmark.v3.infrastructure.sqlite.projections import (
    SQLiteFieldProjectionStore,
    SQLiteRollingLifecycleResolver,
)
from tests.v3.evals.test_factory_audit_isolation import _candidate
from tests.v3.integration.test_derivation_barrier import (
    ACTOR,
    NOW,
    _append,
    _snapshot,
    _submission,
)
from tests.v3.integration.test_rolling_preparation import (
    _council_manifest,
)
from tests.v3.integration.test_rolling_preparation import (
    _repository as _rolling_repository,
)
from tests.v3.system.test_promotion_rollback import (
    ZERO,
    _register_evaluate_promote,
    _report,
)
from tests.v3.system.test_promotion_rollback import (
    _service as _factory_service,
)


class _Rolling:
    def __init__(self) -> None:
        self.effects: set[str] = set()

    def react(self, result) -> None:
        self.effects.add(result.event_set_digest)

    def recover_pending(self) -> int:
        return 0

    def derivation_authority(self, source_global_sequence: int) -> dict[str, object]:
        digest = canonical_digest(
            {
                "source_global_sequence": source_global_sequence,
                "effects": sorted(self.effects),
            }
        )
        return {
            "schema_version": "strathmark-v3-rolling-derivation-authority-v1",
            "source_global_sequence": source_global_sequence,
            "reaction_id": digest,
            "event_set_digest": digest,
            "plan_digest": digest,
            "completion_digest": digest,
            "card_publications": [],
        }


class _DelayedRolling(_Rolling):
    def __init__(self) -> None:
        super().__init__()
        self.publications_ready = False

    def derivation_authority(self, source_global_sequence: int) -> dict[str, object]:
        if not self.publications_ready:
            raise RollingDerivationPending("rolling publications pending")
        authority = super().derivation_authority(source_global_sequence)
        authority["card_publications"] = [
            {
                "card_digest": canonical_digest({"source": source_global_sequence, "card": 1}),
                "publication_digest": canonical_digest(
                    {"source": source_global_sequence, "publication": 1}
                ),
            }
        ]
        return authority


class _FailOnceCredibility(SQLiteCredibilityReactionService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.failed = False

    def react_result(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected post-capability crash")
        return super().react_result(*args, **kwargs)


class _FailOnceCapability(CapabilityReactionService):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.failed = False

    def react(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            kwargs["complete_derivation_barrier"] = False
            super().react(*args, **kwargs)
            raise RuntimeError("injected post-capability-event crash")
        return super().react(*args, **kwargs)


def test_capability_policy_is_closed_content_addressed_bundle_material() -> None:
    policy = SettlementCapabilityPolicy()

    assert policy.component_digest == canonical_digest(policy.to_dict())
    assert policy.prior.to_dict()["population_log_median"]
    assert replace(policy, effective_weight="0.5").component_digest != policy.component_digest
    assert SettlementCapabilityPolicy(prior_median_seconds="40.0") == policy


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("prior_median_seconds", "0"),
        ("calibrated_beta", "0"),
        ("evidence_log_variance", "-1"),
        ("conversion_log_variance", "101"),
        ("effective_weight", "0"),
    ),
)
def test_capability_policy_rejects_non_operational_numeric_authority(
    field: str, value: str
) -> None:
    with pytest.raises(SettlementReactionError):
        SettlementCapabilityPolicy(**{field: value})


def _candidate_with_learning_components(capability_digest: str, credibility_digest: str):
    base = _candidate(name="settlement-learning", rollback_parent_digest=ZERO)
    components = dict(base.component_digests)
    components.update(capability=capability_digest, credibility=credibility_digest)
    return CandidateBuilder(
        allowed_local_models=base.local_model_ids,
        allowed_cloud_models=base.cloud_model_ids,
    ).build(
        display_name=base.display_name,
        code_revision=base.code_revision,
        code_digest=base.code_digest,
        dependency_lock_digest=base.dependency_lock_digest,
        data_snapshot_digest=base.data_snapshot_digest,
        role_snapshots=base.role_snapshots,
        component_digests=components,
        artifact_payloads=base.artifact_payloads,
        local_model_ids=base.local_model_ids,
        cloud_model_ids=base.cloud_model_ids,
        compatibility_contract_digest=base.compatibility_contract_digest,
        rollback_parent_digest=base.rollback_parent_digest,
    )


def _open_issued_field(
    database: Path,
    bundle_digest: str,
    *,
    capacity_authority_digest: str | None = None,
    max_field_entrants: int | None = None,
    include_future_field: bool = False,
) -> tuple[LifecycleService, StableIdentifier]:
    lifecycle = LifecycleService(database)
    tournament = StableIdentifier("tournament:show")
    round_id = StableIdentifier("round:heat")
    field_id = StableIdentifier("field:heat-a")
    field_content = {
        "competitor_ids": ["competitor:a", "competitor:b"],
        "target_context": TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1").to_dict(),
        "stand_ids": ["stand:one", "stand:two"],
    }
    if capacity_authority_digest is not None:
        field_content.update(
            capacity_authority_digest=capacity_authority_digest,
            max_field_entrants=max_field_entrants,
            call_order=1,
            scheduled_at=NOW,
            deadline_at="2026-08-22T01:04:03.004Z",
        )
    _snapshot(
        lifecycle,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {
                "bundle_id": f"bundle:{bundle_digest}",
                "historical_cutoff_key": "history:prior",
            },
        ),
        "settlement-tournament-snapshot",
    )
    _snapshot(
        lifecycle,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            round_id,
            1,
            tournament,
            round_id,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        "settlement-round-snapshot",
    )
    _append(
        lifecycle,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "settlement-configure-tournament",
    )
    _append(
        lifecycle,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        round_id,
        {"configured": True},
        "settlement-configure-round",
    )
    lifecycle.open_tournament(
        tournament,
        bundle_id=StableIdentifier(f"bundle:{bundle_digest}"),
        historical_cutoff_key="history:prior",
        root_round_ids=(round_id,),
        command_id=IdempotencyKey("command:settlement-open"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    epoch, _stored = lifecycle.freeze_round_epoch(
        round_id,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:settlement-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    _snapshot(
        lifecycle,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            field_id,
            1,
            tournament,
            round_id,
            field_content,
        ),
        "settlement-field-snapshot",
    )
    _append(
        lifecycle,
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        AggregateKind.FIELD,
        field_id,
        {"round_id": str(round_id), "epoch_id": str(epoch.epoch_id), "field_revision": 1},
        "settlement-prepare-field",
    )
    _append(
        lifecycle,
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        AggregateKind.FIELD,
        field_id,
        {
            "round_id": str(round_id),
            "epoch_id": str(epoch.epoch_id),
            "field_revision": 1,
            "receipt_id": "receipt:heat-a",
            "competitor_ids": ["competitor:a", "competitor:b"],
            "issued_marks": {"competitor:a": 3, "competitor:b": 3},
        },
        "settlement-issue-field",
    )
    if include_future_field:
        future_round = StableIdentifier("round:final")
        future_field = StableIdentifier("field:final")
        _snapshot(
            lifecycle,
            UpstreamSnapshot(
                SnapshotKind.ROUND,
                future_round,
                1,
                tournament,
                future_round,
                {
                    "round_ordinal": 2,
                    "predecessor_round_ids": [str(round_id)],
                    "successor_round_ids": [],
                },
            ),
            "settlement-final-round-snapshot",
        )
        _snapshot(
            lifecycle,
            UpstreamSnapshot(
                SnapshotKind.FIELD,
                future_field,
                1,
                tournament,
                future_round,
                {
                    "competitor_ids": ["competitor:a", "competitor:b"],
                    "target_context": TargetContext(
                        "standing_block", 300, "wood", "tax:v1", "convert:v1"
                    ).to_dict(),
                    "stand_ids": ["stand:one", "stand:two"],
                    "capacity_authority_digest": capacity_authority_digest,
                    "max_field_entrants": max_field_entrants,
                    "call_order": 2,
                    "scheduled_at": "2026-08-22T01:03:03.004Z",
                    "deadline_at": "2026-08-22T01:04:03.004Z",
                },
            ),
            "settlement-final-field-snapshot",
        )
    return lifecycle, field_id


def _authorities(
    tmp_path: Path,
    *,
    fail_once: bool = False,
    fail_capability_once: bool = False,
    wrong_bundle: bool = False,
):
    factory, repository, bundle_signer, evaluator_signer, database = _factory_service(tmp_path)
    capability_policy = SettlementCapabilityPolicy()
    capability_signer = P256EphemeralSigner.generate("integrity-key:settlement-capability")
    capability_trust = IntegrityTrustStore((capability_signer.identity,))
    capability_capacity = seal_capability_capacity(
        CapabilityCapacityEnvelope(), signer=capability_signer, created_at=NOW
    )
    capability_class = _FailOnceCapability if fail_capability_once else CapabilityReactionService
    capability = capability_class(
        database,
        verifier=CapabilityAdmissionVerifier(capability_trust),
        capacity=capability_capacity,
        capacity_verifier=CapabilityCapacityVerifier(capability_trust),
    )
    credibility_signer = P256EphemeralSigner.generate("integrity-key:settlement-credibility")
    credibility_trust = IntegrityTrustStore((credibility_signer.identity,))
    credibility_policy = seal_credibility_policy(
        CredibilityPolicy(),
        optimizer_bundle_digest="e" * 64,
        signer=credibility_signer,
        created_at=NOW,
    )
    credibility_class = _FailOnceCredibility if fail_once else SQLiteCredibilityReactionService
    credibility = credibility_class(
        database,
        trust_store=credibility_trust,
        consequence_evaluator=None,
        policy_manifest=credibility_policy,
    )
    candidate = _candidate_with_learning_components(
        "f" * 64 if wrong_bundle else capability_policy.component_digest,
        credibility.component_digest,
    )
    report = _report(tmp_path, candidate, evaluator_signer, generation="settlement-learning")
    installed, _receipt = _register_evaluate_promote(
        factory,
        repository,
        candidate,
        report,
        bundle_signer,
        key="settlement-learning",
    )
    return (
        factory,
        database,
        installed.bundle_digest,
        capability_policy,
        capability_signer,
        capability_trust,
        capability_capacity,
        capability,
        credibility_policy,
        credibility_trust,
        credibility,
    )


def _dispatcher(
    database,
    factory,
    capability_policy,
    capability_signer,
    capability,
    credibility,
    rolling,
):
    return SettlementReactionDispatcher(
        database,
        rolling=rolling,
        capability=capability,
        credibility=credibility,
        factory=factory,
        capability_policy=capability_policy,
        admission_signer=capability_signer,
        actor_id=StableIdentifier("actor:settlement-reactions"),
        clock=lambda: NOW,
        monotonic_clock=lambda: 20,
    )


def _record_and_settle(lifecycle: LifecycleService, field_id: StableIdentifier) -> None:
    _record_results(lifecycle, field_id)
    _settle(lifecycle, field_id)


def _record_results(lifecycle: LifecycleService, field_id: StableIdentifier) -> None:
    lifecycle.record_live_result(
        _submission(field_id, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:settlement-result-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    lifecycle.record_live_result(
        _submission(field_id, "b", ResultStatus.DNS),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:settlement-result-b"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )


def _settle(lifecycle: LifecycleService, field_id: StableIdentifier) -> None:
    SettlementService(lifecycle).settle(
        SettlementCommand(
            str(field_id),
            1,
            "receipt:heat-a",
            "command:settlement-final",
            str(ACTOR),
            NOW,
            6,
        )
    )


def test_settlement_dispatches_capability_and_credibility_once_and_retry_is_noop(
    tmp_path: Path,
) -> None:
    (
        factory,
        database,
        bundle_digest,
        capability_policy,
        capability_signer,
        _capability_trust,
        _capability_capacity,
        capability,
        _credibility_policy,
        _credibility_trust,
        credibility,
    ) = _authorities(tmp_path)
    _base, field_id = _open_issued_field(database, bundle_digest)
    rolling = _Rolling()
    dispatcher = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        capability,
        credibility,
        rolling,
    )
    lifecycle = LifecycleService(database, reaction_port=dispatcher)
    _record_and_settle(lifecycle, field_id)
    first_effects = set(rolling.effects)
    _settle(lifecycle, field_id)

    with open_v3_connection(database, read_only=True) as connection:
        capability_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind IN (?,?)",
                (EventKind.CAPABILITY_UPDATED.value, EventKind.CAPABILITY_STATE_REBASED.value),
            ).fetchone()[0]
        )
        completions = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_reactions WHERE state='completed' "
                "AND reaction_type IN (?,?)",
                (MandatoryReaction.CAPABILITY.value, MandatoryReaction.CREDIBILITY.value),
            ).fetchone()[0]
        )
        all_reaction_completions = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_reactions WHERE state='completed'"
            ).fetchone()[0]
        )
        completed_sequences = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_sequence_completions"
            ).fetchone()[0]
        )
        approval_decisions = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
                (EventKind.APPROVAL_DECISION_RECORDED.value,),
            ).fetchone()[0]
        )
    assert capability_events == 2
    assert completions == 4
    assert all_reaction_completions == 2 * len(MandatoryReaction)
    assert completed_sequences == 2
    assert approval_decisions == 0
    assert dispatcher.recover_pending() == 0
    assert rolling.effects == first_effects


def test_pending_rolling_publications_recover_and_close_the_barrier_idempotently(
    tmp_path: Path,
) -> None:
    (
        factory,
        database,
        bundle_digest,
        capability_policy,
        capability_signer,
        _capability_trust,
        _capability_capacity,
        capability,
        _credibility_policy,
        _credibility_trust,
        credibility,
    ) = _authorities(tmp_path)
    base, field_id = _open_issued_field(database, bundle_digest)
    rolling = _DelayedRolling()
    dispatcher = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        capability,
        credibility,
        rolling,
    )
    lifecycle = LifecycleService(database, reaction_port=dispatcher)
    _record_and_settle(lifecycle, field_id)

    with open_v3_connection(database, read_only=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_derivation_sequence_completions"
                ).fetchone()[0]
            )
            == 0
        )
    assert not base.projections.reaction_barrier_for_tournament(
        "tournament:show", SQLiteEventStore(database).current_anchor().global_sequence
    ).complete

    rolling.publications_ready = True
    assert dispatcher.recover_pending() == 2
    assert dispatcher.recover_pending() == 0
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_derivation_sequence_completions"
                ).fetchone()[0]
            )
            == 2
        )
    assert base.projections.reaction_barrier_for_tournament(
        "tournament:show", SQLiteEventStore(database).current_anchor().global_sequence
    ).complete


def test_scheduled_rolling_jobs_do_not_complete_readiness_or_open_the_barrier(
    tmp_path: Path,
) -> None:
    (
        factory,
        database,
        bundle_digest,
        capability_policy,
        capability_signer,
        _capability_trust,
        _capability_capacity,
        capability,
        _credibility_policy,
        _credibility_trust,
        credibility,
    ) = _authorities(tmp_path)
    signer = P256EphemeralSigner.generate("integrity-key:settlement-rolling")
    trust = IntegrityTrustStore((signer.identity,))
    capacity = replace(
        _rolling_repository(tmp_path / "rolling-capacity").capacity,
        max_queued_jobs=64,
        lanes=(
            LaneCapacity(JobLane.HOT_FIELD, 8, 2),
            LaneCapacity(JobLane.INFERENCE, 60, 4),
            LaneCapacity(JobLane.LOOKUP_RECOVERY, 4, 2),
            LaneCapacity(JobLane.MAINTENANCE, 4, 1),
        ),
    )
    capacity_authority = seal_field_capacity_authority(
        capacity,
        bundle_digest=bundle_digest,
        signer=signer,
        created_at=NOW,
    )
    SQLiteFieldProjectionStore(
        database, signer=signer, trust_store=trust
    ).install_capacity_authority(capacity_authority, installed_at=NOW)
    base, field_id = _open_issued_field(
        database,
        bundle_digest,
        capacity_authority_digest=capacity_authority.authority_digest,
        max_field_entrants=capacity.max_field_entrants,
        include_future_field=True,
    )
    jobs = DurableJobRepository(
        database,
        capacity=capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(jobs, signer=signer, trust_store=trust)
    council = _council_manifest(signer, bundle_digest=bundle_digest)
    coordinator.install_council_authority(council, installed_at=NOW)
    rolling = RollingLifecycleReactionService(
        event_store=SQLiteEventStore(database),
        coordinator=coordinator,
        resolver=SQLiteRollingLifecycleResolver(
            database,
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
            council_manifest_digest=council.body_digest,
            trust_store=trust,
        ),
        reaction_store=jobs,
        clock=lambda: NOW,
        test_only_allow_legacy_non_executable=True,
    )
    dispatcher = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        capability,
        credibility,
        rolling,
    )
    lifecycle = LifecycleService(database, reaction_port=dispatcher)
    submissions = (
        _submission(field_id, "a", ResultStatus.COMPLETION),
        _submission(field_id, "b", ResultStatus.DNS),
    )
    acknowledgment = SettlementService(lifecycle).record_and_settle(
        SettlementCommand(
            str(field_id),
            1,
            "receipt:heat-a",
            "command:settlement-atomic-all-reactions",
            str(ACTOR),
            NOW,
            6,
        ),
        submissions,
    )

    assert len(acknowledgment.result_revisions) == 2
    with open_v3_connection(database, read_only=True) as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_reactions WHERE state='completed'"
            ).fetchone()[0]
        ) == 2 * (len(MandatoryReaction) - 2)
        pending = {
            str(row[0])
            for row in connection.execute(
                "SELECT pending.reaction_type FROM v3_derivation_reactions pending "
                "WHERE pending.state='pending' AND NOT EXISTS ("
                "SELECT 1 FROM v3_derivation_reactions completed WHERE "
                "completed.source_global_sequence=pending.source_global_sequence AND "
                "completed.reaction_type=pending.reaction_type AND "
                "completed.state='completed')"
            )
        }
        assert pending == {
            MandatoryReaction.INVALIDATION.value,
            MandatoryReaction.READINESS.value,
        }
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_derivation_sequence_completions"
                ).fetchone()[0]
            )
            == 0
        )
        assert int(
            connection.execute("SELECT COUNT(*) FROM v3_rolling_reaction_completions").fetchone()[0]
        ) == int(
            connection.execute("SELECT COUNT(*) FROM v3_rolling_reaction_obligations").fetchone()[0]
        )
    assert not base.projections.reaction_barrier_for_tournament(
        "tournament:show", SQLiteEventStore(database).current_anchor().global_sequence
    ).complete
    assert dispatcher.recover_pending() == 0


def test_tampered_post_commit_result_is_rejected_before_any_reaction(tmp_path: Path) -> None:
    (
        factory,
        database,
        bundle_digest,
        capability_policy,
        capability_signer,
        _capability_trust,
        _capability_capacity,
        capability,
        _credibility_policy,
        _credibility_trust,
        credibility,
    ) = _authorities(tmp_path)
    lifecycle, field_id = _open_issued_field(database, bundle_digest)
    rolling = _Rolling()
    dispatcher = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        capability,
        credibility,
        rolling,
    )
    _record_results(lifecycle, field_id)
    stored = lifecycle.settle_live_race(
        field_id,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:settlement-final"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=6,
    )

    with pytest.raises(SettlementReactionError, match="event set differs"):
        dispatcher.react(replace(stored, event_set_digest="0" * 64))

    with open_v3_connection(database, read_only=True) as connection:
        completed = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_reactions WHERE state='completed' "
                "AND reaction_type IN (?,?)",
                (MandatoryReaction.CAPABILITY.value, MandatoryReaction.CREDIBILITY.value),
            ).fetchone()[0]
        )
    assert completed == 0
    assert rolling.effects == set()


def test_restart_after_capability_commit_resumes_credibility_without_duplicate_state(
    tmp_path: Path,
) -> None:
    (
        factory,
        database,
        bundle_digest,
        capability_policy,
        capability_signer,
        _capability_trust,
        _capability_capacity,
        capability,
        credibility_policy,
        credibility_trust,
        failing_credibility,
    ) = _authorities(tmp_path, fail_once=True)
    _base, field_id = _open_issued_field(database, bundle_digest)
    dispatcher = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        capability,
        failing_credibility,
        _Rolling(),
    )
    lifecycle = LifecycleService(database, reaction_port=dispatcher)
    with pytest.raises(RuntimeError, match="injected post-capability crash"):
        _record_and_settle(lifecycle, field_id)

    restarted_credibility = SQLiteCredibilityReactionService(
        database,
        trust_store=credibility_trust,
        consequence_evaluator=None,
        policy_manifest=credibility_policy,
    )
    restarted = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        capability,
        restarted_credibility,
        _Rolling(),
    )

    with open_v3_connection(database, read_only=True) as connection:
        capability_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind IN (?,?)",
                (EventKind.CAPABILITY_UPDATED.value, EventKind.CAPABILITY_STATE_REBASED.value),
            ).fetchone()[0]
        )
        completed = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_reactions WHERE state='completed' "
                "AND reaction_type IN (?,?)",
                (MandatoryReaction.CAPABILITY.value, MandatoryReaction.CREDIBILITY.value),
            ).fetchone()[0]
        )
    assert restarted.recover_pending() == 0
    assert capability_events == 2
    assert completed == 4


def test_restart_after_capability_event_reuses_signed_admission_and_finishes_barrier(
    tmp_path: Path,
) -> None:
    (
        factory,
        database,
        bundle_digest,
        capability_policy,
        capability_signer,
        capability_trust,
        capability_capacity,
        failing_capability,
        _credibility_policy,
        _credibility_trust,
        credibility,
    ) = _authorities(tmp_path, fail_capability_once=True)
    _base, field_id = _open_issued_field(database, bundle_digest)
    dispatcher = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        failing_capability,
        credibility,
        _Rolling(),
    )
    lifecycle = LifecycleService(database, reaction_port=dispatcher)
    with pytest.raises(RuntimeError, match="injected post-capability-event crash"):
        _record_and_settle(lifecycle, field_id)

    restarted_capability = CapabilityReactionService(
        database,
        verifier=CapabilityAdmissionVerifier(capability_trust),
        capacity=capability_capacity,
        capacity_verifier=CapabilityCapacityVerifier(capability_trust),
    )
    restarted = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        restarted_capability,
        credibility,
        _Rolling(),
    )

    with open_v3_connection(database, read_only=True) as connection:
        capability_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind IN (?,?)",
                (EventKind.CAPABILITY_UPDATED.value, EventKind.CAPABILITY_STATE_REBASED.value),
            ).fetchone()[0]
        )
        completed = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_reactions WHERE state='completed' "
                "AND reaction_type IN (?,?)",
                (MandatoryReaction.CAPABILITY.value, MandatoryReaction.CREDIBILITY.value),
            ).fetchone()[0]
        )
    assert restarted.recover_pending() == 0
    assert capability_events == 2
    assert completed == 4


def test_settled_correction_rebases_capability_and_reverses_credibility_causally(
    tmp_path: Path,
) -> None:
    (
        factory,
        database,
        bundle_digest,
        capability_policy,
        capability_signer,
        _capability_trust,
        _capability_capacity,
        capability,
        _credibility_policy,
        _credibility_trust,
        credibility,
    ) = _authorities(tmp_path)
    _base, field_id = _open_issued_field(database, bundle_digest)
    dispatcher = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        capability,
        credibility,
        _Rolling(),
    )
    lifecycle = LifecycleService(database, reaction_port=dispatcher)
    _record_and_settle(lifecycle, field_id)

    corrected = replace(
        _submission(field_id, "a", ResultStatus.COMPLETION, revision=2),
        completion_clock_ms=21_000,
        result=OfficialResult(ResultStatus.COMPLETION, 18_000, None, 2, 1),
        source_digest=canonical_digest({"competitor": "a", "revision": 2, "raw": 18_000}),
    )
    lifecycle.record_live_result(
        corrected,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:settlement-correction-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=30,
    )

    context_digest = canonical_digest(
        TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1").to_dict()
    )
    state = capability.replay_active_state(StableIdentifier("competitor:a"), context_digest)
    assert state is not None
    assert state.observation_count == 1
    with open_v3_connection(database, read_only=True) as connection:
        correction_source = int(
            connection.execute(
                "SELECT source_global_sequence FROM v3_result_revisions "
                "WHERE competitor_id='competitor:a' AND revision=2"
            ).fetchone()[0]
        )
        completed = {
            str(row[0])
            for row in connection.execute(
                "SELECT reaction_type FROM v3_derivation_reactions "
                "WHERE source_global_sequence=? AND state='completed'",
                (correction_source,),
            )
        }
        rebases = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
                (EventKind.CAPABILITY_STATE_REBASED.value,),
            ).fetchone()[0]
        )
        reversals = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
                (EventKind.SCORE_REVERSED.value,),
            ).fetchone()[0]
        )
    assert {MandatoryReaction.CAPABILITY.value, MandatoryReaction.CREDIBILITY.value} <= completed
    assert rebases == 1
    assert reversals == 2


def test_bundle_policy_mismatch_commits_official_settlement_but_fails_learning_closed(
    tmp_path: Path,
) -> None:
    (
        factory,
        database,
        bundle_digest,
        capability_policy,
        capability_signer,
        _capability_trust,
        _capability_capacity,
        capability,
        _credibility_policy,
        _credibility_trust,
        credibility,
    ) = _authorities(tmp_path, wrong_bundle=True)
    _base, field_id = _open_issued_field(database, bundle_digest)
    dispatcher = _dispatcher(
        database,
        factory,
        capability_policy,
        capability_signer,
        capability,
        credibility,
        _Rolling(),
    )
    lifecycle = LifecycleService(database, reaction_port=dispatcher)

    with pytest.raises(SettlementReactionError, match="bundle authority"):
        _record_and_settle(lifecycle, field_id)

    with open_v3_connection(database, read_only=True) as connection:
        settled = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_result_revisions WHERE settled_global_sequence IS NOT NULL"
            ).fetchone()[0]
        )
        learned = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_reactions WHERE state='completed' "
                "AND reaction_type IN (?,?)",
                (MandatoryReaction.CAPABILITY.value, MandatoryReaction.CREDIBILITY.value),
            ).fetchone()[0]
        )
    assert settled == 2
    assert learned == 0
