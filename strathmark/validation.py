"""Leakage-safe chronological validation helpers for Prediction Engine V2."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Mapping

import numpy as np
import pandas as pd

from strathmark.features import MODEL_EVIDENCE_FIELDS, SPECIES_PROPERTY_FIELDS
from strathmark.prediction_v2 import (
    ChronologicalCalibrator,
    PredictionV2Model,
    PredictionV2Request,
    _finite_sample_higher_quantile,
    history_band,
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
    target_start: date | None = None,
    target_end_exclusive: date | None = None,
    calibration: ChronologicalCalibrator | None = None,
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

    target_dates = frame["result_date"]
    if target_start is not None:
        target_dates = target_dates[target_dates >= target_start]
    if target_end_exclusive is not None:
        target_dates = target_dates[target_dates < target_end_exclusive]

    for cutoff in sorted(target_dates.unique()):
        training = frame[frame["result_date"] < cutoff].copy()
        targets = frame[frame["result_date"] == cutoff]
        if len(training) < min_training_rows:
            continue
        model = PredictionV2Model.fit(
            training,
            training_cutoff=cutoff,
            model_version=model_version,
        )
        if calibration is not None:
            model = model.with_calibration(calibration)
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
                    "fold_training_cutoff": cutoff,
                    "fold_training_max_date": max(training["result_date"]),
                    "gender": str(row["gender"]),
                    "species": str(row["species"]),
                    "species_missing": bool(row["species_missing"]),
                    "diameter_mm": float(row["diameter_mm"]),
                    "log_diameter_ratio": math.log(float(row["diameter_mm"]) / 300.0),
                    **{name: float(row[name]) for name in SPECIES_PROPERTY_FIELDS},
                    "history_count": distribution.history_count,
                    "effective_history_weight": distribution.effective_history_weight,
                    "same_event_state": float(distribution.metadata.get("same_event_state", 0.0)),
                    "trend_projection": float(distribution.metadata.get("trend_projection", 0.0)),
                    "cross_event_state": float(distribution.metadata.get("cross_event_state", 0.0)),
                    "actual_time": actual,
                    "predicted_median": distribution.median,
                    "core_log_location": distribution.log_location,
                    "core_lower": distribution.interval.lower,
                    "core_upper": distribution.interval.upper,
                    "interval_scope": distribution.interval.scope,
                    "interval_calibration_state": distribution.interval.calibration_state,
                    "absolute_error": abs(actual - distribution.median),
                    "squared_error": (actual - distribution.median) ** 2,
                    "absolute_log_residual": abs(math.log(actual) - distribution.log_location),
                    "core_log_residual": math.log(actual) - distribution.log_location,
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
    bands = output["history_count"].map(history_band)
    for band in ("0", "1-3", "4+"):
        subset = output[bands == band]
        if not subset.empty:
            cohort_metrics[band] = _point_metrics(subset)
    return BacktestReport(output, metrics, cohort_metrics)


def build_residual_training_frame(
    evidence: pd.DataFrame,
    *,
    min_training_rows: int = 30,
    model_version: str = "prediction-v2-residual-fold-core",
) -> pd.DataFrame:
    """Return only core residuals produced by strictly-prior rolling fits."""

    report = chronological_backtest(
        evidence,
        min_training_rows=min_training_rows,
        model_version=model_version,
    )
    return report.predictions.copy()


def partition_benchmark_roles(
    evidence: pd.DataFrame,
    manifest: Mapping[str, object],
) -> dict[str, pd.DataFrame]:
    """Partition canonical evidence using the manifest's exclusive date windows."""

    if "result_date" not in evidence:
        raise ValueError("benchmark evidence missing result_date")
    raw_roles = manifest.get("roles")
    if not isinstance(raw_roles, Mapping):
        raise ValueError("benchmark manifest roles must be an object")
    required = ("fit", "selection", "calibration", "locked_test")
    if set(raw_roles) != set(required):
        raise ValueError("benchmark manifest must define the four fixed roles")
    frame = evidence.copy()
    frame["result_date"] = pd.to_datetime(frame["result_date"], errors="coerce", utc=True).dt.date
    if frame["result_date"].isna().any():
        raise ValueError("benchmark evidence contains invalid or undated rows")
    roles: dict[str, pd.DataFrame] = {}
    for name in required:
        spec = raw_roles[name]
        if not isinstance(spec, Mapping):
            raise ValueError(f"benchmark role {name!r} must be an object")
        try:
            start = date.fromisoformat(str(spec["start"]))
            end = date.fromisoformat(str(spec["end_exclusive"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"benchmark role {name!r} has invalid dates") from exc
        if start >= end:
            raise ValueError(f"benchmark role {name!r} must have a positive window")
        roles[name] = frame[(frame["result_date"] >= start) & (frame["result_date"] < end)].copy()
    return roles


def strict_incumbent_backtest(
    evidence: pd.DataFrame,
    *,
    target_start: date,
    target_end_exclusive: date,
    min_training_rows: int = 30,
) -> BacktestReport:
    """Evaluate the fixed prior-only same-event incumbent.

    For every target date the incumbent learns one event-level log-diameter
    exponent from strictly earlier valid rows. It normalizes prior same-event
    times to the target diameter, uses 730-day recency weights for the target
    competitor, and shrinks that estimate toward the event population median
    with the fixed denominator 5. No species, gender, cross-event, or future
    observation enters this deliberately simple release comparator.
    """

    missing = set(MODEL_EVIDENCE_FIELDS) - set(evidence.columns)
    if missing:
        raise ValueError(f"incumbent evidence missing columns: {sorted(missing)}")
    frame = evidence.loc[:, MODEL_EVIDENCE_FIELDS].copy()
    frame["result_date"] = pd.to_datetime(frame["result_date"], errors="coerce", utc=True).dt.date
    frame = frame.dropna(subset=["result_date"]).sort_values(
        ["result_date", "competitor_id", "event"], kind="mergesort"
    )
    target_dates = frame.loc[
        (frame["result_date"] >= target_start) & (frame["result_date"] < target_end_exclusive),
        "result_date",
    ]
    predictions: list[dict[str, object]] = []
    for cutoff in sorted(target_dates.unique()):
        training = frame[frame["result_date"] < cutoff].copy()
        if len(training) < min_training_rows:
            continue
        targets = frame[frame["result_date"] == cutoff]
        for _, row in targets.iterrows():
            event = str(row["event"]).upper()
            event_training = training[training["event"].astype(str).str.upper() == event].copy()
            if event_training.empty:
                continue
            predicted, history_count, exponent = _strict_incumbent_prediction(
                event_training,
                competitor_id=str(row["competitor_id"]),
                target_diameter=float(row["diameter_mm"]),
                cutoff=cutoff,
            )
            actual = float(row["time_seconds"])
            predictions.append(
                {
                    "competitor_id": str(row["competitor_id"]),
                    "event": event,
                    "result_date": cutoff,
                    "fold_training_cutoff": cutoff,
                    "fold_training_max_date": max(training["result_date"]),
                    "incumbent_history_event": event,
                    "history_count": history_count,
                    "diameter_exponent": exponent,
                    "actual_time": actual,
                    "predicted_median": predicted,
                    "absolute_error": abs(actual - predicted),
                    "squared_error": (actual - predicted) ** 2,
                }
            )
    output = pd.DataFrame(predictions)
    if output.empty:
        return BacktestReport(
            output,
            {"count": 0.0, "mae": math.nan, "rmse": math.nan, "median_absolute_error": math.nan},
            {},
        )
    output = output.sort_values(
        ["result_date", "competitor_id", "event"], kind="mergesort"
    ).reset_index(drop=True)
    metrics = _point_metrics(output)
    cohorts: dict[str, dict[str, float]] = {}
    bands = output["history_count"].map(history_band)
    for band in ("0", "1-3", "4+"):
        subset = output[bands == band]
        if not subset.empty:
            cohorts[band] = _point_metrics(subset)
    return BacktestReport(output, metrics, cohorts)


def evaluate_core_promotion(
    core_metrics: Mapping[str, float],
    incumbent_metrics: Mapping[str, float],
    *,
    minimum_rows: int = 100,
    minimum_mae_relative_improvement: float = 0.01,
    maximum_rmse_relative_worsening: float = 0.005,
) -> dict[str, object]:
    """Apply the fixed core release gate and return an auditable decision."""

    core_count = int(core_metrics.get("count", 0))
    incumbent_count = int(incumbent_metrics.get("count", 0))
    core_mae = float(core_metrics.get("mae", math.nan))
    incumbent_mae = float(incumbent_metrics.get("mae", math.nan))
    core_rmse = float(core_metrics.get("rmse", math.nan))
    incumbent_rmse = float(incumbent_metrics.get("rmse", math.nan))
    values = (core_mae, incumbent_mae, core_rmse, incumbent_rmse)
    mae_improvement = (
        (incumbent_mae - core_mae) / incumbent_mae
        if incumbent_mae > 0 and all(math.isfinite(value) for value in values)
        else math.nan
    )
    rmse_worsening = (
        (core_rmse - incumbent_rmse) / incumbent_rmse
        if incumbent_rmse > 0 and all(math.isfinite(value) for value in values)
        else math.nan
    )
    reasons: list[str] = []
    if core_count < minimum_rows or incumbent_count != core_count:
        reasons.append("global_sample_gate_failed")
    if not math.isfinite(mae_improvement) or (
        mae_improvement + 1e-12 < minimum_mae_relative_improvement
    ):
        reasons.append("mae_gate_failed")
    if not math.isfinite(rmse_worsening) or (
        rmse_worsening > maximum_rmse_relative_worsening + 1e-12
    ):
        reasons.append("rmse_gate_failed")
    return {
        "promoted": not reasons,
        "reasons": reasons,
        "common_rows": core_count if core_count == incumbent_count else 0,
        "core_mae": core_mae,
        "incumbent_mae": incumbent_mae,
        "mae_relative_improvement": mae_improvement,
        "core_rmse": core_rmse,
        "incumbent_rmse": incumbent_rmse,
        "rmse_relative_worsening": rmse_worsening,
        "thresholds": {
            "minimum_rows": minimum_rows,
            "minimum_mae_relative_improvement": minimum_mae_relative_improvement,
            "maximum_rmse_relative_worsening": maximum_rmse_relative_worsening,
        },
    }


def _strict_incumbent_prediction(
    event_training: pd.DataFrame,
    *,
    competitor_id: str,
    target_diameter: float,
    cutoff: date,
) -> tuple[float, int, float]:
    diameters = event_training["diameter_mm"].astype(float).to_numpy()
    low, high = np.quantile(diameters, [0.01, 0.99])
    bounded_diameters = np.clip(diameters, low, high)
    log_diameter = np.log(bounded_diameters / 300.0)
    log_time = np.log(event_training["time_seconds"].astype(float).to_numpy())
    centered_x = log_diameter - float(np.median(log_diameter))
    centered_y = log_time - float(np.median(log_time))
    exponent = float(
        np.clip(
            np.dot(centered_x, centered_y) / (np.dot(centered_x, centered_x) + 0.10),
            0.0,
            3.0,
        )
    )
    bounded_target = float(np.clip(target_diameter, low, high))
    normalized = event_training["time_seconds"].astype(float).to_numpy() * np.power(
        bounded_target / bounded_diameters,
        exponent,
    )
    event_population = float(np.median(normalized))
    personal_mask = event_training["competitor_id"].astype(str).to_numpy() == competitor_id
    history_count = int(np.sum(personal_mask))
    if history_count == 0:
        return event_population, 0, exponent
    dates = event_training.loc[personal_mask, "result_date"].tolist()
    ages = np.asarray([(cutoff - value).days for value in dates], dtype=float)
    weights = np.exp(-math.log(2.0) * ages / 730.0)
    personal = float(np.average(normalized[personal_mask], weights=weights))
    effective = float(np.sum(weights))
    shrinkage = effective / (effective + 5.0)
    return shrinkage * personal + (1.0 - shrinkage) * event_population, history_count, exponent


def _point_metrics(frame: pd.DataFrame) -> dict[str, float]:
    return {
        "count": float(len(frame)),
        "mae": float(frame["absolute_error"].mean()),
        "rmse": float(math.sqrt(frame["squared_error"].mean())),
        "median_absolute_error": float(frame["absolute_error"].median()),
    }


__all__ = [
    "BacktestReport",
    "build_residual_training_frame",
    "chronological_backtest",
    "evaluate_core_promotion",
    "finite_sample_higher_quantile",
    "fit_chronological_calibration",
    "partition_benchmark_roles",
    "strict_incumbent_backtest",
]
