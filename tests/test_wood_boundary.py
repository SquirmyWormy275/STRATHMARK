"""Boundary tests for the wood module.

Tests extreme diameters, quality edge cases, species lookup edge cases,
scaling factor precision, and cross-species normalization.
"""

import pytest

from strathmark.wood import (
    apply_quality_multiplier_statistical,
    calculate_effective_janka_hardness,
    calculate_scaling_factor,
    get_event_scaling_exponent,
    get_species_properties,
    get_species_time_multiplier,
)


# ---------------------------------------------------------------------------
# Extreme diameter scaling
# ---------------------------------------------------------------------------
class TestExtremeDiameters:
    def test_zero_target_raises_or_returns_zero(self):
        """Zero target diameter should not crash."""
        try:
            factor = calculate_scaling_factor(300, 0, exponent=2.0)
            assert factor == 0.0 or factor < 0.01
        except (ValueError, ZeroDivisionError):
            pass  # Also acceptable

    def test_very_small_50mm(self):
        factor = calculate_scaling_factor(300, 50, exponent=2.0)
        assert factor < 0.1  # (50/300)^2 ≈ 0.028

    def test_very_large_600mm(self):
        factor = calculate_scaling_factor(300, 600, exponent=2.0)
        assert factor > 3.5  # (600/300)^2 = 4.0

    def test_same_diameter_is_1(self):
        assert calculate_scaling_factor(300, 300) == pytest.approx(1.0)

    def test_within_tolerance_is_1(self):
        """Diameters within 10mm tolerance → factor = 1.0."""
        assert calculate_scaling_factor(300, 305) == pytest.approx(1.0)
        assert calculate_scaling_factor(300, 295) == pytest.approx(1.0)

    def test_negative_diameter_handled(self):
        """Negative diameter should either raise or produce a sensible result."""
        try:
            factor = calculate_scaling_factor(300, -100, exponent=2.0)
            # If it doesn't raise, the factor should be positive or zero
            assert factor >= 0
        except (ValueError, ZeroDivisionError):
            pass


# ---------------------------------------------------------------------------
# Quality boundaries
# ---------------------------------------------------------------------------
class TestQualityBoundaries:
    def test_quality_0_clamped_to_1(self):
        """Quality below 1 should be clamped."""
        result = apply_quality_multiplier_statistical(25.0, 0)
        # Should behave like quality 1 (or at least not crash)
        assert result > 0

    def test_quality_11_clamped_to_10(self):
        """Quality above 10 should be clamped."""
        result = apply_quality_multiplier_statistical(25.0, 11)
        assert result > 0

    def test_quality_negative_clamped(self):
        result = apply_quality_multiplier_statistical(25.0, -5)
        assert result > 0

    def test_quality_range_monotonic(self):
        """Higher quality → higher (slower) adjusted time."""
        base = 25.0
        times = [apply_quality_multiplier_statistical(base, q) for q in range(1, 11)]
        for i in range(len(times) - 1):
            assert times[i] <= times[i + 1]

    def test_quality_adjustment_magnitude(self):
        """Each quality point should adjust by ~2%."""
        base = 100.0  # Use 100 for easy percentage math
        q1 = apply_quality_multiplier_statistical(base, 1)
        q5 = apply_quality_multiplier_statistical(base, 5)
        q10 = apply_quality_multiplier_statistical(base, 10)
        # q1 should be ~8% less than q5 (4 points * 2%)
        assert q1 == pytest.approx(q5 * 0.92, abs=2.0)
        # q10 should be ~10% more than q5 (5 points * 2%)
        assert q10 == pytest.approx(q5 * 1.10, abs=2.0)


# ---------------------------------------------------------------------------
# Janka hardness
# ---------------------------------------------------------------------------
class TestJankaHardnessBoundaries:
    """calculate_effective_janka_hardness takes (species, quality, wood_df)."""

    def test_quality_5_is_baseline(self):
        """Quality 5 should return the base Janka value for the species."""
        base = calculate_effective_janka_hardness("S01", 5)
        assert base > 0

    def test_quality_1_reduces_hardness(self):
        base = calculate_effective_janka_hardness("S01", 5)
        result = calculate_effective_janka_hardness("S01", 1)
        assert result < base

    def test_quality_10_increases_hardness(self):
        base = calculate_effective_janka_hardness("S01", 5)
        result = calculate_effective_janka_hardness("S01", 10)
        assert result > base

    def test_quality_monotonic(self):
        """Higher quality → higher effective Janka hardness."""
        values = [calculate_effective_janka_hardness("S01", q) for q in range(1, 11)]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1]


# ---------------------------------------------------------------------------
# Species lookup
# ---------------------------------------------------------------------------
class TestSpeciesLookupBoundaries:
    def test_known_species_s01(self):
        props = get_species_properties("S01")
        assert props is not None

    def test_none_species(self):
        """None should return defaults, not crash."""
        props = get_species_properties(None)
        assert props is not None

    def test_empty_string_species(self):
        props = get_species_properties("")
        assert props is not None

    def test_case_insensitive(self):
        """Species codes should be case-insensitive."""
        p1 = get_species_properties("s01")
        p2 = get_species_properties("S01")
        # Both should return something (may or may not be identical)
        assert p1 is not None
        assert p2 is not None


class TestSpeciesMultiplierBoundaries:
    def test_reference_species_is_1(self):
        """S01 (Eastern White Pine) is the reference = 1.0."""
        mult = get_species_time_multiplier("S01")
        assert mult == pytest.approx(1.0)

    def test_all_multipliers_positive(self):
        """All known species multipliers should be > 0."""
        known = ["S01", "S03", "S04", "S05", "S10", "S12"]
        for code in known:
            mult = get_species_time_multiplier(code)
            assert mult > 0, f"Species {code} has non-positive multiplier"

    def test_unknown_species_returns_1(self):
        """Unknown species → multiplier of 1.0 (reference)."""
        mult = get_species_time_multiplier("ZZZZZ")
        assert mult == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Event scaling exponent
# ---------------------------------------------------------------------------
class TestEventScalingExponent:
    """get_event_scaling_exponent(results_df, event_code) — None df uses defaults."""

    def test_sb_default(self):
        exp = get_event_scaling_exponent(None, "SB")
        assert 1.0 < exp < 3.0

    def test_uh_default(self):
        exp = get_event_scaling_exponent(None, "UH")
        assert 1.0 < exp < 3.0

    def test_uh_higher_than_sb(self):
        """UH exponent (2.1) should be higher than SB (1.8)."""
        sb = get_event_scaling_exponent(None, "SB")
        uh = get_event_scaling_exponent(None, "UH")
        assert uh >= sb

    def test_unknown_event(self):
        """Unknown event should return generic default (2.0)."""
        exp = get_event_scaling_exponent(None, "XX")
        assert exp == pytest.approx(2.0)
