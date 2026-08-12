"""Tests for strathmark/analytics.py — backtesting and profiling."""

from datetime import date, timedelta

from strathmark.analytics import (
    profile_competitor,
    rolling_origin_analysis,
    summarise_performance_history,
)
from strathmark.predictor import CompetitorRecord, HistoricalResult


def _make_record(name, n=5, base_time=50.0, event_code="SB"):
    today = date.today()
    history = [
        HistoricalResult(
            event_code=event_code,
            time_seconds=base_time + i,
            species="poplar",
            diameter_mm=300,
            quality=5,
            result_date=today - timedelta(days=(n - i) * 30),
        )
        for i in range(n)
    ]
    return CompetitorRecord(name=name, history=history)


class TestProfileCompetitor:
    def test_basic_profile(self):
        record = _make_record("Alice", n=5, base_time=45.0)
        profile = profile_competitor(record)
        assert profile["name"] == "Alice"
        assert profile["total_results"] == 5
        assert profile["mean_time"] is not None
        assert profile["best_time"] < profile["worst_time"]

    def test_no_history(self):
        record = CompetitorRecord(name="Newbie", history=[])
        profile = profile_competitor(record)
        assert profile["name"] == "Newbie"
        assert profile["total_results"] == 0

    def test_event_filter(self):
        record = _make_record("Alice", n=5, event_code="SB")
        profile = profile_competitor(record, event_code="UH")
        # No UH results
        assert profile["total_results"] == 0


class TestSummarisePerformanceHistory:
    def test_sorted_by_mean_time(self):
        records = [
            _make_record("Fast", base_time=40.0),
            _make_record("Slow", base_time=60.0),
            _make_record("Medium", base_time=50.0),
        ]
        summaries = summarise_performance_history(records)
        times = [s["mean_time"] for s in summaries if s["mean_time"] is not None]
        assert times == sorted(times)

    def test_empty_list(self):
        summaries = summarise_performance_history([])
        assert summaries == []


def test_rolling_origin_analysis_labels_small_cohorts():
    from tests.test_prediction_v2 import _training_frame

    evidence = _training_frame()
    report = rolling_origin_analysis(
        evidence,
        min_training_rows=10,
        minimum_global_rows=1_000,
        minimum_cohort_rows=1_000,
    )

    assert report["metrics"]["sample_label"] == "insufficient_global_sample"
    assert report["metrics"]["claim_eligible"] is False
    assert all(not cohort["claim_eligible"] for cohort in report["cohorts"].values())
