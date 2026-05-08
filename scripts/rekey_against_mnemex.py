"""
Re-key the 1311 existing STRATHMARK results against MNEMEX IDs
==============================================================

One-shot script. Run after MNEMEX is stood up and populated with the
canonical history. Idempotent — safe to re-run; only updates rows whose
mnemex_id is still NULL.

Strategy
--------
For each row in STRATHMARK results where mnemex_id IS NULL:
  1. Build a match key from (competitor_name, show_name, result_date,
     event, time_seconds, size_mm).
  2. Find the corresponding row in MNEMEX with the same key.
  3. If a unique match exists, populate STRATHMARK's mnemex_id and
     competitor_mnemex_id fields. Set source_type='legacy' (preserved)
     and last_synced_at=now().
  4. If zero matches, write to results_orphan with the row payload and
     a reason. The orphan row is preserved for manual review; the
     STRATHMARK row is left as-is.
  5. If multiple matches, also orphan with reason 'ambiguous_match' so
     a human picks the correct linkage.

Acceptance criteria from the migration plan:
  - >= 95% match rate
  - Orphans triaged before phase 4 (RLS enforcement) lands

Operations
----------
  python scripts/rekey_against_mnemex.py --dry-run   (default)
  python scripts/rekey_against_mnemex.py --commit
  python scripts/rekey_against_mnemex.py --commit --batch-size 50

Exit codes:
  0  -- success (clean, with or without orphans below the threshold)
  1  -- input/config error
  2  -- match rate below threshold; review orphans before re-running with --force
  3  -- MNEMEX unreachable
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

REQUIRED_MATCH_RATE = 0.95


def _competitor_name_lookup(client) -> Dict[str, str]:
    """Build {competitor_id -> competitor_name} from STRATHMARK competitors."""
    resp = client.table("competitors").select("competitor_id,name").execute()
    return {str(r["competitor_id"]): str(r["name"]) for r in resp.data or []}


def _mnemex_competitor_name_lookup() -> Dict[str, str]:
    """Build {name_lowercase -> mnemex_competitor_id} from MNEMEX."""
    from strathmark.mnemex import pull_canonical_competitors

    df = pull_canonical_competitors()
    if df.empty:
        return {}
    out: Dict[str, str] = {}
    for _, row in df.iterrows():
        name = str(row.get("name", "")).strip().lower()
        mid = str(row.get("mnemex_id", "")).strip()
        if name and mid:
            out[name] = mid
    return out


def _build_match_key(
    competitor_name: str,
    show_name: str,
    result_date: Optional[str],
    event: str,
    time_seconds: float,
    size_mm: int,
) -> Tuple:
    return (
        competitor_name.strip().lower(),
        show_name.strip().lower(),
        (result_date or "")[:10],
        str(event).strip().upper(),
        round(float(time_seconds), 2),
        int(size_mm),
    )


def _index_mnemex_results(mnemex_client) -> Dict[Tuple, List[dict]]:
    """Pre-index MNEMEX results by match key for O(1) lookup."""
    from strathmark.mnemex import CHOPPING_DISCIPLINE_CODES

    resp = (
        mnemex_client.table("results")
        .select(
            "mnemex_id, competitor_mnemex_id, event_type, time_seconds, "
            "size_mm, result_date, show_name, competitor_name"
        )
        .in_("event_type", list(CHOPPING_DISCIPLINE_CODES))
        .eq("provisional", False)
        .execute()
    )

    index: Dict[Tuple, List[dict]] = {}
    for row in resp.data or []:
        try:
            key = _build_match_key(
                competitor_name=str(row.get("competitor_name", "")),
                show_name=str(row.get("show_name", "")),
                result_date=row.get("result_date"),
                event=str(row.get("event_type", "")),
                time_seconds=float(row.get("time_seconds", 0)),
                size_mm=int(row.get("size_mm", 0)),
            )
        except (TypeError, ValueError):
            continue
        index.setdefault(key, []).append(row)
    return index


def _orphan_table_exists(client) -> bool:
    """results_orphan is created lazily on first use. Returns True if it's there."""
    try:
        client.table("results_orphan").select("orphan_id").limit(0).execute()
        return True
    except Exception:
        return False


def _ensure_orphan_table_warning_printed() -> None:
    print(
        "WARNING: results_orphan table does not exist. Orphans will be printed "
        "but not persisted. Create the table with the SQL block at the bottom "
        "of this script's docstring before re-running for actual orphan capture."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Apply updates to STRATHMARK results. Default is dry-run.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of STRATHMARK rows to update per Supabase call.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Apply updates even if match rate is below the 95%% threshold.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("STRATHMARK <- MNEMEX re-keying")
    print(f"Mode: {'COMMIT' if args.commit else 'DRY RUN'}")
    print("=" * 70)

    from strathmark.mnemex import is_mnemex_configured

    if not is_mnemex_configured():
        print("ERROR: MNEMEX is not configured. Set MNEMEX_SUPABASE_URL and MNEMEX_SUPABASE_KEY.")
        return 3

    from strathmark.db import _get_client
    from strathmark.mnemex import _get_client as _get_mnemex_client

    sm_client = _get_client()
    mn_client = _get_mnemex_client()

    print("Indexing MNEMEX canonical results...")
    mn_index = _index_mnemex_results(mn_client)
    print(f"  -> {len(mn_index)} unique match keys")

    print("Pulling un-keyed STRATHMARK rows...")
    resp = (
        sm_client.table("results")
        .select(
            "result_id, competitor_id, event, time_seconds, size_mm, "
            "result_date, show_name, mnemex_id"
        )
        .is_("mnemex_id", "null")
        .execute()
    )
    sm_rows = resp.data or []
    total = len(sm_rows)
    print(f"  -> {total} rows to re-key")
    if total == 0:
        print("Nothing to do.")
        return 0

    name_by_id = _competitor_name_lookup(sm_client)

    matched: List[Tuple[int, str, str]] = []  # (result_id, mnemex_id, competitor_mnemex_id)
    orphans: List[dict] = []
    reason_counter: Counter = Counter()

    for row in sm_rows:
        comp_name = name_by_id.get(str(row.get("competitor_id", "")), "")
        if not comp_name:
            orphans.append({**row, "_reason": "no_competitor_name"})
            reason_counter["no_competitor_name"] += 1
            continue
        try:
            key = _build_match_key(
                competitor_name=comp_name,
                show_name=str(row.get("show_name", "")),
                result_date=row.get("result_date"),
                event=str(row.get("event", "")),
                time_seconds=float(row.get("time_seconds", 0)),
                size_mm=int(row.get("size_mm", 0)),
            )
        except (TypeError, ValueError):
            orphans.append({**row, "_reason": "malformed_strathmark_row"})
            reason_counter["malformed_strathmark_row"] += 1
            continue

        candidates = mn_index.get(key, [])
        if len(candidates) == 1:
            mn = candidates[0]
            matched.append(
                (
                    int(row["result_id"]),
                    str(mn["mnemex_id"]),
                    str(mn.get("competitor_mnemex_id", "")),
                )
            )
        elif len(candidates) == 0:
            orphans.append({**row, "_reason": "no_match"})
            reason_counter["no_match"] += 1
        else:
            orphans.append({**row, "_reason": "ambiguous_match"})
            reason_counter["ambiguous_match"] += 1

    match_rate = len(matched) / total if total else 0.0
    print()
    print(f"Matched:  {len(matched)} / {total} ({match_rate:.1%})")
    print(f"Orphans:  {len(orphans)}")
    if reason_counter:
        for r, n in reason_counter.most_common():
            print(f"  {r:30s} {n}")

    if match_rate < REQUIRED_MATCH_RATE and args.commit and not args.force:
        print()
        print(
            f"FAIL: match rate {match_rate:.1%} below required "
            f"{REQUIRED_MATCH_RATE:.0%}. Triage orphans first, then re-run with "
            "--force to commit anyway."
        )
        return 2

    if args.commit and matched:
        print()
        print(f"Applying {len(matched)} updates in batches of {args.batch_size}...")
        now_iso = datetime.now(timezone.utc).isoformat()
        for i in range(0, len(matched), args.batch_size):
            batch = matched[i : i + args.batch_size]
            for result_id, mnemex_id, _comp_mid in batch:
                sm_client.table("results").update(
                    {
                        "mnemex_id": mnemex_id,
                        "last_synced_at": now_iso,
                        # source_type stays 'legacy'; rekeying does not change provenance
                    }
                ).eq("result_id", result_id).execute()
            print(f"  ... {min(i + args.batch_size, len(matched))}/{len(matched)}")

    if orphans:
        if args.commit and _orphan_table_exists(sm_client):
            print()
            print(f"Persisting {len(orphans)} orphans to results_orphan...")
            for orph in orphans:
                payload = {
                    "result_id_orig": orph["result_id"],
                    "reason": orph["_reason"],
                    "row_payload": json.dumps(
                        {k: v for k, v in orph.items() if not k.startswith("_")},
                        default=str,
                    ),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                sm_client.table("results_orphan").insert(payload).execute()
        else:
            if args.commit:
                _ensure_orphan_table_warning_printed()
            print()
            print(f"First {min(20, len(orphans))} orphans:")
            for orph in orphans[:20]:
                print(f"  result_id={orph['result_id']:>6} reason={orph['_reason']}")

    print()
    if not args.commit:
        print("DRY RUN complete. Re-run with --commit to apply.")
    else:
        print("Re-keying complete.")
    return 0


# ---------------------------------------------------------------------------
# Optional: results_orphan table (run once via Supabase SQL editor)
# ---------------------------------------------------------------------------
#
# CREATE TABLE IF NOT EXISTS results_orphan (
#     orphan_id       SERIAL       PRIMARY KEY,
#     result_id_orig  INTEGER      NOT NULL,
#     reason          TEXT         NOT NULL,
#     row_payload     JSONB        NOT NULL,
#     created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
# );


if __name__ == "__main__":
    sys.exit(main())
