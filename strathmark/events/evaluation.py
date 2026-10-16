"""Reproducible synthetic reporting through the real packaged V2 engine.

Historical/native replay and prospective verification retain their existing
separate accuracy commands and frozen component requirements.
"""

import argparse
import json
import math
from pathlib import Path
from statistics import median

from .examples import synthetic_event
from .numerics import calculate, numeric_identity


def metrics(rows, key="predicted_raw_seconds"):
    if not rows:
        return {
            "sample_count": 0,
            "mae_seconds": None,
            "median_absolute_error_seconds": None,
            "p90_error_seconds": None,
            "p99_error_seconds": None,
            "max_error_seconds": None,
            "interval_count": 0,
            "interval_coverage": None,
            "mean_interval_width_seconds": None,
        }
    errors = sorted(abs(float(r[key]) - r["actual_raw_seconds"]) for r in rows)
    intervals = [r for r in rows if r.get("interval") and key == "predicted_raw_seconds"]
    return {
        "sample_count": len(rows),
        "mae_seconds": sum(errors) / len(rows),
        "median_absolute_error_seconds": median(errors),
        "p90_error_seconds": errors[math.ceil(0.9 * len(rows)) - 1],
        "p99_error_seconds": errors[math.ceil(0.99 * len(rows)) - 1],
        "max_error_seconds": errors[-1],
        "interval_count": len(intervals),
        "interval_coverage": sum(
            r["interval"]["lower"] <= r["actual_raw_seconds"] <= r["interval"]["upper"]
            for r in intervals
        )
        / len(intervals)
        if intervals
        else None,
        "mean_interval_width_seconds": sum(
            r["interval"]["upper"] - r["interval"]["lower"] for r in intervals
        )
        / len(intervals)
        if intervals
        else None,
    }


def synthetic_evaluation(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    config = synthetic_event()
    rows = []
    for i, supplied in enumerate(config["roster"]):
        one = json.loads(json.dumps(config))
        one["roster"] = [supplied]
        if i >= 4:
            one["wood"]["species"] = "SYNTHETIC-UNKNOWN"
            one["wood"]["diameter_mm"] = 300
        result = calculate(one, [supplied["entry_id"]], forecasts_only=True)[0]
        result.update(
            row_id=f"synthetic:{i}",
            event="UH",
            material=one["wood"]["species"],
            diameter_mm=one["wood"]["diameter_mm"],
            history_depth=len(supplied["history"]),
            actual_raw_seconds=float(31 + i * 2),
            baseline_seconds=float(supplied["history"][-1]["time_seconds"]) if i < 4 else None,
        )
        rows.append(result)
    matched = [r for r in rows if r["baseline_seconds"] is not None]
    slices = {
        key: {
            str(value): metrics([r for r in rows if r[key] == value])
            for value in sorted({r[key] for r in rows}, key=str)
        }
        for key in ("event", "material", "diameter_mm", "history_depth")
    }
    report = {
        "schema_version": "strathmark.synthetic-evaluation.v1",
        "mode": "synthetic_verification",
        "claim": "Plumbing and deterministic numerical verification only; no real-world accuracy or handicap-fairness claim.",
        "counts": {
            "total": len(rows),
            "eligible": len(rows),
            "excluded": 0,
            "exclusions_by_reason": {},
            "prediction_available": len(rows),
            "abstentions": 0,
            "degraded": sum(r["degraded"] for r in rows),
            "baseline_available": len(matched),
            "baseline_unavailable_reason": {
                "no_exact_material_diameter_prior": len(rows) - len(matched)
            },
        },
        "overall": metrics(rows),
        "matched_baseline": {
            "count": len(matched),
            "engine": metrics(matched),
            "last_exact_context_cut": metrics(matched, "baseline_seconds"),
        },
        "slices": slices,
        "numeric_identity": numeric_identity(),
        "rows": rows,
        "historical_replay": {
            "status": "NOT_RUN",
            "reason": "No new private-data run is authorized by this implementation.",
        },
        "prospective": {
            "status": "BLOCKED",
            "minimum_later_results": 100,
            "minimum_new_competitions": 10,
            "reason": "Genuinely later unexamined observations and exact frozen components are required.",
        },
        "numeric_promotion": False,
        "official_use": False,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2))
    (output / "report.txt").write_text(
        "Synthetic verification — real V2 on generated evidence.\n"
        + report["claim"]
        + "\n"
        + f"Eligible/evaluated: {len(rows)}/{len(rows)}. Excluded: 0. Available: {len(rows)}; abstentions: 0.\n"
        + f"MAE: {report['overall']['mae_seconds']:.6f} s; median: {report['overall']['median_absolute_error_seconds']:.6f} s; p90: {report['overall']['p90_error_seconds']:.6f} s.\n"
        + f"Interval coverage: {report['overall']['interval_coverage']}; mean width: {report['overall']['mean_interval_width_seconds']} s.\nMatched baseline cohort: {len(matched)}; unavailable baseline rows retained: {len(rows) - len(matched)}.\nHistorical replay NOT_RUN. Prospective BLOCKED pending 100 later results / 10 new competitions.\n"
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["synthetic"])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = synthetic_evaluation(args.output)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"rows", "slices"}}, indent=2
        )
    )
