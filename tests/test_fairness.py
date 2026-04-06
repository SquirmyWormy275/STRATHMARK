"""Tests for strathmark/fairness.py — fairness assessment."""

from strathmark.fairness import simulate_and_assess_handicaps


class TestSimulateAndAssessHandicaps:
    def test_returns_dict_with_required_keys(self):
        competitors = [
            {"name": "A", "mark": 3, "predicted_time": 50.0, "variance": 3.0},
            {"name": "B", "mark": 13, "predicted_time": 40.0, "variance": 3.0},
        ]
        result = simulate_and_assess_handicaps(
            competitors,
            num_simulations=1000,
            show=False,
        )
        assert isinstance(result, dict)
        assert "analysis" in result

    def test_fewer_than_2_competitors_returns_empty(self):
        result = simulate_and_assess_handicaps(
            [{"name": "A", "mark": 3, "predicted_time": 50.0}],
            show=False,
        )
        assert result == {} or result is None or len(result.get("analysis", {})) == 0

    def test_fair_handicaps_produce_good_rating(self):
        competitors = [
            {"name": "A", "mark": 23, "predicted_time": 45.0, "variance": 3.0},
            {"name": "B", "mark": 13, "predicted_time": 55.0, "variance": 3.0},
            {"name": "C", "mark": 3, "predicted_time": 65.0, "variance": 3.0},
        ]
        result = simulate_and_assess_handicaps(
            competitors,
            num_simulations=5000,
            show=False,
        )
        assert "analysis" in result
        analysis = result["analysis"]
        # Win rates should be roughly equal for fair handicaps
        pcts = analysis.get("winner_percentages", {})
        if pcts:
            spread = max(pcts.values()) - min(pcts.values())
            assert spread < 20  # Within 20% spread for fair handicaps
