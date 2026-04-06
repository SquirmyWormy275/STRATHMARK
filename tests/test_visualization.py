"""Tests for strathmark/visualization.py — plain-text output."""

from strathmark.visualization import (
    generate_simulation_summary,
    visualize_simulation_results,
)


def _mock_analysis():
    return {
        "num_simulations": 10000,
        "avg_spread": 5.0,
        "median_spread": 4.5,
        "min_spread": 0.5,
        "max_spread": 15.0,
        "tight_finish_prob": 0.85,
        "very_tight_finish_prob": 0.45,
        "winner_counts": {"Alice": 3400, "Bob": 3300, "Charlie": 3300},
        "winner_percentages": {"Alice": 34.0, "Bob": 33.0, "Charlie": 33.0},
        "podium_counts": {"Alice": 10000, "Bob": 10000, "Charlie": 10000},
        "podium_percentages": {"Alice": 100.0, "Bob": 100.0, "Charlie": 100.0},
        "avg_finish_positions": {"Alice": 1.98, "Bob": 2.01, "Charlie": 2.01},
        "front_marker_name": "Charlie",
        "back_marker_name": "Alice",
        "front_marker_wins": 3300,
        "back_marker_wins": 3400,
        "competitors": [
            {"name": "Alice", "mark": 23, "predicted_time": 45.0},
            {"name": "Bob", "mark": 13, "predicted_time": 55.0},
            {"name": "Charlie", "mark": 3, "predicted_time": 65.0},
        ],
    }


class TestGenerateSimulationSummary:
    def test_returns_string(self):
        result = generate_simulation_summary(_mock_analysis())
        assert isinstance(result, str)
        assert len(result) > 100

    def test_plain_text_no_ansi(self):
        result = generate_simulation_summary(_mock_analysis())
        assert "\033[" not in result  # No ANSI escape codes

    def test_contains_key_sections(self):
        result = generate_simulation_summary(_mock_analysis())
        assert "simulation" in result.lower() or "Simulation" in result


class TestVisualizeSimulationResults:
    def test_returns_string(self):
        result = visualize_simulation_results(_mock_analysis())
        assert isinstance(result, str)

    def test_contains_competitor_names(self):
        result = visualize_simulation_results(_mock_analysis())
        assert "Alice" in result
        assert "Bob" in result

    def test_no_emojis(self):
        result = visualize_simulation_results(_mock_analysis())
        # Simple check: no common emoji ranges
        for char in result:
            assert ord(char) < 0x1F600 or ord(char) > 0x1F64F
