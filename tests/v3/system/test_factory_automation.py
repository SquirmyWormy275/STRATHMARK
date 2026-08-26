from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.application.factory_automation import (
    FAMILY_COMPONENT_ROLES,
    FAMILY_PHASES,
    FactoryAutomationError,
    FactoryAutomationRunner,
    FactoryAutomationSpec,
    FactoryExecutionBoundary,
    FactoryFamily,
    FactoryPhase,
    FactoryPhaseMaterial,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.factory.candidates import (
    CandidateBuilder,
    FactoryRole,
    RoleSnapshot,
)
from tests.v3.system.test_promotion_rollback import ACTOR, NOW, _report, _service


def _digest(label: str) -> str:
    return canonical_digest({"fixture": label})


def _spec(*, rollback_parent_digest: str = "0" * 64) -> FactoryAutomationSpec:
    return FactoryAutomationSpec(
        display_name="automated-complete-bundle",
        code_revision="f31b4c1",
        code_digest=_digest("code"),
        dependency_lock_digest=_digest("lock"),
        data_snapshot_digest=_digest("data"),
        role_snapshots=tuple(
            sorted(
                (
                    RoleSnapshot(FactoryRole.TRAIN, "generation:train", _digest("train")),
                    RoleSnapshot(FactoryRole.TUNE, "generation:tune", _digest("tune")),
                    RoleSnapshot(
                        FactoryRole.CALIBRATION,
                        "generation:calibration",
                        _digest("calibration"),
                    ),
                ),
                key=lambda item: item.role.value,
            )
        ),
        local_model_ids=("ollama:qwen3.5-9b@sha256:" + "a" * 64,),
        cloud_model_ids=(),
        compatibility_contract_digest=_digest("compatibility"),
        rollback_parent_digest=rollback_parent_digest,
    )


class _FamilyExecutor:
    execution_boundary = FactoryExecutionBoundary.LOCAL_CONFIGURED_ONLY

    def __init__(self, family: FactoryFamily) -> None:
        self.family = family
        self.calls: list[tuple[FactoryPhase, str]] = []

    def execute(self, phase: FactoryPhase, *, input_digest: str) -> FactoryPhaseMaterial:
        self.calls.append((phase, input_digest))
        final = phase is FactoryPhase.COMPARE
        components = {
            role: _digest(f"{self.family.value}:{role}")
            for role in FAMILY_COMPONENT_ROLES[self.family]
        }
        return FactoryPhaseMaterial.create(
            family=self.family,
            phase=phase,
            input_digest=input_digest,
            component_digests=components if final else {},
            artifact_payloads=(
                {f"factory/{self.family.value}.json": b'{"verified":true}'} if final else {}
            ),
        )


def test_automatic_factory_runs_every_required_family_phase_and_promotes_only_through_gate(
    tmp_path,
) -> None:
    service, _repository, bundle_signer, evaluator_signer, _database = _service(tmp_path)
    executors = tuple(_FamilyExecutor(family) for family in FactoryFamily)
    reports = {}

    def evaluate(candidate):
        if candidate.candidate_digest not in reports:
            reports[candidate.candidate_digest] = _report(
                tmp_path / "automation-evaluator",
                candidate,
                evaluator_signer,
                generation="audit-automated-complete",
            )
        return reports[candidate.candidate_digest]

    runner = FactoryAutomationRunner(
        service=service,
        candidate_builder=CandidateBuilder(
            allowed_local_models=("ollama:qwen3.5-9b@sha256:" + "a" * 64,),
            allowed_cloud_models=(),
        ),
        executors=executors,
        evaluator=evaluate,
        bundle_signer=bundle_signer,
    )

    outcome = runner.run(
        _spec(),
        request_identity="factory-cycle:one",
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=10,
    )

    assert tuple(item.family for item in outcome.families) == tuple(FactoryFamily)
    assert outcome.factory.promoted is True
    assert outcome.factory.installed is not None
    assert service.active_bundle_digest() == outcome.factory.installed.bundle_digest
    for executor in executors:
        assert (
            tuple(phase for phase, _digest_value in executor.calls)
            == FAMILY_PHASES[executor.family]
        )
        receipts = next(item for item in outcome.families if item.family is executor.family)
        assert tuple(item.phase for item in receipts.phases) == FAMILY_PHASES[executor.family]
        for prior, current in zip(receipts.phases, receipts.phases[1:]):
            assert current.input_digest == prior.output_digest

    retry = runner.run(
        _spec(),
        request_identity="factory-cycle:one",
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=10,
    )
    assert retry.factory == outcome.factory
    assert retry.candidate.candidate_digest == outcome.candidate.candidate_digest


def test_factory_automation_rejects_incomplete_remote_or_cross_family_execution(tmp_path) -> None:
    service, _repository, bundle_signer, evaluator_signer, _database = _service(tmp_path)
    complete = tuple(_FamilyExecutor(family) for family in FactoryFamily)
    builder = CandidateBuilder(allowed_local_models=(), allowed_cloud_models=())
    arguments = {
        "service": service,
        "candidate_builder": builder,
        "evaluator": lambda candidate: _report(
            tmp_path / "reject-evaluator",
            candidate,
            evaluator_signer,
            generation="audit-reject",
        ),
        "bundle_signer": bundle_signer,
    }
    with pytest.raises(FactoryAutomationError, match="every required family"):
        FactoryAutomationRunner(executors=complete[:-1], **arguments)

    remote = _FamilyExecutor(FactoryFamily.FORMULA)
    remote.execution_boundary = "unrestricted_network"
    with pytest.raises(FactoryAutomationError, match="configured local boundary"):
        FactoryAutomationRunner(executors=(remote, *complete[1:]), **arguments)

    wrong = _FamilyExecutor(FactoryFamily.FORMULA)
    original_execute = wrong.execute

    def cross_family(phase, *, input_digest):
        original_execute(phase, input_digest=input_digest)
        return FactoryPhaseMaterial.create(
            family=FactoryFamily.ML,
            phase=phase,
            input_digest=input_digest,
            component_digests={},
            artifact_payloads={},
        )

    wrong.execute = cross_family
    runner = FactoryAutomationRunner(executors=(wrong, *complete[1:]), **arguments)
    with pytest.raises(FactoryAutomationError, match="family or phase"):
        runner.run(
            replace(_spec(), local_model_ids=()),
            request_identity="factory-cycle:invalid",
            actor_id=StableIdentifier("actor:factory-service"),
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=10,
        )
