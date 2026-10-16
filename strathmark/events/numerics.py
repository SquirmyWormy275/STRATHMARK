"""Reuse the existing predictor; new mark policy is explicitly separate."""

import sys
from dataclasses import asdict
from datetime import date
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from pathlib import Path

import numpy
import pandas

from strathmark.calculator import HandicapCalculator
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    PredictionContext,
    StaticPredictionProvider,
    WoodProfile,
    _load_prediction_bundle,
)

from .contracts import EventError, digest


def numeric_identity():
    root = Path(__file__).resolve().parents[1]
    return digest(
        {
            "runtime": {
                "python": list(sys.version_info[:2]),
                "numpy": numpy.__version__,
                "pandas": pandas.__version__,
            },
            "sources": {
                name: sha256((root / name).read_bytes()).hexdigest()
                for name in (
                    "calculator.py",
                    "prediction_v2.py",
                    "predictor.py",
                    "features.py",
                    "mark_optimizer.py",
                    "models/prediction_v2_core.json",
                    "config.py",
                    "decay.py",
                    "fallback.py",
                    "wood.py",
                    "variance.py",
                    "provenance.py",
                    "events/numerics.py",
                )
            },
        }
    )


def calculate(config, entry_ids, *, forecasts_only=False):
    roster = {r["entry_id"]: r for r in config["roster"]}
    records = []
    for entry_id in entry_ids:
        row = roster[entry_id]
        records.append(
            CompetitorRecord(
                name=row["engine_id"],
                competitor_id=row["engine_id"],
                gender=row["gender"],
                history=[
                    HistoricalResult(
                        event_code=h["event_code"],
                        time_seconds=float(h["time_seconds"]),
                        species=h["species"],
                        diameter_mm=float(h["diameter_mm"]),
                        quality=5,
                        result_date=date.fromisoformat(h["result_date"]),
                    )
                    for h in row["history"]
                ],
            )
        )
    # Pin the packaged model explicitly; ambient operator model paths cannot
    # change an event during an interface handover or package upgrade.
    model = Path(__file__).resolve().parents[1] / "models/prediction_v2_core.json"
    provider = StaticPredictionProvider(
        _load_prediction_bundle(
            model, None, "package", date.fromisoformat(config["prediction_as_of"])
        )
    )
    results = HandicapCalculator(prediction_provider=provider).calculate(
        records,
        WoodProfile(config["wood"]["species"], float(config["wood"]["diameter_mm"]), 5),
        config["discipline"],
        context=PredictionContext(
            prediction_as_of=date.fromisoformat(config["prediction_as_of"]), engine="v2"
        ),
    )
    by_id = {r.competitor_id: r for r in results}
    slowest = max(Decimal(str(r.predicted_time)) for r in results)
    rows = []
    for entry_id in entry_ids:
        result = by_id[roster[entry_id]["engine_id"]]
        value = asdict(result)
        row = {
            "entry_id": entry_id,
            "engine_id": result.competitor_id,
            "predicted_raw_seconds": str(result.predicted_time),
            "engine_version": result.engine_version,
            "model_version": result.model_version,
            "calibration_version": result.calibration_version,
            "interval": value["interval"],
            "warnings": result.warnings,
            "degraded": result.degraded,
            "ignored_factors": result.ignored_factors,
            "provenance": value["provenance"],
        }
        if not forecasts_only:
            mark = result.mark
            if config["mark_policy"] == "mark3-ceil180.v1":
                mark = 3 + int(
                    (slowest - Decimal(str(result.predicted_time))).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                )
                if mark > 180:
                    raise EventError("mark_policy_gap_exceeds_180: field requires policy review")
            row["start_mark_seconds"] = mark
            row["optimizer"] = (
                result.optimizer
                if config["mark_policy"] == "v2-optimizer.v1"
                else "mark3-ceil180.v1"
            )
        rows.append(row)
    return rows
