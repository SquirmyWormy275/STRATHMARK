"""Disposable, loopback-only PostgreSQL migration rehearsal support.

This module deliberately lives under ``tests``.  It is release verification
infrastructure, not a general-purpose migration runner and never accepts a hosted
database target.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

from strathmark.ledger import LedgerPrediction, PredictionLedger
from strathmark.provenance import ENGINE_VERSION

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


def _start_psql(target: RehearsalTarget, dsn: str, sql: str) -> subprocess.Popen[str]:
    """Start one bounded, isolated psql session for concurrency rehearsals."""

    command = [
        "psql",
        "--no-psqlrc",
        "--set=ON_ERROR_STOP=1",
        "--quiet",
        "--tuples-only",
        "--no-align",
        f"--dbname={dsn}",
        "--command",
        sql,
    ]
    try:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=scrubbed_rehearsal_environment(),
        )
    except FileNotFoundError as exc:
        raise RehearsalExecutionError("psql is required for the migration rehearsal") from exc


def _finish_psql(
    target: RehearsalTarget,
    process: subprocess.Popen[str],
    description: str,
) -> str:
    """Collect one concurrency session, killing it if its bounded wait expires."""

    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        detail = _redact(f"{stdout}\n{stderr}".strip(), target)
        raise RehearsalExecutionError(f"{description} timed out: {detail}") from exc
    output = f"{stdout}\n{stderr}".strip()
    if process.returncode:
        raise RehearsalExecutionError(f"{description}: {_redact(output, target)}")
    return stdout.strip()


def _wait_for_advisory_lock(
    target: RehearsalTarget,
    dsn: str,
    process: subprocess.Popen[str],
    description: str,
) -> None:
    """Wait until the background RPC holds its transaction-scoped authority lock."""

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _finish_psql(target, process, description)
            raise RehearsalExecutionError(f"{description} exited before holding its lock")
        locked = _psql(
            target,
            dsn,
            sql=(
                "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks "
                "WHERE locktype='advisory' AND granted)::text;"
            ),
        )
        if locked.strip() == "true":
            return
        time.sleep(0.05)
    process.kill()
    process.communicate()
    raise RehearsalExecutionError(f"{description} never acquired its advisory lock")


def _wait_for_relation_lock(
    target: RehearsalTarget,
    dsn: str,
    process: subprocess.Popen[str],
    relation: str,
    description: str,
) -> None:
    """Wait until a background append holds a write lock on one public table."""

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", relation):
        raise RehearsalExecutionError("unsafe rehearsal relation name")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _finish_psql(target, process, description)
            raise RehearsalExecutionError(f"{description} exited before holding its lock")
        locked = _psql(
            target,
            dsn,
            sql=(
                "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks AS held_lock "
                "JOIN pg_catalog.pg_class AS relation ON relation.oid=held_lock.relation "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid=relation.relnamespace "
                "WHERE namespace.nspname='public' "
                f"AND relation.relname='{relation}' AND held_lock.granted "
                "AND held_lock.mode='RowExclusiveLock')::text;"
            ),
        )
        if locked.strip() == "true":
            return
        time.sleep(0.05)
    process.kill()
    process.communicate()
    raise RehearsalExecutionError(f"{description} never acquired its relation lock")


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


def _shadow_payload_body(payload: Mapping[str, object]) -> dict[str, object]:
    semantic = dict(payload)
    semantic.pop("delivery", None)
    return semantic


def _bind_shadow_delivery(
    payload: dict[str, object],
    *,
    canonical_payload: str | None = None,
    payload_hash: str | None = None,
) -> None:
    if canonical_payload is None:
        canonical_payload = json.dumps(
            _shadow_payload_body(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    payload["delivery"]["canonical_payload"] = canonical_payload
    payload["delivery"]["payload_hash"] = (
        payload_hash or hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    )


def _sync_shadow_active_evidence(payload: dict[str, object]) -> None:
    """Keep a receipt fixture's active evidence projection and hash coherent."""

    core = payload["receipt"]["core"]
    evidence_snapshot = core["evidence_snapshot"]
    active_input = core["active_input"]
    calculation_only_fields = {
        "activated_at",
        "age_days_at_calculation",
        "freshness_at_calculation",
        "integrity",
        "ready_for_offline_at_calculation",
    }
    active_input["evidence_snapshot"] = {
        key: value for key, value in evidence_snapshot.items() if key not in calculation_only_fields
    }
    fingerprint_input = dict(active_input)
    fingerprint_input.pop("fingerprint", None)
    active_input["fingerprint"] = hashlib.sha256(
        json.dumps(
            fingerprint_input,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _shadow_receipt_payload(
    suffix: str,
    *,
    competitor_id: str = "missoula:competitor:1",
    competitor_ids: tuple[str, ...] | None = None,
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
        created_at="2026-08-03T00:00:00+00:00",
    )
    ids = competitor_ids or (competitor_id,)
    prediction_rows = []
    feature_rows = []
    for ordinal, current_competitor_id in enumerate(ids):
        current_prediction = json.loads(json.dumps(prediction))
        prediction_id = (
            prediction["prediction_id"]
            if ordinal == 0
            else f"prediction-shadow-{suffix}-{ordinal + 1}"
        )
        current_prediction.update(
            prediction_id=prediction_id,
            competitor_id=current_competitor_id,
            optimizer="rehearsal",
            optimizer_metadata={
                "optimizer": "rehearsal",
                "simulations": 0,
                "seed": 20260811,
                "passes": 0,
                "reason": "rehearsal",
            },
        )
        prediction_rows.append(current_prediction)
        current_feature = json.loads(json.dumps(field["features"][0]))
        current_feature.update(
            feature_snapshot_id=f"feature-shadow-{suffix}-{ordinal + 1}",
            prediction_id=prediction_id,
        )
        feature_rows.append(current_feature)
    field["predictions"] = prediction_rows
    field["features"] = feature_rows

    request_projection_core = {
        "schema_version": "strathmark.shadow-request-projection.v1",
        "consumer_id": request["caller_id"],
        "tournament_id": "missoula:tournament:2027",
        "event_occurrence_id": "missoula:event:sb",
        "field_run_id": f"missoula:field-run:{suffix}",
        "operator_id": "missoula:operator:judge-1",
        "request_id": request["request_id"],
        "run_revision": f"missoula:run-revision:{suffix}",
        "event_code": "SB",
        "target_contract": "single-elapsed-seconds.v1",
        "prediction_as_of": "2026-08-01",
        "cutoff_semantics": "exclusive-utc-date",
        "schedule_fingerprint": "1" * 64,
        "observation_schema_version": "strathmark.shadow-observation-fingerprint.v1",
        "observation_fingerprint": "2" * 64,
        "seed": 20260811,
        "competitors": [
            {"competitor_id": current_competitor_id, "gender": "UNKNOWN"}
            for current_competitor_id in ids
        ],
        "wood": {"species": "PINE", "diameter_mm": 300.0, "quality": 5},
    }
    request_projection = {
        **request_projection_core,
        "fingerprint": hashlib.sha256(
            json.dumps(
                request_projection_core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    calculation_input = {
        "event_code": "SB",
        "prediction_as_of": "2026-08-01",
        "diameter_mm": 300.0,
        "species": "PINE",
        "wood_properties": {
            "janka_hardness": 1690.0,
            "specific_gravity": 0.34,
            "crush_strength": 4000.0,
            "shear_strength": 1000.0,
            "modulus_of_rupture": 8000.0,
            "modulus_of_elasticity": 1000000.0,
            "species_missing": False,
        },
        "seed": 20260811,
        "engine": "v2",
        "effective_mark_ceiling": 180,
        "competitors": [
            {
                "competitor_id": current_competitor_id,
                "gender": "__MISSING__",
                "manual_time_override": None,
                "history": [],
            }
            for current_competitor_id in ids
        ],
    }
    evidence_snapshot = {
        "schema_version": "strathmark.evidence-snapshot.v1",
        "snapshot_digest": "7" * 64,
        "source_schema_version": "missoula.results.v1",
        "source_id": "missoula:evidence:rehearsal",
        "source_digest": "8" * 64,
        "cutoff": "2026-08-01",
        "cutoff_semantics": "exclusive-utc-date",
        "captured_at": "2026-08-02T00:00:00+00:00",
        "activation_id": f"activation-{suffix}",
        "activation_revision": 1,
        "previous_activation_id": None,
        "supersedes_snapshot_digest": None,
        "completeness": "complete",
        "supplied_row_count": 0,
        "accepted_row_count": 0,
        "rejected_row_count": 0,
        "diagnostics": {},
        "activated_at": "2026-08-02T00:00:00+00:00",
        "age_days_at_calculation": 0,
        "freshness_at_calculation": "current",
        "integrity": "verified",
        "ready_for_offline_at_calculation": True,
    }
    active_input_core = {
        "schema_version": "strathmark.shadow-active-input.v1",
        "tournament_id": request_projection["tournament_id"],
        "event_occurrence_id": request_projection["event_occurrence_id"],
        "field_run_id": request_projection["field_run_id"],
        "target_contract": request_projection["target_contract"],
        "schedule_fingerprint": request_projection["schedule_fingerprint"],
        "caller_input": calculation_input,
        "evidence_snapshot": {
            key: value
            for key, value in evidence_snapshot.items()
            if key
            not in {
                "activated_at",
                "age_days_at_calculation",
                "freshness_at_calculation",
                "integrity",
                "ready_for_offline_at_calculation",
            }
        },
    }
    active_input = {
        **active_input_core,
        "fingerprint": hashlib.sha256(
            json.dumps(
                active_input_core,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    core = {
        "schema_version": "strathmark.shadow-receipt-core.v1",
        "identity_schema_version": "strathmark.namespaced-identity.v1",
        "consumer_id": request["caller_id"],
        "tournament_id": request_projection["tournament_id"],
        "event_occurrence_id": request_projection["event_occurrence_id"],
        "field_run_id": request_projection["field_run_id"],
        "operator_id": request_projection["operator_id"],
        "request_id": request["request_id"],
        "run_revision": f"missoula:run-revision:{suffix}",
        "event_code": "SB",
        "target_contract": "single-elapsed-seconds.v1",
        "prediction_as_of": "2026-08-01",
        "cutoff_semantics": "exclusive-utc-date",
        "request_projection": request_projection,
        "active_input": active_input,
        "calculation_input": calculation_input,
        "observation": {
            "schema_version": "strathmark.shadow-observation-fingerprint.v1",
            "fingerprint": "2" * 64,
        },
        "evidence_snapshot": evidence_snapshot,
        "artifact": {
            "provider_source": "rehearsal",
            "source_digest": "9" * 64,
            "artifact_digest": "a" * 64,
            "model_version": "rehearsal",
            "calibration_version": "rehearsal",
            "residual_version": None,
        },
        "evidence_diagnostics": [
            {
                "ordinal": ordinal,
                "competitor_id": current_competitor_id,
                "total_rows": 0,
                "included_rows": 0,
                "excluded_rows": 0,
                "excluded_by_reason": {},
                "canonicalization_version": "rehearsal-v1",
            }
            for ordinal, current_competitor_id in enumerate(ids)
        ],
        "ledger": {
            "request_hash": request["request_hash"],
            "hash_algorithm": request["hash_algorithm"],
        },
        "created_at": "2026-08-03T00:00:00+00:00",
        "predictions": [
            {
                "ordinal": ordinal,
                "prediction_id": current_prediction["prediction_id"],
                "competitor_id": current_prediction["competitor_id"],
                "event_code": current_prediction["event_code"],
                "median_seconds": current_prediction["median_seconds"],
                "assigned_mark": current_prediction["assigned_mark"],
                "source": current_prediction["source"],
                "training_eligible": current_prediction["training_eligible"],
                "versions": {
                    "engine": current_prediction["engine_version"],
                    "model": current_prediction["model_version"],
                    "calibration": current_prediction["calibration_version"],
                },
                "evidence_cutoff": current_prediction["evidence_cutoff"],
                "interval": {
                    "lower": current_prediction["interval_lower"],
                    "upper": current_prediction["interval_upper"],
                    "nominal_coverage": current_prediction["interval_coverage"],
                    "calibration_state": current_prediction["interval_state"],
                    "scope": current_prediction["interval_scope"],
                },
                "optimizer": current_prediction["optimizer"],
                "optimizer_metadata": current_prediction["optimizer_metadata"],
                "warnings": current_prediction["warnings"],
                "ignored_factors": current_prediction["ignored_factors"],
            }
            for ordinal, current_prediction in enumerate(prediction_rows)
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
            "canonical_payload": "",
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
    _bind_shadow_delivery(payload, payload_hash=payload_hash)
    return payload


def _prediction_ledger_shadow_payload(path: Path) -> dict[str, object]:
    """Build the rehearsal receipt through the real durable ledger/outbox path."""

    template = _shadow_receipt_payload("prediction-ledger")
    template_core = template["receipt"]["core"]
    receipt_metadata = {
        key: value
        for key, value in template_core.items()
        if key not in {"ledger", "created_at", "predictions"}
    }
    optimizer_metadata = {
        "optimizer": "rehearsal",
        "simulations": 0,
        "seed": 20260811,
        "passes": 0,
        "reason": "rehearsal",
    }
    prediction = LedgerPrediction(
        competitor_id="missoula:competitor:1",
        event_code="SB",
        median_seconds=42.5,
        assigned_mark=3,
        source="baseline",
        engine_version=ENGINE_VERSION,
        model_version="rehearsal",
        calibration_version="rehearsal",
        evidence_cutoff=date(2026, 8, 1),
        interval_lower=40.0,
        interval_upper=45.0,
        interval_coverage=0.9,
        interval_state="calibrated",
        interval_scope="competitor",
        optimizer="rehearsal",
        optimizer_metadata=optimizer_metadata,
        feature_snapshot={"history_count": 2.0},
        training_eligible=True,
    )
    captured: list[dict[str, object]] = []
    ledger = PredictionLedger(path)
    ledger.record_field(
        "missoula:service:shadow",
        "missoula:request:prediction-ledger",
        template_core["calculation_input"],
        [prediction],
        receipt_metadata=receipt_metadata,
    )
    ledger._mirror = captured.append
    summary = ledger.flush_mirror_outbox(limit=1)
    if summary.get("recorded") != 1 or len(captured) != 1:
        raise RehearsalExecutionError("PredictionLedger did not emit exactly one receipt envelope")
    envelope = captured[0]
    del ledger
    gc.collect()
    return envelope


def _numeric_shadow_payload(
    *,
    action: str,
    revision: int,
    suffix: str,
    supersedes_revision_id: str | None,
    payload_hash: str | None = None,
    ledger_request_id: str = "ledger-shadow-receipt",
    prediction_id: str = "prediction-shadow-receipt",
    competitor_id: str = "missoula:competitor:1",
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
            "canonical_payload": "",
            "payload_hash": payload_hash or "0" * 64,
        },
        "numeric_outcome_revision": {
            "schema_version": "strathmark.shadow-numeric-outcome-mirror.v1",
            "field_revision_id": field_revision_id,
            "outcome_revision_id": f"missoula:outcome-revision:{suffix}",
            "ledger_request_id": ledger_request_id,
            "caller_id": "missoula:service:shadow",
            "actor": "missoula:operator:judge-1",
            "reason_code": (None if revision == 1 else "retract_invalid_numeric_evidence"),
            "created_at": f"2026-08-0{3 + revision}T00:00:00+00:00",
            "revisions": [
                {
                    "revision_id": f"numeric-revision-{suffix}",
                    "prediction_id": prediction_id,
                    "revision": revision,
                    "competitor_id": competitor_id,
                    "event_code": "SB",
                    "action": action,
                    "actual_time": 43.0 if settle else None,
                    "residual": 0.5 if settle else None,
                    "supersedes_revision_id": supersedes_revision_id,
                }
            ],
        },
    }
    _bind_shadow_delivery(payload, payload_hash=payload_hash)
    return payload


def _assert_invalid_payloads(target: RehearsalTarget, dsn: str, suffix: str) -> int:
    """Exercise explicit shape/linkage rejection against the installed RPC version."""
    request_count = _psql(
        target,
        dsn,
        sql="SELECT pg_catalog.count(*) FROM public.prediction_ledger_requests;",
    ).strip()
    settlement_count = _psql(
        target,
        dsn,
        sql="SELECT pg_catalog.count(*) FROM public.prediction_ledger_settlements;",
    ).strip()
    field = _field_payload(f"invalid-{suffix}", request_hash="9" * 64, hash_algorithm="raw-v1")
    ambiguous = json.loads(json.dumps(field))
    ambiguous["settlement"] = None
    field_outer_extra = json.loads(json.dumps(field))
    field_outer_extra["unknown"] = "not-allowed"
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
            "reason": "invalid-link-probe",
            "payload_hash": "8" * 64,
            "supersedes_settlement_id": None,
            "settled_at": "2026-08-02T00:00:00+00:00",
        }
    }
    request_extra = json.loads(json.dumps(field))
    request_extra["request"]["unknown"] = "not-allowed"
    request_missing = json.loads(json.dumps(field))
    request_missing["request"].pop("request_hash")
    request_wrong_type = json.loads(json.dumps(field))
    request_wrong_type["request"]["event_code"] = False
    prediction_extra = json.loads(json.dumps(field))
    prediction_extra["predictions"][0]["unknown"] = "not-allowed"
    prediction_missing = json.loads(json.dumps(field))
    prediction_missing["predictions"][0].pop("assigned_mark")
    prediction_wrong_type = json.loads(json.dumps(field))
    prediction_wrong_type["predictions"][0]["training_eligible"] = 1
    feature_extra = json.loads(json.dumps(field))
    feature_extra["features"][0]["unknown"] = "not-allowed"
    feature_missing = json.loads(json.dumps(field))
    feature_missing["features"][0].pop("numeric_value")
    feature_wrong_type = json.loads(json.dumps(field))
    feature_wrong_type["features"][0]["numeric_value"] = "2.0"
    settlement_extra = json.loads(json.dumps(bad_settlement_link))
    settlement_extra["settlement"]["unknown"] = "not-allowed"
    settlement_missing = json.loads(json.dumps(bad_settlement_link))
    settlement_missing["settlement"].pop("payload_hash")
    settlement_wrong_type = json.loads(json.dumps(bad_settlement_link))
    settlement_wrong_type["settlement"]["revision"] = 1.5
    settlement_outer_extra = json.loads(json.dumps(bad_settlement_link))
    settlement_outer_extra["unknown"] = "not-allowed"
    cases: list[tuple[object, str]] = [
        ([], "ledger payload must be an object"),
        ({"settlement": None}, "settlement payload must be a non-null object"),
        (ambiguous, "ledger payload must contain exactly one operation kind"),
        (field_outer_extra, "ledger field payload has unknown or missing properties"),
        (settlement_outer_extra, "ledger payload must contain exactly one operation kind"),
        ({}, "field request must be a non-null object"),
        ({"request": {}, "predictions": {}, "features": []}, "non-empty array"),
        ({"request": {}, "predictions": [], "features": []}, "non-empty array"),
        ({"request": {}, "predictions": [{}]}, "field features must be an array"),
        (bad_prediction_link, "prediction request linkage mismatch"),
        (bad_feature_link, "feature prediction linkage mismatch"),
        (bad_settlement_link, "settlement prediction linkage mismatch"),
        (request_extra, "ledger request has unknown or missing properties"),
        (request_missing, "ledger request has unknown or missing properties"),
        (request_wrong_type, "ledger request JSON types are invalid"),
        (prediction_extra, "ledger prediction has unknown or missing properties"),
        (prediction_missing, "ledger prediction has unknown or missing properties"),
        (prediction_wrong_type, "ledger prediction JSON types are invalid"),
        (feature_extra, "ledger feature has unknown or missing properties"),
        (feature_missing, "ledger feature has unknown or missing properties"),
        (feature_wrong_type, "ledger feature JSON types are invalid"),
        (settlement_extra, "ledger settlement has unknown or missing properties"),
        (settlement_missing, "ledger settlement has unknown or missing properties"),
        (settlement_wrong_type, "ledger settlement JSON types are invalid"),
    ]
    for payload, expected in cases:
        _psql(target, dsn, sql=_rpc_sql(payload), expect_error=expected)
    _assert_scalar(
        target,
        dsn,
        f"SELECT (pg_catalog.count(*) = {request_count})::text "
        "FROM public.prediction_ledger_requests;",
        "true",
        f"{suffix} invalid payloads do not mutate requests",
    )
    _assert_scalar(
        target,
        dsn,
        f"SELECT (pg_catalog.count(*) = {settlement_count})::text "
        "FROM public.prediction_ledger_settlements;",
        "true",
        f"{suffix} invalid payloads do not mutate settlements",
    )
    return len(cases) + 2


def _assert_legacy_exact_retry_guards(
    target: RehearsalTarget,
    dsn: str,
    *,
    field: dict[str, object],
    suffix: str,
) -> int:
    """Prove 005/006 RPC retries are exact, complete, and atomic."""
    checks = 0
    _assert_scalar(
        target,
        dsn,
        _rpc_sql(field),
        '{"kind": "field", "accepted": true, "duplicate": true}',
        f"{suffix} exact field retry",
    )
    checks += 1

    request_changed = json.loads(json.dumps(field))
    request_changed["request"]["prediction_as_of"] = "2026-08-02"
    _psql(
        target,
        dsn,
        sql=_rpc_sql(request_changed),
        expect_error="ledger request projection conflict",
    )

    prediction_changed = json.loads(json.dumps(field))
    prediction_changed["predictions"][0]["source"] = "changed-source"
    _psql(
        target,
        dsn,
        sql=_rpc_sql(prediction_changed),
        expect_error="ledger prediction projection conflict",
    )

    prediction_missing = json.loads(json.dumps(field))
    removed_prediction_id = prediction_missing["predictions"].pop()["prediction_id"]
    prediction_missing["features"] = [
        feature
        for feature in prediction_missing["features"]
        if feature["prediction_id"] != removed_prediction_id
    ]
    _psql(
        target,
        dsn,
        sql=_rpc_sql(prediction_missing),
        expect_error="ledger prediction projection conflict",
    )

    prediction_extra = json.loads(json.dumps(field))
    extra_prediction = json.loads(json.dumps(prediction_extra["predictions"][0]))
    extra_prediction.update(
        prediction_id=f"prediction-extra-{suffix}",
        competitor_id="C002",
    )
    prediction_extra["predictions"].append(extra_prediction)
    _psql(
        target,
        dsn,
        sql=_rpc_sql(prediction_extra),
        expect_error="ledger prediction projection conflict",
    )

    feature_changed = json.loads(json.dumps(field))
    feature_changed["features"][0]["numeric_value"] = 99.0
    _psql(
        target,
        dsn,
        sql=_rpc_sql(feature_changed),
        expect_error="ledger feature projection conflict",
    )

    feature_missing = json.loads(json.dumps(field))
    feature_missing["features"].pop()
    _psql(
        target,
        dsn,
        sql=_rpc_sql(feature_missing),
        expect_error="ledger feature projection conflict",
    )

    feature_extra = json.loads(json.dumps(field))
    extra_feature = json.loads(json.dumps(feature_extra["features"][0]))
    extra_feature.update(
        feature_snapshot_id=f"feature-extra-{suffix}",
        feature_name="extra_feature",
    )
    feature_extra["features"].append(extra_feature)
    _psql(
        target,
        dsn,
        sql=_rpc_sql(feature_extra),
        expect_error="ledger feature projection conflict",
    )
    checks += 7

    collision_prediction = _field_payload(
        f"collision-prediction-{suffix}",
        request_hash="4" * 64,
        hash_algorithm=field["request"].get("hash_algorithm"),
    )
    collision_prediction["predictions"][0]["prediction_id"] = field["predictions"][0][
        "prediction_id"
    ]
    collision_prediction["features"][0]["prediction_id"] = collision_prediction["predictions"][0][
        "prediction_id"
    ]
    _psql(
        target,
        dsn,
        sql=_rpc_sql(collision_prediction),
        expect_error="ledger prediction projection conflict",
    )
    collision_feature = _field_payload(
        f"collision-feature-{suffix}",
        request_hash="5" * 64,
        hash_algorithm=field["request"].get("hash_algorithm"),
    )
    collision_feature["features"][0]["feature_snapshot_id"] = field["features"][0][
        "feature_snapshot_id"
    ]
    _psql(
        target,
        dsn,
        sql=_rpc_sql(collision_feature),
        expect_error="ledger feature projection conflict",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 0)::text "
        "FROM public.prediction_ledger_requests "
        f"WHERE request_id IN ('request-collision-prediction-{suffix}', "
        f"'request-collision-feature-{suffix}');",
        "true",
        f"{suffix} cross-request child collisions roll back requests",
    )
    checks += 3

    settlement = {
        "settlement": {
            "settlement_id": "settlement-raw",
            "prediction_id": field["predictions"][0]["prediction_id"],
            "revision": 1,
            "competitor_id": field["predictions"][0]["competitor_id"],
            "event_code": field["predictions"][0]["event_code"],
            "actual_time": 43.0,
            "residual": 0.5,
            "actor": "migration-rehearsal",
            "reason": "initial",
            "payload_hash": "6" * 64,
            "supersedes_settlement_id": None,
            "settled_at": "2026-08-02T00:00:00+00:00",
        }
    }
    _assert_scalar(
        target,
        dsn,
        _rpc_sql(settlement),
        '{"kind": "settlement", "accepted": true}',
        f"{suffix} settlement baseline",
    )
    _assert_scalar(
        target,
        dsn,
        _rpc_sql(settlement),
        '{"kind": "settlement", "accepted": true}',
        f"{suffix} exact settlement retry",
    )
    settlement_count = _psql(
        target,
        dsn,
        sql="SELECT pg_catalog.count(*) FROM public.prediction_ledger_settlements;",
    ).strip()
    for field_name, changed_value in (
        ("actual_time", 44.0),
        ("residual", 1.5),
        ("actor", "changed-actor"),
        ("reason", "changed-reason"),
        ("supersedes_settlement_id", "settlement-other"),
    ):
        changed_settlement = json.loads(json.dumps(settlement))
        changed_settlement["settlement"][field_name] = changed_value
        _psql(
            target,
            dsn,
            sql=_rpc_sql(changed_settlement),
            expect_error="ledger settlement payload conflict",
        )
    checks += 7

    invalid_revision = json.loads(json.dumps(settlement))
    invalid_revision["settlement"].update(
        settlement_id=f"settlement-invalid-revision-{suffix}",
        revision=3,
        reason="correction",
        payload_hash="7" * 64,
        supersedes_settlement_id="settlement-raw",
    )
    _psql(
        target,
        dsn,
        sql=_rpc_sql(invalid_revision),
        expect_error="ledger settlement revision conflict",
    )
    wrong_latest = json.loads(json.dumps(invalid_revision))
    wrong_latest["settlement"].update(
        settlement_id=f"settlement-wrong-latest-{suffix}",
        revision=2,
        payload_hash="8" * 64,
        supersedes_settlement_id="not-the-latest",
    )
    _psql(
        target,
        dsn,
        sql=_rpc_sql(wrong_latest),
        expect_error="ledger settlement revision conflict",
    )
    missing_reason = json.loads(json.dumps(wrong_latest))
    missing_reason["settlement"].update(
        settlement_id=f"settlement-missing-reason-{suffix}",
        payload_hash="9" * 64,
        supersedes_settlement_id="settlement-raw",
        reason=None,
    )
    _psql(
        target,
        dsn,
        sql=_rpc_sql(missing_reason),
        expect_error="ledger settlement correction reason conflict",
    )
    bad_residual = json.loads(json.dumps(missing_reason))
    bad_residual["settlement"].update(
        settlement_id=f"settlement-bad-residual-{suffix}",
        payload_hash="a" * 64,
        reason="correction",
        residual=99.0,
    )
    _psql(
        target,
        dsn,
        sql=_rpc_sql(bad_residual),
        expect_error="ledger settlement residual conflict",
    )
    _assert_scalar(
        target,
        dsn,
        f"SELECT (pg_catalog.count(*) = {settlement_count})::text "
        "FROM public.prediction_ledger_settlements;",
        "true",
        f"{suffix} settlement conflicts roll back atomically",
    )
    checks += 5
    return checks


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
    _assert_scalar(
        target,
        target.dsn,
        "SELECT (NOT owner.rolinherit AND NOT owner.rolsuper "
        "AND NOT owner.rolcreatedb AND NOT owner.rolcreaterole "
        "AND NOT owner.rolreplication AND NOT owner.rolcanlogin "
        "AND NOT owner.rolbypassrls AND NOT EXISTS ("
        "SELECT 1 FROM pg_catalog.pg_auth_members AS membership "
        "WHERE membership.member=owner.oid OR membership.roleid=owner.oid))::text "
        "FROM pg_catalog.pg_roles AS owner "
        "WHERE owner.rolname='strathmark_prediction_rpc_owner';",
        "true",
        "dedicated RPC owner is isolated and unprivileged",
    )
    _psql(target, target.dsn, sql=f"ALTER ROLE {role} INHERIT;")
    try:
        _psql(
            target,
            target.dsn,
            sql_file=prerequisite,
            expect_error="must be isolated and unprivileged",
        )
    finally:
        _psql(target, target.dsn, sql=f"ALTER ROLE {role} NOINHERIT;")

    membership_probe = "strathmark_rehearsal_rpc_membership_probe"
    if _psql(
        target,
        target.dsn,
        sql=(f"SELECT rolname FROM pg_catalog.pg_roles WHERE rolname='{membership_probe}';"),
    ).strip():
        raise RehearsalExecutionError("dedicated owner membership probe role already exists")
    _psql(target, target.dsn, sql=f"CREATE ROLE {membership_probe} NOLOGIN NOBYPASSRLS;")
    try:
        _psql(target, target.dsn, sql=f"GRANT {membership_probe} TO {role};")
        _psql(
            target,
            target.dsn,
            sql_file=prerequisite,
            expect_error="must be isolated and unprivileged",
        )
    finally:
        _psql(target, target.dsn, sql=f"REVOKE {membership_probe} FROM {role};")
        _psql(target, target.dsn, sql=f"DROP ROLE {membership_probe};")
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
    second_prediction = json.loads(json.dumps(raw["predictions"][0]))
    second_prediction.update(
        prediction_id="prediction-raw-2",
        competitor_id="C002",
    )
    raw["predictions"].append(second_prediction)
    second_feature = json.loads(json.dumps(raw["features"][0]))
    second_feature.update(
        feature_snapshot_id="feature-raw-2",
        prediction_id="prediction-raw-2",
    )
    raw["features"].append(second_feature)
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
    checks += _assert_legacy_exact_retry_guards(
        target,
        dsn,
        field=raw,
        suffix="005",
    )

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
        "p.proowner=r.oid AND p.prosecdef "
        "AND p.proconfig @> ARRAY['search_path=\"\"'])::text "
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
    checks += _assert_legacy_exact_retry_guards(
        target,
        dsn,
        field=raw,
        suffix="006",
    )
    _psql(target, dsn, sql_file=rollback_006)
    checks += 1
    checks += _assert_invalid_payloads(target, dsn, "006-down")
    checks += _assert_legacy_exact_retry_guards(
        target,
        dsn,
        field=raw,
        suffix="006-down",
    )
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
    active_process = _start_psql(
        target,
        dsn,
        "BEGIN; " + _rpc_sql(active) + " SELECT pg_catalog.pg_sleep(1.5); COMMIT;",
    )
    _wait_for_relation_lock(
        target,
        dsn,
        active_process,
        "prediction_ledger_requests",
        "concurrent 006 append",
    )
    try:
        _psql(
            target,
            dsn,
            sql_file=rollback_006,
            expect_error="cannot roll back migration 006",
        )
    finally:
        _finish_psql(target, active_process, "concurrent 006 append")
    _assert_scalar(
        target,
        dsn,
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid="
        "'public.prediction_ledger_requests'::pg_catalog.regclass "
        "AND attname='hash_algorithm' AND NOT attisdropped)::text;",
        "true",
        "concurrent 006 append cannot cross guarded rollback",
    )
    _psql(
        target,
        dsn,
        sql=(
            "BEGIN; SET ROLE strathmark_prediction_rpc_owner; "
            "SET LOCAL row_security=off; "
            "SELECT 1 FROM public.prediction_ledger_requests; ROLLBACK;"
        ),
        expect_error="query would be affected by row-level security policy",
    )
    checks += 3  # NOBYPASS owner guard fails closed under RLS.

    _assert_scalar(
        target,
        dsn,
        _rpc_sql(active),
        '{"kind": "field", "accepted": true, "duplicate": true}',
        "006 concurrent active-v2 RPC remains exact",
    )
    checks += 1
    _assert_scalar(
        target,
        dsn,
        _rpc_sql(active),
        '{"kind": "field", "accepted": true, "duplicate": true}',
        "exact retry",
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
        "2",
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
        expect_error="settlement prediction linkage mismatch",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_settlements;",
        "2",
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
        "RESET ROLE; "
        "SELECT pg_catalog.count(*) FROM public.prediction_ledger_requests "
        "WHERE ledger_request_id='ledger-shadow';"
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

    rollback_race_receipt = _shadow_receipt_payload("rollback-race")
    rollback_race_process = _start_psql(
        target,
        dsn,
        "BEGIN; "
        + _shadow_rpc_sql(rollback_race_receipt)
        + " SELECT pg_catalog.pg_sleep(1.5); COMMIT;",
    )
    _wait_for_relation_lock(
        target,
        dsn,
        rollback_race_process,
        "shadow_mirror_deliveries",
        "concurrent 007 append",
    )
    try:
        _psql(
            target,
            dsn,
            sql_file=rollback_007,
            expect_error="cannot roll back migration 007",
        )
    finally:
        _finish_psql(target, rollback_race_process, "concurrent 007 append")
    _assert_scalar(
        target,
        dsn,
        "SELECT EXISTS (SELECT 1 FROM public.shadow_receipt_cores "
        "WHERE ledger_request_id='ledger-shadow-rollback-race')::text;",
        "true",
        "concurrent 007 append cannot cross guarded rollback",
    )
    _psql(
        target,
        dsn,
        sql=(
            "BEGIN; SET ROLE strathmark_prediction_rpc_owner; "
            "SET LOCAL row_security=off; "
            "SELECT 1 FROM public.shadow_mirror_deliveries; ROLLBACK;"
        ),
        expect_error="query would be affected by row-level security policy",
    )
    checks += 3  # NOBYPASS owner guard fails closed under RLS.

    with tempfile.TemporaryDirectory(prefix="strathmark-ledger-rehearsal-") as ledger_dir:
        generated_receipt = _prediction_ledger_shadow_payload(
            Path(ledger_dir) / "prediction-ledger.db"
        )
        _assert_scalar(
            target,
            dsn,
            _shadow_rpc_sql(generated_receipt),
            '{"kind": "shadow_receipt", "accepted": true}',
            "PredictionLedger envelope is accepted byte-for-byte",
        )
    checks += 1

    receipt = _shadow_receipt_payload("receipt")
    receipt["receipt"]["core"]["evidence_snapshot"]["diagnostics"] = {
        "accepted_rows": 1,
        "invalid_time": 0,
    }
    receipt["receipt"]["core"]["evidence_diagnostics"][0]["excluded_by_reason"] = {
        "future_result": 0,
        "same_day_result": 0,
    }
    _sync_shadow_active_evidence(receipt)
    _bind_shadow_delivery(receipt)
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
    lexical_retry = json.loads(json.dumps(receipt))
    alternate_json = json.dumps(
        _shadow_payload_body(lexical_retry),
        ensure_ascii=True,
        indent=2,
        sort_keys=False,
    )
    _bind_shadow_delivery(lexical_retry, canonical_payload=alternate_json)
    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(lexical_retry),
        '{"kind": "shadow_receipt", "accepted": true, "duplicate": true}',
        "007 semantic retry with alternate JSON whitespace",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 3)::text FROM public.shadow_receipt_cores;",
        "true",
        "007 receipt retry remains single-copy",
    )
    checks += 4  # semantic receipt retry accepts alternate canonical JSON whitespace

    poisoned = _shadow_receipt_payload("digest-guard")
    poisoned["delivery"]["payload_hash"] = "5" * 64
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(poisoned),
        expect_error="shadow mirror canonical payload digest does not match",
    )
    legitimate_after_poison = _shadow_receipt_payload("digest-guard")
    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(legitimate_after_poison),
        '{"kind": "shadow_receipt", "accepted": true}',
        "007 legitimate retry after wrong digest",
    )
    checks += 2  # wrong delivery digest cannot poison a legitimate receipt retry

    receipt_conflict = json.loads(json.dumps(receipt))
    receipt_conflict["delivery"]["created_at"] = "2026-08-04T00:00:00+00:00"
    _bind_shadow_delivery(receipt_conflict)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(receipt_conflict),
        expect_error="shadow mirror outbox conflict",
    )
    checks += 1

    receipt_semantic_conflict = json.loads(json.dumps(receipt))
    receipt_semantic_conflict["receipt"]["core"]["artifact"]["provider_source"] = "changed-provider"
    _bind_shadow_delivery(receipt_semantic_conflict)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(receipt_semantic_conflict),
        expect_error="shadow mirror duplicate semantic conflict",
    )
    checks += 1  # same claimed hash with changed receipt semantics conflicts

    nested_ledger_conflict = json.loads(json.dumps(receipt))
    nested_ledger_conflict["ledger"]["predictions"][0]["source"] = "changed-source"
    nested_ledger_conflict["receipt"]["core"]["predictions"][0]["source"] = "changed-source"
    _bind_shadow_delivery(nested_ledger_conflict)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(nested_ledger_conflict),
        expect_error="shadow mirror duplicate semantic conflict",
    )
    nested_ledger_extra = json.loads(json.dumps(receipt))
    nested_ledger_extra["ledger"]["features"][0]["unknown"] = "not-allowed"
    _bind_shadow_delivery(nested_ledger_extra)
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
        "email",
    )
    for forbidden_key in forbidden_shadow_keys:
        privacy_probe = json.loads(json.dumps(receipt))
        privacy_probe["receipt"]["core"]["privacy_probe"] = {
            "nested": [{forbidden_key.upper(): "must-not-cross"}]
        }
        _bind_shadow_delivery(privacy_probe)
        _psql(
            target,
            dsn,
            sql=_shadow_rpc_sql(privacy_probe),
            expect_error="shadow mirror contains prohibited operational or free-text data",
        )
    # direct RPC rejects every recursively prohibited privacy key.
    # A rehashed email field is rejected by the recursive privacy guard.
    checks += len(forbidden_shadow_keys)

    unknown_core = json.loads(json.dumps(receipt))
    unknown_core["receipt"]["core"]["innocuous_but_unfrozen"] = True
    _bind_shadow_delivery(unknown_core)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(unknown_core),
        expect_error="shadow receipt core has unknown or missing properties",
    )
    missing_core = json.loads(json.dumps(receipt))
    missing_core["receipt"]["core"].pop("artifact")
    _bind_shadow_delivery(missing_core)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(missing_core),
        expect_error="shadow receipt core has unknown or missing properties",
    )
    bad_nested_identity = json.loads(json.dumps(receipt))
    bad_nested_identity["receipt"]["core"]["operator_id"] = "not-namespaced"
    bad_nested_identity["receipt"]["core"]["request_projection"]["operator_id"] = "not-namespaced"
    _bind_shadow_delivery(bad_nested_identity)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(bad_nested_identity),
        expect_error="shadow receipt core identity namespace is invalid",
    )
    checks += 3  # frozen receipt core rejects unknown, missing, and invalid nested identity fields

    invalid_count_maps = (
        ("snapshot-cardinality", "snapshot", {f"reason_{index}": 0 for index in range(129)}),
        ("snapshot-key", "snapshot", {"Not_Machine_Code": 1}),
        ("snapshot-negative", "snapshot", {"invalid_time": -1}),
        ("snapshot-bool", "snapshot", {"invalid_time": True}),
        ("snapshot-float", "snapshot", {"invalid_time": 1.5}),
        ("snapshot-integral-float", "snapshot", {"invalid_time": 1.0}),
        ("snapshot-string", "snapshot", {"invalid_time": "1"}),
        ("snapshot-nested", "snapshot", {"invalid_time": {"nested": 1}}),
        ("excluded-cardinality", "excluded", {f"reason_{index}": 0 for index in range(129)}),
        ("excluded-key", "excluded", {"not-machine-code": 1}),
        ("excluded-negative", "excluded", {"invalid_time": -1}),
        ("excluded-bool", "excluded", {"invalid_time": False}),
        ("excluded-float", "excluded", {"invalid_time": 1.5}),
        ("excluded-integral-float", "excluded", {"invalid_time": 1.0}),
        ("excluded-string", "excluded", {"invalid_time": "1"}),
        ("excluded-nested", "excluded", {"invalid_time": [1]}),
    )
    for description, target_map, invalid_map in invalid_count_maps:
        invalid_counts = json.loads(json.dumps(receipt))
        if target_map == "snapshot":
            invalid_counts["receipt"]["core"]["evidence_snapshot"]["diagnostics"] = invalid_map
            _sync_shadow_active_evidence(invalid_counts)
            expected_error = (
                "shadow receipt evidence snapshot diagnostics must be a bounded count map"
            )
        else:
            invalid_counts["receipt"]["core"]["evidence_diagnostics"][0]["excluded_by_reason"] = (
                invalid_map
            )
            expected_error = "shadow receipt excluded_by_reason must be a bounded count map"
        _bind_shadow_delivery(invalid_counts)
        _psql(
            target,
            dsn,
            sql=_shadow_rpc_sql(invalid_counts),
            expect_error=expected_error,
        )
    checks += len(invalid_count_maps)  # direct RPC rejects invalid bounded evidence count maps

    invalid_diagnostic_scalars = (
        ("ordinal-bool", "ordinal", True),
        ("ordinal-float", "ordinal", 0.0),
        ("ordinal-string", "ordinal", "0"),
        ("total-negative", "total_rows", -1),
        ("included-object", "included_rows", {"nested": 1}),
        ("excluded-too-large", "excluded_rows", 2_147_483_648),
    )
    for description, field_name, invalid_value in invalid_diagnostic_scalars:
        invalid_diagnostic = json.loads(json.dumps(receipt))
        invalid_diagnostic["receipt"]["core"]["evidence_diagnostics"][0][field_name] = invalid_value
        _bind_shadow_delivery(invalid_diagnostic)
        _psql(
            target,
            dsn,
            sql=_shadow_rpc_sql(invalid_diagnostic),
            expect_error=(
                "shadow receipt evidence diagnostic counts must be bounded nonnegative integers"
            ),
        )
    invalid_canonicalization_versions = ("", "   ", 1, "v" * 129)
    for invalid_value in invalid_canonicalization_versions:
        invalid_diagnostic = json.loads(json.dumps(receipt))
        invalid_diagnostic["receipt"]["core"]["evidence_diagnostics"][0][
            "canonicalization_version"
        ] = invalid_value
        _bind_shadow_delivery(invalid_diagnostic)
        _psql(
            target,
            dsn,
            sql=_shadow_rpc_sql(invalid_diagnostic),
            expect_error="shadow receipt evidence canonicalization_version is invalid",
        )
    # Direct RPC rejects mistyped or unbounded evidence diagnostic scalars.
    checks += len(invalid_diagnostic_scalars) + len(invalid_canonicalization_versions)

    multi_receipt = _shadow_receipt_payload(
        "multi",
        competitor_ids=("missoula:competitor:1", "missoula:competitor:2"),
    )
    duplicate_prediction = json.loads(json.dumps(multi_receipt))
    duplicate_prediction["receipt"]["core"]["predictions"][1]["prediction_id"] = (
        duplicate_prediction["receipt"]["core"]["predictions"][0]["prediction_id"]
    )
    _bind_shadow_delivery(duplicate_prediction)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(duplicate_prediction),
        expect_error="shadow receipt prediction identities are incomplete or duplicated",
    )
    checks += 1  # duplicate receipt prediction IDs are rejected

    missing_prediction = json.loads(json.dumps(multi_receipt))
    missing_prediction["receipt"]["core"]["predictions"][1]["prediction_id"] = (
        "prediction-shadow-multi-not-in-ledger"
    )
    _bind_shadow_delivery(missing_prediction)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(missing_prediction),
        expect_error="shadow receipt prediction identities are incomplete or duplicated",
    )
    checks += 1  # missing receipt prediction IDs are rejected

    invalid_receipt = _shadow_receipt_payload(
        "invalid-fk",
        competitor_id="missoula:competitor:999",
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
        "SELECT (pg_catalog.count(*) = 4)::text FROM public.shadow_mirror_deliveries;",
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
        "AND p.prosecdef AND p.proconfig @> ARRAY['search_path=\"\"'] AND "
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

    authority_receipt = _shadow_receipt_payload("authority-race")
    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(authority_receipt),
        '{"kind": "shadow_receipt", "accepted": true}',
        "007 split-authority race receipt",
    )
    legacy_authority = {
        "settlement": {
            "settlement_id": "legacy-authority-1",
            "prediction_id": "prediction-shadow-authority-race",
            "revision": 1,
            "competitor_id": "missoula:competitor:1",
            "event_code": "SB",
            "actual_time": 43.0,
            "residual": 0.5,
            "actor": "migration-rehearsal",
            "reason": "initial",
            "payload_hash": "7" * 64,
            "supersedes_settlement_id": None,
            "settled_at": "2026-08-04T00:00:00+00:00",
        }
    }
    legacy_process = _start_psql(
        target,
        dsn,
        "BEGIN; " + _rpc_sql(legacy_authority) + " SELECT pg_catalog.pg_sleep(1.5); COMMIT;",
    )
    _wait_for_advisory_lock(target, dsn, legacy_process, "legacy-first authority race")
    numeric_after_legacy = _numeric_shadow_payload(
        action="settle",
        revision=2,
        suffix="authority-after-legacy",
        supersedes_revision_id="legacy-authority-1",
        ledger_request_id="ledger-shadow-authority-race",
        prediction_id="prediction-shadow-authority-race",
    )
    try:
        _assert_scalar(
            target,
            dsn,
            _shadow_rpc_sql(numeric_after_legacy),
            '{"kind": "numeric_outcome_revision", "accepted": true}',
            "legacy-first authority race serializes into numeric revision",
        )
    finally:
        _finish_psql(target, legacy_process, "legacy-first authority race")

    numeric_first = _numeric_shadow_payload(
        action="void",
        revision=3,
        suffix="authority-numeric-first",
        supersedes_revision_id="numeric-revision-authority-after-legacy",
        ledger_request_id="ledger-shadow-authority-race",
        prediction_id="prediction-shadow-authority-race",
    )
    numeric_process = _start_psql(
        target,
        dsn,
        "BEGIN; " + _shadow_rpc_sql(numeric_first) + " SELECT pg_catalog.pg_sleep(1.5); COMMIT;",
    )
    _wait_for_advisory_lock(target, dsn, numeric_process, "numeric-first authority race")
    legacy_after_numeric = json.loads(json.dumps(legacy_authority))
    legacy_after_numeric["settlement"].update(
        settlement_id="legacy-authority-2",
        revision=2,
        actual_time=44.0,
        residual=1.5,
        reason="correction",
        payload_hash="8" * 64,
        supersedes_settlement_id="legacy-authority-1",
        settled_at="2026-08-06T00:00:00+00:00",
    )
    try:
        _psql(
            target,
            dsn,
            sql=_rpc_sql(legacy_after_numeric),
            expect_error="numeric authority rejects legacy settlement append",
        )
    finally:
        _finish_psql(target, numeric_process, "numeric-first authority race")
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 1)::text "
        "FROM public.prediction_ledger_settlements "
        "WHERE prediction_id='prediction-shadow-authority-race';",
        "true",
        "numeric-first authority race rejects legacy append",
    )
    checks += 5

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
    _bind_shadow_delivery(fractional_revision)
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
    lexical_numeric_retry = json.loads(json.dumps(settle))
    alternate_numeric_json = json.dumps(
        _shadow_payload_body(lexical_numeric_retry),
        ensure_ascii=True,
        indent=2,
        sort_keys=False,
    )
    _bind_shadow_delivery(
        lexical_numeric_retry,
        canonical_payload=alternate_numeric_json,
    )
    _assert_scalar(
        target,
        dsn,
        _shadow_rpc_sql(lexical_numeric_retry),
        '{"kind": "numeric_outcome_revision", "accepted": true, "duplicate": true}',
        "007 semantic numeric retry with alternate JSON whitespace",
    )
    numeric_semantic_conflict = json.loads(json.dumps(settle))
    numeric_semantic_conflict["numeric_outcome_revision"]["actor"] = "missoula:operator:judge-2"
    _bind_shadow_delivery(numeric_semantic_conflict)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(numeric_semantic_conflict),
        expect_error="shadow mirror duplicate semantic conflict",
    )
    # An alternate lexical numeric retry passes.
    # The same claimed hash with changed numeric semantics conflicts.
    checks += 3

    missing_reason = _numeric_shadow_payload(
        action="settle",
        revision=2,
        suffix="missing-reason",
        supersedes_revision_id="numeric-revision-settle",
    )
    missing_reason["numeric_outcome_revision"]["reason_code"] = None
    _bind_shadow_delivery(missing_reason)
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
    _bind_shadow_delivery(wrong_supersedes)
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
    _bind_shadow_delivery(bad_residual)
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
        "SELECT (pg_catalog.count(*) = 4)::text FROM public.shadow_numeric_settlement_revisions;",
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
    _bind_shadow_delivery(invalid_numeric)
    _psql(
        target,
        dsn,
        sql=_shadow_rpc_sql(invalid_numeric),
        expect_error="numeric settlement revision linkage or value is invalid",
    )
    _assert_scalar(
        target,
        dsn,
        "SELECT (pg_catalog.count(*) = 9)::text FROM public.shadow_mirror_deliveries;",
        "true",
        "007 invalid numeric revision rolls back delivery and header",
    )
    checks += 2

    shadowed_receipt = _shadow_receipt_payload("object-shadow")
    shadowed_sql = (
        "SET ROLE service_role; "
        "CREATE TEMP TABLE shadow_receipt_cores (sentinel pg_catalog.text); "
        "CREATE TEMP TABLE shadow_mirror_deliveries (sentinel pg_catalog.text); "
        f"SELECT public.append_shadow_mirror_v1("
        f"'{_json_literal(shadowed_receipt)}'::pg_catalog.jsonb); "
        "RESET ROLE; "
        "SELECT pg_catalog.count(*) FROM public.shadow_receipt_cores "
        "WHERE ledger_request_id='ledger-shadow-object-shadow';"
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
        # rollback-race, generated-ledger, receipt, digest-guard,
        # authority-race, and object-shadow are the six accepted receipts.
        "SELECT (pg_catalog.count(*) = 6)::text FROM public.shadow_receipt_cores;",
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
