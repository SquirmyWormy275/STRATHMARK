"""
Data Loader Utilities
=====================

Provides functions to load woodchopping data from the standard Excel workbook
format used in STRATHEX tournaments.

Public API
----------
    load_woodchopping_xlsx(path)          -> (wood_df, competitor_df, results_df)
    load_results_for_competitor(df, id)   -> pd.DataFrame
"""

import warnings
import pandas as pd


# ---------------------------------------------------------------------------
# Required columns per sheet
# ---------------------------------------------------------------------------

_WOOD_REQUIRED = {"species", "speciesID", "janka_hard", "spec_gravity"}
_COMPETITOR_REQUIRED = {"CompetitorID", "Name"}
_RESULTS_REQUIRED = {"CompetitorID", "Event", "Time (seconds)", "Size (mm)", "Species Code"}


# ---------------------------------------------------------------------------
# Internal sheet validators
# ---------------------------------------------------------------------------

def _validate_wood(df: pd.DataFrame) -> pd.DataFrame:
    unnamed = [c for c in df.columns if str(c).startswith("Unnamed")]
    if unnamed:
        raise ValueError(
            f"Wood sheet contains unnamed columns: {unnamed}. "
            "Remove or rename them before loading."
        )
    missing = _WOOD_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"Wood sheet is missing required columns: {missing}")
    return df


def _validate_competitor(df: pd.DataFrame) -> pd.DataFrame:
    unnamed = [c for c in df.columns if str(c).startswith("Unnamed")]
    if unnamed:
        raise ValueError(
            f"Competitor sheet contains unnamed columns: {unnamed}. "
            "Remove or rename them before loading."
        )
    missing = _COMPETITOR_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"Competitor sheet is missing required columns: {missing}")
    return df


def _validate_results(df: pd.DataFrame) -> pd.DataFrame:
    # Check required columns exist
    missing = _RESULTS_REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"Results sheet is missing required columns: {missing}")

    # Event column: must be uppercase — detect lowercase event codes
    event_col = df["Event"].dropna().astype(str)
    bad_events = event_col[event_col.str.strip().isin(["sb", "uh"])]
    if not bad_events.empty:
        raise ValueError(
            f"Results sheet Event column contains lowercase event codes "
            f"(found: {bad_events.unique().tolist()}). Event codes must be uppercase (e.g. 'SB', 'UH')."
        )

    # Date column: must be datetime/date — detect year-only integers
    if "Date" in df.columns:
        date_col = df["Date"].dropna()
        # If any value is a plain integer it is likely a year
        int_mask = date_col.apply(lambda v: isinstance(v, (int,)) or (
            isinstance(v, float) and v == int(v) and 1900 <= v <= 2100
        ))
        if int_mask.any():
            raise ValueError(
                "Results sheet Date column contains year-only integers. "
                "Provide full dates (e.g. 2024-01-15) instead."
            )
        # Attempt coercion and warn on unparseable values
        try:
            df = df.copy()
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        except Exception:
            pass

    # Drop rows missing any required field and warn
    required_cols = list(_RESULTS_REQUIRED)
    before = len(df)
    df = df.dropna(subset=required_cols)
    dropped = before - len(df)
    if dropped:
        warnings.warn(
            f"Results sheet: dropped {dropped} row(s) with missing values in "
            f"required columns {required_cols}.",
            UserWarning,
            stacklevel=4,
        )

    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_woodchopping_xlsx(
    path: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Read the standard woodchopping Excel workbook and return three DataFrames.

    Parameters
    ----------
    path : str
        Path to the .xlsx file.  Must contain sheets named 'Wood',
        'Competitor', and 'Results' (case-sensitive).

    Returns
    -------
    (wood_df, competitor_df, results_df) : tuple of pd.DataFrame

    Raises
    ------
    ValueError
        If any validation rule is violated (see module docstring).
    """
    xl = pd.ExcelFile(path, engine="openpyxl")

    required_sheets = {"Wood", "Competitor", "Results"}
    missing_sheets = required_sheets - set(xl.sheet_names)
    if missing_sheets:
        raise ValueError(
            f"Excel file is missing required sheets: {missing_sheets}. "
            f"Found: {xl.sheet_names}"
        )

    wood_df = pd.read_excel(xl, sheet_name="Wood")
    competitor_df = pd.read_excel(xl, sheet_name="Competitor")
    results_df = pd.read_excel(xl, sheet_name="Results")

    wood_df = _validate_wood(wood_df)
    competitor_df = _validate_competitor(competitor_df)
    results_df = _validate_results(results_df)

    return wood_df, competitor_df, results_df


def load_results_for_competitor(
    results_df: pd.DataFrame,
    competitor_id: str,
) -> pd.DataFrame:
    """
    Filter results_df to a single competitor, sorted by Date ascending.

    Parameters
    ----------
    results_df : pd.DataFrame
        DataFrame returned by load_woodchopping_xlsx (third element).
    competitor_id : str
        The CompetitorID to filter on.

    Returns
    -------
    pd.DataFrame
        Filtered and sorted subset.  Empty DataFrame if the competitor is not found.
    """
    mask = results_df["CompetitorID"].astype(str) == str(competitor_id)
    subset = results_df[mask].copy()

    if "Date" in subset.columns:
        subset = subset.sort_values("Date", ascending=True, na_position="last")

    return subset.reset_index(drop=True)
