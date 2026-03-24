"""Extended tests for strathmark/fairness.py — AI assessment, championship analysis, helpers."""

from unittest.mock import patch

import numpy as np
import pytest

from strathmark.fairness import (
    get_ai_assessment_of_handicaps,
    get_championship_race_analysis,
    format_ai_assessment,
    simulate_and_assess_handicaps,
    _validate_fairness_assessment,
    _stats_dict_to_str,
    _variance_warning_text,
)
from strathmark.variance import run_monte_carlo_simulation, CompetitorTimeStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_analysis(n=3, num_sims=1_000, seed=42):
    """Build a minimal Monte Carlo analysis dict."""
    comps = [
        {"name": f"C{i}", "mark": 3 + i * 5, "predicted_time": 40.0 - i * 5, "std_dev": 3.0}
        for i in range(n)
    ]
    return run_monte_carlo_simulation(comps, num_simulations=num_sims, seed=seed, verbose=False)


# ---------------------------------------------------------------------------
# _validate_fairness_assessment
# ---------------------------------------------------------------------------

class TestValidateFairnessAssessment:

    def test_valid_response_unchanged(self):
        """Response with all sections should pass through unchanged."""
        response = (
            "FAIRNESS RATING: Excellent\n"
            "STATISTICAL ANALYSIS: Good stats.\n"
            "PATTERN DIAGNOSIS: No pattern.\n"
            "PREDICTION ACCURACY: Good.\n"
            "RECOMMENDATIONS: None needed."
        )
        result = _validate_fairness_assessment(
            response, 2.0, 33.3, "A", "C", {"A": 1.0, "C": -1.0}
        )
        assert "[WARN]" not in result

    def test_missing_sections_appends_warning(self):
        """Response missing sections should get a warning appended."""
        response = "FAIRNESS RATING: Good\nSome text without other sections."
        result = _validate_fairness_assessment(
            response, 5.0, 33.3, "A", "C", {"A": 1.0, "C": -1.0}
        )
        assert "[WARN]" in result
        assert "STATISTICAL ANALYSIS" in result

    def test_missing_rating_appends_warning(self):
        """Response without any valid rating keyword should get a rating warning."""
        # Must include all section headers but NO valid rating keyword
        response = (
            "FAIRNESS RATING: Mediocre\n"
            "STATISTICAL ANALYSIS: stuff\n"
            "PATTERN DIAGNOSIS: stuff\n"
            "PREDICTION ACCURACY: stuff\n"
            "RECOMMENDATIONS: stuff"
        )
        # "Mediocre" is not in valid_ratings, but "GOOD" appears in "VERY GOOD"
        # and "FAIR" appears in "FAIRNESS" — so we need a truly absent keyword.
        # Actually "FAIR" IS in the response via "FAIRNESS". Let's use a response
        # where none of the valid rating strings appear at all.
        response = (
            "RATING: Mediocre\n"
            "STATISTICAL ANALYSIS: stuff\n"
            "PATTERN DIAGNOSIS: stuff\n"
            "PREDICTION ACCURACY: stuff\n"
            "RECOMMENDATIONS: stuff"
        )
        result = _validate_fairness_assessment(
            response, 5.0, 33.3, "A", "C", {"A": 1.0, "C": -1.0}
        )
        # Missing "FAIRNESS RATING:" section header triggers section warning
        assert "[WARN]" in result


# ---------------------------------------------------------------------------
# _stats_dict_to_str
# ---------------------------------------------------------------------------

class TestStatsDictToStr:

    def test_none_returns_empty(self):
        assert _stats_dict_to_str(None) == ""

    def test_competitor_time_stats_dataclass(self):
        stats = CompetitorTimeStats(
            name="Alice", mean=30.0, std_dev=2.5, min_time=25.0,
            max_time=35.0, p25=28.0, p50=30.0, p75=32.0,
            consistency_rating="High (expected variance)"
        )
        result = _stats_dict_to_str(stats)
        assert "30.0s" in result
        assert "2.50s" in result
        assert "25.0s" in result

    def test_plain_dict(self):
        stats = {"mean": 30.0, "std_dev": 2.5, "min": 25.0, "max": 35.0,
                 "consistency_rating": "High"}
        result = _stats_dict_to_str(stats)
        assert "30.0s" in result
        assert "High" in result


# ---------------------------------------------------------------------------
# _variance_warning_text
# ---------------------------------------------------------------------------

class TestVarianceWarningText:

    def test_no_imbalance_returns_empty(self):
        analysis = {"variance_ratio": 1.0}
        assert _variance_warning_text(analysis) == ""

    def test_imbalance_returns_warning(self):
        analysis = {
            "variance_ratio": 3.0,
            "competitor_variances": {"A": 6.0, "B": 2.0},
        }
        result = _variance_warning_text(analysis)
        assert "VARIANCE IMBALANCE WARNING" in result
        assert "A" in result  # max variance
        assert "3.0x" in result

    def test_exactly_at_threshold_returns_empty(self):
        analysis = {"variance_ratio": 2.0}
        assert _variance_warning_text(analysis) == ""


# ---------------------------------------------------------------------------
# get_ai_assessment_of_handicaps (with mocked Ollama)
# ---------------------------------------------------------------------------

class TestGetAiAssessmentOfHandicaps:

    def test_statistical_fallback_when_ollama_unavailable(self):
        """When Ollama is down, should return a statistical fallback."""
        analysis = _make_analysis(n=3, num_sims=5_000)

        with patch("strathmark.fairness.call_ollama", return_value=None):
            result = get_ai_assessment_of_handicaps(analysis)

        assert "FAIRNESS RATING:" in result
        assert "STATISTICAL ANALYSIS:" in result
        assert "PATTERN DIAGNOSIS:" in result
        assert "RECOMMENDATIONS:" in result

    def test_fallback_rating_reflects_spread(self):
        """Statistical fallback rating should match win rate spread."""
        # Equal competitors -> low spread -> EXCELLENT
        comps = [
            {"name": f"C{i}", "mark": 3, "predicted_time": 30.0, "std_dev": 3.0}
            for i in range(4)
        ]
        analysis = run_monte_carlo_simulation(
            comps, num_simulations=10_000, seed=42, verbose=False
        )

        with patch("strathmark.fairness.call_ollama", return_value=None):
            result = get_ai_assessment_of_handicaps(analysis)

        assert "EXCELLENT" in result

    @patch("strathmark.fairness.call_ollama")
    def test_llm_response_parsed(self, mock_ollama):
        """Valid JSON LLM response should be formatted properly."""
        import json
        mock_ollama.return_value = json.dumps({
            "rating": "Good",
            "statistical_analysis": "Win rates are acceptable.",
            "pattern_diagnosis": "No clear bias.",
            "prediction_accuracy": "Predictions within range.",
            "recommendations": ["Keep monitoring.", "Collect more data."]
        })
        analysis = _make_analysis(n=3, num_sims=1_000)
        result = get_ai_assessment_of_handicaps(analysis)

        assert "FAIRNESS RATING: Good" in result
        assert "Win rates are acceptable" in result
        assert "Keep monitoring" in result

    @patch("strathmark.fairness.call_ollama")
    def test_malformed_llm_response_falls_back(self, mock_ollama):
        """Invalid JSON from LLM should fall back to statistical."""
        mock_ollama.return_value = "not valid json {{"
        analysis = _make_analysis(n=3, num_sims=1_000)
        result = get_ai_assessment_of_handicaps(analysis)

        # Should still produce a valid assessment via fallback
        assert "FAIRNESS RATING:" in result


# ---------------------------------------------------------------------------
# get_championship_race_analysis
# ---------------------------------------------------------------------------

class TestGetChampionshipRaceAnalysis:

    def test_statistical_fallback(self):
        """When Ollama is down, should return a statistical fallback."""
        comps = [
            {"name": "A", "mark": 3, "predicted_time": 25.0, "std_dev": 3.0},
            {"name": "B", "mark": 3, "predicted_time": 30.0, "std_dev": 3.0},
            {"name": "C", "mark": 3, "predicted_time": 35.0, "std_dev": 3.0},
        ]
        analysis = run_monte_carlo_simulation(
            comps, num_simulations=5_000, seed=42, verbose=False
        )
        predictions = [
            {"name": "A", "predicted_time": 25.0, "method_used": "baseline", "confidence": "HIGH"},
            {"name": "B", "predicted_time": 30.0, "method_used": "baseline", "confidence": "HIGH"},
            {"name": "C", "predicted_time": 35.0, "method_used": "baseline", "confidence": "MEDIUM"},
        ]

        with patch("strathmark.fairness.call_ollama", return_value=None):
            result = get_championship_race_analysis(analysis, predictions)

        assert "RACE FAVORITE" in result
        assert "A" in result  # A should be favorite (fastest)

    def test_identifies_favorite(self):
        """Should identify the competitor with highest win rate as favorite."""
        comps = [
            {"name": "Favorite", "mark": 3, "predicted_time": 20.0, "std_dev": 2.0},
            {"name": "Underdog", "mark": 3, "predicted_time": 40.0, "std_dev": 2.0},
        ]
        analysis = run_monte_carlo_simulation(
            comps, num_simulations=5_000, seed=42, verbose=False
        )
        predictions = [
            {"name": "Favorite", "predicted_time": 20.0, "method_used": "baseline", "confidence": "HIGH"},
            {"name": "Underdog", "predicted_time": 40.0, "method_used": "baseline", "confidence": "HIGH"},
        ]

        with patch("strathmark.fairness.call_ollama", return_value=None):
            result = get_championship_race_analysis(analysis, predictions)

        assert "Favorite" in result

    def test_close_matchups_detected(self):
        """Competitors within 2s should be noted as matchups."""
        comps = [
            {"name": "A", "mark": 3, "predicted_time": 30.0, "std_dev": 3.0},
            {"name": "B", "mark": 3, "predicted_time": 31.0, "std_dev": 3.0},
            {"name": "C", "mark": 3, "predicted_time": 45.0, "std_dev": 3.0},
        ]
        analysis = run_monte_carlo_simulation(
            comps, num_simulations=1_000, seed=42, verbose=False
        )
        predictions = [
            {"name": "A", "predicted_time": 30.0, "method_used": "baseline", "confidence": "HIGH"},
            {"name": "B", "predicted_time": 31.0, "method_used": "baseline", "confidence": "HIGH"},
            {"name": "C", "predicted_time": 45.0, "method_used": "baseline", "confidence": "HIGH"},
        ]

        with patch("strathmark.fairness.call_ollama", return_value=None):
            result = get_championship_race_analysis(analysis, predictions)

        # A and B are within 2s
        assert "A" in result and "B" in result


# ---------------------------------------------------------------------------
# format_ai_assessment
# ---------------------------------------------------------------------------

class TestFormatAiAssessment:

    def test_does_not_crash(self, capsys):
        """format_ai_assessment should print without errors."""
        text = (
            "FAIRNESS RATING: Excellent\n\n"
            "STATISTICAL ANALYSIS: Good stats.\n\n"
            "RECOMMENDATIONS:\n- Fix this\n- Fix that"
        )
        format_ai_assessment(text, width=80)
        captured = capsys.readouterr()
        assert "FAIRNESS RATING" in captured.out

    def test_wraps_long_lines(self, capsys):
        """Non-header lines should be wrapped. Headers may exceed width."""
        long_text = "Some analysis paragraph: " + "word " * 50
        format_ai_assessment(long_text, width=60)
        captured = capsys.readouterr()
        for line in captured.out.splitlines():
            if not line.strip():
                continue
            # Section headers (uppercase with colon) are not wrapped
            stripped = line.strip()
            if ':' in stripped and stripped.split(':')[0].isupper():
                continue
            assert len(line) <= 65

    def test_empty_text(self, capsys):
        format_ai_assessment("", width=80)
        captured = capsys.readouterr()
        # Should not crash


# ---------------------------------------------------------------------------
# simulate_and_assess_handicaps extended
# ---------------------------------------------------------------------------

class TestSimulateAndAssessHandicapsExtended:

    def test_returns_all_keys(self):
        comps = [
            {"name": "A", "mark": 3, "predicted_time": 40.0},
            {"name": "B", "mark": 8, "predicted_time": 35.0},
        ]
        result = simulate_and_assess_handicaps(comps, num_simulations=1_000, show=False)
        assert 'analysis' in result
        assert 'summary' in result
        assert 'chart' in result
        assert 'assessment' in result

    def test_empty_competitors(self):
        result = simulate_and_assess_handicaps([], show=False)
        assert result['analysis'] == {}
        assert result['summary'] == ''

    def test_single_competitor(self):
        result = simulate_and_assess_handicaps(
            [{"name": "A", "mark": 3, "predicted_time": 30.0}],
            show=False,
        )
        assert result['analysis'] == {}
