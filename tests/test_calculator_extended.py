"""Extended tests for strathmark/calculator.py — factory methods, batch processing, edge cases."""

from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from strathmark.calculator import (
    HandicapCalculator,
    MarkResult,
    StartSheet,
    process_competition_day,
)
from strathmark.predictor import (
    CompetitorRecord,
    HistoricalResult,
    WoodProfile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _history(event_code="SB", n=5, base_time=50.0, days_apart=30):
    today = date.today()
    return [
        HistoricalResult(
            event_code=event_code,
            time_seconds=base_time + i * 0.5,
            species="S01",
            diameter_mm=300,
            quality=5,
            result_date=today - timedelta(days=(n - i) * days_apart),
        )
        for i in range(n)
    ]


def _competitor(name, time_s, **kwargs):
    history = [
        HistoricalResult("SB", time_s, "S01", 300.0, 5, date(2025, 1, 1)),
        HistoricalResult("SB", time_s, "S01", 300.0, 5, date(2025, 2, 1)),
        HistoricalResult("SB", time_s, "S01", 300.0, 5, date(2025, 3, 1)),
    ]
    return CompetitorRecord(name=name, history=history, **kwargs)


PINE_300 = WoodProfile(species="S01", diameter_mm=300, quality=5)


# ---------------------------------------------------------------------------
# from_db factory method
# ---------------------------------------------------------------------------


class TestFromDb:
    def test_raises_without_wood_df(self):
        """from_db requires wood_df."""
        with pytest.raises(ValueError, match="wood_df"):
            HandicapCalculator.from_db(wood_df=None)

    @patch("strathmark.calculator.HandicapCalculator.from_db")
    def test_from_db_returns_calculator(self, mock_from_db):
        """from_db should return a HandicapCalculator instance."""
        mock_from_db.return_value = HandicapCalculator()
        calc = HandicapCalculator.from_db(wood_df=pd.DataFrame())
        assert isinstance(calc, HandicapCalculator)


# ---------------------------------------------------------------------------
# from_xlsx factory method
# ---------------------------------------------------------------------------


class TestFromXlsx:
    @patch("strathmark.predictor._standardize_results_df")
    @patch("strathmark.calculator.HandicapCalculator.from_xlsx")
    def test_from_xlsx_returns_calculator(self, mock_from_xlsx, mock_std):
        """from_xlsx should return a HandicapCalculator instance."""
        mock_from_xlsx.return_value = HandicapCalculator()
        calc = HandicapCalculator.from_xlsx("fake_path.xlsx")
        assert isinstance(calc, HandicapCalculator)


# ---------------------------------------------------------------------------
# process_competition_day
# ---------------------------------------------------------------------------


class TestProcessCompetitionDay:
    def test_single_event(self):
        """Process a single event and get results."""
        competitors = [
            _competitor("Alice", 30.0),
            _competitor("Bob", 40.0),
        ]
        events = [
            {
                "event_name": "300mm SB",
                "event_code": "SB",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": competitors,
            }
        ]
        results = process_competition_day(events)
        assert len(results) == 1
        assert results[0]["event_name"] == "300mm SB"
        assert results[0]["event_code"] == "SB"
        assert len(results[0]["results"]) == 2
        assert isinstance(results[0]["start_sheet"], StartSheet)

    def test_multiple_events(self):
        """Process multiple events in one call."""
        competitors = [
            _competitor("Alice", 30.0),
            _competitor("Bob", 40.0),
        ]
        events = [
            {
                "event_name": "300mm SB",
                "event_code": "SB",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": competitors,
            },
            {
                "event_name": "300mm UH",
                "event_code": "UH",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": [
                    CompetitorRecord(
                        name="Alice", history=_history(event_code="UH", base_time=30.0)
                    ),
                    CompetitorRecord(name="Bob", history=_history(event_code="UH", base_time=40.0)),
                ],
            },
        ]
        results = process_competition_day(events)
        assert len(results) == 2

    def test_global_overrides_applied(self):
        """Global manual overrides should affect results."""
        competitors = [
            _competitor("Alice", 30.0),
            _competitor("Bob", 40.0),
        ]
        events = [
            {
                "event_name": "300mm SB",
                "event_code": "SB",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": competitors,
            }
        ]
        results = process_competition_day(events, overrides={"Alice": 99.0})
        alice_result = next(r for r in results[0]["results"] if r.name == "Alice")
        # Alice should have manual override
        assert alice_result.method_used == "manual"
        assert alice_result.predicted_time == 99.0

    def test_per_event_overrides_applied(self):
        """Per-event overrides should take precedence over global."""
        competitors = [
            _competitor("Alice", 30.0),
            _competitor("Bob", 40.0),
        ]
        events = [
            {
                "event_name": "300mm SB",
                "event_code": "SB",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": competitors,
                "overrides": {"Alice": 77.0},
            }
        ]
        results = process_competition_day(events, overrides={"Alice": 99.0})
        alice_result = next(r for r in results[0]["results"] if r.name == "Alice")
        # Per-event override (77.0) should override global (99.0)
        assert alice_result.predicted_time == 77.0

    def test_event_ceiling_respected(self):
        """Per-event ceiling should clamp marks."""
        competitors = [
            _competitor("Alice", 60.0),
            _competitor("Bob", 20.0),
        ]
        events = [
            {
                "event_name": "300mm SB",
                "event_code": "SB",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": competitors,
                "event_ceiling": 30,
            }
        ]
        results = process_competition_day(events)
        for r in results[0]["results"]:
            assert r.mark <= 30

    def test_tournament_results_are_accepted_but_inactive(self):
        """Legacy tournament input is accepted without changing V2 numerics."""
        competitors = [
            _competitor("Alice", 30.0),
            _competitor("Bob", 40.0),
        ]
        events = [
            {
                "event_name": "300mm SB",
                "event_code": "SB",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": competitors,
                "tournament_results": {"Alice": 25.0},
            }
        ]
        results = process_competition_day(events)
        events[0]["tournament_results"] = {}
        control = process_competition_day(events)
        alice_result = next(r for r in results[0]["results"] if r.name == "Alice")
        control_result = next(r for r in control[0]["results"] if r.name == "Alice")
        assert alice_result.predicted_time.hex() == control_result.predicted_time.hex()


# ---------------------------------------------------------------------------
# HandicapCalculator additional edge cases
# ---------------------------------------------------------------------------


class TestCalculatorEdgeCases:
    def test_calculate_with_manual_overrides_dict(self):
        """manual_overrides dict should override record.manual_time_override."""
        calc = HandicapCalculator()
        comp = _competitor("Alice", 30.0)
        results = calc.calculate([comp], PINE_300, "SB", manual_overrides={"Alice": 88.0})
        assert len(results) == 1
        assert results[0].method_used == "manual"
        assert results[0].predicted_time == 88.0

    def test_calculate_sorts_slowest_first(self):
        """Results should be sorted slowest to fastest."""
        calc = HandicapCalculator()
        comps = [
            _competitor("Fast", 20.0),
            _competitor("Slow", 50.0),
            _competitor("Mid", 35.0),
        ]
        results = calc.calculate(comps, PINE_300, "SB")
        assert results[0].predicted_time >= results[-1].predicted_time

    def test_std_dev_populated(self):
        """Each MarkResult should have a std_dev value."""
        calc = HandicapCalculator()
        comp = _competitor("Alice", 30.0)
        results = calc.calculate([comp], PINE_300, "SB")
        assert results[0].std_dev > 0

    def test_to_simulation_dict(self):
        """MarkResult.to_simulation_dict() should produce valid sim input."""
        mr = MarkResult(
            name="Alice",
            mark=10,
            predicted_time=30.0,
            method_used="baseline",
            confidence="HIGH",
            explanation="test",
            std_dev=3.0,
        )
        d = mr.to_simulation_dict()
        assert d["name"] == "Alice"
        assert d["mark"] == 10
        assert d["predicted_time"] == 30.0
        assert d["std_dev"] == 3.0

    def test_start_sheet_render_contains_competitor_names(self):
        """Rendered start sheet should include all competitor names."""
        calc = HandicapCalculator()
        comps = [_competitor("Alice", 30.0), _competitor("Bob", 40.0)]
        results = calc.calculate(comps, PINE_300, "SB")
        sheet = calc.build_start_sheet(results, "300mm SB", "SB", PINE_300)
        rendered = sheet.render()
        assert "Alice" in rendered
        assert "Bob" in rendered

    def test_empty_assign_marks(self):
        """_assign_marks with empty list should return empty."""
        calc = HandicapCalculator()
        result = calc._assign_marks([])
        assert result == []


# ---------------------------------------------------------------------------
# Boundary condition tests
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_mark_exactly_at_floor(self):
        """Competitor at slowest time gets exactly mark 3."""
        calc = HandicapCalculator()
        from strathmark.calculator import MarkResult as MR

        results = [MR("A", 0, 100.0, "test", "HIGH", "test")]
        calc._assign_marks(results)
        assert results[0].mark == 3

    def test_mark_at_system_ceiling(self):
        """Gap of exactly 180 -> mark = 183."""
        calc = HandicapCalculator()
        from strathmark.calculator import MarkResult as MR

        results = [
            MR("Slow", 0, 200.0, "test", "HIGH", "test"),
            MR("Fast", 0, 20.0, "test", "HIGH", "test"),
        ]
        results.sort(key=lambda r: r.predicted_time, reverse=True)
        calc._assign_marks(results)
        assert results[1].mark == min(3 + round(180.0), 183)

    def test_gaps_with_half_second_increments(self):
        """Verify banker's rounding with 0.5s gaps."""
        calc = HandicapCalculator()
        from strathmark.calculator import MarkResult as MR

        results = [
            MR("Slow", 0, 40.0, "test", "HIGH", "test"),
            MR("Gap1.5", 0, 38.5, "test", "HIGH", "test"),  # gap=1.5 -> round(1.5)=2 -> mark=5
            MR("Gap2.5", 0, 37.5, "test", "HIGH", "test"),  # gap=2.5 -> round(2.5)=2 -> mark=5
            MR("Gap3.5", 0, 36.5, "test", "HIGH", "test"),  # gap=3.5 -> round(3.5)=4 -> mark=7
        ]
        results.sort(key=lambda r: r.predicted_time, reverse=True)
        calc._assign_marks(results)
        assert results[0].mark == 3  # Slow (40.0)
        assert results[1].mark == 5  # Gap1.5 (38.5) -> round(1.5)=2 -> 3+2=5
        assert results[2].mark == 5  # Gap2.5 (37.5) -> round(2.5)=2 -> 3+2=5 (banker's)
        assert results[3].mark == 7  # Gap3.5 (36.5) -> round(3.5)=4 -> 3+4=7 (banker's)


# ---------------------------------------------------------------------------
# Config validation: is_valid_event
# ---------------------------------------------------------------------------


class TestConfigIsValidEvent:
    def test_sb_is_valid(self):
        from strathmark.config import is_valid_event

        assert is_valid_event("SB") is True

    def test_uh_is_valid(self):
        from strathmark.config import is_valid_event

        assert is_valid_event("UH") is True

    def test_invalid_event(self):
        from strathmark.config import is_valid_event

        assert is_valid_event("XX") is False

    def test_case_insensitive(self):
        from strathmark.config import is_valid_event

        assert is_valid_event("sb") is True
        assert is_valid_event("uh") is True


# ---------------------------------------------------------------------------
# Decay: select_half_life
# ---------------------------------------------------------------------------


class TestSelectHalfLife:
    def test_active(self):
        from strathmark.decay import HALF_LIFE_ACTIVE_DAYS, select_half_life

        assert select_half_life("active") == HALF_LIFE_ACTIVE_DAYS

    def test_moderate(self):
        from strathmark.decay import HALF_LIFE_MODERATE_DAYS, select_half_life

        assert select_half_life("moderate") == HALF_LIFE_MODERATE_DAYS

    def test_inactive(self):
        from strathmark.decay import HALF_LIFE_INACTIVE_DAYS, select_half_life

        assert select_half_life("inactive") == HALF_LIFE_INACTIVE_DAYS

    def test_invalid_raises(self):
        from strathmark.decay import select_half_life

        with pytest.raises(ValueError, match="Unknown activity_level"):
            select_half_life("hyperactive")
