from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.application.operations import (
    FieldDisposition,
    RaceDayField,
    RecoveryTrial,
    RoundStage,
    verify_race_day_replay,
    verify_recovery_matrix,
)

DIGEST = "a" * 64


def _field(
    field_id: str,
    stage: RoundStage,
    epoch: int,
    entrants: tuple[str, ...],
    winner: str,
    *,
    ready_ms: int = 80_000,
    disposition: FieldDisposition = FieldDisposition.PREDICTIVE,
    scheduled_start_offset_ms: int = 0,
    called_after_prior_result_ms: int = 0,
) -> RaceDayField:
    marks = tuple((entrant, 3 + index) for index, entrant in enumerate(entrants))
    placing = (winner, *(entrant for entrant in entrants if entrant != winner))
    return RaceDayField(
        field_id=field_id,
        stage=stage,
        epoch=epoch,
        ordered_competitors=entrants,
        marks_seconds=marks,
        official_placing=placing,
        winner=winner,
        result_to_ready_ms=ready_ms,
        field_assembly_ms=1_100,
        disposition=disposition,
        receipt_digest=DIGEST,
        scheduled_start_offset_ms=scheduled_start_offset_ms,
        called_after_prior_result_ms=called_after_prior_result_ms,
    )


def _whole_tournament() -> tuple[RaceDayField, ...]:
    return (
        _field(
            "field:heat-1",
            RoundStage.HEAT,
            1,
            ("c:a", "c:b", "c:c"),
            "c:a",
            scheduled_start_offset_ms=0,
        ),
        _field(
            "field:heat-2",
            RoundStage.HEAT,
            1,
            ("c:d", "c:e", "c:f"),
            "c:d",
            scheduled_start_offset_ms=600_000,
        ),
        _field(
            "field:quarter",
            RoundStage.QUARTER_FINAL,
            2,
            ("c:a", "c:d"),
            "c:d",
            scheduled_start_offset_ms=900_000,
        ),
        _field(
            "field:semi",
            RoundStage.SEMI_FINAL,
            3,
            ("c:d", "c:g"),
            "c:g",
            scheduled_start_offset_ms=1_200_000,
        ),
        _field(
            "field:divisional",
            RoundStage.DIVISIONAL_FINAL,
            4,
            ("c:g", "c:h"),
            "c:g",
            scheduled_start_offset_ms=1_500_000,
        ),
        _field(
            "field:grand",
            RoundStage.GRAND_FINAL,
            5,
            ("c:g", "c:i"),
            "c:i",
            scheduled_start_offset_ms=1_800_000,
            called_after_prior_result_ms=300_000,
        ),
    )


def test_full_tournament_replay_preserves_epochs_mark_three_and_immutable_winners() -> None:
    report = verify_race_day_replay(_whole_tournament())

    assert report.field_count == 6
    assert report.stage_count == 5
    assert report.same_round_epochs_verified is True
    assert report.between_round_updates_verified is True
    assert report.mark_three_rebasing_verified is True
    assert report.immutable_winners_verified is True
    assert report.maximum_result_to_ready_ms == 80_000
    assert report.maximum_field_assembly_ms == 1_100
    assert report.maximum_heat_interval_ms == 600_000
    assert report.grand_final_turnaround_ms == 300_000
    assert report.digest


def test_hard_deadline_accepts_only_complete_predictive_or_explicit_manual_state() -> None:
    manual = _field(
        "field:quarter",
        RoundStage.QUARTER_FINAL,
        2,
        ("c:a", "c:d"),
        "c:d",
        ready_ms=120_000,
        disposition=FieldDisposition.TRADITIONAL_MANUAL,
    )
    tournament = (*_whole_tournament()[:2], manual, *_whole_tournament()[3:])
    report = verify_race_day_replay(tournament)
    assert report.manual_traditional_fields == ("field:quarter",)

    with pytest.raises(ValueError, match="two-minute"):
        verify_race_day_replay(
            (
                *_whole_tournament()[:2],
                replace(manual, result_to_ready_ms=120_001),
                *_whole_tournament()[3:],
            )
        )
    with pytest.raises(ValueError, match="partial"):
        replace(manual, disposition=FieldDisposition.PARTIAL)


@pytest.mark.parametrize(
    "mutation,match",
    (
        (lambda fields: (replace(fields[0], epoch=2), *fields[1:]), "same-round epoch"),
        (lambda fields: (*fields[:2], replace(fields[2], epoch=1), *fields[3:]), "increase"),
        (
            lambda fields: (
                replace(fields[0], marks_seconds=(("c:a", 4), ("c:b", 5), ("c:c", 6))),
                *fields[1:],
            ),
            "Mark-3",
        ),
        (lambda fields: (replace(fields[0], winner="c:b"), *fields[1:]), "placing"),
        (
            lambda fields: (
                *fields[:2],
                replace(
                    fields[2],
                    ordered_competitors=("c:b", "c:d"),
                    marks_seconds=(("c:b", 3), ("c:d", 4)),
                    official_placing=("c:d", "c:b"),
                ),
                *fields[3:],
            ),
            "winner",
        ),
        (
            lambda fields: (
                fields[0],
                replace(fields[1], field_id=fields[0].field_id),
                *fields[2:],
            ),
            "unique",
        ),
    ),
)
def test_replay_rejects_epoch_mark_winner_advancement_and_identity_mutation(
    mutation, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        verify_race_day_replay(mutation(_whole_tournament()))


def test_recovery_matrix_covers_every_failure_without_duplicate_forecast_or_receipt_change() -> (
    None
):
    required = (
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
            failure=name,
            recovered=True,
            duplicate_forecasts=0,
            receipt_before_digest=f"{index:064x}",
            receipt_after_digest=f"{index:064x}",
            authority_after="v3" if name != "blob_corruption" else "traditional_manual",
            recovery_ms=1_000 + index,
        )
        for index, name in enumerate(required, start=1)
    )

    report = verify_recovery_matrix(trials)
    assert report.failures == required
    assert report.maximum_recovery_ms == 1_010
    assert report.zero_duplicate_forecasts is True
    assert report.immutable_receipts is True

    with pytest.raises(ValueError, match="complete"):
        verify_recovery_matrix(trials[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        verify_recovery_matrix((replace(trials[0], duplicate_forecasts=1), *trials[1:]))
    with pytest.raises(ValueError, match="receipt"):
        verify_recovery_matrix((replace(trials[0], receipt_after_digest="f" * 64), *trials[1:]))


def test_replay_rejects_slower_than_ten_minute_heats_or_five_minute_final_call() -> None:
    fields = _whole_tournament()
    with pytest.raises(ValueError, match="ten-minute"):
        verify_race_day_replay(
            (fields[0], replace(fields[1], scheduled_start_offset_ms=600_001), *fields[2:])
        )
    with pytest.raises(ValueError, match="five-minute"):
        verify_race_day_replay(
            (*fields[:-1], replace(fields[-1], called_after_prior_result_ms=300_001))
        )


def test_race_day_field_and_replay_closed_contract_rejections() -> None:
    base = _whole_tournament()[0]
    mutations = (
        {"field_id": "race:bad"},
        {"stage": "heat"},
        {"epoch": 0},
        {"ordered_competitors": ("c:a", "c:a")},
        {"marks_seconds": (("c:a", 3), ("c:b", 2), ("c:c", 5))},
        {"official_placing": ("c:a", "c:b")},
        {"result_to_ready_ms": -1},
        {"field_assembly_ms": 2_000},
        {"disposition": "predictive"},
        {"receipt_digest": "bad"},
    )
    for mutation in mutations:
        with pytest.raises(ValueError):
            replace(base, **mutation)

    fields = _whole_tournament()
    for invalid in ([], (), ("bad",)):
        with pytest.raises(ValueError, match="transcript"):
            verify_race_day_replay(invalid)
    with pytest.raises(ValueError, match="ordered"):
        verify_race_day_replay((fields[2], *fields[:2], *fields[3:]))
    with pytest.raises(ValueError, match="complete"):
        verify_race_day_replay(fields[:-1])
    with pytest.raises(ValueError, match="unique and increasing"):
        verify_race_day_replay(
            (fields[0], replace(fields[1], scheduled_start_offset_ms=0), *fields[2:])
        )
    with pytest.raises(ValueError, match="grand final"):
        verify_race_day_replay((*fields, replace(fields[-1], field_id="field:grand-2")))


def test_recovery_trial_closed_contract_rejections() -> None:
    base = RecoveryTrial("process_restart", True, 0, DIGEST, DIGEST, "v3", 1)
    mutations = (
        {"failure": "unknown"},
        {"recovered": 1},
        {"duplicate_forecasts": -1},
        {"recovery_ms": 300_001},
        {"receipt_before_digest": "bad"},
        {"authority_after": "v2"},
    )
    for mutation in mutations:
        with pytest.raises(ValueError):
            replace(base, **mutation)
