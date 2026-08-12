"""Tests for chronological V2 validation and conformal calibration."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from strathmark.prediction_v2 import ChronologicalCalibrator
from strathmark.validation import (
    chronological_backtest,
    finite_sample_higher_quantile,
)


def _residuals(event: str, history_count: int, count: int, score: float) -> list[dict]:
    return [
        {
            "event": event,
            "history_count": history_count,
            "absolute_log_residual": score + index / 100_000,
            "result_date": date(2025, 1, 1) + timedelta(days=index),
        }
        for index in range(count)
    ]


def test_finite_sample_higher_quantile_is_conservative_and_deterministic():
    scores = np.arange(1, 11, dtype=float)
    assert finite_sample_higher_quantile(scores, 0.90) == 10.0
    assert finite_sample_higher_quantile(scores, 0.50) == 6.0


def test_conformal_pooling_prefers_cohort_then_event_then_global():
    rows = (
        _residuals("SB", 0, 30, 0.10)
        + _residuals("SB", 5, 25, 0.20)
        + _residuals("UH", 5, 60, 0.30)
        + _residuals("UH", 0, 50, 0.40)
    )
    calibrator = ChronologicalCalibrator.fit(pd.DataFrame(rows), version="cal-v1")

    cohort = calibrator.radius("SB", 0)
    event = calibrator.radius("SB", 5)
    global_pool = calibrator.radius("XX", 0)

    assert cohort.scope == "event_history_band"
    assert event.scope == "event"
    assert global_pool.scope == "global"
    assert cohort.calibrated and event.calibrated and global_pool.calibrated


def test_conformal_falls_back_to_analytic_when_all_pools_are_sparse():
    calibrator = ChronologicalCalibrator.fit(pd.DataFrame(_residuals("SB", 0, 20, 0.1)))
    radius = calibrator.radius("SB", 0)

    assert not radius.calibrated
    assert radius.scope == "analytic"
    assert radius.value is None


def test_chronological_backtest_refits_without_future_rows(monkeypatch):
    from strathmark import validation

    rows = []
    start = date(2020, 1, 1)
    for index in range(24):
        rows.append(
            {
                "competitor_id": f"C{index % 6}",
                "event": "SB" if index % 2 == 0 else "UH",
                "time_seconds": 40.0 + (index % 5),
                "result_date": start + timedelta(days=45 * index),
                "diameter_mm": 280.0 + (index % 3) * 20,
                "species": "S01",
                "gender": "M",
                "janka_hardness": 1700.0,
                "specific_gravity": 0.35,
                "crush_strength": 4200.0,
                "shear_strength": 950.0,
                "modulus_of_rupture": 8500.0,
                "modulus_of_elasticity": 1_200_000.0,
                "species_missing": False,
            }
        )
    frame = pd.DataFrame(rows)
    observed: list[tuple[date, date]] = []
    original = validation.PredictionV2Model.fit

    def recording_fit(training, *, training_cutoff, **kwargs):
        observed.append((max(training["result_date"]), training_cutoff))
        return original(training, training_cutoff=training_cutoff, **kwargs)

    monkeypatch.setattr(validation.PredictionV2Model, "fit", staticmethod(recording_fit))
    report = chronological_backtest(frame, min_training_rows=10)

    assert len(report.predictions) > 0
    assert all(max_date < cutoff for max_date, cutoff in observed)
    assert report.predictions["result_date"].is_monotonic_increasing
    assert set(report.metrics) >= {"mae", "rmse", "median_absolute_error", "count"}


def test_calibrator_round_trips_inside_core_artifact():
    from strathmark.prediction_v2 import PredictionV2Model
    from tests.test_prediction_v2 import _training_frame

    model = PredictionV2Model.fit(
        _training_frame(), training_cutoff=date(2024, 1, 1)
    ).with_calibration(
        ChronologicalCalibrator.fit(
            pd.DataFrame(_residuals("SB", 0, 30, 0.12) + _residuals("UH", 0, 30, 0.15)),
            version="cal-v1",
        )
    )
    restored = PredictionV2Model.from_json(model.to_json())

    assert restored.calibration == model.calibration
