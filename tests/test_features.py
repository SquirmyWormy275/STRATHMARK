"""Tests for the Prediction V2 prior-only evidence boundary."""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from strathmark.calculator import HandicapCalculator
from strathmark.features import (
    MISSING_CATEGORY,
    SPECIES_PROPERTY_FIELDS,
    build_prior_evidence,
    normalize_prediction_as_of,
)
from strathmark.predictor import CompetitorRecord, WoodProfile


def _valid_row(**updates):
    row = {
        "CompetitorID": "C-001",
        "Name": "Display Name",
        "Event": "SB",
        "Time (seconds)": 42.5,
        "Date": "2026-08-10",
        "Size (mm)": 300,
        "Species Code": "S01",
        "Gender": "M",
    }
    row.update(updates)
    return row


def _wood_table():
    return pd.DataFrame(
        [
            {
                "speciesID": "S01",
                "species": "Eastern white pine",
                "janka_hard": 1690,
                "spec_gravity": 0.34,
                "shear": 900,
                "crush_strength": 4300,
                "MOR": 8500,
                "MOE": 1_200_000,
            },
            {
                "speciesID": "S02",
                "species": "Yellow-poplar",
                "janka_hard": 2400,
                "spec_gravity": 0.42,
                "shear": 1100,
                "crush_strength": 5100,
                "MOR": 10_000,
                "MOE": 1_500_000,
            },
        ]
    )


def test_aliases_preserve_stable_identity_and_drop_display_name():
    evidence = build_prior_evidence(
        pd.DataFrame([_valid_row()]), date(2026, 8, 11), wood_df=_wood_table()
    )

    assert evidence.rows.loc[0, "competitor_id"] == "C-001"
    assert "competitor_name" not in evidence.rows.columns
    assert "name" not in evidence.rows.columns
    assert evidence.diagnostics.included_rows == 1


def test_exclusive_cutoff_reports_same_day_future_invalid_and_undated():
    rows = [
        _valid_row(CompetitorID="included", Date="2026-08-10"),
        _valid_row(CompetitorID="same", Date="2026-08-11"),
        _valid_row(CompetitorID="future", Date="2026-08-12"),
        _valid_row(CompetitorID="invalid", Date="not-a-date"),
        _valid_row(CompetitorID="undated", Date=None),
    ]

    evidence = build_prior_evidence(pd.DataFrame(rows), "2026-08-11")

    assert evidence.rows["competitor_id"].tolist() == ["included"]
    assert evidence.diagnostics.excluded_by_reason == {
        "same_day": 1,
        "future": 1,
        "invalid_date": 1,
        "undated": 1,
    }


def test_invalid_times_are_excluded_without_treating_large_time_as_dnf():
    rows = [
        _valid_row(CompetitorID="positive", **{"Time (seconds)": 999.0}),
        _valid_row(CompetitorID="zero", **{"Time (seconds)": 0.0}),
        _valid_row(CompetitorID="negative", **{"Time (seconds)": -1.0}),
        _valid_row(CompetitorID="nan", **{"Time (seconds)": np.nan}),
        _valid_row(CompetitorID="inf", **{"Time (seconds)": np.inf}),
    ]

    evidence = build_prior_evidence(pd.DataFrame(rows), date(2026, 8, 11))

    assert evidence.rows["competitor_id"].tolist() == ["positive"]
    assert evidence.rows.loc[0, "time_seconds"] == 999.0
    assert evidence.diagnostics.excluded_by_reason == {"invalid_time": 4}


def test_inactive_fields_cannot_change_canonical_evidence():
    base = _valid_row()
    mutated = _valid_row(
        Division="Open",
        quality=10,
        heat_id="final",
        field_strength=999,
        venue="Somewhere",
        lane=7,
        run_order=22,
        notes="DNF penalty timeout",
        weather="rain",
        equipment="new axe",
    )

    left = build_prior_evidence(pd.DataFrame([base]), date(2026, 8, 11), wood_df=_wood_table())
    right = build_prior_evidence(pd.DataFrame([mutated]), date(2026, 8, 11), wood_df=_wood_table())

    pd.testing.assert_frame_equal(left.rows, right.rows)


def test_unknown_categories_are_explicit_and_use_pooled_species_properties():
    row = _valid_row(**{"Species Code": "UNKNOWN", "Gender": "not-recorded"})

    evidence = build_prior_evidence(pd.DataFrame([row]), date(2026, 8, 11), wood_df=_wood_table())

    result = evidence.rows.iloc[0]
    assert result["gender"] == MISSING_CATEGORY
    assert bool(result["species_missing"])
    for prop in SPECIES_PROPERTY_FIELDS:
        assert np.isfinite(result[prop])


def test_prediction_cutoff_converts_timezone_aware_datetime_to_utc_date():
    cutoff = normalize_prediction_as_of(datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc))
    assert cutoff == date(2026, 8, 10)


def test_duplicate_name_keyed_override_is_rejected_as_ambiguous():
    calc = HandicapCalculator()
    records = [
        CompetitorRecord("Alex", competitor_id="C-1"),
        CompetitorRecord("Alex", competitor_id="C-2"),
    ]

    with pytest.raises(ValueError, match="ambiguous.*Alex"):
        calc.calculate(
            records,
            WoodProfile("Pine", 300, 5),
            "SB",
            manual_overrides={"Alex": 40.0},
        )
