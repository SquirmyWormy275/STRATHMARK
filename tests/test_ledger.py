"""Trusted Prediction Engine V2 ledger tests.

Every test uses a temporary SQLite database.  Cloud behavior is represented by
an injected callable and never contacts Supabase.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from strathmark.calculator import HandicapCalculator
from strathmark.ledger import (
    LedgerConflictError,
    LedgerPrediction,
    PredictionLedger,
    SettlementConflictError,
)
from strathmark.predictor import (
    CompetitorRecord,
    PredictionBundle,
    PredictionContext,
    StaticPredictionProvider,
    WoodProfile,
)


def _pred(competitor_id: str = "competitor-1", *, source: str = "baseline"):
    return LedgerPrediction(
        competitor_id=competitor_id,
        event_code="SB",
        median_seconds=42.5,
        assigned_mark=3,
        source=source,
        engine_version="2.0.0",
        model_version="core-test",
        calibration_version="cal-test",
        evidence_cutoff=date(2026, 8, 11),
        interval_lower=35.0,
        interval_upper=52.0,
        interval_coverage=0.9,
        interval_state="calibrated",
        interval_scope="global",
        ignored_factors=("quality", "venue"),
        warnings=(),
        optimizer="joint_probability_v1",
        optimizer_metadata={"seed": 20260811, "simulations": 2048},
        feature_snapshot={
            "diameter_mm": 300.0,
            "history_count": 4.0,
            "gender_f": 0.0,
        },
    )


def _request_payload(competitor_id: str = "competitor-1"):
    return {
        "event_code": "SB",
        "prediction_as_of": "2026-08-11",
        "diameter_mm": 300.0,
        "species": "PINE",
        "seed": 20260811,
        "competitors": [
            {
                "competitor_id": competitor_id,
                "gender": "M",
                "history": [
                    {
                        "event_code": "SB",
                        "time_seconds": 43.0,
                        "result_date": "2026-07-01",
                        "diameter_mm": 300.0,
                        "species": "PINE",
                    }
                ],
            }
        ],
    }


def test_record_field_is_atomic_and_exact_retry_returns_original_ids(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")

    first = ledger.record_field(
        caller_id="api",
        request_id="request-1",
        request_payload=_request_payload(),
        predictions=[_pred()],
    )
    second = ledger.record_field(
        caller_id="api",
        request_id="request-1",
        request_payload=_request_payload(),
        predictions=[_pred()],
    )

    assert first.recorded is True
    assert first.status == "recorded"
    assert second.recorded is True
    assert second.status == "duplicate"
    assert second.prediction_ids == first.prediction_ids

    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ledger_predictions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM prediction_features").fetchone()[0] == 3


def test_same_caller_key_with_changed_payload_is_conflict(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    ledger.record_field(
        caller_id="api",
        request_id="same-key",
        request_payload=_request_payload(),
        predictions=[_pred()],
    )
    changed = _request_payload()
    changed["diameter_mm"] = 325.0

    with pytest.raises(LedgerConflictError, match="different payload"):
        ledger.record_field(
            caller_id="api",
            request_id="same-key",
            request_payload=changed,
            predictions=[_pred()],
        )


def test_request_keys_are_scoped_to_caller(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    a = ledger.record_field("caller-a", "key", _request_payload(), [_pred()])
    b = ledger.record_field("caller-b", "key", _request_payload(), [_pred()])
    assert a.prediction_ids != b.prediction_ids


def test_concurrent_duplicate_request_creates_one_field(tmp_path):
    path = tmp_path / "ledger.db"
    PredictionLedger(path)

    def write():
        return PredictionLedger(path).record_field(
            "api", "concurrent", _request_payload(), [_pred()]
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        writes = list(pool.map(lambda _: write(), range(12)))

    assert len({item.prediction_ids for item in writes}) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ledger_predictions").fetchone()[0] == 1


def test_transaction_rolls_back_if_any_prediction_is_invalid(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    invalid = _pred("")
    with pytest.raises(ValueError, match="stable competitor_id"):
        ledger.record_field("api", "invalid-field", _request_payload(), [_pred(), invalid])

    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ledger_predictions").fetchone()[0] == 0


def test_feature_snapshot_rejects_non_numeric_or_unallowlisted_values(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    prediction = _pred()
    object.__setattr__(prediction, "feature_snapshot", {"competitor_name": "Secret Name"})
    with pytest.raises(ValueError, match="numeric"):
        ledger.record_field("api", "unsafe", _request_payload(), [prediction])


def test_settlement_is_idempotent_and_correction_appends_revision(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    write = ledger.record_field("api", "settle", _request_payload(), [_pred()])
    prediction_id = write.prediction_ids[0]

    first = ledger.settle(
        prediction_id=prediction_id,
        competitor_id="competitor-1",
        event_code="SB",
        actual_time=44.25,
        actor="chief-handicapper",
    )
    retry = ledger.settle(
        prediction_id=prediction_id,
        competitor_id="competitor-1",
        event_code="SB",
        actual_time=44.25,
        actor="chief-handicapper",
    )
    assert retry.settlement_id == first.settlement_id
    assert retry.status == "duplicate"

    with pytest.raises(SettlementConflictError, match="reason"):
        ledger.settle(
            prediction_id=prediction_id,
            competitor_id="competitor-1",
            event_code="SB",
            actual_time=43.75,
            actor="chief-handicapper",
        )

    correction = ledger.settle(
        prediction_id=prediction_id,
        competitor_id="competitor-1",
        event_code="SB",
        actual_time=43.75,
        actor="chief-handicapper",
        reason="Timing review",
    )
    assert correction.revision == 2
    assert correction.supersedes_settlement_id == first.settlement_id

    events = ledger.get_settlements(prediction_id)
    assert [event.actual_time for event in events] == [44.25, 43.75]
    assert events[0].supersedes_settlement_id is None
    assert events[1].supersedes_settlement_id == events[0].settlement_id


@pytest.mark.parametrize(
    ("competitor_id", "event_code", "message"),
    [
        ("wrong-id", "SB", "competitor"),
        ("competitor-1", "UH", "event"),
    ],
)
def test_settlement_must_match_prediction_identity(tmp_path, competitor_id, event_code, message):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    write = ledger.record_field("api", "settle-match", _request_payload(), [_pred()])
    with pytest.raises(SettlementConflictError, match=message):
        ledger.settle(
            prediction_id=write.prediction_ids[0],
            competitor_id=competitor_id,
            event_code=event_code,
            actual_time=44.0,
            actor="official",
        )


def test_manual_predictions_are_recorded_but_excluded_from_training_rows(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    manual = ledger.record_field("api", "manual", _request_payload(), [_pred(source="manual")])
    model = ledger.record_field(
        "api", "model", _request_payload("competitor-2"), [_pred("competitor-2")]
    )
    ledger.settle(manual.prediction_ids[0], "competitor-1", "SB", 45.0, "official")
    ledger.settle(model.prediction_ids[0], "competitor-2", "SB", 43.0, "official")

    rows = ledger.get_training_rows()
    assert [row["competitor_id"] for row in rows] == ["competitor-2"]


def test_cloud_mirror_failure_is_sanitized_and_nonfatal(tmp_path):
    mirrored = []

    def broken(payload):
        mirrored.append(payload)
        raise RuntimeError("secret cloud credential")

    ledger = PredictionLedger(tmp_path / "ledger.db", mirror=broken)
    result = ledger.record_field("api", "cloud", _request_payload(), [_pred()])

    assert result.recorded is True
    assert result.status == "recorded_cloud_failed"
    assert result.cloud_status == "failed"
    assert "secret" not in result.status
    assert set(mirrored[0]) == {"request", "predictions", "features"}
    serialized = repr(mirrored[0])
    assert "Secret Name" not in serialized
    assert "'history':" not in serialized


def test_schema_creation_preserves_existing_results_rows(tmp_path):
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE results (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO results(value) VALUES ('keep-me')")
        conn.commit()

    PredictionLedger(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM results").fetchall() == [("keep-me",)]


def test_local_evidence_tables_reject_update_and_delete(tmp_path):
    path = tmp_path / "immutable.db"
    ledger = PredictionLedger(path)
    write = ledger.record_field("api", "immutable", _request_payload(), [_pred()])

    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE ledger_predictions SET median_seconds = 1 WHERE prediction_id = ?",
                (write.prediction_ids[0],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM ledger_predictions WHERE prediction_id = ?",
                (write.prediction_ids[0],),
            )


def test_calculator_records_complete_field_and_exposes_ledger_state(tmp_path):
    ledger = PredictionLedger(tmp_path / "calculator.db")
    calculator = HandicapCalculator(
        prediction_provider=StaticPredictionProvider(PredictionBundle()),
        ledger_sink=ledger,
        ledger_caller_id="python-test",
    )
    competitors = [
        CompetitorRecord(name="Alice", competitor_id="athlete-a"),
        CompetitorRecord(name="Bob", competitor_id="athlete-b"),
    ]
    context = PredictionContext(prediction_as_of=date(2026, 8, 11), request_id="field-1")

    first = calculator.calculate(competitors, WoodProfile("Pine", 300, 5), "SB", context=context)
    retry = calculator.calculate(competitors, WoodProfile("Pine", 300, 5), "SB", context=context)

    assert all(result.ledger_recorded is True for result in first)
    assert all(result.ledger_status == "recorded" for result in first)
    assert [result.prediction_id for result in retry] == [result.prediction_id for result in first]
    assert all(result.ledger_status == "duplicate" for result in retry)
    with sqlite3.connect(tmp_path / "calculator.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ledger_predictions").fetchone()[0] == 2
        feature_names = {
            row[0] for row in conn.execute("SELECT feature_name FROM prediction_features")
        }
        assert "diameter_mm" in feature_names
        assert "competitor_name" not in feature_names


def test_calculator_missing_stable_identity_does_not_write(tmp_path):
    ledger = PredictionLedger(tmp_path / "calculator.db")
    calculator = HandicapCalculator(
        prediction_provider=StaticPredictionProvider(PredictionBundle()),
        ledger_sink=ledger,
    )
    result = calculator.calculate(
        [CompetitorRecord(name="Display only")],
        WoodProfile("Pine", 300, 5),
        "SB",
        context=PredictionContext(prediction_as_of=date(2026, 8, 11), request_id="field"),
    )[0]

    assert result.prediction_id is None
    assert result.ledger_recorded is False
    assert result.ledger_status == "missing_competitor_id"
    with sqlite3.connect(tmp_path / "calculator.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_requests").fetchone()[0] == 0


def test_calculator_ledger_failure_never_changes_prediction_or_mark():
    class BrokenLedger:
        def record_field(self, *args, **kwargs):
            raise OSError("disk contains a sensitive path")

    provider = StaticPredictionProvider(PredictionBundle())
    competitors = [CompetitorRecord(name="Alice", competitor_id="athlete-a")]
    context = PredictionContext(prediction_as_of=date(2026, 8, 11), request_id="field")
    plain = HandicapCalculator(prediction_provider=provider).calculate(
        competitors, WoodProfile("Pine", 300, 5), "SB", context=context
    )[0]
    logged = HandicapCalculator(prediction_provider=provider, ledger_sink=BrokenLedger()).calculate(
        competitors, WoodProfile("Pine", 300, 5), "SB", context=context
    )[0]

    assert (logged.predicted_time, logged.mark) == (plain.predicted_time, plain.mark)
    assert logged.prediction_id is None
    assert logged.ledger_recorded is False
    assert logged.ledger_status == "write_failed"


def test_calculator_idempotency_hash_includes_effective_mark_ceiling(tmp_path):
    ledger = PredictionLedger(tmp_path / "ceiling.db")
    provider = StaticPredictionProvider(PredictionBundle())
    competitors = [CompetitorRecord(name="Alice", competitor_id="athlete-a")]
    context = PredictionContext(prediction_as_of=date(2026, 8, 11), request_id="field")
    HandicapCalculator(
        event_ceiling=100,
        prediction_provider=provider,
        ledger_sink=ledger,
    ).calculate(competitors, WoodProfile("Pine", 300, 5), "SB", context=context)

    conflicted = HandicapCalculator(
        event_ceiling=99,
        prediction_provider=provider,
        ledger_sink=ledger,
    ).calculate(competitors, WoodProfile("Pine", 300, 5), "SB", context=context)

    assert conflicted[0].ledger_recorded is False
    assert conflicted[0].ledger_status == "idempotency_conflict"
