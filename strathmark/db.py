"""
Database layer for STRATHMARK — Supabase/PostgreSQL backend.

SQL Schema
----------
All tables live in the Supabase project referenced by STRATHMARK_SUPABASE_URL.

    competitors:
        competitor_id  TEXT PRIMARY KEY,
        name           TEXT,
        country        TEXT,
        state_province TEXT,
        gender         TEXT,
        region         TEXT,
        created_at     TIMESTAMPTZ DEFAULT now()

    results:
        result_id      SERIAL PRIMARY KEY,
        competitor_id  TEXT REFERENCES competitors(competitor_id),
        event          TEXT,
        time_seconds   NUMERIC,
        size_mm        NUMERIC,
        species_code   TEXT,
        result_date    DATE,
        show_name      TEXT,
        source_app     TEXT,
        notes          TEXT,
        created_at     TIMESTAMPTZ DEFAULT now()

    wood_species:
        species_id     TEXT PRIMARY KEY,
        scientific_name TEXT,
        common_name    TEXT,
        janka_hard     NUMERIC,
        spec_gravity   NUMERIC,
        crush_strength NUMERIC,
        shear          NUMERIC,
        mor            NUMERIC,
        moe            NUMERIC,
        country        TEXT,
        region         TEXT

    sync_log:
        sync_id         SERIAL PRIMARY KEY,
        show_name       TEXT,
        source_app      TEXT,
        records_written INTEGER,
        synced_at       TIMESTAMPTZ DEFAULT now()

    prediction_residuals:
        id              SERIAL PRIMARY KEY,
        competitor_id   TEXT,
        predicted_time  NUMERIC,
        actual_time     NUMERIC,
        residual        NUMERIC,
        show_name       TEXT,
        event_code      TEXT,
        result_date     DATE,
        created_at      TIMESTAMPTZ DEFAULT now()

    -- residual = actual_time - predicted_time
    -- Positive: competitor was slower than predicted (undermarked)
    -- Negative: competitor was faster than predicted (overmarked)

    -- CREATE TABLE prediction_residuals (
    --     id             SERIAL PRIMARY KEY,
    --     competitor_id  TEXT,
    --     predicted_time NUMERIC,
    --     actual_time    NUMERIC,
    --     residual       NUMERIC,
    --     show_name      TEXT,
    --     event_code     TEXT,
    --     result_date    DATE,
    --     created_at     TIMESTAMPTZ DEFAULT now()
    -- );

Environment variables required:
    STRATHMARK_SUPABASE_URL  — Supabase project URL
    STRATHMARK_SUPABASE_KEY  — Supabase anon/service key
"""

from __future__ import annotations

import logging
import math
import os
import statistics
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Internal: date serialisation helper
# ---------------------------------------------------------------------------


def _safe_date(val) -> str | None:
    """
    Convert a date/datetime/NaT/NaN value to an ISO-8601 string or None.

    pandas NaT is not JSON-serializable and Supabase rejects it with
    "invalid input syntax for type date: NaT". This helper normalises
    every edge case to either a YYYY-MM-DD string or Python None.
    """
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val) if val else None


_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal: lazy client singleton
# ---------------------------------------------------------------------------

_client = None  # supabase.Client, created on first use


def _get_client():
    """
    Return (and cache) the Supabase client.

    Raises:
        RuntimeError: If STRATHMARK_SUPABASE_URL or STRATHMARK_SUPABASE_KEY
                      is not set as a User environment variable.
    """
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("STRATHMARK_SUPABASE_URL")
    key = os.environ.get("STRATHMARK_SUPABASE_KEY")

    if not url:
        raise RuntimeError(
            "STRATHMARK_SUPABASE_URL is not set. "
            "Set it as a User environment variable before using the database layer."
        )
    if not key:
        raise RuntimeError(
            "STRATHMARK_SUPABASE_KEY is not set. "
            "Set it as a User environment variable before using the database layer."
        )

    from supabase import create_client  # type: ignore[import]

    _client = create_client(url, key)
    return _client


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def push_results(
    results_df: pd.DataFrame,
    show_name: str,
    source_app: str,
) -> int:
    """
    Insert rows from a results DataFrame into the global results table.

    Column mapping (DataFrame -> DB):
        CompetitorID                                    -> competitor_id
        Event                                           -> event
        Time (seconds)                                  -> time_seconds
        Size (mm)                                       -> size_mm
        Species Code                                    -> species_code
        Date (optional)                                 -> result_date
        Notes (Competition, special circumstances, etc.)-> notes

    show_name and source_app are added to every row before insert.

    Duplicate rows are skipped silently. A duplicate is defined as matching
    all of: competitor_id, event, time_seconds, size_mm, result_date.

    Calls log_sync() after a successful push.

    Args:
        results_df: DataFrame with result rows (local Excel column names).
        show_name:  Name of the show/competition this data comes from.
        source_app: Name of the calling application (e.g. 'STRATHEX').

    Returns:
        Count of rows actually inserted (duplicates excluded).
    """
    client = _get_client()

    if results_df.empty:
        return 0

    col_map = {
        "CompetitorID": "competitor_id",
        "Event": "event",
        "Time (seconds)": "time_seconds",
        "Size (mm)": "size_mm",
        "Species Code": "species_code",
        "Date (optional)": "result_date",
        "Notes (Competition, special circumstances, etc.)": "notes",
        "field_strength": "field_strength",
    }

    # Rename columns that exist in the DataFrame
    rename = {k: v for k, v in col_map.items() if k in results_df.columns}
    df = results_df.rename(columns=rename).copy()

    # Keep only the columns we want to insert
    db_cols = list(col_map.values()) + ["show_name", "source_app"]
    df["show_name"] = show_name
    df["source_app"] = source_app

    # Retain only known DB columns that are actually present
    df = df[[c for c in db_cols if c in df.columns]]

    # Convert result_date to ISO string (YYYY-MM-DD) or None.
    # _safe_date handles NaT, NaN, float('nan'), None, and real date/datetime.
    if "result_date" in df.columns:
        df["result_date"] = df["result_date"].apply(_safe_date)

    # Convert NaN -> None for JSON serialisation
    records = [
        {k: (None if pd.isna(v) else v) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]

    # Fetch existing rows to detect duplicates
    dedup_cols = ["competitor_id", "event", "time_seconds", "size_mm", "result_date"]
    existing_response = client.table("results").select(", ".join(dedup_cols)).execute()
    existing_rows = existing_response.data or []
    existing_keys = {
        (
            str(r.get("competitor_id", "")),
            str(r.get("event", "")),
            str(r.get("time_seconds", "")),
            str(r.get("size_mm", "")),
            str(r.get("result_date", "")),
        )
        for r in existing_rows
    }

    new_records = []
    for rec in records:
        key = (
            str(rec.get("competitor_id", "")),
            str(rec.get("event", "")),
            str(rec.get("time_seconds", "")),
            str(rec.get("size_mm", "")),
            str(rec.get("result_date", "")),
        )
        if key not in existing_keys:
            new_records.append(rec)

    if not new_records:
        return 0

    client.table("results").insert(new_records).execute()

    inserted = len(new_records)
    log_sync(show_name, source_app, inserted)
    return inserted


def pull_results(
    competitor_ids: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    Fetch all results from the global database.

    Args:
        competitor_ids: Optional list of competitor IDs to filter by.
                        If None, all results are returned.

    Returns:
        DataFrame with columns:
            competitor_id, Event, Time (seconds), Size (mm), Species Code,
            Date (optional), Notes (Competition, special circumstances, etc.),
            show_name, source_app
        Ordered by competitor_id, result_date ascending.
        Returns an empty DataFrame (never None) if no results are found.
    """
    client = _get_client()

    query = client.table("results").select("*").order("competitor_id").order("result_date")

    if competitor_ids:
        query = query.in_("competitor_id", competitor_ids)

    response = query.execute()
    rows = response.data or []

    if not rows:
        return pd.DataFrame(
            columns=[
                "competitor_id",
                "Event",
                "Time (seconds)",
                "Size (mm)",
                "Species Code",
                "Date (optional)",
                "Notes (Competition, special circumstances, etc.)",
                "show_name",
                "source_app",
                "field_strength",
            ]
        )

    df = pd.DataFrame(rows)

    # Rename DB columns back to local Excel names
    rename = {
        "event": "Event",
        "time_seconds": "Time (seconds)",
        "size_mm": "Size (mm)",
        "species_code": "Species Code",
        "result_date": "Date (optional)",
        "notes": "Notes (Competition, special circumstances, etc.)",
        # field_strength: same name in DB and output -- no rename needed
    }
    df = df.rename(columns=rename)

    # Ensure expected output columns exist
    output_cols = [
        "competitor_id",
        "Event",
        "Time (seconds)",
        "Size (mm)",
        "Species Code",
        "Date (optional)",
        "Notes (Competition, special circumstances, etc.)",
        "show_name",
        "source_app",
        "field_strength",
    ]
    for col in output_cols:
        if col not in df.columns:
            df[col] = None

    return df[output_cols]


def push_competitors(competitor_df: pd.DataFrame) -> int:
    """
    Upsert competitor records on competitor_id.

    Column mapping (DataFrame -> DB):
        CompetitorID   -> competitor_id
        Name           -> name
        Country        -> country
        State/Province -> state_province
        Gender         -> gender
        Region         -> region

    Args:
        competitor_df: DataFrame with competitor rows (local Excel column names).

    Returns:
        Count of rows upserted.
    """
    client = _get_client()

    if competitor_df.empty:
        return 0

    col_map = {
        "CompetitorID": "competitor_id",
        "Name": "name",
        "Country": "country",
        "State/Province": "state_province",
        "Gender": "gender",
        "Region": "region",
    }

    rename = {k: v for k, v in col_map.items() if k in competitor_df.columns}
    df = competitor_df.rename(columns=rename).copy()

    db_cols = list(col_map.values())
    df = df[[c for c in db_cols if c in df.columns]]

    records = [
        {k: (None if pd.isna(v) else v) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]

    if not records:
        return 0

    client.table("competitors").upsert(records, on_conflict="competitor_id").execute()
    return len(records)


def pull_competitors() -> pd.DataFrame:
    """
    Fetch all competitor records from the global database.

    Returns:
        DataFrame with columns matching local Excel format:
            CompetitorID, Name, Country, State/Province, Gender, Region
        Returns an empty DataFrame (never None) if no records are found.
    """
    client = _get_client()

    response = client.table("competitors").select("*").execute()
    rows = response.data or []

    if not rows:
        return pd.DataFrame(
            columns=[
                "CompetitorID",
                "Name",
                "Country",
                "State/Province",
                "Gender",
                "Region",
            ]
        )

    df = pd.DataFrame(rows)

    rename = {
        "competitor_id": "CompetitorID",
        "name": "Name",
        "country": "Country",
        "state_province": "State/Province",
        "gender": "Gender",
        "region": "Region",
    }
    df = df.rename(columns=rename)

    output_cols = ["CompetitorID", "Name", "Country", "State/Province", "Gender", "Region"]
    for col in output_cols:
        if col not in df.columns:
            df[col] = None

    return df[output_cols]


def record_prediction_residuals(
    predicted: Dict[str, float],
    actual: Dict[str, float],
    show_name: str,
    event_code: str,
    result_date: date,
) -> None:
    """
    Insert per-competitor prediction residuals into the prediction_residuals table.

    Only inserts a row for competitors that appear in BOTH predicted and actual.

    residual = actual_time - predicted_time
    Positive: competitor was slower than predicted (undermarked).
    Negative: competitor was faster than predicted (overmarked).

    Args:
        predicted:   {competitor_id -> predicted_time_seconds}
        actual:      {competitor_id -> actual_time_seconds}
        show_name:   Name of the show/competition.
        event_code:  'SB' or 'UH'.
        result_date: Date of the event.

    Never raises on failure -- logs a warning so callers are not interrupted.
    """
    try:
        client = _get_client()
        date_str = _safe_date(result_date)

        rows = []
        for comp_id, pred_time in predicted.items():
            if comp_id not in actual:
                continue
            act_time = actual[comp_id]
            residual = act_time - pred_time
            rows.append(
                {
                    "competitor_id": comp_id,
                    "predicted_time": round(float(pred_time), 3),
                    "actual_time": round(float(act_time), 3),
                    "residual": round(float(residual), 3),
                    "show_name": show_name,
                    "event_code": str(event_code).strip().upper(),
                    "result_date": date_str,
                }
            )

        if rows:
            client.table("prediction_residuals").insert(rows).execute()

    except Exception as exc:  # pragma: no cover
        _log.warning("record_prediction_residuals failed (non-fatal): %s", exc)


def get_competitor_bias(competitor_id: str) -> Optional[float]:
    """
    Return the median signed prediction residual for a competitor.

    Fetches all rows in prediction_residuals for this competitor and returns
    the median residual (actual_time - predicted_time). A positive bias means
    the competitor consistently runs slower than predicted; subtracting the
    bias from the prediction corrects for systematic undermarking.

    Returns None when fewer than 3 residuals exist (not enough data to
    establish a reliable bias estimate).

    Args:
        competitor_id: Competitor ID matching the prediction_residuals table.

    Returns:
        Median residual in seconds, or None if fewer than 3 rows exist.
        Never raises -- returns None on any DB error.
    """
    try:
        client = _get_client()
        response = (
            client.table("prediction_residuals")
            .select("residual")
            .eq("competitor_id", competitor_id)
            .execute()
        )
        rows = response.data or []

        if len(rows) < 3:
            return None

        residuals = [float(r["residual"]) for r in rows if r.get("residual") is not None]
        if len(residuals) < 3:
            return None

        return statistics.median(residuals)

    except Exception as exc:  # pragma: no cover
        _log.debug("get_competitor_bias failed (non-fatal): %s", exc)
        return None


def log_sync(show_name: str, source_app: str, records_written: int) -> None:
    """
    Insert one row into sync_log.

    Never raises on failure — logs a warning instead so that a sync-log
    failure never interrupts the calling push operation.

    Args:
        show_name:       Name of the show/competition.
        source_app:      Name of the calling application.
        records_written: Number of records written in this sync.
    """
    try:
        client = _get_client()
        client.table("sync_log").insert(
            {
                "show_name": show_name,
                "source_app": source_app,
                "records_written": records_written,
            }
        ).execute()
    except Exception as exc:  # pragma: no cover
        _log.warning("log_sync failed (non-fatal): %s", exc)
