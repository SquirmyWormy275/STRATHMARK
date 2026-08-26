"""Build, load, and verify the distinct frozen STRATHMARK V3 consumer contract."""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from typing import Any, cast

V3_CONSUMER_CONTRACT_VERSION = "strathmark.v3-consumer-contract.v3"
EXPECTED_V3_CONSUMER_PATHS = frozenset(
    {
        "/v3/health",
        "/v3/cards/prepare",
        "/v3/fields/assemble",
        "/v3/receipts/lookup",
        "/v3/approvals/decide",
        "/v3/issues/acknowledge",
        "/v3/results/settle",
        "/v3/status",
        "/v3/credentials/rotate",
        "/v3/credentials/revoke",
    }
)
_CONTRACT_RESOURCE = "contracts/v3_consumer.openapi.json"
_CHECKSUM_RESOURCE = "contracts/v3_consumer.openapi.sha256"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class V3ConsumerContractIntegrityError(RuntimeError):
    """The installed V3 consumer contract is absent, malformed, or changed."""


_EXAMPLES: dict[str, dict[str, Any]] = {
    "/v3/health": {"response": {"schema_version": "strathmark-v3-health-v1", "status": "ok"}},
    "/v3/cards/prepare": {
        "request": {
            "schema_version": "strathmark-v3-card-preparation-request-v1",
            "tournament_id": "tournament:show",
            "round_id": "round:heats",
            "field_id": "field:heat-7",
            "competitor_id": "competitor:alice",
            "source_revision": 3,
            "target_context_digest": "a" * 64,
            "deadline_ms": 5000,
        },
        "response": {
            "schema_version": "strathmark-v3-card-preparation-response-v1",
            "job_id": "job:prepare-1",
            "status": "queued",
            "authority_sequence": 10,
        },
    },
    "/v3/fields/assemble": {
        "request": {
            "schema_version": "strathmark-v3-field-assembly-request-v1",
            "field_id": "field:heat-7",
            "upstream_field_revision": 2,
            "ordered_competitor_ids": ["competitor:alice", "competitor:bob"],
            "deadline_ms": 1500,
        },
        "response": {
            "schema_version": "strathmark-v3-field-assembly-response-v1",
            "receipt_id": "receipt:field-1",
            "receipt_digest": "b" * 64,
            "disposition": "prepared",
            "canonical_receipt_json": '{"schema_version":"strathmark-v3-field-receipt-v1"}',
            "authority_sequence": 11,
        },
    },
    "/v3/receipts/lookup": {
        "request": {
            "schema_version": "strathmark-v3-receipt-lookup-request-v1",
            "request_identity": "command:original-request",
            "receipt_id": "receipt:field-1",
            "deadline_ms": 250,
        },
        "response": {
            "schema_version": "strathmark-v3-receipt-lookup-response-v1",
            "found": True,
            "receipt_id": "receipt:field-1",
            "receipt_digest": "b" * 64,
            "canonical_receipt_json": '{"schema_version":"strathmark-v3-field-receipt-v1"}',
            "authority_sequence": 11,
        },
    },
    "/v3/approvals/decide": {
        "request": {
            "schema_version": "strathmark-v3-approval-decision-request-v1",
            "tournament_id": "tournament:show",
            "snapshot_id": f"approval_snapshot:{'a' * 64}",
            "action": "ordinary_batch_accept",
            "selected": [
                {
                    "field_id": "field:heat-7",
                    "receipt_id": "receipt:field-1",
                    "receipt_digest": "b" * 64,
                    "receipt_revision": 2,
                    "upstream_field_revision": 3,
                    "row_digest": "c" * 64,
                    "call_order": 0,
                }
            ],
            "excluded": [
                {
                    "field_id": "field:heat-8",
                    "receipt_id": "receipt:field-2",
                    "receipt_digest": "d" * 64,
                    "receipt_revision": 1,
                    "upstream_field_revision": 3,
                    "row_digest": "e" * 64,
                    "call_order": 1,
                }
            ],
            "actor_metadata": {"judge_station": "station-a"},
            "reason_code": "judge_reviewed_batch",
            "superseded_receipt_id": None,
            "decided_at_utc": "2026-08-25T12:00:00.000Z",
            "deadline_ms": 1000,
        },
        "response": {
            "schema_version": "strathmark-v3-approval-decision-response-v1",
            "command_id": "command:approval-example",
            "caller_namespace": "api",
            "tournament_id": "tournament:show",
            "snapshot_id": f"approval_snapshot:{'a' * 64}",
            "action": "ordinary_batch_accept",
            "decisions": [
                {"receipt_id": "receipt:field-1", "decision_state": "accepted"},
                {"receipt_id": "receipt:field-2", "decision_state": "excluded"},
            ],
            "decided_at_utc": "2026-08-25T12:00:00.000Z",
            "actor_metadata_digest": "f" * 64,
            "receipt_bindings_digest": "1" * 64,
            "command_digest": "2" * 64,
            "decision_digest": "3" * 64,
            "authority_sequence": 12,
        },
    },
    "/v3/issues/acknowledge": {
        "request": {
            "schema_version": "strathmark-v3-issue-acknowledgment-request-v1",
            "upstream_issue_id": "upstream_issue:show-7",
            "receipt_bindings": [{"receipt_id": "receipt:field-1", "receipt_digest": "b" * 64}],
            "issued_at_utc": "2026-08-25T12:00:00.000Z",
            "deadline_ms": 1000,
        },
        "response": {
            "schema_version": "strathmark-v3-issue-acknowledgment-response-v1",
            "issue_batch_id": "issue_batch:show-7",
            "receipt_ids": ["receipt:field-1"],
            "authority_sequence": 12,
            "recovery_marker_digest": "c" * 64,
        },
    },
    "/v3/results/settle": {
        "request": {
            "schema_version": "strathmark-v3-settlement-request-v1",
            "issue_batch_id": "issue_batch:show-7",
            "receipt_id": "receipt:field-1",
            "results": [
                {
                    "competitor_id": "competitor:alice",
                    "status": "completion",
                    "raw_time_ms": 42321,
                    "penalty_ms": None,
                    "source_revision": 1,
                }
            ],
            "observed_at_utc": "2026-08-25T12:01:00.000Z",
            "deadline_ms": 1000,
        },
        "response": {
            "schema_version": "strathmark-v3-settlement-response-v1",
            "settlement_id": "settlement:show-7",
            "receipt_id": "receipt:field-1",
            "authority_sequence": 13,
            "status": "recorded",
        },
    },
    "/v3/status": {
        "response": {
            "schema_version": "strathmark-v3-status-response-v1",
            "service": "ready",
            "authority_sequence": 13,
            "engine_authority": "v2",
            "v3_readiness": "candidate",
            "production_authority": "v2",
            "cutover_receipt_digest": None,
            "cutover_verified_at_utc": None,
            "deep_verification_state": "verified",
            "event_last_deep_verified_at_utc": "2026-08-25T11:59:00.000Z",
            "event_checkpoint_digest": "a" * 64,
            "field_last_deep_verified_at_utc": "2026-08-25T11:59:00.000Z",
            "field_checkpoint_digest": "b" * 64,
            "job_last_deep_verified_at_utc": "2026-08-25T11:59:00.000Z",
            "job_checkpoint_digest": "c" * 64,
            "open_tournament_count": 0,
        }
    },
    "/v3/credentials/rotate": {
        "request": {
            "schema_version": "strathmark-v3-credential-rotation-request-v1",
            "overlap_seconds": 900,
        },
        "response": {
            "schema_version": "strathmark-v3-credential-rotation-response-v1",
            "credential": "<redacted-one-time-credential-value>",
            "key_id_digest": "d" * 64,
            "principal_id": "actor:tournament-manager",
            "overlap_seconds": 900,
        },
    },
    "/v3/credentials/revoke": {
        "request": {
            "schema_version": "strathmark-v3-credential-revocation-request-v1",
            "key_id_digest": "d" * 64,
        },
        "response": {
            "schema_version": "strathmark-v3-credential-revocation-response-v1",
            "key_id_digest": "d" * 64,
            "status": "revoked",
        },
    },
}


def build_v3_consumer_contract() -> dict[str, Any]:
    """Generate reviewed OpenAPI from live route/model declarations, never stale output."""

    from fastapi import FastAPI

    from strathmark.v3.api.router import V3ApplicationPort, create_router
    from strathmark.v3.api.schemas import ErrorResponse
    from strathmark.v3.contracts.commands import CommandKind

    app = FastAPI(
        title="STRATHMARK V3 Consumer Contract",
        version="3.0.0",
        openapi_version="3.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(
        create_router(
            gateway=cast(V3ApplicationPort, object()),
            credentials=cast(Any, object()),
        )
    )
    document = app.openapi()
    document["jsonSchemaDialect"] = "https://json-schema.org/draft/2020-12/schema"
    document["info"]["x-strathmark-contract-version"] = V3_CONSUMER_CONTRACT_VERSION
    document["info"]["description"] = (
        "Frozen V3 tournament-manager boundary. V2 remains separate and is never a V3 fallback."
    )
    online_commands: list[str] = []
    dedicated_commands = {
        CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
        CommandKind.RECORD_APPROVAL_DECISION,
        CommandKind.SETTLE_LIVE_RACE,
        CommandKind.ROTATE_SERVICE_CREDENTIAL,
        CommandKind.REVOKE_SERVICE_CREDENTIAL,
    }
    offline_commands = {
        CommandKind.BOOTSTRAP_SERVICE_CREDENTIAL,
        CommandKind.RECOVER_SERVICE_CREDENTIAL,
    }
    online_command_set = {CommandKind(value) for value in online_commands}
    internal_commands = set(CommandKind) - (
        online_command_set | dedicated_commands | offline_commands
    )
    document["info"]["x-strathmark-command-coverage"] = {
        "authenticated_application_port": online_commands,
        "dedicated_authenticated_routes": sorted(item.value for item in dedicated_commands),
        "internal_typed_application_services": sorted(item.value for item in internal_commands),
        "listener-stopped-offline-only": sorted(item.value for item in offline_commands),
    }
    document["servers"] = [{"url": "http://127.0.0.1:8787", "description": "Loopback default"}]
    components = document.setdefault("components", {})
    components["securitySchemes"] = {
        "ServiceCredential": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "SMV3",
        },
        "PinnedClientCertificate": {
            "type": "mutualTLS",
            "description": "Required in addition to the service credential off loopback.",
        },
    }
    components.setdefault("schemas", {})["ErrorResponse"] = ErrorResponse.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    components["schemas"].pop("HTTPValidationError", None)
    components["schemas"].pop("ValidationError", None)
    for schema in components["schemas"].values():
        if isinstance(schema, dict) and (schema.get("type") == "object" or "properties" in schema):
            schema["required"] = sorted(schema.get("properties", {}))
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post"}:
                continue
            operation["security"] = [] if path == "/v3/health" else [{"ServiceCredential": []}]
            operation["x-non-loopback-additional-security"] = ["PinnedClientCertificate"]
            if path != "/v3/health":
                operation["parameters"] = [
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": method == "post",
                        "schema": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$",
                        },
                    },
                    {
                        "name": "X-STRATHMARK-Upstream-Actor",
                        "in": "header",
                        "required": False,
                        "description": "Audit metadata only; never authorizes or replaces the service principal.",
                        "schema": {"type": "string", "maxLength": 128},
                    },
                    {
                        "name": "X-STRATHMARK-Upstream-Action",
                        "in": "header",
                        "required": False,
                        "description": "Audit metadata only; never a STRATHMARK permission.",
                        "schema": {"type": "string", "maxLength": 64},
                    },
                    {
                        "name": "X-STRATHMARK-Upstream-Trace",
                        "in": "header",
                        "required": False,
                        "description": "Bounded upstream correlation token; non-authoritative.",
                        "schema": {"type": "string", "maxLength": 128},
                    },
                ]
            examples = _EXAMPLES[path]
            if "request" in examples:
                operation["requestBody"]["content"]["application/json"]["example"] = examples[
                    "request"
                ]
            success = next(code for code in operation["responses"] if code.startswith("2"))
            operation["responses"][success]["content"]["application/json"]["example"] = examples[
                "response"
            ]
            for code, name, message in (
                ("400", "request_rejected", "Request metadata is invalid."),
                (
                    "401",
                    "authentication_failed",
                    "A valid V3 service credential is required.",
                ),
                ("404", "route_not_found", "V3 route was not found."),
                ("405", "method_not_allowed", "HTTP method is not allowed."),
                ("413", "request_body_too_large", "Request body is too large."),
                (
                    "415",
                    "media_type_rejected",
                    "V3 request bodies require application/json.",
                ),
                (
                    "422",
                    "request_validation_failed",
                    "Request does not match the frozen V3 schema.",
                ),
                (
                    "409",
                    "authority_conflict",
                    "Command conflicts with current authority.",
                ),
                (
                    "503",
                    "request_capacity_exhausted",
                    "V3 request capacity is temporarily exhausted.",
                ),
                (
                    "504",
                    "operation_deadline_exceeded",
                    "V3 operation deadline expired.",
                ),
                (
                    "500",
                    "internal_service_error",
                    "V3 operation failed without a trusted result.",
                ),
            ):
                operation["responses"][code] = {
                    "description": message,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                            "example": {
                                "schema_version": "strathmark-v3-error-v1",
                                "code": name,
                                "message": message,
                            },
                        }
                    },
                }
    return document


def v3_consumer_contract_bytes(*, document: dict[str, Any] | None = None) -> bytes:
    if document is None:
        try:
            document = json.loads(_resource_text(_CONTRACT_RESOURCE))
        except (OSError, ValueError, TypeError) as exc:
            raise V3ConsumerContractIntegrityError(
                "Installed V3 consumer contract is missing or malformed."
            ) from exc
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_v3_consumer_contract() -> dict[str, Any]:
    try:
        document = json.loads(_resource_text(_CONTRACT_RESOURCE))
    except (OSError, ValueError, TypeError) as exc:
        raise V3ConsumerContractIntegrityError(
            "Installed V3 consumer contract is missing or malformed."
        ) from exc
    if not isinstance(document, dict):
        raise V3ConsumerContractIntegrityError("Installed V3 consumer contract must be an object.")
    if (document.get("info") or {}).get("x-strathmark-contract-version") != (
        V3_CONSUMER_CONTRACT_VERSION
    ):
        raise V3ConsumerContractIntegrityError("Installed V3 contract version is unsupported.")
    if set(document.get("paths") or {}) != EXPECTED_V3_CONSUMER_PATHS:
        raise V3ConsumerContractIntegrityError("Installed V3 contract route surface changed.")
    v3_consumer_contract_digest(document=document)
    return document


def v3_consumer_contract_digest(*, document: dict[str, Any] | None = None) -> str:
    expected = _resource_text(_CHECKSUM_RESOURCE).strip().lower()
    if _SHA256.fullmatch(expected) is None:
        raise V3ConsumerContractIntegrityError("Installed V3 contract checksum is malformed.")
    observed = hashlib.sha256(v3_consumer_contract_bytes(document=document)).hexdigest()
    if observed != expected:
        raise V3ConsumerContractIntegrityError(
            "Installed V3 consumer contract does not match its reviewed checksum."
        )
    return observed


def _resource_text(resource_name: str) -> str:
    return files("strathmark.v3").joinpath(resource_name).read_text(encoding="utf-8")


__all__ = [
    "EXPECTED_V3_CONSUMER_PATHS",
    "V3_CONSUMER_CONTRACT_VERSION",
    "V3ConsumerContractIntegrityError",
    "build_v3_consumer_contract",
    "load_v3_consumer_contract",
    "v3_consumer_contract_bytes",
    "v3_consumer_contract_digest",
]
