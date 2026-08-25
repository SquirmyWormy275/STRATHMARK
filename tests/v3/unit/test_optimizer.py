from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
from fractions import Fraction
from random import Random

import pytest

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.errors import ContractError
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
    optimize_and_verify_field,
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


def test_optimizer_competitor_reuses_its_validated_sample_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    competitor = _field((40_000,)).competitors[0]
    expected = competitor.samples_digest
    assert expected == canonical_digest(competitor.samples_ms)

    def unexpected_rehash(_value):
        pytest.fail("validated optimizer samples were rehashed")

    monkeypatch.setattr("strathmark.v3.domain.optimizer.canonical_digest", unexpected_rehash)
    assert competitor.samples_digest == expected
    assert competitor.samples_digest == expected


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


@pytest.mark.parametrize("entrant_count", (4, 12))
def test_optimizer_gap_objective_is_exact_for_public_two_billion_ms_boundary(
    entrant_count: int,
) -> None:
    expected = (*((1,) * (entrant_count - 1)), 2_000_000_000)
    field = OptimizationField.create(
        field_id=StableIdentifier(f"field:overflow-{entrant_count}"),
        source_receipt_digest="8" * 64,
        competitors=tuple(
            OptimizationCompetitor(
                StableIdentifier(f"competitor:overflow-{index}"),
                value,
                (value,) * 4096,
                index,
            )
            for index, value in enumerate(expected)
        ),
    )
    marks = (*((183,) * (entrant_count - 1)), 3)
    import strathmark.v3.domain.optimizer as optimizer

    raw = optimizer._evaluate_candidates_impl(field, (marks,), marks, 3, parallel=False, raw=True)[
        marks
    ]
    expected_gap = (entrant_count - 1) * (1_999_819_999**2)
    assert raw[0] == expected_gap
    assert raw[0] >= 0
    assert Decimal(evaluate_sheet(field, marks, marks).gap_fidelity) >= 0


def test_optimizer_authority_is_independent_of_hostile_ambient_decimal_context() -> None:
    field = _field(tuple(100_000 - index * 3_500 for index in range(12)))

    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        low = optimize_and_verify_field(field, ceiling=183)
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_UP
        high = optimize_and_verify_field(field, ceiling=183)

    assert low.receipt == high.receipt
    assert low.to_authority_dict() == high.to_authority_dict()
    with localcontext() as context:
        context.prec = 7
        context.rounding = ROUND_DOWN
        assert VerifiedOptimizerReceipt.from_authority_dict(low.to_authority_dict()) == high


@pytest.mark.parametrize("competitor_count", (13,))
def test_optimizer_authority_preflights_roster_before_constructing_sample_rows(
    competitor_count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    verified = optimize_and_verify_field(_field((20_000, 10_000)), ceiling=30)
    authority = verified.to_authority_dict()
    template = authority["field"]["competitors"][0]
    authority["field"]["competitors"] = [dict(template) for _ in range(competitor_count)]

    def forbidden_constructor(*_args, **_kwargs):
        raise AssertionError("oversized authority must fail before row construction")

    monkeypatch.setattr(optimizer, "OptimizationCompetitor", forbidden_constructor)
    with pytest.raises(ContractError, match="at most 12"):
        VerifiedOptimizerReceipt.from_authority_dict(authority)


def test_optimizer_field_enforces_twelve_entrant_capacity() -> None:
    with pytest.raises(ContractError, match="at most 12"):
        OptimizationField.create(
            field_id=StableIdentifier("field:capacity"),
            source_receipt_digest="9" * 64,
            competitors=tuple(
                OptimizationCompetitor(
                    StableIdentifier(f"competitor:capacity-{index}"),
                    40_000,
                    (40_000,) * 4096,
                    index,
                )
                for index in range(13)
            ),
        )


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


def test_optimizer_failure_returns_canonical_sheet_with_closed_reason(
    monkeypatch,
) -> None:
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


def test_generated_optimizer_authority_is_verified_without_duplicate_replay(
    monkeypatch,
) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    field = _field((60_000, 45_000, 30_000))
    original = optimizer.optimize_field
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "optimize_field", counted)
    verified = optimize_and_verify_field(field, ceiling=183)

    assert calls == 1
    assert verified.receipt == original(field, ceiling=183)
    assert verified.field == field
    with pytest.raises(Exception, match="verifier-owned"):
        VerifiedOptimizerReceipt._from_generated(
            verified.receipt,
            field,
            verified.verification_digest,
            _capability=object(),
        )


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
        ({"seed": -1}, "nonnegative"),
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
            {
                "frontier": (),
                "frontier_digest": canonical_digest([]),
                "fallback_reason": None,
            },
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
    monkeypatch.setattr(optimizer, "OPTIMIZER_IMPLEMENTATION_DIGEST", "f" * 64)
    assert OptimizerReceipt.from_dict(encoded) == receipt
    with pytest.raises(Exception, match="artifact"):
        verify_optimizer_receipt(receipt=receipt, field=field)


def test_public_sheet_validation_and_private_exact_helpers_cover_boundaries() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    field = _field((20_000, 10_000))
    with pytest.raises(ContractError, match="typed field"):
        evaluate_sheet(object(), (3, 13), (3, 13))  # type: ignore[arg-type]
    with pytest.raises(Exception, match="positive"):
        canonical_rounded_sheet((0,), floor=3, ceiling=20)
    with pytest.raises(Exception, match="baseline"):
        evaluate_sheet(field, (3, 13), (3,))
    for marks in ((3,), (2, 13), (13, 3)):
        with pytest.raises(Exception, match="legal"):
            evaluate_sheet(field, marks, (3, 13))
    assert Decimal(evaluate_sheet(field, (3, 183), (3, 13)).baseline_movement) > 0
    for marks in ((3, 184), (3, 2_147_483_647), (3, 10**100), (3, True)):
        with pytest.raises(ContractError, match="bounds|legal"):
            evaluate_sheet(field, marks, (3, 13))
    for baseline in (
        (3, 12),
        (3, 184),
        (3, -(1 << 63)),
        (3, 10**100),
        (3, True),
    ):
        with pytest.raises(ContractError, match="bounds|canonical"):
            evaluate_sheet(field, (3, 13), baseline)
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
    assert beam[4] == optimizer._pareto_frontier_raw(beam[0], 2, 4096)
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


def test_raw_frontier_uses_global_nondominated_membership() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    evaluated = {
        (2,): (2, 0, 0, 0),
        (1,): (1, 1, 0, 0),
        (0,): (0, 2, 0, 0),
    }
    frontier = optimizer._pareto_frontier_raw(evaluated, 12, 4096)
    assert tuple(marks for marks, _objective in frontier) == ((0,),)

    huge = 1 << 65
    evaluated = {
        (2,): (huge + 2, 0, 0, 0),
        (1,): (huge + 1, 1, 0, 0),
        (0,): (huge, 2, 0, 0),
        (3,): (huge, 2, 1, 0),
    }
    frontier = optimizer._pareto_frontier_raw(evaluated, 12, 4096)
    assert tuple(marks for marks, _objective in frontier) == ((0,),)


def test_blocked_raw_frontier_matches_global_full_set_oracle() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    random = Random(20260824)
    entrant_count = 12
    draw_count = 4096
    credit_scale = 27_720
    denominators = (
        entrant_count,
        credit_scale * draw_count * entrant_count,
        draw_count,
        entrant_count,
    )
    for case in range(100):
        evaluated = {
            (case, index): tuple(random.randrange(0, 50_000) for _ in range(4))
            for index in range(300)
        }
        expected = tuple(
            marks
            for marks, values in evaluated.items()
            if not any(
                other_marks != marks and optimizer._dominates_raw(other, values, denominators)
                for other_marks, other in evaluated.items()
            )
        )
        actual = optimizer._pareto_frontier_raw(evaluated, entrant_count, draw_count)
        assert tuple(marks for marks, _objective in actual) == tuple(sorted(expected))


def test_direct_column_dominance_matrix_matches_exact_scalar_relation() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    random = Random(20260826)
    sources = optimizer.np.asarray(
        [tuple(random.randrange(0, 100_000) for _ in range(4)) for _ in range(37)],
        dtype=optimizer.np.int64,
    )
    targets = optimizer.np.asarray(
        [tuple(random.randrange(0, 100_000) for _ in range(4)) for _ in range(41)],
        dtype=optimizer.np.int64,
    )
    denominators = optimizer.np.asarray(
        (12, 27_720 * 4096 * 12, 4096, 12), dtype=optimizer.np.int64
    )
    nonstrict = denominators // 1_000_000_000
    strict = (-denominators - 1) // 1_000_000_000

    actual = optimizer._raw_dominance_matrix(sources, targets, nonstrict, strict)
    expected = optimizer.np.asarray(
        [
            [
                all(
                    int(source[index]) - int(target[index]) <= int(nonstrict[index])
                    for index in range(4)
                )
                and any(
                    int(source[index]) - int(target[index]) <= int(strict[index])
                    for index in range(4)
                )
                for target in targets
            ]
            for source in sources
        ],
        dtype=optimizer.np.bool_,
    )
    assert actual.shape == (37, 41)
    assert optimizer.np.array_equal(actual, expected)


def test_incremental_global_frontier_matches_full_oracle_after_every_batch() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    random = Random(20260825)
    evaluated = {
        (index,): tuple(random.randrange(0, 50_000) for _ in range(4)) for index in range(300)
    }
    index = optimizer._GlobalRawParetoIndex(12, 4096)
    accumulated = {}
    for offset in range(0, 300, 100):
        batch = dict(tuple(evaluated.items())[offset : offset + 100])
        accumulated.update(batch)
        index.add(batch)
        assert index.frontier() == optimizer._pareto_frontier_raw(accumulated, 12, 4096)

    nontransitive = optimizer._GlobalRawParetoIndex(12, 4096)
    nontransitive.add({(2,): (2, 0, 0, 0)})
    nontransitive.add({(1,): (1, 1, 0, 0)})
    nontransitive.add({(0,): (0, 2, 0, 0)})
    assert tuple(marks for marks, _ in nontransitive.frontier()) == ((0,),)


def test_incremental_global_frontier_preserves_extreme_field_gap_offsets() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    origin = 12 * (2_000_000_000**2)
    maximum_legal_field_delta = 8_639_999_995_680_000
    evaluated = {
        (0,): (origin, 3, 7, 4),
        (1,): (origin - maximum_legal_field_delta, 4, 7, 4),
        (2,): (origin - maximum_legal_field_delta + 1, 2, 8, 4),
        (3,): (origin + maximum_legal_field_delta, 1, 9, 5),
    }
    index = optimizer._GlobalRawParetoIndex(12, 4096)
    accumulated = {}

    for marks, values in evaluated.items():
        batch = {marks: values}
        accumulated.update(batch)
        index.add(batch)
        assert index.frontier() == optimizer._pareto_frontier_raw(accumulated, 12, 4096)


def test_raw_normalized_beam_is_exactly_identical_to_materialized_authority() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    evaluated = {
        (3, 3, 5): ((1 << 65) + 10, 4, 12_001, 2),
        (3, 4, 4): ((1 << 65) + 20, 3, 12_000, 2),
        (4, 3, 4): ((1 << 65) + 5, 5, 12_002, 1),
    }
    raw_frontier = tuple(sorted(evaluated.items()))
    materialized = optimizer._materialize_pareto_frontier_raw(evaluated, 12, 4096)

    assert optimizer._normalized_beam_raw(
        raw_frontier, (3, 3, 4), 2, entrant_count=12, draw_count=4096
    ) == optimizer._normalized_beam(materialized, (3, 3, 4), 2)


def test_raw_normalized_beam_matches_materialized_authority_across_large_frontiers() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    random = Random(20260824)
    for case in range(25):
        evaluated = {
            (case, index, 3): (
                (1 << 70) + random.randrange(-(10**12), 10**12),
                random.randrange(0, 27_720 * 4_096 * 12),
                random.randrange(0, 2_000_000_000 * 4_096),
                random.randrange(0, 2_000),
            )
            for index in range(96)
        }
        raw_frontier = tuple(sorted(evaluated.items()))
        materialized = optimizer._materialize_pareto_frontier_raw(evaluated, 12, 4_096)
        baseline = (case, 0, 3)

        assert optimizer._normalized_beam_raw(
            raw_frontier,
            baseline,
            32,
            entrant_count=12,
            draw_count=4_096,
        ) == optimizer._normalized_beam(materialized, baseline, 32)


def test_raw_normalized_beam_uses_exact_shared_integer_scale(monkeypatch) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    frontier = (
        ((3, 4), (11, 7, 23, 5)),
        ((4, 3), (13, 5, 17, 9)),
        ((3, 3), (19, 3, 11, 7)),
    )

    def reject_fraction(*_args, **_kwargs):
        raise AssertionError("raw normalization constructed Fraction values")

    monkeypatch.setattr(optimizer, "Fraction", reject_fraction)
    assert optimizer._normalized_beam_raw(frontier, (3, 3), 2, entrant_count=12, draw_count=4_096)


@pytest.mark.parametrize("entrant_count", (4, 12))
def test_raw_frontier_preserves_exact_semantics_for_gap_values_above_int64(
    entrant_count: int,
) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    random = Random(2_000_000_000 + entrant_count)
    draw_count = 4096
    credit_scale = 12 if entrant_count == 4 else 27_720
    denominators = (
        entrant_count,
        credit_scale * draw_count * entrant_count,
        draw_count,
        entrant_count,
    )
    for case in range(10):
        evaluated = {
            (case, index): (
                random.randrange(0, entrant_count * (2_000_000_000**2) + 1),
                random.randrange(0, 50_000),
                random.randrange(0, 50_000),
                random.randrange(0, 50_000),
            )
            for index in range(300)
        }
        expected = tuple(
            marks
            for marks, values in evaluated.items()
            if not any(
                other_marks != marks and optimizer._dominates_raw(other, values, denominators)
                for other_marks, other in evaluated.items()
            )
        )
        actual = optimizer._pareto_frontier_raw(evaluated, entrant_count, draw_count)
        assert tuple(marks for marks, _objective in actual) == tuple(sorted(expected))


@pytest.mark.parametrize(("entrant_count", "candidate_count"), ((7, 255), (12, 256), (12, 257)))
def test_raw_frontier_block_boundaries_match_global_full_set_oracle(
    entrant_count: int,
    candidate_count: int,
) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    random = Random(entrant_count * 10_000 + candidate_count)
    evaluated = {
        (index,): tuple(random.randrange(0, 100_000) for _ in range(4))
        for index in range(candidate_count)
    }
    credit_scale = 420 if entrant_count == 7 else 27_720
    denominators = (
        entrant_count,
        credit_scale * 4096 * entrant_count,
        4096,
        entrant_count,
    )
    expected = tuple(
        marks
        for marks, values in evaluated.items()
        if not any(
            other_marks != marks and optimizer._dominates_raw(other, values, denominators)
            for other_marks, other in evaluated.items()
        )
    )
    actual = optimizer._pareto_frontier_raw(evaluated, entrant_count, 4096)
    assert tuple(marks for marks, _objective in actual) == tuple(sorted(expected))


def test_vectorized_unique_and_tied_winners_reuse_boolean_tie_matrix(
    monkeypatch,
) -> None:
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


def test_bitset_winner_credits_match_exact_scalar_oracle_for_unique_tied_and_extreme_draws() -> (
    None
):
    import strathmark.v3.domain.optimizer as optimizer

    random = Random(991_827)
    credit_scale = 420
    draw_count = 257
    entrant_count = 7
    samples = optimizer.np.asarray(
        [
            [
                (
                    2_000_000_000 - entrant * 11
                    if draw == 0
                    else 40_000
                    if draw % 2 == 0
                    else random.randrange(1, 2_000_000_001)
                )
                for entrant in range(entrant_count)
            ]
            for draw in range(draw_count)
        ],
        dtype=optimizer.np.int64,
    )
    delay_rows = optimizer.np.asarray(
        [
            [random.randrange(0, 181) * 1000 for _entrant in range(entrant_count)]
            for _candidate in range(23)
        ]
        + [[0] * entrant_count],
        dtype=optimizer.np.int64,
    )

    expected = []
    for delays in delay_rows:
        credits = [0] * entrant_count
        for row in samples:
            finishes = [int(value) + int(delay) for value, delay in zip(row, delays)]
            minimum = min(finishes)
            winners = [index for index, value in enumerate(finishes) if value == minimum]
            for winner in winners:
                credits[winner] += credit_scale // len(winners)
        expected.append(credits)

    assert (
        optimizer._winner_credits_bitset(
            samples,
            delay_rows,
            entrant_count=entrant_count,
            draw_count=draw_count,
            credit_scale=credit_scale,
        ).tolist()
        == expected
    )


def test_bit_sliced_tie_credit_matches_exact_shared_winner_oracle() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    winner_sets = (
        (0,),
        (0, 1),
        (1, 2, 3),
        tuple(range(12)),
    )
    masks = tuple(
        sum(1 << draw for draw, winners in enumerate(winner_sets) if entrant in winners)
        for entrant in range(12)
    )
    expected = [0] * 12
    for winners in winner_sets:
        for winner in winners:
            expected[winner] += 27_720 // len(winners)

    assert (
        optimizer._credits_from_winner_masks(
            masks, draw_count=len(winner_sets), credit_scale=27_720
        )
        == expected
    )


def test_streamed_finish_extremes_match_full_tensor_without_allocating_it() -> None:
    import strathmark.v3.domain.optimizer as optimizer

    random = Random(76_421)
    samples = optimizer.np.asarray(
        [
            [
                (
                    2_000_000_000 - entrant
                    if draw == 0
                    else 40_000
                    if draw % 7 == 0
                    else random.randrange(1, 2_000_000_001)
                )
                for entrant in range(12)
            ]
            for draw in range(257)
        ],
        dtype=optimizer.np.int64,
    )
    delays = optimizer.np.asarray(
        [[random.randrange(0, 181) * 1000 for _entrant in range(12)] for _candidate in range(41)],
        dtype=optimizer.np.int64,
    )
    full = samples[optimizer.np.newaxis, :, :] + delays[:, optimizer.np.newaxis, :]
    expected_minima = optimizer.np.min(full, axis=2)
    expected_spreads = optimizer.np.sum(
        optimizer.np.max(full, axis=2) - expected_minima,
        axis=1,
        dtype=optimizer.np.int64,
    )

    minima, spreads = optimizer._streamed_finish_extremes(samples, delays)

    assert optimizer.np.array_equal(minima, expected_minima)
    assert optimizer.np.array_equal(spreads, expected_spreads)


def test_candidate_evaluator_reuses_exact_winner_masks_across_beam_rounds(
    monkeypatch,
) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    monkeypatch.setattr(optimizer, "_NATIVE_OPTIMIZER_KERNEL", None)
    field = _field((90_000, 78_000, 63_000, 40_000))
    baseline = (3, 15, 30, 53)
    context = optimizer._compile_evaluation_context(field, baseline)
    candidates = tuple(
        (
            3 + index % 3,
            15 + (index // 3) % 3,
            30 + (index // 9) % 3,
            53 + (index // 27) % 2,
        )
        for index in range(40)
    )
    original = optimizer.np.packbits
    calls = 0

    def tracked(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer.np, "packbits", tracked)
    first = optimizer._evaluate_candidates_impl(
        field,
        candidates,
        baseline,
        3,
        parallel=False,
        raw=True,
        _context=context,
    )
    first_calls = calls
    second = optimizer._evaluate_candidates_impl(
        field,
        candidates,
        baseline,
        3,
        parallel=False,
        raw=True,
        _context=context,
    )

    assert first == second
    assert first_calls > 0
    assert calls == first_calls


def test_candidate_evaluator_keeps_small_batches_on_vectorized_winner_path(
    monkeypatch,
) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    def forbidden(*_args, **_kwargs):
        raise AssertionError("small batches must not pay bitset cache setup cost")

    monkeypatch.setattr(optimizer, "_winner_credits_bitset", forbidden)
    field = _field((90_000, 78_000, 63_000, 40_000))
    baseline = (3, 15, 30, 53)
    assert optimizer._evaluate_candidates_impl(
        field,
        ((3, 15, 30, 53), (4, 15, 30, 53)),
        baseline,
        3,
        parallel=False,
        raw=True,
    )


@pytest.mark.parametrize("entrant_count", (3, 7, 11, 12))
def test_tie_probabilities_are_exactly_apportioned_at_a_fixed_decimal_quantum(
    entrant_count: int,
) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    shared_samples = tuple(40_000 for _ in range(4096))
    field = OptimizationField.create(
        field_id=StableIdentifier(f"field:tied-{entrant_count}"),
        source_receipt_digest="e" * 64,
        competitors=tuple(
            OptimizationCompetitor(
                StableIdentifier(f"competitor:{index}"),
                40_000,
                shared_samples,
                index,
            )
            for index in range(entrant_count)
        ),
    )

    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        low = optimizer._win_probabilities(field, (3,) * entrant_count)
    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_UP
        high = optimizer._win_probabilities(field, (3,) * entrant_count)

    assert low == high
    assert sum((Fraction(value) for value in low), Fraction(0)) == 1
    quantum = 10**60
    apportioned = tuple(int(Fraction(value) * quantum) for value in low)
    larger_count = quantum % entrant_count
    assert apportioned == (
        *((quantum // entrant_count + 1,) * larger_count),
        *((quantum // entrant_count,) * (entrant_count - larger_count)),
    )


def test_parallel_candidate_evaluation_compiles_field_arrays_once(monkeypatch) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    original = optimizer._compile_evaluation_context
    calls = 0

    def tracked(field, baseline):
        nonlocal calls
        calls += 1
        return original(field, baseline)

    monkeypatch.setattr(optimizer, "_compile_evaluation_context", tracked)
    field = _field((40_000, 40_000))
    candidates = tuple((3, 3) for _index in range(2_049))
    result = optimizer._evaluate_candidates_impl(field, candidates, (3, 3), 3, raw=True)

    assert calls == 1
    assert set(result) == {(3, 3)}


def test_candidate_evaluation_is_exact_across_bounded_batch_sizes(monkeypatch) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    field = _field((40_000, 20_000))
    candidates = tuple((3 + index % 28, 30 - index % 28) for index in range(300))
    reference = optimizer._evaluate_candidates_impl(
        field, candidates, (3, 23), 3, parallel=False, raw=True
    )

    for batch_size in (16, 32, 64, 96, 192, 256):
        monkeypatch.setattr(optimizer, "_EVALUATION_BATCH_SIZE", batch_size)
        assert (
            optimizer._evaluate_candidates_impl(
                field, candidates, (3, 23), 3, parallel=False, raw=True
            )
            == reference
        )


def test_beam_search_compiles_field_arrays_once_for_all_rounds(monkeypatch) -> None:
    import strathmark.v3.domain.optimizer as optimizer

    original = optimizer._compile_evaluation_context
    calls = 0

    def tracked(field, baseline):
        nonlocal calls
        calls += 1
        return original(field, baseline)

    monkeypatch.setattr(optimizer, "_compile_evaluation_context", tracked)
    field = _field((40_000, 20_000))
    result = optimizer._beam_search(
        field,
        (40_000, 20_000),
        (3, 23),
        3,
        30,
        optimizer.DEFAULT_OPTIMIZER_POLICY,
    )

    assert result[2] > 0
    assert calls == 1
