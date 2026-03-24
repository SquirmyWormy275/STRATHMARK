"""Extended tests for strathmark/db.py — _safe_date helper (no Supabase required)."""

import math
from datetime import date, datetime

import pandas as pd
import pytest

from strathmark.db import _safe_date


class TestSafeDate:

    def test_none_returns_none(self):
        assert _safe_date(None) is None

    def test_nan_returns_none(self):
        assert _safe_date(float('nan')) is None

    def test_nat_returns_none(self):
        assert _safe_date(pd.NaT) is None

    def test_date_returns_iso(self):
        result = _safe_date(date(2024, 6, 15))
        assert result == "2024-06-15"

    def test_datetime_returns_iso(self):
        result = _safe_date(datetime(2024, 6, 15, 10, 30, 0))
        assert "2024-06-15" in result

    def test_pandas_timestamp_returns_iso(self):
        ts = pd.Timestamp("2024-06-15")
        result = _safe_date(ts)
        assert "2024-06-15" in result

    def test_string_passthrough(self):
        result = _safe_date("2024-06-15")
        assert result == "2024-06-15"

    def test_empty_string_returns_none(self):
        result = _safe_date("")
        assert result is None

    def test_false_value_returns_none(self):
        result = _safe_date(0)
        assert result is None or result == "0"
