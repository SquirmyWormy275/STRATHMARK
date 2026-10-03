from copy import deepcopy

import pytest

from strathmark.v3.contracts.forecasts import DISTRIBUTION_SCHEMA_VERSION
from strathmark.v3.factory.accuracy_audit import (
    compare_rows,
    freeze_protocol,
    triage_rows,
    verify_prospective,
)


def rows(n=30):
    return [
        {
            "row_id": f"evidence:synthetic-{i}",
            "tournament_id": f"tournament:synthetic-{i // 3}",
            "event": "underhand",
            "species": "synthetic",
            "size_mm": 300,
            "history_depth": i % 20,
            "same_event_history_depth": i % 20,
            "same_material_history_depth": i % 10,
            "actual_seconds": 30.0,
            "pool_seconds": 35.0,
            "occurred_at_utc": "2026-10-04T12:00:00.000Z",
            "pool": {
                "schema_version": DISTRIBUTION_SCHEMA_VERSION,
                "quantiles": [
                    {"probability": p, "time_ms": ms}
                    for p, ms in (
                        ("0.05", 20000),
                        ("0.25", 25000),
                        ("0.5", 35000),
                        ("0.75", 40000),
                        ("0.95", 50000),
                    )
                ],
            },
        }
        for i in range(n)
    ]


def test_triage_keeps_extreme_results_and_sparse_contexts():
    data = rows()
    data[0]["actual_seconds"] = 300
    report = triage_rows(data)
    assert report["overall"]["row_count"] == 30
    assert report["tail"]["worst_decile_count"] == 3
    assert report["overall"]["maximum_absolute_error_seconds"] == 265
    assert "no_event_history" in report["slices"]["relevant_history"]


@pytest.mark.parametrize("change", ["drop", "target", "context", "duplicate", "nonfinite"])
def test_comparison_rejects_changed_cohort_or_invalid_measurements(change):
    baseline = rows()
    candidate = deepcopy(baseline)
    if change == "drop":
        candidate.pop()
    elif change == "target":
        candidate[0]["actual_seconds"] = 31
    elif change == "context":
        candidate[0]["species"] = "changed"
    elif change == "duplicate":
        candidate.append(deepcopy(candidate[0]))
    else:
        candidate[0]["pool_seconds"] = float("nan")
    with pytest.raises(ValueError):
        compare_rows(baseline, candidate)


def test_gate_blocks_large_tail_regression_despite_better_average():
    baseline = rows(100)
    candidate = deepcopy(baseline)
    for r in candidate:
        r["pool_seconds"] = 30
        r["pool"]["quantiles"][2]["time_ms"] = 30000
    candidate[0]["pool_seconds"] = 200
    candidate[0]["pool"]["quantiles"] = [
        {"probability": p, "time_ms": ms}
        for p, ms in (
            ("0.05", 20000),
            ("0.25", 100000),
            ("0.5", 200000),
            ("0.75", 250000),
            ("0.95", 300000),
        )
    ]
    report = compare_rows(baseline, candidate)
    assert report["candidate"]["overall"]["mean_absolute_error_seconds"] < 5
    assert report["accepted"] is False
    assert any(v["metric"] == "maximum_absolute_error_seconds" for v in report["violations"])


def test_gate_does_not_trust_supplied_coverage_or_summary():
    data = rows()
    for r in data:
        r["pool_coverage90"] = True
        r["pool"]["quantiles"] = [
            {"probability": p, "time_ms": ms}
            for p, ms in (
                ("0.05", 31000),
                ("0.25", 33000),
                ("0.5", 35000),
                ("0.75", 36000),
                ("0.95", 37000),
            )
        ]
    report = compare_rows(data, data)
    assert report["candidate"]["overall"]["interval_90_coverage"] == 0
    assert report["accepted"] is False


def test_prospective_gate_binds_model_and_refuses_examined_or_early_rows():
    now = "2026-10-03T12:00:00.000Z"
    summary = {
        "bundle_digest": "a" * 64,
        "formula_digest": "b" * 64,
        "source_implementation_digest": "c" * 64,
    }
    old = rows()
    for r in old:
        r["occurred_at_utc"] = "2025-01-01T12:00:00.000Z"
    protocol = freeze_protocol(summary, old, now=now)
    future = rows()
    for i, r in enumerate(future):
        r["row_id"] = f"evidence:future-{i}"
        r["tournament_id"] = f"tournament:future-{i // 3}"
    assert (
        verify_prospective(protocol, summary, future)["status"]
        == "eligible_for_frozen_model_evaluation"
    )
    with pytest.raises(ValueError, match="previously examined"):
        verify_prospective(protocol, summary, old)
    altered = {**summary, "bundle_digest": "d" * 64}
    with pytest.raises(ValueError, match="frozen component"):
        verify_prospective(protocol, altered, future)
    for r in future:
        r["occurred_at_utc"] = "2026-10-02T12:00:00.000Z"
    with pytest.raises(ValueError, match="after protocol"):
        verify_prospective(protocol, summary, future)


def test_protocol_cannot_be_modified_after_freeze():
    summary = {
        "bundle_digest": "a" * 64,
        "formula_digest": "b" * 64,
        "source_implementation_digest": "c" * 64,
    }
    protocol = freeze_protocol(summary, rows(), now="2026-10-05T12:00:00.000Z")
    protocol["not_before_utc"] = "2020-01-01T00:00:00.000Z"
    with pytest.raises(ValueError, match="protocol digest"):
        verify_prospective(protocol, summary, rows())


def test_protocol_cannot_be_backdated_before_examined_results():
    summary = {
        "bundle_digest": "a" * 64,
        "formula_digest": "b" * 64,
        "source_implementation_digest": "c" * 64,
    }
    with pytest.raises(ValueError, match="precede examined"):
        freeze_protocol(summary, rows(), now="2026-10-03T12:00:00.000Z")
