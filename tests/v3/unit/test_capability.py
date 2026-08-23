from __future__ import annotations

from dataclasses import replace

import pytest

import strathmark.v3.domain.capability as capability_module
from strathmark.v3.application.commands import EventIntent, validate_command_event_intents
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.commands import CommandEnvelope, CommandKind, InlinePayload
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.events import AggregateKind, EventKind
from strathmark.v3.contracts.forecasts import (
    AssessorKind,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import IdempotencyKey, StableIdentifier
from strathmark.v3.contracts.statuses import (
    AggregateLifecycle,
    LifecycleAggregateKind,
    LifecycleStatus,
)
from strathmark.v3.domain.capability import (
    BOCPD_HAZARD,
    BOCPD_RUN_LENGTH_CAP,
    CAPABILITY_OPERATOR_VERSION,
    CapabilityCapacityEnvelope,
    CapabilityEvidence,
    CapabilityPrior,
    CapabilityState,
    CapabilityTransition,
    FastCapabilityRegime,
    HistoricalImportBinding,
    NumericAnomalyPattern,
    RebaseCapacityDecision,
    RunLengthHypothesis,
    _persistence,
    apply_capability_operator,
    evaluate_rebase_capacity,
    replay_capability,
    retain_promotion_scores,
)
from strathmark.v3.domain.evidence import AdmissionReason, EvidenceSource
from strathmark.v3.domain.state_machines import initial_event, transition


def _evidence(
    raw_time_ms: int | None,
    index: int,
    *,
    result_key: str | None = None,
    revision: int = 1,
    supersedes: int | None = None,
    eligible: bool = True,
) -> CapabilityEvidence:
    return CapabilityEvidence(
        result_key=StableIdentifier(result_key or f"result:r{index}"),
        result_revision=revision,
        supersedes_revision=supersedes,
        competitor_id=StableIdentifier("competitor:alice"),
        context_digest="a" * 64,
        source_global_sequence=index,
        observed_at_utc=f"2026-01-01T00:00:{(index - 1) % 60:02d}.000Z",
        raw_time_ms=raw_time_ms,
        source=EvidenceSource.LIVE_ISSUED_RACE,
        numeric_eligible=eligible,
        admission_reason=(
            AdmissionReason.ELIGIBLE_COMPLETION if eligible else AdmissionReason.STATUS_INELIGIBLE
        ),
        observation_digest=canonical_digest(
            {"index": index, "raw_time_ms": raw_time_ms, "revision": revision}
        ),
        authority_digest="b" * 64,
        prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
        evidence_log_variance="0.0025",
        conversion_log_variance="0",
        effective_weight="1",
        historical_binding=None,
    )


def _distribution(median: int) -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        (
            QuantilePoint("0.1", median - 4_000),
            QuantilePoint("0.5", median),
            QuantilePoint("0.9", median + 4_000),
        )
    )


def test_current_form_is_symmetric_and_capability_protects_speed() -> None:
    baseline = replay_capability((_evidence(40_000, 1),))
    faster = replay_capability((_evidence(40_000, 1), _evidence(30_000, 2)))
    slower = replay_capability((_evidence(40_000, 1), _evidence(53_333, 2)))
    assert isinstance(baseline, CapabilityState)
    assert faster is not None and slower is not None
    assert (
        abs(
            float(faster.last_transition.state_update_innovation)
            + float(slower.last_transition.state_update_innovation)
        )
        < 0.001
    )
    assert faster.demonstrated_capability.median_ms < baseline.demonstrated_capability.median_ms


def test_frozen_bocpd_posterior_golden_hazard_and_cap() -> None:
    state = replay_capability(
        tuple(_evidence(40_000 + (index % 3) * 250, index) for index in range(1, 71))
    )
    assert state is not None
    assert BOCPD_HAZARD == "0.05"
    assert BOCPD_RUN_LENGTH_CAP == 64
    assert len(state.run_length_hypotheses) == 65
    assert state.last_transition.change_point_probability == "0.0159173905535527"
    assert state.current_form.median_ms == 40244


def test_rapid_improvement_matters_immediately_but_one_extreme_slow_heat_does_not_erase() -> None:
    improved = replay_capability((_evidence(40_000, 1), _evidence(22_000, 2)))
    extreme_slow = replay_capability(
        (_evidence(40_000, 1), _evidence(22_000, 2), _evidence(80_000, 3))
    )
    assert improved is not None and extreme_slow is not None
    assert improved.last_transition.faster_candidate_opened
    assert improved.demonstrated_capability.median_ms < 40_000
    assert extreme_slow.demonstrated_capability.median_ms < extreme_slow.current_form.median_ms
    assert (
        extreme_slow.demonstrated_capability.median_ms - improved.demonstrated_capability.median_ms
        < extreme_slow.current_form.median_ms - improved.current_form.median_ms
    )
    assert extreme_slow.current_form.median_ms > improved.current_form.median_ms
    assert extreme_slow.last_transition.change_point_probability != "0"
    assert extreme_slow.last_transition.influence != "0"


def test_original_extreme_likelihood_is_retained_while_only_state_innovation_is_clamped() -> None:
    evidence = (_evidence(40_000, 1), _evidence(1_000_000, 2))
    state = replay_capability(evidence)
    inverse = replay_capability((_evidence(40_000, 1), _evidence(1_600, 2)))
    assert state is not None and inverse is not None
    transition = state.last_transition
    assert evidence[-1].raw_time_ms == 1_000_000
    assert float(transition.original_standardized_innovation) > 4
    assert transition.state_update_innovation == "4"
    assert float(transition.evidence_log_likelihood) < 0
    assert inverse.last_transition.state_update_innovation == "-4"


def test_faster_candidate_opens_by_each_frozen_threshold_and_narrows_with_support() -> None:
    posterior_threshold = replay_capability((_evidence(40_000, 1), _evidence(22_000, 2)))
    assert posterior_threshold is not None
    assert float(posterior_threshold.last_transition.faster_candidate_probability) >= 0.90
    assert not posterior_threshold.last_transition.three_sd_triggered

    stable = tuple(_evidence(40_000, index) for index in range(1, 21))
    low_weight_extreme = replace(_evidence(10_000, 21), effective_weight="0.000001")
    three_sd = replay_capability((*stable, low_weight_extreme))
    assert three_sd is not None
    assert three_sd.last_transition.three_sd_triggered
    assert three_sd.last_transition.faster_candidate_opened
    assert float(three_sd.last_transition.faster_candidate_probability) < 0.90

    supported = replay_capability(
        (_evidence(40_000, 1), _evidence(22_000, 2), _evidence(22_100, 3), _evidence(21_900, 4))
    )
    assert supported is not None
    one_width = (
        posterior_threshold.demonstrated_capability.quantiles[-1].time_ms
        - posterior_threshold.demonstrated_capability.quantiles[0].time_ms
    )
    supported_width = (
        supported.demonstrated_capability.quantiles[-1].time_ms
        - supported.demonstrated_capability.quantiles[0].time_ms
    )
    assert supported_width < one_width


def test_demonstrated_capability_persistence_uses_exact_age_and_supported_decline_half_lives() -> (
    None
):
    regime = FastCapabilityRegime(
        "3.5",
        "2",
        "3.5",
        "0.1",
        "6",
        "0",
        "2024-01-01T00:00:00.000Z",
        (("result:fast", "f" * 64),),
    )
    original = _persistence(regime, "2024-01-01T00:00:00.000Z")
    age_half = _persistence(regime, "2025-12-31T00:00:00.000Z")
    decline_half = _persistence(replace(regime, n_supported_slower="4"), "2024-01-01T00:00:00.000Z")
    assert original == pytest.approx(0.65)
    assert age_half == pytest.approx(original / 2)
    assert decline_half == pytest.approx(original / 2)


def test_repeated_supported_decline_eventually_moves_demonstrated_capability() -> None:
    rows = tuple(
        _evidence(value, index)
        for index, value in enumerate((30_000, 42_000, 43_000, 44_000, 45_000), 1)
    )
    state = replay_capability(rows)
    assert state is not None
    assert state.demonstrated_capability.median_ms > 34_000
    assert state.direction_run >= 4
    prior_slower = 0.0
    for index in range(1, len(rows) + 1):
        partial = replay_capability(rows[:index])
        assert partial is not None
        supported_slower = float(partial.fast_regime.n_supported_slower)
        if supported_slower > prior_slower:
            assert float(partial.last_transition.supported_slower_probability) >= 0.80
        prior_slower = supported_slower
    recovered = replay_capability((*rows, _evidence(29_000, 6)))
    assert recovered is not None
    assert recovered.fast_regime.n_supported_slower == "0"
    assert float(recovered.fast_regime.n_fast) > float(state.fast_regime.n_fast)
    assert recovered.fast_regime.lineage[-1][0] == "result:r6"


def test_evidence_and_conversion_uncertainty_enter_as_the_same_variance_sum() -> None:
    evidence_uncertainty = replace(
        _evidence(40_000, 1), evidence_log_variance="0.1", conversion_log_variance="0"
    )
    conversion_uncertainty = replace(
        evidence_uncertainty, evidence_log_variance="0", conversion_log_variance="0.1"
    )
    evidence_state = replay_capability((evidence_uncertainty,))
    conversion_state = replay_capability((conversion_uncertainty,))
    assert evidence_state is not None and conversion_state is not None
    assert evidence_uncertainty.observation_variance == conversion_uncertainty.observation_variance
    assert evidence_state.current_form == conversion_state.current_form
    assert evidence_state.run_length_hypotheses == conversion_state.run_length_hypotheses


def test_ineligible_evidence_is_preserved_but_does_not_manufacture_state() -> None:
    void = _evidence(None, 1, eligible=False)
    assert replay_capability((void,)) is None
    with pytest.raises(ContractError, match="numeric eligibility"):
        replace(void, numeric_eligible=True)


def test_operator_is_identical_for_formula_ml_and_llm_and_never_mutates_original() -> None:
    state = replay_capability((_evidence(40_000, 1), _evidence(30_000, 2)))
    assert state is not None
    original = _distribution(44_000)
    before = original.to_dict()
    adjusted = [
        apply_capability_operator(kind, original, state)
        for kind in (
            AssessorKind.FORMULA,
            AssessorKind.ML,
            AssessorKind.LLM_COUNCIL,
        )
    ]
    assert original.to_dict() == before
    assert {item.adjusted_distribution.digest for item in adjusted} == {
        adjusted[0].adjusted_distribution.digest
    }
    assert all(item.original_distribution_digest == original.digest for item in adjusted)
    assert all(item.operator_version == adjusted[0].operator_version for item in adjusted)


def test_promotion_scores_retain_original_and_adjusted_values() -> None:
    state = replay_capability((_evidence(40_000, 1),))
    assert state is not None
    adjustment = apply_capability_operator(AssessorKind.FORMULA, _distribution(45_000), state)
    score = retain_promotion_scores(adjustment, original_score="0.72", adjusted_score="0.61")
    assert score.original_forecast_digest == adjustment.original_distribution_digest
    assert score.adjusted_forecast_digest == adjustment.adjusted_distribution.digest
    assert score.original_score == "0.72"
    assert score.adjusted_score == "0.61"


def test_capacity_boundary_is_closed_and_evidence_preserving() -> None:
    envelope = CapabilityCapacityEnvelope()
    admitted = evaluate_rebase_capacity(
        envelope, lineage_rows=256, invalidated_work=128, mandatory_reactions=512
    )
    overflow = evaluate_rebase_capacity(
        envelope, lineage_rows=257, invalidated_work=128, mandatory_reactions=512
    )
    assert admitted.admitted and admitted.next_round_barrier_open
    assert not overflow.admitted and not overflow.next_round_barrier_open
    assert overflow.evidence_preserved
    assert overflow.reason == "lineage_capacity_exceeded"
    assert not evaluate_rebase_capacity(
        envelope, lineage_rows=1, invalidated_work=129, mandatory_reactions=1
    ).admitted
    assert not evaluate_rebase_capacity(
        envelope, lineage_rows=1, invalidated_work=1, mandatory_reactions=513
    ).admitted


@pytest.mark.parametrize(
    "change",
    [
        {"context_digest": "short"},
        {"source_global_sequence": 0},
        {"raw_time_ms": 0},
        {"source": "live_issued_race"},
        {"numeric_eligible": "yes"},
        {"admission_reason": AdmissionReason.UNISSUED},
    ],
)
def test_capability_evidence_rejects_unsupported_or_untyped_input(
    change: dict[str, object],
) -> None:
    with pytest.raises(ContractError):
        replace(_evidence(40_000, 1), **change)


def test_capability_evidence_revision_and_round_trip_rejection_matrix() -> None:
    base = _evidence(40_000, 1)
    revision = _evidence(39_000, 2, result_key="result:r1", revision=2, supersedes=1)
    assert CapabilityEvidence.from_dict(base.to_dict()) == base
    assert CapabilityEvidence.from_dict(revision.to_dict()) == revision
    invalid_changes = (
        {"supersedes_revision": 1},
        {"result_revision": 2, "supersedes_revision": None},
        {"admission_reason": "eligible_completion"},
        {"raw_time_ms": 1, "numeric_eligible": False},
        {"source": EvidenceSource.HISTORICAL_IMPORT},
    )
    for change in invalid_changes:
        with pytest.raises(ContractError):
            replace(base, **change)
    value = base.to_dict()
    for key, replacement in (
        ("schema_version", "other"),
        ("source", "other"),
        ("semantic_digest", "f" * 64),
    ):
        changed = {**value, key: replacement}
        with pytest.raises(ContractError):
            CapabilityEvidence.from_dict(changed)


def test_state_transition_and_nested_contract_rejection_matrix() -> None:
    state = replay_capability((_evidence(40_000, 1), _evidence(40_000, 2)))
    assert state is not None
    assert CapabilityState.from_dict(state.to_dict()) == state
    anomaly = state.last_transition.anomaly
    assert NumericAnomalyPattern.from_dict(anomaly.to_dict()) == anomaly
    assert CapabilityTransition.from_dict(state.last_transition.to_dict()) == state.last_transition
    with pytest.raises(ContractError):
        NumericAnomalyPattern.from_dict({})
    with pytest.raises(ContractError):
        replace(anomaly, absolute_standardized_residual=object())
    with pytest.raises(ContractError):
        replace(anomaly, alternation_ratio="2")
    with pytest.raises(ContractError):
        replace(state.last_transition, anomaly="pattern")
    with pytest.raises(ContractError):
        CapabilityTransition.from_dict({})

    state_changes = (
        {"current_form": "distribution"},
        {"state_revision": state.state_revision + 1},
        {"last_direction": 2},
        {"lineage": ()},
        {"last_transition": "transition"},
        {"schema_version": "other"},
        {"state_digest": "f" * 64},
    )
    for change in state_changes:
        with pytest.raises(ContractError):
            replace(state, **change)
    value = state.to_dict()
    for key, replacement in (
        ("operator_version", "other"),
        ("lineage", "bad"),
        ("current_form", "bad"),
    ):
        with pytest.raises(ContractError):
            CapabilityState.from_dict({**value, key: replacement})


def test_replay_and_operator_closed_contract_rejections() -> None:
    base = _evidence(40_000, 1)
    with pytest.raises(ContractError):
        replay_capability([base])  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        replay_capability((base, "bad"))  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        replay_capability((base, replace(base, competitor_id=StableIdentifier("competitor:b"))))
    with pytest.raises(ContractError):
        replay_capability((base, replace(base, raw_time_ms=41_000)))
    with pytest.raises(ContractError):
        replay_capability(
            (
                base,
                _evidence(39_000, 3, result_key="result:r1", revision=3, supersedes=2),
            )
        )
    state = replay_capability((base,))
    assert state is not None
    interior = PositiveTimeDistribution(
        (
            QuantilePoint("0.25", 36_000),
            QuantilePoint("0.5", 40_000),
            QuantilePoint("0.75", 44_000),
        )
    )
    assert apply_capability_operator(AssessorKind.FORMULA, interior, state).adjusted_distribution
    for assessor in (AssessorKind.LLM_MEMBER, "formula"):
        with pytest.raises(ContractError):
            apply_capability_operator(assessor, interior, state)  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        apply_capability_operator(AssessorKind.FORMULA, "forecast", state)  # type: ignore[arg-type]


def test_adjustment_score_and_capacity_contract_rejections() -> None:
    state = replay_capability((_evidence(40_000, 1),))
    assert state is not None
    adjustment = apply_capability_operator(AssessorKind.FORMULA, _distribution(40_000), state)
    adjustment_changes = (
        {"assessor": "formula"},
        {"operator_version": "other"},
        {"adjusted_distribution": "bad"},
        {"adjustment_digest": "f" * 64},
    )
    for change in adjustment_changes:
        with pytest.raises(ContractError):
            replace(adjustment, **change)
    with pytest.raises(ContractError):
        retain_promotion_scores("adjustment", original_score="1", adjusted_score="1")  # type: ignore[arg-type]
    score = retain_promotion_scores(adjustment, original_score="1", adjusted_score="1")
    with pytest.raises(ContractError):
        replace(score, assessor="formula")
    with pytest.raises(ContractError):
        replace(score, original_score="-1")
    with pytest.raises(ContractError):
        CapabilityCapacityEnvelope(0, 1, 1)
    with pytest.raises(ContractError):
        RebaseCapacityDecision("yes", True, True, "within_capacity", 1, 1, 1, "a" * 64)
    with pytest.raises(ContractError):
        RebaseCapacityDecision(True, True, True, "other", 1, 1, 1, "a" * 64)
    with pytest.raises(ContractError):
        RebaseCapacityDecision(True, True, True, "within_capacity", -1, 1, 1, "a" * 64)
    with pytest.raises(ContractError):
        evaluate_rebase_capacity(
            "capacity", lineage_rows=1, invalidated_work=1, mandatory_reactions=1
        )  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        evaluate_rebase_capacity(
            CapabilityCapacityEnvelope(),
            lineage_rows=-1,
            invalidated_work=1,
            mandatory_reactions=1,
        )
    assert adjustment.operator_version == CAPABILITY_OPERATOR_VERSION


def test_bocpd_support_contracts_and_defensive_numeric_branches_are_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12")
    assert CapabilityPrior.from_dict(prior.to_dict()) == prior
    with pytest.raises(ContractError):
        replace(prior, schema_version="wrong")
    with pytest.raises(ContractError):
        CapabilityPrior.from_dict({})
    with pytest.raises(ContractError):
        capability_module._fs(float("nan"))

    binding = HistoricalImportBinding(
        "v2import:" + "1" * 64,
        "2" * 64,
        "2026-01-01T00:00:00.000Z",
        "4" * 64,
        "3" * 64,
    )
    assert HistoricalImportBinding.from_dict(binding.to_dict()) == binding
    with pytest.raises(ContractError):
        HistoricalImportBinding(
            "wrong",
            "2" * 64,
            binding.source_cutoff,
            binding.cutover_manifest_digest,
            "3" * 64,
        )
    with pytest.raises(ContractError):
        HistoricalImportBinding.from_dict({})

    base = _evidence(40_000, 1)
    with pytest.raises(ContractError):
        replace(base, prior="prior")
    with pytest.raises(ContractError):
        replace(
            base,
            source=EvidenceSource.HISTORICAL_IMPORT,
            admission_reason=AdmissionReason.HISTORICAL_CUTOVER,
        )
    with pytest.raises(ContractError):
        replace(base, historical_binding=binding)
    with pytest.raises(ContractError):
        CapabilityEvidence.from_dict({**base.to_dict(), "prior": "bad"})

    hypothesis = RunLengthHypothesis(0, "1", "3.5", "1", "3", "0.1")
    assert RunLengthHypothesis.from_dict(hypothesis.to_dict()) == hypothesis
    with pytest.raises(ContractError):
        replace(hypothesis, run_length=65)
    with pytest.raises(ContractError):
        RunLengthHypothesis.from_dict({})

    regime = FastCapabilityRegime(
        "3.5",
        "2",
        "3.5",
        "0.1",
        "1",
        "0",
        "2026-01-01T00:00:00.000Z",
        (("result:fast", "f" * 64),),
    )
    assert FastCapabilityRegime.from_dict(regime.to_dict()) == regime
    with pytest.raises(ContractError):
        replace(regime, lineage=())
    with pytest.raises(ContractError):
        FastCapabilityRegime.from_dict({})

    state = replay_capability((base,))
    assert state is not None
    with pytest.raises(ContractError):
        replace(state.last_transition, three_sd_triggered="no")
    with pytest.raises(ContractError):
        replace(state, run_length_hypotheses=())
    with pytest.raises(ContractError):
        replace(
            state,
            run_length_hypotheses=(replace(state.run_length_hypotheses[0], probability="0.5"),),
        )
    assert capability_module._ibeta(1, 1, 0) == 0
    assert capability_module._beta_fraction(1, 1, float("nan")) > 0
    monkeypatch.setattr(capability_module, "abs", lambda _value: 1, raising=False)
    assert capability_module._beta_fraction(1, 1, 0.5) > 0
    with pytest.raises(ContractError):
        CapabilityCapacityEnvelope.from_dict({})


@pytest.mark.parametrize(
    ("command_kind", "event_kind"),
    (
        (CommandKind.RECORD_CAPABILITY_UPDATE, EventKind.CAPABILITY_UPDATED),
        (CommandKind.REBASE_CAPABILITY_STATE, EventKind.CAPABILITY_STATE_REBASED),
    ),
)
def test_capability_command_catalog_and_competitor_lifecycle_are_closed(
    command_kind: CommandKind, event_kind: EventKind
) -> None:
    aggregate_id = StableIdentifier("competitor:alice")
    command = CommandEnvelope(
        kind=command_kind,
        command_id=IdempotencyKey(f"command:{command_kind.value}"),
        target_aggregate=aggregate_id,
        expected_versions=((str(aggregate_id), 0),),
        actor_id=StableIdentifier("actor:system"),
        payload=InlinePayload.from_value({}),
    )
    validate_command_event_intents(
        command,
        (EventIntent(AggregateKind.COMPETITOR, aggregate_id, event_kind),),
    )

    assert initial_event(AggregateKind.COMPETITOR) is EventKind.CAPABILITY_UPDATED
    current = transition(AggregateKind.COMPETITOR, None, EventKind.CAPABILITY_UPDATED)
    assert current is LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT
    assert (
        transition(AggregateKind.COMPETITOR, current, event_kind)
        is LifecycleStatus.COMPETITOR_CAPABILITY_CURRENT
    )
    lifecycle = AggregateLifecycle(LifecycleAggregateKind.COMPETITOR, current)
    assert lifecycle.transition_to(current) == lifecycle
