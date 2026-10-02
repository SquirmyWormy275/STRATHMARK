"""Real Formula/ML forecasts over a frozen Linux competition evidence epoch."""

from __future__ import annotations

import statistics
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from strathmark.v3.application.formula_governor import (
    FormulaHistoricalAuthority,
    FormulaLiveAuthority,
    FormulaProjectionFactory,
    HistoricalCutoverAuthority,
    seal_formula_governor_batch,
)
from strathmark.v3.assessors.formula import FormulaManifest, assess_formula
from strathmark.v3.assessors.ml import MLAssessor
from strathmark.v3.contracts.evidence import EvidencePacket, ResultObservation, TargetContext
from strathmark.v3.contracts.forecasts import AssessorKind, ForecastState, SamplingSpec
from strathmark.v3.contracts.identifiers import StableIdentifier, deterministic_identifier
from strathmark.v3.domain.capability import (
    CapabilityEvidence,
    CapabilityPrior,
    apply_capability_operator,
    replay_capability,
)
from strathmark.v3.domain.epochs import EpochMember, ReactionBarrier, freeze_epoch
from strathmark.v3.domain.evidence import AdmissionReason, EvidenceSource, IssuedFieldFact
from strathmark.v3.domain.optimizer import (
    OptimizationCompetitor,
    OptimizationField,
    optimize_and_verify_field,
)
from strathmark.v3.domain.pooling import LinearPoolComponent, LinearPooledDistribution
from strathmark.v3.factory.ml_artifacts import load_ml_bundle
from strathmark.v3.factory.workbook_history import load_workbook_history
from strathmark.v3.linux_lifecycle_store import LinuxLifecycleError, digest

POLICY = "strathmark-linux-competition-forecast-v1"
FORMULA_PATH = Path(__file__).parent / "contracts/formula_manifest.json"


def _result_key(observation, live_by_id):
    if str(observation.evidence_id) not in live_by_id:
        return deterministic_identifier(
            "result", {"historical_evidence_id": str(observation.evidence_id)}
        )
    item = live_by_id[str(observation.evidence_id)]
    return deterministic_identifier(
        "result",
        {
            "field_id": str(observation.field_id),
            "field_revision": item["upstream_field_revision"],
            "competitor_id": str(observation.competitor_id),
        },
    )


def calculate(
    *,
    bundle_root: Path,
    signer,
    trust,
    scope_id: str,
    round_id: str,
    round_snapshot: dict,
    competitor_ids: list[str],
    target_context: dict,
    field_id: str | None = None,
    ceiling: int = 180,
) -> dict:
    """Never read changing live history or add current-round settlements here."""
    import catboost

    if (
        not isinstance(competitor_ids, list)
        or not competitor_ids
        or len(set(competitor_ids)) != len(competitor_ids)
    ):
        raise LinuxLifecycleError("forecast roster must contain distinct stable competitors")
    if len(competitor_ids) > (128 if field_id is None else 12):
        raise LinuxLifecycleError("Linux competition forecast capacity exceeded")
    history = load_workbook_history(
        round_snapshot["history_path"], cutoff_at_utc=round_snapshot["cutoff_at_utc"]
    )
    if history.source_sha256 != round_snapshot["history_sha256"]:
        raise LinuxLifecycleError("frozen historical workbook changed")
    identifiers = dict(history.competitor_ids)
    identifiers.update(
        {
            item: str(deterministic_identifier("competitor", {"local_id": item}))
            for item in competitor_ids
            if item not in identifiers
        }
    )
    context = TargetContext.from_dict(target_context)
    scope = StableIdentifier(scope_id)
    live = round_snapshot["live_results"]
    live_by_id = {item["observation"]["evidence_id"]: item for item in live}
    observations = list(history.observations)
    for offset, item in enumerate(live, len(observations) + 1):
        observations.append(
            replace(ResultObservation.from_dict(item["observation"]), observation_sequence=offset)
        )
    cutoff_key = "history:" + digest(
        {"source": history.source_sha256, "cutoff_at_utc": round_snapshot["cutoff_at_utc"]}
    )
    maximum = len(observations)
    epoch = freeze_epoch(
        round_id=StableIdentifier(round_id),
        epoch_revision=1,
        historical_cutoff_key=cutoff_key,
        closed_through_sequence=maximum,
        members=tuple(
            sorted(
                (
                    EpochMember(
                        str(_result_key(item, live_by_id)),
                        item.result.revision,
                        item.observation_sequence,
                        item.result.status.value == "completion",
                    )
                    for item in observations
                ),
                key=lambda item: item.result_key,
            )
        ),
        barrier=ReactionBarrier.complete_through(maximum),
    )
    legacy = tuple(sorted({item.tournament_id for item in history.observations}))
    authoritative = tuple(
        sorted(
            {
                item.tournament_id
                for item in observations
                if str(item.evidence_id) in live_by_id and item.tournament_id != scope
            }
        )
    )
    projection = FormulaProjectionFactory(
        trust_store=trust,
        cutoff_at_utc=round_snapshot["frozen_at_utc"],
        active_tournament_id=scope,
        authoritative_tournament_ids=authoritative,
        legacy_tournament_ids=legacy,
    )
    cutover = HistoricalCutoverAuthority(
        deterministic_identifier(
            "historical_cutover", {"source": history.source_sha256, "cutoff": cutoff_key}
        ),
        cutoff_key,
        digest({"profile": POLICY, "workbook": history.source_sha256}),
    )
    ml = MLAssessor(
        load_ml_bundle(
            bundle_root,
            installed_catboost_version=catboost.__version__,
            installed_python_abi="cp313",
        )
    )
    manifest = FormulaManifest.load(FORMULA_PATH)
    weights = round_snapshot["weights"]
    if (
        set(weights) != {"formula", "ml"}
        or any(Decimal(value) <= 0 for value in weights.values())
        or sum(map(Decimal, weights.values())) != 1
    ):
        raise LinuxLifecycleError("round weights are invalid")
    forecasts, pools, samples = [], [], []
    for local_id in competitor_ids:
        identifier = identifiers[local_id]
        evidence = tuple(item for item in observations if str(item.competitor_id) == identifier)
        packet = EvidencePacket.create(
            competitor_id=StableIdentifier(identifier),
            target_context=context,
            observations=evidence,
            taxonomy_version=context.taxonomy_version,
            conversion_version=context.conversion_version,
            historical_cutoff_key=cutoff_key,
            tournament_epoch_id=epoch.epoch_id,
            tournament_event_sequence=maximum,
        )
        historical_authorities, live_authorities, capability_evidence = [], [], []
        for item in evidence:
            live_item = live_by_id.get(str(item.evidence_id))
            if live_item is None:
                historical_authorities.append(
                    FormulaHistoricalAuthority(
                        item.evidence_id, _result_key(item, live_by_id), cutover
                    )
                )
                continue
            receipt = live_item["issued_field"]
            issued = IssuedFieldFact(
                StableIdentifier(receipt["field_id"]),
                receipt["upstream_field_revision"],
                tuple(StableIdentifier(value) for value in receipt["competitor_ids"]),
                StableIdentifier(receipt["receipt_id"]),
                StableIdentifier(receipt["scope_id"]),
                StableIdentifier(receipt["round_id"]),
                TargetContext.from_dict(receipt["target_context"]),
                tuple((StableIdentifier(value), mark) for value, mark in receipt["issued_marks"]),
            )
            live_authorities.append(
                FormulaLiveAuthority(
                    item.evidence_id, issued, issued.upstream_revision, issued.receipt_id
                )
            )
            if item.context.digest == context.digest and item.result.status.value == "completion":
                capability_evidence.append(
                    CapabilityEvidence(
                        _result_key(item, live_by_id),
                        item.result.revision,
                        None if item.result.revision == 1 else item.result.revision - 1,
                        item.competitor_id,
                        context.digest,
                        item.observation_sequence,
                        item.occurred_at_utc,
                        item.result.raw_time_ms,
                        EvidenceSource.LIVE_ISSUED_RACE,
                        True,
                        AdmissionReason.ELIGIBLE_COMPLETION,
                        digest(item.to_dict()),
                        live_item["authority_digest"],
                        CapabilityPrior.from_median_seconds(
                            str(Decimal(live_item["prior_median_ms"]) / 1000),
                            calibrated_beta="0.12",
                        ),
                        "0.0025",
                        "0",
                        "1",
                        None,
                    )
                )
        batch = seal_formula_governor_batch(
            evidence=packet,
            epoch=epoch,
            cutoff_at_utc=round_snapshot["frozen_at_utc"],
            active_tournament_id=scope,
            authoritative_tournament_ids=authoritative,
            legacy_tournament_ids=legacy,
            live_authorities=tuple(live_authorities),
            historical_authorities=tuple(historical_authorities),
            signer=signer,
            created_at=round_snapshot["frozen_at_utc"],
        )
        formula = assess_formula(
            projection.project(evidence=packet, epoch=epoch, sealed_batch=batch), manifest
        )
        machine = ml.assess(packet)
        if (
            formula.forecast.state is not ForecastState.COMMITTED
            or machine.forecast.state is not ForecastState.COMMITTED
        ):
            raise LinuxLifecycleError(
                "a required numeric assessor abstained; no engine substitution is permitted"
            )
        state = replay_capability(tuple(capability_evidence))
        adjusted, components = [], []
        for forecast in (formula.forecast, machine.forecast):
            distribution = forecast.distribution
            if state is not None:
                adjustment = apply_capability_operator(forecast.assessor, distribution, state)
                adjusted.append(
                    {
                        **adjustment.content_value(),
                        "adjustment_digest": adjustment.adjustment_digest,
                    }
                )
                distribution = adjustment.adjusted_distribution
            components.append(
                LinearPoolComponent(
                    forecast.assessor, weights[forecast.assessor.value], distribution
                )
            )
        pool = LinearPooledDistribution(tuple(components))
        sample = pool.sample(
            SamplingSpec(
                seed=int(
                    digest(
                        {"packet": packet.content_digest, "pool": pool.digest, "profile": POLICY}
                    )[:15],
                    16,
                ),
                draw_count=4096,
            )
        )
        pools.append(pool)
        samples.append(sample.samples_ms)
        forecasts.append(
            {
                "local_competitor_id": local_id,
                "competitor_id": identifier,
                "evidence_digest": packet.content_digest,
                "formula": formula.to_dict(),
                "ml": machine.to_dict(),
                "pool": pool.to_dict(),
                "predicted_time_ms": pool.median_ms,
                "std_dev_seconds": statistics.pstdev(sample.samples_ms) / 1000,
                "history_count": len(evidence),
                "sampling_seed": sample.seed,
                "samples_digest": sample.samples_digest,
                "capability_adjustments": adjusted,
            }
        )
    output = {
        "policy": POLICY,
        "epoch_digest": epoch.content_digest,
        "epoch_id": str(epoch.epoch_id),
        "history_sha256": history.source_sha256,
        "weights": weights,
        "forecasts": forecasts,
        "issued_mark": False,
    }
    if field_id is not None:
        competitors = tuple(
            OptimizationCompetitor(
                StableIdentifier(item["competitor_id"]),
                pool.median_ms,
                samples[index],
                index,
                pool.digest,
            )
            for index, (item, pool) in enumerate(zip(forecasts, pools, strict=True))
        )
        field = OptimizationField.create(
            field_id=StableIdentifier(field_id),
            source_receipt_digest=digest(output),
            competitors=competitors,
        )
        optimized = optimize_and_verify_field(field, ceiling=ceiling)
        output["optimizer"] = optimized.receipt.to_dict()
        output["marks"] = list(optimized.receipt.selected_marks)
        # Consequences use complete-field counterfactuals, preserving common
        # rebasing instead of treating isolated raw-time gaps as issued marks.
        counterfactuals = {}
        for assessor in (AssessorKind.FORMULA, AssessorKind.ML):
            distributions = [
                next(
                    component.distribution
                    for component in pool.components
                    if component.assessor is assessor
                )
                for pool in pools
            ]
            medians = [distribution.median_ms for distribution in distributions]
            counterfactuals[assessor.value] = [
                min(ceiling, max(3, 3 + round((max(medians) - value) / 1000))) for value in medians
            ]
        output["counterfactual_marks"] = counterfactuals
        output["maximum_mark_disagreement"] = max(
            abs(a - b)
            for a, b in zip(counterfactuals["formula"], counterfactuals["ml"], strict=True)
        )
        output["classification"] = "red" if output["maximum_mark_disagreement"] >= 5 else "amber"
    return output
