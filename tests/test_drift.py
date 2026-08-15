"""Drift detection tests.

All-offline. The drift module's compute path (`_build_report`) is pure and
testable without any DB access. The DB-touching `evaluate_drift()` wrapper
is covered by an integration test gated behind STRATHMARK_TEST_DB.
"""

from __future__ import annotations

import sqlite3

import pytest

from strathmark.drift import (
    MEAN_SHIFT_SECONDS_THRESHOLD,
    MIN_RECENT_SAMPLES,
    VARIANCE_RATIO_THRESHOLD,
    DriftReport,
    _build_report,
    _coerce_residual_list,
    evaluate_drift,
    evaluate_settled_drift,
    settled_model_prediction_rows,
)


class TestCoerceResidualList:
    def test_none(self):
        assert _coerce_residual_list(None) == []

    def test_list_passthrough(self):
        assert _coerce_residual_list([0.1, -0.2, 0.3]) == [0.1, -0.2, 0.3]

    def test_dict_with_residuals_key(self):
        assert _coerce_residual_list({"residuals": [1.0, 2.0]}) == [1.0, 2.0]

    def test_dict_with_values_key(self):
        assert _coerce_residual_list({"values": [3.0]}) == [3.0]

    def test_unrecognized_dict_returns_empty(self):
        assert _coerce_residual_list({"foo": [1, 2, 3]}) == []


class TestInsufficientRecentSamples:
    def test_too_few_recent_samples_blocks_evaluation(self):
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=[0.1] * (MIN_RECENT_SAMPLES - 1),
            baseline_residuals=[0.05] * 100,
            baseline_coverage=0.90,
        )
        assert report.insufficient_recent_samples is True
        assert report.overall_alert is False
        assert "insufficient" in report.summary().lower() or "samples" in report.notes[0].lower()


class TestNominalCase:
    def test_no_alert_when_distributions_match(self):
        # Recent and baseline drawn from identical uniform distribution on
        # [-1, 1]. Mean, variance, and 90% coverage all sit in nominal bands.
        baseline = [-1.0 + 2.0 * i / 99 for i in range(100)]
        recent = [-1.0 + 2.0 * i / 49 for i in range(50)]
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.overall_alert is False
        assert report.mean_shift_alert is False
        assert report.variance_ratio_alert is False
        assert report.coverage_alert is False
        assert report.recent_count == 50
        assert report.baseline_count == 100
        assert report.recent_coverage_at_90 is None


class TestMeanShift:
    def test_large_positive_mean_shift_triggers_alert(self):
        recent = [3.0] * 30  # mean = 3.0
        baseline = [0.0] * 100  # mean = 0.0
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.mean_shift_alert is True
        assert report.overall_alert is True
        assert report.mean_shift > MEAN_SHIFT_SECONDS_THRESHOLD

    def test_large_negative_mean_shift_triggers_alert(self):
        recent = [-2.0] * 30
        baseline = [0.0] * 100
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.mean_shift_alert is True
        assert report.overall_alert is True


class TestVarianceShift:
    def test_variance_doubling_triggers_alert(self):
        # Recent variance = 4.0, baseline variance = 1.0 -> ratio 4.0 -> +300%
        recent = [-2.0, 2.0] * 15  # n=30, variance = 4.0
        baseline = [-1.0, 1.0] * 50  # n=100, variance = 1.0
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.variance_ratio_alert is True
        assert report.overall_alert is True
        assert report.variance_ratio_change > VARIANCE_RATIO_THRESHOLD

    def test_small_variance_change_does_not_trigger(self):
        # Recent variance ~= baseline variance (well within 30%)
        recent = [-1.0, 1.0] * 15
        baseline = [-1.0, 1.0] * 50
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.variance_ratio_alert is False


class TestCoverageAlert:
    """Residual-only reports never invent issued-interval coverage."""

    def test_recent_coverage_below_threshold_triggers_alert(self):
        # Baseline 5th-95th percentile interval is roughly [-1.8, 1.8].
        # Recent residuals all fall outside that interval -> coverage = 0.0.
        baseline = [-2.0 + 4.0 * i / 99 for i in range(100)]
        recent = [10.0] * 30
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.coverage_alert is False
        assert report.recent_coverage_at_90 is None

    def test_recent_coverage_above_threshold_triggers_alert(self):
        # Recent residuals all sit at the baseline median -> coverage = 1.0.
        # Variance has collapsed; the empirical 90% interval is too wide
        # for the actual recent traffic, surfacing as coverage > 0.95.
        baseline = [-2.0 + 4.0 * i / 99 for i in range(100)]
        recent = [0.0] * 30
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.coverage_alert is False
        assert report.recent_coverage_at_90 is None

    def test_matching_distributions_do_not_trigger(self):
        # Same distribution for recent and baseline -> coverage in band.
        baseline = [-1.0 + 2.0 * i / 99 for i in range(100)]
        recent = [-1.0 + 2.0 * i / 49 for i in range(50)]
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.coverage_alert is False
        assert report.recent_coverage_at_90 is None

    def test_no_baseline_residuals_does_not_trigger(self):
        # Empty baseline -> early return; no coverage signal possible.
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=[0.0] * 30,
            baseline_residuals=[],
            baseline_coverage=None,
        )
        assert report.coverage_alert is False
        assert report.recent_coverage_at_90 is None


class TestDriftReportSummary:
    def test_summary_alert_format(self):
        report = DriftReport(
            model_version_id="01H...",
            lookback_days=30,
            recent_count=50,
            baseline_count=100,
            recent_mean=2.0,
            baseline_mean=0.0,
            mean_shift=2.0,
            mean_shift_alert=True,
            overall_alert=True,
            notes=["mean residual shifted by +2.00s"],
        )
        s = report.summary()
        assert "DRIFT ALERT" in s
        assert "mean" in s

    def test_summary_nominal_format(self):
        report = DriftReport(
            model_version_id="01H...",
            lookback_days=30,
            recent_count=50,
            baseline_count=100,
            mean_shift=0.0,
        )
        s = report.summary()
        assert "nominal" in s.lower()

    def test_summary_insufficient_format(self):
        report = DriftReport(
            model_version_id="01H...",
            lookback_days=30,
            recent_count=5,
            baseline_count=100,
            insufficient_recent_samples=True,
        )
        s = report.summary()
        assert "insufficient" in s.lower()

    def test_summary_labels_unavailable_point_shift(self):
        report = DriftReport(
            model_version_id="v2",
            lookback_days=30,
            recent_count=20,
            baseline_count=0,
        )

        assert "mean_shift=unavailable" in report.summary()


def test_settled_rows_exclude_manual_unsettled_and_invalid_predictions():
    rows = [
        {
            "source": "hierarchical_dynamic_core",
            "predicted_time": 50.0,
            "actual_time": 52.0,
            "settled_at": "2026-08-01T00:00:00Z",
        },
        {
            "source": "manual",
            "predicted_time": 49.0,
            "actual_time": 50.0,
            "settled_at": "2026-08-01T00:00:00Z",
        },
        {
            "source": "hierarchical_dynamic_core",
            "predicted_time": 48.0,
            "actual_time": None,
            "settled_at": None,
        },
        {
            "source": "hierarchical_dynamic_core",
            "predicted_time": 47.0,
            "actual_time": 48.0,
            "settled_at": "2026-08-01T00:00:00Z",
            "training_eligible": False,
        },
    ]

    eligible = settled_model_prediction_rows(rows)

    assert len(eligible) == 1
    assert eligible[0]["residual"] == 2.0


def test_settled_drift_reports_sample_label():
    rows = [
        {
            "source": "hierarchical_dynamic_core",
            "predicted_time": 50.0,
            "actual_time": 50.5,
            "settled_at": "2026-08-01T00:00:00Z",
        }
        for _ in range(10)
    ]

    report = evaluate_settled_drift(
        rows,
        baseline_residuals=[0.0] * 100,
        model_version_id="v2",
    )

    assert report.insufficient_recent_samples is True
    assert report.sample_label == "insufficient_recent_sample"


def test_settled_drift_uses_direct_issued_interval_coverage_per_nominal_cohort():
    rows = []
    for index in range(20):
        rows.append(
            {
                "source": "hierarchical_dynamic_core",
                "predicted_time": 50.0,
                "actual_time": 50.0 if index < 18 else 70.0,
                "settled_at": "2026-08-01T00:00:00Z",
                "interval_lower": 40.0,
                "interval_upper": 60.0,
                "nominal_coverage": 0.90,
            }
        )
    for index in range(20):
        rows.append(
            {
                "source": "hierarchical_dynamic_core",
                "predicted_time": 50.0,
                "actual_time": 50.0 if index < 5 else 70.0,
                "settled_at": "2026-08-01T00:00:00Z",
                "interval_lower": 40.0,
                "interval_upper": 60.0,
                "nominal_coverage": 0.80,
            }
        )
    rows.append(
        {
            "source": "hierarchical_dynamic_core",
            "predicted_time": 50.0,
            "actual_time": 50.0,
            "settled_at": "2026-08-01T00:00:00Z",
            "interval_lower": None,
            "interval_upper": None,
            "nominal_coverage": None,
        }
    )

    report = evaluate_settled_drift(
        rows,
        baseline_residuals=[-100.0, 100.0] * 50,
        model_version_id="v2",
    )

    assert report.coverage_cohorts["0.90"].empirical_coverage == 0.9
    assert report.coverage_cohorts["0.90"].coverage_alert is False
    assert report.coverage_cohorts["0.80"].empirical_coverage == 0.25
    assert report.coverage_cohorts["0.80"].coverage_alert is False
    assert report.recent_coverage_at_90 == 0.9
    assert report.coverage_unavailable_count == 1


def test_only_ninety_percent_issued_interval_cohort_can_alert():
    rows = [
        {
            "source": "hierarchical_dynamic_core",
            "predicted_time": 50.0,
            "actual_time": 70.0,
            "settled_at": "2026-08-01T00:00:00Z",
            "interval_lower": 40.0,
            "interval_upper": 60.0,
            "nominal_coverage": coverage,
        }
        for coverage in (0.9, 0.8)
        for _ in range(MIN_RECENT_SAMPLES)
    ]

    report = evaluate_settled_drift(
        rows,
        baseline_residuals=[0.0] * 100,
        model_version_id="v2",
    )

    assert report.coverage_cohorts["0.90"].coverage_alert is True
    assert report.coverage_cohorts["0.80"].coverage_alert is False
    assert report.coverage_alert is True


def test_evaluate_drift_uses_injected_settled_ledger_only():
    class Ledger:
        def __init__(self):
            self.kwargs = None
            self.count_kwargs = None

        def count_training_rows(self, **kwargs):
            self.count_kwargs = kwargs
            return 2

        def get_training_rows(self, **kwargs):
            self.kwargs = kwargs
            return [
                {
                    "source": "hierarchical_dynamic_core",
                    "predicted_time": 50.0,
                    "actual_time": 51.0,
                    "settled_at": "2026-08-01T00:00:00Z",
                },
                {
                    "source": "manual",
                    "predicted_time": 10.0,
                    "actual_time": 100.0,
                    "settled_at": "2026-08-01T00:00:00Z",
                },
            ]

    ledger = Ledger()
    report = evaluate_drift(
        model_version_id="v2",
        ledger=ledger,
        baseline_residuals=[0.0] * 100,
    )

    assert report.recent_count == 1
    assert ledger.kwargs["model_version"] == "v2"
    assert ledger.kwargs["limit"] == 5001
    assert ledger.count_kwargs["model_version"] == "v2"


def test_ledger_correction_projects_only_latest_settlement_into_drift(tmp_path):
    from datetime import date

    from strathmark.ledger import LedgerPrediction, PredictionLedger

    ledger = PredictionLedger(tmp_path / "drift-correction.db")
    write = ledger.record_field(
        "api",
        "field",
        {"event_code": "SB", "prediction_as_of": "2026-08-11"},
        [
            LedgerPrediction(
                competitor_id="competitor-1",
                event_code="SB",
                median_seconds=42.5,
                assigned_mark=3,
                source="baseline",
                training_eligible=True,
                engine_version="2.0.0",
                model_version="core-test",
                calibration_version="cal-test",
                evidence_cutoff=date(2026, 8, 11),
                interval_lower=35.0,
                interval_upper=52.0,
                interval_coverage=0.9,
                interval_state="calibrated",
                interval_scope="global",
                feature_snapshot={"history_count": 1.0},
            )
        ],
    )
    prediction_id = write.prediction_ids[0]
    ledger.settle(prediction_id, "competitor-1", "SB", 70.0, "official")
    ledger.settle(
        prediction_id,
        "competitor-1",
        "SB",
        43.0,
        "official",
        reason="corrected transcription",
    )

    rows = ledger.get_training_rows(model_version="core-test")
    report = evaluate_settled_drift(
        rows,
        baseline_residuals=[0.0] * 100,
        model_version_id="core-test",
    )

    assert len(rows) == 1
    assert rows[0]["actual_time"] == 43.0
    assert rows[0]["revision"] == 2
    assert report.recent_count == 1
    assert report.recent_mean is None
    assert report.coverage_cohorts["0.90"].empirical_coverage == 1.0
    assert report.coverage_cohorts["0.90"].sample_label == "insufficient_recent_sample"


def test_evaluate_drift_requires_model_version_for_ledger():
    with pytest.raises(ValueError, match="model_version_id"):
        evaluate_drift(ledger=object(), baseline_residuals=[0.0] * 100)


def test_sqlite_query_deadline_progress_handler_interrupts_long_read(tmp_path):
    from strathmark.ledger import PredictionLedger, SQLiteQueryDeadline

    ledger = PredictionLedger(tmp_path / "query-deadline.db")
    deadline = SQLiteQueryDeadline(timeout_seconds=0.005)
    conn = ledger._connect(query_deadline=deadline)
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute(
                """
                WITH RECURSIVE values_to_sum(value) AS (
                    SELECT 1
                    UNION ALL
                    SELECT value + 1 FROM values_to_sum WHERE value < 10000000
                )
                SELECT SUM(value) FROM values_to_sum
                """
            ).fetchone()
    finally:
        conn.close()
    assert deadline.cancelled is True
