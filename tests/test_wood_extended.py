"""Extended tests for strathmark/wood.py — scaling, species multipliers, calibration."""

import numpy as np
import pandas as pd

from strathmark.wood import (
    DEFAULT_SCALING_EXPONENT,
    DEFAULT_SCALING_EXPONENT_SB,
    DEFAULT_SCALING_EXPONENT_UH,
    SPECIES_TIME_MULTIPLIERS,
    ScalingMetadata,
    _event_exponent_cache,
    adjust_confidence_for_scaling,
    apply_quality_multiplier_statistical,
    calculate_effective_janka_hardness,
    calibrate_scaling_exponent,
    estimate_species_multiplier_from_shear,
    get_event_scaling_exponent,
    get_species_properties,
    get_species_time_multiplier,
    scale_time,
    scale_time_list,
)

# ---------------------------------------------------------------------------
# get_species_time_multiplier
# ---------------------------------------------------------------------------


class TestGetSpeciesTimeMultiplier:
    def test_s01_is_reference(self):
        assert get_species_time_multiplier("S01") == 1.0

    def test_case_insensitive_code(self):
        assert get_species_time_multiplier("s01") == 1.0
        assert get_species_time_multiplier("s05") == SPECIES_TIME_MULTIPLIERS["S05"]

    def test_by_name(self):
        mult = get_species_time_multiplier("ponderosa pine")
        assert mult == SPECIES_TIME_MULTIPLIERS["S05"]

    def test_unknown_species_returns_1(self):
        assert get_species_time_multiplier("unicorn_wood") == 1.0

    def test_all_known_species_positive(self):
        for code, mult in SPECIES_TIME_MULTIPLIERS.items():
            assert mult > 0, f"{code} has non-positive multiplier {mult}"

    def test_whitespace_handling(self):
        assert get_species_time_multiplier("  S01  ") == 1.0


# ---------------------------------------------------------------------------
# estimate_species_multiplier_from_shear
# ---------------------------------------------------------------------------


class TestEstimateSpeciesMultiplierFromShear:
    def test_known_species_returns_empirical(self):
        """For known species, should return the empirical multiplier, not shear estimate."""
        mult = estimate_species_multiplier_from_shear("S05")
        assert mult == SPECIES_TIME_MULTIPLIERS["S05"]

    def test_unknown_species_uses_shear(self):
        """Unknown species should use shear-strength regression."""
        mult = estimate_species_multiplier_from_shear("unknown_wood")
        # Should compute from default shear props (1000/900)^0.97
        assert mult > 0


# ---------------------------------------------------------------------------
# scale_time
# ---------------------------------------------------------------------------


class TestScaleTime:
    def test_same_diameter_no_scaling(self):
        scaled, note = scale_time(30.0, 300, 300)
        assert scaled == 30.0
        assert note == ""

    def test_smaller_target_reduces_time(self):
        scaled, note = scale_time(30.0, 300, 250)
        assert scaled < 30.0

    def test_larger_target_increases_time(self):
        scaled, note = scale_time(30.0, 300, 400)
        assert scaled > 30.0

    def test_large_diff_downgrades_confidence_two(self):
        """Diameter difference > 50mm -> downgrade_two."""
        _, note = scale_time(30.0, 300, 400)  # 100mm diff
        assert note == "downgrade_two"

    def test_moderate_diff_downgrades_confidence_one(self):
        """Diameter difference > 25mm but <= 50mm -> downgrade_one."""
        _, note = scale_time(30.0, 300, 340)  # 40mm diff
        assert note == "downgrade_one"

    def test_small_diff_no_downgrade(self):
        """Diameter difference <= 25mm -> no confidence note."""
        _, note = scale_time(30.0, 300, 315)  # 15mm diff
        assert note == ""


# ---------------------------------------------------------------------------
# scale_time_list
# ---------------------------------------------------------------------------


class TestScaleTimeList:
    def test_empty_list(self):
        scaled, meta = scale_time_list([], 300, 350)
        assert scaled == []
        assert isinstance(meta, ScalingMetadata)

    def test_scales_all_times(self):
        times = [20.0, 30.0, 40.0]
        scaled, meta = scale_time_list(times, 300, 400)
        assert len(scaled) == 3
        for s, t in zip(scaled, times):
            assert s > t  # larger diameter -> larger times

    def test_metadata_populated_when_scaled(self):
        _, meta = scale_time_list([30.0], 300, 400)
        assert meta.was_scaled is True
        assert meta.original_diameter == 300
        assert meta.target_diameter == 400
        assert meta.scaling_factor > 1.0
        assert "Scaled" in meta.warning_message

    def test_metadata_no_scaling_within_tolerance(self):
        _, meta = scale_time_list([30.0], 300, 305)
        assert meta.was_scaled is False
        assert meta.original_diameter is None
        assert meta.warning_message == ""

    def test_confidence_downgrade_for_large_diff(self):
        _, meta = scale_time_list([30.0], 300, 400)  # 100mm diff
        assert meta.confidence_adjustment == "downgrade"


# ---------------------------------------------------------------------------
# adjust_confidence_for_scaling
# ---------------------------------------------------------------------------


class TestAdjustConfidenceForScaling:
    def test_no_downgrade_preserves_confidence(self):
        meta = ScalingMetadata(True, 300, 315, 1.05, "", "")
        assert adjust_confidence_for_scaling("HIGH", meta) == "HIGH"

    def test_downgrade_high_to_medium(self):
        meta = ScalingMetadata(True, 300, 400, 1.5, "downgrade", "big diff")
        assert adjust_confidence_for_scaling("HIGH", meta) == "MEDIUM"

    def test_downgrade_medium_to_low(self):
        meta = ScalingMetadata(True, 300, 400, 1.5, "downgrade", "big diff")
        assert adjust_confidence_for_scaling("MEDIUM", meta) == "LOW"

    def test_downgrade_low_stays_low(self):
        meta = ScalingMetadata(True, 300, 400, 1.5, "downgrade", "big diff")
        assert adjust_confidence_for_scaling("LOW", meta) == "LOW"


# ---------------------------------------------------------------------------
# calibrate_scaling_exponent
# ---------------------------------------------------------------------------


class TestCalibrateScalingExponent:
    def test_none_df_returns_none(self):
        assert calibrate_scaling_exponent(None, "SB") is None

    def test_empty_df_returns_none(self):
        assert calibrate_scaling_exponent(pd.DataFrame(), "SB") is None

    def test_insufficient_data_returns_none(self):
        df = pd.DataFrame(
            {
                "competitor_name": ["A", "A"],
                "event": ["SB", "SB"],
                "raw_time": [30.0, 35.0],
                "size_mm": [300, 350],
            }
        )
        # Only 2 rows, need min 5
        result = calibrate_scaling_exponent(df, "SB")
        assert result is None

    def test_single_diameter_returns_none(self):
        """If all results are at same diameter, can't compute exponent."""
        df = pd.DataFrame(
            {
                "competitor_name": ["A"] * 10,
                "event": ["SB"] * 10,
                "raw_time": [30.0 + i for i in range(10)],
                "size_mm": [300] * 10,
            }
        )
        result = calibrate_scaling_exponent(df, "SB")
        assert result is None

    def test_multi_diameter_returns_reasonable_exponent(self):
        """Multiple competitors with multiple diameters should produce a valid exponent."""
        rows = []
        # Generate data where time ∝ diameter^1.5
        for comp in ["A", "B", "C", "D"]:
            for d in [275, 300, 325, 350]:
                base = 30.0 if comp in ["A", "B"] else 40.0
                time = base * (d / 300.0) ** 1.5
                rows.append(
                    {
                        "competitor_name": comp,
                        "event": "SB",
                        "raw_time": time,
                        "size_mm": d,
                    }
                )
        df = pd.DataFrame(rows)
        result = calibrate_scaling_exponent(df, "SB")
        assert result is not None
        assert 0.5 < result < 3.0
        # Should be close to 1.5
        assert abs(result - 1.5) < 0.5

    def test_wrong_event_ignored(self):
        """Only data for the requested event should be used."""
        rows = []
        for comp in ["A", "B", "C", "D"]:
            for d in [275, 300, 325, 350]:
                rows.append(
                    {
                        "competitor_name": comp,
                        "event": "UH",
                        "raw_time": 30.0 * (d / 300.0) ** 2.0,
                        "size_mm": d,
                    }
                )
        df = pd.DataFrame(rows)
        # Request SB, but all data is UH
        result = calibrate_scaling_exponent(df, "SB")
        assert result is None


# ---------------------------------------------------------------------------
# get_event_scaling_exponent with cache
# ---------------------------------------------------------------------------


class TestGetEventScalingExponentCache:
    def setup_method(self):
        """Clear cache before each test."""
        _event_exponent_cache.clear()

    def test_sb_returns_default_without_data(self):
        exp = get_event_scaling_exponent(None, "SB")
        assert exp == DEFAULT_SCALING_EXPONENT_SB

    def test_uh_returns_default_without_data(self):
        exp = get_event_scaling_exponent(None, "UH")
        assert exp == DEFAULT_SCALING_EXPONENT_UH

    def test_unknown_event_returns_generic_default(self):
        exp = get_event_scaling_exponent(None, "UNKNOWN")
        assert exp == DEFAULT_SCALING_EXPONENT


# ---------------------------------------------------------------------------
# Quality edge cases
# ---------------------------------------------------------------------------


class TestQualityEdgeCases:
    def test_quality_clamped_below_1(self):
        """Quality 0 should be clamped to 1."""
        result = calculate_effective_janka_hardness("S01", 0)
        q1 = calculate_effective_janka_hardness("S01", 1)
        assert result == q1

    def test_quality_clamped_above_10(self):
        """Quality 15 should be clamped to 10."""
        result = calculate_effective_janka_hardness("S01", 15)
        q10 = calculate_effective_janka_hardness("S01", 10)
        assert result == q10

    def test_statistical_quality_clamped(self):
        """Quality multiplier should clamp at boundaries."""
        result_low = apply_quality_multiplier_statistical(100.0, -5)
        result_high = apply_quality_multiplier_statistical(100.0, 20)
        q1 = apply_quality_multiplier_statistical(100.0, 1)
        q10 = apply_quality_multiplier_statistical(100.0, 10)
        assert result_low == q1
        assert result_high == q10


# ---------------------------------------------------------------------------
# Species properties from DataFrame
# ---------------------------------------------------------------------------


class TestSpeciesPropertiesFromDf:
    def test_lookup_by_species_name(self):
        wood_df = pd.DataFrame(
            {
                "species": ["TestWood"],
                "speciesID": ["T01"],
                "janka_hard": [2000.0],
                "spec_gravity": [0.5],
                "shear": [1200.0],
                "crush_strength": [5000.0],
                "MOR": [9000.0],
                "MOE": [1200000.0],
            }
        )
        props = get_species_properties("TestWood", wood_df)
        assert props.janka_hardness == 2000.0
        assert props.specific_gravity == 0.5

    def test_lookup_by_species_id(self):
        wood_df = pd.DataFrame(
            {
                "species": ["TestWood"],
                "speciesID": ["T01"],
                "janka_hard": [2000.0],
                "spec_gravity": [0.5],
                "shear": [1200.0],
                "crush_strength": [5000.0],
                "MOR": [9000.0],
                "MOE": [1200000.0],
            }
        )
        props = get_species_properties("T01", wood_df)
        assert props.janka_hardness == 2000.0

    def test_nan_values_fallback_to_defaults(self):
        wood_df = pd.DataFrame(
            {
                "species": ["NanWood"],
                "janka_hard": [float("nan")],
                "spec_gravity": [float("nan")],
                "shear": [float("nan")],
                "crush_strength": [float("nan")],
                "MOR": [float("nan")],
                "MOE": [float("nan")],
            }
        )
        props = get_species_properties("NanWood", wood_df)
        # Should fall back to defaults, not NaN
        assert not np.isnan(props.janka_hardness)
        assert not np.isnan(props.specific_gravity)
