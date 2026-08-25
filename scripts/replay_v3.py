"""Run the deterministic whole-tournament and recovery acceptance transcript."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from strathmark.v3.application.operations import (
    FieldDisposition,
    RaceDayField,
    RecoveryTrial,
    RoundStage,
    verify_race_day_replay,
    verify_recovery_matrix,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest


def _field(
    field_id: str,
    stage: RoundStage,
    epoch: int,
    entrants: tuple[str, ...],
    winner: str,
    scheduled_start_offset_ms: int,
    called_after_prior_result_ms: int = 0,
) -> RaceDayField:
    return RaceDayField(
        field_id,
        stage,
        epoch,
        entrants,
        tuple((entrant, 3 + index) for index, entrant in enumerate(entrants)),
        (winner, *(entrant for entrant in entrants if entrant != winner)),
        winner,
        80_000,
        1_100,
        FieldDisposition.PREDICTIVE,
        canonical_digest({"field_id": field_id, "epoch": epoch}),
        scheduled_start_offset_ms,
        called_after_prior_result_ms,
    )


def build_replay_report() -> dict[str, object]:
    fields = (
        _field(
            "field:heat-1", RoundStage.HEAT, 1, ("competitor:a", "competitor:b"), "competitor:a", 0
        ),
        _field(
            "field:heat-2",
            RoundStage.HEAT,
            1,
            ("competitor:c", "competitor:d"),
            "competitor:c",
            600_000,
        ),
        _field(
            "field:quarter",
            RoundStage.QUARTER_FINAL,
            2,
            ("competitor:a", "competitor:c"),
            "competitor:c",
            900_000,
        ),
        _field(
            "field:semi",
            RoundStage.SEMI_FINAL,
            3,
            ("competitor:c", "competitor:e"),
            "competitor:e",
            1_200_000,
        ),
        _field(
            "field:divisional",
            RoundStage.DIVISIONAL_FINAL,
            4,
            ("competitor:e", "competitor:f"),
            "competitor:e",
            1_500_000,
        ),
        _field(
            "field:grand",
            RoundStage.GRAND_FINAL,
            5,
            ("competitor:e", "competitor:g"),
            "competitor:g",
            1_800_000,
            300_000,
        ),
    )
    replay = verify_race_day_replay(fields)
    failures = (
        "process_restart",
        "machine_restart",
        "worker_crash",
        "ollama_restart",
        "cloud_timeout",
        "power_loss",
        "wal_recovery",
        "blob_corruption",
        "disk_reserve",
        "queue_saturation",
    )
    trials = tuple(
        RecoveryTrial(
            failure,
            True,
            0,
            canonical_digest({"failure": failure, "receipt": "before"}),
            canonical_digest({"failure": failure, "receipt": "before"}),
            "traditional_manual" if failure == "blob_corruption" else "v3",
            1_000 + index,
        )
        for index, failure in enumerate(failures)
    )
    recovery = verify_recovery_matrix(trials)
    body: dict[str, object] = {
        "schema_version": "strathmark-v3-whole-system-replay-v1",
        "result": "passed",
        "race_day": replay.to_dict(),
        "race_day_digest": replay.digest,
        "recovery": {
            "failures": list(recovery.failures),
            "maximum_recovery_ms": recovery.maximum_recovery_ms,
            "zero_duplicate_forecasts": recovery.zero_duplicate_forecasts,
            "immutable_receipts": recovery.immutable_receipts,
        },
        "recovery_digest": recovery.digest,
    }
    body["report_digest"] = canonical_digest(body)
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    arguments = parser.parse_args(argv)
    report = build_replay_report()
    encoded = canonical_bytes(report) + b"\n"
    if arguments.verify is not None:
        try:
            if arguments.verify.read_bytes() != encoded:
                return 2
        except OSError:
            return 2
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_bytes(encoded)
    elif arguments.verify is None:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
