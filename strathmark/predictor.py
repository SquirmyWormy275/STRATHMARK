"""
Prediction Types and Cascade Orchestration
==========================================

This module defines the data types used across the prediction cascade and the
CompetitorRecord / WoodProfile value objects that callers provide to
HandicapCalculator.

It also exposes the top-level function get_best_prediction() which implements
the full cascade:

    Priority 1 -- Manual override (operator-supplied time)
    Priority 2 -- LLM quality-adjusted baseline (requires Ollama)
    Priority 3 -- ML model (XGBoost trained on historical data)
    Priority 4 -- Weighted baseline (time-decay weighted historical average)
    Priority 5 -- Panel mark fallback (division-based default)

All predictions return a PredictionResult with:
    value       -- predicted time in seconds (float)
    confidence  -- 'VERY HIGH' | 'HIGH' | 'MEDIUM' | 'LOW' | 'VERY LOW'
    method      -- which cascade level produced the result
    explanation -- plain-text description of the reasoning

Source references (STRATHEX):
    woodchopping/predictions/prediction_aggregator.py -> get_all_predictions()
    woodchopping/predictions/prediction_aggregator.py -> select_best_prediction()
    woodchopping/predictions/ai_predictor.py          -> predict_competitor_time_with_ai()
    woodchopping/predictions/baseline.py              -> predict_baseline_v2_hybrid()
    woodchopping/predictions/ml_model.py              -> predict_time_ml()
    woodchopping/predictions/baseline.py               -> get_panel_mark()
"""

from __future__ import annotations

import collections as _collections
import logging
import math
import os
import statistics as _statistics
import threading as _threading
import time as _time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hot-path Supabase circuit breaker for bias correction
# ---------------------------------------------------------------------------
#
# Policy (from docs/ml-persistence-policy.md section 5):
# - Per-process counter, not per-session.
# - On failure, log a warning AND retry on the next prediction.
# - After 3 consecutive failures within a 60-second window, surface a single
#   warning telemetry event (not log spam) and fall back to in-memory bias
#   state for the remainder of the window.
# - After the window, the breaker enters HALF-OPEN: it admits exactly one
#   probe call. If the probe succeeds, the breaker fully closes. If the
#   probe fails, the breaker re-trips and the cooldown restarts. This
#   prevents a single stale-data success from hiding an ongoing outage.
# - Process restart resets all state.

from strathmark.config import (
    data_req,
    is_valid_event,
    llm_config,
    ml_config,
    prediction_config,
    rules,
)
from strathmark.decay import (
    calculate_performance_weight,
    classify_activity_level,
    compute_weighted_average,
    compute_weights_for_results,
    select_half_life,
)
from strathmark.fallback import (
    _standardize_results_df,
    get_event_baseline,
    get_panel_mark,
)
from strathmark.features import resolve_species_properties
from strathmark.wood import (
    calculate_scaling_factor,
    get_event_scaling_exponent,
    get_species_properties,
    get_species_time_multiplier,
)


class _BiasCircuitBreaker:
    """Per-process circuit breaker for the bias-correction Supabase read.

    States:
      CLOSED    -- normal operation. Failures accumulate in a sliding window.
      OPEN      -- tripped. allow() returns False; calls fall back to in-memory.
      HALF_OPEN -- cooldown elapsed. allow() admits exactly ONE probe call.
                   The probe's outcome decides whether to fully close or
                   re-open. While the probe is in flight, allow() returns
                   False so a thundering herd doesn't all probe at once.

    Trips when THRESHOLD failures land within WINDOW_SECONDS. After
    WINDOW_SECONDS elapses post-trip, transitions OPEN -> HALF_OPEN.

    Thread-safe; the lock guards every state transition.
    """

    WINDOW_SECONDS: float = 60.0
    THRESHOLD: int = 3

    # State constants
    _CLOSED = "closed"
    _OPEN = "open"
    _HALF_OPEN = "half_open"

    def __init__(self) -> None:
        self._failures: _collections.deque[float] = _collections.deque()
        self._state: str = self._CLOSED
        self._tripped_at: float = 0.0
        self._probe_in_flight: bool = False
        self._lock = _threading.Lock()

    @property
    def _tripped(self) -> bool:
        # Backwards-compat shim for tests that inspect ._tripped
        return self._state == self._OPEN

    def _purge(self, now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def allow(self) -> bool:
        """Return True if the caller should attempt the protected operation.

        In HALF_OPEN state, exactly one caller per cooldown receives True;
        subsequent callers see False until the probe resolves via
        record_success() or record_failure().
        """
        with self._lock:
            now = _time.monotonic()
            self._purge(now)
            if self._state == self._OPEN:
                if now - self._tripped_at > self.WINDOW_SECONDS:
                    self._state = self._HALF_OPEN
                    self._probe_in_flight = False
                else:
                    return False
            if self._state == self._HALF_OPEN:
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
                return True
            return True

    def record_success(self) -> None:
        """Successful protected call. Resolves a probe (if any) and closes."""
        with self._lock:
            previously_open = self._state in (self._OPEN, self._HALF_OPEN)
            self._failures.clear()
            self._state = self._CLOSED
            self._probe_in_flight = False
            self._tripped_at = 0.0
            if previously_open:
                _log.info("bias circuit breaker: closed after probe success")

    def record_failure(self) -> bool:
        """Log the failure and update state. Returns True if newly opened."""
        with self._lock:
            now = _time.monotonic()
            # If a half-open probe failed, re-open immediately and restart cooldown.
            if self._state == self._HALF_OPEN:
                self._state = self._OPEN
                self._tripped_at = now
                self._probe_in_flight = False
                _log.warning(
                    "bias circuit breaker: probe failed; re-opened for another %.0fs",
                    self.WINDOW_SECONDS,
                )
                return True
            self._purge(now)
            self._failures.append(now)
            if self._state == self._CLOSED and len(self._failures) >= self.THRESHOLD:
                self._state = self._OPEN
                self._tripped_at = now
                _log.warning(
                    "bias circuit breaker: tripped after %d failures in %.0fs window; "
                    "falling back to in-memory bias state for the next %.0fs",
                    self.THRESHOLD,
                    self.WINDOW_SECONDS,
                    self.WINDOW_SECONDS,
                )
                return True
            _log.debug(
                "bias circuit breaker: failure recorded (%d in window)",
                len(self._failures),
            )
        return False

    def reset(self) -> None:
        """Test hook. Clears all state."""
        with self._lock:
            self._failures.clear()
            self._state = self._CLOSED
            self._tripped_at = 0.0
            self._probe_in_flight = False


_bias_breaker = _BiasCircuitBreaker()

# ---------------------------------------------------------------------------
# Value objects provided by the caller
# ---------------------------------------------------------------------------


@dataclass
class HistoricalResult:
    """A single past competition result for one competitor."""

    event_code: str
    """'SB' or 'UH'."""

    time_seconds: float
    """Raw time recorded (seconds)."""

    species: str
    """Wood species used in that event."""

    diameter_mm: float
    """Block diameter (mm)."""

    quality: int
    """Wood quality at time of event (1-10 scale; 5 = average)."""

    result_date: Optional[date] = None
    """Date of competition. None disables time-decay for this result."""

    heat_id: Optional[str] = None
    """Optional identifier linking results to a specific tournament heat."""

    field_strength: Optional[float] = None
    """
    Average handicap mark across all competitors in the same event at the same
    show. Higher values indicate stronger fields. Null for historical results
    predating the field_strength column; used as ML feature #26.
    """


@dataclass
class CompetitorRecord:
    """
    All data about one competitor needed to generate a prediction.

    This is the primary input to HandicapCalculator.calculate() and to the
    lower-level prediction functions.

    Fields mirror the historical data model in woodchopping.xlsx (Results sheet)
    and the competitor roster (Competitor sheet).
    """

    name: str
    """Display name used for all output."""

    history: List[HistoricalResult] = field(default_factory=list)
    """Historical results, any order. Time-decay weighting is applied internally."""

    division: Optional[str] = None
    """
    Competition division. Used for panel mark fallback when no history exists.
    Recognized values: 'Open', 'Novice', 'Junior', 'Veterans', 'Womens'.
    """

    manual_time_override: Optional[float] = None
    """
    Operator-supplied predicted time (seconds). When set, this is used directly
    at the highest cascade priority -- bypasses all prediction logic.
    """

    tournament_time: Optional[float] = None
    """
    Actual time from an earlier round in the SAME tournament on the SAME wood.
    Weight applied depends on num_tournament_rounds (see graduated weighting below).
    Confidence is upgraded to VERY HIGH.
    """

    num_tournament_rounds: int = 1
    """
    Number of rounds already completed in this tournament for this event.
    Used to graduate the tournament weight:
        1 round -> 65%  (single data point, still uncertain)
        2 rounds -> 80%
        3 rounds -> 90%
        4+ rounds -> 97%
    Only used when tournament_time is set.
    """

    personal_scaling_exponent: Optional[float] = None
    """
    Per-competitor power-law diameter scaling exponent fitted from their own
    multi-diameter history. None until computed; cached after first call to
    predict_baseline() when the competitor has 3+ results across 2+ distinct
    diameters. Falls back to the event-wide calibrated exponent (or 1.4) when None.
    """

    gender: Optional[str] = None
    """
    Competitor gender ('M' or 'F'). Used as ML feature #11.
    Falls back to 0 (female encoding) when not set, which is the safe
    direction (female times are typically longer, so defaulting to 0
    produces slightly conservative predictions for unknown-gender competitors).
    """

    competitor_id: Optional[str] = None
    """Stable identity for V2 population state and trusted persistence.

    This field is intentionally appended so all historical positional
    constructors remain valid. ``name`` remains a request-local display value.
    """


@dataclass
class WoodProfile:
    """
    Wood characteristics for a single event.

    These values drive diameter scaling, species-hardness lookup, and
    the quality multiplier applied during prediction.
    """

    species: str
    """
    Species name matching the wood data table (e.g., 'Pine', 'Cottonwood').
    Used to look up Janka hardness, specific gravity, shear/crush strength, MOR, MOE.
    """

    diameter_mm: float
    """
    Block diameter in mm. Valid range: 225-500 mm.
    Time scales approximately as diameter^1.4 (calibrated from historical data).
    """

    quality: int
    """
    Wood firmness rating 1-10.
        1-3  = Soft/rotten (faster times, multiplier ~0.85-0.92)
        4-6  = Average firmness (baseline reference; 5 = no adjustment)
        7-10 = Above average / very hard (slower times, multiplier ~1.05-1.15)
    """


# ---------------------------------------------------------------------------
# Prediction result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredictionContext:
    """Request-level context shared by every prediction in one field.

    ``prediction_as_of`` is an exclusive UTC date cutoff. ``None`` lets the V2
    boundary resolve the current UTC date once when a request begins.
    """

    prediction_as_of: Optional[date] = None
    request_id: Optional[str] = None
    seed: int = 20260811
    engine: Optional[str] = None


@dataclass(frozen=True)
class PredictionInterval:
    """Forecast interval, separate from race-performance ``std_dev``."""

    lower: float
    upper: float
    nominal_coverage: float = 0.90
    calibration_state: str = "uncalibrated"
    scope: str = "analytic"

    def __post_init__(self) -> None:
        if not math.isfinite(self.lower) or not math.isfinite(self.upper):
            raise ValueError("prediction interval bounds must be finite")
        if self.lower <= 0 or self.upper <= 0 or self.lower > self.upper:
            raise ValueError("prediction interval must have 0 < lower <= upper")
        if not math.isfinite(self.nominal_coverage) or not 0 < self.nominal_coverage < 1:
            raise ValueError("nominal_coverage must be between 0 and 1")


@dataclass
class PredictionResult:
    """Output of a single prediction method."""

    value: float
    """Predicted time in seconds."""

    confidence: str
    """'VERY HIGH' | 'HIGH' | 'MEDIUM' | 'LOW' | 'VERY LOW'."""

    method: str
    """Which cascade level: 'manual' | 'llm' | 'ml' | 'baseline' | 'panel'."""

    explanation: str
    """Plain-text explanation of how this prediction was derived."""

    metadata: Dict = field(default_factory=dict)
    """
    Optional structured metadata from the prediction method.
    For baseline predictions: includes 'std_dev' (competitor variance estimate).
    For LLM predictions: includes raw multiplier and quality context.
    """

    interval: Optional[PredictionInterval] = None
    """Calibrated forecast uncertainty; never performance variability."""

    engine_version: Optional[str] = None
    model_version: Optional[str] = None
    calibration_version: Optional[str] = None
    evidence_cutoff: Optional[date] = None
    prediction_id: Optional[str] = None
    provenance: Dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    ignored_factors: List[str] = field(default_factory=list)
    degraded: bool = False


@dataclass(frozen=True)
class PredictionBundle:
    """Immutable runtime snapshot used for every prediction in one request."""

    core: Any = None
    residual: Any = None
    source: str = "broad_prior"
    warnings: tuple[str, ...] = ()
    degraded: bool = False

    @property
    def core_version(self) -> Optional[str]:
        return getattr(self.core, "model_version", None)

    @property
    def calibration_version(self) -> Optional[str]:
        calibration = getattr(self.core, "calibration", None)
        return getattr(calibration, "version", None)

    @property
    def residual_version(self) -> Optional[str]:
        loaded = getattr(self.residual, "loaded", None)
        manifest = getattr(loaded, "manifest", {}) or {}
        return manifest.get("model_version")

    def health(self, prediction_as_of: date) -> Dict[str, Any]:
        residual_loaded = getattr(self.residual, "loaded", None)
        residual_active = bool(getattr(residual_loaded, "active", False))
        calibration_version = self.calibration_version
        calibration_available = bool(calibration_version and calibration_version != "uncalibrated")
        warnings = list(self.warnings)
        if self.core is not None and not calibration_available:
            warnings.append("calibration_unavailable")
        return {
            "core": {"available": self.core is not None, "version": self.core_version},
            "residual": {
                "available": residual_loaded is not None,
                "active": residual_active,
                "version": self.residual_version,
            },
            "calibration": {
                "available": calibration_available,
                "version": calibration_version,
            },
            "cutoff": prediction_as_of.isoformat(),
            "source": self.source,
            "warnings": list(dict.fromkeys(warnings)),
            "degraded": bool(self.degraded or self.core is None or not calibration_available),
        }


class PredictionEngineProvider:
    """Interface for atomically obtaining one immutable engine snapshot."""

    def snapshot(self, prediction_as_of: date) -> PredictionBundle:
        raise NotImplementedError


class StaticPredictionProvider(PredictionEngineProvider):
    """Explicit provider injection for Python callers and deterministic tests."""

    def __init__(self, bundle: PredictionBundle):
        self._bundle = bundle

    def snapshot(self, prediction_as_of: date) -> PredictionBundle:
        del prediction_as_of
        return self._bundle


class FilePredictionProvider(PredictionEngineProvider):
    """Thread-safe artifact loader with environment, local, package precedence."""

    def __init__(self) -> None:
        self._lock = _threading.RLock()
        self._cache_key: Optional[tuple[Any, ...]] = None
        self._bundle: Optional[PredictionBundle] = None

    def snapshot(self, prediction_as_of: date) -> PredictionBundle:
        from strathmark.features import normalize_prediction_as_of

        cutoff = normalize_prediction_as_of(prediction_as_of)
        core_path, source = _resolve_core_artifact_path()
        residual_path = _resolve_residual_artifact_path(core_path)
        cache_key = (
            str(core_path) if core_path else None,
            _path_signature(core_path),
            str(residual_path) if residual_path else None,
            _path_signature(residual_path),
            cutoff,
        )
        with self._lock:
            if self._cache_key == cache_key and self._bundle is not None:
                return self._bundle
            bundle = _load_prediction_bundle(core_path, residual_path, source, cutoff)
            self._cache_key = cache_key
            self._bundle = bundle
            return bundle


_prediction_provider: PredictionEngineProvider = FilePredictionProvider()


def get_prediction_provider() -> PredictionEngineProvider:
    """Return the process-level thread-safe runtime provider."""

    return _prediction_provider


def _resolve_core_artifact_path() -> tuple[Optional[Path], str]:
    configured = os.environ.get(prediction_config.CORE_ARTIFACT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser(), "environment"
    for candidate in prediction_config.LOCAL_CORE_PATHS:
        path = Path(candidate)
        if path.is_file():
            return path, "local"
    package_path = Path(__file__).resolve().parent / prediction_config.PACKAGE_CORE_PATH
    if package_path.is_file():
        return package_path, "package"
    return None, "broad_prior"


def _resolve_residual_artifact_path(core_path: Optional[Path]) -> Optional[Path]:
    configured = os.environ.get(prediction_config.RESIDUAL_ARTIFACT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    if core_path is not None:
        sibling = core_path.parent / "residual"
        if sibling.is_dir():
            return sibling
    local = Path("models/prediction_v2/residual")
    return local if local.is_dir() else None


def _path_signature(path: Optional[Path]) -> Optional[tuple[int, int]]:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _load_prediction_bundle(
    core_path: Optional[Path],
    residual_path: Optional[Path],
    source: str,
    prediction_as_of: date,
) -> PredictionBundle:
    from strathmark.prediction_v2 import PredictionV2Model

    if core_path is None:
        return PredictionBundle(
            source="broad_prior",
            warnings=("core_artifact_missing", "residual_artifact_missing"),
            degraded=True,
        )
    try:
        core = PredictionV2Model.from_json(core_path.read_bytes())
    except (OSError, ValueError, TypeError):
        return PredictionBundle(
            source=source,
            warnings=("core_artifact_invalid", "residual_artifact_unavailable"),
            degraded=True,
        )

    from strathmark.residual import ResidualRuntime, load_residual_artifact

    loaded = load_residual_artifact(
        residual_path,
        expected_core_checksum=core.source_checksum,
        prediction_as_of=prediction_as_of,
    )
    residual = ResidualRuntime(loaded)
    warnings: list[str] = []
    if loaded.warning:
        warnings.append(loaded.warning)
    if core.calibration.version == "uncalibrated":
        warnings.append("calibration_unavailable")
    return PredictionBundle(
        core=core,
        residual=residual,
        source=source,
        warnings=tuple(warnings),
        degraded=bool(loaded.degraded or core.calibration.version == "uncalibrated"),
    )


# ---------------------------------------------------------------------------
# Calibration classes (from STRATHEX calibration.py)
# ---------------------------------------------------------------------------


class IsotonicCalibrator:
    """
    Isotonic regression calibrator for fixing systematic prediction bias.

    Learns a monotonic mapping from predicted times to actual times using
    validation data, correcting systematic over/under-prediction.
    """

    def __init__(self):
        self.calibrator_sb = None
        self.calibrator_uh = None
        self.is_fitted = False

    def fit(self, predictions: np.ndarray, actuals: np.ndarray, events: np.ndarray):
        """
        Fit isotonic calibrators for SB and UH events.

        Args:
            predictions: Predicted times
            actuals: Actual times
            events: Event codes ('SB' or 'UH')
        """
        try:
            from sklearn.isotonic import IsotonicRegression
        except ImportError:
            return

        # Fit SB calibrator
        sb_mask = events == "SB"
        if sb_mask.sum() > 10:
            self.calibrator_sb = IsotonicRegression(out_of_bounds="clip")
            self.calibrator_sb.fit(predictions[sb_mask], actuals[sb_mask])

        # Fit UH calibrator
        uh_mask = events == "UH"
        if uh_mask.sum() > 10:
            self.calibrator_uh = IsotonicRegression(out_of_bounds="clip")
            self.calibrator_uh.fit(predictions[uh_mask], actuals[uh_mask])

        self.is_fitted = True

    def calibrate(self, prediction: float, event_code: str) -> float:
        """Apply isotonic calibration to prediction"""
        if not self.is_fitted:
            return prediction

        if event_code == "SB" and self.calibrator_sb is not None:
            return float(self.calibrator_sb.predict([prediction])[0])
        elif event_code == "UH" and self.calibrator_uh is not None:
            return float(self.calibrator_uh.predict([prediction])[0])
        else:
            return prediction


class VarianceScaler:
    """
    Predict competitor-specific variance (uncertainty) using XGBoost.

    Trains on absolute residuals to predict std_dev per competitor,
    replacing uniform +/-3s assumption with data-driven uncertainty.
    """

    def __init__(self):
        self.scaler_sb = None
        self.scaler_uh = None
        self.is_fitted = False

    def fit(
        self,
        predictions: np.ndarray,
        actuals: np.ndarray,
        events: np.ndarray,
        competitor_features: pd.DataFrame,
    ):
        """
        Fit variance scalers for SB and UH events.

        Args:
            predictions: Predicted times
            actuals: Actual times
            events: Event codes
            competitor_features: DataFrame with competitor characteristics
        """
        try:
            import xgboost as xgb
        except ImportError:
            return

        residuals = np.abs(predictions - actuals)

        for event in ["SB", "UH"]:
            mask = events == event
            if mask.sum() < 30:
                continue

            X = competitor_features[mask]
            y = residuals[mask]

            model = xgb.XGBRegressor(
                n_estimators=50, max_depth=3, learning_rate=0.1, random_state=42
            )
            model.fit(X, y)

            if event == "SB":
                self.scaler_sb = model
            else:
                self.scaler_uh = model

        self.is_fitted = True

    def predict_std_dev(
        self,
        competitor_features: Dict[str, float],
        event_code: str,
        baseline_std: float = 3.0,
    ) -> float:
        """
        Predict competitor-specific std_dev.

        Args:
            competitor_features: Dict of feature values
            event_code: 'SB' or 'UH'
            baseline_std: Fallback std_dev if model unavailable

        Returns:
            Predicted std_dev clamped to [1.5s, 6.0s]
        """
        if not self.is_fitted:
            return baseline_std

        model = self.scaler_sb if event_code == "SB" else self.scaler_uh
        if model is None:
            return baseline_std

        try:
            X = pd.DataFrame([competitor_features])
            predicted_std = model.predict(X)[0]
            final_std = predicted_std  # allow lower than baseline for consistent competitors
            from strathmark.config import sim_config

            final_std = max(
                sim_config.MIN_COMPETITOR_STD_SECONDS,
                min(final_std, sim_config.MAX_COMPETITOR_STD_SECONDS),
            )
            return float(final_std)
        except Exception:
            return baseline_std


# ---------------------------------------------------------------------------
# ML Model class (ported from STRATHEX ml_model.py)
# ---------------------------------------------------------------------------


class MLModel:
    """
    XGBoost-based time prediction model for Standing Block and Underhand events.

    Separate models are trained for each event type. Time-decay weighting
    is applied to training samples so recent results influence the model more.
    """

    def __init__(self):
        self._models: Dict[str, object] = {}  # 'SB' and/or 'UH' -> xgb model
        self._calibrator: IsotonicCalibrator = IsotonicCalibrator()
        self._training_data_size: int = 0
        self._cv_metrics: Dict[str, Optional[dict]] = {"SB": None, "UH": None}
        self._is_trained: bool = False

    def train(
        self,
        results_df: pd.DataFrame,
        wood_df: Optional[pd.DataFrame] = None,
        verbose: bool = False,
    ) -> bool:
        """
        Train XGBoost models for SB and UH events.

        Applies time-decay sample weighting so recent training examples
        influence the model more than old ones.

        Args:
            results_df: Historical results DataFrame (standardized columns).
            wood_df: Wood properties DataFrame (for Janka hardness etc.).
            verbose: If True, print training progress.

        Returns:
            True if at least one model was trained, False otherwise.
        """
        try:
            import xgboost as xgb
            from sklearn.metrics import mean_absolute_error, r2_score
        except ImportError:
            if verbose:
                print("Warning: XGBoost/scikit-learn not available. ML predictions disabled.")
            return False

        if results_df is None or results_df.empty:
            return False

        df = _standardize_results_df(results_df)
        if df is None or len(df) < data_req.MIN_ML_TRAINING_RECORDS_TOTAL:
            return False

        # Engineer features
        df_eng = self._engineer_features(df, wood_df)
        if df_eng is None or len(df_eng) < data_req.MIN_ML_TRAINING_RECORDS_TOTAL:
            return False

        feature_cols = list(ml_config.FEATURE_NAMES)
        missing = [c for c in feature_cols if c not in df_eng.columns]
        if missing:
            if verbose:
                print(f"Warning: Missing feature columns: {missing}")
            return False

        # Combined model (both events together — validated: combined beats per-event)
        X = df_eng[feature_cols]
        y_raw = df_eng["raw_time"]
        y_log = np.log(y_raw)

        # Remove rows with NaN values
        mask = ~(X.isna().any(axis=1) | y_raw.isna())
        X = X[mask]
        y_log = y_log[mask]
        y_raw = y_raw[mask]

        if len(X) < data_req.MIN_ML_TRAINING_RECORDS_TOTAL:
            if verbose:
                print(
                    f"Insufficient data: {len(X)} rows (need {data_req.MIN_ML_TRAINING_RECORDS_TOTAL})"
                )
            return False

        # Hard minimum: 5 samples per feature to prevent severe overfitting
        n_features = len(feature_cols)
        if len(X) < n_features * 5:
            if verbose:
                print(f"Insufficient data: {len(X)} rows for {n_features} features")
            return False

        # Train XGBoost on log(time)
        xgb_params = {
            "n_estimators": ml_config.N_ESTIMATORS,
            "max_depth": ml_config.MAX_DEPTH,
            "learning_rate": ml_config.LEARNING_RATE,
            "random_state": ml_config.RANDOM_STATE,
            "objective": ml_config.OBJECTIVE,
            "tree_method": ml_config.TREE_METHOD,
            "subsample": ml_config.SUBSAMPLE,
            "colsample_bytree": ml_config.COLSAMPLE_BYTREE,
            "min_child_weight": ml_config.MIN_CHILD_WEIGHT,
            "reg_alpha": ml_config.REG_ALPHA,
            "reg_lambda": ml_config.REG_LAMBDA,
        }
        import xgboost as xgb_lib

        xgb_model = xgb_lib.XGBRegressor(**xgb_params)
        xgb_model.fit(X, y_log)  # NOTE: log(time) target, NO sample weights
        self._models["xgb"] = xgb_model

        # Train LightGBM on log(time)
        try:
            import lightgbm as lgb_lib

            lgb_params = {
                "n_estimators": ml_config.LGB_N_ESTIMATORS,
                "max_depth": ml_config.LGB_MAX_DEPTH,
                "learning_rate": ml_config.LGB_LEARNING_RATE,
                "subsample": ml_config.LGB_SUBSAMPLE,
                "colsample_bytree": ml_config.LGB_COLSAMPLE_BYTREE,
                "min_child_samples": ml_config.LGB_MIN_CHILD_SAMPLES,
                "num_leaves": ml_config.LGB_NUM_LEAVES,
                "reg_alpha": ml_config.LGB_REG_ALPHA,
                "reg_lambda": ml_config.LGB_REG_LAMBDA,
                "random_state": ml_config.RANDOM_STATE,
                "verbose": -1,
            }
            lgb_model = lgb_lib.LGBMRegressor(**lgb_params)
            lgb_model.fit(X, y_log)
            self._models["lgb"] = lgb_model
        except ImportError:
            if verbose:
                print("LightGBM not available; using XGBoost only.")

        trained_any = True

        if verbose:
            # Report in-sample accuracy on raw (exponentiated) predictions
            xgb_preds = np.exp(xgb_model.predict(X))
            mae = mean_absolute_error(y_raw, xgb_preds)
            r2 = r2_score(y_raw, xgb_preds)
            print(f"Trained combined model: {len(X)} records (XGB MAE: {mae:.2f}s, R2: {r2:.3f})")
            if "lgb" in self._models:
                lgb_preds = np.exp(self._models["lgb"].predict(X))
                mae_lgb = mean_absolute_error(y_raw, lgb_preds)
                print(f"  LGB MAE: {mae_lgb:.2f}s")

        # Fit isotonic calibrator on in-sample residuals
        if trained_any:
            predictions_raw = np.exp(xgb_model.predict(X))
            actuals_raw = y_raw.values if hasattr(y_raw, "values") else y_raw
            event_labels = df_eng.loc[
                mask.values if hasattr(mask, "values") else mask, "event"
            ].values
            self._calibrator.fit(
                predictions_raw,
                actuals_raw,
                event_labels,
            )

        self._training_data_size = len(df)
        self._is_trained = trained_any
        return trained_any

    def _engineer_features(
        self,
        df: pd.DataFrame,
        wood_df: Optional[pd.DataFrame] = None,
    ) -> Optional[pd.DataFrame]:
        """
        Build the 23-feature vector from the results DataFrame.
        Matches STRATHEX ml_model.py engineer_features_for_ml().
        """
        if df is None or df.empty:
            return None

        df = df.copy()
        required = {"competitor_name", "event", "raw_time"}
        if not required.issubset(df.columns):
            return None

        df["raw_time"] = pd.to_numeric(df["raw_time"], errors="coerce")
        df = df.dropna(subset=["raw_time"])
        df = df[df["raw_time"] > 0]

        if len(df) == 0:
            return None

        # Wood properties lookup
        wood_props_cache: Dict[str, Dict] = {}

        def _get_wood_props(species_code):
            if species_code in wood_props_cache:
                return wood_props_cache[species_code]
            props = get_species_properties(str(species_code), wood_df)
            result = {
                "janka": props.janka_hardness,
                "spec_grav": props.specific_gravity,
                "shear": props.shear_strength,
                "crush": props.crush_strength,
                "mor": props.mor,
                "moe": props.moe,
            }
            wood_props_cache[species_code] = result
            return result

        # Compute competitor averages by event (time-decay weighted) -- used for cross-event features
        comp_event_avg: Dict[str, float] = {}
        for (comp, event), group in df.groupby(["competitor_name", "event"]):
            if "date" in group.columns:
                weights = group["date"].apply(
                    lambda d: calculate_performance_weight(d, half_life_days=730)
                )
                w_sum = weights.sum()
                if w_sum > 0:
                    avg = float((group["raw_time"] * weights).sum() / w_sum)
                else:
                    avg = float(group["raw_time"].mean())
            else:
                avg = float(group["raw_time"].mean())
            comp_event_avg[f"{comp}||{event}"] = avg

        rows = []
        for _, row in df.iterrows():
            comp = row.get("competitor_name", "")
            event = row.get("event", "")
            species = str(row.get("species", "")).strip()
            diameter = float(row.get("size_mm", 300.0) or 300.0)
            quality = int(row.get("quality", 5) or 5)
            quality = max(1, min(10, quality))

            # Wood properties
            wp = _get_wood_props(species) if species else _get_wood_props("")

            # Competitor average time for this event
            comp_avg = comp_event_avg.get(f"{comp}||{event}", float(df["raw_time"].mean()))

            # Experience (number of results)
            comp_event_data = df[(df["competitor_name"] == comp) & (df["event"] == event)]
            experience = len(comp_event_data)

            # Trend slope
            trend_slope = 0.0
            if (
                "date" in comp_event_data.columns
                and len(comp_event_data) >= ml_config.TREND_MIN_SAMPLES
            ):
                try:
                    dates = pd.to_datetime(comp_event_data["date"], errors="coerce")
                    x = (dates - dates.min()).dt.days.astype(float)
                    y = pd.to_numeric(comp_event_data["raw_time"], errors="coerce").astype(float)
                    valid = np.isfinite(x) & np.isfinite(y)
                    x = x[valid]
                    y = y[valid]
                    if len(x) >= 2 and x.nunique() >= 2:
                        slope, _ = np.polyfit(x, y, 1)
                        trend_slope = float(slope)
                except Exception:
                    trend_slope = 0.0

            # Competitor variance
            comp_variance = (
                float(pd.to_numeric(comp_event_data["raw_time"], errors="coerce").std())
                if len(comp_event_data) > 1
                else 3.0
            )
            if not np.isfinite(comp_variance):
                comp_variance = 3.0

            # Competitor median diameter
            comp_median_diam = (
                float(
                    pd.to_numeric(
                        comp_event_data.get("size_mm", pd.Series([300.0])), errors="coerce"
                    ).median()
                )
                if "size_mm" in comp_event_data.columns
                else diameter
            )
            if not np.isfinite(comp_median_diam):
                comp_median_diam = diameter

            # Competitor best time for this event
            comp_best = (
                float(pd.to_numeric(comp_event_data["raw_time"], errors="coerce").min())
                if len(comp_event_data) > 0
                else comp_avg
            )
            if not np.isfinite(comp_best):
                comp_best = comp_avg

            # Most recent result and days since last
            comp_recent = comp_avg
            days_since_last = 365.0
            if "date" in comp_event_data.columns:
                dated = comp_event_data.dropna(subset=["date"]).sort_values("date")
                if len(dated) > 0:
                    comp_recent = float(dated.iloc[-1]["raw_time"])
                    if len(dated) >= 2:
                        days_since_last = float(
                            (dated.iloc[-1]["date"] - dated.iloc[-2]["date"]).days
                        )
            days_since_last = max(0.0, min(1000.0, days_since_last))

            # Size deviation from competitor's median
            size_deviation = diameter - comp_median_diam

            # Gender encoding
            gender_val = row.get("gender", row.get("Gender", ""))
            gender_encoded = 1.0 if str(gender_val).strip().upper() == "M" else 0.0

            # Species time multiplier
            from strathmark.wood import get_species_time_multiplier as _get_sp_mult_eng

            species_mult = _get_sp_mult_eng(species) if species else 1.0

            # Seasonal encoding
            month = 7
            if "date" in comp_event_data.columns:
                latest = pd.to_datetime(comp_event_data["date"], errors="coerce").dropna()
                if not latest.empty:
                    month = int(latest.max().month)
            month_rad = (month - 1) * (2 * np.pi / 12)

            # Event encoding
            event_enc = (
                ml_config.EVENT_ENCODING_SB if event == "SB" else ml_config.EVENT_ENCODING_UH
            )

            feat = {
                # Competitor ability
                "comp_weighted_avg": comp_avg,
                "comp_count": float(experience),
                "comp_std": comp_variance,
                "comp_best": comp_best,
                "comp_recent": comp_recent,
                "comp_trend": trend_slope,
                "comp_cross_event_avg": comp_event_avg.get(
                    f"{comp}||{'UH' if event == 'SB' else 'SB'}",
                    comp_avg,  # fall back to same-event avg when peer event missing
                ),
                "days_since_last": days_since_last,
                "size_deviation": size_deviation,
                # Event and competitor attributes
                "event_encoded": float(event_enc),
                "gender_encoded": gender_encoded,
                # Wood properties
                "janka_hard": wp["janka"],
                "spec_gravity": wp["spec_grav"],
                "crush_strength": wp["crush"],
                "shear": wp["shear"],
                "MOR": wp["mor"],
                "MOE": wp["moe"],
                "species_mult": species_mult,
                # Block size
                "size_mm": diameter,
                "size_mm_sq": diameter**2,
                "log_size": float(np.log(diameter)),
                # Interaction features
                "event_x_size": float(event_enc) * diameter,
                "species_mult_x_size": species_mult * diameter,
                "comp_avg_x_species": comp_avg * species_mult,
                "comp_avg_x_size": comp_avg * diameter / 300.0,
                # Seasonal
                "month_sin": float(np.sin(month_rad)),
                "month_cos": float(np.cos(month_rad)),
                # Pass-through columns needed after feature engineering
                "competitor_name": comp,
                "event": event,
                "raw_time": float(row["raw_time"]),
            }
            if "date" in row:
                feat["date"] = row["date"]
            rows.append(feat)

        if not rows:
            return None

        return pd.DataFrame(rows)

    def predict(
        self,
        competitor: CompetitorRecord,
        wood: WoodProfile,
        event_code: str,
        results_df: Optional[pd.DataFrame] = None,
    ) -> Optional[PredictionResult]:
        """
        Predict time using the trained ML model.

        Builds the 23-feature vector from competitor history and wood profile,
        then calls the event-specific XGBoost model.

        Args:
            competitor: Competitor record with historical data.
            wood: Wood profile for this event.
            event_code: 'SB' or 'UH'.
            results_df: Optional full results DataFrame (used for training if not yet trained).

        Returns:
            PredictionResult or None if ML is unavailable.
        """
        event_upper = str(event_code).strip().upper()

        if not self._is_trained or "xgb" not in self._models:
            # Try to train on the fly if results_df provided
            if results_df is not None and not self._is_trained:
                self.train(results_df)
            if not self._is_trained or "xgb" not in self._models:
                return None

        # Build features from competitor history
        if not competitor.history:
            return None

        # Calculate time-decay weighted average
        event_history = [h for h in competitor.history if h.event_code.upper() == event_upper]
        all_history = competitor.history

        if event_history:
            times = [h.time_seconds for h in event_history]
            dates = [h.result_date for h in event_history]
            weights = compute_weights_for_results(dates, adaptive=True)
            try:
                comp_avg = compute_weighted_average(times, weights)
            except Exception:
                comp_avg = float(np.mean(times))
            experience = len(event_history)
        elif all_history:
            comp_avg = float(np.mean([h.time_seconds for h in all_history]))
            experience = len(all_history)
        else:
            return None

        # Trend slope
        trend_slope = 0.0
        if len(event_history) >= ml_config.TREND_MIN_SAMPLES:
            try:
                hist_with_dates = [
                    (h.time_seconds, h.result_date)
                    for h in event_history
                    if h.result_date is not None
                ]
                if len(hist_with_dates) >= ml_config.TREND_MIN_SAMPLES:
                    base_date = min(d for _, d in hist_with_dates)
                    x = np.array([(d - base_date).days for _, d in hist_with_dates], dtype=float)
                    y = np.array([t for t, _ in hist_with_dates], dtype=float)
                    if x.nunique() >= 2 if hasattr(x, "nunique") else len(np.unique(x)) >= 2:
                        slope, _ = np.polyfit(x, y, 1)
                        trend_slope = float(slope)
            except Exception:
                trend_slope = 0.0

        # Competitor variance
        history_times = [h.time_seconds for h in (event_history or all_history)]
        comp_variance = float(np.std(history_times)) if len(history_times) > 1 else 3.0
        if not np.isfinite(comp_variance):
            comp_variance = 3.0

        # Competitor median diameter
        diameters = [h.diameter_mm for h in (event_history or all_history) if h.diameter_mm]
        comp_median_diam = float(np.median(diameters)) if diameters else wood.diameter_mm

        # Recency score
        recency_score = 365.0
        all_dates = sorted([h.result_date for h in all_history if h.result_date is not None])
        if len(all_dates) >= 2:
            deltas = [(all_dates[i + 1] - all_dates[i]).days for i in range(len(all_dates) - 1)]
            if deltas:
                recency_score = float(deltas[-1])
        recency_score = max(0.0, min(1000.0, recency_score))

        # Career phase
        if trend_slope > 0.01:
            career_phase = -1
        elif trend_slope < -0.01:
            career_phase = 1
        else:
            career_phase = 0

        # Seasonal encoding (most recent result date)
        month = 7
        if all_dates:
            month = all_dates[-1].month

        month_rad = (month - 1) * (2 * np.pi / 12)
        event_enc = (
            ml_config.EVENT_ENCODING_SB if event_upper == "SB" else ml_config.EVENT_ENCODING_UH
        )

        # Cross-event features: peer event average (SB<->UH correlation)
        peer_event_code = "UH" if event_upper == "SB" else "SB"
        peer_history = [h for h in all_history if h.event_code.upper() == peer_event_code]
        if peer_history:
            peer_times = [h.time_seconds for h in peer_history]
            peer_dates = [h.result_date for h in peer_history]
            peer_weights = compute_weights_for_results(peer_dates, adaptive=True)
            try:
                peer_avg = compute_weighted_average(peer_times, peer_weights)
            except Exception:
                peer_avg = float(np.mean(peer_times))
        else:
            peer_avg = comp_avg  # fall back to same-event avg

        sb_avg = comp_avg if event_upper == "SB" else peer_avg
        uh_avg = comp_avg if event_upper == "UH" else peer_avg
        uh_to_sb_ratio = (uh_avg / sb_avg) if sb_avg > 0 else 1.0

        # Wood properties
        props = get_species_properties(wood.species)
        quality = max(1, min(10, int(wood.quality)))

        # Species time multiplier
        species_mult = get_species_time_multiplier(wood.species)

        # Competitor's best time for this event
        comp_best = min(h.time_seconds for h in event_history) if event_history else comp_avg

        # Most recent result and days since last
        dated_history = sorted(
            [h for h in event_history if h.result_date is not None],
            key=lambda h: h.result_date,
        )
        comp_recent = dated_history[-1].time_seconds if dated_history else comp_avg
        if len(dated_history) >= 2:
            days_since_last = (dated_history[-1].result_date - dated_history[-2].result_date).days
        else:
            days_since_last = 365.0
        days_since_last = max(0.0, min(1000.0, float(days_since_last)))

        # Size deviation from competitor's median
        size_deviation = float(wood.diameter_mm) - comp_median_diam

        # Gender (from competitor.gender attribute)
        # Default to 0 (female) if unknown — safe fallback
        gender_encoded = 0.0
        if hasattr(competitor, "gender"):
            gender_encoded = 1.0 if str(competitor.gender).strip().upper() == "M" else 0.0

        feature_payload = {
            "comp_weighted_avg": comp_avg,
            "comp_count": float(experience),
            "comp_std": comp_variance,
            "comp_best": comp_best,
            "comp_recent": comp_recent,
            "comp_trend": trend_slope,
            "comp_cross_event_avg": float(peer_avg),
            "days_since_last": days_since_last,
            "size_deviation": size_deviation,
            "event_encoded": float(event_enc),
            "gender_encoded": gender_encoded,
            "janka_hard": props.janka_hardness,
            "spec_gravity": props.specific_gravity,
            "crush_strength": props.crush_strength,
            "shear": props.shear_strength,
            "MOR": props.mor,
            "MOE": props.moe,
            "species_mult": species_mult,
            "size_mm": float(wood.diameter_mm),
            "size_mm_sq": float(wood.diameter_mm) ** 2,
            "log_size": float(np.log(wood.diameter_mm)),
            "event_x_size": float(event_enc) * float(wood.diameter_mm),
            "species_mult_x_size": species_mult * float(wood.diameter_mm),
            "comp_avg_x_species": comp_avg * species_mult,
            "comp_avg_x_size": comp_avg * float(wood.diameter_mm) / 300.0,
            "month_sin": float(np.sin(month_rad)),
            "month_cos": float(np.cos(month_rad)),
        }

        feature_cols = list(ml_config.FEATURE_NAMES)
        features = pd.DataFrame([feature_payload])[feature_cols]

        try:
            # Predict in log-space, then exponentiate
            xgb_model = self._models["xgb"]
            xgb_log_pred = float(xgb_model.predict(features)[0])

            lgb_model = self._models.get("lgb")
            if lgb_model is not None:
                lgb_log_pred = float(lgb_model.predict(features)[0])
                # Geometric mean (average in log space)
                log_pred = (xgb_log_pred + lgb_log_pred) / 2.0
            else:
                log_pred = xgb_log_pred

            raw_prediction = np.exp(log_pred)

            # Apply isotonic calibration if available
            base_prediction = self._calibrator.calibrate(raw_prediction, event_upper)

            # Apply quality adjustment (+-2% per quality point from 5)
            quality_offset = quality - 5
            quality_factor = 1.0 + (quality_offset * 0.02)
            predicted_time = base_prediction * quality_factor

            # Sanity check
            if (
                predicted_time < ml_config.MIN_PREDICTION_TIME
                or predicted_time > ml_config.MAX_PREDICTION_TIME
            ):
                return None

            confidence = "HIGH" if experience >= data_req.HIGH_CONFIDENCE_MIN_EVENTS else "MEDIUM"
            method_str = "ml_ensemble" if lgb_model is not None else "ml"
            explanation = (
                f"{event_upper} {method_str} ({self._training_data_size} training records)"
            )
            if quality != 5:
                adj_pct = (quality_factor - 1.0) * 100
                explanation += f", quality {quality}/10 ({adj_pct:+.0f}%)"

            return PredictionResult(
                value=predicted_time,
                confidence=confidence,
                method="ml",
                explanation=explanation,
                metadata={"model_version": "v2_log_ensemble"},
            )

        except Exception as e:
            _log.warning("ML prediction failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Baseline prediction (ported from STRATHEX baseline.py predict_baseline_time)
# ---------------------------------------------------------------------------


def predict_baseline(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    results_df: Optional[pd.DataFrame] = None,
    wood_df: Optional[pd.DataFrame] = None,
) -> Optional[PredictionResult]:
    """
    Predict time using time-decay weighted baseline with shrinkage toward event baseline.

    Phase 1: Compute time-decay weighted mean of competitor's normalized history.
    Phase 2: Apply empirical-Bayes shrinkage toward event baseline.
    Phase 3: Apply quality multiplier (statistical fallback, quality != 5).

    Tournament weighting: if competitor.tournament_time is set, applies 97% weight
    to that time vs 3% historical baseline before returning.

    Args:
        competitor: Competitor record with historical data.
        wood: Wood profile for this event.
        event_code: 'SB' or 'UH'.
        results_df: Historical results for event baseline computation.
        wood_df: Wood properties for species hardness lookup.

    Returns:
        PredictionResult or None if no data available.
    """
    event_upper = str(event_code).strip().upper()
    quality = max(1, min(10, int(wood.quality)))

    # Build results_df from competitor history if external df not provided
    internal_df = _competitor_history_to_df(competitor)

    # Merge with results_df if provided
    if results_df is not None:
        results_std = _standardize_results_df(results_df)
        if internal_df is not None and not internal_df.empty:
            try:
                combined = pd.concat([results_std, internal_df], ignore_index=True)
            except Exception:
                combined = results_std
        else:
            combined = results_std
    else:
        combined = internal_df

    # Fit personal diameter scaling exponent if not yet cached.
    # Requires 3+ total results across 2+ distinct diameters for this event.
    # Uses calibrate_scaling_exponent() on the competitor's own history only.
    if competitor.personal_scaling_exponent is None and len(competitor.history) >= 3:
        _hist_df = _competitor_history_to_df(competitor)
        if _hist_df is not None and "size_mm" in _hist_df.columns:
            _event_rows = _hist_df[_hist_df["event"] == event_upper]
            _distinct_diams = _event_rows["size_mm"].dropna().nunique()
            if _distinct_diams >= 2:
                from strathmark.wood import calibrate_scaling_exponent as _calibrate_exp

                _personal_exp = _calibrate_exp(_hist_df, event_upper)
                if _personal_exp is not None:
                    competitor.personal_scaling_exponent = _personal_exp

    # Determine adaptive half-life for this competitor's activity level
    _comp_dates = []
    if internal_df is not None and not internal_df.empty:
        _event_df = internal_df[internal_df["event"] == event_upper]
        if "date" in _event_df.columns:
            _comp_dates = _event_df["date"].dropna().tolist()
    _activity = classify_activity_level(_comp_dates)
    _half_life = select_half_life(_activity)

    # Get competitor history (event + species filtered, with diameter normalization)
    history_with_weights = []
    data_source = "no history"
    tournament_weighted = False
    historical_baseline = None

    if internal_df is not None and not internal_df.empty:
        comp_df = internal_df[internal_df["event"] == event_upper].copy()

        if not comp_df.empty:
            for _, row in comp_df.iterrows():
                time_val = row.get("raw_time")
                if time_val is None or pd.isna(time_val) or float(time_val) <= 0:
                    continue
                # Skip timeout/DNF results — times beyond the time limit
                # are not representative of a competitor's ability
                if float(time_val) > rules.MAX_TIME_LIMIT_SECONDS:
                    continue
                hist_d = row.get("size_mm", wood.diameter_mm)
                hist_q = row.get("quality", 5.0)

                # Normalize to target diameter using personal exponent when available
                normalized = float(time_val)
                if hist_d and wood.diameter_mm and float(hist_d) != float(wood.diameter_mm):
                    if competitor.personal_scaling_exponent is not None:
                        exponent = competitor.personal_scaling_exponent
                    else:
                        exponent = get_event_scaling_exponent(combined, event_upper)
                    factor = calculate_scaling_factor(
                        float(hist_d), float(wood.diameter_mm), exponent
                    )
                    normalized = normalized * factor

                # Normalize quality to 5
                hist_q_int = max(1, min(10, int(hist_q) if not pd.isna(hist_q) else 5))
                if hist_q_int != 5:
                    q_factor = 1.0 + ((hist_q_int - 5) * 0.02)
                    if q_factor > 0:
                        normalized = normalized / q_factor

                # Normalize species to target species
                hist_species = str(row.get("species", "")).strip()
                if hist_species and wood.species:
                    hist_mult = get_species_time_multiplier(hist_species)
                    target_mult = get_species_time_multiplier(wood.species)
                    if hist_mult > 0 and hist_mult != target_mult:
                        normalized = normalized / hist_mult * target_mult

                result_date = row.get("date")
                w = _calculate_weight_simple(result_date, half_life_days=_half_life)
                history_with_weights.append((normalized, result_date, w))

            if history_with_weights:
                data_source = f"on {wood.species}"

    confidence = "LOW"
    baseline = None

    if len(history_with_weights) >= data_req.MIN_HISTORICAL_TIMES:
        times = [t for t, _, _ in history_with_weights]
        weights = [w for _, _, w in history_with_weights]
        try:
            baseline = compute_weighted_average(times, weights)
        except Exception:
            baseline = float(np.mean(times))

        effective_n = sum(weights)
        confidence = "HIGH"

        # Shrinkage toward event baseline
        if combined is not None and not combined.empty:
            event_base, event_base_conf, event_base_expl = get_event_baseline(
                event_upper,
                wood.species,
                wood.diameter_mm,
                combined,
                exclude_competitor=competitor.name,
            )
            if event_base is not None:
                shrinkage_k = 5.0
                w = effective_n / (effective_n + shrinkage_k)
                baseline = (w * baseline) + ((1.0 - w) * event_base)
                data_source += f" + shrinkage to {event_base_expl}"

    elif len(history_with_weights) > 0:
        times = [t for t, _, _ in history_with_weights]
        weights = [w for _, _, w in history_with_weights]
        try:
            baseline = compute_weighted_average(times, weights)
        except Exception:
            baseline = float(np.mean(times))

        effective_n = sum(weights)
        confidence = "MEDIUM"

        if combined is not None and not combined.empty:
            event_base, _, event_base_expl = get_event_baseline(
                event_upper,
                wood.species,
                wood.diameter_mm,
                combined,
                exclude_competitor=competitor.name,
            )
            if event_base is not None:
                shrinkage_k = 5.0
                w = effective_n / (effective_n + shrinkage_k)
                baseline = (w * baseline) + ((1.0 - w) * event_base)
                data_source += f" + shrinkage to {event_base_expl}"
    else:
        # No competitor history -- use event baseline
        if combined is not None and not combined.empty:
            baseline, conf_str, event_base_expl = get_event_baseline(
                event_upper,
                wood.species,
                wood.diameter_mm,
                combined,
                exclude_competitor=competitor.name,
            )
            if baseline is not None:
                confidence = "LOW"
                data_source = f"event baseline ({event_base_expl})"

    if baseline is None:
        return None

    historical_baseline = baseline

    # Tournament result weighting (graduated by number of rounds) -- V4.4+ feature
    if competitor.tournament_time is not None:
        tournament_time = competitor.tournament_time
        _round_weights = {1: 0.65, 2: 0.80, 3: 0.90, 4: 0.97}
        t_weight = _round_weights.get(min(competitor.num_tournament_rounds, 4), 0.97)
        h_weight = 1.0 - t_weight
        baseline = (tournament_time * t_weight) + (historical_baseline * h_weight)
        confidence = "VERY HIGH"
        tournament_weighted = True
        t_pct = int(t_weight * 100)
        h_pct = int(h_weight * 100)
        data_source = (
            f"Tournament result ({tournament_time:.1f}s @ {t_pct}%, "
            f"{competitor.num_tournament_rounds} round(s)) + "
            f"{data_source} (@ {h_pct}%)"
        )

    # Apply quality multiplier (statistical adjustment, quality != 5)
    if quality != 5:
        quality_factor = 1.0 + ((quality - 5) * 0.02)
        baseline = baseline * quality_factor
        adj_pct = (quality_factor - 1.0) * 100
        quality_label = "softer" if quality < 5 else "harder"
        data_source += f" [Quality {quality}/10: {quality_label}, {adj_pct:+.0f}%]"

    # Apply bias correction from personal prediction residual history.
    # Subtracting the median residual adjusts for systematic over/under-prediction.
    # Circuit breaker absorbs transient Supabase failures without permanently
    # disabling bias correction. See _BiasCircuitBreaker docstring and
    # docs/ml-persistence-policy.md section 5 for the policy.
    if _bias_breaker.allow():
        try:
            from strathmark.db import get_competitor_bias as _get_bias

            _bias = _get_bias(competitor.name)
            _bias_breaker.record_success()
            if _bias is not None:
                baseline -= _bias
                data_source += f" [bias corrected {-_bias:+.1f}s]"
        except Exception:
            _bias_breaker.record_failure()

    explanation = f"Predicted {baseline:.1f}s ({data_source})"

    # Compute per-competitor std-dev from normalized history for Monte Carlo simulation
    if len(history_with_weights) >= 2:
        hist_times = [t for t, _, _ in history_with_weights]
        raw_std = float(np.std(hist_times, ddof=1))
        std_dev = max(1.5, min(raw_std, 6.0))
    else:
        std_dev = float(rules.PERFORMANCE_VARIANCE_SECONDS)

    return PredictionResult(
        value=baseline,
        confidence=confidence,
        method="baseline",
        explanation=explanation,
        metadata={
            "tournament_weighted": tournament_weighted,
            "historical_baseline": historical_baseline,
            "std_dev": std_dev,
        },
    )


def _competitor_history_to_df(competitor: CompetitorRecord) -> Optional[pd.DataFrame]:
    """Convert competitor's HistoricalResult list to a standardized DataFrame."""
    if not competitor.history:
        return None

    rows = []
    for h in competitor.history:
        rows.append(
            {
                "competitor_name": competitor.name,
                "event": str(h.event_code).strip().upper(),
                "raw_time": h.time_seconds,
                "species": str(h.species).strip() if h.species else "",
                "size_mm": h.diameter_mm,
                "quality": h.quality,
                "date": h.result_date,
                "field_strength": h.field_strength,
            }
        )

    df = pd.DataFrame(rows)
    df["raw_time"] = pd.to_numeric(df["raw_time"], errors="coerce")
    df["size_mm"] = pd.to_numeric(df["size_mm"], errors="coerce")
    df = df[(df["raw_time"] > 0) & (df["raw_time"] <= rules.MAX_TIME_LIMIT_SECONDS)]
    return df if not df.empty else None


def _calculate_weight_simple(result_date, half_life_days: int = 730) -> float:
    """Compute exponential decay weight for a single date."""
    return calculate_performance_weight(result_date, None, half_life_days)


# ---------------------------------------------------------------------------
# LLM prediction (ported from STRATHEX ai_predictor.py)
# ---------------------------------------------------------------------------

# JSON schema for LLM quality adjustment response (Ollama structured output)
QUALITY_ADJUSTMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "multiplier": {"type": "number"},
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "explanation": {"type": "string"},
    },
    "required": ["multiplier", "confidence", "explanation"],
}


def _retired_predict_with_llm(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    baseline_time: float,
    ollama_url: str = "http://localhost:11434",
    model: str = None,
    timeout: int = None,
    tournament_weighted: bool = False,
    historical_baseline: Optional[float] = None,
    wood_df: Optional[pd.DataFrame] = None,
) -> Optional[PredictionResult]:
    """
    Apply LLM quality-adjustment multiplier on top of the baseline time.

    Uses Ollama's JSON schema enforcement for reliable structured output.
    Falls back gracefully if Ollama is unavailable.
    """
    import json

    from strathmark.llm import call_ollama

    quality = max(1, min(10, int(wood.quality)))

    # Skip LLM call entirely when quality is 5 (no adjustment needed)
    if quality == 5:
        return PredictionResult(
            value=baseline_time,
            confidence="HIGH" if not tournament_weighted else "VERY HIGH",
            method="llm",
            explanation=f"Predicted {baseline_time:.1f}s (quality 5, no adjustment needed)",
            metadata={"multiplier": 1.0, "tournament_weighted": tournament_weighted},
        )

    if model is None:
        model = llm_config.PREDICTION_MODEL
    if timeout is None:
        timeout = llm_config.TIMEOUT_SECONDS

    # Build concise prompt (semantic guidance — schema handles structure)
    if quality > 5:
        direction = f"Quality {quality} is HARDER than baseline (5). Multiplier should be > 1.00."
    else:
        direction = f"Quality {quality} is SOFTER than baseline (5). Multiplier should be < 1.00."

    tournament_note = ""
    if tournament_weighted and competitor.tournament_time:
        tournament_note = (
            f"\nThis baseline includes 97% weight from a same-tournament result "
            f"({competitor.tournament_time:.1f}s). Apply MINIMAL adjustment (+-1-3% max)."
        )

    prompt = f"""You are a woodchopping handicapper adjusting a time prediction for wood quality.

Competitor: {competitor.name}
Event: {event_code}
Species: {wood.species}
Diameter: {wood.diameter_mm:.0f}mm
Baseline time: {baseline_time:.1f}s (assumes quality 5 = average hardness)
Actual quality: {quality}/10
{direction}{tournament_note}

Return a quality adjustment multiplier between 0.85 and 1.15.
Quality 5 = 1.00 (no change). Each point above 5 adds ~2-3%. Each point below 5 subtracts ~2-3%.

Return your answer as JSON with keys: multiplier (number), confidence (HIGH/MEDIUM/LOW), explanation (one sentence)."""

    # Build Ollama URL for /api/generate endpoint
    api_url = ollama_url.rstrip("/") + "/api/generate"

    response = call_ollama(
        prompt,
        model=model,
        num_predict=llm_config.TOKENS_TIME_PREDICTION,
        ollama_url=api_url,
        timeout=timeout,
        format_schema=QUALITY_ADJUSTMENT_SCHEMA,
    )

    if response is None:
        return None  # Ollama unavailable — cascade falls through to baseline

    try:
        result = json.loads(response)
        multiplier = float(result["multiplier"])
        llm_confidence = str(result.get("confidence", "MEDIUM")).upper().strip()
        explanation = str(result.get("explanation", ""))

        # Validate multiplier is in acceptable range
        if not (
            llm_config.QUALITY_MULTIPLIER_MIN <= multiplier <= llm_config.QUALITY_MULTIPLIER_MAX
        ):
            return None

        predicted_time = baseline_time * multiplier

        # Sanity check: prediction should be within 50% of baseline
        if not (baseline_time * 0.5 <= predicted_time <= baseline_time * 1.5):
            return None

        # Determine final confidence
        final_confidence = "HIGH"
        if tournament_weighted:
            final_confidence = "VERY HIGH"
        elif llm_confidence == "LOW":
            final_confidence = "MEDIUM"

        return PredictionResult(
            value=predicted_time,
            confidence=final_confidence,
            method="llm",
            explanation=f"Predicted {predicted_time:.1f}s (LLM: {explanation})",
            metadata={
                "multiplier": multiplier,
                "quality_explanation": explanation,
                "tournament_weighted": tournament_weighted,
            },
        )

    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def predict_with_llm(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    baseline_time: float,
    ollama_url: str = "http://localhost:11434",
    model: str = None,
    timeout: int = None,
    tournament_weighted: bool = False,
    historical_baseline: Optional[float] = None,
    wood_df: Optional[pd.DataFrame] = None,
) -> Optional[PredictionResult]:
    """Deprecated numeric LLM entry point, permanently disabled in V2."""

    del (
        competitor,
        wood,
        event_code,
        baseline_time,
        ollama_url,
        model,
        timeout,
        tournament_weighted,
        historical_baseline,
        wood_df,
    )
    return None


# ---------------------------------------------------------------------------
# Prediction adjustment helpers (Phase 1C, 1I)
# ---------------------------------------------------------------------------


def _apply_species_affinity(
    result: PredictionResult,
    competitor: CompetitorRecord,
    species: str,
    event_code: str,
    results_df,
) -> PredictionResult:
    """
    1C: Apply species affinity adjustment.

    Computes the competitor's average residual on this specific species
    (average of actual_time - predicted_time). If the competitor has 2+
    results on this species for this event, applies the mean residual
    as an additive correction. If fewer than 2 results, returns unchanged.

    The adjustment is capped at +/-5 seconds to prevent runaway corrections.
    """
    if not competitor.history or not species:
        return result

    species_times = [
        r.time_seconds
        for r in competitor.history
        if (r.species or "").strip().lower() == species.strip().lower()
        and r.event_code.upper() == event_code.upper()
        and r.time_seconds <= rules.MAX_TIME_LIMIT_SECONDS
    ]

    if len(species_times) < 2:
        return result  # insufficient data -- skip

    # Estimated average historical time on this species
    species_mean = _statistics.mean(species_times)
    # Residual: how much faster/slower than predicted this competitor typically runs
    residual = species_mean - result.value
    # Cap residual to prevent runaway
    residual = max(-5.0, min(5.0, residual * 0.25))  # blend 25% (reduced: species norm in baseline)

    if abs(residual) < 0.1:
        return result

    from dataclasses import replace as _replace

    adjusted = result.value + residual
    return _replace(
        result,
        value=round(adjusted, 2),
        explanation=result.explanation + f" [species affinity: {residual:+.1f}s on {species}]",
    )


def _apply_form_trajectory(
    result: PredictionResult,
    competitor: CompetitorRecord,
    event_code: str,
) -> PredictionResult:
    """
    1I: Apply form trajectory adjustment.

    Fits a linear regression on the competitor's last 5 results for this
    event type (time vs. date). If slope is negative (improving) by more
    than 0.5s/month, adjusts prediction downward. If positive (declining),
    adjusts upward. Skips if fewer than 3 recent results.
    """
    if not competitor.history:
        return result

    # Get dated results for this event (exclude timeouts)
    dated = sorted(
        [
            r
            for r in competitor.history
            if r.event_code.upper() == event_code.upper()
            and r.result_date is not None
            and r.time_seconds <= rules.MAX_TIME_LIMIT_SECONDS
        ],
        key=lambda r: r.result_date,
    )

    if len(dated) < 3:
        return result  # insufficient data

    # Use last 5 results
    recent = dated[-5:]

    # Convert dates to days since first result in window
    ref_date = recent[0].result_date
    xs = []
    ys = []
    for r in recent:
        rd = r.result_date
        # Normalize to plain date for consistent subtraction
        if hasattr(rd, "date") and callable(rd.date):
            rd = rd.date()
        ref = ref_date
        if hasattr(ref, "date") and callable(ref.date):
            ref = ref.date()
        delta = (rd - ref).days if isinstance(rd, date) else 0
        xs.append(float(delta))
        ys.append(r.time_seconds)

    if len(set(xs)) < 2:
        return result  # all on same day, no slope

    # Fit linear regression
    x_arr = xs
    y_arr = ys
    n = len(x_arr)
    x_mean = sum(x_arr) / n
    y_mean = sum(y_arr) / n
    num = sum((x_arr[i] - x_mean) * (y_arr[i] - y_mean) for i in range(n))
    den = sum((x_arr[i] - x_mean) ** 2 for i in range(n))
    if den == 0:
        return result
    slope_per_day = num / den  # seconds per day

    slope_per_month = slope_per_day * 30.44

    # Only adjust if slope exceeds threshold (0.5s/month)
    if abs(slope_per_month) < 0.5:
        return result

    # Compute days since last result
    last_date = recent[-1].result_date
    today = date.today()
    if isinstance(last_date, date):
        # Normalize pandas Timestamp / datetime to plain date for subtraction
        if hasattr(last_date, "date"):
            last_date = last_date.date()
        days_since_last = (today - last_date).days
    else:
        days_since_last = 30

    # Projected change over time since last result
    adjustment = slope_per_day * days_since_last
    # Cap adjustment at +/-8 seconds
    adjustment = max(-8.0, min(8.0, adjustment))

    if abs(adjustment) < 0.1:
        return result

    from dataclasses import replace as _replace

    adjusted = result.value + adjustment
    direction = "improving" if slope_per_month < 0 else "declining"
    return _replace(
        result,
        value=round(adjusted, 2),
        explanation=(
            result.explanation + f" [form trajectory: {slope_per_month:+.2f}s/month ({direction}), "
            f"adj {adjustment:+.1f}s]"
        ),
    )


# ---------------------------------------------------------------------------
# Top-level cascade functions
# ---------------------------------------------------------------------------


def _deprecated_numeric_cascade(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    wood_data_df=None,
    results_df=None,
    ml_model=None,
    llm_client=None,
) -> PredictionResult:
    """
    Run the full prediction cascade and return the highest-priority valid result.

    Cascade order:
        1. Manual override  -- if competitor.manual_time_override is set
        2. LLM prediction   -- optional; only runs if llm_client is explicitly passed.
                              Not used by HandicapCalculator.calculate() (hot path).
                              Intended for analytics/reporting callers only.
        3. ML model         -- XGBoost trained on historical data with time-decay weights
                              (requires ml_model; skipped if not trained)
        4. Weighted baseline -- time-decay-weighted historical average with diameter
                              scaling (see decay.py, wood.py)
        5. Panel mark        -- division-based default for competitors with no history
                              (see fallback.py)

    Tournament result weighting (V4.4 feature from STRATHEX):
        If competitor.tournament_time is set, the baseline is computed as:
            weighted_baseline = (tournament_time * 0.97) + (historical_avg * 0.03)
        This applies to levels 3 and 4. Manual override (level 1) is unaffected.

    Args:
        competitor: Full competitor record including history and optional overrides.
        wood: Wood profile for this event.
        event_code: 'SB' or 'UH'.
        wood_data_df: Optional DataFrame of species properties (Janka, etc.).
                      If None, default Eastern White Pine values are used.
        results_df: Optional historical results DataFrame for event baseline
                    calculation when competitor has sparse individual history.
        ml_model: Optional trained MLModel object.
                  If None, ML level is skipped.
        llm_client: Optional dict with keys 'url', 'model', 'timeout' for Ollama.
                    If None, LLM level is skipped.

    Returns:
        PredictionResult from the highest-priority cascade level that succeeded.

    Raises:
        ValueError: If event_code is not 'SB' or 'UH'.
        RuntimeError: If ALL cascade levels fail (should not happen -- panel
                      mark is the unconditional final fallback).
    """
    if not is_valid_event(event_code):
        raise ValueError(f"Invalid event_code: '{event_code}'. Must be 'SB' or 'UH'.")

    _t_start = _time.monotonic()

    # Priority 1: Manual override (5B)
    if competitor.manual_time_override is not None:
        t = float(competitor.manual_time_override)
        result = PredictionResult(
            value=t,
            confidence="VERY HIGH",
            method="manual",
            explanation=f"Manual override: {t:.1f}s (operator-supplied)",
            metadata={"source": "handicapper_override"},
        )
        _log.info(
            "prediction competitor_id=%s method=manual value=%.2f confidence=%s "
            "prediction_time_ms=%.1f",
            getattr(competitor, "competitor_id", competitor.name),
            t,
            result.confidence,
            (_time.monotonic() - _t_start) * 1000,
        )
        return result

    # Compute baseline for LLM and as fallback
    baseline_result = predict_baseline(competitor, wood, event_code, results_df, wood_data_df)

    # 1C: Species affinity adjustment (additive residual for this specific species)
    if baseline_result is not None:
        baseline_result = _apply_species_affinity(
            baseline_result, competitor, wood.species, event_code, results_df
        )

    # 1I: Competitor form trajectory adjustment
    if baseline_result is not None:
        baseline_result = _apply_form_trajectory(baseline_result, competitor, event_code)

    # Priority 2: LLM prediction (requires Ollama and a working baseline)
    if llm_client is not None and baseline_result is not None:
        ollama_url = (
            llm_client.get("url", "http://localhost:11434")
            if isinstance(llm_client, dict)
            else "http://localhost:11434"
        )
        ollama_model = (
            llm_client.get("model", llm_config.PREDICTION_MODEL)
            if isinstance(llm_client, dict)
            else llm_config.PREDICTION_MODEL
        )
        ollama_timeout = (
            llm_client.get("timeout", llm_config.TIMEOUT_SECONDS)
            if isinstance(llm_client, dict)
            else llm_config.TIMEOUT_SECONDS
        )

        tournament_weighted = baseline_result.metadata.get("tournament_weighted", False)
        historical_baseline = baseline_result.metadata.get("historical_baseline")

        # Pass quality=5 baseline to LLM (it handles quality adjustment internally)
        llm_result = predict_with_llm(
            competitor=competitor,
            wood=wood,
            event_code=event_code,
            baseline_time=baseline_result.value,
            ollama_url=ollama_url,
            model=ollama_model,
            timeout=ollama_timeout,
            tournament_weighted=tournament_weighted,
            historical_baseline=historical_baseline,
            wood_df=wood_data_df,
        )
        if llm_result is not None:
            _log.info(
                "prediction competitor_id=%s method=llm value=%.2f confidence=%s "
                "prediction_time_ms=%.1f",
                getattr(competitor, "competitor_id", competitor.name),
                llm_result.value,
                llm_result.confidence,
                (_time.monotonic() - _t_start) * 1000,
            )
            return llm_result

    # Priority 3: ML model
    # 1K: Gate -- skip ML if competitor has no results for this event type
    _event_result_count = sum(
        1 for r in competitor.history if r.event_code.upper() == event_code.upper()
    )
    if ml_model is not None and _event_result_count >= 1:
        ml_result = ml_model.predict(competitor, wood, event_code, results_df)
        if ml_result is not None:
            _log.info(
                "prediction competitor_id=%s method=ml value=%.2f confidence=%s "
                "model_version=%s prediction_time_ms=%.1f",
                getattr(competitor, "competitor_id", competitor.name),
                ml_result.value,
                ml_result.confidence,
                ml_result.metadata.get("model_version", "unknown"),
                (_time.monotonic() - _t_start) * 1000,
            )
            return ml_result

    # Priority 4: Weighted baseline
    if baseline_result is not None:
        _log.info(
            "prediction competitor_id=%s method=baseline value=%.2f confidence=%s "
            "prediction_time_ms=%.1f",
            getattr(competitor, "competitor_id", competitor.name),
            baseline_result.value,
            baseline_result.confidence,
            (_time.monotonic() - _t_start) * 1000,
        )
        return baseline_result

    # Priority 5: Default mark fallback (unconditional)
    default_time, default_expl = get_panel_mark(event_code, competitor.division)

    # Scale default mark from 300mm standard to target diameter
    from strathmark.wood import calculate_scaling_factor as _csf

    if wood.diameter_mm != 300.0:
        exponent = get_event_scaling_exponent(results_df, event_code)
        factor = _csf(300.0, wood.diameter_mm, exponent)
        default_time = default_time * factor

    fallback_result = PredictionResult(
        value=default_time,
        confidence="VERY LOW",
        method="panel",
        explanation=f"Default mark fallback: {default_expl}",
    )
    _log.info(
        "prediction competitor_id=%s method=fallback value=%.2f confidence=%s "
        "prediction_time_ms=%.1f",
        getattr(competitor, "competitor_id", competitor.name),
        default_time,
        "VERY LOW",
        (_time.monotonic() - _t_start) * 1000,
    )
    return fallback_result


def _deprecated_all_predictions(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    wood_data_df=None,
    results_df=None,
    ml_model=None,
    llm_client=None,
) -> Dict[str, Optional[PredictionResult]]:
    """
    Run every cascade level independently and return all results for comparison.

    Useful for display in tournament software (judge can see all three predictions
    before the best one is selected).

    Returns:
        Dict with keys: 'manual', 'llm', 'ml', 'baseline', 'panel'.
        Each value is a PredictionResult or None if that level was not applicable
        or failed.
    """
    results: Dict[str, Optional[PredictionResult]] = {
        "manual": None,
        "llm": None,
        "ml": None,
        "baseline": None,
        "panel": None,
    }

    # Manual override
    if competitor.manual_time_override is not None:
        t = float(competitor.manual_time_override)
        results["manual"] = PredictionResult(
            value=t,
            confidence="VERY HIGH",
            method="manual",
            explanation=f"Manual override: {t:.1f}s (operator-supplied)",
        )

    # Baseline
    baseline_result = predict_baseline(competitor, wood, event_code, results_df, wood_data_df)
    results["baseline"] = baseline_result

    # LLM
    if llm_client is not None and baseline_result is not None:
        ollama_url = (
            llm_client.get("url", "http://localhost:11434")
            if isinstance(llm_client, dict)
            else "http://localhost:11434"
        )
        ollama_model = (
            llm_client.get("model", llm_config.PREDICTION_MODEL)
            if isinstance(llm_client, dict)
            else llm_config.PREDICTION_MODEL
        )
        ollama_timeout = (
            llm_client.get("timeout", llm_config.TIMEOUT_SECONDS)
            if isinstance(llm_client, dict)
            else llm_config.TIMEOUT_SECONDS
        )

        tournament_weighted = baseline_result.metadata.get("tournament_weighted", False)
        historical_baseline = baseline_result.metadata.get("historical_baseline")

        results["llm"] = predict_with_llm(
            competitor=competitor,
            wood=wood,
            event_code=event_code,
            baseline_time=baseline_result.value,
            ollama_url=ollama_url,
            model=ollama_model,
            timeout=ollama_timeout,
            tournament_weighted=tournament_weighted,
            historical_baseline=historical_baseline,
            wood_df=wood_data_df,
        )

    # ML
    if ml_model is not None:
        results["ml"] = ml_model.predict(competitor, wood, event_code, results_df)

    # Default mark
    default_time, default_expl = get_panel_mark(event_code, competitor.division)
    from strathmark.wood import calculate_scaling_factor as _csf

    if wood.diameter_mm != 300.0:
        exponent = get_event_scaling_exponent(results_df, event_code)
        factor = _csf(300.0, wood.diameter_mm, exponent)
        default_time = default_time * factor

    results["panel"] = PredictionResult(
        value=default_time,
        confidence="VERY LOW",
        method="panel",
        explanation=f"Default mark fallback: {default_expl}",
    )

    return results


# ---------------------------------------------------------------------------
# Best-prediction selection with expected-error scoring
# ---------------------------------------------------------------------------


def _deprecated_expected_error_selection(
    all_predictions: Dict[str, Optional[PredictionResult]],
) -> PredictionResult:
    """
    Select the best prediction from a dict returned by get_all_predictions().

    Uses expected-error scoring rather than a simple cascade:
        1. Score each available prediction by expected error (seconds).
        2. Apply a spread deduction if the methods disagree significantly.
        3. Return the prediction with the lowest expected error.

    Manual overrides are always preferred unconditionally.

    Expected error formula (ported from STRATHEX select_best_prediction()):
        base  = confidence_to_error[confidence]
        +0.5  if method is 'llm' (slight variance adjustment)
        +1.5  if metadata['scaled'] is True (diameter/species normalization)
        -1.0  if metadata['tournament_weighted'] is True (same-wood bonus, floor 0.5)

    Spread deduction (applied to overall confidence):
        max_diff >= 6s or >= 25% of mean  -> downgrade 2 steps
        max_diff >= 4s or >= 12% of mean  -> downgrade 1 step

    Args:
        all_predictions: Dict returned by get_all_predictions(), keys are
                         'manual', 'llm', 'ml', 'baseline', 'panel'.
                         Each value is a PredictionResult or None.

    Returns:
        The PredictionResult selected as best. Never None (panel mark is the
        unconditional fallback and is always present).
    """
    _confidence_order = ["VERY HIGH", "HIGH", "MEDIUM", "LOW", "VERY LOW"]
    _error_map = {
        "VERY HIGH": 2.0,
        "HIGH": 3.0,
        "MEDIUM": 5.0,
        "LOW": 7.0,
        "VERY LOW": 9.0,
    }

    def _expected_error(pred: PredictionResult) -> float:
        base = _error_map.get(pred.confidence or "LOW", 7.0)
        if pred.method == "llm":
            base += 0.5
        meta = pred.metadata or {}
        if meta.get("scaled", False):
            base += 1.5
        if meta.get("tournament_weighted", False):
            base = max(0.5, base - 1.0)
        return base

    def _downgrade(conf: str, steps: int) -> str:
        idx = _confidence_order.index(conf) if conf in _confidence_order else 3
        return _confidence_order[min(len(_confidence_order) - 1, idx + steps)]

    # Manual override wins unconditionally
    manual = all_predictions.get("manual")
    if manual is not None:
        return manual

    # Gather scoreable candidates (ml, llm, baseline; not panel unless nothing else)
    primary_keys = ["ml", "llm", "baseline"]
    candidates = [all_predictions[k] for k in primary_keys if all_predictions.get(k) is not None]

    if not candidates:
        # Fall back to panel mark
        panel = all_predictions.get("panel")
        if panel is not None:
            return panel
        raise RuntimeError(
            "select_best_prediction: all prediction levels are None, including panel mark fallback."
        )

    # Score and pick lowest expected error
    scored = [(pred, _expected_error(pred)) for pred in candidates]
    best_pred, best_error = min(scored, key=lambda x: x[1])

    # Apply spread deduction when methods disagree significantly
    values = [p.value for p in candidates]
    spread_deduction = 0
    if len(values) >= 2:
        mean_v = sum(values) / len(values)
        max_diff = max(values) - min(values)
        pct_diff = max_diff / mean_v if mean_v else 0.0
        if max_diff >= 6.0 or pct_diff >= 0.25:
            spread_deduction = 2
        elif max_diff >= 4.0 or pct_diff >= 0.12:
            spread_deduction = 1

    # Derive overall confidence from expected error, then apply spread deduction
    if best_error <= 2.5:
        overall_conf = "VERY HIGH"
    elif best_error <= 3.5:
        overall_conf = "HIGH"
    elif best_error <= 5.5:
        overall_conf = "MEDIUM"
    elif best_error <= 7.5:
        overall_conf = "LOW"
    else:
        overall_conf = "VERY LOW"

    if spread_deduction:
        overall_conf = _downgrade(overall_conf, spread_deduction)

    # If overall confidence differs from method confidence, annotate explanation
    explanation = best_pred.explanation
    if best_pred.confidence != overall_conf and explanation:
        explanation = (
            f"{explanation} [Method conf: {best_pred.confidence}, overall conf: {overall_conf}]"
        )

    return PredictionResult(
        value=best_pred.value,
        confidence=overall_conf,
        method=best_pred.method,
        explanation=explanation,
        metadata=best_pred.metadata,
    )


# ---------------------------------------------------------------------------
# Prediction Engine V2 authoritative compatibility projection
# ---------------------------------------------------------------------------

_IGNORED_V2_FACTORS = (
    "division",
    "round_or_heat",
    "venue",
    "lane_or_stand",
    "run_order",
    "log_block_batch_identity",
    "wood_quality_or_moisture",
    "weather",
    "equipment",
    "rest_or_fatigue",
    "penalty_or_dnf",
    "same_tournament_result",
    "field_strength",
)
_BROAD_EVENT_PRIORS = {"SB": (50.0, 0.45), "UH": (75.0, 0.45)}
_V2_ENGINE_VERSION = "2.0.0"


def _prediction_runtime(
    context: Optional[PredictionContext],
    prediction_bundle: Optional[PredictionBundle],
    prediction_provider: Optional[PredictionEngineProvider],
) -> tuple[PredictionContext, date, PredictionBundle, str]:
    from strathmark.features import normalize_prediction_as_of

    supplied = context or PredictionContext()
    cutoff = normalize_prediction_as_of(supplied.prediction_as_of)
    resolved = PredictionContext(
        prediction_as_of=cutoff,
        request_id=supplied.request_id,
        seed=supplied.seed,
        engine=supplied.engine,
    )
    selected = (supplied.engine or prediction_config.selected_engine()).strip().lower()
    engine = "legacy" if selected in {"legacy", "legacy-baseline"} else "v2"
    if prediction_bundle is not None:
        bundle = prediction_bundle
    else:
        provider = prediction_provider or get_prediction_provider()
        bundle = provider.snapshot(cutoff)
    return resolved, cutoff, bundle, engine


def _manual_prediction(competitor: CompetitorRecord, cutoff: date) -> Optional[PredictionResult]:
    if competitor.manual_time_override is None:
        return None
    value = float(competitor.manual_time_override)
    return PredictionResult(
        value=value,
        confidence="VERY HIGH",
        method="manual",
        explanation=f"Manual override: {value:.1f}s (operator-supplied)",
        metadata={
            "source": "operator_override",
            "is_override": True,
            "confidence_kind": "operator_authority",
        },
        interval=None,
        engine_version=_V2_ENGINE_VERSION,
        evidence_cutoff=cutoff,
        provenance={"source": "operator_override", "model_evidence": False},
        ignored_factors=list(_IGNORED_V2_FACTORS),
    )


def _request_history_frame(
    competitor: CompetitorRecord,
    competitor_id: str,
) -> pd.DataFrame:
    rows = []
    gender = competitor.gender
    for result in competitor.history:
        rows.append(
            {
                "competitor_id": competitor_id,
                "event": result.event_code,
                "time_seconds": result.time_seconds,
                "result_date": result.result_date,
                "diameter_mm": result.diameter_mm,
                "species": result.species,
                "gender": gender,
            }
        )
    return pd.DataFrame(rows)


def _request_identity(competitor: CompetitorRecord, context: PredictionContext) -> tuple[str, str]:
    stable = str(competitor.competitor_id or "").strip()
    if stable:
        return stable, "stable"
    scope = str(context.request_id or "anonymous-request").strip()
    return f"request:{scope}", "request_scoped"


def _v2_request(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    cutoff: date,
    competitor_id: str,
    wood_data_df: Optional[pd.DataFrame],
):
    from strathmark.prediction_v2 import PredictionV2Request

    properties, species_missing = resolve_species_properties(wood.species, wood_data_df)
    return PredictionV2Request(
        competitor_id=competitor_id,
        event=event_code,
        diameter_mm=float(wood.diameter_mm),
        species=str(wood.species),
        gender=str(competitor.gender or ""),
        prediction_as_of=cutoff,
        janka_hardness=float(properties["janka_hardness"]),
        specific_gravity=float(properties["specific_gravity"]),
        crush_strength=float(properties["crush_strength"]),
        shear_strength=float(properties["shear_strength"]),
        modulus_of_rupture=float(properties["modulus_of_rupture"]),
        modulus_of_elasticity=float(properties["modulus_of_elasticity"]),
        species_missing=species_missing,
    )


def _confidence_for_distribution(distribution: Any) -> str:
    if distribution.degraded or distribution.source == "broad_event_prior":
        return "VERY LOW"
    if distribution.interval.calibration_state != "calibrated":
        return "LOW"
    if distribution.history_count >= 5:
        return "HIGH"
    if distribution.history_count >= 2:
        return "MEDIUM"
    return "LOW"


def _distribution_result(
    distribution: Any,
    *,
    method: str,
    cutoff: date,
    bundle: PredictionBundle,
    identity_scope: str,
    extra_warnings: tuple[str, ...] = (),
) -> PredictionResult:
    interval = PredictionInterval(
        lower=float(distribution.interval.lower),
        upper=float(distribution.interval.upper),
        nominal_coverage=float(distribution.interval.nominal_coverage),
        calibration_state=str(distribution.interval.calibration_state),
        scope=str(distribution.interval.scope),
    )
    warnings = list(dict.fromkeys((*bundle.warnings, *distribution.warnings, *extra_warnings)))
    degraded = bool(bundle.degraded or distribution.degraded or extra_warnings)
    source = str(distribution.source)
    label = "Promoted residual V2" if method == "ml" else "Prediction Engine V2 core"
    return PredictionResult(
        value=float(distribution.median),
        confidence=_confidence_for_distribution(distribution),
        method=method,
        explanation=f"{label}: {distribution.median:.1f}s ({source})",
        metadata={
            **dict(distribution.metadata),
            "source": source,
            "history_count": int(distribution.history_count),
            "effective_history_weight": float(distribution.effective_history_weight),
            # Internal posterior parameters let the joint mark optimizer replay
            # the exact distribution without exposing a mutable model object.
            "posterior_log_location": float(distribution.log_location),
            "posterior_log_scale": float(distribution.log_scale),
        },
        interval=interval,
        engine_version=_V2_ENGINE_VERSION,
        model_version=str(distribution.model_version or bundle.core_version or "") or None,
        calibration_version=(
            str(distribution.calibration_version or bundle.calibration_version or "") or None
        ),
        evidence_cutoff=cutoff,
        provenance={
            "engine": "prediction_v2",
            "provider_source": bundle.source,
            "prediction_source": source,
            "core_checksum": getattr(bundle.core, "source_checksum", None),
            "residual_version": bundle.residual_version,
            "identity_scope": identity_scope,
        },
        warnings=warnings,
        ignored_factors=list(_IGNORED_V2_FACTORS),
        degraded=degraded,
    )


def _residual_features(request: Any, distribution: Any) -> Dict[str, Any]:
    metadata = distribution.metadata
    diameter_used = float(metadata.get("diameter_used_mm", request.diameter_mm))
    return {
        "event": request.event,
        "gender": request.gender,
        "species": request.species,
        "species_missing": int(
            request.species_missing or "unknown_species" in distribution.warnings
        ),
        "log_diameter_ratio": math.log(diameter_used / 300.0),
        "janka_hardness": request.janka_hardness,
        "specific_gravity": request.specific_gravity,
        "crush_strength": request.crush_strength,
        "shear_strength": request.shear_strength,
        "modulus_of_rupture": request.modulus_of_rupture,
        "modulus_of_elasticity": request.modulus_of_elasticity,
        "core_log_location": distribution.log_location,
        "history_count": distribution.history_count,
        "effective_history_weight": distribution.effective_history_weight,
        "same_event_state": float(metadata.get("same_event_state", 0.0)),
        "trend_projection": float(metadata.get("trend_projection", 0.0)),
        "cross_event_state": float(metadata.get("cross_event_state", 0.0)),
    }


def _panel_prediction(event_code: str, cutoff: date, bundle: PredictionBundle) -> PredictionResult:
    median, log_scale = _BROAD_EVENT_PRIORS[event_code]
    radius = 1.6448536269514722 * log_scale
    interval = PredictionInterval(
        lower=math.exp(math.log(median) - radius),
        upper=math.exp(math.log(median) + radius),
        calibration_state="broad_prior",
        scope="static_event",
    )
    warnings = list(bundle.warnings)
    if bundle.core is None and "core_artifact_missing" not in warnings:
        warnings.append("core_artifact_unavailable")
    return PredictionResult(
        value=median,
        confidence="VERY LOW",
        method="panel",
        explanation=f"Static broad {event_code} event prior: {median:.1f}s",
        metadata={"source": "broad_event_prior", "std_dev": median * log_scale},
        interval=interval,
        engine_version=_V2_ENGINE_VERSION,
        evidence_cutoff=cutoff,
        provenance={
            "engine": "prediction_v2",
            "provider_source": bundle.source,
            "prediction_source": "broad_event_prior",
        },
        warnings=list(dict.fromkeys(warnings)),
        ignored_factors=list(_IGNORED_V2_FACTORS),
        degraded=True,
    )


def _legacy_baseline_projection(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    cutoff: date,
) -> Optional[PredictionResult]:
    """One-release deterministic rollback; intentionally never calls an LLM."""

    from dataclasses import replace

    history = []
    for item in competitor.history:
        result_date = item.result_date
        if result_date is None:
            continue
        try:
            prior = pd.Timestamp(result_date).date() < cutoff
        except (TypeError, ValueError):
            prior = False
        if prior:
            history.append(replace(item, quality=5, heat_id=None, field_strength=None))
    sanitized = replace(
        competitor,
        history=history,
        division=None,
        tournament_time=None,
        num_tournament_rounds=1,
    )
    result = predict_baseline(
        sanitized,
        WoodProfile(wood.species, wood.diameter_mm, 5),
        event_code,
        None,
        None,
    )
    if result is None:
        return None
    result.engine_version = "legacy-baseline-v1"
    result.evidence_cutoff = cutoff
    result.warnings = ["legacy_engine_selected"]
    result.ignored_factors = list(_IGNORED_V2_FACTORS)
    result.provenance = {"engine": "legacy_baseline", "numeric_llm": False}
    return result


def get_all_predictions(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    wood_data_df=None,
    results_df=None,
    ml_model=None,
    llm_client=None,
    context: Optional[PredictionContext] = None,
    prediction_bundle: Optional[PredictionBundle] = None,
    prediction_provider: Optional[PredictionEngineProvider] = None,
) -> Dict[str, Optional[PredictionResult]]:
    """Project the authoritative V2 engine into the five legacy method keys."""

    del results_df, ml_model, llm_client
    event = str(event_code).strip().upper()
    if not is_valid_event(event):
        raise ValueError(f"Invalid event_code: '{event_code}'. Must be 'SB' or 'UH'.")
    resolved_context, cutoff, bundle, engine = _prediction_runtime(
        context, prediction_bundle, prediction_provider
    )
    projections: Dict[str, Optional[PredictionResult]] = {
        "manual": _manual_prediction(competitor, cutoff),
        "llm": None,
        "ml": None,
        "baseline": None,
        "panel": _panel_prediction(event, cutoff, bundle),
    }
    if engine == "legacy":
        projections["baseline"] = _legacy_baseline_projection(competitor, wood, event, cutoff)
        return projections
    if bundle.core is None:
        return projections

    competitor_id, identity_scope = _request_identity(competitor, resolved_context)
    request = _v2_request(
        competitor,
        wood,
        event,
        cutoff,
        competitor_id,
        wood_data_df,
    )
    history = _request_history_frame(competitor, competitor_id)
    try:
        core_distribution = bundle.core.predict(
            request,
            history=history,
            wood_df=wood_data_df,
        )
    except Exception:
        projections["panel"].warnings = list(
            dict.fromkeys((*projections["panel"].warnings, "core_prediction_failed"))
        )
        projections["panel"].degraded = True
        return projections

    baseline = _distribution_result(
        core_distribution,
        method="baseline",
        cutoff=cutoff,
        bundle=bundle,
        identity_scope=identity_scope,
    )
    projections["baseline"] = baseline

    residual_loaded = getattr(bundle.residual, "loaded", None)
    if bundle.residual is not None and bool(getattr(residual_loaded, "active", False)):
        application = bundle.residual.apply(
            core_distribution,
            _residual_features(request, core_distribution),
        )
        if application.applied:
            projections["ml"] = _distribution_result(
                application.distribution,
                method="ml",
                cutoff=cutoff,
                bundle=bundle,
                identity_scope=identity_scope,
            )
        elif application.warning:
            baseline.warnings = list(dict.fromkeys((*baseline.warnings, application.warning)))
            baseline.degraded = bool(application.degraded or baseline.degraded)
    return projections


def select_best_prediction(
    all_predictions: Dict[str, Optional[PredictionResult]],
) -> PredictionResult:
    """Select by the documented deterministic authority order."""

    for key in ("manual", "ml", "baseline", "panel"):
        prediction = all_predictions.get(key)
        if prediction is not None:
            return prediction
    raise RuntimeError(
        "select_best_prediction: all prediction levels are None, including panel fallback."
    )


def get_best_prediction(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    wood_data_df=None,
    results_df=None,
    ml_model=None,
    llm_client=None,
    context: Optional[PredictionContext] = None,
    prediction_bundle: Optional[PredictionBundle] = None,
    prediction_provider: Optional[PredictionEngineProvider] = None,
) -> PredictionResult:
    """Return the authoritative numeric prediction without invoking an LLM."""

    predictions = get_all_predictions(
        competitor,
        wood,
        event_code,
        wood_data_df=wood_data_df,
        results_df=results_df,
        ml_model=ml_model,
        llm_client=llm_client,
        context=context,
        prediction_bundle=prediction_bundle,
        prediction_provider=prediction_provider,
    )
    return select_best_prediction(predictions)
