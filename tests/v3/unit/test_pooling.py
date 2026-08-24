from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest

from strathmark.v3.contracts.canonical import canonical_decimal_string, canonical_digest
from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import (
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    PositiveTimeDistribution,
    QuantilePoint,
    SamplingSpec,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.capability import (
    CapabilityEvidence,
    CapabilityPrior,
    replay_capability,
)
from strathmark.v3.domain.credibility import ContextNode, WeightReceipt
from strathmark.v3.domain.evidence import AdmissionReason, EvidenceSource
from strathmark.v3.domain.joint_dependence import (
    DependencePolicy,
    FieldCompetitorForecast,
    bind_field_dependence,
    generate_joint_draws,
    train_dependence_artifact,
)
from strathmark.v3.domain.pooling import (
    AvailabilityState,
    LinearPoolComponent,
    LinearPooledDistribution,
    PoolComponentReceipt,
    PoolMode,
    PoolReceipt,
    WeightAuthorityBinding,
    WeightAuthorityStatus,
)
from strathmark.v3.domain.pooling import (
    pool_forecasts as _domain_pool_forecasts,
)

OUTER = (AssessorKind.FORMULA, AssessorKind.ML, AssessorKind.LLM_COUNCIL)


def _distribution(median_ms: int, width_ms: int = 0) -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        (
            QuantilePoint("0.1", median_ms - width_ms),
            QuantilePoint("0.5", median_ms),
            QuantilePoint("0.9", median_ms + width_ms),
        )
    )


def _forecast(
    assessor: AssessorKind,
    median_ms: int,
    *,
    state: ForecastState = ForecastState.COMMITTED,
) -> AssessorForecast:
    distribution = (
        _distribution(median_ms) if state is ForecastState.COMMITTED else None
    )
    return AssessorForecast.create(
        forecast_id=StableIdentifier(f"forecast:{assessor.value}-u13"),
        assessor=assessor,
        state=state,
        evidence_digest="a" * 64,
        distribution=distribution,
        support=EvidenceSupport(4, "4", 4, "history:u13", 1),
        warnings=(),
        artifacts=(),
        abstention_code=(
            None if state is ForecastState.COMMITTED else "runtime_unavailable"
        ),
    )


def _capability_state():
    evidence = CapabilityEvidence(
        result_key=StableIdentifier("result:u13-capability"),
        result_revision=1,
        supersedes_revision=None,
        competitor_id=StableIdentifier("competitor:alice"),
        context_digest="b" * 64,
        source_global_sequence=1,
        observed_at_utc="2026-01-01T00:00:00.000Z",
        raw_time_ms=40_000,
        source=EvidenceSource.LIVE_ISSUED_RACE,
        numeric_eligible=True,
        admission_reason=AdmissionReason.ELIGIBLE_COMPLETION,
        observation_digest=canonical_digest({"u13": "capability"}),
        authority_digest="c" * 64,
        prior=CapabilityPrior.from_median_seconds("40", calibrated_beta="0.12"),
        evidence_log_variance="0.0025",
        conversion_log_variance="0",
        effective_weight="1",
        historical_binding=None,
    )
    state = replay_capability((evidence,))
    assert state is not None
    return state


def _weights(
    values: tuple[str, str, str] = (
        "0.3333333333333333333333333333333333",
        "0.3333333333333333333333333333333333",
        "0.3333333333333333333333333333333334",
    ),
):
    weights = tuple(zip(OUTER, values))
    return WeightReceipt(
        context=ContextNode("event", "underhand-300"),
        weights=weights,
        components=(),
        calibration_cutoff_at_utc="2026-01-01T00:00:00.000Z",
        policy_digest="d" * 64,
        receipt_digest=canonical_digest(
            [(kind.value, value) for kind, value in weights]
        ),
    )


def _weight_authority(receipt: WeightReceipt) -> WeightAuthorityBinding:
    return WeightAuthorityBinding.pending(
        receipt,
        ledger_projection_digest="e" * 64,
        tournament_event_sequence=1,
        source_global_sequence=10,
    )


def pool_forecasts(forecasts, baseline, capability_state, sampling, **kwargs):
    authority = (
        _weight_authority(baseline) if isinstance(baseline, WeightReceipt) else object()
    )
    return _domain_pool_forecasts(
        forecasts,
        baseline,
        capability_state,
        sampling,
        weight_authority=authority,
        **kwargs,
    )


def test_three_way_linear_pool_preserves_component_modes_and_audit_facts() -> None:
    result = pool_forecasts(
        tuple(
            _forecast(kind, median)
            for kind, median in zip(OUTER, (20_000, 40_000, 60_000))
        ),
        _weights(("0.25", "0.5", "0.25")),
        _capability_state(),
        SamplingSpec(seed=20260823, draw_count=2_000),
    )

    assert result.mode is PoolMode.NORMAL
    assert result.distribution is not None and result.samples is not None
    assert result.receipt.available_count == 3
    assert result.receipt.normalization_denominator == "1"
    assert result.receipt.missing_mass == "0"
    assert tuple(row.assessor for row in result.receipt.components) == OUTER
    assert all(
        row.availability is AvailabilityState.VALID for row in result.receipt.components
    )
    assert all(
        row.original_distribution is not None for row in result.receipt.components
    )
    assert all(
        row.adjusted_distribution is not None for row in result.receipt.components
    )
    assert all(
        row.capability_adjustment_digest is not None
        for row in result.receipt.components
    )
    assert result.receipt.pooled_samples_digest == result.samples.samples_digest
    assert result.receipt.pooled_distribution == result.distribution
    assert result.receipt.seed == 20260823
    assert result.receipt.draw_count == 2_000
    assert (
        result.receipt.common_random_map_digest
        == result.samples.common_random_map_digest
    )
    values = set(result.samples.samples_ms)
    assert len(values) >= 3
    assert min(values) < result.distribution.median_ms < max(values)
    assert result == pool_forecasts(
        tuple(
            reversed(
                tuple(
                    _forecast(kind, median)
                    for kind, median in zip(OUTER, (20_000, 40_000, 60_000))
                )
            )
        ),
        _weights(("0.25", "0.5", "0.25")),
        _capability_state(),
        SamplingSpec(seed=20260823, draw_count=2_000),
    )


def test_two_way_pool_preserves_more_than_96_digit_authoritative_weights() -> None:
    first = "0.1" + ("0" * 98) + "1"
    with localcontext() as context:
        context.prec = 256
        third = canonical_decimal_string(Decimal(1) - Decimal(first) - Decimal("0.2"))
    baseline = _weights((first, "0.2", third))
    forecasts = (
        _forecast(AssessorKind.FORMULA, 20_000),
        _forecast(AssessorKind.ML, 40_000),
        _forecast(
            AssessorKind.LLM_COUNCIL,
            60_000,
            state=ForecastState.ABSTAINED,
        ),
    )

    result = pool_forecasts(
        forecasts,
        baseline,
        _capability_state(),
        SamplingSpec(seed=20260824, draw_count=2_000),
    )

    assert result.mode is PoolMode.DEGRADED_TWO
    with localcontext() as context:
        context.prec = 256
        expected_denominator = canonical_decimal_string(Decimal(first) + Decimal("0.2"))
    assert result.receipt.normalization_denominator == expected_denominator
    assert result.receipt.missing_mass == third


def test_exact_three_way_weights_never_round_a_positive_component_below_zero() -> None:
    first = (
        "0.912454333798364736144220996277191966852353581110597537807495251434926103"
        "920291626497162418227840519941037828543250313060700344"
    )
    second = (
        "0.087545666201635263855779003722808033147646418889402462192504748565073896"
        "079708373502837581772159480058962171456749686939199656"
    )
    third = "0." + ("0" * 120) + "1"
    baseline = _weights((first, second, third))

    result = pool_forecasts(
        tuple(
            _forecast(kind, median)
            for kind, median in zip(OUTER, (30_000, 40_000, 50_000), strict=True)
        ),
        baseline,
        _capability_state(),
        SamplingSpec(seed=20260824, draw_count=16),
    )

    assert result.receipt.effective_weights == baseline.weights
    assert all(
        Decimal(value) > 0 for _assessor, value in result.receipt.effective_weights
    )
    assert sum(
        (Decimal(value) for _assessor, value in result.receipt.effective_weights),
        Decimal(0),
    ) == Decimal(1)


def test_two_way_pool_renormalizes_only_effective_weights_and_keeps_missing_mass() -> (
    None
):
    result = pool_forecasts(
        (_forecast(AssessorKind.FORMULA, 30_000), _forecast(AssessorKind.ML, 50_000)),
        _weights(("0.2", "0.3", "0.5")),
        _capability_state(),
        SamplingSpec(seed=7, draw_count=128),
    )

    assert result.mode is PoolMode.DEGRADED_TWO
    assert result.receipt.algorithm == "weighted-linear-opinion-pool-v1"
    assert result.receipt.baseline_weights == _weights(("0.2", "0.3", "0.5")).weights
    assert result.receipt.effective_weights == (
        (AssessorKind.FORMULA, "0.4"),
        (AssessorKind.ML, "0.6"),
    )
    assert result.receipt.normalization_denominator == "0.5"
    assert result.receipt.missing_mass == "0.5"
    council = next(
        row
        for row in result.receipt.components
        if row.assessor is AssessorKind.LLM_COUNCIL
    )
    assert council.availability is AvailabilityState.MISSING
    assert council.effective_weight == "0"


def test_one_way_requires_deliberate_acceptance_and_never_masquerades_as_pool() -> None:
    forecast = _forecast(AssessorKind.ML, 38_000)
    blocked = pool_forecasts(
        (forecast,),
        _weights(),
        _capability_state(),
        SamplingSpec(seed=9, draw_count=32),
    )
    assert blocked.mode is PoolMode.MANUAL_REQUIRED
    assert blocked.distribution is None and blocked.samples is None
    assert blocked.receipt.pooled_samples_digest is None
    assert blocked.receipt.algorithm == "no-pool-manual-construction-v1"

    accepted = pool_forecasts(
        (forecast,),
        _weights(),
        _capability_state(),
        SamplingSpec(seed=9, draw_count=32),
        accept_single_survivor=True,
    )
    survivor = next(
        row
        for row in accepted.receipt.components
        if row.availability is AvailabilityState.VALID
    )
    assert accepted.mode is PoolMode.MANUAL_SINGLE
    assert accepted.receipt.is_ensemble is False
    assert accepted.receipt.algorithm == "exact-survivor-manual-degraded-v1"
    assert accepted.distribution is not None
    assert accepted.distribution == survivor.adjusted_distribution
    assert accepted.receipt.effective_weights == ((AssessorKind.ML, "1"),)
    assert (
        accepted.receipt.normalization_denominator
        == "0.3333333333333333333333333333333333"
    )


def test_zero_valid_forecasts_requires_complete_manual_construction() -> None:
    result = pool_forecasts(
        (),
        _weights(),
        _capability_state(),
        SamplingSpec(seed=1, draw_count=8),
    )
    assert result.mode is PoolMode.MANUAL_REQUIRED
    assert result.receipt.available_count == 0
    assert result.receipt.effective_weights == ()
    assert result.receipt.normalization_denominator == "0"
    assert result.receipt.missing_mass == "1"
    assert result.receipt.algorithm == "no-pool-manual-construction-v1"
    assert result.distribution is None


def test_abstention_invalidity_and_missing_are_distinct_and_never_gain_weight() -> None:
    result = pool_forecasts(
        (
            _forecast(AssessorKind.FORMULA, 32_000),
            _forecast(AssessorKind.ML, 1, state=ForecastState.ABSTAINED),
            _forecast(AssessorKind.LLM_COUNCIL, 1, state=ForecastState.INVALID),
        ),
        _weights(),
        _capability_state(),
        SamplingSpec(seed=2, draw_count=8),
    )
    states = {row.assessor: row.availability for row in result.receipt.components}
    assert states == {
        AssessorKind.FORMULA: AvailabilityState.VALID,
        AssessorKind.ML: AvailabilityState.ABSTAINED,
        AssessorKind.LLM_COUNCIL: AvailabilityState.INVALID,
    }
    assert result.receipt.missing_mass == "0.6666666666666666666666666666666667"


@pytest.mark.parametrize(
    ("forecasts", "weights", "message"),
    [
        (
            (
                _forecast(AssessorKind.FORMULA, 30_000),
                _forecast(AssessorKind.FORMULA, 31_000),
            ),
            _weights(),
            "unique",
        ),
        ((_forecast(AssessorKind.LLM_MEMBER, 30_000),), _weights(), "outer"),
        (
            (_forecast(AssessorKind.FORMULA, 30_000),),
            _weights(("0.2", "0.2", "0.2")),
            "sum",
        ),
    ],
)
def test_pooling_fails_closed_on_duplicate_nonouter_or_invalid_weight_authority(
    forecasts: tuple[AssessorForecast, ...], weights: WeightReceipt, message: str
) -> None:
    with pytest.raises((ContractError, ValueError), match=message):
        pool_forecasts(
            forecasts,
            weights,
            _capability_state(),
            SamplingSpec(seed=1, draw_count=8),
        )


def test_linear_pool_moment_tracks_weighted_component_moment_without_endpoint_averaging() -> (
    None
):
    result = pool_forecasts(
        (_forecast(AssessorKind.FORMULA, 20_000), _forecast(AssessorKind.ML, 60_000)),
        _weights(("0.75", "0.25", "0")),
        _capability_state(),
        SamplingSpec(seed=99, draw_count=10_000),
    )
    assert result.samples is not None
    mean = Decimal(sum(result.samples.samples_ms)) / Decimal(
        len(result.samples.samples_ms)
    )
    component_medians = {
        row.assessor: row.adjusted_distribution.median_ms
        for row in result.receipt.components
        if row.adjusted_distribution is not None
    }
    expected = Decimal(component_medians[AssessorKind.FORMULA]) * Decimal(
        "0.75"
    ) + Decimal(component_medians[AssessorKind.ML]) * Decimal("0.25")
    assert abs(mean - expected) < 500
    assert min(result.samples.samples_ms) < 35_000 < max(result.samples.samples_ms)


def test_sealed_mixture_modes_survive_downstream_joint_and_counterfactual_sampling() -> (
    None
):
    result = pool_forecasts(
        (_forecast(AssessorKind.FORMULA, 12_000), _forecast(AssessorKind.ML, 100_000)),
        _weights(("0.5", "0.5", "0")),
        _capability_state(),
        SamplingSpec(seed=123, draw_count=2_000),
    )
    assert isinstance(result.distribution, LinearPooledDistribution)
    artifact = train_dependence_artifact(
        (),
        ContextNode("underhand"),
        1,
        DependencePolicy(),
        artifact_id=StableIdentifier("artifact:mixture-test"),
        training_evidence_digest="a" * 64,
        active_projection_digest="9" * 64,
        promotion_receipt_digest="b" * 64,
    )
    model = bind_field_dependence(
        artifact,
        ContextNode("underhand"),
        field_id=StableIdentifier("field:mixture-final"),
    )
    joint = generate_joint_draws(
        (
            FieldCompetitorForecast(
                StableIdentifier("competitor:alice"),
                "stand:1",
                result.distribution,
                0,
            ),
        ),
        model,
        installed_artifact=artifact,
        seed=44,
        draw_count=2_000,
    )
    samples = joint.competitors[0].samples_ms
    assert any(item < 40_000 for item in samples)
    assert any(item > 65_000 for item in samples)
    assert not any(45_000 < item < 60_000 for item in samples)
    replay = generate_joint_draws(
        (
            FieldCompetitorForecast(
                StableIdentifier("competitor:renamed"),
                "renamed-slot",
                result.distribution,
                10,
            ),
        ),
        model,
        installed_artifact=artifact,
        seed=44,
        draw_count=2_000,
    )
    assert replay.competitors[0].samples_ms == samples
    assert replay.common_random_map_digest == joint.common_random_map_digest


def test_linear_pool_sampling_is_independent_of_ambient_decimal_context() -> None:
    mixture = LinearPooledDistribution(
        (
            LinearPoolComponent(
                AssessorKind.FORMULA,
                "0.3333333333333333333333333333333333",
                _distribution(30_000, 7_000),
            ),
            LinearPoolComponent(
                AssessorKind.ML,
                "0.3333333333333333333333333333333333",
                _distribution(40_000, 9_000),
            ),
            LinearPoolComponent(
                AssessorKind.LLM_COUNCIL,
                "0.3333333333333333333333333333333334",
                _distribution(50_000, 11_000),
            ),
        )
    )
    spec = SamplingSpec(
        seed=20260824,
        draw_count=8,
        common_uniforms=(
            "0.0000000000000000000000000001",
            "0.3333333333333333333333333332",
            "0.3333333333333333333333333334",
            "0.5",
            "0.6666666666666666666666666665",
            "0.6666666666666666666666666667",
            "0.9999999999999999999999999998",
            "0.9999999999999999999999999999",
        ),
        common_random_map_digest="f" * 64,
    )
    expected = mixture.sample(spec)

    with localcontext() as hostile:
        hostile.prec = 6
        hostile.rounding = ROUND_DOWN
        actual = mixture.sample(spec)

    assert actual == expected


def test_pool_receipt_roundtrip_replays_and_rejects_sample_or_component_substitution() -> (
    None
):
    result = pool_forecasts(
        (_forecast(AssessorKind.FORMULA, 30_000), _forecast(AssessorKind.ML, 50_000)),
        _weights(("0.4", "0.6", "0")),
        _capability_state(),
        SamplingSpec(seed=8, draw_count=64),
    )
    assert PoolReceipt.from_dict(result.receipt.to_dict()) == result.receipt
    corrupted = result.receipt.to_dict()
    corrupted["pooled_samples_ms"][0] += 1
    with pytest.raises(ContractError, match="authority digest"):
        PoolReceipt.from_dict(corrupted)
    with pytest.raises(ContractError, match="component weights"):
        replace(
            result.receipt,
            components=(
                replace(result.receipt.components[0], effective_weight="0.5"),
                *result.receipt.components[1:],
            ),
        )
    assert isinstance(result.receipt.pooled_distribution, LinearPooledDistribution)
    substituted = replace(
        result.receipt.pooled_distribution.components[0],
        distribution=_distribution(90_000),
    )
    with pytest.raises(ContractError, match="sealed mixture"):
        replace(
            result.receipt,
            pooled_distribution=LinearPooledDistribution(
                (substituted, *result.receipt.pooled_distribution.components[1:])
            ),
        )


def test_raw_weight_receipt_is_explicitly_pending_and_cross_bound_to_pool() -> None:
    weights = _weights(("0.4", "0.6", "0"))
    authority = _weight_authority(weights)
    assert authority.verification_status is WeightAuthorityStatus.PENDING
    assert WeightAuthorityBinding.from_dict(authority.to_dict()) == authority
    result = _domain_pool_forecasts(
        (_forecast(AssessorKind.FORMULA, 30_000), _forecast(AssessorKind.ML, 50_000)),
        weights,
        _capability_state(),
        SamplingSpec(1, 16),
        weight_authority=authority,
    )
    assert result.receipt.weight_authority == authority
    with pytest.raises(ContractError, match="U12"):
        replace(authority, verification_status=WeightAuthorityStatus.VERIFIED)
    for changes in (
        {"context": object()},
        {"calibration_cutoff_at_utc": ""},
        {"tournament_event_sequence": True},
        {"tournament_event_sequence": "1"},
        {"tournament_event_sequence": -1},
        {"source_global_sequence": True},
        {"binding_digest": "0" * 64},
    ):
        with pytest.raises((ContractError, TypeError)):
            replace(authority, **changes)
    with pytest.raises(ContractError, match="typed"):
        WeightAuthorityBinding.pending(
            object(),
            ledger_projection_digest="e" * 64,
            tournament_event_sequence=1,
            source_global_sequence=10,
        )
    for change in (
        {"schema_version": "bad"},
        {"context": "bad"},
        {"verification_status": "bad"},
    ):
        encoded = authority.to_dict()
        encoded.update(change)
        with pytest.raises(ContractError):
            WeightAuthorityBinding.from_dict(encoded)
    for substituted in (
        replace(weights, context=ContextNode("standing")),
        replace(weights, calibration_cutoff_at_utc="2026-01-02T00:00:00.000Z"),
        _weights(("0.5", "0.5", "0")),
    ):
        with pytest.raises(ContractError, match="does not bind"):
            _domain_pool_forecasts(
                (
                    _forecast(AssessorKind.FORMULA, 30_000),
                    _forecast(AssessorKind.ML, 50_000),
                ),
                substituted,
                _capability_state(),
                SamplingSpec(1, 16),
                weight_authority=authority,
            )
    with pytest.raises(ContractError, match="baseline"):
        replace(result.receipt, weight_authority=object())
    encoded_receipt = result.receipt.to_dict()
    encoded_receipt["weight_authority"] = "bad"
    with pytest.raises(ContractError, match="weight authority"):
        PoolReceipt.from_dict(encoded_receipt)


def test_pool_contracts_fail_closed_across_constructor_and_decoder_edges() -> None:
    distribution = _distribution(40_000, 4_000)
    component = LinearPoolComponent(AssessorKind.FORMULA, "0.5", distribution)
    with pytest.raises(ContractError, match="outer"):
        LinearPoolComponent(AssessorKind.LLM_MEMBER, "0.5", distribution)
    with pytest.raises(ContractError, match="positive"):
        replace(component, weight="0")
    with pytest.raises(ContractError, match="distribution"):
        replace(component, distribution=object())
    with pytest.raises(ContractError, match="fields"):
        LinearPoolComponent.from_dict({})
    for change in ({"assessor": "unknown"}, {"distribution": "bad"}):
        encoded_component = component.to_dict()
        encoded_component.update(change)
        with pytest.raises(ContractError):
            LinearPoolComponent.from_dict(encoded_component)

    ml_component = LinearPoolComponent(AssessorKind.ML, "0.5", _distribution(50_000))
    mixture = LinearPooledDistribution((component, ml_component))
    assert LinearPooledDistribution.from_dict(mixture.to_dict()) == mixture
    assert mixture.quantile_summary().median_ms == mixture.median_ms
    with pytest.raises(ContractError, match="at least two"):
        LinearPooledDistribution((component,))
    with pytest.raises(ContractError, match="ordered"):
        LinearPooledDistribution((ml_component, component))
    with pytest.raises(ContractError, match="sum"):
        LinearPooledDistribution((component, replace(ml_component, weight="0.4")))
    with pytest.raises(ContractError, match="sum"):
        LinearPooledDistribution(
            (
                component,
                replace(
                    ml_component,
                    weight=(
                        "0.50000000000000000000000000000000000000000000000000"
                        "00000000000000000000000000000000000000000000000001"
                    ),
                ),
            )
        )
    with pytest.raises(ContractError, match="SamplingSpec"):
        mixture.sample(object())
    for change in (
        {"schema_version": "bad"},
        {"algorithm": "bad"},
        {"components": "bad"},
    ):
        encoded_mixture = mixture.to_dict()
        encoded_mixture.update(change)
        with pytest.raises(ContractError, match="algorithm"):
            LinearPooledDistribution.from_dict(encoded_mixture)

    normal = pool_forecasts(
        (_forecast(AssessorKind.FORMULA, 30_000), _forecast(AssessorKind.ML, 50_000)),
        _weights(("0.4", "0.6", "0")),
        _capability_state(),
        SamplingSpec(seed=8, draw_count=16),
    ).receipt
    valid_component = normal.components[0]
    unavailable = normal.components[2]
    for changes in (
        {"assessor": AssessorKind.LLM_MEMBER},
        {"availability": "valid"},
        {"availability_reason": ""},
        {"baseline_weight": "-1"},
        {"forecast_commit_digest": "bad"},
        {"forecast_id": None},
    ):
        with pytest.raises(ContractError):
            replace(valid_component, **changes)
    with pytest.raises(ContractError, match="influence"):
        replace(unavailable, effective_weight="0.1")
    encoded_component = valid_component.to_dict()
    encoded_component["unexpected"] = True
    with pytest.raises(ContractError, match="fields"):
        PoolComponentReceipt.from_dict(encoded_component)
    encoded_component = valid_component.to_dict()
    encoded_component["availability"] = "unknown"
    with pytest.raises(ContractError, match="vocabulary"):
        PoolComponentReceipt.from_dict(encoded_component)

    single = pool_forecasts(
        (_forecast(AssessorKind.FORMULA, 30_000),),
        _weights(),
        _capability_state(),
        SamplingSpec(seed=8, draw_count=16),
        accept_single_survivor=True,
    ).receipt
    blocked = pool_forecasts(
        (), _weights(), _capability_state(), SamplingSpec(seed=8, draw_count=16)
    ).receipt
    assert normal.pooled_summary == normal.pooled_distribution.quantile_summary()
    assert single.pooled_summary == single.pooled_distribution
    assert blocked.pooled_summary is None
    assert (
        PoolReceipt.from_dict(normal.to_dict()).pooled_summary == normal.pooled_summary
    )
    with pytest.raises(ContractError, match="summary"):
        replace(normal, pooled_summary=None)
    with pytest.raises(ContractError, match="summary"):
        replace(normal, pooled_summary=object())
    with pytest.raises(ContractError, match="summary"):
        replace(normal, pooled_summary=_distribution(99_000))
    mutations = (
        {"mode": "normal_three"},
        {"components": list(normal.components)},
        {"components": tuple(reversed(normal.components))},
        {"available_count": 3},
        {"mode": PoolMode.NORMAL},
        {"is_ensemble": False},
        {"effective_weights": ((AssessorKind.ML, "1"),)},
        {"normalization_denominator": "0.5"},
        {"missing_mass": "0.4"},
        {"capability_operator_version": "bad"},
        {"capability_state_digest": "bad"},
        {"pooled_distribution": None},
        {"pooled_distribution": object()},
        {"pooled_samples_digest": "bad"},
        {"pooled_samples_ms": None},
        {"common_uniforms": ()},
        {"source_common_random_map_digest": "bad"},
        {"algorithm": "bad"},
        {"dependency_version": "bad"},
        {"time_quantum_ms": 2},
        {"common_random_map_digest": "0" * 64},
        {"receipt_digest": "0" * 64},
    )
    for changes in mutations:
        with pytest.raises((ContractError, ValueError, TypeError)):
            replace(normal, **changes)
    with pytest.raises(ContractError, match="typed"):
        replace(normal, components=(object(), *normal.components[1:]))
    with pytest.raises(ContractError, match="sum exactly"):
        replace(
            normal,
            effective_weights=(
                (AssessorKind.FORMULA, "0.3"),
                (AssessorKind.ML, "0.6"),
            ),
        )
    with pytest.raises(ContractError, match="sealed linear mixture"):
        replace(normal, pooled_distribution=_distribution(40_000))
    with pytest.raises(ContractError, match="survivor"):
        replace(single, pooled_distribution=_distribution(99_000))
    with pytest.raises(ContractError, match="presence"):
        replace(blocked, pooled_samples_ms=(1,))
    corrupted_component = replace(normal.components[0], samples_digest="0" * 64)
    with pytest.raises(ContractError, match="standalone replay"):
        replace(normal, components=(corrupted_component, *normal.components[1:]))
    with pytest.raises(ContractError, match="standalone replay"):
        replace(
            normal,
            pooled_samples_ms=(
                normal.pooled_samples_ms[0] + 1,
                *normal.pooled_samples_ms[1:],
            ),
            receipt_digest=normal.receipt_digest,
        )

    encoded = normal.to_dict()
    for key, value in (
        ("schema_version", "bad"),
        ("mode", "bad"),
        ("components", "bad"),
        ("pooled_samples_ms", "bad"),
        ("common_uniforms", "bad"),
    ):
        changed = dict(encoded)
        changed[key] = value
        with pytest.raises(ContractError):
            PoolReceipt.from_dict(changed)
    changed = dict(encoded)
    changed["baseline_weights"] = "bad"
    with pytest.raises(ContractError, match="array"):
        PoolReceipt.from_dict(changed)
    changed = dict(encoded)
    changed["baseline_weights"] = [["bad", "1"]]
    with pytest.raises(ContractError, match="unknown"):
        PoolReceipt.from_dict(changed)
    changed = dict(encoded)
    changed["baseline_weights"] = [[AssessorKind.FORMULA.value]]
    with pytest.raises(ContractError, match="row"):
        PoolReceipt.from_dict(changed)
    changed = dict(encoded)
    changed["pooled_distribution"] = "bad"
    with pytest.raises(ContractError, match="distribution"):
        PoolReceipt.from_dict(changed)
    changed = dict(encoded)
    changed["pooled_summary"] = "bad"
    with pytest.raises(ContractError, match="summary"):
        PoolReceipt.from_dict(changed)
    assert PoolReceipt.from_dict(single.to_dict()) == single
    assert PoolReceipt.from_dict(blocked.to_dict()) == blocked

    for invalid_weights in (
        tuple(reversed(_weights().weights)),
        (
            (AssessorKind.FORMULA, "0.3333333333333333333333333333333333"),
            (AssessorKind.ML, "0.3333333333333333333333333333333333"),
            (AssessorKind.LLM_COUNCIL, "0.3333333333333333333333333333333330"),
        ),
        (
            (AssessorKind.FORMULA, "-0.1"),
            (AssessorKind.ML, "0.6"),
            (AssessorKind.LLM_COUNCIL, "0.5"),
        ),
        (
            (AssessorKind.FORMULA, "0.40"),
            (AssessorKind.ML, "0.3"),
            (AssessorKind.LLM_COUNCIL, "0.3"),
        ),
    ):
        with pytest.raises(ContractError):
            pool_forecasts(
                (),
                replace(_weights(), weights=invalid_weights),
                _capability_state(),
                SamplingSpec(1, 2),
            )

    for invalid_baseline in (
        list(normal.baseline_weights),
        tuple(reversed(normal.baseline_weights)),
        (
            (AssessorKind.FORMULA, "-0.1"),
            (AssessorKind.ML, "0.6"),
            (AssessorKind.LLM_COUNCIL, "0.5"),
        ),
        (
            (AssessorKind.FORMULA, "0.4"),
            (AssessorKind.ML, "0.4"),
            (AssessorKind.LLM_COUNCIL, "0.4"),
        ),
        (
            (AssessorKind.FORMULA, "0.40"),
            (AssessorKind.ML, "0.6"),
            (AssessorKind.LLM_COUNCIL, "0"),
        ),
    ):
        with pytest.raises(ContractError):
            replace(normal, baseline_weights=invalid_baseline)

    with pytest.raises(ContractError, match="forecasts"):
        pool_forecasts([], _weights(), _capability_state(), SamplingSpec(1, 2))
    with pytest.raises(ContractError, match="typed weight"):
        pool_forecasts((), object(), _capability_state(), SamplingSpec(1, 2))
    with pytest.raises(ContractError, match="SamplingSpec"):
        pool_forecasts((), _weights(), _capability_state(), object())
