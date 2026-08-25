"""Append-only SQLite event authority with atomic idempotent commands.

All payload construction happens before ``BEGIN IMMEDIATE``.  The transaction
contains only bounded validation, canonical envelope assembly, and row writes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strathmark.v3.application.commands import (
    CommandRequest,
    EventIntent,
    validate_command_event_intents,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandKind, InlinePayload
from strathmark.v3.contracts.errors import ContractError, V3Error
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import deterministic_identifier
from strathmark.v3.domain.state_machines import replay, transition
from strathmark.v3.infrastructure.integrity import (
    CriticalDatabaseCommit,
    CriticalIssueCoordinator,
    CriticalIssueIntent,
)
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import (
    EXPECTED_SCHEMA_DIGEST,
    canonical_schema_digest,
    migrate_connection,
)

ZERO_DIGEST = "0" * 64
EVENT_SET_SCHEMA_VERSION = "strathmark-v3-event-set-v1"
MAX_RESULT_BYTES = 1_048_576


class EventStoreError(V3Error, RuntimeError):
    """Base event-authority failure."""

    code = "event_store_error"


class EventStoreConflict(EventStoreError):
    """A legal command cannot commit against current authoritative state."""

    code = "event_store_conflict"


class EventStoreIntegrityError(EventStoreError):
    """Persisted event authority cannot be cryptographically replayed."""

    code = "event_store_integrity_error"


class InjectedEventStoreFailure(EventStoreError):
    """Test-only fault used to prove transaction rollback."""

    code = "injected_event_store_failure"


@dataclass(frozen=True, slots=True)
class StoredCommandResult:
    """Exact immutable result bytes and the event set that produced them."""

    result_schema_version: str
    result_bytes: bytes
    result_digest: str
    first_global_sequence: int
    last_global_sequence: int
    event_set_digest: str
    event_ids: tuple[str, ...]

    def value(self) -> dict[str, Any]:
        decoded = json.loads(self.result_bytes)
        if not isinstance(decoded, dict):
            raise EventStoreIntegrityError("stored command result is not an object")
        return decoded


@dataclass(frozen=True, slots=True)
class AuthorityAnchor:
    """Externally trusted global tip used to detect coherent tail truncation.

    The store can detect internal gaps and inconsistent truncation by replaying
    SQLite alone.  A deletion that coherently removes a tail event, its head,
    and its idempotency record is indistinguishable from a legitimate shorter
    history without an independently retained checkpoint.  U6 will sign this
    exact value; U4 accepts and verifies it without inventing a parallel ledger.
    """

    global_sequence: int
    event_digest: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.global_sequence, bool)
            or not isinstance(self.global_sequence, int)
            or self.global_sequence < 0
        ):
            raise EventStoreError("anchor global_sequence must be a non-negative integer")
        if (
            not isinstance(self.event_digest, str)
            or len(self.event_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.event_digest)
        ):
            raise EventStoreError("anchor event_digest must be a lower-case SHA-256 digest")
        if self.global_sequence == 0 and self.event_digest != ZERO_DIGEST:
            raise EventStoreError("an empty authority anchor must use the zero digest")
        if self.global_sequence > 0 and self.event_digest == ZERO_DIGEST:
            raise EventStoreError("a nonempty authority anchor cannot use the zero digest")


FaultHook = Callable[[str], None]
ProjectionHook = Callable[[sqlite3.Connection, tuple[EventEnvelope, ...]], None]


class SQLiteEventStore:
    """One connection-per-operation adapter over the U3 authority schema."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        trusted_anchor: AuthorityAnchor | None = None,
    ) -> None:
        if isinstance(database_path, bool) or not isinstance(database_path, (Path, str)):
            raise EventStoreError("database_path must be a filesystem path")
        self._database_path = Path(database_path).expanduser().resolve(strict=False)
        if trusted_anchor is not None and not isinstance(trusted_anchor, AuthorityAnchor):
            raise EventStoreError("trusted_anchor must be an AuthorityAnchor")
        self._trusted_anchor = trusted_anchor
        try:
            with open_v3_connection(self._database_path) as connection:
                migrate_connection(connection)
                self._verify_connection(connection)
        except EventStoreIntegrityError:
            raise
        except Exception as exc:
            raise EventStoreIntegrityError("event-store startup verification failed") from exc

    @classmethod
    def from_checkpoint_registry(
        cls, database_path: Path | str, checkpoint_registry: Any
    ) -> SQLiteEventStore:
        """Open authority only after the external signed checkpoint is present.

        The lazy structural check avoids coupling V2/core imports to the optional
        cryptography runtime while ensuring callers cannot substitute an unsigned
        ``AuthorityAnchor`` at the trusted startup boundary.
        """

        from strathmark.v3.infrastructure.integrity import CheckpointRegistry

        if not isinstance(checkpoint_registry, CheckpointRegistry):
            raise EventStoreError("trusted startup requires a CheckpointRegistry")
        checkpoint = checkpoint_registry.verify_database(database_path, require_current=False)
        return cls(
            database_path,
            trusted_anchor=AuthorityAnchor(
                checkpoint.authority_sequence, checkpoint.authority_digest
            ),
        )

    @property
    def database_path(self) -> Path:
        return self._database_path

    def execute(
        self,
        request: CommandRequest,
        *,
        fault_hook: FaultHook | None = None,
        projection_hook: ProjectionHook | None = None,
    ) -> StoredCommandResult:
        """Commit one command or return its exact prior result on retry."""

        if not isinstance(request, CommandRequest):
            raise EventStoreError("execute requires a validated CommandRequest")
        if fault_hook is not None and not callable(fault_hook):
            raise EventStoreError("fault_hook must be callable")
        if projection_hook is not None and not callable(projection_hook):
            raise EventStoreError("projection_hook must be callable")

        # Canonical bounded work is deliberately completed before taking the
        # writer lock.  Exact retry resolution below performs no new state work.
        command_digest = canonical_digest(request.command.to_dict())
        result_bytes = canonical_bytes(request.result, max_bytes=MAX_RESULT_BYTES)
        result_digest = canonical_digest(request.result, max_bytes=MAX_RESULT_BYTES)
        intents = tuple(sorted(request.events, key=lambda item: str(item.aggregate_id)))

        try:
            with open_v3_connection(self._database_path) as connection:
                with immediate_transaction(connection):
                    from strathmark.v3.infrastructure.sqlite.rolling_restart import (
                        RollingRestartIntegrityError,
                        require_rolling_reaction_cursor_at_event_head,
                    )

                    try:
                        require_rolling_reaction_cursor_at_event_head(connection)
                    except RollingRestartIntegrityError as exc:
                        raise EventStoreIntegrityError(str(exc)) from exc
                    if request.command.kind in {
                        CommandKind.ACKNOWLEDGE_ISSUE,
                        CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
                    }:
                        # R17.10 makes every issue response, including exact
                        # retry lookup, a stronger integrity boundary. Replay
                        # under the same lock before returning or appending.
                        self._verify_connection(connection)
                    retry = self._resolve_retry(
                        connection,
                        principal_id=str(request.principal_id),
                        idempotency_key=str(request.command.command_id),
                        command_digest=command_digest,
                    )
                    if retry is not None:
                        return retry
                    self._validate_versions_and_transitions(connection, request, intents)
                    events = self._append_events(
                        connection,
                        request,
                        intents,
                        command_digest=command_digest,
                        fault_hook=fault_hook,
                    )
                    if projection_hook is not None:
                        projection_hook(connection, events)
                    from strathmark.v3.infrastructure.sqlite.rolling_restart import (
                        advance_rolling_reaction_cursor,
                    )

                    advance_rolling_reaction_cursor(connection, events)
                    _fault(fault_hook, "after_projection")
                    _fault(fault_hook, "before_result")
                    event_set_digest = _event_set_digest(events)
                    connection.execute(
                        "INSERT INTO v3_idempotency_records(principal_id, idempotency_key, "
                        "command_digest, result_schema_version, result_json, result_digest, "
                        "first_global_sequence, last_global_sequence, event_set_digest, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(request.principal_id),
                            str(request.command.command_id),
                            command_digest,
                            request.result_schema_version,
                            result_bytes.decode("utf-8"),
                            result_digest,
                            events[0].global_sequence,
                            events[-1].global_sequence,
                            event_set_digest,
                            request.occurred_at_utc,
                        ),
                    )
                    _fault(fault_hook, "after_result")
                    return _stored_result(
                        request.result_schema_version,
                        result_bytes,
                        result_digest,
                        events,
                        event_set_digest,
                    )
        except (EventStoreConflict, InjectedEventStoreFailure):
            raise
        except sqlite3.IntegrityError as exc:
            raise EventStoreConflict("event command conflicted with concurrent authority") from exc
        except ContractError as exc:
            raise EventStoreConflict(str(exc)) from exc

    def verify(self) -> None:
        """Rebuild and cryptographically verify all authority from genesis."""

        try:
            with open_v3_connection(self._database_path) as connection:
                migrate_connection(connection)
                self._verify_connection(connection)
        except EventStoreIntegrityError:
            raise
        except Exception as exc:
            raise EventStoreIntegrityError("event-store verification failed") from exc

    def execute_critical_issue(
        self,
        request: CommandRequest,
        *,
        intent: CriticalIssueIntent,
        coordinator: CriticalIssueCoordinator,
        critical_fault_hook: FaultHook | None = None,
        event_fault_hook: FaultHook | None = None,
        projection_hook: ProjectionHook | None = None,
    ) -> StoredCommandResult:
        """Run the signed intent -> DB commit -> signed marker issue protocol.

        Blob/report construction remains outside this method.  A crash after SQLite commit
        but before the marker produces an ambiguous timeout; exact retry or journal
        reconciliation recovers the original stored result without issuing twice.
        """

        if request.command.kind not in {
            CommandKind.ACKNOWLEDGE_ISSUE,
            CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
        }:
            raise EventStoreError("critical issue protocol accepts only issue commands")
        if intent.command_id != str(request.command.command_id):
            raise EventStoreError("critical issue intent command differs from the event command")
        if not isinstance(coordinator, CriticalIssueCoordinator):
            raise EventStoreError("critical issue coordinator is required")
        command_digest = canonical_digest(request.command.to_dict())
        approval_snapshot_digest = _critical_approval_snapshot_digest(request)
        receipt_ids = _critical_receipt_ids(request.result)
        if intent.command_digest != command_digest:
            raise EventStoreError("critical issue intent command digest differs")
        if intent.expected_versions != request.command.expected_versions:
            raise EventStoreError("critical issue intent expected versions differ")
        if intent.approval_snapshot_digest != approval_snapshot_digest:
            raise EventStoreError("critical issue intent approval snapshot differs")
        if intent.receipt_ids != receipt_ids:
            raise EventStoreError("critical issue intent receipts differ from the stored result")
        committed: list[StoredCommandResult] = []

        def commit(intent_digest: str) -> CriticalDatabaseCommit:
            result = self.execute(
                request,
                fault_hook=event_fault_hook,
                projection_hook=projection_hook,
            )
            committed.append(result)
            return CriticalDatabaseCommit(
                result.last_global_sequence,
                result.result_digest,
                receipt_ids,
                intent_digest,
            )

        coordinator.execute(intent, database_commit=commit, fault_hook=critical_fault_hook)
        if len(committed) != 1:
            raise EventStoreIntegrityError(
                "critical issue protocol did not resolve one stored result"
            )
        return committed[0]

    def event_count(self) -> int:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM v3_events").fetchone()[0])

    def event_at(self, global_sequence: int) -> EventEnvelope:
        if (
            isinstance(global_sequence, bool)
            or not isinstance(global_sequence, int)
            or global_sequence <= 0
        ):
            raise EventStoreError("event sequence must be a positive integer")
        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify_connection(connection)
            row = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE global_sequence=?",
                (global_sequence,),
            ).fetchone()
            if row is None:
                raise KeyError(global_sequence)
            value = json.loads(str(row[0]))
            return EventEnvelope.from_dict(value)

    def lookup_exact_retry(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        command_kind: CommandKind,
        target_aggregate: str,
        payload_digest: str,
    ) -> StoredCommandResult | None:
        """Resolve a retry before callers read mutable aggregate state.

        Material changes under a claimed key conflict; expected versions are
        deliberately taken from the original stored command, not reconstructed
        from the now-current head.
        """

        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT principal_id, idempotency_key, command_digest, result_schema_version, "
                "result_json, result_digest, first_global_sequence, last_global_sequence, "
                "event_set_digest, created_at FROM v3_idempotency_records "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            if command_kind in {
                CommandKind.ACKNOWLEDGE_ISSUE,
                CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
            }:
                self._verify_connection(connection)
            if str(row[0]) != principal_id:
                raise EventStoreConflict("idempotency key was claimed by another principal")
            result = self._verified_stored_result(connection, row)
            envelope = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE global_sequence=?",
                (result.first_global_sequence,),
            ).fetchone()
            # _verified_stored_result just proved the complete contiguous event set,
            # including this first sequence, on the same read connection.
            event = EventEnvelope.from_dict(json.loads(str(envelope[0])))
            command = event.command
            if (
                command.kind is not command_kind
                or str(command.target_aggregate) != target_aggregate
                or command.payload_digest != payload_digest
            ):
                raise EventStoreConflict("idempotency key already binds different material input")
            return result

    def global_sequences(self) -> tuple[int, ...]:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            return tuple(
                int(row[0])
                for row in connection.execute(
                    "SELECT global_sequence FROM v3_events ORDER BY global_sequence"
                )
            )

    def events(self) -> tuple[EventEnvelope, ...]:
        """Return one fully verified authority snapshot without repeated whole-store replay."""

        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify_connection(connection)
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events ORDER BY global_sequence"
            ).fetchall()
            return tuple(EventEnvelope.from_dict(json.loads(str(row[0]))) for row in rows)

    def aggregate_head(self, aggregate_id: str) -> tuple[int, str] | None:
        with open_v3_connection(self._database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT aggregate_version, event_digest FROM v3_aggregate_heads "
                "WHERE aggregate_id=?",
                (aggregate_id,),
            ).fetchone()
            return None if row is None else (int(row[0]), str(row[1]))

    def current_anchor(self) -> AuthorityAnchor:
        """Return the verified current tip for independent checkpoint signing."""

        with open_v3_connection(self._database_path, read_only=True) as connection:
            self._verify_connection(connection)
            row = connection.execute(
                "SELECT global_sequence, event_digest FROM v3_events "
                "ORDER BY global_sequence DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return AuthorityAnchor(0, ZERO_DIGEST)
            return AuthorityAnchor(int(row[0]), str(row[1]))

    def _resolve_retry(
        self,
        connection: sqlite3.Connection,
        *,
        principal_id: str,
        idempotency_key: str,
        command_digest: str,
    ) -> StoredCommandResult | None:
        rows = connection.execute(
            "SELECT principal_id, idempotency_key, command_digest, result_schema_version, "
            "result_json, result_digest, first_global_sequence, last_global_sequence, "
            "event_set_digest, created_at FROM v3_idempotency_records "
            "WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or str(rows[0][0]) != principal_id:
            raise EventStoreConflict("idempotency key was claimed by another principal")
        row = rows[0]
        if str(row[2]) != command_digest:
            raise EventStoreConflict("idempotency key already binds a different command")
        return self._verified_stored_result(connection, row)

    def _validate_versions_and_transitions(
        self,
        connection: sqlite3.Connection,
        request: CommandRequest,
        intents: tuple[EventIntent, ...],
    ) -> None:
        count, maximum = connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(global_sequence), 0) FROM v3_events"
        ).fetchone()
        if int(count) != int(maximum):
            raise EventStoreIntegrityError("global sequence is not consecutive before append")
        expected = dict(request.command.expected_versions)
        for intent in intents:
            row = connection.execute(
                "SELECT aggregate_version, event_digest FROM v3_aggregate_heads "
                "WHERE aggregate_kind=? AND aggregate_id=?",
                (intent.aggregate_kind.value, str(intent.aggregate_id)),
            ).fetchone()
            actual_version = 0 if row is None else int(row[0])
            latest = connection.execute(
                "SELECT aggregate_version, event_digest FROM v3_events "
                "WHERE aggregate_kind=? AND aggregate_id=? ORDER BY aggregate_version DESC LIMIT 1",
                (intent.aggregate_kind.value, str(intent.aggregate_id)),
            ).fetchone()
            observed_tip = None if latest is None else (int(latest[0]), str(latest[1]))
            head_tip = None if row is None else (int(row[0]), str(row[1]))
            if head_tip != observed_tip:
                raise EventStoreIntegrityError(
                    "aggregate head does not match its stream before append"
                )
            if actual_version != expected[str(intent.aggregate_id)]:
                raise EventStoreConflict(
                    f"expected version {expected[str(intent.aggregate_id)]} for "
                    f"{intent.aggregate_id}, observed {actual_version}"
                )
            kinds = tuple(
                EventKind(str(item[0]))
                for item in connection.execute(
                    "SELECT event_kind FROM v3_events WHERE aggregate_kind=? AND aggregate_id=? "
                    "ORDER BY aggregate_version",
                    (intent.aggregate_kind.value, str(intent.aggregate_id)),
                )
            )
            try:
                current = replay(intent.aggregate_kind, kinds)
                transition(intent.aggregate_kind, current, intent.event_kind)
            except ContractError as exc:
                raise EventStoreConflict(str(exc)) from exc

    def _append_events(
        self,
        connection: sqlite3.Connection,
        request: CommandRequest,
        intents: tuple[EventIntent, ...],
        *,
        command_digest: str,
        fault_hook: FaultHook | None,
    ) -> tuple[EventEnvelope, ...]:
        global_row = connection.execute(
            "SELECT global_sequence, event_digest FROM v3_events "
            "ORDER BY global_sequence DESC LIMIT 1"
        ).fetchone()
        next_global = 1 if global_row is None else int(global_row[0]) + 1
        prior_global = ZERO_DIGEST if global_row is None else str(global_row[1])
        expected = dict(request.command.expected_versions)
        built: list[EventEnvelope] = []
        for index, intent in enumerate(intents):
            _fault(fault_hook, f"before_event:{index}")
            aggregate_version = expected[str(intent.aggregate_id)] + 1
            head = connection.execute(
                "SELECT event_digest FROM v3_aggregate_heads WHERE aggregate_kind=? "
                "AND aggregate_id=?",
                (intent.aggregate_kind.value, str(intent.aggregate_id)),
            ).fetchone()
            prior_aggregate = ZERO_DIGEST if head is None else str(head[0])
            event = EventEnvelope.create(
                event_id=deterministic_identifier(
                    "event",
                    {
                        "command_digest": command_digest,
                        "aggregate_id": str(intent.aggregate_id),
                        "aggregate_version": aggregate_version,
                        "event_kind": intent.event_kind.value,
                    },
                ),
                kind=intent.event_kind,
                aggregate_kind=intent.aggregate_kind,
                aggregate_id=intent.aggregate_id,
                aggregate_version=aggregate_version,
                global_sequence=next_global,
                prior_global_digest=prior_global,
                prior_aggregate_digest=prior_aggregate,
                occurred_at_utc=request.occurred_at_utc,
                monotonic_elapsed_ms=request.monotonic_elapsed_ms,
                command=request.command,
            )
            envelope_json = canonical_bytes(event.to_dict()).decode("utf-8")
            connection.execute(
                "INSERT INTO v3_events(global_sequence, event_id, aggregate_kind, aggregate_id, "
                "aggregate_version, event_kind, envelope_json, event_digest, "
                "prior_global_digest, prior_aggregate_digest, occurred_at_utc, command_id, "
                "source_import_id, training_eligible) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                (
                    event.global_sequence,
                    str(event.event_id),
                    event.aggregate_kind.value,
                    str(event.aggregate_id),
                    event.aggregate_version,
                    event.kind.value,
                    envelope_json,
                    event.event_digest,
                    event.prior_global_digest,
                    event.prior_aggregate_digest,
                    event.occurred_at_utc,
                    str(event.command.command_id),
                ),
            )
            _fault(fault_hook, f"after_event:{index}")
            write = connection.execute(
                "INSERT INTO v3_aggregate_heads(aggregate_kind, aggregate_id, aggregate_version, "
                "event_digest) VALUES (?, ?, ?, ?) ON CONFLICT(aggregate_kind, aggregate_id) "
                "DO UPDATE SET aggregate_version=excluded.aggregate_version, "
                "event_digest=excluded.event_digest WHERE "
                "v3_aggregate_heads.aggregate_version=excluded.aggregate_version-1 AND "
                "v3_aggregate_heads.event_digest=?",
                (
                    event.aggregate_kind.value,
                    str(event.aggregate_id),
                    event.aggregate_version,
                    event.event_digest,
                    event.prior_aggregate_digest,
                ),
            )
            _require_head_advance(write)
            _fault(fault_hook, f"after_head:{index}")
            built.append(event)
            next_global += 1
            prior_global = event.event_digest
        return tuple(built)

    def _verify_connection(self, connection: sqlite3.Connection) -> None:
        try:
            events, expected_heads = self._verify_events(connection)
            self._verify_heads(connection, expected_heads)
            self._verify_idempotency(connection, events)
            self._verify_historical_import_links(connection, events)
            self._verify_trusted_anchor(events)
        except EventStoreIntegrityError:
            raise
        except Exception as exc:
            raise EventStoreIntegrityError("authority replay failed closed") from exc

    def _verify_trusted_anchor(self, events: dict[int, EventEnvelope]) -> None:
        if self._trusted_anchor is None:
            return
        if self._trusted_anchor.global_sequence == 0:
            return
        anchored_event = events.get(self._trusted_anchor.global_sequence)
        if (
            anchored_event is None
            or anchored_event.event_digest != self._trusted_anchor.event_digest
        ):
            raise EventStoreIntegrityError(
                "authority chain does not contain the independently trusted checkpoint anchor"
            )

    def _verify_events(
        self, connection: sqlite3.Connection
    ) -> tuple[dict[int, EventEnvelope], dict[tuple[str, str], tuple[int, str]]]:
        rows = connection.execute(
            "SELECT global_sequence, event_id, aggregate_kind, aggregate_id, aggregate_version, "
            "event_kind, envelope_json, event_digest, prior_global_digest, "
            "prior_aggregate_digest, occurred_at_utc, command_id, source_import_id, "
            "training_eligible FROM v3_events ORDER BY global_sequence"
        ).fetchall()
        prior_global = ZERO_DIGEST
        streams: dict[tuple[str, str], tuple[int, str, list[EventKind]]] = {}
        decoded: dict[int, EventEnvelope] = {}
        for expected_sequence, row in enumerate(rows, start=1):
            if int(row[0]) != expected_sequence:
                raise EventStoreIntegrityError("global event sequence has a gap or reorder")
            try:
                raw = str(row[6])
                value = json.loads(raw)
                event = EventEnvelope.from_dict(value)
                if canonical_bytes(value).decode("utf-8") != raw:
                    raise ContractError("event envelope bytes are not canonical")
            except Exception as exc:
                raise EventStoreIntegrityError("event envelope is malformed or tampered") from exc
            if event.event_id != _expected_event_id(event):
                raise EventStoreIntegrityError(
                    "event identity is not the deterministic command identity"
                )
            persisted = (
                event.global_sequence,
                str(event.event_id),
                event.aggregate_kind.value,
                str(event.aggregate_id),
                event.aggregate_version,
                event.kind.value,
                canonical_bytes(event.to_dict()).decode("utf-8"),
                event.event_digest,
                event.prior_global_digest,
                event.prior_aggregate_digest,
                event.occurred_at_utc,
                str(event.command.command_id),
            )
            if tuple(row[:12]) != persisted:
                raise EventStoreIntegrityError("event columns do not match canonical envelope")
            if event.prior_global_digest != prior_global:
                raise EventStoreIntegrityError("global event digest chain is broken")
            key = (event.aggregate_kind.value, str(event.aggregate_id))
            version, prior_stream, kinds = streams.get(key, (0, ZERO_DIGEST, []))
            if (
                event.aggregate_version != version + 1
                or event.prior_aggregate_digest != prior_stream
            ):
                raise EventStoreIntegrityError("aggregate version or digest chain is broken")
            if event.aggregate_kind in {
                AggregateKind.TOURNAMENT_INGRESS,
                AggregateKind.ROUND_INGRESS,
                AggregateKind.FIELD_INGRESS,
                AggregateKind.RESULT,
                AggregateKind.SETTLEMENT,
                AggregateKind.EPOCH,
                AggregateKind.REACTION,
                AggregateKind.DERIVATION,
                AggregateKind.TOURNAMENT,
                AggregateKind.ROUND,
                AggregateKind.FIELD,
                AggregateKind.JOB,
                AggregateKind.BUNDLE,
                AggregateKind.ISSUE_BATCH,
                AggregateKind.COMPETITOR,
                AggregateKind.FORECAST,
                AggregateKind.SCORE,
                AggregateKind.WEIGHTS,
                AggregateKind.APPROVAL_DECISION,
                AggregateKind.AUDIT_GENERATION,
                AggregateKind.MONITORING,
            }:
                try:
                    transition(
                        event.aggregate_kind,
                        replay(event.aggregate_kind, tuple(kinds)),
                        event.kind,
                    )
                except ContractError as exc:
                    raise EventStoreIntegrityError(
                        "persisted lifecycle transition is illegal"
                    ) from exc
            elif event.kind is not EventKind.HISTORY_IMPORTED:
                raise EventStoreIntegrityError("unsupported non-lifecycle aggregate event")
            kinds.append(event.kind)
            streams[key] = (event.aggregate_version, event.event_digest, kinds)
            decoded[event.global_sequence] = event
            prior_global = event.event_digest
            if int(row[13]) not in (0, 1):
                raise EventStoreIntegrityError("training eligibility column is invalid")
            if event.kind is EventKind.HISTORY_IMPORTED:
                if row[12] is None or int(row[13]) != 0:
                    raise EventStoreIntegrityError("history import provenance is missing")
            elif row[12] is not None:
                raise EventStoreIntegrityError("ordinary event carries import provenance")
        heads = {key: (value[0], value[1]) for key, value in streams.items()}
        return decoded, heads

    def _verify_heads(
        self,
        connection: sqlite3.Connection,
        expected: dict[tuple[str, str], tuple[int, str]],
    ) -> None:
        observed = {
            (str(row[0]), str(row[1])): (int(row[2]), str(row[3]))
            for row in connection.execute(
                "SELECT aggregate_kind, aggregate_id, aggregate_version, event_digest "
                "FROM v3_aggregate_heads"
            )
        }
        if observed != expected:
            raise EventStoreIntegrityError("aggregate heads are missing, stale, or incorrect")

    def _verify_idempotency(
        self, connection: sqlite3.Connection, events: dict[int, EventEnvelope]
    ) -> None:
        rows = connection.execute(
            "SELECT principal_id, idempotency_key, command_digest, result_schema_version, "
            "result_json, result_digest, first_global_sequence, last_global_sequence, "
            "event_set_digest, created_at FROM v3_idempotency_records "
            "ORDER BY first_global_sequence"
        ).fetchall()
        if len({str(row[1]) for row in rows}) != len(rows):
            raise EventStoreIntegrityError("one idempotency key is claimed by multiple principals")
        covered: set[int] = set()
        for row in rows:
            result = self._verified_stored_result(connection, row)
            sequence_set = set(range(result.first_global_sequence, result.last_global_sequence + 1))
            covered.update(sequence_set)
            group = tuple(events[sequence] for sequence in sorted(sequence_set))
            command = group[0].command
            if command.kind is not CommandKind.IMPORT_HISTORY:
                intents = tuple(
                    EventIntent(event.aggregate_kind, event.aggregate_id, event.kind)
                    for event in group
                )
                try:
                    validate_command_event_intents(command, intents)
                except ContractError as exc:
                    raise EventStoreIntegrityError("persisted command/event kind mismatch") from exc
        if covered != set(events):
            raise EventStoreIntegrityError("event authority has missing idempotency linkage")

    def _verified_stored_result(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> StoredCommandResult:
        try:
            principal = str(row[0])
            key = str(row[1])
            command_digest = str(row[2])
            schema = str(row[3])
            raw = str(row[4])
            value = json.loads(raw)
            if not isinstance(value, dict) or canonical_bytes(value).decode("utf-8") != raw:
                raise ValueError("noncanonical result")
            if canonical_digest(value) != str(row[5]) or not schema:
                raise ValueError("result digest or schema mismatch")
            first, last = int(row[6]), int(row[7])
            group_rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE global_sequence BETWEEN ? AND ? "
                "ORDER BY global_sequence",
                (first, last),
            ).fetchall()
            group = tuple(EventEnvelope.from_dict(json.loads(str(item[0]))) for item in group_rows)
            if not group or len(group) != last - first + 1:
                raise ValueError("missing event set")
            command = group[0].command
            if (
                any(item.command != command for item in group)
                or str(command.command_id) != key
                or str(command.actor_id) != principal
                or canonical_digest(command.to_dict()) != command_digest
                or _event_set_digest(group) != str(row[8])
            ):
                raise ValueError("command or event-set linkage mismatch")
            require_utc_milliseconds(str(row[9]))
            return StoredCommandResult(
                schema,
                raw.encode("utf-8"),
                str(row[5]),
                first,
                last,
                str(row[8]),
                tuple(str(item.event_id) for item in group),
            )
        except Exception as exc:
            raise EventStoreIntegrityError(
                "idempotency result or event-set linkage is corrupt"
            ) from exc

    def _verify_historical_import_links(
        self, connection: sqlite3.Connection, events: dict[int, EventEnvelope]
    ) -> None:
        event_imports = {
            str(row[0])
            for row in connection.execute(
                "SELECT source_import_id FROM v3_events WHERE source_import_id IS NOT NULL"
            )
        }
        recorded_imports = {
            str(row[0]) for row in connection.execute("SELECT import_id FROM v3_historical_imports")
        }
        if event_imports != recorded_imports:
            raise EventStoreIntegrityError("history-import events and records do not reconcile")
        for event in events.values():
            if (
                event.kind is EventKind.HISTORY_IMPORTED
                and event.command.kind is not CommandKind.IMPORT_HISTORY
            ):
                raise EventStoreIntegrityError(
                    "history-import event has the wrong command identity"
                )


def _event_set_digest(events: Iterable[EventEnvelope]) -> str:
    return canonical_digest(
        {
            "schema_version": EVENT_SET_SCHEMA_VERSION,
            "events": [
                {
                    "global_sequence": event.global_sequence,
                    "event_id": str(event.event_id),
                    "event_digest": event.event_digest,
                }
                for event in events
            ],
        }
    )


def _critical_approval_snapshot_digest(request: CommandRequest) -> str:
    payload = request.command.payload
    if not isinstance(payload, InlinePayload):
        raise EventStoreError("critical issue command must inline its approval snapshot binding")
    value = payload.to_value()
    digest = value.get("approval_snapshot_digest")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise EventStoreError("critical issue command has no canonical approval snapshot digest")
    return digest


def _critical_receipt_ids(value: Any) -> tuple[str, ...]:
    found: set[str] = set()

    def collect(candidate: Any) -> None:
        if isinstance(candidate, Mapping):
            receipt = candidate.get("receipt_id")
            if isinstance(receipt, str) and receipt.startswith("receipt:"):
                found.add(receipt)
            receipts = candidate.get("receipt_ids")
            if isinstance(receipts, (list, tuple)):
                found.update(
                    item
                    for item in receipts
                    if isinstance(item, str) and item.startswith("receipt:")
                )
            for child in candidate.values():
                collect(child)
        elif isinstance(candidate, list):
            for child in candidate:
                collect(child)

    collect(value)
    if not found:
        raise EventStoreError("critical issue result contains no receipt identities")
    return tuple(sorted(found))


def _expected_event_id(event: EventEnvelope):
    if event.kind is EventKind.HISTORY_IMPORTED:
        payload = event.command.payload
        if not isinstance(payload, InlinePayload):
            raise EventStoreIntegrityError("history import event must use an inline manifest")
        tip = payload.to_value().get("source_tip_digest")
        if (
            not isinstance(tip, str)
            or len(tip) != 64
            or any(character not in "0123456789abcdef" for character in tip)
        ):
            raise EventStoreIntegrityError("history import manifest has no valid source tip")
        return deterministic_identifier(
            "event", {"kind": EventKind.HISTORY_IMPORTED.value, "tip": tip}
        )
    return deterministic_identifier(
        "event",
        {
            "command_digest": canonical_digest(event.command.to_dict()),
            "aggregate_id": str(event.aggregate_id),
            "aggregate_version": event.aggregate_version,
            "event_kind": event.kind.value,
        },
    )


def _stored_result(
    schema: str,
    result_bytes: bytes,
    result_digest: str,
    events: tuple[EventEnvelope, ...],
    event_set_digest: str,
) -> StoredCommandResult:
    return StoredCommandResult(
        schema,
        result_bytes,
        result_digest,
        events[0].global_sequence,
        events[-1].global_sequence,
        event_set_digest,
        tuple(str(event.event_id) for event in events),
    )


def _fault(hook: FaultHook | None, point: str) -> None:
    if hook is not None:
        hook(point)


def _require_head_advance(cursor: sqlite3.Cursor) -> None:
    if cursor.rowcount != 1:
        raise EventStoreConflict("aggregate head changed during atomic command")


def verify_read_only_authority(
    database_path: Path | str,
    *,
    trusted_anchor: AuthorityAnchor,
) -> AuthorityAnchor:
    """Verify a signed backup without migrating or opening a writer connection."""

    if not isinstance(trusted_anchor, AuthorityAnchor):
        raise EventStoreError("read-only verification requires an AuthorityAnchor")
    path = Path(database_path).expanduser().resolve(strict=False)
    try:
        with open_v3_connection(path, read_only=True) as connection:
            if canonical_schema_digest(connection) != EXPECTED_SCHEMA_DIGEST:
                raise EventStoreIntegrityError("read-only authority schema is stale or different")
            verifier = object.__new__(SQLiteEventStore)
            verifier._database_path = path
            verifier._trusted_anchor = trusted_anchor
            verifier._verify_connection(connection)
            row = connection.execute(
                "SELECT global_sequence, event_digest FROM v3_events "
                "ORDER BY global_sequence DESC LIMIT 1"
            ).fetchone()
            return (
                AuthorityAnchor(0, ZERO_DIGEST)
                if row is None
                else AuthorityAnchor(int(row[0]), str(row[1]))
            )
    except EventStoreIntegrityError:
        raise
    except Exception as exc:
        raise EventStoreIntegrityError("read-only authority verification failed") from exc


__all__ = [
    "AuthorityAnchor",
    "EventStoreConflict",
    "EventStoreError",
    "EventStoreIntegrityError",
    "InjectedEventStoreFailure",
    "SQLiteEventStore",
    "StoredCommandResult",
    "verify_read_only_authority",
]
