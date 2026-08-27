from __future__ import annotations

from decimal import Decimal
from time import perf_counter

import pytest

from strathmark.v3.contracts.forecasts import AssessorKind
from strathmark.v3.domain import credibility as credibility_module
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
    close_live_overlay,
    effective_degraded_weights,
    initial_live_overlay,
    set_live_control,
)
from strathmark.v3.domain.credibility import (
    calibrate_baseline as _calibrate_baseline,
)
from strathmark.v3.domain.credibility import (
    freeze_live_round as _freeze_live_round,
)

CTX = ContextNode("uh", "300_349", "eucalypt", "deep")
CUTOFF = "2026-08-23T12:00:00.000Z"
AUTHORITY_DIGEST = "d" * 64


def calibrate_baseline(ledger, context, policy):
    return _calibrate_baseline(ledger, context, policy, calibration_cutoff_at_utc=CUTOFF)


def freeze_live_round(*args, **kwargs):
    return _freeze_live_round(*args, **kwargs, calibration_cutoff_at_utc=CUTOFF)


def score(
    number: int,
    assessor: AssessorKind,
    loss: str,
    *,
    breach: bool = False,
    result_id: str | None = None,
    context: ContextNode = CTX,
    settled_at_utc: str = CUTOFF,
):
    result_key = result_id or f"result:{number}"
    forecast_digest = f"{number:064x}"
    opportunity = Opportunity.create(
        opportunity_id=f"opportunity:{number}",
        scope=ScoreScope.OPERATIONAL,
        assessor=assessor,
        forecast_digest=forecast_digest,
        result_id=result_key,
        result_revision=1,
        source_sequence=number,
        context=context,
        eligible_at_forecast=True,
        outcome=OpportunityOutcome.SUCCESSFUL,
        difficulty="1",
    )
    consequence = OptimizerConsequenceReceipt.verified(
        forecast_digest=forecast_digest,
        result_revision_digest=f"{number + 100:064x}",
        field_receipt_digest="a" * 64,
        scoring_input_digest="e" * 64,
        optimizer_bundle_digest="b" * 64,
        metrics=HandicapConsequenceMetrics(
            100,
            "0.5" if breach else "0.01",
            0,
            25,
            "0.01",
            False,
        ),
        authority_manifest_digest=AUTHORITY_DIGEST,
    )
    item = PredictiveScore.create(
        score_id=f"score:{number}",
        scope=ScoreScope.OPERATIONAL,
        assessor=assessor,
        forecast_digest=forecast_digest,
        result_id=result_key,
        result_revision=1,
        source_sequence=number,
        context=context,
        evidence_weight="1",
        metrics=PredictiveMetrics(loss, loss, 1, 0, 0, True, 1, "0"),
        consequence=consequence,
        settled_at_utc=settled_at_utc,
    )
    return opportunity, item


def test_zero_evidence_is_exact_equal_thirds() -> None:
    receipt = calibrate_baseline(CredibilityLedger(), CTX, CredibilityPolicy())
    assert receipt.weights == (
        (AssessorKind.FORMULA, "0.33333333333333333333333333333333333333333333333333"),
        (AssessorKind.ML, "0.33333333333333333333333333333333333333333333333333"),
        (
            AssessorKind.LLM_COUNCIL,
            "0.33333333333333333333333333333333333333333333333334",
        ),
    )
    assert all(component.n_eff == "0" for component in receipt.components)


def test_mature_lower_loss_earns_weight_but_caps_and_one_result_bounds_apply() -> None:
    ledger = CredibilityLedger()
    number = 1
    for _ in range(40):
        for assessor, loss in (
            (AssessorKind.FORMULA, "0.1"),
            (AssessorKind.ML, "0.25"),
            (AssessorKind.LLM_COUNCIL, "0.4"),
        ):
            opportunity, item = score(number, assessor, loss)
            ledger = ledger.append_opportunity(opportunity).append_score(item)
            number += 1
    receipt = calibrate_baseline(ledger, CTX, CredibilityPolicy())
    weights = dict(receipt.weights)
    assert weights[AssessorKind.FORMULA] > weights[AssessorKind.ML]
    assert weights[AssessorKind.ML] > weights[AssessorKind.LLM_COUNCIL]
    assert all("0.05" <= value <= "0.8" for value in weights.values())

    sparse = CredibilityLedger()
    opportunity, item = score(999, AssessorKind.FORMULA, "0")
    sparse = sparse.append_opportunity(opportunity).append_score(item)
    sparse_weights = dict(calibrate_baseline(sparse, CTX, CredibilityPolicy()).weights)
    assert abs(float(sparse_weights[AssessorKind.FORMULA]) - 1 / 3) <= 0.05


def test_one_settled_result_limits_every_post_normalization_weight_move_to_point_zero_five() -> (
    None
):
    ledger = CredibilityLedger()
    for number, assessor, loss in (
        (2001, AssessorKind.FORMULA, "0"),
        (2002, AssessorKind.ML, "1"),
        (2003, AssessorKind.LLM_COUNCIL, "2"),
    ):
        opportunity, item = score(number, assessor, loss, result_id="result:single")
        ledger = ledger.append_opportunity(opportunity).append_score(item)
    weights = dict(calibrate_baseline(ledger, CTX, CredibilityPolicy()).weights)
    equal = 1 / 3
    assert all(abs(float(value) - equal) <= 0.05 for value in weights.values())


def test_marginal_deep_history_update_and_consequence_cap_hold_after_normalization() -> None:
    ledger = CredibilityLedger()
    number = 3_000
    for result_number in range(100):
        for assessor in (
            AssessorKind.FORMULA,
            AssessorKind.ML,
            AssessorKind.LLM_COUNCIL,
        ):
            opportunity, item = score(
                number, assessor, "0.2", result_id=f"result:deep-{result_number}"
            )
            ledger = ledger.append_opportunity(opportunity).append_score(item)
            number += 1
    before = dict(calibrate_baseline(ledger, CTX, CredibilityPolicy()).weights)
    for assessor, loss in (
        (AssessorKind.FORMULA, "1000"),
        (AssessorKind.ML, "0.2"),
        (AssessorKind.LLM_COUNCIL, "0.2"),
    ):
        opportunity, item = score(number, assessor, loss, result_id="result:deep-extra")
        ledger = ledger.append_opportunity(opportunity).append_score(item)
        number += 1
    after = dict(calibrate_baseline(ledger, CTX, CredibilityPolicy()).weights)
    assert all(
        abs(Decimal(after[item]) - Decimal(before[item])) <= Decimal("0.05") for item in before
    )

    breached = CredibilityLedger()
    for result_number in range(30):
        for assessor in (
            AssessorKind.FORMULA,
            AssessorKind.ML,
            AssessorKind.LLM_COUNCIL,
        ):
            opportunity, item = score(
                number,
                assessor,
                "0.01" if assessor is AssessorKind.FORMULA else "1",
                breach=assessor is AssessorKind.FORMULA,
                result_id=f"result:breach-{result_number}",
            )
            breached = breached.append_opportunity(opportunity).append_score(item)
            number += 1
    capped = dict(calibrate_baseline(breached, CTX, CredibilityPolicy()).weights)
    assert Decimal(capped[AssessorKind.FORMULA]) <= Decimal("0.45")


def test_baseline_replay_is_streaming_within_the_operator_capacity_budget() -> None:
    ledgers = []
    number = 20_000
    ledger = CredibilityLedger()
    for result_number in range(200):
        for assessor in (
            AssessorKind.FORMULA,
            AssessorKind.ML,
            AssessorKind.LLM_COUNCIL,
        ):
            opportunity, item = score(
                number,
                assessor,
                "0.2",
                result_id=f"result:capacity-{result_number}",
            )
            ledger = ledger.append_opportunity(opportunity).append_score(item)
            number += 1
        if result_number in {49, 199}:
            ledgers.append(ledger)
    elapsed = []
    for candidate in ledgers:
        started = perf_counter()
        calibrate_baseline(candidate, CTX, CredibilityPolicy())
        elapsed.append(perf_counter() - started)
    assert elapsed[1] < 2
    assert elapsed[1] < elapsed[0] * 8


def test_result_batch_index_consumes_each_ledger_stream_once() -> None:
    opportunity, item = score(99_001, AssessorKind.FORMULA, "0.2")

    class Once:
        def __init__(self, value):
            self.value = value
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("ledger stream was rescanned")
            yield self.value

    opportunities = Once(opportunity)
    scores = Once(item)
    ordered, by_opportunity, by_score = credibility_module._index_result_batches(
        opportunities, scores
    )
    assert opportunities.iterations == scores.iterations == 1
    assert ordered == ((opportunity.result_id, opportunity.result_revision),)
    assert by_opportunity[ordered[0]] == [opportunity]
    assert by_score[ordered[0]] == [item]


def test_recent_scores_outweigh_old_scores_at_an_authoritative_cutoff() -> None:
    policy = CredibilityPolicy(recency_half_life_days="10")
    recent_good = CredibilityLedger()
    old_good = CredibilityLedger()
    for batch, settled_at in (
        (0, "2025-08-22T12:00:00.000Z"),
        (1, "2026-08-22T12:00:00.000Z"),
    ):
        for offset, assessor in enumerate(
            (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)
        ):
            neutral = "0.5"
            recent_loss = ("1" if batch == 0 else "0.01") if offset == 0 else neutral
            old_loss = ("0.01" if batch == 0 else "1") if offset == 0 else neutral
            recent = score(
                91_000 + batch * 10 + offset,
                assessor,
                recent_loss,
                result_id=f"result:recent-{batch}",
                settled_at_utc=settled_at,
            )
            old = score(
                92_000 + batch * 10 + offset,
                assessor,
                old_loss,
                result_id=f"result:old-{batch}",
                settled_at_utc=settled_at,
            )
            recent_good = recent_good.append_opportunity(recent[0]).append_score(recent[1])
            old_good = old_good.append_opportunity(old[0]).append_score(old[1])
    recent = dict(calibrate_baseline(recent_good, CTX, policy).weights)
    old = dict(calibrate_baseline(old_good, CTX, policy).weights)
    assert Decimal(recent[AssessorKind.FORMULA]) > Decimal(old[AssessorKind.FORMULA])


def test_ineligible_only_batch_stays_cold_and_infeasible_projection_fails_closed() -> None:
    unavailable = Opportunity.create(
        opportunity_id="opportunity:ineligible-only",
        scope=ScoreScope.OPERATIONAL,
        assessor=AssessorKind.FORMULA,
        forecast_digest="f" * 64,
        result_id="result:ineligible-only",
        result_revision=1,
        source_sequence=1,
        context=CTX,
        eligible_at_forecast=False,
        outcome=OpportunityOutcome.INELIGIBLE,
        difficulty="1",
    )
    receipt = calibrate_baseline(
        CredibilityLedger().append_opportunity(unavailable), CTX, CredibilityPolicy()
    )
    assert (
        receipt.weights == calibrate_baseline(CredibilityLedger(), CTX, CredibilityPolicy()).weights
    )
    equal = dict(credibility_module._equal_weights_decimal())
    with pytest.raises(ValueError, match="feasible simplex"):
        credibility_module._project_bounded_simplex(
            equal,
            equal,
            lower={item: Decimal("0.4") for item in equal},
            upper={item: Decimal("0.5") for item in equal},
        )
    projected = credibility_module._project_bounded_simplex(
        equal,
        equal,
        lower={item: Decimal("0") for item in equal},
        upper={item: Decimal("1") for item in equal},
    )
    assert sum(projected.values()) == 1


def test_hierarchy_uses_siblings_only_through_shared_parents_and_ignores_candidate_scores() -> None:
    sibling = ContextNode("uh", "300_349", "pine", "deep")
    unrelated_event = ContextNode("sb", "300_349", "pine", "deep")
    ledger = CredibilityLedger()
    number = 30_000
    for result_number in range(20):
        for assessor, loss in (
            (AssessorKind.FORMULA, "0.01"),
            (AssessorKind.ML, "1"),
            (AssessorKind.LLM_COUNCIL, "1"),
        ):
            opportunity, item = score(
                number,
                assessor,
                loss,
                result_id=f"result:sibling-{result_number}",
                context=sibling,
            )
            ledger = ledger.append_opportunity(opportunity).append_score(item)
            number += 1
    operational_before_candidate = calibrate_baseline(ledger, CTX, CredibilityPolicy())
    candidate_opportunity, candidate_score = score(
        number,
        AssessorKind.LLM_COUNCIL,
        "0",
        result_id="result:candidate-only",
        context=CTX,
    )
    candidate_opportunity = Opportunity.create(
        **{
            **{
                name: getattr(candidate_opportunity, name)
                for name in credibility_module._OPPORTUNITY_FIELDS
            },
            "opportunity_id": "opportunity:candidate-only",
            "scope": ScoreScope.CANDIDATE,
        }
    )
    candidate_score = PredictiveScore.create(
        score_id="score:candidate-only",
        scope=ScoreScope.CANDIDATE,
        assessor=AssessorKind.LLM_COUNCIL,
        forecast_digest=candidate_opportunity.forecast_digest,
        result_id=candidate_opportunity.result_id,
        result_revision=candidate_opportunity.result_revision,
        source_sequence=candidate_opportunity.source_sequence,
        context=candidate_opportunity.context,
        evidence_weight="1",
        metrics=candidate_score.metrics,
        consequence=candidate_score.consequence,
        settled_at_utc=CUTOFF,
    )
    with_candidate = ledger.append_opportunity(candidate_opportunity).append_score(candidate_score)
    assert (
        calibrate_baseline(with_candidate, CTX, CredibilityPolicy()).weights
        == operational_before_candidate.weights
    )
    shared_parent = dict(operational_before_candidate.weights)
    global_only = dict(calibrate_baseline(ledger, unrelated_event, CredibilityPolicy()).weights)
    assert Decimal(shared_parent[AssessorKind.FORMULA]) > Decimal(global_only[AssessorKind.FORMULA])
    assert Decimal(global_only[AssessorKind.FORMULA]) > Decimal(1) / Decimal(3)


def test_sparse_exact_context_shrinks_to_parent_and_consequence_only_caps_health() -> None:
    ledger = CredibilityLedger()
    opportunity, item = score(1, AssessorKind.FORMULA, "0.01", breach=True)
    ledger = ledger.append_opportunity(opportunity).append_score(item)
    receipt = calibrate_baseline(ledger, CTX, CredibilityPolicy())
    formula = next(c for c in receipt.components if c.assessor is AssessorKind.FORMULA)
    assert formula.predictive_loss == "0.01"
    assert formula.shrunk_loss != formula.predictive_loss
    assert formula.health == "consequence_breach"
    assert formula.effective_cap == "0.45"


def test_live_overlay_freezes_round_controls_and_expires_without_rewriting_baseline() -> None:
    baseline = calibrate_baseline(CredibilityLedger(), CTX, CredibilityPolicy())
    overlay = initial_live_overlay("tournament:one", baseline)
    assert overlay.enabled and not overlay.suspended
    frozen = freeze_live_round(
        overlay,
        round_id="round:two",
        completed_round_id="round:one",
        live_ledger=CredibilityLedger(),
        context=CTX,
        policy=CredibilityPolicy(),
    )
    assert frozen.rounds[-1].weights == baseline.weights
    suspended, event = set_live_control(frozen, action="suspend", reason="judge review")
    assert suspended.suspended and event.reason == "judge review"
    resumed, resume = set_live_control(suspended, action="re_enable", reason="review clear")
    assert resumed.enabled and not resumed.suspended and resume.before_digest != resume.after_digest
    stopped, _ = set_live_control(resumed, action="emergency_stop", reason="integrity alarm")
    assert stopped.emergency_stopped
    reopened, _ = set_live_control(stopped, action="re_enable", reason="signed recovery")
    assert reopened.enabled and not reopened.emergency_stopped
    closed = close_live_overlay(reopened, reason="tournament finalized")
    assert closed.expired and closed.current_weights == baseline.weights


def test_degraded_normalization_is_explicit_and_does_not_change_baseline() -> None:
    baseline = calibrate_baseline(CredibilityLedger(), CTX, CredibilityPolicy())
    degraded = effective_degraded_weights(baseline, (AssessorKind.FORMULA, AssessorKind.ML))
    assert degraded.baseline_weights == baseline.weights
    assert (
        degraded.normalization_denominator == "0.66666666666666666666666666666666666666666666666666"
    )
    assert degraded.missing_mass == "0.33333333333333333333333333333333333333333333333334"
    assert sum(float(value) for _, value in degraded.effective_weights) == 1.0


def test_live_overlay_control_and_round_boundaries_fail_closed() -> None:
    baseline = calibrate_baseline(CredibilityLedger(), CTX, CredibilityPolicy())
    with pytest.raises(Exception):
        initial_live_overlay("field:not-a-tournament", baseline)
    overlay = initial_live_overlay("tournament:boundaries", baseline)
    frozen = freeze_live_round(
        overlay,
        round_id="round:one",
        completed_round_id="round:zero",
        live_ledger=CredibilityLedger(),
        context=CTX,
        policy=CredibilityPolicy(),
    )
    with pytest.raises(ValueError, match="already frozen"):
        freeze_live_round(
            frozen,
            round_id="round:one",
            completed_round_id="round:zero",
            live_ledger=CredibilityLedger(),
            context=CTX,
            policy=CredibilityPolicy(),
        )
    for action in ("suspend", "emergency_stop", "re_enable"):
        changed, event = set_live_control(overlay, action=action, reason=f"reason {action}")
        assert event.action == action
        assert changed.digest == event.after_digest
    with pytest.raises(ValueError, match="reason"):
        set_live_control(overlay, action="suspend", reason="")
    with pytest.raises(ValueError, match="unknown"):
        set_live_control(overlay, action="other", reason="explicit")
    with pytest.raises(ValueError, match="reason"):
        close_live_overlay(overlay, reason="")
    expired = close_live_overlay(overlay, reason="done")
    with pytest.raises(ValueError, match="expired"):
        set_live_control(expired, action="re_enable", reason="no")
    with pytest.raises(ValueError, match="expired"):
        freeze_live_round(
            expired,
            round_id="round:two",
            completed_round_id="round:one",
            live_ledger=CredibilityLedger(),
            context=CTX,
            policy=CredibilityPolicy(),
        )


def test_live_overlay_mature_scores_are_bounded_and_consistency_weighted() -> None:
    baseline = calibrate_baseline(CredibilityLedger(), CTX, CredibilityPolicy())
    live = CredibilityLedger()
    for number, loss in ((2001, "0.01"), (2002, "0.4")):
        opportunity, item = score(number, AssessorKind.FORMULA, loss)
        live = live.append_opportunity(opportunity).append_score(item)
    overlay = freeze_live_round(
        initial_live_overlay("tournament:live", baseline),
        round_id="round:next",
        completed_round_id="round:complete",
        live_ledger=live,
        context=CTX,
        policy=CredibilityPolicy(),
    )
    assert "0" < overlay.rounds[-1].influence <= "0.25"


def test_disabled_live_overlay_uses_baseline_without_learning() -> None:
    baseline = calibrate_baseline(CredibilityLedger(), CTX, CredibilityPolicy())
    suspended, _ = set_live_control(
        initial_live_overlay("tournament:suspended", baseline),
        action="suspend",
        reason="manual hold",
    )
    frozen = freeze_live_round(
        suspended,
        round_id="round:held",
        completed_round_id="round:previous",
        live_ledger=CredibilityLedger(),
        context=CTX,
        policy=CredibilityPolicy(),
    )
    assert frozen.rounds[-1].influence == "0"
    assert frozen.current_weights == baseline.weights
