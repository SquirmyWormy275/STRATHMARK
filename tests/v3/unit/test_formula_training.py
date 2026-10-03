import json
import math
from types import SimpleNamespace

import pytest

from strathmark.v3.factory.formula_training import (
    _build_formula_prior_values,
    build_formula_candidate,
    select_formula_candidate,
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


def test_formula_selection_uses_authenticated_tuning_and_exact_packet_lineage():
    from strathmark.v3.factory.ml_training import (
        MLAuthorityEnvironment,
        _compose_ml_candidate_authority,
    )

    # The public development builder preserves earlier-role context; tests use
    # a separately composed development authority with only synthetic facts.
    from strathmark.v3.infrastructure.integrity import P256EphemeralSigner, sign_manifest

    signer = P256EphemeralSigner.generate("formula-tuning-synthetic")
    signed = sign_manifest(
        "ml_role_manifest",
        {
            "schema_version": "strathmark-v3-ml-role-manifest-v3",
            "generation_digest": "a" * 64,
            "assignments": [
                {"tournament_id": "tournament:train", "role": "training"},
                {"tournament_id": "tournament:tune", "role": "tuning"},
                {"tournament_id": "tournament:cal", "role": "calibration"},
            ],
        },
        signer=signer,
        created_at="2026-01-01T00:00:00.000Z",
    )
    authority = _compose_ml_candidate_authority(
        signed, signer.identity, signer, environment=MLAuthorityEnvironment.DEVELOPMENT_CANDIDATE
    )
    train_obs = _observation(1, 30000, day=1, tournament="train")
    tune_obs = _observation(2, 150000, day=2, tournament="tune")
    training = authority.build_development_causal_rows(
        MLDataRole.TRAINING, (_packet((train_obs,)),)
    )
    packet = _packet((train_obs, tune_obs))
    tuning = authority.build_development_causal_rows(MLDataRole.TUNING, (packet,))
    manifest, selected, trials = select_formula_candidate(authority, training, tuning, (packet,))
    assert manifest.version == "formula:v3-tuned-priors-v1"
    assert len(trials) == 8 and selected["row_count"] == 1
    assert selected["manifest_digest"] == manifest.digest
    with pytest.raises(ValueError):
        select_formula_candidate(authority, tuple(training), tuning, (packet,))
    with pytest.raises(ValueError, match="source rows"):
        select_formula_candidate(authority, training, tuning, (_packet((tune_obs,)),))
    cal = authority.build_development_causal_rows(
        MLDataRole.CALIBRATION,
        (_packet((train_obs, tune_obs, _observation(3, 160000, day=3, tournament="cal"))),),
    )
    with pytest.raises(ValueError):
        select_formula_candidate(authority, training, cal, (packet,))


def test_weak_prior_candidate_responds_to_consistent_slow_history():
    from strathmark.v3.assessors.formula import FormulaManifest, assess_formula
    from strathmark.v3.contracts.canonical import canonical_digest
    from tests.v3.unit.test_formula import evidence, formula_input, observation

    manifest = FormulaManifest.load("strathmark/v3/contracts/formula_manifest.json")
    packet = formula_input(evidence(observation(1, 180000), observation(2, 200000)))
    baseline = assess_formula(packet, manifest)
    value = manifest.to_dict()
    value["version"] = "formula:v3-tuned-priors-v1"
    value["robust_center"]["minimum_scale"] = "0.8"
    for prior in value["context_priors"] + value["discipline_priors"]:
        prior["pseudo_count"] = 1
    value.pop("digest")
    value["digest"] = canonical_digest(value)
    improved = assess_formula(packet, FormulaManifest.from_dict(value))
    assert improved.center_ms > baseline.center_ms * 2
    assert 90000 < improved.center_ms < 200000
    assert manifest.version == "formula:v2-bootstrap" and manifest.prior_pseudo_count == 3
