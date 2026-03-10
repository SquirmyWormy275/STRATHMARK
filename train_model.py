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
    Build the XGBoost/LightGBM feature matrix from raw results.

    Features (27 total, matching MLConfig.FEATURE_NAMES):
        1.  comp_weighted_avg         - leave-one-out mean per competitor/event
        2.  comp_count                - result count for competitor/event
        3.  comp_std                  - std dev of competitor times
        4.  comp_best                 - all-time best for competitor/event
        5.  comp_recent               - most recent result time
        6.  comp_trend                - linear trend slope (sec/result)
        7.  comp_cross_event_avg      - avg time in OTHER event (SB<->UH)
        8.  days_since_last           - days between last two results
        9.  size_deviation            - size_mm minus competitor median diameter
        10. event_encoded             - 0=SB, 1=UH
        11. gender_encoded            - 0=F, 1=M
        12. janka_hard                - Janka hardness
        13. spec_gravity              - specific gravity
        14. crush_strength            - crush strength
        15. shear                     - shear strength
        16. MOR                       - modulus of rupture
        17. MOE                       - modulus of elasticity
        18. species_mult              - empirical species time multiplier
        19. size_mm                   - block diameter
        20. size_mm_sq                - size_mm ** 2
        21. log_size                  - log(size_mm)
        22. event_x_size              - event_encoded * size_mm
        23. species_mult_x_size       - species_mult * size_mm
        24. comp_avg_x_species        - comp_weighted_avg * species_mult
        25. comp_avg_x_size           - comp_weighted_avg * size_mm / 300.0
        26. month_sin                 - sin(2*pi*month/12)
        27. month_cos                 - cos(2*pi*month/12)

    Args:
        results_df: Standardized results DataFrame.
        wood_df: Wood species properties DataFrame.

    Returns:
        (feature_df, target_series) where target is raw_time.
    """
    from strathmark.utils import standardize_results_columns
    from strathmark.wood import get_species_properties, get_species_time_multiplier

    df = standardize_results_columns(results_df).copy()
    df = df.dropna(subset=['raw_time', 'event', 'competitor_name', 'size_mm'])
    df = df[df['raw_time'] > 0]

    # Normalize event codes
    df['event'] = df['event'].str.upper().str.strip()
    df = df[df['event'].isin(['SB', 'UH'])]

    if df.empty:
        raise ValueError("No valid training rows after filtering.")

    _log.info("Building features for %d rows...", len(df))

    # Parse dates
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

    df['janka_hard'] = _prop('janka_hardness', 1690.0) if 'species' in df.columns else 1690.0
    df['spec_gravity'] = _prop('specific_gravity', 0.34) if 'species' in df.columns else 0.34
    df['shear'] = _prop('shear_strength', 5.0) if 'species' in df.columns else 5.0
    df['crush_strength'] = _prop('crush_strength', 30.0) if 'species' in df.columns else 30.0
    df['MOR'] = _prop('mor', 50.0) if 'species' in df.columns else 50.0
    df['MOE'] = _prop('moe', 8.0) if 'species' in df.columns else 8.0

    # Drop helper column
    df = df.drop(columns=['_props'], errors='ignore')

    # --- Species time multiplier ---
    if 'species' in df.columns:
        df['species_mult'] = df['species'].apply(get_species_time_multiplier)
    else:
        df['species_mult'] = 1.0

    # --- Event encoding ---
    df['event_encoded'] = (df['event'] == 'UH').astype(int)

    # --- Gender encoding ---
    if 'gender' in df.columns:
        df['gender_encoded'] = df['gender'].map({'M': 1, 'F': 0, 'male': 1, 'female': 0}).fillna(0).astype(float)
    else:
        df['gender_encoded'] = 0.0

    # --- Per-competitor aggregates (leave-one-out to avoid data leakage) ---
    grp_event = df.groupby(['competitor_name', 'event'])['raw_time']
    df['_comp_count'] = grp_event.transform('count')
    df['_comp_sum'] = grp_event.transform('sum')
    # Leave-one-out mean: (sum - this_value) / (count - 1)
    df['comp_weighted_avg'] = (
        (df['_comp_sum'] - df['raw_time']) /
        (df['_comp_count'] - 1).clip(lower=1)
    )
    df['comp_count'] = df['_comp_count'].astype(float)

    # Competitor variance
    df['comp_std'] = df.groupby(['competitor_name', 'event'])['raw_time'].transform('std').fillna(3.0)

    # Competitor best
    df['comp_best'] = df.groupby(['competitor_name', 'event'])['raw_time'].transform('min')

    # Median diameter for competitor
    df['_comp_median_diam'] = df.groupby('competitor_name')['size_mm'].transform('median')
    df['size_deviation'] = df['size_mm'] - df['_comp_median_diam']

    # Trend slope (linear regression on last 5 results per competitor/event)
    def _trend_slope(group):
        if len(group) < 3:
            return pd.Series(0.0, index=group.index)
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

    df['comp_trend'] = (
        df.groupby(['competitor_name', 'event'], group_keys=False)
        .apply(_trend_slope)
        .fillna(0.0)
    )

    # comp_recent and days_since_last (most recent time and interval)
    def _recent_and_gap(group):
        g = group.copy()
        recent = pd.Series(g['raw_time'].mean(), index=g.index)
        gap = pd.Series(365.0, index=g.index)
        if 'result_date' in g.columns:
            g_sorted = g.dropna(subset=['result_date']).sort_values('result_date')
            if len(g_sorted) > 0:
                last_time = float(g_sorted.iloc[-1]['raw_time'])
                recent = pd.Series(last_time, index=g.index)
                if len(g_sorted) >= 2:
                    last_gap = float((g_sorted.iloc[-1]['result_date'] - g_sorted.iloc[-2]['result_date']).days)
                    last_gap = max(0.0, min(1000.0, last_gap))
                    gap = pd.Series(last_gap, index=g.index)
        return pd.DataFrame({'comp_recent': recent, 'days_since_last': gap})

    recent_gap = (
        df.groupby(['competitor_name', 'event'], group_keys=False)
        .apply(_recent_and_gap)
    )
    df['comp_recent'] = recent_gap['comp_recent']
    df['days_since_last'] = recent_gap['days_since_last']

    # Peer event avg (UH<->SB cross-event)
    peer_means = (
        df.groupby(['competitor_name', 'event'])['raw_time']
        .mean()
    )
    peer_map = {}
    for (name, evt), val in peer_means.items():
        opposite = 'SB' if evt == 'UH' else 'UH'
        peer_map[(name, opposite)] = val

    df['comp_cross_event_avg'] = df.apply(
        lambda r: peer_map.get((r['competitor_name'], r['event']),
                               r['comp_weighted_avg']),
        axis=1,
    )

    # Block size features
    df['size_mm_sq'] = df['size_mm'] ** 2
    df['log_size'] = np.log(df['size_mm'].clip(lower=1.0))

    # Interaction features
    df['event_x_size'] = df['event_encoded'] * df['size_mm']
    df['species_mult_x_size'] = df['species_mult'] * df['size_mm']
    df['comp_avg_x_species'] = df['comp_weighted_avg'] * df['species_mult']
    df['comp_avg_x_size'] = df['comp_weighted_avg'] * df['size_mm'] / 300.0

    # Seasonal features (from result_date)
    df['_month'] = df['result_date'].dt.month.fillna(6).astype(float)
    df['month_sin'] = np.sin(2 * np.pi * df['_month'] / 12)
    df['month_cos'] = np.cos(2 * np.pi * df['_month'] / 12)

    # --- Assemble feature matrix ---
    feature_cols = [
        'comp_weighted_avg',
        'comp_count',
        'comp_std',
        'comp_best',
        'comp_recent',
        'comp_trend',
        'comp_cross_event_avg',
        'days_since_last',
        'size_deviation',
        'event_encoded',
        'gender_encoded',
        'janka_hard',
        'spec_gravity',
        'crush_strength',
        'shear',
        'MOR',
        'MOE',
        'species_mult',
        'size_mm',
        'size_mm_sq',
        'log_size',
        'event_x_size',
        'species_mult_x_size',
        'comp_avg_x_species',
        'comp_avg_x_size',
        'month_sin',
        'month_cos',
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
        X_val = feature_df.loc[val_mask, feature_cols]
        y_val = target.loc[val_mask]

        if len(X_train) < 10 or len(X_val) < 3:
            val_start = val_end
            continue

        model = xgb.XGBRegressor(
            n_estimators=292,
            max_depth=4,
            learning_rate=0.0305,
            objective='reg:squarederror',
            tree_method='hist',
            subsample=0.643,
            colsample_bytree=0.508,
            min_child_weight=7,
            reg_alpha=0.261,
            reg_lambda=0.219,
            random_state=42,
            verbosity=0,
        )
        y_train_log = np.log(y_train.clip(lower=1.0))
        model.fit(X_train, y_train_log)
        preds = np.exp(model.predict(X_val))

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
    Train XGBoost and LightGBM models on log(time) target and save with metadata.

    Trains on np.log(target) and exponentiates predictions at inference time.
    Does NOT use sample weights (validated: weights hurt accuracy by ~0.37s MAE).

    Args:
        feature_df: Feature matrix (includes 'result_date' column).
        target: Target times (raw seconds).
        event_type: 'SB', 'UH', or 'combined'.
        half_life_days: Decay half-life (kept in metadata; not used for weighting).

    Returns:
        Path to the saved model directory.
    """
    try:
        import xgboost as xgb
    except ImportError:
        raise ImportError("xgboost not installed. Run: pip install xgboost>=2.0")

    feature_cols = [c for c in feature_df.columns if c not in ('result_date', 'sample_weight')]
    X = feature_df[feature_cols]
    y_raw = target
    y_log = np.log(y_raw)

    n_rows = len(X)
    _log.info("Training %s model on %d rows (log target, no sample weights)...", event_type, n_rows)

    # Run temporal CV on log-space predictions
    cv_results = temporal_cv(feature_df, target, half_life_days)

    # Train XGBoost on log(time) — NO sample weights
    model_xgb = xgb.XGBRegressor(
        n_estimators=292,
        max_depth=4,
        learning_rate=0.0305,
        objective='reg:squarederror',
        tree_method='hist',
        subsample=0.643,
        colsample_bytree=0.508,
        min_child_weight=7,
        reg_alpha=0.261,
        reg_lambda=0.219,
        random_state=42,
        verbosity=0,
    )
    model_xgb.fit(X, y_log)

    # In-sample MAE (exponentiated)
    xgb_preds_raw = np.exp(model_xgb.predict(X))
    xgb_mae = float(np.mean(np.abs(xgb_preds_raw - y_raw.values)))
    _log.info("  XGBoost in-sample MAE: %.3fs", xgb_mae)

    # Train LightGBM on log(time)
    lgb_model = None
    lgb_mae = None
    try:
        import lightgbm as lgb
        lgb_model = lgb.LGBMRegressor(
            n_estimators=222,
            max_depth=4,
            learning_rate=0.0303,
            subsample=0.938,
            colsample_bytree=0.626,
            min_child_samples=20,
            num_leaves=23,
            reg_alpha=0.079,
            reg_lambda=0.101,
            random_state=42,
            verbose=-1,
        )
        lgb_model.fit(X, y_log)
        lgb_preds_raw = np.exp(lgb_model.predict(X))
        lgb_mae = float(np.mean(np.abs(lgb_preds_raw - y_raw.values)))
        _log.info("  LightGBM in-sample MAE: %.3fs", lgb_mae)
    except ImportError:
        _log.warning("LightGBM not installed; saving XGBoost-only model.")

    # Serialize XGBoost model to get version hash
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
        tmp_path = tmp.name
    model_xgb.save_model(tmp_path)
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

    model_xgb.save_model(str(model_dir / "model.json"))
    if lgb_model is not None:
        lgb_model.booster_.save_model(str(model_dir / "model_lgb.txt"))

    metadata = {
        'model_version': model_version,
        'trained_at': datetime.now().isoformat(),
        'dataset_hash': dataset_hash,
        'rmse': cv_results['overall_rmse'],
        'mae': cv_results['overall_mae'],
        'n_training_rows': n_rows,
        'event_type': event_type,
        'half_life_days': half_life_days,
        'log_target': True,
        'has_lgb': lgb_model is not None,
        'xgb_insample_mae': xgb_mae,
        'lgb_insample_mae': lgb_mae,
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
