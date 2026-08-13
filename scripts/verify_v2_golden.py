"""Verify deterministic Prediction V2 public audit output across platforms."""

from __future__ import annotations

import argparse
import gc
import json
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from strathmark.calculator import HandicapCalculator
from strathmark.ledger import PredictionLedger
from strathmark.mark_optimizer import DEFAULT_MARK_SEED
from strathmark.prediction_v2 import PredictionV2Model
from strathmark.predictor import (
    CompetitorRecord,
    PredictionBundle,
    PredictionContext,
    StaticPredictionProvider,
    WoodProfile,
)

GOLDEN_SCHEMA = "prediction-v2-golden/v1"
DEFAULT_ARTIFACT = Path("strathmark/models/prediction_v2_core.json")
DEFAULT_GOLDEN = Path("benchmarks/prediction_v2_golden.json")


def build_golden(*, artifact_path: str | Path, db_path: str | Path) -> dict[str, Any]:
    """Build one deterministic audit scenario through the public field calculator."""

    model = PredictionV2Model.from_json(Path(artifact_path).read_bytes())
    species = model.species_support[0]
    low, high = model.diameter_support["SB"]
    competitors = [
        CompetitorRecord(name="Golden A", competitor_id="golden-competitor-a", gender="M"),
        CompetitorRecord(name="Golden B", competitor_id="golden-competitor-b", gender="unknown"),
    ]
    ledger = PredictionLedger(db_path)
    calculator = HandicapCalculator(
        event_ceiling=60,
        prediction_provider=StaticPredictionProvider(PredictionBundle(core=model, source="golden")),
        ledger_sink=ledger,
        ledger_caller_id="golden-ci",
    )
    results = calculator.calculate(
        competitors,
        WoodProfile(species=species, diameter_mm=(low + high) / 2.0, quality=5),
        "SB",
        context=PredictionContext(
            prediction_as_of=date(2026, 8, 11),
            request_id="prediction-v2-cross-platform-v1",
            seed=DEFAULT_MARK_SEED,
        ),
    )
    connection = sqlite3.connect(db_path)
    try:
        request_hash = connection.execute(
            "SELECT request_hash FROM prediction_requests"
        ).fetchone()[0]
    finally:
        connection.close()
    output = {
        "schema_version": GOLDEN_SCHEMA,
        "artifact_fingerprint": model.artifact_fingerprint(),
        "canonical_request_hash": request_hash,
        "marks": [result.mark for result in results],
        "optimizer_metadata": results[0].optimizer_metadata,
        "predictions": [
            {
                "competitor_id": result.competitor_id,
                "median_seconds": result.predicted_time,
                "interval": {
                    "lower": result.interval.lower,
                    "upper": result.interval.upper,
                    "coverage": result.interval.nominal_coverage,
                    "state": result.interval.calibration_state,
                    "scope": result.interval.scope,
                },
                "source": result.provenance["prediction_source"],
                "warnings": result.warnings,
            }
            for result in results
        ],
        "stable_prediction_ids": [result.prediction_id for result in results],
    }
    # SQLite connection context managers commit/rollback but do not close.
    # Release any collected handles before a Windows temporary directory exits.
    del calculator
    del ledger
    gc.collect()
    return output


def verify_golden(path: str | Path, actual: Mapping[str, Any]) -> None:
    """Reject any change to the normalized serialized audit contract."""

    try:
        expected = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("golden output is not readable JSON") from exc
    normalized_actual = json.loads(json.dumps(actual, sort_keys=True, allow_nan=False))
    if expected != normalized_actual:
        raise ValueError("Prediction V2 golden output mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="strathmark-golden-") as directory:
        actual = build_golden(
            artifact_path=args.artifact,
            db_path=Path(directory) / "golden.db",
        )
    try:
        verify_golden(args.golden, actual)
    except ValueError as exc:
        print(str(exc))
        return 1
    print(json.dumps(actual, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
