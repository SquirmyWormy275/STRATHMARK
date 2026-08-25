"""Recovery-first atomic issue authority for approved V3 field receipts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from strathmark.v3.application.approval import ApprovalRow, DecisionState
from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.infrastructure.integrity import CriticalIssueCoordinator, CriticalIssueIntent
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import StoredCommandResult
from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore

_TOKEN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_CALLER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MAX_FIELDS = 48
_MAX_ACTOR_METADATA_BYTES = 4096


class IssueError(ValueError):
    """Raised when issue authority is incomplete, ambiguous, or noncanonical."""


@dataclass(frozen=True, slots=True)
class IssueFieldSelection:
    field_id: str
    receipt_id: str
    receipt_revision: int
    upstream_field_revision: int
    expected_field_version: int
    approval_row_digest: str
    call_order: int
    round_id: str
    epoch_id: str
    competitor_ids: tuple[str, ...]
    issued_marks: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        try:
            field_id = str(require_identifier(self.field_id, expected_namespace="field"))
            receipt_id = str(require_identifier(self.receipt_id, expected_namespace="receipt"))
            round_id = str(require_identifier(self.round_id, expected_namespace="round"))
            epoch_id = str(require_identifier(self.epoch_id, expected_namespace="epoch"))
        except Exception as exc:
            raise IssueError("issue selection identifiers are invalid") from exc
        if (
            field_id != self.field_id
            or receipt_id != self.receipt_id
            or round_id != self.round_id
            or epoch_id != self.epoch_id
        ):
            raise IssueError("issue selection identifiers are not canonical")
        for value in (self.receipt_revision, self.upstream_field_revision):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise IssueError("issue selection revisions must be positive integers")
        if (
            isinstance(self.expected_field_version, bool)
            or not isinstance(self.expected_field_version, int)
            or self.expected_field_version < 0
        ):
            raise IssueError("expected field version must be a nonnegative integer")
        if (
            not isinstance(self.approval_row_digest, str)
            or len(self.approval_row_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.approval_row_digest)
        ):
            raise IssueError("approval row digest must be a lower-case SHA-256 digest")
        if (
            isinstance(self.call_order, bool)
            or not isinstance(self.call_order, int)
            or self.call_order < 0
        ):
            raise IssueError("issue selection call order must be nonnegative")
        if not isinstance(self.competitor_ids, tuple) or not self.competitor_ids:
            raise IssueError("issue selection roster must be a nonempty tuple")
        try:
            roster = tuple(
                str(require_identifier(item, expected_namespace="competitor"))
                for item in self.competitor_ids
            )
        except Exception as exc:
            raise IssueError("issue selection roster is invalid") from exc
        if roster != self.competitor_ids or len(set(roster)) != len(roster):
            raise IssueError("issue selection roster must be canonical and unique")
        if (
            not isinstance(self.issued_marks, tuple)
            or tuple(item[0] for item in self.issued_marks) != roster
        ):
            raise IssueError("issued marks must match the exact roster order")
        for competitor_id, mark in self.issued_marks:
            if (
                competitor_id not in roster
                or isinstance(mark, bool)
                or not isinstance(mark, int)
                or not 3 <= mark <= 183
            ):
                raise IssueError("issued mark must be an integer from 3 through 183")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "receipt_id": self.receipt_id,
            "receipt_revision": self.receipt_revision,
            "upstream_field_revision": self.upstream_field_revision,
            "expected_field_version": self.expected_field_version,
            "approval_row_digest": self.approval_row_digest,
            "call_order": self.call_order,
            "round_id": self.round_id,
            "epoch_id": self.epoch_id,
            "competitor_ids": list(self.competitor_ids),
            "issued_marks": [list(item) for item in self.issued_marks],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> IssueFieldSelection:
        expected = {
            "field_id",
            "receipt_id",
            "receipt_revision",
            "upstream_field_revision",
            "expected_field_version",
            "approval_row_digest",
            "call_order",
            "round_id",
            "epoch_id",
            "competitor_ids",
            "issued_marks",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise IssueError("issue selection fields differ")
        return cls(
            value["field_id"],
            value["receipt_id"],
            value["receipt_revision"],
            value["upstream_field_revision"],
            value["expected_field_version"],
            value["approval_row_digest"],
            value["call_order"],
            value["round_id"],
            value["epoch_id"],
            tuple(value["competitor_ids"]),
            tuple((item[0], item[1]) for item in value["issued_marks"]),
        )


@dataclass(frozen=True, slots=True)
class IssueBatchCommand:
    caller_namespace: str
    request_identity: str
    tournament_id: str
    approval_snapshot_id: str
    selections: tuple[IssueFieldSelection, ...]
    actor_id: str
    actor_metadata_json: str
    actor_metadata_digest: str
    reason_code: str
    submitted_at: str
    monotonic_elapsed_ms: int
    issue_batch_id: str
    command_digest: str
    schema_version: str = "strathmark-v3-issue-batch-command-v1"

    def __post_init__(self) -> None:
        if self.schema_version != "strathmark-v3-issue-batch-command-v1":
            raise IssueError("issue command schema differs")
        if not isinstance(self.caller_namespace, str) or not _CALLER.fullmatch(
            self.caller_namespace
        ):
            raise IssueError("issue caller namespace is invalid")
        try:
            request_identity = str(IdempotencyKey(self.request_identity))
            tournament_id = str(
                require_identifier(self.tournament_id, expected_namespace="tournament")
            )
            snapshot_id = str(
                require_identifier(
                    self.approval_snapshot_id, expected_namespace="approval_snapshot"
                )
            )
            actor_id = str(require_identifier(self.actor_id, expected_namespace="actor"))
            issue_batch_id = str(
                require_identifier(self.issue_batch_id, expected_namespace="issue_batch")
            )
        except Exception as exc:
            raise IssueError("issue command identifiers are invalid") from exc
        if (
            request_identity != self.request_identity
            or tournament_id != self.tournament_id
            or snapshot_id != self.approval_snapshot_id
            or actor_id != self.actor_id
            or issue_batch_id != self.issue_batch_id
        ):
            raise IssueError("issue command identifiers are not canonical")
        if not isinstance(self.selections, tuple) or not 1 <= len(self.selections) <= _MAX_FIELDS:
            raise IssueError("issue command requires 1..48 immutable selections")
        if not all(isinstance(item, IssueFieldSelection) for item in self.selections):
            raise IssueError("issue command selections must be typed")
        if self.selections != tuple(sorted(self.selections, key=lambda item: item.field_id)):
            raise IssueError("issue selections must be canonically sorted by field")
        field_ids = tuple(item.field_id for item in self.selections)
        receipt_ids = tuple(item.receipt_id for item in self.selections)
        if len(set(field_ids)) != len(field_ids):
            raise IssueError("issue selections contain a duplicate field")
        if len(set(receipt_ids)) != len(receipt_ids):
            raise IssueError("issue selections contain a duplicate receipt")
        try:
            metadata = json.loads(self.actor_metadata_json)
            encoded = canonical_bytes(metadata, max_bytes=_MAX_ACTOR_METADATA_BYTES)
        except Exception as exc:
            raise IssueError("issue actor metadata is invalid or oversized") from exc
        if (
            not isinstance(metadata, dict)
            or encoded.decode("utf-8") != self.actor_metadata_json
            or canonical_digest(metadata) != self.actor_metadata_digest
        ):
            raise IssueError("issue actor metadata authority differs")
        if not isinstance(self.reason_code, str) or not _TOKEN.fullmatch(self.reason_code):
            raise IssueError("issue reason code is invalid")
        try:
            require_utc_milliseconds(self.submitted_at)
        except Exception as exc:
            raise IssueError("issue submitted time is invalid") from exc
        if (
            isinstance(self.monotonic_elapsed_ms, bool)
            or not isinstance(self.monotonic_elapsed_ms, int)
            or self.monotonic_elapsed_ms < 0
        ):
            raise IssueError("issue monotonic elapsed time must be nonnegative")
        identity_value = _identity_value(self)
        expected_batch_id = str(deterministic_identifier("issue_batch", identity_value))
        if self.issue_batch_id != expected_batch_id:
            raise IssueError("issue batch identity differs from its bound selections")
        if self.command_digest != canonical_digest(_content_value(self)):
            raise IssueError("issue command digest differs")

    @classmethod
    def create(
        cls,
        *,
        caller_namespace: str,
        request_identity: str,
        tournament_id: str,
        approval_snapshot_id: str,
        selections: tuple[IssueFieldSelection, ...],
        actor_id: str,
        actor_metadata: Mapping[str, Any],
        reason_code: str,
        submitted_at: str,
        monotonic_elapsed_ms: int,
    ) -> IssueBatchCommand:
        if not isinstance(actor_metadata, Mapping):
            raise IssueError("issue actor metadata must be an object")
        metadata_json = canonical_bytes(
            dict(actor_metadata), max_bytes=_MAX_ACTOR_METADATA_BYTES
        ).decode("utf-8")
        identity_value = {
            "schema_version": "strathmark-v3-issue-batch-identity-v1",
            "tournament_id": tournament_id,
            "approval_snapshot_id": approval_snapshot_id,
            "selections": [item.to_dict() for item in selections],
        }
        issue_batch_id = str(deterministic_identifier("issue_batch", identity_value))
        values = {
            "schema_version": "strathmark-v3-issue-batch-command-v1",
            "caller_namespace": caller_namespace,
            "request_identity": request_identity,
            "tournament_id": tournament_id,
            "approval_snapshot_id": approval_snapshot_id,
            "selections": [item.to_dict() for item in selections],
            "actor_id": actor_id,
            "actor_metadata_json": metadata_json,
            "actor_metadata_digest": canonical_digest(dict(actor_metadata)),
            "reason_code": reason_code,
            "submitted_at": submitted_at,
            "monotonic_elapsed_ms": monotonic_elapsed_ms,
            "issue_batch_id": issue_batch_id,
        }
        return cls(
            caller_namespace,
            request_identity,
            tournament_id,
            approval_snapshot_id,
            selections,
            actor_id,
            metadata_json,
            values["actor_metadata_digest"],
            reason_code,
            submitted_at,
            monotonic_elapsed_ms,
            issue_batch_id,
            canonical_digest(values),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**_content_value(self), "command_digest": self.command_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> IssueBatchCommand:
        expected = {
            "schema_version",
            "caller_namespace",
            "request_identity",
            "tournament_id",
            "approval_snapshot_id",
            "selections",
            "actor_id",
            "actor_metadata_json",
            "actor_metadata_digest",
            "reason_code",
            "submitted_at",
            "monotonic_elapsed_ms",
            "issue_batch_id",
            "command_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise IssueError("issue command fields differ")
        selections = value["selections"]
        if not isinstance(selections, list):
            raise IssueError("issue command selections are invalid")
        return cls(
            value["caller_namespace"],
            value["request_identity"],
            value["tournament_id"],
            value["approval_snapshot_id"],
            tuple(IssueFieldSelection.from_dict(item) for item in selections),
            value["actor_id"],
            value["actor_metadata_json"],
            value["actor_metadata_digest"],
            value["reason_code"],
            value["submitted_at"],
            value["monotonic_elapsed_ms"],
            value["issue_batch_id"],
            value["command_digest"],
            value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class IssueAcknowledgment:
    issue_batch_id: str
    receipt_ids: tuple[str, ...]
    first_global_sequence: int
    last_global_sequence: int
    result_digest: str


class IssuanceService:
    """Validate one approved snapshot and atomically acknowledge its exact fields."""

    def __init__(self, store: Any, *, coordinator: CriticalIssueCoordinator) -> None:
        if not isinstance(coordinator, CriticalIssueCoordinator):
            raise IssueError("issuance requires a critical issue coordinator")
        if not callable(getattr(store, "approval_page", None)) or not hasattr(
            store, "database_path"
        ):
            raise IssueError("issuance requires a verified field projection store")
        self._store = store
        self._coordinator = coordinator
        self._lifecycle_projection = SQLiteProjectionStore(store.database_path)

    def acknowledge(
        self,
        command: IssueBatchCommand,
        *,
        critical_fault_hook: Any | None = None,
        event_fault_hook: Any | None = None,
    ) -> IssueAcknowledgment:
        if not isinstance(command, IssueBatchCommand):
            raise IssueError("issuance requires a typed batch command")
        payload_value = _issue_payload(command)
        payload = InlinePayload.from_value(payload_value)
        expected_versions = tuple(
            sorted(
                (
                    *tuple(
                        (item.field_id, item.expected_field_version) for item in command.selections
                    ),
                    (command.issue_batch_id, 0),
                )
            )
        )
        envelope = CommandEnvelope(
            kind=CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
            command_id=IdempotencyKey(command.request_identity),
            target_aggregate=require_identifier(
                command.issue_batch_id, expected_namespace="issue_batch"
            ),
            expected_versions=expected_versions,
            actor_id=require_identifier(command.actor_id, expected_namespace="actor"),
            payload=payload,
        )
        result_value = _acknowledgment_value(command)
        request = CommandRequest(
            principal_id=require_identifier(command.actor_id, expected_namespace="actor"),
            command=envelope,
            events=(
                *tuple(
                    EventIntent(
                        AggregateKind.FIELD,
                        require_identifier(item.field_id, expected_namespace="field"),
                        EventKind.FIELD_ISSUED,
                    )
                    for item in command.selections
                ),
                EventIntent(
                    AggregateKind.ISSUE_BATCH,
                    require_identifier(command.issue_batch_id, expected_namespace="issue_batch"),
                    EventKind.ISSUE_BATCH_ISSUED,
                ),
            ),
            result_schema_version="strathmark-v3-issue-acknowledgment-v1",
            result=result_value,
            occurred_at_utc=command.submitted_at,
            monotonic_elapsed_ms=command.monotonic_elapsed_ms,
        )
        existing = self._store._events.lookup_exact_retry(
            principal_id=command.actor_id,
            idempotency_key=command.request_identity,
            command_kind=CommandKind.ACKNOWLEDGE_BATCH_ISSUE,
            target_aggregate=command.issue_batch_id,
            payload_digest=payload.digest,
        )
        if existing is None:
            self._verify_current(command)
        intent = CriticalIssueIntent(
            command.request_identity,
            canonical_digest(envelope.to_dict()),
            command.approval_snapshot_id.split(":", 1)[1],
            expected_versions,
            tuple(sorted(item.receipt_id for item in command.selections)),
            command.submitted_at,
        )

        def projection_hook(connection: Any, events: tuple[Any, ...]) -> None:
            self._verify_current_connection(connection, command)
            self._lifecycle_projection.apply_events(connection, events)

        stored = self._store._events.execute_critical_issue(
            request,
            intent=intent,
            coordinator=self._coordinator,
            critical_fault_hook=critical_fault_hook,
            event_fault_hook=event_fault_hook,
            projection_hook=projection_hook,
        )
        return _acknowledgment(stored, command)

    def _verify_current(self, command: IssueBatchCommand) -> None:
        with open_v3_connection(self._store.database_path, read_only=True) as connection:
            self._verify_current_connection(connection, command)

    @staticmethod
    def _verify_current_connection(connection: Any, command: IssueBatchCommand) -> None:
        from strathmark.v3.application.approval import _VERIFIED_RECEIPT_AUTHORITY

        meta = connection.execute(
            "SELECT snapshot_id FROM v3_approval_projection_meta WHERE tournament_id=?",
            (command.tournament_id,),
        ).fetchone()
        if meta is None or str(meta[0]) != command.approval_snapshot_id:
            raise IssueError("issue approval snapshot is not current")
        for selection in command.selections:
            row = connection.execute(
                "SELECT row_json,row_digest FROM v3_approval_queue_rows "
                "WHERE tournament_id=? AND field_id=?",
                (command.tournament_id, selection.field_id),
            ).fetchone()
            if row is None:
                raise IssueError("issue field is absent from the approved snapshot")
            try:
                approval = ApprovalRow.from_dict(
                    json.loads(str(row[0])), _authority=_VERIFIED_RECEIPT_AUTHORITY
                )
            except Exception as exc:
                raise IssueError("issue approval row is corrupt") from exc
            if (
                str(row[1]) != approval.row_digest
                or approval.row_digest != selection.approval_row_digest
                or approval.receipt_id != selection.receipt_id
                or approval.receipt_revision != selection.receipt_revision
                or approval.upstream_field_revision != selection.upstream_field_revision
                or approval.call_order != selection.call_order
                or approval.decision_state
                not in {DecisionState.ACCEPTED, DecisionState.OVERRIDE_SUBMITTED}
                or approval.proposed_marks != selection.issued_marks
            ):
                raise IssueError("issue selection differs from its approved current row")
            receipt = connection.execute(
                "SELECT receipt_id,upstream_field_revision,superseded_by_sequence "
                "FROM v3_field_receipts WHERE field_id=? AND receipt_id=?",
                (selection.field_id, selection.receipt_id),
            ).fetchone()
            if receipt is None or tuple(receipt) != (
                selection.receipt_id,
                selection.upstream_field_revision,
                None,
            ):
                raise IssueError("issue receipt is not the current sealed receipt")


def _acknowledgment(stored: StoredCommandResult, command: IssueBatchCommand) -> IssueAcknowledgment:
    if stored.value() != _acknowledgment_value(command):
        raise IssueError("stored issue acknowledgment differs from the command")
    return IssueAcknowledgment(
        command.issue_batch_id,
        tuple(item.receipt_id for item in command.selections),
        stored.first_global_sequence,
        stored.last_global_sequence,
        stored.result_digest,
    )


def _acknowledgment_value(command: IssueBatchCommand) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-issue-acknowledgment-v1",
        "issue_batch_id": command.issue_batch_id,
        "receipt_ids": [item.receipt_id for item in command.selections],
        "field_ids": [item.field_id for item in command.selections],
        "approval_snapshot_id": command.approval_snapshot_id,
        "approval_snapshot_digest": command.approval_snapshot_id.split(":", 1)[1],
    }


def _identity_value(command: IssueBatchCommand) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-issue-batch-identity-v1",
        "tournament_id": command.tournament_id,
        "approval_snapshot_id": command.approval_snapshot_id,
        "selections": [item.to_dict() for item in command.selections],
    }


def _content_value(command: IssueBatchCommand) -> dict[str, Any]:
    return {
        "schema_version": command.schema_version,
        "caller_namespace": command.caller_namespace,
        "request_identity": command.request_identity,
        "tournament_id": command.tournament_id,
        "approval_snapshot_id": command.approval_snapshot_id,
        "selections": [item.to_dict() for item in command.selections],
        "actor_id": command.actor_id,
        "actor_metadata_json": command.actor_metadata_json,
        "actor_metadata_digest": command.actor_metadata_digest,
        "reason_code": command.reason_code,
        "submitted_at": command.submitted_at,
        "monotonic_elapsed_ms": command.monotonic_elapsed_ms,
        "issue_batch_id": command.issue_batch_id,
    }


def _issue_payload(command: IssueBatchCommand) -> dict[str, Any]:
    return {
        "schema_version": "strathmark-v3-batch-issue-authority-v1",
        "issue_command": command.to_dict(),
        "approval_snapshot_id": command.approval_snapshot_id,
        "approval_snapshot_digest": command.approval_snapshot_id.split(":", 1)[1],
        "fields": [
            {
                "field_id": item.field_id,
                "round_id": item.round_id,
                "epoch_id": item.epoch_id,
                "field_revision": item.upstream_field_revision,
                "receipt_id": item.receipt_id,
                "competitor_ids": list(item.competitor_ids),
                "issued_marks": dict(item.issued_marks),
            }
            for item in command.selections
        ],
    }


__all__ = [
    "IssueAcknowledgment",
    "IssueBatchCommand",
    "IssueError",
    "IssueFieldSelection",
    "IssuanceService",
]
