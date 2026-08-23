"""Atomic content-addressed field receipt contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import BlobReference, InlinePayload, PayloadReference
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import (
    TargetContext,
    _require_digest,
    _require_id,
    _require_version,
)
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    require_idempotency_key,
    require_identifier,
)
from strathmark.v3.contracts.statuses import (
    _require_fields,
    _require_nonnegative_int,
    _require_positive_int,
    _require_schema,
)

FIELD_RECEIPT_SCHEMA_VERSION = "strathmark-v3-field-receipt-v1"
RECEIPT_SECTION_SCHEMA_VERSION = "strathmark-v3-receipt-section-v1"
MAX_RECEIPT_CANONICAL_BYTES = 1_048_576
_CALLER_NAMESPACE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_WARNING = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ReceiptSectionKind(str, Enum):
    COMPONENT_OUTPUTS = "component_outputs"
    MEMBER_OUTPUTS = "member_outputs"
    VALIDATIONS = "validations"
    CAPABILITY_ADJUSTMENTS = "capability_adjustments"
    CREDIBILITY = "credibility"
    POOLED_DISTRIBUTION = "pooled_distribution"
    DISAGREEMENT = "disagreement"
    OPTIMIZER_FRONTIER = "optimizer_frontier"
    LATENCY_DETAIL = "latency_detail"


@dataclass(frozen=True, slots=True, order=True)
class PacketIdentity:
    competitor_id: StableIdentifier
    packet_digest: str

    def __post_init__(self) -> None:
        _require_id(self.competitor_id, "competitor")
        _require_digest(self.packet_digest, "packet_digest")

    def to_dict(self) -> dict[str, str]:
        return {"competitor_id": str(self.competitor_id), "packet_digest": self.packet_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PacketIdentity:
        _require_fields(value, {"competitor_id", "packet_digest"})
        return cls(
            require_identifier(value["competitor_id"], expected_namespace="competitor"),
            value["packet_digest"],
        )


@dataclass(frozen=True, slots=True, order=True)
class MarkAssignment:
    competitor_id: StableIdentifier
    mark: int

    def __post_init__(self) -> None:
        _require_id(self.competitor_id, "competitor")
        _require_positive_int(self.mark, "mark")
        if not 3 <= self.mark <= 183:
            raise ContractError("mark must be inside the V3 system boundary 3..183")

    def to_dict(self) -> dict[str, Any]:
        return {"competitor_id": str(self.competitor_id), "mark": self.mark}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MarkAssignment:
        _require_fields(value, {"competitor_id", "mark"})
        return cls(
            require_identifier(value["competitor_id"], expected_namespace="competitor"),
            value["mark"],
        )


@dataclass(frozen=True, slots=True, order=True)
class BundleIdentity:
    role: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ContractError("bundle role must be a nonempty string")
        _require_version(self.version, "bundle version")
        _require_digest(self.digest, "bundle digest")

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "version": self.version, "digest": self.digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BundleIdentity:
        _require_fields(value, {"role", "version", "digest"})
        return cls(value["role"], value["version"], value["digest"])


@dataclass(frozen=True, slots=True)
class ReceiptSection:
    kind: ReceiptSectionKind
    payload: PayloadReference
    schema_version: str = RECEIPT_SECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, RECEIPT_SECTION_SCHEMA_VERSION)
        if not isinstance(self.kind, ReceiptSectionKind):
            raise ContractError("receipt section kind must be a ReceiptSectionKind value")
        if not isinstance(self.payload, (InlinePayload, BlobReference)):
            raise ContractError("receipt section payload must be inline or a blob reference")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "payload_type": "inline" if isinstance(self.payload, InlinePayload) else "blob",
            "payload": self.payload.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReceiptSection:
        _require_fields(value, {"schema_version", "kind", "payload_type", "payload"})
        _require_schema(value["schema_version"], RECEIPT_SECTION_SCHEMA_VERSION)
        try:
            kind = ReceiptSectionKind(value["kind"])
        except (TypeError, ValueError) as exc:
            raise ContractError("unknown receipt section kind") from exc
        if value["payload_type"] == "inline":
            payload: PayloadReference = InlinePayload.from_dict(value["payload"])
        elif value["payload_type"] == "blob":
            payload = BlobReference.from_dict(value["payload"])
        else:
            raise ContractError("unknown receipt section payload type")
        return cls(kind, payload)


@dataclass(frozen=True, slots=True)
class FieldReceipt:
    receipt_id: StableIdentifier
    caller_namespace: str
    request_identity: IdempotencyKey
    field_id: StableIdentifier
    field_revision: int
    supersedes_receipt_id: StableIdentifier | None
    ordered_competitor_ids: tuple[StableIdentifier, ...]
    target_context: TargetContext
    target_context_digest: str
    historical_cutoff_key: str
    tournament_epoch_id: StableIdentifier
    tournament_event_sequence: int
    packet_identities: tuple[PacketIdentity, ...]
    sections: tuple[ReceiptSection, ...]
    marks: tuple[MarkAssignment, ...]
    warning_codes: tuple[str, ...]
    total_latency_ms: int
    bundles: tuple[BundleIdentity, ...]
    content_digest: str
    schema_version: str = FIELD_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, FIELD_RECEIPT_SCHEMA_VERSION)
        _require_id(self.receipt_id, "receipt")
        if not isinstance(self.caller_namespace, str) or not _CALLER_NAMESPACE.fullmatch(
            self.caller_namespace
        ):
            raise ContractError("caller_namespace must be a bounded lower-case token")
        if not isinstance(self.request_identity, IdempotencyKey):
            raise ContractError("request_identity must be an IdempotencyKey")
        _require_id(self.field_id, "field")
        _require_positive_int(self.field_revision, "field_revision")
        if self.field_revision == 1 and self.supersedes_receipt_id is not None:
            raise ContractError("field revision 1 cannot supersede a receipt")
        if self.field_revision > 1 and self.supersedes_receipt_id is None:
            raise ContractError("later field revisions require supersedes_receipt_id")
        if self.supersedes_receipt_id is not None:
            _require_id(self.supersedes_receipt_id, "receipt")
        if not isinstance(self.ordered_competitor_ids, tuple) or not self.ordered_competitor_ids:
            raise ContractError("ordered roster must be a nonempty immutable tuple")
        for identifier in self.ordered_competitor_ids:
            _require_id(identifier, "competitor")
        if len(set(self.ordered_competitor_ids)) != len(self.ordered_competitor_ids):
            raise ContractError("ordered roster cannot contain duplicate competitors")
        if not isinstance(self.target_context, TargetContext):
            raise ContractError("target_context must be a TargetContext")
        _require_digest(self.target_context_digest, "target_context_digest")
        if self.target_context_digest != self.target_context.digest:
            raise ContractError("target context digest does not match the embedded context")
        require_identifier(self.historical_cutoff_key, expected_namespace="history")
        _require_id(self.tournament_epoch_id, "epoch")
        _require_nonnegative_int(self.tournament_event_sequence, "tournament_event_sequence")

        roster = self.ordered_competitor_ids
        if (
            not isinstance(self.packet_identities, tuple)
            or tuple(item.competitor_id for item in self.packet_identities) != roster
        ):
            raise ContractError("packet identities must match the ordered roster exactly")
        if (
            not isinstance(self.marks, tuple)
            or tuple(item.competitor_id for item in self.marks) != roster
        ):
            raise ContractError("marks must match the ordered roster exactly")

        required_sections = tuple(ReceiptSectionKind)
        if (
            not isinstance(self.sections, tuple)
            or tuple(item.kind for item in self.sections) != required_sections
        ):
            raise ContractError("receipt sections must contain every required section exactly once")
        if not isinstance(self.warning_codes, tuple):
            raise ContractError("warning_codes must be an immutable tuple")
        if any(
            not isinstance(item, str) or not _WARNING.fullmatch(item) for item in self.warning_codes
        ):
            raise ContractError("warning_codes must contain bounded lower-case codes")
        if self.warning_codes != tuple(sorted(set(self.warning_codes))):
            raise ContractError("warning_codes must be unique and sorted")
        _require_nonnegative_int(self.total_latency_ms, "total_latency_ms")
        if not isinstance(self.bundles, tuple) or not self.bundles:
            raise ContractError("receipt must identify at least one immutable bundle")
        roles = tuple(item.role for item in self.bundles)
        if roles != tuple(sorted(roles)) or len(roles) != len(set(roles)):
            raise ContractError("bundle identities must have unique sorted roles")

        _require_digest(self.content_digest, "content_digest")
        if self.content_digest != self.recompute_content_digest():
            raise ContractError("receipt content digest mismatch")
        if self.receipt_id != self.recompute_receipt_id():
            raise ContractError("receipt identity is not bound to content and caller request")
        try:
            canonical_bytes(self.to_dict(), max_bytes=MAX_RECEIPT_CANONICAL_BYTES)
        except Exception as exc:
            raise ContractError("receipt exceeds the maximum canonical size") from exc

    @classmethod
    def create(
        cls,
        *,
        caller_namespace: str,
        request_identity: IdempotencyKey,
        field_id: StableIdentifier,
        field_revision: int,
        supersedes_receipt_id: StableIdentifier | None,
        ordered_competitor_ids: tuple[StableIdentifier, ...],
        target_context: TargetContext,
        target_context_digest: str,
        historical_cutoff_key: str,
        tournament_epoch_id: StableIdentifier,
        tournament_event_sequence: int,
        packet_identities: tuple[PacketIdentity, ...],
        sections: tuple[ReceiptSection, ...],
        marks: tuple[MarkAssignment, ...],
        warning_codes: tuple[str, ...],
        total_latency_ms: int,
        bundles: tuple[BundleIdentity, ...],
    ) -> FieldReceipt:
        arguments = locals().copy()
        arguments.pop("cls")
        content_digest = canonical_digest(_receipt_content_value(**arguments))
        receipt_id = deterministic_identifier(
            "receipt",
            {
                "caller_namespace": caller_namespace,
                "request_identity": str(request_identity),
                "content_digest": content_digest,
            },
        )
        return cls(receipt_id=receipt_id, content_digest=content_digest, **arguments)

    def creation_arguments(self) -> dict[str, Any]:
        return {
            "caller_namespace": self.caller_namespace,
            "request_identity": self.request_identity,
            "field_id": self.field_id,
            "field_revision": self.field_revision,
            "supersedes_receipt_id": self.supersedes_receipt_id,
            "ordered_competitor_ids": self.ordered_competitor_ids,
            "target_context": self.target_context,
            "target_context_digest": self.target_context_digest,
            "historical_cutoff_key": self.historical_cutoff_key,
            "tournament_epoch_id": self.tournament_epoch_id,
            "tournament_event_sequence": self.tournament_event_sequence,
            "packet_identities": self.packet_identities,
            "sections": self.sections,
            "marks": self.marks,
            "warning_codes": self.warning_codes,
            "total_latency_ms": self.total_latency_ms,
            "bundles": self.bundles,
        }

    def recompute_content_digest(self) -> str:
        return canonical_digest(_receipt_content_value(**self.creation_arguments()))

    def recompute_receipt_id(self) -> StableIdentifier:
        return deterministic_identifier(
            "receipt",
            {
                "caller_namespace": self.caller_namespace,
                "request_identity": str(self.request_identity),
                "content_digest": self.content_digest,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": str(self.receipt_id),
            "caller_namespace": self.caller_namespace,
            "request_identity": str(self.request_identity),
            **_receipt_content_value(**self.creation_arguments()),
            "content_digest": self.content_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FieldReceipt:
        expected = {
            "schema_version",
            "receipt_id",
            "caller_namespace",
            "request_identity",
            "field_id",
            "field_revision",
            "supersedes_receipt_id",
            "ordered_competitor_ids",
            "target_context",
            "target_context_digest",
            "historical_cutoff_key",
            "tournament_epoch_id",
            "tournament_event_sequence",
            "packet_identities",
            "sections",
            "marks",
            "warning_codes",
            "total_latency_ms",
            "bundles",
            "content_digest",
        }
        _require_fields(value, expected)
        _require_schema(value["schema_version"], FIELD_RECEIPT_SCHEMA_VERSION)
        for label in (
            "ordered_competitor_ids",
            "packet_identities",
            "sections",
            "marks",
            "warning_codes",
            "bundles",
        ):
            if not isinstance(value[label], list):
                raise ContractError(f"{label} must be a JSON array")
        supersedes = value["supersedes_receipt_id"]
        return cls(
            receipt_id=require_identifier(value["receipt_id"], expected_namespace="receipt"),
            caller_namespace=value["caller_namespace"],
            request_identity=require_idempotency_key(value["request_identity"]),
            field_id=require_identifier(value["field_id"], expected_namespace="field"),
            field_revision=value["field_revision"],
            supersedes_receipt_id=(
                None
                if supersedes is None
                else require_identifier(supersedes, expected_namespace="receipt")
            ),
            ordered_competitor_ids=tuple(
                require_identifier(item, expected_namespace="competitor")
                for item in value["ordered_competitor_ids"]
            ),
            target_context=TargetContext.from_dict(value["target_context"]),
            target_context_digest=value["target_context_digest"],
            historical_cutoff_key=value["historical_cutoff_key"],
            tournament_epoch_id=require_identifier(
                value["tournament_epoch_id"], expected_namespace="epoch"
            ),
            tournament_event_sequence=value["tournament_event_sequence"],
            packet_identities=tuple(
                PacketIdentity.from_dict(item) for item in value["packet_identities"]
            ),
            sections=tuple(ReceiptSection.from_dict(item) for item in value["sections"]),
            marks=tuple(MarkAssignment.from_dict(item) for item in value["marks"]),
            warning_codes=tuple(value["warning_codes"]),
            total_latency_ms=value["total_latency_ms"],
            bundles=tuple(BundleIdentity.from_dict(item) for item in value["bundles"]),
            content_digest=value["content_digest"],
        )


def _receipt_content_value(**arguments: Any) -> dict[str, Any]:
    supersedes = arguments["supersedes_receipt_id"]
    return {
        "schema_version": FIELD_RECEIPT_SCHEMA_VERSION,
        "field_id": str(arguments["field_id"]),
        "field_revision": arguments["field_revision"],
        "supersedes_receipt_id": None if supersedes is None else str(supersedes),
        "ordered_competitor_ids": [str(item) for item in arguments["ordered_competitor_ids"]],
        "target_context": arguments["target_context"].to_dict(),
        "target_context_digest": arguments["target_context_digest"],
        "historical_cutoff_key": arguments["historical_cutoff_key"],
        "tournament_epoch_id": str(arguments["tournament_epoch_id"]),
        "tournament_event_sequence": arguments["tournament_event_sequence"],
        "packet_identities": [item.to_dict() for item in arguments["packet_identities"]],
        "sections": [item.to_dict() for item in arguments["sections"]],
        "marks": [item.to_dict() for item in arguments["marks"]],
        "warning_codes": list(arguments["warning_codes"]),
        "total_latency_ms": arguments["total_latency_ms"],
        "bundles": [item.to_dict() for item in arguments["bundles"]],
    }


__all__ = [
    "FIELD_RECEIPT_SCHEMA_VERSION",
    "MAX_RECEIPT_CANONICAL_BYTES",
    "RECEIPT_SECTION_SCHEMA_VERSION",
    "BundleIdentity",
    "FieldReceipt",
    "MarkAssignment",
    "PacketIdentity",
    "ReceiptSection",
    "ReceiptSectionKind",
]
