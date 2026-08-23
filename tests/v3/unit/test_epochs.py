from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.epochs import (
    EpochMember,
    MandatoryReaction,
    ReactionBarrier,
    correction_destination,
    freeze_epoch,
)


def _members() -> tuple[EpochMember, ...]:
    return (
        EpochMember("result:a", 2, 9, True),
        EpochMember("result:b", 1, 11, False),
    )


def test_epoch_is_content_addressed_and_requires_complete_barrier() -> None:
    barrier = ReactionBarrier.complete_through(11)
    epoch = freeze_epoch(
        round_id=StableIdentifier("round:semi"),
        epoch_revision=2,
        historical_cutoff_key="history:2026-08-21",
        closed_through_sequence=11,
        members=_members(),
        barrier=barrier,
    )
    assert epoch.maximum_tournament_sequence == 11
    assert epoch.epoch_id.namespace == "epoch"
    assert epoch == freeze_epoch(
        round_id=StableIdentifier("round:semi"),
        epoch_revision=2,
        historical_cutoff_key="history:2026-08-21",
        closed_through_sequence=11,
        members=_members(),
        barrier=barrier,
    )
    with pytest.raises(Exception, match="barrier"):
        freeze_epoch(
            round_id=StableIdentifier("round:semi"),
            epoch_revision=2,
            historical_cutoff_key="history:2026-08-21",
            closed_through_sequence=11,
            members=_members(),
            barrier=ReactionBarrier(10, frozenset(MandatoryReaction)),
        )


def test_epoch_rejects_mixed_or_unsorted_final_revisions() -> None:
    with pytest.raises(Exception, match="sorted"):
        freeze_epoch(
            round_id=StableIdentifier("round:semi"),
            epoch_revision=1,
            historical_cutoff_key="history:cutoff",
            closed_through_sequence=11,
            members=tuple(reversed(_members())),
            barrier=ReactionBarrier.complete_through(11),
        )
    with pytest.raises(Exception, match="duplicate"):
        freeze_epoch(
            round_id=StableIdentifier("round:semi"),
            epoch_revision=1,
            historical_cutoff_key="history:cutoff",
            closed_through_sequence=12,
            members=(_members()[0], EpochMember("result:a", 3, 12, True)),
            barrier=ReactionBarrier.complete_through(12),
        )


def test_first_issue_seal_routes_corrections_without_rewriting_issued_round() -> None:
    current = StableIdentifier("round:quarter")
    following = StableIdentifier("round:semi")
    assert correction_destination(None, 20, current, following) == current
    assert correction_destination(21, 20, current, following) == current
    assert correction_destination(21, 22, current, following) == following


def test_empty_epoch_keeps_the_explicit_closed_event_boundary() -> None:
    epoch = freeze_epoch(
        round_id=StableIdentifier("round:empty"),
        epoch_revision=1,
        historical_cutoff_key="history:cutoff",
        closed_through_sequence=17,
        members=(),
        barrier=ReactionBarrier.complete_through(17),
    )
    assert epoch.maximum_tournament_sequence == 17


@pytest.mark.parametrize("revision", [True, "1", 0])
def test_epoch_member_rejects_invalid_revisions(revision) -> None:
    with pytest.raises(Exception, match="revision"):
        EpochMember("result:a", revision, 1, True)


@pytest.mark.parametrize("sequence", [True, "1", 0])
def test_epoch_member_rejects_invalid_source_sequences(sequence) -> None:
    with pytest.raises(Exception, match="source_sequence"):
        EpochMember("result:a", 1, sequence, True)


def test_epoch_member_requires_explicit_eligibility() -> None:
    with pytest.raises(Exception, match="eligibility"):
        EpochMember("result:a", 1, 1, 1)


@pytest.mark.parametrize("sequence", [True, "1", -1])
def test_reaction_barrier_rejects_invalid_sequences(sequence) -> None:
    with pytest.raises(Exception, match="non-negative"):
        ReactionBarrier(sequence, frozenset(MandatoryReaction))


def test_reaction_barrier_rejects_mutable_or_unknown_reactions() -> None:
    with pytest.raises(Exception, match="closed vocabulary"):
        ReactionBarrier(1, tuple(MandatoryReaction))
    with pytest.raises(Exception, match="closed vocabulary"):
        ReactionBarrier(1, frozenset({"capability"}))


def test_evidence_epoch_validates_every_content_addressed_field() -> None:
    valid = freeze_epoch(
        round_id=StableIdentifier("round:semi"),
        epoch_revision=1,
        historical_cutoff_key="history:cutoff",
        closed_through_sequence=11,
        members=_members(),
        barrier=ReactionBarrier.complete_through(11),
    )
    for revision in (True, "1", 0):
        with pytest.raises(Exception, match="revision"):
            replace(valid, epoch_revision=revision)
    for maximum in (True, "1", -1):
        with pytest.raises(Exception, match="maximum"):
            replace(valid, maximum_tournament_sequence=maximum)
    with pytest.raises(Exception, match="immutable"):
        replace(valid, members=list(valid.members))
    with pytest.raises(Exception, match="immutable"):
        replace(valid, members=("member",))
    with pytest.raises(Exception, match="causal boundary"):
        replace(valid, maximum_tournament_sequence=10)
    with pytest.raises(Exception, match="digest"):
        replace(valid, content_digest="f" * 64)
    with pytest.raises(Exception, match="content addressed"):
        replace(valid, epoch_id=StableIdentifier("epoch:wrong"))


@pytest.mark.parametrize("boundary", [True, "1", -1])
def test_freeze_rejects_invalid_closed_boundaries(boundary) -> None:
    with pytest.raises(Exception, match="closed tournament sequence"):
        freeze_epoch(
            round_id=StableIdentifier("round:semi"),
            epoch_revision=1,
            historical_cutoff_key="history:cutoff",
            closed_through_sequence=boundary,
            members=(),
            barrier=ReactionBarrier.complete_through(1),
        )


def test_freeze_rejects_open_barrier_member_leakage_and_incomplete_reactions() -> None:
    with pytest.raises(Exception, match="reaction barrier"):
        freeze_epoch(
            round_id=StableIdentifier("round:semi"),
            epoch_revision=1,
            historical_cutoff_key="history:cutoff",
            closed_through_sequence=1,
            members=(),
            barrier="complete",
        )
    with pytest.raises(Exception, match="exceeds"):
        freeze_epoch(
            round_id=StableIdentifier("round:semi"),
            epoch_revision=1,
            historical_cutoff_key="history:cutoff",
            closed_through_sequence=1,
            members=(EpochMember("result:a", 1, 2, True),),
            barrier=ReactionBarrier.complete_through(2),
        )
    with pytest.raises(Exception, match="barrier"):
        freeze_epoch(
            round_id=StableIdentifier("round:semi"),
            epoch_revision=1,
            historical_cutoff_key="history:cutoff",
            closed_through_sequence=1,
            members=(),
            barrier=ReactionBarrier(1, frozenset()),
        )


@pytest.mark.parametrize("correction", [True, "1", 0])
def test_correction_routing_rejects_invalid_correction_sequences(correction) -> None:
    with pytest.raises(Exception, match="correction sequence"):
        correction_destination(
            None,
            correction,
            StableIdentifier("round:quarter"),
            StableIdentifier("round:semi"),
        )


@pytest.mark.parametrize("first_issue", [True, "1", 0])
def test_correction_routing_rejects_invalid_issue_sequences(first_issue) -> None:
    with pytest.raises(Exception, match="first issue sequence"):
        correction_destination(
            first_issue,
            2,
            StableIdentifier("round:quarter"),
            StableIdentifier("round:semi"),
        )
