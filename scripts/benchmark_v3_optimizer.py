"""Generate the signed-input Windows capacity evidence for the V3 optimizer."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.contracts.identifiers import StableIdentifier
from strathmark.v3.domain.optimizer import (
    DEFAULT_OPTIMIZER_POLICY,
    NUMPY_DEPENDENCY_VERSION,
    OptimizationCompetitor,
    OptimizationField,
    _source_sha256,
    implementation_artifact_digest,
    optimize_field,
)

REPETITIONS = 100
OPTIMIZER_P99_LIMIT_MS = 1_500
RSS_DELTA_LIMIT_MIB = 256
FIELD_ASSEMBLY_P99_LIMIT_MS = 2_000
ROOT = Path(__file__).resolve().parents[1]
FIELD_ASSEMBLY_MANIFEST_PATH = ROOT / "benchmarks" / "v3" / "field_assembly_manifest.json"


@dataclass(frozen=True, slots=True)
class Fixture:
    name: str
    expected_times_ms: tuple[int, ...]
    source_receipt_digest: str
    ceiling: int

    def field(self) -> OptimizationField:
        rows = []
        for competitor_index, median in enumerate(self.expected_times_ms):
            samples = tuple(
                max(1, median + ((draw * (17 + competitor_index * 2)) % 1001) - 500)
                for draw in range(DEFAULT_OPTIMIZER_POLICY.sample_count)
            )
            rows.append(
                OptimizationCompetitor(
                    StableIdentifier(f"competitor:{competitor_index}"),
                    median,
                    samples,
                    competitor_index,
                )
            )
        return OptimizationField.create(
            field_id=StableIdentifier("field:properties"),
            source_receipt_digest=self.source_receipt_digest,
            competitors=tuple(rows),
        )


FIXTURES = (
    Fixture(
        "six_entrant_worst_radius_exhaustive",
        (61_000, 53_000, 46_000, 39_000, 32_000, 25_000),
        "1" * 64,
        80,
    ),
    Fixture(
        "twelve_entrant_realistic_beam",
        tuple(100_000 - index * 3_500 for index in range(12)),
        "2" * 64,
        90,
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--field-assembly-manifest",
        type=Path,
        default=FIELD_ASSEMBLY_MANIFEST_PATH,
    )
    arguments = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("the V3 optimizer capacity authority is the Windows host")

    field_authority = _load_field_assembly_authority(arguments.field_assembly_manifest)
    fixture_reports = [_measure_fixture(item) for item in FIXTURES]
    optimizer_passed = all(
        item["observed_p99_ms"] < OPTIMIZER_P99_LIMIT_MS
        and item["peak_rss_delta_mib"] < RSS_DELTA_LIMIT_MIB
        for item in fixture_reports
    )
    passed = optimizer_passed and field_authority["passed"]
    body = {
        "schema_version": "strathmark-v3-optimizer-manifest-v2",
        "status": "passed" if passed else "failed",
        "measured_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "algorithm": DEFAULT_OPTIMIZER_POLICY.version,
        "implementation_artifact_digest": implementation_artifact_digest(),
        "source_sha256": _source_sha256(),
        "numpy_version": NUMPY_DEPENDENCY_VERSION,
        "policy_digest": DEFAULT_OPTIMIZER_POLICY.digest,
        "policy": DEFAULT_OPTIMIZER_POLICY.to_dict(),
        "environment": _environment(),
        "capacity_gate": {
            "passed": passed,
            "optimizer_passed": optimizer_passed,
            "field_assembly_passed": field_authority["passed"],
            "required_repetitions": REPETITIONS,
            "optimizer_p99_limit_ms": OPTIMIZER_P99_LIMIT_MS,
            "rss_delta_limit_mib": RSS_DELTA_LIMIT_MIB,
            "field_assembly_p99_limit_ms": FIELD_ASSEMBLY_P99_LIMIT_MS,
            "field_assembly_p99_ms": field_authority["p99_ms"],
            "field_assembly_manifest_digest": field_authority["manifest_digest"],
            "field_assembly_artifact_identity_digest": field_authority["artifact_identity_digest"],
        },
        "windows_capacity_fixtures": fixture_reports,
        "release_blocker": None if passed else _release_blocker(optimizer_passed, field_authority),
    }
    manifest = {
        "schema_version": body.pop("schema_version"),
        "manifest_digest": canonical_digest(
            {"schema_version": "strathmark-v3-optimizer-manifest-v2", **body}
        ),
        **body,
    }
    encoded = json.dumps(manifest, indent=2) + "\n"
    if arguments.output is None:
        sys.stdout.write(encoded)
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8", newline="\n")
        print(arguments.output.resolve())
    return 0 if passed else 1


def _load_field_assembly_authority(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid field-assembly capacity manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SystemExit("field-assembly capacity manifest must be an object")
    body = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    if manifest.get("schema_version") != "strathmark-v3-field-assembly-benchmark-v2":
        raise SystemExit("unsupported field-assembly capacity manifest schema")
    if manifest.get("manifest_digest") != canonical_digest(body):
        raise SystemExit("field-assembly capacity manifest digest mismatch")

    expected_identity = _current_field_artifact_identity()
    if manifest.get("artifact_identity") != expected_identity:
        raise SystemExit("field-assembly capacity manifest does not bind current sources")
    gates = manifest.get("gates")
    assembly = manifest.get("complete_confirmed_field_assembly")
    if not isinstance(gates, dict) or not isinstance(assembly, dict):
        raise SystemExit("field-assembly capacity manifest is incomplete")
    p99_ms = assembly.get("observed_p99_ms")
    formal = (
        manifest.get("status") == "passed"
        and bool(gates)
        and all(value is True for value in gates.values())
        and assembly.get("runs") == REPETITIONS
        and assembly.get("failed_runs") == 0
        and isinstance(p99_ms, (int, float))
        and not isinstance(p99_ms, bool)
        and p99_ms < FIELD_ASSEMBLY_P99_LIMIT_MS
    )
    return {
        "passed": formal,
        "p99_ms": p99_ms,
        "manifest_digest": manifest["manifest_digest"],
        "artifact_identity_digest": canonical_digest(expected_identity),
    }


def _current_field_artifact_identity() -> dict[str, str]:
    paths = {
        "benchmark_script_sha256": ROOT / "scripts" / "benchmark_v3_field_assembly.py",
        "fixture_source_sha256": ROOT / "tests" / "v3" / "integration" / "test_field_receipts.py",
        "field_assembly_source_sha256": ROOT
        / "strathmark"
        / "v3"
        / "application"
        / "field_assembly.py",
        "projection_source_sha256": ROOT
        / "strathmark"
        / "v3"
        / "infrastructure"
        / "sqlite"
        / "projections.py",
        "joint_dependence_source_sha256": ROOT
        / "strathmark"
        / "v3"
        / "domain"
        / "joint_dependence.py",
        "optimizer_source_sha256": ROOT / "strathmark" / "v3" / "domain" / "optimizer.py",
        "native_kernel_source_sha256": ROOT
        / "strathmark"
        / "v3"
        / "native"
        / "optimizer_kernel.rs",
        "native_kernel_binary_sha256": ROOT
        / "strathmark"
        / "v3"
        / "native"
        / "strathmark_v3_optimizer_kernel.dll",
    }
    return {name: sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


def _release_blocker(optimizer_passed: bool, field_authority: dict[str, Any]) -> str:
    blockers = []
    if not optimizer_passed:
        blockers.append("optimizer latency or memory capacity failed")
    if not field_authority["passed"]:
        blockers.append("formal current-source field-assembly capacity failed")
    return "; ".join(blockers)


def _measure_fixture(fixture: Fixture) -> dict[str, Any]:
    field = fixture.field()
    rss_before = _process_rss_bytes()
    stop = threading.Event()
    peak = [rss_before]

    def sample_rss() -> None:
        while not stop.wait(0.002):
            peak[0] = max(peak[0], _process_rss_bytes())

    sampler = threading.Thread(target=sample_rss, name="v3-rss-sampler", daemon=True)
    sampler.start()
    cold_started = perf_counter_ns()
    authority = optimize_field(field, ceiling=fixture.ceiling)
    cold_ms = (perf_counter_ns() - cold_started) / 1_000_000
    measurements = []
    try:
        for _index in range(REPETITIONS):
            started = perf_counter_ns()
            replay = optimize_field(field, ceiling=fixture.ceiling)
            measurements.append((perf_counter_ns() - started) / 1_000_000)
            if replay != authority:
                raise RuntimeError("optimizer capacity replay changed receipt bytes")
    finally:
        stop.set()
        sampler.join(timeout=1)
    peak[0] = max(peak[0], _process_rss_bytes())
    ordered = sorted(measurements)
    return {
        "name": fixture.name,
        "candidate_search_strategy": (
            "exhaustive_radius_v1"
            if len(field.competitors) <= DEFAULT_OPTIMIZER_POLICY.small_field_maximum
            else "deterministic_beam_v1"
        ),
        "receipt_search_strategy": authority.search_strategy,
        "fallback_reason": (
            None if authority.fallback_reason is None else authority.fallback_reason.value
        ),
        "receipt_digest": authority.receipt_digest,
        "expected_times_ms": list(fixture.expected_times_ms),
        "source_receipt_digest": fixture.source_receipt_digest,
        "input_digest": field.input_digest,
        "sample_matrix_digest": field.sample_matrix_digest,
        "ceiling": fixture.ceiling,
        "legal_sheets_evaluated": authority.work_budget.candidates_evaluated,
        "expansion_rounds": authority.work_budget.expansion_rounds,
        "repetitions": REPETITIONS,
        "cold_optimizer_ms": round(cold_ms, 3),
        "optimizer_only_measurements_ms": [round(value, 3) for value in measurements],
        "observed_p50_ms": round(_percentile(ordered, 0.50), 3),
        "observed_p95_ms": round(_percentile(ordered, 0.95), 3),
        "observed_p99_ms": round(_percentile(ordered, 0.99), 3),
        "observed_worst_ms": round(max(measurements), 3),
        "peak_rss_delta_mib": round(max(0, peak[0] - rss_before) / (1024 * 1024), 3),
    }


def _percentile(ordered: list[float], quantile: float) -> float:
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    fraction = position - lower
    if fraction == 0:
        return ordered[lower]
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * fraction


def _environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "physical_cpu_count": None,
        "memory_total_mib": round(_physical_memory_bytes() / (1024 * 1024), 1),
        "rss_sampler_interval_ms": 2,
        "percentile_method": "linear_index_n_minus_1",
    }


def _process_rss_bytes() -> int:
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = (
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        )

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    process = get_current_process()
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    get_process_memory_info.restype = ctypes.c_int
    if not get_process_memory_info(process, ctypes.byref(counters), counters.cb):
        raise OSError("Windows could not report process RSS")
    return int(counters.WorkingSetSize)


def _physical_memory_bytes() -> int:
    class MemoryStatus(ctypes.Structure):
        _fields_ = (
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("Windows could not report physical memory")
    return int(status.ullTotalPhys)


if __name__ == "__main__":
    raise SystemExit(main())
