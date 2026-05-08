"""
Sync function: MNEMEX -> STRATHMARK Supabase
=============================================

Three paths, one upsert core:

1. nightly_batch()                  -- cron at 03:00 UTC
2. strathex_finalization(event_id)  -- webhook from STRATHEX on finalize
3. manual_force_sync(show_name=None) -- admin button / CLI

All three call into _do_sync() which:
- pulls canonical rows from MNEMEX (filtered to chopping disciplines and
  non-provisional)
- maps MNEMEX columns to STRATHMARK schema
- upserts into the STRATHMARK cache by mnemex_id
- writes a sync_log row with the appropriate sync_path

Dry-run behavior
----------------
When MNEMEX is unconfigured (`MNEMEX_SUPABASE_URL` / `MNEMEX_SUPABASE_KEY`
unset), every path returns a SyncResult with `dry_run=True`,
`rows_pulled=0`, `rows_upserted=0`. No writes occur. This is intentional:
the sync function ships and runs in production BEFORE MNEMEX exists.

Failure semantics
-----------------
The sync function is operator-action infrastructure, NOT hot-path. It DOES
raise on Supabase failures so the operator (or the cron job) knows the
sync didn't land. Callers (cron, webhook) decide whether to retry.

Public API
----------
    SyncResult          -- result dataclass
    nightly_batch()
    strathex_finalization(event_id)
    manual_force_sync(show_name=None, since=None)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd

_log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Result of one sync invocation."""

    sync_path: str  # 'nightly_batch' | 'strathex_finalization' | 'manual_force_sync'
    dry_run: bool
    rows_pulled: int
    rows_upserted: int
    rows_skipped: int = 0
    errors: List[str] = field(default_factory=list)
    mnemex_cursor: Optional[datetime] = None
    notes: Optional[str] = None

    def summary(self) -> str:
        prefix = "DRY RUN" if self.dry_run else "SYNCED"
        return (
            f"{prefix} {self.sync_path}: "
            f"pulled={self.rows_pulled} upserted={self.rows_upserted} "
            f"skipped={self.rows_skipped} errors={len(self.errors)}"
        )


# ---------------------------------------------------------------------------
# Public sync paths
# ---------------------------------------------------------------------------


def nightly_batch(
    cursor_lookback_hours: int = 25,
    dry_run: bool = False,
) -> SyncResult:
    """Pull everything in MNEMEX updated since the last successful nightly sync.

    Args:
        cursor_lookback_hours: How far back to look. Defaults to 25 hours so
                               consecutive nightly runs always overlap by at
                               least an hour, absorbing clock skew. The
                               upsert is idempotent so overlap is safe.
        dry_run:               If True, validate but do not write.

    Returns:
        SyncResult with sync_path='nightly_batch'.
    """
    cursor = _read_last_sync_cursor() or (
        datetime.now(timezone.utc) - timedelta(hours=cursor_lookback_hours)
    )
    return _do_sync(
        sync_path="nightly_batch",
        since=cursor,
        event_id=None,
        show_name=None,
        dry_run=dry_run,
    )


def _read_last_sync_cursor() -> Optional[datetime]:
    """Return the mnemex_cursor of the most-recent successful nightly_batch row.

    Reading the cursor from sync_log (rather than computing it from
    wall-clock) means a cron outage longer than the lookback window doesn't
    silently lose the rows MNEMEX accumulated during the gap. Returns None
    on first run, error, or empty sync_log; the caller falls back to
    wall-clock minus the lookback.
    """
    try:
        from strathmark.db import _get_client

        client = _get_client()
        resp = (
            client.table("sync_log")
            .select("mnemex_cursor")
            .eq("sync_path", "nightly_batch")
            .order("synced_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        cursor_str = rows[0].get("mnemex_cursor")
        if not cursor_str:
            return None
        cursor = datetime.fromisoformat(cursor_str)
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=timezone.utc)
        return cursor
    except Exception as exc:
        _log.warning("could not read last sync cursor (%s); falling back to wall clock", exc)
        return None


def strathex_finalization(event_id: str, dry_run: bool = False) -> SyncResult:
    """Pull just the rows associated with one STRATHEX-finalized event.

    Args:
        event_id: MNEMEX-side event ID. Must be passed through from STRATHEX.
        dry_run:  If True, validate but do not write.

    Returns:
        SyncResult with sync_path='strathex_finalization'.
    """
    return _do_sync(
        sync_path="strathex_finalization",
        since=None,
        event_id=event_id,
        show_name=None,
        dry_run=dry_run,
    )


def manual_force_sync(
    show_name: Optional[str] = None,
    since: Optional[datetime] = None,
    dry_run: bool = False,
) -> SyncResult:
    """Operator-driven sync. Either a show_name filter, a since cutoff, or both.

    Args:
        show_name: Optional MNEMEX show_name filter.
        since:     Optional datetime cutoff (UTC).
        dry_run:   If True, validate but do not write.

    Returns:
        SyncResult with sync_path='manual_force_sync'.
    """
    return _do_sync(
        sync_path="manual_force_sync",
        since=since,
        event_id=None,
        show_name=show_name,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Internal core
# ---------------------------------------------------------------------------


_VALID_SYNC_PATHS = ("nightly_batch", "strathex_finalization", "manual_force_sync")


def _do_sync(
    sync_path: str,
    since: Optional[datetime],
    event_id: Optional[str],
    show_name: Optional[str],
    dry_run: bool,
) -> SyncResult:
    if sync_path not in _VALID_SYNC_PATHS:
        raise ValueError(f"unknown sync_path: {sync_path!r}")

    from strathmark.mnemex import is_mnemex_configured, pull_canonical_results

    # Pre-flight: if MNEMEX isn't configured, dry-run regardless of caller intent.
    if not is_mnemex_configured():
        _log.warning(
            "%s: MNEMEX is not configured (MNEMEX_SUPABASE_URL/KEY unset); "
            "treating as dry run with zero rows.",
            sync_path,
        )
        return SyncResult(
            sync_path=sync_path,
            dry_run=True,
            rows_pulled=0,
            rows_upserted=0,
            mnemex_cursor=since,
            notes="MNEMEX unconfigured; sync is no-op until env vars are set",
        )

    # Pull from MNEMEX
    df = pull_canonical_results(since=since, include_provisional=False)
    if event_id is not None and not df.empty:
        # Filter by event_id at the client side. MNEMEX may or may not have
        # an event_id column; if absent, this filter yields zero rows.
        if "event_id" in df.columns:
            df = df[df["event_id"] == event_id]
        else:
            _log.warning(
                "strathex_finalization: MNEMEX results table has no event_id "
                "column; cannot filter by event_id=%r. Skipping all rows.",
                event_id,
            )
            df = df.iloc[0:0]
    if show_name is not None and not df.empty:
        if "show_name" in df.columns:
            df = df[df["show_name"] == show_name]
        else:
            _log.warning(
                "manual_force_sync: MNEMEX results table has no show_name "
                "column; cannot filter by show_name=%r.",
                show_name,
            )
            df = df.iloc[0:0]

    rows_pulled = len(df)
    if rows_pulled == 0:
        _log.info("%s: zero rows to sync", sync_path)
        result = SyncResult(
            sync_path=sync_path,
            dry_run=dry_run,
            rows_pulled=0,
            rows_upserted=0,
            mnemex_cursor=since,
        )
        if not dry_run:
            _write_sync_log(result)
        return result

    # Map MNEMEX columns to STRATHMARK schema
    try:
        records, unresolved_count = _map_mnemex_to_strathmark(df)
    except KeyError as exc:
        # Surfaces missing required columns from MNEMEX
        return SyncResult(
            sync_path=sync_path,
            dry_run=dry_run,
            rows_pulled=rows_pulled,
            rows_upserted=0,
            errors=[f"MNEMEX row mapping failed: {exc}"],
            mnemex_cursor=since,
        )

    rows_upserted = 0
    errors: list[str] = []
    if unresolved_count > 0:
        msg = (
            f"{unresolved_count} of {len(records)} rows had unresolved "
            f"competitor_mnemex_id and were upserted with NULL competitor_id; "
            f"run register_competitor or rekey_against_mnemex to backfill."
        )
        errors.append(msg)
        _log.warning("%s: %s", sync_path, msg)

    if dry_run:
        _log.info("%s: DRY RUN. would upsert %d rows", sync_path, len(records))
    else:
        from strathmark.db import _get_client

        client = _get_client()
        try:
            client.table("results").upsert(records, on_conflict="mnemex_id").execute()
            rows_upserted = len(records)
        except Exception as exc:
            # Audit-trail-then-raise: write a sync_log row marking the
            # failure (best-effort -- don't mask the original exception
            # if logging itself fails), then re-raise so cron / webhook
            # callers see a non-zero exit per the module docstring.
            errors.append(f"upsert into results failed: {exc}")
            _log.error("%s: upsert failed: %s", sync_path, exc)
            failure_result = SyncResult(
                sync_path=sync_path,
                dry_run=False,
                rows_pulled=rows_pulled,
                rows_upserted=0,
                errors=list(errors),
                mnemex_cursor=since,
            )
            try:
                _write_sync_log(failure_result)
            except Exception:  # pragma: no cover
                pass
            raise

    result = SyncResult(
        sync_path=sync_path,
        dry_run=dry_run,
        rows_pulled=rows_pulled,
        rows_upserted=rows_upserted,
        errors=errors,
        mnemex_cursor=since,
    )
    if not dry_run:
        _write_sync_log(result)
    return result


def _map_mnemex_to_strathmark(df: pd.DataFrame) -> tuple[list[dict], int]:
    """Map MNEMEX-side columns to STRATHMARK results-table columns.

    Required MNEMEX columns:
        mnemex_id, competitor_mnemex_id, event_type, time_seconds, size_mm,
        species_code, result_date, show_name, updated_at

    Optional:
        notes, field_strength

    The STRATHMARK schema also requires competitor_id (FK to local
    competitors table, distinct from competitor_mnemex_id). This function
    looks up the local competitor_id from the competitors.mnemex_id column.
    Rows whose competitor cannot be resolved are written with NULL
    competitor_id (the FK is nullable) and counted in `unresolved`. Callers
    surface the count in the SyncResult so operators see how many rows are
    waiting for a competitor sync.

    Returns:
        (records, unresolved_count)
    """
    required = (
        "mnemex_id",
        "competitor_mnemex_id",
        "event_type",
        "time_seconds",
        "size_mm",
        "species_code",
        "result_date",
        "show_name",
    )
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"MNEMEX results missing required columns: {missing}")

    # Resolve competitor_mnemex_id -> local competitor_id
    competitor_lookup = _resolve_competitor_lookup(df["competitor_mnemex_id"].unique().tolist())

    now_iso = datetime.now(timezone.utc).isoformat()
    records: list[dict] = []
    unresolved = 0
    for _, row in df.iterrows():
        local_competitor_id = competitor_lookup.get(str(row["competitor_mnemex_id"]).strip())
        if local_competitor_id is None:
            unresolved += 1
        records.append(
            {
                "mnemex_id": str(row["mnemex_id"]),
                "competitor_id": local_competitor_id,
                "event": _normalize_event(row["event_type"]),
                "time_seconds": float(row["time_seconds"]),
                "size_mm": int(row["size_mm"]),
                "species_code": str(row["species_code"]),
                "result_date": _safe_date_str(row["result_date"]),
                "show_name": str(row["show_name"]),
                "source_app": "mnemex_sync",
                "source_type": "mnemex_sync",
                "notes": row.get("notes") if "notes" in df.columns else None,
                "field_strength": (
                    float(row["field_strength"])
                    if "field_strength" in df.columns and pd.notna(row.get("field_strength"))
                    else None
                ),
                "last_synced_at": now_iso,
            }
        )
    return records, unresolved


_COMPETITOR_LOOKUP_CHUNK = 200


def _resolve_competitor_lookup(mnemex_competitor_ids: list[str]) -> dict[str, str]:
    """Build {mnemex_competitor_id -> strathmark competitor_id} from STRATHMARK.

    Chunks the .in_() query so large pulls don't exceed PostgREST's URL
    length limit. With ULID-shaped IDs (~26 chars) and chunk size 200,
    each request URL stays well under the typical 8 KB limit.
    """
    if not mnemex_competitor_ids:
        return {}
    from strathmark.db import _get_client

    client = _get_client()
    cleaned = [str(x).strip() for x in mnemex_competitor_ids if x is not None]
    out: dict[str, str] = {}
    for i in range(0, len(cleaned), _COMPETITOR_LOOKUP_CHUNK):
        chunk = cleaned[i : i + _COMPETITOR_LOOKUP_CHUNK]
        resp = (
            client.table("competitors")
            .select("competitor_id, mnemex_id")
            .in_("mnemex_id", chunk)
            .execute()
        )
        for row in resp.data or []:
            if row.get("mnemex_id") and row.get("competitor_id"):
                out[str(row["mnemex_id"]).strip()] = str(row["competitor_id"]).strip()
    return out


_EVENT_NORMALIZATION = {
    "SB": "SB",
    "STR_SB": "SB",
    "STANDING_BLOCK": "SB",
    "UH": "UH",
    "STR_UH": "UH",
    "UNDERHAND": "UH",
    "JACK_AND_JILL": "JACK_AND_JILL",
}


def _normalize_event(raw) -> str:
    """Normalize a MNEMEX event_type code to STRATHMARK's canonical form.

    Uses an exact-match dispatch table (not substring) so codes like 'PUSH',
    'CRUSH', or future MNEMEX additions don't accidentally collide with
    'UH'. Unknown codes pass through uppercased; downstream constraints
    will surface them.
    """
    s = str(raw).strip().upper().replace(" ", "_")
    return _EVENT_NORMALIZATION.get(s, s)


def _safe_date_str(val) -> Optional[str]:
    """Coerce a date-ish value to an ISO date string (YYYY-MM-DD).

    Catches only the narrow set of exceptions a malformed datetime can raise
    so genuine programmer errors propagate. A logged warning surfaces the
    offending value so MNEMEX data quality issues are visible to operators.
    """
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        try:
            return val.isoformat()[:10]
        except (TypeError, ValueError, AttributeError) as exc:
            _log.warning("malformed date in MNEMEX row %r: %s", val, exc)
            return None
    s = str(val).strip()
    return s[:10] if s else None


def _write_sync_log(result: SyncResult) -> None:
    """Append the sync_log row reflecting this run.

    Raises on Supabase failure. Loss of audit trail is a real failure
    operators must see, matching the module's overall raise-on-failure
    contract. The failure path inside `_do_sync` wraps this call in a
    nested try/except so a sync_log failure doesn't mask the original
    upsert exception.
    """
    from strathmark.db import _get_client

    client = _get_client()
    record = {
        "show_name": result.notes or result.sync_path,
        "source_app": "mnemex_sync",
        "records_written": result.rows_upserted,
        "sync_path": result.sync_path,
        "mnemex_cursor": (result.mnemex_cursor.isoformat() if result.mnemex_cursor else None),
        "rows_pulled": result.rows_pulled,
        "rows_upserted": result.rows_upserted,
        "errors_jsonb": result.errors or None,
    }
    client.table("sync_log").insert(record).execute()
