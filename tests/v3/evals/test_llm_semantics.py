from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

import strathmark.v3.assessors.llm_council as council
from strathmark.v3.assessors.llm_council import (
    CandidateEvaluationReport,
    CandidatePromotionError,
    CandidateStatus,
    CouncilAvailability,
    DeadlineBudget,
    EphemeralTestCandidateEvaluationAuthority,
    HMACTokenKey,
    MemberOutcome,
    MemoryRawOutputSink,
    ProviderCallError,
    ProviderKind,
    RawAttempt,
    SealedLLMJob,
    configured_cloud_candidate,
    evaluate_candidate_rotation_receipts,
    initial_local_candidates,
    replay_sealed_council,
    seal_council_receipt,
)
from strathmark.v3.assessors.output_validation import ValidatedMemberOutput
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.forecasts import (
    LLMMemberAudit,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.infrastructure.blobs import ContentAddressedBlobStore
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.ollama import ContentAddressedRawOutputSink

aggregate_council = council._aggregate_outcomes


def _distribution(center: int, width: int) -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        tuple(
            QuantilePoint(probability, center + offset * width)
            for probability, offset in (
                ("0.05", -3),
                ("0.1", -2),
                ("0.25", -1),
                ("0.5", 0),
                ("0.75", 1),
                ("0.9", 2),
                ("0.95", 3),
            )
        )
    )


def _member(
    name: str,
    center: int,
    reliability: str = "1",
    *,
    provider_kind: ProviderKind | None = None,
    family: str | None = None,
) -> MemberOutcome:
    validated = ValidatedMemberOutput(
        valid=True,
        validator_code="valid_committed",
        distribution=_distribution(center, 1_000),
        evidence_refs=("obs",),
        warnings=(),
        fact_codes=("observed_raw_time",),
        abstention_reason=None,
    )
    return MemberOutcome.valid_for_test(
        member_id=name,
        provider_kind=(
            ProviderKind.CLOUD
            if provider_kind is None and name in {"cloud", "middle"}
            else (provider_kind or ProviderKind.LOCAL)
        ),
        family=family or name,
        evidence_digest="a" * 64,
        validated=validated,
        reliability_weight=reliability,
        context_weight="1",
    )


def test_three_members_normal_and_two_members_explicitly_degraded() -> None:
    members = (
        _member("qwen", 40_000),
        _member("ministral", 41_000),
        _member("cloud", 39_000),
    )
    normal = aggregate_council(members)
    assert normal.availability is CouncilAvailability.NORMAL
    assert normal.valid_member_count == 3
    assert not normal.upstream_approval_required
    degraded = aggregate_council((members[0], members[1], replace(members[2], validated=None)))
    assert degraded.availability is CouncilAvailability.DEGRADED
    assert degraded.valid_member_count == 2
    assert degraded.upstream_approval_required
    unavailable = aggregate_council(
        (
            members[0],
            replace(members[1], validated=None),
            replace(members[2], validated=None),
        )
    )
    assert unavailable.availability is CouncilAvailability.UNAVAILABLE
    assert unavailable.distribution is None


def test_public_aggregation_rejects_unpromoted_and_reconstructed_outcomes() -> None:
    members = (
        _member("qwen", 40_000),
        _member("ministral", 41_000),
        _member("cloud", 39_000),
    )
    with pytest.raises(ValueError, match="promoted council authority"):
        council.aggregate_council(members)
    reconstructed = tuple(replace(item) for item in members)
    with pytest.raises(ValueError, match="promoted council authority"):
        council.aggregate_council(reconstructed)
    forged_first = object.__new__(MemberOutcome)
    for field_name in MemberOutcome.__dataclass_fields__:
        object.__setattr__(forged_first, field_name, getattr(members[0], field_name))
    forged = (forged_first, members[1], members[2])
    with pytest.raises(ValueError, match="promoted council authority"):
        council.aggregate_council(forged)
    with pytest.raises(ValueError, match="promoted council authority"):
        council.aggregate_council((object(), object(), object()))  # type: ignore[arg-type]


def test_context_reliability_weights_move_center_and_dissent_widens_mixture() -> None:
    low = _member("low", 30_000, "4")
    high = _member("high", 50_000, "1")
    middle = _member("middle", 40_000, "1")
    weighted = aggregate_council((low, high, middle))
    balanced = aggregate_council((replace(low, reliability_weight="1"), high, middle))
    assert weighted.distribution is not None
    assert balanced.distribution is not None
    assert weighted.distribution.median_ms < balanced.distribution.median_ms
    low95, high95 = balanced.distribution.central_interval("0.05", "0.95")
    assert high95 - low95 > 6_000


def test_sealed_replay_is_deterministic_and_makes_zero_provider_calls() -> None:
    sealed = aggregate_council(
        (
            _member("qwen", 40_000),
            _member("ministral", 41_000),
            _member("cloud", 39_000),
        )
    )

    def forbidden_provider_call() -> None:
        raise AssertionError("sealed replay called a provider")

    receipt = seal_council_receipt(sealed)
    replayed = replay_sealed_council(receipt, provider_call=forbidden_provider_call)
    assert replayed == sealed

    envelope = json.loads(receipt)
    envelope["receipt_digest"] = "0" * 64
    with pytest.raises(ValueError, match="receipt digest"):
        replay_sealed_council(json.dumps(envelope).encode())


def test_sealed_replay_verifies_attempt_and_storage_reference_identity(tmp_path) -> None:
    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "blobs"))
    digest = sink.publish(b"raw-output")
    members = (
        replace(
            _member("qwen", 40_000),
            attempts=(RawAttempt(digest, "valid_committed", True),),
            storage_references=(sink.references[0],),
        ),
        _member("ministral", 41_000),
        _member("cloud", 39_000),
    )
    sealed = aggregate_council(members)
    receipt = seal_council_receipt(sealed)
    assert replay_sealed_council(receipt) == sealed

    envelope = json.loads(receipt)
    envelope["assessment"]["outcomes"][0]["attempts"][0]["output_digest"] = "0" * 64
    envelope["receipt_digest"] = canonical_digest(envelope["assessment"])
    with pytest.raises(ValueError, match="attempt storage"):
        replay_sealed_council(canonical_bytes(envelope))

    envelope = json.loads(receipt)
    envelope["assessment"]["outcomes"][0]["attempts"].append(
        {"output_digest": "0" * 64, "validator_code": "fabricated", "valid": True}
    )
    with pytest.raises(ValueError, match="receipt digest"):
        replay_sealed_council(json.dumps(envelope).encode())


def test_sealed_receipt_defensive_schema_and_reconstruction_paths(tmp_path) -> None:
    members = (
        _member("qwen", 40_000),
        _member("ministral", 41_000),
        _member("cloud", 39_000),
    )
    assessment = aggregate_council(members)
    receipt = seal_council_receipt(assessment)

    with pytest.raises(ValueError, match="receipt bytes"):
        replay_sealed_council(b"")
    with pytest.raises(ValueError, match="fields"):
        replay_sealed_council(canonical_bytes({"schema_version": "wrong"}))

    def sealed_change(change) -> bytes:
        envelope = json.loads(receipt)
        change(envelope)
        envelope["receipt_digest"] = canonical_digest(envelope["assessment"])
        return canonical_bytes(envelope)

    with pytest.raises(ValueError, match="schema"):
        replay_sealed_council(
            sealed_change(lambda value: value.__setitem__("schema_version", "wrong"))
        )
    noncanonical = json.dumps(json.loads(receipt), indent=2).encode()
    with pytest.raises(ValueError, match="not canonical"):
        replay_sealed_council(noncanonical)
    with pytest.raises(ValueError, match="assessment fields"):
        replay_sealed_council(
            sealed_change(lambda value: value["assessment"].pop("candidate_status"))
        )
    with pytest.raises(ValueError, match="outcomes must be"):
        replay_sealed_council(
            sealed_change(lambda value: value["assessment"].__setitem__("outcomes", {}))
        )
    with pytest.raises(ValueError, match="verification differs"):
        replay_sealed_council(
            sealed_change(lambda value: value["assessment"].__setitem__("availability", "degraded"))
        )
    with pytest.raises(ValueError, match="outcome fields"):
        replay_sealed_council(
            sealed_change(lambda value: value["assessment"]["outcomes"][0].pop("family"))
        )
    with pytest.raises(ValueError, match="validated output fields"):
        replay_sealed_council(
            sealed_change(
                lambda value: value["assessment"]["outcomes"][0]["validated"].pop("warnings")
            )
        )
    with pytest.raises(ValueError, match="attempt audit"):
        replay_sealed_council(
            sealed_change(
                lambda value: value["assessment"]["outcomes"][0].__setitem__("attempts", {})
            )
        )
    with pytest.raises(ValueError, match="attempt fields"):
        replay_sealed_council(
            sealed_change(
                lambda value: value["assessment"]["outcomes"][0].__setitem__("attempts", [{}])
            )
        )

    unavailable = aggregate_council(
        (
            replace(members[0], validated=None),
            replace(members[1], validated=None),
            replace(members[2], validated=None),
        )
    )
    assert replay_sealed_council(seal_council_receipt(unavailable)) == unavailable
    with pytest.raises(ValueError, match="typed council assessment"):
        seal_council_receipt(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reproducible"):
        seal_council_receipt(replace(assessment, valid_member_count=2))
    with pytest.raises(ValueError, match="serializable"):
        seal_council_receipt(
            aggregate_council((replace(members[0], storage_references=(object(),)), *members[1:]))
        )

    sink = ContentAddressedRawOutputSink(ContentAddressedBlobStore(tmp_path / "audit"))
    digest = sink.publish(b"raw")
    outcome = replace(
        members[0],
        attempts=(RawAttempt(digest, "valid_committed", True),),
        storage_references=(sink.references[0],),
    )
    with_audit = aggregate_council((outcome, *members[1:]))
    audit = LLMMemberAudit(
        "1" * 64,
        "schema:v1",
        "runtime:v1",
        "2" * 64,
        "Q4",
        "3" * 64,
        "4" * 64,
        "valid_committed",
        1,
        "model:v1",
        None,
        None,
        None,
    )
    audit_receipt = json.loads(seal_council_receipt(with_audit))
    audit_receipt["assessment"]["outcomes"][0]["audit"] = audit.to_dict()
    audit_receipt["receipt_digest"] = canonical_digest(audit_receipt["assessment"])
    with pytest.raises(ValueError, match="audit response"):
        replay_sealed_council(canonical_bytes(audit_receipt))


def test_candidate_specs_are_exact_unpromoted_and_reject_aliases() -> None:
    candidates = initial_local_candidates()
    assert [item.family for item in candidates] == ["qwen3.5", "ministral3"]
    assert all(item.status is CandidateStatus.CANDIDATE for item in candidates)
    assert all(item.quantization == "Q4_K_M" for item in candidates)
    assert (
        candidates[0].model_digest
        == "6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7"
    )
    assert (
        candidates[1].model_digest
        == "1922accd5827ebe6829e536369195db25eaf664528dc66206d646ea3bb386b71"
    )
    cloud = configured_cloud_candidate(
        provider_id="frontier_api",
        family="frontier_family",
        model_id="frontier-2026-08-01",
        model_digest="4" * 64,
        runtime_version="api:2026-08",
        runtime_digest="5" * 64,
        sampling_parameters={"seed": 7, "temperature": "0"},
    )
    assert cloud.provider_kind.value == "cloud"
    assert cloud.status is CandidateStatus.CANDIDATE
    base = candidates[0]
    invalid = [
        {"provider_kind": "local"},
        {"status": "candidate"},
        {"model_id": "latest"},
        {"quantization": ""},
        {"sampling_parameters": []},
        {"sampling_parameters_digest": "0" * 64},
        {"model_digest": "bad"},
        {"member_id": "BAD"},
    ]
    for change in invalid:
        with pytest.raises(ValueError):
            replace(base, **change)
    with pytest.raises(ValueError, match="secret"):
        HMACTokenKey("key:test", b"short")
    key = HMACTokenKey("key:test", b"z" * 32)
    assert "zzzz" not in repr(key)


def test_candidate_promotion_requires_two_equal_signed_rotation_receipts() -> None:
    candidate = initial_local_candidates()[0]
    signer = P256EphemeralSigner.generate("integrity-key:candidate-harness")
    trust = IntegrityTrustStore((signer.identity,))
    authority = EphemeralTestCandidateEvaluationAuthority(trust)
    distribution = _distribution(40_000, 1_000).to_dict()

    def receipt(
        rotation: str,
        *,
        center: int = 40_000,
        kind: str = "llm_candidate_rotation_result",
        remove: str | None = None,
        **changes,
    ):
        value = {
            "schema_version": "strathmark-v3-candidate-rotation-receipt-v1",
            "harness": "u19_candidate_harness",
            "candidate_manifest_digest": council._member_manifest_digest(candidate),
            "provider_id": candidate.provider_id,
            "member_id": candidate.member_id,
            "model_id": candidate.model_id,
            "model_digest": candidate.model_digest,
            "runtime_version": candidate.runtime_version,
            "runtime_digest": candidate.runtime_digest,
            "rotation_id": rotation,
            "token_key_id": f"key:{rotation}",
            "numeric_packet_digest": "8" * 64,
            "provider_execution_digest": ("6" if rotation == "old" else "7") * 64,
            "distribution": (
                distribution if center == 40_000 else _distribution(center, 1_000).to_dict()
            ),
        }
        value.update(changes)
        if remove is not None:
            value.pop(remove)
        return sign_manifest(
            kind,
            value,
            signer=signer,
            created_at="2026-08-23T10:00:00.000Z",
        )

    with pytest.raises(CandidatePromotionError, match="two"):
        evaluate_candidate_rotation_receipts(candidate, None, None, authority)  # type: ignore[arg-type]
    with pytest.raises(CandidatePromotionError, match="metadata"):
        evaluate_candidate_rotation_receipts(
            candidate, receipt("old", model_digest="0" * 64), receipt("new"), authority
        )
    with pytest.raises(CandidatePromotionError, match="distribution"):
        evaluate_candidate_rotation_receipts(
            candidate, receipt("old"), receipt("new", center=41_000), authority
        )
    for old, new, message in (
        (receipt("old", kind="wrong_kind"), receipt("new"), "metadata"),
        (receipt("old", remove="harness"), receipt("new"), "metadata"),
        (receipt("old", numeric_packet_digest="bad"), receipt("new"), "metadata"),
        (receipt("old", rotation_id=1), receipt("new"), "metadata"),
        (receipt("old", distribution={}), receipt("new"), "distribution"),
        (receipt("same"), receipt("same"), "distinct"),
        (
            receipt("old"),
            receipt("new", numeric_packet_digest="9" * 64),
            "numeric",
        ),
    ):
        with pytest.raises(CandidatePromotionError, match=message):
            evaluate_candidate_rotation_receipts(candidate, old, new, authority)
    with pytest.raises(CandidatePromotionError, match="test-only authority"):
        evaluate_candidate_rotation_receipts(candidate, receipt("old"), receipt("new"), object())
    untrusted = P256EphemeralSigner.generate("integrity-key:untrusted-harness")
    with pytest.raises(CandidatePromotionError, match="signature"):
        evaluate_candidate_rotation_receipts(
            candidate,
            receipt("old"),
            receipt("new"),
            EphemeralTestCandidateEvaluationAuthority(IntegrityTrustStore((untrusted.identity,))),
        )

    evaluation = evaluate_candidate_rotation_receipts(
        candidate, receipt("old"), receipt("new"), authority
    )
    assert candidate.status is CandidateStatus.CANDIDATE
    assert evaluation.authority_class == "test_ephemeral"
    assert evaluation.receipt_digest == canonical_digest(
        {
            "candidate_manifest_digest": council._member_manifest_digest(candidate),
            "rotation_receipt_digests": [receipt("old").body_digest, receipt("new").body_digest],
        }
    )
    forged = replace(candidate)
    object.__setattr__(forged, "status", CandidateStatus.PROMOTED)
    with pytest.raises(CandidatePromotionError, match="unpromoted"):
        evaluate_candidate_rotation_receipts(
            forged,
            receipt("old"),
            receipt("new"),
            authority,
        )
    with pytest.raises(CandidatePromotionError, match="sealed gate"):
        council.VerifiedCandidateEvaluation("0" * 64, "1" * 64, "test_ephemeral")
    with pytest.raises(CandidatePromotionError, match="typed trust store"):
        EphemeralTestCandidateEvaluationAuthority(object())
    report_args = (
        CouncilAvailability.NORMAL,
        0,
        None,
        (),
        (),
        ((candidate.member_id, evaluation),),
    )
    with pytest.raises(ValueError, match="authority"):
        CandidateEvaluationReport("installed", CandidateStatus.CANDIDATE, *report_args)
    with pytest.raises(ValueError, match="unpromoted"):
        CandidateEvaluationReport("test_ephemeral", CandidateStatus.PROMOTED, *report_args)
    with pytest.raises(ValueError, match="sealed member receipts"):
        CandidateEvaluationReport(
            "test_ephemeral",
            CandidateStatus.CANDIDATE,
            CouncilAvailability.NORMAL,
            0,
            None,
            (),
            (),
            (),
        )


def test_candidate_cannot_claim_promoted_status_without_sealed_gate() -> None:
    with pytest.raises(ValueError, match="unavailable until U19"):
        replace(initial_local_candidates()[0], status=CandidateStatus.PROMOTED)
    with pytest.raises(ValueError, match="cannot carry"):
        replace(initial_local_candidates()[0], promotion=object())
    with pytest.raises(ValueError, match="provider error"):
        ProviderCallError("BAD")


def test_deadlines_sealed_jobs_and_memory_sink_are_closed_contracts() -> None:
    for arguments in ((0, 1, 1, 1, 1), (2, 1, 1, 1, 1)):
        with pytest.raises(ValueError, match="deadline"):
            DeadlineBudget(*arguments)
    member = initial_local_candidates()[0]
    with pytest.raises(ValueError, match="persisted fenced lease"):
        SealedLLMJob("job:sealed", True, "a" * 64)
    sink = MemoryRawOutputSink()
    with pytest.raises(ValueError, match="bytes"):
        sink.publish("not bytes")  # type: ignore[arg-type]


def test_aggregation_rejects_duplicates_mixed_evidence_and_bad_weights() -> None:
    first = _member("one", 40_000)
    second = _member("two", 41_000)
    with pytest.raises(ValueError, match="outcomes"):
        aggregate_council(())
    with pytest.raises(ValueError, match="unique"):
        aggregate_council((first, first, _member("cloud", 39_000)))
    with pytest.raises(ValueError, match="evidence"):
        aggregate_council(
            (first, replace(second, evidence_digest="b" * 64), _member("cloud", 39_000))
        )
    for bad in ("0", "-1", "nan", "not-a-number"):
        with pytest.raises(ValueError, match="weights"):
            aggregate_council(
                (
                    replace(first, reliability_weight=bad),
                    second,
                    _member("cloud", 39_000),
                )
            )
    with pytest.raises(ValueError, match="sealed replay"):
        replay_sealed_council(b"bad")
    with pytest.raises(ValueError, match="two distinct local"):
        aggregate_council(
            (
                _member("one", 40_000),
                _member("two", 41_000),
                _member("three", 42_000),
            )
        )


def test_mixture_guards_and_equal_time_cdf_are_deterministic() -> None:
    distribution = _distribution(40_000, 0)
    assert council._distribution_cdf(distribution, 39_999) == Decimal(0)
    assert council._distribution_cdf(distribution, 40_000) == Decimal(1)
    with pytest.raises(ValueError, match="mixture"):
        council._mixture_distribution((distribution,), (Decimal(1),))
    with pytest.raises(ValueError, match="mixture"):
        council._mixture_distribution((distribution, None), (Decimal("0.5"), Decimal("0.5")))
    assert council._decimal_string(Decimal(0)) == "0"
    plateau = PositiveTimeDistribution(
        tuple(
            QuantilePoint(probability, time_ms)
            for probability, time_ms in (
                ("0.05", 30_000),
                ("0.1", 30_000),
                ("0.25", 35_000),
                ("0.5", 40_000),
                ("0.75", 45_000),
                ("0.9", 50_000),
                ("0.95", 55_000),
            )
        )
    )
    assert council._distribution_cdf(plateau, 30_000) == Decimal("0.1")


def test_nonvalidated_member_is_unavailable_not_an_implicit_default() -> None:
    outcome = MemberOutcome(
        "failed",
        ProviderKind.LOCAL,
        "qwen3.5",
        "a" * 64,
        None,
        "1",
        "1",
        (RawAttempt("d" * 64, "invalid_json", False),),
        None,
        (),
        "invalid_output_after_correction",
    )
    assert outcome.valid_distribution is None
    other = replace(outcome, member_id="failed2", family="ministral3")
    cloud = replace(
        outcome,
        member_id="failed3",
        provider_kind=ProviderKind.CLOUD,
        family="frontier",
    )
    result = aggregate_council((outcome, other, cloud))
    assert result.availability is CouncilAvailability.UNAVAILABLE
    assert result.distribution is None
    assert result.candidate_status is CandidateStatus.CANDIDATE
