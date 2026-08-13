"""Contract tests for the deterministic Prediction Engine V2 mark optimizer."""

from __future__ import annotations

import itertools
import math

import numpy as np

from strathmark.mark_optimizer import (
    MarkOptimizationResult,
    legacy_rounded_gap_marks,
    optimize_joint_marks,
)
from strathmark.prediction_v2 import ForecastInterval, PredictiveDistribution


def _distribution(
    median: float,
    *,
    log_scale: float = 0.12,
    shared_log_scale: float = 0.03,
) -> PredictiveDistribution:
    radius = 1.6448536269514722 * log_scale
    location = math.log(median)
    return PredictiveDistribution(
        median=median,
        log_location=location,
        log_scale=log_scale,
        interval=ForecastInterval(
            lower=math.exp(location - radius),
            upper=math.exp(location + radius),
        ),
        source="test",
        history_count=5,
        effective_history_weight=3.0,
        metadata={"shared_log_scale": shared_log_scale},
    )


def _exhaustive_oracle(
    distributions: list[PredictiveDistribution],
    *,
    ceiling: int,
    seed: int,
    num_samples: int,
    floor: int = 3,
) -> tuple[tuple[int, ...], tuple[float, float, int, tuple[int, ...]]]:
    """Independently score every legal small-field mark sheet."""

    shared = np.random.default_rng(seed).standard_normal(num_samples)
    samples = np.column_stack(
        [
            distribution.sample(
                num_samples,
                seed=seed + (index + 1) * 1_000_003,
                shared_standard_normal=shared,
            )
            for index, distribution in enumerate(distributions)
        ]
    )
    legacy = legacy_rounded_gap_marks(
        [distribution.median for distribution in distributions],
        ceiling=ceiling,
        floor=floor,
    )
    ordered_indices = sorted(
        range(len(distributions)),
        key=lambda index: (-distributions[index].median, index),
    )

    best: tuple[tuple[int, ...], tuple[float, float, int, tuple[int, ...]]] | None = None
    for ordered_tail in itertools.combinations_with_replacement(
        range(floor, ceiling + 1), len(distributions) - 1
    ):
        marks_by_order = (floor, *ordered_tail)
        marks = [floor] * len(distributions)
        for position, index in enumerate(ordered_indices):
            marks[index] = marks_by_order[position]
        marks_tuple = tuple(marks)
        finishes = samples + (np.asarray(marks_tuple, dtype=float) - floor)
        winners = np.argmin(finishes, axis=1)
        win_probabilities = np.bincount(winners, minlength=len(distributions)) / num_samples
        objective = (
            float(np.sum((win_probabilities - 1.0 / len(distributions)) ** 2)),
            float(np.mean(np.ptp(finishes, axis=1))),
            sum(
                abs(mark - legacy_mark)
                for mark, legacy_mark in zip(marks_tuple, legacy, strict=True)
            ),
            marks_tuple,
        )
        if best is None or _oracle_compare(objective, best[1]) < 0:
            best = marks_tuple, objective

    assert best is not None
    return best


def _oracle_compare(
    left: tuple[float, float, int, tuple[int, ...]],
    right: tuple[float, float, int, tuple[int, ...]],
) -> int:
    for left_value, right_value in zip(left[:2], right[:2], strict=True):
        difference = float(left_value) - float(right_value)
        if difference < -1e-12:
            return -1
        if difference > 1e-12:
            return 1
    return (left[2:] > right[2:]) - (left[2:] < right[2:])


def test_legacy_marks_use_current_rounded_gap_contract() -> None:
    assert legacy_rounded_gap_marks([40.5, 35.0], ceiling=183) == (3, 9)
    assert legacy_rounded_gap_marks([39.5, 35.0], ceiling=183) == (3, 7)


def test_optimizer_is_deterministic_and_uses_exact_sample_budget(monkeypatch) -> None:
    distributions = [_distribution(60.0), _distribution(45.0), _distribution(30.0)]
    calls: list[tuple[int, int, tuple[int, ...]]] = []
    original = PredictiveDistribution.sample

    def recording_sample(self, size, *, seed=20260811, shared_standard_normal=None):
        calls.append((size, seed, np.asarray(shared_standard_normal).shape))
        return original(
            self,
            size,
            seed=seed,
            shared_standard_normal=shared_standard_normal,
        )

    monkeypatch.setattr(PredictiveDistribution, "sample", recording_sample)
    first = optimize_joint_marks(distributions, ceiling=183, seed=812)
    second = optimize_joint_marks(distributions, ceiling=183, seed=812)

    assert first == second
    assert all(size == 2048 and shape == (2048,) for size, _, shape in calls)
    assert len(calls) == 2 * len(distributions)


def test_optimizer_enforces_bounds_floor_and_median_order() -> None:
    # Deliberately unsorted input, including an equal-median pair. Stable input
    # order is the tie breaker used by the monotonic constraint.
    distributions = [
        _distribution(30.0, log_scale=0.30),
        _distribution(60.0),
        _distribution(30.0, log_scale=0.06),
        _distribution(45.0),
    ]
    result = optimize_joint_marks(distributions, ceiling=25)

    assert min(result.marks) == 3
    assert all(3 <= mark <= 25 for mark in result.marks)
    stable_slowest_to_fastest = sorted(
        range(len(distributions)), key=lambda index: (-distributions[index].median, index)
    )
    ordered_marks = [result.marks[index] for index in stable_slowest_to_fastest]
    assert ordered_marks == sorted(ordered_marks)


def test_search_never_worsens_legacy_lexicographic_objective() -> None:
    distributions = [
        _distribution(62.0, log_scale=0.06),
        _distribution(48.0, log_scale=0.25),
        _distribution(31.0, log_scale=0.08),
    ]
    result = optimize_joint_marks(distributions, ceiling=80, seed=99)

    assert result.objective <= result.legacy_objective
    assert result.legacy_marks == legacy_rounded_gap_marks(
        [distribution.median for distribution in distributions], ceiling=80
    )
    assert result.optimizer in {"posterior_crn_v2", "rounded_gap_fallback"}


def test_optimizer_matches_exhaustive_oracle_for_adversarial_small_fields() -> None:
    scenarios = [
        # A coordinate-search local minimum found by an exhaustive measurement.
        ([40.0, 37.0, 34.0], [0.12, 0.04, 0.04], 8, 5),
        # Equal medians retain caller order while all legal ties are scored.
        ([40.0, 40.0, 35.0], [0.08, 0.20, 0.04], 7, 11),
        # Four competitors under ceiling pressure exercise the larger state space.
        ([50.0, 45.0, 40.0, 35.0], [0.04, 0.12, 0.20, 0.08], 7, 19),
    ]

    for medians, scales, ceiling, seed in scenarios:
        distributions = [
            _distribution(median, log_scale=scale)
            for median, scale in zip(medians, scales, strict=True)
        ]
        result = optimize_joint_marks(
            distributions,
            ceiling=ceiling,
            seed=seed,
            num_samples=128,
        )
        oracle_marks, oracle_objective = _exhaustive_oracle(
            distributions,
            ceiling=ceiling,
            seed=seed,
            num_samples=128,
        )

        assert result.marks == oracle_marks
        assert result.objective == oracle_objective
        assert result.metadata()["search_strategy"] == "exhaustive_global"


def test_single_competitor_always_receives_mark_three() -> None:
    result = optimize_joint_marks([_distribution(35.0)], ceiling=183)

    assert result.marks == (3,)
    assert result.optimizer == "posterior_crn_v2"


def test_sampling_failure_returns_canonical_fallback(monkeypatch) -> None:
    distributions = [_distribution(50.0), _distribution(40.0)]

    def fail_sample(*args, **kwargs):
        raise RuntimeError("simulator unavailable")

    monkeypatch.setattr(PredictiveDistribution, "sample", fail_sample)
    result = optimize_joint_marks(distributions, ceiling=183)

    assert isinstance(result, MarkOptimizationResult)
    assert result.marks == legacy_rounded_gap_marks([50.0, 40.0], ceiling=183)
    assert result.optimizer == "rounded_gap_fallback"
    assert result.reason == "optimizer_failure"
    assert result.metadata()["objective"][:2] == [None, None]


def test_invalid_sample_output_returns_canonical_fallback(monkeypatch) -> None:
    distributions = [_distribution(50.0), _distribution(40.0)]
    monkeypatch.setattr(
        PredictiveDistribution,
        "sample",
        lambda *args, **kwargs: np.array([float("nan")] * 2048),
    )

    result = optimize_joint_marks(distributions, ceiling=183)

    assert result.optimizer == "rounded_gap_fallback"
    assert result.marks == (3, 13)
