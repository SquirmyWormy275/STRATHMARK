from __future__ import annotations

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.capability import (
    CapabilityCapacityEnvelope,
    CapabilityEvidence,
    CapabilityPrior,
    evaluate_rebase_capacity,
    replay_capability,
)
from strathmark.v3.domain.evidence import AdmissionReason, EvidenceSource


def _rows(values: tuple[int, ...]) -> tuple[CapabilityEvidence, ...]:
    return tuple(
        CapabilityEvidence(
            result_key=StableIdentifier(f"result:trajectory-{index}"),
            result_revision=1,
            supersedes_revision=None,
            competitor_id=StableIdentifier("competitor:trajectory"),
            context_digest="c" * 64,
            source_global_sequence=index,
            observed_at_utc=f"2026-02-{index:02d}T12:00:00.000Z",
            raw_time_ms=value,
            source=EvidenceSource.LIVE_ISSUED_RACE,
            numeric_eligible=True,
            admission_reason=AdmissionReason.ELIGIBLE_COMPLETION,
            observation_digest=canonical_digest({"index": index, "value": value}),
            authority_digest="d" * 64,
            prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
            evidence_log_variance="0.0025",
            conversion_log_variance="0",
            effective_weight="1",
            historical_binding=None,
        )
        for index, value in enumerate(values, 1)
    )


def test_alternating_and_strategic_coasting_shapes_are_numeric_and_auditable() -> None:
    alternating = replay_capability(_rows((40_000, 55_000, 35_000, 55_000, 35_000, 55_000)))
    steady = replay_capability(_rows((40_000, 41_000, 42_000, 43_000, 44_000, 45_000)))
    assert alternating is not None and steady is not None
    assert alternating.alternation_count > steady.alternation_count
    assert alternating.last_transition.anomaly.alternation_ratio != "0"
    serialized = str(alternating.to_dict()).lower()
    assert "fox" not in serialized and "cheat" not in serialized and "motive" not in serialized


def test_time_decay_and_permutation_of_append_sequences_preserve_clean_trajectory_digest() -> None:
    rows = _rows((40_000, 35_000, 37_000))
    delayed = tuple(
        row
        if index != 2
        else row.__class__(
            **{
                field: (
                    "2026-08-03T12:00:00.000Z"
                    if field == "observed_at_utc"
                    else getattr(row, field)
                )
                for field in row.__dataclass_fields__
            }
        )
        for index, row in enumerate(rows)
    )
    ordinary = replay_capability(rows)
    decayed = replay_capability(delayed)
    assert ordinary is not None and decayed is not None
    assert ordinary.state_digest != decayed.state_digest
    resequenced = tuple(
        row.__class__(
            **{
                field: (100 - index if field == "source_global_sequence" else getattr(row, field))
                for field in row.__dataclass_fields__
            }
        )
        for index, row in enumerate(rows)
    )
    assert replay_capability(resequenced).state_digest == ordinary.state_digest  # type: ignore[union-attr]


def test_correction_replay_equals_clean_final_revision_ledger() -> None:
    original = _rows((40_000, 70_000, 42_000, 41_000))
    corrected = CapabilityEvidence(
        **{
            field: (
                2
                if field == "result_revision"
                else 1
                if field == "supersedes_revision"
                else 35_000
                if field == "raw_time_ms"
                else 99
                if field == "source_global_sequence"
                else canonical_digest({"corrected": True})
                if field == "observation_digest"
                else getattr(original[1], field)
            )
            for field in original[1].__dataclass_fields__
        }
    )
    active = (original[0], corrected, original[2], original[3])
    correction_ledger = (*original, corrected)
    assert (
        replay_capability(correction_ledger).state_digest
        == replay_capability(active).state_digest
        == replay_capability(tuple(reversed(active))).state_digest
    )  # type: ignore[union-attr]


def test_signed_capacity_envelope_converges_at_256_rows_and_closes_one_beyond() -> None:
    seed = _rows((40_000,))[0]
    ledger = tuple(
        CapabilityEvidence(
            **{
                field: (
                    StableIdentifier(f"result:capacity-{index:03d}")
                    if field == "result_key"
                    else index
                    if field == "source_global_sequence"
                    else 40_000 + (index % 7) * 100
                    if field == "raw_time_ms"
                    else canonical_digest({"capacity_index": index})
                    if field == "observation_digest"
                    else getattr(seed, field)
                )
                for field in seed.__dataclass_fields__
            }
        )
        for index in range(1, 257)
    )
    forward = replay_capability(ledger)
    reverse = replay_capability(tuple(reversed(ledger)))
    assert forward is not None and reverse is not None
    assert forward.state_revision == 256
    assert forward.state_digest == reverse.state_digest

    envelope = CapabilityCapacityEnvelope()
    assert evaluate_rebase_capacity(
        envelope,
        lineage_rows=256,
        invalidated_work=128,
        mandatory_reactions=512,
    ).admitted
    assert not evaluate_rebase_capacity(
        envelope,
        lineage_rows=257,
        invalidated_work=128,
        mandatory_reactions=512,
    ).admitted
