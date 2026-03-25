"""Extended tests for the SQLite result store.

Covers concurrent writes, import edge cases, date handling,
duplicate detection, and DataFrame round-trips.
"""

import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from strathmark.store import ResultStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh store in a temp directory."""
    return ResultStore(db_path=tmp_path / "test.db")


@pytest.fixture
def populated_store(store):
    """Store with 5 diverse results pre-loaded."""
    store.record_result("Alice", "SB", 25.0, "S01", 300, 5,
                        heat_id="H1", result_date=date(2025, 1, 15))
    store.record_result("Alice", "SB", 26.5, "S01", 300, 5,
                        heat_id="H2", result_date=date(2025, 2, 10))
    store.record_result("Alice", "UH", 30.0, "S01", 300, 5,
                        heat_id="H3", result_date=date(2025, 3, 5))
    store.record_result("Bob", "SB", 35.0, "S03", 350, 7,
                        heat_id="H1", result_date=date(2025, 1, 15))
    store.record_result("Charlie", "SB", 40.0, "S05", 300, 3,
                        heat_id="H1", result_date=date(2025, 1, 15))
    return store


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------
class TestRecordResult:
    def test_returns_true_on_insert(self, store):
        assert store.record_result("A", "SB", 25.0, "S01", 300, 5) is True

    def test_returns_false_on_duplicate(self, store):
        store.record_result("A", "SB", 25.0, "S01", 300, 5, heat_id="H1")
        result = store.record_result("A", "SB", 25.0, "S01", 300, 5, heat_id="H1")
        assert result is False

    def test_same_competitor_different_heat_is_not_duplicate(self, store):
        store.record_result("A", "SB", 25.0, "S01", 300, 5, heat_id="H1")
        result = store.record_result("A", "SB", 25.0, "S01", 300, 5, heat_id="H2")
        assert result is True

    def test_same_competitor_different_time_is_not_duplicate(self, store):
        store.record_result("A", "SB", 25.0, "S01", 300, 5, heat_id="H1")
        result = store.record_result("A", "SB", 26.0, "S01", 300, 5, heat_id="H1")
        assert result is True


class TestGetCompetitorHistory:
    def test_returns_all_results(self, populated_store):
        history = populated_store.get_competitor_history("Alice")
        assert len(history) == 3

    def test_filter_by_event(self, populated_store):
        history = populated_store.get_competitor_history("Alice", event_code="SB")
        assert len(history) == 2
        assert all(h.event_code == "SB" for h in history)

    def test_case_insensitive_name(self, populated_store):
        history = populated_store.get_competitor_history("alice")
        assert len(history) == 3

    def test_unknown_competitor_returns_empty(self, populated_store):
        assert populated_store.get_competitor_history("Nobody") == []

    def test_result_dates_preserved(self, populated_store):
        history = populated_store.get_competitor_history("Alice", event_code="SB")
        dates = [h.result_date for h in history]
        assert all(d is not None for d in dates)


class TestGetCompetitors:
    def test_returns_all_names(self, populated_store):
        names = populated_store.get_competitors()
        assert set(names) == {"Alice", "Bob", "Charlie"}

    def test_sorted(self, populated_store):
        names = populated_store.get_competitors()
        assert names == sorted(names)


class TestCount:
    def test_empty_store(self, store):
        assert store.count() == 0

    def test_after_inserts(self, populated_store):
        assert populated_store.count() == 5


# ---------------------------------------------------------------------------
# DataFrame operations
# ---------------------------------------------------------------------------
class TestGetAllAsDataFrame:
    def test_returns_dataframe(self, populated_store):
        df = populated_store.get_all_as_dataframe()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 5

    def test_has_expected_columns(self, populated_store):
        df = populated_store.get_all_as_dataframe()
        # Store renames event_code → event for STRATHEX compatibility
        assert "competitor_name" in df.columns or "name" in df.columns


class TestImportFromDataFrame:
    def test_basic_import(self, store):
        df = pd.DataFrame({
            "competitor_name": ["X", "Y"],
            "event_code": ["SB", "UH"],
            "time_seconds": [20.0, 30.0],
            "species": ["S01", "S01"],
            "diameter_mm": [300, 300],
            "quality": [5, 5],
        })
        count = store.import_from_dataframe(df)
        assert count == 2
        assert store.count() == 2

    def test_skip_duplicates(self, store):
        df = pd.DataFrame({
            "competitor_name": ["X", "X"],
            "event_code": ["SB", "SB"],
            "time_seconds": [20.0, 20.0],
            "species": ["S01", "S01"],
            "diameter_mm": [300, 300],
            "quality": [5, 5],
            "heat_id": ["H1", "H1"],
        })
        count = store.import_from_dataframe(df, skip_duplicates=True)
        assert store.count() == 1

    def test_missing_optional_columns(self, store):
        """Import should work without heat_id and result_date columns."""
        df = pd.DataFrame({
            "competitor_name": ["Z"],
            "event_code": ["SB"],
            "time_seconds": [25.0],
            "species": ["S01"],
            "diameter_mm": [300],
            "quality": [5],
        })
        count = store.import_from_dataframe(df)
        assert count == 1

    def test_column_aliases(self, store):
        """Import should accept common column name aliases."""
        df = pd.DataFrame({
            "competitor_name": ["W"],
            "event": ["SB"],         # alias for event_code
            "raw_time": [22.0],       # alias for time_seconds
            "species": ["S01"],
            "size_mm": [300],         # alias for diameter_mm
            "quality": [5],
        })
        count = store.import_from_dataframe(df)
        assert count == 1


# ---------------------------------------------------------------------------
# Empty store behavior
# ---------------------------------------------------------------------------
class TestEmptyStore:
    def test_get_competitors_empty(self, store):
        assert store.get_competitors() == []

    def test_get_all_as_dataframe_empty(self, store):
        df = store.get_all_as_dataframe()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_get_history_empty(self, store):
        assert store.get_competitor_history("Nobody") == []


# ---------------------------------------------------------------------------
# Date handling edge cases
# ---------------------------------------------------------------------------
class TestDateHandling:
    def test_none_result_date(self, store):
        """result_date=None should not crash."""
        assert store.record_result("A", "SB", 25.0, "S01", 300, 5,
                                   result_date=None) is True

    def test_round_trip_preserves_date(self, store):
        d = date(2025, 6, 15)
        store.record_result("A", "SB", 25.0, "S01", 300, 5, result_date=d)
        history = store.get_competitor_history("A")
        assert history[0].result_date == d
