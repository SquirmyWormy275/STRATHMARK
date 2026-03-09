"""
Time-Decay Weighting
====================

Exponential time-decay weighting for historical performance results.

All prediction methods that consume historical data MUST apply time-decay
so that recent performances carry more weight than old ones. This is critical
for aging competitors whose recent ability may differ significantly from their
peak, and for competitors returning after a long absence.

Decay formula:
    weight = 0.5 ^ (days_old / half_life_days)

Standard half-life: 730 days (2 years).
    A result from today        -> weight 1.000
    A result from 1 year ago   -> weight 0.707
    A result from 2 years ago  -> weight 0.500
    A result from 3 years ago  -> weight 0.354
    A result from 5 years ago  -> weight 0.177
    A result from 10 years ago -> weight 0.031

Adaptive half-lives (from STRATHEX config.py -> BaselineV2HybridConfig):
    Active competitors   (5+ results in last 2 years):  365-day half-life
        Rationale: Heavy emphasis on recent form; older data becomes noise.
    Moderate competitors (2+ results in last 2 years):  730-day half-life (standard)
    Inactive competitors (< 2 results in last 2 years): 1095-day half-life
        Rationale: Preserve historical data longer when recent data is scarce.

Source references (STRATHEX):
    woodchopping/predictions/baseline.py -> calculate_performance_weight()
    woodchopping/predictions/baseline.py -> predict_baseline_v2_hybrid()
    woodchopping/predictions/ml_model.py -> train_ml_model()  (sample weights)
    config.py -> BaselineV2HybridConfig  (HALF_LIFE_* constants)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from strathmark.config import decay_config


# ---------------------------------------------------------------------------
# Constants (mirrored from STRATHEX config.py -> BaselineV2HybridConfig)
# ---------------------------------------------------------------------------

HALF_LIFE_ACTIVE_DAYS: int = decay_config.HALF_LIFE_ACTIVE_DAYS
"""Half-life for active competitors (5+ results in last 2 years)."""

HALF_LIFE_MODERATE_DAYS: int = decay_config.HALF_LIFE_MODERATE_DAYS
"""Standard 2-year half-life for moderate competitors."""

HALF_LIFE_INACTIVE_DAYS: int = decay_config.HALF_LIFE_INACTIVE_DAYS
"""Extended 3-year half-life for inactive competitors (preserve old data)."""

ACTIVITY_WINDOW_DAYS: int = decay_config.ACTIVITY_WINDOW_DAYS
"""Lookback window used to classify activity level (2 years)."""

ACTIVE_MIN_RESULTS: int = decay_config.ACTIVE_MIN_RESULTS
"""Minimum results in activity window to be classified as 'active'."""

MODERATE_MIN_RESULTS: int = decay_config.MODERATE_MIN_RESULTS
"""Minimum results in activity window to be classified as 'moderate'."""


# ---------------------------------------------------------------------------
# Core decay functions
# ---------------------------------------------------------------------------

def calculate_performance_weight(
    result_date: Optional[date],
    reference_date: Optional[date] = None,
    half_life_days: int = HALF_LIFE_MODERATE_DAYS,
) -> float:
    """
    Calculate the exponential time-decay weight for a single historical result.

    Formula: weight = 0.5 ^ (days_old / half_life_days)

    Uses exponential decay formula: weight = 0.5^(days_old / half_life_days)

    This ensures recent performances have much greater influence than old
    performances, which is critical for aging competitors whose recent ability
    differs from their peak.

    Args:
        result_date: Date when the performance occurred (datetime or None).
                     If None, weight is 1.0 (treated as fully recent).
        reference_date: Date to measure age from. Defaults to today.
        half_life_days: Days until weight halves. Use one of the named
                        constants (HALF_LIFE_*_DAYS) or pass a custom value.
                        Default: 730 = 2 years.

    Returns:
        float: Weight between 0.0 and 1.0
            - Returns 1.0 if result_date is None (missing date = no decay)
            - Returns 1.0 if result is from today
            - Returns 0.5 if result is exactly half_life_days old
            - Returns ~0.0 for very old results (15+ years)

    Weight Examples (730-day / 2-year half-life):
        Current season (0-180 days): weight 0.87-1.00
        Last season (365 days): weight 0.71
        2 years ago (730 days): weight 0.50
        3 years ago (1095 days): weight 0.35
        4 years ago (1460 days): weight 0.25
        6 years ago (2190 days): weight 0.125
        10 years ago (3650 days): weight 0.031 (~3% of current)
        15 years ago (5475 days): weight 0.006 (~0.6% of current, essentially zero)

    Example:
        >>> from datetime import datetime
        >>> result_from_2020 = datetime(2020, 7, 15)
        >>> today = datetime(2025, 7, 15)  # 5 years = 1825 days later
        >>> weight = calculate_performance_weight(result_from_2020, today, 730)
        >>> print(f"{weight:.3f}")  # Should be ~0.177 (2^(-1825/730) = 2^(-2.5))
        0.177

    Note:
        - Designed for seasonal sport (American woodchopping: April-September)
        - 730-day (2-year) half-life balances recent trajectory vs career history
        - Performances from 10+ years ago have weight < 0.05
        - Missing dates return weight 1.0 to maintain backward compatibility
    """
    # If no date provided, return full weight (backward compatibility)
    if result_date is None:
        return 1.0

    # Handle pandas NaT
    try:
        if pd.isna(result_date):
            return 1.0
    except (TypeError, ValueError):
        pass

    # Default to current date if not specified
    if reference_date is None:
        reference_date = datetime.now()

    # Calculate days between dates
    try:
        # Handle both datetime and pandas Timestamp objects
        if isinstance(result_date, str):
            result_date = pd.to_datetime(result_date)
        if isinstance(reference_date, str):
            reference_date = pd.to_datetime(reference_date)

        days_old = (reference_date - result_date).days

        # Can't have negative age (future dates get full weight)
        if days_old < 0:
            return 1.0

        # Exponential decay: weight = 0.5^(days_old / half_life)
        # This is equivalent to: weight = 2^(-days_old / half_life)
        weight = 0.5 ** (days_old / half_life_days)

        return weight

    except Exception as e:
        # If date parsing fails, return full weight
        print(f"Warning: Failed to calculate weight for date {result_date}: {e}")
        return 1.0


def classify_activity_level(
    result_dates: Sequence[Optional[date]],
    reference_date: Optional[date] = None,
    window_days: int = ACTIVITY_WINDOW_DAYS,
) -> str:
    """
    Classify competitor activity level for adaptive half-life selection.

    Counts results within the activity window and returns:
        'active'    -> use HALF_LIFE_ACTIVE_DAYS    (365)
        'moderate'  -> use HALF_LIFE_MODERATE_DAYS  (730)
        'inactive'  -> use HALF_LIFE_INACTIVE_DAYS  (1095)

    Args:
        result_dates: Sequence of result dates for one competitor/event combination.
                      None values are ignored in the count.
        reference_date: Reference point for the activity window. Defaults to today.
        window_days: Lookback window length in days.

    Returns:
        One of: 'active', 'moderate', 'inactive'.
    """
    if reference_date is None:
        reference_date = datetime.now()

    # Convert reference_date to datetime if needed
    if isinstance(reference_date, date) and not isinstance(reference_date, datetime):
        reference_date = datetime(reference_date.year, reference_date.month, reference_date.day)

    count_in_window = 0
    for d in result_dates:
        if d is None:
            continue
        try:
            if pd.isna(d):
                continue
        except (TypeError, ValueError):
            pass
        try:
            # Normalize to datetime
            if isinstance(d, str):
                d = pd.to_datetime(d).to_pydatetime()
            elif isinstance(d, date) and not isinstance(d, datetime):
                d = datetime(d.year, d.month, d.day)
            days_old = (reference_date - d).days
            if 0 <= days_old <= window_days:
                count_in_window += 1
        except Exception:
            continue

    if count_in_window >= ACTIVE_MIN_RESULTS:
        return 'active'
    elif count_in_window >= MODERATE_MIN_RESULTS:
        return 'moderate'
    else:
        return 'inactive'


def select_half_life(activity_level: str) -> int:
    """
    Map an activity level string to the corresponding half-life in days.

    Args:
        activity_level: Output of classify_activity_level().

    Returns:
        Half-life in days (365, 730, or 1095).

    Raises:
        ValueError: If activity_level is not a recognized value.
    """
    mapping = {
        'active': HALF_LIFE_ACTIVE_DAYS,
        'moderate': HALF_LIFE_MODERATE_DAYS,
        'inactive': HALF_LIFE_INACTIVE_DAYS,
    }
    if activity_level not in mapping:
        raise ValueError(
            f"Unknown activity_level: '{activity_level}'. "
            f"Must be one of: {list(mapping.keys())}"
        )
    return mapping[activity_level]


def compute_weighted_average(
    times: Sequence[float],
    weights: Sequence[float],
) -> float:
    """
    Compute a weighted arithmetic mean of times.

    Uses robust median/MAD clipping (from STRATHEX compute_robust_weighted_mean)
    to reduce influence of extreme values before computing the weighted mean.

    Both sequences must have the same length and at least one element.
    Weights need not sum to 1.0; they are normalized internally.

    Args:
        times: Sequence of performance times (seconds).
        weights: Corresponding decay weights (positive floats).

    Returns:
        Weighted mean time in seconds.

    Raises:
        ValueError: If sequences have different lengths or are empty.
        ZeroDivisionError: If all weights are zero.
    """
    if len(times) != len(weights):
        raise ValueError(
            f"times and weights must have equal length: "
            f"{len(times)} vs {len(weights)}"
        )
    if len(times) == 0:
        raise ValueError("times and weights must not be empty")

    times_arr = np.array(times, dtype=float)
    weights_arr = np.array(weights, dtype=float)

    weight_sum = weights_arr.sum()
    if weight_sum <= 0:
        raise ZeroDivisionError("All weights are zero; cannot compute weighted average")

    # Use median/MAD clipping to reduce influence of extreme values
    # (matches STRATHEX compute_robust_weighted_mean logic)
    median = float(np.median(times_arr))
    if len(times_arr) < 5:
        # Too few samples for robust clipping; return simple weighted median fallback
        return float(np.average(times_arr, weights=weights_arr))

    mad = float(np.median(np.abs(times_arr - median)))
    if mad > 0:
        clip_low = median - 2.5 * mad
        clip_high = median + 2.5 * mad
        clipped = np.clip(times_arr, clip_low, clip_high)
    else:
        clipped = times_arr

    return float(np.average(clipped, weights=weights_arr))


def compute_weights_for_results(
    result_dates: Sequence[Optional[date]],
    reference_date: Optional[date] = None,
    adaptive: bool = True,
) -> List[float]:
    """
    Compute time-decay weights for a full sequence of historical results.

    If adaptive=True, automatically selects the half-life based on activity level.
    If adaptive=False, always uses HALF_LIFE_MODERATE_DAYS (for ML training consistency).

    Args:
        result_dates: Date for each historical result. None dates get weight 1.0.
        reference_date: Reference date. Defaults to today.
        adaptive: Whether to select half-life based on competitor activity level.

    Returns:
        List of weights in (0.0, 1.0], one per result date.
    """
    if reference_date is None:
        reference_date = datetime.now()

    if adaptive:
        activity = classify_activity_level(result_dates, reference_date)
        half_life = select_half_life(activity)
    else:
        half_life = HALF_LIFE_MODERATE_DAYS

    return [
        calculate_performance_weight(d, reference_date, half_life)
        for d in result_dates
    ]
