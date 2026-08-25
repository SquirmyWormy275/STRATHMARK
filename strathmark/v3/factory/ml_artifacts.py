"""Bounded JSON-only persistence and activation for V3 ML bundles."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from strathmark.v3.assessors.ml import PITCalibrator, SpecialistGate
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.factory.ml_training import (
    CATEGORICAL_FEATURES,
    FEATURE_NAMES,
    SpecialistEligibility,
)

MANIFEST_SCHEMA = "strathmark-v3-ml-bundle-manifest-v1"
FEATURE_SCHEMA = "strathmark-v3-ml-feature-schema-v1"
VOCABULARY_SCHEMA = "strathmark-v3-ml-category-vocabulary-v1"
DEPENDENCY_SCHEMA = "strathmark-v3-ml-dependency-lock-v1"
BUNDLE_METADATA_SCHEMA = "strathmark-v3-ml-bundle-metadata-v1"
MAX_MANIFEST_BYTES = 1_000_000
MAX_JSON_FILE_BYTES = 5_000_000
MAX_BUNDLE_BYTES = 50_000_000
MAX_JSON_DEPTH = 32
_MODEL_REQUIRED = {"features_info", "model_info", "oblivious_trees", "scale_and_bias"}
_MODEL_OPTIONAL = {"ctr_data"}
_PROHIBITED_FRAGMENTS = (
    "callback",
    "pickle",
    "joblib",
    "__reduce__",
    "__import__",
    "executable",
    "python_code",
)


class MLArtifactError(ValueError):
    """A candidate bundle failed bounded verification before activation."""


@dataclass(frozen=True, slots=True)
class LoadedMLBundle:
    digest: str
    version: str
    universal_model: Any
    specialist_models: Mapping[str, Any]
    specialist_eligibility: Mapping[str, SpecialistEligibility]
    gate: SpecialistGate
    calibrator: PITCalibrator
    feature_names: tuple[str, ...]
    categorical_features: tuple[str, ...]
    vocabulary: Mapping[str, tuple[str, ...]]
    dependency_lock: Mapping[str, str]
    metadata: Mapping[str, str]

    @classmethod
    def for_testing(
        cls,
        *,
        digest: str,
        version: str,
        universal_model: Any,
        specialist_models: Mapping[str, Any],
        specialist_eligibility: Mapping[str, SpecialistEligibility],
        gate: SpecialistGate,
        calibrator: PITCalibrator,
        feature_names: tuple[str, ...],
        categorical_features: tuple[str, ...],
        vocabulary: Mapping[str, tuple[str, ...]],
        taxonomy_version: str,
        conversion_version: str,
    ) -> LoadedMLBundle:
        return cls(
            digest,
            version,
            universal_model,
            dict(specialist_models),
            dict(specialist_eligibility),
            gate,
            calibrator,
            feature_names,
            categorical_features,
            dict(vocabulary),
            {"catboost_version": "test", "python_abi": "test"},
            {
                "schema_version": BUNDLE_METADATA_SCHEMA,
                "code_revision": "test",
                "training_snapshot_digest": "0" * 64,
                "role_manifest_digest": "1" * 64,
                "gate_oof_digest": "2" * 64,
                "calibrator_source_digest": calibrator.source_digest,
                "taxonomy_version": taxonomy_version,
                "conversion_version": conversion_version,
            },
        )

    def normalize_features(
        self, features: Mapping[str, object]
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        missing = set(self.feature_names) - set(features)
        if missing:
            raise MLArtifactError(f"inference features missing schema fields: {sorted(missing)}")
        normalized = {name: features[name] for name in self.feature_names}
        unseen: list[str] = []
        for name in self.categorical_features:
            value = str(normalized[name])
            vocabulary = self.vocabulary[name]
            if value not in vocabulary:
                value = "__other__"
                unseen.append(name)
            normalized[name] = value
        return normalized, tuple(sorted(unseen))


def write_ml_bundle(
    root: str | Path,
    *,
    universal_model_json: bytes,
    specialist_model_json: Mapping[str, bytes],
    gate: SpecialistGate,
    calibrator: PITCalibrator,
    feature_schema: Mapping[str, Any],
    category_vocabulary: Mapping[str, Any],
    dependency_lock: Mapping[str, Any],
    bundle_metadata: Mapping[str, Any],
    bundle_version: str,
    specialist_eligibility: Mapping[str, SpecialistEligibility] | None = None,
) -> Path:
    """Create one immutable bundle after validating every proposed JSON payload."""

    if not isinstance(universal_model_json, bytes):
        raise MLArtifactError("CatBoost model must be documented JSON bytes")
    models: dict[str, tuple[str, bytes]] = {}
    _parse_catboost_json(universal_model_json)
    for key, raw in sorted(specialist_model_json.items()):
        if not isinstance(key, str) or not key or not isinstance(raw, bytes):
            raise MLArtifactError("specialist models require bounded key and JSON bytes")
        _parse_catboost_json(raw)
        filename = f"specialists/{canonical_digest({'specialist_key': key})}.json"
        models[key] = (filename, raw)
    gate_value = gate.to_dict()
    calibrator_value = calibrator.to_dict()
    schema = _validate_feature_schema(feature_schema)
    vocabulary = _validate_vocabulary(category_vocabulary, schema)
    dependency = _validate_dependency_lock(dependency_lock)
    metadata = _validate_bundle_metadata(bundle_metadata, calibrator)
    eligibility = (
        {key: SpecialistEligibility(500, 30, 10) for key in models}
        if specialist_eligibility is None
        else specialist_eligibility
    )
    if set(eligibility) != set(models):
        raise MLArtifactError("specialist eligibility must exactly cover specialist models")
    if any(not item.available for item in eligibility.values()):
        raise MLArtifactError("ineligible specialist models cannot enter a trusted bundle")
    payloads: dict[str, bytes] = {
        "universal.json": universal_model_json,
        "gate.json": canonical_bytes(gate_value),
        "calibrator.json": canonical_bytes(calibrator_value),
        "feature_schema.json": canonical_bytes(schema),
        "category_vocabulary.json": canonical_bytes(vocabulary),
        "dependency_lock.json": canonical_bytes(dependency),
        "bundle_metadata.json": canonical_bytes(metadata),
    }
    payloads.update({filename: raw for filename, raw in models.values()})
    total = sum(len(raw) for raw in payloads.values())
    if total > MAX_BUNDLE_BYTES or any(len(raw) > MAX_JSON_FILE_BYTES for raw in payloads.values()):
        raise MLArtifactError("ML bundle exceeds the maximum safe byte bounds")
    body = {
        "schema_version": MANIFEST_SCHEMA,
        "bundle_version": bundle_version,
        "universal_model": "universal.json",
        "specialists": {
            key: {
                "path": models[key][0],
                "eligibility": eligibility[key].to_dict(),
            }
            for key in sorted(models)
        },
        "files": {
            name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in sorted(payloads.items())
        },
    }
    manifest = {**body, "manifest_digest": canonical_digest(body)}
    manifest_bytes = canonical_bytes(manifest, max_bytes=MAX_MANIFEST_BYTES)
    destination = Path(root)
    if destination.exists():
        raise FileExistsError("refusing to overwrite an ML artifact bundle")
    (destination / "specialists").mkdir(parents=True, exist_ok=False)
    for relative, raw in payloads.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (destination / "manifest.json").write_bytes(manifest_bytes)
    return destination


def load_ml_bundle(
    root: str | Path,
    *,
    installed_catboost_version: str,
    installed_python_abi: str,
    model_loader: Callable[[Path], Any] | None = None,
) -> LoadedMLBundle:
    """Verify the complete inert bundle, then and only then instantiate models."""

    directory = Path(root)
    manifest_path = directory / "manifest.json"
    try:
        if not directory.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
            raise MLArtifactError("ML bundle manifest is missing")
        raw_manifest = _read_bounded(manifest_path, MAX_MANIFEST_BYTES, "manifest")
        manifest = _parse_json(raw_manifest, "manifest")
        expected_manifest = {
            "schema_version",
            "bundle_version",
            "universal_model",
            "specialists",
            "files",
            "manifest_digest",
        }
        if not isinstance(manifest, dict) or set(manifest) != expected_manifest:
            raise MLArtifactError("ML manifest fields do not match the closed schema")
        if manifest["schema_version"] != MANIFEST_SCHEMA:
            raise MLArtifactError("ML manifest schema is unsupported")
        body = {key: value for key, value in manifest.items() if key != "manifest_digest"}
        if manifest["manifest_digest"] != canonical_digest(body):
            raise MLArtifactError("ML manifest digest mismatch")
        files = _validate_file_manifest(manifest["files"])
        specialists = _validate_specialist_manifest(manifest["specialists"])
        expected_paths = {
            "universal.json",
            "gate.json",
            "calibrator.json",
            "feature_schema.json",
            "category_vocabulary.json",
            "dependency_lock.json",
            "bundle_metadata.json",
            *(item[0] for item in specialists.values()),
        }
        if manifest["universal_model"] != "universal.json" or set(files) != expected_paths:
            raise MLArtifactError("ML manifest file coverage does not match the closed schema")
        actual_paths = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        }
        if actual_paths != expected_paths:
            raise MLArtifactError("ML bundle contains missing or prohibited extra files")
        verified: dict[str, bytes] = {}
        total = 0
        for relative, identity in sorted(files.items()):
            path = _safe_path(directory, relative)
            raw = _read_bounded(path, MAX_JSON_FILE_BYTES, relative)
            total += len(raw)
            if total > MAX_BUNDLE_BYTES:
                raise MLArtifactError("ML bundle exceeds the maximum safe byte bound")
            if len(raw) != identity[0] or hashlib.sha256(raw).hexdigest() != identity[1]:
                raise MLArtifactError(f"ML artifact digest or size mismatch: {relative}")
            verified[relative] = raw
        _parse_catboost_json(verified["universal.json"])
        for path, _eligibility in specialists.values():
            _parse_catboost_json(verified[path])
        gate = SpecialistGate.from_dict(_parse_json(verified["gate.json"], "gate"))
        calibrator = PITCalibrator.from_dict(_parse_json(verified["calibrator.json"], "calibrator"))
        schema = _validate_feature_schema(
            _parse_json(verified["feature_schema.json"], "feature schema")
        )
        vocabulary_value = _validate_vocabulary(
            _parse_json(verified["category_vocabulary.json"], "category vocabulary"),
            schema,
        )
        dependency = _validate_dependency_lock(
            _parse_json(verified["dependency_lock.json"], "dependency lock")
        )
        metadata = _validate_bundle_metadata(
            _parse_json(verified["bundle_metadata.json"], "bundle metadata"), calibrator
        )
        if (
            dependency["catboost_version"] != installed_catboost_version
            or dependency["python_abi"] != installed_python_abi
        ):
            raise MLArtifactError("ML artifact dependency lock is incompatible")
    except MLArtifactError:
        raise
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise MLArtifactError("ML artifact is invalid before activation") from exc

    loader = model_loader or _default_model_loader
    universal, specialist_models = _activate_verified_models(verified, specialists, loader)
    eligibility_map = {key: value[1] for key, value in specialists.items()}
    vocabulary = {name: tuple(values) for name, values in vocabulary_value["values"].items()}
    return LoadedMLBundle(
        digest=hashlib.sha256(raw_manifest).hexdigest(),
        version=manifest["bundle_version"],
        universal_model=universal,
        specialist_models=specialist_models,
        specialist_eligibility=eligibility_map,
        gate=gate,
        calibrator=calibrator,
        feature_names=tuple(schema["features"]),
        categorical_features=tuple(schema["categorical"]),
        vocabulary=vocabulary,
        dependency_lock=dependency,
        metadata=metadata,
    )


def export_catboost_json(model: Any, path: str | Path) -> bytes:
    """Export through CatBoost's documented JSON format and validate the result."""

    destination = Path(path)
    model.save_model(str(destination), format="json")
    raw = _read_bounded(destination, MAX_JSON_FILE_BYTES, "CatBoost JSON")
    _parse_catboost_json(raw)
    return raw


def _default_model_loader(path: Path) -> Any:
    from catboost import CatBoostRegressor

    model = CatBoostRegressor()
    model.load_model(str(path), format="json")
    return model


def _activate_verified_models(
    verified: Mapping[str, bytes],
    specialists: Mapping[str, tuple[str, SpecialistEligibility]],
    loader: Callable[[Path], Any],
) -> tuple[Any, dict[str, Any]]:
    """Activate only private copies of the exact bytes verified by the bundle gate."""

    model_paths = ("universal.json", *(item[0] for item in specialists.values()))
    try:
        with tempfile.TemporaryDirectory(prefix="strathmark-v3-ml-activate-") as temporary:
            root = Path(temporary)
            copies: dict[str, Path] = {}
            for index, relative in enumerate(model_paths):
                copy = root / f"model-{index}.json"
                copy.write_bytes(verified[relative])
                if copy.read_bytes() != verified[relative]:
                    raise MLArtifactError("verified ML activation copy differs before load")
                copies[relative] = copy
            universal = loader(copies["universal.json"])
            specialist_models = {
                key: loader(copies[path]) for key, (path, _eligibility) in specialists.items()
            }
            if any(copies[path].read_bytes() != verified[path] for path in model_paths):
                raise MLArtifactError("verified ML activation copy changed during load")
            return universal, specialist_models
    except MLArtifactError:
        raise
    except Exception as exc:
        raise MLArtifactError("verified CatBoost JSON failed model activation") from exc


def _validate_feature_schema(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "features",
        "categorical",
        "quantiles",
    }:
        raise MLArtifactError("ML feature schema fields are invalid")
    if value["schema_version"] != FEATURE_SCHEMA:
        raise MLArtifactError("ML feature schema is unsupported")
    features, categorical, quantiles = (
        value["features"],
        value["categorical"],
        value["quantiles"],
    )
    if (
        features != list(FEATURE_NAMES)
        or categorical != list(CATEGORICAL_FEATURES)
        or quantiles != ["0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"]
    ):
        raise MLArtifactError("ML feature schema is incompatible")
    return dict(value)


def _validate_bundle_metadata(
    value: Mapping[str, Any], calibrator: PITCalibrator
) -> dict[str, str]:
    expected = {
        "schema_version",
        "code_revision",
        "training_snapshot_digest",
        "role_manifest_digest",
        "gate_oof_digest",
        "calibrator_source_digest",
        "taxonomy_version",
        "conversion_version",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise MLArtifactError("ML bundle metadata fields do not match the closed schema")
    if value["schema_version"] != BUNDLE_METADATA_SCHEMA:
        raise MLArtifactError("ML bundle metadata schema is unsupported")
    if not all(
        _is_digest(value[name])
        for name in (
            "training_snapshot_digest",
            "role_manifest_digest",
            "gate_oof_digest",
            "calibrator_source_digest",
        )
    ):
        raise MLArtifactError("ML bundle metadata digests are invalid")
    if value["calibrator_source_digest"] != calibrator.source_digest:
        raise MLArtifactError("ML bundle metadata calibrator role binding differs")
    if not all(
        isinstance(value[name], str) and value[name]
        for name in ("code_revision", "taxonomy_version", "conversion_version")
    ):
        raise MLArtifactError("ML bundle metadata versions are invalid")
    return dict(value)


def _validate_vocabulary(
    value: Mapping[str, Any], feature_schema: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "values"}:
        raise MLArtifactError("ML category vocabulary fields are invalid")
    if value["schema_version"] != VOCABULARY_SCHEMA or not isinstance(value["values"], Mapping):
        raise MLArtifactError("ML category vocabulary schema is unsupported")
    if set(value["values"]) != set(feature_schema["categorical"]):
        raise MLArtifactError("ML category vocabulary does not cover categorical features")
    for values in value["values"].values():
        if (
            not isinstance(values, list)
            or "__other__" not in values
            or values != sorted(set(values))
            or not all(isinstance(item, str) and item for item in values)
        ):
            raise MLArtifactError("ML category vocabulary is not deterministic")
    return {"schema_version": value["schema_version"], "values": dict(value["values"])}


def _validate_dependency_lock(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "catboost_version",
        "python_abi",
    }:
        raise MLArtifactError("ML dependency lock fields are invalid")
    if value["schema_version"] != DEPENDENCY_SCHEMA or not all(
        isinstance(value[key], str) and value[key] for key in ("catboost_version", "python_abi")
    ):
        raise MLArtifactError("ML dependency lock schema is unsupported")
    return dict(value)


def _validate_file_manifest(value: Any) -> dict[str, tuple[int, str]]:
    if not isinstance(value, Mapping) or not value:
        raise MLArtifactError("ML file manifest must be a nonempty object")
    result: dict[str, tuple[int, str]] = {}
    for path, identity in value.items():
        if (
            not isinstance(path, str)
            or not path.endswith(".json")
            or not isinstance(identity, Mapping)
            or set(identity) != {"bytes", "sha256"}
            or isinstance(identity["bytes"], bool)
            or not isinstance(identity["bytes"], int)
            or identity["bytes"] <= 0
            or identity["bytes"] > MAX_JSON_FILE_BYTES
            or not _is_digest(identity["sha256"])
        ):
            raise MLArtifactError("ML file manifest contains an invalid bounded identity")
        result[path] = (identity["bytes"], identity["sha256"])
    return result


def _validate_specialist_manifest(
    value: Any,
) -> dict[str, tuple[str, SpecialistEligibility]]:
    if not isinstance(value, Mapping):
        raise MLArtifactError("ML specialists manifest must be an object")
    result: dict[str, tuple[str, SpecialistEligibility]] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(item, Mapping)
            or set(item)
            != {
                "path",
                "eligibility",
            }
        ):
            raise MLArtifactError("ML specialist manifest fields are invalid")
        eligibility_value = item["eligibility"]
        if not isinstance(eligibility_value, Mapping) or set(eligibility_value) != {
            "admitted_rows",
            "competitors",
            "tournaments",
            "available",
        }:
            raise MLArtifactError("ML specialist eligibility fields are invalid")
        eligibility = SpecialistEligibility(
            eligibility_value["admitted_rows"],
            eligibility_value["competitors"],
            eligibility_value["tournaments"],
        )
        if eligibility_value["available"] is not eligibility.available or not eligibility.available:
            raise MLArtifactError("ML specialist eligibility is inconsistent")
        result[key] = (item["path"], eligibility)
    return result


def _parse_catboost_json(raw: bytes) -> dict[str, Any]:
    value = _parse_json(raw, "CatBoost model")
    _reject_executable_shapes(value)
    if (
        not isinstance(value, dict)
        or not _MODEL_REQUIRED <= set(value)
        or not set(value) <= (_MODEL_REQUIRED | _MODEL_OPTIONAL)
    ):
        raise MLArtifactError("CatBoost JSON model schema is invalid")
    if (
        not isinstance(value["features_info"], Mapping)
        or not isinstance(value["model_info"], Mapping)
        or not isinstance(value["oblivious_trees"], list)
        or not isinstance(value["scale_and_bias"], list)
    ):
        raise MLArtifactError("CatBoost JSON model schema types are invalid")
    feature_info = value["features_info"]
    categorical = feature_info.get("categorical_features", [])
    numeric = feature_info.get("float_features", [])
    if not isinstance(categorical, list) or not isinstance(numeric, list):
        raise MLArtifactError("CatBoost JSON feature lists are invalid")
    declared: list[tuple[int, str, str]] = []
    for kind, features in (("categorical", categorical), ("numeric", numeric)):
        for feature in features:
            if (
                not isinstance(feature, Mapping)
                or not isinstance(feature.get("flat_feature_index"), int)
                or not isinstance(feature.get("feature_id"), str)
            ):
                raise MLArtifactError("CatBoost JSON feature identity is invalid")
            declared.append((feature["flat_feature_index"], feature["feature_id"], kind))
    declared.sort()
    expected = [
        (
            index,
            name,
            "categorical" if name in CATEGORICAL_FEATURES else "numeric",
        )
        for index, name in enumerate(FEATURE_NAMES)
    ]
    if declared != expected:
        raise MLArtifactError("CatBoost JSON feature order or types are incompatible")
    parameters = value["model_info"].get("params")
    if isinstance(parameters, str):
        parameters = _parse_json(parameters.encode("utf-8"), "CatBoost parameters")
    if not isinstance(parameters, Mapping):
        raise MLArtifactError("CatBoost JSON objective parameters are missing")
    loss = parameters.get("loss_function")
    if not isinstance(loss, Mapping) or loss.get("type") != "MultiQuantile":
        raise MLArtifactError("CatBoost JSON objective is not MultiQuantile")
    loss_parameters = loss.get("params")
    if not isinstance(loss_parameters, Mapping) or loss_parameters.get("alpha") != (
        "0.05,0.1,0.25,0.5,0.75,0.9,0.95"
    ):
        raise MLArtifactError("CatBoost JSON MultiQuantile levels are incompatible")
    scale_and_bias = value["scale_and_bias"]
    if (
        len(scale_and_bias) != 2
        or not isinstance(scale_and_bias[1], list)
        or len(scale_and_bias[1]) != 7
    ):
        raise MLArtifactError("CatBoost JSON output dimension must be seven")
    if any(
        not isinstance(tree, Mapping)
        or not isinstance(tree.get("leaf_values"), list)
        or len(tree["leaf_values"]) % 7
        for tree in value["oblivious_trees"]
    ):
        raise MLArtifactError("CatBoost JSON tree leaves do not preserve seven outputs")
    return value


def _reject_executable_shapes(value: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise MLArtifactError("CatBoost JSON exceeds the maximum depth")
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in _PROHIBITED_FRAGMENTS):
                raise MLArtifactError("CatBoost JSON contains a prohibited executable shape")
            _reject_executable_shapes(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_executable_shapes(item, depth=depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise MLArtifactError("CatBoost JSON contains a prohibited value type")


def _parse_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MLArtifactError(f"{label} must be valid inert JSON") from exc


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise MLArtifactError(f"{label} is missing or prohibited")
    size = path.stat().st_size
    if size <= 0 or size > limit:
        raise MLArtifactError(f"{label} exceeds the maximum safe size")
    return path.read_bytes()


def _safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise MLArtifactError("ML artifact path is prohibited")
    candidate = (root / relative).resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise MLArtifactError("ML artifact path escapes the bundle") from exc
    if any(part in {"", ".", ".."} for part in Path(relative).parts):
        raise MLArtifactError("ML artifact path is prohibited")
    return candidate


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(item in "0123456789abcdef" for item in value)
    )


__all__ = [
    "LoadedMLBundle",
    "MLArtifactError",
    "export_catboost_json",
    "load_ml_bundle",
    "write_ml_bundle",
]
