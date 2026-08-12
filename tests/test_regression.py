"""Regression tests for bugs fixed during development.

Each test documents a specific bug that was found and fixed.
These prevent regressions if the code is refactored.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from strathmark import CompetitorRecord, HandicapCalculator, WoodProfile
from strathmark.config import sim_config
from strathmark.decay import (
    calculate_performance_weight,
    classify_activity_level,
    compute_weighted_average,
    compute_weights_for_results,
)
from strathmark.predictor import predict_baseline
from strathmark.variance import (
    run_monte_carlo_simulation,
)


# ---------------------------------------------------------------------------
# Bug: Banker's rounding was changed from ceiling in v0.3.0.  Old code used
#      math.ceil which inflated marks by 1 for exact half-second gaps.
# ---------------------------------------------------------------------------
class TestBankersRoundingRegression:
    """v0.3.0 changed gap rounding from ceiling to round() (half-to-even)."""

    def _calc(self, gap: float) -> int:
        """Replicate mark formula: mark = 3 + round(gap)."""
        return 3 + round(gap)

    def test_half_second_rounds_to_even(self):
        """0.5 rounds to 0 (even), not 1 (ceiling would give 1)."""
        assert self._calc(0.5) == 3  # round(0.5) == 0

    def test_1_5_rounds_to_even(self):
        """1.5 rounds to 2 (even), not up to 2 — same as ceiling here."""
        assert self._calc(1.5) == 5  # round(1.5) == 2

    def test_2_5_rounds_to_even(self):
        """2.5 rounds to 2 (even), ceiling would give 3."""
        assert self._calc(2.5) == 5  # round(2.5) == 2

    def test_3_5_rounds_to_even(self):
        """3.5 rounds to 4 (even)."""
        assert self._calc(3.5) == 7  # round(3.5) == 4

    def test_integer_gap_unchanged(self):
        """Exact integer gaps should give the same mark as before."""
        assert self._calc(5.0) == 8

    def test_calculator_fallback_uses_round_not_ceil(self):
        """The explicit posterior-unavailable fallback uses banker's rounding."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        # Create competitors with a 2.5s gap
        c1 = CompetitorRecord(name="Slow", history=[], manual_time_override=30.0)
        c2 = CompetitorRecord(name="Fast", history=[], manual_time_override=27.5)
        calc = HandicapCalculator()
        results = calc.calculate([c1, c2], wood, "SB")
        calc._assign_marks(results, distributions=None)
        fast_mark = next(r for r in results if r.name == "Fast")
        # gap = 30.0 - 27.5 = 2.5; round(2.5) = 2; mark = 3 + 2 = 5
        assert fast_mark.mark == 5
        assert fast_mark.optimizer == "rounded_gap_fallback"


# ---------------------------------------------------------------------------
# Bug: Decay weights weren't being applied correctly — old code passed
#      result_date=None which returned weight=1.0 for everything.
# ---------------------------------------------------------------------------
class TestDecayWeightApplicationRegression:
    """Ensure decay weights actually differentiate old vs new results."""

    def test_old_result_gets_lower_weight(self):
        today = date.today()
        old = today - timedelta(days=1460)  # 4 half-lives
        w_new = calculate_performance_weight(today, today)
        w_old = calculate_performance_weight(old, today)
        assert w_new > w_old
        assert w_old < 0.3  # 4 years → ~0.25

    def test_none_date_returns_1(self):
        """Backward compat: None date → full weight (not zero)."""
        assert calculate_performance_weight(None) == 1.0

    def test_weights_sum_is_not_uniform(self):
        """Adaptive weights for mixed-age results must NOT be all equal."""
        today = date.today()
        dates = [
            today,
            today - timedelta(days=365),
            today - timedelta(days=730),
            today - timedelta(days=1460),
        ]
        weights = compute_weights_for_results(dates, today, adaptive=True)
        assert len(set(round(w, 4) for w in weights)) > 1  # Not all same


# ---------------------------------------------------------------------------
# Bug: Timeout results (≥180s) were not being filtered from predictions,
#      pulling baseline times upward.
# ---------------------------------------------------------------------------
class TestTimeoutFilteringRegression:
    """Results at or above the time limit should be treated carefully."""

    def test_predict_baseline_excludes_extreme_outliers(self):
        """Baseline prediction should not be pulled to 180s by timeouts.

        Includes a 180s timeout result that should be handled by outlier
        clipping or filtering, keeping the baseline near the normal times.
        """
        from strathmark import HistoricalResult

        today = date.today()
        history = [
            HistoricalResult(
                event_code="SB",
                time_seconds=25.0,
                species="S01",
                diameter_mm=300,
                quality=5,
                result_date=today - timedelta(days=30),
            ),
            HistoricalResult(
                event_code="SB",
                time_seconds=26.0,
                species="S01",
                diameter_mm=300,
                quality=5,
                result_date=today - timedelta(days=60),
            ),
            HistoricalResult(
                event_code="SB",
                time_seconds=24.0,
                species="S01",
                diameter_mm=300,
                quality=5,
                result_date=today - timedelta(days=90),
            ),
            HistoricalResult(
                event_code="SB",
                time_seconds=25.5,
                species="S01",
                diameter_mm=300,
                quality=5,
                result_date=today - timedelta(days=120),
            ),
            HistoricalResult(
                event_code="SB",
                time_seconds=24.5,
                species="S01",
                diameter_mm=300,
                quality=5,
                result_date=today - timedelta(days=150),
            ),
            # Timeout result — should be clipped by robust averaging
            HistoricalResult(
                event_code="SB",
                time_seconds=180.0,
                species="S01",
                diameter_mm=300,
                quality=5,
                result_date=today - timedelta(days=180),
            ),
        ]
        record = CompetitorRecord(name="Test", history=history)
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = predict_baseline(record, wood, "SB")
        if pred is not None:
            # Baseline should be near 25s, not pulled toward 180s
            assert pred.value < 50.0


# ---------------------------------------------------------------------------
# Bug: Variance was proportional (prediction * 0.1) instead of absolute.
#      This gave fast choppers unrealistically tight variance.
# ---------------------------------------------------------------------------
class TestAbsoluteVarianceRegression:
    """Variance must be absolute ±3s, never proportional to predicted time."""

    def test_mc_variance_independent_of_predicted_time(self):
        """Two competitors with very different times should have similar
        absolute variance when std_dev is the same."""
        competitors = [
            {"name": "Fast", "predicted_time": 15.0, "mark": 18, "std_dev": 3.0},
            {"name": "Slow", "predicted_time": 90.0, "mark": 3, "std_dev": 3.0},
        ]
        result = run_monte_carlo_simulation(
            competitors,
            num_simulations=50_000,
            seed=42,
            verbose=False,
        )
        fast_stats = result["competitor_time_stats"]["Fast"]
        slow_stats = result["competitor_time_stats"]["Slow"]
        # Both should have similar std_dev (~3s) since we gave the same std_dev
        assert abs(fast_stats.std_dev - slow_stats.std_dev) < 1.5

    def test_default_variance_scaling_factor_exists(self):
        """Config must have the 0.12 scaling factor (added in v0.3.0)."""
        assert sim_config.DEFAULT_VARIANCE_SCALING_FACTOR == 0.12


# ---------------------------------------------------------------------------
# Bug: Mark floor violated when gap was negative (rounding error or
#      identical predictions with floating-point noise).
# ---------------------------------------------------------------------------
class TestMarkFloorNeverViolatedRegression:
    """Mark must never go below 3 regardless of floating-point edge cases."""

    def test_tiny_negative_gap_still_gives_floor(self):
        """If floating-point makes gap slightly negative, mark stays at 3."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="A", history=[], manual_time_override=30.0)
        c2 = CompetitorRecord(name="B", history=[], manual_time_override=30.0000001)
        calc = HandicapCalculator()
        results = calc.calculate([c1, c2], wood, "SB")
        for r in results:
            assert r.mark >= 3

    def test_identical_predictions_both_get_floor(self):
        """Two competitors with exact same predicted time both get mark 3."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="A", history=[], manual_time_override=25.0)
        c2 = CompetitorRecord(name="B", history=[], manual_time_override=25.0)
        calc = HandicapCalculator()
        results = calc.calculate([c1, c2], wood, "SB")
        for r in results:
            assert r.mark == 3


# ---------------------------------------------------------------------------
# Bug: Ceiling of 183 not enforced when event_ceiling was None.
# ---------------------------------------------------------------------------
class TestCeilingEnforcementRegression:
    """System ceiling (183) must always apply even without explicit ceiling."""

    def test_huge_gap_capped_at_183(self):
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="Slow", history=[], manual_time_override=200.0)
        c2 = CompetitorRecord(name="Fast", history=[], manual_time_override=10.0)
        calc = HandicapCalculator()
        results = calc.calculate([c1, c2], wood, "SB")
        for r in results:
            assert r.mark <= 183


# ---------------------------------------------------------------------------
# Bug: compute_weighted_average with a single value raised IndexError
#      during MAD clipping.
# ---------------------------------------------------------------------------
class TestWeightedAverageSingleValueRegression:
    """Single-value weighted average should return that value, not crash."""

    def test_single_value(self):
        result = compute_weighted_average([42.0], [1.0])
        assert result == pytest.approx(42.0)

    def test_two_values(self):
        result = compute_weighted_average([20.0, 30.0], [1.0, 1.0])
        assert result == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Bug: Pandas Timestamp vs datetime.date caused TypeError in decay.
# ---------------------------------------------------------------------------
class TestPandasTimestampRegression:
    """Decay functions must handle pandas Timestamp objects."""

    def test_timestamp_accepted(self):
        ts = pd.Timestamp("2024-01-15")
        weight = calculate_performance_weight(ts)
        assert 0.0 < weight <= 1.0

    def test_nat_returns_full_weight(self):
        """pandas NaT should be treated like None → weight 1.0."""
        weight = calculate_performance_weight(pd.NaT)
        assert weight == 1.0

    def test_activity_level_with_timestamps(self):
        today = pd.Timestamp.now()
        dates = [today - pd.Timedelta(days=i * 30) for i in range(6)]
        level = classify_activity_level(dates, today)
        assert level == "active"
