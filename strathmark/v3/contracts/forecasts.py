"""Provider-independent positive-time forecast and sampling contracts."""

from __future__ import annotations

import hashlib
import struct
import sys
from bisect import bisect_left
from dataclasses import InitVar, dataclass, field
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
_STANDARD_QUANTILE_GRID = (Decimal("0.1"), Decimal("0.5"), Decimal("0.9"))
_NATIVE_STANDARD_SAMPLER: Any | None = None
_NATIVE_STANDARD_SAMPLER_INITIALIZED = False


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
            raise ContractError("quantile probability must be strictly between zero and one")
        _require_positive_int(self.time_ms, "quantile time_ms")

    def to_dict(self) -> dict[str, Any]:
        return {"probability": self.probability, "time_ms": self.time_ms}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> QuantilePoint:
        _require_fields(value, {"probability", "time_ms"})
        return cls(value["probability"], value["time_ms"])


@dataclass(frozen=True, slots=True)
class _DerivedSamplingSpecProof:
    token: object
    seed: int
    draw_count: int
    common_uniforms: tuple[str, ...]
    common_random_map_digest: str
    validated_common_uniforms: tuple[Decimal, ...]
    common_uniform_exponent: int | None
    scaled_common_uniforms: tuple[int, ...]
    standard_probability_words_le: bytes | None


@dataclass(frozen=True, slots=True)
class SamplingSpec:
    seed: int
    draw_count: int
    common_uniforms: tuple[str, ...] = ()
    common_random_map_digest: str | None = None
    _derived_proof: InitVar[_DerivedSamplingSpecProof | None] = None
    _validated_common_uniforms: tuple[Decimal, ...] = field(
        init=False, repr=False, compare=False, default=()
    )
    _common_uniform_exponent: int | None = field(
        init=False, repr=False, compare=False, default=None
    )
    _scaled_common_uniforms: tuple[int, ...] = field(
        init=False, repr=False, compare=False, default=()
    )
    _standard_probability_words_le: bytes | None = field(
        init=False, repr=False, compare=False, default=None
    )

    def __post_init__(self, _derived_proof: _DerivedSamplingSpecProof | None) -> None:
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
        if self.common_random_map_digest is not None:
            _require_digest(self.common_random_map_digest, "common_random_map_digest")
        if self.common_uniforms and self.common_random_map_digest is None:
            raise ContractError("injected common uniforms require a common_random_map_digest")
        if _accepts_derived_sampling_spec_proof(
            _derived_proof,
            seed=self.seed,
            draw_count=self.draw_count,
            common_uniforms=self.common_uniforms,
            common_random_map_digest=self.common_random_map_digest,
        ):
            assert _derived_proof is not None
            validated = _derived_proof.validated_common_uniforms
            common_exponent = _derived_proof.common_uniform_exponent
            scaled = _derived_proof.scaled_common_uniforms
            standard_words = _derived_proof.standard_probability_words_le
        else:
            parsed = []
            for value in self.common_uniforms:
                probability = _require_probability(value)
                if not (Decimal("0") < probability < Decimal("1")):
                    raise ContractError("common uniforms must be strictly between zero and one")
                parsed.append(probability)
            validated = tuple(parsed)
            common_exponent, scaled = _decimals_to_common_scale(validated)
            standard_words = _standard_probability_words(self.common_uniforms)
        object.__setattr__(self, "_validated_common_uniforms", validated)
        object.__setattr__(self, "_common_uniform_exponent", common_exponent)
        object.__setattr__(self, "_scaled_common_uniforms", scaled)
        object.__setattr__(self, "_standard_probability_words_le", standard_words)

    @property
    def validated_common_uniforms(self) -> tuple[Decimal, ...]:
        """Return the exact already-validated probabilities without reparsing."""

        return self._validated_common_uniforms


def _install_sampling_spec_derivation_capability():
    token = object()

    def accepts(
        proof: _DerivedSamplingSpecProof | None,
        *,
        seed: int,
        draw_count: int,
        common_uniforms: tuple[str, ...],
        common_random_map_digest: str | None,
    ) -> bool:
        return (
            isinstance(proof, _DerivedSamplingSpecProof)
            and proof.token is token
            and proof.seed == seed
            and proof.draw_count == draw_count
            and proof.common_uniforms is common_uniforms
            and proof.common_random_map_digest == common_random_map_digest
            and len(proof.validated_common_uniforms) == draw_count
        )

    def derive(source: SamplingSpec, *, common_random_map_digest: str) -> SamplingSpec:
        if not isinstance(source, SamplingSpec) or not source.common_uniforms:
            raise ContractError("sampling derivation requires validated common uniforms")
        return SamplingSpec(
            source.seed,
            source.draw_count,
            source.common_uniforms,
            common_random_map_digest,
            _DerivedSamplingSpecProof(
                token,
                source.seed,
                source.draw_count,
                source.common_uniforms,
                common_random_map_digest,
                source.validated_common_uniforms,
                source._common_uniform_exponent,
                source._scaled_common_uniforms,
                source._standard_probability_words_le,
            ),
        )

    def build_generated(
        *,
        seed: int,
        draw_count: int,
        common_uniforms: tuple[str, ...],
        common_random_map_digest: str,
    ) -> SamplingSpec:
        """Bind uniforms produced by the frozen in-process generator once.

        The joint generator already creates canonical probabilities inside this
        process.  Parsing their decimal values is retained for the public typed
        view, while the expensive generic transport validation and Decimal-tree
        rescaling are replaced by the equivalent direct canonical-string scale.
        Serialized/reconstructed SamplingSpec values still take the full public
        validation path in ``SamplingSpec.__post_init__``.
        """

        if (
            not isinstance(common_uniforms, tuple)
            or len(common_uniforms) != draw_count
            or any(
                not isinstance(value, str)
                or not value.startswith("0.")
                or not value[2:]
                or not value[2:].isdigit()
                or value.endswith("0")
                for value in common_uniforms
            )
        ):
            raise ContractError("generated common uniforms are not canonical")
        validated = tuple(Decimal(value) for value in common_uniforms)
        # The Windows production path consumes the sealed 1e-28 words directly.
        # Retain parsed Decimals as the exact portable fallback instead of also
        # constructing a second 4,096-element arbitrary-precision integer cache.
        exponent, scaled = None, ()
        standard_words = _standard_probability_words(common_uniforms)
        return SamplingSpec(
            seed,
            draw_count,
            common_uniforms,
            common_random_map_digest,
            _DerivedSamplingSpecProof(
                token,
                seed,
                draw_count,
                common_uniforms,
                common_random_map_digest,
                validated,
                exponent,
                scaled,
                standard_words,
            ),
        )

    return derive, build_generated, accepts


(
    _derive_sampling_spec,
    _build_generated_sampling_spec,
    _accepts_derived_sampling_spec_proof,
) = _install_sampling_spec_derivation_capability()
del _install_sampling_spec_derivation_capability


@dataclass(frozen=True, slots=True)
class _GeneratedDistributionSamplesProof:
    token: object
    samples_ms: tuple[int, ...]
    seed: int
    distribution_digest: str
    common_random_map_digest: str | None
    samples_digest: str
    samples_authority_digest: str


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
    _generated_proof: InitVar[_GeneratedDistributionSamplesProof | None] = None
    _samples_authority_digest_cache: str = field(init=False, repr=False, compare=False, default="")

    def __post_init__(self, _generated_proof: _GeneratedDistributionSamplesProof | None) -> None:
        if not isinstance(self.samples_ms, tuple) or not self.samples_ms:
            raise ContractError("samples_ms must be a nonempty immutable tuple")
        trusted_generation = _accepts_generated_distribution_samples_proof(
            _generated_proof,
            samples_ms=self.samples_ms,
            seed=self.seed,
            distribution_digest=self.distribution_digest,
            common_random_map_digest=self.common_random_map_digest,
            samples_digest=self.samples_digest,
        )
        if not trusted_generation:
            for sample in self.samples_ms:
                _require_positive_int(sample, "sample time_ms")
        if self.algorithm != SAMPLING_ALGORITHM:
            raise ContractError("unknown distribution sampling algorithm")
        if self.dependency_version != "stdlib-only-v1":
            raise ContractError("unknown sampling dependency version")
        if not trusted_generation:
            SamplingSpec(self.seed, self.draw_count)
        if self.draw_count != len(self.samples_ms):
            raise ContractError("draw_count must match samples_ms")
        if self.time_quantum_ms != 1:
            raise ContractError("unknown sampling time quantum")
        _require_digest(self.distribution_digest, "distribution_digest")
        _require_digest(self.samples_digest, "samples_digest")
        if self.common_random_map_digest is not None:
            _require_digest(self.common_random_map_digest, "common_random_map_digest")
        if not trusted_generation:
            expected_digest = _samples_digest(
                samples_ms=self.samples_ms,
                seed=self.seed,
                distribution_digest=self.distribution_digest,
                common_random_map_digest=self.common_random_map_digest,
            )
            if self.samples_digest != expected_digest:
                raise ContractError("distribution samples digest mismatch")
            samples_authority_digest = canonical_digest(self.samples_ms)
        else:
            assert _generated_proof is not None
            samples_authority_digest = _generated_proof.samples_authority_digest
        object.__setattr__(self, "_samples_authority_digest_cache", samples_authority_digest)

    @property
    def samples_authority_digest(self) -> str:
        return self._samples_authority_digest_cache

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


def _install_distribution_samples_generation_capability():
    token = object()

    def accepts(
        proof: _GeneratedDistributionSamplesProof | None,
        *,
        samples_ms: tuple[int, ...],
        seed: int,
        distribution_digest: str,
        common_random_map_digest: str | None,
        samples_digest: str,
    ) -> bool:
        return (
            isinstance(proof, _GeneratedDistributionSamplesProof)
            and proof.token is token
            and proof.samples_ms is samples_ms
            and proof.seed == seed
            and proof.distribution_digest == distribution_digest
            and proof.common_random_map_digest == common_random_map_digest
            and proof.samples_digest == samples_digest
        )

    def build(
        *,
        samples_ms: tuple[int, ...],
        seed: int,
        distribution_digest: str,
        common_random_map_digest: str | None,
    ) -> DistributionSamples:
        generated_digests = _samples_digest(
            samples_ms=samples_ms,
            seed=seed,
            distribution_digest=distribution_digest,
            common_random_map_digest=common_random_map_digest,
            _include_authority=True,
        )
        assert isinstance(generated_digests, tuple)
        samples_digest, samples_authority_digest = generated_digests
        return DistributionSamples(
            samples_ms=samples_ms,
            algorithm=SAMPLING_ALGORITHM,
            dependency_version="stdlib-only-v1",
            seed=seed,
            draw_count=len(samples_ms),
            time_quantum_ms=1,
            distribution_digest=distribution_digest,
            samples_digest=samples_digest,
            common_random_map_digest=common_random_map_digest,
            _generated_proof=_GeneratedDistributionSamplesProof(
                token,
                samples_ms,
                seed,
                distribution_digest,
                common_random_map_digest,
                samples_digest,
                samples_authority_digest,
            ),
        )

    return build, accepts


_build_distribution_samples, _accepts_generated_distribution_samples_proof = (
    _install_distribution_samples_generation_capability()
)
del _install_distribution_samples_generation_capability


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
            raise ContractError("distribution requires at least three immutable quantiles")
        if not all(isinstance(item, QuantilePoint) for item in self.quantiles):
            raise ContractError("distribution quantiles must be QuantilePoint values")
        probabilities = tuple(Decimal(item.probability) for item in self.quantiles)
        times = tuple(item.time_ms for item in self.quantiles)
        if probabilities != tuple(sorted(probabilities)) or len(set(probabilities)) != len(
            probabilities
        ):
            raise ContractError("quantile probabilities must be unique and strictly ordered")
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
        native = _sample_standard_quantile_rows((self,), spec)
        if native is not None:
            samples = native[0]
        elif spec._common_uniform_exponent is not None:
            samples = self._sample_scaled_probabilities(
                spec._scaled_common_uniforms,
                spec._common_uniform_exponent,
            )
        else:
            probabilities = spec.validated_common_uniforms or tuple(
                Decimal(item) for item in _splitmix_uniforms(spec.seed, spec.draw_count)
            )
            samples = self._sample_probabilities(probabilities)
        distribution_digest = self.digest
        return _build_distribution_samples(
            samples_ms=samples,
            seed=spec.seed,
            distribution_digest=distribution_digest,
            common_random_map_digest=spec.common_random_map_digest,
        )

    def _at_probability(self, probability: Decimal) -> int:
        with localcontext() as context:
            context.prec = 256
            context.rounding = ROUND_HALF_EVEN
            return self._at_probability_compiled(probability)

    def _sample_probabilities(self, probabilities: tuple[Decimal, ...]) -> tuple[int, ...]:
        """Evaluate a batch under one frozen Decimal context."""

        with localcontext() as context:
            context.prec = 256
            context.rounding = ROUND_HALF_EVEN
            return tuple(
                self._at_probability_compiled(probability) for probability in probabilities
            )

    def _sample_scaled_probabilities(
        self, probabilities: tuple[int, ...], probability_exponent: int
    ) -> tuple[int, ...]:
        """Evaluate exact finite-decimal probabilities with integer arithmetic."""

        grid_exponent, grid = _decimals_to_common_scale(self._probabilities)
        assert grid_exponent is not None
        common_exponent = min(probability_exponent, grid_exponent)
        probability_factor = 10 ** (probability_exponent - common_exponent)
        grid_factor = 10 ** (grid_exponent - common_exponent)
        scaled_grid = tuple(value * grid_factor for value in grid)
        return tuple(
            _interpolate_scaled_probability(
                probability * probability_factor,
                scaled_grid,
                self._times_ms,
            )
            for probability in probabilities
        )

    def _sample_rational_probabilities(
        self, probabilities: tuple[tuple[int, int], ...]
    ) -> tuple[int, ...]:
        """Evaluate exact nonnegative rational probabilities with integer math."""

        grid_exponent, grid = _decimals_to_common_scale(self._probabilities)
        assert grid_exponent is not None
        grid_denominator = 10 ** (-grid_exponent)
        results = []
        for numerator, denominator in probabilities:
            index = 0
            target = numerator * grid_denominator
            while index < len(grid) and grid[index] * denominator < target:
                index += 1
            if index == 0:
                results.append(self._times_ms[0])
                continue
            if index == len(grid):
                results.append(self._times_ms[-1])
                continue
            left_grid = grid[index - 1]
            ratio_numerator = target - left_grid * denominator
            ratio_denominator = denominator * (grid[index] - left_grid)
            left_time = self._times_ms[index - 1]
            exact_numerator = left_time * ratio_denominator + ratio_numerator * (
                self._times_ms[index] - left_time
            )
            rounded, remainder = divmod(exact_numerator, ratio_denominator)
            doubled = remainder * 2
            if doubled > ratio_denominator or (doubled == ratio_denominator and rounded % 2):
                rounded += 1
            results.append(rounded)
        return tuple(results)

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


def sample_aligned_positive_distributions(
    distributions: tuple[PositiveTimeDistribution, ...],
    spec: SamplingSpec,
) -> tuple[DistributionSamples, ...]:
    """Sample distributions while sharing inverse-CDF ratios for one grid.

    This is an exact batch form of ``PositiveTimeDistribution.sample``.  A
    mismatched quantile grid uses the individual oracle path.
    """

    if not isinstance(distributions, tuple) or not distributions:
        raise ContractError("aligned sampling requires a nonempty distribution tuple")
    if not all(isinstance(item, PositiveTimeDistribution) for item in distributions):
        raise ContractError("aligned sampling requires positive-time distributions")
    if not isinstance(spec, SamplingSpec):
        raise ContractError("aligned sampling requires a SamplingSpec")

    shared_grid = distributions[0]._probabilities
    if any(item._probabilities != shared_grid for item in distributions[1:]):
        return tuple(item.sample(spec) for item in distributions)

    native = _sample_standard_quantile_rows(distributions, spec)
    if native is not None:
        return _distribution_sample_rows(distributions, [list(row) for row in native], spec)
    if spec._common_uniform_exponent is not None:
        sample_rows = [
            list(
                distribution._sample_scaled_probabilities(
                    spec._scaled_common_uniforms,
                    spec._common_uniform_exponent,
                )
            )
            for distribution in distributions
        ]
        return _distribution_sample_rows(distributions, sample_rows, spec)

    probabilities = spec.validated_common_uniforms or tuple(
        Decimal(item) for item in _splitmix_uniforms(spec.seed, spec.draw_count)
    )
    sample_rows = [[] for _item in distributions]
    with localcontext() as context:
        context.prec = 256
        context.rounding = ROUND_HALF_EVEN
        for probability in probabilities:
            index = bisect_left(shared_grid, probability)
            if index == 0:
                for row, distribution in zip(sample_rows, distributions, strict=True):
                    row.append(distribution._times_ms[0])
                continue
            if index == len(shared_grid):
                for row, distribution in zip(sample_rows, distributions, strict=True):
                    row.append(distribution._times_ms[-1])
                continue
            left_p = shared_grid[index - 1]
            ratio = (probability - left_p) / (shared_grid[index] - left_p)
            for row, distribution in zip(sample_rows, distributions, strict=True):
                left_t = distribution._times_ms[index - 1]
                right_t = distribution._times_ms[index]
                interpolated = Decimal(left_t) + ratio * Decimal(right_t - left_t)
                row.append(int(interpolated.quantize(Decimal("1"))))

    return _distribution_sample_rows(distributions, sample_rows, spec)


def _sample_standard_quantile_rows(
    distributions: tuple[PositiveTimeDistribution, ...],
    spec: SamplingSpec,
) -> tuple[tuple[int, ...], ...] | None:
    """Use the hash-bound exact kernel for the production three-point grid."""

    if (
        spec.draw_count != 4096
        or spec._standard_probability_words_le is None
        or not 1 <= len(distributions) <= 3
        or any(item._probabilities != _STANDARD_QUANTILE_GRID for item in distributions)
    ):
        return None
    global _NATIVE_STANDARD_SAMPLER, _NATIVE_STANDARD_SAMPLER_INITIALIZED
    if not _NATIVE_STANDARD_SAMPLER_INITIALIZED:
        from strathmark.v3.domain.optimizer_kernel import load_bundled_kernel

        _NATIVE_STANDARD_SAMPLER = load_bundled_kernel(required=sys.platform == "win32")
        _NATIVE_STANDARD_SAMPLER_INITIALIZED = True
    if _NATIVE_STANDARD_SAMPLER is None:
        return None
    return _NATIVE_STANDARD_SAMPLER.sample_three_quantiles(
        spec._standard_probability_words_le,
        tuple(item._times_ms for item in distributions),
        draw_count=spec.draw_count,
    )


def _distribution_sample_rows(
    distributions: tuple[PositiveTimeDistribution, ...],
    sample_rows: list[list[int]],
    spec: SamplingSpec,
) -> tuple[DistributionSamples, ...]:
    results = []
    for distribution, row in zip(distributions, sample_rows, strict=True):
        samples = tuple(row)
        distribution_digest = distribution.digest
        results.append(
            _build_distribution_samples(
                samples_ms=samples,
                seed=spec.seed,
                distribution_digest=distribution_digest,
                common_random_map_digest=spec.common_random_map_digest,
            )
        )
    return tuple(results)


def _decimals_to_common_scale(
    values: tuple[Decimal, ...],
) -> tuple[int | None, tuple[int, ...]]:
    """Represent finite decimals as integers sharing one base-ten exponent."""

    if not values:
        return None, ()
    exponent = min(value.as_tuple().exponent for value in values)
    scaled = []
    for value in values:
        sign, digits, value_exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        if sign:
            coefficient = -coefficient
        scaled.append(coefficient * 10 ** (value_exponent - exponent))
    return exponent, tuple(scaled)


def _canonical_fraction_strings_to_common_scale(
    values: tuple[str, ...],
) -> tuple[int | None, tuple[int, ...]]:
    """Scale canonical generated ``0.<digits>`` values without Decimal walking."""

    if not values:
        return None, ()
    fractions = tuple(value[2:] for value in values)
    exponent = -max(len(value) for value in fractions)
    return exponent, tuple(int(value) * 10 ** (-len(value) - exponent) for value in fractions)


def _standard_probability_words(values: tuple[str, ...]) -> bytes | None:
    """Pack exact 1e-28 probabilities for the sealed three-quantile kernel.

    Generated rank uniforms have at most 28 significant digits. Values with
    additional leading fractional zeroes are below the standard 0.1 floor and
    can be represented as zero without changing inverse-quantile output. Public
    higher-precision values inside the interpolation range retain the generic
    integer oracle instead.
    """

    if not values:
        return None
    packed = bytearray(len(values) * 16)
    for index, value in enumerate(values):
        digits = value[2:]
        if len(digits) > 28:
            if digits[0] != "0":
                return None
            scaled = 0
        else:
            scaled = int(digits) * 10 ** (28 - len(digits))
        struct.pack_into(
            "<QQ",
            packed,
            index * 16,
            scaled & _UINT64_MAX,
            scaled >> 64,
        )
    return bytes(packed)


def _interpolate_scaled_probability(
    probability: int,
    grid: tuple[int, ...],
    times_ms: tuple[int, ...],
) -> int:
    index = bisect_left(grid, probability)
    if index == 0:
        return times_ms[0]
    if index == len(grid):
        return times_ms[-1]
    left_probability = grid[index - 1]
    denominator = grid[index] - left_probability
    numerator = probability - left_probability
    left_time = times_ms[index - 1]
    exact_numerator = left_time * denominator + numerator * (times_ms[index] - left_time)
    rounded, remainder = divmod(exact_numerator, denominator)
    doubled_remainder = remainder * 2
    if doubled_remainder > denominator or (doubled_remainder == denominator and rounded % 2):
        rounded += 1
    return rounded


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
        _require_nonnegative_decimal(self.effective_sample_size, "effective_sample_size")
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
        _require_nonnegative_int(self.tournament_event_sequence, "tournament_event_sequence")

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
                raise ContractError("committed forecast requires a positive distribution")
            if self.abstention_code is not None:
                raise ContractError("committed forecast cannot carry an abstention code")
        else:
            if self.distribution is not None:
                raise ContractError("distribution must be absent for non-committed forecasts")
            if not isinstance(self.abstention_code, str) or not self.abstention_code:
                raise ContractError("non-committed forecast requires an abstention code")
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
            raise ContractError("unknown assessor, forecast state, or warning code") from exc
        distribution = value["distribution"]
        return cls(
            forecast_id=require_identifier(value["forecast_id"], expected_namespace="forecast"),
            assessor=assessor,
            state=state,
            evidence_digest=value["evidence_digest"],
            distribution=(
                None if distribution is None else PositiveTimeDistribution.from_dict(distribution)
            ),
            support=EvidenceSupport.from_dict(value["support"]),
            warnings=warnings,
            artifacts=tuple(ArtifactIdentity.from_dict(item) for item in value["artifacts"]),
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
            None if arguments["distribution"] is None else arguments["distribution"].to_dict()
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


def _generated_samples_digests(
    *,
    samples_ms: tuple[int, ...],
    seed: int,
    distribution_digest: str,
    common_random_map_digest: str | None,
) -> tuple[str, str]:
    """Hash one sampler-produced vector and its envelope in one encoding pass."""

    samples_json = ("[" + ",".join(str(item) for item in samples_ms) + "]").encode("ascii")
    map_value = "null" if common_random_map_digest is None else f'"{common_random_map_digest}"'
    encoded = (
        (
            f'{{"algorithm":"{SAMPLING_ALGORITHM}",'
            f'"common_random_map_digest":{map_value},'
            '"dependency_version":"stdlib-only-v1",'
            f'"distribution_digest":"{distribution_digest}",'
            f'"draw_count":{len(samples_ms)},"samples_ms":'
        ).encode("ascii")
        + samples_json
        + (
            f',"schema_version":"{SAMPLING_SCHEMA_VERSION}","seed":{seed},"time_quantum_ms":1}}'
        ).encode("ascii")
    )
    return hashlib.sha256(encoded).hexdigest(), hashlib.sha256(samples_json).hexdigest()


def _samples_digest(
    *,
    samples_ms: tuple[int, ...],
    seed: int,
    distribution_digest: str,
    common_random_map_digest: str | None,
    _include_authority: bool = False,
) -> str | tuple[str, str]:
    if _include_authority:
        return _generated_samples_digests(
            samples_ms=samples_ms,
            seed=seed,
            distribution_digest=distribution_digest,
            common_random_map_digest=common_random_map_digest,
        )
    value = {
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
    # This closed-schema path receives validated positive int64 samples and
    # lower-case SHA-256 identifiers.  Stream its canonical JSON directly so
    # race-card assembly does not normalize thousands of integers hundreds of
    # times.  Values outside the proven language retain the generic canonical
    # boundary and its exact failures.
    digests = (distribution_digest,) + (
        () if common_random_map_digest is None else (common_random_map_digest,)
    )
    fast_path = (
        isinstance(samples_ms, tuple)
        and 0 < len(samples_ms) < 100_000
        and all(type(item) is int and 0 < item <= 2**63 - 1 for item in samples_ms)
        and type(seed) is int
        and 0 <= seed <= 2**63 - 1
        and all(
            isinstance(item, str)
            and len(item) == 64
            and all(character in "0123456789abcdef" for character in item)
            for item in digests
        )
    )
    if not fast_path:
        return canonical_digest(value)
    map_value = "null" if common_random_map_digest is None else f'"{common_random_map_digest}"'
    encoded = (
        f'{{"algorithm":"{SAMPLING_ALGORITHM}",'
        f'"common_random_map_digest":{map_value},'
        '"dependency_version":"stdlib-only-v1",'
        f'"distribution_digest":"{distribution_digest}",'
        f'"draw_count":{len(samples_ms)},"samples_ms":['
        + ",".join(str(item) for item in samples_ms)
        + f'],"schema_version":"{SAMPLING_SCHEMA_VERSION}",'
        f'"seed":{seed},"time_quantum_ms":1}}'
    ).encode("ascii")
    if len(encoded) > 1_048_576:
        return canonical_digest(value)
    return hashlib.sha256(encoded).hexdigest()


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
    "sample_aligned_positive_distributions",
]
