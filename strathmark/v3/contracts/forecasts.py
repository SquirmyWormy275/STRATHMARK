"""Provider-independent positive-time forecast and sampling contracts."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum
from typing import Any, Mapping, Protocol

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import (
    _require_digest,
    _require_id,
    _require_version,
)
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.contracts.statuses import (
    _require_fields,
    _require_nonnegative_int,
    _require_positive_int,
    _require_schema,
)

DISTRIBUTION_SCHEMA_VERSION = "strathmark-v3-positive-quantile-distribution-v1"
SAMPLING_SCHEMA_VERSION = "strathmark-v3-distribution-sampling-v1"
DEPENDENCE_SCHEMA_VERSION = "strathmark-v3-field-dependence-v1"
FORECAST_SCHEMA_VERSION = "strathmark-v3-assessor-forecast-v1"
LLM_AUDIT_SCHEMA_VERSION = "strathmark-v3-llm-member-audit-v1"
SAMPLING_ALGORITHM = "splitmix64-inverse-quantile-v1"
MAX_DRAWS = 1_000_000
_UINT64_MAX = 2**64 - 1


class DependenceMode(str, Enum):
    INDEPENDENCE = "independence"
    GAUSSIAN_COPULA = "gaussian_copula"
    SHARED_RANK_COPULA = "shared_rank_copula"


class AssessorKind(str, Enum):
    FORMULA = "formula"
    ML = "ml"
    LLM_COUNCIL = "llm_council"
    LLM_MEMBER = "llm_member"


class ForecastState(str, Enum):
    COMMITTED = "committed"
    ABSTAINED = "abstained"
    INVALID = "invalid"


class ForecastWarning(str, Enum):
    SPARSE_EVIDENCE = "sparse_evidence"
    INSUFFICIENT_SUPPORT = "insufficient_support"
    PRIOR_ONLY = "prior_only"
    MISSING_CONTEXT = "missing_context"
    DEGRADED_MEMBER_POOL = "degraded_member_pool"
    INDEPENDENCE_FALLBACK = "independence_fallback"
    CAPABILITY_PROTECTION = "capability_protection"
    PROVIDER_FINGERPRINT_LIMITATION = "provider_fingerprint_limitation"


class PredictiveDistributionContract(Protocol):
    """Narrow dependency-free interface consumed by pooling and optimization."""

    @property
    def digest(self) -> str: ...

    @property
    def median_ms(self) -> int: ...

    def sample(self, spec: SamplingSpec) -> DistributionSamples: ...


@dataclass(frozen=True, slots=True, order=True)
class QuantilePoint:
    probability: str
    time_ms: int

    def __post_init__(self) -> None:
        probability = _require_probability(self.probability)
        if not (Decimal("0") < probability < Decimal("1")):
            raise ContractError(
                "quantile probability must be strictly between zero and one"
            )
        _require_positive_int(self.time_ms, "quantile time_ms")

    def to_dict(self) -> dict[str, Any]:
        return {"probability": self.probability, "time_ms": self.time_ms}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> QuantilePoint:
        _require_fields(value, {"probability", "time_ms"})
        return cls(value["probability"], value["time_ms"])


@dataclass(frozen=True, slots=True)
class SamplingSpec:
    seed: int
    draw_count: int
    common_uniforms: tuple[str, ...] = ()
    common_random_map_digest: str | None = None

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.seed, "seed")
        if self.seed > _UINT64_MAX:
            raise ContractError("seed must fit an unsigned 64-bit integer")
        _require_positive_int(self.draw_count, "draw_count")
        if self.draw_count > MAX_DRAWS:
            raise ContractError("draw_count exceeds the contract maximum")
        if not isinstance(self.common_uniforms, tuple):
            raise ContractError("common_uniforms must be an immutable tuple")
        if self.common_uniforms and len(self.common_uniforms) != self.draw_count:
            raise ContractError("common_uniforms length must equal draw_count")
        for value in self.common_uniforms:
            probability = _require_probability(value)
            if not (Decimal("0") < probability < Decimal("1")):
                raise ContractError(
                    "common uniforms must be strictly between zero and one"
                )
        if self.common_random_map_digest is not None:
            _require_digest(self.common_random_map_digest, "common_random_map_digest")
        if self.common_uniforms and self.common_random_map_digest is None:
            raise ContractError(
                "injected common uniforms require a common_random_map_digest"
            )


@dataclass(frozen=True, slots=True)
class DistributionSamples:
    samples_ms: tuple[int, ...]
    algorithm: str
    dependency_version: str
    seed: int
    draw_count: int
    time_quantum_ms: int
    distribution_digest: str
    samples_digest: str
    common_random_map_digest: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.samples_ms, tuple) or not self.samples_ms:
            raise ContractError("samples_ms must be a nonempty immutable tuple")
        for sample in self.samples_ms:
            _require_positive_int(sample, "sample time_ms")
        if self.algorithm != SAMPLING_ALGORITHM:
            raise ContractError("unknown distribution sampling algorithm")
        if self.dependency_version != "stdlib-only-v1":
            raise ContractError("unknown sampling dependency version")
        SamplingSpec(self.seed, self.draw_count)
        if self.draw_count != len(self.samples_ms):
            raise ContractError("draw_count must match samples_ms")
        if self.time_quantum_ms != 1:
            raise ContractError("unknown sampling time quantum")
        _require_digest(self.distribution_digest, "distribution_digest")
        _require_digest(self.samples_digest, "samples_digest")
        if self.common_random_map_digest is not None:
            _require_digest(self.common_random_map_digest, "common_random_map_digest")
        expected_digest = _samples_digest(
            samples_ms=self.samples_ms,
            seed=self.seed,
            distribution_digest=self.distribution_digest,
            common_random_map_digest=self.common_random_map_digest,
        )
        if self.samples_digest != expected_digest:
            raise ContractError("distribution samples digest mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SAMPLING_SCHEMA_VERSION,
            "samples_ms": list(self.samples_ms),
            "algorithm": self.algorithm,
            "dependency_version": self.dependency_version,
            "seed": self.seed,
            "draw_count": self.draw_count,
            "time_quantum_ms": self.time_quantum_ms,
            "distribution_digest": self.distribution_digest,
            "samples_digest": self.samples_digest,
            "common_random_map_digest": self.common_random_map_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DistributionSamples:
        expected = {
            "schema_version",
            "samples_ms",
            "algorithm",
            "dependency_version",
            "seed",
            "draw_count",
            "time_quantum_ms",
            "distribution_digest",
            "samples_digest",
            "common_random_map_digest",
        }
        _require_fields(value, expected)
        _require_schema(value["schema_version"], SAMPLING_SCHEMA_VERSION)
        if not isinstance(value["samples_ms"], list):
            raise ContractError("samples_ms must be a JSON array")
        return cls(
            samples_ms=tuple(value["samples_ms"]),
            algorithm=value["algorithm"],
            dependency_version=value["dependency_version"],
            seed=value["seed"],
            draw_count=value["draw_count"],
            time_quantum_ms=value["time_quantum_ms"],
            distribution_digest=value["distribution_digest"],
            samples_digest=value["samples_digest"],
            common_random_map_digest=value["common_random_map_digest"],
        )


@dataclass(frozen=True, slots=True)
class PositiveTimeDistribution:
    """A finite positive quantile function shared by every V3 assessor.

    Sampling uses a locally implemented SplitMix64 stream plus integer-millisecond
    inverse-quantile interpolation.  It therefore has no NumPy or ambient RNG state.
    """

    quantiles: tuple[QuantilePoint, ...]
    schema_version: str = DISTRIBUTION_SCHEMA_VERSION
    _probabilities: tuple[Decimal, ...] = field(init=False, repr=False, compare=False)
    _times_ms: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _digest_cache: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, DISTRIBUTION_SCHEMA_VERSION)
        if not isinstance(self.quantiles, tuple) or len(self.quantiles) < 3:
            raise ContractError(
                "distribution requires at least three immutable quantiles"
            )
        if not all(isinstance(item, QuantilePoint) for item in self.quantiles):
            raise ContractError("distribution quantiles must be QuantilePoint values")
        probabilities = tuple(Decimal(item.probability) for item in self.quantiles)
        times = tuple(item.time_ms for item in self.quantiles)
        if probabilities != tuple(sorted(probabilities)) or len(
            set(probabilities)
        ) != len(probabilities):
            raise ContractError(
                "quantile probabilities must be unique and strictly ordered"
            )
        if Decimal("0.5") not in probabilities:
            raise ContractError("distribution must include the median quantile 0.5")
        if times != tuple(sorted(times)):
            raise ContractError("quantile times must be nondecreasing")
        object.__setattr__(self, "_probabilities", probabilities)
        object.__setattr__(self, "_times_ms", times)
        object.__setattr__(self, "_digest_cache", canonical_digest(self.to_dict()))

    @property
    def digest(self) -> str:
        return self._digest_cache

    @property
    def median_ms(self) -> int:
        return self._at_probability(Decimal("0.5"))

    def central_interval(self, lower: str, upper: str) -> tuple[int, int]:
        low = _require_probability(lower)
        high = _require_probability(upper)
        if not Decimal("0") < low < Decimal("0.5") < high < Decimal("1"):
            raise ContractError("central interval must straddle the median")
        return self._at_probability(low), self._at_probability(high)

    def sample(self, spec: SamplingSpec) -> DistributionSamples:
        if not isinstance(spec, SamplingSpec):
            raise ContractError("sampling requires a SamplingSpec")
        uniforms = spec.common_uniforms or _splitmix_uniforms(
            spec.seed, spec.draw_count
        )
        samples = self._sample_probabilities(tuple(Decimal(item) for item in uniforms))
        distribution_digest = self.digest
        return DistributionSamples(
            samples_ms=samples,
            algorithm=SAMPLING_ALGORITHM,
            dependency_version="stdlib-only-v1",
            seed=spec.seed,
            draw_count=spec.draw_count,
            time_quantum_ms=1,
            distribution_digest=distribution_digest,
            samples_digest=_samples_digest(
                samples_ms=samples,
                seed=spec.seed,
                distribution_digest=distribution_digest,
                common_random_map_digest=spec.common_random_map_digest,
            ),
            common_random_map_digest=spec.common_random_map_digest,
        )

    def _at_probability(self, probability: Decimal) -> int:
        with localcontext() as context:
            context.prec = 256
            context.rounding = ROUND_HALF_EVEN
            return self._at_probability_compiled(probability)

    def _sample_probabilities(
        self, probabilities: tuple[Decimal, ...]
    ) -> tuple[int, ...]:
        """Evaluate a batch under one frozen Decimal context."""

        with localcontext() as context:
            context.prec = 256
            context.rounding = ROUND_HALF_EVEN
            return tuple(
                self._at_probability_compiled(probability)
                for probability in probabilities
            )

    def _at_probability_compiled(self, probability: Decimal) -> int:
        index = bisect_left(self._probabilities, probability)
        if index == 0:
            return self._times_ms[0]
        if index == len(self._probabilities):
            return self._times_ms[-1]
        left_p = self._probabilities[index - 1]
        right_p = self._probabilities[index]
        left_t = self._times_ms[index - 1]
        right_t = self._times_ms[index]
        ratio = (probability - left_p) / (right_p - left_p)
        interpolated = Decimal(left_t) + ratio * Decimal(right_t - left_t)
        return int(interpolated.quantize(Decimal("1")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "quantiles": [item.to_dict() for item in self.quantiles],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PositiveTimeDistribution:
        _require_fields(value, {"schema_version", "quantiles"})
        _require_schema(value["schema_version"], DISTRIBUTION_SCHEMA_VERSION)
        quantiles = value["quantiles"]
        if not isinstance(quantiles, list):
            raise ContractError("quantiles must be a JSON array")
        return cls(tuple(QuantilePoint.from_dict(item) for item in quantiles))


@dataclass(frozen=True, slots=True)
class DependenceInputs:
    field_id: StableIdentifier
    mode: DependenceMode
    version: str
    seed: int
    draw_count: int
    parameters_digest: str | None
    effective_sample_size: str
    fallback_code: str | None
    schema_version: str = DEPENDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, DEPENDENCE_SCHEMA_VERSION)
        _require_id(self.field_id, "field")
        if not isinstance(self.mode, DependenceMode):
            raise ContractError("dependence mode must be a DependenceMode value")
        _require_version(self.version, "dependence version")
        SamplingSpec(self.seed, self.draw_count)
        _require_nonnegative_decimal(
            self.effective_sample_size, "effective_sample_size"
        )
        if self.parameters_digest is not None:
            _require_digest(self.parameters_digest, "parameters_digest")
        if self.mode in {
            DependenceMode.GAUSSIAN_COPULA,
            DependenceMode.SHARED_RANK_COPULA,
        }:
            if self.parameters_digest is None:
                raise ContractError("learned dependence requires parameters_digest")
            if self.fallback_code is not None:
                raise ContractError("learned dependence cannot carry a fallback code")
        else:
            if self.parameters_digest is not None:
                raise ContractError("independence cannot carry parameters_digest")
            if not isinstance(self.fallback_code, str) or not self.fallback_code:
                raise ContractError("independence requires an explicit fallback code")

    @classmethod
    def independence(
        cls, *, field_id: StableIdentifier, seed: int, draw_count: int
    ) -> DependenceInputs:
        return cls(
            field_id=field_id,
            mode=DependenceMode.INDEPENDENCE,
            version="dependence:v1",
            seed=seed,
            draw_count=draw_count,
            parameters_digest=None,
            effective_sample_size="0",
            fallback_code="unsupported_context_independence",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "field_id": str(self.field_id),
            "mode": self.mode.value,
            "version": self.version,
            "seed": self.seed,
            "draw_count": self.draw_count,
            "parameters_digest": self.parameters_digest,
            "effective_sample_size": self.effective_sample_size,
            "fallback_code": self.fallback_code,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DependenceInputs:
        fields = {
            "schema_version",
            "field_id",
            "mode",
            "version",
            "seed",
            "draw_count",
            "parameters_digest",
            "effective_sample_size",
            "fallback_code",
        }
        _require_fields(value, fields)
        _require_schema(value["schema_version"], DEPENDENCE_SCHEMA_VERSION)
        try:
            mode = DependenceMode(value["mode"])
        except (TypeError, ValueError) as exc:
            raise ContractError("unknown dependence mode") from exc
        return cls(
            field_id=require_identifier(value["field_id"], expected_namespace="field"),
            mode=mode,
            version=value["version"],
            seed=value["seed"],
            draw_count=value["draw_count"],
            parameters_digest=value["parameters_digest"],
            effective_sample_size=value["effective_sample_size"],
            fallback_code=value["fallback_code"],
        )


@dataclass(frozen=True, slots=True, order=True)
class EvidenceSupport:
    eligible_count: int
    effective_weight: str
    exact_context_count: int
    max_historical_key: str | None
    tournament_event_sequence: int

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.eligible_count, "eligible_count")
        _require_nonnegative_decimal(self.effective_weight, "effective_weight")
        _require_nonnegative_int(self.exact_context_count, "exact_context_count")
        if self.exact_context_count > self.eligible_count:
            raise ContractError("exact_context_count cannot exceed eligible_count")
        if self.max_historical_key is not None:
            require_identifier(self.max_historical_key, expected_namespace="history")
        _require_nonnegative_int(
            self.tournament_event_sequence, "tournament_event_sequence"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible_count": self.eligible_count,
            "effective_weight": self.effective_weight,
            "exact_context_count": self.exact_context_count,
            "max_historical_key": self.max_historical_key,
            "tournament_event_sequence": self.tournament_event_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceSupport:
        _require_fields(
            value,
            {
                "eligible_count",
                "effective_weight",
                "exact_context_count",
                "max_historical_key",
                "tournament_event_sequence",
            },
        )
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, order=True)
class ArtifactIdentity:
    role: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ContractError("artifact role must be a nonempty string")
        _require_version(self.version, "artifact version")
        _require_digest(self.digest, "artifact digest")

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "version": self.version, "digest": self.digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactIdentity:
        _require_fields(value, {"role", "version", "digest"})
        return cls(value["role"], value["version"], value["digest"])


@dataclass(frozen=True, slots=True)
class AssessorForecast:
    forecast_id: StableIdentifier
    assessor: AssessorKind
    state: ForecastState
    evidence_digest: str
    distribution: PositiveTimeDistribution | None
    support: EvidenceSupport
    warnings: tuple[ForecastWarning, ...]
    artifacts: tuple[ArtifactIdentity, ...]
    abstention_code: str | None
    commit_digest: str
    schema_version: str = FORECAST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, FORECAST_SCHEMA_VERSION)
        _require_id(self.forecast_id, "forecast")
        if not isinstance(self.assessor, AssessorKind):
            raise ContractError("assessor must be an AssessorKind value")
        if not isinstance(self.state, ForecastState):
            raise ContractError("state must be a ForecastState value")
        _require_digest(self.evidence_digest, "evidence_digest")
        if not isinstance(self.support, EvidenceSupport):
            raise ContractError("support must be EvidenceSupport")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(item, ForecastWarning) for item in self.warnings
        ):
            raise ContractError("warnings must be immutable ForecastWarning values")
        if tuple(item.value for item in self.warnings) != tuple(
            sorted(item.value for item in self.warnings)
        ) or len(self.warnings) != len(set(self.warnings)):
            raise ContractError("warnings must be unique and sorted")
        if not isinstance(self.artifacts, tuple) or not all(
            isinstance(item, ArtifactIdentity) for item in self.artifacts
        ):
            raise ContractError("artifacts must be immutable ArtifactIdentity values")
        if self.state is ForecastState.COMMITTED:
            if not isinstance(self.distribution, PositiveTimeDistribution):
                raise ContractError(
                    "committed forecast requires a positive distribution"
                )
            if self.abstention_code is not None:
                raise ContractError(
                    "committed forecast cannot carry an abstention code"
                )
        else:
            if self.distribution is not None:
                raise ContractError(
                    "distribution must be absent for non-committed forecasts"
                )
            if not isinstance(self.abstention_code, str) or not self.abstention_code:
                raise ContractError(
                    "non-committed forecast requires an abstention code"
                )
        _require_digest(self.commit_digest, "commit_digest")
        if self.commit_digest != self.recompute_digest():
            raise ContractError("forecast commit digest mismatch")

    @classmethod
    def create(cls, **arguments: Any) -> AssessorForecast:
        content = _forecast_content_value(**arguments)
        return cls(commit_digest=canonical_digest(content), **arguments)

    def _content_value(self) -> dict[str, Any]:
        return _forecast_content_value(
            forecast_id=self.forecast_id,
            assessor=self.assessor,
            state=self.state,
            evidence_digest=self.evidence_digest,
            distribution=self.distribution,
            support=self.support,
            warnings=self.warnings,
            artifacts=self.artifacts,
            abstention_code=self.abstention_code,
        )

    def recompute_digest(self) -> str:
        return canonical_digest(self._content_value())

    def to_dict(self) -> dict[str, Any]:
        return {**self._content_value(), "commit_digest": self.commit_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AssessorForecast:
        expected = {
            "schema_version",
            "forecast_id",
            "assessor",
            "state",
            "evidence_digest",
            "distribution",
            "support",
            "warnings",
            "artifacts",
            "abstention_code",
            "commit_digest",
        }
        _require_fields(value, expected)
        _require_schema(value["schema_version"], FORECAST_SCHEMA_VERSION)
        try:
            assessor = AssessorKind(value["assessor"])
            state = ForecastState(value["state"])
            warnings = tuple(ForecastWarning(item) for item in value["warnings"])
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "unknown assessor, forecast state, or warning code"
            ) from exc
        distribution = value["distribution"]
        return cls(
            forecast_id=require_identifier(
                value["forecast_id"], expected_namespace="forecast"
            ),
            assessor=assessor,
            state=state,
            evidence_digest=value["evidence_digest"],
            distribution=(
                None
                if distribution is None
                else PositiveTimeDistribution.from_dict(distribution)
            ),
            support=EvidenceSupport.from_dict(value["support"]),
            warnings=warnings,
            artifacts=tuple(
                ArtifactIdentity.from_dict(item) for item in value["artifacts"]
            ),
            abstention_code=value["abstention_code"],
            commit_digest=value["commit_digest"],
        )


@dataclass(frozen=True, slots=True)
class LLMMemberAudit:
    prompt_digest: str
    schema_version: str
    runtime_version: str
    model_digest: str
    quantization: str
    sampling_parameters_digest: str
    raw_response_digest: str
    validator_code: str
    latency_ms: int
    provider_model_version: str
    provider_fingerprint: str | None
    api_revision: str | None
    canary_digest: str | None
    contract_schema_version: str = LLM_AUDIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.contract_schema_version, LLM_AUDIT_SCHEMA_VERSION)
        for label in (
            "prompt_digest",
            "model_digest",
            "sampling_parameters_digest",
            "raw_response_digest",
        ):
            _require_digest(getattr(self, label), label)
        for label in (
            "schema_version",
            "runtime_version",
            "quantization",
            "validator_code",
            "provider_model_version",
        ):
            if not isinstance(getattr(self, label), str) or not getattr(self, label):
                raise ContractError(f"{label} must be a nonempty string")
        _require_nonnegative_int(self.latency_ms, "latency_ms")
        for label in ("provider_fingerprint", "canary_digest"):
            value = getattr(self, label)
            if value is not None:
                _require_digest(value, label)
        if self.api_revision is not None and (
            not isinstance(self.api_revision, str) or not self.api_revision
        ):
            raise ContractError("api_revision must be absent or a nonempty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_schema_version": self.contract_schema_version,
            "prompt_digest": self.prompt_digest,
            "schema_version": self.schema_version,
            "runtime_version": self.runtime_version,
            "model_digest": self.model_digest,
            "quantization": self.quantization,
            "sampling_parameters_digest": self.sampling_parameters_digest,
            "raw_response_digest": self.raw_response_digest,
            "validator_code": self.validator_code,
            "latency_ms": self.latency_ms,
            "provider_model_version": self.provider_model_version,
            "provider_fingerprint": self.provider_fingerprint,
            "api_revision": self.api_revision,
            "canary_digest": self.canary_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LLMMemberAudit:
        expected = {
            "contract_schema_version",
            "prompt_digest",
            "schema_version",
            "runtime_version",
            "model_digest",
            "quantization",
            "sampling_parameters_digest",
            "raw_response_digest",
            "validator_code",
            "latency_ms",
            "provider_model_version",
            "provider_fingerprint",
            "api_revision",
            "canary_digest",
        }
        _require_fields(value, expected)
        _require_schema(value["contract_schema_version"], LLM_AUDIT_SCHEMA_VERSION)
        return cls(**value)  # type: ignore[arg-type]


def _forecast_content_value(**arguments: Any) -> dict[str, Any]:
    return {
        "schema_version": FORECAST_SCHEMA_VERSION,
        "forecast_id": str(arguments["forecast_id"]),
        "assessor": arguments["assessor"].value,
        "state": arguments["state"].value,
        "evidence_digest": arguments["evidence_digest"],
        "distribution": (
            None
            if arguments["distribution"] is None
            else arguments["distribution"].to_dict()
        ),
        "support": arguments["support"].to_dict(),
        "warnings": [item.value for item in arguments["warnings"]],
        "artifacts": [item.to_dict() for item in arguments["artifacts"]],
        "abstention_code": arguments["abstention_code"],
    }


def _require_probability(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ContractError("probability must be a canonical decimal string")
    try:
        if canonical_decimal_string(value) != value:
            raise ContractError("probability must be a canonical decimal string")
        return Decimal(value)
    except ContractError:
        raise
    except Exception as exc:  # pragma: no cover - canonical helper owns exact failure
        raise ContractError("probability must be a canonical decimal string") from exc


def _require_nonnegative_decimal(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a canonical decimal string")
    if canonical_decimal_string(value) != value:
        raise ContractError(f"{label} must be a canonical decimal string")
    decimal = Decimal(value)
    if decimal < 0:
        raise ContractError(f"{label} must be non-negative")
    return decimal


def _splitmix_uniforms(seed: int, count: int) -> tuple[str, ...]:
    state = seed
    values: list[str] = []
    for _ in range(count):
        state = (state + 0x9E3779B97F4A7C15) & _UINT64_MAX
        mixed = state
        mixed = ((mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9) & _UINT64_MAX
        mixed = ((mixed ^ (mixed >> 27)) * 0x94D049BB133111EB) & _UINT64_MAX
        mixed ^= mixed >> 31
        numerator = mixed + 1
        with localcontext() as context:
            context.prec = 96
            context.rounding = ROUND_HALF_EVEN
            probability = Decimal(numerator) / Decimal(2**64 + 1)
        values.append(canonical_decimal_string(probability))
    return tuple(values)


def _samples_digest(
    *,
    samples_ms: tuple[int, ...],
    seed: int,
    distribution_digest: str,
    common_random_map_digest: str | None,
) -> str:
    return canonical_digest(
        {
            "schema_version": SAMPLING_SCHEMA_VERSION,
            "algorithm": SAMPLING_ALGORITHM,
            "dependency_version": "stdlib-only-v1",
            "seed": seed,
            "draw_count": len(samples_ms),
            "time_quantum_ms": 1,
            "samples_ms": samples_ms,
            "distribution_digest": distribution_digest,
            "common_random_map_digest": common_random_map_digest,
        }
    )


__all__ = [
    "DEPENDENCE_SCHEMA_VERSION",
    "DISTRIBUTION_SCHEMA_VERSION",
    "FORECAST_SCHEMA_VERSION",
    "LLM_AUDIT_SCHEMA_VERSION",
    "SAMPLING_ALGORITHM",
    "SAMPLING_SCHEMA_VERSION",
    "ArtifactIdentity",
    "AssessorForecast",
    "AssessorKind",
    "DependenceInputs",
    "DependenceMode",
    "DistributionSamples",
    "EvidenceSupport",
    "ForecastState",
    "ForecastWarning",
    "LLMMemberAudit",
    "PositiveTimeDistribution",
    "PredictiveDistributionContract",
    "QuantilePoint",
    "SamplingSpec",
]
