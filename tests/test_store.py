"""Tests for strathmark/store.py — SQLite result persistence."""

import tempfile
from datetime import date
from pathlib import Path

import pytest

from strathmark.store import ResultStore


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
            competitor_name="Alice", event_code="SB", time_seconds=45.0,
            species="poplar", diameter_mm=300, quality=5,
        )
        assert tmp_store.record_result(**kwargs) is True
        assert tmp_store.record_result(**kwargs) is False

    def test_count(self, tmp_store):
        assert tmp_store.count() == 0
        tmp_store.record_result(
            competitor_name="A", event_code="SB", time_seconds=50.0,
            species="poplar", diameter_mm=300, quality=5,
        )
        assert tmp_store.count() == 1

    def test_get_competitors(self, tmp_store):
        tmp_store.record_result(
            competitor_name="Bob", event_code="SB", time_seconds=50.0,
            species="poplar", diameter_mm=300, quality=5,
        )
        tmp_store.record_result(
            competitor_name="Alice", event_code="SB", time_seconds=45.0,
            species="poplar", diameter_mm=300, quality=5,
        )
        names = tmp_store.get_competitors()
        assert "Alice" in names
        assert "Bob" in names

    def test_filter_by_event_code(self, tmp_store):
        tmp_store.record_result(
            competitor_name="A", event_code="SB", time_seconds=50.0,
            species="poplar", diameter_mm=300, quality=5,
        )
        tmp_store.record_result(
            competitor_name="A", event_code="UH", time_seconds=60.0,
            species="poplar", diameter_mm=300, quality=5,
        )
        sb_only = tmp_store.get_competitor_history("A", event_code="SB")
        assert len(sb_only) == 1
        assert sb_only[0].event_code == "SB"

    def test_get_all_as_dataframe(self, tmp_store):
        tmp_store.record_result(
            competitor_name="A", event_code="SB", time_seconds=50.0,
            species="poplar", diameter_mm=300, quality=5,
        )
        df = tmp_store.get_all_as_dataframe()
        assert len(df) == 1

    def test_empty_store_returns_empty(self, tmp_store):
        history = tmp_store.get_competitor_history("Nobody")
        assert history == []
        assert tmp_store.get_competitors() == []
        df = tmp_store.get_all_as_dataframe()
        assert len(df) == 0
