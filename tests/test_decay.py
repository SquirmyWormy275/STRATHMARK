"""Tests for strathmark/decay.py — time-decay weighting."""

from datetime import date, timedelta

import pytest

from strathmark.decay import (
    calculate_performance_weight,
    classify_activity_level,
    compute_weighted_average,
    compute_weights_for_results,
)
from strathmark.config import decay_config


class TestCalculatePerformanceWeight:

    def test_today_returns_1(self):
        today = date.today()
        w = calculate_performance_weight(today, today, half_life_days=730)
        assert w == pytest.approx(1.0)

    def test_one_half_life_returns_half(self):
        today = date.today()
        old = today - timedelta(days=730)
        w = calculate_performance_weight(old, today, half_life_days=730)
        assert w == pytest.approx(0.5, abs=0.01)

    def test_two_half_lives_returns_quarter(self):
        today = date.today()
        old = today - timedelta(days=1460)
        w = calculate_performance_weight(old, today, half_life_days=730)
        assert w == pytest.approx(0.25, abs=0.01)

    def test_none_date_returns_full_weight(self):
        w = calculate_performance_weight(None)
        assert w == 1.0

    def test_future_date_returns_full_weight(self):
        future = date.today() + timedelta(days=30)
        w = calculate_performance_weight(future)
        assert w == 1.0


class TestClassifyActivityLevel:

    def test_active_5_plus_results(self):
        today = date.today()
        dates = [today - timedelta(days=i * 60) for i in range(6)]
        assert classify_activity_level(dates, today) == "active"

    def test_moderate_2_to_4_results(self):
        today = date.today()
        dates = [today - timedelta(days=i * 120) for i in range(3)]
        assert classify_activity_level(dates, today) == "moderate"

    def test_inactive_fewer_than_2(self):
        today = date.today()
        dates = [today - timedelta(days=800)]
        assert classify_activity_level(dates, today) == "inactive"

    def test_none_dates_skipped(self):
        today = date.today()
        dates = [None, None, today - timedelta(days=100)]
        assert classify_activity_level(dates, today) == "inactive"


class TestComputeWeightedAverage:

    def test_equal_weights(self):
        avg = compute_weighted_average([10.0, 20.0, 30.0], [1.0, 1.0, 1.0])
        assert avg == pytest.approx(20.0, abs=0.1)

    def test_heavy_first_weight(self):
        avg = compute_weighted_average([10.0, 20.0], [0.99, 0.01])
        assert avg == pytest.approx(10.0, abs=0.5)

    def test_empty_raises(self):
        with pytest.raises((ValueError, ZeroDivisionError)):
            compute_weighted_average([], [])


class TestComputeWeightsForResults:

    def test_recent_dates_higher_weight(self):
        today = date.today()
        dates = [today - timedelta(days=730), today]
        weights = compute_weights_for_results(dates, today)
        assert weights[1] > weights[0]

    def test_adaptive_uses_activity_level(self):
        today = date.today()
        dates = [today - timedelta(days=i * 30) for i in range(6)]
        weights = compute_weights_for_results(dates, today, adaptive=True)
        assert len(weights) == 6
        assert all(0 < w <= 1.0 for w in weights)
