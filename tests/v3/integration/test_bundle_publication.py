from __future__ import annotations

import json
from dataclasses import replace

import pytest

from strathmark.v3.factory.evaluator import (
    AuditGenerationRegistry,
    EvaluationGate,
    FrozenEvaluationHarness,
    FrozenEvaluator,
)
from strathmark.v3.infrastructure.artifacts import (
    ActivationPurpose,
    ArtifactError,
    BundleRepository,
    BundleRuntimeInventory,
    FactoryTrustPolicy,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
)
from tests.v3.evals.test_factory_audit_isolation import DIGESTS, _candidate


def _passing_report(tmp_path, candidate, evaluator_signer):
    harness = FrozenEvaluationHarness.create(
        generation_id="audit-generation-publication",
        audit_snapshot_digest=DIGESTS[22],
        harness_code_digest=DIGESTS[23],
        precommit_digest=DIGESTS[24],
        gates=(EvaluationGate("normalized_crps", "lte", 0.25),),
        frozen_at="2026-08-25T09:00:00.000Z",
    )
    return FrozenEvaluator(
        harness,
        AuditGenerationRegistry(tmp_path / "consumed"),
        signer=evaluator_signer,
    ).evaluate(
        candidate,
        metrics={"normalized_crps": 0.20},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at="2026-08-25T09:01:00.000Z",
    )


def _repository(tmp_path, *, bundle_signer, evaluator_signer, retired=(), revoked=()):
    policy = FactoryTrustPolicy(
        bundle_trust_store=IntegrityTrustStore((bundle_signer.identity,)),
        evaluator_trust_store=IntegrityTrustStore((evaluator_signer.identity,)),
        retired_key_ids=retired,
        revoked_key_ids=revoked,
    )
    return BundleRepository(tmp_path / "bundles", trust_policy=policy)


def test_bundle_publication_is_signed_content_addressed_and_immutable(tmp_path) -> None:
    candidate = _candidate()
    evaluator = P256EphemeralSigner.generate("integrity-key:evaluator-publish")
    bundle = P256EphemeralSigner.generate("integrity-key:bundle-publish")
    report = _passing_report(tmp_path, candidate, evaluator)
    repository = _repository(tmp_path, bundle_signer=bundle, evaluator_signer=evaluator)

    installed = repository.publish(
        candidate,
        report,
        signer=bundle,
        created_at="2026-08-25T09:02:00.000Z",
    )
    assert installed.path.name == installed.bundle_digest
    assert installed.path.is_dir()
    assert (
        repository.verify(installed.bundle_digest, purpose=ActivationPurpose.NEW_ACTIVATION)
        == installed
    )
    assert (
        repository.publish(
            candidate,
            report,
            signer=bundle,
            created_at="2026-08-25T09:02:00.000Z",
        )
        == installed
    )

    manifest = json.loads((installed.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["key_id"] == bundle.identity.key_id
    assert candidate.code_digest in manifest["body_json"]
    assert report.manifest.body_digest in manifest["body_json"]
    assert candidate.rollback_parent_digest in manifest["body_json"]

    with pytest.raises((FileExistsError, PermissionError, OSError)):
        (installed.path / "formula" / "manifest.json").write_bytes(b"tamper")


def test_partial_install_never_becomes_a_visible_bundle(tmp_path) -> None:
    candidate = _candidate()
    evaluator = P256EphemeralSigner.generate("integrity-key:evaluator-crash")
    bundle = P256EphemeralSigner.generate("integrity-key:bundle-crash")
    report = _passing_report(tmp_path, candidate, evaluator)
    repository = _repository(tmp_path, bundle_signer=bundle, evaluator_signer=evaluator)

    def crash(stage: str) -> None:
        if stage == "after_artifacts":
            raise RuntimeError("simulated power loss")

    with pytest.raises(RuntimeError, match="power loss"):
        repository.publish(
            candidate,
            report,
            signer=bundle,
            created_at="2026-08-25T09:03:00.000Z",
            fault_hook=crash,
        )
    assert repository.installed_digests() == ()


def test_production_publication_rejects_exportable_or_ephemeral_signing_keys(tmp_path) -> None:
    candidate = _candidate()
    evaluator = P256EphemeralSigner.generate("integrity-key:evaluator-production-gate")
    bundle = P256EphemeralSigner.generate("integrity-key:bundle-production-gate")
    report = _passing_report(tmp_path, candidate, evaluator)
    policy = FactoryTrustPolicy(
        bundle_trust_store=IntegrityTrustStore((bundle.identity,)),
        evaluator_trust_store=IntegrityTrustStore((evaluator.identity,)),
    )
    repository = BundleRepository(
        tmp_path / "production-bundles", trust_policy=policy, production=True
    )

    with pytest.raises(ArtifactError, match="CNG"):
        repository.publish(candidate, report, signer=bundle, created_at="2026-08-25T09:03:00.000Z")


def test_preflight_requires_exact_dependencies_models_credentials_warmth_and_no_downloads(
    tmp_path,
) -> None:
    candidate = _candidate(rollback_parent_digest="0" * 64)
    evaluator = P256EphemeralSigner.generate("integrity-key:evaluator-preflight")
    bundle = P256EphemeralSigner.generate("integrity-key:bundle-preflight")
    report = _passing_report(tmp_path, candidate, evaluator)
    repository = _repository(tmp_path, bundle_signer=bundle, evaluator_signer=evaluator)
    installed = repository.publish(
        candidate,
        report,
        signer=bundle,
        created_at="2026-08-25T09:03:30.000Z",
    )
    inventory = BundleRuntimeInventory(
        dependency_lock_digest=candidate.dependency_lock_digest,
        compatibility_contract_digest=candidate.compatibility_contract_digest,
        installed_local_model_ids=candidate.local_model_ids,
        warmed_local_model_ids=candidate.local_model_ids,
        configured_cloud_model_ids=candidate.cloud_model_ids,
        cloud_credentials_configured=True,
        offline_fallbacks_ready=True,
        download_attempted=False,
    )
    attestation = repository.preflight(
        installed.bundle_digest,
        inventory,
        purpose=ActivationPurpose.NEW_TOURNAMENT,
    )
    assert attestation.bundle_digest == installed.bundle_digest
    assert "downloads_forbidden" in attestation.checks

    with pytest.raises(ArtifactError, match="downloads"):
        repository.preflight(
            installed.bundle_digest,
            replace(inventory, download_attempted=True),
            purpose=ActivationPurpose.NEW_TOURNAMENT,
        )
    with pytest.raises(ArtifactError, match="warmed"):
        repository.preflight(
            installed.bundle_digest,
            replace(inventory, warmed_local_model_ids=()),
            purpose=ActivationPurpose.NEW_TOURNAMENT,
        )


def test_report_artifact_parent_and_signer_substitution_fail_closed(tmp_path) -> None:
    candidate = _candidate()
    evaluator = P256EphemeralSigner.generate("integrity-key:evaluator-substitution")
    bundle = P256EphemeralSigner.generate("integrity-key:bundle-substitution")
    untrusted = P256EphemeralSigner.generate("integrity-key:untrusted")
    report = _passing_report(tmp_path, candidate, evaluator)
    repository = _repository(tmp_path, bundle_signer=bundle, evaluator_signer=evaluator)

    with pytest.raises(ArtifactError, match="trusted"):
        repository.publish(
            candidate,
            report,
            signer=untrusted,
            created_at="2026-08-25T09:04:00.000Z",
        )

    installed = repository.publish(
        candidate,
        report,
        signer=bundle,
        created_at="2026-08-25T09:05:00.000Z",
    )
    artifact = installed.path / "ml" / "universal.json"
    artifact.chmod(0o600)
    artifact.write_bytes(b'{"tampered":true}')
    with pytest.raises(ArtifactError, match="digest"):
        repository.verify(installed.bundle_digest, purpose=ActivationPurpose.HISTORICAL_VERIFY)


def test_retirement_preserves_pinned_history_but_revocation_blocks_new_use(tmp_path) -> None:
    candidate = _candidate()
    evaluator = P256EphemeralSigner.generate("integrity-key:evaluator-retire")
    bundle = P256EphemeralSigner.generate("integrity-key:bundle-retire")
    report = _passing_report(tmp_path, candidate, evaluator)
    repository = _repository(tmp_path, bundle_signer=bundle, evaluator_signer=evaluator)
    installed = repository.publish(
        candidate,
        report,
        signer=bundle,
        created_at="2026-08-25T09:06:00.000Z",
    )

    retired = _repository(
        tmp_path,
        bundle_signer=bundle,
        evaluator_signer=evaluator,
        retired=(bundle.identity.key_id,),
    )
    assert (
        retired.verify(
            installed.bundle_digest, purpose=ActivationPurpose.PINNED_TOURNAMENT
        ).bundle_digest
        == installed.bundle_digest
    )
    with pytest.raises(ArtifactError, match="retired"):
        retired.verify(installed.bundle_digest, purpose=ActivationPurpose.NEW_TOURNAMENT)

    revoked = _repository(
        tmp_path,
        bundle_signer=bundle,
        evaluator_signer=evaluator,
        revoked=(bundle.identity.key_id,),
    )
    assert (
        revoked.verify(
            installed.bundle_digest, purpose=ActivationPurpose.HISTORICAL_VERIFY
        ).bundle_digest
        == installed.bundle_digest
    )
    with pytest.raises(ArtifactError, match="revoked"):
        revoked.verify(installed.bundle_digest, purpose=ActivationPurpose.PINNED_TOURNAMENT)
