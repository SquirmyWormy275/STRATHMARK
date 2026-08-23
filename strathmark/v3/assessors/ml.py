"""Deterministic V3 universal-plus-specialist ML inference.

CatBoost produces log-seconds quantiles.  This module repairs crossings, applies the
separate-role PIT calibration map, adds explicit exponential tails, and projects the
result into the common positive-time forecast contract.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.evidence import EvidencePacket, _require_digest
from strathmark.v3.contracts.forecasts import (
    ArtifactIdentity,
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    ForecastWarning,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import deterministic_identifier

if TYPE_CHECKING:
    from strathmark.v3.factory.ml_artifacts import LoadedMLBundle

MODEL_LEVELS = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)
OUTPUT_LEVELS = (0.001, *MODEL_LEVELS, 0.999)
GATE_FEATURE_NAMES = ("log_history_depth", "missing_fraction")
MIN_LOG_SECONDS = math.log(0.001)
MAX_LOG_SECONDS = math.log(600.0)


@dataclass(frozen=True, slots=True)
class SpecialistGate:
    intercept: str
    coefficients: tuple[tuple[str, str], ...]
    schema_version: str = "strathmark-v3-ml-specialist-gate-v1"

    def __post_init__(self) -> None:
        if self.schema_version != "strathmark-v3-ml-specialist-gate-v1":
            raise ValueError("unsupported ML gate schema")
        if canonical_decimal_string(self.intercept) != self.intercept:
            raise ValueError("ML gate intercept must be canonical")
        if not isinstance(self.coefficients, tuple):
            raise ValueError("ML gate coefficients must be immutable")
        names = tuple(name for name, _ in self.coefficients)
        if names != GATE_FEATURE_NAMES:
            raise ValueError(
                "ML gate coefficient names must exactly match the closed schema"
            )
        for name, value in self.coefficients:
            if not name or canonical_decimal_string(value) != value:
                raise ValueError(
                    "ML gate coefficients must be named canonical decimals"
                )

    def weight(
        self, features: Mapping[str, float], *, specialist_available: bool
    ) -> float:
        if not specialist_available:
            return 0.0
        if set(features) != set(GATE_FEATURE_NAMES):
            raise ValueError("ML gate features must exactly match the closed schema")
        score = float(self.intercept)
        for name, coefficient in self.coefficients:
            value = float(features[name])
            if not math.isfinite(value):
                raise ValueError("ML gate features must be finite")
            score += float(coefficient) * value
        score = max(-40.0, min(40.0, score))
        return max(0.1, min(0.9, 1.0 / (1.0 + math.exp(-score))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intercept": self.intercept,
            "coefficients": {name: value for name, value in self.coefficients},
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SpecialistGate:
        if set(value) != {"schema_version", "intercept", "coefficients"}:
            raise ValueError("ML gate fields do not match the closed schema")
        coefficients = value["coefficients"]
        if not isinstance(coefficients, Mapping) or not all(
            isinstance(name, str) and isinstance(item, str)
            for name, item in coefficients.items()
        ):
            raise ValueError("ML gate coefficients must be a string object")
        return cls(
            value["intercept"],
            tuple(sorted(coefficients.items())),
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class PITCalibrator:
    role: str
    points: tuple[tuple[str, str], ...]
    source_digest: str
    schema_version: str = "strathmark-v3-ml-pit-calibrator-v1"

    def __post_init__(self) -> None:
        if self.schema_version != "strathmark-v3-ml-pit-calibrator-v1":
            raise ValueError("unsupported ML PIT calibrator schema")
        if self.role != "calibration":
            raise ValueError("PIT calibrator requires the separate calibration role")
        _require_digest(self.source_digest, "ML calibrator source_digest")
        if not isinstance(self.points, tuple) or len(self.points) < 2:
            raise ValueError("PIT calibrator requires immutable boundary points")
        parsed = [(float(left), float(right)) for left, right in self.points]
        if parsed[0] != (0.0, 0.0) or parsed[-1] != (1.0, 1.0):
            raise ValueError("PIT calibrator must close both probability boundaries")
        if [item[0] for item in parsed] != sorted(item[0] for item in parsed):
            raise ValueError("PIT calibrator inputs must be monotone")
        if any(not 0 <= value <= 1 for item in parsed for value in item):
            raise ValueError("PIT calibrator probabilities must lie in [0, 1]")
        if [item[1] for item in parsed] != sorted(item[1] for item in parsed):
            raise ValueError("PIT calibrator outputs must be monotone isotonic values")

    @classmethod
    def _fit_authorized_values(
        cls, pits: Sequence[float], *, source_digest: str
    ) -> PITCalibrator:
        if not pits or any(
            not math.isfinite(item) or not 0 <= item <= 1 for item in pits
        ):
            raise ValueError("PIT inputs must be finite probabilities")
        ordered = sorted(float(item) for item in pits)
        grouped: list[tuple[float, float]] = []
        for value in sorted(set(ordered)):
            positions = [
                index + 1 for index, item in enumerate(ordered) if item == value
            ]
            empirical = sum(
                position / (len(ordered) + 1) for position in positions
            ) / len(positions)
            grouped.append((value, empirical))
        points = [(0.0, 0.0), *grouped, (1.0, 1.0)]
        collapsed: list[tuple[float, float]] = []
        for point in points:
            if collapsed and point[0] == collapsed[-1][0]:
                collapsed[-1] = (point[0], max(point[1], collapsed[-1][1]))
            else:
                collapsed.append(point)
        collapsed[0] = (0.0, 0.0)
        collapsed[-1] = (1.0, 1.0)
        return cls(
            "calibration",
            tuple(
                (canonical_decimal_string(left), canonical_decimal_string(right))
                for left, right in collapsed
            ),
            source_digest,
        )

    @classmethod
    def identity(cls, *, source_digest: str) -> PITCalibrator:
        return cls("calibration", (("0", "0"), ("1", "1")), source_digest)

    def map_probability(self, probability: float) -> float:
        return _interpolate_points(self.points, probability, inverse=False)

    def inverse_probability(self, probability: float) -> float:
        return _interpolate_points(self.points, probability, inverse=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role": self.role,
            "points": [
                {"raw": left, "calibrated": right} for left, right in self.points
            ],
            "source_digest": self.source_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PITCalibrator:
        if set(value) != {"schema_version", "role", "points", "source_digest"}:
            raise ValueError("ML PIT calibrator fields do not match the closed schema")
        points = value["points"]
        if not isinstance(points, list) or any(
            not isinstance(item, Mapping) or set(item) != {"raw", "calibrated"}
            for item in points
        ):
            raise ValueError("ML PIT calibrator points do not match the closed schema")
        return cls(
            value["role"],
            tuple((item["raw"], item["calibrated"]) for item in points),
            value["source_digest"],
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class MLAssessment:
    forecast: AssessorForecast
    specialist_key: str | None
    specialist_weight: float
    universal_quantiles_ms: tuple[int, ...]
    specialist_quantiles_ms: tuple[int, ...]
    unseen_categories: tuple[str, ...]
    bundle_digest: str
    assessment_digest: str

    @classmethod
    def create(cls, **arguments: Any) -> MLAssessment:
        content = cls._content_value(**arguments)
        return cls(**arguments, assessment_digest=canonical_digest(content))

    @staticmethod
    def _content_value(**arguments: Any) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-ml-assessment-v1",
            "forecast": arguments["forecast"].to_dict(),
            "specialist_key": arguments["specialist_key"],
            "specialist_weight": canonical_decimal_string(
                arguments["specialist_weight"]
            ),
            "universal_quantiles_ms": list(arguments["universal_quantiles_ms"]),
            "specialist_quantiles_ms": list(arguments["specialist_quantiles_ms"]),
            "unseen_categories": list(arguments["unseen_categories"]),
            "bundle_digest": arguments["bundle_digest"],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._content_value(
                forecast=self.forecast,
                specialist_key=self.specialist_key,
                specialist_weight=self.specialist_weight,
                universal_quantiles_ms=self.universal_quantiles_ms,
                specialist_quantiles_ms=self.specialist_quantiles_ms,
                unseen_categories=self.unseen_categories,
                bundle_digest=self.bundle_digest,
            ),
            "assessment_digest": self.assessment_digest,
        }


class MLAssessor:
    """Run one verified, frozen, whole-domain ML bundle against a sealed packet."""

    def __init__(self, bundle: LoadedMLBundle):
        self.bundle = bundle

    def assess(self, packet: EvidencePacket) -> MLAssessment:
        from strathmark.v3.factory.ml_training import (
            build_inference_features,
            canonical_gate_features,
            context_key,
        )

        if (
            self.bundle.metadata.get("taxonomy_version") != packet.taxonomy_version
            or self.bundle.metadata.get("conversion_version")
            != packet.conversion_version
        ):
            raise ValueError(
                "ML bundle taxonomy or conversion metadata is incompatible"
            )
        features = build_inference_features(packet)
        normalized, unseen = self.bundle.normalize_features(features)
        ordered = [[normalized[name] for name in self.bundle.feature_names]]
        universal = _predict_log_quantiles(self.bundle.universal_model, ordered)
        key = context_key(packet.target_context)
        specialist_model = self.bundle.specialist_models.get(key)
        eligibility = self.bundle.specialist_eligibility.get(key)
        available = specialist_model is not None and bool(
            eligibility is not None and eligibility.available
        )
        missing_flags = tuple(
            int(features[name]) for name in features if name.endswith("_missing")
        )
        weight = self.bundle.gate.weight(
            canonical_gate_features(
                history_depth=float(features["history_depth"]),
                missing_fraction=sum(missing_flags) / max(1, len(missing_flags)),
            ),
            specialist_available=available,
        )
        specialist = (
            _predict_log_quantiles(specialist_model, ordered)
            if available
            else universal
        )
        combined = combine_quantiles(universal, specialist, weight)
        distribution = build_positive_distribution(combined, self.bundle.calibrator)
        warnings = ((ForecastWarning.MISSING_CONTEXT,) if unseen else ()) + (
            (ForecastWarning.SPARSE_EVIDENCE,) if not available else ()
        )
        warnings = tuple(sorted(set(warnings), key=lambda item: item.value))
        artifact = ArtifactIdentity(
            "ml_bundle", self.bundle.version, self.bundle.digest
        )
        forecast = AssessorForecast.create(
            forecast_id=deterministic_identifier(
                "forecast",
                {
                    "assessor": "ml",
                    "evidence_digest": packet.content_digest,
                    "bundle_digest": self.bundle.digest,
                },
            ),
            assessor=AssessorKind.ML,
            state=ForecastState.COMMITTED,
            evidence_digest=packet.content_digest,
            distribution=distribution,
            support=EvidenceSupport(
                eligible_count=len(packet.eligible_raw_times_ms),
                effective_weight=canonical_decimal_string(
                    len(packet.eligible_raw_times_ms)
                ),
                exact_context_count=sum(
                    item.context.digest == packet.target_context.digest
                    for item in packet.observations
                    if item.result.raw_time_ms is not None
                ),
                max_historical_key=packet.historical_cutoff_key,
                tournament_event_sequence=packet.tournament_event_sequence,
            ),
            warnings=warnings,
            artifacts=(artifact,),
            abstention_code=None,
        )
        universal_ms = tuple(_log_seconds_to_ms(item) for item in universal)
        specialist_ms = (
            tuple(_log_seconds_to_ms(item) for item in specialist) if available else ()
        )
        return MLAssessment.create(
            forecast=forecast,
            specialist_key=key if available else None,
            specialist_weight=weight,
            universal_quantiles_ms=universal_ms,
            specialist_quantiles_ms=specialist_ms,
            unseen_categories=unseen,
            bundle_digest=self.bundle.digest,
        )


def combine_quantiles(
    universal_log_quantiles: Sequence[float],
    specialist_log_quantiles: Sequence[float],
    specialist_weight: float,
) -> tuple[float, ...]:
    if (
        not math.isfinite(specialist_weight)
        or not 0 <= specialist_weight <= 1
        or len(universal_log_quantiles) != len(MODEL_LEVELS)
        or len(specialist_log_quantiles) != len(MODEL_LEVELS)
    ):
        raise ValueError("ML specialist weight or quantile dimensions are invalid")
    values = [
        (1 - specialist_weight) * float(universal)
        + specialist_weight * float(specialist)
        for universal, specialist in zip(
            universal_log_quantiles, specialist_log_quantiles
        )
    ]
    if any(not math.isfinite(item) for item in values):
        raise ValueError("ML log quantiles must be finite")
    _validate_log_bounds(values)
    return _isotonic_non_decreasing(values)


def build_positive_distribution(
    log_quantiles: Sequence[float], calibrator: PITCalibrator
) -> PositiveTimeDistribution:
    if len(log_quantiles) != len(MODEL_LEVELS):
        raise ValueError("ML distribution requires the frozen seven quantiles")
    repaired = _isotonic_non_decreasing([float(item) for item in log_quantiles])
    _validate_log_bounds(repaired)
    output_logs: list[float] = []
    for probability in OUTPUT_LEVELS:
        raw_probability = calibrator.inverse_probability(probability)
        output_logs.append(_quantile_log_at(raw_probability, repaired))
    output_logs = list(_isotonic_non_decreasing(output_logs))
    rendered_levels = (
        "0.001",
        "0.05",
        "0.1",
        "0.25",
        "0.5",
        "0.75",
        "0.9",
        "0.95",
        "0.999",
    )
    return PositiveTimeDistribution(
        tuple(
            QuantilePoint(probability, _log_seconds_to_ms(value))
            for probability, value in zip(rendered_levels, output_logs)
        )
    )


def _predict_log_quantiles(model: Any, rows: list[list[object]]) -> tuple[float, ...]:
    predicted = model.predict(rows)
    if hasattr(predicted, "tolist"):
        predicted = predicted.tolist()
    values: Any = (
        predicted[0]
        if isinstance(predicted, (list, tuple)) and len(predicted) == 1
        else predicted
    )
    if not isinstance(values, (list, tuple)) or len(values) != len(MODEL_LEVELS):
        raise ValueError("CatBoost MultiQuantile prediction must contain seven values")
    numeric = tuple(float(item) for item in values)
    if any(not math.isfinite(item) for item in numeric):
        raise ValueError("CatBoost MultiQuantile prediction must be finite")
    _validate_log_bounds(numeric)
    return _isotonic_non_decreasing(numeric)


def _isotonic_non_decreasing(values: Sequence[float]) -> tuple[float, ...]:
    blocks: list[list[float]] = []
    for value in values:
        blocks.append([float(value), 1.0])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            right = blocks.pop()
            left = blocks.pop()
            count = left[1] + right[1]
            blocks.append([(left[0] * left[1] + right[0] * right[1]) / count, count])
    result: list[float] = []
    for value, count in blocks:
        result.extend([value] * int(count))
    return tuple(result)


def _quantile_log_at(probability: float, values: Sequence[float]) -> float:
    if probability < MODEL_LEVELS[0]:
        slope = (values[1] - values[0]) / (MODEL_LEVELS[1] - MODEL_LEVELS[0])
        slope = max(slope, 0.25)
        return values[0] + slope * (probability - MODEL_LEVELS[0])
    if probability > MODEL_LEVELS[-1]:
        slope = (values[-1] - values[-2]) / (MODEL_LEVELS[-1] - MODEL_LEVELS[-2])
        slope = max(slope, 0.25)
        return values[-1] + slope * (probability - MODEL_LEVELS[-1])
    index = bisect_left(MODEL_LEVELS, probability)
    if index < len(MODEL_LEVELS) and MODEL_LEVELS[index] == probability:
        return values[index]
    left = index - 1
    ratio = (probability - MODEL_LEVELS[left]) / (
        MODEL_LEVELS[index] - MODEL_LEVELS[left]
    )
    return values[left] + ratio * (values[index] - values[left])


def _interpolate_points(
    points: tuple[tuple[str, str], ...], probability: float, *, inverse: bool
) -> float:
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("calibration probability must lie in [0, 1]")
    pairs = [
        (float(right), float(left)) if inverse else (float(left), float(right))
        for left, right in points
    ]
    pairs.sort()
    index = bisect_left([item[0] for item in pairs], probability)
    if index == 0:
        return pairs[0][1]
    if index == len(pairs):
        return pairs[-1][1]
    left_x, left_y = pairs[index - 1]
    right_x, right_y = pairs[index]
    ratio = (probability - left_x) / (right_x - left_x)
    return left_y + ratio * (right_y - left_y)


def _seconds_to_ms(value: float) -> int:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("ML time predictions must be finite and positive")
    return max(
        1,
        int(
            Decimal(str(value * 1000)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
        ),
    )


def _log_seconds_to_ms(value: float) -> int:
    _validate_log_bounds((value,))
    return _seconds_to_ms(math.exp(value))


def _validate_log_bounds(values: Sequence[float]) -> None:
    if any(
        not math.isfinite(item) or item < MIN_LOG_SECONDS or item > MAX_LOG_SECONDS
        for item in values
    ):
        raise ValueError(
            "ML log-time prediction is outside the frozen positive-time bounds"
        )


__all__ = [
    "MLAssessment",
    "MLAssessor",
    "MODEL_LEVELS",
    "PITCalibrator",
    "SpecialistGate",
    "build_positive_distribution",
    "combine_quantiles",
]
