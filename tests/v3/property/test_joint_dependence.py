from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from strathmark.v3.contracts.errors import ContractError
from strathmark.v3.contracts.forecasts import (
    DependenceMode,
    PositiveTimeDistribution,
    QuantilePoint,
)
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.credibility import ContextNode
from strathmark.v3.domain.joint_dependence import (
    DependenceArtifact,
    DependenceModel,
    DependencePolicy,
    FieldCompetitorForecast,
    JointCompetitorDraws,
    JointDraws,
    ResidualObservation,
    bind_field_dependence,
    fit_field_dependence,
    generate_joint_draws,
    train_dependence_artifact,
)


def _distribution(median: int) -> PositiveTimeDistribution:
    return PositiveTimeDistribution(
        (
            QuantilePoint("0.01", median - 8_000),
            QuantilePoint("0.5", median),
            QuantilePoint("0.99", median + 8_000),
        )
    )


def _observation(field: int, competitor: int, residual: str, sequence: int, context: ContextNode):
    return ResidualObservation(
        field_id=StableIdentifier(f"field:history-{field}"),
        competitor_id=StableIdentifier(f"competitor:history-{competitor}"),
        context=context,
        source_sequence=sequence,
        source_revision=1,
        active_projection_digest="9" * 64,
        standardized_residual=residual,
    )


def _correlation(left: tuple[int, ...], right: tuple[int, ...]) -> Decimal:
    lm = Decimal(sum(left)) / Decimal(len(left))
    rm = Decimal(sum(right)) / Decimal(len(right))
    covariance = sum((Decimal(a) - lm) * (Decimal(b) - rm) for a, b in zip(left, right))
    lv = sum((Decimal(a) - lm) ** 2 for a in left)
    rv = sum((Decimal(b) - rm) ** 2 for b in right)
    return covariance / (lv * rv).sqrt()


def _installed(
    observations: tuple[ResidualObservation, ...],
    context: ContextNode,
    cutoff: int,
    policy: DependencePolicy,
    field_id: StableIdentifier,
) -> tuple[DependenceArtifact, DependenceModel]:
    artifact = train_dependence_artifact(
        observations,
        context,
        cutoff,
        policy,
        artifact_id=StableIdentifier("artifact:test-dependence"),
        training_evidence_digest="a" * 64,
        active_projection_digest="9" * 64,
        promotion_receipt_digest="b" * 64,
    )
    return artifact, bind_field_dependence(artifact, context, field_id=field_id)


def test_training_produces_one_frozen_artifact_then_runtime_only_binds_fields() -> None:
    context = ContextNode("event", "underhand")
    observations = (
        _observation(1, 1, "1", 1, context),
        _observation(1, 2, "1", 1, context),
    )
    artifact = train_dependence_artifact(
        observations,
        context,
        2,
        DependencePolicy(minimum_pair_count=1),
        artifact_id=StableIdentifier("artifact:dependence-2026-08-23"),
        training_evidence_digest="a" * 64,
        active_projection_digest="9" * 64,
        promotion_receipt_digest="b" * 64,
    )
    assert DependenceArtifact.from_dict(artifact.to_dict()) == artifact

    first = bind_field_dependence(
        artifact,
        context,
        field_id=StableIdentifier("field:first"),
    )
    second = bind_field_dependence(
        artifact,
        context,
        field_id=StableIdentifier("field:second"),
    )
    assert first.artifact_digest == second.artifact_digest == artifact.artifact_digest
    assert first.rho == second.rho == artifact.rho
    assert first.field_id != second.field_id

    field = (
        FieldCompetitorForecast(
            StableIdentifier("competitor:a"), "stand:1", _distribution(40_000), 0
        ),
    )
    replay = generate_joint_draws(field, first, installed_artifact=artifact, seed=9, draw_count=32)
    assert replay == generate_joint_draws(
        field, first, installed_artifact=artifact, seed=9, draw_count=32
    )

    with pytest.raises(ContractError, match="artifact"):
        replace(artifact, training_evidence_digest="c" * 64)
    with pytest.raises(ContractError, match="artifact digest"):
        replace(
            artifact,
            artifact_id=StableIdentifier("artifact:forged"),
            artifact_digest=artifact.artifact_digest,
        )
    substituted = train_dependence_artifact(
        observations,
        context,
        2,
        DependencePolicy(minimum_pair_count=1),
        artifact_id=StableIdentifier("artifact:substituted"),
        training_evidence_digest="a" * 64,
        active_projection_digest="9" * 64,
        promotion_receipt_digest="b" * 64,
    )
    with pytest.raises(ContractError, match="installed artifact"):
        generate_joint_draws(
            field,
            first,
            installed_artifact=substituted,
            seed=9,
            draw_count=32,
        )


def test_sparse_fallback_is_a_frozen_artifact_decision() -> None:
    context = ContextNode("event", "underhand")
    artifact = train_dependence_artifact(
        (),
        context,
        10,
        DependencePolicy(),
        artifact_id=StableIdentifier("artifact:sparse-dependence"),
        training_evidence_digest="d" * 64,
        active_projection_digest="9" * 64,
        promotion_receipt_digest="e" * 64,
    )
    model = bind_field_dependence(artifact, context, field_id=StableIdentifier("field:sparse"))
    assert artifact.fallback_code == "unsupported_context_independence"
    assert model.fallback_code == artifact.fallback_code
    assert model.mode is DependenceMode.INDEPENDENCE


def test_cold_start_is_explicit_independence() -> None:
    context = ContextNode("event", "underhand")
    artifact, model = _installed(
        (),
        context,
        10,
        DependencePolicy(),
        StableIdentifier("field:future"),
    )
    assert model.mode is DependenceMode.INDEPENDENCE
    assert model.fallback_code == "unsupported_context_independence"
    assert model.rho == "0"
    assert model.effective_pair_count == 0


@pytest.mark.parametrize(
    ("pairs", "direction"),
    [
        (("1", "1", "-1", "-1"), 1),
        (("1", "-1", "-1", "1"), -1),
    ],
)
def test_supported_same_field_residuals_learn_positive_and_negative_dependence(
    pairs: tuple[str, ...], direction: int
) -> None:
    context = ContextNode("event", "underhand")
    observations = tuple(
        _observation(field, competitor, pairs[(field - 1) * 2 + competitor - 1], field, context)
        for field in (1, 2)
        for competitor in (1, 2)
    )
    artifact, model = _installed(
        observations,
        context,
        3,
        DependencePolicy(prior_strength="0.25", minimum_pair_count=2),
        StableIdentifier("field:future"),
    )
    assert model.mode is DependenceMode.SHARED_RANK_COPULA
    assert (Decimal(model.rho) > 0) is (direction > 0)
    assert model.effective_pair_count == 2
    assert model.parameters_digest is not None

    field = (
        FieldCompetitorForecast(
            StableIdentifier("competitor:a"), "stand:1", _distribution(40_000), 0
        ),
        FieldCompetitorForecast(
            StableIdentifier("competitor:b"), "stand:2", _distribution(45_000), 1
        ),
    )
    draws = generate_joint_draws(
        field, model, installed_artifact=artifact, seed=22, draw_count=2_000
    )
    assert draws.inputs.parameters_digest == model.parameters_digest
    correlation = _correlation(draws.competitors[0].samples_ms, draws.competitors[1].samples_ms)
    assert (correlation > Decimal("0.25")) is (direction > 0)
    assert (correlation < Decimal("-0.25")) is (direction < 0)


def test_hierarchical_sparse_context_shrinks_toward_supported_parent_then_independence() -> None:
    root = ContextNode()
    parent = ContextNode("underhand")
    target = ContextNode("underhand", "300", "pine")
    observations = tuple(
        _observation(field, competitor, residual, field, parent)
        for field, pair in enumerate((("1", "1"), ("1", "1"), ("-1", "-1")), 1)
        for competitor, residual in enumerate(pair, 1)
    ) + (
        _observation(20, 1, "1", 4, target),
        _observation(20, 2, "-1", 4, target),
    )
    artifact, model = _installed(
        observations,
        target,
        5,
        DependencePolicy(prior_strength="4", minimum_pair_count=1),
        StableIdentifier("field:future"),
    )
    assert Decimal("-1") < Decimal(model.rho) < Decimal("1")
    assert model.context_pair_counts == (
        ("global", 4),
        ("underhand", 4),
        ("underhand/300", 1),
        ("underhand/300/pine", 1),
    )
    assert model.shrinkage_path[0] == "0"
    assert model.shrinkage_path[-1] == model.rho


def test_hierarchy_uses_global_evidence_then_excludes_unrelated_from_target_branch() -> None:
    target = ContextNode("underhand", "300", "pine")
    exact = ContextNode("underhand", "300", "pine")
    sibling = ContextNode("underhand", "350", "pine")
    unrelated = ContextNode("standing", "300", "pine")
    observations = tuple(
        _observation(field, competitor, residual, field, context)
        for field, context, pair in (
            (1, exact, ("1", "1")),
            (2, sibling, ("1", "1")),
            (3, unrelated, ("-100", "100")),
        )
        for competitor, residual in enumerate(pair, 1)
    )
    artifact, model = _installed(
        observations,
        target,
        4,
        DependencePolicy(prior_strength="1", minimum_pair_count=1),
        StableIdentifier("field:hierarchy"),
    )
    assert (
        artifact.context_pair_counts
        == model.context_pair_counts
        == (
            ("global", 3),
            ("underhand", 2),
            ("underhand/300", 1),
            ("underhand/300/pine", 1),
        )
    )
    assert artifact.effective_pair_count == 3
    assert Decimal(artifact.rho) > 0


def test_future_or_other_field_context_evidence_cannot_change_frozen_model() -> None:
    context = ContextNode("event", "underhand")
    past = (
        _observation(1, 1, "1", 1, context),
        _observation(1, 2, "1", 1, context),
    )
    future = (
        _observation(2, 1, "100", 10, context),
        _observation(2, 2, "-100", 10, context),
    )
    baseline_artifact, baseline = _installed(
        past,
        context,
        10,
        DependencePolicy(minimum_pair_count=1),
        StableIdentifier("field:future"),
    )
    future_artifact, with_future = _installed(
        past + future,
        context,
        10,
        DependencePolicy(minimum_pair_count=1),
        StableIdentifier("field:future"),
    )
    assert with_future == baseline
    assert future_artifact == baseline_artifact


def test_training_artifact_is_byte_identical_under_evidence_permutation() -> None:
    context = ContextNode("event", "underhand")
    observations = (
        _observation(2, 2, "-1", 2, context),
        _observation(1, 1, "1", 1, context),
        _observation(2, 1, "-1", 2, context),
        _observation(1, 2, "1", 1, context),
    )
    artifact, _ = _installed(
        observations,
        context,
        3,
        DependencePolicy(minimum_pair_count=1),
        StableIdentifier("field:permutation"),
    )
    reversed_artifact, _ = _installed(
        tuple(reversed(observations)),
        context,
        3,
        DependencePolicy(minimum_pair_count=1),
        StableIdentifier("field:permutation"),
    )
    assert reversed_artifact.to_dict() == artifact.to_dict()
    assert reversed_artifact.artifact_digest == artifact.artifact_digest


def test_training_rejects_superseded_or_wrong_active_projection_residuals() -> None:
    context = ContextNode("underhand")
    original = _observation(1, 1, "1", 1, context)
    replacement = replace(
        original, source_sequence=2, source_revision=2, standardized_residual="-1"
    )
    with pytest.raises(ValueError, match="one active revision"):
        _installed(
            (original, replacement),
            context,
            3,
            DependencePolicy(minimum_pair_count=1),
            StableIdentifier("field:corrected"),
        )
    wrong_projection = replace(original, active_projection_digest="8" * 64)
    with pytest.raises(ValueError, match="active projection"):
        _installed(
            (wrong_projection,),
            context,
            3,
            DependencePolicy(),
            StableIdentifier("field:wrong-projection"),
        )


def test_joint_draws_are_permutation_identity_and_roster_invariant() -> None:
    context = ContextNode("event", "underhand")
    artifact, model = _installed(
        (),
        context,
        1,
        DependencePolicy(),
        StableIdentifier("field:future"),
    )
    alice = FieldCompetitorForecast(
        StableIdentifier("competitor:alice"), "stand:1", _distribution(40_000), 0
    )
    bob = FieldCompetitorForecast(
        StableIdentifier("competitor:bob"), "stand:2", _distribution(45_000), 1
    )
    carol = FieldCompetitorForecast(
        StableIdentifier("competitor:carol"), "stand:3", _distribution(50_000), 2
    )
    original = generate_joint_draws(
        (alice, bob), model, installed_artifact=artifact, seed=7, draw_count=128
    )
    permuted = generate_joint_draws(
        (bob, alice), model, installed_artifact=artifact, seed=7, draw_count=128
    )
    expanded = generate_joint_draws(
        (carol, bob, alice), model, installed_artifact=artifact, seed=7, draw_count=128
    )
    renamed = generate_joint_draws(
        (
            FieldCompetitorForecast(
                StableIdentifier("competitor:opaque-renamed"),
                "stand:1",
                alice.distribution,
                0,
            ),
            bob,
        ),
        model,
        installed_artifact=artifact,
        seed=7,
        draw_count=128,
    )

    def by_slot(result):
        return {row.draw_slot: row.samples_ms for row in result.competitors}

    assert by_slot(original) == by_slot(permuted)
    assert all(by_slot(expanded)[slot] == samples for slot, samples in by_slot(original).items())
    assert by_slot(renamed)["stand:1"] == by_slot(original)["stand:1"]
    assert original.common_random_map_digest == permuted.common_random_map_digest


def test_same_generator_and_common_random_numbers_apply_to_counterfactual_distributions() -> None:
    context = ContextNode("event", "underhand")
    artifact, model = _installed(
        (),
        context,
        1,
        DependencePolicy(),
        StableIdentifier("field:future"),
    )
    first = generate_joint_draws(
        (
            FieldCompetitorForecast(
                StableIdentifier("competitor:a"), "stand:1", _distribution(40_000), 0
            ),
        ),
        model,
        installed_artifact=artifact,
        seed=5,
        draw_count=64,
    )
    second = generate_joint_draws(
        (
            FieldCompetitorForecast(
                StableIdentifier("competitor:a"), "stand:1", _distribution(60_000), 0
            ),
        ),
        model,
        installed_artifact=artifact,
        seed=5,
        draw_count=64,
    )
    assert first.common_random_map_digest == second.common_random_map_digest
    assert (
        tuple(
            a < b for a, b in zip(first.competitors[0].samples_ms, second.competitors[0].samples_ms)
        )
        == (True,) * 64
    )


def test_dependence_contracts_reject_noncausal_duplicate_or_unbounded_inputs() -> None:
    context = ContextNode("event", "underhand")
    duplicate = _observation(1, 1, "0.5", 1, context)
    with pytest.raises(ValueError, match="one active revision"):
        train_dependence_artifact(
            (duplicate, duplicate),
            context,
            2,
            DependencePolicy(),
            artifact_id=StableIdentifier("artifact:duplicate"),
            training_evidence_digest="a" * 64,
            active_projection_digest="9" * 64,
            promotion_receipt_digest="b" * 64,
        )


@pytest.mark.parametrize("field_size", [3, 4])
def test_negative_rank_structure_reports_field_adjusted_truth_and_is_label_invariant(
    field_size: int,
) -> None:
    context = ContextNode("underhand")
    observations = tuple(
        _observation(1, index + 1, residual, 1, context)
        for index, residual in enumerate(("3", "-1", "-1", "-1")[:field_size])
    )
    artifact, model = _installed(
        observations,
        context,
        2,
        DependencePolicy(prior_strength="0.25", minimum_pair_count=3),
        StableIdentifier("field:negative-final"),
    )
    field = tuple(
        FieldCompetitorForecast(
            StableIdentifier(f"competitor:n{index}"),
            f"stand:{index + 1}",
            _distribution(40_000 + index * 500),
            index,
        )
        for index in range(field_size)
    )
    draws = generate_joint_draws(
        field, model, installed_artifact=artifact, seed=77, draw_count=4_000
    )
    expected = Decimal(model.rho) * Decimal(field_size + 1) / Decimal(field_size**2)
    assert Decimal(draws.effective_rho) == expected
    correlations = [
        _correlation(left.samples_ms, right.samples_ms)
        for index, left in enumerate(draws.competitors)
        for right in draws.competitors[index + 1 :]
    ]
    assert all(item < Decimal("-0.05") for item in correlations)
    renamed = tuple(
        FieldCompetitorForecast(
            item.competitor_id,
            f"opaque:{index}",
            item.distribution,
            item.crn_index + 10,
        )
        for index, item in enumerate(field)
    )
    replay = generate_joint_draws(
        renamed, model, installed_artifact=artifact, seed=77, draw_count=4_000
    )
    assert tuple(item.samples_ms for item in replay.competitors) == tuple(
        item.samples_ms for item in draws.competitors
    )
    assert replay.common_random_map_digest == draws.common_random_map_digest


def test_dependence_and_joint_receipts_roundtrip_and_reject_forgery() -> None:
    context = ContextNode("underhand")
    artifact, model = _installed(
        (),
        context,
        2,
        DependencePolicy(),
        StableIdentifier("field:roundtrip"),
    )
    assert DependenceModel.from_dict(model.to_dict()) == model
    draws = generate_joint_draws(
        (
            FieldCompetitorForecast(
                StableIdentifier("competitor:a"), "stand:1", _distribution(40_000), 0
            ),
            FieldCompetitorForecast(
                StableIdentifier("competitor:b"), "stand:2", _distribution(45_000), 1
            ),
        ),
        model,
        installed_artifact=artifact,
        seed=4,
        draw_count=32,
    )
    assert JointDraws.from_dict(draws.to_dict()) == draws
    with pytest.raises(ContractError, match="misreported"):
        replace(draws, effective_rho="0.1")
    encoded = draws.to_dict()
    encoded["competitors"][0]["samples_ms"][0] += 1
    with pytest.raises(ContractError, match="sample digest"):
        JointDraws.from_dict(encoded)
    with pytest.raises(ContractError, match="digest"):
        replace(model, model_digest="0" * 64)
    with pytest.raises(ValueError, match="positive"):
        train_dependence_artifact(
            (),
            context,
            0,
            DependencePolicy(),
            artifact_id=StableIdentifier("artifact:invalid-cutoff"),
            training_evidence_digest="a" * 64,
            active_projection_digest="9" * 64,
            promotion_receipt_digest="b" * 64,
        )


def test_joint_contracts_fail_closed_across_all_public_constructor_edges() -> None:
    context = ContextNode("underhand")
    observation = _observation(1, 1, "1", 1, context)
    for changes in (
        {"prior_strength": "0"},
        {"minimum_pair_count": True},
        {"minimum_pair_count": "4"},
        {"minimum_pair_count": 0},
        {"rho_floor": "-1"},
        {"rho_cap": "1"},
        {"version": "bad"},
        {"prior_strength": "1.0"},
    ):
        with pytest.raises(ContractError):
            replace(DependencePolicy(), **changes)
    assert DependencePolicy.from_dict(DependencePolicy().to_dict()) == DependencePolicy()
    with pytest.raises(ContractError, match="fields"):
        DependencePolicy.from_dict({})

    for changes in (
        {"context": object()},
        {"source_sequence": True},
        {"source_sequence": "1"},
        {"source_sequence": 0},
        {"source_revision": True},
        {"source_revision": "1"},
        {"source_revision": 0},
        {"active_projection_digest": "bad"},
        {"standardized_residual": "1.0"},
    ):
        with pytest.raises((ContractError, TypeError)):
            replace(observation, **changes)

    observations = (
        _observation(1, 1, "1", 1, context),
        _observation(1, 2, "1", 1, context),
    )
    learned = train_dependence_artifact(
        observations,
        context,
        2,
        DependencePolicy(minimum_pair_count=1),
        artifact_id=StableIdentifier("artifact:edge-learned"),
        training_evidence_digest="a" * 64,
        active_projection_digest="9" * 64,
        promotion_receipt_digest="b" * 64,
    )
    cold = train_dependence_artifact(
        (),
        ContextNode(),
        2,
        DependencePolicy(),
        artifact_id=StableIdentifier("artifact:edge-cold"),
        training_evidence_digest="a" * 64,
        active_projection_digest="9" * 64,
        promotion_receipt_digest="b" * 64,
    )
    sparse = train_dependence_artifact(
        observations,
        context,
        2,
        DependencePolicy(minimum_pair_count=4),
        artifact_id=StableIdentifier("artifact:edge-sparse"),
        training_evidence_digest="a" * 64,
        active_projection_digest="9" * 64,
        promotion_receipt_digest="b" * 64,
    )
    assert sparse.fallback_code == "shrunk_to_independence"
    assert (
        fit_field_dependence(
            (),
            ContextNode(),
            2,
            DependencePolicy(),
            artifact_id=StableIdentifier("artifact:compatibility"),
            training_evidence_digest="a" * 64,
            active_projection_digest="9" * 64,
            promotion_receipt_digest="b" * 64,
        ).mode
        is DependenceMode.INDEPENDENCE
    )

    for base, changes in (
        (learned, {"version": "bad"}),
        (learned, {"target_context": object()}),
        (learned, {"cutoff_sequence": True}),
        (learned, {"cutoff_sequence": "2"}),
        (learned, {"cutoff_sequence": 0}),
        (learned, {"mode": "bad"}),
        (learned, {"rho": "1"}),
        (learned, {"effective_pair_count": True}),
        (learned, {"effective_pair_count": "1"}),
        (learned, {"effective_pair_count": -1}),
        (learned, {"context_pair_counts": []}),
        (learned, {"shrinkage_path": []}),
        (learned, {"policy": object()}),
        (learned, {"training_evidence_digest": "bad"}),
        (learned, {"fallback_code": "invented"}),
        (cold, {"rho": "0.1"}),
        (cold, {"fallback_code": None}),
        (learned, {"mode": DependenceMode.GAUSSIAN_COPULA}),
        (learned, {"parameters_digest": "0" * 64}),
        (learned, {"artifact_digest": "0" * 64}),
    ):
        with pytest.raises((ContractError, TypeError)):
            replace(base, **changes)

    encoded_artifact = learned.to_dict()
    for mutate in (
        lambda value: value.update(schema_version="bad"),
        lambda value: value.update(parameters="bad"),
        lambda value: value["parameters"].update(schema_version="bad"),
        lambda value: value["parameters"].update(target_context="bad"),
        lambda value: value["parameters"].update(policy="bad"),
        lambda value: value["parameters"].update(mode="bad"),
    ):
        changed = learned.to_dict()
        mutate(changed)
        with pytest.raises(ContractError):
            DependenceArtifact.from_dict(changed)

    model = bind_field_dependence(learned, context, field_id=StableIdentifier("field:edge"))
    cold_model = bind_field_dependence(
        cold, ContextNode(), field_id=StableIdentifier("field:edge-cold")
    )
    with pytest.raises(ContractError, match="installed"):
        bind_field_dependence(object(), context, field_id=StableIdentifier("field:edge"))
    with pytest.raises(ContractError, match="context"):
        bind_field_dependence(
            learned, ContextNode("standing"), field_id=StableIdentifier("field:edge")
        )
    for base, changes in (
        (model, {"target_context": object()}),
        (model, {"cutoff_sequence": True}),
        (model, {"cutoff_sequence": "2"}),
        (model, {"cutoff_sequence": 0}),
        (model, {"mode": "bad"}),
        (model, {"rho": "1"}),
        (model, {"effective_pair_count": True}),
        (model, {"effective_pair_count": "1"}),
        (model, {"effective_pair_count": -1}),
        (model, {"context_pair_counts": []}),
        (model, {"shrinkage_path": []}),
        (model, {"parameters_digest": None}),
        (model, {"fallback_code": "bad"}),
        (cold_model, {"parameters_digest": "a" * 64}),
        (cold_model, {"fallback_code": None}),
        (model, {"mode": DependenceMode.GAUSSIAN_COPULA}),
    ):
        with pytest.raises((ContractError, TypeError)):
            replace(base, **changes)
    for change in (
        {"schema_version": "bad"},
        {"target_context": "bad"},
        {"mode": "bad"},
    ):
        encoded = model.to_dict()
        encoded.update(change)
        with pytest.raises(ContractError):
            DependenceModel.from_dict(encoded)

    forecast = FieldCompetitorForecast(
        StableIdentifier("competitor:edge-a"), "stand:1", _distribution(40_000), 0
    )
    for changes in (
        {"draw_slot": ""},
        {"draw_slot": "x" * 97},
        {"distribution": object()},
        {"crn_index": True},
        {"crn_index": "0"},
        {"crn_index": -1},
    ):
        with pytest.raises((ContractError, TypeError)):
            replace(forecast, **changes)

    second = FieldCompetitorForecast(
        StableIdentifier("competitor:edge-b"), "stand:2", _distribution(45_000), 1
    )
    draws = generate_joint_draws(
        (forecast, second),
        model,
        installed_artifact=learned,
        seed=4,
        draw_count=16,
    )
    row = draws.competitors[0]
    for changes in (
        {"draw_slot": ""},
        {"crn_index": True},
        {"crn_index": "0"},
        {"crn_index": -1},
        {"common_uniforms": []},
        {"samples_ms": []},
        {"common_uniforms": ()},
        {"common_uniforms": ("0",) * len(row.common_uniforms)},
        {"samples_ms": (0,) * len(row.samples_ms)},
    ):
        with pytest.raises((ContractError, TypeError)):
            replace(row, **changes)
    for change in (
        {"extra": True},
        {"common_uniforms": "bad"},
        {"samples_ms": "bad"},
    ):
        encoded = row.to_dict()
        encoded.update(change)
        with pytest.raises(ContractError):
            JointCompetitorDraws.from_dict(encoded)

    duplicate_row = replace(draws.competitors[1], crn_index=0)
    changed_uniform_row = replace(
        row,
        common_uniforms=("0.5", *row.common_uniforms[1:]),
    )
    short_row = replace(
        row,
        common_uniforms=row.common_uniforms[:-1],
        samples_ms=row.samples_ms[:-1],
    )
    for changes in (
        {"inputs": object()},
        {"competitors": []},
        {"competitors": ()},
        {"competitors": (row, duplicate_row)},
        {"competitors": (short_row, draws.competitors[1])},
        {"competitors": (changed_uniform_row, draws.competitors[1])},
        {"algorithm": "bad"},
        {"dependency_version": "bad"},
        {"time_quantum_ms": 2},
        {"common_random_map_digest": "0" * 64},
        {"joint_samples_digest": "0" * 64},
    ):
        with pytest.raises((ContractError, TypeError)):
            replace(draws, **changes)
    for change in (
        {"schema_version": "bad"},
        {"inputs": "bad"},
        {"competitors": "bad"},
    ):
        encoded = draws.to_dict()
        encoded.update(change)
        with pytest.raises(ContractError):
            JointDraws.from_dict(encoded)

    for args in (
        ([], context, 2, DependencePolicy()),
        ((), object(), 2, DependencePolicy()),
        ((), context, 2, object()),
    ):
        with pytest.raises((ContractError, TypeError)):
            train_dependence_artifact(
                *args,
                artifact_id=StableIdentifier("artifact:bad-input"),
                training_evidence_digest="a" * 64,
                active_projection_digest="9" * 64,
                promotion_receipt_digest="b" * 64,
            )
    with pytest.raises(ContractError, match="field"):
        generate_joint_draws([], model, installed_artifact=learned, seed=1, draw_count=2)
    with pytest.raises(ContractError, match="model"):
        generate_joint_draws(
            (forecast,), object(), installed_artifact=learned, seed=1, draw_count=2
        )
    with pytest.raises(ContractError, match="artifact"):
        generate_joint_draws((forecast,), model, installed_artifact=object(), seed=1, draw_count=2)
    with pytest.raises(ContractError, match="context"):
        generate_joint_draws(
            (forecast,), cold_model, installed_artifact=learned, seed=1, draw_count=2
        )
    duplicate_identity = replace(second, competitor_id=forecast.competitor_id)
    duplicate_index = replace(second, crn_index=forecast.crn_index)
    with pytest.raises(ValueError, match="identities"):
        generate_joint_draws(
            (forecast, duplicate_identity),
            model,
            installed_artifact=learned,
            seed=1,
            draw_count=2,
        )
    with pytest.raises(ValueError, match="crn_index"):
        generate_joint_draws(
            (forecast, duplicate_index),
            model,
            installed_artifact=learned,
            seed=1,
            draw_count=2,
        )
    duplicate_artifact, duplicate_model = _installed(
        (), context, 1, DependencePolicy(), StableIdentifier("field:future")
    )
    with pytest.raises(ValueError, match="draw_slot"):
        generate_joint_draws(
            (
                FieldCompetitorForecast(
                    StableIdentifier("competitor:a"),
                    "stand:1",
                    _distribution(40_000),
                    0,
                ),
                FieldCompetitorForecast(
                    StableIdentifier("competitor:b"),
                    "stand:1",
                    _distribution(45_000),
                    1,
                ),
            ),
            duplicate_model,
            installed_artifact=duplicate_artifact,
            seed=1,
            draw_count=8,
        )
