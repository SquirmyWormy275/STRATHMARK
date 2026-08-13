"""Tests for strathmark/predictor.py — prediction cascade and form trajectory."""

from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from strathmark.prediction_v2 import ForecastInterval, PredictiveDistribution
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    PredictionBundle,
    PredictionContext,
    PredictionInterval,
    PredictionResult,
    StaticPredictionProvider,
    WoodProfile,
    _apply_form_trajectory,
    get_all_predictions,
    get_best_prediction,
    select_best_prediction,
)


class _RecordingCore:
    model_version = "core-test"
    source_checksum = "a" * 64

    class _Calibration:
        version = "cal-test"

    calibration = _Calibration()

    def __init__(self, value=41.0):
        self.value = value
        self.requests = []
        self.histories = []

    def predict(self, request, *, history=None, wood_df=None):
        self.requests.append(request)
        self.histories.append(history.copy())
        return PredictiveDistribution(
            median=self.value,
            log_location=3.7,
            log_scale=0.2,
            interval=ForecastInterval(30.0, 55.0, calibration_state="calibrated"),
            source="hierarchical_dynamic_core",
            history_count=len(history) if history is not None else 0,
            effective_history_weight=1.0,
            model_version=self.model_version,
            calibration_version=self.calibration.version,
        )


def _provider(value=41.0):
    core = _RecordingCore(value)
    bundle = PredictionBundle(core=core, source="injected")
    return core, StaticPredictionProvider(bundle)


def test_public_dataclasses_preserve_old_positional_constructors_and_add_v2_fields():
    history = HistoricalResult("SB", 40.0, "S01", 300.0, 5)
    competitor = CompetitorRecord("Alex", [history])
    wood = WoodProfile("S01", 300.0, 5)
    prediction = PredictionResult(40.0, "HIGH", "baseline", "legacy constructor")

    assert competitor.competitor_id is None
    assert wood.quality == 5
    assert prediction.interval is None
    assert prediction.engine_version is None
    assert prediction.metadata == {}

    interval = PredictionInterval(35.0, 47.0, 0.9, "calibrated", "event")
    context = PredictionContext(prediction_as_of=date(2026, 8, 11), request_id="request-1")
    assert interval.lower == 35.0
    assert interval.upper == 47.0
    assert context.prediction_as_of == date(2026, 8, 11)


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
        assert result.interval is None
        assert result.metadata["source"] == "operator_override"
        assert result.metadata["is_override"] is True
        assert result.metadata["confidence_kind"] == "operator_authority"

    def test_v2_never_calls_numeric_llm(self, monkeypatch):
        def fail(*args, **kwargs):
            raise AssertionError("numeric LLM must never run")

        monkeypatch.setattr("strathmark.predictor.predict_with_llm", fail)
        _, provider = _provider()
        comp = CompetitorRecord(name="A", history=_history())
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=9)

        result = get_best_prediction(
            comp,
            wood,
            "SB",
            llm_client={"url": "unused"},
            prediction_provider=provider,
            context=PredictionContext(prediction_as_of=date.today()),
        )

        assert result.method == "baseline"
        assert result.value == 41.0

    def test_cutoff_is_forwarded_to_the_core(self):
        core, provider = _provider()
        cutoff = date(2025, 7, 1)
        comp = CompetitorRecord(name="A", history=[], competitor_id="C-1")
        result = get_best_prediction(
            comp,
            WoodProfile("S01", 300, 5),
            "SB",
            prediction_provider=provider,
            context=PredictionContext(prediction_as_of=cutoff),
        )

        assert result.evidence_cutoff == cutoff
        assert core.requests[0].prediction_as_of == cutoff

    def test_unknown_species_uses_pooled_properties_and_is_flagged(self):
        core, provider = _provider()
        wood_data = pd.DataFrame(
            {
                "speciesID": ["S01", "S02", "S03"],
                "janka_hard": [1000.0, 2000.0, 9000.0],
                "spec_gravity": [0.3, 0.5, 0.9],
                "crush_strength": [3000.0, 5000.0, 9000.0],
                "shear": [700.0, 1100.0, 1900.0],
                "MOR": [6000.0, 8000.0, 14000.0],
                "MOE": [800000.0, 1200000.0, 2400000.0],
            }
        )

        get_best_prediction(
            CompetitorRecord("A"),
            WoodProfile("UNLISTED", 300, 5),
            "SB",
            wood_data_df=wood_data,
            prediction_provider=provider,
        )

        request = core.requests[0]
        assert request.species_missing is True
        assert request.janka_hardness == 2000.0
        assert request.specific_gravity == 0.5
        assert request.crush_strength == 5000.0
        assert request.shear_strength == 1100.0
        assert request.modulus_of_rupture == 8000.0
        assert request.modulus_of_elasticity == 1200000.0

    def test_inactive_inputs_are_numeric_no_ops(self):
        _, provider = _provider()
        cutoff = date.today()
        base = CompetitorRecord(name="A", history=_history(), division="Open")
        changed = CompetitorRecord(
            name="A",
            history=_history(),
            division="Junior",
            tournament_time=5.0,
            num_tournament_rounds=99,
        )

        first = get_best_prediction(
            base,
            WoodProfile("S01", 300, 1),
            "SB",
            prediction_provider=provider,
            context=PredictionContext(prediction_as_of=cutoff),
        )
        second = get_best_prediction(
            changed,
            WoodProfile("S01", 300, 10),
            "SB",
            prediction_provider=provider,
            context=PredictionContext(prediction_as_of=cutoff),
        )

        assert first.value.hex() == second.value.hex()
        assert first.interval == second.interval

    def test_request_history_is_self_contained_and_global_frame_is_ignored(self):
        core, provider = _provider()
        comp = CompetitorRecord(
            name="A",
            competitor_id="C-1",
            history=[HistoricalResult("SB", 42.0, "S01", 300, 5, date(2025, 1, 1))],
        )
        global_frame = pd.DataFrame(
            {
                "competitor_id": ["C-1"],
                "event": ["SB"],
                "time_seconds": [5.0],
                "result_date": [date(2025, 1, 2)],
                "diameter_mm": [300],
                "species": ["S01"],
            }
        )

        get_best_prediction(
            comp,
            WoodProfile("S01", 300, 5),
            "SB",
            results_df=global_frame,
            prediction_provider=provider,
            context=PredictionContext(prediction_as_of=date(2026, 1, 1)),
        )

        assert core.histories[0]["time_seconds"].tolist() == [42.0]

    def test_missing_core_degrades_visibly_to_static_broad_prior(self):
        provider = StaticPredictionProvider(
            PredictionBundle(
                source="test",
                warnings=("core_artifact_invalid",),
                degraded=True,
            )
        )

        result = get_best_prediction(
            CompetitorRecord("A"),
            WoodProfile("S01", 300, 5),
            "SB",
            prediction_provider=provider,
        )

        assert result.method == "panel"
        assert result.value == 50.0
        assert result.interval.scope == "static_event"
        assert result.degraded is True
        assert "core_artifact_invalid" in result.warnings

    def test_explicit_rollback_is_deterministic_baseline_only(self, monkeypatch):
        def fail(*args, **kwargs):
            raise AssertionError("rollback invoked numeric LLM")

        monkeypatch.setattr("strathmark.predictor.predict_with_llm", fail)
        comp = CompetitorRecord(
            "A",
            [HistoricalResult("SB", 40.0, "S01", 300, 9, date(2025, 1, 1))],
        )
        context = PredictionContext(prediction_as_of=date(2026, 1, 1), engine="legacy")

        first = get_best_prediction(comp, WoodProfile("S01", 300, 1), "SB", context=context)
        second = get_best_prediction(comp, WoodProfile("S01", 300, 10), "SB", context=context)

        assert first.method == "baseline"
        assert first.value.hex() == second.value.hex()
        assert first.warnings == ["legacy_engine_selected"]

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

    def test_tournament_time_is_an_inactive_no_op(self):
        with_tournament = CompetitorRecord(
            name="A",
            history=_history(n=3),
            tournament_time=45.0,
        )
        without_tournament = CompetitorRecord(name="A", history=_history(n=3))
        wood = WoodProfile(species="poplar", diameter_mm=300, quality=5)
        first = get_best_prediction(with_tournament, wood, "SB")
        second = get_best_prediction(without_tournament, wood, "SB")

        assert first.value.hex() == second.value.hex()


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

    def test_v2_projection_has_exact_keys_and_no_llm(self):
        _, provider = _provider()
        comp = CompetitorRecord(name="A", history=_history())
        preds = get_all_predictions(
            comp,
            WoodProfile("S01", 300, 5),
            "SB",
            prediction_provider=provider,
        )

        assert list(preds) == ["manual", "llm", "ml", "baseline", "panel"]
        assert preds["llm"] is None
        assert preds["ml"] is None
        assert preds["baseline"].method == "baseline"
        assert preds["panel"].method == "panel"
        assert preds["baseline"].metadata["posterior_log_location"] == 3.7
        assert preds["baseline"].metadata["posterior_log_scale"] == 0.2

    def test_promoted_residual_is_the_only_ml_projection(self):
        core, _ = _provider()

        class Residual:
            loaded = SimpleNamespace(
                active=True,
                manifest={"model_version": "residual-test"},
            )

            def apply(self, distribution, features):
                assert set(features) >= {"core_log_location", "history_count", "event"}
                corrected = replace(
                    distribution,
                    median=39.0,
                    source="hierarchical_dynamic_core+catboost_residual",
                )
                return SimpleNamespace(
                    distribution=corrected,
                    applied=True,
                    degraded=False,
                    warning=None,
                )

        provider = StaticPredictionProvider(
            PredictionBundle(core=core, residual=Residual(), source="injected")
        )
        preds = get_all_predictions(
            CompetitorRecord("A"),
            WoodProfile("S01", 300, 5),
            "SB",
            prediction_provider=provider,
        )

        assert preds["ml"].value == 39.0
        assert preds["ml"].method == "ml"
        assert select_best_prediction(preds).method == "ml"

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

    @pytest.mark.parametrize("available", ["ml", "baseline", "panel"])
    def test_selection_is_manual_then_ml_then_baseline_then_panel(self, available):
        order = ["ml", "baseline", "panel"]
        preds = {"manual": None, "llm": None, "ml": None, "baseline": None, "panel": None}
        start = order.index(available)
        for method in order[start:]:
            preds[method] = PredictionResult(40.0, "LOW", method, method)

        assert select_best_prediction(preds).method == available


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
