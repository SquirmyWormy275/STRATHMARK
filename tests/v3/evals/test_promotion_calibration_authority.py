from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.contracts.forecasts import AssessorKind
from strathmark.v3.domain.capability import (
    CapabilityPromotionPolicy,
    PromotionScoreRetention,
    evaluate_capability_promotion,
)
from strathmark.v3.domain.credibility import (
    CredibilityPolicy,
    SelectiveAbstentionTrial,
    evaluate_selective_abstention_trials,
)
from strathmark.v3.domain.disagreement import (
    ConsequenceColor,
    DisagreementThresholds,
    ThresholdReplayObservation,
    freeze_disagreement_policy,
    select_historical_thresholds,
    verify_disjoint_thresholds,
)
from strathmark.v3.factory.evaluator import (
    AuditGenerationRegistry,
    EvaluationError,
    EvaluationGate,
    FrozenEvaluationHarness,
    FrozenEvaluator,
    PromotionCalibrationEvidence,
)
from strathmark.v3.infrastructure.integrity import P256EphemeralSigner
from tests.v3.evals.test_factory_audit_isolation import DIGESTS, _candidate

NOW = "2026-08-25T10:00:00.000Z"


def _capability(candidate_digest: str):
    retentions = tuple(
        PromotionScoreRetention(
            assessor,
            DIGESTS[index],
            DIGESTS[index + 3],
            "0.2",
            "0.22",
        )
        for index, assessor in enumerate(
            (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)
        )
    )
    return evaluate_capability_promotion(
        candidate_digest=candidate_digest,
        retentions=retentions,
        operator_application_counts=(
            (AssessorKind.FORMULA, 1),
            (AssessorKind.ML, 1),
            (AssessorKind.LLM_COUNCIL, 1),
        ),
        policy=CapabilityPromotionPolicy(max_adjusted_score_regression="0.05"),
    )


def _selective(candidate_digest: str):
    return evaluate_selective_abstention_trials(
        candidate_digest=candidate_digest,
        policy=CredibilityPolicy(),
        trials=(
            SelectiveAbstentionTrial(
                "honest",
                "honest_baseline",
                expected_opportunity_mass="10",
                recorded_opportunity_mass="10",
                successful_mass="10",
                invalid_mass="0",
                claimed_principled_abstention_mass="0",
                predictive_loss="0.2",
            ),
            SelectiveAbstentionTrial(
                "missing",
                "selective_missing",
                expected_opportunity_mass="10",
                recorded_opportunity_mass="5",
                successful_mass="5",
                invalid_mass="0",
                claimed_principled_abstention_mass="0",
                predictive_loss="0.1",
            ),
            SelectiveAbstentionTrial(
                "invalid",
                "invalid_as_abstention",
                expected_opportunity_mass="10",
                recorded_opportunity_mass="10",
                successful_mass="5",
                invalid_mass="5",
                claimed_principled_abstention_mass="5",
                predictive_loss="0.1",
            ),
        ),
    )


def _observation(identity: str, expected: ConsequenceColor, delta: int):
    return ThresholdReplayObservation(
        identity,
        median_delta_ms=delta * 100,
        interval_endpoint_delta_ms=delta * 100,
        mark_delta=delta,
        win_probability_delta=f"0.{delta}",
        spread_delta_ms=delta * 100,
        ordering_reversal=False,
        expected_color=expected,
    )


def _threshold_authority():
    conservative = DisagreementThresholds(
        "thresholds:conservative",
        100,
        500,
        100,
        500,
        0,
        2,
        "0.05",
        "0.2",
        100,
        500,
    )
    permissive = replace(
        conservative,
        threshold_id="thresholds:permissive",
        green_median_delta_ms=200,
        green_interval_endpoint_delta_ms=200,
        green_mark_delta=1,
        green_win_probability_delta="0.1",
        green_spread_delta_ms=200,
    )
    replay = (
        _observation("replay:green", ConsequenceColor.GREEN, 1),
        _observation("replay:red", ConsequenceColor.RED, 2),
    )
    selection = select_historical_thresholds((permissive, conservative), replay)
    verification = verify_disjoint_thresholds(
        selection,
        (_observation("holdout:green", ConsequenceColor.GREEN, 1),),
        minimum_accuracy="1",
    )
    policy = freeze_disagreement_policy(selection, verification)
    return selection, verification, policy


def _evidence(candidate_digest: str) -> PromotionCalibrationEvidence:
    selection, verification, policy = _threshold_authority()
    return PromotionCalibrationEvidence.create(
        candidate_digest=candidate_digest,
        capability=_capability(candidate_digest),
        selective_abstention=_selective(candidate_digest),
        threshold_selection=selection,
        threshold_verification=verification,
        disagreement_policy=policy,
        member_weight_authority_digest=DIGESTS[20],
    )


def _evaluator(tmp_path, signer):
    harness = FrozenEvaluationHarness.create(
        generation_id="audit:calibration",
        audit_snapshot_digest=DIGESTS[22],
        harness_code_digest=DIGESTS[23],
        precommit_digest=DIGESTS[24],
        gates=(EvaluationGate("normalized_crps", "lte", 0.25),),
        frozen_at=NOW,
    )
    return FrozenEvaluator(
        harness,
        AuditGenerationRegistry(tmp_path / "audit"),
        signer=signer,
    )


def test_promotion_report_transitively_binds_all_calibration_authorities(tmp_path) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:promotion-calibration")
    candidate = _candidate(name="calibrated")
    evidence = _evidence(candidate.candidate_digest)

    report = _evaluator(tmp_path, signer).evaluate(
        candidate,
        metrics={"normalized_crps": 0.2},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at=NOW,
        promotion_evidence=evidence,
    )

    assert report.passed is True
    assert report.promotion_authorized is True
    assert report.promotion_evidence_digest == evidence.evidence_digest


def test_diagnostic_report_and_cross_candidate_evidence_cannot_authorize_promotion(
    tmp_path,
) -> None:
    signer = P256EphemeralSigner.generate("integrity-key:promotion-diagnostic")
    candidate = _candidate(name="diagnostic")
    diagnostic = _evaluator(tmp_path / "diagnostic", signer).evaluate(
        candidate,
        metrics={"normalized_crps": 0.2},
        observed_audit_snapshot_digest=DIGESTS[22],
        created_at=NOW,
    )
    assert diagnostic.passed is True
    assert diagnostic.promotion_authorized is False
    assert diagnostic.promotion_evidence_digest is None

    other = _candidate(name="other", dependency_digest=DIGESTS[30])
    with pytest.raises(EvaluationError, match="candidate"):
        _evaluator(tmp_path / "cross", signer).evaluate(
            candidate,
            metrics={"normalized_crps": 0.2},
            observed_audit_snapshot_digest=DIGESTS[22],
            created_at=NOW,
            promotion_evidence=_evidence(other.candidate_digest),
        )


def test_capability_and_selective_adversaries_fail_closed() -> None:
    candidate_digest = DIGESTS[1]
    with pytest.raises(ValueError, match="exactly once"):
        evaluate_capability_promotion(
            candidate_digest=candidate_digest,
            retentions=_capability(candidate_digest).retentions,
            operator_application_counts=(
                (AssessorKind.FORMULA, 2),
                (AssessorKind.ML, 1),
                (AssessorKind.LLM_COUNCIL, 1),
            ),
            policy=CapabilityPromotionPolicy(max_adjusted_score_regression="0.05"),
        )
    overprotected = tuple(
        replace(item, adjusted_score="0.4") if item.assessor is AssessorKind.FORMULA else item
        for item in _capability(candidate_digest).retentions
    )
    failed = evaluate_capability_promotion(
        candidate_digest=candidate_digest,
        retentions=overprotected,
        operator_application_counts=(
            (AssessorKind.FORMULA, 1),
            (AssessorKind.ML, 1),
            (AssessorKind.LLM_COUNCIL, 1),
        ),
        policy=CapabilityPromotionPolicy(max_adjusted_score_regression="0.05"),
    )
    assert failed.passed is False
    with pytest.raises(EvaluationError, match="capability evidence"):
        PromotionCalibrationEvidence.create(
            candidate_digest=candidate_digest,
            capability=failed,
            selective_abstention=_selective(candidate_digest),
            threshold_selection=_threshold_authority()[0],
            threshold_verification=_threshold_authority()[1],
            disagreement_policy=_threshold_authority()[2],
            member_weight_authority_digest=DIGESTS[20],
        )
    selective = _selective(candidate_digest)
    assert selective.passed is True
    scores = dict(selective.effective_scores)
    assert scores["missing"] < scores["honest"]
    assert scores["invalid"] < scores["honest"]


def test_threshold_selection_is_deterministic_and_holdout_must_be_disjoint() -> None:
    selection, verification, policy = _threshold_authority()
    assert selection.selected_threshold_id == "thresholds:permissive"
    assert verification.passed is True
    assert policy.replay_evidence_digest == selection.selection_digest
    assert policy.disjoint_verification_digest == verification.verification_digest
    with pytest.raises(ValueError, match="disjoint"):
        verify_disjoint_thresholds(
            selection,
            (_observation("replay:green", ConsequenceColor.GREEN, 1),),
            minimum_accuracy="1",
        )
    object.__setattr__(selection, "selection_digest", "0" * 64)
    with pytest.raises(EvaluationError, match="threshold authority"):
        PromotionCalibrationEvidence.create(
            candidate_digest=DIGESTS[1],
            capability=_capability(DIGESTS[1]),
            selective_abstention=_selective(DIGESTS[1]),
            threshold_selection=selection,
            threshold_verification=verification,
            disagreement_policy=policy,
            member_weight_authority_digest=DIGESTS[20],
        )
