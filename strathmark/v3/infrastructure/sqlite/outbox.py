"""Finite durable delivery lifecycle for optional V3 consumers.

Local authority commits do not call this adapter synchronously.  Delivery work reads exact
canonical bytes, performs network I/O outside SQLite, and then records one bounded outcome.
No state transition deletes an undelivered authoritative payload.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.infrastructure.sqlite.connection import (
    immediate_transaction,
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.migrations import migrate_connection

_TOKEN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_REASON = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
MAX_OUTBOX_PAYLOAD_BYTES = 1_048_576


class OutboxError(RuntimeError):
    """Base outbox repository error."""


class OutboxConflict(OutboxError):
    """A caller attempted a stale or materially different transition."""


class OutboxState(str, Enum):
    PENDING = "pending"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    QUARANTINED = "quarantined"
    REPAIRED = "repaired"
    ACKNOWLEDGED = "acknowledged"


class DeliveryOutcome(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    ACKNOWLEDGED = "acknowledged"


@dataclass(frozen=True, slots=True)
class OutboxItem:
    outbox_id: str
    destination: str
    source_global_sequence: int | None
    payload_json: str
    payload_digest: str
    state: OutboxState
    revision: int
    attempt_count: int
    next_attempt_at: str | None
    terminal_reason: str | None
    created_at: str
    updated_at: str

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise OutboxError("persisted outbox payload is not an object")
        return value


@dataclass(frozen=True, slots=True)
class OutboxTransition:
    transition_sequence: int
    operation_id: str
    outbox_id: str
    expected_revision: int
    operation_kind: str
    material_digest: str
    from_state: OutboxState
    result_state: OutboxState
    result_revision: int
    result_attempt_count: int
    result_next_attempt_at: str | None
    result_terminal_reason: str | None
    reason: str | None
    observed_at: str
    prior_transition_digest: str
    transition_digest: str
    signed_transition_json: str


FaultHook = Callable[[str], None]


class OutboxRepository:
    """One-operation-per-connection outbox with deterministic exact replay."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        signer: Any,
        trust_store: Any,
        active_key_id: str,
        base_backoff_ms: int = 1_000,
        maximum_backoff_ms: int = 60_000,
    ) -> None:
        if isinstance(database_path, bool) or not isinstance(database_path, (Path, str)):
            raise OutboxError("database path must be a filesystem path")
        if (
            isinstance(base_backoff_ms, bool)
            or not isinstance(base_backoff_ms, int)
            or base_backoff_ms <= 0
        ):
            raise OutboxError("base backoff must be a positive integer")
        if (
            isinstance(maximum_backoff_ms, bool)
            or not isinstance(maximum_backoff_ms, int)
            or maximum_backoff_ms < base_backoff_ms
        ):
            raise OutboxError("maximum backoff must be an integer at least the base")
        self.database_path = Path(database_path).expanduser().resolve(strict=False)
        self.base_backoff_ms = base_backoff_ms
        self.maximum_backoff_ms = maximum_backoff_ms
        from strathmark.v3.infrastructure.integrity import (
            IntegrityKeyIdentity,
            IntegrityTrustStore,
        )

        if (
            not isinstance(trust_store, IntegrityTrustStore)
            or not isinstance(getattr(signer, "identity", None), IntegrityKeyIdentity)
            or not callable(getattr(signer, "sign", None))
        ):
            raise OutboxError("outbox requires an external signer and typed trust store")
        _require_token(active_key_id, "active outbox signing key id")
        try:
            trusted_identity = trust_store.identity(active_key_id)
        except Exception as exc:
            raise OutboxError("active outbox signing key is not externally trusted") from exc
        if signer.identity != trusted_identity or signer.identity.key_id != active_key_id:
            raise OutboxError("outbox signer is not the externally trusted active key")
        self.signer = signer
        self.trust_store = trust_store
        self.active_key_id = active_key_id
        with open_v3_connection(self.database_path) as connection:
            migrate_connection(connection)
            verify_outbox_integrity(connection, trust_store=self.trust_store)

    def enqueue(
        self,
        *,
        outbox_id: str,
        destination: str,
        payload: Mapping[str, Any],
        created_at: str,
        source_global_sequence: int | None = None,
    ) -> OutboxItem:
        _require_token(outbox_id, "outbox id")
        _require_token(destination, "outbox destination")
        timestamp = require_utc_milliseconds(created_at)
        if not isinstance(payload, Mapping):
            raise OutboxError("outbox payload must be a mapping")
        encoded = canonical_bytes(payload, max_bytes=MAX_OUTBOX_PAYLOAD_BYTES)
        digest = canonical_digest(payload, max_bytes=MAX_OUTBOX_PAYLOAD_BYTES)
        if source_global_sequence is not None and (
            isinstance(source_global_sequence, bool)
            or not isinstance(source_global_sequence, int)
            or source_global_sequence <= 0
        ):
            raise OutboxError("source global sequence must be a positive integer")
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                verify_outbox_integrity(connection, trust_store=self.trust_store)
                existing = connection.execute(
                    "SELECT * FROM v3_outbox WHERE outbox_id=?", (outbox_id,)
                ).fetchone()
                if existing is not None:
                    item = _decode(existing)
                    if (
                        item.destination != destination
                        or item.source_global_sequence != source_global_sequence
                        or item.payload_digest != digest
                        or item.payload_json != encoded.decode("utf-8")
                        or item.created_at != timestamp
                    ):
                        raise OutboxConflict(
                            "outbox identity already binds different immutable material"
                        )
                    return item
                connection.execute(
                    "INSERT INTO v3_outbox(outbox_id, destination, source_global_sequence, "
                    "payload_json, payload_digest, state, revision, attempt_count, "
                    "next_attempt_at, terminal_reason, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', 1, 0, ?, NULL, ?, ?)",
                    (
                        outbox_id,
                        destination,
                        source_global_sequence,
                        encoded.decode("utf-8"),
                        digest,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                return self._get_connection(connection, outbox_id)

    def get(self, outbox_id: str) -> OutboxItem:
        _require_token(outbox_id, "outbox id")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            verify_outbox_integrity(connection, trust_store=self.trust_store)
            row = connection.execute(
                "SELECT * FROM v3_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise KeyError(outbox_id)
            return _decode(row)

    def due(self, through: str, *, limit: int) -> tuple[OutboxItem, ...]:
        timestamp = require_utc_milliseconds(through)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise OutboxError("outbox page limit must be between 1 and 1000")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            verify_outbox_integrity(connection, trust_store=self.trust_store)
            rows = connection.execute(
                "SELECT * FROM v3_outbox WHERE state IN ('pending', 'transient', 'repaired') "
                "AND next_attempt_at <= ? ORDER BY next_attempt_at, created_at, outbox_id LIMIT ?",
                (timestamp, limit),
            ).fetchall()
            return tuple(_decode(row) for row in rows)

    def record_outcome(
        self,
        outbox_id: str,
        *,
        operation_id: str,
        expected_revision: int,
        outcome: DeliveryOutcome,
        observed_at: str,
        reason: str | None = None,
        fault_hook: FaultHook | None = None,
    ) -> OutboxItem:
        if not isinstance(outcome, DeliveryOutcome):
            raise OutboxError("delivery outcome must be a DeliveryOutcome")
        timestamp = require_utc_milliseconds(observed_at)
        _validate_reason(reason, required=outcome is not DeliveryOutcome.ACKNOWLEDGED)
        _require_token(operation_id, "outbox operation id")
        if fault_hook is not None and not callable(fault_hook):
            raise OutboxError("outbox fault hook must be callable")
        material_digest = _operation_digest(
            operation_id,
            outbox_id,
            expected_revision,
            outcome.value,
            timestamp,
            reason,
        )
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                verify_outbox_integrity(connection, trust_store=self.trust_store)
                retry = self._resolve_operation(connection, operation_id, material_digest)
                if retry is not None:
                    return retry
                current = self._require_mutable(connection, outbox_id, expected_revision)
                if current.state not in {
                    OutboxState.PENDING,
                    OutboxState.TRANSIENT,
                    OutboxState.REPAIRED,
                }:
                    raise OutboxConflict("terminal outbox state cannot record a delivery outcome")
                attempts = current.attempt_count + 1
                if outcome is DeliveryOutcome.TRANSIENT:
                    delay = min(
                        self.maximum_backoff_ms,
                        self.base_backoff_ms * (2 ** min(attempts - 1, 30)),
                    )
                    next_attempt = _add_milliseconds(timestamp, delay)
                    terminal_reason = reason
                else:
                    next_attempt = None
                    terminal_reason = reason
                connection.execute(
                    "UPDATE v3_outbox SET state=?, revision=revision+1, attempt_count=?, "
                    "next_attempt_at=?, terminal_reason=?, updated_at=? WHERE outbox_id=?",
                    (
                        outcome.value,
                        attempts,
                        next_attempt,
                        terminal_reason,
                        timestamp,
                        outbox_id,
                    ),
                )
                _fault(fault_hook, "after_state_update")
                result = self._get_connection(connection, outbox_id)
                self._record_transition(
                    connection,
                    operation_id=operation_id,
                    outbox_id=outbox_id,
                    expected_revision=expected_revision,
                    operation_kind=outcome.value,
                    material_digest=material_digest,
                    from_state=current.state,
                    result=result,
                    reason=reason,
                    observed_at=timestamp,
                )
                _fault(fault_hook, "after_transition_record")
                return result

    def quarantine(
        self,
        outbox_id: str,
        *,
        operation_id: str,
        expected_revision: int,
        observed_at: str,
        reason: str,
    ) -> OutboxItem:
        return self._operator_transition(
            outbox_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            observed_at=observed_at,
            reason=reason,
            target=OutboxState.QUARANTINED,
        )

    def repair(
        self,
        outbox_id: str,
        *,
        operation_id: str,
        expected_revision: int,
        observed_at: str,
        reason: str,
    ) -> OutboxItem:
        return self._operator_transition(
            outbox_id,
            operation_id=operation_id,
            expected_revision=expected_revision,
            observed_at=observed_at,
            reason=reason,
            target=OutboxState.REPAIRED,
        )

    def _operator_transition(
        self,
        outbox_id: str,
        *,
        operation_id: str,
        expected_revision: int,
        observed_at: str,
        reason: str,
        target: OutboxState,
    ) -> OutboxItem:
        timestamp = require_utc_milliseconds(observed_at)
        _validate_reason(reason, required=True)
        _require_token(operation_id, "outbox operation id")
        operation_kind = "quarantine" if target is OutboxState.QUARANTINED else "repair"
        material_digest = _operation_digest(
            operation_id,
            outbox_id,
            expected_revision,
            operation_kind,
            timestamp,
            reason,
        )
        with open_v3_connection(self.database_path) as connection:
            with immediate_transaction(connection):
                verify_outbox_integrity(connection, trust_store=self.trust_store)
                retry = self._resolve_operation(connection, operation_id, material_digest)
                if retry is not None:
                    return retry
                current = self._require_mutable(connection, outbox_id, expected_revision)
                if target is OutboxState.QUARANTINED:
                    if current.state is OutboxState.ACKNOWLEDGED:
                        raise OutboxConflict("acknowledged outbox delivery is immutable")
                    next_attempt = None
                else:
                    if current.state not in {OutboxState.PERMANENT, OutboxState.QUARANTINED}:
                        raise OutboxConflict("only terminal failures may be marked repaired")
                    next_attempt = timestamp
                connection.execute(
                    "UPDATE v3_outbox SET state=?, revision=revision+1, next_attempt_at=?, "
                    "terminal_reason=?, updated_at=? WHERE outbox_id=?",
                    (target.value, next_attempt, reason, timestamp, outbox_id),
                )
                result = self._get_connection(connection, outbox_id)
                self._record_transition(
                    connection,
                    operation_id=operation_id,
                    outbox_id=outbox_id,
                    expected_revision=expected_revision,
                    operation_kind=operation_kind,
                    material_digest=material_digest,
                    from_state=current.state,
                    result=result,
                    reason=reason,
                    observed_at=timestamp,
                )
                return result

    def history(self, outbox_id: str) -> tuple[OutboxTransition, ...]:
        _require_token(outbox_id, "outbox id")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            verify_outbox_integrity(connection, trust_store=self.trust_store)
            rows = connection.execute(
                "SELECT * FROM v3_outbox_transitions WHERE outbox_id=? "
                "ORDER BY transition_sequence",
                (outbox_id,),
            ).fetchall()
            return tuple(_decode_transition(row) for row in rows)

    def _record_transition(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        outbox_id: str,
        expected_revision: int,
        operation_kind: str,
        material_digest: str,
        from_state: OutboxState,
        result: OutboxItem,
        reason: str | None,
        observed_at: str,
    ) -> None:
        prior = connection.execute(
            "SELECT transition_sequence, transition_digest FROM v3_outbox_transitions "
            "ORDER BY transition_sequence DESC LIMIT 1"
        ).fetchone()
        sequence = 1 if prior is None else int(prior[0]) + 1
        prior_digest = "0" * 64 if prior is None else str(prior[1])
        transition_value = {
            "schema_version": "strathmark-v3-outbox-transition-v1",
            "transition_sequence": sequence,
            "operation_id": operation_id,
            "outbox_id": outbox_id,
            "expected_revision": expected_revision,
            "operation_kind": operation_kind,
            "material_digest": material_digest,
            "from_state": from_state.value,
            "result_state": result.state.value,
            "result_revision": result.revision,
            "result_attempt_count": result.attempt_count,
            "result_next_attempt_at": result.next_attempt_at,
            "result_terminal_reason": result.terminal_reason,
            "reason": reason,
            "observed_at": observed_at,
            "prior_transition_digest": prior_digest,
        }
        transition_digest = canonical_digest(transition_value)
        from strathmark.v3.infrastructure.integrity import sign_manifest

        signed_transition = sign_manifest(
            "outbox_transition",
            transition_value,
            signer=self.signer,
            created_at=observed_at,
        )
        signed_transition_json = canonical_bytes(signed_transition.to_dict()).decode("utf-8")
        connection.execute(
            "INSERT INTO v3_outbox_transitions(transition_sequence, operation_id, outbox_id, expected_revision, "
            "operation_kind, material_digest, from_state, result_state, result_revision, "
            "result_attempt_count, result_next_attempt_at, result_terminal_reason, reason, observed_at, "
            "prior_transition_digest, transition_digest, signed_transition_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sequence,
                operation_id,
                outbox_id,
                expected_revision,
                operation_kind,
                material_digest,
                from_state.value,
                result.state.value,
                result.revision,
                result.attempt_count,
                result.next_attempt_at,
                result.terminal_reason,
                reason,
                observed_at,
                prior_digest,
                transition_digest,
                signed_transition_json,
            ),
        )

    @staticmethod
    def _resolve_operation(
        connection: sqlite3.Connection, operation_id: str, material_digest: str
    ) -> OutboxItem | None:
        row = connection.execute(
            "SELECT transition.*, outbox.destination, outbox.source_global_sequence, "
            "outbox.payload_json, outbox.payload_digest, outbox.created_at "
            "FROM v3_outbox_transitions transition JOIN v3_outbox outbox "
            "ON outbox.outbox_id=transition.outbox_id WHERE transition.operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        if str(row["material_digest"]) != material_digest:
            raise OutboxConflict("outbox operation identity binds different material")
        return OutboxItem(
            outbox_id=str(row["outbox_id"]),
            destination=str(row["destination"]),
            source_global_sequence=(
                None
                if row["source_global_sequence"] is None
                else int(row["source_global_sequence"])
            ),
            payload_json=str(row["payload_json"]),
            payload_digest=str(row["payload_digest"]),
            state=OutboxState(str(row["result_state"])),
            revision=int(row["result_revision"]),
            attempt_count=int(row["result_attempt_count"]),
            next_attempt_at=(
                None
                if row["result_next_attempt_at"] is None
                else str(row["result_next_attempt_at"])
            ),
            terminal_reason=(
                None
                if row["result_terminal_reason"] is None
                else str(row["result_terminal_reason"])
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["observed_at"]),
        )

    @staticmethod
    def _require_mutable(
        connection: sqlite3.Connection, outbox_id: str, expected_revision: int
    ) -> OutboxItem:
        _require_token(outbox_id, "outbox id")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise OutboxError("expected revision must be a positive integer")
        row = connection.execute(
            "SELECT * FROM v3_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise KeyError(outbox_id)
        item = _decode(row)
        if item.revision != expected_revision:
            raise OutboxConflict("outbox expected revision is stale")
        return item

    @staticmethod
    def _get_connection(connection: sqlite3.Connection, outbox_id: str) -> OutboxItem:
        row = connection.execute(
            "SELECT * FROM v3_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        assert row is not None
        return _decode(row)


def _decode(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        outbox_id=str(row["outbox_id"]),
        destination=str(row["destination"]),
        source_global_sequence=(
            None if row["source_global_sequence"] is None else int(row["source_global_sequence"])
        ),
        payload_json=str(row["payload_json"]),
        payload_digest=str(row["payload_digest"]),
        state=OutboxState(str(row["state"])),
        revision=int(row["revision"]),
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=(None if row["next_attempt_at"] is None else str(row["next_attempt_at"])),
        terminal_reason=(None if row["terminal_reason"] is None else str(row["terminal_reason"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _decode_transition(row: sqlite3.Row) -> OutboxTransition:
    return OutboxTransition(
        transition_sequence=int(row["transition_sequence"]),
        operation_id=str(row["operation_id"]),
        outbox_id=str(row["outbox_id"]),
        expected_revision=int(row["expected_revision"]),
        operation_kind=str(row["operation_kind"]),
        material_digest=str(row["material_digest"]),
        from_state=OutboxState(str(row["from_state"])),
        result_state=OutboxState(str(row["result_state"])),
        result_revision=int(row["result_revision"]),
        result_attempt_count=int(row["result_attempt_count"]),
        result_next_attempt_at=(
            None if row["result_next_attempt_at"] is None else str(row["result_next_attempt_at"])
        ),
        result_terminal_reason=(
            None if row["result_terminal_reason"] is None else str(row["result_terminal_reason"])
        ),
        reason=None if row["reason"] is None else str(row["reason"]),
        observed_at=str(row["observed_at"]),
        prior_transition_digest=str(row["prior_transition_digest"]),
        transition_digest=str(row["transition_digest"]),
        signed_transition_json=str(row["signed_transition_json"]),
    )


def verify_outbox_integrity(connection: sqlite3.Connection, *, trust_store: Any) -> None:
    """Reconstruct every mutable row from immutable payload and transition authority."""

    from strathmark.v3.infrastructure.integrity import (
        IntegrityError,
        IntegrityTrustStore,
        SignedManifest,
        verify_manifest,
    )

    if not isinstance(trust_store, IntegrityTrustStore):
        raise OutboxError("outbox verification requires an external typed trust store")
    try:
        rows = connection.execute("SELECT * FROM v3_outbox ORDER BY outbox_id").fetchall()
        items = {str(row["outbox_id"]): _decode(row) for row in rows}
        reconstructed: dict[str, OutboxItem] = {}
        for outbox_id, item in items.items():
            _require_token(outbox_id, "persisted outbox id")
            _require_token(item.destination, "persisted outbox destination")
            payload = json.loads(item.payload_json)
            if not isinstance(payload, dict):
                raise OutboxError("persisted outbox payload is not an object")
            encoded = canonical_bytes(payload, max_bytes=MAX_OUTBOX_PAYLOAD_BYTES).decode("utf-8")
            if (
                encoded != item.payload_json
                or canonical_digest(payload, max_bytes=MAX_OUTBOX_PAYLOAD_BYTES)
                != item.payload_digest
            ):
                raise OutboxError("persisted outbox payload bytes or digest are not canonical")
            created_at = require_utc_milliseconds(item.created_at)
            reconstructed[outbox_id] = OutboxItem(
                outbox_id,
                item.destination,
                item.source_global_sequence,
                item.payload_json,
                item.payload_digest,
                OutboxState.PENDING,
                1,
                0,
                created_at,
                None,
                created_at,
                created_at,
            )

        transitions = connection.execute(
            "SELECT * FROM v3_outbox_transitions ORDER BY transition_sequence"
        ).fetchall()
        prior_digest = "0" * 64
        for expected_sequence, row in enumerate(transitions, start=1):
            transition = _decode_transition(row)
            if transition.transition_sequence != expected_sequence:
                raise OutboxError("outbox transition sequence has a gap or reorder")
            if transition.outbox_id not in reconstructed:
                raise OutboxError("outbox transition references an unknown payload")
            current = reconstructed[transition.outbox_id]
            _require_token(transition.operation_id, "persisted outbox operation id")
            observed_at = require_utc_milliseconds(transition.observed_at)
            _validate_reason(
                transition.reason,
                required=transition.operation_kind != DeliveryOutcome.ACKNOWLEDGED.value,
            )
            if (
                transition.expected_revision != current.revision
                or transition.result_revision != current.revision + 1
                or transition.from_state is not current.state
            ):
                raise OutboxError("outbox transition revision/state history is not contiguous")
            expected_material = _operation_digest(
                transition.operation_id,
                transition.outbox_id,
                transition.expected_revision,
                transition.operation_kind,
                observed_at,
                transition.reason,
            )
            if transition.material_digest != expected_material:
                raise OutboxError("outbox transition operation digest differs")
            transition_value = {
                "schema_version": "strathmark-v3-outbox-transition-v1",
                "transition_sequence": transition.transition_sequence,
                "operation_id": transition.operation_id,
                "outbox_id": transition.outbox_id,
                "expected_revision": transition.expected_revision,
                "operation_kind": transition.operation_kind,
                "material_digest": transition.material_digest,
                "from_state": transition.from_state.value,
                "result_state": transition.result_state.value,
                "result_revision": transition.result_revision,
                "result_attempt_count": transition.result_attempt_count,
                "result_next_attempt_at": transition.result_next_attempt_at,
                "result_terminal_reason": transition.result_terminal_reason,
                "reason": transition.reason,
                "observed_at": observed_at,
                "prior_transition_digest": transition.prior_transition_digest,
            }
            if (
                transition.prior_transition_digest != prior_digest
                or transition.transition_digest != canonical_digest(transition_value)
            ):
                raise OutboxError("outbox transition digest chain differs")
            delivery = transition.operation_kind in {item.value for item in DeliveryOutcome}
            if delivery:
                if current.state not in {
                    OutboxState.PENDING,
                    OutboxState.TRANSIENT,
                    OutboxState.REPAIRED,
                }:
                    raise OutboxError("terminal outbox state has a delivery transition")
                expected_state = OutboxState(transition.operation_kind)
                expected_attempts = current.attempt_count + 1
                if expected_state is OutboxState.TRANSIENT:
                    if (
                        transition.result_next_attempt_at is None
                        or require_utc_milliseconds(transition.result_next_attempt_at)
                        <= observed_at
                    ):
                        raise OutboxError("transient transition has no future retry instant")
                elif transition.result_next_attempt_at is not None:
                    raise OutboxError("terminal delivery transition retained a retry instant")
            elif transition.operation_kind == "quarantine":
                if current.state is OutboxState.ACKNOWLEDGED:
                    raise OutboxError("acknowledged delivery was quarantined")
                expected_state = OutboxState.QUARANTINED
                expected_attempts = current.attempt_count
                if transition.result_next_attempt_at is not None:
                    raise OutboxError("quarantine transition retained a retry instant")
            elif transition.operation_kind == "repair":
                if current.state not in {OutboxState.PERMANENT, OutboxState.QUARANTINED}:
                    raise OutboxError("repair transition did not follow a terminal failure")
                expected_state = OutboxState.REPAIRED
                expected_attempts = current.attempt_count
                if transition.result_next_attempt_at != observed_at:
                    raise OutboxError("repair transition retry instant differs")
            else:
                raise OutboxError("outbox transition kind is unknown")
            if (
                transition.result_state is not expected_state
                or transition.result_attempt_count != expected_attempts
                or transition.result_terminal_reason != transition.reason
            ):
                raise OutboxError("outbox transition result material is inconsistent")
            try:
                signed_value = json.loads(transition.signed_transition_json)
                if not isinstance(signed_value, dict):
                    raise OutboxError("signed outbox transition is not an object")
                signed_transition = SignedManifest.from_dict(signed_value)
                if (
                    signed_transition.kind != "outbox_transition"
                    or canonical_bytes(signed_transition.to_dict()).decode("utf-8")
                    != transition.signed_transition_json
                    or verify_manifest(signed_transition, trust_store) != transition_value
                ):
                    raise OutboxError("signed outbox transition binding differs")
            except OutboxError:
                raise
            except (IntegrityError, TypeError, ValueError) as exc:
                raise OutboxError("outbox transition signature is invalid or untrusted") from exc
            prior_digest = transition.transition_digest
            reconstructed[transition.outbox_id] = OutboxItem(
                current.outbox_id,
                current.destination,
                current.source_global_sequence,
                current.payload_json,
                current.payload_digest,
                transition.result_state,
                transition.result_revision,
                transition.result_attempt_count,
                transition.result_next_attempt_at,
                transition.result_terminal_reason,
                current.created_at,
                observed_at,
            )

        for outbox_id, item in items.items():
            if reconstructed[outbox_id] != item:
                raise OutboxError("outbox mutable row differs from immutable transition history")
    except OutboxError:
        raise
    except Exception as exc:
        raise OutboxError("outbox authority cannot be decoded or reconstructed") from exc


def _require_token(value: str, label: str) -> None:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise OutboxError(f"{label} must be a bounded opaque token")


def _validate_reason(reason: str | None, *, required: bool) -> None:
    if reason is None:
        if required:
            raise OutboxError("a bounded machine reason is required")
        return
    if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        raise OutboxError("outbox reason must be a bounded machine token")


def _add_milliseconds(timestamp: str, milliseconds: int) -> str:
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return (moment + timedelta(milliseconds=milliseconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[
        :-3
    ] + "Z"


def _operation_digest(
    operation_id: str,
    outbox_id: str,
    expected_revision: int,
    operation_kind: str,
    observed_at: str,
    reason: str | None,
) -> str:
    return canonical_digest(
        {
            "schema_version": "strathmark-v3-outbox-operation-v1",
            "operation_id": operation_id,
            "outbox_id": outbox_id,
            "expected_revision": expected_revision,
            "operation_kind": operation_kind,
            "observed_at": observed_at,
            "reason": reason,
        }
    )


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


__all__ = [
    "DeliveryOutcome",
    "MAX_OUTBOX_PAYLOAD_BYTES",
    "OutboxConflict",
    "OutboxError",
    "OutboxItem",
    "OutboxRepository",
    "OutboxState",
    "OutboxTransition",
    "verify_outbox_integrity",
]
