from __future__ import annotations

from strathmark.v3.contracts.forecasts import AssessorKind
from strathmark.v3.domain.credibility import (
    ContextNode,
    CredibilityLedger,
    CredibilityPolicy,
    HandicapConsequenceMetrics,
    Opportunity,
    OpportunityOutcome,
    OptimizerConsequenceReceipt,
    PredictiveMetrics,
    PredictiveScore,
    ScoreScope,
)
from strathmark.v3.domain.credibility import (
    calibrate_baseline as _calibrate_baseline,
)

CTX = ContextNode("sb", "300_349", "pine", "medium")
CUTOFF = "2026-08-23T12:00:00.000Z"


def calibrate_baseline(ledger, context, policy):
    return _calibrate_baseline(ledger, context, policy, calibration_cutoff_at_utc=CUTOFF)


def append_success(
    ledger: CredibilityLedger,
    number: int,
    assessor: AssessorKind,
    loss: str,
    difficulty: str,
) -> CredibilityLedger:
    digest = f"{number:064x}"
    opportunity = Opportunity.create(
        opportunity_id=f"opportunity:{assessor.value}:{number}",
        scope=ScoreScope.OPERATIONAL,
        assessor=assessor,
        forecast_digest=digest,
        result_id=f"result:{number}",
        result_revision=1,
        source_sequence=number,
        context=CTX,
        eligible_at_forecast=True,
        outcome=OpportunityOutcome.SUCCESSFUL,
        difficulty=difficulty,
    )
    consequence = OptimizerConsequenceReceipt.verified(
        forecast_digest=digest,
        result_revision_digest=f"{number + 1000:064x}",
        field_receipt_digest="a" * 64,
        scoring_input_digest="e" * 64,
        optimizer_bundle_digest="b" * 64,
        metrics=HandicapConsequenceMetrics(10, "0", 0, 0, "0", False),
        authority_manifest_digest="f" * 64,
    )
    score = PredictiveScore.create(
        score_id=f"score:{assessor.value}:{number}",
        scope=ScoreScope.OPERATIONAL,
        assessor=assessor,
        forecast_digest=digest,
        result_id=f"result:{number}",
        result_revision=1,
        source_sequence=number,
        context=CTX,
        evidence_weight="1",
        metrics=PredictiveMetrics(loss, loss, 1, 0, 0, True, 1, "0"),
        consequence=consequence,
        settled_at_utc=CUTOFF,
    )
    return ledger.append_opportunity(opportunity).append_score(score)


def test_easy_case_withholder_never_beats_honest_always_predicting_assessor() -> None:
    ledger = CredibilityLedger()
    for number in range(1, 31):
        difficulty = "0.25" if number <= 15 else "2"
        ledger = append_success(ledger, number, AssessorKind.FORMULA, "0.2", difficulty)
        if number <= 15:
            ledger = append_success(ledger, number + 100, AssessorKind.ML, "0.05", difficulty)
        else:
            ledger = ledger.append_opportunity(
                Opportunity.create(
                    opportunity_id=f"opportunity:ml:{number}",
                    scope=ScoreScope.OPERATIONAL,
                    assessor=AssessorKind.ML,
                    forecast_digest=f"{number + 100:064x}",
                    result_id=f"result:{number}",
                    result_revision=1,
                    source_sequence=number,
                    context=CTX,
                    eligible_at_forecast=True,
                    outcome=OpportunityOutcome.PRINCIPLED_ABSTENTION,
                    difficulty=difficulty,
                )
            )
    weights = dict(calibrate_baseline(ledger, CTX, CredibilityPolicy()).weights)
    assert weights[AssessorKind.ML] <= weights[AssessorKind.FORMULA]


def test_invalid_transport_runtime_and_deadline_cannot_be_relabelled_or_rewarded() -> None:
    for outcome in (
        OpportunityOutcome.SCHEMA_INVALID,
        OpportunityOutcome.TRANSPORT_FAILURE,
        OpportunityOutcome.RUNTIME_FAILURE,
        OpportunityOutcome.DEADLINE_MISS,
    ):
        ledger = CredibilityLedger().append_opportunity(
            Opportunity.create(
                opportunity_id=f"opportunity:{outcome.value}",
                scope=ScoreScope.OPERATIONAL,
                assessor=AssessorKind.ML,
                forecast_digest="a" * 64,
                result_id="result:hard",
                result_revision=1,
                source_sequence=1,
                context=CTX,
                eligible_at_forecast=True,
                outcome=outcome,
                difficulty="2",
            )
        )
        receipt = calibrate_baseline(ledger, CTX, CredibilityPolicy())
        component = next(item for item in receipt.components if item.assessor is AssessorKind.ML)
        assert component.coverage_rate == "0"
        assert component.effective_cap == "0.05"


def test_adversarial_ninety_percent_easy_only_withholding_cannot_gain_promotion() -> None:
    ledger = CredibilityLedger()
    for number in range(1, 101):
        difficulty = "0.25" if number <= 90 else "4"
        ledger = append_success(ledger, number, AssessorKind.FORMULA, "0.2", difficulty)
        ledger = append_success(ledger, number, AssessorKind.LLM_COUNCIL, "0.2", difficulty)
        if number <= 90:
            ledger = append_success(ledger, number, AssessorKind.ML, "0.01", difficulty)
        else:
            ledger = ledger.append_opportunity(
                Opportunity.create(
                    opportunity_id=f"opportunity:ml-withheld:{number}",
                    scope=ScoreScope.OPERATIONAL,
                    assessor=AssessorKind.ML,
                    forecast_digest=f"{number + 10_000:064x}",
                    result_id=f"result:{number}",
                    result_revision=1,
                    source_sequence=number,
                    context=CTX,
                    eligible_at_forecast=True,
                    outcome=OpportunityOutcome.PRINCIPLED_ABSTENTION,
                    difficulty=difficulty,
                )
            )
    receipt = calibrate_baseline(ledger, CTX, CredibilityPolicy())
    weights = dict(receipt.weights)
    ml = next(item for item in receipt.components if item.assessor is AssessorKind.ML)
    assert ml.health == "coverage_below_minimum"
    assert weights[AssessorKind.ML] < weights[AssessorKind.FORMULA]
    assert weights[AssessorKind.ML] < weights[AssessorKind.LLM_COUNCIL]
