"""
Configuration constants for the STRATHMARK handicap engine.

This module centralizes all configuration values, magic numbers, and system
parameters to improve maintainability and make it easier to adjust system
behavior. All values are derived from the production-validated STRATHEX V5.2.1
parameter set.

Import specific instances, not classes:
    from strathmark.config import rules, sim_config, decay_config
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to *default* on any error.

    Used by LLMConfig so that deployments without a reachable Ollama (e.g. the
    Pro-Am Manager on Railway) can dial down timeouts and retries to avoid
    minute-long hangs in the cascade fall-through.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    """Read a string from the environment, falling back to *default* if unset."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


# =============================================================================
# AAA Competition Rules
# =============================================================================


@dataclass(frozen=True)
class Rules:
    """Australian Axemen's Association Competition Rules"""

    # Handicap mark constraints
    MIN_MARK_SECONDS: int = 3
    """Minimum handicap mark (front marker)"""

    MAX_TIME_LIMIT_SECONDS: int = 180
    """Maximum time limit for completion"""

    MAX_MARK_SECONDS: int = 183
    """
    System-wide mark ceiling = MAX_TIME_LIMIT_SECONDS + MIN_MARK_SECONDS.
    Any computed mark above this is clamped to 183.
    Individual events may configure a lower ceiling.
    """

    # Performance variance (critical for fairness)
    PERFORMANCE_VARIANCE_SECONDS: int = 3
    """Absolute performance variation (+-3 seconds) for all competitors"""


# =============================================================================
# Data Requirements & Validation
# =============================================================================


@dataclass(frozen=True)
class DataRequirements:
    """Data validation thresholds and requirements"""

    # Historical data requirements
    MIN_HISTORICAL_TIMES: int = 3
    """Minimum historical times required for new competitors"""

    MIN_ML_TRAINING_RECORDS_TOTAL: int = 100
    """Minimum total records required for ML model training (23 features x 10:1 ratio = 230 ideal; 100 hard floor)"""

    MIN_ML_TRAINING_RECORDS_PER_EVENT: int = 75
    """Minimum records per event (SB/UH) for event-specific models (was 15; raised to prevent XGBoost overfitting)"""

    # Validation ranges
    MIN_DIAMETER_MM: int = 225
    """Minimum valid block diameter in millimeters"""

    MAX_DIAMETER_MM: int = 500
    """Maximum valid block diameter in millimeters"""

    MIN_VALID_TIME_SECONDS: float = 10.0
    """Minimum valid time (exclusive) - elite choppers can achieve sub-15s times"""

    MAX_VALID_TIME_SECONDS: float = 300.0
    """Maximum valid time (inclusive)"""

    # Outlier detection
    OUTLIER_IQR_MULTIPLIER: float = 3.0
    """IQR multiplier for outlier detection (3x IQR = extreme outliers only)"""

    # Confidence thresholds
    HIGH_CONFIDENCE_MIN_EVENTS: int = 5
    """Minimum events for HIGH confidence predictions"""

    MEDIUM_CONFIDENCE_MIN_EVENTS: int = 3
    """Minimum events for MEDIUM confidence predictions"""


# =============================================================================
# Machine Learning Configuration
# =============================================================================


@dataclass(frozen=True)
class MLConfig:
    """XGBoost + LightGBM ML model hyperparameters and settings"""

    # XGBoost hyperparameters (Optuna-tuned, 30 trials, GroupKFold validation)
    N_ESTIMATORS: int = 292
    """Number of boosting rounds"""

    MAX_DEPTH: int = 4
    """Maximum tree depth"""

    LEARNING_RATE: float = 0.0305
    """Boosting learning rate (Optuna-tuned)"""

    RANDOM_STATE: int = 42
    """Random seed for reproducibility"""

    OBJECTIVE: str = "reg:squarederror"
    """Loss function (applied to log-transformed target)"""

    TREE_METHOD: str = "hist"
    """Tree construction algorithm"""

    SUBSAMPLE: float = 0.643
    """Row subsampling ratio (Optuna-tuned)"""

    COLSAMPLE_BYTREE: float = 0.508
    """Column subsampling ratio (Optuna-tuned)"""

    MIN_CHILD_WEIGHT: int = 7
    """Minimum sum of instance weight in a child (Optuna-tuned)"""

    REG_ALPHA: float = 0.261
    """L1 regularization (Optuna-tuned)"""

    REG_LAMBDA: float = 0.219
    """L2 regularization (Optuna-tuned)"""

    # LightGBM hyperparameters (Optuna-tuned, 30 trials, GroupKFold validation)
    LGB_N_ESTIMATORS: int = 222
    """LightGBM number of boosting rounds"""

    LGB_MAX_DEPTH: int = 4
    """LightGBM maximum tree depth"""

    LGB_LEARNING_RATE: float = 0.0303
    """LightGBM learning rate"""

    LGB_SUBSAMPLE: float = 0.938
    """LightGBM row subsampling"""

    LGB_COLSAMPLE_BYTREE: float = 0.626
    """LightGBM column subsampling"""

    LGB_MIN_CHILD_SAMPLES: int = 20
    """LightGBM minimum samples in a leaf"""

    LGB_NUM_LEAVES: int = 23
    """LightGBM maximum number of leaves"""

    LGB_REG_ALPHA: float = 0.079
    """LightGBM L1 regularization"""

    LGB_REG_LAMBDA: float = 0.101
    """LightGBM L2 regularization"""

    # Cross-validation
    CV_FOLDS: int = 5
    """Number of folds for cross-validation"""

    # Prediction validation
    MIN_PREDICTION_TIME: float = 5.0
    """Minimum reasonable prediction time (seconds)"""

    MAX_PREDICTION_TIME: float = 300.0
    """Maximum reasonable prediction time (seconds)"""

    # Feature names (27 features — optimized from empirical testing)
    # Feature importance ranking (log-target XGBoost, gain-based):
    #   comp_avg_x_species (26%), comp_avg_x_size (19%), gender (9%),
    #   comp_weighted_avg (7%), species_mult (5%), shear (5%)
    FEATURE_NAMES: tuple = (
        # Competitor ability (temporal, leak-free)
        "comp_weighted_avg",  # 1 - Time-decay weighted historical average
        "comp_count",  # 2 - Number of prior results for this event
        "comp_std",  # 3 - Historical performance std dev
        "comp_best",  # 4 - All-time best for this event (NEW)
        "comp_recent",  # 5 - Most recent prior time
        "comp_trend",  # 6 - Linear slope of last 5 results (sec/day)
        "comp_cross_event_avg",  # 7 - Average time in OTHER event (SB<->UH)
        "days_since_last",  # 8 - Days since last competition
        "size_deviation",  # 9 - Target size minus competitor's median size (NEW)
        # Event and competitor attributes
        "event_encoded",  # 10 - 0=SB, 1=UH
        "gender_encoded",  # 11 - 0=F, 1=M (NEW — 9% importance)
        # Wood properties
        "janka_hard",  # 12
        "spec_gravity",  # 13
        "crush_strength",  # 14
        "shear",  # 15 - 5% importance
        "MOR",  # 16
        "MOE",  # 17
        "species_mult",  # 18 - Empirical species time multiplier (NEW — 5%)
        # Block size
        "size_mm",  # 19
        "size_mm_sq",  # 20
        "log_size",  # 21 - log(diameter) (NEW)
        # Interaction features (KEY for accuracy)
        "event_x_size",  # 22 - event_encoded * size_mm
        "species_mult_x_size",  # 23 - species_mult * size_mm (NEW)
        "comp_avg_x_species",  # 24 - comp_weighted_avg * species_mult (NEW — 26% importance!)
        "comp_avg_x_size",  # 25 - comp_weighted_avg * size_mm / 300.0 (NEW — 19% importance!)
        # Seasonal
        "month_sin",  # 26
        "month_cos",  # 27
    )

    # Bayesian optimization parameters (NEW for Phase 2)
    BAYESIAN_OPT_ITERATIONS: int = 50
    """Number of Bayesian optimization iterations for hyperparameter tuning"""

    BAYESIAN_OPT_CV_FOLDS: int = 3
    """CV folds for Bayesian optimization (faster than full 5-fold)"""

    # Event encoding
    EVENT_ENCODING_SB: int = 0
    """Standing Block event encoding"""

    EVENT_ENCODING_UH: int = 1
    """Underhand event encoding"""

    # Trend-based weighting (performance-driven)
    TREND_MIN_SAMPLES: int = 5
    """Minimum samples to estimate a reliable trend slope"""

    TREND_R2_THRESHOLD: float = 0.30
    """Minimum R2 to trust trend-based estimate"""

    TREND_SLOPE_THRESHOLD_SECONDS_PER_DAY: float = 0.005
    """Minimum absolute slope to prefer trend over time-decay weighting"""

    # Per-competitor calibration
    CALIBRATION_MIN_SAMPLES: int = 5
    """Minimum samples to apply per-competitor calibration"""

    CALIBRATION_MAX_STD_SECONDS: float = 4.0
    """Maximum residual std-dev to trust per-competitor calibration"""

    # ML confidence calibration (based on CV MAE)
    ML_MAE_HIGH_CONFIDENCE: float = 3.0
    """Max MAE for HIGH confidence calibration"""

    ML_MAE_MEDIUM_CONFIDENCE: float = 5.0
    """Max MAE for MEDIUM confidence calibration"""


# =============================================================================
# Monte Carlo Simulation Configuration
# =============================================================================


@dataclass(frozen=True)
class SimulationConfig:
    """Monte Carlo simulation parameters"""

    NUM_SIMULATIONS: int = 500_000
    """Number of race simulations to run for high statistical precision"""

    NUM_SIMULATIONS_QUICK: int = 100_000
    """Quick simulation count for fast pre-competition checks"""

    HEAT_VARIANCE_SECONDS: float = 1.0
    """Shared heat-level variance applied to all competitors (wind, grain, conditions)"""

    MIN_COMPETITOR_STD_SECONDS: float = 1.5
    """Minimum per-competitor performance std-dev when historical data is used"""

    MAX_COMPETITOR_STD_SECONDS: float = 15.0
    """Maximum per-competitor performance std-dev when historical data is used"""

    DEFAULT_VARIANCE_SCALING_FACTOR: float = 0.12
    """Fraction of predicted time used as default std-dev when no historical
    variance data exists (e.g., 0.12 = assume 12% of predicted time as
    uncertainty). Empirically validated against AAA competition data."""

    # Fairness assessment thresholds
    FAIRNESS_THRESHOLD_EXCELLENT: float = 0.02
    """Win rate spread threshold for 'Excellent' rating (2%)"""

    FAIRNESS_THRESHOLD_VERY_GOOD: float = 0.05
    """Win rate spread threshold for 'Very Good' rating (5%)"""

    FAIRNESS_THRESHOLD_GOOD: float = 0.10
    """Win rate spread threshold for 'Good' rating (10%)"""

    FAIRNESS_THRESHOLD_FAIR: float = 0.15
    """Win rate spread threshold for 'Fair' rating (15%)"""

    # Visualization settings
    VISUALIZATION_BAR_MAX_LENGTH: int = 40
    """Maximum length of text-based bar charts"""

    # Consistency rating thresholds (per-competitor std-dev in seconds)
    CONSISTENCY_VERY_HIGH_THRESHOLD: float = 2.5
    CONSISTENCY_HIGH_THRESHOLD: float = 3.0
    CONSISTENCY_MODERATE_THRESHOLD: float = 3.5
    # Above 3.5s = Low consistency


# =============================================================================
# Baseline V2 Hybrid Model Configuration
# =============================================================================


@dataclass(frozen=True)
class BaselineConfig:
    """Configuration for Hybrid Baseline V2 model (Phases 1-3)"""

    # Adaptive time-decay weighting (Phase 1)
    # NOTE: half-life values are also in DecayConfig for standalone use by decay.py.
    # BaselineConfig owns the full set; DecayConfig mirrors only the half-life fields
    # so that decay.py has no config dependency on the full baseline model.
    HALF_LIFE_ACTIVE_DAYS: int = 365
    """Half-life for active competitors (5+ results in last 2 years)"""

    HALF_LIFE_MODERATE_DAYS: int = 730
    """Half-life for moderate activity competitors (standard 2-year half-life)"""

    HALF_LIFE_INACTIVE_DAYS: int = 1095
    """Half-life for inactive competitors (3 years - preserve old data longer)"""

    ACTIVITY_WINDOW_DAYS: int = 730
    """Window to assess activity level (2 years)"""

    ACTIVE_MIN_RESULTS: int = 5
    """Minimum results in activity window to be considered 'active'"""

    MODERATE_MIN_RESULTS: int = 2
    """Minimum results in activity window to be considered 'moderate'"""

    # Wood hardness index (Phase 1)
    MIN_SPECIES_SAMPLES: int = 5
    """Minimum samples per species for hardness index regression"""

    MIN_TOTAL_SAMPLES_FOR_INDEX: int = 50
    """Minimum total samples to fit wood hardness index"""

    MIN_SPECIES_VARIETY: int = 3
    """Minimum number of species required for hardness index"""

    # Hierarchical model fitting (Phase 2)
    MIN_DATA_FOR_HIERARCHICAL_MODEL: int = 30
    """Minimum observations to fit hierarchical regression model"""

    DIAMETER_CURVE_MIN_SAMPLES: int = 10
    """Minimum samples to estimate diameter curve"""

    DIAMETER_CURVE_MIN_RANGE_MM: float = 25.0
    """Minimum diameter range to fit curve (mm)"""

    SELECTION_BIAS_DEFAULT_DIAMETER: float = 300.0
    """Default median diameter if no competitor history"""

    # Competitor variance modeling (Phase 2)
    MIN_STD_DEV_SECONDS: float = 1.5
    """Minimum competitor std_dev (floor for elite)"""

    MAX_STD_DEV_SECONDS: float = 15.0
    """Maximum competitor std_dev (ceiling for high-variance)"""

    CONSISTENCY_VERY_HIGH_THRESHOLD: float = 2.5
    """Max std_dev for VERY HIGH consistency rating"""

    CONSISTENCY_HIGH_THRESHOLD: float = 3.0
    """Max std_dev for HIGH consistency rating"""

    CONSISTENCY_MODERATE_THRESHOLD: float = 3.5
    """Max std_dev for MODERATE consistency rating"""

    MIN_SAMPLES_FOR_STD_DEV: int = 3
    """Minimum samples to estimate competitor std_dev"""

    # Convergence calibration layer (Phase 3)
    TARGET_FINISH_TIME_SPREAD_SECONDS: float = 2.0
    """Target finish-time spread after convergence adjustment (killer feature!)"""

    CONVERGENCE_PRESERVE_RANKING: bool = True
    """Preserve skill ranking during convergence adjustment"""

    BIAS_CORRECTION_MIN_SAMPLES: int = 10
    """Minimum samples in diameter bin for bias correction"""

    BIAS_CORRECTION_THRESHOLD_SECONDS: float = 1.0
    """Minimum bias magnitude to trigger correction"""

    SOFT_CONSTRAINT_QUANTILE: float = 0.90
    """Quantile threshold for soft constraint floor (90th percentile)"""

    SOFT_CONSTRAINT_FLOOR_MULTIPLIER: float = 0.95
    """Multiplier for historical floor (95% of 90th percentile)"""

    # Confidence calibration (Phase 2 & 3)
    CONFIDENCE_VERY_HIGH_MIN_WEIGHTED_SAMPLES: int = 10
    """Minimum weighted samples for VERY HIGH confidence"""

    CONFIDENCE_VERY_HIGH_MAX_STD_DEV: float = 2.5
    """Maximum std_dev for VERY HIGH confidence"""

    CONFIDENCE_HIGH_MIN_WEIGHTED_SAMPLES: int = 5
    """Minimum weighted samples for HIGH confidence"""

    CONFIDENCE_HIGH_MAX_STD_DEV: float = 3.5
    """Maximum std_dev for HIGH confidence"""

    CONFIDENCE_MEDIUM_MIN_WEIGHTED_SAMPLES: int = 2
    """Minimum weighted samples for MEDIUM confidence"""

    # Model caching (Phase 4)
    ENABLE_MODEL_CACHE: bool = True
    """Enable global model caching for performance"""

    CACHE_INVALIDATION_ON_DATA_UPDATE: bool = True
    """Invalidate cache when roster/results are updated"""


# =============================================================================
# Time-Decay Configuration (standalone subset for decay.py)
# =============================================================================


@dataclass(frozen=True)
class DecayConfig:
    """
    Exponential time-decay parameters — standalone subset of BaselineConfig.

    Exists so that decay.py can import only what it needs without depending
    on the full baseline model configuration.

    Formula: weight = 0.5 ^ (days_old / half_life_days)
    Standard half-life is 730 days (2 years).
    """

    HALF_LIFE_ACTIVE_DAYS: int = 365
    """Half-life for active competitors (5+ results in last 2 years)"""

    HALF_LIFE_MODERATE_DAYS: int = 730
    """Standard 2-year half-life"""

    HALF_LIFE_INACTIVE_DAYS: int = 1095
    """Half-life for inactive competitors (3 years - preserve old data longer)"""

    ACTIVITY_WINDOW_DAYS: int = 730
    """Window to assess activity level (2 years)"""

    ACTIVE_MIN_RESULTS: int = 5
    """Minimum results in activity window to be considered 'active'"""

    MODERATE_MIN_RESULTS: int = 2
    """Minimum results in activity window to be considered 'moderate'"""


# =============================================================================
# LLM Configuration (Ollama)
# =============================================================================


@dataclass(frozen=True)
class LLMConfig:
    """Ollama + Gemini LLM settings for AI-enhanced predictions"""

    DEFAULT_MODEL: str = "qwen3.5:9b"
    """Default Ollama model (Qwen 3.5 9B — fits 8GB VRAM at Q4_K_M, released Feb 2026)"""

    PREDICTION_MODEL: str = "qwen3.5:9b"
    """Model for time predictions and quality adjustment (same as default)"""

    OLLAMA_URL: str = _env_str("STRATHMARK_OLLAMA_URL", "http://localhost:11434/api/generate")
    """Ollama API endpoint — overridden at runtime via HandicapCalculator(ollama_url=...)
    or at module-import time via STRATHMARK_OLLAMA_URL env var.  Note: prefer the
    newer OLLAMA_HOST env var (host only, no path) which is composed at call time
    by get_ollama_url() — see helpers below."""

    TIMEOUT_SECONDS: int = _env_int("STRATHMARK_OLLAMA_TIMEOUT", 30)
    """Legacy single-value request timeout (kept for backwards-compat with callers
    that pass an int).  The new code path in llm.call_ollama() uses the explicit
    (CONNECT, READ) tuple below, which is what race-day Railway needs."""

    OLLAMA_CONNECT_TIMEOUT: int = _env_int("STRATHMARK_OLLAMA_CONNECT_TIMEOUT", 3)
    """TCP connect timeout in seconds.  3s is enough for a healthy local Ollama
    and short enough that a fail-fast cascade fall-through stays under 5s on
    Railway when Ollama is unreachable."""

    OLLAMA_READ_TIMEOUT: int = _env_int("STRATHMARK_OLLAMA_READ_TIMEOUT", 15)
    """HTTP read timeout in seconds.  Qwen 3.5 9B at Q4_K_M typically responds
    in 1-5s for short prompts; 15s leaves headroom for cold starts without
    blocking the cascade for half a minute."""

    MAX_RETRIES: int = _env_int("STRATHMARK_OLLAMA_MAX_RETRIES", 0)
    """Race-day fail-fast: zero retries by default.  call_ollama() now does a
    single attempt then falls through to Gemini / ML / Baseline.  Retries add
    multi-second latency on race day when Ollama is the unreachable tier."""

    GEMINI_MODEL: str = _env_str("GEMINI_MODEL", "gemini-2.0-flash-lite")
    """Cloud LLM fallback model.  2.0 Flash-Lite is the cheapest tier and is
    fast enough for the race-day cascade.  Only invoked when Ollama returns
    None AND GEMINI_API_KEY is set."""

    GEMINI_CONNECT_TIMEOUT: int = _env_int("GEMINI_CONNECT_TIMEOUT", 5)
    """TCP connect timeout for Gemini cloud calls (5s — slightly longer than
    Ollama because it's a real internet round-trip)."""

    GEMINI_READ_TIMEOUT: int = _env_int("GEMINI_READ_TIMEOUT", 15)
    """HTTP read timeout for Gemini cloud calls."""

    # Token limits for different use cases
    TOKENS_TIME_PREDICTION: int = 150
    """Token limit for quality adjustment JSON response"""

    TOKENS_ANALYSIS_SHORT: int = 200
    """Token limit for short comparative analysis (3-4 sentences)"""

    TOKENS_FAIRNESS_ASSESSMENT: int = 5000
    """Token limit for comprehensive fairness assessment (detailed multi-paragraph analysis)"""

    TOKENS_PREDICTION_ANALYSIS: int = 1000
    """Token limit for comprehensive prediction method analysis (15-20 sentence multi-section analysis)"""

    TOKENS_CHAMPIONSHIP_ANALYSIS: int = 800
    """Token limit for championship race analysis (6-section sports commentary, 2-4 sentences each)"""

    # Quality multiplier bounds (used for LLM response validation)
    QUALITY_MULTIPLIER_MIN: float = 0.85
    """Minimum LLM quality multiplier (quality 1 = very soft/rotten wood)"""

    QUALITY_MULTIPLIER_MAX: float = 1.15
    """Maximum LLM quality multiplier (quality 10 = extremely hard/firm wood)"""


# =============================================================================
# Event Codes
# =============================================================================


@dataclass(frozen=True)
class EventCodes:
    """Valid event type codes"""

    STANDING_BLOCK: str = "SB"
    """Standing Block event code"""

    UNDERHAND: str = "UH"
    """Underhand event code"""

    VALID_EVENTS: tuple = ("SB", "UH")
    """List of all valid event codes"""


# =============================================================================
# Display & UI Configuration
# =============================================================================


@dataclass(frozen=True)
class DisplayConfig:
    """Display and formatting settings for plain-text output"""

    # Column widths for tables
    COMPETITOR_NAME_WIDTH: int = 35
    """Width for competitor name column"""

    TIME_COLUMN_WIDTH: int = 10
    """Width for time display columns"""

    # Separators
    SEPARATOR_LENGTH: int = 70
    """Length of separator lines"""

    # Formatting
    TIME_DECIMAL_PLACES: int = 1
    """Decimal places for time display"""


# =============================================================================
# Confidence Level Strings
# =============================================================================


class ConfidenceLevels:
    """Confidence level string constants"""

    VERY_HIGH: Final[str] = "VERY HIGH"
    HIGH: Final[str] = "HIGH"
    MEDIUM: Final[str] = "MEDIUM"
    LOW: Final[str] = "LOW"
    VERY_LOW: Final[str] = "VERY LOW"


# =============================================================================
# Instantiate Config Objects
# =============================================================================

# Create singleton instances for easy import
rules = Rules()
data_req = DataRequirements()
ml_config = MLConfig()
sim_config = SimulationConfig()
baseline_config = BaselineConfig()
decay_config = DecayConfig()
llm_config = LLMConfig()
events = EventCodes()
display = DisplayConfig()
confidence = ConfidenceLevels()


# =============================================================================
# Helper Functions
# =============================================================================


def get_event_encoding(event_code: str) -> int:
    """
    Get numeric encoding for event type.

    Args:
        event_code: Event code (SB or UH)

    Returns:
        0 for SB, 1 for UH

    Raises:
        ValueError: If event_code is not valid
    """
    event_upper = event_code.upper()
    if event_upper == events.STANDING_BLOCK:
        return ml_config.EVENT_ENCODING_SB
    elif event_upper == events.UNDERHAND:
        return ml_config.EVENT_ENCODING_UH
    else:
        raise ValueError(f"Invalid event code: {event_code}. Must be SB or UH")


def is_valid_event(event_code: str) -> bool:
    """
    Check if event code is valid.

    Args:
        event_code: Event code to validate

    Returns:
        True if valid, False otherwise
    """
    return event_code.upper() in events.VALID_EVENTS


def get_ollama_url() -> str:
    """
    Resolve the Ollama API URL at call time (not import time).

    Resolution order:
        1. STRATHMARK_OLLAMA_URL (legacy full-URL override)
        2. OLLAMA_HOST + "/api/generate" (new host-only env var)
        3. http://localhost:11434/api/generate (default)

    Reading at call time means tests can monkeypatch env vars without
    re-importing the module, and Railway can switch hosts via OLLAMA_HOST
    without a code change (e.g. point at a tunnel URL for Option B).

    Returns:
        Full Ollama generate-endpoint URL, or "" if the LLM tier is disabled.
    """
    if is_ollama_disabled():
        return ""
    legacy = os.environ.get("STRATHMARK_OLLAMA_URL")
    if legacy:
        return legacy
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return f"{host.rstrip('/')}/api/generate"


def is_ollama_disabled() -> bool:
    """
    Race-day kill switch — set OLLAMA_HOST to "" or "disabled" on Railway
    to skip the Ollama tier entirely without touching code.

    The legacy STRATHMARK_OLLAMA_URL env var, if set, always wins and is
    treated as enabled.
    """
    if os.environ.get("STRATHMARK_OLLAMA_URL"):
        return False
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return host == "" or host.strip().lower() == "disabled"


def get_gemini_api_key() -> str:
    """
    Read GEMINI_API_KEY at call time.

    Returns empty string if unset, which signals the cloud LLM tier should
    be skipped.  Never logged — treat as a secret.
    """
    return os.environ.get("GEMINI_API_KEY", "") or ""


def get_confidence_level(num_events: int) -> str:
    """
    Determine confidence level based on number of historical events.

    Args:
        num_events: Number of historical events for competitor

    Returns:
        Confidence level string (HIGH/MEDIUM/LOW)
    """
    if num_events >= data_req.HIGH_CONFIDENCE_MIN_EVENTS:
        return confidence.HIGH
    elif num_events >= data_req.MEDIUM_CONFIDENCE_MIN_EVENTS:
        return confidence.MEDIUM
    else:
        return confidence.LOW
