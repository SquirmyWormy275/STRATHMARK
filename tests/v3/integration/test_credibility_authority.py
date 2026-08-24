from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from threading import Barrier

import pytest

from strathmark.v3.application import credibility_reactions as reactions
from strathmark.v3.application.credibility_reactions import (
    CredibilityReactionError,
    InstalledOptimizerEvaluator,
    OptimizerScoringInput,
    SQLiteCredibilityReactionService,
    seal_credibility_policy,
    seal_forecast_commit,
    seal_optimizer_evaluator_authority,
)
from strathmark.v3.application.formula_governor import (
    FormulaProjectionFactory,
    seal_formula_governor_batch,
)
from strathmark.v3.application.lifecycle import SnapshotKind, UpstreamSnapshot
from strathmark.v3.assessors.base import AssessmentResult
from strathmark.v3.assessors.formula import FormulaManifest, assess_formula
from strathmark.v3.assessors.ml import (
    MLAssessment,
    MLAssessor,
    PITCalibrator,
    SpecialistGate,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import CommandKind
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.evidence import EvidencePacket, TargetContext
from strathmark.v3.contracts.forecasts import (
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.statuses import ResultStatus
from strathmark.v3.domain.credibility import (
    ConsequenceStatus,
    ContextNode,
    CredibilityPolicy,
    HandicapConsequenceMetrics,
    OptimizerConsequenceReceipt,
)
from strathmark.v3.domain.epochs import EvidenceEpoch, MandatoryReaction
from strathmark.v3.factory.ml_artifacts import LoadedMLBundle
from strathmark.v3.factory.ml_training import CATEGORICAL_FEATURES, FEATURE_NAMES
from strathmark.v3.infrastructure.integrity import (
    IntegrityTrustStore,
    P256EphemeralSigner,
    sign_manifest,
)
from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
from tests.v3.integration.test_derivation_barrier import (
    ACTOR,
    NOW,
    _append,
    _authority_event,
    _bootstrap,
    _bootstrap_empty_closure,
    _snapshot,
    _start_round_close,
    _submission,
)


class Evaluator:
    calls = 0
    inputs = []
    bundle_digest = "e" * 64
    implementation_digest = "1" * 64
    evaluator_port = "shared_optimizer_evaluator_v1"

    def evaluate(
        self, *, forecast: AssessorForecast, scoring_input: OptimizerScoringInput
    ) -> OptimizerConsequenceReceipt:
        type(self).calls += 1
        type(self).inputs.append(scoring_input)
        assert tuple(item.competitor_id for item in scoring_input.field_results) == (
            "competitor:a",
            "competitor:b",
        )
        completion_times = tuple(
            item.raw_time_ms for item in scoring_input.field_results if item.raw_time_ms is not None
        )
        consequence_spread = sum(completion_times) + len(scoring_input.field_results)
        return OptimizerConsequenceReceipt.create(
            forecast_digest=forecast.commit_digest,
            result_revision_digest=scoring_input.result_revision_digest,
            field_receipt_digest=scoring_input.field_receipt_digest,
            scoring_input_digest=scoring_input.scoring_input_digest,
            optimizer_bundle_digest=self.bundle_digest,
            metrics=HandicapConsequenceMetrics(
                consequence_spread,
                "0",
                0,
                len(scoring_input.field_forecasts),
                "0",
                False,
            ),
        )


class UntypedEvaluator:
    bundle_digest = Evaluator.bundle_digest
    implementation_digest = "2" * 64
    evaluator_port = Evaluator.evaluator_port

    def evaluate(self, *, forecast, scoring_input):
        return None


class MisboundEvaluator:
    bundle_digest = Evaluator.bundle_digest
    implementation_digest = "3" * 64
    evaluator_port = Evaluator.evaluator_port

    def evaluate(self, *, forecast, scoring_input):
        return OptimizerConsequenceReceipt.create(
            forecast_digest=forecast.commit_digest,
            result_revision_digest=scoring_input.result_revision_digest,
            field_receipt_digest=scoring_input.field_receipt_digest,
            scoring_input_digest="0" * 64,
            optimizer_bundle_digest=self.bundle_digest,
            metrics=HandicapConsequenceMetrics(0, "0", 0, 0, "0", False),
        )


def _policy(signer):
    return seal_credibility_policy(
        CredibilityPolicy(),
        optimizer_bundle_digest=Evaluator.bundle_digest,
        signer=signer,
        created_at=NOW,
    )


def _installed(evaluator, signer):
    return InstalledOptimizerEvaluator(
        evaluator,
        seal_optimizer_evaluator_authority(evaluator, signer=signer, created_at=NOW),
    )


def _forecast(
    number: int,
    assessor: AssessorKind,
    maximum_sequence: int,
    *,
    evidence_digest: str | None = None,
) -> AssessorForecast:
    return AssessorForecast.create(
        forecast_id=StableIdentifier(f"forecast:{assessor.value}-{number}"),
        assessor=assessor,
        state=ForecastState.COMMITTED,
        evidence_digest=evidence_digest or f"{number:064x}",
        distribution=PositiveTimeDistribution(
            (
                QuantilePoint("0.1", 10_000),
                QuantilePoint("0.5", 12_000),
                QuantilePoint("0.9", 14_000),
            )
        ),
        support=EvidenceSupport(3, "3", 1, "history:prior", maximum_sequence),
        warnings=(),
        artifacts=(),
        abstention_code=None,
    )


def _authority(tmp_path, evaluator=None):
    Evaluator.calls = 0
    Evaluator.inputs = []
    lifecycle, round_id, field_id = _bootstrap(tmp_path)
    issue = _authority_event(lifecycle, EventKind.FIELD_ISSUED, aggregate_id=str(field_id))
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        epoch = connection.execute(
            "SELECT epoch_id, epoch_digest, historical_cutoff_key, maximum_tournament_sequence "
            "FROM v3_evidence_epochs WHERE round_id=?",
            (str(round_id),),
        ).fetchone()
    assert epoch is not None
    signer = P256EphemeralSigner.generate("integrity-key:credibility-test")
    credibility = SQLiteCredibilityReactionService(
        lifecycle.projections.database_path,
        trust_store=IntegrityTrustStore((signer.identity,)),
        consequence_evaluator=_installed(evaluator or Evaluator(), signer),
        policy_manifest=_policy(signer),
    )
    return lifecycle, credibility, signer, field_id, issue, epoch


def _canonical_evidence(epoch):
    context = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1")
    return EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:a"),
        target_context=context,
        observations=(),
        taxonomy_version=context.taxonomy_version,
        conversion_version=context.conversion_version,
        historical_cutoff_key=str(epoch[2]),
        tournament_epoch_id=StableIdentifier(str(epoch[0])),
        tournament_event_sequence=int(epoch[3]),
    )


def _empty_live_authority(tmp_path):
    lifecycle, tournament, completed, successor, _closure = _bootstrap_empty_closure(tmp_path)
    signer = P256EphemeralSigner.generate("integrity-key:live-concurrency")
    credibility = SQLiteCredibilityReactionService(
        lifecycle.projections.database_path,
        trust_store=IntegrityTrustStore((signer.identity,)),
        consequence_evaluator=None,
        policy_manifest=_policy(signer),
    )
    return lifecycle, credibility, signer, tournament, completed, successor


class _MLModel:
    def predict(self, _rows):
        return [[2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8]]


def _real_ml_assessment(evidence, number=1):
    calibrator = PITCalibrator.identity(source_digest="c" * 64)
    bundle = LoadedMLBundle.for_testing(
        digest=f"{number:064x}",
        version=f"ml:credibility-test-{number}",
        universal_model=_MLModel(),
        specialist_models={},
        specialist_eligibility={},
        gate=SpecialistGate("0", (("log_history_depth", "0"), ("missing_fraction", "0"))),
        calibrator=calibrator,
        feature_names=FEATURE_NAMES,
        categorical_features=CATEGORICAL_FEATURES,
        vocabulary={
            "event_family": ("__other__", "underhand"),
            "species": ("__other__", "wood"),
        },
        taxonomy_version="tax:v1",
        conversion_version="convert:v1",
    )
    return MLAssessor(bundle).assess(evidence)


def _commit(credibility, signer, field_id, issue, epoch, assessor, number=1):
    if assessor is AssessorKind.FORMULA:
        _real_formula_commit(credibility, signer, field_id, issue, epoch, number=number)
        return
    if assessor is not AssessorKind.ML:
        raise AssertionError("integration helper supports operational Formula and ML only")
    evidence = _canonical_evidence(epoch)
    assessment = _real_ml_assessment(evidence, number)
    sealed = seal_forecast_commit(
        assessment.forecast,
        evidence_packet=evidence,
        assessor_input=None,
        assessor_receipt=assessment,
        field_id=field_id,
        competitor_id=StableIdentifier("competitor:a"),
        field_revision=1,
        evidence_epoch_id=StableIdentifier(str(epoch[0])),
        evidence_epoch_digest=str(epoch[1]),
        historical_cutoff_key=str(epoch[2]),
        receipt_id=StableIdentifier("receipt:heat-a"),
        issue_event_digest=issue.event_digest,
        signer=signer,
        created_at=NOW,
    )
    credibility.commit_forecast(
        sealed,
        command_id=IdempotencyKey(f"command:forecast-{assessor.value}-{number}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )


def _real_formula_material(signer, epoch):
    evidence = _canonical_evidence(epoch)
    epoch_value = EvidenceEpoch(
        StableIdentifier(str(epoch[0])),
        StableIdentifier("round:heat"),
        1,
        str(epoch[2]),
        int(epoch[3]),
        (),
        str(epoch[1]),
    )
    batch = seal_formula_governor_batch(
        evidence=evidence,
        epoch=epoch_value,
        cutoff_at_utc=NOW,
        active_tournament_id=StableIdentifier("tournament:show"),
        authoritative_tournament_ids=(),
        legacy_tournament_ids=(),
        live_authorities=(),
        historical_authorities=(),
        signer=signer,
        created_at=NOW,
    )
    formula_input = FormulaProjectionFactory(
        trust_store=IntegrityTrustStore((signer.identity,)),
        cutoff_at_utc=NOW,
        active_tournament_id=StableIdentifier("tournament:show"),
        authoritative_tournament_ids=(),
        legacy_tournament_ids=(),
    ).project(evidence=evidence, epoch=epoch_value, sealed_batch=batch)
    assessment = assess_formula(
        formula_input,
        FormulaManifest.load("benchmarks/v3/formula_manifest.json"),
    )
    return evidence, formula_input, assessment


def _real_formula_commit(credibility, signer, field_id, issue, epoch, *, number=1):
    evidence, formula_input, assessment = _real_formula_material(signer, epoch)
    sealed = seal_forecast_commit(
        assessment.forecast,
        evidence_packet=evidence,
        assessor_input=formula_input,
        assessor_receipt=assessment,
        field_id=field_id,
        competitor_id=StableIdentifier("competitor:a"),
        field_revision=1,
        evidence_epoch_id=StableIdentifier(str(epoch[0])),
        evidence_epoch_digest=str(epoch[1]),
        historical_cutoff_key=str(epoch[2]),
        receipt_id=StableIdentifier("receipt:heat-a"),
        issue_event_digest=issue.event_digest,
        signer=signer,
        created_at=NOW,
    )
    credibility.commit_forecast(
        sealed,
        command_id=IdempotencyKey(f"command:real-formula-{number}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    return evidence, formula_input, assessment


def test_real_formula_packet_and_assessment_are_the_evidence_authority(tmp_path):
    _lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    evidence, formula_input, assessment = _real_formula_commit(
        credibility, signer, field_id, issue, epoch
    )

    assert assessment.forecast.evidence_digest == formula_input.digest
    with pytest.raises(CredibilityReactionError, match="evidence|assessor|governor"):
        seal_forecast_commit(
            assessment.forecast,
            evidence_packet=evidence,
            assessor_input=None,
            assessor_receipt=assessment,
            field_id=field_id,
            competitor_id=StableIdentifier("competitor:a"),
            field_revision=1,
            evidence_epoch_id=StableIdentifier(str(epoch[0])),
            evidence_epoch_digest=str(epoch[1]),
            historical_cutoff_key=str(epoch[2]),
            receipt_id=StableIdentifier("receipt:heat-a"),
            issue_event_digest=issue.event_digest,
            signer=signer,
            created_at=NOW,
        )


def test_forecast_commit_conflict_is_closed_at_the_service_boundary(tmp_path, monkeypatch):
    _lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    evidence, formula_input, assessment = _real_formula_material(signer, epoch)
    sealed = seal_forecast_commit(
        assessment.forecast,
        evidence_packet=evidence,
        assessor_input=formula_input,
        assessor_receipt=assessment,
        field_id=field_id,
        competitor_id=StableIdentifier("competitor:a"),
        field_revision=1,
        evidence_epoch_id=StableIdentifier(str(epoch[0])),
        evidence_epoch_digest=str(epoch[1]),
        historical_cutoff_key=str(epoch[2]),
        receipt_id=StableIdentifier("receipt:heat-a"),
        issue_event_digest=issue.event_digest,
        signer=signer,
        created_at=NOW,
    )

    def conflict(**_arguments):
        raise reactions.EventStoreConflict("forced concurrent forecast conflict")

    monkeypatch.setattr(credibility, "_append_event", conflict)
    with pytest.raises(CredibilityReactionError, match="duplicate or conflicting"):
        credibility.commit_forecast(
            sealed,
            command_id=IdempotencyKey("command:forced-forecast-conflict"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=3,
        )


def test_authority_scoring_uses_numeric_history_scale(tmp_path):
    lifecycle, credibility, _signer, field_id, _issue, epoch = _authority(tmp_path)
    result_id, _source = _settle(lifecycle, field_id)
    _row, observation, _issued, _payload = credibility._active_settled_result(result_id)
    packet = EvidencePacket.create(
        competitor_id=observation.competitor_id,
        target_context=observation.context,
        observations=(observation,),
        taxonomy_version=observation.context.taxonomy_version,
        conversion_version=observation.context.conversion_version,
        historical_cutoff_key=str(epoch[2]),
        tournament_epoch_id=StableIdentifier(str(epoch[0])),
        tournament_event_sequence=observation.observation_sequence,
    )

    parameters = credibility._authority_scoring_parameters(packet)

    assert parameters["robust_context_scale_ms"] == 1_200
    assert parameters["evidence_weight"] == "1"


def test_forged_forecast_support_cannot_change_difficulty_or_hierarchy(tmp_path):
    _lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    evidence = _canonical_evidence(epoch)
    original = _real_ml_assessment(evidence, 77)
    for index, claimed_count in enumerate((1, 10_000), start=1):
        support = EvidenceSupport(
            claimed_count,
            str(claimed_count),
            claimed_count,
            evidence.historical_cutoff_key,
            evidence.tournament_event_sequence,
        )
        forecast = AssessorForecast.create(
            forecast_id=StableIdentifier(f"forecast:forged-support-{index}"),
            assessor=AssessorKind.ML,
            state=original.forecast.state,
            evidence_digest=original.forecast.evidence_digest,
            distribution=original.forecast.distribution,
            support=support,
            warnings=original.forecast.warnings,
            artifacts=original.forecast.artifacts,
            abstention_code=original.forecast.abstention_code,
        )
        assessment = type(original).create(
            forecast=forecast,
            specialist_key=original.specialist_key,
            specialist_weight=original.specialist_weight,
            universal_quantiles_ms=original.universal_quantiles_ms,
            specialist_quantiles_ms=original.specialist_quantiles_ms,
            unseen_categories=original.unseen_categories,
            bundle_digest=original.bundle_digest,
        )
        sealed = seal_forecast_commit(
            forecast,
            evidence_packet=evidence,
            assessor_input=None,
            assessor_receipt=assessment,
            field_id=field_id,
            competitor_id=StableIdentifier("competitor:a"),
            field_revision=1,
            evidence_epoch_id=StableIdentifier(str(epoch[0])),
            evidence_epoch_digest=str(epoch[1]),
            historical_cutoff_key=str(epoch[2]),
            receipt_id=StableIdentifier("receipt:heat-a"),
            issue_event_digest=issue.event_digest,
            signer=signer,
            created_at=NOW,
        )
        with pytest.raises(CredibilityReactionError, match="support"):
            credibility.commit_forecast(
                sealed,
                command_id=IdempotencyKey(f"command:forged-support-{index}"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=3,
            )


def test_packet_and_candidate_causality_reject_projection_and_authority_drift(
    tmp_path,
):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    evidence, formula_input, assessment = _real_formula_material(signer, epoch)
    sealed = seal_forecast_commit(
        assessment.forecast,
        evidence_packet=evidence,
        assessor_input=formula_input,
        assessor_receipt=assessment,
        field_id=field_id,
        competitor_id=StableIdentifier("competitor:a"),
        field_revision=1,
        evidence_epoch_id=StableIdentifier(str(epoch[0])),
        evidence_epoch_digest=str(epoch[1]),
        historical_cutoff_key=str(epoch[2]),
        receipt_id=StableIdentifier("receipt:heat-a"),
        issue_event_digest=issue.event_digest,
        signer=signer,
        created_at=NOW,
    )
    payload = dict(sealed.manifest.body()["payload"])
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT snapshot_json FROM v3_ingress_snapshots "
            "WHERE entity_kind='field' AND entity_id=? AND upstream_revision=1",
            (str(field_id),),
        ).fetchone()
    assert row is not None
    snapshot = json.loads(str(row[0]))

    with pytest.raises(CredibilityReactionError, match="target context is invalid"):
        credibility._verify_packet_causality(payload, evidence, {**snapshot, "target_context": {}})
    with pytest.raises(CredibilityReactionError, match="canonical evidence packet"):
        credibility._verify_packet_causality(
            {**payload, "competitor_id": "competitor:b"}, evidence, snapshot
        )
    with pytest.raises(CredibilityReactionError, match="pre-result issue authority"):
        credibility._verify_candidate_causality(
            {**payload, "evidence_epoch_id": "epoch:missing"},
            assessment.forecast,
            evidence,
        )
    with pytest.raises(CredibilityReactionError, match="differs from issued authority"):
        credibility._verify_candidate_causality(
            {**payload, "issue_event_digest": "0" * 64},
            assessment.forecast,
            evidence,
        )


def test_real_u11_candidate_report_updates_only_candidate_ledger(tmp_path):
    from strathmark.v3.assessors import llm_council as council
    from tests.v3.integration import test_llm_job_adapters as u11

    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    evidence = _canonical_evidence(epoch)
    local_member = u11._evaluated(u11._spec(council.ProviderKind.LOCAL))
    local_two = u11._repinned(
        local_member,
        member_id="ministral",
        provider_id="ollama_ministral",
        family="ministral3",
    )
    cloud_member = u11._evaluated(u11._spec(council.ProviderKind.CLOUD))
    local = u11._record(
        council.ProviderKind.LOCAL,
        member=local_member,
        evidence_digest=evidence.content_digest,
    )
    local2 = u11._record(
        council.ProviderKind.LOCAL,
        job_id="job:credibility-two",
        member=local_two,
        evidence_digest=evidence.content_digest,
    )
    cloud = u11._record(
        council.ProviderKind.CLOUD,
        job_id="job:credibility-cloud",
        member=cloud_member,
        evidence_digest=evidence.content_digest,
    )

    class Good:
        def __init__(self, member):
            self.member = member

        @property
        def lease_authority(self):
            return u11._test_lease_authority()

        def execute(self, job):
            value = u11._audited_executed(self.member)
            return council.ProviderResponse(
                value.audit.raw_response_digest,
                job.evidence_digest,
                job.bundle_digest,
                value,
                value.execution_audit,
            )

    weights = {
        local_member.member_id: "1",
        local_two.member_id: "1",
        cloud_member.member_id: "1",
    }
    report = u11._CandidateEvaluationHarness().evaluate(
        local_jobs=(local, local2),
        cloud_job=cloud,
        local_adapters=(Good(local_member), Good(local_two)),
        cloud_adapter=Good(cloud_member),
        reliability_weights=weights,
        context_weights=weights,
        clock=lambda job: job.lease_acquired_at,
    )
    promoted = deepcopy(report)
    object.__setattr__(promoted, "candidate_status", council.CandidateStatus.PROMOTED)
    with pytest.raises(CredibilityReactionError, match="candidate authority"):
        credibility.commit_candidate_evaluation(
            promoted,
            evidence_packet=evidence,
            field_id=field_id,
            competitor_id=StableIdentifier("competitor:a"),
            field_revision=1,
            evidence_epoch_id=StableIdentifier(str(epoch[0])),
            evidence_epoch_digest=str(epoch[1]),
            historical_cutoff_key=str(epoch[2]),
            receipt_id=StableIdentifier("receipt:heat-a"),
            issue_event_digest=issue.event_digest,
            command_id=IdempotencyKey("command:promoted-candidate-report"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=3,
        )
    wrong_packet = deepcopy(report)
    object.__setattr__(wrong_packet.outcomes[0], "evidence_digest", "0" * 64)
    with pytest.raises(CredibilityReactionError, match="evidence packet differs"):
        credibility.commit_candidate_evaluation(
            wrong_packet,
            evidence_packet=evidence,
            field_id=field_id,
            competitor_id=StableIdentifier("competitor:a"),
            field_revision=1,
            evidence_epoch_id=StableIdentifier(str(epoch[0])),
            evidence_epoch_digest=str(epoch[1]),
            historical_cutoff_key=str(epoch[2]),
            receipt_id=StableIdentifier("receipt:heat-a"),
            issue_event_digest=issue.event_digest,
            command_id=IdempotencyKey("command:wrong-packet-candidate-report"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=3,
        )
    mixed_report = deepcopy(report)
    abstained = replace(
        mixed_report.outcomes[0].validated,
        distribution=None,
        abstention_reason="insufficient_history",
    )
    object.__setattr__(mixed_report.outcomes[0], "validated", abstained)
    object.__setattr__(mixed_report.outcomes[1], "validated", None)
    object.__setattr__(mixed_report.outcomes[1], "unavailable_code", "provider_timeout")
    credibility.commit_candidate_evaluation(
        mixed_report,
        evidence_packet=evidence,
        field_id=field_id,
        competitor_id=StableIdentifier("competitor:a"),
        field_revision=1,
        evidence_epoch_id=StableIdentifier(str(epoch[0])),
        evidence_epoch_digest=str(epoch[1]),
        historical_cutoff_key=str(epoch[2]),
        receipt_id=StableIdentifier("receipt:heat-a"),
        issue_event_digest=issue.event_digest,
        command_id=IdempotencyKey("command:u11-candidate-report"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    result_id, _source = _settle(lifecycle, field_id)
    ledger, baseline = credibility.react_result(
        result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7
    )
    candidate = [item for item in ledger.active_opportunities if item.scope.value == "candidate"]
    assert len(candidate) == 4
    assert {item.assessor for item in candidate} == {
        AssessorKind.LLM_MEMBER,
        AssessorKind.LLM_COUNCIL,
    }
    assert dict(baseline.weights)[AssessorKind.LLM_COUNCIL].startswith("0.333333")


def _settle(lifecycle, field_id):
    result = lifecycle.record_live_result(
        _submission(field_id, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:credibility-result-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    lifecycle.record_live_result(
        _submission(field_id, "b", ResultStatus.DNS),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:credibility-result-b"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    lifecycle.settle_live_race(
        field_id,
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:credibility-settle"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=6,
    )
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT result_key FROM v3_result_revisions WHERE source_global_sequence=?",
            (result.first_global_sequence,),
        ).fetchone()
    assert row is not None
    return StableIdentifier(str(row[0])), result.first_global_sequence


def test_verified_sqlite_settlement_persists_exact_opportunities_scores_and_reaction(
    tmp_path,
):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.FORMULA, 1)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.ML, 2)
    result_id, source = _settle(lifecycle, field_id)

    ledger, baseline = credibility.react_result(
        result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7
    )
    assert {row.assessor for row in ledger.active_opportunities} == {
        AssessorKind.FORMULA,
        AssessorKind.ML,
    }
    assert len(ledger.active_scores) == 2
    assert len({row.opportunity_id for row in ledger.active_opportunities}) == 2
    assert all("0.333333333333" in weight for _, weight in baseline.weights)
    assert reactions._decode_weight_receipt(reactions._encode_weight_receipt(baseline)) == baseline
    assert all(
        reactions._opportunity_from_dict(row.to_dict()) == row for row in ledger.opportunities
    )
    assert all(reactions._score_from_dict(row.to_dict()) == row for row in ledger.scores)
    with pytest.raises(CredibilityReactionError, match="context"):
        reactions._context_from_dict({})
    with pytest.raises(CredibilityReactionError, match="opportunity schema"):
        reactions._opportunity_from_dict({})
    with pytest.raises(CredibilityReactionError, match="predictive metrics"):
        reactions._metrics_from_dict({})
    with pytest.raises(CredibilityReactionError, match="consequence receipt"):
        reactions._consequence_from_dict({})
    malformed_consequence = dict(ledger.scores[0].consequence.to_dict())
    malformed_consequence["metrics"] = {}
    with pytest.raises(CredibilityReactionError, match="consequence metrics"):
        reactions._consequence_from_dict(malformed_consequence)
    with pytest.raises(CredibilityReactionError, match="score schema"):
        reactions._score_from_dict({})
    malformed_baseline = reactions._encode_weight_receipt(baseline)
    malformed_baseline["receipt_digest"] = "0" * 64
    with pytest.raises(CredibilityReactionError, match="baseline receipt"):
        reactions._decode_weight_receipt(malformed_baseline)
    event_count = credibility._events.event_count()
    retry_ledger, retry_baseline = credibility.react_result(
        result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7
    )
    assert Evaluator.calls == 2
    assert credibility._events.event_count() == event_count
    assert retry_ledger.current_projection_digest == ledger.current_projection_digest
    assert retry_baseline.receipt_digest == baseline.receipt_digest

    restarted = SQLiteCredibilityReactionService(
        lifecycle.projections.database_path,
        trust_store=IntegrityTrustStore((signer.identity,)),
        consequence_evaluator=Evaluator(),
        policy_manifest=_policy(signer),
    )
    assert restarted.load_ledger().current_projection_digest == ledger.current_projection_digest
    restarted.react_result(result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7)
    assert Evaluator.calls == 2
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        reaction = connection.execute(
            "SELECT state FROM v3_derivation_reactions WHERE source_global_sequence=? "
            "AND reaction_type=?",
            (source, MandatoryReaction.CREDIBILITY.value),
        ).fetchone()
    assert reaction is not None and reaction[0] == "completed"


def test_signed_policy_substitution_is_rejected_on_restart(tmp_path):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.FORMULA)
    result_id, _source = _settle(lifecycle, field_id)
    credibility.react_result(result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7)
    changed = replace(CredibilityPolicy(), learning_rate="0.2")
    changed_manifest = seal_credibility_policy(
        changed,
        optimizer_bundle_digest=Evaluator.bundle_digest,
        signer=signer,
        created_at=NOW,
    )
    with pytest.raises(CredibilityReactionError, match="restart authority"):
        SQLiteCredibilityReactionService(
            lifecycle.projections.database_path,
            trust_store=IntegrityTrustStore((signer.identity,)),
            consequence_evaluator=Evaluator(),
            policy_manifest=changed_manifest,
        )


def test_raw_evaluator_claims_cannot_change_operational_consequence_health(tmp_path):
    lifecycle, _installed_service, signer, field_id, issue, epoch = _authority(tmp_path)
    raw = Evaluator()
    service = SQLiteCredibilityReactionService(
        lifecycle.projections.database_path,
        trust_store=IntegrityTrustStore((signer.identity,)),
        consequence_evaluator=raw,
        policy_manifest=_policy(signer),
    )
    _commit(service, signer, field_id, issue, epoch, AssessorKind.FORMULA)
    result_id, _source = _settle(lifecycle, field_id)
    ledger, weights = service.react_result(
        result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7
    )
    assert Evaluator.calls == 0
    assert len(ledger.active_scores) == 1
    assert ledger.active_scores[0].consequence.status is ConsequenceStatus.PENDING
    formula = next(row for row in weights.components if row.assessor is AssessorKind.FORMULA)
    assert formula.health != "consequence_breach"


def test_optimizer_installation_manifest_rejects_untrusted_and_substituted_code(tmp_path):
    signer = P256EphemeralSigner.generate("integrity-key:optimizer-install")
    other = P256EphemeralSigner.generate("integrity-key:optimizer-other")
    trust = IntegrityTrustStore((signer.identity,))
    evaluator = Evaluator()
    with pytest.raises(CredibilityReactionError, match="manifest kind differs"):
        reactions.SealedOptimizerEvaluatorAuthority(
            replace(_policy(signer).manifest, kind="not-optimizer-authority")
        )

    class WrongPort:
        bundle_digest = "a" * 64
        implementation_digest = "b" * 64
        evaluator_port = "wrong"

        def evaluate(self, **_arguments):
            return None

    with pytest.raises(CredibilityReactionError, match="does not implement"):
        seal_optimizer_evaluator_authority(WrongPort(), signer=signer, created_at=NOW)
    untrusted = InstalledOptimizerEvaluator(
        evaluator,
        seal_optimizer_evaluator_authority(evaluator, signer=other, created_at=NOW),
    )
    with pytest.raises(CredibilityReactionError, match="signature is untrusted"):
        SQLiteCredibilityReactionService(
            tmp_path / "untrusted-optimizer.sqlite3",
            trust_store=trust,
            consequence_evaluator=untrusted,
            policy_manifest=_policy(signer),
        )
    substituted = MisboundEvaluator()
    substituted_authority = seal_optimizer_evaluator_authority(
        evaluator, signer=signer, created_at=NOW
    )
    with pytest.raises(CredibilityReactionError, match="installation binding differs"):
        SQLiteCredibilityReactionService(
            tmp_path / "substituted-optimizer.sqlite3",
            trust_store=trust,
            consequence_evaluator=InstalledOptimizerEvaluator(substituted, substituted_authority),
            policy_manifest=_policy(signer),
        )
    malformed_authority = reactions.SealedOptimizerEvaluatorAuthority(
        sign_manifest(
            reactions.OPTIMIZER_EVALUATOR_AUTHORITY_MANIFEST_KIND,
            {
                "schema_version": "strathmark-v3-optimizer-evaluator-authority-v1",
                "evaluator_port": evaluator.evaluator_port,
                "optimizer_bundle_digest": evaluator.bundle_digest,
                "implementation_digest": evaluator.implementation_digest,
                "extra": True,
            },
            signer=signer,
            created_at=NOW,
        )
    )
    with pytest.raises(CredibilityReactionError, match="authority is not closed"):
        SQLiteCredibilityReactionService(
            tmp_path / "malformed-optimizer.sqlite3",
            trust_store=trust,
            consequence_evaluator=InstalledOptimizerEvaluator(evaluator, malformed_authority),
            policy_manifest=_policy(signer),
        )


def test_wrong_field_nonmember_and_duplicate_assessor_fail_closed(tmp_path):
    _lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    evidence, formula_input, assessment = _real_formula_material(signer, epoch)
    for wrong_field, competitor in (
        (StableIdentifier("field:unissued"), StableIdentifier("competitor:a")),
        (field_id, StableIdentifier("competitor:not-issued")),
    ):
        sealed = seal_forecast_commit(
            assessment.forecast,
            evidence_packet=evidence,
            assessor_input=formula_input,
            assessor_receipt=assessment,
            field_id=wrong_field,
            competitor_id=competitor,
            field_revision=1,
            evidence_epoch_id=StableIdentifier(str(epoch[0])),
            evidence_epoch_digest=str(epoch[1]),
            historical_cutoff_key=str(epoch[2]),
            receipt_id=StableIdentifier("receipt:heat-a"),
            issue_event_digest=issue.event_digest,
            signer=signer,
            created_at=NOW,
        )
        with pytest.raises(CredibilityReactionError, match="issued-field|exact causal"):
            credibility.commit_forecast(
                sealed,
                command_id=IdempotencyKey(f"command:wrong-{wrong_field}-{competitor}"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=3,
            )

    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.ML, 2)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.ML, 3)
    result_id, _source = _settle(_lifecycle, field_id)
    with pytest.raises(CredibilityReactionError, match="multiple forecasts"):
        credibility.react_result(
            result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7
        )


def test_event_tampering_is_detected_before_restart_projection(tmp_path):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.FORMULA)
    result_id, _source = _settle(lifecycle, field_id)
    credibility.react_result(result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7)
    with open_v3_connection(lifecycle.projections.database_path) as connection:
        connection.execute("DROP TRIGGER v3_events_no_update")
        row = connection.execute(
            "SELECT global_sequence, envelope_json FROM v3_events WHERE aggregate_kind='score' "
            "ORDER BY global_sequence LIMIT 1"
        ).fetchone()
        assert row is not None
        envelope = json.loads(str(row[1]))
        envelope["event_digest"] = "0" * 64
        connection.execute(
            "UPDATE v3_events SET envelope_json=? WHERE global_sequence=?",
            (json.dumps(envelope), int(row[0])),
        )
    with pytest.raises(Exception, match="verification|digest|integrity"):
        credibility.load_ledger()


def test_active_correction_appends_reversals_and_replays_only_latest_revision(tmp_path):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.FORMULA)
    result_id, _source = _settle(lifecycle, field_id)
    first, _baseline = credibility.react_result(
        result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7
    )
    assert len(first.active_opportunities) == 2
    assert (
        next(
            row for row in first.active_opportunities if row.assessor is AssessorKind.ML
        ).outcome.value
        == "unavailable"
    )
    assert {row.result_revision for row in first.active_opportunities} == {1}

    lifecycle.record_live_result(
        _submission(field_id, "a", ResultStatus.COMPLETION, revision=2),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:credibility-correction-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=8,
    )
    corrected, _baseline = credibility.react_result(
        result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=9
    )
    assert corrected.reversals
    assert all(reactions._reversal_from_dict(row.to_dict()) == row for row in corrected.reversals)
    with pytest.raises(CredibilityReactionError, match="reversal schema"):
        reactions._reversal_from_dict({})
    assert {row.result_revision for row in corrected.active_opportunities} == {2}
    assert {row.result_revision for row in corrected.active_scores} == {2}
    assert (
        next(
            item
            for item in Evaluator.inputs[-1].field_results
            if item.competitor_id == "competitor:a"
        ).result_revision
        == 2
    )
    assert (
        SQLiteCredibilityReactionService(
            lifecycle.projections.database_path,
            trust_store=IntegrityTrustStore((signer.identity,)),
            consequence_evaluator=Evaluator(),
            policy_manifest=_policy(signer),
        )
        .load_ledger()
        .current_projection_digest
        == corrected.current_projection_digest
    )


def test_reaction_contract_helpers_reject_malformed_values_and_bind_full_field() -> None:
    forecast = _forecast(90, AssessorKind.FORMULA, 1, evidence_digest="a" * 64)
    results = (
        reactions.SettledFieldResult(
            "competitor:a", "result:a", 1, "b" * 64, 1, "completion", 10_000
        ),
        reactions.SettledFieldResult(
            "competitor:b", "result:b", 1, "c" * 64, 2, "completion", 11_000
        ),
    )
    cards = (reactions.FieldForecastCard("competitor:a", None, forecast),)
    scoring_input = reactions._optimizer_scoring_input(
        tournament_id="tournament:show",
        round_id="round:heat",
        field_id="field:heat-a",
        competitor_id="competitor:a",
        result_id="result:a",
        result_revision=1,
        result_revision_digest="b" * 64,
        source_sequence=1,
        issued_field_members=("competitor:a", "competitor:b"),
        issued_marks=(("competitor:a", 3), ("competitor:b", 4)),
        field_results=results,
        field_forecasts=cards,
        field_receipt_digest="d" * 64,
        optimizer_bundle_digest="e" * 64,
        credibility_policy_digest="f" * 64,
        raw_time_ms=10_000,
        context=ContextNode("underhand", "300_349", "wood", "zero"),
        robust_context_scale_ms=1_000,
        evidence_weight="1",
    )
    assert scoring_input.field_results[0].to_dict()["raw_time_ms"] == 10_000
    assert scoring_input.field_forecasts[0].to_dict()["forecast"] == forecast.to_dict()
    for mutation, message in (
        ({"field_results": ()}, "terminal result"),
        ({"field_forecasts": []}, "forecast cards"),
        ({"scoring_input_digest": "0" * 64}, "digest mismatch"),
    ):
        with pytest.raises(CredibilityReactionError, match=message):
            replace(scoring_input, **mutation)
    for value in (True, "1", 0, -1):
        with pytest.raises(CredibilityReactionError, match="positive integer"):
            reactions._positive_int(value, "value")
    for value in (None, object(), "0", "-1", "NaN"):
        with pytest.raises(CredibilityReactionError, match="positive decimal"):
            reactions._positive_decimal(value, "value")
    with pytest.raises(CredibilityReactionError, match="positive decimal"):
        reactions._positive_decimal(1, "value")
    reactions._positive_decimal("1", "value")
    for value in (None, "a", "A" * 64, "g" * 64):
        with pytest.raises(CredibilityReactionError, match="SHA-256"):
            reactions._digest(value, "value")
    for value in (True, "1", -1):
        with pytest.raises(CredibilityReactionError, match="history support"):
            reactions._derived_difficulty(value)
    assert reactions._candidate_failure_kind(None) is None
    assert reactions._candidate_failure_kind("provider_timeout") == "deadline_miss"
    assert reactions._candidate_failure_kind("runtime_crash") == "runtime_failure"
    assert reactions._candidate_failure_kind("cloud_unavailable") == "transport_failure"
    assert reactions._candidate_failure_kind("schema") is None


def test_sealed_policy_and_service_composition_fail_closed_at_every_trust_boundary(
    tmp_path,
):
    signer = P256EphemeralSigner.generate("integrity-key:policy-boundaries")
    other = P256EphemeralSigner.generate("integrity-key:policy-untrusted")
    trust = IntegrityTrustStore((signer.identity,))
    policy = CredibilityPolicy()
    value = {name: getattr(policy, name) for name in policy.__dataclass_fields__}
    valid_payload = {
        "schema_version": "strathmark-v3-credibility-policy-v1",
        "policy": value,
        "policy_digest": canonical_digest(value),
        "optimizer_bundle_digest": Evaluator.bundle_digest,
    }

    def sealed(payload, *, authority=signer):
        return reactions.SealedCredibilityPolicy(
            sign_manifest(
                reactions.CREDIBILITY_POLICY_MANIFEST_KIND,
                payload,
                signer=authority,
                created_at=NOW,
            )
        )

    with pytest.raises(CredibilityReactionError, match="manifest kind"):
        reactions.SealedCredibilityPolicy(
            replace(_policy(signer).manifest, kind="not-credibility-policy")
        )
    with pytest.raises(CredibilityReactionError, match="frozen and typed"):
        seal_credibility_policy(
            {},
            optimizer_bundle_digest=Evaluator.bundle_digest,
            signer=signer,
            created_at=NOW,
        )
    with pytest.raises(CredibilityReactionError, match="pinned P-256"):
        SQLiteCredibilityReactionService(
            tmp_path / "trust.sqlite3",
            trust_store=None,
            consequence_evaluator=Evaluator(),
            policy_manifest=_policy(signer),
        )
    with pytest.raises(CredibilityReactionError, match="optimizer evaluator"):
        SQLiteCredibilityReactionService(
            tmp_path / "evaluator.sqlite3",
            trust_store=trust,
            consequence_evaluator=object(),
            policy_manifest=_policy(signer),
        )
    with pytest.raises(CredibilityReactionError, match="sealed policy"):
        SQLiteCredibilityReactionService(
            tmp_path / "manifest.sqlite3",
            trust_store=trust,
            consequence_evaluator=Evaluator(),
            policy_manifest=None,
        )
    with pytest.raises(CredibilityReactionError, match="signature is untrusted"):
        SQLiteCredibilityReactionService(
            tmp_path / "signature.sqlite3",
            trust_store=trust,
            consequence_evaluator=Evaluator(),
            policy_manifest=sealed(valid_payload, authority=other),
        )
    with pytest.raises(CredibilityReactionError, match="not closed"):
        SQLiteCredibilityReactionService(
            tmp_path / "closed.sqlite3",
            trust_store=trust,
            consequence_evaluator=Evaluator(),
            policy_manifest=sealed({**valid_payload, "extra": True}),
        )
    for payload in ({**valid_payload, "policy_digest": "0" * 64},):
        with pytest.raises(CredibilityReactionError, match="authority binding"):
            SQLiteCredibilityReactionService(
                tmp_path
                / f"binding-{payload['policy_digest'][:1]}-{payload['optimizer_bundle_digest'][:1]}.sqlite3",
                trust_store=trust,
                consequence_evaluator=Evaluator(),
                policy_manifest=sealed(payload),
            )
    invalid_policy = {**value, "weight_floor": "0.9"}
    with pytest.raises(CredibilityReactionError, match="payload is invalid"):
        SQLiteCredibilityReactionService(
            tmp_path / "invalid-policy.sqlite3",
            trust_store=trust,
            consequence_evaluator=Evaluator(),
            policy_manifest=sealed(
                {
                    **valid_payload,
                    "policy": invalid_policy,
                    "policy_digest": canonical_digest(invalid_policy),
                }
            ),
        )


def test_forecast_sealing_rejects_cross_assessor_and_packet_relabel_attacks(tmp_path):
    _lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    evidence, formula_input, formula = _real_formula_material(signer, epoch)
    base = {
        "evidence_packet": evidence,
        "assessor_input": formula_input,
        "assessor_receipt": formula,
        "field_id": field_id,
        "competitor_id": StableIdentifier("competitor:a"),
        "field_revision": 1,
        "evidence_epoch_id": StableIdentifier(str(epoch[0])),
        "evidence_epoch_digest": str(epoch[1]),
        "historical_cutoff_key": str(epoch[2]),
        "receipt_id": StableIdentifier("receipt:heat-a"),
        "issue_event_digest": issue.event_digest,
        "signer": signer,
        "created_at": NOW,
    }

    def rejected_commit(sealed, message, number):
        with pytest.raises(CredibilityReactionError, match=message):
            credibility.commit_forecast(
                sealed,
                command_id=IdempotencyKey(f"command:manifest-attack-{number}"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=3,
            )

    valid_sealed = seal_forecast_commit(formula.forecast, **base)
    valid_payload = valid_sealed.manifest.body()["payload"]
    rejected_commit(None, "sealed commit", 0)
    untrusted = P256EphemeralSigner.generate("integrity-key:forecast-untrusted")
    rejected_commit(
        reactions.SealedForecastCommit(
            sign_manifest(
                reactions.FORECAST_COMMIT_MANIFEST_KIND,
                valid_payload,
                signer=untrusted,
                created_at=NOW,
            )
        ),
        "invalid or untrusted",
        1,
    )

    def resigned(mutation):
        return reactions.SealedForecastCommit(
            sign_manifest(
                reactions.FORECAST_COMMIT_MANIFEST_KIND,
                {**valid_payload, **mutation},
                signer=signer,
                created_at=NOW,
            )
        )

    rejected_commit(resigned({"extra": True}), "not closed", 2)
    rejected_commit(resigned({"forecast": {}}), "valid forecast", 3)
    receipt = dict(valid_payload["assessor_receipt"])
    rejected_commit(
        resigned({"assessor_receipt": {**receipt, "forecast": {}}}),
        "does not bind",
        4,
    )
    rejected_commit(
        resigned({"assessor_receipt": {**receipt, "assessment_digest": "0" * 64}}),
        "receipt digest",
        5,
    )
    rejected_commit(resigned({"assessor_input": None}), "input receipt", 6)
    formula_value = dict(valid_payload["assessor_input"])

    def internally_consistent_formula_attack(mutated_input):
        forged_forecast = AssessorForecast.create(
            forecast_id=formula.forecast.forecast_id,
            assessor=formula.forecast.assessor,
            state=formula.forecast.state,
            evidence_digest=canonical_digest(mutated_input),
            distribution=formula.forecast.distribution,
            support=formula.forecast.support,
            warnings=formula.forecast.warnings,
            artifacts=formula.forecast.artifacts,
            abstention_code=formula.forecast.abstention_code,
        )
        forged_receipt = {**receipt, "forecast": forged_forecast.to_dict()}
        forged_receipt.pop("assessment_digest")
        forged_receipt["assessment_digest"] = canonical_digest(forged_receipt)
        return resigned(
            {
                "forecast": forged_forecast.to_dict(),
                "assessor_input": mutated_input,
                "assessor_receipt": forged_receipt,
            }
        )

    rejected_commit(
        internally_consistent_formula_attack({**formula_value, "governor_receipt": None}),
        "governor receipt is absent",
        7,
    )
    governor = dict(formula_value["governor_receipt"])
    governor["evidence_digest"] = "0" * 64
    governor.pop("receipt_digest")
    governor["receipt_digest"] = canonical_digest(governor)
    rejected_commit(
        internally_consistent_formula_attack({**formula_value, "governor_receipt": governor}),
        "not packet-bound",
        8,
    )
    for forecast, mutation, message in (
        (None, {}, "typed sealed forecast"),
        (formula.forecast, {"evidence_packet": None}, "canonical evidence packet"),
        (formula.forecast, {"assessor_receipt": object()}, "typed assessor receipt"),
    ):
        with pytest.raises(CredibilityReactionError, match=message):
            seal_forecast_commit(forecast, **{**base, **mutation})

    ml = _real_ml_assessment(evidence, 91)
    with pytest.raises(CredibilityReactionError, match="does not bind the forecast"):
        seal_forecast_commit(formula.forecast, **{**base, "assessor_receipt": ml})
    other_packet = EvidencePacket.create(
        competitor_id=StableIdentifier("competitor:b"),
        target_context=evidence.target_context,
        observations=(),
        taxonomy_version=evidence.taxonomy_version,
        conversion_version=evidence.conversion_version,
        historical_cutoff_key=evidence.historical_cutoff_key,
        tournament_epoch_id=evidence.tournament_epoch_id,
        tournament_event_sequence=evidence.tournament_event_sequence,
    )
    with pytest.raises(CredibilityReactionError, match="input does not bind"):
        seal_forecast_commit(formula.forecast, **{**base, "evidence_packet": other_packet})
    tampered_formula_forecast = deepcopy(formula.forecast)
    object.__setattr__(tampered_formula_forecast, "evidence_digest", "0" * 64)
    tampered_formula_receipt = AssessmentResult.create(
        forecast=tampered_formula_forecast,
        review=formula.review,
        center_ms=formula.center_ms,
        uncertainty_ms=formula.uncertainty_ms,
        log_center=formula.log_center,
        log_scale=formula.log_scale,
        effective_sample_size=formula.effective_sample_size,
        personal_weight=formula.personal_weight,
        manifest_digest=formula.manifest_digest,
        trace=formula.trace,
    )
    with pytest.raises(CredibilityReactionError, match="exact input"):
        seal_forecast_commit(
            tampered_formula_forecast,
            **{**base, "assessor_receipt": tampered_formula_receipt},
        )
    formula_shaped_ml = MLAssessment.create(
        forecast=formula.forecast,
        specialist_key=ml.specialist_key,
        specialist_weight=ml.specialist_weight,
        universal_quantiles_ms=ml.universal_quantiles_ms,
        specialist_quantiles_ms=ml.specialist_quantiles_ms,
        unseen_categories=ml.unseen_categories,
        bundle_digest=ml.bundle_digest,
    )
    with pytest.raises(CredibilityReactionError, match="assessment receipt"):
        seal_forecast_commit(formula.forecast, **{**base, "assessor_receipt": formula_shaped_ml})
    with pytest.raises(CredibilityReactionError, match="ML assessment receipt"):
        seal_forecast_commit(
            ml.forecast,
            **{
                **base,
                "assessor_receipt": ml,
                "assessor_input": formula_input,
            },
        )
    ml_sealed = seal_forecast_commit(
        ml.forecast,
        **{
            **base,
            "assessor_input": None,
            "assessor_receipt": ml,
        },
    )
    ml_payload = ml_sealed.manifest.body()["payload"]
    rejected_commit(
        reactions.SealedForecastCommit(
            sign_manifest(
                reactions.FORECAST_COMMIT_MANIFEST_KIND,
                {**ml_payload, "assessor_input": formula_input.to_dict()},
                signer=signer,
                created_at=NOW,
            )
        ),
        "ML assessor receipt",
        9,
    )
    tampered_ml_forecast = deepcopy(ml.forecast)
    object.__setattr__(tampered_ml_forecast, "evidence_digest", "0" * 64)
    tampered_ml = MLAssessment.create(
        forecast=tampered_ml_forecast,
        specialist_key=ml.specialist_key,
        specialist_weight=ml.specialist_weight,
        universal_quantiles_ms=ml.universal_quantiles_ms,
        specialist_quantiles_ms=ml.specialist_quantiles_ms,
        unseen_categories=ml.unseen_categories,
        bundle_digest=ml.bundle_digest,
    )
    with pytest.raises(CredibilityReactionError, match="evidence packet"):
        seal_forecast_commit(
            tampered_ml_forecast,
            **{
                **base,
                "assessor_input": None,
                "assessor_receipt": tampered_ml,
            },
        )
    with pytest.raises(CredibilityReactionError, match="signed manifest"):
        reactions.SealedForecastCommit(None)
    with pytest.raises(CredibilityReactionError, match="manifest kind"):
        reactions.SealedForecastCommit(_policy(signer).manifest)

    def llm_forecast(assessor, state, code=None):
        return AssessorForecast.create(
            forecast_id=StableIdentifier(
                f"forecast:seal-{assessor.value}-{state.value}-{code or 'none'}"
            ),
            assessor=assessor,
            state=state,
            evidence_digest=evidence.content_digest,
            distribution=(
                formula.forecast.distribution if state is ForecastState.COMMITTED else None
            ),
            support=formula.forecast.support,
            warnings=(),
            artifacts=(),
            abstention_code=code,
        )

    def llm_receipt(forecast):
        return AssessmentResult.create(
            forecast=forecast,
            review=formula.review,
            center_ms=formula.center_ms,
            uncertainty_ms=formula.uncertainty_ms,
            log_center=formula.log_center,
            log_scale=formula.log_scale,
            effective_sample_size=formula.effective_sample_size,
            personal_weight=formula.personal_weight,
            manifest_digest=formula.manifest_digest,
            trace=formula.trace,
        )

    member = llm_forecast(AssessorKind.LLM_MEMBER, ForecastState.COMMITTED)
    council = llm_forecast(AssessorKind.LLM_COUNCIL, ForecastState.COMMITTED)
    assert (
        seal_forecast_commit(
            member,
            **{
                **base,
                "assessor_input": None,
                "assessor_receipt": llm_receipt(member),
                "member_id": "member:one",
            },
        ).manifest.kind
        == reactions.FORECAST_COMMIT_MANIFEST_KIND
    )
    member_sealed = seal_forecast_commit(
        member,
        **{
            **base,
            "assessor_input": None,
            "assessor_receipt": llm_receipt(member),
            "member_id": "member:one",
        },
    )
    member_payload = member_sealed.manifest.body()["payload"]
    for index, (mutation, message) in enumerate(
        (
            ({"assessor_input": formula_input.to_dict()}, "non-Formula"),
            ({"operational_promotion_digest": "f" * 64}, "promotion cannot"),
            ({"member_id": None}, "member identity"),
        ),
        start=10,
    ):
        rejected_commit(
            reactions.SealedForecastCommit(
                sign_manifest(
                    reactions.FORECAST_COMMIT_MANIFEST_KIND,
                    {**member_payload, **mutation},
                    signer=signer,
                    created_at=NOW,
                )
            ),
            message,
            index,
        )
    credibility.commit_forecast(
        member_sealed,
        command_id=IdempotencyKey("command:valid-llm-member-forecast"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    for candidate, mutation, message in (
        (member, {"assessor_input": formula_input}, "non-Formula"),
        (member, {"member_id": None}, "member identity"),
        (council, {"member_id": "member:one"}, "outer assessor"),
        (council, {"operational_promotion_digest": "bad"}, "promotion digest"),
    ):
        with pytest.raises(CredibilityReactionError, match=message):
            seal_forecast_commit(
                candidate,
                **{
                    **base,
                    "assessor_input": None,
                    "assessor_receipt": llm_receipt(candidate),
                    **mutation,
                },
            )
    taxonomy = (
        (
            llm_forecast(
                AssessorKind.LLM_COUNCIL,
                ForecastState.ABSTAINED,
                "transport_failure",
            ),
            None,
            "principled model abstention",
        ),
        (
            llm_forecast(AssessorKind.LLM_COUNCIL, ForecastState.INVALID, "schema_invalid"),
            "transport_failure",
            "trusted execution failure",
        ),
        (
            llm_forecast(
                AssessorKind.LLM_COUNCIL,
                ForecastState.INVALID,
                "transport_failure",
            ),
            None,
            "cannot be relabelled",
        ),
        (
            llm_forecast(AssessorKind.LLM_COUNCIL, ForecastState.INVALID, "unknown"),
            None,
            "closed taxonomy",
        ),
    )
    for candidate, execution_failure, message in taxonomy:
        with pytest.raises(CredibilityReactionError, match=message):
            seal_forecast_commit(
                candidate,
                **{
                    **base,
                    "assessor_input": None,
                    "assessor_receipt": llm_receipt(candidate),
                    "execution_failure_kind": execution_failure,
                },
            )
    execution_failure = llm_forecast(
        AssessorKind.LLM_COUNCIL, ForecastState.INVALID, "transport_failure"
    )
    execution_sealed = seal_forecast_commit(
        execution_failure,
        **{
            **base,
            "assessor_input": None,
            "assessor_receipt": llm_receipt(execution_failure),
            "execution_failure_kind": "transport_failure",
        },
    )
    assert execution_sealed.manifest.kind == reactions.FORECAST_COMMIT_MANIFEST_KIND
    credibility.commit_forecast(
        execution_sealed,
        command_id=IdempotencyKey("command:trusted-execution-failure"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    result_id, source = _settle(_lifecycle, field_id)
    ledger, _receipt = credibility.react_result(
        result_id,
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=7,
    )
    failure_opportunity = next(
        item for item in ledger.active_opportunities if item.assessor is AssessorKind.LLM_COUNCIL
    )
    assert failure_opportunity.outcome.value == "transport_failure"
    assert not any(item.assessor is AssessorKind.LLM_COUNCIL for item in ledger.active_scores)
    row, observation, issue_event, _issue_payload = credibility._active_settled_result(result_id)
    unchanged = credibility._append_missing_opportunity(
        ledger,
        result_id=str(result_id),
        assessor=AssessorKind.FORMULA,
        observation=observation,
        result_revision=int(row[1]),
        source_sequence=source,
        issue_event=issue_event,
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=8,
    )
    assert unchanged.current_projection_digest == ledger.current_projection_digest


def test_persisted_credibility_payload_decoder_fails_closed_on_authoritative_events(
    tmp_path,
):
    signer = P256EphemeralSigner.generate("integrity-key:decoder-boundaries")
    trust = IntegrityTrustStore((signer.identity,))
    payloads = (
        ({"extra": True}, "not closed"),
        (
            {"schema_version": "wrong", "record_type": "score", "record": {}},
            "schema differs",
        ),
        (
            {
                "schema_version": "strathmark-v3-credibility-authority-event-v1",
                "record_type": "score",
                "record": None,
            },
            "record is malformed",
        ),
        (
            {
                "schema_version": "strathmark-v3-credibility-authority-event-v1",
                "record_type": "unknown",
                "record": {},
            },
            "type is unknown",
        ),
    )
    for index, (payload, message) in enumerate(payloads):
        service = SQLiteCredibilityReactionService(
            tmp_path / f"decoder-{index}.sqlite3",
            trust_store=trust,
            consequence_evaluator=Evaluator(),
            policy_manifest=_policy(signer),
        )
        service._append_event(
            command_kind=CommandKind.RECORD_SCORE,
            event_kind=EventKind.SCORE_RECORDED,
            aggregate_kind=AggregateKind.SCORE,
            aggregate_id=StableIdentifier(f"score:malformed-{index}"),
            payload=payload,
            result={"recorded": True},
            command_id=IdempotencyKey(f"command:malformed-{index}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
        with pytest.raises(CredibilityReactionError, match=message):
            service.load_ledger()
    empty = SQLiteCredibilityReactionService(
        tmp_path / "empty.sqlite3",
        trust_store=trust,
        consequence_evaluator=Evaluator(),
        policy_manifest=_policy(signer),
    )
    with pytest.raises(CredibilityReactionError, match="typed context"):
        empty.baseline_weights(None, calibration_cutoff_at_utc=NOW)
    with pytest.raises(CredibilityReactionError, match="tournament authority"):
        empty._require_open_tournament(StableIdentifier("tournament:missing"))
    with pytest.raises(CredibilityReactionError, match="ingress authority"):
        empty.freeze_live_weights(
            StableIdentifier("round:missing-one"),
            StableIdentifier("round:missing-two"),
            context=ContextNode(),
            command_id=IdempotencyKey("command:missing-round-freeze"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1,
        )
    with pytest.raises(CredibilityReactionError, match="one weight receipt"):
        empty._load_weight_receipt(999)
    receipt = empty.baseline_weights(ContextNode(), calibration_cutoff_at_utc=NOW)
    encoded = reactions._encode_weight_receipt(receipt)
    for source_sequence, digest in ((998, receipt.receipt_digest), (999, "0" * 64)):
        empty._append_event(
            command_kind=CommandKind.CHANGE_WEIGHTS,
            event_kind=EventKind.WEIGHTS_CHANGED,
            aggregate_kind=AggregateKind.WEIGHTS,
            aggregate_id=StableIdentifier(f"weights:decoder-{source_sequence}"),
            payload={
                "schema_version": "strathmark-v3-credibility-weights-event-v1",
                "source_sequence": source_sequence,
                "receipt_digest": digest,
                "context": encoded["context"],
                "calibration_cutoff_at_utc": encoded["calibration_cutoff_at_utc"],
                "policy_digest": encoded["policy_digest"],
                "weights": encoded["weights"],
                "components": encoded["components"],
            },
            result={"recorded": True},
            command_id=IdempotencyKey(f"command:weight-decoder-{source_sequence}"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=2,
        )
    with pytest.raises(CredibilityReactionError, match="receipt digest differs"):
        empty._load_weight_receipt(999)
    with pytest.raises(CredibilityReactionError, match="open authority"):
        empty._tournament_baseline(
            StableIdentifier("tournament:missing"),
            ContextNode(),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=2,
        )


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("candidate_digest", "candidate report digest"),
        ("candidate_malformed", "candidate forecast event is malformed"),
        ("candidate_operational", "cannot become an operational forecast"),
        ("authority_malformed", "forecast authority event payload is malformed"),
        ("scoring_drift", "scoring parameters drifted"),
        ("issue_mismatch", "differs from settled issue authority"),
        ("unrelated", None),
    ),
)
def test_forecast_event_decoder_rejects_tampering_and_ignores_unrelated_cards(
    tmp_path, case, message
):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    evidence, formula_input, assessment = _real_formula_material(signer, epoch)
    sealed = seal_forecast_commit(
        assessment.forecast,
        evidence_packet=evidence,
        assessor_input=formula_input,
        assessor_receipt=assessment,
        field_id=field_id,
        competitor_id=StableIdentifier("competitor:a"),
        field_revision=1,
        evidence_epoch_id=StableIdentifier(str(epoch[0])),
        evidence_epoch_digest=str(epoch[1]),
        historical_cutoff_key=str(epoch[2]),
        receipt_id=StableIdentifier("receipt:heat-a"),
        issue_event_digest=issue.event_digest,
        signer=signer,
        created_at=NOW,
    )
    parameters = credibility._authority_scoring_parameters(evidence)
    report_value = {"candidate": case}
    candidate_payload = {
        "schema_version": "strathmark-v3-candidate-diagnostic-event-v1",
        "field_id": str(field_id),
        "competitor_id": "competitor:a",
        "field_revision": 1,
        "evidence_epoch_id": str(epoch[0]),
        "evidence_epoch_digest": str(epoch[1]),
        "historical_cutoff_key": str(epoch[2]),
        "receipt_id": "receipt:heat-a",
        "issue_event_digest": issue.event_digest,
        "member_id": "member:probe",
        "operational_promotion_digest": None,
        "execution_failure_kind": None,
        "assessor_input": None,
        "evidence_packet": evidence.to_dict(),
        "candidate_report": report_value,
        "candidate_report_digest": canonical_digest(report_value),
        "authority_scoring_parameters": parameters,
        "forecast": assessment.forecast.to_dict(),
    }
    authority_payload = {
        "schema_version": "strathmark-v3-forecast-authority-event-v1",
        "sealed_manifest": sealed.manifest.to_dict(),
        "authority_scoring_parameters": parameters,
    }
    if case == "candidate_digest":
        payload = {**candidate_payload, "candidate_report_digest": "0" * 64}
    elif case == "candidate_malformed":
        payload = {**candidate_payload, "forecast": {}}
    elif case == "candidate_operational":
        payload = candidate_payload
    elif case == "authority_malformed":
        payload = {"schema_version": "unexpected"}
    elif case == "scoring_drift":
        payload = {
            **authority_payload,
            "authority_scoring_parameters": {
                **parameters,
                "evidence_weight": "999",
            },
        }
    else:
        signed_payload = dict(sealed.manifest.body()["payload"])
        mutation = (
            {"field_id": "field:unrelated"}
            if case == "unrelated"
            else {"receipt_id": "receipt:wrong"}
        )
        mutated = reactions.SealedForecastCommit(
            sign_manifest(
                reactions.FORECAST_COMMIT_MANIFEST_KIND,
                {**signed_payload, **mutation},
                signer=signer,
                created_at=NOW,
            )
        )
        payload = {**authority_payload, "sealed_manifest": mutated.manifest.to_dict()}
    credibility._append_event(
        command_kind=CommandKind.COMMIT_FORECAST,
        event_kind=EventKind.COMPONENT_FORECAST_COMMITTED,
        aggregate_kind=AggregateKind.FORECAST,
        aggregate_id=StableIdentifier(f"forecast:decoder-{case}"),
        payload=payload,
        result={"recorded": True},
        command_id=IdempotencyKey(f"command:decoder-{case}"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=3,
    )
    result_id, source = _settle(lifecycle, field_id)
    _row, observation, issue_event, issue_payload = credibility._active_settled_result(result_id)
    if message is not None:
        with pytest.raises(CredibilityReactionError, match=message):
            credibility._eligible_forecasts(
                observation=observation,
                result_source_sequence=source,
                issue_event=issue_event,
                issue_payload=issue_payload,
            )
    else:
        assert not credibility._eligible_forecasts(
            observation=observation,
            result_source_sequence=source,
            issue_event=issue_event,
            issue_payload=issue_payload,
        )
        assert not credibility._field_forecast_cards(str(field_id), issue_payload, source)


def test_partial_field_never_invokes_consequence_settlement(tmp_path):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.FORMULA)
    result = lifecycle.record_live_result(
        _submission(field_id, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:partial-field-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        row = connection.execute(
            "SELECT result_key FROM v3_result_revisions WHERE source_global_sequence=?",
            (result.first_global_sequence,),
        ).fetchone()
    assert row is not None
    with pytest.raises(CredibilityReactionError, match="active settled"):
        credibility.react_result(
            StableIdentifier(str(row[0])),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=5,
        )
    assert Evaluator.calls == 0 and not credibility.load_ledger().scores


def test_settlement_projection_requires_preceding_issue_and_exact_active_revision(
    tmp_path,
):
    lifecycle, credibility, _signer, field_id, _issue, _epoch = _authority(tmp_path)
    result_id, _source = _settle(lifecycle, field_id)
    database_path = lifecycle.projections.database_path
    with open_v3_connection(database_path) as connection:
        original = connection.execute(
            "SELECT settled_global_sequence, competitor_id FROM v3_result_revisions "
            "WHERE result_key=?",
            (str(result_id),),
        ).fetchone()
        assert original is not None
        connection.execute(
            "UPDATE v3_result_revisions SET settled_global_sequence=1 WHERE result_key=?",
            (str(result_id),),
        )
    with pytest.raises(CredibilityReactionError, match="no issued field authority"):
        credibility._active_settled_result(result_id)
    with open_v3_connection(database_path) as connection:
        connection.execute(
            "UPDATE v3_result_revisions SET settled_global_sequence=?, competitor_id=? "
            "WHERE result_key=?",
            (int(original[0]), "competitor:wrong", str(result_id)),
        )
    with pytest.raises(CredibilityReactionError, match="issued membership differ"):
        credibility._active_settled_result(result_id)


def test_complete_field_projection_rejects_unsettled_members_and_digest_drift(tmp_path):
    pending_path = tmp_path / "pending"
    pending_path.mkdir()
    lifecycle, credibility, _signer, field_id, issue, _epoch = _authority(pending_path)
    lifecycle.record_live_result(
        _submission(field_id, "a", ResultStatus.COMPLETION),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:pending-result-a"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=4,
    )
    lifecycle.record_live_result(
        _submission(field_id, "b", ResultStatus.DNS),
        field_revision=1,
        claimed_receipt_id=StableIdentifier("receipt:heat-a"),
        command_id=IdempotencyKey("command:pending-result-b"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=5,
    )
    issue_payload = issue.command.payload.to_value()
    with pytest.raises(CredibilityReactionError, match="awaits every terminal"):
        credibility._complete_field_results(str(field_id), issue_payload, 1)

    settled_path = tmp_path / "settled"
    settled_path.mkdir()
    lifecycle, credibility, _signer, field_id, issue, _epoch = _authority(settled_path)
    result_id, _source = _settle(lifecycle, field_id)
    with open_v3_connection(lifecycle.projections.database_path) as connection:
        connection.execute(
            "UPDATE v3_result_revisions SET observation_digest=? WHERE result_key=?",
            ("0" * 64, str(result_id)),
        )
    with pytest.raises(CredibilityReactionError, match="revision digest is invalid"):
        credibility._complete_field_results(str(field_id), issue.command.payload.to_value(), 1)


@pytest.mark.parametrize(
    ("evaluator", "message"),
    ((UntypedEvaluator(), "no typed receipt"), (MisboundEvaluator(), "differs")),
)
def test_optimizer_evaluator_output_is_reverified_before_score_authority(
    tmp_path, evaluator, message
):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path, evaluator)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.FORMULA)
    result_id, _source = _settle(lifecycle, field_id)
    with pytest.raises(CredibilityReactionError, match=message):
        credibility.react_result(
            result_id,
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=7,
        )
    assert not credibility.load_ledger().scores


@pytest.mark.parametrize("checkpoint", ("after_scoring", "after_weights"))
def test_reaction_restart_resumes_each_persisted_crash_checkpoint_without_rescoring(
    tmp_path, checkpoint
):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.FORMULA, 1)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.ML, 2)
    result_id, _source = _settle(lifecycle, field_id)

    def crash(name):
        if name == checkpoint:
            raise RuntimeError(f"crash:{name}")

    with pytest.raises(RuntimeError, match=checkpoint):
        credibility.react_result(
            result_id,
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=7,
            fault_hook=crash,
        )
    assert Evaluator.calls == 2
    restarted = SQLiteCredibilityReactionService(
        lifecycle.projections.database_path,
        trust_store=IntegrityTrustStore((signer.identity,)),
        consequence_evaluator=Evaluator(),
        policy_manifest=_policy(signer),
    )
    ledger, _weights = restarted.react_result(
        result_id, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=8
    )
    assert Evaluator.calls == 2
    assert len(ledger.active_scores) == 2
    assert restarted._credibility_reaction_complete(_source)


def test_live_controls_are_atomic_and_exact_retry_returns_original_after_restart(
    tmp_path, monkeypatch
):
    _lifecycle, credibility, signer, tournament, _completed, _successor = _empty_live_authority(
        tmp_path
    )
    barrier = Barrier(2)
    original = credibility._append_event

    def synchronized_append(**arguments):
        if arguments["payload"].get("schema_version") == (
            "strathmark-v3-live-credibility-control-v1"
        ):
            barrier.wait(timeout=10)
        return original(**arguments)

    monkeypatch.setattr(credibility, "_append_event", synchronized_append)

    def control(index):
        command_id = IdempotencyKey(f"command:concurrent-control-{index}")
        try:
            result = credibility.record_live_control(
                tournament,
                action="suspend",
                reason=f"judge reason {index}",
                command_id=command_id,
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=10 + index,
            )
            return command_id, f"judge reason {index}", result
        except CredibilityReactionError as exc:
            return command_id, f"judge reason {index}", exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(control, (1, 2)))
    winners = [item for item in outcomes if not isinstance(item[2], Exception)]
    losers = [item for item in outcomes if isinstance(item[2], Exception)]
    assert len(winners) == len(losers) == 1
    original_result = winners[0][2]
    restarted = SQLiteCredibilityReactionService(
        credibility.database_path,
        trust_store=IntegrityTrustStore((signer.identity,)),
        consequence_evaluator=None,
        policy_manifest=_policy(signer),
    )
    exact = restarted.record_live_control(
        tournament,
        action="suspend",
        reason=winners[0][1],
        command_id=winners[0][0],
        actor_id=ACTOR,
        occurred_at_utc="2026-08-23T12:01:00.000Z",
        monotonic_elapsed_ms=999,
    )
    assert exact == original_result
    with pytest.raises(CredibilityReactionError, match="changed its original request"):
        restarted.record_live_control(
            tournament,
            action="suspend",
            reason="changed retry reason",
            command_id=winners[0][0],
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1_000,
        )
    target = reactions.deterministic_identifier("weights", {"tournament_id": str(tournament)})
    stale_before = {
        "tournament_id": str(tournament),
        "enabled": True,
        "suspended": False,
        "emergency_stopped": False,
        "expired": False,
    }
    stale_after = {**stale_before, "suspended": True}
    stale_payload = {
        "schema_version": "strathmark-v3-live-credibility-control-v1",
        "tournament_id": str(tournament),
        "action": "suspend",
        "reason": "stale before-state",
        "before_digest": canonical_digest(stale_before),
        "after_digest": canonical_digest(stale_after),
        "before": stale_before,
        "after": stale_after,
    }
    with pytest.raises(reactions.EventStoreConflict, match="before-state changed"):
        restarted._append_event(
            command_kind=CommandKind.SUSPEND_LIVE,
            event_kind=EventKind.LIVE_SUSPENDED,
            aggregate_kind=AggregateKind.WEIGHTS,
            aggregate_id=target,
            payload=stale_payload,
            result={"stale": True},
            command_id=IdempotencyKey("command:stale-live-control"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1_000,
            projection_hook=restarted._live_control_guard(
                target=target, tournament_id=tournament, payload=stale_payload
            ),
        )
    append_after_restart = restarted._append_event
    closed = False

    def close_during_control(**arguments):
        nonlocal closed
        if not closed:
            closed = True
            append_after_restart(
                command_kind=CommandKind.CLOSE_TOURNAMENT,
                event_kind=EventKind.TOURNAMENT_CLOSED,
                aggregate_kind=AggregateKind.TOURNAMENT,
                aggregate_id=tournament,
                payload={"reason": "race close"},
                result={"closed": True},
                command_id=IdempotencyKey("command:close-during-control"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=1_001,
            )
        return append_after_restart(**arguments)

    monkeypatch.setattr(restarted, "_append_event", close_during_control)
    with pytest.raises(CredibilityReactionError, match="concurrent authority"):
        restarted.record_live_control(
            tournament,
            action="re_enable",
            reason="racing close",
            command_id=IdempotencyKey("command:control-during-close"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=1_002,
        )


def test_round_freeze_is_atomically_unique_across_threads_and_restart(tmp_path, monkeypatch):
    _lifecycle, credibility, signer, tournament, completed, successor = _empty_live_authority(
        tmp_path
    )
    context = ContextNode()
    credibility._tournament_baseline(
        tournament,
        context,
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=19,
    )
    barrier = Barrier(2)
    original = credibility._append_event

    def synchronized_append(**arguments):
        if arguments["payload"].get("schema_version") == (
            "strathmark-v3-live-round-weight-freeze-v1"
        ):
            barrier.wait(timeout=10)
        return original(**arguments)

    monkeypatch.setattr(credibility, "_append_event", synchronized_append)

    def freeze(index):
        command_id = IdempotencyKey(f"command:concurrent-freeze-{index}")
        try:
            result = credibility.freeze_live_weights(
                completed,
                successor,
                context=context,
                command_id=command_id,
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=20 + index,
            )
            return command_id, result
        except CredibilityReactionError as exc:
            return command_id, exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(freeze, (1, 2)))
    winners = [item for item in outcomes if not isinstance(item[1], Exception)]
    losers = [item for item in outcomes if isinstance(item[1], Exception)]
    assert len(winners) == len(losers) == 1
    with open_v3_connection(credibility.database_path, read_only=True) as connection:
        rows = connection.execute(
            "SELECT envelope_json FROM v3_events WHERE aggregate_kind=? AND event_kind=?",
            (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
        ).fetchall()
    freezes = []
    for row in rows:
        event = reactions.EventEnvelope.from_dict(json.loads(str(row[0])))
        value = event.command.payload.to_value()
        if value.get("schema_version") == "strathmark-v3-live-round-weight-freeze-v1":
            freezes.append(event)
    assert len(freezes) == 1
    persisted = credibility._load_round_freeze(successor)
    assert persisted is not None
    persisted_event, persisted_payload = persisted
    wrong_round_payload = {**persisted_payload, "completed_round_id": "round:other"}
    with monkeypatch.context() as patch:
        patch.setattr(
            credibility,
            "_load_round_freeze",
            lambda _round_id: (persisted_event, wrong_round_payload),
        )
        with pytest.raises(CredibilityReactionError, match="changed round authority"):
            credibility.freeze_live_weights(
                completed,
                successor,
                context=context,
                command_id=winners[0][0],
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=998,
            )
    restarted = SQLiteCredibilityReactionService(
        credibility.database_path,
        trust_store=IntegrityTrustStore((signer.identity,)),
        consequence_evaluator=None,
        policy_manifest=_policy(signer),
    )
    exact = restarted.freeze_live_weights(
        completed,
        successor,
        context=context,
        command_id=winners[0][0],
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=999,
    )
    assert exact.current_weights == winners[0][1].current_weights


def test_tournament_baseline_concurrent_creation_recovers_the_winning_receipt(
    tmp_path, monkeypatch
):
    _lifecycle, credibility, _signer, tournament, _completed, _successor = _empty_live_authority(
        tmp_path
    )
    barrier = Barrier(2)
    original = credibility._append_event

    def synchronized_append(**arguments):
        if arguments["payload"].get("schema_version") == (
            "strathmark-v3-tournament-baseline-snapshot-v1"
        ):
            barrier.wait(timeout=10)
        return original(**arguments)

    monkeypatch.setattr(credibility, "_append_event", synchronized_append)

    def baseline(index):
        return credibility._tournament_baseline(
            tournament,
            ContextNode(),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=40 + index,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = tuple(pool.map(baseline, (1, 2)))
    assert receipts[0] == receipts[1]


def test_tournament_baseline_recovers_a_persisted_concurrent_winner(tmp_path, monkeypatch):
    _lifecycle, credibility, _signer, tournament, _completed, _successor = _empty_live_authority(
        tmp_path
    )
    credibility.record_live_control(
        tournament,
        action="suspend",
        reason="preexisting non-baseline event",
        command_id=IdempotencyKey("command:preexisting-baseline-control"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=44,
    )
    target = reactions.deterministic_identifier("weights", {"tournament_id": str(tournament)})
    credibility._append_event(
        command_kind=CommandKind.CHANGE_WEIGHTS,
        event_kind=EventKind.WEIGHTS_CHANGED,
        aggregate_kind=AggregateKind.WEIGHTS,
        aggregate_id=target,
        payload={"schema_version": "non-baseline-weight-event-v1"},
        result={"non_baseline": True},
        command_id=IdempotencyKey("command:preexisting-non-baseline-weight"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=44,
    )
    original = credibility._append_event

    def persisted_then_conflict(**arguments):
        result = original(**arguments)
        if arguments["payload"].get("schema_version") == (
            "strathmark-v3-tournament-baseline-snapshot-v1"
        ):
            raise reactions.EventStoreConflict("simulated concurrent winner")
        return result

    monkeypatch.setattr(credibility, "_append_event", persisted_then_conflict)
    receipt = credibility._tournament_baseline(
        tournament,
        ContextNode(),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=45,
    )
    assert receipt.calibration_cutoff_at_utc == NOW


def test_tournament_baseline_does_not_hide_an_unrelated_append_conflict(tmp_path, monkeypatch):
    _lifecycle, credibility, _signer, tournament, _completed, _successor = _empty_live_authority(
        tmp_path
    )

    def unrelated_conflict(**_arguments):
        raise reactions.EventStoreConflict("unrelated append conflict")

    monkeypatch.setattr(credibility, "_append_event", unrelated_conflict)
    with pytest.raises(reactions.EventStoreConflict, match="unrelated append"):
        credibility._tournament_baseline(
            tournament,
            ContextNode(),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=46,
        )


def test_field_issue_and_live_freeze_race_has_one_valid_sqlite_order(tmp_path, monkeypatch):
    lifecycle, credibility, _signer, tournament, completed, successor = _empty_live_authority(
        tmp_path
    )
    field = StableIdentifier("field:empty-next-a")
    target = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1")
    _snapshot(
        lifecycle,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            field,
            1,
            tournament,
            successor,
            {
                "competitor_ids": ["competitor:a"],
                "target_context": target.to_dict(),
                "stand_ids": ["stand:one"],
            },
        ),
        "race-next-field",
    )
    with open_v3_connection(credibility.database_path, read_only=True) as connection:
        epoch = connection.execute(
            "SELECT epoch_id FROM v3_evidence_epochs WHERE round_id=?",
            (str(successor),),
        ).fetchone()
    assert epoch is not None
    _append(
        lifecycle,
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        AggregateKind.FIELD,
        field,
        {"round_id": str(successor), "epoch_id": str(epoch[0]), "field_revision": 1},
        "race-optimize-field",
    )
    issue_store = reactions.SQLiteEventStore(credibility.database_path)
    issue_head = issue_store.aggregate_head(str(field))
    issue_payload = {
        "round_id": str(successor),
        "epoch_id": str(epoch[0]),
        "field_revision": 1,
        "receipt_id": "receipt:race-next",
        "competitor_ids": ["competitor:a"],
        "issued_marks": {"competitor:a": 3},
    }
    issue_command = reactions.CommandEnvelope(
        CommandKind.ACKNOWLEDGE_ISSUE,
        IdempotencyKey("command:race-issue-field"),
        field,
        ((str(field), 0 if issue_head is None else issue_head[0]),),
        ACTOR,
        reactions.InlinePayload.from_value(issue_payload),
    )
    issue_request = reactions.CommandRequest(
        ACTOR,
        issue_command,
        (reactions.EventIntent(AggregateKind.FIELD, field, EventKind.FIELD_ISSUED),),
        "test-result-v1",
        {"accepted": True},
        NOW,
        1,
    )
    barrier = Barrier(2)
    original = credibility._append_event

    def synchronized_append(**arguments):
        if arguments["payload"].get("schema_version") == (
            "strathmark-v3-live-round-weight-freeze-v1"
        ):
            barrier.wait(timeout=10)
        return original(**arguments)

    monkeypatch.setattr(credibility, "_append_event", synchronized_append)

    def freeze():
        try:
            return credibility.freeze_live_weights(
                completed,
                successor,
                context=ContextNode(),
                command_id=IdempotencyKey("command:issue-race-freeze"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=30,
            )
        except CredibilityReactionError as exc:
            return exc

    def issue():
        barrier.wait(timeout=10)
        return issue_store.execute(
            issue_request,
            projection_hook=lifecycle.projections.apply_events,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        freeze_future = pool.submit(freeze)
        issue_future = pool.submit(issue)
        freeze_outcome = freeze_future.result()
        issue_future.result()
    with open_v3_connection(credibility.database_path, read_only=True) as connection:
        issue_sequence = int(
            connection.execute(
                "SELECT global_sequence FROM v3_events WHERE aggregate_id=? AND event_kind=?",
                (str(field), EventKind.FIELD_ISSUED.value),
            ).fetchone()[0]
        )
        rows = connection.execute(
            "SELECT global_sequence, envelope_json FROM v3_events WHERE aggregate_kind=? "
            "AND event_kind=? ORDER BY global_sequence",
            (AggregateKind.WEIGHTS.value, EventKind.WEIGHTS_CHANGED.value),
        ).fetchall()
    freeze_sequences = []
    for row in rows:
        event = reactions.EventEnvelope.from_dict(json.loads(str(row[1])))
        if event.command.payload.to_value().get("schema_version") == (
            "strathmark-v3-live-round-weight-freeze-v1"
        ):
            freeze_sequences.append(int(row[0]))
    if isinstance(freeze_outcome, Exception):
        assert freeze_sequences == []
    else:
        assert len(freeze_sequences) == 1
        assert freeze_sequences[0] < issue_sequence


def test_live_freeze_requires_persisted_close_and_next_epoch_then_survives_restart(
    tmp_path, monkeypatch
):
    lifecycle, credibility, signer, field_id, issue, epoch = _authority(tmp_path)
    _commit(credibility, signer, field_id, issue, epoch, AssessorKind.FORMULA)
    result_a, source_a = _settle(lifecycle, field_id)
    credibility.react_result(result_a, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=7)
    with open_v3_connection(lifecycle.projections.database_path, read_only=True) as connection:
        result_b_row = connection.execute(
            "SELECT result_key, source_global_sequence FROM v3_result_revisions "
            "WHERE competitor_id='competitor:b'"
        ).fetchone()
    assert result_b_row is not None
    result_b = StableIdentifier(str(result_b_row[0]))
    source_b = int(result_b_row[1])
    credibility.react_result(result_b, actor_id=ACTOR, occurred_at_utc=NOW, monotonic_elapsed_ms=8)
    context = ContextNode("underhand", "300_349", "wood", "medium")
    with pytest.raises(CredibilityReactionError, match="ingress authority|closed round"):
        credibility.freeze_live_weights(
            StableIdentifier("round:heat"),
            StableIdentifier("round:quarter"),
            context=context,
            command_id=IdempotencyKey("command:early-live-freeze"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=9,
        )
    for source in (source_a, source_b):
        for reaction in MandatoryReaction:
            if reaction is MandatoryReaction.CREDIBILITY:
                continue
            lifecycle.complete_derivation_reaction(
                source,
                reaction,
                canonical_digest({"source": source, "reaction": reaction.value}),
                command_id=IdempotencyKey(f"command:live-{source}-{reaction.value}"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=10,
            )
    heat = StableIdentifier("round:heat")
    quarter = StableIdentifier("round:quarter")
    _start_round_close(lifecycle, heat, "credibility-live")
    closure, _event = lifecycle.close_evidence_round(
        heat,
        command_id=IdempotencyKey("command:credibility-close-heat"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=11,
    )
    _snapshot(
        lifecycle,
        UpstreamSnapshot(
            SnapshotKind.ROUND,
            quarter,
            1,
            StableIdentifier("tournament:show"),
            quarter,
            {
                "round_ordinal": 2,
                "predecessor_round_ids": [str(heat)],
                "successor_round_ids": [],
            },
        ),
        "credibility-quarter",
    )
    _append(
        lifecycle,
        CommandKind.CONFIGURE_ROUND,
        EventKind.ROUND_CONFIGURED,
        AggregateKind.ROUND,
        quarter,
        {"configured": True},
        "credibility-configure-quarter",
    )
    with pytest.raises(CredibilityReactionError, match="closed round and next epoch"):
        credibility.freeze_live_weights(
            heat,
            quarter,
            context=context,
            command_id=IdempotencyKey("command:freeze-before-next-epoch"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=12,
        )
    quarter_epoch, _quarter_epoch_event = lifecycle.freeze_round_epoch(
        quarter,
        epoch_revision=1,
        historical_cutoff_key="history:prior",
        closure_ids=(closure,),
        command_id=IdempotencyKey("command:credibility-freeze-quarter"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=12,
    )
    with open_v3_connection(lifecycle.projections.database_path) as connection:
        epoch_row = connection.execute(
            "SELECT maximum_tournament_sequence FROM v3_evidence_epochs WHERE round_id=?",
            (str(quarter),),
        ).fetchone()
        assert epoch_row is not None
        connection.execute(
            "UPDATE v3_evidence_epochs SET maximum_tournament_sequence=0 WHERE round_id=?",
            (str(quarter),),
        )
    with pytest.raises(CredibilityReactionError, match="predates completed-round closure"):
        credibility.freeze_live_weights(
            heat,
            quarter,
            context=context,
            command_id=IdempotencyKey("command:freeze-stale-next-epoch"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=12,
        )
    with open_v3_connection(lifecycle.projections.database_path) as connection:
        snapshot_row = connection.execute(
            "SELECT snapshot_json FROM v3_ingress_snapshots "
            "WHERE entity_kind='round' AND entity_id=?",
            (str(quarter),),
        ).fetchone()
        assert snapshot_row is not None
        original_snapshot = str(snapshot_row[0])
        connection.execute(
            "UPDATE v3_evidence_epochs SET maximum_tournament_sequence=? WHERE round_id=?",
            (int(epoch_row[0]), str(quarter)),
        )
        changed_snapshot = json.loads(original_snapshot)
        changed_snapshot["predecessor_round_ids"] = []
        connection.execute(
            "UPDATE v3_ingress_snapshots SET snapshot_json=? "
            "WHERE entity_kind='round' AND entity_id=?",
            (json.dumps(changed_snapshot, sort_keys=True), str(quarter)),
        )
    with pytest.raises(CredibilityReactionError, match="declared predecessor"):
        credibility.freeze_live_weights(
            heat,
            quarter,
            context=context,
            command_id=IdempotencyKey("command:freeze-wrong-predecessor"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=12,
        )
    with open_v3_connection(lifecycle.projections.database_path) as connection:
        connection.execute(
            "UPDATE v3_ingress_snapshots SET snapshot_json=? "
            "WHERE entity_kind='round' AND entity_id=?",
            (original_snapshot, str(quarter)),
        )
    for action, reason, message in (
        ("suspend", "", "explicit reason"),
        ("unknown", "judge review", "action is unknown"),
    ):
        with pytest.raises(CredibilityReactionError, match=message):
            credibility.record_live_control(
                StableIdentifier("tournament:show"),
                action=action,
                reason=reason,
                command_id=IdempotencyKey(f"command:invalid-live-{action}"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=12,
            )
    credibility.record_live_control(
        StableIdentifier("tournament:show"),
        action="emergency_stop",
        reason="integrity alarm",
        command_id=IdempotencyKey("command:emergency-before-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=12,
    )
    credibility.record_live_control(
        StableIdentifier("tournament:show"),
        action="re_enable",
        reason="signed recovery",
        command_id=IdempotencyKey("command:reenable-before-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=12,
    )
    credibility.record_live_control(
        StableIdentifier("tournament:show"),
        action="suspend",
        reason="judge review",
        command_id=IdempotencyKey("command:suspend-before-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=12,
    )
    with pytest.raises(CredibilityReactionError, match="typed target context"):
        credibility.freeze_live_weights(
            heat,
            quarter,
            context=None,
            command_id=IdempotencyKey("command:invalid-live-context"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=13,
        )
    frozen = credibility.freeze_live_weights(
        heat,
        quarter,
        context=context,
        command_id=IdempotencyKey("command:credibility-live-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=13,
    )
    assert frozen.rounds[-1].round_id == str(quarter) and frozen.suspended
    exact = credibility.freeze_live_weights(
        heat,
        quarter,
        context=context,
        command_id=IdempotencyKey("command:credibility-live-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=13,
    )
    assert exact.current_weights == frozen.current_weights and exact.suspended
    with pytest.raises(CredibilityReactionError, match="changed target context"):
        credibility.freeze_live_weights(
            heat,
            quarter,
            context=ContextNode("underhand", "300_349", "wood", "deep"),
            command_id=IdempotencyKey("command:credibility-live-freeze"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=13,
        )
    with pytest.raises(CredibilityReactionError, match="already frozen"):
        credibility.freeze_live_weights(
            heat,
            quarter,
            context=context,
            command_id=IdempotencyKey("command:duplicate-live-freeze"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=14,
        )
    restarted = SQLiteCredibilityReactionService(
        lifecycle.projections.database_path,
        trust_store=IntegrityTrustStore((signer.identity,)),
        consequence_evaluator=Evaluator(),
        policy_manifest=_policy(signer),
    )
    assert (
        restarted.load_ledger().current_projection_digest
        == credibility.load_ledger().current_projection_digest
    )
    restarted_exact = restarted.freeze_live_weights(
        heat,
        quarter,
        context=context,
        command_id=IdempotencyKey("command:credibility-live-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=13,
    )
    assert restarted_exact.current_weights == frozen.current_weights
    assert restarted_exact.suspended and restarted_exact.enabled
    quarter_field = StableIdentifier("field:quarter-a")
    target_context = TargetContext("underhand", 300, "wood", "tax:v1", "convert:v1")
    _snapshot(
        lifecycle,
        UpstreamSnapshot(
            SnapshotKind.FIELD,
            quarter_field,
            1,
            StableIdentifier("tournament:show"),
            quarter,
            {
                "competitor_ids": ["competitor:a"],
                "target_context": target_context.to_dict(),
                "stand_ids": ["stand:one"],
            },
        ),
        "credibility-issued-quarter-field",
    )
    _append(
        lifecycle,
        CommandKind.OPTIMIZE_FIELD,
        EventKind.FIELD_OPTIMIZED,
        AggregateKind.FIELD,
        quarter_field,
        {
            "round_id": str(quarter),
            "epoch_id": str(quarter_epoch.epoch_id),
            "field_revision": 1,
        },
        "credibility-optimize-quarter-field",
    )
    _append(
        lifecycle,
        CommandKind.ACKNOWLEDGE_ISSUE,
        EventKind.FIELD_ISSUED,
        AggregateKind.FIELD,
        quarter_field,
        {
            "round_id": str(quarter),
            "epoch_id": str(quarter_epoch.epoch_id),
            "field_revision": 1,
            "receipt_id": "receipt:quarter-a",
            "competitor_ids": ["competitor:a"],
            "issued_marks": {"competitor:a": 3},
        },
        "credibility-issue-quarter-field",
    )
    with monkeypatch.context() as patch:
        patch.setattr(credibility, "_load_round_freeze", lambda _round_id: None)
        with pytest.raises(CredibilityReactionError, match="after next field issue"):
            credibility.freeze_live_weights(
                heat,
                quarter,
                context=context,
                command_id=IdempotencyKey("command:freeze-after-next-issue"),
                actor_id=ACTOR,
                occurred_at_utc=NOW,
                monotonic_elapsed_ms=15,
            )
    _freeze_event, freeze_payload = credibility._load_round_freeze(quarter)
    target = reactions.deterministic_identifier("weights", {"tournament_id": "tournament:show"})
    with pytest.raises(reactions.EventStoreConflict, match="atomically unique"):
        credibility._append_event(
            command_kind=CommandKind.CHANGE_WEIGHTS,
            event_kind=EventKind.WEIGHTS_CHANGED,
            aggregate_kind=AggregateKind.WEIGHTS,
            aggregate_id=target,
            payload=freeze_payload,
            result={"guarded_duplicate": True},
            command_id=IdempotencyKey("command:guarded-duplicate-live-freeze"),
            actor_id=ACTOR,
            occurred_at_utc=NOW,
            monotonic_elapsed_ms=16,
            projection_hook=credibility._round_freeze_guard(
                tournament_id=StableIdentifier("tournament:show"),
                next_round_id=quarter,
            ),
        )
    credibility._append_event(
        command_kind=CommandKind.CHANGE_WEIGHTS,
        event_kind=EventKind.WEIGHTS_CHANGED,
        aggregate_kind=AggregateKind.WEIGHTS,
        aggregate_id=target,
        payload=freeze_payload,
        result={"duplicate": True},
        command_id=IdempotencyKey("command:duplicate-persisted-live-freeze"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=16,
    )
    with pytest.raises(CredibilityReactionError, match="duplicate persisted"):
        credibility._load_round_freeze(quarter)
    credibility._append_event(
        command_kind=CommandKind.SUSPEND_LIVE,
        event_kind=EventKind.LIVE_SUSPENDED,
        aggregate_kind=AggregateKind.WEIGHTS,
        aggregate_id=target,
        payload={
            "schema_version": "strathmark-v3-live-credibility-control-v1",
            "after": None,
        },
        result={"malformed": True},
        command_id=IdempotencyKey("command:malformed-live-control"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=17,
    )
    with pytest.raises(CredibilityReactionError, match="control state is malformed"):
        credibility._load_live_control_state(target, StableIdentifier("tournament:show"))
    credibility._append_event(
        command_kind=CommandKind.CLOSE_TOURNAMENT,
        event_kind=EventKind.TOURNAMENT_CLOSED,
        aggregate_kind=AggregateKind.TOURNAMENT,
        aggregate_id=StableIdentifier("tournament:show"),
        payload={"closed": True},
        result={"closed": True},
        command_id=IdempotencyKey("command:credibility-close-tournament"),
        actor_id=ACTOR,
        occurred_at_utc=NOW,
        monotonic_elapsed_ms=18,
    )
    with pytest.raises(CredibilityReactionError, match="expired at tournament close"):
        credibility._require_open_tournament(StableIdentifier("tournament:show"))
