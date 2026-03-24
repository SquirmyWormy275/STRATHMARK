"""Tests for strathmark/wood.py — species properties, scaling, quality."""

import pytest

from strathmark.wood import (
    calculate_scaling_factor,
    calculate_effective_janka_hardness,
    apply_quality_multiplier_statistical,
    get_event_scaling_exponent,
    get_species_properties,
    DEFAULT_SCALING_EXPONENT,
)


class TestGetSpeciesProperties:

    def test_known_species_returns_properties(self):
        props = get_species_properties("poplar")
        assert props.janka_hardness > 0
        assert props.specific_gravity > 0

    def test_unknown_species_returns_defaults(self):
        props = get_species_properties("unicorn_wood")
        # Should return S01 (Eastern White Pine) defaults
        assert props.janka_hardness > 0

    def test_none_species_returns_defaults(self):
        props = get_species_properties(None)
        assert props.janka_hardness > 0


class TestCalculateScalingFactor:

    def test_same_diameter_returns_1(self):
        assert calculate_scaling_factor(300, 300) == 1.0

    def test_within_tolerance_returns_1(self):
        # 10mm tolerance
        assert calculate_scaling_factor(300, 305) == 1.0

    def test_larger_diameter_increases_time(self):
        factor = calculate_scaling_factor(300, 350)
        assert factor > 1.0

    def test_smaller_diameter_decreases_time(self):
        factor = calculate_scaling_factor(300, 250)
        assert factor < 1.0

    def test_custom_exponent(self):
        factor_low = calculate_scaling_factor(300, 400, exponent=1.0)
        factor_high = calculate_scaling_factor(300, 400, exponent=2.0)
        assert factor_high > factor_low


class TestCalculateEffectiveJankaHardness:

    def test_quality_5_is_baseline(self):
        base = get_species_properties("poplar").janka_hardness
        effective = calculate_effective_janka_hardness("poplar", 5)
        assert abs(effective - base) < 1.0

    def test_quality_1_reduces_hardness(self):
        q5 = calculate_effective_janka_hardness("poplar", 5)
        q1 = calculate_effective_janka_hardness("poplar", 1)
        assert q1 < q5

    def test_quality_10_increases_hardness(self):
        q5 = calculate_effective_janka_hardness("poplar", 5)
        q10 = calculate_effective_janka_hardness("poplar", 10)
        assert q10 > q5


class TestApplyQualityMultiplierStatistical:

    def test_quality_5_no_change(self):
        result = apply_quality_multiplier_statistical(50.0, 5)
        assert result == 50.0

    def test_quality_1_reduces_time(self):
        result = apply_quality_multiplier_statistical(50.0, 1)
        assert result < 50.0

    def test_quality_10_increases_time(self):
        result = apply_quality_multiplier_statistical(50.0, 10)
        assert result > 50.0

    def test_adjustment_is_2pct_per_point(self):
        base = 100.0
        q6 = apply_quality_multiplier_statistical(base, 6)
        assert abs(q6 - 102.0) < 0.1  # +2% for 1 point above 5


class TestGetEventScalingExponent:

    def test_sb_default(self):
        exp = get_event_scaling_exponent(None, "SB")
        assert 1.0 < exp < 3.0

    def test_uh_default(self):
        exp = get_event_scaling_exponent(None, "UH")
        assert 1.0 < exp < 3.0

    def test_unknown_event_uses_generic_default(self):
        exp = get_event_scaling_exponent(None, "UNKNOWN")
        assert exp == DEFAULT_SCALING_EXPONENT
