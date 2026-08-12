"""Tests for the optional, evidence-gated CatBoost residual learner."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from strathmark.prediction_v2 import ForecastInterval, PredictiveDistribution
from strathmark.residual import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    RESIDUAL_FEATURE_NAMES,
    ResidualRuntime,
    evaluate_residual_promotion,
    fit_catboost_residual,
    load_residual_artifact,
    save_residual_artifact,
)


class _FakeModel:
    def save_model(self, path: str, *, format: str) -> None:
        assert format == "cbm"
        with open(path, "wb") as handle:
            handle.write(b"safe-native-catboost-placeholder")


class _FakeCatBoostRegressor:
    def load_model(self, path: str, *, format: str) -> None:
        assert format == "cbm"
        with open(path, "rb") as handle:
            assert handle.read() == b"safe-native-catboost-placeholder"

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        assert tuple(frame.columns) == RESIDUAL_FEATURE_NAMES
        return np.asarray([0.05] * len(frame))


def _comparison(count: int = 100) -> pd.DataFrame:
    actual = np.full(count, 100.0)
    return pd.DataFrame(
        {
            "actual_time": actual,
            "core_prediction": np.full(count, 110.0),
            "residual_prediction": np.full(count, 109.9),
            "core_lower": np.where(np.arange(count) < 90, 90.0, 101.0),
            "core_upper": np.full(count, 120.0),
            "residual_lower": np.where(np.arange(count) < 88, 90.0, 101.0),
            "residual_upper": np.full(count, 120.0),
            "history_count": np.full(count, 5),
        }
    )


def _accepted_decision():
    decision = evaluate_residual_promotion(_comparison())
    assert decision.promoted
    return decision


def _core_distribution() -> PredictiveDistribution:
    return PredictiveDistribution(
        median=100.0,
        log_location=math.log(100.0),
        log_scale=0.20,
        interval=ForecastInterval(80.0, 125.0),
        source="hierarchical_dynamic_core",
        history_count=5,
        effective_history_weight=4.0,
        model_version="core-v1",
    )


def _feature_row() -> dict[str, object]:
    return {
        "event": "SB",
        "gender": "M",
        "species": "S01",
        "species_missing": False,
        "log_diameter_ratio": 0.0,
        "janka_hardness": 1700.0,
        "specific_gravity": 0.35,
        "crush_strength": 4200.0,
        "shear_strength": 950.0,
        "modulus_of_rupture": 8500.0,
        "modulus_of_elasticity": 1_200_000.0,
        "core_log_location": math.log(100.0),
        "history_count": 5,
        "effective_history_weight": 4.0,
        "same_event_state": 0.01,
        "trend_projection": 0.0,
        "cross_event_state": 0.0,
    }


def test_exact_locked_thresholds_accept_deterministically():
    first = evaluate_residual_promotion(_comparison())
    second = evaluate_residual_promotion(_comparison())

    assert first == second
    assert first.promoted
    assert first.mae_relative_improvement == pytest.approx(0.01)
    assert first.rmse_relative_improvement == pytest.approx(0.01)
    assert first.coverage_distance_change == pytest.approx(0.02)
    assert first.bootstrap_resamples == BOOTSTRAP_RESAMPLES == 2_000
    assert first.bootstrap_seed == BOOTSTRAP_SEED == 20260811


def test_ties_reject_residual():
    comparison = _comparison()
    comparison["residual_prediction"] = comparison["core_prediction"]
    decision = evaluate_residual_promotion(comparison)

    assert not decision.promoted
    assert "point_accuracy_gate_failed" in decision.reasons


def test_eligible_history_cohort_cannot_be_sacrificed_for_global_lift():
    comparison = _comparison(100)
    comparison["history_count"] = [0] * 30 + [5] * 70
    comparison.loc[:29, "residual_prediction"] = 110.6
    comparison.loc[30:, "residual_prediction"] = 102.0
    decision = evaluate_residual_promotion(comparison)

    assert not decision.promoted
    assert "cohort_harm_gate_failed" in decision.reasons
    assert decision.cohorts["0"]["count"] == 30
    assert decision.cohorts["0"]["mae_worsening"] == pytest.approx(0.06)


def test_exact_five_percent_cohort_worsening_is_allowed():
    comparison = _comparison(100)
    comparison["history_count"] = [0] * 30 + [5] * 70
    comparison.loc[:29, "residual_prediction"] = 110.5
    comparison.loc[30:, "residual_prediction"] = 102.0
    decision = evaluate_residual_promotion(comparison)

    assert decision.promoted
    assert decision.cohorts["0"]["mae_worsening"] == pytest.approx(0.05)


def test_missing_artifact_preserves_exact_core_distribution(tmp_path):
    loaded = load_residual_artifact(tmp_path / "absent")
    core = _core_distribution()
    application = ResidualRuntime(loaded).apply(core, _feature_row())

    assert application.distribution is core
    assert application.warning == "residual_artifact_missing"
    assert application.degraded
    assert not application.applied


def test_missing_dependency_preserves_exact_core_distribution(tmp_path, monkeypatch):
    from strathmark import residual

    artifact = save_residual_artifact(
        _FakeModel(),
        tmp_path / "artifact",
        model_version="residual-v1",
        training_cutoff=date(2025, 1, 1),
        evidence_max_date=date(2024, 12, 31),
        core_source_checksum="a" * 64,
        promotion=_accepted_decision(),
    )

    def unavailable():
        raise ImportError("catboost deliberately absent")

    monkeypatch.setattr(residual, "_import_catboost_regressor", unavailable)
    loaded = load_residual_artifact(artifact)
    core = _core_distribution()
    application = ResidualRuntime(loaded).apply(core, _feature_row())

    assert application.distribution is core
    assert application.warning == "residual_dependency_unavailable"
    assert application.degraded


@pytest.mark.parametrize("damage", ["model", "checksum", "schema"])
def test_corrupt_checksum_or_schema_fails_closed(tmp_path, monkeypatch, damage):
    from strathmark import residual

    artifact = save_residual_artifact(
        _FakeModel(),
        tmp_path / damage,
        model_version="residual-v1",
        training_cutoff=date(2025, 1, 1),
        evidence_max_date=date(2024, 12, 31),
        core_source_checksum="b" * 64,
        promotion=_accepted_decision(),
    )
    manifest_path = artifact / "manifest.json"
    envelope = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "model":
        (artifact / "residual.cbm").write_bytes(b"tampered")
    elif damage == "checksum":
        envelope["payload"]["model_version"] = "tampered"
        manifest_path.write_text(json.dumps(envelope), encoding="utf-8")
    else:
        envelope["schema_version"] = 999
        manifest_path.write_text(json.dumps(envelope), encoding="utf-8")

    monkeypatch.setattr(residual, "_import_catboost_regressor", lambda: _FakeCatBoostRegressor)
    loaded = load_residual_artifact(artifact)
    core = _core_distribution()
    application = ResidualRuntime(loaded).apply(core, _feature_row())

    assert application.distribution is core
    assert loaded.warning == "residual_artifact_invalid"
    assert loaded.degraded


def test_core_checksum_mismatch_fails_closed(tmp_path, monkeypatch):
    from strathmark import residual

    artifact = save_residual_artifact(
        _FakeModel(),
        tmp_path / "artifact",
        model_version="residual-v1",
        training_cutoff=date(2025, 1, 1),
        evidence_max_date=date(2024, 12, 31),
        core_source_checksum="c" * 64,
        promotion=_accepted_decision(),
    )
    monkeypatch.setattr(residual, "_import_catboost_regressor", lambda: _FakeCatBoostRegressor)

    loaded = load_residual_artifact(artifact, expected_core_checksum="d" * 64)

    assert not loaded.active
    assert loaded.warning == "residual_artifact_incompatible"


def test_valid_promoted_artifact_applies_a_bounded_log_correction(tmp_path, monkeypatch):
    from strathmark import residual

    artifact = save_residual_artifact(
        _FakeModel(),
        tmp_path / "valid",
        model_version="residual-v1",
        training_cutoff=date(2025, 1, 1),
        evidence_max_date=date(2024, 12, 31),
        core_source_checksum="1" * 64,
        promotion=_accepted_decision(),
    )
    monkeypatch.setattr(residual, "_import_catboost_regressor", lambda: _FakeCatBoostRegressor)

    loaded = load_residual_artifact(artifact, expected_core_checksum="1" * 64)
    application = ResidualRuntime(loaded).apply(_core_distribution(), _feature_row())

    assert loaded.active
    assert application.applied
    assert not application.degraded
    assert application.distribution.log_location == pytest.approx(math.log(100.0) + 0.05)
    assert application.distribution.median == pytest.approx(100.0 * math.exp(0.05))


def test_training_rejects_non_fold_residuals_before_importing_catboost(monkeypatch):
    from strathmark import residual

    monkeypatch.setattr(
        residual,
        "_import_catboost_regressor",
        lambda: pytest.fail("CatBoost must not load for unproven residuals"),
    )
    frame = pd.DataFrame([{**_feature_row(), "core_log_residual": 0.1}])

    with pytest.raises(ValueError, match="rolling-fold provenance"):
        fit_catboost_residual(frame)


def test_forged_promotion_cannot_mark_an_artifact_promoted(tmp_path):
    forged = replace(
        evaluate_residual_promotion(_comparison().assign(residual_prediction=110.0)),
        promoted=True,
        reasons=(),
    )

    with pytest.raises(ValueError, match="locked gates"):
        save_residual_artifact(
            _FakeModel(),
            tmp_path / "forged",
            model_version="forged",
            training_cutoff=date(2025, 1, 1),
            evidence_max_date=date(2024, 12, 31),
            core_source_checksum="f" * 64,
            promotion=forged,
        )


def test_valid_native_artifact_round_trip_when_catboost_is_installed(tmp_path):
    catboost = pytest.importorskip("catboost")
    frame = pd.DataFrame([_feature_row(), replace_row(_feature_row(), event="UH")])
    target = np.asarray([0.05, -0.03])
    model = catboost.CatBoostRegressor(iterations=3, depth=2, verbose=False, random_seed=17)
    model.fit(frame, target, cat_features=["event", "gender", "species"])
    artifact = save_residual_artifact(
        model,
        tmp_path / "native",
        model_version="residual-native-test",
        training_cutoff=date(2025, 1, 1),
        evidence_max_date=date(2024, 12, 31),
        core_source_checksum="e" * 64,
        promotion=_accepted_decision(),
    )

    loaded = load_residual_artifact(artifact, expected_core_checksum="e" * 64)
    application = ResidualRuntime(loaded).apply(_core_distribution(), _feature_row())

    assert loaded.active
    assert application.applied
    assert application.distribution.log_location != _core_distribution().log_location


def replace_row(row: dict[str, object], **changes: object) -> dict[str, object]:
    copy = dict(row)
    copy.update(changes)
    return copy
