"""
Tests for strathmark.variance — absolute variance model and Monte Carlo simulation.

These tests validate the critical invariant:
    Variance is ABSOLUTE (seconds), NEVER proportional (% of predicted time).

They also validate:
    - Competitor std-dev is clamped to [1.5, 6.0] seconds
    - Consistency ratings map to correct thresholds
    - Monte Carlo results sum to ~100% win rates
    - Fairness thresholds are correct
"""

import pytest

from strathmark.variance import (
    simulate_single_race,
    run_monte_carlo_simulation,
    calculate_consistency_rating,
    _get_competitor_variance_seconds,
    MIN_COMPETITOR_STD_SECONDS,
    MAX_COMPETITOR_STD_SECONDS,
    CONSISTENCY_VERY_HIGH_THRESHOLD,
    CONSISTENCY_HIGH_THRESHOLD,
    CONSISTENCY_MODERATE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _comp(name: str, mark: int, predicted_time: float, std_dev: float = None) -> dict:
    """Build a minimal competitor dict for simulation."""
    d = {"name": name, "mark": mark, "predicted_time": predicted_time}
    if std_dev is not None:
        d["performance_std_dev"] = std_dev
    return d


# ---------------------------------------------------------------------------
# Absolute variance invariant
# ---------------------------------------------------------------------------

class TestAbsoluteVariance:
    """Variance must be absolute seconds, never proportional."""

    def test_std_dev_same_regardless_of_predicted_time(self):
        """
        A competitor predicted at 30s and one at 90s must have the same
        std-dev if their historical data shows identical consistency.
        Proportional variance would give 30s chopper half the variance -- wrong.
        """
        fast_comp = _comp("Fast", 30, 30.0, std_dev=3.0)
        slow_comp = _comp("Slow", 3, 90.0, std_dev=3.0)

        fast_std = _get_competitor_variance_seconds(fast_comp)
        slow_std = _get_competitor_variance_seconds(slow_comp)

        # Both return exactly 3.0 -- absolute, not scaled by predicted_time
        assert fast_std == slow_std == 3.0

    def test_simulate_single_race_uses_absolute_seconds(self):
        """
        With equal handicap marks and equal std_dev, competitors at very
        different predicted times should show similar absolute spread in
        their finish time distributions (not proportional spread).
        """
        # Mark both so finish times cluster around the same point
        # Fast competitor: predicted=30s, mark=63 -> expected finish ~90s
        # Slow competitor: predicted=90s, mark=3  -> expected finish ~90s
        fast = _comp("Fast", 63, 30.0, std_dev=3.0)
        slow = _comp("Slow", 3, 90.0, std_dev=3.0)

        result = run_monte_carlo_simulation(
            [fast, slow], num_simulations=10_000, seed=42, verbose=False
        )

        fast_stats = result["competitor_time_stats"]["Fast"]
        slow_stats = result["competitor_time_stats"]["Slow"]

        # Both should have similar std_dev (absolute variance model)
        # If proportional: Fast would have ~1.5s, Slow would have ~4.5s
        # If absolute: both should be ~3s regardless
        diff = abs(fast_stats.std_dev - slow_stats.std_dev)
        assert diff < 1.5, (
            f"Std-devs differ by {diff:.2f}s — suggests proportional variance. "
            f"Fast={fast_stats.std_dev:.2f}s, Slow={slow_stats.std_dev:.2f}s"
        )


# ---------------------------------------------------------------------------
# Std-dev clamping
# ---------------------------------------------------------------------------

class TestStdDevClamping:
    """Per-competitor std-dev is clamped to [1.5, 6.0] seconds."""

    def test_very_consistent_competitor_clamped_at_floor(self):
        """
        A competitor with historically sub-1.5s variation gets clamped up to 1.5s.
        Even elite choppers have some irreducible variance.
        """
        comp = _comp("Elite", 3, 30.0, std_dev=0.1)
        result = _get_competitor_variance_seconds(comp)
        assert result == MIN_COMPETITOR_STD_SECONDS, (
            f"Expected floor {MIN_COMPETITOR_STD_SECONDS}s, got {result}s"
        )

    def test_very_inconsistent_competitor_clamped_at_ceiling(self):
        """
        A competitor with historically >6.0s variation gets clamped down to 6.0s.
        Prevents unrealistic blow-outs in simulation.
        """
        comp = _comp("Wildcard", 3, 30.0, std_dev=99.9)
        result = _get_competitor_variance_seconds(comp)
        assert result == MAX_COMPETITOR_STD_SECONDS, (
            f"Expected ceiling {MAX_COMPETITOR_STD_SECONDS}s, got {result}s"
        )


# ---------------------------------------------------------------------------
# Consistency ratings
# ---------------------------------------------------------------------------

class TestConsistencyRating:
    """calculate_consistency_rating() thresholds."""

    def test_very_high_at_boundary(self):
        """std_dev = 2.5 -> 'Very High'; std_dev = 2.51 -> 'High'."""
        at_boundary = calculate_consistency_rating(CONSISTENCY_VERY_HIGH_THRESHOLD)
        just_above = calculate_consistency_rating(CONSISTENCY_VERY_HIGH_THRESHOLD + 0.01)

        assert at_boundary.startswith("Very High"), (
            f"Expected 'Very High' at {CONSISTENCY_VERY_HIGH_THRESHOLD}s, got '{at_boundary}'"
        )
        assert just_above.startswith("High") and "Very" not in just_above, (
            f"Expected 'High' at {CONSISTENCY_VERY_HIGH_THRESHOLD + 0.01}s, got '{just_above}'"
        )

    def test_high_at_boundary(self):
        """std_dev = 3.0 -> 'High'; std_dev = 3.01 -> 'Moderate'."""
        at_boundary = calculate_consistency_rating(CONSISTENCY_HIGH_THRESHOLD)
        just_above = calculate_consistency_rating(CONSISTENCY_HIGH_THRESHOLD + 0.01)

        assert at_boundary.startswith("High"), (
            f"Expected 'High' at {CONSISTENCY_HIGH_THRESHOLD}s, got '{at_boundary}'"
        )
        assert just_above.startswith("Moderate"), (
            f"Expected 'Moderate' at {CONSISTENCY_HIGH_THRESHOLD + 0.01}s, got '{just_above}'"
        )

    def test_moderate_at_boundary(self):
        """std_dev = 3.5 -> 'Moderate'; std_dev = 3.51 -> 'Low'."""
        at_boundary = calculate_consistency_rating(CONSISTENCY_MODERATE_THRESHOLD)
        just_above = calculate_consistency_rating(CONSISTENCY_MODERATE_THRESHOLD + 0.01)

        assert at_boundary.startswith("Moderate"), (
            f"Expected 'Moderate' at {CONSISTENCY_MODERATE_THRESHOLD}s, got '{at_boundary}'"
        )
        assert just_above.startswith("Low"), (
            f"Expected 'Low' at {CONSISTENCY_MODERATE_THRESHOLD + 0.01}s, got '{just_above}'"
        )

    def test_low_above_threshold(self):
        """std_dev = 4.0 -> 'Low'."""
        result = calculate_consistency_rating(4.0)
        assert result.startswith("Low"), (
            f"Expected 'Low' at 4.0s, got '{result}'"
        )


# ---------------------------------------------------------------------------
# Monte Carlo simulation
# ---------------------------------------------------------------------------

class TestMonteCarlo:
    """run_monte_carlo_simulation() statistical properties."""

    def _equal_field(self, n: int = 4, num_simulations: int = 10_000) -> dict:
        """All competitors with mark=3 and same predicted time -> equal win rates."""
        competitors = [
            _comp(f"Comp{i}", 3, 30.0, std_dev=3.0)
            for i in range(n)
        ]
        return run_monte_carlo_simulation(
            competitors, num_simulations=num_simulations, seed=42, verbose=False
        )

    def test_win_rates_sum_to_100(self):
        """All winner_percentages values must sum to 100.0 (within floating point tolerance)."""
        result = self._equal_field(4, 10_000)
        total = sum(result["winner_percentages"].values())
        assert abs(total - 100.0) < 0.01, (
            f"Win rates sum to {total:.4f}, expected 100.0"
        )

    def test_each_competitor_can_win(self):
        """
        With properly handicapped competitors (all marks from same calculator),
        each competitor should have a win rate > 0. No competitor should be
        guaranteed to lose.
        """
        result = self._equal_field(4, 10_000)
        for name, pct in result["winner_percentages"].items():
            assert pct > 0.0, f"{name} has 0% win rate in {result['num_simulations']:,} races"

    def test_perfect_handicaps_produce_excellent_fairness(self):
        """
        When all competitors are assigned identical predicted times (and thus
        identical marks), win rates should be nearly equal -> EXCELLENT fairness.
        """
        n = 4
        result = self._equal_field(n, 100_000)
        percentages = list(result["winner_percentages"].values())
        spread = max(percentages) - min(percentages)
        ideal = 100.0 / n  # 25.0%

        # With 100K simulations and equal setup, spread should be < 5%
        assert spread < 5.0, (
            f"Win rate spread {spread:.2f}% too high for equal-handicap field "
            f"(ideal: {ideal:.1f}% per competitor)"
        )

    def test_competitor_time_stats_populated(self):
        """competitor_time_stats dict should have an entry for each competitor."""
        result = self._equal_field(3, 1_000)
        for comp in result["competitors"]:
            name = comp["name"]
            assert name in result["competitor_time_stats"], (
                f"{name} missing from competitor_time_stats"
            )
            stats = result["competitor_time_stats"][name]
            assert stats.mean > 0
            assert stats.std_dev >= 0
            assert stats.min_time <= stats.mean <= stats.max_time

    def test_reproducible_with_seed(self):
        """Two runs with the same seed must produce identical results."""
        competitors = [
            _comp("Alice", 3, 60.0),
            _comp("Bob", 10, 55.0),
            _comp("Carol", 18, 45.0),
        ]
        r1 = run_monte_carlo_simulation(competitors, num_simulations=1_000, seed=7, verbose=False)
        r2 = run_monte_carlo_simulation(competitors, num_simulations=1_000, seed=7, verbose=False)

        assert r1["winner_counts"] == r2["winner_counts"], (
            "Different winner counts with same seed -- not reproducible"
        )
