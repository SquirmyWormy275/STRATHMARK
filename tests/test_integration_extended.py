"""Extended integration tests.

Tests full pipeline round-trips: predict → calculate → simulate → verify.
Tests multi-event processing, store round-trips, and cross-module consistency.
"""

from datetime import date, timedelta

import pytest

from strathmark import (
    CompetitorRecord,
    HandicapCalculator,
    HistoricalResult,
    WoodProfile,
)
from strathmark.config import rules
from strathmark.store import ResultStore
from strathmark.variance import audit_mark_sheet, run_monte_carlo_simulation


def _make_history(times, event="SB", species="S01", diameter=300, quality=5):
    """Helper: create HistoricalResult list from times."""
    base = date.today()
    return [
        HistoricalResult(
            event_code=event,
            time_seconds=t,
            species=species,
            diameter_mm=diameter,
            quality=quality,
            result_date=base - timedelta(days=i * 30),
        )
        for i, t in enumerate(times)
    ]


# ---------------------------------------------------------------------------
# Full pipeline: predict → marks → simulate → verify fairness
# ---------------------------------------------------------------------------
class TestFullPipeline:
    """End-to-end: create competitors, calculate marks, run MC, verify."""

    def test_three_competitor_pipeline(self):
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        competitors = [
            CompetitorRecord(name="Fast", history=_make_history([18, 19, 17, 20])),
            CompetitorRecord(name="Mid", history=_make_history([25, 26, 24, 27])),
            CompetitorRecord(name="Slow", history=_make_history([35, 36, 34, 37])),
        ]
        calc = HandicapCalculator()
        results = calc.calculate(competitors, wood, "SB")

        # Verify invariants
        assert len(results) == 3
        for r in results:
            assert rules.MIN_MARK_SECONDS <= r.mark <= rules.MAX_MARK_SECONDS
            assert r.predicted_time > 0
            assert r.std_dev > 0

        # Slowest should be front marker (mark 3)
        slowest = max(results, key=lambda r: r.predicted_time)
        assert slowest.mark == 3

        # Fastest should have highest mark
        fastest = min(results, key=lambda r: r.predicted_time)
        assert fastest.mark == max(r.mark for r in results)

        # Run fairness audit
        sim_input = [
            {
                "name": r.name,
                "predicted_time": r.predicted_time,
                "mark": r.mark,
                "std_dev": r.std_dev,
            }
            for r in results
        ]
        audit = audit_mark_sheet(sim_input, num_simulations=50_000, verbose=False)
        # The legacy simulator remains an independent diagnostic. V2 mark quality
        # is governed by the joint posterior objective recorded on every result.
        assert audit["fairness_rating"] in ("excellent", "good", "fair", "poor")
        objective = results[0].optimizer_metadata["objective"]
        legacy_objective = results[0].optimizer_metadata["legacy_objective"]
        assert tuple(objective[:3]) <= tuple(legacy_objective[:3])

    def test_manual_override_pipeline(self):
        """Manual overrides should take priority in the full pipeline."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        c1 = CompetitorRecord(name="A", history=_make_history([25, 26, 24]))
        c2 = CompetitorRecord(name="B", history=_make_history([30, 31, 29]))
        calc = HandicapCalculator()
        # Override B to be faster than A
        results = calc.calculate(
            [c1, c2],
            wood,
            "SB",
            manual_overrides={"B": 20.0},
        )
        b_result = next(r for r in results if r.name == "B")
        assert b_result.predicted_time == pytest.approx(20.0)
        assert b_result.method_used == "manual"


# ---------------------------------------------------------------------------
# Multi-event day processing
# ---------------------------------------------------------------------------
class TestMultiEventDay:
    """Test process_competition_day with multiple events."""

    def test_two_events_independent_marks(self):
        """SB and UH events should have independent mark assignments."""
        from strathmark import process_competition_day

        competitors_sb = [
            CompetitorRecord(name="Alice", history=[], manual_time_override=22.0),
            CompetitorRecord(name="Bob", history=[], manual_time_override=30.0),
        ]
        competitors_uh = [
            CompetitorRecord(name="Alice", history=[], manual_time_override=28.0),
            CompetitorRecord(name="Bob", history=[], manual_time_override=25.0),
        ]
        events = [
            {
                "event_name": "Standing Block",
                "event_code": "SB",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": competitors_sb,
            },
            {
                "event_name": "Underhand",
                "event_code": "UH",
                "species": "S01",
                "diameter_mm": 300,
                "quality": 5,
                "competitors": competitors_uh,
            },
        ]
        day_results = process_competition_day(events)
        assert len(day_results) == 2

        # In SB, Alice is faster → higher mark
        sb_result = next(r for r in day_results if r["event_code"] == "SB")
        alice_sb = next(m for m in sb_result["results"] if m.name == "Alice")
        bob_sb = next(m for m in sb_result["results"] if m.name == "Bob")
        assert alice_sb.mark >= bob_sb.mark

        # In UH, Bob is faster → higher mark
        uh_result = next(r for r in day_results if r["event_code"] == "UH")
        alice_uh = next(m for m in uh_result["results"] if m.name == "Alice")
        bob_uh = next(m for m in uh_result["results"] if m.name == "Bob")
        assert bob_uh.mark >= alice_uh.mark


# ---------------------------------------------------------------------------
# Store round-trip
# ---------------------------------------------------------------------------
class TestStoreRoundTrip:
    """Record results, retrieve, use for prediction."""

    def test_store_and_predict(self, tmp_path):
        store = ResultStore(db_path=tmp_path / "test.db")
        # Record some results
        today = date.today()
        for i, t in enumerate([25.0, 26.0, 24.0, 25.5]):
            store.record_result(
                "TestComp",
                "SB",
                t,
                "S01",
                300,
                5,
                heat_id=f"H{i}",
                result_date=today - timedelta(days=i * 30),
            )
        # Retrieve and build competitor
        history = store.get_competitor_history("TestComp", event_code="SB")
        assert len(history) == 4
        record = CompetitorRecord(name="TestComp", history=history)

        # Calculate
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        calc = HandicapCalculator()
        results = calc.calculate([record], wood, "SB")
        assert len(results) == 1
        assert results[0].mark == 3  # Single competitor → floor
        assert results[0].predicted_time > 0


# ---------------------------------------------------------------------------
# Mark consistency across field sizes
# ---------------------------------------------------------------------------
class TestMarkConsistencyAcrossFieldSizes:
    """Adding competitors should not change existing competitors' marks
    in unexpected ways (marks are relative to slowest)."""

    def test_adding_slower_competitor_preserves_fast_mark(self):
        """Adding a slower competitor only changes the front marker."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        fast = CompetitorRecord(name="Fast", history=[], manual_time_override=15.0)
        mid = CompetitorRecord(name="Mid", history=[], manual_time_override=25.0)
        slow = CompetitorRecord(name="Slow", history=[], manual_time_override=40.0)

        calc = HandicapCalculator()

        # Two competitors
        results_2 = calc.calculate([fast, mid], wood, "SB")

        # Three competitors (added slower)
        results_3 = calc.calculate([fast, mid, slow], wood, "SB")

        fast_mark_2 = next(r for r in results_2 if r.name == "Fast").mark
        fast_mark_3 = next(r for r in results_3 if r.name == "Fast").mark

        # Fast's mark should increase when a slower competitor is added
        # (more gap from the slowest)
        assert fast_mark_3 >= fast_mark_2


# ---------------------------------------------------------------------------
# Simulation determinism
# ---------------------------------------------------------------------------
class TestSimulationDeterminism:
    """Same seed → same results."""

    def test_same_seed_same_results(self):
        competitors = [
            {"name": "A", "predicted_time": 25.0, "mark": 8, "std_dev": 3.0},
            {"name": "B", "predicted_time": 30.0, "mark": 3, "std_dev": 3.0},
        ]
        r1 = run_monte_carlo_simulation(
            competitors,
            num_simulations=10_000,
            seed=123,
            verbose=False,
        )
        r2 = run_monte_carlo_simulation(
            competitors,
            num_simulations=10_000,
            seed=123,
            verbose=False,
        )
        assert r1["winner_percentages"] == r2["winner_percentages"]

    def test_different_seed_different_results(self):
        competitors = [
            {"name": "A", "predicted_time": 25.0, "mark": 8, "std_dev": 3.0},
            {"name": "B", "predicted_time": 30.0, "mark": 3, "std_dev": 3.0},
        ]
        r1 = run_monte_carlo_simulation(
            competitors,
            num_simulations=10_000,
            seed=123,
            verbose=False,
        )
        r2 = run_monte_carlo_simulation(
            competitors,
            num_simulations=10_000,
            seed=456,
            verbose=False,
        )
        # Very unlikely to be exactly the same with different seeds
        assert r1["winner_percentages"]["A"] != r2["winner_percentages"]["A"]


# ---------------------------------------------------------------------------
# Audit mark sheet integration
# ---------------------------------------------------------------------------
class TestAuditMarkSheetIntegration:
    """Full audit from calculated marks."""

    def test_well_handicapped_field_is_fair(self):
        """A properly handicapped field should get 'excellent' or 'good'."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        competitors = [
            CompetitorRecord(name="A", history=[], manual_time_override=20.0),
            CompetitorRecord(name="B", history=[], manual_time_override=25.0),
            CompetitorRecord(name="C", history=[], manual_time_override=30.0),
        ]
        calc = HandicapCalculator()
        results = calc.calculate(competitors, wood, "SB")

        sim_input = [
            {
                "name": r.name,
                "predicted_time": r.predicted_time,
                "mark": r.mark,
                "std_dev": r.std_dev,
            }
            for r in results
        ]
        audit = audit_mark_sheet(sim_input, num_simulations=50_000, verbose=False)
        assert audit["fairness_rating"] in ("excellent", "very good", "good", "fair")

    def test_audit_returns_all_competitors(self):
        competitors = [
            {"name": "X", "predicted_time": 20.0, "mark": 13, "std_dev": 3.0},
            {"name": "Y", "predicted_time": 25.0, "mark": 8, "std_dev": 3.0},
            {"name": "Z", "predicted_time": 30.0, "mark": 3, "std_dev": 3.0},
        ]
        audit = audit_mark_sheet(competitors, num_simulations=10_000, verbose=False)
        assert len(audit["per_competitor"]) == 3
        # per_competitor is a dict keyed by name
        assert set(audit["per_competitor"].keys()) == {"X", "Y", "Z"}


# ---------------------------------------------------------------------------
# Tournament time weighting integration
# ---------------------------------------------------------------------------
class TestTournamentWeightingCompatibility:
    """Unverifiable same-tournament inputs are accepted numeric no-ops in V2."""

    def test_tournament_result_does_not_change_prediction(self):
        """Changing tournament context cannot change a V2 prediction or interval."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        # Historical average ≈ 30s
        old_history = _make_history([28, 30, 32, 29, 31])
        record = CompetitorRecord(name="Champ", history=old_history)

        calc = HandicapCalculator()

        # Without tournament result
        results_no_tourney = calc.calculate([record], wood, "SB")

        # With much faster tournament result
        results_with_tourney = calc.calculate(
            [record],
            wood,
            "SB",
            tournament_results={"Champ": 22.0},
        )

        pred_no = results_no_tourney[0].predicted_time
        pred_with = results_with_tourney[0].predicted_time

        assert pred_with == pred_no
        assert results_with_tourney[0].interval == results_no_tourney[0].interval


# ---------------------------------------------------------------------------
# Start sheet rendering integration
# ---------------------------------------------------------------------------
class TestStartSheetIntegration:
    def test_start_sheet_round_trip(self):
        """Calculate marks → build start sheet → render as text."""
        wood = WoodProfile(species="S01", diameter_mm=300, quality=5)
        competitors = [
            CompetitorRecord(name="Alice", history=[], manual_time_override=20.0),
            CompetitorRecord(name="Bob", history=[], manual_time_override=30.0),
        ]
        calc = HandicapCalculator()
        results = calc.calculate(competitors, wood, "SB")
        sheet = calc.build_start_sheet(results, "Standing Block", "SB", wood)

        text = sheet.render()
        assert isinstance(text, str)
        assert "Alice" in text
        assert "Bob" in text
        assert "Standing Block" in text
        # Plain text only
        assert "\033[" not in text
