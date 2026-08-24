"""Deterministic capability-adjusted linear pooling for V3 assessor forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import (
    SAMPLING_ALGORITHM,
    AssessorForecast,
    AssessorKind,
    DistributionSamples,
    ForecastState,
    PositiveTimeDistribution,
    QuantilePoint,
    SamplingSpec,
    _samples_digest,
    _splitmix_uniforms,
)
from strathmark.v3.domain.capability import (
    CAPABILITY_OPERATOR_VERSION,
    CapabilityState,
    apply_capability_operator,
)
from strathmark.v3.domain.credibility import ContextNode, WeightReceipt

_OUTER = (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)
_QUANTILES = (
    Decimal("0.05"),
    Decimal("0.1"),
    Decimal("0.25"),
    Decimal("0.5"),
    Decimal("0.75"),
    Decimal("0.9"),
    Decimal("0.95"),
)


class AvailabilityState(str, Enum):
    VALID = "valid"
    ABSTAINED = "abstained"
    INVALID = "invalid"
    MISSING = "missing"


class PoolMode(str, Enum):
    NORMAL = "normal_three"
    DEGRADED_TWO = "degraded_two"
    MANUAL_SINGLE = "manual_single_survivor"
    MANUAL_REQUIRED = "manual_construction_required"


class WeightAuthorityStatus(str, Enum):
    PENDING = "pending_u12_verifier"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class WeightAuthorityBinding:
    weights: tuple[tuple[AssessorKind, str], ...]
    weight_receipt_digest: str
    context: ContextNode
    calibration_cutoff_at_utc: str
    policy_digest: str
    ledger_projection_digest: str
    tournament_event_sequence: int
    source_global_sequence: int
    verification_status: WeightAuthorityStatus
    binding_digest: str

    def __post_init__(self) -> None:
        _weight_values(self.weights, require_all=True)
        for value, label in (
            (self.weight_receipt_digest, "weight_receipt_digest"),
            (self.policy_digest, "weight policy digest"),
            (self.ledger_projection_digest, "weight ledger projection digest"),
            (self.binding_digest, "weight authority binding digest"),
        ):
            _require_digest(value, label)
        if not isinstance(self.context, ContextNode):
            raise ContractError("weight authority context must be typed")
        if (
            not isinstance(self.calibration_cutoff_at_utc, str)
            or not self.calibration_cutoff_at_utc
        ):
            raise ContractError("weight authority cutoff is required")
        for value, label in (
            (self.tournament_event_sequence, "weight event sequence"),
            (self.source_global_sequence, "weight source sequence"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"{label} must be a nonnegative integer")
        if self.verification_status is not WeightAuthorityStatus.PENDING:
            raise ContractError("U12 trusted weight verifier is required for VERIFIED authority")
        if self.binding_digest != canonical_digest(self.content_value()):
            raise ContractError("weight authority binding digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-weight-authority-binding-v1",
            "weights": [[item.value, value] for item, value in self.weights],
            "weight_receipt_digest": self.weight_receipt_digest,
            "context": self.context.to_dict(),
            "calibration_cutoff_at_utc": self.calibration_cutoff_at_utc,
            "policy_digest": self.policy_digest,
            "ledger_projection_digest": self.ledger_projection_digest,
            "tournament_event_sequence": self.tournament_event_sequence,
            "source_global_sequence": self.source_global_sequence,
            "verification_status": self.verification_status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "binding_digest": self.binding_digest}

    @classmethod
    def pending(
        cls,
        receipt: WeightReceipt,
        *,
        ledger_projection_digest: str,
        tournament_event_sequence: int,
        source_global_sequence: int,
    ) -> WeightAuthorityBinding:
        if not isinstance(receipt, WeightReceipt):
            raise ContractError("weight authority requires a typed weight receipt")
        values = {
            "weights": receipt.weights,
            "weight_receipt_digest": receipt.receipt_digest,
            "context": receipt.context,
            "calibration_cutoff_at_utc": receipt.calibration_cutoff_at_utc,
            "policy_digest": receipt.policy_digest,
            "ledger_projection_digest": ledger_projection_digest,
            "tournament_event_sequence": tournament_event_sequence,
            "source_global_sequence": source_global_sequence,
            "verification_status": WeightAuthorityStatus.PENDING,
        }
        content = {
            "schema_version": "strathmark-v3-weight-authority-binding-v1",
            "weights": [[item.value, value] for item, value in receipt.weights],
            "weight_receipt_digest": receipt.receipt_digest,
            "context": receipt.context.to_dict(),
            "calibration_cutoff_at_utc": receipt.calibration_cutoff_at_utc,
            "policy_digest": receipt.policy_digest,
            "ledger_projection_digest": ledger_projection_digest,
            "tournament_event_sequence": tournament_event_sequence,
            "source_global_sequence": source_global_sequence,
            "verification_status": WeightAuthorityStatus.PENDING.value,
        }
        return cls(**values, binding_digest=canonical_digest(content))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WeightAuthorityBinding:
        expected = {
            "schema_version",
            "weights",
            "weight_receipt_digest",
            "context",
            "calibration_cutoff_at_utc",
            "policy_digest",
            "ledger_projection_digest",
            "tournament_event_sequence",
            "source_global_sequence",
            "verification_status",
            "binding_digest",
        }
        if (
            set(value) != expected
            or value.get("schema_version") != "strathmark-v3-weight-authority-binding-v1"
        ):
            raise ContractError("weight authority fields or schema differ")
        context = value["context"]
        if not isinstance(context, Mapping):
            raise ContractError("weight authority context is invalid")
        try:
            status = WeightAuthorityStatus(value["verification_status"])
        except (TypeError, ValueError) as exc:
            raise ContractError("weight authority status is unknown") from exc
        return cls(
            _decode_weights(value["weights"]),
            value["weight_receipt_digest"],
            ContextNode(
                context.get("event_code"),
                context.get("size_band"),
                context.get("material_group"),
                context.get("history_depth"),
            ),
            value["calibration_cutoff_at_utc"],
            value["policy_digest"],
            value["ledger_projection_digest"],
            value["tournament_event_sequence"],
            value["source_global_sequence"],
            status,
            value["binding_digest"],
        )


@dataclass(frozen=True, slots=True)
class LinearPoolComponent:
    assessor: AssessorKind
    weight: str
    distribution: PositiveTimeDistribution

    def __post_init__(self) -> None:
        if self.assessor not in _OUTER:
            raise ContractError("linear pool accepts outer assessors only")
        weight = _canonical_decimal(self.weight, "linear pool weight")
        if weight <= 0:
            raise ContractError("linear pool component weight must be positive")
        if not isinstance(self.distribution, PositiveTimeDistribution):
            raise ContractError("linear pool component requires a positive distribution")

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessor": self.assessor.value,
            "weight": self.weight,
            "distribution": self.distribution.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LinearPoolComponent:
        if set(value) != {"assessor", "weight", "distribution"}:
            raise ContractError("linear pool component fields differ")
        try:
            assessor = AssessorKind(value["assessor"])
        except (TypeError, ValueError) as exc:
            raise ContractError("linear pool assessor is unknown") from exc
        distribution = value["distribution"]
        if not isinstance(distribution, Mapping):
            raise ContractError("linear pool distribution is invalid")
        return cls(assessor, value["weight"], PositiveTimeDistribution.from_dict(distribution))


@dataclass(frozen=True, slots=True)
class LinearPooledDistribution:
    """A sealed weighted mixture whose sampler retains every component mode."""

    components: tuple[LinearPoolComponent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.components, tuple) or len(self.components) < 2:
            raise ContractError("linear pool requires at least two immutable components")
        assessors = tuple(item.assessor for item in self.components)
        if assessors != tuple(item for item in _OUTER if item in assessors):
            raise ContractError("linear pool components must be unique and canonically ordered")
        if sum((Decimal(item.weight) for item in self.components), Decimal(0)) != 1:
            raise ContractError("linear pool effective weights must sum exactly to one")

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    @property
    def median_ms(self) -> int:
        return self._at_probability(Decimal("0.5"))

    def sample(self, spec: SamplingSpec) -> DistributionSamples:
        if not isinstance(spec, SamplingSpec):
            raise ContractError("linear pool sampling requires a SamplingSpec")
        uniforms = spec.common_uniforms or _splitmix_uniforms(spec.seed, spec.draw_count)
        samples = tuple(self._sample_at_uniform(Decimal(value)) for value in uniforms)
        return DistributionSamples(
            samples_ms=samples,
            algorithm=SAMPLING_ALGORITHM,
            dependency_version="stdlib-only-v1",
            seed=spec.seed,
            draw_count=spec.draw_count,
            time_quantum_ms=1,
            distribution_digest=self.digest,
            samples_digest=_samples_digest(
                samples_ms=samples,
                seed=spec.seed,
                distribution_digest=self.digest,
                common_random_map_digest=spec.common_random_map_digest,
            ),
            common_random_map_digest=spec.common_random_map_digest,
        )

    def quantile_summary(self) -> PositiveTimeDistribution:
        return PositiveTimeDistribution(
            tuple(
                QuantilePoint(_decimal_string(probability), self._at_probability(probability))
                for probability in _QUANTILES
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-linear-pooled-distribution-v1",
            "algorithm": "weighted-linear-opinion-pool-v1",
            "components": [item.to_dict() for item in self.components],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LinearPooledDistribution:
        if (
            set(value) != {"schema_version", "algorithm", "components"}
            or value["schema_version"] != "strathmark-v3-linear-pooled-distribution-v1"
            or value["algorithm"] != "weighted-linear-opinion-pool-v1"
            or not isinstance(value["components"], list)
        ):
            raise ContractError("linear pooled distribution fields or algorithm differ")
        return cls(tuple(LinearPoolComponent.from_dict(item) for item in value["components"]))

    def _sample_at_uniform(self, probability: Decimal) -> int:
        left = Decimal(0)
        for index, component in enumerate(self.components):
            right = left + Decimal(component.weight)
            if probability < right or index == len(self.components) - 1:
                local = (probability - left) / Decimal(component.weight)
                return component.distribution._at_probability(local)
            left = right
        raise AssertionError(  # pragma: no cover - validated weights cover the unit interval
            "validated mixture weights did not cover the unit interval"
        )

    def _at_probability(self, probability: Decimal) -> int:
        low = min(item.distribution.quantiles[0].time_ms for item in self.components)
        high = max(item.distribution.quantiles[-1].time_ms for item in self.components)
        while low < high:
            midpoint = (low + high) // 2
            if self._cdf(midpoint) >= probability:
                high = midpoint
            else:
                low = midpoint + 1
        return low

    def _cdf(self, time_ms: int) -> Decimal:
        return sum(
            (
                Decimal(item.weight) * _distribution_cdf(item.distribution, time_ms)
                for item in self.components
            ),
            Decimal(0),
        )


@dataclass(frozen=True, slots=True)
class PoolComponentReceipt:
    assessor: AssessorKind
    availability: AvailabilityState
    availability_reason: str
    baseline_weight: str
    effective_weight: str
    forecast_id: str | None
    forecast_commit_digest: str | None
    original_distribution: PositiveTimeDistribution | None
    adjusted_distribution: PositiveTimeDistribution | None
    capability_adjustment_digest: str | None
    samples_digest: str | None

    def __post_init__(self) -> None:
        if self.assessor not in _OUTER or not isinstance(self.availability, AvailabilityState):
            raise ContractError("pool component identity is invalid")
        if not isinstance(self.availability_reason, str) or not self.availability_reason:
            raise ContractError("pool component availability reason is required")
        baseline = _canonical_decimal(self.baseline_weight, "component baseline weight")
        effective = _canonical_decimal(self.effective_weight, "component effective weight")
        if baseline < 0 or effective < 0:
            raise ContractError("pool component weights must be nonnegative")
        for value, label in (
            (self.forecast_commit_digest, "forecast_commit_digest"),
            (self.capability_adjustment_digest, "capability_adjustment_digest"),
            (self.samples_digest, "samples_digest"),
        ):
            if value is not None:
                _require_digest(value, label)
        if self.availability is AvailabilityState.VALID:
            if not all(
                (
                    self.forecast_id,
                    self.forecast_commit_digest,
                    isinstance(self.original_distribution, PositiveTimeDistribution),
                    isinstance(self.adjusted_distribution, PositiveTimeDistribution),
                    self.capability_adjustment_digest,
                    self.samples_digest,
                )
            ):
                raise ContractError("valid pool component audit evidence is incomplete")
        elif (
            any(
                value is not None
                for value in (
                    self.original_distribution,
                    self.adjusted_distribution,
                    self.capability_adjustment_digest,
                    self.samples_digest,
                )
            )
            or effective
        ):
            raise ContractError("unavailable pool component cannot carry forecast influence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessor": self.assessor.value,
            "availability": self.availability.value,
            "availability_reason": self.availability_reason,
            "baseline_weight": self.baseline_weight,
            "effective_weight": self.effective_weight,
            "forecast_id": self.forecast_id,
            "forecast_commit_digest": self.forecast_commit_digest,
            "original_distribution": (
                self.original_distribution.to_dict() if self.original_distribution else None
            ),
            "adjusted_distribution": (
                self.adjusted_distribution.to_dict() if self.adjusted_distribution else None
            ),
            "capability_adjustment_digest": self.capability_adjustment_digest,
            "samples_digest": self.samples_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PoolComponentReceipt:
        expected = {
            "assessor",
            "availability",
            "availability_reason",
            "baseline_weight",
            "effective_weight",
            "forecast_id",
            "forecast_commit_digest",
            "original_distribution",
            "adjusted_distribution",
            "capability_adjustment_digest",
            "samples_digest",
        }
        if set(value) != expected:
            raise ContractError("pool component receipt fields differ")
        try:
            assessor = AssessorKind(value["assessor"])
            availability = AvailabilityState(value["availability"])
        except (TypeError, ValueError) as exc:
            raise ContractError("pool component receipt vocabulary is unknown") from exc
        original = value["original_distribution"]
        adjusted = value["adjusted_distribution"]
        return cls(
            assessor,
            availability,
            value["availability_reason"],
            value["baseline_weight"],
            value["effective_weight"],
            value["forecast_id"],
            value["forecast_commit_digest"],
            (
                PositiveTimeDistribution.from_dict(original)
                if isinstance(original, Mapping)
                else None
            ),
            (
                PositiveTimeDistribution.from_dict(adjusted)
                if isinstance(adjusted, Mapping)
                else None
            ),
            value["capability_adjustment_digest"],
            value["samples_digest"],
        )


@dataclass(frozen=True, slots=True)
class PoolReceipt:
    mode: PoolMode
    available_count: int
    is_ensemble: bool
    baseline_weights: tuple[tuple[AssessorKind, str], ...]
    weight_authority: WeightAuthorityBinding
    effective_weights: tuple[tuple[AssessorKind, str], ...]
    normalization_denominator: str
    missing_mass: str
    capability_operator_version: str
    capability_state_digest: str
    components: tuple[PoolComponentReceipt, ...]
    pooled_distribution: LinearPooledDistribution | PositiveTimeDistribution | None
    pooled_summary: PositiveTimeDistribution | None
    pooled_samples_ms: tuple[int, ...] | None
    pooled_samples_digest: str | None
    seed: int
    draw_count: int
    algorithm: str
    dependency_version: str
    time_quantum_ms: int
    common_random_map_digest: str
    common_uniforms: tuple[str, ...]
    source_common_random_map_digest: str | None
    receipt_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.mode, PoolMode):
            raise ContractError("pool receipt mode must be typed")
        if not isinstance(self.components, tuple):
            raise ContractError("pool receipt components must be immutable")
        if not all(isinstance(item, PoolComponentReceipt) for item in self.components):
            raise ContractError("pool receipt components must be typed")
        if tuple(item.assessor for item in self.components) != _OUTER:
            raise ContractError("pool receipt must include all outer components in order")
        valid = tuple(
            item.assessor
            for item in self.components
            if item.availability is AvailabilityState.VALID
        )
        if self.available_count != len(valid):
            raise ContractError("pool receipt available_count differs from component evidence")
        expected_mode = _mode(self.available_count, self.mode is PoolMode.MANUAL_SINGLE)
        if self.mode is not expected_mode:
            raise ContractError("pool mode is inconsistent with assessor availability")
        if self.is_ensemble != (self.mode in {PoolMode.NORMAL, PoolMode.DEGRADED_TWO}):
            raise ContractError("pool ensemble flag is inconsistent with mode")
        baseline = _weight_values(self.baseline_weights, require_all=True)
        if (
            not isinstance(self.weight_authority, WeightAuthorityBinding)
            or self.weight_authority.weights != self.baseline_weights
        ):
            raise ContractError("pool baseline differs from weight authority binding")
        effective = _weight_values(self.effective_weights, require_all=False)
        if tuple(effective) != valid:
            raise ContractError("effective weights must cover exactly the valid assessors")
        with localcontext() as context:
            context.prec = 96
            denominator = _canonical_decimal(
                self.normalization_denominator, "normalization_denominator"
            )
            missing = _canonical_decimal(self.missing_mass, "missing_mass")
            if denominator != sum((baseline[item] for item in valid), Decimal(0)):
                raise ContractError("pool normalization denominator differs from baseline")
            if missing != Decimal(1) - denominator:
                raise ContractError("pool missing mass differs from baseline")
            if valid and sum(effective.values(), Decimal(0)) != 1:
                raise ContractError("pool effective weights must sum exactly to one")
        if self.capability_operator_version != CAPABILITY_OPERATOR_VERSION:
            raise ContractError("pool capability operator version differs")
        _require_digest(self.capability_state_digest, "capability_state_digest")
        has_output = self.mode is not PoolMode.MANUAL_REQUIRED
        if has_output != (
            self.pooled_distribution is not None and self.pooled_samples_digest is not None
        ):
            raise ContractError("pool output presence is inconsistent with mode")
        if self.pooled_distribution is not None and not isinstance(
            self.pooled_distribution,
            (PositiveTimeDistribution, LinearPooledDistribution),
        ):
            raise ContractError("pool distribution authority is invalid")
        if has_output != isinstance(self.pooled_summary, PositiveTimeDistribution):
            raise ContractError("pool summary presence or type is inconsistent with mode")
        if self.pooled_samples_digest is not None:
            _require_digest(self.pooled_samples_digest, "pooled_samples_digest")
        if has_output != (self.pooled_samples_ms is not None):
            raise ContractError("pool sample authority presence is inconsistent with mode")
        if (
            not isinstance(self.common_uniforms, tuple)
            or len(self.common_uniforms) != self.draw_count
        ):
            raise ContractError("pool common uniforms must cover every draw")
        if self.source_common_random_map_digest is not None:
            _require_digest(self.source_common_random_map_digest, "source_common_random_map_digest")
        if self.algorithm != _algorithm(self.mode) or self.dependency_version != "stdlib-only-v1":
            raise ContractError("pool algorithm or dependency version differs")
        if self.time_quantum_ms != 1:
            raise ContractError("pool time quantum differs")
        SamplingSpec(self.seed, self.draw_count)
        _require_digest(self.common_random_map_digest, "common_random_map_digest")
        available_components = tuple(
            item for item in self.components if item.availability is AvailabilityState.VALID
        )
        for item in self.components:
            if item.baseline_weight != dict(self.baseline_weights)[
                item.assessor
            ] or item.effective_weight != dict(self.effective_weights).get(item.assessor, "0"):
                raise ContractError("pool component weights differ from top-level authority")
        if self.mode in {PoolMode.NORMAL, PoolMode.DEGRADED_TWO}:
            if not isinstance(self.pooled_distribution, LinearPooledDistribution):
                raise ContractError("ensemble mode requires a sealed linear mixture")
            expected_components = tuple(
                LinearPoolComponent(
                    item.assessor, item.effective_weight, item.adjusted_distribution
                )
                for item in available_components
                if item.adjusted_distribution is not None
            )
            if self.pooled_distribution.components != expected_components:
                raise ContractError("sealed mixture differs from component receipt authority")
        elif (
            self.mode is PoolMode.MANUAL_SINGLE
            and self.pooled_distribution != available_components[0].adjusted_distribution
        ):
            raise ContractError("manual single output must equal the exact adjusted survivor")
        expected_summary = (
            self.pooled_distribution.quantile_summary()
            if isinstance(self.pooled_distribution, LinearPooledDistribution)
            else self.pooled_distribution
        )
        if self.pooled_summary != expected_summary:
            raise ContractError("pooled summary differs from sealed distribution authority")
        expected_map = canonical_digest(
            {
                "schema_version": "strathmark-v3-linear-pool-crn-map-v1",
                "seed": self.seed,
                "draw_count": self.draw_count,
                "provided_common_map_digest": self.source_common_random_map_digest,
                "uniforms_digest": canonical_digest(self.common_uniforms),
                "component_order": [item.assessor.value for item in available_components],
            }
        )
        if self.common_random_map_digest != expected_map:
            raise ContractError("pool common-random map differs from sealed inputs")
        replay_spec = SamplingSpec(
            self.seed,
            self.draw_count,
            self.common_uniforms,
            self.common_random_map_digest,
        )
        for item in available_components:
            assert item.adjusted_distribution is not None
            if item.adjusted_distribution.sample(replay_spec).samples_digest != item.samples_digest:
                raise ContractError("component samples digest differs from standalone replay")
        if self.pooled_distribution is not None:
            replay = self.pooled_distribution.sample(replay_spec)
            if (
                replay.samples_ms != self.pooled_samples_ms
                or replay.samples_digest != self.pooled_samples_digest
            ):
                raise ContractError("pooled samples differ from standalone replay")
        _require_digest(self.receipt_digest, "receipt_digest")
        if self.receipt_digest != canonical_digest(self.content_value()):
            raise ContractError("pool receipt digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-pool-receipt-v1",
            "mode": self.mode.value,
            "available_count": self.available_count,
            "is_ensemble": self.is_ensemble,
            "baseline_weights": [[kind.value, value] for kind, value in self.baseline_weights],
            "weight_authority": self.weight_authority.to_dict(),
            "effective_weights": [[kind.value, value] for kind, value in self.effective_weights],
            "normalization_denominator": self.normalization_denominator,
            "missing_mass": self.missing_mass,
            "capability_operator_version": self.capability_operator_version,
            "capability_state_digest": self.capability_state_digest,
            "components": [row.to_dict() for row in self.components],
            "pooled_distribution": (
                self.pooled_distribution.to_dict() if self.pooled_distribution else None
            ),
            "pooled_summary": self.pooled_summary.to_dict() if self.pooled_summary else None,
            "pooled_samples_authority_digest": (
                canonical_digest(self.pooled_samples_ms)
                if self.pooled_samples_ms is not None
                else None
            ),
            "pooled_samples_digest": self.pooled_samples_digest,
            "seed": self.seed,
            "draw_count": self.draw_count,
            "algorithm": self.algorithm,
            "dependency_version": self.dependency_version,
            "time_quantum_ms": self.time_quantum_ms,
            "common_random_map_digest": self.common_random_map_digest,
            "common_uniforms_digest": canonical_digest(self.common_uniforms),
            "source_common_random_map_digest": self.source_common_random_map_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "pooled_samples_ms": (
                list(self.pooled_samples_ms) if self.pooled_samples_ms is not None else None
            ),
            "common_uniforms": list(self.common_uniforms),
            "receipt_digest": self.receipt_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PoolReceipt:
        expected = {
            "schema_version",
            "mode",
            "available_count",
            "is_ensemble",
            "baseline_weights",
            "weight_authority",
            "effective_weights",
            "normalization_denominator",
            "missing_mass",
            "capability_operator_version",
            "capability_state_digest",
            "components",
            "pooled_distribution",
            "pooled_summary",
            "pooled_samples_authority_digest",
            "pooled_samples_ms",
            "pooled_samples_digest",
            "seed",
            "draw_count",
            "algorithm",
            "dependency_version",
            "time_quantum_ms",
            "common_random_map_digest",
            "common_uniforms_digest",
            "common_uniforms",
            "source_common_random_map_digest",
            "receipt_digest",
        }
        if set(value) != expected or value["schema_version"] != "strathmark-v3-pool-receipt-v1":
            raise ContractError("pool receipt fields or schema differ")
        try:
            mode = PoolMode(value["mode"])
        except (TypeError, ValueError) as exc:
            raise ContractError("pool receipt mode is unknown") from exc
        components = value["components"]
        distribution = value["pooled_distribution"]
        summary = value["pooled_summary"]
        if not isinstance(components, list):
            raise ContractError("pool receipt components are invalid")
        serialized_samples = value["pooled_samples_ms"]
        serialized_uniforms = value["common_uniforms"]
        if serialized_samples is not None and not isinstance(serialized_samples, list):
            raise ContractError("pool receipt samples are invalid")
        if not isinstance(serialized_uniforms, list):
            raise ContractError("pool receipt common uniforms are invalid")
        if value["pooled_samples_authority_digest"] != (
            canonical_digest(tuple(serialized_samples)) if serialized_samples is not None else None
        ) or value["common_uniforms_digest"] != canonical_digest(tuple(serialized_uniforms)):
            raise ContractError("pool receipt serialized sample authority digest mismatch")
        decoded_distribution = _decode_distribution(distribution)
        if summary is not None and not isinstance(summary, Mapping):
            raise ContractError("serialized pooled summary is invalid")
        decoded_summary = (
            PositiveTimeDistribution.from_dict(summary) if isinstance(summary, Mapping) else None
        )
        authority = value["weight_authority"]
        if not isinstance(authority, Mapping):
            raise ContractError("serialized weight authority is invalid")
        return cls(
            mode=mode,
            available_count=value["available_count"],
            is_ensemble=value["is_ensemble"],
            baseline_weights=_decode_weights(value["baseline_weights"]),
            weight_authority=WeightAuthorityBinding.from_dict(authority),
            effective_weights=_decode_weights(value["effective_weights"]),
            normalization_denominator=value["normalization_denominator"],
            missing_mass=value["missing_mass"],
            capability_operator_version=value["capability_operator_version"],
            capability_state_digest=value["capability_state_digest"],
            components=tuple(PoolComponentReceipt.from_dict(item) for item in components),
            pooled_distribution=decoded_distribution,
            pooled_summary=decoded_summary,
            pooled_samples_ms=(
                tuple(serialized_samples) if serialized_samples is not None else None
            ),
            pooled_samples_digest=value["pooled_samples_digest"],
            seed=value["seed"],
            draw_count=value["draw_count"],
            algorithm=value["algorithm"],
            dependency_version=value["dependency_version"],
            time_quantum_ms=value["time_quantum_ms"],
            common_random_map_digest=value["common_random_map_digest"],
            common_uniforms=tuple(serialized_uniforms),
            source_common_random_map_digest=value["source_common_random_map_digest"],
            receipt_digest=value["receipt_digest"],
        )


@dataclass(frozen=True, slots=True)
class PoolResult:
    mode: PoolMode
    distribution: LinearPooledDistribution | PositiveTimeDistribution | None
    samples: DistributionSamples | None
    receipt: PoolReceipt


def pool_forecasts(
    forecasts: tuple[AssessorForecast, ...],
    baseline: WeightReceipt,
    capability_state: CapabilityState,
    sampling: SamplingSpec,
    *,
    weight_authority: WeightAuthorityBinding,
    accept_single_survivor: bool = False,
) -> PoolResult:
    """Apply one capability operator and form an auditable linear opinion pool."""

    if not isinstance(forecasts, tuple) or not all(
        isinstance(item, AssessorForecast) for item in forecasts
    ):
        raise ContractError("forecasts must be immutable AssessorForecast values")
    if not isinstance(baseline, WeightReceipt) or not isinstance(capability_state, CapabilityState):
        raise ContractError("pooling requires typed weight and capability authority")
    if not isinstance(sampling, SamplingSpec):
        raise ContractError("pooling requires a frozen SamplingSpec")
    if (
        not isinstance(weight_authority, WeightAuthorityBinding)
        or weight_authority.weights != baseline.weights
        or weight_authority.weight_receipt_digest != baseline.receipt_digest
        or weight_authority.context != baseline.context
        or weight_authority.calibration_cutoff_at_utc != baseline.calibration_cutoff_at_utc
        or weight_authority.policy_digest != baseline.policy_digest
    ):
        raise ContractError("weight authority does not bind the supplied baseline receipt")
    assessors = tuple(item.assessor for item in forecasts)
    if len(assessors) != len(set(assessors)):
        raise ContractError("assessor forecasts must be unique")
    if any(item not in _OUTER for item in assessors):
        raise ContractError("pooling accepts outer assessor forecasts only")
    baseline_values = _weight_values(baseline.weights, require_all=True)
    by_assessor = {item.assessor: item for item in forecasts}
    availability = {assessor: _availability(by_assessor.get(assessor)) for assessor in _OUTER}
    available = tuple(
        assessor for assessor in _OUTER if availability[assessor] is AvailabilityState.VALID
    )
    with localcontext() as context:
        context.prec = 96
        denominator = sum((baseline_values[item] for item in available), Decimal(0))
        missing_mass = Decimal(1) - denominator
        effective = (
            tuple(
                (item, _decimal_string(baseline_values[item] / denominator)) for item in available
            )
            if denominator
            else ()
        )
    effective_values = {item: Decimal(value) for item, value in effective}
    mode = _mode(len(available), accept_single_survivor)
    source_uniforms = sampling.common_uniforms or _splitmix_uniforms(
        sampling.seed, sampling.draw_count
    )
    common_map_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-linear-pool-crn-map-v1",
            "seed": sampling.seed,
            "draw_count": sampling.draw_count,
            "provided_common_map_digest": sampling.common_random_map_digest,
            "uniforms_digest": canonical_digest(source_uniforms),
            "component_order": [item.value for item in available],
        }
    )
    pool_sampling = SamplingSpec(
        seed=sampling.seed,
        draw_count=sampling.draw_count,
        common_uniforms=source_uniforms,
        common_random_map_digest=common_map_digest,
    )
    adjusted: dict[AssessorKind, PositiveTimeDistribution] = {}
    adjustment_digests: dict[AssessorKind, str] = {}
    sampled: dict[AssessorKind, DistributionSamples] = {}
    for assessor in available:
        forecast = by_assessor[assessor]
        assert forecast.distribution is not None
        adjustment = apply_capability_operator(assessor, forecast.distribution, capability_state)
        adjusted[assessor] = adjustment.adjusted_distribution
        adjustment_digests[assessor] = adjustment.adjustment_digest
        sampled[assessor] = adjustment.adjusted_distribution.sample(pool_sampling)

    distribution: LinearPooledDistribution | PositiveTimeDistribution | None = None
    samples: DistributionSamples | None = None
    if mode in {PoolMode.NORMAL, PoolMode.DEGRADED_TWO}:
        distribution = LinearPooledDistribution(
            tuple(
                LinearPoolComponent(
                    assessor,
                    _decimal_string(effective_values[assessor]),
                    adjusted[assessor],
                )
                for assessor in available
            )
        )
        samples = distribution.sample(pool_sampling)
    elif mode is PoolMode.MANUAL_SINGLE:
        survivor = available[0]
        distribution = adjusted[survivor]
        samples = distribution.sample(pool_sampling)

    components = tuple(
        _component_receipt(
            assessor,
            by_assessor.get(assessor),
            availability[assessor],
            baseline_values[assessor],
            effective_values.get(assessor, Decimal(0)),
            adjusted.get(assessor),
            adjustment_digests.get(assessor),
            sampled.get(assessor),
        )
        for assessor in _OUTER
    )
    receipt_values = {
        "mode": mode,
        "available_count": len(available),
        "is_ensemble": mode in {PoolMode.NORMAL, PoolMode.DEGRADED_TWO},
        "baseline_weights": baseline.weights,
        "weight_authority": weight_authority,
        "effective_weights": effective,
        "normalization_denominator": _decimal_string(denominator),
        "missing_mass": _decimal_string(missing_mass),
        "capability_operator_version": CAPABILITY_OPERATOR_VERSION,
        "capability_state_digest": capability_state.state_digest,
        "components": components,
        "pooled_distribution": distribution,
        "pooled_summary": (
            distribution.quantile_summary()
            if isinstance(distribution, LinearPooledDistribution)
            else distribution
        ),
        "pooled_samples_ms": samples.samples_ms if samples else None,
        "pooled_samples_digest": samples.samples_digest if samples else None,
        "seed": sampling.seed,
        "draw_count": sampling.draw_count,
        "algorithm": _algorithm(mode),
        "dependency_version": "stdlib-only-v1",
        "time_quantum_ms": 1,
        "common_random_map_digest": common_map_digest,
        "common_uniforms": source_uniforms,
        "source_common_random_map_digest": sampling.common_random_map_digest,
    }
    receipt = PoolReceipt(
        **receipt_values,
        receipt_digest=canonical_digest(_pool_receipt_content(receipt_values)),
    )
    return PoolResult(mode, distribution, samples, receipt)


def _weight_values(
    values: tuple[tuple[AssessorKind, str], ...], *, require_all: bool
) -> dict[AssessorKind, Decimal]:
    if not isinstance(values, tuple):
        raise ContractError("pool weights must be immutable")
    assessors = tuple(item for item, _ in values)
    expected = _OUTER if require_all else tuple(item for item in _OUTER if item in assessors)
    if assessors != expected:
        raise ContractError("pool weights must be unique and canonically ordered")
    result = {item: _canonical_decimal(value, "pool weight") for item, value in values}
    if any(value < 0 for value in result.values()):
        raise ContractError("pool weights must be nonnegative")
    if require_all:
        with localcontext() as context:
            context.prec = 96
            if sum(result.values(), Decimal(0)) != 1:
                raise ContractError("baseline weights must sum exactly to one")
    return result


def _decode_weights(value: object) -> tuple[tuple[AssessorKind, str], ...]:
    if not isinstance(value, list):
        raise ContractError("serialized pool weights must be an array")
    result = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise ContractError("serialized pool weight row is invalid")
        try:
            assessor = AssessorKind(item[0])
        except (TypeError, ValueError) as exc:
            raise ContractError("serialized pool assessor is unknown") from exc
        result.append((assessor, item[1]))
    return tuple(result)


def _decode_distribution(
    value: object,
) -> LinearPooledDistribution | PositiveTimeDistribution | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContractError("serialized pool distribution is invalid")
    if value.get("schema_version") == "strathmark-v3-linear-pooled-distribution-v1":
        return LinearPooledDistribution.from_dict(value)
    return PositiveTimeDistribution.from_dict(value)


def _pool_receipt_content(values: Mapping[str, Any]) -> dict[str, Any]:
    distribution = values["pooled_distribution"]
    return {
        "schema_version": "strathmark-v3-pool-receipt-v1",
        "mode": values["mode"].value,
        "available_count": values["available_count"],
        "is_ensemble": values["is_ensemble"],
        "baseline_weights": [[kind.value, value] for kind, value in values["baseline_weights"]],
        "weight_authority": values["weight_authority"].to_dict(),
        "effective_weights": [[kind.value, value] for kind, value in values["effective_weights"]],
        "normalization_denominator": values["normalization_denominator"],
        "missing_mass": values["missing_mass"],
        "capability_operator_version": values["capability_operator_version"],
        "capability_state_digest": values["capability_state_digest"],
        "components": [row.to_dict() for row in values["components"]],
        "pooled_distribution": distribution.to_dict() if distribution else None,
        "pooled_summary": (
            values["pooled_summary"].to_dict() if values["pooled_summary"] else None
        ),
        "pooled_samples_authority_digest": (
            canonical_digest(values["pooled_samples_ms"])
            if values["pooled_samples_ms"] is not None
            else None
        ),
        "pooled_samples_digest": values["pooled_samples_digest"],
        "seed": values["seed"],
        "draw_count": values["draw_count"],
        "algorithm": values["algorithm"],
        "dependency_version": values["dependency_version"],
        "time_quantum_ms": values["time_quantum_ms"],
        "common_random_map_digest": values["common_random_map_digest"],
        "common_uniforms_digest": canonical_digest(values["common_uniforms"]),
        "source_common_random_map_digest": values["source_common_random_map_digest"],
    }


def _availability(forecast: AssessorForecast | None) -> AvailabilityState:
    if forecast is None:
        return AvailabilityState.MISSING
    if forecast.state is ForecastState.COMMITTED:
        return AvailabilityState.VALID
    if forecast.state is ForecastState.ABSTAINED:
        return AvailabilityState.ABSTAINED
    return AvailabilityState.INVALID


def _mode(count: int, accept_single: bool) -> PoolMode:
    if count == 3:
        return PoolMode.NORMAL
    if count == 2:
        return PoolMode.DEGRADED_TWO
    if count == 1 and accept_single:
        return PoolMode.MANUAL_SINGLE
    return PoolMode.MANUAL_REQUIRED


def _algorithm(mode: PoolMode) -> str:
    if mode in {PoolMode.NORMAL, PoolMode.DEGRADED_TWO}:
        return "weighted-linear-opinion-pool-v1"
    if mode is PoolMode.MANUAL_SINGLE:
        return "exact-survivor-manual-degraded-v1"
    return "no-pool-manual-construction-v1"


def _component_receipt(
    assessor: AssessorKind,
    forecast: AssessorForecast | None,
    availability: AvailabilityState,
    baseline_weight: Decimal,
    effective_weight: Decimal,
    adjusted: PositiveTimeDistribution | None,
    adjustment_digest: str | None,
    samples: DistributionSamples | None,
) -> PoolComponentReceipt:
    return PoolComponentReceipt(
        assessor=assessor,
        availability=availability,
        availability_reason=(
            "available"
            if availability is AvailabilityState.VALID
            else (forecast.abstention_code if forecast is not None else "missing_forecast")
        ),
        baseline_weight=_decimal_string(baseline_weight),
        effective_weight=_decimal_string(effective_weight),
        forecast_id=str(forecast.forecast_id) if forecast else None,
        forecast_commit_digest=forecast.commit_digest if forecast else None,
        original_distribution=forecast.distribution if forecast else None,
        adjusted_distribution=adjusted,
        capability_adjustment_digest=adjustment_digest,
        samples_digest=samples.samples_digest if samples else None,
    )


def _decimal_string(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 96
        return canonical_decimal_string(+value)


def _canonical_decimal(value: str, label: str) -> Decimal:
    if not isinstance(value, str) or canonical_decimal_string(value) != value:
        raise ContractError(f"{label} must be a canonical decimal string")
    return Decimal(value)


def _require_digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise ContractError(f"{label} must be a lowercase SHA-256 digest")


def _distribution_cdf(distribution: PositiveTimeDistribution, time_ms: int) -> Decimal:
    points = [(Decimal(item.probability), item.time_ms) for item in distribution.quantiles]
    if time_ms < points[0][1]:
        return Decimal(0)
    if time_ms >= points[-1][1]:
        return Decimal(1)
    equal = [probability for probability, value in points if value == time_ms]
    if equal:
        return max(equal)
    left_probability, left_time, right_probability, right_time = next(
        (left_probability, left_time, right_probability, right_time)
        for (left_probability, left_time), (right_probability, right_time) in zip(
            points, points[1:]
        )
        if left_time <= time_ms <= right_time
    )
    return left_probability + (right_probability - left_probability) * Decimal(
        time_ms - left_time
    ) / Decimal(right_time - left_time)


__all__ = [
    "AvailabilityState",
    "LinearPoolComponent",
    "LinearPooledDistribution",
    "PoolComponentReceipt",
    "PoolMode",
    "PoolReceipt",
    "PoolResult",
    "WeightAuthorityBinding",
    "WeightAuthorityStatus",
    "pool_forecasts",
]
