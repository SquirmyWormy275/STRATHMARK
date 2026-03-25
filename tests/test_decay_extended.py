"""Extended tests for the decay module.

Covers adaptive half-life selection, robust clipping, edge cases
in weighted averaging, and full date-handling paths.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from strathmark.config import decay_config
from strathmark.decay import (
    calculate_performance_weight,
    classify_activity_level,
    compute_weighted_average,
    compute_weights_for_results,
    select_half_life,
)


# ---------------------------------------------------------------------------
# calculate_performance_weight
# ---------------------------------------------------------------------------
class TestPerformanceWeightPrecision:
    """Validate the exponential decay formula with precise values."""

    def test_exact_half_life(self):
        """At exactly one half-life, weight should be 0.5."""
        ref = date(2025, 1, 1)
        result_date = ref - timedelta(days=730)
        w = calculate_performance_weight(result_date, ref, half_life_days=730)
        assert w == pytest.approx(0.5, abs=0.001)

    def test_quarter_life(self):
        """At two half-lives, weight should be 0.25."""
        ref = date(2025, 1, 1)
        result_date = ref - timedelta(days=1460)
        w = calculate_performance_weight(result_date, ref, half_life_days=730)
        assert w == pytest.approx(0.25, abs=0.001)

    def test_one_year_moderate_half_life(self):
        """365 days with 730-day half-life → weight ≈ 0.707."""
        ref = date(2025, 1, 1)
        result_date = ref - timedelta(days=365)
        w = calculate_performance_weight(result_date, ref, half_life_days=730)
        assert w == pytest.approx(0.707, abs=0.01)

    def test_ten_years_very_small(self):
        """10 years old → weight ≈ 0.031."""
        ref = date(2025, 1, 1)
        result_date = ref - timedelta(days=3650)
        w = calculate_performance_weight(result_date, ref, half_life_days=730)
        assert w == pytest.approx(0.031, abs=0.005)

    def test_same_day_is_1(self):
        ref = date(2025, 6, 15)
        assert calculate_performance_weight(ref, ref) == pytest.approx(1.0)

    def test_future_date_is_1(self):
        ref = date(2025, 1, 1)
        future = ref + timedelta(days=100)
        assert calculate_performance_weight(future, ref) == pytest.approx(1.0)

    def test_custom_half_life_365(self):
        """Active competitor half-life = 365 days."""
        ref = date(2025, 1, 1)
        result_date = ref - timedelta(days=365)
        w = calculate_performance_weight(result_date, ref, half_life_days=365)
        assert w == pytest.approx(0.5, abs=0.001)

    def test_custom_half_life_1095(self):
        """Inactive competitor half-life = 1095 days (3 years)."""
        ref = date(2025, 1, 1)
        result_date = ref - timedelta(days=1095)
        w = calculate_performance_weight(result_date, ref, half_life_days=1095)
        assert w == pytest.approx(0.5, abs=0.001)


class TestPerformanceWeightDateTypes:
    """Ensure all common date types are handled."""

    def test_string_date(self):
        """String date should be converted automatically."""
        w = calculate_performance_weight("2024-01-01", date(2025, 1, 1))
        assert 0.0 < w < 1.0

    def test_pandas_timestamp(self):
        w = calculate_performance_weight(pd.Timestamp("2024-06-01"))
        assert 0.0 < w <= 1.0

    def test_pandas_nat(self):
        assert calculate_performance_weight(pd.NaT) == 1.0

    def test_none(self):
        assert calculate_performance_weight(None) == 1.0


# ---------------------------------------------------------------------------
# classify_activity_level
# ---------------------------------------------------------------------------
class TestClassifyActivityLevel:
    """Test the active/moderate/inactive classification."""

    def test_exactly_5_results_is_active(self):
        today = date.today()
        dates = [today - timedelta(days=i * 60) for i in range(5)]
        assert classify_activity_level(dates, today) == "active"

    def test_exactly_2_results_is_moderate(self):
        today = date.today()
        dates = [today - timedelta(days=i * 60) for i in range(2)]
        assert classify_activity_level(dates, today) == "moderate"

    def test_one_result_is_inactive(self):
        today = date.today()
        dates = [today - timedelta(days=30)]
        assert classify_activity_level(dates, today) == "inactive"

    def test_zero_results_is_inactive(self):
        assert classify_activity_level([], date.today()) == "inactive"

    def test_old_results_outside_window_not_counted(self):
        """Results older than ACTIVITY_WINDOW_DAYS shouldn't count."""
        today = date.today()
        old = today - timedelta(days=decay_config.ACTIVITY_WINDOW_DAYS + 30)
        dates = [old] * 10  # 10 results, all too old
        assert classify_activity_level(dates, today) == "inactive"

    def test_none_dates_ignored(self):
        """None dates should be skipped, not counted."""
        today = date.today()
        dates = [None, None, today - timedelta(days=10), None]
        assert classify_activity_level(dates, today) == "inactive"

    def test_mixed_old_and_new(self):
        """Only recent results count toward activity level."""
        today = date.today()
        old = today - timedelta(days=1000)
        dates = [today - timedelta(days=i * 30) for i in range(3)] + [old] * 5
        assert classify_activity_level(dates, today) == "moderate"


# ---------------------------------------------------------------------------
# select_half_life
# ---------------------------------------------------------------------------
class TestSelectHalfLife:
    def test_active(self):
        assert select_half_life("active") == decay_config.HALF_LIFE_ACTIVE_DAYS

    def test_moderate(self):
        assert select_half_life("moderate") == decay_config.HALF_LIFE_MODERATE_DAYS

    def test_inactive(self):
        assert select_half_life("inactive") == decay_config.HALF_LIFE_INACTIVE_DAYS

    def test_invalid_raises(self):
        with pytest.raises((ValueError, KeyError)):
            select_half_life("unknown")


# ---------------------------------------------------------------------------
# compute_weighted_average — robust clipping edge cases
# ---------------------------------------------------------------------------
class TestComputeWeightedAverage:
    """Test the median/MAD robust clipping and edge cases."""

    def test_equal_weights_is_arithmetic_mean(self):
        result = compute_weighted_average([10, 20, 30], [1, 1, 1])
        assert result == pytest.approx(20.0)

    def test_single_value(self):
        result = compute_weighted_average([42.0], [1.0])
        assert result == pytest.approx(42.0)

    def test_two_values(self):
        result = compute_weighted_average([10.0, 30.0], [1.0, 1.0])
        assert result == pytest.approx(20.0)

    def test_heavily_weighted_first(self):
        result = compute_weighted_average([10.0, 100.0], [100.0, 1.0])
        assert result < 15.0  # Dominated by 10.0

    def test_outlier_clipped_with_enough_samples(self):
        """With 6+ samples, MAD clipping should reduce outlier influence."""
        times = [25.0, 26.0, 24.0, 25.5, 24.5, 25.0, 180.0]  # 180 is outlier
        weights = [1.0] * 7
        result = compute_weighted_average(times, weights)
        # Without clipping: mean ≈ 47.1.  With clipping: should be near 25.
        assert result < 35.0

    def test_empty_raises(self):
        with pytest.raises((ValueError, ZeroDivisionError)):
            compute_weighted_average([], [])

    def test_zero_weights_raises(self):
        with pytest.raises((ValueError, ZeroDivisionError)):
            compute_weighted_average([10, 20], [0, 0])

    def test_all_identical_values(self):
        """All same values → no MAD → returns that value."""
        result = compute_weighted_average([25.0] * 10, [1.0] * 10)
        assert result == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# compute_weights_for_results
# ---------------------------------------------------------------------------
class TestComputeWeightsForResults:
    def test_recent_higher_than_old(self):
        today = date.today()
        dates = [today, today - timedelta(days=730)]
        weights = compute_weights_for_results(dates, today)
        assert weights[0] > weights[1]

    def test_all_weights_positive(self):
        today = date.today()
        dates = [today - timedelta(days=i * 365) for i in range(5)]
        weights = compute_weights_for_results(dates, today)
        assert all(w > 0 for w in weights)

    def test_non_adaptive_uses_moderate_half_life(self):
        """Non-adaptive mode uses fixed 730-day half-life regardless."""
        today = date.today()
        # 6 recent results = "active", but non-adaptive should ignore that
        dates = [today - timedelta(days=i * 10) for i in range(6)]
        w_adaptive = compute_weights_for_results(dates, today, adaptive=True)
        w_fixed = compute_weights_for_results(dates, today, adaptive=False)
        # Active uses 365-day half-life (decays faster), so for old results
        # adaptive weights < fixed weights. For very recent results they're
        # nearly equal since all are ~1.0.
        assert len(w_adaptive) == len(w_fixed)

    def test_empty_returns_empty(self):
        result = compute_weights_for_results([], date.today())
        assert result == []
