"""Append-only application lifecycle for candidate evaluation, promotion, and rollback."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.contracts.canonical import (
    canonical_decimal_string,
    canonical_digest,
    canonical_expected_versions,
)
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.factory.candidates import CandidateBundle
from strathmark.v3.factory.evaluator import EvaluationGate, SignedEvaluationReport
from strathmark.v3.infrastructure.artifacts import (
    ActivationPurpose,
    ArtifactError,
    BundleRepository,
    InstalledBundle,
)
from strathmark.v3.infrastructure.integrity import IntegrityError, P256Signer, verify_manifest
from strathmark.v3.infrastructure.sqlite.event_store import (
    EventStoreConflict,
    SQLiteEventStore,
    StoredCommandResult,
)

ZERO_BUNDLE_DIGEST = "0" * 64


class FactoryError(RuntimeError):
    """Model-factory authority or future-only lifecycle validation failed."""


@dataclass(frozen=True, slots=True)
class MonitoringObservation:
    window_id: str
    bundle_digest: str
    settled_evidence_digest: str
    policy_digest: str
    gates: tuple[EvaluationGate, ...]
    metrics: Mapping[str, float]
    failed_gates: tuple[str, ...]
    observation_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.window_id, str) or not self.window_id or len(self.window_id) > 128:
            raise FactoryError("monitoring window must be a bounded identity")
        for value, label in (
            (self.bundle_digest, "monitoring bundle"),
            (self.settled_evidence_digest, "monitoring evidence"),
            (self.policy_digest, "monitoring policy"),
            (self.observation_digest, "monitoring observation"),
        ):
            _digest(value, label)
        if not isinstance(self.gates, tuple) or self.gates != tuple(
            sorted(self.gates, key=lambda item: item.name)
        ):
            raise FactoryError("monitoring gates must be immutable and sorted")
        if len({item.name for item in self.gates}) != len(self.gates) or not self.gates:
            raise FactoryError("monitoring gates must be nonempty and unique")
        if not isinstance(self.metrics, Mapping) or tuple(self.metrics) != tuple(
            item.name for item in self.gates
        ):
            raise FactoryError("monitoring metrics must exactly cover frozen gates")
        failures = tuple(
            gate.name for gate in self.gates if not gate.passes(float(self.metrics[gate.name]))
        )
        if self.failed_gates != failures:
            raise FactoryError("monitoring failed-gate projection differs")
        if canonical_digest(self.body()) != self.observation_digest:
            raise FactoryError("monitoring observation digest differs")

    @classmethod
    def create(
        cls,
        *,
        window_id: str,
        bundle_digest: str,
        settled_evidence_digest: str,
        policy_digest: str,
        gates: tuple[EvaluationGate, ...],
        metrics: Mapping[str, float],
    ) -> MonitoringObservation:
        ordered = tuple(sorted(gates, key=lambda item: item.name))
        if tuple(sorted(metrics)) != tuple(item.name for item in ordered):
            raise FactoryError("monitoring metrics must exactly cover frozen gates")
        normalized: dict[str, float] = {}
        for name in sorted(metrics):
            value = metrics[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FactoryError("monitoring metric must be numeric")
            numeric = float(value)
            if numeric != numeric or numeric in {float("inf"), float("-inf")}:
                raise FactoryError("monitoring metric must be finite")
            normalized[name] = numeric
        shell = _MonitoringShell(
            window_id,
            bundle_digest,
            settled_evidence_digest,
            policy_digest,
            ordered,
            normalized,
        )
        failures = tuple(gate.name for gate in ordered if not gate.passes(normalized[gate.name]))
        return cls(
            window_id,
            bundle_digest,
            settled_evidence_digest,
            policy_digest,
            ordered,
            MappingProxyType(normalized),
            failures,
            canonical_digest(_monitoring_body(shell)),
        )

    @property
    def healthy(self) -> bool:
        return not self.failed_gates

    def body(self) -> dict[str, object]:
        return _monitoring_body(self)


@dataclass(frozen=True, slots=True)
class _MonitoringShell:
    window_id: str
    bundle_digest: str
    settled_evidence_digest: str
    policy_digest: str
    gates: tuple[EvaluationGate, ...]
    metrics: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class MonitoringReceipt:
    observation_digest: str
    monitored_bundle_digest: str
    active_bundle_digest: str
    rolled_back: bool
    first_global_sequence: int
    last_global_sequence: int
    result_digest: str


@dataclass(frozen=True, slots=True)
class FactoryRunOutcome:
    candidate_digest: str
    evaluation_passed: bool
    promoted: bool
    installed: InstalledBundle | None
    registration: StoredCommandResult
    evaluation: StoredCommandResult
    promotion: StoredCommandResult | None


class FactoryService:
    """Deterministic factory transitions over the shared V3 event authority."""

    def __init__(self, database_path: str | Path, *, repository: BundleRepository) -> None:
        if not isinstance(repository, BundleRepository):
            raise FactoryError("factory service requires an immutable bundle repository")
        self._events = SQLiteEventStore(database_path)
        self.repository = repository

    @property
    def event_store(self) -> SQLiteEventStore:
        return self._events

    @staticmethod
    def candidate_id(candidate: CandidateBundle) -> StableIdentifier:
        if not isinstance(candidate, CandidateBundle):
            raise FactoryError("candidate identity requires a closed candidate")
        return deterministic_identifier("bundle", {"candidate_digest": candidate.candidate_digest})

    def register_candidate(
        self,
        candidate: CandidateBundle,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        target = self.candidate_id(candidate)
        payload = {
            "schema_version": "strathmark-v3-factory-candidate-created-v1",
            "candidate_id": str(target),
            "candidate": candidate.manifest_value(include_display_name=True),
            "candidate_digest": candidate.candidate_digest,
            "lineage_digest": candidate.lineage_digest,
        }
        return self._execute(
            CommandKind.CREATE_MODEL_CANDIDATE,
            target,
            (EventIntent(AggregateKind.BUNDLE, target, EventKind.MODEL_CANDIDATE_CREATED),),
            payload,
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def run_candidate(
        self,
        candidate: CandidateBundle,
        report: SignedEvaluationReport,
        *,
        signer: P256Signer,
        request_identity: str,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> FactoryRunOutcome:
        """Record one frozen evaluation and automatically promote an exact passing bundle."""

        if (
            not isinstance(request_identity, str)
            or not request_identity
            or len(request_identity) > 128
        ):
            raise FactoryError("factory run request identity must be bounded")

        def command_id(action: str) -> IdempotencyKey:
            identifier = deterministic_identifier(
                "factory_command",
                {
                    "request_identity": request_identity,
                    "actor_id": str(actor_id),
                    "candidate_digest": candidate.candidate_digest,
                    "action": action,
                },
            )
            return IdempotencyKey(str(identifier))

        registration = self.register_candidate(
            candidate,
            command_id=command_id("register"),
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        evaluation = self.record_evaluation(
            candidate,
            report,
            command_id=command_id("evaluate"),
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        if not report.passed:
            return FactoryRunOutcome(
                candidate.candidate_digest,
                False,
                False,
                None,
                registration,
                evaluation,
                None,
            )
        installed = self.repository.publish(
            candidate, report, signer=signer, created_at=occurred_at_utc
        )
        promotion = self.promote(
            candidate,
            installed,
            command_id=command_id("promote"),
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        return FactoryRunOutcome(
            candidate.candidate_digest,
            True,
            True,
            installed,
            registration,
            evaluation,
            promotion,
        )

    def record_evaluation(
        self,
        candidate: CandidateBundle,
        report: SignedEvaluationReport,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        target = self.candidate_id(candidate)
        _verified_report_payload(self.repository, candidate, report)
        audit_id = deterministic_identifier(
            "audit_generation", {"generation_id": report.generation_id}
        )
        payload = {
            "schema_version": "strathmark-v3-factory-candidate-evaluated-v1",
            "candidate_id": str(target),
            "candidate_digest": candidate.candidate_digest,
            "lineage_digest": candidate.lineage_digest,
            "audit_generation_id": str(audit_id),
            "report": report.manifest.to_dict(),
            "report_digest": report.report_digest,
            "harness_digest": report.harness_digest,
            "passed": report.passed,
            "failed_gates": list(report.failed_gates),
        }
        return self._execute(
            CommandKind.EVALUATE_MODEL_CANDIDATE,
            target,
            (
                EventIntent(AggregateKind.BUNDLE, target, EventKind.MODEL_CANDIDATE_EVALUATED),
                EventIntent(
                    AggregateKind.AUDIT_GENERATION,
                    audit_id,
                    EventKind.AUDIT_GENERATION_CONSUMED,
                ),
            ),
            payload,
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def promote(
        self,
        candidate: CandidateBundle,
        installed: InstalledBundle,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        if not isinstance(installed, InstalledBundle):
            raise FactoryError("promotion requires an installed signed bundle")
        target = self.candidate_id(candidate)
        payload = {
            "schema_version": "strathmark-v3-factory-bundle-promotion-v1",
            "candidate_id": str(target),
            "candidate_digest": candidate.candidate_digest,
            "bundle_digest": installed.bundle_digest,
            "bundle_manifest_digest": installed.manifest.body_digest,
            "evaluator_report_digest": installed.evaluator_report_digest,
            "rollback_parent_digest": installed.rollback_parent_digest,
            "signer_key_id": installed.signer_key_id,
            "future_tournaments_only": True,
        }
        inline = InlinePayload.from_value(payload)
        retry = self._lookup_retry(
            command_id,
            actor_id,
            CommandKind.PROMOTE_BUNDLE,
            target,
            inline,
        )
        if retry is not None:
            return retry
        try:
            verified = self.repository.verify(
                installed.bundle_digest, purpose=ActivationPurpose.NEW_ACTIVATION
            )
        except ArtifactError as exc:
            raise FactoryError("promotion bundle is not installed and authorized") from exc
        if verified != installed or installed.candidate_digest != candidate.candidate_digest:
            raise FactoryError("promotion bundle differs from candidate or verified installation")
        evaluation = self._candidate_evaluation(target)
        if evaluation is None:
            raise FactoryError("candidate has no immutable evaluator result")
        if evaluation.get("report_digest") != installed.evaluator_report_digest:
            raise FactoryError("promotion evaluator report differs from installed bundle")
        if evaluation.get("passed") is not True:
            raise FactoryError("failed candidate cannot be promoted")
        active = self.active_bundle_digest()
        if installed.rollback_parent_digest != active:
            raise FactoryError("promotion rollback parent is not the current healthy champion")
        return self._execute_prebuilt(
            CommandKind.PROMOTE_BUNDLE,
            target,
            (EventIntent(AggregateKind.BUNDLE, target, EventKind.BUNDLE_PROMOTED),),
            inline,
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def active_bundle_digest(self) -> str:
        active = ZERO_BUNDLE_DIGEST
        for event in self._all_events():
            payload = _event_payload(event)
            if event.kind is EventKind.BUNDLE_PROMOTED:
                parent = payload.get("rollback_parent_digest")
                if parent != active:
                    raise FactoryError("promotion history does not descend from active champion")
                active = _digest(payload.get("bundle_digest"), "promoted bundle")
            elif event.kind is EventKind.BUNDLE_ROLLED_BACK:
                if payload.get("bundle_digest") != active:
                    raise FactoryError("rollback history does not target the active champion")
                active = _digest(payload.get("rollback_to_bundle_digest"), "rollback target")
        return active

    def bundle_for_tournament(self, tournament_id: StableIdentifier) -> InstalledBundle:
        require_identifier(tournament_id, expected_namespace="tournament")
        pinned: str | None = None
        for event in self._all_events():
            if event.aggregate_id == tournament_id and event.kind is EventKind.TOURNAMENT_OPENED:
                bundle_id = _event_payload(event).get("bundle_id")
                if not isinstance(bundle_id, str) or not bundle_id.startswith("bundle:"):
                    raise FactoryError("tournament open event has an invalid bundle pin")
                pinned = bundle_id.split(":", 1)[1]
        digest = pinned if pinned is not None else self.active_bundle_digest()
        if digest == ZERO_BUNDLE_DIGEST:
            raise FactoryError("no promoted bundle is available for an unopened tournament")
        purpose = (
            ActivationPurpose.PINNED_TOURNAMENT
            if pinned is not None
            else ActivationPurpose.NEW_TOURNAMENT
        )
        try:
            return self.repository.verify(digest, purpose=purpose)
        except ArtifactError as exc:
            raise FactoryError("tournament bundle pin is not installed or authorized") from exc

    def record_monitoring(
        self,
        observation: MonitoringObservation,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> MonitoringReceipt:
        if not isinstance(observation, MonitoringObservation):
            raise FactoryError("monitoring command requires a closed observation")
        try:
            installed = self.repository.verify(
                observation.bundle_digest, purpose=ActivationPurpose.HISTORICAL_VERIFY
            )
        except ArtifactError as exc:
            raise FactoryError("monitored bundle is not installed") from exc
        rollback_to = installed.rollback_parent_digest if not observation.healthy else None
        if rollback_to == ZERO_BUNDLE_DIGEST:
            raise FactoryError("unhealthy initial champion has no automatic rollback parent")
        target = deterministic_identifier(
            "monitoring",
            {
                "window_id": observation.window_id,
                "bundle_digest": observation.bundle_digest,
                "settled_evidence_digest": observation.settled_evidence_digest,
                "policy_digest": observation.policy_digest,
            },
        )
        payload = {
            "schema_version": "strathmark-v3-factory-monitoring-v1",
            "monitoring_id": str(target),
            "observation": observation.body(),
            "observation_digest": observation.observation_digest,
            "bundle_digest": observation.bundle_digest,
            "healthy": observation.healthy,
            "failed_gates": list(observation.failed_gates),
            "rollback_to_bundle_digest": rollback_to,
            "future_tournaments_only": True,
        }
        inline = InlinePayload.from_value(payload)
        retry = self._lookup_retry(
            command_id,
            actor_id,
            CommandKind.RECORD_MONITORING,
            target,
            inline,
        )
        if retry is not None:
            return _monitoring_receipt(retry)
        if self.active_bundle_digest() != observation.bundle_digest:
            raise FactoryError("monitoring observation does not target the active champion")
        intents = [EventIntent(AggregateKind.MONITORING, target, EventKind.MONITORING_RECORDED)]
        if rollback_to is not None:
            candidate_id = self._candidate_id_for_promoted_bundle(observation.bundle_digest)
            intents.append(
                EventIntent(AggregateKind.BUNDLE, candidate_id, EventKind.BUNDLE_ROLLED_BACK)
            )
        result = self._execute_prebuilt(
            CommandKind.RECORD_MONITORING,
            target,
            tuple(intents),
            inline,
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
            result={
                "schema_version": "strathmark-v3-factory-monitoring-result-v1",
                "observation_digest": observation.observation_digest,
                "monitored_bundle_digest": observation.bundle_digest,
                "active_bundle_digest": (
                    rollback_to if rollback_to is not None else observation.bundle_digest
                ),
                "rolled_back": rollback_to is not None,
            },
        )
        return _monitoring_receipt(result)

    def _execute(
        self,
        kind: CommandKind,
        target: StableIdentifier,
        intents: tuple[EventIntent, ...],
        payload: Mapping[str, Any],
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        inline = InlinePayload.from_value(payload)
        retry = self._lookup_retry(command_id, actor_id, kind, target, inline)
        if retry is not None:
            return retry
        return self._execute_prebuilt(
            kind,
            target,
            intents,
            inline,
            command_id,
            actor_id,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )

    def _execute_prebuilt(
        self,
        kind: CommandKind,
        target: StableIdentifier,
        intents: tuple[EventIntent, ...],
        inline: InlinePayload,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
        *,
        result: Mapping[str, Any] | None = None,
    ) -> StoredCommandResult:
        require_identifier(actor_id, expected_namespace="actor")
        versions = {
            str(intent.aggregate_id): (
                0
                if self._events.aggregate_head(str(intent.aggregate_id)) is None
                else self._events.aggregate_head(str(intent.aggregate_id))[0]
            )
            for intent in intents
        }
        command = CommandEnvelope(
            kind,
            command_id,
            target,
            canonical_expected_versions(versions),
            actor_id,
            inline,
        )
        result_value = result or {
            "schema_version": "strathmark-v3-factory-command-result-v1",
            "accepted": True,
            "target": str(target),
            "payload_digest": inline.digest,
        }
        request = CommandRequest(
            actor_id,
            command,
            intents,
            str(result_value["schema_version"]),
            result_value,
            occurred_at_utc,
            monotonic_elapsed_ms,
        )
        try:
            return self._events.execute(request)
        except EventStoreConflict:
            raced = self._lookup_retry(command_id, actor_id, kind, target, inline)
            if raced is not None:
                return raced
            raise

    def _lookup_retry(
        self,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        kind: CommandKind,
        target: StableIdentifier,
        payload: InlinePayload,
    ) -> StoredCommandResult | None:
        return self._events.lookup_exact_retry(
            principal_id=str(actor_id),
            idempotency_key=str(command_id),
            command_kind=kind,
            target_aggregate=str(target),
            payload_digest=payload.digest,
        )

    def _candidate_evaluation(self, candidate_id: StableIdentifier) -> Mapping[str, Any] | None:
        for event in reversed(self._all_events()):
            if event.aggregate_id == candidate_id and event.kind is (
                EventKind.MODEL_CANDIDATE_EVALUATED
            ):
                return _event_payload(event)
        return None

    def _candidate_id_for_promoted_bundle(self, bundle_digest: str) -> StableIdentifier:
        for event in reversed(self._all_events()):
            if (
                event.kind is EventKind.BUNDLE_PROMOTED
                and _event_payload(event).get("bundle_digest") == bundle_digest
            ):
                return event.aggregate_id
        raise FactoryError("active bundle has no promotion authority")

    def _all_events(self) -> tuple[EventEnvelope, ...]:
        return self._events.events()


def _monitoring_body(value: MonitoringObservation | _MonitoringShell) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-monitoring-observation-v1",
        "window_id": value.window_id,
        "bundle_digest": value.bundle_digest,
        "settled_evidence_digest": value.settled_evidence_digest,
        "policy_digest": value.policy_digest,
        "gates": [item.to_dict() for item in value.gates],
        "metrics": {name: canonical_decimal_string(value.metrics[name]) for name in value.metrics},
    }


def _verified_report_payload(
    repository: BundleRepository,
    candidate: CandidateBundle,
    report: SignedEvaluationReport,
) -> Mapping[str, Any]:
    if not isinstance(report, SignedEvaluationReport):
        raise FactoryError("candidate evaluation requires a typed signed report")
    try:
        payload = verify_manifest(report.manifest, repository.trust_policy.evaluator_trust_store)
    except IntegrityError as exc:
        raise FactoryError("candidate evaluation report signer is not trusted") from exc
    if (
        payload.get("candidate_digest") != candidate.candidate_digest
        or payload.get("candidate_lineage_digest") != candidate.lineage_digest
        or payload.get("generation_id") != report.generation_id
        or payload.get("harness_digest") != report.harness_digest
        or payload.get("passed") is not report.passed
        or payload.get("failed_gates") != list(report.failed_gates)
    ):
        raise FactoryError("candidate evaluation report binding differs")
    return payload


def _event_payload(event: EventEnvelope) -> Mapping[str, Any]:
    payload = event.command.payload
    if not isinstance(payload, InlinePayload):
        raise FactoryError("factory lifecycle event payload must remain bounded inline authority")
    value = json.loads(payload.canonical_json)
    if not isinstance(value, dict):
        raise FactoryError("factory lifecycle event payload is malformed")
    return value


def _monitoring_receipt(result: StoredCommandResult) -> MonitoringReceipt:
    value = result.value()
    expected = {
        "schema_version",
        "observation_digest",
        "monitored_bundle_digest",
        "active_bundle_digest",
        "rolled_back",
    }
    if set(value) != expected or value["schema_version"] != (
        "strathmark-v3-factory-monitoring-result-v1"
    ):
        raise FactoryError("stored monitoring result schema differs")
    if not isinstance(value["rolled_back"], bool):
        raise FactoryError("stored monitoring rollback state is not boolean")
    return MonitoringReceipt(
        _digest(value["observation_digest"], "monitoring observation"),
        _digest(value["monitored_bundle_digest"], "monitored bundle"),
        _digest(value["active_bundle_digest"], "active bundle"),
        value["rolled_back"],
        result.first_global_sequence,
        result.last_global_sequence,
        result.result_digest,
    )


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FactoryError(f"{label} digest must be lower-case SHA-256")
    return value


__all__ = [
    "FactoryError",
    "FactoryRunOutcome",
    "FactoryService",
    "MonitoringObservation",
    "MonitoringReceipt",
    "ZERO_BUNDLE_DIGEST",
]
