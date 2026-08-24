from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.contracts.forecasts import (
    AssessorKind,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.domain import credibility as credibility_module
from strathmark.v3.domain.credibility import (
    ContextNode,
    CredibilityLedger,
    CredibilityPolicy,
    HandicapConsequenceMetrics,
    LedgerReversal,
    Opportunity,
    OpportunityOutcome,
    OptimizerConsequenceReceipt,
    PredictiveMetrics,
    PredictiveScore,
    ScoreScope,
    compute_predictive_metrics,
    effective_degraded_weights,
)
from strathmark.v3.domain.credibility import (
    calibrate_baseline as _calibrate_baseline,
)

CUTOFF = "2026-08-23T12:00:00.000Z"
AUTHORITY_DIGEST = "f" * 64


def calibrate_baseline(ledger, context, policy):
    return _calibrate_baseline(ledger, context, policy, calibration_cutoff_at_utc=CUTOFF)


def distribution(*times: int) -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        tuple(
            QuantilePoint(probability, time)
            for probability, time in zip(("0.05", "0.1", "0.5", "0.9", "0.95"), times)
        )
    )


def test_frozen_predictive_scores_keep_each_metric_separate() -> None:
    metrics = compute_predictive_metrics(
        distribution(10_000, 10_000, 10_000, 10_000, 10_000),
        actual_time_ms=12_000,
        robust_context_scale_ms=10_000,
    )

    assert metrics.crps_ms == "2000"
    assert metrics.normalized_crps == "0.2"
    assert metrics.median_absolute_error_ms == 2_000
    assert metrics.median_bias_ms == -2_000
    assert metrics.tail_loss_ms == 2_000
    assert metrics.central_interval_covered is False
    assert metrics.sharpness_ms == 0
    assert metrics.calibration_residual == "0.5"


def test_crps_fixture_is_deterministic_for_piecewise_quantile_distribution() -> None:
    forecast = distribution(8_000, 9_000, 10_000, 13_000, 15_000)
    first = compute_predictive_metrics(
        forecast, actual_time_ms=11_000, robust_context_scale_ms=20_000
    )
    second = compute_predictive_metrics(
        forecast, actual_time_ms=11_000, robust_context_scale_ms=20_000
    )
    assert first == second
    assert first.crps_ms == "564.1666666666666666666666587"
    assert first.normalized_crps == "0.02820833333333333333333333294"
    assert first.median_absolute_error_ms == 1_000
    assert first.tail_loss_ms == 0
    assert first.central_interval_covered
    assert first.sharpness_ms == 4_000


def test_consequence_metrics_require_exact_shared_optimizer_receipt() -> None:
    metrics = HandicapConsequenceMetrics(
        spread_ms=1_200,
        win_probability_distortion="0.04",
        class_context_bias_ms=-300,
        gap_error_ms=250,
        breakout_exposure="0.02",
        optimizer_repair=True,
    )
    receipt = OptimizerConsequenceReceipt.verified(
        forecast_digest="a" * 64,
        result_revision_digest="b" * 64,
        field_receipt_digest="c" * 64,
        scoring_input_digest="e" * 64,
        optimizer_bundle_digest="d" * 64,
        metrics=metrics,
        authority_manifest_digest=AUTHORITY_DIGEST,
    )
    score = PredictiveScore.create(
        score_id="score:one",
        scope=ScoreScope.OPERATIONAL,
        assessor=AssessorKind.FORMULA,
        forecast_digest="a" * 64,
        result_id="result:one",
        result_revision=1,
        source_sequence=10,
        context=ContextNode("uh", "300_349", "eucalypt", "deep"),
        evidence_weight="1",
        metrics=compute_predictive_metrics(
            distribution(10_000, 10_000, 10_000, 10_000, 10_000),
            actual_time_ms=10_000,
            robust_context_scale_ms=10_000,
        ),
        consequence=receipt,
        settled_at_utc=CUTOFF,
    )
    assert score.consequence.metrics == metrics
    with pytest.raises(ValueError, match="receipt digest"):
        replace(receipt, receipt_digest="f" * 64)


def test_coverage_vocabulary_is_closed_and_invalid_is_not_abstention() -> None:
    assert OpportunityOutcome.PRINCIPLED_ABSTENTION.value == "principled_abstention"
    assert OpportunityOutcome.SCHEMA_INVALID is not OpportunityOutcome.PRINCIPLED_ABSTENTION
    assert OpportunityOutcome.TRANSPORT_FAILURE is not OpportunityOutcome.PRINCIPLED_ABSTENTION
    with pytest.raises(ValueError, match="OpportunityOutcome"):
        OpportunityOutcome("invalid_as_abstention")


def valid_ledger_items():
    forecast_digest = "a" * 64
    opportunity = Opportunity.create(
        opportunity_id="opportunity:valid",
        scope=ScoreScope.OPERATIONAL,
        assessor=AssessorKind.FORMULA,
        forecast_digest=forecast_digest,
        result_id="result:valid",
        result_revision=1,
        source_sequence=1,
        context=ContextNode("uh", "300", "gum", "deep"),
        eligible_at_forecast=True,
        outcome=OpportunityOutcome.SUCCESSFUL,
        difficulty="1",
    )
    consequence = OptimizerConsequenceReceipt.verified(
        forecast_digest=forecast_digest,
        result_revision_digest="b" * 64,
        field_receipt_digest="c" * 64,
        scoring_input_digest="e" * 64,
        optimizer_bundle_digest="d" * 64,
        metrics=HandicapConsequenceMetrics(1, "0", 0, 1, "0", False),
        authority_manifest_digest=AUTHORITY_DIGEST,
    )
    score = PredictiveScore.create(
        score_id="score:valid",
        scope=ScoreScope.OPERATIONAL,
        assessor=AssessorKind.FORMULA,
        forecast_digest=forecast_digest,
        result_id="result:valid",
        result_revision=1,
        source_sequence=1,
        context=opportunity.context,
        evidence_weight="1",
        metrics=PredictiveMetrics("1", "0.1", 1, 0, 0, True, 1, "0"),
        consequence=consequence,
        settled_at_utc=CUTOFF,
    )
    return opportunity, score, consequence


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: ContextNode("uh", "", None, None), "context dimensions"),
        (lambda: ContextNode("uh", None, "gum", None), "skip"),
        (lambda: PredictiveMetrics("1", "1", 1, True, 1, True, 1, "0"), "bias"),
        (lambda: PredictiveMetrics("1", "1", 1, 0, 1, 1, 1, "0"), "coverage"),
        (lambda: HandicapConsequenceMetrics(1, "0", True, 1, "0", False), "bias"),
        (lambda: HandicapConsequenceMetrics(1, "0", 0, 1, "0", 1), "repair"),
    ],
)
def test_closed_numeric_contracts_reject_invalid_shapes(factory, message) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_receipt_opportunity_score_and_reversal_fail_closed() -> None:
    opportunity, score, consequence = valid_ledger_items()
    with pytest.raises(ValueError, match="shared optimizer"):
        replace(consequence, evaluator_port="other")
    with pytest.raises(ValueError, match="typed"):
        replace(consequence, metrics={})
    with pytest.raises(ValueError, match="closed vocabulary"):
        replace(consequence, status="verified")
    pending = OptimizerConsequenceReceipt.pending(
        forecast_digest=consequence.forecast_digest,
        result_revision_digest=consequence.result_revision_digest,
        field_receipt_digest=consequence.field_receipt_digest,
        scoring_input_digest=consequence.scoring_input_digest,
        optimizer_bundle_digest=consequence.optimizer_bundle_digest,
    )
    with pytest.raises(ValueError, match="cannot claim metrics"):
        replace(pending, metrics=consequence.metrics)
    diagnostic = OptimizerConsequenceReceipt.create(
        forecast_digest=consequence.forecast_digest,
        result_revision_digest=consequence.result_revision_digest,
        field_receipt_digest=consequence.field_receipt_digest,
        scoring_input_digest=consequence.scoring_input_digest,
        optimizer_bundle_digest=consequence.optimizer_bundle_digest,
        metrics=consequence.metrics,
    )
    with pytest.raises(ValueError, match="cannot claim operational authority"):
        replace(diagnostic, authority_manifest_digest=AUTHORITY_DIGEST)
    with pytest.raises(ValueError, match="diagnostic consequence"):
        replace(score, consequence=diagnostic)

    for mutation, message in (
        ({"eligible_at_forecast": 1}, "eligibility"),
        ({"outcome": "successful"}, "outcome"),
        (
            {"eligible_at_forecast": False, "outcome": OpportunityOutcome.SUCCESSFUL},
            "ineligible forecasts",
        ),
        ({"outcome": OpportunityOutcome.INELIGIBLE}, "eligible forecasts"),
        ({"event_digest": "f" * 64}, "event digest"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(opportunity, **mutation)

    for mutation, message in (
        ({"evidence_weight": "0"}, "positive"),
        ({"metrics": {}}, "metrics"),
        ({"consequence": {}}, "mandatory"),
        (
            {
                "consequence": OptimizerConsequenceReceipt.verified(
                    forecast_digest="e" * 64,
                    result_revision_digest="b" * 64,
                    field_receipt_digest="c" * 64,
                    scoring_input_digest="e" * 64,
                    optimizer_bundle_digest="d" * 64,
                    metrics=consequence.metrics,
                    authority_manifest_digest=AUTHORITY_DIGEST,
                )
            },
            "binding",
        ),
        ({"event_digest": "f" * 64}, "event digest"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(score, **mutation)

    reversal = LedgerReversal.create(
        reversal_id="reversal:valid",
        target_kind="opportunity",
        target_id=opportunity.opportunity_id,
        original_result_revision=1,
        replacement_result_revision=2,
        source_sequence=2,
    )
    assert reversal.to_dict()["target_kind"] == "opportunity"
    for mutation, message in (
        ({"target_kind": "other"}, "target kind"),
        ({"replacement_result_revision": 1}, "advance"),
        ({"event_digest": "f" * 64}, "event digest"),
    ):
        with pytest.raises(ValueError, match=message):
            replace(reversal, **mutation)


def test_append_only_ledger_rejects_duplicates_mismatches_and_wrong_types() -> None:
    opportunity, score, _ = valid_ledger_items()
    empty = CredibilityLedger()
    with pytest.raises(ValueError, match="typed opportunity"):
        empty.append_opportunity({})
    ledger = empty.append_opportunity(opportunity)
    with pytest.raises(ValueError, match="identity"):
        ledger.append_opportunity(opportunity)
    same_key = Opportunity.create(
        **{
            **{
                name: getattr(opportunity, name)
                for name in (
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
                )
            },
            "opportunity_id": "opportunity:duplicate-key",
        }
    )
    with pytest.raises(ValueError, match="exactly one"):
        ledger.append_opportunity(same_key)
    with pytest.raises(ValueError, match="typed score"):
        ledger.append_score({})
    scored = ledger.append_score(score)
    with pytest.raises(ValueError, match="identity"):
        scored.append_score(score)
    with pytest.raises(ValueError, match="successful"):
        empty.append_score(score)
    with pytest.raises(ValueError, match="typed reversal"):
        scored.append_reversal({})
    inactive = LedgerReversal.create(
        reversal_id="reversal:inactive",
        target_kind="score",
        target_id="score:missing",
        original_result_revision=1,
        replacement_result_revision=2,
        source_sequence=2,
    )
    with pytest.raises(ValueError, match="not active"):
        scored.append_reversal(inactive)

    with pytest.raises(ValueError, match="opportunity ledger"):
        CredibilityLedger(({},))
    with pytest.raises(ValueError, match="score ledger"):
        CredibilityLedger((), ({},))
    with pytest.raises(ValueError, match="reversal ledger"):
        CredibilityLedger((), (), ({},))
    with pytest.raises(ValueError, match="unique"):
        CredibilityLedger((opportunity, opportunity))


def test_candidate_member_identity_and_ledger_serialization_are_explicit() -> None:
    opportunity, score, consequence = valid_ledger_items()
    with pytest.raises(ValueError, match="member identity"):
        Opportunity.create(
            **{
                **{
                    name: getattr(opportunity, name)
                    for name in credibility_module._OPPORTUNITY_FIELDS
                },
                "opportunity_id": "opportunity:missing-member",
                "scope": ScoreScope.CANDIDATE,
                "assessor": AssessorKind.LLM_MEMBER,
            }
        )
    with pytest.raises(ValueError, match="only an LLM member"):
        Opportunity.create(
            **{
                **{
                    name: getattr(opportunity, name)
                    for name in credibility_module._OPPORTUNITY_FIELDS
                },
                "opportunity_id": "opportunity:outer-member",
                "member_id": "member-one",
            }
        )
    candidate = Opportunity.create(
        **{
            **{name: getattr(opportunity, name) for name in credibility_module._OPPORTUNITY_FIELDS},
            "opportunity_id": "opportunity:candidate-member",
            "scope": ScoreScope.CANDIDATE,
            "assessor": AssessorKind.LLM_MEMBER,
            "member_id": "member-one",
        }
    )
    candidate_score = PredictiveScore.create(
        score_id="score:candidate-member",
        scope=ScoreScope.CANDIDATE,
        assessor=AssessorKind.LLM_MEMBER,
        forecast_digest=candidate.forecast_digest,
        result_id=candidate.result_id,
        result_revision=candidate.result_revision,
        source_sequence=candidate.source_sequence,
        context=candidate.context,
        evidence_weight="1",
        metrics=score.metrics,
        consequence=consequence,
        settled_at_utc=CUTOFF,
    )
    ledger = CredibilityLedger().append_opportunity(candidate).append_score(candidate_score)
    assert ledger.candidate_scores == (candidate_score,)
    assert ledger.to_dict()["scores"] == [candidate_score.to_dict()]


def test_policy_calibration_and_degraded_boundaries_fail_closed() -> None:
    for mutation, message in (
        ({"minimum_coverage": "1.1"}, "cannot exceed"),
        ({"weight_floor": "0.34"}, "floors/caps"),
        ({"weight_cap": "0.2"}, "floors/caps"),
    ):
        with pytest.raises(ValueError, match=message):
            CredibilityPolicy(**mutation)
    with pytest.raises(ValueError, match="typed ledger"):
        calibrate_baseline({}, ContextNode(), CredibilityPolicy())
    with pytest.raises(ValueError, match="frozen policy"):
        calibrate_baseline(CredibilityLedger(), ContextNode(), {})
    baseline = calibrate_baseline(CredibilityLedger(), ContextNode(), CredibilityPolicy())
    with pytest.raises(ValueError, match="nonempty"):
        effective_degraded_weights(baseline, ())
    with pytest.raises(ValueError, match="unique"):
        effective_degraded_weights(baseline, (AssessorKind.FORMULA, AssessorKind.FORMULA))
    with pytest.raises(ValueError, match="outer"):
        effective_degraded_weights(baseline, (AssessorKind.LLM_MEMBER,))
    opportunity, score, _consequence = valid_ledger_items()
    future = PredictiveScore.create(
        score_id="score:future",
        scope=score.scope,
        assessor=score.assessor,
        forecast_digest=score.forecast_digest,
        result_id=score.result_id,
        result_revision=score.result_revision,
        source_sequence=score.source_sequence,
        context=score.context,
        evidence_weight=score.evidence_weight,
        metrics=score.metrics,
        consequence=score.consequence,
        settled_at_utc="2026-08-24T12:00:00.000Z",
    )
    future_ledger = CredibilityLedger().append_opportunity(opportunity).append_score(future)
    with pytest.raises(ValueError, match="exceeds calibration cutoff"):
        calibrate_baseline(future_ledger, opportunity.context, CredibilityPolicy())


def test_prediction_metric_input_boundaries_and_distribution_tails() -> None:
    forecast = distribution(8_000, 9_000, 10_000, 13_000, 15_000)
    below = compute_predictive_metrics(
        forecast, actual_time_ms=7_000, robust_context_scale_ms=10_000
    )
    assert below.calibration_residual == "-0.5"
    with pytest.raises(ValueError, match="distribution"):
        compute_predictive_metrics({}, actual_time_ms=1, robust_context_scale_ms=1)
    with pytest.raises(ValueError, match="positive"):
        compute_predictive_metrics(forecast, actual_time_ms=0, robust_context_scale_ms=1)
    with pytest.raises(ValueError, match="positive"):
        compute_predictive_metrics(forecast, actual_time_ms=1, robust_context_scale_ms=0)
    tied = distribution(8_000, 10_000, 10_000, 13_000, 15_000)
    assert (
        compute_predictive_metrics(
            tied, actual_time_ms=10_000, robust_context_scale_ms=10_000
        ).calibration_residual
        == "0"
    )


def test_low_level_closed_validators_cover_all_invalid_boundaries() -> None:
    opportunity, _, _ = valid_ledger_items()
    for mutation, message in (
        ({"scope": "operational"}, "scope"),
        ({"assessor": "formula"}, "assessor"),
        ({"context": {}}, "context"),
        ({"scope": ScoreScope.CANDIDATE}, "candidate ledger"),
        ({"forecast_digest": "A" * 64}, "digest"),
        ({"result_revision": 0}, "positive"),
        ({"source_sequence": True}, "positive"),
        ({"difficulty": "-1"}, "nonnegative"),
        ({"difficulty": "01"}, "canonical"),
    ):
        with pytest.raises((ValueError, Exception), match=message):
            replace(opportunity, **mutation)
    for factory, message in (
        (lambda: CredibilityPolicy(prior_strength="0"), "positive"),
        (lambda: HandicapConsequenceMetrics(1, "1.1", 0, 1, "0", False), "exceed"),
        (lambda: HandicapConsequenceMetrics(-1, "0", 0, 1, "0", False), "nonnegative"),
        (lambda: replace(opportunity, forecast_digest="short"), "digest"),
    ):
        with pytest.raises(ValueError, match=message):
            factory()
    assert credibility_module._normalize(
        {
            AssessorKind.FORMULA: credibility_module.Decimal("0"),
            AssessorKind.ML: credibility_module.Decimal("0"),
            AssessorKind.LLM_COUNCIL: credibility_module.Decimal("0"),
        }
    ) == dict(credibility_module._equal_weights_decimal())
