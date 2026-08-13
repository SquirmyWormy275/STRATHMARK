"""Disposable, loopback-only PostgreSQL migration rehearsal support.

This module deliberately lives under ``tests``.  It is release verification
infrastructure, not a general-purpose migration runner and never accepts a hosted
database target.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

PRODUCTION_PROJECT_REF = "iordtvxryrdhqvdkfgzf"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_DATABASE_PATTERN = re.compile(r"^strathmark_rehearsal_[a-z0-9_]+$")
_KNOWN_PRODUCTION_DATABASES = {
    "postgres",
    "prod",
    "production",
    "strathmark",
    "strathmark_production",
}
_BOOTSTRAP_ROLES = ("anon", "authenticated", "service_role")
_ROLES = (*_BOOTSTRAP_ROLES, "strathmark_prediction_rpc_owner")
_AMBIENT_CONNECTION_KEYS = {
    "DATABASE_URL",
    "DIRECT_URL",
    "PGDATABASE",
    "PGHOST",
    "PGHOSTADDR",
    "PGOPTIONS",
    "PGPASSWORD",
    "PGPORT",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGUSER",
    "RAILWAY_DATABASE_URL",
    "STRATHMARK_REHEARSAL_DSN",
    "STRATHMARK_SUPABASE_KEY",
    "STRATHMARK_SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_DB_URL",
    "SUPABASE_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_URL",
}


class RehearsalTargetError(ValueError):
    """Raised before any process is launched for an unsafe database target."""


class RehearsalExecutionError(RuntimeError):
    """Raised when an isolated rehearsal command violates an expected behavior."""


@dataclass(frozen=True)
class RehearsalTarget:
    dsn: str
    host: str
    port: int
    database: str
    password: str | None

    def database_dsn(self, database: str) -> str:
        if not _DATABASE_PATTERN.fullmatch(database):
            raise RehearsalTargetError("generated database name is not disposable")
        parsed = urlsplit(self.dsn)
        return urlunsplit(parsed._replace(path=f"/{database}"))


@dataclass(frozen=True)
class RehearsalReport:
    database: str
    checks_run: int
    database_dropped: bool


def validate_rehearsal_dsn(dsn: str) -> RehearsalTarget:
    """Validate a controller DSN without opening a socket."""
    if not dsn or PRODUCTION_PROJECT_REF.lower() in unquote(dsn).lower():
        raise RehearsalTargetError("known production target is forbidden")

    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RehearsalTargetError("rehearsal DSN must use postgresql")
    if parsed.query or parsed.fragment:
        raise RehearsalTargetError("connection override parameters are forbidden")
    if parsed.hostname not in _LOOPBACK_HOSTS:
        raise RehearsalTargetError("rehearsal database host must be loopback")
    if not parsed.username:
        raise RehearsalTargetError("rehearsal DSN must include an explicit user")

    database = unquote(parsed.path.lstrip("/"))
    if database.lower() in _KNOWN_PRODUCTION_DATABASES:
        raise RehearsalTargetError("known production database name is forbidden")
    if not _DATABASE_PATTERN.fullmatch(database):
        raise RehearsalTargetError("database name must identify a disposable rehearsal")

    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise RehearsalTargetError("rehearsal DSN contains an invalid port") from exc
    return RehearsalTarget(dsn, parsed.hostname, port, database, parsed.password)


def scrubbed_rehearsal_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child-process environment with ambient cloud/PG targets removed."""
    clean = dict(os.environ if source is None else source)
    for key in tuple(clean):
        upper = key.upper()
        if (
            upper in _AMBIENT_CONNECTION_KEYS
            or "SUPABASE" in upper
            or upper.startswith("RAILWAY_")
            or upper.startswith("PG")
        ):
            clean.pop(key, None)
    clean["PGCONNECT_TIMEOUT"] = "5"
    return clean


def required_rehearsal_dsn(environment: Mapping[str, str]) -> str | None:
    """Return the explicit rehearsal DSN, failing closed when CI expects the gate."""
    dsn = environment.get("STRATHMARK_REHEARSAL_DSN")
    if dsn:
        return dsn
    if environment.get("CI", "").strip().lower() in {"1", "true", "yes"}:
        raise RehearsalTargetError("STRATHMARK_REHEARSAL_DSN is required in CI")
    return None


def _redact(text: str, target: RehearsalTarget) -> str:
    if target.password:
        return text.replace(target.password, "<redacted>")
    return text


def _psql(
    target: RehearsalTarget,
    dsn: str,
    *,
    sql: str | None = None,
    sql_file: Path | None = None,
    expect_error: str | None = None,
) -> str:
    if (sql is None) == (sql_file is None):
        raise ValueError("provide exactly one of sql or sql_file")
    command = [
        "psql",
        "--no-psqlrc",
        "--set=ON_ERROR_STOP=1",
        "--quiet",
        "--tuples-only",
        "--no-align",
        f"--dbname={dsn}",
    ]
    if sql_file is not None:
        command.extend(("--file", str(sql_file)))
    else:
        command.extend(("--command", sql or ""))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=scrubbed_rehearsal_environment(),
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RehearsalExecutionError("psql is required for the migration rehearsal") from exc

    output = f"{result.stdout}\n{result.stderr}".strip()
    if expect_error is not None:
        if result.returncode == 0:
            raise RehearsalExecutionError(f"expected PostgreSQL error containing {expect_error!r}")
        if expect_error.lower() not in output.lower():
            raise RehearsalExecutionError(
                f"unexpected PostgreSQL error; wanted {expect_error!r}: {_redact(output, target)}"
            )
        return output
    if result.returncode:
        raise RehearsalExecutionError(_redact(output, target))
    return result.stdout.strip()


def _assert_scalar(
    target: RehearsalTarget,
    dsn: str,
    sql: str,
    expected: str,
    description: str,
) -> None:
    actual = _psql(target, dsn, sql=sql).splitlines()
    value = actual[-1].strip() if actual else ""
    if value != expected:
        raise RehearsalExecutionError(f"{description}: expected {expected!r}, received {value!r}")


def _json_literal(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).replace("'", "''")


def _field_payload(
    suffix: str,
    *,
    request_hash: str,
    hash_algorithm: str | None,
    competitor_id: str = "C001",
) -> dict[str, object]:
    request: dict[str, object] = {
        "ledger_request_id": f"ledger-{suffix}",
        "caller_id": "migration-rehearsal",
        "request_id": f"request-{suffix}",
        "request_hash": request_hash,
        "event_code": "SB",
        "prediction_as_of": "2026-08-01",
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    if hash_algorithm is not None:
        request["hash_algorithm"] = hash_algorithm
    return {
        "request": request,
        "predictions": [
            {
                "prediction_id": f"prediction-{suffix}",
                "ledger_request_id": f"ledger-{suffix}",
                "competitor_id": competitor_id,
                "event_code": "SB",
                "median_seconds": 42.5,
                "assigned_mark": 3,
                "source": "rehearsal",
                "training_eligible": True,
                "engine_version": "rehearsal",
                "model_version": "rehearsal",
                "calibration_version": "rehearsal",
                "evidence_cutoff": "2026-08-01",
                "interval_lower": 40.0,
                "interval_upper": 45.0,
                "interval_coverage": 0.9,
                "interval_state": "calibrated",
                "interval_scope": "competitor",
                "ignored_factors": [],
                "warnings": [],
                "optimizer": "rehearsal",
                "optimizer_metadata": {},
                "created_at": "2026-08-01T00:00:00+00:00",
            }
        ],
        "features": [
            {
                "feature_snapshot_id": f"feature-{suffix}",
                "prediction_id": f"prediction-{suffix}",
                "feature_name": "history_count",
                "numeric_value": 2.0,
                "created_at": "2026-08-01T00:00:00+00:00",
            }
        ],
    }


def _rpc_sql(payload: object, role: str = "service_role") -> str:
    return (
        f"SET ROLE {role}; SELECT public.append_prediction_ledger_v2"
        f"('{_json_literal(payload)}'::pg_catalog.jsonb); RESET ROLE;"
    )


def _assert_invalid_payloads(target: RehearsalTarget, dsn: str, suffix: str) -> int:
    """Exercise explicit shape/linkage rejection against the installed RPC version."""
    field = _field_payload(f"invalid-{suffix}", request_hash="9" * 64, hash_algorithm="raw-v1")
    ambiguous = json.loads(json.dumps(field))
    ambiguous["settlement"] = None
    bad_prediction_link = json.loads(json.dumps(field))
    bad_prediction_link["predictions"][0]["ledger_request_id"] = "another-request"
    bad_feature_link = json.loads(json.dumps(field))
    bad_feature_link["features"][0]["prediction_id"] = "another-prediction"
    bad_settlement_link = {
        "settlement": {
            "settlement_id": f"settlement-invalid-link-{suffix}",
            "prediction_id": "prediction-raw",
            "revision": 1,
            "competitor_id": "C002",
            "event_code": "SB",
            "actual_time": 43.0,
            "residual": 0.5,
            "actor": "migration-rehearsal",
            "payload_hash": "8" * 64,
            "settled_at": "2026-08-02T00:00:00+00:00",
        }
    }
    cases: list[tuple[object, str]] = [
        ([], "ledger payload must be an object"),
        ({"settlement": None}, "settlement payload must be a non-null object"),
        (ambiguous, "ledger payload must contain exactly one operation kind"),
        ({}, "field request must be a non-null object"),
        ({"request": {}, "predictions": {}, "features": []}, "non-empty array"),
        ({"request": {}, "predictions": [], "features": []}, "non-empty array"),
        ({"request": {}, "predictions": [{}]}, "field features must be an array"),
        (bad_prediction_link, "prediction request linkage mismatch"),
        (bad_feature_link, "feature prediction linkage mismatch"),
        (bad_settlement_link, "settlement prediction linkage mismatch"),
    ]
    for payload, expected in cases:
        _psql(target, dsn, sql=_rpc_sql(payload), expect_error=expected)
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 1)::text FROM public.prediction_ledger_requests;",
        "true",
        f"{suffix} invalid payloads do not mutate requests",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 0)::text FROM public.prediction_ledger_settlements;",
        "true",
        f"{suffix} invalid payloads do not mutate settlements",
    )
    return len(cases) + 2


def _create_roles(target: RehearsalTarget, created: list[str]) -> None:
    existing = set(
        _psql(
            target,
            target.dsn,
            sql="SELECT rolname FROM pg_catalog.pg_roles WHERE rolname IN "
            "('anon','authenticated','service_role','strathmark_prediction_rpc_owner');",
        ).splitlines()
    )
    for role in _BOOTSTRAP_ROLES:
        if role not in existing:
            _psql(target, target.dsn, sql=f"CREATE ROLE {role} NOLOGIN NOBYPASSRLS;")
            created.append(role)


def _ensure_rpc_owner(repo_root: Path, target: RehearsalTarget, created: list[str]) -> None:
    """Execute the same checked-in RPC-owner prerequisite used by operators."""
    role = "strathmark_prediction_rpc_owner"
    existed = (
        _psql(
            target,
            target.dsn,
            sql="SELECT rolname FROM pg_catalog.pg_roles "
            "WHERE rolname='strathmark_prediction_rpc_owner';",
        ).strip()
        == role
    )
    prerequisite = (
        repo_root / "strathmark" / "migrations" / "prerequisites" / "prediction_rpc_owner.sql"
    )
    _psql(target, target.dsn, sql_file=prerequisite)
    if not existed:
        created.append(role)


def _drop_roles(target: RehearsalTarget, roles: list[str]) -> None:
    errors: list[Exception] = []
    for role in reversed(roles):
        try:
            _psql(target, target.dsn, sql=f"DROP ROLE IF EXISTS {role};")
        except Exception as exc:
            errors.append(exc)
    if errors:
        detail = "; ".join(str(error) for error in errors)
        raise RehearsalExecutionError(f"temporary role cleanup failed: {detail}") from errors[0]


def _run_matrix(repo_root: Path, target: RehearsalTarget, dsn: str) -> int:
    checks = 0
    migration_dir = repo_root / "strathmark" / "migrations"
    migration_005 = migration_dir / "20260811_005_prediction_v2.sql"
    migration_006 = migration_dir / "20260813_006_prediction_hash_algorithm.sql"
    rollback_006 = migration_dir / "20260813_006_prediction_hash_algorithm.down.sql"

    _psql(
        target,
        dsn,
        sql="CREATE TABLE public.competitors (competitor_id pg_catalog.text PRIMARY KEY); "
        "INSERT INTO public.competitors VALUES ('C001'), ('C002');",
    )
    checks += 1

    _psql(target, dsn, sql_file=migration_005)
    checks += 1

    raw = _field_payload("raw", request_hash="a" * 64, hash_algorithm="raw-v1")
    _assert_scalar(
        target, dsn, _rpc_sql(raw), '{"kind": "field", "accepted": true}', "005 raw-v1 RPC"
    )
    checks += 1

    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.to_regclass('public.idx_prediction_ledger_competitor') "
        "IS NOT NULL AND pg_catalog.to_regclass("
        "'public.idx_prediction_ledger_settlement_current') IS NOT NULL)::text;",
        "true",
        "required prediction ledger indexes installed",
    )
    checks += 1

    checks += _assert_invalid_payloads(target, dsn, "005")

    active_before_006 = _field_payload(
        "active-before-006", request_hash="b" * 64, hash_algorithm="active-v2"
    )
    _psql(
        target,
        dsn,
        sql=_rpc_sql(active_before_006),
        expect_error="active-v2 request hashes require migration 006",
    )
    checks += 1

    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_requests;",
        "1",
        "005 rejection is atomic",
    )
    checks += 1

    for role in ("anon", "authenticated"):
        _psql(
            target,
            dsn,
            sql=f"SET ROLE {role}; SELECT * FROM public.prediction_ledger_requests;",
            expect_error="permission denied",
        )
        checks += 1
    _psql(
        target,
        dsn,
        sql="SET ROLE service_role; INSERT INTO public.prediction_ledger_requests "
        "(ledger_request_id,caller_id,request_id,request_hash,event_code,prediction_as_of,created_at) "
        "VALUES ('direct','direct','direct','cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc','SB','2026-08-01',now());",
        expect_error="permission denied",
    )
    checks += 1

    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class WHERE relnamespace = "
        "'public'::pg_catalog.regnamespace AND relname LIKE 'prediction_ledger_%' "
        "AND relrowsecurity AND relforcerowsecurity;",
        "4",
        "all mirror tables force RLS",
    )
    checks += 1
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger WHERE tgrelid IN "
        "(SELECT oid FROM pg_catalog.pg_class WHERE relnamespace = "
        "'public'::pg_catalog.regnamespace AND relname LIKE 'prediction_ledger_%') "
        "AND NOT tgisinternal;",
        "4",
        "immutable triggers installed",
    )
    checks += 1
    _psql(
        target,
        dsn,
        sql="UPDATE public.prediction_ledger_requests SET request_id='mutated' "
        "WHERE ledger_request_id='ledger-raw';",
        expect_error="append-only",
    )
    checks += 1

    _assert_scalar(
        target,
        dsn,
        "SELECT (NOT r.rolcanlogin AND NOT r.rolbypassrls AND "
        "p.proowner=r.oid AND p.prosecdef AND p.proconfig @> ARRAY['search_path='])::text "
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_roles r ON r.oid=p.proowner "
        "WHERE p.oid='public.append_prediction_ledger_v2(pg_catalog.jsonb)'::pg_catalog.regprocedure;",
        "true",
        "RPC owner and empty search path",
    )
    checks += 1
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.has_function_privilege('service_role', "
        "'public.append_prediction_ledger_v2(pg_catalog.jsonb)', 'EXECUTE') AND "
        "NOT pg_catalog.has_function_privilege('anon', "
        "'public.append_prediction_ledger_v2(pg_catalog.jsonb)', 'EXECUTE') AND "
        "NOT pg_catalog.has_function_privilege('authenticated', "
        "'public.append_prediction_ledger_v2(pg_catalog.jsonb)', 'EXECUTE'))::text;",
        "true",
        "minimum RPC grants",
    )
    checks += 1

    _psql(target, dsn, sql_file=migration_006)
    checks += 1
    checks += _assert_invalid_payloads(target, dsn, "006")
    _psql(target, dsn, sql_file=rollback_006)
    checks += 1
    checks += _assert_invalid_payloads(target, dsn, "006-down")
    _assert_scalar(
        target,
        dsn,
        "SELECT (NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid="
        "'public.prediction_ledger_requests'::pg_catalog.regclass AND attname='hash_algorithm' "
        "AND NOT attisdropped))::text;",
        "true",
        "pre-activation rollback",
    )
    checks += 1
    _psql(target, dsn, sql_file=migration_006)
    checks += 1

    active = _field_payload("active", request_hash="d" * 64, hash_algorithm="active-v2")
    _assert_scalar(
        target, dsn, _rpc_sql(active), '{"kind": "field", "accepted": true}', "006 active-v2 RPC"
    )
    checks += 1
    _assert_scalar(
        target, dsn, _rpc_sql(active), '{"kind": "field", "accepted": true}', "exact retry"
    )
    checks += 1
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_predictions "
        "WHERE prediction_id='prediction-active';",
        "1",
        "exact retry remains single-copy",
    )
    checks += 1

    changed = _field_payload("active", request_hash="e" * 64, hash_algorithm="active-v2")
    _psql(target, dsn, sql=_rpc_sql(changed), expect_error="ledger request hash conflict")
    checks += 1

    invalid_field = _field_payload(
        "invalid-fk", request_hash="f" * 64, hash_algorithm="active-v2", competitor_id="C999"
    )
    _psql(
        target,
        dsn,
        sql=_rpc_sql(invalid_field),
        expect_error="foreign key constraint",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_requests "
        "WHERE ledger_request_id='ledger-invalid-fk';",
        "0",
        "field RPC FK failure rolls back request",
    )
    checks += 2

    settlement = {
        "settlement": {
            "settlement_id": "settlement-1",
            "prediction_id": "prediction-active",
            "revision": 1,
            "competitor_id": "C001",
            "event_code": "SB",
            "actual_time": 43.0,
            "residual": 0.5,
            "actor": "migration-rehearsal",
            "reason": "test",
            "payload_hash": "1" * 64,
            "supersedes_settlement_id": None,
            "settled_at": "2026-08-02T00:00:00+00:00",
        }
    }
    _assert_scalar(
        target,
        dsn,
        _rpc_sql(settlement),
        '{"kind": "settlement", "accepted": true}',
        "settlement RPC",
    )
    _assert_scalar(
        target,
        dsn,
        _rpc_sql(settlement),
        '{"kind": "settlement", "accepted": true}',
        "settlement retry",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_settlements;",
        "1",
        "settlement retry remains single-copy",
    )
    checks += 3

    invalid_settlement = json.loads(json.dumps(settlement))
    invalid_settlement["settlement"].update(
        settlement_id="settlement-invalid",
        revision=2,
        competitor_id="C999",
        payload_hash="2" * 64,
        supersedes_settlement_id="settlement-1",
    )
    _psql(
        target,
        dsn,
        sql=_rpc_sql(invalid_settlement),
        expect_error="foreign key constraint",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_settlements;",
        "1",
        "invalid correction rolls back atomically",
    )
    checks += 2

    _psql(target, dsn, sql_file=rollback_006, expect_error="cannot roll back migration 006")
    _assert_scalar(
        target,
        dsn,
        "SELECT (EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid="
        "'public.prediction_ledger_requests'::pg_catalog.regclass AND attname='hash_algorithm' "
        "AND NOT attisdropped))::text;",
        "true",
        "post-activation rollback refusal preserves schema",
    )
    checks += 2

    _psql(target, dsn, sql_file=migration_005)
    _psql(target, dsn, sql_file=migration_006)
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_requests;",
        "2",
        "ordered migration rerun is idempotent",
    )
    checks += 2

    shadow = _field_payload("shadow", request_hash="3" * 64, hash_algorithm="active-v2")
    shadow_sql = (
        "SET ROLE service_role; "
        "CREATE TEMP TABLE prediction_ledger_requests (sentinel pg_catalog.text); "
        f"SELECT public.append_prediction_ledger_v2('{_json_literal(shadow)}'::pg_catalog.jsonb); "
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_requests "
        "WHERE ledger_request_id='ledger-shadow'; RESET ROLE;"
    )
    _assert_scalar(target, dsn, shadow_sql, "1", "temporary object shadowing is harmless")
    checks += 1

    return checks


def run_prediction_v2_rehearsal(repo_root: Path, controller_dsn: str) -> RehearsalReport:
    """Create, exercise, and destroy one isolated database on a loopback cluster."""
    target = validate_rehearsal_dsn(controller_dsn)
    database = f"strathmark_rehearsal_{uuid.uuid4().hex}"
    created_roles: list[str] = []
    database_created = False
    database_dropped = False
    checks = 0
    primary_error: Exception | None = None
    try:
        _create_roles(target, created_roles)
        _ensure_rpc_owner(repo_root, target, created_roles)
        _psql(target, target.dsn, sql=f"CREATE DATABASE {database};")
        database_created = True
        checks = _run_matrix(repo_root, target, target.database_dsn(database))
    except Exception as exc:
        primary_error = exc
    finally:
        cleanup_errors: list[Exception] = []
        if database_created:
            try:
                _psql(target, target.dsn, sql=f"DROP DATABASE {database} WITH (FORCE);")
                database_dropped = True
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            _drop_roles(target, created_roles)
        except Exception as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            detail = "; ".join(str(error) for error in cleanup_errors)
            cleanup_failure = RehearsalExecutionError(
                f"disposable rehearsal cleanup failed: {detail}"
            )
            if primary_error is not None:
                raise cleanup_failure from primary_error
            raise cleanup_failure from cleanup_errors[0]
    if primary_error is not None:
        raise primary_error
    return RehearsalReport(database, checks, database_dropped)
