"""Closed Pydantic transport schemas for the frozen V3 consumer boundary."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from strathmark.v3.contracts.canonical import canonical_bytes
from strathmark.v3.contracts.commands import CommandKind

_ID = r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"
_DIGEST = r"^[0-9a-f]{64}$"
_UTC_MS = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
OFFLINE_OR_DEDICATED_CREDENTIAL_COMMANDS = frozenset(
    {
        CommandKind.BOOTSTRAP_SERVICE_CREDENTIAL,
        CommandKind.ROTATE_SERVICE_CREDENTIAL,
        CommandKind.REVOKE_SERVICE_CREDENTIAL,
        CommandKind.RECOVER_SERVICE_CREDENTIAL,
    }
)
GENERIC_ONLINE_COMMAND_KINDS = frozenset(
    {
        CommandKind.REVISE_TOURNAMENT_SNAPSHOT,
        CommandKind.REVISE_ROUND_SNAPSHOT,
        CommandKind.CONFIGURE_TOURNAMENT,
        CommandKind.OPEN_TOURNAMENT,
        CommandKind.CLOSE_TOURNAMENT,
        CommandKind.CONFIGURE_ROUND,
        CommandKind.FREEZE_ROUND,
        CommandKind.BEGIN_ROUND_CLOSING,
        CommandKind.CLOSE_ROUND,
        CommandKind.SUPERSEDE_FIELD,
        CommandKind.REGENERATE_FIELD,
        CommandKind.RECORD_RESULT,
        CommandKind.CORRECT_RESULT,
        CommandKind.VOID_RESULT,
        CommandKind.COMPLETE_DERIVATION_REACTION,
        CommandKind.COMPLETE_DERIVATION_SEQUENCE,
        CommandKind.RECORD_CAPABILITY_UPDATE,
        CommandKind.REBASE_CAPABILITY_STATE,
        CommandKind.CHANGE_WEIGHTS,
        CommandKind.RECORD_APPROVAL_DECISION,
        CommandKind.OPTIMIZE_FIELD,
        CommandKind.SETTLE_FIELD,
        CommandKind.CREATE_MODEL_CANDIDATE,
        CommandKind.QUEUE_JOB,
        CommandKind.LEASE_JOB,
        CommandKind.SUCCEED_JOB,
        CommandKind.INVALIDATE_JOB,
        CommandKind.RECORD_RETRYABLE_JOB_FAILURE,
        CommandKind.REQUEUE_JOB,
        CommandKind.MARK_JOB_STALE,
        CommandKind.RECORD_PERMANENT_JOB_FAILURE,
        CommandKind.CANCEL_JOB,
    }
)
OnlineCommandKind = Enum(
    "OnlineCommandKind",
    {item.name: item.value for item in CommandKind if item in GENERIC_ONLINE_COMMAND_KINDS},
    type=str,
    module=__name__,
)


class StrictV3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ErrorResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-error-v1"] = "strathmark-v3-error-v1"
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=256)


class HealthResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-health-v1"] = "strathmark-v3-health-v1"
    status: Literal["ok"] = "ok"


class PrepareCardRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-card-preparation-request-v1"]
    tournament_id: str = Field(pattern=_ID)
    round_id: str = Field(pattern=_ID)
    field_id: str = Field(pattern=_ID)
    competitor_id: str = Field(pattern=_ID)
    source_revision: int = Field(ge=1)
    target_context_digest: str = Field(pattern=_DIGEST)
    deadline_ms: int = Field(ge=25, le=60_000)


class ExpectedVersion(StrictV3Model):
    aggregate_id: str = Field(pattern=_ID)
    version: int = Field(ge=0)


class ExecuteCommandRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-command-execution-request-v1"]
    command_kind: Annotated[OnlineCommandKind, Field(strict=False)]
    target_aggregate: str = Field(pattern=_ID)
    expected_versions: list[ExpectedVersion] = Field(min_length=1, max_length=128)
    payload_schema_version: str = Field(
        pattern=r"^strathmark-v3-[a-z0-9][a-z0-9-]{0,94}-v[1-9][0-9]*$"
    )
    canonical_payload_json: str = Field(min_length=2, max_length=65_536)
    payload_digest: str = Field(pattern=_DIGEST)
    deadline_ms: int = Field(ge=25, le=60_000)

    @model_validator(mode="after")
    def _canonical_command(self) -> ExecuteCommandRequest:
        try:
            parsed = json.loads(self.canonical_payload_json)
            encoded = canonical_bytes(parsed, max_bytes=65_536)
        except Exception as exc:
            raise ValueError("command payload must be bounded canonical JSON") from exc
        if not isinstance(parsed, dict) or encoded.decode("utf-8") != self.canonical_payload_json:
            raise ValueError("command payload must be a canonical JSON object")
        if parsed.get("schema_version") != self.payload_schema_version:
            raise ValueError("command payload schema version does not match its envelope")
        if hashlib.sha256(encoded).hexdigest() != self.payload_digest:
            raise ValueError("command payload digest does not match canonical bytes")
        versions = tuple((item.aggregate_id, item.version) for item in self.expected_versions)
        if versions != tuple(sorted(versions)) or len({item[0] for item in versions}) != len(
            versions
        ):
            raise ValueError("expected versions must be unique and sorted")
        if self.target_aggregate not in {item[0] for item in versions}:
            raise ValueError("expected versions must include the target aggregate")
        return self


class ExecuteCommandResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-command-execution-response-v1"] = (
        "strathmark-v3-command-execution-response-v1"
    )
    command_id: str = Field(pattern=_ID)
    disposition: Literal["committed", "recovered"]
    result_schema_version: str = Field(
        pattern=r"^strathmark-v3-[a-z0-9][a-z0-9-]{0,94}-v[1-9][0-9]*$"
    )
    result_digest: str = Field(pattern=_DIGEST)
    canonical_result_json: str = Field(min_length=2, max_length=1_048_576)
    first_global_sequence: int = Field(ge=1)
    last_global_sequence: int = Field(ge=1)
    event_set_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def _canonical_result(self) -> ExecuteCommandResponse:
        try:
            parsed = json.loads(self.canonical_result_json)
            encoded = canonical_bytes(parsed, max_bytes=1_048_576)
        except Exception as exc:
            raise ValueError("command result must be bounded canonical JSON") from exc
        if not isinstance(parsed, dict) or encoded.decode("utf-8") != self.canonical_result_json:
            raise ValueError("command result must be a canonical JSON object")
        if hashlib.sha256(encoded).hexdigest() != self.result_digest:
            raise ValueError("command result digest does not match canonical bytes")
        if self.last_global_sequence < self.first_global_sequence:
            raise ValueError("command result sequence range is invalid")
        return self


class PrepareCardResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-card-preparation-response-v1"] = (
        "strathmark-v3-card-preparation-response-v1"
    )
    job_id: str = Field(pattern=_ID)
    status: Literal["queued", "already_queued", "ready"]
    authority_sequence: int = Field(ge=0)


class AssembleFieldRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-field-assembly-request-v1"]
    field_id: str = Field(pattern=_ID)
    upstream_field_revision: int = Field(ge=1)
    ordered_competitor_ids: list[str] = Field(min_length=2, max_length=64)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _closed_roster(self) -> AssembleFieldRequest:
        if len(set(self.ordered_competitor_ids)) != len(self.ordered_competitor_ids):
            raise ValueError("ordered competitor IDs must be unique")
        if any(
            __import__("re").fullmatch(_ID, value) is None for value in self.ordered_competitor_ids
        ):
            raise ValueError("ordered competitor IDs must be namespaced identifiers")
        return self


class AssembleFieldResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-field-assembly-response-v1"] = (
        "strathmark-v3-field-assembly-response-v1"
    )
    receipt_id: str = Field(pattern=_ID)
    receipt_digest: str = Field(pattern=_DIGEST)
    disposition: Literal["prepared", "recovered"]
    canonical_receipt_json: str = Field(min_length=2, max_length=1_048_576)
    authority_sequence: int = Field(ge=1)


class ReceiptLookupRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-receipt-lookup-request-v1"]
    request_identity: str = Field(pattern=_ID)
    receipt_id: str | None = Field(pattern=_ID)
    deadline_ms: int = Field(ge=25, le=5_000)


class ReceiptLookupResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-receipt-lookup-response-v1"] = (
        "strathmark-v3-receipt-lookup-response-v1"
    )
    found: bool
    receipt_id: str | None = Field(default=None, pattern=_ID)
    receipt_digest: str | None = Field(default=None, pattern=_DIGEST)
    canonical_receipt_json: str | None = Field(default=None, max_length=1_048_576)
    authority_sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def _complete_result(self) -> ReceiptLookupResponse:
        present = (self.receipt_id, self.receipt_digest, self.canonical_receipt_json)
        if self.found != all(value is not None for value in present):
            raise ValueError("receipt lookup result must be complete exactly when found")
        return self


class ReceiptBinding(StrictV3Model):
    receipt_id: str = Field(pattern=_ID)
    receipt_digest: str = Field(pattern=_DIGEST)


class IssueAcknowledgmentRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-issue-acknowledgment-request-v1"]
    upstream_issue_id: str = Field(pattern=_ID)
    receipt_bindings: list[ReceiptBinding] = Field(min_length=1, max_length=64)
    issued_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _unique_receipts(self) -> IssueAcknowledgmentRequest:
        identities = tuple(item.receipt_id for item in self.receipt_bindings)
        if len(set(identities)) != len(identities):
            raise ValueError("issue acknowledgment cannot repeat a receipt")
        return self


class IssueAcknowledgmentResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-issue-acknowledgment-response-v1"] = (
        "strathmark-v3-issue-acknowledgment-response-v1"
    )
    issue_batch_id: str = Field(pattern=_ID)
    receipt_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    authority_sequence: int = Field(ge=1)
    recovery_marker_digest: str = Field(pattern=_DIGEST)


class ResultRow(StrictV3Model):
    competitor_id: str = Field(pattern=_ID)
    status: Literal["completion", "dnf", "dq", "dns", "void", "penalty"]
    raw_time_ms: int | None = Field(ge=1, le=600_000)
    penalty_ms: int | None = Field(ge=1, le=600_000)
    source_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def _status_fields(self) -> ResultRow:
        if self.status == "completion" and (
            self.raw_time_ms is None or self.penalty_ms is not None
        ):
            raise ValueError("completion requires raw time and no penalty")
        if self.status == "penalty" and (self.raw_time_ms is None or self.penalty_ms is None):
            raise ValueError("penalty requires raw time and penalty")
        if self.status not in {"completion", "penalty"} and (
            self.raw_time_ms is not None or self.penalty_ms is not None
        ):
            raise ValueError("nonfinish/void result cannot carry numeric time")
        return self


class SettlementRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-settlement-request-v1"]
    issue_batch_id: str = Field(pattern=_ID)
    receipt_id: str = Field(pattern=_ID)
    results: list[ResultRow] = Field(min_length=1, max_length=64)
    observed_at_utc: str = Field(pattern=_UTC_MS)
    deadline_ms: int = Field(ge=25, le=10_000)

    @model_validator(mode="after")
    def _unique_results(self) -> SettlementRequest:
        identities = tuple(item.competitor_id for item in self.results)
        if len(set(identities)) != len(identities):
            raise ValueError("settlement cannot repeat a competitor")
        return self


class SettlementResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-settlement-response-v1"] = (
        "strathmark-v3-settlement-response-v1"
    )
    settlement_id: str = Field(pattern=_ID)
    receipt_id: str = Field(pattern=_ID)
    authority_sequence: int = Field(ge=1)
    status: Literal["recorded", "recovered"]


class StatusResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-status-response-v1"] = "strathmark-v3-status-response-v1"
    service: Literal["ready", "degraded", "stopped"] = Field(
        description="Health of this V3 service process; not a production-authority claim."
    )
    authority_sequence: int = Field(ge=0)
    engine_authority: Literal["v2", "v3", "traditional_manual"] = Field(
        description="Deprecated compatibility alias for production_authority."
    )
    v3_readiness: Literal["candidate", "production"] = Field(
        description="V3 release posture, independent of local process health."
    )
    production_authority: Literal["v2", "v3", "traditional_manual"] = Field(
        description="Current externally verified production authority."
    )
    cutover_receipt_digest: str | None = Field(pattern=_DIGEST)
    cutover_verified_at_utc: str | None = Field(pattern=_UTC_MS)
    deep_verification_state: Literal["verified", "unavailable"]
    event_last_deep_verified_at_utc: str = Field(pattern=_UTC_MS)
    event_checkpoint_digest: str = Field(pattern=_DIGEST)
    field_last_deep_verified_at_utc: str = Field(pattern=_UTC_MS)
    field_checkpoint_digest: str = Field(pattern=_DIGEST)
    job_last_deep_verified_at_utc: str = Field(pattern=_UTC_MS)
    job_checkpoint_digest: str = Field(pattern=_DIGEST)
    open_tournament_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent_authority(self) -> StatusResponse:
        if self.engine_authority != self.production_authority:
            raise ValueError("legacy engine authority must mirror production authority")
        if (self.v3_readiness == "production") != (self.production_authority == "v3"):
            raise ValueError("V3 production readiness requires verified V3 authority")
        cutover_evidence_present = (
            self.cutover_receipt_digest is not None and self.cutover_verified_at_utc is not None
        )
        if (self.cutover_receipt_digest is None) != (self.cutover_verified_at_utc is None):
            raise ValueError("cutover evidence must be complete")
        if (self.production_authority == "v3") != cutover_evidence_present:
            raise ValueError("V3 production authority requires cutover evidence")
        return self


class CredentialRotationRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-credential-rotation-request-v1"]
    overlap_seconds: int = Field(ge=1, le=900)


class CredentialRotationResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-credential-rotation-response-v1"] = (
        "strathmark-v3-credential-rotation-response-v1"
    )
    credential: str = Field(min_length=30, max_length=264, repr=False)
    key_id_digest: str = Field(pattern=_DIGEST)
    principal_id: str = Field(pattern=_ID)
    overlap_seconds: int = Field(ge=1, le=900)


class CredentialRevocationRequest(StrictV3Model):
    schema_version: Literal["strathmark-v3-credential-revocation-request-v1"]
    key_id_digest: str = Field(pattern=_DIGEST)


class CredentialRevocationResponse(StrictV3Model):
    schema_version: Literal["strathmark-v3-credential-revocation-response-v1"] = (
        "strathmark-v3-credential-revocation-response-v1"
    )
    key_id_digest: str = Field(pattern=_DIGEST)
    status: Literal["revoked"] = "revoked"


__all__ = [name for name in globals() if name.endswith(("Request", "Response"))]
