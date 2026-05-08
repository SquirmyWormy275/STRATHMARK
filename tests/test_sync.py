"""Sync function tests — all offline.

The sync function operates in dry-run mode whenever MNEMEX is unconfigured,
which is the natural state for pre-MNEMEX environments and for CI. We
exercise that contract here. Live tests against a real MNEMEX project
would be added once one exists.
"""

from __future__ import annotations

import pandas as pd
import pytest


class TestSyncResultDataclass:
    def test_summary_dry_run(self):
        from strathmark.sync import SyncResult

        result = SyncResult(
            sync_path="manual_force_sync",
            dry_run=True,
            rows_pulled=42,
            rows_upserted=0,
        )
        s = result.summary()
        assert "DRY RUN" in s
        assert "manual_force_sync" in s
        assert "pulled=42" in s

    def test_summary_real(self):
        from strathmark.sync import SyncResult

        result = SyncResult(
            sync_path="nightly_batch",
            dry_run=False,
            rows_pulled=42,
            rows_upserted=42,
        )
        s = result.summary()
        assert "SYNCED" in s
        assert "upserted=42" in s


class TestNoOpWhenMnemexUnconfigured:
    def setup_method(self):
        import strathmark.mnemex as m

        m._client = None

    def test_nightly_batch_noop(self, monkeypatch):
        monkeypatch.delenv("MNEMEX_SUPABASE_URL", raising=False)
        monkeypatch.delenv("MNEMEX_SUPABASE_KEY", raising=False)
        from strathmark.sync import nightly_batch

        result = nightly_batch()
        assert result.dry_run is True
        assert result.rows_pulled == 0
        assert result.rows_upserted == 0
        assert result.sync_path == "nightly_batch"
        assert "MNEMEX unconfigured" in (result.notes or "")

    def test_strathex_finalization_noop(self, monkeypatch):
        monkeypatch.delenv("MNEMEX_SUPABASE_URL", raising=False)
        monkeypatch.delenv("MNEMEX_SUPABASE_KEY", raising=False)
        from strathmark.sync import strathex_finalization

        result = strathex_finalization(event_id="EVT_123")
        assert result.dry_run is True
        assert result.sync_path == "strathex_finalization"

    def test_manual_force_sync_noop(self, monkeypatch):
        monkeypatch.delenv("MNEMEX_SUPABASE_URL", raising=False)
        monkeypatch.delenv("MNEMEX_SUPABASE_KEY", raising=False)
        from strathmark.sync import manual_force_sync

        result = manual_force_sync(show_name="some show")
        assert result.dry_run is True
        assert result.sync_path == "manual_force_sync"


class TestInternalMappers:
    """Pure functions — exercised without any DB access."""

    def test_normalize_event_sb(self):
        from strathmark.sync import _normalize_event

        assert _normalize_event("SB") == "SB"
        assert _normalize_event("STR_SB") == "SB"
        assert _normalize_event("Standing Block") == "SB"

    def test_normalize_event_uh(self):
        from strathmark.sync import _normalize_event

        assert _normalize_event("UH") == "UH"
        assert _normalize_event("STR_UH") == "UH"
        assert _normalize_event("underhand") == "UH"

    def test_normalize_event_unknown_passes_through(self):
        from strathmark.sync import _normalize_event

        # Lets the downstream constraint catch unexpected codes rather than
        # silently rewriting to a default.
        assert _normalize_event("HOT_SAW") == "HOT_SAW"

    def test_safe_date_str_handles_none(self):
        from strathmark.sync import _safe_date_str

        assert _safe_date_str(None) is None

    def test_safe_date_str_truncates(self):
        from strathmark.sync import _safe_date_str

        assert _safe_date_str("2026-04-30T10:30:00Z") == "2026-04-30"


class TestMappingMissingColumns:
    def test_map_raises_keyerror_on_missing_required_column(self):
        from strathmark.sync import _map_mnemex_to_strathmark

        # Missing competitor_mnemex_id, etc.
        df = pd.DataFrame([{"mnemex_id": "m1", "event_type": "SB"}])
        with pytest.raises(KeyError):
            _map_mnemex_to_strathmark(df)


class TestSyncPathValidation:
    def test_invalid_sync_path_raises(self):
        from strathmark.sync import _do_sync

        with pytest.raises(ValueError, match="unknown sync_path"):
            _do_sync(
                sync_path="cosmic_intervention",
                since=None,
                event_id=None,
                show_name=None,
                dry_run=True,
            )


class TestExports:
    def test_package_reexports(self):
        import strathmark

        for name in (
            "SyncResult",
            "nightly_batch",
            "strathex_finalization",
            "manual_force_sync",
        ):
            assert hasattr(strathmark, name), f"strathmark missing export: {name}"
