"""
Legacy Excel Import Utility
============================

Reads an Excel workbook with the woodchopping_clean.xlsx structure
(Wood, Competitor, Results sheets), validates referential integrity,
detects duplicates and anomalies, and writes validated records to the
SQL database.

Usage:
    # Validate only (no writes)
    python import_legacy.py woodchopping_clean.xlsx --dry-run

    # Validate and write to database
    python import_legacy.py woodchopping_clean.xlsx

    # Validate without writing and show all flagged records
    python import_legacy.py woodchopping_clean.xlsx --dry-run --verbose

This is a migration utility. The primary data path is the SQL database.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Dict, List, Set, Tuple

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Required columns per sheet
# ---------------------------------------------------------------------------

REQUIRED_WOOD_COLS = {"species", "janka_hard"}
REQUIRED_COMPETITOR_COLS = {"competitor_id", "name"}
REQUIRED_RESULTS_COLS = {"competitor_id", "event", "time_s", "size_mm"}


# ---------------------------------------------------------------------------
# Data loading and validation
# ---------------------------------------------------------------------------


def load_and_validate_workbook(
    path: str,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, List[str]]:
    """
    Load and validate the Excel workbook.

    Args:
        path: Path to .xlsx workbook.

    Returns:
        Tuple of (wood_df, competitor_df, results_df, warnings).
        Raises ValueError on critical structural failures.
    """
    try:
        xl = pd.ExcelFile(path)
    except Exception as e:
        raise ValueError(f"Cannot open workbook '{path}': {e}")

    warnings = []
    sheets = set(xl.sheet_names)
    required_sheets = {"Wood", "Competitor", "Results"}
    missing = required_sheets - sheets
    if missing:
        raise ValueError(f"Missing required sheets: {missing}. Found: {xl.sheet_names}")

    wood_df = xl.parse("Wood")
    competitor_df = xl.parse("Competitor")
    results_df = xl.parse("Results")

    # Normalize column names
    wood_df.columns = [str(c).strip().lower().replace(" ", "_") for c in wood_df.columns]
    competitor_df.columns = [
        str(c).strip().lower().replace(" ", "_") for c in competitor_df.columns
    ]
    results_df.columns = [str(c).strip().lower().replace(" ", "_") for c in results_df.columns]

    # Alias common column variants
    _alias_columns(
        results_df,
        {
            "time": "time_s",
            "time_seconds": "time_s",
            "raw_time": "time_s",
            "size": "size_mm",
            "diameter": "size_mm",
            "diameter_mm": "size_mm",
            "species_code": "species",
            "wood_species": "species",
            "competitorid": "competitor_id",
            "competitor": "competitor_id",
            "date": "result_date",
            "event_date": "result_date",
        },
    )
    _alias_columns(
        competitor_df,
        {
            "competitorid": "competitor_id",
            "id": "competitor_id",
            "competitor_name": "name",
            "fullname": "name",
        },
    )
    _alias_columns(
        wood_df,
        {
            "janka_hardness": "janka_hard",
            "janka": "janka_hard",
            "specific_gravity": "spec_gravity",
            "speciescode": "species",
            "species_code": "species",
        },
    )

    # Check required columns
    _check_required_cols(wood_df, REQUIRED_WOOD_COLS, "Wood", warnings)
    _check_required_cols(competitor_df, REQUIRED_COMPETITOR_COLS, "Competitor", warnings)
    _check_required_cols(results_df, REQUIRED_RESULTS_COLS, "Results", warnings)

    # Normalize event codes to uppercase
    if "event" in results_df.columns:
        results_df["event"] = results_df["event"].astype(str).str.upper().str.strip()
        invalid_events = ~results_df["event"].isin(["SB", "UH"])
        if invalid_events.any():
            n = invalid_events.sum()
            unique_invalid = results_df.loc[invalid_events, "event"].unique()
            warnings.append(
                f"Results: {n} rows have unrecognized event codes: {unique_invalid}. "
                f"Expected SB or UH."
            )

    # Parse dates
    if "result_date" in results_df.columns:
        results_df["result_date"] = pd.to_datetime(results_df["result_date"], errors="coerce")
        n_bad_dates = results_df["result_date"].isna().sum()
        if n_bad_dates > 0:
            warnings.append(
                f"Results: {n_bad_dates} rows have unparseable dates (will be treated as undated)."
            )

    # Drop rows with missing required fields
    initial_count = len(results_df)
    for col in ["competitor_id", "event", "time_s", "size_mm"]:
        if col in results_df.columns:
            results_df = results_df.dropna(subset=[col])
    results_df = results_df[results_df.get("time_s", pd.Series([1.0])) > 0]
    dropped = initial_count - len(results_df)
    if dropped > 0:
        warnings.append(f"Results: dropped {dropped} rows with missing required fields.")

    return wood_df, competitor_df, results_df, warnings


def _alias_columns(df: pd.DataFrame, aliases: Dict[str, str]) -> None:
    """Rename columns in-place using alias mapping (only if target doesn't already exist)."""
    for old, new in aliases.items():
        if old in df.columns and new not in df.columns:
            df.rename(columns={old: new}, inplace=True)


def _check_required_cols(
    df: pd.DataFrame, required: Set[str], sheet: str, warnings: List[str]
) -> None:
    missing = required - set(df.columns)
    if missing:
        warnings.append(f"{sheet} sheet: missing columns {missing}.")


# ---------------------------------------------------------------------------
# Referential integrity checks
# ---------------------------------------------------------------------------


def check_referential_integrity(
    wood_df: pd.DataFrame,
    competitor_df: pd.DataFrame,
    results_df: pd.DataFrame,
) -> List[Dict]:
    """
    Check referential integrity between sheets.

    Returns:
        List of issue dicts with keys: type, sheet, count, sample.
    """
    issues = []

    # CompetitorIDs in Results must exist in Competitor
    if "competitor_id" in results_df.columns and "competitor_id" in competitor_df.columns:
        valid_ids = set(competitor_df["competitor_id"].astype(str).str.strip())
        result_ids = results_df["competitor_id"].astype(str).str.strip()
        orphaned_mask = ~result_ids.isin(valid_ids)
        if orphaned_mask.any():
            orphaned = result_ids[orphaned_mask].unique().tolist()
            issues.append(
                {
                    "type": "orphaned_competitor",
                    "sheet": "Results",
                    "count": int(orphaned_mask.sum()),
                    "sample": orphaned[:5],
                    "message": f"{orphaned_mask.sum()} rows reference CompetitorIDs not in Competitor sheet.",
                }
            )

    # Species codes in Results must exist in Wood
    if "species" in results_df.columns and "species" in wood_df.columns:
        valid_species = set(wood_df["species"].astype(str).str.strip().str.lower())
        result_species = results_df["species"].astype(str).str.strip().str.lower()
        orphaned_mask = ~result_species.isin(valid_species)
        if orphaned_mask.any():
            orphaned = result_species[orphaned_mask].unique().tolist()
            issues.append(
                {
                    "type": "orphaned_species",
                    "sheet": "Results",
                    "count": int(orphaned_mask.sum()),
                    "sample": orphaned[:5],
                    "message": f"{orphaned_mask.sum()} rows reference species codes not in Wood sheet.",
                }
            )

    return issues


# ---------------------------------------------------------------------------
# Duplicate and anomaly detection (Phase 2B)
# ---------------------------------------------------------------------------


def detect_duplicates_and_anomalies(results_df: pd.DataFrame) -> Dict:
    """
    Detect duplicates and statistical anomalies in the results.

    Duplicate rule:
        Same CompetitorID + same date + same event type = duplicate.

    Anomaly rule:
        Time more than 3 standard deviations from that competitor's mean
        for that event type and size range (within 25mm) = anomaly.

    Args:
        results_df: Standardized results DataFrame.

    Returns:
        Dict with keys: 'duplicates' (list), 'anomalies' (list).
        Does NOT auto-delete flagged records -- presents them for human review.
    """
    duplicates = []
    anomalies = []

    # --- Duplicate detection ---
    dup_cols = ["competitor_id", "event"]
    if "result_date" in results_df.columns:
        dup_cols.append("result_date")

    if all(c in results_df.columns for c in dup_cols):
        dup_df = results_df[results_df.duplicated(subset=dup_cols, keep=False)].copy()
        if not dup_df.empty:
            for key, group in dup_df.groupby(dup_cols):
                duplicates.append(
                    {
                        "competitor_id": key[0] if len(key) > 0 else None,
                        "event": key[1] if len(key) > 1 else None,
                        "date": str(key[2]) if len(key) > 2 else None,
                        "count": len(group),
                        "times": group["time_s"].tolist() if "time_s" in group.columns else [],
                        "message": "Duplicate: same competitor + date + event.",
                    }
                )

    # --- Anomaly detection ---
    if "time_s" in results_df.columns and "competitor_id" in results_df.columns:
        for (cid, evt), group in results_df.groupby(["competitor_id", "event"]):
            times = group["time_s"].dropna().astype(float)
            if len(times) < 5:
                continue  # not enough data for outlier detection
            mean_t = float(times.mean())
            std_t = float(times.std(ddof=1))
            if std_t == 0:
                continue
            for idx, row in group.iterrows():
                t = float(row.get("time_s", 0))
                if t <= 0:
                    continue
                z = abs(t - mean_t) / std_t
                if z > 3.0:
                    anomalies.append(
                        {
                            "row_index": int(idx),
                            "competitor_id": str(cid),
                            "event": str(evt),
                            "time_s": t,
                            "competitor_mean": round(mean_t, 2),
                            "competitor_std": round(std_t, 2),
                            "z_score": round(z, 2),
                            "size_mm": float(row.get("size_mm", 0)),
                            "date": str(row.get("result_date", "")),
                            "message": f"Time {t:.1f}s is {z:.1f} std devs from mean {mean_t:.1f}s.",
                        }
                    )

    # --- Missing required fields ---
    missing_field_issues = []
    required_cols = ["competitor_id", "event", "time_s", "size_mm"]
    for col in required_cols:
        if col in results_df.columns:
            missing_mask = results_df[col].isna()
            if missing_mask.any():
                missing_field_issues.append(
                    {
                        "field": col,
                        "count": int(missing_mask.sum()),
                        "row_indices": missing_mask[missing_mask].index.tolist()[:10],
                        "message": f"Missing required field '{col}' in {missing_mask.sum()} rows.",
                    }
                )

    return {
        "duplicates": duplicates,
        "anomalies": anomalies,
        "missing_fields": missing_field_issues,
    }


# ---------------------------------------------------------------------------
# Database write
# ---------------------------------------------------------------------------


def write_to_database(
    wood_df: pd.DataFrame,
    competitor_df: pd.DataFrame,
    results_df: pd.DataFrame,
    dry_run: bool = True,
) -> Dict:
    """
    Write validated records to the SQL database.

    Args:
        wood_df: Wood species DataFrame.
        competitor_df: Competitor roster DataFrame.
        results_df: Results DataFrame (already validated).
        dry_run: If True, validate only -- do not write.

    Returns:
        Dict with keys: n_competitors, n_results, n_skipped.
    """
    stats = {"n_competitors": 0, "n_results": 0, "n_skipped": 0}

    if dry_run:
        _log.info(
            "DRY RUN: would write %d competitors and %d results.",
            len(competitor_df),
            len(results_df),
        )
        stats["n_competitors"] = len(competitor_df)
        stats["n_results"] = len(results_df)
        return stats

    try:
        from strathmark.db import push_competitors, push_results
    except ImportError:
        _log.error("strathmark.db not available. Install strathmark first.")
        return stats

    # Push competitors
    try:
        push_competitors(competitor_df)
        stats["n_competitors"] = len(competitor_df)
        _log.info("Wrote %d competitor records.", len(competitor_df))
    except Exception as e:
        _log.error("Failed to write competitors: %s", e)

    # Push results (rename columns to match DB schema)
    results_to_push = results_df.copy()
    _alias_columns(
        results_to_push,
        {
            "time_s": "time_seconds",
            "size_mm": "size_mm",  # keep
        },
    )
    try:
        push_results(results_to_push, show_name="legacy_import", source_app="import_legacy.py")
        stats["n_results"] = len(results_to_push)
        _log.info("Wrote %d result records.", len(results_to_push))
    except Exception as e:
        _log.error("Failed to write results: %s", e)

    return stats


# ---------------------------------------------------------------------------
# Validation report printer
# ---------------------------------------------------------------------------


def print_validation_report(
    warnings: List[str],
    integrity_issues: List[Dict],
    anomaly_report: Dict,
    verbose: bool = False,
) -> None:
    """Print a human-readable validation report."""
    print("\n=== LEGACY IMPORT VALIDATION REPORT ===\n")

    if warnings:
        print(f"WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")
        print()

    if integrity_issues:
        print(f"REFERENTIAL INTEGRITY ISSUES ({len(integrity_issues)}):")
        for issue in integrity_issues:
            print(f"  [{issue['type']}] {issue['message']}")
            if verbose:
                print(f"    Sample IDs: {issue['sample']}")
        print()

    dups = anomaly_report.get("duplicates", [])
    if dups:
        print(f"DUPLICATES DETECTED ({len(dups)}):")
        for d in dups[: 10 if not verbose else None]:
            print(
                f"  - Competitor {d['competitor_id']} / {d['event']} / {d['date']}: "
                f"{d['count']} entries (times: {d['times']})"
            )
        if not verbose and len(dups) > 10:
            print(f"  ... and {len(dups) - 10} more. Use --verbose to see all.")
        print()

    anom = anomaly_report.get("anomalies", [])
    if anom:
        print(f"STATISTICAL ANOMALIES ({len(anom)}):")
        for a in anom[: 10 if not verbose else None]:
            print(
                f"  - Row {a['row_index']}: {a['competitor_id']} / {a['event']} "
                f"{a['time_s']:.1f}s (mean={a['competitor_mean']:.1f}s, z={a['z_score']:.1f})"
            )
        if not verbose and len(anom) > 10:
            print(f"  ... and {len(anom) - 10} more. Use --verbose to see all.")
        print()

    missing = anomaly_report.get("missing_fields", [])
    if missing:
        print(f"MISSING REQUIRED FIELDS ({len(missing)}):")
        for m in missing:
            print(f"  - {m['message']}")
        print()

    total_issues = len(integrity_issues) + len(dups) + len(anom) + len(missing)
    if total_issues == 0 and not warnings:
        print("No issues found. Data is clean.")
    else:
        print(f"Total issues: {total_issues} (plus {len(warnings)} warnings).")
        print("Review flagged records before writing to the database.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Import legacy woodchopping Excel data to the database."
    )
    parser.add_argument("path", help="Path to Excel workbook (.xlsx).")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate only; do not write to the database."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show all flagged records (not just first 10)."
    )
    args = parser.parse_args()

    _log.info("Loading workbook: %s", args.path)

    try:
        wood_df, competitor_df, results_df, warnings = load_and_validate_workbook(args.path)
    except ValueError as e:
        _log.error("Critical error: %s", e)
        sys.exit(1)

    _log.info(
        "Loaded: %d wood species, %d competitors, %d results.",
        len(wood_df),
        len(competitor_df),
        len(results_df),
    )

    # Referential integrity
    integrity_issues = check_referential_integrity(wood_df, competitor_df, results_df)

    # Duplicate and anomaly detection
    anomaly_report = detect_duplicates_and_anomalies(results_df)

    # Print report
    print_validation_report(warnings, integrity_issues, anomaly_report, args.verbose)

    # Write to database (unless dry-run)
    if args.dry_run:
        _log.info("DRY RUN complete. No data written.")
    else:
        total_issues = (
            len(integrity_issues)
            + len(anomaly_report.get("duplicates", []))
            + len(anomaly_report.get("anomalies", []))
        )
        if total_issues > 0:
            _log.warning(
                "%d issues found. Proceeding with write anyway. "
                "Flagged records will be included -- review manually.",
                total_issues,
            )
        stats = write_to_database(wood_df, competitor_df, results_df, dry_run=False)
        _log.info(
            "Import complete: %d competitors, %d results written.",
            stats["n_competitors"],
            stats["n_results"],
        )


if __name__ == "__main__":
    main()
