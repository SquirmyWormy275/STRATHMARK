from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.assessors import llm_council as council
from strathmark.v3.assessors.llm_council import (
    CandidateStatus,
    configured_cloud_candidate,
    initial_local_candidates,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.forecasts import LLMMemberAudit
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.factory.candidates import CandidateBuilder
from tests.v3.evals.test_factory_audit_isolation import _candidate
from tests.v3.evals.test_llm_semantics import _member
from tests.v3.system.test_promotion_rollback import (
    ZERO,
    _register_evaluate_promote,
    _report,
    _service,
)


def _members():
    cloud = configured_cloud_candidate(
        provider_id="openai",
        family="gpt-5.6-terra",
        model_id="gpt-5.6-terra-2026-08-01",
        model_digest="c" * 64,
        runtime_version="responses-api:2026-08-01",
        runtime_digest="d" * 64,
        sampling_parameters={"seed": 1729, "temperature": "0", "top_p": "1"},
    )
    return (*initial_local_candidates(), cloud)


def _council_candidate(members):
    base = _candidate(rollback_parent_digest=ZERO)
    components = dict(base.component_digests)
    components["llm_members"] = council.council_component_digest(members)
    builder = CandidateBuilder(
        allowed_local_models=tuple(
            sorted(council.council_factory_model_identity(item) for item in members[:2])
        ),
        allowed_cloud_models=(council.council_factory_model_identity(members[2]),),
    )
    return builder.build(
        display_name="operational-council",
        code_revision=base.code_revision,
        code_digest=base.code_digest,
        dependency_lock_digest=base.dependency_lock_digest,
        data_snapshot_digest=base.data_snapshot_digest,
        role_snapshots=base.role_snapshots,
        component_digests=components,
        artifact_payloads=dict(base.artifact_payloads),
        local_model_ids=tuple(
            sorted(council.council_factory_model_identity(item) for item in members[:2])
        ),
        cloud_model_ids=(council.council_factory_model_identity(members[2]),),
        compatibility_contract_digest=base.compatibility_contract_digest,
        rollback_parent_digest=base.rollback_parent_digest,
    )


def _audit(member):
    return LLMMemberAudit(
        prompt_digest=canonical_digest({"member": member.member_id}),
        schema_version="strathmark-v3-llm-output-v1",
        runtime_version=member.runtime_version,
        model_digest=member.model_digest,
        quantization=member.quantization,
        sampling_parameters_digest=member.sampling_parameters_digest,
        raw_response_digest=canonical_digest({"response": member.member_id}),
        validator_code="valid_committed",
        latency_ms=10,
        provider_model_version=member.model_id,
        provider_fingerprint=None,
        api_revision=None,
        canary_digest=None,
    )


def test_promoted_whole_bundle_authorizes_numeric_council_and_replay(tmp_path) -> None:
    members = _members()
    candidate = _council_candidate(members)
    service, repository, bundle_signer, evaluator_signer, _database = _service(tmp_path)
    report = _report(tmp_path, candidate, evaluator_signer, generation="audit-council")
    installed, _receipt = _register_evaluate_promote(
        service,
        repository,
        candidate,
        report,
        bundle_signer,
        key="council",
    )

    authority = council.load_promoted_council(
        service,
        StableIdentifier("tournament:not-open-yet"),
        members,
    )
    outcomes = tuple(
        replace(
            _member(
                member.member_id,
                40_000 + index * 1_000,
                provider_kind=member.provider_kind,
                family=member.family,
            ),
            audit=_audit(member),
        )
        for index, member in enumerate(members)
    )

    assessment = council.aggregate_council(outcomes, authority=authority)
    assert assessment.authority_class == "installed_promoted_bundle"
    assert assessment.candidate_status is CandidateStatus.PROMOTED
    assert assessment.bundle_digest == installed.bundle_digest
    assert assessment.distribution is not None

    sealed = council.seal_council_receipt(assessment, authority=authority)
    assert council.replay_sealed_council(sealed, authority=authority) == assessment
    member_sealed = tuple(
        council.seal_member_outcome(outcome, authority=authority) for outcome in outcomes
    )
    assert (
        tuple(
            council.replay_sealed_member_outcome(item, authority=authority)
            for item in member_sealed
        )
        == outcomes
    )
    with pytest.raises(ValueError, match="promoted council authority"):
        council.replay_sealed_council(sealed)
    with pytest.raises(ValueError, match="promoted council authority"):
        council.replay_sealed_member_outcome(member_sealed[0])


def test_operational_council_rejects_unpromoted_or_identity_drift(tmp_path) -> None:
    members = _members()
    candidate = _council_candidate(members)
    service, repository, bundle_signer, evaluator_signer, _database = _service(tmp_path)
    report = _report(tmp_path, candidate, evaluator_signer, generation="audit-council-drift")
    _register_evaluate_promote(
        service,
        repository,
        candidate,
        report,
        bundle_signer,
        key="council-drift",
    )
    authority = council.load_promoted_council(
        service,
        StableIdentifier("tournament:not-open-yet"),
        members,
    )
    outcomes = tuple(
        replace(
            _member(
                member.member_id,
                40_000 + index * 1_000,
                provider_kind=member.provider_kind,
                family=member.family,
            ),
            audit=_audit(member),
        )
        for index, member in enumerate(members)
    )

    with pytest.raises(ValueError, match="promoted council authority"):
        council.aggregate_council(outcomes)
    with pytest.raises(ValueError, match="identity"):
        council.aggregate_council(
            (replace(outcomes[0], family="forged"), *outcomes[1:]),
            authority=authority,
        )
    with pytest.raises(ValueError, match="artifact"):
        council.aggregate_council(
            (
                replace(outcomes[0], audit=replace(outcomes[0].audit, model_digest="f" * 64)),
                *outcomes[1:],
            ),
            authority=authority,
        )
    sealed = council.seal_member_outcome(outcomes[0], authority=authority)
    with pytest.raises(ValueError, match="canonical|digest|fields"):
        council.replay_sealed_member_outcome(sealed[:-1] + b"0", authority=authority)
