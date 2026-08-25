"""Frozen, one-use causal evaluation for V3 factory candidates."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from strathmark.v3.contracts.canonical import (
    canonical_bytes,
    canonical_decimal_string,
    canonical_digest,
)
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.factory.candidates import CandidateBundle
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    sign_manifest,
    verify_manifest,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
_COMPARATORS = frozenset({"gte", "lte"})


class EvaluationError(RuntimeError):
    """A frozen evaluation or audit-isolation rule failed closed."""


class FactoryServiceRole(str, Enum):
    BUILDER = "builder"
    EVALUATOR = "evaluator"
    BUNDLE_SIGNER = "bundle_signer"
    APPLICATION = "application"


@dataclass(frozen=True, slots=True, order=True)
class IsolationProbe:
    role: FactoryServiceRole
    principal_id: str
    can_read_candidate_inputs: bool
    can_write_candidate_artifacts: bool
    can_read_locked_audit: bool
    can_read_raw_audit_rows: bool
    can_use_bundle_private_key: bool
    network_allowed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.role, FactoryServiceRole):
            raise EvaluationError("isolation probe role must use the closed vocabulary")
        _token(self.principal_id, "OS service principal")
        capabilities = (
            self.can_read_candidate_inputs,
            self.can_write_candidate_artifacts,
            self.can_read_locked_audit,
            self.can_read_raw_audit_rows,
            self.can_use_bundle_private_key,
            self.network_allowed,
        )
        if any(not isinstance(value, bool) for value in capabilities):
            raise EvaluationError("isolation probe capabilities must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "principal_id": self.principal_id,
            "can_read_candidate_inputs": self.can_read_candidate_inputs,
            "can_write_candidate_artifacts": self.can_write_candidate_artifacts,
            "can_read_locked_audit": self.can_read_locked_audit,
            "can_read_raw_audit_rows": self.can_read_raw_audit_rows,
            "can_use_bundle_private_key": self.can_use_bundle_private_key,
            "network_allowed": self.network_allowed,
        }


@dataclass(frozen=True, slots=True)
class FactoryIsolationAttestation:
    host_id: str
    probes: tuple[IsolationProbe, ...]
    observed_at: str
    probe_evidence_digest: str
    attestation_digest: str

    def __post_init__(self) -> None:
        _token(self.host_id, "factory host")
        require_utc_milliseconds(self.observed_at)
        _digest(self.probe_evidence_digest, "OS isolation evidence")
        if not isinstance(self.probes, tuple) or self.probes != tuple(
            sorted(self.probes, key=lambda item: item.role.value)
        ):
            raise EvaluationError("factory isolation probes must be immutable and sorted")
        if {item.role for item in self.probes} != set(FactoryServiceRole):
            raise EvaluationError("factory isolation must probe every service role")
        if len({item.principal_id for item in self.probes}) != len(self.probes):
            raise EvaluationError("factory service roles require distinct OS principals")
        by_role = {item.role: item for item in self.probes}
        builder = by_role[FactoryServiceRole.BUILDER]
        evaluator = by_role[FactoryServiceRole.EVALUATOR]
        signer = by_role[FactoryServiceRole.BUNDLE_SIGNER]
        app = by_role[FactoryServiceRole.APPLICATION]
        if (
            not builder.can_read_candidate_inputs
            or not builder.can_write_candidate_artifacts
            or builder.can_read_locked_audit
            or builder.can_read_raw_audit_rows
            or builder.can_use_bundle_private_key
            or builder.network_allowed
        ):
            raise EvaluationError("builder OS boundary can read audit/signing material")
        if (
            not evaluator.can_read_candidate_inputs
            or evaluator.can_write_candidate_artifacts
            or not evaluator.can_read_locked_audit
            or not evaluator.can_read_raw_audit_rows
            or evaluator.can_use_bundle_private_key
            or evaluator.network_allowed
        ):
            raise EvaluationError(
                "evaluator OS boundary permits candidate writes, signing, or network"
            )
        if (
            signer.can_read_candidate_inputs
            or signer.can_write_candidate_artifacts
            or signer.can_read_locked_audit
            or signer.can_read_raw_audit_rows
            or not signer.can_use_bundle_private_key
            or signer.network_allowed
        ):
            raise EvaluationError("bundle signer OS boundary can access raw audit or network")
        if (
            app.can_write_candidate_artifacts
            or app.can_read_locked_audit
            or app.can_read_raw_audit_rows
            or app.can_use_bundle_private_key
        ):
            raise EvaluationError("ordinary application identity can access factory authority")
        if self.attestation_digest != canonical_digest(self.body()):
            raise EvaluationError("factory isolation attestation digest differs")

    @classmethod
    def create(
        cls,
        *,
        host_id: str,
        probes: tuple[IsolationProbe, ...],
        observed_at: str,
        probe_evidence_digest: str,
    ) -> FactoryIsolationAttestation:
        ordered = tuple(sorted(probes, key=lambda item: item.role.value))
        body = {
            "schema_version": "strathmark-v3-factory-isolation-attestation-v1",
            "host_id": host_id,
            "probes": [item.to_dict() for item in ordered],
            "observed_at": observed_at,
            "probe_evidence_digest": probe_evidence_digest,
        }
        return cls(
            host_id,
            ordered,
            observed_at,
            probe_evidence_digest,
            canonical_digest(body),
        )

    def body(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-factory-isolation-attestation-v1",
            "host_id": self.host_id,
            "probes": [item.to_dict() for item in self.probes],
            "observed_at": self.observed_at,
            "probe_evidence_digest": self.probe_evidence_digest,
        }


@dataclass(frozen=True, slots=True, order=True)
class EvaluationGate:
    name: str
    comparator: str
    threshold: float

    def __post_init__(self) -> None:
        _token(self.name, "evaluation gate")
        if self.comparator not in _COMPARATORS:
            raise EvaluationError("evaluation gate comparator is unsupported")
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float)):
            raise EvaluationError("evaluation gate threshold must be numeric")
        numeric = float(self.threshold)
        if numeric != numeric or numeric in {float("inf"), float("-inf")}:
            raise EvaluationError("evaluation gate threshold must be finite")
        object.__setattr__(self, "threshold", numeric)

    def passes(self, value: float) -> bool:
        return value >= self.threshold if self.comparator == "gte" else value <= self.threshold

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "comparator": self.comparator,
            "threshold": canonical_decimal_string(self.threshold),
        }


@dataclass(frozen=True, slots=True)
class FrozenEvaluationHarness:
    generation_id: str
    audit_snapshot_digest: str
    harness_code_digest: str
    precommit_digest: str
    gates: tuple[EvaluationGate, ...]
    frozen_at: str
    harness_digest: str

    def __post_init__(self) -> None:
        _token(self.generation_id, "audit generation")
        for value, label in (
            (self.audit_snapshot_digest, "audit snapshot"),
            (self.harness_code_digest, "harness code"),
            (self.precommit_digest, "evaluation precommit"),
        ):
            _digest(value, label)
        require_utc_milliseconds(self.frozen_at)
        if not isinstance(self.gates, tuple) or not self.gates:
            raise EvaluationError("frozen harness requires immutable gates")
        if self.gates != tuple(sorted(self.gates, key=lambda item: item.name)):
            raise EvaluationError("frozen evaluation gates must be uniquely sorted")
        if len({item.name for item in self.gates}) != len(self.gates):
            raise EvaluationError("frozen evaluation gates cannot repeat a name")
        object.__setattr__(self, "harness_digest", canonical_digest(self.body()))

    @classmethod
    def create(
        cls,
        *,
        generation_id: str,
        audit_snapshot_digest: str,
        harness_code_digest: str,
        precommit_digest: str,
        gates: tuple[EvaluationGate, ...],
        frozen_at: str,
    ) -> FrozenEvaluationHarness:
        ordered = tuple(sorted(gates, key=lambda item: item.name))
        return cls(
            generation_id,
            audit_snapshot_digest,
            harness_code_digest,
            precommit_digest,
            ordered,
            frozen_at,
            "0" * 64,
        )

    def body(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-frozen-evaluation-harness-v1",
            "generation_id": self.generation_id,
            "audit_snapshot_digest": self.audit_snapshot_digest,
            "harness_code_digest": self.harness_code_digest,
            "precommit_digest": self.precommit_digest,
            "gates": [item.to_dict() for item in self.gates],
            "frozen_at": self.frozen_at,
        }


@dataclass(frozen=True, slots=True)
class SignedEvaluationReport:
    manifest: SignedManifest
    candidate_digest: str
    lineage_digest: str
    generation_id: str
    harness_digest: str
    passed: bool
    failed_gates: tuple[str, ...]
    public_summary: Mapping[str, float]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest, SignedManifest)
            or self.manifest.kind != "factory_evaluation"
        ):
            raise EvaluationError("evaluation report requires a signed factory manifest")
        for value, label in (
            (self.candidate_digest, "candidate"),
            (self.lineage_digest, "lineage"),
            (self.harness_digest, "harness"),
        ):
            _digest(value, label)
        _token(self.generation_id, "audit generation")
        if not isinstance(self.passed, bool):
            raise EvaluationError("evaluation pass state must be boolean")
        if not isinstance(self.failed_gates, tuple) or self.failed_gates != tuple(
            sorted(set(self.failed_gates))
        ):
            raise EvaluationError("failed gates must be uniquely sorted")
        if not isinstance(self.public_summary, Mapping):
            raise EvaluationError("evaluation summary must be a mapping")

    @property
    def report_digest(self) -> str:
        return self.manifest.body_digest


class AuditGenerationRegistry:
    """Durable no-clobber consumption markers owned by the evaluator identity."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)

    def consume(
        self,
        harness: FrozenEvaluationHarness,
        candidate: CandidateBundle,
        *,
        consumed_at: str,
    ) -> Path:
        require_utc_milliseconds(consumed_at)
        record = {
            "schema_version": "strathmark-v3-audit-generation-consumption-v1",
            "generation_id": harness.generation_id,
            "audit_snapshot_digest": harness.audit_snapshot_digest,
            "harness_digest": harness.harness_digest,
            "candidate_lineage_digest": candidate.lineage_digest,
            "candidate_digest": candidate.candidate_digest,
            "consumed_at": consumed_at,
        }
        payload = canonical_bytes(record)
        destination = (
            self.root / f"{canonical_digest({'generation_id': harness.generation_id})}.json"
        )
        temporary = self.root / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == "nt":
                _windows_publish_no_clobber(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except FileExistsError as exc:
                    raise EvaluationError("locked audit generation was already consumed") from exc
                descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        destination.chmod(0o444)
        return destination

    def record(self, generation_id: str) -> Mapping[str, object] | None:
        _token(generation_id, "audit generation")
        path = self.root / f"{canonical_digest({'generation_id': generation_id})}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise EvaluationError("audit consumption record is corrupt") from exc
        if not isinstance(value, dict) or canonical_bytes(value) != path.read_bytes():
            raise EvaluationError("audit consumption record is not canonical")
        return MappingProxyType(value)


class FrozenEvaluator:
    """Evaluate only declared gate summaries, then irreversibly consume the audit role."""

    def __init__(
        self,
        harness: FrozenEvaluationHarness,
        registry: AuditGenerationRegistry,
        *,
        signer: P256Signer,
    ) -> None:
        if not isinstance(harness, FrozenEvaluationHarness):
            raise EvaluationError("evaluator requires a frozen harness")
        if not isinstance(registry, AuditGenerationRegistry):
            raise EvaluationError("evaluator requires a durable audit registry")
        if not callable(getattr(signer, "sign", None)) or not hasattr(signer, "identity"):
            raise EvaluationError("evaluator requires a separate signing identity")
        self.harness = harness
        self.registry = registry
        self.signer = signer

    def evaluate(
        self,
        candidate: CandidateBundle,
        *,
        metrics: Mapping[str, float],
        observed_audit_snapshot_digest: str,
        created_at: str,
    ) -> SignedEvaluationReport:
        if not isinstance(candidate, CandidateBundle):
            raise EvaluationError("evaluator requires a closed candidate bundle")
        if self.harness.audit_snapshot_digest in {
            candidate.data_snapshot_digest,
            *(item.digest for item in candidate.role_snapshots),
        }:
            raise EvaluationError("locked audit role is not disjoint from candidate roles")
        if observed_audit_snapshot_digest != self.harness.audit_snapshot_digest:
            raise EvaluationError("observed audit snapshot differs from the frozen harness")
        expected_names = tuple(item.name for item in self.harness.gates)
        if not isinstance(metrics, Mapping) or tuple(sorted(metrics)) != expected_names:
            raise EvaluationError("evaluation metrics must exactly match frozen gates")
        normalized: dict[str, float] = {}
        failed: list[str] = []
        for gate in self.harness.gates:
            value = metrics[gate.name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EvaluationError("evaluation metric must be numeric")
            numeric = float(value)
            if numeric != numeric or numeric in {float("inf"), float("-inf")}:
                raise EvaluationError("evaluation metric must be finite")
            normalized[gate.name] = numeric
            if not gate.passes(numeric):
                failed.append(gate.name)
        require_utc_milliseconds(created_at)
        # Once the sealed outcome has been opened, every result—including a failed one—is
        # consumed. The marker lands before a report is returned, so crashes cannot probe it.
        self.registry.consume(self.harness, candidate, consumed_at=created_at)
        payload = {
            "schema_version": "strathmark-v3-factory-evaluation-report-v1",
            "candidate_digest": candidate.candidate_digest,
            "candidate_lineage_digest": candidate.lineage_digest,
            "generation_id": self.harness.generation_id,
            "audit_snapshot_digest": self.harness.audit_snapshot_digest,
            "harness_digest": self.harness.harness_digest,
            "harness_code_digest": self.harness.harness_code_digest,
            "precommit_digest": self.harness.precommit_digest,
            "gates": [item.to_dict() for item in self.harness.gates],
            "gate_results": [
                {
                    "name": gate.name,
                    "value": canonical_decimal_string(normalized[gate.name]),
                    "passed": gate.name not in failed,
                }
                for gate in self.harness.gates
            ],
            "passed": not failed,
            "failed_gates": sorted(failed),
        }
        manifest = sign_manifest(
            "factory_evaluation", payload, signer=self.signer, created_at=created_at
        )
        return SignedEvaluationReport(
            manifest,
            candidate.candidate_digest,
            candidate.lineage_digest,
            self.harness.generation_id,
            self.harness.harness_digest,
            not failed,
            tuple(sorted(failed)),
            MappingProxyType(normalized),
        )


def verify_evaluation_report(
    report: SignedEvaluationReport,
    *,
    trust_store: IntegrityTrustStore,
    expected_candidate: CandidateBundle,
    expected_harness: FrozenEvaluationHarness,
) -> SignedEvaluationReport:
    if not isinstance(report, SignedEvaluationReport):
        raise EvaluationError("evaluation report must use the typed contract")
    try:
        payload = verify_manifest(report.manifest, trust_store)
    except IntegrityError as exc:
        raise EvaluationError("evaluation report signer is not trusted") from exc
    expected_fields = {
        "schema_version",
        "candidate_digest",
        "candidate_lineage_digest",
        "generation_id",
        "audit_snapshot_digest",
        "harness_digest",
        "harness_code_digest",
        "precommit_digest",
        "gates",
        "gate_results",
        "passed",
        "failed_gates",
    }
    if set(payload) != expected_fields or payload["schema_version"] != (
        "strathmark-v3-factory-evaluation-report-v1"
    ):
        raise EvaluationError("evaluation report schema is not closed")
    if (
        payload["candidate_digest"] != expected_candidate.candidate_digest
        or payload["candidate_lineage_digest"] != expected_candidate.lineage_digest
    ):
        raise EvaluationError("evaluation report candidate binding differs")
    if (
        payload["generation_id"] != expected_harness.generation_id
        or payload["audit_snapshot_digest"] != expected_harness.audit_snapshot_digest
    ):
        raise EvaluationError("evaluation report audit snapshot binding differs")
    if (
        payload["harness_digest"] != expected_harness.harness_digest
        or payload["harness_code_digest"] != expected_harness.harness_code_digest
        or payload["precommit_digest"] != expected_harness.precommit_digest
        or payload["gates"] != [item.to_dict() for item in expected_harness.gates]
    ):
        raise EvaluationError("evaluation report frozen harness binding differs")
    results = payload["gate_results"]
    if not isinstance(results, list) or len(results) != len(expected_harness.gates):
        raise EvaluationError("evaluation report gate results are incomplete")
    summary: dict[str, float] = {}
    failed: list[str] = []
    for gate, result in zip(expected_harness.gates, results):
        if not isinstance(result, dict) or set(result) != {"name", "value", "passed"}:
            raise EvaluationError("evaluation report gate result is malformed")
        value = result["value"]
        if (
            result["name"] != gate.name
            or not isinstance(value, str)
            or canonical_decimal_string(value) != value
        ):
            raise EvaluationError("evaluation report gate identity or value differs")
        numeric = float(value)
        passed = gate.passes(numeric)
        if result["passed"] is not passed:
            raise EvaluationError("evaluation report gate outcome differs from frozen threshold")
        summary[gate.name] = numeric
        if not passed:
            failed.append(gate.name)
    expected_failed = sorted(failed)
    if payload["failed_gates"] != expected_failed or payload["passed"] is not (not failed):
        raise EvaluationError("evaluation report aggregate outcome differs")
    verified = SignedEvaluationReport(
        report.manifest,
        expected_candidate.candidate_digest,
        expected_candidate.lineage_digest,
        expected_harness.generation_id,
        expected_harness.harness_digest,
        not failed,
        tuple(expected_failed),
        MappingProxyType(summary),
    )
    if verified != report:
        raise EvaluationError("evaluation report typed projection differs from signed authority")
    return verified


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EvaluationError(f"{label} digest must be lower-case SHA-256")
    return value


def _windows_publish_no_clobber(source: Path, destination: Path) -> None:
    import ctypes
    from ctypes import wintypes

    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    if move(str(source), str(destination), 0x8):
        return
    error = ctypes.get_last_error()
    if error in {80, 183}:
        raise EvaluationError("locked audit generation was already consumed")
    raise EvaluationError(f"Windows write-through audit consumption failed ({error})")


def _token(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise EvaluationError(f"{label} must be a bounded opaque token")
    return value


__all__ = [
    "AuditGenerationRegistry",
    "EvaluationError",
    "EvaluationGate",
    "FactoryIsolationAttestation",
    "FactoryServiceRole",
    "FrozenEvaluationHarness",
    "FrozenEvaluator",
    "IsolationProbe",
    "SignedEvaluationReport",
    "verify_evaluation_report",
]
