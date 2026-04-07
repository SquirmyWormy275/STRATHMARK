"""Tests for strathmark/config.py — configuration constants and invariants."""

import importlib

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


class TestLLMConfigEnvOverrides:
    """LLMConfig reads STRATHMARK_OLLAMA_* env vars at module-import time.

    These overrides exist so deployments without a reachable Ollama (e.g. the
    Pro-Am Manager on Railway) can dial timeouts and retries down to fail-fast
    through the LLM cascade level instead of hanging for minutes.
    """

    def _reload_config(self):
        import strathmark.config as cfg

        return importlib.reload(cfg)

    def test_defaults_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("STRATHMARK_OLLAMA_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_OLLAMA_TIMEOUT", raising=False)
        monkeypatch.delenv("STRATHMARK_OLLAMA_MAX_RETRIES", raising=False)
        cfg = self._reload_config()
        assert cfg.llm_config.OLLAMA_URL == "http://localhost:11434/api/generate"
        assert cfg.llm_config.TIMEOUT_SECONDS == 30
        assert cfg.llm_config.MAX_RETRIES == 2

    def test_url_override(self, monkeypatch):
        monkeypatch.setenv("STRATHMARK_OLLAMA_URL", "http://ollama.internal:9999/api/generate")
        cfg = self._reload_config()
        assert cfg.llm_config.OLLAMA_URL == "http://ollama.internal:9999/api/generate"

    def test_timeout_override(self, monkeypatch):
        monkeypatch.setenv("STRATHMARK_OLLAMA_TIMEOUT", "2")
        cfg = self._reload_config()
        assert cfg.llm_config.TIMEOUT_SECONDS == 2

    def test_max_retries_override(self, monkeypatch):
        monkeypatch.setenv("STRATHMARK_OLLAMA_MAX_RETRIES", "0")
        cfg = self._reload_config()
        assert cfg.llm_config.MAX_RETRIES == 0

    def test_invalid_int_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("STRATHMARK_OLLAMA_TIMEOUT", "not-a-number")
        cfg = self._reload_config()
        assert cfg.llm_config.TIMEOUT_SECONDS == 30

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("STRATHMARK_OLLAMA_URL", "")
        cfg = self._reload_config()
        assert cfg.llm_config.OLLAMA_URL == "http://localhost:11434/api/generate"

    def teardown_method(self):
        # Restore the unmodified module so other tests are not affected
        import strathmark.config as cfg

        importlib.reload(cfg)
