"""Disposable SQLite projections for ingress, evidence epochs, and derivations.

The append-only event stream remains authority.  This adapter can reconstruct
reaction registrations from typed result events and derives its cursor solely
from that ledger; the cursor is never accepted as evidence by itself.
"""

from __future__ import annotations

import json
import sqlite3
from itertools import groupby
from pathlib import Path
from typing import cast

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import InlinePayload
from strathmark.v3.contracts.errors import ContractError, V3Error
from strathmark.v3.contracts.events import EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import TargetContext
from strathmark.v3.contracts.identifiers import (
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.domain.epochs import MandatoryReaction, ReactionBarrier
from strathmark.v3.domain.evidence import (
    AdmissionReason,
    IssuedFieldFact,
    LiveResultSubmission,
    admit_observation,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection

_PENDING_AT = "1970-01-01T00:00:00.000Z"
_RESULT_EVENTS = (
    EventKind.RESULT_RECORDED.value,
    EventKind.RESULT_SUPERSEDED.value,
)


class ProjectionError(V3Error, RuntimeError):
    code = "projection_error"


class ProjectionConflict(ProjectionError):
    code = "projection_conflict"


class SQLiteProjectionStore:
    """Rebuildable views and the append-only mandatory-reaction ledger."""

    def __init__(self, database_path: Path | str) -> None:
        if isinstance(database_path, bool) or not isinstance(database_path, (Path, str)):
            raise ProjectionError("database_path must be a filesystem path")
        self._database_path = Path(database_path).expanduser().resolve(strict=False)
        with open_v3_connection(self._database_path) as connection:
            migrate_connection(connection)
            with immediate_transaction(connection):
                self._reconcile_result_sources(connection)
                self._advance_barrier(connection)

    @property
    def database_path(self) -> Path:
        return self._database_path

    def pending_reactions(self, source_global_sequence: int) -> tuple[MandatoryReaction, ...]:
        _positive_sequence(source_global_sequence)
        with open_v3_connection(self._database_path, read_only=True) as connection:
            completed = {
                str(row[0])
                for row in connection.execute(
                    "SELECT reaction_type FROM v3_derivation_reactions "
                    "WHERE source_global_sequence=? AND state='completed'",
                    (source_global_sequence,),
                )
            }
            registered = {
                str(row[0])
                for row in connection.execute(
                    "SELECT reaction_type FROM v3_derivation_reactions "
                    "WHERE source_global_sequence=? AND state='pending'",
                    (source_global_sequence,),
                )
            }
        if registered and registered != {item.value for item in MandatoryReaction}:
            raise ProjectionError("mandatory reaction registration is incomplete")
        return tuple(item for item in MandatoryReaction if item.value in registered - completed)

    def barrier_sequence(self) -> int:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT through_global_sequence, barrier_digest FROM v3_derivation_barrier "
                "WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise ProjectionError("derivation barrier is missing")
            through = int(row[0])
            if str(row[1]) != self._barrier_digest(connection, through):
                raise ProjectionError("derivation barrier digest does not match its ledger")
            return through

    def reaction_barrier_for_tournament(
        self, tournament_id: StableIdentifier | str, through_sequence: int
    ) -> ReactionBarrier:
        require_identifier(tournament_id, expected_namespace="tournament")
        _positive_sequence(through_sequence)
        with open_v3_connection(self._database_path, read_only=True) as connection:
            incomplete = connection.execute(
                "SELECT result.source_global_sequence FROM v3_result_revisions result "
                "WHERE result.tournament_id=? AND result.source_global_sequence<=? "
                "AND NOT EXISTS (SELECT 1 FROM v3_derivation_sequence_completions complete "
                "WHERE complete.source_global_sequence=result.source_global_sequence) "
                "ORDER BY result.source_global_sequence LIMIT 1",
                (str(tournament_id), through_sequence),
            ).fetchone()
        if incomplete is None:
            return ReactionBarrier.complete_through(through_sequence)
        return ReactionBarrier(int(incomplete[0]) - 1, frozenset())

    def rebuild_reaction_projection(self) -> str:
        """Wipe U5 views and deterministically replay them from event genesis."""

        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                for table in (
                    "v3_prepared_field_dependencies",
                    "v3_round_issue_seals",
                    "v3_evidence_epoch_members",
                    "v3_evidence_epochs",
                    "v3_round_closures",
                    "v3_derivation_sequence_completions",
                    "v3_derivation_reactions",
                    "v3_result_revisions",
                    "v3_ingress_snapshots",
                ):
                    connection.execute(f"DELETE FROM {table}")  # noqa: S608
                connection.execute(
                    "UPDATE v3_derivation_barrier SET through_global_sequence=0, "
                    "barrier_digest=? WHERE singleton=1",
                    (
                        canonical_digest(
                            {
                                "schema_version": "strathmark-v3-derivation-barrier-v1",
                                "through_global_sequence": 0,
                                "completed": [],
                            }
                        ),
                    ),
                )
                rows = connection.execute(
                    "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
                ).fetchall()
                events = tuple(EventEnvelope.from_dict(json.loads(str(row[0]))) for row in rows)
                for _command_id, grouped in groupby(
                    events, key=lambda item: str(item.command.command_id)
                ):
                    self.apply_events(connection, tuple(grouped))
                self._advance_barrier(connection)
            return self.projection_digest(connection)

    def rebuild_from_checkpoint_registry(self, checkpoint_registry: object) -> str:
        """Rebuild only from authority containing the external signed checkpoint."""

        from strathmark.v3.infrastructure.integrity import CheckpointRegistry

        if not isinstance(checkpoint_registry, CheckpointRegistry):
            raise ProjectionError("trusted rebuild requires a CheckpointRegistry")
        checkpoint_registry.verify_database(self._database_path, require_current=False)
        rebuilt = self.rebuild_reaction_projection()
        checkpoint_registry.verify_database(self._database_path, require_current=False)
        return rebuilt

    def apply_events(
        self, connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]
    ) -> None:
        """Apply newly authoritative events inside the event-store transaction."""

        if not connection.in_transaction:
            raise ProjectionError("projection application requires the event writer transaction")
        if not events:
            raise ProjectionError("projection application requires authoritative events")
        command_ids = {str(event.command.command_id) for event in events}
        if len(command_ids) != 1:
            raise ProjectionError("one projection batch must contain exactly one command")
        if any(not isinstance(event.command.payload, InlinePayload) for event in events):
            raise ProjectionError("U5 authority events require bounded inline payloads")
        self._validate_atomic_event_set(connection, events)
        for event in events:
            payload = cast(InlinePayload, event.command.payload)
            value = payload.to_value()
            if value.get("schema_version") == "strathmark-v3-correction-settlement-v1":
                nested_key = (
                    "result"
                    if event.kind is EventKind.RESULT_SUPERSEDED
                    else "settlement"
                    if event.kind is EventKind.LIVE_RACE_SETTLED
                    else None
                )
                if nested_key is not None:
                    nested = value.get(nested_key)
                    if not isinstance(nested, dict):
                        raise ProjectionError("atomic correction payload is malformed")
                    value = nested
            if event.kind in {
                EventKind.TOURNAMENT_SNAPSHOT_REVISED,
                EventKind.ROUND_SNAPSHOT_REVISED,
                EventKind.FIELD_ROSTER_REVISED,
            }:
                self._apply_snapshot(connection, event, value)
            elif event.kind is EventKind.TOURNAMENT_OPENED:
                self._apply_tournament_open(connection, event, value)
            elif event.kind is EventKind.TOURNAMENT_CLOSED:
                self._apply_tournament_close(connection, event, value)
            elif event.kind in {EventKind.RESULT_RECORDED, EventKind.RESULT_SUPERSEDED}:
                self._apply_result(connection, event, value)
            elif event.kind is EventKind.DERIVATION_SEQUENCE_COMPLETED:
                self._apply_derivation_sequence(connection, event, value)
            elif event.kind is EventKind.DERIVATION_REACTION_COMPLETED:
                self._apply_derivation_reaction(connection, event, value)
            elif event.kind is EventKind.ROUND_EPOCH_FROZEN:
                self._apply_epoch(connection, event, value)
            elif (
                event.kind is EventKind.ROUND_CLOSED
                and value.get("schema_version") == "strathmark-v3-round-closure-v1"
            ):
                self._apply_round_closure(connection, event, value)
            elif event.kind is EventKind.LIVE_RACE_SETTLED:
                self._apply_settlement(connection, event, value)
            elif event.kind is EventKind.FIELD_ISSUED:
                self._apply_issue_seal(connection, event, value)
            elif event.kind in {EventKind.FIELD_OPTIMIZED, EventKind.FIELD_REGENERATED}:
                self._apply_prepared_dependency(connection, event, value)
            elif event.kind is EventKind.FIELD_SUPERSEDED:
                self._apply_field_superseded(connection, event)
            elif event.kind is EventKind.ROUND_CLOSING_STARTED:
                self._apply_round_closing_started(connection, event)
        self._advance_barrier(connection)

    @staticmethod
    def _validate_atomic_event_set(
        connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]
    ) -> None:
        command_kind = events[0].command.kind
        field_supersessions = {
            str(event.aggregate_id) for event in events if event.kind is EventKind.FIELD_SUPERSEDED
        }
        if command_kind.value == "freeze_evidence_epoch":
            epochs = [event for event in events if event.kind is EventKind.ROUND_EPOCH_FROZEN]
            rounds = [event for event in events if event.kind is EventKind.ROUND_FROZEN]
            if len(epochs) != 1 or len(rounds) != 1:
                raise ProjectionConflict("epoch freeze must atomically freeze its round")
            payload = cast(InlinePayload, epochs[0].command.payload)
            value = payload.to_value()
            epoch_value = value.get("epoch")
            if not isinstance(epoch_value, dict) or str(rounds[0].aggregate_id) != epoch_value.get(
                "round_id"
            ):
                raise ProjectionConflict("epoch freeze round authority does not match its epoch")
        elif command_kind.value == "settle_live_race":
            settlements = [event for event in events if event.kind is EventKind.LIVE_RACE_SETTLED]
            fields = [event for event in events if event.kind is EventKind.FIELD_SETTLED]
            if len(settlements) != 1 or len(fields) != 1:
                raise ProjectionConflict("live settlement must atomically settle its field")
            payload = cast(InlinePayload, settlements[0].command.payload)
            if payload.to_value().get("field_id") != str(fields[0].aggregate_id):
                raise ProjectionConflict("live settlement field authority does not match")
        elif command_kind.value == "revise_field_roster":
            ingress = next(
                (event for event in events if event.kind is EventKind.FIELD_ROSTER_REVISED),
                None,
            )
            if ingress is None:
                raise ProjectionConflict("field revision has no ingress authority event")
            payload = cast(InlinePayload, ingress.command.payload)
            field_id = str(payload.to_value().get("entity_id"))
            prepared = connection.execute(
                "SELECT 1 FROM v3_prepared_field_dependencies dependency "
                "LEFT JOIN v3_round_issue_seals seal ON seal.round_id=dependency.round_id "
                "WHERE dependency.field_id=? AND dependency.invalidated_by_sequence IS NULL "
                "AND seal.round_id IS NULL",
                (field_id,),
            ).fetchone()
            expected = {field_id} if prepared is not None else set()
            if field_supersessions != expected:
                raise ProjectionConflict(
                    "field revision must supersede the complete dependent field set"
                )
        elif command_kind.value == "supersede_and_settle_result":
            result_event = next(
                (event for event in events if event.kind is EventKind.RESULT_SUPERSEDED),
                None,
            )
            if result_event is None:
                raise ProjectionConflict("atomic correction has no result supersession")
            payload = cast(InlinePayload, result_event.command.payload)
            value = payload.to_value()
            nested = value.get("result")
            if not isinstance(nested, dict):
                raise ProjectionConflict("atomic correction result payload is malformed")
            rows = connection.execute(
                "SELECT DISTINCT dependency.field_id FROM v3_prepared_field_dependencies dependency "
                "JOIN v3_evidence_epoch_members member ON member.epoch_id=dependency.epoch_id "
                "LEFT JOIN v3_round_issue_seals seal ON seal.round_id=dependency.round_id "
                "WHERE member.result_key=? AND dependency.invalidated_by_sequence IS NULL "
                "AND seal.round_id IS NULL ORDER BY dependency.field_id",
                (nested.get("result_key"),),
            ).fetchall()
            expected = {str(row[0]) for row in rows}
            if field_supersessions != expected:
                raise ProjectionConflict(
                    "correction must supersede the complete dependent field set"
                )

    @staticmethod
    def _apply_tournament_open(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        required = {
            "schema_version",
            "bundle_id",
            "historical_cutoff_key",
            "root_round_ids",
        }
        if set(value) != required or value["schema_version"] != "strathmark-v3-tournament-open-v1":
            raise ProjectionError("tournament-open payload is not closed")
        roots = value["root_round_ids"]
        if not isinstance(roots, list) or not roots or len(roots) != len(set(roots)):
            raise ProjectionError("tournament open requires unique root rounds")
        tournament = connection.execute(
            "SELECT snapshot_json FROM v3_ingress_snapshots "
            "WHERE entity_kind='tournament' AND entity_id=? "
            "AND source_global_sequence<? ORDER BY upstream_revision DESC LIMIT 1",
            (str(event.aggregate_id), event.global_sequence),
        ).fetchone()
        if tournament is None:
            raise ProjectionConflict("tournament open requires its authoritative snapshot")
        if json.loads(str(tournament[0])) != {
            "bundle_id": value["bundle_id"],
            "historical_cutoff_key": value["historical_cutoff_key"],
        }:
            raise ProjectionConflict("tournament open bundle or historical boundary drifted")
        configured_roots: set[str] = set()
        rows = connection.execute(
            "SELECT entity_id, snapshot_json FROM v3_ingress_snapshots current "
            "WHERE entity_kind='round' AND tournament_id=? AND source_global_sequence<? "
            "AND upstream_revision=(SELECT MAX(latest.upstream_revision) "
            "FROM v3_ingress_snapshots latest WHERE latest.entity_kind='round' "
            "AND latest.entity_id=current.entity_id AND latest.source_global_sequence<?)",
            (str(event.aggregate_id), event.global_sequence, event.global_sequence),
        ).fetchall()
        for row in rows:
            snapshot = json.loads(str(row[1]))
            if not snapshot["predecessor_round_ids"]:
                configured_roots.add(str(row[0]))
        if configured_roots != set(roots):
            raise ProjectionConflict("tournament root round lineage is incomplete")

    @staticmethod
    def _apply_tournament_close(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        required = {"schema_version", "deferred_reactions"}
        if set(value) != required or value["schema_version"] != "strathmark-v3-tournament-close-v1":
            raise ProjectionError("tournament-close payload is not closed")
        if value["deferred_reactions"] != ["cancel_jobs", "expire_overlay", "seal_exports"]:
            raise ProjectionError("tournament-close reactions are not closed")
        round_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT entity_id FROM v3_ingress_snapshots "
                "WHERE entity_kind='round' AND tournament_id=? AND source_global_sequence<?",
                (str(event.aggregate_id), event.global_sequence),
            )
        }
        closed = {
            str(row[0])
            for row in connection.execute(
                "SELECT source_round_id FROM v3_round_closures "
                "WHERE tournament_id=? AND closure_global_sequence<?",
                (str(event.aggregate_id), event.global_sequence),
            )
        }
        if not round_ids.issubset(closed):
            raise ProjectionConflict("tournament close requires every configured round closure")
        incomplete = connection.execute(
            "SELECT 1 FROM v3_result_revisions result "
            "WHERE result.tournament_id=? AND result.source_global_sequence<? AND NOT EXISTS ("
            "SELECT 1 FROM v3_derivation_sequence_completions complete "
            "WHERE complete.source_global_sequence=result.source_global_sequence "
            "AND complete.completion_global_sequence<?) LIMIT 1",
            (str(event.aggregate_id), event.global_sequence, event.global_sequence),
        ).fetchone()
        if incomplete is not None:
            raise ProjectionConflict("tournament close is blocked by mandatory derivations")

    @staticmethod
    def _apply_snapshot(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        required = {
            "schema_version",
            "entity_kind",
            "entity_id",
            "upstream_revision",
            "tournament_id",
            "round_id",
            "snapshot",
            "snapshot_digest",
        }
        if (
            set(value) != required
            or value["schema_version"] != "strathmark-v3-upstream-snapshot-v1"
        ):
            raise ProjectionError("upstream snapshot payload is not closed")
        revision = value["upstream_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ProjectionError("upstream revision must be positive")
        latest = connection.execute(
            "SELECT COALESCE(MAX(upstream_revision), 0), MAX(tournament_id), MAX(round_id) "
            "FROM v3_ingress_snapshots "
            "WHERE entity_kind=? AND entity_id=?",
            (value["entity_kind"], value["entity_id"]),
        ).fetchone()
        if revision <= int(latest[0]):
            raise ProjectionConflict("upstream revisions must be strictly monotonic")
        if int(latest[0]) and (
            str(latest[1]) != value["tournament_id"]
            or (None if latest[2] is None else str(latest[2])) != value["round_id"]
        ):
            raise ProjectionConflict("an upstream identity cannot change its parent lineage")
        tournament_closed = connection.execute(
            "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind=? "
            "AND global_sequence<? LIMIT 1",
            (
                value["tournament_id"],
                EventKind.TOURNAMENT_CLOSED.value,
                event.global_sequence,
            ),
        ).fetchone()
        if tournament_closed is not None:
            raise ProjectionConflict("no upstream ingress is accepted after tournament close")
        if value["entity_kind"] == "tournament":
            tournament_opened = connection.execute(
                "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "AND global_sequence<? LIMIT 1",
                (
                    value["tournament_id"],
                    EventKind.TOURNAMENT_OPENED.value,
                    event.global_sequence,
                ),
            ).fetchone()
            if tournament_opened is not None:
                raise ProjectionConflict(
                    "tournament bundle and historical cutoff are pinned at open"
                )
        if value["entity_kind"] == "round":
            round_frozen = connection.execute(
                "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "AND global_sequence<? LIMIT 1",
                (value["round_id"], EventKind.ROUND_FROZEN.value, event.global_sequence),
            ).fetchone()
            if round_frozen is not None:
                raise ProjectionConflict("round lineage is pinned by its first frozen epoch")
        if value["entity_kind"] == "field":
            issued = connection.execute(
                "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "AND global_sequence<?",
                (value["entity_id"], EventKind.FIELD_ISSUED.value, event.global_sequence),
            ).fetchone()
            if issued is not None:
                raise ProjectionConflict(
                    "an issued legal field is immutable; create a new field for reissue"
                )
        snapshot = value["snapshot"]
        if not isinstance(snapshot, dict) or canonical_digest(snapshot) != value["snapshot_digest"]:
            raise ProjectionError("upstream snapshot digest mismatch")
        SQLiteProjectionStore._validate_snapshot_contract(event, value, snapshot)
        connection.execute(
            "INSERT INTO v3_ingress_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                value["entity_kind"],
                value["entity_id"],
                revision,
                value["tournament_id"],
                value["round_id"],
                canonical_bytes(snapshot).decode(),
                value["snapshot_digest"],
                event.global_sequence,
            ),
        )

    def _apply_result(
        self, connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        required = {
            "schema_version",
            "result_key",
            "submission",
            "field_revision",
            "claimed_receipt_id",
            "candidate_numeric_eligible",
            "admission_reason",
        }
        if set(value) != required or value["schema_version"] != "strathmark-v3-live-result-v1":
            raise ProjectionError("live result payload is not closed")
        submitted = value["submission"]
        if not isinstance(submitted, dict):
            raise ProjectionError("live result submission must be an object")
        normalized_contract = LiveResultSubmission.from_dict(submitted).to_observation(
            event.global_sequence
        )
        observation = normalized_contract.to_dict()
        result = cast(dict[str, object], observation["result"])
        revision = result.get("revision")
        revision = cast(int, revision)
        latest = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM v3_result_revisions WHERE result_key=?",
            (value["result_key"],),
        ).fetchone()
        if int(latest[0]) + 1 != revision:
            raise ProjectionConflict("result revisions must be consecutive")
        candidate = value["candidate_numeric_eligible"]
        if not isinstance(candidate, bool):
            raise ProjectionError("candidate eligibility must be explicit")
        issued = self._issued_field_fact(
            connection, normalized_contract.field_id, before_sequence=event.global_sequence
        )
        ingress = connection.execute(
            "SELECT tournament_id, round_id, snapshot_json FROM v3_ingress_snapshots "
            "WHERE entity_kind='field' AND entity_id=? AND source_global_sequence<? "
            "ORDER BY upstream_revision DESC LIMIT 1",
            (str(normalized_contract.field_id), event.global_sequence),
        ).fetchone()
        if ingress is None:
            raise ProjectionConflict("live result field has no authoritative ingress")
        field_snapshot = json.loads(str(ingress[2]))
        if (
            str(ingress[0]) != str(normalized_contract.tournament_id)
            or str(ingress[1]) != str(normalized_contract.round_id)
            or field_snapshot["target_context"] != normalized_contract.context.to_dict()
        ):
            raise ProjectionConflict("live result does not match its field lineage or context")
        expected_result_key = deterministic_identifier(
            "result",
            {
                "field_id": str(normalized_contract.field_id),
                "field_revision": value["field_revision"],
                "competitor_id": str(normalized_contract.competitor_id),
            },
        )
        if (
            str(event.aggregate_id) != value["result_key"]
            or str(expected_result_key) != value["result_key"]
        ):
            raise ProjectionConflict("live result identity is not field-revision addressed")
        classified = admit_observation(
            normalized_contract,
            issued_field=issued,
            field_revision=value["field_revision"],
            claimed_receipt_id=require_identifier(
                value["claimed_receipt_id"], expected_namespace="receipt"
            ),
        )
        if classified.reason not in {
            AdmissionReason.ELIGIBLE_COMPLETION,
            AdmissionReason.STATUS_INELIGIBLE,
        }:
            raise ProjectionConflict(
                f"live result does not match authoritative issue: {classified.reason.value}"
            )
        if (
            classified.numeric_eligible != candidate
            or classified.reason.value != value["admission_reason"]
        ):
            raise ProjectionConflict("result admission claims do not match authoritative issue")
        connection.execute(
            "INSERT INTO v3_result_revisions(result_key, tournament_id, revision, source_global_sequence, "
            "round_id, field_id, competitor_id, field_revision, claimed_receipt_id, observation_json, "
            "observation_digest, candidate_numeric_eligible, numeric_eligible, admission_reason, "
            "settled_global_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)",
            (
                value["result_key"],
                observation["tournament_id"],
                revision,
                event.global_sequence,
                observation["round_id"],
                observation["field_id"],
                observation["competitor_id"],
                value["field_revision"],
                value["claimed_receipt_id"],
                canonical_bytes(observation).decode(),
                canonical_digest(observation),
                int(candidate),
                value["admission_reason"],
            ),
        )
        self._register_result_source(connection, event.global_sequence, _PENDING_AT)

    @staticmethod
    def _validate_snapshot_contract(
        event: EventEnvelope, value: dict[str, object], snapshot: dict[str, object]
    ) -> None:
        event_binding = {
            EventKind.TOURNAMENT_SNAPSHOT_REVISED: "tournament",
            EventKind.ROUND_SNAPSHOT_REVISED: "round",
            EventKind.FIELD_ROSTER_REVISED: "field",
        }
        expected_kind = event_binding[event.kind]
        if value["entity_kind"] != expected_kind:
            raise ProjectionConflict("snapshot event kind does not match its entity kind")
        entity_id = require_identifier(value["entity_id"], expected_namespace=expected_kind)
        tournament_id = require_identifier(value["tournament_id"], expected_namespace="tournament")
        expected_aggregate = deterministic_identifier(
            event.aggregate_kind.value, {"entity_id": str(entity_id)}
        )
        if event.aggregate_id != expected_aggregate:
            raise ProjectionConflict("snapshot aggregate identity is not deterministic")
        if expected_kind == "tournament":
            if (
                value["round_id"] is not None
                or entity_id != tournament_id
                or set(snapshot)
                != {
                    "bundle_id",
                    "historical_cutoff_key",
                }
            ):
                raise ProjectionConflict("tournament snapshot identity or fields are invalid")
            require_identifier(snapshot["bundle_id"], expected_namespace="bundle")
            require_identifier(snapshot["historical_cutoff_key"], expected_namespace="history")
            return
        round_id = require_identifier(value["round_id"], expected_namespace="round")
        if expected_kind == "round":
            if entity_id != round_id or set(snapshot) != {
                "round_ordinal",
                "predecessor_round_ids",
                "successor_round_ids",
            }:
                raise ProjectionConflict("round snapshot identity or fields are invalid")
            ordinal = snapshot["round_ordinal"]
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
                raise ProjectionConflict("round snapshot ordinal must be positive")
            for name in ("predecessor_round_ids", "successor_round_ids"):
                relations = snapshot[name]
                if not isinstance(relations, list):
                    raise ProjectionConflict("round snapshot relations must be arrays")
                normalized = [
                    str(require_identifier(item, expected_namespace="round")) for item in relations
                ]
                if len(normalized) != len(set(normalized)):
                    raise ProjectionConflict("round snapshot relations cannot repeat")
            return
        if set(snapshot) != {"competitor_ids", "target_context", "stand_ids"}:
            raise ProjectionConflict("field snapshot fields are invalid")
        roster = snapshot["competitor_ids"]
        stands = snapshot["stand_ids"]
        if not isinstance(roster, list) or not roster or not isinstance(stands, list):
            raise ProjectionConflict("field snapshot roster and stands must be arrays")
        competitors = [
            str(require_identifier(item, expected_namespace="competitor")) for item in roster
        ]
        stand_ids = [str(require_identifier(item, expected_namespace="stand")) for item in stands]
        if (
            len(competitors) != len(set(competitors))
            or len(stand_ids) != len(set(stand_ids))
            or len(competitors) != len(stand_ids)
        ):
            raise ProjectionConflict("field snapshot roster and stands must be one-to-one")
        TargetContext.from_dict(snapshot["target_context"])

    def _apply_derivation_reaction(
        self, connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        if (
            set(value) != {"schema_version", "source_global_sequence", "reaction", "output_digest"}
            or value["schema_version"] != "strathmark-v3-derivation-reaction-v1"
        ):
            raise ProjectionError("derivation reaction payload is not closed")
        source = value["source_global_sequence"]
        _positive_sequence(source)
        try:
            reaction = MandatoryReaction(value["reaction"])
        except (TypeError, ValueError) as exc:
            raise ProjectionError("derivation reaction vocabulary is invalid") from exc
        output_digest = value["output_digest"]
        _digest(output_digest)
        expected_reaction_id = deterministic_identifier(
            "reaction", {"source": source, "reaction": reaction.value}
        )
        if event.aggregate_id != expected_reaction_id:
            raise ProjectionConflict("derivation reaction authority identity is not deterministic")
        self._register_result_source(connection, source, _PENDING_AT)
        settled = connection.execute(
            "SELECT settled_global_sequence FROM v3_result_revisions "
            "WHERE source_global_sequence=?",
            (source,),
        ).fetchone()
        if settled is None or settled[0] is None:
            raise ProjectionConflict("unsettled result cannot complete learning reactions")
        connection.execute(
            "INSERT INTO v3_derivation_reactions VALUES (?, ?, 'completed', ?, ?)",
            (source, reaction.value, output_digest, event.occurred_at_utc),
        )

    @staticmethod
    def _apply_derivation_sequence(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        if (
            set(value) != {"schema_version", "source_global_sequence", "completion_digest"}
            or value["schema_version"] != "strathmark-v3-derivation-sequence-v1"
        ):
            raise ProjectionError("derivation sequence payload is not closed")
        source = value["source_global_sequence"]
        _positive_sequence(source)
        _digest(value["completion_digest"])
        expected_derivation_id = deterministic_identifier("derivation", {"source": source})
        if event.aggregate_id != expected_derivation_id:
            raise ProjectionConflict("derivation sequence authority identity is not deterministic")
        rows = connection.execute(
            "SELECT reaction_type, output_digest FROM v3_derivation_reactions "
            "WHERE source_global_sequence=? AND state='completed' ORDER BY reaction_type",
            (source,),
        ).fetchall()
        if len(rows) != len(MandatoryReaction):
            raise ProjectionConflict("derivation sequence cannot complete before every reaction")
        expected_digest = canonical_digest([[str(row[0]), str(row[1])] for row in rows])
        if value["completion_digest"] != expected_digest:
            raise ProjectionConflict("derivation sequence digest does not match completed outputs")
        connection.execute(
            "INSERT INTO v3_derivation_sequence_completions VALUES (?, ?, ?)",
            (source, event.global_sequence, value["completion_digest"]),
        )

    @staticmethod
    def _apply_round_closure(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        required = {
            "schema_version",
            "closure_id",
            "tournament_id",
            "source_round_id",
            "target_round_ids",
            "results",
            "result_set_digest",
        }
        if set(value) != required or value["schema_version"] != "strathmark-v3-round-closure-v1":
            raise ProjectionError("round closure payload is not closed")
        if str(event.aggregate_id) != value["source_round_id"]:
            raise ProjectionConflict("round closure source does not match its round authority")
        SQLiteProjectionStore._require_all_fields_settled(
            connection,
            str(value["source_round_id"]),
            before_sequence=event.global_sequence,
        )
        ingress = connection.execute(
            "SELECT tournament_id, snapshot_json FROM v3_ingress_snapshots "
            "WHERE entity_kind='round' AND entity_id=? AND source_global_sequence<? "
            "ORDER BY upstream_revision DESC LIMIT 1",
            (value["source_round_id"], event.global_sequence),
        ).fetchone()
        if ingress is None or str(ingress[0]) != value["tournament_id"]:
            raise ProjectionConflict("round closure tournament lineage is invalid")
        if json.loads(str(ingress[1]))["successor_round_ids"] != value["target_round_ids"]:
            raise ProjectionConflict("round closure successor lineage drifted")
        results = value["results"]
        if not isinstance(results, list) or canonical_digest(results) != value["result_set_digest"]:
            raise ProjectionError("round closure result set digest mismatch")
        expected = SQLiteProjectionStore._active_set_for_round(
            connection,
            str(value["tournament_id"]),
            str(value["source_round_id"]),
            before_sequence=event.global_sequence,
        )
        if results != expected:
            raise ProjectionConflict(
                "round closure does not seal the exact active settled result set"
            )
        expected_closure_id = deterministic_identifier(
            "round_closure",
            {
                "tournament_id": value["tournament_id"],
                "source_round_id": value["source_round_id"],
                "target_round_ids": value["target_round_ids"],
                "results": results,
            },
        )
        if str(expected_closure_id) != value["closure_id"]:
            raise ProjectionConflict("round closure identity is not content addressed")
        connection.execute(
            "INSERT INTO v3_round_closures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                value["closure_id"],
                value["tournament_id"],
                value["source_round_id"],
                canonical_bytes(value["target_round_ids"]).decode(),
                event.global_sequence,
                canonical_bytes(results).decode(),
                value["result_set_digest"],
                event.occurred_at_utc,
            ),
        )

    @staticmethod
    def _active_set_for_round(
        connection: sqlite3.Connection,
        tournament_id: str,
        round_id: str,
        *,
        before_sequence: int,
    ) -> list[dict[str, object]]:
        epoch = connection.execute(
            "SELECT epoch_id FROM v3_evidence_epochs WHERE round_id=? "
            "AND frozen_global_sequence<? ORDER BY epoch_revision DESC LIMIT 1",
            (round_id, before_sequence),
        ).fetchone()
        if epoch is None:
            raise ProjectionConflict("round closure requires its frozen evidence epoch")
        seed = [
            {
                "result_key": str(row[0]),
                "revision": int(row[1]),
                "source_sequence": int(row[2]),
                "numeric_eligible": bool(row[3]),
            }
            for row in connection.execute(
                "SELECT result_key, result_revision, source_global_sequence, numeric_eligible "
                "FROM v3_evidence_epoch_members WHERE epoch_id=? ORDER BY result_key",
                (str(epoch[0]),),
            )
        ]
        selected = {
            item["result_key"]: item
            for item in SQLiteProjectionStore._refresh_active_members(
                connection, tournament_id, seed, before_sequence=before_sequence
            )
        }
        rows = connection.execute(
            "SELECT result_key, revision, source_global_sequence, numeric_eligible "
            "FROM v3_result_revisions current WHERE tournament_id=? AND round_id=? "
            "AND source_global_sequence<? AND settled_global_sequence<? "
            "AND revision=(SELECT MAX(latest.revision) FROM v3_result_revisions latest "
            "WHERE latest.result_key=current.result_key AND latest.source_global_sequence<? "
            "AND latest.settled_global_sequence<?) ORDER BY result_key",
            (
                tournament_id,
                round_id,
                before_sequence,
                before_sequence,
                before_sequence,
                before_sequence,
            ),
        ).fetchall()
        for row in rows:
            item = {
                "result_key": str(row[0]),
                "revision": int(row[1]),
                "source_sequence": int(row[2]),
                "numeric_eligible": bool(row[3]),
            }
            selected[item["result_key"]] = item
        return [selected[key] for key in sorted(selected)]

    @staticmethod
    def _refresh_active_members(
        connection: sqlite3.Connection,
        tournament_id: str,
        seed: list[dict[str, object]],
        *,
        before_sequence: int,
    ) -> list[dict[str, object]]:
        if not seed:
            return []
        keys = tuple(str(item["result_key"]) for item in seed)
        placeholders = ",".join("?" for _item in keys)
        rows = connection.execute(
            "SELECT result_key, revision, source_global_sequence, numeric_eligible "
            "FROM v3_result_revisions current WHERE tournament_id=? "
            f"AND result_key IN ({placeholders}) AND source_global_sequence<? "  # noqa: S608
            "AND settled_global_sequence<? AND revision=(SELECT MAX(latest.revision) "
            "FROM v3_result_revisions latest WHERE latest.result_key=current.result_key "
            "AND latest.source_global_sequence<? AND latest.settled_global_sequence<?) "
            "ORDER BY result_key",
            (
                tournament_id,
                *keys,
                before_sequence,
                before_sequence,
                before_sequence,
                before_sequence,
            ),
        ).fetchall()
        refreshed = {
            str(row[0]): {
                "result_key": str(row[0]),
                "revision": int(row[1]),
                "source_sequence": int(row[2]),
                "numeric_eligible": bool(row[3]),
            }
            for row in rows
        }
        return [refreshed.get(str(item["result_key"]), item) for item in seed]

    @staticmethod
    def _apply_epoch(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        if set(value) != {
            "schema_version",
            "epoch_id",
            "content_digest",
            "epoch",
            "closure_ids",
        }:
            raise ProjectionError("epoch event payload is not closed")
        epoch = value["epoch"]
        if not isinstance(epoch, dict) or canonical_digest(epoch) != value["content_digest"]:
            raise ProjectionError("epoch event digest mismatch")
        closure_ids = value["closure_ids"]
        if not isinstance(closure_ids, list) or len(closure_ids) != len(set(closure_ids)):
            raise ProjectionError("epoch predecessor closures must be a unique array")
        ingress = connection.execute(
            "SELECT tournament_id, snapshot_json FROM v3_ingress_snapshots "
            "WHERE entity_kind='round' AND entity_id=? AND source_global_sequence<? "
            "ORDER BY upstream_revision DESC LIMIT 1",
            (epoch["round_id"], event.global_sequence),
        ).fetchone()
        if ingress is None:
            raise ProjectionConflict("epoch target has no authoritative round ingress")
        round_snapshot = json.loads(str(ingress[1]))
        predecessors = set(round_snapshot["predecessor_round_ids"])
        closures = []
        for closure_id in closure_ids:
            row = connection.execute(
                "SELECT source_round_id, target_round_ids_json, closure_global_sequence, "
                "result_set_json, tournament_id FROM v3_round_closures WHERE closure_id=? "
                "AND closure_global_sequence<?",
                (closure_id, event.global_sequence),
            ).fetchone()
            if (
                row is None
                or epoch["round_id"] not in json.loads(str(row[1]))
                or str(row[4]) != str(ingress[0])
            ):
                raise ProjectionConflict("epoch references an unrelated predecessor closure")
            closures.append(row)
        if {str(row[0]) for row in closures} != predecessors:
            raise ProjectionConflict("epoch is missing a predecessor round closure")
        if predecessors:
            boundary = max(int(row[2]) for row in closures)
            selected: dict[str, dict[str, object]] = {}
            for row in closures:
                for member in json.loads(str(row[3])):
                    prior = selected.get(member["result_key"])
                    if prior is None or (member["revision"], member["source_sequence"]) > (
                        prior["revision"],
                        prior["source_sequence"],
                    ):
                        selected[member["result_key"]] = member
            expected_members = SQLiteProjectionStore._refresh_active_members(
                connection,
                str(ingress[0]),
                [selected[key] for key in sorted(selected)],
                before_sequence=event.global_sequence,
            )
            boundary = max(
                [boundary, *(int(member["source_sequence"]) for member in expected_members)]
            )
        else:
            opened = connection.execute(
                "SELECT global_sequence FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "AND global_sequence<? ORDER BY global_sequence DESC LIMIT 1",
                (str(ingress[0]), EventKind.TOURNAMENT_OPENED.value, event.global_sequence),
            ).fetchone()
            if opened is None:
                raise ProjectionConflict("root epoch requires tournament-open authority")
            boundary = int(opened[0])
            expected_members = []
        opened_event = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_id=? AND event_kind=? "
            "AND global_sequence<? ORDER BY global_sequence DESC LIMIT 1",
            (str(ingress[0]), EventKind.TOURNAMENT_OPENED.value, event.global_sequence),
        ).fetchone()
        opening_row = cast(sqlite3.Row, opened_event)
        opening = EventEnvelope.from_dict(json.loads(str(opening_row[0])))
        opening_payload = cast(InlinePayload, opening.command.payload)
        if opening_payload.to_value().get("historical_cutoff_key") != epoch.get(
            "historical_cutoff_key"
        ):
            raise ProjectionConflict("epoch historical boundary drifted from tournament open")
        sealed = connection.execute(
            "SELECT 1 FROM v3_round_issue_seals WHERE round_id=? AND first_issue_global_sequence<?",
            (epoch["round_id"], event.global_sequence),
        ).fetchone()
        if sealed is not None:
            raise ProjectionConflict("an issued round cannot refreeze its evidence epoch")
        if epoch["maximum_tournament_sequence"] != boundary or epoch["members"] != expected_members:
            raise ProjectionConflict("epoch content does not match its closed causal lineage")
        if (
            str(event.aggregate_id) != value["epoch_id"]
            or str(
                deterministic_identifier(
                    "epoch",
                    epoch,
                )
            )
            != value["epoch_id"]
        ):
            raise ProjectionConflict("epoch identity is not content addressed")
        incomplete = connection.execute(
            "SELECT DISTINCT pending.source_global_sequence FROM v3_derivation_reactions pending "
            "JOIN v3_result_revisions result "
            "ON result.source_global_sequence=pending.source_global_sequence "
            "WHERE result.tournament_id=? AND pending.source_global_sequence<=? AND NOT EXISTS ("
            "SELECT 1 FROM v3_derivation_sequence_completions complete "
            "WHERE complete.source_global_sequence=pending.source_global_sequence "
            "AND complete.completion_global_sequence<=?) "
            "ORDER BY pending.source_global_sequence LIMIT 1",
            (str(ingress[0]), boundary, event.global_sequence),
        ).fetchone()
        barrier = event.global_sequence if incomplete is None else int(incomplete[0]) - 1
        if barrier < boundary:
            raise ProjectionConflict("epoch cannot freeze before the derivation barrier")
        connection.execute(
            "INSERT INTO v3_evidence_epochs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                value["epoch_id"],
                epoch["round_id"],
                epoch["epoch_revision"],
                epoch["maximum_tournament_sequence"],
                epoch["historical_cutoff_key"],
                canonical_bytes(epoch).decode(),
                value["content_digest"],
                event.global_sequence,
                event.occurred_at_utc,
            ),
        )
        for member in epoch["members"]:
            connection.execute(
                "INSERT INTO v3_evidence_epoch_members VALUES (?, ?, ?, ?, ?)",
                (
                    value["epoch_id"],
                    member["result_key"],
                    member["revision"],
                    member["source_sequence"],
                    int(member["numeric_eligible"]),
                ),
            )

    @staticmethod
    def _apply_settlement(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        if set(value) != {"schema_version", "field_id", "field_revision", "receipt_id", "results"}:
            raise ProjectionError("live settlement payload is not closed")
        expected_settlement_id = deterministic_identifier(
            "settlement", {key: item for key, item in value.items() if key != "schema_version"}
        )
        if event.aggregate_id != expected_settlement_id:
            raise ProjectionConflict("live settlement authority identity is not deterministic")
        results = value["results"]
        if not isinstance(results, list):
            raise ProjectionError("live settlement results must be an array")
        issued_row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_id=? AND event_kind=? "
            "AND global_sequence<? ORDER BY global_sequence DESC LIMIT 1",
            (value["field_id"], EventKind.FIELD_ISSUED.value, event.global_sequence),
        ).fetchone()
        if issued_row is None:
            raise ProjectionConflict("settlement requires an acknowledged issued receipt")
        issued_event = EventEnvelope.from_dict(json.loads(str(issued_row[0])))
        issued_payload = cast(InlinePayload, issued_event.command.payload)
        issued = issued_payload.to_value()
        if (
            issued.get("field_revision") != value["field_revision"]
            or issued.get("receipt_id") != value["receipt_id"]
        ):
            raise ProjectionConflict("settlement does not match the acknowledged receipt")
        roster = cast(list[object], issued["competitor_ids"])
        competitors: list[str] = []
        for item in results:
            if not isinstance(item, dict) or set(item) != {
                "result_key",
                "revision",
                "competitor_id",
            }:
                raise ProjectionError("live settlement result identity is malformed")
            row = connection.execute(
                "SELECT candidate_numeric_eligible, field_id, field_revision, competitor_id, "
                "source_global_sequence, claimed_receipt_id FROM v3_result_revisions "
                "WHERE result_key=? AND revision=? AND source_global_sequence<?",
                (item["result_key"], item["revision"], event.global_sequence),
            ).fetchone()
            if (
                row is None
                or str(row[1]) != value["field_id"]
                or int(row[2]) != value["field_revision"]
                or str(row[3]) != item["competitor_id"]
                or str(row[5]) != value["receipt_id"]
            ):
                raise ProjectionConflict("settlement references the wrong result or field revision")
            competitors.append(str(item["competitor_id"]))
            connection.execute(
                "UPDATE v3_result_revisions SET numeric_eligible=candidate_numeric_eligible, "
                "settled_global_sequence=? WHERE result_key=? AND revision=?",
                (event.global_sequence, item["result_key"], item["revision"]),
            )
        if len(competitors) != len(set(competitors)) or set(competitors) != set(roster):
            raise ProjectionConflict(
                "settlement must contain exactly one active outcome per entrant"
            )

    @staticmethod
    def _issued_field_fact(
        connection: sqlite3.Connection, field_id: object, *, before_sequence: int
    ) -> IssuedFieldFact | None:
        row = connection.execute(
            "SELECT global_sequence, envelope_json FROM v3_events "
            "WHERE aggregate_id=? AND event_kind=? "
            "AND global_sequence<? ORDER BY global_sequence DESC LIMIT 1",
            (str(field_id), EventKind.FIELD_ISSUED.value, before_sequence),
        ).fetchone()
        if row is None:
            return None
        issue_sequence = int(row[0])
        event = EventEnvelope.from_dict(json.loads(str(row[1])))
        payload = cast(InlinePayload, event.command.payload)
        value = payload.to_value()
        ingress = connection.execute(
            "SELECT tournament_id, round_id, snapshot_json FROM v3_ingress_snapshots "
            "WHERE entity_kind='field' AND entity_id=? AND source_global_sequence<? "
            "ORDER BY upstream_revision DESC LIMIT 1",
            (str(field_id), issue_sequence),
        ).fetchone()
        ingress = cast(sqlite3.Row, ingress)
        snapshot = json.loads(str(ingress[2]))
        roster = tuple(
            require_identifier(item, expected_namespace="competitor")
            for item in value["competitor_ids"]
        )
        marks = cast(dict[str, int], value["issued_marks"])
        return IssuedFieldFact(
            require_identifier(field_id, expected_namespace="field"),
            value["field_revision"],
            roster,
            require_identifier(value["receipt_id"], expected_namespace="receipt"),
            require_identifier(ingress[0], expected_namespace="tournament"),
            require_identifier(ingress[1], expected_namespace="round"),
            TargetContext.from_dict(snapshot["target_context"]),
            tuple((competitor_id, marks[str(competitor_id)]) for competitor_id in roster),
        )

    @staticmethod
    def _apply_issue_seal(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        required = {
            "round_id",
            "epoch_id",
            "field_revision",
            "receipt_id",
            "competitor_ids",
            "issued_marks",
        }
        if not required.issubset(value):
            raise ProjectionError("issued field payload is missing mandatory U5 bindings")
        require_identifier(value["receipt_id"], expected_namespace="receipt")
        ingress = connection.execute(
            "SELECT upstream_revision, round_id, snapshot_json FROM v3_ingress_snapshots "
            "WHERE entity_kind='field' AND entity_id=? AND source_global_sequence<? "
            "ORDER BY upstream_revision DESC LIMIT 1",
            (str(event.aggregate_id), event.global_sequence),
        ).fetchone()
        if ingress is None:
            raise ProjectionConflict("issued field requires authoritative field ingress")
        snapshot = json.loads(str(ingress[2]))
        marks = value["issued_marks"]
        roster = value["competitor_ids"]
        if (
            not isinstance(marks, dict)
            or not isinstance(roster, list)
            or set(marks) != set(roster)
            or any(
                isinstance(mark, bool) or not isinstance(mark, int) or mark <= 0
                for mark in marks.values()
            )
        ):
            raise ProjectionConflict("issued marks must exactly bind the issued roster")
        if str(ingress[1]) != value["round_id"] or snapshot["competitor_ids"] != roster:
            raise ProjectionConflict("issued field does not match its exact roster revision")
        existing = connection.execute(
            "SELECT epoch_id FROM v3_round_issue_seals WHERE round_id=?", (value["round_id"],)
        ).fetchone()
        if existing is not None and str(existing[0]) != value["epoch_id"]:
            raise ProjectionConflict("a round cannot issue fields from mixed epochs")
        dependency = connection.execute(
            "SELECT field_revision, round_id, epoch_id FROM v3_prepared_field_dependencies "
            "WHERE field_id=? AND invalidated_by_sequence IS NULL "
            "AND prepared_global_sequence<? ORDER BY prepared_global_sequence DESC LIMIT 1",
            (str(event.aggregate_id), event.global_sequence),
        ).fetchone()
        if dependency is None or (
            int(dependency[0]),
            str(dependency[1]),
            str(dependency[2]),
        ) != (value["field_revision"], value["round_id"], value["epoch_id"]):
            raise ProjectionConflict(
                "issued field must match its exact current prepared dependency"
            )
        SQLiteProjectionStore._require_round_frozen_epoch(
            connection,
            value["round_id"],
            value["epoch_id"],
            before_sequence=event.global_sequence,
        )
        if existing is not None:
            return
        connection.execute(
            "INSERT INTO v3_round_issue_seals VALUES (?, ?, ?, ?)",
            (value["round_id"], value["epoch_id"], event.global_sequence, event.occurred_at_utc),
        )

    @staticmethod
    def _apply_prepared_dependency(
        connection: sqlite3.Connection, event: EventEnvelope, value: dict[str, object]
    ) -> None:
        required = {"round_id", "epoch_id", "field_revision"}
        if not required.issubset(value):
            raise ProjectionError("prepared field payload is missing mandatory U5 bindings")
        revision = value["field_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ProjectionError("prepared field revision must be positive")
        round_id = require_identifier(value["round_id"], expected_namespace="round")
        require_identifier(value["epoch_id"], expected_namespace="epoch")
        ingress = connection.execute(
            "SELECT round_id FROM v3_ingress_snapshots WHERE entity_kind='field' "
            "AND entity_id=? AND source_global_sequence<? "
            "ORDER BY upstream_revision DESC LIMIT 1",
            (str(event.aggregate_id), event.global_sequence),
        ).fetchone()
        if ingress is None or str(ingress[0]) != str(round_id):
            raise ProjectionConflict("prepared field does not match authoritative field ingress")
        SQLiteProjectionStore._require_round_frozen_epoch(
            connection,
            str(round_id),
            value["epoch_id"],
            before_sequence=event.global_sequence,
        )
        connection.execute(
            "INSERT OR REPLACE INTO v3_prepared_field_dependencies VALUES (?, ?, ?, ?, ?, NULL)",
            (
                str(event.aggregate_id),
                value["field_revision"],
                value["round_id"],
                value["epoch_id"],
                event.global_sequence,
            ),
        )

    @staticmethod
    def _require_round_frozen_epoch(
        connection: sqlite3.Connection,
        round_id: object,
        epoch_id: object,
        *,
        before_sequence: int,
    ) -> None:
        row = connection.execute(
            "SELECT event_kind, envelope_json FROM v3_events WHERE aggregate_id=? "
            "AND aggregate_kind='round' AND global_sequence<? "
            "ORDER BY aggregate_version DESC LIMIT 1",
            (str(round_id), before_sequence),
        ).fetchone()
        if row is None or str(row[0]) != EventKind.ROUND_FROZEN.value:
            raise ProjectionConflict("field round is not currently frozen for preparation")
        frozen = EventEnvelope.from_dict(json.loads(str(row[1])))
        payload = cast(InlinePayload, frozen.command.payload)
        value = payload.to_value()
        if value.get("schema_version") != "strathmark-v3-epoch-event-v1" or value.get(
            "epoch_id"
        ) != str(epoch_id):
            raise ProjectionConflict("field does not reference its round's current frozen epoch")

    @staticmethod
    def _apply_field_superseded(connection: sqlite3.Connection, event: EventEnvelope) -> None:
        write = connection.execute(
            "UPDATE v3_prepared_field_dependencies SET invalidated_by_sequence=? "
            "WHERE field_id=? AND invalidated_by_sequence IS NULL "
            "AND prepared_global_sequence<?",
            (event.global_sequence, str(event.aggregate_id), event.global_sequence),
        )
        if write.rowcount != 1:
            raise ProjectionConflict("field supersession has no current prepared dependency")

    @staticmethod
    def _apply_round_closing_started(connection: sqlite3.Connection, event: EventEnvelope) -> None:
        SQLiteProjectionStore._require_all_fields_settled(
            connection, str(event.aggregate_id), before_sequence=event.global_sequence
        )

    @staticmethod
    def _require_all_fields_settled(
        connection: sqlite3.Connection, round_id: str, *, before_sequence: int
    ) -> None:
        rows = connection.execute(
            "SELECT DISTINCT field_id FROM v3_prepared_field_dependencies "
            "WHERE round_id=? AND invalidated_by_sequence IS NULL "
            "AND prepared_global_sequence<? ORDER BY field_id",
            (round_id, before_sequence),
        ).fetchall()
        for row in rows:
            settled = connection.execute(
                "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "AND global_sequence<? LIMIT 1",
                (str(row[0]), EventKind.FIELD_SETTLED.value, before_sequence),
            ).fetchone()
            if settled is None:
                raise ProjectionConflict(
                    "round closing requires every prepared field to be exactly settled"
                )

    @staticmethod
    def _register_result_source(
        connection: sqlite3.Connection, source_global_sequence: int, recorded_at: str
    ) -> None:
        for reaction in MandatoryReaction:
            connection.execute(
                "INSERT OR IGNORE INTO v3_derivation_reactions(source_global_sequence, "
                "reaction_type, state, output_digest, recorded_at) "
                "VALUES (?, ?, 'pending', NULL, ?)",
                (source_global_sequence, reaction.value, recorded_at),
            )

    def _reconcile_result_sources(self, connection: sqlite3.Connection) -> None:
        authoritative = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT global_sequence FROM v3_events WHERE event_kind IN (?, ?) "
                "ORDER BY global_sequence",
                _RESULT_EVENTS,
            )
        )
        for sequence in authoritative:
            self._register_result_source(connection, sequence, _PENDING_AT)
        registered = {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source_global_sequence FROM v3_derivation_reactions"
            )
        }
        if not registered.issubset(set(authoritative)):
            raise ProjectionError("reaction ledger contains a non-authoritative source")

    def _advance_barrier(self, connection: sqlite3.Connection) -> None:
        pending_rows = connection.execute(
            "SELECT source_global_sequence FROM v3_derivation_reactions pending "
            "WHERE pending.state='pending' AND NOT EXISTS ("
            "SELECT 1 FROM v3_derivation_sequence_completions complete "
            "WHERE complete.source_global_sequence=pending.source_global_sequence) "
            "ORDER BY source_global_sequence LIMIT 1"
        ).fetchone()
        event_tip = int(
            connection.execute(
                "SELECT COALESCE(MAX(global_sequence), 0) FROM v3_events"
            ).fetchone()[0]
        )
        registered_tip = int(
            connection.execute(
                "SELECT COALESCE(MAX(source_global_sequence), 0) FROM v3_derivation_reactions"
            ).fetchone()[0]
        )
        tip = max(event_tip, registered_tip)
        through = tip if pending_rows is None else int(pending_rows[0]) - 1
        digest = self._barrier_digest(connection, through)
        connection.execute(
            "UPDATE v3_derivation_barrier SET through_global_sequence=?, barrier_digest=? "
            "WHERE singleton=1",
            (through, digest),
        )

    @staticmethod
    def _barrier_digest(connection: sqlite3.Connection, through: int) -> str:
        completed = [
            [int(row[0]), str(row[1]), str(row[2])]
            for row in connection.execute(
                "SELECT source_global_sequence, reaction_type, output_digest "
                "FROM v3_derivation_reactions WHERE state='completed' "
                "AND source_global_sequence<=? ORDER BY source_global_sequence, reaction_type",
                (through,),
            )
        ]
        return canonical_digest(
            {
                "schema_version": "strathmark-v3-derivation-barrier-v1",
                "through_global_sequence": through,
                "completed": completed,
            }
        )

    @staticmethod
    def projection_digest(connection: sqlite3.Connection) -> str:
        tables = (
            "v3_ingress_snapshots",
            "v3_result_revisions",
            "v3_derivation_reactions",
            "v3_derivation_barrier",
            "v3_evidence_epochs",
            "v3_evidence_epoch_members",
            "v3_derivation_sequence_completions",
            "v3_round_closures",
            "v3_round_issue_seals",
            "v3_prepared_field_dependencies",
        )
        material: dict[str, list[list[object]]] = {}
        for table in tables:
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            material[table] = [
                list(row) for row in sorted(rows, key=lambda row: tuple(str(x) for x in row))
            ]
        return canonical_digest(
            {"schema_version": "strathmark-v3-u5-projections-v1", "tables": material}
        )


def _positive_sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProjectionError("source global sequence must be positive")
    return value


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractError("output digest must be a lower-case SHA-256 digest")
    return value


__all__ = [
    "ProjectionConflict",
    "ProjectionError",
    "SQLiteProjectionStore",
]
