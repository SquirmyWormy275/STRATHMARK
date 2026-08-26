"""Frozen, one-use causal evaluation for V3 factory candidates."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from strathmark.v3.contracts.canonical import (
    canonical_bytes,
    canonical_decimal_string,
    canonical_digest,
)
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.domain.capability import CapabilityPromotionEvaluation
from strathmark.v3.domain.credibility import SelectiveAbstentionEvaluation
from strathmark.v3.domain.disagreement import (
    DisagreementPolicy,
    DisjointThresholdVerification,
    HistoricalThresholdSelection,
    freeze_disagreement_policy,
    verify_disjoint_thresholds,
)
from strathmark.v3.factory.candidates import (
    CandidateBundle,
    CandidateError,
    CloudModelIdentity,
    cloud_model_identity,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
_COMPARATORS = frozenset({"gte", "lte"})
_MAX_AUDIT_CONSUMPTION_RECORD_BYTES = 512 * 1024


class EvaluationError(RuntimeError):
    """A frozen evaluation or audit-isolation rule failed closed."""


class FactoryServiceRole(str, Enum):
    BUILDER = "builder"
    EVALUATOR = "evaluator"
    BUNDLE_SIGNER = "bundle_signer"
    APPLICATION = "application"


@dataclass(frozen=True, slots=True, order=True)
class IsolationProbe:
    role: FactoryServiceRole
    principal_id: str
    can_read_candidate_inputs: bool
    can_write_candidate_artifacts: bool
    can_read_locked_audit: bool
    can_read_raw_audit_rows: bool
    can_use_bundle_private_key: bool
    network_allowed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.role, FactoryServiceRole):
            raise EvaluationError("isolation probe role must use the closed vocabulary")
        _token(self.principal_id, "OS service principal")
        capabilities = (
            self.can_read_candidate_inputs,
            self.can_write_candidate_artifacts,
            self.can_read_locked_audit,
            self.can_read_raw_audit_rows,
            self.can_use_bundle_private_key,
            self.network_allowed,
        )
        if any(not isinstance(value, bool) for value in capabilities):
            raise EvaluationError("isolation probe capabilities must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "principal_id": self.principal_id,
            "can_read_candidate_inputs": self.can_read_candidate_inputs,
            "can_write_candidate_artifacts": self.can_write_candidate_artifacts,
            "can_read_locked_audit": self.can_read_locked_audit,
            "can_read_raw_audit_rows": self.can_read_raw_audit_rows,
            "can_use_bundle_private_key": self.can_use_bundle_private_key,
            "network_allowed": self.network_allowed,
        }


@dataclass(frozen=True, slots=True)
class FactoryIsolationAttestation:
    host_id: str
    probes: tuple[IsolationProbe, ...]
    observed_at: str
    probe_evidence_digest: str
    attestation_digest: str

    def __post_init__(self) -> None:
        _token(self.host_id, "factory host")
        require_utc_milliseconds(self.observed_at)
        _digest(self.probe_evidence_digest, "OS isolation evidence")
        if not isinstance(self.probes, tuple) or self.probes != tuple(
            sorted(self.probes, key=lambda item: item.role.value)
        ):
            raise EvaluationError("factory isolation probes must be immutable and sorted")
        if {item.role for item in self.probes} != set(FactoryServiceRole):
            raise EvaluationError("factory isolation must probe every service role")
        if len({item.principal_id for item in self.probes}) != len(self.probes):
            raise EvaluationError("factory service roles require distinct OS principals")
        by_role = {item.role: item for item in self.probes}
        builder = by_role[FactoryServiceRole.BUILDER]
        evaluator = by_role[FactoryServiceRole.EVALUATOR]
        signer = by_role[FactoryServiceRole.BUNDLE_SIGNER]
        app = by_role[FactoryServiceRole.APPLICATION]
        if (
            not builder.can_read_candidate_inputs
            or not builder.can_write_candidate_artifacts
            or builder.can_read_locked_audit
            or builder.can_read_raw_audit_rows
            or builder.can_use_bundle_private_key
            or builder.network_allowed
        ):
            raise EvaluationError("builder OS boundary can read audit/signing material")
        if (
            not evaluator.can_read_candidate_inputs
            or evaluator.can_write_candidate_artifacts
            or not evaluator.can_read_locked_audit
            or not evaluator.can_read_raw_audit_rows
            or evaluator.can_use_bundle_private_key
            or evaluator.network_allowed
        ):
            raise EvaluationError(
                "evaluator OS boundary permits candidate writes, signing, or network"
            )
        if (
            signer.can_read_candidate_inputs
            or signer.can_write_candidate_artifacts
            or signer.can_read_locked_audit
            or signer.can_read_raw_audit_rows
            or not signer.can_use_bundle_private_key
            or signer.network_allowed
        ):
            raise EvaluationError("bundle signer OS boundary can access raw audit or network")
        if (
            app.can_write_candidate_artifacts
            or app.can_read_locked_audit
            or app.can_read_raw_audit_rows
            or app.can_use_bundle_private_key
        ):
            raise EvaluationError("ordinary application identity can access factory authority")
        if self.attestation_digest != canonical_digest(self.body()):
            raise EvaluationError("factory isolation attestation digest differs")

    @classmethod
    def create(
        cls,
        *,
        host_id: str,
        probes: tuple[IsolationProbe, ...],
        observed_at: str,
        probe_evidence_digest: str,
    ) -> FactoryIsolationAttestation:
        ordered = tuple(sorted(probes, key=lambda item: item.role.value))
        body = {
            "schema_version": "strathmark-v3-factory-isolation-attestation-v1",
            "host_id": host_id,
            "probes": [item.to_dict() for item in ordered],
            "observed_at": observed_at,
            "probe_evidence_digest": probe_evidence_digest,
        }
        return cls(
            host_id,
            ordered,
            observed_at,
            probe_evidence_digest,
            canonical_digest(body),
        )

    def body(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-factory-isolation-attestation-v1",
            "host_id": self.host_id,
            "probes": [item.to_dict() for item in self.probes],
            "observed_at": self.observed_at,
            "probe_evidence_digest": self.probe_evidence_digest,
        }


@dataclass(frozen=True, slots=True, order=True)
class EvaluationGate:
    name: str
    comparator: str
    threshold: float

    def __post_init__(self) -> None:
        _token(self.name, "evaluation gate")
        if self.comparator not in _COMPARATORS:
            raise EvaluationError("evaluation gate comparator is unsupported")
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float)):
            raise EvaluationError("evaluation gate threshold must be numeric")
        numeric = float(self.threshold)
        if numeric != numeric or numeric in {float("inf"), float("-inf")}:
            raise EvaluationError("evaluation gate threshold must be finite")
        object.__setattr__(self, "threshold", numeric)

    def passes(self, value: float) -> bool:
        return value >= self.threshold if self.comparator == "gte" else value <= self.threshold

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "comparator": self.comparator,
            "threshold": canonical_decimal_string(self.threshold),
        }


@dataclass(frozen=True, slots=True)
class FrozenEvaluationHarness:
    generation_id: str
    audit_snapshot_digest: str
    harness_code_digest: str
    precommit_digest: str
    gates: tuple[EvaluationGate, ...]
    frozen_at: str
    harness_digest: str
    selection_metric: str | None = None

    def __post_init__(self) -> None:
        _token(self.generation_id, "audit generation")
        for value, label in (
            (self.audit_snapshot_digest, "audit snapshot"),
            (self.harness_code_digest, "harness code"),
            (self.precommit_digest, "evaluation precommit"),
        ):
            _digest(value, label)
        require_utc_milliseconds(self.frozen_at)
        if not isinstance(self.gates, tuple) or not self.gates:
            raise EvaluationError("frozen harness requires immutable gates")
        if self.gates != tuple(sorted(self.gates, key=lambda item: item.name)):
            raise EvaluationError("frozen evaluation gates must be uniquely sorted")
        if len({item.name for item in self.gates}) != len(self.gates):
            raise EvaluationError("frozen evaluation gates cannot repeat a name")
        if self.selection_metric is not None:
            _token(self.selection_metric, "cloud selection metric")
            if self.selection_metric not in {item.name for item in self.gates}:
                raise EvaluationError("cloud selection metric must name one frozen gate")
        object.__setattr__(self, "harness_digest", canonical_digest(self.body()))

    @classmethod
    def create(
        cls,
        *,
        generation_id: str,
        audit_snapshot_digest: str,
        harness_code_digest: str,
        precommit_digest: str,
        gates: tuple[EvaluationGate, ...],
        frozen_at: str,
        selection_metric: str | None = None,
    ) -> FrozenEvaluationHarness:
        ordered = tuple(sorted(gates, key=lambda item: item.name))
        return cls(
            generation_id,
            audit_snapshot_digest,
            harness_code_digest,
            precommit_digest,
            ordered,
            frozen_at,
            "0" * 64,
            selection_metric,
        )

    def body(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": "strathmark-v3-frozen-evaluation-harness-v1",
            "generation_id": self.generation_id,
            "audit_snapshot_digest": self.audit_snapshot_digest,
            "harness_code_digest": self.harness_code_digest,
            "precommit_digest": self.precommit_digest,
            "gates": [item.to_dict() for item in self.gates],
            "frozen_at": self.frozen_at,
        }
        if self.selection_metric is not None:
            value["selection_metric"] = self.selection_metric
        return value


@dataclass(frozen=True, slots=True)
class PromotionCalibrationEvidence:
    """Candidate-bound transitive authority required by manual bundle promotion."""

    candidate_digest: str
    capability: CapabilityPromotionEvaluation
    selective_abstention: SelectiveAbstentionEvaluation
    threshold_selection: HistoricalThresholdSelection
    threshold_verification: DisjointThresholdVerification
    disagreement_policy: DisagreementPolicy
    member_weight_authority_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _digest(self.candidate_digest, "promotion evidence candidate")
        _digest(self.member_weight_authority_digest, "promotion member-weight authority")
        if (
            not isinstance(self.capability, CapabilityPromotionEvaluation)
            or not self.capability.passed
            or self.capability.candidate_digest != self.candidate_digest
        ):
            raise EvaluationError("promotion capability evidence differs or failed")
        if (
            not isinstance(self.selective_abstention, SelectiveAbstentionEvaluation)
            or not self.selective_abstention.passed
            or self.selective_abstention.candidate_digest != self.candidate_digest
        ):
            raise EvaluationError("promotion selective-abstention evidence differs or failed")
        if (
            not isinstance(self.threshold_selection, HistoricalThresholdSelection)
            or not isinstance(self.threshold_verification, DisjointThresholdVerification)
            or not self.threshold_verification.passed
            or self.threshold_verification.selection_digest
            != self.threshold_selection.selection_digest
            or not isinstance(self.disagreement_policy, DisagreementPolicy)
            or self.disagreement_policy.replay_evidence_digest
            != self.threshold_selection.selection_digest
            or self.disagreement_policy.disjoint_verification_digest
            != self.threshold_verification.verification_digest
        ):
            raise EvaluationError("promotion disagreement threshold authority differs or failed")
        try:
            replayed_verification = verify_disjoint_thresholds(
                self.threshold_selection,
                self.threshold_verification.holdout_observations,
                minimum_accuracy=self.threshold_verification.minimum_accuracy,
            )
            replayed_policy = freeze_disagreement_policy(
                self.threshold_selection, replayed_verification
            )
        except ValueError as exc:
            raise EvaluationError("promotion disagreement threshold replay failed") from exc
        if (
            replayed_verification != self.threshold_verification
            or replayed_policy != self.disagreement_policy
        ):
            raise EvaluationError("promotion disagreement threshold replay differs")
        _digest(self.evidence_digest, "promotion calibration evidence")
        if self.evidence_digest != canonical_digest(self.body()):
            raise EvaluationError("promotion calibration evidence digest differs")

    @classmethod
    def create(
        cls,
        *,
        candidate_digest: str,
        capability: CapabilityPromotionEvaluation,
        selective_abstention: SelectiveAbstentionEvaluation,
        threshold_selection: HistoricalThresholdSelection,
        threshold_verification: DisjointThresholdVerification,
        disagreement_policy: DisagreementPolicy,
        member_weight_authority_digest: str,
    ) -> PromotionCalibrationEvidence:
        body = _promotion_evidence_body(
            candidate_digest,
            capability,
            selective_abstention,
            threshold_selection,
            threshold_verification,
            disagreement_policy,
            member_weight_authority_digest,
        )
        return cls(
            candidate_digest,
            capability,
            selective_abstention,
            threshold_selection,
            threshold_verification,
            disagreement_policy,
            member_weight_authority_digest,
            canonical_digest(body),
        )

    def body(self) -> dict[str, object]:
        return _promotion_evidence_body(
            self.candidate_digest,
            self.capability,
            self.selective_abstention,
            self.threshold_selection,
            self.threshold_verification,
            self.disagreement_policy,
            self.member_weight_authority_digest,
        )


def _promotion_evidence_body(
    candidate_digest: str,
    capability: CapabilityPromotionEvaluation,
    selective_abstention: SelectiveAbstentionEvaluation,
    threshold_selection: HistoricalThresholdSelection,
    threshold_verification: DisjointThresholdVerification,
    disagreement_policy: DisagreementPolicy,
    member_weight_authority_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-promotion-calibration-evidence-v1",
        "candidate_digest": candidate_digest,
        "capability_evaluation_digest": capability.evaluation_digest,
        "selective_abstention_evaluation_digest": selective_abstention.evaluation_digest,
        "threshold_selection_digest": threshold_selection.selection_digest,
        "threshold_verification_digest": threshold_verification.verification_digest,
        "disagreement_policy_digest": disagreement_policy.digest,
        "member_weight_authority_digest": member_weight_authority_digest,
    }


@dataclass(frozen=True, slots=True)
class SignedEvaluationReport:
    manifest: SignedManifest
    candidate_digest: str
    lineage_digest: str
    generation_id: str
    harness_digest: str
    passed: bool
    failed_gates: tuple[str, ...]
    public_summary: Mapping[str, float]
    promotion_evidence_digest: str | None = None
    promotion_authorized: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest, SignedManifest)
            or self.manifest.kind != "factory_evaluation"
        ):
            raise EvaluationError("evaluation report requires a signed factory manifest")
        for value, label in (
            (self.candidate_digest, "candidate"),
            (self.lineage_digest, "lineage"),
            (self.harness_digest, "harness"),
        ):
            _digest(value, label)
        _token(self.generation_id, "audit generation")
        if not isinstance(self.passed, bool):
            raise EvaluationError("evaluation pass state must be boolean")
        if not isinstance(self.failed_gates, tuple) or self.failed_gates != tuple(
            sorted(set(self.failed_gates))
        ):
            raise EvaluationError("failed gates must be uniquely sorted")
        if not isinstance(self.public_summary, Mapping):
            raise EvaluationError("evaluation summary must be a mapping")
        if self.promotion_evidence_digest is None:
            if self.promotion_authorized is not False:
                raise EvaluationError("diagnostic evaluation cannot authorize promotion")
        else:
            _digest(self.promotion_evidence_digest, "evaluation promotion evidence")
            if self.promotion_authorized is not self.passed:
                raise EvaluationError("promotion authority must follow the complete gate outcome")

    @property
    def report_digest(self) -> str:
        return self.manifest.body_digest


@dataclass(frozen=True, slots=True)
class CloudCandidateOutcome:
    """One comparable cloud result, including honest unavailability."""

    candidate_digest: str
    lineage_digest: str
    provider: str
    family: str
    model_id: str
    harness_digest: str
    metrics: Mapping[str, float] | None
    abstention_reason: str | None
    passed: bool
    failed_gates: tuple[str, ...]
    report_digest: str | None

    def __post_init__(self) -> None:
        for value, label in (
            (self.candidate_digest, "cloud candidate"),
            (self.lineage_digest, "cloud candidate lineage"),
            (self.harness_digest, "cloud candidate harness"),
        ):
            _digest(value, label)
        CloudModelIdentity(self.provider, self.family, self.model_id)
        if self.abstention_reason is None:
            if not isinstance(self.metrics, Mapping) or self.report_digest is None:
                raise EvaluationError("available cloud candidate requires metrics and report")
            _digest(self.report_digest, "cloud candidate report")
        else:
            _token(self.abstention_reason, "cloud candidate abstention")
            if self.metrics is not None or self.report_digest is not None or self.passed:
                raise EvaluationError("abstaining cloud candidate cannot carry numeric authority")
        if not isinstance(self.passed, bool):
            raise EvaluationError("cloud candidate pass state must be boolean")
        if not isinstance(self.failed_gates, tuple) or self.failed_gates != tuple(
            sorted(set(self.failed_gates))
        ):
            raise EvaluationError("cloud candidate failed gates must be uniquely sorted")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_digest": self.candidate_digest,
            "candidate_lineage_digest": self.lineage_digest,
            "provider": self.provider,
            "family": self.family,
            "model_id": self.model_id,
            "harness_digest": self.harness_digest,
            "metrics": (
                None
                if self.metrics is None
                else {
                    name: canonical_decimal_string(self.metrics[name])
                    for name in sorted(self.metrics)
                }
            ),
            "abstention_reason": self.abstention_reason,
            "passed": self.passed,
            "failed_gates": list(self.failed_gates),
            "report_digest": self.report_digest,
        }


@dataclass(frozen=True, slots=True)
class CloudChampionSelection:
    """Signed comparison result; deliberately grants no bundle promotion authority."""

    manifest: SignedManifest
    generation_id: str
    harness_digest: str
    selection_metric: str
    outcomes: tuple[CloudCandidateOutcome, ...]
    reports: tuple[SignedEvaluationReport, ...]
    selected_candidate_digest: str | None
    selected_provider: str | None
    selected_model_id: str | None
    promotion_authorized: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest, SignedManifest)
            or self.manifest.kind != "factory_cloud_champion_selection"
        ):
            raise EvaluationError("cloud selection requires a signed factory manifest")
        _token(self.generation_id, "cloud selection generation")
        _digest(self.harness_digest, "cloud selection harness")
        _token(self.selection_metric, "cloud selection metric")
        if not isinstance(self.outcomes, tuple) or self.outcomes != tuple(
            sorted(self.outcomes, key=lambda item: item.model_id)
        ):
            raise EvaluationError("cloud selection outcomes must be immutable and sorted")
        if not isinstance(self.reports, tuple) or self.reports != tuple(
            sorted(self.reports, key=lambda item: item.candidate_digest)
        ):
            raise EvaluationError("cloud selection reports must be immutable and sorted")
        if self.promotion_authorized is not False:
            raise EvaluationError("cloud selection cannot grant bundle promotion authority")
        selected = tuple(
            item
            for item in self.outcomes
            if item.candidate_digest == self.selected_candidate_digest
        )
        if self.selected_candidate_digest is None:
            if self.selected_provider is not None or self.selected_model_id is not None:
                raise EvaluationError("empty cloud selection cannot name provider or model")
        elif (
            len(selected) != 1
            or selected[0].provider != self.selected_provider
            or selected[0].model_id != self.selected_model_id
            or not selected[0].passed
        ):
            raise EvaluationError("cloud selection winner differs from comparable outcomes")

    @property
    def selected_report(self) -> SignedEvaluationReport | None:
        if self.selected_candidate_digest is None:
            return None
        return next(
            (
                report
                for report in self.reports
                if report.candidate_digest == self.selected_candidate_digest
            ),
            None,
        )


class AuditGenerationRegistry:
    """Durable no-clobber consumption markers owned by the evaluator identity."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)

    def consume(
        self,
        harness: FrozenEvaluationHarness,
        candidate: CandidateBundle,
        *,
        consumed_at: str,
    ) -> Path:
        require_utc_milliseconds(consumed_at)
        record = {
            "schema_version": "strathmark-v3-audit-generation-consumption-v1",
            "generation_id": harness.generation_id,
            "audit_snapshot_digest": harness.audit_snapshot_digest,
            "harness_digest": harness.harness_digest,
            "candidate_lineage_digest": candidate.lineage_digest,
            "candidate_digest": candidate.candidate_digest,
            "consumed_at": consumed_at,
        }
        return self._publish(harness.generation_id, record)

    def consume_cloud_comparison(
        self,
        harness: FrozenEvaluationHarness,
        candidates: tuple[CandidateBundle, ...],
        *,
        consumed_at: str,
    ) -> Path:
        require_utc_milliseconds(consumed_at)
        ordered = tuple(sorted(candidates, key=lambda item: item.candidate_digest))
        if not ordered or len({item.candidate_digest for item in ordered}) != len(ordered):
            raise EvaluationError("cloud comparison candidates must be unique")
        record = {
            "schema_version": "strathmark-v3-audit-generation-consumption-v1",
            "generation_id": harness.generation_id,
            "audit_snapshot_digest": harness.audit_snapshot_digest,
            "harness_digest": harness.harness_digest,
            "comparison_kind": "cloud_champion",
            "candidate_lineage_digests": sorted(candidate.lineage_digest for candidate in ordered),
            "candidate_digests": [candidate.candidate_digest for candidate in ordered],
            "consumed_at": consumed_at,
        }
        return self._publish(harness.generation_id, record)

    def consume_evaluation(
        self,
        harness: FrozenEvaluationHarness,
        candidate: CandidateBundle,
        report: SignedEvaluationReport,
        *,
        consumed_at: str,
        request_digest: str,
        signer: P256Signer,
    ) -> Path:
        """Atomically commit one-use consumption and its already-signed result."""

        require_utc_milliseconds(consumed_at)
        _digest(request_digest, "evaluator request")
        if (
            not isinstance(report, SignedEvaluationReport)
            or report.candidate_digest != candidate.candidate_digest
            or report.lineage_digest != candidate.lineage_digest
            or report.generation_id != harness.generation_id
            or report.harness_digest != harness.harness_digest
            or report.manifest.key_id != signer.identity.key_id
        ):
            raise EvaluationError("audit result differs from its consumption authority")
        payload = {
            "schema_version": "strathmark-v3-audit-generation-evaluation-result-v1",
            "generation_id": harness.generation_id,
            "audit_snapshot_digest": harness.audit_snapshot_digest,
            "harness_digest": harness.harness_digest,
            "candidate_lineage_digest": candidate.lineage_digest,
            "candidate_digest": candidate.candidate_digest,
            "request_digest": request_digest,
            "report_manifest": report.manifest.to_dict(),
        }
        authority = sign_manifest(
            "factory_evaluation_consumption",
            payload,
            signer=signer,
            created_at=consumed_at,
        )
        record = {
            "schema_version": "strathmark-v3-audit-generation-evaluation-record-v1",
            "result_manifest": authority.to_dict(),
        }
        return self._publish(harness.generation_id, record)

    def recover_evaluation(
        self,
        harness: FrozenEvaluationHarness,
        candidate: CandidateBundle,
        *,
        request_digest: str,
        trust_store: IntegrityTrustStore,
    ) -> SignedEvaluationReport | None:
        """Recover only an exact, trusted result from the atomic consumption record."""

        _digest(request_digest, "evaluator request")
        record = self.record(harness.generation_id)
        if record is None:
            return None
        if (
            set(record) != {"schema_version", "result_manifest"}
            or record.get("schema_version") != "strathmark-v3-audit-generation-evaluation-record-v1"
        ):
            raise EvaluationError("audit generation has no recoverable evaluator result")
        expected = {
            "schema_version",
            "generation_id",
            "audit_snapshot_digest",
            "harness_digest",
            "candidate_lineage_digest",
            "candidate_digest",
            "request_digest",
            "report_manifest",
        }
        try:
            authority = SignedManifest.from_dict(record["result_manifest"])
            payload = verify_manifest(authority, trust_store)
        except (IntegrityError, TypeError, ValueError) as exc:
            raise EvaluationError("durable evaluator result is untrusted or malformed") from exc
        if (
            authority.kind != "factory_evaluation_consumption"
            or set(payload) != expected
            or payload.get("schema_version")
            != "strathmark-v3-audit-generation-evaluation-result-v1"
        ):
            raise EvaluationError("durable evaluator result schema differs")
        if (
            payload["request_digest"] != request_digest
            or payload["generation_id"] != harness.generation_id
            or payload["audit_snapshot_digest"] != harness.audit_snapshot_digest
            or payload["harness_digest"] != harness.harness_digest
            or payload["candidate_lineage_digest"] != candidate.lineage_digest
            or payload["candidate_digest"] != candidate.candidate_digest
        ):
            raise EvaluationError("durable evaluator result conflicts with request")
        try:
            manifest = SignedManifest.from_dict(payload["report_manifest"])
            report = evaluation_report_from_manifest(manifest)
            verified = verify_evaluation_report(
                report,
                trust_store=trust_store,
                expected_candidate=candidate,
                expected_harness=harness,
            )
        except (IntegrityError, EvaluationError, TypeError, ValueError) as exc:
            raise EvaluationError("durable evaluator result is untrusted or malformed") from exc
        if (
            verified.candidate_digest != payload["candidate_digest"]
            or verified.lineage_digest != payload["candidate_lineage_digest"]
            or verified.harness_digest != payload["harness_digest"]
            or verified.manifest.body().get("created_at") != authority.body().get("created_at")
        ):
            raise EvaluationError("durable evaluator result authority differs")
        return verified

    def _publish(self, generation_id: str, record: Mapping[str, object]) -> Path:
        payload = canonical_bytes(record)
        if not payload or len(payload) > _MAX_AUDIT_CONSUMPTION_RECORD_BYTES:
            raise EvaluationError("audit consumption record exceeds its bounded size")
        destination = self.root / f"{canonical_digest({'generation_id': generation_id})}.json"
        temporary = self.root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == "nt":
                _windows_publish_no_clobber(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError as exc:
                    raise EvaluationError("locked audit generation was already consumed") from exc
                descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        destination.chmod(0o444)
        return destination

    def record(self, generation_id: str) -> Mapping[str, object] | None:
        _token(generation_id, "audit generation")
        path = self.root / f"{canonical_digest({'generation_id': generation_id})}.json"
        try:
            with path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size <= 0 or size > _MAX_AUDIT_CONSUMPTION_RECORD_BYTES:
                    raise EvaluationError("audit consumption record exceeds its bounded size")
                raw = handle.read(_MAX_AUDIT_CONSUMPTION_RECORD_BYTES + 1)
        except FileNotFoundError:
            return None
        except EvaluationError:
            raise
        except Exception as exc:
            raise EvaluationError("audit consumption record is corrupt") from exc
        if len(raw) > _MAX_AUDIT_CONSUMPTION_RECORD_BYTES:
            raise EvaluationError("audit consumption record exceeds its bounded size")
        try:
            value = json.loads(raw)
        except Exception as exc:
            raise EvaluationError("audit consumption record is corrupt") from exc
        if not isinstance(value, dict) or canonical_bytes(value) != raw:
            raise EvaluationError("audit consumption record is not canonical")
        return MappingProxyType(value)


class FrozenEvaluator:
    """Evaluate only declared gate summaries, then irreversibly consume the audit role."""

    def __init__(
        self,
        harness: FrozenEvaluationHarness,
        registry: AuditGenerationRegistry,
        *,
        signer: P256Signer,
    ) -> None:
        if not isinstance(harness, FrozenEvaluationHarness):
            raise EvaluationError("evaluator requires a frozen harness")
        if not isinstance(registry, AuditGenerationRegistry):
            raise EvaluationError("evaluator requires a durable audit registry")
        if not callable(getattr(signer, "sign", None)) or not hasattr(signer, "identity"):
            raise EvaluationError("evaluator requires a separate signing identity")
        self.harness = harness
        self.registry = registry
        self.signer = signer

    def evaluate(
        self,
        candidate: CandidateBundle,
        *,
        metrics: Mapping[str, float],
        observed_audit_snapshot_digest: str,
        created_at: str,
        promotion_evidence: PromotionCalibrationEvidence | None = None,
        request_digest: str | None = None,
    ) -> SignedEvaluationReport:
        if not isinstance(candidate, CandidateBundle):
            raise EvaluationError("evaluator requires a closed candidate bundle")
        if self.harness.audit_snapshot_digest in {
            candidate.data_snapshot_digest,
            *(item.digest for item in candidate.role_snapshots),
        }:
            raise EvaluationError("locked audit role is not disjoint from candidate roles")
        if observed_audit_snapshot_digest != self.harness.audit_snapshot_digest:
            raise EvaluationError("observed audit snapshot differs from the frozen harness")
        normalized, failed = _evaluate_metrics(self.harness, metrics)
        if promotion_evidence is not None and (
            not isinstance(promotion_evidence, PromotionCalibrationEvidence)
            or promotion_evidence.candidate_digest != candidate.candidate_digest
        ):
            raise EvaluationError("promotion evidence candidate binding differs")
        require_utc_milliseconds(created_at)
        if self.registry.record(self.harness.generation_id) is not None:
            raise EvaluationError("locked audit generation was already consumed")
        report = _sign_evaluation_report(
            self.harness,
            candidate,
            normalized,
            failed,
            promotion_evidence=promotion_evidence,
            signer=self.signer,
            created_at=created_at,
        )
        if request_digest is None:
            request_digest = canonical_digest(
                {
                    "schema_version": "strathmark-v3-direct-evaluation-request-v1",
                    "candidate_digest": candidate.candidate_digest,
                    "harness_digest": self.harness.harness_digest,
                    "metrics": {
                        name: canonical_decimal_string(normalized[name])
                        for name in sorted(normalized)
                    },
                    "observed_audit_snapshot_digest": observed_audit_snapshot_digest,
                    "created_at": created_at,
                    "promotion_evidence_digest": (
                        None if promotion_evidence is None else promotion_evidence.evidence_digest
                    ),
                }
            )
        else:
            _digest(request_digest, "evaluator request")
        # Consumption and the exact signed result share one durable no-clobber record.
        # A crash after this publication can recover the result without reopening audit.
        self.registry.consume_evaluation(
            self.harness,
            candidate,
            report,
            consumed_at=created_at,
            request_digest=request_digest,
            signer=self.signer,
        )
        return report

    def select_cloud_champion(
        self,
        candidates: tuple[CandidateBundle, ...],
        *,
        metrics: Mapping[str, Mapping[str, float] | None],
        abstentions: Mapping[str, str | None],
        observed_audit_snapshot_digest: str,
        created_at: str,
    ) -> CloudChampionSelection:
        """Compare one exact GPT, Claude, and Gemini cohort without promoting it."""

        if self.harness.selection_metric is None:
            raise EvaluationError("cloud selection metric must be frozen before audit")
        ordered = _cloud_candidates(candidates)
        candidate_keys = tuple(candidate.candidate_digest for candidate, _identity in ordered)
        if (
            not isinstance(metrics, Mapping)
            or not isinstance(abstentions, Mapping)
            or tuple(sorted(metrics)) != tuple(sorted(candidate_keys))
            or tuple(sorted(abstentions)) != tuple(sorted(candidate_keys))
        ):
            raise EvaluationError("cloud comparison evidence must exactly cover every candidate")
        if observed_audit_snapshot_digest != self.harness.audit_snapshot_digest:
            raise EvaluationError("observed audit snapshot differs from the frozen harness")
        require_utc_milliseconds(created_at)

        prepared: list[
            tuple[
                CandidateBundle,
                CloudModelIdentity,
                dict[str, float] | None,
                tuple[str, ...],
                str | None,
            ]
        ] = []
        for candidate, identity in ordered:
            if self.harness.audit_snapshot_digest in {
                candidate.data_snapshot_digest,
                *(item.digest for item in candidate.role_snapshots),
            }:
                raise EvaluationError("locked audit role is not disjoint from candidate roles")
            reason = abstentions[candidate.candidate_digest]
            values = metrics[candidate.candidate_digest]
            if reason is not None:
                _token(reason, "cloud candidate abstention")
                if values is not None:
                    raise EvaluationError("abstaining cloud candidate cannot carry metrics")
                prepared.append((candidate, identity, None, (), reason))
                continue
            if values is None:
                raise EvaluationError("available cloud candidate requires frozen-gate metrics")
            normalized, failed = _evaluate_metrics(self.harness, values)
            prepared.append((candidate, identity, normalized, failed, None))

        self.registry.consume_cloud_comparison(
            self.harness,
            tuple(candidate for candidate, _identity in ordered),
            consumed_at=created_at,
        )
        reports: list[SignedEvaluationReport] = []
        outcomes: list[CloudCandidateOutcome] = []
        for candidate, identity, normalized, failed, reason in prepared:
            report = None
            if normalized is not None:
                report = _sign_evaluation_report(
                    self.harness,
                    candidate,
                    normalized,
                    failed,
                    signer=self.signer,
                    created_at=created_at,
                )
                reports.append(report)
            outcomes.append(
                CloudCandidateOutcome(
                    candidate.candidate_digest,
                    candidate.lineage_digest,
                    identity.provider,
                    identity.family,
                    identity.model_id,
                    self.harness.harness_digest,
                    None if normalized is None else MappingProxyType(normalized),
                    reason,
                    report.passed if report is not None else False,
                    report.failed_gates if report is not None else (),
                    report.report_digest if report is not None else None,
                )
            )
        frozen_outcomes = tuple(sorted(outcomes, key=lambda item: item.model_id))
        selected = _select_cloud_outcome(self.harness, frozen_outcomes)
        payload = _cloud_selection_payload(
            self.harness,
            frozen_outcomes,
            selected,
        )
        manifest = sign_manifest(
            "factory_cloud_champion_selection",
            payload,
            signer=self.signer,
            created_at=created_at,
        )
        return CloudChampionSelection(
            manifest,
            self.harness.generation_id,
            self.harness.harness_digest,
            self.harness.selection_metric,
            frozen_outcomes,
            tuple(sorted(reports, key=lambda item: item.candidate_digest)),
            None if selected is None else selected.candidate_digest,
            None if selected is None else selected.provider,
            None if selected is None else selected.model_id,
            False,
        )


def _sign_evaluation_report(
    harness: FrozenEvaluationHarness,
    candidate: CandidateBundle,
    normalized: Mapping[str, float],
    failed: tuple[str, ...],
    *,
    promotion_evidence: PromotionCalibrationEvidence | None = None,
    signer: P256Signer,
    created_at: str,
) -> SignedEvaluationReport:
    payload = {
        "schema_version": "strathmark-v3-factory-evaluation-report-v1",
        "candidate_digest": candidate.candidate_digest,
        "candidate_lineage_digest": candidate.lineage_digest,
        "generation_id": harness.generation_id,
        "audit_snapshot_digest": harness.audit_snapshot_digest,
        "harness_digest": harness.harness_digest,
        "harness_code_digest": harness.harness_code_digest,
        "precommit_digest": harness.precommit_digest,
        "gates": [item.to_dict() for item in harness.gates],
        "gate_results": [
            {
                "name": gate.name,
                "value": canonical_decimal_string(normalized[gate.name]),
                "passed": gate.name not in failed,
            }
            for gate in harness.gates
        ],
        "passed": not failed,
        "failed_gates": sorted(failed),
        "promotion_evidence_digest": (
            None if promotion_evidence is None else promotion_evidence.evidence_digest
        ),
        "promotion_authorized": promotion_evidence is not None and not failed,
    }
    manifest = sign_manifest("factory_evaluation", payload, signer=signer, created_at=created_at)
    return SignedEvaluationReport(
        manifest,
        candidate.candidate_digest,
        candidate.lineage_digest,
        harness.generation_id,
        harness.harness_digest,
        not failed,
        tuple(sorted(failed)),
        MappingProxyType(dict(normalized)),
        None if promotion_evidence is None else promotion_evidence.evidence_digest,
        promotion_evidence is not None and not failed,
    )


def evaluation_report_from_manifest(manifest: SignedManifest) -> SignedEvaluationReport:
    """Project a signed manifest into a typed report without trusting its signature."""

    try:
        payload = manifest.body().get("payload")
        if not isinstance(payload, dict):
            raise ValueError
        results = payload.get("gate_results")
        if not isinstance(results, list):
            raise ValueError
        summary = MappingProxyType({str(item["name"]): float(item["value"]) for item in results})
        return SignedEvaluationReport(
            manifest,
            payload["candidate_digest"],
            payload["candidate_lineage_digest"],
            payload["generation_id"],
            payload["harness_digest"],
            payload["passed"],
            tuple(payload["failed_gates"]),
            summary,
            payload["promotion_evidence_digest"],
            payload["promotion_authorized"],
        )
    except (KeyError, TypeError, ValueError, IntegrityError, EvaluationError) as exc:
        raise EvaluationError("evaluation report manifest is malformed") from exc


def verify_evaluation_report(
    report: SignedEvaluationReport,
    *,
    trust_store: IntegrityTrustStore,
    expected_candidate: CandidateBundle,
    expected_harness: FrozenEvaluationHarness,
) -> SignedEvaluationReport:
    if not isinstance(report, SignedEvaluationReport):
        raise EvaluationError("evaluation report must use the typed contract")
    try:
        payload = verify_manifest(report.manifest, trust_store)
    except IntegrityError as exc:
        raise EvaluationError("evaluation report signer is not trusted") from exc
    expected_fields = {
        "schema_version",
        "candidate_digest",
        "candidate_lineage_digest",
        "generation_id",
        "audit_snapshot_digest",
        "harness_digest",
        "harness_code_digest",
        "precommit_digest",
        "gates",
        "gate_results",
        "passed",
        "failed_gates",
        "promotion_evidence_digest",
        "promotion_authorized",
    }
    if set(payload) != expected_fields or payload["schema_version"] != (
        "strathmark-v3-factory-evaluation-report-v1"
    ):
        raise EvaluationError("evaluation report schema is not closed")
    if (
        payload["candidate_digest"] != expected_candidate.candidate_digest
        or payload["candidate_lineage_digest"] != expected_candidate.lineage_digest
    ):
        raise EvaluationError("evaluation report candidate binding differs")
    if (
        payload["generation_id"] != expected_harness.generation_id
        or payload["audit_snapshot_digest"] != expected_harness.audit_snapshot_digest
    ):
        raise EvaluationError("evaluation report audit snapshot binding differs")
    if (
        payload["harness_digest"] != expected_harness.harness_digest
        or payload["harness_code_digest"] != expected_harness.harness_code_digest
        or payload["precommit_digest"] != expected_harness.precommit_digest
        or payload["gates"] != [item.to_dict() for item in expected_harness.gates]
    ):
        raise EvaluationError("evaluation report frozen harness binding differs")
    results = payload["gate_results"]
    if not isinstance(results, list) or len(results) != len(expected_harness.gates):
        raise EvaluationError("evaluation report gate results are incomplete")
    summary: dict[str, float] = {}
    failed: list[str] = []
    for gate, result in zip(expected_harness.gates, results):
        if not isinstance(result, dict) or set(result) != {"name", "value", "passed"}:
            raise EvaluationError("evaluation report gate result is malformed")
        value = result["value"]
        if (
            result["name"] != gate.name
            or not isinstance(value, str)
            or canonical_decimal_string(value) != value
        ):
            raise EvaluationError("evaluation report gate identity or value differs")
        numeric = float(value)
        passed = gate.passes(numeric)
        if result["passed"] is not passed:
            raise EvaluationError("evaluation report gate outcome differs from frozen threshold")
        summary[gate.name] = numeric
        if not passed:
            failed.append(gate.name)
    expected_failed = sorted(failed)
    if payload["failed_gates"] != expected_failed or payload["passed"] is not (not failed):
        raise EvaluationError("evaluation report aggregate outcome differs")
    evidence_digest = payload["promotion_evidence_digest"]
    if evidence_digest is not None:
        _digest(evidence_digest, "evaluation promotion evidence")
    promotion_authorized = evidence_digest is not None and not failed
    if payload["promotion_authorized"] is not promotion_authorized:
        raise EvaluationError("evaluation report promotion authority differs")
    verified = SignedEvaluationReport(
        report.manifest,
        expected_candidate.candidate_digest,
        expected_candidate.lineage_digest,
        expected_harness.generation_id,
        expected_harness.harness_digest,
        not failed,
        tuple(expected_failed),
        MappingProxyType(summary),
        evidence_digest,
        promotion_authorized,
    )
    if verified != report:
        raise EvaluationError("evaluation report typed projection differs from signed authority")
    return verified


def verify_cloud_champion_selection(
    selection: CloudChampionSelection,
    *,
    trust_store: IntegrityTrustStore,
    expected_candidates: tuple[CandidateBundle, ...],
    expected_harness: FrozenEvaluationHarness,
) -> CloudChampionSelection:
    """Verify the signed cohort, every component report, and deterministic winner."""

    if not isinstance(selection, CloudChampionSelection):
        raise EvaluationError("cloud selection must use the typed contract")
    try:
        payload = verify_manifest(selection.manifest, trust_store)
    except IntegrityError as exc:
        raise EvaluationError("cloud selection signer is not trusted") from exc
    expected_fields = {
        "schema_version",
        "generation_id",
        "audit_snapshot_digest",
        "harness_digest",
        "harness_code_digest",
        "precommit_digest",
        "selection_metric",
        "selection_comparator",
        "outcomes",
        "selected_candidate_digest",
        "selected_provider",
        "selected_model_id",
        "promotion_authorized",
    }
    if set(payload) != expected_fields or payload["schema_version"] != (
        "strathmark-v3-cloud-champion-selection-v1"
    ):
        raise EvaluationError("cloud selection schema is not closed")
    if expected_harness.selection_metric is None:
        raise EvaluationError("cloud selection metric must be frozen before audit")
    selection_gate = next(
        gate for gate in expected_harness.gates if gate.name == expected_harness.selection_metric
    )
    if (
        payload["generation_id"] != expected_harness.generation_id
        or payload["audit_snapshot_digest"] != expected_harness.audit_snapshot_digest
        or payload["harness_digest"] != expected_harness.harness_digest
        or payload["harness_code_digest"] != expected_harness.harness_code_digest
        or payload["precommit_digest"] != expected_harness.precommit_digest
        or payload["selection_metric"] != expected_harness.selection_metric
        or payload["selection_comparator"] != selection_gate.comparator
        or payload["promotion_authorized"] is not False
    ):
        raise EvaluationError("cloud selection frozen harness or authority binding differs")

    ordered = _cloud_candidates(expected_candidates)
    report_by_candidate = {report.candidate_digest: report for report in selection.reports}
    if len(report_by_candidate) != len(selection.reports):
        raise EvaluationError("cloud selection reports repeat a candidate")
    values = payload["outcomes"]
    if not isinstance(values, list) or len(values) != len(ordered):
        raise EvaluationError("cloud selection outcomes are incomplete")
    outcomes: list[CloudCandidateOutcome] = []
    used_reports: set[str] = set()
    outcome_fields = {
        "candidate_digest",
        "candidate_lineage_digest",
        "provider",
        "family",
        "model_id",
        "harness_digest",
        "metrics",
        "abstention_reason",
        "passed",
        "failed_gates",
        "report_digest",
    }
    for (candidate, identity), value in zip(ordered, values, strict=True):
        if not isinstance(value, dict) or set(value) != outcome_fields:
            raise EvaluationError("cloud selection outcome schema is malformed")
        if (
            value["candidate_digest"] != candidate.candidate_digest
            or value["candidate_lineage_digest"] != candidate.lineage_digest
            or value["provider"] != identity.provider
            or value["family"] != identity.family
            or value["model_id"] != identity.model_id
            or value["harness_digest"] != expected_harness.harness_digest
        ):
            raise EvaluationError("cloud selection provider/model or candidate binding differs")
        reason = value["abstention_reason"]
        if reason is not None:
            _token(reason, "cloud candidate abstention")
            if (
                value["metrics"] is not None
                or value["report_digest"] is not None
                or value["passed"] is not False
                or value["failed_gates"] != []
                or candidate.candidate_digest in report_by_candidate
            ):
                raise EvaluationError("cloud selection abstention carries numeric authority")
            outcomes.append(
                CloudCandidateOutcome(
                    candidate.candidate_digest,
                    candidate.lineage_digest,
                    identity.provider,
                    identity.family,
                    identity.model_id,
                    expected_harness.harness_digest,
                    None,
                    reason,
                    False,
                    (),
                    None,
                )
            )
            continue
        report = report_by_candidate.get(candidate.candidate_digest)
        if report is None:
            raise EvaluationError("available cloud selection outcome has no signed report")
        verified_report = verify_evaluation_report(
            report,
            trust_store=trust_store,
            expected_candidate=candidate,
            expected_harness=expected_harness,
        )
        used_reports.add(candidate.candidate_digest)
        raw_metrics = value["metrics"]
        if not isinstance(raw_metrics, dict) or tuple(sorted(raw_metrics)) != tuple(
            gate.name for gate in expected_harness.gates
        ):
            raise EvaluationError("cloud selection metrics do not match frozen gates")
        normalized: dict[str, float] = {}
        for gate in expected_harness.gates:
            raw = raw_metrics[gate.name]
            if not isinstance(raw, str) or canonical_decimal_string(raw) != raw:
                raise EvaluationError("cloud selection metric is not canonical")
            normalized[gate.name] = float(raw)
        if (
            normalized != dict(verified_report.public_summary)
            or value["report_digest"] != verified_report.report_digest
            or value["passed"] is not verified_report.passed
            or value["failed_gates"] != list(verified_report.failed_gates)
        ):
            raise EvaluationError("cloud selection outcome differs from signed report")
        outcomes.append(
            CloudCandidateOutcome(
                candidate.candidate_digest,
                candidate.lineage_digest,
                identity.provider,
                identity.family,
                identity.model_id,
                expected_harness.harness_digest,
                MappingProxyType(normalized),
                None,
                verified_report.passed,
                verified_report.failed_gates,
                verified_report.report_digest,
            )
        )
    if used_reports != set(report_by_candidate):
        raise EvaluationError("cloud selection contains an unbound signed report")
    frozen_outcomes = tuple(sorted(outcomes, key=lambda item: item.model_id))
    selected = _select_cloud_outcome(expected_harness, frozen_outcomes)
    if (
        payload["selected_candidate_digest"]
        != (None if selected is None else selected.candidate_digest)
        or payload["selected_provider"] != (None if selected is None else selected.provider)
        or payload["selected_model_id"] != (None if selected is None else selected.model_id)
    ):
        raise EvaluationError("cloud selection winner differs from signed comparable evidence")
    verified = CloudChampionSelection(
        selection.manifest,
        expected_harness.generation_id,
        expected_harness.harness_digest,
        expected_harness.selection_metric,
        frozen_outcomes,
        tuple(sorted(selection.reports, key=lambda item: item.candidate_digest)),
        None if selected is None else selected.candidate_digest,
        None if selected is None else selected.provider,
        None if selected is None else selected.model_id,
        False,
    )
    if verified != selection:
        raise EvaluationError("cloud selection typed projection differs from signed authority")
    return verified


def _evaluate_metrics(
    harness: FrozenEvaluationHarness,
    metrics: Mapping[str, float],
) -> tuple[dict[str, float], tuple[str, ...]]:
    expected_names = tuple(item.name for item in harness.gates)
    if not isinstance(metrics, Mapping) or tuple(sorted(metrics)) != expected_names:
        raise EvaluationError("evaluation metrics must exactly match frozen gates")
    normalized: dict[str, float] = {}
    failed: list[str] = []
    for gate in harness.gates:
        value = metrics[gate.name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvaluationError("evaluation metric must be numeric")
        numeric = float(value)
        if numeric != numeric or numeric in {float("inf"), float("-inf")}:
            raise EvaluationError("evaluation metric must be finite")
        normalized[gate.name] = numeric
        if not gate.passes(numeric):
            failed.append(gate.name)
    return normalized, tuple(sorted(failed))


def _cloud_candidates(
    candidates: tuple[CandidateBundle, ...],
) -> tuple[tuple[CandidateBundle, CloudModelIdentity], ...]:
    if not isinstance(candidates, tuple) or len(candidates) != 3:
        raise EvaluationError("cloud comparison requires exactly GPT, Claude, and Gemini")
    try:
        identified = tuple((candidate, cloud_model_identity(candidate)) for candidate in candidates)
    except CandidateError as exc:
        raise EvaluationError("cloud comparison requires exactly GPT, Claude, and Gemini") from exc
    expected = {("anthropic", "claude"), ("google", "gemini"), ("openai", "gpt")}
    if {
        (identity.provider, identity.family) for _candidate, identity in identified
    } != expected or len({candidate.candidate_digest for candidate, _identity in identified}) != 3:
        raise EvaluationError("cloud comparison requires exactly GPT, Claude, and Gemini")
    return tuple(sorted(identified, key=lambda item: item[1].model_id))


def _select_cloud_outcome(
    harness: FrozenEvaluationHarness,
    outcomes: tuple[CloudCandidateOutcome, ...],
) -> CloudCandidateOutcome | None:
    if harness.selection_metric is None:
        raise EvaluationError("cloud selection metric must be frozen before audit")
    gate = next(item for item in harness.gates if item.name == harness.selection_metric)
    eligible = tuple(item for item in outcomes if item.passed and item.metrics is not None)
    if not eligible:
        return None
    if gate.comparator == "lte":
        return min(
            eligible, key=lambda item: (item.metrics[harness.selection_metric], item.model_id)
        )
    return min(eligible, key=lambda item: (-item.metrics[harness.selection_metric], item.model_id))


def _cloud_selection_payload(
    harness: FrozenEvaluationHarness,
    outcomes: tuple[CloudCandidateOutcome, ...],
    selected: CloudCandidateOutcome | None,
) -> dict[str, object]:
    if harness.selection_metric is None:
        raise EvaluationError("cloud selection metric must be frozen before audit")
    gate = next(item for item in harness.gates if item.name == harness.selection_metric)
    return {
        "schema_version": "strathmark-v3-cloud-champion-selection-v1",
        "generation_id": harness.generation_id,
        "audit_snapshot_digest": harness.audit_snapshot_digest,
        "harness_digest": harness.harness_digest,
        "harness_code_digest": harness.harness_code_digest,
        "precommit_digest": harness.precommit_digest,
        "selection_metric": harness.selection_metric,
        "selection_comparator": gate.comparator,
        "outcomes": [item.to_dict() for item in outcomes],
        "selected_candidate_digest": None if selected is None else selected.candidate_digest,
        "selected_provider": None if selected is None else selected.provider,
        "selected_model_id": None if selected is None else selected.model_id,
        "promotion_authorized": False,
    }


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EvaluationError(f"{label} digest must be lower-case SHA-256")
    return value


def _windows_publish_no_clobber(source: Path, destination: Path) -> None:
    import ctypes
    from ctypes import wintypes

    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    if move(str(source), str(destination), 0x8):
        return
    error = ctypes.get_last_error()
    if error in {80, 183}:
        raise EvaluationError("locked audit generation was already consumed")
    raise EvaluationError(f"Windows write-through audit consumption failed ({error})")


def _token(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise EvaluationError(f"{label} must be a bounded opaque token")
    return value


__all__ = [
    "AuditGenerationRegistry",
    "CloudCandidateOutcome",
    "CloudChampionSelection",
    "EvaluationError",
    "EvaluationGate",
    "evaluation_report_from_manifest",
    "FactoryIsolationAttestation",
    "FactoryServiceRole",
    "FrozenEvaluationHarness",
    "FrozenEvaluator",
    "IsolationProbe",
    "SignedEvaluationReport",
    "verify_cloud_champion_selection",
    "verify_evaluation_report",
]
