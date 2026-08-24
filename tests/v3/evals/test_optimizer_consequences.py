from __future__ import annotations

from dataclasses import replace

import pytest

from strathmark.v3.application.credibility_reactions import (
    FieldForecastCard,
    OptimizerScoringInput,
    SettledFieldResult,
    _optimizer_scoring_input,
)
from strathmark.v3.contracts.forecasts import (
    AssessorForecast,
    AssessorKind,
    EvidenceSupport,
    ForecastState,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.credibility import ConsequenceStatus, ContextNode
from strathmark.v3.domain.joint_dependence import (
    DependencePolicy,
    FieldCompetitorForecast,
    bind_field_dependence,
    generate_joint_draws,
    train_dependence_artifact,
)
from strathmark.v3.domain.optimizer import (
    ConsequenceReplayBinding,
    OptimizationField,
    SharedOptimizerConsequenceEvaluator,
    consequence_metrics,
    optimize_field,
)


def _forecast(
    identity: str,
    median: int,
    assessor: AssessorKind = AssessorKind.FORMULA,
) -> AssessorForecast:
    return AssessorForecast.create(
        forecast_id=StableIdentifier(identity),
        assessor=assessor,
        state=ForecastState.COMMITTED,
        evidence_digest="1" * 64,
        distribution=PositiveTimeDistribution(
            (
                QuantilePoint("0.1", median - 1_000),
                QuantilePoint("0.5", median),
                QuantilePoint("0.9", median + 1_000),
            )
        ),
        support=EvidenceSupport(10, "10", 5, "history:prior", 10),
        warnings=(),
        artifacts=(),
        abstention_code=None,
    )


def _input(
    cards: tuple[FieldForecastCard, ...],
    *,
    field_results: tuple[SettledFieldResult, ...] | None = None,
    optimizer_bundle_digest: str = "5" * 64,
    context: ContextNode = ContextNode("uh", "300_349", "eucalypt", "deep"),
) -> OptimizerScoringInput:
    field_results = field_results or (
        SettledFieldResult("competitor:a", "result:a", 1, "2" * 64, 20, "valid", 50_000),
        SettledFieldResult("competitor:b", "result:b", 1, "3" * 64, 21, "valid", 40_000),
    )
    return _optimizer_scoring_input(
        tournament_id="tournament:test",
        round_id="round:test",
        field_id="field:test",
        competitor_id="competitor:a",
        result_id="result:a",
        result_revision=1,
        result_revision_digest="2" * 64,
        source_sequence=20,
        issued_field_members=("competitor:a", "competitor:b"),
        issued_marks=(("competitor:a", 3), ("competitor:b", 13)),
        field_results=field_results,
        field_forecasts=cards,
        field_receipt_digest="4" * 64,
        optimizer_bundle_digest=optimizer_bundle_digest,
        credibility_policy_digest="6" * 64,
        raw_time_ms=50_000,
        context=context,
        robust_context_scale_ms=5_000,
        evidence_weight="1",
    )


def _artifact():
    return train_dependence_artifact(
        (),
        ContextNode("uh", "300_349", "eucalypt", "deep"),
        10,
        DependencePolicy(),
        artifact_id=StableIdentifier("artifact:optimizer-test"),
        training_evidence_digest="7" * 64,
        active_projection_digest="8" * 64,
        promotion_receipt_digest="9" * 64,
    )


def test_installed_shared_evaluator_replays_same_optimizer_for_numeric_consequences() -> None:
    a = _forecast("forecast:a", 50_000)
    b = _forecast("forecast:b", 40_000)
    scoring = _input(
        (FieldForecastCard("competitor:a", None, a), FieldForecastCard("competitor:b", None, b))
    )
    artifact = _artifact()
    source_digest = "b" * 64
    seed = int(source_digest[:16], 16) & ((1 << 63) - 1)
    basis = (
        FieldCompetitorForecast(
            StableIdentifier("competitor:b"), "issued-slot:b", b.distribution, 0
        ),
        FieldCompetitorForecast(
            StableIdentifier("competitor:a"), "issued-slot:a", a.distribution, 1
        ),
    )
    model = bind_field_dependence(
        artifact,
        artifact.target_context,
        field_id=StableIdentifier("field:test"),
    )
    issued_draws = generate_joint_draws(
        basis, model, installed_artifact=artifact, seed=seed, draw_count=4096
    )
    issued_input = OptimizationField.from_joint_draws(
        issued_draws,
        forecasts=basis,
        source_receipt_digest=source_digest,
        pool_receipt_digest="a" * 64,
    )
    issued_optimizer = optimize_field(issued_input, ceiling=183)
    binding = ConsequenceReplayBinding.create(
        field_receipt_digest="4" * 64,
        field_id=StableIdentifier("field:test"),
        dependence_artifact_digest=artifact.artifact_digest,
        pool_receipt_digest="a" * 64,
        optimizer_source_receipt_digest=source_digest,
        optimizer_seed=seed,
        common_random_map_digest=issued_draws.common_random_map_digest,
        issued_joint_samples_digest=issued_draws.joint_samples_digest,
        issued_optimizer_receipt_digest=issued_optimizer.receipt_digest,
        slots=(
            (StableIdentifier("competitor:b"), "issued-slot:b", 0),
            (StableIdentifier("competitor:a"), "issued-slot:a", 1),
        ),
    )
    evaluator = SharedOptimizerConsequenceEvaluator(
        bundle_digest="5" * 64,
        installed_dependence_artifact=artifact,
        replay_bindings={"4" * 64: binding},
    )
    with pytest.raises(Exception, match="installed U13"):
        SharedOptimizerConsequenceEvaluator(
            bundle_digest="5" * 64,
            installed_dependence_artifact=object(),
            replay_bindings={"4" * 64: binding},
        )
    with pytest.raises(Exception, match="U15"):
        SharedOptimizerConsequenceEvaluator(
            bundle_digest="5" * 64,
            installed_dependence_artifact=artifact,
            replay_bindings={},
        )
    with pytest.raises(Exception, match="installed artifact"):
        SharedOptimizerConsequenceEvaluator(
            bundle_digest="5" * 64,
            installed_dependence_artifact=artifact,
            replay_bindings={"0" * 64: binding},
        )
    with pytest.raises(Exception, match="sealed"):
        evaluator.evaluate(forecast=object(), scoring_input=scoring)
    with pytest.raises(Exception, match="bundle"):
        evaluator.evaluate(
            forecast=a,
            scoring_input=_input(
                (
                    FieldForecastCard("competitor:a", None, a),
                    FieldForecastCard("competitor:b", None, b),
                ),
                optimizer_bundle_digest="0" * 64,
            ),
        )
    abstained = AssessorForecast.create(
        forecast_id=StableIdentifier("forecast:abstained"),
        assessor=AssessorKind.FORMULA,
        state=ForecastState.ABSTAINED,
        evidence_digest="1" * 64,
        distribution=None,
        support=a.support,
        warnings=(),
        artifacts=(),
        abstention_code="insufficient_evidence",
    )
    assert (
        evaluator.evaluate(forecast=abstained, scoring_input=scoring).status
        is ConsequenceStatus.PENDING
    )
    receipt = evaluator.evaluate(forecast=a, scoring_input=scoring)
    assert receipt.status is ConsequenceStatus.DIAGNOSTIC
    assert receipt.metrics is not None
    assert receipt.metrics.spread_ms == 0
    assert receipt.metrics.gap_error_ms == 0
    assert receipt.metrics.class_context_bias_ms == 0
    assert receipt.metrics.win_probability_distortion >= "0"
    assert receipt.optimizer_bundle_digest == "5" * 64
    assert evaluator.implementation_digest
    assert issued_input.joint_samples_digest == issued_draws.joint_samples_digest

    with pytest.raises(Exception, match="seed"):
        OptimizationField.from_joint_draws(
            issued_draws,
            forecasts=basis,
            source_receipt_digest="c" * 64,
            pool_receipt_digest="a" * 64,
        )
    with pytest.raises(Exception, match="typed U13"):
        OptimizationField.from_joint_draws(
            object(),
            forecasts=basis,
            source_receipt_digest=source_digest,
            pool_receipt_digest="a" * 64,
        )
    short_draws = generate_joint_draws(
        basis, model, installed_artifact=artifact, seed=seed, draw_count=10
    )
    with pytest.raises(Exception, match="4096"):
        OptimizationField.from_joint_draws(
            short_draws,
            forecasts=basis,
            source_receipt_digest=source_digest,
            pool_receipt_digest="a" * 64,
        )
    with pytest.raises(Exception, match="typed U13 forecasts"):
        OptimizationField.from_joint_draws(
            issued_draws,
            forecasts=[*basis],
            source_receipt_digest=source_digest,
            pool_receipt_digest="a" * 64,
        )
    with pytest.raises(Exception, match="roster"):
        OptimizationField.from_joint_draws(
            issued_draws,
            forecasts=basis[:1],
            source_receipt_digest=source_digest,
            pool_receipt_digest="a" * 64,
        )
    mismatched_basis = (
        replace(basis[0], competitor_id=StableIdentifier("competitor:c")),
        basis[1],
    )
    with pytest.raises(Exception, match="bound"):
        OptimizationField.from_joint_draws(
            issued_draws,
            forecasts=mismatched_basis,
            source_receipt_digest=source_digest,
            pool_receipt_digest="a" * 64,
        )

    for changes, message in (
        ({"optimizer_seed": 0}, "seed"),
        ({"slots": ()}, "complete field"),
        ({"slots": ((StableIdentifier("competitor:a"), "", 0),)}, "draw slot"),
        (
            {"slots": ((StableIdentifier("competitor:a"), "issued-slot:a", -1),)},
            "crn index",
        ),
        (
            {
                "slots": (
                    (StableIdentifier("competitor:a"), "issued-slot:a", 1),
                    (StableIdentifier("competitor:b"), "issued-slot:b", 0),
                )
            },
            "CRN order",
        ),
        (
            {
                "slots": (
                    (StableIdentifier("competitor:a"), "issued-slot:a", 0),
                    (StableIdentifier("competitor:a"), "issued-slot:b", 1),
                )
            },
            "unique",
        ),
        ({"binding_digest": "0" * 64}, "binding digest"),
    ):
        with pytest.raises(Exception, match=message):
            replace(binding, **changes)

    incomplete = _input(
        (FieldForecastCard("competitor:a", None, a), FieldForecastCard("competitor:b", None, b)),
        field_results=(
            SettledFieldResult("competitor:a", "result:a", 1, "2" * 64, 20, "valid", 50_000),
            SettledFieldResult("competitor:b", "result:b", 1, "3" * 64, 21, "dnf", None),
        ),
    )
    pending = evaluator.evaluate(forecast=a, scoring_input=incomplete)
    assert pending.status is ConsequenceStatus.PENDING
    assert pending.metrics is None

    missing_assessor = _input((FieldForecastCard("competitor:a", None, a),))
    pending = evaluator.evaluate(forecast=a, scoring_input=missing_assessor)
    assert pending.status is ConsequenceStatus.PENDING
    assert pending.metrics is None

    other_binding = ConsequenceReplayBinding.create(
        **{
            **{k: v for k, v in binding.content_value().items() if k != "schema_version"},
            "field_receipt_digest": "6" * 64,
            "field_id": StableIdentifier("field:test"),
            "slots": binding.slots,
        }
    )
    awaiting = SharedOptimizerConsequenceEvaluator(
        bundle_digest="5" * 64,
        installed_dependence_artifact=artifact,
        replay_bindings={"6" * 64: other_binding},
    )
    assert awaiting.evaluate(forecast=a, scoring_input=scoring).status is ConsequenceStatus.PENDING

    wrong_field = ConsequenceReplayBinding.create(
        **{
            **{k: v for k, v in binding.content_value().items() if k != "schema_version"},
            "field_id": StableIdentifier("field:other"),
            "slots": binding.slots,
        }
    )
    wrong_field_evaluator = SharedOptimizerConsequenceEvaluator(
        bundle_digest="5" * 64,
        installed_dependence_artifact=artifact,
        replay_bindings={"4" * 64: wrong_field},
    )
    with pytest.raises(Exception, match="exact field"):
        wrong_field_evaluator.evaluate(forecast=a, scoring_input=scoring)

    wrong_roster = ConsequenceReplayBinding.create(
        **{
            **{k: v for k, v in binding.content_value().items() if k != "schema_version"},
            "field_id": StableIdentifier("field:test"),
            "slots": (binding.slots[0],),
        }
    )
    wrong_roster_evaluator = SharedOptimizerConsequenceEvaluator(
        bundle_digest="5" * 64,
        installed_dependence_artifact=artifact,
        replay_bindings={"4" * 64: wrong_roster},
    )
    with pytest.raises(Exception, match="roster"):
        wrong_roster_evaluator.evaluate(forecast=a, scoring_input=scoring)

    wrong_context = _input(
        (FieldForecastCard("competitor:a", None, a), FieldForecastCard("competitor:b", None, b)),
        context=ContextNode("standing", "300_349", "eucalypt", "deep"),
    )
    with pytest.raises(Exception, match="context"):
        evaluator.evaluate(forecast=a, scoring_input=wrong_context)

    wrong_map = ConsequenceReplayBinding.create(
        **{
            **{k: v for k, v in binding.content_value().items() if k != "schema_version"},
            "field_id": StableIdentifier("field:test"),
            "common_random_map_digest": "0" * 64,
            "slots": binding.slots,
        }
    )
    wrong_map_evaluator = SharedOptimizerConsequenceEvaluator(
        bundle_digest="5" * 64,
        installed_dependence_artifact=artifact,
        replay_bindings={"4" * 64: wrong_map},
    )
    with pytest.raises(Exception, match="common-random"):
        wrong_map_evaluator.evaluate(forecast=a, scoring_input=scoring)


def test_consequence_metrics_detect_spread_gap_breakout_and_optimizer_repair() -> None:
    metrics = consequence_metrics(
        expected_times_ms=(50_000, 40_000),
        actual_times_ms=(40_000, 40_000),
        marks=(3, 13),
        baseline_marks=(3, 12),
        win_probabilities=("0.75", "0.25"),
    )
    assert metrics.spread_ms == 10_000
    assert metrics.class_context_bias_ms == -5_000
    assert metrics.gap_error_ms == 10_000
    assert metrics.win_probability_distortion == "0.25"
    assert metrics.breakout_exposure == "0.5"
    assert metrics.optimizer_repair is True
    with pytest.raises(Exception, match="nonempty"):
        consequence_metrics(
            expected_times_ms=(),
            actual_times_ms=(),
            marks=(),
            baseline_marks=(),
            win_probabilities=(),
        )


def test_llm_member_lineage_is_selected_exactly_and_never_collapsed() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    a1 = _forecast("forecast:a1", 51_000, AssessorKind.LLM_MEMBER)
    a2 = _forecast("forecast:a2", 52_000, AssessorKind.LLM_MEMBER)
    a3 = _forecast("forecast:a3", 53_000, AssessorKind.LLM_MEMBER)
    b1 = _forecast("forecast:b1", 41_000, AssessorKind.LLM_MEMBER)
    b2 = _forecast("forecast:b2", 42_000, AssessorKind.LLM_MEMBER)
    b3 = _forecast("forecast:b3", 43_000, AssessorKind.LLM_MEMBER)
    council_a = _forecast("forecast:council-a", 50_500, AssessorKind.LLM_COUNCIL)
    council_b = _forecast("forecast:council-b", 40_500, AssessorKind.LLM_COUNCIL)
    scoring = _input(
        (
            FieldForecastCard("competitor:a", "member:1", a1),
            FieldForecastCard("competitor:a", "member:2", a2),
            FieldForecastCard("competitor:a", "member:3", a3),
            FieldForecastCard("competitor:b", "member:1", b1),
            FieldForecastCard("competitor:b", "member:2", b2),
            FieldForecastCard("competitor:b", "member:3", b3),
            FieldForecastCard("competitor:a", None, council_a),
            FieldForecastCard("competitor:b", None, council_b),
        )
    )
    selected = optimizer._matching_field_distributions(a2, scoring)
    assert selected is not None
    assert selected["competitor:a"].digest == a2.distribution.digest
    assert selected["competitor:b"].digest == b2.distribution.digest
    council = optimizer._matching_field_distributions(council_a, scoring)
    assert council is not None
    assert council["competitor:b"].digest == council_b.distribution.digest

    missing = _input(
        tuple(card for card in scoring.field_forecasts if card is not scoring.field_forecasts[4])
    )
    assert optimizer._matching_field_distributions(a2, missing) is None
    absent = _forecast("forecast:absent", 54_000, AssessorKind.LLM_MEMBER)
    assert optimizer._matching_field_distributions(absent, scoring) is None
    no_member = _input(
        (
            FieldForecastCard("competitor:a", None, a2),
            FieldForecastCard("competitor:b", None, b2),
        )
    )
    assert optimizer._matching_field_distributions(a2, no_member) is None
    duplicate = _input(
        (
            FieldForecastCard("competitor:a", "member:2", a2),
            FieldForecastCard("competitor:a", "member:2", a3),
            FieldForecastCard("competitor:b", "member:2", b2),
        )
    )
    assert optimizer._matching_field_distributions(a2, duplicate) is None
