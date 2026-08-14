"""Minimum, versioned cloud-mirror contract for trusted shadow evidence.

All Python tests use temporary SQLite ledgers and injected mirror doubles.  The
PostgreSQL assertions are static unless the explicit loopback-only rehearsal
gate in ``test_postgres_rehearsal.py`` is configured.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from pathlib import Path

import pytest

import tests.postgres_rehearsal as postgres_rehearsal
from strathmark import db
from strathmark.ledger import (
    MIRROR_DELIVERY_SCHEMA_VERSION,
    NUMERIC_OUTCOME_MIRROR_SCHEMA_VERSION,
    SHADOW_MIRROR_ENVELOPE_SCHEMA_VERSION,
    SHADOW_RECEIPT_MIRROR_SCHEMA_VERSION,
)
from tests.test_settlement_revisions import PredictionLedger, _field, _settle, _void

REPO_ROOT = Path(__file__).parents[1]
MIGRATION = REPO_ROOT / "strathmark/migrations/20260813_007_shadow_mirror_contract.sql"
DOWN = REPO_ROOT / "strathmark/migrations/20260813_007_shadow_mirror_contract.down.sql"


def _pending_payloads(ledger: PredictionLedger) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []
    ledger._mirror = captured.append
    summary = ledger.flush_mirror_outbox(limit=100)
    assert summary["recorded"] >= 1
    return captured


def _rehash(payload: dict[str, object]) -> None:
    semantic = dict(payload)
    semantic.pop("delivery")
    canonical_payload = json.dumps(
        semantic,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    payload["delivery"]["canonical_payload"] = canonical_payload
    payload["delivery"]["payload_hash"] = hashlib.sha256(
        canonical_payload.encode("utf-8")
    ).hexdigest()


def _inject_evidence_diagnostics(payload: dict[str, object]) -> dict[str, object]:
    core = payload["receipt"]["core"]
    competitor_id = core["predictions"][0]["competitor_id"]
    core["evidence_snapshot"] = {"diagnostics": {}}
    diagnostic = {
        "ordinal": 0,
        "competitor_id": competitor_id,
        "total_rows": 0,
        "included_rows": 0,
        "excluded_rows": 0,
        "excluded_by_reason": {},
        "canonicalization_version": "strathmark.evidence-canonicalization.v1",
    }
    core["evidence_diagnostics"] = [diagnostic]
    return diagnostic


def test_shadow_receipt_outbox_is_versioned_minimal_and_restart_replays_exactly(tmp_path):
    path = tmp_path / "receipt-mirror.db"
    ledger = PredictionLedger(path)
    _field(ledger, "missoula:competitor:1")

    first = _pending_payloads(ledger)
    assert len(first) == 1
    envelope = first[0]
    assert set(envelope) == {"schema_version", "kind", "delivery", "ledger", "receipt"}
    assert envelope["schema_version"] == SHADOW_MIRROR_ENVELOPE_SCHEMA_VERSION
    assert envelope["kind"] == "shadow_receipt"
    assert envelope["delivery"]["schema_version"] == MIRROR_DELIVERY_SCHEMA_VERSION
    assert set(envelope["delivery"]) == {
        "schema_version",
        "outbox_id",
        "entity_id",
        "created_at",
        "canonical_payload",
        "payload_hash",
    }
    canonical_payload = envelope["delivery"]["canonical_payload"]
    semantic_envelope = dict(envelope)
    semantic_envelope.pop("delivery")
    assert json.loads(canonical_payload) == semantic_envelope
    assert (
        envelope["delivery"]["payload_hash"]
        == hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    )
    receipt = envelope["receipt"]
    assert receipt["schema_version"] == SHADOW_RECEIPT_MIRROR_SCHEMA_VERSION
    assert receipt["core"]["schema_version"] == "strathmark.shadow-receipt-core.v1"
    assert receipt["identity_schema_version"] == "strathmark.namespaced-identity.v1"
    assert receipt["observation_schema_version"] == ("strathmark.shadow-observation-fingerprint.v1")
    assert receipt["observation_fingerprint"] == "2" * 64

    serialized = json.dumps(envelope, sort_keys=True).lower()
    for forbidden in (
        "competitor_name",
        "display_name",
        "fatigue_notes",
        "medical",
        "weather_history",
        "outcome_history",
        "secret",
        "email",
    ):
        assert forbidden not in serialized

    # A completed row does not replay.  Mark only its delivery state pending and
    # prove a new process sends the byte-identical immutable envelope.
    with ledger._connect() as conn:
        conn.execute("DELETE FROM prediction_mirror_delivery")
    restarted = PredictionLedger(path)
    second = _pending_payloads(restarted)
    assert second == first


def test_numeric_settle_void_envelopes_are_minimal_and_append_only(tmp_path):
    ledger = PredictionLedger(tmp_path / "numeric-mirror.db")
    [prediction_id] = _field(ledger, "missoula:competitor:1")
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:settle-1",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:void-1",
        [_void(prediction_id, "missoula:competitor:1", expected_revision=1)],
        actor="missoula:operator:judge-1",
        reason_code="retract_invalid_numeric_evidence",
    )

    payloads = _pending_payloads(ledger)
    numeric = [item for item in payloads if item["kind"] == "numeric_outcome_revision"]
    assert len(numeric) == 2
    assert all(item["schema_version"] == SHADOW_MIRROR_ENVELOPE_SCHEMA_VERSION for item in numeric)
    assert all(
        item["numeric_outcome_revision"]["schema_version"] == NUMERIC_OUTCOME_MIRROR_SCHEMA_VERSION
        for item in numeric
    )
    assert [item["numeric_outcome_revision"]["revisions"][0]["action"] for item in numeric] == [
        "settle",
        "void",
    ]
    assert numeric[1]["numeric_outcome_revision"]["reason_code"] == (
        "retract_invalid_numeric_evidence"
    )
    assert numeric[1]["numeric_outcome_revision"]["revisions"][0]["actual_time"] is None

    serialized = json.dumps(numeric, sort_keys=True).lower()
    for forbidden in (
        "dnf",
        "dq",
        "penalty",
        "nonfinish",
        "outcome_history",
        "context_history",
        "name",
        "notes",
    ):
        assert forbidden not in serialized


def test_cloud_client_dispatches_shadow_envelope_to_dedicated_rpc(monkeypatch, tmp_path):
    ledger = PredictionLedger(tmp_path / "dispatch.db")
    _field(ledger, "missoula:competitor:1")
    [payload] = _pending_payloads(ledger)
    captured = []

    class RPC:
        def execute(self):
            return None

    class Client:
        def rpc(self, name, params):
            captured.append((name, params))
            return RPC()

    monkeypatch.setattr(db, "_get_client", lambda: Client())
    assert db.mirror_prediction_ledger(payload) is True
    assert captured == [("append_shadow_mirror_v1", {"mirror_payload": payload})]


def test_cloud_client_accepts_semantically_equal_alternate_json_encoding(monkeypatch, tmp_path):
    ledger = PredictionLedger(tmp_path / "alternate-json.db")
    _field(ledger, "missoula:competitor:1")
    [payload] = _pending_payloads(ledger)
    semantic = dict(payload)
    semantic.pop("delivery")
    alternate_json = json.dumps(semantic, ensure_ascii=True, indent=2, sort_keys=False)
    payload["delivery"]["canonical_payload"] = alternate_json
    payload["delivery"]["payload_hash"] = hashlib.sha256(alternate_json.encode("utf-8")).hexdigest()

    class RPC:
        def execute(self):
            return None

    class Client:
        def rpc(self, name, params):
            assert name == "append_shadow_mirror_v1"
            assert params == {"mirror_payload": payload}
            return RPC()

    monkeypatch.setattr(db, "_get_client", lambda: Client())
    assert db.mirror_prediction_ledger(payload) is True


def test_cloud_client_rejects_well_formed_wrong_delivery_digest(monkeypatch, tmp_path):
    ledger = PredictionLedger(tmp_path / "wrong-digest.db")
    _field(ledger, "missoula:competitor:1")
    [payload] = _pending_payloads(ledger)
    payload["delivery"]["payload_hash"] = "0" * 64
    monkeypatch.setattr(
        db,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("network client must not be created")),
    )

    with pytest.raises(ValueError, match="canonical payload digest does not match"):
        db.mirror_prediction_ledger(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": "not-allowed"}),
        lambda value: value["receipt"].update({"competitor_name": "Private Person"}),
        lambda value: value["receipt"]["core"].update({"email": "private@example.invalid"}),
        lambda value: value["delivery"].update({"secret": "credential"}),
        lambda value: value["receipt"]["core"].update({"created_at": "2026-08-13T23:59:59+00:00"}),
    ],
)
def test_cloud_client_rejects_unsanitized_shadow_payload_before_client(
    monkeypatch, tmp_path, mutation
):
    ledger = PredictionLedger(tmp_path / "reject.db")
    _field(ledger, "missoula:competitor:1")
    [payload] = _pending_payloads(ledger)
    mutation(payload)
    monkeypatch.setattr(
        db,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("network client must not be created")),
    )

    with pytest.raises(ValueError, match="unsanitized|invalid|does not match"):
        db.mirror_prediction_ledger(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda ledger: ledger["request"].update({"unknown": "not-allowed"}),
        lambda ledger: ledger["request"].pop("request_hash"),
        lambda ledger: ledger["request"].update({"event_code": False}),
        lambda ledger: ledger["predictions"][0].update({"unknown": "not-allowed"}),
        lambda ledger: ledger["predictions"][0].pop("assigned_mark"),
        lambda ledger: ledger["predictions"][0].update({"training_eligible": 1}),
        lambda ledger: ledger["features"][0].update({"unknown": "not-allowed"}),
        lambda ledger: ledger["features"][0].pop("numeric_value"),
        lambda ledger: ledger["features"][0].update({"numeric_value": "1.0"}),
        lambda ledger: ledger.update({"predictions": []}),
    ],
)
def test_cloud_client_rejects_nonexact_embedded_legacy_rows_before_client(
    monkeypatch, tmp_path, mutation
):
    ledger = PredictionLedger(tmp_path / "nested-reject.db")
    _field(ledger, "missoula:competitor:1")
    [payload] = _pending_payloads(ledger)
    mutation(payload["ledger"])
    _rehash(payload)
    monkeypatch.setattr(
        db,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("network client must not be created")),
    )

    with pytest.raises(ValueError, match="request|prediction|feature|cardinality|invalid"):
        db.mirror_prediction_ledger(payload)


def test_cloud_client_accepts_bounded_evidence_count_maps(monkeypatch, tmp_path):
    ledger = PredictionLedger(tmp_path / "bounded-evidence-counts.db")
    _field(ledger, "missoula:competitor:1")
    [payload] = _pending_payloads(ledger)
    diagnostic = _inject_evidence_diagnostics(payload)
    core = payload["receipt"]["core"]
    core["evidence_snapshot"]["diagnostics"] = {
        "accepted_rows": 7,
        "invalid_time": 0,
    }
    diagnostic["excluded_by_reason"] = {
        "future_result": 2,
        "same_day_result": 1,
    }
    _rehash(payload)

    class RPC:
        def execute(self):
            return None

    class Client:
        def rpc(self, name, params):
            assert name == "append_shadow_mirror_v1"
            assert params == {"mirror_payload": payload}
            return RPC()

    monkeypatch.setattr(db, "_get_client", lambda: Client())
    assert db.mirror_prediction_ledger(payload) is True


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("ordinal", True),
        ("ordinal", 0.0),
        ("ordinal", "0"),
        ("ordinal", {}),
        ("ordinal", -1),
        ("ordinal", 2_147_483_648),
        ("total_rows", False),
        ("total_rows", 1.0),
        ("included_rows", "1"),
        ("included_rows", []),
        ("excluded_rows", -1),
        ("excluded_rows", 2_147_483_648),
        ("canonicalization_version", ""),
        ("canonicalization_version", "   "),
        ("canonicalization_version", 1),
        ("canonicalization_version", "v" * 129),
    ],
)
def test_cloud_client_rejects_mistyped_or_unbounded_evidence_diagnostic_scalars(
    monkeypatch, tmp_path, field_name, bad_value
):
    ledger = PredictionLedger(tmp_path / "invalid-evidence-diagnostic-scalars.db")
    _field(ledger, "missoula:competitor:1")
    [payload] = _pending_payloads(ledger)
    diagnostic = _inject_evidence_diagnostics(payload)
    diagnostic[field_name] = bad_value
    _rehash(payload)
    monkeypatch.setattr(
        db,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("network client must not be created")),
    )

    with pytest.raises(ValueError, match="evidence diagnostic"):
        db.mirror_prediction_ledger(payload)


def test_postgres_rehearsal_uses_exact_prediction_ledger_shadow_envelope(tmp_path):
    payload = postgres_rehearsal._prediction_ledger_shadow_payload(
        tmp_path / "postgres-shadow-source.db"
    )

    assert payload["kind"] == "shadow_receipt"
    assert json.loads(payload["delivery"]["canonical_payload"]) == {
        key: value for key, value in payload.items() if key != "delivery"
    }
    assert (
        payload["receipt"]["ledger_request_id"] == payload["ledger"]["request"]["ledger_request_id"]
    )


@pytest.mark.parametrize(
    ("target", "bad_value"),
    [
        ("snapshot", {f"reason_{index}": 0 for index in range(129)}),
        ("snapshot", {"Not_Machine_Code": 1}),
        ("snapshot", {"invalid-time": 1}),
        ("snapshot", {"invalid_time": -1}),
        ("snapshot", {"invalid_time": True}),
        ("snapshot", {"invalid_time": 1.0}),
        ("snapshot", {"invalid_time": "1"}),
        ("snapshot", {"invalid_time": {"nested": 1}}),
        ("excluded", {f"reason_{index}": 0 for index in range(129)}),
        ("excluded", {"Not_Machine_Code": 1}),
        ("excluded", {"invalid_time": -1}),
        ("excluded", {"invalid_time": False}),
        ("excluded", {"invalid_time": 1.5}),
        ("excluded", {"invalid_time": [1]}),
    ],
)
def test_cloud_client_rejects_unbounded_or_mistyped_evidence_count_maps_before_client(
    monkeypatch, tmp_path, target, bad_value
):
    ledger = PredictionLedger(tmp_path / "invalid-evidence-counts.db")
    _field(ledger, "missoula:competitor:1")
    [payload] = _pending_payloads(ledger)
    diagnostic = _inject_evidence_diagnostics(payload)
    core = payload["receipt"]["core"]
    if target == "snapshot":
        core["evidence_snapshot"]["diagnostics"] = bad_value
    else:
        diagnostic["excluded_by_reason"] = bad_value
    _rehash(payload)
    monkeypatch.setattr(
        db,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("network client must not be created")),
    )

    with pytest.raises(ValueError, match="bounded machine count map"):
        db.mirror_prediction_ledger(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda outcome, revision: outcome.update({"reason_code": None}),
            "reason_code",
        ),
        (
            lambda outcome, revision: revision.update({"supersedes_revision_id": None}),
            "supersedes_revision_id",
        ),
        (
            lambda outcome, revision: revision.update({"residual": math.inf}),
            "finite",
        ),
        (
            lambda outcome, revision: revision.update({"revision": 2_147_483_648}),
            "revision",
        ),
    ],
)
def test_cloud_client_rejects_invalid_noninitial_numeric_semantics_before_client(
    monkeypatch, tmp_path, mutation, message
):
    ledger = PredictionLedger(tmp_path / "numeric-reject.db")
    [prediction_id] = _field(ledger, "missoula:competitor:1")
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:settle-client",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:void-client",
        [_void(prediction_id, "missoula:competitor:1", expected_revision=1)],
        actor="missoula:operator:judge-1",
        reason_code="retract_invalid_numeric_evidence",
    )
    numeric = [
        item for item in _pending_payloads(ledger) if item["kind"] == "numeric_outcome_revision"
    ][-1]
    outcome = numeric["numeric_outcome_revision"]
    revision = outcome["revisions"][0]
    mutation(outcome, revision)
    if math.isfinite(revision.get("residual") or 0.0):
        _rehash(numeric)
    monkeypatch.setattr(
        db,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("network client must not be created")),
    )

    with pytest.raises(ValueError, match=message):
        db.mirror_prediction_ledger(numeric)


def test_migration_007_is_forward_only_hardened_and_in_the_disposable_matrix():
    sql = MIGRATION.read_text(encoding="utf-8")
    down = DOWN.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "CREATE OR REPLACE FUNCTION public.append_shadow_mirror_v1" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = ''" in sql
    assert "OWNER TO strathmark_prediction_rpc_owner" in sql
    assert "FROM PUBLIC, anon, authenticated" in normalized
    assert "TO service_role" in sql
    assert sql.count("FORCE ROW LEVEL SECURITY") == 4
    assert sql.count("BEFORE UPDATE OR DELETE") == 4
    assert "pg_catalog.jsonb_object_keys" in sql
    assert "pg_catalog.pg_column_size" in sql
    assert "pg_catalog.sha256" in sql
    assert "pg_catalog.convert_to" in sql
    assert "canonical payload digest does not match" in sql
    assert "canonical payload semantics do not match" in sql
    assert "EXECUTE format" not in sql
    assert "EXECUTE IMMEDIATE" not in sql
    assert "cannot roll back migration 007 while active shadow evidence exists" in down
    assert "RAISE EXCEPTION" in down
    assert "shadow mirror duplicate semantic conflict" in sql
    assert "numeric correction or void requires a reason_code" in sql
    assert "numeric settlement must supersede the exact latest authoritative revision" in sql
    assert "numeric residual does not match mirrored prediction" in sql
    assert "1e-9 seconds" in sql
    for forbidden_key in (
        "name",
        "display_name",
        "fatigue",
        "fatigue_notes",
        "medical",
        "medical_notes",
        "weather",
        "equipment",
        "outcome_history",
        "context_history",
        "penalty",
        "dnf",
        "dq",
        "notes",
        "secret",
        "email",
    ):
        assert f"'{forbidden_key}'" in sql
    assert "WITH RECURSIVE shadow_nodes" in sql
    assert "pg_catalog.lower(node_key)" in sql
    assert "numeric revision must be an exact bounded integer" in sql
    assert "numeric authority rejects legacy settlement append" in sql
    assert "CREATE CONSTRAINT TRIGGER prediction_ledger_settlements_numeric_authority" in sql
    assert "pg_catalog.pg_advisory_xact_lock" in sql
    assert "ORDER BY prediction_id" in sql
    assert "LOCK TABLE public.shadow_mirror_deliveries IN ACCESS EXCLUSIVE MODE" in down
    assert "SET LOCAL row_security = off" in down
    assert "pg_catalog.floor((item->>'revision')::pg_catalog.numeric)" in sql
    assert "shadow receipt core has unknown or missing properties" in sql
    assert "shadow receipt prediction identities are incomplete or duplicated" in sql
    assert "shadow receipt evidence snapshot diagnostics must be a bounded count map" in sql
    assert "shadow receipt excluded_by_reason must be a bounded count map" in sql
    assert "shadow receipt evidence diagnostic counts must be bounded nonnegative integers" in sql
    assert "shadow receipt evidence canonicalization_version is invalid" in sql
    rehearsal_source = inspect.getsource(postgres_rehearsal._run_matrix)
    assert "same claimed hash with changed receipt semantics conflicts" in rehearsal_source
    assert "same claimed hash with changed numeric semantics conflicts" in rehearsal_source
    assert "numeric residual must match mirrored median" in rehearsal_source
    assert "noninitial numeric revision requires a reason" in rehearsal_source
    assert "numeric supersession must target exact latest revision" in rehearsal_source
    assert "same claimed hash with changed nested ledger semantics conflicts" in rehearsal_source
    assert "direct RPC rejects every recursively prohibited privacy key" in rehearsal_source
    assert "wrong delivery digest cannot poison a legitimate receipt retry" in rehearsal_source
    assert "semantic receipt retry accepts alternate canonical JSON whitespace" in rehearsal_source
    assert "rehashed email field is rejected by the recursive privacy guard" in rehearsal_source
    assert "duplicate receipt prediction IDs are rejected" in rehearsal_source
    assert "missing receipt prediction IDs are rejected" in rehearsal_source
    assert "frozen receipt core rejects unknown, missing" in rehearsal_source
    assert "direct RPC rejects invalid bounded evidence count maps" in rehearsal_source
    assert "PredictionLedger envelope is accepted byte-for-byte" in rehearsal_source
    assert "fractional numeric revision is rejected before integer cast" in rehearsal_source
    assert "concurrent 006 append cannot cross guarded rollback" in rehearsal_source
    assert "concurrent 007 append cannot cross guarded rollback" in rehearsal_source
    assert "legacy-first authority race serializes into numeric revision" in rehearsal_source
    assert "numeric-first authority race rejects legacy append" in rehearsal_source
    assert "NOBYPASS owner guard fails closed under RLS" in rehearsal_source
    assert MIGRATION.name in inspect.getsource(postgres_rehearsal._run_matrix)
    assert DOWN.name in inspect.getsource(postgres_rehearsal._run_matrix)
