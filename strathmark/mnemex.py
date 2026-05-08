"""
MNEMEX client
=============

MNEMEX is the universal archive of record for ALL timbersports results
across ALL disciplines. STRATHMARK reads MNEMEX directly via the MNEMEX
service-role key, ONLY from:

- the sync function (`strathmark.sync`), and
- the rewritten `register_competitor()` (which mints rosters in MNEMEX)

The prediction hot path NEVER reaches MNEMEX. Cache reads stay in
STRATHMARK Supabase (or the local SQLite fallback).

Configuration
-------------
This module reads two env vars at call time (NOT at import time, so test
fixtures can monkey-patch):

    MNEMEX_SUPABASE_URL  -- MNEMEX project URL
    MNEMEX_SUPABASE_KEY  -- MNEMEX service-role key

When either is unset (the pre-MNEMEX transition state), every function in
this module degrades to a documented no-op rather than raising. Callers
gate writes on `is_mnemex_configured()` and route around MNEMEX-dependent
logic when it returns False.

This means the entire controlled-write architecture can ship and run in
production BEFORE MNEMEX exists. The sync function dry-runs, the rewritten
register_competitor returns a clear "MNEMEX not configured" error, and the
rest of STRATHMARK is unaffected.

Public API
----------
    is_mnemex_configured() -> bool
    pull_canonical_results(since=None, event_types=None, include_provisional=False)
        -> pd.DataFrame
    pull_canonical_competitors(since=None) -> pd.DataFrame
    register_competitor_in_mnemex(name, country, state, gender, region) -> dict
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable, Optional

import pandas as pd

_log = logging.getLogger(__name__)


# Chopping discipline filter applied at the source. MNEMEX may carry many
# disciplines (throwing, climbing, dendro, ...); STRATHMARK only consumes
# chopping. Adjust if MNEMEX uses different codes.
CHOPPING_DISCIPLINE_CODES: tuple[str, ...] = (
    "SB",  # Standing block
    "UH",  # Underhand
    "STR_SB",  # Variants — extend as MNEMEX vocabulary settles
    "STR_UH",
    "JACK_AND_JILL",
)


_client = None  # supabase.Client, created lazily; reset by reset_client()


def _get_client():
    """Return (and cache) the MNEMEX Supabase client.

    Raises RuntimeError if env vars are missing. Callers that need to handle
    the unconfigured case should use `is_mnemex_configured()` first.
    """
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("MNEMEX_SUPABASE_URL", "").strip()
    key = os.environ.get("MNEMEX_SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "MNEMEX is not configured. Set MNEMEX_SUPABASE_URL and "
            "MNEMEX_SUPABASE_KEY before calling MNEMEX functions, or check "
            "is_mnemex_configured() first to gate."
        )

    from supabase import create_client

    _client = create_client(url, key)
    return _client


def reset_client() -> None:
    """Test hook. Forget the cached client so env-var changes take effect."""
    global _client
    _client = None


def is_mnemex_configured() -> bool:
    """Return True iff MNEMEX_SUPABASE_URL and MNEMEX_SUPABASE_KEY are both set.

    Read at call time, not import time, so tests can monkey-patch the env.
    """
    url = os.environ.get("MNEMEX_SUPABASE_URL", "").strip()
    key = os.environ.get("MNEMEX_SUPABASE_KEY", "").strip()
    return bool(url) and bool(key)


# ---------------------------------------------------------------------------
# Reads (sync function consumers)
# ---------------------------------------------------------------------------


def pull_canonical_results(
    since: Optional[datetime] = None,
    event_types: Optional[Iterable[str]] = None,
    include_provisional: bool = False,
) -> pd.DataFrame:
    """Pull canonical chopping results from MNEMEX, filtered for STRATHMARK.

    Args:
        since:               Only return rows updated/inserted at or after this
                             datetime. None pulls everything (use sparingly).
        event_types:         Override the default chopping discipline filter.
                             Pass an explicit iterable to narrow further.
        include_provisional: If False (default), filter out provisional rows.
                             STRATHMARK's hydrated cache only holds canonical.

    Returns:
        DataFrame with MNEMEX-side columns. The sync function maps these to
        the STRATHMARK schema before upsert. Empty DataFrame when MNEMEX is
        unconfigured (no exception — see module docstring).

    The expected MNEMEX columns are documented in the MNEMEX repo. STRATHMARK
    relies on at minimum: mnemex_id, competitor_mnemex_id, event_type,
    time_seconds, size_mm, species_code, result_date, show_name, provisional,
    updated_at.
    """
    if not is_mnemex_configured():
        _log.info("pull_canonical_results: MNEMEX unconfigured, returning empty DataFrame")
        return pd.DataFrame()

    client = _get_client()
    types = tuple(event_types) if event_types is not None else CHOPPING_DISCIPLINE_CODES

    query = client.table("results").select("*").in_("event_type", list(types))
    if not include_provisional:
        query = query.eq("provisional", False)
    if since is not None:
        # Normalize to UTC ISO so MNEMEX's timestamptz comparisons are stable
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        query = query.gte("updated_at", since.isoformat())

    resp = query.execute()
    rows = resp.data or []
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def pull_canonical_competitors(since: Optional[datetime] = None) -> pd.DataFrame:
    """Pull canonical competitor roster from MNEMEX.

    Returns empty DataFrame when MNEMEX is unconfigured.

    The expected MNEMEX columns: mnemex_id, name, country, state_province,
    gender, region, updated_at.
    """
    if not is_mnemex_configured():
        _log.info("pull_canonical_competitors: MNEMEX unconfigured, returning empty DataFrame")
        return pd.DataFrame()

    client = _get_client()
    query = client.table("competitors").select("*")
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        query = query.gte("updated_at", since.isoformat())
    resp = query.execute()
    rows = resp.data or []
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ---------------------------------------------------------------------------
# Writes (register_competitor only)
# ---------------------------------------------------------------------------


def register_competitor_in_mnemex(
    name: str,
    country: str = "USA",
    state: str = "",
    gender: str = "",
    region: str = "",
) -> dict:
    """Mint a new competitor in MNEMEX.

    Returns:
        {'mnemex_id': str, 'status': 'created' | 'existing', 'name': str}

    Raises:
        ValueError:   If name is empty.
        RuntimeError: If MNEMEX is unconfigured. (No silent no-op on writes —
                      the operator MUST know that the registration didn't land.)
    """
    name_clean = (name or "").strip()
    if not name_clean:
        raise ValueError("register_competitor_in_mnemex: name must not be empty")

    if not is_mnemex_configured():
        raise RuntimeError(
            "MNEMEX is not configured; cannot register competitor. "
            "Set MNEMEX_SUPABASE_URL and MNEMEX_SUPABASE_KEY, or use the "
            "legacy STRATHMARK-local register_competitor() during the "
            "transition (deprecated; see strathmark.db.register_competitor)."
        )

    client = _get_client()

    # Deduplicate on case-insensitive name match. MNEMEX is the canonical
    # source so we trust its existing records.
    existing = (
        client.table("competitors").select("mnemex_id, name").ilike("name", name_clean).execute()
    )
    for row in existing.data or []:
        if str(row.get("name", "")).strip().lower() == name_clean.lower():
            return {
                "mnemex_id": str(row["mnemex_id"]),
                "status": "existing",
                "name": row.get("name", name_clean),
            }

    # MNEMEX-side ULID minting. We let MNEMEX assign the canonical ID by
    # passing nothing for mnemex_id; the MNEMEX schema is expected to default
    # it via a column DEFAULT (e.g. gen_ulid() if MNEMEX has that extension,
    # or a trigger). If MNEMEX requires the client to supply the ID, we mint
    # a ULID locally here. This branch handles both shapes.
    try:
        import ulid

        candidate_id = str(ulid.new())
    except ImportError:
        candidate_id = None

    record = {
        "name": name_clean,
        "country": country or None,
        "state_province": state or None,
        "gender": gender or None,
        "region": region or None,
    }
    if candidate_id is not None:
        record["mnemex_id"] = candidate_id

    resp = client.table("competitors").insert(record).execute()
    inserted = (resp.data or [{}])[0]
    new_id = inserted.get("mnemex_id") or candidate_id
    if not new_id:
        # MNEMEX neither defaulted nor returned the ID. This is a contract
        # violation worth surfacing rather than papering over.
        raise RuntimeError(
            "MNEMEX did not return a mnemex_id for the newly registered "
            "competitor. Check that the MNEMEX competitors table has either "
            "a default ID generator or returns the ID on INSERT."
        )

    return {"mnemex_id": str(new_id), "status": "created", "name": name_clean}
