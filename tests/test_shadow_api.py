"""Authenticated, bounded shadow transport contract tests.

Every test replaces the default ledger with a temporary SQLite database or an
in-memory test double and removes ambient cloud configuration.  No external
service is contacted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import asdict

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from strathmark.api import (  # noqa: E402
    _SHADOW_OPERATION_SLOTS,
    app,
    get_ledger,
    get_shadow_service,
    get_store,
)
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
    ShadowReceipt,
)
from strathmark.store import ResultStore  # noqa: E402

CONSUMER = "missoula:service:shadow"
ACTOR = "missoula:operator:7"
TOKEN = "test-shadow-service-token"
KEY = "test-shadow-attestation-key-with-enough-entropy"
AUDIENCE = "strathmark.shadow.v1"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


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
) -> str:
    now = int(time.time())
    payload = {
        "schema_version": "strathmark.actor-attestation.v1",
        "consumer_id": consumer_id,
        "actor_id": actor_id,
        "roles": roles or ["judge"],
        "action": action,
        "subject_revision": revision,
        "audience": audience,
        "nonce": nonce or f"nonce-{time.time_ns()}",
        "issued_at": now,
        "expires_at": expires_at if expires_at is not None else now + 30,
    }
    encoded = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _b64(hmac.new(KEY.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _headers(action: str, revision: str = "missoula:run-revision:1", **kwargs):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-STRATHMARK-Actor-Attestation": _attestation(action, revision, **kwargs),
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


class _ShadowService:
    def __init__(self):
        self.calls = 0

    def calculate(self, request, competitors, wood):
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
    ):
        if request_id.endswith("missing"):
            return None
        core = {
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
        if kwargs.get("caller_id") is not None and kwargs.get("request_id") is not None:
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
    ):
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
    service = _ShadowService()
    ledger = _Ledger()
    ledger.path = tmp_path / "shadow-api-ledger.db"
    PredictionLedger(ledger.path)
    monkeypatch.setenv("STRATHMARK_DB_PATH", str(ledger.path))
    app.dependency_overrides[get_shadow_service] = lambda: service
    app.dependency_overrides[get_ledger] = lambda: ledger
    app.dependency_overrides[get_store] = lambda: ResultStore(tmp_path / "shadow-api-results.db")
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


def test_early_service_preauth_compares_every_configured_token(monkeypatch):
    import strathmark.auth as auth_module

    second_token = "other-shadow-service-token"
    monkeypatch.setenv(
        "STRATHMARK_SHADOW_SERVICE_CREDENTIALS",
        json.dumps({CONSUMER: TOKEN, "other:service:shadow": second_token}),
    )
    real_compare = hmac.compare_digest
    comparisons = []

    def tracking_compare(left, right):
        comparisons.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(auth_module.hmac, "compare_digest", tracking_compare)
    auth_module.preauthenticate_shadow_service(f"Bearer {TOKEN}")

    assert comparisons == [(TOKEN, TOKEN), (TOKEN, second_token)]


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
    headers = _headers("shadow.calculate", nonce="one-use-nonce-001")
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
        headers=_headers("shadow.receipt.lookup"),
    )
    assert receipt.status_code == 200
    assert receipt.json()["receipt"]["core"]["request_id"] == "missoula:request:1"
    assert receipt.json()["receipt"]["status"]["freshness"] == "stale"

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
        headers=_headers("shadow.status.read"),
    )
    assert status.status_code == 200
    assert status.json()["status"]["local_trust"] == "recorded"
    assert status.json()["status"]["drift_calibration_advisory"] == "insufficient-evidence"


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
        headers=_headers(action, "missoula:run-revision:2"),
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
        headers=_headers("shadow.outcome.apply", "missoula:run-revision:1"),
    )
    assert response.status_code == 200
    assert ledger.outcome_call[2] == CONSUMER
    assert ledger.outcome_call[3] == "missoula:request:1"
    assert ledger.outcome_call[4] == "missoula:run-revision:1"
    assert ledger.outcome_call[5] == ACTOR
    assert ledger.outcome_call[1][0]["expected_revision"] == 0


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
        headers=_headers("shadow.outcome.apply", "missoula:outcome-revision:1"),
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
            headers=_headers("shadow.mirror.replay", roles=["admin"]),
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
    response = client.post("/v1/shadow/drift", json=drift, headers=_headers("shadow.drift.read"))
    assert response.status_code == 200
    assert response.json()["report"]["insufficient_recent_samples"] is True
    assert response.json()["report"]["overall_alert"] is False
    assert ledger.training_calls[0][0] == "count"
    assert ledger.training_calls[0][1]["caller_id"] == CONSUMER
    assert ledger.training_calls[1][1]["limit"] == 5001


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
    response = client.post("/v1/shadow/drift", json=payload, headers=_headers("shadow.drift.read"))
    assert response.status_code == 413
    assert [kind for kind, _ in ledger.training_calls] == ["count"]

    ledger.training_count = 0
    ledger.training_calls.clear()
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    try:
        response = client.post(
            "/v1/shadow/drift", json=payload, headers=_headers("shadow.drift.read")
        )
        assert response.status_code == 429
        assert not ledger.training_calls
    finally:
        _SHADOW_OPERATION_SLOTS.release()
        _SHADOW_OPERATION_SLOTS.release()


def test_status_has_shared_capacity_timeout_and_cooperative_slot_recovery(shadow_client):
    client, _, ledger = shadow_client
    payload = {
        "schema_version": "strathmark.shadow-status.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
        "timeout_ms": 25,
    }
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    try:
        busy = client.post(
            "/v1/shadow/status",
            json=payload,
            headers=_headers("shadow.status.read"),
        )
        assert busy.status_code == 429
    finally:
        _SHADOW_OPERATION_SLOTS.release()
        _SHADOW_OPERATION_SLOTS.release()

    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    try:
        ledger.status_delay_seconds = 0.25
        timed_out = client.post(
            "/v1/shadow/status",
            json=payload,
            headers=_headers("shadow.status.read"),
        )
        assert timed_out.status_code == 504
        ledger.status_delay_seconds = 0.0
        recovered = client.post(
            "/v1/shadow/status",
            json=payload,
            headers=_headers("shadow.status.read"),
        )
        assert recovered.status_code == 200
    finally:
        _SHADOW_OPERATION_SLOTS.release()


def test_drift_timeout_interrupts_work_and_releases_shared_slot(shadow_client):
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
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    try:
        ledger.training_delay_seconds = 0.25
        timed_out = client.post(
            "/v1/shadow/drift",
            json=payload,
            headers=_headers("shadow.drift.read"),
        )
        assert timed_out.status_code == 504
        ledger.training_delay_seconds = 0.0
        recovered = client.post(
            "/v1/shadow/drift",
            json=payload,
            headers=_headers("shadow.drift.read"),
        )
        assert recovered.status_code == 200
    finally:
        _SHADOW_OPERATION_SLOTS.release()


def test_test_double_monitoring_shape_stays_serializable():
    """Guard the fixture against silent dataclass contract drift."""

    assert asdict(_Ledger().get_monitoring_status())["local_trust"] == "recorded"


def test_role_action_matrix_rejects_signed_but_unauthorized_actor(shadow_client):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    response = client.post(
        "/v1/shadow/calculate",
        json=payload,
        headers=_headers("shadow.calculate", roles=["scorer"]),
    )
    assert response.status_code == 403
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
        headers=_headers("shadow.mirror.replay", roles=["judge"]),
    )
    assert response.status_code == 403


def test_nonce_replay_remains_blocked_after_new_ledger_instance(tmp_path, monkeypatch):
    for name in ("STRATHMARK_SUPABASE_URL", "STRATHMARK_SUPABASE_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STRATHMARK_SHADOW_SERVICE_CREDENTIALS", json.dumps({CONSUMER: TOKEN}))
    monkeypatch.setenv("STRATHMARK_SHADOW_ATTESTATION_KEYS", json.dumps({CONSUMER: KEY}))
    path = tmp_path / "nonce-restart.db"
    service = _ShadowService()
    first_ledger = PredictionLedger(path)
    app.dependency_overrides[get_shadow_service] = lambda: service
    app.dependency_overrides[get_ledger] = lambda: first_ledger
    headers = _headers("shadow.calculate", nonce="restart-proof-nonce-001")
    payload = _calculate_payload()
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
        == "sqlite-read-write-observed-not-durability-proof"
    )
    assert health["ready_for_trusted_shadow"] is True

    ledger.path = tmp_path / "missing-active-ledger.db"
    missing = client.get("/health").json()["shadow_service"]
    assert missing["ledger_persistence"]["path_exists"] is False
    assert missing["ledger_persistence"]["read_write_open_observed"] is False
    assert missing["ledger_persistence"]["assurance"] == "unverified"
    assert missing["ready_for_trusted_shadow"] is False


def test_calculation_capacity_and_timeout_have_explicit_recovery_semantics(shadow_client):
    client, service, _ = shadow_client
    payload = _calculate_payload()
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    assert _SHADOW_OPERATION_SLOTS.acquire(blocking=False)
    try:
        busy = client.post(
            "/v1/shadow/calculate", json=payload, headers=_headers("shadow.calculate")
        )
        assert busy.status_code == 429
        assert "receipt lookup" in busy.json()["detail"].lower()
        assert service.calls == 0
    finally:
        _SHADOW_OPERATION_SLOTS.release()
        _SHADOW_OPERATION_SLOTS.release()

    class _SlowService(_ShadowService):
        def calculate(self, request, competitors, wood):
            time.sleep(0.1)
            return super().calculate(request, competitors, wood)

    slow = _SlowService()
    app.dependency_overrides[get_shadow_service] = lambda: slow
    payload["timeout_ms"] = 25
    timed_out = client.post(
        "/v1/shadow/calculate", json=payload, headers=_headers("shadow.calculate")
    )
    assert timed_out.status_code == 504
    assert "receipt lookup" in timed_out.json()["detail"].lower()
    time.sleep(0.15)

    lookup = {
        "schema_version": "strathmark.shadow-receipt-lookup.v1",
        "consumer_id": CONSUMER,
        "request_id": "missoula:request:1",
        "run_revision": "missoula:run-revision:1",
    }
    recovered = client.post(
        "/v1/shadow/receipts/lookup",
        json=lookup,
        headers=_headers("shadow.receipt.lookup"),
    )
    assert recovered.status_code == 200


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
