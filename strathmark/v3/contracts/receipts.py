"""Atomic content-addressed field receipt contracts."""

from __future__ import annotations

import re
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import (
    BLOB_REFERENCE_V2_SCHEMA_VERSION,
    BlobReference,
    BlobReferenceV2,
    InlinePayload,
    PayloadReference,
)
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
    EngineExecutionMode,
    PredictionEngine,
    _require_fields,
    _require_nonnegative_int,
    _require_positive_int,
    _require_schema,
)

FIELD_RECEIPT_SCHEMA_VERSION = "strathmark-v3-field-receipt-v2"
LEGACY_FIELD_RECEIPT_SCHEMA_VERSION = "strathmark-v3-field-receipt-v1"
RECEIPT_SECTION_SCHEMA_VERSION = "strathmark-v3-receipt-section-v1"
MAX_RECEIPT_CANONICAL_BYTES = 1_048_576
_CALLER_NAMESPACE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_WARNING = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True, slots=True)
class EngineAuthorityBinding:
    """Exact competition-scoped numeric authority carried by a V3 receipt."""

    scope_id: StableIdentifier
    engine: PredictionEngine
    mode: EngineExecutionMode
    selection_digest: str
    consumer_contract_digest: str
    source_commit: str

    def __post_init__(self) -> None:
        require_identifier(self.scope_id, expected_namespace="tournament")
        if self.engine is not PredictionEngine.V3:
            raise ContractError("V3 receipt authority must name the V3 engine")
        if not isinstance(self.mode, EngineExecutionMode):
            raise ContractError("receipt execution mode is invalid")
        _require_digest(self.selection_digest, "selection digest")
        _require_digest(self.consumer_contract_digest, "consumer contract digest")
        if (
            not isinstance(self.source_commit, str)
            or _SOURCE_COMMIT.fullmatch(self.source_commit) is None
        ):
            raise ContractError("receipt source commit is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "scope_id": str(self.scope_id),
            "engine": self.engine.value,
            "mode": self.mode.value,
            "selection_digest": self.selection_digest,
            "consumer_contract_digest": self.consumer_contract_digest,
            "source_commit": self.source_commit,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EngineAuthorityBinding:
        _require_fields(
            value,
            {
                "scope_id",
                "engine",
                "mode",
                "selection_digest",
                "consumer_contract_digest",
                "source_commit",
            },
        )
        try:
            engine = PredictionEngine(value["engine"])
            mode = EngineExecutionMode(value["mode"])
        except (TypeError, ValueError) as exc:
            raise ContractError("receipt engine authority vocabulary is invalid") from exc
        return cls(
            scope_id=require_identifier(value["scope_id"], expected_namespace="tournament"),
            engine=engine,
            mode=mode,
            selection_digest=value["selection_digest"],
            consumer_contract_digest=value["consumer_contract_digest"],
            source_commit=value["source_commit"],
        )


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
        return {
            "competitor_id": str(self.competitor_id),
            "packet_digest": self.packet_digest,
        }

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
        if not isinstance(self.payload, (InlinePayload, BlobReference, BlobReferenceV2)):
            raise ContractError("receipt section payload must be inline or a blob reference")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "payload_type": ("inline" if isinstance(self.payload, InlinePayload) else "blob"),
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
            payload_value = value["payload"]
            if not isinstance(payload_value, Mapping):
                raise ContractError("receipt blob payload must be an object")
            if payload_value.get("schema_version") == BLOB_REFERENCE_V2_SCHEMA_VERSION:
                payload = BlobReferenceV2.from_dict(payload_value)
            else:
                payload = BlobReference.from_dict(payload_value)
        else:
            raise ContractError("unknown receipt section payload type")
        return cls(kind, payload)


@dataclass(frozen=True, slots=True)
class _GeneratedFieldReceiptProof:
    token: object
    receipt_id: StableIdentifier
    content_digest: str
    canonical_payload: bytes


_GENERATED_FIELD_RECEIPT_TOKEN = object()


@dataclass(frozen=True, slots=True)
class FieldReceipt:
    receipt_id: StableIdentifier
    caller_namespace: str
    request_identity: IdempotencyKey
    field_id: StableIdentifier
    upstream_field_revision: int
    receipt_revision: int
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
    engine_authority: EngineAuthorityBinding | None = None
    schema_version: str = LEGACY_FIELD_RECEIPT_SCHEMA_VERSION
    _generated_proof: InitVar[_GeneratedFieldReceiptProof | None] = None
    _canonical_payload_cache: bytes = field(init=False, repr=False, compare=False, default=b"")

    def __post_init__(self, _generated_proof: _GeneratedFieldReceiptProof | None) -> None:
        expected_schema = (
            FIELD_RECEIPT_SCHEMA_VERSION
            if self.engine_authority is not None
            else LEGACY_FIELD_RECEIPT_SCHEMA_VERSION
        )
        _require_schema(self.schema_version, expected_schema)
        _require_id(self.receipt_id, "receipt")
        if not isinstance(self.caller_namespace, str) or not _CALLER_NAMESPACE.fullmatch(
            self.caller_namespace
        ):
            raise ContractError("caller_namespace must be a bounded lower-case token")
        if not isinstance(self.request_identity, IdempotencyKey):
            raise ContractError("request_identity must be an IdempotencyKey")
        _require_id(self.field_id, "field")
        _require_positive_int(self.upstream_field_revision, "upstream_field_revision")
        _require_positive_int(self.receipt_revision, "receipt_revision")
        if self.receipt_revision == 1 and self.supersedes_receipt_id is not None:
            raise ContractError("receipt revision 1 cannot supersede a receipt")
        if self.receipt_revision > 1 and self.supersedes_receipt_id is None:
            raise ContractError("later receipt revisions require supersedes_receipt_id")
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
        if self.engine_authority is not None and not isinstance(
            self.engine_authority, EngineAuthorityBinding
        ):
            raise ContractError("engine authority must be a typed receipt binding")

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
        generated = (
            isinstance(_generated_proof, _GeneratedFieldReceiptProof)
            and _generated_proof.token is _GENERATED_FIELD_RECEIPT_TOKEN
            and _generated_proof.receipt_id == self.receipt_id
            and _generated_proof.content_digest == self.content_digest
        )
        if generated:
            assert _generated_proof is not None
            canonical_payload = _generated_proof.canonical_payload
        else:
            if self.content_digest != self.recompute_content_digest():
                raise ContractError("receipt content digest mismatch")
            if self.receipt_id != self.recompute_receipt_id():
                raise ContractError("receipt identity is not bound to content and caller request")
            try:
                canonical_payload = canonical_bytes(
                    self.to_dict(), max_bytes=MAX_RECEIPT_CANONICAL_BYTES
                )
            except Exception as exc:
                raise ContractError("receipt exceeds the maximum canonical size") from exc
        object.__setattr__(self, "_canonical_payload_cache", canonical_payload)

    @classmethod
    def create(
        cls,
        *,
        caller_namespace: str,
        request_identity: IdempotencyKey,
        field_id: StableIdentifier,
        upstream_field_revision: int,
        receipt_revision: int,
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
        engine_authority: EngineAuthorityBinding | None = None,
    ) -> FieldReceipt:
        arguments = locals().copy()
        arguments.pop("cls")
        content_value = _receipt_content_value(**arguments)
        content_digest = canonical_digest(content_value)
        receipt_id = deterministic_identifier(
            "receipt",
            {
                "caller_namespace": caller_namespace,
                "request_identity": str(request_identity),
                "content_digest": content_digest,
            },
        )
        try:
            canonical_payload = canonical_bytes(
                {
                    "schema_version": FIELD_RECEIPT_SCHEMA_VERSION,
                    "receipt_id": str(receipt_id),
                    "caller_namespace": caller_namespace,
                    "request_identity": str(request_identity),
                    **content_value,
                    "content_digest": content_digest,
                },
                max_bytes=MAX_RECEIPT_CANONICAL_BYTES,
            )
        except Exception as exc:
            raise ContractError("receipt exceeds the maximum canonical size") from exc
        return cls(
            receipt_id=receipt_id,
            content_digest=content_digest,
            schema_version=(
                FIELD_RECEIPT_SCHEMA_VERSION
                if engine_authority is not None
                else LEGACY_FIELD_RECEIPT_SCHEMA_VERSION
            ),
            **arguments,
            _generated_proof=_GeneratedFieldReceiptProof(
                _GENERATED_FIELD_RECEIPT_TOKEN,
                receipt_id,
                content_digest,
                canonical_payload,
            ),
        )

    @property
    def canonical_payload(self) -> bytes:
        """Return exact validated bytes, cached only after complete construction."""

        return self._canonical_payload_cache

    def creation_arguments(self) -> dict[str, Any]:
        return {
            "caller_namespace": self.caller_namespace,
            "request_identity": self.request_identity,
            "field_id": self.field_id,
            "upstream_field_revision": self.upstream_field_revision,
            "receipt_revision": self.receipt_revision,
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
            "engine_authority": self.engine_authority,
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
            "upstream_field_revision",
            "receipt_revision",
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
        schema_version = value.get("schema_version")
        if schema_version == FIELD_RECEIPT_SCHEMA_VERSION:
            expected.add("engine_authority")
        _require_fields(value, expected)
        if schema_version not in {
            FIELD_RECEIPT_SCHEMA_VERSION,
            LEGACY_FIELD_RECEIPT_SCHEMA_VERSION,
        }:
            raise ContractError("unsupported field receipt schema version")
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
            upstream_field_revision=value["upstream_field_revision"],
            receipt_revision=value["receipt_revision"],
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
            engine_authority=(
                None
                if schema_version == LEGACY_FIELD_RECEIPT_SCHEMA_VERSION
                else EngineAuthorityBinding.from_dict(value["engine_authority"])
            ),
            schema_version=schema_version,
        )


def _receipt_content_value(**arguments: Any) -> dict[str, Any]:
    supersedes = arguments["supersedes_receipt_id"]
    engine_authority = arguments.get("engine_authority")
    schema_version = (
        FIELD_RECEIPT_SCHEMA_VERSION
        if engine_authority is not None
        else LEGACY_FIELD_RECEIPT_SCHEMA_VERSION
    )
    value = {
        "schema_version": schema_version,
        "field_id": str(arguments["field_id"]),
        "upstream_field_revision": arguments["upstream_field_revision"],
        "receipt_revision": arguments["receipt_revision"],
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
    if engine_authority is not None:
        value["engine_authority"] = engine_authority.to_dict()
    return value


__all__ = [
    "FIELD_RECEIPT_SCHEMA_VERSION",
    "MAX_RECEIPT_CANONICAL_BYTES",
    "RECEIPT_SECTION_SCHEMA_VERSION",
    "BundleIdentity",
    "EngineAuthorityBinding",
    "FieldReceipt",
    "MarkAssignment",
    "PacketIdentity",
    "ReceiptSection",
    "ReceiptSectionKind",
]
