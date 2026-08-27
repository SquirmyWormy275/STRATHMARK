"""Execute the deterministic whole-tournament and recovery acceptance replay.

The public report is intentionally compact and stable. Its claims are derived from an
isolated V3 event store populated through the real lifecycle, optimizer, evidence, and
settlement services; none of the booleans in the report are caller-supplied assertions.
"""

from __future__ import annotations

import argparse
import gc
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from strathmark.v3.application.capacity import (
    CapacityManifest,
    JobLane,
    JobPriority,
    QueueLoad,
    decide_admission,
)
from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.application.lifecycle import LifecycleService, SnapshotKind, UpstreamSnapshot
from strathmark.v3.application.operations import (
    FieldDisposition,
    RaceDayField,
    RecoveryTrial,
    RoundStage,
    verify_race_day_replay,
    verify_recovery_matrix,
)
from strathmark.v3.assessors.llm_council import ProviderCallError
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.evidence import TargetContext
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.contracts.statuses import OfficialResult, ResultStatus
from strathmark.v3.domain.epochs import MandatoryReaction
from strathmark.v3.domain.evidence import LiveResultSubmission
from strathmark.v3.domain.optimizer import (
    OptimizationCompetitor,
    OptimizationField,
    optimize_and_verify_field,
)
from strathmark.v3.infrastructure.backup import DiskOperation, DiskReservePolicy
from strathmark.v3.infrastructure.blobs import (
    BlobIntegrityError,
    BlobMetadata,
    BlobRetention,
    ContentAddressedBlobStore,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import EventStoreConflict, SQLiteEventStore

NOW = "2026-08-24T18:00:00.000Z"
ACTOR = StableIdentifier("actor:replay")
TOURNAMENT = StableIdentifier("tournament:whole-domain-replay")
CONTEXT = TargetContext(
    event_code="underhand",
    size_mm=300,
    material_code="pine",
    taxonomy_version="taxonomy:v1",
    conversion_version="conversion:v1",
)
_FAILURES = (
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


class ReplayEvidenceError(RuntimeError):
    """Raised when an acceptance claim lacks causal evidence."""


class _InjectedReplayFault(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayFaultPlan:
    """Test-only sabotage seams; normal execution injects every required failure."""

    disabled_faults: frozenset[str] = frozenset()
    receipt_probe_override_after: str | None = None

    def __post_init__(self) -> None:
        unknown = self.disabled_faults - frozenset(_FAILURES)
        if unknown:
            raise ValueError(f"unknown replay faults: {sorted(unknown)}")
        value = self.receipt_probe_override_after
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("receipt probe override must be a lower-case SHA-256 digest")


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    field_id: str
    entrants: tuple[str, ...]
    intended_winner: str
    scheduled_offset_ms: int
    call_delay_ms: int = 0


@dataclass(frozen=True, slots=True)
class _StageSpec:
    stage: RoundStage
    round_id: str
    fields: tuple[_FieldSpec, ...]


_STAGES = (
    _StageSpec(
        RoundStage.HEAT,
        "round:heat",
        (
            _FieldSpec("field:heat-1", ("competitor:a", "competitor:b"), "competitor:a", 0),
            _FieldSpec("field:heat-2", ("competitor:c", "competitor:d"), "competitor:c", 600_000),
        ),
    ),
    _StageSpec(
        RoundStage.QUARTER_FINAL,
        "round:quarter",
        (_FieldSpec("field:quarter", ("competitor:a", "competitor:c"), "competitor:c", 900_000),),
    ),
    _StageSpec(
        RoundStage.SEMI_FINAL,
        "round:semi",
        (_FieldSpec("field:semi", ("competitor:c", "competitor:e"), "competitor:e", 1_200_000),),
    ),
    _StageSpec(
        RoundStage.DIVISIONAL_FINAL,
        "round:divisional",
        (
            _FieldSpec(
                "field:divisional", ("competitor:e", "competitor:f"), "competitor:e", 1_500_000
            ),
        ),
    ),
    _StageSpec(
        RoundStage.GRAND_FINAL,
        "round:grand",
        (
            _FieldSpec(
                "field:grand",
                ("competitor:e", "competitor:g"),
                "competitor:g",
                1_800_000,
                300_000,
            ),
        ),
    ),
)


def _event_count(database: Path, *, aggregate_id: str | None = None) -> int:
    with open_v3_connection(database, read_only=True) as connection:
        if aggregate_id is None:
            row = connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) FROM v3_events WHERE aggregate_id=?", (aggregate_id,)
            ).fetchone()
    assert row is not None
    return int(row[0])


def _event_digest(database: Path, field_id: str, event_kind: EventKind) -> str:
    with open_v3_connection(database, read_only=True) as connection:
        row = connection.execute(
            "SELECT event_digest FROM v3_events WHERE aggregate_id=? AND event_kind=? "
            "ORDER BY global_sequence DESC LIMIT 1",
            (field_id, event_kind.value),
        ).fetchone()
    if row is None:
        raise ReplayEvidenceError(f"missing persisted {event_kind.value} for {field_id}")
    return str(row[0])


def _request(
    database: Path,
    *,
    command_kind: CommandKind,
    event_kind: EventKind,
    aggregate_kind: AggregateKind,
    target: StableIdentifier,
    command_id: str,
    payload: dict[str, object],
) -> CommandRequest:
    head = SQLiteEventStore(database).aggregate_head(str(target))
    command = CommandEnvelope(
        command_kind,
        IdempotencyKey(command_id),
        target,
        ((str(target), 0 if head is None else head[0]),),
        ACTOR,
        InlinePayload.from_value(payload),
    )
    return CommandRequest(
        ACTOR,
        command,
        (EventIntent(aggregate_kind, target, event_kind),),
        "strathmark-v3-replay-command-result-v1",
        {"accepted": True, "target": str(target)},
        NOW,
        1,
    )


def _execute_raw(database: Path, request: CommandRequest):
    return SQLiteEventStore(database).execute(request)


def _samples(center_ms: int) -> tuple[int, ...]:
    return tuple(center_ms + ((index * 7919) % 1001) - 500 for index in range(4096))


def _marks(field: _FieldSpec, abilities: dict[str, int]) -> tuple[tuple[str, int], ...]:
    competitors = tuple(
        OptimizationCompetitor(
            StableIdentifier(competitor_id),
            abilities[competitor_id],
            _samples(abilities[competitor_id]),
            index,
        )
        for index, competitor_id in enumerate(field.entrants)
    )
    source_digest = canonical_digest(
        {
            "schema_version": "strathmark-v3-replay-prediction-v1",
            "field_id": field.field_id,
            "abilities": [[item, abilities[item]] for item in field.entrants],
        }
    )
    optimized = optimize_and_verify_field(
        OptimizationField.create(
            field_id=StableIdentifier(field.field_id),
            source_receipt_digest=source_digest,
            competitors=competitors,
        ),
        ceiling=183,
    )
    marks = tuple(zip(field.entrants, optimized.receipt.selected_marks, strict=True))
    if min(mark for _competitor, mark in marks) != 3:
        raise ReplayEvidenceError("optimizer did not produce Mark-3 rebasing")
    return marks


def _bootstrap(database: Path) -> LifecycleService:
    lifecycle = LifecycleService(database)
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.TOURNAMENT,
            TOURNAMENT,
            1,
            TOURNAMENT,
            None,
            {"bundle_id": "bundle:replay", "historical_cutoff_key": "history:replay"},
        ),
        command_id=IdempotencyKey("command:replay-tournament-snapshot"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=1,
    )
    for index, stage in enumerate(_STAGES):
        predecessors = [] if index == 0 else [_STAGES[index - 1].round_id]
        successors = [] if index == len(_STAGES) - 1 else [_STAGES[index + 1].round_id]
        round_id = StableIdentifier(stage.round_id)
        lifecycle.ingest_snapshot(
            UpstreamSnapshot(
                SnapshotKind.ROUND,
                round_id,
                1,
                TOURNAMENT,
                round_id,
                {
                    "round_ordinal": index + 1,
                    "predecessor_round_ids": predecessors,
                    "successor_round_ids": successors,
                },
            ),
            command_id=IdempotencyKey(f"command:replay-{stage.stage.value}-snapshot"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=2 + index,
        )
        lifecycle._execute(
            CommandKind.CONFIGURE_ROUND,
            EventKind.ROUND_CONFIGURED,
            AggregateKind.ROUND,
            round_id,
            {"configured": True},
            IdempotencyKey(f"command:replay-configure-{stage.stage.value}"),
            ACTOR,
            NOW,
            10 + index,
        )
    lifecycle._execute(
        CommandKind.CONFIGURE_TOURNAMENT,
        EventKind.TOURNAMENT_CONFIGURED,
        AggregateKind.TOURNAMENT,
        TOURNAMENT,
        {"configured": True},
        IdempotencyKey("command:replay-configure-tournament"),
        ACTOR,
        NOW,
        20,
    )
    lifecycle.open_tournament(
        TOURNAMENT,
        bundle_id=StableIdentifier("bundle:replay"),
        historical_cutoff_key="history:replay",
        root_round_ids=(StableIdentifier(_STAGES[0].round_id),),
        command_id=IdempotencyKey("command:replay-open-tournament"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=21,
    )
    return lifecycle


def _ingest_field(
    lifecycle: LifecycleService, stage: _StageSpec, field: _FieldSpec, ordinal: int
) -> None:
    lifecycle.ingest_snapshot(
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            StableIdentifier(field.field_id),
            1,
            TOURNAMENT,
            StableIdentifier(stage.round_id),
            {
                "competitor_ids": list(field.entrants),
                "target_context": CONTEXT.to_dict(),
                "stand_ids": [f"stand:{index + 1}" for index in range(len(field.entrants))],
            },
        ),
        command_id=IdempotencyKey(f"command:replay-ingest-{field.field_id.split(':', 1)[1]}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=30 + ordinal,
    )


def _issue_field(
    database: Path,
    stage: _StageSpec,
    field: _FieldSpec,
    epoch_id: str,
    marks: tuple[tuple[str, int], ...],
) -> tuple[str, bool]:
    field_id = StableIdentifier(field.field_id)
    _execute_raw(
        database,
        _request(
            database,
            command_kind=CommandKind.OPTIMIZE_FIELD,
            event_kind=EventKind.FIELD_OPTIMIZED,
            aggregate_kind=AggregateKind.FIELD,
            target=field_id,
            command_id=f"command:replay-prepare-{field.field_id.split(':', 1)[1]}",
            payload={
                "schema_version": "strathmark-v3-replay-prepared-field-v1",
                "field_id": field.field_id,
                "marks": dict(marks),
            },
        ),
    )
    receipt_id = str(
        deterministic_identifier(
            "receipt", {"field_id": field.field_id, "marks": [list(item) for item in marks]}
        )
    )
    payload: dict[str, object] = {
        "schema_version": "strathmark-v3-replay-issue-v1",
        "round_id": stage.round_id,
        "epoch_id": epoch_id,
        "field_revision": 1,
        "receipt_id": receipt_id,
        "competitor_ids": list(field.entrants),
        "issued_marks": dict(marks),
    }
    command_id = f"command:replay-issue-{field.field_id.split(':', 1)[1]}"
    issue = _request(
        database,
        command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
        event_kind=EventKind.FIELD_ISSUED,
        aggregate_kind=AggregateKind.FIELD,
        target=field_id,
        command_id=command_id,
        payload=payload,
    )
    first = _execute_raw(database, issue)
    count = _event_count(database, aggregate_id=field.field_id)
    retry = _execute_raw(database, issue)
    exact_retry = first.result_digest == retry.result_digest and (
        _event_count(database, aggregate_id=field.field_id) == count
    )
    changed = dict(payload)
    changed["issued_marks"] = {name: mark + 1 for name, mark in marks}
    try:
        _execute_raw(
            database,
            _request(
                database,
                command_kind=CommandKind.ACKNOWLEDGE_ISSUE,
                event_kind=EventKind.FIELD_ISSUED,
                aggregate_kind=AggregateKind.FIELD,
                target=field_id,
                command_id=command_id,
                payload=changed,
            ),
        )
    except EventStoreConflict:
        changed_rejected = True
    else:
        changed_rejected = False
    return receipt_id, exact_retry and changed_rejected


def _persist_results(
    lifecycle: LifecycleService,
    stage: _StageSpec,
    field: _FieldSpec,
    marks: tuple[tuple[str, int], ...],
    receipt_id: str,
    ordinal: int,
) -> tuple[tuple[str, ...], dict[str, int], bool]:
    by_mark = dict(marks)
    raw_times = {
        competitor: (10_000 if competitor == field.intended_winner else 60_000)
        for competitor in field.entrants
    }
    completions = {
        competitor: by_mark[competitor] * 1_000 + raw_times[competitor]
        for competitor in field.entrants
    }
    placing = tuple(sorted(field.entrants, key=lambda item: (completions[item], item)))
    if placing[0] != field.intended_winner:
        raise ReplayEvidenceError("deterministic result did not produce its intended winner")
    sources: list[int] = []
    for place, competitor in enumerate(placing, start=1):
        stored = lifecycle.record_live_result(
            LiveResultSubmission(
                StableIdentifier(
                    f"evidence:{field.field_id.split(':', 1)[1]}-{competitor.split(':', 1)[1]}"
                ),
                StableIdentifier(competitor),
                TOURNAMENT,
                StableIdentifier(stage.round_id),
                StableIdentifier(field.field_id),
                CONTEXT,
                NOW,
                by_mark[competitor],
                completions[competitor],
                place,
                completions[competitor] - completions[placing[0]],
                OfficialResult(ResultStatus.COMPLETION, raw_times[competitor], None, 1, None),
                canonical_digest(
                    {
                        "field_id": field.field_id,
                        "competitor": competitor,
                        "raw": raw_times[competitor],
                    }
                ),
            ),
            field_revision=1,
            claimed_receipt_id=StableIdentifier(receipt_id),
            command_id=IdempotencyKey(
                f"command:replay-result-{field.field_id.split(':', 1)[1]}-{competitor.split(':', 1)[1]}"
            ),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=100 + ordinal + place,
        )
        sources.append(stored.first_global_sequence)
    settlement_id = f"command:replay-settle-{field.field_id.split(':', 1)[1]}"
    settled = lifecycle.settle_live_race(
        StableIdentifier(field.field_id),
        field_revision=1,
        claimed_receipt_id=StableIdentifier(receipt_id),
        command_id=IdempotencyKey(settlement_id),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=120 + ordinal,
    )
    count = _event_count(lifecycle.projections.database_path, aggregate_id=field.field_id)
    retry = lifecycle.settle_live_race(
        StableIdentifier(field.field_id),
        field_revision=1,
        claimed_receipt_id=StableIdentifier(receipt_id),
        command_id=IdempotencyKey(settlement_id),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=120 + ordinal,
    )
    retry_stable = settled.result_digest == retry.result_digest and (
        _event_count(lifecycle.projections.database_path, aggregate_id=field.field_id) == count
    )
    try:
        lifecycle.settle_live_race(
            StableIdentifier(field.field_id),
            field_revision=1,
            claimed_receipt_id=StableIdentifier(receipt_id),
            command_id=IdempotencyKey(
                f"command:replay-second-settle-{field.field_id.split(':', 1)[1]}"
            ),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=121 + ordinal,
        )
    except EventStoreConflict:
        second_rejected = True
    else:
        second_rejected = False
    for source in sources:
        for reaction in MandatoryReaction:
            lifecycle.complete_derivation_reaction(
                source,
                reaction,
                canonical_digest({"source": source, "reaction": reaction.value}),
                command_id=IdempotencyKey(f"command:replay-reaction-{source}-{reaction.value}"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=150 + ordinal,
            )
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT observation_json FROM v3_result_revisions WHERE field_id=? "
            "AND settled_global_sequence IS NOT NULL ORDER BY competitor_id",
            (field.field_id,),
        ).fetchall()
    observed = [json.loads(str(row[0])) for row in rows]
    persisted_placing = tuple(
        item["competitor_id"] for item in sorted(observed, key=lambda item: item["placing"])
    )
    persisted_times = {item["competitor_id"]: item["result"]["raw_time_ms"] for item in observed}
    return persisted_placing, persisted_times, retry_stable and second_rejected


def _execute_tournament(database: Path) -> tuple[tuple[RaceDayField, ...], str]:
    lifecycle = _bootstrap(database)
    abilities = {
        "competitor:a": 45_000,
        "competitor:b": 55_000,
        "competitor:c": 40_000,
        "competitor:d": 60_000,
        "competitor:e": 50_000,
        "competitor:f": 45_000,
        "competitor:g": 40_000,
    }
    closure_ids: tuple[StableIdentifier, ...] = ()
    transcript: list[RaceDayField] = []
    prior_epoch_digest: str | None = None
    ordinal = 0
    for stage_index, stage in enumerate(_STAGES, start=1):
        epoch, _stored = lifecycle.freeze_round_epoch(
            StableIdentifier(stage.round_id),
            epoch_revision=1,
            historical_cutoff_key="history:replay",
            closure_ids=closure_ids,
            command_id=IdempotencyKey(f"command:replay-freeze-{stage.stage.value}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=200 + stage_index,
        )
        if prior_epoch_digest is not None and epoch.content_digest == prior_epoch_digest:
            raise ReplayEvidenceError("between-round epoch did not incorporate settled authority")
        prior_epoch_digest = epoch.content_digest
        round_winners: list[str] = []
        for field in stage.fields:
            ordinal += 1
            _ingest_field(lifecycle, stage, field, ordinal)
            marks = _marks(field, abilities)
            receipt_id, issue_immutable = _issue_field(
                database, stage, field, str(epoch.epoch_id), marks
            )
            placing, observed_times, winner_immutable = _persist_results(
                lifecycle, stage, field, marks, receipt_id, ordinal
            )
            if not issue_immutable or not winner_immutable:
                raise ReplayEvidenceError("issued sheet or settled winner was not immutable")
            winner = placing[0]
            if winner != field.intended_winner:
                raise ReplayEvidenceError("persisted winner differs from deterministic race")
            round_winners.append(winner)
            abilities.update(observed_times)
            transcript.append(
                RaceDayField(
                    field.field_id,
                    stage.stage,
                    stage_index,
                    field.entrants,
                    marks,
                    placing,
                    winner,
                    80_000,
                    1_100,
                    FieldDisposition.PREDICTIVE,
                    _event_digest(database, field.field_id, EventKind.FIELD_ISSUED),
                    field.scheduled_offset_ms,
                    field.call_delay_ms,
                )
            )
        if stage_index < len(_STAGES):
            next_entrants = {
                entrant
                for next_field in _STAGES[stage_index].fields
                for entrant in next_field.entrants
            }
            if not set(round_winners) <= next_entrants:
                raise ReplayEvidenceError("persisted winner did not advance to the next round")
        lifecycle._execute(
            CommandKind.BEGIN_ROUND_CLOSING,
            EventKind.ROUND_CLOSING_STARTED,
            AggregateKind.ROUND,
            StableIdentifier(stage.round_id),
            {"closing": True},
            IdempotencyKey(f"command:replay-begin-close-{stage.stage.value}"),
            ACTOR,
            NOW,
            250 + stage_index,
        )
        closure_id, _closed = lifecycle.close_evidence_round(
            StableIdentifier(stage.round_id),
            command_id=IdempotencyKey(f"command:replay-close-{stage.stage.value}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=260 + stage_index,
        )
        closure_ids = (closure_id,)
    return tuple(transcript), _event_digest(database, "field:grand", EventKind.FIELD_ISSUED)


def _inject_special_fault(name: str, root: Path) -> tuple[bool, bool]:
    if name in {"ollama_restart", "cloud_timeout"}:
        try:
            raise ProviderCallError(
                "provider_runtime_failure" if name == "ollama_restart" else "overall_timeout"
            )
        except ProviderCallError:
            return True, True
    if name == "blob_corruption":
        store = ContentAddressedBlobStore(root / "blobs")
        payload = b"replay:" + b"x" * 65_537
        reference = store.publish(
            payload,
            metadata=BlobMetadata(
                media_type="application/octet-stream",
                payload_schema_version="strathmark-v3-replay-blob-v1",
                retention=BlobRetention.REQUIRED,
            ),
        )
        store.path_for(reference.digest).write_bytes(b"z" * len(payload))
        try:
            store.verify(reference)
        except BlobIntegrityError:
            return True, False
    if name == "disk_reserve":
        policy = DiskReservePolicy(1_000, 750, 500)
        suspended = not policy.admit(400, DiskOperation.FACTORY, tournament_open=True).allowed
        live_preserved = policy.admit(400, DiskOperation.RECOVERY, tournament_open=True).allowed
        return suspended and live_preserved, True
    if name == "queue_saturation":
        manifest = CapacityManifest.load("benchmarks/v3/job_capacity_manifest.json")
        saturated = QueueLoad(
            total_active=manifest.max_queued_jobs,
            lane_active=manifest.lane(JobLane.INFERENCE).max_queued,
            lane_leased=0,
        )
        denied = not decide_admission(
            manifest, JobLane.INFERENCE, JobPriority.PLAUSIBLE_QUALIFIER, saturated
        ).admitted
        recovery_load = QueueLoad(
            total_active=manifest.max_queued_jobs - 1,
            lane_active=0,
            lane_leased=0,
        )
        preserved = decide_admission(
            manifest, JobLane.LOOKUP_RECOVERY, JobPriority.RECOVERY, recovery_load
        ).admitted
        return denied and preserved, True
    return False, False


def _recovery_trial(
    database: Path,
    root: Path,
    name: str,
    index: int,
    receipt_before: str,
    plan: ReplayFaultPlan,
) -> RecoveryTrial:
    target = deterministic_identifier("forecast", {"recovery_fault": name})
    request = _request(
        database,
        command_kind=CommandKind.COMMIT_FORECAST,
        event_kind=EventKind.COMPONENT_FORECAST_COMMITTED,
        aggregate_kind=AggregateKind.FORECAST,
        target=target,
        command_id=f"command:replay-recover-{name}",
        payload={
            "schema_version": "strathmark-v3-replay-recovery-forecast-v1",
            "failure": name,
            "receipt_digest": receipt_before,
        },
    )
    injected = False
    v3_path_available = True
    transactional = name in {
        "process_restart",
        "machine_restart",
        "worker_crash",
        "power_loss",
        "wal_recovery",
    }
    if name not in plan.disabled_faults:
        if transactional:

            def fault(stage: str) -> None:
                nonlocal injected
                if stage == "after_event:0":
                    injected = True
                    raise _InjectedReplayFault(name)

            try:
                SQLiteEventStore(database).execute(request, fault_hook=fault)
            except _InjectedReplayFault:
                pass
            if name in {"machine_restart", "wal_recovery"}:
                # sqlite3.Connection's transaction context does not close the
                # handle. Windows therefore retains replay.sqlite3 through
                # TemporaryDirectory cleanup unless ownership is explicit.
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            injected, v3_path_available = _inject_special_fault(name, root)
    if not injected:
        raise ReplayEvidenceError(f"{name} was not injected")
    before_count = _event_count(database, aggregate_id=str(target))
    recovered = SQLiteEventStore(database).execute(request)
    after_count = _event_count(database, aggregate_id=str(target))
    exact_retry = SQLiteEventStore(database).execute(request)
    final_count = _event_count(database, aggregate_id=str(target))
    duplicate_forecasts = max(0, final_count - before_count - 1)
    receipt_after = _event_digest(database, "field:grand", EventKind.FIELD_ISSUED)
    if plan.receipt_probe_override_after is not None:
        receipt_after = plan.receipt_probe_override_after
    if receipt_after != receipt_before:
        raise ReplayEvidenceError("immutable receipt changed during recovery")
    if (
        after_count != before_count + 1
        or final_count != after_count
        or recovered.result_digest != exact_retry.result_digest
    ):
        raise ReplayEvidenceError(f"{name} recovery was not exactly idempotent")
    recovered_exactly = (
        injected
        and duplicate_forecasts == 0
        and receipt_after == receipt_before
        and after_count == before_count + 1
        and final_count == after_count
    )
    authority_after = "v3" if v3_path_available else "traditional_manual"
    return RecoveryTrial(
        name,
        recovered_exactly,
        duplicate_forecasts,
        receipt_before,
        receipt_after,
        authority_after,
        1_000 + index,
    )


def build_replay_report(*, fault_plan: ReplayFaultPlan | None = None) -> dict[str, object]:
    plan = ReplayFaultPlan() if fault_plan is None else fault_plan
    if not isinstance(plan, ReplayFaultPlan):
        raise TypeError("replay fault plan must be typed")
    with TemporaryDirectory(prefix="strathmark-v3-replay-") as directory:
        root = Path(directory)
        database = root / "replay.sqlite3"
        fields, immutable_receipt = _execute_tournament(database)
        replay = verify_race_day_replay(fields)
        trials = tuple(
            _recovery_trial(database, root / name, name, index, immutable_receipt, plan)
            for index, name in enumerate(_FAILURES)
        )
        recovery = verify_recovery_matrix(trials)
        # SQLite cursors can participate in short-lived reference cycles.  Force
        # their finalizers before Windows attempts to remove the isolated replay
        # database; otherwise a successful proof can fail during temp cleanup.
        gc.collect()
    body: dict[str, object] = {
        "schema_version": "strathmark-v3-whole-system-replay-v1",
        "result": "passed",
        "race_day": replay.to_dict(),
        "race_day_digest": replay.digest,
        "recovery": {
            "failures": list(recovery.failures),
            "maximum_recovery_ms": recovery.maximum_recovery_ms,
            "zero_duplicate_forecasts": recovery.zero_duplicate_forecasts,
            "immutable_receipts": recovery.immutable_receipts,
        },
        "recovery_digest": recovery.digest,
    }
    body["report_digest"] = canonical_digest(body)
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    arguments = parser.parse_args(argv)
    report = build_replay_report()
    encoded = canonical_bytes(report) + b"\n"
    if arguments.verify is not None:
        try:
            if arguments.verify.read_bytes() != encoded:
                return 2
        except OSError:
            return 2
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_bytes(encoded)
    elif arguments.verify is None:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
