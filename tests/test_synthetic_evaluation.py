from strathmark.events.evaluation import synthetic_evaluation


def test_synthetic_report_retains_unmatched_hard_cases_and_intervals(tmp_path):
    report = synthetic_evaluation(tmp_path / "report")
    assert report["counts"]["eligible"] == report["overall"]["sample_count"] == 8
    assert report["matched_baseline"]["count"] == 4
    assert (
        report["matched_baseline"]["engine"]["sample_count"]
        == report["matched_baseline"]["last_exact_context_cut"]["sample_count"]
    )
    assert report["overall"]["mean_interval_width_seconds"] > 0
    assert all(r["model_version"] for r in report["rows"])
    assert report["prospective"]["minimum_later_results"] == 100
    assert report["prospective"]["minimum_new_competitions"] == 10
    assert report["historical_replay"]["status"] == "NOT_RUN"
    assert (tmp_path / "report" / "report.txt").is_file()
