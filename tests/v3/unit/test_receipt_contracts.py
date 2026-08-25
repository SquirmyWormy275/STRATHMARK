from __future__ import annotations

from dataclasses import replace

import pytest

import strathmark.v3.contracts.receipts as receipt_contracts
from strathmark.v3.contracts.commands import (
    MAX_INLINE_PAYLOAD_BYTES,
    BlobReference,
    BlobReferenceV2,
    BlobRetentionClass,
    InlinePayload,
)
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import ContextProperty, TargetContext
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.contracts.receipts import (
    BundleIdentity,
    FieldReceipt,
    MarkAssignment,
    PacketIdentity,
    ReceiptSection,
    ReceiptSectionKind,
)


def _sections() -> tuple[ReceiptSection, ...]:
    sections = []
    for kind in ReceiptSectionKind:
        sections.append(ReceiptSection(kind, InlinePayload.from_value({"kind": kind.value})))
    return tuple(sections)


def _target_context() -> TargetContext:
    return TargetContext(
        event_code="underhand",
        size_mm=300,
        material_code="eucalyptus",
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        properties=(ContextProperty("density", "720.5", "kg_m3", None),),
    )


def _receipt() -> FieldReceipt:
    return FieldReceipt.create(
        caller_namespace="tournament-manager",
        request_identity=IdempotencyKey("request:field-final-v1"),
        field_id=StableIdentifier("field:grand-final"),
        upstream_field_revision=1,
        receipt_revision=1,
        supersedes_receipt_id=None,
        ordered_competitor_ids=(
            StableIdentifier("competitor:opaque-a"),
            StableIdentifier("competitor:opaque-b"),
        ),
        target_context=_target_context(),
        target_context_digest=_target_context().digest,
        historical_cutoff_key="history:2026-08-01",
        tournament_epoch_id=StableIdentifier("epoch:grand-final-v1"),
        tournament_event_sequence=41,
        packet_identities=(
            PacketIdentity(StableIdentifier("competitor:opaque-a"), "b" * 64),
            PacketIdentity(StableIdentifier("competitor:opaque-b"), "c" * 64),
        ),
        sections=_sections(),
        marks=(
            MarkAssignment(StableIdentifier("competitor:opaque-a"), 3),
            MarkAssignment(StableIdentifier("competitor:opaque-b"), 18),
        ),
        warning_codes=("sparse_evidence",),
        total_latency_ms=1832,
        bundles=(BundleIdentity("complete_bundle", "bundle:v3.0.0", "d" * 64),),
    )


def test_atomic_field_receipt_is_content_addressed_and_round_trips() -> None:
    receipt = _receipt()
    assert receipt.receipt_id.namespace == "receipt"
    assert FieldReceipt.from_dict(receipt.to_dict()) == receipt
    assert receipt.content_digest == receipt.recompute_content_digest()
    assert receipt.receipt_id == receipt.recompute_receipt_id()
    assert receipt.target_context.event_code == "underhand"
    assert receipt.target_context_digest == receipt.target_context.digest


def test_fresh_receipt_creation_reuses_its_same_call_content_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _receipt().creation_arguments()

    def reject_recomputation(_receipt: FieldReceipt) -> str:
        raise AssertionError("fresh receipt content was recomputed")

    monkeypatch.setattr(FieldReceipt, "recompute_content_digest", reject_recomputation)
    receipt = FieldReceipt.create(**arguments)

    assert receipt.canonical_payload == receipt_contracts.canonical_bytes(
        receipt.to_dict(), max_bytes=receipt_contracts.MAX_RECEIPT_CANONICAL_BYTES
    )
    with pytest.raises(AssertionError, match="recomputed"):
        FieldReceipt.from_dict(receipt.to_dict())


def test_receipt_section_round_trips_authoritative_v2_blob_and_rejects_nonobject() -> None:
    digest = "e" * 64
    reference = BlobReferenceV2(
        deterministic_identifier("blob", {"digest": digest}),
        digest,
        MAX_INLINE_PAYLOAD_BYTES + 1,
        "application/json",
        "strathmark-v3-receipt-audit-v1",
        BlobRetentionClass.ISSUED_RECEIPT,
    )
    section = ReceiptSection(ReceiptSectionKind.COMPONENT_OUTPUTS, reference)
    assert ReceiptSection.from_dict(section.to_dict()) == section
    value = section.to_dict()
    value["payload"] = []
    with pytest.raises(ContractError, match="blob payload must be an object"):
        ReceiptSection.from_dict(value)


def test_receipt_rejects_mismatched_or_tampered_target_context() -> None:
    receipt = _receipt()
    arguments = receipt.creation_arguments()
    arguments["target_context_digest"] = "f" * 64
    with pytest.raises(ContractError, match="target context digest"):
        FieldReceipt.create(**arguments)

    value = receipt.to_dict()
    value["target_context"]["event_code"] = "standing_block"
    with pytest.raises(ContractError, match="target context digest"):
        FieldReceipt.from_dict(value)


def test_caller_identity_is_part_of_receipt_identity_not_numeric_content() -> None:
    first = _receipt()
    second = FieldReceipt.create(
        **{
            **first.creation_arguments(),
            "request_identity": IdempotencyKey("request:field-final-retry-other"),
        }
    )
    assert first.content_digest == second.content_digest
    assert first.receipt_id != second.receipt_id


def test_receipt_requires_every_atomic_audit_section_once() -> None:
    receipt = _receipt()
    arguments = receipt.creation_arguments()
    arguments["sections"] = receipt.sections[:-1]
    with pytest.raises(ContractError, match="sections"):
        FieldReceipt.create(**arguments)

    arguments = receipt.creation_arguments()
    arguments["sections"] = receipt.sections + (receipt.sections[0],)
    with pytest.raises(ContractError, match="sections"):
        FieldReceipt.create(**arguments)


def test_ordered_roster_packet_and_marks_must_match_exactly() -> None:
    receipt = _receipt()
    arguments = receipt.creation_arguments()
    arguments["marks"] = tuple(reversed(receipt.marks))
    with pytest.raises(ContractError, match="ordered roster"):
        FieldReceipt.create(**arguments)

    arguments = receipt.creation_arguments()
    arguments["packet_identities"] = receipt.packet_identities[:1]
    with pytest.raises(ContractError, match="packet"):
        FieldReceipt.create(**arguments)


def test_receipt_revision_requires_explicit_supersession() -> None:
    receipt = _receipt()
    arguments = receipt.creation_arguments()
    arguments["receipt_revision"] = 2
    with pytest.raises(ContractError, match="supersedes"):
        FieldReceipt.create(**arguments)

    arguments = receipt.creation_arguments()
    arguments["supersedes_receipt_id"] = StableIdentifier("receipt:old")
    with pytest.raises(ContractError, match="revision 1"):
        FieldReceipt.create(**arguments)


def test_large_receipt_sections_must_be_blob_references() -> None:
    reference = BlobReference(
        StableIdentifier("blob:member-output"),
        "e" * 64,
        MAX_INLINE_PAYLOAD_BYTES + 1,
        "application/json",
    )
    section = ReceiptSection(ReceiptSectionKind.MEMBER_OUTPUTS, reference)
    assert ReceiptSection.from_dict(section.to_dict()) == section


def test_receipt_leaf_contracts_and_section_discriminators_fail_closed() -> None:
    with pytest.raises(ContractError, match="3..183"):
        MarkAssignment(StableIdentifier("competitor:opaque-a"), 2)
    with pytest.raises(ContractError, match="nonempty"):
        BundleIdentity("", "bundle:v1", "a" * 64)
    section = _sections()[0]
    with pytest.raises(ContractError, match="ReceiptSectionKind"):
        replace(section, kind="component_outputs")  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="inline or"):
        replace(section, payload=object())

    encoded = section.to_dict()
    encoded["kind"] = "invented"
    with pytest.raises(ContractError, match="unknown receipt section"):
        ReceiptSection.from_dict(encoded)
    encoded = section.to_dict()
    encoded["payload_type"] = "invented"
    with pytest.raises(ContractError, match="unknown receipt section payload"):
        ReceiptSection.from_dict(encoded)


def test_field_receipt_constructor_closes_every_collection_and_identity_boundary() -> None:
    receipt = _receipt()
    for changes, message in (
        ({"caller_namespace": "Bad Caller"}, "caller_namespace"),
        (
            {"request_identity": StableIdentifier("request:not-idempotency")},
            "IdempotencyKey",
        ),
        ({"ordered_competitor_ids": []}, "ordered roster"),
        (
            {
                "ordered_competitor_ids": (
                    StableIdentifier("competitor:opaque-a"),
                    StableIdentifier("competitor:opaque-a"),
                )
            },
            "duplicate",
        ),
        ({"target_context": object()}, "TargetContext"),
        ({"packet_identities": list(receipt.packet_identities)}, "packet identities"),
        ({"marks": list(receipt.marks)}, "marks"),
        ({"sections": list(receipt.sections)}, "sections"),
        ({"warning_codes": ["sparse_evidence"]}, "immutable tuple"),
        ({"warning_codes": ("Bad Warning",)}, "lower-case"),
        (
            {"warning_codes": ("sparse_evidence", "sparse_evidence")},
            "unique and sorted",
        ),
        ({"bundles": ()}, "at least one"),
        (
            {
                "bundles": (
                    receipt.bundles[0],
                    BundleIdentity("complete_bundle", "bundle:v3.0.1", "e" * 64),
                )
            },
            "unique sorted roles",
        ),
    ):
        with pytest.raises(ContractError, match=message):
            replace(receipt, **changes)


def test_superseding_receipt_and_decoder_array_boundaries_round_trip() -> None:
    first = _receipt()
    arguments = first.creation_arguments()
    arguments["receipt_revision"] = 2
    arguments["supersedes_receipt_id"] = first.receipt_id
    second = FieldReceipt.create(**arguments)
    assert FieldReceipt.from_dict(second.to_dict()) == second

    for label in (
        "ordered_competitor_ids",
        "packet_identities",
        "sections",
        "marks",
        "warning_codes",
        "bundles",
    ):
        encoded = first.to_dict()
        encoded[label] = "not-an-array"
        with pytest.raises(ContractError, match=f"{label} must be a JSON array"):
            FieldReceipt.from_dict(encoded)


def test_receipt_maximum_canonical_size_is_checked_before_storage(monkeypatch) -> None:
    monkeypatch.setattr(receipt_contracts, "MAX_RECEIPT_CANONICAL_BYTES", 256)
    with pytest.raises(ContractError, match="maximum canonical size"):
        _receipt()


@pytest.mark.parametrize("mutation", ["missing", "extra", "schema", "digest", "id"])
def test_receipt_decoder_fails_closed_before_storage(mutation: str) -> None:
    receipt = _receipt()
    value = receipt.to_dict()
    if mutation == "missing":
        del value["marks"]
    elif mutation == "extra":
        value["explanation"] = "not part of numeric core"
    elif mutation == "schema":
        value["schema_version"] = "strathmark-v3-field-receipt-v999"
    elif mutation == "digest":
        value["content_digest"] = "f" * 64
    else:
        value["receipt_id"] = "receipt:not-content-addressed"
    with pytest.raises(ContractError):
        FieldReceipt.from_dict(value)
