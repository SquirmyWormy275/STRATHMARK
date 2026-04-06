"""
Result Store
=============

SQLite-backed persistence layer for tournament results.

Results accumulate across competitions so predictions grow more accurate
over time. Multiple projects (STRATHEX, future tournament software) can
share the same store by pointing at the same database path.

Default path: ~/.strathmark/results.db
Override:     Set the STRATHMARK_DB_PATH environment variable.

Public API:
    ResultStore(db_path=None)                       -- open/create the store
    .record_result(...)                             -- append one result
    .import_from_dataframe(df, skip_duplicates=True)-- bulk import from DataFrame
    .get_competitor_history(name, event_code=None)  -- List[HistoricalResult]
    .get_all_as_dataframe()                         -- full table as DataFrame
    .get_competitors()                              -- List[str] of all names
    .count()                                        -- total row count

Schema (table: results):
    id              INTEGER PRIMARY KEY AUTOINCREMENT
    competitor_name TEXT NOT NULL
    event_code      TEXT NOT NULL     ('SB' or 'UH')
    time_seconds    REAL NOT NULL
    species         TEXT NOT NULL
    diameter_mm     REAL NOT NULL
    quality         INTEGER NOT NULL
    heat_id         TEXT NOT NULL DEFAULT ''  (empty string, never NULL)
    result_date     TEXT              (ISO 8601 date, e.g. '2025-06-14', nullable)
    recorded_at     TEXT NOT NULL     (ISO 8601 datetime of when row was inserted)

Unique constraint: (competitor_name, heat_id, event_code, time_seconds)
Using empty string for heat_id (not NULL) makes the unique constraint reliable.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

from strathmark.predictor import HistoricalResult

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENV_VAR = "STRATHMARK_DB_PATH"
_DEFAULT_DB_DIR = Path.home() / ".strathmark"
_DEFAULT_DB_NAME = "results.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_name TEXT NOT NULL,
    event_code      TEXT NOT NULL,
    time_seconds    REAL NOT NULL,
    species         TEXT NOT NULL,
    diameter_mm     REAL NOT NULL,
    quality         INTEGER NOT NULL,
    heat_id         TEXT NOT NULL DEFAULT '',
    result_date     TEXT,
    recorded_at     TEXT NOT NULL,
    UNIQUE(competitor_name, heat_id, event_code, time_seconds)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_results_competitor
    ON results(competitor_name, event_code);
"""


# ---------------------------------------------------------------------------
# ResultStore
# ---------------------------------------------------------------------------


class ResultStore:
    """
    Persistent store for woodchopping tournament results.

    Thread-safety: uses ``check_same_thread=False`` so a single instance can be
    shared across threads, but each operation acquires/releases the connection
    internally via context managers.

    Args:
        db_path: Explicit path to the SQLite file. If None, reads the
                 STRATHMARK_DB_PATH environment variable; falls back to
                 ~/.strathmark/results.db.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is not None:
            self._path = Path(db_path)
        elif _ENV_VAR in os.environ:
            self._path = Path(os.environ[_ENV_VAR])
        else:
            self._path = _DEFAULT_DB_DIR / _DEFAULT_DB_NAME

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def record_result(
        self,
        competitor_name: str,
        event_code: str,
        time_seconds: float,
        species: str,
        diameter_mm: float,
        quality: int,
        heat_id: Optional[str] = None,
        result_date: Optional[date] = None,
    ) -> bool:
        """
        Append a single tournament result to the store.

        Duplicate results (same competitor_name + heat_id + event_code + time_seconds)
        are silently ignored via INSERT OR IGNORE.

        Args:
            competitor_name: Competitor display name.
            event_code: 'SB' or 'UH'.
            time_seconds: Raw cutting time in seconds.
            species: Wood species code/name.
            diameter_mm: Block diameter.
            quality: Wood quality (1-10).
            heat_id: Optional heat/round identifier (e.g. 'SB-225mmSB-Heat1').
            result_date: Date of competition. None if unknown.

        Returns:
            True if a new row was inserted, False if it was a duplicate.
        """
        _heat_id = heat_id if heat_id is not None else ""
        _result_date = result_date.isoformat() if result_date is not None else None
        _recorded_at = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO results
                    (competitor_name, event_code, time_seconds, species,
                     diameter_mm, quality, heat_id, result_date, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(competitor_name).strip(),
                    str(event_code).strip().upper(),
                    float(time_seconds),
                    str(species).strip(),
                    float(diameter_mm),
                    int(quality),
                    _heat_id,
                    _result_date,
                    _recorded_at,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def import_from_dataframe(
        self,
        df: pd.DataFrame,
        skip_duplicates: bool = True,
    ) -> int:
        """
        Bulk-import results from a DataFrame.

        Expects columns matching the STRATHEX results_df format:
            competitor_name, event (or event_code), raw_time (or time_seconds),
            species, size_mm (or diameter_mm), quality, heat_id, date (or result_date).

        Missing columns are tolerated (heat_id and date default to empty/'').

        Args:
            df: DataFrame of historical results.
            skip_duplicates: If True (default), existing rows are silently skipped.

        Returns:
            Number of rows actually inserted.
        """
        if df is None or df.empty:
            return 0

        # Normalize column names
        df = df.copy()
        df.columns = [str(c).strip().lower() for c in df.columns]
        col_map = {
            "event": "event_code",
            "raw_time": "time_seconds",
            "size_mm": "diameter_mm",
            "date": "result_date",
        }
        df.rename(columns=col_map, inplace=True)

        required = [
            "competitor_name",
            "event_code",
            "time_seconds",
            "species",
            "diameter_mm",
            "quality",
        ]
        for col in required:
            if col not in df.columns:
                return 0

        # Fill optional columns
        if "heat_id" not in df.columns:
            df["heat_id"] = ""
        else:
            df["heat_id"] = df["heat_id"].fillna("").astype(str)
        if "result_date" not in df.columns:
            df["result_date"] = None

        _recorded_at = datetime.now(timezone.utc).isoformat()
        insert_sql = (
            (
                "INSERT OR IGNORE INTO results "
                "(competitor_name, event_code, time_seconds, species, "
                "diameter_mm, quality, heat_id, result_date, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            if skip_duplicates
            else (
                "INSERT INTO results "
                "(competitor_name, event_code, time_seconds, species, "
                "diameter_mm, quality, heat_id, result_date, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
        )

        inserted = 0
        with self._connect() as conn:
            for _, row in df.iterrows():
                try:
                    time_val = float(row["time_seconds"])
                    if pd.isna(time_val):
                        continue
                    quality_val = int(row["quality"]) if not pd.isna(row["quality"]) else 5
                    diameter_val = (
                        float(row["diameter_mm"]) if not pd.isna(row["diameter_mm"]) else 300.0
                    )

                    # Parse result_date
                    rd = row.get("result_date")
                    if (
                        pd.isna(rd)
                        if hasattr(rd, "__class__") and rd.__class__.__name__ in ("float", "NaT")
                        else False
                    ):
                        rd = None
                    if rd is not None:
                        try:
                            if hasattr(rd, "isoformat"):
                                rd = rd.date().isoformat() if hasattr(rd, "date") else str(rd)[:10]
                            else:
                                rd = str(rd)[:10]
                        except Exception:
                            rd = None

                    heat_id = str(row.get("heat_id", "") or "")

                    cursor = conn.execute(
                        insert_sql,
                        (
                            str(row["competitor_name"]).strip(),
                            str(row["event_code"]).strip().upper(),
                            time_val,
                            str(row["species"]).strip(),
                            diameter_val,
                            quality_val,
                            heat_id,
                            rd,
                            _recorded_at,
                        ),
                    )
                    inserted += cursor.rowcount
                except Exception:
                    continue
            conn.commit()

        return inserted

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_competitor_history(
        self,
        competitor_name: str,
        event_code: Optional[str] = None,
    ) -> List[HistoricalResult]:
        """
        Return all stored results for a competitor as HistoricalResult objects.

        Args:
            competitor_name: Exact competitor name (case-insensitive match).
            event_code: Optional filter ('SB' or 'UH'). None returns all events.

        Returns:
            List of HistoricalResult, ordered oldest-first.
        """
        params: list = [competitor_name.strip().lower()]
        sql = (
            "SELECT event_code, time_seconds, species, diameter_mm, quality, "
            "result_date, heat_id "
            "FROM results "
            "WHERE LOWER(TRIM(competitor_name)) = ? "
        )
        if event_code is not None:
            sql += "AND event_code = ? "
            params.append(event_code.strip().upper())
        sql += "ORDER BY result_date ASC NULLS LAST, recorded_at ASC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            rd = None
            if row["result_date"]:
                try:
                    rd = date.fromisoformat(row["result_date"])
                except (ValueError, TypeError):
                    rd = None
            results.append(
                HistoricalResult(
                    event_code=row["event_code"],
                    time_seconds=float(row["time_seconds"]),
                    species=row["species"],
                    diameter_mm=float(row["diameter_mm"]),
                    quality=int(row["quality"]),
                    result_date=rd,
                    heat_id=row["heat_id"] or None,
                )
            )
        return results

    def get_all_as_dataframe(self) -> pd.DataFrame:
        """
        Return the full results table as a pandas DataFrame.

        Column names match the STRATHEX results_df format:
            competitor_name, event_code, raw_time, species, size_mm, quality,
            heat_id, result_date, recorded_at.
        """
        with self._connect() as conn:
            df = pd.read_sql_query(
                "SELECT competitor_name, event_code, time_seconds AS raw_time, "
                "species, diameter_mm AS size_mm, quality, heat_id, "
                "result_date, recorded_at FROM results "
                "ORDER BY result_date ASC, recorded_at ASC",
                conn,
            )
        # Normalize event column alias for STRATHEX compatibility
        if "event_code" in df.columns:
            df = df.rename(columns={"event_code": "event"})
        return df

    def get_competitors(self) -> List[str]:
        """Return a sorted list of all distinct competitor names in the store."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT competitor_name FROM results ORDER BY competitor_name"
            ).fetchall()
        return [r["competitor_name"] for r in rows]

    def count(self) -> int:
        """Return the total number of result rows in the store."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM results").fetchone()
        return int(row["n"])

    def __repr__(self) -> str:
        return f"ResultStore(path={self._path!r}, rows={self.count()})"
