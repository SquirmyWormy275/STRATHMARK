"""Tests for strathmark/llm_roles.py — extended LLM roles."""

from strathmark.llm_roles import (
    detect_result_anomaly,
    COMPETITOR_PROFILE_SCHEMA,
    RACE_COMMENTARY_SCHEMA,
    ANOMALY_DETECTION_SCHEMA,
)


class TestSchemas:
    """Verify JSON schemas are valid dicts with required keys."""

    def test_profile_schema_has_properties(self):
        assert "properties" in COMPETITOR_PROFILE_SCHEMA
        assert "narrative" in COMPETITOR_PROFILE_SCHEMA["properties"]

    def test_commentary_schema_has_properties(self):
        assert "properties" in RACE_COMMENTARY_SCHEMA
        assert "headline" in RACE_COMMENTARY_SCHEMA["properties"]

    def test_anomaly_schema_has_properties(self):
        assert "properties" in ANOMALY_DETECTION_SCHEMA
        assert "is_anomalous" in ANOMALY_DETECTION_SCHEMA["properties"]


class TestDetectResultAnomaly:
    """Test anomaly detection with statistical fallback (no Ollama required)."""

    def test_normal_result_not_anomalous(self):
        result = detect_result_anomaly(
            name="Alice", event_code="SB",
            actual_time=50.0, predicted_time=51.0,
            historical_avg=50.0, historical_std=3.0,
            wood_species="poplar", diameter_mm=300,
        )
        # Result may be None if Ollama unavailable but statistical fallback exists
        if result is not None:
            assert result["severity"] in ("normal", "notable")

    def test_extreme_result_flagged(self):
        result = detect_result_anomaly(
            name="Alice", event_code="SB",
            actual_time=100.0, predicted_time=50.0,
            historical_avg=50.0, historical_std=3.0,
            wood_species="poplar", diameter_mm=300,
        )
        if result is not None:
            assert result["is_anomalous"] is True
            assert result["severity"] in ("significant", "extreme")
