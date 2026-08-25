"""Prove the designated-Windows U15 field-assembly and recovery capacity gates."""

from __future__ import annotations

import argparse
import ctypes
import gc
import importlib.util
import json
import os
import platform
import shutil
import sys
import threading
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter_ns
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STRATHMARK_DB_PATH", str(ROOT / ".tmp" / "v3-field-capacity-import.sqlite3"))

from strathmark.v3.application.capacity import (  # noqa: E402
    CapacityManifest,
    CapacityUse,
    JobKind,
    JobLane,
    JobPriority,
)
from strathmark.v3.application.field_assembly import FieldAssemblyService  # noqa: E402
from strathmark.v3.contracts.canonical import canonical_digest  # noqa: E402
from strathmark.v3.infrastructure.sqlite.connection import (  # noqa: E402
    open_v3_connection,
)
from strathmark.v3.infrastructure.sqlite.jobs import (  # noqa: E402
    DurableJobRepository,
    JobRequest,
)
from strathmark.v3.infrastructure.sqlite.projections import (  # noqa: E402
    SQLiteFieldProjectionStore,
)

REPETITIONS = 100
FIELD_P99_LIMIT_MS = 2_000
LOOKUP_P99_LIMIT_MS = 250
LOOKUP_WORST_LIMIT_MS = 250
RESTART_LIMIT_MS = 5_000
RSS_DELTA_LIMIT_MIB = 512
FIELD_ENTRANTS = 12
PLAUSIBLE_QUALIFIER_CARDS = 48
COMPONENT_JOBS_PER_CARD = 5
QUEUE_JOB_COUNT = PLAUSIBLE_QUALIFIER_CARDS * COMPONENT_JOBS_PER_CARD
FIXTURE_PATH = ROOT / "tests" / "v3" / "integration" / "test_field_receipts.py"
CAPACITY_PATH = ROOT / "benchmarks" / "v3" / "job_capacity_manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    parser.add_argument("--keep-work", action="store_true")
    arguments = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("field-assembly capacity authority requires Windows")
    if not 1 <= arguments.repetitions <= REPETITIONS:
        raise SystemExit("repetitions must be between 1 and 100")

    fixture = _load_fixture_module()
    capacity = CapacityManifest.load(CAPACITY_PATH)
    work_root = (ROOT / ".tmp" / f"field-capacity-{uuid4().hex}").resolve()
    expected_parent = (ROOT / ".tmp").resolve()
    if work_root.parent != expected_parent:
        raise SystemExit("capacity work directory escaped the repository .tmp root")
    work_root.mkdir(parents=True, exist_ok=False)

    gc.collect()
    rss_before = _process_rss_bytes()
    peak_rss = [rss_before]
    stop = threading.Event()

    def sample_rss() -> None:
        while not stop.wait(0.002):
            peak_rss[0] = max(peak_rss[0], _process_rss_bytes())

    sampler = threading.Thread(target=sample_rss, name="v3-field-capacity-rss", daemon=True)
    sampler.start()
    last_material: tuple[Any, Any, Any, Path, bytes, str] | None = None
    assembly_report: dict[str, Any]
    lookup_report: dict[str, Any]
    restart_report: dict[str, Any]
    try:
        assembly_report, last_material = _measure_complete_assembly(
            fixture, work_root, arguments.repetitions, capacity
        )
        lookup_report = _measure_saturated_recovery(
            last_material, capacity, repetitions=arguments.repetitions
        )
        restart_report = _measure_cold_restart(last_material, repetitions=5)
    finally:
        stop.set()
        sampler.join(timeout=1)
        peak_rss[0] = max(peak_rss[0], _process_rss_bytes())
        if not arguments.keep_work:
            resolved = work_root.resolve()
            if resolved.parent != expected_parent or not resolved.name.startswith(
                "field-capacity-"
            ):
                raise RuntimeError("refusing to remove an unverified work directory")
            shutil.rmtree(resolved)

    rss_delta_mib = round(max(0, peak_rss[0] - rss_before) / (1024 * 1024), 3)
    formal_run = arguments.repetitions == REPETITIONS
    gates = {
        "formal_repetition_count": formal_run,
        "complete_assembly_p99": assembly_report["observed_p99_ms"] < FIELD_P99_LIMIT_MS,
        "complete_assembly_no_failures": assembly_report["failed_runs"] == 0,
        "authority_blob_within_capacity": assembly_report["maximum_disagreement_authority_bytes"]
        <= capacity.max_blob_bytes,
        "saturated_recovery_p99": lookup_report["observed_p99_ms"] <= LOOKUP_P99_LIMIT_MS,
        "saturated_recovery_worst": lookup_report["observed_worst_ms"] <= LOOKUP_WORST_LIMIT_MS,
        "critical_restart_worst": restart_report["observed_worst_ms"] <= RESTART_LIMIT_MS,
        "rss_delta": rss_delta_mib < RSS_DELTA_LIMIT_MIB,
        "provider_independent": True,
    }
    passed = all(gates.values())
    capacity_headroom_ms = round(120_000 - assembly_report["observed_p99_ms"], 3)
    body = {
        "schema_version": "strathmark-v3-field-assembly-benchmark-v2",
        "status": "passed" if passed else "failed",
        "measured_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "environment": _environment(),
        "capacity_manifest_digest": capacity.digest,
        "capacity": {
            "open_tournaments": 1,
            "round_entrants": capacity.max_round_entrants,
            "field_entrants": FIELD_ENTRANTS,
            "plausible_qualifier_cards": PLAUSIBLE_QUALIFIER_CARDS,
            "component_jobs_per_card": COMPONENT_JOBS_PER_CARD,
            "queued_inference_jobs": QUEUE_JOB_COUNT,
        },
        "artifact_identity": _artifact_identity(),
        "gates": gates,
        "limits": {
            "complete_assembly_p99_ms_exclusive": FIELD_P99_LIMIT_MS,
            "exact_retry_p99_ms_inclusive": LOOKUP_P99_LIMIT_MS,
            "exact_retry_worst_ms_inclusive": LOOKUP_WORST_LIMIT_MS,
            "critical_restart_ms_inclusive": RESTART_LIMIT_MS,
            "rss_delta_mib_exclusive": RSS_DELTA_LIMIT_MIB,
        },
        "complete_confirmed_field_assembly": assembly_report,
        "saturated_inference_queue_recovery": lookup_report,
        "critical_restart": restart_report,
        "cadence": {
            "result_to_ready_budget_ms": 120_000,
            "five_minute_final_turnaround_ms": 300_000,
            "deterministic_assembly_p99_headroom_ms": capacity_headroom_ms,
            "provider_call_during_measured_assembly": False,
            "scope": (
                "sealed current cards through receipt blob, event append, and projection "
                "commit; unfinished inference is excluded by contract"
            ),
        },
        "notes": [
            "Every complete assembly uses a fresh SQLite database and a 12-entrant field.",
            "The same-database recovery check runs with 48 plausible cards represented by 240 queued inference jobs.",
            "Receipt recovery is forced to succeed while the supplied provider callback raises if invoked.",
            "The checked-in integration fixture is hash-bound and uses only production constructors and adapters.",
        ],
    }
    manifest = {
        "schema_version": body.pop("schema_version"),
        "manifest_digest": canonical_digest(
            {"schema_version": "strathmark-v3-field-assembly-benchmark-v2", **body}
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


def _measure_complete_assembly(
    fixture: Any,
    work_root: Path,
    repetitions: int,
    capacity: CapacityManifest,
) -> tuple[dict[str, Any], tuple[Any, Any, Any, Path, bytes, str]]:
    durations: list[float] = []
    failed_runs = 0
    authority_sizes: list[int] = []
    rolling_preparation_ms: list[float] = []
    currentness_verifications: list[int] = []
    expected_sheet: tuple[tuple[str, int], ...] | None = None
    last_material: tuple[Any, Any, Any, Path, bytes, str] | None = None
    cold_ms: float | None = None
    for index in range(repetitions):
        database_path = work_root / f"field-{index:03d}.sqlite3"
        store, field, fixture_build, _lifecycle = fixture._bootstrap(
            database_path, competitor_count=FIELD_ENTRANTS
        )
        preparation_started = perf_counter_ns()
        build, source = _prepare_current_rolling_builder(fixture, store, field, fixture_build)
        rolling_preparation_ms.append((perf_counter_ns() - preparation_started) / 1_000_000)
        captured: dict[str, Any] = {}

        def capture(current_field: Any) -> Any:
            pipeline = build(current_field)
            captured["pipeline"] = pipeline
            return pipeline

        started = perf_counter_ns()
        try:
            result = FieldAssemblyService(store).assemble(
                field=field,
                caller_namespace="manager",
                request_identity=f"idempotency:field-capacity-{index}",
                actor_id="actor:manager",
                occurred_at=fixture.NOW,
                build_pipeline=capture,
            )
        except Exception:
            failed_runs += 1
            raise
        elapsed = (perf_counter_ns() - started) / 1_000_000
        if cold_ms is None:
            cold_ms = elapsed
        durations.append(elapsed)
        sheet = tuple((str(item.competitor_id), item.mark) for item in result.receipt.marks)
        if expected_sheet is None:
            expected_sheet = sheet
        elif sheet != expected_sheet:
            raise RuntimeError("field capacity replay changed the issued mark sheet")
        pipeline = captured["pipeline"]
        currentness_verifications.append(source.verifications)
        if source.verifications != 2:
            raise RuntimeError("production rolling builder omitted a currentness verification")
        if pipeline.disagreement is None:
            raise RuntimeError("capacity fixture omitted operational disagreement")
        authority_size = len(pipeline.disagreement.canonical_authority_payload)
        if authority_size > capacity.max_blob_bytes:
            raise RuntimeError("disagreement authority exceeded signed blob capacity")
        authority_sizes.append(authority_size)
        last_material = (
            store,
            field,
            fixture,
            database_path,
            result.canonical_bytes,
            f"idempotency:field-capacity-{index}",
        )
    assert last_material is not None and cold_ms is not None
    ordered = sorted(durations)
    return (
        {
            "runs": repetitions,
            "failed_runs": failed_runs,
            "cold_first_run_ms": round(cold_ms, 3),
            "measurements_ms": [round(value, 3) for value in durations],
            "observed_p50_ms": round(_percentile(ordered, 0.50), 3),
            "observed_p95_ms": round(_percentile(ordered, 0.95), 3),
            "observed_p99_ms": round(_percentile(ordered, 0.99), 3),
            "observed_worst_ms": round(max(durations), 3),
            "maximum_disagreement_authority_bytes": max(authority_sizes),
            "rolling_preparation_outside_gate": {
                "runs": repetitions,
                "observed_p50_ms": round(_percentile(sorted(rolling_preparation_ms), 0.50), 3),
                "observed_p99_ms": round(_percentile(sorted(rolling_preparation_ms), 0.99), 3),
                "observed_worst_ms": round(max(rolling_preparation_ms), 3),
                "currentness_verifications_per_assembly": sorted(set(currentness_verifications)),
                "scope": (
                    "prospective card and capability fixture construction; excluded because "
                    "rolling preparation completes before field confirmation"
                ),
            },
            "field_entrants": FIELD_ENTRANTS,
            "selected_sheet": [list(item) for item in (expected_sheet or ())],
            "includes": [
                "authoritative_current_field_verification",
                "precomputed_card_loading",
                "typed_pool_and_joint_draw_validation",
                "operational_disagreement_replay",
                "optimizer_replay",
                "receipt_and_disagreement_blob_publish",
                "atomic_event_and_projection_commit",
            ],
        },
        last_material,
    )


def _prepare_current_rolling_builder(
    fixture: Any,
    store: SQLiteFieldProjectionStore,
    field: Any,
    fixture_build: Any,
) -> tuple[Any, Any]:
    """Prepare prospective card/capability authority outside the confirmation gate."""

    from strathmark.v3.application.field_assembly import RollingCapabilityBinding
    from strathmark.v3.application.pipeline_builder import (
        RollingCapabilityAuthority,
        RollingCurrentCard,
        RollingFieldBuildInputs,
        RollingFieldPipelineBuilder,
    )
    from strathmark.v3.contracts.forecasts import AssessorKind
    from strathmark.v3.domain.capability import CapabilityState
    from strathmark.v3.domain.credibility import WeightReceipt

    baseline = fixture_build(field)
    if baseline.disagreement is None:
        raise RuntimeError("capacity fixture omitted ordinary disagreement authority")
    cards = tuple(
        RollingCurrentCard(
            evidence.card,
            *fixture._test_rolling_publication_material(
                field,
                evidence.card,
                dependency_revision=max(1, field.tournament_event_sequence),
                signer=store._signer,
            ),
        )
        for evidence in baseline.prediction_evidence
    )
    capabilities = []
    for index, assignment in enumerate(field.ordered_assignments):
        value = fixture._capability(assignment.competitor_id, 40_000 + index * 10_000).to_dict()
        value["context_digest"] = field.target_context.digest
        value["state_digest"] = canonical_digest(
            {key: item for key, item in value.items() if key != "state_digest"}
        )
        state = CapabilityState.from_dict(value)
        capabilities.append(
            RollingCapabilityAuthority(
                state,
                RollingCapabilityBinding.create(
                    competitor_id=state.competitor_id,
                    context_digest=state.context_digest,
                    state_revision=state.state_revision,
                    state_digest=state.state_digest,
                    aggregate_version=state.state_revision,
                    aggregate_event_digest=canonical_digest(
                        {
                            "competitor_id": str(state.competitor_id),
                            "state_digest": state.state_digest,
                        }
                    ),
                ),
            )
        )
    weight_receipt = WeightReceipt(
        baseline.weight_authority.context,
        baseline.weight_authority.weights,
        (),
        baseline.weight_authority.calibration_cutoff_at_utc,
        baseline.weight_authority.policy_digest,
        baseline.weight_authority.weight_receipt_digest,
    )
    inputs = RollingFieldBuildInputs(
        cards,
        weight_receipt,
        baseline.operational_weight_authority,
        baseline.dependence_artifact,
        tuple(capabilities),
        baseline.disagreement.decision.policy,
    )

    class CurrentSource:
        def __init__(self) -> None:
            self.verifications = 0

        def load_current(self, revision: Any) -> Any:
            if revision != field:
                raise RuntimeError("rolling benchmark loaded the wrong field revision")
            return inputs

        def verify_current(
            self,
            revision: Any,
            publications: tuple[Any, ...],
            capability_bindings: tuple[Any, ...],
        ) -> None:
            if (
                revision != field
                or publications != tuple(item.publication for item in cards)
                or capability_bindings != tuple(item.binding for item in capabilities)
            ):
                raise RuntimeError("rolling benchmark current authority changed")
            self.verifications += 1

    source = CurrentSource()
    production_builder = RollingFieldPipelineBuilder(
        source,
        signer=store._signer,
        trust_store=store._trust_store,
        clock=lambda: fixture.NOW,
    )

    def build(revision: Any) -> Any:
        rolling = production_builder(revision)
        if not hasattr(rolling, "pipeline"):
            raise RuntimeError("ordinary capacity field requested a manual action")
        return rolling.pipeline

    expected = (
        AssessorKind.FORMULA,
        AssessorKind.ML,
        AssessorKind.LLM_COUNCIL,
    )
    if tuple(item.assessor for item in cards[0].card.forecasts) != expected:
        raise RuntimeError("capacity fixture assessor roster differs")
    return build, source


def _measure_saturated_recovery(
    material: tuple[Any, Any, Any, Path, bytes, str],
    capacity: CapacityManifest,
    *,
    repetitions: int,
) -> dict[str, Any]:
    store, field, fixture, database_path, expected_bytes, request_identity = material
    repository = DurableJobRepository(
        database_path,
        capacity=capacity,
        signer=store._signer,
        trust_store=store._trust_store,
    )
    kinds = (
        JobKind.FORMULA_CARD,
        JobKind.ML_CARD,
        JobKind.LOCAL_LLM_CARD,
        JobKind.CLOUD_LLM_CARD,
        JobKind.LOCAL_LLM_CARD,
    )
    for card in range(PLAUSIBLE_QUALIFIER_CARDS):
        for component, kind in enumerate(kinds):
            ordinal = card * len(kinds) + component
            repository.enqueue(
                JobRequest.create(
                    job_id=f"job:capacity-{ordinal:03d}",
                    job_revision=1,
                    idempotency_key=f"job_request:capacity-{ordinal:03d}",
                    job_kind=kind,
                    lane=JobLane.INFERENCE,
                    priority=JobPriority.PLAUSIBLE_QUALIFIER,
                    capacity_use=CapacityUse(
                        1,
                        capacity.max_round_entrants,
                        FIELD_ENTRANTS,
                        PLAUSIBLE_QUALIFIER_CARDS,
                        PLAUSIBLE_QUALIFIER_CARDS,
                        1_024,
                        4_096,
                        capacity.max_api_page_size,
                    ),
                    payload={
                        "schema_version": "strathmark-v3-capacity-queue-fixture-v1",
                        "card_ordinal": card,
                        "component_ordinal": component,
                    },
                    evidence_digest="a" * 64,
                    bundle_digest="b" * 64,
                    retry_policy_version="retry.v1",
                    created_at="2026-08-25T08:00:00.000Z",
                    not_before_at="2026-08-25T08:00:00.000Z",
                    hard_deadline_at="2026-08-25T08:05:00.000Z",
                    max_attempts=3,
                )
            )
            if (ordinal + 1) % 48 == 0:
                repository.refresh_rolling_restart_checkpoint_if_due(
                    observed_at="2026-08-25T08:00:00.000Z",
                    delta_threshold=48,
                )
    with open_v3_connection(database_path, read_only=True) as connection:
        queued = int(
            connection.execute(
                "SELECT COUNT(*) FROM v3_jobs WHERE lane=? AND state='queued'",
                (JobLane.INFERENCE.value,),
            ).fetchone()[0]
        )
    if queued != QUEUE_JOB_COUNT:
        raise RuntimeError("capacity queue fixture did not reach its declared load")

    def unavailable(_field: Any) -> Any:
        raise RuntimeError("exact recovery attempted to load a provider")

    service = FieldAssemblyService(store)
    durations = []
    for _index in range(repetitions):
        started = perf_counter_ns()
        result = service.assemble(
            field=field,
            caller_namespace="manager",
            request_identity=request_identity,
            actor_id="actor:manager",
            occurred_at=fixture.NOW,
            build_pipeline=unavailable,
        )
        durations.append((perf_counter_ns() - started) / 1_000_000)
        if result.canonical_bytes != expected_bytes:
            raise RuntimeError("saturated recovery changed exact receipt bytes")
    ordered = sorted(durations)
    return {
        "runs": repetitions,
        "queued_inference_jobs": queued,
        "plausible_qualifier_cards": PLAUSIBLE_QUALIFIER_CARDS,
        "provider_loaded": False,
        "measurements_ms": [round(value, 3) for value in durations],
        "observed_p50_ms": round(_percentile(ordered, 0.50), 3),
        "observed_p95_ms": round(_percentile(ordered, 0.95), 3),
        "observed_p99_ms": round(_percentile(ordered, 0.99), 3),
        "observed_worst_ms": round(max(durations), 3),
    }


def _measure_cold_restart(
    material: tuple[Any, Any, Any, Path, bytes, str], *, repetitions: int
) -> dict[str, Any]:
    store, field, _fixture, database_path, expected_bytes, request_identity = material
    durations = []
    for _index in range(repetitions):
        started = perf_counter_ns()
        restarted = SQLiteFieldProjectionStore(
            database_path,
            signer=store._signer,
            trust_store=store._trust_store,
        )
        result = restarted.lookup_exact(
            caller_namespace="manager",
            request_identity=request_identity,
            field_revision_digest=field.revision_digest,
        )
        durations.append((perf_counter_ns() - started) / 1_000_000)
        if result is None or result.canonical_bytes != expected_bytes:
            raise RuntimeError("critical restart did not recover exact receipt bytes")
    ordered = sorted(durations)
    return {
        "runs": repetitions,
        "measurements_ms": [round(value, 3) for value in durations],
        "observed_p50_ms": round(_percentile(ordered, 0.50), 3),
        "observed_p95_ms": round(_percentile(ordered, 0.95), 3),
        "observed_p99_ms": round(_percentile(ordered, 0.99), 3),
        "observed_worst_ms": round(max(durations), 3),
        "receipt_lookup_available": True,
    }


def _load_fixture_module() -> Any:
    spec = importlib.util.spec_from_file_location("v3_field_capacity_fixture", FIXTURE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("field capacity fixture cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact_identity() -> dict[str, Any]:
    paths = {
        "benchmark_script_sha256": Path(__file__),
        "fixture_source_sha256": FIXTURE_PATH,
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
    return {name: _sha256(path) for name, path in paths.items()}


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
        "memory_total_mib": round(_physical_memory_bytes() / (1024 * 1024), 1),
        "rss_sampler_interval_ms": 2,
        "percentile_method": "linear_index_n_minus_1",
    }


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


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
