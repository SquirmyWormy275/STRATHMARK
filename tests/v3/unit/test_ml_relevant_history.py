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
