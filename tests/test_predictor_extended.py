"""Extended tests for strathmark/predictor.py — MLModel, LLM, species affinity, edge cases."""

from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    PredictionResult,
    WoodProfile,
    MLModel,
    IsotonicCalibrator,
    VarianceScaler,
    get_best_prediction,
    get_all_predictions,
    select_best_prediction,
    predict_baseline,
    predict_with_llm,
    _competitor_history_to_df,
)
from strathmark.config import rules, data_req, ml_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _history(event_code="SB", n=5, base_time=50.0, days_apart=30, species="S01", diameter=300):
    """Build a list of HistoricalResult spanning *n* dates."""
    today = date.today()
    return [
        HistoricalResult(
            event_code=event_code,
            time_seconds=base_time + i * 0.5,
            species=species,
            diameter_mm=diameter,
            quality=5,
            result_date=today - timedelta(days=(n - i) * days_apart),
        )
        for i in range(n)
    ]


PINE_300 = WoodProfile(species="S01", diameter_mm=300, quality=5)


def _make_training_df(n=200, seed=42):
    """Build a minimal results DataFrame for ML training."""
    rng = np.random.RandomState(seed)
    names = [f"Comp{i}" for i in range(20)]
    rows = []
    for _ in range(n):
        comp = rng.choice(names)
        event = rng.choice(["SB", "UH"])
        diameter = rng.choice([275, 300, 325, 350])
        base_time = rng.uniform(15, 60)
        rows.append({
            'competitor_name': comp,
            'event': event,
            'raw_time': base_time,
            'species': rng.choice(["S01", "S03", "S05"]),
            'size_mm': diameter,
            'quality': rng.randint(3, 8),
            'date': date.today() - timedelta(days=rng.randint(1, 700)),
            'gender': rng.choice(['M', 'F']),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# MLModel class
# ---------------------------------------------------------------------------

class TestMLModel:

    def test_untrained_predict_returns_none(self):
        """Prediction on untrained model should return None."""
        ml = MLModel()
        comp = CompetitorRecord(name="A", history=_history())
        result = ml.predict(comp, PINE_300, "SB")
        assert result is None

    def test_train_with_none_returns_false(self):
        ml = MLModel()
        assert ml.train(None) is False

    def test_train_with_empty_df_returns_false(self):
        ml = MLModel()
        assert ml.train(pd.DataFrame()) is False

    def test_train_with_insufficient_data_returns_false(self):
        """Need at least MIN_ML_TRAINING_RECORDS_TOTAL rows."""
        small_df = _make_training_df(n=10)
        ml = MLModel()
        result = ml.train(small_df)
        assert result is False

    def test_train_with_sufficient_data(self):
        """Training with enough data should succeed."""
        df = _make_training_df(n=250)
        ml = MLModel()
        result = ml.train(df)
        assert result is True
        assert ml._is_trained is True

    def test_predict_after_training(self):
        """After training, predict should return a PredictionResult."""
        df = _make_training_df(n=250)
        ml = MLModel()
        ml.train(df)

        comp = CompetitorRecord(
            name="Comp0",
            history=_history(n=5, base_time=30.0),
            gender="M",
        )
        result = ml.predict(comp, PINE_300, "SB")
        # ML may return None if prediction is out of range, but shouldn't crash
        if result is not None:
            assert result.method == "ml"
            assert result.value > 0
            assert result.confidence in ("HIGH", "MEDIUM")

    def test_predict_with_no_history_returns_none(self):
        """MLModel.predict with empty history should return None."""
        df = _make_training_df(n=250)
        ml = MLModel()
        ml.train(df)

        comp = CompetitorRecord(name="NewGuy", history=[])
        result = ml.predict(comp, PINE_300, "SB")
        assert result is None

    def test_predict_cross_event_history(self):
        """Competitor with only UH history should still attempt SB prediction."""
        df = _make_training_df(n=250)
        ml = MLModel()
        ml.train(df)

        comp = CompetitorRecord(
            name="UHOnly",
            history=_history(event_code="UH", n=5, base_time=40.0),
        )
        # Predicting SB with only UH history — may return None or use cross-event
        result = ml.predict(comp, PINE_300, "SB")
        # Should not crash either way


# ---------------------------------------------------------------------------
# IsotonicCalibrator
# ---------------------------------------------------------------------------

class TestIsotonicCalibrator:

    def test_unfitted_returns_original(self):
        cal = IsotonicCalibrator()
        assert cal.calibrate(30.0, "SB") == 30.0
        assert cal.calibrate(30.0, "UH") == 30.0

    def test_unknown_event_returns_original(self):
        cal = IsotonicCalibrator()
        cal.is_fitted = True
        assert cal.calibrate(30.0, "XX") == 30.0


# ---------------------------------------------------------------------------
# VarianceScaler
# ---------------------------------------------------------------------------

class TestVarianceScaler:

    def test_unfitted_returns_baseline(self):
        vs = VarianceScaler()
        assert vs.predict_std_dev({}, "SB") == 3.0
        assert vs.predict_std_dev({}, "SB", baseline_std=5.0) == 5.0


# ---------------------------------------------------------------------------
# predict_with_llm
# ---------------------------------------------------------------------------

class TestPredictWithLLM:

    def test_quality_5_skips_llm_call(self):
        """Quality 5 should skip LLM entirely and return baseline directly."""
        comp = CompetitorRecord(name="A", history=_history())
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        result = predict_with_llm(comp, wood, "SB", baseline_time=30.0)
        assert result is not None
        assert result.value == 30.0
        assert result.method == "llm"
        assert "quality 5" in result.explanation.lower()

    @patch("strathmark.llm.call_ollama")
    def test_quality_above_5_calls_llm(self, mock_ollama):
        """Quality > 5 should call LLM for adjustment."""
        import json
        mock_ollama.return_value = json.dumps({
            "multiplier": 1.06,
            "confidence": "HIGH",
            "explanation": "Quality 8 is harder"
        })
        comp = CompetitorRecord(name="A", history=_history())
        wood = WoodProfile(species="S01", diameter_mm=300, quality=8)
        result = predict_with_llm(comp, wood, "SB", baseline_time=30.0)
        # Either returns a result with LLM adjustment, or falls back
        # depending on whether mock response is parsed
        if result is not None:
            assert result.value > 0

    @patch("strathmark.llm.call_ollama")
    def test_ollama_unavailable_returns_none_or_fallback(self, mock_ollama):
        """When Ollama is down, predict_with_llm should return None or fallback."""
        mock_ollama.return_value = None
        comp = CompetitorRecord(name="A", history=_history())
        wood = WoodProfile(species="S01", diameter_mm=300, quality=8)
        result = predict_with_llm(comp, wood, "SB", baseline_time=30.0)
        # Should either return None or a statistical fallback


# ---------------------------------------------------------------------------
# _competitor_history_to_df
# ---------------------------------------------------------------------------

class TestCompetitorHistoryToDf:

    def test_no_history_returns_none(self):
        comp = CompetitorRecord(name="A", history=[])
        assert _competitor_history_to_df(comp) is None

    def test_creates_df_with_correct_columns(self):
        comp = CompetitorRecord(name="A", history=_history(n=3))
        df = _competitor_history_to_df(comp)
        assert df is not None
        assert 'competitor_name' in df.columns
        assert 'event' in df.columns
        assert 'raw_time' in df.columns
        assert len(df) == 3

    def test_filters_zero_times(self):
        history = [
            HistoricalResult("SB", 30.0, "S01", 300, 5, date.today()),
            HistoricalResult("SB", 0.0, "S01", 300, 5, date.today()),
        ]
        comp = CompetitorRecord(name="A", history=history)
        df = _competitor_history_to_df(comp)
        assert len(df) == 1


# ---------------------------------------------------------------------------
# predict_baseline edge cases
# ---------------------------------------------------------------------------

class TestPredictBaselineEdgeCases:

    def test_competitor_with_one_result(self):
        """Single result should still produce a prediction (low confidence)."""
        comp = CompetitorRecord(
            name="OneTimer",
            history=[HistoricalResult("SB", 35.0, "S01", 300, 5, date.today())],
        )
        result = predict_baseline(comp, PINE_300, "SB")
        assert result is not None
        assert result.value > 0
        assert result.confidence in ("MEDIUM", "LOW")

    def test_all_history_wrong_event(self):
        """All history in UH, predicting SB -> should use cross-event or fall back."""
        comp = CompetitorRecord(
            name="UHOnly",
            history=_history(event_code="UH", n=5, base_time=40.0),
        )
        result = predict_baseline(comp, PINE_300, "SB")
        # May return None or a baseline from event data
        # Should not crash

    def test_quality_adjustment_applied(self):
        """Quality != 5 should adjust the baseline."""
        comp = CompetitorRecord(name="A", history=_history(n=5, base_time=30.0))
        wood_q5 = WoodProfile(species="S01", diameter_mm=300, quality=5)
        wood_q8 = WoodProfile(species="S01", diameter_mm=300, quality=8)

        result_q5 = predict_baseline(comp, wood_q5, "SB")
        result_q8 = predict_baseline(comp, wood_q8, "SB")

        assert result_q5 is not None
        assert result_q8 is not None
        # Higher quality = harder wood = longer time
        assert result_q8.value > result_q5.value

    def test_tournament_time_round1_vs_round4(self):
        """More tournament rounds should give higher weight to tournament time."""
        comp_r1 = CompetitorRecord(
            name="A", history=_history(n=3, base_time=30.0),
            tournament_time=50.0, num_tournament_rounds=1,
        )
        comp_r4 = CompetitorRecord(
            name="A", history=_history(n=3, base_time=30.0),
            tournament_time=50.0, num_tournament_rounds=4,
        )

        result_r1 = predict_baseline(comp_r1, PINE_300, "SB")
        result_r4 = predict_baseline(comp_r4, PINE_300, "SB")

        assert result_r1 is not None
        assert result_r4 is not None
        # Round 4 -> 97% weight on tournament_time (50s)
        # Round 1 -> 65% weight on tournament_time (50s)
        # Both should be above historical baseline (~30s) but R4 closer to 50
        assert result_r4.value > result_r1.value

    def test_no_history_no_results_df_returns_none(self):
        """No history and no external data -> None."""
        comp = CompetitorRecord(name="Ghost", history=[])
        result = predict_baseline(comp, PINE_300, "SB")
        assert result is None


# ---------------------------------------------------------------------------
# Cascade priority ordering
# ---------------------------------------------------------------------------

class TestCascadePriority:

    def test_manual_beats_everything(self):
        preds = {
            "manual": PredictionResult(40.0, "VERY HIGH", "manual", "override"),
            "llm": PredictionResult(45.0, "HIGH", "llm", "llm adj"),
            "ml": PredictionResult(42.0, "HIGH", "ml", "model"),
            "baseline": PredictionResult(50.0, "HIGH", "baseline", "history"),
            "panel": PredictionResult(20.0, "VERY LOW", "panel", "default"),
        }
        best = select_best_prediction(preds)
        assert best.method == "manual"

    def test_ml_beats_llm_due_to_llm_penalty(self):
        """select_best_prediction applies +0.5 expected error penalty to LLM.
        With equal confidence, ML should win over LLM."""
        preds = {
            "manual": None,
            "llm": PredictionResult(45.0, "HIGH", "llm", "llm adj"),
            "ml": PredictionResult(42.0, "HIGH", "ml", "model"),
            "baseline": PredictionResult(50.0, "HIGH", "baseline", "history"),
            "panel": PredictionResult(20.0, "VERY LOW", "panel", "default"),
        }
        best = select_best_prediction(preds)
        assert best.method == "ml"

    def test_ml_beats_baseline_and_panel(self):
        preds = {
            "manual": None,
            "llm": None,
            "ml": PredictionResult(42.0, "HIGH", "ml", "model"),
            "baseline": PredictionResult(50.0, "HIGH", "baseline", "history"),
            "panel": PredictionResult(20.0, "VERY LOW", "panel", "default"),
        }
        best = select_best_prediction(preds)
        assert best.method == "ml"

    def test_baseline_beats_panel(self):
        preds = {
            "manual": None,
            "llm": None,
            "ml": None,
            "baseline": PredictionResult(50.0, "HIGH", "baseline", "history"),
            "panel": PredictionResult(20.0, "VERY LOW", "panel", "default"),
        }
        best = select_best_prediction(preds)
        assert best.method == "baseline"

    def test_panel_is_last_resort(self):
        preds = {
            "manual": None,
            "llm": None,
            "ml": None,
            "baseline": None,
            "panel": PredictionResult(20.0, "VERY LOW", "panel", "default"),
        }
        best = select_best_prediction(preds)
        assert best.method == "panel"


# ---------------------------------------------------------------------------
# get_best_prediction edge cases
# ---------------------------------------------------------------------------

class TestGetBestPredictionEdgeCases:

    def test_uh_event_code(self):
        """UH event code should work identically to SB."""
        comp = CompetitorRecord(name="A", history=_history(event_code="UH", n=5))
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        result = get_best_prediction(comp, wood, "UH")
        assert result is not None
        assert result.value > 0

    def test_case_insensitive_event_code(self):
        """'sb' should be treated the same as 'SB'."""
        comp = CompetitorRecord(name="A", history=_history(n=5))
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        result = get_best_prediction(comp, wood, "sb")
        assert result is not None

    def test_many_historical_results(self):
        """20+ results should not cause issues."""
        comp = CompetitorRecord(
            name="Veteran",
            history=_history(n=20, base_time=30.0, days_apart=30),
        )
        result = get_best_prediction(comp, PINE_300, "SB")
        assert result is not None
        assert result.value > 0

    def test_very_fast_competitor(self):
        """Extremely fast times should still produce valid predictions."""
        comp = CompetitorRecord(
            name="Elite",
            history=_history(n=5, base_time=12.0),
        )
        result = get_best_prediction(comp, PINE_300, "SB")
        assert result is not None
        assert result.value > 0

    def test_very_slow_competitor(self):
        """Very slow times should still produce valid predictions."""
        comp = CompetitorRecord(
            name="Novice",
            history=_history(n=5, base_time=120.0),
        )
        result = get_best_prediction(comp, PINE_300, "SB")
        assert result is not None
        assert result.value > 0
