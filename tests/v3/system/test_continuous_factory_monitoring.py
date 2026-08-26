from __future__ import annotations

from pathlib import Path

import pytest

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.application.factory import FactoryError
from strathmark.v3.application.factory_monitoring import (
    ContinuousFactoryMonitoringRunner,
    DurableMonitoringCursorStore,
    MonitoringExecutionBoundary,
    MonitoringPolicy,
    SQLiteReceiptBundleAuthority,
    SQLiteSettledEvidenceSource,
)
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.receipts import BundleIdentity
from strathmark.v3.factory.evaluator import EvaluationGate
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from strathmark.v3.infrastructure.sqlite.event_store import SQLiteEventStore
from tests.v3.evals.test_factory_audit_isolation import DIGESTS, _candidate
from tests.v3.system.test_promotion_rollback import (
    ACTOR,
    NOW,
    ZERO,
    _register_evaluate_promote,
    _report,
    _service,
)


class _ReceiptAuthority:
    def __init__(self, bindings: dict[str, str]) -> None:
        self.bindings = bindings

    def bundle_digest(self, receipt_id: str) -> str:
        try:
            return self.bindings[receipt_id]
        except KeyError as exc:
            raise FactoryError("receipt lacks verified bundle authority") from exc


class _MetricEvaluator:
    execution_boundary = MonitoringExecutionBoundary.CONFIGURED_LOCAL_ONLY

    def __init__(self, value: float = 0.40) -> None:
        self.value = value

    def evaluate(self, facts):
        assert facts
        return {"normalized_crps": self.value}


class _VerifiedReceiptProjection:
    def __init__(self, bundles: tuple[BundleIdentity, ...]) -> None:
        self._receipt = type("VerifiedReceipt", (), {"bundles": bundles})()

    def verified_receipt(self, receipt_id: str):
        assert receipt_id == "receipt:monitor-window"
        return self._receipt


def _promoted_pair(tmp_path):
    service, repository, bundle_signer, evaluator_signer, database = _service(tmp_path)
    first = _candidate(name="monitor-parent", rollback_parent_digest=ZERO)
    first_report = _report(tmp_path, first, evaluator_signer, generation="audit-monitor-parent")
    first_installed, _ = _register_evaluate_promote(
        service, repository, first, first_report, bundle_signer, key="monitor-parent"
    )
    second = _candidate(
        name="monitor-active",
        dependency_digest=DIGESTS[31],
        rollback_parent_digest=first_installed.bundle_digest,
    )
    second_report = _report(tmp_path, second, evaluator_signer, generation="audit-monitor-active")
    second_installed, _ = _register_evaluate_promote(
        service, repository, second, second_report, bundle_signer, key="monitor-active"
    )
    return service, database, first_installed, second_installed


def _append_settlement(database: Path, *, receipt_id: str) -> int:
    settlement = StableIdentifier("settlement:monitor-window")
    field = StableIdentifier("field:monitor-window")
    payload = InlinePayload.from_value(
        {
            "schema_version": "strathmark-v3-live-settlement-v1",
            "field_id": str(field),
            "field_revision": 1,
            "receipt_id": receipt_id,
            "results": [],
        }
    )
    store = SQLiteEventStore(database)
    for command_kind, command_id, expected, event_kind in (
        (
            CommandKind.OPTIMIZE_FIELD,
            "command:monitor-field-optimize",
            0,
            EventKind.FIELD_OPTIMIZED,
        ),
        (
            CommandKind.ACKNOWLEDGE_ISSUE,
            "command:monitor-field-issue",
            1,
            EventKind.FIELD_ISSUED,
        ),
    ):
        store.execute(
            CommandRequest(
                ACTOR,
                CommandEnvelope(
                    command_kind,
                    IdempotencyKey(command_id),
                    field,
                    ((str(field), expected),),
                    ACTOR,
                    InlinePayload.from_value(
                        {
                            "schema_version": "strathmark-v3-monitoring-fixture-v1",
                            "field_id": str(field),
                        }
                    ),
                ),
                (EventIntent(AggregateKind.FIELD, field, event_kind),),
                "test-monitor-field-transition-result-v1",
                {"event_kind": event_kind.value},
                NOW,
                5,
            )
        )
    stored = store.execute(
        CommandRequest(
            ACTOR,
            CommandEnvelope(
                CommandKind.SETTLE_LIVE_RACE,
                IdempotencyKey("command:monitor-settlement"),
                settlement,
                ((str(field), 2), (str(settlement), 0)),
                ACTOR,
                payload,
            ),
            (
                EventIntent(
                    AggregateKind.SETTLEMENT,
                    settlement,
                    EventKind.LIVE_RACE_SETTLED,
                ),
                EventIntent(AggregateKind.FIELD, field, EventKind.FIELD_SETTLED),
            ),
            "test-monitor-settlement-result-v1",
            {"settled": True},
            NOW,
            10,
        )
    )
    for sequence in range(stored.first_global_sequence, stored.last_global_sequence + 1):
        if store.event_at(sequence).kind is EventKind.LIVE_RACE_SETTLED:
            return sequence
    raise AssertionError("monitoring fixture did not append a settlement event")


def test_continuous_monitor_constructs_verified_observation_and_rolls_back_restart_safely(
    tmp_path,
) -> None:
    service, database, first, second = _promoted_pair(tmp_path)
    receipt_id = "receipt:monitor-window"
    settlement_sequence = _append_settlement(database, receipt_id=receipt_id)
    policy = MonitoringPolicy.create(gates=(EvaluationGate("normalized_crps", "lte", 0.25),))
    metric_evaluator = _MetricEvaluator()
    source = SQLiteSettledEvidenceSource(
        database,
        receipt_authority=_ReceiptAuthority({receipt_id: second.bundle_digest}),
        policy=policy,
        metric_evaluator=metric_evaluator,
    )
    cursor_signer = P256EphemeralSigner.generate("integrity-key:monitor-cursor")
    cursor = DurableMonitoringCursorStore(
        tmp_path / "monitoring-cursor.json",
        signer=cursor_signer,
        trust_store=IntegrityTrustStore((cursor_signer.identity,)),
    )
    runner = ContinuousFactoryMonitoringRunner(
        service=service,
        source=source,
        cursor_store=cursor,
        actor_id=ACTOR,
        clock=lambda: NOW,
        monotonic_clock=lambda: 20,
        max_windows_per_cycle=4,
    )

    cycle = runner.run_once()
    assert cycle.processed_window_count == 1
    assert cycle.skipped_window_count == 0
    assert cycle.receipts[0].rolled_back is True
    assert cycle.receipts[0].active_bundle_digest == first.bundle_digest
    assert cycle.cursor.through_global_sequence == settlement_sequence
    assert service.active_bundle_digest() == first.bundle_digest

    restarted = ContinuousFactoryMonitoringRunner(
        service=service,
        source=source,
        cursor_store=DurableMonitoringCursorStore(
            tmp_path / "monitoring-cursor.json",
            signer=cursor_signer,
            trust_store=IntegrityTrustStore((cursor_signer.identity,)),
        ),
        actor_id=ACTOR,
        clock=lambda: NOW,
        monotonic_clock=lambda: 21,
        max_windows_per_cycle=4,
    )
    assert restarted.run_once().processed_window_count == 0
    assert (
        restarted.run_continuously(
            stop_requested=lambda: True,
            wait_for_next_cycle=lambda: None,
        )
        is None
    )


def test_monitoring_recovers_commit_before_cursor_and_rejects_tampered_cursor(
    tmp_path, monkeypatch
) -> None:
    service, database, _first, second = _promoted_pair(tmp_path)
    receipt_id = "receipt:monitor-window"
    _append_settlement(database, receipt_id=receipt_id)
    policy = MonitoringPolicy.create(gates=(EvaluationGate("normalized_crps", "lte", 0.25),))
    metric_evaluator = _MetricEvaluator()
    source = SQLiteSettledEvidenceSource(
        database,
        receipt_authority=_ReceiptAuthority({receipt_id: second.bundle_digest}),
        policy=policy,
        metric_evaluator=metric_evaluator,
    )
    signer = P256EphemeralSigner.generate("integrity-key:monitor-cursor-crash")
    cursor_path = tmp_path / "crash-cursor.json"
    cursor = DurableMonitoringCursorStore(
        cursor_path,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    runner = ContinuousFactoryMonitoringRunner(
        service=service,
        source=source,
        cursor_store=cursor,
        actor_id=ACTOR,
        clock=lambda: NOW,
        monotonic_clock=lambda: 30,
        max_windows_per_cycle=1,
    )
    original_persist = cursor.persist
    persist_count = 0

    def crash_after_monitoring_commit(state):
        nonlocal persist_count
        persist_count += 1
        if persist_count == 2:
            raise OSError("crash")
        original_persist(state)

    monkeypatch.setattr(cursor, "persist", crash_after_monitoring_commit)
    with pytest.raises(OSError, match="crash"):
        runner.run_once()

    pending = DurableMonitoringCursorStore(
        cursor_path,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    ).load()
    assert pending.pending_observation_digest is not None
    assert service.active_bundle_digest() != second.bundle_digest

    recovered_cursor = DurableMonitoringCursorStore(
        cursor_path,
        signer=signer,
        trust_store=IntegrityTrustStore((signer.identity,)),
    )
    recovered_runner = ContinuousFactoryMonitoringRunner(
        service=service,
        source=source,
        cursor_store=recovered_cursor,
        actor_id=ACTOR,
        clock=lambda: NOW,
        monotonic_clock=lambda: 31,
        max_windows_per_cycle=1,
    )
    metric_evaluator.value = 0.10
    with pytest.raises(FactoryError, match="pending monitoring observation differs"):
        recovered_runner.run_once()

    metric_evaluator.value = 0.40
    recovered = recovered_runner.run_once()
    assert recovered.processed_window_count == 1
    assert recovered.receipts[0].rolled_back is True

    original_persist(recovered.cursor)
    raw = bytearray(cursor_path.read_bytes())
    raw[len(raw) // 2] ^= 1
    cursor_path.write_bytes(bytes(raw))
    with pytest.raises(FactoryError, match="cursor"):
        recovered_cursor.load()


def test_monitoring_source_rejects_network_unspecified_metrics_and_binds_runtime_bundle(
    tmp_path,
) -> None:
    policy = MonitoringPolicy.create(gates=(EvaluationGate("normalized_crps", "lte", 0.25),))
    with pytest.raises(FactoryError, match="configured local boundary"):
        SQLiteSettledEvidenceSource(
            tmp_path / "authority.sqlite3",
            receipt_authority=_ReceiptAuthority({}),
            policy=policy,
            metric_evaluator=lambda facts: {"normalized_crps": 0.1},
        )

    authority = SQLiteReceiptBundleAuthority.__new__(SQLiteReceiptBundleAuthority)
    authority._projections = _VerifiedReceiptProjection(
        (BundleIdentity("runtime", "bundle:v3", "a" * 64),)
    )
    assert authority.bundle_digest("receipt:monitor-window") == "a" * 64

    authority._projections = _VerifiedReceiptProjection(
        (BundleIdentity("support", "bundle:v3", "b" * 64),)
    )
    with pytest.raises(FactoryError, match="runtime bundle authority"):
        authority.bundle_digest("receipt:monitor-window")
