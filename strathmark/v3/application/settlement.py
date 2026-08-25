"""Issued-race-bound live settlement application authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from strathmark.v3.application.lifecycle import LifecycleService
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier, require_identifier
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection


class SettlementError(ValueError):
    """Raised when settlement does not bind one exact issued race."""


@dataclass(frozen=True, slots=True)
class SettlementCommand:
    field_id: str
    field_revision: int
    receipt_id: str
    command_id: str
    actor_id: str
    occurred_at: str
    monotonic_elapsed_ms: int

    def __post_init__(self) -> None:
        try:
            require_identifier(self.field_id, expected_namespace="field")
            require_identifier(self.receipt_id, expected_namespace="receipt")
            IdempotencyKey(self.command_id)
            require_identifier(self.actor_id, expected_namespace="actor")
            require_utc_milliseconds(self.occurred_at)
        except Exception as exc:
            raise SettlementError("settlement command identifiers or time are invalid") from exc
        if (
            isinstance(self.field_revision, bool)
            or not isinstance(self.field_revision, int)
            or self.field_revision <= 0
        ):
            raise SettlementError("settlement field revision must be positive")
        if (
            isinstance(self.monotonic_elapsed_ms, bool)
            or not isinstance(self.monotonic_elapsed_ms, int)
            or self.monotonic_elapsed_ms < 0
        ):
            raise SettlementError("settlement monotonic elapsed time must be nonnegative")

    @classmethod
    def create(cls, **values: Any) -> SettlementCommand:
        return cls(**values)


@dataclass(frozen=True, slots=True)
class SettlementAcknowledgment:
    field_id: str
    field_revision: int
    receipt_id: str
    result_revisions: tuple[tuple[str, int, str], ...]
    first_global_sequence: int
    last_global_sequence: int
    result_digest: str


class SettlementService:
    def __init__(self, lifecycle: LifecycleService) -> None:
        if not isinstance(lifecycle, LifecycleService):
            raise SettlementError("settlement requires lifecycle authority")
        self._lifecycle = lifecycle

    def settle(self, command: SettlementCommand) -> SettlementAcknowledgment:
        if not isinstance(command, SettlementCommand):
            raise SettlementError("settlement requires a typed command")
        stored = self._lifecycle.settle_live_race(
            StableIdentifier(command.field_id),
            field_revision=command.field_revision,
            claimed_receipt_id=StableIdentifier(command.receipt_id),
            command_id=IdempotencyKey(command.command_id),
            actor_id=StableIdentifier(command.actor_id),
            occurred_at_utc=command.occurred_at,
            monotonic_elapsed_ms=command.monotonic_elapsed_ms,
        )
        with open_v3_connection(
            self._lifecycle.projections.database_path, read_only=True
        ) as connection:
            rows = connection.execute(
                "SELECT result_key,revision,competitor_id FROM v3_result_revisions "
                "WHERE field_id=? AND field_revision=? "
                "AND settled_global_sequence BETWEEN ? AND ? "
                "ORDER BY competitor_id",
                (
                    command.field_id,
                    command.field_revision,
                    stored.first_global_sequence,
                    stored.last_global_sequence,
                ),
            ).fetchall()
        if not rows:
            raise SettlementError("settlement acknowledgment has no bound result revisions")
        return SettlementAcknowledgment(
            command.field_id,
            command.field_revision,
            command.receipt_id,
            tuple((str(row[0]), int(row[1]), str(row[2])) for row in rows),
            stored.first_global_sequence,
            stored.last_global_sequence,
            stored.result_digest,
        )


__all__ = [
    "SettlementAcknowledgment",
    "SettlementCommand",
    "SettlementError",
    "SettlementService",
]
