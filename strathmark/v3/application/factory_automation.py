"""Deterministic orchestration for the complete automated V3 model factory."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from strathmark.v3.application.factory import FactoryRunOutcome, FactoryService
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.factory.candidates import CandidateBuilder, CandidateBundle, RoleSnapshot
from strathmark.v3.factory.evaluator import SignedEvaluationReport
from strathmark.v3.infrastructure.integrity import P256Signer


class FactoryAutomationError(RuntimeError):
    """An automated family phase was incomplete, unbounded, or crossed authority."""


class FactoryFamily(str, Enum):
    FORMULA = "formula"
    ML = "ml"
    LLM = "llm"
    CREDIBILITY = "credibility"
    CAPABILITY = "capability"
    OPTIMIZER_GATE = "optimizer_gate"


class FactoryPhase(str, Enum):
    CONSTRUCT = "construct"
    TRAIN = "train"
    TUNE = "tune"
    REPLAY = "replay"
    CALIBRATE = "calibrate"
    COMPARE = "compare"


class FactoryExecutionBoundary(str, Enum):
    LOCAL_CONFIGURED_ONLY = "local_configured_only"


FAMILY_PHASES: Mapping[FactoryFamily, tuple[FactoryPhase, ...]] = MappingProxyType(
    {
        FactoryFamily.FORMULA: (
            FactoryPhase.CONSTRUCT,
            FactoryPhase.REPLAY,
            FactoryPhase.CALIBRATE,
            FactoryPhase.COMPARE,
        ),
        FactoryFamily.ML: tuple(FactoryPhase),
        FactoryFamily.LLM: (
            FactoryPhase.CONSTRUCT,
            FactoryPhase.TUNE,
            FactoryPhase.REPLAY,
            FactoryPhase.CALIBRATE,
            FactoryPhase.COMPARE,
        ),
        FactoryFamily.CREDIBILITY: (
            FactoryPhase.CONSTRUCT,
            FactoryPhase.TUNE,
            FactoryPhase.REPLAY,
            FactoryPhase.CALIBRATE,
            FactoryPhase.COMPARE,
        ),
        FactoryFamily.CAPABILITY: (
            FactoryPhase.CONSTRUCT,
            FactoryPhase.TUNE,
            FactoryPhase.REPLAY,
            FactoryPhase.CALIBRATE,
            FactoryPhase.COMPARE,
        ),
        FactoryFamily.OPTIMIZER_GATE: (
            FactoryPhase.CONSTRUCT,
            FactoryPhase.TUNE,
            FactoryPhase.REPLAY,
            FactoryPhase.CALIBRATE,
            FactoryPhase.COMPARE,
        ),
    }
)

FAMILY_COMPONENT_ROLES: Mapping[FactoryFamily, tuple[str, ...]] = MappingProxyType(
    {
        FactoryFamily.FORMULA: ("formula",),
        FactoryFamily.ML: ("ml",),
        FactoryFamily.LLM: ("llm_members", "llm_prompts_schemas"),
        FactoryFamily.CREDIBILITY: ("credibility",),
        FactoryFamily.CAPABILITY: ("capability",),
        FactoryFamily.OPTIMIZER_GATE: ("calibration", "disagreement_gate", "optimizer"),
    }
)


@dataclass(frozen=True, slots=True)
class FactoryPhaseMaterial:
    family: FactoryFamily
    phase: FactoryPhase
    input_digest: str
    component_digests: Mapping[str, str]
    artifact_payloads: Mapping[str, bytes]
    output_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.family, FactoryFamily) or not isinstance(self.phase, FactoryPhase):
            raise FactoryAutomationError("factory material family or phase is invalid")
        _digest(self.input_digest, "factory phase input")
        _digest(self.output_digest, "factory phase output")
        components = dict(self.component_digests)
        artifacts = dict(self.artifact_payloads)
        if tuple(components) != tuple(sorted(components)) or tuple(artifacts) != tuple(
            sorted(artifacts)
        ):
            raise FactoryAutomationError("factory phase material must be canonical")
        if len(components) > 9 or len(artifacts) > 32:
            raise FactoryAutomationError("factory phase material exceeds bounded capacity")
        for value in components.values():
            _digest(value, "factory phase component")
        if any(not isinstance(value, bytes) for value in artifacts.values()):
            raise FactoryAutomationError("factory phase artifacts must be immutable bytes")
        if canonical_digest(_phase_body(self)) != self.output_digest:
            raise FactoryAutomationError("factory phase output digest differs")

    @classmethod
    def create(
        cls,
        *,
        family: FactoryFamily,
        phase: FactoryPhase,
        input_digest: str,
        component_digests: Mapping[str, str],
        artifact_payloads: Mapping[str, bytes],
    ) -> FactoryPhaseMaterial:
        components = {name: component_digests[name] for name in sorted(component_digests)}
        artifacts = {name: artifact_payloads[name] for name in sorted(artifact_payloads)}
        shell = _PhaseShell(family, phase, input_digest, components, artifacts)
        return cls(
            family,
            phase,
            input_digest,
            MappingProxyType(components),
            MappingProxyType(artifacts),
            canonical_digest(_phase_body(shell)),
        )


@dataclass(frozen=True, slots=True)
class _PhaseShell:
    family: FactoryFamily
    phase: FactoryPhase
    input_digest: str
    component_digests: Mapping[str, str]
    artifact_payloads: Mapping[str, bytes]


class FactoryFamilyExecutor(Protocol):
    family: FactoryFamily
    execution_boundary: FactoryExecutionBoundary

    def execute(self, phase: FactoryPhase, *, input_digest: str) -> FactoryPhaseMaterial: ...


@dataclass(frozen=True, slots=True)
class FactoryAutomationSpec:
    display_name: str
    code_revision: str
    code_digest: str
    dependency_lock_digest: str
    data_snapshot_digest: str
    role_snapshots: tuple[RoleSnapshot, ...]
    local_model_ids: tuple[str, ...]
    cloud_model_ids: tuple[str, ...]
    compatibility_contract_digest: str
    rollback_parent_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.display_name, str) or not self.display_name:
            raise FactoryAutomationError("factory automation display name is invalid")
        if not isinstance(self.code_revision, str) or not self.code_revision:
            raise FactoryAutomationError("factory automation code revision is invalid")
        for value, label in (
            (self.code_digest, "code"),
            (self.dependency_lock_digest, "dependency lock"),
            (self.data_snapshot_digest, "data snapshot"),
            (self.compatibility_contract_digest, "compatibility"),
            (self.rollback_parent_digest, "rollback parent"),
        ):
            _digest(value, label)
        if not isinstance(self.role_snapshots, tuple) or len(self.role_snapshots) != 3:
            raise FactoryAutomationError("factory automation requires three candidate roles")
        if not isinstance(self.local_model_ids, tuple) or not isinstance(
            self.cloud_model_ids, tuple
        ):
            raise FactoryAutomationError("factory model configuration must be immutable")

    def basis(self, family: FactoryFamily) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-factory-family-input-v1",
            "family": family.value,
            "code_revision": self.code_revision,
            "code_digest": self.code_digest,
            "dependency_lock_digest": self.dependency_lock_digest,
            "data_snapshot_digest": self.data_snapshot_digest,
            "role_snapshots": [item.to_dict() for item in self.role_snapshots],
            "local_model_ids": list(self.local_model_ids),
            "cloud_model_ids": list(self.cloud_model_ids),
            "compatibility_contract_digest": self.compatibility_contract_digest,
            "rollback_parent_digest": self.rollback_parent_digest,
        }


@dataclass(frozen=True, slots=True)
class FactoryFamilyOutcome:
    family: FactoryFamily
    phases: tuple[FactoryPhaseMaterial, ...]


@dataclass(frozen=True, slots=True)
class FactoryAutomationOutcome:
    candidate: CandidateBundle
    families: tuple[FactoryFamilyOutcome, ...]
    factory: FactoryRunOutcome


class FactoryAutomationRunner:
    """Drive every required family, then delegate all authority to FactoryService."""

    def __init__(
        self,
        *,
        service: FactoryService,
        candidate_builder: CandidateBuilder,
        executors: tuple[FactoryFamilyExecutor, ...],
        evaluator: Callable[[CandidateBundle], SignedEvaluationReport],
        bundle_signer: P256Signer,
    ) -> None:
        if not isinstance(service, FactoryService) or not isinstance(
            candidate_builder, CandidateBuilder
        ):
            raise FactoryAutomationError("factory automation requires typed services")
        if not callable(evaluator) or not callable(getattr(bundle_signer, "sign", None)):
            raise FactoryAutomationError("factory automation requires evaluator and signer")
        if not isinstance(executors, tuple) or tuple(item.family for item in executors) != tuple(
            FactoryFamily
        ):
            raise FactoryAutomationError("factory automation requires every required family")
        if any(
            item.execution_boundary is not FactoryExecutionBoundary.LOCAL_CONFIGURED_ONLY
            for item in executors
        ):
            raise FactoryAutomationError("factory executors must use the configured local boundary")
        self._service = service
        self._builder = candidate_builder
        self._executors = executors
        self._evaluator = evaluator
        self._bundle_signer = bundle_signer

    def run(
        self,
        spec: FactoryAutomationSpec,
        *,
        request_identity: str,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> FactoryAutomationOutcome:
        if not isinstance(spec, FactoryAutomationSpec):
            raise FactoryAutomationError("factory automation requires a typed specification")
        components: dict[str, str] = {}
        artifacts: dict[str, bytes] = {}
        families: list[FactoryFamilyOutcome] = []
        for executor in self._executors:
            family = executor.family
            prior = canonical_digest(spec.basis(family))
            phases: list[FactoryPhaseMaterial] = []
            expected_phases = FAMILY_PHASES[family]
            for index, phase in enumerate(expected_phases):
                try:
                    material = executor.execute(phase, input_digest=prior)
                except Exception as exc:
                    raise FactoryAutomationError(
                        f"factory {family.value} {phase.value} phase failed closed"
                    ) from exc
                if (
                    not isinstance(material, FactoryPhaseMaterial)
                    or material.family is not family
                    or material.phase is not phase
                    or material.input_digest != prior
                ):
                    raise FactoryAutomationError("factory material family or phase differs")
                final = index == len(expected_phases) - 1
                expected_roles = FAMILY_COMPONENT_ROLES[family]
                if final:
                    if tuple(material.component_digests) != expected_roles or not (
                        material.artifact_payloads
                    ):
                        raise FactoryAutomationError(
                            "factory compare phase does not cover required components"
                        )
                    for role, digest in material.component_digests.items():
                        if role in components:
                            raise FactoryAutomationError("factory component ownership overlaps")
                        components[role] = digest
                    for name, payload in material.artifact_payloads.items():
                        if name in artifacts:
                            raise FactoryAutomationError("factory artifact ownership overlaps")
                        artifacts[name] = payload
                elif material.component_digests or material.artifact_payloads:
                    raise FactoryAutomationError(
                        "factory intermediate phase cannot publish candidate material"
                    )
                phases.append(material)
                prior = material.output_digest
            families.append(FactoryFamilyOutcome(family, tuple(phases)))
        candidate = self._builder.build(
            display_name=spec.display_name,
            code_revision=spec.code_revision,
            code_digest=spec.code_digest,
            dependency_lock_digest=spec.dependency_lock_digest,
            data_snapshot_digest=spec.data_snapshot_digest,
            role_snapshots=spec.role_snapshots,
            component_digests=components,
            artifact_payloads=artifacts,
            local_model_ids=spec.local_model_ids,
            cloud_model_ids=spec.cloud_model_ids,
            compatibility_contract_digest=spec.compatibility_contract_digest,
            rollback_parent_digest=spec.rollback_parent_digest,
        )
        report = self._evaluator(candidate)
        if not isinstance(report, SignedEvaluationReport):
            raise FactoryAutomationError("factory evaluator did not return a signed report")
        outcome = self._service.run_candidate(
            candidate,
            report,
            signer=self._bundle_signer,
            request_identity=request_identity,
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        return FactoryAutomationOutcome(candidate, tuple(families), outcome)


def _phase_body(value: FactoryPhaseMaterial | _PhaseShell) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-factory-phase-material-v1",
        "family": value.family.value,
        "phase": value.phase.value,
        "input_digest": value.input_digest,
        "component_digests": dict(value.component_digests),
        "artifacts": {
            name: {"byte_count": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for name, payload in value.artifact_payloads.items()
        },
    }


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FactoryAutomationError(f"{label} digest is invalid")
    return value


__all__ = [
    "FAMILY_COMPONENT_ROLES",
    "FAMILY_PHASES",
    "FactoryAutomationError",
    "FactoryAutomationOutcome",
    "FactoryAutomationRunner",
    "FactoryAutomationSpec",
    "FactoryExecutionBoundary",
    "FactoryFamily",
    "FactoryFamilyExecutor",
    "FactoryFamilyOutcome",
    "FactoryPhase",
    "FactoryPhaseMaterial",
]
