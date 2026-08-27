"""Durable bounded monitoring of newly settled evidence for automatic factory rollback."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from strathmark.v3.application.factory import (
    FactoryError,
    FactoryService,
    MonitoringObservation,
    MonitoringReceipt,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.events import EventKind
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
)
from strathmark.v3.factory.evaluator import EvaluationGate
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore

_CURSOR_MANIFEST_KIND = "factory_monitoring_cursor"


class MonitoringExecutionBoundary(str, Enum):
    CONFIGURED_LOCAL_ONLY = "configured_local_only"


@dataclass(frozen=True, slots=True)
class MonitoringPolicy:
    gates: tuple[EvaluationGate, ...]
    policy_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.gates, tuple)
            or not self.gates
            or len(self.gates) > 32
            or self.gates != tuple(sorted(self.gates, key=lambda item: item.name))
            or len({item.name for item in self.gates}) != len(self.gates)
        ):
            raise FactoryError("monitoring policy gates must be bounded, unique, and sorted")
        _digest(self.policy_digest, "monitoring policy")
        if canonical_digest(self.body()) != self.policy_digest:
            raise FactoryError("monitoring policy digest differs")

    @classmethod
    def create(cls, *, gates: tuple[EvaluationGate, ...]) -> MonitoringPolicy:
        ordered = tuple(sorted(gates, key=lambda item: item.name))
        body = {
            "schema_version": "strathmark-v3-continuous-monitoring-policy-v1",
            "gates": [item.to_dict() for item in ordered],
        }
        return cls(ordered, canonical_digest(body))

    def body(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-continuous-monitoring-policy-v1",
            "gates": [item.to_dict() for item in self.gates],
        }


@dataclass(frozen=True, slots=True)
class SettledEvidenceFact:
    source_global_sequence: int
    source_event_digest: str
    settlement_id: str
    receipt_id: str
    bundle_digest: str
    settlement_payload_digest: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.source_global_sequence, bool)
            or not isinstance(self.source_global_sequence, int)
            or self.source_global_sequence <= 0
        ):
            raise FactoryError("settled evidence source sequence is invalid")
        for value, label in (
            (self.source_event_digest, "settlement event"),
            (self.bundle_digest, "settlement bundle"),
            (self.settlement_payload_digest, "settlement payload"),
        ):
            _digest(value, label)
        for value, namespace in (
            (self.settlement_id, "settlement"),
            (self.receipt_id, "receipt"),
        ):
            try:
                StableIdentifier(value)
            except (TypeError, ValueError) as exc:
                raise FactoryError(f"settled evidence {namespace} identity is invalid") from exc
            if not value.startswith(f"{namespace}:"):
                raise FactoryError(f"settled evidence {namespace} identity is invalid")

    def body(self) -> dict[str, object]:
        return {
            "source_global_sequence": self.source_global_sequence,
            "source_event_digest": self.source_event_digest,
            "settlement_id": self.settlement_id,
            "receipt_id": self.receipt_id,
            "bundle_digest": self.bundle_digest,
            "settlement_payload_digest": self.settlement_payload_digest,
        }


@dataclass(frozen=True, slots=True)
class SettledEvidenceWindow:
    window_id: str
    through_global_sequence: int
    bundle_digest: str
    settled_evidence_digest: str
    policy_digest: str
    facts: tuple[SettledEvidenceFact, ...]
    metrics: Mapping[str, float]

    def __post_init__(self) -> None:
        if not isinstance(self.window_id, str) or not self.window_id or len(self.window_id) > 128:
            raise FactoryError("settled evidence window identity is invalid")
        if (
            not isinstance(self.facts, tuple)
            or not self.facts
            or len(self.facts) > 64
            or self.facts != tuple(sorted(self.facts, key=lambda item: item.source_global_sequence))
            or len({item.source_global_sequence for item in self.facts}) != len(self.facts)
        ):
            raise FactoryError("settled evidence facts must be bounded, unique, and sorted")
        if self.through_global_sequence != self.facts[-1].source_global_sequence:
            raise FactoryError("settled evidence window cursor differs")
        if any(item.bundle_digest != self.bundle_digest for item in self.facts):
            raise FactoryError("settled evidence window crosses bundle authority")
        for value, label in (
            (self.bundle_digest, "settled evidence bundle"),
            (self.settled_evidence_digest, "settled evidence"),
            (self.policy_digest, "settled evidence policy"),
        ):
            _digest(value, label)
        if canonical_digest([item.body() for item in self.facts]) != self.settled_evidence_digest:
            raise FactoryError("settled evidence window digest differs")
        if not isinstance(self.metrics, Mapping) or not self.metrics:
            raise FactoryError("settled evidence metrics are absent")

    def observation(self, policy: MonitoringPolicy) -> MonitoringObservation:
        if policy.policy_digest != self.policy_digest:
            raise FactoryError("settled evidence monitoring policy differs")
        return MonitoringObservation.create(
            window_id=self.window_id,
            bundle_digest=self.bundle_digest,
            settled_evidence_digest=self.settled_evidence_digest,
            policy_digest=self.policy_digest,
            gates=policy.gates,
            metrics=self.metrics,
        )


class ReceiptBundleAuthority(Protocol):
    def bundle_digest(self, receipt_id: str) -> str: ...


class SettlementMetricEvaluator(Protocol):
    execution_boundary: MonitoringExecutionBoundary

    def evaluate(self, facts: tuple[SettledEvidenceFact, ...]) -> Mapping[str, float]: ...


class SQLiteReceiptBundleAuthority:
    def __init__(self, database_path: str | Path) -> None:
        self._projections = SQLiteProjectionStore(database_path)

    def bundle_digest(self, receipt_id: str) -> str:
        try:
            receipt = self._projections.verified_receipt(receipt_id)
        except Exception as exc:
            raise FactoryError("receipt lacks verified bundle authority") from exc
        runtime_bundles = tuple(item.digest for item in receipt.bundles if item.role == "runtime")
        if len(runtime_bundles) != 1:
            raise FactoryError("receipt lacks one verified runtime bundle authority")
        return _digest(runtime_bundles[0], "verified receipt runtime bundle")


class SQLiteSettledEvidenceSource:
    """Construct monitoring windows only from verified settlement and receipt authority."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        receipt_authority: ReceiptBundleAuthority | None = None,
        policy: MonitoringPolicy,
        metric_evaluator: SettlementMetricEvaluator,
    ) -> None:
        if not isinstance(policy, MonitoringPolicy):
            raise FactoryError("settled evidence source requires a monitoring policy")
        if (
            getattr(metric_evaluator, "execution_boundary", None)
            is not MonitoringExecutionBoundary.CONFIGURED_LOCAL_ONLY
        ):
            raise FactoryError("settled evidence metrics require the configured local boundary")
        if not callable(getattr(metric_evaluator, "evaluate", None)):
            raise FactoryError("settled evidence source requires a metric evaluator")
        self._events = SQLiteEventStore(database_path)
        self._receipt_authority = receipt_authority or SQLiteReceiptBundleAuthority(database_path)
        if not callable(getattr(self._receipt_authority, "bundle_digest", None)):
            raise FactoryError("settled evidence source requires receipt authority")
        self.policy = policy
        self._metric_evaluator = metric_evaluator

    def load_after(
        self, through_global_sequence: int, *, limit: int
    ) -> tuple[SettledEvidenceWindow, ...]:
        if (
            isinstance(through_global_sequence, bool)
            or not isinstance(through_global_sequence, int)
            or through_global_sequence < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 32
        ):
            raise FactoryError("settled evidence cursor or limit is invalid")
        with open_v3_connection(self._events.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT global_sequence FROM v3_events WHERE global_sequence>? "
                "AND event_kind=? ORDER BY global_sequence LIMIT ?",
                (through_global_sequence, EventKind.LIVE_RACE_SETTLED.value, limit),
            ).fetchall()
        windows: list[SettledEvidenceWindow] = []
        for row in rows:
            event = self._events.event_at(int(row[0]))
            if event.kind is not EventKind.LIVE_RACE_SETTLED:
                raise FactoryError("settled evidence source event kind differs")
            payload = event.command.payload.to_value()
            settlement = payload.get("settlement", payload)
            if not isinstance(settlement, Mapping):
                raise FactoryError("settled evidence payload is malformed")
            receipt_id = settlement.get("receipt_id")
            if not isinstance(receipt_id, str):
                raise FactoryError("settled evidence receipt identity is absent")
            bundle_digest = self._receipt_authority.bundle_digest(receipt_id)
            fact = SettledEvidenceFact(
                event.global_sequence,
                event.event_digest,
                str(event.aggregate_id),
                receipt_id,
                bundle_digest,
                event.command.payload.digest,
            )
            facts = (fact,)
            try:
                metrics = self._metric_evaluator.evaluate(facts)
            except Exception as exc:
                raise FactoryError("settled evidence metric evaluation failed closed") from exc
            if not isinstance(metrics, Mapping):
                raise FactoryError("settled evidence metrics are malformed")
            normalized = {name: metrics[name] for name in sorted(metrics)}
            evidence_digest = canonical_digest([fact.body()])
            windows.append(
                SettledEvidenceWindow(
                    f"settlement-window:{event.global_sequence}",
                    event.global_sequence,
                    bundle_digest,
                    evidence_digest,
                    self.policy.policy_digest,
                    facts,
                    MappingProxyType(normalized),
                )
            )
        return tuple(windows)


@dataclass(frozen=True, slots=True)
class MonitoringCursor:
    through_global_sequence: int
    last_window_digest: str
    updated_at: str
    pending_through_global_sequence: int | None = None
    pending_evidence_digest: str | None = None
    pending_observation_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.through_global_sequence, bool)
            or not isinstance(self.through_global_sequence, int)
            or self.through_global_sequence < 0
        ):
            raise FactoryError("monitoring cursor sequence is invalid")
        _digest(self.last_window_digest, "monitoring cursor window")
        require_utc_milliseconds(self.updated_at)
        pending = (
            self.pending_through_global_sequence,
            self.pending_evidence_digest,
            self.pending_observation_digest,
        )
        if all(item is None for item in pending):
            return
        if any(item is None for item in pending):
            raise FactoryError("monitoring cursor pending checkpoint is incomplete")
        if (
            isinstance(self.pending_through_global_sequence, bool)
            or not isinstance(self.pending_through_global_sequence, int)
            or self.pending_through_global_sequence <= self.through_global_sequence
        ):
            raise FactoryError("monitoring cursor pending sequence is invalid")
        _digest(self.pending_evidence_digest, "monitoring cursor pending evidence")
        _digest(self.pending_observation_digest, "monitoring cursor pending observation")

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-factory-monitoring-cursor-v2",
            "through_global_sequence": self.through_global_sequence,
            "last_window_digest": self.last_window_digest,
            "updated_at": self.updated_at,
            "pending_through_global_sequence": self.pending_through_global_sequence,
            "pending_evidence_digest": self.pending_evidence_digest,
            "pending_observation_digest": self.pending_observation_digest,
        }


class DurableMonitoringCursorStore:
    def __init__(
        self,
        path: str | Path,
        *,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
    ) -> None:
        if not callable(getattr(signer, "sign", None)) or not isinstance(
            trust_store, IntegrityTrustStore
        ):
            raise FactoryError("monitoring cursor requires pinned signing authority")
        trust_store.identity(signer.identity.key_id)
        self.path = Path(path).expanduser().resolve(strict=False)
        self._signer = signer
        self._trust_store = trust_store

    def load(self) -> MonitoringCursor:
        if not self.path.exists():
            return MonitoringCursor(0, "0" * 64, "1970-01-01T00:00:00.000Z")
        try:
            value = json.loads(self.path.read_bytes())
            manifest = SignedManifest.from_dict(value)
            if manifest.kind != _CURSOR_MANIFEST_KIND:
                raise FactoryError("monitoring cursor manifest kind differs")
            payload = dict(verify_manifest(manifest, self._trust_store))
            if (
                set(payload)
                != {
                    "schema_version",
                    "through_global_sequence",
                    "last_window_digest",
                    "updated_at",
                    "pending_through_global_sequence",
                    "pending_evidence_digest",
                    "pending_observation_digest",
                }
                or payload.get("schema_version") != "strathmark-v3-factory-monitoring-cursor-v2"
            ):
                raise FactoryError("monitoring cursor payload is not closed")
            return MonitoringCursor(
                payload["through_global_sequence"],
                payload["last_window_digest"],
                payload["updated_at"],
                payload["pending_through_global_sequence"],
                payload["pending_evidence_digest"],
                payload["pending_observation_digest"],
            )
        except FactoryError:
            raise
        except Exception as exc:
            raise FactoryError("monitoring cursor verification failed closed") from exc

    def persist(self, state: MonitoringCursor) -> None:
        if not isinstance(state, MonitoringCursor):
            raise FactoryError("monitoring cursor state must be typed")
        current = self.load()
        if state.through_global_sequence < current.through_global_sequence:
            raise FactoryError("monitoring cursor cannot regress")
        if current.pending_through_global_sequence is None:
            if state.through_global_sequence != current.through_global_sequence:
                raise FactoryError("monitoring cursor cannot advance without a pending checkpoint")
        elif state.pending_through_global_sequence is None:
            if (
                state.through_global_sequence != current.pending_through_global_sequence
                or state.last_window_digest != current.pending_evidence_digest
            ):
                raise FactoryError("monitoring cursor completion differs from pending checkpoint")
        elif (
            state.through_global_sequence != current.through_global_sequence
            or state.pending_through_global_sequence != current.pending_through_global_sequence
            or state.pending_evidence_digest != current.pending_evidence_digest
            or state.pending_observation_digest != current.pending_observation_digest
        ):
            raise FactoryError("monitoring cursor pending checkpoint cannot be replaced")
        manifest = sign_manifest(
            _CURSOR_MANIFEST_KIND,
            state.payload(),
            signer=self._signer,
            created_at=state.updated_at,
        )
        encoded = canonical_bytes(manifest.to_dict())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
            ) as handle:
                temporary_name = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)


@dataclass(frozen=True, slots=True)
class MonitoringCycleOutcome:
    processed_window_count: int
    skipped_window_count: int
    receipts: tuple[MonitoringReceipt, ...]
    cursor: MonitoringCursor


class ContinuousFactoryMonitoringRunner:
    def __init__(
        self,
        *,
        service: FactoryService,
        source: SQLiteSettledEvidenceSource,
        cursor_store: DurableMonitoringCursorStore,
        actor_id: StableIdentifier,
        clock: Callable[[], str],
        monotonic_clock: Callable[[], int],
        max_windows_per_cycle: int = 8,
    ) -> None:
        if (
            not isinstance(service, FactoryService)
            or not isinstance(source, SQLiteSettledEvidenceSource)
            or not isinstance(cursor_store, DurableMonitoringCursorStore)
        ):
            raise FactoryError("continuous monitoring requires typed services")
        if not callable(clock) or not callable(monotonic_clock):
            raise FactoryError("continuous monitoring requires explicit clocks")
        if (
            isinstance(max_windows_per_cycle, bool)
            or not isinstance(max_windows_per_cycle, int)
            or not 1 <= max_windows_per_cycle <= 32
        ):
            raise FactoryError("continuous monitoring cycle bound is invalid")
        self._service = service
        self._source = source
        self._cursor_store = cursor_store
        self._actor_id = actor_id
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._limit = max_windows_per_cycle

    def run_once(self) -> MonitoringCycleOutcome:
        cursor = self._cursor_store.load()
        windows = self._source.load_after(cursor.through_global_sequence, limit=self._limit)
        if cursor.pending_through_global_sequence is not None and not windows:
            raise FactoryError("pending monitoring source evidence is absent")
        receipts: list[MonitoringReceipt] = []
        skipped = 0
        for window in windows:
            if window.through_global_sequence <= cursor.through_global_sequence:
                raise FactoryError("settled evidence source did not advance monotonically")
            command_id = IdempotencyKey(
                str(
                    deterministic_identifier(
                        "monitoring_command",
                        {
                            "window_id": window.window_id,
                            "source_global_sequence": window.through_global_sequence,
                            "bundle_digest": window.bundle_digest,
                            "policy_digest": window.policy_digest,
                        },
                    )
                )
            )
            observation = window.observation(self._source.policy)
            if cursor.pending_through_global_sequence is None:
                cursor = MonitoringCursor(
                    cursor.through_global_sequence,
                    cursor.last_window_digest,
                    require_utc_milliseconds(self._clock()),
                    window.through_global_sequence,
                    window.settled_evidence_digest,
                    observation.observation_digest,
                )
                self._cursor_store.persist(cursor)
            elif (
                cursor.pending_through_global_sequence != window.through_global_sequence
                or cursor.pending_evidence_digest != window.settled_evidence_digest
                or cursor.pending_observation_digest != observation.observation_digest
            ):
                raise FactoryError("pending monitoring observation differs")
            try:
                receipt = self._service.record_monitoring(
                    observation,
                    command_id=command_id,
                    actor_id=self._actor_id,
                    occurred_at_utc=self._clock(),
                    monotonic_elapsed_ms=self._monotonic_clock(),
                )
            except FactoryError as exc:
                if (
                    str(exc) != "monitoring observation does not target the active champion"
                    or self._service.active_bundle_digest() == window.bundle_digest
                ):
                    raise
                skipped += 1
            else:
                receipts.append(receipt)
            cursor = MonitoringCursor(
                window.through_global_sequence,
                window.settled_evidence_digest,
                require_utc_milliseconds(self._clock()),
            )
            self._cursor_store.persist(cursor)
        return MonitoringCycleOutcome(len(windows), skipped, tuple(receipts), cursor)

    def run_continuously(
        self,
        *,
        stop_requested: Callable[[], bool],
        wait_for_next_cycle: Callable[[], None],
    ) -> None:
        if not callable(stop_requested) or not callable(wait_for_next_cycle):
            raise FactoryError("continuous monitoring requires stop and wait controls")
        while not stop_requested():
            self.run_once()
            wait_for_next_cycle()


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FactoryError(f"{label} digest is invalid")
    return value


__all__ = [
    "ContinuousFactoryMonitoringRunner",
    "DurableMonitoringCursorStore",
    "MonitoringCursor",
    "MonitoringCycleOutcome",
    "MonitoringExecutionBoundary",
    "MonitoringPolicy",
    "ReceiptBundleAuthority",
    "SQLiteReceiptBundleAuthority",
    "SQLiteSettledEvidenceSource",
    "SettlementMetricEvaluator",
    "SettledEvidenceFact",
    "SettledEvidenceWindow",
]
