"""Optional, evidence-gated CatBoost residual corrections for Prediction V2.

CatBoost is imported only by explicit training or artifact-loading calls.  A
missing, rejected, incompatible, or corrupt artifact is represented as a
disabled runtime so callers can preserve the exact core forecast while
reporting a separate degradation warning.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd

from strathmark.prediction_v2 import (
    ForecastInterval,
    PredictiveDistribution,
    _canonical_json,
    history_band,
)

RESIDUAL_ARTIFACT_SCHEMA = "strathmark.prediction-v2-catboost-residual"
RESIDUAL_ARTIFACT_SCHEMA_VERSION = 1
RESIDUAL_MANIFEST_MAX_BYTES = 100_000
RESIDUAL_MODEL_MAX_BYTES = 100_000_000
RESIDUAL_MODEL_FILENAME = "residual.cbm"

BOOTSTRAP_RESAMPLES = 2_000
BOOTSTRAP_SEED = 20260811
POINT_IMPROVEMENT_THRESHOLD = 0.01
COVERAGE_DISTANCE_TOLERANCE = 0.02
COHORT_MAE_WORSENING_TOLERANCE = 0.05
MIN_COHORT_ROWS = 30
MAX_ABSOLUTE_LOG_CORRECTION = 0.35

CATEGORICAL_RESIDUAL_FEATURES = ("event", "gender", "species")
RESIDUAL_FEATURE_NAMES = (
    *CATEGORICAL_RESIDUAL_FEATURES,
    "species_missing",
    "log_diameter_ratio",
    "janka_hardness",
    "specific_gravity",
    "crush_strength",
    "shear_strength",
    "modulus_of_rupture",
    "modulus_of_elasticity",
    "core_log_location",
    "history_count",
    "effective_history_weight",
    "same_event_state",
    "trend_projection",
    "cross_event_state",
)

_COMPARISON_COLUMNS = (
    "actual_time",
    "core_prediction",
    "residual_prediction",
    "core_lower",
    "core_upper",
    "residual_lower",
    "residual_upper",
    "history_count",
)


@dataclass(frozen=True)
class PromotionDecision:
    """Deterministic result of the locked residual-promotion contract."""

    promoted: bool
    reasons: tuple[str, ...]
    common_rows: int
    core_mae: float
    residual_mae: float
    core_rmse: float
    residual_rmse: float
    mae_relative_improvement: float
    rmse_relative_improvement: float
    core_coverage: float
    residual_coverage: float
    coverage_distance_change: float
    cohorts: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    bootstrap_support: float = 0.0
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES
    bootstrap_seed: int = BOOTSTRAP_SEED

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe immutable audit record."""

        return asdict(self)


@dataclass(frozen=True)
class ResidualArtifactLoad:
    """Validated optional model state, including fail-closed diagnostics."""

    model: Any = None
    manifest: Mapping[str, Any] = field(default_factory=dict)
    warning: Optional[str] = None
    degraded: bool = False

    @property
    def active(self) -> bool:
        return self.model is not None and self.warning is None and not self.degraded


@dataclass(frozen=True)
class ResidualApplication:
    """Core or corrected distribution plus separate optional-layer status."""

    distribution: PredictiveDistribution
    applied: bool
    degraded: bool
    warning: Optional[str] = None


class ResidualRuntime:
    """Apply a validated native CatBoost artifact without weakening fallback."""

    def __init__(self, loaded: ResidualArtifactLoad):
        self.loaded = loaded

    def apply(
        self,
        core: PredictiveDistribution,
        features: Mapping[str, Any],
    ) -> ResidualApplication:
        """Return the identical core object unless a correction succeeds."""

        if not self.loaded.active:
            return ResidualApplication(
                distribution=core,
                applied=False,
                degraded=self.loaded.degraded,
                warning=self.loaded.warning,
            )
        try:
            frame = _validated_feature_frame(pd.DataFrame([dict(features)]))
            raw = np.asarray(self.loaded.model.predict(frame), dtype=float).reshape(-1)
            if raw.size != 1 or not math.isfinite(float(raw[0])):
                raise ValueError("residual prediction must be one finite number")
            correction = float(
                np.clip(raw[0], -MAX_ABSOLUTE_LOG_CORRECTION, MAX_ABSOLUTE_LOG_CORRECTION)
            )
        except Exception:
            return ResidualApplication(
                distribution=core,
                applied=False,
                degraded=True,
                warning="residual_prediction_failed",
            )

        factor = math.exp(correction)
        payload = self.loaded.manifest
        corrected = replace(
            core,
            median=core.median * factor,
            log_location=core.log_location + correction,
            interval=ForecastInterval(
                lower=core.interval.lower * factor,
                upper=core.interval.upper * factor,
                nominal_coverage=core.interval.nominal_coverage,
                calibration_state=core.interval.calibration_state,
                scope=core.interval.scope,
            ),
            source=f"{core.source}+catboost_residual",
            metadata={
                **dict(core.metadata),
                "residual_correction_log": correction,
                "residual_model_version": payload["model_version"],
            },
        )
        return ResidualApplication(corrected, applied=True, degraded=False)


def evaluate_residual_promotion(comparison: pd.DataFrame) -> PromotionDecision:
    """Evaluate CatBoost and core on identical rows under the locked gates.

    The paired bootstrap is deterministic.  Its support is the fraction of
    resamples where both point metrics meet the one-percent lift threshold;
    exactly 50 percent is a rejected tie.
    """

    missing = set(_COMPARISON_COLUMNS) - set(comparison.columns)
    if missing:
        raise ValueError(f"promotion comparison missing columns: {sorted(missing)}")
    frame = comparison.loc[:, _COMPARISON_COLUMNS].copy()
    for column in _COMPARISON_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    finite = np.all(np.isfinite(frame.to_numpy(dtype=float)), axis=1)
    positive = np.all(
        frame[
            [
                "actual_time",
                "core_prediction",
                "residual_prediction",
                "core_lower",
                "core_upper",
                "residual_lower",
                "residual_upper",
            ]
        ].to_numpy(dtype=float)
        > 0,
        axis=1,
    )
    ordered = (frame["core_lower"] <= frame["core_upper"]) & (
        frame["residual_lower"] <= frame["residual_upper"]
    )
    frame = frame.loc[finite & positive & ordered].reset_index(drop=True)
    if frame.empty:
        return _empty_promotion_decision("no_common_rows")

    actual = frame["actual_time"].to_numpy(dtype=float)
    core_errors = np.abs(frame["core_prediction"].to_numpy(dtype=float) - actual)
    residual_errors = np.abs(frame["residual_prediction"].to_numpy(dtype=float) - actual)
    core_squared = np.square(frame["core_prediction"].to_numpy(dtype=float) - actual)
    residual_squared = np.square(frame["residual_prediction"].to_numpy(dtype=float) - actual)

    core_mae = float(np.mean(core_errors))
    residual_mae = float(np.mean(residual_errors))
    core_rmse = float(math.sqrt(float(np.mean(core_squared))))
    residual_rmse = float(math.sqrt(float(np.mean(residual_squared))))
    mae_improvement = _relative_improvement(core_mae, residual_mae)
    rmse_improvement = _relative_improvement(core_rmse, residual_rmse)

    core_coverage = float(
        np.mean((frame["core_lower"] <= actual) & (actual <= frame["core_upper"]))
    )
    residual_coverage = float(
        np.mean((frame["residual_lower"] <= actual) & (actual <= frame["residual_upper"]))
    )
    coverage_distance_change = abs(residual_coverage - 0.90) - abs(core_coverage - 0.90)

    cohorts: dict[str, dict[str, float]] = {}
    cohort_gate = True
    bands = frame["history_count"].map(history_band)
    for band in ("0", "1-3", "4+"):
        indices = np.flatnonzero(bands.to_numpy() == band)
        if len(indices) < MIN_COHORT_ROWS:
            continue
        cohort_core_mae = float(np.mean(core_errors[indices]))
        cohort_residual_mae = float(np.mean(residual_errors[indices]))
        worsening = _relative_worsening(cohort_core_mae, cohort_residual_mae)
        cohorts[band] = {
            "count": int(len(indices)),
            "core_mae": cohort_core_mae,
            "residual_mae": cohort_residual_mae,
            "mae_worsening": worsening,
        }
        if worsening > COHORT_MAE_WORSENING_TOLERANCE + 1e-12:
            cohort_gate = False

    bootstrap_support = _paired_bootstrap_support(core_errors, residual_errors)
    point_gate = (
        mae_improvement + 1e-12 >= POINT_IMPROVEMENT_THRESHOLD
        and rmse_improvement + 1e-12 >= POINT_IMPROVEMENT_THRESHOLD
    )
    coverage_gate = coverage_distance_change <= COVERAGE_DISTANCE_TOLERANCE + 1e-12
    bootstrap_gate = bootstrap_support > 0.50

    reasons: list[str] = []
    if not point_gate:
        reasons.append("point_accuracy_gate_failed")
    if not coverage_gate:
        reasons.append("coverage_gate_failed")
    if not cohort_gate:
        reasons.append("cohort_harm_gate_failed")
    if not bootstrap_gate:
        reasons.append("paired_bootstrap_gate_failed")
    return PromotionDecision(
        promoted=not reasons,
        reasons=tuple(reasons),
        common_rows=len(frame),
        core_mae=core_mae,
        residual_mae=residual_mae,
        core_rmse=core_rmse,
        residual_rmse=residual_rmse,
        mae_relative_improvement=mae_improvement,
        rmse_relative_improvement=rmse_improvement,
        core_coverage=core_coverage,
        residual_coverage=residual_coverage,
        coverage_distance_change=float(coverage_distance_change),
        cohorts=cohorts,
        bootstrap_support=bootstrap_support,
    )


def fit_catboost_residual(
    training_frame: pd.DataFrame,
    *,
    iterations: int = 250,
    depth: int = 5,
    learning_rate: float = 0.03,
    random_seed: int = BOOTSTRAP_SEED,
) -> Any:
    """Fit a native CatBoost regressor on already out-of-fold core residuals."""

    provenance = {
        "actual_time",
        "core_log_location",
        "core_log_residual",
        "result_date",
        "fold_training_cutoff",
        "fold_training_max_date",
    }
    missing_provenance = provenance - set(training_frame.columns)
    if missing_provenance:
        raise ValueError(
            "residual training requires rolling-fold provenance columns: "
            f"{sorted(missing_provenance)}"
        )
    result_dates = pd.to_datetime(training_frame["result_date"], errors="coerce", utc=True)
    fold_cutoffs = pd.to_datetime(training_frame["fold_training_cutoff"], errors="coerce", utc=True)
    fold_max_dates = pd.to_datetime(
        training_frame["fold_training_max_date"], errors="coerce", utc=True
    )
    if (
        result_dates.isna().any()
        or fold_cutoffs.isna().any()
        or fold_max_dates.isna().any()
        or not (fold_max_dates < fold_cutoffs).all()
        or not (result_dates == fold_cutoffs).all()
    ):
        raise ValueError("residual training rows must come from strictly-prior rolling folds")
    features = _validated_feature_frame(training_frame)
    target = pd.to_numeric(training_frame["core_log_residual"], errors="coerce").to_numpy(
        dtype=float
    )
    if not np.all(np.isfinite(target)) or len(target) != len(features):
        raise ValueError("core_log_residual must be finite for every training row")
    actual = pd.to_numeric(training_frame["actual_time"], errors="coerce").to_numpy(dtype=float)
    core_location = pd.to_numeric(training_frame["core_log_location"], errors="coerce").to_numpy(
        dtype=float
    )
    if (
        not np.all(np.isfinite(actual))
        or np.any(actual <= 0)
        or not np.all(np.isfinite(core_location))
        or not np.allclose(target, np.log(actual) - core_location, rtol=0.0, atol=1e-12)
    ):
        raise ValueError("core_log_residual does not match its out-of-fold core prediction")
    CatBoostRegressor = _import_catboost_regressor()
    model = CatBoostRegressor(
        loss_function="RMSE",
        iterations=int(iterations),
        depth=int(depth),
        learning_rate=float(learning_rate),
        random_seed=int(random_seed),
        verbose=False,
        allow_writing_files=False,
        thread_count=1,
    )
    model.fit(features, target, cat_features=list(CATEGORICAL_RESIDUAL_FEATURES))
    return model


def save_residual_artifact(
    model: Any,
    artifact_directory: str | Path,
    *,
    model_version: str,
    training_cutoff: date,
    evidence_max_date: date,
    core_source_checksum: str,
    promotion: PromotionDecision,
) -> Path:
    """Save a native CatBoost model and checksummed JSON manifest (never pickle)."""

    if not str(model_version).strip():
        raise ValueError("model_version is required")
    if evidence_max_date >= training_cutoff:
        raise ValueError("residual evidence must be earlier than training_cutoff")
    checksum = str(core_source_checksum).lower()
    if not _is_sha256(checksum):
        raise ValueError("core_source_checksum must be a SHA-256 digest")
    if promotion.promoted and not _promotion_audit_passes(promotion.to_dict()):
        raise ValueError("promoted residual decision does not satisfy the locked gates")
    directory = Path(artifact_directory)
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / RESIDUAL_MODEL_FILENAME
    manifest_path = directory / "manifest.json"
    if model_path.exists() or manifest_path.exists():
        raise FileExistsError("refusing to overwrite an existing residual artifact")
    model.save_model(str(model_path), format="cbm")
    size = model_path.stat().st_size
    if size <= 0 or size > RESIDUAL_MODEL_MAX_BYTES:
        raise ValueError("residual native model size is invalid")
    model_checksum = _file_sha256(model_path)
    payload = {
        "model_version": str(model_version),
        "training_cutoff": training_cutoff.isoformat(),
        "evidence_max_date": evidence_max_date.isoformat(),
        "core_source_checksum": checksum,
        "feature_names": list(RESIDUAL_FEATURE_NAMES),
        "categorical_features": list(CATEGORICAL_RESIDUAL_FEATURES),
        "model_filename": RESIDUAL_MODEL_FILENAME,
        "model_bytes": size,
        "model_checksum": model_checksum,
        "promoted": bool(promotion.promoted),
        "promotion": promotion.to_dict(),
    }
    payload_bytes = _canonical_json(payload).encode("utf-8")
    envelope = {
        "schema": RESIDUAL_ARTIFACT_SCHEMA,
        "schema_version": RESIDUAL_ARTIFACT_SCHEMA_VERSION,
        "payload_bytes": len(payload_bytes),
        "payload_checksum": hashlib.sha256(payload_bytes).hexdigest(),
        "payload": payload,
    }
    encoded = _canonical_json(envelope)
    if len(encoded.encode("utf-8")) > RESIDUAL_MANIFEST_MAX_BYTES:
        raise ValueError("residual manifest exceeds maximum safe size")
    manifest_path.write_text(encoded, encoding="utf-8")
    return directory


def load_residual_artifact(
    artifact_directory: str | Path | None,
    *,
    expected_core_checksum: Optional[str] = None,
    prediction_as_of: Optional[date] = None,
) -> ResidualArtifactLoad:
    """Load an eligible native artifact or return a disabled fail-closed state."""

    if artifact_directory is None:
        return _disabled("residual_artifact_missing")
    directory = Path(artifact_directory)
    manifest_path = directory / "manifest.json"
    if not directory.is_dir() or not manifest_path.is_file():
        return _disabled("residual_artifact_missing")
    try:
        payload = _load_manifest(manifest_path, directory)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return _disabled("residual_artifact_invalid")
    if not payload["promoted"]:
        return _disabled("residual_artifact_not_promoted", payload)
    if expected_core_checksum is not None:
        expected = str(expected_core_checksum).lower()
        if not _is_sha256(expected) or payload["core_source_checksum"] != expected:
            return _disabled("residual_artifact_incompatible", payload)
    if prediction_as_of is not None:
        if date.fromisoformat(payload["evidence_max_date"]) >= prediction_as_of:
            return _disabled("residual_artifact_incompatible", payload)
    try:
        CatBoostRegressor = _import_catboost_regressor()
    except (ImportError, ModuleNotFoundError):
        return _disabled("residual_dependency_unavailable", payload)
    try:
        model = CatBoostRegressor()
        model.load_model(str(directory / RESIDUAL_MODEL_FILENAME), format="cbm")
    except Exception:
        return _disabled("residual_artifact_invalid", payload)
    return ResidualArtifactLoad(model=model, manifest=payload)


def _load_manifest(manifest_path: Path, directory: Path) -> dict[str, Any]:
    raw = manifest_path.read_bytes()
    if len(raw) > RESIDUAL_MANIFEST_MAX_BYTES:
        raise ValueError("residual manifest exceeds maximum safe size")
    envelope = json.loads(raw)
    if not isinstance(envelope, dict):
        raise ValueError("residual manifest envelope must be an object")
    if set(envelope) != {
        "schema",
        "schema_version",
        "payload_bytes",
        "payload_checksum",
        "payload",
    }:
        raise ValueError("residual manifest envelope fields do not match schema")
    if (
        envelope["schema"] != RESIDUAL_ARTIFACT_SCHEMA
        or envelope["schema_version"] != RESIDUAL_ARTIFACT_SCHEMA_VERSION
    ):
        raise ValueError("unsupported residual artifact schema")
    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise ValueError("residual manifest payload must be an object")
    required = {
        "model_version",
        "training_cutoff",
        "evidence_max_date",
        "core_source_checksum",
        "feature_names",
        "categorical_features",
        "model_filename",
        "model_bytes",
        "model_checksum",
        "promoted",
        "promotion",
    }
    if set(payload) != required:
        raise ValueError("residual manifest payload fields do not match schema")
    payload_bytes = _canonical_json(payload).encode("utf-8")
    if envelope["payload_bytes"] != len(payload_bytes):
        raise ValueError("residual manifest payload size mismatch")
    if envelope["payload_checksum"] != hashlib.sha256(payload_bytes).hexdigest():
        raise ValueError("residual manifest checksum mismatch")
    if payload["feature_names"] != list(RESIDUAL_FEATURE_NAMES):
        raise ValueError("residual feature schema is incompatible")
    if payload["categorical_features"] != list(CATEGORICAL_RESIDUAL_FEATURES):
        raise ValueError("residual categorical schema is incompatible")
    if payload["model_filename"] != RESIDUAL_MODEL_FILENAME:
        raise ValueError("residual model filename is incompatible")
    if not isinstance(payload["promoted"], bool):
        raise ValueError("residual promotion state must be boolean")
    if not isinstance(payload["promotion"], dict):
        raise ValueError("residual promotion audit must be an object")
    if payload["promotion"].get("promoted") is not payload["promoted"]:
        raise ValueError("residual promotion state is inconsistent")
    if payload["promoted"] and not _promotion_audit_passes(payload["promotion"]):
        raise ValueError("promoted residual audit does not satisfy the locked gates")
    if not _is_sha256(payload["core_source_checksum"]):
        raise ValueError("residual core checksum is invalid")
    training_cutoff = date.fromisoformat(payload["training_cutoff"])
    evidence_max_date = date.fromisoformat(payload["evidence_max_date"])
    if evidence_max_date >= training_cutoff:
        raise ValueError("residual evidence cutoff is not causal")
    model_path = directory / RESIDUAL_MODEL_FILENAME
    if not model_path.is_file():
        raise ValueError("residual native model is missing")
    expected_size = int(payload["model_bytes"])
    actual_size = model_path.stat().st_size
    if (
        expected_size <= 0
        or expected_size > RESIDUAL_MODEL_MAX_BYTES
        or actual_size != expected_size
    ):
        raise ValueError("residual native model size mismatch")
    if not _is_sha256(payload["model_checksum"]):
        raise ValueError("residual native model checksum is invalid")
    if _file_sha256(model_path) != payload["model_checksum"]:
        raise ValueError("residual native model checksum mismatch")
    return payload


def _validated_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(RESIDUAL_FEATURE_NAMES) - set(frame.columns)
    if missing:
        raise ValueError(f"residual features missing columns: {sorted(missing)}")
    result = frame.loc[:, RESIDUAL_FEATURE_NAMES].copy()
    for column in CATEGORICAL_RESIDUAL_FEATURES:
        values = result[column]
        if values.isna().any() or values.astype(str).str.strip().eq("").any():
            raise ValueError(f"residual categorical feature {column} is missing")
        result[column] = values.astype(str)
    numeric_columns = [
        name for name in RESIDUAL_FEATURE_NAMES if name not in CATEGORICAL_RESIDUAL_FEATURES
    ]
    for column in numeric_columns:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if not np.all(np.isfinite(result[numeric_columns].to_numpy(dtype=float))):
        raise ValueError("residual numeric features must be finite")
    result["species_missing"] = result["species_missing"].astype(int)
    return result


def _paired_bootstrap_support(core_errors: np.ndarray, residual_errors: np.ndarray) -> float:
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    count = len(core_errors)
    supported = 0
    for _ in range(BOOTSTRAP_RESAMPLES):
        indices = rng.integers(0, count, size=count)
        core = core_errors[indices]
        residual = residual_errors[indices]
        mae_lift = _relative_improvement(float(np.mean(core)), float(np.mean(residual)))
        core_rmse = float(math.sqrt(float(np.mean(np.square(core)))))
        residual_rmse = float(math.sqrt(float(np.mean(np.square(residual)))))
        rmse_lift = _relative_improvement(core_rmse, residual_rmse)
        if (
            mae_lift + 1e-12 >= POINT_IMPROVEMENT_THRESHOLD
            and rmse_lift + 1e-12 >= POINT_IMPROVEMENT_THRESHOLD
        ):
            supported += 1
    return supported / BOOTSTRAP_RESAMPLES


def _promotion_audit_passes(value: Mapping[str, Any]) -> bool:
    required = {item.name for item in PromotionDecision.__dataclass_fields__.values()}
    if set(value) != required or value.get("promoted") is not True:
        return False
    if value.get("reasons") not in ((), []):
        return False
    try:
        finite_values = (
            float(value["core_mae"]),
            float(value["residual_mae"]),
            float(value["core_rmse"]),
            float(value["residual_rmse"]),
            float(value["mae_relative_improvement"]),
            float(value["rmse_relative_improvement"]),
            float(value["core_coverage"]),
            float(value["residual_coverage"]),
            float(value["coverage_distance_change"]),
            float(value["bootstrap_support"]),
        )
        if not all(math.isfinite(item) for item in finite_values):
            return False
        if int(value["common_rows"]) <= 0:
            return False
        if int(value["bootstrap_resamples"]) != BOOTSTRAP_RESAMPLES:
            return False
        if int(value["bootstrap_seed"]) != BOOTSTRAP_SEED:
            return False
        if float(value["mae_relative_improvement"]) + 1e-12 < POINT_IMPROVEMENT_THRESHOLD:
            return False
        if float(value["rmse_relative_improvement"]) + 1e-12 < POINT_IMPROVEMENT_THRESHOLD:
            return False
        if float(value["coverage_distance_change"]) > COVERAGE_DISTANCE_TOLERANCE + 1e-12:
            return False
        if float(value["bootstrap_support"]) <= 0.50:
            return False
        cohorts = value["cohorts"]
        if not isinstance(cohorts, Mapping):
            return False
        for metrics in cohorts.values():
            if not isinstance(metrics, Mapping):
                return False
            if int(metrics["count"]) < MIN_COHORT_ROWS:
                return False
            if float(metrics["mae_worsening"]) > COHORT_MAE_WORSENING_TOLERANCE + 1e-12:
                return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def _relative_improvement(core: float, residual: float) -> float:
    if core <= 1e-15:
        return 0.0 if residual >= core - 1e-15 else 1.0
    return (core - residual) / core


def _relative_worsening(core: float, residual: float) -> float:
    if core <= 1e-15:
        return 0.0 if residual <= core + 1e-15 else math.inf
    return (residual - core) / core


def _empty_promotion_decision(reason: str) -> PromotionDecision:
    return PromotionDecision(
        promoted=False,
        reasons=(reason,),
        common_rows=0,
        core_mae=math.nan,
        residual_mae=math.nan,
        core_rmse=math.nan,
        residual_rmse=math.nan,
        mae_relative_improvement=math.nan,
        rmse_relative_improvement=math.nan,
        core_coverage=math.nan,
        residual_coverage=math.nan,
        coverage_distance_change=math.nan,
    )


def _disabled(warning: str, manifest: Optional[Mapping[str, Any]] = None) -> ResidualArtifactLoad:
    return ResidualArtifactLoad(
        manifest={} if manifest is None else manifest,
        warning=warning,
        degraded=True,
    )


def _import_catboost_regressor() -> Any:
    from catboost import CatBoostRegressor

    return CatBoostRegressor


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "CATEGORICAL_RESIDUAL_FEATURES",
    "PromotionDecision",
    "RESIDUAL_FEATURE_NAMES",
    "ResidualApplication",
    "ResidualArtifactLoad",
    "ResidualRuntime",
    "evaluate_residual_promotion",
    "fit_catboost_residual",
    "load_residual_artifact",
    "save_residual_artifact",
]
