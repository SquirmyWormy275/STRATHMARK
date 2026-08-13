"""Tests for chronological V2 validation and conformal calibration."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from strathmark.prediction_v2 import ChronologicalCalibrator
from strathmark.validation import (
    build_residual_training_frame,
    chronological_backtest,
    evaluate_core_promotion,
    finite_sample_higher_quantile,
    partition_benchmark_roles,
    strict_incumbent_backtest,
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


def test_active_calibration_rejects_undated_evidence():
    residuals = pd.DataFrame(_residuals("SB", 0, 50, 0.1)).drop(columns="result_date")

    with pytest.raises(ValueError, match="evidence date"):
        ChronologicalCalibrator.fit(residuals, version="undated-calibration")


def test_calibration_excludes_undated_rows_from_all_pools():
    dated = pd.DataFrame(_residuals("SB", 0, 100, 0.1))
    undated = dated.copy()
    undated["result_date"] = None
    undated["absolute_log_residual"] = 99.0
    mixed = pd.concat([dated, undated], ignore_index=True)

    expected = ChronologicalCalibrator.fit(dated, version="dated-only")
    actual = ChronologicalCalibrator.fit(mixed, version="mixed")

    assert actual.cohort_counts == expected.cohort_counts
    assert actual.event_counts == expected.event_counts
    assert actual.global_radius == expected.global_radius
    assert actual.cohort_radii == expected.cohort_radii


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


def test_residual_training_frame_uses_only_prior_fold_core_residuals(monkeypatch):
    from strathmark import validation
    from tests.test_prediction_v2 import _training_frame

    evidence = _training_frame()
    observed: list[tuple[date, date]] = []
    original = validation.PredictionV2Model.fit

    def recording_fit(training, *, training_cutoff, **kwargs):
        observed.append((max(training["result_date"]), training_cutoff))
        return original(training, training_cutoff=training_cutoff, **kwargs)

    monkeypatch.setattr(validation.PredictionV2Model, "fit", staticmethod(recording_fit))
    residuals = build_residual_training_frame(evidence, min_training_rows=10)

    assert not residuals.empty
    assert all(max_date < cutoff for max_date, cutoff in observed)
    assert (residuals["fold_training_max_date"] < residuals["fold_training_cutoff"]).all()
    assert np.allclose(
        residuals["core_log_residual"],
        np.log(residuals["actual_time"]) - residuals["core_log_location"],
    )


def test_residual_training_features_use_the_serving_diameter_clip():
    from tests.test_prediction_v2 import _training_frame

    evidence = _training_frame().copy()
    target_index = evidence["result_date"].idxmax()
    evidence.loc[target_index, "diameter_mm"] = 2000.0
    residuals = build_residual_training_frame(evidence, min_training_rows=10)
    target = residuals[
        (residuals["competitor_id"] == evidence.loc[target_index, "competitor_id"])
        & (residuals["event"] == evidence.loc[target_index, "event"])
        & (residuals["result_date"] == evidence.loc[target_index, "result_date"])
    ]

    assert len(target) == 1
    clipped = float(np.exp(target.iloc[0]["log_diameter_ratio"]) * 300.0)
    assert clipped < 2000.0


def test_partition_benchmark_roles_uses_exact_exclusive_windows():
    from tests.test_prediction_v2 import _training_frame

    evidence = _training_frame().copy()
    evidence["result_date"] = [
        date(2023, 12, 31) + timedelta(days=index * 120) for index in range(len(evidence))
    ]
    manifest = {
        "roles": {
            "fit": {"start": "1900-01-01", "end_exclusive": "2024-01-01"},
            "selection": {"start": "2024-01-01", "end_exclusive": "2025-01-01"},
            "calibration": {"start": "2025-01-01", "end_exclusive": "2025-07-01"},
            "locked_test": {"start": "2025-07-01", "end_exclusive": "2026-02-07"},
        }
    }

    roles = partition_benchmark_roles(evidence, manifest)

    for name, spec in manifest["roles"].items():
        assert (roles[name]["result_date"] >= date.fromisoformat(spec["start"])).all()
        assert (roles[name]["result_date"] < date.fromisoformat(spec["end_exclusive"])).all()


def test_strict_incumbent_is_prior_only_and_same_event():
    from tests.test_prediction_v2 import _training_frame

    evidence = _training_frame()
    start = min(evidence["result_date"]) + timedelta(days=180)
    report = strict_incumbent_backtest(
        evidence,
        target_start=start,
        target_end_exclusive=date(2030, 1, 1),
        min_training_rows=4,
    )

    assert not report.predictions.empty
    assert (
        report.predictions["fold_training_max_date"] < report.predictions["fold_training_cutoff"]
    ).all()
    assert set(report.predictions["incumbent_history_event"].unique()) <= {"SB", "UH"}
    assert (report.predictions["incumbent_history_event"] == report.predictions["event"]).all()


def test_core_promotion_gate_applies_locked_thresholds():
    passing = evaluate_core_promotion(
        {"mae": 9.8, "rmse": 12.05, "count": 128},
        {"mae": 10.0, "rmse": 12.0, "count": 128},
    )
    failing = evaluate_core_promotion(
        {"mae": 9.95, "rmse": 12.07, "count": 128},
        {"mae": 10.0, "rmse": 12.0, "count": 128},
    )

    assert passing["promoted"] is True
    assert passing["mae_relative_improvement"] == pytest.approx(0.02)
    assert failing["promoted"] is False
    assert "mae_gate_failed" in failing["reasons"]
    assert "rmse_gate_failed" in failing["reasons"]
