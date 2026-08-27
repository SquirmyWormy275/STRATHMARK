"""Provider-independent execution over the fenced durable-job boundary."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from strathmark.v3.application.capacity import (
    CapacityUse,
    JobKind,
    JobLane,
    JobPriority,
)
from strathmark.v3.application.job_ports import (
    DurableJobError,
    FailureKind,
    JobRecordPort,
    JobRepositoryPort,
    ProviderExecutionAudit,
    PublicationPort,
    QueueHealthPort,
    ReadinessProbePort,
    RetryPolicy,
    RollingJobRepositoryPort,
)
from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.events import EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import (
    EvidencePacket,
    ResultObservation,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    require_identifier,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

if TYPE_CHECKING:
    from strathmark.v3.application.field_assembly import (
        FrozenFieldRevision,
        OperationalWeightAuthority,
    )


class ProviderPort(Protocol):
    """A local or cloud adapter; scheduling does not depend on provider identity."""

    def execute(self, job: JobRecordPort) -> ProviderResponse: ...


class ContextDigestPort(Protocol):
    def __call__(self, job: JobRecordPort) -> tuple[str, str]: ...


class ClockPort(Protocol):
    def __call__(self) -> str: ...


class FieldAuthorityVerifierPort(Protocol):
    def verify_current_field(self, field: FrozenFieldRevision) -> None: ...

    def verify_weight_authority(self, authority: OperationalWeightAuthority) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    result_digest: str
    evidence_digest: str
    bundle_digest: str
    value: Any
    provider_audit: ProviderExecutionAudit | None = None

    def __post_init__(self) -> None:
        for label in ("result_digest", "evidence_digest", "bundle_digest"):
            value = getattr(self, label)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise DurableJobError(f"{label} must be a lower-case SHA-256 digest")
        if self.provider_audit is not None and not isinstance(
            self.provider_audit, ProviderExecutionAudit
        ):
            raise DurableJobError("provider response audit must be typed")


class ProviderFailure(RuntimeError):
    """Typed provider outcome; free-form exception text is never persisted."""

    def __init__(
        self,
        kind: FailureKind,
        reason: str,
        provider_audit: ProviderExecutionAudit | None = None,
    ) -> None:
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
        if provider_audit is not None and not isinstance(provider_audit, ProviderExecutionAudit):
            raise DurableJobError("provider failure audit must be typed")
        self.provider_audit = provider_audit


@dataclass(frozen=True, slots=True)
class RunOutcome:
    claimed: bool
    job: JobRecordPort | None
    provider_failure: ProviderFailure | None = None
    provider_response: ProviderResponse | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claimed, bool):
            raise DurableJobError("run outcome claimed flag must be explicit")
        if self.claimed != (self.job is not None):
            raise DurableJobError("run outcome must bind claimed state to a job")
        if self.provider_failure is not None and not isinstance(
            self.provider_failure, ProviderFailure
        ):
            raise DurableJobError("run outcome provider failure must be typed")
        if self.provider_response is not None and not isinstance(
            self.provider_response, ProviderResponse
        ):
            raise DurableJobError("run outcome provider response must be typed")
        if self.provider_failure is not None and self.provider_response is not None:
            raise DurableJobError("run outcome cannot be both successful and failed")


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

        return self.run_claimed(
            lease,
            provider=provider,
            current_context=current_context,
            publish=publish,
            clock=clock,
        )

    def run_claimed(
        self,
        lease: JobRecordPort,
        *,
        provider: ProviderPort,
        current_context: ContextDigestPort,
        publish: PublicationPort,
        clock: ClockPort,
    ) -> RunOutcome:
        """Execute and atomically settle a lease already claimed by this repository."""

        if not isinstance(getattr(lease, "job_id", None), str):
            raise DurableJobError("claimed execution requires a typed persisted lease")
        worker_id = getattr(lease, "lease_owner", None)
        if not isinstance(worker_id, str) or not worker_id:
            raise DurableJobError("claimed execution requires a persisted lease owner")
        for callback, label in (
            (getattr(provider, "execute", None), "provider"),
            (current_context, "current context"),
            (publish, "publication"),
            (clock, "clock"),
        ):
            if not callable(callback):
                raise DurableJobError(f"{label} port must be callable")

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
                provider_audit=exc.provider_audit,
            )
            return RunOutcome(True, failed, exc)
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
            provider_audit=response.provider_audit,
            current_context=lambda _transaction, current: current_context(current),
            clock=clock,
            publish=lambda _transaction, current: publish(current, response),
        )
        return RunOutcome(True, result, provider_response=response)

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


class PreparationClass(str, Enum):
    """Closed race-day priority order for prospective capability cards."""

    SCHEDULED = "scheduled"
    PLAUSIBLE_QUALIFIER = "plausible_qualifier"
    IMMINENT_FIELD = "imminent_field"

    @property
    def rank(self) -> int:
        return {
            PreparationClass.SCHEDULED: 1,
            PreparationClass.PLAUSIBLE_QUALIFIER: 2,
            PreparationClass.IMMINENT_FIELD: 3,
        }[self]


@dataclass(frozen=True, slots=True)
class CardKey:
    """Every causal dependency of one competitor/context capability card."""

    competitor_id: StableIdentifier
    target_context_digest: str
    historical_cutoff_key: StableIdentifier
    tournament_epoch_id: StableIdentifier
    bundle_digest: str
    evidence_digest: str
    dependency_revision: int
    card_digest: str
    idempotency_key: IdempotencyKey

    def __post_init__(self) -> None:
        require_identifier(self.competitor_id, expected_namespace="competitor")
        require_identifier(self.historical_cutoff_key, expected_namespace="history")
        require_identifier(self.tournament_epoch_id, expected_namespace="epoch")
        for value, label in (
            (self.target_context_digest, "target context"),
            (self.bundle_digest, "bundle"),
            (self.evidence_digest, "evidence"),
            (self.card_digest, "card"),
        ):
            _require_digest(value, label)
        if (
            isinstance(self.dependency_revision, bool)
            or not isinstance(self.dependency_revision, int)
            or self.dependency_revision <= 0
        ):
            raise DurableJobError("card dependency revision must be positive")
        if self.card_digest != canonical_digest(self.content_value()):
            raise DurableJobError("card digest differs from its causal dependencies")
        if str(self.idempotency_key) != f"card:{self.card_digest}":
            raise DurableJobError("card idempotency key differs from its digest")

    @classmethod
    def create(
        cls,
        *,
        competitor_id: str | StableIdentifier,
        target_context_digest: str,
        historical_cutoff_key: str | StableIdentifier,
        tournament_epoch_id: str | StableIdentifier,
        bundle_digest: str,
        evidence_digest: str,
        dependency_revision: int,
    ) -> CardKey:
        values = {
            "competitor_id": require_identifier(competitor_id, expected_namespace="competitor"),
            "target_context_digest": target_context_digest,
            "historical_cutoff_key": require_identifier(
                historical_cutoff_key, expected_namespace="history"
            ),
            "tournament_epoch_id": require_identifier(
                tournament_epoch_id, expected_namespace="epoch"
            ),
            "bundle_digest": bundle_digest,
            "evidence_digest": evidence_digest,
            "dependency_revision": dependency_revision,
        }
        content = _card_content(values)
        digest = canonical_digest(content)
        return cls(
            **values,
            card_digest=digest,
            idempotency_key=IdempotencyKey(f"card:{digest}"),
        )

    def content_value(self) -> dict[str, object]:
        return _card_content(
            {
                "competitor_id": self.competitor_id,
                "target_context_digest": self.target_context_digest,
                "historical_cutoff_key": self.historical_cutoff_key,
                "tournament_epoch_id": self.tournament_epoch_id,
                "bundle_digest": self.bundle_digest,
                "evidence_digest": self.evidence_digest,
                "dependency_revision": self.dependency_revision,
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **self.content_value(),
            "card_digest": self.card_digest,
            "idempotency_key": str(self.idempotency_key),
        }


@dataclass(frozen=True, slots=True)
class PreparationCandidate:
    key: CardKey
    preparation_class: PreparationClass
    hard_deadline_at: str
    evidence_packet: Any | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, CardKey) or not isinstance(
            self.preparation_class, PreparationClass
        ):
            raise DurableJobError("preparation candidate requires typed key and class")
        require_utc_milliseconds(self.hard_deadline_at)
        if self.evidence_packet is not None:
            from strathmark.v3.contracts.evidence import EvidencePacket

            packet = self.evidence_packet
            if (
                not isinstance(packet, EvidencePacket)
                or str(packet.competitor_id) != str(self.key.competitor_id)
                or packet.target_context.digest != self.key.target_context_digest
                or str(packet.historical_cutoff_key) != str(self.key.historical_cutoff_key)
                or str(packet.tournament_epoch_id) != str(self.key.tournament_epoch_id)
                or packet.content_digest != self.key.evidence_digest
            ):
                raise DurableJobError("preparation evidence packet differs from card key")

    @classmethod
    def create(cls, **values: object) -> PreparationCandidate:
        preparation_class = values.pop("preparation_class")
        hard_deadline_at = values.pop("hard_deadline_at")
        evidence_packet = values.pop("evidence_packet", None)
        return cls(
            CardKey.create(**values),  # type: ignore[arg-type]
            preparation_class,  # type: ignore[arg-type]
            hard_deadline_at,  # type: ignore[arg-type]
            evidence_packet,
        )


@dataclass(frozen=True, slots=True)
class CompletedCard:
    key: CardKey
    result_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, CardKey):
            raise DurableJobError("completed card key must be typed")
        _require_digest(self.result_digest, "card result")


@dataclass(frozen=True, slots=True)
class PreparationPlan:
    pending: tuple[PreparationCandidate, ...]
    cached: tuple[CompletedCard, ...]
    invalidated: tuple[CardKey, ...]


class RollingPreparationPlanner:
    """Deterministic in-process view over durable card identities.

    Durable inference and recovery remain owned by ``DurableJobRepository``.  This
    planner decides which exact card identities need work and can be reconstructed
    from its canonical snapshot after process restart.
    """

    def __init__(self, completed: tuple[CompletedCard, ...] = ()) -> None:
        if not isinstance(completed, tuple) or not all(
            isinstance(item, CompletedCard) for item in completed
        ):
            raise DurableJobError("planner snapshot must contain typed completed cards")
        self._completed = {item.key.card_digest: item for item in completed}
        self._active_by_subject: dict[tuple[str, str, str, str], CardKey] = {
            _card_subject(item.key): item.key for item in completed
        }

    def plan(self, candidates: tuple[PreparationCandidate, ...]) -> PreparationPlan:
        if not isinstance(candidates, tuple) or not all(
            isinstance(item, PreparationCandidate) for item in candidates
        ):
            raise DurableJobError("preparation plan requires immutable typed candidates")
        exact: dict[str, PreparationCandidate] = {}
        for item in candidates:
            prior = exact.get(item.key.card_digest)
            if prior is None:
                exact[item.key.card_digest] = item
                continue
            exact[item.key.card_digest] = PreparationCandidate(
                item.key,
                max(
                    (prior.preparation_class, item.preparation_class),
                    key=lambda value: value.rank,
                ),
                min(prior.hard_deadline_at, item.hard_deadline_at),
                item.evidence_packet or prior.evidence_packet,
            )
        by_subject: dict[tuple[str, str, str, str], list[PreparationCandidate]] = {}
        for item in exact.values():
            by_subject.setdefault(_card_subject(item.key), []).append(item)
        unique: dict[str, PreparationCandidate] = {}
        for subject, rows in by_subject.items():
            maximum = max(item.key.dependency_revision for item in rows)
            current = tuple(item for item in rows if item.key.dependency_revision == maximum)
            if len({item.key.card_digest for item in current}) != 1:
                raise DurableJobError(
                    "one logical card revision contains conflicting causal material"
                )
            selected = current[0]
            active = self._active_by_subject.get(subject)
            if active is not None and (
                active.tournament_epoch_id == selected.key.tournament_epoch_id
            ):
                if maximum < active.dependency_revision:
                    continue
                if (
                    maximum == active.dependency_revision
                    and selected.key.card_digest != active.card_digest
                ):
                    raise DurableJobError("card revision conflicts with current durable authority")
            unique[selected.key.card_digest] = selected
        invalidated: dict[str, CardKey] = {}
        for candidate in unique.values():
            subject = _card_subject(candidate.key)
            prior = self._active_by_subject.get(subject)
            if prior is not None and prior != candidate.key:
                invalidated[prior.card_digest] = prior
                self._completed.pop(prior.card_digest, None)
            self._active_by_subject[subject] = candidate.key
        cached = tuple(
            self._completed[item.key.card_digest]
            for item in unique.values()
            if item.key.card_digest in self._completed
        )
        pending = tuple(
            sorted(
                (item for item in unique.values() if item.key.card_digest not in self._completed),
                key=lambda item: (
                    -item.preparation_class.rank,
                    item.hard_deadline_at,
                    str(item.key.competitor_id),
                    item.key.target_context_digest,
                ),
            )
        )
        return PreparationPlan(
            pending,
            tuple(sorted(cached, key=lambda item: item.key.card_digest)),
            tuple(sorted(invalidated.values(), key=lambda item: item.card_digest)),
        )

    def _checkpoint(
        self,
    ) -> tuple[dict[str, CompletedCard], dict[tuple[str, str, str, str], CardKey]]:
        return dict(self._completed), dict(self._active_by_subject)

    def _restore(
        self,
        checkpoint: tuple[dict[str, CompletedCard], dict[tuple[str, str, str, str], CardKey]],
    ) -> None:
        self._completed, self._active_by_subject = checkpoint

    def record_completed_from_job(
        self,
        repository: JobRepositoryPort,
        key: CardKey,
        *,
        job_id: str,
        job_revision: int,
    ) -> CompletedCard:
        if not callable(getattr(repository, "verify", None)) or not callable(
            getattr(repository, "get", None)
        ):
            raise DurableJobError("completed cards require the durable job authority")
        repository.verify()
        record = repository.get(job_id, job_revision)
        payload = record.payload()
        subject = _card_subject(key)
        active = self._active_by_subject.get(subject)
        if active is not None and (
            key.dependency_revision < active.dependency_revision
            or (
                key.dependency_revision == active.dependency_revision
                and key.card_digest != active.card_digest
            )
        ):
            raise DurableJobError("superseded card publication cannot become current")
        if (
            getattr(record.state, "value", None) != "succeeded"
            or record.result_digest is None
            or payload.get("card_key") != key.to_dict()
            or record.evidence_digest != key.evidence_digest
            or record.bundle_digest != key.bundle_digest
            or record.fencing_token <= 0
        ):
            raise DurableJobError("job publication does not bind a current successful card")
        raise DurableJobError(
            "one job is not a durable whole-card publication; use the rolling coordinator"
        )

    def snapshot(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "key": item.key.to_dict(),
                "result_digest": item.result_digest,
            }
            for item in sorted(self._completed.values(), key=lambda row: row.key.card_digest)
        )

    @classmethod
    def from_snapshot(cls, value: tuple[dict[str, object], ...]) -> RollingPreparationPlanner:
        # Exported snapshots are diagnostic only. Operational cache authority is rebuilt
        # from signed/fenced durable job publications through record_completed_from_job.
        for item in value:
            key_value = item["key"]
            if not isinstance(key_value, dict):
                raise DurableJobError("planner key snapshot is invalid")
            key = CardKey.create(
                competitor_id=key_value["competitor_id"],
                target_context_digest=key_value["target_context_digest"],
                historical_cutoff_key=key_value["historical_cutoff_key"],
                tournament_epoch_id=key_value["tournament_epoch_id"],
                bundle_digest=key_value["bundle_digest"],
                evidence_digest=key_value["evidence_digest"],
                dependency_revision=key_value["dependency_revision"],
            )
            if key.card_digest != key_value.get("card_digest") or str(
                key.idempotency_key
            ) != key_value.get("idempotency_key"):
                raise DurableJobError("planner snapshot card authority differs")
            _require_digest(item["result_digest"], "diagnostic card result")
        return cls()


class RollingComponentOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class RollingComponentReceipt:
    component_id: str
    component_ordinal: int
    job_id: str
    job_revision: int
    job_kind: JobKind
    outcome: RollingComponentOutcome
    result_digest: str | None
    terminal_reason_code: str | None
    fencing_token: int
    payload_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.component_id, str)
            or not self.component_id
            or len(self.component_id) > 128
        ):
            raise DurableJobError("rolling component identity is invalid")
        require_identifier(self.job_id, expected_namespace="job")
        if (
            isinstance(self.component_ordinal, bool)
            or not isinstance(self.component_ordinal, int)
            or not 1 <= self.component_ordinal <= 5
            or isinstance(self.job_revision, bool)
            or not isinstance(self.job_revision, int)
            or self.job_revision <= 0
            or isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 0
        ):
            raise DurableJobError("rolling component revision or ordinal is invalid")
        if not isinstance(self.job_kind, JobKind) or not isinstance(
            self.outcome, RollingComponentOutcome
        ):
            raise DurableJobError("rolling component kind or outcome is invalid")
        _require_digest(self.payload_digest, "rolling component payload")
        if self.outcome is RollingComponentOutcome.SUCCEEDED:
            _require_digest(self.result_digest, "rolling component result")
            if self.fencing_token <= 0:
                raise DurableJobError("successful rolling component lacks a fence")
            if self.terminal_reason_code is not None:
                raise DurableJobError("successful rolling component carries a terminal reason")
        else:
            if self.result_digest is not None:
                raise DurableJobError("unsuccessful rolling component carries a result")
            if (
                not isinstance(self.terminal_reason_code, str)
                or not self.terminal_reason_code
                or len(self.terminal_reason_code) > 128
                or not self.terminal_reason_code[0].isalpha()
                or any(
                    not (character.islower() or character.isdigit() or character in "_.-")
                    for character in self.terminal_reason_code
                )
            ):
                raise DurableJobError(
                    "rolling component terminal reason must be a bounded machine token"
                )
            is_deadline = self.terminal_reason_code in {
                "deadline_exceeded",
                "deadline_sealed",
            }
            if (self.outcome is RollingComponentOutcome.TIMED_OUT) != is_deadline:
                raise DurableJobError(
                    "rolling component timeout outcome and terminal reason differ"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "component_ordinal": self.component_ordinal,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_kind": self.job_kind.value,
            "outcome": self.outcome.value,
            "result_digest": self.result_digest,
            "terminal_reason_code": self.terminal_reason_code,
            "fencing_token": self.fencing_token,
            "payload_digest": self.payload_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RollingComponentReceipt:
        if set(value) != {
            "component_id",
            "component_ordinal",
            "job_id",
            "job_revision",
            "job_kind",
            "outcome",
            "result_digest",
            "terminal_reason_code",
            "fencing_token",
            "payload_digest",
        }:
            raise DurableJobError("rolling component receipt fields differ")
        for name in ("component_ordinal", "job_revision", "fencing_token"):
            if isinstance(value[name], bool) or not isinstance(value[name], int):
                raise DurableJobError("rolling component numeric fields must be integers")
        try:
            return cls(
                str(value["component_id"]),
                value["component_ordinal"],
                str(value["job_id"]),
                value["job_revision"],
                JobKind(value["job_kind"]),
                RollingComponentOutcome(value["outcome"]),
                None if value["result_digest"] is None else str(value["result_digest"]),
                (
                    None
                    if value["terminal_reason_code"] is None
                    else str(value["terminal_reason_code"])
                ),
                value["fencing_token"],
                str(value["payload_digest"]),
            )
        except (TypeError, ValueError) as exc:
            raise DurableJobError("rolling component receipt is invalid") from exc


@dataclass(frozen=True, slots=True)
class RollingCardPublication:
    key: CardKey
    authority: Any
    components: tuple[RollingComponentReceipt, ...]
    availability: tuple[tuple[str, str], ...]
    council_manifest_digest: str
    council_aggregate_manifest: SignedManifest
    hard_deadline_at: str
    sealed_at: str
    manifest: SignedManifest
    publication_digest: str

    def __post_init__(self) -> None:
        from strathmark.v3.application.field_assembly import CompetitorCardAuthority

        if not isinstance(self.key, CardKey) or not isinstance(
            self.authority, CompetitorCardAuthority
        ):
            raise DurableJobError("rolling publication requires typed card authority")
        if (
            not isinstance(self.components, tuple)
            or len(self.components) != 5
            or not all(isinstance(item, RollingComponentReceipt) for item in self.components)
            or tuple(item.component_ordinal for item in self.components) != (1, 2, 3, 4, 5)
        ):
            raise DurableJobError("rolling publication requires five ordered components")
        if not isinstance(self.availability, tuple) or tuple(
            item[0] for item in self.availability
        ) != ("formula", "ml", "llm_council"):
            raise DurableJobError("rolling publication availability differs")
        _require_digest(self.council_manifest_digest, "rolling council manifest")
        if (
            not isinstance(self.council_aggregate_manifest, SignedManifest)
            or self.council_aggregate_manifest.kind != "rolling_council_aggregate_authority"
        ):
            raise DurableJobError("rolling publication lacks council aggregate authority")
        # Deadline sealing is deliberately allowed after the deadline: terminal
        # component receipts preserve the timeout reasons and the immutable
        # sealed_at records the actual recovery instant.
        require_utc_milliseconds(self.hard_deadline_at)
        require_utc_milliseconds(self.sealed_at)
        if not isinstance(self.manifest, SignedManifest) or self.manifest.kind != (
            "rolling_card_publication"
        ):
            raise DurableJobError("rolling card publication lacks signed authority")
        _require_digest(self.publication_digest, "rolling publication")
        if self.publication_digest != canonical_digest(self.content_value()):
            raise DurableJobError("rolling publication digest differs")

    def content_value(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-rolling-card-publication-v1",
            "card_key": self.key.to_dict(),
            "card_authority_digest": canonical_digest(self.authority.to_dict()),
            "component_refs_digest": canonical_digest([item.to_dict() for item in self.components]),
            "availability": [list(item) for item in self.availability],
            "council_manifest_digest": self.council_manifest_digest,
            "council_aggregate_manifest_digest": self.council_aggregate_manifest.body_digest,
            "hard_deadline_at": self.hard_deadline_at,
            "sealed_at": self.sealed_at,
        }


@dataclass(frozen=True, slots=True)
class RollingReadiness:
    observed_at: str
    total_cards: int
    ready_count: int
    pending_count: int
    failed_count: int
    earliest_deadline_at: str | None
    all_ready: bool

    def __post_init__(self) -> None:
        require_utc_milliseconds(self.observed_at)
        for value in (
            self.total_cards,
            self.ready_count,
            self.pending_count,
            self.failed_count,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DurableJobError("rolling readiness counts must be non-negative")
        if self.ready_count + self.pending_count + self.failed_count != self.total_cards:
            raise DurableJobError("rolling readiness counts do not reconcile")
        if self.earliest_deadline_at is not None:
            require_utc_milliseconds(self.earliest_deadline_at)
        if self.all_ready != (self.total_cards > 0 and self.ready_count == self.total_cards):
            raise DurableJobError("rolling readiness flag differs from counts")


@dataclass(frozen=True, slots=True)
class RollingLifecycleReactionPlan:
    """Bounded work derived from one immutable U5 command event set."""

    candidates: tuple[PreparationCandidate, ...]
    capacity_use: CapacityUse
    council_manifest_digest: str
    closed_epoch_ids: tuple[StableIdentifier, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(item, PreparationCandidate) for item in self.candidates
        ):
            raise DurableJobError("rolling lifecycle candidates must be typed")
        if not isinstance(self.capacity_use, CapacityUse):
            raise DurableJobError("rolling lifecycle capacity must be typed")
        if len(self.candidates) > self.capacity_use.context_cards:
            raise DurableJobError("rolling lifecycle candidates exceed declared capacity")
        _require_digest(self.council_manifest_digest, "rolling council manifest")
        if not isinstance(self.closed_epoch_ids, tuple):
            raise DurableJobError("rolling lifecycle closed epochs must be immutable")
        closed = tuple(
            require_identifier(item, expected_namespace="epoch") for item in self.closed_epoch_ids
        )
        if len(closed) != len(set(closed)):
            raise DurableJobError("rolling lifecycle closed epochs cannot repeat")

    def content_value(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-rolling-lifecycle-plan-v1",
            "candidates": [
                {
                    "card_key": item.key.to_dict(),
                    "preparation_class": item.preparation_class.value,
                    "hard_deadline_at": item.hard_deadline_at,
                    "evidence_packet_digest": (
                        None
                        if item.evidence_packet is None
                        else item.evidence_packet.content_digest
                    ),
                }
                for item in self.candidates
            ],
            "capacity_use": self.capacity_use.to_dict(),
            "council_manifest_digest": self.council_manifest_digest,
            "closed_epoch_ids": [str(item) for item in self.closed_epoch_ids],
        }


class RollingLifecycleResolverPort(Protocol):
    def resolve(self, events: tuple[EventEnvelope, ...]) -> RollingLifecycleReactionPlan: ...


class RollingEventAuthorityPort(Protocol):
    def event_at(self, global_sequence: int) -> EventEnvelope: ...


class LifecycleCommandResultPort(Protocol):
    first_global_sequence: int
    last_global_sequence: int
    event_ids: tuple[str, ...]


class DurableRollingPreparationCoordinator:
    """Durable five-component card orchestration and restart-safe publication."""

    _COUNCIL_KIND = "rolling_council_roster_authority"
    _ACTIVE_STATES = {"queued", "leased", "retryable-failed"}

    def __init__(
        self,
        repository: RollingJobRepositoryPort,
        *,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
    ) -> None:
        if (
            not callable(getattr(repository, "records_for_card", None))
            or not callable(getattr(repository, "enqueue_rolling_job", None))
            or not callable(getattr(repository, "commit_rolling_publication", None))
            or not callable(getattr(signer, "sign", None))
            or not isinstance(trust_store, IntegrityTrustStore)
        ):
            raise DurableJobError("rolling coordinator requires durable typed authorities")
        trust_store.identity(signer.identity.key_id)
        self._repository = repository
        self._signer = signer
        self._trust_store = trust_store
        self._planner = RollingPreparationPlanner()
        self._current: dict[tuple[str, str, str, str], RollingCardPublication] = {}
        self._repository.recover_rolling_restart()
        self._repository.rebuild_rolling_current_projection()
        self._repository.cancel_closed_rolling_jobs()
        self._repository.supersede_closed_rolling_publications()
        self._repository.recover_rolling_restart()
        self._recover_current()

    def install_council_authority(self, manifest: SignedManifest, *, installed_at: str) -> str:
        timestamp = require_utc_milliseconds(installed_at)
        payload = self._verify_council_manifest(manifest)
        return self._repository.install_rolling_council_authority(
            manifest, bundle_digest=payload["bundle_digest"], installed_at=timestamp
        )

    def schedule(
        self,
        candidates: tuple[PreparationCandidate, ...],
        *,
        capacity_use: CapacityUse,
        council_manifest_digest: str,
        observed_at: str,
        promoted_council_authority: Any | None = None,
        token_key: Any | None = None,
        member_deadlines: Mapping[str, Any] | None = None,
    ) -> tuple[Any, ...]:
        if not isinstance(capacity_use, CapacityUse):
            raise DurableJobError("rolling scheduling requires typed capacity use")
        now = require_utc_milliseconds(observed_at)
        council = self._load_council(council_manifest_digest)
        checkpoint = self._planner._checkpoint()
        try:
            plan = self._planner.plan(candidates)
            for invalidated in plan.invalidated:
                self._cancel_card_jobs(invalidated, now, "dependency_superseded")
                self._supersede_current(invalidated, now, "dependency_superseded")
            scheduled: list[Any] = []
            for candidate in plan.pending:
                if self._repository.rolling_epoch_closed(str(candidate.key.tournament_epoch_id)):
                    raise DurableJobError("rolling epoch is closed")
                if council["bundle_digest"] != candidate.key.bundle_digest:
                    raise DurableJobError("rolling council bundle differs from card bundle")
                if candidate.evidence_packet is None:
                    raise DurableJobError("durable rolling work requires its evidence packet")
                existing = self._repository.records_for_card(candidate.key.card_digest)
                existing_by_ordinal = {
                    item.payload()["component_ordinal"]: item for item in existing
                }
                if len(existing_by_ordinal) != len(existing):
                    raise DurableJobError("rolling card has duplicate component ordinals")
                component_plan = self._component_plan(council)
                executable_llm = self._executable_llm_payloads(
                    candidate,
                    council,
                    promoted_council_authority=promoted_council_authority,
                    token_key=token_key,
                    member_deadlines=member_deadlines,
                )
                payloads = tuple(
                    {
                        "schema_version": "strathmark-v3-rolling-component-job-v1",
                        "card_key": candidate.key.to_dict(),
                        "component_id": component_id,
                        "component_ordinal": ordinal,
                        "member_manifest_digest": member_digest,
                        "council_manifest_digest": council_manifest_digest,
                        "evidence_packet": candidate.evidence_packet.to_dict(),
                        **(
                            {}
                            if component_id not in executable_llm
                            else {"llm_job_payload": executable_llm[component_id]}
                        ),
                    }
                    for ordinal, (component_id, _kind, member_digest) in enumerate(
                        component_plan, start=1
                    )
                )
                actual_blob_bytes = sum(len(canonical_bytes(payload)) for payload in payloads)
                effective_capacity = CapacityUse(
                    capacity_use.open_tournaments,
                    capacity_use.round_entrants,
                    capacity_use.field_entrants,
                    capacity_use.plausible_qualifiers,
                    capacity_use.context_cards,
                    capacity_use.receipt_bytes,
                    actual_blob_bytes,
                    capacity_use.api_page_size,
                )
                for ordinal, (
                    (component_id, kind, member_digest),
                    payload,
                ) in enumerate(zip(component_plan, payloads, strict=True), start=1):
                    existing_item = existing_by_ordinal.get(ordinal)
                    if existing_item is not None:
                        if (
                            existing_item.payload() != payload
                            or existing_item.job_kind is not kind
                            or existing_item.evidence_digest != candidate.key.evidence_digest
                            or existing_item.bundle_digest != candidate.key.bundle_digest
                        ):
                            raise DurableJobError(
                                "partial rolling component differs from canonical work"
                            )
                        scheduled.append(existing_item)
                        continue
                    digest_prefix = candidate.key.card_digest[:32]
                    request_values = dict(
                        job_id=f"job:card-{digest_prefix}-{ordinal}",
                        job_revision=1,
                        idempotency_key=f"job_request:card-{digest_prefix}-{ordinal}",
                        job_kind=kind,
                        lane=kind.lane,
                        priority=self._priority(candidate.preparation_class),
                        capacity_use=effective_capacity,
                        payload=payload,
                        evidence_digest=candidate.key.evidence_digest,
                        bundle_digest=candidate.key.bundle_digest,
                        retry_policy_version="rolling-card-v1",
                        created_at=now,
                        not_before_at=now,
                        hard_deadline_at=candidate.hard_deadline_at,
                        max_attempts=3,
                    )
                    scheduled.append(self._repository.enqueue_rolling_job(**request_values))
            return tuple(scheduled)
        except Exception:
            self._planner._restore(checkpoint)
            raise

    def schedule_executable(
        self,
        candidates: tuple[PreparationCandidate, ...],
        *,
        capacity_use: CapacityUse,
        council_manifest_digest: str,
        promoted_council_authority: Any,
        token_key: Any,
        member_deadlines: Mapping[str, Any],
        observed_at: str,
    ) -> tuple[Any, ...]:
        """Schedule the ordinary rolling path with exact executable member payloads."""

        return self.schedule(
            candidates,
            capacity_use=capacity_use,
            council_manifest_digest=council_manifest_digest,
            observed_at=observed_at,
            promoted_council_authority=promoted_council_authority,
            token_key=token_key,
            member_deadlines=member_deadlines,
        )

    @staticmethod
    def _executable_llm_payloads(
        candidate: PreparationCandidate,
        council: Mapping[str, Any],
        *,
        promoted_council_authority: Any | None,
        token_key: Any | None,
        member_deadlines: Mapping[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        supplied = (
            promoted_council_authority is not None,
            token_key is not None,
            member_deadlines is not None,
        )
        if not any(supplied):
            return {}
        if not all(supplied):
            raise DurableJobError("executable council scheduling requires every typed authority")
        from strathmark.v3.assessors.llm_council import (
            DeadlineBudget,
            HMACTokenKey,
            PromotedCouncilAuthority,
            build_provider_packet,
            create_llm_job_payload,
        )

        if (
            not isinstance(promoted_council_authority, PromotedCouncilAuthority)
            or not isinstance(token_key, HMACTokenKey)
            or not isinstance(member_deadlines, Mapping)
            or candidate.evidence_packet is None
            or promoted_council_authority.bundle_digest != candidate.key.bundle_digest
        ):
            raise DurableJobError("executable council authority differs from the causal card")
        members = {item.member_id: item for item in promoted_council_authority.members}
        roster = {item["member_id"]: item for item in council["members"]}
        if set(members) != set(roster) or set(member_deadlines) != set(members):
            raise DurableJobError("promoted council roster or deadline coverage differs")
        payloads = {}
        for member_id, member in members.items():
            row = roster[member_id]
            deadline = member_deadlines[member_id]
            if (
                row["provider_kind"] != member.provider_kind.value
                or row["family"] != member.family
                or not isinstance(deadline, DeadlineBudget)
            ):
                raise DurableJobError("promoted council member differs from rolling authority")
            packet = build_provider_packet(
                candidate.evidence_packet,
                member,
                token_key,
                scope=f"card_{candidate.key.card_digest}",
            )
            payload = create_llm_job_payload(packet, member, deadline)
            if payload["member_manifest_digest"] != row["member_manifest_digest"]:
                raise DurableJobError("promoted council member manifest differs")
            payloads[member_id] = payload
        return payloads

    def seal_card(
        self,
        key: CardKey,
        authority: Any,
        *,
        council_manifest_digest: str,
        council_aggregate_authority: SignedManifest,
        observed_at: str,
    ) -> RollingCardPublication:
        from strathmark.v3.application.field_assembly import CompetitorCardAuthority
        from strathmark.v3.contracts.forecasts import AssessorKind, ForecastState

        if not isinstance(key, CardKey) or not isinstance(authority, CompetitorCardAuthority):
            raise DurableJobError("rolling seal requires a typed card and authority")
        now = require_utc_milliseconds(observed_at)
        council = self._load_council(council_manifest_digest)
        self._verify_card_binding(key, authority)
        jobs = list(self._repository.records_for_card(key.card_digest))
        if len(jobs) != 5:
            raise DurableJobError("rolling card cannot seal before all component jobs exist")
        if any(item.state.value in self._ACTIVE_STATES for item in jobs):
            if now < min(item.hard_deadline_at for item in jobs):
                raise DurableJobError("rolling card components are not terminal before deadline")
            for item in jobs:
                if item.state.value in self._ACTIVE_STATES:
                    self._repository.cancel(
                        item.job_id,
                        item.job_revision,
                        observed_at=now,
                        reason="deadline_sealed",
                    )
            jobs = list(self._repository.records_for_card(key.card_digest))
        receipts = tuple(self._component_receipt(item) for item in jobs)
        self._verify_component_set(
            key, receipts, council, council_manifest_digest=council_manifest_digest
        )
        by_id = {item.component_id: item for item in receipts}
        success = RollingComponentOutcome.SUCCEEDED
        formula = by_id["formula"].outcome is success
        ml = by_id["ml"].outcome is success
        council_success = sum(
            by_id[item["member_id"]].outcome is success for item in council["members"]
        )
        forecast_by_kind = {item.assessor: item for item in authority.forecasts}
        if (
            formula
            and by_id["formula"].result_digest
            != forecast_by_kind[AssessorKind.FORMULA].commit_digest
        ):
            raise DurableJobError("formula job publication differs from signed card")
        if ml and by_id["ml"].result_digest != forecast_by_kind[AssessorKind.ML].commit_digest:
            raise DurableJobError("ML job publication differs from signed card")
        expected_committed = {
            AssessorKind.FORMULA: formula,
            AssessorKind.ML: ml,
            AssessorKind.LLM_COUNCIL: council_success >= 2,
        }
        if any(
            (forecast.state is ForecastState.COMMITTED) != expected_committed[kind]
            for kind, forecast in forecast_by_kind.items()
        ):
            raise DurableJobError("card availability differs from terminal component receipts")
        self._verify_council_aggregate(
            council_aggregate_authority,
            key=key,
            authority=authority,
            receipts=receipts,
            council=council,
            council_manifest_digest=council_manifest_digest,
        )
        availability = (
            ("formula", "available" if formula else "unavailable"),
            ("ml", "available" if ml else "unavailable"),
            (
                "llm_council",
                (
                    "normal_3_of_3"
                    if council_success == 3
                    else (
                        "degraded_2_of_3"
                        if council_success == 2
                        else f"unavailable_{council_success}_of_3"
                    )
                ),
            ),
        )
        authority_value = authority.to_dict()
        components_value = [item.to_dict() for item in receipts]
        existing = self._repository.rolling_publication_row(card_digest=key.card_digest)
        if existing is not None:
            published = self._decode_publication_row(existing)
            if (
                published.key != key
                or published.authority.content_value() != authority.content_value()
                or published.components != receipts
                or published.availability != availability
                or published.council_manifest_digest != council_manifest_digest
                or published.council_aggregate_manifest.body_digest
                != council_aggregate_authority.body_digest
            ):
                raise DurableJobError("existing rolling publication material differs")
            return published
        scheduled_key = self._repository.rolling_card_key_for_field(
            competitor_id=str(key.competitor_id),
            target_context_digest=key.target_context_digest,
            historical_cutoff_key=str(key.historical_cutoff_key),
            tournament_epoch_id=str(key.tournament_epoch_id),
            bundle_digest=key.bundle_digest,
        )
        if scheduled_key is None or scheduled_key != key.to_dict():
            raise DurableJobError("superseded card publication cannot become current")
        content = {
            "schema_version": "strathmark-v3-rolling-card-publication-v1",
            "card_key": key.to_dict(),
            "card_authority_digest": canonical_digest(authority_value),
            "component_refs_digest": canonical_digest(components_value),
            "availability": [list(item) for item in availability],
            "council_manifest_digest": council_manifest_digest,
            "council_aggregate_manifest_digest": council_aggregate_authority.body_digest,
            "hard_deadline_at": min(item.hard_deadline_at for item in jobs),
            "sealed_at": now,
        }
        publication_digest = canonical_digest(content)
        manifest = sign_manifest(
            "rolling_card_publication",
            {**content, "publication_digest": publication_digest},
            signer=self._signer,
            created_at=now,
        )
        publication = RollingCardPublication(
            key,
            authority,
            receipts,
            availability,
            council_manifest_digest,
            council_aggregate_authority,
            content["hard_deadline_at"],
            now,
            manifest,
            publication_digest,
        )
        storage_row = {
            "publication_digest": publication_digest,
            "card_digest": key.card_digest,
            "competitor_id": str(key.competitor_id),
            "target_context_digest": key.target_context_digest,
            "dependency_revision": key.dependency_revision,
            "tournament_epoch_id": str(key.tournament_epoch_id),
            "bundle_digest": key.bundle_digest,
            "evidence_digest": key.evidence_digest,
            "hard_deadline_at": publication.hard_deadline_at,
            "sealed_at": publication.sealed_at,
            "authority_json": canonical_bytes(authority_value).decode("utf-8"),
            "authority_digest": canonical_digest(authority_value),
            "component_refs_json": canonical_bytes(components_value).decode("utf-8"),
            "component_refs_digest": canonical_digest(components_value),
            "availability_json": canonical_bytes([list(item) for item in availability]).decode(
                "utf-8"
            ),
            "availability_digest": canonical_digest([list(item) for item in availability]),
            "council_manifest_digest": council_manifest_digest,
            "council_aggregate_manifest_json": canonical_bytes(
                council_aggregate_authority.to_dict()
            ).decode("utf-8"),
            "publication_manifest_json": canonical_bytes(manifest.to_dict()).decode("utf-8"),
        }
        stored = self._repository.commit_rolling_publication(
            storage_row, expected_jobs=tuple(jobs), observed_at=now
        )
        publication = self._decode_publication_row(stored)
        self._current[_card_subject(key)] = publication
        self._planner = RollingPreparationPlanner(
            tuple(
                CompletedCard(item.key, item.publication_digest) for item in self._current.values()
            )
        )
        return publication

    def cached(self, key: CardKey) -> RollingCardPublication:
        if not isinstance(key, CardKey):
            raise DurableJobError("rolling cache lookup requires a CardKey")
        publication = self._current.get(_card_subject(key))
        if publication is None or publication.key != key:
            raise KeyError(key.card_digest)
        return publication

    def current_publications_for_field(
        self, field: FrozenFieldRevision
    ) -> tuple[RollingCardPublication, ...]:
        """Return the exact ordered rolling authority for one frozen field."""

        from strathmark.v3.application.field_assembly import FrozenFieldRevision

        if not isinstance(field, FrozenFieldRevision):
            raise DurableJobError("rolling field lookup requires frozen authority")
        publications = []
        for assignment in field.ordered_assignments:
            publication = self._current.get(
                (
                    str(assignment.competitor_id),
                    field.target_context.digest,
                    str(field.tournament_epoch_id),
                    field.bundle_digest,
                )
            )
            if publication is None:
                raise DurableJobError("rolling field publication is missing")
            key = publication.key
            packet = publication.authority.evidence_packet
            if (
                key.competitor_id != assignment.competitor_id
                or key.target_context_digest != field.target_context.digest
                or key.historical_cutoff_key != field.historical_cutoff_key
                or key.tournament_epoch_id != field.tournament_epoch_id
                or key.bundle_digest != field.bundle_digest
                or key.evidence_digest != packet.content_digest
                or packet.target_context != field.target_context
                or str(packet.historical_cutoff_key) != str(field.historical_cutoff_key)
                or str(packet.tournament_epoch_id) != str(field.tournament_epoch_id)
                or packet.tournament_event_sequence != field.tournament_event_sequence
            ):
                raise DurableJobError("rolling field publication differs from frozen authority")
            publications.append(publication)
        return tuple(publications)

    def publications_for_forecast(self, snapshot: object) -> tuple[RollingCardPublication, ...]:
        """Return exact ordered cards for a field-independent frozen forecast set."""

        from strathmark.v3.contracts.pre_field_forecasts import ForecastSetSnapshot

        if not isinstance(snapshot, ForecastSetSnapshot):
            raise DurableJobError("rolling forecast lookup requires frozen authority")
        publications = []
        for competitor_id in snapshot.ordered_competitor_ids:
            key_value = self._repository.rolling_card_key_for_field(
                competitor_id=str(competitor_id),
                target_context_digest=snapshot.target_context.digest,
                historical_cutoff_key=str(snapshot.historical_cutoff_key),
                tournament_epoch_id=str(snapshot.tournament_epoch_id),
                bundle_digest=snapshot.bundle_digest,
            )
            if key_value is None:
                raise DurableJobError("rolling forecast publication is missing")
            row = self._repository.rolling_publication_row(
                card_digest=str(key_value["card_digest"])
            )
            if row is None:
                raise DurableJobError("rolling forecast card is not published")
            publication = self._decode_publication_row(row)
            packet = publication.authority.evidence_packet
            if (
                publication.key.to_dict() != key_value
                or publication.key.competitor_id != competitor_id
                or packet.target_context != snapshot.target_context
                or packet.tournament_event_sequence > snapshot.maximum_tournament_sequence
            ):
                raise DurableJobError("rolling forecast publication differs from snapshot")
            publications.append(publication)
        return tuple(publications)

    def schedule_forecast(self, snapshot: object, *, observed_at: str) -> tuple[Any, ...]:
        """Schedule exact field-independent card work from frozen round authority.

        This derives only a competitor's causal evidence packet.  It deliberately
        has no field, roster-opponent, stand, mark, or simulation input.
        """

        from strathmark.v3.contracts.pre_field_forecasts import ForecastSetSnapshot
        from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection

        if not isinstance(snapshot, ForecastSetSnapshot):
            raise DurableJobError("rolling forecast scheduling requires frozen authority")
        now = require_utc_milliseconds(observed_at)
        with open_v3_connection(self._repository.database_path, read_only=True) as connection:
            council_rows = connection.execute(
                "SELECT manifest_digest FROM v3_rolling_council_authorities WHERE bundle_digest=?",
                (snapshot.bundle_digest,),
            ).fetchall()
            if len(council_rows) != 1:
                raise DurableJobError("rolling forecast bundle has no unique council authority")
            candidates = []
            for competitor_id in snapshot.ordered_competitor_ids:
                rows = connection.execute(
                    "SELECT observation_json FROM v3_result_revisions revision "
                    "WHERE revision.tournament_id=? AND revision.competitor_id=? "
                    "AND revision.source_global_sequence<=? AND revision.revision=("
                    "SELECT MAX(current.revision) FROM v3_result_revisions current "
                    "WHERE current.result_key=revision.result_key AND "
                    "current.source_global_sequence<=?) ORDER BY source_global_sequence "
                    "LIMIT 257",
                    (
                        str(snapshot.tournament_id),
                        str(competitor_id),
                        snapshot.maximum_tournament_sequence,
                        snapshot.maximum_tournament_sequence,
                    ),
                ).fetchall()
                if len(rows) > 256:
                    raise DurableJobError("rolling forecast evidence exceeds bounded capacity")
                packet = EvidencePacket.create(
                    competitor_id=competitor_id,
                    target_context=snapshot.target_context,
                    observations=tuple(
                        ResultObservation.from_dict(json.loads(str(row[0]))) for row in rows
                    ),
                    taxonomy_version=snapshot.target_context.taxonomy_version,
                    conversion_version=snapshot.target_context.conversion_version,
                    historical_cutoff_key=str(snapshot.historical_cutoff_key),
                    tournament_epoch_id=snapshot.tournament_epoch_id,
                    tournament_event_sequence=snapshot.maximum_tournament_sequence,
                )
                candidates.append(
                    PreparationCandidate.create(
                        competitor_id=str(competitor_id),
                        target_context_digest=snapshot.target_context.digest,
                        historical_cutoff_key=str(snapshot.historical_cutoff_key),
                        tournament_epoch_id=str(snapshot.tournament_epoch_id),
                        bundle_digest=snapshot.bundle_digest,
                        evidence_digest=packet.content_digest,
                        dependency_revision=snapshot.maximum_tournament_sequence,
                        preparation_class=PreparationClass.SCHEDULED,
                        hard_deadline_at=snapshot.hard_deadline_at,
                        evidence_packet=packet,
                    )
                )
        count = len(candidates)
        return self.schedule(
            tuple(candidates),
            capacity_use=CapacityUse(1, count, 0, count, count, 0, 0, count),
            council_manifest_digest=str(council_rows[0][0]),
            observed_at=now,
        )

    def readiness(self, keys: tuple[CardKey, ...], *, observed_at: str) -> RollingReadiness:
        now = require_utc_milliseconds(observed_at)
        if not isinstance(keys, tuple) or not all(isinstance(item, CardKey) for item in keys):
            raise DurableJobError("rolling readiness requires typed card keys")
        if len({item.card_digest for item in keys}) != len(keys):
            raise DurableJobError("rolling readiness cannot repeat cards")
        ready = 0
        pending = 0
        failed = 0
        deadlines: list[str] = []
        for key in keys:
            current = self._current.get(_card_subject(key))
            if current is not None and current.key == key:
                ready += 1
                deadlines.append(current.hard_deadline_at)
                continue
            jobs = self._repository.records_for_card(key.card_digest)
            deadlines.extend(item.hard_deadline_at for item in jobs)
            if jobs and any(item.state.value in self._ACTIVE_STATES for item in jobs):
                pending += 1
            else:
                failed += 1
        return RollingReadiness(
            now,
            len(keys),
            ready,
            pending,
            failed,
            min(deadlines, default=None),
            bool(keys) and ready == len(keys),
        )

    def close_epoch(
        self,
        epoch_id: str | StableIdentifier,
        *,
        source_event: EventEnvelope,
        observed_at: str,
    ) -> tuple[Any, ...]:
        epoch = require_identifier(epoch_id, expected_namespace="epoch")
        now = require_utc_milliseconds(observed_at)
        if (
            not isinstance(source_event, EventEnvelope)
            or source_event.kind not in {EventKind.ROUND_CLOSED, EventKind.TOURNAMENT_CLOSED}
            or now < source_event.occurred_at_utc
        ):
            raise DurableJobError("epoch close requires exact canonical lifecycle event")
        self._repository.close_rolling_epoch(str(epoch), source_event)
        cancelled = list(self._repository.cancel_closed_rolling_jobs())
        for value in self._repository.rolling_card_keys_for_epoch(str(epoch)):
            key = _card_key_from_dict(value)
            self._supersede_current(key, now, "epoch_closed")
        self._planner = RollingPreparationPlanner(
            tuple(
                CompletedCard(item.key, item.publication_digest) for item in self._current.values()
            )
        )
        return tuple(cancelled)

    def enqueue_weight_recombination(
        self,
        *,
        field: FrozenFieldRevision,
        keys: tuple[CardKey, ...],
        weight_authority: OperationalWeightAuthority,
        authority_store: FieldAuthorityVerifierPort,
        capacity_use: CapacityUse,
        observed_at: str,
        hard_deadline_at: str,
    ) -> Any:
        from strathmark.v3.application.field_assembly import (
            FrozenFieldRevision,
            OperationalWeightAuthority,
        )

        if not isinstance(field, FrozenFieldRevision) or not isinstance(
            weight_authority, OperationalWeightAuthority
        ):
            raise DurableJobError("weight recombination requires typed field authority")
        if not callable(getattr(authority_store, "verify_current_field", None)) or not callable(
            getattr(authority_store, "verify_weight_authority", None)
        ):
            raise DurableJobError("weight recombination requires an authority verifier")
        authority_store.verify_current_field(field)
        authority_store.verify_weight_authority(weight_authority)
        now = require_utc_milliseconds(observed_at)
        deadline = require_utc_milliseconds(hard_deadline_at)
        if now >= deadline or not isinstance(capacity_use, CapacityUse):
            raise DurableJobError("weight recombination timing or capacity is invalid")
        if not keys or len({item.card_digest for item in keys}) != len(keys):
            raise DurableJobError("weight recombination requires unique sealed cards")
        publications = tuple(self.cached(item) for item in keys)
        publication_by_competitor = {str(item.key.competitor_id): item for item in publications}
        expected_competitors = {str(item.competitor_id) for item in field.ordered_assignments}
        if (
            {str(item.key.competitor_id) for item in publications} != expected_competitors
            or len(publications) != len(field.ordered_assignments)
            or any(
                item.key.target_context_digest != field.target_context.digest
                or item.key.tournament_epoch_id != field.tournament_epoch_id
                or item.key.bundle_digest != field.bundle_digest
                or item.key.historical_cutoff_key != field.historical_cutoff_key
                for item in publications
            )
            or weight_authority.tournament_id != field.tournament_id
            or weight_authority.round_id != field.round_id
            or weight_authority.epoch_id != field.tournament_epoch_id
            or weight_authority.epoch_digest != field.evidence_digest
            or weight_authority.frozen_tournament_sequence != field.tournament_event_sequence
        ):
            raise DurableJobError("recombination authority differs from current field")
        from strathmark.v3.domain.pooling import ContextNode

        expected_context = ContextNode(
            field.target_context.event_code,
            f"{(field.target_context.size_mm // 50) * 50}_"
            f"{(field.target_context.size_mm // 50) * 50 + 49}",
            field.target_context.material_code,
        )
        if (
            weight_authority.binding.context.history_depth is not None
            or not weight_authority.binding.context.contains(expected_context)
        ):
            raise DurableJobError("recombination weight context differs from field")
        publications = tuple(
            publication_by_competitor[str(item.competitor_id)] for item in field.ordered_assignments
        )
        payload = {
            "schema_version": "strathmark-v3-weight-only-recombination-v1",
            "tournament_id": str(field.tournament_id),
            "round_id": str(field.round_id),
            "field_id": str(field.field_id),
            "upstream_field_revision": field.field_revision,
            "field_revision_digest": field.revision_digest,
            "tournament_epoch_id": str(field.tournament_epoch_id),
            "field_authority": {
                **field.content_value(),
                "revision_digest": field.revision_digest,
            },
            "card_publication_digests": [item.publication_digest for item in publications],
            "weight_authority_digest": weight_authority.authority_digest,
            "weight_authority": weight_authority.to_dict(),
            "provider_recall": False,
        }
        digest = canonical_digest(payload)
        request_values = dict(
            job_id=f"job:recombine-{digest[:40]}",
            job_revision=1,
            idempotency_key=f"job_request:recombine-{digest[:40]}",
            job_kind=JobKind.HOT_FIELD_ASSEMBLY,
            lane=JobKind.HOT_FIELD_ASSEMBLY.lane,
            priority=JobPriority.IMMINENT_FIELD,
            capacity_use=capacity_use,
            payload=payload,
            evidence_digest=canonical_digest([item.key.evidence_digest for item in publications]),
            bundle_digest=publications[0].key.bundle_digest,
            retry_policy_version="rolling-recombination-v1",
            created_at=now,
            not_before_at=now,
            hard_deadline_at=deadline,
            max_attempts=2,
        )
        return self._repository.enqueue_rolling_job(**request_values)

    def verify(self) -> None:
        self._repository.verify()
        self._repository.verify_rolling_storage()
        for row in self._repository.rolling_publication_rows():
            self._decode_publication_row(row)

    def _recover_current(self) -> None:
        all_publications = tuple(
            self._decode_publication_row(row) for row in self._repository.rolling_publication_rows()
        )
        newest: dict[tuple[str, str, str, str], RollingCardPublication] = {}
        for publication in all_publications:
            current_key = self._repository.rolling_card_key_for_field(
                competitor_id=str(publication.key.competitor_id),
                target_context_digest=publication.key.target_context_digest,
                historical_cutoff_key=str(publication.key.historical_cutoff_key),
                tournament_epoch_id=str(publication.key.tournament_epoch_id),
                bundle_digest=publication.key.bundle_digest,
            )
            if current_key != publication.key.to_dict():
                continue
            subject = _card_subject(publication.key)
            prior = newest.get(subject)
            if prior is None or publication.key.dependency_revision > prior.key.dependency_revision:
                newest[subject] = publication
            elif publication.key.dependency_revision == prior.key.dependency_revision:
                raise DurableJobError("scoped rolling dependency revision conflicts")
        recovered = tuple(newest.values())
        self._current = dict(newest)
        self._planner = RollingPreparationPlanner(
            tuple(CompletedCard(item.key, item.publication_digest) for item in recovered)
        )

    def _load_council(self, digest: str) -> dict[str, Any]:
        _require_digest(digest, "rolling council manifest")
        bundle_digest, manifest = self._repository.rolling_council_authority(digest)
        payload = self._verify_council_manifest(manifest)
        if bundle_digest != payload["bundle_digest"]:
            raise DurableJobError("rolling council bundle index differs")
        return payload

    def _verify_council_manifest(self, manifest: SignedManifest) -> dict[str, Any]:
        if not isinstance(manifest, SignedManifest) or manifest.kind != self._COUNCIL_KIND:
            raise DurableJobError("rolling council authority kind differs")
        payload = verify_manifest(manifest, self._trust_store)
        if set(payload) != {
            "schema_version",
            "purpose",
            "bundle_digest",
            "members",
        } or (
            payload["schema_version"] != "strathmark-v3-rolling-council-roster-v1"
            or payload["purpose"] != "rolling_card_council"
        ):
            raise DurableJobError("rolling council authority schema differs")
        _require_digest(payload["bundle_digest"], "rolling council bundle")
        members = payload["members"]
        if not isinstance(members, list) or len(members) != 3:
            raise DurableJobError("rolling council requires three exact members")
        kinds = []
        families = []
        identities = []
        for member in members:
            if not isinstance(member, dict) or set(member) != {
                "member_id",
                "provider_kind",
                "family",
                "member_manifest_digest",
            }:
                raise DurableJobError("rolling council member authority fields differ")
            for field in ("member_id", "family"):
                candidate = member[field]
                if (
                    not isinstance(candidate, str)
                    or not candidate
                    or len(candidate) > 128
                    or any(
                        not (character.islower() or character.isdigit() or character in "_.:-")
                        for character in candidate
                    )
                ):
                    raise DurableJobError(
                        f"rolling council {field} must be a bounded machine token"
                    )
            if not isinstance(member["provider_kind"], str):
                raise DurableJobError("rolling council provider kind must be a token")
            _require_digest(member["member_manifest_digest"], "council member manifest")
            identities.append(member["member_id"])
            families.append(member["family"])
            kinds.append(member["provider_kind"])
        if (
            len(set(identities)) != 3
            or len(set(families)) != 3
            or kinds.count("local") != 2
            or kinds.count("cloud") != 1
            or any(kind not in {"local", "cloud"} for kind in kinds)
        ):
            raise DurableJobError("rolling council roster lacks independent 2-local/1-cloud pins")
        return payload

    @staticmethod
    def _component_plan(
        council: dict[str, Any],
    ) -> tuple[tuple[str, JobKind, str | None], ...]:
        members = tuple(council["members"])
        local = tuple(item for item in members if item["provider_kind"] == "local")
        cloud = next(item for item in members if item["provider_kind"] == "cloud")
        return (
            ("formula", JobKind.FORMULA_CARD, None),
            ("ml", JobKind.ML_CARD, None),
            *tuple(
                (
                    item["member_id"],
                    JobKind.LOCAL_LLM_CARD,
                    item["member_manifest_digest"],
                )
                for item in local
            ),
            (
                cloud["member_id"],
                JobKind.CLOUD_LLM_CARD,
                cloud["member_manifest_digest"],
            ),
        )

    @staticmethod
    def _priority(value: PreparationClass) -> JobPriority:
        return {
            PreparationClass.SCHEDULED: JobPriority.SCHEDULED_ENTRANT,
            PreparationClass.PLAUSIBLE_QUALIFIER: JobPriority.PLAUSIBLE_QUALIFIER,
            PreparationClass.IMMINENT_FIELD: JobPriority.IMMINENT_FIELD,
        }[value]

    def _verify_card_binding(self, key: CardKey, authority: Any) -> None:
        packet = authority.evidence_packet
        if (
            str(packet.competitor_id) != str(key.competitor_id)
            or packet.target_context.digest != key.target_context_digest
            or str(packet.historical_cutoff_key) != str(key.historical_cutoff_key)
            or str(packet.tournament_epoch_id) != str(key.tournament_epoch_id)
            or packet.content_digest != key.evidence_digest
            or authority.bundle_digest != key.bundle_digest
            or verify_manifest(authority.manifest, self._trust_store) != authority.content_value()
        ):
            raise DurableJobError("signed card authority differs from causal card key")

    @staticmethod
    def _component_receipt(record: Any) -> RollingComponentReceipt:
        payload = record.payload()
        state = getattr(record.state, "value", None)
        outcome = {
            "succeeded": RollingComponentOutcome.SUCCEEDED,
            "permanent-failed": RollingComponentOutcome.FAILED,
            "cancelled": RollingComponentOutcome.CANCELLED,
            "stale": RollingComponentOutcome.STALE,
            "invalid": RollingComponentOutcome.INVALID,
        }.get(state)
        if outcome is None:
            raise DurableJobError("rolling component is not terminal")
        if record.terminal_reason in {"deadline_exceeded", "deadline_sealed"}:
            outcome = RollingComponentOutcome.TIMED_OUT
        return RollingComponentReceipt(
            payload["component_id"],
            payload["component_ordinal"],
            record.job_id,
            record.job_revision,
            record.job_kind,
            outcome,
            record.result_digest,
            record.terminal_reason,
            record.fencing_token,
            record.payload_digest,
        )

    def _verify_component_set(
        self,
        key: CardKey,
        receipts: tuple[RollingComponentReceipt, ...],
        council: dict[str, Any],
        *,
        council_manifest_digest: str,
    ) -> None:
        expected = self._component_plan(council)
        if tuple((item.component_id, item.job_kind) for item in receipts) != tuple(
            (component, kind) for component, kind, _digest in expected
        ):
            raise DurableJobError("rolling component set differs from installed council roster")
        records = self._repository.records_for_card(key.card_digest)
        by_ordinal = {item.payload()["component_ordinal"]: item for item in records}
        if len(by_ordinal) != 5:
            raise DurableJobError("rolling component receipts differ from durable jobs")
        for ordinal, (component_id, kind, member_digest) in enumerate(expected, start=1):
            record = by_ordinal[ordinal]
            from strathmark.v3.contracts.evidence import EvidencePacket

            packet = EvidencePacket.from_dict(record.payload()["evidence_packet"])
            if (
                packet.content_digest != key.evidence_digest
                or str(packet.competitor_id) != str(key.competitor_id)
                or packet.target_context.digest != key.target_context_digest
                or str(packet.historical_cutoff_key) != str(key.historical_cutoff_key)
                or str(packet.tournament_epoch_id) != str(key.tournament_epoch_id)
            ):
                raise DurableJobError("rolling job evidence packet differs from card key")
            expected_payload = {
                "schema_version": "strathmark-v3-rolling-component-job-v1",
                "card_key": key.to_dict(),
                "component_id": component_id,
                "component_ordinal": ordinal,
                "member_manifest_digest": member_digest,
                "council_manifest_digest": council_manifest_digest,
                "evidence_packet": packet.to_dict(),
            }
            if (
                kind
                in {
                    JobKind.LOCAL_LLM_CARD,
                    JobKind.CLOUD_LLM_CARD,
                }
                and "llm_job_payload" in record.payload()
            ):
                nested = record.payload().get("llm_job_payload")
                if (
                    not isinstance(nested, dict)
                    or set(nested)
                    != {
                        "schema_version",
                        "member_manifest_digest",
                        "provider_packet",
                        "deadlines",
                    }
                    or nested.get("schema_version") != "strathmark-v3-llm-job-payload-v1"
                    or nested.get("member_manifest_digest") != member_digest
                ):
                    raise DurableJobError("rolling LLM executable payload differs")
                expected_payload["llm_job_payload"] = nested
            if (
                record.payload() != expected_payload
                or record.job_kind is not kind
                or record.evidence_digest != key.evidence_digest
                or record.bundle_digest != key.bundle_digest
                or record.payload_digest != receipts[ordinal - 1].payload_digest
            ):
                raise DurableJobError(
                    "rolling component payload differs from causal card authority"
                )

    def _verify_council_aggregate(
        self,
        manifest: SignedManifest,
        *,
        key: CardKey,
        authority: Any,
        receipts: tuple[RollingComponentReceipt, ...],
        council: dict[str, Any],
        council_manifest_digest: str,
    ) -> None:
        from strathmark.v3.contracts.forecasts import AssessorKind, ForecastState

        if (
            not isinstance(manifest, SignedManifest)
            or manifest.kind != "rolling_council_aggregate_authority"
        ):
            raise DurableJobError("rolling council aggregate authority kind differs")
        payload = verify_manifest(manifest, self._trust_store)
        by_id = {item.component_id: item for item in receipts}
        member_receipts = []
        successes = 0
        for member in council["members"]:
            receipt = by_id[member["member_id"]]
            if receipt.outcome is RollingComponentOutcome.SUCCEEDED:
                successes += 1
            member_receipts.append(
                {
                    "member_id": member["member_id"],
                    "member_manifest_digest": member["member_manifest_digest"],
                    "job_id": receipt.job_id,
                    "job_revision": receipt.job_revision,
                    "fencing_token": receipt.fencing_token,
                    "outcome": receipt.outcome.value,
                    "result_digest": receipt.result_digest,
                    "terminal_reason_code": receipt.terminal_reason_code,
                }
            )
        council_forecast = next(
            item for item in authority.forecasts if item.assessor is AssessorKind.LLM_COUNCIL
        )
        expected = {
            "schema_version": "strathmark-v3-rolling-council-aggregate-v1",
            "purpose": "rolling_card_council_aggregate",
            "card_digest": key.card_digest,
            "council_manifest_digest": council_manifest_digest,
            "member_receipts": member_receipts,
            "valid_member_count": successes,
            "aggregate_available": successes >= 2,
            "aggregate_forecast_commit_digest": council_forecast.commit_digest,
        }
        receipt_reference = payload.get("council_receipt_reference")
        if receipt_reference is not None:
            from strathmark.v3.infrastructure.ollama import RawOutputStorageReference

            try:
                reference = RawOutputStorageReference.from_dict(receipt_reference)
            except (TypeError, ValueError) as exc:
                raise DurableJobError("rolling council receipt reference differs") from exc
            receipt_artifacts = tuple(
                item for item in council_forecast.artifacts if item.role == "llm_council_receipt"
            )
            if len(receipt_artifacts) != 1 or receipt_artifacts[0].digest != reference.raw_digest:
                raise DurableJobError("rolling council forecast receipt binding differs")
            expected["council_receipt_reference"] = reference.to_dict()
        if payload != expected or (
            (council_forecast.state is ForecastState.COMMITTED) != (successes >= 2)
        ):
            raise DurableJobError(
                "rolling council aggregate differs from exact member publications"
            )

    def _cancel_card_jobs(self, key: CardKey, observed_at: str, reason: str) -> None:
        for item in self._repository.records_for_card(key.card_digest):
            if item.state.value in self._ACTIVE_STATES:
                self._repository.cancel(
                    item.job_id,
                    item.job_revision,
                    observed_at=observed_at,
                    reason=reason,
                )

    def _supersede_current(self, key: CardKey, observed_at: str, reason: str) -> None:
        subject = _card_subject(key)
        current = self._current.get(subject)
        if current is None or current.key != key:
            return
        self._repository.supersede_rolling_publication(
            publication_digest=current.publication_digest,
            competitor_id=subject[0],
            target_context_digest=subject[1],
            tournament_epoch_id=subject[2],
            bundle_digest=subject[3],
            observed_at=observed_at,
            reason=reason,
        )
        self._current.pop(subject, None)

    def _decode_publication_row(self, row: Mapping[str, Any]) -> RollingCardPublication:
        from strathmark.v3.application.field_assembly import CompetitorCardAuthority

        if not row:
            raise DurableJobError("rolling publication is missing")
        authority_value = json.loads(str(row["authority_json"]))
        component_value = json.loads(str(row["component_refs_json"]))
        availability_value = json.loads(str(row["availability_json"]))
        aggregate_manifest = SignedManifest.from_dict(
            json.loads(str(row["council_aggregate_manifest_json"]))
        )
        manifest = SignedManifest.from_dict(json.loads(str(row["publication_manifest_json"])))
        authority = CompetitorCardAuthority.from_dict(authority_value)
        key_value = manifest.body()["payload"]["card_key"]
        key = _card_key_from_dict(key_value)
        if key.to_dict() != key_value:
            raise DurableJobError("rolling publication card key differs")
        publication = RollingCardPublication(
            key,
            authority,
            tuple(RollingComponentReceipt.from_dict(item) for item in component_value),
            tuple((str(item[0]), str(item[1])) for item in availability_value),
            str(row["council_manifest_digest"]),
            aggregate_manifest,
            str(row["hard_deadline_at"]),
            str(row["sealed_at"]),
            manifest,
            str(row["publication_digest"]),
        )
        content = publication.content_value()
        if (
            verify_manifest(manifest, self._trust_store)
            != {**content, "publication_digest": publication.publication_digest}
            or str(row["card_digest"]) != key.card_digest
            or str(row["competitor_id"]) != str(key.competitor_id)
            or str(row["target_context_digest"]) != key.target_context_digest
            or int(row["dependency_revision"]) != key.dependency_revision
            or str(row["tournament_epoch_id"]) != str(key.tournament_epoch_id)
            or str(row["bundle_digest"]) != key.bundle_digest
            or str(row["evidence_digest"]) != key.evidence_digest
            or str(row["authority_digest"]) != canonical_digest(authority_value)
            or str(row["component_refs_digest"]) != canonical_digest(component_value)
            or str(row["availability_digest"]) != canonical_digest(availability_value)
        ):
            raise DurableJobError("rolling publication projection differs")
        self._verify_card_binding(key, authority)
        council = self._load_council(publication.council_manifest_digest)
        self._verify_council_aggregate(
            aggregate_manifest,
            key=key,
            authority=authority,
            receipts=publication.components,
            council=council,
            council_manifest_digest=publication.council_manifest_digest,
        )
        return publication


@dataclass(frozen=True, slots=True)
class ExecutableCouncilSchedule:
    """Immutable promoted authority required by the live rolling reaction path."""

    authority: Any
    token_key: Any
    member_deadlines: tuple[tuple[str, Any], ...]

    def __post_init__(self) -> None:
        from strathmark.v3.assessors.llm_council import (
            DeadlineBudget,
            HMACTokenKey,
            PromotedCouncilAuthority,
        )

        if not isinstance(self.authority, PromotedCouncilAuthority) or not isinstance(
            self.token_key, HMACTokenKey
        ):
            raise DurableJobError("executable rolling reaction requires promoted council authority")
        expected = tuple(item.member_id for item in self.authority.members)
        if (
            not isinstance(self.member_deadlines, tuple)
            or tuple(item[0] for item in self.member_deadlines) != expected
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[1], DeadlineBudget)
                for item in self.member_deadlines
            )
        ):
            raise DurableJobError("executable rolling deadlines must match the promoted roster")

    def authority_value(self) -> dict[str, Any]:
        return {
            "schema_version": "strathmark-v3-executable-council-schedule-v1",
            "bundle_digest": self.authority.bundle_digest,
            "component_digest": self.authority.component_digest,
            "signer_key_id": self.authority.signer_key_id,
            "token_key_id": self.token_key.key_id,
            "member_deadlines": [
                {
                    "member_id": member_id,
                    "queue_ms": budget.queue_ms,
                    "connect_ms": budget.connect_ms,
                    "read_ms": budget.read_ms,
                    "retry_ms": budget.retry_ms,
                    "overall_ms": budget.overall_ms,
                }
                for member_id, budget in self.member_deadlines
            ],
        }


class RollingLifecycleReactionService:
    """Translate canonical U5 event sets into idempotent durable rolling work."""

    def __init__(
        self,
        *,
        event_store: RollingEventAuthorityPort,
        coordinator: DurableRollingPreparationCoordinator,
        resolver: RollingLifecycleResolverPort,
        reaction_store: RollingJobRepositoryPort,
        clock: ClockPort,
        executable_council: ExecutableCouncilSchedule | None = None,
        test_only_allow_legacy_non_executable: bool = False,
    ) -> None:
        if (
            not callable(getattr(event_store, "event_at", None))
            or not isinstance(coordinator, DurableRollingPreparationCoordinator)
            or not callable(getattr(resolver, "resolve", None))
            or not callable(getattr(reaction_store, "pending_rolling_reactions", None))
            or not callable(getattr(reaction_store, "complete_rolling_reaction", None))
            or not callable(clock)
        ):
            raise DurableJobError("rolling lifecycle reaction requires typed ports")
        self._events = event_store
        self._coordinator = coordinator
        self._resolver = resolver
        self._reaction_store = reaction_store
        self._clock = clock
        if not isinstance(test_only_allow_legacy_non_executable, bool):
            raise DurableJobError("legacy rolling mode must be an explicit test-only choice")
        if executable_council is None and not test_only_allow_legacy_non_executable:
            raise DurableJobError("live rolling reaction requires executable council authority")
        if test_only_allow_legacy_non_executable and os.environ.get("STRATHMARK_TEST_DB") != "1":
            raise DurableJobError("legacy rolling mode is restricted to the isolated test harness")
        if executable_council is not None and not isinstance(
            executable_council, ExecutableCouncilSchedule
        ):
            raise DurableJobError("rolling lifecycle executable council authority differs")
        if executable_council is not None and test_only_allow_legacy_non_executable:
            raise DurableJobError("executable and legacy test-only rolling modes conflict")
        self._executable_council = executable_council
        self.recover_pending()

    def react(self, result: LifecycleCommandResultPort) -> None:
        if (
            isinstance(result.first_global_sequence, bool)
            or not isinstance(result.first_global_sequence, int)
            or isinstance(result.last_global_sequence, bool)
            or not isinstance(result.last_global_sequence, int)
            or result.first_global_sequence <= 0
            or result.last_global_sequence < result.first_global_sequence
            or not isinstance(result.event_ids, tuple)
        ):
            raise DurableJobError("rolling lifecycle command result is invalid")
        self.recover_pending()

    @property
    def database_path(self) -> Path:
        """Expose the exact durable authority for safe composition checks."""

        value = getattr(self._reaction_store, "database_path", None)
        if not isinstance(value, Path):
            raise DurableJobError("rolling reaction store lacks a typed database authority")
        return value

    def recover_pending(self) -> int:
        total = 0
        while True:
            pending = self._reaction_store.pending_rolling_reactions(
                limit=self._reaction_store.capacity.max_context_cards
            )
            if not pending:
                return total
            for obligation in pending:
                self._process(obligation)
                total += 1

    def derivation_authority(self, source_global_sequence: int) -> dict[str, Any]:
        """Expose only the verified durable output used by barrier reactions."""

        resolver = getattr(self._reaction_store, "rolling_derivation_authority", None)
        if not callable(resolver):
            raise DurableJobError("rolling reaction store lacks derivation authority")
        value = resolver(source_global_sequence)
        if not isinstance(value, dict):
            raise DurableJobError("rolling derivation authority differs")
        return value

    def _process(self, obligation: Mapping[str, Any]) -> None:
        events = tuple(
            self._events.event_at(sequence)
            for sequence in range(
                obligation["first_global_sequence"],
                obligation["last_global_sequence"] + 1,
            )
        )
        if tuple(str(item.event_id) for item in events) != obligation["event_ids"]:
            raise DurableJobError("rolling lifecycle event set differs from authority")
        completed_at = require_utc_milliseconds(self._clock())
        source_at = max(item.occurred_at_utc for item in events)
        if completed_at < source_at:
            raise DurableJobError("rolling reaction completion predates its source event")
        plan = self._resolver.resolve(events)
        if not isinstance(plan, RollingLifecycleReactionPlan):
            raise DurableJobError("rolling lifecycle resolver must return a typed plan")
        if plan.closed_epoch_ids:
            close_events = tuple(
                item
                for item in events
                if item.kind in {EventKind.ROUND_CLOSED, EventKind.TOURNAMENT_CLOSED}
            )
            if len(close_events) != 1:
                raise DurableJobError("rolling epoch reaction requires one close event")
            close_event = close_events[0]
            for epoch_id in plan.closed_epoch_ids:
                self._coordinator.close_epoch(
                    epoch_id,
                    source_event=close_event,
                    observed_at=completed_at,
                )
        timely_candidates = tuple(
            candidate for candidate in plan.candidates if completed_at < candidate.hard_deadline_at
        )
        expired_card_digests = tuple(
            candidate.key.card_digest
            for candidate in plan.candidates
            if completed_at >= candidate.hard_deadline_at
        )
        if timely_candidates:
            self._schedule_timely(timely_candidates, plan=plan, observed_at=completed_at)
        execution_value = {
            "schema_version": "strathmark-v3-rolling-lifecycle-execution-v1",
            "plan": plan.content_value(),
            "execution_authority": (
                {"mode": "legacy_non_executable"}
                if self._executable_council is None
                else self._executable_council.authority_value()
            ),
            "scheduled_card_digests": [item.key.card_digest for item in timely_candidates],
            "deadline_expired_card_digests": list(expired_card_digests),
        }
        self._reaction_store.complete_rolling_reaction(
            obligation["reaction_id"],
            plan_digest=canonical_digest(execution_value),
            scheduled_card_digests=tuple(item.key.card_digest for item in timely_candidates),
            completed_at=completed_at,
        )

    def _schedule_timely(
        self,
        candidates: tuple[PreparationCandidate, ...],
        *,
        plan: RollingLifecycleReactionPlan,
        observed_at: str,
    ) -> None:
        if self._executable_council is None:
            self._coordinator.schedule(
                candidates,
                capacity_use=plan.capacity_use,
                council_manifest_digest=plan.council_manifest_digest,
                observed_at=observed_at,
            )
            return
        self._coordinator.schedule_executable(
            candidates,
            capacity_use=plan.capacity_use,
            council_manifest_digest=plan.council_manifest_digest,
            promoted_council_authority=self._executable_council.authority,
            token_key=self._executable_council.token_key,
            member_deadlines=dict(self._executable_council.member_deadlines),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class CardDependency:
    result_recorded_at: str
    final_ready_at: str
    final_call_at: str

    def __post_init__(self) -> None:
        for value in (
            self.result_recorded_at,
            self.final_ready_at,
            self.final_call_at,
        ):
            require_utc_milliseconds(value)
        if not self.result_recorded_at <= self.final_ready_at <= self.final_call_at:
            raise DurableJobError("readiness timestamps must be monotonic")

    @property
    def within_result_to_ready_sla(self) -> bool:
        return _elapsed_ms(self.result_recorded_at, self.final_ready_at) <= 120_000

    @property
    def within_last_heat_to_final_window(self) -> bool:
        return _elapsed_ms(self.result_recorded_at, self.final_call_at) <= 300_000


def _card_content(values: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-capability-card-key-v1",
        "competitor_id": str(values["competitor_id"]),
        "target_context_digest": values["target_context_digest"],
        "historical_cutoff_key": str(values["historical_cutoff_key"]),
        "tournament_epoch_id": str(values["tournament_epoch_id"]),
        "bundle_digest": values["bundle_digest"],
        "evidence_digest": values["evidence_digest"],
        "dependency_revision": values["dependency_revision"],
    }


def _card_subject(key: CardKey) -> tuple[str, str, str, str]:
    """Identity for independently current rolling work across epochs and bundles."""

    return (
        str(key.competitor_id),
        key.target_context_digest,
        str(key.tournament_epoch_id),
        key.bundle_digest,
    )


def _card_key_from_dict(value: Mapping[str, Any]) -> CardKey:
    if not isinstance(value, Mapping):
        raise DurableJobError("rolling card key payload is not an object")
    key = CardKey.create(
        competitor_id=value["competitor_id"],
        target_context_digest=value["target_context_digest"],
        historical_cutoff_key=value["historical_cutoff_key"],
        tournament_epoch_id=value["tournament_epoch_id"],
        bundle_digest=value["bundle_digest"],
        evidence_digest=value["evidence_digest"],
        dependency_revision=value["dependency_revision"],
    )
    if key.to_dict() != dict(value):
        raise DurableJobError("rolling card key material differs")
    return key


def _require_digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DurableJobError(f"{label} digest must be lower-case SHA-256")
    return value


def _elapsed_ms(left: str, right: str) -> int:
    return int(
        (
            datetime.strptime(right, "%Y-%m-%dT%H:%M:%S.%fZ")
            - datetime.strptime(left, "%Y-%m-%dT%H:%M:%S.%fZ")
        ).total_seconds()
        * 1000
    )


__all__ = [
    "ClockPort",
    "CardDependency",
    "CardKey",
    "CompletedCard",
    "ContextDigestPort",
    "DurableCoordinator",
    "DurableRollingPreparationCoordinator",
    "ExecutableCouncilSchedule",
    "JobRepositoryPort",
    "ProviderFailure",
    "ProviderPort",
    "ProviderResponse",
    "PreparationCandidate",
    "PreparationClass",
    "PreparationPlan",
    "PublicationPort",
    "RunOutcome",
    "RollingPreparationPlanner",
    "RollingLifecycleReactionPlan",
    "RollingLifecycleReactionService",
    "RollingLifecycleResolverPort",
    "RollingReadiness",
]
