from __future__ import annotations

from decimal import Decimal

import pytest

from strathmark.v3.assessors.llm_council import (
    seal_member_weight_authority,
    verify_member_weight_authority,
)
from strathmark.v3.domain.credibility import (
    MemberCredibilityEvidence,
    derive_member_subweights,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
)

NOW = "2026-08-25T10:00:00.000Z"


def _receipt():
    return derive_member_subweights(
        members=(
            MemberCredibilityEvidence("qwen", "0.2", "0.1", "20", "1"),
            MemberCredibilityEvidence("ministral", "0.3", "0.2", "20", "1"),
            MemberCredibilityEvidence("cloud", "0.4", "0.3", "20", "1"),
        ),
        council_outer_weight="0.3",
        credibility_ledger_digest="a" * 64,
        credibility_policy_digest="b" * 64,
        context_digest="c" * 64,
        calibration_cutoff_at_utc=NOW,
    )


def test_member_weights_are_accuracy_earned_context_sensitive_and_sum_outer_weight() -> None:
    receipt = _receipt()
    weights = dict(receipt.member_subweights)
    assert sum(Decimal(value) for value in weights.values()) == Decimal("0.3")
    assert weights["qwen"] > weights["ministral"] > weights["cloud"]
    assert tuple(member for member, _weight in receipt.member_subweights) == (
        "cloud",
        "ministral",
        "qwen",
    )


def test_signed_member_weight_authority_binds_roster_bundle_evidence_and_exact_receipt() -> None:
    signer = P256EphemeralSigner.generate("integrity-key:member-weights")
    trust = IntegrityTrustStore((signer.identity,))
    receipt = _receipt()
    authority = seal_member_weight_authority(
        receipt,
        member_ids=("cloud", "ministral", "qwen"),
        evidence_digest="d" * 64,
        bundle_digest="e" * 64,
        council_component_digest="f" * 64,
        signer=signer,
        created_at=NOW,
    )

    verified = verify_member_weight_authority(
        authority,
        trust_store=trust,
        expected_member_ids=("cloud", "ministral", "qwen"),
        expected_evidence_digest="d" * 64,
        expected_bundle_digest="e" * 64,
        expected_council_component_digest="f" * 64,
    )
    assert verified == receipt

    object.__setattr__(authority.manifest, "signature_der_b64", "A" * 88)
    with pytest.raises(ValueError, match="weight authority"):
        verify_member_weight_authority(
            authority,
            trust_store=trust,
            expected_member_ids=("cloud", "ministral", "qwen"),
            expected_evidence_digest="d" * 64,
            expected_bundle_digest="e" * 64,
            expected_council_component_digest="f" * 64,
        )
