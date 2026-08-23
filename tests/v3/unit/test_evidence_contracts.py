from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.evidence import (
    ContextProperty,
    EvidencePacket,
    ResultObservation,
    TargetContext,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.contracts.statuses import (
    AggregateLifecycle,
    LifecycleAggregateKind,
    LifecycleStatus,
    OfficialResult,
    ResultStatus,
    admit_raw_completion,
)


def _context() -> TargetContext:
    return TargetContext(
        event_code="underhand",
        size_mm=300,
        material_code="eucalyptus",
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        properties=(
            ContextProperty(
                code="density",
                value="720.5",
                unit="kg_m3",
                missing_reason=None,
            ),
        ),
    )


def _observation() -> ResultObservation:
    return ResultObservation(
        evidence_id=StableIdentifier("evidence:result-17"),
        competitor_id=StableIdentifier("competitor:opaque-1"),
        tournament_id=StableIdentifier("tournament:2026-show"),
        round_id=StableIdentifier("round:heat-1"),
        field_id=StableIdentifier("field:heat-1-a"),
        context=_context(),
        observation_sequence=17,
        occurred_at_utc="2026-08-22T17:30:00.000Z",
        issued_mark=8,
        completion_clock_ms=42000,
        placing=1,
        gap_ms=0,
        result=OfficialResult(
            status=ResultStatus.COMPLETION,
            raw_time_ms=34000,
            penalty_ms=None,
            revision=1,
            supersedes_revision=None,
        ),
        source_digest="a" * 64,
    )


def test_completion_admission_is_explicit_and_never_uses_adjusted_time() -> None:
    completion = OfficialResult(ResultStatus.COMPLETION, 34000, None, 1, None)
    admitted = admit_raw_completion(completion)
    assert admitted is not None
    assert admitted.raw_time_ms == 34000
    assert admitted.source_revision == 1

    penalty = OfficialResult(ResultStatus.PENALTY, 34000, 5000, 1, None)
    assert admit_raw_completion(penalty) is None


def test_aggregate_lifecycle_states_and_transitions_are_closed() -> None:
    prepared = AggregateLifecycle(LifecycleAggregateKind.FIELD, LifecycleStatus.FIELD_PREPARED)
    issued = prepared.transition_to(LifecycleStatus.FIELD_ISSUED)
    assert issued.status is LifecycleStatus.FIELD_ISSUED
    assert AggregateLifecycle.from_dict(issued.to_dict()) == issued

    with pytest.raises(ContractError, match="illegal"):
        prepared.transition_to(LifecycleStatus.FIELD_SETTLED)
    with pytest.raises(ContractError, match="does not belong"):
        AggregateLifecycle(LifecycleAggregateKind.FIELD, LifecycleStatus.TOURNAMENT_OPEN)

    value = issued.to_dict()
    value["status"] = "field_invented"
    with pytest.raises(ContractError, match="unknown"):
        AggregateLifecycle.from_dict(value)
    with pytest.raises(ContractError, match="LifecycleAggregateKind"):
        AggregateLifecycle("field", LifecycleStatus.FIELD_PREPARED)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="LifecycleStatus"):
        AggregateLifecycle(LifecycleAggregateKind.FIELD, "field_prepared")  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="next status"):
        prepared.transition_to("field_issued")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("aggregate_kind", "current", "next_status"),
    [
        (
            LifecycleAggregateKind.TOURNAMENT,
            LifecycleStatus.TOURNAMENT_CONFIGURED,
            LifecycleStatus.TOURNAMENT_OPEN,
        ),
        (
            LifecycleAggregateKind.TOURNAMENT,
            LifecycleStatus.TOURNAMENT_OPEN,
            LifecycleStatus.TOURNAMENT_CLOSED,
        ),
        (
            LifecycleAggregateKind.ROUND,
            LifecycleStatus.ROUND_CONFIGURED,
            LifecycleStatus.ROUND_FROZEN,
        ),
        (
            LifecycleAggregateKind.ROUND,
            LifecycleStatus.ROUND_FROZEN,
            LifecycleStatus.ROUND_CLOSING,
        ),
        (
            LifecycleAggregateKind.ROUND,
            LifecycleStatus.ROUND_CLOSING,
            LifecycleStatus.ROUND_CLOSED,
        ),
        (
            LifecycleAggregateKind.FIELD,
            LifecycleStatus.FIELD_PREPARED,
            LifecycleStatus.FIELD_SUPERSEDED,
        ),
        (
            LifecycleAggregateKind.FIELD,
            LifecycleStatus.FIELD_SUPERSEDED,
            LifecycleStatus.FIELD_PREPARED,
        ),
        (
            LifecycleAggregateKind.FIELD,
            LifecycleStatus.FIELD_PREPARED,
            LifecycleStatus.FIELD_ISSUED,
        ),
        (
            LifecycleAggregateKind.FIELD,
            LifecycleStatus.FIELD_ISSUED,
            LifecycleStatus.FIELD_SETTLED,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_QUEUED,
            LifecycleStatus.JOB_LEASED,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_LEASED,
            LifecycleStatus.JOB_SUCCEEDED,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_LEASED,
            LifecycleStatus.JOB_INVALID,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_LEASED,
            LifecycleStatus.JOB_RETRYABLE_FAILED,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_RETRYABLE_FAILED,
            LifecycleStatus.JOB_QUEUED,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_LEASED,
            LifecycleStatus.JOB_STALE,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_LEASED,
            LifecycleStatus.JOB_PERMANENT_FAILED,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_QUEUED,
            LifecycleStatus.JOB_CANCELLED,
        ),
    ],
)
def test_plan_lifecycle_transitions_are_representable(
    aggregate_kind: LifecycleAggregateKind,
    current: LifecycleStatus,
    next_status: LifecycleStatus,
) -> None:
    assert AggregateLifecycle(aggregate_kind, current).transition_to(next_status) == (
        AggregateLifecycle(aggregate_kind, next_status)
    )


@pytest.mark.parametrize(
    ("aggregate_kind", "current", "forbidden"),
    [
        (
            LifecycleAggregateKind.TOURNAMENT,
            LifecycleStatus.TOURNAMENT_CONFIGURED,
            LifecycleStatus.TOURNAMENT_CLOSED,
        ),
        (
            LifecycleAggregateKind.ROUND,
            LifecycleStatus.ROUND_FROZEN,
            LifecycleStatus.ROUND_CLOSED,
        ),
        (
            LifecycleAggregateKind.FIELD,
            LifecycleStatus.FIELD_SUPERSEDED,
            LifecycleStatus.FIELD_ISSUED,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_LEASED,
            LifecycleStatus.JOB_QUEUED,
        ),
        (
            LifecycleAggregateKind.JOB,
            LifecycleStatus.JOB_RETRYABLE_FAILED,
            LifecycleStatus.JOB_SUCCEEDED,
        ),
    ],
)
def test_plan_lifecycle_forbidden_shortcuts_fail_closed(
    aggregate_kind: LifecycleAggregateKind,
    current: LifecycleStatus,
    forbidden: LifecycleStatus,
) -> None:
    with pytest.raises(ContractError, match="illegal lifecycle transition"):
        AggregateLifecycle(aggregate_kind, current).transition_to(forbidden)


@pytest.mark.parametrize(
    "status", [ResultStatus.DNF, ResultStatus.DQ, ResultStatus.DNS, ResultStatus.VOID]
)
def test_nonfinish_and_void_states_cannot_carry_or_manufacture_raw_time(
    status: ResultStatus,
) -> None:
    with pytest.raises(ContractError, match="raw_time_ms"):
        OfficialResult(status, 34000, None, 1, None)
    assert admit_raw_completion(OfficialResult(status, None, None, 1, None)) is None


def test_nonfinish_observation_cannot_carry_finish_facts() -> None:
    observation = _observation()
    with pytest.raises(ContractError, match="nonfinish"):
        ResultObservation(
            **{
                **{
                    field: getattr(observation, field) for field in observation.__dataclass_fields__
                },
                "result": OfficialResult(ResultStatus.DNF, None, None, 1, None),
            }
        )


def test_status_and_numeric_boundaries_fail_closed_without_coercion() -> None:
    with pytest.raises(ContractError, match="ResultStatus"):
        OfficialResult("completion", 34000, None, 1, None)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="positive integer"):
        OfficialResult(ResultStatus.COMPLETION, 34.0, None, 1, None)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="penalty_ms"):
        OfficialResult(ResultStatus.PENALTY, 34000, None, 1, None)
    with pytest.raises(ContractError, match="only valid"):
        OfficialResult(ResultStatus.COMPLETION, 34000, 5000, 1, None)
    with pytest.raises(ContractError, match="only valid"):
        OfficialResult(ResultStatus.DNF, None, 5000, 1, None)


def test_official_result_decoder_and_admission_reject_unknown_types() -> None:
    result = OfficialResult(ResultStatus.COMPLETION, 34000, None, 1, None)
    assert OfficialResult.from_dict(result.to_dict()) == result
    encoded = result.to_dict()
    encoded["status"] = "invented"
    with pytest.raises(ContractError, match="unknown official"):
        OfficialResult.from_dict(encoded)
    with pytest.raises(ContractError, match="OfficialResult"):
        admit_raw_completion(object())  # type: ignore[arg-type]


def test_supersession_is_monotonic_and_explicit() -> None:
    corrected = OfficialResult(ResultStatus.COMPLETION, 33000, None, 2, 1)
    assert corrected.supersedes_revision == 1
    with pytest.raises(ContractError, match="supersedes"):
        OfficialResult(ResultStatus.COMPLETION, 33000, None, 2, 2)
    with pytest.raises(ContractError, match="revision 1"):
        OfficialResult(ResultStatus.COMPLETION, 33000, None, 1, 0)


def test_context_requires_explicit_missingness_and_canonical_decimals() -> None:
    missing = ContextProperty("moisture", None, "percent", "not_observed")
    assert missing.value is None
    with pytest.raises(ContractError, match="missing_reason"):
        ContextProperty("moisture", None, "percent", None)
    with pytest.raises(ContractError, match="must be absent"):
        ContextProperty("moisture", "14.2", "percent", "not_observed")
    with pytest.raises(ContractError, match="canonical decimal"):
        ContextProperty("density", "720.50", "kg_m3", None)
    with pytest.raises(ContractError, match="string"):
        ContextProperty("density", 720.5, "kg_m3", None)  # type: ignore[arg-type]


def test_context_round_trip_and_collection_rules_are_closed() -> None:
    context = _context()
    assert TargetContext.from_dict(context.to_dict()) == context
    assert ContextProperty.from_dict(context.properties[0].to_dict()) == context.properties[0]
    with pytest.raises(ContractError, match="immutable tuple"):
        replace(context, properties=list(context.properties))  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="unique and sorted"):
        replace(
            context,
            properties=(
                ContextProperty("moisture", "10", "percent", None),
                ContextProperty("density", "720", "kg_m3", None),
            ),
        )
    encoded = context.to_dict()
    encoded["properties"] = "not-an-array"
    with pytest.raises(ContractError, match="JSON array"):
        TargetContext.from_dict(encoded)


def test_observation_round_trip_and_closed_nested_types() -> None:
    observation = _observation()
    assert ResultObservation.from_dict(observation.to_dict()) == observation
    with pytest.raises(ContractError, match="TargetContext"):
        replace(observation, context=object())
    with pytest.raises(ContractError, match="OfficialResult"):
        replace(observation, result=object())
    with pytest.raises(ContractError, match="SHA-256"):
        replace(observation, source_digest="not-a-digest")
    with pytest.raises(ContractError, match="non-negative integer"):
        replace(observation, gap_ms=-1)
    assert (
        replace(
            observation, completion_clock_ms=None, placing=None, gap_ms=None
        ).completion_clock_ms
        is None
    )
    with pytest.raises(ContractError, match="lower-case token"):
        replace(observation.context, event_code="Underhand")
    with pytest.raises(ContractError, match="version identifier"):
        replace(observation.context, taxonomy_version="not-versioned")
    with pytest.raises(ContractError, match="StableIdentifier"):
        replace(observation, evidence_id="evidence:raw-string")  # type: ignore[arg-type]


def test_evidence_packet_round_trip_is_closed_and_digest_verified() -> None:
    observation = _observation()
    packet = EvidencePacket.create(
        competitor_id=observation.competitor_id,
        target_context=_context(),
        observations=(observation,),
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:2026-08-01",
        tournament_epoch_id=StableIdentifier("epoch:round-2-v1"),
        tournament_event_sequence=17,
    )

    restored = EvidencePacket.from_dict(packet.to_dict())
    assert restored == packet
    assert restored.content_digest == packet.recompute_digest()
    assert packet.eligible_raw_times_ms == (34000,)
    with pytest.raises(FrozenInstanceError):
        packet.tournament_event_sequence = 18  # type: ignore[misc]


@pytest.mark.parametrize("mutation", ["missing", "extra", "unknown_schema", "bad_digest"])
def test_evidence_packet_decoder_fails_closed(mutation: str) -> None:
    observation = _observation()
    packet = EvidencePacket.create(
        competitor_id=observation.competitor_id,
        target_context=_context(),
        observations=(observation,),
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:2026-08-01",
        tournament_epoch_id=StableIdentifier("epoch:round-2-v1"),
        tournament_event_sequence=17,
    )
    value = packet.to_dict()
    if mutation == "missing":
        del value["content_digest"]
    elif mutation == "extra":
        value["name"] = "PII must not enter the packet"
    elif mutation == "unknown_schema":
        value["schema_version"] = "strathmark-v3-evidence-v999"
    else:
        value["content_digest"] = "b" * 64

    with pytest.raises(ContractError):
        EvidencePacket.from_dict(value)


def test_observation_must_match_packet_identity_and_causal_boundary() -> None:
    observation = _observation()
    with pytest.raises(ContractError, match="competitor"):
        EvidencePacket.create(
            competitor_id=StableIdentifier("competitor:someone-else"),
            target_context=_context(),
            observations=(observation,),
            taxonomy_version="taxonomy:v1",
            conversion_version="conversion:v1",
            historical_cutoff_key="history:2026-08-01",
            tournament_epoch_id=StableIdentifier("epoch:round-2-v1"),
            tournament_event_sequence=17,
        )
    with pytest.raises(ContractError, match="sequence"):
        EvidencePacket.create(
            competitor_id=observation.competitor_id,
            target_context=_context(),
            observations=(observation,),
            taxonomy_version="taxonomy:v1",
            conversion_version="conversion:v1",
            historical_cutoff_key="history:2026-08-01",
            tournament_epoch_id=StableIdentifier("epoch:round-2-v1"),
            tournament_event_sequence=16,
        )


def test_packet_rejects_mutable_collections_context_version_drift_and_bad_order() -> None:
    observation = _observation()
    packet = EvidencePacket.create(
        competitor_id=observation.competitor_id,
        target_context=_context(),
        observations=(observation,),
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:2026-08-01",
        tournament_epoch_id=StableIdentifier("epoch:round-2-v1"),
        tournament_event_sequence=18,
    )
    with pytest.raises(ContractError, match="TargetContext"):
        replace(packet, target_context=object())
    with pytest.raises(ContractError, match="immutable observation tuple"):
        replace(packet, observations=[observation])  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="taxonomy_version"):
        replace(packet, taxonomy_version="taxonomy:v2")
    with pytest.raises(ContractError, match="conversion_version"):
        replace(packet, conversion_version="conversion:v2")

    later = replace(
        observation,
        evidence_id=StableIdentifier("evidence:result-18"),
        observation_sequence=18,
        source_digest="b" * 64,
    )
    two = EvidencePacket.create(
        competitor_id=observation.competitor_id,
        target_context=_context(),
        observations=(observation, later),
        taxonomy_version="taxonomy:v1",
        conversion_version="conversion:v1",
        historical_cutoff_key="history:2026-08-01",
        tournament_epoch_id=StableIdentifier("epoch:round-2-v1"),
        tournament_event_sequence=18,
    )
    with pytest.raises(ContractError, match="unique and sorted"):
        replace(two, observations=(later, observation))
    with pytest.raises(ContractError, match="unique and sorted"):
        replace(two, observations=(observation, observation))

    encoded = packet.to_dict()
    encoded["observations"] = "not-an-array"
    with pytest.raises(ContractError, match="JSON array"):
        EvidencePacket.from_dict(encoded)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-99-22T17:30:00.000Z",
        "2026-08-31T25:30:00.000Z",
        "2026-02-29T17:30:00.000Z",
        "2026-08-22T17:30:00Z",
        "2026-08-22T17:30:00.000+00:00",
    ],
)
def test_observation_rejects_impossible_or_noncanonical_utc(timestamp: str) -> None:
    observation = _observation()
    arguments = {field: getattr(observation, field) for field in observation.__dataclass_fields__}
    arguments["occurred_at_utc"] = timestamp
    with pytest.raises(ContractError, match="canonical UTC"):
        ResultObservation(**arguments)
