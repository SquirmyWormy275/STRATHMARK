"""Extended tests for strathmark/variance.py — std-dev estimation, helpers, quick fairness."""

import numpy as np
import pandas as pd

from strathmark.config import rules
from strathmark.variance import (
    MAX_COMPETITOR_STD_SECONDS,
    MIN_COMPETITOR_STD_SECONDS,
    _get_competitor_variance_seconds,
    _global_fallback_std_dev,
    _pooled_std_dev_by_event,
    audit_mark_sheet,
    estimate_competitor_std_dev,
    quick_fairness_check,
    run_monte_carlo_simulation,
    simulate_single_race,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _comp(name, mark, predicted_time, std_dev=None):
    d = {"name": name, "mark": mark, "predicted_time": predicted_time}
    if std_dev is not None:
        d["performance_std_dev"] = std_dev
    return d


def _make_competitor_df(name="Alice", event="SB", times=None, n=10):
    """Build a results DataFrame for one or more competitors."""
    if times is None:
        rng = np.random.RandomState(42)
        times = 30.0 + rng.normal(0, 2.5, n)
    rows = [{"competitor_name": name, "event": event, "raw_time": t} for t in times]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# _pooled_std_dev_by_event
# ---------------------------------------------------------------------------


class TestPooledStdDevByEvent:
    def test_none_returns_none(self):
        assert _pooled_std_dev_by_event(None, 3) is None

    def test_empty_returns_none(self):
        assert _pooled_std_dev_by_event(pd.DataFrame(), 3) is None

    def test_insufficient_samples_returns_none(self):
        df = pd.DataFrame(
            {
                "raw_time": [30.0, 31.0],
                "event": ["SB", "SB"],
            }
        )
        result = _pooled_std_dev_by_event(df, min_samples=5)
        assert result is None

    def test_single_event_group(self):
        times = [30.0, 31.0, 29.0, 30.5, 28.5, 32.0]
        df = pd.DataFrame(
            {
                "raw_time": times,
                "event": ["SB"] * len(times),
            }
        )
        result = _pooled_std_dev_by_event(df, min_samples=3)
        assert result is not None
        assert 0 < result < 5.0

    def test_two_event_groups_pooled(self):
        """Pooling SB and UH should combine within-group variances."""
        sb_times = [30.0, 31.0, 29.0, 30.5]
        uh_times = [40.0, 41.0, 39.0, 40.5]
        df = pd.DataFrame(
            {
                "raw_time": sb_times + uh_times,
                "event": ["SB"] * 4 + ["UH"] * 4,
            }
        )
        result = _pooled_std_dev_by_event(df, min_samples=3)
        assert result is not None
        # Pooled std should reflect within-group variance, not between-group
        assert result < 3.0  # each group has ~1s std

    def test_no_event_column_uses_overall_std(self):
        df = pd.DataFrame({"raw_time": [30.0, 31.0, 29.0, 30.5]})
        result = _pooled_std_dev_by_event(df, min_samples=3)
        assert result is not None
        assert result > 0


# ---------------------------------------------------------------------------
# _global_fallback_std_dev
# ---------------------------------------------------------------------------


class TestGlobalFallbackStdDev:
    def test_none_returns_none(self):
        assert _global_fallback_std_dev(None, 3) is None

    def test_empty_returns_none(self):
        assert _global_fallback_std_dev(pd.DataFrame(), 3) is None

    def test_computes_median_across_competitors(self):
        """Should return median of per-competitor pooled std devs."""
        rows = []
        # Competitor A: low variance
        for t in [30.0, 30.5, 29.5, 31.0, 29.0]:
            rows.append({"competitor_name": "A", "event": "SB", "raw_time": t})
        # Competitor B: higher variance
        for t in [30.0, 35.0, 25.0, 33.0, 27.0]:
            rows.append({"competitor_name": "B", "event": "SB", "raw_time": t})
        # Competitor C: moderate variance
        for t in [30.0, 32.0, 28.0, 31.0, 29.0]:
            rows.append({"competitor_name": "C", "event": "SB", "raw_time": t})

        df = pd.DataFrame(rows)
        result = _global_fallback_std_dev(df, min_samples=3)
        assert result is not None
        assert 0 < result < 10.0


# ---------------------------------------------------------------------------
# estimate_competitor_std_dev — cascading fallback
# ---------------------------------------------------------------------------


class TestEstimateCompetitorStdDev:
    def test_none_results_returns_default(self):
        std, rating = estimate_competitor_std_dev("Alice", "SB", None)
        assert std == rules.PERFORMANCE_VARIANCE_SECONDS
        assert rating == "MODERATE"

    def test_empty_results_returns_default(self):
        std, rating = estimate_competitor_std_dev("Alice", "SB", pd.DataFrame())
        assert std == rules.PERFORMANCE_VARIANCE_SECONDS

    def test_non_dataframe_returns_default(self):
        std, rating = estimate_competitor_std_dev("Alice", "SB", "not a df")
        assert std == rules.PERFORMANCE_VARIANCE_SECONDS

    def test_competitor_with_enough_data(self):
        """Competitor with 10+ results should get a data-driven std-dev."""
        df = _make_competitor_df("Alice", "SB", n=15)
        std, rating = estimate_competitor_std_dev("Alice", "SB", df)
        assert MIN_COMPETITOR_STD_SECONDS <= std <= MAX_COMPETITOR_STD_SECONDS
        assert rating in ("VERY HIGH", "HIGH", "MODERATE", "LOW")

    def test_competitor_not_in_data_uses_global(self):
        """If competitor not found, should fall back to global median."""
        df = _make_competitor_df("Bob", "SB", n=15)
        std, rating = estimate_competitor_std_dev("NonexistentPerson", "SB", df)
        assert MIN_COMPETITOR_STD_SECONDS <= std <= MAX_COMPETITOR_STD_SECONDS

    def test_clamped_at_floor(self):
        """Very consistent competitor should be clamped at min."""
        # All identical times -> 0 variance -> clamped to floor
        times = [30.0] * 10
        df = _make_competitor_df("Alice", "SB", times=times)
        std, _ = estimate_competitor_std_dev("Alice", "SB", df)
        assert std == MIN_COMPETITOR_STD_SECONDS

    def test_clamped_at_ceiling(self):
        """Very inconsistent competitor should be clamped at max."""
        # Extremely variable times
        times = [10.0, 100.0, 15.0, 95.0, 20.0, 90.0, 25.0, 85.0, 30.0, 80.0]
        df = _make_competitor_df("Alice", "SB", times=times)
        std, _ = estimate_competitor_std_dev("Alice", "SB", df)
        assert std == MAX_COMPETITOR_STD_SECONDS

    def test_case_insensitive_name(self):
        df = _make_competitor_df("Alice Smith", "SB", n=10)
        std1, _ = estimate_competitor_std_dev("Alice Smith", "SB", df)
        std2, _ = estimate_competitor_std_dev("alice smith", "SB", df)
        assert std1 == std2


# ---------------------------------------------------------------------------
# _get_competitor_variance_seconds edge cases
# ---------------------------------------------------------------------------


class TestGetCompetitorVarianceEdgeCases:
    def test_nan_std_dev_uses_default(self):
        comp = _comp("A", 3, 30.0, std_dev=float("nan"))
        result = _get_competitor_variance_seconds(comp)
        assert result == rules.PERFORMANCE_VARIANCE_SECONDS

    def test_string_std_dev_uses_default(self):
        comp = {
            "name": "A",
            "mark": 3,
            "predicted_time": 30.0,
            "performance_std_dev": "not a number",
        }
        result = _get_competitor_variance_seconds(comp)
        assert result == rules.PERFORMANCE_VARIANCE_SECONDS

    def test_std_dev_key_alias(self):
        """Both 'std_dev' and 'performance_std_dev' should work."""
        comp = {"name": "A", "mark": 3, "predicted_time": 30.0, "std_dev": 4.0}
        result = _get_competitor_variance_seconds(comp)
        assert result == 4.0

    def test_no_std_dev_uses_default(self):
        comp = {"name": "A", "mark": 3, "predicted_time": 30.0}
        result = _get_competitor_variance_seconds(comp)
        assert result == rules.PERFORMANCE_VARIANCE_SECONDS


# ---------------------------------------------------------------------------
# simulate_single_race
# ---------------------------------------------------------------------------


class TestSimulateSingleRace:
    def test_returns_a_competitor_name(self):
        comps = [_comp("A", 3, 30.0), _comp("B", 10, 25.0)]
        rng = np.random.default_rng(42)
        winner = simulate_single_race(comps, rng=rng)
        assert winner in ("A", "B")

    def test_single_competitor_always_wins(self):
        comps = [_comp("Solo", 3, 30.0)]
        rng = np.random.default_rng(42)
        winner = simulate_single_race(comps, rng=rng)
        assert winner == "Solo"

    def test_heavily_favored_competitor_wins_more(self):
        """A competitor with mark=3 and predicted_time=10s vs mark=100 and predicted_time=10s."""
        fast_start = _comp("FrontRunner", 3, 10.0, std_dev=1.5)
        late_start = _comp("BackMarker", 100, 10.0, std_dev=1.5)
        # BackMarker starts 97s late, so FrontRunner should almost always win
        wins = {"FrontRunner": 0, "BackMarker": 0}
        rng = np.random.default_rng(42)
        for _ in range(100):
            winner = simulate_single_race([fast_start, late_start], rng=rng)
            wins[winner] += 1
        assert wins["FrontRunner"] > 95


# ---------------------------------------------------------------------------
# audit_mark_sheet
# ---------------------------------------------------------------------------


class TestAuditMarkSheet:
    def test_basic_audit(self):
        comps = [
            {"name": "A", "predicted_time": 30.0, "mark": 3},
            {"name": "B", "predicted_time": 25.0, "mark": 8},
        ]
        result = audit_mark_sheet(comps, num_simulations=10_000, verbose=False)
        assert "per_competitor" in result
        assert "fairness_rating" in result
        assert result["fairness_rating"] in ("excellent", "good", "fair", "poor")
        assert "A" in result["per_competitor"]
        assert "B" in result["per_competitor"]

    def test_equal_handicaps_produce_excellent(self):
        comps = [{"name": f"C{i}", "predicted_time": 30.0, "mark": 3} for i in range(4)]
        result = audit_mark_sheet(comps, num_simulations=50_000, verbose=False)
        assert result["fairness_rating"] == "excellent"

    def test_variance_override_applied(self):
        """Custom variance should be injected into competitors."""
        comps = [
            {"name": "A", "predicted_time": 30.0, "mark": 3},
            {"name": "B", "predicted_time": 25.0, "mark": 8},
        ]
        result = audit_mark_sheet(comps, num_simulations=1_000, variance=1.0, verbose=False)
        assert result is not None


# ---------------------------------------------------------------------------
# quick_fairness_check
# ---------------------------------------------------------------------------


class TestQuickFairnessCheck:
    def test_returns_same_structure_as_audit(self):
        comps = [
            {"name": "A", "predicted_time": 30.0, "mark": 3},
            {"name": "B", "predicted_time": 25.0, "mark": 8},
        ]
        result = quick_fairness_check(comps)
        assert "fairness_rating" in result
        assert "win_rate_spread" in result
        assert "per_competitor" in result


# ---------------------------------------------------------------------------
# Monte Carlo: variance ratio and imbalance detection
# ---------------------------------------------------------------------------


class TestVarianceRatioDetection:
    def test_balanced_variance_ratio_near_1(self):
        comps = [
            _comp("A", 3, 30.0, std_dev=3.0),
            _comp("B", 8, 25.0, std_dev=3.0),
        ]
        result = run_monte_carlo_simulation(comps, num_simulations=1_000, seed=42, verbose=False)
        assert abs(result["variance_ratio"] - 1.0) < 0.01
        assert result["variance_imbalanced"] is False

    def test_imbalanced_variance_flagged(self):
        comps = [
            _comp("A", 3, 30.0, std_dev=1.5),
            _comp("B", 8, 25.0, std_dev=6.0),
        ]
        result = run_monte_carlo_simulation(comps, num_simulations=1_000, seed=42, verbose=False)
        assert result["variance_ratio"] == 6.0 / 1.5
        assert result["variance_imbalanced"] is True


# ---------------------------------------------------------------------------
# Monte Carlo: finish order tracking
# ---------------------------------------------------------------------------


class TestFinishOrderTracking:
    def test_track_finish_orders(self):
        comps = [_comp(f"C{i}", 3, 30.0, std_dev=3.0) for i in range(3)]
        result = run_monte_carlo_simulation(
            comps, num_simulations=1_000, seed=42, track_finish_orders=True, verbose=False
        )
        assert result["most_common_order"] is not None
        assert result["most_common_order_pct"] is not None
        assert result["most_common_order_pct"] > 0

    def test_podium_margin_tracking(self):
        comps = [_comp(f"C{i}", 3, 30.0, std_dev=3.0) for i in range(3)]
        result = run_monte_carlo_simulation(
            comps, num_simulations=1_000, seed=42, track_podium_margins=True, verbose=False
        )
        assert result["avg_podium_margin_12"] is not None
        assert result["avg_podium_margin_12"] > 0
        assert result["photo_finish_pct"] is not None
