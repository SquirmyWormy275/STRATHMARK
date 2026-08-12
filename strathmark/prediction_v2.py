"""Deterministic hierarchical log-time core for Prediction Engine V2.

The dependable core intentionally uses only NumPy and Pandas.  Population
parameters live in a safe, checksummed JSON artifact; competitor state is
always reconstructed from the request's strictly-prior history.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd

from strathmark.features import (
    CANONICALIZATION_VERSION,
    MISSING_CATEGORY,
    MODEL_EVIDENCE_FIELDS,
    SPECIES_PROPERTY_FIELDS,
    PriorEvidence,
    build_prior_evidence,
    normalize_prediction_as_of,
)

ENGINE_VERSION = "2.0.0"
ARTIFACT_SCHEMA = "strathmark.prediction-v2-core"
ARTIFACT_SCHEMA_VERSION = 2
ARTIFACT_MAX_BYTES = 1_000_000

HUBER_THRESHOLD = 1.345
RIDGE_PENALTY = 10.0
IRLS_MAX_ITERATIONS = 25
IRLS_TOLERANCE = 1e-8
RECENCY_HALF_LIFE_DAYS = 730.0
SAME_EVENT_SHRINKAGE = 4.0
TREND_SHRINKAGE = 6.0
TREND_CAP_LOG_PER_YEAR = 0.12
MIN_TREND_SPAN_DAYS = 180
CROSS_EVENT_MIN_PAIRS = 10
CROSS_EVENT_RIDGE = 0.10
CROSS_EVENT_CAP = 0.75
NORMAL_90_RADIUS = 1.6448536269514722

_EVENTS = ("SB", "UH")
_BROAD_PRIORS = {"SB": (50.0, 0.45), "UH": (75.0, 0.45)}


@dataclass(frozen=True)
class PredictionV2Request:
    """Verified request factors for one V2 prediction."""

    competitor_id: str
    event: str
    diameter_mm: float
    species: str
    gender: str
    prediction_as_of: date
    janka_hardness: float = 1690.0
    specific_gravity: float = 0.34
    crush_strength: float = 4000.0
    shear_strength: float = 1000.0
    modulus_of_rupture: float = 8000.0
    modulus_of_elasticity: float = 1_000_000.0
    species_missing: bool = False

    def __post_init__(self) -> None:
        if not str(self.competitor_id).strip():
            raise ValueError("competitor_id is required for V2 competitor state")
        event = str(self.event).upper()
        if event not in _EVENTS:
            raise ValueError("event must be 'SB' or 'UH'")
        if not math.isfinite(float(self.diameter_mm)) or float(self.diameter_mm) <= 0:
            raise ValueError("diameter_mm must be positive and finite")
        for name in SPECIES_PROPERTY_FIELDS:
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        object.__setattr__(self, "event", event)
        species = str(self.species).strip().upper()
        object.__setattr__(self, "species", species or MISSING_CATEGORY)
        gender = str(self.gender).strip().upper()
        object.__setattr__(self, "gender", gender if gender in {"M", "F"} else MISSING_CATEGORY)
        object.__setattr__(
            self, "prediction_as_of", normalize_prediction_as_of(self.prediction_as_of)
        )


@dataclass(frozen=True)
class ForecastInterval:
    """Central forecast interval on positive time support."""

    lower: float
    upper: float
    nominal_coverage: float = 0.90
    calibration_state: str = "analytic"
    scope: str = "analytic"

    def __post_init__(self) -> None:
        if not math.isfinite(self.lower) or not math.isfinite(self.upper):
            raise ValueError("forecast interval bounds must be finite")
        if self.lower <= 0 or self.lower > self.upper:
            raise ValueError("forecast interval must satisfy 0 < lower <= upper")
        if not 0 < self.nominal_coverage < 1:
            raise ValueError("nominal coverage must be between zero and one")


@dataclass(frozen=True)
class CalibrationRadius:
    """Selected absolute-log-residual calibration radius."""

    value: Optional[float]
    scope: str
    sample_count: int
    calibrated: bool


@dataclass(frozen=True)
class ChronologicalCalibrator:
    """Chronological split-conformal radii with deterministic pooling."""

    version: str = "uncalibrated"
    nominal_coverage: float = 0.90
    cohort_radii: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    cohort_counts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    event_radii: Mapping[str, float] = field(default_factory=dict)
    event_counts: Mapping[str, int] = field(default_factory=dict)
    global_radius: Optional[float] = None
    global_count: int = 0
    max_evidence_date: Optional[date] = None

    @classmethod
    def fit(
        cls,
        residuals: pd.DataFrame,
        *,
        version: str = "chronological-conformal-v1",
        nominal_coverage: float = 0.90,
    ) -> "ChronologicalCalibrator":
        required = {"event", "history_count", "absolute_log_residual"}
        missing = required - set(residuals.columns)
        if missing:
            raise ValueError(f"calibration residuals missing columns: {sorted(missing)}")
        frame = residuals.copy()
        frame["event"] = frame["event"].astype(str).str.upper()
        frame["history_band"] = frame["history_count"].map(_history_band)
        frame["absolute_log_residual"] = pd.to_numeric(
            frame["absolute_log_residual"], errors="coerce"
        )
        frame = frame[
            frame["event"].isin(_EVENTS)
            & np.isfinite(frame["absolute_log_residual"])
            & (frame["absolute_log_residual"] >= 0)
        ].copy()

        cohort_radii: dict[str, dict[str, float]] = {}
        cohort_counts: dict[str, dict[str, int]] = {}
        for (event, band), group in frame.groupby(["event", "history_band"], sort=True):
            count = len(group)
            cohort_counts.setdefault(event, {})[band] = count
            if count >= 30:
                cohort_radii.setdefault(event, {})[band] = _finite_sample_higher_quantile(
                    group["absolute_log_residual"].to_numpy(), nominal_coverage
                )

        event_radii: dict[str, float] = {}
        event_counts: dict[str, int] = {}
        for event, group in frame.groupby("event", sort=True):
            count = len(group)
            event_counts[event] = count
            if count >= 50:
                event_radii[event] = _finite_sample_higher_quantile(
                    group["absolute_log_residual"].to_numpy(), nominal_coverage
                )

        global_count = len(frame)
        global_radius = None
        if global_count >= 100:
            global_radius = _finite_sample_higher_quantile(
                frame["absolute_log_residual"].to_numpy(), nominal_coverage
            )

        max_evidence_date = None
        if "result_date" in frame and not frame.empty:
            parsed = pd.to_datetime(frame["result_date"], errors="coerce", utc=True).dropna()
            if not parsed.empty:
                max_evidence_date = parsed.max().date()

        return cls(
            version=version,
            nominal_coverage=nominal_coverage,
            cohort_radii=cohort_radii,
            cohort_counts=cohort_counts,
            event_radii=event_radii,
            event_counts=event_counts,
            global_radius=global_radius,
            global_count=global_count,
            max_evidence_date=max_evidence_date,
        )

    def radius(self, event: str, history_count: int) -> CalibrationRadius:
        event = str(event).upper()
        band = _history_band(history_count)
        cohort_value = self.cohort_radii.get(event, {}).get(band)
        if cohort_value is not None:
            return CalibrationRadius(
                cohort_value,
                "event_history_band",
                self.cohort_counts[event][band],
                True,
            )
        event_value = self.event_radii.get(event)
        if event_value is not None:
            return CalibrationRadius(event_value, "event", self.event_counts[event], True)
        if self.global_radius is not None:
            return CalibrationRadius(self.global_radius, "global", self.global_count, True)
        return CalibrationRadius(None, "analytic", self.global_count, False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "nominal_coverage": self.nominal_coverage,
            "cohort_radii": {key: dict(value) for key, value in self.cohort_radii.items()},
            "cohort_counts": {key: dict(value) for key, value in self.cohort_counts.items()},
            "event_radii": dict(self.event_radii),
            "event_counts": dict(self.event_counts),
            "global_radius": self.global_radius,
            "global_count": self.global_count,
            "max_evidence_date": (
                self.max_evidence_date.isoformat() if self.max_evidence_date else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChronologicalCalibrator":
        max_date = value.get("max_evidence_date")
        return cls(
            version=str(value["version"]),
            nominal_coverage=float(value["nominal_coverage"]),
            cohort_radii={
                str(event): {str(band): float(radius) for band, radius in bands.items()}
                for event, bands in value.get("cohort_radii", {}).items()
            },
            cohort_counts={
                str(event): {str(band): int(count) for band, count in bands.items()}
                for event, bands in value.get("cohort_counts", {}).items()
            },
            event_radii={
                str(key): float(number) for key, number in value.get("event_radii", {}).items()
            },
            event_counts={
                str(key): int(number) for key, number in value.get("event_counts", {}).items()
            },
            global_radius=(
                None if value.get("global_radius") is None else float(value["global_radius"])
            ),
            global_count=int(value.get("global_count", 0)),
            max_evidence_date=date.fromisoformat(max_date) if max_date else None,
        )


@dataclass(frozen=True)
class PredictiveDistribution:
    """A deterministic lognormal forecast and its audit metadata."""

    median: float
    log_location: float
    log_scale: float
    interval: ForecastInterval
    source: str
    history_count: int
    effective_history_weight: float
    warnings: tuple[str, ...] = ()
    degraded: bool = False
    model_version: str = ""
    calibration_version: str = "uncalibrated"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def sample(
        self,
        size: int,
        *,
        seed: int = 20260811,
        shared_standard_normal: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Draw reproducible positive-support samples for the mark optimizer."""

        if size <= 0:
            raise ValueError("size must be positive")
        rng = np.random.default_rng(seed)
        independent = rng.standard_normal(size)
        shared_scale = float(self.metadata.get("shared_log_scale", 0.0))
        shared = 0.0
        if shared_standard_normal is not None:
            individual_scale = math.sqrt(max(self.log_scale**2 - shared_scale**2, 1e-12))
            supplied = np.asarray(shared_standard_normal, dtype=float)
            if supplied.shape != (size,) or not np.all(np.isfinite(supplied)):
                raise ValueError("shared_standard_normal must be a finite vector matching size")
            shared = shared_scale * supplied
        else:
            individual_scale = self.log_scale
        return np.exp(self.log_location + shared + individual_scale * independent)


@dataclass(frozen=True)
class PredictionV2Model:
    """Fitted population core plus optional chronological calibration state."""

    model_version: str
    training_cutoff: date
    evidence_max_date: date
    source_checksum: str
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    property_means: Mapping[str, float]
    property_scales: Mapping[str, float]
    diameter_support: Mapping[str, tuple[float, float]]
    event_scales: Mapping[str, float]
    event_counts: Mapping[str, int]
    species_support: tuple[str, ...]
    cross_event_coefficients: Mapping[str, float]
    species_properties: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    calibration: ChronologicalCalibrator = field(default_factory=ChronologicalCalibrator)
    validation_metrics: Mapping[str, float] = field(default_factory=dict)
    engine_version: str = ENGINE_VERSION
    canonicalization_version: str = CANONICALIZATION_VERSION

    @classmethod
    def fit(
        cls,
        evidence: PriorEvidence | pd.DataFrame,
        *,
        training_cutoff: date,
        model_version: str = "prediction-v2-core",
        source_checksum: Optional[str] = None,
        validation_metrics: Optional[Mapping[str, float]] = None,
    ) -> "PredictionV2Model":
        cutoff = normalize_prediction_as_of(training_cutoff)
        frame = _canonical_evidence(evidence, cutoff)
        if len(frame) < 4:
            raise ValueError("at least four valid prior rows are required to fit V2")
        frame = frame.sort_values(
            ["result_date", "competitor_id", "event"], kind="mergesort"
        ).reset_index(drop=True)

        property_means: dict[str, float] = {}
        property_scales: dict[str, float] = {}
        for name in SPECIES_PROPERTY_FIELDS:
            values = frame[name].astype(float).to_numpy()
            mean = float(np.mean(values))
            scale = float(np.std(values))
            property_means[name] = mean
            property_scales[name] = scale if scale > 1e-12 else 1.0

        diameter_support: dict[str, tuple[float, float]] = {}
        event_counts: dict[str, int] = {}
        for event in _EVENTS:
            values = frame.loc[frame["event"] == event, "diameter_mm"].astype(float).to_numpy()
            event_counts[event] = int(len(values))
            if len(values):
                low, high = np.quantile(values, [0.01, 0.99])
                diameter_support[event] = (float(low), float(high))
            else:
                diameter_support[event] = (250.0, 350.0)

        feature_names = _feature_names()
        matrix = _design_matrix(
            frame,
            property_means=property_means,
            property_scales=property_scales,
            diameter_support=diameter_support,
        )
        target = np.log(frame["time_seconds"].astype(float).to_numpy())
        coefficients = _fit_huber_ridge(matrix, target)
        residuals = target - matrix @ coefficients

        event_scales: dict[str, float] = {}
        for event in _EVENTS:
            event_residuals = residuals[frame["event"].to_numpy() == event]
            event_scales[event] = _robust_scale(event_residuals, floor=0.05)

        residual_frame = frame[["competitor_id", "event"]].copy()
        residual_frame["residual"] = residuals
        cross = _learn_cross_event_coefficients(residual_frame)
        species_properties: dict[str, dict[str, float]] = {}
        known_species = frame.loc[~frame["species_missing"].astype(bool)].copy()
        for species, group in known_species.groupby("species", sort=True):
            species_properties[str(species)] = {
                name: float(group[name].astype(float).median()) for name in SPECIES_PROPERTY_FIELDS
            }
        digest = source_checksum or _frame_checksum(frame)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise ValueError("source_checksum must be a SHA-256 hexadecimal digest")

        return cls(
            model_version=str(model_version),
            training_cutoff=cutoff,
            evidence_max_date=max(frame["result_date"]),
            source_checksum=digest.lower(),
            feature_names=feature_names,
            coefficients=tuple(float(value) for value in coefficients),
            property_means=property_means,
            property_scales=property_scales,
            diameter_support=diameter_support,
            event_scales=event_scales,
            event_counts=event_counts,
            species_support=tuple(sorted(set(frame["species"].astype(str)) - {MISSING_CATEGORY})),
            cross_event_coefficients=cross,
            species_properties=species_properties,
            validation_metrics=dict(validation_metrics or {}),
        )

    def with_calibration(self, calibration: ChronologicalCalibrator) -> "PredictionV2Model":
        return replace(self, calibration=calibration)

    def artifact_fingerprint(self) -> str:
        """Return the exact immutable core payload digest for residual binding."""

        return hashlib.sha256(_canonical_json(self._payload()).encode("utf-8")).hexdigest()

    def is_compatible(self, prediction_as_of: date) -> bool:
        cutoff = normalize_prediction_as_of(prediction_as_of)
        component_dates = [self.evidence_max_date]
        if self.calibration.max_evidence_date is not None:
            component_dates.append(self.calibration.max_evidence_date)
        return all(component_date < cutoff for component_date in component_dates)

    def predict(
        self,
        request: PredictionV2Request,
        *,
        history: PriorEvidence | pd.DataFrame | None = None,
        wood_df: Optional[pd.DataFrame] = None,
    ) -> PredictiveDistribution:
        if not self.is_compatible(request.prediction_as_of):
            return _broad_event_prediction(
                request.event,
                self.model_version,
                warning="artifact_newer_than_prediction_cutoff",
            )
        if self.event_counts.get(request.event, 0) == 0:
            return _broad_event_prediction(
                request.event,
                self.model_version,
                warning="event_absent_from_artifact",
            )

        warnings: list[str] = []
        low, high = self.diameter_support[request.event]
        diameter = float(np.clip(float(request.diameter_mm), low, high))
        outside_support = diameter != float(request.diameter_mm)
        if outside_support:
            warnings.append("diameter_outside_training_support")

        species_missing = (
            bool(request.species_missing)
            or request.species == MISSING_CATEGORY
            or request.species not in self.species_support
        )
        if species_missing:
            warnings.append("unknown_species")
        gender = request.gender
        if gender == MISSING_CATEGORY:
            warnings.append("unknown_gender")

        target = {
            "event": request.event,
            "diameter_mm": diameter,
            "gender": gender,
            "species_missing": species_missing,
            **{name: float(getattr(request, name)) for name in SPECIES_PROPERTY_FIELDS},
        }
        population_mu = self._population_location(pd.DataFrame([target]))[0]
        effective_wood_df = wood_df if wood_df is not None else self.species_property_frame()
        history_frame = _canonical_history(history, request.prediction_as_of, effective_wood_df)
        if not history_frame.empty:
            history_frame = history_frame[
                history_frame["competitor_id"].astype(str) == str(request.competitor_id)
            ].copy()

        states: dict[str, _CompetitorState] = {}
        for event in _EVENTS:
            event_history = history_frame[history_frame["event"] == event].copy()
            states[event] = self._competitor_state(
                event_history,
                event=event,
                prediction_as_of=request.prediction_as_of,
            )

        same = states[request.event]
        other_event = "UH" if request.event == "SB" else "SB"
        other = states[other_event]
        cross_key = f"{request.event}_from_{other_event}"
        cross = self.cross_event_coefficients.get(cross_key, 0.0) * other.location
        cross *= max(0.0, 1.0 - same.shrinkage)
        log_location = population_mu + same.location + same.trend_projection + cross

        base_scale = self.event_scales[request.event]
        # Epistemic state uncertainty contracts smoothly while preserving the
        # irreducible robust event-performance scale.
        log_scale = base_scale * math.sqrt(1.0 + 4.0 / (same.effective_weight + 1.0))
        metadata_uncertainty_multiplier = 1.0
        if species_missing:
            metadata_uncertainty_multiplier *= 1.15
        if gender == MISSING_CATEGORY:
            metadata_uncertainty_multiplier *= 1.10
        if outside_support:
            metadata_uncertainty_multiplier *= 1.20
        log_scale *= metadata_uncertainty_multiplier
        log_scale = max(float(log_scale), 0.05)

        calibration = self.calibration.radius(request.event, same.count)
        if calibration.calibrated and calibration.value is not None:
            radius = float(calibration.value) * metadata_uncertainty_multiplier
            calibration_state = "calibrated"
            calibration_scope = calibration.scope
            sampling_scale = max(log_scale, radius / NORMAL_90_RADIUS)
        else:
            radius = NORMAL_90_RADIUS * log_scale
            calibration_state = "analytic_fallback"
            calibration_scope = "analytic"
            sampling_scale = log_scale

        median = math.exp(log_location)
        interval = ForecastInterval(
            lower=math.exp(log_location - radius),
            upper=math.exp(log_location + radius),
            nominal_coverage=self.calibration.nominal_coverage,
            calibration_state=calibration_state,
            scope=calibration_scope,
        )
        source = (
            "hierarchical_dynamic_core"
            if same.count or other.count
            else "conditional_population_prior"
        )
        return PredictiveDistribution(
            median=median,
            log_location=log_location,
            log_scale=sampling_scale,
            interval=interval,
            source=source,
            history_count=same.count,
            effective_history_weight=same.effective_weight,
            warnings=tuple(warnings),
            degraded=False,
            model_version=self.model_version,
            calibration_version=self.calibration.version,
            metadata={
                "same_event_state": same.location,
                "trend_projection": same.trend_projection,
                "cross_event_state": cross,
                "diameter_used_mm": diameter,
                "shared_log_scale": min(base_scale * 0.25, sampling_scale * 0.5),
                "calibration_sample_count": calibration.sample_count,
            },
        )

    def resolve_species_properties(self, species: Any) -> tuple[dict[str, float], bool]:
        """Resolve a request species from the exact lookup packaged with this core."""

        key = str(species or "").strip().upper()
        values = self.species_properties.get(key)
        if values is None:
            return dict(self.property_means), True
        return {name: float(values[name]) for name in SPECIES_PROPERTY_FIELDS}, False

    def species_property_frame(self) -> pd.DataFrame:
        """Return a canonical lookup frame for request-owned history preparation."""

        rows = [
            {"species": species, **dict(values)}
            for species, values in sorted(self.species_properties.items())
        ]
        return pd.DataFrame(rows, columns=("species", *SPECIES_PROPERTY_FIELDS))

    def _population_location(self, frame: pd.DataFrame) -> np.ndarray:
        matrix = _design_matrix(
            frame,
            property_means=self.property_means,
            property_scales=self.property_scales,
            diameter_support=self.diameter_support,
        )
        return matrix @ np.asarray(self.coefficients)

    def _competitor_state(
        self,
        history: pd.DataFrame,
        *,
        event: str,
        prediction_as_of: date,
    ) -> "_CompetitorState":
        if history.empty:
            return _CompetitorState()
        residuals = np.log(
            history["time_seconds"].astype(float).to_numpy()
        ) - self._population_location(history)
        dates = pd.to_datetime(history["result_date"]).dt.date.to_list()
        ages = np.asarray(
            [(prediction_as_of - result_date).days for result_date in dates], dtype=float
        )
        recency = np.exp(-math.log(2.0) * ages / RECENCY_HALF_LIFE_DAYS)
        robust = _huber_location_weights(residuals, self.event_scales[event])
        weights = recency * robust
        effective_weight = float(np.sum(weights))
        if effective_weight <= 1e-12:
            return _CompetitorState(count=len(history))
        center = float(np.sum(weights * residuals) / effective_weight)
        shrinkage = effective_weight / (effective_weight + SAME_EVENT_SHRINKAGE)
        location = center * shrinkage

        trend_projection = 0.0
        if len(history) >= 3 and (max(dates) - min(dates)).days >= MIN_TREND_SPAN_DAYS:
            latest = max(dates)
            years = np.asarray([(result_date - latest).days / 365.25 for result_date in dates])
            x_mean = float(np.sum(weights * years) / effective_weight)
            y_mean = float(np.sum(weights * residuals) / effective_weight)
            denominator = float(np.sum(weights * (years - x_mean) ** 2))
            if denominator > 1e-12:
                slope = float(
                    np.sum(weights * (years - x_mean) * (residuals - y_mean)) / denominator
                )
                slope *= effective_weight / (effective_weight + TREND_SHRINKAGE)
                slope = float(np.clip(slope, -TREND_CAP_LOG_PER_YEAR, TREND_CAP_LOG_PER_YEAR))
                projection_years = min(max((prediction_as_of - latest).days / 365.25, 0.0), 1.0)
                trend_projection = slope * projection_years

        return _CompetitorState(
            count=len(history),
            effective_weight=effective_weight,
            shrinkage=shrinkage,
            location=location,
            trend_projection=trend_projection,
        )

    def to_json(self) -> str:
        payload = self._payload()
        payload_bytes = _canonical_json(payload).encode("utf-8")
        envelope = {
            "schema": ARTIFACT_SCHEMA,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "payload_bytes": len(payload_bytes),
            "payload_checksum": hashlib.sha256(payload_bytes).hexdigest(),
            "payload": payload,
        }
        encoded = _canonical_json(envelope)
        if len(encoded.encode("utf-8")) > ARTIFACT_MAX_BYTES:
            raise ValueError("artifact exceeds maximum safe size")
        return encoded

    def _payload(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "model_version": self.model_version,
            "canonicalization_version": self.canonicalization_version,
            "training_cutoff": self.training_cutoff.isoformat(),
            "evidence_max_date": self.evidence_max_date.isoformat(),
            "source_checksum": self.source_checksum,
            "active_allowlist": list(MODEL_EVIDENCE_FIELDS),
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients),
            "property_means": dict(self.property_means),
            "property_scales": dict(self.property_scales),
            "diameter_support": {key: list(value) for key, value in self.diameter_support.items()},
            "event_scales": dict(self.event_scales),
            "event_counts": dict(self.event_counts),
            "species_support": list(self.species_support),
            "species_properties": {
                species: dict(values) for species, values in sorted(self.species_properties.items())
            },
            "cross_event_coefficients": dict(self.cross_event_coefficients),
            "calibration": self.calibration.to_dict(),
            "validation_metrics": dict(self.validation_metrics),
        }

    @classmethod
    def from_json(cls, encoded: str | bytes) -> "PredictionV2Model":
        raw = encoded.encode("utf-8") if isinstance(encoded, str) else bytes(encoded)
        if len(raw) > ARTIFACT_MAX_BYTES:
            raise ValueError("artifact exceeds maximum safe size")
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise ValueError("artifact is not valid JSON") from exc
        if not isinstance(envelope, dict):
            raise ValueError("artifact envelope must be an object")
        schema_version = envelope.get("schema_version")
        if envelope.get("schema") != ARTIFACT_SCHEMA or schema_version not in {1, 2}:
            raise ValueError("unsupported prediction artifact schema")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("artifact payload must be an object")
        payload_bytes = _canonical_json(payload).encode("utf-8")
        checksum = hashlib.sha256(payload_bytes).hexdigest()
        if envelope.get("payload_checksum") != checksum:
            raise ValueError("artifact checksum mismatch")
        if envelope.get("payload_bytes") != len(payload_bytes):
            raise ValueError("artifact payload size mismatch")
        _validate_json_tree(payload)
        required = {
            "engine_version",
            "model_version",
            "canonicalization_version",
            "training_cutoff",
            "evidence_max_date",
            "source_checksum",
            "active_allowlist",
            "feature_names",
            "coefficients",
            "property_means",
            "property_scales",
            "diameter_support",
            "event_scales",
            "event_counts",
            "species_support",
            "cross_event_coefficients",
            "calibration",
            "validation_metrics",
        }
        if schema_version == 2:
            required.add("species_properties")
        if set(payload) != required:
            raise ValueError("artifact payload fields do not match schema")
        if payload["canonicalization_version"] != CANONICALIZATION_VERSION:
            raise ValueError("artifact canonicalization version is incompatible")
        if payload["active_allowlist"] != list(MODEL_EVIDENCE_FIELDS):
            raise ValueError("artifact active allowlist is incompatible")
        feature_names = tuple(str(value) for value in payload["feature_names"])
        coefficients = tuple(float(value) for value in payload["coefficients"])
        if feature_names != _feature_names() or len(coefficients) != len(feature_names):
            raise ValueError("artifact feature schema is incompatible")
        property_means = _validated_property_mapping(payload["property_means"], "property means")
        property_scales = _validated_property_mapping(
            payload["property_scales"], "property scales", positive=True
        )
        raw_species_properties = payload.get("species_properties", {})
        if not isinstance(raw_species_properties, Mapping):
            raise ValueError("artifact species properties must be an object")
        species_properties: dict[str, dict[str, float]] = {}
        for species, values in raw_species_properties.items():
            species_key = str(species).strip().upper()
            if not species_key or species_key == MISSING_CATEGORY:
                raise ValueError("artifact species property key is invalid")
            species_properties[species_key] = _validated_property_mapping(
                values, f"species {species_key} properties"
            )
        species_support = tuple(str(value).strip().upper() for value in payload["species_support"])
        if schema_version == 2 and set(species_properties) != set(species_support):
            raise ValueError("artifact species lookup does not match species support")
        diameter_support = _validated_event_pair_mapping(payload["diameter_support"])
        event_scales = _validated_event_scalar_mapping(payload["event_scales"], positive=True)
        event_counts = _validated_event_count_mapping(payload["event_counts"])
        model = cls(
            engine_version=str(payload["engine_version"]),
            model_version=str(payload["model_version"]),
            canonicalization_version=str(payload["canonicalization_version"]),
            training_cutoff=date.fromisoformat(payload["training_cutoff"]),
            evidence_max_date=date.fromisoformat(payload["evidence_max_date"]),
            source_checksum=str(payload["source_checksum"]),
            feature_names=feature_names,
            coefficients=coefficients,
            property_means=property_means,
            property_scales=property_scales,
            diameter_support=diameter_support,
            event_scales=event_scales,
            event_counts=event_counts,
            species_support=species_support,
            species_properties=species_properties,
            cross_event_coefficients={
                key: float(value) for key, value in payload["cross_event_coefficients"].items()
            },
            calibration=ChronologicalCalibrator.from_dict(payload["calibration"]),
            validation_metrics={
                key: float(value) for key, value in payload["validation_metrics"].items()
            },
        )
        source_checksum = model.source_checksum.lower()
        if len(source_checksum) != 64 or any(
            character not in "0123456789abcdef" for character in source_checksum
        ):
            raise ValueError("artifact source checksum is invalid")
        if model.engine_version != ENGINE_VERSION:
            raise ValueError("artifact engine version is incompatible")
        return model


def _validated_property_mapping(
    raw: Any, label: str, *, positive: bool = False
) -> dict[str, float]:
    if not isinstance(raw, Mapping) or set(raw) != set(SPECIES_PROPERTY_FIELDS):
        raise ValueError(f"artifact {label} fields are incompatible")
    result = {name: float(raw[name]) for name in SPECIES_PROPERTY_FIELDS}
    if any(not math.isfinite(value) or (positive and value <= 0) for value in result.values()):
        raise ValueError(f"artifact {label} values are invalid")
    return result


def _validated_event_pair_mapping(raw: Any) -> dict[str, tuple[float, float]]:
    if not isinstance(raw, Mapping) or set(raw) != set(_EVENTS):
        raise ValueError("artifact diameter support event fields are incompatible")
    result: dict[str, tuple[float, float]] = {}
    for event in _EVENTS:
        values = raw[event]
        if not isinstance(values, (list, tuple)) or len(values) != 2:
            raise ValueError("artifact diameter support values are invalid")
        pair = (float(values[0]), float(values[1]))
        if any(not math.isfinite(value) or value <= 0 for value in pair) or pair[0] > pair[1]:
            raise ValueError("artifact diameter support values are invalid")
        result[event] = pair
    return result


def _validated_event_scalar_mapping(raw: Any, *, positive: bool = False) -> dict[str, float]:
    if not isinstance(raw, Mapping) or set(raw) != set(_EVENTS):
        raise ValueError("artifact event scale fields are incompatible")
    result = {event: float(raw[event]) for event in _EVENTS}
    if any(not math.isfinite(value) or (positive and value <= 0) for value in result.values()):
        raise ValueError("artifact event scales are invalid")
    return result


def _validated_event_count_mapping(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping) or set(raw) != set(_EVENTS):
        raise ValueError("artifact event count fields are incompatible")
    result = {event: int(raw[event]) for event in _EVENTS}
    if any(value < 0 for value in result.values()):
        raise ValueError("artifact event counts are invalid")
    return result


@dataclass(frozen=True)
class _CompetitorState:
    count: int = 0
    effective_weight: float = 0.0
    shrinkage: float = 0.0
    location: float = 0.0
    trend_projection: float = 0.0


def _feature_names() -> tuple[str, ...]:
    return (
        "intercept_SB",
        "intercept_UH",
        "log_diameter_SB",
        "log_diameter_UH",
        *(f"species_{name}_z" for name in SPECIES_PROPERTY_FIELDS),
        "species_missing",
        "SB_gender_F",
        "SB_gender_missing",
        "UH_gender_F",
        "UH_gender_missing",
    )


def _design_matrix(
    frame: pd.DataFrame,
    *,
    property_means: Mapping[str, float],
    property_scales: Mapping[str, float],
    diameter_support: Mapping[str, tuple[float, float]],
) -> np.ndarray:
    count = len(frame)
    event = frame["event"].astype(str).str.upper().to_numpy()
    diameter = frame["diameter_mm"].astype(float).to_numpy().copy()
    for event_name in _EVENTS:
        mask = event == event_name
        low, high = diameter_support[event_name]
        diameter[mask] = np.clip(diameter[mask], low, high)
    log_diameter = np.log(diameter / 300.0)
    columns: list[np.ndarray] = [
        (event == "SB").astype(float),
        (event == "UH").astype(float),
        log_diameter * (event == "SB"),
        log_diameter * (event == "UH"),
    ]
    for name in SPECIES_PROPERTY_FIELDS:
        values = pd.to_numeric(frame[name], errors="coerce").fillna(property_means[name]).to_numpy()
        columns.append((values - property_means[name]) / property_scales[name])
    columns.append(frame["species_missing"].astype(bool).astype(float).to_numpy())
    gender = frame["gender"].astype(str).str.upper().to_numpy()
    columns.extend(
        [
            ((event == "SB") & (gender == "F")).astype(float),
            ((event == "SB") & ~np.isin(gender, ["M", "F"])).astype(float),
            ((event == "UH") & (gender == "F")).astype(float),
            ((event == "UH") & ~np.isin(gender, ["M", "F"])).astype(float),
        ]
    )
    return np.column_stack(columns)


def _fit_huber_ridge(matrix: np.ndarray, target: np.ndarray) -> np.ndarray:
    penalty = np.eye(matrix.shape[1]) * RIDGE_PENALTY
    # Both event-specific intercepts are unpenalized.  Every slope, property,
    # missing flag, and event-by-gender contrast receives the fixed ridge.
    penalty[0, 0] = 0.0
    penalty[1, 1] = 0.0
    coefficients = _ridge_solve(matrix, target, np.ones(len(target)), penalty)
    for _ in range(IRLS_MAX_ITERATIONS):
        residuals = target - matrix @ coefficients
        scale = _robust_scale(residuals, floor=1e-6)
        threshold = HUBER_THRESHOLD * scale
        absolute = np.abs(residuals)
        weights = np.ones_like(absolute)
        mask = absolute > threshold
        weights[mask] = threshold / absolute[mask]
        updated = _ridge_solve(matrix, target, weights, penalty)
        if float(np.max(np.abs(updated - coefficients))) < IRLS_TOLERANCE:
            coefficients = updated
            break
        coefficients = updated
    return coefficients


def _ridge_solve(
    matrix: np.ndarray,
    target: np.ndarray,
    weights: np.ndarray,
    penalty: np.ndarray,
) -> np.ndarray:
    """Solve a weighted ridge system, including a structurally absent event."""

    weighted_matrix = matrix * weights[:, None]
    system = matrix.T @ weighted_matrix + penalty
    right_hand_side = matrix.T @ (weights * target)
    return np.linalg.pinv(system, rcond=1e-12) @ right_hand_side


def _robust_scale(values: np.ndarray, *, floor: float) -> float:
    if len(values) == 0:
        return floor
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    return max(1.4826 * mad, floor)


def _huber_location_weights(residuals: np.ndarray, scale: float) -> np.ndarray:
    threshold = HUBER_THRESHOLD * max(scale, 1e-6)
    centered = residuals - np.median(residuals)
    absolute = np.abs(centered)
    weights = np.ones_like(absolute)
    mask = absolute > threshold
    weights[mask] = threshold / absolute[mask]
    return weights


def _learn_cross_event_coefficients(frame: pd.DataFrame) -> dict[str, float]:
    summaries = frame.groupby(["competitor_id", "event"], sort=True)["residual"].median().unstack()
    summaries = summaries.reindex(columns=list(_EVENTS))
    paired = summaries.dropna(subset=["SB", "UH"])
    if len(paired) < CROSS_EVENT_MIN_PAIRS:
        return {"SB_from_UH": 0.0, "UH_from_SB": 0.0}

    def coefficient(source: str, target: str) -> float:
        x = paired[source].to_numpy(dtype=float)
        y = paired[target].to_numpy(dtype=float)
        x = x - np.median(x)
        y = y - np.median(y)
        value = float(np.dot(x, y) / (np.dot(x, x) + CROSS_EVENT_RIDGE))
        return float(np.clip(value, -CROSS_EVENT_CAP, CROSS_EVENT_CAP))

    return {
        "SB_from_UH": coefficient("UH", "SB"),
        "UH_from_SB": coefficient("SB", "UH"),
    }


def _canonical_evidence(evidence: PriorEvidence | pd.DataFrame, cutoff: date) -> pd.DataFrame:
    if isinstance(evidence, PriorEvidence):
        frame = evidence.rows.copy()
    elif set(MODEL_EVIDENCE_FIELDS).issubset(evidence.columns):
        frame = evidence.loc[:, MODEL_EVIDENCE_FIELDS].copy()
    else:
        frame = build_prior_evidence(evidence, cutoff).rows
    return _validate_canonical_rows(frame, cutoff)


def _canonical_history(
    history: PriorEvidence | pd.DataFrame | None,
    cutoff: date,
    wood_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    if history is None:
        return pd.DataFrame(columns=MODEL_EVIDENCE_FIELDS)
    if isinstance(history, PriorEvidence):
        frame = history.rows.copy()
    elif set(MODEL_EVIDENCE_FIELDS).issubset(history.columns):
        frame = history.loc[:, MODEL_EVIDENCE_FIELDS].copy()
    else:
        frame = build_prior_evidence(history, cutoff, wood_df=wood_df).rows
    return _validate_canonical_rows(frame, normalize_prediction_as_of(cutoff))


def _validate_canonical_rows(frame: pd.DataFrame, cutoff: date) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=MODEL_EVIDENCE_FIELDS)
    frame = frame.copy()
    frame["result_date"] = pd.to_datetime(frame["result_date"], errors="coerce", utc=True).dt.date
    frame["time_seconds"] = pd.to_numeric(frame["time_seconds"], errors="coerce")
    frame["diameter_mm"] = pd.to_numeric(frame["diameter_mm"], errors="coerce")
    for name in SPECIES_PROPERTY_FIELDS:
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    finite_properties = np.all(np.isfinite(frame[list(SPECIES_PROPERTY_FIELDS)].to_numpy()), axis=1)
    mask = (
        frame["competitor_id"].notna()
        & frame["event"].astype(str).str.upper().isin(_EVENTS)
        & np.isfinite(frame["time_seconds"])
        & (frame["time_seconds"] > 0)
        & frame["result_date"].notna()
        & (frame["result_date"] < cutoff)
        & np.isfinite(frame["diameter_mm"])
        & (frame["diameter_mm"] > 0)
        & finite_properties
    )
    clean = frame.loc[mask, MODEL_EVIDENCE_FIELDS].copy()
    clean["event"] = clean["event"].astype(str).str.upper()
    clean["gender"] = clean["gender"].astype(str).str.upper()
    clean["species"] = clean["species"].astype(str)
    clean["competitor_id"] = clean["competitor_id"].astype(str)
    clean["species_missing"] = clean["species_missing"].astype(bool)
    return clean.reset_index(drop=True)


def _frame_checksum(frame: pd.DataFrame) -> str:
    records = []
    for row in frame.sort_values(
        ["result_date", "competitor_id", "event"], kind="mergesort"
    ).to_dict("records"):
        records.append(
            {
                key: value.isoformat() if isinstance(value, date) else _json_scalar(value)
                for key, value in row.items()
            }
        )
    return hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()


def _json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


def _broad_event_prediction(
    event: str, model_version: str, *, warning: str
) -> PredictiveDistribution:
    median, log_scale = _BROAD_PRIORS[event]
    log_location = math.log(median)
    radius = NORMAL_90_RADIUS * log_scale
    return PredictiveDistribution(
        median=median,
        log_location=log_location,
        log_scale=log_scale,
        interval=ForecastInterval(
            math.exp(log_location - radius),
            math.exp(log_location + radius),
            calibration_state="broad_prior",
            scope="static_event",
        ),
        source="broad_event_prior",
        history_count=0,
        effective_history_weight=0.0,
        warnings=(warning,),
        degraded=True,
        model_version=model_version,
        metadata={"shared_log_scale": 0.0},
    )


def history_band(history_count: int | float) -> str:
    """Return the shared calibration and reporting band for a history count."""

    count = max(0, int(history_count))
    if count == 0:
        return "0"
    if count <= 3:
        return "1-3"
    return "4+"


_history_band = history_band


def _finite_sample_higher_quantile(values: np.ndarray, coverage: float) -> float:
    scores = np.sort(np.asarray(values, dtype=float))
    if len(scores) == 0:
        raise ValueError("at least one conformal score is required")
    rank = min(len(scores), math.ceil((len(scores) + 1) * coverage))
    return float(scores[rank - 1])


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _validate_json_tree(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        raise ValueError("artifact JSON nesting exceeds safe limit")
    if isinstance(value, dict):
        if len(value) > 500:
            raise ValueError("artifact object exceeds safe field count")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 200:
                raise ValueError("artifact contains an invalid key")
            _validate_json_tree(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 10_000:
            raise ValueError("artifact array exceeds safe length")
        for child in value:
            _validate_json_tree(child, depth=depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("artifact contains a non-finite number")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError("artifact contains an unsupported value")


__all__ = [
    "ARTIFACT_MAX_BYTES",
    "ChronologicalCalibrator",
    "ForecastInterval",
    "history_band",
    "PredictionV2Model",
    "PredictionV2Request",
    "PredictiveDistribution",
]
