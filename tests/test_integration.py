"""
Integration tests — full pipeline from Excel workbook to mark sheet.

Loads real data from woodchopping_clean.xlsx (must exist in the project root),
picks SB competitors, runs the full prediction cascade, and asserts invariants
on the resulting marks and the Monte Carlo fairness audit.

These tests require the workbook to be present and will be skipped otherwise.
They deliberately avoid mocking so that end-to-end regressions surface early.
"""

import os
import pytest

WORKBOOK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "woodchopping_clean.xlsx"
)


@pytest.fixture(scope="module")
def loaded_data():
    """Load wood and results DataFrames once for all tests in this module."""
    if not os.path.exists(WORKBOOK_PATH):
        pytest.skip("woodchopping_clean.xlsx not found — skipping integration tests")

    from strathmark.utils import load_woodchopping_xlsx
    wood_df, results_df = load_woodchopping_xlsx(WORKBOOK_PATH)
    return wood_df, results_df


@pytest.fixture(scope="module")
def six_sb_competitors(loaded_data):
    """
    Build 6 CompetitorRecord objects from the first 6 unique SB competitors
    found in the results DataFrame.
    """
    from strathmark.utils import standardize_results_columns
    from strathmark.predictor import CompetitorRecord, HistoricalResult

    _, results_df = loaded_data
    df = standardize_results_columns(results_df)

    sb_df = df[df['event'].str.upper() == 'SB'].dropna(subset=['raw_time'])
    sb_df = sb_df[sb_df['raw_time'] > 0]

    unique_names = sb_df['competitor_name'].dropna().unique()
    if len(unique_names) < 2:
        pytest.skip("Fewer than 2 SB competitors found in workbook")

    names = list(unique_names[:6])
    competitors = []
    for name in names:
        rows = sb_df[sb_df['competitor_name'] == name]
        history = []
        for _, row in rows.iterrows():
            try:
                history.append(
                    HistoricalResult(
                        event=str(row.get('event', 'SB')).upper(),
                        time_seconds=float(row['raw_time']),
                        species=str(row.get('species', 'Pine')),
                        diameter_mm=float(row.get('size_mm', 300)),
                        quality=int(row.get('quality', 5)),
                        result_date=row.get('result_date'),
                    )
                )
            except (TypeError, ValueError):
                continue

        competitors.append(
            CompetitorRecord(name=name, history=history)
        )

    if not competitors:
        pytest.skip("Could not build any CompetitorRecord from workbook data")

    return competitors


@pytest.fixture(scope="module")
def mark_results(loaded_data, six_sb_competitors):
    """Run the full calculate() pipeline and return the MarkResult list."""
    from strathmark.calculator import HandicapCalculator
    from strathmark.predictor import WoodProfile

    wood_df, results_df = loaded_data
    calc = HandicapCalculator(wood_df=wood_df, results_df=results_df)
    wood = WoodProfile(species="Pine", diameter_mm=300, quality=5)
    return calc.calculate(six_sb_competitors, wood, "SB")


# ---------------------------------------------------------------------------
# Mark invariants
# ---------------------------------------------------------------------------

class TestIntegrationMarkInvariants:

    def test_all_marks_at_least_3(self, mark_results):
        for r in mark_results:
            assert r.mark >= 3, f"{r.name} mark {r.mark} is below floor"

    def test_all_marks_at_most_180(self, mark_results):
        """Event ceiling for real competitions is typically 180s."""
        for r in mark_results:
            assert r.mark <= 180, f"{r.name} mark {r.mark} exceeds 180"

    def test_slowest_gets_mark_3(self, mark_results):
        """The first entry in the sorted list (slowest predicted time) gets mark 3."""
        sorted_results = sorted(mark_results, key=lambda r: r.predicted_time, reverse=True)
        assert sorted_results[0].mark == 3

    def test_all_marks_are_integers(self, mark_results):
        for r in mark_results:
            assert isinstance(r.mark, int), f"{r.name}.mark is not int: {type(r.mark)}"

    def test_marks_sorted_ascending(self, mark_results):
        """calculate() output must be sorted from front marker (mark 3) to back marker."""
        marks = [r.mark for r in mark_results]
        assert marks == sorted(marks), f"Marks not sorted ascending: {marks}"


# ---------------------------------------------------------------------------
# Monte Carlo fairness audit
# ---------------------------------------------------------------------------

class TestIntegrationFairnessAudit:

    def test_fairness_not_poor(self, mark_results):
        """
        The mark sheet must not be rated 'poor' by the fairness audit.
        A poor rating indicates systematic bias (>20% win-rate spread).
        """
        from strathmark.variance import audit_mark_sheet

        competitors_with_marks = [
            {
                'name': r.name,
                'predicted_time': r.predicted_time,
                'mark': r.mark,
                'std_dev': r.std_dev,
            }
            for r in mark_results
        ]

        audit = audit_mark_sheet(competitors_with_marks, num_simulations=50_000, verbose=False)

        assert audit['fairness_rating'] != 'poor', (
            f"Mark sheet rated 'poor'. Win-rate spread: {audit['win_rate_spread']:.1f}%. "
            f"Per-competitor win rates: "
            + ", ".join(
                f"{name}: {d['win_rate']:.1f}%"
                for name, d in audit['per_competitor'].items()
            )
        )

    def test_audit_returns_required_keys(self, mark_results):
        """audit_mark_sheet() must return all required keys."""
        from strathmark.variance import audit_mark_sheet

        competitors_with_marks = [
            {'name': r.name, 'predicted_time': r.predicted_time, 'mark': r.mark}
            for r in mark_results
        ]
        audit = audit_mark_sheet(competitors_with_marks, num_simulations=10_000, verbose=False)

        required_keys = {
            'per_competitor', 'front_marker_win_rate', 'back_marker_win_rate',
            'win_rate_spread', 'fairness_rating',
        }
        assert required_keys.issubset(set(audit.keys()))
        assert audit['fairness_rating'] in ('excellent', 'good', 'fair', 'poor')
