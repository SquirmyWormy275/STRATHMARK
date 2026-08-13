"""
Calibration drift detection
===========================

Compares a rolling window of recent residuals against the residual distribution
captured at calibration time. Trusted ledger rows also report direct empirical
coverage from their issued intervals, grouped by nominal coverage. Surfaces
advisory alerts. Never
auto-deactivates a model or triggers retraining — alerts prompt the
operator to consider an early retraining; the operator decides.

Policy reference: `docs/ml-persistence-policy.md` section 3.

Thresholds (from the policy doc):
- Mean residual shift > 1 second        -> alert
- Variance shift > 30%                  -> alert
- 90% conformal coverage < 0.85 or > 0.95 -> alert

Public API:
    evaluate_drift(model_version_id=None, lookback_days=30) -> DriftReport
    is_drifting(model_version_id=None, lookback_days=30)    -> bool
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Mapping, Optional

_log = logging.getLogger(__name__)


# Default thresholds. Live in this module, not config.py, because they are
# drift-specific and changing them requires rerunning calibration to be
# meaningful — they are NOT general-purpose tuning knobs.
MEAN_SHIFT_SECONDS_THRESHOLD: float = 1.0
VARIANCE_RATIO_THRESHOLD: float = 0.30  # |new/baseline - 1| > this -> alert
COVERAGE_LOW_THRESHOLD: float = 0.85
COVERAGE_HIGH_THRESHOLD: float = 0.95
MIN_RECENT_SAMPLES: int = 20  # below this, drift signal is too noisy to act on


@dataclass(frozen=True)
class CoverageCohort:
    """Direct containment evidence for one issued nominal coverage level."""

    nominal_coverage: float
    eligible_count: int
    covered_count: int
    empirical_coverage: Optional[float]
    sample_label: str
    coverage_alert: bool = False


@dataclass
class DriftReport:
    """Result of a drift evaluation. Plain data, no behavior."""

    model_version_id: Optional[str]
    lookback_days: int
    recent_count: int
    baseline_count: int

    # Means and variances; None when sample sizes are too small to compute
    recent_mean: Optional[float] = None
    baseline_mean: Optional[float] = None
    mean_shift: Optional[float] = None  # recent_mean - baseline_mean

    recent_variance: Optional[float] = None
    baseline_variance: Optional[float] = None
    variance_ratio_change: Optional[float] = None  # (recent/baseline) - 1

    # Coverage at 90% from the calibration_tables row, if available
    baseline_coverage_at_90: Optional[float] = None
    # Empirical containment for the issued 90% interval cohort. This is the
    # only nominal cohort compared against the current coverage thresholds.
    recent_coverage_at_90: Optional[float] = None
    coverage_cohorts: dict[str, CoverageCohort] = field(default_factory=dict)
    coverage_unavailable_count: int = 0

    # Boolean flags for each rule
    mean_shift_alert: bool = False
    variance_ratio_alert: bool = False
    coverage_alert: bool = False
    insufficient_recent_samples: bool = False
    sample_label: str = "sample_adequate"

    # Aggregated state
    overall_alert: bool = False

    # Human-readable explanation
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """One-line summary suitable for logging or operator dashboards."""
        if self.overall_alert:
            return (
                f"DRIFT ALERT: model={self.model_version_id} "
                f"recent_n={self.recent_count} "
                f"flags={self._flags_compact()} "
                f"detail={'; '.join(self.notes)}"
            )
        if self.insufficient_recent_samples:
            return (
                f"drift status: insufficient samples (recent_n={self.recent_count}, "
                f"need >= {MIN_RECENT_SAMPLES})"
            )
        mean_shift = "unavailable" if self.mean_shift is None else f"{self.mean_shift:+.2f}s"
        return (
            f"drift status: nominal (model={self.model_version_id}, "
            f"recent_n={self.recent_count}, mean_shift={mean_shift})"
        )

    def _flags_compact(self) -> str:
        flags = []
        if self.mean_shift_alert:
            flags.append("mean")
        if self.variance_ratio_alert:
            flags.append("variance")
        if self.coverage_alert:
            flags.append("coverage")
        return ",".join(flags) or "none"


def evaluate_drift(
    model_version_id: Optional[str] = None,
    lookback_days: int = 30,
    model_type: str = "xgboost_lightgbm_ensemble",
    *,
    ledger: Any = None,
    baseline_residuals: Optional[Iterable[float]] = None,
) -> DriftReport:
    """Evaluate drift for the given model version against its baseline calibration.

    Args:
        model_version_id: ULID of the model to evaluate. If None, uses the
                          currently-active model for `model_type`.
        lookback_days:    Window for recent residuals.
        model_type:       Used to resolve the active model when
                          model_version_id is None.

    Returns:
        DriftReport.

    Never raises on missing data — fields are None and `insufficient_recent_samples`
    is True when the recent window is too small. Raises only on Supabase env-var
    misconfiguration (since the operator running this clearly intends to query).
    """
    if ledger is not None:
        if model_version_id is None or not str(model_version_id).strip():
            raise ValueError("model_version_id is required for V2 ledger drift")
        if baseline_residuals is None:
            raise ValueError("baseline_residuals are required for V2 ledger drift")
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        rows = ledger.get_training_rows(
            since=cutoff,
            model_version=model_version_id,
        )
        return evaluate_settled_drift(
            rows,
            baseline_residuals=baseline_residuals,
            model_version_id=model_version_id,
            lookback_days=lookback_days,
        )

    from strathmark.db import _get_client, get_active_model_version

    client = _get_client()

    if model_version_id is None:
        model_version_id = get_active_model_version(model_type)
        if model_version_id is None:
            return DriftReport(
                model_version_id=None,
                lookback_days=lookback_days,
                recent_count=0,
                baseline_count=0,
                insufficient_recent_samples=True,
                notes=[f"no active model for type {model_type!r}"],
            )

    # Pull baseline calibration: latest calibration_tables row for this model.
    cal_resp = (
        client.table("calibration_tables")
        .select("holdout_residuals, coverage_at_90")
        .eq("model_version_id", model_version_id)
        .order("calibrated_at", desc=True)
        .limit(1)
        .execute()
    )
    cal_rows = cal_resp.data or []
    if not cal_rows:
        return DriftReport(
            model_version_id=model_version_id,
            lookback_days=lookback_days,
            recent_count=0,
            baseline_count=0,
            insufficient_recent_samples=True,
            notes=["no calibration_tables row found for this model_version_id"],
        )

    baseline_residuals = _coerce_residual_list(cal_rows[0].get("holdout_residuals"))
    baseline_coverage = cal_rows[0].get("coverage_at_90")

    # Pull recent residuals from prediction_residuals.
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    res_resp = (
        client.table("prediction_residuals")
        .select("residual, created_at")
        .eq("model_version_id", model_version_id)
        .gte("created_at", cutoff.isoformat())
        .execute()
    )
    recent_rows = res_resp.data or []
    recent_residuals = [float(r["residual"]) for r in recent_rows if r.get("residual") is not None]

    report = _build_report(
        model_version_id=model_version_id,
        lookback_days=lookback_days,
        recent_residuals=recent_residuals,
        baseline_residuals=baseline_residuals,
        baseline_coverage=(float(baseline_coverage) if baseline_coverage is not None else None),
    )
    _record_unavailable_intervals(report, len(recent_residuals), "residual-only rows")
    return report


def is_drifting(
    model_version_id: Optional[str] = None,
    lookback_days: int = 30,
    model_type: str = "xgboost_lightgbm_ensemble",
) -> bool:
    """Convenience wrapper. Returns True iff `evaluate_drift().overall_alert`."""
    return evaluate_drift(
        model_version_id=model_version_id,
        lookback_days=lookback_days,
        model_type=model_type,
    ).overall_alert


# ---------------------------------------------------------------------------
# Internal helpers — exposed for testing without a live DB
# ---------------------------------------------------------------------------


def _coerce_residual_list(raw) -> List[float]:
    """The JSONB column may store a list, a dict with a 'residuals' key, or
    something else entirely. Normalise to a flat list of floats."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [float(x) for x in raw if x is not None]
    if isinstance(raw, dict):
        # Common shapes: {"residuals": [...]} or {"values": [...]}
        for key in ("residuals", "values", "data"):
            if key in raw and isinstance(raw[key], list):
                return [float(x) for x in raw[key] if x is not None]
    return []


def _build_report(
    model_version_id: Optional[str],
    lookback_days: int,
    recent_residuals: List[float],
    baseline_residuals: List[float],
    baseline_coverage: Optional[float],
) -> DriftReport:
    """Pure compute. No Supabase. Easy to unit-test."""
    report = DriftReport(
        model_version_id=model_version_id,
        lookback_days=lookback_days,
        recent_count=len(recent_residuals),
        baseline_count=len(baseline_residuals),
        baseline_coverage_at_90=baseline_coverage,
    )

    if len(recent_residuals) < MIN_RECENT_SAMPLES:
        report.insufficient_recent_samples = True
        report.sample_label = "insufficient_recent_sample"
        report.notes.append(
            f"only {len(recent_residuals)} recent samples (need >= {MIN_RECENT_SAMPLES})"
        )
        return report

    if not baseline_residuals:
        report.notes.append(
            "calibration row has no residuals to compare against; cannot evaluate drift"
        )
        return report

    # Means
    report.recent_mean = statistics.fmean(recent_residuals)
    report.baseline_mean = statistics.fmean(baseline_residuals)
    report.mean_shift = report.recent_mean - report.baseline_mean
    if abs(report.mean_shift) > MEAN_SHIFT_SECONDS_THRESHOLD:
        report.mean_shift_alert = True
        report.notes.append(
            f"mean residual shifted by {report.mean_shift:+.2f}s "
            f"(threshold |x| > {MEAN_SHIFT_SECONDS_THRESHOLD}s)"
        )

    # Variances
    if len(recent_residuals) >= 2:
        report.recent_variance = statistics.variance(recent_residuals)
    if len(baseline_residuals) >= 2:
        report.baseline_variance = statistics.variance(baseline_residuals)
    if (
        report.recent_variance is not None
        and report.baseline_variance is not None
        and report.baseline_variance > 0
    ):
        report.variance_ratio_change = report.recent_variance / report.baseline_variance - 1.0
        if abs(report.variance_ratio_change) > VARIANCE_RATIO_THRESHOLD:
            report.variance_ratio_alert = True
            report.notes.append(
                f"variance changed by {report.variance_ratio_change:+.0%} "
                f"(threshold |x| > {VARIANCE_RATIO_THRESHOLD:.0%})"
            )

    report.overall_alert = report.mean_shift_alert or report.variance_ratio_alert
    return report


def _record_unavailable_intervals(report: DriftReport, count: int, label: str) -> None:
    """Record rows that cannot contribute to direct issued-interval coverage."""

    report.coverage_unavailable_count = count
    if count:
        report.notes.append(f"issued interval unavailable for {count} {label}")


def settled_model_prediction_rows(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only settled, non-manual, finite model predictions for monitoring."""

    eligible: list[dict[str, Any]] = []
    for raw in rows:
        source = str(raw.get("source", "")).strip().lower()
        if (
            source in {"manual", "panel", "broad_prior", "broad_event_prior"}
            or not source
            or raw.get("training_eligible") is False
            or bool(raw.get("degraded", False))
        ):
            continue
        if raw.get("settled_at") in (None, ""):
            continue
        try:
            predicted = float(raw.get("predicted_time"))
            actual = float(raw.get("actual_time"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(predicted) or not math.isfinite(actual):
            continue
        if predicted <= 0 or actual <= 0:
            continue
        item = dict(raw)
        item["predicted_time"] = predicted
        item["actual_time"] = actual
        item["residual"] = actual - predicted
        eligible.append(item)
    return eligible


def evaluate_settled_drift(
    rows: Iterable[Mapping[str, Any]],
    *,
    baseline_residuals: Iterable[float],
    baseline_coverage: Optional[float] = 0.90,
    model_version_id: Optional[str] = None,
    lookback_days: int = 30,
) -> DriftReport:
    """Evaluate drift from trusted settlement rows only, without database access."""

    eligible = settled_model_prediction_rows(rows)
    residuals = [float(row["residual"]) for row in eligible]
    report = _build_report(
        model_version_id=model_version_id,
        lookback_days=lookback_days,
        recent_residuals=residuals,
        baseline_residuals=[float(value) for value in baseline_residuals],
        baseline_coverage=baseline_coverage,
    )
    grouped: dict[float, list[bool]] = {}
    for row in eligible:
        try:
            lower = float(row.get("interval_lower"))
            upper = float(row.get("interval_upper"))
            nominal = float(row.get("nominal_coverage", row.get("interval_coverage")))
        except (TypeError, ValueError, OverflowError):
            report.coverage_unavailable_count += 1
            continue
        if not (
            math.isfinite(lower)
            and math.isfinite(upper)
            and math.isfinite(nominal)
            and 0 < lower < upper
            and 0 < nominal < 1
        ):
            report.coverage_unavailable_count += 1
            continue
        grouped.setdefault(nominal, []).append(lower <= row["actual_time"] <= upper)

    for nominal in sorted(grouped):
        observations = grouped[nominal]
        count = len(observations)
        covered = sum(observations)
        empirical = covered / count
        adequate = count >= MIN_RECENT_SAMPLES
        is_ninety = math.isclose(nominal, 0.90, rel_tol=0.0, abs_tol=1e-12)
        alert = bool(
            is_ninety
            and adequate
            and (empirical < COVERAGE_LOW_THRESHOLD or empirical > COVERAGE_HIGH_THRESHOLD)
        )
        key = _coverage_key(nominal)
        report.coverage_cohorts[key] = CoverageCohort(
            nominal_coverage=nominal,
            eligible_count=count,
            covered_count=covered,
            empirical_coverage=empirical,
            sample_label=("sample_adequate" if adequate else "insufficient_recent_sample"),
            coverage_alert=alert,
        )
        if is_ninety:
            report.recent_coverage_at_90 = empirical
            report.coverage_alert = alert
            if alert:
                report.notes.append(
                    f"issued 90% coverage {empirical:.2f} outside "
                    f"[{COVERAGE_LOW_THRESHOLD:.2f}, {COVERAGE_HIGH_THRESHOLD:.2f}]"
                )
    _record_unavailable_intervals(report, report.coverage_unavailable_count, "settled rows")
    report.overall_alert = bool(
        report.mean_shift_alert or report.variance_ratio_alert or report.coverage_alert
    )
    return report


def _coverage_key(nominal: float) -> str:
    text = f"{nominal:.12g}"
    if "." not in text:
        return f"{text}.00"
    whole, fraction = text.split(".", 1)
    return f"{whole}.{fraction.ljust(2, '0')}"
