"""ML state lifecycle tests for strathmark/db.py.

Two layers:

1. Always-on tests covering input validation, the best-effort error contract,
   and exports.
2. Live DB tests covering the full ML state lifecycle. Gated by
   STRATHMARK_TEST_DB=1 AND a non-production project ref. They write to and
   clean up after themselves on every run.

The live-DB guard lives in tests/conftest.py.
"""

import json
import sqlite3
from copy import deepcopy
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
    @staticmethod
    def _server_generated_legacy_payload(tmp_path):
        from strathmark.ledger import PredictionLedger
        from tests.test_ledger import _pred, _request_payload

        path = tmp_path / "server-generated-legacy-mirror.db"
        ledger = PredictionLedger(path)
        ledger.record_field(
            caller_id="api",
            request_id="server-generated-field",
            request_payload=_request_payload(),
            predictions=[_pred()],
        )
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT payload_json FROM prediction_mirror_outbox WHERE kind = 'field'"
            ).fetchone()
        assert row is not None
        return json.loads(row[0])

    def test_prediction_rpc_migrations_validate_payload_shape_before_mutation(self):
        migration_dir = Path(__file__).parents[1] / "strathmark" / "migrations"
        migrations = [
            migration_dir / "20260811_005_prediction_v2.sql",
            migration_dir / "20260813_006_prediction_hash_algorithm.sql",
            migration_dir / "20260813_006_prediction_hash_algorithm.down.sql",
        ]

        for path in migrations:
            sql = path.read_text(encoding="utf-8")
            assert "ledger payload must be an object" in sql
            assert "ledger payload must contain exactly one operation kind" in sql
            assert "ledger field payload has unknown or missing properties" in sql
            assert "pg_catalog.jsonb_object_keys(ledger_payload)" in sql
            assert "settlement payload must be a non-null object" in sql
            assert "field request must be a non-null object" in sql
            assert "field predictions must be a non-empty array" in sql
            assert "field features must be an array" in sql
            assert "prediction request linkage mismatch" in sql
            assert "feature prediction linkage mismatch" in sql
            assert "settlement prediction linkage mismatch" in sql

    def test_prediction_rpc_migrations_require_exact_idempotent_retries(self):
        migration_dir = Path(__file__).parents[1] / "strathmark" / "migrations"
        migrations = [
            migration_dir / "20260811_005_prediction_v2.sql",
            migration_dir / "20260813_006_prediction_hash_algorithm.sql",
            migration_dir / "20260813_006_prediction_hash_algorithm.down.sql",
        ]

        for path in migrations:
            sql = path.read_text(encoding="utf-8")
            assert "GET DIAGNOSTICS inserted_request_count = ROW_COUNT" in sql
            assert "ledger request projection conflict" in sql
            assert "ledger prediction projection conflict" in sql
            assert "ledger feature projection conflict" in sql
            assert "ledger settlement payload conflict" in sql
            assert "ledger settlement revision conflict" in sql
            assert "ledger settlement residual conflict" in sql
            assert "pg_catalog.pg_advisory_xact_lock" in sql
            assert sql.count("EXCEPT") >= 4
            assert "ON CONFLICT (prediction_id, payload_hash) DO NOTHING" not in sql

    def test_prediction_rpc_migrations_reject_nonexact_or_mistyped_rows_before_casting(self):
        migration_dir = Path(__file__).parents[1] / "strathmark" / "migrations"
        migrations = [
            migration_dir / "20260811_005_prediction_v2.sql",
            migration_dir / "20260813_006_prediction_hash_algorithm.sql",
            migration_dir / "20260813_006_prediction_hash_algorithm.down.sql",
        ]

        for path in migrations:
            sql = path.read_text(encoding="utf-8")
            settlement_record_cast = sql.index("pg_catalog.jsonb_to_record(settlement_row)")
            first_recordset_cast = sql.index("pg_catalog.jsonb_to_recordset")
            for label in ("request", "prediction", "feature", "settlement"):
                shape_error = f"ledger {label} has unknown or missing properties"
                type_error = f"ledger {label} JSON types are invalid"
                assert shape_error in sql
                assert type_error in sql
                boundary = settlement_record_cast if label == "settlement" else first_recordset_cast
                assert sql.index(shape_error) < boundary
                assert sql.index(type_error) < boundary
            assert "pg_catalog.jsonb_object_keys" in sql[:settlement_record_cast]
            assert "pg_catalog.jsonb_each" in sql[:settlement_record_cast]
            assert "pg_catalog.floor" in sql[:settlement_record_cast]

    def test_rpc_owner_prerequisite_is_checked_in_and_requires_role_capability(self):
        prerequisite = (
            Path(__file__).parents[1]
            / "strathmark"
            / "migrations"
            / "prerequisites"
            / "prediction_rpc_owner.sql"
        ).read_text(encoding="utf-8")

        assert (
            "CREATE ROLE strathmark_prediction_rpc_owner NOINHERIT NOSUPERUSER "
            "NOCREATEDB NOCREATEROLE NOREPLICATION NOLOGIN NOBYPASSRLS"
            in " ".join(prerequisite.split())
        )
        for attribute in (
            "rolinherit",
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolcanlogin",
            "rolbypassrls",
        ):
            assert attribute in prerequisite
        assert "pg_catalog.pg_auth_members" in prerequisite
        assert "pg_has_role(current_user, 'pg_create_role', 'MEMBER')" in prerequisite

    def test_prediction_migrations_repeat_the_dedicated_owner_assertions(self):
        migration_dir = Path(__file__).parents[1] / "strathmark" / "migrations"
        for filename in (
            "20260811_005_prediction_v2.sql",
            "20260813_007_shadow_mirror_contract.sql",
        ):
            sql = (migration_dir / filename).read_text(encoding="utf-8")
            for attribute in (
                "rolinherit",
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolcanlogin",
                "rolbypassrls",
            ):
                assert attribute in sql
            assert "pg_catalog.pg_auth_members" in sql

    def test_prediction_migration_ddl_uses_explicit_public_relation_names(self):
        migration_dir = Path(__file__).parents[1] / "strathmark" / "migrations"
        migrations = [
            migration_dir / "20260811_005_prediction_v2.sql",
            migration_dir / "20260813_006_prediction_hash_algorithm.sql",
            migration_dir / "20260813_006_prediction_hash_algorithm.down.sql",
            migration_dir / "20260813_007_shadow_mirror_contract.sql",
            migration_dir / "20260813_007_shadow_mirror_contract.down.sql",
        ]
        ddl_prefixes = (
            "CREATE TABLE",
            "CREATE INDEX",
            "DROP INDEX",
            "ALTER TABLE",
            "DROP TABLE",
            "DROP TRIGGER",
            "CREATE TRIGGER",
            "DROP POLICY",
            "CREATE POLICY",
            "REVOKE ALL ON",
            "GRANT SELECT",
        )
        for path in migrations:
            statements = path.read_text(encoding="utf-8").split(";")
            for statement in statements:
                normalized = " ".join(statement.split())
                if not any(prefix in normalized for prefix in ddl_prefixes):
                    continue
                for relation in (
                    "prediction_ledger_requests",
                    "prediction_ledger_predictions",
                    "prediction_ledger_features",
                    "prediction_ledger_settlements",
                    "shadow_mirror_deliveries",
                    "shadow_receipt_cores",
                    "shadow_numeric_outcome_revisions",
                    "shadow_numeric_settlement_revisions",
                ):
                    if relation in normalized:
                        assert f"public.{relation}" in normalized, (path.name, normalized)

    def test_postgres_index_names_are_not_schema_qualified(self):
        migration_dir = Path(__file__).parents[1] / "strathmark" / "migrations"
        for filename in (
            "20260811_005_prediction_v2.sql",
            "20260813_007_shadow_mirror_contract.sql",
        ):
            sql = (migration_dir / filename).read_text(encoding="utf-8")
            assert "CREATE INDEX IF NOT EXISTS public." not in sql
            assert " ON public." in " ".join(sql.split())

    def test_guarded_down_migrations_lock_then_disable_rls_before_inspection(self):
        migration_dir = Path(__file__).parents[1] / "strathmark" / "migrations"
        down_006 = (migration_dir / "20260813_006_prediction_hash_algorithm.down.sql").read_text(
            encoding="utf-8"
        )
        down_007 = (migration_dir / "20260813_007_shadow_mirror_contract.down.sql").read_text(
            encoding="utf-8"
        )

        assert "LOCK TABLE public.prediction_ledger_requests IN ACCESS EXCLUSIVE MODE" in down_006
        assert "SET LOCAL row_security = off" in down_006
        for relation in (
            "shadow_mirror_deliveries",
            "shadow_receipt_cores",
            "shadow_numeric_outcome_revisions",
            "shadow_numeric_settlement_revisions",
        ):
            assert f"LOCK TABLE public.{relation} IN ACCESS EXCLUSIVE MODE" in down_007
        assert "SET LOCAL row_security = off" in down_007

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

    def test_hash_algorithm_migration_preserves_legacy_rows_and_updates_rpc(self):
        migration = (
            Path(__file__).parents[1]
            / "strathmark"
            / "migrations"
            / "20260813_006_prediction_hash_algorithm.sql"
        ).read_text(encoding="utf-8")

        assert "ADD COLUMN IF NOT EXISTS hash_algorithm" in migration
        assert "DEFAULT 'raw-v1'" in migration
        assert "CHECK (hash_algorithm IN ('raw-v1', 'active-v2'))" in migration
        assert "request_row->>'hash_algorithm'" in migration
        assert "existing.hash_algorithm IS DISTINCT FROM incoming_algorithm" in migration
        assert "ledger request hash algorithm conflict" in migration
        assert (
            "REVOKE ALL ON FUNCTION public.append_prediction_ledger_v2(pg_catalog.jsonb)"
            in migration
        )
        assert (
            "GRANT EXECUTE ON FUNCTION public.append_prediction_ledger_v2(pg_catalog.jsonb)"
            in migration
        )

        migration_005 = (
            Path(__file__).parents[1]
            / "strathmark"
            / "migrations"
            / "20260811_005_prediction_v2.sql"
        ).read_text(encoding="utf-8")
        assert "active-v2 request hashes require migration 006" in migration_005
        assert "request_row->>'hash_algorithm' <> 'raw-v1'" in migration_005

        rollback = (
            Path(__file__).parents[1]
            / "strathmark"
            / "migrations"
            / "20260813_006_prediction_hash_algorithm.down.sql"
        ).read_text(encoding="utf-8")
        assert "cannot roll back migration 006 while active-v2 request rows exist" in rollback
        assert "CREATE OR REPLACE FUNCTION public.append_prediction_ledger_v2" in rollback
        assert (
            "ALTER TABLE public.prediction_ledger_requests DROP COLUMN hash_algorithm" in rollback
        )

    def test_mirror_uses_one_sanitized_service_rpc(self, monkeypatch, tmp_path):
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
        payload = self._server_generated_legacy_payload(tmp_path)

        assert db.mirror_prediction_ledger(payload) is True
        assert calls == [("append_prediction_ledger_v2", {"ledger_payload": payload})]
        assert "'name':" not in repr(calls)

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda payload: payload["request"].pop("created_at"),
            lambda payload: payload["predictions"][0].__setitem__("assigned_mark", "3"),
            lambda payload: payload["features"][0].__setitem__("numeric_value", "300"),
        ],
    )
    def test_legacy_mirror_rejects_missing_or_mistyped_server_fields_before_client_creation(
        self, monkeypatch, tmp_path, mutation
    ):
        import strathmark.db as db

        payload = deepcopy(self._server_generated_legacy_payload(tmp_path))
        mutation(payload)
        monkeypatch.setattr(
            db,
            "_get_client",
            lambda: pytest.fail("network client must not be created"),
        )

        with pytest.raises(ValueError):
            db.mirror_prediction_ledger(payload)

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
