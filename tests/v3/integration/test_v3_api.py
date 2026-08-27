from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import strathmark.v3.api.router as router_module  # noqa: E402
from strathmark.v3.api.app import (  # noqa: E402
    AuthenticatedBoundedMiddleware,
    ListenerSecurityPolicy,
    create_v3_app,
)
from strathmark.v3.api.auth import (  # noqa: E402
    InMemoryCredentialSecretStore,
    ServiceCredentialRegistry,
)
from strathmark.v3.api.router import (  # noqa: E402
    BlockingOperationTimeout,
    BoundedBlockingExecutor,
    RequestContext,
)
from strathmark.v3.api.schemas import (  # noqa: E402
    ApprovalDecisionResponse,
    AssembleFieldRequest,
    AssembleFieldResponse,
    IssueAcknowledgmentRequest,
    IssueAcknowledgmentResponse,
    PrepareCardResponse,
    ReceiptLookupResponse,
    ResultRow,
    SettlementRequest,
    SettlementResponse,
    StatusResponse,
)
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore  # noqa: E402


class Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], RequestContext]] = []
        self.startup_verifications = 0

    def verify_startup(self) -> None:
        self.startup_verifications += 1

    async def prepare_card(self, payload, context):
        self.calls.append(("prepare_card", payload, context))
        return PrepareCardResponse(job_id="job:prepare-1", status="queued", authority_sequence=2)

    async def assemble_field(self, payload, context):
        self.calls.append(("assemble_field", payload, context))
        return AssembleFieldResponse(
            receipt_id="receipt:field-1",
            receipt_digest="a" * 64,
            disposition="prepared",
            canonical_receipt_json='{"schema_version":"strathmark-v3-field-receipt-v1"}',
            authority_sequence=3,
        )

    async def lookup_receipt(self, payload, context):
        self.calls.append(("lookup_receipt", payload, context))
        return ReceiptLookupResponse(
            found=True,
            receipt_id="receipt:field-1",
            receipt_digest="a" * 64,
            canonical_receipt_json='{"schema_version":"strathmark-v3-field-receipt-v1"}',
            authority_sequence=3,
        )

    async def acknowledge_issue(self, payload, context):
        self.calls.append(("acknowledge_issue", payload, context))
        return IssueAcknowledgmentResponse(
            issue_batch_id="issue_batch:one",
            receipt_ids=("receipt:field-1",),
            authority_sequence=4,
            recovery_marker_digest="b" * 64,
        )

    async def record_approval_decision(self, payload, context):
        self.calls.append(("record_approval_decision", payload, context))
        return ApprovalDecisionResponse(
            command_id=str(context.command_id),
            caller_namespace="api",
            tournament_id=payload["tournament_id"],
            snapshot_id=payload["snapshot_id"],
            action=payload["action"],
            decisions=(
                {"receipt_id": "receipt:field-1", "decision_state": "accepted"},
                {"receipt_id": "receipt:field-2", "decision_state": "excluded"},
            ),
            decided_at_utc=payload["decided_at_utc"],
            actor_metadata_digest="b" * 64,
            receipt_bindings_digest="a" * 64,
            command_digest="c" * 64,
            decision_digest="d" * 64,
            authority_sequence=4,
        )

    async def settle_result(self, payload, context):
        self.calls.append(("settle_result", payload, context))
        return SettlementResponse(
            settlement_id="settlement:one",
            receipt_id="receipt:field-1",
            authority_sequence=5,
            status="recorded",
        )

    async def status(self, context):
        self.calls.append(("status", {}, context))
        return StatusResponse(
            service="ready",
            authority_sequence=5,
            engine_authority="v2",
            v3_readiness="candidate",
            production_authority="v2",
            cutover_receipt_digest=None,
            cutover_verified_at_utc=None,
            deep_verification_state="verified",
            event_last_deep_verified_at_utc="2026-08-25T11:59:00.000Z",
            event_checkpoint_digest="a" * 64,
            field_last_deep_verified_at_utc="2026-08-25T11:59:00.000Z",
            field_checkpoint_digest="b" * 64,
            job_last_deep_verified_at_utc="2026-08-25T11:59:00.000Z",
            job_checkpoint_digest="c" * 64,
            open_tournament_count=0,
            v3_option_state="rehearsal_ready",
            rehearsal_eligible=True,
            production_eligible=False,
            eligibility_reason_codes=("production_cutover_not_verified",),
            consumer_contract_version="strathmark.v3-consumer-contract.v6",
            consumer_contract_digest="d" * 64,
            source_commit="c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
        )

    async def open_scope(self, payload, context):
        self.calls.append(("open_scope", payload, context))
        return {
            "scope_id": payload["scope_id"],
            "selection_digest": "1" * 64,
            "authority_sequence": 3,
            "status": "opened",
        }

    async def synchronize_snapshot(self, payload, context):
        self.calls.append(("synchronize_snapshot", payload, context))
        return {
            "entity_id": payload["entity_id"],
            "upstream_revision": payload["upstream_revision"],
            "snapshot_digest": "2" * 64,
            "authority_sequence": 4,
            "status": "synchronized",
        }

    async def freeze_round(self, payload, context):
        self.calls.append(("freeze_round", payload, context))
        return {
            "round_id": payload["round_id"],
            "epoch_id": "epoch:root-1",
            "epoch_revision": payload["epoch_revision"],
            "authority_sequence": 5,
            "status": "frozen",
        }

    async def approval_page(self, payload, context):
        self.calls.append(("approval_page", payload, context))
        return {
            "tournament_id": payload["tournament_id"],
            "snapshot_id": f"approval_snapshot:{'3' * 64}",
            "offset": payload["offset"],
            "limit": payload["limit"],
            "total": 0,
            "lifecycle_state": "preparing",
            "rows": [],
            "authority_sequence": 5,
        }

    async def approval_detail(self, payload, context):
        self.calls.append(("approval_detail", payload, context))
        return {
            "tournament_id": payload["tournament_id"],
            "snapshot_id": payload["snapshot_id"],
            "receipt_id": payload["receipt_id"],
            "detail": {"schema_version": "strathmark-v3-approval-detail-v1"},
            "authority_sequence": 5,
        }

    async def close_round(self, payload, context):
        self.calls.append(("close_round", payload, context))
        return {
            "round_id": payload["round_id"],
            "closure_id": "round_closure:root-1",
            "authority_sequence": 6,
            "status": "closed",
        }

    async def close_scope(self, payload, context):
        self.calls.append(("close_scope", payload, context))
        return {
            "scope_id": payload["scope_id"],
            "authority_sequence": 7,
            "status": "closed",
        }


@pytest.fixture
def api(tmp_path: Path):
    registry = ServiceCredentialRegistry(
        SQLiteEventStore(tmp_path / "api.sqlite3"), InMemoryCredentialSecretStore()
    )
    issued = registry.bootstrap_offline(
        principal_id="actor:tournament-manager",
        listener_stopped=True,
        credential="smv3.api-key.api-secret-1234567890123456",
    )
    gateway = Gateway()
    app = create_v3_app(gateway=gateway, credentials=registry)
    client = TestClient(app, raise_server_exceptions=False)
    return client, gateway, registry, issued


def test_app_runs_explicit_startup_verification(api) -> None:
    _client, gateway, _registry, _issued = api
    assert gateway.startup_verifications == 1


def _headers(credential: str, key: str = "heat-7") -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}", "Idempotency-Key": key}


def _prepare_payload() -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-card-preparation-request-v1",
        "tournament_id": "tournament:show",
        "round_id": "round:heats",
        "field_id": "field:heat-7",
        "competitor_id": "competitor:alice",
        "source_revision": 3,
        "target_context_digest": "a" * 64,
        "deadline_ms": 5000,
    }


def _mtls_files(tmp_path: Path, hostname: str):
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "STRATHMARK test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert = tmp_path / "server.pem"
    key = tmp_path / "server-key.pem"
    ca = tmp_path / "client-ca.pem"
    cert.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ca.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    return cert, key, ca


def test_health_is_public_but_every_trusted_route_authenticates_before_gateway(api) -> None:
    client, gateway, registry, _issued = api
    assert client.get("/v3/health").json() == {
        "schema_version": "strathmark-v3-health-v1",
        "status": "ok",
    }

    registry._authority.events = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("authentication must not query SQLite")
    )
    for header in (None, "Bearer malformed", "Bearer smv3.api-key.wrong-secret-1234567890"):
        headers = {} if header is None else {"Authorization": header, "Idempotency-Key": "x"}
        response = client.post("/v3/cards/prepare", headers=headers, json=_prepare_payload())
        assert response.status_code == 401
        assert response.json()["schema_version"] == "strathmark-v3-error-v1"
    assert gateway.calls == []


def test_authenticated_principal_and_idempotency_are_transport_derived(api) -> None:
    client, gateway, _registry, issued = api
    headers = {
        **_headers(issued.credential),
        "X-STRATHMARK-Upstream-Actor": "operator:judge-7",
        "X-STRATHMARK-Upstream-Action": "marks.approve",
        "X-STRATHMARK-Upstream-Trace": "manager-run-77",
    }
    response = client.post("/v3/cards/prepare", headers=headers, json=_prepare_payload())
    assert response.status_code == 202
    assert response.json()["job_id"] == "job:prepare-1"
    operation, payload, context = gateway.calls[-1]
    assert operation == "prepare_card"
    assert payload["competitor_id"] == "competitor:alice"
    assert str(context.principal.principal_id) == "actor:tournament-manager"
    assert str(context.command_id).startswith("command:")
    assert context.external_idempotency_key == "heat-7"
    assert context.upstream_actor_id == "operator:judge-7"
    assert context.upstream_action == "marks.approve"
    assert context.upstream_trace_id == "manager-run-77"

    # The same caller/key is stable; a different key is materially different.
    first = context.command_id
    client.post("/v3/cards/prepare", headers=_headers(issued.credential), json=_prepare_payload())
    assert gateway.calls[-1][2].command_id == first
    client.post(
        "/v3/cards/prepare",
        headers=_headers(issued.credential, "heat-7-retry-2"),
        json=_prepare_payload(),
    )
    assert gateway.calls[-1][2].command_id != first


def test_upstream_audit_validation_sync_port_and_command_identity_binding(api) -> None:
    client, gateway, _registry, issued = api
    for header, value in (
        ("X-STRATHMARK-Upstream-Actor", "not namespaced"),
        ("X-STRATHMARK-Upstream-Action", "UPPER"),
        ("X-STRATHMARK-Upstream-Trace", "bad trace"),
    ):
        response = client.post(
            "/v3/cards/prepare",
            headers={**_headers(issued.credential), header: value},
            json=_prepare_payload(),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "upstream_audit_invalid"
    assert gateway.calls == []

    gateway.status = lambda _context: StatusResponse(
        service="ready",
        authority_sequence=1,
        engine_authority="v2",
        v3_readiness="candidate",
        production_authority="v2",
        cutover_receipt_digest=None,
        cutover_verified_at_utc=None,
        deep_verification_state="verified",
        event_last_deep_verified_at_utc="2026-08-25T11:59:00.000Z",
        event_checkpoint_digest="a" * 64,
        field_last_deep_verified_at_utc="2026-08-25T11:59:00.000Z",
        field_checkpoint_digest="b" * 64,
        job_last_deep_verified_at_utc="2026-08-25T11:59:00.000Z",
        job_checkpoint_digest="c" * 64,
        open_tournament_count=0,
        v3_option_state="rehearsal_ready",
        rehearsal_eligible=True,
        production_eligible=False,
        eligibility_reason_codes=("production_cutover_not_verified",),
        consumer_contract_version="strathmark.v3-consumer-contract.v6",
        consumer_contract_digest="d" * 64,
        source_commit="c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
    )
    assert client.get("/v3/status", headers=_headers(issued.credential)).status_code == 200

    with pytest.raises(RuntimeError, match="principal"):
        router_module._context(type("Request", (), {"state": object()})())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation",
    [
        {"actor_id": "actor:spoofed"},
        {"source_revision": "3"},
        {"unknown": 1},
        {"deadline_ms": 0},
        {"target_context_digest": "not-a-digest"},
    ],
)
def test_unknown_coerced_spoofed_and_out_of_bounds_inputs_fail_before_gateway(
    api, mutation
) -> None:
    client, gateway, _registry, issued = api
    payload = _prepare_payload()
    payload.update(mutation)
    response = client.post("/v3/cards/prepare", headers=_headers(issued.credential), json=payload)
    assert response.status_code == 422
    assert response.json() == {
        "schema_version": "strathmark-v3-error-v1",
        "code": "request_validation_failed",
        "message": "Request does not match the frozen V3 schema.",
    }
    assert gateway.calls == []


def test_missing_idempotency_and_oversized_body_fail_before_gateway(api) -> None:
    client, gateway, _registry, issued = api
    response = client.post(
        "/v3/cards/prepare",
        headers={"Authorization": f"Bearer {issued.credential}"},
        json=_prepare_payload(),
    )
    assert response.status_code == 400

    response = client.post(
        "/v3/cards/prepare",
        headers=_headers(issued.credential, "unsafe key with spaces"),
        json=_prepare_payload(),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_invalid"

    response = client.post(
        "/v3/cards/prepare",
        headers={
            **_headers(issued.credential),
            "Content-Type": "application/json",
            "Content-Length": "2000000",
        },
        content=b"{}",
    )
    assert response.status_code == 413
    assert gateway.calls == []


def test_media_types_routes_redirects_and_cache_policy_are_fail_closed(api) -> None:
    client, gateway, _registry, issued = api
    headers = _headers(issued.credential)
    wrong_media = client.post(
        "/v3/cards/prepare",
        headers={**headers, "Content-Type": "text/plain"},
        content=b"{}",
    )
    assert wrong_media.status_code == 415
    assert wrong_media.json()["code"] == "media_type_rejected"
    assert gateway.calls == []

    duplicate = client.post(
        "/v3/cards/prepare",
        headers=[
            ("Authorization", f"Bearer {issued.credential}"),
            ("Authorization", "Bearer smv3.other-key.other-secret-123456789012"),
            ("Idempotency-Key", "duplicate-header"),
            ("Content-Type", "application/json"),
        ],
        content=b"{}",
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["code"] == "ambiguous_request_headers"

    trailing = client.post(
        "/v3/cards/prepare/", headers=headers, json=_prepare_payload(), follow_redirects=False
    )
    assert trailing.status_code == 404
    assert "location" not in trailing.headers
    assert trailing.json()["code"] == "route_not_found"

    valid = client.post("/v3/cards/prepare", headers=headers, json=_prepare_payload())
    assert valid.headers["cache-control"] == "no-store"
    assert valid.headers["pragma"] == "no-cache"
    assert valid.headers["x-content-type-options"] == "nosniff"


def test_all_consumer_operations_map_to_separate_application_port_methods(api) -> None:
    client, gateway, _registry, issued = api
    headers = _headers(issued.credential)

    removed = client.post(
        "/v3/commands/execute",
        headers=headers,
        json={"schema_version": "strathmark-v3-command-execution-request-v1"},
    )
    assert removed.status_code == 404
    assert removed.json()["code"] == "route_not_found"
    cases = [
        (
            "/v3/fields/assemble",
            {
                "schema_version": "strathmark-v3-field-assembly-request-v1",
                "field_id": "field:heat-7",
                "upstream_field_revision": 2,
                "ordered_competitor_ids": ["competitor:alice", "competitor:bob"],
                "deadline_ms": 1500,
            },
            "assemble_field",
        ),
        (
            "/v3/receipts/lookup",
            {
                "schema_version": "strathmark-v3-receipt-lookup-request-v1",
                "request_identity": "command:original-request",
                "receipt_id": "receipt:field-1",
                "deadline_ms": 250,
            },
            "lookup_receipt",
        ),
        (
            "/v3/approvals/decide",
            {
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
            "record_approval_decision",
        ),
        (
            "/v3/issues/acknowledge",
            {
                "schema_version": "strathmark-v3-issue-acknowledgment-request-v1",
                "upstream_issue_id": "upstream_issue:show-7",
                "receipt_bindings": [{"receipt_id": "receipt:field-1", "receipt_digest": "a" * 64}],
                "issued_at_utc": "2026-08-25T12:00:00.000Z",
                "deadline_ms": 1000,
            },
            "acknowledge_issue",
        ),
        (
            "/v3/results/settle",
            {
                "schema_version": "strathmark-v3-settlement-request-v1",
                "issue_batch_id": "issue_batch:one",
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
            "settle_result",
        ),
    ]
    for path, payload, expected in cases:
        response = client.post(path, headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert gateway.calls[-1][0] == expected
    assert client.get("/v3/status", headers=headers).status_code == 200
    assert gateway.calls[-1][0] == "status"


def test_public_competition_lifecycle_routes_are_typed_authenticated_and_separate(api) -> None:
    client, gateway, _registry, issued = api
    headers = _headers(issued.credential, "lifecycle-1")
    selection = {
        "schema_version": "strathmark-v3-competition-engine-selection-v1",
        "scope_id": "tournament:show",
        "engine": "v3",
        "mode": "rehearsal",
        "selected_by_actor_id": "actor:tournament-manager",
        "selected_at_utc": "2026-08-25T12:00:00.000Z",
        "reason_code": "new_competition",
        "consumer_contract_digest": "a" * 64,
        "source_commit": "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
    }
    cases = [
        (
            "/v3/scopes/open",
            {
                "schema_version": "strathmark-v3-scope-open-request-v1",
                "scope_id": "tournament:show",
                "bundle_id": "bundle:current",
                "historical_cutoff_key": "history:before-show",
                "root_round_ids": ["round:heats"],
                "engine_selection": selection,
                "opened_at_utc": "2026-08-25T12:00:01.000Z",
                "deadline_ms": 1000,
            },
            "open_scope",
        ),
        (
            "/v3/snapshots/synchronize",
            {
                "schema_version": "strathmark-v3-snapshot-sync-request-v1",
                "entity_kind": "round",
                "entity_id": "round:heats",
                "upstream_revision": 1,
                "tournament_id": "tournament:show",
                "round_id": "round:heats",
                "snapshot": {
                    "round_ordinal": 1,
                    "predecessor_round_ids": [],
                    "successor_round_ids": [],
                },
                "engine_selection": selection,
                "synchronized_at_utc": "2026-08-25T12:00:02.000Z",
                "deadline_ms": 1000,
            },
            "synchronize_snapshot",
        ),
        (
            "/v3/rounds/freeze",
            {
                "schema_version": "strathmark-v3-round-freeze-request-v1",
                "round_id": "round:heats",
                "epoch_revision": 1,
                "historical_cutoff_key": "history:before-show",
                "closure_ids": [],
                "frozen_at_utc": "2026-08-25T12:00:03.000Z",
                "deadline_ms": 1000,
            },
            "freeze_round",
        ),
        (
            "/v3/rounds/close",
            {
                "schema_version": "strathmark-v3-round-close-request-v1",
                "round_id": "round:heats",
                "closed_at_utc": "2026-08-25T12:01:00.000Z",
                "deadline_ms": 1000,
            },
            "close_round",
        ),
        (
            "/v3/scopes/close",
            {
                "schema_version": "strathmark-v3-scope-close-request-v1",
                "scope_id": "tournament:show",
                "closed_at_utc": "2026-08-25T12:02:00.000Z",
                "deadline_ms": 1000,
            },
            "close_scope",
        ),
    ]
    for path, payload, expected in cases:
        response = client.post(path, headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert gateway.calls[-1][0] == expected

    page = client.get(
        "/v3/approvals/page?tournament_id=tournament:show&offset=0&limit=25",
        headers={"Authorization": f"Bearer {issued.credential}"},
    )
    assert page.status_code == 200, page.text
    snapshot_id = page.json()["snapshot_id"]
    detail = client.get(
        "/v3/approvals/detail"
        f"?tournament_id=tournament:show&snapshot_id={snapshot_id}"
        "&receipt_id=receipt:one",
        headers={"Authorization": f"Bearer {issued.credential}"},
    )
    assert detail.status_code == 200, detail.text
    assert gateway.calls[-1][0] == "approval_detail"


def test_snapshot_transport_rejects_names_narrative_and_unknown_identity_fields(api) -> None:
    client, gateway, _registry, issued = api
    payload = {
        "schema_version": "strathmark-v3-snapshot-sync-request-v1",
        "entity_kind": "field",
        "entity_id": "field:one",
        "upstream_revision": 1,
        "tournament_id": "tournament:show",
        "round_id": "round:heats",
        "snapshot": {
            "competitor_ids": ["competitor:one", "competitor:two"],
            "target_context": {
                "event": "standing_block",
                "wood_species": "radiata_pine",
                "wood_class": "softwood",
                "diameter_mm": 300,
                "competition_class": "open",
            },
            "stand_ids": ["stand:one", "stand:two"],
            "display_name": "Private Person",
        },
        "engine_selection": {
            "schema_version": "strathmark-v3-competition-engine-selection-v1",
            "scope_id": "tournament:show",
            "engine": "v3",
            "mode": "rehearsal",
            "selected_by_actor_id": "actor:judge-seven",
            "selected_at_utc": "2026-08-25T12:00:00.000Z",
            "reason_code": "new_competition",
            "consumer_contract_digest": "a" * 64,
            "source_commit": "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
        },
        "synchronized_at_utc": "2026-08-25T12:00:02.000Z",
        "deadline_ms": 1000,
    }
    response = client.post(
        "/v3/snapshots/synchronize",
        headers=_headers(issued.credential, "pii-reject"),
        json=payload,
    )
    assert response.status_code == 422
    assert gateway.calls == []


def test_rotation_and_revocation_routes_have_no_human_role_layer(api) -> None:
    client, _gateway, registry, issued = api
    rotated = client.post(
        "/v3/credentials/rotate",
        headers=_headers(issued.credential, "rotate-1"),
        json={
            "schema_version": "strathmark-v3-credential-rotation-request-v1",
            "overlap_seconds": 120,
        },
    )
    assert rotated.status_code == 200
    assert rotated.headers["cache-control"] == "no-store"
    next_credential = rotated.json()["credential"]
    old_digest = issued.key_id_digest
    revoked = client.post(
        "/v3/credentials/revoke",
        headers=_headers(next_credential, "revoke-1"),
        json={
            "schema_version": "strathmark-v3-credential-revocation-request-v1",
            "key_id_digest": old_digest,
        },
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert registry.authenticate(f"Bearer {next_credential}").principal_id == (
        registry.principal_id
    )
    historical = client.post(
        "/v3/receipts/lookup",
        headers=_headers(next_credential, "historical-recovery"),
        json={
            "schema_version": "strathmark-v3-receipt-lookup-request-v1",
            "request_identity": "command:original-request",
            "receipt_id": "receipt:field-1",
            "deadline_ms": 250,
        },
    )
    assert historical.status_code == 200
    assert historical.json()["receipt_id"] == "receipt:field-1"


def test_credential_write_retries_are_exact_and_changed_retries_conflict(api) -> None:
    client, _gateway, registry, issued = api
    payload = {
        "schema_version": "strathmark-v3-credential-rotation-request-v1",
        "overlap_seconds": 120,
    }
    first = client.post(
        "/v3/credentials/rotate",
        headers=_headers(issued.credential, "rotation-exact-retry"),
        json=payload,
    )
    second = client.post(
        "/v3/credentials/rotate",
        headers=_headers(issued.credential, "rotation-exact-retry"),
        json=payload,
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert registry._authority.event_count() == 2

    changed = client.post(
        "/v3/credentials/rotate",
        headers=_headers(issued.credential, "rotation-exact-retry"),
        json={**payload, "overlap_seconds": 121},
    )
    assert changed.status_code == 409
    assert registry._authority.event_count() == 2

    next_credential = first.json()["credential"]
    revoke_payload = {
        "schema_version": "strathmark-v3-credential-revocation-request-v1",
        "key_id_digest": issued.key_id_digest,
    }
    revoke_first = client.post(
        "/v3/credentials/revoke",
        headers=_headers(next_credential, "revocation-exact-retry"),
        json=revoke_payload,
    )
    revoke_second = client.post(
        "/v3/credentials/revoke",
        headers=_headers(next_credential, "revocation-exact-retry"),
        json=revoke_payload,
    )
    assert revoke_first.status_code == revoke_second.status_code == 200
    assert registry._authority.event_count() == 3


def test_listener_policy_defaults_loopback_and_nonloopback_requires_complete_mtls(
    tmp_path: Path,
) -> None:
    assert ListenerSecurityPolicy().is_loopback is True
    with pytest.raises(ValueError, match="mutual TLS"):
        ListenerSecurityPolicy(host="10.0.0.5")

    cert, key, ca = _mtls_files(tmp_path, "strathmark.venue.internal")
    policy = ListenerSecurityPolicy(
        host="10.0.0.5",
        server_hostname="strathmark.venue.internal",
        tls_certificate=cert,
        tls_private_key=key,
        pinned_client_ca=ca,
    )
    assert policy.is_loopback is False
    assert policy.uvicorn_ssl_kwargs["ssl_cert_reqs"] != 0
    assert len(policy.pinned_client_ca_digest) == 64
    scope = {
        "extensions": {
            "strathmark.verified_client_certificate": {
                "verified": True,
                "server_hostname": "strathmark.venue.internal",
                "principal_id": "actor:tournament-manager",
                "client_ca_digest": policy.pinned_client_ca_digest,
            }
        }
    }
    assert policy.certificate_principal(scope) == "actor:tournament-manager"
    scope["extensions"]["strathmark.verified_client_certificate"]["client_ca_digest"] = "0" * 64
    with pytest.raises(Exception, match="CA binding"):
        policy.certificate_principal(scope)
    with pytest.raises(ValueError, match="hostname"):
        ListenerSecurityPolicy(
            host="10.0.0.5",
            server_hostname="wrong.venue.internal",
            tls_certificate=cert,
            tls_private_key=key,
            pinned_client_ca=ca,
        )
    with pytest.raises(ValueError, match="proxy"):
        ListenerSecurityPolicy(trust_proxy_headers=True)
    with pytest.raises(ValueError, match="redirect"):
        ListenerSecurityPolicy(follow_redirects=True)


def test_listener_and_app_configuration_reject_invalid_bounds_and_certificate_scope(
    api, tmp_path: Path
) -> None:
    _client, gateway, registry, _issued = api
    localhost = ListenerSecurityPolicy(host="localhost")
    assert localhost.is_loopback is True
    assert localhost.uvicorn_ssl_kwargs == {}
    with pytest.raises(ValueError, match="pinned client CA"):
        _ = localhost.pinned_client_ca_digest
    for arguments in ({"host": ""}, {"port": 0}, {"port": True}):
        with pytest.raises(ValueError):
            ListenerSecurityPolicy(**arguments)
    for arguments in (
        {"credentials": object()},
        {"credentials": registry, "max_body_bytes": 1},
        {"credentials": registry, "max_inflight": 0},
        {"credentials": registry, "blocking_max_concurrency": 0},
        {"credentials": registry, "authentication_timeout_ms": 0},
        {"credentials": registry, "credential_operation_timeout_ms": True},
    ):
        with pytest.raises((TypeError, ValueError)):
            create_v3_app(gateway=gateway, **arguments)  # type: ignore[arg-type]

    cert, key, ca = _mtls_files(tmp_path, "strathmark.venue.internal")
    policy = ListenerSecurityPolicy(
        host="service.venue.internal",
        server_hostname="strathmark.venue.internal",
        tls_certificate=cert,
        tls_private_key=key,
        pinned_client_ca=ca,
    )
    assert policy.is_loopback is False
    with pytest.raises(Exception, match="certificate"):
        policy.certificate_principal({})
    with pytest.raises(Exception, match="hostname"):
        policy.certificate_principal(
            {
                "extensions": {
                    "strathmark.verified_client_certificate": {
                        "verified": True,
                        "server_hostname": "wrong.example",
                        "principal_id": "actor:manager",
                        "client_ca_digest": policy.pinned_client_ca_digest,
                    }
                }
            }
        )
    with pytest.raises(Exception, match="principal"):
        policy.certificate_principal(
            {
                "extensions": {
                    "strathmark.verified_client_certificate": {
                        "verified": True,
                        "server_hostname": "strathmark.venue.internal",
                        "principal_id": 7,
                        "client_ca_digest": policy.pinned_client_ca_digest,
                    }
                }
            }
        )


def test_nonloopback_rejects_missing_certificate_wrong_host_and_proxy_before_body(
    api, tmp_path: Path
) -> None:
    _client, gateway, registry, issued = api
    cert, key, ca = _mtls_files(tmp_path, "strathmark.venue.internal")
    policy = ListenerSecurityPolicy(
        host="10.0.0.5",
        server_hostname="strathmark.venue.internal",
        tls_certificate=cert,
        tls_private_key=key,
        pinned_client_ca=ca,
    )
    remote = TestClient(
        create_v3_app(gateway=gateway, credentials=registry, listener=policy),
        base_url="https://strathmark.venue.internal",
        raise_server_exceptions=False,
    )
    response = remote.post(
        "/v3/cards/prepare", headers=_headers(issued.credential), json=_prepare_payload()
    )
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_failed"

    wrong_host = TestClient(
        create_v3_app(gateway=gateway, credentials=registry, listener=policy),
        base_url="https://wrong.venue.internal",
        raise_server_exceptions=False,
    )
    response = wrong_host.post(
        "/v3/cards/prepare", headers=_headers(issued.credential), json=_prepare_payload()
    )
    assert response.status_code == 400
    assert response.json()["code"] == "host_identity_rejected"

    response = remote.post(
        "/v3/cards/prepare",
        headers={**_headers(issued.credential), "Forwarded": "host=attacker.example"},
        json=_prepare_payload(),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "proxy_identity_rejected"
    assert gateway.calls == []


def test_deadline_and_application_contract_failures_use_closed_errors(api) -> None:
    client, gateway, _registry, issued = api

    async def slow(_payload, _context):
        await asyncio.sleep(0.1)
        raise AssertionError("cancelled operation must not finish")

    gateway.prepare_card = slow
    payload = _prepare_payload()
    payload["deadline_ms"] = 25
    response = client.post("/v3/cards/prepare", headers=_headers(issued.credential), json=payload)
    assert response.status_code == 504
    assert response.json() == {
        "schema_version": "strathmark-v3-error-v1",
        "code": "operation_deadline_exceeded",
        "message": "V3 operation deadline expired.",
    }

    async def broken(_payload, _context):
        return {"unexpected": True}

    gateway.prepare_card = broken
    response = client.post(
        "/v3/cards/prepare", headers=_headers(issued.credential), json=_prepare_payload()
    )
    assert response.status_code == 500
    assert response.json()["code"] == "application_contract_violation"

    async def exploded(_payload, _context):
        raise ValueError("private failure detail")

    gateway.prepare_card = exploded
    response = client.post(
        "/v3/cards/prepare", headers=_headers(issued.credential), json=_prepare_payload()
    )
    assert response.status_code == 500
    assert response.json()["code"] == "internal_service_error"
    assert "private failure" not in response.text


def test_bounded_blocking_executor_retains_capacity_after_timeout_or_cancellation() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked() -> str:
        started.set()
        release.wait(timeout=1)
        return "finished"

    async def scenario() -> None:
        executor = BoundedBlockingExecutor(max_concurrency=1)
        pending = asyncio.create_task(executor.run(blocked, timeout_ms=25))
        assert await asyncio.to_thread(started.wait, 0.5)
        with pytest.raises(BlockingOperationTimeout):
            await pending
        with pytest.raises(BlockingOperationTimeout):
            await executor.run(lambda: "must-not-start", timeout_ms=25)
        release.set()
        for _attempt in range(100):
            if executor.active_count == 0:
                break
            await asyncio.sleep(0.005)
        assert executor.active_count == 0
        assert await executor.run(lambda: "recovered", timeout_ms=100) == "recovered"

        started.clear()
        release.clear()
        cancelled = asyncio.create_task(executor.run(blocked, timeout_ms=500))
        assert await asyncio.to_thread(started.wait, 0.5)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert executor.active_count == 1
        with pytest.raises(BlockingOperationTimeout):
            await executor.run(lambda: "must-still-not-start", timeout_ms=25)
        release.set()
        for _attempt in range(100):
            if executor.active_count == 0:
                break
            await asyncio.sleep(0.005)
        assert executor.active_count == 0

    asyncio.run(scenario())


def test_slow_prebody_authentication_does_not_block_public_health_or_read_body(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, _gateway, registry, issued = api
    started = threading.Event()
    release = threading.Event()
    original = registry.authenticate
    body_read = False
    downstream_paths: list[str] = []

    def slow_authenticate(*args, **kwargs):
        started.set()
        release.wait(timeout=1)
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "authenticate", slow_authenticate)

    async def downstream(scope, receive, send):
        downstream_paths.append(str(scope["path"]))
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = AuthenticatedBoundedMiddleware(
        downstream,
        credentials=registry,
        listener=ListenerSecurityPolicy(),
        max_body_bytes=1024,
        max_inflight=2,
        authentication_timeout_ms=500,
        blocking_executor=BoundedBlockingExecutor(max_concurrency=1),
    )

    async def protected_receive():
        nonlocal body_read
        body_read = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def public_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    async def scenario() -> float:
        protected = asyncio.create_task(
            middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v3/cards/prepare",
                    "headers": [
                        (b"authorization", f"Bearer {issued.credential}".encode()),
                        (b"content-type", b"application/json"),
                    ],
                },
                protected_receive,
                send,
            )
        )
        assert await asyncio.to_thread(started.wait, 0.5)
        before = time.monotonic()
        await asyncio.wait_for(
            middleware(
                {"type": "http", "method": "GET", "path": "/v3/health", "headers": []},
                public_receive,
                send,
            ),
            timeout=0.1,
        )
        elapsed = time.monotonic() - before
        assert body_read is False
        release.set()
        await protected
        return elapsed

    assert asyncio.run(scenario()) < 0.1
    assert downstream_paths == ["/v3/health", "/v3/cards/prepare"]


def test_authentication_timeout_fails_closed_before_body_receive(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, _gateway, registry, issued = api
    release = threading.Event()
    original = registry.authenticate
    body_read = False

    def slow_authenticate(*args, **kwargs):
        release.wait(timeout=1)
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "authenticate", slow_authenticate)

    async def downstream(_scope, _receive, _send):
        raise AssertionError("timed-out authentication must not reach the application")

    middleware = AuthenticatedBoundedMiddleware(
        downstream,
        credentials=registry,
        listener=ListenerSecurityPolicy(),
        max_body_bytes=1024,
        max_inflight=1,
        authentication_timeout_ms=25,
        blocking_executor=BoundedBlockingExecutor(max_concurrency=1),
    )

    async def receive():
        nonlocal body_read
        body_read = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def scenario():
        messages = []

        async def send(message):
            messages.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/v3/cards/prepare",
                "headers": [
                    (b"authorization", f"Bearer {issued.credential}".encode()),
                    (b"content-type", b"application/json"),
                ],
            },
            receive,
            send,
        )
        release.set()
        return messages

    messages = asyncio.run(scenario())
    assert messages[0]["status"] == 503
    assert b'"code":"authentication_timeout"' in messages[1]["body"]
    assert body_read is False


def test_timed_out_rotation_and_slow_revoke_do_not_block_health_or_weaken_retry(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client, gateway, registry, issued = api
    started = threading.Event()
    release = threading.Event()
    original = registry.rotate

    def delayed_rotate(*args, **kwargs):
        started.set()
        release.wait(timeout=1)
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "rotate", delayed_rotate)
    app = create_v3_app(
        gateway=gateway,
        credentials=registry,
        blocking_max_concurrency=1,
        authentication_timeout_ms=250,
        credential_operation_timeout_ms=25,
    )
    payload = {
        "schema_version": "strathmark-v3-credential-rotation-request-v1",
        "overlap_seconds": 60,
    }
    with TestClient(app, raise_server_exceptions=False) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                client.post,
                "/v3/credentials/rotate",
                headers=_headers(issued.credential, "slow-rotation"),
                json=payload,
            )
            assert started.wait(timeout=0.5)
            before = time.monotonic()
            health = client.get("/v3/health")
            assert time.monotonic() - before < 0.1
            assert health.status_code == 200
            response = future.result(timeout=1)
        assert response.status_code == 504
        release.set()
        for _attempt in range(100):
            if (
                registry._authority.event_count() == 2
                and app.state.blocking_executor.active_count == 0
            ):
                break
            time.sleep(0.005)
        assert registry._authority.event_count() == 2
        assert app.state.blocking_executor.active_count == 0

    with TestClient(
        create_v3_app(
            gateway=gateway,
            credentials=registry,
            blocking_max_concurrency=1,
            credential_operation_timeout_ms=500,
        ),
        raise_server_exceptions=False,
    ) as retry_client:
        retry = retry_client.post(
            "/v3/credentials/rotate",
            headers=_headers(issued.credential, "slow-rotation"),
            json=payload,
        )
        assert retry.status_code == 200
        next_credential = retry.json()["credential"]

    revoke_started = threading.Event()
    revoke_release = threading.Event()
    original_revoke = registry.revoke

    def delayed_revoke(*args, **kwargs):
        revoke_started.set()
        revoke_release.wait(timeout=1)
        return original_revoke(*args, **kwargs)

    monkeypatch.setattr(registry, "revoke", delayed_revoke)
    with TestClient(
        create_v3_app(
            gateway=gateway,
            credentials=registry,
            blocking_max_concurrency=1,
            credential_operation_timeout_ms=500,
        ),
        raise_server_exceptions=False,
    ) as revoke_client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending_revoke = pool.submit(
                revoke_client.post,
                "/v3/credentials/revoke",
                headers=_headers(next_credential, "slow-revoke"),
                json={
                    "schema_version": "strathmark-v3-credential-revocation-request-v1",
                    "key_id_digest": issued.key_id_digest,
                },
            )
            assert revoke_started.wait(timeout=0.5)
            before = time.monotonic()
            assert revoke_client.get("/v3/health").status_code == 200
            assert time.monotonic() - before < 0.1
            revoke_release.set()
            assert pending_revoke.result(timeout=1).status_code == 200


def test_method_content_encoding_and_reserved_capacity_errors_are_closed(api) -> None:
    client, gateway, registry, issued = api
    response = client.request(
        "GET", "/v3/cards/prepare", headers={"Authorization": f"Bearer {issued.credential}"}
    )
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"
    response = client.post(
        "/v3/cards/prepare",
        headers={
            **_headers(issued.credential),
            "Content-Encoding": "gzip",
            "Content-Type": "application/json",
        },
        content=b"not-read",
    )
    assert response.status_code == 415
    assert response.json()["code"] == "content_encoding_rejected"
    assert gateway.calls == []

    downstream_called = False

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    middleware = AuthenticatedBoundedMiddleware(
        downstream,
        credentials=registry,
        listener=ListenerSecurityPolicy(),
        max_body_bytes=1024,
        max_inflight=1,
    )

    async def exercise_capacity():
        assert await middleware._gate.enter() is True
        messages = []

        async def forbidden_receive():
            raise AssertionError("capacity rejection must precede body receive")

        async def send(message):
            messages.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/v3/cards/prepare",
                "headers": [
                    (b"authorization", f"Bearer {issued.credential}".encode()),
                    (b"content-type", b"application/json"),
                ],
            },
            forbidden_receive,
            send,
        )
        await middleware._gate.leave()
        return messages

    messages = asyncio.run(exercise_capacity())
    assert messages[0]["status"] == 503
    assert downstream_called is False


def test_low_level_stream_failures_reject_before_application_work(api) -> None:
    _client, _gateway, registry, issued = api
    calls: list[str] = []

    async def downstream(scope, receive, send):
        calls.append(str(scope["type"]))
        if scope["type"] == "http":
            await receive()
            await receive()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    middleware = AuthenticatedBoundedMiddleware(
        downstream,
        credentials=registry,
        listener=ListenerSecurityPolicy(),
        max_body_bytes=4,
        max_inflight=1,
    )
    base_scope = {
        "type": "http",
        "method": "POST",
        "path": "/v3/cards/prepare",
        "headers": [
            (b"authorization", f"Bearer {issued.credential}".encode()),
            (b"content-type", b"application/json"),
        ],
    }

    async def run_case(scope, incoming):
        messages = []
        queue = list(incoming)

        async def receive():
            return queue.pop(0)

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    invalid_length = {**base_scope, "headers": [*base_scope["headers"], (b"content-length", b"x")]}
    assert asyncio.run(run_case(invalid_length, []))[0]["status"] == 400
    assert (
        asyncio.run(run_case(base_scope, [{"type": "websocket.receive", "bytes": b"x"}]))[0][
            "status"
        ]
        == 400
    )
    assert (
        asyncio.run(
            run_case(
                base_scope,
                [{"type": "http.request", "body": b"12345", "more_body": False}],
            )
        )[0]["status"]
        == 413
    )
    assert asyncio.run(run_case(base_scope, [{"type": "http.disconnect"}])) == []
    state_invalid = {**base_scope, "state": object()}
    assert asyncio.run(run_case(state_invalid, []))[0]["status"] == 500
    assert (
        asyncio.run(
            run_case(
                base_scope,
                [{"type": "http.request", "body": b"{}", "more_body": False}],
            )
        )[0]["status"]
        == 204
    )

    async def non_http():
        async def receive():
            return {"type": "lifespan.startup"}

        async def send(_message):
            return None

        await middleware({"type": "lifespan"}, receive, send)

    asyncio.run(non_http())
    assert calls == ["http", "lifespan"]


def test_cross_field_transport_contracts_reject_noncanonical_or_inconsistent_values() -> None:
    status = {
        "service": "ready",
        "authority_sequence": 1,
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
        "v3_option_state": "rehearsal_ready",
        "rehearsal_eligible": True,
        "production_eligible": False,
        "eligibility_reason_codes": ("production_cutover_not_verified",),
        "consumer_contract_version": "strathmark.v3-consumer-contract.v6",
        "consumer_contract_digest": "d" * 64,
        "source_commit": "c468e2f59eb42ba1affe0f1669c7a4fb57570d6f",
    }
    StatusResponse.model_validate(status)
    for value in (
        {**status, "engine_authority": "v3"},
        {
            **status,
            "engine_authority": "v3",
            "production_authority": "v3",
            "v3_readiness": "production",
        },
        {**status, "cutover_receipt_digest": "a" * 64},
    ):
        with pytest.raises(Exception):
            StatusResponse.model_validate(value)

    with pytest.raises(Exception, match="unique"):
        AssembleFieldRequest.model_validate(
            {
                "schema_version": "strathmark-v3-field-assembly-request-v1",
                "field_id": "field:one",
                "upstream_field_revision": 1,
                "ordered_competitor_ids": ["competitor:a", "competitor:a"],
                "deadline_ms": 100,
            }
        )
    with pytest.raises(Exception, match="complete"):
        from strathmark.v3.api.schemas import ReceiptLookupResponse

        ReceiptLookupResponse(found=True, authority_sequence=1)
    with pytest.raises(Exception, match="repeat"):
        IssueAcknowledgmentRequest.model_validate(
            {
                "schema_version": "strathmark-v3-issue-acknowledgment-request-v1",
                "upstream_issue_id": "upstream_issue:one",
                "receipt_bindings": [
                    {"receipt_id": "receipt:one", "receipt_digest": "a" * 64},
                    {"receipt_id": "receipt:one", "receipt_digest": "a" * 64},
                ],
                "issued_at_utc": "2026-08-25T12:00:00.000Z",
                "deadline_ms": 100,
            }
        )
    for row in (
        {"status": "completion", "raw_time_ms": None, "penalty_ms": None},
        {"status": "penalty", "raw_time_ms": 10, "penalty_ms": None},
        {"status": "dnf", "raw_time_ms": 10, "penalty_ms": None},
    ):
        with pytest.raises(Exception):
            ResultRow(
                competitor_id="competitor:a",
                source_revision=1,
                **row,
            )
    with pytest.raises(Exception, match="repeat"):
        SettlementRequest.model_validate(
            {
                "schema_version": "strathmark-v3-settlement-request-v1",
                "issue_batch_id": "issue_batch:one",
                "receipt_id": "receipt:one",
                "results": [
                    {
                        "competitor_id": "competitor:a",
                        "status": "completion",
                        "raw_time_ms": 10,
                        "penalty_ms": None,
                        "source_revision": 1,
                    },
                    {
                        "competitor_id": "competitor:a",
                        "status": "completion",
                        "raw_time_ms": 11,
                        "penalty_ms": None,
                        "source_revision": 2,
                    },
                ],
                "observed_at_utc": "2026-08-25T12:00:00.000Z",
                "deadline_ms": 100,
            }
        )
