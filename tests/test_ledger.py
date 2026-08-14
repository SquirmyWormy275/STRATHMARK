"""Trusted Prediction Engine V2 ledger tests.

Every test uses a temporary SQLite database.  Cloud behavior is represented by
an injected callable and never contacts Supabase.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from strathmark.calculator import HandicapCalculator
from strathmark.ledger import (
    LedgerConflictError,
    LedgerPrediction,
    PredictionLedger,
    SettlementConflictError,
    canonical_hash,
)
from strathmark.predictor import (
    CompetitorRecord,
    PredictionBundle,
    PredictionContext,
    StaticPredictionProvider,
    WoodProfile,
)


def _pred(
    competitor_id: str = "competitor-1",
    *,
    source: str = "baseline",
    training_eligible: bool | None = None,
):
    if training_eligible is None:
        training_eligible = source != "manual"
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
        training_eligible=training_eligible,
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


def test_new_requests_record_active_v2_hash_algorithm(tmp_path):
    path = tmp_path / "ledger.db"
    PredictionLedger(path).record_field("api", "active", _request_payload(), [_pred()])

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT hash_algorithm FROM prediction_requests").fetchone() == (
            "active-v2",
        )


def test_existing_raw_v1_row_retries_with_its_recorded_algorithm(tmp_path):
    path = tmp_path / "legacy.db"
    raw_payload = _request_payload()
    active_payload = {**raw_payload, "competitors": [{"competitor_id": "competitor-1"}]}
    validated = PredictionLedger._validate_prediction(_pred())
    raw_digest = canonical_hash({"request": raw_payload, "predictions": [validated]})
    request_row_id = "legacy-request-row"
    prediction_id = "legacy-prediction-row"

    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE prediction_requests (
                ledger_request_id TEXT PRIMARY KEY,
                caller_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                event_code TEXT NOT NULL,
                prediction_as_of TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(caller_id, request_id)
            );
            CREATE TABLE ledger_predictions (
                prediction_id TEXT PRIMARY KEY,
                ledger_request_id TEXT NOT NULL,
                competitor_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                event_code TEXT NOT NULL,
                median_seconds REAL NOT NULL,
                assigned_mark INTEGER NOT NULL,
                source TEXT NOT NULL,
                training_eligible INTEGER NOT NULL,
                engine_version TEXT,
                model_version TEXT,
                calibration_version TEXT,
                evidence_cutoff TEXT,
                interval_lower REAL,
                interval_upper REAL,
                interval_coverage REAL,
                interval_state TEXT,
                interval_scope TEXT,
                ignored_factors_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                optimizer TEXT,
                optimizer_metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(ledger_request_id, competitor_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO prediction_requests VALUES (?, 'api', 'legacy', ?, 'SB', "
            "'2026-08-11', '2026-08-11T00:00:00Z')",
            (request_row_id, raw_digest),
        )
        conn.execute(
            """
            INSERT INTO ledger_predictions VALUES (
                ?, ?, 'competitor-1', 0, 'SB', 42.5, 3, 'baseline', 1,
                '2.0.0', 'core-test', 'cal-test', '2026-08-11', 35.0, 52.0,
                0.9, 'calibrated', 'global', '[]', '[]', 'joint_probability_v1',
                '{}', '2026-08-11T00:00:00Z'
            )
            """,
            (prediction_id, request_row_id),
        )
        conn.commit()

    retry = PredictionLedger(path).record_field(
        "api",
        "legacy",
        active_payload,
        [_pred()],
        legacy_request_payload=raw_payload,
    )

    assert retry.status == "duplicate"
    assert retry.prediction_ids == (prediction_id,)
    assert retry.request_hash == raw_digest
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT request_hash, hash_algorithm FROM prediction_requests"
        ).fetchone() == (raw_digest, "raw-v1")


def test_existing_raw_v1_retry_ignores_post_v1_optimizer_metadata(tmp_path):
    path = tmp_path / "legacy-optimizer.db"
    raw_payload = _request_payload()
    active_payload = {**raw_payload, "competitors": [{"competitor_id": "competitor-1"}]}
    prediction = _pred()
    object.__setattr__(
        prediction,
        "optimizer_metadata",
        {**prediction.optimizer_metadata, "search_strategy": "exhaustive_global"},
    )
    old_validated = PredictionLedger._validate_prediction(prediction)
    old_validated["optimizer_metadata"].pop("search_strategy")
    raw_digest = canonical_hash({"request": raw_payload, "predictions": [old_validated]})
    request_row_id = "legacy-request-row"

    ledger = PredictionLedger(path)
    with ledger._connect() as conn:
        conn.execute(
            """
            INSERT INTO prediction_requests (
                ledger_request_id, caller_id, request_id, request_hash, hash_algorithm,
                event_code, prediction_as_of, created_at
            ) VALUES (?, 'api', 'legacy-optimizer', ?, 'raw-v1', 'SB',
                      '2026-08-11', '2026-08-11T00:00:00Z')
            """,
            (request_row_id, raw_digest),
        )
        conn.execute(
            """
            INSERT INTO ledger_predictions (
                prediction_id, ledger_request_id, competitor_id, ordinal, event_code,
                median_seconds, assigned_mark, source, training_eligible,
                engine_version, model_version, calibration_version, evidence_cutoff,
                interval_lower, interval_upper, interval_coverage, interval_state,
                interval_scope, ignored_factors_json, warnings_json, optimizer,
                optimizer_metadata_json, created_at
            ) VALUES (
                'legacy-prediction-row', ?, 'competitor-1', 0, 'SB', 42.5, 3,
                'baseline', 1, '2.0.0', 'core-test', 'cal-test', '2026-08-11',
                35.0, 52.0, 0.9, 'calibrated', 'global', '[]', '[]',
                'joint_probability_v1', '{}', '2026-08-11T00:00:00Z'
            )
            """,
            (request_row_id,),
        )

    retry = ledger.record_field(
        "api",
        "legacy-optimizer",
        active_payload,
        [prediction],
        legacy_request_payload=raw_payload,
    )

    assert retry.status == "duplicate"
    assert retry.request_hash == raw_digest


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


def test_same_caller_key_with_changed_prediction_is_conflict(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    ledger.record_field("api", "model-swap", _request_payload(), [_pred()])
    changed_prediction = _pred()
    object.__setattr__(changed_prediction, "model_version", "core-next")
    object.__setattr__(changed_prediction, "median_seconds", 39.0)

    with pytest.raises(LedgerConflictError, match="different payload"):
        ledger.record_field(
            "api",
            "model-swap",
            _request_payload(),
            [changed_prediction],
        )


def test_request_keys_are_scoped_to_caller(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    a = ledger.record_field("caller-a", "key", _request_payload(), [_pred()])
    b = ledger.record_field("caller-b", "key", _request_payload(), [_pred()])
    assert a.prediction_ids != b.prediction_ids


def test_separate_ledgers_allocate_same_ids_for_same_caller_request(tmp_path):
    first = PredictionLedger(tmp_path / "first.db").record_field(
        "api", "shared-field", _request_payload(), [_pred()]
    )
    second = PredictionLedger(tmp_path / "second.db").record_field(
        "api", "shared-field", _request_payload(), [_pred()]
    )

    assert second.prediction_ids == first.prediction_ids


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
    assert retry.status in {"duplicate", "duplicate_cloud_pending"}

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


def test_degraded_fallback_is_recorded_but_excluded_from_training_rows(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    write = ledger.record_field(
        "api",
        "fallback",
        _request_payload(),
        [_pred(source="panel", training_eligible=False)],
    )
    ledger.settle(write.prediction_ids[0], "competitor-1", "SB", 45.0, "official")

    assert ledger.get_training_rows() == []


def test_ledger_forces_manual_and_panel_sources_ineligible(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    for index, source in enumerate(("manual", "panel")):
        competitor_id = f"competitor-{index}"
        write = ledger.record_field(
            "api",
            f"unsafe-{source}",
            _request_payload(competitor_id),
            [_pred(competitor_id, source=source, training_eligible=True)],
        )
        ledger.settle(write.prediction_ids[0], competitor_id, "SB", 45.0, "official")

    assert ledger.get_training_rows() == []


def test_ledger_forces_legacy_and_degraded_predictions_ineligible(tmp_path):
    ledger = PredictionLedger(tmp_path / "ledger.db")
    legacy = _pred("legacy", training_eligible=True)
    object.__setattr__(legacy, "engine_version", "legacy-baseline-v1")
    degraded = _pred("degraded", training_eligible=True)
    object.__setattr__(degraded, "degraded", True)
    for request_id, prediction in (("legacy", legacy), ("degraded", degraded)):
        write = ledger.record_field(
            "api",
            request_id,
            _request_payload(prediction.competitor_id),
            [prediction],
        )
        ledger.settle(write.prediction_ids[0], prediction.competitor_id, "SB", 45.0, "official")

    assert ledger.get_training_rows() == []


def test_ledger_rejects_fabricated_residual_source_from_training(tmp_path):
    ledger = PredictionLedger(tmp_path / "fabricated-source.db")
    prediction = _pred(source="fabricated+catboost_residual", training_eligible=True)

    write = ledger.record_field("api", "fabricated", _request_payload(), [prediction])
    ledger.settle(write.prediction_ids[0], "competitor-1", "SB", 45.0, "official")

    assert ledger.get_training_rows() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_version", None),
        ("calibration_version", None),
        ("evidence_cutoff", None),
        ("interval_lower", None),
        ("interval_upper", None),
        ("interval_coverage", None),
        ("interval_coverage", 1.0),
        ("interval_state", None),
        ("interval_scope", None),
    ],
)
def test_ledger_fails_closed_training_eligibility_for_incomplete_provenance(tmp_path, field, value):
    prediction = _pred(training_eligible=True)
    object.__setattr__(prediction, field, value)
    ledger = PredictionLedger(tmp_path / f"{field}.db")

    write = ledger.record_field("api", field, _request_payload(), [prediction])
    ledger.settle(write.prediction_ids[0], "competitor-1", "SB", 45.0, "official")

    assert ledger.get_training_rows() == []


def test_training_rows_project_issued_interval_and_cohort_metadata(tmp_path):
    ledger = PredictionLedger(tmp_path / "cohorts.db")
    write = ledger.record_field("api", "cohort", _request_payload(), [_pred()])
    ledger.settle(write.prediction_ids[0], "competitor-1", "SB", 45.0, "official")

    [row] = ledger.get_training_rows(
        model_version="core-test",
        calibration_version="cal-test",
        event_code="SB",
        history_band="4+",
    )

    assert row["interval_lower"] == 35.0
    assert row["interval_upper"] == 52.0
    assert row["nominal_coverage"] == 0.9
    assert row["interval_state"] == "calibrated"
    assert row["interval_scope"] == "global"
    assert row["history_count"] == 4
    assert row["history_band"] == "4+"


def test_cloud_mirror_failure_is_sanitized_and_nonfatal(tmp_path):
    mirrored = []

    def broken(payload):
        mirrored.append(payload)
        raise RuntimeError("secret cloud credential")

    ledger = PredictionLedger(tmp_path / "ledger.db", mirror=broken)
    result = ledger.record_field("api", "cloud", _request_payload(), [_pred()])
    flushed = ledger.flush_mirror_outbox()

    assert result.recorded is True
    assert result.status == "recorded_cloud_pending"
    assert result.cloud_status == "pending"
    assert flushed["failed"] <= 1
    assert "secret" not in result.status
    assert set(mirrored[0]) == {"request", "predictions", "features"}
    serialized = repr(mirrored[0])
    assert "Secret Name" not in serialized
    assert "'history':" not in serialized


def test_cloud_mirror_latency_is_off_the_calculation_response_path(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def slow(payload):
        started.set()
        release.wait(timeout=5)

    ledger = PredictionLedger(tmp_path / "ledger.db", mirror=slow)
    before = time.perf_counter()
    result = ledger.record_field("api", "slow-cloud", _request_payload(), [_pred()])
    elapsed = time.perf_counter() - before

    try:
        assert result.recorded is True
        assert result.cloud_status == "pending"
        assert elapsed < 0.5
        assert started.wait(timeout=1)
    finally:
        release.set()


def test_retry_storm_uses_one_nonblocking_worker_per_ledger(tmp_path, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    created_workers = []
    real_thread = threading.Thread

    def counting_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        created_workers.append(worker)
        return worker

    def blocked(payload):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr("strathmark.ledger.threading.Thread", counting_thread)
    ledger = PredictionLedger(tmp_path / "storm.db", mirror=blocked)
    try:
        first = ledger.record_field("api", "storm", _request_payload(), [_pred()])
        assert first.cloud_status == "pending"
        assert started.wait(timeout=1)
        for _ in range(100):
            retry = ledger.record_field("api", "storm", _request_payload(), [_pred()])
            assert retry.cloud_status == "pending"
        second = ledger.record_field("api", "storm-2", _request_payload(), [_pred()])
        assert second.cloud_status == "pending"
        assert len(created_workers) == 1
    finally:
        release.set()
        for worker in created_workers:
            worker.join(timeout=1)


def test_unique_mirror_storm_keeps_in_memory_queue_bounded(tmp_path, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    mirrored = []

    def blocked(payload):
        started.set()
        release.wait(timeout=5)
        mirrored.append(payload)

    monkeypatch.setattr("strathmark.ledger.MAX_MIRROR_QUEUE", 2, raising=False)
    ledger = PredictionLedger(tmp_path / "unique-storm.db", mirror=blocked)
    try:
        for index in range(6):
            competitor_id = f"competitor-{index}"
            ledger.record_field(
                "api",
                f"unique-{index}",
                _request_payload(competitor_id),
                [_pred(competitor_id)],
            )
        assert started.wait(timeout=1)
        assert len(ledger._delivery_queue) <= 2
        with ledger._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM prediction_mirror_outbox").fetchone()[0] == 6
    finally:
        release.set()

    deadline = time.monotonic() + 2
    while len(mirrored) < 6 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(mirrored) == 6


def test_explicit_flush_limit_preserves_remaining_durable_work(tmp_path):
    path = tmp_path / "flush-limit.db"
    ledger = PredictionLedger(path)
    for index in range(3):
        ledger.record_field("api", f"field-{index}", _request_payload(), [_pred()])

    mirrored = []
    ledger._mirror = mirrored.append
    summary = ledger.flush_mirror_outbox(limit=2)

    assert summary["recorded"] == 2
    assert len(mirrored) == 2
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM prediction_mirror_outbox o
            LEFT JOIN prediction_mirror_delivery d ON d.outbox_id = o.outbox_id
            WHERE d.status IS NULL OR d.status != 'recorded'
            """
        ).fetchone() == (1,)


def test_explicit_flush_prioritizes_never_attempted_rows_before_failed_retries(tmp_path):
    attempts = []

    def always_fails(payload):
        attempts.append(payload["request"]["request_id"])
        raise OSError("offline")

    path = tmp_path / "fair-flush.db"
    ledger = PredictionLedger(path)
    ledger.record_field("api", "oldest", _request_payload(), [_pred()])
    ledger.record_field("api", "newer", _request_payload(), [_pred()])
    ledger._mirror = always_fails

    first = ledger.flush_mirror_outbox(limit=1)
    second = ledger.flush_mirror_outbox(limit=1)

    assert first["failed"] == 1
    assert second["failed"] == 1
    assert attempts == ["oldest", "newer"]


def test_explicit_flush_retries_oldest_last_attempt_with_deterministic_ties(tmp_path):
    attempts = []

    def always_fails(payload):
        attempts.append(payload["request"]["request_id"])
        raise OSError("offline")

    path = tmp_path / "fair-retry.db"
    ledger = PredictionLedger(path)
    for request_id in ("first", "second"):
        ledger.record_field("api", request_id, _request_payload(), [_pred()])
    ledger._mirror = always_fails
    ledger.flush_mirror_outbox(limit=2)
    attempts.clear()
    with ledger._connect() as conn:
        conn.execute(
            "UPDATE prediction_mirror_delivery SET last_attempt_at = ?",
            ("2026-08-14T00:00:00+00:00",),
        )

    ledger.flush_mirror_outbox(limit=1)

    assert attempts == ["first"]


def test_pending_mirror_claim_uses_bounded_keyset_pages_without_starvation(tmp_path, monkeypatch):
    monkeypatch.setattr("strathmark.ledger.MAX_MIRROR_PENDING_SCAN", 2)
    ledger = PredictionLedger(tmp_path / "pending-keyset.db")
    for index in range(7):
        competitor_id = f"competitor-{index}"
        ledger.record_field(
            "api",
            f"keyset-{index}",
            _request_payload(competitor_id),
            [_pred(competitor_id)],
        )
    with ledger._connect() as conn:
        ordered = [
            (str(row["kind"]), str(row["entity_id"]))
            for row in conn.execute(
                """
                SELECT kind, entity_id FROM prediction_mirror_outbox
                ORDER BY created_at, outbox_id
                """
            ).fetchall()
        ]
        indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(prediction_mirror_outbox)").fetchall()
        }
    assert "idx_prediction_mirror_outbox_pending_scan" in indexes

    attempted = set(ordered[:5])
    cursor = None
    claimed = []
    exhausted = False
    for _ in range(10):
        key, cursor, exhausted = ledger._claim_pending_delivery(attempted, after=cursor)
        if key is not None:
            claimed.append(key)
            attempted.add(key)
            with ledger._worker_lock:
                ledger._delivery_in_flight.discard(key)
        if exhausted:
            break
    else:
        pytest.fail("bounded pending cursor did not reach the end of the durable backlog")

    assert claimed == ordered[5:]


def test_automatic_mirror_scan_memory_is_bounded_by_page_not_backlog(tmp_path, monkeypatch):
    ledger = PredictionLedger(tmp_path / "bounded-auto-scan.db", mirror=lambda payload: None)
    payload = json.dumps({"schema_version": "test"})
    backlog = 2_000
    with ledger._connect() as conn:
        conn.executemany(
            """
            INSERT INTO prediction_mirror_outbox (
                outbox_id, kind, entity_id, payload_json, created_at
            ) VALUES (?, 'field', ?, ?, ?)
            """,
            [
                (
                    f"outbox-{index:05d}",
                    f"entity-{index:05d}",
                    payload,
                    f"2026-08-14T00:00:{index // 1_000:02d}.{index:06d}+00:00",
                )
                for index in range(backlog)
            ],
        )
    attempts = 0

    def fail_without_allocating(kind, entity_id):
        nonlocal attempts
        del kind, entity_id
        attempts += 1
        return "failed"

    monkeypatch.setattr(ledger, "_deliver_pending", fail_without_allocating)
    tracemalloc.start()
    ledger._mirror_worker_loop()
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert attempts == backlog
    assert peak_bytes < 350_000


def test_duplicate_retries_pending_cloud_outbox_without_new_local_rows(tmp_path):
    mirrored = []

    def flaky(payload):
        mirrored.append(payload)
        if len(mirrored) == 1:
            raise OSError("offline")

    path = tmp_path / "ledger.db"
    ledger = PredictionLedger(path, mirror=flaky)
    first = ledger.record_field("api", "cloud-retry", _request_payload(), [_pred()])
    ledger.flush_mirror_outbox()
    retry = ledger.record_field("api", "cloud-retry", _request_payload(), [_pred()])
    ledger.flush_mirror_outbox()
    third = ledger.record_field("api", "cloud-retry", _request_payload(), [_pred()])

    assert first.cloud_status == "pending"
    assert retry.status in {"duplicate", "duplicate_cloud_pending"}
    assert retry.cloud_status in {"pending", "recorded"}
    assert third.cloud_status == "recorded"
    assert len(mirrored) >= 2
    assert mirrored[0]["request"]["caller_id"] == "api"
    assert mirrored[0]["request"]["request_id"] == "cloud-retry"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_requests").fetchone()[0] == 1
        assert conn.execute("SELECT attempts FROM prediction_mirror_delivery").fetchone()[0] == 2


def test_post_commit_delivery_state_failure_keeps_local_field_recorded(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    ledger = PredictionLedger(path, mirror=lambda payload: True)
    original_connect = ledger._connect

    class DeliveryStateFailure:
        def __init__(self):
            self.connection = original_connect()

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, statement, parameters=()):
            if "INSERT INTO prediction_mirror_delivery" in statement:
                raise sqlite3.OperationalError("delivery state unavailable")
            return self.connection.execute(statement, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(ledger, "_connect", DeliveryStateFailure)

    result = ledger.record_field("api", "delivery-state", _request_payload(), [_pred()])

    assert result.recorded is True
    assert result.status == "recorded_cloud_pending"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM prediction_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM prediction_mirror_outbox").fetchone()[0] == 1


def test_duplicate_settlement_retries_pending_cloud_outbox(tmp_path):
    mirrored = []

    def flaky(payload):
        mirrored.append(payload)
        if "settlement" in payload and sum("settlement" in item for item in mirrored) == 1:
            raise OSError("offline")

    ledger = PredictionLedger(tmp_path / "ledger.db", mirror=flaky)
    write = ledger.record_field("api", "settlement-retry", _request_payload(), [_pred()])
    first = ledger.settle(write.prediction_ids[0], "competitor-1", "SB", 45.0, "official")
    ledger.flush_mirror_outbox()
    retry = ledger.settle(write.prediction_ids[0], "competitor-1", "SB", 45.0, "official")
    ledger.flush_mirror_outbox()

    assert first.cloud_status == "pending"
    assert retry.status == "duplicate"
    assert retry.cloud_status in {"pending", "recorded"}
    settlement_payloads = [item for item in mirrored if "settlement" in item]
    assert len(settlement_payloads) >= 2
    assert settlement_payloads[0] == settlement_payloads[1]


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
