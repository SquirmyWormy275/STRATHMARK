"""Tests for strathmark/utils.py — column standardization."""

import pandas as pd

from strathmark.utils import standardize_results_columns


class TestStandardizeResultsColumns:
    def test_renames_time_to_raw_time(self):
        df = pd.DataFrame({"time": [45.0, 50.0], "name": ["A", "B"]})
        result = standardize_results_columns(df)
        assert "raw_time" in result.columns

    def test_renames_diameter_mm_to_size_mm(self):
        df = pd.DataFrame({"diameter_mm": [300, 350]})
        result = standardize_results_columns(df)
        assert "size_mm" in result.columns

    def test_uppercases_event_column(self):
        df = pd.DataFrame({"event": ["sb", "uh"]})
        result = standardize_results_columns(df)
        assert list(result["event"]) == ["SB", "UH"]

    def test_empty_dataframe_unchanged(self):
        df = pd.DataFrame()
        result = standardize_results_columns(df)
        assert len(result) == 0

    def test_already_standard_columns_unchanged(self):
        df = pd.DataFrame({"raw_time": [45.0], "size_mm": [300], "event": ["SB"]})
        result = standardize_results_columns(df)
        assert "raw_time" in result.columns
        assert "size_mm" in result.columns
