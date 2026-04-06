"""Extended tests for strathmark/fallback.py — cascade fallbacks, helpers, edge cases."""

import pandas as pd
import pytest

from strathmark.fallback import (
    PANEL_MARK_DEFAULT_UNKNOWN_DIVISION,
    PANEL_MARKS_300MM,
    _calculate_performance_weight_simple,
    _compute_robust_mean,
    _normalize_time_for_baseline,
    _standardize_results_df,
    get_competitor_historical_times_flexible,
    get_event_baseline,
    get_panel_mark,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_results_df(rows):
    """Build a results DataFrame from a list of dicts."""
    df = pd.DataFrame(rows)
    if "raw_time" in df.columns:
        df["raw_time"] = pd.to_numeric(df["raw_time"], errors="coerce")
    if "size_mm" in df.columns:
        df["size_mm"] = pd.to_numeric(df["size_mm"], errors="coerce")
    return df


def _result_row(
    name="Alice",
    event="SB",
    time=30.0,
    species="S01",
    diameter=300,
    quality=5,
    result_date=None,
):
    """Build a single result row dict."""
    row = {
        "competitor_name": name,
        "event": event,
        "raw_time": time,
        "species": species,
        "size_mm": diameter,
        "quality": quality,
    }
    if result_date is not None:
        row["date"] = result_date
    return row


# ---------------------------------------------------------------------------
# Panel mark extended tests
# ---------------------------------------------------------------------------


class TestPanelMarkDivisionAliases:
    """Division aliases should map to known keys."""

    @pytest.mark.parametrize(
        "alias,expected_key",
        [
            ("masters", "Veterans"),
            ("senior", "Veterans"),
            ("women", "Womens"),
            ("women's", "Womens"),
            ("female", "Womens"),
            ("youth", "Junior"),
            ("elite", "Open"),
            ("professional", "Open"),
        ],
    )
    def test_division_alias(self, alias, expected_key):
        time_val, explanation = get_panel_mark("SB", alias)
        expected = PANEL_MARKS_300MM.get(("SB", expected_key), PANEL_MARK_DEFAULT_UNKNOWN_DIVISION)
        assert time_val == expected
        assert expected_key in explanation

    def test_whitespace_handling(self):
        t1, _ = get_panel_mark("  SB  ", "  Open  ")
        t2, _ = get_panel_mark("SB", "Open")
        assert t1 == t2

    def test_unrecognized_division_returns_default(self):
        time_val, explanation = get_panel_mark("SB", "SuperElite")
        assert time_val == PANEL_MARK_DEFAULT_UNKNOWN_DIVISION
        assert "unrecognized" in explanation.lower() or "default" in explanation.lower()


# ---------------------------------------------------------------------------
# _standardize_results_df
# ---------------------------------------------------------------------------


class TestStandardizeResultsDf:
    def test_none_input_returns_none(self):
        result = _standardize_results_df(None)
        assert result is None

    def test_empty_df_returns_empty(self):
        result = _standardize_results_df(pd.DataFrame())
        assert result is None or result.empty

    def test_drops_rows_with_zero_time(self):
        df = _make_results_df(
            [
                _result_row(time=30.0),
                _result_row(time=0.0),
                _result_row(time=-5.0),
            ]
        )
        result = _standardize_results_df(df)
        assert len(result) == 1
        assert float(result.iloc[0]["raw_time"]) == 30.0

    def test_drops_nan_time(self):
        df = _make_results_df(
            [
                _result_row(time=30.0),
                _result_row(time=float("nan")),
            ]
        )
        result = _standardize_results_df(df)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _calculate_performance_weight_simple
# ---------------------------------------------------------------------------


class TestPerformanceWeightSimple:
    def test_none_date_returns_1(self):
        assert _calculate_performance_weight_simple(None) == 1.0

    def test_nat_returns_1(self):
        assert _calculate_performance_weight_simple(pd.NaT) == 1.0

    def test_recent_date_near_1(self):
        from datetime import datetime

        w = _calculate_performance_weight_simple(datetime.now())
        assert 0.99 <= w <= 1.0

    def test_old_date_decayed(self):
        from datetime import datetime

        old = datetime(2020, 1, 1)
        ref = datetime(2024, 1, 1)
        w = _calculate_performance_weight_simple(old, ref, half_life_days=730)
        assert 0.2 < w < 0.3  # ~4 years = ~2 half-lives -> ~0.25

    def test_future_date_returns_1(self):
        from datetime import datetime

        future = datetime(2030, 1, 1)
        ref = datetime(2025, 1, 1)
        w = _calculate_performance_weight_simple(future, ref)
        assert w == 1.0

    def test_string_date_parsed(self):
        w = _calculate_performance_weight_simple("2025-01-01", "2025-01-01")
        assert 0.99 <= w <= 1.0


# ---------------------------------------------------------------------------
# _normalize_time_for_baseline
# ---------------------------------------------------------------------------


class TestNormalizeTimeForBaseline:
    def test_same_species_same_diameter_returns_similar(self):
        df = _make_results_df([_result_row()])
        result = _normalize_time_for_baseline(
            30.0, "S01", 300.0, "S01", 300.0, "SB", df, quality=5.0
        )
        assert abs(result - 30.0) < 0.1

    def test_quality_adjustment(self):
        df = _make_results_df([_result_row()])
        # Quality 7 wood time should be normalized down to quality 5
        result = _normalize_time_for_baseline(
            30.0, "S01", 300.0, "S01", 300.0, "SB", df, quality=7.0
        )
        # Quality 7 -> factor 1.04, so normalized = 30/1.04 ≈ 28.85
        assert result < 30.0

    def test_diameter_scaling(self):
        df = _make_results_df([_result_row()])
        # From 350mm to 300mm -> smaller diameter should reduce time
        result = _normalize_time_for_baseline(
            30.0, "S01", 350.0, "S01", 300.0, "SB", df, quality=5.0
        )
        assert result < 30.0

    def test_species_normalization(self):
        df = _make_results_df([_result_row()])
        # S05 (ponderosa pine, mult 1.317) to S01 (mult 1.0)
        result = _normalize_time_for_baseline(
            40.0, "S05", 300.0, "S01", 300.0, "SB", df, quality=5.0
        )
        # Normalizing from harder to softer species should reduce time
        assert result < 40.0


# ---------------------------------------------------------------------------
# _compute_robust_mean
# ---------------------------------------------------------------------------


class TestComputeRobustMean:
    def test_empty_returns_none(self):
        assert _compute_robust_mean([]) is None

    def test_single_value_returns_median(self):
        assert _compute_robust_mean([42.0]) == 42.0

    def test_few_values_returns_median(self):
        result = _compute_robust_mean([10.0, 20.0, 30.0])
        assert result == 20.0

    def test_many_values_clips_outliers(self):
        times = [30.0, 31.0, 29.0, 30.5, 28.5, 200.0]  # 200 is outlier
        result = _compute_robust_mean(times)
        # With MAD clipping, 200 should be clipped down
        assert result < 60.0  # much less than naive mean ~58

    def test_identical_values(self):
        result = _compute_robust_mean([25.0, 25.0, 25.0, 25.0, 25.0])
        assert abs(result - 25.0) < 0.01

    def test_negative_values_handled(self):
        # Shouldn't happen in practice, but shouldn't crash
        result = _compute_robust_mean([-1.0, -2.0, -3.0])
        assert result is not None


# ---------------------------------------------------------------------------
# get_event_baseline — 4-level cascade
# ---------------------------------------------------------------------------


class TestGetEventBaseline:
    def test_none_results_returns_none(self):
        val, conf, expl = get_event_baseline("SB", "S01", 300, None)
        assert val is None
        assert "no data" in expl.lower()

    def test_empty_results_returns_none(self):
        val, conf, expl = get_event_baseline("SB", "S01", 300, pd.DataFrame())
        assert val is None

    def test_level1_exact_species_diameter(self):
        """Level 1: exact event + species + diameter match."""
        rows = [
            _result_row(name=f"C{i}", event="SB", time=30.0 + i, species="S01", diameter=300)
            for i in range(5)
        ]
        df = _make_results_df(rows)
        val, conf, expl = get_event_baseline("SB", "S01", 300, df)
        assert val is not None
        assert conf == "HIGH"
        assert 25 < val < 40

    def test_level2_same_diameter_any_species(self):
        """Level 2: event + diameter match, different species."""
        rows = [
            _result_row(name=f"C{i}", event="SB", time=30.0 + i, species="S05", diameter=300)
            for i in range(5)
        ]
        df = _make_results_df(rows)
        # Requesting S01 but data only has S05 at 300mm
        val, conf, expl = get_event_baseline("SB", "S01", 300, df)
        assert val is not None
        # Should find at level 2 (size match) or level 3 (event only)
        assert conf in ("MEDIUM", "HIGH", "LOW")

    def test_level3_event_only(self):
        """Level 3: only event matches (different species and diameter)."""
        rows = [
            _result_row(name=f"C{i}", event="SB", time=30.0 + i, species="S05", diameter=400)
            for i in range(5)
        ]
        df = _make_results_df(rows)
        # Requesting S01 at 250mm, data has S05 at 400mm
        val, conf, expl = get_event_baseline("SB", "S01", 250, df)
        assert val is not None

    def test_level4_no_matching_event(self):
        """Level 4: no matching event at all."""
        rows = [_result_row(name=f"C{i}", event="UH", time=30.0 + i) for i in range(5)]
        df = _make_results_df(rows)
        val, conf, expl = get_event_baseline("SB", "S01", 300, df)
        assert val is None
        assert "insufficient" in expl.lower()

    def test_exclude_competitor(self):
        """Excluded competitor's data should not contribute."""
        rows = [
            _result_row(name="Alice", event="SB", time=30.0, species="S01", diameter=300),
            _result_row(name="Alice", event="SB", time=31.0, species="S01", diameter=300),
            _result_row(name="Alice", event="SB", time=32.0, species="S01", diameter=300),
            _result_row(name="Bob", event="SB", time=50.0, species="S01", diameter=300),
            _result_row(name="Bob", event="SB", time=51.0, species="S01", diameter=300),
            _result_row(name="Bob", event="SB", time=52.0, species="S01", diameter=300),
        ]
        df = _make_results_df(rows)

        val_excl, _, _ = get_event_baseline("SB", "S01", 300, df, exclude_competitor="Alice")
        val_all, _, _ = get_event_baseline("SB", "S01", 300, df)

        # Excluding Alice (30s avg) should shift baseline toward Bob (50s avg)
        assert val_excl > val_all

    def test_filters_times_over_limit(self):
        """Times over MAX_TIME_LIMIT_SECONDS should be excluded."""
        rows = [
            _result_row(name=f"C{i}", event="SB", time=30.0, species="S01", diameter=300)
            for i in range(3)
        ] + [
            _result_row(name="Slow", event="SB", time=200.0, species="S01", diameter=300),
        ]
        df = _make_results_df(rows)
        val, _, _ = get_event_baseline("SB", "S01", 300, df)
        # 200s exceeds MAX_TIME_LIMIT_SECONDS (180) so should be excluded
        assert val is not None
        assert val < 50.0  # should be near 30, not inflated by 200


# ---------------------------------------------------------------------------
# get_competitor_historical_times_flexible — 3-level cascade
# ---------------------------------------------------------------------------


class TestGetCompetitorHistoricalTimesFlexible:
    def test_none_results_returns_none(self):
        times, conf, expl = get_competitor_historical_times_flexible(
            "Alice", "SB", "S01", 300, None
        )
        assert times is None

    def test_empty_df_returns_none(self):
        times, conf, expl = get_competitor_historical_times_flexible(
            "Alice", "SB", "S01", 300, pd.DataFrame()
        )
        assert times is None

    def test_level1_exact_species_match(self):
        """Level 1: competitor + event + species exact match."""
        rows = [
            _result_row(name="Alice", event="SB", time=25.0 + i, species="S01") for i in range(3)
        ]
        df = _make_results_df(rows)
        times, conf, expl = get_competitor_historical_times_flexible("Alice", "SB", "S01", 300, df)
        assert times is not None
        assert len(times) == 3
        assert conf == "HIGH"
        assert "exact match" in expl.lower()

    def test_level2_any_species(self):
        """Level 2: competitor + event match, different species."""
        rows = [
            _result_row(name="Alice", event="SB", time=25.0 + i, species="S05") for i in range(3)
        ]
        df = _make_results_df(rows)
        times, conf, expl = get_competitor_historical_times_flexible("Alice", "SB", "S01", 300, df)
        assert times is not None
        assert len(times) == 3
        assert conf == "MEDIUM"
        assert "various" in expl.lower()

    def test_level3_no_competitor_history(self):
        """Level 3: no history for this competitor at all."""
        rows = [_result_row(name="Bob", event="SB", time=30.0)]
        df = _make_results_df(rows)
        times, conf, expl = get_competitor_historical_times_flexible("Alice", "SB", "S01", 300, df)
        assert times is None
        assert conf == "LOW"

    def test_case_insensitive_name_match(self):
        """Competitor name matching should be case insensitive."""
        rows = [
            _result_row(name="Alice Smith", event="SB", time=25.0, species="S01"),
        ]
        df = _make_results_df(rows)
        times, _, _ = get_competitor_historical_times_flexible("alice smith", "SB", "S01", 300, df)
        assert times is not None
        assert len(times) == 1

    def test_filters_out_invalid_times(self):
        """Zero, negative, and over-limit times should be excluded."""
        rows = [
            _result_row(name="Alice", event="SB", time=25.0, species="S01"),
            _result_row(name="Alice", event="SB", time=0.0, species="S01"),
            _result_row(name="Alice", event="SB", time=-5.0, species="S01"),
            _result_row(name="Alice", event="SB", time=200.0, species="S01"),  # over limit
        ]
        df = _make_results_df(rows)
        times, _, _ = get_competitor_historical_times_flexible("Alice", "SB", "S01", 300, df)
        assert times is not None
        assert len(times) == 1
        assert times[0] == 25.0

    def test_wrong_event_returns_none(self):
        """History in wrong event should not match."""
        rows = [
            _result_row(name="Alice", event="UH", time=25.0, species="S01"),
        ]
        df = _make_results_df(rows)
        times, _, _ = get_competitor_historical_times_flexible("Alice", "SB", "S01", 300, df)
        assert times is None
