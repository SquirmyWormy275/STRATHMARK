"""Measure the real V3 final-result-to-approval-ready race-day path on Windows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STRATHMARK_TEST_DB", "1")
os.environ.setdefault(
    "STRATHMARK_DB_PATH", str(ROOT / ".tmp" / "v3-result-to-ready-import.sqlite3")
)

from strathmark.v3.application.capacity import CapacityManifest, CapacityUse, JobLane  # noqa: E402
from strathmark.v3.application.coordinator import (  # noqa: E402
    DurableRollingPreparationCoordinator,
    PreparationCandidate,
)
from strathmark.v3.application.credibility_reactions import (  # noqa: E402
    SQLiteCredibilityReactionService,
    seal_credibility_policy,
)
from strathmark.v3.application.field_assembly import (  # noqa: E402
    FieldAssemblyService,
    FrozenEntrantAssignment,
    FrozenFieldRevision,
    OperationalWeightAuthority,
    OperationalWeightKind,
    RollingCapabilityBinding,
    WeightAuthorityBinding,
    live_effective_weight_receipt_digest,
    seal_competitor_card_authority,
    seal_field_capacity_authority,
)
from strathmark.v3.application.pipeline_builder import (  # noqa: E402
    RollingCapabilityAuthority,
    RollingCurrentCard,
    RollingFieldBuildInputs,
    RollingFieldPipelineBuilder,
)
from strathmark.v3.application.settlement import SettlementCommand, SettlementService  # noqa: E402
from strathmark.v3.contracts.canonical import canonical_digest  # noqa: E402
from strathmark.v3.contracts.commands import CommandKind  # noqa: E402
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind  # noqa: E402
from strathmark.v3.contracts.forecasts import (  # noqa: E402
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
)
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier  # noqa: E402
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus  # noqa: E402
from strathmark.v3.domain.capability import CapabilityState  # noqa: E402
from strathmark.v3.domain.credibility import (  # noqa: E402
    ContextNode,
    CredibilityPolicy,
    WeightReceipt,
)
from strathmark.v3.domain.epochs import MandatoryReaction  # noqa: E402
from strathmark.v3.domain.evidence import LiveResultSubmission  # noqa: E402
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection  # noqa: E402
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore  # noqa: E402
from strathmark.v3.infrastructure.sqlite.jobs import DurableJobRepository  # noqa: E402
from strathmark.v3.infrastructure.sqlite.projections import (  # noqa: E402
    SQLiteRollingLifecycleResolver,
)

RESULT_TO_READY_BUDGET_MS = 120_000
FORMAL_REPETITIONS = 5
MEASURED_COMPONENTS = (
    "final_heat_settlement",
    "deliberate_round_close",
    "newly_affected_cards",
    "gate_optimizer",
    "receipt_commit",
    "approval_projection",
)
CAPACITY_PATH = ROOT / "benchmarks" / "v3" / "job_capacity_manifest.json"
SOURCE_PATHS = (
    "scripts/benchmark_v3_result_to_ready.py",
    "strathmark/v3/application/lifecycle.py",
    "strathmark/v3/application/settlement.py",
    "strathmark/v3/application/coordinator.py",
    "strathmark/v3/application/pipeline_builder.py",
    "strathmark/v3/application/field_assembly.py",
    "strathmark/v3/application/approval.py",
    "strathmark/v3/domain/optimizer.py",
    "strathmark/v3/infrastructure/sqlite/event_store.py",
    "strathmark/v3/infrastructure/sqlite/jobs.py",
    "strathmark/v3/infrastructure/sqlite/projections.py",
    "tests/v3/integration/test_field_receipts.py",
    "tests/v3/integration/test_rolling_preparation.py",
)


def _elapsed_ms(started_ns: int) -> int:
    return max(1, (perf_counter_ns() - started_ns + 999_999) // 1_000_000)


def _event_set_digest(events: tuple[Any, ...]) -> str:
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-event-set-v1",
            "events": [
                {
                    "global_sequence": event.global_sequence,
                    "event_id": str(event.event_id),
                    "event_digest": event.event_digest,
                }
                for event in events
            ],
        }
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_bindings() -> dict[str, str]:
    bindings = {path: _sha256(ROOT / path) for path in SOURCE_PATHS}
    bindings["benchmarks/v3/job_capacity_manifest.json"] = _sha256(CAPACITY_PATH)
    return bindings


def _card_for_candidate(candidate: PreparationCandidate, signer: Any, ordinal: int) -> Any:
    from tests.v3.integration.test_field_receipts import _distribution

    packet = candidate.evidence_packet
    if packet is None:
        raise RuntimeError("affected card candidate omitted its evidence packet")
    forecasts = tuple(
        AssessorForecast.create(
            forecast_id=StableIdentifier(f"forecast:r158-{ordinal}-{kind.value}"),
            assessor=kind,
            state=ForecastState.COMMITTED,
            evidence_digest=packet.content_digest,
            distribution=_distribution(40_000 + ordinal * 1_000 + offset),
            support=EvidenceSupport(
                len(packet.observations),
                str(len(packet.observations)),
                len(packet.observations),
                str(packet.historical_cutoff_key),
                21,
            ),
            warnings=(),
            artifacts=(),
            abstention_code=None,
        )
        for kind, offset in zip(
            (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL),
            (-500, 0, 500),
            strict=True,
        )
    )
    return seal_competitor_card_authority(
        packet,
        forecasts,
        bundle_digest=candidate.key.bundle_digest,
        signer=signer,
        created_at="2026-08-25T18:00:01.000Z",
    )


def _finish_affected_cards(
    repository: DurableJobRepository,
    coordinator: DurableRollingPreparationCoordinator,
    candidates: tuple[PreparationCandidate, ...],
    council: Any,
    signer: Any,
) -> tuple[Any, ...]:
    from tests.v3.integration.test_rolling_preparation import _aggregate_manifest

    cards = tuple(
        _card_for_candidate(candidate, signer, card_ordinal)
        for card_ordinal, candidate in enumerate(candidates, start=1)
    )
    results_by_card = {}
    for candidate, card in zip(candidates, cards, strict=True):
        records = repository.records_for_card(candidate.key.card_digest)
        if len(records) != 5:
            raise RuntimeError("affected card did not schedule every component")
        results_by_card[candidate.key.card_digest] = {
            "formula": card.forecasts[0].commit_digest,
            "ml": card.forecasts[1].commit_digest,
            "local_qwen35_9b": canonical_digest(
                {"card": candidate.key.card_digest, "member": "local_qwen35_9b"}
            ),
            "local_ministral3_8b": canonical_digest(
                {"card": candidate.key.card_digest, "member": "local_ministral3_8b"}
            ),
            "frontier_cloud": canonical_digest(
                {"card": candidate.key.card_digest, "member": "frontier_cloud"}
            ),
        }
    for job_ordinal in range(1, len(candidates) * 5 + 1):
        worker_id = f"worker:r158-{job_ordinal}"
        claimed = repository.claim(
            JobLane.INFERENCE,
            worker_id=worker_id,
            clock=lambda: "2026-08-25T18:00:03.300Z",
            lease_duration_ms=60_000,
        )
        if claimed is None:
            raise RuntimeError("affected card component queue drained early")
        payload = claimed.payload()
        repository.commit_success(
            claimed.job_id,
            claimed.job_revision,
            worker_id=worker_id,
            fencing_token=claimed.fencing_token,
            result_digest=results_by_card[payload["card_key"]["card_digest"]][
                payload["component_id"]
            ],
            current_context=lambda _connection, record: (
                record.evidence_digest,
                record.bundle_digest,
            ),
            clock=lambda: "2026-08-25T18:00:03.400Z",
        )
    publications = []
    for candidate, card in zip(candidates, cards, strict=True):
        publications.append(
            coordinator.seal_card(
                candidate.key,
                card,
                council_manifest_digest=council.body_digest,
                council_aggregate_authority=_aggregate_manifest(
                    signer, candidate, card, council, repository
                ),
                observed_at="2026-08-25T18:00:03.450Z",
            )
        )
    return tuple(publications)


def _future_field(
    original: FrozenFieldRevision,
    candidates: tuple[PreparationCandidate, ...],
    *,
    epoch: Any,
) -> FrozenFieldRevision:
    dependency_revision = candidates[0].key.dependency_revision
    if any(candidate.key.dependency_revision != dependency_revision for candidate in candidates):
        raise RuntimeError("affected cards do not share one causal source sequence")
    return FrozenFieldRevision.create(
        tournament_id=original.tournament_id,
        round_id="round:r158-final",
        field_id="field:r158-final",
        field_revision=1,
        assignments=tuple(
            FrozenEntrantAssignment.create(
                candidate.key.competitor_id,
                f"stand:r158-{index}",
                index,
            )
            for index, candidate in enumerate(candidates)
        ),
        target_context=original.target_context,
        historical_cutoff_key=original.historical_cutoff_key,
        tournament_epoch_id=epoch.epoch_id,
        tournament_event_sequence=epoch.maximum_tournament_sequence,
        evidence_digest=epoch.content_digest,
        bundle_digest=original.bundle_digest,
        capacity_authority_digest=original.capacity_authority_digest,
        max_field_entrants=original.max_field_entrants,
        call_order=2,
        scheduled_at="2026-08-25T18:03:00.000Z",
        deadline_at="2026-08-25T18:05:00.000Z",
    )


def _bootstrap_benchmark(database_path: Path, trial_ordinal: int) -> tuple[Any, ...]:
    from strathmark.v3.application.lifecycle import LifecycleService, SnapshotKind, UpstreamSnapshot
    from strathmark.v3.domain.joint_dependence import DependencePolicy, train_dependence_artifact
    from strathmark.v3.infrastructure.integrity import (
        IntegrityTrustStore,
        P256EphemeralSigner,
        sign_manifest,
    )
    from strathmark.v3.infrastructure.sqlite.projections import SQLiteFieldProjectionStore
    from tests.v3.integration.test_approval_projection import _append_lifecycle_event
    from tests.v3.integration.test_field_receipts import (
        ACTOR,
        NOW,
        _append_config,
        _capacity_manifest,
        _field,
        _ingest_field,
        _pipeline,
    )

    lifecycle = LifecycleService(database_path)
    tournament = StableIdentifier("tournament:show")
    heat_round = StableIdentifier("round:final")
    future_round = StableIdentifier("round:r158-final")
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:cutoff"},
        ),
        command_id=IdempotencyKey(f"command:r158-tournament-snapshot-{trial_ordinal}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    for ordinal, round_id, predecessors, successors in (
        (1, heat_round, [], [str(future_round)]),
        (2, future_round, [str(heat_round)], []),
    ):
        lifecycle.ingest_snapshot(
            UpstreamSnapshot(
                SnapshotKind.ROUND,
                round_id,
                1,
                tournament,
                round_id,
                {
                    "round_ordinal": ordinal,
                    "predecessor_round_ids": predecessors,
                    "successor_round_ids": successors,
                },
            ),
            command_id=IdempotencyKey(f"command:r158-round-snapshot-{trial_ordinal}-{ordinal}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=ordinal + 1,
        )
    _append_config(
        lifecycle,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
    )
    _append_config(
        lifecycle,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        heat_round,
    )
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.CONFIGURE_ROUND,
        event_kind=EventKind.ROUND_CONFIGURED,
        aggregate_kind=AggregateKind.ROUND,
        target=future_round,
        payload={"configured": True},
        command_id=f"command:r158-configure-successor-{trial_ordinal}",
        occurred_at=NOW,
    )
    lifecycle.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:cutoff",
        root_round_ids=(heat_round,),
        command_id=IdempotencyKey(f"command:r158-open-{trial_ordinal}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    signer = P256EphemeralSigner.generate(f"integrity-key:r158-{trial_ordinal}")
    trust_store = IntegrityTrustStore((signer.identity,))
    capacity = _capacity_manifest()
    capacity_authority = seal_field_capacity_authority(
        capacity,
        bundle_digest=canonical_digest({"bundle_id": "bundle:verified"}),
        signer=signer,
        created_at=NOW,
    )
    credibility = SQLiteCredibilityReactionService(
        database_path,
        trust_store=trust_store,
        consequence_evaluator=None,
        policy_manifest=seal_credibility_policy(
            CredibilityPolicy(),
            optimizer_bundle_digest="e" * 64,
            signer=signer,
            created_at=NOW,
        ),
    )
    context = ContextNode("underhand", "300_349", "pine")
    baseline_receipt = credibility._tournament_baseline(
        tournament,
        context,
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    with open_v3_connection(database_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? "
            "AND event_kind=? ORDER BY global_sequence DESC LIMIT 1",
            (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
        ).fetchone()
    if row is None:
        raise RuntimeError("root weight event is missing")
    weight_event = EventEnvelope.from_dict(json.loads(str(row[0])))
    weight_payload = weight_event.command.payload.to_value()
    epoch, _ = lifecycle.freeze_round_epoch(
        heat_round,
        epoch_revision=1,
        historical_cutoff_key="history:cutoff",
        closure_ids=(),
        command_id=IdempotencyKey(f"command:r158-freeze-root-{trial_ordinal}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=6,
    )
    store = SQLiteFieldProjectionStore(database_path, signer=signer, trust_store=trust_store)
    store.install_capacity_authority(capacity_authority, installed_at=NOW)
    _ingest_field(lifecycle, 1, capacity=capacity)
    heat = _field(
        evidence_digest=epoch.content_digest,
        tournament_event_sequence=epoch.maximum_tournament_sequence,
        tournament_epoch_id=epoch.epoch_id,
        capacity_authority_digest=capacity_authority.authority_digest,
        max_field_entrants=capacity.max_field_entrants,
    )
    binding = WeightAuthorityBinding.pending(
        baseline_receipt,
        ledger_projection_digest=weight_payload["baseline_ledger_projection_digest"],
        tournament_event_sequence=heat.tournament_event_sequence,
        source_global_sequence=weight_event.global_sequence,
    )
    operational = OperationalWeightAuthority.create(
        kind=OperationalWeightKind.ROOT_BASELINE,
        binding=binding,
        tournament_id=tournament,
        round_id=heat_round,
        epoch_id=epoch.epoch_id,
        epoch_digest=epoch.content_digest,
        frozen_tournament_sequence=epoch.maximum_tournament_sequence,
        authority_event_sequence=weight_event.global_sequence,
        authority_event_digest=weight_event.event_digest,
        completed_round_id=None,
        round_close_event_digest=None,
        baseline_receipt_digest=baseline_receipt.receipt_digest,
    )
    artifact = train_dependence_artifact(
        (),
        baseline_receipt.context,
        1,
        DependencePolicy(),
        artifact_id=StableIdentifier("artifact:dependence"),
        training_evidence_digest="3" * 64,
        active_projection_digest="4" * 64,
        promotion_receipt_digest="5" * 64,
    )
    promotion = sign_manifest(
        "field_dependence_authority",
        {
            "schema_version": "strathmark-v3-field-dependence-promotion-v1",
            "purpose": "field_dependence_operational",
            "artifact": artifact.to_dict(),
            "promotion_receipt_digest": artifact.promotion_receipt_digest,
        },
        signer=signer,
        created_at=NOW,
    )
    store.install_weight_authority(operational, installed_at=NOW)
    store.install_dependence_authority(artifact, promotion_manifest=promotion, installed_at=NOW)

    def build(revision: FrozenFieldRevision) -> Any:
        return _pipeline(
            revision,
            receipt=baseline_receipt,
            authority=binding,
            operational_authority=operational,
            artifact=artifact,
            card_signer=signer,
            authority_signer=signer,
        )

    return store, heat, build, lifecycle


def _run_trial(database_path: Path, trial_ordinal: int) -> dict[str, Any]:
    from strathmark.v3.application.lifecycle import SnapshotKind, UpstreamSnapshot
    from tests.v3.integration.test_approval_projection import _append_lifecycle_event
    from tests.v3.integration.test_field_receipts import (
        NOW,
        _capability,
    )
    from tests.v3.integration.test_rolling_preparation import _council_manifest

    store, heat, fixture_build, lifecycle = _bootstrap_benchmark(database_path, trial_ordinal)
    baseline = fixture_build(heat)
    future_round = StableIdentifier("round:r158-final")
    heat_receipt = FieldAssemblyService(store).assemble(
        field=heat,
        caller_namespace="manager",
        request_identity=f"idempotency:r158-heat-{trial_ordinal}",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: baseline,
    )
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
        event_kind=EventKind.FIELD_ISSUED,
        aggregate_kind=AggregateKind.FIELD,
        target=heat.field_id,
        payload={
            "round_id": str(heat.round_id),
            "epoch_id": str(heat.tournament_epoch_id),
            "field_revision": heat.field_revision,
            "receipt_id": str(heat_receipt.receipt.receipt_id),
            "competitor_ids": [str(item) for item in heat_receipt.receipt.ordered_competitor_ids],
            "issued_marks": {
                str(item.competitor_id): item.mark for item in heat_receipt.receipt.marks
            },
        },
        command_id=f"command:r158-issue-{trial_ordinal}",
        occurred_at="2026-08-25T18:00:00.000Z",
    )
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier("field:r158-final"),
            1,
            heat.tournament_id,
            future_round,
            {
                "competitor_ids": [str(item.competitor_id) for item in heat.ordered_assignments],
                "target_context": heat.target_context.to_dict(),
                "stand_ids": [
                    f"stand:r158-{index}" for index in range(len(heat.ordered_assignments))
                ],
                "capacity_authority_digest": heat.capacity_authority_digest,
                "max_field_entrants": heat.max_field_entrants,
                "call_order": 2,
                "scheduled_at": "2026-08-25T18:03:00.000Z",
                "deadline_at": "2026-08-25T18:05:00.000Z",
            },
        ),
        command_id=IdempotencyKey(f"command:r158-field-snapshot-{trial_ordinal}"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-25T18:00:00.200Z",
        monotonic_elapsed_ms=11,
    )

    capacity = CapacityManifest.load(CAPACITY_PATH)
    repository = DurableJobRepository(
        database_path,
        capacity=capacity,
        signer=store._signer,
        trust_store=store._trust_store,
    )
    coordinator = DurableRollingPreparationCoordinator(
        repository, signer=store._signer, trust_store=store._trust_store
    )
    council = _council_manifest(store._signer, bundle_digest=heat.bundle_digest)
    coordinator.install_council_authority(council, installed_at="2026-08-25T18:00:00.250Z")
    capacity_use = CapacityUse(1, 12, 2, 2, 2, 1_024, 4_096, 25)
    resolver = SQLiteRollingLifecycleResolver(
        database_path,
        capacity_use=capacity_use,
        council_manifest_digest=council.body_digest,
        trust_store=store._trust_store,
    )

    marks = {str(item.competitor_id): item.mark for item in heat_receipt.receipt.marks}
    submissions = tuple(
        LiveResultSubmission(
            StableIdentifier(f"evidence:r158-{trial_ordinal}-{placing}"),
            assignment.competitor_id,
            heat.tournament_id,
            heat.round_id,
            heat.field_id,
            heat.target_context,
            "2026-08-25T18:00:00.500Z",
            marks[str(assignment.competitor_id)],
            40_000 + placing * 1_000 + marks[str(assignment.competitor_id)] * 1_000,
            placing,
            0 if placing == 1 else 1_000,
            OfficialResult(ResultStatus.COMPLETION, 40_000 + placing * 1_000, None, 1, None),
            canonical_digest({"competitor_id": str(assignment.competitor_id), "placing": placing}),
        )
        for placing, assignment in enumerate(heat.ordered_assignments, start=1)
    )
    component_latency = {name: 0 for name in MEASURED_COMPONENTS}
    total_started = perf_counter_ns()
    started = perf_counter_ns()
    settled = SettlementService(lifecycle).record_and_settle(
        SettlementCommand(
            str(heat.field_id),
            heat.field_revision,
            str(heat_receipt.receipt.receipt_id),
            f"command:r158-settle-{trial_ordinal}",
            "actor:manager",
            "2026-08-25T18:00:00.500Z",
            20,
        ),
        submissions,
    )
    component_latency["final_heat_settlement"] = _elapsed_ms(started)
    events = SQLiteEventStore(database_path)
    settlement_events = tuple(
        events.event_at(sequence)
        for sequence in range(settled.first_global_sequence, settled.last_global_sequence + 1)
    )
    settlement_event_set_digest = _event_set_digest(settlement_events)

    started = perf_counter_ns()
    _append_lifecycle_event(
        lifecycle,
        command_kind=CommandKind.BEGIN_ROUND_CLOSING,
        event_kind=EventKind.ROUND_CLOSING_STARTED,
        aggregate_kind=AggregateKind.ROUND,
        target=heat.round_id,
        payload={"closing": True},
        command_id=f"command:r158-begin-close-{trial_ordinal}",
        occurred_at="2026-08-25T18:00:00.800Z",
    )
    with open_v3_connection(database_path, read_only=True) as connection:
        sources = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source_global_sequence FROM v3_derivation_reactions "
                "WHERE state='pending' ORDER BY source_global_sequence"
            )
        )
    for source in sources:
        for ordinal, reaction in enumerate(MandatoryReaction, start=1):
            lifecycle.complete_derivation_reaction(
                source,
                reaction,
                canonical_digest(
                    {
                        "source": source,
                        "reaction": reaction.value,
                        "settlement_event_set_digest": settlement_event_set_digest,
                    }
                ),
                command_id=IdempotencyKey(
                    f"command:r158-reaction-{trial_ordinal}-{source}-{reaction.value}"
                ),
                actor_id=StableIdentifier("actor:manager"),
                occurred_at_utc="2026-08-25T18:00:02.500Z",
                monotonic_elapsed_ms=30 + ordinal,
            )
    closure_id, close_result = lifecycle.close_evidence_round(
        heat.round_id,
        command_id=IdempotencyKey(f"command:r158-close-{trial_ordinal}"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-25T18:00:03.000Z",
        monotonic_elapsed_ms=40,
    )
    epoch, epoch_result = lifecycle.freeze_round_epoch(
        future_round,
        epoch_revision=1,
        historical_cutoff_key=str(heat.historical_cutoff_key),
        closure_ids=(closure_id,),
        command_id=IdempotencyKey(f"command:r158-freeze-successor-{trial_ordinal}"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-25T18:00:03.100Z",
        monotonic_elapsed_ms=41,
    )
    component_latency["deliberate_round_close"] = _elapsed_ms(started)

    started = perf_counter_ns()
    epoch_events = (events.event_at(epoch_result.first_global_sequence),)
    plan = resolver.resolve(epoch_events)
    candidates = tuple(plan.candidates)
    if len(candidates) != 2:
        raise RuntimeError("successor epoch did not isolate the two newly affected cards")
    coordinator.schedule(
        candidates,
        capacity_use=capacity_use,
        council_manifest_digest=council.body_digest,
        observed_at="2026-08-25T18:00:03.150Z",
    )
    publications = _finish_affected_cards(
        repository, coordinator, candidates, council, store._signer
    )

    credibility = SQLiteCredibilityReactionService(
        database_path,
        trust_store=store._trust_store,
        consequence_evaluator=None,
        policy_manifest=seal_credibility_policy(
            CredibilityPolicy(),
            optimizer_bundle_digest="e" * 64,
            signer=store._signer,
            created_at="2026-08-25T18:00:03.200Z",
        ),
    )
    frozen = credibility.freeze_live_weights(
        heat.round_id,
        future_round,
        context=baseline.weight_authority.context,
        command_id=IdempotencyKey(f"command:r158-live-freeze-{trial_ordinal}"),
        actor_id=StableIdentifier("actor:manager"),
        occurred_at_utc="2026-08-25T18:00:03.250Z",
        monotonic_elapsed_ms=42,
    )
    with open_v3_connection(database_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? "
            "AND event_kind=? ORDER BY global_sequence DESC LIMIT 1",
            (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
        ).fetchone()
    if row is None:
        raise RuntimeError("live successor weight freeze event is missing")
    freeze_event = EventEnvelope.from_dict(json.loads(str(row[0])))
    freeze_payload = freeze_event.command.payload.to_value()
    live_receipt = WeightReceipt(
        baseline.weight_authority.context,
        frozen.current_weights,
        frozen.baseline.components,
        frozen.baseline.calibration_cutoff_at_utc,
        frozen.baseline.policy_digest,
        live_effective_weight_receipt_digest(
            freeze_event.event_digest,
            baseline.weight_authority.context,
            frozen.current_weights,
        ),
    )
    live_binding = WeightAuthorityBinding.pending(
        live_receipt,
        ledger_projection_digest=freeze_payload["ledger_projection_digest"],
        tournament_event_sequence=epoch.maximum_tournament_sequence,
        source_global_sequence=freeze_event.global_sequence,
    )
    operational_authority = OperationalWeightAuthority.create(
        kind=OperationalWeightKind.LIVE_ROUND_FREEZE,
        binding=live_binding,
        tournament_id=heat.tournament_id,
        round_id=future_round,
        epoch_id=epoch.epoch_id,
        epoch_digest=epoch.content_digest,
        frozen_tournament_sequence=epoch.maximum_tournament_sequence,
        authority_event_sequence=freeze_event.global_sequence,
        authority_event_digest=freeze_event.event_digest,
        completed_round_id=heat.round_id,
        round_close_event_digest=freeze_payload["round_close_event_digest"],
        baseline_receipt_digest=freeze_payload["baseline_receipt_digest"],
        control_event_sequence=freeze_event.global_sequence,
        control_event_digest=freeze_event.event_digest,
    )
    store.install_weight_authority(operational_authority, installed_at="2026-08-25T18:00:03.100Z")
    component_latency["newly_affected_cards"] = _elapsed_ms(started)
    final_field = _future_field(heat, candidates, epoch=epoch)
    cards = tuple(RollingCurrentCard.from_publication(item) for item in publications)
    capabilities = []
    for index, candidate in enumerate(candidates):
        value = _capability(candidate.key.competitor_id, 40_000 + index * 1_000).to_dict()
        value["context_digest"] = final_field.target_context.digest
        value["state_digest"] = canonical_digest(
            {key: item for key, item in value.items() if key != "state_digest"}
        )
        state = CapabilityState.from_dict(value)
        capabilities.append(
            RollingCapabilityAuthority(
                state,
                RollingCapabilityBinding.create(
                    competitor_id=state.competitor_id,
                    context_digest=state.context_digest,
                    state_revision=state.state_revision,
                    state_digest=state.state_digest,
                    aggregate_version=state.state_revision,
                    aggregate_event_digest=canonical_digest(
                        {
                            "competitor_id": str(state.competitor_id),
                            "state_digest": state.state_digest,
                        }
                    ),
                ),
            )
        )
    inputs = RollingFieldBuildInputs(
        cards,
        live_receipt,
        operational_authority,
        baseline.dependence_artifact,
        tuple(capabilities),
        baseline.disagreement.decision.policy,
    )

    class CurrentSource:
        def load_current(self, field: FrozenFieldRevision) -> RollingFieldBuildInputs:
            if field != final_field:
                raise RuntimeError("benchmark loaded a different frozen field")
            return inputs

        def verify_current(self, field, card_bindings, capability_bindings) -> None:
            if (
                field != final_field
                or card_bindings != tuple(item.publication for item in cards)
                or capability_bindings != tuple(item.binding for item in capabilities)
            ):
                raise RuntimeError("benchmark current authority changed during assembly")

    started = perf_counter_ns()
    rolling_build = RollingFieldPipelineBuilder(
        CurrentSource(),
        signer=store._signer,
        trust_store=store._trust_store,
        clock=lambda: "2026-08-25T18:00:03.500Z",
    )(final_field)
    component_latency["gate_optimizer"] = _elapsed_ms(started)
    started = perf_counter_ns()
    assembled = FieldAssemblyService(store).assemble(
        field=final_field,
        caller_namespace="manager",
        request_identity=f"idempotency:r158-final-{trial_ordinal}",
        actor_id="actor:manager",
        occurred_at="2026-08-25T18:00:04.000Z",
        build_pipeline=lambda _field: rolling_build.pipeline,
    )
    component_latency["receipt_commit"] = _elapsed_ms(started)
    started = perf_counter_ns()
    approval = store.approval_page(tournament_id=str(final_field.tournament_id), offset=0, limit=10)
    component_latency["approval_projection"] = _elapsed_ms(started)
    measured_ms = _elapsed_ms(total_started)
    row = next(item for item in approval.rows if item.field_id == str(final_field.field_id))
    round_close_event = events.event_at(close_result.last_global_sequence)
    return {
        "trial_ordinal": trial_ordinal,
        "measured_result_to_ready_ms": measured_ms,
        "component_latency_ms": component_latency,
        "settlement_result_digest": settled.result_digest,
        "settlement_event_set_digest": settlement_event_set_digest,
        "round_closure_id": str(closure_id),
        "round_close_event_set_digest": close_result.event_set_digest,
        "round_close_event_digest": round_close_event.event_digest,
        "successor_epoch_digest": epoch.content_digest,
        "successor_epoch_event_set_digest": epoch_result.event_set_digest,
        "newly_affected_card_count": len(publications),
        "newly_affected_card_publication_digests": [
            item.publication_digest for item in publications
        ],
        "optimizer_receipt_digest": rolling_build.pipeline.optimizer.receipt.receipt_digest,
        "optimizer_verification_digest": rolling_build.pipeline.optimizer.verification_digest,
        "field_receipt_id": str(assembled.receipt.receipt_id),
        "field_receipt_digest": assembled.receipt.content_digest,
        "approval_snapshot_id": approval.snapshot_id,
        "approval_row_digest": row.row_digest,
    }


def run_benchmark(work_root: Path, *, repetitions: int = FORMAL_REPETITIONS) -> dict[str, Any]:
    if sys.platform != "win32":
        raise RuntimeError("result-to-ready authority requires the designated Windows machine")
    if not 1 <= repetitions <= FORMAL_REPETITIONS:
        raise ValueError("repetitions must be between 1 and 5")
    root = work_root.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=False)
    trials = []
    try:
        for ordinal in range(1, repetitions + 1):
            trials.append(_run_trial(root / f"trial-{ordinal:02d}.sqlite3", ordinal))
    finally:
        if root.exists():
            shutil.rmtree(root)
    maximum = max(item["measured_result_to_ready_ms"] for item in trials)
    source_bindings = _source_bindings()
    gates = {
        "formal_repetition_count": repetitions == FORMAL_REPETITIONS,
        "result_to_ready_within_budget": maximum <= RESULT_TO_READY_BUDGET_MS,
        "all_trials_completed": len(trials) == repetitions,
        "exact_source_bindings": bool(source_bindings),
    }
    status_gates = {key: value for key, value in gates.items() if key != "formal_repetition_count"}
    body = {
        "schema_version": "strathmark-v3-result-to-ready-benchmark-v1",
        "status": "passed" if all(status_gates.values()) else "failed",
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "repetitions": repetitions,
        "limits": {"result_to_ready_ms_inclusive": RESULT_TO_READY_BUDGET_MS},
        "gates": gates,
        "maximum_measured_result_to_ready_ms": maximum,
        "source_bindings": source_bindings,
        "source_bindings_digest": canonical_digest(source_bindings),
        "trials": trials,
    }
    return {**body, "manifest_digest": canonical_digest(body)}


def verify_benchmark_manifest(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or value.get("schema_version") != (
        "strathmark-v3-result-to-ready-benchmark-v1"
    ):
        raise ValueError("result-to-ready benchmark schema differs")
    body = dict(value)
    digest = body.pop("manifest_digest", None)
    if digest != canonical_digest(body):
        raise ValueError("result-to-ready benchmark digest differs")
    if value.get("source_bindings_digest") != canonical_digest(value.get("source_bindings")):
        raise ValueError("result-to-ready source digest differs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-root", type=Path, default=ROOT / ".tmp" / "result-to-ready-work")
    parser.add_argument("--repetitions", type=int, default=FORMAL_REPETITIONS)
    arguments = parser.parse_args(argv)
    manifest = run_benchmark(arguments.work_root, repetitions=arguments.repetitions)
    encoded = json.dumps(manifest, indent=2) + "\n"
    if arguments.output is None:
        sys.stdout.write(encoded)
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8", newline="\n")
        print(arguments.output.resolve())
    return 0 if manifest["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
