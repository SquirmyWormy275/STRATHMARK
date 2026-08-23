from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, getcontext
from pathlib import Path

from strathmark.v3.assessors.formula import FormulaManifest, assess_formula
from strathmark.v3.contracts.evidence import EvidencePacket
from strathmark.v3.contracts.identifiers import StableIdentifier
from tests.v3.unit.test_formula import (
    _epoch_for_observations,
    context,
    evidence,
    formula_input,
    observation,
)

MANIFEST = FormulaManifest.load(Path("benchmarks/v3/formula_manifest.json"))


def numeric(result: object) -> tuple[object, ...]:
    return (
        result.center_ms,  # type: ignore[attr-defined]
        result.uncertainty_ms,  # type: ignore[attr-defined]
        result.log_center,  # type: ignore[attr-defined]
        result.log_scale,  # type: ignore[attr-defined]
        result.effective_sample_size,  # type: ignore[attr-defined]
        result.forecast.distribution,  # type: ignore[attr-defined]
    )


def test_permutation_and_opaque_identifier_renaming_do_not_change_numeric_forecast() -> None:
    observations = (observation(1, 39000), observation(2, 41000), observation(3, 40000))
    original = assess_formula(formula_input(evidence(*observations)), MANIFEST)
    renamed_observations = tuple(
        replace(
            item,
            competitor_id=StableIdentifier("competitor:opaque-z"),
            evidence_id=StableIdentifier(f"evidence:renamed-{item.observation_sequence}"),
            tournament_id=StableIdentifier("tournament:renamed"),
            round_id=StableIdentifier("round:renamed"),
            field_id=StableIdentifier(f"field:renamed-{item.observation_sequence}"),
            source_digest=f"{item.observation_sequence + 10:064x}",
        )
        for item in reversed(observations)
    )
    ordered_renamed = tuple(
        sorted(renamed_observations, key=lambda item: item.observation_sequence)
    )
    renamed_epoch = _epoch_for_observations(ordered_renamed, 3)
    renamed = EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:opaque-z"),
        target_context=context(),
        observations=ordered_renamed,
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:2026-08-01",
        tournament_epoch_id=renamed_epoch.epoch_id,
        tournament_event_sequence=3,
    )
    changed = assess_formula(formula_input(renamed, active_tournament="renamed"), MANIFEST)
    assert numeric(changed) == numeric(original)


def test_fixed_manifest_evidence_only_plus_five_seconds_has_declared_response() -> None:
    original = assess_formula(
        formula_input(evidence(observation(1, 39000), observation(2, 41000))), MANIFEST
    )
    shifted = assess_formula(
        formula_input(evidence(observation(1, 44000), observation(2, 46000))), MANIFEST
    )
    assert MANIFEST.digest == "2c58a9527c77a33e0b813fe938db44c6298ac0ea8b543a199b720d97baaf1354"
    assert (original.center_ms, original.uncertainty_ms) == (43184, 29604)
    assert (shifted.center_ms, shifted.uncertainty_ms) == (44996, 30209)
    assert shifted.center_ms > original.center_ms


def test_size_conversion_direction_reverses_and_diameter_similarity_is_symmetric() -> None:
    larger = assess_formula(
        formula_input(
            evidence(
                observation(1, 40000, observed_context=context(size=250)), target=context(size=300)
            )
        ),
        MANIFEST,
    )
    smaller = assess_formula(
        formula_input(
            evidence(
                observation(1, 40000, observed_context=context(size=300)), target=context(size=250)
            )
        ),
        MANIFEST,
    )
    larger_row = next(row for row in larger.trace if row.stage == "observation")
    smaller_row = next(row for row in smaller.trace if row.stage == "observation")
    assert int(larger_row.details["transformed_time_ms"]) > 40000
    assert int(smaller_row.details["transformed_time_ms"]) < 40000
    assert Decimal(larger_row.details["diameter_similarity"]) == Decimal(
        smaller_row.details["diameter_similarity"]
    )


def test_signed_extreme_influence_is_symmetric_in_log_space_and_bounded() -> None:
    baseline = assess_formula(formula_input(evidence(observation(1, 45000))), MANIFEST)
    prior_log = Decimal("45").ln()
    faster_ms = int((prior_log - Decimal("2")).exp() * 1000)
    slower_ms = int((prior_log + Decimal("2")).exp() * 1000)
    faster = assess_formula(formula_input(evidence(observation(1, faster_ms))), MANIFEST)
    slower = assess_formula(formula_input(evidence(observation(1, slower_ms))), MANIFEST)
    faster_delta = prior_log - Decimal(faster.log_center)
    slower_delta = Decimal(slower.log_center) - prior_log
    assert abs(faster_delta - slower_delta) < Decimal("0.00001")
    assert faster_delta < Decimal("0.1")
    assert baseline.center_ms == 45000


def test_ambient_decimal_context_cannot_change_replay_bytes() -> None:
    packet = formula_input(evidence(observation(1, 38200), observation(2, 40100)))
    expected = assess_formula(packet, MANIFEST).to_dict()
    ambient = getcontext()
    original_precision, original_rounding = ambient.prec, ambient.rounding
    try:
        ambient.prec = 6
        ambient.rounding = "ROUND_UP"
        assert assess_formula(packet, MANIFEST).to_dict() == expected
        assert MANIFEST.prior_sigma_ms > 0
    finally:
        ambient.prec = original_precision
        ambient.rounding = original_rounding
