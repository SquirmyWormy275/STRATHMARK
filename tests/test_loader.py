"""Tests for strathmark/loader.py — Excel workbook loading.

Tests that require the Excel file are skipped if the file is not present.
"""

import os
from pathlib import Path

import pytest

from strathmark.loader import load_woodchopping_xlsx


_XLSX = Path(__file__).parent.parent / "woodchopping_clean.xlsx"


class TestLoadWoodchoppingXlsx:

    @pytest.mark.skipif(not _XLSX.exists(), reason="Excel file not present")
    def test_loads_three_dataframes(self):
        wood_df, competitor_df, results_df = load_woodchopping_xlsx(str(_XLSX))
        assert len(wood_df) > 0 or len(results_df) > 0

    @pytest.mark.skipif(not _XLSX.exists(), reason="Excel file not present")
    def test_results_have_required_columns(self):
        _, _, results_df = load_woodchopping_xlsx(str(_XLSX))
        if len(results_df) > 0:
            cols = set(results_df.columns.str.lower())
            assert any("name" in c or "competitor" in c for c in cols)

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_woodchopping_xlsx("/nonexistent/path.xlsx")
