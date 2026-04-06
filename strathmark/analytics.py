"""
Analytics
==========

Backtesting, competitor profiling, and performance history analysis.

Public functions:
    backtest_predictions()       -- compare predicted vs actual times
    profile_competitor()         -- summarise a single competitor's history
    summarise_performance_history() -- tournament history summary for a field

Source references (STRATHEX):
    woodchopping/analytics/prediction_accuracy.py
    woodchopping/analytics/competitor_profiling.py
    woodchopping/analytics/performance_history.py
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import numpy as np

from strathmark.predictor import (
    CompetitorRecord,
    WoodProfile,
    get_best_prediction,
)

# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------


def backtest_predictions(
    competitors: List[CompetitorRecord],
    wood: WoodProfile,
    event_code: str,
    actuals: Dict[str, float],
    ml_model=None,
    results_df=None,
    wood_df=None,
) -> Dict[str, Any]:
    """
    Compare predicted times against actual race times.

    Uses the full prediction cascade (get_best_prediction) when ml_model or
    results_df are provided, otherwise falls back to baseline only.

    Args:
        competitors: List of CompetitorRecord objects.
        wood: Wood profile used in the event.
        event_code: 'SB' or 'UH'.
        actuals: Dict mapping competitor name to actual cutting time (seconds).
        ml_model: Optional trained MLModel for ML cascade level.
        results_df: Optional historical results DataFrame.
        wood_df: Optional wood properties DataFrame.

    Returns:
        Dict with keys:
            'results'        -- List of per-competitor dicts
            'mae'            -- Mean Absolute Error (seconds)
            'rmse'           -- Root Mean Squared Error (seconds)
            'bias'           -- Mean signed error (positive = predictions too high)
            'within_3s_pct'  -- % of predictions within 3 seconds of actual
    """
    results = []
    errors = []

    for record in competitors:
        if record.name not in actuals:
            continue
        actual = actuals[record.name]
        pred_result = get_best_prediction(
            record,
            wood,
            event_code,
            wood_data_df=wood_df,
            results_df=results_df,
            ml_model=ml_model,
        )
        if pred_result is None:
            continue
        predicted = pred_result.value
        error = predicted - actual
        abs_error = abs(error)
        errors.append(error)
        results.append(
            {
                "name": record.name,
                "predicted": predicted,
                "actual": actual,
                "error": error,
                "abs_error": abs_error,
                "confidence": pred_result.confidence,
            }
        )

    if not errors:
        return {
            "results": results,
            "mae": None,
            "rmse": None,
            "bias": None,
            "within_3s_pct": None,
        }

    errors_arr = np.array(errors)
    within_3s = sum(1 for e in errors if abs(e) <= 3.0)
    return {
        "results": results,
        "mae": float(np.mean(np.abs(errors_arr))),
        "rmse": float(np.sqrt(np.mean(errors_arr**2))),
        "bias": float(np.mean(errors_arr)),
        "within_3s_pct": within_3s / len(errors) * 100,
    }


# ---------------------------------------------------------------------------
# Competitor profiling
# ---------------------------------------------------------------------------


def profile_competitor(
    record: CompetitorRecord,
    event_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Summarise a competitor's historical performance.

    Args:
        record: Competitor record with history populated.
        event_code: Optional filter ('SB' or 'UH'). None analyses all events.

    Returns:
        Dict with:
            'name', 'division', 'total_results', 'events_contested',
            'mean_time', 'std_dev', 'best_time', 'worst_time',
            'most_recent_date', 'activity_level'
    """
    history = record.history
    if event_code is not None:
        history = [r for r in history if r.event_code.upper() == event_code.upper()]

    if not history:
        return {
            "name": record.name,
            "division": record.division,
            "total_results": 0,
            "events_contested": [],
            "mean_time": None,
            "std_dev": None,
            "best_time": None,
            "worst_time": None,
            "most_recent_date": None,
            "activity_level": "inactive",
        }

    times = [r.time_seconds for r in history]
    events = sorted({r.event_code for r in history})
    dates = [r.result_date for r in history if r.result_date is not None]
    most_recent = max(dates) if dates else None

    today = date.today()
    recent_count = sum(1 for d in dates if d is not None and (today - d).days <= 730)
    if recent_count >= 5:
        activity = "active"
    elif recent_count >= 2:
        activity = "moderate"
    else:
        activity = "inactive"

    return {
        "name": record.name,
        "division": record.division,
        "total_results": len(times),
        "events_contested": events,
        "mean_time": float(np.mean(times)),
        "std_dev": float(np.std(times, ddof=1)) if len(times) >= 2 else None,
        "best_time": float(min(times)),
        "worst_time": float(max(times)),
        "most_recent_date": most_recent,
        "activity_level": activity,
    }


# ---------------------------------------------------------------------------
# Performance history summary
# ---------------------------------------------------------------------------


def summarise_performance_history(
    competitors: List[CompetitorRecord],
    event_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return a ranked performance summary for a field of competitors.

    Sorted by mean_time ascending (fastest first).

    Args:
        competitors: List of CompetitorRecord objects.
        event_code: Optional filter.

    Returns:
        List of profile dicts (see profile_competitor()), sorted fastest first.
    """
    profiles = [profile_competitor(c, event_code) for c in competitors]
    with_data = [p for p in profiles if p["mean_time"] is not None]
    without_data = [p for p in profiles if p["mean_time"] is None]
    with_data.sort(key=lambda p: p["mean_time"])
    return with_data + without_data
