"""Tests for strathmark/db.py — Supabase/PostgreSQL backend.

These tests require network access and valid Supabase credentials.
They are skipped by default; run with STRATHMARK_TEST_DB=1 to enable.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("STRATHMARK_TEST_DB"),
    reason="Supabase tests require STRATHMARK_TEST_DB=1 and valid credentials",
)


class TestSupabaseBackend:
    def test_import_module(self):
        from strathmark.db import pull_results, push_results

        assert callable(push_results)
        assert callable(pull_results)
