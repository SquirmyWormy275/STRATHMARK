"""
Variance Modeling
=================

Absolute +/-3-second performance variance for Monte Carlo simulation.

CRITICAL DESIGN RULE -- enforced here:
    Variance is ABSOLUTE (seconds), never proportional (% of predicted time).

Rationale (from STRATHEX documentation):
    Proportional variance gives faster competitors an unfair advantage.
    Example: a 30s chopper with +/-5% = +/-1.5s range; a 60s chopper gets +/-3s.
    Real-world consistency factors (technique, wood grain, equipment condition)
    affect competitors equally in absolute terms regardless of skill level.
    Testing confirmed absolute +/-3s variance: 6.7% win rate spread vs 31% with
    proportional variance.

Variance components used in Monte Carlo simulation:
    heat_delta      -- shared heat-level variance (wind, grain, moisture conditions)
                      applied identically to all competitors in a heat.
                      Default: Normal(0, 1.0s)
    competitor_std  -- per-competitor performance std-dev derived from historical
                      data by estimate_competitor_std_dev().
                      Clamped: [1.5s, 6.0s]

Source references (STRATHEX):
    woodchopping/simulation/monte_carlo.py  -> simulate_single_race()
    woodchopping/simulation/monte_carlo.py  -> run_monte_carlo_simulation()
    woodchopping/simulation/monte_carlo.py  -> _get_competitor_variance_seconds()
    woodchopping/simulation/monte_carlo.py  -> _calculate_consistency_rating()
    woodchopping/predictions/baseline.py    -> estimate_competitor_std_dev()
    config.py                               -> SimulationConfig (all thresholds)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from strathmark.config import rules, sim_config, baseline_config


# ---------------------------------------------------------------------------
# Constants (mirrored from STRATHEX config.py -> SimulationConfig)
# ---------------------------------------------------------------------------

HEAT_VARIANCE_SECONDS: float = sim_config.HEAT_VARIANCE_SECONDS
"""Shared heat-level variance std-dev (same for all competitors in a heat)."""

MIN_COMPETITOR_STD_SECONDS: float = sim_config.MIN_COMPETITOR_STD_SECONDS
"""Floor on per-competitor std-dev. Even elite choppers have some variance."""

MAX_COMPETITOR_STD_SECONDS: float = sim_config.MAX_COMPETITOR_STD_SECONDS
"""Ceiling on per-competitor std-dev. Prevents unrealistic blow-outs."""

# Consistency rating thresholds (std-dev in seconds)
CONSISTENCY_VERY_HIGH_THRESHOLD: float = sim_config.CONSISTENCY_VERY_HIGH_THRESHOLD
CONSISTENCY_HIGH_THRESHOLD: float = sim_config.CONSISTENCY_HIGH_THRESHOLD
CONSISTENCY_MODERATE_THRESHOLD: float = sim_config.CONSISTENCY_MODERATE_THRESHOLD
# Above 3.5s -> 'Low'


# ---------------------------------------------------------------------------
# Private helpers (ported from STRATHEX baseline.py)
# ---------------------------------------------------------------------------

def _standardize_results_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column names for results DataFrames."""
    if df is None or df.empty:
        return df
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    rename_map = {
        'time': 'raw_time',
        'actualtime': 'raw_time',
        'actual_time': 'raw_time',
        'competitorname': 'competitor_name',
        'competitor name': 'competitor_name',
        'name': 'competitor_name',
        'event_code': 'event',
        'eventcode': 'event',
        'diameter': 'size_mm',
        'diameter_mm': 'size_mm',
    }
    df.rename(columns=rename_map, inplace=True)
    if 'raw_time' in df.columns:
        df['raw_time'] = pd.to_numeric(df['raw_time'], errors='coerce')
    if 'event' in df.columns:
        df['event'] = df['event'].astype(str).str.strip().str.upper()
    if 'competitor_name' in df.columns:
        df['competitor_name'] = df['competitor_name'].astype(str).str.strip()
    return df


def _pooled_std_dev_by_event(
    competitor_data: pd.DataFrame,
    min_samples: int,
) -> Optional[float]:
    """
    Compute a pooled within-group std-dev across event groups for one competitor.
    Pools variance across SB/UH separately to avoid between-event variance inflating
    the estimate.
    """
    if competitor_data is None or competitor_data.empty:
        return None

    times = competitor_data.get('raw_time')
    if times is None:
        return None

    total_samples = int(times.count())
    if total_samples < min_samples:
        return None

    if 'event' not in competitor_data.columns:
        std_dev = float(times.std(ddof=1)) if total_samples >= 2 else None
        return std_dev

    total_df = 0.0
    var_sum = 0.0
    for _, group in competitor_data.groupby('event'):
        group_times = pd.to_numeric(group['raw_time'], errors='coerce').dropna().astype(float)
        if len(group_times) < 2:
            continue
        var = float(group_times.var(ddof=1))
        df_val = float(len(group_times) - 1)
        var_sum += var * df_val
        total_df += df_val

    if total_df <= 0:
        return None

    return float(np.sqrt(var_sum / total_df))


def _global_fallback_std_dev(
    results_df: pd.DataFrame,
    min_samples: int,
) -> Optional[float]:
    """
    Compute dataset-level median std-dev across all competitors.
    Used when a specific competitor has too few results.
    """
    if results_df is None or results_df.empty:
        return None

    pooled_values = []
    for _, group in results_df.groupby('competitor_name'):
        pooled = _pooled_std_dev_by_event(group, min_samples)
        if pooled is not None:
            pooled_values.append(pooled)

    if not pooled_values:
        return None

    return float(np.median(pooled_values))


# ---------------------------------------------------------------------------
# Per-competitor variance estimation
# ---------------------------------------------------------------------------

def estimate_competitor_std_dev(
    competitor_name: str,
    event_code: str,
    results_df,
    min_samples: int = 3,
) -> Tuple[float, str]:
    """
    Estimate per-competitor performance std-dev from historical results.

    Uses pooled variance across events for the competitor (data-driven and stable).
    Falls back to a dataset-level median std-dev when the competitor lacks samples.
    Finally falls back to the default clamped value if no data is available.

    Uses IQR-robust calculation (outlier-resistant) from STRATHEX Baseline V2.
    Falls back to event-level std-dev if competitor has sparse data.
    Finally falls back to the default clamped value if no data is available.

    Clamping: result is always in [MIN_COMPETITOR_STD_SECONDS, MAX_COMPETITOR_STD_SECONDS].

    Args:
        competitor_name: Competitor display name.
        event_code: 'SB' or 'UH'.
        results_df: Historical results DataFrame. Must contain columns:
                    competitor_name, event, raw_time.
        min_samples: Minimum samples required to compute competitor-specific std-dev.
                     Below this threshold, falls back to event-level std-dev.

    Returns:
        Tuple of (std_dev_seconds, source_description) where:
            std_dev_seconds  -- clamped std-dev in [1.5, 6.0]
            source_description -- 'VERY HIGH' | 'HIGH' | 'MODERATE' | 'LOW'
    """
    if results_df is None:
        return rules.PERFORMANCE_VARIANCE_SECONDS, "MODERATE"

    # Convert to DataFrame if needed (e.g., passed a list of HistoricalResult)
    if not isinstance(results_df, pd.DataFrame):
        return rules.PERFORMANCE_VARIANCE_SECONDS, "MODERATE"

    if results_df.empty:
        return rules.PERFORMANCE_VARIANCE_SECONDS, "MODERATE"

    # Standardize if needed
    if 'raw_time' not in results_df.columns:
        results_df = _standardize_results_columns(results_df)

    comp_match = (
        results_df['competitor_name'].astype(str).str.strip().str.lower()
        == str(competitor_name).strip().lower()
    )
    competitor_data = results_df[comp_match]

    min_s = baseline_config.MIN_SAMPLES_FOR_STD_DEV
    pooled_std = _pooled_std_dev_by_event(competitor_data, min_s)
    if pooled_std is None:
        pooled_std = _global_fallback_std_dev(results_df, min_s)

    if pooled_std is None:
        pooled_std = float(rules.PERFORMANCE_VARIANCE_SECONDS)

    std_dev = max(
        MIN_COMPETITOR_STD_SECONDS,
        min(float(pooled_std), MAX_COMPETITOR_STD_SECONDS)
    )

    if std_dev <= baseline_config.CONSISTENCY_VERY_HIGH_THRESHOLD:
        consistency_rating = "VERY HIGH"
    elif std_dev <= baseline_config.CONSISTENCY_HIGH_THRESHOLD:
        consistency_rating = "HIGH"
    elif std_dev <= baseline_config.CONSISTENCY_MODERATE_THRESHOLD:
        consistency_rating = "MODERATE"
    else:
        consistency_rating = "LOW"

    return std_dev, consistency_rating


# ---------------------------------------------------------------------------
# Consistency rating
# ---------------------------------------------------------------------------

def calculate_consistency_rating(std_dev_seconds: float) -> str:
    """
    Map a std-dev value to a human-readable consistency rating.

    Rate competitor consistency based on finish time standard deviation.

    This rating indicates how predictable a competitor's performance is across
    thousands of simulated races. Lower standard deviation means more consistent
    (predictable) performance.

    Thresholds (from STRATHEX config.py -> BaselineV2HybridConfig):
        Very High  std_dev <= 2.5s  (elite consistency, very predictable)
        High       std_dev <= 3.0s  (expected variance, matches +/-3s model)
        Moderate   std_dev <= 3.5s  (slightly above expected)
        Low        std_dev >  3.5s  (high variability, unpredictable)

    Note:
        The default +/-3 second variance model assumes all competitors have equal
        absolute variance. When per-competitor variance is provided, expected
        std-dev may shift accordingly.

    Args:
        std_dev_seconds: Per-competitor std-dev estimate in seconds.

    Returns:
        One of: 'Very High (low variance)', 'High (expected variance)',
                'Moderate (above expected)', 'Low (high variance)'.
    """
    if std_dev_seconds <= CONSISTENCY_VERY_HIGH_THRESHOLD:
        return "Very High (low variance)"
    elif std_dev_seconds <= CONSISTENCY_HIGH_THRESHOLD:
        return "High (expected variance)"
    elif std_dev_seconds <= CONSISTENCY_MODERATE_THRESHOLD:
        return "Moderate (above expected)"
    else:
        return "Low (high variance)"


# ---------------------------------------------------------------------------
# Simulation result types
# ---------------------------------------------------------------------------

@dataclass
class CompetitorTimeStats:
    """
    Per-competitor finish time statistics collected across Monte Carlo simulations.

    Populated by run_monte_carlo_simulation() and exposed to championship
    simulator and fairness analysis.
    """

    name: str
    mean: float
    """Mean finish time across all simulations (seconds)."""

    std_dev: float
    """Std-dev of finish times. Should be close to competitor_std + heat_variance."""

    min_time: float
    max_time: float
    p25: float
    p50: float
    p75: float
    consistency_rating: str
    """Output of calculate_consistency_rating(std_dev)."""


# ---------------------------------------------------------------------------
# Monte Carlo simulation
# ---------------------------------------------------------------------------

def _get_competitor_variance_seconds(comp: Dict) -> float:
    """
    Return per-competitor variance (std-dev) with reasonable bounds.

    Accepts a competitor dict with optional 'performance_std_dev', 'std_dev',
    or falls back to the default PERFORMANCE_VARIANCE_SECONDS.
    """
    # Support both 'performance_std_dev' (STRATHEX legacy) and 'std_dev' (STRATHMARK)
    variance = comp.get('performance_std_dev') or comp.get('std_dev')
    if variance is None:
        variance = rules.PERFORMANCE_VARIANCE_SECONDS

    try:
        variance = float(variance)
        if np.isnan(variance):
            variance = rules.PERFORMANCE_VARIANCE_SECONDS
    except (TypeError, ValueError):
        variance = rules.PERFORMANCE_VARIANCE_SECONDS

    variance = max(MIN_COMPETITOR_STD_SECONDS, min(variance, MAX_COMPETITOR_STD_SECONDS))
    return variance


def simulate_single_race(
    competitors: List[Dict],
    heat_variance_seconds: float = HEAT_VARIANCE_SECONDS,
    rng=None,
) -> str:
    """
    Simulate one race and return the winner's name.

    Each competitor's actual time is sampled as:
        heat_delta   = Normal(0, heat_variance_seconds)   # shared
        actual_time  = Normal(predicted_time + heat_delta, competitor_std_dev)
        actual_time  = max(actual_time, predicted_time * 0.5)  # sanity floor
        finish_time  = (mark - 3) + actual_time

    The competitor with the lowest finish_time wins.

    INVARIANT: competitor_std_dev is always absolute seconds (never a % of
    predicted_time). This is enforced by _get_competitor_variance_seconds().

    Args:
        competitors: List of dicts, each with:
                     'name', 'mark', 'predicted_time', and optionally
                     'performance_std_dev' or 'std_dev' for per-competitor variance.
        heat_variance_seconds: Std-dev of shared heat-level noise.
        rng: Optional numpy random Generator for reproducibility.

    Returns:
        Name of the winning competitor.
    """
    if rng is None:
        rand_normal = np.random.normal
    else:
        rand_normal = rng.normal

    finish_results = []

    # Shared heat conditions (wind, grain pattern, moisture) affect everyone equally
    heat_delta = rand_normal(0.0, heat_variance_seconds)

    for comp in competitors:
        # Per-competitor variance with shared heat effect
        variance_seconds = _get_competitor_variance_seconds(comp)
        actual_time = rand_normal(
            comp['predicted_time'] + heat_delta,
            variance_seconds
        )

        # Prevent unreasonably fast times (minimum 50% of predicted time)
        actual_time = max(actual_time, comp['predicted_time'] * 0.5)

        # Calculate finish time accounting for handicap
        # Front marker (mark=3) starts immediately; start_delay = 0
        start_delay = comp['mark'] - rules.MIN_MARK_SECONDS
        finish_time = start_delay + actual_time

        finish_results.append({
            'name': comp['name'],
            'finish_time': finish_time,
        })

    # Sort by finish time; return winner name
    finish_results.sort(key=lambda x: x['finish_time'])
    return finish_results[0]['name']


def run_monte_carlo_simulation(
    competitors: List[Dict],
    num_simulations: int = 2_000_000,
    heat_variance_seconds: float = HEAT_VARIANCE_SECONDS,
    seed: Optional[int] = None,
    track_finish_orders: bool = False,
    track_podium_margins: bool = False,
    show_live_leaders: bool = False,
    progress_interval: int = 50000,
) -> Dict:
    """
    Run num_simulations races and return fairness statistics.

    Simulates thousands of races to determine if all competitors have equal
    probability of winning. Tracks win rates, finish positions, and spread statistics.

    INVARIANT: Variance is absolute seconds (never proportional). See module docstring.

    Args:
        competitors: List of dicts, each containing:
                     'name', 'mark', 'predicted_time', and optionally
                     'performance_std_dev' or 'std_dev'.
                     Same format as simulate_single_race().
        num_simulations: Number of race iterations (default 2M for high precision).
        heat_variance_seconds: Shared heat-level noise std-dev.
        seed: Optional random seed for reproducibility.

    Returns:
        Analysis dict with keys:
            num_simulations         -- int
            winner_counts           -- {name: count}
            winner_percentages      -- {name: win_pct}  (0.0-100.0)
            podium_counts           -- {name: top_3_count}
            podium_percentages      -- {name: top_3_pct}
            avg_finish_positions    -- {name: avg_position}  (1.0 = always wins)
            front_marker_name       -- name of slowest predicted competitor
            back_marker_name        -- name of fastest predicted competitor
            front_marker_wins       -- win count for front marker
            back_marker_wins        -- win count for back marker
            competitors             -- original competitors list
            competitor_time_stats   -- {name: CompetitorTimeStats}
            heat_variance_seconds   -- float
            finish_spreads          -- list of per-race finish spread values
            avg_spread              -- float
            median_spread           -- float
            min_spread              -- float
            max_spread              -- float
            tight_finish_prob       -- fraction of races within 10 seconds
            very_tight_finish_prob  -- fraction of races within 5 seconds

    Fairness assessment (based on win rate spread):
        EXCELLENT  win rate spread <= 2%  (all competitors near-equal chance)
        VERY GOOD  win rate spread <= 5%
        GOOD       win rate spread <= 10%
        FAIR       win rate spread <= 15%
        POOR       win rate spread >  15%

    Statistical Significance:
        With 2M simulations, margin of error is extremely small (<0.1%).
        Even 1-2% win rate differences are statistically meaningful.
    """
    if seed is not None:
        rng = np.random.default_rng(seed)
    else:
        rng = None  # Use module-level numpy random state for speed

    # Track statistics
    finish_spreads = []
    winner_counts = {comp['name']: 0 for comp in competitors}
    podium_counts = {comp['name']: 0 for comp in competitors}
    finish_position_sums = {comp['name']: 0 for comp in competitors}
    # Track individual finish times for per-competitor statistics
    competitor_finish_times = {comp['name']: [] for comp in competitors}

    # Optional tracking for finish orders, podium margins, photo-finish
    order_counts: Optional[Dict] = {} if track_finish_orders else None
    order_scope = "podium" if track_finish_orders and len(competitors) > 8 else "full"
    margin_12_sum = 0.0
    margin_23_sum = 0.0
    margin_12_count = 0
    margin_23_count = 0
    photo_finish_count = 0
    photo_finish_threshold = 0.25

    # Track front marker (slowest predicted, starts first)
    front_marker_name = max(competitors, key=lambda x: x['predicted_time'])['name']
    back_marker_name = min(competitors, key=lambda x: x['predicted_time'])['name']

    print(f"\nRUNNING MONTE CARLO SIMULATION ({num_simulations:,} races)")
    print(f"Simulating races with per-competitor variance and +/-{heat_variance_seconds:.1f}s heat variance...")

    for i in range(num_simulations):
        if progress_interval and (i + 1) % progress_interval == 0:
            print(f"  Completed {i + 1:,}/{num_simulations:,} simulations...")
            if show_live_leaders:
                leaders = sorted(winner_counts.items(), key=lambda x: x[1], reverse=True)[:3]
                leader_str = ", ".join(
                    [f"{name} {count / (i + 1) * 100:.1f}%" for name, count in leaders]
                )
                print(f"    Leaders: {leader_str}")

        # Run one race (full result needed for spread and position tracking)
        if rng is None:
            rand_normal = np.random.normal
        else:
            rand_normal = rng.normal

        heat_delta = rand_normal(0.0, heat_variance_seconds)
        race_results = []

        for comp in competitors:
            variance_seconds = _get_competitor_variance_seconds(comp)
            actual_time = rand_normal(comp['predicted_time'] + heat_delta, variance_seconds)
            actual_time = max(actual_time, comp['predicted_time'] * 0.5)
            start_delay = comp['mark'] - rules.MIN_MARK_SECONDS
            finish_time = start_delay + actual_time

            race_results.append({
                'name': comp['name'],
                'finish_time': finish_time,
                'actual_time': actual_time,
            })

        race_results.sort(key=lambda x: x['finish_time'])

        # Calculate finish spread
        spread = race_results[-1]['finish_time'] - race_results[0]['finish_time']
        finish_spreads.append(spread)

        # Track winner
        winner_counts[race_results[0]['name']] += 1

        # Track podium (top 3)
        for j in range(min(3, len(race_results))):
            podium_counts[race_results[j]['name']] += 1

        # Track average finish positions
        for pos, result in enumerate(race_results, 1):
            finish_position_sums[result['name']] += pos

        # Track individual finish times for per-competitor statistics
        for result in race_results:
            competitor_finish_times[result['name']].append(result['finish_time'])

        # Track podium margins and photo-finish rate
        if track_podium_margins and len(race_results) >= 2:
            margin_12 = race_results[1]['finish_time'] - race_results[0]['finish_time']
            margin_12_sum += margin_12
            margin_12_count += 1
            if margin_12 <= photo_finish_threshold:
                photo_finish_count += 1
            if len(race_results) >= 3:
                margin_23 = race_results[2]['finish_time'] - race_results[1]['finish_time']
                margin_23_sum += margin_23
                margin_23_count += 1

        # Track most common finish order
        if order_counts is not None:
            if order_scope == "full":
                order_key = tuple(r['name'] for r in race_results)
            else:
                order_key = tuple(r['name'] for r in race_results[:3])
            order_counts[order_key] = order_counts.get(order_key, 0) + 1

    # Calculate statistics
    avg_finish_positions = {
        name: pos_sum / num_simulations
        for name, pos_sum in finish_position_sums.items()
    }

    # Calculate per-competitor time statistics
    competitor_time_stats = {}
    for name, times in competitor_finish_times.items():
        times_array = np.array(times)
        std = float(np.std(times_array))
        competitor_time_stats[name] = CompetitorTimeStats(
            name=name,
            mean=float(np.mean(times_array)),
            std_dev=std,
            min_time=float(np.min(times_array)),
            max_time=float(np.max(times_array)),
            p25=float(np.percentile(times_array, 25)),
            p50=float(np.percentile(times_array, 50)),
            p75=float(np.percentile(times_array, 75)),
            consistency_rating=calculate_consistency_rating(std),
        )

    spreads_arr = np.array(finish_spreads)

    # Derive optional tracking results
    most_common_order = None
    most_common_order_pct = None
    if order_counts:
        most_common_order = max(order_counts.items(), key=lambda x: x[1])[0]
        most_common_order_pct = (order_counts[most_common_order] / num_simulations) * 100.0

    avg_margin_12 = (margin_12_sum / margin_12_count) if margin_12_count else None
    avg_margin_23 = (margin_23_sum / margin_23_count) if margin_23_count else None
    photo_finish_pct = (
        (photo_finish_count / margin_12_count * 100.0) if margin_12_count else None
    )

    competitor_variances = {
        comp['name']: _get_competitor_variance_seconds(comp)
        for comp in competitors
    }

    analysis = {
        'num_simulations': num_simulations,
        'finish_spreads': finish_spreads,
        'avg_spread': float(np.mean(spreads_arr)),
        'median_spread': float(np.median(spreads_arr)),
        'min_spread': float(np.min(spreads_arr)),
        'max_spread': float(np.max(spreads_arr)),
        'tight_finish_prob': float(np.sum(spreads_arr < 10) / num_simulations),
        'very_tight_finish_prob': float(np.sum(spreads_arr < 5) / num_simulations),
        'winner_counts': winner_counts,
        'winner_percentages': {
            name: (count / num_simulations * 100)
            for name, count in winner_counts.items()
        },
        'podium_counts': podium_counts,
        'podium_percentages': {
            name: (count / num_simulations * 100)
            for name, count in podium_counts.items()
        },
        'avg_finish_positions': avg_finish_positions,
        'front_marker_name': front_marker_name,
        'back_marker_name': back_marker_name,
        'front_marker_wins': winner_counts[front_marker_name],
        'back_marker_wins': winner_counts[back_marker_name],
        'competitors': competitors,
        'competitor_time_stats': competitor_time_stats,
        'heat_variance_seconds': heat_variance_seconds,
        'competitor_variances': competitor_variances,
        # Optional finish order tracking
        'most_common_order': most_common_order,
        'most_common_order_pct': most_common_order_pct,
        'most_common_order_scope': order_scope if order_counts is not None else None,
        # Optional podium margin tracking
        'avg_podium_margin_12': avg_margin_12,
        'avg_podium_margin_23': avg_margin_23,
        'photo_finish_pct': photo_finish_pct,
        'photo_finish_threshold': photo_finish_threshold,
    }

    return analysis
