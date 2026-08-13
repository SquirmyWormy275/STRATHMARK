"""Deterministic joint handicap-mark optimization.

The optimizer uses common random numbers so every candidate mark sheet is
compared against the same posterior race outcomes.  It never relies on elapsed
wall time: the work budget is a fixed sample count plus either a bounded number
of exhaustive candidates for tractable fields or a fixed maximum number of
coordinate passes.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Sequence, TypeAlias

import numpy as np

from strathmark.prediction_v2 import PredictiveDistribution

DEFAULT_MARK_SAMPLES = 2048
DEFAULT_MARK_SEED = 20260811
MAX_COORDINATE_PASSES = 8
MAX_EXHAUSTIVE_CANDIDATES = 4096
OBJECTIVE_TOLERANCE = 1e-12

MarkObjective: TypeAlias = tuple[float, float, int, tuple[int, ...]]


@dataclass(frozen=True)
class MarkOptimizationResult:
    """Marks and reproducibility metadata returned by the joint optimizer."""

    marks: tuple[int, ...]
    optimizer: str
    objective: MarkObjective
    legacy_marks: tuple[int, ...]
    legacy_objective: MarkObjective
    simulations: int
    seed: int
    passes: int
    reason: str | None = None
    search_strategy: str = "unspecified"

    def metadata(self) -> dict[str, object]:
        """Return JSON-safe optimizer metadata for each field result."""

        objective_probability = self.objective[0] if np.isfinite(self.objective[0]) else None
        objective_spread = self.objective[1] if np.isfinite(self.objective[1]) else None
        legacy_probability = (
            self.legacy_objective[0] if np.isfinite(self.legacy_objective[0]) else None
        )
        legacy_spread = self.legacy_objective[1] if np.isfinite(self.legacy_objective[1]) else None
        return {
            "optimizer": self.optimizer,
            "simulations": self.simulations,
            "seed": self.seed,
            "passes": self.passes,
            "search_strategy": self.search_strategy,
            "objective": [
                objective_probability,
                objective_spread,
                self.objective[2],
                list(self.objective[3]),
            ],
            "legacy_objective": [
                legacy_probability,
                legacy_spread,
                self.legacy_objective[2],
                list(self.legacy_objective[3]),
            ],
            "reason": self.reason,
        }


def legacy_rounded_gap_marks(
    medians: Sequence[float],
    *,
    ceiling: int,
    floor: int = 3,
) -> tuple[int, ...]:
    """Return the canonical bounded point-estimate marks used for fallback."""

    if ceiling < floor:
        raise ValueError("ceiling must be greater than or equal to floor")
    values = np.asarray(medians, dtype=float)
    if values.ndim != 1 or not np.all(np.isfinite(values)) or np.any(values <= 0):
        raise ValueError("medians must be a finite, positive one-dimensional sequence")
    if values.size == 0:
        return ()
    slowest = float(np.max(values))
    return tuple(
        min(ceiling, max(floor, floor + round(slowest - float(median)))) for median in values
    )


def _objective(
    samples: np.ndarray,
    marks: tuple[int, ...],
    legacy_marks: tuple[int, ...],
    floor: int,
) -> MarkObjective:
    delays = np.asarray(marks, dtype=float) - floor
    finishes = samples + delays[np.newaxis, :]
    winners = np.argmin(finishes, axis=1)
    win_probabilities = np.bincount(winners, minlength=len(marks)) / samples.shape[0]
    equal_probability = 1.0 / len(marks)
    probability_loss = float(np.sum((win_probabilities - equal_probability) ** 2))
    expected_spread = float(np.mean(np.max(finishes, axis=1) - np.min(finishes, axis=1)))
    legacy_departure = int(
        sum(abs(mark - legacy) for mark, legacy in zip(marks, legacy_marks, strict=True))
    )
    return probability_loss, expected_spread, legacy_departure, marks


def _compare_objectives(left: MarkObjective, right: MarkObjective) -> int:
    """Compare objectives lexicographically with the specified float tolerance."""

    for left_value, right_value in zip(left[:2], right[:2], strict=True):
        difference = float(left_value) - float(right_value)
        if difference < -OBJECTIVE_TOLERANCE:
            return -1
        if difference > OBJECTIVE_TOLERANCE:
            return 1
    if left[2] < right[2]:
        return -1
    if left[2] > right[2]:
        return 1
    if left[3] < right[3]:
        return -1
    if left[3] > right[3]:
        return 1
    return 0


def _exhaustive_small_field_search(
    samples: np.ndarray,
    legacy_marks: tuple[int, ...],
    ordered_indices: Sequence[int],
    *,
    ceiling: int,
    floor: int,
) -> tuple[tuple[int, ...], MarkObjective]:
    """Return the global optimum after scoring every legal mark sheet."""

    best_marks: tuple[int, ...] | None = None
    best_objective: MarkObjective | None = None
    for ordered_tail in itertools.combinations_with_replacement(
        range(floor, ceiling + 1), len(ordered_indices) - 1
    ):
        ordered_marks = (floor, *ordered_tail)
        marks = [floor] * len(ordered_indices)
        for position, index in enumerate(ordered_indices):
            marks[index] = ordered_marks[position]
        marks_tuple = tuple(marks)
        objective = _objective(samples, marks_tuple, legacy_marks, floor)
        if best_objective is None or _compare_objectives(objective, best_objective) < 0:
            best_marks = marks_tuple
            best_objective = objective

    if best_marks is None or best_objective is None:  # pragma: no cover - guarded input
        raise ValueError("exhaustive search produced no candidates")
    return best_marks, best_objective


def _fallback_result(
    legacy_marks: tuple[int, ...],
    *,
    simulations: int,
    seed: int,
    reason: str,
    objective: MarkObjective | None = None,
) -> MarkOptimizationResult:
    unavailable: MarkObjective = (
        float("inf"),
        float("inf"),
        0,
        legacy_marks,
    )
    resolved = objective or unavailable
    return MarkOptimizationResult(
        marks=legacy_marks,
        optimizer="rounded_gap_fallback",
        objective=resolved,
        legacy_marks=legacy_marks,
        legacy_objective=resolved,
        simulations=simulations,
        seed=seed,
        passes=0,
        search_strategy="rounded_gap_fallback",
        reason=reason,
    )


def optimize_joint_marks(
    distributions: Sequence[PredictiveDistribution],
    *,
    ceiling: int,
    floor: int = 3,
    seed: int = DEFAULT_MARK_SEED,
    num_samples: int = DEFAULT_MARK_SAMPLES,
    max_passes: int = MAX_COORDINATE_PASSES,
) -> MarkOptimizationResult:
    """Choose deterministic integer marks from posterior race simulations.

    Candidate sheets are compared by equal-win-probability loss, expected
    finish spread, departure from rounded median-gap marks, then their mark
    tuple in caller input order.  Any sampling or search failure returns the
    canonical rounded-gap sheet instead of preventing race-day operation.
    """

    medians = [float(distribution.median) for distribution in distributions]
    legacy_marks = legacy_rounded_gap_marks(medians, ceiling=ceiling, floor=floor)
    if not distributions:
        return _fallback_result(
            legacy_marks,
            simulations=num_samples,
            seed=seed,
            reason="empty_field",
        )
    if num_samples <= 0 or max_passes < 0:
        return _fallback_result(
            legacy_marks,
            simulations=num_samples,
            seed=seed,
            reason="invalid_optimizer_config",
        )

    try:
        shared_standard_normal = np.random.default_rng(seed).standard_normal(num_samples)
        sampled_columns = []
        for index, distribution in enumerate(distributions):
            sample_seed = int(seed) + (index + 1) * 1_000_003
            sampled_columns.append(
                distribution.sample(
                    num_samples,
                    seed=sample_seed,
                    shared_standard_normal=shared_standard_normal,
                )
            )
        samples = np.column_stack(sampled_columns)
        if samples.shape != (num_samples, len(distributions)):
            raise ValueError("posterior samples have an unexpected shape")
        if not np.all(np.isfinite(samples)) or np.any(samples <= 0):
            raise ValueError("posterior samples must be positive and finite")

        legacy_objective = _objective(samples, legacy_marks, legacy_marks, floor)
        if len(distributions) == 1:
            return MarkOptimizationResult(
                marks=(floor,),
                optimizer="posterior_crn_v2",
                objective=legacy_objective,
                legacy_marks=(floor,),
                legacy_objective=legacy_objective,
                simulations=num_samples,
                seed=seed,
                passes=0,
                search_strategy="single_competitor",
            )

        # A stable descending-median order defines the monotonicity constraint.
        # Equal medians retain caller input order through the index tie breaker.
        ordered_indices = sorted(
            range(len(distributions)), key=lambda index: (-medians[index], index)
        )
        candidate_count = math.comb(
            ceiling - floor + len(distributions) - 1, len(distributions) - 1
        )
        if candidate_count <= MAX_EXHAUSTIVE_CANDIDATES:
            optimized_marks, best_objective = _exhaustive_small_field_search(
                samples,
                legacy_marks,
                ordered_indices,
                ceiling=ceiling,
                floor=floor,
            )
            completed_passes = 0
            search_strategy = "exhaustive_global"
        else:
            order_position = {index: position for position, index in enumerate(ordered_indices)}
            marks = list(legacy_marks)
            best_objective = legacy_objective
            completed_passes = 0

            for pass_number in range(max_passes):
                changed = False
                # The coordinate order is caller input order, not median order.
                for index in range(len(marks)):
                    position = order_position[index]
                    if position == 0:
                        candidates = (floor,)
                    else:
                        lower = marks[ordered_indices[position - 1]]
                        upper = (
                            ceiling
                            if position == len(marks) - 1
                            else marks[ordered_indices[position + 1]]
                        )
                        candidates = range(lower, upper + 1)

                    coordinate_mark = marks[index]
                    coordinate_objective = best_objective
                    for candidate in candidates:
                        trial = list(marks)
                        trial[index] = candidate
                        trial_tuple = tuple(trial)
                        trial_objective = _objective(samples, trial_tuple, legacy_marks, floor)
                        if _compare_objectives(trial_objective, coordinate_objective) < 0:
                            coordinate_mark = candidate
                            coordinate_objective = trial_objective
                    if coordinate_mark != marks[index]:
                        marks[index] = coordinate_mark
                        best_objective = coordinate_objective
                        changed = True
                completed_passes = pass_number + 1
                if not changed:
                    break

            optimized_marks = tuple(marks)
            search_strategy = "bounded_coordinate"
        # This is also a guard against future search changes accidentally
        # accepting a sheet that is inferior to the established fallback.
        if _compare_objectives(best_objective, legacy_objective) > 0:
            return _fallback_result(
                legacy_marks,
                simulations=num_samples,
                seed=seed,
                reason="search_worse_than_legacy",
                objective=legacy_objective,
            )
        return MarkOptimizationResult(
            marks=optimized_marks,
            optimizer="posterior_crn_v2",
            objective=best_objective,
            legacy_marks=legacy_marks,
            legacy_objective=legacy_objective,
            simulations=num_samples,
            seed=seed,
            passes=completed_passes,
            search_strategy=search_strategy,
        )
    except Exception:  # Race-day fail-open boundary; details are never user data.
        return _fallback_result(
            legacy_marks,
            simulations=num_samples,
            seed=seed,
            reason="optimizer_failure",
        )


__all__ = [
    "DEFAULT_MARK_SAMPLES",
    "DEFAULT_MARK_SEED",
    "MAX_EXHAUSTIVE_CANDIDATES",
    "MAX_COORDINATE_PASSES",
    "MarkOptimizationResult",
    "legacy_rounded_gap_marks",
    "optimize_joint_marks",
]
