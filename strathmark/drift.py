"""
Calibration drift detection
===========================

Compares a rolling window of recent residuals (from `prediction_residuals`)
against the residual distribution captured at calibration time (in
`calibration_tables.holdout_residuals`). Surfaces advisory alerts. Never
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
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

_log = logging.getLogger(__name__)


# Default thresholds. Live in this module, not config.py, because they are
# drift-specific and changing them requires rerunning calibration to be
# meaningful — they are NOT general-purpose tuning knobs.
MEAN_SHIFT_SECONDS_THRESHOLD: float = 1.0
VARIANCE_RATIO_THRESHOLD: float = 0.30  # |new/baseline - 1| > this -> alert
COVERAGE_LOW_THRESHOLD: float = 0.85
COVERAGE_HIGH_THRESHOLD: float = 0.95
MIN_RECENT_SAMPLES: int = 20  # below this, drift signal is too noisy to act on


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
    # Empirical 90% coverage on the recent residual window. This is the
    # number compared against [COVERAGE_LOW_THRESHOLD, COVERAGE_HIGH_THRESHOLD]
    # to surface drift -- baseline_coverage_at_90 is informational context.
    recent_coverage_at_90: Optional[float] = None

    # Boolean flags for each rule
    mean_shift_alert: bool = False
    variance_ratio_alert: bool = False
    coverage_alert: bool = False
    insufficient_recent_samples: bool = False

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
        return (
            f"drift status: nominal (model={self.model_version_id}, "
            f"recent_n={self.recent_count}, mean_shift={self.mean_shift:+.2f}s)"
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
    recent_residuals = [
        float(r["residual"]) for r in recent_rows if r.get("residual") is not None
    ]

    return _build_report(
        model_version_id=model_version_id,
        lookback_days=lookback_days,
        recent_residuals=recent_residuals,
        baseline_residuals=baseline_residuals,
        baseline_coverage=(
            float(baseline_coverage) if baseline_coverage is not None else None
        ),
    )


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
        report.variance_ratio_change = (
            report.recent_variance / report.baseline_variance - 1.0
        )
        if abs(report.variance_ratio_change) > VARIANCE_RATIO_THRESHOLD:
            report.variance_ratio_alert = True
            report.notes.append(
                f"variance changed by {report.variance_ratio_change:+.0%} "
                f"(threshold |x| > {VARIANCE_RATIO_THRESHOLD:.0%})"
            )

    # Coverage drift: compute empirical 90% coverage on recent residuals
    # against the 90% prediction interval derived from the baseline residual
    # distribution (5th-95th percentile). Compare empirical coverage against
    # [COVERAGE_LOW_THRESHOLD, COVERAGE_HIGH_THRESHOLD]. This detects drift
    # in coverage on recent traffic, which is what the policy actually
    # specifies. The baseline coverage_at_90 stored in calibration_tables is
    # informational only -- it's a static calibration-time number, not a
    # drift signal.
    if len(baseline_residuals) >= 2:
        sorted_baseline = sorted(baseline_residuals)
        n = len(sorted_baseline)
        # Linear interpolation between the bracketing samples for the 5th
        # and 95th percentiles.
        lo_pos = 0.05 * (n - 1)
        hi_pos = 0.95 * (n - 1)
        lo_idx = int(lo_pos)
        hi_idx = int(hi_pos)
        lo_frac = lo_pos - lo_idx
        hi_frac = hi_pos - hi_idx
        baseline_lo = sorted_baseline[lo_idx] + lo_frac * (
            sorted_baseline[min(lo_idx + 1, n - 1)] - sorted_baseline[lo_idx]
        )
        baseline_hi = sorted_baseline[hi_idx] + hi_frac * (
            sorted_baseline[min(hi_idx + 1, n - 1)] - sorted_baseline[hi_idx]
        )
        inside = sum(1 for r in recent_residuals if baseline_lo <= r <= baseline_hi)
        report.recent_coverage_at_90 = inside / len(recent_residuals)
        if (
            report.recent_coverage_at_90 < COVERAGE_LOW_THRESHOLD
            or report.recent_coverage_at_90 > COVERAGE_HIGH_THRESHOLD
        ):
            report.coverage_alert = True
            report.notes.append(
                f"recent 90% coverage {report.recent_coverage_at_90:.2f} outside "
                f"[{COVERAGE_LOW_THRESHOLD:.2f}, {COVERAGE_HIGH_THRESHOLD:.2f}] "
                f"(baseline interval [{baseline_lo:+.2f}, {baseline_hi:+.2f}]s)"
            )

    report.overall_alert = (
        report.mean_shift_alert or report.variance_ratio_alert or report.coverage_alert
    )
    return report
