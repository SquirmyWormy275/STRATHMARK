"""
Wood Properties and Factors
============================

Species hardness lookup, diameter scaling, and quality adjustment.

This module provides all wood-related computations needed before time
prediction can occur:

    1. Species hardness lookup
       Loads Janka hardness, specific gravity, shear strength, crush strength,
       MOR (Modulus of Rupture), and MOE (Modulus of Elasticity) from the
       species table. Six properties combined give r=0.621 vs shear alone r=0.523.

    2. Diameter scaling
       Time scales approximately as diameter^exponent where exponent ~ 1.4.
       The exponent is calibrated from historical data when enough
       multi-diameter pairs exist; falls back to 1.4 otherwise.

    3. Quality adjustment
       Wood firmness rated 1-10 (5 = average reference, no adjustment).
       Linear Janka adjustment: effective_janka = base_janka * (1 + (quality-5)*0.1)
           quality 1  -> 0.6x base Janka (very soft/rotten, faster times)
           quality 5  -> 1.0x base Janka (average, baseline reference)
           quality 10 -> 1.5x base Janka (extremely hard, slower times)
       LLM multiplier range: 0.85-1.15 on predicted time.
       Statistical fallback: +-2% per quality point from 5.

    4. QAA empirical table interpolation
       Blends softwood / medium / hardwood QAA mark tables using triangular
       membership functions keyed on effective Janka hardness.

Source references (STRATHEX):
    woodchopping/predictions/diameter_scaling.py -> scale_time()
    woodchopping/predictions/diameter_scaling.py -> calculate_scaling_factor()
    woodchopping/predictions/diameter_scaling.py -> calibrate_scaling_exponent()
    woodchopping/predictions/diameter_scaling.py -> get_event_scaling_exponent()
    woodchopping/predictions/qaa_scaling.py      -> calculate_effective_janka_hardness()
    woodchopping/predictions/qaa_scaling.py      -> calculate_hardness_blend_weights()
    woodchopping/predictions/qaa_scaling.py      -> interpolate_qaa_tables()
    woodchopping/predictions/qaa_scaling.py      -> scale_mark_qaa()
    config.py                                    -> MLConfig (DEFAULT_JANKA_HARDNESS, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SCALING_EXPONENT: float = 1.4
"""
Power-law diameter scaling exponent (fallback when calibration is unavailable).
Chosen because time ~ diameter^1.4 fits between linear (1.0) and quadratic (2.0).
Calibrated from historical data when multi-diameter competitor pairs exist.
"""

DIAMETER_TOLERANCE: float = 10.0
"""Diameter tolerance (mm) - differences smaller than this return factor 1.0."""

DEFAULT_JANKA_HARDNESS: float = 1690.0
"""Fallback Janka hardness when species is not in table (Eastern White Pine S01)."""

DEFAULT_SPECIFIC_GRAVITY: float = 0.34
"""Fallback specific gravity (Eastern White Pine)."""

QUALITY_JANKA_ADJUSTMENT_PER_POINT: float = 0.10
"""Linear Janka adjustment per quality point from baseline 5 (+-10% per point)."""

LLM_QUALITY_MULTIPLIER_MIN: float = 0.85
"""Minimum LLM quality multiplier (quality 1: very soft wood)."""

LLM_QUALITY_MULTIPLIER_MAX: float = 1.15
"""Maximum LLM quality multiplier (quality 10: extremely hard wood)."""

STATISTICAL_QUALITY_ADJUSTMENT_PER_POINT: float = 0.02
"""+-2% per quality point from baseline 5 (statistical fallback when LLM unavailable)."""

# QAA hardness table blend peaks (Janka hardness in Newtons)
QAA_SOFTWOOD_PEAK_N: float = 1300.0
"""QAA softwood table reference (Cottonwood, soft White Pine)."""

QAA_MEDIUM_PEAK_N: float = 2000.0
"""QAA medium-wood table reference (Yellow Pine, Ponderosa)."""

QAA_HARDWOOD_PEAK_N: float = 2800.0
"""QAA hardwood table reference (Alder, harder woods)."""

# Transition width between QAA categories
QAA_TRANSITION_WIDTH: float = 700.0

# Cache calibrated exponents per event (mutable module-level dict)
_event_exponent_cache: Dict[str, float] = {}


# ---------------------------------------------------------------------------
# Species properties lookup
# ---------------------------------------------------------------------------

@dataclass
class SpeciesProperties:
    """Physical properties of a wood species used as ML features."""

    species: str
    janka_hardness: float
    specific_gravity: float
    shear_strength: float
    crush_strength: float
    mor: float
    """Modulus of Rupture."""
    moe: float
    """Modulus of Elasticity."""


@dataclass
class ScalingMetadata:
    """Metadata about diameter scaling applied to a prediction."""
    was_scaled: bool
    original_diameter: Optional[float]
    target_diameter: Optional[float]
    scaling_factor: float
    confidence_adjustment: str  # "" (no change), "downgrade"
    warning_message: str


def get_species_properties(
    species: str,
    wood_df: Optional[pd.DataFrame] = None,
) -> SpeciesProperties:
    """
    Look up physical properties for a wood species.

    Args:
        species: Species name or code (e.g., 'Pine', 'S01').
        wood_df: Optional DataFrame from the wood Excel sheet. If None, falls back
                 to default values (Eastern White Pine).

    Returns:
        SpeciesProperties populated from the lookup table.
        Falls back to DEFAULT_JANKA_HARDNESS / DEFAULT_SPECIFIC_GRAVITY values
        if the species is not found.
    """
    defaults = SpeciesProperties(
        species=species,
        janka_hardness=DEFAULT_JANKA_HARDNESS,
        specific_gravity=DEFAULT_SPECIFIC_GRAVITY,
        shear_strength=1000.0,
        crush_strength=4000.0,
        mor=8000.0,
        moe=1000000.0,
    )

    if wood_df is None or wood_df.empty:
        return defaults

    # Try matching by species name or speciesID
    row = None
    if 'species' in wood_df.columns:
        match = wood_df[wood_df['species'].astype(str).str.strip().str.lower() == str(species).strip().lower()]
        if not match.empty:
            row = match.iloc[0]

    if row is None and 'speciesID' in wood_df.columns:
        match = wood_df[wood_df['speciesID'].astype(str).str.strip().str.upper() == str(species).strip().upper()]
        if not match.empty:
            row = match.iloc[0]

    if row is None:
        return defaults

    def _get(col, fallback):
        val = row.get(col, fallback)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return fallback
        return float(val)

    return SpeciesProperties(
        species=species,
        janka_hardness=_get('janka_hard', DEFAULT_JANKA_HARDNESS),
        specific_gravity=_get('spec_gravity', DEFAULT_SPECIFIC_GRAVITY),
        shear_strength=_get('shear', 1000.0),
        crush_strength=_get('crush_strength', 4000.0),
        mor=_get('MOR', 8000.0),
        moe=_get('MOE', 1000000.0),
    )


# ---------------------------------------------------------------------------
# Diameter scaling
# ---------------------------------------------------------------------------

def calculate_scaling_factor(
    from_diameter_mm: float,
    to_diameter_mm: float,
    exponent: float = DEFAULT_SCALING_EXPONENT,
) -> float:
    """
    Compute the diameter-based time scaling factor.

    Theory: Chopping time scales with wood volume/area to be cut.
    - Volume scales with diameter^2
    - But actual chopping efficiency doesn't scale linearly
    - Empirically, exponent between 1.3-1.5 works best

    Formula:
        scaling_factor = (to_diameter_mm / from_diameter_mm) ^ exponent

    Args:
        from_diameter_mm: Reference diameter (mm).
        to_diameter_mm: Target diameter (mm).
        exponent: Power-law exponent. Default 1.4; calibrate with
                  calibrate_scaling_exponent() when data allows.

    Returns:
        Multiplicative scaling factor (> 1.0 means more time for larger block).

    Example:
        >>> # Moses: 29s in 325mm, predict for 275mm
        >>> factor = calculate_scaling_factor(325, 275)
        >>> scaled_time = 29 * factor
        >>> print(f"{scaled_time:.1f}s")  # ~24.5s
    """
    if abs(from_diameter_mm - to_diameter_mm) < DIAMETER_TOLERANCE:
        return 1.0

    ratio = to_diameter_mm / from_diameter_mm
    # Time scales with diameter^exponent
    # If target is smaller (ratio < 1), scaling_factor < 1 (faster)
    # If target is larger (ratio > 1), scaling_factor > 1 (slower)
    scaling_factor = ratio ** exponent

    return scaling_factor


def scale_time(
    known_time: float,
    from_diameter_mm: float,
    to_diameter_mm: float,
    exponent: float = DEFAULT_SCALING_EXPONENT,
) -> Tuple[float, str]:
    """
    Scale a known time from one diameter to another.

    Confidence degrades when the diameter difference is large:
        > 50mm difference -> confidence downgraded by two levels
        > 25mm difference -> confidence downgraded by one level

    Args:
        known_time: Observed time at from_diameter_mm (seconds).
        from_diameter_mm: Diameter at which known_time was recorded (mm).
        to_diameter_mm: Target diameter to predict for (mm).
        exponent: Power-law exponent.

    Returns:
        Tuple of (scaled_time_seconds, confidence_note).
    """
    factor = calculate_scaling_factor(from_diameter_mm, to_diameter_mm, exponent)
    scaled_time = known_time * factor

    was_scaled = abs(factor - 1.0) > 0.05  # More than 5% adjustment

    diameter_diff = abs(to_diameter_mm - from_diameter_mm)
    if diameter_diff > 50:
        confidence_note = "downgrade_two"
    elif diameter_diff > 25:
        confidence_note = "downgrade_one"
    else:
        confidence_note = ""

    return scaled_time, confidence_note


def scale_time_list(
    times: List[float],
    from_diameter_mm: float,
    to_diameter_mm: float,
    exponent: float = DEFAULT_SCALING_EXPONENT,
) -> Tuple[List[float], ScalingMetadata]:
    """
    Scale a list of times for diameter difference.

    Args:
        times: List of time values
        from_diameter_mm: Original diameter (mm)
        to_diameter_mm: Target diameter (mm)
        exponent: Scaling exponent

    Returns:
        Tuple of (scaled_times, metadata)
    """
    factor = calculate_scaling_factor(from_diameter_mm, to_diameter_mm, exponent)
    scaled_times = [t * factor for t in times]

    was_scaled = abs(factor - 1.0) > 0.05
    diameter_diff = abs(to_diameter_mm - from_diameter_mm)

    if was_scaled:
        direction = "smaller" if to_diameter_mm < from_diameter_mm else "larger"
        warning = (
            f"Scaled from {from_diameter_mm:.0f}mm to {to_diameter_mm:.0f}mm "
            f"({direction}, {diameter_diff:.0f}mm difference)"
        )
    else:
        warning = ""

    if diameter_diff > 50:
        confidence_adj = "downgrade"
    elif diameter_diff > 25:
        confidence_adj = "downgrade"
    else:
        confidence_adj = ""

    metadata = ScalingMetadata(
        was_scaled=was_scaled,
        original_diameter=from_diameter_mm if was_scaled else None,
        target_diameter=to_diameter_mm if was_scaled else None,
        scaling_factor=factor,
        confidence_adjustment=confidence_adj,
        warning_message=warning,
    )

    return scaled_times, metadata


def adjust_confidence_for_scaling(
    original_confidence: str,
    metadata: ScalingMetadata,
) -> str:
    """
    Adjust confidence level when diameter scaling is applied.

    Cross-diameter predictions are less reliable than exact-match predictions.

    Args:
        original_confidence: "HIGH", "MEDIUM", or "LOW"
        metadata: Scaling metadata from scale_time_list()

    Returns:
        Adjusted confidence level
    """
    if metadata.confidence_adjustment != "downgrade":
        return original_confidence

    confidence_map = {
        "HIGH": "MEDIUM",
        "MEDIUM": "LOW",
        "LOW": "LOW"
    }
    return confidence_map.get(original_confidence, original_confidence)


def calibrate_scaling_exponent(
    results_df: pd.DataFrame,
    event_code: str,
    min_samples: int = 5,
) -> Optional[float]:
    """
    Fit the best power-law exponent from competitors with multi-diameter history.

    This function finds competitors who have times in multiple wood sizes and
    calculates the best-fit exponent for the scaling relationship.

    Method:
        1. Find competitor/species pairs with results at 2+ distinct diameters.
        2. For each pair: exponent = log(t2/t1) / log(d2/d1)
        3. Return the robust median across all pairs.
        4. Fall back to DEFAULT_SCALING_EXPONENT if fewer than 3 pairs found.

    Args:
        results_df: Historical results DataFrame.
        event_code: 'SB' or 'UH' (calibrate separately per event type).
        min_samples: Minimum number of cross-diameter pairs needed.

    Returns:
        Calibrated exponent, or None if insufficient data.
    """
    if results_df is None or results_df.empty:
        return None

    # Filter to this event
    event_data = results_df[
        results_df['event'].astype(str).str.strip().str.upper() == event_code.strip().upper()
    ].copy()

    if len(event_data) < min_samples:
        return None

    if 'size_mm' not in event_data.columns or 'raw_time' not in event_data.columns:
        return None

    # Find competitors with multiple diameter sizes
    competitor_diameters = event_data.groupby('competitor_name')['size_mm'].nunique()
    multi_diameter_competitors = competitor_diameters[competitor_diameters >= 2].index

    if len(multi_diameter_competitors) == 0:
        return None

    exponents = []

    for comp in multi_diameter_competitors:
        comp_data = event_data[event_data['competitor_name'] == comp]

        # Get average time for each diameter
        diameter_times = comp_data.groupby('size_mm')['raw_time'].mean()

        if len(diameter_times) < 2:
            continue

        # Compare all pairs of diameters
        diameters = sorted(diameter_times.index)
        for i in range(len(diameters)):
            for j in range(i + 1, len(diameters)):
                d1, d2 = diameters[i], diameters[j]
                t1, t2 = diameter_times[d1], diameter_times[d2]

                if t1 <= 0 or t2 <= 0:
                    continue

                # Calculate what exponent gives us the observed time ratio
                # t2/t1 = (d2/d1)^exp
                # exp = log(t2/t1) / log(d2/d1)
                time_ratio = t2 / t1
                diameter_ratio = d2 / d1

                if diameter_ratio <= 1.0:
                    continue

                exponent = np.log(time_ratio) / np.log(diameter_ratio)

                # Sanity check: exponent should be between 0.5 and 3.0
                if 0.5 <= exponent <= 3.0:
                    exponents.append(exponent)

    if len(exponents) < 3:  # Need at least a few samples
        return None

    # Return median exponent (robust to outliers)
    return float(np.median(exponents))


def get_event_scaling_exponent(
    results_df: Optional[pd.DataFrame],
    event_code: str,
) -> float:
    """
    Return a calibrated diameter scaling exponent for an event.

    Falls back to the default exponent when calibration is not possible.
    Results are cached per event code in the module-level _event_exponent_cache.
    """
    event_key = str(event_code).strip().upper()
    if event_key in _event_exponent_cache:
        return _event_exponent_cache[event_key]

    if results_df is None or results_df.empty:
        _event_exponent_cache[event_key] = DEFAULT_SCALING_EXPONENT
        return DEFAULT_SCALING_EXPONENT

    # Standardize column names if needed
    df = _standardize_results_columns(results_df)

    exponent = calibrate_scaling_exponent(df, event_key)
    if exponent is None:
        exponent = DEFAULT_SCALING_EXPONENT

    _event_exponent_cache[event_key] = float(exponent)
    return float(exponent)


def _standardize_results_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column names for results DataFrames (internal helper)."""
    if df is None or df.empty:
        return df
    df = df.copy()
    # Lowercase all column names
    df.columns = [c.strip().lower() for c in df.columns]
    # Rename common variants
    rename_map = {
        'time': 'raw_time',
        'competitorname': 'competitor_name',
        'competitor name': 'competitor_name',
        'event_code': 'event',
        'diameter': 'size_mm',
        'size': 'size_mm',
    }
    df.rename(columns=rename_map, inplace=True)
    return df


# ---------------------------------------------------------------------------
# Quality adjustment
# ---------------------------------------------------------------------------

def calculate_effective_janka_hardness(
    species: str,
    quality: int,
    wood_df: Optional[pd.DataFrame] = None,
) -> float:
    """
    Adjust base Janka hardness by the observed wood quality rating.

    This is critical because:
    - Hard White Pine (quality 9) can be as hard as average Ponderosa Pine
    - Soft Rock Maple (quality 2) can be softer than average White Pine
    - The quality scale captures block-specific firmness variations

    Formula (from STRATHEX qaa_scaling.py):
        quality_factor = 1.0 + ((quality - 5) * QUALITY_JANKA_ADJUSTMENT_PER_POINT)
        effective_janka = base_janka * quality_factor

    Quality Adjustment (1 = softest, 10 = hardest):
        Quality 1 (very soft/rotten): 0.6x base Janka (punky, decomposed)
        Quality 5 (average): 1.0x base Janka (normal for species)
        Quality 10 (very hard/firm): 1.5x base Janka (green wood, knots)

    Examples:
        quality 1  -> factor 0.6  (very soft/rotten)
        quality 5  -> factor 1.0  (average, no adjustment)
        quality 10 -> factor 1.5  (extremely firm/hard)

    Args:
        species: Species name or code.
        quality: Wood quality 1-10 (5 = average reference).
        wood_df: Optional species properties DataFrame.

    Returns:
        Effective Janka hardness in Newtons.
    """
    quality = max(1, min(10, int(quality)))

    # Get base Janka from wood database
    props = get_species_properties(species, wood_df)
    base_janka = props.janka_hardness

    # Quality adjustment factor (1 softest -> 10 hardest)
    # Linear interpolation: factor = 1.0 + ((quality - 5) * 0.1)
    # Quality 1: 0.6x (very soft)
    # Quality 5: 1.0x (average)
    # Quality 10: 1.5x (very hard)
    quality_factor = 1.0 + ((quality - 5) * QUALITY_JANKA_ADJUSTMENT_PER_POINT)

    # Clamp quality factor to reasonable bounds (0.3 to 2.0)
    quality_factor = max(0.3, min(2.0, quality_factor))

    effective_janka = base_janka * quality_factor

    return effective_janka


def apply_quality_multiplier_statistical(
    baseline_time: float,
    quality: int,
) -> float:
    """
    Apply the statistical fallback quality adjustment (no LLM required).

    Formula:
        adjustment = (quality - 5) * STATISTICAL_QUALITY_ADJUSTMENT_PER_POINT
        adjusted_time = baseline_time * (1 + adjustment)

    Range: quality 1 -> -8%, quality 10 -> +10% (relative to quality 5).

    This is the fallback used when Ollama is unavailable. The LLM multiplier
    (0.85-1.15) is slightly wider because the LLM also considers species context.

    Args:
        baseline_time: Predicted time at quality 5 (seconds).
        quality: Wood quality 1-10.

    Returns:
        Adjusted predicted time (seconds).
    """
    quality = max(1, min(10, int(quality)))
    quality_adjustment = (quality - 5) * STATISTICAL_QUALITY_ADJUSTMENT_PER_POINT
    return baseline_time * (1.0 + quality_adjustment)


# ---------------------------------------------------------------------------
# QAA empirical lookup tables (verbatim from STRATHEX qaa_scaling.py)
# ---------------------------------------------------------------------------

# ============================================================================
# OPEN UNDERHAND & STANDING BLOCK HANDICAP SCALE (Page 9)
# ============================================================================
# Standard: 300mm (12") diameter
# Book marks range: 3-43 seconds

QAA_UH_SB_HARDWOOD = {
    # book_mark_300mm: {diameter_mm: scaled_mark}
    3: {225: 2, 250: 2, 275: 3, 300: 3, 325: 3, 350: 3},
    4: {225: 2, 250: 3, 275: 4, 300: 4, 325: 4, 350: 4},
    5: {225: 3, 250: 3, 275: 4, 300: 5, 325: 5, 350: 5},
    6: {225: 3, 250: 4, 275: 5, 300: 6, 325: 6, 350: 6},
    7: {225: 4, 250: 4, 275: 6, 300: 7, 325: 7, 350: 7},
    8: {225: 4, 250: 5, 275: 7, 300: 8, 325: 8, 350: 8},
    9: {225: 5, 250: 6, 275: 8, 300: 9, 325: 9, 350: 9},
    10: {225: 5, 250: 6, 275: 8, 300: 10, 325: 10, 350: 10},
    11: {225: 6, 250: 7, 275: 9, 300: 11, 325: 11, 350: 11},
    12: {225: 6, 250: 8, 275: 10, 300: 12, 325: 12, 350: 12},
    13: {225: 7, 250: 8, 275: 11, 300: 13, 325: 14, 350: 15},
    14: {225: 8, 250: 9, 275: 12, 300: 14, 325: 15, 350: 16},
    15: {225: 8, 250: 9, 275: 12, 300: 15, 325: 16, 350: 17},
    16: {225: 8, 250: 10, 275: 13, 300: 16, 325: 17, 350: 18},
    17: {225: 9, 250: 11, 275: 14, 300: 17, 325: 18, 350: 19},
    18: {225: 9, 250: 11, 275: 15, 300: 18, 325: 19, 350: 20},
    19: {225: 10, 250: 12, 275: 16, 300: 19, 325: 20, 350: 21},
    20: {225: 11, 250: 13, 275: 17, 300: 20, 325: 21, 350: 23},
    21: {225: 11, 250: 13, 275: 17, 300: 21, 325: 23, 350: 25},
    22: {225: 11, 250: 14, 275: 18, 300: 22, 325: 24, 350: 26},
    23: {225: 12, 250: 14, 275: 19, 300: 23, 325: 25, 350: 27},
    24: {225: 13, 250: 15, 275: 20, 300: 24, 325: 26, 350: 28},
    25: {225: 13, 250: 16, 275: 21, 300: 25, 325: 27, 350: 29},
    26: {225: 13, 250: 16, 275: 21, 300: 26, 325: 28, 350: 30},
    27: {225: 14, 250: 17, 275: 22, 300: 27, 325: 29, 350: 31},
    28: {225: 14, 250: 18, 275: 23, 300: 28, 325: 30, 350: 32},
    29: {225: 15, 250: 18, 275: 24, 300: 29, 325: 31, 350: 34},
    30: {225: 16, 250: 19, 275: 25, 300: 30, 325: 32, 350: 34},
    31: {225: 16, 250: 19, 275: 25, 300: 31, 325: 34, 350: 37},
    32: {225: 16, 250: 20, 275: 26, 300: 32, 325: 35, 350: 38},
    33: {225: 17, 250: 21, 275: 27, 300: 33, 325: 36, 350: 39},
    34: {225: 18, 250: 21, 275: 28, 300: 34, 325: 37, 350: 40},
    35: {225: 18, 250: 22, 275: 29, 300: 35, 325: 38, 350: 41},
    36: {225: 19, 250: 23, 275: 30, 300: 36, 325: 39, 350: 42},
    37: {225: 19, 250: 23, 275: 30, 300: 37, 325: 40, 350: 43},
    38: {225: 19, 250: 24, 275: 31, 300: 38, 325: 41, 350: 45},
    39: {225: 20, 250: 24, 275: 32, 300: 39, 325: 42, 350: 46},
    40: {225: 20, 250: 25, 275: 33, 300: 40, 325: 43, 350: 47},
    41: {225: 21, 250: 26, 275: 34, 300: 41, 325: 45, 350: 49},
    42: {225: 21, 250: 26, 275: 34, 300: 42, 325: 46, 350: 50},
    43: {225: 22, 250: 27, 275: 35, 300: 43, 325: 47, 350: 51},
}

QAA_UH_SB_MEDIUM_WOOD = {
    # book_mark_300mm: {diameter_mm: scaled_mark}
    3: {225: 1, 250: 2, 275: 3, 300: 3, 325: 3, 350: 3},
    4: {225: 2, 250: 3, 275: 3, 300: 4, 325: 4, 350: 4},
    5: {225: 2, 250: 3, 275: 4, 300: 4, 325: 4, 350: 4},
    6: {225: 3, 250: 4, 275: 4, 300: 5, 325: 5, 350: 5},
    7: {225: 3, 250: 4, 275: 5, 300: 6, 325: 6, 350: 6},
    8: {225: 3, 250: 4, 275: 6, 300: 7, 325: 7, 350: 7},
    9: {225: 4, 250: 5, 275: 7, 300: 8, 325: 8, 350: 8},
    10: {225: 4, 250: 5, 275: 7, 300: 8, 325: 8, 350: 8},
    11: {225: 4, 250: 6, 275: 8, 300: 9, 325: 9, 350: 9},
    12: {225: 5, 250: 7, 275: 8, 300: 10, 325: 10, 350: 10},
    13: {225: 5, 250: 7, 275: 9, 300: 11, 325: 12, 350: 12},
    14: {225: 6, 250: 8, 275: 10, 300: 12, 325: 12, 350: 13},
    15: {225: 6, 250: 8, 275: 10, 300: 12, 325: 13, 350: 14},
    16: {225: 6, 250: 8, 275: 11, 300: 13, 325: 14, 350: 15},
    17: {225: 7, 250: 9, 275: 12, 300: 14, 325: 15, 350: 16},
    18: {225: 7, 250: 9, 275: 12, 300: 15, 325: 16, 350: 17},
    19: {225: 8, 250: 10, 275: 13, 300: 16, 325: 17, 350: 17},
    20: {225: 8, 250: 11, 275: 14, 300: 17, 325: 17, 350: 19},
    21: {225: 8, 250: 11, 275: 14, 300: 17, 325: 19, 350: 21},
    22: {225: 9, 250: 12, 275: 15, 300: 18, 325: 20, 350: 21},
    23: {225: 9, 250: 12, 275: 16, 300: 19, 325: 21, 350: 22},
    24: {225: 9, 250: 12, 275: 17, 300: 20, 325: 21, 350: 23},
    25: {225: 10, 250: 13, 275: 17, 300: 21, 325: 22, 350: 24},
    26: {225: 10, 250: 13, 275: 17, 300: 21, 325: 23, 350: 25},
    27: {225: 11, 250: 14, 275: 18, 300: 22, 325: 24, 350: 25},
    28: {225: 11, 250: 15, 275: 19, 300: 23, 325: 25, 350: 26},
    29: {225: 11, 250: 15, 275: 20, 300: 24, 325: 25, 350: 28},
    30: {225: 12, 250: 16, 275: 21, 300: 25, 325: 26, 350: 28},
    31: {225: 12, 250: 16, 275: 21, 300: 25, 325: 28, 350: 30},
    32: {225: 13, 250: 17, 275: 21, 300: 26, 325: 29, 350: 31},
    33: {225: 13, 250: 17, 275: 22, 300: 27, 325: 30, 350: 32},
    34: {225: 13, 250: 17, 275: 23, 300: 28, 325: 30, 350: 33},
    35: {225: 14, 250: 18, 275: 24, 300: 29, 325: 31, 350: 34},
    36: {225: 15, 250: 19, 275: 25, 300: 30, 325: 32, 350: 34},
    37: {225: 15, 250: 19, 275: 25, 300: 30, 325: 33, 350: 35},
    38: {225: 16, 250: 20, 275: 25, 300: 31, 325: 34, 350: 37},
    39: {225: 16, 250: 20, 275: 26, 300: 32, 325: 34, 350: 37},
    40: {225: 16, 250: 21, 275: 27, 300: 33, 325: 35, 350: 38},
    41: {225: 16, 250: 21, 275: 28, 300: 34, 325: 37, 350: 40},
    42: {225: 16, 250: 21, 275: 28, 300: 34, 325: 37, 350: 40},
    43: {225: 17, 250: 22, 275: 29, 300: 35, 325: 38, 350: 41},
}

QAA_UH_SB_SOFTWOOD = {
    # book_mark_300mm: {diameter_mm: scaled_mark}
    3: {225: 1, 250: 1, 275: 2, 300: 2, 325: 2, 350: 2},
    4: {225: 2, 250: 2, 275: 2, 300: 3, 325: 3, 350: 3},
    5: {225: 2, 250: 2, 275: 3, 300: 3, 325: 3, 350: 3},
    6: {225: 2, 250: 3, 275: 3, 300: 4, 325: 4, 350: 4},
    7: {225: 2, 250: 3, 275: 4, 300: 4, 325: 4, 350: 4},
    8: {225: 2, 250: 3, 275: 4, 300: 5, 325: 5, 350: 5},
    9: {225: 3, 250: 4, 275: 5, 300: 6, 325: 6, 350: 6},
    10: {225: 3, 250: 4, 275: 5, 300: 6, 325: 6, 350: 6},
    11: {225: 3, 250: 4, 275: 6, 300: 7, 325: 7, 350: 7},
    12: {225: 4, 250: 5, 275: 6, 300: 8, 325: 8, 350: 8},
    13: {225: 4, 250: 5, 275: 7, 300: 8, 325: 9, 350: 9},
    14: {225: 5, 250: 6, 275: 8, 300: 9, 325: 9, 350: 10},
    15: {225: 5, 250: 6, 275: 8, 300: 9, 325: 10, 350: 11},
    16: {225: 5, 250: 6, 275: 8, 300: 10, 325: 11, 350: 11},
    17: {225: 6, 250: 7, 275: 9, 300: 11, 325: 11, 350: 12},
    18: {225: 6, 250: 7, 275: 9, 300: 11, 325: 12, 350: 13},
    19: {225: 6, 250: 8, 275: 10, 300: 12, 325: 13, 350: 13},
    20: {225: 6, 250: 8, 275: 11, 300: 13, 325: 13, 350: 14},
    21: {225: 6, 250: 8, 275: 11, 300: 13, 325: 14, 350: 16},
    22: {225: 7, 250: 9, 275: 11, 300: 14, 325: 15, 350: 16},
    23: {225: 7, 250: 9, 275: 12, 300: 14, 325: 16, 350: 17},
    24: {225: 7, 250: 9, 275: 13, 300: 16, 325: 16, 350: 18},
    25: {225: 8, 250: 10, 275: 13, 300: 16, 325: 17, 350: 18},
    26: {225: 8, 250: 10, 275: 13, 300: 16, 325: 18, 350: 19},
    27: {225: 9, 250: 11, 275: 14, 300: 17, 325: 18, 350: 19},
    28: {225: 9, 250: 11, 275: 14, 300: 18, 325: 19, 350: 20},
    29: {225: 9, 250: 11, 275: 15, 300: 18, 325: 19, 350: 21},
    30: {225: 10, 250: 12, 275: 16, 300: 19, 325: 20, 350: 21},
    31: {225: 10, 250: 12, 275: 16, 300: 19, 325: 21, 350: 23},
    32: {225: 11, 250: 13, 275: 16, 300: 20, 325: 22, 350: 24},
    33: {225: 11, 250: 13, 275: 17, 300: 21, 325: 23, 350: 24},
    34: {225: 11, 250: 13, 275: 18, 300: 21, 325: 23, 350: 25},
    35: {225: 11, 250: 14, 275: 18, 300: 22, 325: 24, 350: 26},
    36: {225: 12, 250: 15, 275: 19, 300: 23, 325: 24, 350: 26},
    37: {225: 12, 250: 15, 275: 19, 300: 23, 325: 25, 350: 27},
    38: {225: 13, 250: 16, 275: 19, 300: 24, 325: 26, 350: 28},
    39: {225: 13, 250: 16, 275: 20, 300: 24, 325: 26, 350: 28},
    40: {225: 13, 250: 16, 275: 20, 300: 25, 325: 27, 350: 29},
    41: {225: 13, 250: 16, 275: 21, 300: 26, 325: 28, 350: 30},
    42: {225: 13, 250: 16, 275: 21, 300: 26, 325: 28, 350: 30},
    43: {225: 14, 250: 17, 275: 22, 300: 27, 325: 30, 350: 31},
}


def get_wood_type_category(species_code: str, wood_df: Optional[pd.DataFrame] = None) -> str:
    """
    Classify a species as 'softwood', 'medium', or 'hardwood' for QAA table selection.

    Uses Janka hardness relative to the QAA peak values.

    Args:
        species_code: Species name or code.
        wood_df: Optional wood properties DataFrame.

    Returns:
        One of: 'softwood', 'medium', 'hardwood'.
    """
    props = get_species_properties(species_code, wood_df)
    janka = props.janka_hardness

    if janka <= QAA_SOFTWOOD_PEAK_N:
        return 'softwood'
    elif janka <= QAA_MEDIUM_PEAK_N:
        return 'medium'
    else:
        return 'hardwood'


# ---------------------------------------------------------------------------
# QAA table interpolation
# ---------------------------------------------------------------------------

def calculate_hardness_blend_weights(effective_janka: float) -> Dict[str, float]:
    """
    Compute triangular membership function weights for QAA table blending.

    Three tables (softwood, medium, hardwood) are blended using linear
    interpolation between their peak Janka values. Weights sum to 1.0.

    Uses triangular membership functions for smooth transitions between
    Softwood, Medium, and Hardwood QAA scaling tables.

    Hardness Ranges (database values appear to be in Newtons):
        Softwood peak: 1300 (Cottonwood, soft White Pine) [~290 lbf]
        Medium peak:   2000 (Yellow Pine, Ponderosa) [~450 lbf]
        Hardwood peak: 2800 (Alder, harder woods) [~630 lbf]

    Transition zones: 700 overlap between categories

    Examples:
        1100 (rotten pine):          100% soft,  0% med,   0% hard
        1690 (avg white pine):        75% soft, 25% med,   0% hard
        2000 (ponderosa pine):        25% soft, 50% med,  25% hard
        2800 (alder):                  0% soft,  0% med, 100% hard

    Args:
        effective_janka: Effective Janka hardness (from calculate_effective_janka_hardness).

    Returns:
        Dict with keys 'softwood', 'medium', 'hardwood'; values in [0.0, 1.0].
    """
    SOFT_PEAK = QAA_SOFTWOOD_PEAK_N
    MED_PEAK = QAA_MEDIUM_PEAK_N
    HARD_PEAK = QAA_HARDWOOD_PEAK_N
    TRANSITION = QAA_TRANSITION_WIDTH

    weights = {
        'softwood': 0.0,
        'medium': 0.0,
        'hardwood': 0.0
    }

    # Softwood weight (triangular: peak at SOFT_PEAK, fade to 0 by SOFT_PEAK+TRANSITION)
    if effective_janka <= SOFT_PEAK:
        weights['softwood'] = 1.0
    elif effective_janka < (SOFT_PEAK + TRANSITION):
        # Linear fade from 1.0 to 0.0
        weights['softwood'] = 1.0 - ((effective_janka - SOFT_PEAK) / TRANSITION)

    # Medium weight (triangular: ramp up from SOFT_PEAK, peak at MED_PEAK, fade by MED_PEAK+TRANSITION)
    if SOFT_PEAK <= effective_janka <= MED_PEAK:
        # Ramp up from 0 to 1
        weights['medium'] = (effective_janka - SOFT_PEAK) / (MED_PEAK - SOFT_PEAK)
    elif MED_PEAK < effective_janka <= (MED_PEAK + TRANSITION):
        # Ramp down from 1 to 0
        weights['medium'] = 1.0 - ((effective_janka - MED_PEAK) / TRANSITION)

    # Hardwood weight (triangular: start at HARD_PEAK-TRANSITION, peak at HARD_PEAK)
    if effective_janka >= HARD_PEAK:
        weights['hardwood'] = 1.0
    elif effective_janka > (HARD_PEAK - TRANSITION):
        # Linear ramp from 0.0 to 1.0
        weights['hardwood'] = (effective_janka - (HARD_PEAK - TRANSITION)) / TRANSITION

    # Normalize to sum to 1.0 (safety check)
    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total for k, v in weights.items()}
    else:
        # Fallback if something went wrong
        weights['medium'] = 1.0

    return weights


def scale_mark_qaa(
    book_mark_300mm: float,
    target_diameter: float,
    wood_type: str = 'hardwood',
) -> Tuple[float, str]:
    """
    Scale a handicap mark using QAA empirical tables.

    Args:
        book_mark_300mm: Competitor's book mark at 300mm standard
        target_diameter: Target diameter in mm (225, 250, 275, 300, 325, 350)
        wood_type: 'hardwood', 'medium', or 'softwood'

    Returns:
        Tuple of (scaled_mark, explanation)

    Example:
        >>> scale_mark_qaa(20, 275, 'hardwood')
        (17, "QAA table: 20s @ 300mm -> 17s @ 275mm (hardwood)")
    """
    # Select appropriate table
    if wood_type == 'softwood':
        table = QAA_UH_SB_SOFTWOOD
    elif wood_type == 'medium':
        table = QAA_UH_SB_MEDIUM_WOOD
    else:  # hardwood or unknown
        table = QAA_UH_SB_HARDWOOD

    # Round book mark to nearest integer for table lookup
    book_mark_int = round(book_mark_300mm)

    # Clamp to valid range (3-43 for Open events)
    if book_mark_int < 3:
        book_mark_int = 3
    elif book_mark_int > 43:
        book_mark_int = 43

    # Round target diameter to nearest standard size
    standard_diameters = [225, 250, 275, 300, 325, 350]
    target_rounded = min(standard_diameters, key=lambda x: abs(x - target_diameter))

    # Look up scaled mark
    if book_mark_int in table and target_rounded in table[book_mark_int]:
        scaled_mark = table[book_mark_int][target_rounded]
        explanation = (
            f"QAA table: {book_mark_int}s @ 300mm = {scaled_mark}s @ "
            f"{target_rounded}mm ({wood_type})"
        )

        # If target wasn't exactly on standard, note the approximation
        if abs(target_rounded - target_diameter) > 5:
            explanation += f" [target {target_diameter}mm rounded to {target_rounded}mm]"

        return float(scaled_mark), explanation

    else:
        # Fallback: use proportional scaling if outside table range
        # This shouldn't happen for normal Open events (3-43 range)
        ratio = target_diameter / 300.0
        scaled_mark = book_mark_300mm * ratio
        explanation = (
            f"Proportional scaling: {book_mark_300mm:.1f}s x {ratio:.3f} = "
            f"{scaled_mark:.1f}s (outside QAA table range)"
        )
        return scaled_mark, explanation


def interpolate_qaa_tables(
    book_mark_300: float,
    target_diameter_mm: float,
    effective_janka: float,
) -> Tuple[float, Dict[str, float]]:
    """
    Interpolate QAA empirical mark tables to get a mark at the target diameter.

    Instead of picking one table, blends all three based on where the wood
    falls on the hardness spectrum.

    Steps:
        1. Compute blend weights via calculate_hardness_blend_weights().
        2. Look up mark in each of the three QAA tables for book_mark_300.
        3. Blend: result = soft*w_soft + med*w_med + hard*w_hard

    Args:
        book_mark_300: Reference mark at 300mm standard diameter (seconds).
        target_diameter_mm: Target block diameter (mm).
        effective_janka: Quality-adjusted Janka hardness (N).

    Returns:
        Tuple of (scaled_mark, weights_dict)

    Example:
        book_mark=20, diameter=275mm, janka=750 (Yellow Pine)

        Hardwood table: 17s
        Medium table:   14s
        Softwood table: 11s

        Weights: 25% soft, 50% med, 25% hard
        Result: (11x0.25) + (14x0.50) + (17x0.25) = 14.0s
    """
    # Get scaled values from each table
    hard_value, _ = scale_mark_qaa(book_mark_300, target_diameter_mm, 'hardwood')
    med_value, _ = scale_mark_qaa(book_mark_300, target_diameter_mm, 'medium')
    soft_value, _ = scale_mark_qaa(book_mark_300, target_diameter_mm, 'softwood')

    # Calculate blend weights
    weights = calculate_hardness_blend_weights(effective_janka)

    # Interpolate
    result = (
        soft_value * weights['softwood']
        + med_value * weights['medium']
        + hard_value * weights['hardwood']
    )

    return result, weights
