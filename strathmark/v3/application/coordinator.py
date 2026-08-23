"""Provider-independent execution over the fenced durable-job boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from strathmark.v3.application.capacity import JobLane
from strathmark.v3.application.job_ports import (
    DurableJobError,
    FailureKind,
    JobRecordPort,
    JobRepositoryPort,
    PublicationPort,
    QueueHealthPort,
    ReadinessProbePort,
    RetryPolicy,
)


class ProviderPort(Protocol):
    """A local or cloud adapter; scheduling does not depend on provider identity."""

    def execute(self, job: JobRecordPort) -> ProviderResponse: ...


class ContextDigestPort(Protocol):
    def __call__(self, job: JobRecordPort) -> tuple[str, str]: ...


class ClockPort(Protocol):
    def __call__(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    result_digest: str
    evidence_digest: str
    bundle_digest: str
    value: Any

    def __post_init__(self) -> None:
        for label in ("result_digest", "evidence_digest", "bundle_digest"):
            value = getattr(self, label)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise DurableJobError(f"{label} must be a lower-case SHA-256 digest")


class ProviderFailure(RuntimeError):
    """Typed provider outcome; free-form exception text is never persisted."""

    def __init__(self, kind: FailureKind, reason: str) -> None:
        if not isinstance(kind, FailureKind):
            raise DurableJobError("provider failure kind must be typed")
        if (
            not isinstance(reason, str)
            or not reason
            or len(reason) > 128
            or not reason[0].isalpha()
            or any(
                not (character.islower() or character.isdigit() or character in "_.-")
                for character in reason
            )
        ):
            raise DurableJobError("provider failure reason must be a bounded machine token")
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RunOutcome:
    claimed: bool
    job: JobRecordPort | None

    def __post_init__(self) -> None:
        if not isinstance(self.claimed, bool):
            raise DurableJobError("run outcome claimed flag must be explicit")
        if self.claimed != (self.job is not None):
            raise DurableJobError("run outcome must bind claimed state to a job")


class DurableCoordinator:
    """Claims durably, calls one provider, then publishes through the current fence."""

    def __init__(self, repository: JobRepositoryPort, *, retry_policy: RetryPolicy) -> None:
        required = ("claim", "record_failure", "mark_stale", "commit_success", "health")
        if any(not callable(getattr(repository, name, None)) for name in required):
            raise DurableJobError("coordinator requires a durable job repository port")
        if not isinstance(retry_policy, RetryPolicy):
            raise DurableJobError("coordinator requires a RetryPolicy")
        self._repository = repository
        self._retry_policy = retry_policy

    def run_one(
        self,
        lane: JobLane,
        *,
        worker_id: str,
        lease_duration_ms: int,
        provider: ProviderPort,
        current_context: ContextDigestPort,
        publish: PublicationPort,
        clock: ClockPort,
    ) -> RunOutcome:
        if not isinstance(lane, JobLane):
            raise DurableJobError("coordinator lane must be typed")
        for callback, label in (
            (getattr(provider, "execute", None), "provider"),
            (current_context, "current context"),
            (publish, "publication"),
            (clock, "clock"),
        ):
            if not callable(callback):
                raise DurableJobError(f"{label} port must be callable")

        lease = self._repository.claim(
            lane,
            worker_id=worker_id,
            clock=clock,
            lease_duration_ms=lease_duration_ms,
        )
        if lease is None:
            return RunOutcome(False, None)

        try:
            response = provider.execute(lease)
            if not isinstance(response, ProviderResponse):
                raise ProviderFailure(FailureKind.VALIDATION, "invalid_provider_response")
        except ProviderFailure as exc:
            failed = self._repository.record_failure(
                lease.job_id,
                lease.job_revision,
                worker_id=worker_id,
                fencing_token=lease.fencing_token,
                observed_at=clock(),
                failure_kind=exc.kind,
                reason=exc.reason,
                policy=self._retry_policy,
            )
            return RunOutcome(True, failed)
        except Exception:
            failed = self._repository.record_failure(
                lease.job_id,
                lease.job_revision,
                worker_id=worker_id,
                fencing_token=lease.fencing_token,
                observed_at=clock(),
                failure_kind=FailureKind.PROCESS,
                reason="provider_process_failure",
                policy=self._retry_policy,
            )
            return RunOutcome(True, failed)

        if (
            response.evidence_digest != lease.evidence_digest
            or response.bundle_digest != lease.bundle_digest
        ):
            stale = self._repository.mark_stale(
                lease.job_id,
                lease.job_revision,
                worker_id=worker_id,
                fencing_token=lease.fencing_token,
                observed_at=clock(),
                reason="provider_context_mismatch",
            )
            return RunOutcome(True, stale)
        result = self._repository.commit_success(
            lease.job_id,
            lease.job_revision,
            worker_id=worker_id,
            fencing_token=lease.fencing_token,
            result_digest=response.result_digest,
            current_context=lambda _transaction, current: current_context(current),
            clock=clock,
            publish=lambda _transaction, current: publish(current, response),
        )
        return RunOutcome(True, result)

    def health(
        self,
        *,
        observed_at: str,
        dependency_probe: ReadinessProbePort,
        deadline_risk_window_ms: int = 120_000,
    ) -> QueueHealthPort:
        """Return a fresh immutable snapshot; no mutable module-global health exists."""

        return self._repository.health(
            observed_at=observed_at,
            dependency_probe=dependency_probe,
            deadline_risk_window_ms=deadline_risk_window_ms,
        )


__all__ = [
    "ClockPort",
    "ContextDigestPort",
    "DurableCoordinator",
    "JobRepositoryPort",
    "ProviderFailure",
    "ProviderPort",
    "ProviderResponse",
    "PublicationPort",
    "RunOutcome",
]
