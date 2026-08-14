"""
STRATHMARK HTTP REST API
=========================

FastAPI server exposing STRATHMARK's handicap engine over HTTP.

Designed for future projects (web apps, mobile, non-Python consumers)
that cannot use the Python import API directly.

Start the server:
    pip install strathmark[api]
    uvicorn strathmark.api:app --host 0.0.0.0 --port 8000

STRATHEX (Python) uses the direct import API (zero overhead).
Other projects use this HTTP API.

Endpoints:
    GET  /health                   -- Check Ollama and store availability
    POST /calculate                -- Compute handicap marks for a field
    POST /predict                  -- Get all predictions for one competitor
    POST /simulate                 -- Run Monte Carlo fairness simulation
    POST /results                  -- Record a tournament result to the store
    GET  /results/{competitor}     -- Get competitor history from the store

Optional dependency:
    pip install fastapi uvicorn[standard]
"""

from __future__ import annotations

import hmac
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from pydantic import BaseModel, ConfigDict, Field
    from starlette.responses import JSONResponse

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

from strathmark import __version__
from strathmark.auth import (
    ShadowAttestationReplayError,
    ShadowAuthenticationConfigurationError,
    ShadowAuthenticationError,
    ShadowAuthorizationError,
    preauthenticate_shadow_service,
    shadow_auth_configuration_status,
    verify_shadow_action,
)
from strathmark.calculator import HandicapCalculator
from strathmark.config import data_req, llm_config, prediction_config, rules, sim_config
from strathmark.drift import MAX_DRIFT_ROWS, DriftRowLimitError, evaluate_drift
from strathmark.ledger import (
    MAX_NUMERIC_RAW_TIME_SECONDS,
    LedgerConflictError,
    LedgerQueryTimeoutError,
    PredictionLedger,
    SettlementConflictError,
    SQLiteQueryDeadline,
)
from strathmark.llm import check_ollama_connection
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    PredictionContext,
    PredictionEngineProvider,
    PredictionInterval,
    WoodProfile,
    get_all_predictions,
    get_prediction_provider,
    select_best_prediction,
)
from strathmark.shadow import (
    OBSERVATION_SCHEMA_VERSION,
    SHADOW_TARGET_SINGLE_ELAPSED,
    ShadowFieldRequest,
    ShadowPredictionService,
    ShadowReceiptCorruptionError,
)
from strathmark.store import EvidenceSnapshotIntegrityError, ResultStore
from strathmark.variance import run_monte_carlo_simulation

# ---------------------------------------------------------------------------
# Raise a helpful error if FastAPI is not installed
# ---------------------------------------------------------------------------

if not _FASTAPI_AVAILABLE:
    raise ImportError(
        "FastAPI and uvicorn are required to run the STRATHMARK HTTP API.\n"
        "Install them with: pip install strathmark[api]\n"
        "  or: pip install fastapi uvicorn[standard]"
    )


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class HistoricalResultSchema(BaseModel):
    event_code: Literal["SB", "UH"]
    time_seconds: float = Field(
        ge=rules.MIN_MARK_SECONDS,
        le=rules.MAX_TIME_LIMIT_SECONDS,
        description="Time in seconds (3-180)",
    )
    species: str = Field(min_length=1, max_length=100)
    diameter_mm: float = Field(
        ge=data_req.MIN_DIAMETER_MM,
        le=data_req.MAX_DIAMETER_MM,
        description="Log diameter in mm (225-500)",
    )
    quality: int = Field(ge=1, le=10, description="Wood quality 1-10")
    result_date: Optional[date] = None
    heat_id: Optional[str] = Field(default=None, max_length=100)


class CompetitorSchema(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    history: List[HistoricalResultSchema] = Field(default_factory=list)
    division: Optional[str] = Field(default=None, max_length=100)
    manual_time_override: Optional[float] = Field(
        default=None, ge=rules.MIN_MARK_SECONDS, le=rules.MAX_TIME_LIMIT_SECONDS
    )
    tournament_time: Optional[float] = Field(
        default=None, ge=rules.MIN_MARK_SECONDS, le=rules.MAX_TIME_LIMIT_SECONDS
    )
    competitor_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    gender: Optional[Literal["M", "F"]] = None


class WoodSchema(BaseModel):
    species: str = Field(min_length=1, max_length=100)
    diameter_mm: float = Field(
        ge=data_req.MIN_DIAMETER_MM,
        le=data_req.MAX_DIAMETER_MM,
        description="Log diameter in mm (225-500)",
    )
    quality: int = Field(ge=1, le=10, description="Wood quality 1-10")


class CalculateRequest(BaseModel):
    competitors: List[CompetitorSchema] = Field(max_length=64)
    wood: WoodSchema
    event_code: Literal["SB", "UH"]
    tournament_results: Optional[Dict[str, float]] = None
    manual_overrides: Optional[Dict[str, float]] = None
    prediction_as_of: Optional[date] = None


class LedgerCalculateRequest(CalculateRequest):
    request_id: str = Field(min_length=1, max_length=128)


class PredictionIntervalResponse(BaseModel):
    lower: float
    upper: float
    nominal_coverage: float
    calibration_state: str
    scope: str


class MarkResultResponse(BaseModel):
    name: str
    mark: int
    predicted_time: float
    method_used: str
    confidence: str
    explanation: str
    std_dev: Optional[float] = None
    competitor_id: Optional[str] = None
    interval: Optional[PredictionIntervalResponse] = None
    engine_version: Optional[str] = None
    model_version: Optional[str] = None
    calibration_version: Optional[str] = None
    evidence_cutoff: Optional[date] = None
    optimizer: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    prediction_id: Optional[str] = None
    ledger_recorded: Optional[bool] = None
    degraded: bool = False
    optimizer_metadata: Dict[str, Any] = Field(default_factory=dict)
    ledger_status: Optional[str] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)
    ignored_factors: List[str] = Field(default_factory=list)


class PredictRequest(BaseModel):
    competitor: CompetitorSchema
    wood: WoodSchema
    event_code: Literal["SB", "UH"]
    prediction_as_of: Optional[date] = None


class PredictResponse(BaseModel):
    best: MarkResultResponse
    all_predictions: Dict[str, Optional[Dict[str, Any]]]


class SimulationCompetitorSchema(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mark: int = Field(ge=rules.MIN_MARK_SECONDS, le=rules.MAX_MARK_SECONDS)
    predicted_time: float = Field(gt=0, le=rules.MAX_TIME_LIMIT_SECONDS)
    std_dev: Optional[float] = Field(default=None, gt=0, le=sim_config.MAX_COMPETITOR_STD_SECONDS)
    performance_std_dev: Optional[float] = Field(
        default=None, gt=0, le=sim_config.MAX_COMPETITOR_STD_SECONDS
    )
    variance: Optional[float] = Field(default=None, gt=0, le=sim_config.MAX_COMPETITOR_STD_SECONDS)


class SimulateRequest(BaseModel):
    competitors: List[SimulationCompetitorSchema] = Field(min_length=2, max_length=64)
    num_simulations: int = Field(default=250_000, ge=1, le=250_000)
    track_finish_orders: bool = False
    track_podium_margins: bool = False


class RecordResultRequest(HistoricalResultSchema):
    competitor_name: str = Field(min_length=1, max_length=100)
    competition_id: str = Field(min_length=1, max_length=128)


class RecordResultResponse(BaseModel):
    inserted: bool
    message: str


class SettlePredictionRequest(BaseModel):
    competitor_id: str = Field(min_length=1, max_length=128)
    event_code: Literal["SB", "UH"]
    actual_time: float = Field(
        ge=rules.MIN_MARK_SECONDS,
        le=rules.MAX_TIME_LIMIT_SECONDS,
    )
    reason: Optional[str] = Field(default=None, max_length=500)


class SettlementResponse(BaseModel):
    settlement_id: str
    prediction_id: str
    revision: int
    actual_time: float
    residual: float
    actor: str
    reason: Optional[str] = None
    supersedes_settlement_id: Optional[str] = None
    settled_at: str
    status: str
    cloud_status: str


class StrictShadowSchema(BaseModel):
    """External trusted contracts reject unreviewed properties."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


_NAMESPACED_ID_PATTERN = r"^[a-z][a-z0-9_.-]{0,31}:[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,94}$"


class TrustedCompetitorSchema(StrictShadowSchema):
    competitor_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    gender: Optional[Literal["M", "F"]] = None


class TrustedWoodSchema(StrictShadowSchema):
    species: str = Field(min_length=1, max_length=100)
    diameter_mm: float = Field(ge=data_req.MIN_DIAMETER_MM, le=data_req.MAX_DIAMETER_MM)
    quality: int = Field(ge=1, le=10)


class ShadowCalculateRequest(StrictShadowSchema):
    schema_version: Literal["strathmark.shadow-calculate.v1"]
    consumer_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    tournament_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    event_occurrence_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    field_run_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    operator_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    request_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    run_revision: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    event_code: Literal["SB", "UH"]
    target_contract: Literal[SHADOW_TARGET_SINGLE_ELAPSED]
    prediction_as_of: date
    schedule_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_schema_version: Literal[OBSERVATION_SCHEMA_VERSION]
    observation_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    competitors: List[TrustedCompetitorSchema] = Field(min_length=1, max_length=64)
    wood: TrustedWoodSchema
    seed: int = 20260811
    timeout_ms: int = Field(default=5000, ge=25, le=10_000)


class ShadowReceiptLookupRequest(StrictShadowSchema):
    schema_version: Literal["strathmark.shadow-receipt-lookup.v1"]
    consumer_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    request_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    run_revision: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    current_active_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ShadowStatusRequest(StrictShadowSchema):
    schema_version: Literal["strathmark.shadow-status.v1"]
    consumer_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    request_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    run_revision: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    current_active_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_version: Optional[str] = Field(default=None, max_length=128)
    timeout_ms: int = Field(default=2000, ge=25, le=10_000)


class NumericSettlementRevisionSchema(StrictShadowSchema):
    prediction_id: str = Field(min_length=1, max_length=128)
    competitor_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    event_code: Literal["SB", "UH"]
    action: Literal["settle", "void"]
    actual_time: Optional[float] = Field(default=None, gt=0, le=MAX_NUMERIC_RAW_TIME_SECONDS)
    expected_revision: int = Field(ge=0, le=1_000_000)


class ShadowNumericOutcomeRequest(StrictShadowSchema):
    schema_version: Literal["strathmark.shadow-numeric-outcome.v1"]
    consumer_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    request_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    run_revision: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    outcome_revision_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    reason_code: Optional[
        Literal["corrected_time", "retract_invalid_numeric_evidence", "valid_replacement"]
    ] = None
    revisions: List[NumericSettlementRevisionSchema] = Field(min_length=1, max_length=512)


class ShadowMirrorReplayRequest(StrictShadowSchema):
    schema_version: Literal["strathmark.shadow-mirror-replay.v1"]
    consumer_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    run_revision: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    limit: int = Field(default=25, ge=1, le=100)
    timeout_ms: int = Field(default=5000, ge=25, le=10_000)


class ShadowDriftRequest(StrictShadowSchema):
    schema_version: Literal["strathmark.shadow-drift.v1"]
    consumer_id: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    run_revision: str = Field(min_length=3, max_length=128, pattern=_NAMESPACED_ID_PATTERN)
    model_version: str = Field(min_length=1, max_length=128)
    lookback_days: int = Field(default=30, ge=1, le=365)
    baseline_residuals: List[float] = Field(min_length=1, max_length=5000)
    timeout_ms: int = Field(default=5000, ge=25, le=10_000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_store: Optional[ResultStore] = None
_ledger: Optional[PredictionLedger] = None
_MAX_SHADOW_BODY_BYTES = 262_144
_SHADOW_OPERATION_SLOTS = threading.BoundedSemaphore(value=2)
_SHADOW_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="strathmark-shadow")
# The vectorized simulator holds several float/int matrices concurrently. Keep
# one bounded request below a conservative worker-memory envelope until it is
# redesigned to aggregate fixed-size batches.
_MAX_SIMULATION_CELLS = 4_000_000
_SIMULATION_SLOTS = threading.BoundedSemaphore(value=1)


def get_store() -> ResultStore:
    """Lazily create the default store so imports never touch user data."""
    global _store
    if _store is None:
        _store = ResultStore()
    return _store


def get_ledger() -> PredictionLedger:
    """Lazily open the additive local ledger; imports never touch user data."""

    global _ledger
    if _ledger is None:
        mirror = None
        if os.environ.get("STRATHMARK_SUPABASE_URL") and os.environ.get("STRATHMARK_SUPABASE_KEY"):
            from strathmark.db import mirror_prediction_ledger

            mirror = mirror_prediction_ledger
        _ledger = PredictionLedger(mirror=mirror)
    return _ledger


def get_shadow_service(
    ledger: PredictionLedger = Depends(get_ledger),
    store: ResultStore = Depends(get_store),
    prediction_provider: PredictionEngineProvider = Depends(get_prediction_provider),
) -> ShadowPredictionService:
    """Construct the thin recovery-first facade over the shared durable ledger."""

    return ShadowPredictionService(
        ledger,
        prediction_provider=prediction_provider,
        result_store=store,
    )


def require_shadow_body_limit(request: Request) -> None:
    """Reject declared oversized trusted bodies before model or ledger work."""

    raw_length = request.headers.get("content-length")
    if raw_length is None:
        raise HTTPException(
            status_code=411,
            detail="Trusted shadow requests require a bounded Content-Length header.",
        )
    try:
        length = int(raw_length)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Content-Length header.")
    if length < 0 or length > _MAX_SHADOW_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Trusted shadow request body must not exceed {_MAX_SHADOW_BODY_BYTES} bytes.",
        )


def _authorize_shadow_action(
    *,
    authorization: Optional[str],
    actor_attestation: Optional[str],
    consumer_id: str,
    actor_id: Optional[str],
    action: str,
    subject_revision: str,
    ledger: PredictionLedger,
) -> Any:
    try:
        return verify_shadow_action(
            authorization=authorization,
            encoded_attestation=actor_attestation,
            expected_consumer_id=consumer_id,
            expected_actor_id=actor_id,
            expected_action=action,
            expected_subject_revision=subject_revision,
            ledger=ledger,
        )
    except ShadowAuthenticationConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ShadowAttestationReplayError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ShadowAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ShadowAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _receipt_response(receipt: Any) -> Optional[Dict[str, Any]]:
    if receipt is None:
        return None
    return {
        "core_json": receipt.core_json,
        "core": dict(receipt.core),
        "status": asdict(receipt.status),
    }


def _run_bounded_shadow_calculation(
    service: ShadowPredictionService,
    request: ShadowFieldRequest,
    competitors: List[CompetitorRecord],
    wood: WoodProfile,
    *,
    timeout_ms: int,
) -> Any:
    if not _SHADOW_OPERATION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Trusted shadow calculation capacity is busy. Retry by receipt lookup first.",
        )
    future = _SHADOW_EXECUTOR.submit(service.calculate, request, competitors, wood)
    future.add_done_callback(lambda unused: _SHADOW_OPERATION_SLOTS.release())
    try:
        return future.result(timeout=timeout_ms / 1000.0)
    except FutureTimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                "Trusted calculation outcome is unknown after timeout. "
                "Recover by receipt lookup before retrying calculation."
            ),
        )


def _run_bounded_shadow_read(
    operation: Any,
    *,
    timeout_ms: int,
    timeout_detail: str,
) -> Any:
    """Run one cooperative SQLite read and reclaim capacity before timeout response."""

    if not _SHADOW_OPERATION_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Trusted shadow operation capacity is busy.")
    query_deadline = SQLiteQueryDeadline(timeout_seconds=timeout_ms / 1000.0)

    def invoke() -> Any:
        try:
            return operation(query_deadline)
        finally:
            _SHADOW_OPERATION_SLOTS.release()

    future = _SHADOW_EXECUTOR.submit(invoke)
    try:
        return future.result(timeout=timeout_ms / 1000.0)
    except (FutureTimeoutError, LedgerQueryTimeoutError) as exc:
        query_deadline.cancel()
        try:
            future.result(timeout=0.5)
        except (FutureTimeoutError, LedgerQueryTimeoutError, sqlite3.OperationalError):
            pass
        raise HTTPException(status_code=504, detail=timeout_detail) from exc
    except sqlite3.OperationalError as exc:
        if query_deadline.cancelled:
            raise HTTPException(status_code=504, detail=timeout_detail) from exc
        raise


def require_results_token(authorization: Optional[str] = Header(default=None)) -> None:
    """Protect persisted athlete histories when the API is deployed remotely."""
    expected_token = os.environ.get("STRATHMARK_API_TOKEN", "")
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Results endpoints are disabled until STRATHMARK_API_TOKEN is configured.",
        )
    supplied_token = (authorization or "").removeprefix("Bearer ")
    if not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(
            status_code=401, detail="Valid bearer token required for results endpoints."
        )


def _to_competitor_record(schema: CompetitorSchema) -> CompetitorRecord:
    history = [
        HistoricalResult(
            event_code=h.event_code,
            time_seconds=h.time_seconds,
            species=h.species,
            diameter_mm=h.diameter_mm,
            quality=h.quality,
            result_date=h.result_date,
            heat_id=h.heat_id,
        )
        for h in schema.history
    ]
    return CompetitorRecord(
        name=schema.name,
        history=history,
        division=schema.division,
        manual_time_override=getattr(schema, "manual_time_override", None),
        tournament_time=getattr(schema, "tournament_time", None),
        competitor_id=schema.competitor_id,
        gender=schema.gender,
    )


def _to_wood_profile(schema: WoodSchema) -> WoodProfile:
    return WoodProfile(
        species=schema.species,
        diameter_mm=schema.diameter_mm,
        quality=schema.quality,
    )


def _to_trusted_competitor_record(schema: TrustedCompetitorSchema) -> CompetitorRecord:
    """Build a numeric record without accepting or retaining display-name PII."""

    return CompetitorRecord(
        name=schema.competitor_id,
        competitor_id=schema.competitor_id,
        gender=schema.gender,
        history=[],
    )


def _prediction_result_to_dict(pr: Any) -> Optional[Dict[str, Any]]:
    if pr is None:
        return None
    interval = _interval_to_response(pr.interval)
    return {
        "value": pr.value,
        "confidence": pr.confidence,
        "method": pr.method,
        "explanation": pr.explanation,
        "interval": interval.model_dump() if interval else None,
        "engine_version": pr.engine_version,
        "model_version": pr.model_version,
        "calibration_version": pr.calibration_version,
        "evidence_cutoff": pr.evidence_cutoff,
        "prediction_id": pr.prediction_id,
        "provenance": pr.provenance,
        "warnings": pr.warnings,
        "ignored_factors": pr.ignored_factors,
        "degraded": pr.degraded,
    }


def _interval_to_response(
    interval: Optional[PredictionInterval],
) -> Optional[PredictionIntervalResponse]:
    if interval is None:
        return None
    return PredictionIntervalResponse(
        lower=interval.lower,
        upper=interval.upper,
        nominal_coverage=interval.nominal_coverage,
        calibration_state=interval.calibration_state,
        scope=interval.scope,
    )


def _mark_result_response(result: Any) -> MarkResultResponse:
    return MarkResultResponse(
        name=result.name,
        mark=result.mark,
        predicted_time=result.predicted_time,
        method_used=result.method_used,
        confidence=result.confidence,
        explanation=result.explanation,
        std_dev=result.std_dev,
        competitor_id=result.competitor_id,
        interval=_interval_to_response(result.interval),
        engine_version=result.engine_version,
        model_version=result.model_version,
        calibration_version=result.calibration_version,
        evidence_cutoff=result.evidence_cutoff,
        optimizer=result.optimizer,
        warnings=result.warnings,
        prediction_id=result.prediction_id,
        ledger_recorded=result.ledger_recorded,
        degraded=result.degraded,
        optimizer_metadata=result.optimizer_metadata,
        ledger_status=result.ledger_status,
        provenance=result.provenance,
        ignored_factors=result.ignored_factors,
    )


def _ledger_persistence_health(ledger: PredictionLedger) -> Dict[str, Any]:
    """Observe the active ledger target without altering immutable evidence."""

    path = Path(ledger.path)
    memory = str(ledger.path) == ":memory:"
    exists = False if memory else path.is_file()
    readable = bool(exists and os.access(path, os.R_OK))
    writable = bool(exists and os.access(path, os.W_OK))
    read_write_open_observed = False
    if readable and writable:
        try:
            with sqlite3.connect(
                f"file:{path.as_posix()}?mode=rw",
                uri=True,
                timeout=0.1,
                isolation_level=None,
            ) as conn:
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            read_write_open_observed = True
        except sqlite3.Error:
            read_write_open_observed = False
    observed = bool(exists and read_write_open_observed)
    return {
        "configured_as_memory": memory,
        "path_exists": exists,
        "readable": readable and read_write_open_observed,
        "writable": writable and read_write_open_observed,
        "read_write_open_observed": read_write_open_observed,
        "persistence_observed": observed,
        "assurance": (
            "sqlite-read-write-observed-not-durability-proof" if observed else "unverified"
        ),
    }


class TrustedShadowRequestGate:
    """Pre-body gate for bearer validation and declared/actual byte bounds."""

    def __init__(self, application: Any) -> None:
        self.application = application

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/v1/shadow/"):
            await self.application(scope, receive, send)
            return

        headers = {bytes(key).lower(): bytes(value) for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is None:
            await self._reject(scope, receive, send, 411, "Bounded Content-Length required.")
            return
        try:
            declared_length = int(raw_length.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            await self._reject(scope, receive, send, 400, "Invalid Content-Length header.")
            return
        if declared_length < 0 or declared_length > _MAX_SHADOW_BODY_BYTES:
            await self._reject(scope, receive, send, 413, "Trusted shadow body is too large.")
            return
        try:
            preauthenticate_shadow_service(headers.get(b"authorization", b"").decode("latin-1"))
        except ShadowAuthenticationConfigurationError as exc:
            await self._reject(scope, receive, send, 503, str(exc))
            return
        except ShadowAuthenticationError as exc:
            await self._reject(scope, receive, send, 401, str(exc))
            return

        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > _MAX_SHADOW_BODY_BYTES:
                await self._reject(scope, receive, send, 413, "Trusted shadow body is too large.")
                return
            if not message.get("more_body", False):
                break
        if len(body) != declared_length:
            await self._reject(
                scope, receive, send, 400, "Content-Length does not match the request body."
            )
            return

        delivered = False

        async def replay_body() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.application(scope, replay_body, send)

    @staticmethod
    async def _reject(
        scope: Any,
        receive: Any,
        send: Any,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse({"detail": detail}, status_code=status_code)
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="STRATHMARK Handicap Engine",
    description=(
        "REST API for the STRATHMARK woodchopping handicap calculation engine. "
        "Exposes HandicapCalculator, Monte Carlo simulation, and result storage."
    ),
    version=__version__,
)
app.add_middleware(TrustedShadowRequestGate)


@app.get("/health")
def health(
    prediction_as_of: Optional[date] = None,
    store: ResultStore = Depends(get_store),
    ledger: PredictionLedger = Depends(get_ledger),
    prediction_provider: PredictionEngineProvider = Depends(get_prediction_provider),
) -> Dict[str, Any]:
    """Check Ollama connectivity and store availability."""
    from strathmark.features import normalize_prediction_as_of

    cutoff = normalize_prediction_as_of(prediction_as_of)
    bundle = prediction_provider.snapshot(cutoff)
    active_engine = prediction_config.selected_engine()
    engine_health = bundle.health(cutoff)
    engine_health["active_engine"] = active_engine
    for component in ("core", "residual", "calibration"):
        engine_health[component]["serving_active"] = active_engine == "v2"
    ollama_ok = check_ollama_connection()
    store_count = store.count()
    try:
        evidence_status = store.get_evidence_snapshot_status()
    except EvidenceSnapshotIntegrityError:
        evidence_snapshot = {
            "schema_version": "strathmark.evidence-snapshot-health.v1",
            "state": "invalid",
            "integrity": "failed",
            "ready_for_offline": False,
        }
        evidence_ready = False
    else:
        if evidence_status is None:
            evidence_snapshot = {
                "schema_version": "strathmark.evidence-snapshot-health.v1",
                "state": "missing",
                "integrity": "unavailable",
                "ready_for_offline": False,
            }
            evidence_ready = False
        else:
            evidence_snapshot = {
                "schema_version": "strathmark.evidence-snapshot-health.v1",
                "state": "active",
                "attestation": evidence_status.receipt_projection(),
                "integrity": evidence_status.integrity,
                "freshness": evidence_status.freshness,
                "completeness": evidence_status.completeness,
                "ready_for_offline": evidence_status.ready_for_offline,
            }
            evidence_ready = evidence_status.ready_for_offline
    auth_status = shadow_auth_configuration_status()
    topology = os.environ.get("STRATHMARK_TRUSTED_TOPOLOGY", "").strip().lower()
    topology_attested = topology in {"single-writer-durable", "offline-single-writer-durable"}
    persistence = _ledger_persistence_health(ledger)
    shadow_ready = (
        auth_status == "configured"
        and topology_attested
        and persistence["persistence_observed"]
        and evidence_ready
    )
    return {
        "status": "ok",
        "ollama_available": ollama_ok,
        "ollama_model": llm_config.DEFAULT_MODEL,
        "store_available": True,
        "store_results_count": store_count,
        "prediction_engine": engine_health,
        "shadow_service": {
            "schema_version": "strathmark.shadow-service-health.v1",
            "authentication": auth_status,
            "topology": "operator-attested-unverified" if topology_attested else "unattested",
            "topology_claim": topology if topology_attested else None,
            "topology_assurance": "operator-attested-not-infrastructure-proven",
            "ledger_persistence": persistence,
            "evidence_snapshot": evidence_snapshot,
            "ready_for_trusted_shadow": shadow_ready,
            "readiness": "ready" if shadow_ready else "not-ready",
        },
    }


@app.post("/calculate", response_model=List[MarkResultResponse])
def calculate(
    req: CalculateRequest,
    prediction_provider: PredictionEngineProvider = Depends(get_prediction_provider),
) -> List[MarkResultResponse]:
    """
    Compute AAA-compliant handicap marks for a field of competitors.

    Returns competitors sorted slowest-to-fastest (front marker first).
    """
    if not req.competitors:
        raise HTTPException(status_code=400, detail="competitors list must not be empty")

    calc = HandicapCalculator(prediction_provider=prediction_provider)
    records = [_to_competitor_record(c) for c in req.competitors]
    wood = _to_wood_profile(req.wood)

    try:
        mark_results = calc.calculate(
            competitors=records,
            wood=wood,
            event_code=req.event_code,
            tournament_results=req.tournament_results or {},
            manual_overrides=req.manual_overrides or {},
            context=PredictionContext(prediction_as_of=req.prediction_as_of),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return [_mark_result_response(result) for result in mark_results]


@app.post(
    "/ledger/calculate",
    response_model=List[MarkResultResponse],
    dependencies=[Depends(require_results_token)],
)
def ledger_calculate(
    req: LedgerCalculateRequest,
    ledger: PredictionLedger = Depends(get_ledger),
    prediction_provider: PredictionEngineProvider = Depends(get_prediction_provider),
) -> List[MarkResultResponse]:
    """Calculate and atomically record a trusted stable-identity field."""

    if not req.competitors:
        raise HTTPException(status_code=400, detail="competitors list must not be empty")
    missing = [item.name for item in req.competitors if not item.competitor_id]
    if missing:
        raise HTTPException(
            status_code=422,
            detail="stable competitor_id is required for every trusted ledger entry",
        )

    calc = HandicapCalculator(
        prediction_provider=prediction_provider,
        ledger_sink=ledger,
        ledger_caller_id=os.environ.get("STRATHMARK_LEDGER_CALLER", "api"),
    )
    try:
        results = calc.calculate(
            competitors=[_to_competitor_record(item) for item in req.competitors],
            wood=_to_wood_profile(req.wood),
            event_code=req.event_code,
            tournament_results=req.tournament_results or {},
            manual_overrides=req.manual_overrides or {},
            context=PredictionContext(
                prediction_as_of=req.prediction_as_of,
                request_id=req.request_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if any(result.ledger_status == "idempotency_conflict" for result in results):
        raise HTTPException(
            status_code=409,
            detail="request_id was already used for a different canonical payload",
        )
    return [_mark_result_response(result) for result in results]


@app.post(
    "/ledger/predictions/{prediction_id}/settle",
    response_model=SettlementResponse,
    dependencies=[Depends(require_results_token)],
)
def settle_ledger_prediction(
    prediction_id: str,
    req: SettlePredictionRequest,
    ledger: PredictionLedger = Depends(get_ledger),
) -> SettlementResponse:
    """Append an explicit immutable settlement or attributed correction."""

    try:
        settlement = ledger.settle(
            prediction_id=prediction_id,
            competitor_id=req.competitor_id,
            event_code=req.event_code,
            actual_time=req.actual_time,
            actor=os.environ.get("STRATHMARK_LEDGER_ACTOR", "api"),
            reason=req.reason,
        )
    except SettlementConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return SettlementResponse(**settlement.__dict__)


@app.post(
    "/v1/shadow/receipts/lookup",
    dependencies=[Depends(require_shadow_body_limit)],
)
def lookup_shadow_receipt(
    req: ShadowReceiptLookupRequest,
    authorization: Optional[str] = Header(default=None),
    actor_attestation: Optional[str] = Header(default=None, alias="X-STRATHMARK-Actor-Attestation"),
    ledger: PredictionLedger = Depends(get_ledger),
) -> Dict[str, Any]:
    """Recover one exact immutable receipt core without recalculation."""

    _authorize_shadow_action(
        authorization=authorization,
        actor_attestation=actor_attestation,
        consumer_id=req.consumer_id,
        actor_id=None,
        action="shadow.receipt.lookup",
        subject_revision=req.run_revision,
        ledger=ledger,
    )
    try:
        receipt = ledger.get_shadow_receipt(
            req.consumer_id,
            req.request_id,
            current_active_fingerprint=req.current_active_fingerprint,
            expected_run_revision=req.run_revision,
        )
    except LedgerConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ShadowReceiptCorruptionError:
        raise HTTPException(
            status_code=409, detail="Persisted shadow receipt failed integrity checks."
        )
    if receipt is None:
        raise HTTPException(status_code=404, detail="Shadow receipt was not found.")
    return {
        "schema_version": "strathmark.shadow-receipt-lookup-response.v1",
        "receipt": _receipt_response(receipt),
    }


@app.post(
    "/v1/shadow/calculate",
    dependencies=[Depends(require_shadow_body_limit)],
)
def calculate_shadow_field(
    req: ShadowCalculateRequest,
    authorization: Optional[str] = Header(default=None),
    actor_attestation: Optional[str] = Header(default=None, alias="X-STRATHMARK-Actor-Attestation"),
    ledger: PredictionLedger = Depends(get_ledger),
    service: ShadowPredictionService = Depends(get_shadow_service),
) -> Dict[str, Any]:
    """Recover first, or atomically calculate and record one trusted field."""

    _authorize_shadow_action(
        authorization=authorization,
        actor_attestation=actor_attestation,
        consumer_id=req.consumer_id,
        actor_id=req.operator_id,
        action="shadow.calculate",
        subject_revision=req.run_revision,
        ledger=ledger,
    )
    request = ShadowFieldRequest(
        consumer_id=req.consumer_id,
        tournament_id=req.tournament_id,
        event_occurrence_id=req.event_occurrence_id,
        field_run_id=req.field_run_id,
        operator_id=req.operator_id,
        request_id=req.request_id,
        run_revision=req.run_revision,
        event_code=req.event_code,
        target_contract=req.target_contract,
        prediction_as_of=req.prediction_as_of,
        schedule_fingerprint=req.schedule_fingerprint,
        observation_schema_version=req.observation_schema_version,
        observation_fingerprint=req.observation_fingerprint,
        seed=req.seed,
    )
    competitors = [_to_trusted_competitor_record(item) for item in req.competitors]
    wood = _to_wood_profile(req.wood)
    try:
        result = _run_bounded_shadow_calculation(
            service,
            request,
            competitors,
            wood,
            timeout_ms=req.timeout_ms,
        )
    except LedgerConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ShadowReceiptCorruptionError:
        raise HTTPException(
            status_code=409, detail="Persisted shadow receipt failed integrity checks."
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "schema_version": "strathmark.shadow-calculate-response.v1",
        "trusted": result.trusted,
        "receipt": _receipt_response(result.receipt),
        "status": asdict(result.status),
        "draft_predictions": [dict(row) for row in result.draft_predictions],
    }


@app.post(
    "/v1/shadow/status",
    dependencies=[Depends(require_shadow_body_limit)],
)
def read_shadow_status(
    req: ShadowStatusRequest,
    authorization: Optional[str] = Header(default=None),
    actor_attestation: Optional[str] = Header(default=None, alias="X-STRATHMARK-Actor-Attestation"),
    ledger: PredictionLedger = Depends(get_ledger),
) -> Dict[str, Any]:
    """Return payload-free current trust, freshness, mirror, and evidence axes."""

    _authorize_shadow_action(
        authorization=authorization,
        actor_attestation=actor_attestation,
        consumer_id=req.consumer_id,
        actor_id=None,
        action="shadow.status.read",
        subject_revision=req.run_revision,
        ledger=ledger,
    )
    try:
        status = _run_bounded_shadow_read(
            lambda query_deadline: ledger.get_monitoring_status(
                model_version=req.model_version,
                caller_id=req.consumer_id,
                request_id=req.request_id,
                current_active_fingerprint=req.current_active_fingerprint,
                expected_run_revision=req.run_revision,
                query_deadline=query_deadline,
            ),
            timeout_ms=req.timeout_ms,
            timeout_detail=("Shadow status read timed out without changing trusted state."),
        )
    except LedgerConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "schema_version": "strathmark.shadow-status-response.v1",
        "status": asdict(status),
    }


@app.post(
    "/v1/shadow/outcomes/apply",
    dependencies=[Depends(require_shadow_body_limit)],
)
def apply_shadow_numeric_outcome(
    req: ShadowNumericOutcomeRequest,
    authorization: Optional[str] = Header(default=None),
    actor_attestation: Optional[str] = Header(default=None, alias="X-STRATHMARK-Actor-Attestation"),
    ledger: PredictionLedger = Depends(get_ledger),
) -> Dict[str, Any]:
    """Append one field-atomic numeric settlement/void projection."""

    actor = _authorize_shadow_action(
        authorization=authorization,
        actor_attestation=actor_attestation,
        consumer_id=req.consumer_id,
        actor_id=None,
        action="shadow.outcome.apply",
        subject_revision=req.run_revision,
        ledger=ledger,
    )
    try:
        result = ledger.apply_numeric_outcome_revision(
            req.outcome_revision_id,
            [item.model_dump() for item in req.revisions],
            caller_id=req.consumer_id,
            request_id=req.request_id,
            run_revision=req.run_revision,
            actor=actor.actor_id,
            reason_code=req.reason_code,
        )
    except SettlementConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "schema_version": "strathmark.shadow-numeric-outcome-response.v1",
        "outcome": asdict(result),
    }


@app.post(
    "/v1/shadow/mirror/replay",
    dependencies=[Depends(require_shadow_body_limit)],
)
def replay_shadow_mirror(
    req: ShadowMirrorReplayRequest,
    authorization: Optional[str] = Header(default=None),
    actor_attestation: Optional[str] = Header(default=None, alias="X-STRATHMARK-Actor-Attestation"),
    ledger: PredictionLedger = Depends(get_ledger),
) -> Dict[str, Any]:
    """Retry a bounded number of durable mirror outbox rows off the trust path."""

    _authorize_shadow_action(
        authorization=authorization,
        actor_attestation=actor_attestation,
        consumer_id=req.consumer_id,
        actor_id=None,
        action="shadow.mirror.replay",
        subject_revision=req.run_revision,
        ledger=ledger,
    )
    if not _SHADOW_OPERATION_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Trusted shadow operation capacity is busy.")
    future = _SHADOW_EXECUTOR.submit(
        ledger.flush_mirror_outbox,
        limit=req.limit,
        caller_id=req.consumer_id,
    )
    future.add_done_callback(lambda unused: _SHADOW_OPERATION_SLOTS.release())
    try:
        summary = future.result(timeout=req.timeout_ms / 1000.0)
    except FutureTimeoutError:
        raise HTTPException(
            status_code=504,
            detail="Mirror replay continues off-path; refresh status before requesting another replay.",
        )
    return {
        "schema_version": "strathmark.shadow-mirror-replay-response.v1",
        "attempted_limit": req.limit,
        "summary": summary,
    }


@app.post(
    "/v1/shadow/drift",
    dependencies=[Depends(require_shadow_body_limit)],
)
def read_shadow_drift(
    req: ShadowDriftRequest,
    authorization: Optional[str] = Header(default=None),
    actor_attestation: Optional[str] = Header(default=None, alias="X-STRATHMARK-Actor-Attestation"),
    ledger: PredictionLedger = Depends(get_ledger),
) -> Dict[str, Any]:
    """Return bounded advisory drift evidence; it never blocks race-day work."""

    if any(abs(value) > MAX_NUMERIC_RAW_TIME_SECONDS for value in req.baseline_residuals):
        raise HTTPException(
            status_code=422, detail="baseline residual is outside the numeric bound"
        )
    _authorize_shadow_action(
        authorization=authorization,
        actor_attestation=actor_attestation,
        consumer_id=req.consumer_id,
        actor_id=None,
        action="shadow.drift.read",
        subject_revision=req.run_revision,
        ledger=ledger,
    )
    try:
        report = _run_bounded_shadow_read(
            lambda query_deadline: evaluate_drift(
                model_version_id=req.model_version,
                lookback_days=req.lookback_days,
                ledger=ledger,
                baseline_residuals=req.baseline_residuals,
                caller_id=req.consumer_id,
                max_rows=MAX_DRIFT_ROWS,
                query_deadline=query_deadline,
            ),
            timeout_ms=req.timeout_ms,
            timeout_detail=("Advisory drift evaluation timed out without changing trusted state."),
        )
    except DriftRowLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "schema_version": "strathmark.shadow-drift-response.v1",
        "advisory_only": True,
        "row_limit": MAX_DRIFT_ROWS,
        "truncated": False,
        "report": asdict(report),
    }


@app.post("/predict", response_model=PredictResponse)
def predict(
    req: PredictRequest,
    prediction_provider: PredictionEngineProvider = Depends(get_prediction_provider),
) -> PredictResponse:
    """
    Run all prediction methods for a single competitor and return all results.

    Useful for the 3-column prediction display in STRATHEX.
    """
    record = _to_competitor_record(req.competitor)
    wood = _to_wood_profile(req.wood)

    all_preds = get_all_predictions(
        record,
        wood,
        req.event_code,
        context=PredictionContext(prediction_as_of=req.prediction_as_of),
        prediction_provider=prediction_provider,
    )
    best_pred = select_best_prediction(all_preds)

    return PredictResponse(
        best=MarkResultResponse(
            name=record.name,
            mark=3,  # Mark not computed without a field; caller computes marks via /calculate
            predicted_time=best_pred.value,
            method_used=best_pred.method,
            confidence=best_pred.confidence,
            explanation=best_pred.explanation,
            std_dev=best_pred.metadata.get("std_dev"),
            competitor_id=record.competitor_id,
            interval=_interval_to_response(best_pred.interval),
            engine_version=best_pred.engine_version,
            model_version=best_pred.model_version,
            calibration_version=best_pred.calibration_version,
            evidence_cutoff=best_pred.evidence_cutoff,
            warnings=best_pred.warnings,
            prediction_id=best_pred.prediction_id,
            degraded=best_pred.degraded,
            provenance=best_pred.provenance,
            ignored_factors=best_pred.ignored_factors,
        ),
        all_predictions={k: _prediction_result_to_dict(v) for k, v in all_preds.items()},
    )


@app.post("/simulate")
def simulate(req: SimulateRequest) -> Dict[str, Any]:
    """
    Run Monte Carlo fairness simulation for a handicapped field.

    Input competitors format:
        [{"name": "...", "mark": 3, "predicted_time": 60.0, ...}, ...]
    """
    if len(req.competitors) * req.num_simulations > _MAX_SIMULATION_CELLS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Simulation workload is too large. Reduce competitors or num_simulations so "
                f"their product is at most {_MAX_SIMULATION_CELLS:,}."
            ),
        )

    if not _SIMULATION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Simulation capacity is busy. Retry the request shortly.",
        )
    try:
        analysis = run_monte_carlo_simulation(
            competitors=[
                competitor.model_dump(exclude_none=True) for competitor in req.competitors
            ],
            num_simulations=req.num_simulations,
            track_finish_orders=req.track_finish_orders,
            track_podium_margins=req.track_podium_margins,
            include_finish_spreads=False,
        )
    finally:
        _SIMULATION_SLOTS.release()

    # Convert CompetitorTimeStats dataclasses to dicts for JSON serialization
    cts = {}
    for name, stats in analysis.get("competitor_time_stats", {}).items():
        if hasattr(stats, "mean"):
            cts[name] = {
                "mean": stats.mean,
                "std_dev": stats.std_dev,
                "min": stats.min_time,
                "max": stats.max_time,
                "p25": stats.p25,
                "p50": stats.p50,
                "p75": stats.p75,
                "consistency_rating": stats.consistency_rating,
            }
        else:
            cts[name] = stats
    analysis["competitor_time_stats"] = cts

    # most_common_order is a tuple; convert to list for JSON
    if analysis.get("most_common_order") is not None:
        analysis["most_common_order"] = list(analysis["most_common_order"])

    return analysis


@app.post(
    "/results",
    response_model=RecordResultResponse,
    dependencies=[Depends(require_results_token)],
)
def record_result(
    req: RecordResultRequest, store: ResultStore = Depends(get_store)
) -> RecordResultResponse:
    """Record a tournament result to the persistent store."""
    inserted = store.record_result(
        competitor_name=req.competitor_name,
        event_code=req.event_code,
        time_seconds=req.time_seconds,
        species=req.species,
        diameter_mm=req.diameter_mm,
        quality=req.quality,
        heat_id=req.heat_id,
        result_date=req.result_date,
        competition_id=req.competition_id,
    )
    return RecordResultResponse(
        inserted=inserted,
        message="Result recorded." if inserted else "Duplicate result skipped.",
    )


@app.get("/results/{competitor_name}", dependencies=[Depends(require_results_token)])
def get_results(
    competitor_name: str,
    event_code: Optional[str] = None,
    store: ResultStore = Depends(get_store),
) -> List[Dict[str, Any]]:
    """Retrieve all stored results for a competitor."""
    history = store.get_competitor_history(competitor_name, event_code)
    return [
        {
            "event_code": r.event_code,
            "time_seconds": r.time_seconds,
            "species": r.species,
            "diameter_mm": r.diameter_mm,
            "quality": r.quality,
            "result_date": r.result_date.isoformat() if r.result_date else None,
            "heat_id": r.heat_id,
        }
        for r in history
    ]
