"""Extended tests for strathmark/loader.py — validators, load_results_for_competitor."""

import warnings

import pandas as pd
import pytest

from strathmark.loader import (
    load_results_for_competitor,
    _validate_wood,
    _validate_competitor,
    _validate_results,
    _WOOD_REQUIRED,
    _COMPETITOR_REQUIRED,
    _RESULTS_REQUIRED,
)


# ---------------------------------------------------------------------------
# _validate_wood
# ---------------------------------------------------------------------------

class TestValidateWood:

    def test_valid_wood_df_passes(self):
        df = pd.DataFrame({
            'species': ['Pine'],
            'speciesID': ['S01'],
            'janka_hard': [1690],
            'spec_gravity': [0.34],
        })
        result = _validate_wood(df)
        assert len(result) == 1

    def test_missing_column_raises(self):
        df = pd.DataFrame({
            'species': ['Pine'],
            # Missing speciesID, janka_hard, spec_gravity
        })
        with pytest.raises(ValueError, match="missing required columns"):
            _validate_wood(df)

    def test_unnamed_columns_raises(self):
        df = pd.DataFrame({
            'species': ['Pine'],
            'speciesID': ['S01'],
            'janka_hard': [1690],
            'spec_gravity': [0.34],
            'Unnamed: 4': [None],
        })
        with pytest.raises(ValueError, match="unnamed columns"):
            _validate_wood(df)


# ---------------------------------------------------------------------------
# _validate_competitor
# ---------------------------------------------------------------------------

class TestValidateCompetitor:

    def test_valid_competitor_df_passes(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001'],
            'Name': ['Alice'],
        })
        result = _validate_competitor(df)
        assert len(result) == 1

    def test_missing_column_raises(self):
        df = pd.DataFrame({'Name': ['Alice']})  # Missing CompetitorID
        with pytest.raises(ValueError, match="missing required columns"):
            _validate_competitor(df)

    def test_unnamed_columns_raises(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001'],
            'Name': ['Alice'],
            'Unnamed: 2': [None],
        })
        with pytest.raises(ValueError, match="unnamed columns"):
            _validate_competitor(df)


# ---------------------------------------------------------------------------
# _validate_results
# ---------------------------------------------------------------------------

class TestValidateResults:

    def test_valid_results_df_passes(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001'],
            'Event': ['SB'],
            'Time (seconds)': [30.0],
            'Size (mm)': [300],
            'Species Code': ['S01'],
        })
        result = _validate_results(df)
        assert len(result) == 1

    def test_missing_column_raises(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001'],
            'Event': ['SB'],
            # Missing Time, Size, Species Code
        })
        with pytest.raises(ValueError, match="missing required columns"):
            _validate_results(df)

    def test_lowercase_event_raises(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001'],
            'Event': ['sb'],
            'Time (seconds)': [30.0],
            'Size (mm)': [300],
            'Species Code': ['S01'],
        })
        with pytest.raises(ValueError, match="lowercase event codes"):
            _validate_results(df)

    def test_year_only_date_raises(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001'],
            'Event': ['SB'],
            'Time (seconds)': [30.0],
            'Size (mm)': [300],
            'Species Code': ['S01'],
            'Date': [2024],  # Year-only integer
        })
        with pytest.raises(ValueError, match="year-only integers"):
            _validate_results(df)

    def test_drops_rows_with_missing_required_fields(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001', None],
            'Event': ['SB', 'UH'],
            'Time (seconds)': [30.0, 40.0],
            'Size (mm)': [300, 350],
            'Species Code': ['S01', 'S02'],
        })
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = _validate_results(df)
            assert len(result) == 1
            assert any("dropped" in str(warning.message).lower() for warning in w)

    def test_valid_date_column_coerced(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001'],
            'Event': ['SB'],
            'Time (seconds)': [30.0],
            'Size (mm)': [300],
            'Species Code': ['S01'],
            'Date': ['2024-06-15'],
        })
        result = _validate_results(df)
        assert pd.api.types.is_datetime64_any_dtype(result['Date'])


# ---------------------------------------------------------------------------
# load_results_for_competitor
# ---------------------------------------------------------------------------

class TestLoadResultsForCompetitor:

    def test_filters_by_competitor(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001', 'C002', 'C001'],
            'Event': ['SB', 'SB', 'UH'],
            'Time (seconds)': [30.0, 40.0, 35.0],
        })
        result = load_results_for_competitor(df, 'C001')
        assert len(result) == 2
        assert all(result['CompetitorID'] == 'C001')

    def test_nonexistent_competitor_returns_empty(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001'],
            'Event': ['SB'],
            'Time (seconds)': [30.0],
        })
        result = load_results_for_competitor(df, 'C999')
        assert len(result) == 0

    def test_sorted_by_date(self):
        df = pd.DataFrame({
            'CompetitorID': ['C001', 'C001', 'C001'],
            'Event': ['SB', 'SB', 'SB'],
            'Time (seconds)': [30.0, 35.0, 32.0],
            'Date': pd.to_datetime(['2024-03-01', '2024-01-01', '2024-02-01']),
        })
        result = load_results_for_competitor(df, 'C001')
        dates = result['Date'].tolist()
        assert dates == sorted(dates)

    def test_string_competitor_id_matching(self):
        """CompetitorID should match as strings."""
        df = pd.DataFrame({
            'CompetitorID': [1, 2, 1],
            'Event': ['SB', 'SB', 'UH'],
            'Time (seconds)': [30.0, 40.0, 35.0],
        })
        result = load_results_for_competitor(df, '1')
        assert len(result) == 2

    def test_reset_index(self):
        """Returned DataFrame should have a clean 0-based index."""
        df = pd.DataFrame({
            'CompetitorID': ['C001', 'C002', 'C001'],
            'Event': ['SB', 'SB', 'UH'],
            'Time (seconds)': [30.0, 40.0, 35.0],
        })
        result = load_results_for_competitor(df, 'C001')
        assert list(result.index) == [0, 1]
