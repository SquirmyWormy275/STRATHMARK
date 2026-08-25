"""Verify the closed V3 rehearsal/release evidence set without changing authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from replay_v3 import build_replay_report

from strathmark.v3.application.cutover import (
    REQUIRED_RELEASE_EVIDENCE,
    EvidenceReceipt,
    ReleaseTier,
    create_release_attestation,
    verify_release_attestation,
    verify_windows_capacity_manifest,
)
from strathmark.v3.contracts.canonical import canonical_digest
from strathmark.v3.infrastructure.integrity import (
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256EphemeralSigner,
    SignedManifest,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPACITY = ROOT / "benchmarks" / "v3" / "windows_capacity_manifest.json"
DEFAULT_ATTESTATION = ROOT / "benchmarks" / "v3" / "v3_release_attestation.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(paths: tuple[Path, ...]) -> str:
    return canonical_digest(
        {
            path.relative_to(ROOT).as_posix(): _sha(path)
            for path in sorted(paths, key=lambda item: item.as_posix())
        }
    )


def _verify_dependency_lock(path: Path) -> None:
    locked: dict[str, str] = {}
    for raw in path.read_text("utf-8").splitlines():
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
    if not locked:
        raise ValueError("V3 dependency lock is empty")


def expected_evidence(
    capacity: dict[str, Any], *, capacity_path: Path = DEFAULT_CAPACITY
) -> tuple[EvidenceReceipt, ...]:
    replay = build_replay_report()
    observed_at = capacity["recorded_at"]
    dependency_paths = (
        ROOT / "pyproject.toml",
        ROOT / "requirements" / "v3-release.lock",
        ROOT / "requirements" / "api-oldest.txt",
        ROOT / "requirements" / "api-current.txt",
    )
    package_paths = tuple(
        path
        for path in (ROOT / "strathmark" / "v3").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pdb", ".lib", ".exp"}
    )
    equity_paths = (
        ROOT / "tests" / "v3" / "evals" / "test_optimizer_consequences.py",
        ROOT / "tests" / "v3" / "evals" / "test_selective_abstention.py",
        ROOT / "tests" / "v3" / "integration" / "test_credibility_authority.py",
    )
    backup_paths = (
        ROOT / "strathmark" / "v3" / "infrastructure" / "backup.py",
        ROOT / "strathmark" / "v3" / "infrastructure" / "integrity.py",
        ROOT / "tests" / "v3" / "system" / "test_backup_restore.py",
        ROOT / "tests" / "v3" / "system" / "test_critical_issue_recovery.py",
    )
    bundle_paths = tuple(
        path
        for path in (ROOT / "benchmarks" / "v3").glob("*.json")
        if path.name not in {"windows_capacity_manifest.json", "v3_release_attestation.json"}
    ) + tuple(
        path for path in (ROOT / "strathmark" / "v3").rglob("*manifest*.json") if path.is_file()
    )
    evidence_digests = {
        "installed_artifact": _tree_digest((ROOT / "pyproject.toml", *package_paths)),
        "dependency_lock": _tree_digest(dependency_paths),
        "consumer_contract": _sha(
            ROOT / "strathmark" / "v3" / "contracts" / "v3_consumer.openapi.json"
        ),
        "full_causal_replay": str(replay["report_digest"]),
        "manipulation_equity_slices": _tree_digest(equity_paths),
        "provider_failure_matrix": str(replay["recovery_digest"]),
        "race_day_recovery": str(replay["race_day_digest"]),
        "windows_capacity": _sha(capacity_path),
        "thermal_memory_storage_stress": canonical_digest(capacity["stress_matrix"]),
        "database_backup_restore": _tree_digest(backup_paths),
        "bundle_model_integrity": _tree_digest(bundle_paths),
    }
    return tuple(
        EvidenceReceipt(name, "passed", evidence_digests[name], observed_at)
        for name in REQUIRED_RELEASE_EVIDENCE
    )


def verify_release_files(
    *,
    capacity_path: Path = DEFAULT_CAPACITY,
    attestation_path: Path = DEFAULT_ATTESTATION,
    require_production: bool = False,
) -> dict[str, Any]:
    _verify_dependency_lock(ROOT / "requirements" / "v3-release.lock")
    capacity = verify_windows_capacity_manifest(json.loads(capacity_path.read_text("utf-8")))
    pins = capacity["artifact_pins"]
    pin_paths = {
        "field_assembly_manifest_sha256": ROOT
        / "benchmarks"
        / "v3"
        / "field_assembly_manifest.json",
        "rolling_restart_manifest_sha256": ROOT
        / "benchmarks"
        / "v3"
        / "rolling_restart_manifest.json",
        "job_capacity_manifest_sha256": ROOT / "benchmarks" / "v3" / "job_capacity_manifest.json",
    }
    if any(_sha(pin_paths[name]) != digest for name, digest in pins.items()):
        raise ValueError("Windows capacity artifact pin mismatch")
    wrapper = json.loads(attestation_path.read_text("utf-8"))
    if (
        set(wrapper) != {"schema_version", "signer_identity", "attestation"}
        or wrapper["schema_version"] != "strathmark-v3-release-attestation-envelope-v1"
    ):
        raise ValueError("release attestation envelope differs")
    identity = IntegrityKeyIdentity.from_dict(wrapper["signer_identity"])
    attestation = SignedManifest.from_dict(wrapper["attestation"])
    payload = verify_release_attestation(
        attestation,
        trust_store=IntegrityTrustStore((identity,)),
    )
    expected = expected_evidence(capacity, capacity_path=capacity_path)
    observed = tuple(
        EvidenceReceipt(item["name"], item["result"], item["artifact_digest"], item["observed_at"])
        for item in payload["evidence"]
    )
    if observed != expected:
        raise ValueError("release evidence differs from the exact current artifacts")
    if require_production and payload["tier"] != ReleaseTier.PRODUCTION.value:
        raise ValueError("production_attestation_required")
    return {
        "schema_version": "strathmark-v3-release-verification-v1",
        "result": "passed",
        "tier": payload["tier"],
        "attestation_digest": attestation.body_digest,
        "capacity_manifest_digest": _sha(capacity_path),
        "evidence_count": len(observed),
        "authority_changed": False,
    }


def build_rehearsal_envelope(*, source_commit: str) -> dict[str, Any]:
    capacity = verify_windows_capacity_manifest(json.loads(DEFAULT_CAPACITY.read_text("utf-8")))
    signer = P256EphemeralSigner.generate("integrity-key:v3-release-rehearsal")
    attestation = create_release_attestation(
        evidence=expected_evidence(capacity),
        source_commit=source_commit,
        platform="windows-11-x86_64-python-3.13",
        tier=ReleaseTier.REHEARSAL,
        signer=signer,
        created_at=capacity["recorded_at"],
    )
    return {
        "schema_version": "strathmark-v3-release-attestation-envelope-v1",
        "signer_identity": signer.identity.to_dict(),
        "attestation": attestation.to_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capacity", type=Path, default=DEFAULT_CAPACITY)
    parser.add_argument("--attestation", type=Path, default=DEFAULT_ATTESTATION)
    parser.add_argument("--require-production", action="store_true")
    parser.add_argument("--emit-rehearsal", metavar="SOURCE_COMMIT")
    arguments = parser.parse_args(argv)
    if arguments.emit_rehearsal is not None:
        print(
            json.dumps(
                build_rehearsal_envelope(source_commit=arguments.emit_rehearsal),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    try:
        result = verify_release_files(
            capacity_path=arguments.capacity,
            attestation_path=arguments.attestation,
            require_production=arguments.require_production,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "strathmark-v3-release-verification-v1",
                    "result": "failed",
                    "reason": str(exc),
                    "authority_changed": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
