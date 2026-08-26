from __future__ import annotations

import json
from pathlib import Path

import pytest

from strathmark.v3.application.factory_automation import FactoryAutomationSpec, FactoryFamily
from strathmark.v3.application.factory_monitoring import (
    DurableMonitoringCursorStore,
    MonitoringExecutionBoundary,
    MonitoringPolicy,
    SQLiteSettledEvidenceSource,
)
from strathmark.v3.application.factory_runtime import (
    FactoryRuntimeConfig,
    FactoryRuntimeError,
    compose_local_factory_runtime,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.factory.evaluator import EvaluationGate
from strathmark.v3.infrastructure.integrity import IntegrityTrustStore, P256EphemeralSigner
from tests.v3.system.test_continuous_factory_monitoring import _ReceiptAuthority
from tests.v3.system.test_factory_automation import _FamilyExecutor, _spec
from tests.v3.system.test_promotion_rollback import NOW, _report, _service


class _MetricEvaluator:
    execution_boundary = MonitoringExecutionBoundary.CONFIGURED_LOCAL_ONLY

    def evaluate(self, facts):
        assert facts
        return {"normalized_crps": 0.1}


def _runtime(tmp_path: Path):
    service, _repository, bundle_signer, evaluator_signer, database = _service(tmp_path)
    cursor_signer = P256EphemeralSigner.generate("integrity-key:runtime-cursor")
    source = SQLiteSettledEvidenceSource(
        database,
        receipt_authority=_ReceiptAuthority({}),
        policy=MonitoringPolicy.create(gates=(EvaluationGate("normalized_crps", "lte", 0.25),)),
        metric_evaluator=_MetricEvaluator(),
    )
    reports = {}

    def evaluate(candidate):
        if candidate.candidate_digest not in reports:
            reports[candidate.candidate_digest] = _report(
                tmp_path / "runtime-evaluator",
                candidate,
                evaluator_signer,
                generation="audit-runtime-boundary",
            )
        return reports[candidate.candidate_digest]

    return compose_local_factory_runtime(
        FactoryRuntimeConfig(
            actor_id=StableIdentifier("actor:factory-runtime"),
            allowed_local_models=("ollama:qwen3.5-9b@sha256:" + "a" * 64,),
            allowed_cloud_models=(),
            max_monitoring_windows=4,
        ),
        service=service,
        executors=tuple(_FamilyExecutor(family) for family in FactoryFamily),
        evaluator=evaluate,
        bundle_signer=bundle_signer,
        monitoring_source=source,
        cursor_store=DurableMonitoringCursorStore(
            tmp_path / "runtime-cursor.json",
            signer=cursor_signer,
            trust_store=IntegrityTrustStore((cursor_signer.identity,)),
        ),
        clock=lambda: NOW,
        monotonic_clock=lambda: 10,
    )


def test_local_runtime_composes_both_runners_and_scheduler_stops_cleanly(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    idle = runtime.run_once()
    assert idle.automation is None
    assert idle.monitoring.processed_window_count == 0

    waits = []
    runtime.run_continuously(
        next_spec=lambda: None,
        stop_requested=lambda: bool(waits),
        wait_for_next_cycle=lambda: waits.append("waited"),
    )
    assert waits == ["waited"]


def test_runtime_rejects_missing_or_unconfigured_algorithmic_components(tmp_path) -> None:
    service, _repository, bundle_signer, evaluator_signer, database = _service(tmp_path)
    signer = P256EphemeralSigner.generate("integrity-key:missing-runtime-cursor")
    source = SQLiteSettledEvidenceSource(
        database,
        receipt_authority=_ReceiptAuthority({}),
        policy=MonitoringPolicy.create(gates=(EvaluationGate("normalized_crps", "lte", 0.25),)),
        metric_evaluator=_MetricEvaluator(),
    )
    arguments = dict(
        config=FactoryRuntimeConfig(
            actor_id=StableIdentifier("actor:factory-runtime"),
            allowed_local_models=(),
            allowed_cloud_models=(),
            max_monitoring_windows=1,
        ),
        service=service,
        evaluator=lambda candidate: _report(
            tmp_path / "missing-evaluator",
            candidate,
            evaluator_signer,
            generation="audit-missing-runtime",
        ),
        bundle_signer=bundle_signer,
        monitoring_source=source,
        cursor_store=DurableMonitoringCursorStore(
            tmp_path / "missing-cursor.json",
            signer=signer,
            trust_store=IntegrityTrustStore((signer.identity,)),
        ),
        clock=lambda: NOW,
        monotonic_clock=lambda: 1,
    )
    with pytest.raises(FactoryRuntimeError, match="every configured family"):
        compose_local_factory_runtime(executors=(), **arguments)

    remote = tuple(_FamilyExecutor(family) for family in FactoryFamily)
    remote[0].execution_boundary = "download_or_shell"
    with pytest.raises(FactoryRuntimeError, match="local configured boundary"):
        compose_local_factory_runtime(executors=remote, **arguments)


def test_factory_runtime_is_not_exposed_by_consumer_api_or_v2_authority() -> None:
    contract = json.loads(
        Path("strathmark/v3/contracts/v3_consumer.openapi.json").read_text(encoding="utf-8")
    )
    assert all("factory" not in path for path in contract["paths"])

    import strathmark.prediction_v2 as v2

    assert not hasattr(v2, "FactoryRuntimeConfig")
    assert not hasattr(v2, "compose_local_factory_runtime")


def test_runtime_automation_exact_retry_remains_factory_service_authority(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    specification: FactoryAutomationSpec = _spec()

    first = runtime.run_once(specification, request_identity="factory-runtime:cycle-one")
    retry = runtime.run_once(specification, request_identity="factory-runtime:cycle-one")

    assert first.automation is not None
    assert retry.automation is not None
    assert retry.automation.factory == first.automation.factory
