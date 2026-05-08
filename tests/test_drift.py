"""Drift detection tests.

All-offline. The drift module's compute path (`_build_report`) is pure and
testable without any DB access. The DB-touching `evaluate_drift()` wrapper
is covered by an integration test gated behind STRATHMARK_TEST_DB.
"""

from __future__ import annotations

from strathmark.drift import (
    COVERAGE_HIGH_THRESHOLD,
    COVERAGE_LOW_THRESHOLD,
    MEAN_SHIFT_SECONDS_THRESHOLD,
    MIN_RECENT_SAMPLES,
    VARIANCE_RATIO_THRESHOLD,
    DriftReport,
    _build_report,
    _coerce_residual_list,
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
        assert (
            "insufficient" in report.summary().lower()
            or "samples" in report.notes[0].lower()
        )


class TestNominalCase:
    def test_no_alert_when_distributions_match(self):
        # Same five-value pattern for both, so mean and variance match exactly.
        # This is the canonical nominal case: model behavior on recent traffic
        # mirrors what calibration expected.
        pattern = [0.1, -0.05, 0.0, 0.2, -0.1]
        recent = pattern * 10  # n=50
        baseline = pattern * 20  # n=100
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
    def test_coverage_below_low_threshold(self):
        recent = [0.0] * 30
        baseline = [0.0] * 100
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=COVERAGE_LOW_THRESHOLD - 0.01,
        )
        assert report.coverage_alert is True
        assert report.overall_alert is True

    def test_coverage_above_high_threshold(self):
        recent = [0.0] * 30
        baseline = [0.0] * 100
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=COVERAGE_HIGH_THRESHOLD + 0.01,
        )
        assert report.coverage_alert is True
        assert report.overall_alert is True

    def test_coverage_at_target_does_not_trigger(self):
        recent = [0.0] * 30
        baseline = [0.0] * 100
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=recent,
            baseline_residuals=baseline,
            baseline_coverage=0.90,
        )
        assert report.coverage_alert is False

    def test_missing_coverage_does_not_trigger(self):
        report = _build_report(
            model_version_id="01H...",
            lookback_days=30,
            recent_residuals=[0.0] * 30,
            baseline_residuals=[0.0] * 100,
            baseline_coverage=None,
        )
        assert report.coverage_alert is False


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
