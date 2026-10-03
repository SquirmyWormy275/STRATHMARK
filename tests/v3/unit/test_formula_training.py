import json
import math
from types import SimpleNamespace

import pytest

from strathmark.v3.factory.formula_training import (
    _build_formula_prior_values,
    build_formula_candidate,
)
from strathmark.v3.factory.ml_training import MLDataRole
from strathmark.v3.linux_forecasts import load_formula_manifest
from tests.v3.property.test_ml_temporal_features import _authorized_rows, _observation, _packet


def test_training_priors_preserve_physical_units_and_bind_training_lineage():
    rows = tuple(
        SimpleNamespace(
            row_id=f"evidence:synthetic-{i}",
            target_log_seconds=str(math.log(seconds)),
            feature_dict={"event_family": "underhand", "species": "gum", "size_mm": size},
        )
        for i, (size, seconds) in enumerate(((300, 100), (600, 400)))
    )
    manifest = _build_formula_prior_values(rows, "a" * 64)
    prior = next(p for p in manifest.context_priors if p.size_mm == 450)
    assert float(prior.median_seconds) == pytest.approx(225)
    assert float(prior.log_variance) == 0.04
    changed = _build_formula_prior_values(rows, "b" * 64)
    assert changed.digest != manifest.digest
    assert manifest.huber_tuning == "1.5" and manifest.prior_pseudo_count == 3


def test_formula_factory_rejects_tuning_rows_and_unsigned_rows():
    packet = _packet((_observation(1, 30000, day=1),))
    train = _authorized_rows((packet,))
    assert build_formula_candidate(train.authority, train.rows).context_priors
    tuning = _authorized_rows((packet,), MLDataRole.TUNING)
    with pytest.raises(ValueError):
        build_formula_candidate(tuning.authority, tuning.rows)
    with pytest.raises(ValueError):
        build_formula_candidate(train.authority, tuple(train.rows))


def test_optional_formula_manifest_is_bounded_and_never_silently_replaced(tmp_path):
    bundle = tmp_path / "model"
    bundle.mkdir()
    assert load_formula_manifest(bundle).version == "formula:v2-bootstrap"
    rows = (
        SimpleNamespace(
            row_id="evidence:synthetic",
            target_log_seconds=str(math.log(100)),
            feature_dict={"event_family": "underhand", "species": "gum", "size_mm": 300},
        ),
    )
    manifest = _build_formula_prior_values(rows, "a" * 64)
    path = tmp_path / "formula_manifest.json"
    path.write_text(json.dumps(manifest.to_dict()))
    assert load_formula_manifest(bundle).digest == manifest.digest
    path.write_text("x" * 1_000_001)
    with pytest.raises(ValueError, match="bounded"):
        load_formula_manifest(bundle)
    path.write_text("{}")
    with pytest.raises(ValueError):
        load_formula_manifest(bundle)
