from __future__ import annotations

import ctypes
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import ROUND_DOWN, ROUND_UP, localcontext
from hashlib import sha256
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from types import SimpleNamespace
from typing import Callable

import pytest

from strathmark.v3.application.approval import FreshnessState
from strathmark.v3.application.capacity import CapacityManifest
from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.application.coordinator import CardKey
from strathmark.v3.application.credibility_reactions import (
    SQLiteCredibilityReactionService,
    seal_credibility_policy,
)
from strathmark.v3.application.field_assembly import (
    AssemblyConflict,
    AssemblyError,
    CapabilityPoolBasis,
    CompetitorCardAuthority,
    CompetitorPoolEvidence,
    CompetitorPredictionEvidence,
    FieldAssemblyService,
    FrozenEntrantAssignment,
    FrozenFieldRevision,
    JudgeReceiptExplanation,
    ManualCompetitorEstimate,
    ManualConstructionMode,
    ManualConstructionSubmission,
    ManualExpectedTimeBasis,
    ManualFieldAuthority,
    OperationalDisagreementReceipt,
    OperationalExpectedTimeOverrideAuthority,
    OperationalWeightAuthority,
    OperationalWeightKind,
    RollingCapabilityBinding,
    RollingPublicationBinding,
    SealedPipelineOutput,
    counterfactual_sheet_from_optimizer,
    live_effective_weight_receipt_digest,
    render_verified_receipt_explanation,
    seal_competitor_card_authority,
    seal_council_field_audit_authority,
    seal_disagreement_policy_authority,
    seal_field_capacity_authority,
)
from strathmark.v3.application.lifecycle import (
    LifecycleService,
    SnapshotKind,
    UpstreamSnapshot,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import (
    BlobReferenceV2,
    CommandEnvelope,
    CommandKind,
    InlinePayload,
)
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import (
    AggregateKind,
    CompetitionEngineSelection,
    EventEnvelope,
    EventKind,
)
from strathmark.v3.contracts.evidence import EvidencePacket, TargetContext
from strathmark.v3.contracts.forecasts import (
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.receipts import (
    FieldReceipt,
    MarkAssignment,
    ReceiptSectionKind,
)
from strathmark.v3.domain.capability import (
    CapabilityEvidence,
    CapabilityPrior,
    replay_capability,
)
from strathmark.v3.domain.credibility import (
    ContextNode,
    CredibilityPolicy,
    WeightReceipt,
)
from strathmark.v3.domain.disagreement import (
    CouncilAudit,
    CouncilMemberAudit,
    CouncilMemberStatus,
    DisagreementPolicy,
    ExpectedTimeOverrideRequest,
    FieldSheetSnapshot,
    OptimizerVerificationStatus,
    OverrideRecomputationProof,
    OverrideScope,
    classify_disagreement,
    create_override_receipt,
)
from strathmark.v3.domain.evidence import AdmissionReason, EvidenceSource
from strathmark.v3.domain.joint_dependence import (
    DependenceArtifact,
    DependencePolicy,
    FieldCompetitorForecast,
    JointDraws,
    bind_field_dependence,
    generate_aligned_component_joint_draws,
    generate_joint_draws,
    generate_joint_draws_from_pool_results,
    generate_joint_uniforms,
    train_dependence_artifact,
)
from strathmark.v3.domain.optimizer import (
    OptimizationField,
    VerifiedOptimizerReceipt,
    optimize_and_verify_field,
)
from strathmark.v3.domain.pooling import WeightAuthorityBinding, pool_forecasts
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    SignedManifest,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
from strathmark.v3.infrastructure.sqlite.projections import (
    ProjectionConflict,
    ProjectionError,
    SQLiteFieldProjectionStore,
)
from tests.v3.integration.test_derivation_barrier import _bootstrap_empty_closure

NOW = "2026-08-24T18:00:00.000Z"
ACTOR = StableIdentifier("actor:manager")


def _control_preview(service, signer, tournament, action):
    return service.seal_current_live_control_preview(
        tournament_id=tournament,
        action=action,
        signer=signer,
        created_at=NOW,
    )


def _context() -> TargetContext:
    return TargetContext(
        event_code="underhand",
        size_mm=300,
        material_code="pine",
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
    )


def _field(
    revision: int = 1,
    *,
    reverse_transport: bool = False,
    evidence_digest: str,
    tournament_event_sequence: int,
    tournament_epoch_id: StableIdentifier,
    call_order: int | None = None,
    scheduled_at: str = "2026-08-24T18:00:00.000Z",
    deadline_at: str = "2026-08-24T18:02:00.000Z",
    capacity_authority_digest: str | None = None,
    max_field_entrants: int | None = None,
    assignments: tuple[FrozenEntrantAssignment, ...] | None = None,
) -> FrozenFieldRevision:
    rows = assignments or (
        FrozenEntrantAssignment.create("competitor:a", "stand:right", 1),
        FrozenEntrantAssignment.create("competitor:b", "stand:left", 0),
    )
    return FrozenFieldRevision.create(
        tournament_id="tournament:show",
        round_id="round:final",
        field_id="field:final",
        field_revision=revision,
        assignments=tuple(reversed(rows)) if reverse_transport else rows,
        target_context=_context(),
        historical_cutoff_key="history:cutoff",
        tournament_epoch_id=tournament_epoch_id,
        tournament_event_sequence=tournament_event_sequence,
        bundle_digest=canonical_digest({"bundle_id": "bundle:verified"}),
        evidence_digest=evidence_digest,
        capacity_authority_digest=(
            _capacity_authority_digest()
            if capacity_authority_digest is None
            else capacity_authority_digest
        ),
        max_field_entrants=(
            _capacity_manifest().max_field_entrants
            if max_field_entrants is None
            else max_field_entrants
        ),
        call_order=revision if call_order is None else call_order,
        scheduled_at=scheduled_at,
        deadline_at=deadline_at,
    )


def _capacity_manifest() -> CapacityManifest:
    return CapacityManifest.load("benchmarks/v3/job_capacity_manifest.json")


def _capacity_authority_digest(
    capacity: CapacityManifest | None = None,
) -> str:
    capacity = _capacity_manifest() if capacity is None else capacity
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-field-capacity-authority-v1",
            "purpose": "field_assembly_capacity",
            "bundle_digest": canonical_digest({"bundle_id": "bundle:verified"}),
            "capacity_manifest": capacity.to_dict(),
            "capacity_manifest_digest": capacity.digest,
        }
    )


def _distribution(median: int) -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        (
            QuantilePoint("0.1", median - 2_000),
            QuantilePoint("0.5", median),
            QuantilePoint("0.9", median + 2_000),
        )
    )


def _capability(competitor: StableIdentifier, median: int):
    evidence = CapabilityEvidence(
        result_key=StableIdentifier(f"result:{str(competitor).split(':')[1]}-cap"),
        result_revision=1,
        supersedes_revision=None,
        competitor_id=competitor,
        context_digest="c" * 64,
        source_global_sequence=1,
        observed_at_utc="2026-01-01T00:00:00.000Z",
        raw_time_ms=median,
        source=EvidenceSource.LIVE_ISSUED_RACE,
        numeric_eligible=True,
        admission_reason=AdmissionReason.ELIGIBLE_COMPLETION,
        observation_digest=canonical_digest({"competitor": str(competitor)}),
        authority_digest="d" * 64,
        prior=CapabilityPrior.from_median_seconds(str(median / 1000), calibrated_beta="0.12"),
        evidence_log_variance="0.0025",
        conversion_log_variance="0",
        effective_weight="1",
        historical_binding=None,
    )
    state = replay_capability((evidence,))
    assert state is not None
    return state


def _weight_receipt() -> WeightReceipt:
    outer = (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)
    weights = tuple(
        zip(
            outer,
            ("0.2", "0.3", "0.5"),
        )
    )
    return WeightReceipt(
        ContextNode("underhand", "300", "pine", "mature"),
        weights,
        (),
        "2026-08-24T17:59:00.000Z",
        "1" * 64,
        canonical_digest([(item.value, value) for item, value in weights]),
    )


def _test_rolling_publication_binding(
    field: FrozenFieldRevision,
    card: CompetitorCardAuthority,
    *,
    dependency_revision: int,
    signer: P256EphemeralSigner,
) -> RollingPublicationBinding:
    return _test_rolling_publication_material(
        field,
        card,
        dependency_revision=dependency_revision,
        signer=signer,
    )[0]


def _test_rolling_publication_material(
    field: FrozenFieldRevision,
    card: CompetitorCardAuthority,
    *,
    dependency_revision: int,
    signer: P256EphemeralSigner,
    components: tuple[object, ...] | None = None,
) -> tuple[RollingPublicationBinding, SignedManifest, SignedManifest]:
    key = CardKey.create(
        competitor_id=card.competitor_id,
        target_context_digest=field.target_context.digest,
        historical_cutoff_key=field.historical_cutoff_key,
        tournament_epoch_id=field.tournament_epoch_id,
        bundle_digest=field.bundle_digest,
        evidence_digest=card.packet_digest,
        dependency_revision=dependency_revision,
    )
    member_receipts = [
        {
            "member_id": member_id,
            "member_manifest_digest": str(index) * 64,
            "job_id": f"job:{str(card.competitor_id).split(':')[1]}-{member_id}",
            "job_revision": 1,
            "fencing_token": 1,
            "outcome": "succeeded",
            "result_digest": str(index + 3) * 64,
            "terminal_reason_code": None,
        }
        for index, member_id in enumerate(
            ("local_qwen35_9b", "local_ministral3_8b", "frontier_cloud"),
            start=1,
        )
    ]
    council_digest = "8" * 64
    aggregate = sign_manifest(
        "rolling_council_aggregate_authority",
        {
            "schema_version": "strathmark-v3-rolling-council-aggregate-v1",
            "purpose": "rolling_card_council_aggregate",
            "card_digest": key.card_digest,
            "council_manifest_digest": council_digest,
            "member_receipts": member_receipts,
            "valid_member_count": 3,
            "aggregate_available": True,
            "aggregate_forecast_commit_digest": card.forecasts[2].commit_digest,
        },
        signer=signer,
        created_at=NOW,
    )
    component_rows = (
        member_receipts if components is None else [item.to_dict() for item in components]
    )
    content = {
        "schema_version": "strathmark-v3-rolling-card-publication-v1",
        "card_key": key.to_dict(),
        "card_authority_digest": canonical_digest(card.to_dict()),
        "component_refs_digest": canonical_digest(component_rows),
        "availability": [
            ["formula", "available"],
            ["ml", "available"],
            ["llm_council", "normal_3_of_3"],
        ],
        "council_manifest_digest": council_digest,
        "council_aggregate_manifest_digest": aggregate.body_digest,
        "hard_deadline_at": field.deadline_at,
        "sealed_at": NOW,
    }
    publication_digest = canonical_digest(content)
    manifest = sign_manifest(
        "rolling_card_publication",
        {**content, "publication_digest": publication_digest},
        signer=signer,
        created_at=NOW,
    )
    binding = RollingPublicationBinding.create(
        card_key=key.to_dict(),
        card_manifest_digest=card.manifest.body_digest,
        publication_digest=publication_digest,
        publication_manifest_digest=manifest.body_digest,
        component_refs_digest=content["component_refs_digest"],
        availability=tuple(tuple(item) for item in content["availability"]),
        council_manifest_digest=council_digest,
        council_aggregate_manifest_digest=aggregate.body_digest,
        hard_deadline_at=field.deadline_at,
        sealed_at=NOW,
    )
    return binding, manifest, aggregate


def _pipeline(
    field: FrozenFieldRevision,
    *,
    receipt: WeightReceipt,
    authority: WeightAuthorityBinding,
    operational_authority: OperationalWeightAuthority,
    artifact: DependenceArtifact,
    card_signer: P256EphemeralSigner,
    authority_signer: P256EphemeralSigner | None = None,
    available_assessors: tuple[AssessorKind, ...] | None = None,
    available_assessors_by_competitor: (dict[str, tuple[AssessorKind, ...]] | None) = None,
    council_statuses: tuple[CouncilMemberStatus, ...] | None = None,
    manual_medians: dict[str, int] | None = None,
    component_seed_offset: int = 0,
) -> SealedPipelineOutput:
    authority_signer = card_signer if authority_signer is None else authority_signer
    outer = (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)
    available_assessors = outer if available_assessors is None else available_assessors
    council_statuses = council_statuses or (
        CouncilMemberStatus.VALID,
        CouncilMemberStatus.VALID,
        CouncilMemberStatus.VALID,
    )
    context_node = receipt.context
    model = bind_field_dependence(artifact, context_node, field_id=field.field_id)
    crn_source_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-field-crn-source-v1",
            "field_revision_digest": field.revision_digest,
            "dependence_artifact_digest": artifact.artifact_digest,
            "weight_authority_digest": authority.binding_digest,
        }
    )
    seed = int(crn_source_digest[:16], 16) & ((1 << 63) - 1)
    slot_basis = tuple(
        FieldCompetitorForecast(
            assignment.competitor_id,
            str(assignment.stand_id),
            _distribution(40_000),
            assignment.crn_index,
        )
        for assignment in field.ordered_assignments
    )
    uniform_plan = generate_joint_uniforms(
        slot_basis,
        model,
        installed_artifact=artifact,
        seed=seed,
        draw_count=4096,
    )
    uniforms_by_slot = dict(uniform_plan.uniforms)
    pools = []
    for index, assignment in enumerate(field.ordered_assignments):
        competitor_available = (
            available_assessors
            if available_assessors_by_competitor is None
            else available_assessors_by_competitor[str(assignment.competitor_id)]
        )
        packet = EvidencePacket.create(
            competitor_id=assignment.competitor_id,
            target_context=field.target_context,
            observations=(),
            taxonomy_version=field.target_context.taxonomy_version,
            conversion_version=field.target_context.conversion_version,
            historical_cutoff_key=str(field.historical_cutoff_key),
            tournament_epoch_id=field.tournament_epoch_id,
            tournament_event_sequence=field.tournament_event_sequence,
        )
        forecast_rows = []
        for kind, offset in zip(outer, (-500, 0, 500), strict=True):
            committed = kind in competitor_available
            forecast_rows.append(
                AssessorForecast.create(
                    forecast_id=StableIdentifier(
                        f"forecast:{str(assignment.competitor_id).split(':')[1]}-{kind.value}"
                    ),
                    assessor=kind,
                    state=(ForecastState.COMMITTED if committed else ForecastState.ABSTAINED),
                    evidence_digest=packet.content_digest,
                    distribution=(
                        _distribution(40_000 + index * 10_000 + offset) if committed else None
                    ),
                    support=EvidenceSupport(4, "4", 4, "history:cutoff", 21),
                    warnings=(),
                    artifacts=(),
                    abstention_code=(None if committed else "insufficient_support"),
                )
            )
        forecasts = tuple(forecast_rows)
        pooled = pool_forecasts(
            forecasts,
            receipt,
            _capability(assignment.competitor_id, 40_000 + index * 10_000),
            uniform_plan.sampling_spec(str(assignment.stand_id)),
            weight_authority=authority,
            accept_single_survivor=len(competitor_available) == 1,
        )
        card = seal_competitor_card_authority(
            packet,
            forecasts,
            bundle_digest=field.bundle_digest,
            signer=card_signer,
            created_at=NOW,
        )
        pools.append(CompetitorPoolEvidence(card, pooled))
    rolling_publications = tuple(
        _test_rolling_publication_binding(
            field,
            item.card,
            dependency_revision=max(1, field.tournament_event_sequence),
            signer=authority_signer,
        )
        for item in pools
    )
    capability_bindings = tuple(
        RollingCapabilityBinding.create(
            competitor_id=item.competitor_id,
            context_digest=field.target_context.digest,
            state_revision=max(1, field.tournament_event_sequence),
            state_digest=item.pool.receipt.capability_state_digest,
            aggregate_version=max(1, field.tournament_event_sequence),
            aggregate_event_digest=canonical_digest(
                {
                    "competitor_id": str(item.competitor_id),
                    "state_digest": item.pool.receipt.capability_state_digest,
                }
            ),
        )
        for item in pools
    )
    pool_receipt_digests = [item.pool.receipt.receipt_digest for item in pools]
    manual_authority = None
    valid_sets = tuple(
        tuple(
            component.assessor
            for component in item.pool.receipt.components
            if component.availability.value == "valid"
        )
        for item in pools
    )
    exact_single = (
        all(len(items) == 1 for items in valid_sets)
        and len({items[0] for items in valid_sets}) == 1
    )
    manual_required = min(map(len, valid_sets)) < 2 or len(set(valid_sets)) != 1
    if manual_required:
        manual_mode = (
            ManualConstructionMode.EXACT_SINGLE_SURVIVOR
            if exact_single
            else ManualConstructionMode.COMPLETE_EXPECTED_TIME
        )
        estimates = tuple(
            ManualCompetitorEstimate(
                item.competitor_id,
                (
                    item.pool.distribution
                    if manual_mode is ManualConstructionMode.EXACT_SINGLE_SURVIVOR
                    else _distribution(
                        45_000 + index * 10_000
                        if manual_medians is None
                        else manual_medians[str(item.competitor_id)]
                    )
                ),
                (
                    valid_sets[index][0]
                    if manual_mode is ManualConstructionMode.EXACT_SINGLE_SURVIVOR
                    else None
                ),
            )
            for index, item in enumerate(pools)
        )
        manual_authority = ManualFieldAuthority.create(
            mode=manual_mode,
            field_revision_digest=field.revision_digest,
            estimates=estimates,
            actor_id=ACTOR,
            reason_code=(
                "judge_single_survivor_acceptance"
                if manual_mode is ManualConstructionMode.EXACT_SINGLE_SURVIVOR
                else "judge_complete_expected_time_construction"
            ),
            scope=OverrideScope.UPCOMING_RACE,
            created_at=NOW,
        )
        basis = {item.competitor_id: item.distribution for item in estimates}
    else:
        basis = {item.competitor_id: item.pool.distribution for item in pools}
    forecasts = tuple(
        FieldCompetitorForecast(
            assignment.competitor_id,
            str(assignment.stand_id),
            basis[assignment.competitor_id],
            assignment.crn_index,
        )
        for assignment, pool in zip(field.ordered_assignments, pools, strict=True)
    )
    manual_authority_digest = (
        None if manual_authority is None else manual_authority.authority_digest
    )
    prediction_basis_digests = (
        pool_receipt_digests
        if manual_authority is None
        or manual_authority.mode is ManualConstructionMode.EXACT_SINGLE_SURVIVOR
        else [
            canonical_digest(
                {
                    "basis_kind": "manual_expected_time",
                    "competitor_id": str(item.competitor_id),
                    "distribution": item.distribution.to_dict(),
                    "manual_authority_digest": manual_authority.authority_digest,
                    "source_assessor": (
                        None if item.source_assessor is None else item.source_assessor.value
                    ),
                }
            )
            for competitor_id in (
                assignment.competitor_id for assignment in field.ordered_assignments
            )
            for item in manual_authority.estimates
            if item.competitor_id == competitor_id
        ]
    )
    source_digest = canonical_digest(
        {
            "field": field.revision_digest,
            "prediction_basis_digests": prediction_basis_digests,
            "manual_authority_digest": manual_authority_digest,
        }
    )
    joint = (
        generate_joint_draws_from_pool_results(
            forecasts,
            tuple(item.pool for item in pools),
            model,
            installed_artifact=artifact,
            seed=seed,
            draw_count=4096,
            uniform_plan=uniform_plan,
        )
        if manual_authority is None
        else generate_joint_draws(
            forecasts,
            model,
            installed_artifact=artifact,
            seed=seed,
            draw_count=4096,
            uniform_plan=uniform_plan,
        )
    )
    pool_digest = canonical_digest(
        {
            "prediction_basis_digests": prediction_basis_digests,
            "manual_authority_digest": manual_authority_digest,
        }
    )
    optimizer_field = OptimizationField.from_joint_draws(
        joint,
        forecasts=forecasts,
        source_receipt_digest=source_digest,
        pool_receipt_digest=pool_digest,
    )
    verified = optimize_and_verify_field(optimizer_field, ceiling=183)
    prediction_evidence = []
    for pool, publication, capability in zip(
        pools, rolling_publications, capability_bindings, strict=True
    ):
        if manual_authority is None or (
            manual_authority.mode is ManualConstructionMode.EXACT_SINGLE_SURVIVOR
        ):
            prediction_basis = CapabilityPoolBasis(pool.pool, capability)
        else:
            estimate = next(
                item
                for item in manual_authority.estimates
                if item.competitor_id == pool.competitor_id
            )
            manual_content = {
                "basis_kind": "manual_expected_time",
                "competitor_id": str(pool.competitor_id),
                "distribution": estimate.distribution.to_dict(),
                "manual_authority_digest": manual_authority.authority_digest,
                "source_assessor": (
                    None if estimate.source_assessor is None else estimate.source_assessor.value
                ),
            }
            prediction_basis = ManualExpectedTimeBasis(
                pool.competitor_id,
                estimate.distribution,
                manual_authority.authority_digest,
                estimate.source_assessor,
                canonical_digest(manual_content),
            )
        prediction_evidence.append(
            CompetitorPredictionEvidence(pool.card, publication, prediction_basis)
        )
    sealed_prediction_evidence = tuple(prediction_evidence)
    if manual_authority is not None:
        return SealedPipelineOutput.create(
            field_revision_digest=field.revision_digest,
            prediction_evidence=sealed_prediction_evidence,
            joint_draws=joint,
            optimizer=verified,
            disagreement=None,
            weight_authority=authority,
            operational_weight_authority=operational_authority,
            dependence_artifact=artifact,
            manual_authority=manual_authority,
            total_latency_ms=15,
        )
    component_optimizers = []
    component_joint_draws = []
    component_sheets = []
    component_inputs = []
    card_by_id = {item.competitor_id: item.card for item in pools}
    slots = [
        [str(draw.competitor_id), draw.draw_slot, draw.crn_index] for draw in joint.competitors
    ]
    for source in outer:
        forecast_index = outer.index(source)
        component_commits = [
            card_by_id[item.competitor_id].forecasts[forecast_index].commit_digest
            for item in optimizer_field.competitors
        ]
        component_pool_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-component-card-set-v1",
                "source": source.value,
                "forecast_commit_digests": component_commits,
            }
        )
        component_source_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-component-counterfactual-source-v1",
                "field_revision_digest": field.revision_digest,
                "source": source.value,
                "card_pool_digest": component_pool_digest,
                "dependence_artifact_digest": artifact.artifact_digest,
                "crn_slots": slots,
            }
        )
        component_forecasts = tuple(
            FieldCompetitorForecast(
                assignment.competitor_id,
                str(assignment.stand_id),
                card_by_id[assignment.competitor_id].forecasts[forecast_index].distribution,
                assignment.crn_index,
            )
            for assignment in field.ordered_assignments
        )
        component_inputs.append(
            (
                source,
                component_forecasts,
                component_source_digest,
                component_pool_digest,
            )
        )
    generated_component_draws = (
        generate_aligned_component_joint_draws(
            tuple(item[1] for item in component_inputs),
            model,
            installed_artifact=artifact,
            seed=joint.inputs.seed,
            draw_count=4096,
            uniform_plan=uniform_plan,
        )
        if component_seed_offset == 0
        else tuple(
            generate_joint_draws(
                item[1],
                model,
                installed_artifact=artifact,
                seed=joint.inputs.seed + component_seed_offset,
                draw_count=4096,
            )
            for item in component_inputs
        )
    )
    for (
        source,
        component_forecasts,
        component_source_digest,
        component_pool_digest,
    ), component_joint in zip(component_inputs, generated_component_draws, strict=True):
        component_field = OptimizationField.from_joint_draws(
            component_joint,
            forecasts=component_forecasts,
            source_receipt_digest=component_source_digest,
            pool_receipt_digest=component_pool_digest,
        )
        component_verified = optimize_and_verify_field(component_field, ceiling=183)
        component_optimizers.append((source, component_verified))
        component_joint_draws.append((source, component_joint))
        component_sheets.append(counterfactual_sheet_from_optimizer(source, component_verified))
    pooled_sheet = counterfactual_sheet_from_optimizer("pooled", verified)
    council_audit = CouncilAudit.create(
        aggregate_sheet=component_sheets[-1],
        aggregate_forecast_digest=canonical_digest(component_commits),
        evidence_digest=canonical_digest([item.card.packet_digest for item in pools]),
        evidence_epoch_id=field.tournament_epoch_id,
        members=tuple(
            CouncilMemberAudit(
                StableIdentifier(f"llm_member:{name}"),
                status,
                canonical_digest({"member": name, "outcome": status.value}),
                canonical_digest({"member": name, "receipt": status.value}),
            )
            for name, status in zip(("cloud", "local_a", "local_b"), council_statuses, strict=True)
        ),
    )
    disagreement_policy = DisagreementPolicy(
        "disagreement:v1",
        2_000,
        20_000,
        5_000,
        20_000,
        1,
        2,
        "0.2",
        "0.49",
        10_000,
        50_000,
        "6" * 64,
        "7" * 64,
    )
    disagreement = classify_disagreement(
        pooled_sheet,
        tuple(component_sheets),
        council_audit,
        disagreement_policy,
        available_assessors=outer,
    )
    policy_manifest = seal_disagreement_policy_authority(
        disagreement_policy,
        bundle_digest=field.bundle_digest,
        signer=authority_signer,
        created_at=NOW,
    )
    council_manifest = seal_council_field_audit_authority(
        council_audit,
        field_revision_digest=field.revision_digest,
        card_manifest_digests=tuple(item.card.manifest.body_digest for item in pools),
        signer=authority_signer,
        created_at=NOW,
    )
    operational_disagreement = OperationalDisagreementReceipt.create(
        field_revision_digest=field.revision_digest,
        decision=disagreement,
        pooled_optimizer=verified,
        component_optimizers=tuple(component_optimizers),
        component_joint_draws=tuple(component_joint_draws),
        policy_manifest=policy_manifest,
        council_manifest=council_manifest,
    )
    return SealedPipelineOutput.create(
        field_revision_digest=field.revision_digest,
        prediction_evidence=sealed_prediction_evidence,
        joint_draws=joint,
        optimizer=verified,
        disagreement=operational_disagreement,
        weight_authority=authority,
        operational_weight_authority=operational_authority,
        dependence_artifact=artifact,
        manual_authority=None,
        total_latency_ms=15,
    )


def _pipeline_with_supersession(
    pipeline: SealedPipelineOutput,
    *,
    construction_submission: ManualConstructionSubmission | None = None,
    expected_time_override: OperationalExpectedTimeOverrideAuthority | None = None,
) -> SealedPipelineOutput:
    return SealedPipelineOutput.create(
        field_revision_digest=pipeline.field_revision_digest,
        prediction_evidence=pipeline.prediction_evidence,
        joint_draws=pipeline.joint_draws,
        optimizer=pipeline.optimizer,
        disagreement=pipeline.disagreement,
        weight_authority=pipeline.weight_authority,
        operational_weight_authority=pipeline.operational_weight_authority,
        dependence_artifact=pipeline.dependence_artifact,
        manual_authority=pipeline.manual_authority,
        construction_submission=construction_submission,
        expected_time_override=expected_time_override,
        total_latency_ms=pipeline.total_latency_ms,
    )


def _append_config(
    service: LifecycleService,
    command_kind: CommandKind,
    event_kind: EventKind,
    aggregate_kind: AggregateKind,
    target: StableIdentifier,
) -> None:
    command = CommandEnvelope(
        command_kind,
        IdempotencyKey(f"command:configure-{target.namespace}"),
        target,
        ((str(target), 0),),
        ACTOR,
        InlinePayload.from_value({"configured": True}),
    )
    SQLiteEventStore(service.projections.database_path).execute(
        CommandRequest(
            ACTOR,
            command,
            (EventIntent(aggregate_kind, target, event_kind),),
            "strathmark-v3-test-result-v1",
            {"configured": True},
            NOW,
            1,
        ),
        projection_hook=service.projections.apply_events,
    )


def _ingest_field(
    service: LifecycleService,
    revision: int,
    *,
    capacity: CapacityManifest | None = None,
    competitors: list[str] | None = None,
    stands: list[str] | None = None,
    engine_selection: CompetitionEngineSelection | None = None,
) -> None:
    capacity = _capacity_manifest() if capacity is None else capacity
    competitors = ["competitor:b", "competitor:a"] if competitors is None else competitors
    stands = ["stand:left", "stand:right"] if stands is None else stands
    service.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier("field:final"),
            revision,
            StableIdentifier("tournament:show"),
            StableIdentifier("round:final"),
            {
                "competitor_ids": competitors,
                "target_context": _context().to_dict(),
                "stand_ids": stands,
                "capacity_authority_digest": _capacity_authority_digest(capacity),
                "max_field_entrants": capacity.max_field_entrants,
                "call_order": revision,
                "scheduled_at": "2026-08-24T18:00:00.000Z",
                "deadline_at": "2026-08-24T18:02:00.000Z",
            },
            engine_selection,
        ),
        command_id=IdempotencyKey(f"command:field-revision-{revision}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=revision + 10,
    )


def _bootstrap(
    path: Path,
    *,
    available_assessors: tuple[AssessorKind, ...] | None = None,
    available_assessors_by_competitor: (dict[str, tuple[AssessorKind, ...]] | None) = None,
    council_statuses: tuple[CouncilMemberStatus, ...] | None = None,
    install_dependence: bool = True,
    trust_pipeline_cards: bool = True,
    capacity_manifest: CapacityManifest | None = None,
    manual_medians: dict[str, int] | None = None,
    competitor_count: int = 2,
    component_seed_offset: int = 0,
    engine_selection: CompetitionEngineSelection | None = None,
    persist_field_snapshot: bool = True,
) -> tuple[
    SQLiteFieldProjectionStore,
    FrozenFieldRevision,
    Callable[[FrozenFieldRevision], SealedPipelineOutput],
    LifecycleService,
]:
    lifecycle = LifecycleService(path)
    tournament = StableIdentifier("tournament:show")
    round_id = StableIdentifier("round:final")
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:cutoff"},
            engine_selection,
        ),
        command_id=IdempotencyKey("command:tournament-snapshot"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    lifecycle.ingest_snapshot(
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
            engine_selection,
        ),
        command_id=IdempotencyKey("command:round-snapshot"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
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
        round_id,
    )
    lifecycle.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:cutoff",
        root_round_ids=(round_id,),
        command_id=IdempotencyKey("command:open"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
        engine_selection=engine_selection,
    )
    signer = P256EphemeralSigner.generate("integrity-key:u15")
    trust_store = IntegrityTrustStore((signer.identity,))
    capacity_manifest = _capacity_manifest() if capacity_manifest is None else capacity_manifest
    if competitor_count == 2:
        competitor_ids = ["competitor:b", "competitor:a"]
        stand_ids = ["stand:left", "stand:right"]
    else:
        competitor_ids = [f"competitor:bench-{index:02d}" for index in range(competitor_count)]
        stand_ids = [f"stand:bench-{index:02d}" for index in range(competitor_count)]
    assignments = tuple(
        FrozenEntrantAssignment.create(competitor_id, stand_id, index)
        for index, (competitor_id, stand_id) in enumerate(
            zip(competitor_ids, stand_ids, strict=True)
        )
    )
    capacity_authority = seal_field_capacity_authority(
        capacity_manifest,
        bundle_digest=canonical_digest({"bundle_id": "bundle:verified"}),
        signer=signer,
        created_at=NOW,
    )
    credibility = SQLiteCredibilityReactionService(
        path,
        trust_store=trust_store,
        consequence_evaluator=None,
        policy_manifest=seal_credibility_policy(
            CredibilityPolicy(),
            optimizer_bundle_digest="e" * 64,
            signer=signer,
            created_at=NOW,
        ),
    )
    weight_receipt = credibility._tournament_baseline(
        tournament,
        ContextNode("underhand", "300_349", "pine"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    with open_v3_connection(path, read_only=True) as connection:
        row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? "
            "AND event_kind=? ORDER BY global_sequence DESC LIMIT 1",
            (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
        ).fetchone()
    assert row is not None
    weight_event = EventEnvelope.from_dict(json.loads(str(row[0])))
    weight_payload = weight_event.command.payload.to_value()
    epoch, _ = lifecycle.freeze_round_epoch(
        round_id,
        epoch_revision=1,
        historical_cutoff_key="history:cutoff",
        closure_ids=(),
        command_id=IdempotencyKey("command:freeze-after-weight"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    store = SQLiteFieldProjectionStore(path, signer=signer, trust_store=trust_store)
    store.install_capacity_authority(capacity_authority, installed_at=NOW)
    if persist_field_snapshot:
        _ingest_field(
            lifecycle,
            1,
            capacity=capacity_manifest,
            competitors=competitor_ids,
            stands=stand_ids,
            engine_selection=engine_selection,
        )
    field = _field(
        evidence_digest=epoch.content_digest,
        tournament_event_sequence=epoch.maximum_tournament_sequence,
        tournament_epoch_id=epoch.epoch_id,
        capacity_authority_digest=capacity_authority.authority_digest,
        max_field_entrants=capacity_manifest.max_field_entrants,
        assignments=assignments,
    )
    authority = WeightAuthorityBinding.pending(
        weight_receipt,
        ledger_projection_digest=weight_payload["baseline_ledger_projection_digest"],
        tournament_event_sequence=field.tournament_event_sequence,
        source_global_sequence=weight_event.global_sequence,
    )
    operational_authority = OperationalWeightAuthority.create(
        kind=OperationalWeightKind.ROOT_BASELINE,
        binding=authority,
        tournament_id=tournament,
        round_id=round_id,
        epoch_id=epoch.epoch_id,
        epoch_digest=epoch.content_digest,
        frozen_tournament_sequence=epoch.maximum_tournament_sequence,
        authority_event_sequence=weight_event.global_sequence,
        authority_event_digest=weight_event.event_digest,
        completed_round_id=None,
        round_close_event_digest=None,
        baseline_receipt_digest=weight_receipt.receipt_digest,
    )
    artifact = train_dependence_artifact(
        (),
        weight_receipt.context,
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
    store.install_weight_authority(operational_authority, installed_at=NOW)
    if install_dependence:
        store.install_dependence_authority(artifact, promotion_manifest=promotion, installed_at=NOW)
    pipeline_card_signer = (
        signer
        if trust_pipeline_cards
        else P256EphemeralSigner.generate("integrity-key:untrusted-card")
    )

    def build(revision: FrozenFieldRevision) -> SealedPipelineOutput:
        return _pipeline(
            revision,
            receipt=weight_receipt,
            authority=authority,
            operational_authority=operational_authority,
            artifact=artifact,
            card_signer=pipeline_card_signer,
            authority_signer=signer,
            available_assessors=available_assessors,
            available_assessors_by_competitor=available_assessors_by_competitor,
            council_statuses=council_statuses,
            manual_medians=manual_medians,
            component_seed_offset=component_seed_offset,
        )

    return store, field, build, lifecycle


def test_optimizer_failure_receipt_is_explicitly_degraded_and_preserves_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    monkeypatch.setattr(optimizer, "_evaluate_candidates", lambda *_args, **_kwargs: 1 / 0)
    store, field, build, _lifecycle = _bootstrap(tmp_path / "optimizer-failure.sqlite3")
    pipeline = build(field)

    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:optimizer-failure",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: pipeline,
    )

    optimizer_section = next(
        section.payload.to_value()
        for section in result.receipt.sections
        if section.kind is ReceiptSectionKind.OPTIMIZER_FRONTIER
    )
    assert (
        pipeline.optimizer.receipt.fallback_reason is optimizer.OptimizerFallback.OPTIMIZER_FAILURE
    )
    assert optimizer_section["receipt_digest"] == pipeline.optimizer.receipt.receipt_digest
    assert optimizer_section["verification_digest"] == pipeline.optimizer.verification_digest
    assert optimizer_section["fallback_reason"] == "optimizer_failure"
    assert optimizer_section["operational_state"] == "degraded_review"
    assert (
        tuple(optimizer_section["rounded_baseline"]) == pipeline.optimizer.receipt.rounded_baseline
    )
    assert tuple(item.mark for item in result.receipt.marks) == (
        pipeline.optimizer.receipt.rounded_baseline
    )
    assert "degraded_optimizer_failure" in result.receipt.warning_codes


@pytest.mark.parametrize(
    ("install_dependence", "trust_pipeline_cards", "expected_error"),
    (
        (False, True, "dependence artifact is not installed signed authority"),
        (True, False, "competitor card authority is untrusted"),
    ),
)
def test_direct_commit_atomically_rechecks_dependence_and_card_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    install_dependence: bool,
    trust_pipeline_cards: bool,
    expected_error: str,
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, build, _lifecycle = _bootstrap(
        path,
        install_dependence=install_dependence,
        trust_pipeline_cards=trust_pipeline_cards,
    )
    if not install_dependence:
        monkeypatch.setattr(store, "verify_dependence_authority", lambda _value: None)
    if not trust_pipeline_cards:
        monkeypatch.setattr(store, "verify_card_authority", lambda _value: None)

    with pytest.raises((ProjectionConflict, AssemblyConflict), match=expected_error):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:atomic-authority-recheck",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=build,
        )

    with open_v3_connection(path, read_only=True) as connection:
        canonical_events = connection.execute(
            "SELECT COUNT(*) FROM v3_events WHERE aggregate_id=? AND event_kind IN (?, ?)",
            (
                str(field.field_id),
                EventKind.FIELD_OPTIMIZED.value,
                EventKind.FIELD_REGENERATED.value,
            ),
        ).fetchone()[0]
        projected_receipts = connection.execute(
            "SELECT COUNT(*) FROM v3_field_receipts WHERE field_id=?",
            (str(field.field_id),),
        ).fetchone()[0]
    assert canonical_events == 0
    assert projected_receipts == 0


def test_direct_store_rejects_receipt_not_derived_from_sealed_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    captured: dict[str, object] = {}

    class ReceiptCaptured(Exception):
        pass

    def capture(**values: object) -> None:
        captured.update(values)
        raise ReceiptCaptured

    monkeypatch.setattr(store, "commit_receipt", capture)
    with pytest.raises(ReceiptCaptured):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:capture-sealed-receipt",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=build,
        )
    monkeypatch.undo()
    receipt = captured["receipt"]
    assert isinstance(receipt, FieldReceipt)
    forged = FieldReceipt.create(
        caller_namespace=receipt.caller_namespace,
        request_identity=IdempotencyKey("idempotency:forged-direct-store"),
        field_id=receipt.field_id,
        upstream_field_revision=receipt.upstream_field_revision,
        receipt_revision=receipt.receipt_revision,
        supersedes_receipt_id=receipt.supersedes_receipt_id,
        ordered_competitor_ids=receipt.ordered_competitor_ids,
        target_context=receipt.target_context,
        target_context_digest=receipt.target_context_digest,
        historical_cutoff_key=receipt.historical_cutoff_key,
        tournament_epoch_id=receipt.tournament_epoch_id,
        tournament_event_sequence=receipt.tournament_event_sequence,
        packet_identities=receipt.packet_identities,
        sections=receipt.sections,
        marks=tuple(
            MarkAssignment(item.competitor_id, item.mark + (1 if index == 0 else 0))
            for index, item in enumerate(receipt.marks)
        ),
        warning_codes=tuple(sorted((*receipt.warning_codes, "forged_warning"))),
        total_latency_ms=receipt.total_latency_ms,
        bundles=receipt.bundles,
    )
    direct_values = dict(captured)
    direct_values["receipt"] = forged

    with pytest.raises(AssemblyConflict, match="differs from sealed pipeline"):
        store.commit_receipt(**direct_values)

    with open_v3_connection(path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE aggregate_id=? AND event_kind IN (?, ?)",
                (
                    str(field.field_id),
                    EventKind.FIELD_OPTIMIZED.value,
                    EventKind.FIELD_REGENERATED.value,
                ),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_field_receipts WHERE field_id=?",
                (str(field.field_id),),
            ).fetchone()[0]
            == 0
        )


def test_exact_retry_recovers_bytes_before_pipeline_or_provider_loading(
    tmp_path: Path,
) -> None:
    store, field, pipeline_builder, _lifecycle = _bootstrap(tmp_path / "field.sqlite3")
    service = FieldAssemblyService(store)
    calls = 0

    def build(field: FrozenFieldRevision) -> SealedPipelineOutput:
        nonlocal calls
        calls += 1
        return pipeline_builder(field)

    first = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:assemble-final",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )

    def unavailable(_field: FrozenFieldRevision) -> SealedPipelineOutput:
        raise AssertionError("exact retry loaded an unavailable provider")

    restarted = FieldAssemblyService(store)
    second = restarted.assemble(
        field=_field(
            reverse_transport=True,
            evidence_digest=field.evidence_digest,
            tournament_event_sequence=field.tournament_event_sequence,
            tournament_epoch_id=field.tournament_epoch_id,
        ),
        caller_namespace="manager",
        request_identity="idempotency:assemble-final",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=unavailable,
    )
    assert calls == 1
    assert first.receipt == second.receipt
    assert first.canonical_bytes == second.canonical_bytes
    assert min(item.mark for item in second.receipt.marks) == 3
    assert second.crn_assignments == (
        ("competitor:b", "stand:left", 0),
        ("competitor:a", "stand:right", 1),
    )


def test_verified_receipt_explanation_is_bounded_deterministic_and_model_free(
    tmp_path: Path,
) -> None:
    store, field, build, _lifecycle = _bootstrap(tmp_path / "explanation.sqlite3")
    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:explanation",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )

    first = render_verified_receipt_explanation(result.receipt)
    second = render_verified_receipt_explanation(result.receipt)

    assert first == second
    assert len(first.text.encode("utf-8")) <= 4096
    assert first.reason_tokens == (
        "availability_3_of_3",
        "consequence_amber",
        "optimizer_replay_verified",
        "rebase_mark_3",
    )
    assert first.lines[0] == ("Field field:final receipt revision 1 uses upstream revision 1.")
    assert first.lines[1] == ("Start order: competitor:b Mark 13; competitor:a Mark 3.")
    assert "model narrative" not in first.text.lower()
    assert JudgeReceiptExplanation.from_dict(first.to_dict()) == first
    tampered = {**first.to_dict(), "lines": [*first.to_dict()["lines"], "invented"]}
    with pytest.raises(AssemblyError, match="digest"):
        JudgeReceiptExplanation.from_dict(tampered)


@pytest.mark.parametrize(
    ("column", "tampered", "lookup_namespace", "lookup_request", "lookup_revision"),
    (
        (
            "caller_namespace",
            "other-manager",
            "other-manager",
            "idempotency:local-proof",
            None,
        ),
        (
            "request_identity",
            "idempotency:substituted",
            "manager",
            "idempotency:substituted",
            None,
        ),
        (
            "field_revision_digest",
            "f" * 64,
            "manager",
            "idempotency:local-proof",
            "f" * 64,
        ),
        (
            "pipeline_digest",
            "e" * 64,
            "manager",
            "idempotency:local-proof",
            None,
        ),
        (
            "source_global_sequence",
            1,
            "manager",
            "idempotency:local-proof",
            None,
        ),
    ),
)
def test_exact_retry_local_proof_rejects_projection_column_substitution(
    tmp_path: Path,
    column: str,
    tampered: object,
    lookup_namespace: str,
    lookup_request: str,
    lookup_revision: str | None,
) -> None:
    path = tmp_path / f"local-proof-{column}.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:local-proof",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    assert (
        store.lookup_exact(
            caller_namespace="manager",
            request_identity="idempotency:local-proof",
            field_revision_digest=field.revision_digest,
        )
        is not None
    )
    with open_v3_connection(path) as connection:
        connection.execute(
            f"UPDATE v3_field_receipts SET {column}=? WHERE field_id=?",
            (tampered, str(field.field_id)),
        )

    with pytest.raises(ProjectionError, match="local authority"):
        store.lookup_exact(
            caller_namespace=lookup_namespace,
            request_identity=lookup_request,
            field_revision_digest=lookup_revision or field.revision_digest,
        )


def test_operational_disagreement_summary_requires_content_addressed_authority_resolver(
    tmp_path: Path,
) -> None:
    _store, field, build, _lifecycle = _bootstrap(tmp_path / "resolver.sqlite3")
    pipeline = build(field)
    authority = pipeline.disagreement
    assert authority is not None
    summary = authority.to_dict()

    resolved = OperationalDisagreementReceipt.from_dict(
        summary, authority_resolver=lambda digest: authority
    )

    assert resolved is authority
    assert all(
        draws.common_random_map_digest == pipeline.joint_draws.common_random_map_digest
        and tuple(item.common_uniforms for item in draws.competitors)
        == tuple(item.common_uniforms for item in pipeline.joint_draws.competitors)
        for _source, draws in authority.component_joint_draws
    )
    tampered = {**summary, "color": "red" if summary["color"] != "red" else "green"}
    with pytest.raises(AssemblyConflict, match="summary"):
        OperationalDisagreementReceipt.from_dict(
            tampered, authority_resolver=lambda digest: authority
        )
    with pytest.raises(AssemblyConflict, match="missing"):
        OperationalDisagreementReceipt.from_dict(summary, authority_resolver=lambda digest: None)


def test_component_counterfactuals_reject_distinct_common_random_streams(
    tmp_path: Path,
) -> None:
    _store, field, build, _lifecycle = _bootstrap(
        tmp_path / "distinct-crn.sqlite3", component_seed_offset=1
    )
    with pytest.raises(AssemblyError, match="exact signed cards and dependence"):
        build(field)


def test_component_counterfactuals_bind_original_signed_assessor_distributions(
    tmp_path: Path,
) -> None:
    """Capability-adjusted pool components cannot replace assessor authority."""

    _store, field, build, _lifecycle = _bootstrap(
        tmp_path / "original-assessor-counterfactual.sqlite3"
    )
    pipeline = build(field)
    assert pipeline.disagreement is not None
    source = AssessorKind.FORMULA
    source_index = (
        AssessorKind.FORMULA,
        AssessorKind.ML,
        AssessorKind.LLM_COUNCIL,
    ).index(source)
    original_draws = dict(pipeline.disagreement.component_joint_draws)[source]
    adjusted_forecasts = tuple(
        FieldCompetitorForecast(
            draw.competitor_id,
            draw.draw_slot,
            next(
                component.adjusted_distribution
                for component in pool.pool.receipt.components
                if component.assessor is source
            ),
            draw.crn_index,
        )
        for draw, pool in zip(original_draws.competitors, pipeline.pools, strict=True)
    )
    assert all(item.distribution is not None for item in adjusted_forecasts)
    assert any(
        item.distribution != pool.card.forecasts[source_index].distribution
        for item, pool in zip(adjusted_forecasts, pipeline.pools, strict=True)
    )
    model = bind_field_dependence(
        pipeline.dependence_artifact,
        pipeline.dependence_artifact.target_context,
        field_id=field.field_id,
    )
    adjusted_plan = generate_joint_uniforms(
        adjusted_forecasts,
        model,
        installed_artifact=pipeline.dependence_artifact,
        seed=original_draws.inputs.seed,
        draw_count=original_draws.inputs.draw_count,
    )
    adjusted_draws = generate_joint_draws(
        adjusted_forecasts,
        model,
        installed_artifact=pipeline.dependence_artifact,
        seed=original_draws.inputs.seed,
        draw_count=original_draws.inputs.draw_count,
        uniform_plan=adjusted_plan,
    )
    original_optimizer = dict(pipeline.disagreement.component_optimizers)[source]
    adjusted_field = OptimizationField.from_joint_draws(
        adjusted_draws,
        forecasts=adjusted_forecasts,
        source_receipt_digest=original_optimizer.field.source_receipt_digest,
        pool_receipt_digest=original_optimizer.field.pool_receipt_digest,
    )
    adjusted_optimizer = optimize_and_verify_field(adjusted_field, ceiling=183)
    optimizers = tuple(
        (kind, adjusted_optimizer if kind is source else receipt)
        for kind, receipt in pipeline.disagreement.component_optimizers
    )
    draws = tuple(
        (kind, adjusted_draws if kind is source else receipt)
        for kind, receipt in pipeline.disagreement.component_joint_draws
    )
    sheets = tuple(
        counterfactual_sheet_from_optimizer(kind, receipt) for kind, receipt in optimizers
    )
    decision = classify_disagreement(
        counterfactual_sheet_from_optimizer("pooled", pipeline.optimizer),
        sheets,
        pipeline.disagreement.decision.council_audit,
        pipeline.disagreement.decision.policy,
        available_assessors=tuple(kind for kind, _receipt in optimizers),
    )
    substituted = OperationalDisagreementReceipt.create(
        field_revision_digest=field.revision_digest,
        decision=decision,
        pooled_optimizer=pipeline.optimizer,
        component_optimizers=optimizers,
        component_joint_draws=draws,
        policy_manifest=pipeline.disagreement.policy_manifest,
        council_manifest=pipeline.disagreement.council_manifest,
    )

    with pytest.raises((AssemblyError, ContractError), match="exact signed cards|exact U13 draws"):
        SealedPipelineOutput.create(
            field_revision_digest=pipeline.field_revision_digest,
            prediction_evidence=pipeline.prediction_evidence,
            joint_draws=pipeline.joint_draws,
            optimizer=pipeline.optimizer,
            disagreement=substituted,
            weight_authority=pipeline.weight_authority,
            operational_weight_authority=pipeline.operational_weight_authority,
            dependence_artifact=pipeline.dependence_artifact,
            manual_authority=pipeline.manual_authority,
            total_latency_ms=pipeline.total_latency_ms,
        )


def test_field_pipeline_generates_one_common_uniform_plan_for_all_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.domain.joint_dependence as joint_module

    generated = 0
    aligned_batches = 0
    original = joint_module._joint_uniforms_from_slots
    original_aligned = joint_module.sample_aligned_positive_distributions

    def counted(*args, **kwargs):
        nonlocal generated
        generated += 1
        return original(*args, **kwargs)

    def counted_aligned(*args, **kwargs):
        nonlocal aligned_batches
        aligned_batches += 1
        return original_aligned(*args, **kwargs)

    monkeypatch.setattr(joint_module, "_joint_uniforms_from_slots", counted)
    monkeypatch.setattr(joint_module, "sample_aligned_positive_distributions", counted_aligned)
    _store, field, build, _lifecycle = _bootstrap(tmp_path / "one-crn-plan.sqlite3")

    pipeline = build(field)

    assert pipeline.disagreement is not None
    assert generated == 1
    assert aligned_batches == len(pipeline.pools)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        low = counterfactual_sheet_from_optimizer("pooled", pipeline.optimizer)
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_UP
        high = counterfactual_sheet_from_optimizer("pooled", pipeline.optimizer)
    assert low == high


def test_serialized_pooled_draws_cannot_substitute_recomputed_samples(
    tmp_path: Path,
) -> None:
    from strathmark.v3.contracts.forecasts import _samples_digest

    _store, field, build, _lifecycle = _bootstrap(
        tmp_path / "forged-pooled-samples.sqlite3",
        available_assessors=(AssessorKind.FORMULA,),
    )
    pipeline = build(field)
    value = pipeline.joint_draws.to_dict()
    first = value["competitors"][0]
    first["samples_ms"] = [item + 37 for item in first["samples_ms"]]
    first["samples_digest"] = _samples_digest(
        samples_ms=tuple(first["samples_ms"]),
        seed=value["inputs"]["seed"],
        distribution_digest=first["distribution_digest"],
        common_random_map_digest=value["common_random_map_digest"],
    )
    content = {key: item for key, item in value.items() if key != "joint_samples_digest"}
    value["joint_samples_digest"] = canonical_digest(
        content, max_bytes=16_777_216, max_items=500_000
    )
    forged_draws = JointDraws.from_dict(value)
    distributions = {
        item.competitor_id: item.distribution for item in pipeline.manual_authority.estimates
    }
    forecasts = tuple(
        FieldCompetitorForecast(
            draw.competitor_id,
            draw.draw_slot,
            distributions[draw.competitor_id],
            draw.crn_index,
        )
        for draw in forged_draws.competitors
    )
    forged_field = OptimizationField.from_joint_draws(
        forged_draws,
        forecasts=forecasts,
        source_receipt_digest=pipeline.optimizer.field.source_receipt_digest,
        pool_receipt_digest=pipeline.optimizer.field.pool_receipt_digest,
    )
    forged_optimizer = optimize_and_verify_field(forged_field, ceiling=183)

    with pytest.raises(AssemblyError, match="exact pooled.*dependence"):
        SealedPipelineOutput.create(
            field_revision_digest=field.revision_digest,
            prediction_evidence=pipeline.prediction_evidence,
            joint_draws=forged_draws,
            optimizer=forged_optimizer,
            disagreement=None,
            weight_authority=pipeline.weight_authority,
            operational_weight_authority=pipeline.operational_weight_authority,
            dependence_artifact=pipeline.dependence_artifact,
            manual_authority=pipeline.manual_authority,
            total_latency_ms=15,
        )


def test_disagreement_authority_preflights_component_sources_before_optimizer_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, field, build, _lifecycle = _bootstrap(tmp_path / "disagreement-preflight.sqlite3")
    pipeline = build(field)
    assert pipeline.disagreement is not None
    authority = pipeline.disagreement.to_authority_dict()
    authority["component_optimizers"] = authority["component_optimizers"] * 4

    def forbidden_replay(_cls, _value):
        raise AssertionError("invalid component cardinality must fail before U14 replay")

    monkeypatch.setattr(
        VerifiedOptimizerReceipt,
        "from_authority_dict",
        classmethod(forbidden_replay),
    )
    with pytest.raises(AssemblyConflict, match="component.*cardinality"):
        OperationalDisagreementReceipt.from_authority_dict(authority)


@pytest.mark.parametrize("mutation", ("corrupt", "delete", "blob_corrupt", "blob_delete"))
def test_operational_disagreement_authority_survives_restart_and_corruption_blocks(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "resolver.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    authority = pipeline.disagreement
    assert authority is not None
    FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:persist-disagreement",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: pipeline,
    )

    with open_v3_connection(path, read_only=True) as connection:
        row = connection.execute(
            "SELECT authority_blob_json, authority_blob_digest "
            "FROM v3_field_disagreement_authority_blobs "
            "WHERE receipt_digest=?",
            (authority.receipt_digest,),
        ).fetchone()
    assert row is not None
    authority_projection = json.loads(str(row[0]))
    assert authority_projection["schema_version"] == (
        "strathmark-v3-disagreement-authority-blob-projection-v1"
    )
    assert "receipt" not in authority_projection
    assert BlobReferenceV2.from_dict(
        authority_projection["authority_blob_reference"]
    ).digest == str(row[1])
    authority_blob_reference = BlobReferenceV2.from_dict(
        authority_projection["authority_blob_reference"]
    )

    restarted = SQLiteFieldProjectionStore(
        path, signer=store._signer, trust_store=store._trust_store
    )
    resolved = OperationalDisagreementReceipt.from_dict(
        authority.to_dict(),
        authority_resolver=restarted.resolve_disagreement_authority,
    )
    assert resolved == authority

    if mutation.startswith("blob_"):
        blob_path = store._blob_store.path_for(authority_blob_reference.digest)
        if mutation == "blob_corrupt":
            blob_path.write_bytes(b"{}")
        else:
            blob_path.unlink()
    else:
        with open_v3_connection(path) as connection:
            connection.execute(
                "DROP TRIGGER v3_field_disagreement_authority_blobs_no_update"
                if mutation == "corrupt"
                else "DROP TRIGGER v3_field_disagreement_authority_blobs_no_delete"
            )
            if mutation == "corrupt":
                connection.execute(
                    "UPDATE v3_field_disagreement_authority_blobs "
                    "SET authority_blob_json='{}' WHERE receipt_digest=?",
                    (authority.receipt_digest,),
                )
            else:
                connection.execute(
                    "DELETE FROM v3_field_disagreement_authority_blobs WHERE receipt_digest=?",
                    (authority.receipt_digest,),
                )
            connection.executescript(
                "CREATE TRIGGER v3_field_disagreement_authority_blobs_no_update "
                "BEFORE UPDATE ON v3_field_disagreement_authority_blobs BEGIN "
                "SELECT RAISE(ABORT, 'field disagreement authority blob is immutable'); END;"
                if mutation == "corrupt"
                else "CREATE TRIGGER v3_field_disagreement_authority_blobs_no_delete "
                "BEFORE DELETE ON v3_field_disagreement_authority_blobs BEGIN "
                "SELECT RAISE(ABORT, 'field disagreement authority blob is immutable'); END;"
            )
            connection.commit()
    if mutation != "delete":
        with pytest.raises((ProjectionConflict, AssemblyConflict), match="authority blob"):
            restarted.resolve_disagreement_authority(authority.receipt_digest)
    else:
        assert restarted.resolve_disagreement_authority(authority.receipt_digest) is None
    with pytest.raises(
        (ProjectionConflict, ProjectionError, AssemblyConflict), match="authority|blob"
    ):
        SQLiteFieldProjectionStore(path, signer=store._signer, trust_store=store._trust_store)


def test_preexisting_disagreement_digest_conflict_rolls_back_field_event(
    tmp_path: Path,
) -> None:
    path = tmp_path / "disagreement-conflict.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    authority = pipeline.disagreement
    assert authority is not None
    with open_v3_connection(path) as connection:
        event_count = int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])
        connection.execute(
            "INSERT INTO v3_field_disagreement_authority_blobs VALUES (?, ?, ?, '{}', ?, ?, ?, ?)",
            (
                authority.receipt_digest,
                authority.field_revision_digest,
                field.bundle_digest,
                "0" * 64,
                authority.policy_manifest.body_digest,
                (
                    None
                    if authority.council_manifest is None
                    else authority.council_manifest.body_digest
                ),
                NOW,
            ),
        )
        connection.commit()

    with pytest.raises(
        (ProjectionConflict, AssemblyConflict),
        match="stored disagreement authority|conflicted",
    ):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:disagreement-conflict",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=lambda _field: pipeline,
        )

    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0]) == event_count
        )
        assert connection.execute("SELECT 1 FROM v3_field_receipts").fetchone() is None


def test_commit_uses_preverified_disagreement_blob_without_in_lock_typed_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "prepared-disagreement.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)

    def forbidden(_cls, _value):
        raise AssertionError("writer lock decoded and replayed disagreement authority")

    monkeypatch.setattr(
        OperationalDisagreementReceipt,
        "from_authority_dict",
        classmethod(forbidden),
    )
    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:prepared-disagreement",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: pipeline,
    )
    assert result.receipt.field_id == field.field_id


def test_live_field_commit_refreshes_only_its_tournament_and_matches_full_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import strathmark.v3.infrastructure.sqlite.projections as projection_module

    path = tmp_path / "incremental-approval-refresh.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    original = projection_module._rebuild_approval_projection_connection
    refresh_calls: list[tuple[str | None, bool]] = []

    def tracked_refresh(connection, **kwargs):
        refresh_calls.append((kwargs.get("tournament_id"), kwargs.get("rebuild_decisions", True)))
        return original(connection, **kwargs)

    monkeypatch.setattr(
        projection_module,
        "_rebuild_approval_projection_connection",
        tracked_refresh,
    )
    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:incremental-approval-refresh",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: pipeline,
    )
    assert refresh_calls == [(str(field.tournament_id), False)]

    monkeypatch.setattr(
        projection_module,
        "_rebuild_approval_projection_connection",
        original,
    )
    store.verify()
    page = store.approval_page(tournament_id=str(field.tournament_id), offset=0, limit=10)
    assert [row.receipt_id for row in page.rows] == [str(result.receipt.receipt_id)]
    assert page.rows[0].ordinary_batch_eligible is True


def test_missing_receipt_projection_rebuilds_from_event_and_cas_before_exact_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "receipt-projection-rebuild.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    service = FieldAssemblyService(store)
    first = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:receipt-rebuild",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: pipeline,
    )
    with open_v3_connection(path) as connection:
        for table in (
            "v3_approval_snapshot_rows",
            "v3_approval_snapshot_history",
            "v3_approval_decision_projection",
            "v3_approval_command_projection",
            "v3_approval_details",
            "v3_approval_queue_rows",
            "v3_approval_schedule",
            "v3_approval_projection_meta",
        ):
            connection.execute(f"DELETE FROM {table}")
        connection.execute("DELETE FROM v3_field_receipts")
        connection.commit()
    with pytest.raises(ProjectionError, match="coverage"):
        store.verify()

    original_rebuild = store.rebuild_field_receipt_projection
    rebuild_calls = 0

    def tracked_rebuild():
        nonlocal rebuild_calls
        rebuild_calls += 1
        return original_rebuild()

    monkeypatch.setattr(store, "rebuild_field_receipt_projection", tracked_rebuild)

    def forbidden_provider(_field):
        raise AssertionError("exact receipt recovery reran the field pipeline")

    recovered = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:receipt-rebuild",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=forbidden_provider,
    )
    assert rebuild_calls == 1
    assert recovered == first
    store.verify()
    page = store.approval_page(tournament_id="tournament:show", offset=0, limit=100)
    assert any(row.receipt_id == str(first.receipt.receipt_id) for row in page.rows)


def test_field_commit_hashes_required_blobs_before_entering_event_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    import strathmark.v3.infrastructure.blobs as blob_module
    import strathmark.v3.infrastructure.sqlite.event_store as event_store_module

    path = tmp_path / "blob-hash-before-writer.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    original_transaction = event_store_module.immediate_transaction
    original_digest = blob_module._file_digest
    writer_active = False
    digest_calls = 0

    @contextmanager
    def tracked_transaction(connection):
        nonlocal writer_active
        with original_transaction(connection):
            writer_active = True
            try:
                yield
            finally:
                writer_active = False

    def reject_in_writer_digest(blob_path: Path) -> str:
        nonlocal digest_calls
        assert not writer_active
        digest_calls += 1
        return original_digest(blob_path)

    monkeypatch.setattr(event_store_module, "immediate_transaction", tracked_transaction)
    monkeypatch.setattr(blob_module, "_file_digest", reject_in_writer_digest)
    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:prehashed-blobs",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: pipeline,
    )

    assert result.receipt.field_id == field.field_id
    assert digest_calls == 2


def test_same_length_blob_swap_between_prepare_and_commit_emits_no_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "prepared-blob-swap.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    original_execute = store._events.execute
    with open_v3_connection(path, read_only=True) as connection:
        event_count = int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])

    def swap_before_writer(*args, **kwargs):
        candidates = store._blob_store.complete_paths()
        assert candidates
        target = max(candidates, key=lambda item: item.stat().st_size)
        target.write_bytes(b"x" * target.stat().st_size)
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(store._events, "execute", swap_before_writer)
    with pytest.raises((ProjectionConflict, AssemblyConflict), match="blob|authority|conflict"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:prepared-blob-swap",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=lambda _field: pipeline,
        )

    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0]) == event_count
        )
        assert connection.execute("SELECT 1 FROM v3_field_receipts").fetchone() is None


def test_blob_publish_before_event_failure_leaves_only_recoverable_orphan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "blob-orphan.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    authority = pipeline.disagreement
    assert authority is not None
    authority_bytes = canonical_bytes(
        authority.to_authority_dict(), max_bytes=16_777_216, max_items=2_000_000
    )
    authority_digest = sha256(authority_bytes).hexdigest()
    with open_v3_connection(path, read_only=True) as connection:
        event_count = int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])

    def crash_before_event(*_args, **_kwargs):
        raise RuntimeError("injected event append crash")

    monkeypatch.setattr(store._events, "execute", crash_before_event)
    with pytest.raises(RuntimeError, match="injected event append crash"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:blob-orphan",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=lambda _field: pipeline,
        )

    assert store._blob_store.path_for(authority_digest).is_file()
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0]) == event_count
        )
        assert connection.execute("SELECT 1 FROM v3_field_receipts").fetchone() is None


@pytest.mark.parametrize("authority_kind", ("capacity", "dependence"))
def test_receipt_requires_capacity_and_dependence_authority_after_restart(
    tmp_path: Path, authority_kind: str
) -> None:
    path = tmp_path / "authority-reference.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:authority-reference",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: pipeline,
    )
    with open_v3_connection(path) as connection:
        if authority_kind == "capacity":
            connection.execute("DROP TRIGGER v3_field_capacity_authorities_no_delete")
            connection.execute(
                "DELETE FROM v3_field_capacity_authorities WHERE authority_digest=?",
                (field.capacity_authority_digest,),
            )
            connection.executescript(
                "CREATE TRIGGER v3_field_capacity_authorities_no_delete "
                "BEFORE DELETE ON v3_field_capacity_authorities BEGIN "
                "SELECT RAISE(ABORT, 'field capacity authority is immutable'); END;"
            )
        else:
            connection.execute(
                "DELETE FROM v3_field_dependence_authorities WHERE artifact_digest=?",
                (pipeline.dependence_artifact.artifact_digest,),
            )
        connection.commit()

    with pytest.raises((ProjectionConflict, ProjectionError), match="authority"):
        SQLiteFieldProjectionStore(path, signer=store._signer, trust_store=store._trust_store)


def test_call_order_and_deadline_are_u5_authority_and_bind_receipt(
    tmp_path: Path,
) -> None:
    store, field, build, _lifecycle = _bootstrap(tmp_path / "schedule.sqlite3")
    calls = 0

    def counted(revision: FrozenFieldRevision) -> SealedPipelineOutput:
        nonlocal calls
        calls += 1
        return build(revision)

    forged = _field(
        evidence_digest=field.evidence_digest,
        tournament_event_sequence=field.tournament_event_sequence,
        tournament_epoch_id=field.tournament_epoch_id,
        call_order=2,
    )
    with pytest.raises(AssemblyConflict, match="current U5 ingress"):
        FieldAssemblyService(store).assemble(
            field=forged,
            caller_namespace="manager",
            request_identity="idempotency:forged-schedule",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=counted,
        )
    assert calls == 0

    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:real-schedule",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=counted,
    )
    validations = next(
        section.payload.to_value()
        for section in result.receipt.sections
        if section.kind.value == "validations"
    )
    assert validations["call_order"] == 1
    assert validations["scheduled_at"] == "2026-08-24T18:00:00.000Z"
    assert validations["deadline_at"] == "2026-08-24T18:02:00.000Z"


def test_u5_rejects_field_above_declared_signed_entrant_capacity() -> None:
    competitors = [f"competitor:c{index}" for index in range(13)]
    stands = [f"stand:s{index}" for index in range(13)]

    with pytest.raises(ContractError, match="declared capacity"):
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier("field:oversized"),
            1,
            StableIdentifier("tournament:show"),
            StableIdentifier("round:final"),
            {
                "competitor_ids": competitors,
                "target_context": _context().to_dict(),
                "stand_ids": stands,
                "capacity_authority_digest": _capacity_authority_digest(),
                "max_field_entrants": 12,
                "call_order": 1,
                "scheduled_at": "2026-08-24T18:00:00.000Z",
                "deadline_at": "2026-08-24T18:02:00.000Z",
            },
        )


def test_signed_capacity_can_raise_field_limit_and_is_u5_revision_authority(
    tmp_path: Path,
) -> None:
    capacity = replace(_capacity_manifest(), max_field_entrants=14)
    store, _field_revision, _build, lifecycle = _bootstrap(
        tmp_path / "capacity.sqlite3", capacity_manifest=capacity
    )
    competitors = [f"competitor:c{index}" for index in range(14)]
    stands = [f"stand:s{index}" for index in range(14)]

    _ingest_field(
        lifecycle,
        2,
        capacity=capacity,
        competitors=competitors,
        stands=stands,
    )
    with open_v3_connection(store.database_path, read_only=True) as connection:
        snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' AND entity_id='field:final' "
                "ORDER BY upstream_revision DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert len(snapshot["competitor_ids"]) == 14
    assert snapshot["max_field_entrants"] == 14
    assert snapshot["capacity_authority_digest"] == _capacity_authority_digest(capacity)

    with pytest.raises(ContractError, match="declared capacity"):
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier("field:too-large"),
            1,
            StableIdentifier("tournament:show"),
            StableIdentifier("round:final"),
            {
                "competitor_ids": [f"competitor:x{index}" for index in range(15)],
                "target_context": _context().to_dict(),
                "stand_ids": [f"stand:x{index}" for index in range(15)],
                "capacity_authority_digest": _capacity_authority_digest(capacity),
                "max_field_entrants": 14,
                "call_order": 3,
                "scheduled_at": "2026-08-24T18:00:00.000Z",
                "deadline_at": "2026-08-24T18:02:00.000Z",
            },
        )


def test_pending_per_result_weight_binding_is_not_operational_authority(
    tmp_path: Path,
) -> None:
    store, field, pipeline_builder, _lifecycle = _bootstrap(tmp_path / "raw-weight.sqlite3")
    pending = pipeline_builder(field).weight_authority

    with pytest.raises(ProjectionConflict, match="operational weight authority"):
        store.install_weight_authority(pending, installed_at=NOW)


def test_real_u12_live_freeze_and_latest_control_are_operational_authority(
    tmp_path: Path,
) -> None:
    lifecycle, tournament, completed, successor, _closure = _bootstrap_empty_closure(
        tmp_path / "live"
    )
    path = lifecycle.projections.database_path
    signer = P256EphemeralSigner.generate("integrity-key:u15-live")
    trust_store = IntegrityTrustStore((signer.identity,))
    credibility = SQLiteCredibilityReactionService(
        path,
        trust_store=trust_store,
        consequence_evaluator=None,
        policy_manifest=seal_credibility_policy(
            CredibilityPolicy(),
            optimizer_bundle_digest="e" * 64,
            signer=signer,
            created_at=NOW,
        ),
    )
    context = ContextNode()
    frozen = credibility.freeze_live_weights(
        completed,
        successor,
        context=context,
        command_id=IdempotencyKey("command:u15-live-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=20,
    )
    with open_v3_connection(path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? "
            "AND event_kind=? ORDER BY global_sequence",
            (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
        ).fetchall()
        epoch = connection.execute(
            "SELECT epoch_id, epoch_digest, maximum_tournament_sequence "
            "FROM v3_evidence_epochs WHERE round_id=?",
            (str(successor),),
        ).fetchone()
    assert epoch is not None
    weight_events = tuple(EventEnvelope.from_dict(json.loads(str(row[0]))) for row in rows)
    freeze_event = next(
        event
        for event in weight_events
        if event.command.payload.to_value().get("schema_version")
        == "strathmark-v3-live-round-weight-freeze-v1"
    )
    freeze_payload = freeze_event.command.payload.to_value()
    live_receipt = WeightReceipt(
        context,
        frozen.current_weights,
        frozen.baseline.components,
        frozen.baseline.calibration_cutoff_at_utc,
        frozen.baseline.policy_digest,
        live_effective_weight_receipt_digest(
            freeze_event.event_digest, context, frozen.current_weights
        ),
    )
    binding = WeightAuthorityBinding.pending(
        live_receipt,
        ledger_projection_digest=freeze_payload["ledger_projection_digest"],
        tournament_event_sequence=int(epoch[2]),
        source_global_sequence=freeze_event.global_sequence,
    )

    def authority_for(control: EventEnvelope) -> OperationalWeightAuthority:
        return OperationalWeightAuthority.create(
            kind=OperationalWeightKind.LIVE_ROUND_FREEZE,
            binding=binding,
            tournament_id=tournament,
            round_id=successor,
            epoch_id=StableIdentifier(str(epoch[0])),
            epoch_digest=str(epoch[1]),
            frozen_tournament_sequence=int(epoch[2]),
            authority_event_sequence=freeze_event.global_sequence,
            authority_event_digest=freeze_event.event_digest,
            completed_round_id=completed,
            round_close_event_digest=freeze_payload["round_close_event_digest"],
            baseline_receipt_digest=freeze_payload["baseline_receipt_digest"],
            control_event_sequence=control.global_sequence,
            control_event_digest=control.event_digest,
        )

    store = SQLiteFieldProjectionStore(path, signer=signer, trust_store=trust_store)
    initial = authority_for(freeze_event)
    store.install_weight_authority(initial, installed_at=NOW)
    store.verify_weight_authority(initial)

    credibility.record_live_control(
        tournament,
        action="suspend",
        reason="judge pause",
        preview_manifest=_control_preview(credibility, signer, tournament, "suspend"),
        command_id=IdempotencyKey("command:u15-live-suspend"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=21,
    )
    with pytest.raises(ProjectionConflict, match="stale"):
        store.verify_weight_authority(initial)
    credibility.record_live_control(
        tournament,
        action="re_enable",
        reason="judge resumed",
        preview_manifest=_control_preview(credibility, signer, tournament, "re_enable"),
        command_id=IdempotencyKey("command:u15-live-resume"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=22,
    )
    with open_v3_connection(path, read_only=True) as connection:
        row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE event_kind=? "
            "ORDER BY global_sequence DESC LIMIT 1",
            (EventKind.LIVE_RESUMED.value,),
        ).fetchone()
    assert row is not None
    resumed_event = EventEnvelope.from_dict(json.loads(str(row[0])))
    resumed = authority_for(resumed_event)
    store.install_weight_authority(resumed, installed_at=NOW)
    store.verify_weight_authority(resumed)

    forged_binding = WeightAuthorityBinding.pending(
        replace(live_receipt, calibration_cutoff_at_utc="2026-08-24T17:58:00.000Z"),
        ledger_projection_digest=binding.ledger_projection_digest,
        tournament_event_sequence=binding.tournament_event_sequence,
        source_global_sequence=binding.source_global_sequence,
    )
    with pytest.raises(ProjectionConflict, match="baseline policy"):
        store.install_weight_authority(
            OperationalWeightAuthority.create(
                **{
                    **resumed.content_value(),
                    "binding": forged_binding,
                }
            ),
            installed_at=NOW,
        )


@pytest.mark.parametrize(
    ("available", "warning"),
    [
        ((AssessorKind.FORMULA,), "manual_single_survivor"),
        ((), "manual_construction_required"),
    ],
)
def test_deliberate_one_or_zero_assessor_path_optimizes_complete_field(
    tmp_path: Path,
    available: tuple[AssessorKind, ...],
    warning: str,
) -> None:
    store, field, build, _lifecycle = _bootstrap(
        tmp_path / f"manual-{len(available)}.sqlite3",
        available_assessors=available,
    )

    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity=f"idempotency:manual-{len(available)}",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )

    assert warning in result.receipt.warning_codes
    assert min(item.mark for item in result.receipt.marks) == 3
    assert tuple(item.competitor_id for item in result.receipt.marks) == (
        StableIdentifier("competitor:b"),
        StableIdentifier("competitor:a"),
    )
    validations = next(
        section.payload.to_value()
        for section in result.receipt.sections
        if section.kind.value == "validations"
    )
    assert validations["manual_authority"]["actor_id"] == "actor:manager"
    assert validations["manual_authority"]["scope"] == "upcoming_race"


def test_manual_authority_actor_must_match_authenticated_assembly_actor(
    tmp_path: Path,
) -> None:
    store, field, build, _lifecycle = _bootstrap(
        tmp_path / "manual-actor.sqlite3",
        available_assessors=(),
    )

    with pytest.raises(AssemblyConflict, match="actor"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:manual-wrong-actor",
            actor_id="actor:other",
            occurred_at=NOW,
            build_pipeline=build,
        )


def test_mixed_two_assessor_source_sets_require_complete_manual_construction(
    tmp_path: Path,
) -> None:
    store, field, build, _lifecycle = _bootstrap(
        tmp_path / "manual-mixed-two.sqlite3",
        available_assessors_by_competitor={
            "competitor:b": (AssessorKind.FORMULA, AssessorKind.ML),
            "competitor:a": (AssessorKind.FORMULA, AssessorKind.LLM_COUNCIL),
        },
    )

    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:manual-mixed-two",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )

    assert "manual_construction_required" in result.receipt.warning_codes


def test_submit_construction_advances_receipt_revision_without_impersonating_u5(
    tmp_path: Path,
) -> None:
    path = tmp_path / "construction.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path, available_assessors=())
    service = FieldAssemblyService(store)
    initial_pipeline = build(field)
    initial = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:zero-candidate",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: initial_pipeline,
    )
    assert initial.receipt.upstream_field_revision == 1
    assert initial.receipt.receipt_revision == 1
    candidate = build(field)
    assert candidate.manual_authority is not None
    submission = ManualConstructionSubmission.create(
        prior_receipt_id=initial.receipt.receipt_id,
        prior_receipt_digest=initial.receipt.content_digest,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        manual_authority_digest=candidate.manual_authority.authority_digest,
        actor_id=ACTOR,
        reason_code="judge_complete_expected_time_construction",
        scope=OverrideScope.UPCOMING_RACE,
        submitted_at="2026-08-24T18:00:01.000Z",
    )
    constructed_pipeline = _pipeline_with_supersession(
        candidate, construction_submission=submission
    )

    constructed = service.submit_construction(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:submit-construction",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:00:01.000Z",
        build_pipeline=lambda _field: constructed_pipeline,
    )

    assert constructed.receipt.upstream_field_revision == 1
    assert constructed.receipt.receipt_revision == 2
    assert constructed.receipt.supersedes_receipt_id == initial.receipt.receipt_id
    with pytest.raises(AssemblyConflict, match="supersession kind"):
        service.assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:untyped-same-upstream",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:00:02.000Z",
            build_pipeline=lambda _field: constructed_pipeline,
        )
    validations = next(
        section.payload.to_value()
        for section in constructed.receipt.sections
        if section.kind.value == "validations"
    )
    assert (
        validations["manual_authority"]["mode"]
        == ManualConstructionMode.COMPLETE_EXPECTED_TIME.value
    )
    assert "degraded_two_assessors" not in constructed.receipt.warning_codes


def test_initial_complete_manual_construction_creates_first_trusted_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "initial-construction.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path, available_assessors=())
    candidate = build(field)
    assert candidate.manual_authority is not None
    submission = ManualConstructionSubmission.create(
        prior_receipt_id=None,
        prior_receipt_digest=None,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        manual_authority_digest=candidate.manual_authority.authority_digest,
        actor_id=ACTOR,
        reason_code="judge_complete_expected_time_construction",
        scope=OverrideScope.UPCOMING_RACE,
        submitted_at="2026-08-24T18:02:01.000Z",
    )
    pipeline = _pipeline_with_supersession(candidate, construction_submission=submission)

    result = FieldAssemblyService(store).submit_construction(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:initial-submit-construction",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:02:01.000Z",
        build_pipeline=lambda _field: pipeline,
    )

    assert result.receipt.receipt_revision == 1
    assert result.receipt.supersedes_receipt_id is None


def test_manual_action_kind_cannot_resolve_with_the_wrong_judge_construction(
    tmp_path: Path,
) -> None:
    from strathmark.v3.application.manual_actions import (
        ManualActionEntrant,
        create_manual_action_requirement,
    )
    from strathmark.v3.infrastructure.sqlite.manual_actions import (
        SQLiteManualActionRequirementStore,
    )

    path = tmp_path / "wrong-manual-action-kind.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path, available_assessors=(AssessorKind.FORMULA,))
    candidate = build(field)
    requirement = create_manual_action_requirement(
        field_id=field.field_id,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        target_context_digest=field.target_context.digest,
        historical_cutoff_key=field.historical_cutoff_key,
        tournament_epoch_id=field.tournament_epoch_id,
        bundle_digest=field.bundle_digest,
        hard_deadline_at=field.deadline_at,
        entrants=tuple(
            ManualActionEntrant(
                item.competitor_id,
                (),
                item.publication.binding_digest,
                None,
            )
            for item in candidate.prediction_evidence
        ),
        signer=store._signer,
        created_at=field.deadline_at,
    )
    action_store = SQLiteManualActionRequirementStore(
        path, signer=store._signer, trust_store=store._trust_store
    )
    action_store.publish(requirement)
    assert candidate.manual_authority is not None
    submission = ManualConstructionSubmission.create(
        prior_receipt_id=None,
        prior_receipt_digest=None,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        manual_authority_digest=candidate.manual_authority.authority_digest,
        actor_id=ACTOR,
        reason_code="judge_single_survivor_acceptance",
        scope=OverrideScope.UPCOMING_RACE,
        submitted_at="2026-08-24T18:02:01.000Z",
    )
    pipeline = _pipeline_with_supersession(candidate, construction_submission=submission)

    with pytest.raises(AssemblyConflict, match="kind differs"):
        FieldAssemblyService(store, manual_action_store=action_store).submit_construction(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:wrong-manual-action-kind",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:02:01.000Z",
            build_pipeline=lambda _field: pipeline,
            manual_action_binding=requirement.binding,
        )
    assert action_store.current(field.field_id) == requirement
    with open_v3_connection(path, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_field_receipts").fetchone()[0] == 0


def test_expected_time_override_binds_u13_receipt_and_same_upstream_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "expected-time-override.sqlite3"
    manual_medians = {"competitor:b": 45_000, "competitor:a": 55_000}
    store, field, build, _lifecycle = _bootstrap(
        path,
        available_assessors=(),
        manual_medians=manual_medians,
    )
    service = FieldAssemblyService(store)
    before_pipeline = build(field)
    before_result = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:override-before",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: before_pipeline,
    )
    before_sheet = FieldSheetSnapshot.create(
        field_id=field.field_id,
        expected_times_ms=tuple(
            (item.competitor_id, item.expected_time_ms)
            for item in before_pipeline.optimizer.field.competitors
        ),
        marks=tuple((item.competitor_id, item.mark) for item in before_result.receipt.marks),
        pool_receipt_digest=before_pipeline.optimizer.field.pool_receipt_digest,
        optimizer_receipt_digest=before_pipeline.optimizer.receipt.receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )

    manual_medians["competitor:b"] = 45_100
    after_pipeline = build(field)
    after_sheet = FieldSheetSnapshot.create(
        field_id=field.field_id,
        expected_times_ms=tuple(
            (item.competitor_id, item.expected_time_ms)
            for item in after_pipeline.optimizer.field.competitors
        ),
        marks=tuple(
            zip(
                after_pipeline.optimizer.receipt.competitor_ids,
                after_pipeline.optimizer.receipt.selected_marks,
                strict=True,
            )
        ),
        pool_receipt_digest=after_pipeline.optimizer.field.pool_receipt_digest,
        optimizer_receipt_digest=after_pipeline.optimizer.receipt.receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    request = ExpectedTimeOverrideRequest.create(
        override_id=StableIdentifier("override:competitor-b-final"),
        competitor_id=StableIdentifier("competitor:b"),
        target_context_digest=field.target_context.digest,
        expected_raw_time_ms=45_100,
        scope=OverrideScope.UPCOMING_RACE,
        scope_boundary_id=field.field_id,
        actor="actor:manager",
        reason="judge corrected the starting estimate",
        supersedes_override_id=None,
    )
    proof = OverrideRecomputationProof.create(before_sheet, after_sheet)
    after_sections = after_pipeline.section_values()
    component_outputs = next(
        value for kind, value in after_sections.items() if kind.value == "component_outputs"
    )
    pooled_distribution = next(
        value for kind, value in after_sections.items() if kind.value == "pooled_distribution"
    )
    override_receipt = create_override_receipt(
        request,
        before_sheet,
        after_sheet,
        proof,
        canonical_digest(component_outputs),
        canonical_digest(pooled_distribution),
        field.evidence_digest,
        field.tournament_epoch_id,
    )
    authority = OperationalExpectedTimeOverrideAuthority.create(
        prior_receipt_id=before_result.receipt.receipt_id,
        prior_receipt_digest=before_result.receipt.content_digest,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        override_receipt=override_receipt,
        after_optimizer_verification_digest=after_pipeline.optimizer.verification_digest,
    )
    sealed_after = _pipeline_with_supersession(
        after_pipeline,
        expected_time_override=authority,
    )

    overridden = service.submit_expected_time_override(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:override-submit",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:00:01.000Z",
        build_pipeline=lambda _field: sealed_after,
    )

    assert overridden.receipt.upstream_field_revision == 1
    assert overridden.receipt.receipt_revision == 2
    assert overridden.receipt.supersedes_receipt_id == before_result.receipt.receipt_id
    assert override_receipt.before_time_ms == 45_000
    assert override_receipt.after_time_ms == 45_100
    assert (
        dict(before_sheet.marks)[StableIdentifier("competitor:b")]
        == dict(after_sheet.marks)[StableIdentifier("competitor:b")]
    )
    validations = next(
        section.payload.to_value()
        for section in overridden.receipt.sections
        if section.kind.value == "validations"
    )
    assert (
        validations["expected_time_override"]["override_receipt"]["receipt_digest"]
        == override_receipt.receipt_digest
    )


@pytest.mark.parametrize(
    ("valid_count", "warns", "assembles"),
    [(3, False, True), (2, True, True), (1, False, False), (0, False, False)],
)
def test_council_member_availability_uses_typed_valid_status(
    tmp_path: Path, valid_count: int, warns: bool, assembles: bool
) -> None:
    statuses = tuple(
        CouncilMemberStatus.VALID if index < valid_count else CouncilMemberStatus.FAILED
        for index in range(3)
    )
    store, field, build, _lifecycle = _bootstrap(
        tmp_path / f"council-{valid_count}.sqlite3", council_statuses=statuses
    )

    if not assembles:
        with pytest.raises(AssemblyError, match="council unavailable"):
            build(field)
        return

    pipeline = build(field)
    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity=f"idempotency:council-{valid_count}",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _revision: pipeline,
    )

    assert pipeline.council_valid_count == valid_count
    assert ("degraded_llm_council" in result.receipt.warning_codes) is warns


def test_scratch_or_context_change_requires_new_revision_and_supersedes_before_issue(
    tmp_path: Path,
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    service = FieldAssemblyService(store)
    first = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:first",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    with pytest.raises(AssemblyConflict, match="monotonic"):
        service.assemble(
            field=_field(
                reverse_transport=True,
                evidence_digest=field.evidence_digest,
                tournament_event_sequence=field.tournament_event_sequence,
                tournament_epoch_id=field.tournament_epoch_id,
            ),
            caller_namespace="manager",
            request_identity="idempotency:changed-same-revision",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=build,
        )
    _ingest_field(lifecycle, 2)
    field2 = _field(
        2,
        evidence_digest=field.evidence_digest,
        tournament_event_sequence=field.tournament_event_sequence,
        tournament_epoch_id=field.tournament_epoch_id,
    )
    second = service.assemble(
        field=field2,
        caller_namespace="manager",
        request_identity="idempotency:second",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:00:01.000Z",
        build_pipeline=build,
    )
    assert second.receipt.supersedes_receipt_id == first.receipt.receipt_id
    assert store.current_receipt("field:final").receipt_id == second.receipt.receipt_id
    assert store.receipt(str(first.receipt.receipt_id)) == first.receipt


@pytest.mark.parametrize(
    "operation",
    ("upstream", "construction", "expected_time_override"),
)
def test_every_supersession_authenticates_current_receipt_before_provider_work(
    tmp_path: Path,
    operation: str,
) -> None:
    path = tmp_path / f"prior-pre-provider-{operation}.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    service = FieldAssemblyService(store)
    service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:prior-initial",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    if operation == "upstream":
        _ingest_field(lifecycle, 2)
        candidate = _field(
            2,
            evidence_digest=field.evidence_digest,
            tournament_event_sequence=field.tournament_event_sequence,
            tournament_epoch_id=field.tournament_epoch_id,
        )
        submit = service.assemble
    elif operation == "construction":
        candidate = field
        submit = service.submit_construction
    else:
        candidate = field
        submit = service.submit_expected_time_override
    with open_v3_connection(path) as connection:
        connection.execute(
            "UPDATE v3_field_receipts SET pipeline_digest=? "
            "WHERE field_id=? AND superseded_by_sequence IS NULL",
            ("f" * 64, str(field.field_id)),
        )
        before_events = int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])

    provider_called = False

    def forbidden_provider(_field: FrozenFieldRevision) -> SealedPipelineOutput:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider work ran before prior receipt authentication")

    with pytest.raises(ProjectionError, match="local authority"):
        submit(
            field=candidate,
            caller_namespace="manager",
            request_identity=f"idempotency:prior-{operation}",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:00:01.000Z",
            build_pipeline=forbidden_provider,
        )

    assert provider_called is False
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0]) == before_events
        )


@pytest.mark.parametrize(
    "mutation",
    ("roster", "stands", "context", "call_order", "deadline", "epoch"),
)
def test_self_redigested_u5_projection_tamper_fails_before_provider_work(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = tmp_path / f"u5-source-{mutation}.sqlite3"
    store, field, _build, lifecycle = _bootstrap(path)
    candidate_values = {
        "tournament_id": field.tournament_id,
        "round_id": field.round_id,
        "field_id": field.field_id,
        "field_revision": field.field_revision,
        "assignments": field.ordered_assignments,
        "target_context": field.target_context,
        "historical_cutoff_key": field.historical_cutoff_key,
        "tournament_epoch_id": field.tournament_epoch_id,
        "tournament_event_sequence": field.tournament_event_sequence,
        "bundle_digest": field.bundle_digest,
        "evidence_digest": field.evidence_digest,
        "capacity_authority_digest": field.capacity_authority_digest,
        "max_field_entrants": field.max_field_entrants,
        "call_order": field.call_order,
        "scheduled_at": field.scheduled_at,
        "deadline_at": field.deadline_at,
    }
    with open_v3_connection(path) as connection:
        if mutation == "epoch":
            epoch_row = connection.execute(
                "SELECT epoch_json FROM v3_evidence_epochs WHERE epoch_id=?",
                (str(field.tournament_epoch_id),),
            ).fetchone()
            epoch = json.loads(str(epoch_row[0]))
            epoch["maximum_tournament_sequence"] += 1
            epoch_digest = canonical_digest(epoch)
            connection.execute(
                "UPDATE v3_evidence_epochs SET maximum_tournament_sequence=?, "
                "epoch_json=?, epoch_digest=? WHERE epoch_id=?",
                (
                    epoch["maximum_tournament_sequence"],
                    canonical_bytes(epoch).decode(),
                    epoch_digest,
                    str(field.tournament_epoch_id),
                ),
            )
            candidate_values["tournament_event_sequence"] = epoch["maximum_tournament_sequence"]
            candidate_values["evidence_digest"] = epoch_digest
        else:
            ingress = connection.execute(
                "SELECT snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' AND entity_id=?",
                (str(field.field_id),),
            ).fetchone()
            snapshot = json.loads(str(ingress[0]))
            if mutation == "roster":
                snapshot["competitor_ids"] = ["competitor:a", "competitor:b"]
                candidate_values["assignments"] = (
                    FrozenEntrantAssignment.create("competitor:a", "stand:left", 0),
                    FrozenEntrantAssignment.create("competitor:b", "stand:right", 1),
                )
            elif mutation == "stands":
                snapshot["stand_ids"] = ["stand:right", "stand:left"]
                candidate_values["assignments"] = (
                    FrozenEntrantAssignment.create("competitor:b", "stand:right", 0),
                    FrozenEntrantAssignment.create("competitor:a", "stand:left", 1),
                )
            elif mutation == "context":
                context = TargetContext(
                    event_code="underhand",
                    size_mm=300,
                    material_code="fir",
                    taxonomy_version="taxonomy:v1",
                    conversion_version="conversion:v1",
                )
                snapshot["target_context"] = context.to_dict()
                candidate_values["target_context"] = context
            elif mutation == "call_order":
                snapshot["call_order"] += 10
                candidate_values["call_order"] = snapshot["call_order"]
            else:
                snapshot["deadline_at"] = "2026-08-24T18:03:00.000Z"
                candidate_values["deadline_at"] = snapshot["deadline_at"]
            connection.execute(
                "UPDATE v3_ingress_snapshots SET snapshot_json=?, snapshot_digest=? "
                "WHERE entity_kind='field' AND entity_id=?",
                (
                    canonical_bytes(snapshot).decode(),
                    canonical_digest(snapshot),
                    str(field.field_id),
                ),
            )
        before_events = int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])
    candidate = FrozenFieldRevision.create(**candidate_values)
    provider_called = False

    def forbidden_provider(_field: FrozenFieldRevision) -> SealedPipelineOutput:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider work ran against unauthenticated U5 projection")

    with pytest.raises((AssemblyConflict, ProjectionError), match="U5|authority"):
        FieldAssemblyService(store).assemble(
            field=candidate,
            caller_namespace="manager",
            request_identity=f"idempotency:u5-source-{mutation}",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:00:01.000Z",
            build_pipeline=forbidden_provider,
        )

    assert provider_called is False
    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0]) == before_events
        )


def test_deleted_latest_u5_ingress_cannot_fall_back_to_authenticated_old_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "u5-latest-ingress.sqlite3"
    store, field, _build, lifecycle = _bootstrap(path)
    _ingest_field(lifecycle, 2)
    with open_v3_connection(path) as connection:
        connection.execute(
            "DELETE FROM v3_ingress_snapshots WHERE entity_kind='field' "
            "AND entity_id=? AND upstream_revision=2",
            (str(field.field_id),),
        )

    provider_called = False

    def forbidden_provider(_field: FrozenFieldRevision) -> SealedPipelineOutput:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider work ran against stale U5 ingress")

    with pytest.raises((AssemblyConflict, ProjectionError), match="U5|ingress"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:u5-stale-ingress",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:00:01.000Z",
            build_pipeline=forbidden_provider,
        )
    assert provider_called is False


def test_prior_receipt_tamper_after_prepare_is_rejected_inside_event_writer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "prior-in-writer-cas.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    service = FieldAssemblyService(store)
    service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:prior-cas-initial",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    _ingest_field(lifecycle, 2)
    field2 = _field(
        2,
        evidence_digest=field.evidence_digest,
        tournament_event_sequence=field.tournament_event_sequence,
        tournament_epoch_id=field.tournament_epoch_id,
    )
    pipeline2 = build(field2)
    original_execute = store._events.execute
    with open_v3_connection(path, read_only=True) as connection:
        before_events = int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])
        before_idempotency = int(
            connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0]
        )

    def tamper_before_writer(*args, **kwargs):
        with open_v3_connection(path) as connection:
            connection.execute(
                "UPDATE v3_field_receipts SET pipeline_digest=? "
                "WHERE field_id=? AND superseded_by_sequence IS NULL",
                ("e" * 64, str(field.field_id)),
            )
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(store._events, "execute", tamper_before_writer)
    with pytest.raises(ProjectionError, match="local authority"):
        service.assemble(
            field=field2,
            caller_namespace="manager",
            request_identity="idempotency:prior-cas-second",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:00:01.000Z",
            build_pipeline=lambda _field: pipeline2,
        )

    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0]) == before_events
        )
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0])
            == before_idempotency
        )


def test_u5_tamper_after_prepare_is_rejected_inside_event_writer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "u5-in-writer-cas.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    original_execute = store._events.execute
    with open_v3_connection(path, read_only=True) as connection:
        before_events = int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])
        before_idempotency = int(
            connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0]
        )

    def tamper_before_writer(*args, **kwargs):
        with open_v3_connection(path) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' AND entity_id=?",
                (str(field.field_id),),
            ).fetchone()
            snapshot = json.loads(str(row[0]))
            snapshot["call_order"] += 1
            connection.execute(
                "UPDATE v3_ingress_snapshots SET snapshot_json=?, snapshot_digest=? "
                "WHERE entity_kind='field' AND entity_id=?",
                (
                    canonical_bytes(snapshot).decode(),
                    canonical_digest(snapshot),
                    str(field.field_id),
                ),
            )
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(store._events, "execute", tamper_before_writer)
    with pytest.raises((AssemblyConflict, ProjectionError), match="U5|ingress"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:u5-in-writer-cas",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=lambda _field: pipeline,
        )

    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0]) == before_events
        )
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0])
            == before_idempotency
        )


@pytest.mark.parametrize(
    "tamper",
    ("ingress_neighbor", "tournament_open_idempotency"),
)
def test_u5_event_authority_tamper_after_prepare_emits_no_event(
    tmp_path: Path,
    monkeypatch,
    tamper: str,
) -> None:
    path = tmp_path / f"u5-event-cas-{tamper}.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    pipeline = build(field)
    original_execute = store._events.execute
    with open_v3_connection(path, read_only=True) as connection:
        before_events = int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])
        before_idempotency = int(
            connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0]
        )

    def tamper_before_writer(*args, **kwargs):
        with open_v3_connection(path) as connection:
            if tamper == "ingress_neighbor":
                source = int(
                    connection.execute(
                        "SELECT source_global_sequence FROM v3_ingress_snapshots "
                        "WHERE entity_kind='field' AND entity_id=?",
                        (str(field.field_id),),
                    ).fetchone()[0]
                )
                connection.execute("DROP TRIGGER v3_events_no_update")
                connection.execute(
                    "UPDATE v3_events SET event_digest=? WHERE global_sequence=?",
                    ("f" * 64, source - 1),
                )
            else:
                opened = connection.execute(
                    "SELECT command_id FROM v3_events WHERE aggregate_id=? AND event_kind=?",
                    (str(field.tournament_id), EventKind.TOURNAMENT_OPENED.value),
                ).fetchone()
                connection.execute("DROP TRIGGER v3_idempotency_records_no_update")
                connection.execute(
                    "UPDATE v3_idempotency_records SET command_digest=? WHERE idempotency_key=?",
                    ("f" * 64, str(opened[0])),
                )
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(store._events, "execute", tamper_before_writer)
    with pytest.raises((AssemblyConflict, ProjectionError), match="U5|authority"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity=f"idempotency:u5-event-cas-{tamper}",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=lambda _field: pipeline,
        )

    with open_v3_connection(path, read_only=True) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0]) == before_events
        )
        assert (
            int(connection.execute("SELECT COUNT(*) FROM v3_idempotency_records").fetchone()[0])
            == before_idempotency
        )


def test_changed_idempotent_retry_conflicts_and_untrusted_diagnostic_never_projects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    service = FieldAssemblyService(store)
    service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:one",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    with pytest.raises(AssemblyConflict, match="different material"):
        service.assemble(
            field=_field(
                2,
                evidence_digest=field.evidence_digest,
                tournament_event_sequence=field.tournament_event_sequence,
                tournament_epoch_id=field.tournament_epoch_id,
            ),
            caller_namespace="manager",
            request_identity="idempotency:one",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=build,
        )
    _ingest_field(lifecycle, 2)
    with pytest.raises(AssemblyConflict, match="untrusted"):
        service.assemble(
            field=_field(
                2,
                evidence_digest=field.evidence_digest,
                tournament_event_sequence=field.tournament_event_sequence,
                tournament_epoch_id=field.tournament_epoch_id,
            ),
            caller_namespace="manager",
            request_identity="idempotency:diagnostic",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=lambda _field: object(),
        )
    assert store.current_receipt("field:final").upstream_field_revision == 1


def test_closed_tournament_rejects_new_assembly_before_pipeline_loading(
    tmp_path: Path,
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, _build, lifecycle = _bootstrap(path)
    close = CommandEnvelope(
        CommandKind.CLOSE_TOURNAMENT,
        IdempotencyKey("command:close-before-assembly"),
        field.tournament_id,
        ((str(field.tournament_id), 2),),
        ACTOR,
        InlinePayload.from_value(
            {
                "schema_version": "strathmark-v3-tournament-close-v1",
                "deferred_reactions": ["cancel_jobs", "expire_overlay", "seal_exports"],
            }
        ),
    )
    SQLiteEventStore(path).execute(
        CommandRequest(
            ACTOR,
            close,
            (
                EventIntent(
                    AggregateKind.TOURNAMENT,
                    field.tournament_id,
                    EventKind.TOURNAMENT_CLOSED,
                ),
            ),
            "strathmark-v3-test-result-v1",
            {"closed": True},
            "2026-08-24T18:00:02.000Z",
            20,
        ),
        projection_hook=lifecycle.projections._register_rolling_reaction,
    )
    called = False

    def unavailable(_field: FrozenFieldRevision) -> SealedPipelineOutput:
        nonlocal called
        called = True
        raise AssertionError("closed tournament loaded a pipeline")

    with pytest.raises(AssemblyConflict, match="closed"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:closed-final",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:00:03.000Z",
            build_pipeline=unavailable,
        )
    assert called is False

    with open_v3_connection(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER v3_events_no_delete")
        connection.execute(
            "DELETE FROM v3_events WHERE aggregate_id=? AND event_kind=?",
            (str(field.tournament_id), EventKind.TOURNAMENT_CLOSED.value),
        )
    with pytest.raises((AssemblyConflict, ProjectionError), match="lifecycle|authority"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:deleted-close-final",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:00:04.000Z",
            build_pipeline=unavailable,
        )
    assert called is False


def test_concurrent_exact_retry_has_one_receipt_and_no_cross_namespace_disclosure(
    tmp_path: Path,
) -> None:
    store, field, build, _lifecycle = _bootstrap(tmp_path / "field.sqlite3")

    def assemble() -> bytes:
        return (
            FieldAssemblyService(store)
            .assemble(
                field=field,
                caller_namespace="manager",
                request_identity="idempotency:concurrent-final",
                actor_id="actor:manager",
                occurred_at=NOW,
                build_pipeline=build,
            )
            .canonical_bytes
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = tuple(executor.map(lambda _index: assemble(), range(2)))
    assert outputs[0] == outputs[1]
    assert (
        store.lookup_exact(
            caller_namespace="other-manager",
            request_identity="idempotency:concurrent-final",
            field_revision_digest=field.revision_digest,
        )
        is None
    )


def test_self_digested_weight_candidate_cannot_mint_operational_authority(
    tmp_path: Path,
) -> None:
    store, field, _build, _lifecycle = _bootstrap(tmp_path / "field.sqlite3")
    forged = WeightAuthorityBinding.pending(
        _weight_receipt(),
        ledger_projection_digest="f" * 64,
        tournament_event_sequence=field.tournament_event_sequence,
        source_global_sequence=field.tournament_event_sequence,
    )
    with pytest.raises(ProjectionConflict, match="U12"):
        store.install_weight_authority(forged, installed_at=NOW)


def test_ingress_change_during_pipeline_build_rejects_stale_atomic_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)

    def scratch_during_build(revision: FrozenFieldRevision) -> SealedPipelineOutput:
        pipeline = build(revision)
        _ingest_field(lifecycle, 2)
        return pipeline

    with pytest.raises(AssemblyConflict, match="current U5 ingress"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:stale-during-build",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=scratch_during_build,
        )
    assert store.current_receipt("field:final") is None


def test_current_approval_facts_turn_stale_immediately_on_new_u5_ingress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, build, lifecycle = _bootstrap(path)
    receipt = (
        FieldAssemblyService(store)
        .assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:approval-freshness",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=build,
        )
        .receipt
    )
    assert store.approval_facts(str(receipt.receipt_id)).freshness is FreshnessState.CURRENT
    _ingest_field(lifecycle, 2)
    assert store.approval_facts(str(receipt.receipt_id)).freshness is FreshnessState.STALE


def test_twelve_entrant_receipt_uses_required_content_addressed_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path, competitor_count=12)
    expected_pipeline = build(field)
    assert expected_pipeline.disagreement is not None
    compact_authority = expected_pipeline.disagreement.to_authority_dict()
    assert (
        compact_authority["schema_version"] == "strathmark-v3-operational-disagreement-authority-v3"
    )
    assert len(compact_authority["component_common_random_plan"]["rows"]) == 12
    assert all(len(row) == 2 for row in compact_authority["component_common_random_plan"]["rows"])
    assert all(
        "common_uniforms" not in competitor
        for _source, draws in compact_authority["component_joint_draws"]
        for competitor in draws["competitors"]
    )
    assert all(
        "samples_ms" not in competitor
        for _source, draws in compact_authority["component_joint_draws"]
        for competitor in draws["competitors"]
    )
    assert (
        OperationalDisagreementReceipt.from_authority_dict(compact_authority)
        == expected_pipeline.disagreement
    )
    assert len(expected_pipeline.disagreement.canonical_authority_payload) < 1_850_000
    assert expected_pipeline.disagreement.canonical_authority_payload == canonical_bytes(
        compact_authority,
        max_bytes=16_777_216,
        max_items=2_000_000,
    )
    disagreement_type = type(expected_pipeline.disagreement)
    original_authority_dict = disagreement_type.to_authority_dict
    authority_serializations = 0

    def tracked_authority_dict(authority):
        nonlocal authority_serializations
        authority_serializations += 1
        return original_authority_dict(authority)

    monkeypatch.setattr(disagreement_type, "to_authority_dict", tracked_authority_dict)

    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:twelve-entrant-blob",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: expected_pipeline,
    )
    assert authority_serializations == 1

    with open_v3_connection(path, read_only=True) as connection:
        row = connection.execute(
            "SELECT receipt_json, envelope_json FROM v3_field_receipts "
            "JOIN v3_events ON global_sequence=source_global_sequence "
            "WHERE receipt_id=?",
            (str(result.receipt.receipt_id),),
        ).fetchone()
    assert row is not None
    projection = json.loads(str(row[0]))
    event = EventEnvelope.from_dict(json.loads(str(row[1])))
    event_payload = event.command.payload.to_value()
    assert len(str(row[0]).encode()) <= 65_536
    assert "sections" not in projection
    assert "receipt" not in event_payload
    assert projection["receipt_blob_reference"] == event_payload["receipt_blob_reference"]
    assert projection["receipt_summary"]["content_digest"] == result.receipt.content_digest

    restarted = SQLiteFieldProjectionStore(
        path, signer=store._signer, trust_store=store._trust_store
    )
    assert restarted.receipt(str(result.receipt.receipt_id)) == result.receipt

    reference = BlobReferenceV2.from_dict(projection["receipt_blob_reference"])
    store._blob_store.path_for(reference.digest).unlink()
    with pytest.raises(ProjectionError, match="receipt blob"):
        SQLiteFieldProjectionStore(path, signer=store._signer, trust_store=store._trust_store)


def test_field_receipt_core_obeys_receipt_capacity_outside_and_inside_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capacity = replace(_capacity_manifest(), max_receipt_bytes=1)
    path = tmp_path / "field.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path, capacity_manifest=capacity)
    service = FieldAssemblyService(store)

    with pytest.raises(ProjectionConflict, match="receipt capacity"):
        service.assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:receipt-capacity-outside",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=build,
        )

    original = store.verify_capacity_authority

    def outside_lies(*args, **kwargs):
        installed = original(*args, **kwargs)
        if kwargs.get("_connection") is None:
            return SimpleNamespace(
                capacity=replace(installed.capacity, max_receipt_bytes=16_777_216)
            )
        return installed

    monkeypatch.setattr(store, "verify_capacity_authority", outside_lies)
    with pytest.raises(ProjectionConflict, match="receipt capacity"):
        service.assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:receipt-capacity-atomic",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=build,
        )

    with open_v3_connection(path, read_only=True) as connection:
        receipt_count = connection.execute("SELECT COUNT(*) FROM v3_field_receipts").fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM v3_events WHERE event_kind=?",
            (EventKind.FIELD_OPTIMIZED.value,),
        ).fetchone()
        assert receipt_count is not None and int(receipt_count[0]) == 0
        assert event_count is not None and int(event_count[0]) == 0


def test_restart_deep_verifies_each_disagreement_authority_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "field.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    result = FieldAssemblyService(store).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:restart-single-deep-verify",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    disagreement_digest = next(
        section.payload.to_value()["operational_receipt"]["receipt_digest"]
        for section in result.receipt.sections
        if section.kind is ReceiptSectionKind.DISAGREEMENT
    )
    original = SQLiteFieldProjectionStore._resolve_disagreement_connection
    calls = 0

    def tracked(self, connection, receipt_digest):
        nonlocal calls
        if receipt_digest == disagreement_digest:
            calls += 1
        return original(self, connection, receipt_digest)

    monkeypatch.setattr(SQLiteFieldProjectionStore, "_resolve_disagreement_connection", tracked)
    SQLiteFieldProjectionStore(path, signer=store._signer, trust_store=store._trust_store)

    assert calls == 1


def test_exact_retry_lookup_microbenchmark_repeated_windows_cadence(
    tmp_path: Path,
) -> None:
    store, field, build, _lifecycle = _bootstrap(tmp_path / "field.sqlite3")
    service = FieldAssemblyService(store)
    expected = service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:benchmark-final",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    ).canonical_bytes

    def unavailable(_field: FrozenFieldRevision) -> SealedPipelineOutput:
        raise AssertionError("cached benchmark loaded a provider")

    rss_before = _process_rss_bytes()
    durations = []
    for _index in range(100):
        started = perf_counter_ns()
        result = service.assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:benchmark-final",
            actor_id="actor:manager",
            occurred_at=NOW,
            build_pipeline=unavailable,
        )
        durations.append((perf_counter_ns() - started) / 1_000_000)
        assert result.canonical_bytes == expected
    rss_after = _process_rss_bytes()
    ordered = sorted(durations)
    metrics = {
        "runs": len(ordered),
        "p50_ms": round(median(ordered), 3),
        "p95_ms": round(ordered[94], 3),
        "p99_ms": round(ordered[98], 3),
        "worst_ms": round(ordered[-1], 3),
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "rss_growth_bytes": max(0, rss_after - rss_before),
    }
    print("U15_BENCHMARK=" + json.dumps(metrics, sort_keys=True))
    assert metrics["p99_ms"] <= 250
    assert metrics["worst_ms"] <= 250


def test_exact_retry_decodes_verified_receipt_blob_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, field, build, _lifecycle = _bootstrap(tmp_path / "decode-once.sqlite3")
    service = FieldAssemblyService(store)
    service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:decode-once",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=build,
    )
    original = store._decode_receipt
    calls = 0

    def tracked(row):
        nonlocal calls
        calls += 1
        return original(row)

    monkeypatch.setattr(store, "_decode_receipt", tracked)
    service.assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:decode-once",
        actor_id="actor:manager",
        occurred_at=NOW,
        build_pipeline=lambda _field: pytest.fail("exact retry loaded provider"),
    )

    assert calls == 1


def _process_rss_bytes() -> int:
    """Read process RSS from the host OS without Python allocation tracing."""

    if sys.platform == "win32":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = (
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            )

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        process = get_current_process()
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        )
        get_process_memory_info.restype = ctypes.c_int
        if not get_process_memory_info(
            process,
            ctypes.byref(counters),
            counters.cb,
        ):
            raise OSError("Windows could not report process RSS")
        return int(counters.WorkingSetSize)
    import resource

    maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum_rss if sys.platform == "darwin" else maximum_rss * 1024)


def test_field_assembly_rejects_bare_pipeline_from_configured_production_builder(
    tmp_path: Path,
) -> None:
    path = tmp_path / "configured-pipeline-builder.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    built: list[str] = []

    def configured(revision: FrozenFieldRevision) -> SealedPipelineOutput:
        built.append(revision.revision_digest)
        return build(revision)

    with pytest.raises(
        AssemblyConflict, match="configured builder did not return rolling authority"
    ):
        FieldAssemblyService(store, pipeline_builder=configured).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:configured-pipeline-builder",
            actor_id="actor:manager",
            occurred_at=NOW,
        )

    assert built == [field.revision_digest]


def test_field_assembly_fails_closed_without_configured_or_explicit_pipeline_builder(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-pipeline-builder.sqlite3"
    store, field, _build, _lifecycle = _bootstrap(path)

    with pytest.raises(AssemblyError, match="pipeline builder is not configured"):
        FieldAssemblyService(store).assemble(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:missing-pipeline-builder",
            actor_id="actor:manager",
            occurred_at=NOW,
        )


def test_production_rolling_builder_uses_current_publications_and_original_cards(
    tmp_path: Path,
) -> None:
    from strathmark.v3.application.pipeline_builder import (
        RollingCapabilityAuthority,
        RollingCurrentCard,
        RollingFieldBuildInputs,
        RollingFieldPipelineBuilder,
    )
    from strathmark.v3.domain.capability import CapabilityState
    from strathmark.v3.domain.disagreement import AcceptedExpectedTimeOverrideState

    path = tmp_path / "production-rolling-builder.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    baseline = build(field)
    signer = store._signer
    cards = tuple(
        RollingCurrentCard(
            pool.card,
            *_test_rolling_publication_material(
                field,
                pool.card,
                dependency_revision=max(1, field.tournament_event_sequence),
                signer=signer,
            ),
        )
        for pool in baseline.pools
    )
    capabilities = []
    for index, assignment in enumerate(field.ordered_assignments):
        value = _capability(assignment.competitor_id, 40_000 + index * 10_000).to_dict()
        value["context_digest"] = field.target_context.digest
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
    policy = DisagreementPolicy(
        "disagreement:v1",
        2_000,
        20_000,
        5_000,
        20_000,
        1,
        2,
        "0.2",
        "0.49",
        10_000,
        50_000,
        "6" * 64,
        "7" * 64,
    )
    inputs = RollingFieldBuildInputs(
        cards,
        WeightReceipt(
            baseline.weight_authority.context,
            baseline.weight_authority.weights,
            (),
            baseline.weight_authority.calibration_cutoff_at_utc,
            baseline.weight_authority.policy_digest,
            baseline.weight_authority.weight_receipt_digest,
        ),
        baseline.operational_weight_authority,
        baseline.dependence_artifact,
        tuple(capabilities),
        policy,
        override_states=(
            AcceptedExpectedTimeOverrideState.create(
                override_id=StableIdentifier("override:rolling-start"),
                competitor_id=field.ordered_assignments[0].competitor_id,
                tournament_id=field.tournament_id,
                target_context_digest=field.target_context.digest,
                expected_raw_time_ms=35_000,
                scope=OverrideScope.REMAINING_TOURNAMENT,
                scope_boundary_id=field.tournament_id,
                accepted_field_id=field.field_id,
                accepted_round_id=field.round_id,
                accepted_call_order=field.call_order,
                accepted_capability_revision=capabilities[0].state.state_revision,
                actor="actor:judge",
                reason="accepted starting estimate",
                supersedes_override_id=None,
                override_receipt_digest="8" * 64,
                accepted_global_sequence=1,
                accepted_event_digest="9" * 64,
            ),
        ),
    )

    class CurrentSource:
        def __init__(self) -> None:
            self.verifications = 0

        def load_current(self, revision: FrozenFieldRevision):
            assert revision == field
            return inputs

        def verify_current(
            self, revision, publications, capability_bindings, override_states
        ) -> None:
            assert revision == field
            assert publications == tuple(item.publication for item in cards)
            assert capability_bindings == tuple(item.binding for item in capabilities)
            assert override_states == inputs.override_states
            self.verifications += 1

    source = CurrentSource()
    monotonic_values = iter((1_000_000_000, 1_012_000_000))
    rolling_build = RollingFieldPipelineBuilder(
        source,
        signer=signer,
        trust_store=store._trust_store,
        clock=lambda: NOW,
        monotonic_ns=lambda: next(monotonic_values),
    )(field)
    pipeline = rolling_build.pipeline

    assert source.verifications == 2
    assert pipeline.rolling_publications == tuple(item.publication for item in cards)
    assert tuple(item.card for item in pipeline.pools) == tuple(item.card for item in cards)
    assert pipeline.disagreement is not None
    assert pipeline.prediction_evidence[0].distribution.median_ms == 35_000
    assert pipeline.total_latency_ms == 12
    assert tuple(source for source, _receipt in pipeline.disagreement.component_optimizers) == (
        AssessorKind.FORMULA,
        AssessorKind.ML,
        AssessorKind.LLM_COUNCIL,
    )


def test_production_rolling_builder_uses_signed_formula_prior_for_zero_history(
    tmp_path: Path,
) -> None:
    from strathmark.v3.application.capacity import JobKind
    from strathmark.v3.application.coordinator import (
        RollingComponentOutcome,
        RollingComponentReceipt,
    )
    from strathmark.v3.application.field_assembly import ZeroHistoryPriorBasis
    from strathmark.v3.application.pipeline_builder import (
        RollingCapabilityAuthority,
        RollingCurrentCard,
        RollingFieldBuildInputs,
        RollingFieldPipelineBuilder,
    )
    from strathmark.v3.assessors.formula import (
        FormulaManifest,
        resolve_zero_history_prior,
    )
    from strathmark.v3.contracts.forecasts import ArtifactIdentity, ForecastWarning
    from strathmark.v3.domain.capability import CapabilityState
    from strathmark.v3.domain.disagreement import ZeroHistoryPolicy

    path = tmp_path / "production-zero-history-builder.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    baseline = build(field)
    signer = store._signer
    manifest = FormulaManifest.load("benchmarks/v3/formula_manifest.json")
    resolved = resolve_zero_history_prior(field.target_context, manifest)
    zero_id = field.ordered_assignments[0].competitor_id
    cards = []
    capabilities = []
    for index, pool in enumerate(baseline.pools):
        card = pool.card
        components = None
        if card.competitor_id == zero_id:
            formula = AssessorForecast.create(
                forecast_id=card.forecasts[0].forecast_id,
                assessor=AssessorKind.FORMULA,
                state=ForecastState.COMMITTED,
                evidence_digest=card.forecasts[0].evidence_digest,
                distribution=resolved.distribution,
                support=EvidenceSupport(
                    0,
                    "0",
                    0,
                    str(field.historical_cutoff_key),
                    field.tournament_event_sequence,
                ),
                warnings=(ForecastWarning.PRIOR_ONLY,),
                artifacts=(
                    ArtifactIdentity("formula_manifest", manifest.version, manifest.digest),
                ),
                abstention_code=None,
            )
            card = seal_competitor_card_authority(
                card.evidence_packet,
                (formula, *card.forecasts[1:]),
                bundle_digest=card.bundle_digest,
                signer=signer,
                created_at=NOW,
            )
            components = (
                RollingComponentReceipt(
                    "formula",
                    1,
                    "job:zero-formula",
                    1,
                    JobKind.FORMULA_CARD,
                    RollingComponentOutcome.SUCCEEDED,
                    formula.commit_digest,
                    None,
                    1,
                    "1" * 64,
                ),
            )
        binding, publication_manifest, aggregate = _test_rolling_publication_material(
            field,
            card,
            dependency_revision=max(1, field.tournament_event_sequence),
            signer=signer,
            components=components,
        )
        cards.append(
            RollingCurrentCard(
                card,
                binding,
                publication_manifest,
                aggregate,
                () if components is None else components,
                () if components is None else binding.availability,
            )
        )
        if card.competitor_id != zero_id:
            value = _capability(card.competitor_id, 50_000 + index * 10_000).to_dict()
            value["context_digest"] = field.target_context.digest
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
                        aggregate_event_digest=canonical_digest(state.to_dict()),
                    ),
                )
            )
    policy = ZeroHistoryPolicy("0.05", "0.95", 10_000, "zero-history:v1")
    inputs = RollingFieldBuildInputs(
        tuple(cards),
        WeightReceipt(
            baseline.weight_authority.context,
            baseline.weight_authority.weights,
            (),
            baseline.weight_authority.calibration_cutoff_at_utc,
            baseline.weight_authority.policy_digest,
            baseline.weight_authority.weight_receipt_digest,
        ),
        baseline.operational_weight_authority,
        baseline.dependence_artifact,
        tuple(capabilities),
        baseline.disagreement.decision.policy,
        manifest,
        policy,
    )

    class Source:
        def load_current(self, _revision):
            return inputs

        def verify_current(self, _revision, publications, capability_bindings, override_states):
            assert publications == tuple(item.publication for item in cards)
            assert capability_bindings == tuple(item.binding for item in capabilities)
            assert override_states == ()

    pipeline = RollingFieldPipelineBuilder(
        Source(),
        signer=signer,
        trust_store=store._trust_store,
        clock=lambda: NOW,
    )(field).pipeline

    evidence = next(item for item in pipeline.prediction_evidence if item.competitor_id == zero_id)
    assert isinstance(evidence.basis, ZeroHistoryPriorBasis)
    assert evidence.basis.distribution == resolved.distribution
    assert evidence.basis.formula_manifest_digest == manifest.digest
    assert pipeline.zero_history_competitors == (zero_id,)
    assert pipeline.disagreement is None


def test_production_rolling_builder_returns_signed_manual_action_before_draw_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from strathmark.v3.application.manual_actions import (
        ManualActionKind,
        ManualActionRequirement,
    )
    from strathmark.v3.application.pipeline_builder import (
        RollingCapabilityAuthority,
        RollingCurrentCard,
        RollingFieldBuildInputs,
        RollingFieldPipelineBuilder,
    )
    from strathmark.v3.domain.capability import CapabilityState

    path = tmp_path / "production-manual-action-builder.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path, available_assessors=(AssessorKind.FORMULA,))
    baseline = build(field)
    signer = store._signer
    cards = tuple(
        RollingCurrentCard(
            evidence.card,
            *_test_rolling_publication_material(
                field,
                evidence.card,
                dependency_revision=max(1, field.tournament_event_sequence),
                signer=signer,
            ),
        )
        for evidence in baseline.prediction_evidence
    )
    capabilities = []
    for index, assignment in enumerate(field.ordered_assignments):
        value = _capability(assignment.competitor_id, 40_000 + index * 10_000).to_dict()
        value["context_digest"] = field.target_context.digest
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
                    aggregate_event_digest=canonical_digest(state.to_dict()),
                ),
            )
        )
    inputs = RollingFieldBuildInputs(
        cards,
        WeightReceipt(
            baseline.weight_authority.context,
            baseline.weight_authority.weights,
            (),
            baseline.weight_authority.calibration_cutoff_at_utc,
            baseline.weight_authority.policy_digest,
            baseline.weight_authority.weight_receipt_digest,
        ),
        baseline.operational_weight_authority,
        baseline.dependence_artifact,
        tuple(capabilities),
        DisagreementPolicy(
            "disagreement:v1",
            2_000,
            20_000,
            5_000,
            20_000,
            1,
            2,
            "0.2",
            "0.49",
            10_000,
            50_000,
            "6" * 64,
            "7" * 64,
        ),
    )

    class Source:
        def load_current(self, _revision):
            return inputs

        def verify_current(self, _revision, publications, capability_bindings, override_states):
            assert publications == tuple(item.publication for item in cards)
            assert capability_bindings == tuple(item.binding for item in capabilities)
            assert override_states == ()

    monkeypatch.setattr(
        "strathmark.v3.application.pipeline_builder.generate_joint_uniforms",
        lambda *_args, **_kwargs: pytest.fail(
            "manual-action routing must happen before draw generation"
        ),
    )
    requirement = RollingFieldPipelineBuilder(
        Source(),
        signer=signer,
        trust_store=store._trust_store,
        clock=lambda: field.deadline_at,
    )(field)

    assert isinstance(requirement, ManualActionRequirement)
    assert requirement.action is ManualActionKind.ACCEPT_SINGLE_SURVIVOR
    assert all(item.candidate_basis_digest for item in requirement.entrants)
    assert tuple(item.available_assessors for item in requirement.entrants) == (
        (AssessorKind.FORMULA,),
    ) * len(cards)

    from strathmark.v3.infrastructure.sqlite.manual_actions import (
        SQLiteManualActionRequirementStore,
    )

    action_store = SQLiteManualActionRequirementStore(
        path, signer=signer, trust_store=store._trust_store
    )
    returned = FieldAssemblyService(
        store,
        pipeline_builder=lambda _field: requirement,
        manual_action_store=action_store,
    ).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:manual-action-required",
        actor_id="actor:manager",
        occurred_at=field.deadline_at,
    )
    assert returned == requirement
    assert action_store.current(field.field_id) == requirement
    recovered = FieldAssemblyService(
        store,
        pipeline_builder=lambda _field: pytest.fail(
            "sealed manual-action retry must not rebuild providers or candidates"
        ),
        manual_action_store=action_store,
    ).assemble(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:manual-action-required-retry",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:03:00.000Z",
    )
    assert recovered == requirement
    assert baseline.manual_authority is not None
    submission = ManualConstructionSubmission.create(
        prior_receipt_id=None,
        prior_receipt_digest=None,
        upstream_field_revision=field.field_revision,
        field_revision_digest=field.revision_digest,
        manual_authority_digest=baseline.manual_authority.authority_digest,
        actor_id=ACTOR,
        reason_code="judge_single_survivor_acceptance",
        scope=OverrideScope.UPCOMING_RACE,
        submitted_at="2026-08-24T18:03:01.000Z",
    )
    constructed_pipeline = _pipeline_with_supersession(baseline, construction_submission=submission)
    resolve_connection = action_store.resolve_connection

    def fail_after_resolution(connection, binding, resolution):
        resolve_connection(connection, binding, resolution)
        raise RuntimeError("fault after manual-action resolution")

    monkeypatch.setattr(action_store, "resolve_connection", fail_after_resolution)
    with pytest.raises(RuntimeError, match="fault after manual-action resolution"):
        FieldAssemblyService(store, manual_action_store=action_store).submit_construction(
            field=field,
            caller_namespace="manager",
            request_identity="idempotency:accept-single-survivor-fault",
            actor_id="actor:manager",
            occurred_at="2026-08-24T18:03:01.000Z",
            build_pipeline=lambda _field: constructed_pipeline,
            manual_action_binding=requirement.binding,
        )
    assert action_store.current(field.field_id) == requirement
    with open_v3_connection(path, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_field_receipts").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM v3_manual_action_resolutions").fetchone()[0]
            == 0
        )
    monkeypatch.setattr(action_store, "resolve_connection", resolve_connection)
    accepted = FieldAssemblyService(store, manual_action_store=action_store).submit_construction(
        field=field,
        caller_namespace="manager",
        request_identity="idempotency:accept-single-survivor",
        actor_id="actor:manager",
        occurred_at="2026-08-24T18:03:01.000Z",
        build_pipeline=lambda _field: constructed_pipeline,
        manual_action_binding=requirement.binding,
    )
    assert accepted.receipt.receipt_revision == 1
    assert action_store.current(field.field_id) is None


def test_production_rolling_builder_rejects_stale_or_mixed_publications_before_pooling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from strathmark.v3.application.pipeline_builder import (
        RollingCapabilityAuthority,
        RollingCurrentCard,
        RollingFieldBuildInputs,
        RollingFieldPipelineBuilder,
    )

    path = tmp_path / "stale-rolling-builder.sqlite3"
    store, field, build, _lifecycle = _bootstrap(path)
    baseline = build(field)
    signer = store._signer
    first = baseline.pools[0]
    binding, manifest, aggregate = _test_rolling_publication_material(
        field,
        first.card,
        dependency_revision=max(1, field.tournament_event_sequence),
        signer=signer,
    )
    current = RollingCurrentCard(first.card, binding, manifest, aggregate)
    inputs = RollingFieldBuildInputs(
        (current,),
        WeightReceipt(
            baseline.weight_authority.context,
            baseline.weight_authority.weights,
            (),
            baseline.weight_authority.calibration_cutoff_at_utc,
            baseline.weight_authority.policy_digest,
            baseline.weight_authority.weight_receipt_digest,
        ),
        baseline.operational_weight_authority,
        baseline.dependence_artifact,
        (
            RollingCapabilityAuthority(
                _capability(first.competitor_id, 40_000),
                RollingCapabilityBinding.create(
                    competitor_id=first.competitor_id,
                    context_digest="c" * 64,
                    state_revision=1,
                    state_digest=_capability(first.competitor_id, 40_000).state_digest,
                    aggregate_version=1,
                    aggregate_event_digest="a" * 64,
                ),
            ),
        ),
        baseline.disagreement.decision.policy,
    )

    class MissingSource:
        def load_current(self, _revision):
            return inputs

        def verify_current(
            self, _revision, _publications, _capability_bindings, _override_states
        ) -> None:
            raise AssertionError("missing roster must fail before currentness hook")

    def forbidden_pool(*_args, **_kwargs):
        raise AssertionError("stale rolling cards must fail before pooling")

    monkeypatch.setattr(
        "strathmark.v3.application.pipeline_builder.pool_forecasts",
        forbidden_pool,
    )
    with pytest.raises(AssemblyConflict, match="publication roster differs"):
        RollingFieldPipelineBuilder(
            MissingSource(),
            signer=signer,
            trust_store=store._trust_store,
            clock=lambda: NOW,
        )(field)
