"""Disposable SQLite projections for ingress, evidence epochs, and derivations.

The append-only event stream remains authority.  This adapter can reconstruct
reaction registrations from typed result events and derives its cursor solely
from that ledger; the cursor is never accepted as evidence by itself.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, cast

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import (
    MAX_INLINE_PAYLOAD_BYTES,
    BlobReferenceV2,
    BlobRetentionClass,
    InlinePayload,
)
from strathmark.v3.contracts.errors import ContractError, V3Error
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import TargetContext, require_utc_milliseconds
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
from strathmark.v3.infrastructure.blobs import (
    BlobMetadata,
    BlobStoreError,
    ContentAddressedBlobStore,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection
from strathmark.v3.infrastructure.sqlite.rolling_restart import (
    ZERO_DIGEST,
    advance_rolling_reaction_cursor,
    reset_rolling_reaction_cursor,
)

_PENDING_AT = "1970-01-01T00:00:00.000Z"
_RESULT_EVENTS = (
    EventKind.RESULT_RECORDED.value,
    EventKind.RESULT_SUPERSEDED.value,
)
_MAX_DISAGREEMENT_AUTHORITY_BLOB_BYTES = 16_777_216
_MAX_DISAGREEMENT_AUTHORITY_BLOB_ITEMS = 2_000_000
_FIELD_RECEIPT_PROJECTION_SCHEMA = "strathmark-v3-field-receipt-projection-v1"
_DISAGREEMENT_BLOB_PROJECTION_SCHEMA = "strathmark-v3-disagreement-authority-blob-projection-v1"
_ROLLING_REACTION_EVENTS = {
    EventKind.FIELD_ROSTER_REVISED,
    EventKind.FIELD_SUPERSEDED,
    EventKind.RESULT_RECORDED,
    EventKind.RESULT_SUPERSEDED,
    EventKind.ROUND_EPOCH_FROZEN,
    EventKind.ROUND_CLOSED,
    EventKind.TOURNAMENT_CLOSED,
}
_MODEL_STATUS_EVENTS = {
    EventKind.MODEL_CANDIDATE_CREATED,
    EventKind.MODEL_CANDIDATE_EVALUATED,
    EventKind.BUNDLE_PROMOTED,
    EventKind.BUNDLE_ROLLED_BACK,
    EventKind.TOURNAMENT_OPENED,
}
_MODEL_STATUS_EVENT_VALUES = tuple(sorted(item.value for item in _MODEL_STATUS_EVENTS))
_PROJECTION_TABLES = (
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
    "v3_model_status",
    "v3_model_candidates",
    "v3_model_tournament_pins",
    "v3_expected_time_override_states",
)


class ProjectionError(V3Error, RuntimeError):
    code = "projection_error"


class ProjectionConflict(ProjectionError):
    code = "projection_conflict"


@dataclass(frozen=True, slots=True)
class _VerifiedReceiptCacheEntry:
    projection_json: str
    receipt_digest: str
    blob_signature: tuple[int, int, int, int, int, int] | None
    receipt: Any


@dataclass(frozen=True, slots=True)
class _VerifiedExactRetryCacheEntry:
    field_revision_digest: str
    database_signature: tuple[tuple[str, int, int, int] | None, ...]
    receipt_projection: Mapping[str, Any]
    receipt_blob_signature: tuple[int, int, int, int, int, int] | None
    result: Any


def _sqlite_files_signature(
    database_path: Path,
) -> tuple[tuple[str, int, int, int] | None, ...]:
    """Fingerprint SQLite authority files so cached exact proofs never mask writes."""

    paths = (
        database_path,
        Path(f"{database_path}-wal"),
    )
    signatures: list[tuple[str, int, int, int] | None] = []
    for path in paths:
        try:
            stored = path.stat()
        except FileNotFoundError:
            signatures.append(None)
        else:
            signatures.append((path.name, stored.st_size, stored.st_mtime_ns, stored.st_ctime_ns))
    return tuple(signatures)


def _event_row_matches(row: sqlite3.Row, event: EventEnvelope) -> bool:
    return tuple(
        row[name]
        for name in (
            "global_sequence",
            "event_id",
            "aggregate_kind",
            "aggregate_id",
            "aggregate_version",
            "event_kind",
            "envelope_json",
            "event_digest",
            "prior_global_digest",
            "prior_aggregate_digest",
            "occurred_at_utc",
            "command_id",
        )
    ) == (
        event.global_sequence,
        str(event.event_id),
        event.aggregate_kind.value,
        str(event.aggregate_id),
        event.aggregate_version,
        event.kind.value,
        canonical_bytes(event.to_dict()).decode(),
        event.event_digest,
        event.prior_global_digest,
        event.prior_aggregate_digest,
        event.occurred_at_utc,
        str(event.command.command_id),
    )


_PREPARED_FIELD_COMMIT_CAPABILITY = object()
_VERIFIED_U5_FIELD_CAPABILITY = object()


@dataclass(frozen=True, slots=True)
class _VerifiedU5FieldAuthority:
    field_revision_digest: str
    ingress_source_global_sequence: int
    ingress_event_digest: str
    epoch_source_global_sequence: int
    epoch_event_digest: str
    tournament_open_source_global_sequence: int
    tournament_open_event_digest: str
    tournament_lifecycle_digest: str
    derivation_barrier_digest: str
    proof_digest: str
    _capability: object

    def verify(self) -> None:
        if self._capability is not _VERIFIED_U5_FIELD_CAPABILITY:
            raise ProjectionConflict("U5 field proof is not verifier-owned")
        expected = canonical_digest(
            {
                "schema_version": "strathmark-v3-verified-u5-field-authority-v1",
                "field_revision_digest": self.field_revision_digest,
                "ingress_source_global_sequence": self.ingress_source_global_sequence,
                "ingress_event_digest": self.ingress_event_digest,
                "epoch_source_global_sequence": self.epoch_source_global_sequence,
                "epoch_event_digest": self.epoch_event_digest,
                "tournament_open_source_global_sequence": (
                    self.tournament_open_source_global_sequence
                ),
                "tournament_open_event_digest": self.tournament_open_event_digest,
                "tournament_lifecycle_digest": self.tournament_lifecycle_digest,
                "derivation_barrier_digest": self.derivation_barrier_digest,
            }
        )
        if self.proof_digest != expected:
            raise ProjectionConflict("U5 field proof digest differs")


@dataclass(frozen=True, slots=True)
class _PreparedFieldCommit:
    receipt_digest: str
    field_revision_digest: str
    pipeline_digest: str
    weight_authority_digest: str
    dependence_artifact_digest: str
    card_manifest_digests: tuple[str, ...]
    crn_assignments_digest: str
    receipt_blob_digest: str | None
    disagreement_blob_digest: str | None
    prior_receipt_id: str | None
    prior_source_global_sequence: int | None
    prior_event_digest: str | None
    u5_field_authority_digest: str
    rolling_publications_digest: str | None
    rolling_capabilities_digest: str | None
    manual_action_binding_digest: str | None
    proof_digest: str
    _capability: object

    def verify(self) -> None:
        if self._capability is not _PREPARED_FIELD_COMMIT_CAPABILITY:
            raise ProjectionConflict("field commit proof is not verifier-owned")
        expected = canonical_digest(
            {
                "schema_version": "strathmark-v3-prepared-field-commit-v1",
                "receipt_digest": self.receipt_digest,
                "field_revision_digest": self.field_revision_digest,
                "pipeline_digest": self.pipeline_digest,
                "weight_authority_digest": self.weight_authority_digest,
                "dependence_artifact_digest": self.dependence_artifact_digest,
                "card_manifest_digests": list(self.card_manifest_digests),
                "crn_assignments_digest": self.crn_assignments_digest,
                "receipt_blob_digest": self.receipt_blob_digest,
                "disagreement_blob_digest": self.disagreement_blob_digest,
                "prior_receipt_id": self.prior_receipt_id,
                "prior_source_global_sequence": self.prior_source_global_sequence,
                "prior_event_digest": self.prior_event_digest,
                "u5_field_authority_digest": self.u5_field_authority_digest,
                "rolling_publications_digest": self.rolling_publications_digest,
                "rolling_capabilities_digest": self.rolling_capabilities_digest,
                "manual_action_binding_digest": self.manual_action_binding_digest,
            }
        )
        if self.proof_digest != expected:
            raise ProjectionConflict("field commit proof digest differs")


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
                self._ensure_model_status_projection(connection)

    def bootstrap_rolling_reaction_cursor_cutover(self) -> int:
        """Perform the one-time verified populated-0011 to 0012 cursor replay."""

        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        with open_v3_connection(self._database_path, read_only=True) as connection:
            head = connection.execute(
                "SELECT global_sequence,event_digest FROM v3_events "
                "ORDER BY global_sequence DESC LIMIT 1"
            ).fetchone()
            cursor = connection.execute(
                "SELECT cursor_revision,through_global_sequence,through_event_digest,"
                "relevant_command_count,latest_reaction_id,cursor_digest,updated_at "
                "FROM v3_rolling_reaction_cursor WHERE singleton=1"
            ).fetchone()
        if cursor is None:
            raise ProjectionError("rolling reaction cursor is missing")
        preflight_value = {
            "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
            "cursor_revision": int(cursor[0]),
            "through_global_sequence": int(cursor[1]),
            "through_event_digest": str(cursor[2]),
            "relevant_command_count": int(cursor[3]),
            "latest_reaction_id": str(cursor[4]),
            "updated_at": str(cursor[6]),
        }
        if canonical_digest(preflight_value) != str(cursor[5]):
            raise ProjectionError("rolling reaction cursor digest differs")
        preflight_anchor = (
            0 if head is None else int(head[0]),
            ZERO_DIGEST if head is None else str(head[1]),
        )
        if (
            preflight_value["through_global_sequence"],
            preflight_value["through_event_digest"],
        ) == preflight_anchor:
            return 0
        event_store = SQLiteEventStore(self._database_path)
        event_store.verify()
        trusted_anchor = event_store.current_anchor()
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                head = connection.execute(
                    "SELECT global_sequence,event_digest FROM v3_events "
                    "ORDER BY global_sequence DESC LIMIT 1"
                ).fetchone()
                observed_anchor = (
                    0 if head is None else int(head[0]),
                    ZERO_DIGEST if head is None else str(head[1]),
                )
                if observed_anchor != (
                    trusted_anchor.global_sequence,
                    trusted_anchor.event_digest,
                ):
                    raise ProjectionError(
                        "rolling cursor cutover event authority changed during verification"
                    )
                cursor = connection.execute(
                    "SELECT cursor_revision,through_global_sequence,through_event_digest,"
                    "relevant_command_count,latest_reaction_id,cursor_digest,updated_at "
                    "FROM v3_rolling_reaction_cursor WHERE singleton=1"
                ).fetchone()
                if cursor is None:
                    raise ProjectionError("rolling reaction cursor is missing")
                cursor_value = {
                    "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
                    "cursor_revision": int(cursor[0]),
                    "through_global_sequence": int(cursor[1]),
                    "through_event_digest": str(cursor[2]),
                    "relevant_command_count": int(cursor[3]),
                    "latest_reaction_id": str(cursor[4]),
                    "updated_at": str(cursor[6]),
                }
                if canonical_digest(cursor_value) != str(cursor[5]):
                    raise ProjectionError("rolling reaction cursor digest differs")
                if (
                    cursor_value["through_global_sequence"],
                    cursor_value["through_event_digest"],
                ) == observed_anchor:
                    return 0
                zero_value = {
                    "schema_version": "strathmark-v3-rolling-reaction-cursor-v1",
                    "cursor_revision": 0,
                    "through_global_sequence": 0,
                    "through_event_digest": ZERO_DIGEST,
                    "relevant_command_count": 0,
                    "latest_reaction_id": ZERO_DIGEST,
                    "updated_at": "1970-01-01T00:00:00.000Z",
                }
                checkpoint = connection.execute(
                    "SELECT 1 FROM v3_rolling_restart_checkpoints LIMIT 1"
                ).fetchone()
                if cursor_value != zero_value or checkpoint is not None:
                    raise ProjectionError(
                        "rolling reaction cursor is not eligible for one-time cutover"
                    )
                for table in (
                    "v3_model_tournament_pins",
                    "v3_model_candidates",
                    "v3_model_status",
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
                self._write_model_status(connection, ZERO_DIGEST, None, 0, ZERO_DIGEST)
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
                reset_rolling_reaction_cursor(connection)
                events = tuple(
                    EventEnvelope.from_dict(json.loads(str(row[0])))
                    for row in connection.execute(
                        "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
                    )
                )
                command_count = 0
                for _command_id, grouped in groupby(
                    events, key=lambda item: str(item.command.command_id)
                ):
                    grouped_events = tuple(grouped)
                    self.apply_events(connection, grouped_events)
                    advance_rolling_reaction_cursor(connection, grouped_events)
                    command_count += 1
                self._advance_barrier(connection)
                return command_count

    def rebuild_rolling_reaction_projection_offline(
        self, expected_global_sequence: int, expected_event_digest: str
    ) -> tuple[int, str]:
        """Fully verify authority, then rebuild only U5 views and rolling obligations.

        This is an explicit offline recovery seam.  Callers must stop event writers
        and independently re-anchor the rolling cursor/checkpoint after this method
        returns.  The writer transaction still compares the verified event head so
        a concurrent append cannot be projected against a stale authority snapshot.
        """

        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        if (
            isinstance(expected_global_sequence, bool)
            or not isinstance(expected_global_sequence, int)
            or expected_global_sequence < 0
        ):
            raise ProjectionError("offline rebuild expected global sequence must be non-negative")
        try:
            expected_event_digest = _digest(expected_event_digest)
        except ContractError as exc:
            raise ProjectionError("offline rebuild event digest is invalid") from exc
        if expected_global_sequence == 0 and expected_event_digest != ZERO_DIGEST:
            raise ProjectionError("empty offline rebuild requires the zero digest")
        if expected_global_sequence > 0 and expected_event_digest == ZERO_DIGEST:
            raise ProjectionError("populated offline rebuild requires an event digest")

        event_store = SQLiteEventStore(self._database_path)
        event_store.verify()
        verified_anchor = event_store.current_anchor()
        expected_anchor = (expected_global_sequence, expected_event_digest)
        if (
            verified_anchor.global_sequence,
            verified_anchor.event_digest,
        ) != expected_anchor:
            raise ProjectionError("offline rebuild expected event head differs")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE global_sequence<=? "
                "ORDER BY global_sequence",
                (expected_global_sequence,),
            ).fetchall()
        events = tuple(EventEnvelope.from_dict(json.loads(str(row[0]))) for row in rows)
        if len(events) != expected_global_sequence or (
            events and events[-1].event_digest != expected_event_digest
        ):
            raise ProjectionError("offline rebuild verified event material differs")

        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                head = connection.execute(
                    "SELECT global_sequence,event_digest FROM v3_events "
                    "ORDER BY global_sequence DESC LIMIT 1"
                ).fetchone()
                observed_anchor = (
                    0 if head is None else int(head[0]),
                    ZERO_DIGEST if head is None else str(head[1]),
                )
                if observed_anchor != expected_anchor:
                    raise ProjectionError("event authority changed during offline rebuild")
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
                connection.execute("DROP TRIGGER v3_rolling_reaction_obligations_no_delete")
                connection.execute("DELETE FROM v3_rolling_reaction_obligations")
                connection.execute(
                    "CREATE TRIGGER v3_rolling_reaction_obligations_no_delete "
                    "BEFORE DELETE ON v3_rolling_reaction_obligations BEGIN "
                    "SELECT RAISE(ABORT, 'rolling reaction obligation is immutable'); END"
                )
                for _command_id, grouped in groupby(
                    events, key=lambda item: str(item.command.command_id)
                ):
                    self.apply_events(connection, tuple(grouped), _rebuild_approval=False)
                self._advance_barrier(connection)
        return expected_anchor

    def verify_rolling_reaction_projection(
        self, expected_global_sequence: int, expected_event_digest: str
    ) -> str:
        """Prove current U5 views and rolling obligations equal a genesis replay."""

        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        if (
            isinstance(expected_global_sequence, bool)
            or not isinstance(expected_global_sequence, int)
            or expected_global_sequence < 0
        ):
            raise ProjectionError(
                "rolling projection expected global sequence must be non-negative"
            )
        try:
            expected_event_digest = _digest(expected_event_digest)
        except ContractError as exc:
            raise ProjectionError("rolling projection expected event digest is invalid") from exc
        expected_anchor = (expected_global_sequence, expected_event_digest)
        event_store = SQLiteEventStore(self._database_path)
        event_store.verify()
        verified_anchor = event_store.current_anchor()
        if (
            verified_anchor.global_sequence,
            verified_anchor.event_digest,
        ) != expected_anchor:
            raise ProjectionError("rolling projection expected event head differs")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            events = tuple(
                EventEnvelope.from_dict(json.loads(str(row[0])))
                for row in connection.execute(
                    "SELECT envelope_json FROM v3_events WHERE global_sequence<=? "
                    "ORDER BY global_sequence",
                    (expected_global_sequence,),
                )
            )
        if len(events) != expected_global_sequence or (
            events and events[-1].event_digest != expected_event_digest
        ):
            raise ProjectionError("rolling projection verified event material differs")

        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                head = connection.execute(
                    "SELECT global_sequence,event_digest FROM v3_events "
                    "ORDER BY global_sequence DESC LIMIT 1"
                ).fetchone()
                observed_anchor = (
                    0 if head is None else int(head[0]),
                    ZERO_DIGEST if head is None else str(head[1]),
                )
                if observed_anchor != expected_anchor:
                    raise ProjectionError(
                        "event authority changed during rolling projection verification"
                    )
                observed_digest = self.projection_digest(connection)
                observed_obligations = _rolling_obligation_digest(connection)
                connection.execute("SAVEPOINT verify_rolling_reaction_projection")
                try:
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
                    connection.execute("DROP TRIGGER v3_rolling_reaction_obligations_no_delete")
                    connection.execute("DELETE FROM v3_rolling_reaction_obligations")
                    connection.execute(
                        "CREATE TRIGGER v3_rolling_reaction_obligations_no_delete "
                        "BEFORE DELETE ON v3_rolling_reaction_obligations BEGIN "
                        "SELECT RAISE(ABORT, 'rolling reaction obligation is immutable'); END"
                    )
                    for _command_id, grouped in groupby(
                        events, key=lambda item: str(item.command.command_id)
                    ):
                        self.apply_events(connection, tuple(grouped), _rebuild_approval=False)
                    self._advance_barrier(connection)
                    expected_digest = self.projection_digest(connection)
                    expected_obligations = _rolling_obligation_digest(connection)
                finally:
                    connection.execute("ROLLBACK TO verify_rolling_reaction_projection")
                    connection.execute("RELEASE verify_rolling_reaction_projection")
                if (
                    observed_digest != expected_digest
                    or observed_obligations != expected_obligations
                ):
                    raise ProjectionError(
                        "rolling reaction projection differs from deterministic replay"
                    )
                return observed_digest

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
                reset_rolling_reaction_cursor(connection)
                rows = connection.execute(
                    "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
                ).fetchall()
                events = tuple(EventEnvelope.from_dict(json.loads(str(row[0]))) for row in rows)
                for _command_id, grouped in groupby(
                    events, key=lambda item: str(item.command.command_id)
                ):
                    grouped_events = tuple(grouped)
                    self.apply_events(connection, grouped_events)
                    advance_rolling_reaction_cursor(connection, grouped_events)
                self._advance_barrier(connection)
            return self.projection_digest(connection)

    def active_model_bundle_digest(self) -> str:
        """Return the digest-bound active champion without replaying model history."""

        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = self._verify_model_status_connection(connection)
            return str(row[1])

    def model_candidate_evaluation(
        self, candidate_id: StableIdentifier
    ) -> Mapping[str, Any] | None:
        require_identifier(candidate_id, expected_namespace="bundle")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify_model_status_connection(connection)
            row = connection.execute(
                "SELECT candidate_id,candidate_digest,lineage_digest,evaluation_json,"
                "evaluation_digest,promoted_bundle_digest,source_global_sequence,"
                "source_event_digest,row_digest FROM v3_model_candidates WHERE candidate_id=?",
                (str(candidate_id),),
            ).fetchone()
            if row is None or row[3] is None:
                return None
            self._verify_model_candidate_row(connection, row)
            value = json.loads(str(row[3]))
            if not isinstance(value, dict):
                raise ProjectionError("model candidate evaluation is malformed")
            return value

    def candidate_for_promoted_bundle(self, bundle_digest: str) -> StableIdentifier:
        _require_projection_digest(bundle_digest, "promoted bundle")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify_model_status_connection(connection)
            row = connection.execute(
                "SELECT candidate_id,candidate_digest,lineage_digest,evaluation_json,"
                "evaluation_digest,promoted_bundle_digest,source_global_sequence,"
                "source_event_digest,row_digest FROM v3_model_candidates "
                "WHERE promoted_bundle_digest=?",
                (bundle_digest,),
            ).fetchone()
            if row is None:
                raise ProjectionError("active bundle has no projected promotion authority")
            self._verify_model_candidate_row(connection, row)
            return StableIdentifier(str(row[0]))

    def tournament_bundle_pin(self, tournament_id: StableIdentifier) -> str | None:
        require_identifier(tournament_id, expected_namespace="tournament")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify_model_status_connection(connection)
            row = connection.execute(
                "SELECT tournament_id,bundle_id,source_global_sequence,"
                "source_event_digest,row_digest FROM v3_model_tournament_pins "
                "WHERE tournament_id=?",
                (str(tournament_id),),
            ).fetchone()
            if row is None:
                return None
            value = {
                "schema_version": "strathmark-v3-model-tournament-pin-v1",
                "tournament_id": str(row[0]),
                "bundle_id": str(row[1]),
                "source_global_sequence": int(row[2]),
                "source_event_digest": str(row[3]),
            }
            if canonical_digest(value) != str(row[4]):
                raise ProjectionError("model tournament pin row digest differs")
            self._verify_projected_source(connection, int(row[2]), str(row[3]))
            return str(row[1])

    def rebuild_model_status_projection(self) -> str:
        """Deterministically rebuild model status from the indexed relevant event stream."""

        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        SQLiteEventStore(self._database_path).verify()
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                connection.execute("DELETE FROM v3_model_tournament_pins")
                connection.execute("DELETE FROM v3_model_candidates")
                connection.execute("DELETE FROM v3_model_status")
                self._write_model_status(connection, ZERO_DIGEST, None, 0, ZERO_DIGEST)
                placeholders = ",".join("?" for _ in _MODEL_STATUS_EVENT_VALUES)
                rows = connection.execute(
                    "SELECT envelope_json FROM v3_events WHERE event_kind IN ("
                    + placeholders
                    + ") ORDER BY global_sequence",
                    _MODEL_STATUS_EVENT_VALUES,
                ).fetchall()
                for row in rows:
                    event = EventEnvelope.from_dict(json.loads(str(row[0])))
                    self._apply_model_status_event(connection, event)
                self._verify_model_status_connection(connection)
                return self.model_status_digest(connection)

    def capture_projection_checkpoint(self, *, captured_at: str = _PENDING_AT) -> str:
        """Persist a full projection snapshot for a subsequently signed registry anchor."""

        require_utc_milliseconds(captured_at)
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                projection_digest = self.projection_digest(connection)
                head = connection.execute(
                    "SELECT global_sequence,event_digest FROM v3_events "
                    "ORDER BY global_sequence DESC LIMIT 1"
                ).fetchone()
                authority_sequence = 0 if head is None else int(head[0])
                authority_digest = ZERO_DIGEST if head is None else str(head[1])
                tables = self._projection_snapshot_material(connection)
                snapshot = {
                    "schema_version": "strathmark-v3-projection-restore-snapshot-v1",
                    "authority_sequence": authority_sequence,
                    "authority_digest": authority_digest,
                    "projection_digest": projection_digest,
                    "tables": tables,
                }
                encoded = canonical_bytes(
                    snapshot, max_bytes=16_777_216, max_items=2_000_000
                ).decode("utf-8")
                snapshot_digest = canonical_digest(
                    snapshot, max_bytes=16_777_216, max_items=2_000_000
                )
                material = (
                    projection_digest,
                    authority_sequence,
                    authority_digest,
                    encoded,
                    snapshot_digest,
                    captured_at,
                )
                existing = connection.execute(
                    "SELECT projection_digest,authority_sequence,authority_digest,"
                    "snapshot_json,snapshot_digest,captured_at "
                    "FROM v3_projection_restore_snapshots WHERE projection_digest=?",
                    (projection_digest,),
                ).fetchone()
                if existing is not None and tuple(existing) != material:
                    raise ProjectionConflict("projection restore snapshot conflicts")
                if existing is None:
                    connection.execute(
                        "INSERT INTO v3_projection_restore_snapshots VALUES (?,?,?,?,?,?)",
                        material,
                    )
                return projection_digest

    def rebuild_from_checkpoint_registry(self, checkpoint_registry: object) -> str:
        """Restore a signed projection checkpoint, then replay only its event suffix."""

        from strathmark.v3.infrastructure.integrity import CheckpointRegistry

        if not isinstance(checkpoint_registry, CheckpointRegistry):
            raise ProjectionError("trusted rebuild requires a CheckpointRegistry")
        checkpoint = checkpoint_registry.verify_database(self._database_path, require_current=False)
        with open_v3_connection(self._database_path) as connection:
            row = connection.execute(
                "SELECT authority_sequence,authority_digest,snapshot_json,snapshot_digest "
                "FROM v3_projection_restore_snapshots WHERE projection_digest=?",
                (checkpoint.projection_digest,),
            ).fetchone()
            if row is None:
                raise ProjectionError("signed projection checkpoint has no restore snapshot")
            try:
                snapshot = json.loads(str(row[2]))
                encoded = canonical_bytes(
                    snapshot, max_bytes=16_777_216, max_items=2_000_000
                ).decode("utf-8")
            except Exception as exc:
                raise ProjectionError("projection restore snapshot is not canonical") from exc
            if encoded != str(row[2]) or canonical_digest(
                snapshot, max_bytes=16_777_216, max_items=2_000_000
            ) != str(row[3]):
                raise ProjectionError("projection restore snapshot digest differs")
            if not isinstance(snapshot, dict) or (
                snapshot.get("schema_version") != "strathmark-v3-projection-restore-snapshot-v1"
            ):
                raise ProjectionError("projection restore snapshot is malformed")
            expected_anchor = (checkpoint.authority_sequence, checkpoint.authority_digest)
            if (
                int(row[0]),
                str(row[1]),
            ) != expected_anchor or (
                snapshot.get("authority_sequence"),
                snapshot.get("authority_digest"),
                snapshot.get("projection_digest"),
            ) != (*expected_anchor, checkpoint.projection_digest):
                raise ProjectionError("projection restore snapshot authority binding differs")
            with immediate_transaction(connection):
                connection.execute("PRAGMA defer_foreign_keys=ON")
                self._restore_projection_snapshot(connection, snapshot)
                if self.projection_digest(connection) != checkpoint.projection_digest:
                    raise ProjectionError("restored projection differs from signed checkpoint")
                rows = connection.execute(
                    "SELECT envelope_json FROM v3_events WHERE global_sequence>? "
                    "ORDER BY global_sequence",
                    (checkpoint.authority_sequence,),
                ).fetchall()
                events = tuple(EventEnvelope.from_dict(json.loads(str(item[0]))) for item in rows)
                for _command_id, grouped in groupby(
                    events, key=lambda item: str(item.command.command_id)
                ):
                    self.apply_events(connection, tuple(grouped))
                self._advance_barrier(connection)
                rebuilt = self.projection_digest(connection)
        checkpoint_registry.verify_database(self._database_path, require_current=False)
        return rebuilt

    def rebuild_rolling_reaction_obligations(self) -> int:
        """Restore missing rolling outbox obligations from verified command event sets."""

        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        SQLiteEventStore(self._database_path).verify()
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                before = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM v3_rolling_reaction_obligations"
                    ).fetchone()[0]
                )
                events = tuple(
                    EventEnvelope.from_dict(json.loads(str(row[0])))
                    for row in connection.execute(
                        "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
                    )
                )
                reset_rolling_reaction_cursor(connection)
                for _command_id, grouped in groupby(
                    events, key=lambda item: str(item.command.command_id)
                ):
                    grouped_events = tuple(grouped)
                    self._register_rolling_reaction(connection, grouped_events)
                    advance_rolling_reaction_cursor(connection, grouped_events)
                after = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM v3_rolling_reaction_obligations"
                    ).fetchone()[0]
                )
        return after - before

    def apply_events(
        self,
        connection: sqlite3.Connection,
        events: tuple[EventEnvelope, ...],
        *,
        _rebuild_approval: bool = True,
    ) -> None:
        """Apply newly authoritative events inside the event-store transaction."""

        if not connection.in_transaction:
            raise ProjectionError("projection application requires the event writer transaction")
        if not events:
            raise ProjectionError("projection application requires authoritative events")
        command_ids = {str(event.command.command_id) for event in events}
        if len(command_ids) != 1:
            raise ProjectionError("one projection batch must contain exactly one command")
        if _rebuild_approval and any(
            not isinstance(event.command.payload, InlinePayload) for event in events
        ):
            raise ProjectionError("U5 authority events require bounded inline payloads")
        self._validate_atomic_event_set(connection, events)
        self._register_rolling_reaction(connection, events)
        for event in events:
            payload = cast(InlinePayload, event.command.payload)
            value = payload.to_value()
            if value.get("schema_version") == "strathmark-v3-correction-settlement-v1":
                nested_key = (
                    "result"
                    if event.kind is EventKind.RESULT_SUPERSEDED
                    else ("settlement" if event.kind is EventKind.LIVE_RACE_SETTLED else None)
                )
                if nested_key is not None:
                    nested = value.get(nested_key)
                    if not isinstance(nested, dict):
                        raise ProjectionError("atomic correction payload is malformed")
                    value = nested
            elif value.get("schema_version") == "strathmark-v3-record-and-settle-live-race-v1":
                if event.kind in {EventKind.RESULT_RECORDED, EventKind.RESULT_SUPERSEDED}:
                    candidates = value.get("result_submissions")
                    if not isinstance(candidates, list):
                        raise ProjectionError("atomic settlement result payloads are malformed")
                    matched = [
                        item
                        for item in candidates
                        if isinstance(item, dict)
                        and item.get("result_key") == str(event.aggregate_id)
                    ]
                    if len(matched) != 1:
                        raise ProjectionError("atomic settlement result payload is missing")
                    value = matched[0]
                elif event.kind is EventKind.LIVE_RACE_SETTLED:
                    nested = value.get("settlement")
                    if not isinstance(nested, dict):
                        raise ProjectionError("atomic settlement payload is malformed")
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
            if event.kind in _MODEL_STATUS_EVENTS:
                self._apply_model_status_event(connection, event)
        self._advance_barrier(connection)
        if any(
            event.kind
            in {
                EventKind.FIELD_ROSTER_REVISED,
                EventKind.FIELD_OPTIMIZED,
                EventKind.FIELD_REGENERATED,
                EventKind.FIELD_SUPERSEDED,
                EventKind.FIELD_ISSUED,
                EventKind.TOURNAMENT_OPENED,
                EventKind.TOURNAMENT_CLOSED,
            }
            for event in events
        ):
            _rebuild_approval_projection_connection(
                connection, rebuilt_at=events[-1].occurred_at_utc
            )

    @staticmethod
    def _register_rolling_reaction(
        connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]
    ) -> str | None:
        if not any(event.kind in _ROLLING_REACTION_EVENTS for event in events):
            return None
        event_refs = [
            {
                "event_id": str(event.event_id),
                "event_digest": event.event_digest,
                "global_sequence": event.global_sequence,
            }
            for event in events
        ]
        event_set_digest = canonical_digest(
            {
                "schema_version": "strathmark-v3-rolling-reaction-event-set-v1",
                "events": event_refs,
            }
        )
        reaction_id = canonical_digest(
            {
                "source_command_id": str(events[0].command.command_id),
                "event_set_digest": event_set_digest,
            }
        )
        material = (
            reaction_id,
            str(events[0].command.command_id),
            events[0].global_sequence,
            events[-1].global_sequence,
            canonical_bytes(event_refs).decode("utf-8"),
            event_set_digest,
            events[-1].occurred_at_utc,
        )
        existing = connection.execute(
            "SELECT reaction_id,source_command_id,first_global_sequence,"
            "last_global_sequence,event_ids_json,event_set_digest,registered_at "
            "FROM v3_rolling_reaction_obligations WHERE reaction_id=?",
            (reaction_id,),
        ).fetchone()
        if existing is not None and tuple(existing) != material:
            raise ProjectionConflict("rolling reaction obligation conflicts")
        if existing is None:
            connection.execute(
                "INSERT INTO v3_rolling_reaction_obligations VALUES (?,?,?,?,?,?,?)",
                material,
            )
        return reaction_id

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
            results = [
                event
                for event in events
                if event.kind in {EventKind.RESULT_RECORDED, EventKind.RESULT_SUPERSEDED}
            ]
            settlements = [event for event in events if event.kind is EventKind.LIVE_RACE_SETTLED]
            fields = [event for event in events if event.kind is EventKind.FIELD_SETTLED]
            if len(settlements) != 1 or len(fields) != 1:
                raise ProjectionConflict("live settlement must atomically settle its field")
            payload = cast(InlinePayload, settlements[0].command.payload)
            payload_value = payload.to_value()
            if payload_value.get("field_id") != str(fields[0].aggregate_id):
                raise ProjectionConflict("live settlement field authority does not match")
            if payload_value.get("schema_version") == (
                "strathmark-v3-record-and-settle-live-race-v1"
            ):
                nested_results = payload_value.get("result_submissions")
                settlement = payload_value.get("settlement")
                if (
                    not isinstance(nested_results, list)
                    or not isinstance(settlement, dict)
                    or {item.get("result_key") for item in nested_results if isinstance(item, dict)}
                    != {str(event.aggregate_id) for event in results}
                    or len(nested_results) != len(results)
                    or settlement.get("field_id") != str(fields[0].aggregate_id)
                    or settlement.get("results")
                    != [
                        {
                            "result_key": item.get("result_key"),
                            "revision": item.get("submission", {}).get("result", {}).get("revision")
                            if isinstance(item.get("submission"), dict)
                            else None,
                            "competitor_id": item.get("submission", {}).get("competitor_id")
                            if isinstance(item.get("submission"), dict)
                            else None,
                        }
                        for item in nested_results
                        if isinstance(item, dict)
                    ]
                ):
                    raise ProjectionConflict("atomic settlement result authority differs")
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
        if value["deferred_reactions"] != [
            "cancel_jobs",
            "expire_overlay",
            "seal_exports",
        ]:
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
                (
                    value["round_id"],
                    EventKind.ROUND_FROZEN.value,
                    event.global_sequence,
                ),
            ).fetchone()
            if round_frozen is not None:
                raise ProjectionConflict("round lineage is pinned by its first frozen epoch")
        if value["entity_kind"] == "field":
            issued = connection.execute(
                "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "AND global_sequence<?",
                (
                    value["entity_id"],
                    EventKind.FIELD_ISSUED.value,
                    event.global_sequence,
                ),
            ).fetchone()
            if issued is not None:
                raise ProjectionConflict(
                    "an issued legal field is immutable; create a new field for reissue"
                )
        snapshot = value["snapshot"]
        if not isinstance(snapshot, dict) or canonical_digest(snapshot) != value["snapshot_digest"]:
            raise ProjectionError("upstream snapshot digest mismatch")
        SQLiteProjectionStore._validate_snapshot_contract(event, value, snapshot)
        if value["entity_kind"] == "field" and "call_order" in snapshot:
            capacity_row = connection.execute(
                "SELECT capacity_manifest_json FROM v3_field_capacity_authorities "
                "WHERE authority_digest=?",
                (snapshot["capacity_authority_digest"],),
            ).fetchone()
            if capacity_row is None:
                raise ProjectionConflict("field snapshot capacity authority is not installed")
            try:
                from strathmark.v3.application.capacity import CapacityManifest

                capacity = CapacityManifest.from_dict(json.loads(str(capacity_row[0])))
            except (ContractError, TypeError, ValueError) as exc:
                raise ProjectionConflict("field snapshot capacity authority is corrupt") from exc
            if (
                snapshot["max_field_entrants"] != capacity.max_field_entrants
                or len(snapshot["competitor_ids"]) > capacity.max_field_entrants
            ):
                raise ProjectionConflict("field snapshot differs from installed capacity authority")
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
        self,
        connection: sqlite3.Connection,
        event: EventEnvelope,
        value: dict[str, object],
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
            connection,
            normalized_contract.field_id,
            before_sequence=event.global_sequence,
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
        scheduled_fields = {
            "competitor_ids",
            "target_context",
            "stand_ids",
            "capacity_authority_digest",
            "max_field_entrants",
            "call_order",
            "scheduled_at",
            "deadline_at",
        }
        legacy_fields = {"competitor_ids", "target_context", "stand_ids"}
        if set(snapshot) not in (scheduled_fields, legacy_fields):
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
        if "call_order" in snapshot:
            _digest(snapshot["capacity_authority_digest"])
            max_field_entrants = snapshot["max_field_entrants"]
            if (
                isinstance(max_field_entrants, bool)
                or not isinstance(max_field_entrants, int)
                or max_field_entrants <= 0
                or len(competitors) > max_field_entrants
            ):
                raise ProjectionConflict("field roster exceeds its declared capacity authority")
            call_order = snapshot["call_order"]
            if isinstance(call_order, bool) or not isinstance(call_order, int) or call_order < 0:
                raise ProjectionConflict("field call order must be nonnegative")
            require_utc_milliseconds(snapshot["scheduled_at"])
            require_utc_milliseconds(snapshot["deadline_at"])
            if snapshot["deadline_at"] <= snapshot["scheduled_at"]:
                raise ProjectionConflict("field deadline must follow its scheduled instant")

    def _apply_derivation_reaction(
        self,
        connection: sqlite3.Connection,
        event: EventEnvelope,
        value: dict[str, object],
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
                    if prior is None or (
                        member["revision"],
                        member["source_sequence"],
                    ) > (
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
                [
                    boundary,
                    *(int(member["source_sequence"]) for member in expected_members),
                ]
            )
        else:
            opened = connection.execute(
                "SELECT global_sequence FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "AND global_sequence<? ORDER BY global_sequence DESC LIMIT 1",
                (
                    str(ingress[0]),
                    EventKind.TOURNAMENT_OPENED.value,
                    event.global_sequence,
                ),
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
        if set(value) != {
            "schema_version",
            "field_id",
            "field_revision",
            "receipt_id",
            "results",
        }:
            raise ProjectionError("live settlement payload is not closed")
        expected_settlement_id = deterministic_identifier(
            "settlement",
            {key: item for key, item in value.items() if key != "schema_version"},
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
        if issued.get("schema_version") == "strathmark-v3-batch-issue-authority-v1":
            fields = issued.get("fields")
            if not isinstance(fields, list):
                raise ProjectionConflict("batch issue fields are invalid")
            matches = [
                item
                for item in fields
                if isinstance(item, dict) and item.get("field_id") == value["field_id"]
            ]
            if len(matches) != 1:
                raise ProjectionConflict("batch issue does not bind the settled field")
            issued = matches[0]
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
        if value.get("schema_version") == "strathmark-v3-batch-issue-authority-v1":
            fields = value.get("fields")
            if not isinstance(fields, list):
                raise ProjectionConflict("batch issue fields are invalid")
            matches = [
                item
                for item in fields
                if isinstance(item, dict) and item.get("field_id") == str(field_id)
            ]
            if len(matches) != 1:
                raise ProjectionConflict("batch issue does not bind the settled field")
            value = matches[0]
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
        if value.get("schema_version") == "strathmark-v3-batch-issue-authority-v1":
            fields = value.get("fields")
            if not isinstance(fields, list):
                raise ProjectionError("batch issue fields are invalid")
            matches = [
                item
                for item in fields
                if isinstance(item, dict) and item.get("field_id") == str(event.aggregate_id)
            ]
            if len(matches) != 1:
                raise ProjectionConflict("batch issue must bind each field exactly once")
            value = matches[0]
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
            "SELECT epoch_id FROM v3_round_issue_seals WHERE round_id=?",
            (value["round_id"],),
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
            (
                value["round_id"],
                value["epoch_id"],
                event.global_sequence,
                event.occurred_at_utc,
            ),
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

    def _ensure_model_status_projection(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT singleton FROM v3_model_status WHERE singleton=1"
        ).fetchone()
        if row is not None:
            self._verify_model_status_connection(connection)
            return
        self._write_model_status(connection, ZERO_DIGEST, None, 0, ZERO_DIGEST)
        placeholders = ",".join("?" for _ in _MODEL_STATUS_EVENT_VALUES)
        rows = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE event_kind IN ("
            + placeholders
            + ") ORDER BY global_sequence",
            _MODEL_STATUS_EVENT_VALUES,
        ).fetchall()
        for event_row in rows:
            self._apply_model_status_event(
                connection, EventEnvelope.from_dict(json.loads(str(event_row[0])))
            )

    @staticmethod
    def _write_model_status(
        connection: sqlite3.Connection,
        active_bundle_digest: str,
        active_candidate_id: str | None,
        source_global_sequence: int,
        source_event_digest: str,
    ) -> None:
        value = {
            "schema_version": "strathmark-v3-model-status-v1",
            "active_bundle_digest": active_bundle_digest,
            "active_candidate_id": active_candidate_id,
            "source_global_sequence": source_global_sequence,
            "source_event_digest": source_event_digest,
        }
        connection.execute(
            "INSERT INTO v3_model_status(singleton,active_bundle_digest,active_candidate_id,"
            "source_global_sequence,source_event_digest,checkpoint_digest) VALUES (1,?,?,?,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET active_bundle_digest=excluded.active_bundle_digest,"
            "active_candidate_id=excluded.active_candidate_id,"
            "source_global_sequence=excluded.source_global_sequence,"
            "source_event_digest=excluded.source_event_digest,"
            "checkpoint_digest=excluded.checkpoint_digest",
            (
                active_bundle_digest,
                active_candidate_id,
                source_global_sequence,
                source_event_digest,
                canonical_digest(value),
            ),
        )

    def _apply_model_status_event(
        self, connection: sqlite3.Connection, event: EventEnvelope
    ) -> None:
        if event.kind not in _MODEL_STATUS_EVENTS:
            return
        if not isinstance(event.command.payload, InlinePayload):
            raise ProjectionError("model status event requires an inline payload")
        value = event.command.payload.to_value()
        status = connection.execute(
            "SELECT active_bundle_digest,active_candidate_id FROM v3_model_status WHERE singleton=1"
        ).fetchone()
        if status is None:
            raise ProjectionError("model status checkpoint is missing")
        active_bundle = str(status[0])
        active_candidate = None if status[1] is None else str(status[1])
        if event.kind is EventKind.MODEL_CANDIDATE_CREATED:
            self._store_model_candidate(
                connection,
                str(event.aggregate_id),
                _require_projection_digest(value.get("candidate_digest"), "model candidate"),
                _require_projection_digest(value.get("lineage_digest"), "model candidate lineage"),
                None,
                None,
                None,
                event,
            )
        elif event.kind is EventKind.MODEL_CANDIDATE_EVALUATED:
            candidate_id = str(event.aggregate_id)
            row = connection.execute(
                "SELECT candidate_digest,lineage_digest,promoted_bundle_digest "
                "FROM v3_model_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise ProjectionError("model evaluation has no projected candidate")
            evaluation_json = canonical_bytes(value, max_bytes=MAX_INLINE_PAYLOAD_BYTES).decode(
                "utf-8"
            )
            self._store_model_candidate(
                connection,
                candidate_id,
                str(row[0]),
                str(row[1]),
                evaluation_json,
                canonical_digest(value, max_bytes=MAX_INLINE_PAYLOAD_BYTES),
                None if row[2] is None else str(row[2]),
                event,
            )
        elif event.kind is EventKind.BUNDLE_PROMOTED:
            candidate_id = str(event.aggregate_id)
            row = connection.execute(
                "SELECT candidate_digest,lineage_digest,evaluation_json,evaluation_digest "
                "FROM v3_model_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None or row[2] is None:
                raise ProjectionError("model promotion has no evaluated candidate projection")
            parent = _require_projection_digest(
                value.get("rollback_parent_digest"), "model promotion parent"
            )
            if parent != active_bundle:
                raise ProjectionConflict("model promotion does not descend from active champion")
            promoted = _require_projection_digest(
                value.get("bundle_digest"), "model promoted bundle"
            )
            self._store_model_candidate(
                connection,
                candidate_id,
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                promoted,
                event,
            )
            active_bundle = promoted
            active_candidate = candidate_id
        elif event.kind is EventKind.BUNDLE_ROLLED_BACK:
            target = _require_projection_digest(value.get("bundle_digest"), "model rollback source")
            if target != active_bundle:
                raise ProjectionConflict("model rollback does not target active champion")
            active_bundle = _require_projection_digest(
                value.get("rollback_to_bundle_digest"), "model rollback target"
            )
            if active_bundle == ZERO_DIGEST:
                active_candidate = None
            else:
                candidate = connection.execute(
                    "SELECT candidate_id FROM v3_model_candidates WHERE promoted_bundle_digest=?",
                    (active_bundle,),
                ).fetchone()
                if candidate is None:
                    raise ProjectionError("model rollback target has no promotion projection")
                active_candidate = str(candidate[0])
        elif event.kind is EventKind.TOURNAMENT_OPENED:
            bundle_id = value.get("bundle_id")
            if not isinstance(bundle_id, str) or not bundle_id.startswith("bundle:"):
                raise ProjectionError("tournament open has an invalid model bundle pin")
            pin = {
                "schema_version": "strathmark-v3-model-tournament-pin-v1",
                "tournament_id": str(event.aggregate_id),
                "bundle_id": bundle_id,
                "source_global_sequence": event.global_sequence,
                "source_event_digest": event.event_digest,
            }
            connection.execute(
                "INSERT INTO v3_model_tournament_pins VALUES (?,?,?,?,?) "
                "ON CONFLICT(tournament_id) DO UPDATE SET bundle_id=excluded.bundle_id,"
                "source_global_sequence=excluded.source_global_sequence,"
                "source_event_digest=excluded.source_event_digest,row_digest=excluded.row_digest",
                (
                    str(event.aggregate_id),
                    bundle_id,
                    event.global_sequence,
                    event.event_digest,
                    canonical_digest(pin),
                ),
            )
        self._write_model_status(
            connection,
            active_bundle,
            active_candidate,
            event.global_sequence,
            event.event_digest,
        )

    @staticmethod
    def _store_model_candidate(
        connection: sqlite3.Connection,
        candidate_id: str,
        candidate_digest: str,
        lineage_digest: str,
        evaluation_json: str | None,
        evaluation_digest: str | None,
        promoted_bundle_digest: str | None,
        event: EventEnvelope,
    ) -> None:
        material = {
            "schema_version": "strathmark-v3-model-candidate-projection-v1",
            "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "lineage_digest": lineage_digest,
            "evaluation_json": evaluation_json,
            "evaluation_digest": evaluation_digest,
            "promoted_bundle_digest": promoted_bundle_digest,
            "source_global_sequence": event.global_sequence,
            "source_event_digest": event.event_digest,
        }
        connection.execute(
            "INSERT INTO v3_model_candidates VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET candidate_digest=excluded.candidate_digest,"
            "lineage_digest=excluded.lineage_digest,evaluation_json=excluded.evaluation_json,"
            "evaluation_digest=excluded.evaluation_digest,"
            "promoted_bundle_digest=excluded.promoted_bundle_digest,"
            "source_global_sequence=excluded.source_global_sequence,"
            "source_event_digest=excluded.source_event_digest,row_digest=excluded.row_digest",
            (
                candidate_id,
                candidate_digest,
                lineage_digest,
                evaluation_json,
                evaluation_digest,
                promoted_bundle_digest,
                event.global_sequence,
                event.event_digest,
                canonical_digest(material, max_bytes=MAX_INLINE_PAYLOAD_BYTES * 2),
            ),
        )

    def _verify_model_status_connection(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT singleton,active_bundle_digest,active_candidate_id,source_global_sequence,"
            "source_event_digest,checkpoint_digest FROM v3_model_status WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ProjectionError("model status checkpoint is missing")
        value = {
            "schema_version": "strathmark-v3-model-status-v1",
            "active_bundle_digest": str(row[1]),
            "active_candidate_id": None if row[2] is None else str(row[2]),
            "source_global_sequence": int(row[3]),
            "source_event_digest": str(row[4]),
        }
        if canonical_digest(value) != str(row[5]):
            raise ProjectionError("model status checkpoint digest differs")
        status_row = row
        candidates: list[sqlite3.Row] = []
        for kind in _MODEL_STATUS_EVENT_VALUES:
            candidate_row = connection.execute(
                "SELECT global_sequence,event_digest FROM v3_events "
                "WHERE event_kind=? ORDER BY global_sequence DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if candidate_row is not None:
                candidates.append(candidate_row)
        latest = max(candidates, key=lambda item: int(item[0]), default=None)
        expected = (0, ZERO_DIGEST) if latest is None else (int(latest[0]), str(latest[1]))
        if (int(status_row[3]), str(status_row[4])) != expected:
            raise ProjectionError("model status checkpoint is stale")
        if status_row[2] is not None:
            candidate = connection.execute(
                "SELECT promoted_bundle_digest FROM v3_model_candidates WHERE candidate_id=?",
                (str(status_row[2]),),
            ).fetchone()
            if candidate is None or str(candidate[0]) != str(status_row[1]):
                raise ProjectionError("model status active candidate binding differs")
        return status_row

    @staticmethod
    def _verify_projected_source(
        connection: sqlite3.Connection, sequence: int, event_digest: str
    ) -> None:
        row = connection.execute(
            "SELECT event_digest FROM v3_events WHERE global_sequence=?", (sequence,)
        ).fetchone()
        if row is None or str(row[0]) != event_digest:
            raise ProjectionError("model projection source authority differs")

    def _verify_model_candidate_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        material = {
            "schema_version": "strathmark-v3-model-candidate-projection-v1",
            "candidate_id": str(row[0]),
            "candidate_digest": str(row[1]),
            "lineage_digest": str(row[2]),
            "evaluation_json": None if row[3] is None else str(row[3]),
            "evaluation_digest": None if row[4] is None else str(row[4]),
            "promoted_bundle_digest": None if row[5] is None else str(row[5]),
            "source_global_sequence": int(row[6]),
            "source_event_digest": str(row[7]),
        }
        if canonical_digest(material, max_bytes=MAX_INLINE_PAYLOAD_BYTES * 2) != str(row[8]):
            raise ProjectionError("model candidate row digest differs")
        if row[3] is not None:
            try:
                evaluation = json.loads(str(row[3]))
            except Exception as exc:
                raise ProjectionError("model candidate evaluation is malformed") from exc
            if canonical_bytes(evaluation, max_bytes=MAX_INLINE_PAYLOAD_BYTES).decode(
                "utf-8"
            ) != str(row[3]) or canonical_digest(
                evaluation, max_bytes=MAX_INLINE_PAYLOAD_BYTES
            ) != str(row[4]):
                raise ProjectionError("model candidate evaluation digest differs")
        self._verify_projected_source(connection, int(row[6]), str(row[7]))

    @staticmethod
    def model_status_digest(connection: sqlite3.Connection) -> str:
        material: dict[str, list[list[object]]] = {}
        for table in (
            "v3_model_status",
            "v3_model_candidates",
            "v3_model_tournament_pins",
        ):
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            material[table] = [
                list(row) for row in sorted(rows, key=lambda item: tuple(str(x) for x in item))
            ]
        return canonical_digest(
            {"schema_version": "strathmark-v3-model-status-projection-v1", "tables": material},
            max_bytes=16_777_216,
            max_items=2_000_000,
        )

    @staticmethod
    def _projection_snapshot_material(
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, object]]:
        material: dict[str, dict[str, object]] = {}
        for table in _PROJECTION_TABLES:
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            material[table] = {
                "columns": columns,
                "rows": [
                    list(row) for row in sorted(rows, key=lambda item: tuple(str(x) for x in item))
                ],
            }
        return material

    @staticmethod
    def _restore_projection_snapshot(
        connection: sqlite3.Connection, snapshot: Mapping[str, Any]
    ) -> None:
        tables = snapshot.get("tables")
        if not isinstance(tables, dict) or set(tables) != set(_PROJECTION_TABLES):
            raise ProjectionError("projection restore snapshot table set differs")
        for table in reversed(_PROJECTION_TABLES):
            connection.execute(f"DELETE FROM {table}")  # noqa: S608
        for table in _PROJECTION_TABLES:
            value = tables[table]
            if not isinstance(value, dict):
                raise ProjectionError("projection restore snapshot table is malformed")
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
            if value.get("columns") != columns or not isinstance(value.get("rows"), list):
                raise ProjectionError("projection restore snapshot columns differ")
            placeholders = ",".join("?" for _ in columns)
            column_sql = ",".join(columns)
            for row in value["rows"]:
                if not isinstance(row, list) or len(row) != len(columns):
                    raise ProjectionError("projection restore snapshot row is malformed")
                connection.execute(
                    f"INSERT INTO {table}({column_sql}) VALUES ({placeholders})",  # noqa: S608
                    tuple(row),
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
        material: dict[str, list[list[object]]] = {}
        for table in _PROJECTION_TABLES:
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            material[table] = [
                list(row) for row in sorted(rows, key=lambda row: tuple(str(x) for x in row))
            ]
        return canonical_digest(
            {"schema_version": "strathmark-v3-u5-projections-v1", "tables": material},
            max_bytes=16_777_216,
            max_items=2_000_000,
        )


def _require_projection_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectionError(f"{label} digest is invalid")
    return value


def _default_field_blob_root(database_path: Path) -> Path:
    return database_path.parent / f"{database_path.name}.blobs"


def _field_blob_store_for_connection(
    connection: sqlite3.Connection,
) -> ContentAddressedBlobStore:
    rows = connection.execute("PRAGMA database_list").fetchall()
    main = next((row for row in rows if str(row[1]) == "main"), None)
    if main is None or not str(main[2]):
        raise ProjectionError("field projection database identity is unavailable")
    return ContentAddressedBlobStore(
        _default_field_blob_root(Path(str(main[2])).resolve(strict=False)),
        create=False,
    )


def _field_receipt_summary(receipt: Any) -> dict[str, Any]:
    """Bounded event/projection index for a content-addressed full receipt."""

    return {
        "schema_version": "strathmark-v3-field-receipt-summary-v1",
        "receipt_id": str(receipt.receipt_id),
        "caller_namespace": receipt.caller_namespace,
        "request_identity": str(receipt.request_identity),
        "field_id": str(receipt.field_id),
        "upstream_field_revision": receipt.upstream_field_revision,
        "receipt_revision": receipt.receipt_revision,
        "supersedes_receipt_id": (
            None if receipt.supersedes_receipt_id is None else str(receipt.supersedes_receipt_id)
        ),
        "ordered_competitor_ids": [str(item) for item in receipt.ordered_competitor_ids],
        "target_context": receipt.target_context.to_dict(),
        "target_context_digest": receipt.target_context_digest,
        "tournament_epoch_id": str(receipt.tournament_epoch_id),
        "tournament_event_sequence": receipt.tournament_event_sequence,
        "marks": [item.to_dict() for item in receipt.marks],
        "warning_codes": list(receipt.warning_codes),
        "bundles": [item.to_dict() for item in receipt.bundles],
        "content_digest": receipt.content_digest,
    }


def _disagreement_blob_projection(
    authority: Any,
    reference: BlobReferenceV2,
    *,
    bundle_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": _DISAGREEMENT_BLOB_PROJECTION_SCHEMA,
        "receipt_digest": authority.receipt_digest,
        "field_revision_digest": authority.field_revision_digest,
        "bundle_digest": bundle_digest,
        "policy_manifest_digest": authority.policy_manifest.body_digest,
        "council_manifest_digest": (
            None if authority.council_manifest is None else authority.council_manifest.body_digest
        ),
        "authority_blob_reference": reference.to_dict(),
    }


def _field_receipt_projection(receipt: Any, reference: BlobReferenceV2) -> dict[str, Any]:
    return {
        "schema_version": _FIELD_RECEIPT_PROJECTION_SCHEMA,
        "receipt_summary": _field_receipt_summary(receipt),
        "receipt_blob_reference": reference.to_dict(),
    }


def _decode_field_receipt_projection(
    value: Mapping[str, Any], blob_store: ContentAddressedBlobStore
) -> Any:
    from strathmark.v3.contracts.receipts import FieldReceipt

    if value.get("schema_version") != _FIELD_RECEIPT_PROJECTION_SCHEMA:
        return FieldReceipt.from_dict(value)
    if set(value) != {
        "schema_version",
        "receipt_summary",
        "receipt_blob_reference",
    }:
        raise ProjectionError("field receipt projection fields differ")
    try:
        reference = BlobReferenceV2.from_dict(value["receipt_blob_reference"])
        receipt_value = json.loads(blob_store.read(reference))
        receipt = FieldReceipt.from_dict(receipt_value)
    except Exception as exc:
        raise ProjectionError("required field receipt blob is missing or corrupt") from exc
    if value["receipt_summary"] != _field_receipt_summary(receipt):
        raise ProjectionError("field receipt projection summary differs from blob")
    return receipt


def _receipt_blob_signature(
    value: Mapping[str, Any], blob_store: ContentAddressedBlobStore
) -> tuple[int, int, int, int, int, int] | None:
    """Fingerprint immutable receipt files before reusing a verified decode."""

    if value.get("schema_version") != _FIELD_RECEIPT_PROJECTION_SCHEMA:
        return None
    try:
        reference = BlobReferenceV2.from_dict(value["receipt_blob_reference"])
        content = blob_store.path_for(reference.digest).stat()
        metadata = blob_store._metadata_path(reference).stat()
    except Exception as exc:
        raise ProjectionError("required field receipt blob is missing or corrupt") from exc
    return (
        content.st_size,
        content.st_mtime_ns,
        content.st_ctime_ns,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _field_receipt_event_matches(
    payload: Mapping[str, Any], receipt: Any, projection: Mapping[str, Any]
) -> bool:
    if "receipt" in payload:
        return payload.get("receipt") == receipt.to_dict()
    return bool(
        payload.get("receipt_summary") == _field_receipt_summary(receipt)
        and payload.get("receipt_blob_reference") == projection.get("receipt_blob_reference")
    )


class SQLiteFieldProjectionStore:
    """Verified U15 authority installs and disposable current-field reads."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        signer: Any,
        trust_store: Any,
        blob_store: ContentAddressedBlobStore | None = None,
    ) -> None:
        from strathmark.v3.application.approval import _VERIFIED_RECEIPT_AUTHORITY
        from strathmark.v3.infrastructure.integrity import IntegrityTrustStore

        if isinstance(database_path, bool) or not isinstance(database_path, (Path, str)):
            raise ProjectionError("field projection database path is invalid")
        if not callable(getattr(signer, "sign", None)) or not hasattr(signer, "identity"):
            raise ProjectionError("field projection requires an external signer")
        if not isinstance(trust_store, IntegrityTrustStore):
            raise ProjectionError("field projection requires an integrity trust store")
        trust_store.identity(signer.identity.key_id)
        self._database_path = Path(database_path).expanduser().resolve(strict=False)
        if blob_store is not None and not isinstance(blob_store, ContentAddressedBlobStore):
            raise ProjectionError("field projection blob store is invalid")
        self._blob_store = blob_store or ContentAddressedBlobStore(
            _default_field_blob_root(self._database_path)
        )
        self._signer = signer
        self._trust_store = trust_store
        self._approval_authority_token = _VERIFIED_RECEIPT_AUTHORITY
        self._verified_receipt_cache: dict[str, _VerifiedReceiptCacheEntry] = {}
        self._verified_exact_retry_cache: dict[tuple[str, str], _VerifiedExactRetryCacheEntry] = {}
        with open_v3_connection(self._database_path) as connection:
            migrate_connection(connection)
        self.verify()
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                _refresh_projection_deep_checkpoints(connection)
        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        self._events = SQLiteEventStore(self._database_path)

    @property
    def database_path(self) -> Path:
        return self._database_path

    def install_capacity_authority(self, authority: Any, *, installed_at: str) -> None:
        from strathmark.v3.application.field_assembly import FieldCapacityAuthority
        from strathmark.v3.infrastructure.integrity import verify_manifest

        if not isinstance(authority, FieldCapacityAuthority):
            raise ProjectionConflict("capacity installation requires typed authority")
        require_utc_milliseconds(installed_at)
        verify_manifest(authority.manifest, self._trust_store)
        encoded_capacity = canonical_bytes(authority.capacity.to_dict()).decode()
        encoded_manifest = canonical_bytes(authority.manifest.to_dict()).decode()
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                existing = connection.execute(
                    "SELECT bundle_digest, capacity_manifest_json, signed_manifest_json "
                    "FROM v3_field_capacity_authorities WHERE authority_digest=?",
                    (authority.authority_digest,),
                ).fetchone()
                material = (
                    authority.bundle_digest,
                    encoded_capacity,
                    encoded_manifest,
                )
                if existing is not None:
                    if tuple(existing) != material:
                        raise ProjectionConflict(
                            "capacity authority digest conflicts with installed material"
                        )
                    return
                bundle = connection.execute(
                    "SELECT authority_digest FROM v3_field_capacity_authorities "
                    "WHERE bundle_digest=?",
                    (authority.bundle_digest,),
                ).fetchone()
                if bundle is not None:
                    raise ProjectionConflict("bundle already has a different capacity authority")
                connection.execute(
                    "INSERT INTO v3_field_capacity_authorities VALUES (?, ?, ?, ?, ?)",
                    (
                        authority.authority_digest,
                        authority.bundle_digest,
                        encoded_capacity,
                        encoded_manifest,
                        installed_at,
                    ),
                )

    def verify_capacity_authority(
        self,
        authority_digest: str,
        *,
        bundle_digest: str,
        entrant_count: int,
        declared_max_field_entrants: int,
        _connection: sqlite3.Connection | None = None,
    ) -> Any:
        from strathmark.v3.application.capacity import CapacityManifest
        from strathmark.v3.application.field_assembly import FieldCapacityAuthority
        from strathmark.v3.infrastructure.integrity import (
            SignedManifest,
            verify_manifest,
        )

        _digest(authority_digest)
        _digest(bundle_digest)
        if (
            isinstance(entrant_count, bool)
            or not isinstance(entrant_count, int)
            or entrant_count <= 0
        ):
            raise ProjectionConflict("field entrant count must be positive")
        context = (
            open_v3_connection(self._database_path, read_only=True)
            if _connection is None
            else nullcontext(_connection)
        )
        with context as connection:
            row = connection.execute(
                "SELECT bundle_digest, capacity_manifest_json, signed_manifest_json "
                "FROM v3_field_capacity_authorities WHERE authority_digest=?",
                (authority_digest,),
            ).fetchone()
            if row is None:
                raise ProjectionConflict("field capacity authority is not installed")
            try:
                capacity = CapacityManifest.from_dict(json.loads(str(row[1])))
                manifest = SignedManifest.from_dict(json.loads(str(row[2])))
                verify_manifest(manifest, self._trust_store)
                authority = FieldCapacityAuthority(
                    capacity, str(row[0]), manifest, authority_digest
                )
            except Exception as exc:
                raise ProjectionConflict("field capacity authority is corrupt") from exc
            if authority.bundle_digest != bundle_digest:
                raise ProjectionConflict("field capacity authority bundle differs")
            if declared_max_field_entrants != authority.capacity.max_field_entrants:
                raise ProjectionConflict("field declared capacity differs from installed authority")
            if entrant_count > authority.capacity.max_field_entrants:
                raise ProjectionConflict("field exceeds installed entrant capacity")
            return authority

    def install_weight_authority(self, binding: Any, *, installed_at: str) -> None:
        from strathmark.v3.application.field_assembly import OperationalWeightAuthority

        if not isinstance(binding, OperationalWeightAuthority):
            raise ProjectionConflict(
                "weight installation requires typed operational weight authority from U12"
            )
        value = binding.to_dict()
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                authority = self._find_weight_event(connection, binding)
                connection.execute(
                    "INSERT INTO v3_field_weight_authorities VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(binding_digest) DO NOTHING",
                    (
                        binding.authority_digest,
                        canonical_bytes(value).decode(),
                        canonical_bytes(authority.to_dict()).decode(),
                        installed_at,
                    ),
                )
                self._verify_weight_connection(connection, binding)

    def install_dependence_authority(
        self, artifact: Any, *, promotion_manifest: Any, installed_at: str
    ) -> None:
        from strathmark.v3.domain.joint_dependence import DependenceArtifact
        from strathmark.v3.infrastructure.integrity import (
            SignedManifest,
            verify_manifest,
        )

        if not isinstance(artifact, DependenceArtifact) or not isinstance(
            promotion_manifest, SignedManifest
        ):
            raise ProjectionError("dependence installation requires a typed artifact")
        value = artifact.to_dict()
        authority = verify_manifest(promotion_manifest, self._trust_store)
        if authority != {
            "schema_version": "strathmark-v3-field-dependence-promotion-v1",
            "purpose": "field_dependence_operational",
            "artifact": value,
            "promotion_receipt_digest": artifact.promotion_receipt_digest,
        }:
            raise ProjectionConflict("dependence promotion manifest differs from artifact")
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "INSERT INTO v3_field_dependence_authorities VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(artifact_digest) DO NOTHING",
                    (
                        artifact.artifact_digest,
                        canonical_bytes(value).decode(),
                        canonical_bytes(promotion_manifest.to_dict()).decode(),
                        installed_at,
                    ),
                )
                self._verify_dependence_connection(connection, artifact)

    def verify_weight_authority(self, binding: Any) -> None:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify_weight_connection(connection, binding)

    def verify_card_authority(self, card: Any) -> None:
        self._verify_card_authority(card)

    def _verify_card_authority(self, card: Any) -> None:
        from strathmark.v3.application.field_assembly import CompetitorCardAuthority
        from strathmark.v3.infrastructure.integrity import (
            IntegrityError,
            verify_manifest,
        )

        if not isinstance(card, CompetitorCardAuthority):
            raise ProjectionConflict("typed competitor card authority is required")
        try:
            verified = verify_manifest(card.manifest, self._trust_store)
        except IntegrityError as exc:
            raise ProjectionConflict("competitor card authority is untrusted") from exc
        if verified != card.content_value():
            raise ProjectionConflict("competitor card manifest differs from card authority")

    def verify_disagreement_authority(
        self, authority: Any, *, field: Any, cards: tuple[Any, ...]
    ) -> None:
        from strathmark.v3.application.field_assembly import (
            CompetitorCardAuthority,
            FrozenFieldRevision,
            OperationalDisagreementReceipt,
        )
        from strathmark.v3.infrastructure.integrity import verify_manifest

        if authority is None:
            return
        if (
            not isinstance(authority, OperationalDisagreementReceipt)
            or not isinstance(field, FrozenFieldRevision)
            or not all(isinstance(item, CompetitorCardAuthority) for item in cards)
        ):
            raise ProjectionConflict("typed operational disagreement authority is required")
        policy = verify_manifest(authority.policy_manifest, self._trust_store)
        if policy != {
            "schema_version": "strathmark-v3-field-disagreement-policy-authority-v1",
            "purpose": "field_disagreement_operational",
            "bundle_digest": field.bundle_digest,
            "policy": authority.decision.policy.to_dict(),
            "policy_digest": authority.decision.policy_digest,
        }:
            raise ProjectionConflict("disagreement policy manifest differs from field bundle")
        if authority.decision.council_audit is None:
            if authority.council_manifest is not None:
                raise ProjectionConflict("unavailable council has audit authority")
            return
        if authority.council_manifest is None:
            raise ProjectionConflict("operational council audit manifest is absent")
        council = verify_manifest(authority.council_manifest, self._trust_store)
        expected_cards = [item.manifest.body_digest for item in cards]
        if council != {
            "schema_version": "strathmark-v3-field-council-audit-authority-v1",
            "purpose": "field_council_operational",
            "field_revision_digest": field.revision_digest,
            "card_manifest_digests": expected_cards,
            "council_audit": authority.decision.council_audit.to_dict(),
            "council_audit_digest": authority.decision.council_audit.audit_digest,
        }:
            raise ProjectionConflict("council audit manifest differs from signed cards")

    def resolve_disagreement_authority(self, receipt_digest: str) -> Any:
        _digest(receipt_digest)
        with open_v3_connection(self._database_path, read_only=True) as connection:
            return self._resolve_disagreement_connection(connection, receipt_digest)

    def verify_dependence_authority(self, artifact: Any) -> None:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify_dependence_connection(connection, artifact)

    @staticmethod
    def _verify_u5_source_event(
        connection: sqlite3.Connection,
        *,
        source_global_sequence: int,
        event_kind: EventKind,
        aggregate_kind: AggregateKind,
        aggregate_id: str,
    ) -> EventEnvelope:
        """Authenticate one U5 projection row to its exact command/event authority."""

        row = connection.execute(
            "SELECT * FROM v3_events WHERE global_sequence=?",
            (source_global_sequence,),
        ).fetchone()
        if row is None:
            raise ProjectionConflict("U5 projection source event is absent")
        try:
            event = EventEnvelope.from_dict(json.loads(str(row["envelope_json"])))
        except (ContractError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProjectionConflict("U5 projection source event is corrupt") from exc
        if (
            not _event_row_matches(row, event)
            or event.kind is not event_kind
            or event.aggregate_kind is not aggregate_kind
            or str(event.aggregate_id) != aggregate_id
        ):
            raise ProjectionConflict("U5 projection source event differs")

        idempotency = connection.execute(
            "SELECT command_digest,result_json,result_digest,first_global_sequence,"
            "last_global_sequence,event_set_digest FROM v3_idempotency_records "
            "WHERE principal_id=? AND idempotency_key=?",
            (str(event.command.actor_id), str(event.command.command_id)),
        ).fetchone()
        if idempotency is None:
            raise ProjectionConflict("U5 source idempotency authority is absent")
        first_sequence = int(idempotency[3])
        last_sequence = int(idempotency[4])
        command_events = connection.execute(
            "SELECT global_sequence,event_id,event_digest,command_id FROM v3_events "
            "WHERE global_sequence BETWEEN ? AND ? ORDER BY global_sequence",
            (first_sequence, last_sequence),
        ).fetchall()
        try:
            result_value = json.loads(str(idempotency[1]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProjectionConflict("U5 source idempotency result is corrupt") from exc
        expected_event_set = canonical_digest(
            {
                "schema_version": "strathmark-v3-event-set-v1",
                "events": [
                    {
                        "global_sequence": int(item[0]),
                        "event_id": str(item[1]),
                        "event_digest": str(item[2]),
                    }
                    for item in command_events
                ],
            }
        )
        if (
            not (first_sequence <= source_global_sequence <= last_sequence)
            or len(command_events) != last_sequence - first_sequence + 1
            or any(str(item[3]) != str(event.command.command_id) for item in command_events)
            or str(idempotency[0]) != canonical_digest(event.command.to_dict())
            or canonical_bytes(result_value).decode() != str(idempotency[1])
            or str(idempotency[2]) != canonical_digest(result_value)
            or str(idempotency[5]) != expected_event_set
        ):
            raise ProjectionConflict("U5 source idempotency authority differs")

        head = connection.execute(
            "SELECT aggregate_version,event_digest FROM v3_aggregate_heads "
            "WHERE aggregate_kind=? AND aggregate_id=?",
            (aggregate_kind.value, aggregate_id),
        ).fetchone()
        successor_row = connection.execute(
            "SELECT * FROM v3_events WHERE aggregate_kind=? "
            "AND aggregate_id=? AND aggregate_version=?",
            (aggregate_kind.value, aggregate_id, event.aggregate_version + 1),
        ).fetchone()
        successor_event = None
        if successor_row is not None:
            try:
                successor_event = EventEnvelope.from_dict(
                    json.loads(str(successor_row["envelope_json"]))
                )
            except (ContractError, TypeError, ValueError, json.JSONDecodeError):
                successor_event = None
        successor_valid = bool(
            successor_event is not None
            and _event_row_matches(successor_row, successor_event)
            and successor_event.aggregate_kind is aggregate_kind
            and str(successor_event.aggregate_id) == aggregate_id
            and successor_event.aggregate_version == event.aggregate_version + 1
            and successor_event.prior_aggregate_digest == event.event_digest
        )
        anchored = bool(
            head is not None
            and (
                (int(head[0]) == event.aggregate_version and str(head[1]) == event.event_digest)
                or (int(head[0]) > event.aggregate_version and successor_valid)
            )
        )
        previous_global_row = connection.execute(
            "SELECT * FROM v3_events WHERE global_sequence=?",
            (event.global_sequence - 1,),
        ).fetchone()
        next_global_row = connection.execute(
            "SELECT * FROM v3_events WHERE global_sequence=?",
            (event.global_sequence + 1,),
        ).fetchone()
        previous_global_event = None
        next_global_event = None
        try:
            if previous_global_row is not None:
                previous_global_event = EventEnvelope.from_dict(
                    json.loads(str(previous_global_row["envelope_json"]))
                )
            if next_global_row is not None:
                next_global_event = EventEnvelope.from_dict(
                    json.loads(str(next_global_row["envelope_json"]))
                )
        except (ContractError, TypeError, ValueError, json.JSONDecodeError):
            previous_global_event = None
            next_global_event = None
        global_edge = bool(
            (event.global_sequence == 1 and event.prior_global_digest == ZERO_DIGEST)
            or (
                previous_global_event is not None
                and _event_row_matches(previous_global_row, previous_global_event)
                and previous_global_event.event_digest == event.prior_global_digest
            )
        ) and bool(
            next_global_row is None
            or (
                next_global_event is not None
                and _event_row_matches(next_global_row, next_global_event)
                and next_global_event.prior_global_digest == event.event_digest
            )
        )
        if not anchored or not global_edge:
            raise ProjectionConflict("U5 source event chain authority differs")
        return event

    def verify_current_field(
        self, field: Any, *, _connection: sqlite3.Connection | None = None
    ) -> _VerifiedU5FieldAuthority:
        """Bind a request to the exact current U5 ingress and frozen epoch."""

        from strathmark.v3.application.field_assembly import (
            AssemblyConflict,
            FrozenFieldRevision,
        )

        if not isinstance(field, FrozenFieldRevision):
            raise AssemblyConflict("current field verification requires a typed revision")
        context = (
            open_v3_connection(self._database_path, read_only=True)
            if _connection is None
            else nullcontext(_connection)
        )
        with context as connection:
            ingress = connection.execute(
                "SELECT upstream_revision, tournament_id, round_id, snapshot_json, "
                "source_global_sequence,snapshot_digest FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' "
                "AND entity_id=? ORDER BY upstream_revision DESC LIMIT 1",
                (str(field.field_id),),
            ).fetchone()
            if ingress is None:
                raise AssemblyConflict("field lacks current U5 ingress authority")
            snapshot = json.loads(str(ingress[3]))
            ingress_event = self._verify_u5_source_event(
                connection,
                source_global_sequence=int(ingress[4]),
                event_kind=EventKind.FIELD_ROSTER_REVISED,
                aggregate_kind=AggregateKind.FIELD_INGRESS,
                aggregate_id=str(
                    deterministic_identifier(
                        AggregateKind.FIELD_INGRESS.value,
                        {"entity_id": str(field.field_id)},
                    )
                ),
            )
            ingress_payload = cast(InlinePayload, ingress_event.command.payload).to_value()
            ingress_head = connection.execute(
                "SELECT aggregate_version,event_digest FROM v3_aggregate_heads "
                "WHERE aggregate_kind=? AND aggregate_id=?",
                (
                    AggregateKind.FIELD_INGRESS.value,
                    str(ingress_event.aggregate_id),
                ),
            ).fetchone()
            expected_roster = [str(item.competitor_id) for item in field.ordered_assignments]
            expected_stands = [str(item.stand_id) for item in field.ordered_assignments]
            if (
                int(ingress[0]) != field.field_revision
                or str(ingress[1]) != str(field.tournament_id)
                or str(ingress[2]) != str(field.round_id)
                or snapshot.get("competitor_ids") != expected_roster
                or snapshot.get("stand_ids") != expected_stands
                or snapshot.get("target_context") != field.target_context.to_dict()
                or snapshot.get("capacity_authority_digest") != field.capacity_authority_digest
                or snapshot.get("max_field_entrants") != field.max_field_entrants
                or snapshot.get("call_order") != field.call_order
                or snapshot.get("scheduled_at") != field.scheduled_at
                or snapshot.get("deadline_at") != field.deadline_at
                or str(ingress[5]) != canonical_digest(snapshot)
                or ingress_payload
                != {
                    "schema_version": "strathmark-v3-upstream-snapshot-v1",
                    "entity_kind": "field",
                    "entity_id": str(field.field_id),
                    "upstream_revision": int(ingress[0]),
                    "tournament_id": str(ingress[1]),
                    "round_id": str(ingress[2]),
                    "snapshot": snapshot,
                    "snapshot_digest": str(ingress[5]),
                }
                or ingress_head is None
                or int(ingress_head[0]) != ingress_event.aggregate_version
                or str(ingress_head[1]) != ingress_event.event_digest
            ):
                raise AssemblyConflict("field request differs from current U5 ingress")
            epoch = connection.execute(
                "SELECT epoch_json, epoch_digest, maximum_tournament_sequence, "
                "historical_cutoff_key,frozen_global_sequence FROM v3_evidence_epochs "
                "WHERE epoch_id=? AND round_id=?",
                (str(field.tournament_epoch_id), str(field.round_id)),
            ).fetchone()
            if epoch is None:
                raise AssemblyConflict("field request lacks its frozen U5 epoch")
            if (
                str(epoch[1]) != field.evidence_digest
                or int(epoch[2]) != field.tournament_event_sequence
                or str(epoch[3]) != str(field.historical_cutoff_key)
            ):
                raise AssemblyConflict("field request differs from frozen epoch authority")
            epoch_value = json.loads(str(epoch[0]))
            epoch_event = self._verify_u5_source_event(
                connection,
                source_global_sequence=int(epoch[4]),
                event_kind=EventKind.ROUND_EPOCH_FROZEN,
                aggregate_kind=AggregateKind.EPOCH,
                aggregate_id=str(field.tournament_epoch_id),
            )
            epoch_payload = cast(InlinePayload, epoch_event.command.payload).to_value()
            if (
                str(epoch[1]) != canonical_digest(epoch_value)
                or epoch_payload.get("schema_version") != "strathmark-v3-epoch-event-v1"
                or epoch_payload.get("epoch_id") != str(field.tournament_epoch_id)
                or epoch_payload.get("content_digest") != str(epoch[1])
                or epoch_payload.get("epoch") != epoch_value
            ):
                raise AssemblyConflict("frozen epoch differs from its U5 event authority")
            opened = connection.execute(
                "SELECT global_sequence FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "ORDER BY global_sequence LIMIT 1",
                (str(field.tournament_id), EventKind.TOURNAMENT_OPENED.value),
            ).fetchone()
            if opened is None:
                raise AssemblyConflict("field tournament is not open")
            tournament_open_event = self._verify_u5_source_event(
                connection,
                source_global_sequence=int(opened[0]),
                event_kind=EventKind.TOURNAMENT_OPENED,
                aggregate_kind=AggregateKind.TOURNAMENT,
                aggregate_id=str(field.tournament_id),
            )
            lifecycle_rows = connection.execute(
                "SELECT * FROM v3_events WHERE aggregate_kind=? AND aggregate_id=? "
                "ORDER BY aggregate_version",
                (AggregateKind.TOURNAMENT.value, str(field.tournament_id)),
            ).fetchall()
            lifecycle_events: list[EventEnvelope] = []
            prior_lifecycle_digest = ZERO_DIGEST
            prior_lifecycle_version = 0
            for lifecycle_row in lifecycle_rows:
                try:
                    lifecycle_event = EventEnvelope.from_dict(
                        json.loads(str(lifecycle_row["envelope_json"]))
                    )
                except (ContractError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise AssemblyConflict(
                        "tournament lifecycle event authority is corrupt"
                    ) from exc
                if (
                    not _event_row_matches(lifecycle_row, lifecycle_event)
                    or lifecycle_event.aggregate_kind is not AggregateKind.TOURNAMENT
                    or lifecycle_event.aggregate_id != field.tournament_id
                    or lifecycle_event.aggregate_version != prior_lifecycle_version + 1
                    or lifecycle_event.prior_aggregate_digest != prior_lifecycle_digest
                ):
                    raise AssemblyConflict("tournament lifecycle chain authority differs")
                lifecycle_events.append(lifecycle_event)
                prior_lifecycle_version = lifecycle_event.aggregate_version
                prior_lifecycle_digest = lifecycle_event.event_digest
            lifecycle_head = connection.execute(
                "SELECT aggregate_version,event_digest FROM v3_aggregate_heads "
                "WHERE aggregate_kind=? AND aggregate_id=?",
                (AggregateKind.TOURNAMENT.value, str(field.tournament_id)),
            ).fetchone()
            if (
                not lifecycle_events
                or sum(item == tournament_open_event for item in lifecycle_events) != 1
                or any(item.kind is EventKind.TOURNAMENT_CLOSED for item in lifecycle_events)
                or lifecycle_head is None
                or int(lifecycle_head[0]) != prior_lifecycle_version
                or str(lifecycle_head[1]) != prior_lifecycle_digest
            ):
                raise AssemblyConflict("field tournament lifecycle is closed or corrupt")
            tournament_lifecycle_digest = canonical_digest(
                {
                    "schema_version": "strathmark-v3-tournament-lifecycle-v1",
                    "events": [
                        {
                            "global_sequence": item.global_sequence,
                            "event_digest": item.event_digest,
                        }
                        for item in lifecycle_events
                    ],
                }
            )
            open_value = cast(InlinePayload, tournament_open_event.command.payload).to_value()
            expected_bundle = canonical_digest({"bundle_id": open_value.get("bundle_id")})
            if expected_bundle != field.bundle_digest or open_value.get(
                "historical_cutoff_key"
            ) != str(field.historical_cutoff_key):
                raise AssemblyConflict("field bundle or historical boundary drifted")
            # The exact frozen epoch event was admitted only after its U5
            # derivation barrier completed.  Its immutable event/payload digest
            # is the hot-path barrier authority; disposable completion rows are
            # neither trusted nor scanned here.
            derivation_barrier_digest = canonical_digest(
                {
                    "schema_version": "strathmark-v3-field-derivation-barrier-v1",
                    "epoch_event_digest": epoch_event.event_digest,
                    "maximum_tournament_sequence": field.tournament_event_sequence,
                    "epoch_digest": field.evidence_digest,
                }
            )
            issued = connection.execute(
                "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind=? LIMIT 1",
                (str(field.field_id), EventKind.FIELD_ISSUED.value),
            ).fetchone()
            if issued is not None:
                raise AssemblyConflict("issued field cannot be superseded or regenerated")
            proof_value = {
                "schema_version": "strathmark-v3-verified-u5-field-authority-v1",
                "field_revision_digest": field.revision_digest,
                "ingress_source_global_sequence": int(ingress[4]),
                "ingress_event_digest": ingress_event.event_digest,
                "epoch_source_global_sequence": int(epoch[4]),
                "epoch_event_digest": epoch_event.event_digest,
                "tournament_open_source_global_sequence": int(opened[0]),
                "tournament_open_event_digest": tournament_open_event.event_digest,
                "tournament_lifecycle_digest": tournament_lifecycle_digest,
                "derivation_barrier_digest": derivation_barrier_digest,
            }
            authority = _VerifiedU5FieldAuthority(
                field.revision_digest,
                int(ingress[4]),
                ingress_event.event_digest,
                int(epoch[4]),
                epoch_event.event_digest,
                int(opened[0]),
                tournament_open_event.event_digest,
                tournament_lifecycle_digest,
                derivation_barrier_digest,
                canonical_digest(proof_value),
                _VERIFIED_U5_FIELD_CAPABILITY,
            )
            authority.verify()
            return authority

    def lookup_exact(
        self,
        *,
        caller_namespace: str,
        request_identity: str,
        field_revision_digest: str,
    ) -> Any | None:
        from strathmark.v3.application.field_assembly import AssemblyConflict

        cache_key = (caller_namespace, request_identity)
        database_signature = _sqlite_files_signature(self._database_path)
        cached = self._verified_exact_retry_cache.get(cache_key)
        if (
            cached is not None
            and cached.field_revision_digest == field_revision_digest
            and cached.database_signature == database_signature
            and _receipt_blob_signature(cached.receipt_projection, self._blob_store)
            == cached.receipt_blob_signature
            and _sqlite_files_signature(self._database_path) == database_signature
        ):
            return cached.result

        recover_projection = False
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM v3_field_receipts WHERE caller_namespace=? AND request_identity=?",
                (caller_namespace, request_identity),
            ).fetchone()
            if row is None:
                command_id = deterministic_identifier(
                    "assembly",
                    {
                        "caller_namespace": caller_namespace,
                        "request_identity": request_identity,
                    },
                )
                recover_projection = (
                    connection.execute(
                        "SELECT 1 FROM v3_events WHERE command_id=? AND event_kind IN (?,?) "
                        "LIMIT 1",
                        (
                            str(command_id),
                            EventKind.FIELD_OPTIMIZED.value,
                            EventKind.FIELD_REGENERATED.value,
                        ),
                    ).fetchone()
                    is not None
                )
            else:
                if str(row["field_revision_digest"]) != field_revision_digest:
                    raise AssemblyConflict("idempotency key already binds different material")
                receipt = self._verify_exact_receipt_row(connection, row)
                result = self._decode_result(row, receipt=receipt)
        if row is not None:
            verified_signature = _sqlite_files_signature(self._database_path)
            if verified_signature == database_signature:
                projection_value = json.loads(str(row["receipt_json"]))
                self._verified_exact_retry_cache[cache_key] = _VerifiedExactRetryCacheEntry(
                    field_revision_digest,
                    verified_signature,
                    projection_value,
                    _receipt_blob_signature(projection_value, self._blob_store),
                    result,
                )
            else:
                self._verified_exact_retry_cache.pop(cache_key, None)
            return result
        if not recover_projection:
            return None
        self.rebuild_field_receipt_projection()
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM v3_field_receipts WHERE caller_namespace=? AND request_identity=?",
                (caller_namespace, request_identity),
            ).fetchone()
            if row is None:
                raise ProjectionError(
                    "field receipt recovery did not restore exact event authority"
                )
            if str(row["field_revision_digest"]) != field_revision_digest:
                raise AssemblyConflict("idempotency key already binds different material")
            receipt = self._verify_exact_receipt_row(connection, row)
            return self._decode_result(row, receipt=receipt)

    def rebuild_field_receipt_projection(self) -> int:
        """Offline-rebuild receipt and approval views from EventStore/CAS authority."""

        from strathmark.v3.contracts.receipts import FieldReceipt
        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        event_store = SQLiteEventStore(self._database_path)
        event_store.verify()
        anchor = event_store.current_anchor()
        materials: list[dict[str, Any]] = []
        with open_v3_connection(self._database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE event_kind IN (?,?) "
                "ORDER BY global_sequence",
                (
                    EventKind.FIELD_OPTIMIZED.value,
                    EventKind.FIELD_REGENERATED.value,
                ),
            ).fetchall()
            for stored in rows:
                event = EventEnvelope.from_dict(json.loads(str(stored[0])))
                payload_reference = event.command.payload
                if not isinstance(payload_reference, InlinePayload):
                    continue
                payload = payload_reference.to_value()
                if payload.get("schema_version") != "strathmark-v3-field-assembly-event-v1":
                    continue
                if isinstance(payload.get("receipt"), dict):
                    receipt_projection = cast(dict[str, Any], payload["receipt"])
                    receipt = FieldReceipt.from_dict(receipt_projection)
                else:
                    receipt_projection = {
                        "schema_version": _FIELD_RECEIPT_PROJECTION_SCHEMA,
                        "receipt_summary": payload.get("receipt_summary"),
                        "receipt_blob_reference": payload.get("receipt_blob_reference"),
                    }
                    receipt = _decode_field_receipt_projection(receipt_projection, self._blob_store)
                if not _field_receipt_event_matches(payload, receipt, receipt_projection):
                    raise ProjectionError("field receipt rebuild event authority differs")
                command_id = deterministic_identifier(
                    "assembly",
                    {
                        "caller_namespace": receipt.caller_namespace,
                        "request_identity": str(receipt.request_identity),
                    },
                )
                if str(event.command.command_id) != str(command_id):
                    raise ProjectionError("field receipt rebuild command authority differs")
                idempotency = connection.execute(
                    "SELECT 1 FROM v3_idempotency_records WHERE principal_id=? "
                    "AND idempotency_key=?",
                    (str(event.command.actor_id), str(command_id)),
                ).fetchone()
                if idempotency is None:
                    raise ProjectionError("field receipt rebuild lacks idempotency authority")
                crn_assignments = payload.get("crn_assignments")
                if not isinstance(crn_assignments, list):
                    raise ProjectionError("field receipt rebuild CRN authority differs")
                materials.append(
                    {
                        "receipt": receipt,
                        "receipt_projection": receipt_projection,
                        "event": event,
                        "field_revision_digest": payload.get("field_revision_digest"),
                        "pipeline_digest": payload.get("pipeline_digest"),
                        "crn_assignments": crn_assignments,
                    }
                )
        superseded_by = {
            str(item["receipt"].supersedes_receipt_id): item["event"].global_sequence
            for item in materials
            if item["receipt"].supersedes_receipt_id is not None
        }
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                head = connection.execute(
                    "SELECT global_sequence,event_digest FROM v3_events "
                    "ORDER BY global_sequence DESC LIMIT 1"
                ).fetchone()
                observed_anchor = (
                    0 if head is None else int(head[0]),
                    ZERO_DIGEST if head is None else str(head[1]),
                )
                if observed_anchor != (
                    anchor.global_sequence,
                    anchor.event_digest,
                ):
                    raise ProjectionError("event authority changed during field receipt rebuild")
                for table in (
                    "v3_approval_snapshot_rows",
                    "v3_approval_snapshot_history",
                    "v3_approval_decision_projection",
                    "v3_approval_command_projection",
                    "v3_approval_details",
                    "v3_approval_queue_rows",
                    "v3_approval_schedule",
                    "v3_approval_projection_meta",
                ):
                    connection.execute(f"DELETE FROM {table}")  # noqa: S608
                connection.execute("DELETE FROM v3_field_receipts")
                for material in materials:
                    receipt = material["receipt"]
                    event = material["event"]
                    connection.execute(
                        "INSERT INTO v3_field_receipts(receipt_id, field_id, "
                        "receipt_revision, supersedes_receipt_id, caller_namespace, "
                        "request_identity, field_revision_digest, pipeline_digest, "
                        "receipt_json, receipt_digest, crn_assignments_json, "
                        "source_global_sequence, superseded_by_sequence, created_at, "
                        "upstream_field_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            str(receipt.receipt_id),
                            str(receipt.field_id),
                            receipt.receipt_revision,
                            (
                                None
                                if receipt.supersedes_receipt_id is None
                                else str(receipt.supersedes_receipt_id)
                            ),
                            receipt.caller_namespace,
                            str(receipt.request_identity),
                            material["field_revision_digest"],
                            material["pipeline_digest"],
                            canonical_bytes(material["receipt_projection"]).decode(),
                            receipt.content_digest,
                            canonical_bytes(material["crn_assignments"]).decode(),
                            event.global_sequence,
                            superseded_by.get(str(receipt.receipt_id)),
                            event.occurred_at_utc,
                            receipt.upstream_field_revision,
                        ),
                    )
                for row in connection.execute(
                    "SELECT * FROM v3_field_receipts ORDER BY source_global_sequence"
                ):
                    self._verify_exact_receipt_row(connection, row)
                _rebuild_approval_snapshot_history_connection(
                    connection,
                    rebuilt_at=(
                        "1970-01-01T00:00:00.000Z"
                        if not materials
                        else materials[-1]["event"].occurred_at_utc
                    ),
                )
        return len(materials)

    def current_receipt(self, field_id: str) -> Any | None:
        require_identifier(field_id, expected_namespace="field")
        recover_projection = False
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM v3_field_receipts WHERE field_id=? "
                "AND superseded_by_sequence IS NULL",
                (field_id,),
            ).fetchone()
            if row is not None:
                self._verify_exact_receipt_row(connection, row)
                return self._decode_receipt(row)
            recover_projection = (
                connection.execute(
                    "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind IN (?,?) LIMIT 1",
                    (
                        field_id,
                        EventKind.FIELD_OPTIMIZED.value,
                        EventKind.FIELD_REGENERATED.value,
                    ),
                ).fetchone()
                is not None
            )
        if not recover_projection:
            return None
        self.rebuild_field_receipt_projection()
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM v3_field_receipts WHERE field_id=? "
                "AND superseded_by_sequence IS NULL",
                (field_id,),
            ).fetchone()
            if row is None:
                raise ProjectionError("field receipt recovery did not restore current authority")
            self._verify_exact_receipt_row(connection, row)
            return self._decode_receipt(row)

    def receipt(self, receipt_id: str) -> Any:
        require_identifier(receipt_id, expected_namespace="receipt")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM v3_field_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
        if row is None:
            raise KeyError(receipt_id)
        return self._decode_receipt(row)

    def verified_receipt(self, receipt_id: str) -> Any:
        """Load a receipt only after its event-backed projection has verified."""

        require_identifier(receipt_id, expected_namespace="receipt")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM v3_field_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(receipt_id)
            return self._verify_exact_receipt_row(connection, row)

    def active_expected_time_override(
        self,
        *,
        tournament_id: StableIdentifier,
        field_id: StableIdentifier,
        target_context_digest: str,
        call_order: int,
        competitor_id: StableIdentifier,
    ) -> Any | None:
        """Load the one authenticated accepted override applicable to a future field."""

        from strathmark.v3.domain.disagreement import AcceptedExpectedTimeOverrideState

        tournament = require_identifier(tournament_id, expected_namespace="tournament")
        field = require_identifier(field_id, expected_namespace="field")
        competitor = require_identifier(competitor_id, expected_namespace="competitor")
        _require_projection_digest(target_context_digest, "override target context")
        if isinstance(call_order, bool) or not isinstance(call_order, int) or call_order < 0:
            raise ProjectionError("override target call order must be nonnegative")
        matches = []
        with open_v3_connection(self._database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT state_json,state_digest,source_global_sequence,source_event_digest "
                "FROM v3_expected_time_override_states WHERE competitor_id=? "
                "AND tournament_id=? AND active=1 AND accepted_call_order<=? "
                "ORDER BY source_global_sequence DESC LIMIT 2",
                (str(competitor), str(tournament), call_order),
            ).fetchall()
            for row in rows:
                try:
                    value = json.loads(str(row[0]))
                    state = AcceptedExpectedTimeOverrideState.from_dict(value)
                except Exception as exc:
                    raise ProjectionError("accepted override state is malformed") from exc
                source = connection.execute(
                    "SELECT event_digest,event_kind FROM v3_events WHERE global_sequence=?",
                    (int(row[2]),),
                ).fetchone()
                if (
                    canonical_bytes(value).decode() != str(row[0])
                    or state.state_digest != str(row[1])
                    or state.accepted_global_sequence != int(row[2])
                    or state.accepted_event_digest != str(row[3])
                    or source is None
                    or str(source[0]) != str(row[3])
                    or str(source[1]) != EventKind.APPROVAL_DECISION_RECORDED.value
                ):
                    raise ProjectionError("accepted override state differs from event authority")
                if state.applies_to(
                    tournament_id=tournament,
                    field_id=field,
                    target_context_digest=target_context_digest,
                    call_order=call_order,
                ):
                    matches.append(state)
        if len(matches) > 1:
            raise ProjectionError("multiple accepted overrides overlap without supersession")
        return None if not matches else matches[0]

    def verify_bounded_checkpoint(self) -> str:
        """Verify the newest transactional approval checkpoint without a rebuild."""

        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT tournament_id FROM v3_approval_projection_meta "
                "ORDER BY source_global_sequence DESC,tournament_id LIMIT 1"
            ).fetchone()
            if row is None:
                return ZERO_DIGEST
            return _verify_approval_checkpoint_connection(connection, str(row[0]))

    def integrity_checkpoint_status(self) -> dict[str, object]:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT subject_id,source_global_sequence,source_event_digest,"
                "projection_digest,last_deep_verified_at,checkpoint_digest "
                "FROM v3_projection_integrity_checkpoints "
                "WHERE projection_kind='approval' "
                "ORDER BY source_global_sequence DESC,subject_id LIMIT 1"
            ).fetchone()
            if row is None:
                return {
                    "authority_sequence": 0,
                    "authority_digest": ZERO_DIGEST,
                    "projection_digest": ZERO_DIGEST,
                    "last_deep_verified_at": "1970-01-01T00:00:00.000Z",
                    "checkpoint_digest": ZERO_DIGEST,
                }
            verified = _verify_approval_checkpoint_connection(connection, str(row[0]))
            return {
                "authority_sequence": int(row[1]),
                "authority_digest": str(row[2]),
                "projection_digest": verified,
                "last_deep_verified_at": str(row[4]),
                "checkpoint_digest": str(row[5]),
            }

    def approval_facts(self, receipt_id: str) -> Any:
        """Derive KTD10 facts from verified receipt/current projection authority."""

        from strathmark.v3.application.approval import (
            FreshnessState,
            derive_receipt_approval_facts,
        )

        receipt = self.verified_receipt(receipt_id)
        current = self.current_receipt(str(receipt.field_id))
        with open_v3_connection(self._database_path, read_only=True) as connection:
            ingress = connection.execute(
                "SELECT upstream_revision, snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' AND entity_id=? "
                "ORDER BY upstream_revision DESC LIMIT 1",
                (str(receipt.field_id),),
            ).fetchone()
        snapshot = None if ingress is None else json.loads(str(ingress[1]))
        u5_current = (
            ingress is not None
            and int(ingress[0]) == receipt.upstream_field_revision
            and isinstance(snapshot, dict)
            and snapshot.get("competitor_ids")
            == [str(item) for item in receipt.ordered_competitor_ids]
            and snapshot.get("target_context") == receipt.target_context.to_dict()
        )
        facts = derive_receipt_approval_facts(
            receipt,
            u5_current=(
                current is not None and current.receipt_id == receipt.receipt_id and u5_current
            ),
            integrity_verified=True,
        )
        assert facts.freshness in {FreshnessState.CURRENT, FreshnessState.STALE}
        return facts

    def rebuild_approval_projection(self, *, tournament_id: str, rebuilt_at: str) -> str:
        """Deterministically replace the compact view from verified sealed authority."""

        from strathmark.v3.contracts.evidence import require_utc_milliseconds
        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        tournament_id = str(require_identifier(tournament_id, expected_namespace="tournament"))
        require_utc_milliseconds(rebuilt_at)
        SQLiteEventStore(self._database_path).verify()
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                snapshots = _rebuild_approval_snapshot_history_connection(
                    connection, rebuilt_at=rebuilt_at
                )
                if tournament_id not in snapshots:
                    raise KeyError(tournament_id)
                return snapshots[tournament_id]

    def approval_page(
        self,
        *,
        tournament_id: str,
        offset: int,
        limit: int,
        snapshot_id: str | None = None,
    ) -> Any:
        """Read one compact bounded page without loading a receipt detail blob."""

        from strathmark.v3.application.approval import (
            _VERIFIED_RECEIPT_AUTHORITY,
            ApprovalConflict,
            ApprovalError,
            ApprovalPage,
            ApprovalRow,
            QueueEmptyReason,
        )

        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ApprovalError("approval page must use offset >=0 and limit 1..100")
        tournament_id = str(require_identifier(tournament_id, expected_namespace="tournament"))
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                _verify_approval_checkpoint_connection(connection, tournament_id)
                meta = connection.execute(
                    "SELECT snapshot_id, row_count, lifecycle_state, "
                    "preparation_completed, preparation_total, preparing_count, "
                    "ready_count, blocked_count, issued_count, counts_json "
                    ", source_global_sequence, decision_global_sequence "
                    "FROM v3_approval_projection_meta WHERE tournament_id=?",
                    (tournament_id,),
                ).fetchone()
                if meta is None:
                    raise ProjectionError("approval projection is not initialized")
                current_snapshot = str(meta[0])
                if snapshot_id is not None and snapshot_id != current_snapshot:
                    raise ApprovalConflict("approval snapshot is stale", ())
                rows = connection.execute(
                    "SELECT field_id,row_json,row_digest FROM v3_approval_queue_rows "
                    "WHERE tournament_id=? "
                    "ORDER BY call_order, deadline_at, field_id LIMIT ? OFFSET ?",
                    (tournament_id, limit, offset),
                ).fetchall()
                snapshot = connection.execute(
                    "SELECT snapshot_json FROM v3_approval_snapshot_history WHERE snapshot_id=?",
                    (current_snapshot,),
                ).fetchone()
                if snapshot is None:
                    raise ProjectionError("approval snapshot checkpoint is missing")
                snapshot_value = json.loads(str(snapshot[0]))
                snapshot_rows = {
                    str(item[0]): str(item[5])
                    for item in snapshot_value["fields"]
                    if isinstance(item, list) and len(item) == 7
                }
                for stored in rows:
                    if snapshot_rows.get(str(stored[0])) != str(stored[2]):
                        raise ProjectionError(
                            "approval row differs from canonical authority checkpoint"
                        )
        decoded = []
        for stored in rows:
            try:
                value = json.loads(str(stored[1]))
                row = ApprovalRow.from_dict(value, _authority=_VERIFIED_RECEIPT_AUTHORITY)
            except Exception as exc:
                raise ProjectionError("approval scan row is corrupt") from exc
            if row.row_digest != str(stored[2]):
                raise ProjectionError("approval scan row digest differs")
            decoded.append(row)
        counts = json.loads(str(meta[9]))
        lanes = counts.get("lanes", {}) if isinstance(counts, dict) else {}
        batch_eligible = int(counts.get("batch_eligible", 0)) if isinstance(counts, dict) else 0
        lifecycle = str(meta[2])
        completed = int(meta[3])
        total = int(meta[4])
        row_count = int(meta[1])
        blocked = int(meta[7])
        empty_reason = (
            QueueEmptyReason.TOURNAMENT_CLOSED
            if lifecycle == "closed"
            else (
                QueueEmptyReason.ALL_ISSUED
                if lifecycle == "all_issued"
                else (
                    QueueEmptyReason.STILL_PREPARING
                    if completed < total
                    else (
                        QueueEmptyReason.NO_SCHEDULED_FIELDS
                        if total == 0
                        else (
                            QueueEmptyReason.ALL_BLOCKED
                            if row_count > 0 and blocked == row_count
                            else (
                                QueueEmptyReason.NO_BATCH_ELIGIBLE_FIELDS
                                if batch_eligible == 0
                                else None
                            )
                        )
                    )
                )
            )
        )
        return ApprovalPage(
            tournament_id=tournament_id,
            snapshot_id=current_snapshot,
            offset=offset,
            limit=limit,
            total=row_count,
            rows=tuple(decoded),
            lifecycle_state=lifecycle,
            preparation_completed=completed,
            preparation_total=total,
            preparing_count=int(meta[5]),
            ready_count=int(meta[6]),
            blocked_count=blocked,
            issued_count=int(meta[8]),
            projection_current=True,
            empty_reason=empty_reason,
            source_global_sequence=int(meta[10]),
            decision_global_sequence=int(meta[11]),
            lane_counts=tuple((str(lane), int(count)) for lane, count in sorted(lanes.items())),
            earliest_deadline_at=(
                None if not isinstance(counts, dict) else counts.get("earliest_deadline_at")
            ),
            retry_guidance=(
                "reload_snapshot_on_conflict",
                "poll_while_preparing",
                "open_verified_detail_for_exceptions",
            ),
        )

    def approval_detail(
        self, *, tournament_id: str, snapshot_id: str, receipt_id: str
    ) -> dict[str, Any]:
        """Read and verify one bounded exception detail separately from the queue scan."""

        from strathmark.v3.application.approval import (
            _VERIFIED_RECEIPT_AUTHORITY,
            ApprovalConflict,
            ApprovalRow,
        )

        require_identifier(receipt_id, expected_namespace="receipt")
        tournament_id = str(require_identifier(tournament_id, expected_namespace="tournament"))
        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                _verify_live_approval_projection_connection(connection)
                meta = connection.execute(
                    "SELECT snapshot_id FROM v3_approval_projection_meta WHERE tournament_id=?",
                    (tournament_id,),
                ).fetchone()
                if meta is None or str(meta[0]) != snapshot_id:
                    raise ApprovalConflict("approval snapshot is stale", ())
                row = connection.execute(
                    "SELECT detail.detail_json, detail.detail_digest, "
                    "detail.source_global_sequence, queue.row_json, queue.row_digest, "
                    "event.envelope_json FROM v3_approval_details detail "
                    "JOIN v3_approval_queue_rows queue USING(receipt_id) "
                    "JOIN v3_events event ON event.global_sequence=detail.source_global_sequence "
                    "WHERE detail.tournament_id=? AND queue.tournament_id=? "
                    "AND detail.receipt_id=?",
                    (tournament_id, tournament_id, receipt_id),
                ).fetchone()
        if row is None:
            raise KeyError(receipt_id)
        try:
            value = json.loads(str(row[0]))
        except (TypeError, ValueError) as exc:
            raise ProjectionError("approval detail is malformed") from exc
        if canonical_digest(value) != str(row[1]):
            raise ProjectionError("approval detail digest differs")
        if (
            set(value)
            != {
                "schema_version",
                "row",
                "receipt",
                "explanation",
                "source_event_digest",
            }
            or value.get("schema_version") != "strathmark-v3-approval-detail-v1"
        ):
            raise ProjectionError("approval detail fields or schema differ")
        try:
            from strathmark.v3.application.field_assembly import (
                JudgeReceiptExplanation,
                render_verified_receipt_explanation,
            )
            from strathmark.v3.contracts.receipts import FieldReceipt

            queue_value = json.loads(str(row[3]))
            projected_row = ApprovalRow.from_dict(
                value["row"], _authority=_VERIFIED_RECEIPT_AUTHORITY
            )
            receipt = FieldReceipt.from_dict(value["receipt"])
            explanation = JudgeReceiptExplanation.from_dict(value["explanation"])
            event = EventEnvelope.from_dict(json.loads(str(row[5])))
            payload = cast(InlinePayload, event.command.payload).to_value()
        except Exception as exc:
            raise ProjectionError("approval detail authority is malformed") from exc
        if (
            value["row"] != queue_value
            or projected_row.row_digest != str(row[4])
            or event.global_sequence != int(row[2])
            or event.event_digest != value["source_event_digest"]
            or event.kind not in {EventKind.FIELD_OPTIMIZED, EventKind.FIELD_REGENERATED}
            or (
                payload.get("receipt") != value["receipt"]
                if "receipt" in payload
                else _decode_field_receipt_projection(
                    {
                        "schema_version": _FIELD_RECEIPT_PROJECTION_SCHEMA,
                        "receipt_summary": payload.get("receipt_summary"),
                        "receipt_blob_reference": payload.get("receipt_blob_reference"),
                    },
                    self._blob_store,
                )
                != receipt
            )
            or explanation != render_verified_receipt_explanation(receipt)
        ):
            raise ProjectionError("approval detail differs from event authority")
        return cast(dict[str, Any], value)

    def record_approval_decision(self, command: Any) -> Any:
        """Append one exact immutable approval decision and advance its view atomically."""

        from strathmark.v3.application.approval import (
            ApprovalDecisionCommand,
            ApprovalDecisionReceipt,
        )
        from strathmark.v3.application.commands import CommandRequest, EventIntent
        from strathmark.v3.contracts.commands import (
            CommandEnvelope,
            CommandKind,
            InlinePayload,
        )
        from strathmark.v3.contracts.identifiers import IdempotencyKey
        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        if not isinstance(command, ApprovalDecisionCommand):
            raise ProjectionError("approval decision requires a typed command")
        decision = ApprovalDecisionReceipt.create(command)
        decision_id = deterministic_identifier(
            "approval_decision",
            {
                "tournament_id": command.tournament_id,
                "caller_namespace": command.caller_namespace,
                "request_identity": command.request_identity,
            },
        )
        event_command_id = deterministic_identifier(
            "approval_command",
            {
                "tournament_id": command.tournament_id,
                "caller_namespace": command.caller_namespace,
                "request_identity": command.request_identity,
            },
        )
        payload = InlinePayload.from_value(
            {
                "schema_version": "strathmark-v3-approval-decision-event-v1",
                "command": command.to_dict(),
                "decision": decision.to_dict(),
            }
        )
        envelope = CommandEnvelope(
            CommandKind.RECORD_APPROVAL_DECISION,
            IdempotencyKey(str(event_command_id)),
            decision_id,
            ((str(decision_id), 0),),
            require_identifier(command.actor_id, expected_namespace="actor"),
            payload,
        )
        request = CommandRequest(
            require_identifier(command.actor_id, expected_namespace="actor"),
            envelope,
            (
                EventIntent(
                    AggregateKind.APPROVAL_DECISION,
                    decision_id,
                    EventKind.APPROVAL_DECISION_RECORDED,
                ),
            ),
            "strathmark-v3-approval-decision-receipt-v1",
            decision.to_dict(),
            command.submitted_at,
            0,
        )

        def project(connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]) -> None:
            _project_approval_decision(connection, events[0], command, decision)

        stored = SQLiteEventStore(self._database_path).execute(request, projection_hook=project)
        return ApprovalDecisionReceipt.from_dict(stored.value())

    @staticmethod
    def _verify_rolling_pipeline_build_connection(
        connection: sqlite3.Connection, rolling_build: Any
    ) -> None:
        from strathmark.v3.infrastructure.integrity import SignedManifest

        for binding in rolling_build.publications:
            current = connection.execute(
                "SELECT publication_digest, dependency_revision FROM "
                "v3_rolling_card_current WHERE competitor_id=? "
                "AND target_context_digest=?",
                (str(binding.competitor_id), binding.target_context_digest),
            ).fetchone()
            publication = connection.execute(
                "SELECT card_digest, dependency_revision, tournament_epoch_id, "
                "bundle_digest, evidence_digest, hard_deadline_at, sealed_at, "
                "component_refs_digest, availability_json, "
                "council_manifest_digest, council_aggregate_manifest_json, "
                "publication_manifest_json "
                "FROM v3_rolling_card_publications WHERE publication_digest=?",
                (binding.publication_digest,),
            ).fetchone()
            if (
                current is None
                or str(current[0]) != binding.publication_digest
                or int(current[1]) != binding.dependency_revision
                or publication is None
                or str(publication[0]) != binding.card_digest
                or int(publication[1]) != binding.dependency_revision
                or str(publication[2]) != str(binding.tournament_epoch_id)
                or str(publication[3]) != binding.bundle_digest
                or str(publication[4]) != binding.evidence_digest
                or str(publication[5]) != binding.hard_deadline_at
                or str(publication[6]) != binding.sealed_at
                or str(publication[7]) != binding.component_refs_digest
                or tuple(tuple(item) for item in json.loads(str(publication[8])))
                != binding.availability
                or str(publication[9]) != binding.council_manifest_digest
                or SignedManifest.from_dict(json.loads(str(publication[10]))).body_digest
                != binding.council_aggregate_manifest_digest
                or SignedManifest.from_dict(json.loads(str(publication[11]))).body_digest
                != binding.publication_manifest_digest
            ):
                raise ProjectionConflict(
                    "rolling publication is no longer current for field commit"
                )
        for binding in rolling_build.capabilities:
            head = connection.execute(
                "SELECT aggregate_version, event_digest FROM v3_aggregate_heads "
                "WHERE aggregate_kind='competitor' AND aggregate_id=?",
                (str(binding.aggregate_id),),
            ).fetchone()
            if (
                head is None
                or int(head[0]) != binding.aggregate_version
                or str(head[1]) != binding.aggregate_event_digest
            ):
                raise ProjectionConflict("rolling capability is no longer current for field commit")

    def commit_receipt(
        self,
        *,
        field: Any,
        receipt: Any,
        pipeline: Any,
        pipeline_digest: str,
        weight_authority: Any,
        disagreement_authority: Any,
        cards: tuple[Any, ...],
        crn_assignments: tuple[tuple[str, str, int], ...],
        actor_id: str,
        occurred_at: str,
        rolling_build: Any | None = None,
        manual_action_store: Any | None = None,
        manual_action_binding: Any | None = None,
    ) -> Any:
        from strathmark.v3.application.commands import CommandRequest, EventIntent
        from strathmark.v3.application.field_assembly import (
            AssemblyConflict,
            AssemblyResult,
            FrozenFieldRevision,
            SealedPipelineOutput,
            verify_judge_supersession_authority,
            verify_receipt_matches_pipeline,
        )
        from strathmark.v3.application.manual_actions import (
            ManualActionBinding,
            ManualActionKind,
            create_manual_action_resolution,
        )
        from strathmark.v3.application.pipeline_builder import (
            RollingPipelineBuild,
            unwrap_rolling_pipeline_build,
        )
        from strathmark.v3.contracts.commands import (
            CommandEnvelope,
            CommandKind,
            InlinePayload,
        )
        from strathmark.v3.contracts.events import AggregateKind, EventKind
        from strathmark.v3.contracts.identifiers import (
            IdempotencyKey,
            deterministic_identifier,
        )
        from strathmark.v3.contracts.receipts import FieldReceipt
        from strathmark.v3.infrastructure.sqlite.event_store import (
            EventStoreConflict,
            EventStoreIntegrityError,
        )

        if (
            not isinstance(field, FrozenFieldRevision)
            or not isinstance(receipt, FieldReceipt)
            or not isinstance(pipeline, SealedPipelineOutput)
        ):
            raise ProjectionError("field commit requires typed revision and receipt")
        if rolling_build is not None:
            if not isinstance(rolling_build, RollingPipelineBuild):
                raise ProjectionConflict("rolling field commit authority is untyped")
            if unwrap_rolling_pipeline_build(rolling_build) is not pipeline:
                raise ProjectionConflict("rolling field commit pipeline differs")
        manual_resolution = None
        if manual_action_binding is not None or manual_action_store is not None:
            if (
                not isinstance(manual_action_binding, ManualActionBinding)
                or manual_action_store is None
                or not callable(getattr(manual_action_store, "require_current_connection", None))
                or not callable(getattr(manual_action_store, "resolve_connection", None))
                or pipeline.construction_submission is None
                or pipeline.manual_authority is None
            ):
                raise ProjectionConflict("field commit manual-action authority is incomplete")
            requirement = manual_action_store.require_current(manual_action_binding)
            expected_action = (
                ManualActionKind.ACCEPT_SINGLE_SURVIVOR
                if pipeline.manual_authority.mode.value == "exact_single_survivor"
                else ManualActionKind.COMPLETE_EXPECTED_TIME
            )
            if requirement.action is not expected_action:
                raise ProjectionConflict(
                    "field commit manual-action kind differs from construction"
                )
            manual_resolution = create_manual_action_resolution(
                requirement,
                receipt_id=receipt.receipt_id,
                receipt_digest=receipt.content_digest,
                actor_id=actor_id,
                resolved_at=occurred_at,
                signer=self._signer,
            )
        verify_receipt_matches_pipeline(
            field=field,
            pipeline=pipeline,
            receipt=receipt,
            crn_assignments=crn_assignments,
        )
        if (
            pipeline.pipeline_digest != pipeline_digest
            or pipeline.operational_weight_authority != weight_authority
            or pipeline.disagreement != disagreement_authority
            or tuple(item.card for item in pipeline.prediction_evidence) != cards
        ):
            raise ProjectionConflict("field commit authority differs from sealed pipeline")
        _digest(pipeline_digest)
        existing = self.lookup_exact(
            caller_namespace=receipt.caller_namespace,
            request_identity=str(receipt.request_identity),
            field_revision_digest=field.revision_digest,
        )
        if existing is not None:
            return existing
        receipt_value = receipt.to_dict()
        receipt_bytes = receipt.canonical_payload
        capacity_authority = self.verify_capacity_authority(
            field.capacity_authority_digest,
            bundle_digest=field.bundle_digest,
            entrant_count=len(field.ordered_assignments),
            declared_max_field_entrants=field.max_field_entrants,
        )
        if len(receipt_bytes) > capacity_authority.capacity.max_receipt_bytes:
            raise ProjectionConflict("field receipt exceeds installed receipt capacity authority")
        receipt_blob_reference = None
        receipt_projection_value = receipt_value
        if len(receipt_bytes) > MAX_INLINE_PAYLOAD_BYTES:
            receipt_blob_reference = self._blob_store.publish(
                receipt_bytes,
                metadata=BlobMetadata(
                    "application/json",
                    receipt.schema_version,
                    BlobRetentionClass.REQUIRED,
                ),
            )
            receipt_projection_value = _field_receipt_projection(receipt, receipt_blob_reference)
        disagreement_blob_reference = None
        disagreement_blob_projection = None
        if disagreement_authority is not None:
            self.verify_disagreement_authority(disagreement_authority, field=field, cards=cards)
            disagreement_bytes = disagreement_authority.canonical_authority_payload
            if len(disagreement_bytes) > capacity_authority.capacity.max_blob_bytes:
                raise ProjectionConflict("disagreement authority exceeds installed blob capacity")
            disagreement_blob_reference = self._blob_store.publish(
                disagreement_bytes,
                metadata=BlobMetadata(
                    "application/json",
                    "strathmark-v3-operational-disagreement-authority-v3",
                    BlobRetentionClass.REQUIRED,
                ),
            )
            disagreement_blob_projection = _disagreement_blob_projection(
                disagreement_authority,
                disagreement_blob_reference,
                bundle_digest=field.bundle_digest,
            )
        u5_field_authority = self.verify_current_field(field)
        self.verify_weight_authority(weight_authority)
        self.verify_dependence_authority(pipeline.dependence_artifact)
        for card in cards:
            self.verify_card_authority(card)
        rolling_publications_digest = None
        rolling_capabilities_digest = None
        if rolling_build is not None:
            rolling_publications_digest = canonical_digest(
                [item.to_dict() for item in rolling_build.publications]
            )
            rolling_capabilities_digest = canonical_digest(
                [item.to_dict() for item in rolling_build.capabilities]
            )
            with open_v3_connection(self._database_path, read_only=True) as connection:
                self._verify_rolling_pipeline_build_connection(connection, rolling_build)
        prior_receipt_id = None
        prior_source_global_sequence = None
        prior_event_digest = None
        with open_v3_connection(self._database_path, read_only=True) as connection:
            prior_authority = connection.execute(
                "SELECT * FROM v3_field_receipts WHERE field_id=? "
                "AND superseded_by_sequence IS NULL",
                (str(field.field_id),),
            ).fetchone()
            if prior_authority is not None:
                self._verify_exact_receipt_row(connection, prior_authority)
                prior_receipt_id = str(prior_authority["receipt_id"])
                prior_source_global_sequence = int(prior_authority["source_global_sequence"])
                prior_event = connection.execute(
                    "SELECT event_digest FROM v3_events WHERE global_sequence=?",
                    (prior_source_global_sequence,),
                ).fetchone()
                if prior_event is None:
                    raise ProjectionError("current receipt lacks its exact source event")
                prior_event_digest = str(prior_event[0])
        prepared_value = {
            "schema_version": "strathmark-v3-prepared-field-commit-v1",
            "receipt_digest": receipt.content_digest,
            "field_revision_digest": field.revision_digest,
            "pipeline_digest": pipeline_digest,
            "weight_authority_digest": weight_authority.authority_digest,
            "dependence_artifact_digest": pipeline.dependence_artifact.artifact_digest,
            "card_manifest_digests": [card.manifest.body_digest for card in cards],
            "crn_assignments_digest": canonical_digest(crn_assignments),
            "receipt_blob_digest": (
                None if receipt_blob_reference is None else receipt_blob_reference.digest
            ),
            "disagreement_blob_digest": (
                None if disagreement_blob_reference is None else disagreement_blob_reference.digest
            ),
            "prior_receipt_id": prior_receipt_id,
            "prior_source_global_sequence": prior_source_global_sequence,
            "prior_event_digest": prior_event_digest,
            "u5_field_authority_digest": u5_field_authority.proof_digest,
            "rolling_publications_digest": rolling_publications_digest,
            "rolling_capabilities_digest": rolling_capabilities_digest,
            "manual_action_binding_digest": (
                None if manual_action_binding is None else manual_action_binding.binding_digest
            ),
        }
        prepared = _PreparedFieldCommit(
            receipt.content_digest,
            field.revision_digest,
            pipeline_digest,
            weight_authority.authority_digest,
            pipeline.dependence_artifact.artifact_digest,
            tuple(card.manifest.body_digest for card in cards),
            canonical_digest(crn_assignments),
            prepared_value["receipt_blob_digest"],
            prepared_value["disagreement_blob_digest"],
            prior_receipt_id,
            prior_source_global_sequence,
            prior_event_digest,
            u5_field_authority.proof_digest,
            rolling_publications_digest,
            rolling_capabilities_digest,
            prepared_value["manual_action_binding_digest"],
            canonical_digest(prepared_value),
            _PREPARED_FIELD_COMMIT_CAPABILITY,
        )
        event_store = self._events
        head = event_store.aggregate_head(str(field.field_id))
        expected = 0 if head is None else head[0]
        command_kind = CommandKind.OPTIMIZE_FIELD if head is None else CommandKind.REGENERATE_FIELD
        event_kind = EventKind.FIELD_OPTIMIZED if head is None else EventKind.FIELD_REGENERATED
        command_id = deterministic_identifier(
            "assembly",
            {
                "caller_namespace": receipt.caller_namespace,
                "request_identity": str(receipt.request_identity),
            },
        )
        payload_value: dict[str, Any] = {
            "schema_version": "strathmark-v3-field-assembly-event-v1",
            "round_id": str(field.round_id),
            "epoch_id": str(field.tournament_epoch_id),
            "field_revision": field.field_revision,
            "upstream_field_revision": field.field_revision,
            "receipt_revision": receipt.receipt_revision,
            "field_revision_digest": field.revision_digest,
            "pipeline_digest": pipeline_digest,
            "approval_call_order": field.call_order,
            "scheduled_at": field.scheduled_at,
            "approval_deadline_at": field.deadline_at,
            "operational_weight_authority_digest": weight_authority.authority_digest,
            "crn_assignments": [list(item) for item in crn_assignments],
        }
        if receipt_blob_reference is None:
            payload_value["receipt"] = receipt.to_dict()
        else:
            payload_value["receipt_summary"] = _field_receipt_summary(receipt)
            payload_value["receipt_blob_reference"] = receipt_blob_reference.to_dict()
        payload = InlinePayload.from_value(payload_value)
        command = CommandEnvelope(
            command_kind,
            IdempotencyKey(str(command_id)),
            field.field_id,
            ((str(field.field_id), expected),),
            require_identifier(actor_id, expected_namespace="actor"),
            payload,
        )
        result: dict[str, Any] = {
            "schema_version": "strathmark-v3-field-assembly-result-v1",
            "crn_assignments": [list(item) for item in crn_assignments],
        }
        if receipt_blob_reference is None:
            result["receipt"] = receipt.to_dict()
        else:
            result["receipt_summary"] = _field_receipt_summary(receipt)
            result["receipt_blob_reference"] = receipt_blob_reference.to_dict()
        request = CommandRequest(
            require_identifier(actor_id, expected_namespace="actor"),
            command,
            (EventIntent(AggregateKind.FIELD, field.field_id, event_kind),),
            "strathmark-v3-field-assembly-result-v1",
            result,
            occurred_at,
            0,
        )

        def project(connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]) -> None:
            # EventStore holds BEGIN IMMEDIATE here. Rechecking through a fresh
            # read connection therefore observes the last committed U5 authority
            # while excluding any concurrent ingress writer until this commit ends.
            prepared.verify()
            prior = connection.execute(
                "SELECT * FROM v3_field_receipts WHERE field_id=? "
                "AND superseded_by_sequence IS NULL",
                (str(field.field_id),),
            ).fetchone()
            observed_prior_receipt_id = None
            observed_prior_source_sequence = None
            observed_prior_event_digest = None
            if prior is not None:
                self._verify_exact_receipt_row(connection, prior)
                observed_prior_receipt_id = str(prior["receipt_id"])
                observed_prior_source_sequence = int(prior["source_global_sequence"])
                observed_prior_event = connection.execute(
                    "SELECT event_digest FROM v3_events WHERE global_sequence=?",
                    (observed_prior_source_sequence,),
                ).fetchone()
                if observed_prior_event is None:
                    raise ProjectionConflict("current receipt lacks source event authority")
                observed_prior_event_digest = str(observed_prior_event[0])
            if (
                observed_prior_receipt_id != prepared.prior_receipt_id
                or observed_prior_source_sequence != prepared.prior_source_global_sequence
                or observed_prior_event_digest != prepared.prior_event_digest
            ):
                raise ProjectionConflict("prepared prior receipt authority is no longer current")
            if (
                prepared.receipt_digest != receipt.content_digest
                or prepared.field_revision_digest != field.revision_digest
                or prepared.pipeline_digest != pipeline.pipeline_digest
                or prepared.weight_authority_digest != weight_authority.authority_digest
                or prepared.dependence_artifact_digest
                != pipeline.dependence_artifact.artifact_digest
                or prepared.card_manifest_digests
                != tuple(card.manifest.body_digest for card in cards)
                or prepared.crn_assignments_digest != canonical_digest(crn_assignments)
                or prepared.rolling_publications_digest != rolling_publications_digest
                or prepared.rolling_capabilities_digest != rolling_capabilities_digest
                or prepared.manual_action_binding_digest
                != (None if manual_action_binding is None else manual_action_binding.binding_digest)
            ):
                raise ProjectionConflict("prepared field commit authority differs")
            if manual_action_binding is not None:
                observed_requirement = manual_action_store.require_current_connection(
                    connection, manual_action_binding
                )
                if (
                    manual_resolution is None
                    or observed_requirement.requirement_digest
                    != manual_resolution.requirement_digest
                ):
                    raise ProjectionConflict(
                        "prepared manual-action requirement is no longer current"
                    )
            if rolling_build is not None:
                self._verify_rolling_pipeline_build_connection(connection, rolling_build)
            installed_capacity = self.verify_capacity_authority(
                field.capacity_authority_digest,
                bundle_digest=field.bundle_digest,
                entrant_count=len(field.ordered_assignments),
                declared_max_field_entrants=field.max_field_entrants,
                _connection=connection,
            )
            if len(receipt_bytes) > installed_capacity.capacity.max_receipt_bytes:
                raise ProjectionConflict(
                    "field receipt exceeds installed receipt capacity authority"
                )
            if receipt_blob_reference is not None:
                if receipt_blob_lease is None:
                    raise ProjectionConflict("prepared receipt blob lease is absent")
                receipt_blob_lease.verify_current()
            observed_u5_authority = self.verify_current_field(field, _connection=connection)
            if observed_u5_authority.proof_digest != prepared.u5_field_authority_digest:
                raise ProjectionConflict("prepared U5 field authority is no longer current")
            self._verify_weight_connection(connection, weight_authority)
            self._verify_dependence_connection(connection, pipeline.dependence_artifact)
            for card in cards:
                self._verify_card_authority(card)
            self.verify_disagreement_authority(disagreement_authority, field=field, cards=cards)
            if prepared.receipt_blob_digest is not None and (
                receipt_blob_reference is None
                or receipt_blob_reference.digest != prepared.receipt_blob_digest
            ):
                raise ProjectionConflict("prepared receipt blob authority differs")
            if prepared.disagreement_blob_digest is not None and (
                disagreement_blob_reference is None
                or disagreement_blob_reference.digest != prepared.disagreement_blob_digest
            ):
                raise ProjectionConflict("prepared disagreement blob authority differs")
            if disagreement_authority is not None:
                if disagreement_blob_reference is None or disagreement_blob_projection is None:
                    raise ProjectionConflict("prepared disagreement blob authority is absent")
                if disagreement_blob_lease is None:
                    raise ProjectionConflict("prepared disagreement blob lease is absent")
                disagreement_blob_lease.verify_current()
                connection.execute(
                    "INSERT INTO v3_field_disagreement_authority_blobs "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(receipt_digest) DO NOTHING",
                    (
                        disagreement_authority.receipt_digest,
                        disagreement_authority.field_revision_digest,
                        field.bundle_digest,
                        canonical_bytes(disagreement_blob_projection).decode(),
                        disagreement_blob_reference.digest,
                        disagreement_authority.policy_manifest.body_digest,
                        (
                            None
                            if disagreement_authority.council_manifest is None
                            else disagreement_authority.council_manifest.body_digest
                        ),
                        occurred_at,
                    ),
                )
                stored_disagreement = connection.execute(
                    "SELECT authority_blob_json, authority_blob_digest, "
                    "field_revision_digest, bundle_digest, policy_manifest_digest, "
                    "council_manifest_digest FROM "
                    "v3_field_disagreement_authority_blobs WHERE receipt_digest=?",
                    (disagreement_authority.receipt_digest,),
                ).fetchone()
                if stored_disagreement is None or (
                    str(stored_disagreement[0])
                    != canonical_bytes(disagreement_blob_projection).decode()
                    or str(stored_disagreement[1]) != disagreement_blob_reference.digest
                    or str(stored_disagreement[2]) != disagreement_authority.field_revision_digest
                    or str(stored_disagreement[3]) != field.bundle_digest
                    or str(stored_disagreement[4])
                    != disagreement_authority.policy_manifest.body_digest
                    or stored_disagreement[5]
                    != disagreement_blob_projection["council_manifest_digest"]
                ):
                    raise ProjectionConflict(
                        "stored disagreement authority differs from sealed pipeline"
                    )
            event = events[0]
            SQLiteProjectionStore._apply_prepared_dependency(connection, event, payload_value)
            expected_supersedes = None if prior is None else str(prior["receipt_id"])
            judge_supersession = (
                pipeline.construction_submission is not None
                or pipeline.expected_time_override is not None
            )
            same_upstream = prior is not None and receipt.upstream_field_revision == int(
                prior["upstream_field_revision"]
            )
            judge_binding_valid = not judge_supersession
            if prior is None and pipeline.construction_submission is not None:
                judge_binding_valid = bool(
                    pipeline.construction_submission.prior_receipt_id is None
                    and pipeline.construction_submission.prior_receipt_digest is None
                    and pipeline.construction_submission.upstream_field_revision
                    == receipt.upstream_field_revision
                )
            if prior is not None and judge_supersession:
                verify_judge_supersession_authority(
                    field=field,
                    pipeline=pipeline,
                    prior_receipt=_decode_field_receipt_projection(
                        json.loads(str(prior["receipt_json"])), self._blob_store
                    ),
                    actor_id=actor_id,
                )
            if prior is not None and pipeline.construction_submission is not None:
                judge_binding_valid = bool(
                    pipeline.construction_submission.prior_receipt_id
                    == receipt.supersedes_receipt_id
                    and pipeline.construction_submission.prior_receipt_digest
                    == str(prior["receipt_digest"])
                    and pipeline.construction_submission.upstream_field_revision
                    == receipt.upstream_field_revision
                )
            if prior is not None and pipeline.expected_time_override is not None:
                judge_binding_valid = bool(
                    pipeline.expected_time_override.prior_receipt_id
                    == receipt.supersedes_receipt_id
                    and pipeline.expected_time_override.prior_receipt_digest
                    == str(prior["receipt_digest"])
                    and pipeline.expected_time_override.upstream_field_revision
                    == receipt.upstream_field_revision
                )
            if (
                (prior is None and receipt.receipt_revision != 1)
                or (
                    prior is not None
                    and receipt.receipt_revision != int(prior["receipt_revision"]) + 1
                )
                or receipt.upstream_field_revision != field.field_revision
                or (
                    prior is not None
                    and receipt.upstream_field_revision < int(prior["upstream_field_revision"])
                )
                or (
                    prior is None
                    and judge_supersession
                    and pipeline.construction_submission is None
                )
                or (same_upstream and not judge_supersession)
                or (prior is not None and not same_upstream and judge_supersession)
                or not judge_binding_valid
                or (
                    None
                    if receipt.supersedes_receipt_id is None
                    else str(receipt.supersedes_receipt_id)
                )
                != expected_supersedes
            ):
                raise ProjectionConflict("field receipt revision/supersession is not current")
            if prior is not None:
                connection.execute(
                    "UPDATE v3_field_receipts SET superseded_by_sequence=? WHERE receipt_id=?",
                    (event.global_sequence, prior["receipt_id"]),
                )
            encoded = canonical_bytes(receipt_projection_value).decode()
            connection.execute(
                "INSERT INTO v3_field_receipts(receipt_id, field_id, receipt_revision, "
                "supersedes_receipt_id, caller_namespace, request_identity, "
                "field_revision_digest, pipeline_digest, receipt_json, receipt_digest, "
                "crn_assignments_json, source_global_sequence, superseded_by_sequence, created_at, "
                "upstream_field_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    str(receipt.receipt_id),
                    str(receipt.field_id),
                    receipt.receipt_revision,
                    expected_supersedes,
                    receipt.caller_namespace,
                    str(receipt.request_identity),
                    field.revision_digest,
                    pipeline_digest,
                    encoded,
                    receipt.content_digest,
                    canonical_bytes(crn_assignments).decode(),
                    event.global_sequence,
                    occurred_at,
                    receipt.upstream_field_revision,
                ),
            )
            _refresh_approval_tournament_connection(
                connection,
                tournament_id=str(field.tournament_id),
                rebuilt_at=occurred_at,
                prepared_receipts={str(receipt.receipt_id): receipt},
                prepared_disagreements=(
                    None
                    if disagreement_authority is None
                    else {disagreement_authority.receipt_digest: disagreement_authority}
                ),
                prepared_disagreement_digests=(
                    None
                    if disagreement_authority is None or disagreement_blob_reference is None
                    else {
                        disagreement_authority.receipt_digest: (disagreement_blob_reference.digest)
                    }
                ),
            )
            if manual_action_binding is not None:
                manual_action_store.resolve_connection(
                    connection, manual_action_binding, manual_resolution
                )

        try:
            receipt_lease_context = (
                nullcontext(None)
                if receipt_blob_reference is None
                else self._blob_store.verified_lease(receipt_blob_reference)
            )
            disagreement_lease_context = (
                nullcontext(None)
                if disagreement_blob_reference is None
                else self._blob_store.verified_lease(disagreement_blob_reference)
            )
            with (
                receipt_lease_context as receipt_blob_lease,
                disagreement_lease_context as disagreement_blob_lease,
            ):
                stored = event_store.execute(request, projection_hook=project)
        except (BlobStoreError, OSError) as exc:
            raise AssemblyConflict("field receipt required blob integrity failed") from exc
        except EventStoreIntegrityError as exc:
            raise AssemblyConflict("field receipt event authority integrity failed") from exc
        except EventStoreConflict as exc:
            concurrent_retry = self.lookup_exact(
                caller_namespace=receipt.caller_namespace,
                request_identity=str(receipt.request_identity),
                field_revision_digest=field.revision_digest,
            )
            if concurrent_retry is not None:
                return concurrent_retry
            raise AssemblyConflict("field receipt conflicted with concurrent authority") from exc
        stored_value = stored.value()
        stored_crn_value = stored_value.get("crn_assignments")
        if not isinstance(stored_crn_value, list):
            raise ProjectionError("stored field assembly result is not closed")
        authoritative_crn = tuple(tuple(item) for item in stored_crn_value)
        if stored_value != result or authoritative_crn != crn_assignments:
            raise ProjectionError(
                "stored field assembly result differs from the committed authority"
            )
        # EventStore has now atomically authenticated the exact command result and
        # the projection hook has committed this same receipt.  Return the already
        # validated typed value rather than rereading and reparsing its required
        # blob; exact retries and restarts still take the full durable decode path.
        return AssemblyResult(
            receipt,
            receipt_bytes,
            authoritative_crn,
        )

    def verify(self) -> None:
        from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore

        SQLiteEventStore(self._database_path).verify()
        with open_v3_connection(self._database_path, read_only=True) as connection:
            verified_disagreements: dict[str, Any] = {}
            for row in connection.execute("SELECT * FROM v3_field_weight_authorities"):
                self._verify_weight_row(row)
            for row in connection.execute("SELECT * FROM v3_field_dependence_authorities"):
                self._verify_dependence_row(row)
            for row in connection.execute(
                "SELECT authority_digest, bundle_digest, capacity_manifest_json "
                "FROM v3_field_capacity_authorities"
            ):
                from strathmark.v3.application.capacity import CapacityManifest

                capacity = CapacityManifest.from_dict(json.loads(str(row[2])))
                self.verify_capacity_authority(
                    str(row[0]),
                    bundle_digest=str(row[1]),
                    entrant_count=1,
                    declared_max_field_entrants=capacity.max_field_entrants,
                    _connection=connection,
                )
            for row in connection.execute(
                "SELECT receipt_digest FROM v3_field_disagreement_authority_blobs"
            ):
                disagreement_digest = str(row[0])
                resolved = self._resolve_disagreement_connection(connection, disagreement_digest)
                if resolved is None:
                    raise ProjectionError("stored disagreement authority blob is missing")
                verified_disagreements[disagreement_digest] = resolved
            current_counts = connection.execute(
                "SELECT field_id, COUNT(*) FROM v3_field_receipts "
                "WHERE superseded_by_sequence IS NULL GROUP BY field_id HAVING COUNT(*) != 1"
            ).fetchall()
            if current_counts:
                raise ProjectionError("field projection has multiple current receipts")
            expected_receipt_sequences = set()
            for stored_event in connection.execute(
                "SELECT envelope_json FROM v3_events WHERE event_kind IN (?,?)",
                (
                    EventKind.FIELD_OPTIMIZED.value,
                    EventKind.FIELD_REGENERATED.value,
                ),
            ):
                authority_event = EventEnvelope.from_dict(json.loads(str(stored_event[0])))
                if not isinstance(authority_event.command.payload, InlinePayload):
                    continue
                authority_payload = authority_event.command.payload.to_value()
                if (
                    authority_payload.get("schema_version")
                    == "strathmark-v3-field-assembly-event-v1"
                ):
                    expected_receipt_sequences.add(authority_event.global_sequence)
            observed_receipt_sequences = {
                int(row[0])
                for row in connection.execute(
                    "SELECT source_global_sequence FROM v3_field_receipts"
                )
            }
            if observed_receipt_sequences != expected_receipt_sequences:
                raise ProjectionError(
                    "field receipt projection coverage differs from event authority"
                )
            for row in connection.execute("SELECT * FROM v3_field_receipts"):
                receipt = self._decode_receipt(row)
                receipt_projection = json.loads(str(row["receipt_json"]))
                if str(row["receipt_digest"]) != receipt.content_digest:
                    raise ProjectionError("field receipt projection digest differs")
                event = connection.execute(
                    "SELECT envelope_json FROM v3_events WHERE global_sequence=?",
                    (row["source_global_sequence"],),
                ).fetchone()
                if event is None:
                    raise ProjectionError("field receipt lacks event authority")
                envelope = EventEnvelope.from_dict(json.loads(str(event[0])))
                payload = cast(InlinePayload, envelope.command.payload).to_value()
                section_values = {
                    section.kind.value: cast(InlinePayload, section.payload).to_value()
                    for section in receipt.sections
                }
                validation = section_values.get("validations", {})
                credibility = section_values.get("credibility", {})
                disagreement_section = section_values.get("disagreement", {})
                disagreement_summary = (
                    disagreement_section.get("operational_receipt")
                    if isinstance(disagreement_section, dict)
                    else None
                )
                if disagreement_summary is not None:
                    if not isinstance(disagreement_summary, dict):
                        raise ProjectionError("field disagreement summary is not canonical")
                    disagreement_digest = str(disagreement_summary.get("receipt_digest", ""))
                    resolved_disagreement = verified_disagreements.get(disagreement_digest)
                    if resolved_disagreement is None:
                        resolved_disagreement = self._resolve_disagreement_connection(
                            connection, disagreement_digest
                        )
                        if resolved_disagreement is not None:
                            verified_disagreements[disagreement_digest] = resolved_disagreement
                    if (
                        resolved_disagreement is None
                        or resolved_disagreement.to_dict() != disagreement_summary
                        or resolved_disagreement.field_revision_digest
                        != str(row["field_revision_digest"])
                    ):
                        raise ProjectionError(
                            "field receipt lacks exact disagreement blob authority"
                        )
                operational = credibility.get("operational_weight_authority")
                operational_digest = payload.get("operational_weight_authority_digest")
                installed_weight = connection.execute(
                    "SELECT 1 FROM v3_field_weight_authorities WHERE binding_digest=?",
                    (operational_digest,),
                ).fetchone()
                if not isinstance(validation, dict):
                    raise ProjectionError("field validation authority is malformed")
                dependence_digest = validation.get("dependence_artifact_digest")
                dependence_row = connection.execute(
                    "SELECT * FROM v3_field_dependence_authorities WHERE artifact_digest=?",
                    (dependence_digest,),
                ).fetchone()
                if (
                    dependence_row is None
                    or self._verify_dependence_row(dependence_row).artifact_digest
                    != dependence_digest
                ):
                    raise ProjectionError("field receipt lacks installed dependence authority")
                try:
                    self.verify_capacity_authority(
                        validation["capacity_authority_digest"],
                        bundle_digest=receipt.bundles[0].digest,
                        entrant_count=len(receipt.ordered_competitor_ids),
                        declared_max_field_entrants=validation["max_field_entrants"],
                        _connection=connection,
                    )
                except (KeyError, IndexError, ProjectionConflict) as exc:
                    raise ProjectionError(
                        "field receipt lacks installed capacity authority"
                    ) from exc
                projected_crn = json.loads(str(row["crn_assignments_json"]))
                expected_superseded = connection.execute(
                    "SELECT source_global_sequence FROM v3_field_receipts "
                    "WHERE supersedes_receipt_id=? ORDER BY source_global_sequence LIMIT 1",
                    (str(receipt.receipt_id),),
                ).fetchone()
                expected_superseded_sequence = (
                    None if expected_superseded is None else int(expected_superseded[0])
                )
                if (
                    envelope.global_sequence != int(row["source_global_sequence"])
                    or envelope.aggregate_id != receipt.field_id
                    or envelope.kind not in {EventKind.FIELD_OPTIMIZED, EventKind.FIELD_REGENERATED}
                    or not _field_receipt_event_matches(payload, receipt, receipt_projection)
                    or payload.get("upstream_field_revision") != int(row["upstream_field_revision"])
                    or payload.get("receipt_revision") != int(row["receipt_revision"])
                    or receipt.upstream_field_revision != int(row["upstream_field_revision"])
                    or receipt.receipt_revision != int(row["receipt_revision"])
                    or payload.get("field_revision_digest") != str(row["field_revision_digest"])
                    or payload.get("pipeline_digest") != str(row["pipeline_digest"])
                    or not isinstance(validation, dict)
                    or validation.get("operational_weight_authority_digest") != operational_digest
                    or not isinstance(operational, dict)
                    or operational.get("authority_digest") != operational_digest
                    or installed_weight is None
                    or payload.get("crn_assignments") != projected_crn
                    or str(row["receipt_id"]) != str(receipt.receipt_id)
                    or str(row["field_id"]) != str(receipt.field_id)
                    or str(row["caller_namespace"]) != receipt.caller_namespace
                    or str(row["request_identity"]) != str(receipt.request_identity)
                    or (
                        None
                        if row["supersedes_receipt_id"] is None
                        else str(row["supersedes_receipt_id"])
                    )
                    != (
                        None
                        if receipt.supersedes_receipt_id is None
                        else str(receipt.supersedes_receipt_id)
                    )
                    or (
                        None
                        if row["superseded_by_sequence"] is None
                        else int(row["superseded_by_sequence"])
                    )
                    != expected_superseded_sequence
                    or str(row["created_at"]) != envelope.occurred_at_utc
                ):
                    raise ProjectionError("field projection differs from event authority")
        self._verify_approval_projection()

    def _verify_approval_projection(self) -> None:
        """Compare every disposable approval row with an in-transaction rebuild."""

        with open_v3_connection(self._database_path) as connection:
            with immediate_transaction(connection):
                before = _approval_projection_material(connection)
                current_receipts = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM v3_field_receipts "
                        "WHERE superseded_by_sequence IS NULL"
                    ).fetchone()[0]
                )
                if before is None and current_receipts == 0:
                    return
                connection.execute("SAVEPOINT verify_approval_projection")
                try:
                    _rebuild_approval_snapshot_history_connection(
                        connection,
                        rebuilt_at="1970-01-01T00:00:00.000Z",
                    )
                    expected = _approval_projection_material(connection)
                finally:
                    connection.execute("ROLLBACK TO verify_approval_projection")
                    connection.execute("RELEASE verify_approval_projection")
                if before != expected:
                    raise ProjectionError("approval projection differs from canonical authority")

    def _verify_weight_connection(self, connection: sqlite3.Connection, binding: Any) -> None:
        row = connection.execute(
            "SELECT * FROM v3_field_weight_authorities WHERE binding_digest=?",
            (getattr(binding, "authority_digest", None),),
        ).fetchone()
        if row is None or self._verify_weight_row(row) != binding:
            raise ProjectionConflict("weight binding is not installed signed authority")

    def _verify_dependence_connection(self, connection: sqlite3.Connection, artifact: Any) -> None:
        row = connection.execute(
            "SELECT * FROM v3_field_dependence_authorities WHERE artifact_digest=?",
            (getattr(artifact, "artifact_digest", None),),
        ).fetchone()
        if row is None or self._verify_dependence_row(row) != artifact:
            raise ProjectionConflict("dependence artifact is not installed signed authority")

    def _resolve_disagreement_connection(
        self, connection: sqlite3.Connection, receipt_digest: str
    ) -> Any:
        from strathmark.v3.application.field_assembly import (
            AssemblyConflict,
            OperationalDisagreementReceipt,
        )
        from strathmark.v3.infrastructure.integrity import verify_manifest

        row = connection.execute(
            "SELECT * FROM v3_field_disagreement_authority_blobs WHERE receipt_digest=?",
            (receipt_digest,),
        ).fetchone()
        if row is None:
            return None
        try:
            stored_value = json.loads(str(row["authority_blob_json"]))
            if not isinstance(stored_value, dict):
                raise ProjectionConflict(
                    "operational disagreement authority blob projection differs"
                )
            if stored_value.get("schema_version") == _DISAGREEMENT_BLOB_PROJECTION_SCHEMA:
                expected_projection_fields = {
                    "schema_version",
                    "receipt_digest",
                    "field_revision_digest",
                    "bundle_digest",
                    "policy_manifest_digest",
                    "council_manifest_digest",
                    "authority_blob_reference",
                }
                if set(stored_value) != expected_projection_fields:
                    raise ProjectionConflict(
                        "operational disagreement authority blob projection differs"
                    )
                reference = BlobReferenceV2.from_dict(stored_value["authority_blob_reference"])
                if (
                    reference.digest != str(row["authority_blob_digest"])
                    or stored_value["receipt_digest"] != str(row["receipt_digest"])
                    or stored_value["field_revision_digest"] != str(row["field_revision_digest"])
                    or stored_value["bundle_digest"] != str(row["bundle_digest"])
                    or stored_value["policy_manifest_digest"] != str(row["policy_manifest_digest"])
                    or stored_value["council_manifest_digest"] != row["council_manifest_digest"]
                ):
                    raise ProjectionConflict(
                        "operational disagreement authority blob columns differ"
                    )
                value = json.loads(self._blob_store.read(reference))
            else:
                value = stored_value
                if canonical_digest(
                    value,
                    max_bytes=_MAX_DISAGREEMENT_AUTHORITY_BLOB_BYTES,
                    max_items=_MAX_DISAGREEMENT_AUTHORITY_BLOB_ITEMS,
                ) != str(row["authority_blob_digest"]):
                    raise ProjectionConflict(
                        "operational disagreement authority blob digest differs"
                    )
            if not isinstance(value, dict):
                raise ProjectionConflict("operational disagreement authority blob payload differs")
            authority = OperationalDisagreementReceipt.from_authority_dict(value)
            policy = verify_manifest(authority.policy_manifest, self._trust_store)
            if policy != {
                "schema_version": "strathmark-v3-field-disagreement-policy-authority-v1",
                "purpose": "field_disagreement_operational",
                "bundle_digest": str(row["bundle_digest"]),
                "policy": authority.decision.policy.to_dict(),
                "policy_digest": authority.decision.policy_digest,
            }:
                raise ProjectionConflict("operational disagreement authority blob policy differs")
            council_digest = None
            if authority.council_manifest is not None:
                council = verify_manifest(authority.council_manifest, self._trust_store)
                council_digest = authority.council_manifest.body_digest
                if (
                    council.get("schema_version")
                    != "strathmark-v3-field-council-audit-authority-v1"
                    or council.get("purpose") != "field_council_operational"
                    or council.get("field_revision_digest") != authority.field_revision_digest
                    or council.get("council_audit") != authority.decision.council_audit.to_dict()
                    or council.get("council_audit_digest")
                    != authority.decision.council_audit.audit_digest
                ):
                    raise ProjectionConflict(
                        "operational disagreement authority blob council differs"
                    )
            if (
                authority.receipt_digest != str(row["receipt_digest"])
                or authority.field_revision_digest != str(row["field_revision_digest"])
                or policy.get("bundle_digest") != str(row["bundle_digest"])
                or authority.policy_manifest.body_digest != str(row["policy_manifest_digest"])
                or council_digest != row["council_manifest_digest"]
            ):
                raise ProjectionConflict("operational disagreement authority blob columns differ")
            return authority
        except (
            AssemblyConflict,
            BlobStoreError,
            KeyError,
            TypeError,
            ValueError,
            V3Error,
        ) as exc:
            raise ProjectionConflict("operational disagreement authority blob is corrupt") from exc

    def _verify_weight_row(self, row: sqlite3.Row) -> Any:
        from strathmark.v3.application.field_assembly import OperationalWeightAuthority

        value = json.loads(str(row["binding_json"]))
        binding = OperationalWeightAuthority.from_dict(value)
        if binding.authority_digest != str(row["binding_digest"]):
            raise ProjectionError("stored weight binding identity differs")
        authority = EventEnvelope.from_dict(json.loads(str(row["manifest_json"])))
        if authority != self._find_weight_event_from_binding(binding):
            raise ProjectionError("stored weight event differs from causal ledger authority")
        return binding

    def _verify_dependence_row(self, row: sqlite3.Row) -> Any:
        from strathmark.v3.domain.joint_dependence import DependenceArtifact
        from strathmark.v3.infrastructure.integrity import (
            SignedManifest,
            verify_manifest,
        )

        value = json.loads(str(row["artifact_json"]))
        manifest = SignedManifest.from_dict(json.loads(str(row["manifest_json"])))
        payload = verify_manifest(manifest, self._trust_store)
        if payload != {
            "schema_version": "strathmark-v3-field-dependence-promotion-v1",
            "purpose": "field_dependence_operational",
            "artifact": value,
            "promotion_receipt_digest": value.get("promotion_receipt_digest"),
        }:
            raise ProjectionError("signed dependence manifest differs from stored artifact")
        artifact = DependenceArtifact.from_dict(value)
        if artifact.artifact_digest != str(row["artifact_digest"]):
            raise ProjectionError("stored dependence artifact identity differs")
        return artifact

    def _find_weight_event_from_binding(self, binding: Any) -> EventEnvelope:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            return self._find_weight_event(connection, binding)

    @staticmethod
    def _find_weight_event(connection: sqlite3.Connection, authority: Any) -> EventEnvelope:
        from strathmark.v3.application.field_assembly import (
            OperationalWeightAuthority,
            OperationalWeightKind,
            live_effective_weight_receipt_digest,
        )

        if not isinstance(authority, OperationalWeightAuthority):
            raise ProjectionConflict("operational weight authority is required")
        row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE global_sequence=?",
            (authority.authority_event_sequence,),
        ).fetchone()
        if row is None:
            raise ProjectionConflict("weight authority event is missing")
        event = EventEnvelope.from_dict(json.loads(str(row[0])))
        if (
            event.event_digest != authority.authority_event_digest
            or event.aggregate_kind is not AggregateKind.WEIGHTS
            or event.kind is not EventKind.WEIGHTS_CHANGED
            or event.aggregate_id
            != deterministic_identifier("weights", {"tournament_id": str(authority.tournament_id)})
        ):
            raise ProjectionConflict("weight authority event identity differs")
        payload = cast(InlinePayload, event.command.payload).to_value()
        binding = authority.binding
        epoch = connection.execute(
            "SELECT epoch_digest, maximum_tournament_sequence, round_id "
            "FROM v3_evidence_epochs WHERE epoch_id=?",
            (str(authority.epoch_id),),
        ).fetchone()
        if (
            epoch is None
            or str(epoch[0]) != authority.epoch_digest
            or int(epoch[1]) != authority.frozen_tournament_sequence
            or str(epoch[2]) != str(authority.round_id)
        ):
            raise ProjectionConflict("weight authority differs from frozen U5 epoch")
        round_row = connection.execute(
            "SELECT snapshot_json, tournament_id FROM v3_ingress_snapshots "
            "WHERE entity_kind='round' AND entity_id=? "
            "ORDER BY upstream_revision DESC LIMIT 1",
            (str(authority.round_id),),
        ).fetchone()
        if round_row is None or str(round_row[1]) != str(authority.tournament_id):
            raise ProjectionConflict("weight authority lacks round ingress")
        round_snapshot = json.loads(str(round_row[0]))
        if authority.kind is OperationalWeightKind.ROOT_BASELINE:
            SQLiteFieldProjectionStore._verify_root_weight_event(
                connection, authority, event, payload, round_snapshot
            )
        else:
            SQLiteFieldProjectionStore._verify_live_weight_event(
                connection, authority, event, payload, round_snapshot
            )
        if binding.source_global_sequence != event.global_sequence:
            raise ProjectionConflict("U13 weight binding source differs from U12 event")
        if binding.context.to_dict() != payload.get("context"):
            raise ProjectionConflict("U13 weight context differs from U12 event")
        expected_weights = [[item.value, weight] for item, weight in binding.weights]
        if authority.kind is OperationalWeightKind.LIVE_ROUND_FREEZE:
            if payload.get("weights") != expected_weights or (
                binding.weight_receipt_digest
                != live_effective_weight_receipt_digest(
                    event.event_digest, binding.context, binding.weights
                )
            ):
                raise ProjectionConflict("live effective weights differ from U13 binding")
        return event

    @staticmethod
    def _verify_root_weight_event(
        connection: sqlite3.Connection,
        authority: Any,
        event: EventEnvelope,
        payload: dict[str, Any],
        round_snapshot: dict[str, Any],
    ) -> None:
        if (
            payload.get("schema_version") != "strathmark-v3-tournament-baseline-snapshot-v1"
            or payload.get("tournament_id") != str(authority.tournament_id)
            or round_snapshot.get("predecessor_round_ids") != []
        ):
            raise ProjectionConflict("root baseline authority is not current for root round")
        opened_row = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_id=? AND event_kind=? "
            "ORDER BY global_sequence LIMIT 1",
            (str(authority.tournament_id), EventKind.TOURNAMENT_OPENED.value),
        ).fetchone()
        if opened_row is None:
            raise ProjectionConflict("root baseline lacks tournament open authority")
        opened = EventEnvelope.from_dict(json.loads(str(opened_row[0])))
        opened_payload = cast(InlinePayload, opened.command.payload).to_value()
        receipt = payload.get("receipt")
        if not isinstance(receipt, dict):
            raise ProjectionConflict("root baseline receipt is invalid")
        receipt_content = dict(receipt)
        receipt_digest = receipt_content.pop("receipt_digest", None)
        binding = authority.binding
        if (
            payload.get("tournament_open_sequence") != opened.global_sequence
            or payload.get("tournament_open_event_digest") != opened.event_digest
            or str(authority.round_id) not in opened_payload.get("root_round_ids", [])
            or receipt_digest != canonical_digest(receipt_content)
            or receipt_digest != authority.baseline_receipt_digest
            or receipt_digest != binding.weight_receipt_digest
            or receipt.get("context") != binding.context.to_dict()
            or receipt.get("weights") != [[item.value, weight] for item, weight in binding.weights]
            or receipt.get("calibration_cutoff_at_utc") != binding.calibration_cutoff_at_utc
            or receipt.get("policy_digest") != binding.policy_digest
            or payload.get("baseline_ledger_projection_digest") != binding.ledger_projection_digest
        ):
            raise ProjectionConflict("root baseline differs from U12 receipt authority")

    @staticmethod
    def _verify_live_weight_event(
        connection: sqlite3.Connection,
        authority: Any,
        _event: EventEnvelope,
        payload: dict[str, Any],
        round_snapshot: dict[str, Any],
    ) -> None:
        if (
            payload.get("schema_version") != "strathmark-v3-live-round-weight-freeze-v1"
            or payload.get("tournament_id") != str(authority.tournament_id)
            or payload.get("next_round_id") != str(authority.round_id)
            or payload.get("next_epoch_id") != str(authority.epoch_id)
            or payload.get("next_epoch_digest") != authority.epoch_digest
            or payload.get("completed_round_id") != str(authority.completed_round_id)
            or payload.get("round_close_event_digest") != authority.round_close_event_digest
            or payload.get("baseline_receipt_digest") != authority.baseline_receipt_digest
            or payload.get("ledger_projection_digest") != authority.binding.ledger_projection_digest
            or str(authority.completed_round_id)
            not in round_snapshot.get("predecessor_round_ids", [])
        ):
            raise ProjectionConflict("live round freeze differs from U12/U5 authority")
        baseline_rows = []
        for row in connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_id=? AND event_kind=?",
            (str(_event.aggregate_id), EventKind.WEIGHTS_CHANGED.value),
        ):
            candidate = EventEnvelope.from_dict(json.loads(str(row[0])))
            candidate_payload = cast(InlinePayload, candidate.command.payload).to_value()
            receipt = candidate_payload.get("receipt")
            if (
                candidate_payload.get("schema_version")
                == "strathmark-v3-tournament-baseline-snapshot-v1"
                and candidate_payload.get("tournament_id") == str(authority.tournament_id)
                and candidate_payload.get("context") == payload.get("context")
                and isinstance(receipt, dict)
                and receipt.get("receipt_digest") == authority.baseline_receipt_digest
            ):
                baseline_rows.append((candidate, candidate_payload, receipt))
        if len(baseline_rows) != 1:
            raise ProjectionConflict("live freeze lacks one exact baseline authority")
        _baseline_event, _baseline_payload, baseline_receipt = baseline_rows[0]
        baseline_content = dict(baseline_receipt)
        baseline_digest = baseline_content.pop("receipt_digest", None)
        if (
            baseline_digest != canonical_digest(baseline_content)
            or baseline_receipt.get("calibration_cutoff_at_utc")
            != authority.binding.calibration_cutoff_at_utc
            or baseline_receipt.get("policy_digest") != authority.binding.policy_digest
        ):
            raise ProjectionConflict("live freeze baseline policy authority differs")
        control_rows: list[tuple[EventEnvelope, dict[str, Any]]] = []
        for row in connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_id=? "
            "AND event_kind IN (?, ?, ?) ORDER BY global_sequence",
            (
                str(_event.aggregate_id),
                EventKind.LIVE_SUSPENDED.value,
                EventKind.LIVE_RESUMED.value,
                EventKind.EMERGENCY_STOPPED.value,
            ),
        ):
            control_event = EventEnvelope.from_dict(json.loads(str(row[0])))
            if control_event.global_sequence > _event.global_sequence:
                control_rows.append(
                    (
                        control_event,
                        cast(InlinePayload, control_event.command.payload).to_value(),
                    )
                )
        if control_rows:
            control_event, control_payload = control_rows[-1]
            state = control_payload.get("after")
        else:
            control_event, state = _event, payload.get("control_state")
        if (
            control_event.global_sequence != authority.control_event_sequence
            or control_event.event_digest != authority.control_event_digest
        ):
            raise ProjectionConflict("live weight control authority is stale")
        if not isinstance(state, dict) or not (
            state.get("enabled") is True
            and state.get("suspended") is False
            and state.get("emergency_stopped") is False
            and state.get("expired") is False
        ):
            raise ProjectionConflict("live round weights are not operationally enabled")
        close = connection.execute(
            "SELECT event_digest FROM v3_events WHERE aggregate_id=? AND event_kind=?",
            (str(authority.completed_round_id), EventKind.ROUND_CLOSED.value),
        ).fetchone()
        if close is None or str(close[0]) != authority.round_close_event_digest:
            raise ProjectionConflict("live weights lack exact predecessor close authority")

    def _verify_exact_receipt_row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Any:
        """Verify one exact retry through indexed local authority references."""

        from strathmark.v3.contracts.commands import CommandKind

        receipt = self._decode_receipt(row)
        receipt_projection = json.loads(str(row["receipt_json"]))
        event_row = connection.execute(
            "SELECT * FROM v3_events WHERE global_sequence=?",
            (row["source_global_sequence"],),
        ).fetchone()
        if event_row is None:
            raise ProjectionError("exact receipt lacks canonical event authority")
        event = EventEnvelope.from_dict(json.loads(str(event_row["envelope_json"])))
        persisted_event = (
            event.global_sequence,
            str(event.event_id),
            event.aggregate_kind.value,
            str(event.aggregate_id),
            event.aggregate_version,
            event.kind.value,
            canonical_bytes(event.to_dict()).decode(),
            event.event_digest,
            event.prior_global_digest,
            event.prior_aggregate_digest,
            event.occurred_at_utc,
            str(event.command.command_id),
        )
        observed_event = tuple(
            event_row[name]
            for name in (
                "global_sequence",
                "event_id",
                "aggregate_kind",
                "aggregate_id",
                "aggregate_version",
                "event_kind",
                "envelope_json",
                "event_digest",
                "prior_global_digest",
                "prior_aggregate_digest",
                "occurred_at_utc",
                "command_id",
            )
        )
        payload = cast(InlinePayload, event.command.payload).to_value()
        sections = {
            section.kind.value: cast(InlinePayload, section.payload).to_value()
            for section in receipt.sections
        }
        validation = sections.get("validations")
        credibility = sections.get("credibility")
        if not isinstance(validation, dict) or not isinstance(credibility, dict):
            raise ProjectionError("exact receipt authority sections are malformed")
        operational = credibility.get("operational_weight_authority")
        weight_digest = (
            None if not isinstance(operational, dict) else operational.get("authority_digest")
        )
        dependence_digest = validation.get("dependence_artifact_digest")
        capacity_digest = validation.get("capacity_authority_digest")
        disagreement = sections.get("disagreement")
        summary = (
            None if not isinstance(disagreement, dict) else disagreement.get("operational_receipt")
        )
        blob_valid = summary is None
        if isinstance(summary, dict):
            blob = connection.execute(
                "SELECT field_revision_digest, bundle_digest, policy_manifest_digest, "
                "council_manifest_digest FROM v3_field_disagreement_authority_blobs "
                "WHERE receipt_digest=?",
                (summary.get("receipt_digest"),),
            ).fetchone()
            blob_valid = bool(
                blob is not None
                and str(blob[0]) == str(row["field_revision_digest"])
                and str(blob[1]) in {item.digest for item in receipt.bundles}
                and str(blob[2]) == summary.get("policy_manifest_digest")
                and blob[3] == summary.get("council_manifest_digest")
            )
        command_id = deterministic_identifier(
            "assembly",
            {
                "caller_namespace": receipt.caller_namespace,
                "request_identity": str(receipt.request_identity),
            },
        )
        idempotency = connection.execute(
            "SELECT principal_id, idempotency_key, command_digest, result_schema_version, "
            "result_json, result_digest, first_global_sequence, last_global_sequence, "
            "event_set_digest, created_at FROM v3_idempotency_records "
            "WHERE principal_id=? AND idempotency_key=?",
            (str(event.command.actor_id), str(command_id)),
        ).fetchone()
        idempotency_valid = False
        if idempotency is not None:
            try:
                result_value = json.loads(str(idempotency[4]))
                expected_result: dict[str, Any] = {
                    "schema_version": "strathmark-v3-field-assembly-result-v1",
                    "crn_assignments": json.loads(str(row["crn_assignments_json"])),
                }
                if receipt_projection.get("schema_version") == _FIELD_RECEIPT_PROJECTION_SCHEMA:
                    expected_result["receipt_summary"] = receipt_projection["receipt_summary"]
                    expected_result["receipt_blob_reference"] = receipt_projection[
                        "receipt_blob_reference"
                    ]
                else:
                    expected_result["receipt"] = receipt.to_dict()
                event_set_digest = canonical_digest(
                    {
                        "schema_version": "strathmark-v3-event-set-v1",
                        "events": [
                            {
                                "global_sequence": event.global_sequence,
                                "event_id": str(event.event_id),
                                "event_digest": event.event_digest,
                            }
                        ],
                    }
                )
                idempotency_valid = bool(
                    str(idempotency[0]) == str(event.command.actor_id)
                    and str(idempotency[1]) == str(command_id)
                    and str(idempotency[2]) == canonical_digest(event.command.to_dict())
                    and str(idempotency[3]) == "strathmark-v3-field-assembly-result-v1"
                    and result_value == expected_result
                    and canonical_bytes(result_value).decode() == str(idempotency[4])
                    and str(idempotency[5]) == canonical_digest(result_value)
                    and int(idempotency[6]) == event.global_sequence
                    and int(idempotency[7]) == event.global_sequence
                    and str(idempotency[8]) == event_set_digest
                    and str(idempotency[9]) == str(row["created_at"])
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                idempotency_valid = False

        # Authenticate this event to its indexed aggregate head.  This walk is
        # bounded by one field's receipt/issue history and is independent of
        # unrelated tournament events or authority blobs.
        stream_rows = connection.execute(
            "SELECT * FROM v3_events WHERE aggregate_kind=? AND aggregate_id=? "
            "AND aggregate_version>=? ORDER BY aggregate_version",
            (
                AggregateKind.FIELD.value,
                str(receipt.field_id),
                event.aggregate_version,
            ),
        ).fetchall()
        stream_valid = bool(stream_rows)
        prior_stream_digest = event.prior_aggregate_digest
        prior_stream_version = event.aggregate_version - 1
        final_stream_event = event
        for stream_row in stream_rows:
            try:
                stream_event = EventEnvelope.from_dict(json.loads(str(stream_row["envelope_json"])))
            except (ContractError, TypeError, ValueError, json.JSONDecodeError):
                stream_valid = False
                break
            stream_valid = stream_valid and bool(
                stream_event.aggregate_kind is AggregateKind.FIELD
                and stream_event.aggregate_id == receipt.field_id
                and stream_event.aggregate_version == prior_stream_version + 1
                and stream_event.prior_aggregate_digest == prior_stream_digest
                and str(stream_row["event_digest"]) == stream_event.event_digest
                and int(stream_row["global_sequence"]) == stream_event.global_sequence
                and str(stream_row["aggregate_kind"]) == stream_event.aggregate_kind.value
                and str(stream_row["aggregate_id"]) == str(stream_event.aggregate_id)
                and int(stream_row["aggregate_version"]) == stream_event.aggregate_version
                and str(stream_row["event_kind"]) == stream_event.kind.value
            )
            prior_stream_digest = stream_event.event_digest
            prior_stream_version = stream_event.aggregate_version
            final_stream_event = stream_event
        head = connection.execute(
            "SELECT aggregate_version, event_digest FROM v3_aggregate_heads "
            "WHERE aggregate_kind=? AND aggregate_id=?",
            (AggregateKind.FIELD.value, str(receipt.field_id)),
        ).fetchone()
        stream_valid = stream_valid and bool(
            head is not None
            and int(head[0]) == final_stream_event.aggregate_version
            and str(head[1]) == final_stream_event.event_digest
            and stream_rows[0]["event_digest"] == event.event_digest
        )
        previous_global = connection.execute(
            "SELECT event_digest FROM v3_events WHERE global_sequence=?",
            (event.global_sequence - 1,),
        ).fetchone()
        next_global = connection.execute(
            "SELECT prior_global_digest FROM v3_events WHERE global_sequence=?",
            (event.global_sequence + 1,),
        ).fetchone()
        global_edge_valid = bool(
            (event.global_sequence == 1 and event.prior_global_digest == "0" * 64)
            or (
                previous_global is not None and str(previous_global[0]) == event.prior_global_digest
            )
        ) and bool(next_global is None or str(next_global[0]) == event.event_digest)
        predecessor = (
            None
            if receipt.supersedes_receipt_id is None
            else connection.execute(
                "SELECT receipt_revision, field_id FROM v3_field_receipts WHERE receipt_id=?",
                (str(receipt.supersedes_receipt_id),),
            ).fetchone()
        )
        successor = connection.execute(
            "SELECT receipt_id, source_global_sequence FROM v3_field_receipts "
            "WHERE supersedes_receipt_id=?",
            (str(receipt.receipt_id),),
        ).fetchone()
        supersession_valid = bool(
            (
                receipt.receipt_revision == 1
                and receipt.supersedes_receipt_id is None
                and predecessor is None
            )
            or (
                receipt.receipt_revision > 1
                and predecessor is not None
                and int(predecessor[0]) == receipt.receipt_revision - 1
                and str(predecessor[1]) == str(receipt.field_id)
            )
        ) and bool(
            (row["superseded_by_sequence"] is None and successor is None)
            or (
                successor is not None
                and int(row["superseded_by_sequence"]) == int(successor["source_global_sequence"])
            )
        )
        if (
            observed_event != persisted_event
            or event.kind not in {EventKind.FIELD_OPTIMIZED, EventKind.FIELD_REGENERATED}
            or event.aggregate_kind is not AggregateKind.FIELD
            or event.aggregate_id != receipt.field_id
            or event.command.kind not in {CommandKind.OPTIMIZE_FIELD, CommandKind.REGENERATE_FIELD}
            or event.command.target_aggregate != receipt.field_id
            or str(event.command.command_id) != str(command_id)
            or payload.get("schema_version") != "strathmark-v3-field-assembly-event-v1"
            or not _field_receipt_event_matches(payload, receipt, receipt_projection)
            or payload.get("upstream_field_revision") != receipt.upstream_field_revision
            or payload.get("receipt_revision") != receipt.receipt_revision
            or payload.get("field_revision_digest") != str(row["field_revision_digest"])
            or payload.get("pipeline_digest") != str(row["pipeline_digest"])
            or validation.get("field_revision_digest") != str(row["field_revision_digest"])
            or str(row["receipt_id"]) != str(receipt.receipt_id)
            or str(row["field_id"]) != str(receipt.field_id)
            or str(row["caller_namespace"]) != receipt.caller_namespace
            or str(row["request_identity"]) != str(receipt.request_identity)
            or str(row["receipt_digest"]) != receipt.content_digest
            or int(row["receipt_revision"]) != receipt.receipt_revision
            or int(row["upstream_field_revision"]) != receipt.upstream_field_revision
            or int(row["source_global_sequence"]) != event.global_sequence
            or str(row["created_at"]) != event.occurred_at_utc
            or (None if row["supersedes_receipt_id"] is None else str(row["supersedes_receipt_id"]))
            != (
                None
                if receipt.supersedes_receipt_id is None
                else str(receipt.supersedes_receipt_id)
            )
            or payload.get("crn_assignments") != json.loads(str(row["crn_assignments_json"]))
            or not idempotency_valid
            or not stream_valid
            or not global_edge_valid
            or not supersession_valid
            or connection.execute(
                "SELECT 1 FROM v3_field_weight_authorities WHERE binding_digest=?",
                (weight_digest,),
            ).fetchone()
            is None
            or connection.execute(
                "SELECT 1 FROM v3_field_dependence_authorities WHERE artifact_digest=?",
                (dependence_digest,),
            ).fetchone()
            is None
            or connection.execute(
                "SELECT 1 FROM v3_field_capacity_authorities WHERE authority_digest=?",
                (capacity_digest,),
            ).fetchone()
            is None
            or not blob_valid
        ):
            raise ProjectionError("exact receipt local authority verification failed")
        return receipt

    def _decode_receipt(self, row: sqlite3.Row) -> Any:
        projection_json = str(row["receipt_json"])
        receipt_digest = str(row["receipt_digest"])
        receipt_id = str(row["receipt_id"])
        value = json.loads(projection_json)
        signature = _receipt_blob_signature(value, self._blob_store)
        cached = self._verified_receipt_cache.get(receipt_id)
        if (
            cached is not None
            and cached.projection_json == projection_json
            and cached.receipt_digest == receipt_digest
            and cached.blob_signature == signature
        ):
            return cached.receipt
        receipt = _decode_field_receipt_projection(value, self._blob_store)
        if _receipt_blob_signature(value, self._blob_store) != signature:
            raise ProjectionError("field receipt blob changed during verification")
        if len(self._verified_receipt_cache) >= 128:
            self._verified_receipt_cache.pop(next(iter(self._verified_receipt_cache)))
        self._verified_receipt_cache[receipt_id] = _VerifiedReceiptCacheEntry(
            projection_json,
            receipt_digest,
            signature,
            receipt,
        )
        return receipt

    def _decode_result(self, row: sqlite3.Row, *, receipt: Any | None = None) -> Any:
        from strathmark.v3.application.field_assembly import AssemblyResult

        if receipt is None:
            receipt = self._decode_receipt(row)
        crn = tuple(tuple(item) for item in json.loads(str(row["crn_assignments_json"])))
        return AssemblyResult(receipt, canonical_bytes(receipt.to_dict()), crn)


def _project_approval_decision(
    connection: sqlite3.Connection,
    event: EventEnvelope,
    command: Any,
    decision: Any,
) -> None:
    """Validate one deliberate decision against the exact current compact snapshot."""

    from strathmark.v3.application.approval import (
        _VERIFIED_RECEIPT_AUTHORITY,
        ApprovalConflict,
        ApprovalConflictChange,
        ApprovalDecisionAction,
        ApprovalDecisionCommand,
        ApprovalDecisionReceipt,
        ApprovalLane,
        ApprovalRow,
        DecisionState,
    )

    if not isinstance(command, ApprovalDecisionCommand) or not isinstance(
        decision, ApprovalDecisionReceipt
    ):
        raise ProjectionError("approval projection requires typed decision material")
    before = _approval_projection_material(connection)
    connection.execute("SAVEPOINT verify_predecision_approval_projection")
    try:
        _rebuild_approval_projection_connection(
            connection,
            rebuilt_at="1970-01-01T00:00:00.000Z",
            through_sequence=event.global_sequence - 1,
        )
        expected = _approval_projection_material(connection)
    finally:
        connection.execute("ROLLBACK TO verify_predecision_approval_projection")
        connection.execute("RELEASE verify_predecision_approval_projection")
    if before != expected:
        raise ProjectionError("approval projection differs from canonical authority")
    meta = connection.execute(
        "SELECT snapshot_id FROM v3_approval_projection_meta WHERE tournament_id=?",
        (command.tournament_id,),
    ).fetchone()
    if meta is None:
        raise ApprovalConflict("approval projection is not initialized", ())
    selections = (*command.selected, *command.excluded)
    if str(meta[0]) != command.snapshot_id:
        replacements: list[tuple[str, str, int]] = []
        changes: list[ApprovalConflictChange] = []
        history = connection.execute(
            "SELECT snapshot_json, snapshot_digest FROM v3_approval_snapshot_history "
            "WHERE snapshot_id=? AND tournament_id=?",
            (command.snapshot_id, command.tournament_id),
        ).fetchone()
        if history is None:
            raise ApprovalConflict("approval snapshot history is unavailable", ())
        try:
            history_value = json.loads(str(history[0]))
            history_digest = canonical_digest(history_value)
            historical_rows = history_value["fields"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError("approval snapshot history is malformed") from exc
        if (
            history_digest != str(history[1])
            or command.snapshot_id != f"approval_snapshot:{history_digest}"
            or not isinstance(historical_rows, list)
        ):
            raise ProjectionError("approval snapshot history digest differs")
        old = {
            str(item[0]): (
                None if item[1] is None else str(item[1]),
                None if item[2] is None else int(item[2]),
                None if item[3] is None else int(item[3]),
                int(item[4]),
                str(item[6]),
            )
            for item in historical_rows
        }
        current_history = connection.execute(
            "SELECT snapshot_json, snapshot_digest FROM v3_approval_snapshot_history "
            "WHERE snapshot_id=? AND tournament_id=?",
            (str(meta[0]), command.tournament_id),
        ).fetchone()
        if current_history is None:
            raise ProjectionError("current approval snapshot history is unavailable")
        try:
            current_value = json.loads(str(current_history[0]))
            current_digest = canonical_digest(current_value)
            current_rows = current_value["fields"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError("current approval snapshot history is malformed") from exc
        if (
            current_digest != str(current_history[1])
            or str(meta[0]) != f"approval_snapshot:{current_digest}"
            or not isinstance(current_rows, list)
        ):
            raise ProjectionError("current approval snapshot history digest differs")
        current = {
            str(item[0]): (
                None if item[1] is None else str(item[1]),
                None if item[2] is None else int(item[2]),
                None if item[3] is None else int(item[3]),
                int(item[4]),
                str(item[6]),
            )
            for item in current_rows
        }
        conflict_fields, total_changes = _prioritized_approval_conflict_fields(
            old,
            current,
            priority_field_ids=tuple(item.field_id for item in selections),
            limit=100,
        )
        for field_id in conflict_fields:
            prior = old.get(field_id)
            replacement = current.get(field_id)
            if prior == replacement:
                continue
            if prior is None and replacement is not None:
                changes.append(
                    ApprovalConflictChange(
                        field_id,
                        None,
                        replacement[0],
                        (replacement[1] if replacement[1] is not None else replacement[3]),
                        "added_field",
                    )
                )
            elif replacement is None and prior is not None:
                changes.append(
                    ApprovalConflictChange(
                        field_id,
                        prior[0],
                        None,
                        None,
                        "removed_or_issued",
                    )
                )
            elif prior is not None and replacement is not None:
                if replacement[0] is not None:
                    replacements.append(
                        (
                            field_id,
                            replacement[0],
                            (replacement[1] if replacement[1] is not None else replacement[3]),
                        )
                    )
                changes.append(
                    ApprovalConflictChange(
                        field_id,
                        prior[0],
                        replacement[0],
                        (replacement[1] if replacement[1] is not None else replacement[3]),
                        (
                            "replacement_receipt"
                            if replacement[:4] != prior[:4]
                            else "material_state_changed"
                        ),
                    )
                )
        raise ApprovalConflict(
            "approval snapshot is stale",
            tuple(replacements),
            changes=tuple(changes),
            total_changes=total_changes,
        )
    rows: dict[str, ApprovalRow] = {}
    for selection in selections:
        stored = connection.execute(
            "SELECT row_json, row_digest FROM v3_approval_queue_rows "
            "WHERE tournament_id=? AND receipt_id=?",
            (command.tournament_id, selection.receipt_id),
        ).fetchone()
        if stored is None:
            raise ApprovalConflict("approval receipt is no longer current", ())
        try:
            row = ApprovalRow.from_dict(
                json.loads(str(stored[0])),
                _authority=_VERIFIED_RECEIPT_AUTHORITY,
            )
        except Exception as exc:
            raise ProjectionError("approval decision row is corrupt") from exc
        if row.row_digest != str(stored[1]):
            raise ProjectionError("approval decision row digest differs")
        if (
            row.field_id != selection.field_id
            or row.receipt_revision != selection.receipt_revision
            or row.upstream_field_revision != selection.upstream_field_revision
            or row.row_digest != selection.row_digest
            or row.call_order != selection.call_order
        ):
            raise ApprovalConflict("approval receipt revision is stale", ())
        if row.decision_state is not DecisionState.UNDECIDED:
            raise ApprovalConflict("approval receipt already has a decision", ())
        if row.lane in {ApprovalLane.STALE, ApprovalLane.INTEGRITY_BLOCKED}:
            raise ApprovalConflict("approval receipt is not decision-ready", ())
        rows[row.receipt_id] = row
    action = command.action
    if action is ApprovalDecisionAction.ORDINARY_BATCH_ACCEPT:
        if any(not rows[item.receipt_id].ordinary_batch_eligible for item in command.selected):
            raise ApprovalConflict("ordinary batch contains an ineligible receipt", ())
    elif action is ApprovalDecisionAction.DEGRADED_BATCH_ACCEPT:
        if any(not rows[item.receipt_id].degraded_batch_eligible for item in command.selected):
            raise ApprovalConflict("degraded batch contains an ineligible receipt", ())
    elif action is ApprovalDecisionAction.INDIVIDUAL_ACCEPT:
        row = rows[command.selected[0].receipt_id]
        if row.lane is ApprovalLane.MANUAL_ZERO and not _approval_verified_construction_submission(
            connection, row
        ):
            raise ApprovalConflict("zero-assessor construction requires a verified submission", ())
    elif action is ApprovalDecisionAction.OVERRIDE_SUBMITTED:
        row = rows[command.selected[0].receipt_id]
        override = _approval_verified_expected_time_override(connection, row)
        if override is None or (
            row.prior_receipt_id != command.superseded_receipt_id
            or str(override.override_receipt.actor) != command.actor_id
        ):
            raise ApprovalConflict("override lacks exact typed whole-field authority", ())
    expected_payload = {
        "schema_version": "strathmark-v3-approval-decision-event-v1",
        "command": command.to_dict(),
        "decision": decision.to_dict(),
    }
    payload_reference = event.command.payload
    if (
        not isinstance(payload_reference, InlinePayload)
        or payload_reference.to_value() != expected_payload
    ):
        raise ProjectionError("approval event payload differs from typed decision")
    _rebuild_approval_projection_connection(connection, rebuilt_at=command.submitted_at)


def _rebuild_approval_projection_connection(
    connection: sqlite3.Connection,
    *,
    rebuilt_at: str,
    through_sequence: int | None = None,
    tournament_id: str | None = None,
    rebuild_decisions: bool = True,
    prepared_receipts: Mapping[str, Any] | None = None,
    prepared_disagreements: Mapping[str, Any] | None = None,
    prepared_disagreement_digests: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Replace the compact view solely from canonical events and receipt material."""

    from strathmark.v3.application.approval import (
        _VERIFIED_RECEIPT_AUTHORITY,
        ApprovalFacts,
        ApprovalRow,
        DecisionState,
        FreshnessState,
        IntegrityState,
        derive_receipt_approval_facts,
    )
    from strathmark.v3.application.field_assembly import (
        render_verified_receipt_explanation,
    )
    from strathmark.v3.contracts.evidence import require_utc_milliseconds

    require_utc_milliseconds(rebuilt_at)
    if tournament_id is not None:
        require_identifier(tournament_id, expected_namespace="tournament")
    if through_sequence is not None and (
        isinstance(through_sequence, bool)
        or not isinstance(through_sequence, int)
        or through_sequence < 0
    ):
        raise ProjectionError("approval rebuild boundary must be nonnegative")
    if rebuild_decisions:
        _rebuild_approval_decisions_connection(connection, through_sequence=through_sequence)
    if tournament_id is None:
        connection.execute("DELETE FROM v3_approval_projection_meta")
        connection.execute("DELETE FROM v3_approval_details")
        connection.execute("DELETE FROM v3_approval_queue_rows")
        connection.execute("DELETE FROM v3_approval_schedule")
    else:
        connection.execute(
            "DELETE FROM v3_approval_projection_meta WHERE tournament_id=?",
            (tournament_id,),
        )
        connection.execute(
            "DELETE FROM v3_approval_details WHERE tournament_id=?",
            (tournament_id,),
        )
        connection.execute(
            "DELETE FROM v3_approval_queue_rows WHERE tournament_id=?",
            (tournament_id,),
        )
        connection.execute(
            "DELETE FROM v3_approval_schedule WHERE tournament_id=?",
            (tournament_id,),
        )
    schedules: dict[str, dict[str, Any]] = {}
    outstanding_deadlines: dict[str, dict[str, str]] = {}
    latest_ingress = connection.execute(
        "SELECT ingress.* FROM v3_ingress_snapshots ingress JOIN ("
        "SELECT entity_id, MAX(upstream_revision) AS revision "
        "FROM v3_ingress_snapshots WHERE entity_kind='field' "
        "AND (? IS NULL OR source_global_sequence<=?) GROUP BY entity_id"
        ") latest ON latest.entity_id=ingress.entity_id "
        "AND latest.revision=ingress.upstream_revision "
        "WHERE ingress.entity_kind='field' "
        "AND (? IS NULL OR ingress.tournament_id=?) "
        "ORDER BY ingress.source_global_sequence",
        (through_sequence, through_sequence, tournament_id, tournament_id),
    ).fetchall()
    for ingress in latest_ingress:
        try:
            snapshot = json.loads(str(ingress["snapshot_json"]))
        except (TypeError, ValueError) as exc:
            raise ProjectionError("approval schedule ingress is malformed") from exc
        if not isinstance(snapshot, dict) or "call_order" not in snapshot:
            continue
        tournament_id = str(
            require_identifier(ingress["tournament_id"], expected_namespace="tournament")
        )
        round_id = str(require_identifier(ingress["round_id"], expected_namespace="round"))
        field_id = str(require_identifier(ingress["entity_id"], expected_namespace="field"))
        call_order = snapshot.get("call_order")
        scheduled_at = snapshot.get("scheduled_at")
        deadline_at = snapshot.get("deadline_at")
        if (
            isinstance(call_order, bool)
            or not isinstance(call_order, int)
            or call_order < 0
            or not isinstance(scheduled_at, str)
            or not isinstance(deadline_at, str)
        ):
            raise ProjectionError("approval schedule authority is invalid")
        require_utc_milliseconds(scheduled_at)
        require_utc_milliseconds(deadline_at)
        schedule = {
            "tournament_id": tournament_id,
            "round_id": round_id,
            "field_id": field_id,
            "upstream_field_revision": int(ingress["upstream_revision"]),
            "call_order": call_order,
            "scheduled_at": scheduled_at,
            "deadline_at": deadline_at,
            "receipt_id": None,
            "receipt_revision": None,
            "receipt_upstream_field_revision": None,
            "source_global_sequence": int(ingress["source_global_sequence"]),
            "snapshot": snapshot,
        }
        schedules[field_id] = schedule
        outstanding_deadlines.setdefault(tournament_id, {})[field_id] = deadline_at
        connection.execute(
            "INSERT INTO v3_approval_schedule VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                tournament_id,
                round_id,
                field_id,
                schedule["upstream_field_revision"],
                call_order,
                scheduled_at,
                deadline_at,
                schedule["source_global_sequence"],
            ),
        )
    receipt_rows = connection.execute(
        "SELECT receipt.* FROM v3_field_receipts receipt "
        "WHERE receipt.superseded_by_sequence IS NULL "
        "AND (? IS NULL OR receipt.source_global_sequence<=?) "
        "AND (? IS NULL OR EXISTS (SELECT 1 FROM v3_approval_schedule schedule "
        "WHERE schedule.tournament_id=? AND schedule.field_id=receipt.field_id)) "
        "ORDER BY receipt.source_global_sequence",
        (
            through_sequence,
            through_sequence,
            tournament_id,
            tournament_id,
        ),
    ).fetchall()
    row_digests: dict[str, list[str]] = {}
    row_entries: dict[str, list[tuple[str, str, int, int, str]]] = {}
    lane_counts: dict[str, dict[str, int]] = {}
    decision_counts: dict[str, dict[str, int]] = {}
    blocked_counts: dict[str, int] = {}
    batch_eligible_counts: dict[str, int] = {}
    issued_counts: dict[str, int] = {}
    issued_field_ids: set[str] = set()
    for stored in receipt_rows:
        schedule = schedules.get(str(stored["field_id"]))
        if schedule is None:
            continue
        tournament_id = str(schedule["tournament_id"])
        closed = _approval_tournament_closed(
            connection, tournament_id, through_sequence=through_sequence
        )
        schedule["receipt_id"] = str(stored["receipt_id"])
        schedule["receipt_revision"] = int(stored["receipt_revision"])
        schedule["receipt_upstream_field_revision"] = int(stored["upstream_field_revision"])
        connection.execute(
            "UPDATE v3_approval_schedule SET receipt_id=? WHERE tournament_id=? AND field_id=?",
            (str(stored["receipt_id"]), tournament_id, str(stored["field_id"])),
        )
        issued = connection.execute(
            "SELECT 1 FROM v3_events WHERE aggregate_id=? AND event_kind=? "
            "AND global_sequence>? AND (? IS NULL OR global_sequence<=?) "
            "ORDER BY global_sequence LIMIT 1",
            (
                str(stored["field_id"]),
                EventKind.FIELD_ISSUED.value,
                int(stored["source_global_sequence"]),
                through_sequence,
                through_sequence,
            ),
        ).fetchone()
        if issued is not None:
            issued_counts[tournament_id] = issued_counts.get(tournament_id, 0) + 1
            issued_field_ids.add(str(stored["field_id"]))
            outstanding_deadlines.get(tournament_id, {}).pop(str(stored["field_id"]), None)
            continue
        if closed:
            outstanding_deadlines.get(tournament_id, {}).pop(str(stored["field_id"]), None)
            continue
        receipt, integrity, authority_event, authority_payload = _approval_receipt_authority(
            connection,
            stored,
            prepared_receipt=(
                None
                if prepared_receipts is None
                else prepared_receipts.get(str(stored["receipt_id"]))
            ),
        )
        operational_authority_available = _approval_operational_authority_available(
            connection,
            receipt,
            prepared_authorities=prepared_disagreements,
            prepared_authority_digests=prepared_disagreement_digests,
        )
        integrity = integrity and operational_authority_available
        call_order = schedule["call_order"]
        deadline_at = schedule["deadline_at"]
        snapshot = schedule["snapshot"]
        validation = _approval_validation_payload(receipt)
        u5_current = (
            schedule["upstream_field_revision"] == receipt.upstream_field_revision
            and snapshot.get("competitor_ids")
            == [str(item) for item in receipt.ordered_competitor_ids]
            and snapshot.get("target_context") == receipt.target_context.to_dict()
            and validation.get("tournament_id") == tournament_id
            and validation.get("round_id") == schedule["round_id"]
            and validation.get("field_id") == str(receipt.field_id)
            and validation.get("field_revision") == receipt.upstream_field_revision
            and validation.get("call_order") == call_order
            and validation.get("scheduled_at") == schedule["scheduled_at"]
            and validation.get("deadline_at") == deadline_at
        )
        prior = None
        if receipt.supersedes_receipt_id is not None:
            prior_stored = connection.execute(
                "SELECT receipt.* FROM v3_field_receipts receipt WHERE receipt_id=?",
                (str(receipt.supersedes_receipt_id),),
            ).fetchone()
            if prior_stored is None:
                integrity = False
            else:
                prior, prior_integrity, _prior_event, _prior_payload = _approval_receipt_authority(
                    connection, prior_stored
                )
                integrity = integrity and prior_integrity
        facts = derive_receipt_approval_facts(
            receipt,
            u5_current=u5_current,
            integrity_verified=integrity,
        )
        if not integrity and facts.integrity is not IntegrityState.BLOCKED:
            facts = ApprovalFacts(
                IntegrityState.BLOCKED,
                FreshnessState.CURRENT if u5_current else FreshnessState.STALE,
                facts.availability,
                facts.consequence,
                facts.zero_history,
                facts.council_degraded,
                facts.manual_construction,
                tuple(
                    sorted(
                        set(
                            (
                                *facts.reason_codes,
                                (
                                    "receipt_integrity_unverified"
                                    if operational_authority_available
                                    else "operational_authority_unavailable"
                                ),
                            )
                        )
                    )
                ),
                facts.availability_counts,
                facts.manual_mode,
                facts.flagged_competitor_ids,
                facts.flag_reason_tokens,
            )
        row = ApprovalRow._from_verified_material(
            receipt=receipt,
            prior=prior,
            facts=facts,
            call_order=int(call_order),
            deadline_at=str(deadline_at),
            _authority=_VERIFIED_RECEIPT_AUTHORITY,
        )
        decision_row = connection.execute(
            "SELECT decision_state FROM v3_approval_decision_projection "
            "WHERE tournament_id=? AND receipt_id=?",
            (tournament_id, row.receipt_id),
        ).fetchone()
        if decision_row is not None:
            row = row._with_decision(
                DecisionState(str(decision_row[0])),
                _authority=_VERIFIED_RECEIPT_AUTHORITY,
            )
        if row.decision_state not in {DecisionState.UNDECIDED, DecisionState.BLOCKED}:
            outstanding_deadlines.get(tournament_id, {}).pop(row.field_id, None)
        row_value = row.to_dict()
        detail = {
            "schema_version": "strathmark-v3-approval-detail-v1",
            "row": row_value,
            "receipt": receipt.to_dict(),
            "explanation": render_verified_receipt_explanation(receipt).to_dict(),
            "source_event_digest": authority_event.event_digest,
        }
        detail_digest = canonical_digest(detail)
        connection.execute(
            "INSERT INTO v3_approval_queue_rows VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tournament_id,
                str(schedule["round_id"]),
                row.field_id,
                row.receipt_id,
                row.receipt_revision,
                row.upstream_field_revision,
                row.call_order,
                row.deadline_at,
                row.lane.value,
                row.facts.consequence.value,
                row.decision_state.value,
                int(row.ordinary_batch_eligible),
                int(row.degraded_batch_eligible),
                canonical_bytes(row_value).decode(),
                row.row_digest,
                detail_digest,
                authority_event.global_sequence,
            ),
        )
        connection.execute(
            "INSERT INTO v3_approval_details VALUES (?, ?, ?, ?, ?)",
            (
                row.receipt_id,
                tournament_id,
                canonical_bytes(detail).decode(),
                detail_digest,
                authority_event.global_sequence,
            ),
        )
        row_digests.setdefault(tournament_id, []).append(row.row_digest)
        row_entries.setdefault(tournament_id, []).append(
            (
                row.field_id,
                row.receipt_id,
                row.receipt_revision,
                row.upstream_field_revision,
                row.row_digest,
            )
        )
        tournament_lanes = lane_counts.setdefault(tournament_id, {})
        tournament_lanes[row.lane.value] = tournament_lanes.get(row.lane.value, 0) + 1
        tournament_decisions = decision_counts.setdefault(tournament_id, {})
        tournament_decisions[row.decision_state.value] = (
            tournament_decisions.get(row.decision_state.value, 0) + 1
        )
        if row.lane.value == "integrity_blocked":
            blocked_counts[tournament_id] = blocked_counts.get(tournament_id, 0) + 1
        if row.ordinary_batch_eligible or row.degraded_batch_eligible:
            batch_eligible_counts[tournament_id] = batch_eligible_counts.get(tournament_id, 0) + 1
    tournament_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT tournament_id FROM v3_ingress_snapshots "
            "WHERE entity_kind='tournament' AND (? IS NULL OR source_global_sequence<=?) "
            "AND (? IS NULL OR tournament_id=?)",
            (through_sequence, through_sequence, tournament_id, tournament_id),
        )
    } | {str(item["tournament_id"]) for item in schedules.values()}
    snapshots: dict[str, str] = {}
    for tournament_id in sorted(tournament_ids):
        tournament_schedules = [
            item for item in schedules.values() if item["tournament_id"] == tournament_id
        ]
        scheduled_total = len(tournament_schedules)
        digests = row_digests.get(tournament_id, [])
        blocked_total = blocked_counts.get(tournament_id, 0)
        ready_total = len(digests) - blocked_total
        issued_total = issued_counts.get(tournament_id, 0)
        completed_total = ready_total + blocked_total + issued_total
        preparing_total = max(0, scheduled_total - completed_total)
        closed = _approval_tournament_closed(
            connection, tournament_id, through_sequence=through_sequence
        )
        if closed:
            ready_total = 0
            blocked_total = 0
            completed_total = scheduled_total
            preparing_total = 0
        lifecycle_state = (
            "closed"
            if closed
            else (
                "all_issued" if scheduled_total > 0 and issued_total == scheduled_total else "open"
            )
        )
        decision_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(source_global_sequence), 0) "
                "FROM v3_approval_command_projection WHERE tournament_id=?",
                (tournament_id,),
            ).fetchone()[0]
        )
        source_sequence = _approval_tournament_source_sequence(
            connection,
            tournament_id,
            field_ids=tuple(str(item["field_id"]) for item in tournament_schedules),
            through_sequence=through_sequence,
        )
        tournament_rows = {
            field_id: (receipt_id, receipt_revision, upstream_revision, row_digest)
            for (
                field_id,
                receipt_id,
                receipt_revision,
                upstream_revision,
                row_digest,
            ) in row_entries.get(tournament_id, [])
        }
        snapshot_fields: list[list[Any]] = []
        for item in tournament_schedules:
            field_id = str(item["field_id"])
            projected = tournament_rows.get(field_id)
            state_material = (
                "closed"
                if closed
                else (
                    "issued"
                    if field_id in issued_field_ids
                    else ("preparing" if projected is None else projected[3])
                )
            )
            snapshot_fields.append(
                [
                    field_id,
                    item["receipt_id"],
                    item["receipt_revision"],
                    item["receipt_upstream_field_revision"],
                    int(item["upstream_field_revision"]),
                    state_material,
                    canonical_digest(
                        {
                            "schema_version": "strathmark-v3-approval-snapshot-field-v1",
                            "field_id": field_id,
                            "receipt_revision": item["receipt_revision"],
                            "receipt_upstream_field_revision": item[
                                "receipt_upstream_field_revision"
                            ],
                            "upstream_field_revision": int(item["upstream_field_revision"]),
                            "receipt_id": item["receipt_id"],
                            "call_order": int(item["call_order"]),
                            "deadline_at": str(item["deadline_at"]),
                            "state": state_material,
                        }
                    ),
                ]
            )
        content = {
            "schema_version": "strathmark-v3-approval-projection-v1",
            "tournament_id": tournament_id,
            "source_global_sequence": source_sequence,
            "decision_global_sequence": decision_sequence,
            "lifecycle_state": lifecycle_state,
            "preparation_completed": completed_total,
            "preparation_total": scheduled_total,
            "preparing_count": preparing_total,
            "ready_count": ready_total,
            "blocked_count": blocked_total,
            "issued_count": issued_total,
            "fields": snapshot_fields,
        }
        digest = canonical_digest(content)
        snapshot_id = f"approval_snapshot:{digest}"
        counts = canonical_bytes(
            {
                "rows": len(digests),
                "lanes": dict(sorted(lane_counts.get(tournament_id, {}).items())),
                "decisions": dict(sorted(decision_counts.get(tournament_id, {}).items())),
                "preparing": preparing_total,
                "ready": ready_total,
                "blocked": blocked_total,
                "issued": issued_total,
                "batch_eligible": batch_eligible_counts.get(tournament_id, 0),
                "earliest_deadline_at": (
                    None
                    if closed
                    else min(
                        outstanding_deadlines.get(tournament_id, {}).values(),
                        default=None,
                    )
                ),
            }
        ).decode()
        connection.execute(
            "INSERT INTO v3_approval_projection_meta VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ",
            (
                tournament_id,
                snapshot_id,
                source_sequence,
                decision_sequence,
                lifecycle_state,
                completed_total,
                scheduled_total,
                preparing_total,
                ready_total,
                blocked_total,
                issued_total,
                len(digests),
                counts,
                digest,
                rebuilt_at,
            ),
        )
        encoded_snapshot = canonical_bytes(content).decode()
        connection.execute(
            "INSERT OR IGNORE INTO v3_approval_snapshot_history VALUES (?, ?, ?, ?, ?)",
            (
                snapshot_id,
                tournament_id,
                encoded_snapshot,
                digest,
                source_sequence,
            ),
        )
        for snapshot_field in snapshot_fields:
            field_id = str(snapshot_field[0])
            receipt_id = snapshot_field[1]
            receipt_revision = snapshot_field[2]
            upstream_revision = snapshot_field[3]
            row_digest = str(snapshot_field[6])
            if receipt_id is None:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO v3_approval_snapshot_rows VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    tournament_id,
                    field_id,
                    receipt_id,
                    receipt_revision,
                    upstream_revision,
                    row_digest,
                ),
            )
        stored_history = connection.execute(
            "SELECT snapshot_json, snapshot_digest FROM v3_approval_snapshot_history "
            "WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        stored_rows = connection.execute(
            "SELECT field_id, receipt_id, receipt_revision, "
            "upstream_field_revision, row_digest "
            "FROM v3_approval_snapshot_rows WHERE snapshot_id=? ORDER BY rowid",
            (snapshot_id,),
        ).fetchall()
        if (
            stored_history is None
            or str(stored_history[0]) != encoded_snapshot
            or str(stored_history[1]) != digest
            or [list(item) for item in stored_rows]
            != [
                [item[0], item[1], item[2], item[3], item[6]]
                for item in snapshot_fields
                if item[1] is not None
            ]
        ):
            raise ProjectionError("approval snapshot history differs from authority")
        snapshots[tournament_id] = snapshot_id
    for projected_tournament_id in snapshots:
        _write_approval_checkpoint(
            connection,
            projected_tournament_id,
            checkpointed_at=rebuilt_at,
            deep_verified=False,
        )
    return snapshots


def _refresh_approval_tournament_connection(
    connection: sqlite3.Connection,
    *,
    tournament_id: str,
    rebuilt_at: str,
    prepared_receipts: Mapping[str, Any] | None = None,
    prepared_disagreements: Mapping[str, Any] | None = None,
    prepared_disagreement_digests: Mapping[str, str] | None = None,
) -> str:
    """Refresh only one live tournament; full replay remains the recovery oracle."""

    snapshots = _rebuild_approval_projection_connection(
        connection,
        rebuilt_at=rebuilt_at,
        tournament_id=tournament_id,
        rebuild_decisions=False,
        prepared_receipts=prepared_receipts,
        prepared_disagreements=prepared_disagreements,
        prepared_disagreement_digests=prepared_disagreement_digests,
    )
    try:
        return snapshots[tournament_id]
    except KeyError as exc:
        raise ProjectionError(
            "approval tournament refresh lacks canonical tournament authority"
        ) from exc


def _rebuild_approval_snapshot_history_connection(
    connection: sqlite3.Connection, *, rebuilt_at: str
) -> dict[str, str]:
    """Replay every approval-relevant event boundary and restore durable snapshots."""

    require_utc_milliseconds(rebuilt_at)
    boundaries = {
        int(row[0])
        for row in connection.execute(
            "SELECT MAX(global_sequence) FROM v3_events WHERE command_id IN ("
            "SELECT command_id FROM v3_events WHERE event_kind IN "
            "(?, ?, ?, ?, ?, ?, ?, ?)) GROUP BY command_id",
            (
                EventKind.APPROVAL_DECISION_RECORDED.value,
                EventKind.FIELD_ROSTER_REVISED.value,
                EventKind.FIELD_ISSUED.value,
                EventKind.FIELD_OPTIMIZED.value,
                EventKind.FIELD_REGENERATED.value,
                EventKind.FIELD_SUPERSEDED.value,
                EventKind.TOURNAMENT_OPENED.value,
                EventKind.TOURNAMENT_CLOSED.value,
            ),
        )
    }
    connection.execute("DELETE FROM v3_approval_snapshot_rows")
    connection.execute("DELETE FROM v3_approval_snapshot_history")
    latest: dict[str, str] = {}
    ordered = sorted(boundaries)
    for index, boundary in enumerate(ordered):
        latest = _rebuild_approval_projection_connection(
            connection,
            rebuilt_at=(rebuilt_at if index == len(ordered) - 1 else "1970-01-01T00:00:00.000Z"),
            through_sequence=boundary,
        )
    if not ordered:
        latest = _rebuild_approval_projection_connection(connection, rebuilt_at=rebuilt_at)
    _verify_approval_snapshot_history(connection)
    return latest


def _approval_tournament_closed(
    connection: sqlite3.Connection,
    tournament_id: str,
    *,
    through_sequence: int | None,
) -> bool:
    latest = connection.execute(
        "SELECT event_kind FROM v3_events WHERE aggregate_id=? "
        "AND event_kind IN (?, ?) AND (? IS NULL OR global_sequence<=?) "
        "ORDER BY global_sequence DESC LIMIT 1",
        (
            tournament_id,
            EventKind.TOURNAMENT_OPENED.value,
            EventKind.TOURNAMENT_CLOSED.value,
            through_sequence,
            through_sequence,
        ),
    ).fetchone()
    return latest is not None and str(latest[0]) == EventKind.TOURNAMENT_CLOSED.value


def _approval_tournament_source_sequence(
    connection: sqlite3.Connection,
    tournament_id: str,
    *,
    field_ids: tuple[str, ...],
    through_sequence: int | None,
) -> int:
    candidates = [
        int(
            connection.execute(
                "SELECT COALESCE(MAX(source_global_sequence), 0) "
                "FROM v3_ingress_snapshots WHERE tournament_id=? "
                "AND (? IS NULL OR source_global_sequence<=?)",
                (tournament_id, through_sequence, through_sequence),
            ).fetchone()[0]
        ),
        int(
            connection.execute(
                "SELECT COALESCE(MAX(source_global_sequence), 0) "
                "FROM v3_approval_command_projection WHERE tournament_id=?",
                (tournament_id,),
            ).fetchone()[0]
        ),
        int(
            connection.execute(
                "SELECT COALESCE(MAX(global_sequence), 0) FROM v3_events "
                "WHERE aggregate_id=? AND event_kind IN (?, ?) "
                "AND (? IS NULL OR global_sequence<=?)",
                (
                    tournament_id,
                    EventKind.TOURNAMENT_OPENED.value,
                    EventKind.TOURNAMENT_CLOSED.value,
                    through_sequence,
                    through_sequence,
                ),
            ).fetchone()[0]
        ),
    ]
    for field_id in field_ids:
        candidates.append(
            int(
                connection.execute(
                    "SELECT COALESCE(MAX(source_global_sequence), 0) "
                    "FROM v3_field_receipts WHERE field_id=? "
                    "AND (? IS NULL OR source_global_sequence<=?)",
                    (field_id, through_sequence, through_sequence),
                ).fetchone()[0]
            )
        )
        candidates.append(
            int(
                connection.execute(
                    "SELECT COALESCE(MAX(global_sequence), 0) FROM v3_events "
                    "WHERE aggregate_id=? AND event_kind=? "
                    "AND (? IS NULL OR global_sequence<=?)",
                    (
                        field_id,
                        EventKind.FIELD_ISSUED.value,
                        through_sequence,
                        through_sequence,
                    ),
                ).fetchone()[0]
            )
        )
    return max(candidates)


def _approval_validation_payload(receipt: Any) -> dict[str, Any]:
    from strathmark.v3.contracts.receipts import ReceiptSectionKind

    section = next(
        (item for item in receipt.sections if item.kind is ReceiptSectionKind.VALIDATIONS),
        None,
    )
    if section is None or not isinstance(section.payload, InlinePayload):
        raise ProjectionError("approval receipt validation authority is unavailable")
    value = section.payload.to_value()
    if not isinstance(value, dict):
        raise ProjectionError("approval receipt validation authority is malformed")
    return value


def _approval_verified_supersession_material(
    connection: sqlite3.Connection, row: Any
) -> tuple[Any, dict[str, Any], sqlite3.Row] | None:
    """Resolve one exact current receipt and its predecessor from ledger authority."""

    stored = connection.execute(
        "SELECT receipt.* FROM v3_field_receipts receipt WHERE receipt_id=?",
        (row.receipt_id,),
    ).fetchone()
    if stored is None:
        return None
    try:
        receipt, integrity, _event, _payload = _approval_receipt_authority(connection, stored)
        validation = _approval_validation_payload(receipt)
    except Exception:
        return None
    prior = connection.execute(
        "SELECT receipt.* FROM v3_field_receipts receipt WHERE receipt_id=?",
        (str(receipt.supersedes_receipt_id),),
    ).fetchone()
    if (
        not integrity
        or prior is None
        or str(receipt.receipt_id) != row.receipt_id
        or receipt.receipt_revision != row.receipt_revision
        or receipt.upstream_field_revision != row.upstream_field_revision
        or str(receipt.field_id) != row.field_id
        or str(receipt.supersedes_receipt_id) != row.prior_receipt_id
        or str(prior["receipt_digest"]) != row.prior_receipt_content_digest
        or prior["superseded_by_sequence"] is None
        or int(prior["superseded_by_sequence"]) != int(stored["source_global_sequence"])
    ):
        return None
    return receipt, validation, prior


def _approval_verified_construction_submission(connection: sqlite3.Connection, row: Any) -> bool:
    """Recognize the separately audited construction that made 0/3 approvable."""

    from strathmark.v3.application.field_assembly import ManualConstructionSubmission

    material = _approval_verified_supersession_material(connection, row)
    if material is None:
        return False
    receipt, validation, prior = material
    value = validation.get("construction_submission")
    manual = validation.get("manual_authority")
    if not isinstance(value, dict) or not isinstance(manual, dict):
        return False
    expected_fields = {
        "schema_version",
        "prior_receipt_id",
        "prior_receipt_digest",
        "upstream_field_revision",
        "field_revision_digest",
        "manual_authority_digest",
        "actor_id",
        "reason_code",
        "scope",
        "submitted_at",
        "submission_digest",
    }
    if set(value) != expected_fields:
        return False
    try:
        decoded = ManualConstructionSubmission.create(
            **{
                key: item
                for key, item in value.items()
                if key not in {"schema_version", "submission_digest"}
            }
        )
    except Exception:
        return False
    return bool(
        canonical_bytes(decoded.to_dict()) == canonical_bytes(value)
        and str(decoded.prior_receipt_id) == str(receipt.supersedes_receipt_id)
        and decoded.prior_receipt_digest == str(prior["receipt_digest"])
        and decoded.upstream_field_revision == receipt.upstream_field_revision
        and decoded.field_revision_digest == validation.get("field_revision_digest")
        and decoded.manual_authority_digest == manual.get("authority_digest")
        and validation.get("expected_time_override") is None
    )


def _approval_verified_expected_time_override(
    connection: sqlite3.Connection, row: Any
) -> Any | None:
    """Decode and cross-bind exact U13 raw-time override authority for one receipt."""

    from strathmark.v3.application.field_assembly import (
        OperationalExpectedTimeOverrideAuthority,
    )
    from strathmark.v3.domain.disagreement import ExpectedTimeOverrideReceipt

    material = _approval_verified_supersession_material(connection, row)
    if material is None:
        return None
    receipt, validation, prior = material
    value = validation.get("expected_time_override")
    if not isinstance(value, dict) or validation.get("construction_submission") is not None:
        return None
    expected_fields = {
        "schema_version",
        "prior_receipt_id",
        "prior_receipt_digest",
        "upstream_field_revision",
        "field_revision_digest",
        "reason_code",
        "override_receipt",
        "after_optimizer_verification_digest",
        "authority_digest",
    }
    if set(value) != expected_fields or not isinstance(value.get("override_receipt"), dict):
        return None
    try:
        override_receipt = ExpectedTimeOverrideReceipt.from_dict(value["override_receipt"])
        decoded = OperationalExpectedTimeOverrideAuthority.create(
            prior_receipt_id=value["prior_receipt_id"],
            prior_receipt_digest=value["prior_receipt_digest"],
            upstream_field_revision=value["upstream_field_revision"],
            field_revision_digest=value["field_revision_digest"],
            reason_code=value["reason_code"],
            override_receipt=override_receipt,
            after_optimizer_verification_digest=value["after_optimizer_verification_digest"],
        )
    except Exception:
        return None
    if not (
        canonical_bytes(decoded.to_dict()) == canonical_bytes(value)
        and str(decoded.prior_receipt_id) == str(receipt.supersedes_receipt_id)
        and decoded.prior_receipt_digest == str(prior["receipt_digest"])
        and decoded.upstream_field_revision == receipt.upstream_field_revision
        and decoded.field_revision_digest == validation.get("field_revision_digest")
        and decoded.after_optimizer_verification_digest
        == validation.get("optimizer_verification_digest")
        and override_receipt.before_time_ms != override_receipt.after_time_ms
    ):
        return None
    return decoded


def _approval_operational_authority_available(
    connection: sqlite3.Connection,
    receipt: Any,
    *,
    prepared_authorities: Mapping[str, Any] | None = None,
    prepared_authority_digests: Mapping[str, str] | None = None,
) -> bool:
    from strathmark.v3.contracts.receipts import ReceiptSectionKind

    section = next(
        (item for item in receipt.sections if item.kind is ReceiptSectionKind.DISAGREEMENT),
        None,
    )
    if section is None or not isinstance(section.payload, InlinePayload):
        return False
    value = section.payload.to_value()
    if not isinstance(value, dict):
        return False
    operational = value.get("operational_receipt")
    if operational is None:
        return True
    if not isinstance(operational, dict):
        return False
    receipt_digest = operational.get("receipt_digest")
    row = connection.execute(
        "SELECT authority_blob_json, authority_blob_digest, bundle_digest "
        "FROM v3_field_disagreement_authority_blobs WHERE receipt_digest=?",
        (receipt_digest,),
    ).fetchone()
    if row is None:
        return False
    try:
        stored_value = json.loads(str(row[0]))
        prepared_authority = (
            None if prepared_authorities is None else prepared_authorities.get(str(receipt_digest))
        )
        if (
            isinstance(stored_value, dict)
            and stored_value.get("schema_version") == _DISAGREEMENT_BLOB_PROJECTION_SCHEMA
        ):
            reference = BlobReferenceV2.from_dict(stored_value["authority_blob_reference"])
            if reference.digest != str(row[1]):
                return False
            if prepared_authority is not None:
                prepared_digest = (
                    None
                    if prepared_authority_digests is None
                    else prepared_authority_digests.get(str(receipt_digest))
                )
                return bool(
                    prepared_digest == reference.digest
                    and prepared_authority.receipt_digest == receipt_digest
                    and str(row[2]) in {item.digest for item in receipt.bundles}
                )
            authority = json.loads(_field_blob_store_for_connection(connection).read(reference))
            authority_digest = reference.digest
        else:
            authority = stored_value
            authority_digest = canonical_digest(authority, max_bytes=16 * 1_024 * 1_024)
    except Exception:
        return False
    return bool(
        authority_digest == str(row[1])
        and isinstance(authority, dict)
        and authority.get("receipt_digest") == receipt_digest
        and str(row[2]) in {item.digest for item in receipt.bundles}
    )


def _approval_receipt_authority(
    connection: sqlite3.Connection,
    stored: sqlite3.Row,
    *,
    prepared_receipt: Any | None = None,
) -> tuple[Any, bool, EventEnvelope, dict[str, Any]]:
    """Load receipt authority from one canonical assembly event."""

    event_row = connection.execute(
        "SELECT envelope_json FROM v3_events WHERE global_sequence=?",
        (int(stored["source_global_sequence"]),),
    ).fetchone()
    if event_row is None:
        raise ProjectionError("approval receipt lacks canonical event authority")
    event = EventEnvelope.from_dict(json.loads(str(event_row[0])))
    payload_reference = event.command.payload
    if not isinstance(payload_reference, InlinePayload):
        raise ProjectionError("approval receipt event payload is not inline")
    payload = payload_reference.to_value()
    projection_value = json.loads(str(stored["receipt_json"]))
    receipt = (
        _decode_field_receipt_projection(
            projection_value, _field_blob_store_for_connection(connection)
        )
        if prepared_receipt is None
        else prepared_receipt
    )
    integrity = True
    if prepared_receipt is not None:
        projected = prepared_receipt
    else:
        try:
            projected = _decode_field_receipt_projection(
                projection_value, _field_blob_store_for_connection(connection)
            )
        except Exception:
            projected = None
            integrity = False
    integrity = integrity and bool(
        event.kind in {EventKind.FIELD_OPTIMIZED, EventKind.FIELD_REGENERATED}
        and event.aggregate_kind is AggregateKind.FIELD
        and event.aggregate_id == receipt.field_id
        and payload.get("schema_version") == "strathmark-v3-field-assembly-event-v1"
        and payload.get("receipt_revision") == receipt.receipt_revision
        and payload.get("upstream_field_revision") == receipt.upstream_field_revision
        and str(stored["receipt_id"]) == str(receipt.receipt_id)
        and str(stored["field_id"]) == str(receipt.field_id)
        and int(stored["receipt_revision"]) == receipt.receipt_revision
        and int(stored["upstream_field_revision"]) == receipt.upstream_field_revision
        and str(stored["receipt_digest"]) == receipt.content_digest
        and projected == receipt
        and _field_receipt_event_matches(payload, receipt, projection_value)
    )
    return receipt, integrity, event, payload


def _rebuild_approval_decisions_connection(
    connection: sqlite3.Connection, *, through_sequence: int | None = None
) -> int:
    """Replay approval decision projections from canonical EventStore authority."""

    from strathmark.v3.application.approval import (
        ApprovalDecisionAction,
        ApprovalDecisionCommand,
        ApprovalDecisionReceipt,
    )
    from strathmark.v3.contracts.commands import CommandKind

    connection.execute("DELETE FROM v3_approval_decision_projection")
    connection.execute("DELETE FROM v3_approval_command_projection")
    connection.execute("DELETE FROM v3_expected_time_override_states")
    latest = 0
    for stored in connection.execute(
        "SELECT envelope_json FROM v3_events WHERE event_kind=? "
        "AND (? IS NULL OR global_sequence<=?) ORDER BY global_sequence",
        (
            EventKind.APPROVAL_DECISION_RECORDED.value,
            through_sequence,
            through_sequence,
        ),
    ):
        event = EventEnvelope.from_dict(json.loads(str(stored[0])))
        payload_reference = event.command.payload
        if not isinstance(payload_reference, InlinePayload):
            raise ProjectionError("approval decision event payload is not inline")
        payload = payload_reference.to_value()
        try:
            command = ApprovalDecisionCommand.from_dict(payload["command"])
            decision = ApprovalDecisionReceipt.from_dict(payload["decision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError("approval decision event payload is invalid") from exc
        expected_id = deterministic_identifier(
            "approval_decision",
            {
                "tournament_id": command.tournament_id,
                "caller_namespace": command.caller_namespace,
                "request_identity": command.request_identity,
            },
        )
        expected_command_id = deterministic_identifier(
            "approval_command",
            {
                "tournament_id": command.tournament_id,
                "caller_namespace": command.caller_namespace,
                "request_identity": command.request_identity,
            },
        )
        if (
            payload.get("schema_version") != "strathmark-v3-approval-decision-event-v1"
            or decision != ApprovalDecisionReceipt.create(command)
            or event.aggregate_kind is not AggregateKind.APPROVAL_DECISION
            or event.aggregate_id != expected_id
            or event.command.kind is not CommandKind.RECORD_APPROVAL_DECISION
            or str(event.command.command_id) != str(expected_command_id)
            or str(event.command.actor_id) != command.actor_id
            or event.occurred_at_utc != command.submitted_at
        ):
            raise ProjectionError("approval decision differs from canonical authority")
        connection.execute(
            "INSERT INTO v3_approval_command_projection VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.global_sequence,
                command.tournament_id,
                command.caller_namespace,
                command.request_identity,
                command.command_digest,
                command.action.value,
                command.snapshot_id,
                canonical_bytes(command.to_dict()).decode(),
                canonical_bytes(decision.to_dict()).decode(),
                decision.decision_digest,
                event.event_digest,
                command.submitted_at,
            ),
        )
        selection_by_receipt = {
            item.receipt_id: item for item in (*command.selected, *command.excluded)
        }
        for receipt_id, state in decision.decisions:
            selection = selection_by_receipt[receipt_id]
            connection.execute(
                "INSERT INTO v3_approval_decision_projection VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    command.tournament_id,
                    selection.field_id,
                    selection.receipt_revision,
                    selection.upstream_field_revision,
                    state.value,
                    decision.decision_digest,
                    event.global_sequence,
                ),
            )
        if command.action is ApprovalDecisionAction.OVERRIDE_SUBMITTED:
            _project_accepted_expected_time_override(
                connection,
                event=event,
                command=command,
                selection=command.selected[0],
            )
        latest = event.global_sequence
    return latest


def _project_accepted_expected_time_override(
    connection: sqlite3.Connection,
    *,
    event: EventEnvelope,
    command: Any,
    selection: Any,
) -> None:
    from strathmark.v3.contracts.receipts import ReceiptSectionKind
    from strathmark.v3.domain.disagreement import AcceptedExpectedTimeOverrideState

    stored = connection.execute(
        "SELECT * FROM v3_field_receipts WHERE receipt_id=?",
        (selection.receipt_id,),
    ).fetchone()
    if stored is None:
        raise ProjectionError("accepted override receipt authority is missing")
    try:
        receipt, integrity, _receipt_event, _payload = _approval_receipt_authority(
            connection, stored
        )
        validation = _approval_validation_payload(receipt)
    except Exception as exc:
        raise ProjectionError("accepted override receipt authority is malformed") from exc
    prior = connection.execute(
        "SELECT receipt_digest FROM v3_field_receipts WHERE receipt_id=?",
        (str(receipt.supersedes_receipt_id),),
    ).fetchone()
    if not integrity or prior is None:
        raise ProjectionError("accepted override predecessor authority is missing")
    row = SimpleNamespace(
        receipt_id=str(receipt.receipt_id),
        receipt_revision=receipt.receipt_revision,
        upstream_field_revision=receipt.upstream_field_revision,
        field_id=str(receipt.field_id),
        prior_receipt_id=str(receipt.supersedes_receipt_id),
        prior_receipt_content_digest=str(prior[0]),
    )
    authority = _approval_verified_expected_time_override(connection, row)
    if authority is None:
        raise ProjectionError("accepted override lacks exact whole-field authority")
    override = authority.override_receipt
    sections = {
        item.kind: item.payload.to_value()
        for item in receipt.sections
        if isinstance(item.payload, InlinePayload)
    }
    pooled = sections.get(ReceiptSectionKind.POOLED_DISTRIBUTION)
    capability_revision = 0
    if isinstance(pooled, dict) and isinstance(pooled.get("bases"), list):
        for item in pooled["bases"]:
            if (
                isinstance(item, list)
                and len(item) == 2
                and item[0] == str(override.competitor_id)
                and isinstance(item[1], dict)
            ):
                basis = item[1]
                if basis.get("basis_kind") == "capability_pool" and isinstance(
                    basis.get("capability_binding"), dict
                ):
                    capability_revision = int(basis["capability_binding"]["state_revision"])
                elif basis.get("basis_kind") == "accepted_override_starting_estimate":
                    capability_revision = int(basis.get("current_capability_revision", 0))
                break
    state = AcceptedExpectedTimeOverrideState.create(
        override_id=override.override_id,
        competitor_id=override.competitor_id,
        tournament_id=require_identifier(command.tournament_id, expected_namespace="tournament"),
        target_context_digest=override.target_context_digest,
        expected_raw_time_ms=override.after_time_ms,
        scope=override.scope,
        scope_boundary_id=override.scope_boundary_id,
        accepted_field_id=receipt.field_id,
        accepted_round_id=require_identifier(
            validation.get("round_id"), expected_namespace="round"
        ),
        accepted_call_order=validation.get("call_order"),
        accepted_capability_revision=capability_revision,
        actor=override.actor,
        reason=override.reason,
        supersedes_override_id=override.supersedes_override_id,
        override_receipt_digest=override.receipt_digest,
        accepted_global_sequence=event.global_sequence,
        accepted_event_digest=event.event_digest,
    )
    existing = connection.execute(
        "SELECT override_id FROM v3_expected_time_override_states "
        "WHERE competitor_id=? AND tournament_id=? AND active=1",
        (str(state.competitor_id), str(state.tournament_id)),
    ).fetchall()
    if state.supersedes_override_id is None:
        if existing:
            raise ProjectionError("accepted override requires explicit supersession")
    elif len(existing) != 1 or str(existing[0][0]) != str(state.supersedes_override_id):
        raise ProjectionError("accepted override supersession target is not current")
    connection.execute(
        "INSERT INTO v3_expected_time_override_states VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,NULL)",
        (
            str(state.override_id),
            str(state.competitor_id),
            str(state.tournament_id),
            state.target_context_digest,
            state.scope.value,
            str(state.scope_boundary_id),
            str(state.accepted_field_id),
            str(state.accepted_round_id),
            state.accepted_call_order,
            state.accepted_capability_revision,
            canonical_bytes(state.to_dict()).decode(),
            state.state_digest,
            state.accepted_global_sequence,
            state.accepted_event_digest,
        ),
    )
    if state.supersedes_override_id is not None:
        connection.execute(
            "UPDATE v3_expected_time_override_states SET active=0,"
            "superseded_by_override_id=? WHERE override_id=? AND active=1",
            (str(state.override_id), str(state.supersedes_override_id)),
        )


def _prioritized_approval_conflict_fields(
    old: Mapping[str, object],
    current: Mapping[str, object],
    *,
    priority_field_ids: tuple[str, ...],
    limit: int,
) -> tuple[tuple[str, ...], int]:
    """Bound a stale diff without ever omitting a changed command member."""

    changed = {
        field_id
        for field_id in set(old) | set(current)
        if old.get(field_id) != current.get(field_id)
    }
    priority = tuple(
        field_id for field_id in dict.fromkeys(priority_field_ids) if field_id in changed
    )
    remaining = tuple(sorted(changed - set(priority)))
    return (priority + remaining)[:limit], len(changed)


def _approval_projection_material(connection: sqlite3.Connection) -> str | None:
    """Canonicalize all U15 projection tables while ignoring rebuild wall time."""

    _verify_approval_snapshot_history(connection)
    table_orders = {
        "v3_approval_projection_meta": "tournament_id",
        "v3_approval_queue_rows": "tournament_id, field_id",
        "v3_approval_details": "receipt_id",
        "v3_approval_schedule": "tournament_id, field_id",
        "v3_approval_command_projection": "source_global_sequence",
        "v3_approval_decision_projection": "receipt_id",
        "v3_approval_snapshot_history": "snapshot_id",
        "v3_approval_snapshot_rows": "snapshot_id, field_id",
        "v3_expected_time_override_states": "source_global_sequence",
    }
    material: dict[str, list[list[Any]]] = {}
    populated = False
    for table, order in table_orders.items():
        rows = [list(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        if table == "v3_approval_projection_meta":
            for row in rows:
                row[-1] = "<rebuilt-at>"
        if rows:
            populated = True
        material[table] = rows
    if not populated:
        return None
    return canonical_bytes(
        {
            "schema_version": "strathmark-v3-approval-projection-material-v1",
            "tables": material,
        }
    ).decode()


def _projection_checkpoint_digest(
    subject_id: str,
    source_global_sequence: int,
    source_event_digest: str,
    projection_digest: str,
    last_deep_verified_at: str,
) -> str:
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-projection-integrity-checkpoint-v1",
            "projection_kind": "approval",
            "subject_id": subject_id,
            "source_global_sequence": source_global_sequence,
            "source_event_digest": source_event_digest,
            "projection_digest": projection_digest,
            "last_deep_verified_at": last_deep_verified_at,
        }
    )


def _write_approval_checkpoint(
    connection: sqlite3.Connection,
    tournament_id: str,
    *,
    checkpointed_at: str,
    deep_verified: bool,
) -> None:
    meta = connection.execute(
        "SELECT projection_digest,source_global_sequence "
        "FROM v3_approval_projection_meta WHERE tournament_id=?",
        (tournament_id,),
    ).fetchone()
    if meta is None:
        return
    sequence = int(meta[1])
    source = (
        None
        if sequence == 0
        else connection.execute(
            "SELECT event_digest FROM v3_events WHERE global_sequence=?", (sequence,)
        ).fetchone()
    )
    if sequence > 0 and source is None:
        raise ProjectionError("approval projection source authority is missing")
    event_digest = ZERO_DIGEST if source is None else str(source[0])
    existing = connection.execute(
        "SELECT last_deep_verified_at FROM v3_projection_integrity_checkpoints "
        "WHERE projection_kind='approval' AND subject_id=?",
        (tournament_id,),
    ).fetchone()
    deep_at = checkpointed_at if deep_verified or existing is None else str(existing[0])
    require_utc_milliseconds(deep_at)
    projection_digest = str(meta[0])
    digest = _projection_checkpoint_digest(
        tournament_id, sequence, event_digest, projection_digest, deep_at
    )
    connection.execute(
        "INSERT INTO v3_projection_integrity_checkpoints VALUES "
        "('approval',?,?,?,?,?,?) ON CONFLICT(projection_kind,subject_id) DO UPDATE SET "
        "source_global_sequence=excluded.source_global_sequence,"
        "source_event_digest=excluded.source_event_digest,"
        "projection_digest=excluded.projection_digest,"
        "last_deep_verified_at=excluded.last_deep_verified_at,"
        "checkpoint_digest=excluded.checkpoint_digest",
        (tournament_id, sequence, event_digest, projection_digest, deep_at, digest),
    )


def _refresh_projection_deep_checkpoints(connection: sqlite3.Connection) -> None:
    verified_at = (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    for row in connection.execute(
        "SELECT tournament_id FROM v3_approval_projection_meta ORDER BY tournament_id"
    ):
        _write_approval_checkpoint(
            connection,
            str(row[0]),
            checkpointed_at=verified_at,
            deep_verified=True,
        )


def _verify_approval_checkpoint_connection(
    connection: sqlite3.Connection, tournament_id: str
) -> str:
    row = connection.execute(
        "SELECT source_global_sequence,source_event_digest,projection_digest,"
        "last_deep_verified_at,checkpoint_digest "
        "FROM v3_projection_integrity_checkpoints "
        "WHERE projection_kind='approval' AND subject_id=?",
        (tournament_id,),
    ).fetchone()
    meta = connection.execute(
        "SELECT snapshot_id,projection_digest FROM v3_approval_projection_meta "
        "WHERE tournament_id=?",
        (tournament_id,),
    ).fetchone()
    if row is None or meta is None:
        raise ProjectionError("approval projection checkpoint is missing")
    sequence = int(row[0])
    event_digest = str(row[1])
    projection_digest = str(row[2])
    deep_at = require_utc_milliseconds(str(row[3]))
    if str(row[4]) != _projection_checkpoint_digest(
        tournament_id, sequence, event_digest, projection_digest, deep_at
    ):
        raise ProjectionError("approval projection checkpoint digest differs")
    source = (
        None
        if sequence == 0
        else connection.execute(
            "SELECT event_digest FROM v3_events WHERE global_sequence=?", (sequence,)
        ).fetchone()
    )
    observed = (0, ZERO_DIGEST) if source is None else (sequence, str(source[0]))
    if observed != (sequence, event_digest):
        raise ProjectionError("approval projection checkpoint is stale at database head")
    history = connection.execute(
        "SELECT snapshot_json,snapshot_digest FROM v3_approval_snapshot_history "
        "WHERE snapshot_id=? AND tournament_id=?",
        (str(meta[0]), tournament_id),
    ).fetchone()
    if history is None:
        raise ProjectionError("approval snapshot checkpoint is missing")
    try:
        value = json.loads(str(history[0]))
    except Exception as exc:
        raise ProjectionError("approval snapshot checkpoint is malformed") from exc
    if (
        canonical_bytes(value).decode("utf-8") != str(history[0])
        or canonical_digest(value) != str(history[1])
        or str(history[1]) != projection_digest
        or str(meta[1]) != projection_digest
        or str(meta[0]) != f"approval_snapshot:{projection_digest}"
    ):
        raise ProjectionError("approval snapshot checkpoint digest differs")
    return projection_digest


def _verify_live_approval_projection_connection(
    connection: sqlite3.Connection,
) -> None:
    """Reject coordinated disposable-view tampering before exposing judge data."""

    before = _approval_projection_material(connection)
    connection.execute("SAVEPOINT verify_live_approval_projection")
    try:
        _rebuild_approval_projection_connection(
            connection,
            rebuilt_at="1970-01-01T00:00:00.000Z",
        )
        expected = _approval_projection_material(connection)
    finally:
        connection.execute("ROLLBACK TO verify_live_approval_projection")
        connection.execute("RELEASE verify_live_approval_projection")
    if before != expected:
        raise ProjectionError("approval projection differs from canonical authority")


def _verify_approval_snapshot_history(connection: sqlite3.Connection) -> None:
    """Verify content-addressed immutable snapshot history before stale recovery."""

    history_ids: set[str] = set()
    for stored in connection.execute(
        "SELECT snapshot_id, tournament_id, snapshot_json, snapshot_digest, "
        "source_global_sequence FROM v3_approval_snapshot_history ORDER BY snapshot_id"
    ):
        snapshot_id = str(stored[0])
        tournament_id = str(stored[1])
        try:
            value = json.loads(str(stored[2]))
            digest = canonical_digest(value)
            fields = value["fields"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError("approval snapshot history is malformed") from exc
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "tournament_id",
                "source_global_sequence",
                "decision_global_sequence",
                "lifecycle_state",
                "preparation_completed",
                "preparation_total",
                "preparing_count",
                "ready_count",
                "blocked_count",
                "issued_count",
                "fields",
            }
            or value.get("schema_version") != "strathmark-v3-approval-projection-v1"
            or value.get("tournament_id") != tournament_id
            or value.get("source_global_sequence") != int(stored[4])
            or digest != str(stored[3])
            or snapshot_id != f"approval_snapshot:{digest}"
            or not isinstance(fields, list)
        ):
            raise ProjectionError("approval snapshot history digest differs")
        expected_rows: list[list[Any]] = []
        seen_fields: set[str] = set()
        for item in fields:
            if (
                not isinstance(item, list)
                or len(item) != 7
                or not isinstance(item[0], str)
                or item[0] in seen_fields
                or (item[1] is not None and not isinstance(item[1], str))
                or (item[2] is not None and type(item[2]) is not int)
                or (item[3] is not None and type(item[3]) is not int)
                or type(item[4]) is not int
                or not isinstance(item[5], str)
                or not isinstance(item[6], str)
                or len(item[6]) != 64
            ):
                raise ProjectionError("approval snapshot field material is malformed")
            seen_fields.add(item[0])
            if item[1] is not None and item[2] is not None and item[3] is not None:
                expected_rows.append([item[0], item[1], item[2], item[3], item[6]])
        projected_rows = [
            list(item)
            for item in connection.execute(
                "SELECT field_id, receipt_id, receipt_revision, "
                "upstream_field_revision, row_digest "
                "FROM v3_approval_snapshot_rows WHERE snapshot_id=? "
                "ORDER BY field_id",
                (snapshot_id,),
            )
        ]
        if projected_rows != sorted(expected_rows, key=lambda item: item[0]):
            raise ProjectionError("approval snapshot rows differ from history")
        history_ids.add(snapshot_id)
    required_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT snapshot_id FROM v3_approval_projection_meta "
            "UNION SELECT snapshot_id FROM v3_approval_command_projection"
        )
    }
    if not required_ids.issubset(history_ids):
        raise ProjectionError("approval snapshot history is incomplete")


def _positive_sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProjectionError("source global sequence must be positive")
    return value


class SQLiteRollingLifecycleResolver:
    """Derive prospective card inputs only from canonical U5 SQLite authority."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        capacity_use: object,
        council_manifest_digest: str,
        trust_store: object,
    ) -> None:
        from strathmark.v3.application.capacity import CapacityUse
        from strathmark.v3.infrastructure.integrity import IntegrityTrustStore

        if not isinstance(capacity_use, CapacityUse):
            raise ProjectionError("rolling resolver capacity must be typed")
        _digest(council_manifest_digest)
        if not isinstance(trust_store, IntegrityTrustStore):
            raise ProjectionError("rolling resolver trust store must be typed")
        self._database_path = Path(database_path).expanduser().resolve(strict=False)
        self._capacity_use = capacity_use
        self._council_manifest_digest = council_manifest_digest
        self._trust_store = trust_store

    def resolve(self, events: tuple[EventEnvelope, ...]) -> object:
        from strathmark.v3.application.capacity import CapacityManifest
        from strathmark.v3.application.coordinator import (
            PreparationCandidate,
            PreparationClass,
            RollingLifecycleReactionPlan,
        )
        from strathmark.v3.application.field_assembly import FieldCapacityAuthority
        from strathmark.v3.contracts.evidence import EvidencePacket, ResultObservation
        from strathmark.v3.infrastructure.integrity import (
            SignedManifest,
            verify_manifest,
        )

        if not isinstance(events, tuple) or not events:
            raise ProjectionError("rolling resolver requires an immutable event set")
        if tuple(event.global_sequence for event in events) != tuple(
            range(events[0].global_sequence, events[-1].global_sequence + 1)
        ):
            raise ProjectionError("rolling resolver event set is not contiguous")
        source_sequence = events[-1].global_sequence
        field_ids: set[str] = set()
        tournament_ids: set[str] = set()
        affected_competitors: set[str] = set()
        round_ids: set[str] = set()
        frozen_epoch_triggered = False
        close_events = tuple(
            event
            for event in events
            if event.kind in {EventKind.ROUND_CLOSED, EventKind.TOURNAMENT_CLOSED}
        )
        for event in events:
            value = cast(InlinePayload, event.command.payload).to_value()
            if value.get("schema_version") == "strathmark-v3-correction-settlement-v1":
                value = value["result"]
            elif value.get(
                "schema_version"
            ) == "strathmark-v3-record-and-settle-live-race-v1" and event.kind in {
                EventKind.RESULT_RECORDED,
                EventKind.RESULT_SUPERSEDED,
            }:
                matches = [
                    item
                    for item in value.get("result_submissions", [])
                    if isinstance(item, dict) and item.get("result_key") == str(event.aggregate_id)
                ]
                if len(matches) != 1:
                    raise ProjectionError("atomic rolling result payload is missing")
                value = matches[0]
            if event.kind is EventKind.FIELD_ROSTER_REVISED:
                field_ids.add(str(value["entity_id"]))
                tournament_ids.add(str(value["tournament_id"]))
            elif event.kind is EventKind.FIELD_SUPERSEDED:
                field_ids.add(str(event.aggregate_id))
            elif event.kind in {EventKind.RESULT_RECORDED, EventKind.RESULT_SUPERSEDED}:
                submission = value["submission"]
                affected_competitors.add(str(submission["competitor_id"]))
                tournament_ids.add(str(submission["tournament_id"]))
            elif event.kind is EventKind.ROUND_EPOCH_FROZEN:
                epoch = value.get("epoch")
                if not isinstance(epoch, dict):
                    raise ProjectionError("rolling epoch payload is malformed")
                try:
                    round_id = require_identifier(epoch.get("round_id"), expected_namespace="round")
                except ContractError as exc:
                    raise ProjectionError("rolling epoch round identity is invalid") from exc
                round_ids.add(str(round_id))
                frozen_epoch_triggered = True
            elif event.kind is EventKind.ROUND_CLOSED:
                round_ids.add(str(event.aggregate_id))
            elif event.kind is EventKind.TOURNAMENT_CLOSED:
                tournament_ids.add(str(event.aggregate_id))
        candidates: list[PreparationCandidate] = []
        closed_epochs: list[StableIdentifier] = []
        if not (field_ids or tournament_ids or affected_competitors or round_ids or close_events):
            return RollingLifecycleReactionPlan(
                (), self._capacity_use, self._council_manifest_digest, ()
            )
        with open_v3_connection(self._database_path, read_only=True) as connection:
            for event in close_events:
                if event.kind is EventKind.ROUND_CLOSED:
                    rows = connection.execute(
                        "SELECT epoch_id FROM v3_evidence_epochs WHERE round_id=?",
                        (str(event.aggregate_id),),
                    )
                else:
                    rows = connection.execute(
                        "SELECT DISTINCT epoch.epoch_id FROM v3_evidence_epochs epoch "
                        "JOIN v3_ingress_snapshots ingress ON ingress.entity_kind='round' "
                        "AND ingress.entity_id=epoch.round_id WHERE ingress.tournament_id=?",
                        (str(event.aggregate_id),),
                    )
                closed_epochs.extend(StableIdentifier(str(row[0])) for row in rows)
            if close_events:
                return RollingLifecycleReactionPlan(
                    (),
                    self._capacity_use,
                    self._council_manifest_digest,
                    tuple(sorted(set(closed_epochs), key=str)),
                )
            field_predicates = []
            field_parameters: list[object] = [
                source_sequence,
                source_sequence,
                source_sequence,
            ]
            for values, column in (
                (field_ids, "ingress.entity_id"),
                (tournament_ids, "ingress.tournament_id"),
                (round_ids, "ingress.round_id"),
            ):
                if values:
                    ordered_values = sorted(values)
                    field_predicates.append(
                        f"AND {column} IN ({','.join('?' for _ in ordered_values)}) "
                    )
                    field_parameters.extend(ordered_values)
            field_parameters.append(self._capacity_use.context_cards + 1)
            field_rows = connection.execute(
                "SELECT ingress.entity_id,ingress.tournament_id,ingress.round_id,"
                "ingress.snapshot_json FROM v3_ingress_snapshots ingress "
                "LEFT JOIN v3_round_issue_seals seal ON seal.round_id=ingress.round_id "
                "AND seal.first_issue_global_sequence<=? "
                "WHERE ingress.entity_kind='field' AND seal.round_id IS NULL "
                "AND ingress.source_global_sequence<=? "
                "AND ingress.upstream_revision=(SELECT MAX(current.upstream_revision) "
                "FROM v3_ingress_snapshots current WHERE current.entity_kind='field' "
                "AND current.entity_id=ingress.entity_id "
                "AND current.source_global_sequence<=?) "
                + "".join(field_predicates)
                + "ORDER BY ingress.entity_id LIMIT ?",
                tuple(field_parameters),
            ).fetchall()
            if len(field_rows) > self._capacity_use.context_cards:
                raise ProjectionConflict("rolling field contexts exceed declared capacity")
            selected_fields = []
            for row in field_rows:
                if field_ids and str(row[0]) not in field_ids:
                    continue
                if round_ids and str(row[2]) not in round_ids:
                    continue
                if tournament_ids and str(row[1]) not in tournament_ids:
                    continue
                selected_fields.append(row)
            if not selected_fields:
                return RollingLifecycleReactionPlan(
                    (), self._capacity_use, self._council_manifest_digest, ()
                )
            minimum_call_order = min(
                int(json.loads(str(row[3]))["call_order"]) for row in selected_fields
            )
            for row in selected_fields:
                snapshot = json.loads(str(row[3]))
                tournament_id = str(row[1])
                round_id = str(row[2])
                context = TargetContext.from_dict(snapshot["target_context"])
                tournament = connection.execute(
                    "SELECT snapshot_json FROM v3_ingress_snapshots WHERE "
                    "entity_kind='tournament' AND entity_id=? AND source_global_sequence<=? "
                    "ORDER BY upstream_revision DESC LIMIT 1",
                    (tournament_id, source_sequence),
                ).fetchone()
                epoch = connection.execute(
                    "SELECT epoch_id,historical_cutoff_key,maximum_tournament_sequence "
                    "FROM v3_evidence_epochs "
                    "WHERE round_id=? AND frozen_global_sequence<=? "
                    "ORDER BY epoch_revision DESC LIMIT 1",
                    (round_id, source_sequence),
                ).fetchone()
                if epoch is None:
                    epoch = connection.execute(
                        "SELECT epoch.epoch_id,epoch.historical_cutoff_key,"
                        "epoch.maximum_tournament_sequence "
                        "FROM v3_evidence_epochs epoch JOIN v3_ingress_snapshots ingress "
                        "ON ingress.entity_kind='round' AND ingress.entity_id=epoch.round_id "
                        "WHERE ingress.tournament_id=? AND epoch.frozen_global_sequence<=? "
                        "ORDER BY epoch.frozen_global_sequence DESC LIMIT 1",
                        (tournament_id, source_sequence),
                    ).fetchone()
                capacity_row = connection.execute(
                    "SELECT bundle_digest,capacity_manifest_json,signed_manifest_json "
                    "FROM v3_field_capacity_authorities WHERE authority_digest=?",
                    (snapshot["capacity_authority_digest"],),
                ).fetchone()
                if tournament is None or epoch is None or capacity_row is None:
                    raise ProjectionConflict("rolling field causal authority is incomplete")
                capacity = CapacityManifest.from_dict(json.loads(str(capacity_row[1])))
                manifest = SignedManifest.from_dict(json.loads(str(capacity_row[2])))
                verify_manifest(manifest, self._trust_store)
                FieldCapacityAuthority(
                    capacity,
                    str(capacity_row[0]),
                    manifest,
                    str(snapshot["capacity_authority_digest"]),
                )
                tournament_snapshot = json.loads(str(tournament[0]))
                roster = tuple(str(item) for item in snapshot["competitor_ids"])
                competitors = (
                    tuple(sorted(affected_competitors)) if affected_competitors else roster
                )
                for competitor_id in competitors:
                    observation_rows = connection.execute(
                        "SELECT observation_json FROM v3_result_revisions revision "
                        "WHERE revision.tournament_id=? AND revision.competitor_id=? "
                        "AND revision.source_global_sequence<=? AND revision.revision=("
                        "SELECT MAX(current.revision) FROM v3_result_revisions current "
                        "WHERE current.result_key=revision.result_key AND "
                        "current.source_global_sequence<=?) ORDER BY source_global_sequence "
                        "LIMIT 257",
                        (
                            tournament_id,
                            competitor_id,
                            source_sequence,
                            source_sequence,
                        ),
                    ).fetchall()
                    if len(observation_rows) > 256:
                        raise ProjectionConflict(
                            "rolling evidence packet exceeds bounded observation capacity"
                        )
                    packet = EvidencePacket.create(
                        competitor_id=StableIdentifier(competitor_id),
                        target_context=context,
                        observations=tuple(
                            ResultObservation.from_dict(json.loads(str(item[0])))
                            for item in observation_rows
                        ),
                        taxonomy_version=context.taxonomy_version,
                        conversion_version=context.conversion_version,
                        historical_cutoff_key=json.loads(str(tournament[0]))[
                            "historical_cutoff_key"
                        ],
                        tournament_epoch_id=StableIdentifier(str(epoch[0])),
                        tournament_event_sequence=(
                            int(epoch[2]) if frozen_epoch_triggered else source_sequence
                        ),
                    )
                    if competitor_id in roster:
                        preparation_class = (
                            PreparationClass.IMMINENT_FIELD
                            if int(snapshot["call_order"]) == minimum_call_order
                            else PreparationClass.SCHEDULED
                        )
                    else:
                        preparation_class = PreparationClass.PLAUSIBLE_QUALIFIER
                    candidates.append(
                        PreparationCandidate.create(
                            competitor_id=competitor_id,
                            target_context_digest=context.digest,
                            historical_cutoff_key=packet.historical_cutoff_key,
                            tournament_epoch_id=str(packet.tournament_epoch_id),
                            bundle_digest=str(capacity_row[0]),
                            evidence_digest=packet.content_digest,
                            dependency_revision=source_sequence,
                            preparation_class=preparation_class,
                            hard_deadline_at=snapshot["deadline_at"],
                            evidence_packet=packet,
                        )
                    )
        if len(candidates) > self._capacity_use.context_cards:
            raise ProjectionConflict("rolling candidates exceed declared context capacity")
        return RollingLifecycleReactionPlan(
            tuple(candidates), self._capacity_use, self._council_manifest_digest, ()
        )


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractError("output digest must be a lower-case SHA-256 digest")
    return value


def _rolling_obligation_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT reaction_id,source_command_id,first_global_sequence,"
        "last_global_sequence,event_ids_json,event_set_digest,registered_at "
        "FROM v3_rolling_reaction_obligations "
        "ORDER BY first_global_sequence,reaction_id"
    ).fetchall()
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-rolling-reaction-obligations-v1",
            "rows": [list(row) for row in rows],
        }
    )


__all__ = [
    "ProjectionConflict",
    "ProjectionError",
    "SQLiteProjectionStore",
    "SQLiteFieldProjectionStore",
    "SQLiteRollingLifecycleResolver",
]
