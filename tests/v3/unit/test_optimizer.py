from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.optimizer import (
    FrontierCandidate,
    ObjectiveVector,
    OptimizationCompetitor,
    OptimizationField,
    OptimizerFallback,
    OptimizerPolicy,
    OptimizerReceipt,
    OptimizerWorkBudget,
    VerifiedOptimizerReceipt,
    canonical_rounded_sheet,
    evaluate_sheet,
    optimize_field,
    verify_optimizer_receipt,
)


def _samples(center: int, *, width: int = 500) -> tuple[int, ...]:
    return tuple(center + ((index * 7919) % (2 * width + 1)) - width for index in range(4096))


def _field(
    medians: tuple[int, ...],
    *,
    widths: tuple[int, ...] | None = None,
    receipt: str = "a" * 64,
) -> OptimizationField:
    widths = widths or tuple(500 for _ in medians)
    competitors = tuple(
        OptimizationCompetitor(
            StableIdentifier(f"competitor:{index}"),
            median,
            _samples(median, width=width),
            index,
        )
        for index, (median, width) in enumerate(zip(medians, widths, strict=True))
    )
    return OptimizationField.create(
        field_id=StableIdentifier("field:test"),
        source_receipt_digest=receipt,
        competitors=competitors,
    )


def test_v2_optimizer_golden_remains_unchanged() -> None:
    from strathmark.mark_optimizer import legacy_rounded_gap_marks

    assert legacy_rounded_gap_marks([60.0, 45.0, 30.0], ceiling=183) == (3, 18, 33)
    assert legacy_rounded_gap_marks([40.5, 35.0], ceiling=183) == (3, 9)


def test_continuous_ideal_and_canonical_half_even_rounding_are_integer_ms_exact() -> None:
    ideal, baseline = canonical_rounded_sheet((40_500, 35_000), floor=3, ceiling=183)
    assert ideal == ("3", "8.5")
    assert baseline == (3, 9)

    ideal, baseline = canonical_rounded_sheet((39_500, 35_000), floor=3, ceiling=183)
    assert ideal == ("3", "7.5")
    assert baseline == (3, 7)


def test_empty_and_single_fields_return_typed_auditable_receipts() -> None:
    empty = OptimizationField.create(
        field_id=StableIdentifier("field:empty"),
        source_receipt_digest="b" * 64,
        competitors=(),
    )
    empty_receipt = optimize_field(empty, ceiling=183)
    assert empty_receipt.selected_marks == ()
    assert empty_receipt.fallback_reason is OptimizerFallback.EMPTY_FIELD

    single = optimize_field(_field((35_000,)), ceiling=183)
    assert single.selected_marks == (3,)
    assert single.rounded_baseline == (3,)
    assert single.fallback_reason is None
    assert single.sample_count == 4096
    assert single.seed == int(("a" * 64)[:16], 16) & ((1 << 63) - 1)
    assert OptimizerReceipt.from_dict(single.to_dict()) == single


def test_optimizer_receipt_exposes_full_frontier_and_consequences() -> None:
    receipt = optimize_field(_field((60_000, 45_000, 30_000)), ceiling=80)

    assert receipt.continuous_ideal == ("3", "18", "33")
    assert receipt.rounded_baseline == (3, 18, 33)
    assert len(receipt.frontier) >= 1
    assert receipt.selected_marks in tuple(item.marks for item in receipt.frontier)
    assert receipt.deltas == tuple(
        selected - baseline
        for selected, baseline in zip(receipt.selected_marks, receipt.rounded_baseline, strict=True)
    )
    assert receipt.frontier_digest == canonical_digest(
        [candidate.to_dict() for candidate in receipt.frontier]
    )
    assert receipt.work_budget.sample_count == 4096
    assert receipt.work_budget.small_field_radius_seconds == 3
    assert receipt.gap_fidelity_cost == receipt.selected_objectives.gap_fidelity
    from decimal import Decimal

    assert Decimal(receipt.fairness_gain) == (
        Decimal(receipt.baseline_objectives.win_probability_parity)
        - Decimal(receipt.selected_objectives.win_probability_parity)
    )
    assert Decimal(receipt.spread_change_ms) == (
        Decimal(receipt.selected_objectives.expected_finish_spread_ms)
        - Decimal(receipt.baseline_objectives.expected_finish_spread_ms)
    )


def test_ceiling_pressure_large_gap_and_exact_ties_stay_legal_and_stable() -> None:
    result = optimize_field(_field((90_000, 30_000, 30_000, 10_000)), ceiling=20)
    assert result.selected_marks[0] == 3
    assert all(3 <= mark <= 20 for mark in result.selected_marks)
    assert result.selected_marks == tuple(sorted(result.selected_marks))
    assert result.selected_marks[1] == result.selected_marks[2]


def test_invalid_inputs_and_receipt_tampering_fail_closed() -> None:
    with pytest.raises(Exception, match="4096"):
        OptimizationCompetitor(StableIdentifier("competitor:short"), 10_000, (10_000,), 0)
    with pytest.raises(Exception, match="duplicate"):
        OptimizationField.create(
            field_id=StableIdentifier("field:duplicate"),
            source_receipt_digest="c" * 64,
            competitors=(
                OptimizationCompetitor(
                    StableIdentifier("competitor:a"), 10_000, _samples(10_000), 0
                ),
                OptimizationCompetitor(
                    StableIdentifier("competitor:a"), 11_000, _samples(11_000), 1
                ),
            ),
        )
    good = optimize_field(_field((20_000, 10_000)), ceiling=30)
    with pytest.raises(Exception, match="digest"):
        OptimizerReceipt.from_dict({**good.to_dict(), "receipt_digest": "0" * 64})


def test_optimizer_failure_returns_canonical_sheet_with_closed_reason(monkeypatch) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    field = _field((50_000, 40_000))
    monkeypatch.setattr(optimizer, "_evaluate_candidates", lambda *_args, **_kwargs: 1 / 0)
    result = optimize_field(field, ceiling=183)
    assert result.selected_marks == (3, 13)
    assert result.fallback_reason is OptimizerFallback.OPTIMIZER_FAILURE
    assert result.frontier == ()
    assert result.selected_objectives == result.baseline_objectives


@pytest.mark.parametrize(
    "objectives",
    (
        ObjectiveVector("0", "0", "0", "0"),
        ObjectiveVector("1.25", "0.1", "22.5", "0.5"),
    ),
)
def test_objective_vector_is_canonical_and_round_trips(objectives) -> None:
    assert ObjectiveVector.from_dict(objectives.to_dict()) == objectives
    with pytest.raises(Exception):
        replace(objectives, gap_fidelity="NaN")


def test_verified_receipt_requires_exact_input_bound_deterministic_replay() -> None:
    field = _field((45_000, 35_000))
    receipt = optimize_field(field, ceiling=60)
    verified = verify_optimizer_receipt(receipt=receipt, field=field)
    assert verified.receipt is receipt

    substituted = _field((46_000, 35_000))
    with pytest.raises(Exception, match="replay"):
        verify_optimizer_receipt(receipt=receipt, field=substituted)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    (
        ("selected_marks", [20, 3], "deltas|illegal"),
        ("rounded_baseline", [3, 3], "baseline"),
        ("optimizer_version", "forged", "algorithm"),
        ("ceiling", 2, "bounds"),
        ("fallback_reason", "optimizer_failure", "fallback"),
    ),
)
def test_self_consistent_receipt_digest_cannot_forge_local_authority(key, value, message) -> None:
    receipt = optimize_field(_field((45_000, 35_000)), ceiling=60)
    encoded = receipt.to_dict()
    encoded[key] = value
    content = dict(encoded)
    content.pop("receipt_digest")
    encoded["receipt_digest"] = canonical_digest(content)
    with pytest.raises(Exception, match=message):
        OptimizerReceipt.from_dict(encoded)


def test_frozen_optimizer_value_objects_reject_every_noncanonical_shape() -> None:
    good_competitor = _field((20_000,)).competitors[0]
    with pytest.raises(Exception, match="frozen"):
        OptimizerPolicy(sample_count=1)
    for changes, message in (
        ({"samples_ms": (0,) * 4096}, "positive"),
        ({"expected_time_ms": 2_000_000_001}, "int32"),
        ({"upstream_index": -1}, "nonnegative"),
    ):
        with pytest.raises(Exception, match=message):
            replace(good_competitor, **changes)

    field = _field((20_000, 10_000))
    duplicate = replace(field.competitors[1], competitor_id=field.competitors[0].competitor_id)
    for changes, message in (
        ({"seed": 0}, "seed"),
        ({"competitors": [*field.competitors]}, "immutable typed"),
        ({"competitors": (field.competitors[0], duplicate)}, "duplicate"),
        (
            {
                "competitors": (
                    field.competitors[0],
                    replace(field.competitors[1], upstream_index=3),
                )
            },
            "contiguous",
        ),
        ({"sample_matrix_digest": "0" * 64}, "sample matrix"),
        ({"input_digest": "0" * 64}, "input digest"),
    ):
        with pytest.raises(Exception, match=message):
            replace(field, **changes)

    with pytest.raises(Exception, match="nonnegative"):
        ObjectiveVector("-1", "0", "0", "0")
    with pytest.raises(Exception, match="typed"):
        ObjectiveVector("0", "0", "0", "0").dominates(object())
    with pytest.raises(Exception, match="fields"):
        ObjectiveVector.from_dict({"gap_fidelity": "0"})

    objective = ObjectiveVector("0", "0", "0", "0")
    candidate = FrontierCandidate((3,), objective, ("0", "0", "0", "0"), "0")
    for changes, message in (
        ({"marks": [3]}, "immutable"),
        ({"objectives": object()}, "typed"),
        ({"normalized_objectives": ("0",)}, "four"),
        ({"normalized_objectives": ("2", "0", "0", "0")}, "inside"),
    ):
        with pytest.raises(Exception, match=message):
            replace(candidate, **changes)
    with pytest.raises(Exception, match="fields"):
        FrontierCandidate.from_dict({"marks": [3]})

    budget = OptimizerWorkBudget(4096, 3, 512, 0, 0, 1, 1, 1)
    for changes, message in (
        ({"candidates_generated": -1}, "nonnegative"),
        ({"sample_count": 1}, "frozen"),
        ({"parallel_workers": 1}, "parallel"),
        ({"expansion_rounds": 1}, "rounds"),
        ({"candidates_evaluated": 2}, "evaluations"),
    ):
        with pytest.raises(Exception, match=message):
            replace(budget, **changes)
    with pytest.raises(Exception, match="fields"):
        OptimizerWorkBudget.from_dict({"sample_count": 4096})


def test_receipt_and_verification_local_invariants_fail_before_authority_replay(
    monkeypatch,
) -> None:
    field = _field((45_000, 35_000))
    receipt = optimize_field(field, ceiling=60)
    bad_frontier = (
        FrontierCandidate(
            (20, 3),
            receipt.frontier[0].objectives,
            receipt.frontier[0].normalized_objectives,
            receipt.frontier[0].knee_distance,
        ),
    )
    invalids = (
        ({"frontier": [*receipt.frontier]}, "immutable"),
        ({"frontier_digest": "0" * 64}, "frontier digest"),
        ({"competitor_ids": (receipt.competitor_ids[0],) * 2}, "roster"),
        ({"expected_times_ms": (45_000,)}, "expected-time"),
        ({"rounded_baseline": (3,)}, "arrays|baseline"),
        ({"selected_marks": (3,), "deltas": (0,)}, "arrays"),
        ({"deltas": (1, 0)}, "deltas"),
        ({"selected_marks": (3, 14), "deltas": (0, 1)}, "absent"),
        (
            {
                "frontier": bad_frontier,
                "frontier_digest": canonical_digest([bad_frontier[0].to_dict()]),
                "selected_marks": (20, 3),
                "deltas": (17, -10),
            },
            "illegal",
        ),
        (
            {
                "frontier": (*receipt.frontier, bad_frontier[0]),
                "frontier_digest": canonical_digest(
                    [item.to_dict() for item in (*receipt.frontier, bad_frontier[0])]
                ),
            },
            "illegal",
        ),
        ({"selected_objectives": object()}, "objectives"),
        ({"gap_fidelity_cost": "99"}, "gap-fidelity"),
        ({"fairness_gain": "99"}, "consequence"),
        ({"sample_count": 1}, "sampling"),
        ({"work_budget": object()}, "work budget"),
        ({"fallback_reason": "bogus"}, "fallback"),
        (
            {"frontier": (), "frontier_digest": canonical_digest([]), "fallback_reason": None},
            "frontier/strategy",
        ),
    )
    for changes, message in invalids:
        with pytest.raises(Exception, match=message):
            replace(receipt, **changes)

    empty = optimize_field(
        OptimizationField.create(
            field_id=StableIdentifier("field:empty"),
            source_receipt_digest="b" * 64,
            competitors=(),
        ),
        ceiling=60,
    )
    with pytest.raises(Exception, match="empty-field"):
        replace(empty, search_strategy="canonical_fallback")
    import strathmark.v3.domain.optimizer as optimizer

    original_evaluate_candidates = optimizer._evaluate_candidates
    monkeypatch.setattr(optimizer, "_evaluate_candidates", lambda *_args, **_kwargs: 1 / 0)
    failure = optimize_field(field, ceiling=60)
    monkeypatch.setattr(optimizer, "_evaluate_candidates", original_evaluate_candidates)
    with pytest.raises(Exception, match="canonical baseline"):
        replace(failure, selected_marks=(3, 14), deltas=(0, 1))
    with pytest.raises(Exception, match="frontier or completed work"):
        replace(failure, frontier=receipt.frontier, frontier_digest=receipt.frontier_digest)
    with pytest.raises(Exception, match="frontier"):
        replace(failure, fallback_reason=OptimizerFallback.NO_VALID_IMPROVEMENT)

    with pytest.raises(Exception, match="schema"):
        OptimizerReceipt.from_dict({"schema_version": "wrong"})
    with pytest.raises(Exception, match="typed"):
        verify_optimizer_receipt(receipt=object(), field=field)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="typed"):
        VerifiedOptimizerReceipt(receipt, object(), "0" * 64)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="verification digest"):
        VerifiedOptimizerReceipt(receipt, field, "0" * 64)

    encoded = receipt.to_dict()
    monkeypatch.setattr(optimizer, "implementation_artifact_digest", lambda: "f" * 64)
    assert OptimizerReceipt.from_dict(encoded) == receipt
    with pytest.raises(Exception, match="artifact"):
        verify_optimizer_receipt(receipt=receipt, field=field)


def test_public_sheet_validation_and_private_exact_helpers_cover_boundaries() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    field = _field((20_000, 10_000))
    with pytest.raises(Exception, match="positive"):
        canonical_rounded_sheet((0,), floor=3, ceiling=20)
    with pytest.raises(Exception, match="baseline"):
        evaluate_sheet(field, (3, 13), (3,))
    for marks in ((3,), (2, 13), (13, 3)):
        with pytest.raises(Exception, match="legal"):
            evaluate_sheet(field, marks, (3, 13))
    with pytest.raises(Exception, match="bounds"):
        canonical_rounded_sheet((10_000,), floor=2, ceiling=20)
    assert optimizer._evaluate_candidates_impl(field, (), (3, 13), 3) == {}
    assert optimizer._median_absolute((-3, 1, 2)) == 2
    assert optimizer._median_absolute((-3, 1)) == 2
    with pytest.raises(Exception, match="positive"):
        optimizer._positive_int(True, "value")
    with pytest.raises(Exception, match="digest"):
        optimizer._digest("ABC", "digest")
    assert not optimizer._is_legal((20_000, 10_000), (3, 99), 3, 20)
    assert optimizer._OptimizerAbort(OptimizerFallback.RANK_INVALID).reason is (
        OptimizerFallback.RANK_INVALID
    )


def test_optimizer_abort_parallel_and_chim_defensive_paths(monkeypatch) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    field = _field((20_000, 10_000))
    original = optimizer._pareto_frontier_raw
    monkeypatch.setattr(optimizer, "_pareto_frontier_raw", lambda *_args: ())
    receipt = optimize_field(field, ceiling=30)
    assert receipt.fallback_reason is OptimizerFallback.EMPTY_FRONTIER
    monkeypatch.setattr(optimizer, "_pareto_frontier_raw", original)
    with pytest.raises(Exception, match="typed"):
        optimize_field(object(), ceiling=30)  # type: ignore[arg-type]

    repeated = ((3, 13),) * 2049
    parallel = optimizer._evaluate_candidates_impl(field, repeated, (3, 13), 3, raw=True)
    serial = optimizer._evaluate_candidates_impl(
        field, ((3, 13),), (3, 13), 3, parallel=False, raw=True
    )
    assert parallel == serial

    from types import SimpleNamespace

    beam = optimizer._beam_search(
        field,
        (20_000, 10_000),
        (3, 13),
        3,
        30,
        SimpleNamespace(maximum_expansion_rounds=1, beam_width=0),
    )
    assert beam[2] == 0
    assert (
        optimizer._beam_search(
            field,
            (20_000, 10_000),
            (3, 13),
            3,
            30,
            SimpleNamespace(maximum_expansion_rounds=0, beam_width=512),
        )[2]
        == 0
    )
    tied = _field((10_000,) * 7)
    assert (
        optimizer._beam_search(
            tied,
            (10_000,) * 7,
            (3,) * 7,
            3,
            3,
            optimizer.DEFAULT_OPTIMIZER_POLICY,
        )[2]
        == 0
    )

    rows = (
        ((3, 10), ObjectiveVector("0", "1", "1", "1")),
        ((3, 11), ObjectiveVector("1", "0", "1", "1")),
        ((3, 12), ObjectiveVector("1", "1", "0", "1")),
        ((3, 13), ObjectiveVector("1", "1", "1", "0")),
    )
    original_nullspace = optimizer._decimal_svd_nullspace
    monkeypatch.setattr(optimizer, "_decimal_svd_nullspace", lambda *_args: ())
    with pytest.raises(optimizer._OptimizerAbort) as exc:
        optimizer._select_chim(rows, (3, 10), ObjectiveVector("2", "2", "2", "2"))
    assert exc.value.reason is OptimizerFallback.RANK_INVALID
    monkeypatch.setattr(optimizer, "_decimal_svd_nullspace", original_nullspace)
    original_projection = optimizer._project_onto_basis
    monkeypatch.setattr(
        optimizer,
        "_project_onto_basis",
        lambda _vector, _basis: (Decimal(1), Decimal(1), Decimal(1), Decimal(1)),
    )
    optimizer._select_chim(rows, (3, 10), ObjectiveVector("2", "2", "2", "2"))
    monkeypatch.setattr(optimizer, "_project_onto_basis", original_projection)
    identity = tuple(
        tuple(Decimal(1) if row == column else Decimal(0) for column in range(4))
        for row in range(4)
    )
    assert optimizer._decimal_svd_nullspace(identity, 4) == ()
    assert optimizer._project_onto_basis((Decimal(1), Decimal(0)), ((Decimal(0), Decimal(0)),)) == (
        Decimal(0),
        Decimal(0),
    )


def test_vectorized_unique_and_tied_winners_reuse_boolean_tie_matrix(monkeypatch) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    unique = _field((20_000, 10_000), widths=(500, 700))
    same = tuple(40_000 + (index % 11) for index in range(4096))
    tied = OptimizationField.create(
        field_id=StableIdentifier("field:tied-vector"),
        source_receipt_digest="e" * 64,
        competitors=(
            OptimizationCompetitor(StableIdentifier("competitor:a"), 40_000, same, 0),
            OptimizationCompetitor(StableIdentifier("competitor:b"), 40_000, same, 1),
        ),
    )

    def forbidden_argmin(*_args, **_kwargs):
        raise AssertionError("integer argmin duplicates the canonical tie matrix")

    monkeypatch.setattr(optimizer.np, "argmin", forbidden_argmin)
    unique_raw = optimizer._evaluate_candidates_impl(
        unique, ((3, 13),), (3, 13), 3, parallel=False, raw=True
    )
    tied_raw = optimizer._evaluate_candidates_impl(
        tied, ((3, 3),), (3, 3), 3, parallel=False, raw=True
    )
    assert optimizer._materialize_raw_objective(
        unique_raw[(3, 13)], 2, 16_384, 4096
    ) == evaluate_sheet(unique, (3, 13), (3, 13))
    assert optimizer._materialize_raw_objective(
        tied_raw[(3, 3)], 2, 16_384, 4096
    ) == ObjectiveVector("0", "0", "0", "0")
