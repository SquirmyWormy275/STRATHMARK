"""Closed, side-effect-free lifecycle transitions for V3 aggregates."""

from __future__ import annotations

from collections.abc import Iterable

from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.statuses import LifecycleStatus

State = LifecycleStatus | None

_EDGES: dict[AggregateKind, dict[tuple[State, EventKind], LifecycleStatus]] = {
    AggregateKind.TOURNAMENT_INGRESS: {
        (None, EventKind.TOURNAMENT_SNAPSHOT_REVISED): LifecycleStatus.TOURNAMENT_INGRESS_CURRENT,
        (
            LifecycleStatus.TOURNAMENT_INGRESS_CURRENT,
            EventKind.TOURNAMENT_SNAPSHOT_REVISED,
        ): LifecycleStatus.TOURNAMENT_INGRESS_CURRENT,
    },
    AggregateKind.ROUND_INGRESS: {
        (None, EventKind.ROUND_SNAPSHOT_REVISED): LifecycleStatus.ROUND_INGRESS_CURRENT,
        (
            LifecycleStatus.ROUND_INGRESS_CURRENT,
            EventKind.ROUND_SNAPSHOT_REVISED,
        ): LifecycleStatus.ROUND_INGRESS_CURRENT,
    },
    AggregateKind.FIELD_INGRESS: {
        (None, EventKind.FIELD_ROSTER_REVISED): LifecycleStatus.FIELD_INGRESS_CURRENT,
        (
            LifecycleStatus.FIELD_INGRESS_CURRENT,
            EventKind.FIELD_ROSTER_REVISED,
        ): LifecycleStatus.FIELD_INGRESS_CURRENT,
    },
    AggregateKind.RESULT: {
        (None, EventKind.RESULT_RECORDED): LifecycleStatus.RESULT_ACTIVE,
        (LifecycleStatus.RESULT_ACTIVE, EventKind.RESULT_SUPERSEDED): LifecycleStatus.RESULT_ACTIVE,
    },
    AggregateKind.SETTLEMENT: {
        (None, EventKind.LIVE_RACE_SETTLED): LifecycleStatus.SETTLEMENT_RECORDED,
    },
    AggregateKind.EPOCH: {
        (None, EventKind.ROUND_EPOCH_FROZEN): LifecycleStatus.EPOCH_FROZEN,
    },
    AggregateKind.REACTION: {
        (None, EventKind.DERIVATION_REACTION_COMPLETED): LifecycleStatus.REACTION_COMPLETED,
    },
    AggregateKind.DERIVATION: {
        (None, EventKind.DERIVATION_SEQUENCE_COMPLETED): LifecycleStatus.DERIVATION_COMPLETED,
    },
    AggregateKind.FORECAST: {
        (None, EventKind.COMPONENT_FORECAST_COMMITTED): LifecycleStatus.FORECAST_CURRENT,
        (None, EventKind.COMPONENT_FORECAST_REJECTED): LifecycleStatus.FORECAST_CURRENT,
    },
    AggregateKind.SCORE: {
        (None, EventKind.SCORE_RECORDED): LifecycleStatus.SCORE_CURRENT,
        (LifecycleStatus.SCORE_CURRENT, EventKind.SCORE_REVERSED): LifecycleStatus.SCORE_REVERSED,
    },
    AggregateKind.WEIGHTS: {
        (None, EventKind.WEIGHTS_CHANGED): LifecycleStatus.WEIGHTS_CURRENT,
        (None, EventKind.LIVE_SUSPENDED): LifecycleStatus.WEIGHTS_CURRENT,
        (None, EventKind.LIVE_RESUMED): LifecycleStatus.WEIGHTS_CURRENT,
        (None, EventKind.EMERGENCY_STOPPED): LifecycleStatus.WEIGHTS_CURRENT,
        (
            LifecycleStatus.WEIGHTS_CURRENT,
            EventKind.WEIGHTS_CHANGED,
        ): LifecycleStatus.WEIGHTS_CURRENT,
        (
            LifecycleStatus.WEIGHTS_CURRENT,
            EventKind.LIVE_SUSPENDED,
        ): LifecycleStatus.WEIGHTS_CURRENT,
        (LifecycleStatus.WEIGHTS_CURRENT, EventKind.LIVE_RESUMED): LifecycleStatus.WEIGHTS_CURRENT,
        (
            LifecycleStatus.WEIGHTS_CURRENT,
            EventKind.EMERGENCY_STOPPED,
        ): LifecycleStatus.WEIGHTS_CURRENT,
    },
    AggregateKind.TOURNAMENT: {
        (None, EventKind.TOURNAMENT_CONFIGURED): LifecycleStatus.TOURNAMENT_CONFIGURED,
        (
            LifecycleStatus.TOURNAMENT_CONFIGURED,
            EventKind.TOURNAMENT_OPENED,
        ): LifecycleStatus.TOURNAMENT_OPEN,
        (
            LifecycleStatus.TOURNAMENT_OPEN,
            EventKind.TOURNAMENT_CLOSED,
        ): LifecycleStatus.TOURNAMENT_CLOSED,
    },
    AggregateKind.ROUND: {
        (None, EventKind.ROUND_CONFIGURED): LifecycleStatus.ROUND_CONFIGURED,
        (LifecycleStatus.ROUND_CONFIGURED, EventKind.ROUND_FROZEN): LifecycleStatus.ROUND_FROZEN,
        (LifecycleStatus.ROUND_FROZEN, EventKind.ROUND_FROZEN): LifecycleStatus.ROUND_FROZEN,
        (
            LifecycleStatus.ROUND_FROZEN,
            EventKind.ROUND_CLOSING_STARTED,
        ): LifecycleStatus.ROUND_CLOSING,
        (LifecycleStatus.ROUND_CLOSING, EventKind.ROUND_CLOSED): LifecycleStatus.ROUND_CLOSED,
    },
    AggregateKind.FIELD: {
        (None, EventKind.FIELD_OPTIMIZED): LifecycleStatus.FIELD_PREPARED,
        (
            LifecycleStatus.FIELD_PREPARED,
            EventKind.FIELD_SUPERSEDED,
        ): LifecycleStatus.FIELD_SUPERSEDED,
        (
            LifecycleStatus.FIELD_SUPERSEDED,
            EventKind.FIELD_REGENERATED,
        ): LifecycleStatus.FIELD_PREPARED,
        (
            LifecycleStatus.FIELD_PREPARED,
            EventKind.FIELD_REGENERATED,
        ): LifecycleStatus.FIELD_PREPARED,
        (LifecycleStatus.FIELD_PREPARED, EventKind.FIELD_ISSUED): LifecycleStatus.FIELD_ISSUED,
        (LifecycleStatus.FIELD_ISSUED, EventKind.FIELD_SETTLED): LifecycleStatus.FIELD_SETTLED,
    },
    AggregateKind.JOB: {
        (None, EventKind.JOB_QUEUED): LifecycleStatus.JOB_QUEUED,
        (LifecycleStatus.JOB_QUEUED, EventKind.JOB_LEASED): LifecycleStatus.JOB_LEASED,
        (LifecycleStatus.JOB_QUEUED, EventKind.JOB_CANCELLED): LifecycleStatus.JOB_CANCELLED,
        (LifecycleStatus.JOB_LEASED, EventKind.JOB_SUCCEEDED): LifecycleStatus.JOB_SUCCEEDED,
        (LifecycleStatus.JOB_LEASED, EventKind.JOB_INVALID): LifecycleStatus.JOB_INVALID,
        (
            LifecycleStatus.JOB_LEASED,
            EventKind.JOB_RETRYABLE_FAILED,
        ): LifecycleStatus.JOB_RETRYABLE_FAILED,
        (LifecycleStatus.JOB_LEASED, EventKind.JOB_STALE): LifecycleStatus.JOB_STALE,
        (
            LifecycleStatus.JOB_LEASED,
            EventKind.JOB_PERMANENT_FAILED,
        ): LifecycleStatus.JOB_PERMANENT_FAILED,
        (
            LifecycleStatus.JOB_RETRYABLE_FAILED,
            EventKind.JOB_REQUEUED,
        ): LifecycleStatus.JOB_QUEUED,
    },
    AggregateKind.BUNDLE: {
        (None, EventKind.MODEL_CANDIDATE_CREATED): LifecycleStatus.BUNDLE_CANDIDATE,
        (
            LifecycleStatus.BUNDLE_CANDIDATE,
            EventKind.MODEL_CANDIDATE_EVALUATED,
        ): LifecycleStatus.BUNDLE_EVALUATED,
        (
            LifecycleStatus.BUNDLE_EVALUATED,
            EventKind.BUNDLE_PROMOTED,
        ): LifecycleStatus.BUNDLE_PROMOTED,
        (
            LifecycleStatus.BUNDLE_PROMOTED,
            EventKind.BUNDLE_ROLLED_BACK,
        ): LifecycleStatus.BUNDLE_ROLLED_BACK,
    },
    AggregateKind.AUDIT_GENERATION: {
        (None, EventKind.AUDIT_GENERATION_CONSUMED): LifecycleStatus.AUDIT_GENERATION_CONSUMED,
    },
    AggregateKind.MONITORING: {
        (None, EventKind.MONITORING_RECORDED): LifecycleStatus.MONITORING_RECORDED,
    },
    AggregateKind.SERVICE_CREDENTIAL: {
        (
            None,
            EventKind.SERVICE_CREDENTIAL_BOOTSTRAPPED,
        ): LifecycleStatus.SERVICE_CREDENTIAL_ACTIVE,
        (
            LifecycleStatus.SERVICE_CREDENTIAL_ACTIVE,
            EventKind.SERVICE_CREDENTIAL_ROTATED,
        ): LifecycleStatus.SERVICE_CREDENTIAL_ACTIVE,
        (
            LifecycleStatus.SERVICE_CREDENTIAL_ACTIVE,
            EventKind.SERVICE_CREDENTIAL_REVOKED,
        ): LifecycleStatus.SERVICE_CREDENTIAL_ACTIVE,
        (
            LifecycleStatus.SERVICE_CREDENTIAL_ACTIVE,
            EventKind.SERVICE_CREDENTIAL_RECOVERED,
        ): LifecycleStatus.SERVICE_CREDENTIAL_ACTIVE,
    },
    AggregateKind.ISSUE_BATCH: {
        (None, EventKind.ISSUE_BATCH_ISSUED): LifecycleStatus.ISSUE_BATCH_ISSUED,
    },
    AggregateKind.APPROVAL_DECISION: {
        (None, EventKind.APPROVAL_DECISION_RECORDED): (LifecycleStatus.APPROVAL_DECISION_RECORDED),
    },
    AggregateKind.COMPETITOR: {
        (None, EventKind.CAPABILITY_UPDATED): LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT,
        (
            LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT,
            EventKind.CAPABILITY_UPDATED,
        ): LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT,
        (
            LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT,
            EventKind.CAPABILITY_STATE_REBASED,
        ): LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT,
    },
}


def initial_event(aggregate_kind: AggregateKind) -> EventKind:
    """Return the sole legal genesis event for a stateful aggregate."""

    edges = _machine(aggregate_kind)
    initial = [event for (state, event), _next in edges.items() if state is None]
    if len(initial) != 1:
        raise ContractError("aggregate state machine must define exactly one initial event")
    return initial[0]


def transition(
    aggregate_kind: AggregateKind, current: State, event_kind: EventKind
) -> LifecycleStatus:
    """Apply one legal edge or fail without mutating any state."""

    edges = _machine(aggregate_kind)
    if current is not None:
        if not isinstance(current, LifecycleStatus) or not current.value.startswith(
            f"{aggregate_kind.value}_"
        ):
            raise ContractError("current lifecycle status does not belong to aggregate kind")
    if not isinstance(event_kind, EventKind):
        raise ContractError("event kind must be an EventKind")
    try:
        return edges[(current, event_kind)]
    except KeyError as exc:
        state = "genesis" if current is None else current.value
        raise ContractError(
            f"illegal {aggregate_kind.value} lifecycle transition from {state} "
            f"using {event_kind.value}"
        ) from exc


def replay(aggregate_kind: AggregateKind, events: Iterable[EventKind]) -> State:
    """Rebuild one aggregate state from its immutable event-kind stream."""

    state: State = None
    for event_kind in events:
        state = transition(aggregate_kind, state, event_kind)
    return state


def _machine(aggregate_kind: AggregateKind) -> dict[tuple[State, EventKind], LifecycleStatus]:
    if not isinstance(aggregate_kind, AggregateKind) or aggregate_kind not in _EDGES:
        raise ContractError("aggregate kind has no lifecycle state machine")
    return _EDGES[aggregate_kind]


__all__ = ["State", "initial_event", "replay", "transition"]
