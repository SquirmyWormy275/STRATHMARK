"""Extended tests for strathmark/analytics.py — backtest_predictions and edge cases."""

from datetime import date, timedelta

import numpy as np
import pytest

from strathmark.analytics import (
    backtest_predictions,
    profile_competitor,
    summarise_performance_history,
)
from strathmark.predictor import CompetitorRecord, HistoricalResult, WoodProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(name, n=5, base_time=50.0, event_code="SB", days_apart=30):
    today = date.today()
    history = [
        HistoricalResult(
            event_code=event_code,
            time_seconds=base_time + i * 0.5,
            species="S01",
            diameter_mm=300,
            quality=5,
            result_date=today - timedelta(days=(n - i) * days_apart),
        )
        for i in range(n)
    ]
    return CompetitorRecord(name=name, history=history)


PINE_300 = WoodProfile(species="S01", diameter_mm=300, quality=5)


# ---------------------------------------------------------------------------
# backtest_predictions
# ---------------------------------------------------------------------------

class TestBacktestPredictions:

    def test_basic_backtest(self):
        """Backtest with matching actuals should produce valid metrics."""
        comps = [
            _make_record("Alice", n=5, base_time=30.0),
            _make_record("Bob", n=5, base_time=40.0),
        ]
        actuals = {"Alice": 31.0, "Bob": 42.0}
        result = backtest_predictions(comps, PINE_300, "SB", actuals)

        assert result['mae'] is not None
        assert result['rmse'] is not None
        assert result['bias'] is not None
        assert result['within_3s_pct'] is not None
        assert len(result['results']) == 2

    def test_no_matching_actuals_returns_none_metrics(self):
        """If no competitors match actuals, metrics should be None."""
        comps = [_make_record("Alice", n=5)]
        actuals = {"Bob": 50.0}  # Alice not in actuals
        result = backtest_predictions(comps, PINE_300, "SB", actuals)

        assert result['mae'] is None
        assert result['rmse'] is None
        assert len(result['results']) == 0

    def test_perfect_predictions(self):
        """If predicted == actual, MAE and RMSE should be very small."""
        comp = _make_record("Alice", n=5, base_time=30.0)
        # Get the predicted time first
        from strathmark.predictor import get_best_prediction
        pred = get_best_prediction(comp, PINE_300, "SB")
        if pred is None:
            pytest.skip("No prediction available")

        actuals = {"Alice": pred.value}
        result = backtest_predictions([comp], PINE_300, "SB", actuals)

        assert result['mae'] is not None
        assert result['mae'] < 0.1
        assert result['rmse'] < 0.1
        assert result['within_3s_pct'] == 100.0

    def test_bias_positive_when_over_predicting(self):
        """Bias should be positive when predictions are higher than actuals."""
        comp = _make_record("Alice", n=5, base_time=50.0)
        # Actual time much lower than predicted (~50s)
        actuals = {"Alice": 20.0}
        result = backtest_predictions([comp], PINE_300, "SB", actuals)

        if result['bias'] is not None:
            assert result['bias'] > 0  # predicted > actual = positive bias

    def test_within_3s_pct_calculation(self):
        """Check within_3s_pct is correctly calculated."""
        comps = [
            _make_record("Close", n=5, base_time=30.0),
            _make_record("Far", n=5, base_time=80.0),
        ]
        from strathmark.predictor import get_best_prediction
        pred_close = get_best_prediction(comps[0], PINE_300, "SB")
        pred_far = get_best_prediction(comps[1], PINE_300, "SB")

        if pred_close is None or pred_far is None:
            pytest.skip("Predictions unavailable")

        actuals = {
            "Close": pred_close.value,       # 0 error
            "Far": pred_far.value + 10.0,    # 10s error
        }
        result = backtest_predictions(comps, PINE_300, "SB", actuals)

        assert result['within_3s_pct'] is not None
        assert result['within_3s_pct'] == 50.0  # 1 of 2 within 3s

    def test_empty_competitors_list(self):
        """Empty competitor list should return empty results."""
        result = backtest_predictions([], PINE_300, "SB", {"Alice": 30.0})
        assert result['mae'] is None
        assert len(result['results']) == 0

    def test_per_competitor_error_fields(self):
        """Each result entry should have predicted, actual, error, abs_error."""
        comp = _make_record("Alice", n=5, base_time=30.0)
        actuals = {"Alice": 35.0}
        result = backtest_predictions([comp], PINE_300, "SB", actuals)

        if result['results']:
            entry = result['results'][0]
            assert 'predicted' in entry
            assert 'actual' in entry
            assert 'error' in entry
            assert 'abs_error' in entry
            assert entry['actual'] == 35.0
            assert entry['abs_error'] == abs(entry['error'])


# ---------------------------------------------------------------------------
# profile_competitor extended
# ---------------------------------------------------------------------------

class TestProfileCompetitorExtended:

    def test_activity_level_active(self):
        """5+ results in last 2 years should be 'active'."""
        record = _make_record("Active", n=10, base_time=30.0, days_apart=30)
        profile = profile_competitor(record)
        assert profile['activity_level'] == 'active'

    def test_activity_level_moderate(self):
        """2-4 results in last 2 years should be 'moderate'."""
        today = date.today()
        history = [
            HistoricalResult("SB", 30.0, "S01", 300, 5,
                             today - timedelta(days=100)),
            HistoricalResult("SB", 31.0, "S01", 300, 5,
                             today - timedelta(days=200)),
        ]
        record = CompetitorRecord(name="Moderate", history=history)
        profile = profile_competitor(record)
        assert profile['activity_level'] == 'moderate'

    def test_activity_level_inactive(self):
        """0-1 results in last 2 years should be 'inactive'."""
        old_date = date(2020, 1, 1)
        history = [
            HistoricalResult("SB", 30.0, "S01", 300, 5, old_date),
            HistoricalResult("SB", 31.0, "S01", 300, 5, old_date),
        ]
        record = CompetitorRecord(name="Inactive", history=history)
        profile = profile_competitor(record)
        assert profile['activity_level'] == 'inactive'

    def test_std_dev_with_single_result(self):
        """Single result should have None std_dev."""
        history = [HistoricalResult("SB", 30.0, "S01", 300, 5, date.today())]
        record = CompetitorRecord(name="OneShot", history=history)
        profile = profile_competitor(record)
        assert profile['std_dev'] is None

    def test_events_contested(self):
        """Should list all distinct event codes."""
        history = [
            HistoricalResult("SB", 30.0, "S01", 300, 5, date.today()),
            HistoricalResult("UH", 40.0, "S01", 300, 5, date.today()),
        ]
        record = CompetitorRecord(name="Multi", history=history)
        profile = profile_competitor(record)
        assert set(profile['events_contested']) == {"SB", "UH"}

    def test_division_passed_through(self):
        record = CompetitorRecord(
            name="Alice", history=[], division="Open"
        )
        profile = profile_competitor(record)
        assert profile['division'] == 'Open'


# ---------------------------------------------------------------------------
# summarise_performance_history extended
# ---------------------------------------------------------------------------

class TestSummarisePerformanceHistoryExtended:

    def test_no_history_competitors_at_end(self):
        """Competitors without data should be sorted to the end."""
        records = [
            CompetitorRecord(name="NoData", history=[]),
            _make_record("HasData", n=3, base_time=30.0),
        ]
        summaries = summarise_performance_history(records)
        assert summaries[0]['name'] == 'HasData'
        assert summaries[1]['name'] == 'NoData'

    def test_event_filter(self):
        """Filtering by event should only include matching results."""
        record = _make_record("Alice", n=5, event_code="SB")
        summaries = summarise_performance_history([record], event_code="UH")
        assert summaries[0]['total_results'] == 0

    def test_multiple_competitors_ranked(self):
        """Faster competitors should rank higher."""
        records = [
            _make_record("Slow", n=5, base_time=60.0),
            _make_record("Fast", n=5, base_time=20.0),
            _make_record("Medium", n=5, base_time=40.0),
        ]
        summaries = summarise_performance_history(records)
        names = [s['name'] for s in summaries if s['mean_time'] is not None]
        assert names[0] == 'Fast'
        assert names[-1] == 'Slow'
