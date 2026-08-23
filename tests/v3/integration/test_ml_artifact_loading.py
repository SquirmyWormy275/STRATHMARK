from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from strathmark.v3.assessors.ml import MLAssessor, PITCalibrator, SpecialistGate
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket, TargetContext
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.factory.ml_artifacts import (
    BUNDLE_METADATA_SCHEMA,
    DEPENDENCY_SCHEMA,
    FEATURE_SCHEMA,
    VOCABULARY_SCHEMA,
    MLArtifactError,
    _activate_verified_models,
    _default_model_loader,
    _is_digest,
    _parse_catboost_json,
    _parse_json,
    _read_bounded,
    _reject_executable_shapes,
    _safe_path,
    _validate_bundle_metadata,
    _validate_dependency_lock,
    _validate_feature_schema,
    _validate_file_manifest,
    _validate_specialist_manifest,
    _validate_vocabulary,
    export_catboost_json,
    load_ml_bundle,
    write_ml_bundle,
)
from strathmark.v3.factory.ml_training import (
    CATEGORICAL_FEATURES,
    FEATURE_NAMES,
    CausalTrainingRow,
    SpecialistEligibility,
    _train_catboost_hierarchy,
)


def _catboost_json(offset: float = 0.0) -> bytes:
    categorical = [
        {"feature_id": name, "flat_feature_index": index}
        for index, name in enumerate(FEATURE_NAMES)
        if name in CATEGORICAL_FEATURES
    ]
    numeric = [
        {"feature_id": name, "flat_feature_index": index}
        for index, name in enumerate(FEATURE_NAMES)
        if name not in CATEGORICAL_FEATURES
    ]
    return json.dumps(
        {
            "features_info": {
                "categorical_features": categorical,
                "float_features": numeric,
            },
            "model_info": {
                "params": json.dumps(
                    {
                        "loss_function": {
                            "type": "MultiQuantile",
                            "params": {"alpha": "0.05,0.1,0.25,0.5,0.75,0.9,0.95"},
                        }
                    }
                ),
                "train_finish_time": "redacted",
            },
            "oblivious_trees": [],
            "scale_and_bias": [1, [offset] * 7],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _write(root: Path) -> Path:
    return write_ml_bundle(
        root,
        universal_model_json=_catboost_json(),
        specialist_model_json={"underhand|300|gum": _catboost_json(0.1)},
        gate=SpecialistGate(
            "0", (("log_history_depth", "1"), ("missing_fraction", "0"))
        ),
        calibrator=PITCalibrator.identity(source_digest="c" * 64),
        feature_schema={
            "schema_version": "strathmark-v3-ml-feature-schema-v1",
            "features": list(FEATURE_NAMES),
            "categorical": list(CATEGORICAL_FEATURES),
            "quantiles": ["0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"],
        },
        category_vocabulary={
            "schema_version": "strathmark-v3-ml-category-vocabulary-v1",
            "values": {
                "event_family": ["__other__", "underhand"],
                "species": ["__other__", "gum"],
            },
        },
        dependency_lock={
            "schema_version": "strathmark-v3-ml-dependency-lock-v1",
            "catboost_version": "1.2.8",
            "python_abi": "cp313",
        },
        bundle_metadata={
            "schema_version": BUNDLE_METADATA_SCHEMA,
            "code_revision": "test-revision",
            "training_snapshot_digest": "1" * 64,
            "role_manifest_digest": "2" * 64,
            "gate_oof_digest": "3" * 64,
            "calibrator_source_digest": "c" * 64,
            "taxonomy_version": "taxonomy:v1",
            "conversion_version": "conversion:v1",
        },
        bundle_version="ml:v1",
    )


def _rewrite_manifest(root: Path, mutation: object) -> None:
    manifest = json.loads((root / "manifest.json").read_text())
    mutation(manifest)  # type: ignore[operator]
    body = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    manifest["manifest_digest"] = canonical_digest(body)
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


class _Model:
    def __init__(self, path: Path):
        self.path = path


def test_json_bundle_round_trip_verifies_every_digest_before_model_loading(
    tmp_path: Path,
) -> None:
    root = _write(tmp_path / "bundle")
    loaded_paths: list[Path] = []

    def loader(path: Path) -> _Model:
        loaded_paths.append(path)
        return _Model(path)

    bundle = load_ml_bundle(
        root,
        installed_catboost_version="1.2.8",
        installed_python_abi="cp313",
        model_loader=loader,
    )
    assert bundle.universal_model.path.name == "model-0.json"
    assert set(bundle.specialist_models) == {"underhand|300|gum"}
    assert len(loaded_paths) == 2
    assert (
        bundle.digest
        == hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("corrupt", "digest"),
        ("wrong_schema", "schema"),
        ("dependency", "incompatible"),
        ("code", "prohibited"),
        ("oversized", "maximum"),
    ],
)
def test_unsafe_artifacts_fail_before_any_model_activation(
    tmp_path: Path, mutation: str, message: str
) -> None:
    root = _write(tmp_path / mutation)
    if mutation == "corrupt":
        (root / "universal.json").write_bytes(b"{}")
    elif mutation == "wrong_schema":
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["schema_version"] = "unknown"
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "dependency":
        pass
    elif mutation == "code":
        model = json.loads((root / "universal.json").read_text())
        model["python_callback"] = "__import__('os').system('whoami')"
        encoded = json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
        (root / "universal.json").write_bytes(encoded)
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["files"]["universal.json"] = {
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
        body = {
            key: value for key, value in manifest.items() if key != "manifest_digest"
        }
        manifest["manifest_digest"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        (root / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
    else:
        (root / "universal.json").write_bytes(b" " * 5_000_001)

    calls = 0

    def loader(_path: Path) -> object:
        nonlocal calls
        calls += 1
        return object()

    with pytest.raises(MLArtifactError, match=message):
        load_ml_bundle(
            root,
            installed_catboost_version="0" if mutation == "dependency" else "1.2.8",
            installed_python_abi="cp313",
            model_loader=loader,
        )
    assert calls == 0


def test_writer_refuses_executable_or_non_json_model_payloads(tmp_path: Path) -> None:
    with pytest.raises(MLArtifactError, match="JSON"):
        write_ml_bundle(
            tmp_path / "bad",
            universal_model_json=b"\x80\x04pickle",
            specialist_model_json={},
            gate=SpecialistGate(
                "0", (("log_history_depth", "0"), ("missing_fraction", "0"))
            ),
            calibrator=PITCalibrator.identity(source_digest="d" * 64),
            feature_schema={
                "schema_version": "strathmark-v3-ml-feature-schema-v1",
                "features": [],
                "categorical": [],
                "quantiles": [],
            },
            category_vocabulary={
                "schema_version": "strathmark-v3-ml-category-vocabulary-v1",
                "values": {},
            },
            dependency_lock={
                "schema_version": "strathmark-v3-ml-dependency-lock-v1",
                "catboost_version": "1.2.8",
                "python_abi": "cp313",
            },
            bundle_metadata={
                "schema_version": BUNDLE_METADATA_SCHEMA,
                "code_revision": "test-revision",
                "training_snapshot_digest": "1" * 64,
                "role_manifest_digest": "2" * 64,
                "gate_oof_digest": "3" * 64,
                "calibrator_source_digest": "d" * 64,
                "taxonomy_version": "taxonomy:v1",
                "conversion_version": "conversion:v1",
            },
            bundle_version="ml:v1",
        )


def test_writer_rejects_invalid_models_eligibility_bounds_and_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = {
        "gate": SpecialistGate(
            "0", (("log_history_depth", "0"), ("missing_fraction", "0"))
        ),
        "calibrator": PITCalibrator.identity(source_digest="d" * 64),
        "feature_schema": {
            "schema_version": FEATURE_SCHEMA,
            "features": list(FEATURE_NAMES),
            "categorical": list(CATEGORICAL_FEATURES),
            "quantiles": ["0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"],
        },
        "category_vocabulary": {
            "schema_version": VOCABULARY_SCHEMA,
            "values": {
                "event_family": ["__other__"],
                "species": ["__other__"],
            },
        },
        "dependency_lock": {
            "schema_version": DEPENDENCY_SCHEMA,
            "catboost_version": "1.2.8",
            "python_abi": "cp313",
        },
        "bundle_metadata": {
            "schema_version": BUNDLE_METADATA_SCHEMA,
            "code_revision": "test-revision",
            "training_snapshot_digest": "1" * 64,
            "role_manifest_digest": "2" * 64,
            "gate_oof_digest": "3" * 64,
            "calibrator_source_digest": "d" * 64,
            "taxonomy_version": "taxonomy:v1",
            "conversion_version": "conversion:v1",
        },
        "bundle_version": "ml:v1",
    }
    with pytest.raises(MLArtifactError, match="documented JSON bytes"):
        write_ml_bundle(
            tmp_path / "type",
            universal_model_json={},
            specialist_model_json={},
            **common,
        )  # type: ignore[arg-type]
    with pytest.raises(MLArtifactError, match="bounded key"):
        write_ml_bundle(
            tmp_path / "key",
            universal_model_json=_catboost_json(),
            specialist_model_json={"": _catboost_json()},
            **common,
        )
    with pytest.raises(MLArtifactError, match="exactly cover"):
        write_ml_bundle(
            tmp_path / "coverage",
            universal_model_json=_catboost_json(),
            specialist_model_json={"key": _catboost_json()},
            specialist_eligibility={},
            **common,
        )
    with pytest.raises(MLArtifactError, match="ineligible"):
        write_ml_bundle(
            tmp_path / "eligible",
            universal_model_json=_catboost_json(),
            specialist_model_json={"key": _catboost_json()},
            specialist_eligibility={"key": SpecialistEligibility(499, 30, 10)},
            **common,
        )
    existing = write_ml_bundle(
        tmp_path / "existing",
        universal_model_json=_catboost_json(),
        specialist_model_json={},
        **common,
    )
    with pytest.raises(FileExistsError):
        write_ml_bundle(
            existing,
            universal_model_json=_catboost_json(),
            specialist_model_json={},
            **common,
        )
    monkeypatch.setattr("strathmark.v3.factory.ml_artifacts.MAX_BUNDLE_BYTES", 1)
    with pytest.raises(MLArtifactError, match="maximum"):
        write_ml_bundle(
            tmp_path / "large",
            universal_model_json=_catboost_json(),
            specialist_model_json={},
            **common,
        )


def test_loader_rejects_missing_extra_and_activation_failure(tmp_path: Path) -> None:
    with pytest.raises(MLArtifactError, match="missing"):
        load_ml_bundle(
            tmp_path / "missing",
            installed_catboost_version="1.2.8",
            installed_python_abi="cp313",
        )


def test_loader_covers_manifest_scope_bounds_and_wrapped_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write(tmp_path / "closed")
    (root / "manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(MLArtifactError, match="closed schema"):
        load_ml_bundle(
            root, installed_catboost_version="1.2.8", installed_python_abi="cp313"
        )

    root = _write(tmp_path / "schema")
    _rewrite_manifest(root, lambda value: value.__setitem__("schema_version", "old"))
    with pytest.raises(MLArtifactError, match="schema"):
        load_ml_bundle(
            root, installed_catboost_version="1.2.8", installed_python_abi="cp313"
        )

    root = _write(tmp_path / "digest")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["manifest_digest"] = "0" * 64
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(MLArtifactError, match="manifest digest"):
        load_ml_bundle(
            root, installed_catboost_version="1.2.8", installed_python_abi="cp313"
        )

    root = _write(tmp_path / "coverage")
    _rewrite_manifest(root, lambda value: value["files"].pop("gate.json"))
    with pytest.raises(MLArtifactError, match="file coverage"):
        load_ml_bundle(
            root, installed_catboost_version="1.2.8", installed_python_abi="cp313"
        )

    root = _write(tmp_path / "total")
    monkeypatch.setattr("strathmark.v3.factory.ml_artifacts.MAX_BUNDLE_BYTES", 1)
    with pytest.raises(MLArtifactError, match="maximum safe byte"):
        load_ml_bundle(
            root, installed_catboost_version="1.2.8", installed_python_abi="cp313"
        )
    monkeypatch.setattr(
        "strathmark.v3.factory.ml_artifacts.MAX_BUNDLE_BYTES", 50_000_000
    )

    root = _write(tmp_path / "wrapped")
    _rewrite_manifest(
        root,
        lambda value: value["specialists"]["underhand|300|gum"][
            "eligibility"
        ].__setitem__("admitted_rows", -1),
    )
    with pytest.raises(MLArtifactError, match="invalid before activation"):
        load_ml_bundle(
            root, installed_catboost_version="1.2.8", installed_python_abi="cp313"
        )


def test_loaded_bundle_missing_feature_and_default_catboost_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = load_ml_bundle(
        _write(tmp_path / "normalize"),
        installed_catboost_version="1.2.8",
        installed_python_abi="cp313",
        model_loader=lambda path: object(),
    )
    with pytest.raises(MLArtifactError, match="missing schema fields"):
        bundle.normalize_features({})

    class CatBoostRegressor:
        def load_model(self, path: str, *, format: str) -> None:
            self.loaded = (path, format)

    monkeypatch.setitem(
        sys.modules, "catboost", SimpleNamespace(CatBoostRegressor=CatBoostRegressor)
    )
    path = tmp_path / "model.json"
    path.write_bytes(_catboost_json())
    model = _default_model_loader(path)
    assert model.loaded == (str(path), "json")
    root = _write(tmp_path / "extra")
    (root / "payload.joblib").write_bytes(b"unsafe")
    with pytest.raises(MLArtifactError, match="prohibited extra"):
        load_ml_bundle(
            root,
            installed_catboost_version="1.2.8",
            installed_python_abi="cp313",
            model_loader=lambda path: object(),
        )
    root = _write(tmp_path / "activation")
    with pytest.raises(MLArtifactError, match="activation"):
        load_ml_bundle(
            root,
            installed_catboost_version="1.2.8",
            installed_python_abi="cp313",
            model_loader=lambda path: (_ for _ in ()).throw(RuntimeError("boom")),
        )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {
            "schema_version": "old",
            "features": ["a"],
            "categorical": [],
            "quantiles": [],
        },
        {
            "schema_version": FEATURE_SCHEMA,
            "features": [],
            "categorical": [],
            "quantiles": [],
        },
        {
            "schema_version": FEATURE_SCHEMA,
            "features": ["a", "a"],
            "categorical": [],
            "quantiles": ["0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"],
        },
        {
            "schema_version": FEATURE_SCHEMA,
            "features": ["a"],
            "categorical": ["b"],
            "quantiles": ["0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"],
        },
    ],
)
def test_feature_schema_validator_rejects_every_incompatible_shape(
    value: dict[str, object],
) -> None:
    with pytest.raises(MLArtifactError, match="schema|incompatible"):
        _validate_feature_schema(value)


def test_vocabulary_dependency_and_manifest_validators_fail_closed() -> None:
    schema = {
        "schema_version": FEATURE_SCHEMA,
        "features": ["category"],
        "categorical": ["category"],
        "quantiles": ["0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"],
    }
    for value in (
        {},
        {"schema_version": "old", "values": {}},
        {"schema_version": VOCABULARY_SCHEMA, "values": {}},
        {"schema_version": VOCABULARY_SCHEMA, "values": {"category": []}},
        {
            "schema_version": VOCABULARY_SCHEMA,
            "values": {"category": ["z", "__other__"]},
        },
    ):
        with pytest.raises(MLArtifactError, match="vocabulary"):
            _validate_vocabulary(value, schema)
    for value in (
        {},
        {"schema_version": "old", "catboost_version": "1", "python_abi": "x"},
        {
            "schema_version": DEPENDENCY_SCHEMA,
            "catboost_version": "",
            "python_abi": "x",
        },
    ):
        with pytest.raises(MLArtifactError, match="dependency"):
            _validate_dependency_lock(value)
    for value in (
        {},
        [],
        {"model.pkl": {"bytes": 1, "sha256": "a" * 64}},
        {"a.json": {}},
    ):
        with pytest.raises(MLArtifactError, match="file manifest"):
            _validate_file_manifest(value)
    assert _is_digest("a" * 64)
    assert not _is_digest("A" * 64)
    assert not _is_digest(1)


def test_specialist_and_catboost_json_validators_fail_closed() -> None:
    for value in (
        [],
        {1: {}},
        {"key": {"path": "a.json", "eligibility": {}}},
        {
            "key": {
                "path": "a.json",
                "eligibility": {
                    "admitted_rows": 499,
                    "competitors": 30,
                    "tournaments": 10,
                    "available": False,
                },
            }
        },
    ):
        with pytest.raises(MLArtifactError, match="specialist"):
            _validate_specialist_manifest(value)
    for value in (
        b"[]",
        json.dumps(
            {
                "features_info": [],
                "model_info": {},
                "oblivious_trees": [],
                "scale_and_bias": [],
            }
        ).encode(),
        json.dumps(
            {
                "features_info": {},
                "model_info": {},
                "oblivious_trees": {},
                "scale_and_bias": [],
            }
        ).encode(),
    ):
        with pytest.raises(MLArtifactError, match="schema"):
            _parse_catboost_json(value)
    with pytest.raises(MLArtifactError, match="depth"):
        nested: object = None
        for _ in range(40):
            nested = [nested]
        _reject_executable_shapes(nested)
    with pytest.raises(MLArtifactError, match="prohibited value"):
        _reject_executable_shapes(object())
    with pytest.raises(MLArtifactError, match="valid inert JSON"):
        _parse_json(b"\xff", "payload")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["features_info"]["float_features"][0].__setitem__(
                "feature_id", "competitor_id"
            ),
            "feature order",
        ),
        (
            lambda value: value["model_info"].__setitem__(
                "params", {"loss_function": {"type": "RMSE", "params": {}}}
            ),
            "MultiQuantile",
        ),
        (lambda value: value.__setitem__("scale_and_bias", [1, [0]]), "seven"),
    ],
)
def test_catboost_contract_rejects_identity_wrong_objective_and_one_output(
    mutation: object, message: str
) -> None:
    value = json.loads(_catboost_json())
    mutation(value)  # type: ignore[operator]
    with pytest.raises(MLArtifactError, match=message):
        _parse_catboost_json(json.dumps(value).encode())


def test_activation_detects_loader_mutation_of_verified_private_copy(
    tmp_path: Path,
) -> None:
    root = _write(tmp_path / "mutation")

    def mutating_loader(path: Path) -> object:
        path.write_bytes(b"{}")
        return object()

    with pytest.raises(MLArtifactError, match="changed during load"):
        load_ml_bundle(
            root,
            installed_catboost_version="1.2.8",
            installed_python_abi="cp313",
            model_loader=mutating_loader,
        )


def test_activation_detects_private_copy_mismatch_before_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read_bytes = Path.read_bytes

    def mismatching_read_bytes(path: Path) -> bytes:
        if path.name == "model-0.json":
            return b"changed"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", mismatching_read_bytes)
    with pytest.raises(MLArtifactError, match="differs before load"):
        _activate_verified_models(
            {"universal.json": b"{}"}, {}, loader=lambda path: object()
        )


def test_bundle_metadata_validator_rejects_each_closed_contract_breach() -> None:
    calibrator = PITCalibrator.identity(source_digest="d" * 64)
    valid: dict[str, object] = {
        "schema_version": BUNDLE_METADATA_SCHEMA,
        "code_revision": "revision",
        "training_snapshot_digest": "1" * 64,
        "role_manifest_digest": "2" * 64,
        "gate_oof_digest": "3" * 64,
        "calibrator_source_digest": calibrator.source_digest,
        "taxonomy_version": "taxonomy:v1",
        "conversion_version": "conversion:v1",
    }
    mutations = (
        lambda value: value.pop("code_revision"),
        lambda value: value.__setitem__("schema_version", "old"),
        lambda value: value.__setitem__("gate_oof_digest", "bad"),
        lambda value: value.__setitem__("calibrator_source_digest", "e" * 64),
        lambda value: value.__setitem__("code_revision", ""),
    )
    for mutation in mutations:
        candidate = dict(valid)
        mutation(candidate)
        with pytest.raises(MLArtifactError, match="metadata"):
            _validate_bundle_metadata(candidate, calibrator)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["features_info"].__setitem__("float_features", {}),
            "feature lists",
        ),
        (
            lambda value: value["features_info"]["float_features"][0].pop(
                "flat_feature_index"
            ),
            "feature identity",
        ),
        (lambda value: value["model_info"].pop("params"), "parameters are missing"),
        (
            lambda value: value["model_info"].__setitem__(
                "params",
                {
                    "loss_function": {
                        "type": "MultiQuantile",
                        "params": {"alpha": "0.5"},
                    }
                },
            ),
            "levels",
        ),
        (
            lambda value: value.__setitem__(
                "oblivious_trees", [{"leaf_values": [0.0]}]
            ),
            "seven outputs",
        ),
    ],
)
def test_catboost_parser_rejects_remaining_schema_and_dimension_breaches(
    mutation: object, message: str
) -> None:
    value = json.loads(_catboost_json())
    mutation(value)  # type: ignore[operator]
    with pytest.raises(MLArtifactError, match=message):
        _parse_catboost_json(json.dumps(value).encode())


def test_bounded_path_and_export_helpers(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(MLArtifactError, match="missing"):
        _read_bounded(missing, 1, "payload")
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(MLArtifactError, match="maximum"):
        _read_bounded(empty, 1, "payload")
    for relative in ("", "..\\escape.json", "../escape.json", "a/../inside.json"):
        with pytest.raises(MLArtifactError, match="prohibited|escapes"):
            _safe_path(tmp_path, relative)

    class Model:
        def save_model(self, path: str, *, format: str) -> None:
            assert format == "json"
            Path(path).write_bytes(_catboost_json())

    destination = tmp_path / "export.json"
    assert export_catboost_json(Model(), destination) == _catboost_json()


def test_real_catboost_train_export_verify_load_and_predict(tmp_path: Path) -> None:
    catboost = pytest.importorskip("catboost")
    rows: list[CausalTrainingRow] = []
    for index in range(40):
        features: dict[str, object] = {
            "event_family": "underhand",
            "species": "gum",
            "size_mm": 300,
            "density": 720.0,
            "density_missing": 0,
            "history_depth": index,
            "exact_history_depth": index,
            "history_log_median": 3.5 + index / 1000,
            "history_log_spread": 0.1,
            "history_missing": 0,
            "sequence_recency": 0,
            "history_log_trend": 0.001,
            "context_distance": 0.0,
            "eligible_tournament_sequence": index,
            "current_form_log_seconds": 3.5 + index / 1000,
        }
        rows.append(
            CausalTrainingRow(
                f"evidence:real-{index}",
                f"competitor:c{index % 10}",
                f"tournament:t{index % 5}",
                "2026-01-02T00:00:00.000Z",
                index + 1,
                "underhand|300|gum",
                tuple((name, features[name]) for name in FEATURE_NAMES),
                str(3.5 + index / 1000).rstrip("0").rstrip("."),
                "a" * 64,
                index,
                "2026-01-01T00:00:00.000Z" if index else "0001-01-01T00:00:00.000Z",
                f"field:real-{index}",
                "taxonomy:v1",
                "conversion:v1",
            )
        )
    universal, specialists, _eligibility = _train_catboost_hierarchy(
        tuple(rows),
        iterations=4,
        depth=2,
    )
    assert specialists == {}
    raw = export_catboost_json(universal, tmp_path / "real.json")
    calibrator = PITCalibrator.identity(source_digest="e" * 64)
    root = write_ml_bundle(
        tmp_path / "real-bundle",
        universal_model_json=raw,
        specialist_model_json={},
        gate=SpecialistGate(
            "0", (("log_history_depth", "0"), ("missing_fraction", "0"))
        ),
        calibrator=calibrator,
        feature_schema={
            "schema_version": FEATURE_SCHEMA,
            "features": list(FEATURE_NAMES),
            "categorical": list(CATEGORICAL_FEATURES),
            "quantiles": ["0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"],
        },
        category_vocabulary={
            "schema_version": VOCABULARY_SCHEMA,
            "values": {
                "event_family": ["__other__", "underhand"],
                "species": ["__other__", "gum"],
            },
        },
        dependency_lock={
            "schema_version": DEPENDENCY_SCHEMA,
            "catboost_version": catboost.__version__,
            "python_abi": "cp313",
        },
        bundle_metadata={
            "schema_version": BUNDLE_METADATA_SCHEMA,
            "code_revision": "real-test",
            "training_snapshot_digest": "1" * 64,
            "role_manifest_digest": "2" * 64,
            "gate_oof_digest": "3" * 64,
            "calibrator_source_digest": calibrator.source_digest,
            "taxonomy_version": "taxonomy:v1",
            "conversion_version": "conversion:v1",
        },
        bundle_version="ml:real-test",
    )
    bundle = load_ml_bundle(
        root,
        installed_catboost_version=catboost.__version__,
        installed_python_abi="cp313",
    )
    context = TargetContext("underhand", 300, "gum", "taxonomy:v1", "conversion:v1", ())
    packet = EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:real"),
        target_context=context,
        observations=(),
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:2026-01-01",
        tournament_epoch_id=StableIdentifier("epoch:real"),
        tournament_event_sequence=0,
    )
    result = MLAssessor(bundle).assess(packet)
    assert result.forecast.distribution is not None
    assert result.forecast.distribution.median_ms > 0
