"""Measure the public 64-competitor optimizer capacity contract."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

from strathmark.config import rules
from strathmark.mark_optimizer import DEFAULT_MARK_SAMPLES, optimize_joint_marks
from strathmark.prediction_v2 import (
    NORMAL_90_RADIUS,
    ForecastInterval,
    PredictiveDistribution,
)

FIELD_SIZE = 64
MAX_SECONDS = 10.0
MAX_INCREMENTAL_MIB = 256.0
CAPACITY_SCHEMA = "prediction-v2-optimizer-capacity/v1"


def build_capacity_field() -> list[PredictiveDistribution]:
    """Return a deterministic maximum-size public field."""

    field = []
    for index in range(FIELD_SIZE):
        median = 70.0 - index * 0.55
        log_scale = 0.08 + (index % 5) * 0.01
        location = math.log(median)
        radius = NORMAL_90_RADIUS * log_scale
        field.append(
            PredictiveDistribution(
                median=median,
                log_location=location,
                log_scale=log_scale,
                interval=ForecastInterval(
                    lower=math.exp(location - radius),
                    upper=math.exp(location + radius),
                ),
                source="capacity_fixture",
                history_count=5,
                effective_history_weight=3.0,
                metadata={"shared_log_scale": 0.03},
            )
        )
    return field


def _peak_rss_bytes() -> int:
    if os.name == "nt":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
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
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = ctypes.c_void_p
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        get_process_memory_info.restype = ctypes.c_int
        if not get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)

    import resource

    maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return maximum if sys.platform == "darwin" else maximum * 1024


def measure_capacity() -> dict[str, Any]:
    """Measure runtime and incremental peak RSS and enforce the release budget."""

    field = build_capacity_field()
    memory_before = _peak_rss_bytes()
    started = time.perf_counter()
    result = optimize_joint_marks(field, ceiling=rules.MAX_MARK_SECONDS)
    elapsed = time.perf_counter() - started
    incremental_mib = max(0, _peak_rss_bytes() - memory_before) / (1024 * 1024)
    passed = (
        result.optimizer == "posterior_crn_v2"
        and len(result.marks) == FIELD_SIZE
        and result.simulations == DEFAULT_MARK_SAMPLES
        and elapsed <= MAX_SECONDS
        and incremental_mib <= MAX_INCREMENTAL_MIB
    )
    return {
        "schema_version": CAPACITY_SCHEMA,
        "scenario": {
            "field_size": FIELD_SIZE,
            "ceiling": rules.MAX_MARK_SECONDS,
            "simulations": DEFAULT_MARK_SAMPLES,
        },
        "limits": {
            "elapsed_seconds": MAX_SECONDS,
            "incremental_peak_rss_mib": MAX_INCREMENTAL_MIB,
        },
        "measurement": {
            "elapsed_seconds": elapsed,
            "incremental_peak_rss_mib": incremental_mib,
            "passes": result.passes,
            "optimizer": result.optimizer,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "passed": passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = measure_capacity()
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
