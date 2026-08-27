from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.factory.candidates import (
    REQUIRED_COMPONENT_ROLES,
    CandidateBuilder,
    CandidateError,
    FactoryRole,
    RoleSnapshot,
)
from strathmark.v3.factory.evaluator import (
    AuditGenerationRegistry,
    EvaluationError,
    EvaluationGate,
    FactoryIsolationAttestation,
    FactoryServiceRole,
    FrozenEvaluationHarness,
    FrozenEvaluator,
    IsolationProbe,
    verify_evaluation_report,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
)

DIGESTS = tuple(f"{value:064x}" for value in range(1, 40))


def _candidate(
    *,
    name: str = "candidate-a",
    dependency_digest: str = DIGESTS[2],
    data_snapshot_digest: str = DIGESTS[1],
    rollback_parent_digest: str = DIGESTS[21],
):
    builder = CandidateBuilder(
        allowed_local_models=("ollama:qwen3.5-9b@sha256:1234",),
        allowed_cloud_models=("openai:gpt-5.6-terra@2026-08-01",),
    )
    roles = (
        RoleSnapshot(FactoryRole.TRAIN, "generation-1", DIGESTS[3]),
        RoleSnapshot(FactoryRole.TUNE, "generation-1", DIGESTS[4]),
        RoleSnapshot(FactoryRole.CALIBRATION, "generation-1", DIGESTS[5]),
    )
    components = {role: DIGESTS[index + 6] for index, role in enumerate(REQUIRED_COMPONENT_ROLES)}
    return builder.build(
        display_name=name,
        code_revision="git:abc123",
        code_digest=DIGESTS[0],
        dependency_lock_digest=dependency_digest,
        data_snapshot_digest=data_snapshot_digest,
        role_snapshots=roles,
        component_digests=components,
        artifact_payloads={
            "formula/manifest.json": b'{"schema_version":"formula-v1"}',
            "ml/universal.json": b'{"schema_version":"ml-v1"}',
            "llm/prompts.json": b'{"schema_version":"prompts-v1"}',
        },
        local_model_ids=("ollama:qwen3.5-9b@sha256:1234",),
        cloud_model_ids=("openai:gpt-5.6-terra@2026-08-01",),
        compatibility_contract_digest=DIGESTS[20],
        rollback_parent_digest=rollback_parent_digest,
    )


def _harness() -> FrozenEvaluationHarness:
    return FrozenEvaluationHarness.create(
        generation_id="audit-generation-2026-01",
        audit_snapshot_digest=DIGESTS[22],
        harness_code_digest=DIGESTS[23],
        precommit_digest=DIGESTS[24],
        gates=(
            EvaluationGate("coverage", "gte", 0.90),
            EvaluationGate("normalized_crps", "lte", 0.25),
        ),
        frozen_at="2026-08-25T08:00:00.000Z",
    )


def test_builder_never_accepts_the_locked_audit_role() -> None:
    candidate = _candidate()
    audit = RoleSnapshot(FactoryRole.AUDIT, "audit-generation-2026-01", DIGESTS[22])

    with pytest.raises(CandidateError, match="audit"):
        CandidateBuilder(
            allowed_local_models=candidate.local_model_ids,
            allowed_cloud_models=candidate.cloud_model_ids,
        ).build(
            display_name="illegal-audit-reader",
            code_revision=candidate.code_revision,
            code_digest=candidate.code_digest,
            dependency_lock_digest=candidate.dependency_lock_digest,
            data_snapshot_digest=candidate.data_snapshot_digest,
            role_snapshots=(*candidate.role_snapshots, audit),
            component_digests=dict(candidate.component_digests),
            artifact_payloads=dict(candidate.artifact_payloads),
            local_model_ids=candidate.local_model_ids,
            cloud_model_ids=candidate.cloud_model_ids,
            compatibility_contract_digest=candidate.compatibility_contract_digest,
            rollback_parent_digest=candidate.rollback_parent_digest,
        )


def test_builder_rejects_alias_drift_unconfigured_models_and_executable_artifacts() -> None:
    builder = CandidateBuilder(
        allowed_local_models=("ollama:qwen3.5-9b@sha256:1234",),
        allowed_cloud_models=("openai:gpt-5.6-terra@2026-08-01",),
    )
    base = _candidate()
    common = dict(
        display_name="bad-input",
        code_revision=base.code_revision,
        code_digest=base.code_digest,
        dependency_lock_digest=base.dependency_lock_digest,
        data_snapshot_digest=base.data_snapshot_digest,
        role_snapshots=base.role_snapshots,
        component_digests=dict(base.component_digests),
        artifact_payloads=dict(base.artifact_payloads),
        local_model_ids=base.local_model_ids,
        compatibility_contract_digest=base.compatibility_contract_digest,
        rollback_parent_digest=base.rollback_parent_digest,
    )
    with pytest.raises(CandidateError, match="pinned"):
        builder.build(**common, cloud_model_ids=("openai:gpt-latest",))
    with pytest.raises(CandidateError, match="configured"):
        builder.build(**common, cloud_model_ids=("anthropic:claude@2026-08-01",))
    with pytest.raises(CandidateError, match="executable"):
        builder.build(
            **{**common, "artifact_payloads": {"model.pkl": b"pickle"}},
            cloud_model_ids=base.cloud_model_ids,
        )
    with pytest.raises(CandidateError, match="secret"):
        builder.build(
            **{
                **common,
                "artifact_payloads": {"provider/config.txt": b"API_KEY=not-for-a-bundle"},
            },
            cloud_model_ids=base.cloud_model_ids,
        )


def test_os_isolation_attestation_requires_four_distinct_least_privilege_identities() -> None:
    probes = (
        IsolationProbe(
            FactoryServiceRole.BUILDER,
            "service:factory-builder",
            True,
            True,
            False,
            False,
            False,
            False,
        ),
        IsolationProbe(
            FactoryServiceRole.EVALUATOR,
            "service:factory-evaluator",
            True,
            False,
            True,
            True,
            False,
            False,
        ),
        IsolationProbe(
            FactoryServiceRole.BUNDLE_SIGNER,
            "service:factory-signer",
            False,
            False,
            False,
            False,
            True,
            False,
        ),
        IsolationProbe(
            FactoryServiceRole.APPLICATION,
            "service:strathmark-app",
            False,
            False,
            False,
            False,
            False,
            False,
        ),
    )
    attestation = FactoryIsolationAttestation.create(
        host_id="host:race-day-windows",
        probes=probes,
        observed_at="2026-08-25T08:00:00.000Z",
        probe_evidence_digest=DIGESTS[26],
    )
    assert attestation.attestation_digest == canonical_digest(attestation.body())

    insecure = tuple(
        replace(item, network_allowed=True) if item.role is FactoryServiceRole.EVALUATOR else item
        for item in probes
    )
    with pytest.raises(EvaluationError, match="network"):
        FactoryIsolationAttestation.create(
            host_id="host:race-day-windows",
            probes=insecure,
            observed_at="2026-08-25T08:00:00.000Z",
            probe_evidence_digest=DIGESTS[26],
        )


def test_one_use_audit_generation_is_lineage_bound_and_not_a_tuning_oracle(tmp_path) -> None:
    candidate = _candidate()
    signer = P256EphemeralSigner.generate("integrity-key:evaluator-1")
    trust = IntegrityTrustStore((signer.identity,))
    evaluator = FrozenEvaluator(
        _harness(), AuditGenerationRegistry(tmp_path / "audit-consumption"), signer=signer
    )

    report = evaluator.evaluate(
        candidate,
        metrics={"coverage": 0.94, "normalized_crps": 0.21},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:01:00.000Z",
    )
    verified = verify_evaluation_report(
        report,
        trust_store=trust,
        expected_candidate=candidate,
        expected_harness=_harness(),
    )
    assert verified.passed is True
    assert verified.failed_gates == ()
    assert set(verified.public_summary) == {"coverage", "normalized_crps"}
    assert "rows" not in report.manifest.body_json
    assert "slices" not in report.manifest.body_json

    # A cosmetic name is deliberately outside lineage identity.
    renamed = _candidate(name="same-artifacts-new-name")
    assert renamed.lineage_digest == candidate.lineage_digest
    with pytest.raises(EvaluationError, match="consumed"):
        evaluator.evaluate(
            renamed,
            metrics={"coverage": 0.95, "normalized_crps": 0.20},
            observed_audit_snapshot_digest=DIGESTS[22],
            created_at="2026-08-25T08:02:00.000Z",
        )

    # Dependency-only and descendant mutations cannot probe the same answer sheet either.
    mutated = _candidate(name="dependency-mutation", dependency_digest=DIGESTS[25])
    assert mutated.lineage_digest != candidate.lineage_digest
    with pytest.raises(EvaluationError, match="consumed"):
        evaluator.evaluate(
            mutated,
            metrics={"coverage": 0.99, "normalized_crps": 0.01},
            observed_audit_snapshot_digest=DIGESTS[22],
            created_at="2026-08-25T08:03:00.000Z",
        )


def test_report_binds_frozen_thresholds_candidate_and_audit_snapshot(tmp_path) -> None:
    candidate = _candidate()
    signer = P256EphemeralSigner.generate("integrity-key:evaluator-2")
    trust = IntegrityTrustStore((signer.identity,))
    harness = _harness()
    report = FrozenEvaluator(
        harness, AuditGenerationRegistry(tmp_path / "audit-consumption"), signer=signer
    ).evaluate(
        candidate,
        metrics={"coverage": 0.89, "normalized_crps": 0.20},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T08:04:00.000Z",
    )
    verified = verify_evaluation_report(
        report,
        trust_store=trust,
        expected_candidate=candidate,
        expected_harness=harness,
    )
    assert verified.passed is False
    assert verified.failed_gates == ("coverage",)

    relaxed = replace(
        harness,
        gates=(
            EvaluationGate("coverage", "gte", 0.80),
            EvaluationGate("normalized_crps", "lte", 0.25),
        ),
    )
    assert relaxed.harness_digest != harness.harness_digest
    with pytest.raises(EvaluationError, match="harness"):
        verify_evaluation_report(
            report,
            trust_store=trust,
            expected_candidate=candidate,
            expected_harness=relaxed,
        )
    with pytest.raises(EvaluationError, match="audit snapshot"):
        FrozenEvaluator(
            harness, AuditGenerationRegistry(tmp_path / "other"), signer=signer
        ).evaluate(
            candidate,
            metrics={"coverage": 0.99, "normalized_crps": 0.01},
            observed_audit_snapshot_digest=canonical_digest({"wrong": True}),
            created_at="2026-08-25T08:05:00.000Z",
        )

    leaky = _candidate(name="leaky-role", data_snapshot_digest=DIGESTS[22])
    with pytest.raises(EvaluationError, match="not disjoint"):
        FrozenEvaluator(
            harness, AuditGenerationRegistry(tmp_path / "leaky"), signer=signer
        ).evaluate(
            leaky,
            metrics={"coverage": 0.99, "normalized_crps": 0.01},
            observed_audit_snapshot_digest=DIGESTS[22],
            created_at="2026-08-25T08:06:00.000Z",
        )
