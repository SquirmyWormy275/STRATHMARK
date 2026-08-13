"""Safety and executable semantics tests for the disposable PostgreSQL rehearsal."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import tests.postgres_rehearsal as rehearsal
from tests.postgres_rehearsal import (
    PRODUCTION_PROJECT_REF,
    RehearsalExecutionError,
    RehearsalTargetError,
    required_rehearsal_dsn,
    run_prediction_v2_rehearsal,
    scrubbed_rehearsal_environment,
    validate_rehearsal_dsn,
)


@pytest.mark.parametrize(
    "dsn, message",
    [
        ("postgresql://postgres:secret@example.com/strathmark_rehearsal_ci", "loopback"),
        ("postgresql://postgres:secret@127.0.0.1/postgres", "production"),
        ("postgresql://postgres:secret@localhost/production", "production"),
        (
            f"postgresql://postgres:secret@127.0.0.1/{PRODUCTION_PROJECT_REF}",
            "production",
        ),
        (
            "postgresql://postgres:secret@127.0.0.1/strathmark_rehearsal_ci?host=example.com",
            "connection override",
        ),
    ],
)
def test_rehearsal_target_rejects_unsafe_connections(dsn: str, message: str) -> None:
    with pytest.raises(RehearsalTargetError, match=message):
        validate_rehearsal_dsn(dsn)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_rehearsal_target_accepts_only_named_loopback_disposable_database(host: str) -> None:
    target = validate_rehearsal_dsn(
        f"postgresql://rehearsal:secret@{host}:5432/strathmark_rehearsal_controller"
    )

    assert target.database == "strathmark_rehearsal_controller"
    assert target.port == 5432


def test_rehearsal_environment_scrubs_ambient_cloud_and_pg_connections() -> None:
    ambient = {
        "PATH": os.environ.get("PATH", ""),
        "STRATHMARK_SUPABASE_URL": "https://production.invalid",
        "STRATHMARK_SUPABASE_KEY": "secret",
        "SUPABASE_URL": "https://production.invalid",
        "SUPABASE_SERVICE_ROLE_KEY": "secret",
        "DATABASE_URL": "postgresql://production.invalid/postgres",
        "RAILWAY_DATABASE_URL": "postgresql://production.invalid/postgres",
        "PGHOST": "production.invalid",
        "PGDATABASE": "postgres",
        "PGUSER": "production",
        "PGPASSWORD": "secret",
        "PGSERVICE": "production",
    }

    clean = scrubbed_rehearsal_environment(ambient)

    assert clean["PATH"] == ambient["PATH"]
    assert not set(ambient).intersection(clean) - {"PATH"}
    assert clean["PGCONNECT_TIMEOUT"] == "5"


def test_missing_rehearsal_dsn_fails_closed_in_ci() -> None:
    with pytest.raises(RehearsalTargetError, match="required in CI"):
        required_rehearsal_dsn({"CI": "true"})


def test_partial_role_creation_is_tracked_for_cleanup(monkeypatch) -> None:
    target = validate_rehearsal_dsn(
        "postgresql://rehearsal:secret@127.0.0.1/strathmark_rehearsal_controller"
    )
    commands: list[str] = []

    def fake_psql(_target, _dsn, *, sql=None, sql_file=None, expect_error=None):
        del _target, _dsn, sql_file, expect_error
        commands.append(sql or "")
        if sql and sql.startswith("SELECT rolname"):
            return ""
        if sql and "CREATE ROLE authenticated" in sql:
            raise RehearsalExecutionError("role creation failed")
        return ""

    monkeypatch.setattr(rehearsal, "_psql", fake_psql)

    with pytest.raises(RehearsalExecutionError, match="role creation failed"):
        run_prediction_v2_rehearsal(Path(__file__).parents[1], target.dsn)

    assert "DROP ROLE IF EXISTS anon;" in commands


def test_cleanup_failure_is_reported_with_primary_failure_as_cause(monkeypatch) -> None:
    target = validate_rehearsal_dsn(
        "postgresql://rehearsal:secret@127.0.0.1/strathmark_rehearsal_controller"
    )

    def fake_create_roles(_target, created):
        del _target
        created.append("anon")

    def fake_psql(_target, _dsn, *, sql=None, sql_file=None, expect_error=None):
        del _target, _dsn, sql_file, expect_error
        if sql and sql.startswith("DROP ROLE"):
            raise RehearsalExecutionError("role drop failed")
        return ""

    monkeypatch.setattr(rehearsal, "_create_roles", fake_create_roles)
    monkeypatch.setattr(rehearsal, "_psql", fake_psql)
    monkeypatch.setattr(
        rehearsal,
        "_run_matrix",
        lambda *_args: (_ for _ in ()).throw(RehearsalExecutionError("matrix failed")),
    )

    with pytest.raises(RehearsalExecutionError, match="cleanup failed") as captured:
        run_prediction_v2_rehearsal(Path(__file__).parents[1], target.dsn)

    assert "role drop failed" in str(captured.value)
    assert isinstance(captured.value.__cause__, RehearsalExecutionError)
    assert "matrix failed" in str(captured.value.__cause__)


def test_prediction_v2_migrations_execute_against_disposable_postgres() -> None:
    """Run the full 005/006 matrix only with an explicit safe controller DSN."""
    dsn = required_rehearsal_dsn(os.environ)
    if not dsn:
        pytest.skip("set STRATHMARK_REHEARSAL_DSN to a loopback disposable controller")

    report = run_prediction_v2_rehearsal(Path(__file__).parents[1], dsn)

    assert report.database.startswith("strathmark_rehearsal_")
    assert report.checks_run >= 20
    assert report.database_dropped is True
