"""Frozen local consumer contract and offline lifecycle rehearsal.

The contract is intentionally exercised with a temporary SQLite database and an
in-process verified evidence adapter.  Cloud configuration is removed by the
test fixture before any STRATHMARK object is created.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator, ValidationError  # noqa: E402

from strathmark import api as api_module  # noqa: E402
from strathmark.api import (  # noqa: E402
    ShadowCalculateRequest,
    ShadowDriftRequest,
    ShadowMirrorReplayRequest,
    ShadowNumericOutcomeRequest,
    ShadowReceiptLookupRequest,
    ShadowStatusRequest,
    app,
    get_ledger,
    get_shadow_service,
    get_store,
)
from strathmark.config import data_req  # noqa: E402
from strathmark.consumer_contract import (
    SHADOW_CONSUMER_CONTRACT_VERSION,
    load_shadow_consumer_contract,
    shadow_consumer_contract_bytes,
    shadow_consumer_contract_digest,
)
from strathmark.ledger import (  # noqa: E402
    MAX_NUMERIC_RAW_TIME_SECONDS,
    PredictionLedger,
)
from strathmark.shadow import ShadowPredictionService  # noqa: E402
from tests.test_shadow_receipts import _prepared_store, _Provider  # noqa: E402

_CONSUMER = "missoula:service:shadow"
_ACTOR = "missoula:operator:007"
_TOKEN = "isolated-shadow-contract-token"
_KEY = "isolated-shadow-contract-attestation-key"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _headers(action: str, revision: str, *, roles=("judge",)) -> dict[str, str]:
    now = int(time.time())
    payload = {
        "schema_version": "strathmark.actor-attestation.v1",
        "consumer_id": _CONSUMER,
        "actor_id": _ACTOR,
        "roles": list(roles),
        "action": action,
        "subject_revision": revision,
        "audience": "strathmark.shadow.v1",
        "nonce": f"u6-{action}-{time.time_ns()}",
        "issued_at": now,
        "expires_at": now + 30,
    }
    encoded = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _b64(hmac.new(_KEY.encode(), encoded.encode(), hashlib.sha256).digest())
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "X-STRATHMARK-Actor-Attestation": f"{encoded}.{signature}",
    }


def _validate(document, schema_name: str, value) -> None:
    schema = {
        "$schema": document["jsonSchemaDialect"],
        "$ref": f"#/components/schemas/{schema_name}",
        "components": document["components"],
    }
    Draft202012Validator(schema).validate(value)


def _reject_extra_field(document, schema_name: str, value, path) -> None:
    mutated = copy.deepcopy(value)
    target = mutated
    for part in path:
        target = target[part]
    target["unreviewed_contract_field"] = 0
    with pytest.raises(ValidationError):
        _validate(document, schema_name, mutated)


def _calculate_payload(request_id="missoula:request:001", revision="missoula:run-revision:001"):
    document = load_shadow_consumer_contract()
    payload = json.loads(
        json.dumps(
            document["paths"]["/v1/shadow/calculate"]["post"]["requestBody"]["content"][
                "application/json"
            ]["example"]
        )
    )
    payload["prediction_as_of"] = "2026-11-02"
    payload["request_id"] = request_id
    payload["run_revision"] = revision
    return payload


def _numeric_predictions(core):
    return [
        (row["competitor_id"], row["median_seconds"], row["assigned_mark"])
        for row in core["predictions"]
    ]


def test_packaged_contract_is_canonical_and_checksum_verified():
    raw = shadow_consumer_contract_bytes()
    document = load_shadow_consumer_contract()

    assert document["info"]["x-strathmark-contract-version"] == (SHADOW_CONSUMER_CONTRACT_VERSION)
    assert raw == (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert shadow_consumer_contract_digest() == hashlib.sha256(raw).hexdigest()
    assert document["openapi"] == "3.1.0"
    assert set(document["paths"]) == {
        "/health",
        "/v1/shadow/calculate",
        "/v1/shadow/drift",
        "/v1/shadow/mirror/replay",
        "/v1/shadow/outcomes/apply",
        "/v1/shadow/receipts/lookup",
        "/v1/shadow/status",
    }


def test_every_frozen_example_validates_against_its_schema_and_live_request_model():
    document = load_shadow_consumer_contract()
    request_models = {
        "/v1/shadow/calculate": ("CalculateRequest", ShadowCalculateRequest),
        "/v1/shadow/receipts/lookup": ("LookupRequest", ShadowReceiptLookupRequest),
        "/v1/shadow/status": ("StatusRequest", ShadowStatusRequest),
        "/v1/shadow/outcomes/apply": (
            "NumericOutcomeRequest",
            ShadowNumericOutcomeRequest,
        ),
        "/v1/shadow/mirror/replay": ("MirrorReplayRequest", ShadowMirrorReplayRequest),
        "/v1/shadow/drift": ("DriftRequest", ShadowDriftRequest),
    }
    response_schemas = {
        "/health": "HealthResponse",
        "/v1/shadow/calculate": "CalculateResponse",
        "/v1/shadow/receipts/lookup": "LookupResponse",
        "/v1/shadow/status": "StatusResponse",
        "/v1/shadow/outcomes/apply": "NumericOutcomeResponse",
        "/v1/shadow/mirror/replay": "MirrorReplayResponse",
        "/v1/shadow/drift": "DriftResponse",
    }
    for name, schema in document["components"]["schemas"].items():
        Draft202012Validator.check_schema(schema)
    for path, (schema_name, model) in request_models.items():
        example = document["paths"][path]["post"]["requestBody"]["content"]["application/json"][
            "example"
        ]
        _validate(document, schema_name, example)
        model.model_validate(example)
    for path, schema_name in response_schemas.items():
        method = "get" if path == "/health" else "post"
        example = document["paths"][path][method]["responses"]["200"]["content"][
            "application/json"
        ]["example"]
        _validate(document, schema_name, example)


def test_request_boundaries_match_live_validation_exactly():
    document = load_shadow_consumer_contract()
    calculate = _calculate_payload()
    numeric = copy.deepcopy(
        document["paths"]["/v1/shadow/outcomes/apply"]["post"]["requestBody"]["content"][
            "application/json"
        ]["example"]
    )
    drift = copy.deepcopy(
        document["paths"]["/v1/shadow/drift"]["post"]["requestBody"]["content"]["application/json"][
            "example"
        ]
    )

    wood_schema = document["components"]["schemas"]["Wood"]["properties"]["diameter_mm"]
    assert wood_schema["minimum"] == data_req.MIN_DIAMETER_MM == 225
    assert wood_schema["maximum"] == data_req.MAX_DIAMETER_MM == 500
    for accepted in (data_req.MIN_DIAMETER_MM, data_req.MAX_DIAMETER_MM):
        calculate["wood"]["diameter_mm"] = accepted
        _validate(document, "CalculateRequest", calculate)
        ShadowCalculateRequest.model_validate(calculate)
    for rejected in (
        math.nextafter(float(data_req.MIN_DIAMETER_MM), -math.inf),
        math.nextafter(float(data_req.MAX_DIAMETER_MM), math.inf),
    ):
        calculate["wood"]["diameter_mm"] = rejected
        with pytest.raises(ValidationError):
            _validate(document, "CalculateRequest", calculate)
        with pytest.raises(ValueError):
            ShadowCalculateRequest.model_validate(calculate)

    revision_schema = document["components"]["schemas"]["NumericRevision"]["properties"][
        "actual_time"
    ]
    assert revision_schema["maximum"] == MAX_NUMERIC_RAW_TIME_SECONDS == 300.0
    numeric["revisions"][0]["actual_time"] = MAX_NUMERIC_RAW_TIME_SECONDS
    _validate(document, "NumericOutcomeRequest", numeric)
    ShadowNumericOutcomeRequest.model_validate(numeric)
    numeric["revisions"][0]["actual_time"] = math.nextafter(MAX_NUMERIC_RAW_TIME_SECONDS, math.inf)
    with pytest.raises(ValidationError):
        _validate(document, "NumericOutcomeRequest", numeric)
    with pytest.raises(ValueError):
        ShadowNumericOutcomeRequest.model_validate(numeric)

    residual_schema = document["components"]["schemas"]["DriftRequest"]["properties"][
        "baseline_residuals"
    ]["items"]
    assert residual_schema["minimum"] == -MAX_NUMERIC_RAW_TIME_SECONDS
    assert residual_schema["maximum"] == MAX_NUMERIC_RAW_TIME_SECONDS
    drift["baseline_residuals"] = [
        -MAX_NUMERIC_RAW_TIME_SECONDS,
        MAX_NUMERIC_RAW_TIME_SECONDS,
    ]
    _validate(document, "DriftRequest", drift)
    ShadowDriftRequest.model_validate(drift)
    for rejected in (
        math.nextafter(-MAX_NUMERIC_RAW_TIME_SECONDS, -math.inf),
        math.nextafter(MAX_NUMERIC_RAW_TIME_SECONDS, math.inf),
    ):
        drift["baseline_residuals"] = [rejected]
        with pytest.raises(ValidationError):
            _validate(document, "DriftRequest", drift)


def test_response_schemas_recursively_close_objects_and_type_arrays():
    document = load_shadow_consumer_contract()
    schemas = document["components"]["schemas"]
    response_roots = {
        "CalculateResponse",
        "LookupResponse",
        "StatusResponse",
        "NumericOutcomeResponse",
        "MirrorReplayResponse",
        "DriftResponse",
        "HealthResponse",
    }
    visited = set()

    def inspect(schema, location):
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            if name in visited:
                return
            visited.add(name)
            inspect(schemas[name], f"components.schemas.{name}")
            return
        for keyword in ("oneOf", "anyOf", "allOf"):
            for index, choice in enumerate(schema.get(keyword, [])):
                inspect(choice, f"{location}.{keyword}[{index}]")
        schema_type = schema.get("type")
        types = set(schema_type if isinstance(schema_type, list) else [schema_type])
        if "object" in types or "properties" in schema:
            assert "additionalProperties" in schema, location
            additional = schema["additionalProperties"]
            assert additional is False or isinstance(additional, dict), location
            if schema.get("properties"):
                assert set(schema.get("required", [])) == set(schema["properties"]), location
                for key, child in schema["properties"].items():
                    inspect(child, f"{location}.properties.{key}")
            if isinstance(additional, dict):
                inspect(additional, f"{location}.additionalProperties")
        if "array" in types:
            assert "items" in schema, location
            inspect(schema["items"], f"{location}.items")

    for root in response_roots:
        inspect(schemas[root], f"components.schemas.{root}")


def test_existing_installed_distribution_smoke_verifies_frozen_contract_in_ci():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "smoke_installed_distribution.py").read_text(encoding="utf-8")
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / ".github" / "workflows").glob("*.yml"))
    )
    assert "scripts/smoke_installed_distribution.py" in workflows
    assert "load_shadow_consumer_contract" in script
    assert "shadow_consumer_contract_digest" in script
    assert "EXPECTED_SHADOW_CONSUMER_PATHS" in script


def test_offline_consumer_lifecycle_replays_core_and_projects_current_state(tmp_path, monkeypatch):
    for name in (
        "STRATHMARK_SUPABASE_URL",
        "STRATHMARK_SUPABASE_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STRATHMARK_SHADOW_SERVICE_CREDENTIALS", json.dumps({_CONSUMER: _TOKEN}))
    monkeypatch.setenv("STRATHMARK_SHADOW_ATTESTATION_KEYS", json.dumps({_CONSUMER: _KEY}))
    monkeypatch.setenv("STRATHMARK_TRUSTED_TOPOLOGY", "offline-single-writer-durable")
    path = Path(tmp_path) / "consumer-contract.db"
    monkeypatch.setenv("STRATHMARK_DB_PATH", str(path))
    store = _prepared_store(path)
    ledger = PredictionLedger(path)
    first_provider = _Provider(version="core-a", median=42.0, digest="a" * 64)
    service = ShadowPredictionService(
        ledger, result_store=store, prediction_provider=first_provider
    )
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_ledger] = lambda: ledger
    app.dependency_overrides[get_shadow_service] = lambda: service
    monkeypatch.setattr(api_module, "check_ollama_connection", lambda: False)
    document = load_shadow_consumer_contract()
    try:
        with TestClient(app) as client:
            payload = _calculate_payload()
            calculated = client.post(
                "/v1/shadow/calculate",
                headers=_headers("shadow.calculate", payload["run_revision"]),
                json=payload,
            )
            assert calculated.status_code == 200, calculated.text
            calculated_json = calculated.json()
            _validate(document, "CalculateResponse", calculated_json)
            for nested_path in (
                (),
                ("receipt",),
                ("receipt", "core"),
                ("receipt", "core", "active_input"),
                ("receipt", "core", "predictions", 0),
                ("receipt", "core", "ledger"),
                ("receipt", "status"),
            ):
                _reject_extra_field(document, "CalculateResponse", calculated_json, nested_path)
            assert calculated_json["trusted"] is True
            assert calculated_json["status"]["ready_for_review"] is True
            original_core_json = calculated_json["receipt"]["core_json"]
            original_core = calculated_json["receipt"]["core"]
            assert first_provider.calls == 1

            context_only = _calculate_payload(
                "missoula:request:context-only", "missoula:run-revision:context-only"
            )
            context_only["observation_fingerprint"] = "3" * 64
            changed = client.post(
                "/v1/shadow/calculate",
                headers=_headers("shadow.calculate", context_only["run_revision"]),
                json=context_only,
            )
            assert changed.status_code == 200, changed.text
            changed_core = changed.json()["receipt"]["core"]
            assert _numeric_predictions(changed_core) == _numeric_predictions(original_core)
            assert changed_core["calculation_input"] == original_core["calculation_input"]
            assert (
                changed_core["active_input"]["fingerprint"]
                == original_core["active_input"]["fingerprint"]
            )
            assert (
                changed_core["observation"]["fingerprint"]
                != (original_core["observation"]["fingerprint"])
            )

            conflicting_context = copy.deepcopy(payload)
            conflicting_context["observation_fingerprint"] = "4" * 64
            conflict = client.post(
                "/v1/shadow/calculate",
                headers=_headers("shadow.calculate", payload["run_revision"]),
                json=conflicting_context,
            )
            assert conflict.status_code == 409, conflict.text

            class ArtifactMustNotLoad:
                def snapshot(self, prediction_as_of):
                    raise AssertionError("receipt recovery recalculated after restart")

            restarted_store = _prepared_store(path)
            restarted_ledger = PredictionLedger(path)
            restarted_service = ShadowPredictionService(
                restarted_ledger,
                result_store=restarted_store,
                prediction_provider=ArtifactMustNotLoad(),
            )
            app.dependency_overrides[get_store] = lambda: restarted_store
            app.dependency_overrides[get_ledger] = lambda: restarted_ledger
            app.dependency_overrides[get_shadow_service] = lambda: restarted_service

            replay = client.post(
                "/v1/shadow/calculate",
                headers=_headers("shadow.calculate", payload["run_revision"]),
                json=payload,
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["receipt"]["core_json"] == original_core_json

            lookup_payload = {
                "schema_version": "strathmark.shadow-receipt-lookup.v1",
                "consumer_id": _CONSUMER,
                "request_id": payload["request_id"],
                "run_revision": payload["run_revision"],
                "current_active_fingerprint": original_core["active_input"]["fingerprint"],
            }
            lookup = client.post(
                "/v1/shadow/receipts/lookup",
                headers=_headers("shadow.receipt.lookup", payload["run_revision"]),
                json=lookup_payload,
            )
            assert lookup.status_code == 200, lookup.text
            _validate(document, "LookupResponse", lookup.json())
            _reject_extra_field(document, "LookupResponse", lookup.json(), ("receipt", "core"))
            assert lookup.json()["receipt"]["core_json"] == original_core_json

            status_payload = {
                "schema_version": "strathmark.shadow-status.v1",
                "consumer_id": _CONSUMER,
                "request_id": payload["request_id"],
                "run_revision": payload["run_revision"],
                "current_active_fingerprint": original_core["active_input"]["fingerprint"],
                "model_version": None,
                "timeout_ms": 2000,
            }
            status = client.post(
                "/v1/shadow/status",
                headers=_headers("shadow.status.read", payload["run_revision"]),
                json=status_payload,
            )
            assert status.status_code == 200, status.text
            _validate(document, "StatusResponse", status.json())
            _reject_extra_field(document, "StatusResponse", status.json(), ("status",))
            assert status.json()["status"]["local_trust"] == "recorded"
            assert status.json()["status"]["receipt_readiness"] == "ready"

            prediction = original_core["predictions"][0]
            settle_payload = {
                "schema_version": "strathmark.shadow-numeric-outcome.v1",
                "consumer_id": _CONSUMER,
                "request_id": payload["request_id"],
                "run_revision": payload["run_revision"],
                "outcome_revision_id": "missoula:outcome-revision:settle-001",
                "reason_code": None,
                "revisions": [
                    {
                        "prediction_id": prediction["prediction_id"],
                        "competitor_id": prediction["competitor_id"],
                        "event_code": prediction["event_code"],
                        "action": "settle",
                        "actual_time": 42.5,
                        "expected_revision": 0,
                    }
                ],
            }
            settled = client.post(
                "/v1/shadow/outcomes/apply",
                headers=_headers("shadow.outcome.apply", payload["run_revision"]),
                json=settle_payload,
            )
            assert settled.status_code == 200, settled.text
            _validate(document, "NumericOutcomeResponse", settled.json())
            _reject_extra_field(
                document, "NumericOutcomeResponse", settled.json(), ("outcome", "revisions", 0)
            )

            void_payload = json.loads(json.dumps(settle_payload))
            void_payload["outcome_revision_id"] = "missoula:outcome-revision:void-001"
            void_payload["reason_code"] = "retract_invalid_numeric_evidence"
            void_payload["revisions"][0].update(
                {"action": "void", "actual_time": None, "expected_revision": 1}
            )
            voided = client.post(
                "/v1/shadow/outcomes/apply",
                headers=_headers("shadow.outcome.apply", payload["run_revision"]),
                json=void_payload,
            )
            assert voided.status_code == 200, voided.text
            _validate(document, "NumericOutcomeResponse", voided.json())

            after_void = client.post(
                "/v1/shadow/status",
                headers=_headers("shadow.status.read", payload["run_revision"]),
                json=status_payload,
            ).json()["status"]
            assert after_void["numeric_revision_count"] == 2
            assert after_void["active_numeric_settlement_count"] == 0
            assert after_void["voided_prediction_count"] == 1

            snapshot = restarted_store.get_evidence_snapshot_status(max_age_days=7)
            assert snapshot is not None
            assert snapshot.age_days <= 7
            assert snapshot.ready_for_offline is True

            health = client.get("/health", params={"prediction_as_of": "2026-11-02"})
            assert health.status_code == 200, health.text
            _validate(document, "HealthResponse", health.json())
            for nested_path in (
                ("prediction_engine",),
                ("prediction_engine", "core"),
                ("shadow_service",),
                ("shadow_service", "ledger_persistence"),
                ("shadow_service", "evidence_snapshot"),
                ("shadow_service", "evidence_snapshot", "attestation"),
            ):
                _reject_extra_field(document, "HealthResponse", health.json(), nested_path)
            assert health.json()["shadow_service"]["ready_for_trusted_shadow"] is True

            mirror = client.post(
                "/v1/shadow/mirror/replay",
                headers=_headers("shadow.mirror.replay", payload["run_revision"], roles=("admin",)),
                json={
                    "schema_version": "strathmark.shadow-mirror-replay.v1",
                    "consumer_id": _CONSUMER,
                    "run_revision": payload["run_revision"],
                    "limit": 25,
                    "timeout_ms": 5000,
                },
            )
            assert mirror.status_code == 200, mirror.text
            _validate(document, "MirrorReplayResponse", mirror.json())
            _reject_extra_field(document, "MirrorReplayResponse", mirror.json(), ("summary",))

            drift = client.post(
                "/v1/shadow/drift",
                headers=_headers("shadow.drift.read", payload["run_revision"]),
                json={
                    "schema_version": "strathmark.shadow-drift.v1",
                    "consumer_id": _CONSUMER,
                    "run_revision": payload["run_revision"],
                    "model_version": "fixture-core-a",
                    "lookback_days": 30,
                    "baseline_residuals": [-0.5, 0.0, 0.5],
                    "timeout_ms": 5000,
                },
            )
            assert drift.status_code == 200, drift.text
            _validate(document, "DriftResponse", drift.json())
            _reject_extra_field(document, "DriftResponse", drift.json(), ("report",))
            assert drift.json()["advisory_only"] is True
    finally:
        app.dependency_overrides.clear()
