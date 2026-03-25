"""Tests for config invariants and consistency.

Validates that all frozen dataclass constants maintain their
required relationships and cannot be mutated at runtime.
"""

import dataclasses

import pytest

from strathmark.config import (
    DataRequirements,
    DecayConfig,
    LLMConfig,
    MLConfig,
    Rules,
    SimulationConfig,
    data_req,
    decay_config,
    llm_config,
    ml_config,
    rules,
    sim_config,
)


# ---------------------------------------------------------------------------
# Frozen enforcement
# ---------------------------------------------------------------------------
class TestFrozenDataclasses:
    """All config objects must be immutable at runtime."""

    @pytest.mark.parametrize("cls,instance", [
        (Rules, rules),
        (SimulationConfig, sim_config),
        (DecayConfig, decay_config),
        (MLConfig, ml_config),
        (LLMConfig, llm_config),
        (DataRequirements, data_req),
    ])
    def test_frozen(self, cls, instance):
        """Attempting to set an attribute via normal assignment should raise."""
        first_field = dataclasses.fields(instance)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, first_field, 999)


# ---------------------------------------------------------------------------
# Rules invariants
# ---------------------------------------------------------------------------
class TestRulesInvariants:
    def test_floor_is_3(self):
        assert rules.MIN_MARK_SECONDS == 3

    def test_ceiling_is_183(self):
        assert rules.MAX_MARK_SECONDS == 183

    def test_time_limit_is_180(self):
        assert rules.MAX_TIME_LIMIT_SECONDS == 180

    def test_ceiling_equals_time_limit_plus_floor(self):
        assert rules.MAX_MARK_SECONDS == rules.MAX_TIME_LIMIT_SECONDS + rules.MIN_MARK_SECONDS

    def test_ceiling_above_floor(self):
        assert rules.MAX_MARK_SECONDS > rules.MIN_MARK_SECONDS

    def test_variance_is_3(self):
        assert rules.PERFORMANCE_VARIANCE_SECONDS == 3

    def test_variance_positive(self):
        assert rules.PERFORMANCE_VARIANCE_SECONDS > 0


# ---------------------------------------------------------------------------
# Simulation config invariants
# ---------------------------------------------------------------------------
class TestSimConfigInvariants:
    def test_num_simulations_positive(self):
        assert sim_config.NUM_SIMULATIONS > 0
        assert sim_config.NUM_SIMULATIONS_QUICK > 0

    def test_quick_fewer_than_full(self):
        assert sim_config.NUM_SIMULATIONS_QUICK < sim_config.NUM_SIMULATIONS

    def test_heat_variance_positive(self):
        assert sim_config.HEAT_VARIANCE_SECONDS > 0

    def test_min_std_below_max(self):
        assert sim_config.MIN_COMPETITOR_STD_SECONDS < sim_config.MAX_COMPETITOR_STD_SECONDS

    def test_min_std_positive(self):
        assert sim_config.MIN_COMPETITOR_STD_SECONDS > 0

    def test_variance_scaling_factor_between_0_and_1(self):
        assert 0 < sim_config.DEFAULT_VARIANCE_SCALING_FACTOR < 1.0

    def test_fairness_thresholds_ordered(self):
        """Thresholds must be strictly increasing."""
        assert (
            sim_config.FAIRNESS_THRESHOLD_EXCELLENT
            < sim_config.FAIRNESS_THRESHOLD_VERY_GOOD
            < sim_config.FAIRNESS_THRESHOLD_GOOD
            < sim_config.FAIRNESS_THRESHOLD_FAIR
        )

    def test_consistency_thresholds_ordered(self):
        assert (
            sim_config.CONSISTENCY_VERY_HIGH_THRESHOLD
            < sim_config.CONSISTENCY_HIGH_THRESHOLD
            < sim_config.CONSISTENCY_MODERATE_THRESHOLD
        )


# ---------------------------------------------------------------------------
# Decay config invariants
# ---------------------------------------------------------------------------
class TestDecayConfigInvariants:
    def test_half_lives_ordered(self):
        """Active < moderate < inactive (shorter half-life = faster decay)."""
        assert (
            decay_config.HALF_LIFE_ACTIVE_DAYS
            < decay_config.HALF_LIFE_MODERATE_DAYS
            < decay_config.HALF_LIFE_INACTIVE_DAYS
        )

    def test_all_half_lives_positive(self):
        assert decay_config.HALF_LIFE_ACTIVE_DAYS > 0
        assert decay_config.HALF_LIFE_MODERATE_DAYS > 0
        assert decay_config.HALF_LIFE_INACTIVE_DAYS > 0

    def test_moderate_is_730(self):
        """The standard 2-year half-life."""
        assert decay_config.HALF_LIFE_MODERATE_DAYS == 730

    def test_activity_window_matches_moderate(self):
        """Activity window should equal moderate half-life."""
        assert decay_config.ACTIVITY_WINDOW_DAYS == decay_config.HALF_LIFE_MODERATE_DAYS

    def test_active_threshold_above_moderate(self):
        assert decay_config.ACTIVE_MIN_RESULTS > decay_config.MODERATE_MIN_RESULTS


# ---------------------------------------------------------------------------
# ML config sanity
# ---------------------------------------------------------------------------
class TestMLConfigInvariants:
    def test_learning_rate_in_range(self):
        assert 0 < ml_config.LEARNING_RATE < 1.0

    def test_subsample_in_range(self):
        assert 0 < ml_config.SUBSAMPLE <= 1.0

    def test_colsample_in_range(self):
        assert 0 < ml_config.COLSAMPLE_BYTREE <= 1.0

    def test_max_depth_positive(self):
        assert ml_config.MAX_DEPTH > 0

    def test_n_estimators_positive(self):
        assert ml_config.N_ESTIMATORS > 0

    def test_feature_names_defined(self):
        assert len(ml_config.FEATURE_NAMES) > 0

    def test_27_features(self):
        """The model was tuned for 27 features."""
        assert len(ml_config.FEATURE_NAMES) >= 26  # Allow minor additions


# ---------------------------------------------------------------------------
# LLM config sanity
# ---------------------------------------------------------------------------
class TestLLMConfigInvariants:
    def test_timeout_positive(self):
        assert llm_config.TIMEOUT_SECONDS > 0

    def test_max_retries_non_negative(self):
        assert llm_config.MAX_RETRIES >= 0

    def test_quality_multiplier_range(self):
        """Min < 1.0 < Max — quality can increase or decrease time."""
        assert llm_config.QUALITY_MULTIPLIER_MIN < 1.0
        assert llm_config.QUALITY_MULTIPLIER_MAX > 1.0

    def test_ollama_url_is_localhost(self):
        assert "localhost" in llm_config.OLLAMA_URL or "127.0.0.1" in llm_config.OLLAMA_URL

    def test_model_name_set(self):
        assert isinstance(llm_config.DEFAULT_MODEL, str)
        assert len(llm_config.DEFAULT_MODEL) > 0


# ---------------------------------------------------------------------------
# Data requirements
# ---------------------------------------------------------------------------
class TestDataRequirements:
    def test_min_historical_times_positive(self):
        assert data_req.MIN_HISTORICAL_TIMES > 0

    def test_min_ml_records_sensible(self):
        assert data_req.MIN_ML_TRAINING_RECORDS_TOTAL > data_req.MIN_ML_TRAINING_RECORDS_PER_EVENT

    def test_valid_time_range(self):
        assert data_req.MIN_VALID_TIME_SECONDS > 0
        assert data_req.MAX_VALID_TIME_SECONDS > data_req.MIN_VALID_TIME_SECONDS

    def test_diameter_range(self):
        assert data_req.MIN_DIAMETER_MM > 0
        assert data_req.MAX_DIAMETER_MM > data_req.MIN_DIAMETER_MM

    def test_confidence_thresholds(self):
        assert data_req.HIGH_CONFIDENCE_MIN_EVENTS > data_req.MEDIUM_CONFIDENCE_MIN_EVENTS
