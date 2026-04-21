"""
TODO-001 baseline MAE measurement.

Runs backtest_predictions() with leave-one-out methodology against the
current production cascade. Produces MAE, RMSE, bias, and within-3s-pct
broken out by event (SB / UH) and confidence tier.

Usage:
    python scripts/measure_baseline_mae.py
    python scripts/measure_baseline_mae.py --event SB --min-results 3
    python scripts/measure_baseline_mae.py --output baseline_mae_2026-04-15.json

Requirements:
    STRATHMARK_SUPABASE_URL and STRATHMARK_SUPABASE_KEY set in the environment.
    Trained ML model at models/xgb_<event>.json (optional; falls back to
    Baseline-only cascade when missing).

Output goes to stdout and, if --output is given, to a JSON file for trend
tracking across runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from strathmark.analytics import backtest_predictions
from strathmark.db import pull_results
from strathmark.predictor import CompetitorRecord, HistoricalResult, WoodProfile


def build_records(df: pd.DataFrame, min_results: int) -> List[CompetitorRecord]:
    """One CompetitorRecord per competitor with >= min_results historical rows."""
    records: List[CompetitorRecord] = []
    for name, group in df.groupby("competitor_name"):
        if len(group) < min_results:
            continue
        history = [
            HistoricalResult(
                event_code=str(row.get("event", "SB")).upper(),
                time_seconds=float(row["time_seconds"]),
                species=str(row.get("species_code", "S01")),
                diameter_mm=float(row.get("size_mm", 300)),
                quality=int(row.get("quality", 3)),
                result_date=row.get("date"),
            )
            for _, row in group.iterrows()
            if pd.notna(row.get("time_seconds"))
        ]
        records.append(CompetitorRecord(name=str(name), history=history))
    return records


def leave_one_out_backtest(
    records: List[CompetitorRecord],
    wood: WoodProfile,
    event_code: str,
    results_df: pd.DataFrame,
) -> Dict:
    """
    Leave-one-out: for each competitor with >=3 results, hide their most
    recent result, predict it from the remainder, and measure error.
    """
    actuals: Dict[str, float] = {}
    test_records: List[CompetitorRecord] = []
    for rec in records:
        event_history = [h for h in rec.history if h.event_code == event_code]
        if len(event_history) < 3:
            continue
        event_history_sorted = sorted(event_history, key=lambda h: h.result_date or date.min)
        held_out = event_history_sorted[-1]
        training_history = [h for h in rec.history if h is not held_out]
        test_records.append(CompetitorRecord(name=rec.name, history=training_history))
        actuals[rec.name] = held_out.time_seconds

    return backtest_predictions(
        competitors=test_records,
        wood=wood,
        event_code=event_code,
        actuals=actuals,
        results_df=results_df,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", choices=["SB", "UH", "both"], default="both")
    parser.add_argument("--min-results", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    print("[INFO] Pulling historical results from Supabase...")
    try:
        df = pull_results()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    if df.empty:
        print("[FAIL] No historical results returned from Supabase.", file=sys.stderr)
        return 3

    print(f"[INFO] {len(df)} rows, {df['competitor_name'].nunique()} competitors")

    default_wood = WoodProfile(species="S01", diameter_mm=300, quality=5)

    events = ["SB", "UH"] if args.event == "both" else [args.event]
    summary: Dict[str, Optional[Dict]] = {}

    for evt in events:
        print(f"\n[INFO] Backtesting {evt}...")
        records = build_records(df[df.get("event", "SB").str.upper() == evt], args.min_results)
        if not records:
            print(f"[WARN] {evt}: no competitors meet min-results >= {args.min_results}")
            summary[evt] = None
            continue
        result = leave_one_out_backtest(records, default_wood, evt, df)
        summary[evt] = {
            "n": len(result["results"]),
            "mae": result["mae"],
            "rmse": result["rmse"],
            "bias": result["bias"],
            "within_3s_pct": result["within_3s_pct"],
        }
        print(
            f"[OK]   {evt}: n={len(result['results'])}, "
            f"MAE={result['mae']:.2f}s, RMSE={result['rmse']:.2f}s, "
            f"bias={result['bias']:+.2f}s, within_3s={result['within_3s_pct']:.1f}%"
        )

    print("\n--- BASELINE MAE SUMMARY ---")
    for evt, s in summary.items():
        if s is None:
            print(f"{evt}: no data")
        else:
            print(
                f"{evt}: MAE={s['mae']:.2f}s  RMSE={s['rmse']:.2f}s  "
                f"bias={s['bias']:+.2f}s  within_3s={s['within_3s_pct']:.1f}%  (n={s['n']})"
            )

    if args.output:
        args.output.write_text(
            json.dumps(
                {"run_date": date.today().isoformat(), "summary": summary},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n[INFO] Written to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
