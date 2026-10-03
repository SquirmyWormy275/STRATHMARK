import math
from dataclasses import replace

import pytest

from strathmark.v3.factory.ml_training import _features, _train_catboost_hierarchy
from tests.v3.property.test_ml_temporal_features import _context, _observation


def test_relevant_history_excludes_other_discipline_and_preserves_exact_raw_times():
    target = _context(size=300)
    own = _observation(1, 40_000, day=1)
    smaller = replace(_observation(2, 20_000, day=2), context=_context(size=250))
    other = replace(_observation(3, 200_000, day=3), context=_context(event="standing_block"))
    features = _features(target, (own, smaller, other), eligible_sequence=3)
    assert features["history_depth"] == 3
    assert features["same_event_history_depth"] == 2
    assert features["same_material_history_depth"] == 2
    assert features["exact_history_log_median"] == pytest.approx(math.log(40))
    assert features["same_event_scaled_log_median"] == pytest.approx(
        (math.log(40) + math.log(20) + 2 * math.log(300 / 250)) / 2
    )


def test_material_features_do_not_treat_other_species_as_an_exact_match():
    other = replace(_observation(1, 60_000, day=1), context=_context(material="pine"))
    features = _features(_context(), (other,), eligible_sequence=1)
    assert features["same_material_history_depth"] == 0
    assert features["same_material_scaled_log_median"] == 0
    assert features["same_event_history_depth"] == 1
    assert features["same_event_scaled_log_median"] == pytest.approx(math.log(60))


@pytest.mark.parametrize("power", [-1, 3, float("nan"), float("inf")])
def test_training_rejects_invalid_target_weighting(power):
    from tests.v3.unit.test_ml_gate import _training_row

    with pytest.raises(ValueError, match="sample weight power"):
        _train_catboost_hierarchy((_training_row(0),), sample_weight_power=power)


def test_native_residual_export_reload_restores_seconds_at_unseen_diameter(tmp_path):
    catboost = pytest.importorskip("catboost")
    from strathmark.v3.factory.ml_artifacts import export_catboost_json
    from strathmark.v3.factory.ml_training import FEATURE_NAMES, predict_model_log_quantiles
    from tests.v3.unit.test_ml_gate import _training_row

    rows = []
    for index in range(30):
        row = _training_row(index)
        features = row.feature_dict
        features["same_event_scaled_log_median"] = math.log(30)
        features["same_event_history_depth"] = 2
        rows.append(
            replace(
                row,
                features=tuple((name, features[name]) for name in FEATURE_NAMES),
                target_log_seconds=str(math.log(30) + 0.1 + index / 1000),
            )
        )
    model, _, _ = _train_catboost_hierarchy(
        rows, iterations=40, depth=2, target_transform="event_history_residual_v1"
    )
    export_catboost_json(model, tmp_path / "native.json")
    reloaded = catboost.CatBoostRegressor()
    reloaded.load_model(str(tmp_path / "native.json"), format="json")
    features = rows[-1].feature_dict
    features["same_event_scaled_log_median"] = math.log(180)
    features["size_mm"] = 500
    ordered = [[features[name] for name in FEATURE_NAMES]]
    expected = tuple(float(value) + math.log(180) for value in model.predict(ordered)[0])
    assert predict_model_log_quantiles(reloaded, ordered) == pytest.approx(expected)
    assert math.exp(predict_model_log_quantiles(reloaded, ordered)[3]) > 180
