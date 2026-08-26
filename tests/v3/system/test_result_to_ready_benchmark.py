from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from scripts import benchmark_v3_result_to_ready as benchmark


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value), set())
    return set()


def test_measured_result_to_ready_pipeline_is_digest_bound_and_within_budget(
    tmp_path: Path,
) -> None:
    manifest = benchmark.run_benchmark(tmp_path / "measured", repetitions=1)

    assert manifest["schema_version"] == "strathmark-v3-result-to-ready-benchmark-v1"
    assert manifest["status"] == "passed"
    assert manifest["gates"] == {
        "formal_repetition_count": False,
        "result_to_ready_within_budget": True,
        "all_trials_completed": True,
        "exact_source_bindings": True,
    }
    assert manifest["limits"]["result_to_ready_ms_inclusive"] == 120_000
    trial = manifest["trials"][0]
    assert trial["measured_result_to_ready_ms"] > 0
    assert trial["measured_result_to_ready_ms"] <= 120_000
    assert tuple(trial["component_latency_ms"]) == benchmark.MEASURED_COMPONENTS
    assert sum(trial["component_latency_ms"].values()) <= trial["measured_result_to_ready_ms"]
    assert trial["newly_affected_card_count"] == 2
    assert len(trial["newly_affected_card_publication_digests"]) == 2
    assert trial["optimizer_receipt_digest"]
    assert trial["field_receipt_digest"]
    assert trial["approval_snapshot_id"].startswith("approval_snapshot:")
    assert "ready_ms" not in _keys(manifest)
    benchmark.verify_benchmark_manifest(manifest)


def test_measured_budget_and_manifest_tamper_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark, "RESULT_TO_READY_BUDGET_MS", 0)
    manifest = benchmark.run_benchmark(tmp_path / "over-budget", repetitions=1)
    assert manifest["status"] == "failed"
    assert manifest["gates"]["result_to_ready_within_budget"] is False

    tampered = deepcopy(manifest)
    tampered["trials"][0]["component_latency_ms"]["approval_projection"] += 1
    with pytest.raises(ValueError, match="digest"):
        benchmark.verify_benchmark_manifest(tampered)
