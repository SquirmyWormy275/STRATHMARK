"""Verify exact executable V3 release evidence without changing authority."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.release_evidence import (
        canonical_json_bytes,
        evidence_receipt_digests,
        load_canonical_envelope,
        verify_evidence_envelope,
    )
except ModuleNotFoundError:  # direct ``python scripts/verify_v3_release.py`` execution
    from release_evidence import (
        canonical_json_bytes,
        evidence_receipt_digests,
        load_canonical_envelope,
        verify_evidence_envelope,
    )

from strathmark.v3.application.cutover import (
    REQUIRED_RELEASE_EVIDENCE,
    EvidenceReceipt,
    ReleaseTier,
    create_release_attestation,
    verify_release_attestation,
)
from strathmark.v3.infrastructure.integrity import (
    IntegrityKeyIdentity,
    IntegrityTrustStore,
    P256EphemeralSigner,
    SignedManifest,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / "benchmarks/v3/v3_executable_evidence.json"
DEFAULT_ATTESTATION = ROOT / "benchmarks/v3/v3_release_attestation.json"


def expected_evidence(payload: dict[str, Any]) -> tuple[EvidenceReceipt, ...]:
    digests = evidence_receipt_digests(payload)
    proofs = {item["name"]: item for item in payload["proofs"]}
    return tuple(
        EvidenceReceipt(name, "passed", digests[name], proofs[name]["observed_at"])
        for name in REQUIRED_RELEASE_EVIDENCE
    )


def _wheel_from_untrusted_envelope(envelope: dict[str, Any]) -> Path:
    """Locate the candidate for signature verification without trusting its contents."""

    manifest = SignedManifest.from_dict(envelope["evidence_manifest"])
    body = manifest.body()
    try:
        relative = body["payload"]["wheel"]["path"]
    except (KeyError, TypeError) as exc:
        raise ValueError("executable evidence wheel path is missing") from exc
    if not isinstance(relative, str):
        raise ValueError("executable evidence wheel path differs")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("executable evidence wheel path escaped the repository")
    resolved = (ROOT / path).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError("executable evidence wheel path escaped the repository") from exc
    return resolved


def load_verified_executable_evidence(
    *, evidence_path: Path = DEFAULT_EVIDENCE, wheel_path: Path | None = None
) -> tuple[dict[str, Any], SignedManifest, Path]:
    envelope = load_canonical_envelope(evidence_path)
    candidate = _wheel_from_untrusted_envelope(envelope) if wheel_path is None else wheel_path
    payload, manifest = verify_evidence_envelope(envelope, root=ROOT, wheel_path=candidate)
    return payload, manifest, candidate


def verify_release_files(
    *,
    evidence_path: Path = DEFAULT_EVIDENCE,
    wheel_path: Path | None = None,
    attestation_path: Path = DEFAULT_ATTESTATION,
    require_production: bool = False,
    trusted_production_identity: IntegrityKeyIdentity | None = None,
) -> dict[str, Any]:
    evidence_payload, evidence_manifest, candidate = load_verified_executable_evidence(
        evidence_path=evidence_path, wheel_path=wheel_path
    )
    wrapper = json.loads(attestation_path.read_text("utf-8"))
    if (
        set(wrapper) != {"schema_version", "signer_identity", "attestation"}
        or wrapper["schema_version"] != "strathmark-v3-release-attestation-envelope-v1"
    ):
        raise ValueError("release attestation envelope differs")
    identity = IntegrityKeyIdentity.from_dict(wrapper["signer_identity"])
    attestation = SignedManifest.from_dict(wrapper["attestation"])
    try:
        claimed_tier = ReleaseTier(attestation.body()["payload"]["tier"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("release attestation tier differs") from exc
    if claimed_tier is ReleaseTier.PRODUCTION:
        if trusted_production_identity is None:
            raise ValueError("production_trust_identity_required")
        if identity != trusted_production_identity:
            raise ValueError("production release signer differs from pinned trust identity")
        trust_identity = trusted_production_identity
    else:
        trust_identity = identity
    payload = verify_release_attestation(
        attestation,
        trust_store=IntegrityTrustStore((trust_identity,)),
    )
    expected = expected_evidence(evidence_payload)
    observed = tuple(
        EvidenceReceipt(item["name"], item["result"], item["artifact_digest"], item["observed_at"])
        for item in payload["evidence"]
    )
    if observed != expected:
        raise ValueError("release attestation differs from executable proof receipts")
    if payload["source_commit"] != evidence_payload["source_commit"]:
        raise ValueError("release attestation source commit differs from executable evidence")
    if payload["platform"] != evidence_payload["platform"]:
        raise ValueError("release attestation platform differs from executable evidence")
    if require_production and payload["tier"] != ReleaseTier.PRODUCTION.value:
        raise ValueError("production_attestation_required")
    return {
        "schema_version": "strathmark-v3-release-verification-v2",
        "result": "passed",
        "tier": payload["tier"],
        "source_commit": payload["source_commit"],
        "attestation_digest": attestation.body_digest,
        "executable_evidence_digest": evidence_manifest.body_digest,
        "installed_wheel": candidate.relative_to(ROOT).as_posix(),
        "installed_wheel_digest": evidence_payload["wheel"]["sha256"],
        "evidence_count": len(observed),
        "authority_changed": False,
    }


def build_rehearsal_envelope(
    *,
    source_commit: str,
    evidence_path: Path = DEFAULT_EVIDENCE,
    wheel_path: Path | None = None,
) -> dict[str, Any]:
    evidence_payload, _manifest, _candidate = load_verified_executable_evidence(
        evidence_path=evidence_path, wheel_path=wheel_path
    )
    exact_commit = evidence_payload["source_commit"]
    if not exact_commit.startswith(source_commit):
        raise ValueError("requested rehearsal source commit differs from executable evidence")
    signer = P256EphemeralSigner.generate("integrity-key:v3-release-rehearsal")
    proofs = evidence_payload["proofs"]
    created_at = max(item["observed_at"] for item in proofs)
    attestation = create_release_attestation(
        evidence=expected_evidence(evidence_payload),
        source_commit=exact_commit,
        platform=evidence_payload["platform"],
        tier=ReleaseTier.REHEARSAL,
        signer=signer,
        created_at=created_at,
    )
    return {
        "schema_version": "strathmark-v3-release-attestation-envelope-v1",
        "signer_identity": signer.identity.to_dict(),
        "attestation": attestation.to_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--attestation", type=Path, default=DEFAULT_ATTESTATION)
    parser.add_argument("--require-production", action="store_true")
    parser.add_argument(
        "--trusted-production-identity",
        type=Path,
        help="operator-pinned public CNG identity JSON; never read from the attestation",
    )
    parser.add_argument("--emit-rehearsal", metavar="SOURCE_COMMIT")
    parser.add_argument("--output-attestation", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.output_attestation is not None and arguments.emit_rehearsal is None:
            raise ValueError("attestation output requires --emit-rehearsal")
        if arguments.emit_rehearsal is not None:
            result = build_rehearsal_envelope(
                source_commit=arguments.emit_rehearsal,
                evidence_path=arguments.evidence,
                wheel_path=arguments.wheel,
            )
            if arguments.output_attestation is not None:
                arguments.output_attestation.parent.mkdir(parents=True, exist_ok=True)
                arguments.output_attestation.write_bytes(canonical_json_bytes(result))
                result = {
                    "schema_version": "strathmark-v3-release-attestation-write-v1",
                    "result": "passed",
                    "tier": "rehearsal",
                    "output": str(arguments.output_attestation.resolve()),
                    "authority_changed": False,
                }
        else:
            trusted_production_identity = None
            if arguments.trusted_production_identity is not None:
                trusted_value = json.loads(
                    arguments.trusted_production_identity.read_text(encoding="utf-8")
                )
                trusted_production_identity = IntegrityKeyIdentity.from_dict(trusted_value)
            result = verify_release_files(
                evidence_path=arguments.evidence,
                wheel_path=arguments.wheel,
                attestation_path=arguments.attestation,
                require_production=arguments.require_production,
                trusted_production_identity=trusted_production_identity,
            )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "strathmark-v3-release-verification-v2",
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
