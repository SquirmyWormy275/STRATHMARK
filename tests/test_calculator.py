"""
Tests for strathmark.calculator — HandicapCalculator and mark assignment.

These tests validate the core invariants that must NEVER be violated:
    1. Mark floor: all marks >= 3 seconds
    2. Mark ceiling: all marks <= 183 seconds
    3. Gap logic: slowest competitor gets exactly Mark 3
    4. Ceiling arithmetic: gaps are rounded UP (not nearest)
    5. Ranking preserved: faster predicted time -> higher mark

Tests are organized in three groups:
    - Unit: _assign_marks() with known inputs
    - Integration: full calculate() with minimal fixture data
    - Edge cases: single competitor, tied predictions, extreme gaps
"""

import re
import pytest
from datetime import date

from strathmark.calculator import HandicapCalculator, MarkResult, StartSheet
from strathmark.predictor import (
    CompetitorRecord,
    WoodProfile,
    HistoricalResult,
    predict_baseline,
)
from strathmark.config import rules


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _mark_result(name: str, predicted_time: float) -> MarkResult:
    """Build a MarkResult with mark=0 (to be filled by _assign_marks)."""
    return MarkResult(
        name=name,
        mark=0,
        predicted_time=predicted_time,
        method_used="test",
        confidence="HIGH",
        explanation="test fixture",
    )


def _competitor(name: str, time_s: float, tournament_time: float = None) -> CompetitorRecord:
    """Build a CompetitorRecord with three identical historical results."""
    history = [
        HistoricalResult("SB", time_s, "Pine", 300.0, 5, date(2025, 1, 1)),
        HistoricalResult("SB", time_s, "Pine", 300.0, 5, date(2025, 2, 1)),
        HistoricalResult("SB", time_s, "Pine", 300.0, 5, date(2025, 3, 1)),
    ]
    return CompetitorRecord(name=name, history=history, tournament_time=tournament_time)


PINE_300 = WoodProfile(species="Pine", diameter_mm=300, quality=5)


# ---------------------------------------------------------------------------
# Mark floor invariant
# ---------------------------------------------------------------------------

class TestMarkFloor:
    """Mark floor = 3 seconds. Never lower under any circumstances."""

    def test_slowest_competitor_gets_mark_3(self):
        """The competitor with the highest predicted time must receive exactly Mark 3."""
        calc = HandicapCalculator()
        # Pass slowest first (expected order for _assign_marks)
        results = [
            _mark_result("Alice", 60.0),   # slowest -> front marker
            _mark_result("Bob", 45.0),
        ]
        calc._assign_marks(results)
        assert results[0].mark == rules.MIN_MARK_SECONDS

    def test_single_competitor_gets_mark_3(self):
        """A heat with one competitor always yields Mark 3."""
        calc = HandicapCalculator()
        results = [_mark_result("Solo", 30.0)]
        calc._assign_marks(results)
        assert results[0].mark == rules.MIN_MARK_SECONDS

    def test_mark_never_below_floor(self):
        """No mark in any result should be below Rules.MIN_MARK_SECONDS."""
        calc = HandicapCalculator()
        results = [
            _mark_result("Alice", 60.0),
            _mark_result("Bob", 50.0),
            _mark_result("Carol", 40.0),
        ]
        calc._assign_marks(results)
        for r in results:
            assert r.mark >= rules.MIN_MARK_SECONDS


# ---------------------------------------------------------------------------
# Mark ceiling invariant
# ---------------------------------------------------------------------------

class TestMarkCeiling:
    """Mark ceiling = 183 seconds system-wide."""

    def test_extreme_gap_clamped_to_ceiling(self):
        """
        A 200-second gap between slowest and fastest would compute mark=203.
        Must be clamped to 183.
        """
        calc = HandicapCalculator()
        results = [
            _mark_result("Slow", 200.0),   # slowest -> mark 3
            _mark_result("Fast", 3.0),     # gap=197 -> unclamped mark=200, clamped to 183
        ]
        calc._assign_marks(results)
        fast = next(r for r in results if r.name == "Fast")
        assert fast.mark == rules.MAX_MARK_SECONDS

    def test_ceiling_never_exceeded(self):
        """No mark should exceed Rules.MAX_MARK_SECONDS."""
        calc = HandicapCalculator()
        results = [
            _mark_result("Slow", 500.0),
            _mark_result("Medium", 300.0),
            _mark_result("Fast", 1.0),
        ]
        calc._assign_marks(results)
        for r in results:
            assert r.mark <= rules.MAX_MARK_SECONDS

    def test_custom_event_ceiling_respected(self):
        """HandicapCalculator(event_ceiling=50) must clamp all marks at 50."""
        calc = HandicapCalculator(event_ceiling=50)
        results = [
            _mark_result("Slow", 100.0),   # slowest -> mark 3
            _mark_result("Fast", 40.0),    # gap=60 -> unclamped mark=63, clamped to 50
        ]
        calc._assign_marks(results)
        fast = next(r for r in results if r.name == "Fast")
        assert fast.mark == 50


# ---------------------------------------------------------------------------
# Gap logic and ceiling arithmetic
# ---------------------------------------------------------------------------

class TestGapLogic:
    """Verify the gap formula: mark = 3 + int(gap + 0.999) (ceiling arithmetic)."""

    def test_exact_integer_gap(self):
        """
        Gap of exactly 5.0 seconds -> mark = 3 + 5 = 8.
        int(5.0 + 0.999) = int(5.999) = 5 — no +1 error on exact integers.
        """
        calc = HandicapCalculator()
        results = [
            _mark_result("Slow", 35.0),    # slowest -> mark 3
            _mark_result("Fast", 30.0),    # gap=5.0 -> mark=8
        ]
        calc._assign_marks(results)
        assert results[0].mark == 3
        assert results[1].mark == 8

    def test_fractional_gap_rounds_up(self):
        """
        Gap of 5.5 -> mark = 3 + 6 = 9  (ceiling: int(5.5 + 0.999) = int(6.499) = 6).
        Gap of 4.5 -> mark = 3 + 5 = 8  (ceiling: int(4.5 + 0.999) = int(5.499) = 5).

        Use 0.5-second fractions which are exact in IEEE 754 float, avoiding
        floating point subtraction errors that can occur near integer boundaries.
        """
        calc = HandicapCalculator()

        # gap = 5.5 -> int(5.5 + 0.999) = int(6.499) = 6 -> mark = 9
        r1 = [
            _mark_result("Slow", 40.5),
            _mark_result("Fast", 35.0),
        ]
        calc._assign_marks(r1)
        assert r1[1].mark == 9

        # gap = 4.5 -> int(4.5 + 0.999) = int(5.499) = 5 -> mark = 8
        r2 = [
            _mark_result("Slow", 39.5),
            _mark_result("Fast", 35.0),
        ]
        calc._assign_marks(r2)
        assert r2[1].mark == 8

    def test_zero_gap_gives_mark_3(self):
        """Two competitors with identical predicted times both get Mark 3."""
        calc = HandicapCalculator()
        results = [
            _mark_result("Alice", 30.0),
            _mark_result("Bob", 30.0),
        ]
        calc._assign_marks(results)
        assert results[0].mark == 3
        assert results[1].mark == 3

    def test_ranking_preserved(self):
        """
        Competitor with lower predicted time must always get a higher mark.
        (Faster chopper starts later in handicap events.)
        """
        calc = HandicapCalculator()
        results = [
            _mark_result("Slow", 60.0),
            _mark_result("Medium", 45.0),
            _mark_result("Fast", 30.0),
        ]
        calc._assign_marks(results)
        # Marks must be non-decreasing from slowest to fastest
        for i in range(len(results) - 1):
            assert results[i].mark <= results[i + 1].mark


# ---------------------------------------------------------------------------
# Tournament result weighting
# ---------------------------------------------------------------------------

class TestTournamentWeighting:
    """Verify 97% weighting of same-tournament results (tested via predict_baseline)."""

    def test_tournament_result_weighted_97_pct(self):
        """
        When tournament_time is provided, the baseline is:
            (tournament_time * 0.97) + (historical_baseline * 0.03)
        """
        historical_time = 30.0
        tournament_time_val = 50.0
        comp = _competitor("Alice", historical_time, tournament_time=tournament_time_val)

        result = predict_baseline(comp, PINE_300, "SB")

        assert result is not None
        expected = (tournament_time_val * 0.97) + (historical_time * 0.03)
        # Allow +-2s tolerance for quality normalization and shrinkage rounding
        assert abs(result.value - expected) < 2.0, (
            f"Expected ~{expected:.2f}s, got {result.value:.2f}s"
        )

    def test_confidence_upgraded_with_tournament_result(self):
        """Confidence must be VERY HIGH when tournament_time is used."""
        comp = _competitor("Bob", 30.0, tournament_time=35.0)
        result = predict_baseline(comp, PINE_300, "SB")
        assert result is not None
        assert result.confidence == "VERY HIGH"


# ---------------------------------------------------------------------------
# Start sheet
# ---------------------------------------------------------------------------

class TestStartSheet:
    """StartSheet ordering and rendering."""

    def _build_sheet(self) -> StartSheet:
        calc = HandicapCalculator()
        results = [
            _mark_result("Alice", 60.0),
            _mark_result("Bob", 45.0),
            _mark_result("Carol", 30.0),
        ]
        calc._assign_marks(results)
        return calc.build_start_sheet(results, "300mm SB", "SB", PINE_300)

    def test_start_sheet_ordered_front_to_back(self):
        """Entries must be sorted by ascending mark (smallest mark first)."""
        sheet = self._build_sheet()
        marks = [e.mark for e in sheet.entries]
        assert marks == sorted(marks), f"Entries not sorted by mark: {marks}"

    def test_start_sheet_render_max_line_width(self):
        """All lines in render() output must be at most 70 characters."""
        sheet = self._build_sheet()
        rendered = sheet.render()
        for i, line in enumerate(rendered.splitlines()):
            assert len(line) <= 70, (
                f"Line {i} has {len(line)} chars (max 70): {line!r}"
            )

    def test_start_sheet_render_plain_text(self):
        """render() output must contain no ANSI escape codes."""
        sheet = self._build_sheet()
        rendered = sheet.render()
        # ANSI escape sequences start with ESC (\x1b or \033)
        ansi_pattern = re.compile(r'\x1b\[[\d;]*m')
        assert not ansi_pattern.search(rendered), (
            "render() output contains ANSI escape codes"
        )
