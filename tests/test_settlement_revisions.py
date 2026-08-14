"""Trusted numeric settlement revision tests.

Every test uses a temporary SQLite ledger and injected mirror doubles.  The
operational outcome remains in Missoula; STRATHMARK receives only eligible
numeric settlement or void projections.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from strathmark.drift import evaluate_settled_drift
from strathmark.ledger import (
    LEGACY_SETTLEMENT_REASON_CODES,
    MAX_NUMERIC_RAW_TIME_SECONDS,
    NUMERIC_OUTCOME_REASON_CODES,
    LedgerPrediction,
    NumericSettlementRevision,
    PredictionLedger,
    SettlementConflictError,
    canonical_hash,
)
from strathmark.shadow import RECEIPT_CORE_SCHEMA_VERSION
from strathmark.store import ResultStore


def _prediction(competitor_id: str) -> LedgerPrediction:
    return LedgerPrediction(
        competitor_id=competitor_id,
        event_code="SB",
        median_seconds=42.5,
        assigned_mark=3,
        source="baseline",
        engine_version="2.0.0",
        model_version="core-test",
        calibration_version="cal-test",
        evidence_cutoff=date(2026, 8, 11),
        interval_lower=35.0,
        interval_upper=52.0,
        interval_coverage=0.9,
        interval_state="calibrated",
        interval_scope="global",
        feature_snapshot={"history_count": 4.0},
        training_eligible=True,
    )


def _field(
    ledger: PredictionLedger,
    *competitor_ids: str,
    caller_id: str = "missoula:service:shadow",
    request_id: str = "missoula:request:field-1",
) -> tuple[str, ...]:
    return ledger.record_field(
        caller_id,
        request_id,
        {
            "event_code": "SB",
            "prediction_as_of": "2026-08-11",
            "competitors": [{"competitor_id": item} for item in competitor_ids],
        },
        [_prediction(item) for item in competitor_ids],
    ).prediction_ids


def _field_with_receipt(ledger: PredictionLedger, competitor_id: str) -> tuple[str, str, str]:
    caller_id = "missoula:service:shadow"
    request_id = "missoula:request:receipt-field"
    caller_input = {"competitors": [{"competitor_id": competitor_id}]}
    active_input = {"caller_input": caller_input}
    fingerprint = canonical_hash(active_input)
    prediction_ids = ledger.record_field(
        caller_id,
        request_id,
        {
            "event_code": "SB",
            "prediction_as_of": "2026-08-11",
            "competitors": [{"competitor_id": competitor_id}],
        },
        [_prediction(competitor_id)],
        receipt_metadata={
            "schema_version": RECEIPT_CORE_SCHEMA_VERSION,
            "consumer_id": caller_id,
            "request_id": request_id,
            "active_input": {**active_input, "fingerprint": fingerprint},
        },
    ).prediction_ids
    return prediction_ids[0], request_id, fingerprint


def _settle(
    prediction_id: str,
    competitor_id: str,
    actual_time: float,
    *,
    expected_revision: int,
) -> NumericSettlementRevision:
    return NumericSettlementRevision(
        prediction_id=prediction_id,
        competitor_id=competitor_id,
        event_code="SB",
        action="settle",
        actual_time=actual_time,
        expected_revision=expected_revision,
    )


def _void(
    prediction_id: str,
    competitor_id: str,
    *,
    expected_revision: int,
) -> NumericSettlementRevision:
    return NumericSettlementRevision(
        prediction_id=prediction_id,
        competitor_id=competitor_id,
        event_code="SB",
        action="void",
        actual_time=None,
        expected_revision=expected_revision,
    )


def test_numeric_outcome_revision_is_field_atomic_on_one_bad_prediction(tmp_path):
    path = tmp_path / "atomic.db"
    ledger = PredictionLedger(path)
    prediction_ids = _field(
        ledger,
        "missoula:competitor:1",
        "missoula:competitor:2",
    )

    with pytest.raises(SettlementConflictError, match="competitor_id"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:1",
            [
                _settle(
                    prediction_ids[0],
                    "missoula:competitor:1",
                    43.0,
                    expected_revision=0,
                ),
                _settle(
                    prediction_ids[1],
                    "missoula:competitor:wrong",
                    44.0,
                    expected_revision=0,
                ),
            ],
            actor="missoula:operator:judge-1",
        )

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM numeric_outcome_revisions").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM numeric_settlement_revisions").fetchone() == (0,)


def test_exact_retry_is_duplicate_but_changed_or_stale_revision_conflicts(tmp_path):
    ledger = PredictionLedger(tmp_path / "idempotency.db")
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    revision = _settle(
        prediction_id,
        "missoula:competitor:1",
        43.0,
        expected_revision=0,
    )

    first = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [revision],
        actor="missoula:operator:judge-1",
    )
    retry = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [revision],
        actor="missoula:operator:judge-1",
    )

    assert first.status == "recorded"
    assert retry.status == "duplicate"
    assert retry.revisions == first.revisions

    with pytest.raises(SettlementConflictError, match="different payload"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:1",
            [
                _settle(
                    prediction_id,
                    "missoula:competitor:1",
                    43.1,
                    expected_revision=0,
                )
            ],
            actor="missoula:operator:judge-1",
        )

    with pytest.raises(SettlementConflictError, match="expected revision 0.*latest is 1"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:2",
            [
                _settle(
                    prediction_id,
                    "missoula:competitor:1",
                    43.2,
                    expected_revision=0,
                )
            ],
            actor="missoula:operator:judge-1",
            reason_code="corrected_time",
        )


def test_finish_void_and_later_valid_replacement_are_append_only_latest_authority(tmp_path):
    path = tmp_path / "void-replace.db"
    ledger = PredictionLedger(path)
    prediction_id = _field(ledger, "missoula:competitor:1")[0]

    initial = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 70.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    assert ledger.get_training_rows()[0]["actual_time"] == 70.0

    voided = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:2",
        [_void(prediction_id, "missoula:competitor:1", expected_revision=1)],
        actor="missoula:operator:judge-1",
        reason_code="retract_invalid_numeric_evidence",
    )
    assert ledger.get_training_rows() == []

    replacement = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:3",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=2)],
        actor="missoula:operator:judge-1",
        reason_code="valid_replacement",
    )

    rows = ledger.get_training_rows(model_version="core-test")
    assert [(row["actual_time"], row["revision"]) for row in rows] == [(43.0, 3)]
    assert initial.revisions[0].action == "settle"
    assert voided.revisions[0].action == "void"
    assert voided.revisions[0].actual_time is None
    assert replacement.revisions[0].supersedes_revision_id == voided.revisions[0].revision_id

    report = evaluate_settled_drift(
        rows,
        baseline_residuals=[0.0] * 100,
        model_version_id="core-test",
    )
    assert report.recent_count == 1
    assert report.sample_label == "insufficient_recent_sample"

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM numeric_settlement_revisions").fetchone() == (3,)


def test_positive_finish_correction_requires_reason_code_and_replaces_training_value(tmp_path):
    ledger = PredictionLedger(tmp_path / "correction.db")
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 70.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )

    with pytest.raises(SettlementConflictError, match="reason_code"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:2",
            [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=1)],
            actor="missoula:operator:judge-1",
        )

    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:2",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=1)],
        actor="missoula:operator:judge-1",
        reason_code="corrected_time",
    )

    [row] = ledger.get_training_rows()
    assert row["actual_time"] == 43.0
    assert row["revision"] == 2


def test_unknown_operational_fields_and_non_numeric_outcomes_are_rejected(tmp_path):
    ledger = PredictionLedger(tmp_path / "strict.db")
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    base = {
        "prediction_id": prediction_id,
        "competitor_id": "missoula:competitor:1",
        "event_code": "SB",
        "action": "void",
        "actual_time": None,
        "expected_revision": 0,
    }

    with pytest.raises(ValueError, match="unknown properties: outcome_status"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:1",
            [{**base, "outcome_status": "DQ"}],
            actor="missoula:operator:judge-1",
            reason_code="retract_invalid_numeric_evidence",
        )

    with pytest.raises(ValueError, match="action must be 'settle' or 'void'"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:2",
            [{**base, "action": "DQ"}],
            actor="missoula:operator:judge-1",
            reason_code="retract_invalid_numeric_evidence",
        )


def test_mirror_pending_and_recovery_are_derived_without_exposing_payload(tmp_path):
    attempts = []
    online = {"value": False}

    def offline_then_online(payload):
        attempts.append(payload)
        if "numeric_outcome_revision" in payload and not online["value"]:
            raise OSError("offline")

    ledger = PredictionLedger(tmp_path / "mirror.db", mirror=offline_then_online)
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    result = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    ledger.flush_mirror_outbox()

    pending = ledger.get_numeric_outcome_revision("missoula:outcome-revision:1")
    assert result.cloud_status == "pending"
    assert pending is not None
    assert pending.cloud_status in {"pending", "retryable-failed"}
    assert not hasattr(pending, "payload")

    online["value"] = True
    ledger.flush_mirror_outbox()
    recovered = ledger.get_numeric_outcome_revision("missoula:outcome-revision:1")
    assert recovered is not None
    assert recovered.cloud_status == "recorded"
    assert len([item for item in attempts if "numeric_outcome_revision" in item]) >= 2


def test_numeric_reason_code_is_strict_and_mirror_contains_no_narrative_reason(tmp_path):
    attempts = []
    ledger = PredictionLedger(tmp_path / "reason-code.db", mirror=attempts.append)
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )

    assert NUMERIC_OUTCOME_REASON_CODES == {
        "corrected_time",
        "retract_invalid_numeric_evidence",
        "valid_replacement",
    }
    with pytest.raises(ValueError, match="reason_code"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:2",
            [_settle(prediction_id, "missoula:competitor:1", 42.0, expected_revision=1)],
            actor="missoula:operator:judge-1",
            reason_code="Judge says the clock operator made a transcription error",
        )

    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:2",
        [_settle(prediction_id, "missoula:competitor:1", 42.0, expected_revision=1)],
        actor="missoula:operator:judge-1",
        reason_code="corrected_time",
    )
    ledger.flush_mirror_outbox()
    [numeric_payload] = [
        payload
        for payload in attempts
        if payload.get("numeric_outcome_revision", {}).get("outcome_revision_id")
        == "missoula:outcome-revision:2"
    ]
    assert numeric_payload["numeric_outcome_revision"]["reason_code"] == "corrected_time"
    assert "reason" not in numeric_payload["numeric_outcome_revision"]


def test_monitoring_status_is_derived_and_labels_small_evidence_advisory(tmp_path):
    ledger = PredictionLedger(tmp_path / "monitoring.db")
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    initial = ledger.get_monitoring_status(model_version="core-test")

    assert initial.mirror == "not-configured"
    assert initial.numeric_revision_count == 0
    assert initial.evidence_sample_count == 0
    assert initial.evidence_status == "insufficient-evidence"
    assert initial.local_trust == "unavailable"
    assert initial.receipt_freshness == "unavailable"
    assert initial.receipt_readiness == "unavailable"
    assert initial.numeric_mirror == "not-configured"
    assert initial.numeric_mirror_backlog_count == 0
    assert initial.numeric_mirror_oldest_pending_at is None
    assert initial.numeric_mirror_last_attempt_at is None
    assert initial.drift_calibration_advisory == "insufficient-evidence"

    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    settled = ledger.get_monitoring_status(model_version="core-test")
    assert settled.active_numeric_settlement_count == 1
    assert settled.voided_prediction_count == 0
    assert settled.evidence_sample_count == 1
    assert settled.evidence_status == "insufficient-evidence"
    assert settled.drift_calibration_advisory == "insufficient-evidence"

    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:2",
        [_void(prediction_id, "missoula:competitor:1", expected_revision=1)],
        actor="missoula:operator:judge-1",
        reason_code="retract_invalid_numeric_evidence",
    )
    voided = ledger.get_monitoring_status(model_version="core-test")
    assert voided.numeric_revision_count == 2
    assert voided.active_numeric_settlement_count == 0
    assert voided.voided_prediction_count == 1
    assert voided.evidence_sample_count == 0


def test_monitoring_receipt_readiness_ignores_nonblocking_cloud_backlog(tmp_path):
    def offline(_payload):
        raise OSError("offline")

    ledger = PredictionLedger(tmp_path / "monitoring-receipt.db", mirror=offline)
    prediction_id, request_id, fingerprint = _field_with_receipt(ledger, "missoula:competitor:1")
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    ledger.flush_mirror_outbox()

    status = ledger.get_monitoring_status(
        model_version="core-test",
        caller_id="missoula:service:shadow",
        request_id=request_id,
        current_active_fingerprint=fingerprint,
    )
    assert status.local_trust == "recorded"
    assert status.receipt_freshness == "current"
    assert status.receipt_readiness == "ready"
    assert status.numeric_mirror in {"pending", "retryable-failed"}
    assert status.numeric_mirror_backlog_count == 1
    assert status.numeric_mirror_oldest_pending_at is not None
    assert status.numeric_mirror_last_attempt_at is not None

    stale = ledger.get_monitoring_status(
        caller_id="missoula:service:shadow",
        request_id=request_id,
        current_active_fingerprint="f" * 64,
    )
    assert stale.local_trust == "recorded"
    assert stale.receipt_freshness == "stale"
    assert stale.receipt_readiness == "not-ready"


def test_numeric_revision_requires_one_field_one_caller_and_matching_namespace(tmp_path):
    ledger = PredictionLedger(tmp_path / "field-binding.db")
    first = _field(
        ledger,
        "missoula:competitor:1",
        request_id="missoula:request:field-1",
    )[0]
    second = _field(
        ledger,
        "missoula:competitor:2",
        request_id="missoula:request:field-2",
    )[0]
    foreign = _field(
        ledger,
        "other:competitor:3",
        caller_id="other:service:shadow",
        request_id="other:request:field-3",
    )[0]

    with pytest.raises(SettlementConflictError, match="one ledger_request_id"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:mixed-field",
            [
                _settle(first, "missoula:competitor:1", 43.0, expected_revision=0),
                _settle(second, "missoula:competitor:2", 44.0, expected_revision=0),
            ],
            actor="missoula:operator:judge-1",
        )

    with pytest.raises(SettlementConflictError, match="one caller_id"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:mixed-caller",
            [
                _settle(first, "missoula:competitor:1", 43.0, expected_revision=0),
                _settle(foreign, "other:competitor:3", 44.0, expected_revision=0),
            ],
            actor="missoula:operator:judge-1",
        )

    with pytest.raises(SettlementConflictError, match="namespace"):
        ledger.apply_numeric_outcome_revision(
            "other:outcome-revision:1",
            [_settle(first, "missoula:competitor:1", 43.0, expected_revision=0)],
            actor="missoula:operator:judge-1",
        )

    result = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(first, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    with sqlite3.connect(ledger.path) as conn:
        ledger_request_id, caller_id = conn.execute(
            "SELECT ledger_request_id, caller_id FROM numeric_outcome_revisions"
        ).fetchone()
    assert result.ledger_request_id == ledger_request_id
    assert result.caller_id == caller_id == "missoula:service:shadow"


def test_legacy_then_numeric_is_allowed_but_numeric_then_legacy_is_rejected(tmp_path):
    ledger = PredictionLedger(tmp_path / "one-authority.db")
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    legacy = ledger.settle(
        prediction_id,
        "missoula:competitor:1",
        "SB",
        44.0,
        "legacy-official",
    )
    numeric = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=1)],
        actor="missoula:operator:judge-1",
        reason_code="corrected_time",
    )
    assert numeric.revisions[0].revision == 2
    assert numeric.revisions[0].supersedes_revision_id == legacy.settlement_id

    retry = ledger.settle(
        prediction_id,
        "missoula:competitor:1",
        "SB",
        44.0,
        "legacy-official",
    )
    assert retry.settlement_id == legacy.settlement_id
    assert retry.status in {"duplicate", "duplicate_cloud_pending"}

    with pytest.raises(SettlementConflictError, match="numeric.*authoritative"):
        ledger.settle(
            prediction_id,
            "missoula:competitor:1",
            "SB",
            42.0,
            "legacy-official",
            reason="legacy retry",
        )


def test_legacy_settlement_persists_and_mirrors_only_allowlisted_reason_codes(tmp_path):
    path = tmp_path / "legacy-reason.db"
    ledger = PredictionLedger(path)
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    ledger.settle(
        prediction_id,
        "missoula:competitor:1",
        "SB",
        44.0,
        "legacy-official",
    )

    with pytest.raises(ValueError, match="reason.*one of"):
        ledger.settle(
            prediction_id,
            "missoula:competitor:1",
            "SB",
            43.0,
            "legacy-official",
            reason="Judge notes that Alex's clock operator corrected the time",
        )

    correction = ledger.settle(
        prediction_id,
        "missoula:competitor:1",
        "SB",
        43.0,
        "legacy-official",
        reason="corrected_time",
    )
    assert correction.reason == "corrected_time"
    assert correction.reason in LEGACY_SETTLEMENT_REASON_CODES

    with sqlite3.connect(path) as conn:
        stored_reason = conn.execute(
            "SELECT reason FROM prediction_settlements WHERE settlement_id = ?",
            (correction.settlement_id,),
        ).fetchone()[0]
        mirrored_payload = conn.execute(
            """
            SELECT payload_json FROM prediction_mirror_outbox
            WHERE kind = 'settlement' AND entity_id = ?
            """,
            (correction.settlement_id,),
        ).fetchone()[0]

    assert stored_reason == "corrected_time"
    assert '"reason":"corrected_time"' in mirrored_payload
    assert "Alex" not in mirrored_payload


def test_pre_hardening_legacy_narrative_is_redacted_on_read_and_pending_delivery(tmp_path):
    path = tmp_path / "legacy-upgrade-redaction.db"
    ledger = PredictionLedger(path)
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    settlement_id = "legacy-settlement-with-narrative"
    narrative_actor = "Chief Official Jane Doe"
    narrative_reason = "Athlete Alex reported a private timing explanation"
    legacy_payload = {
        "settlement": {
            "settlement_id": settlement_id,
            "prediction_id": prediction_id,
            "revision": 1,
            "competitor_id": "missoula:competitor:1",
            "event_code": "SB",
            "actual_time": 43.0,
            "residual": 0.5,
            "actor": narrative_actor,
            "reason": narrative_reason,
            "payload_hash": "pre-hardening-hash",
            "supersedes_settlement_id": None,
            "settled_at": "2026-08-13T07:00:00+00:00",
        }
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO prediction_settlements (
                settlement_id, prediction_id, revision, competitor_id,
                event_code, actual_time, residual, actor, reason,
                payload_hash, supersedes_settlement_id, settled_at
            ) VALUES (?, ?, 1, 'missoula:competitor:1', 'SB', 43.0, 0.5,
                      ?, ?, 'pre-hardening-hash', NULL,
                      '2026-08-13T07:00:00+00:00')
            """,
            (settlement_id, prediction_id, narrative_actor, narrative_reason),
        )
        conn.execute(
            """
            INSERT INTO prediction_mirror_outbox (
                outbox_id, kind, entity_id, payload_json, created_at
            ) VALUES ('legacy-narrative-outbox', 'settlement', ?, ?,
                      '2026-08-13T07:00:00+00:00')
            """,
            (settlement_id, json.dumps(legacy_payload)),
        )

    [public_result] = PredictionLedger(path).get_settlements(prediction_id)
    assert public_result.actor.startswith("legacy:actor-")
    assert public_result.reason == "legacy_redacted"
    assert "Jane" not in repr(public_result)
    assert "Alex" not in repr(public_result)

    attempts = []
    mirrored = PredictionLedger(path, mirror=attempts.append)
    mirrored.flush_mirror_outbox()
    settlement_attempts = [item for item in attempts if "settlement" in item]
    assert len(settlement_attempts) == 1
    emitted = json.dumps(settlement_attempts[0], sort_keys=True)
    assert f'"actor": "{public_result.actor}"' in emitted
    assert '"reason": "legacy_redacted"' in emitted
    assert "Jane" not in emitted
    assert "Alex" not in emitted

    with sqlite3.connect(path) as conn:
        stored_actor, stored_reason = conn.execute(
            "SELECT actor, reason FROM prediction_settlements WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()
        stored_payload = conn.execute(
            "SELECT payload_json FROM prediction_mirror_outbox WHERE outbox_id = ?",
            ("legacy-narrative-outbox",),
        ).fetchone()[0]
    assert stored_actor == narrative_actor
    assert stored_reason == narrative_reason
    assert narrative_actor in stored_payload
    assert narrative_reason in stored_payload


def test_legacy_actor_pseudonymizes_unnamespaced_values_before_persisting(tmp_path):
    path = tmp_path / "legacy-actor.db"
    ledger = PredictionLedger(path)
    prediction_id = _field(ledger, "missoula:competitor:1")[0]

    result = ledger.settle(
        prediction_id,
        "missoula:competitor:1",
        "SB",
        43.0,
        "Judge Jane Doe",
    )
    assert result.actor.startswith("legacy:actor-")
    assert "Jane" not in result.actor
    retry = ledger.settle(
        prediction_id,
        "missoula:competitor:1",
        "SB",
        43.0,
        "Judge Jane Doe",
    )
    assert retry.settlement_id == result.settlement_id
    assert retry.actor == result.actor
    with sqlite3.connect(path) as conn:
        stored_actor = conn.execute(
            "SELECT actor FROM prediction_settlements WHERE settlement_id = ?",
            (result.settlement_id,),
        ).fetchone()[0]
        payload = conn.execute(
            "SELECT payload_json FROM prediction_mirror_outbox WHERE entity_id = ?",
            (result.settlement_id,),
        ).fetchone()[0]
    assert stored_actor == result.actor
    assert f'"actor":"{result.actor}"' in payload
    assert "Jane" not in stored_actor
    assert "Jane" not in payload


def test_numeric_actor_namespace_must_match_the_bound_caller(tmp_path):
    ledger = PredictionLedger(tmp_path / "actor-namespace.db")
    prediction_id = _field(ledger, "missoula:competitor:1")[0]

    with pytest.raises(SettlementConflictError, match="actor.*namespace"):
        ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:1",
            [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
            actor="other:operator:judge-1",
        )


def test_monitoring_uses_latest_numeric_mirror_attempt_and_does_not_infer_drift(tmp_path):
    def offline(_payload):
        raise OSError("offline")

    path = tmp_path / "monitoring-attempts.db"
    ledger = PredictionLedger(path, mirror=offline)
    first = _field(
        ledger,
        "missoula:competitor:1",
        request_id="missoula:request:attempt-1",
    )[0]
    second = _field(
        ledger,
        "missoula:competitor:2",
        request_id="missoula:request:attempt-2",
    )[0]
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:attempt-1",
        [_settle(first, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:attempt-2",
        [_settle(second, "missoula:competitor:2", 44.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )
    ledger.flush_mirror_outbox()

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            """
            SELECT o.outbox_id
            FROM numeric_outcome_revisions r
            JOIN prediction_mirror_outbox o
              ON o.kind = 'settlement' AND o.entity_id = r.field_revision_id
            ORDER BY r.outcome_revision_id
            """
        ).fetchall()
        assert len(rows) == 2
        conn.execute(
            "UPDATE prediction_mirror_delivery SET last_attempt_at = ? WHERE outbox_id = ?",
            ("2026-08-13T08:00:00+00:00", rows[0][0]),
        )
        conn.execute(
            "UPDATE prediction_mirror_delivery SET last_attempt_at = ? WHERE outbox_id = ?",
            ("2026-08-13T09:00:00+00:00", rows[1][0]),
        )

    status = ledger.get_monitoring_status(model_version="core-test")
    assert status.numeric_mirror_last_attempt_at == "2026-08-13T09:00:00+00:00"
    assert status.evidence_status == "insufficient-evidence"
    assert status.drift_calibration_advisory == "insufficient-evidence"


def test_monitoring_reports_evidence_floor_without_claiming_drift_evaluation(tmp_path):
    ledger = PredictionLedger(tmp_path / "monitoring-evidence.db")
    competitor_ids = tuple(f"missoula:competitor:{index}" for index in range(20))
    prediction_ids = _field(ledger, *competitor_ids)
    ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:evidence-floor",
        [
            _settle(prediction_id, competitor_id, 43.0, expected_revision=0)
            for prediction_id, competitor_id in zip(prediction_ids, competitor_ids, strict=True)
        ],
        actor="missoula:operator:judge-1",
    )

    status = ledger.get_monitoring_status(model_version="core-test")
    assert status.evidence_sample_count == 20
    assert status.evidence_status == "minimum-sample-available"
    assert status.drift_calibration_advisory == "not-evaluated"


def test_preexisting_revision_tie_deterministically_prefers_numeric_authority(tmp_path):
    path = tmp_path / "tie.db"
    ledger = PredictionLedger(path)
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    legacy = ledger.settle(
        prediction_id,
        "missoula:competitor:1",
        "SB",
        70.0,
        "legacy-official",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO numeric_outcome_revisions (
                field_revision_id, outcome_revision_id, ledger_request_id,
                caller_id, payload_hash, actor, reason_code, created_at
            ) SELECT 'field-tie', 'missoula:outcome-revision:tie',
                     p.ledger_request_id, q.caller_id, 'hash-tie',
                     'missoula:operator:judge-1', 'corrected_time',
                     '2026-08-13T00:00:00+00:00'
              FROM ledger_predictions p
              JOIN prediction_requests q USING (ledger_request_id)
             WHERE p.prediction_id = ?
            """,
            (prediction_id,),
        )
        conn.execute(
            """
            INSERT INTO numeric_settlement_revisions (
                revision_id, field_revision_id, prediction_id, revision,
                competitor_id, event_code, action, actual_time, residual,
                supersedes_revision_id, created_at
            ) VALUES ('numeric-tie', 'field-tie', ?, 1,
                      'missoula:competitor:1', 'SB', 'settle', 43.0, 0.5,
                      ?, '2026-08-13T00:00:00+00:00')
            """,
            (prediction_id, legacy.settlement_id),
        )

    [row] = ledger.get_training_rows()
    assert row["actual_time"] == 43.0
    followup = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:after-tie",
        [_settle(prediction_id, "missoula:competitor:1", 42.0, expected_revision=1)],
        actor="missoula:operator:judge-1",
        reason_code="corrected_time",
    )
    assert followup.revisions[0].revision == 2
    assert followup.revisions[0].supersedes_revision_id == "numeric-tie"


@pytest.mark.parametrize(
    ("actual_time", "accepted"),
    [
        (0.0, False),
        (MAX_NUMERIC_RAW_TIME_SECONDS, True),
        (MAX_NUMERIC_RAW_TIME_SECONDS + 0.001, False),
    ],
)
def test_numeric_raw_time_boundaries(tmp_path, actual_time, accepted):
    ledger = PredictionLedger(tmp_path / f"raw-time-{actual_time}.db")
    prediction_id = _field(ledger, "missoula:competitor:1")[0]

    def call():
        return ledger.apply_numeric_outcome_revision(
            "missoula:outcome-revision:1",
            [_settle(prediction_id, "missoula:competitor:1", actual_time, expected_revision=0)],
            actor="missoula:operator:judge-1",
        )

    if accepted:
        assert call().revisions[0].actual_time == actual_time
    else:
        with pytest.raises(ValueError, match="actual_time"):
            call()


def test_result_store_reopens_numeric_revision_without_losing_authority(tmp_path):
    path = tmp_path / "shared-store.db"
    store = ResultStore(path)
    ledger = store.prediction_ledger()
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    first = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )

    reopened = (
        ResultStore(path)
        .prediction_ledger()
        .get_numeric_outcome_revision("missoula:outcome-revision:1")
    )

    assert reopened is not None
    assert reopened.revisions == first.revisions
    assert reopened.status == "recorded"
    assert reopened.cloud_status == "not_configured"


def test_numeric_revision_tables_are_append_only(tmp_path):
    path = tmp_path / "immutable.db"
    ledger = PredictionLedger(path)
    prediction_id = _field(ledger, "missoula:competitor:1")[0]
    result = ledger.apply_numeric_outcome_revision(
        "missoula:outcome-revision:1",
        [_settle(prediction_id, "missoula:competitor:1", 43.0, expected_revision=0)],
        actor="missoula:operator:judge-1",
    )

    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE numeric_outcome_revisions SET reason_code = 'corrected_time' "
                "WHERE outcome_revision_id = 'missoula:outcome-revision:1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM numeric_settlement_revisions WHERE revision_id = ?",
                (result.revisions[0].revision_id,),
            )
