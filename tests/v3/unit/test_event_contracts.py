from __future__ import annotations

from dataclasses import replace

import pytest

import strathmark.v3.contracts.events as event_contracts
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import (
    MAX_BLOB_BYTES,
    MAX_INLINE_PAYLOAD_BYTES,
    BlobReference,
    BlobReferenceV2,
    BlobRetentionClass,
    CommandEnvelope,
    CommandKind,
    InlinePayload,
)
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)


def _command() -> CommandEnvelope:
    return CommandEnvelope(
        kind=CommandKind.RECORD_RESULT,
        command_id=IdempotencyKey("command:record-result-1"),
        target_aggregate=StableIdentifier("field:heat-a"),
        expected_versions=(("field:heat-a", 2), ("tournament:show", 7)),
        actor_id=StableIdentifier("actor:tournament-manager"),
        payload=InlinePayload.from_value({"result_id": "result:17", "raw_time_ms": 34000}),
    )


def test_inline_payload_is_exact_canonical_json_and_bounded() -> None:
    payload = InlinePayload.from_value({"z": 1, "a": "value"})
    assert payload.canonical_json == '{"a":"value","z":1}'
    assert payload.digest == "5aaca98f7ea3b0364fe69ff79e271b638a88fad50ad5482ac2829be070200775"
    assert payload.to_value() == {"a": "value", "z": 1}

    with pytest.raises(ContractError, match="canonical"):
        InlinePayload('{"z":1, "a":"value"}', payload.digest)
    with pytest.raises(ContractError, match="maximum"):
        InlinePayload.from_value({"value": "x" * MAX_INLINE_PAYLOAD_BYTES})


def test_inline_payload_rejects_every_noncanonical_entry_path() -> None:
    with pytest.raises(ContractError, match="text"):
        InlinePayload(123, "a" * 64)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="bounded canonical"):
        InlinePayload("{", "a" * 64)
    with pytest.raises(ContractError, match="JSON object"):
        InlinePayload("[]", canonical_digest([]))
    with pytest.raises(ContractError, match="digest mismatch"):
        InlinePayload("{}", "a" * 64)
    with pytest.raises(ContractError, match="mapping"):
        InlinePayload.from_value([])  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="not canonical"):
        InlinePayload.from_value({"value": object()})


def test_blob_references_are_required_above_the_inline_boundary_and_bounded() -> None:
    reference = BlobReference(
        blob_id=StableIdentifier("blob:response-1"),
        digest="a" * 64,
        byte_count=MAX_INLINE_PAYLOAD_BYTES + 1,
        media_type="application/json",
    )
    assert BlobReference.from_dict(reference.to_dict()) == reference
    with pytest.raises(ContractError, match="inline boundary"):
        BlobReference(
            StableIdentifier("blob:small"), "a" * 64, MAX_INLINE_PAYLOAD_BYTES, "application/json"
        )


def test_v2_blob_reference_binds_content_identity_schema_retention_and_round_trips() -> None:
    digest = "b" * 64
    reference = BlobReferenceV2(
        blob_id=deterministic_identifier("blob", {"digest": digest}),
        digest=digest,
        byte_count=MAX_INLINE_PAYLOAD_BYTES + 1,
        media_type="application/json",
        payload_schema_version="strathmark-v3-model-output-v1",
        retention_class=BlobRetentionClass.REQUIRED,
    )
    assert BlobReferenceV2.from_dict(reference.to_dict()) == reference
    assert (
        CommandEnvelope.from_dict(replace(_command(), payload=reference).to_dict()).payload
        == reference
    )
    with pytest.raises(ContractError, match="content identity"):
        replace(reference, blob_id=StableIdentifier("blob:arbitrary"))
    with pytest.raises(ContractError, match="inline boundary"):
        replace(reference, byte_count=MAX_INLINE_PAYLOAD_BYTES)
    with pytest.raises(ContractError, match="maximum"):
        replace(reference, byte_count=MAX_BLOB_BYTES + 1)
    with pytest.raises(ContractError, match="media_type"):
        replace(reference, media_type="text/plain")
    with pytest.raises(ContractError, match="payload_schema_version"):
        replace(reference, payload_schema_version="bad")
    with pytest.raises(ContractError, match="retention_class"):
        replace(reference, retention_class="required")
    invalid_retention = reference.to_dict()
    invalid_retention["retention_class"] = "unknown"
    with pytest.raises(ContractError, match="unknown blob retention"):
        BlobReferenceV2.from_dict(invalid_retention)
    with pytest.raises(ContractError, match="maximum"):
        BlobReference(
            StableIdentifier("blob:huge"), "a" * 64, MAX_BLOB_BYTES + 1, "application/json"
        )
    with pytest.raises(ContractError, match="media_type"):
        BlobReference(
            StableIdentifier("blob:wrong-media"),
            "a" * 64,
            MAX_INLINE_PAYLOAD_BYTES + 1,
            "text/plain",
        )
    with pytest.raises(ContractError, match="inline boundary"):
        BlobReference(
            StableIdentifier("blob:boolean-size"),
            "a" * 64,
            True,  # type: ignore[arg-type]
            "application/json",
        )


def test_command_expected_versions_are_sorted_and_closed() -> None:
    command = _command()
    assert command.expected_versions == (("field:heat-a", 2), ("tournament:show", 7))
    assert CommandEnvelope.from_dict(command.to_dict()) == command

    with pytest.raises(ContractError, match="sorted"):
        CommandEnvelope(
            kind=CommandKind.RECORD_RESULT,
            command_id=IdempotencyKey("command:bad-order"),
            target_aggregate=StableIdentifier("field:heat-a"),
            expected_versions=(("tournament:show", 7), ("field:heat-a", 2)),
            actor_id=StableIdentifier("actor:tournament-manager"),
            payload=InlinePayload.from_value({}),
        )
    assert command.payload_digest == command.payload.digest


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "record_result"}, "CommandKind"),
        ({"command_id": StableIdentifier("command:not-idempotency-type")}, "IdempotencyKey"),
        ({"target_aggregate": "field:heat-a"}, "StableIdentifier"),
        ({"expected_versions": []}, "immutable tuple"),
        ({"expected_versions": (("field:heat-a",),)}, "pairs"),
        ({"expected_versions": (("field:heat-a", 2), ("field:heat-a", 2))}, "repeat"),
        ({"expected_versions": (("field:heat-a", True),)}, "non-negative integers"),
        ({"expected_versions": (("tournament:show", 7),)}, "target aggregate"),
        ({"payload": object()}, "InlinePayload"),
    ],
)
def test_command_constructor_rejects_nonclosed_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ContractError, match=message):
        replace(_command(), **changes)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("kind", "invented", "unknown command"),
        ("expected_versions", "not-an-array", "JSON array"),
        ("expected_versions", [["field:heat-a"]], "JSON pairs"),
        ("payload_type", "invented", "unknown payload"),
    ],
)
def test_command_decoder_rejects_unknown_shapes(field: str, value: object, message: str) -> None:
    encoded = _command().to_dict()
    encoded[field] = value
    with pytest.raises(ContractError, match=message):
        CommandEnvelope.from_dict(encoded)


def test_command_decoder_supports_a_blob_reference() -> None:
    blob = BlobReference(
        StableIdentifier("blob:command-payload"),
        "a" * 64,
        MAX_INLINE_PAYLOAD_BYTES + 1,
        "application/octet-stream",
    )
    command = replace(_command(), payload=blob)
    assert CommandEnvelope.from_dict(command.to_dict()) == command
    encoded = command.to_dict()
    encoded["payload"] = []
    with pytest.raises(ContractError, match="blob payload must be an object"):
        CommandEnvelope.from_dict(encoded)


def test_plan_transition_command_vocabulary_is_closed_and_round_trips() -> None:
    required = {
        CommandKind.CONFIGURE_TOURNAMENT,
        CommandKind.CONFIGURE_ROUND,
        CommandKind.BEGIN_ROUND_CLOSING,
        CommandKind.SUPERSEDE_FIELD,
        CommandKind.REGENERATE_FIELD,
        CommandKind.QUEUE_JOB,
        CommandKind.LEASE_JOB,
        CommandKind.SUCCEED_JOB,
        CommandKind.INVALIDATE_JOB,
        CommandKind.RECORD_RETRYABLE_JOB_FAILURE,
        CommandKind.REQUEUE_JOB,
        CommandKind.MARK_JOB_STALE,
        CommandKind.RECORD_PERMANENT_JOB_FAILURE,
        CommandKind.CANCEL_JOB,
        CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
    }
    for kind in required:
        command = replace(_command(), kind=kind)
        assert CommandEnvelope.from_dict(command.to_dict()) == command


def test_event_hash_commits_both_prior_chains_and_round_trips() -> None:
    event = EventEnvelope.create(
        event_id=StableIdentifier("event:result-recorded-17"),
        kind=EventKind.RESULT_RECORDED,
        aggregate_kind=AggregateKind.FIELD,
        aggregate_id=StableIdentifier("field:heat-a"),
        aggregate_version=3,
        global_sequence=19,
        prior_global_digest="1" * 64,
        prior_aggregate_digest="2" * 64,
        occurred_at_utc="2026-08-22T17:30:00.000Z",
        monotonic_elapsed_ms=91823,
        command=_command(),
    )
    assert EventEnvelope.from_dict(event.to_dict()) == event
    assert event.event_digest == event.recompute_digest()

    changed = event.to_dict()
    changed["prior_global_digest"] = "3" * 64
    with pytest.raises(ContractError, match="digest"):
        EventEnvelope.from_dict(changed)


def test_event_constructor_rejects_nonclosed_types_and_version_gaps() -> None:
    event = EventEnvelope.create(
        event_id=StableIdentifier("event:base"),
        kind=EventKind.RESULT_RECORDED,
        aggregate_kind=AggregateKind.FIELD,
        aggregate_id=StableIdentifier("field:heat-a"),
        aggregate_version=3,
        global_sequence=19,
        prior_global_digest="1" * 64,
        prior_aggregate_digest="2" * 64,
        occurred_at_utc="2026-08-22T17:30:00.000Z",
        monotonic_elapsed_ms=91823,
        command=_command(),
    )
    for changes, message in (
        ({"kind": "result_recorded"}, "EventKind"),
        ({"aggregate_kind": "field"}, "AggregateKind"),
        ({"aggregate_id": "field:heat-a"}, "StableIdentifier"),
        ({"command": object()}, "CommandEnvelope"),
        ({"aggregate_version": 4}, "expected version"),
    ):
        with pytest.raises(ContractError, match=message):
            replace(event, **changes)


def test_system_aggregate_uses_an_explicit_namespace_without_namespace_coercion() -> None:
    command = replace(
        _command(),
        target_aggregate=StableIdentifier("system:root"),
        expected_versions=(("system:root", 0),),
    )
    event = EventEnvelope.create(
        event_id=StableIdentifier("event:system-stop"),
        kind=EventKind.EMERGENCY_STOPPED,
        aggregate_kind=AggregateKind.SYSTEM,
        aggregate_id=StableIdentifier("system:root"),
        aggregate_version=1,
        global_sequence=20,
        prior_global_digest="1" * 64,
        prior_aggregate_digest="0" * 64,
        occurred_at_utc="2026-08-22T17:30:00.000Z",
        monotonic_elapsed_ms=91824,
        command=command,
    )
    assert event.aggregate_kind is AggregateKind.SYSTEM

    wrong_command = replace(
        command,
        target_aggregate=StableIdentifier("field:not-system"),
        expected_versions=(("field:not-system", 0),),
    )
    with pytest.raises(ContractError, match="namespace 'system'"):
        EventEnvelope.create(
            event_id=StableIdentifier("event:wrong-system-namespace"),
            kind=EventKind.EMERGENCY_STOPPED,
            aggregate_kind=AggregateKind.SYSTEM,
            aggregate_id=StableIdentifier("field:not-system"),
            aggregate_version=1,
            global_sequence=21,
            prior_global_digest="1" * 64,
            prior_aggregate_digest="0" * 64,
            occurred_at_utc="2026-08-22T17:30:00.000Z",
            monotonic_elapsed_ms=91825,
            command=wrong_command,
        )


def test_event_maximum_canonical_size_is_checked_before_storage(monkeypatch) -> None:
    monkeypatch.setattr(event_contracts, "MAX_EVENT_CANONICAL_BYTES", 128)
    with pytest.raises(ContractError, match="maximum canonical size"):
        EventEnvelope.create(
            event_id=StableIdentifier("event:too-large"),
            kind=EventKind.RESULT_RECORDED,
            aggregate_kind=AggregateKind.FIELD,
            aggregate_id=StableIdentifier("field:heat-a"),
            aggregate_version=3,
            global_sequence=19,
            prior_global_digest="1" * 64,
            prior_aggregate_digest="2" * 64,
            occurred_at_utc="2026-08-22T17:30:00.000Z",
            monotonic_elapsed_ms=91823,
            command=_command(),
        )


@pytest.mark.parametrize("mutation", ["missing", "extra", "event_kind", "aggregate_kind", "schema"])
def test_event_decoder_rejects_unknown_or_incomplete_shapes(mutation: str) -> None:
    event = EventEnvelope.create(
        event_id=StableIdentifier("event:result-recorded-17"),
        kind=EventKind.RESULT_RECORDED,
        aggregate_kind=AggregateKind.FIELD,
        aggregate_id=StableIdentifier("field:heat-a"),
        aggregate_version=3,
        global_sequence=19,
        prior_global_digest="1" * 64,
        prior_aggregate_digest="2" * 64,
        occurred_at_utc="2026-08-22T17:30:00.000Z",
        monotonic_elapsed_ms=91823,
        command=_command(),
    )
    value = event.to_dict()
    if mutation == "missing":
        del value["event_digest"]
    elif mutation == "extra":
        value["mutable"] = True
    elif mutation == "event_kind":
        value["kind"] = "made_up"
    elif mutation == "aggregate_kind":
        value["aggregate_kind"] = "made_up"
    else:
        value["schema_version"] = "strathmark-v3-event-v999"
    with pytest.raises(ContractError):
        EventEnvelope.from_dict(value)


def test_plan_transition_event_vocabulary_is_closed_and_round_trips() -> None:
    required = {
        EventKind.TOURNAMENT_CONFIGURED,
        EventKind.ROUND_CONFIGURED,
        EventKind.ROUND_CLOSING_STARTED,
        EventKind.FIELD_SUPERSEDED,
        EventKind.FIELD_REGENERATED,
        EventKind.JOB_SUCCEEDED,
        EventKind.JOB_INVALID,
        EventKind.JOB_RETRYABLE_FAILED,
        EventKind.JOB_REQUEUED,
        EventKind.JOB_STALE,
        EventKind.JOB_PERMANENT_FAILED,
        EventKind.JOB_CANCELLED,
        EventKind.ISSUE_BATCH_ISSUED,
    }
    for sequence, kind in enumerate(sorted(required, key=lambda item: item.value), start=100):
        event = EventEnvelope.create(
            event_id=StableIdentifier(f"event:vocabulary-{sequence}"),
            kind=kind,
            aggregate_kind=AggregateKind.FIELD,
            aggregate_id=StableIdentifier("field:heat-a"),
            aggregate_version=3,
            global_sequence=sequence,
            prior_global_digest="1" * 64,
            prior_aggregate_digest="2" * 64,
            occurred_at_utc="2026-08-22T17:30:00.000Z",
            monotonic_elapsed_ms=sequence,
            command=_command(),
        )
        assert EventEnvelope.from_dict(event.to_dict()) == event


def test_event_envelope_supports_one_declared_nonprimary_aggregate_for_atomic_batch() -> None:
    command = CommandEnvelope(
        kind=CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
        command_id=IdempotencyKey("command:batch-1"),
        target_aggregate=StableIdentifier("issue_batch:batch-1"),
        expected_versions=(("field:heat-a", 2), ("issue_batch:batch-1", 0)),
        actor_id=StableIdentifier("actor:tournament-manager"),
        payload=InlinePayload.from_value({"snapshot": "snapshot:1"}),
    )
    event = EventEnvelope.create(
        event_id=StableIdentifier("event:field-issued-by-batch"),
        kind=EventKind.FIELD_ISSUED,
        aggregate_kind=AggregateKind.FIELD,
        aggregate_id=StableIdentifier("field:heat-a"),
        aggregate_version=3,
        global_sequence=19,
        prior_global_digest="1" * 64,
        prior_aggregate_digest="2" * 64,
        occurred_at_utc="2026-08-22T17:30:00.000Z",
        monotonic_elapsed_ms=91823,
        command=command,
    )
    assert EventEnvelope.from_dict(event.to_dict()) == event


def test_event_sequences_timestamps_and_numeric_types_fail_closed() -> None:
    kwargs = dict(
        event_id=StableIdentifier("event:result-recorded-17"),
        kind=EventKind.RESULT_RECORDED,
        aggregate_kind=AggregateKind.FIELD,
        aggregate_id=StableIdentifier("field:heat-a"),
        aggregate_version=3,
        global_sequence=19,
        prior_global_digest="1" * 64,
        prior_aggregate_digest="2" * 64,
        occurred_at_utc="2026-08-22T17:30:00.000Z",
        monotonic_elapsed_ms=91823,
        command=_command(),
    )
    with pytest.raises(ContractError, match="positive integer"):
        EventEnvelope.create(**{**kwargs, "global_sequence": 19.0})  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="UTC"):
        EventEnvelope.create(**{**kwargs, "occurred_at_utc": "2026-08-22 17:30:00"})
    with pytest.raises(ContractError, match="UTC"):
        EventEnvelope.create(**{**kwargs, "occurred_at_utc": "2026-99-99T99:99:99.999Z"})
    with pytest.raises(ContractError, match="target"):
        EventEnvelope.create(**{**kwargs, "aggregate_id": StableIdentifier("field:different")})
