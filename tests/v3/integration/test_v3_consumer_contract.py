from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from jsonschema import Draft202012Validator  # noqa: E402

import strathmark.v3.consumer_contract as contract_module  # noqa: E402
from strathmark.consumer_contract import shadow_consumer_contract_bytes  # noqa: E402
from strathmark.v3.api.app import create_v3_app  # noqa: E402
from strathmark.v3.api.auth import (  # noqa: E402
    InMemoryCredentialSecretStore,
    ServiceCredentialRegistry,
)
from strathmark.v3.api.schemas import (  # noqa: E402
    AssembleFieldRequest,
    CredentialRevocationRequest,
    CredentialRotationRequest,
    ExecuteCommandRequest,
    IssueAcknowledgmentRequest,
    PrepareCardRequest,
    ReceiptLookupRequest,
    SettlementRequest,
)
from strathmark.v3.consumer_contract import (  # noqa: E402
    EXPECTED_V3_CONSUMER_PATHS,
    V3_CONSUMER_CONTRACT_VERSION,
    V3ConsumerContractIntegrityError,
    build_v3_consumer_contract,
    load_v3_consumer_contract,
    v3_consumer_contract_bytes,
    v3_consumer_contract_digest,
)
from strathmark.v3.contracts.commands import CommandKind  # noqa: E402
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore  # noqa: E402


def _validate(document, schema_ref: str, value) -> None:
    schema = {
        "$schema": document["jsonSchemaDialect"],
        "$ref": schema_ref,
        "components": document["components"],
    }
    Draft202012Validator(schema).validate(value)


def test_packaged_contract_is_canonical_checksum_verified_and_v3_only() -> None:
    raw = v3_consumer_contract_bytes()
    document = load_v3_consumer_contract()
    assert raw == (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert v3_consumer_contract_digest() == hashlib.sha256(raw).hexdigest()
    assert document["openapi"] == "3.1.0"
    assert document["info"]["x-strathmark-contract-version"] == V3_CONSUMER_CONTRACT_VERSION
    assert set(document["paths"]) == EXPECTED_V3_CONSUMER_PATHS
    assert all(path.startswith("/v3/") for path in document["paths"])
    assert b"smv3." not in raw.lower()
    assert b"bearer ey" not in raw.lower()


def test_frozen_contract_is_exact_fresh_generation_and_live_openapi(tmp_path: Path) -> None:
    frozen = load_v3_consumer_contract()
    assert build_v3_consumer_contract() == frozen

    registry = ServiceCredentialRegistry(
        SQLiteEventStore(tmp_path / "contract.sqlite3"), InMemoryCredentialSecretStore()
    )
    registry.bootstrap_offline(
        principal_id="actor:contract",
        listener_stopped=True,
        credential="smv3.contract-key.contract-secret-1234567890",
    )
    app = create_v3_app(gateway=object(), credentials=registry)  # type: ignore[arg-type]
    assert app.openapi() == frozen


def test_every_example_validates_against_frozen_schema_and_live_request_model() -> None:
    document = load_v3_consumer_contract()
    request_models = {
        "/v3/cards/prepare": PrepareCardRequest,
        "/v3/commands/execute": ExecuteCommandRequest,
        "/v3/fields/assemble": AssembleFieldRequest,
        "/v3/receipts/lookup": ReceiptLookupRequest,
        "/v3/issues/acknowledge": IssueAcknowledgmentRequest,
        "/v3/results/settle": SettlementRequest,
        "/v3/credentials/rotate": CredentialRotationRequest,
        "/v3/credentials/revoke": CredentialRevocationRequest,
    }
    for schema in document["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)
    for path, path_item in document["paths"].items():
        method = "get" if "get" in path_item else "post"
        operation = path_item[method]
        if path in request_models:
            media = operation["requestBody"]["content"]["application/json"]
            _validate(document, media["schema"]["$ref"], media["example"])
            request_models[path].model_validate(media["example"])
        success = next(code for code in operation["responses"] if code.startswith("2"))
        media = operation["responses"][success]["content"]["application/json"]
        _validate(document, media["schema"]["$ref"], media["example"])
        for error_code in ("400", "401", "413", "422", "503", "504"):
            error = operation["responses"][error_code]["content"]["application/json"]
            assert error["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}
            _validate(document, error["schema"]["$ref"], error["example"])


def test_all_transport_object_schemas_are_closed_and_required() -> None:
    document = load_v3_consumer_contract()
    schemas = document["components"]["schemas"]
    for name, schema in schemas.items():
        if schema.get("type") == "object" or "properties" in schema:
            assert schema.get("additionalProperties") is False, name
            assert set(schema.get("required", ())) == set(schema.get("properties", {})), name


def test_command_surface_covers_every_kind_without_exposing_offline_recovery_online() -> None:
    document = load_v3_consumer_contract()
    coverage = document["info"]["x-strathmark-command-coverage"]
    online = set(coverage["authenticated_application_port"])
    dedicated = set(coverage["dedicated_authenticated_routes"])
    offline = set(coverage["listener-stopped-offline-only"])
    assert online | dedicated | offline == {item.value for item in CommandKind}
    assert not (online & dedicated or online & offline or dedicated & offline)
    assert "recover_service_credential" in offline
    assert "bootstrap_service_credential" in offline
    enum_values = set(document["components"]["schemas"]["OnlineCommandKind"]["enum"])
    assert enum_values == online


def test_freezer_rebuilds_poisoned_output_and_never_changes_v1(tmp_path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[3]
    freezer_path = root / "scripts" / "freeze_v3_consumer_contract.py"
    spec = importlib.util.spec_from_file_location("v3_contract_freezer_test", freezer_path)
    assert spec is not None and spec.loader is not None
    freezer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(freezer)
    contract = tmp_path / "v3_consumer.openapi.json"
    checksum = tmp_path / "v3_consumer.openapi.sha256"
    monkeypatch.setattr(freezer, "CONTRACT", contract)
    monkeypatch.setattr(freezer, "CHECKSUM", checksum)
    v1_before = shadow_consumer_contract_bytes()

    assert freezer.main() == 0
    pristine = contract.read_bytes()
    poisoned = json.loads(pristine)
    poisoned["paths"]["/v3/stale"] = {"get": {}}
    contract.write_text(json.dumps(poisoned), encoding="utf-8")
    checksum.unlink()
    assert freezer.main() == 0
    assert contract.read_bytes() == pristine
    assert checksum.read_bytes() == hashlib.sha256(pristine).hexdigest().encode() + b"\n"
    assert shadow_consumer_contract_bytes() == v1_before


def test_installed_contract_loader_rejects_malformed_version_surface_and_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pristine = load_v3_consumer_contract()
    raw = json.dumps(pristine, sort_keys=True, separators=(",", ":")) + "\n"

    def resources(contract_text: str, checksum: str):
        monkeypatch.setattr(
            contract_module,
            "_resource_text",
            lambda name: checksum if name.endswith("sha256") else contract_text,
        )

    resources("{", "0" * 64)
    with pytest.raises(V3ConsumerContractIntegrityError, match="malformed"):
        load_v3_consumer_contract()
    with pytest.raises(V3ConsumerContractIntegrityError, match="malformed"):
        v3_consumer_contract_bytes()

    resources("[]", "0" * 64)
    with pytest.raises(V3ConsumerContractIntegrityError, match="object"):
        load_v3_consumer_contract()

    wrong_version = json.loads(raw)
    wrong_version["info"]["x-strathmark-contract-version"] = "wrong"
    resources(json.dumps(wrong_version), "0" * 64)
    with pytest.raises(V3ConsumerContractIntegrityError, match="version"):
        load_v3_consumer_contract()

    wrong_surface = json.loads(raw)
    wrong_surface["paths"]["/v3/unreviewed"] = {}
    resources(json.dumps(wrong_surface), "0" * 64)
    with pytest.raises(V3ConsumerContractIntegrityError, match="surface"):
        load_v3_consumer_contract()

    resources(raw, "not-a-checksum")
    with pytest.raises(V3ConsumerContractIntegrityError, match="checksum"):
        v3_consumer_contract_digest(document=pristine)
    resources(raw, "0" * 64)
    with pytest.raises(V3ConsumerContractIntegrityError, match="reviewed checksum"):
        v3_consumer_contract_digest(document=pristine)
