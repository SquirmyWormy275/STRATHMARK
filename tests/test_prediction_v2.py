"""Focused contract tests for the dependable Prediction Engine V2 core."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from strathmark.features import MISSING_CATEGORY, MODEL_EVIDENCE_FIELDS
from strathmark.prediction_v2 import (
    ARTIFACT_MAX_BYTES,
    PredictionV2Model,
    PredictionV2Request,
)

PROPERTIES = {
    "janka_hardness": 1700.0,
    "specific_gravity": 0.35,
    "crush_strength": 4200.0,
    "shear_strength": 950.0,
    "modulus_of_rupture": 8500.0,
    "modulus_of_elasticity": 1_200_000.0,
}


def _row(
    competitor_id: str,
    event: str,
    when: date,
    seconds: float,
    *,
    diameter: float = 300.0,
    species: str = "S01",
    gender: str = "M",
    species_missing: bool = False,
) -> dict:
    return {
        "competitor_id": competitor_id,
        "event": event,
        "time_seconds": seconds,
        "result_date": when,
        "diameter_mm": diameter,
        "species": species,
        "gender": gender,
        **PROPERTIES,
        "species_missing": species_missing,
    }


def _training_frame(n_competitors: int = 24) -> pd.DataFrame:
    rows = []
    start = date(2022, 1, 1)
    for index in range(n_competitors):
        ability = (index - n_competitors / 2) * 0.008
        # Alternate gender so the synthetic ability gradient is balanced and
        # the population fit can recover the independent gender signal.
        gender = "F" if index % 2 == 0 else "M"
        for event, base, slope in (("SB", 42.0, 1.22), ("UH", 68.0, 1.08)):
            for attempt in range(4):
                diameter = 260.0 + 20.0 * attempt
                log_time = (
                    math.log(base)
                    + slope * math.log(diameter / 300.0)
                    + ability
                    + (0.055 if gender == "F" else 0.0)
                    + (attempt % 2) * 0.004
                )
                rows.append(
                    _row(
                        f"C{index:03d}",
                        event,
                        start + timedelta(days=index * 7 + attempt * 60),
                        math.exp(log_time),
                        diameter=diameter,
                        gender=gender,
                    )
                )
    return pd.DataFrame(rows, columns=MODEL_EVIDENCE_FIELDS)


def _request(**updates) -> PredictionV2Request:
    values = {
        "competitor_id": "NEW",
        "event": "SB",
        "diameter_mm": 300.0,
        "species": "S01",
        "gender": "M",
        "prediction_as_of": date(2025, 1, 1),
        **PROPERTIES,
    }
    values.update(updates)
    return PredictionV2Request(**values)


@pytest.fixture
def model() -> PredictionV2Model:
    return PredictionV2Model.fit(
        _training_frame(),
        training_cutoff=date(2024, 1, 1),
        model_version="synthetic-v1",
    )


def test_population_core_recovers_event_diameter_and_gender_signal(model):
    sb_300 = model.predict(_request())
    sb_330 = model.predict(_request(diameter_mm=330.0))
    uh_300 = model.predict(_request(event="UH"))
    female = model.predict(_request(gender="F"))

    assert sb_300.source == "conditional_population_prior"
    assert sb_300.median == pytest.approx(42.0, rel=0.12)
    assert sb_330.median > sb_300.median
    assert uh_300.median > sb_300.median
    assert female.median > sb_300.median


def test_request_history_owns_state_and_intervals_narrow_smoothly(model):
    cutoff = date(2025, 1, 1)
    histories = []
    for count in (0, 1, 3, 8):
        rows = [
            _row("NEW", "SB", cutoff - timedelta(days=60 * (index + 1)), 35.0 + index * 0.1)
            for index in range(count)
        ]
        histories.append(pd.DataFrame(rows, columns=MODEL_EVIDENCE_FIELDS))

    predictions = [model.predict(_request(prediction_as_of=cutoff), history=h) for h in histories]
    widths = [p.interval.upper - p.interval.lower for p in predictions]

    assert predictions[0].source == "conditional_population_prior"
    assert predictions[-1].source == "hierarchical_dynamic_core"
    assert predictions[-1].median < predictions[0].median
    assert widths == sorted(widths, reverse=True)


def test_history_exclusive_cutoff_prevents_same_day_and_future_access(model):
    cutoff = date(2025, 1, 1)
    prior = pd.DataFrame([_row("NEW", "SB", cutoff - timedelta(days=1), 36.0)])
    contaminated = pd.concat(
        [
            prior,
            pd.DataFrame(
                [
                    _row("NEW", "SB", cutoff, 5.0),
                    _row("NEW", "SB", cutoff + timedelta(days=1), 5.0),
                ]
            ),
        ],
        ignore_index=True,
    )

    clean_prediction = model.predict(_request(prediction_as_of=cutoff), history=prior)
    contaminated_prediction = model.predict(_request(prediction_as_of=cutoff), history=contaminated)

    assert contaminated_prediction == clean_prediction


def test_unknown_metadata_widens_uncertainty_without_breaking_prediction(model):
    known = model.predict(_request())
    unknown = model.predict(
        _request(
            species="UNSEEN",
            gender=MISSING_CATEGORY,
            species_missing=True,
        )
    )

    assert unknown.median > 0
    assert unknown.log_scale > known.log_scale
    assert "unknown_species" in unknown.warnings
    assert "unknown_gender" in unknown.warnings


def test_extreme_diameter_is_clamped_and_reported(model):
    extreme = model.predict(_request(diameter_mm=2000.0))
    edge = model.predict(_request(diameter_mm=model.diameter_support["SB"][1]))

    assert extreme.median == pytest.approx(edge.median)
    assert "diameter_outside_training_support" in extreme.warnings
    assert extreme.log_scale > edge.log_scale


def test_outlier_cannot_dominate_population_or_competitor_state(model):
    cutoff = date(2025, 1, 1)
    normal_history = pd.DataFrame(
        [_row("NEW", "SB", cutoff - timedelta(days=90 * (i + 1)), 37.0) for i in range(5)]
    )
    outlier_history = pd.concat(
        [normal_history, pd.DataFrame([_row("NEW", "SB", cutoff - timedelta(days=10), 9999.0)])],
        ignore_index=True,
    )

    normal = model.predict(_request(prediction_as_of=cutoff), history=normal_history)
    outlier = model.predict(_request(prediction_as_of=cutoff), history=outlier_history)

    assert outlier.median < normal.median * 1.35


def test_notes_caps_and_penalty_labels_are_never_inferred(model):
    cutoff = date(2025, 1, 1)
    base = pd.DataFrame([_row("NEW", "SB", cutoff - timedelta(days=10), 500.0)])
    annotated = base.assign(notes="DNF penalty timeout", cap=180, quality=1, heat="final")

    assert model.predict(_request(), history=base) == model.predict(_request(), history=annotated)


def test_cross_event_borrowing_is_learned_only_with_enough_pairs():
    frame = _training_frame(24)
    model = PredictionV2Model.fit(frame, training_cutoff=date(2024, 1, 1))
    assert abs(model.cross_event_coefficients["SB_from_UH"]) > 0.05
    assert abs(model.cross_event_coefficients["SB_from_UH"]) <= 0.75

    small = PredictionV2Model.fit(_training_frame(4), training_cutoff=date(2024, 1, 1))
    assert small.cross_event_coefficients == {"SB_from_UH": 0.0, "UH_from_SB": 0.0}


def test_single_event_artifact_uses_static_prior_for_absent_event():
    frame = _training_frame().query("event == 'SB'")
    model = PredictionV2Model.fit(frame, training_cutoff=date(2024, 1, 1))

    present = model.predict(_request(event="SB"))
    absent = model.predict(_request(event="UH"))

    assert present.source == "conditional_population_prior"
    assert absent.source == "broad_event_prior"
    assert "event_absent_from_artifact" in absent.warnings


def test_artifact_round_trip_checksum_and_backdated_rejection(model):
    encoded = model.to_json()
    restored = PredictionV2Model.from_json(encoded)
    request = _request()

    assert restored.predict(request) == model.predict(request)
    envelope = json.loads(encoded)
    envelope["payload"]["coefficients"][0] += 1.0
    with pytest.raises(ValueError, match="checksum"):
        PredictionV2Model.from_json(json.dumps(envelope))
    with pytest.raises(ValueError, match="maximum"):
        PredictionV2Model.from_json(" " * (ARTIFACT_MAX_BYTES + 1))

    backdated = model.predict(_request(prediction_as_of=date(2022, 1, 1)))
    assert backdated.source == "broad_event_prior"
    assert backdated.degraded
    assert "artifact_newer_than_prediction_cutoff" in backdated.warnings


def test_inference_serialization_and_positive_samples_are_deterministic(model):
    history = _training_frame().query("competitor_id == 'C000'")
    first = model.predict(_request(competitor_id="C000"), history=history)
    second = model.predict(_request(competitor_id="C000"), history=history)

    assert first == second
    np.testing.assert_array_equal(first.sample(32, seed=7), second.sample(32, seed=7))
    assert np.all(first.sample(32, seed=7) > 0)
    assert model.to_json() == model.to_json()
