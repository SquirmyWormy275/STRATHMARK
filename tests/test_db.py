"""Tests for strathmark/db.py -- Supabase/PostgreSQL backend.

Two modes:

1. Always-on import + signature checks. These run on every CI environment.
2. Live Supabase tests. Gated by STRATHMARK_TEST_DB=1 AND a non-production
   project ref. Run only against an isolated test project.

The live-DB guard lives in tests/conftest.py and is shared with other
test files that touch real Supabase.
"""

import pytest

from tests.conftest import PRODUCTION_PROJECT_REF, live_db_required  # noqa: F401

# ---------------------------------------------------------------------------
# Always-on: imports and exports
# ---------------------------------------------------------------------------


class TestSupabaseBackendImports:
    def test_existing_imports(self):
        from strathmark.db import (  # noqa: F401
            get_competitor_bias,
            log_sync,
            pull_competitors,
            pull_results,
            push_competitors,
            push_results,
            push_results_dicts,
            record_prediction_residuals,
            register_competitor,
        )

    def test_ml_state_imports(self):
        from strathmark.db import (  # noqa: F401
            get_active_model_version,
            record_calibration,
            record_prediction,
            register_model_version,
            set_active_model,
            settle_prediction,
            store_features,
        )

    def test_ml_state_reexports_from_package(self):
        import strathmark

        for name in (
            "register_model_version",
            "set_active_model",
            "get_active_model_version",
            "record_calibration",
            "store_features",
            "record_prediction",
            "settle_prediction",
        ):
            assert hasattr(strathmark, name), f"strathmark missing export: {name}"
            assert callable(getattr(strathmark, name))


# ---------------------------------------------------------------------------
# Always-on: input validation (no network)
# ---------------------------------------------------------------------------


class TestRegisterModelVersionValidation:
    def test_invalid_artifact_storage_raises(self):
        from strathmark.db import register_model_version

        with pytest.raises(ValueError, match="artifact_storage"):
            register_model_version(
                model_type="xgboost_lightgbm_ensemble",
                training_data_cutoff="2026-04-30",
                training_row_count=1311,
                hyperparameters={"foo": "bar"},
                artifact_storage="some-s3-bucket",  # not allowed
                artifact_ref="ignored",
                artifact_size_bytes=0,
            )


class TestRecordCalibrationValidation:
    def test_unknown_calibration_method_raises(self):
        from strathmark.db import record_calibration

        with pytest.raises(ValueError, match="calibration_method"):
            record_calibration(
                model_version_id="01HXXXXXXXXXXXXXXXXXXXXXXX",
                calibration_method="hand_wavy",  # not allowed
                calibration_data={},
                holdout_residuals=[],
            )


class TestRecordPredictionValidation:
    def test_unknown_cascade_level_returns_none(self):
        """Validation runs inside the function's try/except so the non-blocking
        guarantee on the prediction hot path holds even when a caller passes
        a bad value. The function logs a warning and returns None rather
        than raising into the cascade."""
        from strathmark.db import record_prediction

        result = record_prediction(
            model_version_id="01HXXXXXXXXXXXXXXXXXXXXXXX",
            competitor_id="C001",
            event_code="SB",
            show_name="Test Show",
            predicted_time=42.0,
            predicted_variance=2.5,
            cascade_level_used="vibes",  # not allowed
        )
        assert result is None

    def test_record_prediction_returns_none_when_supabase_unreachable(self, monkeypatch):
        """Best-effort write must return None, never raise, on Supabase failure."""
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        import strathmark.db as db

        db._client = None

        from strathmark.db import record_prediction

        # cascade_level_used is valid, env vars are missing -> _get_client() raises
        # -> record_prediction must catch and return None.
        result = record_prediction(
            model_version_id="01HXXXXXXXXXXXXXXXXXXXXXXX",
            competitor_id="C001",
            event_code="SB",
            show_name="Test Show",
            predicted_time=42.0,
            predicted_variance=2.5,
            cascade_level_used="ml",
        )
        assert result is None


class TestSettlePredictionFailureMode:
    def test_settle_prediction_returns_none_when_supabase_unreachable(self, monkeypatch):
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        import strathmark.db as db

        db._client = None

        from strathmark.db import settle_prediction

        result = settle_prediction(
            prediction_id="01HXXXXXXXXXXXXXXXXXXXXXXX",
            result_id=1,
            actual_time=43.5,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Always-on: ULID helper sanity
# ---------------------------------------------------------------------------


class TestUlidHelper:
    def test_new_ulid_is_26_chars(self):
        from strathmark.db import _new_ulid

        u = _new_ulid()
        assert isinstance(u, str)
        assert len(u) == 26

    def test_new_ulid_returns_distinct_values(self):
        from strathmark.db import _new_ulid

        a, b = _new_ulid(), _new_ulid()
        assert a != b


# ---------------------------------------------------------------------------
# Live DB: schema-existence smoke checks (gated)
# ---------------------------------------------------------------------------


@live_db_required
class TestSchemaExistsLive:
    """Confirm the migration columns and tables actually exist in the test DB."""

    def test_results_has_source_tracking_columns(self):
        from strathmark.db import _get_client

        client = _get_client()
        # selecting the new columns must not raise; if any column is missing,
        # PostgREST returns an error.
        resp = (
            client.table("results")
            .select("result_id,mnemex_id,source_type,last_synced_at,field_strength")
            .limit(1)
            .execute()
        )
        assert resp.data is not None  # may be [] but not None

    def test_competitors_has_mnemex_id(self):
        from strathmark.db import _get_client

        client = _get_client()
        resp = client.table("competitors").select("competitor_id,mnemex_id").limit(1).execute()
        assert resp.data is not None

    def test_sync_log_has_operational_columns(self):
        from strathmark.db import _get_client

        client = _get_client()
        resp = (
            client.table("sync_log")
            .select("sync_id,sync_path,mnemex_cursor,rows_pulled,rows_upserted,errors_jsonb")
            .limit(1)
            .execute()
        )
        assert resp.data is not None

    def test_ml_state_tables_exist(self):
        from strathmark.db import _get_client

        client = _get_client()
        for table in (
            "model_versions",
            "calibration_tables",
            "feature_store",
            "predictions",
        ):
            resp = client.table(table).select("*").limit(1).execute()
            assert resp.data is not None, f"table {table} returned None"

    def test_prediction_residuals_has_model_linkage_columns(self):
        from strathmark.db import _get_client

        client = _get_client()
        resp = (
            client.table("prediction_residuals")
            .select("residual_id,model_version_id,prediction_id")
            .limit(1)
            .execute()
        )
        assert resp.data is not None
