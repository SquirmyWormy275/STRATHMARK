from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import (
    AssessorKind,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.disagreement import (
    ConsequenceColor,
    ConsequenceComparison,
    CouncilAudit,
    CouncilMemberAudit,
    CouncilMemberStatus,
    CounterfactualCompetitor,
    CounterfactualSheet,
    DisagreementDecision,
    DisagreementPolicy,
    ExpectedTimeOverrideReceipt,
    ExpectedTimeOverrideRequest,
    FieldSheetSnapshot,
    OptimizerVerificationStatus,
    OverrideRecomputationProof,
    OverrideScope,
    ZeroHistoryEstimate,
    ZeroHistoryPolicy,
    classify_disagreement,
    create_override_receipt,
    create_zero_history_estimate,
)

OUTER = (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)


def _council_audit(sheet: CounterfactualSheet) -> CouncilAudit:
    return CouncilAudit.create(
        aggregate_sheet=sheet,
        aggregate_forecast_digest="7" * 64,
        evidence_digest="8" * 64,
        evidence_epoch_id=StableIdentifier("epoch:council-test"),
        members=tuple(
            CouncilMemberAudit(StableIdentifier(f"llm_member:{member}"), status, digest, receipt)
            for member, status, digest, receipt in (
                ("cloud", CouncilMemberStatus.VALID, "1" * 64, "2" * 64),
                ("local_a", CouncilMemberStatus.FAILED, "3" * 64, "4" * 64),
                ("local_b", CouncilMemberStatus.INVALID, "5" * 64, "6" * 64),
            )
        ),
    )


def _competitor(
    name: str,
    median: int,
    mark: int,
    win_probability: str,
    *,
    lower: int | None = None,
    upper: int | None = None,
) -> CounterfactualCompetitor:
    return CounterfactualCompetitor(
        competitor_id=StableIdentifier(f"competitor:{name}"),
        median_ms=median,
        lower_ms=lower if lower is not None else median - 4_000,
        upper_ms=upper if upper is not None else median + 4_000,
        mark=mark,
        win_probability=win_probability,
    )


def _sheet(
    source: AssessorKind | str,
    *,
    alice_median: int = 40_000,
    alice_mark: int = 3,
    alice_probability: str = "0.5",
    alice_lower: int | None = None,
    alice_upper: int | None = None,
    spread: int = 1_000,
) -> CounterfactualSheet:
    return CounterfactualSheet.create(
        source=source,
        competitors=(
            _competitor(
                "alice",
                alice_median,
                alice_mark,
                alice_probability,
                lower=alice_lower,
                upper=alice_upper,
            ),
            _competitor(
                "bob",
                36_000,
                7,
                str(Decimal(1) - Decimal(alice_probability)),
            ),
        ),
        expected_spread_ms=spread,
        joint_draw_digest="a" * 64,
        optimizer_digest="b" * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )


def _policy() -> DisagreementPolicy:
    return DisagreementPolicy(
        version="disagreement:v1",
        green_median_delta_ms=100,
        red_median_delta_ms=1_000,
        green_interval_endpoint_delta_ms=100,
        red_interval_endpoint_delta_ms=1_000,
        green_mark_delta=0,
        red_mark_delta=2,
        green_win_probability_delta="0.01",
        red_win_probability_delta="0.1",
        green_spread_delta_ms=100,
        red_spread_delta_ms=1_000,
        replay_evidence_digest="c" * 64,
        disjoint_verification_digest="d" * 64,
    )


@pytest.mark.parametrize(
    ("change", "green_value", "amber_value", "red_value"),
    [
        ("median", 100, 101, 1_000),
        ("lower", 100, 101, 1_000),
        ("upper", 100, 101, 1_000),
        ("mark", 0, 1, 2),
        ("probability", "0.01", "0.011", "0.1"),
        ("spread", 100, 101, 1_000),
    ],
)
def test_every_consequence_threshold_has_stable_green_amber_red_boundaries(
    change: str, green_value: int | str, amber_value: int | str, red_value: int | str
) -> None:
    pooled = _sheet("pooled")

    def changed(value: int | str) -> CounterfactualSheet:
        kwargs: dict[str, object] = {}
        if change == "median":
            kwargs["alice_median"] = 40_000 + int(value)
        elif change == "lower":
            kwargs["alice_lower"] = 36_000 - int(value)
        elif change == "upper":
            kwargs["alice_upper"] = 44_000 + int(value)
        elif change == "mark":
            kwargs["alice_mark"] = 3 + int(value)
        elif change == "probability":
            kwargs["alice_probability"] = str(Decimal("0.5") + Decimal(str(value)))
        else:
            kwargs["spread"] = 1_000 + int(value)
        return _sheet(AssessorKind.FORMULA, **kwargs)

    def classify(sheet: CounterfactualSheet):
        council = _sheet(AssessorKind.LLM_COUNCIL)
        return classify_disagreement(
            pooled,
            (sheet, _sheet(AssessorKind.ML), council),
            _council_audit(council),
            _policy(),
            available_assessors=OUTER,
        )

    assert classify(changed(green_value)).color is ConsequenceColor.GREEN
    assert classify(changed(green_value)).operational_status is OptimizerVerificationStatus.PENDING
    assert classify(changed(green_value)).manual_review_required
    assert classify(changed(amber_value)).color is ConsequenceColor.AMBER
    expected_red_boundary = (
        ConsequenceColor.AMBER if change in {"median", "lower", "upper"} else ConsequenceColor.RED
    )
    assert classify(changed(red_value)).color is expected_red_boundary


def test_ordering_reversal_is_red_and_all_components_and_dissent_remain_inspectable() -> None:
    pooled = _sheet("pooled")
    formula = _sheet(AssessorKind.FORMULA, alice_median=35_000)
    ml = _sheet(AssessorKind.ML)
    result = classify_disagreement(
        pooled,
        (formula, ml),
        None,
        _policy(),
        available_assessors=(AssessorKind.FORMULA, AssessorKind.ML),
    )
    assert result.color is ConsequenceColor.RED
    assert result.component_sheets == (formula, ml)
    assert result.council_audit is None
    assert any(item.ordering_reversal for item in result.comparisons)
    assert result.policy_digest == _policy().digest
    assert result.policy == _policy()
    assert DisagreementPolicy.from_dict(result.policy.to_dict()) == result.policy
    assert result.decision_digest == canonical_digest(result.content_value())
    assert DisagreementDecision.from_dict(result.to_dict()) == result
    with pytest.raises(ContractError, match="digest"):
        replace(result, decision_digest="0" * 64)
    with pytest.raises(ContractError, match="deterministic replay"):
        replace(
            result,
            comparisons=(
                replace(
                    result.comparisons[0],
                    mark_delta=result.comparisons[0].mark_delta + 1,
                ),
                *result.comparisons[1:],
            ),
        )
    with pytest.raises(ContractError, match="decision digest"):
        replace(result, policy=replace(_policy(), green_mark_delta=1))
    with pytest.raises(ContractError, match="nonnegative"):
        replace(result.comparisons[0], mark_delta=-1)
    encoded = result.to_dict()
    encoded["policy_digest"] = "0" * 64
    with pytest.raises(ContractError, match="policy digest"):
        DisagreementDecision.from_dict(encoded)


def test_incomplete_roster_or_invalid_policy_fails_closed() -> None:
    pooled = _sheet("pooled")
    incomplete = CounterfactualSheet.create(
        source=AssessorKind.FORMULA,
        competitors=(_competitor("alice", 40_000, 3, "1"),),
        expected_spread_ms=0,
        joint_draw_digest="a" * 64,
        optimizer_digest="b" * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    with pytest.raises(ValueError, match="roster"):
        classify_disagreement(
            pooled,
            (incomplete, _sheet(AssessorKind.ML)),
            None,
            _policy(),
            available_assessors=(AssessorKind.FORMULA, AssessorKind.ML),
        )


def test_available_council_requires_exact_three_cross_bound_member_audits() -> None:
    pooled = _sheet("pooled")
    council_sheet = _sheet(AssessorKind.LLM_COUNCIL)
    audit = _council_audit(council_sheet)
    assert CouncilAudit.from_dict(audit.to_dict()) == audit
    assert tuple(item.status for item in audit.members) == (
        CouncilMemberStatus.VALID,
        CouncilMemberStatus.FAILED,
        CouncilMemberStatus.INVALID,
    )
    for members in (audit.members[:2], audit.members[:1], ()):
        with pytest.raises(ContractError, match="three"):
            replace(audit, members=members)
    with pytest.raises(ContractError, match="unique"):
        replace(audit, members=(audit.members[0], audit.members[0], audit.members[2]))
    with pytest.raises(ContractError, match="audit digest"):
        replace(audit, aggregate_sheet_digest="0" * 64)
    with pytest.raises(ContractError, match="audit digest"):
        replace(audit, evidence_digest="0" * 64)
    substituted_sheet = _sheet(AssessorKind.LLM_COUNCIL, spread=2_000)
    with pytest.raises(ContractError, match="three-member"):
        classify_disagreement(
            _sheet("pooled"),
            (
                _sheet(AssessorKind.FORMULA),
                _sheet(AssessorKind.ML),
                substituted_sheet,
            ),
            audit,
            _policy(),
            available_assessors=OUTER,
        )
    with pytest.raises(ContractError, match="threshold"):
        replace(_policy(), red_mark_delta=0)
    degraded_green = classify_disagreement(
        pooled,
        (_sheet(AssessorKind.FORMULA), _sheet(AssessorKind.ML)),
        None,
        _policy(),
        available_assessors=(AssessorKind.FORMULA, AssessorKind.ML),
    )
    assert degraded_green.color is ConsequenceColor.GREEN
    with pytest.raises(ContractError, match="three-member"):
        classify_disagreement(
            pooled,
            (
                _sheet(AssessorKind.FORMULA),
                _sheet(AssessorKind.ML),
                _sheet(AssessorKind.LLM_COUNCIL),
            ),
            None,
            _policy(),
            available_assessors=OUTER,
        )


def _broad_prior() -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        (
            QuantilePoint("0.1", 20_000),
            QuantilePoint("0.5", 40_000),
            QuantilePoint("0.9", 80_000),
        )
    )


def test_zero_history_uses_broad_population_prior_and_forces_red_manual_review() -> None:
    estimate = create_zero_history_estimate(
        competitor_id=StableIdentifier("competitor:newcomer"),
        target_context_digest="1" * 64,
        population_prior=_broad_prior(),
        population_prior_digest="2" * 64,
        policy=ZeroHistoryPolicy("0.1", "0.9", 30_000, "zero-history:v1"),
    )
    assert estimate.distribution == _broad_prior()
    assert estimate.review_color is ConsequenceColor.RED
    assert estimate.manual_acceptance_required
    assert estimate.maximum_honest_uncertainty
    assert type(estimate).from_dict(estimate.to_dict()) == estimate
    with pytest.raises(ContractError, match="red/manual"):
        replace(estimate, manual_acceptance_required=False)
    narrow = PositiveTimeDistribution(
        (
            QuantilePoint("0.1", 39_000),
            QuantilePoint("0.5", 40_000),
            QuantilePoint("0.9", 41_000),
        )
    )
    with pytest.raises(ValueError, match="broad"):
        create_zero_history_estimate(
            StableIdentifier("competitor:newcomer"),
            "1" * 64,
            narrow,
            "2" * 64,
            ZeroHistoryPolicy("0.1", "0.9", 30_000, "zero-history:v1"),
        )


def _snapshot(alice_time: int, alice_mark: int, bob_mark: int, suffix: str) -> FieldSheetSnapshot:
    return FieldSheetSnapshot.create(
        field_id=StableIdentifier("field:final"),
        expected_times_ms=(
            (StableIdentifier("competitor:alice"), alice_time),
            (StableIdentifier("competitor:bob"), 35_000),
        ),
        marks=(
            (StableIdentifier("competitor:alice"), alice_mark),
            (StableIdentifier("competitor:bob"), bob_mark),
        ),
        pool_receipt_digest=("3" if suffix == "before" else "4") * 64,
        optimizer_receipt_digest=("5" if suffix == "before" else "6") * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )


def test_expected_time_override_audit_binds_scope_recomputation_and_whole_field_rebase() -> None:
    before = _snapshot(40_000, 3, 8, "before")
    after = _snapshot(32_000, 6, 3, "after")
    request = ExpectedTimeOverrideRequest.create(
        override_id=StableIdentifier("override:u13-1"),
        competitor_id=StableIdentifier("competitor:alice"),
        target_context_digest="7" * 64,
        expected_raw_time_ms=32_000,
        scope=OverrideScope.REMAINING_EVENT_CONFIGURATION,
        scope_boundary_id=StableIdentifier("event_config:underhand-300"),
        actor="principal:operator",
        reason="verified starting estimate",
        supersedes_override_id=None,
    )
    proof = OverrideRecomputationProof.create(before, after)
    receipt = create_override_receipt(
        request=request,
        before=before,
        after=after,
        proof=proof,
        assessor_outputs_digest="8" * 64,
        consensus_digest="9" * 64,
        evidence_digest="a" * 64,
        evidence_epoch_id=StableIdentifier("epoch:u13"),
    )
    assert receipt.scope is OverrideScope.REMAINING_EVENT_CONFIGURATION
    assert receipt.scope_boundary_id == StableIdentifier("event_config:underhand-300")
    assert receipt.before_time_ms == 40_000 and receipt.after_time_ms == 32_000
    assert receipt.affected_competitors == (
        StableIdentifier("competitor:alice"),
        StableIdentifier("competitor:bob"),
    )
    assert receipt.before_sheet == before and receipt.after_sheet == after
    assert receipt.recomputation_proof == proof
    assert proof.verification_status is OptimizerVerificationStatus.PENDING
    assert not proof.reoptimized_verified
    assert receipt.completion_status is OptimizerVerificationStatus.PENDING
    assert not receipt.is_result_evidence and not receipt.is_training_evidence
    assert receipt.receipt_digest == canonical_digest(receipt.content_value())
    assert ExpectedTimeOverrideRequest.from_dict(request.to_dict()) == request
    assert FieldSheetSnapshot.from_dict(before.to_dict()) == before
    assert OverrideRecomputationProof.from_dict(proof.to_dict()) == proof
    assert ExpectedTimeOverrideReceipt.from_dict(receipt.to_dict()) == receipt
    with pytest.raises(ContractError, match="digest"):
        replace(request, request_digest="0" * 64)
    with pytest.raises(ContractError, match="digest"):
        replace(before, sheet_digest="0" * 64)
    with pytest.raises(ContractError, match="flags"):
        replace(proof, whole_field_recomputed=False)
    with pytest.raises(ContractError, match="flags"):
        replace(receipt, is_training_evidence=True)
    with pytest.raises(ContractError, match="U14"):
        replace(after, optimizer_verification_status=OptimizerVerificationStatus.VERIFIED)
    with pytest.raises(ContractError, match="U14"):
        replace(
            proof,
            verification_status=OptimizerVerificationStatus.VERIFIED,
            reoptimized_verified=True,
        )


def test_override_has_no_default_scope_and_rejects_isolated_mark_or_incomplete_recomputation() -> (
    None
):
    before = _snapshot(40_000, 3, 8, "before")
    after = _snapshot(32_000, 3, 8, "after")
    with pytest.raises((ContractError, TypeError), match="scope"):
        ExpectedTimeOverrideRequest.create(
            StableIdentifier("override:u13-1"),
            StableIdentifier("competitor:alice"),
            "7" * 64,
            32_000,
            None,
            StableIdentifier("field:final"),
            "principal:operator",
            "reason",
            None,
        )
    not_recomputed = FieldSheetSnapshot.create(
        field_id=StableIdentifier("field:final"),
        expected_times_ms=after.expected_times_ms,
        marks=after.marks,
        pool_receipt_digest=before.pool_receipt_digest,
        optimizer_receipt_digest=before.optimizer_receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    with pytest.raises(ValueError, match="re-pooled"):
        OverrideRecomputationProof.create(before, not_recomputed)
    incomplete = FieldSheetSnapshot.create(
        field_id=StableIdentifier("field:final"),
        expected_times_ms=((StableIdentifier("competitor:alice"), 32_000),),
        marks=((StableIdentifier("competitor:alice"), 3),),
        pool_receipt_digest="4" * 64,
        optimizer_receipt_digest="6" * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    with pytest.raises(ValueError, match="whole.*field"):
        create_override_receipt(
            ExpectedTimeOverrideRequest.create(
                StableIdentifier("override:u13-1"),
                StableIdentifier("competitor:alice"),
                "7" * 64,
                32_000,
                OverrideScope.UPCOMING_RACE,
                StableIdentifier("field:final"),
                "principal:operator",
                "reason",
                None,
            ),
            before,
            incomplete,
            OverrideRecomputationProof.create(before, incomplete),
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        )
    wrong_boundary = ExpectedTimeOverrideRequest.create(
        StableIdentifier("override:u13-wrong-field"),
        StableIdentifier("competitor:alice"),
        "7" * 64,
        32_000,
        OverrideScope.UPCOMING_RACE,
        StableIdentifier("field:other"),
        "principal:operator",
        "reason",
        None,
    )
    with pytest.raises(ContractError, match="affected field"):
        create_override_receipt(
            wrong_boundary,
            before,
            after,
            OverrideRecomputationProof.create(before, after),
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        )


def _rejects(factory, *values: object) -> None:
    for value in values:
        with pytest.raises((ContractError, TypeError, ValueError)):
            factory(value)


def test_disagreement_public_contracts_reject_adversarial_construction_and_decoding() -> None:
    member = _council_audit(_sheet(AssessorKind.LLM_COUNCIL)).members[0]
    _rejects(lambda value: replace(member, status=value), "valid", None)
    _rejects(
        CouncilMemberAudit.from_dict,
        {**member.to_dict(), "extra": True},
        {**member.to_dict(), "status": "unknown"},
        {**member.to_dict(), "status": []},
    )

    competitor = _competitor("alice", 40_000, 3, "0.5")
    _rejects(
        lambda value: replace(competitor, lower_ms=value),
        0,
        True,
        "1",
        40_001,
    )
    _rejects(lambda value: replace(competitor, upper_ms=value), 39_999)
    _rejects(lambda value: replace(competitor, mark=value), 2)
    _rejects(
        CounterfactualCompetitor.from_dict,
        {**competitor.to_dict(), "extra": True},
    )

    sheet = _sheet(AssessorKind.FORMULA)
    _rejects(
        lambda value: replace(sheet, competitors=value),
        [],
        (),
        (object(),),
        tuple(reversed(sheet.competitors)),
        (sheet.competitors[0], sheet.competitors[0]),
    )
    _rejects(lambda value: replace(sheet, expected_spread_ms=value), True, "0", -1)
    _rejects(
        lambda value: replace(sheet, optimizer_verification_status=value),
        OptimizerVerificationStatus.VERIFIED,
        "pending_u14_verifier",
    )
    _rejects(lambda value: replace(sheet, sheet_digest=value), "0" * 64)
    _rejects(
        lambda value: replace(
            sheet,
            competitors=(
                replace(sheet.competitors[0], win_probability="0.6"),
                sheet.competitors[1],
            ),
        ),
        None,
    )
    sheet_encoded = sheet.to_dict()
    _rejects(
        CounterfactualSheet.from_dict,
        {**sheet_encoded, "schema_version": "wrong"},
        {**sheet_encoded, "source": "unknown"},
        {**sheet_encoded, "source": []},
        {**sheet_encoded, "competitors": ()},
        {**sheet_encoded, "optimizer_verification_status": "unknown"},
        {**sheet_encoded, "optimizer_verification_status": []},
    )

    council_sheet = _sheet(AssessorKind.LLM_COUNCIL)
    audit = _council_audit(council_sheet)
    _rejects(lambda value: replace(audit, members=value), list(audit.members))
    _rejects(lambda value: replace(audit, members=value), tuple(reversed(audit.members)))
    _rejects(lambda value: replace(audit, audit_digest=value), "0" * 64)
    _rejects(
        lambda value: CouncilAudit.create(
            aggregate_sheet=value,
            aggregate_forecast_digest="7" * 64,
            evidence_digest="8" * 64,
            evidence_epoch_id=StableIdentifier("epoch:council-test"),
            members=audit.members,
        ),
        object(),
        _sheet(AssessorKind.FORMULA),
    )
    audit_encoded = audit.to_dict()
    _rejects(
        CouncilAudit.from_dict,
        {**audit_encoded, "schema_version": "wrong"},
        {**audit_encoded, "members": ()},
    )

    policy = _policy()
    _rejects(lambda value: replace(policy, version=value), "wrong")
    for field, values in (
        ("green_median_delta_ms", (True, "1", -1)),
        ("red_median_delta_ms", (True, "1", 0)),
    ):
        _rejects(lambda value, field=field: replace(policy, **{field: value}), *values)
    _rejects(lambda value: replace(policy, red_mark_delta=value), 1)
    _rejects(lambda value: replace(policy, red_win_probability_delta=value), "0.01")
    _rejects(lambda value: replace(policy, replay_evidence_digest=value), "x")
    _rejects(DisagreementPolicy.from_dict, {**policy.to_dict(), "extra": True})

    comparison = classify_disagreement(
        _sheet("pooled"),
        (_sheet(AssessorKind.FORMULA), _sheet(AssessorKind.ML)),
        None,
        policy,
        available_assessors=(AssessorKind.FORMULA, AssessorKind.ML),
    ).comparisons[0]
    _rejects(lambda value: replace(comparison, source=value), "", None)
    _rejects(lambda value: replace(comparison, ordering_reversal=value), 1, "false")
    _rejects(lambda value: replace(comparison, color=value), "green")
    comparison_encoded = comparison.to_dict()
    _rejects(
        ConsequenceComparison.from_dict,
        {**comparison_encoded, "extra": True},
        {**comparison_encoded, "color": "unknown"},
        {**comparison_encoded, "color": []},
    )

    decision = classify_disagreement(
        _sheet("pooled"),
        (_sheet(AssessorKind.FORMULA), _sheet(AssessorKind.ML)),
        None,
        policy,
        available_assessors=(AssessorKind.FORMULA, AssessorKind.ML),
    )
    _rejects(lambda value: replace(decision, color=value), "green")
    _rejects(
        lambda value: replace(decision, operational_status=value),
        OptimizerVerificationStatus.VERIFIED,
    )
    _rejects(lambda value: replace(decision, manual_review_required=value), False)
    _rejects(lambda value: replace(decision, pooled_sheet=value), object(), sheet)
    _rejects(
        lambda value: replace(decision, component_sheets=value),
        list(decision.component_sheets),
    )
    _rejects(lambda value: replace(decision, comparisons=value), list(decision.comparisons))
    _rejects(
        lambda value: replace(decision, component_sheets=value),
        (object(), decision.component_sheets[1]),
    )
    _rejects(
        lambda value: replace(decision, comparisons=value),
        (object(), decision.comparisons[1]),
    )
    _rejects(lambda value: replace(decision, policy=value), object())
    _rejects(
        lambda value: replace(decision, comparisons=value),
        tuple(reversed(decision.comparisons)),
    )
    _rejects(
        lambda value: replace(decision, assessor_availability=value),
        tuple(reversed(decision.assessor_availability)),
    )
    _rejects(lambda value: replace(decision, color=value), ConsequenceColor.AMBER)
    _rejects(lambda value: replace(decision, council_audit=value), audit)
    council_decision = classify_disagreement(
        _sheet("pooled"),
        (_sheet(AssessorKind.FORMULA), _sheet(AssessorKind.ML), council_sheet),
        audit,
        policy,
        available_assessors=OUTER,
    )
    _rejects(lambda value: replace(council_decision, council_audit=value), None, object())
    decision_encoded = decision.to_dict()
    _rejects(
        DisagreementDecision.from_dict,
        {**decision_encoded, "schema_version": "wrong"},
        {**decision_encoded, "color": "unknown"},
        {**decision_encoded, "operational_status": "unknown"},
        {**decision_encoded, "assessor_availability": [["unknown", True]]},
        {**decision_encoded, "policy": []},
    )


def test_disagreement_classification_rejects_incomplete_or_forged_inputs() -> None:
    pooled = _sheet("pooled")
    formula = _sheet(AssessorKind.FORMULA)
    ml = _sheet(AssessorKind.ML)
    policy = _policy()

    def classify(p, c, a, available):
        return classify_disagreement(p, c, a, policy, available_assessors=available)

    _rejects(lambda value: classify(value, (formula, ml), None, OUTER[:2]), object())
    _rejects(
        lambda value: classify(pooled, value, None, OUTER[:2]),
        [],
        (),
        (object(),),
    )
    _rejects(lambda value: classify(value, (formula, ml), None, OUTER[:2]), formula)
    _rejects(
        lambda value: classify(pooled, (formula, ml), None, value),
        [AssessorKind.FORMULA, AssessorKind.ML],
        (AssessorKind.ML, AssessorKind.FORMULA),
        (AssessorKind.FORMULA,),
    )
    _rejects(
        lambda value: classify(pooled, (formula, ml), value, OUTER[:2]),
        _council_audit(_sheet(AssessorKind.LLM_COUNCIL)),
    )
    _rejects(lambda value: classify(pooled, value, None, OUTER[:2]), (ml, formula))
    _rejects(
        lambda value: classify(pooled, (formula, ml), None, value),
        (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_MEMBER),
    )


def test_zero_history_contract_edges_fail_closed() -> None:
    policy = ZeroHistoryPolicy("0.1", "0.9", 30_000, "zero-history:v1")
    _rejects(lambda value: replace(policy, interval_lower_probability=value), "0", "0.5")
    _rejects(lambda value: replace(policy, interval_upper_probability=value), "0.5", "1")
    _rejects(lambda value: replace(policy, minimum_interval_width_ms=value), True, 0, "1")
    _rejects(lambda value: replace(policy, version=value), "wrong")
    estimate = create_zero_history_estimate(
        StableIdentifier("competitor:newcomer"),
        "1" * 64,
        _broad_prior(),
        "2" * 64,
        policy,
    )
    _rejects(lambda value: replace(estimate, distribution=value), object())
    _rejects(lambda value: replace(estimate, review_color=value), ConsequenceColor.AMBER)
    _rejects(lambda value: replace(estimate, maximum_honest_uncertainty=value), False)
    _rejects(lambda value: replace(estimate, estimate_digest=value), "0" * 64)
    estimate_encoded = estimate.to_dict()
    _rejects(
        ZeroHistoryEstimate.from_dict,
        {**estimate_encoded, "schema_version": "wrong"},
        {**estimate_encoded, "review_color": "unknown"},
        {**estimate_encoded, "review_color": []},
    )
    _rejects(
        lambda value: create_zero_history_estimate(
            StableIdentifier("competitor:newcomer"), "1" * 64, value, "2" * 64, policy
        ),
        object(),
    )
    _rejects(
        lambda value: create_zero_history_estimate(
            StableIdentifier("competitor:newcomer"),
            "1" * 64,
            _broad_prior(),
            "2" * 64,
            value,
        ),
        object(),
    )


def test_override_contract_edges_fail_closed() -> None:
    before = _snapshot(40_000, 3, 8, "before")
    after = _snapshot(32_000, 6, 3, "after")
    request = ExpectedTimeOverrideRequest.create(
        StableIdentifier("override:u13-edge"),
        StableIdentifier("competitor:alice"),
        "7" * 64,
        32_000,
        OverrideScope.REMAINING_EVENT_CONFIGURATION,
        StableIdentifier("event_config:underhand-300"),
        "principal:operator",
        "reason",
        None,
    )
    request_encoded = request.to_dict()
    _rejects(lambda value: replace(request, scope=value), "remaining_event_configuration")
    _rejects(lambda value: replace(request, actor=value), "", " actor ", None)
    _rejects(lambda value: replace(request, reason=value), "", " reason ", None)
    _rejects(
        lambda value: replace(request, supersedes_override_id=value),
        request.override_id,
        StableIdentifier("competitor:wrong"),
    )
    _rejects(
        ExpectedTimeOverrideRequest.from_dict,
        {**request_encoded, "schema_version": "wrong"},
        {**request_encoded, "scope": "unknown"},
        {**request_encoded, "scope": []},
    )
    _rejects(
        lambda value: ExpectedTimeOverrideRequest.create(
            StableIdentifier("override:u13-edge-2"),
            StableIdentifier("competitor:alice"),
            "7" * 64,
            32_000,
            OverrideScope.REMAINING_EVENT_CONFIGURATION,
            StableIdentifier("event_config:underhand-300"),
            value,
            "reason",
            None,
        ),
        "",
        None,
    )
    _rejects(
        lambda value: ExpectedTimeOverrideRequest.create(
            StableIdentifier("override:u13-edge-2"),
            StableIdentifier("competitor:alice"),
            "7" * 64,
            32_000,
            OverrideScope.REMAINING_EVENT_CONFIGURATION,
            StableIdentifier("event_config:underhand-300"),
            "actor",
            value,
            None,
        ),
        "",
        None,
    )
    _rejects(
        lambda value: ExpectedTimeOverrideRequest.create(
            StableIdentifier("override:u13-edge-2"),
            StableIdentifier("competitor:alice"),
            "7" * 64,
            32_000,
            OverrideScope.REMAINING_EVENT_CONFIGURATION,
            StableIdentifier("event_config:underhand-300"),
            "actor",
            "reason",
            value,
        ),
        StableIdentifier("override:u13-edge-2"),
        StableIdentifier("competitor:wrong"),
    )
    superseding = ExpectedTimeOverrideRequest.create(
        StableIdentifier("override:u13-edge-3"),
        StableIdentifier("competitor:alice"),
        "7" * 64,
        32_000,
        OverrideScope.REMAINING_EVENT_CONFIGURATION,
        StableIdentifier("event_config:underhand-300"),
        "actor",
        "reason",
        StableIdentifier("override:prior"),
    )
    assert ExpectedTimeOverrideRequest.from_dict(superseding.to_dict()) == superseding

    _rejects(
        lambda value: replace(before, expected_times_ms=value),
        tuple(reversed(before.expected_times_ms)),
    )
    _rejects(lambda value: replace(before, marks=value), tuple(reversed(before.marks)))
    _rejects(
        lambda value: replace(before, marks=value),
        ((StableIdentifier("competitor:alice"), 3),),
    )
    _rejects(
        lambda value: replace(before, marks=value),
        (
            (StableIdentifier("competitor:alice"), 4),
            (StableIdentifier("competitor:bob"), 8),
        ),
    )
    _rejects(
        lambda value: replace(before, optimizer_verification_status=value),
        OptimizerVerificationStatus.VERIFIED,
    )
    snapshot_encoded = before.to_dict()
    _rejects(
        FieldSheetSnapshot.from_dict,
        {**snapshot_encoded, "schema_version": "wrong"},
        {**snapshot_encoded, "expected_times_ms": [None]},
        {**snapshot_encoded, "marks": [None]},
        {**snapshot_encoded, "optimizer_verification_status": "unknown"},
        {**snapshot_encoded, "optimizer_verification_status": []},
    )
    _rejects(
        lambda value: FieldSheetSnapshot.create(
            field_id=StableIdentifier("field:edge"),
            expected_times_ms=before.expected_times_ms,
            marks=value,
            pool_receipt_digest="3" * 64,
            optimizer_receipt_digest="5" * 64,
            optimizer_verification_status=OptimizerVerificationStatus.PENDING,
        ),
        ((StableIdentifier("competitor:alice"), 3),),
        (
            (StableIdentifier("competitor:alice"), 4),
            (StableIdentifier("competitor:bob"), 8),
        ),
    )
    _rejects(
        lambda value: FieldSheetSnapshot.create(
            field_id=StableIdentifier("field:edge"),
            expected_times_ms=value,
            marks=before.marks,
            pool_receipt_digest="3" * 64,
            optimizer_receipt_digest="5" * 64,
            optimizer_verification_status=OptimizerVerificationStatus.PENDING,
        ),
        [],
        (),
        (
            (StableIdentifier("competitor:alice"), 40_000),
            (StableIdentifier("competitor:alice"), 39_000),
        ),
    )

    proof = OverrideRecomputationProof.create(before, after)
    _rejects(
        lambda value: replace(proof, before_sheet_digest=value),
        proof.after_sheet_digest,
    )
    _rejects(
        lambda value: replace(proof, before_pool_receipt_digest=value),
        proof.after_pool_receipt_digest,
    )
    _rejects(
        lambda value: replace(proof, before_optimizer_receipt_digest=value),
        proof.after_optimizer_receipt_digest,
    )
    _rejects(lambda value: replace(proof, rebased_to_mark_3=value), False)
    _rejects(lambda value: replace(proof, reoptimized_verified=value), True)
    _rejects(lambda value: replace(proof, proof_digest=value), "0" * 64)
    proof_encoded = proof.to_dict()
    _rejects(
        OverrideRecomputationProof.from_dict,
        {**proof_encoded, "schema_version": "wrong"},
        {**proof_encoded, "verification_status": "unknown"},
        {**proof_encoded, "verification_status": []},
    )
    _rejects(lambda value: OverrideRecomputationProof.create(value, after), object())
    same_optimizer = FieldSheetSnapshot.create(
        field_id=after.field_id,
        expected_times_ms=after.expected_times_ms,
        marks=after.marks,
        pool_receipt_digest=after.pool_receipt_digest,
        optimizer_receipt_digest=before.optimizer_receipt_digest,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    _rejects(lambda value: OverrideRecomputationProof.create(before, value), same_optimizer)

    receipt = create_override_receipt(
        request,
        before,
        after,
        proof,
        "8" * 64,
        "9" * 64,
        "a" * 64,
        StableIdentifier("epoch:u13"),
    )
    _rejects(lambda value: replace(receipt, scope=value), "remaining_event_configuration")
    _rejects(lambda value: replace(receipt, recomputation_proof=value), object())
    other_before = FieldSheetSnapshot.create(
        field_id=StableIdentifier("field:other"),
        expected_times_ms=before.expected_times_ms,
        marks=before.marks,
        pool_receipt_digest="3" * 64,
        optimizer_receipt_digest="5" * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    other_after = FieldSheetSnapshot.create(
        field_id=StableIdentifier("field:other"),
        expected_times_ms=after.expected_times_ms,
        marks=after.marks,
        pool_receipt_digest="4" * 64,
        optimizer_receipt_digest="6" * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    other_proof = OverrideRecomputationProof.create(other_before, other_after)
    _rejects(lambda value: replace(receipt, recomputation_proof=value), other_proof)
    _rejects(lambda value: replace(receipt, request_digest=value), "0" * 64)
    unchanged_request = ExpectedTimeOverrideRequest.create(
        receipt.override_id,
        receipt.competitor_id,
        receipt.target_context_digest,
        receipt.before_time_ms,
        receipt.scope,
        receipt.scope_boundary_id,
        receipt.actor,
        receipt.reason,
        receipt.supersedes_override_id,
    )
    _rejects(
        lambda value: replace(
            receipt,
            request_digest=unchanged_request.request_digest,
            after_time_ms=value,
        ),
        receipt.before_time_ms,
    )
    _rejects(lambda value: replace(receipt, before_time_ms=value), 1)
    _rejects(
        lambda value: replace(receipt, affected_competitors=value),
        receipt.affected_competitors[:1],
    )
    _rejects(lambda value: replace(receipt, permanently_fixed=value), True)
    _rejects(
        lambda value: replace(receipt, completion_status=value),
        OptimizerVerificationStatus.VERIFIED,
    )
    _rejects(lambda value: replace(receipt, receipt_digest=value), "0" * 64)
    receipt_encoded = receipt.to_dict()
    _rejects(
        ExpectedTimeOverrideReceipt.from_dict,
        {**receipt_encoded, "schema_version": "wrong"},
        {**receipt_encoded, "scope": "unknown"},
        {**receipt_encoded, "completion_status": "unknown"},
    )
    _rejects(
        lambda value: create_override_receipt(
            value,
            before,
            after,
            proof,
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        ),
        object(),
    )

    one_person = FieldSheetSnapshot.create(
        field_id=before.field_id,
        expected_times_ms=((StableIdentifier("competitor:alice"), 32_000),),
        marks=((StableIdentifier("competitor:alice"), 3),),
        pool_receipt_digest="4" * 64,
        optimizer_receipt_digest="6" * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    _rejects(
        lambda value: create_override_receipt(
            request,
            before,
            value,
            proof,
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        ),
        one_person,
    )
    absent_request = ExpectedTimeOverrideRequest.create(
        StableIdentifier("override:absent"),
        StableIdentifier("competitor:charlie"),
        "7" * 64,
        32_000,
        OverrideScope.REMAINING_EVENT_CONFIGURATION,
        StableIdentifier("event_config:underhand-300"),
        "actor",
        "reason",
        None,
    )
    _rejects(
        lambda value: create_override_receipt(
            value,
            before,
            after,
            proof,
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        ),
        absent_request,
    )
    _rejects(
        lambda value: create_override_receipt(
            request,
            before,
            after,
            value,
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        ),
        other_proof,
    )
    wrong_time_request = ExpectedTimeOverrideRequest.create(
        StableIdentifier("override:wrong-time"),
        StableIdentifier("competitor:alice"),
        "7" * 64,
        31_000,
        OverrideScope.REMAINING_EVENT_CONFIGURATION,
        StableIdentifier("event_config:underhand-300"),
        "actor",
        "reason",
        None,
    )
    _rejects(
        lambda value: create_override_receipt(
            value,
            before,
            after,
            proof,
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        ),
        wrong_time_request,
    )
    unchanged_after = FieldSheetSnapshot.create(
        field_id=before.field_id,
        expected_times_ms=before.expected_times_ms,
        marks=before.marks,
        pool_receipt_digest="4" * 64,
        optimizer_receipt_digest="6" * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    unchanged_proof = OverrideRecomputationProof.create(before, unchanged_after)
    unchanged = ExpectedTimeOverrideRequest.create(
        StableIdentifier("override:unchanged"),
        StableIdentifier("competitor:alice"),
        "7" * 64,
        40_000,
        OverrideScope.REMAINING_EVENT_CONFIGURATION,
        StableIdentifier("event_config:underhand-300"),
        "actor",
        "reason",
        None,
    )
    _rejects(
        lambda value: create_override_receipt(
            value,
            before,
            unchanged_after,
            unchanged_proof,
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        ),
        unchanged,
    )
    other_changed = FieldSheetSnapshot.create(
        field_id=before.field_id,
        expected_times_ms=(
            (StableIdentifier("competitor:alice"), 32_000),
            (StableIdentifier("competitor:bob"), 34_000),
        ),
        marks=after.marks,
        pool_receipt_digest="4" * 64,
        optimizer_receipt_digest="6" * 64,
        optimizer_verification_status=OptimizerVerificationStatus.PENDING,
    )
    other_changed_proof = OverrideRecomputationProof.create(before, other_changed)
    _rejects(
        lambda value: create_override_receipt(
            request,
            before,
            other_changed,
            value,
            "8" * 64,
            "9" * 64,
            "a" * 64,
            StableIdentifier("epoch:u13"),
        ),
        other_changed_proof,
    )


def test_shared_primitive_validators_reject_noncanonical_values() -> None:
    _rejects(lambda value: _competitor("edge", 40_000, 3, value), "1.0", "-0.1", "1.1")
    _rejects(lambda value: _competitor("edge", value, 3, "1"), True, 0, "40000")
    _rejects(lambda value: _sheet(AssessorKind.LLM_MEMBER), None)
    _rejects(lambda value: _sheet(value), "unknown")
    _rejects(
        lambda value: replace(_sheet("pooled"), joint_draw_digest=value),
        "A" * 64,
        "0" * 63,
        None,
    )


def test_override_receipt_rejects_resigned_noncanonical_metadata_and_non_bool_flags() -> None:
    before = _snapshot(40_000, 3, 8, "before")
    after = _snapshot(32_000, 6, 3, "after")
    request = ExpectedTimeOverrideRequest.create(
        StableIdentifier("override:u13-forgery"),
        StableIdentifier("competitor:alice"),
        "7" * 64,
        32_000,
        OverrideScope.REMAINING_EVENT_CONFIGURATION,
        StableIdentifier("event_config:underhand-300"),
        "principal:operator",
        "reason",
        None,
    )
    receipt = create_override_receipt(
        request,
        before,
        after,
        OverrideRecomputationProof.create(before, after),
        "8" * 64,
        "9" * 64,
        "a" * 64,
        StableIdentifier("epoch:u13"),
    )

    def resigned(**changes: object) -> ExpectedTimeOverrideReceipt:
        request_content = request.content_value()
        for key in ("actor", "reason"):
            if key in changes:
                request_content[key] = changes[key]
        request_digest = canonical_digest(request_content)
        content = receipt.content_value()
        content.update(changes)
        content["request_digest"] = request_digest
        return replace(
            receipt,
            **changes,
            request_digest=request_digest,
            receipt_digest=canonical_digest(content),
        )

    for changes in (
        {"actor": 1},
        {"actor": ""},
        {"actor": " actor "},
        {"reason": 1},
        {"reason": ""},
        {"reason": " reason "},
        {"is_result_evidence": 0},
        {"is_training_evidence": ""},
        {"becomes_starting_estimate": 1},
        {"permanently_fixed": 0},
    ):
        with pytest.raises(ContractError):
            resigned(**changes)
