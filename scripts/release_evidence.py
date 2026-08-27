"""Closed executable-evidence contract for the V3 release verifier.

This module deliberately separates *execution* from ordinary release verification.
The long-running evidence runner records what was actually executed and signs one
canonical manifest.  The ordinary verifier then checks that receipt, the current
source inputs, and the immutable wheel without pretending that source files are test
results.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from strathmark.v3.application.cutover import REQUIRED_RELEASE_EVIDENCE
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.infrastructure.integrity import (
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

EVIDENCE_ENVELOPE_SCHEMA = "strathmark-v3-executable-evidence-envelope-v1"
EVIDENCE_SCHEMA = "strathmark-v3-executable-release-evidence-v1"
EVIDENCE_KIND = "v3_executable_release_evidence"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RUN_DIRECTORY = re.compile(r"^\.tmp/v3-release-evidence-[0-9a-f]{32}$")
_WHEEL_NAME = re.compile(
    r"^strathmark-(?P<version>[A-Za-z0-9_.!+-]+)-[^/\\]+\.whl$",
    re.IGNORECASE,
)

# Generated sidecars are excluded to avoid the impossible requirement that a signed
# attestation contain its own digest.  Every executable input and packaged release
# input remains inside the release-source digest.
GENERATED_SIDECARS = frozenset(
    {
        "benchmarks/v3/v3_executable_evidence.json",
        "benchmarks/v3/v3_release_attestation.json",
    }
)
GENERATED_PREFIXES = ("benchmarks/v3/release_evidence/", "dist/v3-release/")

PROOF_OPERATIONS: dict[str, tuple[str, ...]] = {
    "installed_artifact": ("wheel_build", "wheel_install", "installed_probe"),
    "dependency_lock": ("dependency_probe",),
    "consumer_contract": ("consumer_contract_tests",),
    "full_causal_replay": ("causal_replay_tests",),
    "manipulation_equity_slices": ("manipulation_equity_tests",),
    "provider_failure_matrix": ("provider_failure_tests",),
    "race_day_recovery": ("race_day_recovery_tests",),
    "result_to_ready": ("result_to_ready_benchmark",),
    "windows_capacity": ("windows_capacity_benchmark",),
    "thermal_memory_storage_stress": ("windows_stress_benchmark",),
    "database_backup_restore": ("backup_restore_tests",),
    "bundle_model_integrity": ("bundle_integrity_tests",),
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def git_source_paths(root: Path) -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("release source paths require a readable Git checkout")
    paths: list[Path] = []
    for raw in completed.stdout.decode("utf-8").split("\0"):
        normalized = raw.replace("\\", "/")
        if not raw or normalized in GENERATED_SIDECARS or normalized.startswith(GENERATED_PREFIXES):
            continue
        path = root / raw
        if path.is_file():
            paths.append(path)
    if not paths:
        raise ValueError("release source path set is empty")
    return tuple(sorted(paths, key=lambda item: item.relative_to(root).as_posix()))


def source_tree_digest(root: Path) -> str:
    return canonical_digest(
        {path.relative_to(root).as_posix(): sha256_file(path) for path in git_source_paths(root)}
    )


def git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or _COMMIT.fullmatch(value) is None:
        raise ValueError("release evidence requires a full Git source commit")
    return value


def verify_source_commit(root: Path, source_commit: str) -> None:
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{source_commit}^{{commit}}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        raise ValueError("executable evidence source commit is unavailable")
    changed = subprocess.run(
        ["git", "diff", "--name-only", source_commit, "--"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if changed.returncode != 0:
        raise ValueError("executable evidence source commit could not be compared")
    material = [
        normalized
        for item in changed.stdout.splitlines()
        if item
        for normalized in (item.replace("\\", "/"),)
        if normalized not in GENERATED_SIDECARS and not normalized.startswith(GENERATED_PREFIXES)
    ]
    if material:
        raise ValueError("executable evidence source commit is stale")


def require_clean_release_inputs(root: Path) -> None:
    """Refuse evidence generation from modified or untracked release inputs."""

    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("release source cleanliness could not be determined")
    dirty: list[str] = []
    for line in completed.stdout.splitlines():
        status = line[:2]
        path_text = line[3:].strip().strip('"').replace("\\", "/")
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        generated = path_text in GENERATED_SIDECARS or path_text.startswith(GENERATED_PREFIXES)
        untracked_release_input = status == "??" and path_text.startswith(
            ("strathmark/", "scripts/", "tests/v3/", "requirements/", "benchmarks/v3/")
        )
        if not generated and (status != "??" or untracked_release_input):
            dirty.append(path_text)
    if dirty:
        raise ValueError("release evidence generation requires committed source inputs")


def dependency_snapshot(lock_path: Path) -> tuple[str, str]:
    locked: dict[str, str] = {}
    installed: dict[str, str] = {}
    for raw in lock_path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            raise ValueError("V3 dependency lock contains a non-exact requirement")
        name, expected = line.split("==")
        canonical = re.sub(r"[-_.]+", "-", name).lower()
        if not canonical or not expected or canonical in locked:
            raise ValueError("V3 dependency lock contains an invalid or duplicate package")
        locked[canonical] = expected
        try:
            observed = version(name)
        except PackageNotFoundError as exc:
            raise ValueError("V3 dependency lock package is not installed") from exc
        if observed != expected:
            raise ValueError("V3 dependency lock differs from the installed environment")
        installed[canonical] = observed
    if not locked:
        raise ValueError("V3 dependency lock is empty")
    return sha256_file(lock_path), canonical_digest(installed)


def wheel_identity(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    if not path.is_file() or _WHEEL_NAME.fullmatch(path.name) is None:
        raise ValueError("installed artifact must be a concrete STRATHMARK wheel")
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError("installed artifact has no unique wheel metadata")
            metadata = archive.read(metadata_names[0]).decode("utf-8")
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ValueError("installed artifact is not a readable wheel") from exc
    name = next((line[6:] for line in metadata.splitlines() if line.startswith("Name: ")), None)
    release_version = next(
        (line[9:] for line in metadata.splitlines() if line.startswith("Version: ")),
        None,
    )
    if name is None or re.sub(r"[-_.]+", "-", name).lower() != "strathmark":
        raise ValueError("installed artifact distribution identity differs")
    if release_version is None:
        raise ValueError("installed artifact version is missing")
    display_path = path.name
    if root is not None:
        try:
            display_path = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError("installed wheel artifact escaped the repository") from exc
    return {
        "path": display_path,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "distribution": "strathmark",
        "version": release_version,
    }


def create_evidence_envelope(
    payload: Mapping[str, Any], *, signer: P256Signer, created_at: str
) -> dict[str, Any]:
    manifest = sign_manifest(EVIDENCE_KIND, payload, signer=signer, created_at=created_at)
    return {
        "schema_version": EVIDENCE_ENVELOPE_SCHEMA,
        "signer_identity": signer.identity.to_dict(),
        "evidence_manifest": manifest.to_dict(),
    }


def write_canonical_envelope(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def load_canonical_envelope(path: Path) -> dict[str, Any]:
    encoded = path.read_bytes()
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("executable evidence is not canonical JSON") from exc
    if not isinstance(value, dict) or encoded != canonical_json_bytes(value):
        raise ValueError("executable evidence bytes are not canonical")
    return value


def verify_evidence_envelope(
    envelope: Mapping[str, Any],
    *,
    root: Path,
    wheel_path: Path,
) -> tuple[dict[str, Any], SignedManifest]:
    if (
        not isinstance(envelope, Mapping)
        or set(envelope) != {"schema_version", "signer_identity", "evidence_manifest"}
        or envelope["schema_version"] != EVIDENCE_ENVELOPE_SCHEMA
    ):
        raise ValueError("executable evidence envelope differs")
    try:
        identity = IntegrityKeyIdentity.from_dict(envelope["signer_identity"])
        manifest = SignedManifest.from_dict(envelope["evidence_manifest"])
    except (TypeError, ValueError) as exc:
        raise ValueError("executable evidence signature material differs") from exc
    if manifest.kind != EVIDENCE_KIND:
        raise ValueError("executable evidence manifest kind differs")
    payload = verify_manifest(manifest, IntegrityTrustStore((identity,)))
    _verify_payload(payload, root=root, wheel_path=wheel_path)
    return payload, manifest


def _verify_payload(payload: Mapping[str, Any], *, root: Path, wheel_path: Path) -> None:
    expected_top = {
        "schema_version",
        "source_commit",
        "source_tree_digest",
        "platform",
        "python_executable",
        "python_version",
        "run_directory",
        "dependency_lock_sha256",
        "dependency_versions_digest",
        "consumer_contract_sha256",
        "wheel",
        "proofs",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_top:
        raise ValueError("executable evidence fields differ")
    if payload["schema_version"] != EVIDENCE_SCHEMA:
        raise ValueError("executable evidence schema differs")
    if (
        not isinstance(payload["source_commit"], str)
        or _COMMIT.fullmatch(payload["source_commit"]) is None
    ):
        raise ValueError("executable evidence source commit differs")
    verify_source_commit(root, payload["source_commit"])
    _require_digest(payload["source_tree_digest"], "source tree digest")
    if payload["source_tree_digest"] != source_tree_digest(root):
        raise ValueError("executable evidence source tree is stale")
    if not isinstance(payload["platform"], str) or not payload["platform"]:
        raise ValueError("executable evidence platform differs")
    if payload["python_executable"] != str(Path(sys.executable).resolve()):
        raise ValueError("executable evidence Python executable differs")
    if payload["python_version"] != sys.version.split()[0]:
        raise ValueError("executable evidence Python version differs")
    run_directory = payload["run_directory"]
    if not isinstance(run_directory, str) or _RUN_DIRECTORY.fullmatch(run_directory) is None:
        raise ValueError("executable evidence run directory differs")

    lock_sha, versions_digest = dependency_snapshot(root / "requirements/v3-release.lock")
    if payload["dependency_lock_sha256"] != lock_sha:
        raise ValueError("executable evidence dependency lock is stale")
    if payload["dependency_versions_digest"] != versions_digest:
        raise ValueError("executable evidence dependency environment is stale")
    contract_sha = sha256_file(root / "strathmark/v3/contracts/v3_consumer.openapi.json")
    if payload["consumer_contract_sha256"] != contract_sha:
        raise ValueError("executable evidence consumer contract is stale")
    if payload["wheel"] != wheel_identity(wheel_path, root=root):
        raise ValueError("executable evidence installed wheel is missing or stale")

    proofs = payload["proofs"]
    if (
        not isinstance(proofs, list)
        or tuple(item.get("name") for item in proofs if isinstance(item, Mapping))
        != REQUIRED_RELEASE_EVIDENCE
    ):
        raise ValueError("executable evidence proof set is incomplete or unordered")
    for proof in proofs:
        _verify_proof(proof, run_directory=run_directory, wheel=payload["wheel"], root=root)


def _verify_proof(
    proof: object, *, run_directory: str, wheel: Mapping[str, Any], root: Path
) -> None:
    expected = {
        "name",
        "proof_kind",
        "observed_at",
        "result",
        "steps",
        "artifact_digests",
        "artifact_files",
        "receipt_digest",
    }
    if not isinstance(proof, Mapping) or set(proof) != expected:
        raise ValueError("executable proof fields differ")
    name = proof["name"]
    if name not in PROOF_OPERATIONS or proof["proof_kind"] != f"{name}_execution_v1":
        raise ValueError("executable proof kind differs")
    if proof["result"] != "passed":
        raise ValueError(f"executable proof failed: {name}")
    _require_utc(proof["observed_at"])
    steps = proof["steps"]
    operations = PROOF_OPERATIONS[name]
    if (
        not isinstance(steps, list)
        or tuple(step.get("operation") for step in steps if isinstance(step, Mapping)) != operations
    ):
        raise ValueError(f"executable proof command differs: {name}")
    for step in steps:
        _verify_step(step, run_directory=run_directory, wheel=wheel)
    artifacts = proof["artifact_digests"]
    if not isinstance(artifacts, Mapping) or any(
        not isinstance(key, str) or not key or _DIGEST.fullmatch(str(value)) is None
        for key, value in artifacts.items()
    ):
        raise ValueError(f"executable proof artifacts differ: {name}")
    artifact_files = proof["artifact_files"]
    if not isinstance(artifact_files, Mapping) or set(artifact_files) != set(artifacts):
        raise ValueError(f"executable proof artifact files differ: {name}")
    for role, relative in artifact_files.items():
        if not isinstance(relative, str):
            raise ValueError(f"executable proof artifact path differs: {name}")
        path = _resolve_repository_path(root, relative)
        if not path.is_file() or sha256_file(path) != artifacts[role]:
            raise ValueError(f"executable proof artifact is missing or stale: {name}")
        if name in {
            "result_to_ready",
            "windows_capacity",
            "thermal_memory_storage_stress",
        }:
            _verify_machine_receipt(name, path=path, root=root)
    if name == "installed_artifact" and artifacts != {"wheel_sha256": wheel["sha256"]}:
        raise ValueError("installed artifact proof substituted a source-only digest")
    if name == "installed_artifact" and artifact_files != {"wheel_sha256": wheel["path"]}:
        raise ValueError("installed artifact proof did not bind the candidate wheel path")
    if (
        name
        in {
            "result_to_ready",
            "windows_capacity",
            "thermal_memory_storage_stress",
        }
        and not artifacts
    ):
        raise ValueError(f"machine evidence artifact is missing: {name}")
    receipt_body = {key: value for key, value in proof.items() if key != "receipt_digest"}
    if proof["receipt_digest"] != canonical_digest(receipt_body):
        raise ValueError(f"executable proof receipt digest differs: {name}")


def _verify_step(step: object, *, run_directory: str, wheel: Mapping[str, Any]) -> None:
    expected = {
        "operation",
        "argv",
        "cwd",
        "environment",
        "exit_code",
        "duration_ms",
        "passed",
        "failed",
        "errors",
        "skipped",
        "stdout_sha256",
        "stderr_sha256",
    }
    if not isinstance(step, Mapping) or set(step) != expected:
        raise ValueError("executable proof step fields differ")
    if step["cwd"] != ".":
        raise ValueError("executable proof step working directory differs")
    argv = step["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] != str(Path(sys.executable).resolve())
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise ValueError("executable proof argv differs")
    _verify_operation_argv(str(step["operation"]), argv, run_directory=run_directory, wheel=wheel)
    environment = step["environment"]
    if not isinstance(environment, Mapping) or environment.get("STRATHMARK_TEST_DB") != "1":
        raise ValueError("executable proof did not use isolated test authority")
    for name in ("STRATHMARK_DB_PATH", "STRATHMARK_V3_DB_PATH"):
        value = environment.get(name)
        if not isinstance(value, str) or not value.startswith(f"{run_directory}/"):
            raise ValueError("executable proof database escaped its isolated run directory")
    for name in ("exit_code", "duration_ms", "passed", "failed", "errors", "skipped"):
        value = step[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("executable proof counters differ")
    if step["exit_code"] != 0 or step["passed"] < 1 or step["failed"] or step["errors"]:
        raise ValueError(f"executable proof command did not pass: {step['operation']}")
    _require_digest(step["stdout_sha256"], "proof stdout digest")
    _require_digest(step["stderr_sha256"], "proof stderr digest")


def _verify_operation_argv(
    operation: str,
    argv: Sequence[str],
    *,
    run_directory: str,
    wheel: Mapping[str, Any],
) -> None:
    python = str(Path(sys.executable).resolve())
    pytest_prefix = [python, "-m", "pytest"]
    fixed: dict[str, list[str]] = {
        "dependency_probe": [
            python,
            "scripts/probe_v3_release.py",
            "dependencies",
            "--lock",
            "requirements/v3-release.lock",
        ],
        "consumer_contract_tests": pytest_prefix
        + ["tests/v3/integration/test_v3_consumer_contract.py"],
        "causal_replay_tests": pytest_prefix + ["tests/v3/system/test_executable_replay.py"],
        "manipulation_equity_tests": pytest_prefix
        + [
            "tests/v3/evals/test_optimizer_consequences.py",
            "tests/v3/evals/test_selective_abstention.py",
            "tests/v3/integration/test_credibility_authority.py",
        ],
        "provider_failure_tests": pytest_prefix
        + [
            "tests/v3/integration/test_llm_job_adapters.py::test_provider_failure_matrix_is_typed_and_bounded",
            "tests/v3/integration/test_durable_jobs.py::test_coordinator_classifies_provider_failures",
        ],
        "race_day_recovery_tests": pytest_prefix
        + [
            "tests/v3/system/test_executable_replay.py",
            "tests/v3/system/test_critical_issue_recovery.py",
        ],
        "backup_restore_tests": pytest_prefix + ["tests/v3/system/test_backup_restore.py"],
        "bundle_integrity_tests": pytest_prefix
        + [
            "tests/v3/integration/test_bundle_publication.py",
            "tests/v3/integration/test_ml_artifact_loading.py",
            "tests/v3/evals/test_factory_audit_isolation.py",
        ],
    }
    if operation in fixed:
        expected_prefix = fixed[operation]
        if list(argv[: len(expected_prefix)]) != expected_prefix:
            raise ValueError(f"executable proof used the wrong command: {operation}")
        if operation.endswith("_tests"):
            _verify_pytest_suffix(argv[len(expected_prefix) :], run_directory)
        elif len(argv) != len(expected_prefix):
            raise ValueError(f"executable proof command has unexpected arguments: {operation}")
        return
    if operation == "wheel_build":
        expected = [python, "-m", "build", "--wheel", "--no-isolation", "--outdir"]
        if list(argv[: len(expected)]) != expected or len(argv) != len(expected) + 1:
            raise ValueError("wheel build command differs")
        if not argv[-1].startswith(f"{run_directory}/"):
            raise ValueError("wheel build output escaped the isolated run directory")
        return
    if operation == "wheel_install":
        expected = [
            python,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--disable-pip-version-check",
            "--target",
        ]
        if list(argv[: len(expected)]) != expected or len(argv) != len(expected) + 3:
            raise ValueError("wheel installation command differs")
        if not argv[len(expected)].startswith(f"{run_directory}/"):
            raise ValueError("wheel installation target escaped the isolated run directory")
        if argv[-2] != "--" or Path(argv[-1]).name != Path(str(wheel["path"])).name:
            raise ValueError("wheel installation did not bind the candidate wheel")
        return
    if operation == "installed_probe":
        expected = [python, "-I", "scripts/probe_v3_release.py", "installed-wheel"]
        if list(argv[: len(expected)]) != expected or "--installed-root" not in argv:
            raise ValueError("installed wheel probe command differs")
        return
    if operation in {
        "result_to_ready_benchmark",
        "windows_capacity_benchmark",
        "windows_stress_benchmark",
    }:
        script = {
            "result_to_ready_benchmark": "scripts/benchmark_v3_result_to_ready.py",
            "windows_capacity_benchmark": "scripts/benchmark_v3_field_assembly.py",
            "windows_stress_benchmark": "scripts/run_v3_windows_stress.py",
        }[operation]
        expected = [python, script, "--output"]
        if list(argv[:3]) != expected or len(argv) < 4:
            raise ValueError(f"machine benchmark command differs: {operation}")
        if not argv[3].startswith(f"{run_directory}/"):
            raise ValueError("machine benchmark output escaped the isolated run directory")
        if operation == "result_to_ready_benchmark" and (
            len(argv) != 6
            or argv[4] != "--work-root"
            or not argv[5].startswith(f"{run_directory}/")
        ):
            raise ValueError("result-to-ready work root escaped the isolated run directory")
        return
    raise ValueError(f"executable proof operation is unknown: {operation}")


def _verify_pytest_suffix(suffix: Sequence[str], run_directory: str) -> None:
    expected_tail = ["-q", "-p", "no:cacheprovider", "--basetemp"]
    if len(suffix) != 5 or list(suffix[:4]) != expected_tail:
        raise ValueError("release proof pytest isolation arguments differ")
    if not suffix[4].startswith(f"{run_directory}/"):
        raise ValueError("release proof pytest base directory escaped isolation")


def evidence_receipt_digests(payload: Mapping[str, Any]) -> dict[str, str]:
    proofs = payload.get("proofs")
    if not isinstance(proofs, list):
        raise ValueError("executable proof set is missing")
    return {str(proof["name"]): str(proof["receipt_digest"]) for proof in proofs}


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a SHA-256 digest")
    return value


def _require_utc(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value) is None
    ):
        raise ValueError("executable proof timestamp must be UTC milliseconds")
    return value


def _resolve_repository_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("release evidence artifact path escaped the repository")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("release evidence artifact path escaped the repository") from exc
    return resolved


def _verify_machine_receipt(name: str, *, path: Path, root: Path) -> None:
    encoded = path.read_bytes()
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"machine evidence receipt is unreadable: {name}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"machine evidence receipt is not an object: {name}")
    if name == "result_to_ready":
        _verify_result_to_ready_receipt(value, root=root)
    elif name == "windows_capacity":
        _verify_capacity_receipt(value, root=root)
    else:
        if encoded != canonical_json_bytes(value):
            raise ValueError("Windows stress receipt is not canonical JSON")
        _verify_stress_receipt(value)


def _verify_capacity_receipt(value: Mapping[str, Any], *, root: Path) -> None:
    if (
        value.get("schema_version") != "strathmark-v3-field-assembly-benchmark-v2"
        or value.get("status") != "passed"
    ):
        raise ValueError("Windows capacity execution did not pass")
    body = {key: item for key, item in value.items() if key != "manifest_digest"}
    if value.get("manifest_digest") != canonical_digest(body):
        raise ValueError("Windows capacity receipt digest differs")
    gates = value.get("gates")
    if (
        not isinstance(gates, Mapping)
        or not gates
        or any(item is not True for item in gates.values())
    ):
        raise ValueError("Windows capacity gate is incomplete or failed")
    complete = value.get("complete_confirmed_field_assembly")
    if (
        not isinstance(complete, Mapping)
        or complete.get("runs") != 100
        or complete.get("failed_runs") != 0
        or not isinstance(complete.get("observed_p99_ms"), (int, float))
        or complete["observed_p99_ms"] >= 2_000
    ):
        raise ValueError("Windows capacity field assembly evidence differs")
    identity = value.get("artifact_identity")
    paths = {
        "benchmark_script_sha256": root / "scripts/benchmark_v3_field_assembly.py",
        "fixture_source_sha256": root / "tests/v3/integration/test_field_receipts.py",
        "field_assembly_source_sha256": root / "strathmark/v3/application/field_assembly.py",
        "projection_source_sha256": root / "strathmark/v3/infrastructure/sqlite/projections.py",
        "joint_dependence_source_sha256": root / "strathmark/v3/domain/joint_dependence.py",
        "optimizer_source_sha256": root / "strathmark/v3/domain/optimizer.py",
        "native_kernel_source_sha256": root / "strathmark/v3/native/optimizer_kernel.rs",
        "native_kernel_binary_sha256": root
        / "strathmark/v3/native/strathmark_v3_optimizer_kernel.dll",
    }
    if not isinstance(identity, Mapping) or set(identity) != set(paths):
        raise ValueError("Windows capacity source pins are incomplete")
    if any(identity[name] != sha256_file(source) for name, source in paths.items()):
        raise ValueError("Windows capacity source pin is stale")


def _verify_result_to_ready_receipt(value: Mapping[str, Any], *, root: Path) -> None:
    if (
        value.get("schema_version") != "strathmark-v3-result-to-ready-benchmark-v1"
        or value.get("status") != "passed"
        or value.get("repetitions") != 5
        or value.get("limits") != {"result_to_ready_ms_inclusive": 120_000}
        or value.get("maximum_measured_result_to_ready_ms", 120_001) > 120_000
    ):
        raise ValueError("result-to-ready execution did not pass the formal budget")
    body = {key: item for key, item in value.items() if key != "manifest_digest"}
    if value.get("manifest_digest") != canonical_digest(body):
        raise ValueError("result-to-ready receipt digest differs")
    gates = value.get("gates")
    if (
        not isinstance(gates, Mapping)
        or set(gates)
        != {
            "formal_repetition_count",
            "result_to_ready_within_budget",
            "all_trials_completed",
            "exact_source_bindings",
        }
        or any(item is not True for item in gates.values())
    ):
        raise ValueError("result-to-ready gate is incomplete or failed")
    bindings = value.get("source_bindings")
    if not isinstance(bindings, Mapping) or value.get("source_bindings_digest") != canonical_digest(
        bindings
    ):
        raise ValueError("result-to-ready source binding digest differs")
    for relative, digest in bindings.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ValueError("result-to-ready source binding is invalid")
        source = _resolve_repository_path(root, relative)
        if not source.is_file() or sha256_file(source) != digest:
            raise ValueError("result-to-ready source binding is stale")
    trials = value.get("trials")
    components = {
        "final_heat_settlement",
        "deliberate_round_close",
        "newly_affected_cards",
        "gate_optimizer",
        "receipt_commit",
        "approval_projection",
    }
    if not isinstance(trials, list) or len(trials) != 5:
        raise ValueError("result-to-ready formal trials are incomplete")
    for trial in trials:
        if (
            not isinstance(trial, Mapping)
            or set(trial.get("component_latency_ms", {})) != components
            or trial.get("newly_affected_card_count") != 2
            or not isinstance(trial.get("measured_result_to_ready_ms"), int)
            or trial["measured_result_to_ready_ms"] > 120_000
        ):
            raise ValueError("result-to-ready trial evidence differs")
    if _contains_mapping_key(value, "ready_ms"):
        raise ValueError("result-to-ready evidence contains a synthetic readiness timestamp")


def _contains_mapping_key(value: object, key: str) -> bool:
    if isinstance(value, Mapping):
        return key in value or any(_contains_mapping_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_mapping_key(item, key) for item in value)
    return False


def _verify_stress_receipt(value: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "recorded_at",
        "result",
        "bounded_non_exhaustive",
        "thermal_gate_c",
        "models",
        "model_runs",
        "gpu_samples",
        "pressure_injection",
        "storage_injection",
    }
    if (
        set(value) != required
        or value["schema_version"] != "strathmark-v3-windows-stress-receipt-v1"
        or value["result"] != "passed"
        or value["bounded_non_exhaustive"] is not True
    ):
        raise ValueError("Windows stress execution did not pass")
    _require_utc(value["recorded_at"])
    models = value["models"]
    runs = value["model_runs"]
    samples = value["gpu_samples"]
    if (
        not isinstance(models, list)
        or not models
        or any(not isinstance(item, str) or not item for item in models)
        or not isinstance(runs, list)
        or any(not isinstance(item, Mapping) for item in runs)
        or set(models) - {item.get("model") for item in runs}
        or any(item.get("done") is not True for item in runs)
    ):
        raise ValueError("Windows stress local-model execution is incomplete")
    if (
        not isinstance(samples, list)
        or len(samples) < 3
        or any(not isinstance(item, Mapping) for item in samples)
        or any(item.get("temperature_c", 10_000) > value["thermal_gate_c"] for item in samples)
        or any(
            item.get("memory_used_mib", 10_000) >= item.get("memory_total_mib", 0)
            for item in samples
        )
    ):
        raise ValueError("Windows stress hardware telemetry differs")
    for section_name in ("pressure_injection", "storage_injection"):
        section = value[section_name]
        if (
            not isinstance(section, Mapping)
            or section.get("exit_code") != 0
            or not isinstance(section.get("passed"), int)
            or section["passed"] < 1
        ):
            raise ValueError(f"Windows stress {section_name} did not pass")


__all__ = [
    "EVIDENCE_ENVELOPE_SCHEMA",
    "EVIDENCE_KIND",
    "EVIDENCE_SCHEMA",
    "GENERATED_SIDECARS",
    "PROOF_OPERATIONS",
    "canonical_json_bytes",
    "create_evidence_envelope",
    "dependency_snapshot",
    "evidence_receipt_digests",
    "git_head",
    "load_canonical_envelope",
    "require_clean_release_inputs",
    "sha256_file",
    "source_tree_digest",
    "verify_evidence_envelope",
    "verify_source_commit",
    "wheel_identity",
    "write_canonical_envelope",
]
