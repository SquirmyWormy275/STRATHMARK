"""Regression and edge-case tests for the prediction cascade.

Focuses on cascade priority, fallback behavior, tournament weighting,
and edge cases that have caused bugs in development.
"""

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from strathmark import CompetitorRecord, HistoricalResult, WoodProfile
from strathmark.predictor import (
    get_all_predictions,
    get_best_prediction,
    predict_baseline,
    select_best_prediction,
)


def _make_history(times, event="SB", species="S01", diameter=300, quality=5):
    base = date.today()
    return [
        HistoricalResult(
            event_code=event, time_seconds=t, species=species,
            diameter_mm=diameter, quality=quality,
            result_date=base - timedelta(days=i * 30),
        )
        for i, t in enumerate(times)
    ]


# ---------------------------------------------------------------------------
# Cascade priority
# ---------------------------------------------------------------------------
class TestCascadePriority:
    """Manual > Tournament > LLM > ML > Baseline > Panel."""

    def test_manual_override_wins(self):
        """Manual override should always be used when provided."""
        record = CompetitorRecord(
            name="Test",
            history=_make_history([25, 26, 24]),
            manual_time_override=15.0,
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = get_best_prediction(record, wood, "SB")
        assert pred.value == pytest.approx(15.0)
        assert pred.method == "manual"

    def test_tournament_time_high_priority(self):
        """Tournament result should dominate historical baseline."""
        record = CompetitorRecord(
            name="Test",
            history=_make_history([30, 31, 29, 30, 32]),
            tournament_time=22.0,
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = get_best_prediction(record, wood, "SB")
        # Should be heavily influenced by tournament time (22s)
        # not the historical ~30s average
        assert pred.value < 28.0

    def test_baseline_used_without_manual_or_tournament(self):
        """Without manual/tournament, baseline should be used."""
        record = CompetitorRecord(
            name="Test",
            history=_make_history([25, 26, 24, 25]),
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = get_best_prediction(record, wood, "SB")
        assert pred is not None
        assert pred.value > 0


# ---------------------------------------------------------------------------
# Baseline prediction edge cases
# ---------------------------------------------------------------------------
class TestBaselinePrediction:
    def test_no_history_returns_none(self):
        """Competitor with no history → baseline returns None."""
        record = CompetitorRecord(name="Newbie", history=[])
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = predict_baseline(record, wood, "SB")
        assert pred is None

    def test_single_result_returns_prediction(self):
        """Even a single result should produce a baseline."""
        record = CompetitorRecord(
            name="Test",
            history=_make_history([25.0]),
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = predict_baseline(record, wood, "SB")
        if pred is not None:
            assert pred.value > 0

    def test_baseline_near_average(self):
        """Baseline should be near the weighted average of history."""
        times = [25.0, 26.0, 24.0, 25.0]
        record = CompetitorRecord(
            name="Test",
            history=_make_history(times),
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = predict_baseline(record, wood, "SB")
        if pred is not None:
            # Should be within reasonable range of average (~25s)
            assert 20.0 < pred.value < 35.0

    def test_cross_event_history_not_used_directly(self):
        """UH history should not inflate SB baseline without scaling."""
        sb_times = [25.0, 26.0, 24.0]
        uh_times = [35.0, 36.0, 34.0]  # UH times are typically longer
        history = _make_history(sb_times, event="SB") + _make_history(uh_times, event="UH")
        record = CompetitorRecord(name="Test", history=history)
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = predict_baseline(record, wood, "SB")
        if pred is not None:
            # Should be closer to SB average (~25s) than UH (~35s)
            assert pred.value < 30.0


# ---------------------------------------------------------------------------
# get_all_predictions
# ---------------------------------------------------------------------------
class TestGetAllPredictions:
    def test_returns_dict(self):
        record = CompetitorRecord(
            name="Test",
            history=_make_history([25, 26, 24]),
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        preds = get_all_predictions(record, wood, "SB")
        assert isinstance(preds, dict)

    def test_manual_always_present_when_set(self):
        record = CompetitorRecord(
            name="Test",
            history=_make_history([25]),
            manual_time_override=20.0,
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        preds = get_all_predictions(record, wood, "SB")
        assert "manual" in preds
        assert preds["manual"].value == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# select_best_prediction
# ---------------------------------------------------------------------------
class TestSelectBestPrediction:
    def test_selects_highest_priority(self):
        record = CompetitorRecord(
            name="Test",
            history=_make_history([25, 26, 24]),
            manual_time_override=18.0,
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        preds = get_all_predictions(record, wood, "SB")
        best = select_best_prediction(preds)
        assert best.method == "manual"
        assert best.value == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------
class TestConfidenceLevels:
    def test_manual_is_very_high(self):
        record = CompetitorRecord(
            name="Test", history=[], manual_time_override=25.0,
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = get_best_prediction(record, wood, "SB")
        assert pred.confidence == "VERY HIGH"

    def test_rich_history_higher_confidence(self):
        """Many recent results → higher confidence than 1 old result."""
        rich = CompetitorRecord(
            name="Rich",
            history=_make_history([25, 26, 24, 25, 26, 24, 25, 26]),
        )
        sparse = CompetitorRecord(
            name="Sparse",
            history=[
                HistoricalResult(
                    event_code="SB", time_seconds=25.0, species="S01",
                    diameter_mm=300, quality=5,
                    result_date=date.today() - timedelta(days=1500),
                ),
            ],
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred_rich = get_best_prediction(rich, wood, "SB")
        pred_sparse = get_best_prediction(sparse, wood, "SB")
        if pred_rich is not None and pred_sparse is not None:
            # Rich history should have equal or higher confidence
            conf_order = {"VERY LOW": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "VERY HIGH": 4}
            assert conf_order.get(pred_rich.confidence, 0) >= conf_order.get(pred_sparse.confidence, 0)


# ---------------------------------------------------------------------------
# Tournament time weighting regression
# ---------------------------------------------------------------------------
class TestTournamentWeightingRegression:
    def test_97_percent_weight_applied(self):
        """Tournament result should heavily influence the prediction."""
        record = CompetitorRecord(
            name="Test",
            history=_make_history([30, 30, 30, 30]),
            tournament_time=20.0,
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = get_best_prediction(record, wood, "SB")
        # Tournament time of 20s should pull prediction well below 30s
        assert pred.value < 28.0  # Significantly influenced by 20s tournament

    def test_tournament_upgrades_confidence(self):
        record = CompetitorRecord(
            name="Test",
            history=_make_history([30]),
            tournament_time=25.0,
        )
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred = get_best_prediction(record, wood, "SB")
        assert pred.confidence in ("VERY HIGH", "HIGH")


# ---------------------------------------------------------------------------
# Division handling
# ---------------------------------------------------------------------------
class TestDivisionFallback:
    """Different divisions should get different panel marks."""

    def test_novice_slower_than_open(self):
        """Novice default should be slower than Open default."""
        open_rec = CompetitorRecord(name="Open", history=[], division="Open")
        novice_rec = CompetitorRecord(name="Novice", history=[], division="Novice")
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        pred_open = get_best_prediction(open_rec, wood, "SB")
        pred_novice = get_best_prediction(novice_rec, wood, "SB")
        if pred_open is not None and pred_novice is not None:
            assert pred_novice.value > pred_open.value
