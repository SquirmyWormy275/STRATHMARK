# Schema verified against live Supabase 2026-05-04. Source of truth.
# Verification artifact: docs/schema-reality-2026-05-04.md
# Verification method: PostgREST OpenAPI spec + sampled rows + row counts.
# pg_catalog (indexes, RLS, triggers) NOT verified in this pass; see report.

"""
Database layer for STRATHMARK — Supabase/PostgreSQL backend.

SQL Schema (verified 2026-05-04 against project iordtvxryrdhqvdkfgzf)
---------------------------------------------------------------------
All tables live in the Supabase project referenced by STRATHMARK_SUPABASE_URL.
Column-level metadata reflects the live schema. Required columns are marked
NOT NULL; default expressions are noted. Types use the actual Postgres types,
not the previous docstring's approximations.

    competitors:
        competitor_id  TEXT  PRIMARY KEY  NOT NULL,
        name           TEXT  NOT NULL,
        country        TEXT,
        state_province TEXT,
        gender         TEXT,
        region         TEXT,
        created_at     TIMESTAMPTZ DEFAULT now()

        # Observed competitor_id format: 'C001'..'C085' (3-digit zero-padded).
        # NOTE: register_competitor() in this module mints '4-digit' IDs
        # (e.g. 'C0086'). That divergence is a known latent bug; rewrite is
        # scheduled for the MNEMEX-roster follow-on PR.

    results:
        result_id      INTEGER  PRIMARY KEY  NOT NULL,
        competitor_id  TEXT     REFERENCES competitors(competitor_id),
        event          TEXT     NOT NULL,        -- 'SB' or 'UH'
        time_seconds   NUMERIC  NOT NULL,
        size_mm        INTEGER  NOT NULL,        -- whole mm; not numeric
        species_code   TEXT     NOT NULL,
        result_date    DATE,
        show_name      TEXT     NOT NULL,
        source_app     TEXT,
        notes          TEXT,
        created_at     TIMESTAMPTZ DEFAULT now(),
        field_strength NUMERIC                   -- present in DB; was undocumented
                                                  -- prior to 2026-05-04. 100% null in
                                                  -- current data.

    wood_species:
        species_id      TEXT     PRIMARY KEY  NOT NULL,
        scientific_name TEXT,
        common_name     TEXT     NOT NULL,
        janka_hard      INTEGER,
        spec_gravity    NUMERIC,
        crush_strength  INTEGER,
        shear           INTEGER,
        mor             INTEGER,                  -- lowercase; was 'MOR' in old docstring
        moe             INTEGER,                  -- lowercase; was 'MOE' in old docstring
        country         TEXT,
        region          TEXT

    sync_log:
        sync_id         INTEGER  PRIMARY KEY  NOT NULL,
        show_name       TEXT     NOT NULL,
        source_app      TEXT,
        records_written INTEGER,
        synced_at       TIMESTAMPTZ DEFAULT now()

    prediction_residuals:
        residual_id      INTEGER  PRIMARY KEY  NOT NULL,   -- was 'id' in old docstring;
                                                            -- live PK is 'residual_id'.
        competitor_id    TEXT     REFERENCES competitors(competitor_id),
        predicted_time   NUMERIC  NOT NULL,
        actual_time      NUMERIC  NOT NULL,
        residual         NUMERIC  NOT NULL,
        show_name        TEXT     NOT NULL,
        event_code       TEXT     NOT NULL,
        result_date      DATE,
        created_at       TIMESTAMPTZ DEFAULT now(),
        -- Added by migration 20260504_002:
        model_version_id TEXT     REFERENCES model_versions(model_version_id),
        prediction_id    TEXT     REFERENCES predictions(prediction_id)

    -- residual = actual_time - predicted_time
    -- Positive: competitor was slower than predicted (undermarked)
    -- Negative: competitor was faster than predicted (overmarked)

ML state tables (added by migration 20260504_002):

    model_versions:
        model_version_id     TEXT         PRIMARY KEY,    -- ULID
        model_type           TEXT         NOT NULL,        -- e.g. 'xgboost_lightgbm_ensemble'
        trained_at           TIMESTAMPTZ  NOT NULL,
        training_data_cutoff TIMESTAMPTZ  NOT NULL,
        training_row_count   INTEGER      NOT NULL,
        hyperparameters      JSONB        NOT NULL,
        artifact_storage     TEXT         NOT NULL,        -- 'supabase_storage' | 'inline_jsonb'
        artifact_ref         TEXT         NOT NULL,
        artifact_size_bytes  INTEGER      NOT NULL,
        is_active            BOOLEAN      NOT NULL DEFAULT FALSE,
        retired_at           TIMESTAMPTZ,
        notes                TEXT
        -- Partial unique index: only one is_active=TRUE per model_type.

    calibration_tables:
        calibration_id     TEXT         PRIMARY KEY,                    -- ULID
        model_version_id   TEXT         NOT NULL REFERENCES model_versions,
        calibrated_at      TIMESTAMPTZ  NOT NULL,
        calibration_method TEXT         NOT NULL,
        calibration_data   JSONB        NOT NULL,
        holdout_residuals  JSONB        NOT NULL,
        crps_score         NUMERIC,
        coverage_at_90     NUMERIC,
        notes              TEXT

    feature_store:
        feature_set_id   TEXT         PRIMARY KEY,                    -- ULID
        model_version_id TEXT         NOT NULL REFERENCES model_versions,
        competitor_id    TEXT         NOT NULL REFERENCES competitors,
        event_code       TEXT         NOT NULL,
        features_jsonb   JSONB        NOT NULL,
        computed_at      TIMESTAMPTZ  NOT NULL,
        UNIQUE (model_version_id, competitor_id, event_code)

    predictions:
        prediction_id      TEXT         PRIMARY KEY,                  -- ULID
        model_version_id   TEXT         NOT NULL REFERENCES model_versions,
        competitor_id      TEXT         NOT NULL REFERENCES competitors,
        event_code         TEXT         NOT NULL,
        show_name          TEXT         NOT NULL,
        predicted_time     NUMERIC      NOT NULL,
        predicted_variance NUMERIC      NOT NULL,
        cascade_level_used TEXT         NOT NULL,
        predicted_at       TIMESTAMPTZ  NOT NULL,
        result_id          INTEGER      REFERENCES results,           -- set by settle_prediction()
        residual           NUMERIC,                                   -- set by settle_prediction()
        notes              TEXT

Environment variables required:
    STRATHMARK_SUPABASE_URL  — Supabase project URL
    STRATHMARK_SUPABASE_KEY  — Supabase service-role key (writes) or anon key (reads)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import statistics
from datetime import date
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

from strathmark.mirror_contract import (
    MIRROR_DELIVERY_SCHEMA_VERSION as _MIRROR_DELIVERY_SCHEMA_VERSION,
)
from strathmark.mirror_contract import (
    NUMERIC_OUTCOME_MIRROR_SCHEMA_VERSION as _NUMERIC_OUTCOME_MIRROR_SCHEMA_VERSION,
)
from strathmark.mirror_contract import (
    SHADOW_MIRROR_ENVELOPE_SCHEMA_VERSION as _SHADOW_MIRROR_ENVELOPE_SCHEMA_VERSION,
)
from strathmark.mirror_contract import (
    SHADOW_RECEIPT_MIRROR_SCHEMA_VERSION as _SHADOW_RECEIPT_MIRROR_SCHEMA_VERSION,
)

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


# Bound HTTP timeout on every PostgREST call. Hot-path bias correction
# would otherwise hang up to httpx's default (~30s) on a slow Supabase,
# which the bias circuit breaker can't observe (it counts exceptions, not
# hangs). 2 seconds is generous for a sub-second indexed lookup and short
# enough that a stalled call surfaces as an exception the breaker can act
# on. Operator action infrastructure (training, sync, ML state writes)
# tolerates a longer timeout, so callers that need it pass an override
# via reset_client(timeout=N).
_DEFAULT_POSTGREST_TIMEOUT: float = 2.0


def _get_client(timeout: float | None = None):
    """
    Return (and cache) the Supabase client with a bounded PostgREST timeout.

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

    effective_timeout = timeout if timeout is not None else _DEFAULT_POSTGREST_TIMEOUT
    try:
        from supabase.client import ClientOptions  # type: ignore[import]

        options = ClientOptions(postgrest_client_timeout=effective_timeout)
        _client = create_client(url, key, options=options)
    except (ImportError, TypeError):
        # Older supabase-py without ClientOptions: fall back to the
        # default-timeout client. The breaker still protects via failure
        # counting; only hangs slip through.
        _client = create_client(url, key)
    return _client


def reset_client(timeout: float | None = None) -> None:
    """Test hook / operator hook. Forget the cached client and optionally
    override the PostgREST timeout on the next instantiation.

    `timeout=None` uses the default. Pass a larger value (e.g. 30.0) before
    operator-action calls (training, sync, ML state writes) that legitimately
    take longer than the hot-path 2-second budget.
    """
    global _client
    _client = None
    if timeout is not None:
        # Stash the override so the next _get_client() picks it up. Simple
        # approach: callers that need a non-default timeout pass it via
        # _get_client(timeout=N) directly. We don't carry the value across
        # reset boundaries.
        _get_client(timeout=timeout)


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
    model_version_id: Optional[str] = None,
    prediction_ids: Optional[Dict[str, str]] = None,
) -> None:
    """
    Insert per-competitor prediction residuals into the prediction_residuals table.

    Only inserts a row for competitors that appear in BOTH predicted and actual.

    residual = actual_time - predicted_time
    Positive: competitor was slower than predicted (undermarked).
    Negative: competitor was faster than predicted (overmarked).

    Args:
        predicted:        {competitor_id -> predicted_time_seconds}
        actual:           {competitor_id -> actual_time_seconds}
        show_name:        Name of the show/competition.
        event_code:       'SB' or 'UH'.
        result_date:      Date of the event.
        model_version_id: Optional ULID of the model that produced these
                          predictions. Required by application convention going
                          forward; nullable at the DB layer to keep the function
                          backward-compatible while ML state wiring is rolled in.
        prediction_ids:   Optional {competitor_id -> prediction_id} from
                          record_prediction(). Lets the residual link back to
                          the originating prediction row.

    Never raises on failure -- logs a warning so callers are not interrupted.
    """
    try:
        client = _get_client()
        date_str = _safe_date(result_date)
        prediction_ids = prediction_ids or {}

        rows = []
        for comp_id, pred_time in predicted.items():
            if comp_id not in actual:
                continue
            act_time = actual[comp_id]
            residual = act_time - pred_time
            row = {
                "competitor_id": comp_id,
                "predicted_time": round(float(pred_time), 3),
                "actual_time": round(float(act_time), 3),
                "residual": round(float(residual), 3),
                "show_name": show_name,
                "event_code": str(event_code).strip().upper(),
                "result_date": date_str,
            }
            if model_version_id is not None:
                row["model_version_id"] = model_version_id
            pid = prediction_ids.get(comp_id)
            if pid is not None:
                row["prediction_id"] = pid
            rows.append(row)

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

    Raises:
        Any Supabase / network exception. Callers on the prediction hot path
        MUST wrap calls to this function in the bias-correction circuit
        breaker so transient failures degrade gracefully without disabling
        bias correction permanently. See `_BiasCircuitBreaker` in
        `strathmark/predictor.py` and `docs/ml-persistence-policy.md`
        section 5 for the policy.
    """
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


# ---------------------------------------------------------------------------
# Live ingestion helpers (dict-based, for tournament managers)
# ---------------------------------------------------------------------------


_REQUIRED_RESULT_FIELDS = (
    "competitor_id",
    "event_code",
    "time_seconds",
    "size_mm",
    "species_code",
    "date",
)
_VALID_EVENTS = ("SB", "UH")
_MIN_TIME_SECONDS = 3.0
_MAX_TIME_SECONDS = 180.0


def push_results_dicts(
    results: list[dict],
    source: str = "pro-am-manager",
    show_name: str = "",
    dry_run: bool = False,
) -> dict:
    """
    Push new competition results to Supabase from a list-of-dicts payload.

    This is the ingestion entrypoint for live tournament managers
    (e.g. Missoula-Pro-Am-Manager) that don't already have a pandas
    DataFrame. Internally validates every row and delegates the actual
    insert to push_results().

    Each result dict must contain at minimum:
        competitor_id : str  -- must already exist in the competitors table
        event_code    : str  -- 'SB' or 'UH'
        time_seconds  : float -- must be in [3.0, 180.0]
        size_mm       : int or float
        species_code  : str  -- e.g. 'S01', 'S05'
        date          : str  -- ISO 8601 (YYYY-MM-DD)

    Optional keys:
        notes         : str
        field_strength: float
        show_name     : str (overrides show_name argument)

    Validation rules:
        - Missing required field   -> errored (row description in errors[])
        - Invalid event_code       -> errored
        - time outside [3, 180]    -> errored
        - Unknown competitor_id    -> errored (continues processing other rows)
        - Duplicate (competitor_id+event+time+size+date) -> skipped

    Args:
        results:   List of result dicts (see schema above).
        source:    Name of the calling application (logged to sync_log.source_app).
        show_name: Tournament/show identifier; written to results.show_name.
                   May be overridden per-row by including show_name in the dict.
        dry_run:   If True, validate everything but DO NOT write to Supabase.
                   The returned dict still reports inserted/skipped/errors counts
                   as if the write had happened.

    Returns:
        {
            'inserted': int,    # rows actually written (0 if dry_run)
            'skipped':  int,    # duplicates rejected
            'errors':   list[str],  # one entry per rejected row
            'dry_run':  bool,
        }

    Never raises on validation failures -- a malformed row is always reported
    in errors and processing continues. Network/Supabase exceptions DO propagate
    so the caller can decide whether to retry.
    """
    out: dict = {"inserted": 0, "skipped": 0, "errors": [], "dry_run": dry_run}
    if not results:
        return out

    # Pre-fetch known competitor IDs for validation. We try, but if Supabase
    # is unreachable in dry_run mode we still want to validate field shapes.
    known_ids: set = set()
    try:
        client = _get_client()
        comp_resp = client.table("competitors").select("competitor_id").execute()
        known_ids = {str(r.get("competitor_id", "")).strip() for r in (comp_resp.data or [])}
    except Exception as exc:  # pragma: no cover
        if not dry_run:
            raise
        out["errors"].append(f"WARN: competitor lookup failed in dry_run: {exc}")

    valid_rows: list[dict] = []
    for idx, row in enumerate(results):
        # Required field presence
        missing = [f for f in _REQUIRED_RESULT_FIELDS if row.get(f) in (None, "")]
        if missing:
            out["errors"].append(f"row {idx}: missing required fields {missing}")
            continue

        comp_id = str(row["competitor_id"]).strip()
        event_code = str(row["event_code"]).strip().upper()
        if event_code not in _VALID_EVENTS:
            out["errors"].append(
                f"row {idx} ({comp_id}): invalid event_code '{event_code}', "
                f"must be one of {_VALID_EVENTS}"
            )
            continue

        try:
            t = float(row["time_seconds"])
        except (TypeError, ValueError):
            out["errors"].append(f"row {idx} ({comp_id}): time_seconds is not a number")
            continue
        if not (_MIN_TIME_SECONDS <= t <= _MAX_TIME_SECONDS):
            out["errors"].append(
                f"row {idx} ({comp_id}): time_seconds {t} outside "
                f"[{_MIN_TIME_SECONDS}, {_MAX_TIME_SECONDS}]"
            )
            continue

        try:
            size_mm = float(row["size_mm"])
        except (TypeError, ValueError):
            out["errors"].append(f"row {idx} ({comp_id}): size_mm is not a number")
            continue

        if known_ids and comp_id not in known_ids:
            out["errors"].append(
                f"row {idx} ({comp_id}): competitor_id not found in competitors table; "
                f"register first via register_competitor()"
            )
            continue

        valid_rows.append(
            {
                "CompetitorID": comp_id,
                "Event": event_code,
                "Time (seconds)": t,
                "Size (mm)": size_mm,
                "Species Code": str(row["species_code"]).strip(),
                "Date (optional)": str(row["date"]).strip(),
                "Notes (Competition, special circumstances, etc.)": row.get("notes"),
                "field_strength": row.get("field_strength"),
            }
        )

    if dry_run or not valid_rows:
        # Compute would-be-inserted = len(valid_rows). Duplicates not counted in dry_run.
        out["inserted"] = 0
        out["skipped"] = 0
        if dry_run:
            out["errors"].append(f"DRY RUN: validated {len(valid_rows)} rows, no writes performed")
        return out

    df = pd.DataFrame(valid_rows)
    try:
        inserted = push_results(df, show_name=show_name, source_app=source)
    except Exception:
        raise

    out["inserted"] = int(inserted)
    out["skipped"] = max(0, len(valid_rows) - int(inserted))
    return out


def register_competitor(
    name: str,
    country: str = "USA",
    state: str = "",
    gender: str = "",
    region: str = "",
    *,
    wait_for_sync: bool = False,
    sync_timeout_seconds: float = 30.0,
) -> dict:
    """Register a new competitor.

    Behavior depends on whether MNEMEX is configured:

    A. **MNEMEX configured (post-2026-05-04 controlled-write mode).** The
       competitor is minted in MNEMEX. The MNEMEX competitor_id (a ULID) is
       returned along with the local STRATHMARK `competitor_id` once the sync
       function has propagated the row into the STRATHMARK cache.

       If `wait_for_sync=True`, this function blocks for up to
       `sync_timeout_seconds`, polling for the row to appear in the
       STRATHMARK cache. On timeout, returns with `status='registered_in_mnemex_pending_sync'`.

       If `wait_for_sync=False` (default), this function returns immediately
       after the MNEMEX write with `status='registered_in_mnemex_pending_sync'`
       and leaves propagation to the next sync run. Default flipped to False
       in 2026-05-08: nothing in this function triggers a sync, so
       wait_for_sync=True only ever times out. Operators that need the local
       cache row before returning must opt in explicitly AND ensure a sync
       path is running (cron, webhook, or manual_force_sync).

    B. **MNEMEX not configured (transition mode).** Falls back to the legacy
       behavior of minting a STRATHMARK-local competitor_id directly. Logs a
       prominent deprecation warning. This path will be removed after MNEMEX
       is universally available.

    Args:
        name:                 Display name. Required.
        country:              ISO country or free-text. Defaults to 'USA'.
        state:                State/province (free text).
        gender:               'M' or 'F' (free text accepted).
        region:               Free-text region tag.
        wait_for_sync:        MNEMEX mode only. If True, block for sync to land.
        sync_timeout_seconds: MNEMEX mode only. Maximum block duration.

    Returns dict shape (varies by mode):
        {
            'competitor_id': str | None,    -- STRATHMARK local ID once synced
            'mnemex_id':     str | None,    -- canonical MNEMEX ID
            'status':        'created' | 'existing'
                             | 'registered_in_mnemex_pending_sync'
                             | 'created_in_strathmark_legacy',
            'name':          str,
        }

    Raises:
        ValueError:   If name is empty.
        RuntimeError: If both MNEMEX and STRATHMARK Supabase are unreachable.
    """
    name_clean = (name or "").strip()
    if not name_clean:
        raise ValueError("register_competitor: name must not be empty")

    from strathmark.mnemex import is_mnemex_configured

    if is_mnemex_configured():
        return _register_via_mnemex(
            name_clean=name_clean,
            country=country,
            state=state,
            gender=gender,
            region=region,
            wait_for_sync=wait_for_sync,
            sync_timeout_seconds=sync_timeout_seconds,
        )
    return _register_legacy(
        name_clean=name_clean,
        country=country,
        state=state,
        gender=gender,
        region=region,
    )


def _register_via_mnemex(
    *,
    name_clean: str,
    country: str,
    state: str,
    gender: str,
    region: str,
    wait_for_sync: bool,
    sync_timeout_seconds: float,
) -> dict:
    """MNEMEX path: mint canonical ID in MNEMEX, optionally wait for sync."""
    from strathmark.mnemex import register_competitor_in_mnemex

    mnemex_result = register_competitor_in_mnemex(
        name=name_clean,
        country=country,
        state=state,
        gender=gender,
        region=region,
    )
    mnemex_id = mnemex_result["mnemex_id"]
    client = _get_client()

    # An "existing" MNEMEX result is the only case where we expect to find
    # the row in the cache immediately. For "created", skip straight to the
    # wait-for-sync loop so we don't burn an extra round trip.
    if mnemex_result["status"] == "existing":
        cached = _lookup_cache_row(client, mnemex_id)
        if cached is not None:
            return _cache_hit_response(cached, mnemex_id, status="existing")

    if wait_for_sync and sync_timeout_seconds > 0:
        cached = _wait_for_cache_row(client, mnemex_id, sync_timeout_seconds)
        if cached is not None:
            return _cache_hit_response(cached, mnemex_id, status="created")

    return {
        "competitor_id": None,
        "mnemex_id": mnemex_id,
        "status": "registered_in_mnemex_pending_sync",
        "name": name_clean,
    }


def _lookup_cache_row(client, mnemex_id: str) -> Optional[dict]:
    """One-shot cache lookup by mnemex_id. Returns None if not present."""
    resp = (
        client.table("competitors")
        .select("competitor_id, mnemex_id, name")
        .eq("mnemex_id", mnemex_id)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def _wait_for_cache_row(client, mnemex_id: str, timeout_seconds: float) -> Optional[dict]:
    """Poll the cache for a row matching mnemex_id, up to timeout_seconds."""
    import time

    deadline = time.monotonic() + timeout_seconds
    poll_interval = min(2.0, max(0.5, timeout_seconds / 10.0))
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        cached = _lookup_cache_row(client, mnemex_id)
        if cached is not None:
            return cached
    return None


def _cache_hit_response(row: dict, mnemex_id: str, *, status: str) -> dict:
    return {
        "competitor_id": str(row["competitor_id"]),
        "mnemex_id": mnemex_id,
        "status": status,
        "name": row.get("name", ""),
    }


def _register_legacy(
    *,
    name_clean: str,
    country: str,
    state: str,
    gender: str,
    region: str,
) -> dict:
    """Legacy path: mint a STRATHMARK-local competitor_id directly."""
    _log.warning(
        "register_competitor: MNEMEX is not configured; falling back to "
        "legacy STRATHMARK-local mint. This path is deprecated and will be "
        "removed after MNEMEX is universally available. Set MNEMEX_SUPABASE_URL "
        "and MNEMEX_SUPABASE_KEY to use the canonical roster path."
    )
    client = _get_client()

    existing = _find_existing_competitor_by_name(client, name_clean)
    if existing is not None:
        return {
            "competitor_id": str(existing["competitor_id"]),
            "mnemex_id": None,
            "status": "existing",
            "name": existing.get("name", name_clean),
        }

    new_id = _mint_next_competitor_id(client)
    record = {
        "competitor_id": new_id,
        "name": name_clean,
        "country": country or None,
        "state_province": state or None,
        "gender": gender or None,
        "region": region or None,
    }
    client.table("competitors").insert(record).execute()
    log_sync(
        show_name=f"register:{name_clean}",
        source_app="register_competitor",
        records_written=1,
    )
    return {
        "competitor_id": new_id,
        "mnemex_id": None,
        "status": "created_in_strathmark_legacy",
        "name": name_clean,
    }


def _find_existing_competitor_by_name(client, name_clean: str) -> Optional[dict]:
    """Case-insensitive name lookup. Returns the row dict or None."""
    resp = (
        client.table("competitors")
        .select("competitor_id, name")
        .ilike("name", name_clean)
        .execute()
    )
    target = name_clean.lower()
    for row in resp.data or []:
        if str(row.get("name", "")).strip().lower() == target:
            return row
    return None


def _mint_next_competitor_id(client) -> str:
    """Mint the next unused C-prefixed competitor ID.

    Format matches existing seeded data (C### -- 3 digits) when we have
    <1000 competitors; switches to 4 digits at C1000. This avoids the
    latent format-divergence bug flagged in docs/schema-reality-2026-05-04.md.
    """
    resp = client.table("competitors").select("competitor_id").execute()
    max_n = 0
    for row in resp.data or []:
        cid = str(row.get("competitor_id", ""))
        digits = "".join(c for c in cid if c.isdigit())
        if not digits:
            continue
        try:
            max_n = max(max_n, int(digits))
        except ValueError:
            continue
    next_n = max_n + 1
    return f"C{next_n:03d}" if next_n < 1000 else f"C{next_n:04d}"


def format_proam_results(
    raw_results: list[dict],
    competitor_lookup: Optional[Dict[str, str]] = None,
    default_date: Optional[str] = None,
) -> list[dict]:
    """
    Transform Pro-Am Manager result rows into push_results_dicts() input format.

    Pro-Am Manager export rows typically contain:
        competitor_name : str
        event_name      : str   -- e.g. '275mm SB', '300 Underhand'
        time            : float -- raw time in seconds
        species         : str   -- e.g. 'S05', 'Ponderosa Pine'
        date            : str   -- ISO date (optional, default_date used if absent)
        heat            : str   -- ignored at this layer (sync_log only)

    Args:
        raw_results:        Pro-Am Manager rows.
        competitor_lookup:  Mapping of competitor_name -> competitor_id.
                            Names not in the lookup are skipped (returned in
                            the output with competitor_id=None so the caller
                            can prompt for manual mapping).
        default_date:       ISO date string used when a row has no 'date'.

    Returns:
        List of dicts in push_results_dicts() schema. Rows with unmappable
        names are emitted with competitor_id=None so the calling script can
        present them for manual resolution.
    """
    competitor_lookup = competitor_lookup or {}
    out: list[dict] = []
    for row in raw_results:
        name = str(row.get("competitor_name", "")).strip()
        event_name = str(row.get("event_name", "")).strip()
        event_code = _parse_event_code(event_name)
        size_mm = _parse_diameter_mm(event_name)

        comp_id = competitor_lookup.get(name) or competitor_lookup.get(name.lower())

        out.append(
            {
                "competitor_id": comp_id,
                "_competitor_name": name,  # passthrough for unmapped reporting
                "event_code": event_code,
                "time_seconds": row.get("time"),
                "size_mm": size_mm,
                "species_code": str(row.get("species", "")).strip(),
                "date": str(row.get("date") or default_date or "").strip(),
                "notes": row.get("notes"),
            }
        )
    return out


def _parse_event_code(event_name: str) -> str:
    """Extract 'SB' or 'UH' from a free-text Pro-Am event label."""
    s = event_name.lower()
    if "underhand" in s or "uh" in s.split():
        return "UH"
    if "standing" in s or "sb" in s.split():
        return "SB"
    # Last resort: assume SB if neither token present
    return "SB"


def _parse_diameter_mm(event_name: str) -> Optional[float]:
    """Extract a diameter (mm) from a free-text Pro-Am event label."""
    digits = ""
    for ch in event_name:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Existing helper retained
# ---------------------------------------------------------------------------


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


# ===========================================================================
# ML state tables. Writes to these tables originate in STRATHMARK
# itself, not from the MNEMEX sync function. This is the explicit
# carve-out from the controlled-write rule. See migration
# strathmark/migrations/20260504_002_ml_state_tables.sql for DDL and
# docs/ml-persistence-policy.md for retraining cadence, model versioning,
# calibration, and the non-blocking guarantee.
# ===========================================================================


def _new_ulid() -> str:
    """Return a new 26-char ULID string. Lazy import so test fixtures can mock."""
    import ulid

    return str(ulid.new())


def _now_iso() -> str:
    """ISO-8601 UTC timestamp string for TIMESTAMPTZ columns."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def register_model_version(
    model_type: str,
    training_data_cutoff,
    training_row_count: int,
    hyperparameters: dict,
    artifact_storage: str,
    artifact_ref: str,
    artifact_size_bytes: int,
    notes: Optional[str] = None,
) -> str:
    """
    Insert a new model_versions row and return its model_version_id (ULID).

    The new row starts as is_active=FALSE. Use set_active_model() to flip it
    on, which atomically retires whatever was previously active for the same
    model_type.

    Args:
        model_type:           Free-text identifier (e.g. 'xgboost_lightgbm_ensemble').
        training_data_cutoff: Latest result_date in the training set (date or
                              datetime). Stored as TIMESTAMPTZ.
        training_row_count:   Number of rows the model was trained on.
        hyperparameters:      JSON-serializable dict of tuned params + feature list.
        artifact_storage:     'supabase_storage' or 'inline_jsonb'.
        artifact_ref:         Storage path or inline blob ID.
        artifact_size_bytes:  Size of the serialized artifact.
        notes:                Optional free-text notes.

    Returns:
        ULID of the new model_versions row.

    Raises:
        RuntimeError: If env vars are missing.
        ValueError:   If artifact_storage is not one of the allowed values.
    """
    if artifact_storage not in ("supabase_storage", "inline_jsonb"):
        raise ValueError(
            f"artifact_storage must be 'supabase_storage' or 'inline_jsonb', "
            f"got {artifact_storage!r}"
        )
    client = _get_client()
    mv_id = _new_ulid()
    cutoff = (
        training_data_cutoff.isoformat()
        if hasattr(training_data_cutoff, "isoformat")
        else str(training_data_cutoff)
    )
    record = {
        "model_version_id": mv_id,
        "model_type": str(model_type),
        "trained_at": _now_iso(),
        "training_data_cutoff": cutoff,
        "training_row_count": int(training_row_count),
        "hyperparameters": hyperparameters,
        "artifact_storage": artifact_storage,
        "artifact_ref": str(artifact_ref),
        "artifact_size_bytes": int(artifact_size_bytes),
        "is_active": False,
        "notes": notes,
    }
    client.table("model_versions").insert(record).execute()
    return mv_id


def set_active_model(model_version_id: str) -> None:
    """
    Mark the named model version as active and retire any previously active
    model of the same model_type.

    The "only one active per model_type" invariant is also enforced by a
    partial unique index in the DB. This function provides the atomic flip
    so callers don't have to manage it.

    Args:
        model_version_id: ULID of the model to activate.

    Raises:
        RuntimeError:  If env vars are missing.
        LookupError:   If model_version_id does not exist.
    """
    client = _get_client()

    # Preferred path: atomic server-side swap via the migration-installed
    # set_active_model_atomic(target_model_version_id) function. The two
    # updates run in one transaction, so there is no window in which the
    # model_type has zero active rows.
    try:
        client.rpc(
            "set_active_model_atomic",
            {"target_model_version_id": model_version_id},
        ).execute()
        return
    except Exception as exc:
        # Distinguish "function doesn't exist yet" (migration 004 not applied)
        # from a real error. Postgres returns 42883 for undefined function;
        # supabase-py / postgrest-py propagate the message.
        msg = str(exc).lower()
        if "set_active_model_atomic" in msg and (
            "does not exist" in msg or "42883" in msg or "not found" in msg
        ):
            _log.warning(
                "set_active_model_atomic RPC not present (migration 004 not "
                "applied yet); falling back to two-step swap. The fallback "
                "leaves a brief window with no active model for this type."
            )
        else:
            raise

    # Fallback two-step path. Look up the new model's type.
    resp = (
        client.table("model_versions")
        .select("model_version_id, model_type")
        .eq("model_version_id", model_version_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        raise LookupError(f"model_version_id not found: {model_version_id}")
    model_type = rows[0]["model_type"]

    # Retire any currently-active model of this type EXCEPT the target.
    client.table("model_versions").update({"is_active": False, "retired_at": _now_iso()}).eq(
        "model_type", model_type
    ).eq("is_active", True).neq("model_version_id", model_version_id).execute()

    # Activate the target. retired_at=None clears any stale timestamp from
    # a prior retirement cycle.
    client.table("model_versions").update({"is_active": True, "retired_at": None}).eq(
        "model_version_id", model_version_id
    ).execute()


def get_active_model_version(model_type: str) -> Optional[str]:
    """
    Return the model_version_id currently flagged active for the given type,
    or None if no model of that type is active.
    """
    client = _get_client()
    resp = (
        client.table("model_versions")
        .select("model_version_id")
        .eq("model_type", model_type)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0]["model_version_id"] if rows else None


def record_calibration(
    model_version_id: str,
    calibration_method: str,
    calibration_data: dict,
    holdout_residuals: list,
    crps_score: Optional[float] = None,
    coverage_at_90: Optional[float] = None,
    notes: Optional[str] = None,
) -> str:
    """
    Insert a new calibration_tables row and return its calibration_id (ULID).

    Args:
        model_version_id:   ULID of the model this calibration applies to.
        calibration_method: 'conformal_prediction', 'platt', 'isotonic',
                            or 'uncertainty_toolbox'.
        calibration_data:   JSON-serializable calibration table.
        holdout_residuals:  JSON-serializable list of residuals on the
                            calibration holdout.
        crps_score:         Optional CRPS score (lower is better).
        coverage_at_90:     Optional 90% conformal interval coverage.
        notes:              Optional free-text notes.

    Returns:
        ULID of the new calibration_tables row.
    """
    if calibration_method not in (
        "conformal_prediction",
        "platt",
        "isotonic",
        "uncertainty_toolbox",
    ):
        raise ValueError(f"unknown calibration_method: {calibration_method!r}")
    client = _get_client()
    cal_id = _new_ulid()
    record = {
        "calibration_id": cal_id,
        "model_version_id": model_version_id,
        "calibrated_at": _now_iso(),
        "calibration_method": calibration_method,
        "calibration_data": calibration_data,
        "holdout_residuals": holdout_residuals,
        "crps_score": crps_score,
        "coverage_at_90": coverage_at_90,
        "notes": notes,
    }
    client.table("calibration_tables").insert(record).execute()
    return cal_id


def store_features(
    model_version_id: str,
    competitor_id: str,
    event_code: str,
    features: dict,
) -> str:
    """
    Upsert a feature_store row for (model_version_id, competitor_id, event_code).

    Idempotent: re-storing the same feature vector for the same key replaces it.

    Args:
        model_version_id: ULID of the model that consumed these features.
        competitor_id:    Competitor.
        event_code:       'SB' or 'UH'.
        features:         JSON-serializable dict of feature name -> value.

    Returns:
        feature_set_id (ULID) of the row. On upsert of an existing row, the
        returned ID is the existing row's ID, not a fresh one.
    """
    client = _get_client()

    # Upsert by composite UNIQUE — supabase-py needs an existing on_conflict
    # column list. The migration declared UNIQUE (model_version_id,
    # competitor_id, event_code), so we tell PostgREST to use that.
    fs_id = _new_ulid()
    record = {
        "feature_set_id": fs_id,
        "model_version_id": model_version_id,
        "competitor_id": competitor_id,
        "event_code": str(event_code).strip().upper(),
        "features_jsonb": features,
        "computed_at": _now_iso(),
    }
    resp = (
        client.table("feature_store")
        .upsert(record, on_conflict="model_version_id,competitor_id,event_code")
        .execute()
    )
    rows = resp.data or []
    if rows and "feature_set_id" in rows[0]:
        return rows[0]["feature_set_id"]
    return fs_id


def record_prediction(
    model_version_id: str,
    competitor_id: str,
    event_code: str,
    show_name: str,
    predicted_time: float,
    predicted_variance: float,
    cascade_level_used: str,
    notes: Optional[str] = None,
) -> Optional[str]:
    """
    Insert a predictions row capturing one prediction.

    Best-effort: never raises on Supabase failure (returns None instead).
    The non-blocking guarantee in docs/ml-persistence-policy.md requires that
    a write failure here MUST NOT block the calling cascade from returning
    its prediction.

    Args:
        model_version_id:   ULID of the active model.
        competitor_id:      Competitor.
        event_code:         'SB' or 'UH'.
        show_name:          Tournament name.
        predicted_time:     Predicted time in seconds.
        predicted_variance: Predicted variance (per-competitor std-dev squared,
                            or whatever the model emits).
        cascade_level_used: 'manual', 'llm', 'ml', 'baseline', or 'panel'.
        notes:              Optional free-text.

    Returns:
        prediction_id (ULID) on success, None on any failure (including
        an unknown cascade_level_used). Validation runs inside the
        try/except so the non-blocking guarantee on the prediction hot
        path holds even when a caller passes a bad value.
    """
    pred_id = _new_ulid()
    try:
        if cascade_level_used not in ("manual", "llm", "ml", "baseline", "panel"):
            raise ValueError(f"unknown cascade_level_used: {cascade_level_used!r}")
        client = _get_client()
        record = {
            "prediction_id": pred_id,
            "model_version_id": model_version_id,
            "competitor_id": competitor_id,
            "event_code": str(event_code).strip().upper(),
            "show_name": show_name,
            "predicted_time": round(float(predicted_time), 3),
            "predicted_variance": round(float(predicted_variance), 6),
            "cascade_level_used": cascade_level_used,
            "predicted_at": _now_iso(),
            "notes": notes,
        }
        client.table("predictions").insert(record).execute()
        return pred_id
    except Exception as exc:  # pragma: no cover
        _log.warning("record_prediction failed (non-fatal): %s", exc)
        return None


def settle_prediction(
    prediction_id: str,
    result_id: int,
    actual_time: float,
) -> Optional[float]:
    """
    Update a predictions row with the actual result and computed residual.

    Best-effort: never raises on Supabase failure (returns None instead).
    Residual = actual_time - predicted_time. Looks up predicted_time from the
    existing row so callers don't have to pass it back.

    Args:
        prediction_id: ULID returned from record_prediction().
        result_id:     ID of the actual result row in the results table.
        actual_time:   Observed time in seconds.

    Returns:
        Computed residual (float) on success, or None on Supabase failure or
        if the prediction row is not found.
    """
    try:
        client = _get_client()
        resp = (
            client.table("predictions")
            .select("predicted_time")
            .eq("prediction_id", prediction_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            _log.warning("settle_prediction: prediction_id not found: %s", prediction_id)
            return None
        predicted_time = float(rows[0]["predicted_time"])
        residual = float(actual_time) - predicted_time
        client.table("predictions").update(
            {
                "result_id": int(result_id),
                "residual": round(residual, 3),
            }
        ).eq("prediction_id", prediction_id).execute()
        return round(residual, 3)
    except Exception as exc:  # pragma: no cover
        _log.warning("settle_prediction failed (non-fatal): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Prediction Engine V2 append-only cloud mirror
# ---------------------------------------------------------------------------

_LEDGER_REQUEST_FIELDS = {
    "ledger_request_id",
    "caller_id",
    "request_id",
    "request_hash",
    "hash_algorithm",
    "event_code",
    "prediction_as_of",
    "created_at",
}
_LEDGER_PREDICTION_FIELDS = {
    "prediction_id",
    "ledger_request_id",
    "competitor_id",
    "event_code",
    "median_seconds",
    "assigned_mark",
    "source",
    "training_eligible",
    "engine_version",
    "model_version",
    "calibration_version",
    "evidence_cutoff",
    "interval_lower",
    "interval_upper",
    "interval_coverage",
    "interval_state",
    "interval_scope",
    "ignored_factors",
    "warnings",
    "optimizer",
    "optimizer_metadata",
    "created_at",
}
_LEDGER_FEATURE_FIELDS = {
    "feature_snapshot_id",
    "prediction_id",
    "feature_name",
    "numeric_value",
    "created_at",
}
_LEDGER_SETTLEMENT_FIELDS = {
    "settlement_id",
    "prediction_id",
    "revision",
    "competitor_id",
    "event_code",
    "actual_time",
    "residual",
    "actor",
    "reason",
    "payload_hash",
    "supersedes_settlement_id",
    "settled_at",
}
_SHADOW_RECEIPT_FIELDS = {
    "schema_version",
    "ledger_request_id",
    "caller_id",
    "request_id",
    "core_schema_version",
    "identity_schema_version",
    "observation_schema_version",
    "observation_fingerprint",
    "core",
}
_MIRROR_DELIVERY_FIELDS = {
    "schema_version",
    "outbox_id",
    "entity_id",
    "created_at",
    "payload_hash",
}
_NUMERIC_OUTCOME_MIRROR_FIELDS = {
    "schema_version",
    "field_revision_id",
    "outcome_revision_id",
    "ledger_request_id",
    "caller_id",
    "actor",
    "reason_code",
    "created_at",
    "revisions",
}
_NUMERIC_REVISION_MIRROR_FIELDS = {
    "revision_id",
    "prediction_id",
    "revision",
    "competitor_id",
    "event_code",
    "action",
    "actual_time",
    "residual",
    "supersedes_revision_id",
}
_FORBIDDEN_SHADOW_KEYS = {
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
}


def _reject_extra_fields(record: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extras = set(record) - allowed
    if extras:
        raise ValueError(f"unsanitized {label} fields: {sorted(extras)}")


def _require_exact_fields(record: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(record)
    if actual != expected:
        missing = sorted(expected - actual)
        extras = sorted(actual - expected)
        raise ValueError(f"invalid {label} fields: missing={missing}, extra={extras}")


def _is_json_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _validate_shadow_ledger_projection(ledger: Mapping[str, Any]) -> None:
    """Validate the exact 006 field projection embedded by shadow receipts."""

    _require_exact_fields(ledger, {"request", "predictions", "features"}, "ledger")
    request = ledger.get("request")
    predictions = ledger.get("predictions")
    features = ledger.get("features")
    if not isinstance(request, Mapping):
        raise ValueError("invalid shadow ledger request")
    _require_exact_fields(request, _LEDGER_REQUEST_FIELDS, "request")
    if not all(isinstance(request.get(key), str) for key in _LEDGER_REQUEST_FIELDS):
        raise ValueError("invalid shadow ledger request JSON types")
    if request.get("hash_algorithm") not in {"raw-v1", "active-v2"}:
        raise ValueError("invalid shadow ledger request hash_algorithm")
    if request.get("event_code") not in {"SB", "UH"}:
        raise ValueError("invalid shadow ledger request event_code")

    if not isinstance(predictions, list) or not 1 <= len(predictions) <= 512:
        raise ValueError("invalid shadow ledger prediction cardinality")
    if not isinstance(features, list) or len(features) > 16_384:
        raise ValueError("invalid shadow ledger feature cardinality")

    required_prediction_strings = {
        "prediction_id",
        "ledger_request_id",
        "competitor_id",
        "event_code",
        "source",
        "created_at",
    }
    optional_prediction_strings = {
        "engine_version",
        "model_version",
        "calibration_version",
        "evidence_cutoff",
        "interval_state",
        "interval_scope",
        "optimizer",
    }
    optional_prediction_numbers = {
        "interval_lower",
        "interval_upper",
        "interval_coverage",
    }
    for prediction in predictions:
        if not isinstance(prediction, Mapping):
            raise ValueError("invalid shadow ledger prediction row")
        _require_exact_fields(prediction, _LEDGER_PREDICTION_FIELDS, "prediction")
        if not all(isinstance(prediction.get(key), str) for key in required_prediction_strings):
            raise ValueError("invalid shadow ledger prediction JSON types")
        if prediction.get("event_code") not in {"SB", "UH"}:
            raise ValueError("invalid shadow ledger prediction event_code")
        if not _is_json_number(prediction.get("median_seconds")):
            raise ValueError("invalid shadow ledger prediction median_seconds")
        assigned_mark = prediction.get("assigned_mark")
        if (
            isinstance(assigned_mark, bool)
            or not isinstance(assigned_mark, int)
            or not 3 <= assigned_mark <= 2_147_483_647
        ):
            raise ValueError("invalid shadow ledger prediction assigned_mark")
        if not isinstance(prediction.get("training_eligible"), bool):
            raise ValueError("invalid shadow ledger prediction training_eligible")
        if any(
            prediction.get(key) is not None and not isinstance(prediction.get(key), str)
            for key in optional_prediction_strings
        ):
            raise ValueError("invalid shadow ledger optional prediction JSON types")
        if any(
            prediction.get(key) is not None and not _is_json_number(prediction.get(key))
            for key in optional_prediction_numbers
        ):
            raise ValueError("invalid shadow ledger interval JSON types")
        for key in ("ignored_factors", "warnings"):
            values = prediction.get(key)
            if (
                not isinstance(values, list)
                or len(values) > 128
                or any(not isinstance(value, str) for value in values)
            ):
                raise ValueError(f"invalid shadow ledger prediction {key}")
        if not isinstance(prediction.get("optimizer_metadata"), Mapping):
            raise ValueError("invalid shadow ledger prediction optimizer_metadata")

    for feature in features:
        if not isinstance(feature, Mapping):
            raise ValueError("invalid shadow ledger feature row")
        _require_exact_fields(feature, _LEDGER_FEATURE_FIELDS, "feature")
        if not all(
            isinstance(feature.get(key), str)
            for key in ("feature_snapshot_id", "prediction_id", "feature_name", "created_at")
        ) or not _is_json_number(feature.get("numeric_value")):
            raise ValueError("invalid shadow ledger feature JSON types")


def _validate_legacy_mirror_payload(payload: Mapping[str, Any]) -> None:
    keys = set(payload)
    if keys == {"request", "predictions", "features"}:
        request = payload["request"]
        predictions = payload["predictions"]
        features = payload["features"]
        if not isinstance(request, Mapping):
            raise ValueError("ledger request mirror row must be an object")
        _reject_extra_fields(request, _LEDGER_REQUEST_FIELDS, "request")
        if request.get("hash_algorithm") not in {"raw-v1", "active-v2"}:
            raise ValueError("ledger request hash_algorithm is invalid")
        if not isinstance(predictions, list) or not isinstance(features, list):
            raise ValueError("ledger predictions and features must be lists")
        for prediction in predictions:
            if not isinstance(prediction, Mapping):
                raise ValueError("ledger prediction row must be an object")
            _reject_extra_fields(prediction, _LEDGER_PREDICTION_FIELDS, "prediction")
            if not str(prediction.get("competitor_id") or "").strip():
                raise ValueError("cloud ledger predictions require stable competitor_id")
        for feature in features:
            if not isinstance(feature, Mapping):
                raise ValueError("ledger feature row must be an object")
            _reject_extra_fields(feature, _LEDGER_FEATURE_FIELDS, "feature")
        return
    if keys == {"settlement"}:
        settlement = payload["settlement"]
        if not isinstance(settlement, Mapping):
            raise ValueError("ledger settlement mirror row must be an object")
        _reject_extra_fields(settlement, _LEDGER_SETTLEMENT_FIELDS, "settlement")
        if not str(settlement.get("competitor_id") or "").strip():
            raise ValueError("cloud ledger settlements require stable competitor_id")
        return
    raise ValueError("ledger mirror payload has an invalid or unsanitized shape")


def _reject_forbidden_shadow_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = {str(key).casefold() for key in value}.intersection(_FORBIDDEN_SHADOW_KEYS)
        if forbidden:
            raise ValueError(f"unsanitized shadow fields: {sorted(forbidden)}")
        for child in value.values():
            _reject_forbidden_shadow_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_shadow_keys(child)


def _validate_shadow_mirror_payload(payload: Mapping[str, Any]) -> None:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("shadow mirror payload must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ValueError("shadow mirror payload exceeds the 1 MiB limit")
    _reject_forbidden_shadow_keys(payload)

    if payload.get("schema_version") != _SHADOW_MIRROR_ENVELOPE_SCHEMA_VERSION:
        raise ValueError("shadow mirror envelope schema_version is invalid")
    kind = payload.get("kind")
    expected = (
        {"schema_version", "kind", "delivery", "ledger", "receipt"}
        if kind == "shadow_receipt"
        else {"schema_version", "kind", "delivery", "numeric_outcome_revision"}
        if kind == "numeric_outcome_revision"
        else set()
    )
    if not expected:
        raise ValueError("shadow mirror kind is invalid")
    _reject_extra_fields(payload, expected, "shadow envelope")

    delivery = payload.get("delivery")
    if not isinstance(delivery, Mapping):
        raise ValueError("shadow mirror delivery must be an object")
    _reject_extra_fields(delivery, _MIRROR_DELIVERY_FIELDS, "shadow delivery")
    if delivery.get("schema_version") != _MIRROR_DELIVERY_SCHEMA_VERSION:
        raise ValueError("shadow mirror delivery schema_version is invalid")
    for key in ("outbox_id", "entity_id", "created_at"):
        value = str(delivery.get(key) or "").strip()
        if not value or len(value) > 128:
            raise ValueError(f"shadow mirror delivery {key} is invalid")
    payload_hash = str(delivery.get("payload_hash") or "")
    if len(payload_hash) != 64 or any(char not in "0123456789abcdef" for char in payload_hash):
        raise ValueError("shadow mirror delivery payload_hash is invalid")
    semantic_payload = dict(payload)
    semantic_payload.pop("delivery", None)
    expected_payload_hash = hashlib.sha256(
        json.dumps(
            semantic_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if payload_hash != expected_payload_hash:
        raise ValueError("shadow mirror delivery payload_hash does not match the envelope")

    if kind == "shadow_receipt":
        ledger = payload.get("ledger")
        receipt = payload.get("receipt")
        if not isinstance(ledger, Mapping) or not isinstance(receipt, Mapping):
            raise ValueError("shadow receipt mirror rows must be objects")
        _validate_shadow_ledger_projection(ledger)
        _reject_extra_fields(receipt, _SHADOW_RECEIPT_FIELDS, "shadow receipt")
        if receipt.get("schema_version") != _SHADOW_RECEIPT_MIRROR_SCHEMA_VERSION:
            raise ValueError("shadow receipt mirror schema_version is invalid")
        core = receipt.get("core")
        request = ledger.get("request")
        if not isinstance(core, Mapping) or not isinstance(request, Mapping):
            raise ValueError("shadow receipt core and request must be objects")
        observation = core.get("observation")
        if not isinstance(observation, Mapping):
            raise ValueError("shadow receipt observation metadata must be an object")
        bindings = (
            receipt.get("ledger_request_id") == request.get("ledger_request_id"),
            receipt.get("caller_id") == request.get("caller_id") == core.get("consumer_id"),
            receipt.get("request_id") == request.get("request_id") == core.get("request_id"),
            receipt.get("core_schema_version") == core.get("schema_version"),
            receipt.get("identity_schema_version") == core.get("identity_schema_version"),
            receipt.get("observation_schema_version") == observation.get("schema_version"),
            receipt.get("observation_fingerprint") == observation.get("fingerprint"),
            delivery.get("entity_id") == receipt.get("ledger_request_id"),
        )
        if not all(bindings):
            raise ValueError("shadow receipt mirror linkage is invalid")
        return

    outcome = payload.get("numeric_outcome_revision")
    if not isinstance(outcome, Mapping):
        raise ValueError("numeric outcome mirror row must be an object")
    _reject_extra_fields(outcome, _NUMERIC_OUTCOME_MIRROR_FIELDS, "numeric outcome")
    if outcome.get("schema_version") != _NUMERIC_OUTCOME_MIRROR_SCHEMA_VERSION:
        raise ValueError("numeric outcome mirror schema_version is invalid")
    if delivery.get("entity_id") != outcome.get("field_revision_id"):
        raise ValueError("numeric outcome mirror delivery linkage is invalid")
    reason_code = outcome.get("reason_code")
    if reason_code is not None and reason_code not in {
        "corrected_time",
        "retract_invalid_numeric_evidence",
        "valid_replacement",
    }:
        raise ValueError("numeric outcome reason_code is invalid")
    revisions = outcome.get("revisions")
    if not isinstance(revisions, list) or not 1 <= len(revisions) <= 512:
        raise ValueError("numeric outcome revisions must contain 1 to 512 rows")
    for revision in revisions:
        if not isinstance(revision, Mapping):
            raise ValueError("numeric settlement revision must be an object")
        _reject_extra_fields(revision, _NUMERIC_REVISION_MIRROR_FIELDS, "numeric revision")
        action = revision.get("action")
        if action not in {"settle", "void"}:
            raise ValueError("numeric settlement action is invalid")
        revision_number = revision.get("revision")
        if (
            isinstance(revision_number, bool)
            or not isinstance(revision_number, int)
            or not 1 <= revision_number <= 2_147_483_647
        ):
            raise ValueError("numeric settlement revision is invalid")
        supersedes = revision.get("supersedes_revision_id")
        if revision_number == 1 and supersedes is not None:
            raise ValueError("initial numeric settlement supersedes_revision_id must be null")
        if revision_number > 1 and not str(supersedes or "").strip():
            raise ValueError("noninitial numeric settlement requires supersedes_revision_id")
        if (revision_number > 1 or action == "void") and reason_code is None:
            raise ValueError("numeric correction or void requires a reason_code")
        actual_time = revision.get("actual_time")
        residual = revision.get("residual")
        if action == "void":
            if actual_time is not None or residual is not None:
                raise ValueError("numeric void actual_time and residual must be null")
            continue
        if (
            isinstance(actual_time, bool)
            or not isinstance(actual_time, (int, float))
            or not math.isfinite(actual_time)
            or not 0 < actual_time <= 300
            or isinstance(residual, bool)
            or not isinstance(residual, (int, float))
            or not math.isfinite(residual)
        ):
            raise ValueError("numeric settlement values must be finite and in range")


def mirror_prediction_ledger(payload: Mapping[str, Any]) -> bool:
    """Mirror one sanitized ledger transaction through a service-role RPC.

    The Postgres function performs its own transaction and is granted only to
    ``service_role``.  Validation occurs before client creation so accidental
    names, notes, histories, or secrets cannot cross the network boundary.
    Exceptions intentionally propagate to :class:`PredictionLedger`, which
    converts them into a sanitized non-fatal cloud status.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("ledger mirror payload must be an object")
    if payload.get("schema_version") == _SHADOW_MIRROR_ENVELOPE_SCHEMA_VERSION:
        _validate_shadow_mirror_payload(payload)
        rpc_name = "append_shadow_mirror_v1"
        rpc_parameters = {"mirror_payload": payload}
    else:
        _validate_legacy_mirror_payload(payload)
        rpc_name = "append_prediction_ledger_v2"
        rpc_parameters = {"ledger_payload": payload}

    client = _get_client()
    client.rpc(rpc_name, rpc_parameters).execute()
    return True
