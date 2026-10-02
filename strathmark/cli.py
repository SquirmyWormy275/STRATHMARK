"""Offline V2 demonstration through the preserved public calculation API."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date


def main() -> None:
    parser = argparse.ArgumentParser(description="STRATHMARK portable offline V2 calculation demo.")
    parser.add_argument("command", choices=("demo",))
    parser.parse_args()
    from strathmark import HandicapCalculator, __version__
    from strathmark.predictor import (
        CompetitorRecord,
        HistoricalResult,
        PredictionContext,
        WoodProfile,
    )

    competitors = [
        CompetitorRecord(
            name="Alice",
            competitor_id="demo-alice",
            gender="F",
            history=[HistoricalResult("SB", 28.4, "Pine", 300, 5, date(2025, 3, 1))],
        ),
        CompetitorRecord(
            name="Bob",
            competitor_id="demo-bob",
            gender="M",
            history=[HistoricalResult("SB", 35.2, "Pine", 300, 5, date(2025, 3, 1))],
        ),
    ]
    results = HandicapCalculator().calculate(
        competitors,
        WoodProfile(species="Pine", diameter_mm=300, quality=5),
        event_code="SB",
        context=PredictionContext(prediction_as_of=date(2026, 1, 1)),
    )
    print(
        json.dumps(
            {
                "package_version": __version__,
                "purpose": "synthetic_v2_demo",
                "results": [asdict(result) for result in results],
            },
            default=str,
            indent=2,
        )
    )
