"""
Handicap Mark Calculator
========================

Core handicap mark computation for woodchopping competitions.

This module contains HandicapCalculator, the primary public class for computing
association-agnostic handicap marks. It orchestrates the prediction cascade
(Manual > LLM > ML > Panel fallback), gap computation, floor/ceiling
enforcement, and produces a ranked start sheet.

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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from strathmark.config import llm_config, sim_config
from strathmark.predictor import (
    CompetitorRecord,
    PredictionResult,
    WoodProfile,
    get_best_prediction,
)

_log = logging.getLogger(__name__)


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

    Prediction cascade (highest to lowest priority):
        1. Manual override (explicit time supplied by operator)
        2. LLM quality adjustment on top of weighted baseline
        3. ML model (XGBoost, trained on historical data with time-decay weights)
        4. Panel mark fallback (division-based default for competitors with no history)

    Tournament result weighting:
        When same-tournament times are available (earlier rounds on SAME wood),
        they are weighted at 97% vs 3% historical. Confidence becomes VERY HIGH.

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
    ) -> None:
        """
        Args:
            event_ceiling: Optional lower ceiling for this event (seconds).
                           Must be > MARK_FLOOR. If None, system default of 183 is used.
            ollama_url: Base URL for the Ollama API used by the LLM prediction layer.
            wood_df: Optional species properties DataFrame (Janka hardness, etc.).
                     When provided, passed to get_best_prediction() on every call.
            results_df: Optional historical results DataFrame. When provided and no
                        ML model has been trained yet, training is attempted on the
                        first call to calculate(). Cached in self._ml_model.
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

        self._ollama_url = ollama_url
        self.wood_df: Optional[pd.DataFrame] = wood_df
        self.results_df: Optional[pd.DataFrame] = results_df
        self._ml_model = None  # set on first calculate() when results_df is available

    @classmethod
    def from_db(
        cls,
        competitor_ids: Optional[List[str]] = None,
        wood_df: Optional[pd.DataFrame] = None,
        **kwargs,
    ) -> "HandicapCalculator":
        """
        Construct a HandicapCalculator pre-loaded with data from the global database.

        Calls pull_results() and pull_competitors() internally to fetch the
        historical results needed for ML training and baseline prediction.

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
        results_df already set. ML training is deferred to the first call to
        calculate().

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
    ) -> List[MarkResult]:
        """
        Compute handicap marks for all competitors in a heat/round.

        Args:
            competitors: Ordered sequence of CompetitorRecord objects.
                         Each record includes historical times and metadata.
            wood: Wood characteristics for this event (species, diameter, quality).
            event_code: 'SB' (Standing Block) or 'UH' (Underhand).
            tournament_results: Optional dict of {name: actual_time} from earlier
                                rounds on the SAME wood. When provided, times are
                                weighted 97% vs 3% historical (same-wood optimization).
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

        # Lazy ML training: attempt once per instance when results_df is available
        # and no model has been trained yet.
        if self._ml_model is None and self.results_df is not None:
            from strathmark.predictor import MLModel

            ml = MLModel()
            try:
                trained = ml.train(self.results_df, self.wood_df)
                if trained:
                    self._ml_model = ml
                else:
                    _log.warning(
                        "HandicapCalculator: ML training returned False "
                        "(insufficient data). Continuing with baseline."
                    )
            except Exception as exc:
                _log.warning(
                    "HandicapCalculator: ML training failed (%s). Continuing with baseline.",
                    exc,
                )

        results: List[MarkResult] = []

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

            # Run prediction cascade, forwarding stored data frames and ML model
            llm_client = {
                "url": self._ollama_url,
                "model": llm_config.PREDICTION_MODEL,
                "timeout": llm_config.TIMEOUT_SECONDS,
            }
            prediction: PredictionResult = get_best_prediction(
                effective_record,
                wood,
                event_code,
                wood_data_df=self.wood_df,
                results_df=self.results_df,
                ml_model=self._ml_model,
                llm_client=llm_client,
            )

            # Compute per-competitor std_dev from event history.
            # Done here (not in predictor.py) so all cascade levels get the correct
            # value regardless of which method (ML/LLM/baseline/panel) won.
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

            results.append(
                MarkResult(
                    name=record.name,
                    mark=self.MARK_FLOOR,  # placeholder; filled by _assign_marks
                    predicted_time=prediction.value,
                    method_used=prediction.method,
                    confidence=prediction.confidence,
                    explanation=prediction.explanation,
                    std_dev=competitor_std,
                )
            )

        # Sort slowest -> fastest (front marker first)
        results.sort(key=lambda r: r.predicted_time, reverse=True)

        # Assign final marks
        results = self._assign_marks(results)

        return results

    def _assign_marks(self, results: List[MarkResult]) -> List[MarkResult]:
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

        # Slowest competitor gets mark 3 (front marker)
        slowest_time = results[0].predicted_time

        for result in results:
            gap = slowest_time - result.predicted_time
            mark = self.MARK_FLOOR + round(gap)  # standard rounding
            mark = min(mark, self.effective_ceiling)
            result.mark = mark

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
