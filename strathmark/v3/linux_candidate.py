"""Bounded subprocess interface for the Linux numeric V3 candidate.

This explicitly separate preview profile executes Formula, the verified ML
bundle, linear pooling, and the V3 optimizer. It does not impersonate the frozen
V7 service or produce approval, issue, settlement, or production receipts.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from strathmark.v3.application.formula_governor import (
    FormulaHistoricalAuthority,
    FormulaProjectionFactory,
    HistoricalCutoverAuthority,
    seal_formula_governor_batch,
)
from strathmark.v3.assessors.formula import FormulaManifest, assess_formula
from strathmark.v3.assessors.ml import MLAssessor
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket, TargetContext
from strathmark.v3.contracts.forecasts import AssessorKind, ForecastState, SamplingSpec
from strathmark.v3.contracts.identifiers import StableIdentifier, deterministic_identifier
from strathmark.v3.domain.epochs import EpochMember, ReactionBarrier, freeze_epoch
from strathmark.v3.domain.optimizer import (
    OptimizationCompetitor,
    OptimizationField,
    optimize_and_verify_field,
)
from strathmark.v3.domain.pooling import LinearPoolComponent, LinearPooledDistribution
from strathmark.v3.factory.ml_artifacts import load_ml_bundle
from strathmark.v3.factory.workbook_history import load_workbook_history
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from strathmark.v3.runtime_identity import implementation_digest

PROTOCOL = "strathmark.v3-linux-numeric-candidate.v1"
CONTRACT_DIGEST = canonical_digest(
    {
        "protocol": PROTOCOL,
        "operations": ["status", "forecast", "preview"],
        "purpose": "numeric_preview_only",
        "issued_mark": False,
    }
)


def status(bundle_root: Path) -> dict:
    import catboost

    from strathmark import __version__

    if sys.version_info[:2] != (3, 13):
        raise ValueError("the V3 Linux candidate requires Python 3.13")
    bundle = load_ml_bundle(
        bundle_root, installed_catboost_version=catboost.__version__, installed_python_abi="cp313"
    )
    manifest = FormulaManifest.load(Path(__file__).parent / "contracts" / "formula_manifest.json")
    return {
        "protocol": PROTOCOL,
        "contract_digest": CONTRACT_DIGEST,
        "source_digest": implementation_digest(),
        "package_version": __version__,
        "ml_bundle_digest": bundle.digest,
        "formula_digest": manifest.digest,
        "available": True,
        "mode": "rehearsal",
        "purpose": "numeric_preview_only",
        "production_ready": False,
        "assessors": {"formula": "available", "ml": "available", "llm_council": "unavailable"},
        "weights": "equal bootstrap Formula/ML; no earned credibility claim",
        "dependence": "independent bootstrap; legacy field dependence is unavailable",
    }


def preview(bundle_root: Path, payload: dict, *, forecast_only: bool = False) -> dict:
    import catboost

    required = {
        "workbook",
        "cutoff_at_utc",
        "scope_id",
        "round_id",
        "field_id",
        "competitor_ids",
        "target_context",
        "ceiling",
    }
    if set(payload) != required:
        raise ValueError("candidate preview request fields differ from its protocol")
    readiness = status(bundle_root)
    history = load_workbook_history(payload["workbook"], cutoff_at_utc=payload["cutoff_at_utc"])
    identifiers = dict(history.competitor_ids)
    local_ids = payload["competitor_ids"]
    if (
        not isinstance(local_ids, list)
        or not 1 <= len(local_ids) <= (128 if forecast_only else 12)
        or len(local_ids) != len(set(local_ids))
        or any(item not in identifiers for item in local_ids)
    ):
        raise ValueError("candidate request exceeds its distinct existing competitor capacity")
    context = TargetContext.from_dict(payload["target_context"])
    scope = StableIdentifier(payload["scope_id"])
    cutoff_key = f"history:{canonical_digest({'source': history.source_sha256, 'cutoff_at_utc': payload['cutoff_at_utc']})}"
    by_competitor = {
        identifiers[local_id]: tuple(
            item
            for item in history.observations
            if str(item.competitor_id) == identifiers[local_id]
        )
        for local_id in local_ids
    }
    maximum = max((item.observation_sequence for item in history.observations), default=0)
    keys = {
        str(item.evidence_id): deterministic_identifier(
            "result", {"historical_evidence_id": str(item.evidence_id)}
        )
        for item in history.observations
    }
    epoch = freeze_epoch(
        round_id=StableIdentifier(payload["round_id"]),
        epoch_revision=1,
        historical_cutoff_key=cutoff_key,
        closed_through_sequence=maximum,
        members=tuple(
            sorted(
                (
                    EpochMember(
                        str(keys[str(item.evidence_id)]),
                        item.result.revision,
                        item.observation_sequence,
                        True,
                    )
                    for item in history.observations
                ),
                key=lambda item: item.result_key,
            )
        ),
        barrier=ReactionBarrier.complete_through(maximum),
    )
    # This is an explicit candidate historical boundary, never a live issued-race
    # admission or a declaration that learning reactions ran in the V7 ledger.
    signer = P256EphemeralSigner.generate("integrity-key:linux-numeric-preview")
    trust = IntegrityTrustStore((signer.identity,))
    legacy_tournaments = tuple(sorted({item.tournament_id for item in history.observations}))
    if scope in legacy_tournaments:
        raise ValueError("active scope cannot reuse a historical identity")
    projection = FormulaProjectionFactory(
        trust_store=trust,
        cutoff_at_utc=payload["cutoff_at_utc"],
        active_tournament_id=scope,
        authoritative_tournament_ids=(),
        legacy_tournament_ids=legacy_tournaments,
    )
    cutover = HistoricalCutoverAuthority(
        deterministic_identifier(
            "historical_cutover", {"source_sha256": history.source_sha256, "cutoff": cutoff_key}
        ),
        cutoff_key,
        canonical_digest(
            {
                "profile": PROTOCOL,
                "source_sha256": history.source_sha256,
                "cutoff_at_utc": payload["cutoff_at_utc"],
                "limitations": "legacy raw completions without issued receipts",
            }
        ),
    )
    formula_manifest = FormulaManifest.load(
        Path(__file__).parent / "contracts" / "formula_manifest.json"
    )
    ml = MLAssessor(
        load_ml_bundle(
            bundle_root,
            installed_catboost_version=catboost.__version__,
            installed_python_abi="cp313",
        )
    )
    forecasts, pools, sampled = [], [], []
    for local_id in local_ids:
        competitor_id = identifiers[local_id]
        observations = by_competitor[competitor_id]
        packet = EvidencePacket.create(
            competitor_id=StableIdentifier(competitor_id),
            target_context=context,
            observations=observations,
            taxonomy_version=context.taxonomy_version,
            conversion_version=context.conversion_version,
            historical_cutoff_key=cutoff_key,
            tournament_epoch_id=epoch.epoch_id,
            tournament_event_sequence=maximum,
        )
        batch = seal_formula_governor_batch(
            evidence=packet,
            epoch=epoch,
            cutoff_at_utc=payload["cutoff_at_utc"],
            active_tournament_id=scope,
            authoritative_tournament_ids=(),
            legacy_tournament_ids=legacy_tournaments,
            live_authorities=(),
            historical_authorities=tuple(
                FormulaHistoricalAuthority(item.evidence_id, keys[str(item.evidence_id)], cutover)
                for item in observations
            ),
            signer=signer,
            created_at=payload["cutoff_at_utc"],
        )
        formula = assess_formula(
            projection.project(evidence=packet, epoch=epoch, sealed_batch=batch), formula_manifest
        )
        machine = ml.assess(packet)
        if (
            formula.forecast.state is not ForecastState.COMMITTED
            or machine.forecast.state is not ForecastState.COMMITTED
        ):
            raise ValueError(
                "a numeric assessor abstained; no V2 substitution or automatic single-assessor marks are permitted"
            )
        pool = LinearPooledDistribution(
            (
                LinearPoolComponent(AssessorKind.FORMULA, "0.5", formula.forecast.distribution),
                LinearPoolComponent(AssessorKind.ML, "0.5", machine.forecast.distribution),
            )
        )
        pools.append(pool)
        sample = pool.sample(
            SamplingSpec(
                seed=int(
                    canonical_digest(
                        {
                            "evidence_digest": packet.content_digest,
                            "pool_digest": pool.digest,
                            "profile": PROTOCOL,
                        }
                    )[:15],
                    16,
                ),
                draw_count=4096,
            )
        )
        sampled.append(sample.samples_ms)
        forecasts.append(
            {
                "local_competitor_id": local_id,
                "competitor_id": competitor_id,
                "evidence_digest": packet.content_digest,
                "formula": formula.to_dict(),
                "ml": machine.to_dict(),
                "pool": pool.to_dict(),
                "predicted_time_ms": pool.median_ms,
                "history_count": len(observations),
                "std_dev_seconds": statistics.pstdev(sample.samples_ms) / 1000,
                "sampling_seed": sample.seed,
                "samples_digest": sample.samples_digest,
            }
        )
    basis = {
        "protocol": PROTOCOL,
        "readiness": readiness,
        "request": payload,
        "workbook_sha256": history.source_sha256,
        "epoch_digest": epoch.content_digest,
        "forecasts": forecasts,
    }
    source_digest = canonical_digest(basis, max_bytes=20_000_000)
    if forecast_only:
        return {
            "protocol": PROTOCOL,
            "purpose": "numeric_preview_only",
            "issued_mark": False,
            "production_ready": False,
            "readiness": readiness,
            "request_digest": canonical_digest(payload),
            "source_digest": source_digest,
            "workbook_sha256": history.source_sha256,
            "excluded_history": dict(history.excluded),
            "forecasts": forecasts,
            "rows": [
                {
                    "competitor_id": local_id,
                    "predicted_time": item["predicted_time_ms"] / 1000,
                    "engine_version": readiness["package_version"],
                    "std_dev": item["std_dev_seconds"],
                    "method_used": "V3 Linux pre-field numeric candidate",
                    "history_count": item["history_count"],
                }
                for local_id, item in zip(local_ids, forecasts)
            ],
            "warnings": [
                "unpromoted_linux_numeric_candidate",
                "llm_council_unavailable",
                "equal_bootstrap_weights",
                "no_marks_in_pre_field_forecast",
            ],
        }
    competitors = tuple(
        OptimizationCompetitor(
            StableIdentifier(item["competitor_id"]),
            pool.median_ms,
            sampled[index],
            index,
            pool.digest,
        )
        for index, (item, pool) in enumerate(zip(forecasts, pools))
    )
    field = OptimizationField.create(
        field_id=StableIdentifier(payload["field_id"]),
        source_receipt_digest=source_digest,
        competitors=competitors,
    )
    optimized = optimize_and_verify_field(field, ceiling=payload["ceiling"])
    receipt = optimized.receipt
    return {
        "protocol": PROTOCOL,
        "purpose": "numeric_preview_only",
        "issued_mark": False,
        "production_ready": False,
        "readiness": readiness,
        "request_digest": canonical_digest(payload),
        "source_digest": source_digest,
        "workbook_sha256": history.source_sha256,
        "excluded_history": dict(history.excluded),
        "forecasts": forecasts,
        "optimizer": receipt.to_dict(),
        "rows": [
            {
                "competitor_id": local_id,
                "predicted_time": item["predicted_time_ms"] / 1000,
                "proposed_mark": mark,
                "engine_version": readiness["package_version"],
                "std_dev": item["std_dev_seconds"],
                "method_used": "V3 Linux numeric candidate (Formula + ML)",
                "history_count": item["history_count"],
            }
            for local_id, item, mark in zip(local_ids, forecasts, receipt.selected_marks)
        ],
        "warnings": [
            "unpromoted_linux_numeric_candidate",
            "llm_council_unavailable",
            "equal_bootstrap_weights",
            "independent_bootstrap_dependence",
            "preview_does_not_authorize_issue",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="V3 Linux numeric candidate subprocess; preview output cannot authorize issue."
    )
    parser.add_argument("operation", choices=("status", "forecast", "preview"))
    parser.add_argument("--ml-bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "status":
            response = status(args.ml_bundle)
        else:
            raw = sys.stdin.buffer.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("candidate request exceeds byte limit")
            response = preview(
                args.ml_bundle, json.loads(raw), forecast_only=args.operation == "forecast"
            )
        sys.stdout.buffer.write(canonical_bytes(response, max_bytes=20_000_000) + b"\n")
    except Exception as error:
        print(f"V3 candidate failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
