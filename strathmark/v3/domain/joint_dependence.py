"""Causal same-field residual dependence and deterministic joint race draws."""

from __future__ import annotations

import sys
from dataclasses import InitVar, dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from hashlib import sha256
from itertools import combinations
from json import dumps
from typing import Any

from strathmark.v3.contracts.canonical import (
    canonical_bytes,
    canonical_decimal_string,
    canonical_digest,
)
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import (
    DependenceInputs,
    DependenceMode,
    DistributionSamples,
    PositiveTimeDistribution,
    SamplingSpec,
    _build_generated_sampling_spec,
    _samples_digest,
    sample_aligned_positive_distributions,
)
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.domain.credibility import ContextNode
from strathmark.v3.domain.pooling import LinearPooledDistribution, PoolResult

JOINT_SAMPLING_ALGORITHM = "sha256-u64-shared-rank-copula-v1"
JOINT_DEPENDENCY_VERSION = "stdlib-integer-v1"
MAX_JOINT_DRAW_ARTIFACT_BYTES = 16_777_216
MAX_JOINT_DRAW_ARTIFACT_ITEMS = 500_000
_RANK_UNIFORM_DENOMINATOR = 2**64 + 1
_DECIMAL_POWERS = tuple(10**power for power in range(48))
_NATIVE_RANK_UNIFORM_GENERATOR: Any | None = None
_NATIVE_RANK_UNIFORM_GENERATOR_INITIALIZED = False


@dataclass(frozen=True, slots=True)
class DependencePolicy:
    prior_strength: str = "8"
    minimum_pair_count: int = 4
    rho_floor: str = "-0.8"
    rho_cap: str = "0.8"
    version: str = "dependence:v1"

    def __post_init__(self) -> None:
        prior = _decimal(self.prior_strength, "prior_strength")
        floor = _decimal(self.rho_floor, "rho_floor")
        cap = _decimal(self.rho_cap, "rho_cap")
        if prior <= 0:
            raise ContractError("prior_strength must be positive")
        if (
            isinstance(self.minimum_pair_count, bool)
            or not isinstance(self.minimum_pair_count, int)
            or self.minimum_pair_count <= 0
        ):
            raise ContractError("minimum_pair_count must be positive")
        if not Decimal("-1") < floor < 0 < cap < Decimal("1"):
            raise ContractError("rho bounds must straddle zero inside the valid interval")
        if self.version != "dependence:v1":
            raise ContractError("dependence policy version is not supported")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "prior_strength": self.prior_strength,
                "minimum_pair_count": self.minimum_pair_count,
                "rho_floor": self.rho_floor,
                "rho_cap": self.rho_cap,
                "version": self.version,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prior_strength": self.prior_strength,
            "minimum_pair_count": self.minimum_pair_count,
            "rho_floor": self.rho_floor,
            "rho_cap": self.rho_cap,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DependencePolicy:
        if set(value) != {
            "prior_strength",
            "minimum_pair_count",
            "rho_floor",
            "rho_cap",
            "version",
        }:
            raise ContractError("dependence policy fields differ")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ResidualObservation:
    field_id: StableIdentifier
    competitor_id: StableIdentifier
    context: ContextNode
    source_sequence: int
    source_revision: int
    active_projection_digest: str
    standardized_residual: str

    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if not isinstance(self.context, ContextNode):
            raise ContractError("residual context must be typed")
        if (
            isinstance(self.source_sequence, bool)
            or not isinstance(self.source_sequence, int)
            or self.source_sequence <= 0
        ):
            raise ContractError("residual source_sequence must be positive")
        if (
            isinstance(self.source_revision, bool)
            or not isinstance(self.source_revision, int)
            or self.source_revision <= 0
        ):
            raise ContractError("residual source_revision must be positive")
        _digest(self.active_projection_digest, "active_projection_digest")
        _decimal(self.standardized_residual, "standardized_residual")


@dataclass(frozen=True, slots=True)
class DependenceArtifact:
    """Training-time dependence parameters installed as one immutable bundle artifact."""

    artifact_id: StableIdentifier
    version: str
    target_context: ContextNode
    cutoff_sequence: int
    mode: DependenceMode
    rho: str
    effective_pair_count: int
    context_pair_counts: tuple[tuple[str, int], ...]
    shrinkage_path: tuple[str, ...]
    policy: DependencePolicy
    training_evidence_digest: str
    active_projection_digest: str
    observation_set_digest: str
    promotion_receipt_digest: str
    parameters_digest: str
    fallback_code: str | None
    artifact_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.artifact_id, expected_namespace="artifact")
        if self.version != "dependence-artifact:v1":
            raise ContractError("dependence artifact version differs")
        if not isinstance(self.target_context, ContextNode):
            raise ContractError("dependence artifact context must be typed")
        if (
            isinstance(self.cutoff_sequence, bool)
            or not isinstance(self.cutoff_sequence, int)
            or self.cutoff_sequence <= 0
        ):
            raise ContractError("dependence artifact cutoff must be positive")
        if not isinstance(self.mode, DependenceMode):
            raise ContractError("dependence artifact mode must be typed")
        rho = _decimal(self.rho, "artifact rho")
        if not Decimal("-1") < rho < Decimal("1"):
            raise ContractError("dependence artifact rho must be inside the valid interval")
        if (
            isinstance(self.effective_pair_count, bool)
            or not isinstance(self.effective_pair_count, int)
            or self.effective_pair_count < 0
        ):
            raise ContractError("artifact effective pair count must be nonnegative")
        if not isinstance(self.context_pair_counts, tuple) or not isinstance(
            self.shrinkage_path, tuple
        ):
            raise ContractError("artifact hierarchy evidence must be immutable")
        if not isinstance(self.policy, DependencePolicy):
            raise ContractError("dependence artifact policy must be frozen")
        for value, label in (
            (self.training_evidence_digest, "training_evidence_digest"),
            (self.active_projection_digest, "active_projection_digest"),
            (self.observation_set_digest, "observation_set_digest"),
            (self.promotion_receipt_digest, "promotion_receipt_digest"),
            (self.parameters_digest, "parameters_digest"),
            (self.artifact_digest, "artifact_digest"),
        ):
            _digest(value, label)
        if self.mode is DependenceMode.SHARED_RANK_COPULA:
            if self.fallback_code is not None:
                raise ContractError("learned artifact cannot carry a fallback")
        elif self.mode is DependenceMode.INDEPENDENCE:
            if self.rho != "0" or not self.fallback_code:
                raise ContractError("independence artifact requires a frozen fallback")
        else:
            raise ContractError("dependence artifact mode is unsupported")
        if self.parameters_digest != canonical_digest(self.parameters_value()):
            raise ContractError("dependence artifact parameters digest mismatch")
        if self.artifact_digest != canonical_digest(self.content_value()):
            raise ContractError("dependence artifact digest mismatch")

    def parameters_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-dependence-parameters-v1",
            "target_context": self.target_context.to_dict(),
            "cutoff_sequence": self.cutoff_sequence,
            "mode": self.mode.value,
            "rho": self.rho,
            "effective_pair_count": self.effective_pair_count,
            "context_pair_counts": [list(item) for item in self.context_pair_counts],
            "shrinkage_path": list(self.shrinkage_path),
            "policy": self.policy.to_dict(),
            "training_evidence_digest": self.training_evidence_digest,
            "active_projection_digest": self.active_projection_digest,
            "observation_set_digest": self.observation_set_digest,
        }

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-dependence-artifact-v1",
            "artifact_id": str(self.artifact_id),
            "version": self.version,
            "parameters": self.parameters_value(),
            "parameters_digest": self.parameters_digest,
            "promotion_receipt_digest": self.promotion_receipt_digest,
            "fallback_code": self.fallback_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "artifact_digest": self.artifact_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DependenceArtifact:
        if (
            set(value)
            != {
                "schema_version",
                "artifact_id",
                "version",
                "parameters",
                "parameters_digest",
                "promotion_receipt_digest",
                "fallback_code",
                "artifact_digest",
            }
            or value.get("schema_version") != "strathmark-v3-dependence-artifact-v1"
        ):
            raise ContractError("dependence artifact fields or schema differ")
        parameters = value["parameters"]
        if (
            not isinstance(parameters, dict)
            or set(parameters)
            != {
                "schema_version",
                "target_context",
                "cutoff_sequence",
                "mode",
                "rho",
                "effective_pair_count",
                "context_pair_counts",
                "shrinkage_path",
                "policy",
                "training_evidence_digest",
                "active_projection_digest",
                "observation_set_digest",
            }
            or parameters.get("schema_version") != "strathmark-v3-dependence-parameters-v1"
        ):
            raise ContractError("dependence artifact parameters differ")
        context = parameters["target_context"]
        policy = parameters["policy"]
        if not isinstance(context, dict) or not isinstance(policy, dict):
            raise ContractError("dependence artifact nested values are invalid")
        try:
            mode = DependenceMode(parameters["mode"])
        except (TypeError, ValueError) as exc:
            raise ContractError("dependence artifact mode is unknown") from exc
        return cls(
            artifact_id=require_identifier(value["artifact_id"], expected_namespace="artifact"),
            version=value["version"],
            target_context=ContextNode(
                context.get("event_code"),
                context.get("size_band"),
                context.get("material_group"),
                context.get("history_depth"),
            ),
            cutoff_sequence=parameters["cutoff_sequence"],
            mode=mode,
            rho=parameters["rho"],
            effective_pair_count=parameters["effective_pair_count"],
            context_pair_counts=tuple(tuple(item) for item in parameters["context_pair_counts"]),
            shrinkage_path=tuple(parameters["shrinkage_path"]),
            policy=DependencePolicy.from_dict(policy),
            training_evidence_digest=parameters["training_evidence_digest"],
            active_projection_digest=parameters["active_projection_digest"],
            observation_set_digest=parameters["observation_set_digest"],
            promotion_receipt_digest=value["promotion_receipt_digest"],
            parameters_digest=value["parameters_digest"],
            fallback_code=value["fallback_code"],
            artifact_digest=value["artifact_digest"],
        )


@dataclass(frozen=True, slots=True)
class DependenceModel:
    field_id: StableIdentifier
    artifact_digest: str
    target_context: ContextNode
    cutoff_sequence: int
    mode: DependenceMode
    rho: str
    effective_pair_count: int
    context_pair_counts: tuple[tuple[str, int], ...]
    shrinkage_path: tuple[str, ...]
    policy_digest: str
    parameters_digest: str | None
    fallback_code: str | None
    model_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        _digest(self.artifact_digest, "artifact_digest")
        if not isinstance(self.target_context, ContextNode):
            raise ContractError("dependence target context must be typed")
        if (
            isinstance(self.cutoff_sequence, bool)
            or not isinstance(self.cutoff_sequence, int)
            or self.cutoff_sequence <= 0
        ):
            raise ContractError("dependence cutoff must be positive")
        if not isinstance(self.mode, DependenceMode):
            raise ContractError("dependence mode must be typed")
        rho = _decimal(self.rho, "rho")
        if not Decimal("-1") < rho < Decimal("1"):
            raise ContractError("dependence rho must be inside the valid interval")
        if (
            isinstance(self.effective_pair_count, bool)
            or not isinstance(self.effective_pair_count, int)
            or self.effective_pair_count < 0
        ):
            raise ContractError("effective_pair_count must be nonnegative")
        if not isinstance(self.context_pair_counts, tuple) or not isinstance(
            self.shrinkage_path, tuple
        ):
            raise ContractError("dependence hierarchy evidence must be immutable")
        _digest(self.policy_digest, "policy_digest")
        if self.mode is DependenceMode.SHARED_RANK_COPULA:
            if self.parameters_digest is None or self.fallback_code is not None:
                raise ContractError("learned dependence parameters/fallback are inconsistent")
        elif self.mode is DependenceMode.INDEPENDENCE:
            if self.parameters_digest is not None or not self.fallback_code:
                raise ContractError("independence parameters/fallback are inconsistent")
        else:
            raise ContractError("U13 dependence uses the frozen shared-rank copula")
        if self.parameters_digest is not None:
            _digest(self.parameters_digest, "parameters_digest")
        _digest(self.model_digest, "model_digest")
        if self.model_digest != canonical_digest(self.content_value()):
            raise ContractError("dependence model digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-dependence-model-v1",
            "field_id": str(self.field_id),
            "artifact_digest": self.artifact_digest,
            "target_context": self.target_context.to_dict(),
            "cutoff_sequence": self.cutoff_sequence,
            "mode": self.mode.value,
            "rho": self.rho,
            "effective_pair_count": self.effective_pair_count,
            "context_pair_counts": list(self.context_pair_counts),
            "shrinkage_path": self.shrinkage_path,
            "policy_digest": self.policy_digest,
            "parameters_digest": self.parameters_digest,
            "fallback_code": self.fallback_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_value(), "model_digest": self.model_digest}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DependenceModel:
        expected = {
            "schema_version",
            "field_id",
            "artifact_digest",
            "target_context",
            "cutoff_sequence",
            "mode",
            "rho",
            "effective_pair_count",
            "context_pair_counts",
            "shrinkage_path",
            "policy_digest",
            "parameters_digest",
            "fallback_code",
            "model_digest",
        }
        if set(value) != expected or value["schema_version"] != "strathmark-v3-dependence-model-v1":
            raise ContractError("dependence model fields or schema differ")
        context = value["target_context"]
        if not isinstance(context, dict):
            raise ContractError("dependence target context is invalid")
        try:
            mode = DependenceMode(value["mode"])
        except (TypeError, ValueError) as exc:
            raise ContractError("dependence mode is unknown") from exc
        return cls(
            field_id=require_identifier(value["field_id"], expected_namespace="field"),
            artifact_digest=value["artifact_digest"],
            target_context=ContextNode(
                context.get("event_code"),
                context.get("size_band"),
                context.get("material_group"),
                context.get("history_depth"),
            ),
            cutoff_sequence=value["cutoff_sequence"],
            mode=mode,
            rho=value["rho"],
            effective_pair_count=value["effective_pair_count"],
            context_pair_counts=tuple(tuple(item) for item in value["context_pair_counts"]),
            shrinkage_path=tuple(value["shrinkage_path"]),
            policy_digest=value["policy_digest"],
            parameters_digest=value["parameters_digest"],
            fallback_code=value["fallback_code"],
            model_digest=value["model_digest"],
        )


@dataclass(frozen=True, slots=True)
class FieldCompetitorForecast:
    competitor_id: StableIdentifier
    draw_slot: str
    distribution: PositiveTimeDistribution | LinearPooledDistribution
    crn_index: int

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if not isinstance(self.draw_slot, str) or not self.draw_slot or len(self.draw_slot) > 96:
            raise ContractError("draw_slot must be a nonempty bounded stable field slot")
        if not isinstance(self.distribution, (PositiveTimeDistribution, LinearPooledDistribution)):
            raise ContractError("joint competitor requires a sealed predictive distribution")
        if (
            isinstance(self.crn_index, bool)
            or not isinstance(self.crn_index, int)
            or self.crn_index < 0
        ):
            raise ContractError("joint competitor crn_index must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class _GeneratedJointUniformsProof:
    token: object
    slots: tuple[tuple[str, int], ...]
    uniforms: tuple[tuple[str, tuple[str, ...]], ...]
    common_random_map_digest: str


@dataclass(frozen=True, slots=True)
class GeneratedJointUniforms:
    """One field CRN map reusable across every sealed marginal transform."""

    field_id: StableIdentifier
    artifact_digest: str
    mode: DependenceMode
    rho: str
    effective_rho: str
    seed: int
    draw_count: int
    slots: tuple[tuple[str, int], ...]
    uniforms: tuple[tuple[str, tuple[str, ...]], ...]
    common_random_map_digest: str
    _generated_proof: InitVar[_GeneratedJointUniformsProof | None] = None
    _sampling_specs: tuple[tuple[str, SamplingSpec], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self, _generated_proof: _GeneratedJointUniformsProof | None) -> None:
        require_identifier(self.field_id, expected_namespace="field")
        _digest(self.artifact_digest, "joint uniform artifact_digest")
        if not isinstance(self.mode, DependenceMode):
            raise ContractError("joint uniform mode must be typed")
        rho = _decimal(self.rho, "joint uniform rho")
        effective_rho = _decimal(self.effective_rho, "joint uniform effective rho")
        SamplingSpec(seed=self.seed, draw_count=self.draw_count)
        if (
            not isinstance(self.slots, tuple)
            or not self.slots
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or len(item[0]) > 96
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
                or item[1] < 0
                for item in self.slots
            )
        ):
            raise ContractError("joint uniform slots must be immutable bounded pairs")
        slot_names = tuple(item[0] for item in self.slots)
        indices = tuple(item[1] for item in self.slots)
        if len(slot_names) != len(set(slot_names)) or indices != tuple(sorted(set(indices))):
            raise ContractError("joint uniform slots and crn indices must be unique and sorted")
        if effective_rho != _effective_rank_correlation(rho, len(self.slots)):
            raise ContractError("joint uniform effective rank correlation is misreported")
        if (
            not isinstance(self.uniforms, tuple)
            or tuple(item[0] for item in self.uniforms) != slot_names
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[1], tuple)
                or len(item[1]) != self.draw_count
                for item in self.uniforms
            )
        ):
            raise ContractError("joint uniform arrays must match every slot and draw")
        expected_map = _joint_common_map_digest(
            field_id=self.field_id,
            artifact_digest=self.artifact_digest,
            seed=self.seed,
            draw_count=self.draw_count,
            mode=self.mode,
            effective_rho=self.effective_rho,
            field_size=len(self.slots),
        )
        if self.common_random_map_digest != expected_map:
            raise ContractError("joint uniform common-random map differs")
        trusted_generation = _accepts_generated_joint_uniforms_proof(
            _generated_proof,
            self.slots,
            self.uniforms,
            self.common_random_map_digest,
        )
        if not trusted_generation:
            expected_uniforms = _joint_uniforms_from_slots(
                self.slots,
                self.seed,
                self.draw_count,
                rho,
            )
            if self.uniforms != tuple((slot, expected_uniforms[slot]) for slot in slot_names):
                raise ContractError("joint uniform arrays differ from frozen algorithm")
        object.__setattr__(
            self,
            "_sampling_specs",
            tuple(
                (
                    slot,
                    _build_generated_sampling_spec(
                        seed=self.seed,
                        draw_count=self.draw_count,
                        common_uniforms=values,
                        common_random_map_digest=self.common_random_map_digest,
                    ),
                )
                for slot, values in self.uniforms
            ),
        )

    def sampling_spec(self, draw_slot: str) -> SamplingSpec:
        for slot, spec in self._sampling_specs:
            if slot == draw_slot:
                return spec
        raise ContractError("joint uniform sampling slot is absent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-generated-joint-uniforms-v1",
            "field_id": str(self.field_id),
            "artifact_digest": self.artifact_digest,
            "mode": self.mode.value,
            "rho": self.rho,
            "effective_rho": self.effective_rho,
            "seed": self.seed,
            "draw_count": self.draw_count,
            "slots": [[slot, index] for slot, index in self.slots],
            "uniforms": [[slot, list(values)] for slot, values in self.uniforms],
            "common_random_map_digest": self.common_random_map_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GeneratedJointUniforms:
        expected = {
            "schema_version",
            "field_id",
            "artifact_digest",
            "mode",
            "rho",
            "effective_rho",
            "seed",
            "draw_count",
            "slots",
            "uniforms",
            "common_random_map_digest",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("schema_version") != "strathmark-v3-generated-joint-uniforms-v1"
            or not isinstance(value.get("slots"), list)
            or not isinstance(value.get("uniforms"), list)
        ):
            raise ContractError("generated joint uniform fields or schema differ")
        try:
            sampling = SamplingSpec(seed=value["seed"], draw_count=value["draw_count"])
            if len(value["slots"]) != len(value["uniforms"]):
                raise ContractError("generated joint uniform rows differ")
            minimum_artifact_items = 20 + len(value["slots"]) * (8 + 2 * sampling.draw_count)
            if minimum_artifact_items > MAX_JOINT_DRAW_ARTIFACT_ITEMS:
                raise ContractError("generated joint uniforms exceed the artifact item bound")
            if any(not isinstance(item, list) or len(item) != 2 for item in value["slots"]) or any(
                not isinstance(item, list) or len(item) != 2 for item in value["uniforms"]
            ):
                raise ContractError("generated joint uniform rows differ")
            if any(
                not isinstance(item[0], str)
                or not item[0]
                or len(item[0]) > 96
                or isinstance(item[1], bool)
                or not isinstance(item[1], int)
                or item[1] < 0
                for item in value["slots"]
            ):
                raise ContractError("joint uniform slots require a bounded stable field slot")
            if any(
                not isinstance(item[0], str)
                or not item[0]
                or len(item[0]) > 96
                or not isinstance(item[1], list)
                for item in value["uniforms"]
            ):
                raise ContractError("generated joint uniform rows differ")
            if any(len(item[1]) != sampling.draw_count for item in value["uniforms"]):
                raise ContractError("generated joint uniform rows differ from declared draw count")
            canonical_bytes(
                value,
                max_bytes=MAX_JOINT_DRAW_ARTIFACT_BYTES,
                max_items=MAX_JOINT_DRAW_ARTIFACT_ITEMS,
            )
            slots = tuple((item[0], item[1]) for item in value["slots"])
            uniforms = tuple((item[0], tuple(item[1])) for item in value["uniforms"])
            return cls(
                require_identifier(value["field_id"], expected_namespace="field"),
                value["artifact_digest"],
                DependenceMode(value["mode"]),
                value["rho"],
                value["effective_rho"],
                value["seed"],
                value["draw_count"],
                slots,
                uniforms,
                value["common_random_map_digest"],
            )
        except ContractError:
            raise
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ContractError("generated joint uniform rows differ") from exc


@dataclass(frozen=True, slots=True)
class _GeneratedJointCompetitorProof:
    token: object
    competitor_id: StableIdentifier
    draw_slot: str
    crn_index: int
    distribution_digest: str
    common_uniforms: tuple[str, ...]
    samples_ms: tuple[int, ...]
    samples_digest: str
    samples_authority_digest: str


@dataclass(frozen=True, slots=True)
class JointCompetitorDraws:
    competitor_id: StableIdentifier
    draw_slot: str
    crn_index: int
    distribution_digest: str
    common_uniforms: tuple[str, ...]
    samples_ms: tuple[int, ...]
    samples_digest: str
    _generated_proof: InitVar[_GeneratedJointCompetitorProof | None] = None
    _samples_authority_digest_cache: str = field(init=False, repr=False, compare=False, default="")

    def __post_init__(self, _generated_proof: _GeneratedJointCompetitorProof | None) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if not isinstance(self.draw_slot, str) or not self.draw_slot or len(self.draw_slot) > 96:
            raise ContractError("joint draw slot must be a bounded stable field slot")
        if (
            isinstance(self.crn_index, bool)
            or not isinstance(self.crn_index, int)
            or self.crn_index < 0
        ):
            raise ContractError("joint draw crn_index must be nonnegative")
        _digest(self.distribution_digest, "distribution_digest")
        _digest(self.samples_digest, "samples_digest")
        if not isinstance(self.common_uniforms, tuple) or not isinstance(self.samples_ms, tuple):
            raise ContractError("joint uniforms and samples must be immutable")
        if len(self.common_uniforms) != len(self.samples_ms) or not self.samples_ms:
            raise ContractError("joint uniforms and samples must have equal nonzero length")
        trusted_generation = _accepts_generated_competitor_proof(
            _generated_proof,
            self.competitor_id,
            self.draw_slot,
            self.crn_index,
            self.distribution_digest,
            self.common_uniforms,
            self.samples_ms,
            self.samples_digest,
        )
        if not trusted_generation:
            for value in self.common_uniforms:
                number = _decimal(value, "joint common uniform")
                if not 0 < number < 1:
                    raise ContractError("joint common uniforms must be inside the unit interval")
            if any(
                isinstance(item, bool) or not isinstance(item, int) or item <= 0
                for item in self.samples_ms
            ):
                raise ContractError("joint samples must be positive integer milliseconds")
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
            "competitor_id": str(self.competitor_id),
            "draw_slot": self.draw_slot,
            "crn_index": self.crn_index,
            "distribution_digest": self.distribution_digest,
            "common_uniforms": list(self.common_uniforms),
            "samples_ms": list(self.samples_ms),
            "samples_digest": self.samples_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> JointCompetitorDraws:
        if set(value) != {
            "competitor_id",
            "draw_slot",
            "crn_index",
            "distribution_digest",
            "common_uniforms",
            "samples_ms",
            "samples_digest",
        }:
            raise ContractError("joint competitor draw fields differ")
        if not isinstance(value["common_uniforms"], list) or not isinstance(
            value["samples_ms"], list
        ):
            raise ContractError("joint competitor draw arrays are invalid")
        return cls(
            require_identifier(value["competitor_id"], expected_namespace="competitor"),
            value["draw_slot"],
            value["crn_index"],
            value["distribution_digest"],
            tuple(value["common_uniforms"]),
            tuple(value["samples_ms"]),
            value["samples_digest"],
        )


@dataclass(frozen=True, slots=True)
class _GeneratedJointDrawsProof:
    """Non-serializable provenance for one already-derived in-process value.

    The identity binding is intentional: decoded or reconstructed values cannot
    reuse this marker and therefore take the complete deterministic replay path.
    It is not a security boundary against arbitrary code executing in-process.
    """

    token: object
    inputs: DependenceInputs
    artifact_digest: str
    rho: str
    effective_rho: str
    competitors: tuple[JointCompetitorDraws, ...]
    common_random_map_digest: str
    algorithm: str
    dependency_version: str
    time_quantum_ms: int
    joint_samples_digest: str


@dataclass(frozen=True, slots=True)
class JointDraws:
    inputs: DependenceInputs
    artifact_digest: str
    rho: str
    effective_rho: str
    competitors: tuple[JointCompetitorDraws, ...]
    common_random_map_digest: str
    algorithm: str
    dependency_version: str
    time_quantum_ms: int
    joint_samples_digest: str
    _generated_proof: InitVar[_GeneratedJointDrawsProof | None] = None
    _retained_generated_proof: _GeneratedJointDrawsProof | None = field(
        init=False,
        repr=False,
        compare=False,
        default=None,
    )

    def __post_init__(self, _generated_proof: _GeneratedJointDrawsProof | None) -> None:
        if not isinstance(self.inputs, DependenceInputs):
            raise ContractError("joint receipt inputs must be typed")
        _digest(self.artifact_digest, "artifact_digest")
        rho = _decimal(self.rho, "joint rho")
        effective_rho = _decimal(self.effective_rho, "joint effective rho")
        if (
            not isinstance(self.competitors, tuple)
            or not self.competitors
            or not all(isinstance(item, JointCompetitorDraws) for item in self.competitors)
        ):
            raise ContractError("joint receipt competitors must be immutable typed values")
        indices = tuple(item.crn_index for item in self.competitors)
        if indices != tuple(sorted(set(indices))):
            raise ContractError("joint receipt crn indices must be unique and sorted")
        if any(len(item.samples_ms) != self.inputs.draw_count for item in self.competitors):
            raise ContractError("joint receipt sample counts differ from inputs")
        _digest(self.common_random_map_digest, "common_random_map_digest")
        if (
            self.algorithm != JOINT_SAMPLING_ALGORITHM
            or self.dependency_version != JOINT_DEPENDENCY_VERSION
        ):
            raise ContractError("joint sampling algorithm or dependency differs")
        if self.time_quantum_ms != 1:
            raise ContractError("joint sampling time quantum differs")
        if effective_rho != _effective_rank_correlation(rho, len(self.competitors)):
            raise ContractError("joint effective rank correlation is misreported")
        expected_map = canonical_digest(
            {
                "schema_version": "strathmark-v3-field-crn-map-v1",
                "field_id": str(self.inputs.field_id),
                "artifact_digest": self.artifact_digest,
                "seed": self.inputs.seed,
                "draw_count": self.inputs.draw_count,
                "mode": self.inputs.mode.value,
                "effective_rho": self.effective_rho,
                "crn_ordinals": list(range(len(self.competitors))),
                "algorithm": JOINT_SAMPLING_ALGORITHM,
                "dependency_version": JOINT_DEPENDENCY_VERSION,
                "time_quantum_ms": 1,
            }
        )
        if self.common_random_map_digest != expected_map:
            raise ContractError("joint common-random map differs from receipt authority")
        _digest(self.joint_samples_digest, "joint_samples_digest")
        trusted_generation = _accepts_generated_joint_proof(
            _generated_proof,
            self.inputs,
            self.artifact_digest,
            self.rho,
            self.effective_rho,
            self.competitors,
            self.common_random_map_digest,
            self.algorithm,
            self.dependency_version,
            self.time_quantum_ms,
            self.joint_samples_digest,
        )
        object.__setattr__(
            self,
            "_retained_generated_proof",
            _generated_proof if trusted_generation else None,
        )
        if not trusted_generation:
            expected_uniforms = _joint_uniforms(
                self.competitors, self.inputs.seed, self.inputs.draw_count, rho
            )
            for item in self.competitors:
                if item.common_uniforms != expected_uniforms[item.draw_slot]:
                    raise ContractError("joint common uniforms differ from frozen algorithm")
                expected = _samples_digest(
                    samples_ms=item.samples_ms,
                    seed=self.inputs.seed,
                    distribution_digest=item.distribution_digest,
                    common_random_map_digest=self.common_random_map_digest,
                )
                if item.samples_digest != expected:
                    raise ContractError("joint competitor sample digest mismatch")
            if self.joint_samples_digest != canonical_digest(
                self.content_value(),
                max_bytes=MAX_JOINT_DRAW_ARTIFACT_BYTES,
                max_items=MAX_JOINT_DRAW_ARTIFACT_ITEMS,
            ):
                raise ContractError("joint samples receipt digest mismatch")

    def content_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-joint-draws-v1",
            "inputs": self.inputs.to_dict(),
            "artifact_digest": self.artifact_digest,
            "rho": self.rho,
            "effective_rho": self.effective_rho,
            "competitors": [item.to_dict() for item in self.competitors],
            "common_random_map_digest": self.common_random_map_digest,
            "algorithm": self.algorithm,
            "dependency_version": self.dependency_version,
            "time_quantum_ms": self.time_quantum_ms,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_value(),
            "joint_samples_digest": self.joint_samples_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> JointDraws:
        expected = {
            "schema_version",
            "inputs",
            "artifact_digest",
            "rho",
            "effective_rho",
            "competitors",
            "common_random_map_digest",
            "algorithm",
            "dependency_version",
            "time_quantum_ms",
            "joint_samples_digest",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("schema_version") != "strathmark-v3-joint-draws-v1"
        ):
            raise ContractError("joint draws fields or schema differ")
        if not isinstance(value.get("inputs"), dict) or not isinstance(
            value.get("competitors"), list
        ):
            raise ContractError("joint draws nested values are invalid")
        inputs = value["inputs"]
        sampling = SamplingSpec(seed=inputs.get("seed"), draw_count=inputs.get("draw_count"))
        minimum_artifact_items = 20 + len(value["competitors"]) * (8 + 2 * sampling.draw_count)
        if minimum_artifact_items > MAX_JOINT_DRAW_ARTIFACT_ITEMS:
            raise ContractError("joint draws exceed the artifact item bound")
        competitor_fields = {
            "competitor_id",
            "draw_slot",
            "crn_index",
            "distribution_digest",
            "common_uniforms",
            "samples_ms",
            "samples_digest",
        }
        if any(
            not isinstance(item, dict)
            or set(item) != competitor_fields
            or not isinstance(item.get("draw_slot"), str)
            or not item.get("draw_slot")
            or len(item["draw_slot"]) > 96
            or not isinstance(item.get("common_uniforms"), list)
            or not isinstance(item.get("samples_ms"), list)
            for item in value["competitors"]
        ):
            raise ContractError("joint draw rows require a bounded stable field slot and arrays")
        if any(
            len(item["common_uniforms"]) != sampling.draw_count
            or len(item["samples_ms"]) != sampling.draw_count
            for item in value["competitors"]
        ):
            raise ContractError("joint draw row arrays differ from the declared draw count")
        canonical_bytes(
            value,
            max_bytes=MAX_JOINT_DRAW_ARTIFACT_BYTES,
            max_items=MAX_JOINT_DRAW_ARTIFACT_ITEMS,
        )
        return cls(
            DependenceInputs.from_dict(inputs),
            value["artifact_digest"],
            value["rho"],
            value["effective_rho"],
            tuple(JointCompetitorDraws.from_dict(item) for item in value["competitors"]),
            value["common_random_map_digest"],
            value["algorithm"],
            value["dependency_version"],
            value["time_quantum_ms"],
            value["joint_samples_digest"],
        )


def train_dependence_artifact(
    observations: tuple[ResidualObservation, ...],
    target_context: ContextNode,
    cutoff_sequence: int,
    policy: DependencePolicy,
    *,
    artifact_id: StableIdentifier,
    training_evidence_digest: str,
    active_projection_digest: str,
    promotion_receipt_digest: str,
) -> DependenceArtifact:
    """Fit one promotable artifact using only causal training evidence."""

    if not isinstance(observations, tuple) or not all(
        isinstance(item, ResidualObservation) for item in observations
    ):
        raise ContractError("residual evidence must be an immutable typed tuple")
    if not isinstance(target_context, ContextNode) or not isinstance(policy, DependencePolicy):
        raise ContractError("dependence fit requires typed context and policy")
    require_identifier(artifact_id, expected_namespace="artifact")
    _digest(training_evidence_digest, "training_evidence_digest")
    _digest(active_projection_digest, "active_projection_digest")
    _digest(promotion_receipt_digest, "promotion_receipt_digest")
    if (
        isinstance(cutoff_sequence, bool)
        or not isinstance(cutoff_sequence, int)
        or cutoff_sequence <= 0
    ):
        raise ValueError("cutoff_sequence must be positive")
    identities = tuple((str(item.field_id), str(item.competitor_id)) for item in observations)
    if len(identities) != len(set(identities)):
        raise ValueError("residual evidence requires one active revision per field competitor")
    if any(item.active_projection_digest != active_projection_digest for item in observations):
        raise ValueError("residual evidence differs from the verified active projection")
    eligible = tuple(item for item in observations if item.source_sequence < cutoff_sequence)
    eligible = tuple(
        sorted(
            eligible,
            key=lambda item: (
                str(item.field_id),
                item.source_sequence,
                item.source_revision,
                str(item.competitor_id),
                item.context.to_tuple(),
                item.standardized_residual,
            ),
        )
    )
    chain = tuple(reversed(_context_chain(target_context)))
    current = Decimal(0)
    pair_counts: list[tuple[str, int]] = []
    path = [_decimal_string(current)]
    effective_pairs = len(_same_field_products(eligible))
    for node in chain:
        products = _same_field_products(
            tuple(item for item in eligible if node.contains(item.context))
        )
        count = len(products)
        pair_counts.append((_context_key(node), count))
        if count:
            raw = sum(products, Decimal(0)) / Decimal(count)
            strength = Decimal(policy.prior_strength)
            current = (Decimal(count) * raw + strength * current) / (Decimal(count) + strength)
            current = min(max(current, Decimal(policy.rho_floor)), Decimal(policy.rho_cap))
        path.append(_decimal_string(current))
    supported = effective_pairs >= policy.minimum_pair_count and current != 0
    mode = DependenceMode.SHARED_RANK_COPULA if supported else DependenceMode.INDEPENDENCE
    rho = _decimal_string(current if supported else Decimal(0))
    fallback = (
        None
        if supported
        else (
            "unsupported_context_independence" if not effective_pairs else "shrunk_to_independence"
        )
    )
    observation_set_digest = canonical_digest(
        [
            {
                "field_id": str(item.field_id),
                "competitor_id": str(item.competitor_id),
                "context": item.context.to_dict(),
                "source_sequence": item.source_sequence,
                "source_revision": item.source_revision,
                "active_projection_digest": item.active_projection_digest,
                "standardized_residual": item.standardized_residual,
            }
            for item in eligible
        ]
    )
    values = {
        "artifact_id": artifact_id,
        "version": "dependence-artifact:v1",
        "target_context": target_context,
        "cutoff_sequence": cutoff_sequence,
        "mode": mode,
        "rho": rho,
        "effective_pair_count": effective_pairs,
        "context_pair_counts": tuple(pair_counts),
        "shrinkage_path": tuple(path),
        "policy": policy,
        "training_evidence_digest": training_evidence_digest,
        "active_projection_digest": active_projection_digest,
        "observation_set_digest": observation_set_digest,
        "promotion_receipt_digest": promotion_receipt_digest,
        "fallback_code": fallback,
    }
    parameters_digest = canonical_digest(_dependence_artifact_parameters(values))
    with_parameters = {**values, "parameters_digest": parameters_digest}
    return DependenceArtifact(
        **with_parameters,
        artifact_digest=canonical_digest(_dependence_artifact_content(with_parameters)),
    )


def fit_field_dependence(
    observations: tuple[ResidualObservation, ...],
    target_context: ContextNode,
    cutoff_sequence: int,
    policy: DependencePolicy,
    *,
    artifact_id: StableIdentifier,
    training_evidence_digest: str,
    active_projection_digest: str,
    promotion_receipt_digest: str,
) -> DependenceArtifact:
    """Compatibility name for the training-only artifact builder."""

    return train_dependence_artifact(
        observations,
        target_context,
        cutoff_sequence,
        policy,
        artifact_id=artifact_id,
        training_evidence_digest=training_evidence_digest,
        active_projection_digest=active_projection_digest,
        promotion_receipt_digest=promotion_receipt_digest,
    )


def bind_field_dependence(
    artifact: DependenceArtifact,
    target_context: ContextNode,
    *,
    field_id: StableIdentifier,
) -> DependenceModel:
    """Bind an installed artifact to one field without accepting training evidence."""

    if not isinstance(artifact, DependenceArtifact):
        raise ContractError("field dependence requires an installed frozen artifact")
    if not isinstance(target_context, ContextNode) or target_context != artifact.target_context:
        raise ContractError("field context differs from installed dependence artifact")
    require_identifier(field_id, expected_namespace="field")
    values = {
        "field_id": field_id,
        "artifact_digest": artifact.artifact_digest,
        "target_context": target_context,
        "cutoff_sequence": artifact.cutoff_sequence,
        "mode": artifact.mode,
        "rho": artifact.rho,
        "effective_pair_count": artifact.effective_pair_count,
        "context_pair_counts": artifact.context_pair_counts,
        "shrinkage_path": artifact.shrinkage_path,
        "policy_digest": artifact.policy.digest,
        "parameters_digest": (
            artifact.parameters_digest
            if artifact.mode is DependenceMode.SHARED_RANK_COPULA
            else None
        ),
        "fallback_code": artifact.fallback_code,
    }
    return DependenceModel(
        **values,
        model_digest=canonical_digest(_dependence_model_content(values)),
    )


def _generate_joint_uniforms(
    competitors: tuple[FieldCompetitorForecast, ...],
    model: DependenceModel,
    *,
    installed_artifact: DependenceArtifact,
    seed: int,
    draw_count: int,
    _generation_token: object,
) -> GeneratedJointUniforms:
    if (
        not isinstance(competitors, tuple)
        or not competitors
        or not all(isinstance(item, FieldCompetitorForecast) for item in competitors)
    ):
        raise ContractError("joint uniforms require a nonempty immutable field")
    if not isinstance(model, DependenceModel):
        raise ContractError("joint uniforms require a frozen dependence model")
    if not isinstance(installed_artifact, DependenceArtifact):
        raise ContractError("joint uniforms require the installed dependence artifact")
    expected_model = bind_field_dependence(
        installed_artifact, model.target_context, field_id=model.field_id
    )
    if expected_model != model:
        raise ContractError("joint uniform field binding differs from installed artifact")
    SamplingSpec(seed=seed, draw_count=draw_count)
    minimum_artifact_items = 20 + len(competitors) * (8 + 2 * draw_count)
    if minimum_artifact_items > MAX_JOINT_DRAW_ARTIFACT_ITEMS:
        raise ContractError("joint uniform request exceeds the artifact item bound")
    draw_slots = tuple(item.draw_slot for item in competitors)
    identities = tuple(item.competitor_id for item in competitors)
    crn_indices = tuple(item.crn_index for item in competitors)
    if len(draw_slots) != len(set(draw_slots)):
        raise ValueError("draw_slot values must be unique")
    if len(identities) != len(set(identities)):
        raise ValueError("competitor identities must be unique")
    if len(crn_indices) != len(set(crn_indices)):
        raise ValueError("competitor crn_index values must be unique")
    ordered = tuple(sorted(competitors, key=lambda item: item.crn_index))
    slots = tuple((item.draw_slot, item.crn_index) for item in ordered)
    effective_rho = _decimal_string(_effective_rank_correlation(Decimal(model.rho), len(ordered)))
    common_map_digest = _joint_common_map_digest(
        field_id=model.field_id,
        artifact_digest=installed_artifact.artifact_digest,
        seed=seed,
        draw_count=draw_count,
        mode=model.mode,
        effective_rho=effective_rho,
        field_size=len(ordered),
    )
    generated = _joint_uniforms(ordered, seed, draw_count, Decimal(model.rho))
    uniforms = tuple((slot, generated[slot]) for slot, _index in slots)
    return GeneratedJointUniforms(
        model.field_id,
        installed_artifact.artifact_digest,
        model.mode,
        model.rho,
        effective_rho,
        seed,
        draw_count,
        slots,
        uniforms,
        common_map_digest,
        _GeneratedJointUniformsProof(
            _generation_token,
            slots,
            uniforms,
            common_map_digest,
        ),
    )


def _install_joint_uniform_generation_capability():
    token = object()

    def accepts(
        proof: _GeneratedJointUniformsProof | None,
        slots: tuple[tuple[str, int], ...],
        uniforms: tuple[tuple[str, tuple[str, ...]], ...],
        common_random_map_digest: str,
    ) -> bool:
        return (
            isinstance(proof, _GeneratedJointUniformsProof)
            and proof.token is token
            and proof.slots is slots
            and proof.uniforms is uniforms
            and proof.common_random_map_digest == common_random_map_digest
        )

    def generate(
        competitors: tuple[FieldCompetitorForecast, ...],
        model: DependenceModel,
        *,
        installed_artifact: DependenceArtifact,
        seed: int,
        draw_count: int,
    ) -> GeneratedJointUniforms:
        return _generate_joint_uniforms(
            competitors,
            model,
            installed_artifact=installed_artifact,
            seed=seed,
            draw_count=draw_count,
            _generation_token=token,
        )

    return generate, accepts


generate_joint_uniforms, _accepts_generated_joint_uniforms_proof = (
    _install_joint_uniform_generation_capability()
)
del _install_joint_uniform_generation_capability


def _generate_joint_draws(
    competitors: tuple[FieldCompetitorForecast, ...],
    model: DependenceModel,
    *,
    installed_artifact: DependenceArtifact,
    seed: int,
    draw_count: int,
    _generation_token: object,
    uniform_plan: GeneratedJointUniforms | None,
    pooled_results: tuple[PoolResult, ...] | None = None,
    precomputed_samples: dict[StableIdentifier, DistributionSamples] | None = None,
) -> JointDraws:
    """Generate roster-order-independent common-random field draws."""

    if (
        not isinstance(competitors, tuple)
        or not competitors
        or not all(isinstance(item, FieldCompetitorForecast) for item in competitors)
    ):
        raise ContractError("joint drawing requires a nonempty immutable field")
    if not isinstance(model, DependenceModel):
        raise ContractError("joint drawing requires a frozen dependence model")
    if not isinstance(installed_artifact, DependenceArtifact):
        raise ContractError("joint drawing requires the installed dependence artifact")
    expected_model = bind_field_dependence(
        installed_artifact, model.target_context, field_id=model.field_id
    )
    if expected_model != model:
        raise ContractError("field binding differs from installed artifact")
    SamplingSpec(seed=seed, draw_count=draw_count)
    # The canonical artifact necessarily contains both the uniform and sample
    # vectors for every entrant.  Reject an impossible item envelope before
    # allocating either vector; the final canonical serializer still enforces
    # the exact byte and complete-tree bounds.
    minimum_artifact_items = 20 + len(competitors) * (8 + 2 * draw_count)
    if minimum_artifact_items > MAX_JOINT_DRAW_ARTIFACT_ITEMS:
        raise ContractError("joint draw request exceeds the artifact item bound")
    slots = tuple(item.draw_slot for item in competitors)
    identities = tuple(item.competitor_id for item in competitors)
    crn_indices = tuple(item.crn_index for item in competitors)
    if len(slots) != len(set(slots)):
        raise ValueError("draw_slot values must be unique")
    if len(identities) != len(set(identities)):
        raise ValueError("competitor identities must be unique")
    if len(crn_indices) != len(set(crn_indices)):
        raise ValueError("competitor crn_index values must be unique")
    ordered = tuple(sorted(competitors, key=lambda item: item.crn_index))
    effective_rho = _effective_rank_correlation(Decimal(model.rho), len(ordered))
    if uniform_plan is None:
        uniform_plan = generate_joint_uniforms(
            competitors,
            model,
            installed_artifact=installed_artifact,
            seed=seed,
            draw_count=draw_count,
        )
    expected_slots = tuple((item.draw_slot, item.crn_index) for item in ordered)
    if (
        not isinstance(uniform_plan, GeneratedJointUniforms)
        or uniform_plan.field_id != model.field_id
        or uniform_plan.artifact_digest != installed_artifact.artifact_digest
        or uniform_plan.mode is not model.mode
        or uniform_plan.rho != model.rho
        or uniform_plan.effective_rho != _decimal_string(effective_rho)
        or uniform_plan.seed != seed
        or uniform_plan.draw_count != draw_count
        or uniform_plan.slots != expected_slots
    ):
        raise ContractError("joint uniform plan differs from the requested field")
    common_map_digest = uniform_plan.common_random_map_digest
    uniforms = dict(uniform_plan.uniforms)
    pooled_by_id: dict[StableIdentifier, PoolResult] = {}
    if pooled_results is not None and precomputed_samples is not None:
        raise ContractError("joint draws accept only one generated sample authority")
    if pooled_results is not None:
        if (
            not isinstance(pooled_results, tuple)
            or len(pooled_results) != len(ordered)
            or not all(isinstance(item, PoolResult) for item in pooled_results)
        ):
            raise ContractError("receipt-bound pool results must match the field roster")
        pooled_by_id = {
            forecast.competitor_id: result
            for forecast, result in zip(competitors, pooled_results, strict=True)
        }
        if len(pooled_by_id) != len(ordered):
            raise ContractError("receipt-bound pool results must match unique competitors")
    rows = []
    for item in ordered:
        pooled = pooled_by_id.get(item.competitor_id)
        precomputed = (
            None if precomputed_samples is None else precomputed_samples.get(item.competitor_id)
        )
        if precomputed is not None:
            if (
                len(precomputed_samples or ()) != len(ordered)
                or precomputed.seed != seed
                or precomputed.draw_count != draw_count
                or precomputed.distribution_digest != item.distribution.digest
                or precomputed.common_random_map_digest != common_map_digest
            ):
                raise ContractError(
                    "generated aligned samples differ from the exact field uniform slot"
                )
            samples_ms = precomputed.samples_ms
            samples_authority_digest = precomputed.samples_authority_digest
        elif precomputed_samples is not None:
            raise ContractError("generated aligned samples differ from the exact field roster")
        elif pooled is None:
            samples = item.distribution.sample(uniform_plan.sampling_spec(item.draw_slot))
            samples_ms = samples.samples_ms
            samples_authority_digest = samples.samples_authority_digest
        else:
            receipt = pooled.receipt
            samples = pooled.samples
            if (
                samples is None
                or pooled.distribution != item.distribution
                or receipt.pooled_distribution != item.distribution
                or receipt.pooled_samples_ms != samples.samples_ms
                or receipt.pooled_samples_digest != samples.samples_digest
                or receipt.seed != seed
                or receipt.draw_count != draw_count
                or receipt.common_uniforms != uniforms[item.draw_slot]
                or receipt.source_common_random_map_digest != common_map_digest
                or samples.seed != seed
                or samples.draw_count != draw_count
                or samples.distribution_digest != item.distribution.digest
                or samples.common_random_map_digest != receipt.common_random_map_digest
            ):
                raise ContractError(
                    "receipt-bound pool samples differ from the exact field uniform slot"
                )
            samples_ms = samples.samples_ms
            samples_authority_digest = samples.samples_authority_digest
        joint_samples_digest = _samples_digest(
            samples_ms=samples_ms,
            seed=seed,
            distribution_digest=item.distribution.digest,
            common_random_map_digest=common_map_digest,
        )
        rows.append(
            JointCompetitorDraws(
                item.competitor_id,
                item.draw_slot,
                item.crn_index,
                item.distribution.digest,
                uniforms[item.draw_slot],
                samples_ms,
                joint_samples_digest,
                _GeneratedJointCompetitorProof(
                    _generation_token,
                    item.competitor_id,
                    item.draw_slot,
                    item.crn_index,
                    item.distribution.digest,
                    uniforms[item.draw_slot],
                    samples_ms,
                    joint_samples_digest,
                    samples_authority_digest,
                ),
            )
        )
    inputs = DependenceInputs(
        field_id=model.field_id,
        mode=model.mode,
        version="dependence:v1",
        seed=seed,
        draw_count=draw_count,
        parameters_digest=model.parameters_digest,
        effective_sample_size=_decimal_string(Decimal(model.effective_pair_count)),
        fallback_code=model.fallback_code,
    )
    result_rows = tuple(rows)
    values = {
        "inputs": inputs,
        "artifact_digest": installed_artifact.artifact_digest,
        "rho": model.rho,
        "effective_rho": _decimal_string(effective_rho),
        "competitors": result_rows,
        "common_random_map_digest": common_map_digest,
        "algorithm": JOINT_SAMPLING_ALGORITHM,
        "dependency_version": JOINT_DEPENDENCY_VERSION,
        "time_quantum_ms": 1,
    }
    joint_samples_digest = _generated_joint_draw_content_digest(values)
    return JointDraws(
        **values,
        joint_samples_digest=joint_samples_digest,
        _generated_proof=_GeneratedJointDrawsProof(
            _generation_token,
            inputs,
            installed_artifact.artifact_digest,
            model.rho,
            _decimal_string(effective_rho),
            result_rows,
            common_map_digest,
            JOINT_SAMPLING_ALGORITHM,
            JOINT_DEPENDENCY_VERSION,
            1,
            joint_samples_digest,
        ),
    )


def _install_joint_generation_capability():
    """Install a process-local proof token without exposing an issuing primitive."""

    token = object()

    def accepts(
        proof: _GeneratedJointDrawsProof | None,
        inputs: DependenceInputs,
        artifact_digest: str,
        rho: str,
        effective_rho: str,
        competitors: tuple[JointCompetitorDraws, ...],
        common_random_map_digest: str,
        algorithm: str,
        dependency_version: str,
        time_quantum_ms: int,
        joint_samples_digest: str,
    ) -> bool:
        return (
            isinstance(proof, _GeneratedJointDrawsProof)
            and proof.token is token
            and proof.inputs is inputs
            and proof.artifact_digest == artifact_digest
            and proof.rho == rho
            and proof.effective_rho == effective_rho
            and proof.competitors is competitors
            and proof.common_random_map_digest == common_random_map_digest
            and proof.algorithm == algorithm
            and proof.dependency_version == dependency_version
            and proof.time_quantum_ms == time_quantum_ms
            and proof.joint_samples_digest == joint_samples_digest
        )

    def accepts_competitor(
        proof: _GeneratedJointCompetitorProof | None,
        competitor_id: StableIdentifier,
        draw_slot: str,
        crn_index: int,
        distribution_digest: str,
        common_uniforms: tuple[str, ...],
        samples_ms: tuple[int, ...],
        samples_digest: str,
    ) -> bool:
        return (
            isinstance(proof, _GeneratedJointCompetitorProof)
            and proof.token is token
            and proof.competitor_id == competitor_id
            and proof.draw_slot == draw_slot
            and proof.crn_index == crn_index
            and proof.distribution_digest == distribution_digest
            and proof.common_uniforms is common_uniforms
            and proof.samples_ms is samples_ms
            and proof.samples_digest == samples_digest
            and bool(proof.samples_authority_digest)
        )

    def generate(
        competitors: tuple[FieldCompetitorForecast, ...],
        model: DependenceModel,
        *,
        installed_artifact: DependenceArtifact,
        seed: int,
        draw_count: int,
        uniform_plan: GeneratedJointUniforms | None = None,
    ) -> JointDraws:
        return _generate_joint_draws(
            competitors,
            model,
            installed_artifact=installed_artifact,
            seed=seed,
            draw_count=draw_count,
            _generation_token=token,
            uniform_plan=uniform_plan,
        )

    def generate_from_pool_results(
        competitors: tuple[FieldCompetitorForecast, ...],
        pooled_results: tuple[PoolResult, ...],
        model: DependenceModel,
        *,
        installed_artifact: DependenceArtifact,
        seed: int,
        draw_count: int,
        uniform_plan: GeneratedJointUniforms,
    ) -> JointDraws:
        return _generate_joint_draws(
            competitors,
            model,
            installed_artifact=installed_artifact,
            seed=seed,
            draw_count=draw_count,
            _generation_token=token,
            uniform_plan=uniform_plan,
            pooled_results=pooled_results,
        )

    def generate_aligned_components(
        component_fields: tuple[tuple[FieldCompetitorForecast, ...], ...],
        model: DependenceModel,
        *,
        installed_artifact: DependenceArtifact,
        seed: int,
        draw_count: int,
        uniform_plan: GeneratedJointUniforms,
    ) -> tuple[JointDraws, ...]:
        if (
            not isinstance(component_fields, tuple)
            or not 2 <= len(component_fields) <= 3
            or not all(
                isinstance(field, tuple)
                and field
                and all(isinstance(item, FieldCompetitorForecast) for item in field)
                for field in component_fields
            )
        ):
            raise ContractError("aligned component generation requires two or three typed fields")
        ordered_fields = tuple(
            tuple(sorted(field, key=lambda item: item.crn_index)) for field in component_fields
        )
        roster = tuple(
            (item.competitor_id, item.draw_slot, item.crn_index) for item in ordered_fields[0]
        )
        if any(
            tuple((item.competitor_id, item.draw_slot, item.crn_index) for item in field) != roster
            for field in ordered_fields[1:]
        ):
            raise ContractError("aligned component field rosters differ")

        sample_maps: tuple[dict[StableIdentifier, DistributionSamples], ...] = tuple(
            {} for _field in ordered_fields
        )
        for rows in zip(*ordered_fields, strict=True):
            samples = sample_aligned_positive_distributions(
                tuple(item.distribution for item in rows),
                uniform_plan.sampling_spec(rows[0].draw_slot),
            )
            for sample_map, item, sample in zip(sample_maps, rows, samples, strict=True):
                sample_map[item.competitor_id] = sample
        return tuple(
            _generate_joint_draws(
                field,
                model,
                installed_artifact=installed_artifact,
                seed=seed,
                draw_count=draw_count,
                _generation_token=token,
                uniform_plan=uniform_plan,
                precomputed_samples=sample_map,
            )
            for field, sample_map in zip(component_fields, sample_maps, strict=True)
        )

    return (
        generate,
        generate_from_pool_results,
        generate_aligned_components,
        accepts,
        accepts_competitor,
    )


(
    generate_joint_draws,
    generate_joint_draws_from_pool_results,
    generate_aligned_component_joint_draws,
    _accepts_generated_joint_proof,
    _accepts_generated_competitor_proof,
) = _install_joint_generation_capability()
del _install_joint_generation_capability


def has_fresh_joint_generation_proof(value: object) -> bool:
    """Identify a same-call generator result inside the trusted service process.

    This is a performance provenance marker, not a sandbox against arbitrary
    Python code running in this process.  Serialized or reconstructed values do
    not retain it and must always take the complete deterministic replay path.
    """

    if not isinstance(value, JointDraws):
        return False
    return _accepts_generated_joint_proof(
        value._retained_generated_proof,
        value.inputs,
        value.artifact_digest,
        value.rho,
        value.effective_rho,
        value.competitors,
        value.common_random_map_digest,
        value.algorithm,
        value.dependency_version,
        value.time_quantum_ms,
        value.joint_samples_digest,
    )


def _same_field_products(
    observations: tuple[ResidualObservation, ...],
) -> tuple[Decimal, ...]:
    by_field: dict[StableIdentifier, list[ResidualObservation]] = {}
    for item in observations:
        by_field.setdefault(item.field_id, []).append(item)
    products = []
    for field_id in sorted(by_field, key=str):
        rows = sorted(by_field[field_id], key=lambda item: str(item.competitor_id))
        for left, right in combinations(rows, 2):
            products.append(
                Decimal(left.standardized_residual) * Decimal(right.standardized_residual)
            )
    return tuple(products)


def _joint_uniforms(
    competitors: tuple[FieldCompetitorForecast, ...],
    seed: int,
    draw_count: int,
    rho: Decimal,
) -> dict[str, tuple[str, ...]]:
    return _joint_uniforms_from_slots(
        tuple((item.draw_slot, item.crn_index) for item in competitors),
        seed,
        draw_count,
        rho,
    )


def regenerate_joint_uniforms_for_replay(
    slots: tuple[tuple[str, int], ...],
    *,
    seed: int,
    draw_count: int,
    rho: str,
) -> dict[str, tuple[str, ...]]:
    """Recreate a serialized authority's frozen CRN matrix for verification."""

    if (
        not isinstance(slots, tuple)
        or not 1 <= len(slots) <= 12
        or not all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and bool(item[0])
            and len(item[0]) <= 96
            and not isinstance(item[1], bool)
            and isinstance(item[1], int)
            and item[1] >= 0
            for item in slots
        )
    ):
        raise ContractError("joint replay slots must be unique and canonically ordered")
    if len({slot for slot, _crn_index in slots}) != len(slots) or tuple(
        crn_index for _slot, crn_index in slots
    ) != tuple(sorted({crn_index for _slot, crn_index in slots})):
        raise ContractError("joint replay slots must be unique and canonically ordered")
    SamplingSpec(seed=seed, draw_count=draw_count)
    decoded_rho = _decimal(rho, "joint replay rho")
    if not Decimal("-1") < decoded_rho < Decimal("1"):
        raise ContractError("joint replay rho must remain inside the open unit interval")
    return _joint_uniforms_from_slots(slots, seed, draw_count, decoded_rho)


def _joint_uniforms_from_slots(
    slots: tuple[tuple[str, int], ...],
    seed: int,
    draw_count: int,
    rho: Decimal,
) -> dict[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {slot: [] for slot, _crn_index in slots}
    ordinal = {crn_index: index for index, (_slot, crn_index) in enumerate(slots)}
    strength = rho.copy_abs()
    if strength == 0:
        global _NATIVE_RANK_UNIFORM_GENERATOR
        global _NATIVE_RANK_UNIFORM_GENERATOR_INITIALIZED
        if draw_count == 4096 and not _NATIVE_RANK_UNIFORM_GENERATOR_INITIALIZED:
            from strathmark.v3.domain.optimizer_kernel import load_bundled_kernel

            _NATIVE_RANK_UNIFORM_GENERATOR = load_bundled_kernel(required=sys.platform == "win32")
            _NATIVE_RANK_UNIFORM_GENERATOR_INITIALIZED = True
        if draw_count == 4096 and _NATIVE_RANK_UNIFORM_GENERATOR is not None:
            rows = _NATIVE_RANK_UNIFORM_GENERATOR.generate_independent_rank_uniforms(
                seed=seed,
                draw_count=draw_count,
                stream_count=len(slots),
            )
            return {slot: rows[ordinal[crn_index]] for slot, crn_index in slots}
        for draw in range(draw_count):
            for slot, crn_index in slots:
                stream_index = ordinal[crn_index]
                values[slot].append(_rank_uniform(seed, draw, f"crn:{stream_index}"))
        return {slot: tuple(items) for slot, items in values.items()}

    # Freeze the arithmetic context used by the v1 algorithm.  The previous
    # implementation implicitly relied on Python's default Decimal context;
    # explicitly pinning those same settings preserves its bytes while making
    # generation independent of ambient caller state.
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        for draw in range(draw_count):
            gate = Decimal(_rank_uniform(seed, draw, "gate"))
            use_shared = gate < strength
            if use_shared and rho >= 0:
                shared = _rank_uniform(seed, draw, "shared")
                for slot, _crn_index in slots:
                    values[slot].append(shared)
                continue
            if not use_shared:
                for slot, crn_index in slots:
                    stream_index = ordinal[crn_index]
                    values[slot].append(_rank_uniform(seed, draw, f"crn:{stream_index}"))
                continue

            negative_ranks = {
                row[1]: rank
                for rank, row in enumerate(
                    sorted(
                        slots,
                        key=lambda row: sha256(
                            f"negative-rank:{seed}:{draw}:{ordinal[row[1]]}".encode()
                        ).digest(),
                    )
                )
            }
            field_size = Decimal(len(slots))
            for slot, crn_index in slots:
                stream_index = ordinal[crn_index]
                jitter = Decimal(_rank_uniform(seed, draw, f"negative-jitter:{stream_index}"))
                probability = (Decimal(negative_ranks[crn_index]) + jitter) / field_size
                values[slot].append(_decimal_string(probability))
    return {slot: tuple(items) for slot, items in values.items()}


def _joint_common_map_digest(
    *,
    field_id: StableIdentifier,
    artifact_digest: str,
    seed: int,
    draw_count: int,
    mode: DependenceMode,
    effective_rho: str,
    field_size: int,
) -> str:
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-field-crn-map-v1",
            "field_id": str(field_id),
            "artifact_digest": artifact_digest,
            "seed": seed,
            "draw_count": draw_count,
            "mode": mode.value,
            "effective_rho": effective_rho,
            "crn_ordinals": list(range(field_size)),
            "algorithm": JOINT_SAMPLING_ALGORITHM,
            "dependency_version": JOINT_DEPENDENCY_VERSION,
            "time_quantum_ms": 1,
        }
    )


def _rank_uniform(seed: int, draw: int, stream: str) -> str:
    payload = f"field-crn-v1:{seed}:{draw}:{stream}".encode()
    numerator = int.from_bytes(sha256(payload).digest()[:8], "big") + 1
    # Reproduce Decimal precision=28 division exactly with integer arithmetic.
    # This hot path runs once per draw/entrant and the denominator is frozen by
    # the sampling algorithm, so avoiding Decimal object construction materially
    # shortens confirmed-field assembly without changing a single receipt byte.
    exponent = len(str(numerator)) - 20
    if exponent >= 0:
        exponent = -1
    if numerator * _DECIMAL_POWERS[-exponent] < _RANK_UNIFORM_DENOMINATOR:
        exponent -= 1
    scale = 27 - exponent
    quotient, remainder = divmod(numerator * _DECIMAL_POWERS[scale], _RANK_UNIFORM_DENOMINATOR)
    doubled = remainder * 2
    if doubled > _RANK_UNIFORM_DENOMINATOR or (
        doubled == _RANK_UNIFORM_DENOMINATOR and quotient % 2
    ):
        quotient += 1
    if quotient >= _DECIMAL_POWERS[scale]:
        return "1"
    return ("0." + str(quotient).rjust(scale, "0")).rstrip("0").rstrip(".")


def _effective_rank_correlation(rho: Decimal, field_size: int) -> Decimal:
    """Return the exact pairwise latent-rank correlation of the frozen construction."""

    if rho >= 0 or field_size <= 1:
        return rho
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        size = Decimal(field_size)
        return rho * (size + 1) / (size * size)


def _context_chain(context: ContextNode) -> tuple[ContextNode, ...]:
    values = []
    current: ContextNode | None = context
    while current is not None:
        values.append(current)
        current = current.parent
    return tuple(values)


def _context_key(context: ContextNode) -> str:
    values = tuple(item for item in context.to_tuple() if item is not None)
    return "/".join(values) if values else "global"


def _decimal(value: str, label: str) -> Decimal:
    if not isinstance(value, str) or canonical_decimal_string(value) != value:
        raise ContractError(f"{label} must be a canonical decimal string")
    return Decimal(value)


def _digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise ContractError(f"{label} must be a lowercase SHA-256 digest")


def _dependence_model_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-dependence-model-v1",
        "field_id": str(values["field_id"]),
        "artifact_digest": values["artifact_digest"],
        "target_context": values["target_context"].to_dict(),
        "cutoff_sequence": values["cutoff_sequence"],
        "mode": values["mode"].value,
        "rho": values["rho"],
        "effective_pair_count": values["effective_pair_count"],
        "context_pair_counts": list(values["context_pair_counts"]),
        "shrinkage_path": values["shrinkage_path"],
        "policy_digest": values["policy_digest"],
        "parameters_digest": values["parameters_digest"],
        "fallback_code": values["fallback_code"],
    }


def _dependence_artifact_parameters(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-dependence-parameters-v1",
        "target_context": values["target_context"].to_dict(),
        "cutoff_sequence": values["cutoff_sequence"],
        "mode": values["mode"].value,
        "rho": values["rho"],
        "effective_pair_count": values["effective_pair_count"],
        "context_pair_counts": [list(item) for item in values["context_pair_counts"]],
        "shrinkage_path": list(values["shrinkage_path"]),
        "policy": values["policy"].to_dict(),
        "training_evidence_digest": values["training_evidence_digest"],
        "active_projection_digest": values["active_projection_digest"],
        "observation_set_digest": values["observation_set_digest"],
    }


def _dependence_artifact_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-dependence-artifact-v1",
        "artifact_id": str(values["artifact_id"]),
        "version": values["version"],
        "parameters": _dependence_artifact_parameters(values),
        "parameters_digest": values["parameters_digest"],
        "promotion_receipt_digest": values["promotion_receipt_digest"],
        "fallback_code": values["fallback_code"],
    }


def _joint_draw_content(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-joint-draws-v1",
        "inputs": values["inputs"].to_dict(),
        "artifact_digest": values["artifact_digest"],
        "rho": values["rho"],
        "effective_rho": values["effective_rho"],
        "competitors": [item.to_dict() for item in values["competitors"]],
        "common_random_map_digest": values["common_random_map_digest"],
        "algorithm": values["algorithm"],
        "dependency_version": values["dependency_version"],
        "time_quantum_ms": values["time_quantum_ms"],
    }


def _generated_joint_draw_content_digest(values: dict[str, Any]) -> str:
    """Hash one already-validated generated authority without renormalizing it.

    Every leaf in ``values`` has passed its typed contract before this helper is
    reached.  Sorting keys and using the canonical JSON separators therefore
    produces exactly the same bytes as ``canonical_bytes`` while avoiding a
    second recursive walk over both draw arrays.  Decoded/reconstructed values
    still take the complete canonical validation and deterministic replay path
    in ``JointDraws.__post_init__``.
    """

    payload = dumps(
        _joint_draw_content(values),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > MAX_JOINT_DRAW_ARTIFACT_BYTES:
        raise ContractError("joint draws exceed the artifact byte bound")
    return sha256(payload).hexdigest()


def _decimal_string(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 96
        return canonical_decimal_string(+value)


__all__ = [
    "DependenceArtifact",
    "DependenceModel",
    "DependencePolicy",
    "FieldCompetitorForecast",
    "GeneratedJointUniforms",
    "JointCompetitorDraws",
    "JointDraws",
    "ResidualObservation",
    "JOINT_DEPENDENCY_VERSION",
    "JOINT_SAMPLING_ALGORITHM",
    "bind_field_dependence",
    "fit_field_dependence",
    "generate_aligned_component_joint_draws",
    "generate_joint_draws",
    "generate_joint_draws_from_pool_results",
    "generate_joint_uniforms",
    "has_fresh_joint_generation_proof",
    "regenerate_joint_uniforms_for_replay",
    "train_dependence_artifact",
]
