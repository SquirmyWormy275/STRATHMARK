from __future__ import annotations

import json
from itertools import product
from math import lcm
from pathlib import Path

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain import optimizer as optimizer_module
from strathmark.v3.domain.optimizer import (
    DEFAULT_OPTIMIZER_POLICY,
    ObjectiveVector,
    OptimizationCompetitor,
    OptimizationField,
    canonical_rounded_sheet,
    evaluate_sheet,
    implementation_artifact_digest,
    optimize_field,
)


def _field(medians: tuple[int, ...], digest: str = "d" * 64) -> OptimizationField:
    rows = []
    for competitor_index, median in enumerate(medians):
        samples = tuple(
            max(1, median + ((draw * (17 + competitor_index * 2)) % 1001) - 500)
            for draw in range(4096)
        )
        rows.append(
            OptimizationCompetitor(
                StableIdentifier(f"competitor:{competitor_index}"),
                median,
                samples,
                competitor_index,
            )
        )
    return OptimizationField.create(
        field_id=StableIdentifier("field:properties"),
        source_receipt_digest=digest,
        competitors=tuple(rows),
    )


def _legal(medians: tuple[int, ...], marks: tuple[int, ...], ceiling: int) -> bool:
    if not marks:
        return True
    if min(marks) != 3 or any(mark < 3 or mark > ceiling for mark in marks):
        return False
    ordered = sorted(range(len(medians)), key=lambda index: (-medians[index], index))
    ordered_marks = tuple(marks[index] for index in ordered)
    if ordered_marks != tuple(sorted(ordered_marks)):
        return False
    return all(
        medians[left] != medians[right] or marks[left] == marks[right]
        for left in range(len(medians))
        for right in range(left + 1, len(medians))
    )


def test_determinism_invariants_and_input_order_identity() -> None:
    assert DEFAULT_OPTIMIZER_POLICY.parallel_workers == 8
    field = _field((62_000, 48_000, 31_000))
    first = optimize_field(field, ceiling=80)
    second = optimize_field(field, ceiling=80)
    assert first == second
    assert _legal((62_000, 48_000, 31_000), first.selected_marks, 80)
    assert first.work_budget.candidates_evaluated <= first.work_budget.candidate_limit


def test_small_field_frontier_matches_independent_exhaustive_oracle() -> None:
    medians = (40_000, 37_000, 34_000)
    field = _field(medians)
    receipt = optimize_field(field, ceiling=10)
    _ideal, baseline = canonical_rounded_sheet(medians, floor=3, ceiling=10)
    ranges = tuple(range(max(3, mark - 3), min(10, mark + 3) + 1) for mark in baseline)
    candidates = tuple(marks for marks in product(*ranges) if _legal(medians, marks, 10))
    objectives = {marks: evaluate_sheet(field, marks, baseline) for marks in candidates}
    oracle = tuple(
        sorted(
            (
                marks
                for marks in candidates
                if not any(
                    other != marks and objectives[other].dominates(objectives[marks])
                    for other in candidates
                )
            )
        )
    )
    assert tuple(item.marks for item in receipt.frontier) == oracle


def test_frontier_is_nondominated_and_baseline_is_not_better_than_selected() -> None:
    receipt = optimize_field(_field((51_000, 46_000, 35_000)), ceiling=30)
    for left in receipt.frontier:
        assert not any(
            right.marks != left.marks and right.objectives.dominates(left.objectives)
            for right in receipt.frontier
        )
    assert not receipt.baseline_objectives.dominates(receipt.selected_objectives)


def test_large_field_uses_exact_deterministic_beam_work_budget() -> None:
    medians = tuple(100_000 - index * 3_500 for index in range(12))
    receipt = optimize_field(_field(medians, "e" * 64), ceiling=90)
    assert receipt.search_strategy == "deterministic_beam_v1"
    assert receipt.work_budget.beam_width == 512
    assert receipt.work_budget.expansion_round_limit == 96
    assert receipt.work_budget.expansion_rounds <= 96
    assert receipt.work_budget.candidates_evaluated <= receipt.work_budget.candidate_limit
    assert _legal(medians, receipt.selected_marks, 90)


def test_exact_finish_ties_split_win_credit_and_are_permutation_equivariant() -> None:
    same = tuple(40_000 + (index % 11) for index in range(4096))
    field = OptimizationField.create(
        field_id=StableIdentifier("field:properties"),
        source_receipt_digest="d" * 64,
        competitors=(
            OptimizationCompetitor(StableIdentifier("competitor:0"), 40_000, same, 0),
            OptimizationCompetitor(StableIdentifier("competitor:1"), 40_000, same, 1),
        ),
    )
    objective = evaluate_sheet(field, (3, 3), (3, 3))
    assert objective.win_probability_parity == "0"

    renamed = OptimizationField.create(
        field_id=StableIdentifier("field:properties"),
        source_receipt_digest="d" * 64,
        competitors=tuple(
            OptimizationCompetitor(
                StableIdentifier(f"competitor:opaque-{index}"),
                item.expected_time_ms,
                item.samples_ms,
                index,
            )
            for index, item in enumerate(field.competitors)
        ),
    )
    assert (
        optimize_field(renamed, ceiling=30).selected_marks
        == optimize_field(field, ceiling=30).selected_marks
    )


def test_pareto_tolerance_is_exactly_one_e_minus_nine() -> None:
    baseline = ObjectiveVector("0", "0", "0", "0")
    inside = ObjectiveVector("0.0000000005", "0", "0", "0")
    outside = ObjectiveVector("0.000000002", "0", "0", "0")
    assert not baseline.dominates(inside)
    assert baseline.dominates(outside)


def test_chim_balanced_knee_and_rank_deficient_svd_fallback_are_deterministic() -> None:
    rows = (
        ((3, 10), ObjectiveVector("0", "1", "1", "1")),
        ((3, 11), ObjectiveVector("1", "0", "1", "1")),
        ((3, 12), ObjectiveVector("1", "1", "0", "1")),
        ((3, 13), ObjectiveVector("1", "1", "1", "0")),
        ((3, 14), ObjectiveVector("0.5", "0.5", "0.5", "0.5")),
    )
    selected, frontier = optimizer_module._select_chim(
        rows, (3, 15), ObjectiveVector("1", "1", "1", "1")
    )
    assert selected == (3, 14)
    assert all(item.knee_distance == "0" for item in frontier[:4])
    assert frontier[4].knee_distance > "0"

    deficient = (
        ((3, 3), ObjectiveVector("0", "1", "0", "0")),
        ((3, 4), ObjectiveVector("0.5", "0.5", "0", "0")),
        ((3, 5), ObjectiveVector("1", "0", "0", "0")),
    )
    first = optimizer_module._select_chim(deficient, (3, 6), ObjectiveVector("1", "1", "0", "0"))
    second = optimizer_module._select_chim(deficient, (3, 6), ObjectiveVector("1", "1", "0", "0"))
    assert first == second


def test_chim_keeps_ordinary_two_better_two_worse_pareto_tradeoff_eligible() -> None:
    baseline = ObjectiveVector("1", "1", "1", "0")
    tradeoff = ObjectiveVector("2", "0", "0", "1")
    selected, _frontier = optimizer_module._select_chim(
        (((3, 10), baseline), ((3, 12), tradeoff)),
        (3, 10),
        baseline,
    )
    assert selected == (3, 12)

    selected, _frontier = optimizer_module._select_chim(
        (((3, 10), baseline), ((3, 12), ObjectiveVector("2", "2", "2", "1"))),
        (3, 10),
        baseline,
    )
    assert selected is None

    one_better_all_three_worse = ObjectiveVector("2", "0", "2", "1")
    selected, _frontier = optimizer_module._select_chim(
        (((3, 10), baseline), ((3, 12), one_better_all_three_worse)),
        (3, 10),
        baseline,
    )
    assert selected is None

    one_better_one_equal = ObjectiveVector("1", "0", "2", "1")
    selected, _frontier = optimizer_module._select_chim(
        (((3, 10), baseline), ((3, 12), one_better_one_equal)),
        (3, 10),
        baseline,
    )
    assert selected == (3, 12)


def test_end_to_end_chim_selects_documented_multi_second_fairness_tradeoff() -> None:
    counts = (820, 819, 819, 819, 819)
    a_samples = tuple(
        value
        for value, count in zip((43_000, 13_000, 50_000, 63_000, 69_000), counts, strict=True)
        for _ in range(count)
    )
    b_samples = tuple(
        value
        for value, count in zip((27_000, 40_000, 37_000, 50_000, 53_000), counts, strict=True)
        for _ in range(count)
    )
    field = OptimizationField.create(
        field_id=StableIdentifier("field:tradeoff"),
        source_receipt_digest="a" * 64,
        competitors=(
            OptimizationCompetitor(StableIdentifier("competitor:a"), 50_000, a_samples, 0),
            OptimizationCompetitor(StableIdentifier("competitor:b"), 40_000, b_samples, 1),
        ),
    )
    baseline = evaluate_sheet(field, (3, 13), (3, 13))
    tradeoff = evaluate_sheet(field, (3, 16), (3, 13))
    assert baseline == ObjectiveVector("0", "0.300048828125", "10998.779296875", "0")
    assert tradeoff == ObjectiveVector("4500000", "0.10009765625", "9198.486328125", "1.5")
    receipt = optimize_field(field, ceiling=30)
    assert receipt.selected_marks == (3, 16)
    assert receipt.deltas == (0, 3)
    assert receipt.fallback_reason is None
    assert receipt.fairness_gain == "0.199951171875"


def test_beam_pruning_is_invariant_to_equivalent_objective_unit_rescaling() -> None:
    frontier = tuple(
        (
            (3, 10 + index),
            ObjectiveVector(str(gap), str(fairness), str(spread), str(movement)),
        )
        for index, (gap, fairness, spread, movement) in enumerate(
            ((1, 4, 2, 0), (2, 2, 4, 1), (4, 1, 1, 2), (3, 3, 3, 3))
        )
    )
    scaled = tuple(
        (
            marks,
            ObjectiveVector(
                str(int(objective.gap_fidelity) * 1_000_000),
                objective.win_probability_parity,
                objective.expected_finish_spread_ms,
                objective.baseline_movement,
            ),
        )
        for marks, objective in frontier
    )
    assert optimizer_module._normalized_beam(frontier, (3, 10), 3) == (
        optimizer_module._normalized_beam(scaled, (3, 10), 3)
    )


def test_adversarial_multimodal_joint_samples_remain_finite_and_replay_exactly() -> None:
    low_high = tuple(20_000 if index % 2 == 0 else 80_000 for index in range(4096))
    high_low = tuple(80_000 if index % 2 == 0 else 20_000 for index in range(4096))
    field = OptimizationField.create(
        field_id=StableIdentifier("field:multimodal"),
        source_receipt_digest="f" * 64,
        competitors=(
            OptimizationCompetitor(StableIdentifier("competitor:a"), 50_000, low_high, 0),
            OptimizationCompetitor(StableIdentifier("competitor:b"), 50_000, high_low, 1),
        ),
    )
    first = optimize_field(field, ceiling=30)
    assert first == optimize_field(field, ceiling=30)
    assert first.selected_objectives.win_probability_parity == "0"


def test_raw_integer_objectives_and_frontier_equal_canonical_decimal_oracle() -> None:
    cases = (
        ((51_000, 46_000, 35_000), (3, 8, 19), 30),
        ((90_000, 30_000, 30_000, 10_000), (3, 20, 20, 20), 20),
        ((40_000, 40_000), (3, 3), 30),
    )
    for medians, baseline, ceiling in cases:
        field = _field(medians)
        receipt = optimize_field(field, ceiling=ceiling)
        sheets = tuple(dict.fromkeys((baseline, receipt.rounded_baseline, receipt.selected_marks)))
        sheets = tuple(marks for marks in sheets if _legal(medians, marks, ceiling))
        raw = optimizer_module._evaluate_candidates_impl(
            field, sheets, receipt.rounded_baseline, 3, parallel=False, raw=True
        )
        entrant_count = len(medians)
        parity_denominator = lcm(*range(1, entrant_count + 1)) * 4096 * entrant_count
        decimal = {
            marks: optimizer_module._materialize_raw_objective(
                values, entrant_count, parity_denominator, 4096
            )
            for marks, values in raw.items()
        }
        assert decimal == {
            marks: evaluate_sheet(field, marks, receipt.rounded_baseline) for marks in sheets
        }
        assert optimizer_module._pareto_frontier_raw(raw, entrant_count, 4096) == (
            optimizer_module._pareto_frontier(decimal)
        )

    assert not optimizer_module._dominates_raw((0, 0, 0, 0), (1, 0, 0, 0), (2_000_000_000,) * 4)
    assert optimizer_module._dominates_raw((0, 0, 0, 0), (1, 0, 0, 0), (500_000_000,) * 4)


def test_checked_in_windows_capacity_manifest_binds_artifact_and_worst_radius() -> None:
    manifest = json.loads(Path("benchmarks/v3/optimizer_manifest.json").read_text("utf-8"))
    body = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    assert manifest["manifest_digest"] == canonical_digest(body)
    assert manifest["implementation_artifact_digest"] == implementation_artifact_digest()
    assert manifest["policy_digest"] == DEFAULT_OPTIMIZER_POLICY.digest
    assert manifest["policy"] == DEFAULT_OPTIMIZER_POLICY.to_dict()
    gate = manifest["capacity_gate"]
    assert manifest["status"] == "release_blocked_pending_u15_assembly"
    assert gate == {
        "passed": False,
        "optimizer_passed": True,
        "required_repetitions": 100,
        "optimizer_p99_limit_ms": 1500,
        "rss_delta_limit_mib": 256,
        "field_assembly_p99_limit_ms": 2000,
        "field_assembly_p99_ms": None,
    }
    fixtures = manifest["windows_capacity_fixtures"]
    assert {item["candidate_search_strategy"] for item in fixtures} == {
        "exhaustive_radius_v1",
        "deterministic_beam_v1",
    }
    for fixture in fixtures:
        measurements = fixture["optimizer_only_measurements_ms"]
        assert len(measurements) == fixture["repetitions"]
        assert fixture["repetitions"] >= gate["required_repetitions"]
        assert fixture["observed_worst_ms"] == max(measurements)
        assert fixture["observed_p99_ms"] < gate["optimizer_p99_limit_ms"]
        assert fixture["peak_rss_delta_mib"] < gate["rss_delta_limit_mib"]
        assert len(fixture["sample_matrix_digest"]) == 64
        assert len(fixture["receipt_digest"]) == 64
        assert fixture["peak_rss_delta_mib"] >= 0
    exhaustive = next(item for item in fixtures if item["name"].startswith("six_entrant"))
    assert exhaustive["receipt_search_strategy"] == "canonical_fallback"
    assert exhaustive["fallback_reason"] == "no_valid_improvement"
    beam = next(item for item in fixtures if item["name"].startswith("twelve_entrant"))
    assert beam["receipt_search_strategy"] == "deterministic_beam_v1"
    assert beam["fallback_reason"] is None
