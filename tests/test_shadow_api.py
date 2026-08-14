"""Authenticated, bounded shadow transport contract tests.

Every test replaces the default ledger with a temporary SQLite database or an
in-memory test double and removes ambient cloud configuration.  No external
service is contacted.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from strathmark.api import (  # noqa: E402
    _SHADOW_MAINTENANCE_SLOTS,
    _SHADOW_OPERATION_SLOTS,
    _SHADOW_RECOVERY_SLOTS,
    TrustedShadowRequestGate,
    app,
    get_ledger,
    get_shadow_service,
    get_store,
)
from strathmark.auth import canonical_shadow_request_digest  # noqa: E402
from strathmark.ledger import (  # noqa: E402
    LedgerConflictError,
    LedgerMonitoringStatus,
    NumericOutcomeRevisionResult,
    PredictionLedger,
)
from strathmark.shadow import (  # noqa: E402
    OBSERVATION_SCHEMA_VERSION,
    RECEIPT_CORE_SCHEMA_VERSION,
    SHADOW_TARGET_SINGLE_ELAPSED,
    ShadowCalculationResult,
    ShadowLiveStatus,
    ShadowPredictionService,
    ShadowReceipt,
    ShadowReceiptCorruptionError,
)
from strathmark.store import (  # noqa: E402
    EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
    EvidenceSnapshotPayload,
    ResultStore,
    canonical_evidence_source_digest,
)

CONSUMER = "missoula:service:shadow"
ACTOR = "missoula:operator:7"
TOKEN = "test-shadow-service-token"
KEY = "test-shadow-attestation-key-with-enough-entropy"
AUDIENCE = "strathmark.shadow.v1"
REQUEST_DIGEST_SCHEMA = "strathmark.shadow-request-digest.v1"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _request_digest(payload: dict) -> str:
    return canonical_shadow_request_digest(payload)


def _attestation(
    action: str,
    revision: str = "missoula:run-revision:1",
    *,
    nonce: str | None = None,
    audience: str = AUDIENCE,
    expires_at: int | None = None,
    consumer_id: str = CONSUMER,
    roles: list[str] | None = None,
    actor_id: str = ACTOR,
    request_payload: dict | None = None,
) -> str:
    now = int(time.time())
    payload = {
        "schema_version": "strathmark.actor-attestation.v2",
        "consumer_id": consumer_id,
        "actor_id": actor_id,
        "roles": roles or ["judge"],
        "action": action,
        "subject_revision": revision,
        "request_digest_schema_version": REQUEST_DIGEST_SCHEMA,
        "request_digest": _request_digest(request_payload or {}),
        "audience": audience,
        "nonce": nonce or f"nonce-{time.time_ns()}",
        "issued_at": now,
        "expires_at": expires_at if expires_at is not None else now + 30,
    }
    encoded = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _b64(hmac.new(KEY.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _headers(
    action: str,
    revision: str = "missoula:run-revision:1",
    *,
    request_payload: dict | None = None,
    **kwargs,
):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-STRATHMARK-Actor-Attestation": _attestation(
            action, revision, request_payload=request_payload, **kwargs
        ),
    }


def _calculate_payload():
    return {
        "schema_version": "strathmark.shadow-calculate.v1",
        "consumer_id": CONSUMER,
        "tournament_id": "missoula:tournament:2027",
        "event_occurrence_id": "missoula:event:225-sb",
        "field_run_id": "missoula:field-run:1",
        "operator_id": ACTOR,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "event_code": "SB",
        "target_contract": SHADOW_TARGET_SINGLE_ELAPSED,
        "prediction_as_of": "2026-11-01",
        "schedule_fingerprint": "1" * 64,
        "observation_schema_version": OBSERVATION_SCHEMA_VERSION,
        "observation_fingerprint": "2" * 64,
        "competitors": [
            {"competitor_id": "missoula:competitor:alice", "gender": "F"},
            {"competitor_id": "missoula:competitor:bob", "gender": "M"},
        ],
        "wood": {"species": "Pine", "diameter_mm": 300, "quality": 7},
        "timeout_ms": 2000,
    }


def _empty_snapshot_store(path, *, captured_at=None, cutoff=date(2026, 11, 1)):
    store = ResultStore(path)
    snapshot_cutoff = cutoff
    observed_at = captured_at or datetime.now(timezone.utc)
    source_id = "test:history-export:empty"
    source_digest = canonical_evidence_source_digest(
        source_id=source_id,
        cutoff=snapshot_cutoff,
        captured_at=observed_at,
        rows=(),
    )
    payload = EvidenceSnapshotPayload(
        schema_version=EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        source_id=source_id,
        cutoff=snapshot_cutoff,
        captured_at=observed_at,
        rows=(),
        source_digest=source_digest,
    )

    class SnapshotSource:
        def load_snapshot(self, *, cutoff):
            assert cutoff == snapshot_cutoff
            return payload

    store.refresh_evidence_snapshot(SnapshotSource(), cutoff=snapshot_cutoff)
    return store


class _ShadowService:
    def __init__(self):
        self.calls = 0

    def calculate(self, request, competitors, wood, **kwargs):
        del kwargs
        self.calls += 1
        core = {
            "schema_version": RECEIPT_CORE_SCHEMA_VERSION,
            "consumer_id": request.consumer_id,
            "request_id": request.request_id,
            "actor_from_transport": request.operator_id,
            "competitor_ids": [row.competitor_id for row in competitors],
            "wood_species": wood.species,
        }
        status = ShadowLiveStatus("recorded", "not-configured", "current", True)
        receipt = ShadowReceipt(
            core_json=json.dumps(core, sort_keys=True, separators=(",", ":")),
            core=core,
            status=status,
        )
        return ShadowCalculationResult(receipt=receipt, status=status)


class _Ledger:
    def __init__(self):
        self.replay_limit = None
        self.outcome_call = None
        self.nonces = set()
        self.dependency_calls = 0
        self.training_count = 0
        self.training_calls = []
        self.status_delay_seconds = 0.0
        self.training_delay_seconds = 0.0
        self.outcome_delay_seconds = 0.0
        self.receipt_delay_seconds = 0.0
        self.receipt_core = None
        self.receipt_calls = 0

    def claim_actor_attestation_nonce(self, **kwargs):
        key = (kwargs["consumer_id"], hashlib.sha256(kwargs["nonce"].encode()).hexdigest())
        if key in self.nonces:
            return False
        self.nonces.add(key)
        return True

    def get_shadow_receipt(
        self,
        caller_id,
        request_id,
        *,
        current_active_fingerprint=None,
        expected_run_revision=None,
        query_deadline=None,
    ):
        self.receipt_calls += 1
        deadline = time.monotonic() + self.receipt_delay_seconds
        while time.monotonic() < deadline:
            if query_deadline is not None and query_deadline.cancelled:
                query_deadline.raise_if_expired()
            time.sleep(0.001)
        if request_id.endswith("missing"):
            return None
        core = self.receipt_core or {
            "schema_version": RECEIPT_CORE_SCHEMA_VERSION,
            "consumer_id": caller_id,
            "request_id": request_id,
            "run_revision": "missoula:run-revision:1",
        }
        if expected_run_revision is not None and core["run_revision"] != expected_run_revision:
            raise LedgerConflictError("run_revision does not match receipt")
        freshness = "current" if current_active_fingerprint != "f" * 64 else "stale"
        status = ShadowLiveStatus("recorded", "not-configured", freshness, freshness == "current")
        return ShadowReceipt(json.dumps(core), core, status)

    def get_monitoring_status(self, **kwargs):
        query_deadline = kwargs.get("query_deadline")
        deadline = time.monotonic() + self.status_delay_seconds
        while time.monotonic() < deadline:
            if query_deadline is not None and query_deadline.cancelled:
                query_deadline.raise_if_expired()
            time.sleep(0.001)
        if (
            "validated_receipt" not in kwargs
            and kwargs.get("caller_id") is not None
            and kwargs.get("request_id") is not None
        ):
            self.get_shadow_receipt(
                kwargs["caller_id"],
                kwargs["request_id"],
                current_active_fingerprint=kwargs.get("current_active_fingerprint"),
                expected_run_revision=kwargs.get("expected_run_revision"),
            )
        del kwargs
        return LedgerMonitoringStatus(
            mirror="not-configured",
            mirror_pending_count=0,
            mirror_oldest_pending_at=None,
            mirror_last_attempt_at=None,
            local_trust="recorded",
            receipt_freshness="current",
            receipt_readiness="ready",
            numeric_mirror="not-configured",
            numeric_mirror_backlog_count=0,
            numeric_mirror_oldest_pending_at=None,
            numeric_mirror_last_attempt_at=None,
            numeric_revision_count=0,
            active_numeric_settlement_count=0,
            voided_prediction_count=0,
            evidence_sample_count=0,
            evidence_status="insufficient-evidence",
            drift_calibration_advisory="insufficient-evidence",
        )

    def apply_numeric_outcome_revision(
        self,
        outcome_revision_id,
        revisions,
        *,
        caller_id,
        request_id,
        run_revision,
        actor,
        reason_code,
        query_deadline=None,
    ):
        deadline = time.monotonic() + self.outcome_delay_seconds
        while time.monotonic() < deadline:
            if query_deadline is not None and query_deadline.cancelled:
                query_deadline.raise_if_expired()
            time.sleep(0.001)
        self.outcome_call = (
            outcome_revision_id,
            revisions,
            caller_id,
            request_id,
            run_revision,
            actor,
            reason_code,
        )
        return NumericOutcomeRevisionResult(
            outcome_revision_id=outcome_revision_id,
            ledger_request_id="ledger-request",
            caller_id=CONSUMER,
            revisions=(),
            actor=actor,
            reason_code=reason_code,
            created_at="2026-08-13T00:00:00+00:00",
        )

    def flush_mirror_outbox(self, *, limit, caller_id):
        self.replay_limit = (limit, caller_id)
        return {"recorded": 0, "failed": 0, "not_configured": 0}

    def count_training_rows(self, **kwargs):
        self.training_calls.append(("count", kwargs))
        query_deadline = kwargs.get("query_deadline")
        deadline = time.monotonic() + self.training_delay_seconds
        while time.monotonic() < deadline:
            if query_deadline is not None and query_deadline.cancelled:
                query_deadline.raise_if_expired()
            time.sleep(0.001)
        return self.training_count

    def get_training_rows(self, **kwargs):
        self.training_calls.append(("rows", kwargs))
        assert kwargs.get("caller_id") == CONSUMER
        assert kwargs.get("limit") is not None
        return []


@pytest.fixture
def shadow_client(monkeypatch, tmp_path):
    for name in (
        "STRATHMARK_SUPABASE_URL",
        "STRATHMARK_SUPABASE_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STRATHMARK_SHADOW_SERVICE_CREDENTIALS", json.dumps({CONSUMER: TOKEN}))
    monkeypatch.setenv("STRATHMARK_SHADOW_ATTESTATION_KEYS", json.dumps({CONSUMER: KEY}))
    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "offline-single-writer-durable")
    service = _ShadowService()
    ledger = _Ledger()
    ledger.path = tmp_path / "shadow-api-ledger.db"
    initialized_ledger = PredictionLedger(ledger.path)

    def cached_persistence_health():
        if ledger.path == initialized_ledger.path:
            return initialized_ledger.cached_persistence_health()
        return {
            "configured_as_memory": False,
            "path_exists": ledger.path.is_file(),
            "readable": False,
            "writable": False,
            "read_write_open_observed": False,
            "persistence_observed": False,
            "assurance": "unverified",
        }

    ledger.cached_persistence_health = cached_persistence_health
    snapshot_store = _empty_snapshot_store(tmp_path / "shadow-api-results.db")
    ledger.snapshot_store = snapshot_store
    monkeypatch.setenv("STRATHMARK_DB_PATH", str(ledger.path))
    app.dependency_overrides[get_shadow_service] = lambda: service
    app.dependency_overrides[get_ledger] = lambda: ledger
    app.dependency_overrides[get_store] = lambda: snapshot_store
    try:
        yield TestClient(app), service, ledger
    finally:
        app.dependency_overrides.clear()


def test_shadow_calculate_requires_both_scoped_credentials(shadow_client):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    assert client.post("/v1/shadow/calculate", json=payload).status_code == 401
    only_service = {"Authorization": f"Bearer {TOKEN}"}
    assert (
        client.post("/v1/shadow/calculate", json=payload, headers=only_service).status_code == 401
    )
    wrong_scope = dict(payload, consumer_id="other:service:shadow")
    response = client.post(
        "/v1/shadow/calculate", json=wrong_scope, headers=_headers("shadow.calculate")
    )
    assert response.status_code == 403
    assert service.calls == 0


def test_actor_attestation_binds_the_exact_validated_request_before_nonce_claim(shadow_client):
    client, service, ledger = shadow_client
    signed = _calculate_payload()
    tampered = dict(signed, schedule_fingerprint="f" * 64)
    headers = _headers(
        "shadow.calculate",
        request_payload=signed,
        nonce="request-digest-tamper-001",
    )

    response = client.post("/v1/shadow/calculate", json=tampered, headers=headers)

    assert response.status_code == 403
    assert service.calls == 0
    assert not ledger.nonces


def test_shadow_authentication_rejects_bearer_reused_as_attestation_secret(
    shadow_client, monkeypatch
):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    monkeypatch.setenv("STRATHMARK_SHADOW_ATTESTATION_KEYS", json.dumps({CONSUMER: TOKEN}))

    response = client.post(
        "/v1/shadow/calculate",
        json=payload,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-STRATHMARK-Actor-Attestation": _attestation(
                "shadow.calculate", request_payload=payload
            ),
        },
    )

    assert response.status_code == 503
    assert service.calls == 0


def test_early_service_preauth_compares_every_configured_token(monkeypatch):
    import strathmark.auth as auth_module

    second_token = "other-shadow-service-token"
    monkeypatch.setenv(
        "STRATHMARK_SHADOW_SERVICE_CREDENTIALS",
        json.dumps({CONSUMER: TOKEN, "other:service:shadow": second_token}),
    )
    monkeypatch.setenv(
        "STRATHMARK_SHADOW_ATTESTATION_KEYS",
        json.dumps(
            {
                CONSUMER: KEY,
                "other:service:shadow": "other-shadow-attestation-key",
            }
        ),
    )
    real_compare = hmac.compare_digest
    comparisons = []

    def tracking_compare(left, right):
        comparisons.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(auth_module.hmac, "compare_digest", tracking_compare)
    auth_module.preauthenticate_shadow_service(f"Bearer {TOKEN}")

    assert comparisons == [(TOKEN, TOKEN), (TOKEN, second_token)]


def test_auth_uses_the_stdlib_only_identity_module():
    import strathmark.auth as auth_module

    assert auth_module.validate_namespaced_identity.__module__ == "strathmark.identity"


def test_trusted_shadow_gate_accepts_starlette_app_keyword():
    async def downstream(scope, receive, send):
        del scope, receive, send

    gate = TrustedShadowRequestGate(app=downstream)

    assert gate.application is downstream


def test_early_asgi_gate_rejects_non_ascii_bearer_without_an_exception(shadow_client):
    del shadow_client
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/shadow/calculate",
        "headers": [
            (b"content-length", b"2"),
            (b"authorization", f"Bearer {TOKEN}".encode("ascii") + b"\xc3\xa9"),
        ],
    }
    asyncio.run(TrustedShadowRequestGate(app)(scope, receive, send))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


def test_early_asgi_gate_treats_non_ascii_configured_secret_as_invalid(shadow_client, monkeypatch):
    del shadow_client
    monkeypatch.setenv(
        "STRATHMARK_SHADOW_SERVICE_CREDENTIALS",
        json.dumps({CONSUMER: f"{TOKEN}\N{LATIN SMALL LETTER E WITH ACUTE}"}),
    )
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/shadow/calculate",
        "headers": [
            (b"content-length", b"2"),
            (b"authorization", f"Bearer {TOKEN}".encode("ascii")),
        ],
    }
    asyncio.run(TrustedShadowRequestGate(app)(scope, receive, send))

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 503


@pytest.mark.parametrize(
    ("header_kwargs", "expected"),
    [
        ({"audience": "wrong-audience"}, 403),
        ({"expires_at": 1}, 401),
    ],
)
def test_shadow_attestation_rejects_wrong_audience_and_expiry(
    shadow_client, header_kwargs, expected
):
    client, service, _ = shadow_client
    response = client.post(
        "/v1/shadow/calculate",
        json=_calculate_payload(),
        headers=_headers("shadow.calculate", **header_kwargs),
    )
    assert response.status_code == expected
    assert service.calls == 0


def test_shadow_attestation_rejects_bad_signature_and_non_namespaced_actor(shadow_client):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    token = _attestation("shadow.calculate")
    encoded, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    bad_signature = {
        "Authorization": f"Bearer {TOKEN}",
        "X-STRATHMARK-Actor-Attestation": f"{encoded}.{replacement}{signature[1:]}",
    }
    assert (
        client.post("/v1/shadow/calculate", json=payload, headers=bad_signature).status_code == 401
    )
    assert (
        client.post(
            "/v1/shadow/calculate",
            json=payload,
            headers=_headers("shadow.calculate", actor_id="Display Name"),
        ).status_code
        == 401
    )
    assert service.calls == 0


def test_shadow_attestation_binds_action_revision_and_is_single_use(shadow_client):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    wrong_action = client.post(
        "/v1/shadow/calculate", json=payload, headers=_headers("shadow.receipt.lookup")
    )
    assert wrong_action.status_code == 403
    wrong_revision = client.post(
        "/v1/shadow/calculate",
        json=payload,
        headers=_headers("shadow.calculate", "missoula:run-revision:2"),
    )
    assert wrong_revision.status_code == 403
    headers = _headers("shadow.calculate", request_payload=payload, nonce="one-use-nonce-001")
    assert client.post("/v1/shadow/calculate", json=payload, headers=headers).status_code == 200
    assert client.post("/v1/shadow/calculate", json=payload, headers=headers).status_code == 409
    assert service.calls == 1


def test_shadow_calculate_binds_verified_actor_and_rejects_unknown_or_oversized_input(
    shadow_client,
):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    payload["operator_id"] = "missoula:operator:spoofed"
    assert (
        client.post(
            "/v1/shadow/calculate", json=payload, headers=_headers("shadow.calculate")
        ).status_code
        == 403
    )
    payload = _calculate_payload()
    payload["unknown"] = "x"
    assert (
        client.post(
            "/v1/shadow/calculate", json=payload, headers=_headers("shadow.calculate")
        ).status_code
        == 422
    )
    payload = _calculate_payload()
    payload["competitors"] *= 33
    assert (
        client.post(
            "/v1/shadow/calculate", json=payload, headers=_headers("shadow.calculate")
        ).status_code
        == 422
    )
    assert service.calls == 0


def test_shadow_calculate_rejects_non_namespaced_field_identity_before_service(shadow_client):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    payload["tournament_id"] = "local-integer-7"
    response = client.post(
        "/v1/shadow/calculate", json=payload, headers=_headers("shadow.calculate")
    )
    assert response.status_code == 422
    assert service.calls == 0


def test_trusted_contract_rejects_display_name_and_free_text_context(shadow_client):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    payload["competitors"][0]["name"] = "Alice Example"
    response = client.post(
        "/v1/shadow/calculate", json=payload, headers=_headers("shadow.calculate")
    )
    assert response.status_code == 422
    payload = _calculate_payload()
    payload["competitors"][0]["fatigue_notes"] = "medical/free-text data"
    response = client.post(
        "/v1/shadow/calculate", json=payload, headers=_headers("shadow.calculate")
    )
    assert response.status_code == 422
    assert service.calls == 0


def test_receipt_lookup_and_live_status_are_separate_versioned_views(shadow_client):
    client, _, _ = shadow_client
    lookup = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "current_active_fingerprint": "f" * 64,
    }
    receipt = client.post(
        "/v1/shadow/receipts/lookup",
        json=lookup,
        headers=_headers("shadow.receipt.lookup", request_payload=lookup),
    )
    assert receipt.status_code == 200
    assert receipt.json()["receipt"]["core"]["request_id"] == "missoula:request:1"
    assert receipt.json()["receipt"]["status"]["freshness"] == "invalid"
    assert receipt.json()["receipt"]["status"]["ready_for_review"] is False

    status_payload = {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "current_active_fingerprint": "1" * 64,
    }
    status = client.post(
        "/v1/shadow/status",
        json=status_payload,
        headers=_headers("shadow.status.read", request_payload=status_payload),
    )
    assert status.status_code == 200
    assert status.json()["status"]["local_trust"] == "recorded"
    assert status.json()["status"]["drift_calibration_advisory"] == "insufficient-evidence"


def test_status_maps_persisted_receipt_corruption_to_closed_integrity_conflict(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
    }

    def corrupt_status(**kwargs):
        del kwargs
        raise ShadowReceiptCorruptionError("sensitive persisted corruption detail")

    ledger.get_monitoring_status = corrupt_status
    response = client.post(
        "/v1/shadow/status",
        json=payload,
        headers=_headers("shadow.status.read", request_payload=payload),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Persisted shadow receipt failed integrity checks."}
    assert "sensitive" not in response.text


def test_receipt_lookup_has_shared_capacity_timeout_and_cooperative_slot_recovery(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "timeout_ms": 25,
    }
    assert _SHADOW_RECOVERY_SLOTS.acquire(blocking=False)
    assert _SHADOW_RECOVERY_SLOTS.acquire(blocking=False)
    try:
        busy = client.post(
            "/v1/shadow/receipts/lookup",
            json=payload,
            headers=_headers("shadow.receipt.lookup", request_payload=payload),
        )
        assert busy.status_code == 429
    finally:
        _SHADOW_RECOVERY_SLOTS.release()
        _SHADOW_RECOVERY_SLOTS.release()

    assert _SHADOW_RECOVERY_SLOTS.acquire(blocking=False)
    try:
        ledger.receipt_delay_seconds = 0.25
        timed_out = client.post(
            "/v1/shadow/receipts/lookup",
            json=payload,
            headers=_headers("shadow.receipt.lookup", request_payload=payload),
        )
        assert timed_out.status_code == 504
        assert "retry the exact receipt lookup" in timed_out.json()["detail"].lower()
        ledger.receipt_delay_seconds = 0.0
        recovered = client.post(
            "/v1/shadow/receipts/lookup",
            json=payload,
            headers=_headers("shadow.receipt.lookup", request_payload=payload),
        )
        assert recovered.status_code == 200
    finally:
        _SHADOW_RECOVERY_SLOTS.release()


def test_receipt_lookup_sqlite_lock_times_out_and_releases_worker_slot(shadow_client, tmp_path):
    client, _, _ = shadow_client
    path = tmp_path / "locked-receipt-lookup.db"
    durable = PredictionLedger(path)

    class LockedLookupLedger(_Ledger):
        def __init__(self):
            super().__init__()
            self.path = path

        def get_shadow_receipt(self, *args, **kwargs):
            return durable.get_shadow_receipt(*args, **kwargs)

    ledger = LockedLookupLedger()
    app.dependency_overrides[get_ledger] = lambda: ledger
    lock = sqlite3.connect(path, timeout=1.0, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    payload = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:locked",
        "run_revision": "missoula:run-revision:locked",
        "timeout_ms": 25,
    }
    try:
        timed_out = client.post(
            "/v1/shadow/receipts/lookup",
            json=payload,
            headers=_headers(
                "shadow.receipt.lookup",
                revision=payload["run_revision"],
                request_payload=payload,
            ),
        )
        assert timed_out.status_code == 504
    finally:
        lock.rollback()
        lock.close()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        acquired = []
        for _ in range(2):
            if _SHADOW_RECOVERY_SLOTS.acquire(blocking=False):
                acquired.append(True)
        if len(acquired) == 2:
            for _ in acquired:
                _SHADOW_RECOVERY_SLOTS.release()
            break
        for _ in acquired:
            _SHADOW_RECOVERY_SLOTS.release()
        time.sleep(0.01)
    else:
        pytest.fail("receipt lookup worker did not eventually release shared capacity")


@pytest.mark.parametrize(
    ("endpoint", "action", "schema_version"),
    [
        (
            "/v1/shadow/receipts/lookup",
            "shadow.receipt.lookup",
            "strathmark.shadow-receipt-lookup.v1",
        ),
        ("/v1/shadow/status", "shadow.status.read", "strathmark.shadow-status.v1"),
    ],
)
def test_shadow_recovery_result_store_writer_lock_is_bounded_and_releases_recovery_capacity(
    shadow_client, endpoint, action, schema_version
):
    client, _, ledger = shadow_client
    store = ledger.snapshot_store
    lock = sqlite3.connect(store.path, timeout=1.0, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    payload = {
        "schema_version": schema_version,
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "timeout_ms": 25,
    }
    try:
        timed_out = client.post(
            endpoint,
            json=payload,
            headers=_headers(action, request_payload=payload),
        )
        # SQLite rollback-journal builds block this reader and return the
        # bounded 504; WAL-style builds allow the immutable read to complete.
        # Both are safe, but neither may strand recovery capacity.
        assert timed_out.status_code in {200, 504}
    finally:
        lock.rollback()
        lock.close()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        acquired = []
        for _ in range(2):
            if _SHADOW_RECOVERY_SLOTS.acquire(blocking=False):
                acquired.append(True)
        if len(acquired) == 2:
            for _ in acquired:
                _SHADOW_RECOVERY_SLOTS.release()
            break
        for _ in acquired:
            _SHADOW_RECOVERY_SLOTS.release()
        time.sleep(0.01)
    else:
        pytest.fail("result-store lookup worker did not release critical capacity")

    recovered = client.post(
        endpoint, json=payload, headers=_headers(action, request_payload=payload)
    )
    assert recovered.status_code == 200


def test_recovery_views_do_not_treat_an_omitted_caller_fingerprint_as_current(shadow_client):
    client, _, _ = shadow_client
    lookup = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
    }
    receipt = client.post(
        "/v1/shadow/receipts/lookup",
        json=lookup,
        headers=_headers("shadow.receipt.lookup", request_payload=lookup),
    )
    assert receipt.status_code == 200
    assert receipt.json()["receipt"]["status"] == {
        "trust": "recorded",
        "mirror": "not-configured",
        "freshness": "invalid",
        "ready_for_review": False,
    }

    status_payload = {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
    }
    status = client.post(
        "/v1/shadow/status",
        json=status_payload,
        headers=_headers("shadow.status.read", request_payload=status_payload),
    )
    assert status.status_code == 200
    assert status.json()["status"]["receipt_freshness"] == "invalid"
    assert status.json()["status"]["receipt_readiness"] == "not-ready"


def test_lookup_without_caller_fingerprint_detects_a_server_side_evidence_refresh(shadow_client):
    client, _, ledger = shadow_client
    store = ledger.snapshot_store
    recorded = store.get_evidence_snapshot_status()
    assert recorded is not None
    ledger.receipt_core = {
        "schema_version": RECEIPT_CORE_SCHEMA_VERSION,
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "prediction_as_of": recorded.cutoff.isoformat(),
        "active_input": {
            "fingerprint": "a" * 64,
            "evidence_snapshot": recorded.input_projection(),
        },
    }
    captured_at = recorded.captured_at + timedelta(seconds=1)
    source_id = "test:history-export:route-refresh"
    source_digest = canonical_evidence_source_digest(
        source_id=source_id,
        cutoff=recorded.cutoff,
        captured_at=captured_at,
        rows=(),
    )
    refreshed_payload = EvidenceSnapshotPayload(
        schema_version=EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
        source_id=source_id,
        cutoff=recorded.cutoff,
        captured_at=captured_at,
        rows=(),
        source_digest=source_digest,
    )

    class RefreshedSource:
        def load_snapshot(self, *, cutoff):
            assert cutoff == recorded.cutoff
            return refreshed_payload

    store.refresh_evidence_snapshot(
        RefreshedSource(),
        cutoff=recorded.cutoff,
        expected_active_snapshot_digest=recorded.snapshot_digest,
    )
    request_payload = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
    }

    response = client.post(
        "/v1/shadow/receipts/lookup",
        json=request_payload,
        headers=_headers("shadow.receipt.lookup", request_payload=request_payload),
    )

    assert response.status_code == 200
    assert response.json()["receipt"]["status"]["freshness"] == "stale"
    assert response.json()["receipt"]["status"]["ready_for_review"] is False

    status_payload = {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
    }
    status = client.post(
        "/v1/shadow/status",
        json=status_payload,
        headers=_headers("shadow.status.read", request_payload=status_payload),
    )
    assert status.status_code == 200
    assert status.json()["status"]["receipt_freshness"] == "stale"
    assert status.json()["status"]["receipt_readiness"] == "not-ready"


@pytest.mark.parametrize(
    "route,action",
    [
        ("/v1/shadow/receipts/lookup", "shadow.receipt.lookup"),
        ("/v1/shadow/status", "shadow.status.read"),
    ],
)
def test_recovery_views_reject_a_different_signed_run_revision(shadow_client, route, action):
    client, _, _ = shadow_client
    payload = {
        "schema_version": (
            "strathmark.shadow-receipt-lookup.v1"
            if "receipts" in route
            else "strathmark.shadow-status.v1"
        ),
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:2",
    }
    response = client.post(
        route,
        json=payload,
        headers=_headers(
            action,
            "missoula:run-revision:2",
            request_payload=payload,
        ),
    )
    assert response.status_code == 409
    assert "run_revision" in response.json()["detail"]


def test_numeric_outcome_uses_attested_actor_and_expected_revisions(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": "missoula:outcome-revision:1",
        "reason_code": None,
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "settle",
                "actual_time": 41.25,
                "expected_revision": 0,
            }
        ],
    }
    response = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers(
            "shadow.outcome.apply",
            "missoula:run-revision:1",
            request_payload=payload,
        ),
    )
    assert response.status_code == 200
    assert ledger.outcome_call[2] == CONSUMER
    assert ledger.outcome_call[3] == "missoula:request:1"
    assert ledger.outcome_call[4] == "missoula:run-revision:1"
    assert ledger.outcome_call[5] == ACTOR
    assert ledger.outcome_call[1][0]["expected_revision"] == 0


def test_numeric_outcome_timeout_is_ambiguous_and_exact_retry_is_recoverable(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": "missoula:outcome-revision:timeout-1",
        "reason_code": None,
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "settle",
                "actual_time": 41.25,
                "expected_revision": 0,
            }
        ],
        "timeout_ms": 25,
    }
    ledger.outcome_delay_seconds = 0.25

    timed_out = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers("shadow.outcome.apply", request_payload=payload),
    )

    assert timed_out.status_code == 504
    detail = timed_out.json()["detail"]
    assert payload["outcome_revision_id"] in detail
    assert "recover" in detail.lower()
    assert "retry" in detail.lower()
    ledger.outcome_delay_seconds = 0.0

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        acquired = []
        for _ in range(2):
            if _SHADOW_OPERATION_SLOTS.acquire(blocking=False):
                acquired.append(True)
        if len(acquired) == 2:
            for _ in acquired:
                _SHADOW_OPERATION_SLOTS.release()
            break
        for _ in acquired:
            _SHADOW_OPERATION_SLOTS.release()
        time.sleep(0.01)
    else:
        pytest.fail("numeric outcome worker did not eventually release shared capacity")

    retried = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers("shadow.outcome.apply", request_payload=payload),
    )
    assert retried.status_code == 200


def test_numeric_void_requires_a_reason_before_the_ledger_is_called(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": "missoula:outcome-revision:void-without-reason",
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "void",
                "actual_time": None,
                "expected_revision": 1,
            }
        ],
    }

    response = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers("shadow.outcome.apply", request_payload=payload),
    )

    assert response.status_code == 422
    assert ledger.outcome_call is None


def test_numeric_correction_requires_a_reason_before_the_ledger_is_called(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": "missoula:outcome-revision:correction-without-reason",
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "settle",
                "actual_time": 41.5,
                "expected_revision": 1,
            }
        ],
    }

    response = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers("shadow.outcome.apply", request_payload=payload),
    )

    assert response.status_code == 422
    assert ledger.outcome_call is None


def test_state_changing_shadow_routes_use_the_correct_local_write_readiness(
    shadow_client, monkeypatch, tmp_path
):
    client, service, ledger = shadow_client
    monkeypatch.delenv("STRATHMARK_TRUSTED_TOPOLOGY", raising=False)
    calculate = _calculate_payload()
    blocked_calculate = client.post(
        "/v1/shadow/calculate",
        json=calculate,
        headers=_headers("shadow.calculate", request_payload=calculate),
    )
    assert blocked_calculate.status_code == 503, blocked_calculate.text
    assert service.calls == 0

    outcome = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": "missoula:outcome-revision:blocked",
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "settle",
                "actual_time": 41.25,
                "expected_revision": 0,
            }
        ],
    }
    blocked_outcome = client.post(
        "/v1/shadow/outcomes/apply",
        json=outcome,
        headers=_headers("shadow.outcome.apply", request_payload=outcome),
    )
    assert blocked_outcome.status_code == 503
    assert ledger.outcome_call is None

    lookup = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
    }
    recovery = client.post(
        "/v1/shadow/receipts/lookup",
        json=lookup,
        headers=_headers("shadow.receipt.lookup", request_payload=lookup),
    )
    assert recovery.status_code == 200

    replay_payload = {
        "schema_version": "strathmark.shadow-mirror-replay.v1",
        "consumer_id": CONSUMER,
        "run_revision": "missoula:run-revision:1",
        "limit": 1,
    }
    replay = client.post(
        "/v1/shadow/mirror/replay",
        json=replay_payload,
        headers=_headers(
            "shadow.mirror.replay",
            request_payload=replay_payload,
            roles=["admin"],
        ),
    )
    assert replay.status_code == 200

    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "offline-single-writer-durable")
    original_path = ledger.path
    ledger.path = tmp_path / "missing-ledger.db"
    missing_ledger = client.post(
        "/v1/shadow/calculate",
        json=calculate,
        headers=_headers("shadow.calculate", request_payload=calculate),
    )
    assert missing_ledger.status_code == 503
    assert service.calls == 0
    ledger.path = original_path

    missing_store = ResultStore(tmp_path / "missing-evidence.db")
    app.dependency_overrides[get_store] = lambda: missing_store
    missing_evidence = client.post(
        "/v1/shadow/outcomes/apply",
        json=outcome,
        headers=_headers("shadow.outcome.apply", request_payload=outcome),
    )
    assert missing_evidence.status_code == 200
    assert ledger.outcome_call is not None

    status_payload = {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
    }
    degraded_status = client.post(
        "/v1/shadow/status",
        json=status_payload,
        headers=_headers("shadow.status.read", request_payload=status_payload),
    )
    assert degraded_status.status_code == 200
    assert degraded_status.json()["status"]["receipt_readiness"] == "not-ready"


@pytest.mark.parametrize("evidence_state", ["missing", "stale"])
def test_receipt_bound_numeric_outcomes_remain_available_without_current_evidence(
    shadow_client, monkeypatch, tmp_path, evidence_state
):
    client, _, ledger = shadow_client
    if evidence_state == "missing":
        evidence_store = ResultStore(tmp_path / "missing-outcome-evidence.db")
    else:
        evidence_store = _empty_snapshot_store(
            tmp_path / "stale-outcome-evidence.db",
            captured_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        assert evidence_store.get_evidence_snapshot_status().ready_for_offline is False
    app.dependency_overrides[get_store] = lambda: evidence_store
    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "offline-single-writer-durable")
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": f"missoula:outcome-revision:{evidence_state}",
        "reason_code": None,
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "settle",
                "actual_time": 41.25,
                "expected_revision": 0,
            }
        ],
    }

    response = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers("shadow.outcome.apply", request_payload=payload),
    )

    assert response.status_code == 200
    assert ledger.outcome_call[0] == payload["outcome_revision_id"]


def test_write_readiness_fails_before_nonce_claim_and_same_attestation_can_retry(
    shadow_client, monkeypatch
):
    client, service, ledger = shadow_client
    calculate = _calculate_payload()
    calculate_headers = _headers(
        "shadow.calculate",
        request_payload=calculate,
        nonce="readiness-calculate-retry-001",
    )
    monkeypatch.delenv("STRATHMARK_TRUSTED_TOPOLOGY", raising=False)
    blocked_calculate = client.post(
        "/v1/shadow/calculate", json=calculate, headers=calculate_headers
    )
    assert blocked_calculate.status_code == 503
    assert not ledger.nonces
    assert service.calls == 0

    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "offline-single-writer-durable")
    retried_calculate = client.post(
        "/v1/shadow/calculate", json=calculate, headers=calculate_headers
    )
    assert retried_calculate.status_code == 200
    assert len(ledger.nonces) == 1

    outcome = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": "missoula:outcome-revision:readiness-retry",
        "reason_code": None,
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "settle",
                "actual_time": 41.25,
                "expected_revision": 0,
            }
        ],
    }
    outcome_headers = _headers(
        "shadow.outcome.apply",
        request_payload=outcome,
        nonce="readiness-outcome-retry-001",
    )
    monkeypatch.delenv("STRATHMARK_TRUSTED_TOPOLOGY", raising=False)
    blocked_outcome = client.post(
        "/v1/shadow/outcomes/apply", json=outcome, headers=outcome_headers
    )
    assert blocked_outcome.status_code == 503
    assert len(ledger.nonces) == 1

    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "offline-single-writer-durable")
    retried_outcome = client.post(
        "/v1/shadow/outcomes/apply", json=outcome, headers=outcome_headers
    )
    assert retried_outcome.status_code == 200
    assert len(ledger.nonces) == 2


def test_calculation_evidence_preflight_timeout_does_not_claim_actor_nonce(
    shadow_client, monkeypatch, tmp_path
):
    client, service, ledger = shadow_client
    payload = _calculate_payload()
    payload["timeout_ms"] = 25
    headers = _headers(
        "shadow.calculate",
        request_payload=payload,
        nonce="bounded-preflight-timeout-001",
    )
    backing = _empty_snapshot_store(tmp_path / "slow-preflight-evidence.db")
    real_status = backing.get_evidence_snapshot_status

    def slow_status(*, as_of=None, max_age_days=7, query_deadline=None):
        deadline = time.monotonic() + 0.075
        while time.monotonic() < deadline:
            if query_deadline is not None:
                query_deadline.raise_if_expired()
            time.sleep(0.001)
        return real_status(
            as_of=as_of,
            max_age_days=max_age_days,
            query_deadline=query_deadline,
        )

    monkeypatch.setattr(backing, "get_evidence_snapshot_status", slow_status)
    app.dependency_overrides[get_store] = lambda: backing

    response = client.post("/v1/shadow/calculate", json=payload, headers=headers)

    assert response.status_code == 504
    assert "outcome is unknown" not in response.json()["detail"]
    assert not ledger.nonces
    assert service.calls == 0


def test_status_loads_and_validates_scoped_receipt_once(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "current_active_fingerprint": None,
        "model_version": "core-test",
        "timeout_ms": 2000,
    }

    response = client.post(
        "/v1/shadow/status",
        json=payload,
        headers=_headers("shadow.status.read", request_payload=payload),
    )

    assert response.status_code == 200
    assert ledger.receipt_calls == 1


def test_numeric_outcome_denies_scorer_and_accepts_attributed_system_adapter(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": "missoula:outcome-revision:adapter",
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "settle",
                "actual_time": 41.25,
                "expected_revision": 0,
            }
        ],
    }
    denied = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers(
            "shadow.outcome.apply",
            request_payload=payload,
            roles=["scorer"],
        ),
    )
    assert denied.status_code == 401
    assert ledger.outcome_call is None

    accepted = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers(
            "shadow.outcome.apply",
            request_payload=payload,
            roles=["system-adapter"],
        ),
    )
    assert accepted.status_code == 200
    assert ledger.outcome_call[5] == ACTOR


def test_new_calculation_rechecks_evidence_after_persist_before_review(tmp_path):
    from tests.test_shadow_receipts import (
        WOOD,
        _competitors,
        _prepared_store,
        _Provider,
        _request,
    )

    delegate = PredictionLedger(tmp_path / "post-persist-refresh-ledger.db")
    store = _prepared_store(tmp_path / "post-persist-refresh-results.db")

    class RefreshAfterPersistLedger:
        def __init__(self):
            self.receipt_reads = 0

        def __getattr__(self, name):
            return getattr(delegate, name)

        def get_shadow_receipt(self, *args, **kwargs):
            self.receipt_reads += 1
            receipt = delegate.get_shadow_receipt(*args, **kwargs)
            if self.receipt_reads == 2 and receipt is not None:
                current = store.get_evidence_snapshot_status()
                assert current is not None
                captured_at = current.captured_at + timedelta(seconds=1)
                source_id = "test:history-export:post-persist-refresh"
                source_digest = canonical_evidence_source_digest(
                    source_id=source_id,
                    cutoff=current.cutoff,
                    captured_at=captured_at,
                    rows=(),
                )
                payload = EvidenceSnapshotPayload(
                    schema_version=EVIDENCE_SNAPSHOT_SOURCE_SCHEMA_VERSION,
                    source_id=source_id,
                    cutoff=current.cutoff,
                    captured_at=captured_at,
                    rows=(),
                    source_digest=source_digest,
                )

                class Source:
                    def load_snapshot(self, *, cutoff):
                        assert cutoff == current.cutoff
                        return payload

                store.refresh_evidence_snapshot(
                    Source(),
                    cutoff=current.cutoff,
                    expected_active_snapshot_digest=current.snapshot_digest,
                )
            return receipt

    result = ShadowPredictionService(
        RefreshAfterPersistLedger(),
        result_store=store,
        prediction_provider=_Provider(),
    ).calculate(_request(), _competitors(), WOOD)

    assert result.receipt is not None
    assert result.status.freshness == "stale"
    assert result.status.ready_for_review is False


def test_numeric_outcome_attestation_subject_is_field_run_revision(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-numeric-outcome.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "outcome_revision_id": "missoula:outcome-revision:1",
        "revisions": [
            {
                "prediction_id": "prediction-1",
                "competitor_id": "missoula:competitor:alice",
                "event_code": "SB",
                "action": "settle",
                "actual_time": 41.25,
                "expected_revision": 0,
            }
        ],
    }
    response = client.post(
        "/v1/shadow/outcomes/apply",
        json=payload,
        headers=_headers(
            "shadow.outcome.apply",
            "missoula:outcome-revision:1",
            request_payload=payload,
        ),
    )
    assert response.status_code == 403
    assert ledger.outcome_call is None


def test_mirror_replay_is_bounded_and_drift_is_advisory(shadow_client):
    client, _, ledger = shadow_client
    too_large = {
        "schema_version": "strathmark.shadow-mirror-replay.v1",
        "consumer_id": CONSUMER,
        "run_revision": "missoula:run-revision:1",
        "limit": 101,
    }
    assert (
        client.post(
            "/v1/shadow/mirror/replay",
            json=too_large,
            headers=_headers("shadow.mirror.replay"),
        ).status_code
        == 422
    )
    replay = dict(too_large, limit=25)
    assert (
        client.post(
            "/v1/shadow/mirror/replay",
            json=replay,
            headers=_headers("shadow.mirror.replay", request_payload=replay, roles=["admin"]),
        ).status_code
        == 200
    )
    assert ledger.replay_limit == (25, CONSUMER)

    drift = {
        "schema_version": "strathmark.shadow-drift.v1",
        "consumer_id": CONSUMER,
        "run_revision": "missoula:run-revision:1",
        "model_version": "model-v1",
        "lookback_days": 30,
        "baseline_residuals": [0.0] * 20,
    }
    response = client.post(
        "/v1/shadow/drift",
        json=drift,
        headers=_headers("shadow.drift.read", request_payload=drift),
    )
    assert response.status_code == 200
    assert response.json()["report"]["insufficient_recent_samples"] is True
    assert response.json()["report"]["overall_alert"] is False
    assert ledger.training_calls[0][0] == "count"
    assert ledger.training_calls[0][1]["caller_id"] == CONSUMER
    assert ledger.training_calls[1][1]["limit"] == 5001


def test_blocked_maintenance_workers_do_not_starve_critical_receipt_lookup(shadow_client):
    client, _, ledger = shadow_client
    started = threading.Barrier(3)
    release = threading.Event()

    def blocked_replay(*, limit, caller_id):
        ledger.replay_limit = (limit, caller_id)
        started.wait(timeout=2)
        release.wait(timeout=3)
        return {"recorded": 0, "failed": 0, "not_configured": 0}

    ledger.flush_mirror_outbox = blocked_replay
    replay_payload = {
        "schema_version": "strathmark.shadow-mirror-replay.v1",
        "consumer_id": CONSUMER,
        "run_revision": "missoula:run-revision:maintenance-capacity",
        "limit": 1,
        "timeout_ms": 25,
    }
    responses = []

    def request_replay():
        responses.append(
            client.post(
                "/v1/shadow/mirror/replay",
                json=replay_payload,
                headers=_headers(
                    "shadow.mirror.replay",
                    revision=replay_payload["run_revision"],
                    request_payload=replay_payload,
                    roles=["admin"],
                ),
            )
        )

    workers = [threading.Thread(target=request_replay) for _ in range(2)]
    for worker in workers:
        worker.start()
    started.wait(timeout=2)
    for worker in workers:
        worker.join(timeout=1)
    assert sorted(response.status_code for response in responses) == [504, 504]

    lookup = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "timeout_ms": 1000,
    }
    critical = client.post(
        "/v1/shadow/receipts/lookup",
        json=lookup,
        headers=_headers("shadow.receipt.lookup", request_payload=lookup),
    )
    assert critical.status_code == 200

    maintenance_busy = client.post(
        "/v1/shadow/mirror/replay",
        json=replay_payload,
        headers=_headers(
            "shadow.mirror.replay",
            revision=replay_payload["run_revision"],
            request_payload=replay_payload,
            roles=["admin"],
        ),
    )
    assert maintenance_busy.status_code == 429

    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        acquired = []
        for _ in range(2):
            if _SHADOW_MAINTENANCE_SLOTS.acquire(blocking=False):
                acquired.append(True)
        if len(acquired) == 2:
            for _ in acquired:
                _SHADOW_MAINTENANCE_SLOTS.release()
            break
        for _ in acquired:
            _SHADOW_MAINTENANCE_SLOTS.release()
        time.sleep(0.01)
    else:
        pytest.fail("maintenance workers did not release maintenance capacity")


def test_drift_rejects_over_limit_before_loading_rows_and_is_capacity_bounded(shadow_client):
    client, _, ledger = shadow_client
    ledger.training_count = 5001
    payload = {
        "schema_version": "strathmark.shadow-drift.v1",
        "consumer_id": CONSUMER,
        "run_revision": "missoula:run-revision:1",
        "model_version": "model-v1",
        "lookback_days": 30,
        "baseline_residuals": [0.0] * 20,
        "timeout_ms": 1000,
    }
    response = client.post(
        "/v1/shadow/drift",
        json=payload,
        headers=_headers("shadow.drift.read", request_payload=payload),
    )
    assert response.status_code == 413
    assert [kind for kind, _ in ledger.training_calls] == ["count"]

    ledger.training_count = 0
    ledger.training_calls.clear()
    assert _SHADOW_MAINTENANCE_SLOTS.acquire(blocking=False)
    assert _SHADOW_MAINTENANCE_SLOTS.acquire(blocking=False)
    try:
        response = client.post(
            "/v1/shadow/drift",
            json=payload,
            headers=_headers("shadow.drift.read", request_payload=payload),
        )
        assert response.status_code == 429
        assert not ledger.training_calls
    finally:
        _SHADOW_MAINTENANCE_SLOTS.release()
        _SHADOW_MAINTENANCE_SLOTS.release()


def test_status_has_shared_capacity_timeout_and_cooperative_slot_recovery(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "timeout_ms": 25,
    }
    assert _SHADOW_RECOVERY_SLOTS.acquire(blocking=False)
    assert _SHADOW_RECOVERY_SLOTS.acquire(blocking=False)
    try:
        busy = client.post(
            "/v1/shadow/status",
            json=payload,
            headers=_headers("shadow.status.read", request_payload=payload),
        )
        assert busy.status_code == 429
    finally:
        _SHADOW_RECOVERY_SLOTS.release()
        _SHADOW_RECOVERY_SLOTS.release()

    assert _SHADOW_RECOVERY_SLOTS.acquire(blocking=False)
    try:
        ledger.status_delay_seconds = 0.25
        timed_out = client.post(
            "/v1/shadow/status",
            json=payload,
            headers=_headers("shadow.status.read", request_payload=payload),
        )
        assert timed_out.status_code == 504
        ledger.status_delay_seconds = 0.0
        recovered = client.post(
            "/v1/shadow/status",
            json=payload,
            headers=_headers("shadow.status.read", request_payload=payload),
        )
        assert recovered.status_code == 200
    finally:
        _SHADOW_RECOVERY_SLOTS.release()


def test_drift_timeout_interrupts_work_and_releases_maintenance_slot(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-drift.v1",
        "consumer_id": CONSUMER,
        "run_revision": "missoula:run-revision:1",
        "model_version": "model-v1",
        "lookback_days": 30,
        "baseline_residuals": [0.0] * 20,
        "timeout_ms": 25,
    }
    assert _SHADOW_MAINTENANCE_SLOTS.acquire(blocking=False)
    try:
        ledger.training_delay_seconds = 0.25
        timed_out = client.post(
            "/v1/shadow/drift",
            json=payload,
            headers=_headers("shadow.drift.read", request_payload=payload),
        )
        assert timed_out.status_code == 504
        ledger.training_delay_seconds = 0.0
        recovered = client.post(
            "/v1/shadow/drift",
            json=payload,
            headers=_headers("shadow.drift.read", request_payload=payload),
        )
        assert recovered.status_code == 200
    finally:
        _SHADOW_MAINTENANCE_SLOTS.release()


def test_test_double_monitoring_shape_stays_serializable():
    """Guard the fixture against silent dataclass contract drift."""

    assert asdict(_Ledger().get_monitoring_status())["local_trust"] == "recorded"


def test_role_action_matrix_rejects_signed_but_unauthorized_actor(shadow_client):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    response = client.post(
        "/v1/shadow/calculate",
        json=payload,
        headers=_headers("shadow.calculate", request_payload=payload, roles=["scorer"]),
    )
    assert response.status_code == 401
    assert service.calls == 0

    replay = {
        "schema_version": "strathmark.shadow-mirror-replay.v1",
        "consumer_id": CONSUMER,
        "run_revision": "missoula:run-revision:1",
        "limit": 1,
    }
    response = client.post(
        "/v1/shadow/mirror/replay",
        json=replay,
        headers=_headers("shadow.mirror.replay", request_payload=replay, roles=["judge"]),
    )
    assert response.status_code == 403


def test_nonce_replay_remains_blocked_after_new_ledger_instance(tmp_path, monkeypatch):
    for name in ("STRATHMARK_SUPABASE_URL", "STRATHMARK_SUPABASE_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STRATHMARK_SHADOW_SERVICE_CREDENTIALS", json.dumps({CONSUMER: TOKEN}))
    monkeypatch.setenv("STRATHMARK_SHADOW_ATTESTATION_KEYS", json.dumps({CONSUMER: KEY}))
    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "offline-single-writer-durable")
    path = tmp_path / "nonce-restart.db"
    service = _ShadowService()
    first_ledger = PredictionLedger(path)
    snapshot_store = _empty_snapshot_store(tmp_path / "nonce-restart-results.db")
    app.dependency_overrides[get_shadow_service] = lambda: service
    app.dependency_overrides[get_ledger] = lambda: first_ledger
    app.dependency_overrides[get_store] = lambda: snapshot_store
    payload = _calculate_payload()
    headers = _headers(
        "shadow.calculate",
        request_payload=payload,
        nonce="restart-proof-nonce-001",
    )
    try:
        with TestClient(app) as client:
            assert (
                client.post("/v1/shadow/calculate", json=payload, headers=headers).status_code
                == 200
            )
        second_ledger = PredictionLedger(path)
        app.dependency_overrides[get_ledger] = lambda: second_ledger
        with TestClient(app) as client:
            response = client.post("/v1/shadow/calculate", json=payload, headers=headers)
        assert response.status_code == 409
        assert service.calls == 1
    finally:
        app.dependency_overrides.clear()


def test_trusted_body_limit_rejects_before_auth_or_service(shadow_client):
    client, service, _ = shadow_client
    response = client.post(
        "/v1/shadow/calculate",
        content=json.dumps({"padding": "x" * 270_000}),
        headers={
            **_headers("shadow.calculate"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413
    assert service.calls == 0


def test_early_gate_rejects_bad_bearer_and_actual_size_before_dependencies(shadow_client):
    client, service, ledger = shadow_client
    calls = {"ledger": 0, "service": 0}

    def ledger_dependency():
        calls["ledger"] += 1
        return ledger

    def service_dependency():
        calls["service"] += 1
        return service

    app.dependency_overrides[get_ledger] = ledger_dependency
    app.dependency_overrides[get_shadow_service] = service_dependency
    payload = _calculate_payload()
    bad = client.post(
        "/v1/shadow/calculate",
        json=payload,
        headers={
            "Authorization": "Bearer definitely-not-the-configured-token",
            "X-STRATHMARK-Actor-Attestation": _attestation("shadow.calculate"),
        },
    )
    assert bad.status_code == 401
    assert calls == {"ledger": 0, "service": 0}

    oversized = json.dumps({"padding": "x" * 270_000})
    mismatched = client.post(
        "/v1/shadow/calculate",
        content=oversized,
        headers={
            **_headers("shadow.calculate"),
            "Content-Type": "application/json",
            "Content-Length": "100",
        },
    )
    assert mismatched.status_code in {400, 413}
    assert calls == {"ledger": 0, "service": 0}


def test_health_fails_closed_until_durable_single_writer_topology_is_attested(
    shadow_client, monkeypatch
):
    client, _, _ = shadow_client
    monkeypatch.delenv("STRATHMARK_TRUSTED_TOPOLOGY", raising=False)
    untrusted = client.get("/health").json()["shadow_service"]
    assert untrusted["authentication"] == "configured"
    assert untrusted["topology"] == "unattested"
    assert untrusted["ready_for_trusted_shadow"] is False

    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "single-writer-durable")
    ready = client.get("/health").json()["shadow_service"]
    assert ready["topology"] == "operator-attested-unverified"
    assert ready["topology_claim"] == "single-writer-durable"
    assert ready["topology_assurance"] == "operator-attested-not-infrastructure-proven"
    assert ready["ledger_persistence"]["persistence_observed"] is True
    assert ready["evidence_snapshot"]["state"] == "active"
    assert ready["evidence_snapshot"]["integrity"] == "verified"
    assert ready["evidence_snapshot"]["completeness"] == "empty"
    assert ready["evidence_snapshot"]["ready_for_offline"] is True
    assert ready["ready_for_trusted_shadow"] is True


def test_health_inspects_active_ledger_instead_of_a_changed_environment_path(
    shadow_client, monkeypatch, tmp_path
):
    client, _, ledger = shadow_client
    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "single-writer-durable")
    monkeypatch.setenv("STRATHMARK_DB_PATH", str(tmp_path / "missing-ledger.db"))
    health = client.get("/health").json()["shadow_service"]
    assert health["topology"] == "operator-attested-unverified"
    assert ledger.path.is_file()
    assert health["ledger_persistence"]["path_exists"] is True
    assert health["ledger_persistence"]["read_write_open_observed"] is True
    assert (
        health["ledger_persistence"]["assurance"]
        == "sqlite-initialization-observed-not-durability-proof"
    )
    assert health["ready_for_trusted_shadow"] is True

    ledger.path = tmp_path / "missing-active-ledger.db"
    missing = client.get("/health").json()["shadow_service"]
    assert missing["ledger_persistence"]["path_exists"] is False
    assert missing["ledger_persistence"]["read_write_open_observed"] is False
    assert missing["ledger_persistence"]["assurance"] == "unverified"
    assert missing["ready_for_trusted_shadow"] is False


def test_health_snapshot_gate_fails_closed_for_missing_stale_and_tampered(
    shadow_client, monkeypatch, tmp_path
):
    client, _, _ = shadow_client
    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "single-writer-durable")

    missing_store = ResultStore(tmp_path / "missing-snapshot.db")
    app.dependency_overrides[get_store] = lambda: missing_store
    missing = client.get("/health").json()["shadow_service"]
    assert missing["evidence_snapshot"] == {
        "schema_version": "strathmark.evidence-snapshot-health.v1",
        "state": "missing",
        "integrity": "unavailable",
        "ready_for_offline": False,
    }
    assert missing["ready_for_trusted_shadow"] is False

    stale_store = _empty_snapshot_store(
        tmp_path / "stale-snapshot.db",
        captured_at=datetime.now(timezone.utc) - timedelta(days=8),
    )
    app.dependency_overrides[get_store] = lambda: stale_store
    stale = client.get("/health").json()["shadow_service"]
    assert stale["evidence_snapshot"]["state"] == "active"
    assert stale["evidence_snapshot"]["freshness"] == "stale"
    assert stale["evidence_snapshot"]["ready_for_offline"] is False
    assert stale["ready_for_trusted_shadow"] is False

    tampered_store = _empty_snapshot_store(tmp_path / "tampered-snapshot.db")
    with sqlite3.connect(tampered_store.path) as conn:
        conn.execute("DROP TRIGGER evidence_snapshots_no_update")
        conn.execute("UPDATE evidence_snapshots SET source_id = 'test:tampered'")
        conn.commit()
    app.dependency_overrides[get_store] = lambda: tampered_store
    tampered = client.get("/health").json()["shadow_service"]
    assert tampered["evidence_snapshot"] == {
        "schema_version": "strathmark.evidence-snapshot-health.v1",
        "state": "missing",
        "integrity": "unavailable",
        "ready_for_offline": False,
    }
    assert tampered["ready_for_trusted_shadow"] is False


def test_public_health_reuses_verified_evidence_and_never_probes_a_writer_lock(
    shadow_client, monkeypatch, tmp_path
):
    client, _, _ = shadow_client
    store = _empty_snapshot_store(tmp_path / "cached-health-results.db")
    ledger = PredictionLedger(tmp_path / "cached-health-ledger.db")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_ledger] = lambda: ledger
    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "single-writer-durable")

    full_verification_calls = 0

    def forbidden_full_verification(*args, **kwargs):
        nonlocal full_verification_calls
        full_verification_calls += 1
        raise AssertionError("public health must not run full evidence verification")

    monkeypatch.setattr(store, "get_evidence_snapshot_status", forbidden_full_verification)

    with sqlite3.connect(ledger.path, timeout=0.1, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        responses = [client.get("/health") for _ in range(25)]
        elapsed = time.monotonic() - started

    assert all(response.status_code == 200 for response in responses)
    assert all(
        response.json()["shadow_service"]["ready_for_trusted_shadow"] is True
        for response in responses
    )
    assert full_verification_calls == 0
    assert elapsed < 5


def test_public_health_fails_closed_on_restart_and_observed_snapshot_file_change(
    shadow_client, monkeypatch, tmp_path
):
    client, _, ledger = shadow_client
    path = tmp_path / "restart-health-results.db"
    original = _empty_snapshot_store(path)
    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "single-writer-durable")

    restarted = ResultStore(path)
    app.dependency_overrides[get_store] = lambda: restarted
    cold = client.get("/health").json()["shadow_service"]
    assert cold["evidence_snapshot"]["state"] == "missing"
    assert cold["evidence_snapshot"]["integrity"] == "unavailable"
    assert cold["ready_for_trusted_shadow"] is False

    assert restarted.get_evidence_snapshot_status() is not None
    warmed = client.get("/health").json()["shadow_service"]
    assert warmed["evidence_snapshot"]["state"] == "active"
    assert warmed["evidence_snapshot"]["integrity"] == "verified"
    assert warmed["ready_for_trusted_shadow"] is True

    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER evidence_snapshots_no_update")
        conn.execute("UPDATE evidence_snapshots SET source_id = 'test:tampered'")
        conn.commit()

    changed = client.get("/health").json()["shadow_service"]
    assert changed["evidence_snapshot"]["state"] == "missing"
    assert changed["evidence_snapshot"]["integrity"] == "unavailable"
    assert changed["ready_for_trusted_shadow"] is False
    assert original.path == restarted.path
    assert ledger.path.is_file()


def test_calculation_capacity_and_timeout_have_explicit_recovery_semantics(shadow_client, tmp_path):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    try:
        busy = client.post(
            "/v1/shadow/calculate",
            json=payload,
            headers=_headers("shadow.calculate", request_payload=payload),
        )
        assert busy.status_code == 429
        assert "receipt lookup" in busy.json()["detail"].lower()
        assert service.calls == 0
    finally:
        _SHADOW_OPERATION_SLOTS.release()
        _SHADOW_OPERATION_SLOTS.release()

    persisted = threading.Event()
    release = threading.Event()

    class InterleavingLedger(PredictionLedger):
        def record_field(self, *args, **kwargs):
            result = super().record_field(*args, **kwargs)
            persisted.set()
            release.wait(timeout=5)
            return result

    real_store = _empty_snapshot_store(tmp_path / "real-timeout-results.db", cutoff=date.today())
    real_ledger = InterleavingLedger(tmp_path / "real-timeout-ledger.db")
    real_service = ShadowPredictionService(real_ledger, result_store=real_store)
    app.dependency_overrides[get_store] = lambda: real_store
    app.dependency_overrides[get_ledger] = lambda: real_ledger
    app.dependency_overrides[get_shadow_service] = lambda: real_service
    payload["timeout_ms"] = 100
    payload["prediction_as_of"] = date.today().isoformat()
    try:
        timed_out = client.post(
            "/v1/shadow/calculate",
            json=payload,
            headers=_headers("shadow.calculate", request_payload=payload),
        )
        assert timed_out.status_code == 504
        assert "receipt lookup" in timed_out.json()["detail"].lower()
        assert persisted.wait(timeout=3)

        lookup = {
            "schema_version": "strathmark.shadow-receipt-lookup.v1",
            "consumer_id": CONSUMER,
            "request_id": "missoula:request:1",
            "run_revision": "missoula:run-revision:1",
        }
        recovered = client.post(
            "/v1/shadow/receipts/lookup",
            json=lookup,
            headers=_headers("shadow.receipt.lookup", request_payload=lookup),
        )
        assert recovered.status_code == 200
        assert recovered.json()["receipt"]["core"]["request_id"] == payload["request_id"]
    finally:
        release.set()

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        acquired = []
        for _ in range(2):
            if _SHADOW_OPERATION_SLOTS.acquire(blocking=False):
                acquired.append(True)
        if len(acquired) == 2:
            for _ in acquired:
                _SHADOW_OPERATION_SLOTS.release()
            break
        for _ in acquired:
            _SHADOW_OPERATION_SLOTS.release()
        time.sleep(0.01)
    else:
        pytest.fail("calculation worker did not eventually release shared capacity")


def test_two_persisted_calculation_timeouts_cannot_starve_receipt_recovery(shadow_client, tmp_path):
    client, _, _ = shadow_client
    persisted = threading.Event()
    release = threading.Event()
    persist_lock = threading.Lock()
    persisted_count = 0

    class TwoBlockedCalculationsLedger(PredictionLedger):
        def record_field(self, *args, **kwargs):
            nonlocal persisted_count
            result = super().record_field(*args, **kwargs)
            with persist_lock:
                persisted_count += 1
                if persisted_count == 2:
                    persisted.set()
            release.wait(timeout=5)
            return result

    real_store = _empty_snapshot_store(tmp_path / "two-timeouts-results.db", cutoff=date.today())
    real_ledger = TwoBlockedCalculationsLedger(tmp_path / "two-timeouts-ledger.db")
    real_service = ShadowPredictionService(real_ledger, result_store=real_store)
    app.dependency_overrides[get_store] = lambda: real_store
    app.dependency_overrides[get_ledger] = lambda: real_ledger
    app.dependency_overrides[get_shadow_service] = lambda: real_service

    payloads = []
    for index in (1, 2):
        payload = _calculate_payload()
        payload.update(
            {
                "field_run_id": f"missoula:field-run:recovery-{index}",
                "request_id": f"missoula:request:recovery-{index}",
                "run_revision": f"missoula:run-revision:recovery-{index}",
                "prediction_as_of": date.today().isoformat(),
                "timeout_ms": 100,
            }
        )
        payloads.append(payload)

    responses = []

    def calculate(payload):
        responses.append(
            client.post(
                "/v1/shadow/calculate",
                json=payload,
                headers=_headers(
                    "shadow.calculate",
                    revision=payload["run_revision"],
                    request_payload=payload,
                ),
            )
        )

    workers = [threading.Thread(target=calculate, args=(payload,)) for payload in payloads]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
        assert sorted(response.status_code for response in responses) == [504, 504]
        assert persisted.wait(timeout=3)

        lookup = {
            "schema_version": "strathmark.shadow-receipt-lookup.v1",
            "consumer_id": CONSUMER,
            "request_id": payloads[0]["request_id"],
            "run_revision": payloads[0]["run_revision"],
            "timeout_ms": 1000,
        }
        recovered = client.post(
            "/v1/shadow/receipts/lookup",
            json=lookup,
            headers=_headers(
                "shadow.receipt.lookup",
                revision=lookup["run_revision"],
                request_payload=lookup,
            ),
        )
        assert recovered.status_code == 200
        assert recovered.json()["receipt"]["core"]["request_id"] == lookup["request_id"]
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=2)


def test_nonce_claims_purge_expired_rows_cap_active_per_consumer_and_survive_restart(
    tmp_path, monkeypatch
):
    import strathmark.ledger as ledger_module

    path = tmp_path / "nonce-capacity.db"
    ledger = PredictionLedger(path)
    now = int(time.time())
    nonce = "expired-reusable-nonce-001"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO actor_attestation_nonce_claims (
                consumer_id, nonce_hash, actor_id, action,
                subject_revision, expires_at, claimed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                CONSUMER,
                hashlib.sha256(nonce.encode()).hexdigest(),
                ACTOR,
                "shadow.calculate",
                "missoula:run-revision:old",
                now - 1,
                "2026-08-13T00:00:00+00:00",
            ),
        )
    restarted = PredictionLedger(path)
    with sqlite3.connect(path) as conn:
        trigger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name='actor_attestation_nonce_claims_no_delete'"
        ).fetchone()
    assert trigger is None
    assert restarted.claim_actor_attestation_nonce(
        consumer_id=CONSUMER,
        nonce=nonce,
        actor_id=ACTOR,
        action="shadow.calculate",
        subject_revision="missoula:run-revision:new",
        expires_at=now + 30,
    )

    monkeypatch.setattr(ledger_module, "MAX_ACTIVE_ATTESTATION_NONCES_PER_CONSUMER", 2)
    assert restarted.claim_actor_attestation_nonce(
        consumer_id=CONSUMER,
        nonce="capacity-nonce-value-002",
        actor_id=ACTOR,
        action="shadow.calculate",
        subject_revision="missoula:run-revision:2",
        expires_at=now + 30,
    )
    assert not restarted.claim_actor_attestation_nonce(
        consumer_id=CONSUMER,
        nonce=nonce,
        actor_id=ACTOR,
        action="shadow.calculate",
        subject_revision="missoula:run-revision:new",
        expires_at=now + 30,
    )
    with pytest.raises(RuntimeError, match="capacity"):
        restarted.claim_actor_attestation_nonce(
            consumer_id=CONSUMER,
            nonce="capacity-nonce-value-003",
            actor_id=ACTOR,
            action="shadow.calculate",
            subject_revision="missoula:run-revision:3",
            expires_at=now + 30,
        )
