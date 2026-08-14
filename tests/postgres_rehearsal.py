"""Disposable, loopback-only PostgreSQL migration rehearsal support.

This module deliberately lives under ``tests``.  It is release verification
infrastructure, not a general-purpose migration runner and never accepts a hosted
database target.
"""

from __future__ import annotations

import hashlib
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


def _shadow_rpc_sql(payload: object, role: str = "service_role") -> str:
    return (
        f"SET ROLE {role}; SELECT public.append_shadow_mirror_v1"
        f"('{_json_literal(payload)}'::pg_catalog.jsonb); RESET ROLE;"
    )


def _shadow_payload_hash(payload: Mapping[str, object]) -> str:
    semantic = dict(payload)
    semantic.pop("delivery", None)
    encoded = json.dumps(
        semantic,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _shadow_receipt_payload(
    suffix: str,
    *,
    competitor_id: str = "missoula:competitor:1",
    outbox_id: str | None = None,
    payload_hash: str | None = None,
) -> dict[str, object]:
    field = _field_payload(
        f"shadow-{suffix}",
        request_hash="3" * 64,
        hash_algorithm="active-v2",
        competitor_id=competitor_id,
    )
    request = field["request"]
    prediction = field["predictions"][0]
    request.update(
        caller_id="missoula:service:shadow",
        request_id=f"missoula:request:{suffix}",
    )
    core = {
        "schema_version": "strathmark.shadow-receipt-core.v1",
        "identity_schema_version": "strathmark.namespaced-identity.v1",
        "consumer_id": request["caller_id"],
        "request_id": request["request_id"],
        "run_revision": f"missoula:run-revision:{suffix}",
        "observation": {
            "schema_version": "strathmark.shadow-observation-fingerprint.v1",
            "fingerprint": "2" * 64,
        },
        "created_at": "2026-08-03T00:00:00+00:00",
        "predictions": [
            {
                "prediction_id": prediction["prediction_id"],
                "competitor_id": competitor_id,
                "event_code": "SB",
            }
        ],
    }
    payload = {
        "schema_version": "strathmark.shadow-mirror-envelope.v1",
        "kind": "shadow_receipt",
        "delivery": {
            "schema_version": "strathmark.mirror-delivery.v1",
            "outbox_id": outbox_id or f"outbox-receipt-{suffix}",
            "entity_id": request["ledger_request_id"],
            "created_at": "2026-08-03T00:00:00+00:00",
            "payload_hash": payload_hash or "0" * 64,
        },
        "ledger": field,
        "receipt": {
            "schema_version": "strathmark.shadow-receipt-mirror.v1",
            "ledger_request_id": request["ledger_request_id"],
            "caller_id": request["caller_id"],
            "request_id": request["request_id"],
            "core_schema_version": "strathmark.shadow-receipt-core.v1",
            "identity_schema_version": "strathmark.namespaced-identity.v1",
            "observation_schema_version": "strathmark.shadow-observation-fingerprint.v1",
            "observation_fingerprint": "2" * 64,
            "core": core,
        },
    }
    if payload_hash is None:
        payload["delivery"]["payload_hash"] = _shadow_payload_hash(payload)
    return payload


def _numeric_shadow_payload(
    *,
    action: str,
    revision: int,
    suffix: str,
    supersedes_revision_id: str | None,
    payload_hash: str | None = None,
) -> dict[str, object]:
    settle = action == "settle"
    field_revision_id = f"field-revision-{suffix}"
    payload = {
        "schema_version": "strathmark.shadow-mirror-envelope.v1",
        "kind": "numeric_outcome_revision",
        "delivery": {
            "schema_version": "strathmark.mirror-delivery.v1",
            "outbox_id": f"outbox-numeric-{suffix}",
            "entity_id": field_revision_id,
            "created_at": f"2026-08-0{3 + revision}T00:00:00+00:00",
            "payload_hash": payload_hash or "0" * 64,
        },
        "numeric_outcome_revision": {
            "schema_version": "strathmark.shadow-numeric-outcome-mirror.v1",
            "field_revision_id": field_revision_id,
            "outcome_revision_id": f"missoula:outcome-revision:{suffix}",
            "ledger_request_id": "ledger-shadow-receipt",
            "caller_id": "missoula:service:shadow",
            "actor": "missoula:operator:judge-1",
            "reason_code": (None if revision == 1 else "retract_invalid_numeric_evidence"),
            "created_at": f"2026-08-0{3 + revision}T00:00:00+00:00",
            "revisions": [
                {
                    "revision_id": f"numeric-revision-{suffix}",
                    "prediction_id": "prediction-shadow-receipt",
                    "revision": revision,
                    "competitor_id": "missoula:competitor:1",
                    "event_code": "SB",
                    "action": action,
                    "actual_time": 43.0 if settle else None,
                    "residual": 0.5 if settle else None,
                    "supersedes_revision_id": supersedes_revision_id,
                }
            ],
        },
    }
    if payload_hash is None:
        payload["delivery"]["payload_hash"] = _shadow_payload_hash(payload)
    return payload


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
    migration_007 = migration_dir / "20260813_007_shadow_mirror_contract.sql"
    rollback_007 = migration_dir / "20260813_007_shadow_mirror_contract.down.sql"

    _psql(
        target,
        dsn,
        sql="CREATE TABLE public.competitors (competitor_id pg_catalog.text PRIMARY KEY); "
        "INSERT INTO public.competitors VALUES "
        "('C001'), ('C002'), ('missoula:competitor:1'), ('missoula:competitor:2');",
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

    legacy_requests_before_007 = _psql(
        target,
        dsn,
        sql="SELECT pg_catalog.count(*) FROM public.prediction_ledger_requests;",
    ).strip()
    _psql(target, dsn, sql_file=migration_007)
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 0)::text FROM public.shadow_mirror_deliveries;",
        "true",
        "007 upgrade starts additive without rewriting legacy rows",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = " + legacy_requests_before_007 + ")::text "
        "FROM public.prediction_ledger_requests;",
        "true",
        "007 preserves 005/006 requests",
    )
    checks += 3

    _psql(target, dsn, sql_file=rollback_007)
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.to_regclass('public.shadow_receipt_cores') IS NULL)::text;",
        "true",
        "007 pre-activation rollback",
    )
    _psql(target, dsn, sql_file=migration_007)
    checks += 3

    receipt = _shadow_receipt_payload("receipt")
    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(receipt),
        '{"kind": "shadow_receipt", "accepted": true}',
        "007 complete shadow receipt",
    )
    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(receipt),
        '{"kind": "shadow_receipt", "accepted": true, "duplicate": true}',
        "007 exact receipt retry",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 1)::text FROM public.shadow_receipt_cores;",
        "true",
        "007 receipt retry remains single-copy",
    )
    checks += 3

    receipt_conflict = json.loads(json.dumps(receipt))
    receipt_conflict["delivery"]["payload_hash"] = "5" * 64
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(receipt_conflict),
        expect_error="shadow mirror outbox conflict",
    )
    checks += 1

    receipt_semantic_conflict = json.loads(json.dumps(receipt))
    receipt_semantic_conflict["receipt"]["core"]["run_revision"] = "missoula:run-revision:changed"
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(receipt_semantic_conflict),
        expect_error="shadow mirror duplicate semantic conflict",
    )
    checks += 1  # same claimed hash with changed receipt semantics conflicts

    nested_ledger_conflict = json.loads(json.dumps(receipt))
    nested_ledger_conflict["ledger"]["predictions"][0]["source"] = "changed-source"
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(nested_ledger_conflict),
        expect_error="shadow mirror duplicate semantic conflict",
    )
    nested_ledger_extra = json.loads(json.dumps(receipt))
    nested_ledger_extra["ledger"]["features"][0]["unknown"] = "not-allowed"
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(nested_ledger_extra),
        expect_error="shadow receipt ledger feature contract is invalid",
    )
    checks += 2  # same claimed hash with changed nested ledger semantics conflicts

    forbidden_shadow_keys = (
        "name",
        "display_name",
        "fatigue",
        "fatigue_notes",
        "medical",
        "medical_notes",
        "weather",
        "equipment",
        "outcome_history",
        "context_history",
        "penalty",
        "dnf",
        "dq",
        "notes",
        "secret",
    )
    for forbidden_key in forbidden_shadow_keys:
        privacy_probe = json.loads(json.dumps(receipt))
        privacy_probe["receipt"]["core"]["privacy_probe"] = {
            "nested": [{forbidden_key.upper(): "must-not-cross"}]
        }
        _psql(
            target,
            dsn,
            sql=_shadow_rpc_sql(privacy_probe),
            expect_error="shadow mirror contains prohibited operational or free-text data",
        )
    checks += len(
        forbidden_shadow_keys
    )  # direct RPC rejects every recursively prohibited privacy key

    invalid_receipt = _shadow_receipt_payload(
        "invalid-fk",
        competitor_id="missoula:competitor:999",
        payload_hash="6" * 64,
    )
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(invalid_receipt),
        expect_error="foreign key constraint",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 0)::text FROM public.prediction_ledger_requests "
        "WHERE ledger_request_id='ledger-shadow-invalid-fk';",
        "true",
        "007 receipt FK failure rolls back embedded ledger",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 1)::text FROM public.shadow_mirror_deliveries;",
        "true",
        "007 receipt FK failure rolls back delivery metadata",
    )
    checks += 3

    for role in ("anon", "authenticated"):
        _psql(
            target,
            dsn,
            sql=_shadow_rpc_sql(receipt, role=role),
            expect_error="permission denied",
        )
        checks += 1
    _psql(
        target,
        dsn,
        sql="SET ROLE service_role; DELETE FROM public.shadow_receipt_cores; RESET ROLE;",
        expect_error="permission denied",
    )
    checks += 1
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM pg_catalog.pg_class WHERE relnamespace = "
        "'public'::pg_catalog.regnamespace AND relname IN "
        "('shadow_mirror_deliveries','shadow_receipt_cores',"
        "'shadow_numeric_outcome_revisions','shadow_numeric_settlement_revisions') "
        "AND relrowsecurity AND relforcerowsecurity;",
        "4",
        "all 007 mirror tables force RLS",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM pg_catalog.pg_trigger WHERE tgrelid IN "
        "('public.shadow_mirror_deliveries'::pg_catalog.regclass,"
        "'public.shadow_receipt_cores'::pg_catalog.regclass,"
        "'public.shadow_numeric_outcome_revisions'::pg_catalog.regclass,"
        "'public.shadow_numeric_settlement_revisions'::pg_catalog.regclass) "
        "AND NOT tgisinternal;",
        "4",
        "all 007 mirror tables are append-only",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (NOT r.rolcanlogin AND NOT r.rolbypassrls AND p.proowner=r.oid "
        "AND p.prosecdef AND p.proconfig @> ARRAY['search_path='] AND "
        "pg_catalog.has_function_privilege('service_role', "
        "'public.append_shadow_mirror_v1(pg_catalog.jsonb)', 'EXECUTE') AND "
        "NOT pg_catalog.has_function_privilege('anon', "
        "'public.append_shadow_mirror_v1(pg_catalog.jsonb)', 'EXECUTE') AND "
        "NOT pg_catalog.has_function_privilege('authenticated', "
        "'public.append_shadow_mirror_v1(pg_catalog.jsonb)', 'EXECUTE'))::text "
        "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_roles r ON r.oid=p.proowner "
        "WHERE p.oid='public.append_shadow_mirror_v1(pg_catalog.jsonb)'"
        "::pg_catalog.regprocedure;",
        "true",
        "007 RPC owner, empty search path, and minimum grants",
    )
    checks += 3

    settle = _numeric_shadow_payload(
        action="settle",
        revision=1,
        suffix="settle",
        supersedes_revision_id=None,
    )
    void = _numeric_shadow_payload(
        action="void",
        revision=2,
        suffix="void",
        supersedes_revision_id="numeric-revision-settle",
    )
    fractional_revision = _numeric_shadow_payload(
        action="settle",
        revision=1,
        suffix="fractional",
        supersedes_revision_id=None,
    )
    fractional_revision["numeric_outcome_revision"]["revisions"][0]["revision"] = 1.5
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(fractional_revision),
        expect_error="numeric revision must be an exact bounded integer",
    )
    checks += 1  # fractional numeric revision is rejected before integer cast
    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(settle),
        '{"kind": "numeric_outcome_revision", "accepted": true}',
        "007 numeric settlement",
    )
    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(settle),
        '{"kind": "numeric_outcome_revision", "accepted": true, "duplicate": true}',
        "007 exact numeric retry",
    )
    numeric_semantic_conflict = json.loads(json.dumps(settle))
    numeric_semantic_conflict["numeric_outcome_revision"]["actor"] = "missoula:operator:judge-2"
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(numeric_semantic_conflict),
        expect_error="shadow mirror duplicate semantic conflict",
    )
    checks += 2  # same claimed hash with changed numeric semantics conflicts

    missing_reason = _numeric_shadow_payload(
        action="settle",
        revision=2,
        suffix="missing-reason",
        supersedes_revision_id="numeric-revision-settle",
    )
    missing_reason["numeric_outcome_revision"]["reason_code"] = None
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(missing_reason),
        expect_error="numeric correction or void requires a reason_code",
    )
    checks += 1  # noninitial numeric revision requires a reason

    wrong_supersedes = _numeric_shadow_payload(
        action="settle",
        revision=2,
        suffix="wrong-supersedes",
        supersedes_revision_id="numeric-revision-not-latest",
    )
    wrong_supersedes["numeric_outcome_revision"]["reason_code"] = "corrected_time"
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(wrong_supersedes),
        expect_error="numeric settlement must supersede the exact latest authoritative revision",
    )
    checks += 1  # numeric supersession must target exact latest revision

    bad_residual = _numeric_shadow_payload(
        action="settle",
        revision=2,
        suffix="bad-residual",
        supersedes_revision_id="numeric-revision-settle",
    )
    bad_residual["numeric_outcome_revision"]["reason_code"] = "corrected_time"
    bad_residual["numeric_outcome_revision"]["revisions"][0]["actual_time"] = 44.0
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(bad_residual),
        expect_error="numeric residual does not match mirrored prediction",
    )
    checks += 1  # numeric residual must match mirrored median

    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(void),
        '{"kind": "numeric_outcome_revision", "accepted": true}',
        "007 numeric void",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 2)::text FROM public.shadow_numeric_settlement_revisions;",
        "true",
        "007 settle and void remain append-only",
    )
    checks += 3

    invalid_numeric = _numeric_shadow_payload(
        action="settle",
        revision=1,
        suffix="invalid-fk",
        supersedes_revision_id=None,
    )
    invalid_numeric["numeric_outcome_revision"]["revisions"][0]["prediction_id"] = (
        "prediction-does-not-exist"
    )
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(invalid_numeric),
        expect_error="numeric settlement revision linkage or value is invalid",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 3)::text FROM public.shadow_mirror_deliveries;",
        "true",
        "007 invalid numeric revision rolls back delivery and header",
    )
    checks += 2

    shadowed_receipt = _shadow_receipt_payload("object-shadow", payload_hash="a" * 64)
    shadowed_sql = (
        "SET ROLE service_role; "
        "CREATE TEMP TABLE shadow_receipt_cores (sentinel pg_catalog.text); "
        "CREATE TEMP TABLE shadow_mirror_deliveries (sentinel pg_catalog.text); "
        f"SELECT public.append_shadow_mirror_v1("
        f"'{_json_literal(shadowed_receipt)}'::pg_catalog.jsonb); "
        "SELECT pg_catalog.count(*) FROM public.shadow_receipt_cores "
        "WHERE ledger_request_id='ledger-shadow-object-shadow'; RESET ROLE;"
    )
    _assert_scalar(target, dsn, shadowed_sql, "1", "007 object shadowing is harmless")
    checks += 1

    _psql(
        target,
        dsn,
        sql_file=rollback_007,
        expect_error="cannot roll back migration 007 while active shadow evidence exists",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.to_regclass('public.shadow_receipt_cores') IS NOT NULL)::text;",
        "true",
        "007 post-activation rollback refusal preserves schema",
    )
    _psql(target, dsn, sql_file=migration_007)
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 2)::text FROM public.shadow_receipt_cores;",
        "true",
        "007 rerun preserves active immutable receipt rows",
    )
    checks += 4

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
