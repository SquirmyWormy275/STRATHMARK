"""Generate bounded, executable Windows thermal/memory/storage stress evidence.

The script never attempts to exhaust the operator machine.  It exercises the installed
local models while sampling the real NVIDIA device, and runs the repository's bounded
memory-admission and storage-corruption checks in an isolated test authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _nvidia_sample() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ]
    completed = subprocess.run(command, capture_output=True, timeout=15, check=False)
    if completed.returncode != 0:
        raise RuntimeError("NVIDIA telemetry command failed")
    lines = completed.stdout.decode("utf-8").strip().splitlines()
    if len(lines) != 1:
        raise RuntimeError("NVIDIA telemetry did not identify exactly one GPU")
    fields = [item.strip() for item in lines[0].split(",")]
    if len(fields) != 5:
        raise RuntimeError("NVIDIA telemetry fields differ")
    return {
        "observed_at": _utc_now(),
        "gpu": fields[0],
        "memory_total_mib": int(fields[1]),
        "memory_used_mib": int(fields[2]),
        "temperature_c": int(fields[3]),
        "power_w": float(fields[4]),
    }


def _ollama_generate(model: str, *, timeout: float) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": model,
            "prompt": (
                'Return JSON only: {"status":"ok"}. This is a bounded STRATHMARK '
                "release thermal and VRAM probe; do not add commentary."
            ),
            "stream": False,
            "options": {"temperature": 0, "num_predict": 32},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter_ns()
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
        body = response.read(2_000_000)
        status = response.status
    duration_ms = (time.perf_counter_ns() - started) // 1_000_000
    value = json.loads(body)
    if status != 200 or value.get("done") is not True or value.get("model") != model:
        raise RuntimeError(f"local model probe failed: {model}")
    return {
        "model": model,
        "duration_ms": duration_ms,
        "response_sha256": _sha(body),
        "done": True,
    }


def _run_pytest(
    root: Path, selectors: list[str], base: Path, env: dict[str, str]
) -> dict[str, Any]:
    argv = [
        str(Path(sys.executable).resolve()),
        "-m",
        "pytest",
        *selectors,
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        base.as_posix(),
    ]
    started = time.perf_counter_ns()
    completed = subprocess.run(
        argv,
        cwd=root,
        env={**os.environ, **env},
        capture_output=True,
        timeout=900,
        check=False,
    )
    duration_ms = (time.perf_counter_ns() - started) // 1_000_000
    stdout = completed.stdout.decode("utf-8", errors="replace")
    match = re.search(r"(?P<passed>\d+) passed", stdout)
    if completed.returncode != 0 or match is None:
        raise RuntimeError("bounded stress pytest command failed")
    return {
        "argv": argv,
        "exit_code": completed.returncode,
        "passed": int(match.group("passed")),
        "duration_ms": duration_ms,
        "stdout_sha256": _sha(completed.stdout),
        "stderr_sha256": _sha(completed.stderr),
    }


def build_report(
    *, root: Path, models: tuple[str, ...], duration_seconds: int, max_temperature_c: int
) -> dict[str, Any]:
    if not platform.system().lower().startswith("windows"):
        raise RuntimeError("Windows stress evidence requires the designated Windows host")
    if not models or any(not model.strip() for model in models):
        raise RuntimeError("at least one explicit local model is required")
    samples: list[dict[str, Any]] = []
    sampling_error: list[str] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.25):
            try:
                samples.append(_nvidia_sample())
            except Exception as exc:  # preserve sampler failure for the main gate
                sampling_error.append(str(exc))
                stop.set()

    samples.append(_nvidia_sample())
    sampler = threading.Thread(target=sample, name="v3-release-gpu-sampler", daemon=True)
    sampler.start()
    runs: list[dict[str, Any]] = []
    deadline = time.monotonic() + duration_seconds
    try:
        while time.monotonic() < deadline or len(runs) < len(models):
            model = models[len(runs) % len(models)]
            runs.append(_ollama_generate(model, timeout=180))
    finally:
        stop.set()
        sampler.join(timeout=5)
        samples.append(_nvidia_sample())
    if sampling_error or len(samples) < 3:
        raise RuntimeError("GPU telemetry sampling failed")
    if max(item["temperature_c"] for item in samples) > max_temperature_c:
        raise RuntimeError("bounded model workload exceeded the thermal gate")
    if max(item["memory_used_mib"] for item in samples) >= samples[0]["memory_total_mib"]:
        raise RuntimeError("bounded model workload exhausted VRAM")

    run_root = Path(os.environ["STRATHMARK_V3_DB_PATH"]).parent
    pressure = _run_pytest(
        root,
        [
            "tests/v3/integration/test_llm_job_adapters.py::test_provider_failure_matrix_is_typed_and_bounded",
            "tests/v3/property/test_job_state_machine.py::test_lane_lease_limit_is_atomic_and_does_not_consume_queued_work",
            "tests/v3/integration/test_sqlite_migrations.py::test_connection_policy_rejects_memory_bool_and_coerced_controls",
        ],
        run_root / "stress-pressure-pytest",
        {
            "STRATHMARK_TEST_DB": "1",
            "STRATHMARK_DB_PATH": (run_root / "stress-pressure-v2.sqlite3").as_posix(),
            "STRATHMARK_V3_DB_PATH": (run_root / "stress-pressure-v3.sqlite3").as_posix(),
        },
    )
    storage = _run_pytest(
        root,
        [
            "tests/v3/integration/test_blob_store.py",
            "tests/v3/system/test_backup_restore.py::test_disk_reserve_degrades_maintenance_before_preserving_open_critical_lane",
            "tests/v3/integration/test_v2_readonly_import.py::test_active_wal_source_fails_closed_without_touching_source_or_v3",
        ],
        run_root / "stress-storage-pytest",
        {
            "STRATHMARK_TEST_DB": "1",
            "STRATHMARK_DB_PATH": (run_root / "stress-storage-v2.sqlite3").as_posix(),
            "STRATHMARK_V3_DB_PATH": (run_root / "stress-storage-v3.sqlite3").as_posix(),
        },
    )
    return {
        "schema_version": "strathmark-v3-windows-stress-receipt-v1",
        "recorded_at": _utc_now(),
        "result": "passed",
        "bounded_non_exhaustive": True,
        "thermal_gate_c": max_temperature_c,
        "models": list(models),
        "model_runs": runs,
        "gpu_samples": samples,
        "pressure_injection": pressure,
        "storage_injection": storage,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-model", action="append", required=True)
    parser.add_argument("--duration-seconds", type=int, default=30)
    parser.add_argument("--max-temperature-c", type=int, default=87)
    arguments = parser.parse_args(argv)
    if not 10 <= arguments.duration_seconds <= 300:
        raise SystemExit("duration must be between 10 and 300 seconds")
    root = Path(__file__).resolve().parents[1]
    try:
        report = build_report(
            root=root,
            models=tuple(arguments.local_model),
            duration_seconds=arguments.duration_seconds,
            max_temperature_c=arguments.max_temperature_c,
        )
    except Exception as exc:
        print(json.dumps({"result": "failed", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    print(arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
