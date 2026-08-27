"""Deterministic V3 fairness-frontier handicap-mark optimization.

V3 intentionally does not share code with the locked V2 optimizer.  All time
mechanics cross this boundary as integer milliseconds and every candidate is
compared against the same sealed 4,096-draw field sample.
"""

from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum
from fractions import Fraction  # noqa: F401 - tests prove the hot path avoids it
from functools import wraps
from hashlib import sha256
from itertools import product
from math import lcm
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence, TypeAlias

import numpy as np

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import AssessorForecast, AssessorKind
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.domain.credibility import (
    HandicapConsequenceMetrics,
    OptimizerConsequenceReceipt,
)
from strathmark.v3.domain.optimizer_kernel import (
    bundled_kernel_identity,
    load_bundled_kernel,
)

if TYPE_CHECKING:
    from strathmark.v3.application.credibility_reactions import OptimizerScoringInput
    from strathmark.v3.domain.joint_dependence import JointDraws


OPTIMIZER_VERSION = "fairness-frontier-v1"
PARETO_TOLERANCE = Decimal("0.000000001")
NUMPY_DEPENDENCY_VERSION = np.__version__
_RawObjective: TypeAlias = tuple[int, int, int, int]


@dataclass(slots=True)
class _WinnerComparisonCache:
    dense: list[list[list[int | None]]]
    fallback: dict[tuple[int, int, int], int]

    @classmethod
    def create(cls, entrant_count: int) -> _WinnerComparisonCache:
        return cls(
            [[[None] * 361 for _right in range(entrant_count)] for _left in range(entrant_count)],
            {},
        )


@dataclass(frozen=True, slots=True)
class _EvaluationContext:
    expected: Any
    samples: Any
    baseline: Any
    ideal_delays: Any
    ideal_square_sum: int
    entrant_count: int
    credit_scale: int
    draw_count: int
    parity_denominator: int
    winner_comparison_masks: _WinnerComparisonCache
    native_slot: _NativeEvaluationSlot


@dataclass(slots=True)
class _NativeEvaluationSlot:
    kernel: Any | None
    context: Any | None = None

    def get(self, samples: Any) -> Any | None:
        if self.kernel is None:
            return None
        if self.context is None:
            self.context = self.kernel.context(samples)
        return self.context


_GENERATED_OPTIMIZER_CAPABILITY = object()
MAX_OPTIMIZER_ENTRANTS = 12
OPTIMIZER_DECIMAL_PRECISION = 96
_EVALUATION_BATCH_SIZE = 96
_NATIVE_KERNEL_IDENTITY = bundled_kernel_identity()
_NATIVE_OPTIMIZER_KERNEL = load_bundled_kernel()


def _validated_sample_tuple_digest(values: tuple[int, ...]) -> str:
    """Hash the exact canonical JSON integer array without generic tree walking."""

    digest = sha256()
    digest.update(b"[")
    for index, value in enumerate(values):
        if index:
            digest.update(b",")
        digest.update(str(value).encode("ascii"))
    digest.update(b"]")
    return digest.hexdigest()


def _frozen_optimizer_decimal(function):
    """Run authority-affecting Decimal work under one installed context."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = OPTIMIZER_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return function(*args, **kwargs)

    return wrapped


class OptimizerFallback(str, Enum):
    EMPTY_FIELD = "empty_field"
    OPTIMIZER_FAILURE = "optimizer_failure"
    EMPTY_FRONTIER = "empty_frontier"
    NONFINITE_FRONTIER = "nonfinite_frontier"
    RANK_INVALID = "rank_invalid"
    NO_VALID_IMPROVEMENT = "no_valid_improvement"


@dataclass(frozen=True, slots=True)
class OptimizerPolicy:
    sample_count: int = 4096
    small_field_maximum: int = 6
    small_field_radius_seconds: int = 3
    beam_width: int = 512
    maximum_expansion_rounds: int = 128
    parallel_workers: int = 8
    pareto_tolerance: str = "0.000000001"
    version: str = OPTIMIZER_VERSION

    def __post_init__(self) -> None:
        expected = (4096, 6, 3, 512, 128, 8, "0.000000001", OPTIMIZER_VERSION)
        actual = (
            self.sample_count,
            self.small_field_maximum,
            self.small_field_radius_seconds,
            self.beam_width,
            self.maximum_expansion_rounds,
            self.parallel_workers,
            self.pareto_tolerance,
            self.version,
        )
        if actual != expected:
            raise ContractError("optimizer policy differs from the frozen V3 bootstrap")

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "small_field_maximum": self.small_field_maximum,
            "small_field_radius_seconds": self.small_field_radius_seconds,
            "beam_width": self.beam_width,
            "maximum_expansion_rounds": self.maximum_expansion_rounds,
            "parallel_workers": self.parallel_workers,
            "pareto_tolerance": self.pareto_tolerance,
            "version": self.version,
        }


DEFAULT_OPTIMIZER_POLICY = OptimizerPolicy()


def _implementation_artifact_value() -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-optimizer-implementation-v1",
        "algorithm": OPTIMIZER_VERSION,
        "policy": DEFAULT_OPTIMIZER_POLICY.to_dict(),
        "numeric_core": "numpy-integer-vector-with-sealed-rust-kernel-v1",
        "numpy_version": NUMPY_DEPENDENCY_VERSION,
        "native_kernel": _NATIVE_KERNEL_IDENTITY,
        "chim_geometry": "decimal-jacobi-svd-v1",
        "tie_credit": "exact-lcm-split-v1",
    }


def _source_sha256() -> str:
    return sha256(Path(__file__).read_bytes()).hexdigest()


def implementation_artifact_digest() -> str:
    return canonical_digest({**_implementation_artifact_value(), "source_sha256": _source_sha256()})


OPTIMIZER_IMPLEMENTATION_DIGEST = implementation_artifact_digest()


@dataclass(frozen=True, slots=True)
class OptimizationCompetitor:
    competitor_id: StableIdentifier
    expected_time_ms: int
    samples_ms: tuple[int, ...]
    upstream_index: int
    distribution_digest: str | None = None
    _samples_digest_cache: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        _positive_int(self.expected_time_ms, "expected_time_ms")
        if (
            not isinstance(self.samples_ms, tuple)
            or len(self.samples_ms) != DEFAULT_OPTIMIZER_POLICY.sample_count
        ):
            raise ContractError("optimizer competitors require exactly 4096 joint samples")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.samples_ms
        ):
            raise ContractError("optimizer samples must be positive integer milliseconds")
        if self.expected_time_ms > 2_000_000_000 or max(self.samples_ms) > 2_000_000_000:
            raise ContractError("optimizer time inputs exceed the frozen int32 capacity bound")
        if (
            isinstance(self.upstream_index, bool)
            or not isinstance(self.upstream_index, int)
            or self.upstream_index < 0
        ):
            raise ContractError("upstream_index must be a nonnegative integer")
        object.__setattr__(
            self, "_samples_digest_cache", _validated_sample_tuple_digest(self.samples_ms)
        )
        if self.distribution_digest is None:
            object.__setattr__(
                self,
                "distribution_digest",
                canonical_digest(
                    {
                        "expected_time_ms": self.expected_time_ms,
                        "samples_digest": self.samples_digest,
                    }
                ),
            )
        _digest(self.distribution_digest, "distribution_digest")

    @property
    def samples_digest(self) -> str:
        return self._samples_digest_cache

    @classmethod
    def _from_generated_joint(
        cls,
        *,
        competitor_id: StableIdentifier,
        expected_time_ms: int,
        samples_ms: tuple[int, ...],
        upstream_index: int,
        distribution_digest: str,
        samples_authority_digest: str,
        _capability: object,
    ) -> OptimizationCompetitor:
        if _capability is not _GENERATED_OPTIMIZER_CAPABILITY:
            raise ContractError("generated optimizer input proof is not verifier-owned")
        instance = object.__new__(cls)
        object.__setattr__(instance, "competitor_id", competitor_id)
        object.__setattr__(instance, "expected_time_ms", expected_time_ms)
        object.__setattr__(instance, "samples_ms", samples_ms)
        object.__setattr__(instance, "upstream_index", upstream_index)
        object.__setattr__(instance, "distribution_digest", distribution_digest)
        object.__setattr__(instance, "_samples_digest_cache", samples_authority_digest)
        return instance


@dataclass(frozen=True, slots=True)
class OptimizationField:
    field_id: StableIdentifier
    source_receipt_digest: str
    seed: int
    competitors: tuple[OptimizationCompetitor, ...]
    joint_samples_digest: str
    sample_matrix_digest: str
    pool_receipt_digest: str
    input_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        _digest(self.source_receipt_digest, "source_receipt_digest")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ContractError("optimizer seed must be a nonnegative integer")
        if not isinstance(self.competitors, tuple) or not all(
            isinstance(item, OptimizationCompetitor) for item in self.competitors
        ):
            raise ContractError("optimizer field competitors must be immutable typed values")
        if len(self.competitors) > MAX_OPTIMIZER_ENTRANTS:
            raise ContractError("optimizer field supports at most 12 entrants")
        identities = tuple(str(item.competitor_id) for item in self.competitors)
        indices = tuple(item.upstream_index for item in self.competitors)
        if len(identities) != len(set(identities)):
            raise ContractError("optimizer field contains duplicate competitor identity")
        if indices != tuple(range(len(self.competitors))):
            raise ContractError("optimizer field upstream indices must be contiguous and ordered")
        _digest(self.joint_samples_digest, "joint_samples_digest")
        _digest(self.sample_matrix_digest, "sample_matrix_digest")
        _digest(self.pool_receipt_digest, "pool_receipt_digest")
        _digest(self.input_digest, "input_digest")
        if self.sample_matrix_digest != canonical_digest(
            [item.samples_digest for item in self.competitors]
        ):
            raise ContractError("optimizer sample matrix digest mismatch")
        if self.input_digest != canonical_digest(self.content_value()):
            raise ContractError("optimizer field input digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-optimizer-input-v1",
            "field_id": str(self.field_id),
            "source_receipt_digest": self.source_receipt_digest,
            "seed": self.seed,
            "competitors": [
                {
                    "competitor_id": str(item.competitor_id),
                    "expected_time_ms": item.expected_time_ms,
                    "upstream_index": item.upstream_index,
                    "distribution_digest": item.distribution_digest,
                    "samples_digest": item.samples_digest,
                }
                for item in self.competitors
            ],
            "joint_samples_digest": self.joint_samples_digest,
            "sample_matrix_digest": self.sample_matrix_digest,
            "pool_receipt_digest": self.pool_receipt_digest,
        }

    @classmethod
    def create(
        cls,
        *,
        field_id: StableIdentifier,
        source_receipt_digest: str,
        competitors: tuple[OptimizationCompetitor, ...],
        pool_receipt_digest: str | None = None,
    ) -> OptimizationField:
        _digest(source_receipt_digest, "source_receipt_digest")
        identities = tuple(str(item.competitor_id) for item in competitors)
        if len(identities) != len(set(identities)):
            raise ContractError("optimizer field contains duplicate competitor identity")
        matrix_digest = canonical_digest([item.samples_digest for item in competitors])
        pool_digest = pool_receipt_digest or source_receipt_digest
        _digest(pool_digest, "pool_receipt_digest")
        values = {
            "field_id": field_id,
            "source_receipt_digest": source_receipt_digest,
            "seed": _seed_from_digest(source_receipt_digest),
            "competitors": competitors,
            "joint_samples_digest": matrix_digest,
            "sample_matrix_digest": matrix_digest,
            "pool_receipt_digest": pool_digest,
        }
        content = {
            "schema_version": "strathmark-v3-optimizer-input-v1",
            "field_id": str(field_id),
            "source_receipt_digest": source_receipt_digest,
            "seed": values["seed"],
            "competitors": [
                {
                    "competitor_id": str(item.competitor_id),
                    "expected_time_ms": item.expected_time_ms,
                    "upstream_index": item.upstream_index,
                    "distribution_digest": item.distribution_digest,
                    "samples_digest": item.samples_digest,
                }
                for item in competitors
            ],
            "joint_samples_digest": matrix_digest,
            "sample_matrix_digest": matrix_digest,
            "pool_receipt_digest": pool_digest,
        }
        return cls(**values, input_digest=canonical_digest(content))

    @classmethod
    def from_joint_draws(
        cls,
        draws: JointDraws,
        *,
        forecasts: tuple[Any, ...],
        source_receipt_digest: str,
        pool_receipt_digest: str,
    ) -> OptimizationField:
        from strathmark.v3.domain.joint_dependence import (
            JointDraws,
            has_fresh_joint_generation_proof,
        )

        if not isinstance(draws, JointDraws):
            raise ContractError("optimizer requires typed U13 joint draws")
        if draws.inputs.draw_count != DEFAULT_OPTIMIZER_POLICY.sample_count:
            raise ContractError("optimizer joint draws must contain exactly 4096 samples")
        from strathmark.v3.domain.joint_dependence import FieldCompetitorForecast

        if not isinstance(forecasts, tuple) or not all(
            isinstance(item, FieldCompetitorForecast) for item in forecasts
        ):
            raise ContractError("optimizer expected-time basis must be typed U13 forecasts")
        by_index = {item.crn_index: item for item in forecasts}
        if set(by_index) != {item.crn_index for item in draws.competitors}:
            raise ContractError("U13 forecast basis differs from joint draw roster")
        fresh_joint = has_fresh_joint_generation_proof(draws)
        rows = tuple(
            (
                OptimizationCompetitor._from_generated_joint(
                    competitor_id=item.competitor_id,
                    expected_time_ms=by_index[item.crn_index].distribution.median_ms,
                    samples_ms=item.samples_ms,
                    upstream_index=index,
                    distribution_digest=item.distribution_digest,
                    samples_authority_digest=item.samples_authority_digest,
                    _capability=_GENERATED_OPTIMIZER_CAPABILITY,
                )
                if fresh_joint
                else OptimizationCompetitor(
                    item.competitor_id,
                    by_index[item.crn_index].distribution.median_ms,
                    item.samples_ms,
                    index,
                    item.distribution_digest,
                )
            )
            for index, item in enumerate(draws.competitors)
        )
        for draw, basis in zip(draws.competitors, rows, strict=True):
            forecast_basis = by_index[draw.crn_index]
            if (
                draw.competitor_id != forecast_basis.competitor_id
                or draw.draw_slot != forecast_basis.draw_slot
                or draw.distribution_digest != forecast_basis.distribution.digest
                or basis.distribution_digest != forecast_basis.distribution.digest
            ):
                raise ContractError("expected-time basis is not bound to exact U13 draws")
        matrix_digest = canonical_digest([item.samples_digest for item in rows])
        values = {
            "field_id": draws.inputs.field_id,
            "source_receipt_digest": source_receipt_digest,
            "seed": draws.inputs.seed,
            "competitors": rows,
            "joint_samples_digest": draws.joint_samples_digest,
            "sample_matrix_digest": matrix_digest,
            "pool_receipt_digest": pool_receipt_digest,
        }
        content = {
            "schema_version": "strathmark-v3-optimizer-input-v1",
            "field_id": str(draws.inputs.field_id),
            "source_receipt_digest": source_receipt_digest,
            "seed": values["seed"],
            "competitors": [
                {
                    "competitor_id": str(item.competitor_id),
                    "expected_time_ms": item.expected_time_ms,
                    "upstream_index": item.upstream_index,
                    "distribution_digest": item.distribution_digest,
                    "samples_digest": item.samples_digest,
                }
                for item in rows
            ],
            "joint_samples_digest": draws.joint_samples_digest,
            "sample_matrix_digest": matrix_digest,
            "pool_receipt_digest": pool_receipt_digest,
        }
        return cls(**values, input_digest=canonical_digest(content))


@dataclass(frozen=True, slots=True)
class ObjectiveVector:
    gap_fidelity: str
    win_probability_parity: str
    expected_finish_spread_ms: str
    baseline_movement: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            canonical = canonical_decimal_string(getattr(self, name))
            if Decimal(canonical) < 0:
                raise ContractError("optimizer objectives must be nonnegative")
            object.__setattr__(self, name, canonical)

    def values(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        return tuple(Decimal(getattr(self, name)) for name in self.__dataclass_fields__)  # type: ignore[return-value]

    @_frozen_optimizer_decimal
    def dominates(self, other: ObjectiveVector) -> bool:
        if not isinstance(other, ObjectiveVector):
            raise ContractError("Pareto comparison requires typed objective vectors")
        left = self.values()
        right = other.values()
        return all(a <= b + PARETO_TOLERANCE for a, b in zip(left, right, strict=True)) and any(
            a < b - PARETO_TOLERANCE for a, b in zip(left, right, strict=True)
        )

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ObjectiveVector:
        expected = {
            "gap_fidelity",
            "win_probability_parity",
            "expected_finish_spread_ms",
            "baseline_movement",
        }
        if set(value) != expected:
            raise ContractError("optimizer objective fields differ")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class FrontierCandidate:
    marks: tuple[int, ...]
    objectives: ObjectiveVector
    normalized_objectives: tuple[str, str, str, str]
    knee_distance: str

    def __post_init__(self) -> None:
        if not isinstance(self.marks, tuple) or any(
            isinstance(mark, bool) or not isinstance(mark, int) for mark in self.marks
        ):
            raise ContractError("frontier marks must be immutable integers")
        if not isinstance(self.objectives, ObjectiveVector):
            raise ContractError("frontier objectives must be typed")
        if len(self.normalized_objectives) != 4:
            raise ContractError("frontier normalization must cover four objectives")
        for value in self.normalized_objectives:
            number = Decimal(canonical_decimal_string(value))
            if not 0 <= number <= 1:
                raise ContractError("normalized objective must be inside zero and one")
        canonical_decimal_string(self.knee_distance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "marks": list(self.marks),
            "objectives": self.objectives.to_dict(),
            "normalized_objectives": list(self.normalized_objectives),
            "knee_distance": self.knee_distance,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FrontierCandidate:
        if set(value) != {
            "marks",
            "objectives",
            "normalized_objectives",
            "knee_distance",
        }:
            raise ContractError("frontier candidate fields differ")
        return cls(
            tuple(value["marks"]),
            ObjectiveVector.from_dict(value["objectives"]),
            tuple(value["normalized_objectives"]),
            value["knee_distance"],
        )


@dataclass(frozen=True, slots=True)
class OptimizerWorkBudget:
    sample_count: int
    small_field_radius_seconds: int
    beam_width: int
    expansion_round_limit: int
    expansion_rounds: int
    candidates_generated: int
    candidates_evaluated: int
    candidate_limit: int
    parallel_workers: int = 8

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError("optimizer work budget values must be nonnegative integers")
        if (
            self.sample_count != 4096
            or self.small_field_radius_seconds != 3
            or self.beam_width != 512
        ):
            raise ContractError("optimizer work budget differs from frozen policy")
        if self.parallel_workers != 8:
            raise ContractError("optimizer parallel work budget differs from frozen policy")
        if self.expansion_rounds > self.expansion_round_limit:
            raise ContractError("optimizer expansion rounds exceed the work budget")
        if self.candidates_evaluated > self.candidate_limit:
            raise ContractError("optimizer candidate evaluations exceed the work budget")

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OptimizerWorkBudget:
        if set(value) != set(cls.__dataclass_fields__):
            raise ContractError("optimizer work budget fields differ")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class OptimizerReceipt:
    field_id: StableIdentifier
    input_digest: str
    policy_digest: str
    source_receipt_digest: str
    joint_samples_digest: str
    competitor_ids: tuple[StableIdentifier, ...]
    expected_times_ms: tuple[int, ...]
    floor: int
    ceiling: int
    optimizer_version: str
    dependency_version: str
    implementation_artifact_digest: str
    continuous_ideal: tuple[str, ...]
    rounded_baseline: tuple[int, ...]
    frontier: tuple[FrontierCandidate, ...]
    selected_marks: tuple[int, ...]
    selected_objectives: ObjectiveVector
    baseline_objectives: ObjectiveVector
    deltas: tuple[int, ...]
    fairness_gain: str
    spread_change_ms: str
    gap_fidelity_cost: str
    seed: int
    sample_count: int
    search_strategy: str
    work_budget: OptimizerWorkBudget
    frontier_digest: str
    fallback_reason: OptimizerFallback | None
    receipt_digest: str

    @_frozen_optimizer_decimal
    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        for value, label in (
            (self.input_digest, "input_digest"),
            (self.policy_digest, "policy_digest"),
            (self.source_receipt_digest, "source_receipt_digest"),
            (self.joint_samples_digest, "joint_samples_digest"),
            (self.implementation_artifact_digest, "implementation_artifact_digest"),
            (self.frontier_digest, "frontier_digest"),
            (self.receipt_digest, "receipt_digest"),
        ):
            _digest(value, label)
        if not isinstance(self.frontier, tuple) or not all(
            isinstance(item, FrontierCandidate) for item in self.frontier
        ):
            raise ContractError("optimizer frontier must be immutable typed candidates")
        if self.frontier_digest != canonical_digest([item.to_dict() for item in self.frontier]):
            raise ContractError("optimizer frontier digest mismatch")
        size = len(self.continuous_ideal)
        if (
            not isinstance(self.competitor_ids, tuple)
            or len(self.competitor_ids) != size
            or len(set(self.competitor_ids)) != size
        ):
            raise ContractError("optimizer competitor roster differs")
        for competitor_id in self.competitor_ids:
            require_identifier(competitor_id, expected_namespace="competitor")
        if len(self.expected_times_ms) != size:
            raise ContractError("optimizer expected-time basis differs from roster")
        _bounds(self.floor, self.ceiling)
        if self.optimizer_version != OPTIMIZER_VERSION or not self.dependency_version.startswith(
            "numpy:"
        ):
            raise ContractError("optimizer receipt algorithm/dependency differs")
        expected_ideal, expected_baseline = canonical_rounded_sheet(
            self.expected_times_ms, floor=self.floor, ceiling=self.ceiling
        )
        if self.continuous_ideal != expected_ideal or self.rounded_baseline != expected_baseline:
            raise ContractError("optimizer continuous ideal or canonical baseline differs")
        if not all(
            len(value) == size
            for value in (self.rounded_baseline, self.selected_marks, self.deltas)
        ):
            raise ContractError("optimizer receipt field arrays differ")
        if self.deltas != tuple(
            selected - baseline
            for selected, baseline in zip(self.selected_marks, self.rounded_baseline, strict=True)
        ):
            raise ContractError("optimizer deltas differ from selected and baseline sheets")
        if self.frontier and self.selected_marks not in tuple(item.marks for item in self.frontier):
            raise ContractError("selected sheet is absent from the frontier")
        if self.selected_marks and not _is_legal(
            self.expected_times_ms, self.selected_marks, self.floor, self.ceiling
        ):
            raise ContractError("selected optimizer sheet is illegal")
        if any(
            not _is_legal(self.expected_times_ms, item.marks, self.floor, self.ceiling)
            for item in self.frontier
        ):
            raise ContractError("optimizer frontier contains an illegal sheet")
        if not isinstance(self.selected_objectives, ObjectiveVector) or not isinstance(
            self.baseline_objectives, ObjectiveVector
        ):
            raise ContractError("optimizer receipt objectives must be typed")
        for value in (
            self.fairness_gain,
            self.spread_change_ms,
            self.gap_fidelity_cost,
        ):
            canonical_decimal_string(value)
        if self.gap_fidelity_cost != self.selected_objectives.gap_fidelity:
            raise ContractError("optimizer gap-fidelity consequence differs")
        if self.fairness_gain != canonical_decimal_string(
            Decimal(self.baseline_objectives.win_probability_parity)
            - Decimal(self.selected_objectives.win_probability_parity)
        ) or self.spread_change_ms != canonical_decimal_string(
            Decimal(self.selected_objectives.expected_finish_spread_ms)
            - Decimal(self.baseline_objectives.expected_finish_spread_ms)
        ):
            raise ContractError("optimizer consequence deltas differ from objectives")
        if self.sample_count != 4096 or self.seed < 0:
            raise ContractError("optimizer sampling authority differs")
        if not isinstance(self.work_budget, OptimizerWorkBudget):
            raise ContractError("optimizer work budget must be typed")
        if self.fallback_reason is not None and not isinstance(
            self.fallback_reason, OptimizerFallback
        ):
            raise ContractError("optimizer fallback uses an unknown reason")
        if self.fallback_reason is None:
            if not self.frontier or self.search_strategy not in {
                "single_competitor",
                "exhaustive_radius_v1",
                "deterministic_beam_v1",
            }:
                raise ContractError("successful optimizer receipt lacks its frontier/strategy")
        elif self.fallback_reason is OptimizerFallback.EMPTY_FIELD:
            if (
                size
                or self.selected_marks
                or self.frontier
                or self.search_strategy != "empty_field"
            ):
                raise ContractError("empty-field fallback is inconsistent")
        else:
            if (
                self.selected_marks != self.rounded_baseline
                or self.search_strategy != "canonical_fallback"
            ):
                raise ContractError("optimizer fallback must return the canonical baseline")
            if self.fallback_reason is OptimizerFallback.NO_VALID_IMPROVEMENT:
                if not self.frontier:
                    raise ContractError("no-improvement fallback requires the evaluated frontier")
            elif self.frontier or self.work_budget.candidates_evaluated:
                raise ContractError("failure fallback cannot claim a frontier or completed work")
        if self.receipt_digest != canonical_digest(self.content_value()):
            raise ContractError("optimizer receipt digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-optimizer-receipt-v1",
            "field_id": str(self.field_id),
            "input_digest": self.input_digest,
            "policy_digest": self.policy_digest,
            "source_receipt_digest": self.source_receipt_digest,
            "joint_samples_digest": self.joint_samples_digest,
            "competitor_ids": [str(item) for item in self.competitor_ids],
            "expected_times_ms": list(self.expected_times_ms),
            "floor": self.floor,
            "ceiling": self.ceiling,
            "optimizer_version": self.optimizer_version,
            "dependency_version": self.dependency_version,
            "implementation_artifact_digest": self.implementation_artifact_digest,
            "continuous_ideal": list(self.continuous_ideal),
            "rounded_baseline": list(self.rounded_baseline),
            "frontier": [item.to_dict() for item in self.frontier],
            "selected_marks": list(self.selected_marks),
            "selected_objectives": self.selected_objectives.to_dict(),
            "baseline_objectives": self.baseline_objectives.to_dict(),
            "deltas": list(self.deltas),
            "fairness_gain": self.fairness_gain,
            "spread_change_ms": self.spread_change_ms,
            "gap_fidelity_cost": self.gap_fidelity_cost,
            "seed": self.seed,
            "sample_count": self.sample_count,
            "search_strategy": self.search_strategy,
            "work_budget": self.work_budget.to_dict(),
            "frontier_digest": self.frontier_digest,
            "fallback_reason": (
                None if self.fallback_reason is None else self.fallback_reason.value
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "receipt_digest": self.receipt_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OptimizerReceipt:
        expected = {
            "schema_version",
            "field_id",
            "input_digest",
            "policy_digest",
            "source_receipt_digest",
            "joint_samples_digest",
            "competitor_ids",
            "expected_times_ms",
            "floor",
            "ceiling",
            "optimizer_version",
            "dependency_version",
            "implementation_artifact_digest",
            "continuous_ideal",
            "rounded_baseline",
            "frontier",
            "selected_marks",
            "selected_objectives",
            "baseline_objectives",
            "deltas",
            "fairness_gain",
            "spread_change_ms",
            "gap_fidelity_cost",
            "seed",
            "sample_count",
            "search_strategy",
            "work_budget",
            "frontier_digest",
            "fallback_reason",
            "receipt_digest",
        }
        if (
            set(value) != expected
            or value["schema_version"] != "strathmark-v3-optimizer-receipt-v1"
        ):
            raise ContractError("optimizer receipt fields or schema differ")
        fallback = value["fallback_reason"]
        return cls(
            require_identifier(value["field_id"], expected_namespace="field"),
            value["input_digest"],
            value["policy_digest"],
            value["source_receipt_digest"],
            value["joint_samples_digest"],
            tuple(
                require_identifier(item, expected_namespace="competitor")
                for item in value["competitor_ids"]
            ),
            tuple(value["expected_times_ms"]),
            value["floor"],
            value["ceiling"],
            value["optimizer_version"],
            value["dependency_version"],
            value["implementation_artifact_digest"],
            tuple(value["continuous_ideal"]),
            tuple(value["rounded_baseline"]),
            tuple(FrontierCandidate.from_dict(item) for item in value["frontier"]),
            tuple(value["selected_marks"]),
            ObjectiveVector.from_dict(value["selected_objectives"]),
            ObjectiveVector.from_dict(value["baseline_objectives"]),
            tuple(value["deltas"]),
            value["fairness_gain"],
            value["spread_change_ms"],
            value["gap_fidelity_cost"],
            value["seed"],
            value["sample_count"],
            value["search_strategy"],
            OptimizerWorkBudget.from_dict(value["work_budget"]),
            value["frontier_digest"],
            None if fallback is None else OptimizerFallback(fallback),
            value["receipt_digest"],
        )


@dataclass(frozen=True, slots=True)
class VerifiedOptimizerReceipt:
    receipt: OptimizerReceipt
    field: OptimizationField
    verification_digest: str

    @_frozen_optimizer_decimal
    def __post_init__(self) -> None:
        if not isinstance(self.receipt, OptimizerReceipt) or not isinstance(
            self.field, OptimizationField
        ):
            raise ContractError("optimizer verification requires typed receipt and field")
        if self.receipt.implementation_artifact_digest != OPTIMIZER_IMPLEMENTATION_DIGEST:
            raise ContractError("installed optimizer artifact differs from recorded receipt")
        replay = optimize_field(
            self.field,
            floor=self.receipt.floor,
            ceiling=self.receipt.ceiling,
            policy=DEFAULT_OPTIMIZER_POLICY,
        )
        if replay != self.receipt:
            raise ContractError("optimizer receipt differs from deterministic input-bound replay")
        expected = canonical_digest(
            {
                "schema_version": "strathmark-v3-optimizer-verification-v1",
                "receipt_digest": self.receipt.receipt_digest,
                "input_digest": self.field.input_digest,
                "implementation_artifact_digest": self.receipt.implementation_artifact_digest,
            }
        )
        if self.verification_digest != expected:
            raise ContractError("optimizer verification digest mismatch")

    def to_authority_dict(self) -> dict[str, Any]:
        """Serialize the complete replay input for content-addressed storage."""

        sample_matrix = np.asarray(
            [item.samples_ms for item in self.field.competitors], dtype="<i4"
        )
        return {
            "schema_version": "strathmark-v3-verified-optimizer-authority-v2",
            "receipt": self.receipt.to_dict(),
            "field": {
                **self.field.content_value(),
                "competitors": [
                    {
                        "competitor_id": str(item.competitor_id),
                        "expected_time_ms": item.expected_time_ms,
                        "upstream_index": item.upstream_index,
                        "distribution_digest": item.distribution_digest,
                    }
                    for item in self.field.competitors
                ],
                "sample_matrix_encoding": "int32-le-base64-v1",
                "sample_matrix_i32_le": base64.b64encode(sample_matrix.tobytes(order="C")).decode(
                    "ascii"
                ),
                "input_digest": self.field.input_digest,
            },
            "verification_digest": self.verification_digest,
        }

    @classmethod
    def _from_generated(
        cls,
        receipt: OptimizerReceipt,
        field: OptimizationField,
        verification_digest: str,
        *,
        _capability: object,
    ) -> VerifiedOptimizerReceipt:
        """Seal output produced in this call without rerunning the optimizer."""

        if _capability is not _GENERATED_OPTIMIZER_CAPABILITY:
            raise ContractError("generated optimizer proof is not verifier-owned")
        if receipt.implementation_artifact_digest != OPTIMIZER_IMPLEMENTATION_DIGEST:
            raise ContractError("installed optimizer artifact differs from generated receipt")
        expected = _optimizer_verification_digest(receipt, field)
        if verification_digest != expected:
            raise ContractError("optimizer verification digest mismatch")
        instance = object.__new__(cls)
        object.__setattr__(instance, "receipt", receipt)
        object.__setattr__(instance, "field", field)
        object.__setattr__(instance, "verification_digest", verification_digest)
        return instance

    @classmethod
    def from_authority_dict(cls, value: Mapping[str, Any]) -> VerifiedOptimizerReceipt:
        expected = {
            "schema_version",
            "receipt",
            "field",
            "verification_digest",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value.get("schema_version")
            not in {
                "strathmark-v3-verified-optimizer-authority-v1",
                "strathmark-v3-verified-optimizer-authority-v2",
            }
            or not isinstance(value.get("receipt"), Mapping)
            or not isinstance(value.get("field"), Mapping)
        ):
            raise ContractError("verified optimizer authority fields or schema differ")
        field_value = value["field"]
        authority_schema = value["schema_version"]
        expected_field = {
            "schema_version",
            "field_id",
            "source_receipt_digest",
            "seed",
            "competitors",
            "joint_samples_digest",
            "sample_matrix_digest",
            "pool_receipt_digest",
            "input_digest",
        }
        if authority_schema == "strathmark-v3-verified-optimizer-authority-v2":
            expected_field |= {
                "sample_matrix_encoding",
                "sample_matrix_i32_le",
            }
        competitors = field_value.get("competitors")
        if (
            set(field_value) != expected_field
            or field_value.get("schema_version") != "strathmark-v3-optimizer-input-v1"
            or not isinstance(competitors, list)
        ):
            raise ContractError("optimizer authority input fields or schema differ")
        if len(competitors) > MAX_OPTIMIZER_ENTRANTS:
            raise ContractError("optimizer field supports at most 12 entrants")
        typed_competitors = []
        expected_competitor = {
            "competitor_id",
            "expected_time_ms",
            "upstream_index",
            "distribution_digest",
        }
        if authority_schema == "strathmark-v3-verified-optimizer-authority-v1":
            expected_competitor.add("samples_ms")
        if any(
            not isinstance(item, Mapping) or set(item) != expected_competitor
            for item in competitors
        ):
            raise ContractError("optimizer authority competitor fields differ")
        if authority_schema == "strathmark-v3-verified-optimizer-authority-v1":
            sample_rows: list[tuple[int, ...]] = []
            for item in competitors:
                samples = item["samples_ms"]
                if (
                    not isinstance(samples, list)
                    or len(samples) != DEFAULT_OPTIMIZER_POLICY.sample_count
                    or any(
                        isinstance(sample, bool)
                        or not isinstance(sample, int)
                        or sample <= 0
                        or sample > 2_000_000_000
                        for sample in samples
                    )
                ):
                    raise ContractError("optimizer authority competitor fields differ")
                sample_rows.append(tuple(samples))
        else:
            if field_value.get("sample_matrix_encoding") != "int32-le-base64-v1" or not isinstance(
                field_value.get("sample_matrix_i32_le"), str
            ):
                raise ContractError("optimizer authority sample matrix differs")
            try:
                packed = base64.b64decode(field_value["sample_matrix_i32_le"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ContractError("optimizer authority sample matrix differs") from exc
            expected_bytes = len(competitors) * DEFAULT_OPTIMIZER_POLICY.sample_count * 4
            if len(packed) != expected_bytes:
                raise ContractError("optimizer authority sample matrix differs")
            decoded = np.frombuffer(packed, dtype="<i4").reshape(
                (len(competitors), DEFAULT_OPTIMIZER_POLICY.sample_count)
            )
            if np.any(decoded <= 0) or np.any(decoded > 2_000_000_000):
                raise ContractError("optimizer authority sample matrix differs")
            sample_rows = [tuple(int(value) for value in row) for row in decoded]
        for item, samples_ms in zip(competitors, sample_rows, strict=True):
            typed_competitors.append(
                OptimizationCompetitor(
                    require_identifier(item["competitor_id"], expected_namespace="competitor"),
                    item["expected_time_ms"],
                    samples_ms,
                    item["upstream_index"],
                    item["distribution_digest"],
                )
            )
        field = OptimizationField(
            require_identifier(field_value["field_id"], expected_namespace="field"),
            field_value["source_receipt_digest"],
            field_value["seed"],
            tuple(typed_competitors),
            field_value["joint_samples_digest"],
            field_value["sample_matrix_digest"],
            field_value["pool_receipt_digest"],
            field_value["input_digest"],
        )
        return cls(
            OptimizerReceipt.from_dict(value["receipt"]),
            field,
            value["verification_digest"],
        )


@_frozen_optimizer_decimal
def verify_optimizer_receipt(
    *, receipt: OptimizerReceipt, field: OptimizationField
) -> VerifiedOptimizerReceipt:
    if not isinstance(receipt, OptimizerReceipt) or not isinstance(field, OptimizationField):
        raise ContractError("optimizer verification requires typed receipt and field")
    digest = _optimizer_verification_digest(receipt, field)
    return VerifiedOptimizerReceipt(receipt, field, digest)


@_frozen_optimizer_decimal
def optimize_and_verify_field(
    field: OptimizationField,
    *,
    floor: int = 3,
    ceiling: int,
    policy: OptimizerPolicy = DEFAULT_OPTIMIZER_POLICY,
) -> VerifiedOptimizerReceipt:
    """Optimize once and seal the exact generated receipt for this typed input."""

    if not isinstance(field, OptimizationField):
        raise ContractError("optimizer generation requires a typed field")
    receipt = optimize_field(field, floor=floor, ceiling=ceiling, policy=policy)
    digest = _optimizer_verification_digest(receipt, field)
    return VerifiedOptimizerReceipt._from_generated(
        receipt,
        field,
        digest,
        _capability=_GENERATED_OPTIMIZER_CAPABILITY,
    )


def _optimizer_verification_digest(receipt: OptimizerReceipt, field: OptimizationField) -> str:
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-optimizer-verification-v1",
            "receipt_digest": receipt.receipt_digest,
            "input_digest": field.input_digest,
            "implementation_artifact_digest": receipt.implementation_artifact_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class ConsequenceReplayBinding:
    field_receipt_digest: str
    field_id: StableIdentifier
    dependence_artifact_digest: str
    pool_receipt_digest: str
    optimizer_source_receipt_digest: str
    optimizer_seed: int
    common_random_map_digest: str
    issued_joint_samples_digest: str
    issued_optimizer_receipt_digest: str
    slots: tuple[tuple[StableIdentifier, str, int], ...]
    binding_digest: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.field_receipt_digest, "field_receipt_digest"),
            (self.dependence_artifact_digest, "dependence_artifact_digest"),
            (self.pool_receipt_digest, "pool_receipt_digest"),
            (self.optimizer_source_receipt_digest, "optimizer_source_receipt_digest"),
            (self.common_random_map_digest, "common_random_map_digest"),
            (self.issued_joint_samples_digest, "issued_joint_samples_digest"),
            (self.issued_optimizer_receipt_digest, "issued_optimizer_receipt_digest"),
            (self.binding_digest, "binding_digest"),
        ):
            _digest(value, label)
        require_identifier(self.field_id, expected_namespace="field")
        if (
            isinstance(self.optimizer_seed, bool)
            or not isinstance(self.optimizer_seed, int)
            or self.optimizer_seed < 0
        ):
            raise ContractError("consequence replay seed must be a nonnegative integer")
        if not self.slots:
            raise ContractError("consequence replay binding requires a complete field")
        for competitor_id, draw_slot, crn_index in self.slots:
            require_identifier(competitor_id, expected_namespace="competitor")
            if not isinstance(draw_slot, str) or not draw_slot:
                raise ContractError("consequence replay draw slot is required")
            if isinstance(crn_index, bool) or not isinstance(crn_index, int) or crn_index < 0:
                raise ContractError("consequence replay crn index is invalid")
        if tuple(item[2] for item in self.slots) != tuple(range(len(self.slots))):
            raise ContractError("consequence replay slots must be in exact CRN order")
        if len({item[0] for item in self.slots}) != len(self.slots):
            raise ContractError("consequence replay competitor identities must be unique")
        if self.binding_digest != canonical_digest(self.content_value()):
            raise ContractError("consequence replay binding digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-consequence-replay-binding-v1",
            "field_receipt_digest": self.field_receipt_digest,
            "field_id": str(self.field_id),
            "dependence_artifact_digest": self.dependence_artifact_digest,
            "pool_receipt_digest": self.pool_receipt_digest,
            "optimizer_source_receipt_digest": self.optimizer_source_receipt_digest,
            "optimizer_seed": self.optimizer_seed,
            "common_random_map_digest": self.common_random_map_digest,
            "issued_joint_samples_digest": self.issued_joint_samples_digest,
            "issued_optimizer_receipt_digest": self.issued_optimizer_receipt_digest,
            "slots": [(str(item), slot, index) for item, slot, index in self.slots],
        }

    @classmethod
    def create(cls, **arguments: Any) -> ConsequenceReplayBinding:
        content = {
            "schema_version": "strathmark-v3-consequence-replay-binding-v1",
            "field_receipt_digest": arguments["field_receipt_digest"],
            "field_id": str(arguments["field_id"]),
            "dependence_artifact_digest": arguments["dependence_artifact_digest"],
            "pool_receipt_digest": arguments["pool_receipt_digest"],
            "optimizer_source_receipt_digest": arguments["optimizer_source_receipt_digest"],
            "optimizer_seed": arguments["optimizer_seed"],
            "common_random_map_digest": arguments["common_random_map_digest"],
            "issued_joint_samples_digest": arguments["issued_joint_samples_digest"],
            "issued_optimizer_receipt_digest": arguments["issued_optimizer_receipt_digest"],
            "slots": [(str(item), slot, index) for item, slot, index in arguments["slots"]],
        }
        return cls(**arguments, binding_digest=canonical_digest(content))


@_frozen_optimizer_decimal
def canonical_rounded_sheet(
    expected_times_ms: Sequence[int], *, floor: int, ceiling: int
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    _bounds(floor, ceiling)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in expected_times_ms
    ):
        raise ContractError("expected times must be positive integer milliseconds")
    if not expected_times_ms:
        return (), ()
    slowest = max(expected_times_ms)
    ideal = tuple(
        canonical_decimal_string(Decimal(floor) + Decimal(slowest - value) / Decimal(1000))
        for value in expected_times_ms
    )
    baseline = tuple(
        min(ceiling, max(floor, floor + _round_half_even_seconds(slowest - value)))
        for value in expected_times_ms
    )
    return ideal, baseline


@_frozen_optimizer_decimal
def evaluate_sheet(
    field: OptimizationField,
    marks: tuple[int, ...],
    baseline: tuple[int, ...],
    *,
    floor: int = 3,
) -> ObjectiveVector:
    if not isinstance(field, OptimizationField):
        raise ContractError("optimizer evaluation requires a typed field")
    if not isinstance(marks, tuple) or any(
        isinstance(mark, bool) or not isinstance(mark, int) for mark in marks
    ):
        raise ContractError("optimizer mark sheet is not legal")
    if len(marks) != len(field.competitors):
        raise ContractError("optimizer mark sheet is not legal")
    if not isinstance(baseline, tuple) or len(baseline) != len(marks):
        raise ContractError("baseline must cover the complete optimizer field")
    if any(isinstance(mark, bool) or not isinstance(mark, int) for mark in baseline):
        raise ContractError("baseline must equal the canonical rounded sheet")
    effective_ceiling = max((*marks, *baseline), default=floor)
    _bounds(floor, effective_ceiling)
    _require_sheet(field, marks, floor=floor, ceiling=effective_ceiling)
    expected = tuple(item.expected_time_ms for item in field.competitors)
    _ideal, canonical_baseline = canonical_rounded_sheet(
        expected, floor=floor, ceiling=effective_ceiling
    )
    if baseline != canonical_baseline:
        raise ContractError("baseline must equal the canonical rounded sheet")
    return _evaluate_one(field, marks, baseline, floor)


@_frozen_optimizer_decimal
def optimize_field(
    field: OptimizationField,
    *,
    ceiling: int,
    floor: int = 3,
    policy: OptimizerPolicy = DEFAULT_OPTIMIZER_POLICY,
) -> OptimizerReceipt:
    if not isinstance(field, OptimizationField) or not isinstance(policy, OptimizerPolicy):
        raise ContractError("optimizer requires typed field and frozen policy")
    _bounds(floor, ceiling)
    expected = tuple(item.expected_time_ms for item in field.competitors)
    ideal, baseline = canonical_rounded_sheet(expected, floor=floor, ceiling=ceiling)
    zero = ObjectiveVector("0", "0", "0", "0")
    if not field.competitors:
        budget = OptimizerWorkBudget(4096, 3, 512, 0, 0, 0, 0, 0)
        return _receipt(
            field,
            policy,
            ceiling,
            ideal,
            baseline,
            (),
            (),
            zero,
            zero,
            "empty_field",
            budget,
            OptimizerFallback.EMPTY_FIELD,
        )
    baseline_objectives = _evaluate_one(field, baseline, baseline, floor)
    if len(field.competitors) == 1:
        candidate = FrontierCandidate(baseline, baseline_objectives, ("0", "0", "0", "0"), "0")
        budget = OptimizerWorkBudget(4096, 3, 512, 0, 0, 1, 1, 1)
        return _receipt(
            field,
            policy,
            ceiling,
            ideal,
            baseline,
            (candidate,),
            baseline,
            baseline_objectives,
            baseline_objectives,
            "single_competitor",
            budget,
            None,
        )
    try:
        if len(field.competitors) <= policy.small_field_maximum:
            marks = tuple(_small_candidates(expected, baseline, floor, ceiling, policy))
            evaluated_raw = _evaluate_candidates(field, marks, baseline, floor, raw=True)
            rounds = 0
            strategy = "exhaustive_radius_v1"
            candidate_limit = len(marks)
            generated = len(marks)
            frontier_pairs = _pareto_frontier_raw(
                evaluated_raw,
                len(field.competitors),
                DEFAULT_OPTIMIZER_POLICY.sample_count,
            )
        else:
            (
                evaluated_raw,
                generated,
                rounds,
                candidate_limit,
                frontier_pairs,
            ) = _beam_search(field, expected, baseline, floor, ceiling, policy)
            strategy = "deterministic_beam_v1"
        if not frontier_pairs:
            raise _OptimizerAbort(OptimizerFallback.EMPTY_FRONTIER)
        selected_marks, candidates = _select_chim(frontier_pairs, baseline, baseline_objectives)
        if selected_marks is None:
            budget = OptimizerWorkBudget(
                4096,
                3,
                512,
                (min(8 * len(field.competitors), 128) if len(field.competitors) > 6 else 0),
                rounds,
                generated,
                len(evaluated_raw),
                candidate_limit,
            )
            return _receipt(
                field,
                policy,
                ceiling,
                ideal,
                baseline,
                candidates,
                baseline,
                baseline_objectives,
                baseline_objectives,
                "canonical_fallback",
                budget,
                OptimizerFallback.NO_VALID_IMPROVEMENT,
            )
        by_marks = {item.marks: item for item in candidates}
        selected_objectives = by_marks[selected_marks].objectives
        budget = OptimizerWorkBudget(
            4096,
            3,
            512,
            min(8 * len(field.competitors), 128) if len(field.competitors) > 6 else 0,
            rounds,
            generated,
            len(evaluated_raw),
            candidate_limit,
        )
        return _receipt(
            field,
            policy,
            ceiling,
            ideal,
            baseline,
            candidates,
            selected_marks,
            selected_objectives,
            baseline_objectives,
            strategy,
            budget,
            None,
        )
    except _OptimizerAbort as exc:
        reason = exc.reason
    except Exception:
        reason = OptimizerFallback.OPTIMIZER_FAILURE
    budget = OptimizerWorkBudget(4096, 3, 512, 0, 0, 0, 0, 0)
    return _receipt(
        field,
        policy,
        ceiling,
        ideal,
        baseline,
        (),
        baseline,
        baseline_objectives,
        baseline_objectives,
        "canonical_fallback",
        budget,
        reason,
    )


class SharedOptimizerConsequenceEvaluator:
    """Installed U12 consequence port backed by the exact U14 optimizer."""

    evaluator_port = "shared_optimizer_evaluator_v1"
    implementation_digest = OPTIMIZER_IMPLEMENTATION_DIGEST

    def __init__(
        self,
        *,
        bundle_digest: str,
        installed_dependence_artifact: Any,
        replay_bindings: Mapping[str, ConsequenceReplayBinding],
        ceiling: int = 183,
    ) -> None:
        from strathmark.v3.domain.joint_dependence import DependenceArtifact

        _digest(bundle_digest, "bundle_digest")
        _bounds(3, ceiling)
        if not isinstance(installed_dependence_artifact, DependenceArtifact):
            raise ContractError("shared evaluator requires an installed U13 dependence artifact")
        if not isinstance(replay_bindings, Mapping) or not replay_bindings:
            raise ContractError("shared evaluator requires exact U15 field replay bindings")
        for digest, binding in replay_bindings.items():
            if (
                not isinstance(binding, ConsequenceReplayBinding)
                or digest != binding.field_receipt_digest
                or binding.dependence_artifact_digest
                != installed_dependence_artifact.artifact_digest
            ):
                raise ContractError("consequence replay binding differs from installed artifact")
        self.bundle_digest = bundle_digest
        self.ceiling = ceiling
        self.installed_dependence_artifact = installed_dependence_artifact
        self.replay_bindings = dict(replay_bindings)
        self.implementation_digest = canonical_digest(
            {
                "implementation_artifact_digest": OPTIMIZER_IMPLEMENTATION_DIGEST,
                "dependence_artifact_digest": installed_dependence_artifact.artifact_digest,
                "replay_binding_digests": sorted(
                    item.binding_digest for item in replay_bindings.values()
                ),
            }
        )

    def evaluate(
        self, *, forecast: AssessorForecast, scoring_input: OptimizerScoringInput
    ) -> OptimizerConsequenceReceipt:
        from strathmark.v3.application.credibility_reactions import (
            OptimizerScoringInput,
        )

        if not isinstance(forecast, AssessorForecast) or not isinstance(
            scoring_input, OptimizerScoringInput
        ):
            raise ContractError("consequence evaluation requires sealed forecast and scoring input")
        if scoring_input.optimizer_bundle_digest != self.bundle_digest:
            raise ContractError("consequence input differs from installed optimizer bundle")
        if forecast.distribution is None:
            return self._pending(forecast, scoring_input)
        distributions = _matching_field_distributions(forecast, scoring_input)
        if distributions is None:
            return self._pending(forecast, scoring_input)
        from strathmark.v3.domain.joint_dependence import (
            FieldCompetitorForecast,
            bind_field_dependence,
            generate_joint_draws,
        )

        binding = self.replay_bindings.get(scoring_input.field_receipt_digest)
        if binding is None:
            return self._pending(forecast, scoring_input)
        if str(binding.field_id) != scoring_input.field_id:
            raise ContractError("consequence replay awaits exact field optimizer binding")
        if set(str(item[0]) for item in binding.slots) != set(scoring_input.issued_field_members):
            raise ContractError("consequence replay roster differs from issued field")
        actual_by_id = {
            item.competitor_id: item.raw_time_ms
            for item in scoring_input.field_results
            if item.raw_time_ms is not None
        }
        if len(actual_by_id) != len(scoring_input.field_results) or set(actual_by_id) != set(
            scoring_input.issued_field_members
        ):
            return self._pending(forecast, scoring_input)
        competitors = []
        seed = binding.optimizer_seed
        for competitor_id, draw_slot, crn_index in binding.slots:
            distribution = distributions[str(competitor_id)]
            competitors.append(
                FieldCompetitorForecast(
                    competitor_id,
                    draw_slot,
                    distribution,
                    crn_index,
                )
            )
        basis = tuple(competitors)
        artifact = self.installed_dependence_artifact
        if artifact.target_context != scoring_input.context:
            raise ContractError("consequence context differs from installed dependence artifact")
        model = bind_field_dependence(
            artifact,
            artifact.target_context,
            field_id=require_identifier(scoring_input.field_id, expected_namespace="field"),
        )
        draws = generate_joint_draws(
            basis,
            model,
            installed_artifact=artifact,
            seed=seed,
            draw_count=4096,
        )
        if draws.common_random_map_digest != binding.common_random_map_digest:
            raise ContractError(
                "consequence replay common-random map differs from issued optimizer"
            )
        field = OptimizationField.from_joint_draws(
            draws,
            forecasts=basis,
            source_receipt_digest=binding.optimizer_source_receipt_digest,
            pool_receipt_digest=binding.pool_receipt_digest,
        )
        optimized = optimize_field(field, ceiling=self.ceiling)
        actual = tuple(actual_by_id[str(item[0])] for item in binding.slots)
        probabilities = _win_probabilities(field, optimized.selected_marks)
        metrics = consequence_metrics(
            expected_times_ms=tuple(item.expected_time_ms for item in field.competitors),
            actual_times_ms=actual,
            marks=optimized.selected_marks,
            baseline_marks=optimized.rounded_baseline,
            win_probabilities=probabilities,
        )
        return OptimizerConsequenceReceipt.create(
            forecast_digest=forecast.commit_digest,
            result_revision_digest=scoring_input.result_revision_digest,
            field_receipt_digest=scoring_input.field_receipt_digest,
            scoring_input_digest=scoring_input.scoring_input_digest,
            optimizer_bundle_digest=self.bundle_digest,
            metrics=metrics,
        )

    def _pending(
        self, forecast: AssessorForecast, scoring_input: OptimizerScoringInput
    ) -> OptimizerConsequenceReceipt:
        return OptimizerConsequenceReceipt.pending(
            forecast_digest=forecast.commit_digest,
            result_revision_digest=scoring_input.result_revision_digest,
            field_receipt_digest=scoring_input.field_receipt_digest,
            scoring_input_digest=scoring_input.scoring_input_digest,
            optimizer_bundle_digest=self.bundle_digest,
        )


@_frozen_optimizer_decimal
def consequence_metrics(
    *,
    expected_times_ms: tuple[int, ...],
    actual_times_ms: tuple[int, ...],
    marks: tuple[int, ...],
    baseline_marks: tuple[int, ...],
    win_probabilities: tuple[str, ...],
) -> HandicapConsequenceMetrics:
    size = len(expected_times_ms)
    if size == 0 or not all(
        len(value) == size for value in (actual_times_ms, marks, baseline_marks, win_probabilities)
    ):
        raise ContractError("consequence metrics require one complete nonempty field")
    completions = tuple(raw + mark * 1000 for raw, mark in zip(actual_times_ms, marks, strict=True))
    spread = max(completions) - min(completions)
    residuals = tuple(
        actual - expected
        for actual, expected in zip(actual_times_ms, expected_times_ms, strict=True)
    )
    bias = _round_fraction_half_even(sum(residuals), size)
    expected_gaps = tuple(expected_times_ms[0] - value for value in expected_times_ms[1:])
    actual_gaps = tuple(actual_times_ms[0] - value for value in actual_times_ms[1:])
    gap_error = (
        0
        if not expected_gaps
        else _round_fraction_half_even(
            sum(
                abs(actual - expected)
                for actual, expected in zip(actual_gaps, expected_gaps, strict=True)
            ),
            len(expected_gaps),
        )
    )
    equal = Decimal(1) / Decimal(size)
    distortion = max(abs(Decimal(value) - equal) for value in win_probabilities)
    breakouts = sum(
        actual <= expected - max(1000, expected // 10)
        for actual, expected in zip(actual_times_ms, expected_times_ms, strict=True)
    )
    exposure = Decimal(breakouts) / Decimal(size)
    return HandicapConsequenceMetrics(
        spread,
        canonical_decimal_string(distortion),
        bias,
        gap_error,
        canonical_decimal_string(exposure),
        marks != baseline_marks,
    )


def _matching_field_distributions(
    forecast: AssessorForecast, scoring_input: OptimizerScoringInput
) -> dict[str, Any] | None:
    target_cards = tuple(
        card
        for card in scoring_input.field_forecasts
        if card.competitor_id == scoring_input.competitor_id
        and card.forecast.commit_digest == forecast.commit_digest
    )
    if len(target_cards) != 1:
        return None
    target_member_id = target_cards[0].member_id
    if forecast.assessor is AssessorKind.LLM_MEMBER and target_member_id is None:
        return None
    selected: dict[str, Any] = {}
    for card in scoring_input.field_forecasts:
        same_assessor = card.forecast.assessor is forecast.assessor
        same_lineage = card.member_id == target_member_id
        if same_assessor and same_lineage and card.forecast.distribution is not None:
            current = selected.get(card.competitor_id)
            if current is not None and current.digest != card.forecast.distribution.digest:
                return None
            selected[card.competitor_id] = card.forecast.distribution
    selected[scoring_input.competitor_id] = forecast.distribution
    if set(selected) != set(scoring_input.issued_field_members):
        return None
    return selected


def _small_candidates(
    expected: tuple[int, ...],
    baseline: tuple[int, ...],
    floor: int,
    ceiling: int,
    policy: OptimizerPolicy,
) -> Iterable[tuple[int, ...]]:
    ranges = tuple(
        range(
            max(floor, mark - policy.small_field_radius_seconds),
            min(ceiling, mark + policy.small_field_radius_seconds) + 1,
        )
        for mark in baseline
    )
    ordered = tuple(sorted(range(len(expected)), key=lambda index: (-expected[index], index)))
    tied = tuple(
        (left, right)
        for left in range(len(expected))
        for right in range(left + 1, len(expected))
        if expected[left] == expected[right]
    )
    for marks in product(*ranges):
        ordered_marks = tuple(marks[index] for index in ordered)
        if (
            min(marks) == floor
            and ordered_marks == tuple(sorted(ordered_marks))
            and all(marks[left] == marks[right] for left, right in tied)
        ):
            yield marks


def _beam_search(
    field: OptimizationField,
    expected: tuple[int, ...],
    baseline: tuple[int, ...],
    floor: int,
    ceiling: int,
    policy: OptimizerPolicy,
) -> tuple[
    dict[tuple[int, ...], _RawObjective],
    int,
    int,
    int,
    tuple[tuple[tuple[int, ...], ObjectiveVector], ...],
]:
    round_limit = min(8 * len(expected), policy.maximum_expansion_rounds)
    candidate_limit = 1 + round_limit * policy.beam_width
    evaluation_context = _compile_evaluation_context(field, baseline)
    evaluated = _evaluate_candidates_impl(
        field,
        (baseline,),
        baseline,
        floor,
        parallel=False,
        raw=True,
        _context=evaluation_context,
    )
    frontier_index = _GlobalRawParetoIndex(len(expected), len(field.competitors[0].samples_ms))
    frontier_index.add(evaluated)
    beam = (baseline,)
    generated = 1
    completed = 0
    ordered_indices = tuple(
        sorted(range(len(expected)), key=lambda index: (-expected[index], index))
    )
    tied_pairs = tuple(
        (left, right)
        for left in range(len(expected))
        for right in range(left + 1, len(expected))
        if expected[left] == expected[right]
    )

    def legal_sheet(marks: tuple[int, ...]) -> bool:
        if min(marks) != floor or any(mark < floor or mark > ceiling for mark in marks):
            return False
        ordered_marks = tuple(marks[index] for index in ordered_indices)
        return all(
            ordered_marks[index] <= ordered_marks[index + 1]
            for index in range(len(ordered_marks) - 1)
        ) and all(marks[left] == marks[right] for left, right in tied_pairs)

    for round_number in range(round_limit):
        neighbors = set()
        for marks in beam:
            for index in range(len(marks)):
                for change in (-1, 1):
                    trial = (*marks[:index], marks[index] + change, *marks[index + 1 :])
                    if trial not in evaluated and legal_sheet(trial):
                        neighbors.add(trial)
        if not neighbors:
            break
        ordered = tuple(sorted(neighbors))
        remaining = candidate_limit - len(evaluated)
        if remaining <= 0:
            break
        ordered = ordered[:remaining]
        generated += len(ordered)
        additions = _evaluate_candidates_impl(
            field,
            ordered,
            baseline,
            floor,
            raw=True,
            _context=evaluation_context,
        )
        evaluated.update(additions)
        frontier_index.add(additions)
        raw_frontier = frontier_index.raw_frontier()
        beam = _normalized_beam_raw(
            raw_frontier,
            baseline,
            policy.beam_width,
            entrant_count=len(expected),
            draw_count=len(field.competitors[0].samples_ms),
        )
        completed = round_number + 1
    final_raw_frontier = frontier_index.raw_frontier()
    return (
        evaluated,
        generated,
        completed,
        candidate_limit,
        _materialize_pareto_frontier_raw(
            dict(final_raw_frontier), len(expected), len(field.competitors[0].samples_ms)
        ),
    )


def _normalized_beam(
    frontier: tuple[tuple[tuple[int, ...], ObjectiveVector], ...],
    baseline: tuple[int, ...],
    width: int,
) -> tuple[tuple[int, ...], ...]:
    matrix = tuple(objective.values() for _marks, objective in frontier)
    ideal = tuple(min(row[column] for row in matrix) for column in range(4))
    nadir = tuple(max(row[column] for row in matrix) for column in range(4))
    normalized = tuple(
        tuple(
            (
                Decimal(0)
                if nadir[column] == ideal[column]
                else (row[column] - ideal[column]) / (nadir[column] - ideal[column])
            )
            for column in range(4)
        )
        for row in matrix
    )
    anchors: list[int] = []
    for column in range(4):
        minimum = min(row[column] for row in normalized)
        anchor = min(
            (index for index, row in enumerate(normalized) if row[column] == minimum),
            key=lambda index: _tie_key(normalized[index], frontier[index][0], baseline),
        )
        if anchor not in anchors:
            anchors.append(anchor)
    ranked = sorted(
        (index for index in range(len(frontier)) if index not in anchors),
        key=lambda index: _tie_key(normalized[index], frontier[index][0], baseline),
    )
    return tuple(frontier[index][0] for index in (anchors + ranked)[:width])


def _normalized_beam_raw(
    frontier: tuple[tuple[tuple[int, ...], _RawObjective], ...],
    baseline: tuple[int, ...],
    width: int,
    *,
    entrant_count: int,
    draw_count: int,
) -> tuple[tuple[int, ...], ...]:
    del entrant_count, draw_count
    minima = tuple(min(values[column] for _marks, values in frontier) for column in range(4))
    maxima = tuple(max(values[column] for _marks, values in frontier) for column in range(4))
    denominators = tuple(maximum - minimum for minimum, maximum in zip(minima, maxima, strict=True))
    shared_denominator = 1
    for denominator in denominators:
        if denominator:
            shared_denominator *= denominator
    scales = tuple(
        0 if denominator == 0 else shared_denominator // denominator for denominator in denominators
    )
    matrix = tuple(
        tuple((value - minima[column]) * scales[column] for column, value in enumerate(values))
        for _marks, values in frontier
    )

    def tie_key(index: int) -> tuple[int, int, int, tuple[int, ...]]:
        row = matrix[index]
        marks = frontier[index][0]
        return (
            max(row),
            sum(row),
            sum(abs(mark - original) for mark, original in zip(marks, baseline, strict=True)),
            marks,
        )

    anchors: list[int] = []
    for column in range(4):
        minimum = min(row[column] for row in matrix)
        anchor = min(
            (index for index, row in enumerate(matrix) if row[column] == minimum),
            key=tie_key,
        )
        if anchor not in anchors:
            anchors.append(anchor)
    ranked = sorted(
        (index for index in range(len(frontier)) if index not in anchors),
        key=tie_key,
    )
    return tuple(frontier[index][0] for index in (anchors + ranked)[:width])


def _evaluate_candidates(
    field: OptimizationField,
    candidates: Sequence[tuple[int, ...]],
    baseline: tuple[int, ...],
    floor: int,
    *,
    raw: bool = False,
) -> dict[tuple[int, ...], ObjectiveVector] | dict[tuple[int, ...], _RawObjective]:
    return _evaluate_candidates_impl(field, candidates, baseline, floor, raw=raw)


def _evaluate_candidates_impl(
    field: OptimizationField,
    candidates: Sequence[tuple[int, ...]],
    baseline: tuple[int, ...],
    floor: int,
    *,
    parallel: bool = True,
    raw: bool = False,
    _context: _EvaluationContext | None = None,
) -> dict[tuple[int, ...], ObjectiveVector] | dict[tuple[int, ...], _RawObjective]:
    if not candidates:
        return {}
    context = _compile_evaluation_context(field, baseline) if _context is None else _context
    if parallel and len(candidates) > 2048 and context.native_slot.kernel is None:
        workers = 8
        chunk_size = (len(candidates) + workers - 1) // workers
        chunks = tuple(
            candidates[offset : offset + chunk_size]
            for offset in range(0, len(candidates), chunk_size)
        )
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v3-optimizer") as pool:
            parts = tuple(
                pool.map(
                    lambda chunk: _evaluate_candidates_impl(
                        field,
                        chunk,
                        baseline,
                        floor,
                        parallel=False,
                        raw=raw,
                        _context=context,
                    ),
                    chunks,
                )
            )
        return {marks: objective for part in parts for marks, objective in part.items()}
    expected = context.expected
    samples = context.samples
    result: dict[tuple[int, ...], ObjectiveVector] | dict[tuple[int, ...], _RawObjective] = {}
    # Keep each vectorized working set bounded while amortizing NumPy dispatch
    # over enough candidates for a full twelve-entrant field.
    batch_size = (
        len(candidates) if context.native_slot.kernel is not None else _EVALUATION_BATCH_SIZE
    )
    entrant_count = context.entrant_count
    credit_scale = context.credit_scale
    draw_count = context.draw_count
    for offset in range(0, len(candidates), batch_size):
        batch = tuple(candidates[offset : offset + batch_size])
        mark_array = np.asarray(batch, dtype=np.int32)
        delays = (mark_array - floor) * 1000
        if len(batch) < 32:
            finishes = samples[np.newaxis, :, :] + delays[:, np.newaxis, :]
            minima = np.min(finishes, axis=2)
            credit = _winner_credits_vectorized(
                finishes,
                minima,
                entrant_count=entrant_count,
                credit_scale=credit_scale,
            )
            spreads = np.sum(np.max(finishes, axis=2) - minima, axis=1, dtype=np.int64)
        else:
            native_context = context.native_slot.get(samples)
            if native_context is not None:
                spreads, credit = native_context.evaluate(delays, credit_scale=credit_scale)
            else:
                _minima, spreads = _streamed_finish_extremes(samples, delays)
                credit = _winner_credits_bitset(
                    samples,
                    delays,
                    entrant_count=entrant_count,
                    draw_count=draw_count,
                    credit_scale=credit_scale,
                    comparison_masks=context.winner_comparison_masks,
                )
        ideal_delays = context.ideal_delays
        # The public time contract permits values through two billion ms.  A
        # twelve-entrant sum of squared ideal-delay errors can therefore exceed
        # signed int64 even though its mark-dependent terms cannot.  Expand
        # (delay - ideal)^2 algebraically: keep the constant ideal-square sum as
        # a Python integer, then add the bounded vectorized variable term.
        delay_values = delays.astype(np.int64)
        variable_gaps = np.sum(
            delay_values * delay_values - 2 * delay_values * ideal_delays[np.newaxis, :],
            axis=1,
            dtype=np.int64,
        )
        gaps = tuple(context.ideal_square_sum + int(value) for value in variable_gaps)
        movements = np.sum(
            np.abs(mark_array.astype(np.int64) - context.baseline),
            axis=1,
            dtype=np.int64,
        )
        parity_numerators = np.max(
            np.abs(credit * entrant_count - credit_scale * draw_count),
            axis=1,
        )
        for index, marks in enumerate(batch):
            values = (
                gaps[index],
                int(parity_numerators[index]),
                int(spreads[index]),
                int(movements[index]),
            )
            result[marks] = (
                values
                if raw
                else _materialize_raw_objective(
                    values, entrant_count, context.parity_denominator, draw_count
                )
            )
    return result


def _streamed_finish_extremes(samples: Any, delays: Any) -> tuple[Any, Any]:
    """Compute exact finish minima and spread without a 3-D finish tensor."""

    minimum = samples[:, 0][np.newaxis, :] + delays[:, 0, np.newaxis]
    maximum = minimum.copy()
    values = np.empty_like(minimum)
    for entrant in range(1, samples.shape[1]):
        np.add(
            samples[:, entrant][np.newaxis, :],
            delays[:, entrant, np.newaxis],
            out=values,
        )
        np.minimum(minimum, values, out=minimum)
        np.maximum(maximum, values, out=maximum)
    spreads = np.sum(maximum - minimum, axis=1, dtype=np.int64)
    return minimum, spreads


def _winner_credits_vectorized(
    finishes: Any,
    minima: Any,
    *,
    entrant_count: int,
    credit_scale: int,
) -> Any:
    ties = finishes == minima[:, :, np.newaxis]
    tie_sizes = np.sum(ties, axis=2, dtype=np.int16)
    if np.all(tie_sizes == 1):
        winners = np.argmax(ties, axis=2)
        offsets = winners + np.arange(len(finishes), dtype=np.int64)[:, np.newaxis] * entrant_count
        return (
            np.bincount(offsets.ravel(), minlength=len(finishes) * entrant_count).reshape(
                len(finishes), entrant_count
            )
            * credit_scale
        )
    weights = credit_scale // tie_sizes
    return np.sum(ties * weights[:, :, np.newaxis], axis=1, dtype=np.int64)


def _winner_credits_bitset(
    samples: Any,
    delay_rows: Any,
    *,
    entrant_count: int,
    draw_count: int,
    credit_scale: int,
    comparison_masks: (_WinnerComparisonCache | dict[tuple[int, int, int], int] | None) = None,
) -> Any:
    """Evaluate exact shared-winner credit with context-persistent bitsets."""

    full_mask = (1 << draw_count) - 1
    comparison_masks = (
        _WinnerComparisonCache.create(entrant_count)
        if comparison_masks is None
        else comparison_masks
    )

    result = np.zeros((len(delay_rows), entrant_count), dtype=np.int64)
    for candidate_index, delays in enumerate(delay_rows):
        winner_masks = []
        for left in range(entrant_count):
            mask = full_mask
            for right in range(entrant_count):
                if left == right:
                    continue
                threshold = int(delays[right]) - int(delays[left])
                if (
                    isinstance(comparison_masks, _WinnerComparisonCache)
                    and threshold % 1000 == 0
                    and -180_000 <= threshold <= 180_000
                ):
                    dense = comparison_masks.dense[left][right]
                    dense_index = threshold // 1000 + 180
                    comparison_mask = dense[dense_index]
                    if comparison_mask is None:
                        comparison = samples[:, left] - samples[:, right] <= threshold
                        packed = np.packbits(comparison, bitorder="little")
                        comparison_mask = int.from_bytes(packed.tobytes(), "little") & full_mask
                        dense[dense_index] = comparison_mask
                else:
                    comparison_mask = _draw_comparison_mask(
                        samples,
                        left,
                        right,
                        threshold,
                        full_mask=full_mask,
                        comparison_masks=comparison_masks,
                    )
                mask &= comparison_mask
                if not mask:
                    break
            winner_masks.append(mask)

        prefix = [0]
        for mask in winner_masks:
            prefix.append(prefix[-1] | mask)
        suffix = [0] * (entrant_count + 1)
        for entrant in range(entrant_count - 1, -1, -1):
            suffix[entrant] = suffix[entrant + 1] | winner_masks[entrant]
        unique_union = 0
        for winner, mask in enumerate(winner_masks):
            others = prefix[winner] | suffix[winner + 1]
            unique = mask & ~others & full_mask
            result[candidate_index, winner] = unique.bit_count() * credit_scale
            unique_union |= unique

        tied = full_mask & ~unique_union
        if tied.bit_count() > 32:
            tied_credits = _credits_from_winner_masks(
                tuple(mask & tied for mask in winner_masks),
                draw_count=draw_count,
                credit_scale=credit_scale,
            )
            result[candidate_index] += tied_credits
        else:
            while tied:
                draw = tied & -tied
                winners = [entrant for entrant, mask in enumerate(winner_masks) if mask & draw]
                if not winners:
                    raise ContractError("optimizer bitset winner authority is incomplete")
                weight = credit_scale // len(winners)
                for winner in winners:
                    result[candidate_index, winner] += weight
                tied ^= draw
    return result


def _draw_comparison_mask(
    samples: Any,
    left: int,
    right: int,
    threshold: int,
    *,
    full_mask: int,
    comparison_masks: _WinnerComparisonCache | dict[tuple[int, int, int], int],
) -> int:
    if (
        isinstance(comparison_masks, _WinnerComparisonCache)
        and threshold % 1000 == 0
        and -180_000 <= threshold <= 180_000
    ):
        dense = comparison_masks.dense[left][right]
        index = threshold // 1000 + 180
        cached = dense[index]
        if cached is not None:
            return cached
        comparison = samples[:, left] - samples[:, right] <= threshold
        packed = np.packbits(comparison, bitorder="little")
        value = int.from_bytes(packed.tobytes(), "little") & full_mask
        dense[index] = value
        return value
    target = (
        comparison_masks.fallback
        if isinstance(comparison_masks, _WinnerComparisonCache)
        else comparison_masks
    )
    key = (left, right, threshold)
    cached = target.get(key)
    if cached is not None:
        return cached
    comparison = samples[:, left] - samples[:, right] <= threshold
    packed = np.packbits(comparison, bitorder="little")
    value = int.from_bytes(packed.tobytes(), "little") & full_mask
    target[key] = value
    return value


def _credits_from_winner_masks(
    winner_masks: tuple[int, ...], *, draw_count: int, credit_scale: int
) -> list[int]:
    """Split exact LCM credit by winner multiplicity using bit-sliced counts."""

    full_mask = (1 << draw_count) - 1
    exact_counts = [full_mask] + [0] * len(winner_masks)
    for winner_mask in winner_masks:
        inverse = ~winner_mask & full_mask
        next_counts = [0] * len(exact_counts)
        for count in range(len(exact_counts)):
            next_counts[count] |= exact_counts[count] & inverse
            if count + 1 < len(exact_counts):
                next_counts[count + 1] |= exact_counts[count] & winner_mask
        exact_counts = next_counts
    return [
        sum(
            (winner_mask & exact_counts[count]).bit_count() * (credit_scale // count)
            for count in range(1, len(exact_counts))
        )
        for winner_mask in winner_masks
    ]


def _compile_evaluation_context(
    field: OptimizationField, baseline: tuple[int, ...]
) -> _EvaluationContext:
    expected = np.asarray([item.expected_time_ms for item in field.competitors], dtype=np.int64)
    samples = np.asarray([item.samples_ms for item in field.competitors], dtype=np.int32).T
    baseline_array = np.asarray(baseline, dtype=np.int64)
    ideal_delays = int(np.max(expected)) - expected
    for array in (expected, samples, baseline_array, ideal_delays):
        array.setflags(write=False)
    entrant_count = len(expected)
    credit_scale = lcm(*range(1, entrant_count + 1))
    draw_count = samples.shape[0]
    return _EvaluationContext(
        expected,
        samples,
        baseline_array,
        ideal_delays,
        sum(int(value) ** 2 for value in ideal_delays),
        entrant_count,
        credit_scale,
        draw_count,
        credit_scale * draw_count * entrant_count,
        _WinnerComparisonCache.create(entrant_count),
        _NativeEvaluationSlot(_NATIVE_OPTIMIZER_KERNEL),
    )


def _evaluate_one(
    field: OptimizationField,
    marks: tuple[int, ...],
    baseline: tuple[int, ...],
    floor: int,
) -> ObjectiveVector:
    result = _evaluate_candidates_impl(field, (marks,), baseline, floor)
    return result[marks]  # type: ignore[return-value]


def _materialize_raw_objective(
    values: _RawObjective,
    entrant_count: int,
    parity_denominator: int,
    draw_count: int,
) -> ObjectiveVector:
    return ObjectiveVector(
        _decimal_ratio(values[0], entrant_count),
        _decimal_ratio(values[1], parity_denominator),
        _decimal_ratio(values[2], draw_count),
        _decimal_ratio(values[3], entrant_count),
    )


class _GlobalRawParetoIndex:
    """Exact global nondominance index that compares each admitted pair once."""

    def __init__(self, entrant_count: int, draw_count: int) -> None:
        self._entrant_count = entrant_count
        self._draw_count = draw_count
        credit_scale = lcm(*range(1, entrant_count + 1))
        denominators = np.asarray(
            (
                entrant_count,
                credit_scale * draw_count * entrant_count,
                draw_count,
                entrant_count,
            ),
            dtype=np.int64,
        )
        self._nonstrict = denominators // 1_000_000_000
        self._strict = (-denominators - 1) // 1_000_000_000
        self._items: list[tuple[tuple[int, ...], _RawObjective]] = []
        self._dominated: list[bool] = []
        self._gap_origin: int | None = None
        self._vectors = np.empty((0, 4), dtype=np.int64)

    def add(self, additions: Mapping[tuple[int, ...], _RawObjective]) -> None:
        if not additions:
            return
        prior_marks = {marks for marks, _values in self._items}
        if prior_marks.intersection(additions):
            raise ContractError("Pareto index additions must be new candidates")
        new_items = list(additions.items())
        if self._gap_origin is None:
            self._gap_origin = new_items[0][1][0]
        prior_vectors = self._vectors
        new_vectors = _offset_raw_vectors(
            [values for _marks, values in new_items], self._gap_origin
        )
        prior_dominated = np.asarray(self._dominated, dtype=np.bool_)
        new_dominated = np.zeros(len(new_items), dtype=np.bool_)
        _mark_raw_targets_dominated(
            prior_vectors,
            new_vectors,
            new_dominated,
            self._nonstrict,
            self._strict,
        )
        _mark_raw_targets_dominated(
            new_vectors,
            prior_vectors,
            prior_dominated,
            self._nonstrict,
            self._strict,
        )
        _mark_raw_targets_dominated(
            new_vectors,
            new_vectors,
            new_dominated,
            self._nonstrict,
            self._strict,
        )
        self._items.extend(new_items)
        self._dominated = prior_dominated.tolist() + new_dominated.tolist()
        self._vectors = np.concatenate((prior_vectors, new_vectors), axis=0)

    def frontier(self) -> tuple[tuple[tuple[int, ...], ObjectiveVector], ...]:
        return _materialize_pareto_frontier_raw(
            dict(self.raw_frontier()), self._entrant_count, self._draw_count
        )

    def raw_frontier(self) -> tuple[tuple[tuple[int, ...], _RawObjective], ...]:
        nondominated = {
            marks: values
            for (marks, values), is_dominated in zip(self._items, self._dominated, strict=True)
            if not is_dominated
        }
        return tuple(sorted(nondominated.items()))


def _offset_raw_vectors(values: Sequence[_RawObjective], gap_origin: int) -> np.ndarray:
    rows = []
    for item in values:
        gap_offset = item[0] - gap_origin
        if gap_offset < -(2**63) or gap_offset > 2**63 - 1:
            raise ContractError("optimizer field gap delta exceeds signed work capacity")
        rows.append((gap_offset, item[1], item[2], item[3]))
    return np.asarray(rows, dtype=np.int64).reshape((-1, 4))


def _raw_dominance_matrix(
    sources: np.ndarray,
    targets: np.ndarray,
    nonstrict: np.ndarray,
    strict: np.ndarray,
) -> np.ndarray:
    """Return exact source-dominates-target flags without a 3-D diff tensor."""

    nonstrict_relation = np.ones((len(sources), len(targets)), dtype=np.bool_)
    strict_relation = np.zeros((len(sources), len(targets)), dtype=np.bool_)
    for column in range(4):
        source = sources[:, np.newaxis, column]
        target = targets[np.newaxis, :, column]
        nonstrict_relation &= source <= target + nonstrict[column]
        strict_relation |= source <= target + strict[column]
    return nonstrict_relation & strict_relation


def _mark_raw_targets_dominated(
    sources: np.ndarray,
    targets: np.ndarray,
    target_dominated: np.ndarray,
    nonstrict: np.ndarray,
    strict: np.ndarray,
) -> None:
    if not len(sources) or not len(targets):
        return
    if _NATIVE_OPTIMIZER_KERNEL is not None:
        _NATIVE_OPTIMIZER_KERNEL.mark_dominated(
            sources,
            targets,
            target_dominated,
            nonstrict,
            strict,
        )
        return
    block_size = 256
    source_order = np.lexsort((sources[:, 3], sources[:, 2], sources[:, 1], sources[:, 0]))
    ordered_sources = sources[source_order]
    for source_offset in range(0, len(ordered_sources), block_size):
        active_targets = np.flatnonzero(~target_dominated)
        if not len(active_targets):
            return
        source = ordered_sources[source_offset : source_offset + block_size]
        for target_offset in range(0, len(active_targets), block_size):
            target_indices = active_targets[target_offset : target_offset + block_size]
            target = targets[target_indices]
            dominates = _raw_dominance_matrix(source, target, nonstrict, strict)
            target_dominated[target_indices] |= np.any(dominates, axis=0)


def _pareto_frontier_raw(
    evaluated: Mapping[tuple[int, ...], _RawObjective],
    entrant_count: int,
    draw_count: int,
) -> tuple[tuple[tuple[int, ...], ObjectiveVector], ...]:
    credit_scale = lcm(*range(1, entrant_count + 1))
    if entrant_count <= DEFAULT_OPTIMIZER_POLICY.small_field_maximum:
        return _pareto_frontier_raw_skyline(
            evaluated,
            entrant_count=entrant_count,
            draw_count=draw_count,
            denominators=(
                entrant_count,
                credit_scale * draw_count * entrant_count,
                draw_count,
                entrant_count,
            ),
        )
    denominators = np.asarray(
        (
            entrant_count,
            credit_scale * draw_count * entrant_count,
            draw_count,
            entrant_count,
        ),
        dtype=np.int64,
    )
    nonstrict = denominators // 1_000_000_000
    strict = (-denominators - 1) // 1_000_000_000
    ordered = sorted(evaluated.items(), key=lambda pair: (*pair[1], pair[0]))
    # Gap fidelity has denominator <= 12, so its 1e-9 epsilon threshold is
    # exactly zero.  Mapping each unique Python-int gap to its sorted rank
    # preserves both <= and < while keeping the NumPy dominance matrices in
    # int64 even when the exact squared-error numerator exceeds INT64_MAX.
    ranked_ordered: list[
        tuple[tuple[tuple[int, ...], _RawObjective], tuple[int, int, int, int]]
    ] = []
    previous_gap: int | None = None
    gap_rank = -1
    for item in ordered:
        values = item[1]
        if previous_gap is None or values[0] != previous_gap:
            gap_rank += 1
            previous_gap = values[0]
        ranked_ordered.append((item, (gap_rank, values[1], values[2], values[3])))

    vectors = np.asarray([vector for _item, vector in ranked_ordered], dtype=np.int64)
    dominated = np.zeros(len(ranked_ordered), dtype=np.bool_)
    block_size = 256
    for source_offset in range(0, len(ranked_ordered), block_size):
        source = vectors[source_offset : source_offset + block_size]
        for target_offset in range(0, len(ranked_ordered), block_size):
            target = vectors[target_offset : target_offset + block_size]
            source_dominates_target = _raw_dominance_matrix(source, target, nonstrict, strict)
            dominated[target_offset : target_offset + len(target)] |= np.any(
                source_dominates_target, axis=0
            )
    frontier = {
        item[0]: item[1]
        for item, is_dominated in zip(ordered, dominated.tolist(), strict=True)
        if not is_dominated
    }
    return _materialize_pareto_frontier_raw(frontier, entrant_count, draw_count)


def _pareto_frontier_raw_skyline(
    evaluated: Mapping[tuple[int, ...], _RawObjective],
    *,
    entrant_count: int,
    draw_count: int,
    denominators: tuple[int, int, int, int],
) -> tuple[tuple[tuple[int, ...], ObjectiveVector], ...]:
    """Keep exhaustive small fields linear in candidates and frontier width."""

    skyline: list[tuple[tuple[int, ...], _RawObjective]] = []
    for item in sorted(evaluated.items(), key=lambda pair: (*pair[1], pair[0])):
        if any(_dominates_raw(current[1], item[1], denominators) for current in skyline):
            continue
        skyline = [
            current for current in skyline if not _dominates_raw(item[1], current[1], denominators)
        ]
        skyline.append(item)
    return _materialize_pareto_frontier_raw(dict(skyline), entrant_count, draw_count)


def _materialize_pareto_frontier_raw(
    skyline: Mapping[tuple[int, ...], _RawObjective],
    entrant_count: int,
    draw_count: int,
) -> tuple[tuple[tuple[int, ...], ObjectiveVector], ...]:
    credit_scale = lcm(*range(1, entrant_count + 1))
    parity_denominator = credit_scale * draw_count * entrant_count
    return tuple(
        (
            marks,
            _materialize_raw_objective(values, entrant_count, parity_denominator, draw_count),
        )
        for marks, values in sorted(skyline.items())
    )


def _dominates_raw(
    left: _RawObjective,
    right: _RawObjective,
    denominators: tuple[int, int, int, int],
) -> bool:
    scale = 1_000_000_000
    differences = tuple((a - b) * scale for a, b in zip(left, right, strict=True))
    return all(
        difference <= denominator
        for difference, denominator in zip(differences, denominators, strict=True)
    ) and any(
        difference < -denominator
        for difference, denominator in zip(differences, denominators, strict=True)
    )


def _pareto_frontier(
    evaluated: Mapping[tuple[int, ...], ObjectiveVector],
) -> tuple[tuple[tuple[int, ...], ObjectiveVector], ...]:
    skyline: list[
        tuple[tuple[int, ...], ObjectiveVector, tuple[Decimal, Decimal, Decimal, Decimal]]
    ] = []
    ordered = sorted(
        ((marks, objective, objective.values()) for marks, objective in evaluated.items()),
        key=lambda item: (*item[2], item[0]),
    )
    for item in ordered:
        if any(_dominates_values(current[2], item[2]) for current in skyline):
            continue
        skyline = [current for current in skyline if not _dominates_values(item[2], current[2])]
        skyline.append(item)
    return tuple(
        (marks, objective)
        for marks, objective, _values in sorted(skyline, key=lambda item: item[0])
    )


def _dominates_values(
    left: tuple[Decimal, Decimal, Decimal, Decimal],
    right: tuple[Decimal, Decimal, Decimal, Decimal],
) -> bool:
    return all(a <= b + PARETO_TOLERANCE for a, b in zip(left, right, strict=True)) and any(
        a < b - PARETO_TOLERANCE for a, b in zip(left, right, strict=True)
    )


def _select_chim(
    frontier: tuple[tuple[tuple[int, ...], ObjectiveVector], ...],
    baseline: tuple[int, ...],
    baseline_objectives: ObjectiveVector,
) -> tuple[tuple[int, ...] | None, tuple[FrontierCandidate, ...]]:
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_HALF_EVEN
        return _select_chim_exact(frontier, baseline, baseline_objectives)


def _select_chim_exact(
    frontier: tuple[tuple[tuple[int, ...], ObjectiveVector], ...],
    baseline: tuple[int, ...],
    baseline_objectives: ObjectiveVector,
) -> tuple[tuple[int, ...] | None, tuple[FrontierCandidate, ...]]:
    matrix = tuple(objective.values() for _, objective in frontier)
    ideal = tuple(min(row[column] for row in matrix) for column in range(4))
    nadir = tuple(max(row[column] for row in matrix) for column in range(4))
    normalized = tuple(
        tuple(
            (
                Decimal(0)
                if nadir[column] == ideal[column]
                else (row[column] - ideal[column]) / (nadir[column] - ideal[column])
            )
            for column in range(4)
        )
        for row in matrix
    )
    anchors = []
    for column in range(4):
        minimum = min(row[column] for row in normalized)
        indexes = tuple(index for index, row in enumerate(normalized) if row[column] == minimum)
        anchors.append(
            min(
                indexes,
                key=lambda index: _tie_key(normalized[index], frontier[index][0], baseline),
            )
        )
    anchor_points = tuple(normalized[index] for index in anchors)
    difference = tuple(
        tuple(value - anchor_points[0][column] for column, value in enumerate(row))
        for row in anchor_points[1:]
    )
    null_basis = _decimal_svd_nullspace(difference, 4)
    if not null_basis:
        raise _OptimizerAbort(OptimizerFallback.RANK_INVALID)
    toward_utopia = tuple(-value for value in anchor_points[0])
    normal = _project_onto_basis(toward_utopia, null_basis)
    if all(value == 0 for value in normal):
        normal = null_basis[0]
    orientation = sum(value * target for value, target in zip(normal, toward_utopia, strict=True))
    if orientation < 0:
        normal = tuple(-value for value in normal)
    # Positive values are improvements from the CHIM plane toward utopia.
    knee = tuple(
        sum(normal[column] * (point[column] - anchor_points[0][column]) for column in range(4))
        for point in normalized
    )
    candidates = tuple(
        FrontierCandidate(
            marks,
            objective,
            tuple(canonical_decimal_string(value) for value in normalized[index]),
            ("0" if index in anchors else canonical_decimal_string(knee[index])),
        )
        for index, (marks, objective) in enumerate(frontier)
    )
    baseline_values = baseline_objectives.values()
    meaningful = []
    for index, candidate in enumerate(candidates):
        values = candidate.objectives.values()
        better = tuple(
            a < b - PARETO_TOLERANCE for a, b in zip(values, baseline_values, strict=True)
        )
        not_worse = tuple(
            a <= b + PARETO_TOLERANCE for a, b in zip(values, baseline_values, strict=True)
        )
        better_count = sum(better)
        # A tradeoff is meaningful when at least two objectives improve, or
        # one improves without worsening every other objective. This rejects
        # baseline-dominated/no-improvement sheets and the frozen one-gain-for-
        # three-losses case without suppressing ordinary Pareto tradeoffs.
        if better_count >= 2 or (better_count == 1 and sum(not_worse) >= 2):
            meaningful.append(index)
    if not meaningful:
        return None, candidates
    selected = min(
        meaningful,
        key=lambda index: (
            -knee[index],
            *_tie_key(normalized[index], candidates[index].marks, baseline),
        ),
    )
    return candidates[selected].marks, candidates


def _tie_key(
    normalized: Sequence[Decimal], marks: tuple[int, ...], baseline: tuple[int, ...]
) -> tuple[Decimal, Decimal, int, tuple[int, ...]]:
    return (
        max(normalized),
        sum(normalized, Decimal(0)),
        sum(abs(a - b) for a, b in zip(marks, baseline, strict=True)),
        marks,
    )


def _decimal_svd_nullspace(
    rows: tuple[tuple[Decimal, ...], ...], width: int
) -> tuple[tuple[Decimal, ...], ...]:
    # Fixed-order one-sided Jacobi eigensolver for A^T A.  This is the SVD
    # fallback specialized to the frozen four-objective CHIM matrix; iteration
    # order, precision, convergence threshold, and sign convention are closed.
    gram = [
        [sum((row[left] * row[right] for row in rows), Decimal(0)) for right in range(width)]
        for left in range(width)
    ]
    vectors = [
        [Decimal(1) if row == column else Decimal(0) for column in range(width)]
        for row in range(width)
    ]
    threshold = Decimal("1e-60")
    for _sweep in range(96):
        for left in range(width - 1):
            for right in range(left + 1, width):
                off = gram[left][right]
                if abs(off) <= threshold:
                    continue
                tau = (gram[right][right] - gram[left][left]) / (Decimal(2) * off)
                sign = Decimal(1) if tau >= 0 else Decimal(-1)
                tangent = sign / (abs(tau) + (Decimal(1) + tau * tau).sqrt())
                cosine = Decimal(1) / (Decimal(1) + tangent * tangent).sqrt()
                sine = tangent * cosine
                for index in range(width):
                    old_left = gram[index][left]
                    old_right = gram[index][right]
                    gram[index][left] = cosine * old_left - sine * old_right
                    gram[index][right] = sine * old_left + cosine * old_right
                for index in range(width):
                    old_left = gram[left][index]
                    old_right = gram[right][index]
                    gram[left][index] = cosine * old_left - sine * old_right
                    gram[right][index] = sine * old_left + cosine * old_right
                for index in range(width):
                    old_left = vectors[index][left]
                    old_right = vectors[index][right]
                    vectors[index][left] = cosine * old_left - sine * old_right
                    vectors[index][right] = sine * old_left + cosine * old_right
    eigenpairs = sorted(
        ((abs(gram[index][index]), index) for index in range(width)),
        key=lambda item: (item[0], item[1]),
    )
    scale = max((value for value, _index in eigenpairs), default=Decimal(0))
    zero_tolerance = max(Decimal("1e-50"), scale * Decimal("1e-45"))
    null_indices = [index for value, index in eigenpairs if value <= zero_tolerance]
    if not null_indices:
        return ()
    basis = []
    for column in null_indices:
        vector = tuple(vectors[row][column] for row in range(width))
        first = next((value for value in vector if abs(value) > threshold), Decimal(1))
        multiplier = abs(first) / first
        vector = tuple(multiplier * value for value in vector)
        basis.append(vector)
    return tuple(basis)


def _project_onto_basis(
    vector: tuple[Decimal, ...], basis: tuple[tuple[Decimal, ...], ...]
) -> tuple[Decimal, ...]:
    # Deterministic Gram-Schmidt; Decimal arithmetic avoids BLAS/SVD drift.
    orthogonal: list[tuple[Decimal, ...]] = []
    for candidate in basis:
        current = candidate
        for existing in orthogonal:
            denominator = sum(value * value for value in existing)
            coefficient = (
                sum(left * right for left, right in zip(current, existing, strict=True))
                / denominator
            )
            current = tuple(
                left - coefficient * right for left, right in zip(current, existing, strict=True)
            )
        if any(value != 0 for value in current):
            orthogonal.append(current)
    result = [Decimal(0)] * len(vector)
    for existing in orthogonal:
        denominator = sum(value * value for value in existing)
        coefficient = (
            sum(left * right for left, right in zip(vector, existing, strict=True)) / denominator
        )
        result = [left + coefficient * right for left, right in zip(result, existing, strict=True)]
    return tuple(result)


@_frozen_optimizer_decimal
def _receipt(
    field: OptimizationField,
    policy: OptimizerPolicy,
    ceiling: int,
    ideal: tuple[str, ...],
    baseline: tuple[int, ...],
    frontier: tuple[FrontierCandidate, ...],
    selected: tuple[int, ...],
    selected_objectives: ObjectiveVector,
    baseline_objectives: ObjectiveVector,
    strategy: str,
    budget: OptimizerWorkBudget,
    fallback: OptimizerFallback | None,
) -> OptimizerReceipt:
    fairness = Decimal(baseline_objectives.win_probability_parity) - Decimal(
        selected_objectives.win_probability_parity
    )
    spread = Decimal(selected_objectives.expected_finish_spread_ms) - Decimal(
        baseline_objectives.expected_finish_spread_ms
    )
    frontier_digest = canonical_digest([item.to_dict() for item in frontier])
    values = {
        "field_id": field.field_id,
        "input_digest": field.input_digest,
        "policy_digest": policy.digest,
        "source_receipt_digest": field.source_receipt_digest,
        "joint_samples_digest": field.joint_samples_digest,
        "competitor_ids": tuple(item.competitor_id for item in field.competitors),
        "expected_times_ms": tuple(item.expected_time_ms for item in field.competitors),
        "floor": 3,
        "ceiling": ceiling,
        "optimizer_version": OPTIMIZER_VERSION,
        "dependency_version": f"numpy:{NUMPY_DEPENDENCY_VERSION}",
        "implementation_artifact_digest": OPTIMIZER_IMPLEMENTATION_DIGEST,
        "continuous_ideal": ideal,
        "rounded_baseline": baseline,
        "frontier": frontier,
        "selected_marks": selected,
        "selected_objectives": selected_objectives,
        "baseline_objectives": baseline_objectives,
        "deltas": tuple(a - b for a, b in zip(selected, baseline, strict=True)),
        "fairness_gain": canonical_decimal_string(fairness),
        "spread_change_ms": canonical_decimal_string(spread),
        "gap_fidelity_cost": selected_objectives.gap_fidelity,
        "seed": field.seed,
        "sample_count": 4096,
        "search_strategy": strategy,
        "work_budget": budget,
        "frontier_digest": frontier_digest,
        "fallback_reason": fallback,
    }
    content = {
        "schema_version": "strathmark-v3-optimizer-receipt-v1",
        "field_id": str(field.field_id),
        "input_digest": field.input_digest,
        "policy_digest": policy.digest,
        "source_receipt_digest": field.source_receipt_digest,
        "joint_samples_digest": field.joint_samples_digest,
        "competitor_ids": [str(item.competitor_id) for item in field.competitors],
        "expected_times_ms": [item.expected_time_ms for item in field.competitors],
        "floor": values["floor"],
        "ceiling": values["ceiling"],
        "optimizer_version": OPTIMIZER_VERSION,
        "dependency_version": f"numpy:{NUMPY_DEPENDENCY_VERSION}",
        "implementation_artifact_digest": values["implementation_artifact_digest"],
        "continuous_ideal": list(ideal),
        "rounded_baseline": list(baseline),
        "frontier": [item.to_dict() for item in frontier],
        "selected_marks": list(selected),
        "selected_objectives": selected_objectives.to_dict(),
        "baseline_objectives": baseline_objectives.to_dict(),
        "deltas": list(values["deltas"]),
        "fairness_gain": values["fairness_gain"],
        "spread_change_ms": values["spread_change_ms"],
        "gap_fidelity_cost": values["gap_fidelity_cost"],
        "seed": field.seed,
        "sample_count": 4096,
        "search_strategy": strategy,
        "work_budget": budget.to_dict(),
        "frontier_digest": frontier_digest,
        "fallback_reason": None if fallback is None else fallback.value,
    }
    return OptimizerReceipt(**values, receipt_digest=canonical_digest(content))


@_frozen_optimizer_decimal
def _win_probabilities(field: OptimizationField, marks: tuple[int, ...]) -> tuple[str, ...]:
    samples = np.asarray([item.samples_ms for item in field.competitors], dtype=np.int64).T
    finishes = samples + np.asarray(marks, dtype=np.int64) * 1000
    return _tie_split_probabilities(finishes)


def _tie_split_probabilities(finishes: np.ndarray) -> tuple[str, ...]:
    """Apportion exact split-win credit onto a fixed finite-decimal scale."""

    minima = np.min(finishes, axis=1)
    tied = finishes == minima[:, np.newaxis]
    tie_sizes = np.sum(tied, axis=1, dtype=np.int16)
    draw_count, entrant_count = finishes.shape
    credit_scale = lcm(*range(1, entrant_count + 1))
    numerators = tuple(
        sum(
            int(np.sum(tied[:, competitor] & (tie_sizes == size))) * (credit_scale // size)
            for size in range(1, entrant_count + 1)
        )
        for competitor in range(entrant_count)
    )
    denominator = draw_count * credit_scale
    decimal_scale = 10**60
    apportioned = [numerator * decimal_scale // denominator for numerator in numerators]
    remainders = tuple(numerator * decimal_scale % denominator for numerator in numerators)
    missing = decimal_scale - sum(apportioned)
    order = sorted(range(entrant_count), key=lambda index: (-remainders[index], index))
    for index in order[:missing]:
        apportioned[index] += 1
    return tuple(_fixed_scale_decimal_string(value, places=60) for value in apportioned)


def _fixed_scale_decimal_string(value: int, *, places: int) -> str:
    scale = 10**places
    whole, fractional = divmod(value, scale)
    if not fractional:
        return str(whole)
    digits = f"{fractional:0{places}d}".rstrip("0")
    return f"{whole}.{digits}"


def _is_legal(expected: tuple[int, ...], marks: tuple[int, ...], floor: int, ceiling: int) -> bool:
    if len(expected) != len(marks) or not marks or min(marks) != floor:
        return False
    if any(mark < floor or mark > ceiling for mark in marks):
        return False
    ordered = sorted(range(len(expected)), key=lambda index: (-expected[index], index))
    ordered_marks = tuple(marks[index] for index in ordered)
    if ordered_marks != tuple(sorted(ordered_marks)):
        return False
    return all(
        expected[left] != expected[right] or marks[left] == marks[right]
        for left in range(len(expected))
        for right in range(left + 1, len(expected))
    )


def _require_sheet(
    field: OptimizationField, marks: tuple[int, ...], *, floor: int, ceiling: int
) -> None:
    if not isinstance(marks, tuple) or not _is_legal(
        tuple(item.expected_time_ms for item in field.competitors),
        marks,
        floor,
        ceiling,
    ):
        raise ContractError("optimizer mark sheet is not legal")


def _bounds(floor: int, ceiling: int) -> None:
    if (
        isinstance(floor, bool)
        or not isinstance(floor, int)
        or isinstance(ceiling, bool)
        or not isinstance(ceiling, int)
        or floor != 3
        or ceiling < floor
        or ceiling > 183
    ):
        raise ContractError("optimizer bounds require Mark 3 and a ceiling through 183")


def _round_half_even_seconds(milliseconds: int) -> int:
    with localcontext() as context:
        context.prec = 32
        context.rounding = ROUND_HALF_EVEN
        return int((Decimal(milliseconds) / Decimal(1000)).quantize(Decimal(1)))


def _round_fraction_half_even(numerator: int, denominator: int) -> int:
    with localcontext() as context:
        context.prec = 64
        context.rounding = ROUND_HALF_EVEN
        return int((Decimal(numerator) / Decimal(denominator)).quantize(Decimal(1)))


def _decimal_ratio(numerator: int, denominator: int) -> str:
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_HALF_EVEN
        return canonical_decimal_string(Decimal(numerator) / Decimal(denominator))


def _median_absolute(values: tuple[int, ...]) -> int:
    ordered = sorted(abs(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return _round_fraction_half_even(ordered[middle - 1] + ordered[middle], 2)


def _seed_from_digest(value: str) -> int:
    _digest(value, "receipt digest")
    return int(value[:16], 16) & ((1 << 63) - 1)


def _positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{label} must be a positive integer")


def _digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractError(f"{label} must be a lowercase SHA-256 digest")


class _OptimizerAbort(Exception):
    def __init__(self, reason: OptimizerFallback) -> None:
        self.reason = reason
        super().__init__(reason.value)


__all__ = [
    "DEFAULT_OPTIMIZER_POLICY",
    "ConsequenceReplayBinding",
    "MAX_OPTIMIZER_ENTRANTS",
    "OPTIMIZER_IMPLEMENTATION_DIGEST",
    "OPTIMIZER_VERSION",
    "FrontierCandidate",
    "ObjectiveVector",
    "OptimizationCompetitor",
    "OptimizationField",
    "OptimizerFallback",
    "OptimizerPolicy",
    "OptimizerReceipt",
    "OptimizerWorkBudget",
    "SharedOptimizerConsequenceEvaluator",
    "VerifiedOptimizerReceipt",
    "canonical_rounded_sheet",
    "consequence_metrics",
    "evaluate_sheet",
    "optimize_and_verify_field",
    "optimize_field",
    "verify_optimizer_receipt",
]
