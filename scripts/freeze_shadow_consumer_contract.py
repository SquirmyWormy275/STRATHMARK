"""Deterministically freeze the self-contained local shadow consumer contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from strathmark.auth import (
    ACTOR_ATTESTATION_SCHEMA_VERSION,
    REQUEST_DIGEST_SCHEMA_VERSION,
    SHADOW_ACTION_ROLES,
    SHADOW_ATTESTATION_AUDIENCE,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "strathmark" / "contracts" / "shadow_consumer_v1.openapi.json"
CHECKSUM = ROOT / "strathmark" / "contracts" / "shadow_consumer_v1.openapi.sha256"

NAMESPACED_ID = {"$ref": "#/components/schemas/NamespacedId"}
DIGEST = {"$ref": "#/components/schemas/Digest"}
DATE = {"type": "string", "format": "date"}
TIMESTAMP = {"type": "string", "format": "date-time"}
NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_NUMBER = {"type": ["number", "null"]}


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def array(items: Any, **constraints: Any) -> dict[str, Any]:
    return {"type": "array", "items": items, **constraints}


def obj(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    additional: bool | dict[str, Any] = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": additional,
        "required": list(properties) if required is None else required,
        "properties": properties,
    }


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"oneOf": [schema, {"type": "null"}]}


def response_schemas() -> dict[str, Any]:
    allowed_roles = sorted({role for roles in SHADOW_ACTION_ROLES.values() for role in roles})
    integer_map = {"type": "object", "additionalProperties": {"type": "integer"}}
    calculation_competitor = obj(
        {
            "competitor_id": NAMESPACED_ID,
            "gender": {"enum": ["M", "F", "__MISSING__"]},
            "manual_time_override": {"type": "null"},
            "history": array(ref("EvidenceHistoryRow")),
        }
    )
    calculation_input = obj(
        {
            "event_code": {"enum": ["SB", "UH"]},
            "prediction_as_of": DATE,
            "diameter_mm": {"type": "number", "minimum": 225, "maximum": 500},
            "species": {"type": "string", "minLength": 1, "maxLength": 100},
            "wood_properties": ref("WoodProperties"),
            "seed": {"type": "integer"},
            "engine": {"const": "v2"},
            "effective_mark_ceiling": {"type": "integer", "minimum": 3},
            "competitors": array(calculation_competitor, minItems=1, maxItems=64),
        }
    )
    evidence_input_properties = {
        "schema_version": {"const": "strathmark.evidence-snapshot.v1"},
        "snapshot_digest": DIGEST,
        "source_schema_version": {"type": "string", "minLength": 1},
        "source_id": NAMESPACED_ID,
        "source_digest": DIGEST,
        "cutoff": DATE,
        "cutoff_semantics": {"const": "exclusive-utc-date"},
        "captured_at": TIMESTAMP,
        "activation_id": {"type": "string", "minLength": 1},
        "activation_revision": {"type": "integer", "minimum": 1},
        "previous_activation_id": NULLABLE_STRING,
        "supersedes_snapshot_digest": nullable(DIGEST),
        "completeness": {"type": "string", "minLength": 1},
        "supplied_row_count": {"type": "integer", "minimum": 0},
        "accepted_row_count": {"type": "integer", "minimum": 0},
        "rejected_row_count": {"type": "integer", "minimum": 0},
        "diagnostics": ref("IntegerMap"),
    }
    evidence_receipt_properties = {
        **evidence_input_properties,
        "activated_at": TIMESTAMP,
        "age_days_at_calculation": {"type": "integer", "minimum": 0},
        "freshness_at_calculation": {"type": "string", "minLength": 1},
        "integrity": {"type": "string", "minLength": 1},
        "ready_for_offline_at_calculation": {"type": "boolean"},
    }
    objective_item = {
        "anyOf": [
            {"type": "number"},
            array({"type": "integer"}),
            {"type": "null"},
        ]
    }
    return {
        "ErrorResponse": obj(
            {
                "detail": {"type": "string", "minLength": 1},
            }
        ),
        "ActorAttestationClaims": obj(
            {
                "schema_version": {"const": ACTOR_ATTESTATION_SCHEMA_VERSION},
                "consumer_id": NAMESPACED_ID,
                "actor_id": NAMESPACED_ID,
                "roles": array(
                    {
                        "enum": allowed_roles,
                    },
                    minItems=1,
                    maxItems=8,
                    uniqueItems=True,
                ),
                "action": {"enum": sorted(SHADOW_ACTION_ROLES)},
                "subject_revision": NAMESPACED_ID,
                "request_digest_schema_version": {"const": REQUEST_DIGEST_SCHEMA_VERSION},
                "request_digest": DIGEST,
                "audience": {"const": SHADOW_ATTESTATION_AUDIENCE},
                "nonce": {
                    "type": "string",
                    "minLength": 16,
                    "maxLength": 128,
                    "pattern": r"^[A-Za-z0-9._:-]+$",
                },
                "issued_at": {"type": "integer"},
                "expires_at": {"type": "integer"},
            }
        ),
        "IntegerMap": integer_map,
        "WoodProperties": obj(
            {
                "janka_hardness": {"type": "number"},
                "specific_gravity": {"type": "number"},
                "crush_strength": {"type": "number"},
                "shear_strength": {"type": "number"},
                "modulus_of_rupture": {"type": "number"},
                "modulus_of_elasticity": {"type": "number"},
                "species_missing": {"type": "boolean"},
            }
        ),
        "EvidenceHistoryRow": obj(
            {
                "competitor_id": NAMESPACED_ID,
                "event": {"enum": ["SB", "UH"]},
                "time_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 300},
                "result_date": DATE,
                "diameter_mm": {"type": "number", "minimum": 225, "maximum": 500},
                "species": {"type": "string", "minLength": 1},
                "gender": {"enum": ["M", "F", "__MISSING__"]},
                "janka_hardness": {"type": "number"},
                "specific_gravity": {"type": "number"},
                "crush_strength": {"type": "number"},
                "shear_strength": {"type": "number"},
                "modulus_of_rupture": {"type": "number"},
                "modulus_of_elasticity": {"type": "number"},
                "species_missing": {"type": "boolean"},
            }
        ),
        "CalculationInput": calculation_input,
        "RequestProjectionEntrant": obj(
            {
                "competitor_id": NAMESPACED_ID,
                "gender": {"enum": ["M", "F", "UNKNOWN"]},
            }
        ),
        "RequestProjectionWood": obj(
            {
                "species": {"type": "string", "minLength": 1, "maxLength": 100},
                "diameter_mm": {"type": "number", "minimum": 225, "maximum": 500},
                "quality": {"type": "integer", "minimum": 1, "maximum": 10},
            }
        ),
        "RequestProjection": obj(
            {
                "schema_version": {"const": "strathmark.shadow-request-projection.v1"},
                "consumer_id": NAMESPACED_ID,
                "tournament_id": NAMESPACED_ID,
                "event_occurrence_id": NAMESPACED_ID,
                "field_run_id": NAMESPACED_ID,
                "operator_id": NAMESPACED_ID,
                "request_id": NAMESPACED_ID,
                "run_revision": NAMESPACED_ID,
                "event_code": {"enum": ["SB", "UH"]},
                "target_contract": {"const": "single-elapsed-seconds.v1"},
                "prediction_as_of": DATE,
                "cutoff_semantics": {"const": "exclusive-utc-date"},
                "schedule_fingerprint": DIGEST,
                "observation_schema_version": {
                    "const": "strathmark.shadow-observation-fingerprint.v1"
                },
                "observation_fingerprint": DIGEST,
                "seed": {"type": "integer"},
                "competitors": array(ref("RequestProjectionEntrant"), minItems=1, maxItems=64),
                "wood": ref("RequestProjectionWood"),
                "fingerprint": DIGEST,
            }
        ),
        "EvidenceInputProjection": obj(evidence_input_properties),
        "EvidenceReceiptProjection": obj(evidence_receipt_properties),
        "ActiveInput": obj(
            {
                "schema_version": {"const": "strathmark.shadow-active-input.v1"},
                "tournament_id": NAMESPACED_ID,
                "event_occurrence_id": NAMESPACED_ID,
                "field_run_id": NAMESPACED_ID,
                "target_contract": {"const": "single-elapsed-seconds.v1"},
                "schedule_fingerprint": DIGEST,
                "caller_input": ref("CalculationInput"),
                "evidence_snapshot": ref("EvidenceInputProjection"),
                "fingerprint": DIGEST,
            }
        ),
        "Observation": obj(
            {
                "schema_version": {"const": "strathmark.shadow-observation-fingerprint.v1"},
                "fingerprint": DIGEST,
            }
        ),
        "Artifact": obj(
            {
                "provider_source": {"type": "string"},
                "source_digest": nullable(DIGEST),
                "artifact_digest": nullable(DIGEST),
                "model_version": NULLABLE_STRING,
                "calibration_version": NULLABLE_STRING,
                "residual_version": NULLABLE_STRING,
            }
        ),
        "EvidenceDiagnostic": obj(
            {
                "ordinal": {"type": "integer", "minimum": 0},
                "competitor_id": NAMESPACED_ID,
                "total_rows": {"type": "integer", "minimum": 0},
                "included_rows": {"type": "integer", "minimum": 0},
                "excluded_rows": {"type": "integer", "minimum": 0},
                "excluded_by_reason": ref("IntegerMap"),
                "canonicalization_version": {"type": "string", "minLength": 1},
            }
        ),
        "LedgerCore": obj(
            {
                "request_hash": DIGEST,
                "hash_algorithm": {"enum": ["active-v2", "raw-v1"]},
            }
        ),
        "PredictionVersions": obj(
            {
                "engine": NULLABLE_STRING,
                "model": NULLABLE_STRING,
                "calibration": NULLABLE_STRING,
            }
        ),
        "PredictionInterval": obj(
            {
                "lower": NULLABLE_NUMBER,
                "upper": NULLABLE_NUMBER,
                "nominal_coverage": NULLABLE_NUMBER,
                "calibration_state": NULLABLE_STRING,
                "scope": NULLABLE_STRING,
            }
        ),
        "OptimizerObjective": array(objective_item, minItems=4, maxItems=4),
        "PosteriorOptimizerMetadata": obj(
            {
                "optimizer": {"type": "string"},
                "simulations": {"type": "integer", "minimum": 0},
                "seed": {"type": "integer"},
                "passes": {"type": "integer", "minimum": 0},
                "reason": NULLABLE_STRING,
                "search_strategy": {"type": "string"},
                "objective": ref("OptimizerObjective"),
                "legacy_objective": ref("OptimizerObjective"),
            }
        ),
        "FallbackOptimizerMetadata": obj(
            {
                "optimizer": {"type": "string"},
                "simulations": {"type": "integer", "minimum": 0},
                "seed": {"type": "integer"},
                "passes": {"type": "integer", "minimum": 0},
                "reason": {"type": "string"},
            }
        ),
        "OptimizerMetadata": {
            "oneOf": [ref("PosteriorOptimizerMetadata"), ref("FallbackOptimizerMetadata")]
        },
        "ReceiptPrediction": obj(
            {
                "ordinal": {"type": "integer", "minimum": 0},
                "prediction_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "competitor_id": NAMESPACED_ID,
                "event_code": {"enum": ["SB", "UH"]},
                "median_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 300},
                "assigned_mark": {"type": "integer", "minimum": 3},
                "source": {"type": "string", "minLength": 1},
                "training_eligible": {"type": "boolean"},
                "versions": ref("PredictionVersions"),
                "evidence_cutoff": nullable(DATE),
                "interval": ref("PredictionInterval"),
                "optimizer": NULLABLE_STRING,
                "optimizer_metadata": ref("OptimizerMetadata"),
                "warnings": array({"type": "string"}),
                "ignored_factors": array({"type": "string"}),
            }
        ),
        "ReceiptCore": obj(
            {
                "schema_version": {"const": "strathmark.shadow-receipt-core.v1"},
                "identity_schema_version": {"const": "strathmark.namespaced-identity.v1"},
                "consumer_id": NAMESPACED_ID,
                "tournament_id": NAMESPACED_ID,
                "event_occurrence_id": NAMESPACED_ID,
                "field_run_id": NAMESPACED_ID,
                "operator_id": NAMESPACED_ID,
                "request_id": NAMESPACED_ID,
                "run_revision": NAMESPACED_ID,
                "event_code": {"enum": ["SB", "UH"]},
                "target_contract": {"const": "single-elapsed-seconds.v1"},
                "prediction_as_of": DATE,
                "cutoff_semantics": {"const": "exclusive-utc-date"},
                "request_projection": ref("RequestProjection"),
                "active_input": ref("ActiveInput"),
                "calculation_input": ref("CalculationInput"),
                "observation": ref("Observation"),
                "evidence_snapshot": ref("EvidenceReceiptProjection"),
                "artifact": ref("Artifact"),
                "evidence_diagnostics": array(ref("EvidenceDiagnostic")),
                "ledger": ref("LedgerCore"),
                "created_at": TIMESTAMP,
                "predictions": array(ref("ReceiptPrediction"), minItems=1, maxItems=64),
            }
        ),
        "LiveStatus": obj(
            {
                "trust": {"type": "string"},
                "mirror": {"type": "string"},
                "freshness": {"type": "string"},
                "ready_for_review": {"type": "boolean"},
            }
        ),
        "Receipt": obj(
            {
                "core_json": {"type": "string", "minLength": 2},
                "core": ref("ReceiptCore"),
                "status": ref("LiveStatus"),
            }
        ),
        "DraftPrediction": obj(
            {
                "competitor_id": NAMESPACED_ID,
                "median_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 300},
                "assigned_mark": {"type": "integer", "minimum": 3},
                "prediction_id": NULLABLE_STRING,
                "ledger_status": {"type": "string"},
            }
        ),
        "CalculateResponse": obj(
            {
                "schema_version": {"const": "strathmark.shadow-calculate-response.v1"},
                "trusted": {"type": "boolean"},
                "receipt": nullable(ref("Receipt")),
                "status": ref("LiveStatus"),
                "draft_predictions": array(ref("DraftPrediction"), maxItems=64),
            }
        ),
        "LookupResponse": obj(
            {
                "schema_version": {"const": "strathmark.shadow-receipt-lookup-response.v1"},
                "receipt": ref("Receipt"),
            }
        ),
        "MonitoringStatus": obj(
            {
                "mirror": {"type": "string"},
                "mirror_pending_count": {"type": "integer", "minimum": 0},
                "mirror_oldest_pending_at": NULLABLE_STRING,
                "mirror_last_attempt_at": NULLABLE_STRING,
                "local_trust": {"type": "string"},
                "receipt_freshness": {"type": "string"},
                "receipt_readiness": {"type": "string"},
                "numeric_mirror": {"type": "string"},
                "numeric_mirror_backlog_count": {"type": "integer", "minimum": 0},
                "numeric_mirror_oldest_pending_at": NULLABLE_STRING,
                "numeric_mirror_last_attempt_at": NULLABLE_STRING,
                "numeric_revision_count": {"type": "integer", "minimum": 0},
                "active_numeric_settlement_count": {"type": "integer", "minimum": 0},
                "voided_prediction_count": {"type": "integer", "minimum": 0},
                "evidence_sample_count": {"type": "integer", "minimum": 0},
                "evidence_status": {"type": "string"},
                "drift_calibration_advisory": {"type": "string"},
            }
        ),
        "StatusResponse": obj(
            {
                "schema_version": {"const": "strathmark.shadow-status-response.v1"},
                "status": ref("MonitoringStatus"),
            }
        ),
        "NumericRevisionResult": obj(
            {
                "revision_id": {"type": "string", "minLength": 1},
                "prediction_id": {"type": "string", "minLength": 1},
                "revision": {"type": "integer", "minimum": 1},
                "competitor_id": NAMESPACED_ID,
                "event_code": {"enum": ["SB", "UH"]},
                "action": {"enum": ["settle", "void"]},
                "actual_time": NULLABLE_NUMBER,
                "residual": NULLABLE_NUMBER,
                "supersedes_revision_id": NULLABLE_STRING,
                "created_at": TIMESTAMP,
            }
        ),
        "NumericOutcome": obj(
            {
                "outcome_revision_id": NAMESPACED_ID,
                "ledger_request_id": {"type": "string", "minLength": 1},
                "caller_id": NAMESPACED_ID,
                "revisions": array(ref("NumericRevisionResult"), minItems=1, maxItems=512),
                "actor": NAMESPACED_ID,
                "reason_code": {
                    "type": ["string", "null"],
                    "enum": [
                        "corrected_time",
                        "retract_invalid_numeric_evidence",
                        "valid_replacement",
                        None,
                    ],
                },
                "created_at": TIMESTAMP,
                "status": {"type": "string"},
                "cloud_status": {"type": "string"},
            }
        ),
        "NumericOutcomeResponse": obj(
            {
                "schema_version": {"const": "strathmark.shadow-numeric-outcome-response.v1"},
                "outcome": ref("NumericOutcome"),
            }
        ),
        "MirrorReplaySummary": obj(
            {
                "recorded": {"type": "integer", "minimum": 0},
                "failed": {"type": "integer", "minimum": 0},
                "not_configured": {"type": "integer", "minimum": 0},
            }
        ),
        "MirrorReplayResponse": obj(
            {
                "schema_version": {"const": "strathmark.shadow-mirror-replay-response.v1"},
                "attempted_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "summary": ref("MirrorReplaySummary"),
            }
        ),
        "CoverageCohort": obj(
            {
                "nominal_coverage": {"type": "number", "minimum": 0, "maximum": 1},
                "eligible_count": {"type": "integer", "minimum": 0},
                "covered_count": {"type": "integer", "minimum": 0},
                "empirical_coverage": nullable({"type": "number", "minimum": 0, "maximum": 1}),
                "sample_label": {"type": "string"},
                "coverage_alert": {"type": "boolean"},
            }
        ),
        "CoverageCohortMap": {
            "type": "object",
            "additionalProperties": ref("CoverageCohort"),
        },
        "DriftReport": obj(
            {
                "model_version_id": NULLABLE_STRING,
                "lookback_days": {"type": "integer", "minimum": 1, "maximum": 365},
                "recent_count": {"type": "integer", "minimum": 0},
                "baseline_count": {"type": "integer", "minimum": 0},
                "recent_mean": NULLABLE_NUMBER,
                "baseline_mean": NULLABLE_NUMBER,
                "mean_shift": NULLABLE_NUMBER,
                "recent_variance": NULLABLE_NUMBER,
                "baseline_variance": NULLABLE_NUMBER,
                "variance_ratio_change": NULLABLE_NUMBER,
                "baseline_coverage_at_90": nullable({"type": "number", "minimum": 0, "maximum": 1}),
                "recent_coverage_at_90": nullable({"type": "number", "minimum": 0, "maximum": 1}),
                "coverage_cohorts": ref("CoverageCohortMap"),
                "coverage_unavailable_count": {"type": "integer", "minimum": 0},
                "mean_shift_alert": {"type": "boolean"},
                "variance_ratio_alert": {"type": "boolean"},
                "coverage_alert": {"type": "boolean"},
                "insufficient_recent_samples": {"type": "boolean"},
                "sample_label": {"type": "string"},
                "overall_alert": {"type": "boolean"},
                "notes": array({"type": "string"}),
            }
        ),
        "DriftResponse": obj(
            {
                "schema_version": {"const": "strathmark.shadow-drift-response.v1"},
                "advisory_only": {"const": True},
                "row_limit": {"type": "integer", "minimum": 1},
                "truncated": {"type": "boolean"},
                "report": ref("DriftReport"),
            }
        ),
        "EngineCoreHealth": obj(
            {
                "available": {"type": "boolean"},
                "compatible_with_cutoff": {"type": "boolean"},
                "version": NULLABLE_STRING,
                "serving_active": {"type": "boolean"},
            }
        ),
        "EngineResidualHealth": obj(
            {
                "available": {"type": "boolean"},
                "active": {"type": "boolean"},
                "version": NULLABLE_STRING,
                "serving_active": {"type": "boolean"},
            }
        ),
        "PredictionEngineHealth": obj(
            {
                "core": ref("EngineCoreHealth"),
                "residual": ref("EngineResidualHealth"),
                "calibration": ref("EngineCoreHealth"),
                "cutoff": DATE,
                "source": {"type": "string"},
                "warnings": array({"type": "string"}),
                "degraded": {"type": "boolean"},
                "active_engine": {"type": "string"},
            }
        ),
        "LedgerPersistenceHealth": obj(
            {
                "configured_as_memory": {"type": "boolean"},
                "path_exists": {"type": "boolean"},
                "readable": {"type": "boolean"},
                "writable": {"type": "boolean"},
                "read_write_open_observed": {"type": "boolean"},
                "persistence_observed": {"type": "boolean"},
                "assurance": {"type": "string"},
            }
        ),
        "ActiveEvidenceHealth": obj(
            {
                "schema_version": {"const": "strathmark.evidence-snapshot-health.v1"},
                "state": {"const": "active"},
                "attestation": ref("EvidenceReceiptProjection"),
                "integrity": {"type": "string"},
                "freshness": {"type": "string"},
                "completeness": {"type": "string"},
                "ready_for_offline": {"type": "boolean"},
            }
        ),
        "UnavailableEvidenceHealth": obj(
            {
                "schema_version": {"const": "strathmark.evidence-snapshot-health.v1"},
                "state": {"enum": ["missing", "invalid"]},
                "integrity": {"enum": ["unavailable", "failed"]},
                "ready_for_offline": {"const": False},
            }
        ),
        "EvidenceSnapshotHealth": {
            "oneOf": [ref("ActiveEvidenceHealth"), ref("UnavailableEvidenceHealth")]
        },
        "ShadowServiceHealth": obj(
            {
                "schema_version": {"const": "strathmark.shadow-service-health.v1"},
                "authentication": {"type": "string"},
                "topology": {"type": "string"},
                "topology_claim": NULLABLE_STRING,
                "topology_assurance": {"type": "string"},
                "ledger_persistence": ref("LedgerPersistenceHealth"),
                "evidence_snapshot": ref("EvidenceSnapshotHealth"),
                "ready_for_trusted_shadow": {"type": "boolean"},
                "readiness": {"enum": ["ready", "not-ready"]},
            }
        ),
        "HealthResponse": obj(
            {
                "status": {"const": "ok"},
                "ollama_available": {"type": "boolean"},
                "ollama_model": {"type": "string"},
                "store_available": {"type": "boolean"},
                "store_results_count": {"type": "integer", "minimum": 0},
                "prediction_engine": ref("PredictionEngineHealth"),
                "shadow_service": ref("ShadowServiceHealth"),
            }
        ),
    }


def receipt_core_example() -> dict[str, Any]:
    evidence_input = {
        "schema_version": "strathmark.evidence-snapshot.v1",
        "snapshot_digest": "3" * 64,
        "source_schema_version": "strathmark.evidence-snapshot-source.v1",
        "source_id": "fixture:evidence:001",
        "source_digest": "4" * 64,
        "cutoff": "2026-11-02",
        "cutoff_semantics": "exclusive-utc-date",
        "captured_at": "2026-11-01T10:00:00+00:00",
        "activation_id": "fixture-activation-001",
        "activation_revision": 1,
        "previous_activation_id": None,
        "supersedes_snapshot_digest": None,
        "completeness": "full",
        "supplied_row_count": 3,
        "accepted_row_count": 3,
        "rejected_row_count": 0,
        "diagnostics": {},
    }
    calculation = {
        "event_code": "SB",
        "prediction_as_of": "2026-11-02",
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
        "effective_mark_ceiling": 183,
        "competitors": [
            {
                "competitor_id": "missoula:competitor:001",
                "gender": "F",
                "manual_time_override": None,
                "history": [],
            }
        ],
    }
    request_projection = {
        "schema_version": "strathmark.shadow-request-projection.v1",
        "consumer_id": "missoula:service:shadow",
        "tournament_id": "missoula:tournament:2027",
        "event_occurrence_id": "missoula:event:225-sb",
        "field_run_id": "missoula:field-run:001",
        "operator_id": "missoula:operator:007",
        "request_id": "missoula:request:001",
        "run_revision": "missoula:run-revision:001",
        "event_code": "SB",
        "target_contract": "single-elapsed-seconds.v1",
        "prediction_as_of": "2026-11-02",
        "cutoff_semantics": "exclusive-utc-date",
        "schedule_fingerprint": "1" * 64,
        "observation_schema_version": "strathmark.shadow-observation-fingerprint.v1",
        "observation_fingerprint": "2" * 64,
        "seed": 20260811,
        "competitors": [{"competitor_id": "missoula:competitor:001", "gender": "F"}],
        "wood": {"species": "PINE", "diameter_mm": 300.0, "quality": 5},
        "fingerprint": "5" * 64,
    }
    return {
        "schema_version": "strathmark.shadow-receipt-core.v1",
        "identity_schema_version": "strathmark.namespaced-identity.v1",
        "consumer_id": "missoula:service:shadow",
        "tournament_id": "missoula:tournament:2027",
        "event_occurrence_id": "missoula:event:225-sb",
        "field_run_id": "missoula:field-run:001",
        "operator_id": "missoula:operator:007",
        "request_id": "missoula:request:001",
        "run_revision": "missoula:run-revision:001",
        "event_code": "SB",
        "target_contract": "single-elapsed-seconds.v1",
        "prediction_as_of": "2026-11-02",
        "cutoff_semantics": "exclusive-utc-date",
        "request_projection": request_projection,
        "active_input": {
            "schema_version": "strathmark.shadow-active-input.v1",
            "tournament_id": "missoula:tournament:2027",
            "event_occurrence_id": "missoula:event:225-sb",
            "field_run_id": "missoula:field-run:001",
            "target_contract": "single-elapsed-seconds.v1",
            "schedule_fingerprint": "1" * 64,
            "caller_input": calculation,
            "evidence_snapshot": evidence_input,
            "fingerprint": "6" * 64,
        },
        "calculation_input": calculation,
        "observation": {
            "schema_version": "strathmark.shadow-observation-fingerprint.v1",
            "fingerprint": "2" * 64,
        },
        "evidence_snapshot": {
            **evidence_input,
            "activated_at": "2026-11-01T10:05:00+00:00",
            "age_days_at_calculation": 1,
            "freshness_at_calculation": "current",
            "integrity": "verified",
            "ready_for_offline_at_calculation": True,
        },
        "artifact": {
            "provider_source": "package",
            "source_digest": "7" * 64,
            "artifact_digest": "8" * 64,
            "model_version": "prediction-v2-core-20260207",
            "calibration_version": "prediction-v2-calibration-2025h1",
            "residual_version": None,
        },
        "evidence_diagnostics": [
            {
                "ordinal": 0,
                "competitor_id": "missoula:competitor:001",
                "total_rows": 3,
                "included_rows": 3,
                "excluded_rows": 0,
                "excluded_by_reason": {},
                "canonicalization_version": "prediction-v2-evidence-v1",
            }
        ],
        "ledger": {"request_hash": "9" * 64, "hash_algorithm": "active-v2"},
        "created_at": "2026-11-01T12:00:00+00:00",
        "predictions": [
            {
                "ordinal": 0,
                "prediction_id": "00000000-0000-0000-0000-000000000001",
                "competitor_id": "missoula:competitor:001",
                "event_code": "SB",
                "median_seconds": 42.0,
                "assigned_mark": 3,
                "source": "baseline",
                "training_eligible": True,
                "versions": {
                    "engine": "2.0.0",
                    "model": "prediction-v2-core-20260207",
                    "calibration": "prediction-v2-calibration-2025h1",
                },
                "evidence_cutoff": "2026-11-02",
                "interval": {
                    "lower": 30.0,
                    "upper": 55.0,
                    "nominal_coverage": 0.9,
                    "calibration_state": "analytic",
                    "scope": "analytic",
                },
                "optimizer": "posterior_crn_v2",
                "optimizer_metadata": {
                    "optimizer": "posterior_crn_v2",
                    "simulations": 2048,
                    "seed": 20260811,
                    "passes": 0,
                    "reason": None,
                    "search_strategy": "single_competitor",
                    "objective": [0.0, 0.0, 0, [3]],
                    "legacy_objective": [0.0, 0.0, 0, [3]],
                },
                "warnings": [],
                "ignored_factors": ["division", "venue"],
            }
        ],
    }


def response_examples() -> dict[str, Any]:
    status = {
        "trust": "recorded",
        "mirror": "not-configured",
        "freshness": "current",
        "ready_for_review": True,
    }
    core = receipt_core_example()
    receipt = {
        "core_json": json.dumps(core, sort_keys=True, separators=(",", ":")),
        "core": core,
        "status": status,
    }
    evidence = core["evidence_snapshot"]
    return {
        "/v1/shadow/calculate": {
            "schema_version": "strathmark.shadow-calculate-response.v1",
            "trusted": True,
            "receipt": receipt,
            "status": status,
            "draft_predictions": [],
        },
        "/v1/shadow/receipts/lookup": {
            "schema_version": "strathmark.shadow-receipt-lookup-response.v1",
            "receipt": receipt,
        },
        "/v1/shadow/status": {
            "schema_version": "strathmark.shadow-status-response.v1",
            "status": {
                "mirror": "not-configured",
                "mirror_pending_count": 0,
                "mirror_oldest_pending_at": None,
                "mirror_last_attempt_at": None,
                "local_trust": "recorded",
                "receipt_freshness": "current",
                "receipt_readiness": "ready",
                "numeric_mirror": "not-configured",
                "numeric_mirror_backlog_count": 0,
                "numeric_mirror_oldest_pending_at": None,
                "numeric_mirror_last_attempt_at": None,
                "numeric_revision_count": 0,
                "active_numeric_settlement_count": 0,
                "voided_prediction_count": 0,
                "evidence_sample_count": 0,
                "evidence_status": "insufficient-evidence",
                "drift_calibration_advisory": "insufficient-evidence",
            },
        },
        "/v1/shadow/outcomes/apply": {
            "schema_version": "strathmark.shadow-numeric-outcome-response.v1",
            "outcome": {
                "outcome_revision_id": "missoula:outcome-revision:001",
                "ledger_request_id": "00000000-0000-0000-0000-000000000010",
                "caller_id": "missoula:service:shadow",
                "revisions": [
                    {
                        "revision_id": "00000000-0000-0000-0000-000000000011",
                        "prediction_id": "00000000-0000-0000-0000-000000000001",
                        "revision": 1,
                        "competitor_id": "missoula:competitor:001",
                        "event_code": "SB",
                        "action": "settle",
                        "actual_time": 42.5,
                        "residual": 0.5,
                        "supersedes_revision_id": None,
                        "created_at": "2026-11-01T12:00:00+00:00",
                    }
                ],
                "actor": "missoula:operator:007",
                "reason_code": None,
                "created_at": "2026-11-01T12:00:00+00:00",
                "status": "recorded",
                "cloud_status": "not_configured",
            },
        },
        "/v1/shadow/mirror/replay": {
            "schema_version": "strathmark.shadow-mirror-replay-response.v1",
            "attempted_limit": 25,
            "summary": {"recorded": 0, "failed": 0, "not_configured": 1},
        },
        "/v1/shadow/drift": {
            "schema_version": "strathmark.shadow-drift-response.v1",
            "advisory_only": True,
            "row_limit": 5000,
            "truncated": False,
            "report": {
                "model_version_id": "prediction-v2-core-20260207",
                "lookback_days": 30,
                "recent_count": 0,
                "baseline_count": 3,
                "recent_mean": None,
                "baseline_mean": None,
                "mean_shift": None,
                "recent_variance": None,
                "baseline_variance": None,
                "variance_ratio_change": None,
                "baseline_coverage_at_90": 0.9,
                "recent_coverage_at_90": None,
                "coverage_cohorts": {},
                "coverage_unavailable_count": 0,
                "mean_shift_alert": False,
                "variance_ratio_alert": False,
                "coverage_alert": False,
                "insufficient_recent_samples": True,
                "sample_label": "insufficient_recent_sample",
                "overall_alert": False,
                "notes": ["insufficient evidence"],
            },
        },
        "/health": {
            "status": "ok",
            "ollama_available": False,
            "ollama_model": "local-model",
            "store_available": True,
            "store_results_count": 0,
            "prediction_engine": {
                "core": {
                    "available": True,
                    "compatible_with_cutoff": True,
                    "version": "prediction-v2-core-20260207",
                    "serving_active": True,
                },
                "residual": {
                    "available": False,
                    "active": False,
                    "version": None,
                    "serving_active": True,
                },
                "calibration": {
                    "available": True,
                    "compatible_with_cutoff": True,
                    "version": "prediction-v2-calibration-2025h1",
                    "serving_active": True,
                },
                "cutoff": "2026-11-02",
                "source": "package",
                "warnings": [],
                "degraded": False,
                "active_engine": "v2",
            },
            "shadow_service": {
                "schema_version": "strathmark.shadow-service-health.v1",
                "authentication": "configured",
                "topology": "operator-attested-unverified",
                "topology_claim": "offline-single-writer-durable",
                "topology_assurance": "operator-attested-not-infrastructure-proven",
                "ledger_persistence": {
                    "configured_as_memory": False,
                    "path_exists": True,
                    "readable": True,
                    "writable": True,
                    "read_write_open_observed": True,
                    "persistence_observed": True,
                    "assurance": "sqlite-read-write-observed-not-durability-proof",
                },
                "evidence_snapshot": {
                    "schema_version": "strathmark.evidence-snapshot-health.v1",
                    "state": "active",
                    "attestation": evidence,
                    "integrity": "verified",
                    "freshness": "current",
                    "completeness": "full",
                    "ready_for_offline": True,
                },
                "ready_for_trusted_shadow": True,
                "readiness": "ready",
            },
        },
    }


def request_schemas() -> dict[str, Any]:
    """Return the reviewed request-side contract source.

    This is deliberately independent of the generated OpenAPI artifact.  The
    freezer must be able to recreate that artifact in a clean checkout and
    must never carry unknown fields forward from an edited output file.
    """
    namespaced_id = {
        "type": "string",
        "minLength": 3,
        "maxLength": 128,
        "pattern": r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$",
    }
    digest = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
    schemas = {
        "NamespacedId": namespaced_id,
        "Digest": digest,
        "Competitor": obj(
            {
                "competitor_id": NAMESPACED_ID,
                "gender": {"type": ["string", "null"], "enum": ["M", "F", None]},
            },
            required=["competitor_id"],
        ),
        "Wood": obj(
            {
                "species": {"type": "string", "minLength": 1, "maxLength": 100},
                "diameter_mm": {"type": "number", "minimum": 225, "maximum": 500},
                "quality": {"type": "integer", "minimum": 1, "maximum": 10},
            }
        ),
        "CalculateRequest": obj(
            {
                "schema_version": {"const": "strathmark.shadow-calculate.v1"},
                "consumer_id": NAMESPACED_ID,
                "tournament_id": NAMESPACED_ID,
                "event_occurrence_id": NAMESPACED_ID,
                "field_run_id": NAMESPACED_ID,
                "operator_id": NAMESPACED_ID,
                "request_id": NAMESPACED_ID,
                "run_revision": NAMESPACED_ID,
                "event_code": {"enum": ["SB", "UH"]},
                "target_contract": {"const": "single-elapsed-seconds.v1"},
                "prediction_as_of": DATE,
                "schedule_fingerprint": DIGEST,
                "observation_schema_version": {
                    "const": "strathmark.shadow-observation-fingerprint.v1"
                },
                "observation_fingerprint": DIGEST,
                "competitors": array(ref("Competitor"), minItems=1, maxItems=64),
                "wood": ref("Wood"),
                "seed": {"type": "integer", "default": 20260811},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 25,
                    "maximum": 10_000,
                    "default": 5000,
                },
            },
            required=[
                "schema_version",
                "consumer_id",
                "tournament_id",
                "event_occurrence_id",
                "field_run_id",
                "operator_id",
                "request_id",
                "run_revision",
                "event_code",
                "target_contract",
                "prediction_as_of",
                "schedule_fingerprint",
                "observation_schema_version",
                "observation_fingerprint",
                "competitors",
                "wood",
            ],
        ),
        "LookupRequest": obj(
            {
                "schema_version": {"const": "strathmark.shadow-receipt-lookup.v1"},
                "consumer_id": NAMESPACED_ID,
                "request_id": NAMESPACED_ID,
                "run_revision": NAMESPACED_ID,
                "current_active_fingerprint": nullable(DIGEST),
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 25,
                    "maximum": 10_000,
                    "default": 2000,
                },
            },
            required=["schema_version", "consumer_id", "request_id", "run_revision"],
        ),
        "StatusRequest": obj(
            {
                "schema_version": {"const": "strathmark.shadow-status.v1"},
                "consumer_id": NAMESPACED_ID,
                "request_id": NAMESPACED_ID,
                "run_revision": NAMESPACED_ID,
                "current_active_fingerprint": nullable(DIGEST),
                "model_version": {"type": ["string", "null"], "maxLength": 128},
                "timeout_ms": {"type": "integer", "minimum": 25, "maximum": 10_000},
            },
            required=["schema_version", "consumer_id", "request_id", "run_revision"],
        ),
        "NumericOutcomeRequest": obj(
            {
                "schema_version": {"const": "strathmark.shadow-numeric-outcome.v1"},
                "consumer_id": NAMESPACED_ID,
                "request_id": NAMESPACED_ID,
                "run_revision": NAMESPACED_ID,
                "outcome_revision_id": NAMESPACED_ID,
                "reason_code": {
                    "type": ["string", "null"],
                    "enum": [
                        "corrected_time",
                        "retract_invalid_numeric_evidence",
                        "valid_replacement",
                        None,
                    ],
                },
                "revisions": array(ref("NumericRevision"), minItems=1, maxItems=512),
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 25,
                    "maximum": 10_000,
                    "default": 5000,
                },
            },
            required=[
                "schema_version",
                "consumer_id",
                "request_id",
                "run_revision",
                "outcome_revision_id",
                "revisions",
            ],
        ),
        "MirrorReplayRequest": obj(
            {
                "schema_version": {"const": "strathmark.shadow-mirror-replay.v1"},
                "consumer_id": NAMESPACED_ID,
                "run_revision": NAMESPACED_ID,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "timeout_ms": {"type": "integer", "minimum": 25, "maximum": 10_000},
            },
            required=["schema_version", "consumer_id", "run_revision"],
        ),
        "DriftRequest": obj(
            {
                "schema_version": {"const": "strathmark.shadow-drift.v1"},
                "consumer_id": NAMESPACED_ID,
                "run_revision": NAMESPACED_ID,
                "model_version": {"type": "string", "minLength": 1, "maxLength": 128},
                "lookback_days": {"type": "integer", "minimum": 1, "maximum": 365},
                "baseline_residuals": array(
                    {"type": "number", "minimum": -300, "maximum": 300},
                    minItems=1,
                    maxItems=5000,
                ),
                "timeout_ms": {"type": "integer", "minimum": 25, "maximum": 10_000},
            },
            required=[
                "schema_version",
                "consumer_id",
                "run_revision",
                "model_version",
                "baseline_residuals",
            ],
        ),
    }
    return schemas


def request_examples() -> dict[str, Any]:
    return {
        "/v1/shadow/calculate": {
            "schema_version": "strathmark.shadow-calculate.v1",
            "consumer_id": "missoula:service:shadow",
            "tournament_id": "missoula:tournament:2027",
            "event_occurrence_id": "missoula:event:225-sb",
            "field_run_id": "missoula:field-run:001",
            "operator_id": "missoula:operator:007",
            "request_id": "missoula:request:001",
            "run_revision": "missoula:run-revision:001",
            "event_code": "SB",
            "target_contract": "single-elapsed-seconds.v1",
            "prediction_as_of": "2026-11-01",
            "schedule_fingerprint": "1" * 64,
            "observation_schema_version": "strathmark.shadow-observation-fingerprint.v1",
            "observation_fingerprint": "2" * 64,
            "competitors": [
                {"competitor_id": "missoula:competitor:001", "gender": "F"},
                {"competitor_id": "missoula:competitor:002", "gender": "M"},
            ],
            "wood": {"species": "PINE", "diameter_mm": 300, "quality": 5},
            "seed": 20260811,
            "timeout_ms": 5000,
        },
        "/v1/shadow/receipts/lookup": {
            "schema_version": "strathmark.shadow-receipt-lookup.v1",
            "consumer_id": "missoula:service:shadow",
            "request_id": "missoula:request:001",
            "run_revision": "missoula:run-revision:001",
            "current_active_fingerprint": None,
        },
        "/v1/shadow/status": {
            "schema_version": "strathmark.shadow-status.v1",
            "consumer_id": "missoula:service:shadow",
            "request_id": "missoula:request:001",
            "run_revision": "missoula:run-revision:001",
            "current_active_fingerprint": None,
            "model_version": None,
            "timeout_ms": 2000,
        },
        "/v1/shadow/outcomes/apply": {
            "schema_version": "strathmark.shadow-numeric-outcome.v1",
            "consumer_id": "missoula:service:shadow",
            "request_id": "missoula:request:001",
            "run_revision": "missoula:run-revision:001",
            "outcome_revision_id": "missoula:outcome-revision:001",
            "reason_code": None,
            "revisions": [
                {
                    "prediction_id": "00000000-0000-0000-0000-000000000001",
                    "competitor_id": "missoula:competitor:001",
                    "event_code": "SB",
                    "action": "settle",
                    "actual_time": 42.5,
                    "expected_revision": 0,
                }
            ],
        },
        "/v1/shadow/mirror/replay": {
            "schema_version": "strathmark.shadow-mirror-replay.v1",
            "consumer_id": "missoula:service:shadow",
            "run_revision": "missoula:run-revision:001",
            "limit": 25,
            "timeout_ms": 5000,
        },
        "/v1/shadow/drift": {
            "schema_version": "strathmark.shadow-drift.v1",
            "consumer_id": "missoula:service:shadow",
            "run_revision": "missoula:run-revision:001",
            "model_version": "v2-core-2026-08",
            "lookback_days": 30,
            "baseline_residuals": [-0.5, 0.0, 0.5],
            "timeout_ms": 5000,
        },
    }


def source_document() -> dict[str, Any]:
    """Build the complete contract from reviewed code, never generated output."""
    schemas = request_schemas()
    schemas.update(response_schemas())
    descriptions = {
        "/health": "Current service and trusted-shadow readiness",
        "/v1/shadow/calculate": "Immutable receipt and current live projection",
        "/v1/shadow/receipts/lookup": "Exact persisted core with a current live projection",
        "/v1/shadow/status": (
            "Payload-free current trust, freshness, mirror, numeric and drift axes"
        ),
        "/v1/shadow/outcomes/apply": "Field-atomic append-only numeric revision",
        "/v1/shadow/mirror/replay": "Bounded off-path replay summary",
        "/v1/shadow/drift": "Bounded advisory-only drift report",
    }
    operations = {
        "/v1/shadow/calculate": ("calculateOrRecoverShadowField", "CalculateRequest"),
        "/v1/shadow/receipts/lookup": ("lookupShadowReceipt", "LookupRequest"),
        "/v1/shadow/status": ("readShadowStatus", "StatusRequest"),
        "/v1/shadow/outcomes/apply": ("applyNumericOutcomeRevision", "NumericOutcomeRequest"),
        "/v1/shadow/mirror/replay": ("replayShadowMirror", "MirrorReplayRequest"),
        "/v1/shadow/drift": ("readShadowDrift", "DriftRequest"),
    }
    response_names = {
        "/health": "HealthResponse",
        "/v1/shadow/calculate": "CalculateResponse",
        "/v1/shadow/receipts/lookup": "LookupResponse",
        "/v1/shadow/status": "StatusResponse",
        "/v1/shadow/outcomes/apply": "NumericOutcomeResponse",
        "/v1/shadow/mirror/replay": "MirrorReplayResponse",
        "/v1/shadow/drift": "DriftResponse",
    }
    examples = response_examples()
    paths: dict[str, Any] = {
        "/health": {
            "get": {
                "operationId": "readServiceHealth",
                "parameters": [
                    {
                        "in": "query",
                        "name": "prediction_as_of",
                        "required": False,
                        "schema": DATE,
                    }
                ],
                "responses": {
                    "200": {
                        "description": descriptions["/health"],
                        "content": {
                            "application/json": {
                                "schema": ref(response_names["/health"]),
                                "example": examples["/health"],
                            }
                        },
                    }
                },
            }
        }
    }
    requests = request_examples()
    for path, (operation_id, request_name) in operations.items():
        paths[path] = {
            "post": {
                "operationId": operation_id,
                "security": [{"serviceBearer": [], "actorAttestation": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": ref(request_name),
                            "example": requests[path],
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": descriptions[path],
                        "content": {
                            "application/json": {
                                "schema": ref(response_names[path]),
                                "example": examples[path],
                            }
                        },
                    }
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "STRATHMARK Trusted Shadow Consumer Contract",
            "version": "1.1.0",
            "x-strathmark-contract-version": "strathmark.shadow-consumer-contract.v1",
        },
        "servers": [{"url": "http://127.0.0.1:8000"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "serviceBearer": {"type": "http", "scheme": "bearer"},
                "actorAttestation": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-STRATHMARK-Actor-Attestation",
                    "description": "",
                },
            },
            "schemas": schemas,
        },
    }


def main() -> int:
    document = source_document()
    schemas = document["components"]["schemas"]
    schemas["Wood"]["properties"]["diameter_mm"].update({"minimum": 225, "maximum": 500})
    revision_identity = {
        "prediction_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "competitor_id": NAMESPACED_ID,
        "event_code": {"enum": ["SB", "UH"]},
        "expected_revision": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
    }
    schemas["NumericSettleRevision"] = obj(
        {
            **revision_identity,
            "action": {"const": "settle"},
            "actual_time": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 300,
            },
        }
    )
    schemas["NumericVoidRevision"] = obj(
        {
            **revision_identity,
            "action": {"const": "void"},
            "actual_time": {"type": "null"},
        },
        required=[
            "prediction_id",
            "competitor_id",
            "event_code",
            "action",
            "expected_revision",
        ],
    )
    schemas["NumericRevision"] = {
        "oneOf": [ref("NumericSettleRevision"), ref("NumericVoidRevision")],
        "discriminator": {
            "propertyName": "action",
            "mapping": {
                "settle": "#/components/schemas/NumericSettleRevision",
                "void": "#/components/schemas/NumericVoidRevision",
            },
        },
    }
    schemas["NumericOutcomeRequest"]["allOf"] = [
        {
            "if": {
                "properties": {
                    "revisions": {
                        "contains": {
                            "anyOf": [
                                ref("NumericVoidRevision"),
                                {
                                    "type": "object",
                                    "properties": {
                                        "expected_revision": {
                                            "type": "integer",
                                            "minimum": 1,
                                        }
                                    },
                                    "required": ["expected_revision"],
                                },
                            ]
                        }
                    },
                },
                "required": ["revisions"],
            },
            "then": {
                "properties": {
                    "reason_code": {
                        "enum": [
                            "corrected_time",
                            "retract_invalid_numeric_evidence",
                            "valid_replacement",
                        ]
                    }
                },
                "required": ["reason_code"],
            },
        }
    ]
    schemas["NumericOutcomeRequest"]["properties"]["timeout_ms"] = {
        "type": "integer",
        "minimum": 25,
        "maximum": 10_000,
        "default": 5000,
    }
    schemas["LookupRequest"]["properties"]["timeout_ms"] = {
        "type": "integer",
        "minimum": 25,
        "maximum": 10_000,
        "default": 2000,
    }
    schemas["DriftRequest"]["properties"]["baseline_residuals"]["items"].update(
        {"minimum": -300, "maximum": 300}
    )
    schemas.update(response_schemas())
    document["info"]["version"] = "1.1.0"
    document["info"]["x-strathmark-actor-attestation-schema"] = ACTOR_ATTESTATION_SCHEMA_VERSION
    document["info"]["x-strathmark-request-digest-schema"] = REQUEST_DIGEST_SCHEMA_VERSION
    role_actions: dict[str, list[str]] = {}
    for action, roles in SHADOW_ACTION_ROLES.items():
        for role in roles:
            role_actions.setdefault(role, []).append(action)
    document["info"]["x-strathmark-role-actions"] = {
        role: sorted(actions) for role, actions in sorted(role_actions.items())
    }
    document["components"]["securitySchemes"]["actorAttestation"]["description"] = (
        "Canonical compact HMAC assertion whose decoded claims validate against "
        "ActorAttestationClaims. The required request_digest binds the action and "
        "subject run revision to the exact validated request body before nonce claim."
    )
    actions = {
        "/v1/shadow/calculate": "shadow.calculate",
        "/v1/shadow/receipts/lookup": "shadow.receipt.lookup",
        "/v1/shadow/status": "shadow.status.read",
        "/v1/shadow/outcomes/apply": "shadow.outcome.apply",
        "/v1/shadow/mirror/replay": "shadow.mirror.replay",
        "/v1/shadow/drift": "shadow.drift.read",
    }
    error_response = {
        "description": "Closed payload-free error response",
        "content": {
            "application/json": {
                "schema": ref("ErrorResponse"),
                "example": {"detail": "Request failed schema validation."},
            }
        },
    }
    for path, action in actions.items():
        operation = document["paths"][path]["post"]
        operation["x-strathmark-action"] = action
        operation["x-strathmark-subject-revision-field"] = "run_revision"
        operation["x-strathmark-request-digest-schema"] = REQUEST_DIGEST_SCHEMA_VERSION
        for status_code in (
            "400",
            "401",
            "403",
            "404",
            "409",
            "411",
            "413",
            "422",
            "429",
            "503",
            "504",
        ):
            operation["responses"][status_code] = error_response
    document["paths"]["/health"]["get"]["responses"]["422"] = error_response
    for path, example in response_examples().items():
        method = "get" if path == "/health" else "post"
        document["paths"][path][method]["responses"]["200"]["content"]["application/json"][
            "example"
        ] = example
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    CONTRACT.write_bytes(payload)
    CHECKSUM.write_text(hashlib.sha256(payload).hexdigest() + "\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
