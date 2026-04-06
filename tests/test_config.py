"""Tests for strathmark/config.py — configuration constants and invariants."""

import pytest

from strathmark.config import (
    decay_config,
    get_confidence_level,
    get_event_encoding,
    rules,
    sim_config,
)


class TestRules:
    def test_mark_floor_is_3(self):
        assert rules.MIN_MARK_SECONDS == 3

    def test_system_ceiling_is_183(self):
        assert rules.MAX_MARK_SECONDS == 183

    def test_ceiling_above_floor(self):
        assert rules.MAX_MARK_SECONDS > rules.MIN_MARK_SECONDS

    def test_time_limit_is_180(self):
        assert rules.MAX_TIME_LIMIT_SECONDS == 180

    def test_ceiling_equals_time_limit_plus_floor(self):
        assert rules.MAX_MARK_SECONDS == rules.MAX_TIME_LIMIT_SECONDS + rules.MIN_MARK_SECONDS

    def test_frozen(self):
        with pytest.raises(AttributeError):
            rules.MIN_MARK_SECONDS = 5


class TestSimConfig:
    def test_num_simulations_positive(self):
        assert sim_config.NUM_SIMULATIONS > 0
        assert sim_config.NUM_SIMULATIONS_QUICK > 0

    def test_quick_fewer_than_full(self):
        assert sim_config.NUM_SIMULATIONS_QUICK < sim_config.NUM_SIMULATIONS

    def test_heat_variance_positive(self):
        assert sim_config.HEAT_VARIANCE_SECONDS > 0

    def test_min_std_below_max(self):
        assert sim_config.MIN_COMPETITOR_STD_SECONDS < sim_config.MAX_COMPETITOR_STD_SECONDS

    def test_variance_scaling_factor_in_range(self):
        assert 0 < sim_config.DEFAULT_VARIANCE_SCALING_FACTOR < 1.0

    def test_frozen(self):
        with pytest.raises(AttributeError):
            sim_config.NUM_SIMULATIONS = 0


class TestDecayConfig:
    def test_half_lives_ordered(self):
        assert (
            decay_config.HALF_LIFE_ACTIVE_DAYS
            < decay_config.HALF_LIFE_MODERATE_DAYS
            < decay_config.HALF_LIFE_INACTIVE_DAYS
        )

    def test_all_positive(self):
        assert decay_config.HALF_LIFE_ACTIVE_DAYS > 0
        assert decay_config.HALF_LIFE_MODERATE_DAYS > 0
        assert decay_config.HALF_LIFE_INACTIVE_DAYS > 0


class TestEventEncoding:
    def test_sb_and_uh_exist(self):
        assert get_event_encoding("SB") is not None
        assert get_event_encoding("UH") is not None

    def test_unknown_event_raises(self):
        with pytest.raises(ValueError, match="Invalid event code"):
            get_event_encoding("INVALID")


class TestConfidenceLevel:
    def test_returns_string(self):
        level = get_confidence_level(5)
        assert isinstance(level, str)
        assert level in ("VERY HIGH", "HIGH", "MEDIUM", "LOW", "VERY LOW")
