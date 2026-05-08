"""MNEMEX client tests — all offline.

The MNEMEX module is designed to no-op when MNEMEX_SUPABASE_URL or
MNEMEX_SUPABASE_KEY are unset. This keeps STRATHMARK functional during
the pre-MNEMEX transition. The tests exercise both modes purely via
env-var manipulation; no live MNEMEX project is required.
"""

from __future__ import annotations

import pandas as pd
import pytest


class TestIsMnemexConfigured:
    def test_returns_false_when_both_unset(self, monkeypatch):
        monkeypatch.delenv("MNEMEX_SUPABASE_URL", raising=False)
        monkeypatch.delenv("MNEMEX_SUPABASE_KEY", raising=False)
        from strathmark.mnemex import is_mnemex_configured

        assert is_mnemex_configured() is False

    def test_returns_false_when_url_missing(self, monkeypatch):
        monkeypatch.delenv("MNEMEX_SUPABASE_URL", raising=False)
        monkeypatch.setenv("MNEMEX_SUPABASE_KEY", "sb_secret_test")
        from strathmark.mnemex import is_mnemex_configured

        assert is_mnemex_configured() is False

    def test_returns_false_when_key_missing(self, monkeypatch):
        monkeypatch.setenv("MNEMEX_SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.delenv("MNEMEX_SUPABASE_KEY", raising=False)
        from strathmark.mnemex import is_mnemex_configured

        assert is_mnemex_configured() is False

    def test_returns_true_when_both_set(self, monkeypatch):
        monkeypatch.setenv("MNEMEX_SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("MNEMEX_SUPABASE_KEY", "sb_secret_test")
        from strathmark.mnemex import is_mnemex_configured

        assert is_mnemex_configured() is True

    def test_empty_strings_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MNEMEX_SUPABASE_URL", "")
        monkeypatch.setenv("MNEMEX_SUPABASE_KEY", "")
        from strathmark.mnemex import is_mnemex_configured

        assert is_mnemex_configured() is False


class TestPullsAreNoOpWhenUnconfigured:
    def setup_method(self):
        # Ensure cached client (if any) is forgotten so env changes take effect
        import strathmark.mnemex as m

        m._client = None

    def test_pull_canonical_results_returns_empty_df(self, monkeypatch):
        monkeypatch.delenv("MNEMEX_SUPABASE_URL", raising=False)
        monkeypatch.delenv("MNEMEX_SUPABASE_KEY", raising=False)
        from strathmark.mnemex import pull_canonical_results

        df = pull_canonical_results()
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_pull_canonical_competitors_returns_empty_df(self, monkeypatch):
        monkeypatch.delenv("MNEMEX_SUPABASE_URL", raising=False)
        monkeypatch.delenv("MNEMEX_SUPABASE_KEY", raising=False)
        from strathmark.mnemex import pull_canonical_competitors

        df = pull_canonical_competitors()
        assert isinstance(df, pd.DataFrame)
        assert df.empty


class TestRegisterRaisesWhenUnconfigured:
    """Writes to MNEMEX MUST raise when MNEMEX is unconfigured. The operator
    needs to know that the registration didn't land — silent no-op would
    hide a roster gap."""

    def setup_method(self):
        import strathmark.mnemex as m

        m._client = None

    def test_register_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("MNEMEX_SUPABASE_URL", raising=False)
        monkeypatch.delenv("MNEMEX_SUPABASE_KEY", raising=False)
        from strathmark.mnemex import register_competitor_in_mnemex

        with pytest.raises(RuntimeError, match="MNEMEX is not configured"):
            register_competitor_in_mnemex(name="Some New Competitor")

    def test_register_rejects_empty_name(self):
        from strathmark.mnemex import register_competitor_in_mnemex

        with pytest.raises(ValueError, match="name must not be empty"):
            register_competitor_in_mnemex(name="")


class TestExports:
    def test_package_reexports(self):
        import strathmark

        for name in (
            "is_mnemex_configured",
            "pull_canonical_results",
            "pull_canonical_competitors",
            "register_competitor_in_mnemex",
        ):
            assert hasattr(strathmark, name), f"strathmark missing export: {name}"
            assert callable(getattr(strathmark, name))
