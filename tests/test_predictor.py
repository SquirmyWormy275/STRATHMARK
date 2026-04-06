"""Tests for strathmark/predictor.py — prediction cascade and form trajectory."""

from datetime import date, timedelta

import pytest

from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    PredictionResult,
    WoodProfile,
    _apply_form_trajectory,
    get_all_predictions,
    get_best_prediction,
    select_best_prediction,
)


def _history(event_code="SB", n=5, base_time=50.0, days_apart=30):
    """Build a list of HistoricalResult spanning *n* dates."""
    today = date.today()
    return [
        HistoricalResult(
            event_code=event_code,
            time_seconds=base_time + i * 0.5,
            species="poplar",
            diameter_mm=300,
            quality=5,
            result_date=today - timedelta(days=(n - i) * days_apart),
        )
        for i in range(n)
    ]


class TestGetBestPrediction:
    """Prediction cascade tests."""

    def test_invalid_event_code_raises(self):
        comp = CompetitorRecord(name="A", history=[])
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        with pytest.raises(ValueError, match="Invalid event_code"):
            get_best_prediction(comp, wood, "INVALID")

    def test_manual_override_wins(self):
        comp = CompetitorRecord(
            name="A",
            history=_history(),
            manual_time_override=42.0,
        )
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        result = get_best_prediction(comp, wood, "SB")
        assert result.method == "manual"
        assert result.value == 42.0
        assert result.confidence == "VERY HIGH"

    def test_no_history_falls_back_to_panel(self):
        comp = CompetitorRecord(name="Newbie", history=[])
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        result = get_best_prediction(comp, wood, "SB")
        assert result is not None
        assert result.value > 0
        # With no history and no ML/LLM, should use panel or baseline
        assert result.method in ("panel", "baseline")

    def test_with_history_uses_baseline(self):
        comp = CompetitorRecord(name="Veteran", history=_history(n=5))
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        result = get_best_prediction(comp, wood, "SB")
        assert result is not None
        assert result.value > 0
        assert result.method in ("baseline", "panel")

    def test_tournament_time_weighted(self):
        comp = CompetitorRecord(
            name="A",
            history=_history(n=3),
            tournament_time=45.0,
        )
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        result = get_best_prediction(comp, wood, "SB")
        assert result is not None
        # Tournament time should dominate (97% weight)
        assert abs(result.value - 45.0) < 5.0


class TestGetAllPredictions:
    """Tests for get_all_predictions()."""

    def test_returns_all_keys(self):
        comp = CompetitorRecord(name="A", history=_history())
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        preds = get_all_predictions(comp, wood, "SB")
        assert "manual" in preds
        assert "llm" in preds
        assert "ml" in preds
        assert "baseline" in preds
        assert "panel" in preds

    def test_panel_always_present(self):
        comp = CompetitorRecord(name="A", history=[])
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        preds = get_all_predictions(comp, wood, "SB")
        assert preds["panel"] is not None

    def test_manual_override_present_when_set(self):
        comp = CompetitorRecord(
            name="A",
            history=[],
            manual_time_override=55.0,
        )
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        preds = get_all_predictions(comp, wood, "SB")
        assert preds["manual"] is not None
        assert preds["manual"].value == 55.0


class TestSelectBestPrediction:
    """Tests for select_best_prediction()."""

    def test_manual_always_wins(self):
        preds = {
            "manual": PredictionResult(
                value=40.0, confidence="VERY HIGH", method="manual", explanation="override"
            ),
            "baseline": PredictionResult(
                value=50.0, confidence="HIGH", method="baseline", explanation="history"
            ),
            "panel": PredictionResult(
                value=20.0, confidence="VERY LOW", method="panel", explanation="default"
            ),
            "llm": None,
            "ml": None,
        }
        best = select_best_prediction(preds)
        assert best.method == "manual"
        assert best.value == 40.0

    def test_panel_fallback_when_all_none(self):
        preds = {
            "manual": None,
            "llm": None,
            "ml": None,
            "baseline": None,
            "panel": PredictionResult(
                value=20.0, confidence="VERY LOW", method="panel", explanation="default"
            ),
        }
        best = select_best_prediction(preds)
        assert best.method == "panel"


class TestApplyFormTrajectory:
    """Tests for _apply_form_trajectory()."""

    def test_fewer_than_3_results_no_adjustment(self):
        comp = CompetitorRecord(name="A", history=_history(n=2))
        result = PredictionResult(
            value=50.0, confidence="HIGH", method="baseline", explanation="test"
        )
        adjusted = _apply_form_trajectory(result, comp, "SB")
        assert adjusted.value == result.value

    def test_stable_form_no_adjustment(self):
        """Very flat trend should not adjust."""
        history = [
            HistoricalResult(
                event_code="SB",
                time_seconds=50.0,
                species="poplar",
                diameter_mm=300,
                quality=5,
                result_date=date.today() - timedelta(days=(5 - i) * 30),
            )
            for i in range(5)
        ]
        comp = CompetitorRecord(name="A", history=history)
        result = PredictionResult(
            value=50.0, confidence="HIGH", method="baseline", explanation="test"
        )
        adjusted = _apply_form_trajectory(result, comp, "SB")
        # Should not adjust much (slope <0.5s/month)
        assert abs(adjusted.value - 50.0) < 1.0

    def test_adjustment_capped_at_8s(self):
        """Extreme trend should be capped at ±8 seconds."""
        history = [
            HistoricalResult(
                event_code="SB",
                time_seconds=50.0 + i * 10.0,  # 10s/result = extreme
                species="poplar",
                diameter_mm=300,
                quality=5,
                result_date=date.today() - timedelta(days=(5 - i) * 30),
            )
            for i in range(5)
        ]
        comp = CompetitorRecord(name="A", history=history)
        result = PredictionResult(
            value=90.0, confidence="HIGH", method="baseline", explanation="test"
        )
        adjusted = _apply_form_trajectory(result, comp, "SB")
        assert abs(adjusted.value - result.value) <= 8.0

    def test_pandas_timestamp_dates_handled(self):
        """Regression: ISSUE from eng-review — Timestamp vs date subtraction."""
        import pandas as pd

        history = [
            HistoricalResult(
                event_code="SB",
                time_seconds=50.0 + i,
                species="poplar",
                diameter_mm=300,
                quality=5,
                result_date=pd.Timestamp("2025-06-01") + pd.Timedelta(days=i * 30),
            )
            for i in range(5)
        ]
        comp = CompetitorRecord(name="A", history=history)
        result = PredictionResult(
            value=55.0, confidence="HIGH", method="baseline", explanation="test"
        )
        # Should not raise TypeError
        adjusted = _apply_form_trajectory(result, comp, "SB")
        assert adjusted.value > 0
