from __future__ import annotations

from dataclasses import replace

import pytest

import strathmark.v3.domain.state_machines as state_machine_module
from strathmark.v3.application.commands import (
    CommandRequest,
    EventIntent,
    validate_command_event_intents,
)
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.statuses import (
    AggregateLifecycle,
    LifecycleAggregateKind,
    LifecycleStatus,
)
from strathmark.v3.domain.state_machines import initial_event, transition

LIFECYCLES = {
    AggregateKind.TOURNAMENT: (
        (None, EventKind.TOURNAMENT_CONFIGURED, LifecycleStatus.TOURNAMENT_CONFIGURED),
        (
            LifecycleStatus.TOURNAMENT_CONFIGURED,
            EventKind.TOURNAMENT_OPENED,
            LifecycleStatus.TOURNAMENT_OPEN,
        ),
        (
            LifecycleStatus.TOURNAMENT_OPEN,
            EventKind.TOURNAMENT_CLOSED,
            LifecycleStatus.TOURNAMENT_CLOSED,
        ),
    ),
    AggregateKind.ROUND: (
        (None, EventKind.ROUND_CONFIGURED, LifecycleStatus.ROUND_CONFIGURED),
        (LifecycleStatus.ROUND_CONFIGURED, EventKind.ROUND_FROZEN, LifecycleStatus.ROUND_FROZEN),
        (LifecycleStatus.ROUND_FROZEN, EventKind.ROUND_FROZEN, LifecycleStatus.ROUND_FROZEN),
        (
            LifecycleStatus.ROUND_FROZEN,
            EventKind.ROUND_CLOSING_STARTED,
            LifecycleStatus.ROUND_CLOSING,
        ),
        (LifecycleStatus.ROUND_CLOSING, EventKind.ROUND_CLOSED, LifecycleStatus.ROUND_CLOSED),
    ),
    AggregateKind.FIELD: (
        (None, EventKind.FIELD_OPTIMIZED, LifecycleStatus.FIELD_PREPARED),
        (
            LifecycleStatus.FIELD_PREPARED,
            EventKind.FIELD_SUPERSEDED,
            LifecycleStatus.FIELD_SUPERSEDED,
        ),
        (
            LifecycleStatus.FIELD_SUPERSEDED,
            EventKind.FIELD_REGENERATED,
            LifecycleStatus.FIELD_PREPARED,
        ),
        (LifecycleStatus.FIELD_PREPARED, EventKind.FIELD_ISSUED, LifecycleStatus.FIELD_ISSUED),
        (LifecycleStatus.FIELD_ISSUED, EventKind.FIELD_SETTLED, LifecycleStatus.FIELD_SETTLED),
    ),
    AggregateKind.JOB: (
        (None, EventKind.JOB_QUEUED, LifecycleStatus.JOB_QUEUED),
        (LifecycleStatus.JOB_QUEUED, EventKind.JOB_LEASED, LifecycleStatus.JOB_LEASED),
        (LifecycleStatus.JOB_LEASED, EventKind.JOB_SUCCEEDED, LifecycleStatus.JOB_SUCCEEDED),
    ),
    AggregateKind.BUNDLE: (
        (None, EventKind.MODEL_CANDIDATE_CREATED, LifecycleStatus.BUNDLE_CANDIDATE),
        (
            LifecycleStatus.BUNDLE_CANDIDATE,
            EventKind.MODEL_CANDIDATE_EVALUATED,
            LifecycleStatus.BUNDLE_EVALUATED,
        ),
        (
            LifecycleStatus.BUNDLE_EVALUATED,
            EventKind.BUNDLE_PROMOTED,
            LifecycleStatus.BUNDLE_PROMOTED,
        ),
        (
            LifecycleStatus.BUNDLE_PROMOTED,
            EventKind.BUNDLE_ROLLED_BACK,
            LifecycleStatus.BUNDLE_ROLLED_BACK,
        ),
    ),
    AggregateKind.ISSUE_BATCH: (
        (None, EventKind.ISSUE_BATCH_ISSUED, LifecycleStatus.ISSUE_BATCH_ISSUED),
    ),
    AggregateKind.AUDIT_GENERATION: (
        (
            None,
            EventKind.AUDIT_GENERATION_CONSUMED,
            LifecycleStatus.AUDIT_GENERATION_CONSUMED,
        ),
    ),
    AggregateKind.MONITORING: (
        (None, EventKind.MONITORING_RECORDED, LifecycleStatus.MONITORING_RECORDED),
    ),
}


@pytest.mark.parametrize("aggregate_kind", LIFECYCLES)
def test_every_declared_lifecycle_edge_is_legal(aggregate_kind: AggregateKind) -> None:
    for current, event_kind, expected in LIFECYCLES[aggregate_kind]:
        assert transition(aggregate_kind, current, event_kind) is expected


def test_issue_batch_frozen_contract_round_trips_as_terminal_lifecycle() -> None:
    batch = AggregateLifecycle(
        LifecycleAggregateKind.ISSUE_BATCH,
        LifecycleStatus.ISSUE_BATCH_ISSUED,
    )
    assert AggregateLifecycle.from_dict(batch.to_dict()) == batch
    with pytest.raises(ContractError, match="illegal"):
        batch.transition_to(LifecycleStatus.ISSUE_BATCH_ISSUED)


def test_job_retry_and_every_terminal_edge_are_closed() -> None:
    assert (
        transition(AggregateKind.JOB, LifecycleStatus.JOB_QUEUED, EventKind.JOB_CANCELLED)
        is LifecycleStatus.JOB_CANCELLED
    )
    for event_kind, expected in (
        (EventKind.JOB_INVALID, LifecycleStatus.JOB_INVALID),
        (EventKind.JOB_RETRYABLE_FAILED, LifecycleStatus.JOB_RETRYABLE_FAILED),
        (EventKind.JOB_STALE, LifecycleStatus.JOB_STALE),
        (EventKind.JOB_PERMANENT_FAILED, LifecycleStatus.JOB_PERMANENT_FAILED),
    ):
        assert transition(AggregateKind.JOB, LifecycleStatus.JOB_LEASED, event_kind) is expected
    assert (
        transition(
            AggregateKind.JOB,
            LifecycleStatus.JOB_RETRYABLE_FAILED,
            EventKind.JOB_REQUEUED,
        )
        is LifecycleStatus.JOB_QUEUED
    )


@pytest.mark.parametrize("aggregate_kind", LIFECYCLES)
def test_wrong_initial_and_cross_aggregate_events_fail_closed(
    aggregate_kind: AggregateKind,
) -> None:
    correct = initial_event(aggregate_kind)
    wrong = next(event for event in EventKind if event is not correct)
    with pytest.raises(ContractError, match="illegal"):
        transition(aggregate_kind, None, wrong)
    with pytest.raises(ContractError, match="does not belong"):
        transition(aggregate_kind, "not-a-status", correct)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("aggregate_kind", "terminal"),
    [
        (AggregateKind.TOURNAMENT, LifecycleStatus.TOURNAMENT_CLOSED),
        (AggregateKind.ROUND, LifecycleStatus.ROUND_CLOSED),
        (AggregateKind.FIELD, LifecycleStatus.FIELD_SETTLED),
        (AggregateKind.JOB, LifecycleStatus.JOB_SUCCEEDED),
        (AggregateKind.BUNDLE, LifecycleStatus.BUNDLE_ROLLED_BACK),
        (AggregateKind.ISSUE_BATCH, LifecycleStatus.ISSUE_BATCH_ISSUED),
        (AggregateKind.AUDIT_GENERATION, LifecycleStatus.AUDIT_GENERATION_CONSUMED),
        (AggregateKind.MONITORING, LifecycleStatus.MONITORING_RECORDED),
    ],
)
def test_terminal_states_reject_every_event(
    aggregate_kind: AggregateKind, terminal: LifecycleStatus
) -> None:
    for event_kind in EventKind:
        with pytest.raises(ContractError, match="illegal"):
            transition(aggregate_kind, terminal, event_kind)


def test_non_lifecycle_aggregate_has_no_state_machine() -> None:
    with pytest.raises(ContractError, match="state machine"):
        initial_event(AggregateKind.SYSTEM)
    with pytest.raises(ContractError, match="state machine"):
        transition(AggregateKind.SYSTEM, None, EventKind.HISTORY_IMPORTED)
    with pytest.raises(ContractError, match="state machine"):
        initial_event("field")  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="EventKind"):
        transition(AggregateKind.FIELD, None, "field_optimized")  # type: ignore[arg-type]


def test_initial_event_fails_if_closed_table_invariant_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(state_machine_module._EDGES, AggregateKind.FIELD, {})
    with pytest.raises(ContractError, match="exactly one"):
        initial_event(AggregateKind.FIELD)


def _application_request() -> CommandRequest:
    aggregate_id = StableIdentifier("tournament:show")
    return CommandRequest(
        principal_id=StableIdentifier("actor:judge"),
        command=CommandEnvelope(
            kind=CommandKind.CONFIGURE_TOURNAMENT,
            command_id=IdempotencyKey("command:configure"),
            target_aggregate=aggregate_id,
            expected_versions=((str(aggregate_id), 0),),
            actor_id=StableIdentifier("actor:judge"),
            payload=InlinePayload.from_value({}),
        ),
        events=(
            EventIntent(
                AggregateKind.TOURNAMENT,
                aggregate_id,
                EventKind.TOURNAMENT_CONFIGURED,
            ),
        ),
        result_schema_version="result:v1",
        result={"ok": True},
        occurred_at_utc="2026-08-22T00:00:00.000Z",
        monotonic_elapsed_ms=0,
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"aggregate_kind": "tournament"}, "AggregateKind"),
        ({"aggregate_id": "tournament:show"}, "StableIdentifier"),
        ({"aggregate_id": StableIdentifier("field:wrong")}, "namespace"),
        ({"event_kind": "tournament_configured"}, "EventKind"),
    ],
)
def test_event_intent_rejects_every_open_type_or_namespace(
    changes: dict[str, object], message: str
) -> None:
    valid = _application_request().events[0]
    with pytest.raises(ContractError, match=message):
        replace(valid, **changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"principal_id": StableIdentifier("field:not-actor")}, "namespace"),
        ({"principal_id": "actor:judge"}, "StableIdentifier"),
        ({"command": object()}, "CommandEnvelope"),
        ({"events": ()}, "nonempty"),
        ({"events": []}, "nonempty"),
        ({"events": (object(),)}, "EventIntent"),
        ({"result_schema_version": ""}, "nonempty"),
        ({"result_schema_version": 1}, "nonempty"),
        ({"result": []}, "mapping"),
        ({"occurred_at_utc": "not-utc"}, "UTC"),
        ({"monotonic_elapsed_ms": True}, "non-negative"),
        ({"monotonic_elapsed_ms": "0"}, "non-negative"),
        ({"monotonic_elapsed_ms": -1}, "non-negative"),
    ],
)
def test_command_request_rejects_every_open_boundary(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ContractError, match=message):
        replace(_application_request(), **changes)


def test_command_request_rejects_duplicate_and_inexact_aggregate_scope() -> None:
    request = _application_request()
    with pytest.raises(ContractError, match="at most one"):
        replace(request, events=(request.events[0], request.events[0]))
    other = EventIntent(
        AggregateKind.TOURNAMENT,
        StableIdentifier("tournament:other"),
        EventKind.TOURNAMENT_CONFIGURED,
    )
    with pytest.raises(ContractError, match="exactly match"):
        replace(request, events=(other,))
    with pytest.raises(ContractError, match="maximum"):
        replace(request, result={"too_large": "x" * 1_048_576})


def test_command_request_snapshots_result_mapping_immutably() -> None:
    original: dict[str, object] = {
        "accepted": True,
        "nested": {"mark": 3},
        "members": [{"field": "field:a"}],
    }
    request = replace(_application_request(), result=original)
    original["accepted"] = False
    nested = original["nested"]
    members = original["members"]
    assert isinstance(nested, dict)
    assert isinstance(members, list)
    nested["mark"] = 99
    members.append({"field": "field:b"})
    assert request.result == {
        "accepted": True,
        "nested": {"mark": 3},
        "members": ({"field": "field:a"},),
    }
    with pytest.raises(TypeError):
        request.result["accepted"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        request.result["nested"]["mark"] = 99  # type: ignore[index]
    with pytest.raises(AttributeError):
        request.result["members"].append({"field": "field:b"})  # type: ignore[union-attr]


def test_command_event_catalog_rejects_every_batch_and_single_mismatch() -> None:
    request = _application_request()
    field = EventIntent(AggregateKind.FIELD, StableIdentifier("field:a"), EventKind.FIELD_ISSUED)
    batch = EventIntent(
        AggregateKind.ISSUE_BATCH,
        StableIdentifier("issue_batch:a"),
        EventKind.ISSUE_BATCH_ISSUED,
    )
    batch_command = replace(
        request.command,
        kind=CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
        target_aggregate=batch.aggregate_id,
        expected_versions=(("field:a", 1), ("issue_batch:a", 0)),
    )
    validate_command_event_intents(batch_command, (field, batch))
    invalid_batches = (
        (replace(batch_command, target_aggregate=StableIdentifier("field:a")), (field, batch)),
        (batch_command, (field,)),
        (batch_command, (field, batch, batch)),
        (batch_command, (field, replace(batch, aggregate_id=StableIdentifier("issue_batch:b")))),
        (batch_command, (field, replace(batch, event_kind=EventKind.CHECKPOINT_ANCHORED))),
        (batch_command, (batch,)),
        (batch_command, (replace(field, event_kind=EventKind.FIELD_SETTLED), batch)),
    )
    for command, events in invalid_batches:
        with pytest.raises(ContractError, match="batch issue"):
            validate_command_event_intents(command, events)

    with pytest.raises(ContractError, match="not supported"):
        validate_command_event_intents(
            replace(request.command, kind=CommandKind.PREPARE_FORECAST), request.events
        )
    with pytest.raises(ContractError, match="exactly one"):
        validate_command_event_intents(request.command, request.events * 2)
    for wrong in (
        EventIntent(
            AggregateKind.ROUND,
            StableIdentifier("round:wrong"),
            EventKind.TOURNAMENT_CONFIGURED,
        ),
        replace(request.events[0], event_kind=EventKind.TOURNAMENT_CLOSED),
        replace(request.events[0], aggregate_id=StableIdentifier("tournament:other")),
    ):
        with pytest.raises(ContractError, match="do not match"):
            validate_command_event_intents(request.command, (wrong,))


def test_u5_multi_event_commands_require_the_material_event_as_primary_target() -> None:
    request = _application_request()
    epoch = EventIntent(
        AggregateKind.EPOCH,
        StableIdentifier("epoch:revision-a"),
        EventKind.ROUND_EPOCH_FROZEN,
    )
    round_freeze = EventIntent(
        AggregateKind.ROUND,
        StableIdentifier("round:revision-a"),
        EventKind.ROUND_FROZEN,
    )
    secondary_round_target = replace(
        request.command,
        kind=CommandKind.FREEZE_EVIDENCE_EPOCH,
        target_aggregate=round_freeze.aggregate_id,
        expected_versions=(
            (str(epoch.aggregate_id), 0),
            (str(round_freeze.aggregate_id), 1),
        ),
    )
    with pytest.raises(ContractError, match="evidence epoch freeze"):
        validate_command_event_intents(secondary_round_target, (epoch, round_freeze))

    ingress = EventIntent(
        AggregateKind.FIELD_INGRESS,
        StableIdentifier("field_ingress:revision-a"),
        EventKind.FIELD_ROSTER_REVISED,
    )
    field = EventIntent(
        AggregateKind.FIELD,
        StableIdentifier("field:a"),
        EventKind.FIELD_SUPERSEDED,
    )
    secondary_field_target = replace(
        request.command,
        kind=CommandKind.REVISE_FIELD_ROSTER,
        target_aggregate=field.aggregate_id,
        expected_versions=((str(field.aggregate_id), 1), (str(ingress.aggregate_id), 0)),
    )
    with pytest.raises(ContractError, match="field roster revision"):
        validate_command_event_intents(secondary_field_target, (ingress, field))

    live_settlement = EventIntent(
        AggregateKind.SETTLEMENT,
        StableIdentifier("settlement:live-a"),
        EventKind.LIVE_RACE_SETTLED,
    )
    field_settlement = replace(field, event_kind=EventKind.FIELD_SETTLED)
    secondary_live_field_target = replace(
        request.command,
        kind=CommandKind.SETTLE_LIVE_RACE,
        target_aggregate=field_settlement.aggregate_id,
        expected_versions=(
            (str(field_settlement.aggregate_id), 1),
            (str(live_settlement.aggregate_id), 0),
        ),
    )
    with pytest.raises(ContractError, match="live settlement"):
        validate_command_event_intents(
            secondary_live_field_target,
            (live_settlement, field_settlement),
        )

    result = EventIntent(
        AggregateKind.RESULT,
        StableIdentifier("result:revision-a"),
        EventKind.RESULT_SUPERSEDED,
    )
    settlement = EventIntent(
        AggregateKind.SETTLEMENT,
        StableIdentifier("settlement:revision-a"),
        EventKind.LIVE_RACE_SETTLED,
    )
    secondary_settlement_target = replace(
        request.command,
        kind=CommandKind.SUPERSEDE_AND_SETTLE_RESULT,
        target_aggregate=settlement.aggregate_id,
        expected_versions=(
            (str(field.aggregate_id), 1),
            (str(result.aggregate_id), 1),
            (str(settlement.aggregate_id), 0),
        ),
    )
    with pytest.raises(ContractError, match="atomic correction"):
        validate_command_event_intents(
            secondary_settlement_target,
            (result, settlement, field),
        )
