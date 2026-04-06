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

from datetime import date
from typing import Any, Dict, List, Optional

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

from strathmark import __version__
from strathmark.calculator import HandicapCalculator
from strathmark.config import llm_config
from strathmark.llm import check_ollama_connection
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    WoodProfile,
    get_all_predictions,
    select_best_prediction,
)
from strathmark.store import ResultStore
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
    event_code: str
    time_seconds: float = Field(gt=0, description="Time in seconds (must be positive)")
    species: str
    diameter_mm: float = Field(gt=0, description="Log diameter in mm (must be positive)")
    quality: int = Field(ge=1, le=10, description="Wood quality 1-10")
    result_date: Optional[str] = None  # ISO 8601 date string
    heat_id: Optional[str] = None


class CompetitorSchema(BaseModel):
    name: str
    history: List[HistoricalResultSchema] = Field(default_factory=list)
    division: Optional[str] = None
    manual_time_override: Optional[float] = None
    tournament_time: Optional[float] = None


class WoodSchema(BaseModel):
    species: str
    diameter_mm: float = Field(gt=0, description="Log diameter in mm (must be positive)")
    quality: int = Field(ge=1, le=10, description="Wood quality 1-10")


class CalculateRequest(BaseModel):
    competitors: List[CompetitorSchema]
    wood: WoodSchema
    event_code: str
    tournament_results: Optional[Dict[str, float]] = None
    manual_overrides: Optional[Dict[str, float]] = None


class MarkResultResponse(BaseModel):
    name: str
    mark: int
    predicted_time: float
    method_used: str
    confidence: str
    explanation: str


class PredictRequest(BaseModel):
    competitor: CompetitorSchema
    wood: WoodSchema
    event_code: str


class PredictResponse(BaseModel):
    best: MarkResultResponse
    all_predictions: Dict[str, Optional[Dict[str, Any]]]


class SimulateRequest(BaseModel):
    competitors: List[Dict[str, Any]]  # [{name, mark, predicted_time, ...}]
    num_simulations: int = 250_000
    track_finish_orders: bool = False
    track_podium_margins: bool = False


class RecordResultRequest(BaseModel):
    competitor_name: str
    event_code: str
    time_seconds: float = Field(gt=0, description="Time in seconds (must be positive)")
    species: str
    diameter_mm: float = Field(gt=0, description="Log diameter in mm (must be positive)")
    quality: int = Field(ge=1, le=10, description="Wood quality 1-10")
    heat_id: Optional[str] = None
    result_date: Optional[str] = None  # ISO 8601 date string


class RecordResultResponse(BaseModel):
    inserted: bool
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_store = ResultStore()


def _parse_date(s: Optional[str], *, strict: bool = False) -> Optional[date]:
    if s is None:
        return None
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        if strict:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid date format: '{s}'. Use ISO 8601 (YYYY-MM-DD).",
            )
        return None


def _to_competitor_record(schema: CompetitorSchema) -> CompetitorRecord:
    history = [
        HistoricalResult(
            event_code=h.event_code,
            time_seconds=h.time_seconds,
            species=h.species,
            diameter_mm=h.diameter_mm,
            quality=h.quality,
            result_date=_parse_date(h.result_date),
            heat_id=h.heat_id,
        )
        for h in schema.history
    ]
    return CompetitorRecord(
        name=schema.name,
        history=history,
        division=schema.division,
        manual_time_override=schema.manual_time_override,
        tournament_time=schema.tournament_time,
    )


def _to_wood_profile(schema: WoodSchema) -> WoodProfile:
    return WoodProfile(
        species=schema.species,
        diameter_mm=schema.diameter_mm,
        quality=schema.quality,
    )


def _prediction_result_to_dict(pr: Any) -> Optional[Dict[str, Any]]:
    if pr is None:
        return None
    return {
        "value": pr.value,
        "confidence": pr.confidence,
        "method": pr.method,
        "explanation": pr.explanation,
    }


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


@app.get("/health")
def health() -> Dict[str, Any]:
    """Check Ollama connectivity and store availability."""
    ollama_ok = check_ollama_connection()
    store_count = _store.count()
    return {
        "status": "ok",
        "ollama_available": ollama_ok,
        "ollama_model": llm_config.DEFAULT_MODEL,
        "store_path": str(_store._path),
        "store_results_count": store_count,
    }


@app.post("/calculate", response_model=List[MarkResultResponse])
def calculate(req: CalculateRequest) -> List[MarkResultResponse]:
    """
    Compute AAA-compliant handicap marks for a field of competitors.

    Returns competitors sorted slowest-to-fastest (front marker first).
    """
    if not req.competitors:
        raise HTTPException(status_code=400, detail="competitors list must not be empty")

    calc = HandicapCalculator()
    records = [_to_competitor_record(c) for c in req.competitors]
    wood = _to_wood_profile(req.wood)

    try:
        mark_results = calc.calculate(
            competitors=records,
            wood=wood,
            event_code=req.event_code,
            tournament_results=req.tournament_results or {},
            manual_overrides=req.manual_overrides or {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return [
        MarkResultResponse(
            name=r.name,
            mark=r.mark,
            predicted_time=r.predicted_time,
            method_used=r.method_used,
            confidence=r.confidence,
            explanation=r.explanation,
        )
        for r in mark_results
    ]


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest) -> PredictResponse:
    """
    Run all prediction methods for a single competitor and return all results.

    Useful for the 3-column prediction display in STRATHEX.
    """
    record = _to_competitor_record(req.competitor)
    wood = _to_wood_profile(req.wood)

    all_preds = get_all_predictions(record, wood, req.event_code)
    best_pred = select_best_prediction(all_preds)

    return PredictResponse(
        best=MarkResultResponse(
            name=record.name,
            mark=3,  # Mark not computed without a field; caller computes marks via /calculate
            predicted_time=best_pred.value,
            method_used=best_pred.method,
            confidence=best_pred.confidence,
            explanation=best_pred.explanation,
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
    if len(req.competitors) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 competitors")

    analysis = run_monte_carlo_simulation(
        competitors=req.competitors,
        num_simulations=req.num_simulations,
        track_finish_orders=req.track_finish_orders,
        track_podium_margins=req.track_podium_margins,
    )

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

    # Remove large list from JSON response (finish_spreads can be millions of floats)
    analysis.pop("finish_spreads", None)
    # most_common_order is a tuple; convert to list for JSON
    if analysis.get("most_common_order") is not None:
        analysis["most_common_order"] = list(analysis["most_common_order"])

    return analysis


@app.post("/results", response_model=RecordResultResponse)
def record_result(req: RecordResultRequest) -> RecordResultResponse:
    """Record a tournament result to the persistent store."""
    inserted = _store.record_result(
        competitor_name=req.competitor_name,
        event_code=req.event_code,
        time_seconds=req.time_seconds,
        species=req.species,
        diameter_mm=req.diameter_mm,
        quality=req.quality,
        heat_id=req.heat_id,
        result_date=_parse_date(req.result_date, strict=True),
    )
    return RecordResultResponse(
        inserted=inserted,
        message="Result recorded." if inserted else "Duplicate result skipped.",
    )


@app.get("/results/{competitor_name}")
def get_results(
    competitor_name: str,
    event_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve all stored results for a competitor."""
    history = _store.get_competitor_history(competitor_name, event_code)
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
