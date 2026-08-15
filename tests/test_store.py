"""Tests for strathmark/store.py — SQLite result persistence."""

import json
import sqlite3
from datetime import date

import pytest

from strathmark.store import ResultStore


def test_store_connection_context_closes_the_sqlite_handle(tmp_path):
    store = ResultStore(tmp_path / "closing-store.db")

    with store._connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


def test_cloud_mirror_accepts_active_v2_hash_algorithm(monkeypatch, tmp_path):
    from strathmark import db
    from strathmark.ledger import PredictionLedger
    from tests.test_ledger import _pred, _request_payload

    captured = []

    class RPC:
        def execute(self):
            return None

    class Client:
        def rpc(self, name, payload):
            captured.append((name, payload))
            return RPC()

    monkeypatch.setattr(db, "_get_client", lambda: Client())
    ledger_path = tmp_path / "active-v2-mirror.db"
    ledger = PredictionLedger(ledger_path)
    ledger.record_field("api", "active-v2-mirror", _request_payload(), [_pred()])
    with sqlite3.connect(ledger_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM prediction_mirror_outbox WHERE kind = 'field'"
        ).fetchone()
    assert row is not None
    payload = json.loads(row[0])

    assert db.mirror_prediction_ledger(payload) is True
    assert captured[0][1]["ledger_payload"]["request"]["hash_algorithm"] == "active-v2"


@pytest.fixture
def tmp_store(tmp_path):
    """Create a ResultStore backed by a temporary database."""
    db_path = tmp_path / "test_results.db"
    return ResultStore(db_path=db_path)


class TestResultStore:
    def test_record_and_retrieve(self, tmp_store):
        inserted = tmp_store.record_result(
            competitor_name="Alice",
            event_code="SB",
            time_seconds=45.0,
            species="poplar",
            diameter_mm=300,
            quality=5,
            result_date=date(2025, 6, 1),
        )
        assert inserted is True
        history = tmp_store.get_competitor_history("Alice")
        assert len(history) == 1
        assert history[0].time_seconds == 45.0

    def test_duplicate_skipped(self, tmp_store):
        kwargs = dict(
            competitor_name="Alice",
            event_code="SB",
            time_seconds=45.0,
            species="poplar",
            diameter_mm=300,
            quality=5,
        )
        assert tmp_store.record_result(**kwargs) is True
        assert tmp_store.record_result(**kwargs) is False

    def test_count(self, tmp_store):
        assert tmp_store.count() == 0
        tmp_store.record_result(
            competitor_name="A",
            event_code="SB",
            time_seconds=50.0,
            species="poplar",
            diameter_mm=300,
            quality=5,
        )
        assert tmp_store.count() == 1

    def test_get_competitors(self, tmp_store):
        tmp_store.record_result(
            competitor_name="Bob",
            event_code="SB",
            time_seconds=50.0,
            species="poplar",
            diameter_mm=300,
            quality=5,
        )
        tmp_store.record_result(
            competitor_name="Alice",
            event_code="SB",
            time_seconds=45.0,
            species="poplar",
            diameter_mm=300,
            quality=5,
        )
        names = tmp_store.get_competitors()
        assert "Alice" in names
        assert "Bob" in names

    def test_filter_by_event_code(self, tmp_store):
        tmp_store.record_result(
            competitor_name="A",
            event_code="SB",
            time_seconds=50.0,
            species="poplar",
            diameter_mm=300,
            quality=5,
        )
        tmp_store.record_result(
            competitor_name="A",
            event_code="UH",
            time_seconds=60.0,
            species="poplar",
            diameter_mm=300,
            quality=5,
        )
        sb_only = tmp_store.get_competitor_history("A", event_code="SB")
        assert len(sb_only) == 1
        assert sb_only[0].event_code == "SB"

    def test_get_all_as_dataframe(self, tmp_store):
        tmp_store.record_result(
            competitor_name="A",
            event_code="SB",
            time_seconds=50.0,
            species="poplar",
            diameter_mm=300,
            quality=5,
        )
        df = tmp_store.get_all_as_dataframe()
        assert len(df) == 1

    def test_empty_store_returns_empty(self, tmp_store):
        history = tmp_store.get_competitor_history("Nobody")
        assert history == []
        assert tmp_store.get_competitors() == []
        df = tmp_store.get_all_as_dataframe()
        assert len(df) == 0

    def test_prediction_ledger_factory_keeps_isolated_store_path_and_mirror(self, tmp_store):
        def mirror(payload):
            return payload

        ledger = tmp_store.prediction_ledger(mirror=mirror)

        assert ledger.path == tmp_store.path
        assert ledger._mirror is mirror
