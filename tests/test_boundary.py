"""Boundary and stress tests.

Tests extreme values, edge-of-range inputs, and invariant preservation
under unusual conditions across all core modules.
"""

from datetime import date, timedelta

import pytest

from strathmark import CompetitorRecord, HandicapCalculator, HistoricalResult, WoodProfile
from strathmark.config import rules, sim_config
from strathmark.decay import calculate_performance_weight
from strathmark.variance import (
    calculate_consistency_rating,
    run_monte_carlo_simulation,
)
from strathmark.wood import (
    apply_quality_multiplier_statistical,
    calculate_scaling_factor,
    get_species_properties,
)


# ---------------------------------------------------------------------------
# Mark boundaries
# ---------------------------------------------------------------------------
class TestMarkBoundaries:
    """Test marks at the exact boundaries: floor=3, ceiling=183."""

    def test_gap_of_exactly_180_gives_ceiling(self):
        """180s gap → mark = 3 + 180 = 183 (ceiling)."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="Slow", history=[], manual_time_override=200.0)
        c2 = CompetitorRecord(name="Fast", history=[], manual_time_override=20.0)
        calc = HandicapCalculator()
        results = calc.calculate([c1, c2], wood, "SB")
        fast = next(r for r in results if r.name == "Fast")
        assert fast.mark == 183

    def test_gap_exceeding_180_still_capped(self):
        """Gap > 180 → mark still 183."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="Slow", history=[], manual_time_override=300.0)
        c2 = CompetitorRecord(name="Fast", history=[], manual_time_override=10.0)
        calc = HandicapCalculator()
        results = calc.calculate([c1, c2], wood, "SB")
        fast = next(r for r in results if r.name == "Fast")
        assert fast.mark == 183

    def test_gap_of_0_5_rounds_to_0(self):
        """0.5s gap → round(0.5) = 0 (banker's) → mark = 3."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="A", history=[], manual_time_override=25.5)
        c2 = CompetitorRecord(name="B", history=[], manual_time_override=25.0)
        calc = HandicapCalculator()
        results = calc.calculate([c1, c2], wood, "SB")
        b = next(r for r in results if r.name == "B")
        assert b.mark == 3  # round(0.5) = 0

    def test_gap_of_1_0_gives_mark_4(self):
        """Exactly 1.0s gap → mark = 3 + 1 = 4."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="A", history=[], manual_time_override=26.0)
        c2 = CompetitorRecord(name="B", history=[], manual_time_override=25.0)
        calc = HandicapCalculator()
        results = calc.calculate([c1, c2], wood, "SB")
        b = next(r for r in results if r.name == "B")
        assert b.mark == 4

    def test_custom_event_ceiling_below_system(self):
        """Custom ceiling of 50 should cap marks at 50."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="Slow", history=[], manual_time_override=200.0)
        c2 = CompetitorRecord(name="Fast", history=[], manual_time_override=10.0)
        calc = HandicapCalculator(event_ceiling=50)
        results = calc.calculate([c1, c2], wood, "SB")
        fast = next(r for r in results if r.name == "Fast")
        assert fast.mark == 50


# ---------------------------------------------------------------------------
# Large field stress test
# ---------------------------------------------------------------------------
class TestLargeFieldBoundary:
    """Test with 50 competitors to verify scaling."""

    def test_50_competitors_all_valid(self):
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        competitors = [
            CompetitorRecord(
                name=f"C{i:02d}",
                history=[],
                manual_time_override=10.0 + i * 2.0,
            )
            for i in range(50)
        ]
        calc = HandicapCalculator()
        results = calc.calculate(competitors, wood, "SB")

        assert len(results) == 50
        for r in results:
            assert rules.MIN_MARK_SECONDS <= r.mark <= rules.MAX_MARK_SECONDS

    def test_50_competitors_monotonic(self):
        """Marks should be monotonic: faster → higher mark."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        competitors = [
            CompetitorRecord(
                name=f"C{i:02d}",
                history=[],
                manual_time_override=10.0 + i * 2.0,
            )
            for i in range(50)
        ]
        calc = HandicapCalculator()
        results = calc.calculate(competitors, wood, "SB")

        # Sort by predicted_time ascending (fastest first)
        sorted_results = sorted(results, key=lambda r: r.predicted_time)
        marks = [r.mark for r in sorted_results]
        # Marks should be non-increasing (fastest has highest or equal mark)
        for i in range(len(marks) - 1):
            assert marks[i] >= marks[i + 1]


# ---------------------------------------------------------------------------
# Variance boundaries
# ---------------------------------------------------------------------------
class TestVarianceBoundaries:
    """Test std_dev clamping at [1.5, 15.0]."""

    def test_very_consistent_competitor_floored(self):
        """Competitor with tiny std_dev gets clamped to 1.5."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        # 10 identical results → std_dev ≈ 0
        history = [
            HistoricalResult(
                event_code="SB",
                time_seconds=25.0,
                species="S01",
                diameter_mm=300,
                quality=5,
                result_date=date.today() - timedelta(days=i),
            )
            for i in range(10)
        ]
        record = CompetitorRecord(name="Robot", history=history)
        calc = HandicapCalculator()
        results = calc.calculate([record], wood, "SB")
        assert results[0].std_dev >= sim_config.MIN_COMPETITOR_STD_SECONDS

    def test_very_inconsistent_competitor_capped(self):
        """Competitor with huge std_dev gets capped at 15.0."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        # Wildly varying times
        times = [10.0, 50.0, 15.0, 80.0, 20.0, 90.0, 12.0, 70.0]
        history = [
            HistoricalResult(
                event_code="SB",
                time_seconds=t,
                species="S01",
                diameter_mm=300,
                quality=5,
                result_date=date.today() - timedelta(days=i * 30),
            )
            for i, t in enumerate(times)
        ]
        record = CompetitorRecord(name="Wild", history=history)
        calc = HandicapCalculator()
        results = calc.calculate([record], wood, "SB")
        assert results[0].std_dev <= sim_config.MAX_COMPETITOR_STD_SECONDS

    def test_default_variance_scaling(self):
        """With <3 results, std_dev = prediction * 0.12 (clamped)."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        record = CompetitorRecord(
            name="Newbie",
            history=[],
            manual_time_override=50.0,
        )
        calc = HandicapCalculator()
        results = calc.calculate([record], wood, "SB")
        expected = 50.0 * sim_config.DEFAULT_VARIANCE_SCALING_FACTOR
        expected = max(
            sim_config.MIN_COMPETITOR_STD_SECONDS,
            min(expected, sim_config.MAX_COMPETITOR_STD_SECONDS),
        )
        assert results[0].std_dev == pytest.approx(expected, abs=0.5)


# ---------------------------------------------------------------------------
# Monte Carlo boundaries
# ---------------------------------------------------------------------------
class TestMonteCarloEdgeCases:
    def test_single_competitor_always_wins(self):
        """Single competitor wins 100% of simulations."""
        competitors = [
            {"name": "Solo", "predicted_time": 25.0, "mark": 3, "std_dev": 3.0},
        ]
        result = run_monte_carlo_simulation(
            competitors,
            num_simulations=1_000,
            seed=42,
            verbose=False,
        )
        assert result["winner_percentages"]["Solo"] == pytest.approx(100.0)

    def test_identical_competitors_roughly_equal(self):
        """Identical competitors should each win ~50%."""
        competitors = [
            {"name": "A", "predicted_time": 25.0, "mark": 3, "std_dev": 3.0},
            {"name": "B", "predicted_time": 25.0, "mark": 3, "std_dev": 3.0},
        ]
        result = run_monte_carlo_simulation(
            competitors,
            num_simulations=50_000,
            seed=42,
            verbose=False,
        )
        assert result["winner_percentages"]["A"] == pytest.approx(50.0, abs=3.0)

    def test_heavily_favored_wins_most(self):
        """Competitor with much lower finish time should win overwhelmingly."""
        competitors = [
            {"name": "Fast", "predicted_time": 10.0, "mark": 20, "std_dev": 1.0},
            {"name": "Slow", "predicted_time": 50.0, "mark": 3, "std_dev": 1.0},
        ]
        result = run_monte_carlo_simulation(
            competitors,
            num_simulations=10_000,
            seed=42,
            verbose=False,
        )
        assert result["winner_percentages"]["Fast"] > 90.0

    def test_time_floor_prevents_negative_times(self):
        """Actual times should never go below predicted_time * 0.5."""
        competitors = [
            {"name": "X", "predicted_time": 5.0, "mark": 3, "std_dev": 10.0},
        ]
        result = run_monte_carlo_simulation(
            competitors,
            num_simulations=10_000,
            seed=42,
            verbose=False,
        )
        stats = result["competitor_time_stats"]["X"]
        assert stats.min_time >= 0  # No negative times


# ---------------------------------------------------------------------------
# Wood property boundaries
# ---------------------------------------------------------------------------
class TestWoodBoundaries:
    def test_extreme_diameter_scaling(self):
        """Very large diameter should increase time significantly."""
        factor = calculate_scaling_factor(300, 500, exponent=2.0)
        assert factor > 2.0  # (500/300)^2 ≈ 2.78

    def test_very_small_diameter_scaling(self):
        """Very small diameter should decrease time."""
        factor = calculate_scaling_factor(300, 200, exponent=2.0)
        assert factor < 0.5  # (200/300)^2 ≈ 0.44

    def test_quality_1_extreme(self):
        """Quality 1 should reduce time significantly."""
        base = 25.0
        adjusted = apply_quality_multiplier_statistical(base, 1)
        assert adjusted < base

    def test_quality_10_extreme(self):
        """Quality 10 should increase time significantly."""
        base = 25.0
        adjusted = apply_quality_multiplier_statistical(base, 10)
        assert adjusted > base

    def test_quality_5_no_change(self):
        """Quality 5 is the baseline — no adjustment."""
        base = 25.0
        adjusted = apply_quality_multiplier_statistical(base, 5)
        assert adjusted == pytest.approx(base)

    def test_unknown_species_returns_defaults(self):
        """Unknown species should return safe defaults, not crash."""
        props = get_species_properties("UNKNOWN_SPECIES")
        assert props is not None


# ---------------------------------------------------------------------------
# Consistency rating boundaries
# ---------------------------------------------------------------------------
class TestConsistencyBoundaries:
    def test_exactly_at_very_high_threshold(self):
        result = calculate_consistency_rating(2.5)
        assert "Very High" in result or "very high" in result.lower()

    def test_just_above_very_high(self):
        result = calculate_consistency_rating(2.51)
        assert "High" in result and "Very" not in result

    def test_exactly_at_high_threshold(self):
        result = calculate_consistency_rating(3.0)
        assert "High" in result

    def test_exactly_at_moderate_threshold(self):
        result = calculate_consistency_rating(3.5)
        assert "Moderate" in result or "High" in result

    def test_above_moderate(self):
        result = calculate_consistency_rating(4.0)
        assert "Low" in result

    def test_zero_std_dev(self):
        """Zero std_dev should be Very High consistency."""
        result = calculate_consistency_rating(0.0)
        assert "Very High" in result


# ---------------------------------------------------------------------------
# Decay weight boundaries
# ---------------------------------------------------------------------------
class TestDecayBoundaries:
    def test_very_old_result_near_zero(self):
        """A 20-year-old result should have near-zero weight."""
        ref = date(2025, 1, 1)
        old = ref - timedelta(days=7300)  # 20 years
        w = calculate_performance_weight(old, ref)
        assert w < 0.005

    def test_yesterday_near_1(self):
        """Yesterday's result should be very close to 1.0."""
        ref = date(2025, 1, 1)
        yesterday = ref - timedelta(days=1)
        w = calculate_performance_weight(yesterday, ref)
        assert w > 0.999

    def test_weight_never_negative(self):
        """No matter how old, weight should never be negative."""
        ref = date(2025, 1, 1)
        ancient = ref - timedelta(days=36500)  # 100 years
        w = calculate_performance_weight(ancient, ref)
        assert w >= 0
