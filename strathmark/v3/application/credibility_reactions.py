"""Authority-bound settlement reactions for credibility and coverage ledgers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Callable, Mapping, Protocol, cast

from strathmark.v3.application.commands import CommandRequest, EventIntent
from strathmark.v3.application.lifecycle import LifecycleService
from strathmark.v3.assessors.base import AssessmentResult, FormulaInputPacket
from strathmark.v3.assessors.llm_council import (
    CandidateEvaluationReport,
    CandidateStatus,
)
from strathmark.v3.assessors.ml import MLAssessment
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.events import AggregateKind, EventEnvelope, EventKind
from strathmark.v3.contracts.evidence import (
    EvidencePacket,
    ResultObservation,
    TargetContext,
    require_utc_milliseconds,
)
from strathmark.v3.contracts.forecasts import (
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
)
from strathmark.v3.contracts.identifiers import (
    IdempotencyKey,
    StableIdentifier,
    deterministic_identifier,
    require_identifier,
)
from strathmark.v3.contracts.statuses import admit_raw_completion
from strathmark.v3.domain.credibility import (
    ConsequenceStatus,
    ContextNode,
    CredibilityLedger,
    CredibilityPolicy,
    HandicapConsequenceMetrics,
    LedgerReversal,
    LiveOverlay,
    Opportunity,
    OpportunityOutcome,
    OptimizerConsequenceReceipt,
    PredictiveMetrics,
    PredictiveScore,
    RoundWeightFreeze,
    ScoreScope,
    WeightComponent,
    WeightReceipt,
    calibrate_baseline,
    compute_predictive_metrics,
    freeze_live_round,
)
from strathmark.v3.domain.epochs import MandatoryReaction
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from strathmark.v3.infrastructure.sqlite.event_store import (
    EventStoreConflict,
    SQLiteEventStore,
    StoredCommandResult,
)

FORECAST_COMMIT_MANIFEST_KIND = "credibility_forecast_commit"
CREDIBILITY_POLICY_MANIFEST_KIND = "credibility_policy"
OPTIMIZER_EVALUATOR_AUTHORITY_MANIFEST_KIND = "optimizer_evaluator_authority"


class CredibilityReactionError(ValueError):
    """A settlement or optimizer receipt is not causally authoritative."""


@dataclass(frozen=True, slots=True)
class SealedCredibilityPolicy:
    manifest: SignedManifest

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest, SignedManifest)
            or self.manifest.kind != CREDIBILITY_POLICY_MANIFEST_KIND
        ):
            raise CredibilityReactionError("credibility policy manifest kind differs")


def seal_credibility_policy(
    policy: CredibilityPolicy,
    *,
    optimizer_bundle_digest: str,
    signer: P256Signer,
    created_at: str,
) -> SealedCredibilityPolicy:
    if not isinstance(policy, CredibilityPolicy):
        raise CredibilityReactionError("credibility policy must be frozen and typed")
    _digest(optimizer_bundle_digest, "optimizer bundle digest")
    value = {name: getattr(policy, name) for name in policy.__dataclass_fields__}
    return SealedCredibilityPolicy(
        sign_manifest(
            CREDIBILITY_POLICY_MANIFEST_KIND,
            {
                "schema_version": "strathmark-v3-credibility-policy-v1",
                "policy": value,
                "policy_digest": canonical_digest(value),
                "optimizer_bundle_digest": optimizer_bundle_digest,
            },
            signer=signer,
            created_at=created_at,
        )
    )


@dataclass(frozen=True, slots=True)
class SealedOptimizerEvaluatorAuthority:
    manifest: SignedManifest

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest, SignedManifest)
            or self.manifest.kind != OPTIMIZER_EVALUATOR_AUTHORITY_MANIFEST_KIND
        ):
            raise CredibilityReactionError("optimizer evaluator authority manifest kind differs")


@dataclass(frozen=True, slots=True)
class InstalledOptimizerEvaluator:
    """A raw evaluator plus the signed authority U14 must install around it."""

    evaluator: OptimizerConsequenceEvaluatorPort
    authority: SealedOptimizerEvaluatorAuthority


def seal_optimizer_evaluator_authority(
    evaluator: OptimizerConsequenceEvaluatorPort,
    *,
    signer: P256Signer,
    created_at: str,
) -> SealedOptimizerEvaluatorAuthority:
    bundle_digest = getattr(evaluator, "bundle_digest", None)
    implementation_digest = getattr(evaluator, "implementation_digest", None)
    evaluator_port = getattr(evaluator, "evaluator_port", None)
    _digest(bundle_digest, "optimizer evaluator bundle digest")
    _digest(implementation_digest, "optimizer evaluator implementation digest")
    if evaluator_port != "shared_optimizer_evaluator_v1" or not hasattr(evaluator, "evaluate"):
        raise CredibilityReactionError("optimizer evaluator does not implement the installed port")
    return SealedOptimizerEvaluatorAuthority(
        sign_manifest(
            OPTIMIZER_EVALUATOR_AUTHORITY_MANIFEST_KIND,
            {
                "schema_version": "strathmark-v3-optimizer-evaluator-authority-v1",
                "evaluator_port": evaluator_port,
                "optimizer_bundle_digest": bundle_digest,
                "implementation_digest": implementation_digest,
            },
            signer=signer,
            created_at=created_at,
        )
    )


@dataclass(frozen=True, slots=True)
class SealedForecastCommit:
    manifest: SignedManifest

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, SignedManifest):
            raise CredibilityReactionError("forecast commit requires a signed manifest")
        if self.manifest.kind != FORECAST_COMMIT_MANIFEST_KIND:
            raise CredibilityReactionError("forecast commit manifest kind differs")


def seal_forecast_commit(
    forecast: AssessorForecast,
    *,
    evidence_packet: EvidencePacket,
    assessor_input: FormulaInputPacket | None,
    assessor_receipt: AssessmentResult | MLAssessment,
    field_id: StableIdentifier,
    competitor_id: StableIdentifier,
    field_revision: int,
    evidence_epoch_id: StableIdentifier,
    evidence_epoch_digest: str,
    historical_cutoff_key: str,
    receipt_id: StableIdentifier,
    issue_event_digest: str,
    member_id: str | None = None,
    operational_promotion_digest: str | None = None,
    execution_failure_kind: str | None = None,
    signer: P256Signer,
    created_at: str,
) -> SealedForecastCommit:
    if not isinstance(forecast, AssessorForecast):
        raise CredibilityReactionError("forecast commit requires a typed sealed forecast")
    if not isinstance(evidence_packet, EvidencePacket):
        raise CredibilityReactionError("forecast commit requires a canonical evidence packet")
    if not isinstance(assessor_receipt, (AssessmentResult, MLAssessment)):
        raise CredibilityReactionError("forecast commit requires a typed assessor receipt")
    if assessor_receipt.forecast != forecast:
        raise CredibilityReactionError("assessor receipt does not bind the forecast")
    if forecast.assessor is AssessorKind.FORMULA:
        if not isinstance(assessor_input, FormulaInputPacket):
            raise CredibilityReactionError("Formula forecast requires its governor input receipt")
        if assessor_input.evidence != evidence_packet:
            raise CredibilityReactionError("Formula input does not bind the evidence packet")
        if forecast.evidence_digest != assessor_input.digest:
            raise CredibilityReactionError("Formula forecast does not bind its exact input")
        if not isinstance(assessor_receipt, AssessmentResult):
            raise CredibilityReactionError("Formula forecast requires its assessment receipt")
    elif forecast.assessor is AssessorKind.ML:
        if assessor_input is not None or not isinstance(assessor_receipt, MLAssessment):
            raise CredibilityReactionError("ML forecast requires its ML assessment receipt")
        if forecast.evidence_digest != evidence_packet.content_digest:
            raise CredibilityReactionError("ML forecast does not bind its evidence packet")
    else:
        if assessor_input is not None:
            raise CredibilityReactionError("non-Formula forecast cannot carry Formula input")
    require_identifier(field_id, expected_namespace="field")
    require_identifier(competitor_id, expected_namespace="competitor")
    require_identifier(evidence_epoch_id, expected_namespace="epoch")
    require_identifier(historical_cutoff_key, expected_namespace="history")
    require_identifier(receipt_id, expected_namespace="receipt")
    _positive_int(field_revision, "field revision")
    _digest(evidence_epoch_digest, "evidence epoch digest")
    _digest(issue_event_digest, "issue event digest")
    if forecast.assessor is AssessorKind.LLM_MEMBER:
        if not isinstance(member_id, str) or not member_id:
            raise CredibilityReactionError("LLM member forecast requires a member identity")
    elif member_id is not None:
        raise CredibilityReactionError("outer assessor forecast cannot carry a member identity")
    if operational_promotion_digest is not None:
        _digest(operational_promotion_digest, "operational promotion digest")
    failure_kinds = {"transport_failure", "runtime_failure", "deadline_miss"}
    if forecast.state is ForecastState.ABSTAINED and forecast.abstention_code not in {
        "insufficient_support"
    }:
        raise CredibilityReactionError("forecast reason is not a principled model abstention")
    if execution_failure_kind is not None:
        if (
            execution_failure_kind not in failure_kinds
            or forecast.state is not ForecastState.INVALID
            or forecast.abstention_code != execution_failure_kind
        ):
            raise CredibilityReactionError(
                "trusted execution failure must match the sealed invalid forecast state"
            )
    elif forecast.state is ForecastState.INVALID and forecast.abstention_code in failure_kinds:
        raise CredibilityReactionError("execution failure cannot be relabelled schema-invalid")
    elif forecast.state is ForecastState.INVALID and forecast.abstention_code != "schema_invalid":
        raise CredibilityReactionError("invalid forecast reason is outside the closed taxonomy")
    return SealedForecastCommit(
        sign_manifest(
            FORECAST_COMMIT_MANIFEST_KIND,
            {
                "schema_version": "strathmark-v3-credibility-forecast-commit-v1",
                "field_id": str(field_id),
                "competitor_id": str(competitor_id),
                "field_revision": field_revision,
                "evidence_epoch_id": str(evidence_epoch_id),
                "evidence_epoch_digest": evidence_epoch_digest,
                "historical_cutoff_key": historical_cutoff_key,
                "receipt_id": str(receipt_id),
                "issue_event_digest": issue_event_digest,
                "member_id": member_id,
                "operational_promotion_digest": operational_promotion_digest,
                "execution_failure_kind": execution_failure_kind,
                "evidence_packet": evidence_packet.to_dict(),
                "assessor_input": (None if assessor_input is None else assessor_input.to_dict()),
                "assessor_receipt": assessor_receipt.to_dict(),
                "forecast": forecast.to_dict(),
            },
            signer=signer,
            created_at=created_at,
        )
    )


@dataclass(frozen=True, slots=True)
class SettledFieldResult:
    competitor_id: str
    result_id: str
    result_revision: int
    result_revision_digest: str
    source_sequence: int
    status: str
    raw_time_ms: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "competitor_id": self.competitor_id,
            "result_id": self.result_id,
            "result_revision": self.result_revision,
            "result_revision_digest": self.result_revision_digest,
            "source_sequence": self.source_sequence,
            "status": self.status,
            "raw_time_ms": self.raw_time_ms,
        }


@dataclass(frozen=True, slots=True)
class FieldForecastCard:
    competitor_id: str
    member_id: str | None
    forecast: AssessorForecast

    def to_dict(self) -> dict[str, object]:
        return {
            "competitor_id": self.competitor_id,
            "member_id": self.member_id,
            "forecast": self.forecast.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class OptimizerScoringInput:
    """Exact U5-authority input sent outward to the shared U14 evaluator."""

    tournament_id: str
    round_id: str
    field_id: str
    competitor_id: str
    result_id: str
    result_revision: int
    result_revision_digest: str
    source_sequence: int
    issued_field_members: tuple[str, ...]
    issued_marks: tuple[tuple[str, int], ...]
    field_results: tuple[SettledFieldResult, ...]
    field_forecasts: tuple[FieldForecastCard, ...]
    field_receipt_digest: str
    optimizer_bundle_digest: str
    credibility_policy_digest: str
    raw_time_ms: int
    context: ContextNode
    robust_context_scale_ms: int
    evidence_weight: str
    scoring_input_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.field_results, tuple)
            or not all(isinstance(item, SettledFieldResult) for item in self.field_results)
            or tuple(item.competitor_id for item in self.field_results) != self.issued_field_members
        ):
            raise CredibilityReactionError(
                "optimizer scoring requires one ordered terminal result per issued member"
            )
        if not isinstance(self.field_forecasts, tuple) or not all(
            isinstance(item, FieldForecastCard) for item in self.field_forecasts
        ):
            raise CredibilityReactionError("optimizer scoring requires typed field forecast cards")
        content = self.content_value()
        if self.scoring_input_digest != canonical_digest(content):
            raise CredibilityReactionError("optimizer scoring input digest mismatch")

    def content_value(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-optimizer-scoring-input-v1",
            "tournament_id": self.tournament_id,
            "round_id": self.round_id,
            "field_id": self.field_id,
            "competitor_id": self.competitor_id,
            "result_id": self.result_id,
            "result_revision": self.result_revision,
            "result_revision_digest": self.result_revision_digest,
            "source_sequence": self.source_sequence,
            "issued_field_members": self.issued_field_members,
            "issued_marks": self.issued_marks,
            "field_results": [item.to_dict() for item in self.field_results],
            "field_forecasts": [item.to_dict() for item in self.field_forecasts],
            "field_receipt_digest": self.field_receipt_digest,
            "optimizer_bundle_digest": self.optimizer_bundle_digest,
            "credibility_policy_digest": self.credibility_policy_digest,
            "raw_time_ms": self.raw_time_ms,
            "context": self.context.to_dict(),
            "robust_context_scale_ms": self.robust_context_scale_ms,
            "evidence_weight": self.evidence_weight,
        }


class OptimizerConsequenceEvaluatorPort(Protocol):
    bundle_digest: str
    implementation_digest: str
    evaluator_port: str

    def evaluate(
        self, *, forecast: AssessorForecast, scoring_input: OptimizerScoringInput
    ) -> OptimizerConsequenceReceipt: ...


class SQLiteCredibilityReactionService:
    """Production U12 authority: derive credibility only from the verified U5 ledger."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        trust_store: IntegrityTrustStore,
        consequence_evaluator: InstalledOptimizerEvaluator
        | OptimizerConsequenceEvaluatorPort
        | None,
        policy_manifest: SealedCredibilityPolicy,
    ) -> None:
        if not isinstance(trust_store, IntegrityTrustStore):
            raise CredibilityReactionError("SQLite credibility requires pinned P-256 trust")
        if not isinstance(policy_manifest, SealedCredibilityPolicy):
            raise CredibilityReactionError("SQLite credibility requires a sealed policy")
        try:
            policy_payload = dict(verify_manifest(policy_manifest.manifest, trust_store))
        except IntegrityError as exc:
            raise CredibilityReactionError("credibility policy signature is untrusted") from exc
        if (
            set(policy_payload)
            != {
                "schema_version",
                "policy",
                "policy_digest",
                "optimizer_bundle_digest",
            }
            or policy_payload["schema_version"] != "strathmark-v3-credibility-policy-v1"
        ):
            raise CredibilityReactionError("credibility policy manifest is not closed")
        policy_value = policy_payload["policy"]
        if (
            not isinstance(policy_value, Mapping)
            or canonical_digest(policy_value) != policy_payload["policy_digest"]
        ):
            raise CredibilityReactionError("credibility policy authority binding differs")
        bundle_digest = policy_payload["optimizer_bundle_digest"]
        _digest(bundle_digest, "optimizer policy bundle digest")
        installed_evaluator: OptimizerConsequenceEvaluatorPort | None = None
        diagnostic_evaluator: OptimizerConsequenceEvaluatorPort | None = None
        optimizer_authority_digest: str | None = None
        if consequence_evaluator is not None:
            installed = isinstance(consequence_evaluator, InstalledOptimizerEvaluator)
            evaluator = consequence_evaluator.evaluator if installed else consequence_evaluator
            if getattr(
                evaluator, "evaluator_port", None
            ) != "shared_optimizer_evaluator_v1" or not hasattr(evaluator, "evaluate"):
                raise CredibilityReactionError("optimizer evaluator does not implement the port")
            diagnostic_evaluator = evaluator
            if not installed:
                evaluator = None
            else:
                evaluator = consequence_evaluator.evaluator
        if isinstance(consequence_evaluator, InstalledOptimizerEvaluator):
            try:
                authority = dict(
                    verify_manifest(consequence_evaluator.authority.manifest, trust_store)
                )
            except IntegrityError as exc:
                raise CredibilityReactionError(
                    "optimizer evaluator authority signature is untrusted"
                ) from exc
            if set(authority) != {
                "schema_version",
                "evaluator_port",
                "optimizer_bundle_digest",
                "implementation_digest",
            } or authority["schema_version"] != ("strathmark-v3-optimizer-evaluator-authority-v1"):
                raise CredibilityReactionError("optimizer evaluator authority is not closed")
            if (
                authority["evaluator_port"] != "shared_optimizer_evaluator_v1"
                or authority["optimizer_bundle_digest"] != bundle_digest
                or authority["optimizer_bundle_digest"] != getattr(evaluator, "bundle_digest", None)
                or authority["implementation_digest"]
                != getattr(evaluator, "implementation_digest", None)
                or getattr(evaluator, "evaluator_port", None) != "shared_optimizer_evaluator_v1"
                or not hasattr(evaluator, "evaluate")
            ):
                raise CredibilityReactionError("optimizer evaluator installation binding differs")
            installed_evaluator = evaluator
            optimizer_authority_digest = consequence_evaluator.authority.manifest.body_digest
        try:
            policy = CredibilityPolicy(**policy_value)
        except Exception as exc:
            raise CredibilityReactionError("credibility policy payload is invalid") from exc
        self._events = SQLiteEventStore(database_path)
        self._trust_store = trust_store
        self._evaluator = installed_evaluator
        self._diagnostic_evaluator = diagnostic_evaluator
        self._optimizer_authority_digest = optimizer_authority_digest
        self._optimizer_bundle_digest = bundle_digest
        self._policy = policy
        self._policy_digest = str(policy_payload["policy_digest"])
        self._policy_manifest_digest = policy_manifest.manifest.body_digest
        self._component_digest = canonical_digest(policy_payload)
        self._verify_persisted_policy()

    @property
    def database_path(self) -> Path:
        return self._events.database_path

    @property
    def component_digest(self) -> str:
        """Bundle component identity for the complete verified policy authority."""

        return self._component_digest

    def _verify_persisted_policy(self) -> None:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? "
                "AND event_kind=? ORDER BY global_sequence",
                (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
            ).fetchall()
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            payload = cast(InlinePayload, event.command.payload).to_value()
            persisted = None
            if payload.get("schema_version") == "strathmark-v3-credibility-weights-event-v1":
                persisted = payload.get("policy_digest")
            elif payload.get("schema_version") == "strathmark-v3-tournament-baseline-snapshot-v1":
                receipt = payload.get("receipt")
                persisted = receipt.get("policy_digest") if isinstance(receipt, Mapping) else None
            if persisted is not None and persisted != self._policy_digest:
                raise CredibilityReactionError(
                    "persisted credibility policy differs from restart authority"
                )

    def commit_forecast(
        self,
        sealed: SealedForecastCommit,
        *,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        payload, forecast, packet = self._verify_forecast_manifest(sealed)
        scoring_parameters = self._verify_commit_causality(payload, forecast, packet)
        event_kind = (
            EventKind.COMPONENT_FORECAST_COMMITTED
            if forecast.state is ForecastState.COMMITTED
            else EventKind.COMPONENT_FORECAST_REJECTED
        )
        try:
            return self._append_event(
                command_kind=CommandKind.COMMIT_FORECAST,
                event_kind=event_kind,
                aggregate_kind=AggregateKind.FORECAST,
                aggregate_id=forecast.forecast_id,
                payload={
                    "schema_version": "strathmark-v3-forecast-authority-event-v1",
                    "sealed_manifest": sealed.manifest.to_dict(),
                    "authority_scoring_parameters": scoring_parameters,
                },
                result={"forecast_id": str(forecast.forecast_id), "committed": True},
                command_id=command_id,
                actor_id=actor_id,
                occurred_at_utc=occurred_at_utc,
                monotonic_elapsed_ms=monotonic_elapsed_ms,
            )
        except EventStoreConflict as exc:
            raise CredibilityReactionError(
                "duplicate or conflicting assessor forecast authority"
            ) from exc

    def commit_candidate_evaluation(
        self,
        report: CandidateEvaluationReport,
        *,
        evidence_packet: EvidencePacket,
        field_id: StableIdentifier,
        competitor_id: StableIdentifier,
        field_revision: int,
        evidence_epoch_id: StableIdentifier,
        evidence_epoch_digest: str,
        historical_cutoff_key: str,
        receipt_id: StableIdentifier,
        issue_event_digest: str,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> tuple[StoredCommandResult, ...]:
        """Persist U11 test-ephemeral diagnostics only into the candidate ledger."""

        if (
            not isinstance(report, CandidateEvaluationReport)
            or report.candidate_status is not CandidateStatus.CANDIDATE
            or report.authority_class != "test_ephemeral"
        ):
            raise CredibilityReactionError("candidate evaluation lacks U11 candidate authority")
        base = {
            "field_id": str(field_id),
            "competitor_id": str(competitor_id),
            "field_revision": field_revision,
            "evidence_epoch_id": str(evidence_epoch_id),
            "evidence_epoch_digest": evidence_epoch_digest,
            "historical_cutoff_key": historical_cutoff_key,
            "receipt_id": str(receipt_id),
            "issue_event_digest": issue_event_digest,
            "operational_promotion_digest": None,
            "assessor_input": None,
        }
        report_value = _candidate_report_value(report)
        report_digest = canonical_digest(report_value)
        support = _support_from_packet(evidence_packet)
        candidates: list[tuple[str | None, AssessorForecast, str | None]] = []
        for outcome in report.outcomes:
            if outcome.evidence_digest != evidence_packet.content_digest:
                raise CredibilityReactionError("candidate member evidence packet differs")
            distribution = outcome.valid_distribution
            failure = None
            state = ForecastState.COMMITTED
            abstention = None
            if distribution is None:
                if outcome.validated is not None and outcome.validated.abstained:
                    state = ForecastState.ABSTAINED
                    abstention = "insufficient_support"
                else:
                    state = ForecastState.INVALID
                    failure = _candidate_failure_kind(outcome.unavailable_code)
                    abstention = failure or "schema_invalid"
            forecast = AssessorForecast.create(
                forecast_id=deterministic_identifier(
                    "forecast",
                    {
                        "candidate_report_digest": report_digest,
                        "member_id": outcome.member_id,
                    },
                ),
                assessor=AssessorKind.LLM_MEMBER,
                state=state,
                evidence_digest=evidence_packet.content_digest,
                distribution=distribution,
                support=support,
                warnings=(),
                artifacts=outcome.artifacts,
                abstention_code=abstention,
            )
            candidates.append((outcome.member_id, forecast, failure))
        council_state = (
            ForecastState.COMMITTED
            if report.diagnostic_distribution is not None
            else ForecastState.INVALID
        )
        council = AssessorForecast.create(
            forecast_id=deterministic_identifier(
                "forecast",
                {"candidate_report_digest": report_digest, "outer": "council"},
            ),
            assessor=AssessorKind.LLM_COUNCIL,
            state=council_state,
            evidence_digest=evidence_packet.content_digest,
            distribution=report.diagnostic_distribution,
            support=support,
            warnings=(),
            artifacts=(),
            abstention_code=(
                None if council_state is ForecastState.COMMITTED else "schema_invalid"
            ),
        )
        candidates.append((None, council, None))
        results = []
        for member_id, forecast, failure in candidates:
            payload = {
                "schema_version": "strathmark-v3-candidate-diagnostic-event-v1",
                **base,
                "member_id": member_id,
                "execution_failure_kind": failure,
                "evidence_packet": evidence_packet.to_dict(),
                "candidate_report": report_value,
                "candidate_report_digest": report_digest,
                "authority_scoring_parameters": self._authority_scoring_parameters(evidence_packet),
                "forecast": forecast.to_dict(),
            }
            self._verify_candidate_causality(payload, forecast, evidence_packet)
            key = IdempotencyKey(
                f"command:{canonical_digest({'base': str(command_id), 'forecast': forecast.commit_digest})}"
            )
            results.append(
                self._append_event(
                    command_kind=CommandKind.COMMIT_FORECAST,
                    event_kind=(
                        EventKind.COMPONENT_FORECAST_COMMITTED
                        if forecast.state is ForecastState.COMMITTED
                        else EventKind.COMPONENT_FORECAST_REJECTED
                    ),
                    aggregate_kind=AggregateKind.FORECAST,
                    aggregate_id=forecast.forecast_id,
                    payload=payload,
                    result={
                        "forecast_id": str(forecast.forecast_id),
                        "candidate": True,
                    },
                    command_id=key,
                    actor_id=actor_id,
                    occurred_at_utc=occurred_at_utc,
                    monotonic_elapsed_ms=monotonic_elapsed_ms,
                )
            )
        return tuple(results)

    def react_result(
        self,
        result_id: StableIdentifier,
        *,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
        fault_hook: Callable[[str], None] | None = None,
    ) -> tuple[CredibilityLedger, WeightReceipt]:
        require_identifier(result_id, expected_namespace="result")
        require_utc_milliseconds(occurred_at_utc)
        row, observation, issue_event, issue_payload = self._active_settled_result(result_id)
        source_sequence = int(row[2])
        result_revision = int(row[1])
        admitted = admit_raw_completion(observation.result) if bool(row[5]) else None
        field_results = self._complete_field_results(
            str(observation.field_id), issue_payload, int(row[8])
        )
        field_forecasts = self._field_forecast_cards(
            str(observation.field_id), issue_payload, source_sequence
        )
        ledger = self.load_ledger()
        if self._credibility_reaction_complete(source_sequence):
            return ledger, self._load_weight_receipt(source_sequence)
        ledger = self._append_prior_reversals(
            ledger,
            result_id=str(result_id),
            replacement_revision=result_revision,
            source_sequence=source_sequence,
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        if admitted is not None:
            commits = self._eligible_forecasts(
                observation=observation,
                result_source_sequence=source_sequence,
                issue_event=issue_event,
                issue_payload=issue_payload,
            )
            by_assessor = {item[1].assessor: item for item in commits}
            for assessor in (AssessorKind.FORMULA, AssessorKind.ML):
                if assessor not in by_assessor:
                    ledger = self._append_missing_opportunity(
                        ledger,
                        result_id=str(result_id),
                        assessor=assessor,
                        observation=observation,
                        result_revision=result_revision,
                        source_sequence=source_sequence,
                        issue_event=issue_event,
                        actor_id=actor_id,
                        occurred_at_utc=occurred_at_utc,
                        monotonic_elapsed_ms=monotonic_elapsed_ms,
                    )
            for payload, forecast, _event in commits:
                ledger = self._append_forecast_outcome(
                    ledger,
                    result_id=str(result_id),
                    payload=payload,
                    forecast=forecast,
                    observation=observation,
                    result_revision=result_revision,
                    result_revision_digest=str(row[4]),
                    source_sequence=source_sequence,
                    issue_event=issue_event,
                    issue_payload=issue_payload,
                    field_results=field_results,
                    field_forecasts=field_forecasts,
                    settled_at_utc=str(row[11]),
                    actor_id=actor_id,
                    occurred_at_utc=occurred_at_utc,
                    monotonic_elapsed_ms=monotonic_elapsed_ms,
                )
        context = _context_from_observation(observation, history_count=0)
        ledger = self.load_ledger()
        if fault_hook is not None:
            fault_hook("after_scoring")
        weights = self.baseline_weights(context, calibration_cutoff_at_utc=str(row[11]))
        self._append_weights(
            weights,
            tournament_id=observation.tournament_id,
            source_sequence=source_sequence,
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        if fault_hook is not None:
            fault_hook("after_weights")
        credibility_digest = canonical_digest(
            {
                "ledger_projection_digest": ledger.current_projection_digest,
                "weight_receipt_digest": weights.receipt_digest,
                "source_sequence": source_sequence,
            }
        )
        lifecycle = LifecycleService(self.database_path)
        lifecycle.complete_derivation_reaction(
            source_sequence,
            MandatoryReaction.CREDIBILITY,
            credibility_digest,
            command_id=IdempotencyKey(
                f"command:{canonical_digest({'source': source_sequence, 'reaction': 'credibility', 'output': credibility_digest})}"
            ),
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        return self.load_ledger(), weights

    def _credibility_reaction_complete(self, source_sequence: int) -> bool:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT state FROM v3_derivation_reactions WHERE source_global_sequence=? "
                "AND reaction_type=?",
                (source_sequence, MandatoryReaction.CREDIBILITY.value),
            ).fetchone()
        return row is not None and str(row[0]) == "completed"

    def record_live_control(
        self,
        tournament_id: StableIdentifier,
        *,
        action: str,
        reason: str,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        require_identifier(tournament_id, expected_namespace="tournament")
        if not isinstance(reason, str) or not reason.strip():
            raise CredibilityReactionError("live control requires an explicit reason")
        mapping = {
            "suspend": (CommandKind.SUSPEND_LIVE, EventKind.LIVE_SUSPENDED),
            "re_enable": (CommandKind.RESUME_LIVE, EventKind.LIVE_RESUMED),
            "emergency_stop": (CommandKind.EMERGENCY_STOP, EventKind.EMERGENCY_STOPPED),
        }
        try:
            command_kind, event_kind = mapping[action]
        except KeyError as exc:
            raise CredibilityReactionError("live control action is unknown") from exc
        target = deterministic_identifier("weights", {"tournament_id": str(tournament_id)})
        retry = self._load_live_control_retry(
            target=target,
            tournament_id=tournament_id,
            action=action,
            reason=reason.strip(),
            command_kind=command_kind,
            command_id=command_id,
            actor_id=actor_id,
        )
        if retry is not None:
            return retry
        self._require_open_tournament(tournament_id)
        before = self._load_live_control_state(target, tournament_id)
        after = dict(before)
        if action == "suspend":
            after["suspended"] = True
        elif action == "re_enable":
            after.update(enabled=True, suspended=False, emergency_stopped=False)
        else:
            after.update(enabled=False, emergency_stopped=True)
        before_digest = canonical_digest(before)
        after_digest = canonical_digest(after)
        payload = {
            "schema_version": "strathmark-v3-live-credibility-control-v1",
            "tournament_id": str(tournament_id),
            "action": action,
            "reason": reason.strip(),
            "before_digest": before_digest,
            "after_digest": after_digest,
            "before": before,
            "after": after,
        }
        try:
            return self._append_event(
                command_kind=command_kind,
                event_kind=event_kind,
                aggregate_kind=AggregateKind.WEIGHTS,
                aggregate_id=target,
                payload=payload,
                result={"action": action, "after_digest": after_digest},
                command_id=command_id,
                actor_id=actor_id,
                occurred_at_utc=occurred_at_utc,
                monotonic_elapsed_ms=monotonic_elapsed_ms,
                projection_hook=self._live_control_guard(
                    target=target,
                    tournament_id=tournament_id,
                    payload=payload,
                ),
            )
        except EventStoreConflict as exc:
            raise CredibilityReactionError(
                "live control conflicted with concurrent authority"
            ) from exc

    def freeze_live_weights(
        self,
        completed_round_id: StableIdentifier,
        next_round_id: StableIdentifier,
        *,
        context: ContextNode,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> LiveOverlay:
        """Persist one complete-round live freeze derived from U5 lifecycle authority."""

        require_identifier(completed_round_id, expected_namespace="round")
        require_identifier(next_round_id, expected_namespace="round")
        if not isinstance(context, ContextNode):
            raise CredibilityReactionError("live freeze requires a typed target context")
        with open_v3_connection(self.database_path, read_only=True) as connection:
            round_rows = connection.execute(
                "SELECT entity_id, tournament_id, snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='round' AND entity_id IN (?, ?) "
                "ORDER BY upstream_revision DESC",
                (str(completed_round_id), str(next_round_id)),
            ).fetchall()
            closed = connection.execute(
                "SELECT global_sequence, event_digest, occurred_at_utc FROM v3_events "
                "WHERE aggregate_id=? "
                "AND event_kind=? ORDER BY global_sequence DESC LIMIT 1",
                (str(completed_round_id), EventKind.ROUND_CLOSED.value),
            ).fetchone()
            epoch = connection.execute(
                "SELECT epoch_id, epoch_digest, maximum_tournament_sequence "
                "FROM v3_evidence_epochs WHERE round_id=? ORDER BY epoch_revision DESC LIMIT 1",
                (str(next_round_id),),
            ).fetchone()
            issued_next = connection.execute(
                "SELECT 1 FROM v3_ingress_snapshots snapshot JOIN v3_events event "
                "ON event.aggregate_id=snapshot.entity_id WHERE snapshot.entity_kind='field' "
                "AND snapshot.round_id=? AND event.event_kind=? LIMIT 1",
                (str(next_round_id), EventKind.FIELD_ISSUED.value),
            ).fetchone()
        authority = {str(row[0]): str(row[1]) for row in round_rows}
        if set(authority) != {str(completed_round_id), str(next_round_id)}:
            raise CredibilityReactionError("live freeze round identifiers lack ingress authority")
        tournament_ids = set(authority.values())
        if len(tournament_ids) != 1 or closed is None or epoch is None:
            raise CredibilityReactionError("live freeze requires one closed round and next epoch")
        if int(epoch[2]) < int(closed[0]):
            raise CredibilityReactionError("next-round epoch predates completed-round closure")
        next_snapshot = next(
            json.loads(str(row[2])) for row in round_rows if str(row[0]) == str(next_round_id)
        )
        if str(completed_round_id) not in next_snapshot["predecessor_round_ids"]:
            raise CredibilityReactionError("completed round is not a declared predecessor")
        tournament_id = StableIdentifier(tournament_ids.pop())
        self._require_open_tournament(tournament_id)
        target = deterministic_identifier("weights", {"tournament_id": str(tournament_id)})
        baseline = self._tournament_baseline(
            tournament_id,
            context,
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        existing = self._load_round_freeze(next_round_id)
        if existing is not None:
            event, payload = existing
            if str(event.command.command_id) != str(command_id):
                raise CredibilityReactionError("next-round live weights are already frozen")
            if payload["context"] != context.to_dict():
                raise CredibilityReactionError("exact live-freeze retry changed target context")
            if payload.get("completed_round_id") != str(completed_round_id) or payload.get(
                "tournament_id"
            ) != str(tournament_id):
                raise CredibilityReactionError("exact live-freeze retry changed round authority")
            weights = tuple((AssessorKind(item), value) for item, value in payload["weights"])
            state = payload["control_state"]
            return LiveOverlay(
                str(tournament_id),
                baseline,
                weights,
                enabled=bool(state["enabled"]),
                suspended=bool(state["suspended"]),
                emergency_stopped=bool(state["emergency_stopped"]),
                expired=bool(state["expired"]),
                rounds=(
                    RoundWeightFreeze(
                        str(next_round_id),
                        str(completed_round_id),
                        weights,
                        payload["influence"],
                    ),
                ),
            )
        if issued_next is not None:
            raise CredibilityReactionError("live weights cannot change after next field issue")
        state = self._load_live_control_state(target, tournament_id)
        overlay = LiveOverlay(
            str(tournament_id),
            baseline,
            baseline.weights,
            enabled=bool(state["enabled"]),
            suspended=bool(state["suspended"]),
            emergency_stopped=bool(state["emergency_stopped"]),
            expired=bool(state["expired"]),
        )
        live_ledger = self._tournament_ledger(tournament_id)
        frozen = freeze_live_round(
            overlay,
            round_id=str(next_round_id),
            completed_round_id=str(completed_round_id),
            live_ledger=live_ledger,
            context=context,
            policy=self._policy,
            calibration_cutoff_at_utc=str(closed[2]),
        )
        payload = {
            "schema_version": "strathmark-v3-live-round-weight-freeze-v1",
            "tournament_id": str(tournament_id),
            "completed_round_id": str(completed_round_id),
            "next_round_id": str(next_round_id),
            "round_close_event_digest": str(closed[1]),
            "next_epoch_id": str(epoch[0]),
            "next_epoch_digest": str(epoch[1]),
            "baseline_receipt_digest": baseline.receipt_digest,
            "ledger_projection_digest": live_ledger.current_projection_digest,
            "weights": [(item.value, value) for item, value in frozen.current_weights],
            "influence": frozen.rounds[-1].influence,
            "control_state": state,
            "context": context.to_dict(),
        }
        try:
            self._append_event(
                command_kind=CommandKind.CHANGE_WEIGHTS,
                event_kind=EventKind.WEIGHTS_CHANGED,
                aggregate_kind=AggregateKind.WEIGHTS,
                aggregate_id=target,
                payload=payload,
                result={"freeze_digest": canonical_digest(payload)},
                command_id=command_id,
                actor_id=actor_id,
                occurred_at_utc=occurred_at_utc,
                monotonic_elapsed_ms=monotonic_elapsed_ms,
                projection_hook=self._round_freeze_guard(
                    tournament_id=tournament_id,
                    next_round_id=next_round_id,
                ),
            )
        except EventStoreConflict as exc:
            raise CredibilityReactionError(
                "live freeze conflicted with issued-field or round-freeze authority"
            ) from exc
        return frozen

    def _load_live_control_retry(
        self,
        *,
        target: StableIdentifier,
        tournament_id: StableIdentifier,
        action: str,
        reason: str,
        command_kind: CommandKind,
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
    ) -> StoredCommandResult | None:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT first_global_sequence FROM v3_idempotency_records "
                "WHERE principal_id=? AND idempotency_key=?",
                (str(actor_id), str(command_id)),
            ).fetchone()
            if row is None:
                return None
            event_row = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE global_sequence=?",
                (int(row[0]),),
            ).fetchone()
        event = EventEnvelope.from_dict(json.loads(str(cast(Any, event_row)[0])))
        payload = cast(InlinePayload, event.command.payload).to_value()
        if (
            event.command.kind is not command_kind
            or str(event.command.target_aggregate) != str(target)
            or payload.get("schema_version") != "strathmark-v3-live-credibility-control-v1"
            or payload.get("tournament_id") != str(tournament_id)
            or payload.get("action") != action
            or payload.get("reason") != reason
        ):
            raise CredibilityReactionError("live-control retry changed its original request")
        retry = self._events.lookup_exact_retry(
            principal_id=str(actor_id),
            idempotency_key=str(command_id),
            command_kind=command_kind,
            target_aggregate=str(target),
            payload_digest=event.command.payload.digest,
        )
        return cast(StoredCommandResult, retry)

    def _live_control_guard(
        self,
        *,
        target: StableIdentifier,
        tournament_id: StableIdentifier,
        payload: Mapping[str, Any],
    ) -> Callable[[sqlite3.Connection, tuple[EventEnvelope, ...]], None]:
        def guard(connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]) -> None:
            current = events[0]
            authority = {
                str(row[0])
                for row in connection.execute(
                    "SELECT event_kind FROM v3_events WHERE aggregate_id=?",
                    (str(tournament_id),),
                ).fetchall()
            }
            if (
                EventKind.TOURNAMENT_OPENED.value not in authority
                or EventKind.TOURNAMENT_CLOSED.value in authority
            ):
                raise EventStoreConflict("live control lacks open tournament authority")
            row = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_id=? "
                "AND event_kind IN (?, ?, ?) AND global_sequence<? "
                "ORDER BY global_sequence DESC LIMIT 1",
                (
                    str(target),
                    EventKind.LIVE_SUSPENDED.value,
                    EventKind.LIVE_RESUMED.value,
                    EventKind.EMERGENCY_STOPPED.value,
                    current.global_sequence,
                ),
            ).fetchone()
            if row is None:
                before = {
                    "tournament_id": str(tournament_id),
                    "enabled": True,
                    "suspended": False,
                    "emergency_stopped": False,
                    "expired": False,
                }
            else:
                prior = EventEnvelope.from_dict(json.loads(str(row[0])))
                prior_payload = cast(InlinePayload, prior.command.payload).to_value()
                before = prior_payload.get("after")
            if before != payload["before"] or canonical_digest(before) != payload["before_digest"]:
                raise EventStoreConflict("live control before-state changed concurrently")

        return guard

    def _round_freeze_guard(
        self, *, tournament_id: StableIdentifier, next_round_id: StableIdentifier
    ) -> Callable[[sqlite3.Connection, tuple[EventEnvelope, ...]], None]:
        def guard(connection: sqlite3.Connection, events: tuple[EventEnvelope, ...]) -> None:
            current = events[0]
            matches = []
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? AND event_kind=?",
                (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
            ).fetchall()
            for row in rows:
                event = EventEnvelope.from_dict(json.loads(str(row[0])))
                value = cast(InlinePayload, event.command.payload).to_value()
                if (
                    value.get("schema_version") == "strathmark-v3-live-round-weight-freeze-v1"
                    and value.get("tournament_id") == str(tournament_id)
                    and value.get("next_round_id") == str(next_round_id)
                ):
                    matches.append(event.event_digest)
            issued = connection.execute(
                "SELECT 1 FROM v3_ingress_snapshots snapshot JOIN v3_events event "
                "ON event.aggregate_id=snapshot.entity_id WHERE snapshot.entity_kind='field' "
                "AND snapshot.tournament_id=? AND snapshot.round_id=? "
                "AND event.event_kind=? LIMIT 1",
                (
                    str(tournament_id),
                    str(next_round_id),
                    EventKind.FIELD_ISSUED.value,
                ),
            ).fetchone()
            if matches != [current.event_digest] or issued is not None:
                raise EventStoreConflict("round freeze is not atomically unique before issue")

        return guard

    def _load_round_freeze(
        self, next_round_id: StableIdentifier
    ) -> tuple[EventEnvelope, dict[str, Any]] | None:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? AND event_kind=? "
                "ORDER BY global_sequence",
                (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
            ).fetchall()
        found: tuple[EventEnvelope, dict[str, Any]] | None = None
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            payload = cast(InlinePayload, event.command.payload).to_value()
            if payload.get(
                "schema_version"
            ) == "strathmark-v3-live-round-weight-freeze-v1" and payload.get(
                "next_round_id"
            ) == str(next_round_id):
                if found is not None:
                    raise CredibilityReactionError("duplicate persisted next-round weight freezes")
                found = event, payload
        return found

    def _require_open_tournament(self, tournament_id: StableIdentifier) -> None:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT event_kind FROM v3_events WHERE aggregate_id=? ORDER BY global_sequence",
                (str(tournament_id),),
            ).fetchall()
        kinds = {str(row[0]) for row in rows}
        if EventKind.TOURNAMENT_OPENED.value not in kinds:
            raise CredibilityReactionError("live overlay requires persisted tournament authority")
        if EventKind.TOURNAMENT_CLOSED.value in kinds:
            raise CredibilityReactionError("live overlay expired at tournament close")

    def _load_live_control_state(
        self, target: StableIdentifier, tournament_id: StableIdentifier
    ) -> dict[str, Any]:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_id=? "
                "AND event_kind IN (?, ?, ?) ORDER BY global_sequence DESC LIMIT 1",
                (
                    str(target),
                    EventKind.LIVE_SUSPENDED.value,
                    EventKind.LIVE_RESUMED.value,
                    EventKind.EMERGENCY_STOPPED.value,
                ),
            ).fetchone()
        if row is None:
            return {
                "tournament_id": str(tournament_id),
                "enabled": True,
                "suspended": False,
                "emergency_stopped": False,
                "expired": False,
            }
        event = EventEnvelope.from_dict(json.loads(str(row[0])))
        payload = cast(InlinePayload, event.command.payload).to_value()
        state = payload.get("after")
        if not isinstance(state, dict):
            raise CredibilityReactionError("persisted live control state is malformed")
        return dict(state)

    def load_ledger(self) -> CredibilityLedger:
        self._events.verify()
        ledger = CredibilityLedger()
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? "
                "AND event_kind IN (?, ?) ORDER BY global_sequence",
                (
                    AggregateKind.SCORE.value,
                    EventKind.SCORE_RECORDED.value,
                    EventKind.SCORE_REVERSED.value,
                ),
            ).fetchall()
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            payload = cast(InlinePayload, event.command.payload).to_value()
            if set(payload) != {"schema_version", "record_type", "record"}:
                raise CredibilityReactionError("persisted credibility event payload is not closed")
            if payload["schema_version"] != "strathmark-v3-credibility-authority-event-v1":
                raise CredibilityReactionError("persisted credibility event schema differs")
            record_type = payload["record_type"]
            record = payload["record"]
            if not isinstance(record, Mapping):
                raise CredibilityReactionError("persisted credibility record is malformed")
            if record_type == "opportunity":
                ledger = ledger.append_opportunity(_opportunity_from_dict(record))
            elif record_type == "score":
                ledger = ledger.append_score(_score_from_dict(record))
            elif record_type == "reversal":
                ledger = ledger.append_reversal(_reversal_from_dict(record))
            else:
                raise CredibilityReactionError("persisted credibility record type is unknown")
        return ledger

    def baseline_weights(
        self, context: ContextNode, *, calibration_cutoff_at_utc: str
    ) -> WeightReceipt:
        """Read the long-term baseline from tournament-finalized authority only."""

        if not isinstance(context, ContextNode):
            raise CredibilityReactionError("baseline requires a typed context")
        return calibrate_baseline(
            self._finalized_ledger(),
            context,
            self._policy,
            calibration_cutoff_at_utc=calibration_cutoff_at_utc,
        )

    def _tournament_baseline(
        self,
        tournament_id: StableIdentifier,
        context: ContextNode,
        *,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> WeightReceipt:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            opened = connection.execute(
                "SELECT global_sequence, event_digest, occurred_at_utc FROM v3_events "
                "WHERE aggregate_id=? "
                "AND event_kind=? ORDER BY global_sequence LIMIT 1",
                (str(tournament_id), EventKind.TOURNAMENT_OPENED.value),
            ).fetchone()
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? AND event_kind=? "
                "ORDER BY global_sequence",
                (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
            ).fetchall()
        if opened is None:
            raise CredibilityReactionError("tournament baseline requires its open authority")
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            payload = cast(InlinePayload, event.command.payload).to_value()
            if (
                payload.get("schema_version") == "strathmark-v3-tournament-baseline-snapshot-v1"
                and payload.get("tournament_id") == str(tournament_id)
                and payload.get("context") == context.to_dict()
            ):
                return _decode_weight_receipt(payload["receipt"])
        finalized_ledger = self._finalized_ledger(before_sequence=int(opened[0]))
        receipt = calibrate_baseline(
            finalized_ledger,
            context,
            self._policy,
            calibration_cutoff_at_utc=str(opened[2]),
        )
        payload = {
            "schema_version": "strathmark-v3-tournament-baseline-snapshot-v1",
            "tournament_id": str(tournament_id),
            "tournament_open_sequence": int(opened[0]),
            "tournament_open_event_digest": str(opened[1]),
            "baseline_ledger_projection_digest": (finalized_ledger.current_projection_digest),
            "context": context.to_dict(),
            "receipt": _encode_weight_receipt(receipt),
        }
        digest = canonical_digest(payload)
        target = deterministic_identifier("weights", {"tournament_id": str(tournament_id)})
        try:
            self._append_event(
                command_kind=CommandKind.CHANGE_WEIGHTS,
                event_kind=EventKind.WEIGHTS_CHANGED,
                aggregate_kind=AggregateKind.WEIGHTS,
                aggregate_id=target,
                payload=payload,
                result={"baseline_snapshot_digest": digest},
                command_id=IdempotencyKey(f"command:{digest}"),
                actor_id=actor_id,
                occurred_at_utc=occurred_at_utc,
                monotonic_elapsed_ms=monotonic_elapsed_ms,
            )
        except EventStoreConflict:
            with open_v3_connection(self.database_path, read_only=True) as connection:
                concurrent = connection.execute(
                    "SELECT envelope_json FROM v3_events WHERE aggregate_id=? "
                    "AND event_kind=? ORDER BY global_sequence",
                    (str(target), EventKind.WEIGHTS_CHANGED.value),
                ).fetchall()
            for row in concurrent:
                event = EventEnvelope.from_dict(json.loads(str(row[0])))
                value = cast(InlinePayload, event.command.payload).to_value()
                if canonical_digest(value) == canonical_digest(payload):
                    return _decode_weight_receipt(value["receipt"])
            raise
        return receipt

    def _finalized_ledger(self, *, before_sequence: int | None = None) -> CredibilityLedger:
        """Only tournament-closed history may alter the long-term baseline."""

        ledger = self.load_ledger()
        with open_v3_connection(self.database_path, read_only=True) as connection:
            closed = {
                str(row[0])
                for row in connection.execute(
                    "SELECT aggregate_id, global_sequence FROM v3_events WHERE aggregate_kind=? "
                    "AND event_kind=?",
                    (AggregateKind.TOURNAMENT.value, EventKind.TOURNAMENT_CLOSED.value),
                ).fetchall()
                if before_sequence is None or int(row[1]) < before_sequence
            }
            finalized_results = {
                str(row[0])
                for row in connection.execute(
                    "SELECT result_key, observation_json FROM v3_result_revisions"
                ).fetchall()
                if str(json.loads(str(row[1]))["tournament_id"]) in closed
            }
        opportunities = tuple(
            item for item in ledger.opportunities if item.result_id in finalized_results
        )
        scores = tuple(item for item in ledger.scores if item.result_id in finalized_results)
        target_ids = {item.opportunity_id for item in opportunities} | {
            item.score_id for item in scores
        }
        reversals = tuple(item for item in ledger.reversals if item.target_id in target_ids)
        return CredibilityLedger(opportunities, scores, reversals)

    def _tournament_ledger(self, tournament_id: StableIdentifier) -> CredibilityLedger:
        ledger = self.load_ledger()
        with open_v3_connection(self.database_path, read_only=True) as connection:
            result_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT result_key, observation_json FROM v3_result_revisions"
                ).fetchall()
                if str(json.loads(str(row[1]))["tournament_id"]) == str(tournament_id)
            }
        opportunities = tuple(item for item in ledger.opportunities if item.result_id in result_ids)
        scores = tuple(item for item in ledger.scores if item.result_id in result_ids)
        target_ids = {item.opportunity_id for item in opportunities} | {
            item.score_id for item in scores
        }
        reversals = tuple(item for item in ledger.reversals if item.target_id in target_ids)
        return CredibilityLedger(opportunities, scores, reversals)

    def _verify_forecast_manifest(
        self, sealed: SealedForecastCommit
    ) -> tuple[dict[str, Any], AssessorForecast, EvidencePacket]:
        if not isinstance(sealed, SealedForecastCommit):
            raise CredibilityReactionError("forecast authority requires a sealed commit")
        try:
            payload = dict(verify_manifest(sealed.manifest, self._trust_store))
        except IntegrityError as exc:
            raise CredibilityReactionError(
                "forecast commit signature is invalid or untrusted"
            ) from exc
        expected = {
            "schema_version",
            "field_id",
            "competitor_id",
            "field_revision",
            "evidence_epoch_id",
            "evidence_epoch_digest",
            "historical_cutoff_key",
            "receipt_id",
            "issue_event_digest",
            "member_id",
            "operational_promotion_digest",
            "execution_failure_kind",
            "evidence_packet",
            "assessor_input",
            "assessor_receipt",
            "forecast",
        }
        if set(payload) != expected or payload["schema_version"] != (
            "strathmark-v3-credibility-forecast-commit-v1"
        ):
            raise CredibilityReactionError("forecast commit signed payload is not closed")
        try:
            forecast = AssessorForecast.from_dict(payload["forecast"])
            packet = EvidencePacket.from_dict(payload["evidence_packet"])
        except Exception as exc:
            raise CredibilityReactionError(
                "forecast commit contains no valid forecast or evidence packet"
            ) from exc
        self._verify_assessor_receipt(payload, forecast, packet)
        return payload, forecast, packet

    def _authority_scoring_parameters(self, packet: EvidencePacket) -> dict[str, object]:
        times = list(packet.eligible_raw_times_ms)
        if times:
            center = Decimal(str(median(times)))
            deviations = [abs(Decimal(value) - center) for value in times]
            scale = max(1_000, int(median(deviations) if len(times) >= 3 else center / 10))
        else:
            scale = 10_000
        numeric = len(times)
        quality = Decimal(numeric) / Decimal(max(1, len(packet.observations)))
        newest = max((item.observation_sequence for item in packet.observations), default=0)
        age = max(0, packet.tournament_event_sequence - newest)
        recency = Decimal(1) / (Decimal(1) + Decimal(age) / Decimal(1_000))
        evidence_weight = format(quality * recency if packet.observations else Decimal("0.1"), "f")
        return {
            "schema_version": "strathmark-v3-authority-scoring-parameters-v1",
            "robust_context_scale_ms": scale,
            "evidence_weight": evidence_weight,
            "quality": format(quality, "f"),
            "recency": format(recency, "f"),
            "packet_digest": packet.content_digest,
        }

    def _verify_assessor_receipt(
        self,
        payload: Mapping[str, Any],
        forecast: AssessorForecast,
        packet: EvidencePacket,
    ) -> None:
        receipt = payload["assessor_receipt"]
        if not isinstance(receipt, Mapping) or receipt.get("forecast") != forecast.to_dict():
            raise CredibilityReactionError("assessor receipt does not bind the forecast")
        receipt_value = dict(receipt)
        receipt_digest = receipt_value.pop("assessment_digest", None)
        if receipt_digest != canonical_digest(receipt_value):
            raise CredibilityReactionError("assessor receipt digest is invalid")
        formula_input = payload["assessor_input"]
        if forecast.assessor is AssessorKind.FORMULA:
            if (
                not isinstance(formula_input, Mapping)
                or formula_input.get("schema_version") != "strathmark-v3-formula-input-v1"
                or formula_input.get("evidence") != packet.to_dict()
                or canonical_digest(formula_input) != forecast.evidence_digest
                or receipt.get("schema_version") != "strathmark-v3-assessment-result-v1"
            ):
                raise CredibilityReactionError("Formula assessor input receipt is invalid")
            governor = formula_input.get("governor_receipt")
            if not isinstance(governor, Mapping):
                raise CredibilityReactionError("Formula governor receipt is absent")
            governor_value = dict(governor)
            governor_digest = governor_value.pop("receipt_digest", None)
            if (
                governor_digest != canonical_digest(governor_value)
                or governor.get("evidence_digest") != packet.content_digest
                or governor.get("tournament_epoch_id") != str(packet.tournament_epoch_id)
            ):
                raise CredibilityReactionError("Formula governor receipt is not packet-bound")
            expected_effective_weight = receipt.get("personal_weight")
        elif forecast.assessor is AssessorKind.ML:
            if (
                formula_input is not None
                or forecast.evidence_digest != packet.content_digest
                or receipt.get("schema_version") != "strathmark-v3-ml-assessment-v1"
            ):
                raise CredibilityReactionError("ML assessor receipt is invalid")
            expected_effective_weight = str(len(packet.eligible_raw_times_ms))
        else:
            if formula_input is not None:
                raise CredibilityReactionError("non-Formula assessor input is invalid")
            expected_effective_weight = forecast.support.effective_weight
        exact = sum(
            item.context.digest == packet.target_context.digest
            for item in packet.observations
            if admit_raw_completion(item.result) is not None
        )
        if (
            forecast.support.eligible_count != len(packet.eligible_raw_times_ms)
            or forecast.support.effective_weight != expected_effective_weight
            or forecast.support.exact_context_count != exact
            or forecast.support.max_historical_key != packet.historical_cutoff_key
            or forecast.support.tournament_event_sequence != packet.tournament_event_sequence
        ):
            raise CredibilityReactionError("forecast support differs from canonical evidence")

    def _verify_commit_causality(
        self,
        payload: Mapping[str, Any],
        forecast: AssessorForecast,
        packet: EvidencePacket,
    ) -> dict[str, object]:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            epoch = connection.execute(
                "SELECT round_id, epoch_digest, historical_cutoff_key, "
                "maximum_tournament_sequence FROM v3_evidence_epochs WHERE epoch_id=?",
                (payload["evidence_epoch_id"],),
            ).fetchone()
            issue = connection.execute(
                "SELECT global_sequence, event_digest, envelope_json FROM v3_events "
                "WHERE aggregate_id=? AND event_kind=? ORDER BY global_sequence DESC LIMIT 1",
                (payload["field_id"], EventKind.FIELD_ISSUED.value),
            ).fetchone()
            ingress = connection.execute(
                "SELECT round_id, snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' AND entity_id=? AND upstream_revision=? LIMIT 1",
                (payload["field_id"], payload["field_revision"]),
            ).fetchone()
            prior_result = connection.execute(
                "SELECT 1 FROM v3_result_revisions WHERE field_id=? AND competitor_id=? LIMIT 1",
                (payload["field_id"], payload["competitor_id"]),
            ).fetchone()
        if epoch is None or issue is None or ingress is None:
            raise CredibilityReactionError("forecast commit lacks issued-field and epoch authority")
        issue_event = EventEnvelope.from_dict(json.loads(str(issue[2])))
        issue_payload = cast(InlinePayload, issue_event.command.payload).to_value()
        snapshot = json.loads(str(ingress[1]))
        if (
            str(epoch[0]) != str(ingress[0])
            or str(epoch[1]) != payload["evidence_epoch_digest"]
            or str(epoch[2]) != payload["historical_cutoff_key"]
            or int(epoch[3]) < forecast.support.tournament_event_sequence
            or int(issue_payload["field_revision"]) != payload["field_revision"]
            or issue_payload["epoch_id"] != payload["evidence_epoch_id"]
            or issue_payload["receipt_id"] != payload["receipt_id"]
            or str(issue[1]) != payload["issue_event_digest"]
            or payload["competitor_id"] not in issue_payload["competitor_ids"]
            or payload["competitor_id"] not in snapshot["competitor_ids"]
            or prior_result is not None
        ):
            raise CredibilityReactionError("forecast commit does not bind the exact causal issue")
        if payload["operational_promotion_digest"] is not None:
            raise CredibilityReactionError(
                "operational LLM promotion cannot be trusted before U19 authority exists"
            )
        if forecast.assessor is AssessorKind.LLM_MEMBER and not payload["member_id"]:
            raise CredibilityReactionError("candidate member identity is absent")
        self._verify_packet_causality(payload, packet, snapshot)
        return self._authority_scoring_parameters(packet)

    def _verify_packet_causality(
        self,
        payload: Mapping[str, Any],
        packet: EvidencePacket,
        field_snapshot: Mapping[str, Any],
    ) -> None:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            epoch = connection.execute(
                "SELECT maximum_tournament_sequence FROM v3_evidence_epochs WHERE epoch_id=?",
                (payload["evidence_epoch_id"],),
            ).fetchone()
            rows = connection.execute(
                "SELECT r.observation_json FROM v3_evidence_epoch_members m "
                "JOIN v3_result_revisions r ON r.result_key=m.result_key "
                "AND r.revision=m.result_revision "
                "AND r.source_global_sequence=m.source_global_sequence "
                "WHERE m.epoch_id=? ORDER BY r.source_global_sequence",
                (payload["evidence_epoch_id"],),
            ).fetchall()
        expected_observations = tuple(
            ResultObservation.from_dict(json.loads(str(row[0])))
            for row in rows
            if json.loads(str(row[0]))["competitor_id"] == payload["competitor_id"]
        )
        try:
            target = TargetContext.from_dict(field_snapshot["target_context"])
        except Exception as exc:
            raise CredibilityReactionError("issued field target context is invalid") from exc
        assessor_input = payload["assessor_input"]
        governor = (
            assessor_input.get("governor_receipt") if isinstance(assessor_input, Mapping) else None
        )
        if (
            epoch is None
            or str(packet.competitor_id) != payload["competitor_id"]
            or packet.target_context != target
            or packet.observations != expected_observations
            or str(packet.tournament_epoch_id) != payload["evidence_epoch_id"]
            or packet.historical_cutoff_key != payload["historical_cutoff_key"]
            or packet.tournament_event_sequence != int(epoch[0])
            or (
                isinstance(governor, Mapping)
                and governor.get("tournament_epoch_content_digest")
                != payload["evidence_epoch_digest"]
            )
        ):
            raise CredibilityReactionError(
                "canonical evidence packet differs from epoch or issued field authority"
            )

    def _verify_candidate_causality(
        self,
        payload: Mapping[str, Any],
        forecast: AssessorForecast,
        packet: EvidencePacket,
    ) -> None:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            epoch = connection.execute(
                "SELECT round_id, epoch_digest, historical_cutoff_key, "
                "maximum_tournament_sequence FROM v3_evidence_epochs WHERE epoch_id=?",
                (payload["evidence_epoch_id"],),
            ).fetchone()
            issue = connection.execute(
                "SELECT event_digest, envelope_json FROM v3_events WHERE aggregate_id=? "
                "AND event_kind=? ORDER BY global_sequence DESC LIMIT 1",
                (payload["field_id"], EventKind.FIELD_ISSUED.value),
            ).fetchone()
            ingress = connection.execute(
                "SELECT round_id, snapshot_json FROM v3_ingress_snapshots "
                "WHERE entity_kind='field' AND entity_id=? AND upstream_revision=? LIMIT 1",
                (payload["field_id"], payload["field_revision"]),
            ).fetchone()
            prior = connection.execute(
                "SELECT 1 FROM v3_result_revisions WHERE field_id=? AND competitor_id=? LIMIT 1",
                (payload["field_id"], payload["competitor_id"]),
            ).fetchone()
        if epoch is None or issue is None or ingress is None or prior is not None:
            raise CredibilityReactionError("candidate diagnostic lacks pre-result issue authority")
        issue_event = EventEnvelope.from_dict(json.loads(str(issue[1])))
        issue_payload = cast(InlinePayload, issue_event.command.payload).to_value()
        snapshot = json.loads(str(ingress[1]))
        if (
            str(epoch[0]) != str(ingress[0])
            or str(epoch[1]) != payload["evidence_epoch_digest"]
            or str(epoch[2]) != payload["historical_cutoff_key"]
            or int(epoch[3]) != forecast.support.tournament_event_sequence
            or int(issue_payload["field_revision"]) != payload["field_revision"]
            or issue_payload["epoch_id"] != payload["evidence_epoch_id"]
            or issue_payload["receipt_id"] != payload["receipt_id"]
            or str(issue[0]) != payload["issue_event_digest"]
            or payload["competitor_id"] not in issue_payload["competitor_ids"]
            or payload["competitor_id"] not in snapshot["competitor_ids"]
        ):
            raise CredibilityReactionError("candidate diagnostic differs from issued authority")
        self._verify_packet_causality(payload, packet, snapshot)

    def _active_settled_result(
        self, result_id: StableIdentifier
    ) -> tuple[Any, ResultObservation, EventEnvelope, dict[str, Any]]:
        self._events.verify()
        with open_v3_connection(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT result_key, revision, source_global_sequence, observation_json, "
                "observation_digest, numeric_eligible, settled_global_sequence, field_id, "
                "field_revision, competitor_id, claimed_receipt_id, "
                "(SELECT occurred_at_utc FROM v3_events WHERE global_sequence="
                "settled_global_sequence) FROM v3_result_revisions "
                "WHERE result_key=? ORDER BY revision DESC LIMIT 1",
                (str(result_id),),
            ).fetchone()
            if row is None or row[6] is None:
                raise CredibilityReactionError("credibility requires the active settled result")
            issue_row = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_id=? AND event_kind=? "
                "AND global_sequence<? ORDER BY global_sequence DESC LIMIT 1",
                (str(row[7]), EventKind.FIELD_ISSUED.value, int(row[6])),
            ).fetchone()
        if issue_row is None:
            raise CredibilityReactionError("settled result has no issued field authority")
        observation = ResultObservation.from_dict(json.loads(str(row[3])))
        issue_event = EventEnvelope.from_dict(json.loads(str(issue_row[0])))
        issue_payload = cast(InlinePayload, issue_event.command.payload).to_value()
        if (
            str(observation.competitor_id) != str(row[9])
            or str(observation.field_id) != str(row[7])
            or observation.result.revision != int(row[1])
            or canonical_digest(observation.to_dict()) != str(row[4])
            or issue_payload["field_revision"] != int(row[8])
            or issue_payload["receipt_id"] != str(row[10])
            or str(row[9]) not in issue_payload["competitor_ids"]
        ):
            raise CredibilityReactionError("active result revision and issued membership differ")
        return row, observation, issue_event, issue_payload

    def _eligible_forecasts(
        self,
        *,
        observation: ResultObservation,
        result_source_sequence: int,
        issue_event: EventEnvelope,
        issue_payload: Mapping[str, Any],
    ) -> tuple[tuple[dict[str, Any], AssessorForecast, EventEnvelope], ...]:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? "
                "AND event_kind IN (?, ?) AND global_sequence<? ORDER BY global_sequence",
                (
                    AggregateKind.FORECAST.value,
                    EventKind.COMPONENT_FORECAST_COMMITTED.value,
                    EventKind.COMPONENT_FORECAST_REJECTED.value,
                    result_source_sequence,
                ),
            ).fetchall()
        selected: list[tuple[dict[str, Any], AssessorForecast, EventEnvelope]] = []
        identities: set[tuple[str, str | None]] = set()
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            event_value = cast(InlinePayload, event.command.payload).to_value()
            if event_value.get("schema_version") == ("strathmark-v3-candidate-diagnostic-event-v1"):
                if event_value.get("candidate_report_digest") != canonical_digest(
                    event_value.get("candidate_report")
                ):
                    raise CredibilityReactionError("candidate report digest is invalid")
                try:
                    packet = EvidencePacket.from_dict(event_value["evidence_packet"])
                    forecast = AssessorForecast.from_dict(event_value["forecast"])
                except Exception as exc:
                    raise CredibilityReactionError("candidate forecast event is malformed") from exc
                payload = dict(event_value)
                if (
                    forecast.assessor not in {AssessorKind.LLM_MEMBER, AssessorKind.LLM_COUNCIL}
                    or payload["operational_promotion_digest"] is not None
                ):
                    raise CredibilityReactionError(
                        "candidate diagnostic cannot become an operational forecast"
                    )
            else:
                if set(event_value) != {
                    "schema_version",
                    "sealed_manifest",
                    "authority_scoring_parameters",
                }:
                    raise CredibilityReactionError("forecast authority event payload is malformed")
                sealed = SealedForecastCommit(
                    SignedManifest.from_dict(event_value["sealed_manifest"])
                )
                payload, forecast, packet = self._verify_forecast_manifest(sealed)
            parameters = self._authority_scoring_parameters(packet)
            if event_value["authority_scoring_parameters"] != parameters:
                raise CredibilityReactionError("forecast scoring parameters drifted from authority")
            payload = {
                **payload,
                "robust_context_scale_ms": parameters["robust_context_scale_ms"],
                "evidence_weight": parameters["evidence_weight"],
            }
            if payload["field_id"] != str(observation.field_id) or payload["competitor_id"] != str(
                observation.competitor_id
            ):
                continue
            if (
                payload["field_revision"] != issue_payload["field_revision"]
                or payload["receipt_id"] != issue_payload["receipt_id"]
                or payload["issue_event_digest"] != issue_event.event_digest
                or payload["evidence_epoch_id"] != issue_payload["epoch_id"]
            ):
                raise CredibilityReactionError(
                    "forecast commit differs from settled issue authority"
                )
            key = (forecast.assessor.value, payload["member_id"])
            if key in identities:
                raise CredibilityReactionError(
                    "multiple forecasts exist for one eligible assessor/member"
                )
            identities.add(key)
            selected.append((payload, forecast, event))
        return tuple(selected)

    def _complete_field_results(
        self,
        field_id: str,
        issue_payload: Mapping[str, Any],
        field_revision: int,
    ) -> tuple[SettledFieldResult, ...]:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT result_key, revision, source_global_sequence, observation_json, "
                "observation_digest, settled_global_sequence, competitor_id "
                "FROM v3_result_revisions r WHERE field_id=? AND field_revision=? "
                "AND revision=(SELECT MAX(r2.revision) FROM v3_result_revisions r2 "
                "WHERE r2.result_key=r.result_key)",
                (field_id, field_revision),
            ).fetchall()
        by_competitor: dict[str, SettledFieldResult] = {}
        for row in rows:
            if row[5] is None:
                continue
            observation = ResultObservation.from_dict(json.loads(str(row[3])))
            if canonical_digest(observation.to_dict()) != str(row[4]):
                raise CredibilityReactionError("field result revision digest is invalid")
            by_competitor[str(row[6])] = SettledFieldResult(
                competitor_id=str(row[6]),
                result_id=str(row[0]),
                result_revision=int(row[1]),
                result_revision_digest=str(row[4]),
                source_sequence=int(row[2]),
                status=observation.result.status.value,
                raw_time_ms=observation.result.raw_time_ms,
            )
        members = tuple(sorted(issue_payload["competitor_ids"]))
        if set(by_competitor) != set(members):
            raise CredibilityReactionError(
                "field consequence settlement awaits every terminal issued member"
            )
        return tuple(by_competitor[item] for item in members)

    def _field_forecast_cards(
        self,
        field_id: str,
        issue_payload: Mapping[str, Any],
        result_source_sequence: int,
    ) -> tuple[FieldForecastCard, ...]:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? "
                "AND event_kind IN (?, ?) AND global_sequence<? ORDER BY global_sequence",
                (
                    AggregateKind.FORECAST.value,
                    EventKind.COMPONENT_FORECAST_COMMITTED.value,
                    EventKind.COMPONENT_FORECAST_REJECTED.value,
                    result_source_sequence,
                ),
            ).fetchall()
        cards = []
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            event_value = cast(InlinePayload, event.command.payload).to_value()
            if event_value.get("schema_version") == ("strathmark-v3-candidate-diagnostic-event-v1"):
                payload = event_value
                forecast = AssessorForecast.from_dict(event_value["forecast"])
            else:
                sealed = SealedForecastCommit(
                    SignedManifest.from_dict(event_value["sealed_manifest"])
                )
                payload, forecast, _packet = self._verify_forecast_manifest(sealed)
            if (
                payload["field_id"] == field_id
                and payload["field_revision"] == issue_payload["field_revision"]
                and payload["receipt_id"] == issue_payload["receipt_id"]
            ):
                cards.append(
                    FieldForecastCard(payload["competitor_id"], payload["member_id"], forecast)
                )
        return tuple(
            sorted(
                cards,
                key=lambda item: (
                    item.competitor_id,
                    item.forecast.assessor.value,
                    item.member_id or "",
                    item.forecast.commit_digest,
                ),
            )
        )

    def _append_prior_reversals(
        self,
        ledger: CredibilityLedger,
        *,
        result_id: str,
        replacement_revision: int,
        source_sequence: int,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> CredibilityLedger:
        targets = [
            ("opportunity", item.opportunity_id, item.result_revision)
            for item in ledger.active_opportunities
            if item.result_id == result_id and item.result_revision < replacement_revision
        ] + [
            ("score", item.score_id, item.result_revision)
            for item in ledger.active_scores
            if item.result_id == result_id and item.result_revision < replacement_revision
        ]
        for target_kind, target_id, original_revision in targets:
            reversal = LedgerReversal.create(
                reversal_id=str(
                    deterministic_identifier(
                        "reversal",
                        {
                            "target_kind": target_kind,
                            "target_id": target_id,
                            "replacement_result_revision": replacement_revision,
                        },
                    )
                ),
                target_kind=target_kind,
                target_id=target_id,
                original_result_revision=original_revision,
                replacement_result_revision=replacement_revision,
                source_sequence=source_sequence,
            )
            self._append_credibility_record(
                "reversal",
                reversal.to_dict(),
                aggregate_id=_score_aggregate(target_kind, target_id),
                event_kind=EventKind.SCORE_REVERSED,
                actor_id=actor_id,
                occurred_at_utc=occurred_at_utc,
                monotonic_elapsed_ms=monotonic_elapsed_ms,
            )
        return self.load_ledger()

    def _append_missing_opportunity(
        self,
        ledger: CredibilityLedger,
        *,
        result_id: str,
        assessor: AssessorKind,
        observation: ResultObservation,
        result_revision: int,
        source_sequence: int,
        issue_event: EventEnvelope,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> CredibilityLedger:
        forecast_digest = canonical_digest(
            {
                "unavailable_assessor": assessor.value,
                "field_id": str(observation.field_id),
                "competitor_id": str(observation.competitor_id),
                "issue_event_digest": issue_event.event_digest,
            }
        )
        opportunity = _make_opportunity(
            assessor=assessor,
            scope=ScoreScope.OPERATIONAL,
            forecast_digest=forecast_digest,
            observation=observation,
            result_id=result_id,
            result_revision=result_revision,
            source_sequence=source_sequence,
            context=_context_from_observation(observation, history_count=0),
            outcome=OpportunityOutcome.UNAVAILABLE,
            difficulty=_derived_difficulty(0),
            member_id=None,
        )
        if any(item.opportunity_id == opportunity.opportunity_id for item in ledger.opportunities):
            return ledger
        self._append_credibility_record(
            "opportunity",
            opportunity.to_dict(),
            aggregate_id=_score_aggregate("opportunity", opportunity.opportunity_id),
            event_kind=EventKind.SCORE_RECORDED,
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        return ledger.append_opportunity(opportunity)

    def _append_forecast_outcome(
        self,
        ledger: CredibilityLedger,
        *,
        result_id: str,
        payload: Mapping[str, Any],
        forecast: AssessorForecast,
        observation: ResultObservation,
        result_revision: int,
        result_revision_digest: str,
        source_sequence: int,
        issue_event: EventEnvelope,
        issue_payload: Mapping[str, Any],
        field_results: tuple[SettledFieldResult, ...],
        field_forecasts: tuple[FieldForecastCard, ...],
        settled_at_utc: str,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> CredibilityLedger:
        scope = (
            ScoreScope.CANDIDATE
            if forecast.assessor in {AssessorKind.LLM_COUNCIL, AssessorKind.LLM_MEMBER}
            and payload["operational_promotion_digest"] is None
            else ScoreScope.OPERATIONAL
        )
        outcome = {
            ForecastState.COMMITTED: OpportunityOutcome.SUCCESSFUL,
            ForecastState.ABSTAINED: OpportunityOutcome.PRINCIPLED_ABSTENTION,
            ForecastState.INVALID: OpportunityOutcome.SCHEMA_INVALID,
        }[forecast.state]
        if payload["execution_failure_kind"] is not None:
            outcome = OpportunityOutcome(payload["execution_failure_kind"])
        context = _context_from_observation(
            observation, history_count=forecast.support.eligible_count
        )
        opportunity = _make_opportunity(
            assessor=forecast.assessor,
            scope=scope,
            forecast_digest=forecast.commit_digest,
            observation=observation,
            result_id=result_id,
            result_revision=result_revision,
            source_sequence=source_sequence,
            context=context,
            outcome=outcome,
            difficulty=_derived_difficulty(forecast.support.eligible_count),
            member_id=payload["member_id"],
        )
        if not any(
            item.opportunity_id == opportunity.opportunity_id for item in ledger.opportunities
        ):
            self._append_credibility_record(
                "opportunity",
                opportunity.to_dict(),
                aggregate_id=_score_aggregate("opportunity", opportunity.opportunity_id),
                event_kind=EventKind.SCORE_RECORDED,
                actor_id=actor_id,
                occurred_at_utc=occurred_at_utc,
                monotonic_elapsed_ms=monotonic_elapsed_ms,
            )
            ledger = ledger.append_opportunity(opportunity)
        if outcome is not OpportunityOutcome.SUCCESSFUL:
            return ledger
        if any(
            item.scope is scope
            and item.assessor is forecast.assessor
            and item.forecast_digest == forecast.commit_digest
            and item.result_id == result_id
            and item.result_revision == result_revision
            for item in ledger.scores
        ):
            return ledger
        scoring_input = _optimizer_scoring_input(
            tournament_id=str(observation.tournament_id),
            round_id=str(observation.round_id),
            field_id=str(observation.field_id),
            competitor_id=str(observation.competitor_id),
            result_id=result_id,
            result_revision=result_revision,
            result_revision_digest=result_revision_digest,
            source_sequence=source_sequence,
            issued_field_members=tuple(sorted(issue_payload["competitor_ids"])),
            issued_marks=tuple(sorted(issue_payload["issued_marks"].items())),
            field_results=field_results,
            field_forecasts=field_forecasts,
            field_receipt_digest=issue_event.event_digest,
            optimizer_bundle_digest=self._optimizer_bundle_digest,
            credibility_policy_digest=self._policy_digest,
            raw_time_ms=cast(int, observation.result.raw_time_ms),
            context=context,
            robust_context_scale_ms=payload["robust_context_scale_ms"],
            evidence_weight=payload["evidence_weight"],
        )
        consequence = OptimizerConsequenceReceipt.pending(
            forecast_digest=forecast.commit_digest,
            result_revision_digest=result_revision_digest,
            field_receipt_digest=issue_event.event_digest,
            scoring_input_digest=scoring_input.scoring_input_digest,
            optimizer_bundle_digest=self._optimizer_bundle_digest,
        )
        evaluator = (
            self._evaluator if scope is ScoreScope.OPERATIONAL else self._diagnostic_evaluator
        )
        if evaluator is not None:
            diagnostic = evaluator.evaluate(forecast=forecast, scoring_input=scoring_input)
            if not isinstance(diagnostic, OptimizerConsequenceReceipt):
                raise CredibilityReactionError("optimizer evaluator returned no typed receipt")
            OptimizerConsequenceReceipt(
                diagnostic.evaluator_port,
                diagnostic.forecast_digest,
                diagnostic.result_revision_digest,
                diagnostic.field_receipt_digest,
                diagnostic.scoring_input_digest,
                diagnostic.optimizer_bundle_digest,
                diagnostic.status,
                diagnostic.metrics,
                diagnostic.authority_manifest_digest,
                diagnostic.receipt_digest,
            )
            if (
                diagnostic.status not in {ConsequenceStatus.DIAGNOSTIC, ConsequenceStatus.PENDING}
                or diagnostic.forecast_digest != forecast.commit_digest
                or diagnostic.result_revision_digest != result_revision_digest
                or diagnostic.field_receipt_digest != issue_event.event_digest
                or diagnostic.scoring_input_digest != scoring_input.scoring_input_digest
                or diagnostic.optimizer_bundle_digest != self._optimizer_bundle_digest
            ):
                raise CredibilityReactionError("optimizer receipt differs from exact scoring input")
            if scope is ScoreScope.CANDIDATE:
                consequence = diagnostic
            elif diagnostic.status is ConsequenceStatus.DIAGNOSTIC:
                consequence = OptimizerConsequenceReceipt.verified(
                    forecast_digest=diagnostic.forecast_digest,
                    result_revision_digest=diagnostic.result_revision_digest,
                    field_receipt_digest=diagnostic.field_receipt_digest,
                    scoring_input_digest=diagnostic.scoring_input_digest,
                    optimizer_bundle_digest=diagnostic.optimizer_bundle_digest,
                    metrics=diagnostic.metrics,
                    authority_manifest_digest=self._optimizer_authority_digest,
                )
            else:
                consequence = diagnostic
        raw_time = cast(int, observation.result.raw_time_ms)
        score = PredictiveScore.create(
            score_id=str(
                deterministic_identifier(
                    "score",
                    {
                        "opportunity_id": opportunity.opportunity_id,
                        "result_revision_digest": result_revision_digest,
                    },
                )
            ),
            scope=scope,
            assessor=forecast.assessor,
            forecast_digest=forecast.commit_digest,
            result_id=opportunity.result_id,
            result_revision=result_revision,
            source_sequence=source_sequence,
            context=context,
            evidence_weight=payload["evidence_weight"],
            metrics=compute_predictive_metrics(
                cast(Any, forecast.distribution),
                actual_time_ms=raw_time,
                robust_context_scale_ms=payload["robust_context_scale_ms"],
            ),
            consequence=consequence,
            settled_at_utc=settled_at_utc,
        )
        self._append_credibility_record(
            "score",
            score.to_dict(),
            aggregate_id=_score_aggregate("score", score.score_id),
            event_kind=EventKind.SCORE_RECORDED,
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )
        return ledger.append_score(score)

    def _append_weights(
        self,
        receipt: WeightReceipt,
        *,
        tournament_id: StableIdentifier,
        source_sequence: int,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> None:
        target = deterministic_identifier(
            "weights",
            {"tournament_id": str(tournament_id), "context": receipt.context.to_dict()},
        )
        payload = {
            "schema_version": "strathmark-v3-credibility-weights-event-v1",
            "source_sequence": source_sequence,
            "receipt_digest": receipt.receipt_digest,
            "context": receipt.context.to_dict(),
            "calibration_cutoff_at_utc": receipt.calibration_cutoff_at_utc,
            "policy_digest": receipt.policy_digest,
            "weights": [(item.value, value) for item, value in receipt.weights],
            "components": [
                {
                    name: (
                        getattr(component, name).value
                        if name == "assessor"
                        else getattr(component, name)
                    )
                    for name in component.__dataclass_fields__
                }
                for component in receipt.components
            ],
        }
        digest = canonical_digest(payload)
        self._append_event(
            command_kind=CommandKind.CHANGE_WEIGHTS,
            event_kind=EventKind.WEIGHTS_CHANGED,
            aggregate_kind=AggregateKind.WEIGHTS,
            aggregate_id=target,
            payload=payload,
            result={"weights_digest": digest},
            command_id=IdempotencyKey(f"command:{digest}"),
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )

    def _load_weight_receipt(self, source_sequence: int) -> WeightReceipt:
        with open_v3_connection(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? AND event_kind=? "
                "ORDER BY global_sequence",
                (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
            ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            event = EventEnvelope.from_dict(json.loads(str(row[0])))
            payload = cast(InlinePayload, event.command.payload).to_value()
            if (
                payload.get("schema_version") == "strathmark-v3-credibility-weights-event-v1"
                and payload.get("source_sequence") == source_sequence
            ):
                matches.append(payload)
        if len(matches) != 1:
            raise CredibilityReactionError(
                "completed credibility reaction lacks one weight receipt"
            )
        payload = matches[0]
        context = _context_from_dict(payload["context"])
        weights = tuple((AssessorKind(item), value) for item, value in payload["weights"])
        components = tuple(
            WeightComponent(
                assessor=AssessorKind(item["assessor"]),
                predictive_loss=item["predictive_loss"],
                shrunk_loss=item["shrunk_loss"],
                raw_credibility=item["raw_credibility"],
                n_eff=item["n_eff"],
                coverage_rate=item["coverage_rate"],
                effective_floor=item["effective_floor"],
                effective_cap=item["effective_cap"],
                health=item["health"],
            )
            for item in payload["components"]
        )
        receipt = WeightReceipt(
            context,
            weights,
            components,
            payload["calibration_cutoff_at_utc"],
            payload["policy_digest"],
            payload["receipt_digest"],
        )
        content = {
            "context": context.to_dict(),
            "weights": [(item.value, value) for item, value in weights],
            "components": payload["components"],
            "calibration_cutoff_at_utc": receipt.calibration_cutoff_at_utc,
            "policy_digest": receipt.policy_digest,
        }
        if receipt.receipt_digest != canonical_digest(content):
            raise CredibilityReactionError("persisted credibility weight receipt digest differs")
        return receipt

    def _append_credibility_record(
        self,
        record_type: str,
        record: Mapping[str, Any],
        *,
        aggregate_id: StableIdentifier,
        event_kind: EventKind,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
    ) -> StoredCommandResult:
        payload = {
            "schema_version": "strathmark-v3-credibility-authority-event-v1",
            "record_type": record_type,
            "record": dict(record),
        }
        digest = canonical_digest(payload)
        return self._append_event(
            command_kind=CommandKind.RECORD_SCORE,
            event_kind=event_kind,
            aggregate_kind=AggregateKind.SCORE,
            aggregate_id=aggregate_id,
            payload=payload,
            result={"record_digest": digest},
            command_id=IdempotencyKey(f"command:{digest}"),
            actor_id=actor_id,
            occurred_at_utc=occurred_at_utc,
            monotonic_elapsed_ms=monotonic_elapsed_ms,
        )

    def _append_event(
        self,
        *,
        command_kind: CommandKind,
        event_kind: EventKind,
        aggregate_kind: AggregateKind,
        aggregate_id: StableIdentifier,
        payload: Mapping[str, Any],
        result: Mapping[str, Any],
        command_id: IdempotencyKey,
        actor_id: StableIdentifier,
        occurred_at_utc: str,
        monotonic_elapsed_ms: int,
        projection_hook: Callable[[sqlite3.Connection, tuple[EventEnvelope, ...]], None]
        | None = None,
    ) -> StoredCommandResult:
        retry = self._events.lookup_exact_retry(
            principal_id=str(actor_id),
            idempotency_key=str(command_id),
            command_kind=command_kind,
            target_aggregate=str(aggregate_id),
            payload_digest=InlinePayload.from_value(payload).digest,
        )
        if retry is not None:
            return retry
        head = self._events.aggregate_head(str(aggregate_id))
        command = CommandEnvelope(
            command_kind,
            command_id,
            aggregate_id,
            ((str(aggregate_id), 0 if head is None else head[0]),),
            actor_id,
            InlinePayload.from_value(payload),
        )
        return self._events.execute(
            CommandRequest(
                actor_id,
                command,
                (EventIntent(aggregate_kind, aggregate_id, event_kind),),
                "strathmark-v3-credibility-command-result-v1",
                result,
                occurred_at_utc,
                monotonic_elapsed_ms,
            ),
            projection_hook=projection_hook,
        )


def _optimizer_scoring_input(**arguments: Any) -> OptimizerScoringInput:
    content = {
        "schema_version": "strathmark-v3-optimizer-scoring-input-v1",
        **arguments,
        "context": arguments["context"].to_dict(),
        "field_results": [item.to_dict() for item in arguments["field_results"]],
        "field_forecasts": [item.to_dict() for item in arguments["field_forecasts"]],
    }
    return OptimizerScoringInput(**arguments, scoring_input_digest=canonical_digest(content))


def _encode_weight_receipt(receipt: WeightReceipt) -> dict[str, object]:
    return {
        "context": receipt.context.to_dict(),
        "weights": [(item.value, value) for item, value in receipt.weights],
        "components": [
            {
                name: (
                    getattr(component, name).value
                    if name == "assessor"
                    else getattr(component, name)
                )
                for name in component.__dataclass_fields__
            }
            for component in receipt.components
        ],
        "calibration_cutoff_at_utc": receipt.calibration_cutoff_at_utc,
        "policy_digest": receipt.policy_digest,
        "receipt_digest": receipt.receipt_digest,
    }


def _decode_weight_receipt(value: Mapping[str, Any]) -> WeightReceipt:
    context = _context_from_dict(value["context"])
    weights = tuple((AssessorKind(item), weight) for item, weight in value["weights"])
    components = tuple(
        WeightComponent(
            assessor=AssessorKind(item["assessor"]),
            predictive_loss=item["predictive_loss"],
            shrunk_loss=item["shrunk_loss"],
            raw_credibility=item["raw_credibility"],
            n_eff=item["n_eff"],
            coverage_rate=item["coverage_rate"],
            effective_floor=item["effective_floor"],
            effective_cap=item["effective_cap"],
            health=item["health"],
        )
        for item in value["components"]
    )
    receipt = WeightReceipt(
        context,
        weights,
        components,
        value["calibration_cutoff_at_utc"],
        value["policy_digest"],
        value["receipt_digest"],
    )
    content = dict(value)
    content.pop("receipt_digest")
    if receipt.receipt_digest != canonical_digest(content):
        raise CredibilityReactionError("persisted baseline receipt digest differs")
    return receipt


def _support_from_packet(packet: EvidencePacket) -> EvidenceSupport:
    eligible = len(packet.eligible_raw_times_ms)
    exact = sum(
        item.context.digest == packet.target_context.digest
        for item in packet.observations
        if admit_raw_completion(item.result) is not None
    )
    return EvidenceSupport(
        eligible,
        str(eligible),
        exact,
        packet.historical_cutoff_key,
        packet.tournament_event_sequence,
    )


def _candidate_failure_kind(code: str | None) -> str | None:
    if code is None:
        return None
    if "deadline" in code or "timeout" in code:
        return "deadline_miss"
    if "runtime" in code:
        return "runtime_failure"
    if any(token in code for token in ("transport", "cloud", "local", "provider")):
        return "transport_failure"
    return None


def _candidate_report_value(report: CandidateEvaluationReport) -> dict[str, object]:
    outcomes = []
    for item in report.outcomes:
        validated = item.validated
        outcomes.append(
            {
                "member_id": item.member_id,
                "provider_kind": item.provider_kind.value,
                "family": item.family,
                "evidence_digest": item.evidence_digest,
                "validated": (
                    None
                    if validated is None
                    else {
                        "valid": validated.valid,
                        "validator_code": validated.validator_code,
                        "distribution": (
                            None
                            if validated.distribution is None
                            else validated.distribution.to_dict()
                        ),
                        "evidence_refs": validated.evidence_refs,
                        "warnings": validated.warnings,
                        "fact_codes": validated.fact_codes,
                        "abstention_reason": validated.abstention_reason,
                    }
                ),
                "reliability_weight": item.reliability_weight,
                "context_weight": item.context_weight,
                "audit": None if item.audit is None else item.audit.to_dict(),
                "artifacts": [artifact.to_dict() for artifact in item.artifacts],
                "unavailable_code": item.unavailable_code,
                "execution_audit": (
                    None if item.execution_audit is None else item.execution_audit.to_dict()
                ),
            }
        )
    return {
        "authority_class": report.authority_class,
        "candidate_status": report.candidate_status.value,
        "availability": report.availability.value,
        "valid_member_count": report.valid_member_count,
        "diagnostic_distribution": (
            None
            if report.diagnostic_distribution is None
            else report.diagnostic_distribution.to_dict()
        ),
        "member_weights": report.member_weights,
        "outcomes": outcomes,
        "sealed_member_receipts": [
            {
                "member_id": member_id,
                "candidate_manifest_digest": receipt.candidate_manifest_digest,
                "receipt_digest": receipt.receipt_digest,
                "authority_class": receipt.authority_class,
            }
            for member_id, receipt in report.sealed_member_receipts
        ],
    }


def _context_from_observation(observation: ResultObservation, *, history_count: int) -> ContextNode:
    size_floor = (observation.context.size_mm // 50) * 50
    history_depth = (
        "zero"
        if history_count == 0
        else ("sparse" if history_count < 5 else "medium" if history_count < 20 else "deep")
    )
    return ContextNode(
        observation.context.event_code,
        f"{size_floor}_{size_floor + 49}",
        observation.context.material_code,
        history_depth,
    )


def _derived_difficulty(history_count: int) -> str:
    if isinstance(history_count, bool) or not isinstance(history_count, int) or history_count < 0:
        raise CredibilityReactionError("history support must be a nonnegative integer")
    value = Decimal(1) + Decimal(4) / Decimal(history_count + 1)
    return format(value, "f").rstrip("0").rstrip(".")


def _make_opportunity(
    *,
    assessor: AssessorKind,
    scope: ScoreScope,
    forecast_digest: str,
    observation: ResultObservation,
    result_id: str,
    result_revision: int,
    source_sequence: int,
    context: ContextNode,
    outcome: OpportunityOutcome,
    difficulty: str,
    member_id: str | None,
) -> Opportunity:
    identity = {
        "assessor": assessor.value,
        "member_id": member_id,
        "result_id": result_id,
        "result_revision": result_revision,
        "competitor_id": str(observation.competitor_id),
        "scope": scope.value,
    }
    return Opportunity.create(
        opportunity_id=str(deterministic_identifier("opportunity", identity)),
        scope=scope,
        assessor=assessor,
        forecast_digest=forecast_digest,
        result_id=result_id,
        result_revision=result_revision,
        source_sequence=source_sequence,
        context=context,
        eligible_at_forecast=True,
        outcome=outcome,
        difficulty=difficulty,
        member_id=member_id,
    )


def _score_aggregate(target_kind: str, target_id: str) -> StableIdentifier:
    return deterministic_identifier("score", {"target_kind": target_kind, "target_id": target_id})


def _context_from_dict(value: Any) -> ContextNode:
    if not isinstance(value, Mapping) or set(value) != {
        "event_code",
        "size_band",
        "material_group",
        "history_depth",
    }:
        raise CredibilityReactionError("persisted credibility context is malformed")
    return ContextNode(
        value["event_code"],
        value["size_band"],
        value["material_group"],
        value["history_depth"],
    )


def _opportunity_from_dict(value: Mapping[str, Any]) -> Opportunity:
    expected = {
        "schema_version",
        "opportunity_id",
        "scope",
        "assessor",
        "forecast_digest",
        "result_id",
        "result_revision",
        "source_sequence",
        "context",
        "eligible_at_forecast",
        "outcome",
        "difficulty",
        "member_id",
        "event_digest",
    }
    if set(value) != expected or value["schema_version"] != (
        "strathmark-v3-coverage-opportunity-v1"
    ):
        raise CredibilityReactionError("persisted opportunity schema differs")
    return Opportunity(
        opportunity_id=value["opportunity_id"],
        scope=ScoreScope(value["scope"]),
        assessor=AssessorKind(value["assessor"]),
        forecast_digest=value["forecast_digest"],
        result_id=value["result_id"],
        result_revision=value["result_revision"],
        source_sequence=value["source_sequence"],
        context=_context_from_dict(value["context"]),
        eligible_at_forecast=value["eligible_at_forecast"],
        outcome=OpportunityOutcome(value["outcome"]),
        difficulty=value["difficulty"],
        event_digest=value["event_digest"],
        member_id=value["member_id"],
    )


def _metrics_from_dict(value: Any) -> PredictiveMetrics:
    if not isinstance(value, Mapping) or set(value) != {
        "crps_ms",
        "normalized_crps",
        "median_absolute_error_ms",
        "median_bias_ms",
        "tail_loss_ms",
        "central_interval_covered",
        "sharpness_ms",
        "calibration_residual",
    }:
        raise CredibilityReactionError("persisted predictive metrics are malformed")
    return PredictiveMetrics(**value)


def _consequence_from_dict(value: Any) -> OptimizerConsequenceReceipt:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "evaluator_port",
        "forecast_digest",
        "result_revision_digest",
        "field_receipt_digest",
        "scoring_input_digest",
        "optimizer_bundle_digest",
        "status",
        "metrics",
        "authority_manifest_digest",
        "receipt_digest",
    }:
        raise CredibilityReactionError("persisted consequence receipt is malformed")
    metrics = value["metrics"]
    if metrics is not None and (
        not isinstance(metrics, Mapping)
        or set(metrics)
        != {
            "spread_ms",
            "win_probability_distortion",
            "class_context_bias_ms",
            "gap_error_ms",
            "breakout_exposure",
            "optimizer_repair",
        }
    ):
        raise CredibilityReactionError("persisted consequence metrics are malformed")
    return OptimizerConsequenceReceipt(
        evaluator_port=value["evaluator_port"],
        forecast_digest=value["forecast_digest"],
        result_revision_digest=value["result_revision_digest"],
        field_receipt_digest=value["field_receipt_digest"],
        scoring_input_digest=value["scoring_input_digest"],
        optimizer_bundle_digest=value["optimizer_bundle_digest"],
        status=ConsequenceStatus(value["status"]),
        metrics=None if metrics is None else HandicapConsequenceMetrics(**metrics),
        authority_manifest_digest=value["authority_manifest_digest"],
        receipt_digest=value["receipt_digest"],
    )


def _score_from_dict(value: Mapping[str, Any]) -> PredictiveScore:
    expected = {
        "schema_version",
        "score_id",
        "scope",
        "assessor",
        "forecast_digest",
        "result_id",
        "result_revision",
        "source_sequence",
        "context",
        "evidence_weight",
        "metrics",
        "consequence",
        "settled_at_utc",
        "event_digest",
    }
    if set(value) != expected or value["schema_version"] != ("strathmark-v3-predictive-score-v1"):
        raise CredibilityReactionError("persisted score schema differs")
    return PredictiveScore(
        score_id=value["score_id"],
        scope=ScoreScope(value["scope"]),
        assessor=AssessorKind(value["assessor"]),
        forecast_digest=value["forecast_digest"],
        result_id=value["result_id"],
        result_revision=value["result_revision"],
        source_sequence=value["source_sequence"],
        context=_context_from_dict(value["context"]),
        evidence_weight=value["evidence_weight"],
        metrics=_metrics_from_dict(value["metrics"]),
        consequence=_consequence_from_dict(value["consequence"]),
        settled_at_utc=value["settled_at_utc"],
        event_digest=value["event_digest"],
    )


def _reversal_from_dict(value: Mapping[str, Any]) -> LedgerReversal:
    expected = {
        "schema_version",
        "reversal_id",
        "target_kind",
        "target_id",
        "original_result_revision",
        "replacement_result_revision",
        "source_sequence",
        "event_digest",
    }
    if set(value) != expected or value["schema_version"] != "strathmark-v3-score-reversal-v1":
        raise CredibilityReactionError("persisted reversal schema differs")
    return LedgerReversal(
        reversal_id=value["reversal_id"],
        target_kind=value["target_kind"],
        target_id=value["target_id"],
        original_result_revision=value["original_result_revision"],
        replacement_result_revision=value["replacement_result_revision"],
        source_sequence=value["source_sequence"],
        event_digest=value["event_digest"],
    )


def _positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CredibilityReactionError(f"{label} must be a positive integer")


def _positive_decimal(value: str, label: str) -> None:
    try:
        parsed = Decimal(value)
    except (TypeError, ValueError) as exc:
        raise CredibilityReactionError(f"{label} must be a positive decimal string") from exc
    if not isinstance(value, str) or not parsed.is_finite() or parsed <= 0:
        raise CredibilityReactionError(f"{label} must be a positive decimal string")


def _digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CredibilityReactionError(f"{label} must be a lowercase SHA-256 digest")


__all__ = [
    "CredibilityReactionError",
    "OptimizerConsequenceEvaluatorPort",
    "OptimizerScoringInput",
    "SQLiteCredibilityReactionService",
    "SealedForecastCommit",
    "seal_forecast_commit",
]
