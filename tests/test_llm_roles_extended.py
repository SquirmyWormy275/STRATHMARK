"""Extended tests for strathmark/llm_roles.py — profile generation, commentary, anomaly edge cases."""

from unittest.mock import patch
import json

import pytest

from strathmark.llm_roles import (
    generate_competitor_profile,
    generate_race_commentary,
    detect_result_anomaly,
    COMPETITOR_PROFILE_SCHEMA,
    RACE_COMMENTARY_SCHEMA,
    ANOMALY_DETECTION_SCHEMA,
)


# ---------------------------------------------------------------------------
# generate_competitor_profile
# ---------------------------------------------------------------------------

class TestGenerateCompetitorProfile:

    def test_returns_none_when_ollama_unavailable(self):
        """When Ollama is down, should return None."""
        with patch("strathmark.llm_roles.call_ollama", return_value=None):
            result = generate_competitor_profile(
                name="Alice", event_code="SB",
                history_summary="5 results, avg 30s, improving trend",
                predicted_time=28.0, confidence="HIGH",
            )
        assert result is None

    @patch("strathmark.llm_roles.call_ollama")
    def test_valid_response_parsed(self, mock_ollama):
        """Valid JSON response should be parsed into a dict."""
        mock_ollama.return_value = json.dumps({
            "narrative": "Alice is a rising star in standing block.",
            "strengths": ["Consistent technique", "Good conditioning"],
            "recent_form": "Improving over last 3 competitions",
            "prediction_confidence": "HIGH",
            "watch_factors": ["Wood quality sensitivity"],
        })
        result = generate_competitor_profile(
            name="Alice", event_code="SB",
            history_summary="5 results", predicted_time=28.0, confidence="HIGH",
        )
        assert result is not None
        assert "narrative" in result
        assert "strengths" in result
        assert isinstance(result["strengths"], list)

    @patch("strathmark.llm_roles.call_ollama")
    def test_invalid_json_returns_none(self, mock_ollama):
        """Malformed JSON from LLM should return None."""
        mock_ollama.return_value = "not valid json"
        result = generate_competitor_profile(
            name="Alice", event_code="SB",
            history_summary="5 results", predicted_time=28.0, confidence="HIGH",
        )
        assert result is None

    def test_uh_event_name_in_prompt(self):
        """UH event should use 'Underhand' in the prompt."""
        with patch("strathmark.llm_roles.call_ollama", return_value=None) as mock:
            generate_competitor_profile(
                name="Bob", event_code="UH",
                history_summary="test", predicted_time=30.0, confidence="MEDIUM",
            )
            # Verify the prompt contained "Underhand"
            if mock.called:
                prompt_arg = mock.call_args[0][0]
                assert "Underhand" in prompt_arg


# ---------------------------------------------------------------------------
# generate_race_commentary
# ---------------------------------------------------------------------------

class TestGenerateRaceCommentary:

    def test_returns_none_when_ollama_unavailable(self):
        with patch("strathmark.llm_roles.call_ollama", return_value=None):
            result = generate_race_commentary(
                event_code="SB",
                competitors=[
                    {"name": "A", "mark": 3, "predicted_time": 30.0},
                    {"name": "B", "mark": 8, "predicted_time": 25.0},
                ],
                results=[
                    {"name": "A", "actual_time": 29.0, "finish_position": 2},
                    {"name": "B", "actual_time": 27.0, "finish_position": 1},
                ],
            )
        assert result is None

    @patch("strathmark.llm_roles.call_ollama")
    def test_valid_response_parsed(self, mock_ollama):
        mock_ollama.return_value = json.dumps({
            "headline": "B takes the gold!",
            "commentary": "B edged out A in a tight finish.",
            "standout_performer": "B",
            "upset": False,
        })
        result = generate_race_commentary(
            event_code="SB",
            competitors=[
                {"name": "A", "mark": 3, "predicted_time": 30.0},
                {"name": "B", "mark": 8, "predicted_time": 25.0},
            ],
            results=[
                {"name": "A", "actual_time": 29.0, "finish_position": 2},
                {"name": "B", "actual_time": 27.0, "finish_position": 1},
            ],
        )
        assert result is not None
        assert result["headline"] == "B takes the gold!"
        assert result["upset"] is False

    @patch("strathmark.llm_roles.call_ollama")
    def test_invalid_json_returns_none(self, mock_ollama):
        mock_ollama.return_value = "broken json {"
        result = generate_race_commentary(
            event_code="SB",
            competitors=[{"name": "A", "mark": 3, "predicted_time": 30.0}],
            results=[{"name": "A", "actual_time": 29.0, "finish_position": 1}],
        )
        assert result is None


# ---------------------------------------------------------------------------
# detect_result_anomaly extended
# ---------------------------------------------------------------------------

class TestDetectResultAnomalyExtended:

    def test_statistical_fallback_z_score_below_2(self):
        """Z-score < 2.0 should be 'normal' severity."""
        result = detect_result_anomaly(
            name="Alice", event_code="SB",
            actual_time=31.0, predicted_time=30.0,
            historical_avg=30.0, historical_std=3.0,
            wood_species="S01", diameter_mm=300,
        )
        assert result is not None
        assert result["severity"] == "normal"
        assert result["is_anomalous"] is False

    def test_statistical_fallback_z_score_above_2(self):
        """Z-score > 2.0 but < 2.5 should be 'notable' severity."""
        result = detect_result_anomaly(
            name="Alice", event_code="SB",
            actual_time=37.0, predicted_time=30.0,
            historical_avg=30.0, historical_std=3.0,
            wood_species="S01", diameter_mm=300,
        )
        assert result is not None
        assert result["severity"] == "notable"
        assert result["is_anomalous"] is False

    def test_statistical_fallback_z_score_above_3(self):
        """Z-score > 3.0 should be 'significant' and anomalous."""
        result = detect_result_anomaly(
            name="Alice", event_code="SB",
            actual_time=42.0, predicted_time=30.0,
            historical_avg=30.0, historical_std=3.0,
            wood_species="S01", diameter_mm=300,
        )
        assert result is not None
        assert result["severity"] == "significant"
        assert result["is_anomalous"] is True

    def test_zero_std_dev_handled(self):
        """Historical std of 0 should use max(std, 1.0) = 1.0."""
        result = detect_result_anomaly(
            name="Alice", event_code="SB",
            actual_time=35.0, predicted_time=30.0,
            historical_avg=30.0, historical_std=0.0,
            wood_species="S01", diameter_mm=300,
        )
        assert result is not None
        # z_score = 5/1 = 5.0 -> significant
        assert result["is_anomalous"] is True

    def test_recommended_action_for_anomaly(self):
        """Anomalous results should recommend review."""
        result = detect_result_anomaly(
            name="Alice", event_code="SB",
            actual_time=50.0, predicted_time=30.0,
            historical_avg=30.0, historical_std=3.0,
            wood_species="S01", diameter_mm=300,
        )
        assert result is not None
        assert "review" in result["recommended_action"].lower()

    def test_non_anomalous_no_action_needed(self):
        result = detect_result_anomaly(
            name="Alice", event_code="SB",
            actual_time=30.5, predicted_time=30.0,
            historical_avg=30.0, historical_std=3.0,
            wood_species="S01", diameter_mm=300,
        )
        assert result is not None
        assert "no action" in result["recommended_action"].lower()


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSchemaStructure:

    def test_profile_schema_required_fields(self):
        required = COMPETITOR_PROFILE_SCHEMA.get("required", [])
        assert "narrative" in required
        assert "strengths" in required
        assert "recent_form" in required
        assert "prediction_confidence" in required

    def test_commentary_schema_required_fields(self):
        required = RACE_COMMENTARY_SCHEMA.get("required", [])
        assert "headline" in required
        assert "commentary" in required
        assert "standout_performer" in required
        assert "upset" in required

    def test_anomaly_schema_required_fields(self):
        required = ANOMALY_DETECTION_SCHEMA.get("required", [])
        assert "is_anomalous" in required
        assert "severity" in required
        assert "explanation" in required
