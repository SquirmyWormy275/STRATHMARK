"""Closed command envelopes and bounded inline/blob payload references."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import _require_digest, _require_id
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    require_idempotency_key,
    require_identifier,
)
from strathmark.v3.contracts.statuses import _require_fields, _require_schema

COMMAND_SCHEMA_VERSION = "strathmark-v3-command-envelope-v1"
INLINE_PAYLOAD_SCHEMA_VERSION = "strathmark-v3-inline-payload-v1"
BLOB_REFERENCE_SCHEMA_VERSION = "strathmark-v3-blob-reference-v1"
MAX_INLINE_PAYLOAD_BYTES = 65_536
MAX_BLOB_BYTES = 16_777_216


class CommandKind(str, Enum):
    CONFIGURE_TOURNAMENT = "configure_tournament"
    OPEN_TOURNAMENT = "open_tournament"
    CLOSE_TOURNAMENT = "close_tournament"
    CONFIGURE_ROUND = "configure_round"
    FREEZE_ROUND = "freeze_round"
    BEGIN_ROUND_CLOSING = "begin_round_closing"
    CLOSE_ROUND = "close_round"
    SUPERSEDE_FIELD = "supersede_field"
    REGENERATE_FIELD = "regenerate_field"
    RECORD_RESULT = "record_result"
    CORRECT_RESULT = "correct_result"
    VOID_RESULT = "void_result"
    PREPARE_FORECAST = "prepare_forecast"
    COMMIT_FORECAST = "commit_forecast"
    RECORD_CAPABILITY_UPDATE = "record_capability_update"
    RECORD_SCORE = "record_score"
    CHANGE_WEIGHTS = "change_weights"
    POOL_FORECASTS = "pool_forecasts"
    CLASSIFY_DISAGREEMENT = "classify_disagreement"
    APPLY_OVERRIDE = "apply_override"
    OPTIMIZE_FIELD = "optimize_field"
    ACKNOWLEDGE_ISSUE = "acknowledge_issue"
    SETTLE_FIELD = "settle_field"
    IMPORT_HISTORY = "import_history"
    CREATE_MODEL_CANDIDATE = "create_model_candidate"
    PROMOTE_BUNDLE = "promote_bundle"
    ROLLBACK_BUNDLE = "rollback_bundle"
    SUSPEND_LIVE = "suspend_live"
    RESUME_LIVE = "resume_live"
    EMERGENCY_STOP = "emergency_stop"
    QUEUE_JOB = "queue_job"
    LEASE_JOB = "lease_job"
    SUCCEED_JOB = "succeed_job"
    INVALIDATE_JOB = "invalidate_job"
    RECORD_RETRYABLE_JOB_FAILURE = "record_retryable_job_failure"
    REQUEUE_JOB = "requeue_job"
    MARK_JOB_STALE = "mark_job_stale"
    RECORD_PERMANENT_JOB_FAILURE = "record_permanent_job_failure"
    CANCEL_JOB = "cancel_job"


@dataclass(frozen=True, slots=True)
class InlinePayload:
    """Exact canonical JSON kept inline only below the declared boundary."""

    canonical_json: str
    digest: str
    schema_version: str = INLINE_PAYLOAD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, INLINE_PAYLOAD_SCHEMA_VERSION)
        if not isinstance(self.canonical_json, str):
            raise ContractError("inline payload must be canonical JSON text")
        try:
            value = json.loads(self.canonical_json)
            encoded = canonical_bytes(value, max_bytes=MAX_INLINE_PAYLOAD_BYTES)
        except Exception as exc:
            raise ContractError("inline payload must be bounded canonical JSON") from exc
        if encoded.decode("utf-8") != self.canonical_json:
            raise ContractError("inline payload is not in exact canonical form")
        if not isinstance(value, dict):
            raise ContractError("inline payload must contain a JSON object")
        _require_digest(self.digest, "inline payload digest")
        if canonical_digest(value, max_bytes=MAX_INLINE_PAYLOAD_BYTES) != self.digest:
            raise ContractError("inline payload digest mismatch")

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> InlinePayload:
        if not isinstance(value, Mapping):
            raise ContractError("inline payload must be created from a mapping")
        try:
            encoded = canonical_bytes(value, max_bytes=MAX_INLINE_PAYLOAD_BYTES)
        except Exception as exc:
            raise ContractError("inline payload exceeds maximum or is not canonical") from exc
        return cls(
            encoded.decode("utf-8"), canonical_digest(value, max_bytes=MAX_INLINE_PAYLOAD_BYTES)
        )

    def to_value(self) -> dict[str, Any]:
        value = json.loads(self.canonical_json)
        assert isinstance(value, dict)
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "canonical_json": self.canonical_json,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> InlinePayload:
        _require_fields(value, {"schema_version", "canonical_json", "digest"})
        _require_schema(value["schema_version"], INLINE_PAYLOAD_SCHEMA_VERSION)
        return cls(value["canonical_json"], value["digest"])


@dataclass(frozen=True, slots=True)
class BlobReference:
    """Content-addressed reference required for payloads too large to inline."""

    blob_id: StableIdentifier
    digest: str
    byte_count: int
    media_type: str
    schema_version: str = BLOB_REFERENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, BLOB_REFERENCE_SCHEMA_VERSION)
        _require_id(self.blob_id, "blob")
        _require_digest(self.digest, "blob digest")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count <= MAX_INLINE_PAYLOAD_BYTES
        ):
            raise ContractError("blob byte_count must be above the inline boundary")
        if self.byte_count > MAX_BLOB_BYTES:
            raise ContractError("blob byte_count exceeds the maximum")
        if self.media_type not in {"application/json", "application/octet-stream"}:
            raise ContractError("unsupported blob media_type")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "blob_id": str(self.blob_id),
            "digest": self.digest,
            "byte_count": self.byte_count,
            "media_type": self.media_type,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BlobReference:
        _require_fields(value, {"schema_version", "blob_id", "digest", "byte_count", "media_type"})
        _require_schema(value["schema_version"], BLOB_REFERENCE_SCHEMA_VERSION)
        return cls(
            blob_id=require_identifier(value["blob_id"], expected_namespace="blob"),
            digest=value["digest"],
            byte_count=value["byte_count"],
            media_type=value["media_type"],
        )


PayloadReference = InlinePayload | BlobReference


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    kind: CommandKind
    command_id: IdempotencyKey
    target_aggregate: StableIdentifier
    expected_versions: tuple[tuple[str, int], ...]
    actor_id: StableIdentifier
    payload: PayloadReference
    schema_version: str = COMMAND_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version, COMMAND_SCHEMA_VERSION)
        if not isinstance(self.kind, CommandKind):
            raise ContractError("command kind must be a CommandKind value")
        if not isinstance(self.command_id, IdempotencyKey):
            raise ContractError("command_id must be an IdempotencyKey")
        if not isinstance(self.target_aggregate, StableIdentifier):
            raise ContractError("target_aggregate must be a StableIdentifier")
        _require_id(self.actor_id, "actor")
        if not isinstance(self.expected_versions, tuple):
            raise ContractError("expected_versions must be an immutable tuple")
        normalized: list[tuple[str, int]] = []
        seen: set[str] = set()
        for item in self.expected_versions:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ContractError("expected_versions entries must be identifier/version pairs")
            aggregate_id, version = item
            identifier = str(require_identifier(aggregate_id))
            if identifier in seen:
                raise ContractError("expected_versions cannot repeat an aggregate")
            seen.add(identifier)
            if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                raise ContractError("expected aggregate versions must be non-negative integers")
            normalized.append((identifier, version))
        if tuple(normalized) != tuple(sorted(normalized)):
            raise ContractError("expected_versions must be sorted by aggregate identity")
        if str(self.target_aggregate) not in seen:
            raise ContractError("expected_versions must include the target aggregate")
        if not isinstance(self.payload, (InlinePayload, BlobReference)):
            raise ContractError("payload must be an InlinePayload or BlobReference")

    @property
    def payload_digest(self) -> str:
        return self.payload.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "command_id": str(self.command_id),
            "target_aggregate": str(self.target_aggregate),
            "expected_versions": [list(item) for item in self.expected_versions],
            "actor_id": str(self.actor_id),
            "payload_type": "inline" if isinstance(self.payload, InlinePayload) else "blob",
            "payload": self.payload.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CommandEnvelope:
        expected = {
            "schema_version",
            "kind",
            "command_id",
            "target_aggregate",
            "expected_versions",
            "actor_id",
            "payload_type",
            "payload",
        }
        _require_fields(value, expected)
        _require_schema(value["schema_version"], COMMAND_SCHEMA_VERSION)
        try:
            kind = CommandKind(value["kind"])
        except (TypeError, ValueError) as exc:
            raise ContractError("unknown command kind") from exc
        versions = value["expected_versions"]
        if not isinstance(versions, list):
            raise ContractError("expected_versions must be a JSON array")
        pairs: list[tuple[str, int]] = []
        for item in versions:
            if not isinstance(item, list) or len(item) != 2:
                raise ContractError("expected_versions entries must be JSON pairs")
            pairs.append((item[0], item[1]))
        payload_type = value["payload_type"]
        if payload_type == "inline":
            payload: PayloadReference = InlinePayload.from_dict(value["payload"])
        elif payload_type == "blob":
            payload = BlobReference.from_dict(value["payload"])
        else:
            raise ContractError("unknown payload type")
        return cls(
            kind=kind,
            command_id=require_idempotency_key(value["command_id"]),
            target_aggregate=require_identifier(value["target_aggregate"]),
            expected_versions=tuple(pairs),
            actor_id=require_identifier(value["actor_id"], expected_namespace="actor"),
            payload=payload,
        )


__all__ = [
    "BLOB_REFERENCE_SCHEMA_VERSION",
    "COMMAND_SCHEMA_VERSION",
    "INLINE_PAYLOAD_SCHEMA_VERSION",
    "MAX_BLOB_BYTES",
    "MAX_INLINE_PAYLOAD_BYTES",
    "BlobReference",
    "CommandEnvelope",
    "CommandKind",
    "InlinePayload",
    "PayloadReference",
]
