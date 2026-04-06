"""Extended tests for strathmark/utils.py — score_prediction_accuracy and column edge cases."""

import pandas as pd

from strathmark.utils import score_prediction_accuracy, standardize_results_columns

# ---------------------------------------------------------------------------
# score_prediction_accuracy
# ---------------------------------------------------------------------------


class TestScorePredictionAccuracy:
    def test_basic_scoring(self):
        events = [
            {
                "event_type": "SB",
                "species": "S01",
                "results": [
                    {"name": "A", "predicted_time": 30.0, "actual_time": 32.0},
                    {"name": "B", "predicted_time": 40.0, "actual_time": 38.0},
                ],
            }
        ]
        result = score_prediction_accuracy(events)
        assert result["overall_mae"] is not None
        assert result["overall_rmse"] is not None
        assert result["overall_mae"] == 2.0  # avg of |−2| and |+2|
        assert "SB" in result["by_event_type"]
        assert result["by_event_type"]["SB"]["n"] == 2

    def test_empty_events(self):
        result = score_prediction_accuracy([])
        assert result["overall_rmse"] is None
        assert result["overall_mae"] is None

    def test_empty_results_in_event(self):
        events = [{"event_type": "SB", "species": "S01", "results": []}]
        result = score_prediction_accuracy(events)
        assert result["overall_rmse"] is None

    def test_systematic_bias_positive(self):
        """When predictions are consistently higher, bias should be positive."""
        events = [
            {
                "event_type": "SB",
                "species": "S01",
                "results": [
                    {"name": "A", "predicted_time": 35.0, "actual_time": 30.0},
                    {"name": "B", "predicted_time": 45.0, "actual_time": 40.0},
                ],
            }
        ]
        result = score_prediction_accuracy(events)
        assert result["systematic_biases"]["SB"] > 0

    def test_systematic_bias_negative(self):
        """When predictions are consistently lower, bias should be negative."""
        events = [
            {
                "event_type": "SB",
                "species": "S01",
                "results": [
                    {"name": "A", "predicted_time": 28.0, "actual_time": 30.0},
                    {"name": "B", "predicted_time": 38.0, "actual_time": 40.0},
                ],
            }
        ]
        result = score_prediction_accuracy(events)
        assert result["systematic_biases"]["SB"] < 0

    def test_by_species_breakdown(self):
        events = [
            {
                "event_type": "SB",
                "species": "Pine",
                "results": [
                    {"name": "A", "predicted_time": 30.0, "actual_time": 31.0},
                ],
            },
            {
                "event_type": "SB",
                "species": "Poplar",
                "results": [
                    {"name": "B", "predicted_time": 40.0, "actual_time": 42.0},
                ],
            },
        ]
        result = score_prediction_accuracy(events)
        assert "Pine" in result["by_species"]
        assert "Poplar" in result["by_species"]
        assert result["by_species"]["Pine"]["n"] == 1
        assert result["by_species"]["Poplar"]["n"] == 1

    def test_multiple_event_types(self):
        events = [
            {
                "event_type": "SB",
                "species": "S01",
                "results": [
                    {"name": "A", "predicted_time": 30.0, "actual_time": 30.0},
                ],
            },
            {
                "event_type": "UH",
                "species": "S01",
                "results": [
                    {"name": "B", "predicted_time": 40.0, "actual_time": 45.0},
                ],
            },
        ]
        result = score_prediction_accuracy(events)
        assert "SB" in result["by_event_type"]
        assert "UH" in result["by_event_type"]

    def test_invalid_result_entries_skipped(self):
        """Missing or invalid predicted/actual should be silently skipped."""
        events = [
            {
                "event_type": "SB",
                "species": "S01",
                "results": [
                    {"name": "A", "predicted_time": 30.0, "actual_time": 31.0},
                    {"name": "B"},  # missing fields
                    {"name": "C", "predicted_time": "bad", "actual_time": 30.0},
                ],
            }
        ]
        result = score_prediction_accuracy(events)
        assert result["by_event_type"]["SB"]["n"] == 1

    def test_rmse_greater_than_or_equal_to_mae(self):
        """RMSE is always >= MAE."""
        events = [
            {
                "event_type": "SB",
                "species": "S01",
                "results": [
                    {"name": "A", "predicted_time": 30.0, "actual_time": 35.0},
                    {"name": "B", "predicted_time": 40.0, "actual_time": 40.0},
                    {"name": "C", "predicted_time": 25.0, "actual_time": 28.0},
                ],
            }
        ]
        result = score_prediction_accuracy(events)
        assert result["overall_rmse"] >= result["overall_mae"]

    def test_perfect_predictions_zero_error(self):
        events = [
            {
                "event_type": "SB",
                "species": "S01",
                "results": [
                    {"name": "A", "predicted_time": 30.0, "actual_time": 30.0},
                ],
            }
        ]
        result = score_prediction_accuracy(events)
        assert result["overall_rmse"] == 0.0
        assert result["overall_mae"] == 0.0
        assert result["systematic_biases"]["SB"] == 0.0


# ---------------------------------------------------------------------------
# standardize_results_columns extended
# ---------------------------------------------------------------------------


class TestStandardizeResultsColumnsExtended:
    def test_none_returns_none(self):
        result = standardize_results_columns(None)
        assert result is None

    def test_renames_actual_time(self):
        df = pd.DataFrame({"actual_time": [45.0]})
        result = standardize_results_columns(df)
        assert "raw_time" in result.columns

    def test_renames_competitorname(self):
        df = pd.DataFrame({"CompetitorName": ["Alice"]})
        result = standardize_results_columns(df)
        assert "competitor_name" in result.columns

    def test_renames_event_code(self):
        df = pd.DataFrame({"event_code": ["sb"]})
        result = standardize_results_columns(df)
        assert "event" in result.columns
        assert result.iloc[0]["event"] == "SB"

    def test_renames_wood_species(self):
        df = pd.DataFrame({"wood_species": ["Pine"]})
        result = standardize_results_columns(df)
        assert "species" in result.columns

    def test_coerces_numeric_columns(self):
        df = pd.DataFrame(
            {
                "raw_time": ["30.5", "invalid"],
                "size_mm": ["300", "bad"],
                "quality": ["5", "nope"],
            }
        )
        result = standardize_results_columns(df)
        assert result.iloc[0]["raw_time"] == 30.5
        assert pd.isna(result.iloc[1]["raw_time"])

    def test_strips_whitespace(self):
        df = pd.DataFrame(
            {
                "competitor_name": ["  Alice  "],
                "event": ["  sb  "],
                "species": ["  Pine  "],
            }
        )
        result = standardize_results_columns(df)
        assert result.iloc[0]["competitor_name"] == "Alice"
        assert result.iloc[0]["event"] == "SB"
        assert result.iloc[0]["species"] == "Pine"

    def test_case_insensitive_column_names(self):
        df = pd.DataFrame({"TIME": [30.0], "NAME": ["Alice"]})
        result = standardize_results_columns(df)
        assert "raw_time" in result.columns
        assert "competitor_name" in result.columns

    def test_preserves_extra_columns(self):
        df = pd.DataFrame({"raw_time": [30.0], "custom_field": ["val"]})
        result = standardize_results_columns(df)
        assert "custom_field" in result.columns

    def test_renames_size_variants(self):
        for col_name in ["diameter", "size", "size (mm)", "size(mm)"]:
            df = pd.DataFrame({col_name: [300]})
            result = standardize_results_columns(df)
            assert "size_mm" in result.columns, f"Failed for column name: {col_name}"
