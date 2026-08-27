from __future__ import annotations

from itertools import permutations

import pytest

from strathmark.v3.contracts.forecasts import AssessorKind
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
)
from strathmark.v3.domain.credibility import (
    calibrate_baseline as _calibrate_baseline,
)

CONTEXT = ContextNode("uh", "300_349", "eucalypt", "deep")
CUTOFF = "2026-08-23T12:00:00.000Z"


def calibrate_baseline(ledger, context, policy):
    return _calibrate_baseline(ledger, context, policy, calibration_cutoff_at_utc=CUTOFF)


def opportunity(assessor: AssessorKind, revision: int, sequence: int) -> Opportunity:
    return Opportunity.create(
        opportunity_id=f"opportunity:{assessor.value}-{revision}",
        scope=ScoreScope.OPERATIONAL,
        assessor=assessor,
        forecast_digest=("a" if assessor is AssessorKind.FORMULA else "b") * 64,
        result_id="result:one",
        result_revision=revision,
        source_sequence=sequence,
        context=CONTEXT,
        eligible_at_forecast=True,
        outcome=OpportunityOutcome.SUCCESSFUL,
        difficulty="1",
    )


def score(item: Opportunity) -> PredictiveScore:
    consequence = OptimizerConsequenceReceipt.verified(
        forecast_digest=item.forecast_digest,
        result_revision_digest=f"{item.result_revision:064x}",
        field_receipt_digest="c" * 64,
        scoring_input_digest="d" * 64,
        optimizer_bundle_digest="e" * 64,
        metrics=HandicapConsequenceMetrics(1, "0", 0, 1, "0", False),
        authority_manifest_digest="f" * 64,
    )
    return PredictiveScore.create(
        score_id=f"score:{item.assessor.value}-{item.result_revision}",
        scope=item.scope,
        assessor=item.assessor,
        forecast_digest=item.forecast_digest,
        result_id=item.result_id,
        result_revision=item.result_revision,
        source_sequence=item.source_sequence,
        context=item.context,
        evidence_weight="1",
        metrics=PredictiveMetrics("1", "0.1", 1, 0, 0, True, 2, "0"),
        consequence=consequence,
        settled_at_utc=CUTOFF,
    )


@pytest.mark.parametrize("order", tuple(permutations((AssessorKind.FORMULA, AssessorKind.ML))))
def test_append_order_does_not_change_projection(
    order: tuple[AssessorKind, ...],
) -> None:
    ledger = CredibilityLedger()
    for assessor in order:
        item = opportunity(assessor, 1, 10)
        ledger = ledger.append_opportunity(item).append_score(score(item))
    assert (
        calibrate_baseline(ledger, CONTEXT, CredibilityPolicy()).weights
        == calibrate_baseline(_clean_revision_one(), CONTEXT, CredibilityPolicy()).weights
    )


def test_correction_reversal_replays_to_clean_active_revision() -> None:
    old = _clean_revision_one()
    ledger = old
    for item in (*old.active_opportunities, *old.active_scores):
        target_kind = "opportunity" if isinstance(item, Opportunity) else "score"
        target_id = item.opportunity_id if isinstance(item, Opportunity) else item.score_id
        ledger = ledger.append_reversal(
            LedgerReversal.create(
                reversal_id=f"reversal:{target_kind}-{item.assessor.value}",
                target_kind=target_kind,
                target_id=target_id,
                original_result_revision=1,
                replacement_result_revision=2,
                source_sequence=20,
            )
        )
    clean = CredibilityLedger()
    for assessor in (AssessorKind.FORMULA, AssessorKind.ML):
        item = opportunity(assessor, 2, 20)
        clean = clean.append_opportunity(item).append_score(score(item))
        ledger = ledger.append_opportunity(item).append_score(score(item))
    assert ledger.current_projection_digest == clean.current_projection_digest


def test_uniqueness_is_assessor_result_revision_not_forecast_digest() -> None:
    first = opportunity(AssessorKind.FORMULA, 1, 10)
    duplicate = Opportunity.create(
        opportunity_id="opportunity:formula-duplicate",
        scope=first.scope,
        assessor=first.assessor,
        forecast_digest="f" * 64,
        result_id=first.result_id,
        result_revision=first.result_revision,
        source_sequence=first.source_sequence,
        context=first.context,
        eligible_at_forecast=True,
        outcome=OpportunityOutcome.SUCCESSFUL,
        difficulty="1",
    )
    with pytest.raises(ValueError, match="exactly one"):
        CredibilityLedger().append_opportunity(first).append_opportunity(duplicate)


def _clean_revision_one() -> CredibilityLedger:
    ledger = CredibilityLedger()
    for assessor in (AssessorKind.FORMULA, AssessorKind.ML):
        item = opportunity(assessor, 1, 10)
        ledger = ledger.append_opportunity(item).append_score(score(item))
    return ledger
