"""Fail-closed local composition and scheduler for the V3 model factory.

Concrete family algorithms and metric evaluators are explicit composition inputs. This
module never imports executables by name, downloads artifacts, or changes V2/API authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from strathmark.v3.application.factory import FactoryService
from strathmark.v3.application.factory_automation import (
    FactoryAutomationOutcome,
    FactoryAutomationRunner,
    FactoryAutomationSpec,
    FactoryExecutionBoundary,
    FactoryFamily,
    FactoryFamilyExecutor,
)
from strathmark.v3.application.factory_monitoring import (
    ContinuousFactoryMonitoringRunner,
    DurableMonitoringCursorStore,
    MonitoringCycleOutcome,
    SQLiteSettledEvidenceSource,
)
from strathmark.v3.contracts.identifiers import StableIdentifier, require_identifier
from strathmark.v3.factory.candidates import CandidateBuilder, CandidateBundle
from strathmark.v3.factory.evaluator import SignedEvaluationReport
from strathmark.v3.infrastructure.integrity import P256Signer


class FactoryRuntimeError(RuntimeError):
    """Local factory composition or scheduling is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class FactoryRuntimeConfig:
    actor_id: StableIdentifier
    allowed_local_models: tuple[str, ...]
    allowed_cloud_models: tuple[str, ...]
    max_monitoring_windows: int

    def __post_init__(self) -> None:
        require_identifier(self.actor_id, expected_namespace="actor")
        for values, label in (
            (self.allowed_local_models, "local models"),
            (self.allowed_cloud_models, "cloud models"),
        ):
            if not isinstance(values, tuple) or values != tuple(sorted(set(values))):
                raise FactoryRuntimeError(f"configured {label} must be immutable and unique")
            if any(not isinstance(item, str) or not item or len(item) > 512 for item in values):
                raise FactoryRuntimeError(f"configured {label} contain an invalid identity")
        if (
            isinstance(self.max_monitoring_windows, bool)
            or not isinstance(self.max_monitoring_windows, int)
            or not 1 <= self.max_monitoring_windows <= 32
        ):
            raise FactoryRuntimeError("monitoring window bound must be 1..32")


@dataclass(frozen=True, slots=True)
class FactoryRuntimeCycle:
    automation: FactoryAutomationOutcome | None
    monitoring: MonitoringCycleOutcome


class LocalFactoryRuntime:
    """Run configured factory work and settled-evidence monitoring outside the API."""

    def __init__(
        self,
        *,
        config: FactoryRuntimeConfig,
        automation: FactoryAutomationRunner,
        monitoring: ContinuousFactoryMonitoringRunner,
        clock: Callable[[], str],
        monotonic_clock: Callable[[], int],
    ) -> None:
        if not isinstance(config, FactoryRuntimeConfig):
            raise FactoryRuntimeError("factory runtime requires typed configuration")
        if not isinstance(automation, FactoryAutomationRunner) or not isinstance(
            monitoring, ContinuousFactoryMonitoringRunner
        ):
            raise FactoryRuntimeError("factory runtime requires both configured runners")
        self.config = config
        self.automation = automation
        self.monitoring = monitoring
        self._clock = clock
        self._monotonic_clock = monotonic_clock

    def run_once(
        self,
        spec: FactoryAutomationSpec | None = None,
        *,
        request_identity: str | None = None,
    ) -> FactoryRuntimeCycle:
        if (spec is None) != (request_identity is None):
            raise FactoryRuntimeError(
                "automation spec and request identity must be supplied together"
            )
        automated = None
        if spec is not None:
            if (
                not isinstance(request_identity, str)
                or not request_identity
                or len(request_identity) > 128
            ):
                raise FactoryRuntimeError("factory request identity must be bounded")
            automated = self.automation.run(
                spec,
                request_identity=request_identity,
                actor_id=self.config.actor_id,
                occurred_at_utc=self._clock(),
                monotonic_elapsed_ms=self._monotonic_clock(),
            )
        return FactoryRuntimeCycle(automated, self.monitoring.run_once())

    def run_continuously(
        self,
        *,
        next_spec: Callable[[], tuple[FactoryAutomationSpec, str] | None],
        stop_requested: Callable[[], bool],
        wait_for_next_cycle: Callable[[], None],
    ) -> None:
        if not all(callable(item) for item in (next_spec, stop_requested, wait_for_next_cycle)):
            raise FactoryRuntimeError("factory scheduler controls must be callable")
        while not stop_requested():
            pending = next_spec()
            if pending is None:
                self.run_once()
            elif (
                not isinstance(pending, tuple)
                or len(pending) != 2
                or not isinstance(pending[0], FactoryAutomationSpec)
                or not isinstance(pending[1], str)
            ):
                raise FactoryRuntimeError("scheduled factory work is malformed")
            else:
                self.run_once(pending[0], request_identity=pending[1])
            wait_for_next_cycle()


def compose_local_factory_runtime(
    config: FactoryRuntimeConfig,
    *,
    service: FactoryService,
    executors: tuple[FactoryFamilyExecutor, ...],
    evaluator: Callable[[CandidateBundle], SignedEvaluationReport],
    bundle_signer: P256Signer,
    monitoring_source: SQLiteSettledEvidenceSource,
    cursor_store: DurableMonitoringCursorStore,
    clock: Callable[[], str],
    monotonic_clock: Callable[[], int],
) -> LocalFactoryRuntime:
    if not isinstance(config, FactoryRuntimeConfig) or not isinstance(executors, tuple):
        raise FactoryRuntimeError("factory runtime configuration is invalid")
    if tuple(getattr(item, "family", None) for item in executors) != tuple(FactoryFamily):
        raise FactoryRuntimeError("factory runtime requires every configured family")
    if any(
        getattr(item, "execution_boundary", None)
        is not FactoryExecutionBoundary.LOCAL_CONFIGURED_ONLY
        for item in executors
    ):
        raise FactoryRuntimeError("factory executors must use the local configured boundary")
    try:
        automation = FactoryAutomationRunner(
            service=service,
            candidate_builder=CandidateBuilder(
                allowed_local_models=config.allowed_local_models,
                allowed_cloud_models=config.allowed_cloud_models,
            ),
            executors=executors,
            evaluator=evaluator,
            bundle_signer=bundle_signer,
        )
        monitoring = ContinuousFactoryMonitoringRunner(
            service=service,
            source=monitoring_source,
            cursor_store=cursor_store,
            actor_id=config.actor_id,
            clock=clock,
            monotonic_clock=monotonic_clock,
            max_windows_per_cycle=config.max_monitoring_windows,
        )
    except Exception as exc:
        raise FactoryRuntimeError("factory runtime composition failed closed") from exc
    return LocalFactoryRuntime(
        config=config,
        automation=automation,
        monitoring=monitoring,
        clock=clock,
        monotonic_clock=monotonic_clock,
    )


__all__ = [
    "FactoryRuntimeConfig",
    "FactoryRuntimeCycle",
    "FactoryRuntimeError",
    "LocalFactoryRuntime",
    "compose_local_factory_runtime",
]
