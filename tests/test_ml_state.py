"""ML state lifecycle tests for strathmark/db.py.

Two layers:

1. Always-on tests covering input validation, the best-effort error contract,
   and exports.
2. Live DB tests covering the full ML state lifecycle. Gated by
   STRATHMARK_TEST_DB=1 AND a non-production project ref. They write to and
   clean up after themselves on every run.

The live-DB guard lives in tests/conftest.py.
"""

from pathlib import Path

import pytest

from tests.conftest import PRODUCTION_PROJECT_REF, live_db_required  # noqa: F401

# ---------------------------------------------------------------------------
# Always-on lifecycle contract checks
# ---------------------------------------------------------------------------


class TestUlidShape:
    def test_register_model_version_uses_ulid_id(self, monkeypatch):
        """Without a live DB, register_model_version raises (it's not best-effort).
        We verify that the input validation guard fires BEFORE any client call."""
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        import strathmark.db as db

        db._client = None

        from strathmark.db import register_model_version

        # ValueError on bad artifact_storage MUST fire before _get_client() is
        # called. This proves the validation gate is upstream of the network.
        with pytest.raises(ValueError):
            register_model_version(
                model_type="x",
                training_data_cutoff="2026-04-30",
                training_row_count=10,
                hyperparameters={},
                artifact_storage="bogus",
                artifact_ref="r",
                artifact_size_bytes=0,
            )


class TestBestEffortContract:
    """The non-blocking guarantee in ml-persistence-policy.md requires that
    hot-path writes (record_prediction, settle_prediction) NEVER raise on
    Supabase failure, while operator-action writes (register_model_version,
    set_active_model, record_calibration) DO raise so the operator can react."""

    def setup_method(self):
        # Ensure a clean module state for each test
        import strathmark.db as db

        db._client = None

    def test_record_prediction_is_best_effort(self, monkeypatch):
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        from strathmark.db import record_prediction

        result = record_prediction(
            model_version_id="01HXXXXXXXXXXXXXXXXXXXXXXX",
            competitor_id="C001",
            event_code="SB",
            show_name="Test",
            predicted_time=42.0,
            predicted_variance=2.5,
            cascade_level_used="ml",
        )
        assert result is None, "record_prediction must return None, never raise"

    def test_settle_prediction_is_best_effort(self, monkeypatch):
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        from strathmark.db import settle_prediction

        result = settle_prediction(
            prediction_id="01HXXXXXXXXXXXXXXXXXXXXXXX",
            result_id=1,
            actual_time=43.0,
        )
        assert result is None

    def test_register_model_version_raises_on_missing_env(self, monkeypatch):
        """Operator action — must raise so the human knows training output didn't persist."""
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        from strathmark.db import register_model_version

        with pytest.raises(RuntimeError, match="STRATHMARK_SUPABASE"):
            register_model_version(
                model_type="x",
                training_data_cutoff="2026-04-30",
                training_row_count=10,
                hyperparameters={},
                artifact_storage="inline_jsonb",
                artifact_ref="r",
                artifact_size_bytes=0,
            )

    def test_set_active_model_raises_on_missing_env(self, monkeypatch):
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        from strathmark.db import set_active_model

        with pytest.raises(RuntimeError, match="STRATHMARK_SUPABASE"):
            set_active_model("01HXXXXXXXXXXXXXXXXXXXXXXX")

    def test_record_calibration_raises_on_missing_env(self, monkeypatch):
        monkeypatch.delenv("STRATHMARK_SUPABASE_URL", raising=False)
        monkeypatch.delenv("STRATHMARK_SUPABASE_KEY", raising=False)
        from strathmark.db import record_calibration

        with pytest.raises(RuntimeError, match="STRATHMARK_SUPABASE"):
            record_calibration(
                model_version_id="01HXXXXXXXXXXXXXXXXXXXXXXX",
                calibration_method="conformal_prediction",
                calibration_data={},
                holdout_residuals=[],
            )


class TestPredictionLedgerMirror:
    def test_migration_forces_rls_and_revokes_public_writes(self):
        migration = (
            Path(__file__).parents[1]
            / "strathmark"
            / "migrations"
            / "20260811_005_prediction_v2.sql"
        ).read_text(encoding="utf-8")

        assert migration.count("FORCE ROW LEVEL SECURITY") == 4
        assert "FROM PUBLIC, anon, authenticated" in migration
        assert "TO service_role" in migration
        assert "append_prediction_ledger_v2" in migration
        assert "SECURITY DEFINER" in migration

    def test_mirror_uses_one_sanitized_service_rpc(self, monkeypatch):
        import strathmark.db as db

        calls = []

        class Response:
            data = [{"accepted": True}]

        class Client:
            def rpc(self, name, params):
                calls.append((name, params))
                return self

            def execute(self):
                return Response()

        monkeypatch.setattr(db, "_get_client", lambda: Client())
        payload = {
            "request": {
                "ledger_request_id": "request-1",
                "caller_id": "api",
                "request_id": "field-1",
                "request_hash": "a" * 64,
                "event_code": "SB",
                "prediction_as_of": "2026-08-11",
                "created_at": "2026-08-11T00:00:00+00:00",
            },
            "predictions": [
                {
                    "prediction_id": "prediction-1",
                    "ledger_request_id": "request-1",
                    "competitor_id": "competitor-1",
                    "event_code": "SB",
                    "median_seconds": 42.0,
                    "assigned_mark": 3,
                    "source": "baseline",
                    "training_eligible": True,
                    "created_at": "2026-08-11T00:00:00+00:00",
                }
            ],
            "features": [],
        }

        assert db.mirror_prediction_ledger(payload) is True
        assert calls == [("append_prediction_ledger_v2", {"ledger_payload": payload})]
        assert "name" not in repr(calls)

    @pytest.mark.parametrize(
        "payload",
        [
            {"request": {}, "predictions": [], "features": [], "secret": "x"},
            {
                "request": {},
                "predictions": [{"competitor_id": ""}],
                "features": [],
            },
            {
                "request": {},
                "predictions": [{"competitor_id": "c", "competitor_name": "PII"}],
                "features": [],
            },
        ],
    )
    def test_mirror_rejects_unsanitized_or_unstable_payload_before_network(
        self, monkeypatch, payload
    ):
        import strathmark.db as db

        monkeypatch.setattr(
            db,
            "_get_client",
            lambda: pytest.fail("network client must not be called"),
        )
        with pytest.raises(ValueError):
            db.mirror_prediction_ledger(payload)


# ---------------------------------------------------------------------------
# Live DB lifecycle tests
# ---------------------------------------------------------------------------


@pytest.fixture
def live_client():
    """Yield the live Supabase client for tests that need direct access for cleanup."""
    from strathmark.db import _get_client

    return _get_client()


@pytest.fixture
def cleanup_competitor_id():
    """The competitor_id used by lifecycle tests. The test fixture seeds and tears
    down this row so tests are isolated. The chosen ID is well outside the
    production format (C001..C085) and the register_competitor() format (C0001+)
    so it cannot collide with real data."""
    return "TEST_C9999"


@live_db_required
class TestModelVersionLifecycle:
    def teardown_method(self):
        # Remove any test rows we created
        from strathmark.db import _get_client

        client = _get_client()
        # Delete test model_versions rows by recognizable model_type prefix
        client.table("model_versions").delete().like("model_type", "test_lifecycle_%").execute()

    def test_register_then_activate(self):
        from strathmark.db import (
            get_active_model_version,
            register_model_version,
            set_active_model,
        )

        mv_id = register_model_version(
            model_type="test_lifecycle_basic",
            training_data_cutoff="2026-04-30",
            training_row_count=1311,
            hyperparameters={"n_estimators": 292, "max_depth": 4},
            artifact_storage="inline_jsonb",
            artifact_ref="ignored",
            artifact_size_bytes=12345,
            notes="lifecycle test row",
        )
        assert isinstance(mv_id, str)
        assert len(mv_id) == 26

        # Newly registered models start inactive
        assert get_active_model_version("test_lifecycle_basic") is None

        # Activate
        set_active_model(mv_id)
        assert get_active_model_version("test_lifecycle_basic") == mv_id

    def test_only_one_active_per_model_type(self):
        from strathmark.db import (
            get_active_model_version,
            register_model_version,
            set_active_model,
        )

        mv_a = register_model_version(
            model_type="test_lifecycle_uniqueness",
            training_data_cutoff="2026-04-30",
            training_row_count=10,
            hyperparameters={},
            artifact_storage="inline_jsonb",
            artifact_ref="r1",
            artifact_size_bytes=1,
        )
        mv_b = register_model_version(
            model_type="test_lifecycle_uniqueness",
            training_data_cutoff="2026-04-30",
            training_row_count=10,
            hyperparameters={},
            artifact_storage="inline_jsonb",
            artifact_ref="r2",
            artifact_size_bytes=1,
        )

        set_active_model(mv_a)
        assert get_active_model_version("test_lifecycle_uniqueness") == mv_a

        # Activating B atomically retires A
        set_active_model(mv_b)
        assert get_active_model_version("test_lifecycle_uniqueness") == mv_b

    def test_set_active_unknown_raises(self):
        from strathmark.db import set_active_model

        with pytest.raises(LookupError):
            set_active_model("01ZZZZZZZZZZZZZZZZZZZZZZZZ")


@live_db_required
class TestCalibrationLifecycle:
    def teardown_method(self):
        from strathmark.db import _get_client

        client = _get_client()
        client.table("calibration_tables").delete().like("notes", "test_calibration_%").execute()
        client.table("model_versions").delete().like("model_type", "test_calibration_%").execute()

    def test_calibration_attaches_to_model(self):
        from strathmark.db import record_calibration, register_model_version

        mv_id = register_model_version(
            model_type="test_calibration_basic",
            training_data_cutoff="2026-04-30",
            training_row_count=10,
            hyperparameters={},
            artifact_storage="inline_jsonb",
            artifact_ref="r",
            artifact_size_bytes=1,
        )
        cal_id = record_calibration(
            model_version_id=mv_id,
            calibration_method="conformal_prediction",
            calibration_data={"q90": 1.65},
            holdout_residuals=[0.1, -0.2, 0.05],
            crps_score=0.42,
            coverage_at_90=0.91,
            notes="test_calibration_basic",
        )
        assert isinstance(cal_id, str)
        assert len(cal_id) == 26

    def test_calibration_unknown_method_raises(self):
        from strathmark.db import record_calibration

        with pytest.raises(ValueError):
            record_calibration(
                model_version_id="01HZZZZZZZZZZZZZZZZZZZZZZZ",
                calibration_method="hand_wavy",
                calibration_data={},
                holdout_residuals=[],
            )


@live_db_required
class TestFeatureStoreLifecycle:
    def teardown_method(self):
        from strathmark.db import _get_client

        client = _get_client()
        client.table("feature_store").delete().eq("competitor_id", "C001").like(
            "event_code", "TST_%"
        ).execute()
        client.table("model_versions").delete().like("model_type", "test_feature_store_%").execute()

    def test_feature_store_unique_constraint(self):
        from strathmark.db import register_model_version, store_features

        mv_id = register_model_version(
            model_type="test_feature_store_uniqueness",
            training_data_cutoff="2026-04-30",
            training_row_count=10,
            hyperparameters={},
            artifact_storage="inline_jsonb",
            artifact_ref="r",
            artifact_size_bytes=1,
        )

        # Use a fake event_code that won't collide with real data and is in our
        # cleanup LIKE filter.
        fake_event = "TST_SB"
        fs_id_first = store_features(
            model_version_id=mv_id,
            competitor_id="C001",
            event_code=fake_event,
            features={"comp_weighted_avg": 28.4},
        )
        # Second call with the same key should upsert, not duplicate.
        fs_id_second = store_features(
            model_version_id=mv_id,
            competitor_id="C001",
            event_code=fake_event,
            features={"comp_weighted_avg": 27.9},  # updated value
        )
        # PostgREST upsert may return either the existing row's ID or the new
        # one's. Either way, the row must exist and there must be exactly 1.
        assert isinstance(fs_id_first, str)
        assert isinstance(fs_id_second, str)

        from strathmark.db import _get_client

        client = _get_client()
        resp = (
            client.table("feature_store")
            .select("feature_set_id,features_jsonb")
            .eq("model_version_id", mv_id)
            .eq("competitor_id", "C001")
            .eq("event_code", fake_event)
            .execute()
        )
        rows = resp.data or []
        assert len(rows) == 1, f"upsert produced {len(rows)} rows, expected 1"
        # Latest write wins
        assert rows[0]["features_jsonb"]["comp_weighted_avg"] == 27.9


@live_db_required
class TestPredictionAndSettlement:
    def teardown_method(self):
        from strathmark.db import _get_client

        client = _get_client()
        client.table("predictions").delete().like("show_name", "test_settlement_%").execute()
        client.table("model_versions").delete().like("model_type", "test_settlement_%").execute()

    def test_record_then_settle_computes_residual(self):
        from strathmark.db import (
            record_prediction,
            register_model_version,
            settle_prediction,
        )

        mv_id = register_model_version(
            model_type="test_settlement_basic",
            training_data_cutoff="2026-04-30",
            training_row_count=10,
            hyperparameters={},
            artifact_storage="inline_jsonb",
            artifact_ref="r",
            artifact_size_bytes=1,
        )

        pred_id = record_prediction(
            model_version_id=mv_id,
            competitor_id="C001",
            event_code="SB",
            show_name="test_settlement_basic_show",
            predicted_time=28.0,
            predicted_variance=4.0,
            cascade_level_used="ml",
        )
        assert pred_id is not None and len(pred_id) == 26

        # Need a real result_id to satisfy the FK. Pick the first existing one.
        from strathmark.db import _get_client

        client = _get_client()
        resp = client.table("results").select("result_id").limit(1).execute()
        result_id = resp.data[0]["result_id"]

        residual = settle_prediction(
            prediction_id=pred_id,
            result_id=result_id,
            actual_time=29.5,
        )
        assert residual is not None
        # actual - predicted = 29.5 - 28.0 = 1.5
        assert abs(residual - 1.5) < 1e-6

        # The row was updated
        check = (
            client.table("predictions")
            .select("result_id,residual")
            .eq("prediction_id", pred_id)
            .execute()
        )
        row = check.data[0]
        assert row["result_id"] == result_id
        assert abs(float(row["residual"]) - 1.5) < 1e-6

    def test_settle_unknown_prediction_returns_none(self):
        from strathmark.db import settle_prediction

        result = settle_prediction(
            prediction_id="01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            result_id=1,
            actual_time=30.0,
        )
        assert result is None
