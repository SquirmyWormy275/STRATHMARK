"""
Handicap Mark Calculator
========================

Core handicap mark computation for woodchopping competitions.

This module contains HandicapCalculator, the primary public class for computing
AAA-compliant handicap marks. It orchestrates the prediction cascade (Manual >
LLM > ML > Panel fallback), gap computation, floor/ceiling enforcement, and
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
    woodchopping/handicaps/qaa_legacy.py  -> calculate_qaa_legacy_marks()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from strathmark.predictor import (
    CompetitorRecord,
    WoodProfile,
    PredictionResult,
    get_best_prediction,
)
from strathmark.wood import (
    calculate_effective_janka_hardness,
    interpolate_qaa_tables,
)
from strathmark.config import rules


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
        wood_line = (
            f"{self.species}  {self.diameter_mm:.0f}mm  Quality {self.quality}/10"
        ).center(68)
        lines.append("|" + wood_line + "|")

        lines.append("|" + "-" * 68 + "|")

        # Column header
        # Format: MARK(6) | NAME(30) | PREDICTED(12) | METHOD(12) | CONFIDENCE(8)
        header = (
            f"{'MARK':<6}  "
            f"{'COMPETITOR':<30}  "
            f"{'PRED(s)':<9}  "
            f"{'METHOD':<10}  "
            f"{'CONF':<6}"
        )
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
    ) -> None:
        """
        Args:
            event_ceiling: Optional lower ceiling for this event (seconds).
                           Must be > MARK_FLOOR. If None, system default of 183 is used.
            ollama_url: Base URL for the Ollama API used by the LLM prediction layer.
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
            raise ValueError(
                f"event_code must be 'SB' or 'UH', got '{event_code}'"
            )
        if not competitors:
            raise ValueError("competitors list must not be empty")

        if manual_overrides is None:
            manual_overrides = {}
        if tournament_results is None:
            tournament_results = {}

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

            # Run prediction cascade
            prediction: PredictionResult = get_best_prediction(
                effective_record,
                wood,
                event_code,
                ollama_url=self._ollama_url,
            )

            results.append(
                MarkResult(
                    name=record.name,
                    mark=self.MARK_FLOOR,       # placeholder; filled by _assign_marks
                    predicted_time=prediction.value,
                    method_used=prediction.method,
                    confidence=prediction.confidence,
                    explanation=prediction.explanation,
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
            mark = MARK_FLOOR + int(gap + 0.999)   # ceiling arithmetic
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
            mark = self.MARK_FLOOR + int(gap + 0.999)  # ceiling arithmetic
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
# QAA legacy calculator
# ---------------------------------------------------------------------------

class QAACalculator:
    """
    Calculate handicap marks using the QAA (Queensland Axemen's Association)
    empirical lookup tables.

    QAA methodology differs from AI-enhanced prediction:
        1. Predict a competitor "book mark" at 300mm standard diameter
           using baseline V2 hybrid (no LLM, no ML -- pure historical).
        2. Cap book mark at MAX_BOOK_MARK (43s for open events).
        3. Scale the book mark to the target diameter using QAA lookup tables,
           blending hardwood/medium/softwood tables based on effective Janka hardness.
        4. Round the scaled mark to the nearest whole second.

    This produces marks that are compatible with the traditional QAA handicap
    system used in Australian and international woodchopping competition.

    Design invariants:
        - Mark floor = 3 (enforced after all other logic)
        - MAX_BOOK_MARK = 43 (QAA open event cap)
        - STANDARD_DIAMETER_MM = 300.0 (all book marks computed at 300mm)
        - Quality is FIXED to 5 (standard conditions) for book mark derivation
          -- the QAA tables already encode species-level hardness variation.
    """

    MARK_FLOOR: int = 3
    MAX_BOOK_MARK: float = 43.0
    STANDARD_DIAMETER_MM: float = 300.0

    def calculate(
        self,
        competitors: Sequence[CompetitorRecord],
        wood: WoodProfile,
        event_code: str,
        results_df=None,   # pandas DataFrame -- optional, passed to predict_baseline
        wood_df=None,      # pandas DataFrame -- optional species properties table
    ) -> List[MarkResult]:
        """
        Calculate QAA legacy marks for SB/UH events.

        Steps per competitor:
            1. Predict baseline time at 300mm, quality=5 (standard conditions).
            2. Cap book mark at MAX_BOOK_MARK.
            3. Scale to target diameter via interpolate_qaa_tables().
            4. Round to nearest second, apply floor.

        Args:
            competitors: Sequence of CompetitorRecord objects.
            wood: Wood profile for this event (species, diameter, quality).
            event_code: 'SB' or 'UH'. Other codes are rejected.
            results_df: Optional historical results DataFrame for baseline prediction.
                        If None, baseline prediction falls back to panel mark.
            wood_df: Optional species properties DataFrame for Janka lookup.
                     If None, a default effective_janka of 1000.0 is used.

        Returns:
            List of MarkResult with method_used='QAA', sorted slowest-to-fastest
            (front marker first).

        Raises:
            ValueError: If event_code is not 'SB' or 'UH'.
            ValueError: If competitors list is empty.
        """
        from strathmark.predictor import predict_baseline

        event_code = str(event_code).strip().upper()
        if event_code not in ("SB", "UH"):
            raise ValueError(
                f"QAACalculator only supports 'SB' and 'UH', got '{event_code}'"
            )
        if not competitors:
            raise ValueError("competitors list must not be empty")

        quality_val = max(1, min(10, int(wood.quality)))

        # Resolve effective Janka hardness for this species/quality combination
        # This blends the tables to give the right hardwood/medium/soft weighting.
        effective_janka = calculate_effective_janka_hardness(
            wood.species, quality_val, wood_df
        )

        results: List[MarkResult] = []

        for record in competitors:
            # Step 1: Predict a book mark at 300mm, quality=5 (standard conditions).
            # Use a WoodProfile with the same species but standard diameter/quality.
            standard_wood = WoodProfile(
                species=wood.species,
                diameter_mm=self.STANDARD_DIAMETER_MM,
                quality=5,
            )

            # Build a copy of the record stripped of tournament/manual overrides
            # so the QAA path uses pure historical baseline only.
            from dataclasses import replace
            baseline_record = replace(
                record,
                manual_time_override=None,
                tournament_time=None,
            )

            baseline_pred: PredictionResult = predict_baseline(
                baseline_record,
                standard_wood,
                event_code,
                results_df=results_df,
                wood_df=wood_df,
            )

            base_time = baseline_pred.value

            # Step 2: Cap book mark at MAX_BOOK_MARK (QAA open event limit = 43s)
            book_mark_300 = max(float(self.MARK_FLOOR), min(base_time, self.MAX_BOOK_MARK))

            # Step 3: Scale to target diameter via QAA lookup tables
            scaled_mark, weights = interpolate_qaa_tables(
                book_mark_300,
                float(wood.diameter_mm),
                effective_janka,
            )

            # Step 4: Round to nearest second, apply floor
            mark = int(round(scaled_mark))
            mark = max(self.MARK_FLOOR, mark)

            # Build a compact explanation for the start sheet
            blend_parts = []
            if weights.get("softwood", 0.0) > 0.01:
                blend_parts.append(f"{weights['softwood']*100:.0f}% soft")
            if weights.get("medium", 0.0) > 0.01:
                blend_parts.append(f"{weights['medium']*100:.0f}% med")
            if weights.get("hardwood", 0.0) > 0.01:
                blend_parts.append(f"{weights['hardwood']*100:.0f}% hard")

            blend_str = ", ".join(blend_parts) if blend_parts else "mixed"

            explanation = (
                f"QAA: {book_mark_300:.1f}s @ 300mm -> {scaled_mark:.1f}s "
                f"@ {wood.diameter_mm:.0f}mm "
                f"({blend_str}, {effective_janka:.0f} Janka)"
            )

            results.append(
                MarkResult(
                    name=record.name,
                    mark=mark,
                    predicted_time=scaled_mark,  # use scaled mark as "predicted time"
                    method_used="QAA",
                    confidence=baseline_pred.confidence,
                    explanation=explanation,
                )
            )

        # Sort slowest-to-fastest (highest scaled mark first = front marker first)
        results.sort(key=lambda r: r.predicted_time, reverse=True)

        return results
