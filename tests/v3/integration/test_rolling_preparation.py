from __future__ import annotations

from pathlib import Path

import pytest

from strathmark.v3.application.capacity import (
    CapacityManifest,
    CapacityUse,
    JobKind,
    JobLane,
    JobPriority,
    LaneCapacity,
)
from strathmark.v3.application.coordinator import (
    CardDependency,
    DurableRollingPreparationCoordinator,
    PreparationCandidate,
    PreparationClass,
    RollingComponentOutcome,
    RollingLifecycleReactionService,
    RollingPreparationPlanner,
)
from strathmark.v3.application.field_assembly import seal_competitor_card_authority
from strathmark.v3.application.job_ports import DurableJobError, RollingDerivationPending
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket, TargetContext
from strathmark.v3.contracts.forecasts import (
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.jobs import (
    DurableJobRepository,
    JobRequest,
    JobState,
)

T0 = "2026-08-24T18:00:00.000Z"
T1 = "2026-08-24T18:00:01.000Z"
T2 = "2026-08-24T18:00:02.000Z"


def _repository(tmp_path: Path) -> DurableJobRepository:
    capacity = CapacityManifest(
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
    signer = P256EphemeralSigner.generate("integrity-key:rolling-tests")
    return DurableJobRepository(
        tmp_path / "rolling.sqlite3",
        capacity=capacity,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )


def _rolling_authority(
    signer: P256EphemeralSigner,
    revision: int = 1,
    *,
    epoch_id: str = "epoch:rolling-1",
    competitor: str = "rolling-a",
    council_available: bool = True,
) -> tuple[PreparationCandidate, object]:
    context = TargetContext(
        event_code="underhand",
        size_mm=300,
        material_code="pine",
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
    )
    packet = EvidencePacket.create(
        competitor_id=StableIdentifier(f"competitor:{competitor}"),
        target_context=context,
        observations=(),
        taxonomy_version=context.taxonomy_version,
        conversion_version=context.conversion_version,
        historical_cutoff_key="history:rolling-cutoff",
        tournament_epoch_id=StableIdentifier(epoch_id),
        tournament_event_sequence=2 + revision,
    )
    distributions = tuple(
        PositiveTimeDistribution(
            (
                QuantilePoint("0.1", median - 1000),
                QuantilePoint("0.5", median),
                QuantilePoint("0.9", median + 1000),
            )
        )
        for median in (39_000, 40_000, 41_000)
    )
    forecasts = tuple(
        AssessorForecast.create(
            forecast_id=StableIdentifier(f"forecast:{competitor}-{kind.value}"),
            assessor=kind,
            state=(
                ForecastState.ABSTAINED
                if kind is AssessorKind.LLM_COUNCIL and not council_available
                else ForecastState.COMMITTED
            ),
            evidence_digest=packet.content_digest,
            distribution=(
                None if kind is AssessorKind.LLM_COUNCIL and not council_available else distribution
            ),
            support=EvidenceSupport(0, "0", 0, "history:rolling-cutoff", 3),
            warnings=(),
            artifacts=(),
            abstention_code=(
                "council_unavailable"
                if kind is AssessorKind.LLM_COUNCIL and not council_available
                else None
            ),
        )
        for kind, distribution in zip(
            (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL),
            distributions,
            strict=True,
        )
    )
    card = seal_competitor_card_authority(
        packet,
        forecasts,
        bundle_digest="b" * 64,
        signer=signer,
        created_at=T0,
    )
    candidate = PreparationCandidate.create(
        competitor_id=str(packet.competitor_id),
        target_context_digest=context.digest,
        historical_cutoff_key=packet.historical_cutoff_key,
        tournament_epoch_id=str(packet.tournament_epoch_id),
        bundle_digest="b" * 64,
        evidence_digest=packet.content_digest,
        dependency_revision=revision,
        preparation_class=PreparationClass.IMMINENT_FIELD,
        hard_deadline_at="2026-08-24T18:05:00.000Z",
        evidence_packet=packet,
    )
    return candidate, card


def _council_manifest(signer: P256EphemeralSigner, *, bundle_digest: str = "b" * 64):
    return sign_manifest(
        "rolling_council_roster_authority",
        {
            "schema_version": "strathmark-v3-rolling-council-roster-v1",
            "purpose": "rolling_card_council",
            "bundle_digest": bundle_digest,
            "members": [
                {
                    "member_id": "local_qwen35_9b",
                    "provider_kind": "local",
                    "family": "qwen3.5",
                    "member_manifest_digest": "1" * 64,
                },
                {
                    "member_id": "local_ministral3_8b",
                    "provider_kind": "local",
                    "family": "ministral3",
                    "member_manifest_digest": "2" * 64,
                },
                {
                    "member_id": "frontier_cloud",
                    "provider_kind": "cloud",
                    "family": "frontier",
                    "member_manifest_digest": "3" * 64,
                },
            ],
        },
        signer=signer,
        created_at=T0,
    )


def _aggregate_manifest(
    signer: P256EphemeralSigner,
    candidate: PreparationCandidate,
    card,
    council,
    repository: DurableJobRepository,
    *,
    assume_deadline_cancel: bool = False,
):
    records = repository.records_for_card(candidate.key.card_digest)
    by_id = {item.payload()["component_id"]: item for item in records}
    council_payload = council.body()["payload"]
    members = []
    for member in council_payload["members"]:
        record = by_id[member["member_id"]]
        outcome = {
            JobState.SUCCEEDED: "succeeded",
            JobState.CANCELLED: "cancelled",
            JobState.PERMANENT_FAILED: "failed",
            JobState.STALE: "stale",
            JobState.INVALID: "invalid",
        }.get(record.state)
        fencing_token = record.fencing_token
        if (
            outcome is None
            and assume_deadline_cancel
            and record.state
            in {
                JobState.QUEUED,
                JobState.LEASED,
                JobState.RETRYABLE_FAILED,
            }
        ):
            outcome = "timed_out"
            fencing_token += 1
        assert outcome is not None
        members.append(
            {
                "member_id": member["member_id"],
                "member_manifest_digest": member["member_manifest_digest"],
                "job_id": record.job_id,
                "job_revision": record.job_revision,
                "fencing_token": fencing_token,
                "outcome": outcome,
                "result_digest": record.result_digest,
                "terminal_reason_code": (
                    "deadline_sealed" if outcome == "timed_out" else record.terminal_reason
                ),
            }
        )
    return sign_manifest(
        "rolling_council_aggregate_authority",
        {
            "schema_version": "strathmark-v3-rolling-council-aggregate-v1",
            "purpose": "rolling_card_council_aggregate",
            "card_digest": candidate.key.card_digest,
            "council_manifest_digest": council.body_digest,
            "member_receipts": members,
            "valid_member_count": sum(item["outcome"] == "succeeded" for item in members),
            "aggregate_available": sum(item["outcome"] == "succeeded" for item in members) >= 2,
            "aggregate_forecast_commit_digest": card.forecasts[2].commit_digest,
        },
        signer=signer,
        created_at=T2,
    )


def _succeed(
    repository: DurableJobRepository,
    candidate: PreparationCandidate,
    *,
    number: int,
    kind: JobKind = JobKind.FORMULA_CARD,
) -> tuple[str, int]:
    key = candidate.key
    request = JobRequest.create(
        job_id=f"job:rolling-{number}",
        job_revision=1,
        idempotency_key=f"job_request:rolling-{number}",
        job_kind=kind,
        lane=kind.lane,
        priority=JobPriority.IMMINENT_FIELD,
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        payload={"card_key": key.to_dict(), "component": kind.value},
        evidence_digest=key.evidence_digest,
        bundle_digest=key.bundle_digest,
        retry_policy_version="retry.v1",
        created_at=T0,
        not_before_at=T0,
        hard_deadline_at="2026-08-24T18:05:00.000Z",
        max_attempts=2,
    )
    repository.enqueue(request)
    claimed = repository.claim(
        kind.lane,
        worker_id="worker:rolling",
        clock=lambda: T1,
        lease_duration_ms=60_000,
    )
    repository.commit_success(
        str(request.job_id),
        1,
        worker_id="worker:rolling",
        fencing_token=claimed.fencing_token,
        result_digest=str(number) * 64,
        current_context=lambda _connection, _record: (
            key.evidence_digest,
            key.bundle_digest,
        ),
        clock=lambda: T2,
    )
    return str(request.job_id), 1


def _candidate(
    competitor: str,
    context: str,
    preparation_class: PreparationClass,
    revision: int = 1,
) -> PreparationCandidate:
    return PreparationCandidate.create(
        competitor_id=f"competitor:{competitor}",
        target_context_digest=context * 64,
        historical_cutoff_key="history:cutoff",
        tournament_epoch_id="epoch:round-1",
        bundle_digest="b" * 64,
        evidence_digest="e" * 64,
        dependency_revision=revision,
        preparation_class=preparation_class,
        hard_deadline_at="2026-08-24T18:02:00.000Z",
    )


def test_planner_deduplicates_exact_cards_orders_work_and_invalidates_dependency_changes() -> None:
    planner = RollingPreparationPlanner()
    scheduled = _candidate("a", "1", PreparationClass.SCHEDULED)
    plausible = _candidate("b", "2", PreparationClass.PLAUSIBLE_QUALIFIER)
    imminent = _candidate("c", "3", PreparationClass.IMMINENT_FIELD)
    plan = planner.plan((scheduled, plausible, imminent, plausible))
    assert tuple(str(item.key.competitor_id) for item in plan.pending) == (
        "competitor:c",
        "competitor:b",
        "competitor:a",
    )
    assert len(plan.pending) == 3
    assert len({item.key.idempotency_key for item in plan.pending}) == 3

    revised = _candidate("b", "2", PreparationClass.PLAUSIBLE_QUALIFIER, revision=2)
    changed = planner.plan((revised,))
    assert changed.invalidated == (plausible.key,)
    assert changed.pending[0].key != plausible.key


def test_planner_merge_is_permutation_safe_and_stale_last_never_wins() -> None:
    current = _candidate("a", "1", PreparationClass.SCHEDULED, revision=3)
    stale = _candidate("a", "1", PreparationClass.IMMINENT_FIELD, revision=2)
    urgent = PreparationCandidate(
        current.key, PreparationClass.IMMINENT_FIELD, "2026-08-24T18:01:00.000Z"
    )
    for candidates in ((stale, current, urgent), (urgent, current, stale)):
        plan = RollingPreparationPlanner().plan(candidates)
        assert len(plan.pending) == 1
        assert plan.pending[0].key == current.key
        assert plan.pending[0].preparation_class is PreparationClass.IMMINENT_FIELD
        assert plan.pending[0].hard_deadline_at == "2026-08-24T18:01:00.000Z"


def test_new_epoch_can_reset_revision_and_invalidates_prior_epoch_card() -> None:
    old = _candidate("a", "1", PreparationClass.IMMINENT_FIELD, revision=3)
    new = PreparationCandidate.create(
        competitor_id="competitor:a",
        target_context_digest="1" * 64,
        historical_cutoff_key="history:cutoff",
        tournament_epoch_id="epoch:round-2",
        bundle_digest="b" * 64,
        evidence_digest="f" * 64,
        dependency_revision=1,
        preparation_class=PreparationClass.IMMINENT_FIELD,
        hard_deadline_at="2026-08-24T18:02:00.000Z",
    )
    planner = RollingPreparationPlanner()
    planner.plan((old,))

    plan = planner.plan((new,))

    assert plan.pending == (new,)
    assert plan.invalidated == (old.key,)


def test_card_key_covers_every_causal_dependency_and_survives_restart_export() -> None:
    candidate = _candidate("a", "4", PreparationClass.IMMINENT_FIELD)
    value = candidate.key.to_dict()
    assert set(value) == {
        "schema_version",
        "competitor_id",
        "target_context_digest",
        "historical_cutoff_key",
        "tournament_epoch_id",
        "bundle_digest",
        "evidence_digest",
        "dependency_revision",
        "card_digest",
        "idempotency_key",
    }
    restarted = RollingPreparationPlanner.from_snapshot(RollingPreparationPlanner().snapshot())
    assert restarted.plan((candidate,)).pending[0].key == candidate.key


def test_result_deadlines_surface_two_minute_and_five_minute_readiness() -> None:
    dependency = CardDependency(
        result_recorded_at="2026-08-24T18:00:00.000Z",
        final_ready_at="2026-08-24T18:01:59.999Z",
        final_call_at="2026-08-24T18:05:00.000Z",
    )
    assert dependency.within_result_to_ready_sla
    assert dependency.within_last_heat_to_final_window


def test_single_component_or_non_card_job_cannot_mint_completed_card(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    candidate = _candidate("a", "5", PreparationClass.IMMINENT_FIELD)
    job_id, revision = _succeed(repository, candidate, number=1)
    planner = RollingPreparationPlanner()
    planner.plan((candidate,))
    with pytest.raises(DurableJobError, match="whole-card publication"):
        planner.record_completed_from_job(
            repository, candidate.key, job_id=job_id, job_revision=revision
        )


def test_late_success_cannot_regress_current_dependency_revision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    old = _candidate("a", "6", PreparationClass.IMMINENT_FIELD, revision=1)
    current = _candidate("a", "6", PreparationClass.IMMINENT_FIELD, revision=2)
    old_job, old_revision = _succeed(repository, old, number=2)
    planner = RollingPreparationPlanner()
    planner.plan((old,))
    changed = planner.plan((current,))
    assert changed.invalidated == (old.key,)
    with pytest.raises(DurableJobError, match="superseded card publication"):
        planner.record_completed_from_job(
            repository,
            old.key,
            job_id=old_job,
            job_revision=old_revision,
        )
    assert planner.plan((current,)).pending == (current,)


def test_durable_coordinator_enqueues_exact_components_seals_and_recovers(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from unittest.mock import patch

    signer = P256EphemeralSigner.generate("integrity-key:rolling-production")
    trust = IntegrityTrustStore((signer.identity,))
    repository = DurableJobRepository(
        tmp_path / "durable-rolling.sqlite3",
        capacity=_repository(tmp_path / "capacity").capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(
        repository,
        signer=signer,
        trust_store=trust,
    )
    council = _council_manifest(signer)
    coordinator.install_council_authority(council, installed_at=T0)
    candidate, card = _rolling_authority(signer)
    scheduled = coordinator.schedule(
        (candidate,),
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        observed_at=T0,
    )
    assert tuple(item.job_kind for item in scheduled) == (
        JobKind.FORMULA_CARD,
        JobKind.ML_CARD,
        JobKind.LOCAL_LLM_CARD,
        JobKind.LOCAL_LLM_CARD,
        JobKind.CLOUD_LLM_CARD,
    )
    assert len({item.payload()["component_id"] for item in scheduled}) == 5
    actual_payload_bytes = sum(len(canonical_bytes(item.payload())) for item in scheduled)
    assert {item.capacity_use().blob_bytes for item in scheduled} == {actual_payload_bytes}
    limited_repository = DurableJobRepository(
        tmp_path / "durable-rolling-limited.sqlite3",
        capacity=replace(repository.capacity, max_blob_bytes=actual_payload_bytes - 1),
        signer=signer,
        trust_store=trust,
    )
    limited = DurableRollingPreparationCoordinator(
        limited_repository, signer=signer, trust_store=trust
    )
    limited.install_council_authority(council, installed_at=T0)
    with pytest.raises(DurableJobError, match="blob_bytes_capacity_exceeded"):
        limited.schedule(
            (candidate,),
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 1, 25),
            council_manifest_digest=council.body_digest,
            observed_at=T0,
        )
    assert limited_repository.records_for_card(candidate.key.card_digest) == ()

    by_kind = {item.payload()["component_id"]: item for item in scheduled}
    result_by_component = {
        "formula": card.forecasts[0].commit_digest,
        "ml": card.forecasts[1].commit_digest,
        "local_qwen35_9b": "4" * 64,
        "local_ministral3_8b": "5" * 64,
        "frontier_cloud": "6" * 64,
    }
    for ordinal, component in enumerate(by_kind.values(), start=1):
        claimed = repository.claim(
            component.lane,
            worker_id=f"worker:rolling-{ordinal}",
            clock=lambda: T1,
            lease_duration_ms=60_000,
        )
        repository.commit_success(
            claimed.job_id,
            claimed.job_revision,
            worker_id=f"worker:rolling-{ordinal}",
            fencing_token=claimed.fencing_token,
            result_digest=result_by_component[claimed.payload()["component_id"]],
            current_context=lambda _connection, _record: (
                candidate.key.evidence_digest,
                candidate.key.bundle_digest,
            ),
            clock=lambda: T2,
        )
    with (
        patch.object(
            repository,
            "_verify_rolling_reactions",
            side_effect=AssertionError("lifetime reaction audit entered hot publication path"),
        ),
        patch.object(
            repository,
            "_verify_connection",
            side_effect=AssertionError("lifetime job audit entered hot publication path"),
        ),
    ):
        publication = coordinator.seal_card(
            candidate.key,
            card,
            council_manifest_digest=council.body_digest,
            council_aggregate_authority=_aggregate_manifest(
                signer,
                candidate,
                card,
                council,
                repository,
                assume_deadline_cancel=True,
            ),
            observed_at=T2,
        )
    assert publication.availability == (
        ("formula", "available"),
        ("ml", "available"),
        ("llm_council", "normal_3_of_3"),
    )
    with open_v3_connection(repository.database_path) as connection:
        connection.execute(
            "DELETE FROM v3_rolling_card_current WHERE publication_digest=?",
            (publication.publication_digest,),
        )
    restarted = DurableRollingPreparationCoordinator(
        repository,
        signer=signer,
        trust_store=trust,
    )
    assert restarted.cached(candidate.key).publication_digest == publication.publication_digest
    assert (
        restarted.schedule(
            (candidate,),
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
            council_manifest_digest=council.body_digest,
            observed_at=T2,
        )
        == ()
    )
    exact_retry = restarted.seal_card(
        candidate.key,
        card,
        council_manifest_digest=council.body_digest,
        council_aggregate_authority=_aggregate_manifest(
            signer, candidate, card, council, repository
        ),
        observed_at="2026-08-24T18:00:03.000Z",
    )
    assert exact_retry == publication
    assert len(repository.records_for_card(candidate.key.card_digest)) == 5

    revised, _revised_card = _rolling_authority(signer, revision=2)
    with open_v3_connection(repository.database_path) as connection:
        connection.execute(
            "DELETE FROM v3_rolling_card_current WHERE publication_digest=?",
            (publication.publication_digest,),
        )
    restarted.schedule(
        (revised,),
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        observed_at="2026-08-24T18:00:03.000Z",
    )
    repository.verify_rolling_storage()
    after_revision_restart = DurableRollingPreparationCoordinator(
        repository, signer=signer, trust_store=trust
    )
    historical_retry = after_revision_restart.seal_card(
        candidate.key,
        card,
        council_manifest_digest=council.body_digest,
        council_aggregate_authority=_aggregate_manifest(
            signer, candidate, card, council, repository
        ),
        observed_at="2026-08-24T18:00:04.000Z",
    )
    assert historical_retry == publication
    with pytest.raises(KeyError):
        after_revision_restart.cached(candidate.key)


def test_weight_only_recombination_requires_current_u12_field_authority(
    tmp_path: Path,
) -> None:
    from tests.v3.integration.test_field_receipts import _bootstrap, _ingest_field

    path = tmp_path / "verified-recombination.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    pipeline = build(field)
    signer = store._signer
    trust = store._trust_store
    repository = DurableJobRepository(
        path,
        capacity=_repository(tmp_path / "capacity").capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    council = _council_manifest(signer, bundle_digest=field.bundle_digest)
    coordinator.install_council_authority(council, installed_at=T0)
    cards = tuple(item.card for item in pipeline.pools)
    candidates = tuple(
        PreparationCandidate.create(
            competitor_id=str(card.evidence_packet.competitor_id),
            target_context_digest=card.evidence_packet.target_context.digest,
            historical_cutoff_key=str(card.evidence_packet.historical_cutoff_key),
            tournament_epoch_id=str(card.evidence_packet.tournament_epoch_id),
            bundle_digest=card.bundle_digest,
            evidence_digest=card.evidence_packet.content_digest,
            dependency_revision=field.field_revision,
            preparation_class=PreparationClass.IMMINENT_FIELD,
            hard_deadline_at=field.deadline_at,
            evidence_packet=card.evidence_packet,
        )
        for card in cards
    )
    coordinator.schedule(
        candidates,
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        observed_at=T0,
    )
    card_by_digest = {
        candidate.key.card_digest: card for candidate, card in zip(candidates, cards, strict=True)
    }
    for ordinal in range(1, 11):
        claimed = repository.claim(
            JobLane.INFERENCE,
            worker_id=f"worker:recombine-{ordinal}",
            clock=lambda: T1,
            lease_duration_ms=60_000,
        )
        assert claimed is not None
        payload = claimed.payload()
        card = card_by_digest[payload["card_key"]["card_digest"]]
        component = payload["component_id"]
        result_digest = {
            "formula": card.forecasts[0].commit_digest,
            "ml": card.forecasts[1].commit_digest,
        }.get(component, canonical_digest({"component": component, "job": claimed.job_id}))
        repository.commit_success(
            claimed.job_id,
            claimed.job_revision,
            worker_id=f"worker:recombine-{ordinal}",
            fencing_token=claimed.fencing_token,
            result_digest=result_digest,
            current_context=lambda _connection, record: (
                record.evidence_digest,
                record.bundle_digest,
            ),
            clock=lambda: T2,
        )
    publications = tuple(
        coordinator.seal_card(
            candidate.key,
            card,
            council_manifest_digest=council.body_digest,
            council_aggregate_authority=_aggregate_manifest(
                signer, candidate, card, council, repository
            ),
            observed_at=T2,
        )
        for candidate, card in zip(candidates, cards, strict=True)
    )

    with pytest.raises(DurableJobError, match="current field"):
        coordinator.enqueue_weight_recombination(
            field=field,
            keys=(candidates[0].key,),
            weight_authority=pipeline.operational_weight_authority,
            authority_store=store,
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
            observed_at="2026-08-24T18:00:03.000Z",
            hard_deadline_at="2026-08-24T18:04:00.000Z",
        )
    recombination = coordinator.enqueue_weight_recombination(
        field=field,
        keys=tuple(item.key for item in candidates),
        weight_authority=pipeline.operational_weight_authority,
        authority_store=store,
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        observed_at="2026-08-24T18:00:03.000Z",
        hard_deadline_at="2026-08-24T18:04:00.000Z",
    )
    reversed_retry = coordinator.enqueue_weight_recombination(
        field=field,
        keys=tuple(reversed(tuple(item.key for item in candidates))),
        weight_authority=pipeline.operational_weight_authority,
        authority_store=store,
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        observed_at="2026-08-24T18:00:03.000Z",
        hard_deadline_at="2026-08-24T18:04:00.000Z",
    )
    assert reversed_retry == recombination
    assert recombination.job_kind is JobKind.HOT_FIELD_ASSEMBLY
    assert recombination.payload()["provider_recall"] is False
    assert recombination.payload()["card_publication_digests"] == [
        item.publication_digest for item in publications
    ]
    assert all(
        len(repository.records_for_card(candidate.key.card_digest)) == 5 for candidate in candidates
    )
    _ingest_field(lifecycle, 2)
    assert (
        repository.claim(
            JobLane.HOT_FIELD,
            worker_id="worker:stale-recombination",
            clock=lambda: "2026-08-24T18:00:03.500Z",
            lease_duration_ms=30_000,
        )
        is None
    )
    assert repository.get(recombination.job_id, recombination.job_revision).state is JobState.STALE


@pytest.mark.parametrize("fail_after", (1, 2, 3, 4))
def test_partial_component_enqueue_repairs_exactly_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_after: int
) -> None:
    signer = P256EphemeralSigner.generate(f"integrity-key:rolling-crash-{fail_after}")
    trust = IntegrityTrustStore((signer.identity,))
    capacity = _repository(tmp_path / "capacity").capacity
    repository = DurableJobRepository(
        tmp_path / "repair.sqlite3",
        capacity=capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    council = _council_manifest(signer)
    coordinator.install_council_authority(council, installed_at=T0)
    candidate, _card = _rolling_authority(signer)
    original = repository.enqueue
    calls = 0

    def interrupted(request, *, maintenance_suspended=False):
        nonlocal calls
        calls += 1
        if calls > fail_after:
            raise RuntimeError("injected_crash")
        return original(request, maintenance_suspended=maintenance_suspended)

    monkeypatch.setattr(repository, "enqueue", interrupted)
    with pytest.raises(RuntimeError, match="injected_crash"):
        coordinator.schedule(
            (candidate,),
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
            council_manifest_digest=council.body_digest,
            observed_at=T0,
        )
    monkeypatch.setattr(repository, "enqueue", original)
    restarted = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    repaired = restarted.schedule(
        (candidate,),
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        observed_at=T1,
    )
    assert len(repaired) == 5
    assert len(repository.records_for_card(candidate.key.card_digest)) == 5


def test_rejected_higher_revision_does_not_poison_same_process_planner(
    tmp_path: Path,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:rolling-plan-rollback")
    trust = IntegrityTrustStore((signer.identity,))
    repository = DurableJobRepository(
        tmp_path / "planner-rollback.sqlite3",
        capacity=_repository(tmp_path / "capacity").capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    council = _council_manifest(signer)
    coordinator.install_council_authority(council, installed_at=T0)
    valid, _card = _rolling_authority(signer, revision=1)
    rejected = PreparationCandidate.create(
        competitor_id=str(valid.key.competitor_id),
        target_context_digest=valid.key.target_context_digest,
        historical_cutoff_key=str(valid.key.historical_cutoff_key),
        tournament_epoch_id=str(valid.key.tournament_epoch_id),
        bundle_digest="c" * 64,
        evidence_digest="d" * 64,
        dependency_revision=2,
        preparation_class=PreparationClass.IMMINENT_FIELD,
        hard_deadline_at=valid.hard_deadline_at,
    )
    with pytest.raises(DurableJobError, match="council bundle"):
        coordinator.schedule(
            (rejected,),
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
            council_manifest_digest=council.body_digest,
            observed_at=T0,
        )

    scheduled = coordinator.schedule(
        (valid,),
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        observed_at=T1,
    )
    assert len(scheduled) == 5


@pytest.mark.parametrize(
    ("council_successes", "expected_availability"),
    (
        (3, "normal_3_of_3"),
        (2, "degraded_2_of_3"),
        (1, "unavailable_1_of_3"),
        (0, "unavailable_0_of_3"),
    ),
)
def test_deadline_seal_persists_exact_council_terminal_matrix(
    tmp_path: Path, council_successes: int, expected_availability: str
) -> None:
    signer = P256EphemeralSigner.generate(f"integrity-key:rolling-deadline-{council_successes}")
    trust = IntegrityTrustStore((signer.identity,))
    repository = DurableJobRepository(
        tmp_path / f"deadline-{council_successes}.sqlite3",
        capacity=_repository(tmp_path / "capacity").capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    council = _council_manifest(signer)
    coordinator.install_council_authority(council, installed_at=T0)
    candidate, card = _rolling_authority(
        signer,
        competitor=f"deadline-{council_successes}",
        council_available=council_successes >= 2,
    )
    coordinator.schedule(
        (candidate,),
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        observed_at=T0,
    )
    for ordinal in range(1, 3 + council_successes):
        claimed = repository.claim(
            JobLane.INFERENCE,
            worker_id=f"worker:deadline-{council_successes}-{ordinal}",
            clock=lambda: T1,
            lease_duration_ms=60_000,
        )
        assert claimed is not None
        component = claimed.payload()["component_id"]
        result_digest = {
            "formula": card.forecasts[0].commit_digest,
            "ml": card.forecasts[1].commit_digest,
        }.get(component, canonical_digest({"component": component}))
        repository.commit_success(
            claimed.job_id,
            claimed.job_revision,
            worker_id=f"worker:deadline-{council_successes}-{ordinal}",
            fencing_token=claimed.fencing_token,
            result_digest=result_digest,
            current_context=lambda _connection, record: (
                record.evidence_digest,
                record.bundle_digest,
            ),
            clock=lambda: T2,
        )
    deadline = "2026-08-24T18:05:00.000Z"
    publication = coordinator.seal_card(
        candidate.key,
        card,
        council_manifest_digest=council.body_digest,
        council_aggregate_authority=_aggregate_manifest(
            signer,
            candidate,
            card,
            council,
            repository,
            assume_deadline_cancel=True,
        ),
        observed_at=deadline,
    )
    assert dict(publication.availability)["llm_council"] == expected_availability
    terminal = repository.records_for_card(candidate.key.card_digest)
    assert sum(item.state is JobState.SUCCEEDED for item in terminal) == (2 + council_successes)
    assert sum(item.state is JobState.CANCELLED for item in terminal) == (3 - council_successes)
    component_receipts = {item.component_id: item for item in publication.components}
    council_members = tuple(member["member_id"] for member in council.body()["payload"]["members"])
    for member_id in council_members[:council_successes]:
        assert component_receipts[member_id].outcome is RollingComponentOutcome.SUCCEEDED
        assert component_receipts[member_id].terminal_reason_code is None
    for member_id in council_members[council_successes:]:
        assert component_receipts[member_id].outcome is RollingComponentOutcome.TIMED_OUT
        assert component_receipts[member_id].terminal_reason_code == "deadline_sealed"

    restarted = DurableRollingPreparationCoordinator(
        DurableJobRepository(
            repository.database_path,
            capacity=repository.capacity,
            signer=signer,
            trust_store=trust,
        ),
        signer=signer,
        trust_store=trust,
    )
    restored = restarted.cached(candidate.key)
    assert restored.components == publication.components
    for member_id in council_members[council_successes:]:
        restored_receipt = next(
            item for item in restored.components if item.component_id == member_id
        )
        assert restored_receipt.outcome is RollingComponentOutcome.TIMED_OUT
        assert restored_receipt.terminal_reason_code == "deadline_sealed"


def test_canonical_field_revision_event_schedules_affected_cards_and_exact_retry_repairs(
    tmp_path: Path,
) -> None:
    from strathmark.v3.application.lifecycle import LifecycleService
    from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
    from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
    from strathmark.v3.infrastructure.sqlite.projections import (
        SQLiteProjectionStore,
        SQLiteRollingLifecycleResolver,
    )
    from tests.v3.integration.test_field_receipts import _bootstrap, _ingest_field

    path = tmp_path / "event-reaction.sqlite3"
    store, field, _build, _lifecycle = _bootstrap(path)
    signer = store._signer
    trust = store._trust_store
    repository = DurableJobRepository(
        path,
        capacity=_repository(tmp_path / "capacity").capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    council = _council_manifest(signer, bundle_digest=field.bundle_digest)
    coordinator.install_council_authority(council, installed_at=T0)
    projection = SQLiteProjectionStore(path)
    with open_v3_connection(path) as connection:
        obligation_count = int(
            connection.execute("SELECT COUNT(*) FROM v3_rolling_reaction_obligations").fetchone()[0]
        )
        connection.execute("DROP TRIGGER v3_rolling_reaction_obligations_no_delete")
        connection.execute(
            "DELETE FROM v3_rolling_reaction_obligations WHERE reaction_id=("
            "SELECT reaction_id FROM v3_rolling_reaction_obligations "
            "ORDER BY first_global_sequence LIMIT 1)"
        )
        connection.execute(
            "CREATE TRIGGER v3_rolling_reaction_obligations_no_delete "
            "BEFORE DELETE ON v3_rolling_reaction_obligations BEGIN "
            "SELECT RAISE(ABORT, 'rolling reaction obligation is immutable'); END"
        )
        connection.commit()
    assert projection.rebuild_rolling_reaction_obligations() == 1
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_rolling_reaction_obligations"
                ).fetchone()[0]
            )
            == obligation_count
        )
    resolver = SQLiteRollingLifecycleResolver(
        path,
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        trust_store=trust,
    )
    # The field/epoch events were committed by the original lifecycle process
    # before any reaction callback existed. Constructing the reaction service
    # after that simulated crash drains the durable obligations without a
    # repeated upstream request.
    reaction = RollingLifecycleReactionService(
        event_store=SQLiteEventStore(path),
        coordinator=coordinator,
        resolver=resolver,
        reaction_store=repository,
        clock=lambda: "2026-08-24T18:00:01.000Z",
        test_only_allow_legacy_non_executable=True,
    )
    assert repository.pending_rolling_reactions(limit=12) == ()
    recovered_job_count = len(
        tuple(item for row in repository.rolling_current_rows() for item in (row,))
    )
    assert recovered_job_count == 0
    with open_v3_connection(path, read_only=True) as connection:
        first_count = int(connection.execute("SELECT COUNT(*) FROM v3_jobs").fetchone()[0])
        source_sequences = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT first_global_sequence FROM v3_rolling_reaction_obligations "
                "ORDER BY first_global_sequence"
            )
        )
    assert first_count >= 10
    pending_sources = []
    for source_sequence in source_sequences:
        try:
            reaction.derivation_authority(source_sequence)
        except RollingDerivationPending:
            pending_sources.append(source_sequence)
    assert pending_sources

    lifecycle = LifecycleService(path, reaction_port=reaction)
    _ingest_field(lifecycle, 2)
    with open_v3_connection(path, read_only=True) as connection:
        revised_count = int(connection.execute("SELECT COUNT(*) FROM v3_jobs").fetchone()[0])
        assert int(
            connection.execute("SELECT COUNT(*) FROM v3_rolling_reaction_completions").fetchone()[0]
        ) == int(
            connection.execute("SELECT COUNT(*) FROM v3_rolling_reaction_obligations").fetchone()[0]
        )
    assert revised_count > first_count

    # An ambiguous response exact retry observes the signed completion and does
    # not duplicate any card/component work.
    _ingest_field(lifecycle, 2)
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_jobs").fetchone()[0]) == revised_count
        )


def test_offline_projection_rebuild_restores_obligations_without_reanchoring_cursor(
    tmp_path: Path,
) -> None:
    from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
    from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore
    from tests.v3.integration.test_field_receipts import _bootstrap

    path = tmp_path / "offline-reaction-rebuild.sqlite3"
    store, _field, _build, _lifecycle = _bootstrap(path)
    repository = DurableJobRepository(
        path,
        capacity=_repository(tmp_path / "capacity-offline-rebuild").capacity,
        signer=store._signer,
        trust_store=store._trust_store,
    )
    del repository
    projections = SQLiteProjectionStore(path)
    anchor = SQLiteEventStore(path).current_anchor()
    with open_v3_connection(path) as connection:
        expected_obligations = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM v3_rolling_reaction_obligations "
                "ORDER BY first_global_sequence,reaction_id"
            )
        )
        checkpoints = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM v3_rolling_restart_checkpoints ORDER BY checkpoint_sequence"
            )
        )
        connection.execute("DROP TRIGGER v3_rolling_reaction_obligations_no_delete")
        connection.execute("DELETE FROM v3_rolling_reaction_obligATIONS")
        connection.execute(
            "CREATE TRIGGER v3_rolling_reaction_obligations_no_delete "
            "BEFORE DELETE ON v3_rolling_reaction_obligations BEGIN "
            "SELECT RAISE(ABORT, 'rolling reaction obligation is immutable'); END"
        )
        connection.execute(
            "UPDATE v3_rolling_reaction_cursor SET cursor_digest=? WHERE singleton=1",
            ("f" * 64,),
        )
        connection.commit()

    assert projections.rebuild_rolling_reaction_projection_offline(
        anchor.global_sequence, anchor.event_digest
    ) == (anchor.global_sequence, anchor.event_digest)
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM v3_rolling_reaction_obligations "
                    "ORDER BY first_global_sequence,reaction_id"
                )
            )
            == expected_obligations
        )
        assert (
            str(
                connection.execute(
                    "SELECT cursor_digest FROM v3_rolling_reaction_cursor WHERE singleton=1"
                ).fetchone()[0]
            )
            == "f" * 64
        )
        assert (
            tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM v3_rolling_restart_checkpoints ORDER BY checkpoint_sequence"
                )
            )
            == checkpoints
        )


def test_verified_rolling_projection_matches_replay_without_mutating_tamper(
    tmp_path: Path,
) -> None:
    from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
    from strathmark.v3.infrastructure.sqlite.projections import (
        ProjectionError,
        SQLiteProjectionStore,
    )
    from tests.v3.integration.test_field_receipts import _bootstrap

    path = tmp_path / "verify-reaction-projection.sqlite3"
    _store, _field, _build, _lifecycle = _bootstrap(path)
    projections = SQLiteProjectionStore(path)
    anchor = SQLiteEventStore(path).current_anchor()
    with open_v3_connection(path, read_only=True) as connection:
        expected_digest = projections.projection_digest(connection)
    assert (
        projections.verify_rolling_reaction_projection(anchor.global_sequence, anchor.event_digest)
        == expected_digest
    )

    with open_v3_connection(path) as connection:
        connection.execute("DROP TRIGGER v3_rolling_reaction_obligations_no_delete")
        connection.execute(
            "DELETE FROM v3_rolling_reaction_obligations WHERE reaction_id=("
            "SELECT reaction_id FROM v3_rolling_reaction_obligations "
            "ORDER BY first_global_sequence LIMIT 1)"
        )
        connection.execute(
            "CREATE TRIGGER v3_rolling_reaction_obligations_no_delete "
            "BEFORE DELETE ON v3_rolling_reaction_obligations BEGIN "
            "SELECT RAISE(ABORT, 'rolling reaction obligation is immutable'); END"
        )
        connection.commit()
        tampered_obligations = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM v3_rolling_reaction_obligations "
                "ORDER BY first_global_sequence,reaction_id"
            )
        )
    with pytest.raises(ProjectionError, match="differs from deterministic replay"):
        projections.verify_rolling_reaction_projection(anchor.global_sequence, anchor.event_digest)
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM v3_rolling_reaction_obligations "
                    "ORDER BY first_global_sequence,reaction_id"
                )
            )
            == tampered_obligations
        )


def test_real_result_and_correction_rebuild_only_causal_prospective_packet(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from strathmark.v3.application.field_assembly import FieldAssemblyService
    from strathmark.v3.application.lifecycle import (
        LifecycleService,
        SnapshotKind,
        UpstreamSnapshot,
    )
    from strathmark.v3.contracts.commands import CommandKind
    from strathmark.v3.contracts.events import AggregateKind, EventKind
    from strathmark.v3.contracts.identifiers import IdempotencyKey
    from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
    from strathmark.v3.domain.evidence import LiveResultSubmission
    from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
    from strathmark.v3.infrastructure.sqlite.projections import (
        SQLiteRollingLifecycleResolver,
    )
    from tests.v3.integration.test_approval_projection import _append_lifecycle_event
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    path = tmp_path / "result-correction-reaction.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    future_round = StableIdentifier("round:rolling-successor")
    future_field = StableIdentifier("field:rolling-successor")
    future_context = TargetContext(
        "standing_block",
        field.target_context.size_mm,
        field.target_context.material_code,
        field.target_context.taxonomy_version,
        field.target_context.conversion_version,
    )
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            future_round,
            1,
            field.tournament_id,
            future_round,
            {
                "round_ordinal": 2,
                "predecessor_round_ids": [str(field.round_id)],
                "successor_round_ids": [],
            },
        ),
        command_id=IdempotencyKey("command:rolling-successor-round"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=20,
    )
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            future_field,
            1,
            field.tournament_id,
            future_round,
            {
                "competitor_ids": [str(item.competitor_id) for item in field.ordered_assignments],
                "target_context": future_context.to_dict(),
                "stand_ids": [str(item.stand_id) for item in field.ordered_assignments],
                "capacity_authority_digest": field.capacity_authority_digest,
                "max_field_entrants": field.max_field_entrants,
                "call_order": 2,
                "scheduled_at": "2026-08-24T18:10:00.000Z",
                "deadline_at": "2026-08-24T18:12:00.000Z",
            },
        ),
        command_id=IdempotencyKey("command:rolling-successor-field"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=21,
    )
    signer = store._signer
    trust = store._trust_store
    base_capacity = _repository(tmp_path / "capacity").capacity
    rolling_capacity = replace(
        base_capacity,
        max_queued_jobs=64,
        lanes=(
            LaneCapacity(JobLane.HOT_FIELD, 8, 2),
            LaneCapacity(JobLane.INFERENCE, 60, 4),
            LaneCapacity(JobLane.LOOKUP_RECOVERY, 4, 2),
            LaneCapacity(JobLane.MAINTENANCE, 4, 1),
        ),
    )
    repository = DurableJobRepository(
        path,
        capacity=rolling_capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    council = _council_manifest(signer, bundle_digest=field.bundle_digest)
    coordinator.install_council_authority(council, installed_at=T0)
    resolver = SQLiteRollingLifecycleResolver(
        path,
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        trust_store=trust,
    )
    reaction = RollingLifecycleReactionService(
        event_store=SQLiteEventStore(path),
        coordinator=coordinator,
        resolver=resolver,
        reaction_store=repository,
        clock=lambda: "2026-08-24T18:01:00.000Z",
        test_only_allow_legacy_non_executable=True,
    )
    lifecycle = LifecycleService(path, reaction_port=reaction)
    assembled = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:rolling-result-field",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
        event_kind=EventKind.FIELD_ISSUED,
        aggregate_kind=AggregateKind.FIELD,
        target=field.field_id,
        payload={
            "round_id": str(field.round_id),
            "epoch_id": str(field.tournament_epoch_id),
            "field_revision": field.field_revision,
            "receipt_id": str(assembled.receipt.receipt_id),
            "competitor_ids": [str(item) for item in assembled.receipt.ordered_competitor_ids],
            "issued_marks": {
                str(item.competitor_id): item.mark for item in assembled.receipt.marks
            },
        },
        command_id="command:rolling-result-issue",
        occurred_at="2026-08-24T18:00:30.000Z",
    )
    assignment = field.ordered_assignments[0]
    issued_context_key = repository.current_rolling_card_key(
        str(assignment.competitor_id), field.target_context.digest
    )
    mark = next(
        item.mark
        for item in assembled.receipt.marks
        if item.competitor_id == assignment.competitor_id
    )

    def submission(raw_time_ms: int, revision: int) -> LiveResultSubmission:
        return LiveResultSubmission(
            StableIdentifier(f"evidence:rolling-correction-{revision}"),
            assignment.competitor_id,
            field.tournament_id,
            field.round_id,
            field.field_id,
            field.target_context,
            "2026-08-24T18:00:30.500Z",
            mark,
            raw_time_ms + mark * 1_000,
            1,
            0,
            OfficialResult(
                ResultStatus.COMPLETION,
                raw_time_ms,
                None,
                revision,
                None if revision == 1 else revision - 1,
            ),
            canonical_digest({"raw_time_ms": raw_time_ms, "revision": revision}),
        )

    first = lifecycle.record_live_result(
        submission(41_000, 1),
        field_revision=field.field_revision,
        claimed_receipt_id=assembled.receipt.receipt_id,
        command_id=IdempotencyKey("command:rolling-result-original"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:30.500Z",
        monotonic_elapsed_ms=41,
    )
    first_key = repository.current_rolling_card_key(
        str(assignment.competitor_id), future_context.digest
    )
    assert first_key is not None
    assert first_key["dependency_revision"] == first.first_global_sequence
    first_jobs = repository.records_for_card(first_key["card_digest"])
    assert len(first_jobs) == 5
    assert (
        first_jobs[0].payload()["evidence_packet"]["observations"][0]["result"]["raw_time_ms"]
        == 41_000
    )
    event_store = SQLiteEventStore(path)
    first_events = tuple(
        event_store.event_at(sequence)
        for sequence in range(first.first_global_sequence, first.last_global_sequence + 1)
    )
    first_plan_before_correction = resolver.resolve(first_events).content_value()

    corrected = lifecycle.record_live_result(
        submission(39_000, 2),
        field_revision=field.field_revision,
        claimed_receipt_id=assembled.receipt.receipt_id,
        command_id=IdempotencyKey("command:rolling-result-corrected"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:31.000Z",
        monotonic_elapsed_ms=42,
    )
    corrected_key = repository.current_rolling_card_key(
        str(assignment.competitor_id), future_context.digest
    )
    assert corrected_key is not None
    assert corrected_key["dependency_revision"] == corrected.first_global_sequence
    assert corrected_key["card_digest"] != first_key["card_digest"]
    cancelled_first_jobs = repository.records_for_card(first_key["card_digest"])
    assert all(item.state is JobState.CANCELLED for item in cancelled_first_jobs)
    corrected_jobs = repository.records_for_card(corrected_key["card_digest"])
    assert len(corrected_jobs) == 5
    observations = corrected_jobs[0].payload()["evidence_packet"]["observations"]
    assert len(observations) == 1
    assert observations[0]["result"]["revision"] == 2
    assert observations[0]["result"]["raw_time_ms"] == 39_000
    assert resolver.resolve(first_events).content_value() == first_plan_before_correction
    assert (
        repository.current_rolling_card_key(
            str(assignment.competitor_id), field.target_context.digest
        )
        == issued_context_key
    )


def test_delayed_reaction_recovery_does_not_backdate_expired_provider_work(
    tmp_path: Path,
) -> None:
    import shutil

    from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
    from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
    from strathmark.v3.infrastructure.sqlite.projections import (
        SQLiteRollingLifecycleResolver,
    )
    from tests.v3.integration.test_field_receipts import _bootstrap

    path = tmp_path / "delayed-reaction.sqlite3"
    store, field, _build, _lifecycle = _bootstrap(path)
    forged_path = tmp_path / "delayed-reaction-forged.sqlite3"
    shutil.copy2(path, forged_path)
    with open_v3_connection(forged_path) as connection:
        reaction_id = str(
            connection.execute(
                "SELECT reaction_id FROM v3_rolling_reaction_obligations "
                "ORDER BY first_global_sequence LIMIT 1"
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO v3_rolling_reaction_completions VALUES (?,?,?,?,?)",
            (
                reaction_id,
                "e" * 64,
                "2026-08-24T18:00:00.000Z",
                "d" * 64,
                "{}",
            ),
        )
        connection.commit()
    with pytest.raises(DurableJobError, match="reaction completion integrity"):
        DurableJobRepository(
            forged_path,
            capacity=_repository(tmp_path / "forged-capacity").capacity,
            signer=store._signer,
            trust_store=store._trust_store,
        )
    repository = DurableJobRepository(
        path,
        capacity=_repository(tmp_path / "capacity").capacity,
        signer=store._signer,
        trust_store=store._trust_store,
    )
    coordinator = DurableRollingPreparationCoordinator(
        repository, signer=store._signer, trust_store=store._trust_store
    )
    council = _council_manifest(store._signer, bundle_digest=field.bundle_digest)
    coordinator.install_council_authority(council, installed_at=T0)

    RollingLifecycleReactionService(
        event_store=SQLiteEventStore(path),
        coordinator=coordinator,
        resolver=SQLiteRollingLifecycleResolver(
            path,
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
            council_manifest_digest=council.body_digest,
            trust_store=store._trust_store,
        ),
        reaction_store=repository,
        clock=lambda: "2026-08-24T19:00:00.000Z",
        test_only_allow_legacy_non_executable=True,
    )

    with open_v3_connection(path, read_only=True) as connection:
        assert int(connection.execute("SELECT COUNT(*) FROM v3_jobs").fetchone()[0]) == 0
        completions = tuple(
            connection.execute("SELECT completed_at FROM v3_rolling_reaction_completions")
        )
    assert completions
    assert {str(row[0]) for row in completions} == {"2026-08-24T19:00:00.000Z"}
    repository.verify_rolling_storage()

    tampered_path = tmp_path / "delayed-reaction-tampered.sqlite3"
    shutil.copy2(path, tampered_path)
    tampered = DurableJobRepository(
        tampered_path,
        capacity=repository.capacity,
        signer=store._signer,
        trust_store=store._trust_store,
    )
    with open_v3_connection(tampered_path) as connection:
        connection.execute("DROP TRIGGER v3_rolling_reaction_completions_no_update")
        connection.execute(
            "UPDATE v3_rolling_reaction_completions SET plan_digest=? "
            "WHERE reaction_id=(SELECT reaction_id FROM v3_rolling_reaction_completions "
            "ORDER BY reaction_id LIMIT 1)",
            ("f" * 64,),
        )
        connection.commit()
    with pytest.raises(DurableJobError, match="reaction completion integrity"):
        tampered.verify_rolling_storage()


def test_live_rolling_reaction_requires_executable_council_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

    repository = _repository(tmp_path)
    coordinator = DurableRollingPreparationCoordinator(
        repository, signer=repository._signer, trust_store=repository._trust_store
    )

    class Resolver:
        def resolve(self, _events):  # pragma: no cover - construction must fail first
            raise AssertionError("legacy resolver must not run")

    with pytest.raises(DurableJobError, match="executable council"):
        RollingLifecycleReactionService(
            event_store=SQLiteEventStore(repository.database_path),
            coordinator=coordinator,
            resolver=Resolver(),
            reaction_store=repository,
            clock=lambda: T0,
        )
    monkeypatch.delenv("STRATHMARK_TEST_DB")
    with pytest.raises(DurableJobError, match="isolated test harness"):
        RollingLifecycleReactionService(
            event_store=SQLiteEventStore(repository.database_path),
            coordinator=coordinator,
            resolver=Resolver(),
            reaction_store=repository,
            clock=lambda: T0,
            test_only_allow_legacy_non_executable=True,
        )


def test_reaction_recovery_preserves_head_of_line_after_older_failure(
    tmp_path: Path,
) -> None:
    from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
    from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
    from strathmark.v3.infrastructure.sqlite.projections import (
        SQLiteRollingLifecycleResolver,
    )
    from tests.v3.integration.test_field_receipts import _bootstrap

    path = tmp_path / "reaction-head-of-line.sqlite3"
    store, field, _build, _lifecycle = _bootstrap(path)
    repository = DurableJobRepository(
        path,
        capacity=_repository(tmp_path / "capacity").capacity,
        signer=store._signer,
        trust_store=store._trust_store,
    )
    coordinator = DurableRollingPreparationCoordinator(
        repository, signer=store._signer, trust_store=store._trust_store
    )
    council = _council_manifest(store._signer, bundle_digest=field.bundle_digest)
    coordinator.install_council_authority(council, installed_at=T0)
    resolver = SQLiteRollingLifecycleResolver(
        path,
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        trust_store=store._trust_store,
    )

    class FailingOldestResolver:
        def resolve(self, events):
            raise DurableJobError(f"blocked-oldest:{events[0].global_sequence}")

    with pytest.raises(DurableJobError, match="blocked-oldest"):
        RollingLifecycleReactionService(
            event_store=SQLiteEventStore(path),
            coordinator=coordinator,
            resolver=FailingOldestResolver(),
            reaction_store=repository,
            clock=lambda: "2026-08-24T18:00:01.000Z",
            test_only_allow_legacy_non_executable=True,
        )
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_rolling_reaction_obligations"
                ).fetchone()[0]
            )
            >= 2
        )
        assert (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM v3_rolling_reaction_completions"
                ).fetchone()[0]
            )
            == 0
        )

    RollingLifecycleReactionService(
        event_store=SQLiteEventStore(path),
        coordinator=coordinator,
        resolver=resolver,
        reaction_store=repository,
        clock=lambda: "2026-08-24T18:00:01.000Z",
        test_only_allow_legacy_non_executable=True,
    )
    assert repository.pending_rolling_reactions(limit=12) == ()


def test_epoch_close_cancels_pending_work_and_restart_does_not_reopen_it(
    tmp_path: Path,
) -> None:
    import shutil

    from strathmark.v3.application.field_assembly import FieldAssemblyService
    from strathmark.v3.contracts.canonical import canonical_digest
    from strathmark.v3.contracts.commands import CommandKind
    from strathmark.v3.contracts.events import AggregateKind, EventKind
    from strathmark.v3.contracts.identifiers import IdempotencyKey
    from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
    from strathmark.v3.domain.epochs import MandatoryReaction
    from strathmark.v3.domain.evidence import LiveResultSubmission
    from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
    from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
    from strathmark.v3.infrastructure.sqlite.projections import (
        SQLiteRollingLifecycleResolver,
    )
    from tests.v3.integration.test_approval_projection import _append_lifecycle_event
    from tests.v3.integration.test_field_receipts import NOW, _bootstrap

    signer = P256EphemeralSigner.generate("integrity-key:rolling-close")
    path = tmp_path / "close.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    trust = IntegrityTrustStore((signer.identity, store._signer.identity))
    repository = DurableJobRepository(
        path,
        capacity=_repository(tmp_path / "capacity").capacity,
        signer=signer,
        trust_store=trust,
    )
    coordinator = DurableRollingPreparationCoordinator(repository, signer=signer, trust_store=trust)
    council = _council_manifest(signer)
    coordinator.install_council_authority(council, installed_at=T0)
    candidate, card = _rolling_authority(signer, epoch_id=str(field.tournament_epoch_id))
    coordinator.schedule(
        (candidate,),
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        observed_at=T0,
    )
    result_by_component = {
        "formula": card.forecasts[0].commit_digest,
        "ml": card.forecasts[1].commit_digest,
        "local_qwen35_9b": "4" * 64,
        "local_ministral3_8b": "5" * 64,
        "frontier_cloud": "6" * 64,
    }
    for ordinal in range(1, 6):
        claimed = repository.claim(
            JobLane.INFERENCE,
            worker_id=f"worker:rolling-close-{ordinal}",
            clock=lambda: T1,
            lease_duration_ms=60_000,
        )
        assert claimed is not None
        repository.commit_success(
            claimed.job_id,
            claimed.job_revision,
            worker_id=f"worker:rolling-close-{ordinal}",
            fencing_token=claimed.fencing_token,
            result_digest=result_by_component[claimed.payload()["component_id"]],
            current_context=lambda _connection, _record: (
                candidate.key.evidence_digest,
                candidate.key.bundle_digest,
            ),
            clock=lambda: T2,
        )
    publication = coordinator.seal_card(
        candidate.key,
        card,
        council_manifest_digest=council.body_digest,
        council_aggregate_authority=_aggregate_manifest(
            signer, candidate, card, council, repository
        ),
        observed_at=T2,
    )
    pending, _pending_card = _rolling_authority(
        signer,
        epoch_id=str(field.tournament_epoch_id),
        competitor="rolling-b",
    )
    coordinator.schedule(
        (pending,),
        capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
        council_manifest_digest=council.body_digest,
        observed_at=T2,
    )
    assembled = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:rolling-close-field",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
        event_kind=EventKind.FIELD_ISSUED,
        aggregate_kind=AggregateKind.FIELD,
        target=field.field_id,
        payload={
            "round_id": str(field.round_id),
            "epoch_id": str(field.tournament_epoch_id),
            "field_revision": field.field_revision,
            "receipt_id": str(assembled.receipt.receipt_id),
            "competitor_ids": [str(item) for item in assembled.receipt.ordered_competitor_ids],
            "issued_marks": {
                str(item.competitor_id): item.mark for item in assembled.receipt.marks
            },
        },
        command_id="command:rolling-issue-current-field",
        occurred_at="2026-08-24T18:00:30.000Z",
    )
    marks = {str(item.competitor_id): item.mark for item in assembled.receipt.marks}
    result_sources = []
    for placing, assignment in enumerate(field.ordered_assignments, start=1):
        competitor_id = assignment.competitor_id
        raw_time_ms = 40_000 + placing * 1_000
        recorded = lifecycle.record_live_result(
            LiveResultSubmission(
                StableIdentifier(f"evidence:rolling-close-{placing}"),
                competitor_id,
                field.tournament_id,
                field.round_id,
                field.field_id,
                field.target_context,
                "2026-08-24T18:00:30.500Z",
                marks[str(competitor_id)],
                raw_time_ms + marks[str(competitor_id)] * 1_000,
                placing,
                0 if placing == 1 else 1_000,
                OfficialResult(ResultStatus.COMPLETION, raw_time_ms, None, 1, None),
                canonical_digest({"competitor_id": str(competitor_id), "placing": placing}),
            ),
            field_revision=field.field_revision,
            claimed_receipt_id=assembled.receipt.receipt_id,
            command_id=IdempotencyKey(f"command:rolling-result-{placing}"),
            actor_id=StableIdentifier("actor:manager"),
            occurred_at_utc="2026-08-24T18:00:30.500Z",
            monotonic_elapsed_ms=40 + placing,
        )
        result_sources.append(recorded.first_global_sequence)
    lifecycle.settle_live_race(
        field.field_id,
        field_revision=field.field_revision,
        claimed_receipt_id=assembled.receipt.receipt_id,
        command_id=IdempotencyKey("command:rolling-settle-field"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:30.750Z",
        monotonic_elapsed_ms=45,
    )
    for source in result_sources:
        for ordinal, reaction in enumerate(MandatoryReaction, start=1):
            lifecycle.complete_derivation_reaction(
                source,
                reaction,
                canonical_digest({"source": source, "reaction": reaction.value}),
                command_id=IdempotencyKey(f"command:rolling-reaction-{source}-{reaction.value}"),
                actor_id=StableIdentifier("actor:manager"),
                occurred_at_utc="2026-08-24T18:00:30.900Z",
                monotonic_elapsed_ms=45 + ordinal,
            )
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.BEGIN_ROUND_CLOSING,
        event_kind=EventKind.ROUND_CLOSING_STARTED,
        aggregate_kind=AggregateKind.ROUND,
        target=field.round_id,
        payload={"closing": True},
        command_id="command:rolling-begin-close-round",
        occurred_at="2026-08-24T18:00:31.000Z",
    )
    lifecycle.close_evidence_round(
        field.round_id,
        command_id=IdempotencyKey("command:rolling-close-round"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:32.000Z",
        monotonic_elapsed_ms=51,
    )
    lifecycle.close_tournament(
        field.tournament_id,
        command_id=IdempotencyKey("command:rolling-close-tournament"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-24T18:00:33.000Z",
        monotonic_elapsed_ms=52,
    )
    close_event = lifecycle._events.event_at(lifecycle._events.event_count())
    corrupt_path = tmp_path / "close-corrupt.sqlite3"
    shutil.copy2(path, corrupt_path)
    corrupt_repository = DurableJobRepository(
        corrupt_path,
        capacity=repository.capacity,
        signer=signer,
        trust_store=trust,
    )
    with open_v3_connection(corrupt_path) as connection:
        connection.execute("DROP TRIGGER v3_rolling_card_status_history_no_update")
        connection.execute(
            "UPDATE v3_rolling_card_status_history SET status_digest=? WHERE status_sequence=1",
            ("f" * 64,),
        )
        connection.commit()
        before_tamper_recovery = (
            connection.execute("SELECT COUNT(*) FROM v3_rolling_card_status_history").fetchone()[0],
            connection.execute(
                "SELECT status_digest FROM v3_rolling_card_status_history WHERE status_sequence=1"
            ).fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM v3_rolling_card_current").fetchone()[0],
        )
    with pytest.raises(DurableJobError, match="status.*integrity"):
        DurableRollingPreparationCoordinator(corrupt_repository, signer=signer, trust_store=trust)
    with open_v3_connection(corrupt_path, read_only=True) as connection:
        assert before_tamper_recovery == (
            connection.execute("SELECT COUNT(*) FROM v3_rolling_card_status_history").fetchone()[0],
            connection.execute(
                "SELECT status_digest FROM v3_rolling_card_status_history WHERE status_sequence=1"
            ).fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM v3_rolling_card_current").fetchone()[0],
        )
    # Simulate a crash after the canonical lifecycle close but before its
    # deferred rolling reaction. Repository restart replays the close event,
    # writes the fence, and completes the cancellation sweep.
    restarted_repository = DurableJobRepository(
        path,
        capacity=repository.capacity,
        signer=signer,
        trust_store=trust,
    )
    restarted = DurableRollingPreparationCoordinator(
        restarted_repository, signer=signer, trust_store=trust
    )
    RollingLifecycleReactionService(
        event_store=SQLiteEventStore(path),
        coordinator=restarted,
        resolver=SQLiteRollingLifecycleResolver(
            path,
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
            council_manifest_digest=council.body_digest,
            trust_store=trust,
        ),
        reaction_store=restarted_repository,
        clock=lambda: "2026-08-24T19:00:00.000Z",
        test_only_allow_legacy_non_executable=True,
    )
    assert restarted_repository.pending_rolling_reactions(limit=12) == ()
    repository = restarted_repository
    assert (
        repository.claim(
            JobLane.INFERENCE,
            worker_id="worker:after-close",
            clock=lambda: close_event.occurred_at_utc,
            lease_duration_ms=60_000,
        )
        is None
    )
    cancelled = tuple(
        item
        for item in repository.records_for_card(pending.key.card_digest)
        if item.state is JobState.CANCELLED
    )
    assert (
        coordinator.close_epoch(
            candidate.key.tournament_epoch_id,
            source_event=close_event,
            observed_at=close_event.occurred_at_utc,
        )
        == ()
    )
    assert len(cancelled) == 5
    assert all(item.state is JobState.CANCELLED for item in cancelled)
    assert all(
        row["publication_digest"] != publication.publication_digest
        for row in repository.rolling_current_rows()
    )
    with pytest.raises(KeyError):
        restarted.cached(candidate.key)
    readiness = restarted.readiness((candidate.key,), observed_at=T2)
    assert readiness.ready_count == 0
    assert readiness.failed_count == 1
    assert not readiness.all_ready
    revised, _revised_card = _rolling_authority(
        signer, revision=2, epoch_id=str(field.tournament_epoch_id)
    )
    with pytest.raises(DurableJobError, match="epoch is closed"):
        restarted.schedule(
            (revised,),
            capacity_use=CapacityUse(1, 12, 6, 12, 12, 1024, 4096, 25),
            council_manifest_digest=council.body_digest,
            observed_at="2026-08-24T18:00:34.000Z",
        )
    assert repository.records_for_card(revised.key.card_digest) == ()
