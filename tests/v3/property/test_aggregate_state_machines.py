from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.domain.state_machines import replay, transition

STATEFUL_KINDS = (
    AggregateKind.TOURNAMENT,
    AggregateKind.ROUND,
    AggregateKind.FIELD,
    AggregateKind.JOB,
    AggregateKind.BUNDLE,
    AggregateKind.ISSUE_BATCH,
)


@given(st.sampled_from(STATEFUL_KINDS), st.lists(st.sampled_from(tuple(EventKind)), max_size=20))
def test_replay_is_exactly_the_repeated_pure_transition(
    aggregate_kind: AggregateKind, events: list[EventKind]
) -> None:
    state = None
    accepted: list[EventKind] = []
    for event in events:
        try:
            state = transition(aggregate_kind, state, event)
        except ContractError:
            continue
        accepted.append(event)
    assert replay(aggregate_kind, tuple(accepted)) is state
    assert replay(aggregate_kind, tuple(accepted)) is state


@given(st.sampled_from(STATEFUL_KINDS), st.lists(st.sampled_from(tuple(EventKind)), max_size=15))
def test_first_illegal_edge_never_mutates_the_prior_state(
    aggregate_kind: AggregateKind, events: list[EventKind]
) -> None:
    state = None
    for event in events:
        before = state
        try:
            state = transition(aggregate_kind, state, event)
        except ContractError:
            assert state is before
