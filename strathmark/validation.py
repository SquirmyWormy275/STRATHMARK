"""Leakage-safe chronological validation helpers for Prediction Engine V2."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from strathmark.features import MODEL_EVIDENCE_FIELDS, SPECIES_PROPERTY_FIELDS
from strathmark.prediction_v2 import (
    ChronologicalCalibrator,
    PredictionV2Model,
    PredictionV2Request,
    _finite_sample_higher_quantile,
)


@dataclass(frozen=True)
class BacktestReport:
    """Out-of-time row predictions and aggregate point metrics."""

    predictions: pd.DataFrame
    metrics: Mapping[str, float]
    cohort_metrics: Mapping[str, Mapping[str, float]]


def finite_sample_higher_quantile(values: np.ndarray, coverage: float = 0.90) -> float:
    """Return the finite-sample split-conformal ``higher`` quantile."""

    if not math.isfinite(coverage) or not 0 < coverage < 1:
        raise ValueError("coverage must be between zero and one")
    return _finite_sample_higher_quantile(values, coverage)


def fit_chronological_calibration(
    predictions: pd.DataFrame,
    *,
    version: str = "chronological-conformal-v1",
    nominal_coverage: float = 0.90,
) -> ChronologicalCalibrator:
    """Fit the documented chronological pooling hierarchy from later residuals."""

    return ChronologicalCalibrator.fit(
        predictions,
        version=version,
        nominal_coverage=nominal_coverage,
    )


def chronological_backtest(
    evidence: pd.DataFrame,
    *,
    min_training_rows: int = 30,
    model_version: str = "prediction-v2-backtest",
) -> BacktestReport:
    """Refit on strictly earlier dates and predict each later row.

    This intentionally favors clarity and causal correctness over speed.  The
    offline trainer may cache same-cutoff fits; the dependable contract is that
    each validation prediction is produced by a snapshot whose evidence ends
    before its result date.
    """

    missing = set(MODEL_EVIDENCE_FIELDS) - set(evidence.columns)
    if missing:
        raise ValueError(f"backtest evidence missing columns: {sorted(missing)}")
    frame = evidence.loc[:, MODEL_EVIDENCE_FIELDS].copy()
    frame["result_date"] = pd.to_datetime(frame["result_date"], errors="coerce", utc=True).dt.date
    frame = frame.dropna(subset=["result_date"]).sort_values(
        ["result_date", "competitor_id", "event"], kind="mergesort"
    )
    predictions: list[dict] = []

    for cutoff in sorted(frame["result_date"].unique()):
        training = frame[frame["result_date"] < cutoff].copy()
        targets = frame[frame["result_date"] == cutoff]
        if len(training) < min_training_rows:
            continue
        model = PredictionV2Model.fit(
            training,
            training_cutoff=cutoff,
            model_version=model_version,
        )
        for _, row in targets.iterrows():
            request = PredictionV2Request(
                competitor_id=str(row["competitor_id"]),
                event=str(row["event"]),
                diameter_mm=float(row["diameter_mm"]),
                species=str(row["species"]),
                gender=str(row["gender"]),
                prediction_as_of=cutoff,
                species_missing=bool(row["species_missing"]),
                **{name: float(row[name]) for name in SPECIES_PROPERTY_FIELDS},
            )
            distribution = model.predict(request, history=training)
            actual = float(row["time_seconds"])
            predictions.append(
                {
                    "competitor_id": str(row["competitor_id"]),
                    "event": str(row["event"]),
                    "result_date": cutoff,
                    "history_count": distribution.history_count,
                    "actual_time": actual,
                    "predicted_median": distribution.median,
                    "absolute_error": abs(actual - distribution.median),
                    "squared_error": (actual - distribution.median) ** 2,
                    "absolute_log_residual": abs(math.log(actual) - distribution.log_location),
                }
            )

    output = pd.DataFrame(predictions)
    if output.empty:
        metrics = {
            "count": 0.0,
            "mae": math.nan,
            "rmse": math.nan,
            "median_absolute_error": math.nan,
        }
        return BacktestReport(output, metrics, {})
    output = output.sort_values(
        ["result_date", "competitor_id", "event"], kind="mergesort"
    ).reset_index(drop=True)
    metrics = _point_metrics(output)
    cohort_metrics: dict[str, dict[str, float]] = {}
    bands = output["history_count"].map(_history_band)
    for band in ("0", "1-3", "4+"):
        subset = output[bands == band]
        if not subset.empty:
            cohort_metrics[band] = _point_metrics(subset)
    return BacktestReport(output, metrics, cohort_metrics)


def _point_metrics(frame: pd.DataFrame) -> dict[str, float]:
    return {
        "count": float(len(frame)),
        "mae": float(frame["absolute_error"].mean()),
        "rmse": float(math.sqrt(frame["squared_error"].mean())),
        "median_absolute_error": float(frame["absolute_error"].median()),
    }


def _history_band(history_count: int) -> str:
    if history_count <= 0:
        return "0"
    if history_count <= 3:
        return "1-3"
    return "4+"


__all__ = [
    "BacktestReport",
    "chronological_backtest",
    "finite_sample_higher_quantile",
    "fit_chronological_calibration",
]
