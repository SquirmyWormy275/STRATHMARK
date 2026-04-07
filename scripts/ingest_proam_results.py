"""
Ingest Pro-Am Manager Results into STRATHMARK Supabase
======================================================

Reads a JSON or CSV export from the Missoula-Pro-Am-Manager and pushes
the results into the STRATHMARK Supabase results table.

Usage
-----
    python scripts/ingest_proam_results.py --input results.csv
    python scripts/ingest_proam_results.py --input results.json --show "Missoula Pro-Am 2026"
    python scripts/ingest_proam_results.py --input results.csv --dry-run

Pro-Am Manager export format (CSV columns or JSON keys):
    competitor_name, event_name, time, species, date, [notes]

Behaviour:
    - Looks up competitor_id by name in Supabase. Names with no match are
      reported and the script prompts for either manual ID entry or skip.
    - Parses event_name to extract event_code (SB/UH) and diameter.
    - Validates every row before insert; rejected rows are reported, never
      silently dropped.
    - Default mode is DRY RUN until --commit is passed. Inserts only happen
      with --commit.
    - Output is plain text. No emojis, no ANSI codes.

Exit codes:
    0  -- success (with or without skipped rows)
    1  -- input file missing or unreadable
    2  -- Supabase unreachable
    3  -- one or more rows could not be ingested even after manual mapping
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List


def _load_input(path: Path) -> List[dict]:
    if not path.exists():
        print(f"ERROR: input file not found: {path}")
        sys.exit(1)
    suffix = path.suffix.lower()
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            print("ERROR: JSON input must be a top-level list of result objects")
            sys.exit(1)
        return data
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            return list(reader)
    print(f"ERROR: unsupported input extension: {suffix} (use .csv or .json)")
    sys.exit(1)


def _build_competitor_lookup() -> Dict[str, str]:
    """Fetch all competitors from Supabase and return name -> competitor_id."""
    from strathmark.db import pull_competitors

    df = pull_competitors()
    if df.empty:
        return {}
    lookup: Dict[str, str] = {}
    for _, row in df.iterrows():
        name = str(row.get("Name", "")).strip()
        cid = str(row.get("CompetitorID", "")).strip()
        if name and cid:
            lookup[name] = cid
            lookup[name.lower()] = cid
    return lookup


def _resolve_unmapped(rows: List[dict], interactive: bool) -> List[dict]:
    """
    For rows where competitor_id is None, prompt the user (if interactive)
    to either supply a competitor_id manually or skip. Returns the rows
    that have competitor_id set; unresolved rows are removed.
    """
    resolved: List[dict] = []
    for row in rows:
        if row.get("competitor_id"):
            resolved.append(row)
            continue
        name = row.get("_competitor_name", "<unknown>")
        if not interactive:
            print(f"  SKIP unmapped competitor: {name}")
            continue
        print(f"\nUnmapped competitor: {name}")
        print("  Enter existing competitor_id, or 'r' to register new, or 's' to skip:")
        choice = input("  > ").strip()
        if choice.lower() == "s" or not choice:
            print(f"  SKIPPED: {name}")
            continue
        if choice.lower() == "r":
            from strathmark.db import register_competitor

            try:
                result = register_competitor(name=name)
                print(f"  REGISTERED: {name} -> {result['competitor_id']} ({result['status']})")
                row["competitor_id"] = result["competitor_id"]
                resolved.append(row)
            except Exception as exc:
                print(f"  ERROR registering {name}: {exc}")
            continue
        row["competitor_id"] = choice
        resolved.append(row)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest Pro-Am Manager results into STRATHMARK Supabase"
    )
    parser.add_argument("--input", required=True, help="Path to results.csv or results.json")
    parser.add_argument("--show", default="Missoula Pro-Am 2026", help="Show/tournament name")
    parser.add_argument(
        "--source", default="pro-am-manager", help="Source app identifier for sync_log"
    )
    parser.add_argument(
        "--commit", action="store_true", help="Actually write to Supabase (default is dry-run)"
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip unmapped competitors instead of prompting",
    )
    parser.add_argument(
        "--default-date", default=None, help="ISO date used when a row has no 'date'"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("STRATHMARK -- Pro-Am Result Ingestion")
    print("=" * 60)
    print(f"Input:  {args.input}")
    print(f"Show:   {args.show}")
    print(f"Mode:   {'COMMIT (writes to Supabase)' if args.commit else 'DRY RUN (no writes)'}")
    print("-" * 60)

    raw = _load_input(Path(args.input))
    print(f"Loaded {len(raw)} raw rows from input file")

    try:
        lookup = _build_competitor_lookup()
    except Exception as exc:
        print(f"ERROR: cannot reach Supabase to build competitor lookup: {exc}")
        print("Check STRATHMARK_SUPABASE_URL and STRATHMARK_SUPABASE_KEY env vars.")
        return 2
    print(f"Loaded {len(lookup) // 2} known competitors from Supabase")

    from strathmark.db import format_proam_results, push_results_dicts

    formatted = format_proam_results(raw, competitor_lookup=lookup, default_date=args.default_date)

    unmapped = [r for r in formatted if not r.get("competitor_id")]
    mapped = [r for r in formatted if r.get("competitor_id")]
    print(f"Mapped:   {len(mapped)} rows")
    print(f"Unmapped: {len(unmapped)} rows")

    if unmapped:
        print("\nResolving unmapped competitors...")
        resolved_unmapped = _resolve_unmapped(unmapped, interactive=not args.non_interactive)
        mapped.extend(resolved_unmapped)

    # Strip the passthrough _competitor_name key before pushing
    payload = [{k: v for k, v in r.items() if not k.startswith("_")} for r in mapped]

    print(f"\nValidating and pushing {len(payload)} rows...")
    try:
        result = push_results_dicts(
            payload,
            source=args.source,
            show_name=args.show,
            dry_run=not args.commit,
        )
    except Exception as exc:
        print(f"ERROR: push failed: {exc}")
        return 2

    print("-" * 60)
    print(f"Inserted: {result['inserted']}")
    print(f"Skipped:  {result['skipped']} (duplicates)")
    print(f"Errors:   {len(result['errors'])}")
    if result["errors"]:
        print("\nFirst 20 errors:")
        for line in result["errors"][:20]:
            print(f"  {line}")
        if len(result["errors"]) > 20:
            print(f"  ... and {len(result['errors']) - 20} more")
    print("=" * 60)

    if not args.commit:
        print("DRY RUN complete. Re-run with --commit to actually write.")

    if result["errors"] and not args.commit:
        return 0  # dry-run errors are informational
    if result["errors"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
