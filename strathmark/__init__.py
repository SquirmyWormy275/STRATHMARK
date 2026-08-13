"""
strathmark — Woodchopping Handicap Engine
==========================================

A pip-installable Python package that exposes the STRATHEX handicap calculation
engine for use in external applications (tournament software, scoring apps, etc.).

Quick start
-----------
    from strathmark import HandicapCalculator, CompetitorRecord, WoodProfile, ResultStore
    from strathmark.predictor import HistoricalResult

    store = ResultStore()          # opens ~/.strathmark/results.db
    calc  = HandicapCalculator()

    competitor = CompetitorRecord(
        name="Alice Smith",
        history=store.get_competitor_history("Alice Smith", "SB"),
    )
    wood  = WoodProfile(species="Pine", diameter_mm=300, quality=5)
    marks = calc.calculate([competitor], wood, "SB")

Python import API (for STRATHEX and other Python projects):
    from strathmark import HandicapCalculator
    from strathmark.fairness import simulate_and_assess_handicaps
    from strathmark.variance import run_monte_carlo_simulation

HTTP REST API (for web/mobile/non-Python projects):
    uvicorn strathmark.api:app --port 8000
    POST http://localhost:8000/calculate

Design Rules (invariants enforced in all submodules)
-----------------------------------------------------
    - Mark floor:    3 seconds (never lower, under any circumstances)
    - Mark ceiling:  system-wide 183 seconds (180s time limit + 3s minimum mark)
                     event configs may enforce a lower ceiling
    - Variance:      absolute +-3 seconds ONLY — proportional variance is forbidden
    - Prediction engine: manual override, otherwise the validated V2 posterior
      with an explicit deterministic rollback fallback
    - Time-decay:    exponential decay, 2-year half-life (730 days)
    - Output:        plain text only — no emojis, no ANSI color codes
    - Style:         lean and simple, no unnecessary complexity

Live integration (editable install):
    pip install -e ./STRATHMARK
    Improvements to STRATHMARK are immediately available to STRATHEX on next run.
    No rebuild or reinstall required.
"""

from strathmark.calculator import HandicapCalculator, process_competition_day
from strathmark.db import (
    format_proam_results,
    get_active_model_version,
    get_competitor_bias,
    mirror_prediction_ledger,
    pull_competitors,
    pull_results,
    push_competitors,
    push_results,
    push_results_dicts,
    record_calibration,
    record_prediction,
    record_prediction_residuals,
    register_competitor,
    register_model_version,
    set_active_model,
    settle_prediction,
    store_features,
)
from strathmark.drift import DriftReport, evaluate_drift, is_drifting
from strathmark.fairness import (
    get_ai_assessment_of_handicaps,
    get_championship_race_analysis,
    simulate_and_assess_handicaps,
)
from strathmark.features import (
    ExclusionDiagnostics,
    PriorEvidence,
    build_prior_evidence,
    normalize_prediction_as_of,
    resolve_species_properties,
)
from strathmark.ledger import (
    LedgerConflictError,
    LedgerPrediction,
    LedgerWriteResult,
    PredictionLedger,
    SettlementConflictError,
    SettlementResult,
)
from strathmark.llm import call_ollama, check_ollama_connection
from strathmark.loader import load_results_for_competitor, load_woodchopping_xlsx
from strathmark.mark_optimizer import (
    MarkOptimizationResult,
    legacy_rounded_gap_marks,
    optimize_joint_marks,
)
from strathmark.mnemex import (
    is_mnemex_configured,
    pull_canonical_competitors,
    pull_canonical_results,
    register_competitor_in_mnemex,
)
from strathmark.prediction_v2 import (
    ChronologicalCalibrator,
    ForecastInterval,
    PredictionV2Model,
    PredictionV2Request,
    PredictiveDistribution,
)
from strathmark.predictor import (
    CompetitorRecord,
    FilePredictionProvider,
    HistoricalResult,
    PredictionBundle,
    PredictionContext,
    PredictionEngineProvider,
    PredictionInterval,
    PredictionResult,
    StaticPredictionProvider,
    WoodProfile,
    get_all_predictions,
    get_best_prediction,
    get_prediction_provider,
    predict_baseline,
    select_best_prediction,
)
from strathmark.store import ResultStore
from strathmark.sync import (
    SyncResult,
    manual_force_sync,
    nightly_batch,
    strathex_finalization,
)
from strathmark.utils import score_prediction_accuracy
from strathmark.variance import (
    audit_mark_sheet,
    estimate_competitor_std_dev,
    quick_fairness_check,
    run_monte_carlo_simulation,
)
from strathmark.visualization import generate_simulation_summary, visualize_simulation_results

__all__ = [
    # Core calculation
    "HandicapCalculator",
    "process_competition_day",
    # Data types
    "CompetitorRecord",
    "WoodProfile",
    "HistoricalResult",
    "PredictionResult",
    "PredictionContext",
    "PredictionInterval",
    "PredictionBundle",
    "PredictionEngineProvider",
    "StaticPredictionProvider",
    "FilePredictionProvider",
    "PriorEvidence",
    "ExclusionDiagnostics",
    # Prediction API
    "get_best_prediction",
    "get_prediction_provider",
    "get_all_predictions",
    "select_best_prediction",
    "predict_baseline",
    "build_prior_evidence",
    "normalize_prediction_as_of",
    "resolve_species_properties",
    "PredictionV2Model",
    "PredictionV2Request",
    "PredictiveDistribution",
    "ForecastInterval",
    "ChronologicalCalibrator",
    "MarkOptimizationResult",
    "legacy_rounded_gap_marks",
    "optimize_joint_marks",
    # Persistence
    "ResultStore",
    "PredictionLedger",
    "LedgerPrediction",
    "LedgerWriteResult",
    "LedgerConflictError",
    "SettlementResult",
    "SettlementConflictError",
    # Simulation
    "run_monte_carlo_simulation",
    "estimate_competitor_std_dev",
    "audit_mark_sheet",
    "quick_fairness_check",
    # Visualization
    "generate_simulation_summary",
    "visualize_simulation_results",
    # Fairness
    "get_ai_assessment_of_handicaps",
    "get_championship_race_analysis",
    "simulate_and_assess_handicaps",
    # LLM
    "call_ollama",
    "check_ollama_connection",
    # Data loading
    "load_woodchopping_xlsx",
    "load_results_for_competitor",
    # Database (Supabase)
    "push_results",
    "push_results_dicts",
    "pull_results",
    "push_competitors",
    "pull_competitors",
    "register_competitor",
    "format_proam_results",
    "record_prediction_residuals",
    "get_competitor_bias",
    "mirror_prediction_ledger",
    # ML state (carve-out from controlled-write rule; STRATHMARK-internal)
    "register_model_version",
    "set_active_model",
    "get_active_model_version",
    "record_calibration",
    "store_features",
    "record_prediction",
    "settle_prediction",
    # MNEMEX (canonical archive client)
    "is_mnemex_configured",
    "pull_canonical_results",
    "pull_canonical_competitors",
    "register_competitor_in_mnemex",
    # Sync (MNEMEX -> STRATHMARK Supabase)
    "SyncResult",
    "nightly_batch",
    "strathex_finalization",
    "manual_force_sync",
    # Drift detection
    "DriftReport",
    "evaluate_drift",
    "is_drifting",
    # Scoring / accuracy
    "score_prediction_accuracy",
]

__version__ = "2.0.0"
