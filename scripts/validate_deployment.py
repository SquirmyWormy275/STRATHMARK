"""
STRATHMARK Pre-Event Deployment Validation
==========================================

Run this script the morning of an event to confirm every subsystem is ready.

Usage:
    python scripts/validate_deployment.py
    python scripts/validate_deployment.py --write    # actually call push_results_dicts (NOT a dry run)

By default the script is read-only. It does NOT modify Supabase, the local
SQLite store, or any other persistent state. Use --write only if you want
the result-ingestion check to actually insert a row.

Checks (each prints OK / FAIL / SKIPPED):
    1. Supabase connectivity (competitors, results, wood_species counts)
    2. Prediction cascade WITHOUT Ollama (3 known competitors)
    3. Prediction cascade WITH Ollama (if running)
    4. Full mark sheet generation (3 competitors)
    5. Result ingestion dry-run (validates push_results_dicts() format)

Output is plain text only -- no emojis, no ANSI codes -- so it can be
piped to a log file on the event laptop without garbled characters.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from typing import List, Optional

# Tracks final pass/fail per check for the summary block
_status: dict = {
    "supabase": "FAIL",
    "predictions_base": "FAIL",
    "predictions_llm": "SKIPPED",
    "mark_sheet": "FAIL",
    "result_ingestion": "FAIL",
}


def _hr(title: str = "") -> None:
    print()
    print("-" * 60)
    if title:
        print(title)
        print("-" * 60)


# ---------------------------------------------------------------------------
# Check 3A -- Supabase connectivity
# ---------------------------------------------------------------------------


def check_supabase() -> Optional[List]:
    """Returns a list of CompetitorRecord objects (3 picks) on success, None on failure."""
    _hr("3A. Supabase connectivity")
    try:
        from strathmark.db import pull_competitors, pull_results
    except Exception as exc:
        print(f"FAIL: cannot import db module: {exc}")
        return None

    try:
        comps = pull_competitors()
        results = pull_results()
    except Exception as exc:
        print(f"FAIL: Supabase pull failed: {exc}")
        print("      Check STRATHMARK_SUPABASE_URL and STRATHMARK_SUPABASE_KEY env vars.")
        return None

    n_comp = len(comps)
    n_res = len(results)

    # wood_species
    n_species = 0
    try:
        from strathmark.db import _get_client

        sp_resp = _get_client().table("wood_species").select("species_id").execute()
        n_species = len(sp_resp.data or [])
    except Exception as exc:
        print(f"WARN: wood_species fetch failed (non-fatal): {exc}")

    print(f"Supabase: OK -- {n_comp} competitors, {n_res} results, {n_species} species")
    _status["supabase"] = "OK"

    # Pick 3 competitors with the most history for downstream checks
    if results.empty or comps.empty:
        print("WARN: cannot pick test competitors -- no history available")
        return []

    counts = results.groupby("competitor_id").size().sort_values(ascending=False)
    picks = list(counts.head(3).index)
    print(f"Test competitors selected: {picks}")
    return _build_test_records(picks, comps, results)


def _build_test_records(comp_ids: List[str], comps_df, results_df) -> List:
    from strathmark.predictor import CompetitorRecord, HistoricalResult

    records = []
    for cid in comp_ids:
        name_row = comps_df[comps_df["CompetitorID"] == cid]
        name = name_row.iloc[0]["Name"] if not name_row.empty else cid

        rows = results_df[results_df["competitor_id"] == cid]
        history = []
        for _, r in rows.iterrows():
            try:
                ev = str(r.get("Event", "")).strip().upper()
                if ev not in ("SB", "UH"):
                    continue
                d = r.get("Date (optional)")
                rd = None
                if d:
                    try:
                        rd = date.fromisoformat(str(d)[:10])
                    except (ValueError, TypeError):
                        rd = None
                history.append(
                    HistoricalResult(
                        event_code=ev,
                        time_seconds=float(r["Time (seconds)"]),
                        species=str(r.get("Species Code", "S05") or "S05"),
                        diameter_mm=float(r.get("Size (mm)") or 300),
                        quality=5,
                        result_date=rd,
                    )
                )
            except Exception:
                continue
        records.append(CompetitorRecord(name=str(name), history=history, division="Open"))
    return records


# ---------------------------------------------------------------------------
# Check 3B -- Prediction cascade (no LLM)
# ---------------------------------------------------------------------------


def check_predictions_base(records: List) -> None:
    _hr("3B. Prediction cascade (no LLM)")
    if not records:
        print("FAIL: no test competitors available")
        return

    from strathmark.predictor import WoodProfile, get_best_prediction

    wood = WoodProfile(species="S05", diameter_mm=275, quality=5)
    all_ok = True
    for rec in records:
        try:
            pred = get_best_prediction(rec, wood, "SB")
        except Exception as exc:
            print(f"FAIL: {rec.name}: cascade raised {exc}")
            all_ok = False
            continue
        if pred is None or pred.value is None:
            print(f"FAIL: {rec.name}: cascade returned None")
            all_ok = False
            continue
        in_range = 5.0 <= pred.value <= 120.0
        flag = "OK" if in_range else "WARN"
        print(f"{flag}: {rec.name}: {pred.value:.2f}s via {pred.method} ({pred.confidence})")
        if not in_range:
            all_ok = False

    if all_ok:
        _status["predictions_base"] = "OK"
        print("Prediction cascade (no LLM): OK")
    else:
        print("Prediction cascade (no LLM): FAIL")


# ---------------------------------------------------------------------------
# Check 3C -- Prediction cascade with Ollama
# ---------------------------------------------------------------------------


def check_predictions_llm(records: List) -> None:
    _hr("3C. Prediction cascade (with Ollama)")
    from strathmark.llm import check_ollama_connection

    if not check_ollama_connection(force=True):
        print("Ollama: NOT RUNNING -- cascade will skip LLM level (this is fine)")
        _status["predictions_llm"] = "SKIPPED"
        return
    print("Ollama: reachable on localhost:11434")

    if not records:
        print("FAIL: no test competitors available")
        _status["predictions_llm"] = "FAIL"
        return

    from strathmark.config import llm_config
    from strathmark.predictor import WoodProfile, get_best_prediction

    wood = WoodProfile(species="S05", diameter_mm=275, quality=5)
    llm_client = {
        "url": "http://localhost:11434",
        "model": llm_config.PREDICTION_MODEL,
        "timeout": llm_config.TIMEOUT_SECONDS,
    }
    any_llm_used = False
    for rec in records:
        try:
            pred = get_best_prediction(rec, wood, "SB", llm_client=llm_client)
        except Exception as exc:
            print(f"FAIL: {rec.name}: LLM cascade raised {exc}")
            _status["predictions_llm"] = "FAIL"
            return
        method = pred.method if pred else "none"
        val = pred.value if pred else float("nan")
        print(f"OK: {rec.name}: {val:.2f}s via {method}")
        if method == "llm":
            any_llm_used = True

    if any_llm_used:
        print(f"Ollama LLM: OK -- {llm_client['model']} responding")
        _status["predictions_llm"] = "OK"
    else:
        print("WARN: Ollama reachable but no row chose method=llm (model may have errored)")
        _status["predictions_llm"] = "SKIPPED"


# ---------------------------------------------------------------------------
# Check 3D -- Mark sheet
# ---------------------------------------------------------------------------


def check_mark_sheet(records: List) -> None:
    _hr("3D. Full mark sheet generation")
    if not records or len(records) < 2:
        print("FAIL: need at least 2 competitors for a mark sheet")
        return

    from strathmark.calculator import HandicapCalculator
    from strathmark.predictor import WoodProfile

    wood = WoodProfile(species="S05", diameter_mm=275, quality=5)
    calc = HandicapCalculator()
    try:
        results = calc.calculate(records, wood, "SB")
        sheet = calc.build_start_sheet(results, "275mm SB", "SB", wood)
    except Exception as exc:
        print(f"FAIL: calculate() raised {exc}")
        return

    floor_ok = all(3 <= r.mark <= 183 for r in results)
    front_marker = max(results, key=lambda r: r.predicted_time)
    front_ok = front_marker.mark == 3
    sorted_by_time = sorted(results, key=lambda r: -r.predicted_time)
    monotonic_ok = all(
        sorted_by_time[i].mark <= sorted_by_time[i + 1].mark for i in range(len(sorted_by_time) - 1)
    )

    print(sheet.render())
    print()
    print(f"Floor/ceiling [3,183]: {'OK' if floor_ok else 'FAIL'}")
    print(f"Front marker has mark 3: {'OK' if front_ok else 'FAIL'}")
    print(f"Mark ordering monotonic: {'OK' if monotonic_ok else 'FAIL'}")
    if floor_ok and front_ok and monotonic_ok:
        _status["mark_sheet"] = "OK"


# ---------------------------------------------------------------------------
# Check 3E -- Result ingestion (dry run)
# ---------------------------------------------------------------------------


def check_result_ingestion(records: List, write: bool) -> None:
    _hr("3E. Result ingestion (dry run)" + ("" if not write else " [LIVE WRITE]"))
    if not records:
        print("FAIL: no test competitors available")
        return

    from strathmark.db import push_results_dicts

    fake_row = {
        "competitor_id": getattr(records[0], "competitor_id", None) or "C0001",
        "event_code": "SB",
        "time_seconds": 25.0,
        "size_mm": 275,
        "species_code": "S05",
        "date": date.today().isoformat(),
        "notes": "validation script test row -- DO NOT keep",
    }

    try:
        result = push_results_dicts(
            [fake_row],
            source="validate_deployment",
            show_name="VALIDATION",
            dry_run=not write,
        )
    except Exception as exc:
        print(f"FAIL: push_results_dicts raised {exc}")
        return

    print(
        f"inserted={result['inserted']} skipped={result['skipped']} errors={len(result['errors'])}"
    )
    for line in result["errors"]:
        print(f"  {line}")

    # Acceptable: validation passed even if competitor_id was unknown
    fatal = any(
        "is not a number" in e or "outside" in e or "missing required" in e
        for e in result["errors"]
    )
    if not fatal:
        _status["result_ingestion"] = "OK"
        print("Result ingestion: format OK")
    else:
        print("Result ingestion: validation failed")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary() -> int:
    print()
    print("=" * 60)
    print("STRATHMARK PRE-EVENT VALIDATION")
    print("=" * 60)
    for label, key in [
        ("Supabase:          ", "supabase"),
        ("Predictions (base):", "predictions_base"),
        ("Predictions (LLM): ", "predictions_llm"),
        ("Mark sheet:        ", "mark_sheet"),
        ("Result ingestion:  ", "result_ingestion"),
    ]:
        print(f"{label} [{_status[key]}]")
    print("=" * 60)

    critical = ["supabase", "predictions_base", "mark_sheet", "result_ingestion"]
    ready = all(_status[k] == "OK" for k in critical)
    print(f"READY FOR DEPLOYMENT: [{'YES' if ready else 'NO'}]")
    print("=" * 60)

    if not ready:
        print()
        print("Failure remediation:")
        if _status["supabase"] != "OK":
            print("  - Verify STRATHMARK_SUPABASE_URL and STRATHMARK_SUPABASE_KEY env vars")
            print(
                '  - Test connectivity: python -c "from strathmark.db import pull_competitors; print(len(pull_competitors()))"'
            )
        if _status["predictions_base"] != "OK":
            print("  - Inspect get_best_prediction() output for the picked competitors")
        if _status["mark_sheet"] != "OK":
            print("  - Run pytest tests/test_calculator.py to confirm mark logic invariants")
        if _status["result_ingestion"] != "OK":
            print("  - Inspect the validation errors above and re-run with --write to confirm")
    return 0 if ready else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="STRATHMARK pre-event validation")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually write the validation row to Supabase (default: dry-run only)",
    )
    args = parser.parse_args()

    print("STRATHMARK PRE-EVENT VALIDATION")
    print(f"Mode: {'LIVE WRITE' if args.write else 'READ-ONLY (default)'}")

    records = check_supabase() or []
    check_predictions_base(records)
    check_predictions_llm(records)
    check_mark_sheet(records)
    check_result_ingestion(records, write=args.write)

    return print_summary()


if __name__ == "__main__":
    sys.exit(main())
