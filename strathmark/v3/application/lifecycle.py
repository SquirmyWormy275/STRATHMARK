"""Causal U5 command handlers over event authority and rebuildable views."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, cast

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import (
    AggregateKind,
    CompetitionEngineSelection,
    EventEnvelope,
    EventKind,
)
from strathmark.v3.contracts.evidence import (
    ResultObservation,
    TargetContext,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.contracts.statuses import PredictionEngine
from strathmark.v3.domain.epochs import (
    EpochMember,
    EvidenceEpoch,
    MandatoryReaction,
    freeze_epoch,
)
from strathmark.v3.domain.evidence import (
    AdmissionReason,
    IssuedFieldFact,
    LiveResultSubmission,
    admit_observation,
    validate_result_revision,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import (
    EventStoreConflict,
    SQLiteEventStore,
    StoredCommandResult,
)
from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore


class SnapshotKind(str, Enum):
    TOURNAMENT = "tournament"
    ROUND = "round"
    FIELD = "field"


class LifecycleReactionPort(Protocol):
    """Post-commit durable reaction; exact command retries replay it safely."""

    def react(self, result: StoredCommandResult) -> None: ...


_SNAPSHOT_VOCABULARY = {
    SnapshotKind.TOURNAMENT: (
        CommandKind.REVISE_TOURNAMENT_SNAPSHOT,
        EventKind.TOURNAMENT_SNAPSHOT_REVISED,
        AggregateKind.TOURNAMENT_INGRESS,
    ),
    SnapshotKind.ROUND: (
        CommandKind.REVISE_ROUND_SNAPSHOT,
        EventKind.ROUND_SNAPSHOT_REVISED,
        AggregateKind.ROUND_INGRESS,
    ),
    SnapshotKind.FIELD: (
        CommandKind.REVISE_FIELD_ROSTER,
        EventKind.FIELD_ROSTER_REVISED,
        AggregateKind.FIELD_INGRESS,
    ),
}


@dataclass(frozen=True, slots=True)
class UpstreamSnapshot:
    kind: SnapshotKind
    entity_id: StableIdentifier
    upstream_revision: int
    tournament_id: StableIdentifier
    round_id: StableIdentifier | None
    content: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SnapshotKind):
            raise ContractError("snapshot kind must use the closed vocabulary")
        require_identifier(self.entity_id, expected_namespace=self.kind.value)
        require_identifier(self.tournament_id, expected_namespace="tournament")
        if self.kind is SnapshotKind.TOURNAMENT:
            if self.round_id is not None or self.entity_id != self.tournament_id:
                raise ContractError("tournament snapshot identity or parent is invalid")
        else:
            if self.round_id is None:
                raise ContractError("round and field snapshots require a round identity")
            require_identifier(self.round_id, expected_namespace="round")
            if self.kind is SnapshotKind.ROUND and self.entity_id != self.round_id:
                raise ContractError("round snapshot identity must match its round")
        if (
            isinstance(self.upstream_revision, bool)
            or not isinstance(self.upstream_revision, int)
            or self.upstream_revision <= 0
        ):
            raise ContractError("upstream revision must be positive")
        if not isinstance(self.content, Mapping):
            raise ContractError("snapshot content must be a mapping")
        canonical = InlinePayload.from_value(self.content).to_value()
        allowed = {
            SnapshotKind.TOURNAMENT: {"bundle_id", "historical_cutoff_key"},
            SnapshotKind.ROUND: {
                "round_ordinal",
                "predecessor_round_ids",
                "successor_round_ids",
            },
            SnapshotKind.FIELD: {
                "competitor_ids",
                "target_context",
                "stand_ids",
                "capacity_authority_digest",
                "max_field_entrants",
                "call_order",
                "scheduled_at",
                "deadline_at",
            },
        }[self.kind]
        legacy_field = {"competitor_ids", "target_context", "stand_ids"}
        if set(canonical) != allowed and not (
            self.kind is SnapshotKind.FIELD and set(canonical) == legacy_field
        ):
            raise ContractError("snapshot contains unknown, narrative, or PII fields")
        if self.kind is SnapshotKind.TOURNAMENT:
            require_identifier(canonical["bundle_id"], expected_namespace="bundle")
            require_identifier(canonical["historical_cutoff_key"], expected_namespace="history")
        elif self.kind is SnapshotKind.ROUND:
            ordinal = canonical["round_ordinal"]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
                raise ContractError("round ordinal must be positive")
            for relation in ("predecessor_round_ids", "successor_round_ids"):
                related = canonical[relation]
                relation_ids = tuple(
                    str(require_identifier(item, expected_namespace="round")) for item in related
                )
                if len(relation_ids) != len(set(relation_ids)):
                    raise ContractError("round relations cannot repeat")
        if self.kind is SnapshotKind.FIELD:
            roster = canonical.get("competitor_ids")
            if not roster:
                raise ContractError("field snapshot requires an ordered competitor roster")
            identities = tuple(
                str(require_identifier(item, expected_namespace="competitor")) for item in roster
            )
            if len(identities) != len(set(identities)):
                raise ContractError("field snapshot roster cannot contain duplicates")
            TargetContext.from_dict(canonical["target_context"])
            stands = canonical["stand_ids"]
            if len(stands) != len(roster):
                raise ContractError("field snapshot requires one stand identity per entrant")
            stand_ids = tuple(
                str(require_identifier(item, expected_namespace="stand")) for item in stands
            )
            if len(stand_ids) != len(set(stand_ids)):
                raise ContractError("field stands cannot repeat")
            if "call_order" in canonical:
                capacity_digest = canonical["capacity_authority_digest"]
                if (
                    not isinstance(capacity_digest, str)
                    or len(capacity_digest) != 64
                    or any(character not in "0123456789abcdef" for character in capacity_digest)
                ):
                    raise ContractError("field capacity authority digest is invalid")
                max_field_entrants = canonical["max_field_entrants"]
                if (
                    isinstance(max_field_entrants, bool)
                    or not isinstance(max_field_entrants, int)
                    or max_field_entrants <= 0
                    or len(roster) > max_field_entrants
                ):
                    raise ContractError("field roster exceeds its declared capacity authority")
                call_order = canonical["call_order"]
                if (
                    isinstance(call_order, bool)
                    or not isinstance(call_order, int)
                    or call_order < 0
                ):
                    raise ContractError("field call order must be a nonnegative integer")
                require_utc_milliseconds(canonical["scheduled_at"])
                require_utc_milliseconds(canonical["deadline_at"])
                if canonical["deadline_at"] <= canonical["scheduled_at"]:
                    raise ContractError("field deadline must follow its scheduled instant")
        object.__setattr__(self, "content", _deep_freeze(canonical))

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-upstream-snapshot-v1",
            "entity_kind": self.kind.value,
            "entity_id": str(self.entity_id),
            "upstream_revision": self.upstream_revision,
            "tournament_id": str(self.tournament_id),
            "round_id": None if self.round_id is None else str(self.round_id),
            "snapshot": _deep_thaw(self.content),
            "snapshot_digest": canonical_digest(_deep_thaw(self.content)),
        }


class LifecycleService:
    """Execute typed U5 events and their disposable projection atomically."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        reaction_port: LifecycleReactionPort | None = None,
    ) -> None:
        if reaction_port is not None and not callable(getattr(reaction_port, "react", None)):
            raise ContractError("lifecycle reaction port must be callable")
        self._views = SQLiteProjectionStore(database_path)
        self._views.bootstrap_rolling_reaction_cursor_cutover()
        self._events = SQLiteEventStore(database_path)
        self._reaction_port = reaction_port

    @property
    def projections(self) -> SQLiteProjectionStore:
        return self._views

    def ingest_snapshot(
        self,
        snapshot: UpstreamSnapshot,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        if not isinstance(snapshot, UpstreamSnapshot):
            raise ContractError("ingress requires an UpstreamSnapshot")
        command_kind, event_kind, aggregate_kind = _SNAPSHOT_VOCABULARY[snapshot.kind]
        target = deterministic_identifier(
            aggregate_kind.value, {"entity_id": str(snapshot.entity_id)}
        )
        if snapshot.kind is SnapshotKind.FIELD:
            with open_v3_connection(self._events.database_path, read_only=True) as connection:
                prepared = connection.execute(
                    "SELECT 1 FROM v3_prepared_field_dependencies dependency "
                    "LEFT JOIN v3_round_issue_seals seal ON seal.round_id=dependency.round_id "
                    "WHERE dependency.field_id=? AND dependency.invalidated_by_sequence IS NULL "
                    "AND seal.round_id IS NULL",
                    (str(snapshot.entity_id),),
                ).fetchone()
            if prepared is not None:
                return self._execute_multi(
                    command_kind,
                    target,
                    (
                        EventIntent(aggregate_kind, target, event_kind),
                        EventIntent(
                            AggregateKind.FIELD,
                            snapshot.entity_id,
                            EventKind.FIELD_SUPERSEDED,
                        ),
                    ),
                    snapshot.payload(),
                    command_id,
                    actor_id,
                    occurred_at_utc,
                    monotonic_elapsed_ms,
                )
        return self._execute(
            command_kind,
            event_kind,
            aggregate_kind,
            target,
            snapshot.payload(),
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def open_tournament(
        self,
        tournament_id: StableIdentifier,
        *,
        bundle_id: StableIdentifier,
        historical_cutoff_key: str,
        root_round_ids: tuple[StableIdentifier, ...],
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
        engine_selection: CompetitionEngineSelection | None = None,
    ) -> StoredCommandResult:
        require_identifier(tournament_id, expected_namespace="tournament")
        require_identifier(bundle_id, expected_namespace="bundle")
        require_identifier(historical_cutoff_key, expected_namespace="history")
        if not isinstance(root_round_ids, tuple):
            raise ContractError("root rounds must be immutable")
        roots = tuple(
            str(require_identifier(item, expected_namespace="round")) for item in root_round_ids
        )
        if not roots or len(roots) != len(set(roots)):
            raise ContractError("tournament open requires unique root rounds")
        if engine_selection is not None:
            if not isinstance(engine_selection, CompetitionEngineSelection):
                raise ContractError(
                    "engine selection must use the CompetitionEngineSelection contract"
                )
            if engine_selection.scope_id != tournament_id:
                raise ContractError("engine selection scope identity does not match tournament")
            if engine_selection.selected_by_actor_id != actor_id:
                raise ContractError("engine selection actor does not match tournament opener")
            if engine_selection.selected_at_utc > occurred_at_utc:
                raise ContractError("engine selection cannot occur after tournament open")
            if engine_selection.engine is not PredictionEngine.V3:
                raise ContractError("V2-selected scope cannot enter the V3 lifecycle")
        payload: dict[str, Any] = {
            "schema_version": "strathmark-v3-tournament-open-v1",
            "bundle_id": str(bundle_id),
            "historical_cutoff_key": historical_cutoff_key,
            "root_round_ids": list(roots),
        }
        if engine_selection is not None:
            payload["engine_selection"] = engine_selection.to_dict()
        return self._execute(
            CommandKind.OPEN_TOURNAMENT,
            EventKind.TOURNAMENT_OPENED,
            AggregateKind.TOURNAMENT,
            tournament_id,
            payload,
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def close_tournament(
        self,
        tournament_id: StableIdentifier,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        require_identifier(tournament_id, expected_namespace="tournament")
        return self._execute(
            CommandKind.CLOSE_TOURNAMENT,
            EventKind.TOURNAMENT_CLOSED,
            AggregateKind.TOURNAMENT,
            tournament_id,
            {
                "schema_version": "strathmark-v3-tournament-close-v1",
                "deferred_reactions": ["cancel_jobs", "expire_overlay", "seal_exports"],
            },
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def record_live_result(
        self,
        submission: LiveResultSubmission,
        *,
        field_revision: int,
        claimed_receipt_id: StableIdentifier,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        if not isinstance(submission, LiveResultSubmission):
            raise ContractError("live outcome must be a sequence-free LiveResultSubmission")
        observation = submission.to_observation(1)
        issued = self._issued_field(submission.field_id)
        classified = admit_observation(
            observation,
            issued_field=issued,
            field_revision=field_revision,
            claimed_receipt_id=claimed_receipt_id,
        )
        if classified.reason not in {
            AdmissionReason.ELIGIBLE_COMPLETION,
            AdmissionReason.STATUS_INELIGIBLE,
        }:
            raise ContractError(
                f"live outcome does not match authoritative issue: {classified.reason.value}"
            )
        result_key = deterministic_identifier(
            "result",
            {
                "field_id": str(observation.field_id),
                "field_revision": field_revision,
                "competitor_id": str(observation.competitor_id),
            },
        )
        previous = self._latest_observation(str(result_key))
        validate_result_revision(previous, observation)
        event_kind = EventKind.RESULT_RECORDED if previous is None else EventKind.RESULT_SUPERSEDED
        if previous is None:
            command_kind = CommandKind.RECORD_RESULT
        elif observation.result.status.value == "void":
            command_kind = CommandKind.VOID_RESULT
        else:
            command_kind = CommandKind.CORRECT_RESULT
        payload = {
            "schema_version": "strathmark-v3-live-result-v1",
            "result_key": str(result_key),
            "submission": submission.to_dict(),
            "field_revision": field_revision,
            "claimed_receipt_id": str(claimed_receipt_id),
            "candidate_numeric_eligible": classified.numeric_eligible,
            "admission_reason": classified.reason.value,
        }
        if previous is not None and self._latest_result_is_settled(str(result_key)):
            settlement = self._corrected_settlement_payload(
                observation,
                result_key,
                field_revision=field_revision,
                receipt_id=claimed_receipt_id,
            )
            settlement_target = deterministic_identifier(
                "settlement",
                {key: value for key, value in settlement.items() if key != "schema_version"},
            )
            combined = {
                "schema_version": "strathmark-v3-correction-settlement-v1",
                "result": payload,
                "settlement": settlement,
            }
            invalidated_fields = self._dependent_unissued_fields(str(result_key))
            intents = (
                EventIntent(AggregateKind.RESULT, result_key, EventKind.RESULT_SUPERSEDED),
                EventIntent(
                    AggregateKind.SETTLEMENT,
                    settlement_target,
                    EventKind.LIVE_RACE_SETTLED,
                ),
                *tuple(
                    EventIntent(AggregateKind.FIELD, field_id, EventKind.FIELD_SUPERSEDED)
                    for field_id in invalidated_fields
                ),
            )
            return self._execute_multi(
                CommandKind.SUPERSEDE_AND_SETTLE_RESULT,
                result_key,
                intents,
                combined,
                command_id,
                actor_id,
                occurred_at_utc,
                monotonic_elapsed_ms,
            )
        return self._execute(
            command_kind,
            event_kind,
            AggregateKind.RESULT,
            result_key,
            payload,
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def settle_live_race(
        self,
        field_id: StableIdentifier,
        *,
        field_revision: int,
        claimed_receipt_id: StableIdentifier,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        issued = self._issued_field(field_id)
        if (
            issued is None
            or issued.upstream_revision != field_revision
            or issued.receipt_id != claimed_receipt_id
        ):
            raise ContractError("settlement must match the exact acknowledged receipt")
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT r.result_key, r.revision, r.competitor_id, r.source_global_sequence "
                "FROM v3_result_revisions r WHERE r.field_id=? AND r.field_revision=? "
                "AND r.revision=("
                "SELECT MAX(latest.revision) FROM v3_result_revisions latest "
                "WHERE latest.result_key=r.result_key) ORDER BY r.competitor_id",
                (str(field_id), field_revision),
            ).fetchall()
        competitors = tuple(str(row[2]) for row in rows)
        if len(competitors) != len(set(competitors)) or set(competitors) != {
            str(item) for item in issued.competitor_ids
        }:
            raise ContractError(
                "settlement requires one active outcome revision per issued entrant"
            )
        results = [
            {
                "result_key": str(row[0]),
                "revision": int(row[1]),
                "competitor_id": str(row[2]),
            }
            for row in rows
        ]
        target = deterministic_identifier(
            "settlement",
            {
                "field_id": str(field_id),
                "field_revision": field_revision,
                "receipt_id": str(claimed_receipt_id),
                "results": results,
            },
        )
        return self._execute_multi(
            CommandKind.SETTLE_LIVE_RACE,
            target,
            (
                EventIntent(AggregateKind.SETTLEMENT, target, EventKind.LIVE_RACE_SETTLED),
                EventIntent(AggregateKind.FIELD, field_id, EventKind.FIELD_SETTLED),
            ),
            {
                "schema_version": "strathmark-v3-live-settlement-v1",
                "field_id": str(field_id),
                "field_revision": field_revision,
                "receipt_id": str(claimed_receipt_id),
                "results": results,
            },
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def record_and_settle_live_race(
        self,
        submissions: tuple[LiveResultSubmission, ...],
        *,
        field_id: StableIdentifier,
        field_revision: int,
        claimed_receipt_id: StableIdentifier,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        """Atomically record a complete issued roster and settle its field."""

        if (
            not isinstance(submissions, tuple)
            or not submissions
            or any(not isinstance(item, LiveResultSubmission) for item in submissions)
        ):
            raise ContractError("atomic settlement requires immutable live result submissions")
        issued = self._issued_field(field_id)
        if (
            issued is None
            or issued.upstream_revision != field_revision
            or issued.receipt_id != claimed_receipt_id
        ):
            raise ContractError("settlement must match the exact acknowledged receipt")
        by_competitor = {str(item.competitor_id): item for item in submissions}
        if len(by_competitor) != len(submissions) or set(by_competitor) != {
            str(item) for item in issued.competitor_ids
        }:
            raise ContractError(
                "atomic settlement requires the complete issued roster exactly once"
            )
        result_payloads: list[dict[str, Any]] = []
        result_intents: list[EventIntent] = []
        results: list[dict[str, object]] = []
        for competitor_id in sorted(by_competitor):
            submission = by_competitor[competitor_id]
            if submission.field_id != field_id:
                raise ContractError("atomic settlement submissions must bind one issued field")
            observation = submission.to_observation(1)
            classified = admit_observation(
                observation,
                issued_field=issued,
                field_revision=field_revision,
                claimed_receipt_id=claimed_receipt_id,
            )
            if classified.reason not in {
                AdmissionReason.ELIGIBLE_COMPLETION,
                AdmissionReason.STATUS_INELIGIBLE,
            }:
                raise ContractError(
                    f"live outcome does not match authoritative issue: {classified.reason.value}"
                )
            result_key = deterministic_identifier(
                "result",
                {
                    "field_id": str(field_id),
                    "field_revision": field_revision,
                    "competitor_id": competitor_id,
                },
            )
            previous = self._latest_observation(str(result_key))
            already_settled = previous is not None and self._latest_result_is_settled(
                str(result_key)
            )
            exact_retry = already_settled and previous == submission.to_observation(
                previous.observation_sequence
            )
            if not exact_retry:
                validate_result_revision(previous, observation)
                if already_settled:
                    raise ContractError(
                        "atomic initial settlement cannot revise an already settled result"
                    )
            event_kind = (
                EventKind.RESULT_RECORDED
                if observation.result.revision == 1
                else EventKind.RESULT_SUPERSEDED
            )
            result_payloads.append(
                {
                    "schema_version": "strathmark-v3-live-result-v1",
                    "result_key": str(result_key),
                    "submission": submission.to_dict(),
                    "field_revision": field_revision,
                    "claimed_receipt_id": str(claimed_receipt_id),
                    "candidate_numeric_eligible": classified.numeric_eligible,
                    "admission_reason": classified.reason.value,
                }
            )
            result_intents.append(EventIntent(AggregateKind.RESULT, result_key, event_kind))
            results.append(
                {
                    "result_key": str(result_key),
                    "revision": observation.result.revision,
                    "competitor_id": competitor_id,
                }
            )
        settlement_payload = {
            "schema_version": "strathmark-v3-live-settlement-v1",
            "field_id": str(field_id),
            "field_revision": field_revision,
            "receipt_id": str(claimed_receipt_id),
            "results": results,
        }
        target = deterministic_identifier(
            "settlement",
            {
                "field_id": str(field_id),
                "field_revision": field_revision,
                "receipt_id": str(claimed_receipt_id),
                "results": results,
            },
        )
        return self._execute_multi(
            CommandKind.SETTLE_LIVE_RACE,
            target,
            (
                *result_intents,
                EventIntent(AggregateKind.SETTLEMENT, target, EventKind.LIVE_RACE_SETTLED),
                EventIntent(AggregateKind.FIELD, field_id, EventKind.FIELD_SETTLED),
            ),
            {
                "schema_version": "strathmark-v3-record-and-settle-live-race-v1",
                "field_id": str(field_id),
                "field_revision": field_revision,
                "receipt_id": str(claimed_receipt_id),
                "result_submissions": result_payloads,
                "settlement": settlement_payload,
            },
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def complete_derivation_reaction(
        self,
        source_global_sequence: int,
        reaction: MandatoryReaction,
        output_digest: str,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> tuple[StoredCommandResult, StoredCommandResult | None]:
        if not isinstance(reaction, MandatoryReaction):
            raise ContractError("reaction must use the mandatory vocabulary")
        if not isinstance(output_digest, str) or len(output_digest) != 64:
            raise ContractError("derivation output requires a SHA-256 digest")
        target = deterministic_identifier(
            "reaction", {"source": source_global_sequence, "reaction": reaction.value}
        )
        reaction_result = self._execute(
            CommandKind.COMPLETE_DERIVATION_REACTION,
            EventKind.DERIVATION_REACTION_COMPLETED,
            AggregateKind.REACTION,
            target,
            {
                "schema_version": "strathmark-v3-derivation-reaction-v1",
                "source_global_sequence": source_global_sequence,
                "reaction": reaction.value,
                "output_digest": output_digest,
            },
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )
        if self._views.pending_reactions(source_global_sequence):
            return reaction_result, None
        completion_digest = self._reaction_completion_digest(source_global_sequence)
        sequence_target = deterministic_identifier("derivation", {"source": source_global_sequence})
        sequence_key = IdempotencyKey(
            f"command:{canonical_digest({'source': source_global_sequence, 'complete': completion_digest})}"
        )
        sequence_result = self._execute(
            CommandKind.COMPLETE_DERIVATION_SEQUENCE,
            EventKind.DERIVATION_SEQUENCE_COMPLETED,
            AggregateKind.DERIVATION,
            sequence_target,
            {
                "schema_version": "strathmark-v3-derivation-sequence-v1",
                "source_global_sequence": source_global_sequence,
                "completion_digest": completion_digest,
            },
            sequence_key,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )
        return reaction_result, sequence_result

    def close_evidence_round(
        self,
        round_id: StableIdentifier,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> tuple[StableIdentifier, StoredCommandResult]:
        require_identifier(round_id, expected_namespace="round")
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            ingress = connection.execute(
                "SELECT tournament_id, snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='round' AND entity_id=? ORDER BY upstream_revision DESC LIMIT 1",
                (str(round_id),),
            ).fetchone()
            if ingress is None:
                raise ContractError("round closure requires an authoritative round snapshot")
            before_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(global_sequence), 0) + 1 FROM v3_events"
                ).fetchone()[0]
            )
            results = SQLiteProjectionStore._active_set_for_round(
                connection,
                str(ingress[0]),
                str(round_id),
                before_sequence=before_sequence,
            )
        tournament_id = str(ingress[0])
        round_snapshot = json.loads(str(ingress[1]))
        successors = round_snapshot["successor_round_ids"]
        closure_id = deterministic_identifier(
            "round_closure",
            {
                "tournament_id": tournament_id,
                "source_round_id": str(round_id),
                "target_round_ids": successors,
                "results": results,
            },
        )
        stored = self._execute(
            CommandKind.CLOSE_ROUND,
            EventKind.ROUND_CLOSED,
            AggregateKind.ROUND,
            round_id,
            {
                "schema_version": "strathmark-v3-round-closure-v1",
                "closure_id": str(closure_id),
                "tournament_id": tournament_id,
                "source_round_id": str(round_id),
                "target_round_ids": successors,
                "results": results,
                "result_set_digest": canonical_digest(results),
            },
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )
        return closure_id, stored

    def freeze_round_epoch(
        self,
        round_id: StableIdentifier,
        *,
        epoch_revision: int,
        historical_cutoff_key: str,
        closure_ids: tuple[StableIdentifier, ...],
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> tuple[EvidenceEpoch, StoredCommandResult]:
        if not isinstance(closure_ids, tuple):
            raise ContractError("epoch closure identities must be immutable")
        for closure_id in closure_ids:
            require_identifier(closure_id, expected_namespace="round_closure")
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            ingress = connection.execute(
                "SELECT tournament_id, snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='round' AND entity_id=? ORDER BY upstream_revision DESC LIMIT 1",
                (str(round_id),),
            ).fetchone()
            if ingress is None:
                raise ContractError("epoch target requires an authoritative round snapshot")
            tournament_id = str(ingress[0])
            target_snapshot = json.loads(str(ingress[1]))
            predecessors = set(target_snapshot["predecessor_round_ids"])
            issued = connection.execute(
                "SELECT 1 FROM v3_round_issue_seals WHERE round_id=?",
                (str(round_id),),
            ).fetchone()
            if issued is not None:
                raise ContractError("an issued round cannot refreeze its evidence epoch")
            closures = []
            for closure_id in closure_ids:
                closure = connection.execute(
                    "SELECT source_round_id, target_round_ids_json, closure_global_sequence, "
                    "result_set_json FROM v3_round_closures WHERE closure_id=?",
                    (str(closure_id),),
                ).fetchone()
                if closure is None or str(round_id) not in json.loads(str(closure[1])):
                    raise ContractError("epoch closure does not feed the target round")
                closures.append(closure)
            if {str(item[0]) for item in closures} != predecessors:
                raise ContractError("epoch freeze requires every predecessor closure exactly once")
            if not predecessors:
                opened = connection.execute(
                    "SELECT global_sequence FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                    "ORDER BY global_sequence DESC LIMIT 1",
                    (str(ingress[0]), EventKind.TOURNAMENT_OPENED.value),
                ).fetchone()
                if opened is None:
                    raise ContractError("root round requires the tournament-open boundary")
                closed_through_sequence = int(opened[0])
                sealed_results = []
            else:
                closed_through_sequence = max(int(item[2]) for item in closures)
                selected: dict[str, dict[str, object]] = {}
                for closure in closures:
                    for item in json.loads(str(closure[3])):
                        prior = selected.get(item["result_key"])
                        if prior is None or (item["revision"], item["source_sequence"]) > (
                            prior["revision"],
                            prior["source_sequence"],
                        ):
                            selected[item["result_key"]] = item
                before_sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(global_sequence), 0) + 1 FROM v3_events"
                    ).fetchone()[0]
                )
                sealed_results = SQLiteProjectionStore._refresh_active_members(
                    connection,
                    str(ingress[0]),
                    [selected[key] for key in sorted(selected)],
                    before_sequence=before_sequence,
                )
                if sealed_results:
                    closed_through_sequence = max(
                        closed_through_sequence,
                        max(int(item["source_sequence"]) for item in sealed_results),
                    )
        members = tuple(
            EpochMember(
                item["result_key"],
                item["revision"],
                item["source_sequence"],
                item["numeric_eligible"],
            )
            for item in sealed_results
        )
        epoch = freeze_epoch(
            round_id=round_id,
            epoch_revision=epoch_revision,
            historical_cutoff_key=historical_cutoff_key,
            closed_through_sequence=closed_through_sequence,
            members=members,
            barrier=self._views.reaction_barrier_for_tournament(
                tournament_id, closed_through_sequence
            ),
        )
        result = self._execute_multi(
            CommandKind.FREEZE_EVIDENCE_EPOCH,
            epoch.epoch_id,
            (
                EventIntent(AggregateKind.EPOCH, epoch.epoch_id, EventKind.ROUND_EPOCH_FROZEN),
                EventIntent(AggregateKind.ROUND, round_id, EventKind.ROUND_FROZEN),
            ),
            {
                "schema_version": "strathmark-v3-epoch-event-v1",
                "epoch_id": str(epoch.epoch_id),
                "content_digest": epoch.content_digest,
                "epoch": epoch.content_value(),
                "closure_ids": [str(item) for item in closure_ids],
            },
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )
        return epoch, result

    def _execute(
        self,
        command_kind: CommandKind,
        event_kind: EventKind,
        aggregate_kind: AggregateKind,
        target: StableIdentifier,
        payload: Mapping[str, Any],
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        require_utc_milliseconds(occurred_at_utc)
        require_identifier(actor_id, expected_namespace="actor")
        inline_payload = InlinePayload.from_value(payload)
        retry = self._events.lookup_exact_retry(
            principal_id=str(actor_id),
            idempotency_key=str(command_id),
            command_kind=command_kind,
            target_aggregate=str(target),
            payload_digest=inline_payload.digest,
        )
        if retry is not None:
            return self._after_commit(retry)
        head = self._events.aggregate_head(str(target))
        version = 0 if head is None else head[0]
        command = CommandEnvelope(
            command_kind,
            command_id,
            target,
            ((str(target), version),),
            actor_id,
            inline_payload,
        )
        request = CommandRequest(
            actor_id,
            command,
            (EventIntent(aggregate_kind, target, event_kind),),
            "strathmark-v3-lifecycle-command-result-v1",
            {"accepted": True, "target": str(target), "payload_digest": command.payload_digest},
            occurred_at_utc,
            monotonic_elapsed_ms,
        )
        try:
            return self._after_commit(
                self._events.execute(request, projection_hook=self._views.apply_events)
            )
        except EventStoreConflict:
            raced_retry = self._events.lookup_exact_retry(
                principal_id=str(actor_id),
                idempotency_key=str(command_id),
                command_kind=command_kind,
                target_aggregate=str(target),
                payload_digest=inline_payload.digest,
            )
            if raced_retry is not None:
                return self._after_commit(raced_retry)
            raise

    def _execute_multi(
        self,
        command_kind: CommandKind,
        target: StableIdentifier,
        intents: tuple[EventIntent, ...],
        payload: Mapping[str, Any],
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        inline = InlinePayload.from_value(payload)
        retry = self._events.lookup_exact_retry(
            principal_id=str(actor_id),
            idempotency_key=str(command_id),
            command_kind=command_kind,
            target_aggregate=str(target),
            payload_digest=inline.digest,
        )
        if retry is not None:
            return self._after_commit(retry)
        expected = []
        for intent in intents:
            head = self._events.aggregate_head(str(intent.aggregate_id))
            expected.append((str(intent.aggregate_id), 0 if head is None else head[0]))
        command = CommandEnvelope(
            command_kind,
            command_id,
            target,
            tuple(sorted(expected)),
            actor_id,
            inline,
        )
        request = CommandRequest(
            actor_id,
            command,
            intents,
            "strathmark-v3-lifecycle-command-result-v1",
            {"accepted": True, "target": str(target), "payload_digest": inline.digest},
            occurred_at_utc,
            monotonic_elapsed_ms,
        )
        try:
            return self._after_commit(
                self._events.execute(request, projection_hook=self._views.apply_events)
            )
        except EventStoreConflict:
            raced = self._events.lookup_exact_retry(
                principal_id=str(actor_id),
                idempotency_key=str(command_id),
                command_kind=command_kind,
                target_aggregate=str(target),
                payload_digest=inline.digest,
            )
            if raced is not None:
                return self._after_commit(raced)
            raise

    def _after_commit(self, result: StoredCommandResult) -> StoredCommandResult:
        if self._reaction_port is not None:
            self._reaction_port.react(result)
        return result

    def _issued_field(self, field_id: StableIdentifier) -> IssuedFieldFact | None:
        require_identifier(field_id, expected_namespace="field")
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT global_sequence, envelope_json FROM v3_events "
                "WHERE aggregate_id=? AND event_kind=? "
                "ORDER BY global_sequence DESC LIMIT 1",
                (str(field_id), EventKind.FIELD_ISSUED.value),
            ).fetchone()
            if row is None:
                return None
            issue_sequence = int(row[0])
            event = EventEnvelope.from_dict(json.loads(str(row[1])))
            payload = cast(InlinePayload, event.command.payload)
            value = payload.to_value()
            if value.get("schema_version") == "strathmark-v3-batch-issue-authority-v1":
                fields = value.get("fields")
                if not isinstance(fields, list):
                    raise ContractError("batch issue fields are invalid")
                matches = [
                    item
                    for item in fields
                    if isinstance(item, dict) and item.get("field_id") == str(field_id)
                ]
                if len(matches) != 1:
                    raise ContractError("batch issue does not bind the requested field")
                value = matches[0]
            ingress = connection.execute(
                "SELECT tournament_id, round_id, snapshot_json "
                "FROM v3_ingress_snapshots WHERE entity_kind='field' AND entity_id=? "
                "AND source_global_sequence<? ORDER BY upstream_revision DESC LIMIT 1",
                (str(field_id), issue_sequence),
            ).fetchone()
            snapshot = json.loads(str(ingress[2]))
            roster = tuple(
                require_identifier(item, expected_namespace="competitor")
                for item in value["competitor_ids"]
            )
            marks = cast(dict[str, int], value["issued_marks"])
            return IssuedFieldFact(
                field_id,
                value["field_revision"],
                roster,
                require_identifier(value["receipt_id"], expected_namespace="receipt"),
                require_identifier(ingress[0], expected_namespace="tournament"),
                require_identifier(ingress[1], expected_namespace="round"),
                TargetContext.from_dict(snapshot["target_context"]),
                tuple((competitor_id, marks[str(competitor_id)]) for competitor_id in roster),
            )

    def _latest_observation(self, result_key: str) -> ResultObservation | None:
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT observation_json FROM v3_result_revisions WHERE result_key=? "
                "ORDER BY revision DESC LIMIT 1",
                (result_key,),
            ).fetchone()
        return None if row is None else ResultObservation.from_dict(json.loads(str(row[0])))

    def _latest_result_is_settled(self, result_key: str) -> bool:
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT settled_global_sequence FROM v3_result_revisions WHERE result_key=? "
                "ORDER BY revision DESC LIMIT 1",
                (result_key,),
            ).fetchone()
        return row is not None and row[0] is not None

    def _corrected_settlement_payload(
        self,
        observation: ResultObservation,
        result_key: StableIdentifier,
        *,
        field_revision: int,
        receipt_id: StableIdentifier,
    ) -> dict[str, object]:
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT result_key, revision, competitor_id FROM v3_result_revisions r "
                "WHERE field_id=? AND field_revision=? AND revision=(SELECT MAX(revision) "
                "FROM v3_result_revisions latest WHERE latest.result_key=r.result_key) "
                "ORDER BY competitor_id",
                (str(observation.field_id), field_revision),
            ).fetchall()
        results = []
        for row in rows:
            if str(row[0]) == str(result_key):
                results.append(
                    {
                        "result_key": str(result_key),
                        "revision": observation.result.revision,
                        "competitor_id": str(observation.competitor_id),
                    }
                )
            else:
                results.append(
                    {
                        "result_key": str(row[0]),
                        "revision": int(row[1]),
                        "competitor_id": str(row[2]),
                    }
                )
        return {
            "schema_version": "strathmark-v3-live-settlement-v1",
            "field_id": str(observation.field_id),
            "field_revision": field_revision,
            "receipt_id": str(receipt_id),
            "results": results,
        }

    def _reaction_completion_digest(self, source: int) -> str:
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT reaction_type, output_digest FROM v3_derivation_reactions "
                "WHERE source_global_sequence=? AND state='completed' ORDER BY reaction_type",
                (source,),
            ).fetchall()
        return canonical_digest([[str(row[0]), str(row[1])] for row in rows])

    def _dependent_unissued_fields(self, result_key: str) -> tuple[StableIdentifier, ...]:
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT dependencies.field_id FROM v3_prepared_field_dependencies "
                "dependencies JOIN v3_evidence_epoch_members members "
                "ON members.epoch_id=dependencies.epoch_id "
                "LEFT JOIN v3_round_issue_seals seals ON seals.round_id=dependencies.round_id "
                "WHERE members.result_key=? AND dependencies.invalidated_by_sequence IS NULL "
                "AND seals.round_id IS NULL ORDER BY dependencies.field_id",
                (result_key,),
            ).fetchall()
        return tuple(require_identifier(str(row[0]), expected_namespace="field") for row in rows)


__all__ = [
    "LifecycleReactionPort",
    "LifecycleService",
    "SnapshotKind",
    "UpstreamSnapshot",
]


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value
