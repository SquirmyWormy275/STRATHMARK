"""
Shared utilities for STRATHMARK modules.

Contains helpers that would otherwise be duplicated across variance.py,
wood.py, and fallback.py.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import pandas as pd


def standardize_results_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize column names for a results DataFrame.

    Lowercases all column names, renames common variants to canonical names,
    and coerces types for key columns.

    Returns a copy of the input with standardized column names.
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    rename_map = {
        'time': 'raw_time',
        'actualtime': 'raw_time',
        'actual_time': 'raw_time',
        'time (seconds)': 'raw_time',
        'time(seconds)': 'raw_time',
        'competitorname': 'competitor_name',
        'competitor name': 'competitor_name',
        'name': 'competitor_name',
        'competitorid': 'competitor_name',
        'competitor_id': 'competitor_name',
        'event_code': 'event',
        'eventcode': 'event',
        'diameter': 'size_mm',
        'diameter_mm': 'size_mm',
        'size': 'size_mm',
        'size (mm)': 'size_mm',
        'size(mm)': 'size_mm',
        'wood_species': 'species',
        'woodspecies': 'species',
        'species code': 'species',
        'speciescode': 'species',
        'date': 'result_date',
        'date (optional)': 'result_date',
        'result date': 'result_date',
    }
    df.rename(columns=rename_map, inplace=True)
    for col in ['raw_time', 'size_mm', 'quality']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    for col in ['competitor_name', 'event', 'species']:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
    if 'event' in df.columns:
        df['event'] = df['event'].str.upper()
    return df


def load_woodchopping_xlsx(path: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load a woodchopping Excel workbook and return (wood_df, results_df).

    Expected sheet names:
        'Wood'       -- species properties (Janka hardness, specific gravity, etc.)
        'Results'    -- historical competition results

    Both DataFrames are returned as-is; callers should pass results_df through
    standardize_results_columns() before use if column normalisation is needed.

    Args:
        path: File-system path to the .xlsx workbook.

    Returns:
        Tuple of (wood_df, results_df). Either may be an empty DataFrame if
        the corresponding sheet is absent or empty.

    Raises:
        FileNotFoundError: If the workbook does not exist at path.
    """
    xl = pd.ExcelFile(path)

    wood_df = pd.DataFrame()
    if 'Wood' in xl.sheet_names:
        wood_df = xl.parse('Wood')

    results_df = pd.DataFrame()
    if 'Results' in xl.sheet_names:
        results_df = xl.parse('Results')

    return wood_df, results_df


# ---------------------------------------------------------------------------
# Phase 3B: score_prediction_accuracy
# ---------------------------------------------------------------------------

def score_prediction_accuracy(events: List[Dict]) -> Dict:
    """
    Score historical prediction accuracy across one or more past events.

    Args:
        events: List of event dicts, each containing:
            'event_type'    (str)         -- e.g. 'SB' or 'UH'
            'species'       (str)         -- wood species
            'results'       (list[dict])  -- list of competitor result dicts, each with:
                'name'           (str)   -- competitor name
                'predicted_time' (float) -- time predicted before the event
                'actual_time'    (float) -- time actually recorded

    Returns:
        Dict with keys:
            overall_rmse        (float)  -- root mean squared error across all predictions
            overall_mae         (float)  -- mean absolute error across all predictions
            by_event_type       (dict)   -- {event_type: {rmse, mae, n}}
            by_species          (dict)   -- {species: {rmse, mae, n}}
            systematic_biases   (dict)   -- {event_type: mean_error}
                                           Positive = consistently over-predicts (too slow).
                                           Negative = consistently under-predicts (too fast).
    """
    all_errors: List[float] = []
    all_sq_errors: List[float] = []

    by_event: Dict[str, Dict] = {}
    by_species_data: Dict[str, Dict] = {}

    for event in events:
        event_type = str(event.get('event_type', 'UNKNOWN')).upper()
        species = str(event.get('species', 'UNKNOWN'))
        results = event.get('results', [])

        if event_type not in by_event:
            by_event[event_type] = {'errors': [], 'sq_errors': []}
        if species not in by_species_data:
            by_species_data[species] = {'errors': [], 'sq_errors': []}

        for r in results:
            try:
                pred = float(r['predicted_time'])
                actual = float(r['actual_time'])
            except (KeyError, TypeError, ValueError):
                continue

            err = pred - actual          # signed error (positive = over-predicted)
            sq_err = err ** 2

            all_errors.append(err)
            all_sq_errors.append(sq_err)
            by_event[event_type]['errors'].append(err)
            by_event[event_type]['sq_errors'].append(sq_err)
            by_species_data[species]['errors'].append(err)
            by_species_data[species]['sq_errors'].append(sq_err)

    def _stats(errors: List[float], sq_errors: List[float]) -> Dict:
        n = len(errors)
        if n == 0:
            return {'rmse': None, 'mae': None, 'n': 0}
        rmse = math.sqrt(sum(sq_errors) / n)
        mae = sum(abs(e) for e in errors) / n
        return {'rmse': round(rmse, 4), 'mae': round(mae, 4), 'n': n}

    overall = _stats(all_errors, all_sq_errors)

    by_event_type = {
        et: _stats(d['errors'], d['sq_errors'])
        for et, d in by_event.items()
    }
    by_species = {
        sp: _stats(d['errors'], d['sq_errors'])
        for sp, d in by_species_data.items()
    }
    systematic_biases = {
        et: round(sum(d['errors']) / len(d['errors']), 4) if d['errors'] else None
        for et, d in by_event.items()
    }

    return {
        'overall_rmse': overall['rmse'],
        'overall_mae': overall['mae'],
        'by_event_type': by_event_type,
        'by_species': by_species,
        'systematic_biases': systematic_biases,
    }
