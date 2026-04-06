"""
Panel Mark Fallback
===================

Panel mark defaults for competitors with no historical data.

When a competitor has no recorded results (new entrant, first-time participant),
the prediction cascade falls through to this module as the final unconditional
fallback. Panel marks are division-based defaults representing a typical
competitor for that division at 300mm standard diameter.

This module also handles the broader "sparse data" fallback chain:
    1. Competitor has no history at all          -> default mark (this module)
    2. Competitor has history but wrong event    -> event baseline shrinkage
    3. Competitor has history but wrong species  -> diameter + species scaling
    4. Competitor has history but wrong diameter -> diameter scaling only

The distinction matters because each level of the chain produces a different
confidence level and explanation string.

STRATHEX development defaults (at 300mm standard, quality 5):
    Division        Mark @ 300mm SB    Mark @ 300mm UH    Notes
    Open            20s                20s                Elite division
    Novice          40s                40s                Beginner division
    Junior          30s                30s                Youth division
    Veterans        30s                30s                Masters/Veterans
    Womens          30s                30s                Women's division
    Default         20s                20s                Unknown division

Note: These are starting marks at 300mm. Diameter scaling is applied afterward
to produce the final mark for the actual event diameter. The consuming
application may pass a custom panel_marks dict to override these defaults.

Source references (STRATHEX):
    woodchopping/predictions/baseline.py -> get_event_baseline_flexible()
    woodchopping/predictions/baseline.py -> get_competitor_historical_times_flexible()
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from strathmark.config import rules

# ---------------------------------------------------------------------------
# Panel mark defaults (book marks at 300mm standard, quality 5)
# ---------------------------------------------------------------------------

PANEL_MARKS_300MM: dict = {
    # (event_code, division) -> mark in seconds at 300mm, quality 5
    # STRATHEX development defaults
    ("SB", "Open"): 20.0,
    ("SB", "Novice"): 40.0,
    ("SB", "Junior"): 30.0,
    ("SB", "Veterans"): 30.0,
    ("SB", "Womens"): 30.0,
    ("UH", "Open"): 20.0,
    ("UH", "Novice"): 40.0,
    ("UH", "Junior"): 30.0,
    ("UH", "Veterans"): 30.0,
    ("UH", "Womens"): 30.0,
}

PANEL_MARK_DEFAULT_UNKNOWN_DIVISION: float = 20.0
"""Fallback when division is unknown. STRATHEX development default."""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _standardize_results_df(results_df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardize column names and coerce types; then drop invalid rows.

    Delegates normalization to utils.standardize_results_columns, then
    removes rows with missing or non-positive raw_time.
    """
    from strathmark.utils import standardize_results_columns

    df = standardize_results_columns(results_df)
    if df is None or df.empty:
        return df
    if "raw_time" in df.columns:
        df = df.dropna(subset=["raw_time"])
        df = df[(df["raw_time"] > 0) & (df["raw_time"] <= rules.MAX_TIME_LIMIT_SECONDS)]
    return df


def _calculate_performance_weight_simple(
    result_date, reference_date=None, half_life_days: int = 730
) -> float:
    """
    Calculate exponential time-decay weight for a historical result.
    Inline implementation to avoid circular imports within STRATHMARK.

    Formula: weight = 0.5^(days_old / half_life_days)
    Returns 1.0 for None/NaT dates (backward compatibility).
    """
    if result_date is None:
        return 1.0
    try:
        if pd.isna(result_date):
            return 1.0
    except (TypeError, ValueError):
        pass

    if reference_date is None:
        reference_date = datetime.now()

    try:
        if isinstance(result_date, str):
            result_date = pd.to_datetime(result_date)
        if isinstance(reference_date, str):
            reference_date = pd.to_datetime(reference_date)

        # Normalize both to date to avoid datetime-date subtraction error
        if hasattr(result_date, "date"):
            result_date = result_date.date() if callable(result_date.date) else result_date
        if hasattr(reference_date, "date"):
            reference_date = (
                reference_date.date() if callable(reference_date.date) else reference_date
            )

        days_old = (reference_date - result_date).days
        if days_old < 0:
            return 1.0

        return 0.5 ** (days_old / half_life_days)

    except Exception:
        return 1.0


def _normalize_time_for_baseline(
    time_val: float,
    hist_species: str,
    hist_diameter: float,
    target_species: str,
    target_diameter: float,
    event_code: str,
    results_df: pd.DataFrame,
    quality: float = 5.0,
) -> float:
    """
    Normalize a historical time to the target species and diameter.
    Applies quality normalization (to quality-5 reference), diameter scaling,
    and species time multiplier normalization.
    """
    from strathmark.wood import (
        calculate_scaling_factor,
        get_event_scaling_exponent,
        get_species_time_multiplier,
    )

    quality_val = max(
        1, min(10, int(quality) if quality is not None and not pd.isna(quality) else 5)
    )

    normalized = float(time_val)

    # Normalize historical time to quality 5 reference
    if quality_val != 5:
        quality_offset = quality_val - 5
        quality_factor = 1.0 + (quality_offset * 0.02)
        if quality_factor > 0:
            normalized = normalized / quality_factor

    # Diameter scaling
    if hist_diameter and target_diameter and hist_diameter != target_diameter:
        exponent = get_event_scaling_exponent(results_df, event_code)
        factor = calculate_scaling_factor(float(hist_diameter), float(target_diameter), exponent)
        normalized = normalized * factor

    # Species normalization
    if hist_species and target_species:
        hist_mult = get_species_time_multiplier(hist_species)
        target_mult = get_species_time_multiplier(target_species)
        if hist_mult > 0 and hist_mult != target_mult:
            normalized = normalized / hist_mult * target_mult

    return normalized


def _compute_robust_mean(times: list) -> Optional[float]:
    """Compute a robust mean using median/MAD clipping (no weighting)."""
    if not times:
        return None
    arr = np.array(times, dtype=float)
    if len(arr) < 5:
        return float(np.median(arr))
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    if mad > 0:
        clip_low = median - 2.5 * mad
        clip_high = median + 2.5 * mad
        arr = np.clip(arr, clip_low, clip_high)
    return float(np.mean(arr))


# ---------------------------------------------------------------------------
# Panel mark functions
# ---------------------------------------------------------------------------


def get_panel_mark(
    event_code: str,
    division: Optional[str],
    custom_marks: Optional[dict] = None,
) -> Tuple[float, str]:
    """
    Return the default mark for a competitor with no history.

    Default marks are at 300mm standard diameter and quality 5. The caller
    is responsible for applying diameter scaling (wood.scale_time()) afterward
    to adjust for the actual event diameter.

    STRATHEX development defaults:
        - Open UH/SB: 20 seconds
        - Novice: 40 seconds
        - Junior: 30 seconds
        - Veterans: 30 seconds
        - Women's UH/SB: 30 seconds

    Args:
        event_code: 'SB' or 'UH'.
        division: Competitor's division ('Open', 'Novice', 'Junior', etc.).
                  None or unrecognized values fall back to
                  PANEL_MARK_DEFAULT_UNKNOWN_DIVISION.
        custom_marks: Optional dict of {(event_code, division): mark_seconds}
                      passed by the consuming application to override defaults.

    Returns:
        Tuple of (mark_seconds, explanation_string).
        Confidence for default marks is always 'VERY LOW'.
    """
    event_upper = str(event_code).strip().upper()

    # Normalize division to known keys
    div_key = None
    if division:
        div_lower = str(division).strip().lower()
        if div_lower in ("novice",):
            div_key = "Novice"
        elif div_lower in ("veterans", "masters", "senior"):
            div_key = "Veterans"
        elif div_lower in ("womens", "women", "women's", "female"):
            div_key = "Womens"
        elif div_lower in ("junior", "youth"):
            div_key = "Junior"
        elif div_lower in ("open", "elite", "professional"):
            div_key = "Open"

    # Merge custom_marks with defaults (custom takes priority)
    marks_table = dict(PANEL_MARKS_300MM)
    if custom_marks:
        marks_table.update(custom_marks)

    if div_key is not None:
        key = (event_upper, div_key)
        if key in marks_table:
            mark = marks_table[key]
            explanation = f"Default mark: {div_key} division {event_upper} @ 300mm standard"
            return mark, explanation

    # Fallback for unknown or None division
    mark = PANEL_MARK_DEFAULT_UNKNOWN_DIVISION
    div_label = division if division else "Unknown"
    explanation = (
        f"Default mark: {div_label} division (unrecognized, using default "
        f"{PANEL_MARK_DEFAULT_UNKNOWN_DIVISION:.0f}s)"
    )
    return mark, explanation


def get_event_baseline(
    event_code: str,
    species: str,
    diameter_mm: float,
    results_df: pd.DataFrame,
    exclude_competitor: Optional[str] = None,
) -> Tuple[Optional[float], str, str]:
    """
    Calculate event-level baseline time from all available results.

    Used as a fallback when a competitor has no history at all for this event,
    or when their history is too sparse to use directly.

    Applies time-decay weighting before computing the mean, so recent results
    from other competitors contribute more to the baseline.

    Cascading fallback within this function (from STRATHEX get_event_baseline_flexible()):
        1. Exact match: same event_code + species + diameter_mm (+/-25mm)
        2. Same event_code + species (any diameter, then scale)
        3. Same event_code (any species and diameter, then scale)
        4. None (no data exists at all)

    Args:
        event_code: 'SB' or 'UH'.
        species: Target wood species.
        diameter_mm: Target block diameter (mm).
        results_df: Historical results DataFrame.
        exclude_competitor: Name of competitor to exclude (avoids data leakage
                            when computing a baseline for that competitor).

    Returns:
        Tuple of (baseline_time_seconds, confidence, explanation).
        baseline_time_seconds is None if no data exists at all.
    """
    if results_df is None or results_df.empty:
        return None, "LOW", "no data available"

    df = _standardize_results_df(results_df)

    if df is None or df.empty:
        return None, "LOW", "no data after standardization"

    event_upper = str(event_code).strip().upper()
    event_match = df["event"].str.upper() == event_upper

    # Optionally exclude a specific competitor
    if exclude_competitor and "competitor_name" in df.columns:
        excl = (
            df["competitor_name"].str.strip().str.lower() != str(exclude_competitor).strip().lower()
        )
        event_match = event_match & excl

    # Level 1: species + diameter range + event
    if species and "species" in df.columns and "size_mm" in df.columns:
        species_match = df["species"].str.strip().str.lower() == str(species).strip().lower()
        diameter_match = (df["size_mm"] >= diameter_mm - 25) & (df["size_mm"] <= diameter_mm + 25)
        exact_matches = df[species_match & diameter_match & event_match]

        times = []
        for _, row in exact_matches.iterrows():
            time_val = row.get("raw_time")
            if time_val is None or pd.isna(time_val) or float(time_val) <= 0:
                continue
            if float(time_val) > rules.MAX_TIME_LIMIT_SECONDS:
                continue
            hist_d = row.get("size_mm", diameter_mm)
            hist_q = row.get("quality", 5.0)
            normalized = _normalize_time_for_baseline(
                float(time_val),
                str(row.get("species", species)).strip(),
                float(hist_d) if hist_d is not None else diameter_mm,
                str(species).strip(),
                float(diameter_mm),
                event_upper,
                df,
                quality=hist_q,
            )
            times.append(normalized)

        if len(times) >= 3:
            mean_val = _compute_robust_mean(times)
            return mean_val, "HIGH", f"species/size normalized average ({len(times)} performances)"

    # Level 2: diameter range + event (any species)
    if "size_mm" in df.columns:
        diameter_match = (df["size_mm"] >= diameter_mm - 25) & (df["size_mm"] <= diameter_mm + 25)
        size_matches = df[diameter_match & event_match]

        times = []
        for _, row in size_matches.iterrows():
            time_val = row.get("raw_time")
            if time_val is None or pd.isna(time_val) or float(time_val) <= 0:
                continue
            if float(time_val) > rules.MAX_TIME_LIMIT_SECONDS:
                continue
            hist_d = row.get("size_mm", diameter_mm)
            hist_q = row.get("quality", 5.0)
            normalized = _normalize_time_for_baseline(
                float(time_val),
                str(row.get("species", species)).strip(),
                float(hist_d) if hist_d is not None else diameter_mm,
                str(species).strip(),
                float(diameter_mm),
                event_upper,
                df,
                quality=hist_q,
            )
            times.append(normalized)

        if len(times) >= 3:
            mean_val = _compute_robust_mean(times)
            return mean_val, "MEDIUM", f"size normalized average ({len(times)} performances)"

    # Level 3: event only (all data for this event type)
    event_only = df[event_match]
    times = []
    for _, row in event_only.iterrows():
        time_val = row.get("raw_time")
        if time_val is None or pd.isna(time_val) or float(time_val) <= 0:
            continue
        if float(time_val) > rules.MAX_TIME_LIMIT_SECONDS:
            continue
        hist_d = row.get("size_mm", diameter_mm)
        hist_q = row.get("quality", 5.0)
        normalized = _normalize_time_for_baseline(
            float(time_val),
            str(row.get("species", species)).strip(),
            float(hist_d) if hist_d is not None else diameter_mm,
            str(species).strip(),
            float(diameter_mm),
            event_upper,
            df,
            quality=hist_q,
        )
        times.append(normalized)

    if len(times) >= 3:
        mean_val = _compute_robust_mean(times)
        return mean_val, "LOW", f"event normalized average ({len(times)} performances)"

    return None, "LOW", "insufficient data"


def get_competitor_historical_times_flexible(
    competitor_name: str,
    event_code: str,
    species: str,
    diameter_mm: float,
    results_df: pd.DataFrame,
) -> Tuple[Optional[list], str, str]:
    """
    Retrieve historical times for a competitor using cascading fallback.

    Cascade (from STRATHEX baseline.py get_competitor_historical_times_flexible()):
        Level 1: Exact match -- same competitor + event_code + species (exact)
        Level 2: Same competitor + event_code (any species, any diameter)
        Level 3: None (caller should use get_event_baseline() instead)

    Args:
        competitor_name: Competitor display name.
        event_code: 'SB' or 'UH'.
        species: Target wood species.
        diameter_mm: Target block diameter (mm).
        results_df: Historical results DataFrame.

    Returns:
        Tuple of (times_list, confidence, explanation).
        times_list is None if no competitor history exists at any level.
    """
    if results_df is None or results_df.empty:
        return None, "LOW", "no data available"

    df = _standardize_results_df(results_df)
    if df is None or df.empty:
        return None, "LOW", "no data after standardization"

    event_upper = str(event_code).strip().upper()

    # Match competitor and event (required)
    name_match = (
        df["competitor_name"].str.strip().str.lower() == str(competitor_name).strip().lower()
    )
    event_match = df["event"].str.upper() == event_upper

    # Level 1: Exact species match
    if species and "species" in df.columns:
        species_match = df["species"].str.strip().str.lower() == str(species).strip().lower()
        exact_matches = df[name_match & event_match & species_match]

        times = []
        for _, row in exact_matches.iterrows():
            time_val = row.get("raw_time")
            if (
                time_val is not None
                and not pd.isna(time_val)
                and float(time_val) > 0
                and float(time_val) <= rules.MAX_TIME_LIMIT_SECONDS
            ):
                times.append(float(time_val))

        if times:
            return times, "HIGH", f"on {species} (exact match)"

    # Level 2: Any species for this competitor and event
    any_species_matches = df[name_match & event_match]
    times = []
    for _, row in any_species_matches.iterrows():
        time_val = row.get("raw_time")
        if (
            time_val is not None
            and not pd.isna(time_val)
            and float(time_val) > 0
            and float(time_val) <= rules.MAX_TIME_LIMIT_SECONDS
        ):
            times.append(float(time_val))

    if times:
        return times, "MEDIUM", "on various wood types"

    return None, "LOW", "no competitor history found"
