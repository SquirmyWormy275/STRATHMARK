from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from scripts.release_evidence import (
    EVIDENCE_SCHEMA,
    PROOF_OPERATIONS,
    create_evidence_envelope,
    dependency_snapshot,
    sha256_file,
    source_tree_digest,
    verify_evidence_envelope,
    wheel_identity,
    write_canonical_envelope,
)
from scripts.verify_v3_release import (
    build_rehearsal_envelope,
    expected_evidence,
    verify_release_files,
)
from strathmark.v3.application.cutover import REQUIRED_RELEASE_EVIDENCE
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.infrastructure.integrity import (
    IntegrityKeyClass,
    IntegrityKeyIdentity,
    P256EphemeralSigner,
    SignedManifest,
    sign_manifest,
)

NOW = "2026-08-25T21:00:00.000Z"


@pytest.fixture
def release_artifact_root() -> Path:
    """Keep signed fixture artifacts inside the repository trust boundary."""

    root = Path(__file__).resolve().parents[3]
    path = root / ".tmp" / f"release-verifier-test-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _treat_current_development_tree_as_committed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit receipts use current bytes while the implementation patch is uncommitted."""

    import scripts.release_evidence as module

    root = Path(__file__).resolve().parents[3]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    original = module.verify_source_commit

    def verify(candidate_root: Path, source_commit: str) -> None:
        if source_commit == head:
            return
        original(candidate_root, source_commit)

    monkeypatch.setattr(module, "verify_source_commit", verify)


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/verify_v3_release.py", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _wheel(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "strathmark-3.0.0rc1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: strathmark\nVersion: 3.0.0rc1\n",
        )


def _step(operation: str, argv: list[str], run_directory: str) -> dict[str, object]:
    return {
        "operation": operation,
        "argv": argv,
        "cwd": ".",
        "environment": {
            "STRATHMARK_TEST_DB": "1",
            "STRATHMARK_DB_PATH": f"{run_directory}/{operation}-v2.sqlite3",
            "STRATHMARK_V3_DB_PATH": f"{run_directory}/{operation}-v3.sqlite3",
        },
        "exit_code": 0,
        "duration_ms": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
    }


def _pytest(root: Path, selectors: list[str], run_directory: str, name: str) -> list[str]:
    return [
        str(Path(sys.executable).resolve()),
        "-m",
        "pytest",
        *selectors,
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        f"{run_directory}/{name}-pytest",
    ]


def _capacity_receipt(root: Path, path: Path) -> None:
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
    body = {
        "schema_version": "strathmark-v3-field-assembly-benchmark-v2",
        "status": "passed",
        "gates": {"complete": True},
        "complete_confirmed_field_assembly": {
            "runs": 100,
            "failed_runs": 0,
            "observed_p99_ms": 100,
        },
        "artifact_identity": {name: sha256_file(value) for name, value in paths.items()},
    }
    path.write_text(
        json.dumps({**body, "manifest_digest": canonical_digest(body)}, sort_keys=True),
        encoding="utf-8",
    )


def _result_to_ready_receipt(root: Path, path: Path) -> None:
    source_paths = (
        "scripts/benchmark_v3_result_to_ready.py",
        "strathmark/v3/application/lifecycle.py",
        "strathmark/v3/application/settlement.py",
        "strathmark/v3/application/coordinator.py",
        "strathmark/v3/application/pipeline_builder.py",
        "strathmark/v3/application/field_assembly.py",
        "strathmark/v3/application/approval.py",
        "strathmark/v3/domain/optimizer.py",
        "strathmark/v3/infrastructure/sqlite/event_store.py",
        "strathmark/v3/infrastructure/sqlite/jobs.py",
        "strathmark/v3/infrastructure/sqlite/projections.py",
        "tests/v3/integration/test_field_receipts.py",
        "tests/v3/integration/test_rolling_preparation.py",
        "benchmarks/v3/job_capacity_manifest.json",
    )
    bindings = {name: sha256_file(root / name) for name in source_paths}
    components = {
        "final_heat_settlement": 1,
        "deliberate_round_close": 1,
        "newly_affected_cards": 1,
        "gate_optimizer": 1,
        "receipt_commit": 1,
        "approval_projection": 1,
    }
    trials = [
        {
            "trial_ordinal": ordinal,
            "measured_result_to_ready_ms": 6,
            "component_latency_ms": components,
            "newly_affected_card_count": 2,
        }
        for ordinal in range(1, 6)
    ]
    body = {
        "schema_version": "strathmark-v3-result-to-ready-benchmark-v1",
        "status": "passed",
        "platform": "fixture",
        "python_version": "3.13",
        "repetitions": 5,
        "limits": {"result_to_ready_ms_inclusive": 120_000},
        "gates": {
            "formal_repetition_count": True,
            "result_to_ready_within_budget": True,
            "all_trials_completed": True,
            "exact_source_bindings": True,
        },
        "maximum_measured_result_to_ready_ms": 6,
        "source_bindings": bindings,
        "source_bindings_digest": canonical_digest(bindings),
        "trials": trials,
    }
    path.write_text(
        json.dumps({**body, "manifest_digest": canonical_digest(body)}, sort_keys=True),
        encoding="utf-8",
    )


def _stress_receipt(path: Path) -> None:
    sample = {
        "observed_at": NOW,
        "gpu": "fixture",
        "memory_total_mib": 8_188,
        "memory_used_mib": 4_000,
        "temperature_c": 70,
        "power_w": 50.0,
    }
    value = {
        "schema_version": "strathmark-v3-windows-stress-receipt-v1",
        "recorded_at": NOW,
        "result": "passed",
        "bounded_non_exhaustive": True,
        "thermal_gate_c": 87,
        "models": ["model:a"],
        "model_runs": [
            {"model": "model:a", "duration_ms": 1, "response_sha256": "a" * 64, "done": True}
        ],
        "gpu_samples": [sample, sample, sample],
        "pressure_injection": {"exit_code": 0, "passed": 3},
        "storage_injection": {"exit_code": 0, "passed": 3},
    }
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _fixture(root: Path, tmp_path: Path) -> tuple[dict[str, object], Path, str]:
    run_directory = f".tmp/v3-release-evidence-{uuid4().hex}"
    wheel = tmp_path / "strathmark-3.0.0rc1-py3-none-any.whl"
    _wheel(wheel)
    relative_wheel = wheel.resolve().relative_to(root.resolve()).as_posix()
    capacity = tmp_path / "capacity.json"
    result_to_ready = tmp_path / "result-to-ready.json"
    stress = tmp_path / "stress.json"
    _capacity_receipt(root, capacity)
    _result_to_ready_receipt(root, result_to_ready)
    _stress_receipt(stress)
    relative_capacity = capacity.resolve().relative_to(root.resolve()).as_posix()
    relative_result_to_ready = result_to_ready.resolve().relative_to(root.resolve()).as_posix()
    relative_stress = stress.resolve().relative_to(root.resolve()).as_posix()
    python = str(Path(sys.executable).resolve())
    selectors = {
        "consumer_contract": ["tests/v3/integration/test_v3_consumer_contract.py"],
        "full_causal_replay": ["tests/v3/system/test_executable_replay.py"],
        "manipulation_equity_slices": [
            "tests/v3/evals/test_optimizer_consequences.py",
            "tests/v3/evals/test_selective_abstention.py",
            "tests/v3/integration/test_credibility_authority.py",
        ],
        "provider_failure_matrix": [
            "tests/v3/integration/test_llm_job_adapters.py::test_provider_failure_matrix_is_typed_and_bounded",
            "tests/v3/integration/test_durable_jobs.py::test_coordinator_classifies_provider_failures",
        ],
        "race_day_recovery": [
            "tests/v3/system/test_executable_replay.py",
            "tests/v3/system/test_critical_issue_recovery.py",
        ],
        "database_backup_restore": ["tests/v3/system/test_backup_restore.py"],
        "bundle_model_integrity": [
            "tests/v3/integration/test_bundle_publication.py",
            "tests/v3/integration/test_ml_artifact_loading.py",
            "tests/v3/evals/test_factory_audit_isolation.py",
        ],
    }
    operation_names = {
        "consumer_contract": "consumer_contract_tests",
        "full_causal_replay": "causal_replay_tests",
        "manipulation_equity_slices": "manipulation_equity_tests",
        "provider_failure_matrix": "provider_failure_tests",
        "race_day_recovery": "race_day_recovery_tests",
        "database_backup_restore": "backup_restore_tests",
        "bundle_model_integrity": "bundle_integrity_tests",
    }
    proofs = []
    for name in REQUIRED_RELEASE_EVIDENCE:
        if name == "installed_artifact":
            build_root = f"{run_directory}/wheel-build"
            steps = [
                _step(
                    "wheel_build",
                    [python, "-m", "build", "--wheel", "--no-isolation", "--outdir", build_root],
                    run_directory,
                ),
                _step(
                    "wheel_install",
                    [
                        python,
                        "-m",
                        "pip",
                        "install",
                        "--no-deps",
                        "--disable-pip-version-check",
                        "--target",
                        f"{run_directory}/installed",
                        "--",
                        f"{build_root}/{wheel.name}",
                    ],
                    run_directory,
                ),
                _step(
                    "installed_probe",
                    [
                        python,
                        "-I",
                        "scripts/probe_v3_release.py",
                        "installed-wheel",
                        "--installed-root",
                        f"{run_directory}/installed",
                        "--expected-version",
                        "3.0.0rc1",
                    ],
                    run_directory,
                ),
            ]
            files = {"wheel_sha256": relative_wheel}
        elif name == "dependency_lock":
            steps = [
                _step(
                    "dependency_probe",
                    [
                        python,
                        "scripts/probe_v3_release.py",
                        "dependencies",
                        "--lock",
                        "requirements/v3-release.lock",
                    ],
                    run_directory,
                )
            ]
            files = {}
        elif name == "result_to_ready":
            steps = [
                _step(
                    "result_to_ready_benchmark",
                    [
                        python,
                        "scripts/benchmark_v3_result_to_ready.py",
                        "--output",
                        f"{run_directory}/result-to-ready.json",
                        "--work-root",
                        f"{run_directory}/result-to-ready-work",
                    ],
                    run_directory,
                )
            ]
            files = {"machine_receipt_sha256": relative_result_to_ready}
        elif name == "windows_capacity":
            steps = [
                _step(
                    "windows_capacity_benchmark",
                    [
                        python,
                        "scripts/benchmark_v3_field_assembly.py",
                        "--output",
                        f"{run_directory}/capacity.json",
                    ],
                    run_directory,
                )
            ]
            files = {"machine_receipt_sha256": relative_capacity}
        elif name == "thermal_memory_storage_stress":
            steps = [
                _step(
                    "windows_stress_benchmark",
                    [
                        python,
                        "scripts/run_v3_windows_stress.py",
                        "--output",
                        f"{run_directory}/stress.json",
                        "--local-model",
                        "model:a",
                    ],
                    run_directory,
                )
            ]
            files = {"machine_receipt_sha256": relative_stress}
        else:
            operation = operation_names[name]
            steps = [
                _step(operation, _pytest(root, selectors[name], run_directory, name), run_directory)
            ]
            files = {}
        body = {
            "name": name,
            "proof_kind": f"{name}_execution_v1",
            "observed_at": NOW,
            "result": "passed",
            "steps": steps,
            "artifact_digests": {role: sha256_file(root / path) for role, path in files.items()},
            "artifact_files": files,
        }
        proofs.append({**body, "receipt_digest": canonical_digest(body)})
    lock_sha, versions = dependency_snapshot(root / "requirements/v3-release.lock")
    payload = {
        "schema_version": EVIDENCE_SCHEMA,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "source_tree_digest": source_tree_digest(root),
        "platform": "windows-11-x86_64-python-3.13",
        "python_executable": python,
        "python_version": sys.version.split()[0],
        "run_directory": run_directory,
        "dependency_lock_sha256": lock_sha,
        "dependency_versions_digest": versions,
        "consumer_contract_sha256": sha256_file(
            root / "strathmark/v3/contracts/v3_consumer.openapi.json"
        ),
        "wheel": wheel_identity(wheel, root=root),
        "proofs": proofs,
    }
    signer = P256EphemeralSigner.generate("integrity-key:test-executable-evidence")
    return create_evidence_envelope(payload, signer=signer, created_at=NOW), wheel, run_directory


def _resign(envelope: dict[str, object], mutation) -> dict[str, object]:
    from strathmark.v3.infrastructure.integrity import SignedManifest

    payload = SignedManifest.from_dict(envelope["evidence_manifest"]).body()["payload"]  # type: ignore[arg-type,index]
    changed = copy.deepcopy(payload)
    mutation(changed)
    signer = P256EphemeralSigner.generate("integrity-key:test-executable-evidence-resigned")
    return create_evidence_envelope(changed, signer=signer, created_at=NOW)


def _redigest(proof: dict[str, object]) -> None:
    proof["receipt_digest"] = canonical_digest(
        {key: value for key, value in proof.items() if key != "receipt_digest"}
    )


def test_signed_executable_receipt_validates_exact_current_inputs(
    release_artifact_root: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    envelope, wheel, _run_directory = _fixture(root, release_artifact_root)
    payload, manifest = verify_evidence_envelope(envelope, root=root, wheel_path=wheel)
    assert tuple(item["name"] for item in payload["proofs"]) == REQUIRED_RELEASE_EVIDENCE
    assert len(manifest.body_digest) == 64
    assert all(PROOF_OPERATIONS[item["name"]] for item in payload["proofs"])


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value.__setitem__("source_tree_digest", "f" * 64), "source tree is stale"),
        (
            lambda value: value.__setitem__("source_commit", "0" * 40),
            "source commit is unavailable",
        ),
        (
            lambda value: (
                value["proofs"][1]["steps"][0].__setitem__(
                    "argv", [str(Path(sys.executable).resolve()), "fake.py"]
                ),
                _redigest(value["proofs"][1]),
            ),
            "wrong command",
        ),
        (
            lambda value: (
                value["proofs"][2]["steps"][0].__setitem__("exit_code", 1),
                _redigest(value["proofs"][2]),
            ),
            "did not pass",
        ),
        (
            lambda value: (
                value["proofs"][2]["steps"][0].__setitem__("failed", 1),
                _redigest(value["proofs"][2]),
            ),
            "did not pass",
        ),
        (
            lambda value: (
                value["proofs"][0].__setitem__(
                    "artifact_digests", {"wheel_sha256": value["source_tree_digest"]}
                ),
                _redigest(value["proofs"][0]),
            ),
            "missing or stale",
        ),
        (
            lambda value: (
                value["proofs"][3].__setitem__("steps", []),
                _redigest(value["proofs"][3]),
            ),
            "command differs",
        ),
    ],
)
def test_receipt_rejects_stale_fake_failed_wrong_missing_and_source_only_rows(
    release_artifact_root: Path, mutation, match: str
) -> None:
    root = Path(__file__).resolve().parents[3]
    envelope, wheel, _run_directory = _fixture(root, release_artifact_root)
    changed = _resign(envelope, mutation)
    with pytest.raises(ValueError, match=match):
        verify_evidence_envelope(changed, root=root, wheel_path=wheel)


def test_receipt_rejects_signature_and_canonical_file_tamper(
    tmp_path: Path, release_artifact_root: Path
) -> None:
    root = Path(__file__).resolve().parents[3]
    envelope, wheel, _run_directory = _fixture(root, release_artifact_root)
    envelope["evidence_manifest"]["signature_der_b64"] = "AAAA"  # type: ignore[index]
    with pytest.raises(Exception, match="signature"):
        verify_evidence_envelope(envelope, root=root, wheel_path=wheel)
    path = tmp_path / "evidence.json"
    write_canonical_envelope(path, _fixture(root, release_artifact_root)[0])
    path.write_bytes(path.read_bytes() + b"\n")
    completed = _run(root, "--evidence", str(path), "--emit-rehearsal", "54ab593")
    assert completed.returncode == 2
    assert "not canonical" in json.loads(completed.stderr)["reason"]


def test_rehearsal_generation_requires_current_executable_receipt_and_production_refuses(
    tmp_path: Path,
    release_artifact_root: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    missing = _run(
        root, "--evidence", str(tmp_path / "missing.json"), "--emit-rehearsal", "54ab593"
    )
    assert missing.returncode == 2
    envelope, wheel, _run_directory = _fixture(root, release_artifact_root)
    evidence_path = tmp_path / "valid-evidence.json"
    write_canonical_envelope(evidence_path, envelope)
    from strathmark.v3.infrastructure.integrity import SignedManifest

    evidence_payload = SignedManifest.from_dict(envelope["evidence_manifest"]).body()["payload"]  # type: ignore[arg-type,index]
    rehearsal = build_rehearsal_envelope(
        source_commit=evidence_payload["source_commit"][:7],
        evidence_path=evidence_path,
        wheel_path=wheel,
    )
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_text(json.dumps(rehearsal), encoding="utf-8")
    report = verify_release_files(
        evidence_path=evidence_path,
        wheel_path=wheel,
        attestation_path=attestation_path,
    )
    assert report["result"] == "passed"
    assert report["authority_changed"] is False
    with pytest.raises(ValueError, match="production_attestation_required"):
        verify_release_files(
            evidence_path=evidence_path,
            wheel_path=wheel,
            attestation_path=attestation_path,
            require_production=True,
        )
    assert len(expected_evidence(evidence_payload)) == 12


def test_production_attestation_cannot_supply_its_own_forged_trust_identity(
    tmp_path: Path,
    release_artifact_root: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    envelope, wheel, _run_directory = _fixture(root, release_artifact_root)
    evidence_path = tmp_path / "valid-evidence.json"
    write_canonical_envelope(evidence_path, envelope)
    evidence_payload = SignedManifest.from_dict(envelope["evidence_manifest"]).body()["payload"]  # type: ignore[arg-type,index]
    attacker = P256EphemeralSigner.generate("integrity-key:attacker-release")
    forged_identity = IntegrityKeyIdentity(
        attacker.identity.key_id,
        IntegrityKeyClass.PRODUCTION_CNG,
        "windows_cng_p256_sha256",
        attacker.identity.public_key_der_b64,
    )
    forged = sign_manifest(
        "v3_release_attestation",
        {
            "schema_version": "strathmark-v3-release-attestation-v1",
            "tier": "production",
            "source_commit": evidence_payload["source_commit"],
            "platform": evidence_payload["platform"],
            "evidence": [item.to_dict() for item in expected_evidence(evidence_payload)],
        },
        signer=attacker,
        created_at=NOW,
    )
    attestation_path = tmp_path / "forged-production.json"
    attestation_path.write_text(
        json.dumps(
            {
                "schema_version": "strathmark-v3-release-attestation-envelope-v1",
                "signer_identity": forged_identity.to_dict(),
                "attestation": forged.to_dict(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="production_trust_identity_required"):
        verify_release_files(
            evidence_path=evidence_path,
            wheel_path=wheel,
            attestation_path=attestation_path,
            require_production=True,
        )

    pinned_key = P256EphemeralSigner.generate("integrity-key:pinned-release")
    pinned_identity = IntegrityKeyIdentity(
        pinned_key.identity.key_id,
        IntegrityKeyClass.PRODUCTION_CNG,
        "windows_cng_p256_sha256",
        pinned_key.identity.public_key_der_b64,
    )
    with pytest.raises(ValueError, match="differs from pinned trust identity"):
        verify_release_files(
            evidence_path=evidence_path,
            wheel_path=wheel,
            attestation_path=attestation_path,
            require_production=True,
            trusted_production_identity=pinned_identity,
        )
