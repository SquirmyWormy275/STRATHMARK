from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from strathmark.v3.application.formula_governor import (
    FormulaHistoricalAuthority,
    FormulaLiveAuthority,
    FormulaProjectionFactory,
    HistoricalCutoverAuthority,
    seal_formula_governor_batch,
)
from strathmark.v3.assessors.base import (
    ArithmeticTraceRow,
    AssessmentResult,
    EvidenceOrigin,
    EvidenceQuality,
    FormulaGovernorReceipt,
    FormulaInputPacket,
    FormulaObservationFacts,
    FormulaObservationProvenance,
    ReviewClassification,
    TournamentRelevance,
    _derive_formula_facts,
)
from strathmark.v3.assessors.formula import (
    ContextPrior,
    DisciplinePrior,
    FormulaManifest,
    FormulaZeroHistoryPrior,
    _closed_mapping,
    _huber_weight,
    _irls,
    _mapping_table,
    _validate_table,
    _weighted_median,
    assess_formula,
    resolve_zero_history_prior,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.evidence import (
    ContextProperty,
    EvidencePacket,
    ResultObservation,
    TargetContext,
)
from strathmark.v3.contracts.forecasts import AssessorKind, ForecastState, ForecastWarning
from strathmark.v3.contracts.identifiers import StableIdentifier, deterministic_identifier
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.epochs import (
    EpochMember,
    EvidenceEpoch,
    ReactionBarrier,
    freeze_epoch,
)
from strathmark.v3.domain.evidence import AdmissionReason, EvidenceSource, IssuedFieldFact
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner

MANIFEST_PATH = Path("benchmarks/v3/formula_manifest.json")
_FORMULA_TEST_SIGNER = P256EphemeralSigner.generate("integrity-key:formula-test")
_FORMULA_TEST_TRUST = IntegrityTrustStore((_FORMULA_TEST_SIGNER.identity,))


def context(
    event: str = "underhand",
    size: int = 300,
    material: str = "eucalyptus",
    density: str | None = "720",
) -> TargetContext:
    return TargetContext(
        event,
        size,
        material,
        "taxonomy:v1",
        "conversion:v1",
        (ContextProperty("density", density, "kg_m3", None if density else "not_observed"),),
    )


def observation(
    sequence: int,
    raw_ms: int,
    *,
    observed_context: TargetContext | None = None,
    status: ResultStatus = ResultStatus.COMPLETION,
    occurred_at_utc: str | None = None,
    tournament: str = "show-a",
) -> ResultObservation:
    return ResultObservation(
        StableIdentifier(f"evidence:result-{sequence}"),
        StableIdentifier("competitor:opaque-a"),
        StableIdentifier(f"tournament:{tournament}"),
        StableIdentifier("round:heat"),
        StableIdentifier(f"field:field-{sequence}"),
        observed_context or context(),
        sequence,
        occurred_at_utc or f"2026-08-{min(sequence, 28):02d}T12:00:00.000Z",
        3,
        None,
        None,
        None,
        OfficialResult(
            status, raw_ms if status is ResultStatus.COMPLETION else None, None, 1, None
        ),
        f"{sequence:064x}",
    )


def evidence(
    *observations: ResultObservation, target: TargetContext | None = None
) -> EvidencePacket:
    tournament_sequence = max((item.observation_sequence for item in observations), default=0)
    epoch = _epoch_for_observations(tuple(observations), tournament_sequence)
    return EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:opaque-a"),
        target_context=target or context(),
        observations=tuple(observations),
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:2026-08-01",
        tournament_epoch_id=epoch.epoch_id,
        tournament_event_sequence=tournament_sequence,
    )


def _result_key(observation: ResultObservation) -> StableIdentifier:
    return deterministic_identifier(
        "result",
        {
            "field_id": str(observation.field_id),
            "field_revision": 1,
            "competitor_id": str(observation.competitor_id),
        },
    )


def _epoch_for_observations(
    observations: tuple[ResultObservation, ...], maximum_sequence: int
) -> EvidenceEpoch:
    members = tuple(
        sorted(
            (
                EpochMember(
                    str(_result_key(item)),
                    item.result.revision,
                    item.observation_sequence,
                    item.result.status is ResultStatus.COMPLETION,
                )
                for item in observations
            ),
            key=lambda item: item.result_key,
        )
    )
    return freeze_epoch(
        round_id=StableIdentifier("round:heat"),
        epoch_revision=1,
        historical_cutoff_key="history:2026-08-01",
        closed_through_sequence=maximum_sequence,
        members=members,
        barrier=ReactionBarrier.complete_through(maximum_sequence),
    )


def epoch_for(packet: EvidencePacket) -> EvidenceEpoch:
    epoch = _epoch_for_observations(packet.observations, packet.tournament_event_sequence)
    assert epoch.epoch_id == packet.tournament_epoch_id
    return epoch


def formula_input(
    packet: EvidencePacket,
    *,
    cutoff_at_utc: str = "2026-08-29T12:00:00.000Z",
    origins: tuple[EvidenceOrigin, ...] | None = None,
    active_tournament: str = "show-a",
    authoritative_tournaments: tuple[str, ...] = (),
    legacy_tournaments: tuple[str, ...] = (),
) -> FormulaInputPacket:
    selected_origins = origins or (EvidenceOrigin.ISSUED_RESULT_RECEIPT,) * len(packet.observations)
    live_authorities = []
    historical_authorities = []
    cutover = HistoricalCutoverAuthority(
        StableIdentifier("historical_cutover:formula-test"),
        packet.historical_cutoff_key,
        canonical_digest({"cutover": packet.historical_cutoff_key}),
    )
    for index, item in enumerate(packet.observations):
        if selected_origins[index] is EvidenceOrigin.ISSUED_RESULT_RECEIPT:
            receipt_id = StableIdentifier(f"receipt:formula-{item.observation_sequence}")
            issued = IssuedFieldFact(
                item.field_id,
                1,
                (item.competitor_id,),
                receipt_id,
                item.tournament_id,
                item.round_id,
                item.context,
                ((item.competitor_id, item.issued_mark),),
            )
            live_authorities.append(FormulaLiveAuthority(item.evidence_id, issued, 1, receipt_id))
        else:
            historical_authorities.append(
                FormulaHistoricalAuthority(item.evidence_id, _result_key(item), cutover)
            )
    active = StableIdentifier(f"tournament:{active_tournament}")
    authoritative = tuple(
        sorted(StableIdentifier(f"tournament:{item}") for item in authoritative_tournaments)
    )
    legacy = tuple(sorted(StableIdentifier(f"tournament:{item}") for item in legacy_tournaments))
    epoch = epoch_for(packet)
    sealed = seal_formula_governor_batch(
        evidence=packet,
        epoch=epoch,
        cutoff_at_utc=cutoff_at_utc,
        active_tournament_id=active,
        authoritative_tournament_ids=authoritative,
        legacy_tournament_ids=legacy,
        live_authorities=tuple(live_authorities),
        historical_authorities=tuple(historical_authorities),
        signer=_FORMULA_TEST_SIGNER,
        created_at=cutoff_at_utc,
    )
    factory = FormulaProjectionFactory(
        trust_store=_FORMULA_TEST_TRUST,
        cutoff_at_utc=cutoff_at_utc,
        active_tournament_id=active,
        authoritative_tournament_ids=authoritative,
        legacy_tournament_ids=legacy,
    )
    return factory.project(evidence=packet, epoch=epoch, sealed_batch=sealed)


def run(
    *observations: ResultObservation, target: TargetContext | None = None, **facts: object
) -> AssessmentResult:
    packet = evidence(*observations, target=target)
    return assess_formula(formula_input(packet, **facts), FormulaManifest.load(MANIFEST_PATH))  # type: ignore[arg-type]


def observation_row(result: AssessmentResult, sequence: int = 1) -> ArithmeticTraceRow:
    return next(
        row
        for row in result.trace
        if row.stage == "observation" and row.details["observation_sequence"] == str(sequence)
    )


def test_governor_contract_rejects_every_malformed_authority_shape() -> None:
    packet = evidence(observation(1, 40000))
    projected = formula_input(packet)
    receipt = projected.governor_receipt
    provenance = receipt.provenance[0]
    with pytest.raises(ValueError, match="evidence identifier"):
        FormulaObservationProvenance(
            StableIdentifier("competitor:not-evidence"),
            EvidenceSource.LIVE_ISSUED_RACE,
            AdmissionReason.ELIGIBLE_COMPLETION,
            True,
            "1" * 64,
        )
    with pytest.raises(ValueError, match="EvidenceSource"):
        FormulaObservationProvenance(
            provenance.evidence_id,
            "issued",  # type: ignore[arg-type]
            AdmissionReason.ELIGIBLE_COMPLETION,
            True,
            "1" * 64,
        )
    with pytest.raises(ValueError, match="AdmissionReason"):
        replace(provenance, admission_reason="eligible")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="numeric eligibility"):
        replace(provenance, numeric_eligible=1)  # type: ignore[arg-type]

    invalid_receipts = (
        ({"historical_cutoff_key": "not-history"}, "historical cutoff key"),
        ({"tournament_epoch_id": StableIdentifier("tournament:not-epoch")}, "epoch identifier"),
        ({"active_tournament_id": StableIdentifier("epoch:not-tournament")}, "active tournament"),
        (
            {"authoritative_tournament_ids": [StableIdentifier("tournament:a")]},
            "immutable typed tuple",
        ),
        (
            {
                "authoritative_tournament_ids": (
                    StableIdentifier("tournament:z"),
                    StableIdentifier("tournament:a"),
                )
            },
            "unique and sorted",
        ),
        (
            {"authoritative_tournament_ids": (receipt.active_tournament_id,)},
            "authority sets must be disjoint",
        ),
        ({"provenance": [provenance]}, "provenance must be an immutable"),
        ({"provenance": (provenance, provenance)}, "identifiers must be unique"),
    )
    for changes, message in invalid_receipts:
        values = {
            "evidence_digest": receipt.evidence_digest,
            "historical_cutoff_key": receipt.historical_cutoff_key,
            "tournament_epoch_id": receipt.tournament_epoch_id,
            "tournament_epoch_content_digest": receipt.tournament_epoch_content_digest,
            "cutoff_at_utc": receipt.cutoff_at_utc,
            "active_tournament_id": receipt.active_tournament_id,
            "authoritative_tournament_ids": receipt.authoritative_tournament_ids,
            "legacy_tournament_ids": receipt.legacy_tournament_ids,
            "provenance": receipt.provenance,
            "signed_manifest_body_digest": receipt.signed_manifest_body_digest,
            "signer_key_id": receipt.signer_key_id,
            **changes,
        }
        with pytest.raises(ValueError, match=message):
            FormulaGovernorReceipt(**values, receipt_digest="0" * 64)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="verified governor projection"):
        FormulaInputPacket(packet, "caller-authored", projected.observation_facts)  # type: ignore[arg-type]


def test_governor_receipt_binding_rejects_each_sealed_scope_mismatch() -> None:
    receipt = formula_input(evidence(observation(1, 40000))).governor_receipt
    for changes in (
        {"evidence_digest": "f" * 64},
        {"historical_cutoff_key": "history:other"},
        {"tournament_epoch_id": StableIdentifier("epoch:other")},
        {"provenance": ()},
    ):
        with pytest.raises(ValueError, match="receipt digest mismatch"):
            replace(receipt, **changes)


def test_prior_and_manifest_contracts_reject_every_malformed_hierarchy_shape() -> None:
    manifest = FormulaManifest.load(MANIFEST_PATH)
    context_prior = manifest.context_priors[0]
    discipline_prior = manifest.discipline_priors[0]
    with pytest.raises(ValueError, match="event and material"):
        replace(context_prior, event_code="")
    with pytest.raises(ValueError, match="context prior fields"):
        ContextPrior.from_dict({**context_prior.to_dict(), "extra": True})
    with pytest.raises(ValueError, match="discipline code"):
        replace(discipline_prior, discipline="")
    with pytest.raises(ValueError, match="discipline prior fields"):
        DisciplinePrior.from_dict({**discipline_prior.to_dict(), "extra": True})
    for changes, message in (
        ({"prior_log_variance": "0.5"}, "sigma and variance"),
        ({"context_priors": []}, "context priors must be"),
        ({"context_priors": tuple(reversed(manifest.context_priors))}, "context prior keys"),
        ({"discipline_priors": []}, "discipline priors must be"),
        (
            {"discipline_priors": tuple(reversed(manifest.discipline_priors))},
            "discipline prior keys",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            replace(manifest, **changes)  # type: ignore[arg-type]
    value = manifest.to_dict()
    value["context_priors"] = {}
    with pytest.raises(ValueError, match="context_priors must be a JSON array"):
        FormulaManifest.from_dict(value)
    value = manifest.to_dict()
    value["discipline_priors"] = {}
    with pytest.raises(ValueError, match="discipline_priors must be a JSON array"):
        FormulaManifest.from_dict(value)
    for changes, message in (
        ({"size_mm": 0}, "size must be"),
        ({"median_seconds": "0"}, "median must be"),
        ({"log_variance": "0"}, "variance must be"),
        ({"pseudo_count": 0}, "pseudo strength"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(context_prior, **changes)


def test_manifest_round_trip_digest_and_constants() -> None:
    manifest = FormulaManifest.load(MANIFEST_PATH)
    assert FormulaManifest.from_dict(manifest.to_dict()) == manifest
    assert manifest.version == "formula:v2-bootstrap"
    assert manifest.prior_pseudo_count == 3
    assert manifest.huber_tuning == "1.5"
    assert manifest.irls_max_iterations == 20
    assert manifest.irls_tolerance == "0.0000000001"
    assert manifest.prior_median_ms == 55000
    assert manifest.prior_sigma_ms > 0
    value = manifest.to_dict()
    value["digest"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        FormulaManifest.from_dict(value)
    value = manifest.to_dict()
    value["extra"] = True
    with pytest.raises(ValueError, match="fields"):
        FormulaManifest.from_dict(value)


def test_dense_exact_history_overcomes_population_prior_and_is_green() -> None:
    result = run(*(observation(index, 100000) for index in range(1, 21)))
    assert result.center_ms > 90000
    assert result.forecast.assessor is AssessorKind.FORMULA
    assert result.forecast.state is ForecastState.COMMITTED
    assert result.review is ReviewClassification.GREEN
    assert result.forecast.warnings == ()
    assert result.forecast.support.eligible_count == 20
    assert result.forecast.support.exact_context_count == 20
    assert result.recompute_digest() == result.assessment_digest
    assert len([row for row in result.trace if row.stage == "prior"]) == 3
    iterations = [row for row in result.trace if row.stage == "irls_iteration"]
    assert 1 <= len(iterations) <= 20
    assert [int(row.details["iteration"]) for row in iterations] == list(
        range(1, len(iterations) + 1)
    )
    assert all(
        {"center_start", "center_end", "delta", "scale"} <= row.details.keys() for row in iterations
    )


def test_exact_weight_uses_recency_quality_and_tournament_factors() -> None:
    result = run(
        observation(
            1,
            40000,
            occurred_at_utc="2024-08-29T12:00:00.000Z",
            tournament="legacy-a",
        ),
        cutoff_at_utc="2026-08-29T12:00:00.000Z",
        origins=(EvidenceOrigin.VERIFIED_HISTORICAL_IMPORT,),
        legacy_tournaments=("legacy-a",),
    )
    details = observation_row(result).details
    assert details["context_factor"] == "1"
    assert details["diameter_similarity"] == "1"
    assert details["recency_factor"] == "0.5"
    assert details["quality_factor"] == "0.85"
    assert details["tournament_factor"] == "0.75"
    assert details["conversion_variance"] == "0"
    assert details["total_weight"] == "0.31875"


def test_declared_same_and_cross_discipline_factors_and_directional_transforms() -> None:
    target = context("underhand", 300, "eucalyptus", "720")
    same = run(
        observation(1, 30000, observed_context=context("standing_block", 250, "pine", "500")),
        target=target,
    )
    cross = run(
        observation(1, 30000, observed_context=context("single_buck", 250, "pine", "500")),
        target=target,
    )
    same_details = observation_row(same).details
    cross_details = observation_row(cross).details
    assert same_details["context_factor"] == "0.6"
    assert same_details["conversion_status"] == "declared_same_discipline"
    assert cross_details["context_factor"] == "0.25"
    assert cross_details["conversion_status"] == "declared_cross_discipline"
    assert Decimal(same_details["size_factor"]) > 1
    assert Decimal(same_details["event_factor"]) < 1
    assert Decimal(same_details["material_factor"]) > 1
    assert int(same_details["transformed_time_ms"]) > 30000
    assert Decimal(same_details["diameter_similarity"]) < 1
    assert Decimal(same_details["conversion_variance"]) > 0


@pytest.mark.parametrize(
    "source",
    [
        context("invented_event", 300, "eucalyptus", "720"),
        context("underhand", 300, "pine", None),
    ],
)
def test_unsupported_event_or_material_is_traced_zero_weight_and_red(source: TargetContext) -> None:
    result = run(observation(1, 40000, observed_context=source))
    details = observation_row(result).details
    assert details["total_weight"] == "0"
    assert details["conversion_status"].startswith("unsupported")
    assert result.center_ms == 45000
    assert result.personal_weight == "0"
    assert result.review is ReviewClassification.RED
    assert ForecastWarning.MISSING_CONTEXT in result.forecast.warnings


def test_zero_history_is_exact_broad_positive_prior_and_red() -> None:
    manifest = FormulaManifest.load(MANIFEST_PATH)
    result = run()
    assert result.center_ms == 45000
    assert result.log_scale == "0.4"
    selection = next(row for row in result.trace if row.stage == "prior_selection")
    assert selection.details["prior_tier"] == "exact_context"
    assert result.effective_sample_size == "0"
    assert result.review is ReviewClassification.RED
    assert set(result.forecast.warnings) == {
        ForecastWarning.INSUFFICIENT_SUPPORT,
        ForecastWarning.PRIOR_ONLY,
        ForecastWarning.SPARSE_EVIDENCE,
    }
    assert all(point.time_ms > 0 for point in result.forecast.distribution.quantiles)  # type: ignore[union-attr]


def test_zero_history_prior_hierarchy_is_target_specific_and_visible() -> None:
    exact = run(target=context("standing_block", 300, "eucalyptus", "720"))
    another_exact = run(target=context("underhand", 350, "pine", "500"))
    discipline = run(target=context("underhand", 325, "pine", "500"))
    population = run(target=context("invented_event", 300, "pine", "500"))
    assert [item.center_ms for item in (exact, another_exact, discipline, population)] == [
        52000,
        58000,
        49000,
        55000,
    ]
    assert [
        next(row for row in item.trace if row.stage == "prior_selection").details["prior_tier"]
        for item in (exact, another_exact, discipline, population)
    ] == ["exact_context", "exact_context", "discipline", "population"]
    assert discipline.review is ReviewClassification.RED
    assert population.review is ReviewClassification.RED
    assert ForecastWarning.MISSING_CONTEXT in discipline.forecast.warnings
    assert ForecastWarning.MISSING_CONTEXT in population.forecast.warnings
    assert (
        len({item.uncertainty_ms for item in (exact, another_exact, discipline, population)}) == 4
    )


def test_zero_history_prior_resolver_reproduces_formula_authority_exactly() -> None:
    manifest = FormulaManifest.load(MANIFEST_PATH)
    target = context()
    resolved = resolve_zero_history_prior(target, manifest)
    assessed = run(target=target)

    assert isinstance(resolved, FormulaZeroHistoryPrior)
    assert resolved.distribution == assessed.forecast.distribution
    assert resolved.prior_tier == "exact_context"
    assert resolved.prior_key == "underhand|300|eucalyptus"
    assert resolved.prior_lineage_digest == next(
        item.lineage_digest for item in manifest.context_priors if item.key == resolved.prior_key
    )
    assert resolved.manifest_digest == manifest.digest


def test_governor_derives_age_quality_and_tournament_relevance_without_caller_labels() -> None:
    packet = evidence(
        observation(1, 40000, occurred_at_utc="2026-08-29T12:00:00.000Z"),
        observation(
            2,
            41000,
            occurred_at_utc="2025-08-29T12:00:00.000Z",
            tournament="authority-a",
        ),
        observation(
            3,
            42000,
            occurred_at_utc="2024-08-29T12:00:00.000Z",
            tournament="legacy-a",
        ),
    )
    sealed = formula_input(
        packet,
        cutoff_at_utc="2026-08-29T12:00:00.000Z",
        origins=(
            EvidenceOrigin.ISSUED_RESULT_RECEIPT,
            EvidenceOrigin.VERIFIED_HISTORICAL_IMPORT,
            EvidenceOrigin.VERIFIED_HISTORICAL_IMPORT,
        ),
        authoritative_tournaments=("authority-a",),
        legacy_tournaments=("legacy-a",),
    )
    assert [item.age_days for item in sealed.observation_facts] == ["0", "365", "730"]
    assert [item.quality for item in sealed.observation_facts] == [
        EvidenceQuality.ISSUED_OFFICIAL,
        EvidenceQuality.VERIFIED_HISTORICAL,
        EvidenceQuality.VERIFIED_HISTORICAL,
    ]
    assert [item.tournament for item in sealed.observation_facts] == [
        TournamentRelevance.ACTIVE,
        TournamentRelevance.OTHER_AUTHORITATIVE,
        TournamentRelevance.LEGACY,
    ]
    assert all(
        item.governor_receipt_digest == sealed.governor_receipt.receipt_digest
        for item in sealed.observation_facts
    )


def test_governor_rejects_future_unknown_or_tampered_provenance() -> None:
    future = evidence(observation(1, 40000, occurred_at_utc="2026-08-30T12:00:00.000Z"))
    with pytest.raises(ValueError, match="after the sealed cutoff"):
        formula_input(future, cutoff_at_utc="2026-08-29T12:00:00.000Z")
    unknown = evidence(observation(1, 40000, tournament="unknown-a"))
    with pytest.raises(ValueError, match="no governor authority"):
        formula_input(unknown)
    sealed = formula_input(evidence(observation(1, 40000)))
    with pytest.raises(ValueError, match="digest mismatch"):
        replace(sealed.governor_receipt, evidence_digest="f" * 64)


def test_noncompletion_remains_in_trace_but_never_becomes_numeric_evidence() -> None:
    result = run(observation(1, 1, status=ResultStatus.DNF))
    details = observation_row(result).details
    assert details["admitted"] == "false"
    assert details["raw_time_ms"] == "0"
    assert details["total_weight"] == "0"
    assert result.forecast.support.eligible_count == 0


def test_conflicting_evidence_is_retained_and_widens_log_uncertainty() -> None:
    consistent = run(observation(1, 40000), observation(2, 40000))
    conflict = run(observation(1, 20000), observation(2, 80000))
    assert Decimal(conflict.log_scale) > Decimal(consistent.log_scale)
    assert len([row for row in conflict.trace if row.stage == "observation"]) == 2


def test_assessor_and_input_contract_fail_closed() -> None:
    manifest = FormulaManifest.load(MANIFEST_PATH)
    with pytest.raises(ValueError, match="FormulaInputPacket"):
        assess_formula(evidence(), manifest)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="FormulaManifest"):
        assess_formula(formula_input(evidence()), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="verified governor projection"):
        FormulaInputPacket(object(), object(), ())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            (
                StableIdentifier("competitor:a"),
                "0",
                EvidenceQuality.ISSUED_OFFICIAL,
                TournamentRelevance.ACTIVE,
            ),
            "evidence identifier",
        ),
        (
            (
                StableIdentifier("evidence:a"),
                "-1",
                EvidenceQuality.ISSUED_OFFICIAL,
                TournamentRelevance.ACTIVE,
            ),
            "age_days",
        ),
        (
            (
                StableIdentifier("evidence:a"),
                "1.0",
                EvidenceQuality.ISSUED_OFFICIAL,
                TournamentRelevance.ACTIVE,
            ),
            "age_days",
        ),
        (
            (StableIdentifier("evidence:a"), "0", "issued", TournamentRelevance.ACTIVE),
            "EvidenceQuality",
        ),
        (
            (StableIdentifier("evidence:a"), "0", EvidenceQuality.ISSUED_OFFICIAL, "active"),
            "TournamentRelevance",
        ),
    ],
)
def test_formula_observation_facts_validation(arguments: tuple[object, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        FormulaObservationFacts(*arguments, "0" * 64)  # type: ignore[arg-type]


def test_formula_input_requires_immutable_exact_ordered_fact_coverage() -> None:
    packet = evidence(observation(1, 40000))
    sealed = formula_input(packet)
    facts = sealed.observation_facts[0]
    with pytest.raises(ValueError, match="verified governor projection"):
        FormulaInputPacket(packet, sealed.governor_receipt, [facts])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="verified governor projection"):
        FormulaInputPacket(packet, sealed.governor_receipt, ())
    assert sealed.to_dict()["schema_version"] == "strathmark-v3-formula-input-v1"
    assert len(sealed.digest) == 64
    spoofed = replace(facts, quality=EvidenceQuality.VERIFIED_HISTORICAL)
    with pytest.raises(ValueError, match="verified governor projection"):
        FormulaInputPacket(packet, sealed.governor_receipt, (spoofed,))


def test_verified_formula_projection_internals_fail_closed_on_every_spoofed_shape() -> None:
    packet = evidence(observation(1, 40000))
    sealed = formula_input(packet)
    receipt = sealed.governor_receipt
    facts = sealed.observation_facts

    with pytest.raises(ValueError, match="verified signed authority"):
        FormulaGovernorReceipt._from_verified_projection(
            evidence=packet,
            tournament_epoch_content_digest=receipt.tournament_epoch_content_digest,
            cutoff_at_utc=receipt.cutoff_at_utc,
            active_tournament_id=receipt.active_tournament_id,
            authoritative_tournament_ids=(),
            legacy_tournament_ids=(),
            provenance=receipt.provenance,
            signed_manifest_body_digest=receipt.signed_manifest_body_digest,
            signer_key_id=receipt.signer_key_id,
            _verification=object(),
        )
    with pytest.raises(ValueError, match="verified signed authority"):
        FormulaInputPacket._from_verified_projection(packet, receipt, facts, _verification=object())

    def raw_input(
        evidence_value: object, receipt_value: object, facts_value: object
    ) -> FormulaInputPacket:
        value = object.__new__(FormulaInputPacket)
        object.__setattr__(value, "evidence", evidence_value)
        object.__setattr__(value, "governor_receipt", receipt_value)
        object.__setattr__(value, "observation_facts", facts_value)
        return value

    for arguments, message in (
        ((object(), receipt, facts), "EvidencePacket"),
        ((packet, object(), facts), "FormulaGovernorReceipt"),
        ((packet, receipt, list(facts)), "immutable typed tuple"),
        ((packet, receipt, ()), "exactly cover"),
        (
            (
                packet,
                receipt,
                (replace(facts[0], quality=EvidenceQuality.VERIFIED_HISTORICAL),),
            ),
            "governor-derived",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            raw_input(*arguments).__post_init__()


def test_formula_receipt_derivation_rejects_every_unsigned_binding_mutation() -> None:
    packet = evidence(observation(1, 40000))
    receipt = formula_input(packet).governor_receipt

    def receipt_with(**changes: object) -> FormulaGovernorReceipt:
        values = {
            "evidence_digest": receipt.evidence_digest,
            "historical_cutoff_key": receipt.historical_cutoff_key,
            "tournament_epoch_id": receipt.tournament_epoch_id,
            "tournament_epoch_content_digest": receipt.tournament_epoch_content_digest,
            "cutoff_at_utc": receipt.cutoff_at_utc,
            "active_tournament_id": receipt.active_tournament_id,
            "authoritative_tournament_ids": receipt.authoritative_tournament_ids,
            "legacy_tournament_ids": receipt.legacy_tournament_ids,
            "provenance": receipt.provenance,
            "signed_manifest_body_digest": receipt.signed_manifest_body_digest,
            "signer_key_id": receipt.signer_key_id,
            **changes,
        }
        return FormulaGovernorReceipt(
            **values,
            receipt_digest=canonical_digest(
                FormulaGovernorReceipt._content_value_from_arguments(**values)
            ),
        )

    mutations = (
        ({"evidence_digest": "f" * 64}, "evidence packet"),
        ({"historical_cutoff_key": "history:other"}, "historical cutoff"),
        ({"tournament_epoch_id": StableIdentifier("epoch:other")}, "tournament epoch"),
        ({"provenance": ()}, "exactly cover"),
        (
            {"provenance": (replace(receipt.provenance[0], authority_digest="f" * 64),)},
            "canonical observation source",
        ),
        (
            {"provenance": (replace(receipt.provenance[0], numeric_eligible=False),)},
            "canonical admission classification",
        ),
        (
            {"active_tournament_id": StableIdentifier("tournament:other")},
            "no governor authority",
        ),
    )
    for changes, message in mutations:
        with pytest.raises(ValueError, match=message):
            _derive_formula_facts(packet, receipt_with(**changes))

    with pytest.raises(ValueError, match="integrity key"):
        FormulaGovernorReceipt(
            evidence_digest=receipt.evidence_digest,
            historical_cutoff_key=receipt.historical_cutoff_key,
            tournament_epoch_id=receipt.tournament_epoch_id,
            tournament_epoch_content_digest=receipt.tournament_epoch_content_digest,
            cutoff_at_utc=receipt.cutoff_at_utc,
            active_tournament_id=receipt.active_tournament_id,
            authoritative_tournament_ids=(),
            legacy_tournament_ids=(),
            provenance=receipt.provenance,
            signed_manifest_body_digest=receipt.signed_manifest_body_digest,
            signer_key_id="caller-key",
            receipt_digest="0" * 64,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"stage": ""},
        {"label": ""},
        {"unit": ""},
        {"value": "1.0"},
        {"terms": []},
        {"terms": (("only-one",),)},
        {"terms": (("key", ""),)},
        {"terms": (("z", "1"), ("a", "2"))},
    ],
)
def test_arithmetic_trace_contract_rejects_noncanonical_values(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "stage": "center",
        "label": "step",
        "value": "1",
        "unit": "ms",
        "terms": (("a", "1"),),
    }
    values.update(changes)
    with pytest.raises(ValueError, match="trace"):
        ArithmeticTraceRow(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"forecast": object()}, "AssessorForecast"),
        ({"review": "red"}, "ReviewClassification"),
        ({"center_ms": 0}, "center_ms"),
        ({"center_ms": True}, "center_ms"),
        ({"uncertainty_ms": 0}, "uncertainty_ms"),
        ({"uncertainty_ms": True}, "uncertainty_ms"),
        ({"log_center": "1.0"}, "log_center"),
        ({"log_scale": "-1"}, "nonnegative"),
        ({"effective_sample_size": "-1"}, "nonnegative"),
        ({"personal_weight": "1.0"}, "personal_weight"),
        ({"manifest_digest": "bad"}, "manifest_digest"),
        ({"trace": ()}, "trace"),
        ({"assessment_digest": "f" * 64}, "digest mismatch"),
    ],
)
def test_assessment_result_validation(changes: dict[str, object], message: str) -> None:
    result = run()
    with pytest.raises(ValueError, match=message):
        replace(result, **changes)
    assert result.to_dict()["assessment_digest"] == result.assessment_digest


def manifest_arguments() -> dict[str, object]:
    manifest = FormulaManifest.load(MANIFEST_PATH)
    return {name: getattr(manifest, name) for name in manifest.__dataclass_fields__}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": "unknown"}, "unsupported"),
        ({"version": "old"}, "unsupported"),
        ({"time_quantum_ms": 0}, "positive integer"),
        ({"prior_pseudo_count": True}, "positive integer"),
        ({"time_quantum_ms": 2}, "one-millisecond"),
        ({"prior_pseudo_count": 2}, "three prior"),
        ({"irls_max_iterations": 19}, "iteration"),
        ({"maximum_time_ms": 1}, "bounds"),
        ({"prior_log_sigma": "0"}, "positive canonical"),
        ({"prior_log_sigma": "1.0"}, "positive canonical"),
        ({"exact_context_factor": "0.9"}, "frozen plan"),
        ({"event_scales": (("underhand", "0"),)}, "positive"),
        ({"declared_event_relations": (("a->b", "unknown"),)}, "relations"),
        ({"quantiles": (("0.25", "-1"), ("0.75", "1"))}, "include 0.5"),
        ({"quantiles": (("0.5", "0"), ("0.25", "-1"))}, "sorted"),
    ],
)
def test_manifest_constructor_validation(changes: dict[str, object], message: str) -> None:
    values = manifest_arguments()
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        FormulaManifest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "population_prior",
        "robust_center",
        "context",
        "quality",
        "tournament",
        "conversion_variance",
        "positive_bounds",
    ],
)
def test_manifest_nested_maps_fail_closed(key: str) -> None:
    value = FormulaManifest.load(MANIFEST_PATH).to_dict()
    value[key] = {}
    with pytest.raises(ValueError, match=key):
        FormulaManifest.from_dict(value)


def test_manifest_mapping_and_table_helpers_fail_closed() -> None:
    for invalid in ([], {}, {"a": 1}):
        with pytest.raises(ValueError, match="object"):
            _mapping_table(invalid, "table")
    for invalid in ([], {}):
        with pytest.raises(ValueError, match="closed"):
            _closed_mapping(invalid, {"a"}, "nested")
    assert _closed_mapping({"a": 1}, {"a"}, "nested") == {"a": 1}
    for invalid in (
        (),
        (("only-one",),),
        (("key", ""),),
        (("z", "1"), ("a", "2")),
        (("a", "1"), ("a", "2")),
    ):
        with pytest.raises(ValueError, match="table"):
            _validate_table(invalid, "table")


def test_weighted_median_and_huber_branches_are_deterministic() -> None:
    assert _weighted_median((Decimal("1"), Decimal("2")), (Decimal("1"), Decimal("1"))) == 1
    with pytest.raises(ValueError, match="positive weight"):
        _weighted_median((Decimal("1"),), (Decimal("-1"),))
    assert _huber_weight(Decimal("1"), Decimal("1.5")) == 1
    assert _huber_weight(Decimal("3"), Decimal("1.5")) == Decimal("0.5")
    center, iterations = _irls(
        (Decimal("0"), Decimal("10")),
        (Decimal("3"), Decimal("1")),
        Decimal("0"),
        Decimal("0.05"),
        SimpleNamespace(huber_tuning="1.5", irls_max_iterations=20, irls_tolerance="-1"),
    )
    assert len(iterations) == 20
    assert center > 0


def test_manifest_json_contains_no_unknown_runtime_defaults() -> None:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert raw == FormulaManifest.load(MANIFEST_PATH).to_dict()
