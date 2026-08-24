"""Causal same-field residual dependence and deterministic joint race draws."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from hashlib import sha256
from itertools import combinations
from typing import Any

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import (
    DependenceInputs,
    DependenceMode,
    PositiveTimeDistribution,
    SamplingSpec,
    _samples_digest,
)
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.domain.credibility import ContextNode
from strathmark.v3.domain.pooling import LinearPooledDistribution

JOINT_SAMPLING_ALGORITHM = "sha256-u64-shared-rank-copula-v1"
JOINT_DEPENDENCY_VERSION = "stdlib-integer-v1"


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
class JointCompetitorDraws:
    competitor_id: StableIdentifier
    draw_slot: str
    crn_index: int
    distribution_digest: str
    common_uniforms: tuple[str, ...]
    samples_ms: tuple[int, ...]
    samples_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        if not isinstance(self.draw_slot, str) or not self.draw_slot:
            raise ContractError("joint draw slot is required")
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
        for value in self.common_uniforms:
            number = _decimal(value, "joint common uniform")
            if not 0 < number < 1:
                raise ContractError("joint common uniforms must be inside the unit interval")
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in self.samples_ms
        ):
            raise ContractError("joint samples must be positive integer milliseconds")

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

    def __post_init__(self) -> None:
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
        _digest(self.joint_samples_digest, "joint_samples_digest")
        if self.joint_samples_digest != canonical_digest(self.content_value()):
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
        if set(value) != expected or value["schema_version"] != "strathmark-v3-joint-draws-v1":
            raise ContractError("joint draws fields or schema differ")
        if not isinstance(value["inputs"], dict) or not isinstance(value["competitors"], list):
            raise ContractError("joint draws nested values are invalid")
        return cls(
            DependenceInputs.from_dict(value["inputs"]),
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


def generate_joint_draws(
    competitors: tuple[FieldCompetitorForecast, ...],
    model: DependenceModel,
    *,
    installed_artifact: DependenceArtifact,
    seed: int,
    draw_count: int,
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
    common_map_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-field-crn-map-v1",
            "field_id": str(model.field_id),
            "artifact_digest": installed_artifact.artifact_digest,
            "seed": seed,
            "draw_count": draw_count,
            "mode": model.mode.value,
            "effective_rho": _decimal_string(effective_rho),
            "crn_ordinals": list(range(len(ordered))),
            "algorithm": JOINT_SAMPLING_ALGORITHM,
            "dependency_version": JOINT_DEPENDENCY_VERSION,
            "time_quantum_ms": 1,
        }
    )
    uniforms = _joint_uniforms(ordered, seed, draw_count, Decimal(model.rho))
    rows = []
    for item in ordered:
        samples = item.distribution.sample(
            SamplingSpec(
                seed=seed,
                draw_count=draw_count,
                common_uniforms=uniforms[item.draw_slot],
                common_random_map_digest=common_map_digest,
            )
        )
        rows.append(
            JointCompetitorDraws(
                item.competitor_id,
                item.draw_slot,
                item.crn_index,
                item.distribution.digest,
                uniforms[item.draw_slot],
                samples.samples_ms,
                samples.samples_digest,
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
    return JointDraws(
        **values,
        joint_samples_digest=canonical_digest(_joint_draw_content(values)),
    )


def _same_field_products(
    observations: tuple[ResidualObservation, ...],
) -> tuple[Decimal, ...]:
    by_field: dict[StableIdentifier, list[ResidualObservation]] = {}
    for item in observations:
        by_field.setdefault(item.field_id, []).append(item)
    products = []
    for field in sorted(by_field, key=str):
        rows = sorted(by_field[field], key=lambda item: str(item.competitor_id))
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
    values: dict[str, list[str]] = {item.draw_slot: [] for item in competitors}
    ordinal = {item.crn_index: index for index, item in enumerate(competitors)}
    strength = abs(rho)
    for draw in range(draw_count):
        gate = Decimal(_rank_uniform(seed, draw, "gate"))
        shared = Decimal(_rank_uniform(seed, draw, "shared"))
        use_shared = gate < strength
        negative_ranks = (
            {
                item.crn_index: rank
                for rank, item in enumerate(
                    sorted(
                        competitors,
                        key=lambda row: sha256(
                            f"negative-rank:{seed}:{draw}:{ordinal[row.crn_index]}".encode()
                        ).digest(),
                    )
                )
            }
            if use_shared and rho < 0
            else {}
        )
        for item in competitors:
            stream_index = ordinal[item.crn_index]
            probability = Decimal(_rank_uniform(seed, draw, f"crn:{stream_index}"))
            if use_shared:
                if rho >= 0:
                    probability = shared
                else:
                    jitter = Decimal(_rank_uniform(seed, draw, f"negative-jitter:{stream_index}"))
                    probability = (Decimal(negative_ranks[item.crn_index]) + jitter) / Decimal(
                        len(competitors)
                    )
            values[item.draw_slot].append(_decimal_string(probability))
    return {slot: tuple(items) for slot, items in values.items()}


def _rank_uniform(seed: int, draw: int, stream: str) -> str:
    payload = f"field-crn-v1:{seed}:{draw}:{stream}".encode()
    numerator = int.from_bytes(sha256(payload).digest()[:8], "big") + 1
    return _decimal_string(Decimal(numerator) / Decimal(2**64 + 1))


def _effective_rank_correlation(rho: Decimal, field_size: int) -> Decimal:
    """Return the exact pairwise latent-rank correlation of the frozen construction."""

    if rho >= 0 or field_size <= 1:
        return rho
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


def _decimal_string(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 96
        return canonical_decimal_string(+value)


__all__ = [
    "DependenceArtifact",
    "DependenceModel",
    "DependencePolicy",
    "FieldCompetitorForecast",
    "JointCompetitorDraws",
    "JointDraws",
    "ResidualObservation",
    "JOINT_DEPENDENCY_VERSION",
    "JOINT_SAMPLING_ALGORITHM",
    "bind_field_dependence",
    "fit_field_dependence",
    "generate_joint_draws",
    "train_dependence_artifact",
]
