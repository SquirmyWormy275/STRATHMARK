"""
Wood Properties and Factors
============================

Species hardness lookup, diameter scaling, and legacy quality helpers.

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

    3. Legacy quality adjustment helpers (not used numerically by V2)
       Wood firmness rated 1-10 (5 = average reference, no adjustment).
       Linear Janka adjustment: effective_janka = base_janka * (1 + (quality-5)*0.1)
           quality 1  -> 0.6x base Janka (very soft/rotten, faster times)
           quality 5  -> 1.0x base Janka (average, baseline reference)
           quality 10 -> 1.5x base Janka (extremely hard, slower times)
       LLM multiplier range: 0.85-1.15 on predicted time.
       Statistical fallback: +-2% per quality point from 5.

Source references (STRATHEX):
    woodchopping/predictions/diameter_scaling.py -> scale_time()
    woodchopping/predictions/diameter_scaling.py -> calculate_scaling_factor()
    woodchopping/predictions/diameter_scaling.py -> calibrate_scaling_exponent()
    woodchopping/predictions/diameter_scaling.py -> get_event_scaling_exponent()
    woodchopping/predictions/baseline.py         -> calculate_effective_janka_hardness()
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

DEFAULT_SCALING_EXPONENT_SB: float = 1.8
"""
Standing Block diameter scaling exponent (fallback when calibration unavailable).
Derived from within-competitor same-species diameter pairs (n=11, median=1.26,
MAE-optimized=1.8 with species normalization). Standing block has shallower
diameter scaling than underhand due to the different cutting mechanics.
"""

DEFAULT_SCALING_EXPONENT_UH: float = 2.1
"""
Underhand diameter scaling exponent (fallback when calibration unavailable).
Derived from within-competitor same-species diameter pairs (n=26, median=2.09,
MAE-optimized=2.1 with species normalization). Underhand has steeper diameter
scaling because the competitor must cut through the full cross-section from above.
"""

DEFAULT_SCALING_EXPONENT: float = 2.0
"""
Generic fallback diameter scaling exponent used when event type is unknown.
Midpoint of the SB (1.8) and UH (2.1) event-specific defaults.
Preserved for backward compatibility with any code that references this constant.
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

# ---------------------------------------------------------------------------
# Empirical species time multipliers (vs S01 reference)
# ---------------------------------------------------------------------------

SPECIES_TIME_MULTIPLIERS: Dict[str, float] = {
    # Empirical median time multiplier vs S01 (eastern white pine = 1.000).
    # Derived from 186 within-competitor, diameter-controlled cross-species
    # comparisons in woodchopping_clean.xlsx. A multiplier of 1.317 means
    # the same competitor takes 31.7% longer on that species vs S01.
    #
    # Keyed by speciesID code. The lookup function also resolves species names.
    "S01": 1.000,  # eastern white pine (reference)
    "S02": 1.100,  # yellow-poplar (estimated from hardness — no direct data)
    "S03": 1.132,  # quaking aspen (n=14 comparisons)
    "S04": 1.238,  # alder (n=16)
    "S05": 1.317,  # ponderosa pine (n=20)
    "S06": 1.195,  # western white pine (n=36)
    "S07": 1.000,  # sugar pine (estimated — similar hardness to S01)
    "S08": 1.034,  # cottonwood (n=6)
    "S09": 1.131,  # poplar Hybrid (n=13)
    "S10": 0.971,  # poplar European (n=81)
    "S11": 1.050,  # poplar Lombardi (estimated from hardness)
    "S12": 1.400,  # Monterey pine (estimated — highest Janka in table)
    "S13": 1.050,  # basswood (estimated from hardness)
}
"""
Empirical species time multipliers relative to S01 (eastern white pine).
Values derived from within-competitor cross-species comparisons where the same
competitor cut both S01 and species X at controlled diameters. Entries marked
'estimated' use shear-strength regression for species with insufficient direct data.
"""

# Reverse lookup: species name (lowercase) -> speciesID code
_SPECIES_NAME_TO_ID: Dict[str, str] = {
    "eastern white pine": "S01",
    "yellow-poplar": "S02",
    "quaking aspen": "S03",
    "alder": "S04",
    "ponderosa pine": "S05",
    "western white pine": "S06",
    "sugar pine": "S07",
    "cottonwood": "S08",
    "poplar (hybrid)": "S09",
    "poplar hybrid": "S09",
    "poplar (european)": "S10",
    "poplar european": "S10",
    "poplar (lombardi)": "S11",
    "poplar lombardi": "S11",
    "monterey pine": "S12",
    "basswood": "S13",
}


def get_species_time_multiplier(species: str) -> float:
    """
    Return the empirical time multiplier for a species relative to S01 (1.000).

    Accepts either a speciesID code (e.g. 'S05') or a species name
    (e.g. 'ponderosa pine'). Returns 1.0 for unrecognized species.
    """
    key = str(species).strip()

    # Try direct speciesID lookup (case-insensitive)
    upper = key.upper()
    if upper in SPECIES_TIME_MULTIPLIERS:
        return SPECIES_TIME_MULTIPLIERS[upper]

    # Try species name lookup
    lower = key.lower()
    sid = _SPECIES_NAME_TO_ID.get(lower)
    if sid is not None:
        return SPECIES_TIME_MULTIPLIERS.get(sid, 1.0)

    return 1.0


def estimate_species_multiplier_from_shear(
    species: str,
    wood_df: Optional[pd.DataFrame] = None,
) -> float:
    """
    Fallback: estimate species time multiplier from shear strength ratio vs S01.

    Formula: multiplier = (species_shear / S01_shear) ^ 0.97
    S01 shear = 900. Best single-property approximation (RMSE = 0.096).

    Used only for species not in the SPECIES_TIME_MULTIPLIERS table.
    """
    mult = get_species_time_multiplier(species)
    if mult != 1.0:
        # Already in the empirical table — use the empirical value
        return mult

    props = get_species_properties(species, wood_df)
    S01_SHEAR = 900.0
    ratio = props.shear_strength / S01_SHEAR
    if ratio <= 0:
        return 1.0
    return float(ratio**0.97)


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
    if "species" in wood_df.columns:
        match = wood_df[
            wood_df["species"].astype(str).str.strip().str.lower() == str(species).strip().lower()
        ]
        if not match.empty:
            row = match.iloc[0]

    if row is None and "speciesID" in wood_df.columns:
        match = wood_df[
            wood_df["speciesID"].astype(str).str.strip().str.upper() == str(species).strip().upper()
        ]
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
        janka_hardness=_get("janka_hard", DEFAULT_JANKA_HARDNESS),
        specific_gravity=_get("spec_gravity", DEFAULT_SPECIFIC_GRAVITY),
        shear_strength=_get("shear", 1000.0),
        crush_strength=_get("crush_strength", 4000.0),
        mor=_get("MOR", 8000.0),
        moe=_get("MOE", 1000000.0),
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
    scaling_factor = ratio**exponent

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

    confidence_map = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}
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
        results_df["event"].astype(str).str.strip().str.upper() == event_code.strip().upper()
    ].copy()

    if len(event_data) < min_samples:
        return None

    if "size_mm" not in event_data.columns or "raw_time" not in event_data.columns:
        return None

    # Find competitors with multiple diameter sizes
    competitor_diameters = event_data.groupby("competitor_name")["size_mm"].nunique()
    multi_diameter_competitors = competitor_diameters[competitor_diameters >= 2].index

    if len(multi_diameter_competitors) == 0:
        return None

    exponents = []

    for comp in multi_diameter_competitors:
        comp_data = event_data[event_data["competitor_name"] == comp]

        # Get average time for each diameter
        diameter_times = comp_data.groupby("size_mm")["raw_time"].mean()

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

    Falls back to the event-specific default exponent when calibration is not possible.
    Results are cached per event code in the module-level _event_exponent_cache.
    """
    event_key = str(event_code).strip().upper()
    if event_key in _event_exponent_cache:
        return _event_exponent_cache[event_key]

    # Event-specific default fallback
    if event_key == "SB":
        default = DEFAULT_SCALING_EXPONENT_SB
    elif event_key == "UH":
        default = DEFAULT_SCALING_EXPONENT_UH
    else:
        default = DEFAULT_SCALING_EXPONENT

    if results_df is None or results_df.empty:
        _event_exponent_cache[event_key] = default
        return default

    # Standardize column names if needed
    df = _standardize_results_columns(results_df)

    exponent = calibrate_scaling_exponent(df, event_key)
    if exponent is None:
        exponent = default

    _event_exponent_cache[event_key] = float(exponent)
    return float(exponent)


from strathmark.utils import standardize_results_columns as _standardize_results_columns

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

    Formula:
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
