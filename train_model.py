"""
STRATHMARK Model Training Pipeline
====================================

Trains event-specific XGBoost models for woodchopping time prediction.

Usage:
    # Train from database (primary path)
    python train_model.py

    # Train from Excel (legacy bootstrapping)
    python train_model.py --legacy-import woodchopping_clean.xlsx

    # Test different decay half-lives
    python train_model.py --tune-halflife

Outputs:
    models/{event_type}_{timestamp}/model.json
    models/{event_type}_{timestamp}/metadata.json
    models/combined_{timestamp}/model.json (when per-event data is insufficient)

Design rules:
    - Recency-weighted training (2-year half-life exponential decay)
    - Temporal expanding-window cross-validation (no data leakage)
    - Separate models per event type (SB, UH) when >= 30 records each
    - Falls back to combined model when per-event data is insufficient
    - Model artifacts include SHA256 hash and training metadata
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_RECORDS_PER_EVENT = 30
"""Minimum records per event type to train a separate model."""

MIN_RECORDS_COMBINED = 30
"""Minimum total records to train the combined model."""

DEFAULT_HALF_LIFE_DAYS = 730
"""Default exponential decay half-life (2 years)."""

CV_STEP_MONTHS = 6
"""Temporal CV fold step in months."""

CV_MIN_TRAIN_MONTHS = 12
"""Minimum training window before first CV fold (months)."""

MODELS_DIR = Path("models")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_from_db() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load results and wood data from the Supabase database.

    Returns:
        (results_df, wood_df) DataFrames.
    """
    from strathmark.db import pull_results, pull_competitors
    from strathmark.loader import load_woodchopping_xlsx

    _log.info("Loading results from database...")
    results_df = pull_results()
    _log.info("Loaded %d results from database.", len(results_df))

    # Wood data is not in the database; load from the bundled Excel
    xlsx_candidates = [
        Path("woodchopping_clean.xlsx"),
        Path("strathmark/woodchopping_clean.xlsx"),
    ]
    for p in xlsx_candidates:
        if p.exists():
            wood_df, _, _ = load_woodchopping_xlsx(str(p))
            return results_df, wood_df

    _log.warning("woodchopping_clean.xlsx not found; wood properties will use defaults.")
    return results_df, pd.DataFrame()


def load_from_xlsx(path: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load results and wood data from a legacy Excel workbook.

    Args:
        path: Path to the .xlsx workbook.

    Returns:
        (results_df, wood_df) DataFrames.
    """
    from strathmark.loader import load_woodchopping_xlsx
    _log.info("Loading legacy data from %s...", path)
    wood_df, competitor_df, results_df = load_woodchopping_xlsx(path)
    _log.info(
        "Loaded %d results, %d competitors, %d wood species from Excel.",
        len(results_df), len(competitor_df), len(wood_df),
    )
    return results_df, wood_df


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def build_feature_matrix(
    results_df: pd.DataFrame,
    wood_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.Series]:
    """
    Build the XGBoost feature matrix from raw results.

    Features (25 total, matching MLConfig.FEATURE_NAMES):
        1.  competitor_avg_time_by_event  - leave-one-out mean per competitor/event
        2.  event_encoded                 - 0=SB, 1=UH
        3.  size_mm                       - block diameter
        4.  wood_janka_hardness           - Janka N (continuous)
        5.  wood_spec_gravity             - specific gravity (continuous)
        6.  wood_shear_strength           - shear strength
        7.  wood_crush_strength           - crush strength
        8.  wood_MOR                      - modulus of rupture
        9.  wood_MOE                      - modulus of elasticity
        10. competitor_experience         - total result count for competitor
        11. competitor_trend_slope        - linear trend (seconds/day) last 5 results
        12. wood_quality                  - quality rating 1-10
        13. diameter_squared              - size_mm ** 2
        14. quality_x_diameter            - quality * size_mm
        15. quality_x_hardness            - quality * janka_hard
        16. experience_x_size             - experience * size_mm
        17. competitor_variance           - std dev of competitor times
        18. competitor_median_diameter    - median historical diameter
        19. recency_score                 - decay-weighted recency (days since last result)
        20. career_phase                  - 0=early(<5 results), 1=mid(5-20), 2=established(>20)
        21. seasonal_month_sin            - sin(2*pi*month/12)
        22. seasonal_month_cos            - cos(2*pi*month/12)
        23. event_x_diameter              - event_encoded * size_mm
        24. peer_event_avg_time           - avg time for competitor in OTHER event
        25. uh_to_sb_ratio                - competitor's UH/SB mean ratio

    Also computes per-row sample_weight using exponential time-decay.

    Args:
        results_df: Standardized results DataFrame.
        wood_df: Wood species properties DataFrame.

    Returns:
        (feature_df, target_series) where target is raw_time.
    """
    from strathmark.utils import standardize_results_columns
    from strathmark.wood import get_species_properties

    df = standardize_results_columns(results_df).copy()
    df = df.dropna(subset=['raw_time', 'event', 'competitor_name', 'size_mm'])
    df = df[df['raw_time'] > 0]

    # Normalize event codes
    df['event'] = df['event'].str.upper().str.strip()
    df = df[df['event'].isin(['SB', 'UH'])]

    if df.empty:
        raise ValueError("No valid training rows after filtering.")

    _log.info("Building features for %d rows...", len(df))

    # Parse dates for decay weighting
    today = datetime.now().date()
    if 'result_date' in df.columns:
        df['result_date'] = pd.to_datetime(df['result_date'], errors='coerce')
    else:
        df['result_date'] = pd.NaT

    # --- Wood properties ---
    _wood_cache: Dict[str, object] = {}

    def _get_props(species):
        if species not in _wood_cache:
            _wood_cache[species] = get_species_properties(species, wood_df if not wood_df.empty else None)
        return _wood_cache[species]

    df['_props'] = df['species'].apply(_get_props) if 'species' in df.columns else None

    def _prop(col, default):
        if df['_props'] is None:
            return default
        return df['_props'].apply(lambda p: getattr(p, col, default) if p else default)

    df['wood_janka_hardness'] = _prop('janka_hardness', 1690.0) if 'species' in df.columns else 1690.0
    df['wood_spec_gravity'] = _prop('specific_gravity', 0.34) if 'species' in df.columns else 0.34
    df['wood_shear_strength'] = _prop('shear_strength', 5.0) if 'species' in df.columns else 5.0
    df['wood_crush_strength'] = _prop('crush_strength', 30.0) if 'species' in df.columns else 30.0
    df['wood_MOR'] = _prop('mor', 50.0) if 'species' in df.columns else 50.0
    df['wood_MOE'] = _prop('moe', 8.0) if 'species' in df.columns else 8.0

    # Drop helper column
    df = df.drop(columns=['_props'], errors='ignore')

    # --- Event encoding ---
    df['event_encoded'] = (df['event'] == 'UH').astype(int)

    # --- Diameter squared ---
    df['diameter_squared'] = df['size_mm'] ** 2

    # --- Wood quality (default 5 if not present) ---
    if 'quality' not in df.columns:
        df['wood_quality'] = 5.0
    else:
        df['wood_quality'] = df['quality'].fillna(5.0).clip(1, 10)

    # --- Gender encoding ---
    if 'gender' in df.columns:
        df['gender_encoded'] = df['gender'].map({'M': 0, 'F': 1, 'male': 0, 'female': 1}).fillna(0).astype(int)
    else:
        df['gender_encoded'] = 0

    # --- Per-competitor aggregates (leave-one-out to avoid data leakage) ---
    grp_event = df.groupby(['competitor_name', 'event'])['raw_time']
    df['competitor_count_event'] = grp_event.transform('count')
    df['competitor_sum_event'] = grp_event.transform('sum')
    # Leave-one-out mean: (sum - this_value) / (count - 1)
    df['competitor_avg_time_by_event'] = (
        (df['competitor_sum_event'] - df['raw_time']) /
        (df['competitor_count_event'] - 1).clip(lower=1)
    )

    # Experience (total results per competitor, all events)
    df['competitor_experience'] = df.groupby('competitor_name')['raw_time'].transform('count')

    # Competitor variance
    df['competitor_variance'] = df.groupby(['competitor_name', 'event'])['raw_time'].transform('std').fillna(3.0)

    # Median diameter for competitor
    df['competitor_median_diameter'] = df.groupby('competitor_name')['size_mm'].transform('median')

    # Career phase
    df['career_phase'] = pd.cut(
        df['competitor_experience'],
        bins=[0, 5, 20, float('inf')],
        labels=[0, 1, 2],
    ).astype(float).fillna(0)

    # Trend slope (linear regression on last 5 results per competitor/event)
    def _trend_slope(group):
        if len(group) < 3:
            return pd.Series(0.0, index=group.index)
        # Sort by date
        g = group.copy()
        if 'result_date' in g.columns:
            g = g.sort_values('result_date')
        times = g['raw_time'].values
        slopes = np.zeros(len(g))
        for i in range(len(g)):
            window = times[max(0, i-4):i+1]
            if len(window) >= 3:
                x = np.arange(len(window))
                slope = np.polyfit(x, window, 1)[0]
                slopes[i] = slope
        return pd.Series(slopes, index=g.index)

    df['competitor_trend_slope'] = (
        df.groupby(['competitor_name', 'event'], group_keys=False)
        .apply(_trend_slope)
        .fillna(0.0)
    )

    # Peer event avg (UH<->SB cross-event)
    peer_event = df.copy()
    peer_event['peer_event'] = peer_event['event'].map({'SB': 'UH', 'UH': 'SB'})
    peer_means = (
        peer_event.groupby(['competitor_name', 'event'])['raw_time']
        .mean()
        .rename('peer_event_avg_time')
    )
    # Remap: competitor's UH avg -> for SB rows, and vice versa
    peer_map = {}
    for (name, evt), val in peer_means.items():
        # This is the avg in 'evt' event -> use as peer for opposite event
        opposite = 'SB' if evt == 'UH' else 'UH'
        peer_map[(name, opposite)] = val

    df['peer_event_avg_time'] = df.apply(
        lambda r: peer_map.get((r['competitor_name'], r['event']),
                               r['competitor_avg_time_by_event']),
        axis=1,
    )

    # UH/SB ratio per competitor
    sb_means = df[df['event'] == 'SB'].groupby('competitor_name')['raw_time'].mean().rename('sb_mean')
    uh_means = df[df['event'] == 'UH'].groupby('competitor_name')['raw_time'].mean().rename('uh_mean')
    ratio_df = pd.concat([sb_means, uh_means], axis=1)
    ratio_df['uh_to_sb_ratio'] = (ratio_df['uh_mean'] / ratio_df['sb_mean'].clip(lower=1.0)).fillna(1.0)
    df = df.join(ratio_df['uh_to_sb_ratio'], on='competitor_name', how='left')
    df['uh_to_sb_ratio'] = df['uh_to_sb_ratio'].fillna(1.0)

    # Interaction features
    df['quality_x_diameter'] = df['wood_quality'] * df['size_mm']
    df['quality_x_hardness'] = df['wood_quality'] * df['wood_janka_hardness']
    df['experience_x_size'] = df['competitor_experience'] * df['size_mm']
    df['event_x_diameter'] = df['event_encoded'] * df['size_mm']

    # Seasonal features (from result_date)
    df['_month'] = df['result_date'].dt.month.fillna(6).astype(float)
    df['seasonal_month_sin'] = np.sin(2 * np.pi * df['_month'] / 12)
    df['seasonal_month_cos'] = np.cos(2 * np.pi * df['_month'] / 12)

    # Recency score (days since result / 365, capped at 5)
    def _days_since(d):
        if pd.isna(d):
            return 365.0  # default 1 year
        return max(0.0, (datetime.now() - d).days)

    df['recency_score'] = df['result_date'].apply(_days_since) / 365.0
    df['recency_score'] = df['recency_score'].clip(0, 5)

    # --- Compute sample weights (exponential time-decay) ---
    def _weight(d, half_life=DEFAULT_HALF_LIFE_DAYS):
        if pd.isna(d):
            return 0.5  # moderate weight for undated results
        days_old = max(0, (datetime.now() - d).days)
        return 0.5 ** (days_old / half_life)

    df['sample_weight'] = df['result_date'].apply(_weight)

    # --- Assemble feature matrix ---
    feature_cols = [
        'competitor_avg_time_by_event',
        'event_encoded',
        'size_mm',
        'wood_janka_hardness',
        'wood_spec_gravity',
        'wood_shear_strength',
        'wood_crush_strength',
        'wood_MOR',
        'wood_MOE',
        'competitor_experience',
        'competitor_trend_slope',
        'wood_quality',
        'diameter_squared',
        'quality_x_diameter',
        'quality_x_hardness',
        'experience_x_size',
        'competitor_variance',
        'competitor_median_diameter',
        'recency_score',
        'career_phase',
        'seasonal_month_sin',
        'seasonal_month_cos',
        'event_x_diameter',
        'peer_event_avg_time',
        'uh_to_sb_ratio',
        'gender_encoded',
        'sample_weight',
    ]

    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        _log.warning("Missing feature columns: %s. Filling with 0.", missing)
        for c in missing:
            df[c] = 0.0

    feature_df = df[feature_cols + ['result_date']].copy()
    target = df['raw_time']

    return feature_df, target


# ---------------------------------------------------------------------------
# Temporal cross-validation
# ---------------------------------------------------------------------------

def temporal_cv(
    feature_df: pd.DataFrame,
    target: pd.Series,
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
) -> Dict:
    """
    Expanding-window temporal cross-validation.

    Trains on all data before date T, validates on next CV_STEP_MONTHS months.
    Steps forward and repeats. Reports RMSE and MAE at each fold.

    Args:
        feature_df: Feature matrix including 'result_date' and 'sample_weight' columns.
        target: Target times.
        half_life_days: Decay half-life for sample weighting.

    Returns:
        Dict with keys: fold_results, overall_rmse, overall_mae, n_folds.
    """
    try:
        import xgboost as xgb
    except ImportError:
        _log.error("xgboost not installed. Run: pip install xgboost")
        return {'overall_rmse': float('inf'), 'overall_mae': float('inf'), 'n_folds': 0, 'fold_results': []}

    dates = feature_df['result_date'].dropna()
    if dates.empty:
        _log.warning("No dates available for temporal CV. Using RMSE=0 placeholder.")
        return {'overall_rmse': 0.0, 'overall_mae': 0.0, 'n_folds': 0, 'fold_results': []}

    min_date = dates.min()
    max_date = dates.max()

    feature_cols = [c for c in feature_df.columns if c not in ('result_date', 'sample_weight')]

    fold_results = []
    all_errors = []

    # Start validation after CV_MIN_TRAIN_MONTHS of data
    from dateutil.relativedelta import relativedelta  # type: ignore
    val_start = min_date + relativedelta(months=CV_MIN_TRAIN_MONTHS)

    fold_idx = 0
    while val_start < max_date:
        val_end = val_start + relativedelta(months=CV_STEP_MONTHS)

        train_mask = feature_df['result_date'] < val_start
        val_mask = (feature_df['result_date'] >= val_start) & (feature_df['result_date'] < val_end)

        X_train = feature_df.loc[train_mask, feature_cols]
        y_train = target.loc[train_mask]
        w_train = feature_df.loc[train_mask, 'sample_weight']
        X_val = feature_df.loc[val_mask, feature_cols]
        y_val = target.loc[val_mask]

        if len(X_train) < 10 or len(X_val) < 3:
            val_start = val_end
            continue

        model = xgb.XGBRegressor(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            objective='reg:squarederror',
            tree_method='hist',
            random_state=42,
            verbosity=0,
        )
        model.fit(X_train, y_train, sample_weight=w_train)
        preds = model.predict(X_val)

        errors = y_val.values - preds
        rmse = float(np.sqrt(np.mean(errors ** 2)))
        mae = float(np.mean(np.abs(errors)))

        fold_results.append({
            'fold': fold_idx,
            'train_start': str(min_date.date()),
            'val_start': str(val_start.date()),
            'val_end': str(val_end.date()),
            'n_train': len(X_train),
            'n_val': len(X_val),
            'rmse': round(rmse, 3),
            'mae': round(mae, 3),
        })
        all_errors.extend(errors.tolist())

        fold_idx += 1
        val_start = val_end

    if not all_errors:
        return {'overall_rmse': 0.0, 'overall_mae': 0.0, 'n_folds': 0, 'fold_results': []}

    overall_rmse = float(np.sqrt(np.mean(np.array(all_errors) ** 2)))
    overall_mae = float(np.mean(np.abs(np.array(all_errors))))

    _log.info(
        "Temporal CV: %d folds, overall RMSE=%.3f, MAE=%.3f",
        len(fold_results), overall_rmse, overall_mae,
    )
    for f in fold_results:
        _log.info("  Fold %d [%s - %s]: n_train=%d n_val=%d RMSE=%.3f MAE=%.3f",
                  f['fold'], f['val_start'], f['val_end'],
                  f['n_train'], f['n_val'], f['rmse'], f['mae'])

    return {
        'overall_rmse': round(overall_rmse, 3),
        'overall_mae': round(overall_mae, 3),
        'n_folds': len(fold_results),
        'fold_results': fold_results,
    }


# ---------------------------------------------------------------------------
# Half-life tuning
# ---------------------------------------------------------------------------

def tune_halflife(
    feature_df: pd.DataFrame,
    target: pd.Series,
) -> Dict:
    """
    Test half-lives from 6 months to 5 years in 6-month increments.

    For each half-life, runs full temporal CV and reports RMSE.
    Returns summary table and optimal half-life.

    Args:
        feature_df: Feature matrix with 'result_date' column.
        target: Target times.

    Returns:
        Dict with keys: summary_table (list of dicts), optimal_half_life_days.
    """
    half_lives_days = [int(m * 30.44) for m in range(6, 61, 6)]
    results = []

    _log.info("Testing %d half-life values...", len(half_lives_days))
    for hl in half_lives_days:
        months = hl / 30.44
        _log.info("  Testing half-life = %.0f months (%d days)...", months, hl)

        # Recompute sample weights with this half-life
        fdf = feature_df.copy()
        fdf['sample_weight'] = fdf['result_date'].apply(
            lambda d: 0.5 ** (max(0, (datetime.now() - d).days) / hl) if not pd.isna(d) else 0.5
        )

        cv = temporal_cv(fdf, target, half_life_days=hl)
        results.append({
            'half_life_months': round(months, 1),
            'half_life_days': hl,
            'overall_rmse': cv['overall_rmse'],
            'overall_mae': cv['overall_mae'],
            'n_folds': cv['n_folds'],
        })

    # Find optimal
    valid = [r for r in results if r['n_folds'] > 0]
    if valid:
        optimal = min(valid, key=lambda r: r['overall_rmse'])
        optimal_half_life = optimal['half_life_days']
    else:
        optimal_half_life = DEFAULT_HALF_LIFE_DAYS

    _log.info("\nHalf-life tuning results:")
    _log.info("%-20s %-12s %-12s %s", "Half-life", "RMSE", "MAE", "Folds")
    for r in results:
        _log.info("%-20s %-12.3f %-12.3f %d",
                  f"{r['half_life_months']:.1f} months", r['overall_rmse'], r['overall_mae'], r['n_folds'])
    _log.info("Optimal half-life: %d days (%.1f months)", optimal_half_life, optimal_half_life / 30.44)

    return {
        'summary_table': results,
        'optimal_half_life_days': optimal_half_life,
    }


# ---------------------------------------------------------------------------
# Model training
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_df(df: pd.DataFrame) -> str:
    return _sha256_bytes(df.to_csv(index=False).encode('utf-8'))


def train_and_save(
    feature_df: pd.DataFrame,
    target: pd.Series,
    event_type: str,
    half_life_days: int = DEFAULT_HALF_LIFE_DAYS,
) -> Path:
    """
    Train an XGBoost model and save with metadata.

    Trains three models:
        - median (reg:squarederror) for point prediction
        - q10 (quantile alpha=0.1) for lower bound
        - q90 (quantile alpha=0.9) for upper bound

    Also runs temporal CV to get RMSE/MAE for metadata.

    Args:
        feature_df: Feature matrix (includes 'result_date', 'sample_weight').
        target: Target times.
        event_type: 'SB', 'UH', or 'combined'.
        half_life_days: Decay half-life used for sample weights.

    Returns:
        Path to the saved model directory.
    """
    try:
        import xgboost as xgb
    except ImportError:
        raise ImportError("xgboost not installed. Run: pip install xgboost>=2.0")

    feature_cols = [c for c in feature_df.columns if c not in ('result_date', 'sample_weight')]
    X = feature_df[feature_cols]
    y = target
    weights = feature_df['sample_weight']

    n_rows = len(X)
    _log.info("Training %s model on %d rows (half-life=%d days)...", event_type, n_rows, half_life_days)

    # Run temporal CV first
    cv_results = temporal_cv(feature_df, target, half_life_days)

    # Train median model
    model_median = xgb.XGBRegressor(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        objective='reg:squarederror',
        tree_method='hist',
        random_state=42,
        verbosity=0,
    )
    model_median.fit(X, y, sample_weight=weights)

    # Train quantile models for prediction intervals
    try:
        model_q10 = xgb.XGBRegressor(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            objective='reg:quantileerror',
            quantile_alpha=0.1,
            tree_method='hist',
            random_state=42,
            verbosity=0,
        )
        model_q10.fit(X, y, sample_weight=weights)

        model_q90 = xgb.XGBRegressor(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            objective='reg:quantileerror',
            quantile_alpha=0.9,
            tree_method='hist',
            random_state=42,
            verbosity=0,
        )
        model_q90.fit(X, y, sample_weight=weights)
        has_quantiles = True
    except Exception as e:
        _log.warning("Quantile models failed (%s); prediction intervals unavailable.", e)
        model_q10 = model_q90 = None
        has_quantiles = False

    # Serialize median model to get version hash
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
        tmp_path = tmp.name
    model_median.save_model(tmp_path)
    with open(tmp_path, 'rb') as f:
        model_bytes = f.read()
    model_version = _sha256_bytes(model_bytes)
    os.unlink(tmp_path)

    # Dataset hash
    dataset_hash = _sha256_df(pd.concat([feature_df, target], axis=1))

    # Save to versioned directory
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    model_dir = MODELS_DIR / f"{event_type}_{timestamp}"
    model_dir.mkdir(parents=True, exist_ok=True)

    model_median.save_model(str(model_dir / "model.json"))
    if has_quantiles and model_q10 is not None:
        model_q10.save_model(str(model_dir / "model_q10.json"))
        model_q90.save_model(str(model_dir / "model_q90.json"))

    metadata = {
        'model_version': model_version,
        'trained_at': datetime.now().isoformat(),
        'dataset_hash': dataset_hash,
        'rmse': cv_results['overall_rmse'],
        'mae': cv_results['overall_mae'],
        'n_training_rows': n_rows,
        'event_type': event_type,
        'half_life_days': half_life_days,
        'has_quantile_models': has_quantiles,
        'feature_names': feature_cols,
        'cv_folds': cv_results['n_folds'],
        'cv_fold_results': cv_results['fold_results'],
    }

    with open(model_dir / "metadata.json", 'w') as f:
        json.dump(metadata, f, indent=2)

    _log.info("Saved %s model to %s (version %s...)", event_type, model_dir, model_version[:12])
    _log.info("  CV RMSE=%.3f, MAE=%.3f over %d folds", cv_results['overall_rmse'], cv_results['overall_mae'], cv_results['n_folds'])

    return model_dir


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Train STRATHMARK XGBoost prediction models."
    )
    parser.add_argument(
        '--legacy-import',
        metavar='PATH',
        help="Path to Excel workbook for bootstrapping (skips database).",
    )
    parser.add_argument(
        '--tune-halflife',
        action='store_true',
        help="Run half-life tuning and exit (does not save a model).",
    )
    parser.add_argument(
        '--half-life',
        type=int,
        default=DEFAULT_HALF_LIFE_DAYS,
        metavar='DAYS',
        help=f"Exponential decay half-life in days (default: {DEFAULT_HALF_LIFE_DAYS}).",
    )
    args = parser.parse_args()

    # Load data
    if args.legacy_import:
        results_df, wood_df = load_from_xlsx(args.legacy_import)
    else:
        results_df, wood_df = load_from_db()

    if results_df.empty:
        _log.error("No training data available. Exiting.")
        sys.exit(1)

    # Build features
    feature_df, target = build_feature_matrix(results_df, wood_df)

    if args.tune_halflife:
        tune_halflife(feature_df, target)
        return

    half_life = args.half_life

    # Split by event type
    sb_mask = feature_df.index.isin(
        target.index[feature_df.index.map(lambda i: i in target.index)]
    )
    # Get event encoding from feature matrix
    event_col = feature_df['event_encoded'] if 'event_encoded' in feature_df.columns else None

    if event_col is not None:
        sb_mask = event_col == 0
        uh_mask = event_col == 1

        n_sb = sb_mask.sum()
        n_uh = uh_mask.sum()
        n_total = len(feature_df)

        _log.info("Dataset split: %d SB, %d UH, %d total", n_sb, n_uh, n_total)

        if n_sb >= MIN_RECORDS_PER_EVENT:
            train_and_save(feature_df[sb_mask], target[sb_mask], 'SB', half_life)
        else:
            _log.warning("Insufficient SB records (%d < %d), skipping SB model.", n_sb, MIN_RECORDS_PER_EVENT)

        if n_uh >= MIN_RECORDS_PER_EVENT:
            train_and_save(feature_df[uh_mask], target[uh_mask], 'UH', half_life)
        else:
            _log.warning("Insufficient UH records (%d < %d), skipping UH model.", n_uh, MIN_RECORDS_PER_EVENT)

    # Always train combined model
    if n_total >= MIN_RECORDS_COMBINED:
        train_and_save(feature_df, target, 'combined', half_life)
    else:
        _log.warning("Insufficient total records (%d < %d), skipping combined model.", n_total, MIN_RECORDS_COMBINED)
        _log.error("No models trained. Add more data and retry.")
        sys.exit(1)

    _log.info("Training complete. Models saved to %s/", MODELS_DIR)


if __name__ == '__main__':
    main()
