"""Execute and seal the complete V3 release proof suite.

This is the intentionally slow command.  ``verify_v3_release.py`` consumes its signed
receipt and never substitutes source hashes for these executions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from scripts.release_evidence import (
        EVIDENCE_SCHEMA,
        PROOF_OPERATIONS,
        create_evidence_envelope,
        dependency_snapshot,
        git_head,
        require_clean_release_inputs,
        sha256_file,
        source_tree_digest,
        wheel_identity,
        write_canonical_envelope,
    )
except ModuleNotFoundError:  # direct script execution
    from release_evidence import (
        EVIDENCE_SCHEMA,
        PROOF_OPERATIONS,
        create_evidence_envelope,
        dependency_snapshot,
        git_head,
        require_clean_release_inputs,
        sha256_file,
        source_tree_digest,
        wheel_identity,
        write_canonical_envelope,
    )

from strathmark.v3.application.cutover import REQUIRED_RELEASE_EVIDENCE
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.infrastructure.integrity import P256EphemeralSigner

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "benchmarks/v3/v3_executable_evidence.json"
DEFAULT_WHEEL_DIRECTORY = ROOT / "dist/v3-release"
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "benchmarks/v3/release_evidence"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError("release evidence output escaped the repository") from exc


def _test_environment(run_directory: str, proof_name: str) -> dict[str, str]:
    return {
        "STRATHMARK_TEST_DB": "1",
        "STRATHMARK_DB_PATH": f"{run_directory}/{proof_name}-v2.sqlite3",
        "STRATHMARK_V3_DB_PATH": f"{run_directory}/{proof_name}-v3.sqlite3",
    }


def _pytest_argv(selectors: list[str], *, run_directory: str, proof_name: str) -> list[str]:
    return [
        str(Path(sys.executable).resolve()),
        "-m",
        "pytest",
        *selectors,
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        f"{run_directory}/{proof_name}-pytest",
    ]


def _execute(
    operation: str,
    argv: list[str],
    *,
    environment: dict[str, str],
    timeout: int,
    pytest_command: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter_ns()
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        env={**os.environ, **environment},
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    duration_ms = (time.perf_counter_ns() - started) // 1_000_000
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    passed = 1
    failed = 0
    errors = 0
    skipped = 0
    if pytest_command:
        passed_match = re.search(r"(?P<count>\d+) passed", stdout)
        failed_match = re.search(r"(?P<count>\d+) failed", stdout)
        error_match = re.search(r"(?P<count>\d+) errors?", stdout)
        skipped_match = re.search(r"(?P<count>\d+) skipped", stdout)
        passed = 0 if passed_match is None else int(passed_match.group("count"))
        failed = 0 if failed_match is None else int(failed_match.group("count"))
        errors = 0 if error_match is None else int(error_match.group("count"))
        skipped = 0 if skipped_match is None else int(skipped_match.group("count"))
    step = {
        "operation": operation,
        "argv": argv,
        "cwd": ".",
        "environment": environment,
        "exit_code": completed.returncode,
        "duration_ms": duration_ms,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "stdout_sha256": _sha_bytes(completed.stdout),
        "stderr_sha256": _sha_bytes(completed.stderr),
    }
    if completed.returncode or passed < 1 or failed or errors:
        raise RuntimeError(
            f"release proof command failed: {operation}\n{stdout[-2000:]}\n{stderr[-2000:]}"
        )
    return step


def _proof(
    name: str,
    steps: list[dict[str, Any]],
    *,
    artifact_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    files = artifact_files or {}
    digests = {role: sha256_file(ROOT / path) for role, path in files.items()}
    body = {
        "name": name,
        "proof_kind": f"{name}_execution_v1",
        "observed_at": _utc_now(),
        "result": "passed",
        "steps": steps,
        "artifact_digests": digests,
        "artifact_files": files,
    }
    return {**body, "receipt_digest": canonical_digest(body)}


def _pytest_proof(
    name: str,
    operation: str,
    selectors: list[str],
    *,
    run_directory: str,
    timeout: int = 1_800,
) -> dict[str, Any]:
    environment = _test_environment(run_directory, name)
    step = _execute(
        operation,
        _pytest_argv(selectors, run_directory=run_directory, proof_name=name),
        environment=environment,
        timeout=timeout,
        pytest_command=True,
    )
    return _proof(name, [step])


def _installed_artifact_proof(
    *, run_directory: str, wheel_directory: Path
) -> tuple[dict[str, Any], Path]:
    environment = _test_environment(run_directory, "installed_artifact")
    build_directory = f"{run_directory}/wheel-build"
    build = _execute(
        "wheel_build",
        [
            str(Path(sys.executable).resolve()),
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            build_directory,
        ],
        environment=environment,
        timeout=300,
    )
    wheels = tuple((ROOT / build_directory).glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("wheel build did not produce exactly one artifact")
    built_wheel = wheels[0]
    identity = wheel_identity(built_wheel)
    wheel_directory.mkdir(parents=True, exist_ok=True)
    candidate = wheel_directory / built_wheel.name
    if candidate.exists() and sha256_file(candidate) != identity["sha256"]:
        raise RuntimeError("candidate wheel path already contains different bytes")
    if not candidate.exists():
        shutil.copy2(built_wheel, candidate)
    installed_root = f"{run_directory}/installed-wheel"
    install = _execute(
        "wheel_install",
        [
            str(Path(sys.executable).resolve()),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--disable-pip-version-check",
            "--target",
            installed_root,
            "--",
            built_wheel.as_posix(),
        ],
        environment=environment,
        timeout=180,
    )
    probe = _execute(
        "installed_probe",
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "scripts/probe_v3_release.py",
            "installed-wheel",
            "--installed-root",
            installed_root,
            "--expected-version",
            str(identity["version"]),
        ],
        environment=environment,
        timeout=120,
    )
    candidate_relative = _relative(candidate)
    return (
        _proof(
            "installed_artifact",
            [build, install, probe],
            artifact_files={"wheel_sha256": candidate_relative},
        ),
        candidate,
    )


def _machine_proof(
    name: str,
    operation: str,
    argv: list[str],
    *,
    run_directory: str,
    run_output: Path,
    final_output: Path,
    timeout: int,
) -> dict[str, Any]:
    environment = _test_environment(run_directory, name)
    step = _execute(operation, argv, environment=environment, timeout=timeout)
    if not run_output.is_file():
        raise RuntimeError(f"machine proof did not write its receipt: {name}")
    final_output.parent.mkdir(parents=True, exist_ok=True)
    if final_output.exists() and sha256_file(final_output) != sha256_file(run_output):
        raise RuntimeError(f"machine proof path already contains different bytes: {name}")
    if not final_output.exists():
        shutil.copy2(run_output, final_output)
    return _proof(
        name,
        [step],
        artifact_files={"machine_receipt_sha256": _relative(final_output)},
    )


def build_evidence(
    *,
    output: Path,
    wheel_directory: Path,
    artifact_directory: Path,
    local_models: tuple[str, ...],
    stress_seconds: int,
) -> dict[str, Any]:
    require_clean_release_inputs(ROOT)
    source_commit = git_head(ROOT)
    source_digest = source_tree_digest(ROOT)
    run_directory = f".tmp/v3-release-evidence-{uuid4().hex}"
    (ROOT / run_directory).mkdir(parents=True, exist_ok=False)

    installed, candidate = _installed_artifact_proof(
        run_directory=run_directory, wheel_directory=wheel_directory
    )
    environment = _test_environment(run_directory, "dependency_lock")
    dependency_step = _execute(
        "dependency_probe",
        [
            str(Path(sys.executable).resolve()),
            "scripts/probe_v3_release.py",
            "dependencies",
            "--lock",
            "requirements/v3-release.lock",
        ],
        environment=environment,
        timeout=120,
    )
    dependency = _proof("dependency_lock", [dependency_step])
    consumer = _pytest_proof(
        "consumer_contract",
        "consumer_contract_tests",
        ["tests/v3/integration/test_v3_consumer_contract.py"],
        run_directory=run_directory,
    )
    replay = _pytest_proof(
        "full_causal_replay",
        "causal_replay_tests",
        ["tests/v3/system/test_executable_replay.py"],
        run_directory=run_directory,
    )
    equity = _pytest_proof(
        "manipulation_equity_slices",
        "manipulation_equity_tests",
        [
            "tests/v3/evals/test_optimizer_consequences.py",
            "tests/v3/evals/test_selective_abstention.py",
            "tests/v3/integration/test_credibility_authority.py",
        ],
        run_directory=run_directory,
        timeout=3_600,
    )
    provider = _pytest_proof(
        "provider_failure_matrix",
        "provider_failure_tests",
        [
            "tests/v3/integration/test_llm_job_adapters.py::test_provider_failure_matrix_is_typed_and_bounded",
            "tests/v3/integration/test_durable_jobs.py::test_coordinator_classifies_provider_failures",
        ],
        run_directory=run_directory,
    )
    recovery = _pytest_proof(
        "race_day_recovery",
        "race_day_recovery_tests",
        [
            "tests/v3/system/test_executable_replay.py",
            "tests/v3/system/test_critical_issue_recovery.py",
        ],
        run_directory=run_directory,
        timeout=3_600,
    )
    result_to_ready_run_output = ROOT / run_directory / "result-to-ready.json"
    result_to_ready = _machine_proof(
        "result_to_ready",
        "result_to_ready_benchmark",
        [
            str(Path(sys.executable).resolve()),
            "scripts/benchmark_v3_result_to_ready.py",
            "--output",
            _relative(result_to_ready_run_output),
            "--work-root",
            f"{run_directory}/result-to-ready-work",
        ],
        run_directory=run_directory,
        run_output=result_to_ready_run_output,
        final_output=artifact_directory / f"{source_commit}-result-to-ready.json",
        timeout=3_600,
    )
    capacity_run_output = ROOT / run_directory / "windows-capacity.json"
    capacity = _machine_proof(
        "windows_capacity",
        "windows_capacity_benchmark",
        [
            str(Path(sys.executable).resolve()),
            "scripts/benchmark_v3_field_assembly.py",
            "--output",
            _relative(capacity_run_output),
        ],
        run_directory=run_directory,
        run_output=capacity_run_output,
        final_output=artifact_directory / f"{source_commit}-windows-capacity.json",
        timeout=7_200,
    )
    stress_run_output = ROOT / run_directory / "windows-stress.json"
    stress_argv = [
        str(Path(sys.executable).resolve()),
        "scripts/run_v3_windows_stress.py",
        "--output",
        _relative(stress_run_output),
    ]
    for model in local_models:
        stress_argv.extend(["--local-model", model])
    stress_argv.extend(["--duration-seconds", str(stress_seconds)])
    stress = _machine_proof(
        "thermal_memory_storage_stress",
        "windows_stress_benchmark",
        stress_argv,
        run_directory=run_directory,
        run_output=stress_run_output,
        final_output=artifact_directory / f"{source_commit}-windows-stress.json",
        timeout=3_600,
    )
    backup = _pytest_proof(
        "database_backup_restore",
        "backup_restore_tests",
        ["tests/v3/system/test_backup_restore.py"],
        run_directory=run_directory,
        timeout=3_600,
    )
    bundle = _pytest_proof(
        "bundle_model_integrity",
        "bundle_integrity_tests",
        [
            "tests/v3/integration/test_bundle_publication.py",
            "tests/v3/integration/test_ml_artifact_loading.py",
            "tests/v3/evals/test_factory_audit_isolation.py",
        ],
        run_directory=run_directory,
        timeout=3_600,
    )
    proofs = [
        installed,
        dependency,
        consumer,
        replay,
        equity,
        provider,
        recovery,
        result_to_ready,
        capacity,
        stress,
        backup,
        bundle,
    ]
    if tuple(item["name"] for item in proofs) != REQUIRED_RELEASE_EVIDENCE:
        raise RuntimeError("release proof construction order differs")
    if tuple(operation for item in proofs for operation in PROOF_OPERATIONS[item["name"]]) == ():
        raise RuntimeError("release proof operation registry is empty")
    require_clean_release_inputs(ROOT)
    if source_tree_digest(ROOT) != source_digest:
        raise RuntimeError("release source changed while executable evidence was running")
    lock_sha, versions_digest = dependency_snapshot(ROOT / "requirements/v3-release.lock")
    payload = {
        "schema_version": EVIDENCE_SCHEMA,
        "source_commit": source_commit,
        "source_tree_digest": source_digest,
        "platform": platform.platform(),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": sys.version.split()[0],
        "run_directory": run_directory,
        "dependency_lock_sha256": lock_sha,
        "dependency_versions_digest": versions_digest,
        "consumer_contract_sha256": sha256_file(
            ROOT / "strathmark/v3/contracts/v3_consumer.openapi.json"
        ),
        "wheel": wheel_identity(candidate, root=ROOT),
        "proofs": proofs,
    }
    signer = P256EphemeralSigner.generate("integrity-key:v3-executable-evidence-rehearsal")
    envelope = create_evidence_envelope(payload, signer=signer, created_at=_utc_now())
    write_canonical_envelope(output, envelope)
    return envelope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--wheel-directory", type=Path, default=DEFAULT_WHEEL_DIRECTORY)
    parser.add_argument("--artifact-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    parser.add_argument("--local-model", action="append")
    parser.add_argument("--stress-seconds", type=int, default=30)
    parser.add_argument("--list-proofs", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.list_proofs:
        print(json.dumps(PROOF_OPERATIONS, sort_keys=True, separators=(",", ":")))
        return 0
    if not arguments.local_model:
        parser.error("at least one --local-model is required")
    try:
        envelope = build_evidence(
            output=arguments.output,
            wheel_directory=arguments.wheel_directory,
            artifact_directory=arguments.artifact_directory,
            local_models=tuple(arguments.local_model),
            stress_seconds=arguments.stress_seconds,
        )
    except Exception as exc:
        print(json.dumps({"result": "failed", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "result": "passed",
                "output": str(arguments.output.resolve()),
                "evidence_manifest_digest": envelope["evidence_manifest"]["body_digest"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
