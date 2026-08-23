from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.epochs import EpochMember, ReactionBarrier, freeze_epoch


@given(
    st.lists(
        st.tuples(st.text(alphabet="abc", min_size=1, max_size=5), st.booleans()),
        min_size=1,
        max_size=30,
        unique_by=lambda item: item[0],
    )
)
def test_epoch_membership_is_not_affected_by_heat_delivery_order(
    rows: list[tuple[str, bool]],
) -> None:
    ordered = tuple(
        EpochMember(f"result:{key}", 1, index + 1, eligible)
        for index, (key, eligible) in enumerate(sorted(rows))
    )
    epoch = freeze_epoch(
        round_id=StableIdentifier("round:next"),
        epoch_revision=1,
        historical_cutoff_key="history:fixed",
        closed_through_sequence=len(ordered),
        members=ordered,
        barrier=ReactionBarrier.complete_through(len(ordered)),
    )
    assert tuple(member.result_key for member in epoch.members) == tuple(
        sorted(member.result_key for member in ordered)
    )
    assert all(
        member.source_sequence <= epoch.maximum_tournament_sequence for member in epoch.members
    )
