from __future__ import annotations

import math
from dataclasses import replace

import pytest

from strathmark.v3.assessors.ml import (
    PITCalibrator,
    SpecialistGate,
    _interpolate_points,
    _predict_log_quantiles,
    _quantile_log_at,
    _seconds_to_ms,
    build_positive_distribution,
    combine_quantiles,
)
from strathmark.v3.factory.ml_training import (
    FEATURE_NAMES,
    CausalTrainingRow,
    GateExample,
    OOFComponentPrediction,
    SpecialistEligibility,
    _calibration_error,
    _fit_specialist_gate_values,
    _gate_examples_values,
    _prediction_values,
    _quantile_probability,
    _train_catboost_hierarchy,
    canonical_gate_features,
    mean_pinball_loss,
)

LEVELS = ("0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95")


def test_specialist_eligibility_uses_all_three_exact_context_thresholds() -> None:
    eligible = SpecialistEligibility(500, 30, 10)
    assert eligible.available
    for candidate in (
        SpecialistEligibility(499, 30, 10),
        SpecialistEligibility(500, 29, 10),
        SpecialistEligibility(500, 30, 9),
    ):
        assert not candidate.available


def test_gate_is_exact_zero_without_eligible_specialist_and_bounded_when_available() -> (
    None
):
    gate = SpecialistGate(
        "-20", (("log_history_depth", "4"), ("missing_fraction", "0"))
    )
    assert gate.weight({}, specialist_available=False) == 0.0
    assert (
        gate.weight(
            {"log_history_depth": 100.0, "missing_fraction": 0.0},
            specialist_available=True,
        )
        == 0.9
    )
    assert (
        gate.weight(
            {"log_history_depth": -100.0, "missing_fraction": 0.0},
            specialist_available=True,
        )
        == 0.1
    )
    middle = gate.weight(
        {"log_history_depth": 5.0, "missing_fraction": 0.0}, specialist_available=True
    )
    assert 0.1 <= middle <= 0.9


def test_bounded_logistic_gate_training_is_deterministic_and_rewards_oof_advantage() -> (
    None
):
    examples = tuple(
        GateExample(
            pinball_advantage=value,
            history_depth=20 if value > 0 else 1,
            missing_fraction=0.0,
            specialist_better=value > 0,
            fold_id=f"fold:{index}",
        )
        for index, value in enumerate((-0.4, -0.2, -0.1, 0.1, 0.2, 0.4), 1)
    )
    first = _fit_specialist_gate_values(examples)
    second = _fit_specialist_gate_values(examples)
    assert first == second
    assert first.weight(
        canonical_gate_features(history_depth=20, missing_fraction=0),
        specialist_available=True,
    ) > first.weight(
        canonical_gate_features(history_depth=1, missing_fraction=0),
        specialist_available=True,
    )
    assert first.to_dict() == SpecialistGate.from_dict(first.to_dict()).to_dict()


def test_gate_training_and_inference_share_one_canonical_transform() -> None:
    assert canonical_gate_features(history_depth=10, missing_fraction=0.25) == {
        "log_history_depth": math.log1p(10),
        "missing_fraction": 0.25,
    }
    repeated_folds = (
        GateExample(0.2, 10, 0.25, True, "fold:a"),
        GateExample(0.1, 11, 0.0, True, "fold:a"),
        GateExample(-0.2, 1, 1.0, False, "fold:b"),
        GateExample(-0.1, 2, 0.5, False, "fold:b"),
    )
    gate = _fit_specialist_gate_values(repeated_folds)
    assert {name for name, _ in gate.coefficients} == {
        "log_history_depth",
        "missing_fraction",
    }


def test_pinball_advantage_is_derived_from_component_oof_predictions() -> None:
    predictions = (
        OOFComponentPrediction(
            "evidence:a",
            "fold:a",
            (1.0,) * 7,
            (2.0,) * 7,
            "underhand|300|gum",
            10,
            0.0,
        ),
        OOFComponentPrediction(
            "evidence:b",
            "fold:b",
            (1.0,) * 7,
            None,
            "standing_block|300|gum",
            0,
            1.0,
        ),
    )
    target = replace(
        _training_row(0),
        row_id="evidence:a",
        target_log_seconds="2",
        field_id="field:a",
    )
    examples = _gate_examples_values(predictions, (target,))
    assert len(examples) == 1
    assert examples[0].specialist_better
    assert examples[0].pinball_advantage > 0
    assert mean_pinball_loss(2.0, (2.0,) * 7) == 0.0
    with pytest.raises(ValueError, match="exactly cover"):
        _gate_examples_values(
            (replace(predictions[0], row_id="evidence:missing"),), (target,)
        )


class _FakeCatBoost:
    def __init__(self, settings: dict[str, object]):
        self.settings = settings
        self.fitted = False

    def fit(
        self, features: list[list[object]], targets: list[float], **kwargs: object
    ) -> None:
        assert len(features) == len(targets)
        assert kwargs["cat_features"] == ["event_family", "species"]
        self.fitted = True


def _training_row(index: int) -> CausalTrainingRow:
    features: dict[str, object] = {
        "competitor_id": f"competitor:c{index % 30}",
        "event_family": "underhand",
        "species": "gum",
        "size_mm": 300,
        "density": 720.0,
        "density_missing": 0,
        "history_depth": index,
        "exact_history_depth": index,
        "history_log_median": 3.5,
        "history_log_spread": 0.1,
        "history_missing": 0,
        "sequence_recency": 0,
        "history_log_trend": 0.0,
        "context_distance": 0.0,
        "eligible_tournament_sequence": index,
        "current_form_log_seconds": 3.5,
    }
    return CausalTrainingRow(
        row_id=f"evidence:r{index}",
        competitor_id=f"competitor:c{index % 30}",
        tournament_id=f"tournament:t{index % 10}",
        occurred_at_utc="2026-01-02T00:00:00.000Z",
        observation_sequence=index + 1,
        specialist_key="underhand|300|gum",
        features=tuple((name, features[name]) for name in FEATURE_NAMES),
        target_log_seconds="3.5",
        source_packet_digest="a" * 64,
        training_max_sequence=index,
        training_max_occurred_at_utc=(
            "2026-01-01T00:00:00.000Z" if index else "0001-01-01T00:00:00.000Z"
        ),
        field_id=f"field:f{index}",
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
    )


def test_catboost_hierarchy_uses_only_frozen_multiquantile_and_eligible_specialist() -> (
    None
):
    models: list[_FakeCatBoost] = []

    def factory(**settings: object) -> _FakeCatBoost:
        model = _FakeCatBoost(dict(settings))
        models.append(model)
        return model

    universal, specialists, eligibility = _train_catboost_hierarchy(
        tuple(_training_row(index) for index in range(500)),
        model_factory=factory,
    )
    assert universal is models[0]
    assert set(specialists) == {"underhand|300|gum"}
    assert eligibility["underhand|300|gum"].available
    assert len(models) == 2
    assert all(model.fitted for model in models)
    assert all(
        model.settings["loss_function"]
        == "MultiQuantile:alpha=0.05,0.1,0.25,0.5,0.75,0.9,0.95"
        for model in models
    )
    assert all(model.settings["allow_writing_files"] is False for model in models)
    with pytest.raises(ValueError, match="at least one"):
        _train_catboost_hierarchy((), model_factory=factory)


def test_separate_role_isotonic_calibration_is_monotone_and_closed() -> None:
    quantiles = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    pits = tuple(_quantile_probability(value, quantiles) for value in (1.5, 2.5, 4.5))
    calibrator = PITCalibrator._fit_authorized_values(
        pits,
        source_digest="a" * 64,
    )
    assert calibrator.role == "calibration"
    values = [calibrator.map_probability(index / 100) for index in range(101)]
    assert values == sorted(values)
    assert values[0] == 0.0
    assert values[-1] == 1.0
    assert PITCalibrator.from_dict(calibrator.to_dict()) == calibrator


def test_quantile_crossings_are_repaired_and_explicit_tails_are_positive() -> None:
    crossed_log_quantiles = (
        math.log(30),
        math.log(29),
        math.log(31),
        math.log(30),
        math.log(35),
        math.log(34),
        math.log(40),
    )
    combined = combine_quantiles(crossed_log_quantiles, crossed_log_quantiles, 0.5)
    assert combined == tuple(sorted(combined))
    distribution = build_positive_distribution(
        combined,
        PITCalibrator.identity(source_digest="b" * 64),
    )
    probabilities = [point.probability for point in distribution.quantiles]
    times = [point.time_ms for point in distribution.quantiles]
    assert probabilities == ["0.001", *LEVELS, "0.999"]
    assert all(value > 0 for value in times)
    assert times == sorted(times)
    assert times[0] < times[1]
    assert times[-1] > times[-2]


@pytest.mark.parametrize("weight", [-0.1, 1.1, float("nan"), float("inf")])
def test_quantile_combination_rejects_invalid_weights(weight: float) -> None:
    values = tuple(math.log(item) for item in (20, 25, 30, 35, 40, 45, 50))
    with pytest.raises(ValueError, match="weight"):
        combine_quantiles(values, values, weight)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": "old"}, "unsupported"),
        ({"intercept": "1.0"}, "canonical"),
        ({"coefficients": []}, "immutable"),
        ({"coefficients": (("z", "1"), ("a", "1"))}, "closed schema"),
        ({"coefficients": (("a", "1"), ("a", "2"))}, "closed schema"),
        ({"coefficients": (("", "1"),)}, "closed schema"),
        (
            {
                "coefficients": (
                    ("log_history_depth", "1.0"),
                    ("missing_fraction", "0"),
                )
            },
            "canonical",
        ),
    ],
)
def test_gate_contract_rejects_malformed_values(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "intercept": "0",
        "coefficients": (("log_history_depth", "0"), ("missing_fraction", "0")),
        **changes,
    }
    with pytest.raises(ValueError, match=message):
        SpecialistGate(**values)  # type: ignore[arg-type]


def test_gate_mapping_and_runtime_reject_nonclosed_or_nonfinite_values() -> None:
    gate = SpecialistGate("0", (("log_history_depth", "1"), ("missing_fraction", "0")))
    with pytest.raises(ValueError, match="fields"):
        SpecialistGate.from_dict({"schema_version": gate.schema_version})
    with pytest.raises(ValueError, match="string object"):
        SpecialistGate.from_dict({**gate.to_dict(), "coefficients": []})
    with pytest.raises(ValueError, match="finite"):
        gate.weight(
            {"log_history_depth": math.nan, "missing_fraction": 0.0},
            specialist_available=True,
        )
    with pytest.raises(ValueError, match="closed schema"):
        gate.weight({"log_history_depth": 1.0}, specialist_available=True)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": "old"}, "unsupported"),
        ({"role": "training"}, "separate calibration"),
        ({"points": []}, "boundary points"),
        ({"points": (("0.1", "0"), ("1", "1"))}, "boundaries"),
        (
            {"points": (("0", "0"), ("0.8", "0.5"), ("0.2", "0.7"), ("1", "1"))},
            "inputs",
        ),
        (
            {"points": (("0", "0"), ("0.2", "0.8"), ("0.7", "0.2"), ("1", "1"))},
            "outputs",
        ),
        ({"points": (("0", "0"), ("0.2", "1.2"), ("1", "1"))}, r"\[0, 1\]"),
    ],
)
def test_calibrator_contract_rejects_invalid_shapes(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "role": "calibration",
        "points": (("0", "0"), ("1", "1")),
        "source_digest": "a" * 64,
        **changes,
    }
    with pytest.raises(ValueError, match=message):
        PITCalibrator(**values)  # type: ignore[arg-type]


def test_calibrator_rejects_malformed_fit_mapping_and_probability() -> None:
    with pytest.raises(ValueError, match="finite probabilities"):
        PITCalibrator._fit_authorized_values((), source_digest="a" * 64)
    with pytest.raises(ValueError, match="finite probabilities"):
        PITCalibrator._fit_authorized_values((math.nan,), source_digest="a" * 64)
    identity = PITCalibrator.identity(source_digest="a" * 64)
    with pytest.raises(ValueError, match="fields"):
        PITCalibrator.from_dict({"role": "calibration"})
    with pytest.raises(ValueError, match="points"):
        PITCalibrator.from_dict({**identity.to_dict(), "points": {}})
    for value in (-0.1, 1.1, math.nan):
        with pytest.raises(ValueError, match="probability"):
            identity.map_probability(value)


def test_numeric_helpers_cover_interpolation_prediction_and_positive_bounds() -> None:
    points = (("0", "0"), ("0.5", "0.2"), ("0.5", "0.8"), ("1", "1"))
    assert _interpolate_points(points, 0.5, inverse=True) in {0.5, 1.0}
    assert _interpolate_points(points, 0.0, inverse=False) == 0.0
    assert _interpolate_points(points, 1.0, inverse=False) == 1.0
    assert _interpolate_points((("0", "0"), ("0.5", "0.5")), 1.0, inverse=False) == 0.5
    assert _quantile_log_at(0.2, (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)) == pytest.approx(
        2.6666666666666665
    )
    with pytest.raises(ValueError, match="seven"):
        build_positive_distribution(
            (1.0,), PITCalibrator.identity(source_digest="a" * 64)
        )
    values = (1.0,) * 7
    with pytest.raises(ValueError, match="finite"):
        combine_quantiles((math.nan, *values[1:]), values, 0.5)
    for predicted in ([1.0], [math.nan] * 7):
        model = type(
            "Model", (), {"predict": lambda self, rows, value=predicted: value}
        )()
        with pytest.raises(ValueError, match="seven|finite"):
            _predict_log_quantiles(model, [[]])
    assert _prediction_values([[1.0] * 7]) == (1.0,) * 7
    array_like = type("ArrayLike", (), {"tolist": lambda self: [[1.0] * 7]})()
    assert (
        _predict_log_quantiles(
            type("Model", (), {"predict": lambda self, rows: array_like})(), [[]]
        )
        == (1.0,) * 7
    )
    with pytest.raises(ValueError, match="seven"):
        _prediction_values([1.0])
    with pytest.raises(ValueError, match="finite"):
        _prediction_values([math.nan] * 7)
    for value in (0.0, -1.0, math.inf):
        with pytest.raises(ValueError, match="positive"):
            _seconds_to_ms(value)
    assert _seconds_to_ms(0.0001) == 1
    with pytest.raises(ValueError, match="bounds"):
        combine_quantiles((1_000.0,) * 7, (1_000.0,) * 7, 0.5)
    with pytest.raises(ValueError, match="matched nonempty"):
        _calibration_error((), ())


def test_calibrator_fit_collapses_observed_probability_boundaries() -> None:
    calibrator = PITCalibrator._fit_authorized_values(
        (0.0, 0.5, 1.0), source_digest="a" * 64
    )
    assert calibrator.points[0] == ("0", "0")
    assert calibrator.points[-1] == ("1", "1")
