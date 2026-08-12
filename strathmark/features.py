"""Causal, prior-only evidence preparation for Prediction Engine V2.

This module is the single boundary between raw historical data and numeric V2
model inputs.  It deliberately ignores every field outside the documented
allowlist: accepting a legacy column is not permission to use it as evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd

MISSING_CATEGORY = "__MISSING__"
CANONICALIZATION_VERSION = "prediction-v2-evidence-v1"

ACTIVE_RESULT_FIELDS = (
    "competitor_id",
    "event",
    "time_seconds",
    "result_date",
    "diameter_mm",
    "species",
    "gender",
)
SPECIES_PROPERTY_FIELDS = (
    "janka_hardness",
    "specific_gravity",
    "crush_strength",
    "shear_strength",
    "modulus_of_rupture",
    "modulus_of_elasticity",
)
MODEL_EVIDENCE_FIELDS = ACTIVE_RESULT_FIELDS + SPECIES_PROPERTY_FIELDS + ("species_missing",)

_RESULT_ALIASES: Mapping[str, tuple[str, ...]] = {
    "competitor_id": ("competitor_id", "competitorid"),
    "event": ("event", "event_code", "eventcode"),
    "time_seconds": (
        "time_seconds",
        "time (seconds)",
        "time(seconds)",
        "actual_time",
        "actualtime",
        "raw_time",
        "time",
    ),
    "result_date": ("result_date", "result date", "date", "date (optional)"),
    "diameter_mm": (
        "diameter_mm",
        "diameter",
        "size_mm",
        "size",
        "size (mm)",
        "size(mm)",
    ),
    "species": (
        "species",
        "species_code",
        "species code",
        "speciescode",
        "wood_species",
        "woodspecies",
    ),
    "gender": ("gender", "sex"),
}

_PROPERTY_ALIASES: Mapping[str, tuple[str, ...]] = {
    "species": ("speciesid", "species_id", "species code", "species_code", "species"),
    "janka_hardness": ("janka_hardness", "janka_hard", "janka"),
    "specific_gravity": ("specific_gravity", "spec_gravity"),
    "crush_strength": ("crush_strength", "crush"),
    "shear_strength": ("shear_strength", "shear"),
    "modulus_of_rupture": ("modulus_of_rupture", "mor"),
    "modulus_of_elasticity": ("modulus_of_elasticity", "moe"),
}

_DEFAULT_SPECIES_PROPERTIES: Mapping[str, float] = {
    "janka_hardness": 1690.0,
    "specific_gravity": 0.34,
    "crush_strength": 4000.0,
    "shear_strength": 1000.0,
    "modulus_of_rupture": 8000.0,
    "modulus_of_elasticity": 1_000_000.0,
}


@dataclass(frozen=True)
class ExclusionDiagnostics:
    """Counts describing exactly which raw rows entered an evidence set."""

    total_rows: int
    included_rows: int
    excluded_by_reason: Mapping[str, int] = field(default_factory=dict)

    @property
    def excluded_rows(self) -> int:
        return self.total_rows - self.included_rows


@dataclass(frozen=True)
class PriorEvidence:
    """Canonical model evidence and its causal-boundary diagnostics."""

    rows: pd.DataFrame
    prediction_as_of: date
    diagnostics: ExclusionDiagnostics
    canonicalization_version: str = CANONICALIZATION_VERSION


def normalize_prediction_as_of(value: Optional[date | datetime | str]) -> date:
    """Return the exclusive UTC date cutoff used by one prediction request.

    A missing value resolves once to today's UTC date. Naive datetimes are
    interpreted as UTC; aware datetimes are converted to UTC before taking the
    date. Invalid values fail closed instead of silently using the current date.
    """

    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        timestamp = value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("prediction_as_of must be a valid ISO date")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return date.fromisoformat(text)
            except ValueError as exc:
                raise ValueError("prediction_as_of must be a valid ISO date") from exc
        return normalize_prediction_as_of(parsed)
    raise TypeError("prediction_as_of must be a date, datetime, ISO date string, or None")


def build_prior_evidence(
    results_df: Optional[pd.DataFrame],
    prediction_as_of: Optional[date | datetime | str] = None,
    *,
    wood_df: Optional[pd.DataFrame] = None,
) -> PriorEvidence:
    """Build a deterministic V2 evidence frame using only strictly earlier rows.

    Each excluded row receives one primary reason.  The order is intentional:
    identity/event/time validity is checked before date and diameter validity.
    Notes and context labels are never inspected, so values such as ``DNF`` or
    ``penalty`` cannot silently reclassify an otherwise valid measured time.
    """

    cutoff = normalize_prediction_as_of(prediction_as_of)
    raw = pd.DataFrame() if results_df is None else results_df.copy()
    canonical = _coalesce_columns(raw, _RESULT_ALIASES)
    property_lookup, pooled = _species_property_lookup(wood_df)
    accepted: list[dict[str, Any]] = []
    exclusions: dict[str, int] = {}

    def exclude(reason: str) -> None:
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for _, row in canonical.iterrows():
        competitor_id = _clean_identifier(row.get("competitor_id"))
        if competitor_id is None:
            exclude("missing_competitor_id")
            continue

        event = _clean_category(row.get("event"), uppercase=True)
        if event not in {"SB", "UH"}:
            exclude("invalid_event")
            continue

        time_seconds = _finite_float(row.get("time_seconds"))
        if time_seconds is None or time_seconds <= 0:
            exclude("invalid_time")
            continue

        raw_date = row.get("result_date")
        if _is_missing(raw_date):
            exclude("undated")
            continue
        result_date = _parse_result_date(raw_date)
        if result_date is None:
            exclude("invalid_date")
            continue
        if result_date == cutoff:
            exclude("same_day")
            continue
        if result_date > cutoff:
            exclude("future")
            continue

        diameter_mm = _finite_float(row.get("diameter_mm"))
        if diameter_mm is None or diameter_mm <= 0:
            exclude("invalid_diameter")
            continue

        species = _clean_category(row.get("species"), uppercase=True)
        if species == MISSING_CATEGORY:
            properties = pooled
            species_missing = True
        else:
            properties = property_lookup.get(species, pooled)
            species_missing = species not in property_lookup

        gender = _normalize_gender(row.get("gender"))
        accepted.append(
            {
                "competitor_id": competitor_id,
                "event": event,
                "time_seconds": time_seconds,
                "result_date": result_date,
                "diameter_mm": diameter_mm,
                "species": species,
                "gender": gender,
                **properties,
                "species_missing": species_missing,
            }
        )

    rows = pd.DataFrame(accepted, columns=MODEL_EVIDENCE_FIELDS)
    diagnostics = ExclusionDiagnostics(
        total_rows=len(raw),
        included_rows=len(rows),
        excluded_by_reason=dict(exclusions),
    )
    return PriorEvidence(rows=rows, prediction_as_of=cutoff, diagnostics=diagnostics)


def resolve_species_properties(
    species: Any,
    wood_df: Optional[pd.DataFrame] = None,
) -> tuple[dict[str, float], bool]:
    """Resolve one species without assigning a fabricated known-species identity.

    Known species receive their canonical physical properties. Missing or unknown
    species receive the column-wise pooled medians and are explicitly flagged so
    the prediction engine can widen uncertainty and report extrapolation.
    """

    lookup, pooled = _species_property_lookup(wood_df)
    key = _clean_category(species, uppercase=True)
    if key == MISSING_CATEGORY or key not in lookup:
        return dict(pooled), True
    return dict(lookup[key]), False


def _normal_name(value: Any) -> str:
    return str(value).strip().lower()


def _coalesce_columns(frame: pd.DataFrame, aliases: Mapping[str, tuple[str, ...]]) -> pd.DataFrame:
    normalized: dict[str, list[Any]] = {}
    for column in frame.columns:
        normalized.setdefault(_normal_name(column), []).append(column)

    result = pd.DataFrame(index=frame.index)
    for canonical, candidates in aliases.items():
        series: Optional[pd.Series] = None
        for candidate in candidates:
            for original in normalized.get(candidate, []):
                values = frame[original]
                series = values.copy() if series is None else series.combine_first(values)
        result[canonical] = None if series is None else series
    return result


def _species_property_lookup(
    wood_df: Optional[pd.DataFrame],
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    if wood_df is None or wood_df.empty:
        defaults = dict(_DEFAULT_SPECIES_PROPERTIES)
        return {}, defaults

    canonical = _coalesce_columns(wood_df.copy(), _PROPERTY_ALIASES)
    species_keys = canonical["species"].map(lambda value: _clean_category(value, uppercase=True))
    usable = canonical.assign(species=species_keys)
    usable = usable[usable["species"] != MISSING_CATEGORY].copy()
    if usable["species"].duplicated().any():
        duplicates = sorted(usable.loc[usable["species"].duplicated(False), "species"].unique())
        raise ValueError(f"wood species properties must be one-to-one; duplicates: {duplicates}")

    pooled: dict[str, float] = {}
    for prop in SPECIES_PROPERTY_FIELDS:
        numeric = pd.to_numeric(usable[prop], errors="coerce")
        finite = numeric[np.isfinite(numeric)]
        pooled[prop] = (
            float(finite.median()) if not finite.empty else _DEFAULT_SPECIES_PROPERTIES[prop]
        )

    lookup: dict[str, dict[str, float]] = {}
    for _, row in usable.iterrows():
        values = {}
        for prop in SPECIES_PROPERTY_FIELDS:
            value = _finite_float(row.get(prop))
            values[prop] = pooled[prop] if value is None else value
        lookup[str(row["species"])] = values
    return lookup, pooled


def _clean_identifier(value: Any) -> Optional[str]:
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _clean_category(value: Any, *, uppercase: bool = False) -> str:
    if _is_missing(value):
        return MISSING_CATEGORY
    text = str(value).strip()
    if not text:
        return MISSING_CATEGORY
    return text.upper() if uppercase else text


def _normalize_gender(value: Any) -> str:
    gender = _clean_category(value, uppercase=True)
    return gender if gender in {"M", "F"} else MISSING_CATEGORY


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _parse_result_date(value: Any) -> Optional[date]:
    if isinstance(value, bool) or isinstance(value, (int, float, np.number)):
        return None
    if isinstance(value, datetime):
        timestamp = value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    try:
        timestamp = pd.to_datetime(value, errors="raise", utc=True)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.date()


__all__ = [
    "ACTIVE_RESULT_FIELDS",
    "CANONICALIZATION_VERSION",
    "ExclusionDiagnostics",
    "MISSING_CATEGORY",
    "MODEL_EVIDENCE_FIELDS",
    "PriorEvidence",
    "SPECIES_PROPERTY_FIELDS",
    "build_prior_evidence",
    "normalize_prediction_as_of",
]
