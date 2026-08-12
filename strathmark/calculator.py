"""
Handicap Mark Calculator
========================

Core handicap mark computation for woodchopping competitions.

This module contains HandicapCalculator, the primary public class for computing
association-agnostic handicap marks. It snapshots Prediction Engine V2 once per
field, optimizes the joint marks, enforces floor/ceiling constraints, and
produces a ranked start sheet.

Key constraints (must never be violated):
    - Mark floor:   3 seconds (front marker minimum)
    - Mark ceiling: 183 seconds system-wide (180s time limit + 3s minimum)
                    Individual event configs may set a lower ceiling.
    - Gap logic:    slowest predicted time -> Mark 3;
                    each full second faster -> +1 mark (ceiling arithmetic).
    - Rounding:     marks always rounded UP (ceiling, not nearest).

Source references (STRATHEX):
    woodchopping/handicaps/calculator.py  -> calculate_ai_enhanced_handicaps()
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from strathmark.config import sim_config
from strathmark.mark_optimizer import legacy_rounded_gap_marks, optimize_joint_marks
from strathmark.prediction_v2 import ForecastInterval, PredictiveDistribution
from strathmark.predictor import (
    CompetitorRecord,
    PredictionContext,
    PredictionEngineProvider,
    PredictionInterval,
    PredictionResult,
    WoodProfile,
    get_best_prediction,
    get_prediction_provider,
)

_log = logging.getLogger(__name__)


def _is_positive_finite(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def _optimizer_distribution(
    prediction: PredictionResult,
    performance_std_dev: float,
) -> PredictiveDistribution:
    """Rebuild the immutable posterior used by the joint mark optimizer.

    V2 model results carry their exact log-location and log-scale. Manual and
    static fallback results have no model posterior, so their documented
    interval is converted to a conservative log-scale; a performance-variance
    conversion is the final compatibility fallback.
    """

    median = float(prediction.value)
    metadata = prediction.metadata
    try:
        location = float(metadata["posterior_log_location"])
        log_scale = float(metadata["posterior_log_scale"])
        if not math.isfinite(location) or not math.isfinite(log_scale) or log_scale <= 0:
            raise ValueError("invalid exact posterior parameters")
    except (KeyError, TypeError, ValueError, OverflowError):
        location = math.log(median)
        interval = prediction.interval
        if interval is not None:
            quantile = NormalDist().inv_cdf(0.5 + interval.nominal_coverage / 2.0)
            radius = max(
                location - math.log(interval.lower),
                math.log(interval.upper) - location,
            )
            log_scale = max(radius / quantile, 1e-6)
        else:
            # Manual overrides intentionally have no model interval and no
            # shared model latent. Use their labeled performance variability.
            ratio = max(float(performance_std_dev), 1e-6) / median
            log_scale = max(math.sqrt(math.log1p(ratio * ratio)), 1e-6)

    raw_shared_scale = metadata.get("shared_log_scale", 0.0)
    try:
        shared_scale = float(raw_shared_scale)
    except (TypeError, ValueError, OverflowError):
        shared_scale = 0.0
    if not math.isfinite(shared_scale) or prediction.method in {"manual", "panel"}:
        shared_scale = 0.0
    shared_scale = max(0.0, min(shared_scale, log_scale))

    interval = prediction.interval
    forecast = (
        ForecastInterval(
            lower=interval.lower,
            upper=interval.upper,
            nominal_coverage=interval.nominal_coverage,
            calibration_state=interval.calibration_state,
            scope=interval.scope,
        )
        if interval is not None
        else ForecastInterval(
            lower=math.exp(location - 1.6448536269514722 * log_scale),
            upper=math.exp(location + 1.6448536269514722 * log_scale),
            calibration_state="manual_event_prior",
            scope="analytic",
        )
    )
    return PredictiveDistribution(
        median=median,
        log_location=location,
        log_scale=log_scale,
        interval=forecast,
        source=str(metadata.get("source", prediction.method)),
        history_count=int(metadata.get("history_count", 0)),
        effective_history_weight=float(metadata.get("effective_history_weight", 0.0)),
        warnings=tuple(prediction.warnings),
        degraded=prediction.degraded,
        model_version=prediction.model_version or "",
        calibration_version=prediction.calibration_version or "uncalibrated",
        metadata={"shared_log_scale": shared_scale},
    )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class MarkResult:
    """Handicap mark result for a single competitor."""

    name: str
    """Competitor display name."""

    mark: int
    """Assigned handicap mark in seconds. Always in [3, ceiling]."""

    predicted_time: float
    """Best predicted time used to derive the mark (seconds)."""

    method_used: str
    """Prediction method selected: 'manual', 'llm', 'ml', 'panel', or 'baseline'."""

    confidence: str
    """Confidence level: 'VERY HIGH', 'HIGH', 'MEDIUM', 'LOW', or 'VERY LOW'."""

    explanation: str
    """Human-readable explanation of why this prediction was selected."""

    std_dev: float = 3.0
    """Per-competitor performance std-dev in seconds (used in Monte Carlo simulation).
    Computed directly from competitor event history (3+ results -> clamped sample std;
    <3 results -> 3.0 flat). Independent of which cascade level won the prediction.
    Clamped to [1.5, 6.0]. Default 3.0 (PERFORMANCE_VARIANCE_SECONDS)."""

    competitor_id: Optional[str] = None
    interval: Optional[PredictionInterval] = None
    engine_version: Optional[str] = None
    model_version: Optional[str] = None
    calibration_version: Optional[str] = None
    evidence_cutoff: Optional[date] = None
    optimizer: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    prediction_id: Optional[str] = None
    ledger_recorded: Optional[bool] = None
    degraded: bool = False
    optimizer_metadata: Dict[str, object] = field(default_factory=dict)
    ledger_status: Optional[str] = None

    def to_simulation_dict(self) -> dict:
        """Return a dict suitable for passing to run_monte_carlo_simulation().

        Includes 'std_dev' so per-competitor variance reaches the MC loop.
        The key 'std_dev' is read by _get_competitor_variance_seconds() in variance.py.
        """
        return {
            "name": self.name,
            "mark": self.mark,
            "predicted_time": self.predicted_time,
            "std_dev": self.std_dev,
        }


@dataclass
class StartSheet:
    """
    Ordered start sheet for a single heat/round.

    Competitors are sorted from front marker (smallest mark, starts first)
    to back marker (largest mark, starts last). In a perfectly handicapped
    field every competitor should finish simultaneously.
    """

    event_name: str
    event_code: str
    species: str
    diameter_mm: float
    quality: int
    entries: List[MarkResult] = field(default_factory=list)

    def render(self) -> str:
        """
        Return a plain-text start sheet suitable for printing.

        Format follows the 70-character-wide AAA convention used in STRATHEX.
        No ANSI codes, no emojis.

        Returns:
            Formatted multi-line string. Lines are at most 70 characters wide.
        """
        lines = []

        # Top border (70 chars total: 1 + 68 + 1)
        lines.append("+" + "=" * 68 + "+")

        # Title
        title = f"START SHEET -- {self.event_name}".center(68)
        lines.append("|" + title + "|")

        # Wood info
        wood_line = (f"{self.species}  {self.diameter_mm:.0f}mm  Quality {self.quality}/10").center(
            68
        )
        lines.append("|" + wood_line + "|")

        lines.append("|" + "-" * 68 + "|")

        # Column header
        # Format: MARK(6) | NAME(30) | PREDICTED(12) | METHOD(12) | CONFIDENCE(8)
        header = f"{'MARK':<6}  {'COMPETITOR':<30}  {'PRED(s)':<9}  {'METHOD':<10}  {'CONF':<6}"
        lines.append("|" + header[:68].center(68) + "|")
        lines.append("|" + "-" * 68 + "|")

        # Entries: sorted by mark ascending (front marker first = smallest mark)
        sorted_entries = sorted(self.entries, key=lambda r: r.mark)
        for entry in sorted_entries:
            row = (
                f"{entry.mark:<6}  "
                f"{entry.name[:30]:<30}  "
                f"{entry.predicted_time:>7.2f}s  "
                f"{entry.method_used[:10]:<10}  "
                f"{entry.confidence[:6]:<6}"
            )
            lines.append("|" + row[:68] + "|")

        lines.append("|" + "-" * 68 + "|")

        # Legend
        legend = "Front marker (lowest mark) starts first.".center(68)
        lines.append("|" + legend + "|")

        # Bottom border
        lines.append("+" + "=" * 68 + "+")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main calculator class
# ---------------------------------------------------------------------------


class HandicapCalculator:
    """
    Compute AAA-compliant handicap marks for a field of competitors.

    Usage
    -----
        calc = HandicapCalculator()
        results = calc.calculate(
            competitors=[record1, record2, ...],
            wood=WoodProfile(species="Pine", diameter_mm=300, quality=5),
            event_code="SB",
        )
        sheet = calc.build_start_sheet(results, event_name="225 SB", ...)
        print(sheet.render())

    Prediction selection:
        1. Manual override (explicit time supplied by operator)
        2. The immutable V2 core/residual/calibration bundle
        3. The explicit deterministic legacy-baseline rollback on degradation

    Unsupported context such as same-tournament results, division, wood quality,
    venue, and lane remains accepted for compatibility but is a numeric no-op.

    Design invariants:
        - Mark floor = 3 (enforced after all other logic)
        - Mark ceiling = 183 system-wide (enforced after all other logic)
        - Variance in simulation = absolute +/-3 seconds (never proportional)
    """

    MARK_FLOOR: int = 3
    MARK_CEILING: int = 183  # 180s time limit + 3s minimum mark

    def __init__(
        self,
        event_ceiling: Optional[int] = None,
        ollama_url: str = "http://localhost:11434",
        wood_df: Optional[pd.DataFrame] = None,
        results_df: Optional[pd.DataFrame] = None,
        prediction_provider: Optional[PredictionEngineProvider] = None,
        ledger_sink: Optional[Any] = None,
        ledger_caller_id: str = "python",
    ) -> None:
        """
        Args:
            event_ceiling: Optional lower ceiling for this event (seconds).
                           Must be > MARK_FLOOR. If None, system default of 183 is used.
            ollama_url: Deprecated compatibility input. Numeric LLM prediction is disabled.
            wood_df: Optional species properties DataFrame (Janka hardness, etc.).
                     When provided, passed to get_best_prediction() on every call.
            results_df: Legacy compatibility data. V2 never trains on the request path;
                        prediction state comes from the provider's immutable bundle.
        """
        if event_ceiling is not None:
            if event_ceiling <= self.MARK_FLOOR:
                raise ValueError(
                    f"event_ceiling ({event_ceiling}) must be greater than "
                    f"MARK_FLOOR ({self.MARK_FLOOR})"
                )
            if event_ceiling > self.MARK_CEILING:
                raise ValueError(
                    f"event_ceiling ({event_ceiling}) must be <= system ceiling "
                    f"({self.MARK_CEILING})"
                )
            self.effective_ceiling: int = event_ceiling
        else:
            self.effective_ceiling = self.MARK_CEILING

        del ollama_url
        self.wood_df: Optional[pd.DataFrame] = wood_df
        self.results_df: Optional[pd.DataFrame] = results_df
        self._prediction_provider = prediction_provider or get_prediction_provider()
        self._ledger_sink = ledger_sink
        self._ledger_caller_id = str(ledger_caller_id or "python").strip()

    @classmethod
    def from_db(
        cls,
        competitor_ids: Optional[List[str]] = None,
        wood_df: Optional[pd.DataFrame] = None,
        **kwargs,
    ) -> "HandicapCalculator":
        """
        Construct a HandicapCalculator pre-loaded with data from the global database.

        Calls pull_results() internally to fetch legacy historical results used
        only by compatibility surfaces. V2 model
        state is loaded from the configured immutable prediction bundle.

        Args:
            competitor_ids: Optional list of competitor IDs to filter results by.
                            If None, all results in the database are fetched.
            wood_df: Species properties DataFrame (Janka hardness, etc.).
                     Required — load it from Excel using load_woodchopping_xlsx()
                     or supply a DataFrame directly.
            **kwargs: Forwarded to HandicapCalculator.__init__() (e.g. event_ceiling).

        Returns:
            HandicapCalculator with results_df populated from the database,
            ready to call calculate() on.

        Raises:
            ValueError: If wood_df is None.

        Example:
            wood_df, _ = load_woodchopping_xlsx('woodchopping_clean.xlsx')
            calc = HandicapCalculator.from_db(wood_df=wood_df)
            sheet = calc.calculate(competitors, wood_profile, event_code)
        """
        if wood_df is None:
            raise ValueError(
                "wood_df is required — load it from Excel using "
                "load_woodchopping_xlsx() or supply a DataFrame directly."
            )

        from strathmark.db import pull_results

        results_df = pull_results(competitor_ids=competitor_ids)
        return cls(wood_df=wood_df, results_df=results_df, **kwargs)

    @classmethod
    def from_xlsx(cls, path: str, **kwargs) -> "HandicapCalculator":
        """
        Construct a HandicapCalculator pre-loaded with data from an Excel workbook.

        Calls load_woodchopping_xlsx(path) to read the 'Wood' and 'Results'
        sheets, then returns a fully initialized instance with wood_df and
        results_df already set. V2 does not train during calculation.

        Args:
            path: Path to the .xlsx workbook (e.g. 'woodchopping_clean.xlsx').
            **kwargs: Forwarded to HandicapCalculator.__init__() (e.g. event_ceiling).

        Returns:
            HandicapCalculator with wood_df and results_df populated.

        Example:
            calc = HandicapCalculator.from_xlsx('woodchopping_clean.xlsx')
            sheet = calc.calculate(competitors, wood_profile, event_code)
        """
        from strathmark.utils import load_woodchopping_xlsx

        wood_df, results_df = load_woodchopping_xlsx(path)
        return cls(wood_df=wood_df, results_df=results_df, **kwargs)

    def calculate(
        self,
        competitors: Sequence[CompetitorRecord],
        wood: WoodProfile,
        event_code: str,
        tournament_results: Optional[Dict[str, float]] = None,
        manual_overrides: Optional[Dict[str, float]] = None,
        context: Optional[PredictionContext] = None,
    ) -> List[MarkResult]:
        """
        Compute handicap marks for all competitors in a heat/round.

        Args:
            competitors: Ordered sequence of CompetitorRecord objects.
                         Each record includes historical times and metadata.
            wood: Wood characteristics for this event (species, diameter, quality).
            event_code: 'SB' (Standing Block) or 'UH' (Underhand).
            tournament_results: Deprecated compatibility input. It is validated but
                                cannot affect V2 numeric predictions or marks.
            manual_overrides: Optional dict of {name: predicted_time} supplied
                              directly by the operator. Highest cascade priority.

        Returns:
            List of MarkResult, sorted slowest-to-fastest (front marker first).
            Marks are in [MARK_FLOOR, effective_ceiling], rounded up.

        Raises:
            ValueError: If event_code is not 'SB' or 'UH'.
            ValueError: If competitors list is empty.
        """
        event_code = str(event_code).strip().upper()
        if event_code not in ("SB", "UH"):
            raise ValueError(f"event_code must be 'SB' or 'UH', got '{event_code}'")
        if not competitors:
            raise ValueError("competitors list must not be empty")

        if manual_overrides is None:
            manual_overrides = {}
        if tournament_results is None:
            tournament_results = {}

        self._validate_field_inputs(
            competitors,
            wood,
            event_code,
            tournament_results=tournament_results,
            manual_overrides=manual_overrides,
            context=context,
        )

        # Resolve the exclusive UTC cutoff and atomically snapshot all model,
        # residual, calibration, and provenance state exactly once per field.
        from strathmark.features import normalize_prediction_as_of

        supplied_context = context or PredictionContext()
        cutoff = normalize_prediction_as_of(supplied_context.prediction_as_of)
        resolved_context = PredictionContext(
            prediction_as_of=cutoff,
            request_id=supplied_context.request_id,
            seed=supplied_context.seed,
            engine=supplied_context.engine,
        )
        bundle = self._prediction_provider.snapshot(cutoff)

        results: List[MarkResult] = []
        posterior_by_result_id: Dict[int, PredictiveDistribution] = {}
        prediction_by_result_id: Dict[int, PredictionResult] = {}
        effective_records: List[CompetitorRecord] = []

        for record in competitors:
            # Apply manual override from the external dict if provided
            # (takes precedence over record.manual_time_override)
            effective_record = record
            if record.name in manual_overrides:
                from dataclasses import replace

                effective_record = replace(
                    record,
                    manual_time_override=manual_overrides[record.name],
                )

            # Apply same-tournament time from external dict if provided
            # (takes precedence over record.tournament_time)
            if record.name in tournament_results:
                from dataclasses import replace

                effective_record = replace(
                    effective_record,
                    tournament_time=tournament_results[record.name],
                )
            effective_records.append(effective_record)

            # V2 uses request-owned history and the one immutable field bundle.
            # Legacy context inputs remain accepted above but are numeric no-ops.
            prediction: PredictionResult = get_best_prediction(
                effective_record,
                wood,
                event_code,
                wood_data_df=self.wood_df,
                results_df=self.results_df,
                context=resolved_context,
                prediction_bundle=bundle,
            )

            # Compute per-competitor std_dev from event history.
            # Kept separate from forecast uncertainty so the mark optimizer does
            # not conflate performance variance with prediction uncertainty.
            # Threshold: 3+ results -> clamped sample std; <3 -> flat 3.0.
            event_times = [
                h.time_seconds for h in record.history if h.event_code.upper() == event_code.upper()
            ]
            if len(event_times) >= 3:
                raw_std = float(np.std(event_times, ddof=1))
                competitor_std = max(1.5, min(raw_std, 15.0))
            else:
                # Scale default variance with predicted time
                competitor_std = max(
                    sim_config.MIN_COMPETITOR_STD_SECONDS,
                    min(
                        prediction.value * sim_config.DEFAULT_VARIANCE_SCALING_FACTOR,
                        sim_config.MAX_COMPETITOR_STD_SECONDS,
                    ),
                )

            mark_result = MarkResult(
                name=record.name,
                mark=self.MARK_FLOOR,  # placeholder; filled by _assign_marks
                predicted_time=prediction.value,
                method_used=prediction.method,
                confidence=prediction.confidence,
                explanation=prediction.explanation,
                std_dev=competitor_std,
                competitor_id=record.competitor_id,
                interval=prediction.interval,
                engine_version=prediction.engine_version,
                model_version=prediction.model_version,
                calibration_version=prediction.calibration_version,
                evidence_cutoff=prediction.evidence_cutoff,
                warnings=list(prediction.warnings),
                prediction_id=prediction.prediction_id,
                degraded=prediction.degraded,
            )
            results.append(mark_result)
            posterior_by_result_id[id(mark_result)] = _optimizer_distribution(
                prediction,
                competitor_std,
            )
            prediction_by_result_id[id(mark_result)] = prediction

        # Sort slowest -> fastest (front marker first)
        results.sort(key=lambda r: r.predicted_time, reverse=True)

        # Assign final marks
        results = self._assign_marks(
            results,
            distributions=[posterior_by_result_id[id(result)] for result in results],
            seed=resolved_context.seed,
        )

        self._record_trusted_field(
            results,
            prediction_by_result_id=prediction_by_result_id,
            competitors=effective_records,
            wood=wood,
            event_code=event_code,
            context=resolved_context,
        )

        return results

    def _record_trusted_field(
        self,
        results: Sequence[MarkResult],
        *,
        prediction_by_result_id: Dict[int, PredictionResult],
        competitors: Sequence[CompetitorRecord],
        wood: WoodProfile,
        event_code: str,
        context: PredictionContext,
    ) -> None:
        """Attempt one non-blocking trusted field write after marks are final."""

        if self._ledger_sink is None:
            return
        if not context.request_id or not str(context.request_id).strip():
            self._set_ledger_state(results, False, "missing_request_id")
            return
        if any(not str(record.competitor_id or "").strip() for record in competitors):
            self._set_ledger_state(results, False, "missing_competitor_id")
            return

        try:
            from strathmark.features import resolve_species_properties
            from strathmark.ledger import LedgerConflictError, LedgerPrediction

            properties, species_missing = resolve_species_properties(wood.species, self.wood_df)
            record_by_id = {str(record.competitor_id).strip(): record for record in competitors}
            ledger_predictions = []
            for result in results:
                competitor_id = str(result.competitor_id).strip()
                record = record_by_id[competitor_id]
                prediction = prediction_by_result_id[id(result)]
                metadata = prediction.metadata
                feature_snapshot: Dict[str, float] = {
                    "diameter_mm": float(wood.diameter_mm),
                    **{name: float(value) for name, value in properties.items()},
                    "species_missing": float(bool(species_missing)),
                    "gender_f": float(str(record.gender or "").strip().upper() == "F"),
                    "gender_missing": float(
                        str(record.gender or "").strip().upper() not in {"M", "F"}
                    ),
                    "performance_std_dev": float(result.std_dev),
                }
                metadata_features = {
                    "history_count",
                    "effective_history_weight",
                    "same_event_state",
                    "trend_projection",
                    "cross_event_state",
                    "posterior_log_location",
                    "posterior_log_scale",
                    "shared_log_scale",
                    "calibration_sample_count",
                }
                for name in metadata_features:
                    value = metadata.get(name)
                    if isinstance(value, (int, float)) and math.isfinite(float(value)):
                        feature_snapshot[name] = float(value)

                interval = result.interval
                ledger_predictions.append(
                    LedgerPrediction(
                        competitor_id=competitor_id,
                        event_code=event_code,
                        median_seconds=result.predicted_time,
                        assigned_mark=result.mark,
                        source=result.method_used,
                        engine_version=result.engine_version,
                        model_version=result.model_version,
                        calibration_version=result.calibration_version,
                        evidence_cutoff=result.evidence_cutoff,
                        interval_lower=interval.lower if interval else None,
                        interval_upper=interval.upper if interval else None,
                        interval_coverage=interval.nominal_coverage if interval else None,
                        interval_state=interval.calibration_state if interval else None,
                        interval_scope=interval.scope if interval else None,
                        ignored_factors=tuple(prediction.ignored_factors),
                        warnings=tuple(result.warnings),
                        optimizer=result.optimizer,
                        optimizer_metadata=result.optimizer_metadata,
                        feature_snapshot=feature_snapshot,
                    )
                )

            write = self._ledger_sink.record_field(
                caller_id=self._ledger_caller_id,
                request_id=str(context.request_id).strip(),
                request_payload=self._canonical_ledger_request(
                    competitors, wood, event_code, context
                ),
                predictions=ledger_predictions,
            )
            if len(write.prediction_ids) != len(results):
                raise RuntimeError("ledger returned an incomplete prediction ID set")
            for result, prediction_id in zip(results, write.prediction_ids, strict=True):
                result.prediction_id = prediction_id
                result.ledger_recorded = bool(write.recorded)
                result.ledger_status = str(write.status)
        except LedgerConflictError:
            self._set_ledger_state(results, False, "idempotency_conflict")
        except Exception:
            _log.warning("trusted prediction ledger write failed", exc_info=True)
            self._set_ledger_state(results, False, "write_failed")

    @staticmethod
    def _set_ledger_state(results: Sequence[MarkResult], recorded: bool, status: str) -> None:
        for result in results:
            result.ledger_recorded = recorded
            result.ledger_status = status

    def _canonical_ledger_request(
        self,
        competitors: Sequence[CompetitorRecord],
        wood: WoodProfile,
        event_code: str,
        context: PredictionContext,
    ) -> Dict[str, Any]:
        """Return only request inputs that can affect V2 predictions or marks."""

        competitor_payloads = []
        for record in competitors:
            history = [
                {
                    "event_code": str(item.event_code).strip().upper(),
                    "time_seconds": float(item.time_seconds),
                    "result_date": (
                        item.result_date.isoformat()
                        if hasattr(item.result_date, "isoformat")
                        else item.result_date
                    ),
                    "diameter_mm": float(item.diameter_mm),
                    "species": str(item.species).strip(),
                }
                for item in record.history
            ]
            history.sort(
                key=lambda item: (
                    str(item["result_date"] or ""),
                    item["event_code"],
                    item["time_seconds"],
                    item["diameter_mm"],
                    item["species"],
                )
            )
            competitor_payloads.append(
                {
                    "competitor_id": str(record.competitor_id).strip(),
                    "gender": str(record.gender or "").strip().upper(),
                    "manual_time_override": record.manual_time_override,
                    "history": history,
                }
            )
        return {
            "event_code": event_code,
            "prediction_as_of": context.prediction_as_of.isoformat(),
            "diameter_mm": float(wood.diameter_mm),
            "species": str(wood.species).strip(),
            "seed": int(context.seed),
            "engine": str(context.engine or "v2").strip().lower(),
            "effective_mark_ceiling": int(self.effective_ceiling),
            "competitors": competitor_payloads,
        }

    @staticmethod
    def _validate_field_inputs(
        competitors: Sequence[CompetitorRecord],
        wood: WoodProfile,
        event_code: str,
        *,
        tournament_results: Dict[str, float],
        manual_overrides: Dict[str, float],
        context: Optional[PredictionContext],
    ) -> None:
        """Validate a complete field before model or persistence work begins."""

        from strathmark.features import normalize_prediction_as_of

        normalize_prediction_as_of(context.prediction_as_of if context else None)

        if not str(wood.species or "").strip():
            raise ValueError("wood species must not be empty")
        if not math.isfinite(float(wood.diameter_mm)) or float(wood.diameter_mm) <= 0:
            raise ValueError("wood diameter_mm must be positive and finite")
        if not isinstance(wood.quality, int) or not 1 <= wood.quality <= 10:
            raise ValueError("wood quality must be an integer from 1 to 10")

        names = [str(record.name or "").strip() for record in competitors]
        if any(not name for name in names):
            raise ValueError("competitor names must not be empty")
        duplicate_names = {name for name, count in Counter(names).items() if count > 1}
        ambiguous = duplicate_names.intersection(manual_overrides)
        if ambiguous:
            joined = ", ".join(sorted(ambiguous))
            raise ValueError(f"ambiguous name-keyed manual override for: {joined}")
        ambiguous_tournament = duplicate_names.intersection(tournament_results)
        if ambiguous_tournament:
            joined = ", ".join(sorted(ambiguous_tournament))
            raise ValueError(f"ambiguous name-keyed tournament result for: {joined}")

        stable_ids = [
            str(record.competitor_id).strip()
            for record in competitors
            if record.competitor_id is not None and str(record.competitor_id).strip()
        ]
        duplicate_ids = sorted(
            identity for identity, count in Counter(stable_ids).items() if count > 1
        )
        if duplicate_ids:
            raise ValueError(f"duplicate competitor_id values in field: {duplicate_ids}")

        for label, values in (
            ("manual override", manual_overrides),
            ("tournament result", tournament_results),
        ):
            for name, value in values.items():
                if not str(name).strip() or not _is_positive_finite(value):
                    raise ValueError(f"{label} values must have a name and positive finite time")

        for record in competitors:
            if record.manual_time_override is not None and not _is_positive_finite(
                record.manual_time_override
            ):
                raise ValueError("manual_time_override must be positive and finite")
            if record.tournament_time is not None and not _is_positive_finite(
                record.tournament_time
            ):
                raise ValueError("tournament_time must be positive and finite")
            for historical in record.history:
                if str(historical.event_code).strip().upper() not in {"SB", "UH"}:
                    raise ValueError("historical event_code must be 'SB' or 'UH'")
                if not _is_positive_finite(historical.time_seconds):
                    raise ValueError("historical time_seconds must be positive and finite")
                if not _is_positive_finite(historical.diameter_mm):
                    raise ValueError("historical diameter_mm must be positive and finite")

    def _assign_marks(
        self,
        results: List[MarkResult],
        distributions: Optional[Sequence[PredictiveDistribution]] = None,
        *,
        seed: int = 20260811,
    ) -> List[MarkResult]:
        """
        Apply gap logic to assign final marks from predicted times.

        Gap logic (from STRATHEX calculator.py lines 152-162):
            slowest_time = max(predicted_times)
            gap = slowest_time - competitor_predicted_time
            mark = MARK_FLOOR + round(gap)   # standard rounding
            mark = min(mark, effective_ceiling)

        The slowest competitor receives the floor mark (3).
        Each full second faster than the slowest adds 1 mark.
        Fractional seconds are rounded up (ceiling, not nearest).

        Args:
            results: List with predicted_time populated for each entry.
                     Must be non-empty and already sorted slowest-first.

        Returns:
            Same list with mark field populated. List is returned for chaining.
        """
        if not results:
            return results

        if distributions is None or len(distributions) != len(results):
            marks = legacy_rounded_gap_marks(
                [result.predicted_time for result in results],
                ceiling=self.effective_ceiling,
                floor=self.MARK_FLOOR,
            )
            optimizer = "rounded_gap_fallback"
            optimizer_metadata: Dict[str, object] = {
                "optimizer": optimizer,
                "simulations": 0,
                "seed": seed,
                "passes": 0,
                "reason": "posterior_unavailable",
            }
        else:
            optimization = optimize_joint_marks(
                distributions,
                ceiling=self.effective_ceiling,
                floor=self.MARK_FLOOR,
                seed=seed,
            )
            marks = optimization.marks
            optimizer = optimization.optimizer
            optimizer_metadata = optimization.metadata()

        for result, mark in zip(results, marks, strict=True):
            result.mark = mark
            result.optimizer = optimizer
            result.optimizer_metadata = dict(optimizer_metadata)

        return results

    def build_start_sheet(
        self,
        results: List[MarkResult],
        event_name: str,
        event_code: str,
        wood: WoodProfile,
    ) -> StartSheet:
        """
        Construct a StartSheet from mark results.

        Args:
            results: Output of calculate().
            event_name: Human-readable event label (e.g., "225mm SB").
            event_code: 'SB' or 'UH'.
            wood: Wood profile used for this event.

        Returns:
            StartSheet ordered front-marker to back-marker.
        """
        return StartSheet(
            event_name=event_name,
            event_code=event_code,
            species=wood.species,
            diameter_mm=wood.diameter_mm,
            quality=wood.quality,
            entries=list(results),
        )


# ---------------------------------------------------------------------------
# Phase 5E: process_competition_day -- batch helper
# ---------------------------------------------------------------------------


def process_competition_day(
    events,
    overrides=None,
):
    """
    Process multiple events for a competition day in a single call.

    Iterates over each event specification, builds a HandicapCalculator from
    the provided data source (xlsx path or pre-loaded DataFrames), runs
    calculate(), and returns a list of result dicts -- one per event.

    Args:
        events: List of event specification dicts, each containing:
            'event_name'    (str)         -- human-readable label, e.g. '225mm SB'
            'event_code'    (str)         -- 'SB' or 'UH'
            'species'       (str)         -- wood species
            'diameter_mm'   (float)       -- block diameter in mm
            'quality'       (int)         -- wood quality 1-10
            'competitors'   (list)        -- list of CompetitorRecord objects
            'xlsx_path'     (str, opt)    -- path to Excel workbook (file-based data)
            'wood_df'       (DataFrame, opt) -- pre-loaded wood properties DataFrame
            'results_df'    (DataFrame, opt) -- pre-loaded historical results DataFrame
            'event_ceiling' (int, opt)    -- per-event mark ceiling override
            'tournament_results' (dict, opt) -- {name: time} from earlier rounds
            'overrides'     (dict, opt)   -- per-event {name: predicted_time} overrides
        overrides: Optional global manual override dict {competitor_name: predicted_time}.

    Returns:
        List of dicts, one per event, each containing:
            'event_name'  (str)
            'event_code'  (str)
            'results'     (list[MarkResult])
            'start_sheet' (StartSheet)
    """
    if overrides is None:
        overrides = {}

    day_results = []

    for event_spec in events:
        event_name = event_spec["event_name"]
        event_code = event_spec["event_code"]
        competitors = event_spec["competitors"]

        wood = WoodProfile(
            species=event_spec["species"],
            diameter_mm=float(event_spec["diameter_mm"]),
            quality=int(event_spec.get("quality", 5)),
        )

        xlsx_path = event_spec.get("xlsx_path")
        wood_df = event_spec.get("wood_df")
        results_df = event_spec.get("results_df")
        event_ceiling = event_spec.get("event_ceiling")

        if xlsx_path and wood_df is None:
            kw = {"event_ceiling": event_ceiling} if event_ceiling else {}
            calc = HandicapCalculator.from_xlsx(xlsx_path, **kw)
        else:
            init_kwargs = {}
            if event_ceiling:
                init_kwargs["event_ceiling"] = event_ceiling
            if wood_df is not None:
                init_kwargs["wood_df"] = wood_df
            if results_df is not None:
                init_kwargs["results_df"] = results_df
            calc = HandicapCalculator(**init_kwargs)

        merged_overrides = dict(overrides)
        merged_overrides.update(event_spec.get("overrides", {}))

        tournament_results = event_spec.get("tournament_results")

        mark_results = calc.calculate(
            competitors=competitors,
            wood=wood,
            event_code=event_code,
            tournament_results=tournament_results,
            manual_overrides=merged_overrides if merged_overrides else None,
        )

        sheet = calc.build_start_sheet(
            results=mark_results,
            event_name=event_name,
            event_code=event_code,
            wood=wood,
        )

        day_results.append(
            {
                "event_name": event_name,
                "event_code": event_code,
                "results": mark_results,
                "start_sheet": sheet,
            }
        )

    return day_results
