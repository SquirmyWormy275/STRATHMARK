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
    - Prediction cascade: Manual > LLM > ML > Panel mark fallback
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
    get_competitor_bias,
    pull_competitors,
    pull_results,
    push_competitors,
    push_results,
    record_prediction_residuals,
)
from strathmark.fairness import (
    get_ai_assessment_of_handicaps,
    get_championship_race_analysis,
    simulate_and_assess_handicaps,
)
from strathmark.llm import call_ollama, check_ollama_connection
from strathmark.loader import load_results_for_competitor, load_woodchopping_xlsx
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    PredictionResult,
    WoodProfile,
    get_all_predictions,
    get_best_prediction,
    predict_baseline,
    select_best_prediction,
)
from strathmark.store import ResultStore
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
    # Prediction API
    "get_best_prediction",
    "get_all_predictions",
    "select_best_prediction",
    "predict_baseline",
    # Persistence
    "ResultStore",
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
    "pull_results",
    "push_competitors",
    "pull_competitors",
    "record_prediction_residuals",
    "get_competitor_bias",
    # Scoring / accuracy
    "score_prediction_accuracy",
]

__version__ = "0.3.1"
