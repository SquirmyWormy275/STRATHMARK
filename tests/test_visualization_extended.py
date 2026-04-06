"""Extended tests for the visualization module.

Tests ASCII bar chart accuracy, output formatting constraints,
edge cases with empty/single-competitor data, and section completeness.
"""

import re

import pytest

from strathmark.variance import run_monte_carlo_simulation
from strathmark.visualization import (
    generate_simulation_summary,
    visualize_simulation_results,
)


@pytest.fixture
def two_competitor_analysis():
    """Standard 2-competitor Monte Carlo analysis."""
    competitors = [
        {"name": "Alice", "predicted_time": 25.0, "mark": 8, "std_dev": 3.0},
        {"name": "Bob", "predicted_time": 30.0, "mark": 3, "std_dev": 3.0},
    ]
    return run_monte_carlo_simulation(
        competitors,
        num_simulations=10_000,
        seed=42,
        verbose=False,
    )


@pytest.fixture
def five_competitor_analysis():
    """5-competitor Monte Carlo analysis for richer output."""
    competitors = [
        {"name": "Alice", "predicted_time": 20.0, "mark": 13, "std_dev": 2.0},
        {"name": "Bob", "predicted_time": 25.0, "mark": 8, "std_dev": 3.0},
        {"name": "Charlie", "predicted_time": 28.0, "mark": 5, "std_dev": 3.5},
        {"name": "Diana", "predicted_time": 32.0, "mark": 3, "std_dev": 2.5},
        {"name": "Eve", "predicted_time": 35.0, "mark": 3, "std_dev": 4.0},
    ]
    return run_monte_carlo_simulation(
        competitors,
        num_simulations=10_000,
        seed=42,
        verbose=False,
        track_finish_orders=True,
        track_podium_margins=True,
    )


# ---------------------------------------------------------------------------
# generate_simulation_summary
# ---------------------------------------------------------------------------
class TestSimulationSummaryContent:
    def test_returns_string(self, two_competitor_analysis):
        s = generate_simulation_summary(two_competitor_analysis)
        assert isinstance(s, str)
        assert len(s) > 50

    def test_no_ansi_codes(self, two_competitor_analysis):
        s = generate_simulation_summary(two_competitor_analysis)
        assert "\033[" not in s
        assert "\x1b[" not in s

    def test_no_emojis(self, two_competitor_analysis):
        s = generate_simulation_summary(two_competitor_analysis)
        # Check common emoji ranges
        for char in s:
            cp = ord(char)
            assert not (0x1F600 <= cp <= 0x1F64F), f"Emoji found: {char}"
            assert not (0x1F300 <= cp <= 0x1F5FF), f"Emoji found: {char}"

    def test_contains_competitor_names(self, two_competitor_analysis):
        s = generate_simulation_summary(two_competitor_analysis)
        assert "Alice" in s
        assert "Bob" in s

    def test_contains_win_percentages(self, two_competitor_analysis):
        s = generate_simulation_summary(two_competitor_analysis)
        # Should contain percentage values (digits followed by %)
        assert re.search(r"\d+\.\d+%", s) or re.search(r"\d+%", s)

    def test_five_competitor_all_names_present(self, five_competitor_analysis):
        s = generate_simulation_summary(five_competitor_analysis)
        for name in ["Alice", "Bob", "Charlie", "Diana", "Eve"]:
            assert name in s


class TestSimulationSummaryFormatting:
    def test_line_width_under_80(self, five_competitor_analysis):
        """No line should exceed 80 characters (plain text constraint)."""
        s = generate_simulation_summary(five_competitor_analysis)
        for i, line in enumerate(s.split("\n"), 1):
            assert len(line) <= 80, f"Line {i} is {len(line)} chars: {line!r}"

    def test_plain_text_only(self, five_competitor_analysis):
        """Output should be plain ASCII/Unicode text, no HTML or markup."""
        s = generate_simulation_summary(five_competitor_analysis)
        assert "<html>" not in s.lower()
        assert "<div>" not in s.lower()
        assert "```" not in s


# ---------------------------------------------------------------------------
# visualize_simulation_results (ASCII bar chart)
# ---------------------------------------------------------------------------
class TestBarChart:
    def test_returns_string(self, two_competitor_analysis):
        s = visualize_simulation_results(two_competitor_analysis)
        assert isinstance(s, str)

    def test_contains_all_names(self, five_competitor_analysis):
        s = visualize_simulation_results(five_competitor_analysis)
        for name in ["Alice", "Bob", "Charlie", "Diana", "Eve"]:
            assert name in s

    def test_contains_percentage_values(self, two_competitor_analysis):
        s = visualize_simulation_results(two_competitor_analysis)
        assert "%" in s

    def test_no_ansi_codes(self, two_competitor_analysis):
        s = visualize_simulation_results(two_competitor_analysis)
        assert "\033[" not in s

    def test_no_emojis(self, five_competitor_analysis):
        s = visualize_simulation_results(five_competitor_analysis)
        for char in s:
            cp = ord(char)
            assert not (0x1F600 <= cp <= 0x1F64F), f"Emoji: {char}"

    def test_bar_chart_uses_block_chars(self, two_competitor_analysis):
        """Bar chart should use Unicode block characters."""
        s = visualize_simulation_results(two_competitor_analysis)
        # Should contain ▓ or similar block character
        assert any(ord(c) > 127 for c in s), "No Unicode block chars found"

    def test_percentages_sum_to_100(self, two_competitor_analysis):
        """Win percentages should approximately sum to 100%."""
        s = visualize_simulation_results(two_competitor_analysis)
        pcts = [float(m.group(1)) for m in re.finditer(r"(\d+\.\d+)%", s)]
        if pcts:
            assert sum(pcts) == pytest.approx(100.0, abs=1.0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
class TestVisualizationEdgeCases:
    def test_single_competitor(self):
        """Single competitor should still produce valid output."""
        competitors = [
            {"name": "Solo", "predicted_time": 25.0, "mark": 3, "std_dev": 3.0},
        ]
        analysis = run_monte_carlo_simulation(
            competitors,
            num_simulations=1_000,
            seed=42,
            verbose=False,
        )
        summary = generate_simulation_summary(analysis)
        assert isinstance(summary, str)
        assert "Solo" in summary

        chart = visualize_simulation_results(analysis)
        assert isinstance(chart, str)

    def test_identical_competitors(self):
        """Two identical competitors should get ~50% win rate each."""
        competitors = [
            {"name": "Twin1", "predicted_time": 25.0, "mark": 3, "std_dev": 3.0},
            {"name": "Twin2", "predicted_time": 25.0, "mark": 3, "std_dev": 3.0},
        ]
        analysis = run_monte_carlo_simulation(
            competitors,
            num_simulations=10_000,
            seed=42,
            verbose=False,
        )
        chart = visualize_simulation_results(analysis)
        assert "Twin1" in chart
        assert "Twin2" in chart
        # Both should have roughly 50% ± 5%
        for name in ["Twin1", "Twin2"]:
            pct = analysis["winner_percentages"][name]
            assert 40.0 < pct < 60.0

    def test_large_field(self):
        """10 competitors should render without truncation."""
        competitors = [
            {
                "name": f"Comp{i}",
                "predicted_time": 20 + i * 3,
                "mark": max(3, 20 - i * 2),
                "std_dev": 3.0,
            }
            for i in range(10)
        ]
        analysis = run_monte_carlo_simulation(
            competitors,
            num_simulations=5_000,
            seed=42,
            verbose=False,
        )
        chart = visualize_simulation_results(analysis)
        for i in range(10):
            assert f"Comp{i}" in chart
