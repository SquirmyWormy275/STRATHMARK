"""Extended tests for the SQLite result store.

Covers concurrent writes, import edge cases, date handling,
duplicate detection, and DataFrame round-trips.
"""

import sqlite3
from datetime import date

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
    store.record_result(
        "Alice", "SB", 25.0, "S01", 300, 5, heat_id="H1", result_date=date(2025, 1, 15)
    )
    store.record_result(
        "Alice", "SB", 26.5, "S01", 300, 5, heat_id="H2", result_date=date(2025, 2, 10)
    )
    store.record_result(
        "Alice", "UH", 30.0, "S01", 300, 5, heat_id="H3", result_date=date(2025, 3, 5)
    )
    store.record_result(
        "Bob", "SB", 35.0, "S03", 350, 7, heat_id="H1", result_date=date(2025, 1, 15)
    )
    store.record_result(
        "Charlie", "SB", 40.0, "S05", 300, 3, heat_id="H1", result_date=date(2025, 1, 15)
    )
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

    def test_same_heat_in_different_competitions_is_not_duplicate(self, store):
        first = store.record_result(
            "A",
            "SB",
            25.0,
            "S01",
            300,
            5,
            heat_id="H1",
            competition_id="show-2025",
        )
        second = store.record_result(
            "A",
            "SB",
            25.0,
            "S01",
            300,
            5,
            heat_id="H1",
            competition_id="show-2026",
        )

        assert first is True
        assert second is True
        assert store.count() == 2

    @pytest.mark.parametrize(
        ("event_code", "time_seconds", "diameter_mm", "quality"),
        [
            ("INVALID", 25.0, 300, 5),
            ("SB", 181.0, 300, 5),
            ("SB", 25.0, 200, 5),
            ("SB", 25.0, 300, 11),
        ],
    )
    def test_rejects_invalid_result_data(
        self, store, event_code, time_seconds, diameter_mm, quality
    ):
        with pytest.raises(ValueError):
            store.record_result("A", event_code, time_seconds, "S01", diameter_mm, quality)


class TestSchemaMigration:
    def test_preserves_legacy_rows_and_adds_competition_identity(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    competitor_name TEXT NOT NULL,
                    event_code TEXT NOT NULL,
                    time_seconds REAL NOT NULL,
                    species TEXT NOT NULL,
                    diameter_mm REAL NOT NULL,
                    quality INTEGER NOT NULL,
                    heat_id TEXT NOT NULL DEFAULT '',
                    result_date TEXT,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(competitor_name, heat_id, event_code, time_seconds)
                );
                CREATE INDEX idx_results_competitor ON results(competitor_name, event_code);
                INSERT INTO results (
                    competitor_name, event_code, time_seconds, species,
                    diameter_mm, quality, heat_id, result_date, recorded_at
                ) VALUES ('A', 'SB', 25.0, 'S01', 300, 5, 'H1', '2025-06-01', '2025-06-01T00:00:00+00:00');
                """
            )

        store = ResultStore(db_path=db_path)

        assert store.count() == 1
        with sqlite3.connect(db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
            competition_id = conn.execute(
                "SELECT competition_id FROM results WHERE competitor_name = 'A'"
            ).fetchone()[0]
        assert "competition_id" in columns
        assert competition_id == "legacy:2025-06-01"


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
        assert "competition_id" in df.columns

    def test_round_trip_preserves_competition_identity(self, tmp_path):
        source = ResultStore(db_path=tmp_path / "source.db")
        source.record_result(
            "Alice",
            "SB",
            25.0,
            "S01",
            300,
            5,
            heat_id="H1",
            result_date=date(2025, 1, 15),
            competition_id="show-2025",
        )

        target = ResultStore(db_path=tmp_path / "target.db")
        assert target.import_from_dataframe(source.get_all_as_dataframe()) == 1
        assert target.get_all_as_dataframe().loc[0, "competition_id"] == "show-2025"


class TestImportFromDataFrame:
    def test_basic_import(self, store):
        df = pd.DataFrame(
            {
                "competitor_name": ["X", "Y"],
                "event_code": ["SB", "UH"],
                "time_seconds": [20.0, 30.0],
                "species": ["S01", "S01"],
                "diameter_mm": [300, 300],
                "quality": [5, 5],
            }
        )
        count = store.import_from_dataframe(df)
        assert count == 2
        assert store.count() == 2

    def test_skip_duplicates(self, store):
        df = pd.DataFrame(
            {
                "competitor_name": ["X", "X"],
                "event_code": ["SB", "SB"],
                "time_seconds": [20.0, 20.0],
                "species": ["S01", "S01"],
                "diameter_mm": [300, 300],
                "quality": [5, 5],
                "heat_id": ["H1", "H1"],
            }
        )
        count = store.import_from_dataframe(df, skip_duplicates=True)
        assert store.count() == 1

    def test_missing_optional_columns(self, store):
        """Import should work without heat_id and result_date columns."""
        df = pd.DataFrame(
            {
                "competitor_name": ["Z"],
                "event_code": ["SB"],
                "time_seconds": [25.0],
                "species": ["S01"],
                "diameter_mm": [300],
                "quality": [5],
            }
        )
        count = store.import_from_dataframe(df)
        assert count == 1

    def test_column_aliases(self, store):
        """Import should accept common column name aliases."""
        df = pd.DataFrame(
            {
                "competitor_name": ["W"],
                "event": ["SB"],  # alias for event_code
                "raw_time": [22.0],  # alias for time_seconds
                "species": ["S01"],
                "size_mm": [300],  # alias for diameter_mm
                "quality": [5],
            }
        )
        count = store.import_from_dataframe(df)
        assert count == 1

    def test_missing_competition_id_uses_the_legacy_date_key(self, store):
        df = pd.DataFrame(
            {
                "competitor_name": ["W"],
                "event_code": ["SB"],
                "time_seconds": [22.0],
                "species": ["S01"],
                "diameter_mm": [300],
                "quality": [5],
                "competition_id": [pd.NA],
                "result_date": ["2025-01-15"],
            }
        )

        assert store.import_from_dataframe(df) == 1
        assert store.get_all_as_dataframe().loc[0, "competition_id"] == "legacy:2025-01-15"


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
        assert store.record_result("A", "SB", 25.0, "S01", 300, 5, result_date=None) is True

    def test_round_trip_preserves_date(self, store):
        d = date(2025, 6, 15)
        store.record_result("A", "SB", 25.0, "S01", 300, 5, result_date=d)
        history = store.get_competitor_history("A")
        assert history[0].result_date == d
