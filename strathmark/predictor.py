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
    woodchopping/predictions/qaa_scaling.py           -> get_qaa_panel_mark()
"""

from __future__ import annotations

import re
import statistics as _statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from strathmark.config import (
    rules, data_req, ml_config, llm_config,
    get_event_encoding, is_valid_event,
)
from strathmark.decay import (
    calculate_performance_weight,
    classify_activity_level,
    select_half_life,
    compute_weighted_average,
    compute_weights_for_results,
)
from strathmark.wood import (
    get_event_scaling_exponent,
    calculate_scaling_factor,
    calculate_effective_janka_hardness,
    apply_quality_multiplier_statistical,
    interpolate_qaa_tables,
    get_species_properties,
)
from strathmark.fallback import (
    PANEL_MARKS_300MM,
    PANEL_MARK_DEFAULT_UNKNOWN_DIVISION,
    get_qaa_panel_mark,
    get_event_baseline,
    get_competitor_historical_times_flexible,
    _standardize_results_df,
    _compute_robust_mean,
)


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
    When set, receives 97% weight in the weighted baseline calculation.
    Confidence is upgraded to VERY HIGH.
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
        sb_mask = events == 'SB'
        if sb_mask.sum() > 10:
            self.calibrator_sb = IsotonicRegression(out_of_bounds='clip')
            self.calibrator_sb.fit(predictions[sb_mask], actuals[sb_mask])

        # Fit UH calibrator
        uh_mask = events == 'UH'
        if uh_mask.sum() > 10:
            self.calibrator_uh = IsotonicRegression(out_of_bounds='clip')
            self.calibrator_uh.fit(predictions[uh_mask], actuals[uh_mask])

        self.is_fitted = True

    def calibrate(self, prediction: float, event_code: str) -> float:
        """Apply isotonic calibration to prediction"""
        if not self.is_fitted:
            return prediction

        if event_code == 'SB' and self.calibrator_sb is not None:
            return float(self.calibrator_sb.predict([prediction])[0])
        elif event_code == 'UH' and self.calibrator_uh is not None:
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

        for event in ['SB', 'UH']:
            mask = events == event
            if mask.sum() < 30:
                continue

            X = competitor_features[mask]
            y = residuals[mask]

            model = xgb.XGBRegressor(
                n_estimators=50,
                max_depth=3,
                learning_rate=0.1,
                random_state=42
            )
            model.fit(X, y)

            if event == 'SB':
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

        model = self.scaler_sb if event_code == 'SB' else self.scaler_uh
        if model is None:
            return baseline_std

        try:
            X = pd.DataFrame([competitor_features])
            predicted_std = model.predict(X)[0]
            final_std = max(predicted_std, baseline_std)
            from strathmark.config import sim_config
            final_std = max(sim_config.MIN_COMPETITOR_STD_SECONDS,
                           min(final_std, sim_config.MAX_COMPETITOR_STD_SECONDS))
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
        self._training_data_size: int = 0
        self._cv_metrics: Dict[str, Optional[dict]] = {'SB': None, 'UH': None}
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

        model_params = {
            'n_estimators': ml_config.N_ESTIMATORS,
            'max_depth': ml_config.MAX_DEPTH,
            'learning_rate': ml_config.LEARNING_RATE,
            'random_state': ml_config.RANDOM_STATE,
            'objective': ml_config.OBJECTIVE,
            'tree_method': ml_config.TREE_METHOD,
        }

        trained_any = False
        for event in ['SB', 'UH']:
            event_df = df_eng[df_eng['event'] == event].copy()
            if len(event_df) < data_req.MIN_ML_TRAINING_RECORDS_PER_EVENT:
                continue

            X = event_df[feature_cols]
            y = event_df['raw_time']

            # Calculate time-decay sample weights
            if 'date' in event_df.columns:
                sample_weights = event_df['date'].apply(
                    lambda d: calculate_performance_weight(d, half_life_days=730)
                )
            else:
                sample_weights = pd.Series([1.0] * len(event_df), index=event_df.index)

            # Remove rows with NaN values
            mask = ~(X.isna().any(axis=1) | y.isna())
            X = X[mask]
            y = y[mask]
            sample_weights = sample_weights[mask]

            if len(X) < data_req.MIN_ML_TRAINING_RECORDS_PER_EVENT:
                continue

            model = xgb.XGBRegressor(**model_params)
            model.fit(X, y, sample_weight=sample_weights)

            self._models[event] = model
            trained_any = True

            if verbose:
                y_pred = model.predict(X)
                mae = mean_absolute_error(y, y_pred)
                r2 = r2_score(y, y_pred)
                print(f"Trained {event} model: {len(X)} records (MAE: {mae:.2f}s, R2: {r2:.3f})")

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
        required = {'competitor_name', 'event', 'raw_time'}
        if not required.issubset(df.columns):
            return None

        df['raw_time'] = pd.to_numeric(df['raw_time'], errors='coerce')
        df = df.dropna(subset=['raw_time'])
        df = df[df['raw_time'] > 0]

        if len(df) == 0:
            return None

        # Wood properties lookup
        wood_props_cache: Dict[str, Dict] = {}

        def _get_wood_props(species_code):
            if species_code in wood_props_cache:
                return wood_props_cache[species_code]
            props = get_species_properties(str(species_code), wood_df)
            result = {
                'janka': props.janka_hardness,
                'spec_grav': props.specific_gravity,
                'shear': props.shear_strength,
                'crush': props.crush_strength,
                'mor': props.mor,
                'moe': props.moe,
            }
            wood_props_cache[species_code] = result
            return result

        # Compute competitor averages by event (time-decay weighted)
        comp_event_avg: Dict[str, float] = {}
        for (comp, event), group in df.groupby(['competitor_name', 'event']):
            if 'date' in group.columns:
                weights = group['date'].apply(
                    lambda d: calculate_performance_weight(d, half_life_days=730)
                )
                w_sum = weights.sum()
                if w_sum > 0:
                    avg = float((group['raw_time'] * weights).sum() / w_sum)
                else:
                    avg = float(group['raw_time'].mean())
            else:
                avg = float(group['raw_time'].mean())
            comp_event_avg[f"{comp}||{event}"] = avg

        rows = []
        for _, row in df.iterrows():
            comp = row.get('competitor_name', '')
            event = row.get('event', '')
            species = str(row.get('species', '')).strip()
            diameter = float(row.get('size_mm', 300.0) or 300.0)
            quality = int(row.get('quality', 5) or 5)
            quality = max(1, min(10, quality))

            # Wood properties
            wp = _get_wood_props(species) if species else _get_wood_props('')

            # Competitor average time for this event
            comp_avg = comp_event_avg.get(f"{comp}||{event}", float(df['raw_time'].mean()))

            # Experience (number of results)
            comp_event_data = df[(df['competitor_name'] == comp) & (df['event'] == event)]
            experience = len(comp_event_data)

            # Trend slope
            trend_slope = 0.0
            if 'date' in comp_event_data.columns and len(comp_event_data) >= ml_config.TREND_MIN_SAMPLES:
                try:
                    dates = pd.to_datetime(comp_event_data['date'], errors='coerce')
                    x = (dates - dates.min()).dt.days.astype(float)
                    y = pd.to_numeric(comp_event_data['raw_time'], errors='coerce').astype(float)
                    valid = np.isfinite(x) & np.isfinite(y)
                    x = x[valid]; y = y[valid]
                    if len(x) >= 2 and x.nunique() >= 2:
                        slope, _ = np.polyfit(x, y, 1)
                        trend_slope = float(slope)
                except Exception:
                    trend_slope = 0.0

            # Competitor variance
            comp_variance = float(pd.to_numeric(comp_event_data['raw_time'], errors='coerce').std()) if len(comp_event_data) > 1 else 3.0
            if not np.isfinite(comp_variance):
                comp_variance = 3.0

            # Competitor median diameter
            comp_median_diam = float(pd.to_numeric(comp_event_data.get('size_mm', pd.Series([300.0])), errors='coerce').median()) if 'size_mm' in comp_event_data.columns else diameter
            if not np.isfinite(comp_median_diam):
                comp_median_diam = diameter

            # Recency score
            recency_score = 365.0
            if 'date' in comp_event_data.columns:
                dates_s = pd.to_datetime(comp_event_data['date'], errors='coerce').dropna().sort_values()
                if len(dates_s) >= 2:
                    deltas = dates_s.diff().dt.days.dropna()
                    if not deltas.empty:
                        recency_score = float(deltas.iloc[-1])
            recency_score = max(0.0, min(1000.0, recency_score))

            # Career phase
            if trend_slope > 0.01:
                career_phase = -1
            elif trend_slope < -0.01:
                career_phase = 1
            else:
                career_phase = 0

            # Seasonal encoding
            month = 7
            if 'date' in comp_event_data.columns:
                latest = pd.to_datetime(comp_event_data['date'], errors='coerce').dropna()
                if not latest.empty:
                    month = int(latest.max().month)
            month_rad = (month - 1) * (2 * np.pi / 12)

            # Event encoding
            event_enc = ml_config.EVENT_ENCODING_SB if event == 'SB' else ml_config.EVENT_ENCODING_UH

            feat = {
                'competitor_avg_time_by_event': comp_avg,
                'event_encoded': event_enc,
                'size_mm': diameter,
                'wood_janka_hardness': wp['janka'],
                'wood_spec_gravity': wp['spec_grav'],
                'wood_shear_strength': wp['shear'],
                'wood_crush_strength': wp['crush'],
                'wood_MOR': wp['mor'],
                'wood_MOE': wp['moe'],
                'competitor_experience': float(experience),
                'competitor_trend_slope': trend_slope,
                'wood_quality': float(quality),
                'diameter_squared': diameter ** 2,
                'quality_x_diameter': float(quality) * diameter,
                'quality_x_hardness': float(quality) * wp['janka'],
                'experience_x_size': float(experience) * diameter,
                'competitor_variance': comp_variance,
                'competitor_median_diameter': comp_median_diam,
                'recency_score': recency_score,
                'career_phase': float(career_phase),
                'seasonal_month_sin': float(np.sin(month_rad)),
                'seasonal_month_cos': float(np.cos(month_rad)),
                'event_x_diameter': float(event_enc) * diameter,
                # Pass-through columns needed after feature engineering
                'competitor_name': comp,
                'event': event,
                'raw_time': float(row['raw_time']),
            }
            if 'date' in row:
                feat['date'] = row['date']
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

        if not self._is_trained or event_upper not in self._models:
            # Try to train on the fly if results_df provided
            if results_df is not None and not self._is_trained:
                self.train(results_df)
            if not self._is_trained or event_upper not in self._models:
                return None

        model = self._models[event_upper]

        # Build features from competitor history
        if not competitor.history:
            return None

        # Calculate time-decay weighted average
        event_history = [
            h for h in competitor.history
            if h.event_code.upper() == event_upper
        ]
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
                hist_with_dates = [(h.time_seconds, h.result_date) for h in event_history if h.result_date is not None]
                if len(hist_with_dates) >= ml_config.TREND_MIN_SAMPLES:
                    import datetime as _dt
                    base_date = min(d for _, d in hist_with_dates)
                    x = np.array([(d - base_date).days for _, d in hist_with_dates], dtype=float)
                    y = np.array([t for t, _ in hist_with_dates], dtype=float)
                    if x.nunique() >= 2 if hasattr(x, 'nunique') else len(np.unique(x)) >= 2:
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
            deltas = [(all_dates[i+1] - all_dates[i]).days for i in range(len(all_dates)-1)]
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
        event_enc = ml_config.EVENT_ENCODING_SB if event_upper == 'SB' else ml_config.EVENT_ENCODING_UH

        # Wood properties
        props = get_species_properties(wood.species)
        quality = max(1, min(10, int(wood.quality)))

        feature_payload = {
            'competitor_avg_time_by_event': comp_avg,
            'event_encoded': event_enc,
            'size_mm': float(wood.diameter_mm),
            'wood_janka_hardness': props.janka_hardness,
            'wood_spec_gravity': props.specific_gravity,
            'wood_shear_strength': props.shear_strength,
            'wood_crush_strength': props.crush_strength,
            'wood_MOR': props.mor,
            'wood_MOE': props.moe,
            'competitor_experience': float(experience),
            'competitor_trend_slope': trend_slope,
            'wood_quality': float(quality),
            'diameter_squared': float(wood.diameter_mm) ** 2,
            'quality_x_diameter': float(quality) * float(wood.diameter_mm),
            'quality_x_hardness': float(quality) * props.janka_hardness,
            'experience_x_size': float(experience) * float(wood.diameter_mm),
            'competitor_variance': comp_variance,
            'competitor_median_diameter': comp_median_diam,
            'recency_score': recency_score,
            'career_phase': float(career_phase),
            'seasonal_month_sin': float(np.sin(month_rad)),
            'seasonal_month_cos': float(np.cos(month_rad)),
            'event_x_diameter': float(event_enc) * float(wood.diameter_mm),
        }

        feature_cols = list(ml_config.FEATURE_NAMES)
        features = pd.DataFrame([feature_payload])[feature_cols]

        try:
            base_prediction = float(model.predict(features)[0])

            # Apply quality adjustment (+-2% per quality point from 5)
            quality_offset = quality - 5
            quality_factor = 1.0 + (quality_offset * 0.02)
            predicted_time = base_prediction * quality_factor

            # Sanity check
            if predicted_time < ml_config.MIN_PREDICTION_TIME or predicted_time > ml_config.MAX_PREDICTION_TIME:
                return None

            confidence = "HIGH" if experience >= data_req.HIGH_CONFIDENCE_MIN_EVENTS else "MEDIUM"
            explanation = f"{event_upper} ML model ({self._training_data_size} training records)"
            if quality != 5:
                adj_pct = (quality_factor - 1.0) * 100
                explanation += f", quality {quality}/10 ({adj_pct:+.0f}%)"

            return PredictionResult(
                value=predicted_time,
                confidence=confidence,
                method='ml',
                explanation=explanation,
            )

        except Exception as e:
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

    # Get competitor history (event + species filtered, with diameter normalization)
    history_with_weights = []
    data_source = "no history"
    tournament_weighted = False
    historical_baseline = None

    if internal_df is not None and not internal_df.empty:
        comp_df = internal_df[internal_df['event'] == event_upper].copy()

        if not comp_df.empty:
            for _, row in comp_df.iterrows():
                time_val = row.get('raw_time')
                if time_val is None or pd.isna(time_val) or float(time_val) <= 0:
                    continue
                hist_d = row.get('size_mm', wood.diameter_mm)
                hist_q = row.get('quality', 5.0)

                # Normalize to target diameter
                normalized = float(time_val)
                if hist_d and wood.diameter_mm and float(hist_d) != float(wood.diameter_mm):
                    exponent = get_event_scaling_exponent(combined, event_upper)
                    factor = calculate_scaling_factor(float(hist_d), float(wood.diameter_mm), exponent)
                    normalized = normalized * factor

                # Normalize quality to 5
                hist_q_int = max(1, min(10, int(hist_q) if not pd.isna(hist_q) else 5))
                if hist_q_int != 5:
                    q_factor = 1.0 + ((hist_q_int - 5) * 0.02)
                    if q_factor > 0:
                        normalized = normalized / q_factor

                result_date = row.get('date')
                w = _calculate_weight_simple(result_date)
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
                event_upper, wood.species, wood.diameter_mm, combined,
                exclude_competitor=competitor.name
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
                event_upper, wood.species, wood.diameter_mm, combined,
                exclude_competitor=competitor.name
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
                event_upper, wood.species, wood.diameter_mm, combined,
                exclude_competitor=competitor.name
            )
            if baseline is not None:
                confidence = "LOW"
                data_source = f"event baseline ({event_base_expl})"

    if baseline is None:
        return None

    historical_baseline = baseline

    # Tournament result weighting (97% same-wood, 3% historical) -- V4.4 feature
    if competitor.tournament_time is not None:
        tournament_time = competitor.tournament_time
        baseline = (tournament_time * 0.97) + (historical_baseline * 0.03)
        confidence = "VERY HIGH"
        tournament_weighted = True
        data_source = (
            f"Tournament result ({tournament_time:.1f}s @ 97%) + "
            f"{data_source} (@ 3%)"
        )

    # Apply quality multiplier (statistical adjustment, quality != 5)
    if quality != 5:
        quality_factor = 1.0 + ((quality - 5) * 0.02)
        baseline = baseline * quality_factor
        adj_pct = (quality_factor - 1.0) * 100
        quality_label = "softer" if quality < 5 else "harder"
        data_source += f" [Quality {quality}/10: {quality_label}, {adj_pct:+.0f}%]"

    explanation = f"Predicted {baseline:.1f}s ({data_source})"

    return PredictionResult(
        value=baseline,
        confidence=confidence,
        method='baseline',
        explanation=explanation,
        metadata={
            'tournament_weighted': tournament_weighted,
            'historical_baseline': historical_baseline,
        },
    )


def _competitor_history_to_df(competitor: CompetitorRecord) -> Optional[pd.DataFrame]:
    """Convert competitor's HistoricalResult list to a standardized DataFrame."""
    if not competitor.history:
        return None

    rows = []
    for h in competitor.history:
        rows.append({
            'competitor_name': competitor.name,
            'event': str(h.event_code).strip().upper(),
            'raw_time': h.time_seconds,
            'species': str(h.species).strip() if h.species else '',
            'size_mm': h.diameter_mm,
            'quality': h.quality,
            'date': h.result_date,
        })

    df = pd.DataFrame(rows)
    df['raw_time'] = pd.to_numeric(df['raw_time'], errors='coerce')
    df['size_mm'] = pd.to_numeric(df['size_mm'], errors='coerce')
    df = df[df['raw_time'] > 0]
    return df if not df.empty else None


def _calculate_weight_simple(result_date, half_life_days: int = 730) -> float:
    """Compute exponential decay weight for a single date."""
    return calculate_performance_weight(result_date, None, half_life_days)


# ---------------------------------------------------------------------------
# LLM prediction (ported from STRATHEX ai_predictor.py)
# ---------------------------------------------------------------------------

def predict_with_llm(
    competitor: CompetitorRecord,
    wood: WoodProfile,
    event_code: str,
    baseline_time: float,
    ollama_url: str = "http://localhost:11434",
    model: str = "qwen2.5:32b",
    timeout: int = 120,
    tournament_weighted: bool = False,
    historical_baseline: Optional[float] = None,
    wood_df: Optional[pd.DataFrame] = None,
) -> Optional[PredictionResult]:
    """
    Apply LLM quality-adjustment multiplier on top of the baseline time.

    Calls Ollama locally with a structured prompt that instructs the LLM to return
    a quality multiplier (0.85-1.15). Falls back gracefully if Ollama is unavailable.

    This function implements a multi-stage prediction process:
    1. Get baseline time (passed in as argument)
    2. Apply tournament context section if applicable
    3. Use LLM to adjust baseline for wood quality
    4. Fallback to statistical adjustment if LLM unavailable

    Args:
        competitor: Competitor record (used for name and context).
        wood: Wood profile with quality rating.
        event_code: 'SB' or 'UH'.
        baseline_time: Pre-computed baseline time (seconds) at quality=5.
        ollama_url: Ollama API base URL.
        model: Ollama model name.
        timeout: Request timeout in seconds.
        tournament_weighted: If True, include tournament context in prompt.
        historical_baseline: Historical baseline before tournament weighting (for prompt context).
        wood_df: Wood properties DataFrame for species database text.

    Returns:
        PredictionResult with method='llm', or None if Ollama is unreachable.
    """
    quality = max(1, min(10, int(wood.quality)))
    tournament_time = competitor.tournament_time

    # Build wood species database text for prompt
    wood_data_text = ""
    if wood_df is not None and not wood_df.empty:
        wood_data_text = "\nAVAILABLE WOOD SPECIES DATABASE:\n"
        for _, row in wood_df.iterrows():
            species_name = row.get('species', 'Unknown')
            wood_data_text += f"  - {species_name}"
            if 'hardness_category' in row:
                wood_data_text += f": Category={row.get('hardness_category', 'N/A')}"
            if 'base_adjustment_pct' in row:
                wood_data_text += f", Base Adjustment={row.get('base_adjustment_pct', 0):+.1f}%"
            if 'description' in row:
                wood_data_text += f", Description: {row.get('description', '')}"
            wood_data_text += "\n"

    # Build tournament context section
    historical_baseline_for_context = historical_baseline if historical_baseline is not None else baseline_time
    tournament_context_section = ""
    if tournament_weighted and tournament_time:
        tournament_context_section = f"""
TOURNAMENT CONTEXT - CRITICAL INFORMATION

This competitor has ALREADY COMPETED in this tournament on THIS EXACT WOOD.
Tournament result: {tournament_time:.1f} seconds (recorded in heat/semi, same block)

IMPORTANCE OF SAME-WOOD DATA:
- Same wood across rounds = MOST ACCURATE predictor possible
- Tournament result from TODAY beats historical data from YEARS AGO
- System applies 97% weight to tournament time, 3% to historical baseline
- Your quality adjustment should be MINIMAL - wood characteristics already proven

BASELINE CALCULATION FOR THIS CASE:
Baseline {baseline_time:.1f}s = (Tournament {tournament_time:.1f}s x 97%) + (Historical {historical_baseline_for_context:.1f}s x 3%)

YOUR TASK: Apply MINOR quality adjustment ONLY if wood quality has changed since tournament round.
Expected adjustment range: +-1-3% maximum (wood is proven via tournament result)
Do NOT apply standard quality adjustments - this is PROVEN data from SAME WOOD.
"""

    def _quality_label(q):
        if q == 6: return "Expected multiplier: 1.01-1.03 (increase baseline by 1-3%)"
        if q == 7: return "Expected multiplier: 1.03-1.05 (increase baseline by 3-5%)"
        if q == 8: return "Expected multiplier: 1.05-1.08 (increase baseline by 5-8%)"
        if q >= 9: return "Expected multiplier: 1.08-1.12 (increase baseline by 8-12%)"
        if q == 4: return "Expected multiplier: 0.97-0.99 (reduce baseline by 1-3%)"
        if q == 3: return "Expected multiplier: 0.95-0.97 (reduce baseline by 3-5%)"
        if q == 2: return "Expected multiplier: 0.93-0.95 (reduce baseline by 5-7%)"
        if q == 1: return "Expected multiplier: 0.85-0.90 (reduce baseline by 10-15%)"
        return "Expected multiplier: 1.00 (no change)"

    def _target_range(q, base):
        if q == 6: return f"Target range: {base*1.01:.1f}s - {base*1.03:.1f}s"
        if q == 7: return f"Target range: {base*1.03:.1f}s - {base*1.05:.1f}s"
        if q == 8: return f"Target range: {base*1.05:.1f}s - {base*1.08:.1f}s"
        if q >= 9: return f"Target range: {base*1.08:.1f}s - {base*1.12:.1f}s"
        if q == 4: return f"Target range: {base*0.97:.1f}s - {base*0.99:.1f}s"
        if q == 3: return f"Target range: {base*0.95:.1f}s - {base*0.97:.1f}s"
        if q == 2: return f"Target range: {base*0.93:.1f}s - {base*0.95:.1f}s"
        if q == 1: return f"Target range: {base*0.85:.1f}s - {base*0.90:.1f}s"
        return f"Target: {base:.1f}s (baseline)"

    if quality > 5:
        direction_note = f"Quality {quality} > 5 means HARDER wood -> SLOWER cutting -> HIGHER time than baseline"
    elif quality < 5:
        direction_note = f"Quality {quality} < 5 means SOFTER wood -> FASTER cutting -> LOWER time than baseline"
    else:
        direction_note = "Quality 5 = baseline assumption -> NO ADJUSTMENT needed"

    prompt = f"""You are a master woodchopping handicapper making precision time predictions for competition.

HANDICAPPING OBJECTIVE

Your prediction must account for wood characteristics and competitor ability to create fair handicaps.
When handicaps are applied, all competitors should finish simultaneously if your predictions are accurate.
This requires deep understanding of how wood properties affect cutting times.

COMPETITOR PROFILE

Name: {competitor.name}
Baseline Time: {baseline_time:.1f} seconds
Confidence Level: {"VERY HIGH (tournament data)" if tournament_weighted else "HIGH"}
{tournament_context_section}
BASELINE INTERPRETATION:
- {"This baseline is HEAVILY WEIGHTED (97%) toward same-tournament result - wood is PROVEN" if tournament_weighted else "This baseline assumes QUALITY 5 wood (average hardness)"}
- Your task is to adjust this baseline for the ACTUAL quality rating
- {"Apply MINIMAL adjustment - tournament result already reflects wood characteristics" if tournament_weighted else "Historical data already accounts for competitor's skill level and typical conditions"}

WOOD SPECIFICATIONS

Species: {wood.species}
Diameter: {wood.diameter_mm:.0f}mm
Quality Rating: {quality}/10
Event Type: {event_code}
{wood_data_text}
QUALITY RATING SYSTEM - CRITICAL UNDERSTANDING

Quality measures wood HARDNESS on a 1-10 scale:
- HIGHER number = HARDER wood = SLOWER cutting = HIGHER time
- LOWER number = SOFTER wood = FASTER cutting = LOWER time

5 = AVERAGE HARDNESS (BASELINE REFERENCE POINT)
   - This is what the baseline time assumes
   - MULTIPLY baseline by 1.00 (NO ADJUSTMENT)

CURRENT SITUATION ANALYSIS

Baseline time: {baseline_time:.1f}s (assumes quality 5 wood)
Your wood quality: {quality}/10
Quality deviation: {quality - 5:+d} points from baseline reference

CRITICAL CALCULATION DIRECTION:
{direction_note}

{_quality_label(quality)}
{_target_range(quality, baseline_time)}

RESPONSE REQUIREMENT

Return your analysis in this EXACT format (3 parts separated by " | "):

<multiplier> | <confidence> | <explanation>

Where:
- <multiplier> = decimal between 0.85 and 1.15 (e.g., 1.07)
- <confidence> = HIGH, MEDIUM, or LOW based on quality certainty
- <explanation> = ONE sentence explaining quality adjustment reasoning (max 15 words)

Examples:
1.07 | HIGH | Quality 8 wood increases cutting resistance by approximately 7%
0.95 | HIGH | Quality 3 wood reduces cutting time by approximately 5%
1.00 | MEDIUM | Quality 5 is average, no adjustment needed

Your response:"""

    # Call Ollama
    response = _call_ollama(prompt, ollama_url=ollama_url, model=model, timeout=timeout)

    if response is None:
        # Statistical fallback when LLM unavailable
        quality_adjustment = (quality - 5) * 0.02
        predicted_time = baseline_time * (1 + quality_adjustment)
        explanation = f"Predicted {predicted_time:.1f}s (statistical quality adjustment, LLM unavailable)"
        return None  # Return None so cascade falls through to baseline

    try:
        response = response.strip()

        if '|' in response:
            parts = [p.strip() for p in response.split('|')]
            if len(parts) >= 3:
                multiplier_str = parts[0]
                llm_confidence = parts[1].upper().strip()
                quality_explanation = parts[2]

                numbers = re.findall(r'\d+\.?\d*', multiplier_str)
                if numbers:
                    multiplier = float(numbers[0])

                    if llm_config.QUALITY_MULTIPLIER_MIN <= multiplier <= llm_config.QUALITY_MULTIPLIER_MAX:
                        predicted_time = baseline_time * multiplier

                        if baseline_time * 0.5 <= predicted_time <= baseline_time * 1.5:
                            # Combine baseline confidence with LLM confidence
                            final_confidence = "HIGH"
                            if tournament_weighted:
                                final_confidence = "VERY HIGH"
                            elif llm_confidence == "LOW":
                                final_confidence = "MEDIUM"

                            explanation = (
                                f"Predicted {predicted_time:.1f}s (LLM calibrated: {quality_explanation})"
                            )

                            return PredictionResult(
                                value=predicted_time,
                                confidence=final_confidence,
                                method='llm',
                                explanation=explanation,
                                metadata={
                                    'multiplier': multiplier,
                                    'quality_explanation': quality_explanation,
                                    'tournament_weighted': tournament_weighted,
                                },
                            )

        # Try fallback: parse first number as multiplier
        numbers = re.findall(r'\d+\.?\d*', response)
        if numbers:
            multiplier = float(numbers[0])
            if llm_config.QUALITY_MULTIPLIER_MIN <= multiplier <= llm_config.QUALITY_MULTIPLIER_MAX:
                predicted_time = baseline_time * multiplier
                if baseline_time * 0.5 <= predicted_time <= baseline_time * 1.5:
                    explanation = f"Predicted {predicted_time:.1f}s (LLM calibrated)"
                    return PredictionResult(
                        value=predicted_time,
                        confidence="MEDIUM",
                        method='llm',
                        explanation=explanation,
                        metadata={'multiplier': multiplier},
                    )

    except Exception:
        pass

    return None


def _call_ollama(
    prompt: str,
    ollama_url: str = "http://localhost:11434",
    model: str = "qwen2.5:32b",
    timeout: int = 120,
) -> Optional[str]:
    """
    Send a prompt to the Ollama API and return the response text.

    Args:
        prompt: The prompt to send.
        ollama_url: Base URL for Ollama (without /api/generate suffix).
        model: Model name.
        timeout: Request timeout in seconds.

    Returns:
        Response text, or None if Ollama is unavailable or request fails.
    """
    try:
        import requests
        import json

        url = ollama_url.rstrip('/') + '/api/generate'
        payload = {
            'model': model,
            'prompt': prompt,
            'stream': False,
        }

        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()

        result = response.json()
        return result.get('response', '').strip()

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Top-level cascade functions
# ---------------------------------------------------------------------------

def get_best_prediction(
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
        2. LLM prediction   -- baseline + quality multiplier from Ollama
                              (requires llm_client; graceful fallback if unavailable)
        3. ML model         -- XGBoost trained on historical data with time-decay weights
                              (requires ml_model; skipped if not trained)
        4. Weighted baseline -- time-decay-weighted historical average with diameter
                              scaling (see decay.py, wood.py)
        5. Panel mark        -- division-based default for competitors with no history
                              (see fallback.py)

    Tournament result weighting (V4.4 feature from STRATHEX):
        If competitor.tournament_time is set, the baseline is computed as:
            weighted_baseline = (tournament_time * 0.97) + (historical_avg * 0.03)
        This applies to levels 2, 3, and 4. Manual override (level 1) is unaffected.

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

    # Priority 1: Manual override
    if competitor.manual_time_override is not None:
        t = float(competitor.manual_time_override)
        return PredictionResult(
            value=t,
            confidence="VERY HIGH",
            method='manual',
            explanation=f"Manual override: {t:.1f}s (operator-supplied)",
        )

    # Compute baseline for LLM and as fallback
    baseline_result = predict_baseline(
        competitor, wood, event_code, results_df, wood_data_df
    )

    # Priority 2: LLM prediction (requires Ollama and a working baseline)
    if llm_client is not None and baseline_result is not None:
        ollama_url = llm_client.get('url', 'http://localhost:11434') if isinstance(llm_client, dict) else 'http://localhost:11434'
        ollama_model = llm_client.get('model', llm_config.PREDICTION_MODEL) if isinstance(llm_client, dict) else llm_config.PREDICTION_MODEL
        ollama_timeout = llm_client.get('timeout', llm_config.TIMEOUT_SECONDS) if isinstance(llm_client, dict) else llm_config.TIMEOUT_SECONDS

        tournament_weighted = baseline_result.metadata.get('tournament_weighted', False)
        historical_baseline = baseline_result.metadata.get('historical_baseline')

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
            return llm_result

    # Priority 3: ML model
    if ml_model is not None:
        ml_result = ml_model.predict(competitor, wood, event_code, results_df)
        if ml_result is not None:
            return ml_result

    # Priority 4: Weighted baseline
    if baseline_result is not None:
        return baseline_result

    # Priority 5: Panel mark fallback (unconditional)
    panel_mark, panel_expl = get_qaa_panel_mark(event_code, competitor.division)

    # Scale panel mark from 300mm standard to target diameter
    from strathmark.wood import calculate_scaling_factor as _csf
    if wood.diameter_mm != 300.0:
        exponent = get_event_scaling_exponent(results_df, event_code)
        factor = _csf(300.0, wood.diameter_mm, exponent)
        panel_mark = panel_mark * factor

    return PredictionResult(
        value=panel_mark,
        confidence="VERY LOW",
        method='panel',
        explanation=f"Panel mark fallback: {panel_expl}",
    )


def get_all_predictions(
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
        'manual': None,
        'llm': None,
        'ml': None,
        'baseline': None,
        'panel': None,
    }

    # Manual override
    if competitor.manual_time_override is not None:
        t = float(competitor.manual_time_override)
        results['manual'] = PredictionResult(
            value=t,
            confidence="VERY HIGH",
            method='manual',
            explanation=f"Manual override: {t:.1f}s (operator-supplied)",
        )

    # Baseline
    baseline_result = predict_baseline(competitor, wood, event_code, results_df, wood_data_df)
    results['baseline'] = baseline_result

    # LLM
    if llm_client is not None and baseline_result is not None:
        ollama_url = llm_client.get('url', 'http://localhost:11434') if isinstance(llm_client, dict) else 'http://localhost:11434'
        ollama_model = llm_client.get('model', llm_config.PREDICTION_MODEL) if isinstance(llm_client, dict) else llm_config.PREDICTION_MODEL
        ollama_timeout = llm_client.get('timeout', llm_config.TIMEOUT_SECONDS) if isinstance(llm_client, dict) else llm_config.TIMEOUT_SECONDS

        tournament_weighted = baseline_result.metadata.get('tournament_weighted', False)
        historical_baseline = baseline_result.metadata.get('historical_baseline')

        results['llm'] = predict_with_llm(
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
        results['ml'] = ml_model.predict(competitor, wood, event_code, results_df)

    # Panel mark
    panel_mark, panel_expl = get_qaa_panel_mark(event_code, competitor.division)
    from strathmark.wood import calculate_scaling_factor as _csf
    if wood.diameter_mm != 300.0:
        exponent = get_event_scaling_exponent(results_df, event_code)
        factor = _csf(300.0, wood.diameter_mm, exponent)
        panel_mark = panel_mark * factor

    results['panel'] = PredictionResult(
        value=panel_mark,
        confidence="VERY LOW",
        method='panel',
        explanation=f"Panel mark fallback: {panel_expl}",
    )

    return results
