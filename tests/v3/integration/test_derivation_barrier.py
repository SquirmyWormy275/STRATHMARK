from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.application.lifecycle import LifecycleService, SnapshotKind, UpstreamSnapshot
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import (
    BlobReference,
    CommandEnvelope,
    CommandKind,
    InlinePayload,
)
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import TargetContext
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.epochs import MandatoryReaction
from strathmark.v3.domain.evidence import LiveResultSubmission
from strathmark.v3.infrastructure.sqlite.connection import immediate_transaction, open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import EventStoreConflict, SQLiteEventStore
from strathmark.v3.infrastructure.sqlite.projections import (
    ProjectionConflict,
    ProjectionError,
    SQLiteProjectionStore,
)

NOW = "2026-08-22T01:02:03.004Z"
ACTOR = StableIdentifier("actor:tournament-manager")


def _authority_event(service, kind: EventKind, *, aggregate_id: str | None = None):
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        query = "SELECT envelope_json FROM v3_events WHERE event_kind=?"
        parameters: tuple[object, ...] = (kind.value,)
        if aggregate_id is not None:
            query += " AND aggregate_id=?"
            parameters += (aggregate_id,)
        query += " ORDER BY global_sequence DESC LIMIT 1"
        row = connection.execute(query, parameters).fetchone()
    assert row is not None
    return EventEnvelope.from_dict(json.loads(str(row[0])))


def _event_variant(
    event: EventEnvelope,
    *,
    payload: object | None = None,
    command_kind: CommandKind | None = None,
    event_kind: EventKind | None = None,
    aggregate_kind: AggregateKind | None = None,
    aggregate_id: StableIdentifier | None = None,
    command_id: str = "command:defensive-variant",
) -> EventEnvelope:
    target = aggregate_id or event.aggregate_id
    command = CommandEnvelope(
        command_kind or event.command.kind,
        IdempotencyKey(command_id),
        target,
        ((str(target), event.aggregate_version - 1),),
        ACTOR,
        payload if isinstance(payload, BlobReference) else InlinePayload.from_value(payload or {}),
    )
    return EventEnvelope.create(
        event_id=deterministic_identifier("event", {"command": command_id, "target": str(target)}),
        kind=event_kind or event.kind,
        aggregate_kind=aggregate_kind or event.aggregate_kind,
        aggregate_id=target,
        aggregate_version=event.aggregate_version,
        global_sequence=event.global_sequence,
        prior_global_digest=event.prior_global_digest,
        prior_aggregate_digest=event.prior_aggregate_digest,
        occurred_at_utc=event.occurred_at_utc,
        monotonic_elapsed_ms=event.monotonic_elapsed_ms,
        command=command,
    )


def _append(service, kind, event, aggregate_kind, target, payload, key):
    store = SQLiteEventStore(service.projections.database_path)
    head = store.aggregate_head(str(target))
    command = CommandEnvelope(
        kind,
        IdempotencyKey(f"command:{key}"),
        target,
        ((str(target), 0 if head is None else head[0]),),
        ACTOR,
        InlinePayload.from_value(payload),
    )
    store.execute(
        CommandRequest(
            ACTOR,
            command,
            (EventIntent(aggregate_kind, target, event),),
            "test-result-v1",
            {"accepted": True},
            NOW,
            1,
        ),
        projection_hook=service.projections.apply_events,
    )


def _snapshot(service, snapshot, key):
    service.ingest_snapshot(
        snapshot,
        command_id=IdempotencyKey(f"command:{key}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )


def _bootstrap(tmp_path: Path):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:show")
    heat = StableIdentifier("round:heat")
    field = StableIdentifier("field:heat-a")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "tournament-snapshot",
    )
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            heat,
            1,
            tournament,
            heat,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": ["round:quarter"],
            },
        ),
        "heat-snapshot",
    )
    _append(
        service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "configure-show",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        heat,
        {"configured": True},
        "configure-heat",
    )
    service.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:prior",
        root_round_ids=(heat,),
        command_id=IdempotencyKey("command:open-show"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    epoch, _ = service.freeze_round_epoch(
        heat,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:freeze-root"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    context = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            field,
            1,
            tournament,
            heat,
            {
                "competitor_ids": ["competitor:a", "competitor:b"],
                "target_context": context.to_dict(),
                "stand_ids": ["stand:one", "stand:two"],
            },
        ),
        "field-snapshot",
    )
    _append(
        service,
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        AggregateKind.FIELD,
        field,
        {"round_id": str(heat), "epoch_id": str(epoch.epoch_id), "field_revision": 1},
        "prepare-field",
    )
    _append(
        service,
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        AggregateKind.FIELD,
        field,
        {
            "round_id": str(heat),
            "epoch_id": str(epoch.epoch_id),
            "field_revision": 1,
            "receipt_id": "receipt:heat-a",
            "competitor_ids": ["competitor:a", "competitor:b"],
            "issued_marks": {"competitor:a": 3, "competitor:b": 3},
        },
        "issue-field",
    )
    return service, heat, field


def _bootstrap_prepared_unissued_field(tmp_path: Path):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:show")
    heat = StableIdentifier("round:heat")
    field = StableIdentifier("field:heat-a")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "prepared-tournament",
    )
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            heat,
            1,
            tournament,
            heat,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        "prepared-round",
    )
    _append(
        service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "prepared-configure-tournament",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        heat,
        {"configured": True},
        "prepared-configure-round",
    )
    service.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:prior",
        root_round_ids=(heat,),
        command_id=IdempotencyKey("command:prepared-open"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    epoch, _ = service.freeze_round_epoch(
        heat,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:prepared-epoch"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    content = {
        "competitor_ids": ["competitor:a", "competitor:b"],
        "target_context": TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1").to_dict(),
        "stand_ids": ["stand:one", "stand:two"],
    }
    _snapshot(
        service,
        UpstreamSnapshot(SnapshotKind.FIELD, field, 1, tournament, heat, content),
        "prepared-field",
    )
    _append(
        service,
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        AggregateKind.FIELD,
        field,
        {"round_id": str(heat), "epoch_id": str(epoch.epoch_id), "field_revision": 1},
        "prepared-optimize",
    )
    return service, tournament, heat, field, content


def _bootstrap_empty_closure(tmp_path: Path):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:empty")
    root = StableIdentifier("round:empty-root")
    successor = StableIdentifier("round:empty-next")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "empty-tournament",
    )
    for ordinal, round_id, predecessors, successors in (
        (1, root, [], [str(successor)]),
        (2, successor, [str(root)], []),
    ):
        _snapshot(
            service,
            UpstreamSnapshot(
                SnapshotKind.ROUND,
                round_id,
                1,
                tournament,
                round_id,
                {
                    "round_ordinal": ordinal,
                    "predecessor_round_ids": predecessors,
                    "successor_round_ids": successors,
                },
            ),
            f"empty-round-{ordinal}",
        )
    _append(
        service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "empty-configure-tournament",
    )
    for round_id in (root, successor):
        _append(
            service,
            CommandKind.CONFIGURE_ROUND,
            EventKind.ROUND_CONFIGURED,
            AggregateKind.ROUND,
            round_id,
            {"configured": True},
            f"empty-configure-{round_id.value.split(':')[1]}",
        )
    service.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:prior",
        root_round_ids=(root,),
        command_id=IdempotencyKey("command:empty-open"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    service.freeze_round_epoch(
        root,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:empty-root-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    _start_round_close(service, root, "empty-root")
    closure_id, _ = service.close_evidence_round(
        root,
        command_id=IdempotencyKey("command:empty-root-close"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    service.freeze_round_epoch(
        successor,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(closure_id,),
        command_id=IdempotencyKey("command:empty-next-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    return service, tournament, root, successor, closure_id


def _submission(field, competitor, status, revision=1):
    raw = 12_000 if status is ResultStatus.COMPLETION else None
    return LiveResultSubmission(
        StableIdentifier(f"evidence:{competitor}-r{revision}"),
        StableIdentifier(f"competitor:{competitor}"),
        StableIdentifier("tournament:show"),
        StableIdentifier("round:heat"),
        field,
        TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
        NOW,
        3,
        15_000 if raw else None,
        1 if raw else None,
        0 if raw else None,
        OfficialResult(status, raw, None, revision, None if revision == 1 else revision - 1),
        canonical_digest({"competitor": competitor, "status": status.value, "revision": revision}),
    )


def _complete_source(service, source, key):
    for reaction in MandatoryReaction:
        service.complete_derivation_reaction(
            source,
            reaction,
            canonical_digest({"source": source, "reaction": reaction.value}),
            command_id=IdempotencyKey(f"command:{key}-{reaction.value}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=10,
        )


def _start_round_close(service, round_id, key):
    _append(
        service,
        CommandKind.BEGIN_ROUND_CLOSING,
        EventKind.ROUND_CLOSING_STARTED,
        AggregateKind.ROUND,
        round_id,
        {"closing": True},
        f"{key}-closing",
    )


def _result_source(service, field, competitor, revision):
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT source_global_sequence FROM v3_result_revisions WHERE field_id=? "
            "AND competitor_id=? AND revision=?",
            (str(field), f"competitor:{competitor}", revision),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_partial_results_cannot_settle_or_learn_and_barrier_is_monotonic(tmp_path: Path):
    service, _round, field = _bootstrap(tmp_path)
    first = service.record_live_result(
        _submission(field, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:result-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    source_a = first.first_global_sequence
    barrier = service.projections.barrier_sequence()
    assert barrier == source_a - 1
    with pytest.raises(Exception, match="one active outcome"):
        service.settle_live_race(
            field,
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey("command:partial-settle"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=5,
        )
    with pytest.raises(Exception, match="unsettled"):
        service.complete_derivation_reaction(
            source_a,
            MandatoryReaction.CAPABILITY,
            "a" * 64,
            command_id=IdempotencyKey("command:early-reaction"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=6,
        )
    second = service.record_live_result(
        _submission(field, "b", ResultStatus.DNS),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:result-b"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=7,
    )
    service.settle_live_race(
        field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:settle-heat"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=8,
    )
    assert service.projections.barrier_sequence() == barrier
    for source in (source_a, second.first_global_sequence):
        for reaction in MandatoryReaction:
            service.complete_derivation_reaction(
                source,
                reaction,
                canonical_digest({"source": source, "reaction": reaction.value}),
                command_id=IdempotencyKey(f"command:reaction-{source}-{reaction.value}"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=9,
            )
    assert service.projections.barrier_sequence() >= barrier
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT observation_json, source_global_sequence FROM v3_result_revisions "
            "ORDER BY source_global_sequence"
        ).fetchall()
    assert [json.loads(str(row[0]))["observation_sequence"] for row in rows] == [
        int(row[1]) for row in rows
    ]
    assert "observation_sequence" not in _submission(field, "a", ResultStatus.COMPLETION).to_dict()


def test_projection_wipe_and_genesis_rebuild_are_digest_identical(tmp_path: Path):
    service, _round, field = _bootstrap(tmp_path)
    service.record_live_result(
        _submission(field, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:result-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        incremental = service.projections.projection_digest(connection)
    with open_v3_connection(service.projections.database_path) as connection:
        connection.execute("DELETE FROM v3_ingress_snapshots WHERE entity_kind='field'")
    assert service.projections.rebuild_reaction_projection() == incremental


def test_concurrent_exact_retry_commits_once_and_returns_same_bytes(tmp_path: Path):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    snapshot = UpstreamSnapshot(
        SnapshotKind.TOURNAMENT,
        StableIdentifier("tournament:show"),
        7,
        StableIdentifier("tournament:show"),
        None,
        {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
    )

    def submit():
        return service.ingest_snapshot(
            snapshot,
            command_id=IdempotencyKey("command:same-snapshot"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        ).result_bytes

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: submit(), range(2)))
    assert results[0] == results[1]
    assert SQLiteEventStore(service.projections.database_path).event_count() == 1


def test_snapshot_schema_is_deeply_immutable_and_rejects_pii():
    source = {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"}
    snapshot = UpstreamSnapshot(
        SnapshotKind.TOURNAMENT,
        StableIdentifier("tournament:show"),
        1,
        StableIdentifier("tournament:show"),
        None,
        source,
    )
    source["bundle_id"] = "bundle:changed"
    assert snapshot.payload()["snapshot"]["bundle_id"] == "bundle:verified"
    for forbidden in ("name", "contact", "free_text"):
        with pytest.raises(Exception, match="unknown"):
            UpstreamSnapshot(
                SnapshotKind.TOURNAMENT,
                StableIdentifier("tournament:show"),
                1,
                StableIdentifier("tournament:show"),
                None,
                {
                    "bundle_id": "bundle:verified",
                    "historical_cutoff_key": "history:prior",
                    forbidden: "pii",
                },
            )


def test_round_close_without_ingress_fails_deterministically(tmp_path: Path):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    with pytest.raises(Exception, match="authoritative round snapshot"):
        service.close_evidence_round(
            StableIdentifier("round:missing"),
            command_id=IdempotencyKey("command:close-missing"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )


def test_tournament_open_and_close_are_revalidated_inside_writer_transaction(
    tmp_path: Path,
):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:show")
    _append(
        service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "configure-without-ingress",
    )
    with pytest.raises(ProjectionConflict, match="authoritative snapshot"):
        _append(
            service,
            CommandKind.OPEN_TOURNAMENT,
            EventKind.TOURNAMENT_OPENED,
            AggregateKind.TOURNAMENT,
            tournament,
            {
                "schema_version": "strathmark-v3-tournament-open-v1",
                "bundle_id": "bundle:verified",
                "historical_cutoff_key": "history:prior",
                "root_round_ids": ["round:heat"],
            },
            "open-without-ingress",
        )

    service, heat, _field = _bootstrap(tmp_path / "second")
    with pytest.raises(ProjectionConflict, match="configured round closure"):
        _append(
            service,
            CommandKind.CLOSE_TOURNAMENT,
            EventKind.TOURNAMENT_CLOSED,
            AggregateKind.TOURNAMENT,
            StableIdentifier("tournament:show"),
            {
                "schema_version": "strathmark-v3-tournament-close-v1",
                "deferred_reactions": ["cancel_jobs", "expire_overlay", "seal_exports"],
            },
            "close-with-open-round",
        )
    assert SQLiteEventStore(service.projections.database_path).aggregate_head(str(heat)) is not None


def test_post_closure_correction_reaches_later_unissued_epoch_without_rewriting_seals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, heat, heat_field = _bootstrap(tmp_path)
    sources = []
    for competitor, status in (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)):
        result = service.record_live_result(
            _submission(heat_field, competitor, status),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:heat-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=4,
        )
        sources.append(result.first_global_sequence)
    service.settle_live_race(
        heat_field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:settle-heat"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    for index, source in enumerate(sources):
        _complete_source(service, source, f"heat-source-{index}")
    _start_round_close(service, heat, "heat")
    heat_closure, _ = service.close_evidence_round(
        heat,
        command_id=IdempotencyKey("command:close-heat"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=11,
    )

    quarter = StableIdentifier("round:quarter")
    quarter_field = StableIdentifier("field:quarter-a")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            quarter,
            1,
            StableIdentifier("tournament:show"),
            quarter,
            {
                "round_ordinal": 2,
                "predecessor_round_ids": [str(heat)],
                "successor_round_ids": ["round:semi"],
            },
        ),
        "quarter-snapshot",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        quarter,
        {"configured": True},
        "configure-quarter",
    )
    epoch1, _ = service.freeze_round_epoch(
        quarter,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(heat_closure,),
        command_id=IdempotencyKey("command:freeze-quarter-1"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=12,
    )
    context = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            quarter_field,
            1,
            StableIdentifier("tournament:show"),
            quarter,
            {
                "competitor_ids": ["competitor:a", "competitor:b"],
                "target_context": context.to_dict(),
                "stand_ids": ["stand:one", "stand:two"],
            },
        ),
        "quarter-field-snapshot",
    )
    original_multi = service._execute_multi

    def race_correction(*args, **kwargs):
        _append(
            service,
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            AggregateKind.FIELD,
            quarter_field,
            {"round_id": str(quarter), "epoch_id": str(epoch1.epoch_id), "field_revision": 1},
            "prepare-quarter",
        )
        return original_multi(*args, **kwargs)

    monkeypatch.setattr(service, "_execute_multi", race_correction)
    with pytest.raises(ProjectionConflict, match="complete dependent field set"):
        service.record_live_result(
            _submission(heat_field, "a", ResultStatus.COMPLETION, revision=2),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey("command:correct-a-r2-raced"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=13,
        )
    monkeypatch.setattr(service, "_execute_multi", original_multi)
    service.record_live_result(
        _submission(heat_field, "a", ResultStatus.COMPLETION, revision=2),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:correct-a-r2"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=13,
    )
    _complete_source(service, _result_source(service, heat_field, "a", 2), "correct-a-r2")
    epoch2, _ = service.freeze_round_epoch(
        quarter,
        epoch_revision=2,
        historical_cutoff_key="history:prior",
        closure_ids=(heat_closure,),
        command_id=IdempotencyKey("command:freeze-quarter-2"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=14,
    )
    assert {member.revision for member in epoch1.members} == {1}
    assert max(member.revision for member in epoch2.members) == 2
    _append(
        service,
        CommandKind.REGENERATE_FIELD,
        EventKind.FIELD_REGENERATED,
        AggregateKind.FIELD,
        quarter_field,
        {"round_id": str(quarter), "epoch_id": str(epoch2.epoch_id), "field_revision": 2},
        "regenerate-quarter",
    )
    _append(
        service,
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        AggregateKind.FIELD,
        quarter_field,
        {
            "round_id": str(quarter),
            "epoch_id": str(epoch2.epoch_id),
            "field_revision": 2,
            "receipt_id": "receipt:quarter-a",
            "competitor_ids": ["competitor:a", "competitor:b"],
            "issued_marks": {"competitor:a": 3, "competitor:b": 3},
        },
        "issue-quarter",
    )
    service.record_live_result(
        _submission(heat_field, "a", ResultStatus.COMPLETION, revision=3),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:correct-a-r3"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=15,
    )
    _complete_source(service, _result_source(service, heat_field, "a", 3), "correct-a-r3")
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        seal_before = tuple(
            connection.execute(
                "SELECT epoch_id, first_issue_global_sequence FROM v3_round_issue_seals "
                "WHERE round_id=?",
                (str(quarter),),
            ).fetchone()
        )
        dependency = connection.execute(
            "SELECT invalidated_by_sequence FROM v3_prepared_field_dependencies "
            "WHERE field_id=? AND field_revision=2",
            (str(quarter_field),),
        ).fetchone()
    assert dependency is not None and dependency[0] is None

    quarter_sources = []
    for competitor, status in (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)):
        quarter_result = service.record_live_result(
            replace(
                _submission(quarter_field, competitor, status),
                round_id=quarter,
            ),
            field_revision=2,
            claimed_receipt_id=StableIdentifier("receipt:quarter-a"),
            command_id=IdempotencyKey(f"command:quarter-result-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=16,
        )
        quarter_sources.append(quarter_result.first_global_sequence)
    service.settle_live_race(
        quarter_field,
        field_revision=2,
        claimed_receipt_id=StableIdentifier("receipt:quarter-a"),
        command_id=IdempotencyKey("command:settle-quarter"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=16,
    )
    for index, source in enumerate(quarter_sources):
        _complete_source(service, source, f"quarter-source-{index}")
    _start_round_close(service, quarter, "quarter")
    quarter_closure, _ = service.close_evidence_round(
        quarter,
        command_id=IdempotencyKey("command:close-quarter"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=16,
    )
    semi = StableIdentifier("round:semi")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            semi,
            1,
            StableIdentifier("tournament:show"),
            semi,
            {
                "round_ordinal": 3,
                "predecessor_round_ids": [str(quarter)],
                "successor_round_ids": [],
            },
        ),
        "semi-snapshot",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        semi,
        {"configured": True},
        "configure-semi",
    )
    semi_epoch, _ = service.freeze_round_epoch(
        semi,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(quarter_closure,),
        command_id=IdempotencyKey("command:freeze-semi"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=17,
    )
    assert max(member.revision for member in semi_epoch.members) == 3
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT epoch_id, first_issue_global_sequence FROM v3_round_issue_seals "
                    "WHERE round_id=?",
                    (str(quarter),),
                ).fetchone()
            )
            == seal_before
        )
        incremental = service.projections.projection_digest(connection)
    with open_v3_connection(service.projections.database_path) as connection:
        connection.execute("DELETE FROM v3_result_revisions WHERE revision=1")
    assert service.projections.rebuild_reaction_projection() == incremental


def test_field_preparation_racing_roster_revision_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:show")
    heat = StableIdentifier("round:heat")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "race-tournament-snapshot",
    )
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            heat,
            1,
            tournament,
            heat,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        "race-round-snapshot",
    )
    _append(
        service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "race-configure-tournament",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        heat,
        {"configured": True},
        "race-configure-round",
    )
    service.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:prior",
        root_round_ids=(heat,),
        command_id=IdempotencyKey("command:race-open"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    service.freeze_round_epoch(
        heat,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:race-freeze-epoch"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    racing_field = StableIdentifier("field:racing")
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        epoch_id = str(
            connection.execute(
                "SELECT epoch_id FROM v3_evidence_epochs WHERE round_id=?",
                (str(heat),),
            ).fetchone()[0]
        )
    field_content = {
        "competitor_ids": ["competitor:a", "competitor:b"],
        "target_context": TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1").to_dict(),
        "stand_ids": ["stand:one", "stand:two"],
    }
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            racing_field,
            1,
            StableIdentifier("tournament:show"),
            heat,
            field_content,
        ),
        "racing-field-initial",
    )
    original_execute = service._execute

    def race(*args, **kwargs):
        _append(
            service,
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            AggregateKind.FIELD,
            racing_field,
            {"round_id": str(heat), "epoch_id": epoch_id, "field_revision": 1},
            "racing-prepare",
        )
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(service, "_execute", race)
    with pytest.raises(ProjectionConflict, match="complete dependent field set"):
        service.ingest_snapshot(
            UpstreamSnapshot(
                SnapshotKind.FIELD,
                racing_field,
                2,
                StableIdentifier("tournament:show"),
                heat,
                {
                    "competitor_ids": ["competitor:a", "competitor:b"],
                    "target_context": TargetContext(
                        "underhand", 300, "wood", "tax:v1", "convert:v1"
                    ).to_dict(),
                    "stand_ids": ["stand:one", "stand:two"],
                },
            ),
            command_id=IdempotencyKey("command:racing-field-revision"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=18,
        )


def test_branch_merge_epoch_requires_every_predecessor_and_keeps_empty_boundary(
    tmp_path: Path,
):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:show")
    heat = StableIdentifier("round:merge-heat")
    divisions = (StableIdentifier("round:division-a"), StableIdentifier("round:division-b"))
    parallel = StableIdentifier("round:parallel")
    grand = StableIdentifier("round:grand")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "merge-tournament",
    )
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            heat,
            1,
            tournament,
            heat,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [str(item) for item in divisions],
            },
        ),
        "merge-heat",
    )
    for index, division in enumerate(divisions, start=1):
        _snapshot(
            service,
            UpstreamSnapshot(
                SnapshotKind.ROUND,
                division,
                1,
                tournament,
                division,
                {
                    "round_ordinal": index + 1,
                    "predecessor_round_ids": [str(heat)],
                    "successor_round_ids": [str(grand)],
                },
            ),
            f"merge-division-{index}",
        )
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            parallel,
            1,
            tournament,
            parallel,
            {
                "round_ordinal": 3,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        "merge-parallel",
    )
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            grand,
            1,
            tournament,
            grand,
            {
                "round_ordinal": 4,
                "predecessor_round_ids": [str(item) for item in divisions],
                "successor_round_ids": [],
            },
        ),
        "merge-grand",
    )
    _append(
        service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "merge-configure-tournament",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        heat,
        {"configured": True},
        "merge-configure-heat",
    )
    for index, division in enumerate(divisions, start=1):
        _append(
            service,
            CommandKind.CONFIGURE_ROUND,
            EventKind.ROUND_CONFIGURED,
            AggregateKind.ROUND,
            division,
            {"configured": True},
            f"merge-configure-division-{index}",
        )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        parallel,
        {"configured": True},
        "merge-configure-parallel",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        grand,
        {"configured": True},
        "merge-configure-grand",
    )
    service.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:prior",
        root_round_ids=(heat, parallel),
        command_id=IdempotencyKey("command:merge-open"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    heat_epoch, _ = service.freeze_round_epoch(
        heat,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:merge-freeze-heat"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    heat_field = StableIdentifier("field:merge-heat")
    merge_context = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            heat_field,
            1,
            tournament,
            heat,
            {
                "competitor_ids": ["competitor:merge"],
                "target_context": merge_context.to_dict(),
                "stand_ids": ["stand:merge"],
            },
        ),
        "merge-heat-field",
    )
    _append(
        service,
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        AggregateKind.FIELD,
        heat_field,
        {"round_id": str(heat), "epoch_id": str(heat_epoch.epoch_id), "field_revision": 1},
        "merge-prepare-heat",
    )
    _append(
        service,
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        AggregateKind.FIELD,
        heat_field,
        {
            "round_id": str(heat),
            "epoch_id": str(heat_epoch.epoch_id),
            "field_revision": 1,
            "receipt_id": "receipt:merge-heat",
            "competitor_ids": ["competitor:merge"],
            "issued_marks": {"competitor:merge": 3},
        },
        "merge-issue-heat",
    )
    merge_result = service.record_live_result(
        LiveResultSubmission(
            StableIdentifier("evidence:merge-r1"),
            StableIdentifier("competitor:merge"),
            tournament,
            heat,
            heat_field,
            merge_context,
            NOW,
            3,
            15_000,
            1,
            0,
            OfficialResult(ResultStatus.COMPLETION, 12_000, None, 1, None),
            canonical_digest({"merge": True}),
        ),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:merge-heat"),
        command_id=IdempotencyKey("command:merge-heat-result"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    service.settle_live_race(
        heat_field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:merge-heat"),
        command_id=IdempotencyKey("command:merge-heat-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    _complete_source(service, merge_result.first_global_sequence, "merge-heat")
    _start_round_close(service, heat, "merge-heat")
    heat_closure, _ = service.close_evidence_round(
        heat,
        command_id=IdempotencyKey("command:merge-close-heat"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    closures = []
    for index, division in enumerate(divisions, start=1):
        service.freeze_round_epoch(
            division,
            epoch_revision=1,
            historical_cutoff_key="history:prior",
            closure_ids=(heat_closure,),
            command_id=IdempotencyKey(f"command:merge-freeze-{index}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=2,
        )
        _start_round_close(service, division, f"merge-division-{index}")
        closure, _ = service.close_evidence_round(
            division,
            command_id=IdempotencyKey(f"command:merge-close-{index}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=3,
        )
        closures.append(closure)
    parallel_epoch, _ = service.freeze_round_epoch(
        parallel,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:parallel-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    parallel_field = StableIdentifier("field:parallel")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            parallel_field,
            1,
            tournament,
            parallel,
            {
                "competitor_ids": ["competitor:parallel"],
                "target_context": TargetContext(
                    "underhand", 300, "wood", "tax:v1", "convert:v1"
                ).to_dict(),
                "stand_ids": ["stand:parallel"],
            },
        ),
        "parallel-field-snapshot",
    )
    _append(
        service,
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        AggregateKind.FIELD,
        parallel_field,
        {"round_id": str(parallel), "epoch_id": str(parallel_epoch.epoch_id), "field_revision": 1},
        "parallel-prepare",
    )
    _append(
        service,
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        AggregateKind.FIELD,
        parallel_field,
        {
            "round_id": str(parallel),
            "epoch_id": str(parallel_epoch.epoch_id),
            "field_revision": 1,
            "receipt_id": "receipt:parallel",
            "competitor_ids": ["competitor:parallel"],
            "issued_marks": {"competitor:parallel": 3},
        },
        "parallel-issue",
    )
    service.record_live_result(
        LiveResultSubmission(
            StableIdentifier("evidence:parallel-r1"),
            StableIdentifier("competitor:parallel"),
            tournament,
            parallel,
            parallel_field,
            TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1"),
            NOW,
            3,
            15_000,
            1,
            0,
            OfficialResult(ResultStatus.COMPLETION, 12_000, None, 1, None),
            canonical_digest({"parallel": True}),
        ),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:parallel"),
        command_id=IdempotencyKey("command:parallel-result"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    with pytest.raises(Exception, match="every predecessor closure"):
        service.freeze_round_epoch(
            grand,
            epoch_revision=1,
            historical_cutoff_key="history:prior",
            closure_ids=(closures[0],),
            command_id=IdempotencyKey("command:merge-missing"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=4,
        )
    epoch, _ = service.freeze_round_epoch(
        grand,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=tuple(closures),
        command_id=IdempotencyKey("command:merge-complete"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    assert len(epoch.members) == 1
    assert epoch.maximum_tournament_sequence > 0
    assert (
        _result_source(service, parallel_field, "parallel", 1) > epoch.maximum_tournament_sequence
    )


def test_result_committing_between_round_close_preflight_and_writer_forces_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, heat, field = _bootstrap(tmp_path)
    for competitor, status in (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)):
        service.record_live_result(
            _submission(field, competitor, status),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:close-race-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
    service.settle_live_race(
        field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:close-race-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    _start_round_close(service, heat, "close-race")
    original_execute = service._execute

    def race(*args, **kwargs):
        service.record_live_result(
            _submission(field, "a", ResultStatus.COMPLETION, revision=2),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey("command:close-race-correction"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=3,
        )
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(service, "_execute", race)
    with pytest.raises(ProjectionConflict, match="exact active settled result set"):
        service.close_evidence_round(
            heat,
            command_id=IdempotencyKey("command:close-race-round"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=4,
        )
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM v3_round_closures").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT MAX(revision) FROM v3_result_revisions WHERE competitor_id='competitor:a'"
            ).fetchone()[0]
            == 2
        )


def test_tournament_open_close_exact_retries_are_idempotent(tmp_path: Path):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:show")
    only_round = StableIdentifier("round:only")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "idempotent-tournament",
    )
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            only_round,
            1,
            tournament,
            only_round,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        "idempotent-round",
    )
    _append(
        service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "idempotent-configure-tournament",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        only_round,
        {"configured": True},
        "idempotent-configure-round",
    )
    arguments = {
        "bundle_id": StableIdentifier("bundle:verified"),
        "historical_cutoff_key": "history:prior",
        "root_round_ids": (only_round,),
        "command_id": IdempotencyKey("command:idempotent-open"),
        "actor_id": ACTOR,
        "occurred_at_utc": NOW,
        "monotonic_elapsed_ms": 1,
    }
    opened = service.open_tournament(tournament, **arguments)
    assert service.open_tournament(tournament, **arguments).result_bytes == opened.result_bytes
    service.freeze_round_epoch(
        only_round,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:idempotent-epoch"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    _start_round_close(service, only_round, "idempotent-round")
    service.close_evidence_round(
        only_round,
        command_id=IdempotencyKey("command:idempotent-round-close"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    close_arguments = {
        "command_id": IdempotencyKey("command:idempotent-close"),
        "actor_id": ACTOR,
        "occurred_at_utc": NOW,
        "monotonic_elapsed_ms": 4,
    }
    closed = service.close_tournament(tournament, **close_arguments)
    assert (
        service.close_tournament(tournament, **close_arguments).result_bytes == closed.result_bytes
    )
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE aggregate_id=? AND event_kind=?",
                (str(tournament), EventKind.TOURNAMENT_OPENED.value),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE aggregate_id=? AND event_kind=?",
                (str(tournament), EventKind.TOURNAMENT_CLOSED.value),
            ).fetchone()[0]
            == 1
        )


def test_each_partial_reaction_subset_resumes_after_restart(tmp_path: Path):
    database = tmp_path / "authority.sqlite3"
    service, _heat, field = _bootstrap(tmp_path)
    sources = []
    for competitor, status in (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)):
        result = service.record_live_result(
            _submission(field, competitor, status),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:restart-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
        sources.append(result.first_global_sequence)
    service.settle_live_race(
        field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:restart-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    source = sources[0]
    reactions = tuple(MandatoryReaction)
    for index, reaction in enumerate(reactions, start=1):
        service.complete_derivation_reaction(
            source,
            reaction,
            canonical_digest({"restart": index}),
            command_id=IdempotencyKey(f"command:restart-reaction-{index}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=3,
        )
        service = LifecycleService(database)
        assert service.projections.pending_reactions(source) == reactions[index:]
    with open_v3_connection(database, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM v3_derivation_sequence_completions "
                "WHERE source_global_sequence=?",
                (source,),
            ).fetchone()[0]
            == 1
        )


def test_upstream_revision_jumps_retry_exactly_and_reject_stale_or_duplicate_delivery(
    tmp_path: Path,
):
    service = LifecycleService(tmp_path / "authority.sqlite3")
    tournament = StableIdentifier("tournament:show")

    def ingest(revision, key, bundle="bundle:verified"):
        return service.ingest_snapshot(
            UpstreamSnapshot(
                SnapshotKind.TOURNAMENT,
                tournament,
                revision,
                tournament,
                None,
                {"bundle_id": bundle, "historical_cutoff_key": "history:prior"},
            ),
            command_id=IdempotencyKey(f"command:{key}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )

    first = ingest(7, "revision-seven")
    jumped = ingest(10, "revision-ten")
    assert first.last_global_sequence < jumped.first_global_sequence
    assert ingest(10, "revision-ten").result_bytes == jumped.result_bytes
    with pytest.raises(ProjectionConflict, match="strictly monotonic"):
        ingest(9, "revision-nine-stale")
    with pytest.raises(ProjectionConflict, match="strictly monotonic"):
        ingest(10, "revision-ten-duplicate")
    with pytest.raises(ProjectionConflict, match="strictly monotonic"):
        ingest(8, "revision-eight-conflict", bundle="bundle:changed")


def test_issued_field_is_immutable_new_legal_field_is_allowed_and_round_epoch_cannot_mix(
    tmp_path: Path,
):
    service, heat, issued_field = _bootstrap(tmp_path)
    context = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1")
    with pytest.raises(ProjectionConflict, match="issued legal field is immutable"):
        _snapshot(
            service,
            UpstreamSnapshot(
                SnapshotKind.FIELD,
                issued_field,
                2,
                StableIdentifier("tournament:show"),
                heat,
                {
                    "competitor_ids": ["competitor:a"],
                    "target_context": context.to_dict(),
                    "stand_ids": ["stand:one"],
                },
            ),
            "post-issue-revision",
        )
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        epoch_id = str(
            connection.execute(
                "SELECT epoch_id FROM v3_round_issue_seals WHERE round_id=?", (str(heat),)
            ).fetchone()[0]
        )
    for suffix in ("new", "mixed"):
        field = StableIdentifier(f"field:{suffix}")
        _snapshot(
            service,
            UpstreamSnapshot(
                SnapshotKind.FIELD,
                field,
                1,
                StableIdentifier("tournament:show"),
                heat,
                {
                    "competitor_ids": ["competitor:a"],
                    "target_context": context.to_dict(),
                    "stand_ids": ["stand:one"],
                },
            ),
            f"legal-{suffix}-snapshot",
        )
        _append(
            service,
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            AggregateKind.FIELD,
            field,
            {"round_id": str(heat), "epoch_id": epoch_id, "field_revision": 1},
            f"legal-{suffix}-prepare",
        )
    _append(
        service,
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        AggregateKind.FIELD,
        StableIdentifier("field:new"),
        {
            "round_id": str(heat),
            "epoch_id": epoch_id,
            "field_revision": 1,
            "receipt_id": "receipt:new",
            "competitor_ids": ["competitor:a"],
            "issued_marks": {"competitor:a": 3},
        },
        "legal-new-issue",
    )
    with pytest.raises(ProjectionConflict, match="mixed epochs"):
        _append(
            service,
            CommandKind.ACKNOWLEDGE_ISSUE,
            EventKind.FIELD_ISSUED,
            AggregateKind.FIELD,
            StableIdentifier("field:mixed"),
            {
                "round_id": str(heat),
                "epoch_id": "epoch:other",
                "field_revision": 1,
                "receipt_id": "receipt:mixed",
                "competitor_ids": ["competitor:a"],
                "issued_marks": {"competitor:a": 3},
            },
            "legal-mixed-issue",
        )


@pytest.mark.parametrize("change", ["entrant", "scratch", "substitution", "draw", "context"])
def test_each_preissue_field_change_class_supersedes_the_whole_prepared_field(
    tmp_path: Path, change: str
):
    service, tournament, heat, field, original = _bootstrap_prepared_unissued_field(
        tmp_path / change
    )
    revised = json.loads(json.dumps(original))
    if change == "entrant":
        revised["competitor_ids"].append("competitor:c")
        revised["stand_ids"].append("stand:three")
    elif change == "scratch":
        revised["competitor_ids"].pop()
        revised["stand_ids"].pop()
    elif change == "substitution":
        revised["competitor_ids"][1] = "competitor:c"
    elif change == "draw":
        revised["stand_ids"].reverse()
    else:
        revised["target_context"]["size_mm"] = 325
    _snapshot(
        service,
        UpstreamSnapshot(SnapshotKind.FIELD, field, 2, tournament, heat, revised),
        f"{change}-revision",
    )
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        dependency = connection.execute(
            "SELECT invalidated_by_sequence FROM v3_prepared_field_dependencies "
            "WHERE field_id=? AND field_revision=1",
            (str(field),),
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM v3_events WHERE aggregate_id=? AND event_kind=?",
            (str(field), EventKind.FIELD_SUPERSEDED.value),
        ).fetchone()[0]
    assert dependency is not None and dependency[0] is not None
    assert event_count == 1


def test_concurrent_distinct_results_receive_unique_authoritative_sequences(tmp_path: Path):
    service, _heat, field = _bootstrap(tmp_path)

    def submit(item):
        competitor, status = item
        service.record_live_result(
            _submission(field, competitor, status),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:concurrent-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(
            pool.map(
                submit,
                (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)),
            )
        )
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT source_global_sequence, observation_json FROM v3_result_revisions "
            "ORDER BY source_global_sequence"
        ).fetchall()
    sequences = [int(row[0]) for row in rows]
    assert len(sequences) == len(set(sequences)) == 2
    assert [json.loads(str(row[1]))["observation_sequence"] for row in rows] == sequences


def test_tournament_close_blocks_completed_round_with_pending_derivations(tmp_path: Path):
    service, heat, field = _bootstrap(tmp_path)
    for competitor, status in (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)):
        service.record_live_result(
            _submission(field, competitor, status),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:pending-close-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
    service.settle_live_race(
        field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:pending-close-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    _start_round_close(service, heat, "pending-close")
    service.close_evidence_round(
        heat,
        command_id=IdempotencyKey("command:pending-close-round"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    with pytest.raises(ProjectionConflict, match="mandatory derivations"):
        service.close_tournament(
            StableIdentifier("tournament:show"),
            command_id=IdempotencyKey("command:pending-close-tournament"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=4,
        )


def test_epoch_freeze_atomically_freezes_round_and_preparation_requires_exact_epoch(
    tmp_path: Path,
):
    service, _tournament, heat, _field, _content = _bootstrap_prepared_unissued_field(tmp_path)
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT event_kind, command_id FROM v3_events WHERE aggregate_id IN (?, ?) "
            "AND event_kind IN (?, ?) ORDER BY global_sequence",
            (
                str(heat),
                str(
                    connection.execute(
                        "SELECT epoch_id FROM v3_evidence_epochs WHERE round_id=?",
                        (str(heat),),
                    ).fetchone()[0]
                ),
                EventKind.ROUND_FROZEN.value,
                EventKind.ROUND_EPOCH_FROZEN.value,
            ),
        ).fetchall()
    assert [str(row[0]) for row in rows] == [
        EventKind.ROUND_EPOCH_FROZEN.value,
        EventKind.ROUND_FROZEN.value,
    ]
    assert len({str(row[1]) for row in rows}) == 1

    wrong_epoch = StableIdentifier("epoch:not-the-round-epoch")
    wrong_field = StableIdentifier("field:wrong-epoch")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            wrong_field,
            1,
            StableIdentifier("tournament:show"),
            heat,
            {
                "competitor_ids": ["competitor:a"],
                "target_context": TargetContext(
                    "underhand", 300, "wood", "tax:v1", "convert:v1"
                ).to_dict(),
                "stand_ids": ["stand:one"],
            },
        ),
        "wrong-epoch-field",
    )
    with pytest.raises(ProjectionConflict, match="frozen epoch"):
        _append(
            service,
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            AggregateKind.FIELD,
            wrong_field,
            {"round_id": str(heat), "epoch_id": str(wrong_epoch), "field_revision": 1},
            "wrong-epoch-prepare",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"round_id": "round:other"}, "roster revision"),
        ({"epoch_id": "epoch:other"}, "prepared dependency"),
        ({"field_revision": 2}, "prepared dependency"),
        ({"competitor_ids": ["competitor:a"]}, "roster"),
        (
            {"issued_marks": {"competitor:a": 3}},
            "issued marks",
        ),
    ),
)
def test_issue_requires_exact_prepared_and_roster_bindings(
    tmp_path: Path, mutation: dict[str, object], message: str
):
    service, _tournament, heat, field, _content = _bootstrap_prepared_unissued_field(tmp_path)
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        epoch_id = str(
            connection.execute(
                "SELECT epoch_id FROM v3_evidence_epochs WHERE round_id=?",
                (str(heat),),
            ).fetchone()[0]
        )
    payload = {
        "round_id": str(heat),
        "epoch_id": epoch_id,
        "field_revision": 1,
        "receipt_id": "receipt:binding-check",
        "competitor_ids": ["competitor:a", "competitor:b"],
        "issued_marks": {"competitor:a": 3, "competitor:b": 3},
    }
    payload.update(mutation)
    with pytest.raises(ProjectionConflict, match=message):
        _append(
            service,
            CommandKind.ACKNOWLEDGE_ISSUE,
            EventKind.FIELD_ISSUED,
            AggregateKind.FIELD,
            field,
            payload,
            f"wrong-issue-{canonical_digest(mutation)}",
        )


@pytest.mark.parametrize(
    "submission",
    (
        replace(
            _submission(StableIdentifier("field:heat-a"), "a", ResultStatus.COMPLETION),
            tournament_id=StableIdentifier("tournament:other"),
        ),
        replace(
            _submission(StableIdentifier("field:heat-a"), "a", ResultStatus.COMPLETION),
            round_id=StableIdentifier("round:other"),
        ),
        replace(
            _submission(StableIdentifier("field:heat-a"), "a", ResultStatus.COMPLETION),
            context=TargetContext("standing", 300, "wood", "tax:v1", "convert:v1"),
        ),
        replace(
            _submission(StableIdentifier("field:heat-a"), "a", ResultStatus.COMPLETION),
            issued_mark=4,
        ),
    ),
)
def test_result_rejects_spoofed_issue_lineage_context_and_mark(
    tmp_path: Path, submission: LiveResultSubmission
):
    service, _heat, field = _bootstrap(tmp_path)
    submission = replace(submission, field_id=field)
    with pytest.raises((ContractError, ProjectionConflict), match="authoritative issue"):
        service.record_live_result(
            submission,
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:spoof-{submission.source_digest}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )


def test_ingress_is_locked_by_open_freeze_and_close_lifecycle_boundaries(tmp_path: Path):
    service, tournament, heat, field, content = _bootstrap_prepared_unissued_field(tmp_path)
    with pytest.raises(ProjectionConflict, match="pinned at open"):
        _snapshot(
            service,
            UpstreamSnapshot(
                SnapshotKind.TOURNAMENT,
                tournament,
                2,
                tournament,
                None,
                {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
            ),
            "late-tournament-ingress",
        )
    with pytest.raises(ProjectionConflict, match="first frozen epoch"):
        _snapshot(
            service,
            UpstreamSnapshot(
                SnapshotKind.ROUND,
                heat,
                2,
                tournament,
                heat,
                {
                    "round_ordinal": 2,
                    "predecessor_round_ids": [],
                    "successor_round_ids": [],
                },
            ),
            "late-round-ingress",
        )
    _append(
        service,
        CommandKind.SUPERSEDE_FIELD,
        EventKind.FIELD_SUPERSEDED,
        AggregateKind.FIELD,
        field,
        {"reason": "cancelled-before-issue"},
        "cancel-prepared-field",
    )
    _start_round_close(service, heat, "ingress-lock")
    service.close_evidence_round(
        heat,
        command_id=IdempotencyKey("command:ingress-lock-close-round"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    service.close_tournament(
        tournament,
        command_id=IdempotencyKey("command:ingress-lock-close-tournament"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    with pytest.raises(ProjectionConflict, match="after tournament close"):
        _snapshot(
            service,
            UpstreamSnapshot(
                SnapshotKind.FIELD,
                StableIdentifier("field:post-close"),
                1,
                tournament,
                heat,
                content,
            ),
            "post-close-ingress",
        )


def test_round_closing_rejects_unsettled_field_but_allows_an_empty_frozen_round(
    tmp_path: Path,
):
    service, heat, _field = _bootstrap(tmp_path)
    with pytest.raises(ProjectionConflict, match="every prepared field"):
        _start_round_close(service, heat, "unsettled-round")

    empty_service = LifecycleService(tmp_path / "empty.sqlite3")
    tournament = StableIdentifier("tournament:empty")
    empty_round = StableIdentifier("round:empty")
    _snapshot(
        empty_service,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "empty-tournament-ingress",
    )
    _snapshot(
        empty_service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            empty_round,
            1,
            tournament,
            empty_round,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        "empty-round-ingress",
    )
    _append(
        empty_service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        tournament,
        {"configured": True},
        "empty-configure-tournament",
    )
    _append(
        empty_service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        empty_round,
        {"configured": True},
        "empty-configure-round",
    )
    empty_service.open_tournament(
        tournament,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:prior",
        root_round_ids=(empty_round,),
        command_id=IdempotencyKey("command:empty-open"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    empty_service.freeze_round_epoch(
        empty_round,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:empty-epoch"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    late_field = StableIdentifier("field:late-empty")
    _snapshot(
        empty_service,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            late_field,
            1,
            tournament,
            empty_round,
            {
                "competitor_ids": ["competitor:late"],
                "target_context": TargetContext(
                    "underhand", 300, "wood", "tax:v1", "convert:v1"
                ).to_dict(),
                "stand_ids": ["stand:late"],
            },
        ),
        "late-empty-ingress",
    )
    _start_round_close(empty_service, empty_round, "empty")
    with pytest.raises(ProjectionConflict, match="not currently frozen"):
        _append(
            empty_service,
            CommandKind.OPTIMIZE_FIELD,
            EventKind.FIELD_OPTIMIZED,
            AggregateKind.FIELD,
            late_field,
            {
                "round_id": str(empty_round),
                "epoch_id": "epoch:late",
                "field_revision": 1,
            },
            "late-prepare-after-closing",
        )
    with pytest.raises(Exception, match="illegal field lifecycle transition"):
        _append(
            empty_service,
            CommandKind.ACKNOWLEDGE_ISSUE,
            EventKind.FIELD_ISSUED,
            AggregateKind.FIELD,
            late_field,
            {
                "round_id": str(empty_round),
                "epoch_id": "epoch:late",
                "field_revision": 1,
                "receipt_id": "receipt:late",
                "competitor_ids": ["competitor:late"],
                "issued_marks": {"competitor:late": 3},
            },
            "late-issue-after-closing",
        )
    closure_id, _ = empty_service.close_evidence_round(
        empty_round,
        command_id=IdempotencyKey("command:empty-close"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    assert closure_id.namespace == "round_closure"


@pytest.mark.parametrize("event_kind", (EventKind.FIELD_OPTIMIZED, EventKind.FIELD_ISSUED))
def test_malformed_preparation_and_issue_authority_fail_closed(
    tmp_path: Path, event_kind: EventKind
):
    service, _tournament, _heat, field, _content = _bootstrap_prepared_unissued_field(tmp_path)
    if event_kind is EventKind.FIELD_OPTIMIZED:
        target = StableIdentifier("field:malformed-prepared")
        aggregate_event = EventKind.FIELD_OPTIMIZED
        command_kind = CommandKind.OPTIMIZE_FIELD
    else:
        target = field
        aggregate_event = EventKind.FIELD_ISSUED
        command_kind = CommandKind.ACKNOWLEDGE_ISSUE
    with pytest.raises(ProjectionError, match="missing mandatory U5 bindings"):
        _append(
            service,
            command_kind,
            aggregate_event,
            AggregateKind.FIELD,
            target,
            {"malformed": True},
            f"malformed-{event_kind.value}",
        )


def test_derivation_and_settlement_authority_ids_and_completion_digest_are_recomputed(
    tmp_path: Path,
):
    service, _heat, field = _bootstrap(tmp_path)
    sources = []
    for competitor, status in (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)):
        stored = service.record_live_result(
            _submission(field, competitor, status),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:identity-result-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
        sources.append(stored.first_global_sequence)
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        result_rows = connection.execute(
            "SELECT result_key, revision, competitor_id FROM v3_result_revisions "
            "WHERE field_id=? ORDER BY competitor_id",
            (str(field),),
        ).fetchall()
    settlement_payload = {
        "schema_version": "strathmark-v3-live-settlement-v1",
        "field_id": str(field),
        "field_revision": 1,
        "receipt_id": "receipt:heat-a",
        "results": [
            {
                "result_key": str(row[0]),
                "revision": int(row[1]),
                "competitor_id": str(row[2]),
            }
            for row in result_rows
        ],
    }
    with pytest.raises(ProjectionConflict, match="settlement authority identity"):
        service._execute_multi(
            CommandKind.SETTLE_LIVE_RACE,
            StableIdentifier("settlement:forged"),
            (
                EventIntent(
                    AggregateKind.SETTLEMENT,
                    StableIdentifier("settlement:forged"),
                    EventKind.LIVE_RACE_SETTLED,
                ),
                EventIntent(AggregateKind.FIELD, field, EventKind.FIELD_SETTLED),
            ),
            settlement_payload,
            IdempotencyKey("command:forged-settlement-id"),
            ACTOR,
            NOW,
            2,
        )
    settled = service.settle_live_race(
        field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:identity-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )

    source = sources[0]
    reaction_rows = []
    first_reaction = next(iter(MandatoryReaction))
    first_digest = canonical_digest({"source": source, "reaction": first_reaction.value})
    with pytest.raises(ProjectionConflict, match="reaction authority identity"):
        _append(
            service,
            CommandKind.COMPLETE_DERIVATION_REACTION,
            EventKind.DERIVATION_REACTION_COMPLETED,
            AggregateKind.REACTION,
            StableIdentifier("reaction:forged"),
            {
                "schema_version": "strathmark-v3-derivation-reaction-v1",
                "source_global_sequence": source,
                "reaction": first_reaction.value,
                "output_digest": first_digest,
            },
            "forged-reaction-id",
        )
    for reaction in MandatoryReaction:
        output = canonical_digest({"source": source, "reaction": reaction.value})
        reaction_rows.append([reaction.value, output])
        target = deterministic_identifier(
            "reaction", {"source": source, "reaction": reaction.value}
        )
        _append(
            service,
            CommandKind.COMPLETE_DERIVATION_REACTION,
            EventKind.DERIVATION_REACTION_COMPLETED,
            AggregateKind.REACTION,
            target,
            {
                "schema_version": "strathmark-v3-derivation-reaction-v1",
                "source_global_sequence": source,
                "reaction": reaction.value,
                "output_digest": output,
            },
            f"direct-reaction-{reaction.value}",
        )
    derivation = deterministic_identifier("derivation", {"source": source})
    with pytest.raises(ProjectionConflict, match="sequence authority identity"):
        _append(
            service,
            CommandKind.COMPLETE_DERIVATION_SEQUENCE,
            EventKind.DERIVATION_SEQUENCE_COMPLETED,
            AggregateKind.DERIVATION,
            StableIdentifier("derivation:forged"),
            {
                "schema_version": "strathmark-v3-derivation-sequence-v1",
                "source_global_sequence": source,
                "completion_digest": canonical_digest(sorted(reaction_rows)),
            },
            "forged-derivation-id",
        )
    with pytest.raises(ProjectionConflict, match="digest does not match"):
        _append(
            service,
            CommandKind.COMPLETE_DERIVATION_SEQUENCE,
            EventKind.DERIVATION_SEQUENCE_COMPLETED,
            AggregateKind.DERIVATION,
            derivation,
            {
                "schema_version": "strathmark-v3-derivation-sequence-v1",
                "source_global_sequence": source,
                "completion_digest": "f" * 64,
            },
            "forged-derivation-digest",
        )
    assert settled.last_global_sequence > settled.first_global_sequence


def test_snapshot_and_result_authority_ids_are_recomputed_at_projection_boundary(tmp_path: Path):
    service = LifecycleService(tmp_path / "identity.sqlite3")
    tournament = StableIdentifier("tournament:identity")
    snapshot = UpstreamSnapshot(
        SnapshotKind.TOURNAMENT,
        tournament,
        1,
        tournament,
        None,
        {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
    )
    with pytest.raises(ProjectionConflict, match="aggregate identity"):
        _append(
            service,
            CommandKind.REVISE_TOURNAMENT_SNAPSHOT,
            EventKind.TOURNAMENT_SNAPSHOT_REVISED,
            AggregateKind.TOURNAMENT_INGRESS,
            StableIdentifier("tournament_ingress:forged"),
            snapshot.payload(),
            "forged-snapshot-id",
        )

    service, _heat, field = _bootstrap(tmp_path / "result-case")
    submission = _submission(field, "a", ResultStatus.COMPLETION)
    expected_key = deterministic_identifier(
        "result",
        {
            "field_id": str(field),
            "field_revision": 1,
            "competitor_id": "competitor:a",
        },
    )
    assert expected_key != StableIdentifier("result:forged")
    with pytest.raises(ProjectionConflict, match="field-revision addressed"):
        _append(
            service,
            CommandKind.RECORD_RESULT,
            EventKind.RESULT_RECORDED,
            AggregateKind.RESULT,
            StableIdentifier("result:forged"),
            {
                "schema_version": "strathmark-v3-live-result-v1",
                "result_key": "result:forged",
                "submission": submission.to_dict(),
                "field_revision": 1,
                "claimed_receipt_id": "receipt:heat-a",
                "candidate_numeric_eligible": True,
                "admission_reason": "eligible_completion",
            },
            "forged-result-id",
        )


def test_round_closure_and_epoch_evidence_are_isolated_between_tournaments(tmp_path: Path):
    service, _first_round, first_field = _bootstrap(tmp_path)
    for competitor, status in (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)):
        service.record_live_result(
            _submission(first_field, competitor, status),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:first-tournament-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
    service.settle_live_race(
        first_field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:first-tournament-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )

    other = StableIdentifier("tournament:other")
    other_round = StableIdentifier("round:other-root")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            other,
            1,
            other,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "other-tournament-ingress",
    )
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            other_round,
            1,
            other,
            other_round,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        "other-round-ingress",
    )
    _append(
        service,
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        other,
        {"configured": True},
        "other-configure-tournament",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        other_round,
        {"configured": True},
        "other-configure-round",
    )
    service.open_tournament(
        other,
        bundle_id=StableIdentifier("bundle:verified"),
        historical_cutoff_key="history:prior",
        root_round_ids=(other_round,),
        command_id=IdempotencyKey("command:other-open"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    other_epoch, _ = service.freeze_round_epoch(
        other_round,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(),
        command_id=IdempotencyKey("command:other-epoch"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    _start_round_close(service, other_round, "other")
    other_closure, _ = service.close_evidence_round(
        other_round,
        command_id=IdempotencyKey("command:other-close"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    assert other_epoch.members == ()
    with open_v3_connection(service.projections.database_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT tournament_id, result_set_json FROM v3_round_closures WHERE closure_id=?",
            (str(other_closure),),
        ).fetchone()
    assert tuple(row) == (str(other), "[]")


def test_lifecycle_input_contracts_fail_closed_across_all_snapshot_kinds(tmp_path: Path):
    context = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1").to_dict()
    tournament = StableIdentifier("tournament:contracts")
    round_id = StableIdentifier("round:contracts")

    with pytest.raises(ContractError, match="closed vocabulary"):
        UpstreamSnapshot("field", StableIdentifier("field:a"), 1, tournament, round_id, {})  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="identity or parent"):
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            round_id,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        )
    with pytest.raises(ContractError, match="require a round"):
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier("field:a"),
            1,
            tournament,
            None,
            {
                "competitor_ids": ["competitor:a"],
                "target_context": context,
                "stand_ids": ["stand:a"],
            },
        )
    with pytest.raises(ContractError, match="match its round"):
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            StableIdentifier("round:other"),
            1,
            tournament,
            round_id,
            {"round_ordinal": 1, "predecessor_round_ids": [], "successor_round_ids": []},
        )
    for revision in (True, "1", 0):
        with pytest.raises(ContractError, match="revision"):
            UpstreamSnapshot(
                SnapshotKind.TOURNAMENT,
                tournament,
                revision,  # type: ignore[arg-type]
                tournament,
                None,
                {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
            )
    with pytest.raises(ContractError, match="mapping"):
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            [],  # type: ignore[arg-type]
        )
    with pytest.raises(ContractError, match="unknown"):
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {
                "bundle_id": "bundle:verified",
                "historical_cutoff_key": "history:prior",
                "name": "PII",
            },
        )
    for ordinal in (True, "1", 0):
        with pytest.raises(ContractError, match="ordinal"):
            UpstreamSnapshot(
                SnapshotKind.ROUND,
                round_id,
                1,
                tournament,
                round_id,
                {"round_ordinal": ordinal, "predecessor_round_ids": [], "successor_round_ids": []},
            )
    with pytest.raises(ContractError, match="cannot repeat"):
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            round_id,
            1,
            tournament,
            round_id,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": ["round:a", "round:a"],
                "successor_round_ids": [],
            },
        )
    for roster in ([],):
        with pytest.raises(ContractError, match="ordered competitor roster"):
            UpstreamSnapshot(
                SnapshotKind.FIELD,
                StableIdentifier("field:a"),
                1,
                tournament,
                round_id,
                {"competitor_ids": roster, "target_context": context, "stand_ids": []},
            )
    with pytest.raises(ContractError, match="duplicates"):
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier("field:a"),
            1,
            tournament,
            round_id,
            {
                "competitor_ids": ["competitor:a", "competitor:a"],
                "target_context": context,
                "stand_ids": ["stand:a", "stand:b"],
            },
        )
    for stands in ([],):
        with pytest.raises(ContractError, match="one stand"):
            UpstreamSnapshot(
                SnapshotKind.FIELD,
                StableIdentifier("field:a"),
                1,
                tournament,
                round_id,
                {
                    "competitor_ids": ["competitor:a"],
                    "target_context": context,
                    "stand_ids": stands,
                },
            )
    with pytest.raises(ContractError, match="stands cannot repeat"):
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier("field:a"),
            1,
            tournament,
            round_id,
            {
                "competitor_ids": ["competitor:a", "competitor:b"],
                "target_context": context,
                "stand_ids": ["stand:a", "stand:a"],
            },
        )

    service = LifecycleService(tmp_path / "service.sqlite3")
    common = {
        "command_id": IdempotencyKey("command:invalid-input"),
        "actor_id": ACTOR,
        "occurred_at_utc": NOW,
        "monotonic_elapsed_ms": 1,
    }
    with pytest.raises(ContractError, match="UpstreamSnapshot"):
        service.ingest_snapshot("snapshot", **common)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="immutable"):
        service.open_tournament(
            tournament,
            bundle_id=StableIdentifier("bundle:verified"),
            historical_cutoff_key="history:prior",
            root_round_ids=[],  # type: ignore[arg-type]
            **common,
        )
    for roots in ((), (round_id, round_id)):
        with pytest.raises(ContractError, match="unique root"):
            service.open_tournament(
                tournament,
                bundle_id=StableIdentifier("bundle:verified"),
                historical_cutoff_key="history:prior",
                root_round_ids=roots,
                **common,
            )
    with pytest.raises(ContractError, match="sequence-free"):
        service.record_live_result(  # type: ignore[arg-type]
            "result", field_revision=1, claimed_receipt_id=StableIdentifier("receipt:a"), **common
        )
    with pytest.raises(ContractError, match="acknowledged receipt"):
        service.settle_live_race(
            StableIdentifier("field:unissued"),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:a"),
            **common,
        )
    with pytest.raises(ContractError, match="mandatory vocabulary"):
        service.complete_derivation_reaction(1, "reaction", "a" * 64, **common)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="SHA-256"):
        service.complete_derivation_reaction(1, MandatoryReaction.CAPABILITY, "short", **common)
    with pytest.raises(ContractError, match="immutable"):
        service.freeze_round_epoch(
            round_id,
            epoch_revision=1,
            historical_cutoff_key="history:prior",
            closure_ids=[],  # type: ignore[arg-type]
            **common,
        )


def test_lifecycle_void_and_remaining_epoch_preflight_paths(tmp_path: Path):
    missing = LifecycleService(tmp_path / "missing.sqlite3")
    common = {
        "command_id": IdempotencyKey("command:missing-epoch"),
        "actor_id": ACTOR,
        "occurred_at_utc": NOW,
        "monotonic_elapsed_ms": 1,
    }
    with pytest.raises(ContractError, match="authoritative round snapshot"):
        missing.freeze_round_epoch(
            StableIdentifier("round:missing"),
            epoch_revision=1,
            historical_cutoff_key="history:prior",
            closure_ids=(),
            **common,
        )

    unopened = LifecycleService(tmp_path / "unopened.sqlite3")
    tournament = StableIdentifier("tournament:unopened")
    root = StableIdentifier("round:unopened")
    _snapshot(
        unopened,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            tournament,
            1,
            tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "unopened-tournament",
    )
    _snapshot(
        unopened,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            root,
            1,
            tournament,
            root,
            {"round_ordinal": 1, "predecessor_round_ids": [], "successor_round_ids": []},
        ),
        "unopened-round",
    )
    _append(
        unopened,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        root,
        {"configured": True},
        "unopened-configure-round",
    )
    with pytest.raises(ContractError, match="tournament-open boundary"):
        unopened.freeze_round_epoch(
            root,
            epoch_revision=1,
            historical_cutoff_key="history:prior",
            closure_ids=(),
            command_id=IdempotencyKey("command:unopened-epoch"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )

    service, heat, field = _bootstrap(tmp_path / "issued")
    with pytest.raises(ContractError, match="issued round"):
        service.freeze_round_epoch(
            heat,
            epoch_revision=2,
            historical_cutoff_key="history:prior",
            closure_ids=(),
            command_id=IdempotencyKey("command:issued-refreeze"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
    service.record_live_result(
        _submission(field, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:void-base"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    voided = service.record_live_result(
        _submission(field, "a", ResultStatus.VOID, revision=2),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:void-revision"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    assert voided.first_global_sequence == voided.last_global_sequence

    service.record_live_result(
        _submission(field, "b", ResultStatus.DNS),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:void-b"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    service.settle_live_race(
        field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:void-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    _start_round_close(service, heat, "void")
    closure, _ = service.close_evidence_round(
        heat,
        command_id=IdempotencyKey("command:void-close"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=6,
    )
    unrelated = StableIdentifier("round:unrelated")
    _snapshot(
        service,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            unrelated,
            1,
            StableIdentifier("tournament:show"),
            unrelated,
            {"round_ordinal": 2, "predecessor_round_ids": [str(heat)], "successor_round_ids": []},
        ),
        "unrelated-round",
    )
    _append(
        service,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        unrelated,
        {"configured": True},
        "unrelated-configure",
    )
    with pytest.raises(ContractError, match="does not feed"):
        service.freeze_round_epoch(
            unrelated,
            epoch_revision=1,
            historical_cutoff_key="history:prior",
            closure_ids=(closure,),
            command_id=IdempotencyKey("command:unrelated-epoch"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=7,
        )


def test_lifecycle_exact_retry_paths_recheck_after_writer_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = LifecycleService(tmp_path / "retry.sqlite3")
    sentinel = object()

    def conflict(*_args, **_kwargs):
        raise EventStoreConflict("raced")

    monkeypatch.setattr(service._events, "execute", conflict)
    single_arguments = (
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        StableIdentifier("tournament:retry"),
        {"configured": True},
        IdempotencyKey("command:retry-single"),
        ACTOR,
        NOW,
        1,
    )
    answers = iter((None, sentinel))
    monkeypatch.setattr(service._events, "lookup_exact_retry", lambda **_kwargs: next(answers))
    assert service._execute(*single_arguments) is sentinel
    answers = iter((None, None))
    monkeypatch.setattr(service._events, "lookup_exact_retry", lambda **_kwargs: next(answers))
    with pytest.raises(EventStoreConflict, match="raced"):
        service._execute(*single_arguments)

    epoch_id = StableIdentifier("epoch:retry")
    round_id = StableIdentifier("round:retry")
    intents = (
        EventIntent(AggregateKind.EPOCH, epoch_id, EventKind.ROUND_EPOCH_FROZEN),
        EventIntent(AggregateKind.ROUND, round_id, EventKind.ROUND_FROZEN),
    )
    multi_arguments = (
        CommandKind.FREEZE_EVIDENCE_EPOCH,
        epoch_id,
        intents,
        {
            "schema_version": "strathmark-v3-epoch-event-v1",
            "epoch_id": str(epoch_id),
            "epoch": {"round_id": str(round_id)},
        },
        IdempotencyKey("command:retry-multi"),
        ACTOR,
        NOW,
        1,
    )
    monkeypatch.setattr(service._events, "lookup_exact_retry", lambda **_kwargs: sentinel)
    assert service._execute_multi(*multi_arguments) is sentinel
    answers = iter((None, sentinel))
    monkeypatch.setattr(service._events, "lookup_exact_retry", lambda **_kwargs: next(answers))
    assert service._execute_multi(*multi_arguments) is sentinel
    answers = iter((None, None))
    monkeypatch.setattr(service._events, "lookup_exact_retry", lambda **_kwargs: next(answers))
    with pytest.raises(EventStoreConflict, match="raced"):
        service._execute_multi(*multi_arguments)


def test_projection_public_guards_and_barrier_corruption_fail_closed(tmp_path: Path):
    with pytest.raises(ProjectionError, match="filesystem path"):
        SQLiteProjectionStore(True)

    service = LifecycleService(tmp_path / "projection-guards.sqlite3")
    with pytest.raises(ProjectionError, match="positive"):
        service.projections.pending_reactions(0)
    with pytest.raises(ProjectionError, match="positive"):
        service.projections.reaction_barrier_for_tournament("tournament:show", False)

    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            connection.execute("DELETE FROM v3_derivation_barrier")
    with pytest.raises(ProjectionError, match="missing"):
        service.projections.barrier_sequence()

    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            connection.execute("INSERT INTO v3_derivation_barrier VALUES (1, 0, ?)", ("0" * 64,))
    with pytest.raises(ProjectionError, match="digest"):
        service.projections.barrier_sequence()


def test_projection_apply_batch_guards_and_malformed_nested_correction(tmp_path: Path):
    service, *_ = _bootstrap(tmp_path)
    issue = _authority_event(service, EventKind.FIELD_ISSUED)
    other = _authority_event(service, EventKind.FIELD_OPTIMIZED)
    with open_v3_connection(service.projections.database_path) as connection:
        with pytest.raises(ProjectionError, match="writer transaction"):
            service.projections.apply_events(connection, (issue,))
        with immediate_transaction(connection):
            with pytest.raises(ProjectionError, match="authoritative events"):
                service.projections.apply_events(connection, ())
            with pytest.raises(ProjectionError, match="exactly one command"):
                service.projections.apply_events(connection, (issue, other))

            blob = BlobReference(
                StableIdentifier("blob:defensive"), "0" * 64, 65_537, "application/json"
            )
            blob_event = _event_variant(issue, payload=blob)
            with pytest.raises(ProjectionError, match="inline payloads"):
                service.projections.apply_events(connection, (blob_event,))

            malformed = _event_variant(
                issue,
                payload={
                    "schema_version": "strathmark-v3-correction-settlement-v1",
                    "settlement": [],
                },
                command_kind=CommandKind.RECORD_RESULT,
                event_kind=EventKind.LIVE_RACE_SETTLED,
            )
            with pytest.raises(ProjectionError, match="atomic correction payload"):
                service.projections.apply_events(connection, (malformed,))


def test_projection_atomic_command_shapes_fail_closed(tmp_path: Path):
    service, _, field = _bootstrap(tmp_path)
    epoch_event = _authority_event(service, EventKind.ROUND_EPOCH_FROZEN)
    round_event = _authority_event(service, EventKind.ROUND_FROZEN)
    issue = _authority_event(service, EventKind.FIELD_ISSUED)
    result_payload = {
        "schema_version": "strathmark-v3-correction-settlement-v1",
        "result": {},
    }
    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            validate = service.projections._validate_atomic_event_set
            with pytest.raises(ProjectionConflict, match="atomically freeze"):
                validate(connection, (epoch_event,))
            wrong_round = _event_variant(
                round_event,
                payload=epoch_event.command.payload.to_value(),
                command_kind=CommandKind.FREEZE_EVIDENCE_EPOCH,
                aggregate_id=StableIdentifier("round:wrong"),
                command_id="command:wrong-round-freeze",
            )
            with pytest.raises(ProjectionConflict, match="round authority"):
                validate(connection, (epoch_event, wrong_round))

            settlement_only = _event_variant(
                issue,
                payload={"field_id": str(field)},
                command_kind=CommandKind.SETTLE_LIVE_RACE,
                event_kind=EventKind.LIVE_RACE_SETTLED,
                aggregate_kind=AggregateKind.SETTLEMENT,
                aggregate_id=StableIdentifier("settlement:defensive"),
                command_id="command:missing-field-settle",
            )
            with pytest.raises(ProjectionConflict, match="atomically settle"):
                validate(connection, (settlement_only,))
            wrong_field = _event_variant(
                issue,
                payload={"field_id": "field:other"},
                command_kind=CommandKind.SETTLE_LIVE_RACE,
                event_kind=EventKind.LIVE_RACE_SETTLED,
                aggregate_kind=AggregateKind.SETTLEMENT,
                aggregate_id=StableIdentifier("settlement:defensive"),
                command_id="command:wrong-field-settle",
            )
            field_settled = _event_variant(
                issue,
                payload={"field_id": "field:other"},
                command_kind=CommandKind.SETTLE_LIVE_RACE,
                event_kind=EventKind.FIELD_SETTLED,
                command_id="command:wrong-field-settle",
            )
            with pytest.raises(ProjectionConflict, match="field authority"):
                validate(connection, (wrong_field, field_settled))

            missing_ingress = _event_variant(
                issue,
                payload={},
                command_kind=CommandKind.REVISE_FIELD_ROSTER,
                command_id="command:missing-field-ingress",
            )
            with pytest.raises(ProjectionConflict, match="no ingress"):
                validate(connection, (missing_ingress,))

            missing_result = _event_variant(
                issue,
                payload=result_payload,
                command_kind=CommandKind.SUPERSEDE_AND_SETTLE_RESULT,
                event_kind=EventKind.LIVE_RACE_SETTLED,
                aggregate_kind=AggregateKind.SETTLEMENT,
                aggregate_id=StableIdentifier("settlement:correction"),
                command_id="command:missing-correction-result",
            )
            with pytest.raises(ProjectionConflict, match="no result supersession"):
                validate(connection, (missing_result,))
            malformed_result = _event_variant(
                issue,
                payload={
                    "schema_version": "strathmark-v3-correction-settlement-v1",
                    "result": [],
                },
                command_kind=CommandKind.SUPERSEDE_AND_SETTLE_RESULT,
                event_kind=EventKind.RESULT_SUPERSEDED,
                aggregate_kind=AggregateKind.RESULT,
                aggregate_id=StableIdentifier("result:correction"),
                command_id="command:malformed-correction-result",
            )
            with pytest.raises(ProjectionConflict, match="result payload"):
                validate(connection, (malformed_result,))


def test_projection_snapshot_contract_defensive_matrix(tmp_path: Path):
    service, _, _ = _bootstrap(tmp_path)
    tournament_event = _authority_event(service, EventKind.TOURNAMENT_SNAPSHOT_REVISED)
    round_event = _authority_event(service, EventKind.ROUND_SNAPSHOT_REVISED)
    field_event = _authority_event(service, EventKind.FIELD_ROSTER_REVISED)

    tournament_value = tournament_event.command.payload.to_value()
    tournament_snapshot = dict(tournament_value["snapshot"])
    wrong_kind = dict(tournament_value)
    wrong_kind["entity_kind"] = "round"
    with pytest.raises(ProjectionConflict, match="event kind"):
        service.projections._validate_snapshot_contract(
            tournament_event, wrong_kind, tournament_snapshot
        )
    wrong_tournament = dict(tournament_value)
    wrong_tournament["round_id"] = "round:wrong"
    with pytest.raises(ProjectionConflict, match="tournament snapshot"):
        service.projections._validate_snapshot_contract(
            tournament_event, wrong_tournament, tournament_snapshot
        )

    round_value = round_event.command.payload.to_value()
    round_snapshot = dict(round_value["snapshot"])
    wrong_round = dict(round_value)
    wrong_round["entity_id"] = "round:wrong"
    wrong_round_event = _event_variant(
        round_event,
        payload=wrong_round,
        aggregate_id=deterministic_identifier(
            AggregateKind.ROUND_INGRESS.value, {"entity_id": "round:wrong"}
        ),
        command_id="command:wrong-round-snapshot",
    )
    with pytest.raises(ProjectionConflict, match="round snapshot identity"):
        service.projections._validate_snapshot_contract(
            wrong_round_event, wrong_round, round_snapshot
        )
    for ordinal in (True, 0, "1"):
        malformed = {**round_snapshot, "round_ordinal": ordinal}
        with pytest.raises(ProjectionConflict, match="ordinal"):
            service.projections._validate_snapshot_contract(round_event, round_value, malformed)
    malformed = {**round_snapshot, "predecessor_round_ids": "round:prior"}
    with pytest.raises(ProjectionConflict, match="arrays"):
        service.projections._validate_snapshot_contract(round_event, round_value, malformed)
    malformed = {
        **round_snapshot,
        "successor_round_ids": ["round:quarter", "round:quarter"],
    }
    with pytest.raises(ProjectionConflict, match="cannot repeat"):
        service.projections._validate_snapshot_contract(round_event, round_value, malformed)

    field_value = field_event.command.payload.to_value()
    field_snapshot = dict(field_value["snapshot"])
    with pytest.raises(ProjectionConflict, match="fields are invalid"):
        service.projections._validate_snapshot_contract(
            field_event, field_value, {**field_snapshot, "free_text": "forbidden"}
        )
    for malformed in (
        {**field_snapshot, "competitor_ids": []},
        {**field_snapshot, "stand_ids": "stand:one"},
    ):
        with pytest.raises(ProjectionConflict, match="must be arrays"):
            service.projections._validate_snapshot_contract(field_event, field_value, malformed)
    for malformed in (
        {
            **field_snapshot,
            "competitor_ids": ["competitor:a", "competitor:a"],
        },
        {**field_snapshot, "stand_ids": ["stand:one", "stand:one"]},
        {**field_snapshot, "stand_ids": ["stand:one"]},
    ):
        with pytest.raises(ProjectionConflict, match="one-to-one"):
            service.projections._validate_snapshot_contract(field_event, field_value, malformed)


def test_projection_top_level_payloads_and_lineage_fail_closed(tmp_path: Path):
    service, _, _ = _bootstrap(tmp_path)
    opened = _authority_event(service, EventKind.TOURNAMENT_OPENED)
    opened_value = opened.command.payload.to_value()
    snapshot_event = _authority_event(service, EventKind.TOURNAMENT_SNAPSHOT_REVISED)
    snapshot_value = snapshot_event.command.payload.to_value()
    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            for malformed in ({}, {**opened_value, "schema_version": "wrong"}):
                with pytest.raises(ProjectionError, match="payload is not closed"):
                    service.projections._apply_tournament_open(connection, opened, malformed)
            for roots in ([], "round:heat", ["round:heat", "round:heat"]):
                with pytest.raises(ProjectionError, match="unique root rounds"):
                    service.projections._apply_tournament_open(
                        connection, opened, {**opened_value, "root_round_ids": roots}
                    )
            with pytest.raises(ProjectionConflict, match="boundary drifted"):
                service.projections._apply_tournament_open(
                    connection, opened, {**opened_value, "bundle_id": "bundle:wrong"}
                )
            with pytest.raises(ProjectionConflict, match="lineage is incomplete"):
                service.projections._apply_tournament_open(
                    connection, opened, {**opened_value, "root_round_ids": ["round:other"]}
                )

            close_value = {
                "schema_version": "strathmark-v3-tournament-close-v1",
                "deferred_reactions": ["cancel_jobs", "expire_overlay", "seal_exports"],
            }
            with pytest.raises(ProjectionError, match="payload is not closed"):
                service.projections._apply_tournament_close(connection, opened, {})
            with pytest.raises(ProjectionError, match="reactions are not closed"):
                service.projections._apply_tournament_close(
                    connection, opened, {**close_value, "deferred_reactions": []}
                )
            with pytest.raises(ProjectionConflict, match="every configured round"):
                service.projections._apply_tournament_close(connection, opened, close_value)

            with pytest.raises(ProjectionError, match="payload is not closed"):
                service.projections._apply_snapshot(connection, snapshot_event, {})
            for revision in (False, 0, "2"):
                with pytest.raises(ProjectionError, match="revision must be positive"):
                    service.projections._apply_snapshot(
                        connection,
                        snapshot_event,
                        {**snapshot_value, "upstream_revision": revision},
                    )
            with pytest.raises(ProjectionConflict, match="parent lineage"):
                service.projections._apply_snapshot(
                    connection,
                    snapshot_event,
                    {
                        **snapshot_value,
                        "upstream_revision": 2,
                        "tournament_id": "tournament:other",
                    },
                )
            with pytest.raises(ProjectionError, match="digest mismatch"):
                service.projections._apply_snapshot(
                    connection,
                    snapshot_event,
                    {**snapshot_value, "upstream_revision": 2, "snapshot_digest": "0" * 64},
                )


def test_projection_result_replay_rederives_admission_and_lineage(tmp_path: Path):
    service, _, field = _bootstrap(tmp_path)
    service.record_live_result(
        _submission(field, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:projection-result"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    event = _authority_event(service, EventKind.RESULT_RECORDED)
    value = event.command.payload.to_value()
    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "DELETE FROM v3_derivation_reactions WHERE source_global_sequence=?",
                (event.global_sequence,),
            )
            connection.execute(
                "DELETE FROM v3_result_revisions WHERE source_global_sequence=?",
                (event.global_sequence,),
            )
            with pytest.raises(ProjectionError, match="payload is not closed"):
                service.projections._apply_result(connection, event, {})
            with pytest.raises(ProjectionError, match="submission must be an object"):
                service.projections._apply_result(connection, event, {**value, "submission": []})
            with pytest.raises(ProjectionError, match="eligibility must be explicit"):
                service.projections._apply_result(
                    connection, event, {**value, "candidate_numeric_eligible": 1}
                )
            revision_two = dict(value["submission"])
            revision_two["result"] = {
                **revision_two["result"],
                "revision": 2,
                "supersedes_revision": 1,
            }
            with pytest.raises(ProjectionConflict, match="revisions must be consecutive"):
                service.projections._apply_result(
                    connection, event, {**value, "submission": revision_two}
                )

            missing_submission = dict(value["submission"])
            missing_submission["field_id"] = "field:missing"
            missing_key = deterministic_identifier(
                "result",
                {
                    "field_id": "field:missing",
                    "field_revision": 1,
                    "competitor_id": missing_submission["competitor_id"],
                },
            )
            missing_value = {
                **value,
                "result_key": str(missing_key),
                "submission": missing_submission,
            }
            missing_event = _event_variant(
                event,
                payload=missing_value,
                aggregate_kind=AggregateKind.RESULT,
                aggregate_id=missing_key,
                command_id="command:missing-result-ingress",
            )
            with pytest.raises(ProjectionConflict, match="no authoritative ingress"):
                service.projections._apply_result(connection, missing_event, missing_value)

            spoofed = dict(value["submission"])
            spoofed["tournament_id"] = "tournament:other"
            with pytest.raises(ProjectionConflict, match="lineage or context"):
                service.projections._apply_result(
                    connection, event, {**value, "submission": spoofed}
                )

            wrong_mark = dict(value["submission"])
            wrong_mark["issued_mark"] = 4
            with pytest.raises(ProjectionConflict, match="authoritative issue"):
                service.projections._apply_result(
                    connection, event, {**value, "submission": wrong_mark}
                )
            with pytest.raises(ProjectionConflict, match="admission claims"):
                service.projections._apply_result(
                    connection, event, {**value, "candidate_numeric_eligible": False}
                )


def test_projection_derivation_and_settlement_payload_guards(tmp_path: Path):
    service, _, field = _bootstrap(tmp_path)
    sources = []
    for competitor, status in (("a", ResultStatus.COMPLETION), ("b", ResultStatus.DNS)):
        receipt = service.record_live_result(
            _submission(field, competitor, status),
            field_revision=1,
            claimed_receipt_id=StableIdentifier("receipt:heat-a"),
            command_id=IdempotencyKey(f"command:projection-derive-{competitor}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
        sources.append(receipt.first_global_sequence)
    service.settle_live_race(
        field,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:projection-derive-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=2,
    )
    _complete_source(service, sources[0], "projection-derive")
    reaction = _authority_event(service, EventKind.DERIVATION_REACTION_COMPLETED)
    reaction_value = reaction.command.payload.to_value()
    sequence = _authority_event(service, EventKind.DERIVATION_SEQUENCE_COMPLETED)
    sequence_value = sequence.command.payload.to_value()
    settlement = _authority_event(service, EventKind.LIVE_RACE_SETTLED)
    settlement_value = settlement.command.payload.to_value()

    def settlement_variant(value, key):
        return _event_variant(
            settlement,
            payload=value,
            aggregate_kind=AggregateKind.SETTLEMENT,
            aggregate_id=deterministic_identifier(
                "settlement",
                {name: item for name, item in value.items() if name != "schema_version"},
            ),
            command_id=f"command:{key}",
        )

    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            with pytest.raises(ProjectionError, match="reaction payload is not closed"):
                service.projections._apply_derivation_reaction(connection, reaction, {})
            with pytest.raises(ProjectionError, match="vocabulary"):
                service.projections._apply_derivation_reaction(
                    connection, reaction, {**reaction_value, "reaction": "invented"}
                )
            with pytest.raises(ContractError, match="SHA-256"):
                service.projections._apply_derivation_reaction(
                    connection, reaction, {**reaction_value, "output_digest": "BAD"}
                )
            with pytest.raises(ProjectionError, match="sequence payload is not closed"):
                service.projections._apply_derivation_sequence(connection, sequence, {})
            connection.execute(
                "DELETE FROM v3_derivation_reactions WHERE source_global_sequence=? "
                "AND reaction_type=?",
                (sources[0], next(iter(MandatoryReaction)).value),
            )
            with pytest.raises(ProjectionConflict, match="before every reaction"):
                service.projections._apply_derivation_sequence(connection, sequence, sequence_value)

            with pytest.raises(ProjectionError, match="settlement payload is not closed"):
                service.projections._apply_settlement(connection, settlement, {})
            with pytest.raises(ProjectionError, match="results must be an array"):
                malformed_results = {**settlement_value, "results": {}}
                service.projections._apply_settlement(
                    connection,
                    settlement_variant(malformed_results, "malformed-results"),
                    malformed_results,
                )
            missing_issue = {**settlement_value, "field_id": "field:missing"}
            missing_issue["results"] = []
            missing_event = _event_variant(
                settlement,
                payload=missing_issue,
                aggregate_kind=AggregateKind.SETTLEMENT,
                aggregate_id=deterministic_identifier(
                    "settlement",
                    {key: item for key, item in missing_issue.items() if key != "schema_version"},
                ),
                command_id="command:projection-missing-issue",
            )
            with pytest.raises(ProjectionConflict, match="acknowledged issued receipt"):
                service.projections._apply_settlement(connection, missing_event, missing_issue)
            with pytest.raises(ProjectionConflict, match="acknowledged receipt"):
                wrong_receipt = {**settlement_value, "receipt_id": "receipt:wrong"}
                service.projections._apply_settlement(
                    connection,
                    settlement_variant(wrong_receipt, "wrong-receipt"),
                    wrong_receipt,
                )
            with pytest.raises(ProjectionError, match="identity is malformed"):
                malformed_identity = {**settlement_value, "results": [{}]}
                service.projections._apply_settlement(
                    connection,
                    settlement_variant(malformed_identity, "malformed-identity"),
                    malformed_identity,
                )
            wrong_result = [dict(item) for item in settlement_value["results"]]
            wrong_result[0]["revision"] = 99
            with pytest.raises(ProjectionConflict, match="wrong result"):
                wrong_result_value = {**settlement_value, "results": wrong_result}
                service.projections._apply_settlement(
                    connection,
                    settlement_variant(wrong_result_value, "wrong-result"),
                    wrong_result_value,
                )
            duplicate = [dict(settlement_value["results"][0])] * 2
            with pytest.raises(ProjectionConflict, match="exactly one"):
                duplicate_value = {**settlement_value, "results": duplicate}
                service.projections._apply_settlement(
                    connection,
                    settlement_variant(duplicate_value, "duplicate-result"),
                    duplicate_value,
                )


def test_projection_reaction_ledger_corruption_and_tournament_barrier(tmp_path: Path):
    service, _, field = _bootstrap(tmp_path / "pending")
    receipt = service.record_live_result(
        _submission(field, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:ledger-pending"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    barrier = service.projections.reaction_barrier_for_tournament(
        StableIdentifier("tournament:show"), receipt.first_global_sequence
    )
    assert barrier.through_sequence == receipt.first_global_sequence - 1
    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "DELETE FROM v3_derivation_reactions WHERE source_global_sequence=? "
                "AND reaction_type=?",
                (receipt.first_global_sequence, next(iter(MandatoryReaction)).value),
            )
    with pytest.raises(ProjectionError, match="registration is incomplete"):
        service.projections.pending_reactions(receipt.first_global_sequence)

    stray = LifecycleService(tmp_path / "stray.sqlite3")
    _snapshot(
        stray,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            StableIdentifier("tournament:stray"),
            1,
            StableIdentifier("tournament:stray"),
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "stray-snapshot",
    )
    source = _authority_event(stray, EventKind.TOURNAMENT_SNAPSHOT_REVISED).global_sequence
    with open_v3_connection(stray.projections.database_path) as connection:
        with immediate_transaction(connection):
            stray.projections._register_result_source(connection, source, NOW)
    with pytest.raises(ProjectionError, match="non-authoritative source"):
        SQLiteProjectionStore(stray.projections.database_path)


def test_projection_issue_prepare_and_supersession_guards(tmp_path: Path):
    service, _, field = _bootstrap(tmp_path)
    issue = _authority_event(service, EventKind.FIELD_ISSUED)
    issue_value = issue.command.payload.to_value()
    prepared = _authority_event(service, EventKind.FIELD_OPTIMIZED)
    prepared_value = prepared.command.payload.to_value()
    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            with pytest.raises(ProjectionError, match="mandatory U5 bindings"):
                service.projections._apply_issue_seal(connection, issue, {})
            missing_ingress = _event_variant(
                issue,
                payload=issue_value,
                aggregate_id=StableIdentifier("field:missing"),
                command_id="command:missing-issue-ingress",
            )
            with pytest.raises(ProjectionConflict, match="authoritative field ingress"):
                service.projections._apply_issue_seal(connection, missing_ingress, issue_value)
            for marks in (
                [],
                {"competitor:a": 3},
                {"competitor:a": True, "competitor:b": 3},
                {"competitor:a": 0, "competitor:b": 3},
            ):
                with pytest.raises(ProjectionConflict, match="issued marks"):
                    service.projections._apply_issue_seal(
                        connection, issue, {**issue_value, "issued_marks": marks}
                    )
            with pytest.raises(ProjectionConflict, match="exact roster revision"):
                service.projections._apply_issue_seal(
                    connection,
                    issue,
                    {
                        **issue_value,
                        "competitor_ids": ["competitor:b", "competitor:a"],
                        "issued_marks": {"competitor:a": 3, "competitor:b": 3},
                    },
                )
            with pytest.raises(ProjectionConflict, match="mixed epochs"):
                service.projections._apply_issue_seal(
                    connection, issue, {**issue_value, "epoch_id": "epoch:other"}
                )
            connection.execute(
                "DELETE FROM v3_round_issue_seals WHERE round_id=?", (issue_value["round_id"],)
            )
            connection.execute(
                "DELETE FROM v3_prepared_field_dependencies WHERE field_id=?", (str(field),)
            )
            with pytest.raises(ProjectionConflict, match="prepared dependency"):
                service.projections._apply_issue_seal(connection, issue, issue_value)

            with pytest.raises(ProjectionError, match="mandatory U5 bindings"):
                service.projections._apply_prepared_dependency(connection, prepared, {})
            for revision in (False, 0, "1"):
                with pytest.raises(ProjectionError, match="revision must be positive"):
                    service.projections._apply_prepared_dependency(
                        connection, prepared, {**prepared_value, "field_revision": revision}
                    )
            missing_prepared_ingress = _event_variant(
                prepared,
                payload=prepared_value,
                aggregate_id=StableIdentifier("field:missing"),
                command_id="command:missing-prepared-ingress",
            )
            with pytest.raises(ProjectionConflict, match="authoritative field ingress"):
                service.projections._apply_prepared_dependency(
                    connection, missing_prepared_ingress, prepared_value
                )
            superseded = _event_variant(
                prepared,
                payload={},
                command_kind=CommandKind.SUPERSEDE_FIELD,
                event_kind=EventKind.FIELD_SUPERSEDED,
                command_id="command:missing-dependency-supersede",
            )
            with pytest.raises(ProjectionConflict, match="no current prepared dependency"):
                service.projections._apply_field_superseded(connection, superseded)


def test_projection_round_closure_and_epoch_replay_guards(tmp_path: Path):
    service, tournament, root, successor, closure_id = _bootstrap_empty_closure(tmp_path)
    closure = _authority_event(service, EventKind.ROUND_CLOSED, aggregate_id=str(root))
    closure_value = closure.command.payload.to_value()
    epoch_event = _authority_event(service, EventKind.ROUND_EPOCH_FROZEN, aggregate_id=None)
    # The latest epoch is the successor freeze from the empty predecessor closure.
    epoch_value = epoch_event.command.payload.to_value()
    assert epoch_value["epoch"]["round_id"] == str(successor)

    with open_v3_connection(service.projections.database_path) as connection:
        with immediate_transaction(connection):
            with pytest.raises(ProjectionError, match="closure payload is not closed"):
                service.projections._apply_round_closure(connection, closure, {})
            wrong_source = _event_variant(
                closure,
                payload=closure_value,
                aggregate_kind=AggregateKind.ROUND,
                aggregate_id=StableIdentifier("round:wrong"),
                command_id="command:wrong-closure-source",
            )
            with pytest.raises(ProjectionConflict, match="source does not match"):
                service.projections._apply_round_closure(connection, wrong_source, closure_value)
            with pytest.raises(ProjectionConflict, match="tournament lineage"):
                service.projections._apply_round_closure(
                    connection,
                    closure,
                    {**closure_value, "tournament_id": "tournament:other"},
                )
            with pytest.raises(ProjectionConflict, match="successor lineage"):
                service.projections._apply_round_closure(
                    connection, closure, {**closure_value, "target_round_ids": []}
                )
            with pytest.raises(ProjectionError, match="result set digest"):
                service.projections._apply_round_closure(
                    connection, closure, {**closure_value, "results": {}}
                )
            forged_results = [
                {
                    "result_key": "result:forged",
                    "revision": 1,
                    "source_sequence": closure.global_sequence - 1,
                    "numeric_eligible": True,
                }
            ]
            with pytest.raises(ProjectionConflict, match="exact active settled"):
                service.projections._apply_round_closure(
                    connection,
                    closure,
                    {
                        **closure_value,
                        "results": forged_results,
                        "result_set_digest": canonical_digest(forged_results),
                    },
                )
            with pytest.raises(ProjectionConflict, match="content addressed"):
                service.projections._apply_round_closure(
                    connection, closure, {**closure_value, "closure_id": "round_closure:wrong"}
                )
            with pytest.raises(ProjectionConflict, match="frozen evidence epoch"):
                service.projections._active_set_for_round(
                    connection,
                    str(tournament),
                    "round:missing",
                    before_sequence=epoch_event.global_sequence + 1,
                )

            with pytest.raises(ProjectionError, match="epoch event payload is not closed"):
                service.projections._apply_epoch(connection, epoch_event, {})
            with pytest.raises(ProjectionError, match="epoch event digest"):
                service.projections._apply_epoch(
                    connection, epoch_event, {**epoch_value, "epoch": []}
                )
            for closures in ("round_closure:none", [str(closure_id), str(closure_id)]):
                with pytest.raises(ProjectionError, match="unique array"):
                    service.projections._apply_epoch(
                        connection, epoch_event, {**epoch_value, "closure_ids": closures}
                    )
            missing_epoch = {**epoch_value["epoch"], "round_id": "round:missing"}
            missing_epoch_value = {
                **epoch_value,
                "epoch": missing_epoch,
                "content_digest": canonical_digest(missing_epoch),
            }
            with pytest.raises(ProjectionConflict, match="no authoritative round ingress"):
                service.projections._apply_epoch(connection, epoch_event, missing_epoch_value)
            with pytest.raises(ProjectionConflict, match="unrelated predecessor"):
                service.projections._apply_epoch(
                    connection,
                    epoch_event,
                    {**epoch_value, "closure_ids": ["round_closure:missing"]},
                )
            with pytest.raises(ProjectionConflict, match="missing a predecessor"):
                service.projections._apply_epoch(
                    connection, epoch_event, {**epoch_value, "closure_ids": []}
                )
            wrong_history = {**epoch_value["epoch"], "historical_cutoff_key": "history:wrong"}
            with pytest.raises(ProjectionConflict, match="historical boundary"):
                service.projections._apply_epoch(
                    connection,
                    epoch_event,
                    {
                        **epoch_value,
                        "epoch": wrong_history,
                        "content_digest": canonical_digest(wrong_history),
                    },
                )
            wrong_content = {
                **epoch_value["epoch"],
                "maximum_tournament_sequence": epoch_value["epoch"]["maximum_tournament_sequence"]
                + 1,
            }
            with pytest.raises(ProjectionConflict, match="closed causal lineage"):
                service.projections._apply_epoch(
                    connection,
                    epoch_event,
                    {
                        **epoch_value,
                        "epoch": wrong_content,
                        "content_digest": canonical_digest(wrong_content),
                    },
                )
            with pytest.raises(ProjectionConflict, match="content addressed"):
                service.projections._apply_epoch(
                    connection, epoch_event, {**epoch_value, "epoch_id": "epoch:wrong"}
                )

            connection.execute(
                "INSERT INTO v3_result_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "result:pending-barrier",
                    str(tournament),
                    1,
                    closure.global_sequence,
                    str(root),
                    "field:synthetic",
                    "competitor:synthetic",
                    1,
                    "receipt:synthetic",
                    "{}",
                    "0" * 64,
                    1,
                    1,
                    "eligible_completion",
                    closure.global_sequence,
                ),
            )
            service.projections._register_result_source(connection, closure.global_sequence, NOW)
            with pytest.raises(ProjectionConflict, match="derivation barrier"):
                service.projections._apply_epoch(connection, epoch_event, epoch_value)
            connection.execute(
                "DELETE FROM v3_derivation_reactions WHERE source_global_sequence=?",
                (closure.global_sequence,),
            )
            connection.execute(
                "DELETE FROM v3_result_revisions WHERE source_global_sequence=?",
                (closure.global_sequence,),
            )

            connection.execute(
                "INSERT INTO v3_round_issue_seals VALUES (?, ?, ?, ?)",
                (str(successor), epoch_value["epoch_id"], closure.global_sequence, NOW),
            )
            with pytest.raises(ProjectionConflict, match="issued round cannot refreeze"):
                service.projections._apply_epoch(connection, epoch_event, epoch_value)

    unopened = LifecycleService(tmp_path / "unopened.sqlite3")
    unopened_tournament = StableIdentifier("tournament:unopened")
    unopened_round = StableIdentifier("round:unopened")
    _snapshot(
        unopened,
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            unopened_tournament,
            1,
            unopened_tournament,
            None,
            {"bundle_id": "bundle:verified", "historical_cutoff_key": "history:prior"},
        ),
        "unopened-tournament",
    )
    _snapshot(
        unopened,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            unopened_round,
            1,
            unopened_tournament,
            unopened_round,
            {
                "round_ordinal": 1,
                "predecessor_round_ids": [],
                "successor_round_ids": [],
            },
        ),
        "unopened-round",
    )
    unopened_epoch = {
        **epoch_value["epoch"],
        "round_id": str(unopened_round),
        "maximum_tournament_sequence": 1,
        "members": [],
    }
    unopened_value = {
        "schema_version": "strathmark-v3-epoch-event-v1",
        "epoch_id": str(deterministic_identifier("epoch", unopened_epoch)),
        "content_digest": canonical_digest(unopened_epoch),
        "epoch": unopened_epoch,
        "closure_ids": [],
    }
    unopened_event = _event_variant(
        epoch_event,
        payload=unopened_value,
        aggregate_kind=AggregateKind.EPOCH,
        aggregate_id=StableIdentifier(unopened_value["epoch_id"]),
        command_id="command:unopened-epoch",
    )
    with open_v3_connection(unopened.projections.database_path) as connection:
        with immediate_transaction(connection):
            with pytest.raises(ProjectionConflict, match="tournament-open authority"):
                unopened.projections._apply_epoch(connection, unopened_event, unopened_value)
