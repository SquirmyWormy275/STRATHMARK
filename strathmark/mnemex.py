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
from datetime import datetime
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
    global _client, _sync_client
    _client = None
    _sync_client = None


# ---------------------------------------------------------------------------
# Dedicated mnemex_sync-role client for the sync function
# ---------------------------------------------------------------------------
#
# After migration 003 lands and the operator has rotated keys, the sync
# function MUST authenticate as the `mnemex_sync` Postgres role (not the
# generic STRATHMARK service-role key) so the controlled-write RLS policies
# enforce on it. The two are kept distinct: `_get_client()` is the standard
# MNEMEX read client; `_get_sync_client()` is the dedicated cache-write
# client used only by `strathmark.sync`.
#
# Env vars:
#   MNEMEX_SYNC_DB_URL  -- STRATHMARK Supabase URL (the cache target). Same
#                          as STRATHMARK_SUPABASE_URL today; kept distinct
#                          so the operator can move it without touching
#                          the read path.
#   MNEMEX_SYNC_DB_KEY  -- A JWT whose 'role' claim is mnemex_sync (NOT
#                          service_role).
#
# When either is unset, falls back to STRATHMARK_SUPABASE_URL/KEY with a
# warning. This lets the sync function ship and run before role plumbing
# is complete.

_sync_client = None  # supabase.Client for the mnemex_sync role


def _get_sync_client():
    """Return (and cache) the mnemex_sync-role Supabase client.

    Falls back to the STRATHMARK service-role client when the dedicated
    sync env vars are unset. Logs a one-line warning per process so the
    operator notices that the controlled-write boundary is open.
    """
    global _sync_client
    if _sync_client is not None:
        return _sync_client

    sync_url = os.environ.get("MNEMEX_SYNC_DB_URL", "").strip()
    sync_key = os.environ.get("MNEMEX_SYNC_DB_KEY", "").strip()

    if not sync_url or not sync_key:
        _log.warning(
            "MNEMEX_SYNC_DB_URL/KEY not configured; sync writes will use the "
            "generic STRATHMARK service-role client. After RLS migration 003 "
            "lands AND service_role has BYPASSRLS removed, this fallback "
            "stops working and the sync function will hard-fail at upsert. "
            "Provision a mnemex_sync-role JWT and set the env vars to close "
            "the loop."
        )
        from strathmark.db import _get_client

        _sync_client = _get_client()
        return _sync_client

    from supabase import create_client  # type: ignore[import]

    _sync_client = create_client(sync_url, sync_key)
    return _sync_client


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

    if since is not None and since.tzinfo is None:
        raise ValueError(
            "pull_canonical_results: `since` must be timezone-aware "
            "(use datetime.now(timezone.utc) or attach a tzinfo). "
            "Naive datetimes were previously reinterpreted as UTC, which "
            "produced silent timezone bugs; that behavior is now rejected."
        )

    client = _get_client()
    types = tuple(event_types) if event_types is not None else CHOPPING_DISCIPLINE_CODES

    base_query = client.table("results").select("*").in_("event_type", list(types))
    if not include_provisional:
        base_query = base_query.eq("provisional", False)
    if since is not None:
        base_query = base_query.gte("updated_at", since.isoformat())

    return _paginated_pull(base_query)


def pull_canonical_competitors(since: Optional[datetime] = None) -> pd.DataFrame:
    """Pull canonical competitor roster from MNEMEX.

    Returns empty DataFrame when MNEMEX is unconfigured.

    The expected MNEMEX columns: mnemex_id, name, country, state_province,
    gender, region, updated_at.
    """
    if not is_mnemex_configured():
        _log.info("pull_canonical_competitors: MNEMEX unconfigured, returning empty DataFrame")
        return pd.DataFrame()

    if since is not None and since.tzinfo is None:
        raise ValueError(
            "pull_canonical_competitors: `since` must be timezone-aware "
            "(use datetime.now(timezone.utc) or attach a tzinfo)."
        )

    client = _get_client()
    base_query = client.table("competitors").select("*")
    if since is not None:
        base_query = base_query.gte("updated_at", since.isoformat())
    return _paginated_pull(base_query)


_PAGE_SIZE = 1000


def _paginated_pull(base_query) -> pd.DataFrame:
    """Pull a PostgREST query in 1000-row pages until exhausted.

    PostgREST defaults to a 1000-row limit per response; without paging,
    pulls of populated tables silently truncate. We loop with .range() until
    a page returns fewer rows than the page size.
    """
    all_rows: list[dict] = []
    offset = 0
    while True:
        page = base_query.range(offset, offset + _PAGE_SIZE - 1).execute()
        rows = page.data or []
        all_rows.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()


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

    # Client-side ULID minting. We supply mnemex_id ourselves so insertion is
    # idempotent against MNEMEX schemas without a column DEFAULT and so the
    # caller can correlate without a round trip. ulid-py is a hard runtime
    # dependency since 0.5.0 — no try/except needed.
    import ulid

    candidate_id = str(ulid.new())

    record = {
        "mnemex_id": candidate_id,
        "name": name_clean,
        "country": country or None,
        "state_province": state or None,
        "gender": gender or None,
        "region": region or None,
    }

    resp = client.table("competitors").insert(record).execute()
    inserted = (resp.data or [{}])[0]
    new_id = inserted.get("mnemex_id") or candidate_id
    if not new_id:
        raise RuntimeError(
            "MNEMEX did not return a mnemex_id for the newly registered "
            "competitor and the client-supplied ULID was lost. Check the "
            "MNEMEX competitors table schema."
        )

    return {"mnemex_id": str(new_id), "status": "created", "name": name_clean}
