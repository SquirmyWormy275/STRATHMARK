"""Proof-only V2 to V3 authority handoff preparation.

Preparing a handoff never changes an endpoint and never makes V2 audit-only.  It proves the
preconditions for a later, separately authorized release operation while preserving exactly
one declared authority on every failure path.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.infrastructure.integrity import (
    IntegrityKeyClass,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    require_production_cng_signer,
    sign_manifest,
    verify_manifest,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_PLATFORM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

REQUIRED_RELEASE_EVIDENCE = (
    "installed_artifact",
    "dependency_lock",
    "consumer_contract",
    "full_causal_replay",
    "manipulation_equity_slices",
    "provider_failure_matrix",
    "race_day_recovery",
    "windows_capacity",
    "thermal_memory_storage_stress",
    "database_backup_restore",
    "bundle_model_integrity",
)

_WINDOWS_STRESS = (
    "warm_start",
    "cold_start",
    "thermal",
    "oom",
    "paging",
    "vram",
    "wal",
    "blob",
    "queue",
    "disk_reserve",
)


class ReleaseTier(str, Enum):
    REHEARSAL = "rehearsal"
    PRODUCTION = "production"


class Authority(str, Enum):
    V2 = "v2"
    V3 = "v3"
    TRADITIONAL_MANUAL = "traditional_manual"


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    name: str
    result: str
    artifact_digest: str
    observed_at: str

    def __post_init__(self) -> None:
        if self.name not in REQUIRED_RELEASE_EVIDENCE:
            raise ValueError("release evidence name is not recognized")
        if self.result not in {"passed", "failed"}:
            raise ValueError("release evidence result must be passed or failed")
        _require_digest(self.artifact_digest, "release evidence artifact digest")
        require_utc_milliseconds(self.observed_at)

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "result": self.result,
            "artifact_digest": self.artifact_digest,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class V2FreezeSnapshot:
    trusted_writes_frozen: bool
    open_tournaments: int
    in_flight_requests: int
    ambiguous_requests: int
    final_sequence: int
    schema_digest: str
    receipt_root_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.trusted_writes_frozen, bool):
            raise ValueError("V2 freeze state must be explicit")
        for name in (
            "open_tournaments",
            "in_flight_requests",
            "ambiguous_requests",
            "final_sequence",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("V2 freeze counters must be non-negative integers")
        _require_digest(self.schema_digest, "V2 schema digest")
        _require_digest(self.receipt_root_digest, "V2 receipt root digest")

    @property
    def resolved(self) -> bool:
        return (
            self.trusted_writes_frozen
            and self.open_tournaments == 0
            and self.in_flight_requests == 0
            and self.ambiguous_requests == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trusted_writes_frozen": self.trusted_writes_frozen,
            "open_tournaments": self.open_tournaments,
            "in_flight_requests": self.in_flight_requests,
            "ambiguous_requests": self.ambiguous_requests,
            "final_sequence": self.final_sequence,
            "schema_digest": self.schema_digest,
            "receipt_root_digest": self.receipt_root_digest,
        }


@dataclass(frozen=True, slots=True)
class V3InitializationSnapshot:
    initialized: bool
    open_tournaments: int
    release_attestation_digest: str
    database_digest: str
    bundle_digest: str
    consumer_contract_digest: str
    isolated_rehearsal_digest: str
    isolated_rehearsal_passed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.initialized, bool) or not isinstance(
            self.isolated_rehearsal_passed, bool
        ):
            raise ValueError("V3 initialization flags must be explicit")
        if (
            isinstance(self.open_tournaments, bool)
            or not isinstance(self.open_tournaments, int)
            or self.open_tournaments < 0
        ):
            raise ValueError("V3 open tournament count must be non-negative")
        for name in (
            "release_attestation_digest",
            "database_digest",
            "bundle_digest",
            "consumer_contract_digest",
            "isolated_rehearsal_digest",
        ):
            _require_digest(getattr(self, name), name.replace("_", " "))

    @property
    def verified(self) -> bool:
        return self.initialized and self.open_tournaments == 0 and self.isolated_rehearsal_passed


@dataclass(frozen=True, slots=True)
class CutoverPorts:
    freeze_v2: Callable[[], V2FreezeSnapshot]
    resolve_inflight: Callable[[], V2FreezeSnapshot]
    finalize_v2_manifest: Callable[[], SignedManifest]
    verify_v3: Callable[[], V3InitializationSnapshot]
    rehearse_consumer: Callable[[], str]
    resume_v2: Callable[[], None]

    def __post_init__(self) -> None:
        if any(not callable(getattr(self, name)) for name in self.__dataclass_fields__):
            raise ValueError("cutover requires every explicit operational port")


@dataclass(frozen=True, slots=True)
class CutoverAttempt:
    ready: bool
    declared_authority: Authority
    failure_stage: str | None
    reason_code: str
    handoff: SignedManifest | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "declared_authority": self.declared_authority.value,
            "failure_stage": self.failure_stage,
            "reason_code": self.reason_code,
            "handoff_digest": None if self.handoff is None else self.handoff.body_digest,
        }


def create_release_attestation(
    *,
    evidence: tuple[EvidenceReceipt, ...],
    source_commit: str,
    platform: str,
    tier: ReleaseTier,
    signer: P256Signer,
    created_at: str,
) -> SignedManifest:
    if not isinstance(evidence, tuple) or any(
        not isinstance(item, EvidenceReceipt) for item in evidence
    ):
        raise ValueError("release evidence must contain typed receipts")
    if tuple(item.name for item in evidence) != REQUIRED_RELEASE_EVIDENCE:
        raise ValueError("release evidence must be complete, unique, and canonically ordered")
    if any(item.result != "passed" for item in evidence):
        raise ValueError("release evidence must all be passed")
    if not isinstance(source_commit, str) or _COMMIT.fullmatch(source_commit) is None:
        raise ValueError("release source commit is invalid")
    if not isinstance(platform, str) or _PLATFORM.fullmatch(platform) is None:
        raise ValueError("release platform is invalid")
    if not isinstance(tier, ReleaseTier):
        raise ValueError("release tier must be explicit")
    if tier is ReleaseTier.PRODUCTION:
        require_production_cng_signer(signer)
    payload = {
        "schema_version": "strathmark-v3-release-attestation-v1",
        "tier": tier.value,
        "source_commit": source_commit,
        "platform": platform,
        "evidence": [item.to_dict() for item in evidence],
    }
    return sign_manifest("v3_release_attestation", payload, signer=signer, created_at=created_at)


def verify_release_attestation(
    manifest: SignedManifest, *, trust_store: IntegrityTrustStore
) -> dict[str, Any]:
    if not isinstance(manifest, SignedManifest) or manifest.kind != "v3_release_attestation":
        raise ValueError("V3 release attestation kind differs")
    payload = verify_manifest(manifest, trust_store)
    if set(payload) != {"schema_version", "tier", "source_commit", "platform", "evidence"}:
        raise ValueError("V3 release attestation fields differ")
    try:
        if not isinstance(payload["evidence"], list) or any(
            not isinstance(item, Mapping)
            or set(item) != {"name", "result", "artifact_digest", "observed_at"}
            for item in payload["evidence"]
        ):
            raise ValueError("V3 release attestation evidence fields differ")
        tier = ReleaseTier(payload["tier"])
        evidence = tuple(
            EvidenceReceipt(
                item["name"], item["result"], item["artifact_digest"], item["observed_at"]
            )
            for item in payload["evidence"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("V3 release attestation evidence differs") from exc
    if (
        payload["schema_version"] != "strathmark-v3-release-attestation-v1"
        or tuple(item.name for item in evidence) != REQUIRED_RELEASE_EVIDENCE
        or any(item.result != "passed" for item in evidence)
        or not isinstance(payload["source_commit"], str)
        or _COMMIT.fullmatch(payload["source_commit"]) is None
        or not isinstance(payload["platform"], str)
        or _PLATFORM.fullmatch(payload["platform"]) is None
    ):
        raise ValueError("V3 release attestation is incomplete")
    if tier is ReleaseTier.PRODUCTION:
        identity = trust_store.identity(manifest.key_id)
        if identity.key_class is not IntegrityKeyClass.PRODUCTION_CNG:
            raise ValueError("production release attestation lacks a production CNG identity")
    return payload


def create_v2_final_manifest(
    snapshot: V2FreezeSnapshot, *, signer: P256Signer, created_at: str
) -> SignedManifest:
    if not isinstance(snapshot, V2FreezeSnapshot) or not snapshot.resolved:
        raise ValueError("V2 final manifest requires a resolved zero-open freeze boundary")
    payload = {
        "schema_version": "strathmark-v2-final-authority-v1",
        **snapshot.to_dict(),
    }
    return sign_manifest("v2_final_authority", payload, signer=signer, created_at=created_at)


def _verify_v2_final_manifest(
    manifest: SignedManifest, trust_store: IntegrityTrustStore
) -> V2FreezeSnapshot:
    if not isinstance(manifest, SignedManifest) or manifest.kind != "v2_final_authority":
        raise ValueError("V2 final authority manifest kind differs")
    payload = verify_manifest(manifest, trust_store)
    expected = {
        "schema_version",
        "trusted_writes_frozen",
        "open_tournaments",
        "in_flight_requests",
        "ambiguous_requests",
        "final_sequence",
        "schema_digest",
        "receipt_root_digest",
    }
    if set(payload) != expected or payload["schema_version"] != "strathmark-v2-final-authority-v1":
        raise ValueError("V2 final authority manifest fields differ")
    snapshot = V2FreezeSnapshot(
        payload["trusted_writes_frozen"],
        payload["open_tournaments"],
        payload["in_flight_requests"],
        payload["ambiguous_requests"],
        payload["final_sequence"],
        payload["schema_digest"],
        payload["receipt_root_digest"],
    )
    if not snapshot.resolved:
        raise ValueError("V2 final authority is not resolved")
    return snapshot


class CutoverCoordinator:
    def __init__(
        self,
        ports: CutoverPorts,
        *,
        release_attestation: SignedManifest,
        trust_store: IntegrityTrustStore,
        signer: P256Signer,
    ) -> None:
        if not isinstance(ports, CutoverPorts):
            raise ValueError("cutover coordinator requires typed ports")
        self._ports = ports
        self._release = release_attestation
        self._trust_store = trust_store
        self._signer = signer

    def prepare(self, *, created_at: str) -> CutoverAttempt:
        timestamp = require_utc_milliseconds(created_at)
        frozen = False
        stage = "freeze_v2"
        try:
            initial = self._ports.freeze_v2()
            if (
                not isinstance(initial, V2FreezeSnapshot)
                or not initial.trusted_writes_frozen
                or initial.open_tournaments != 0
            ):
                raise ValueError("V2 did not reach a zero-open frozen boundary")
            frozen = True
            stage = "resolve_inflight"
            resolved = self._ports.resolve_inflight()
            if not isinstance(resolved, V2FreezeSnapshot) or not resolved.resolved:
                raise ValueError("V2 ambiguous or in-flight work remains")
            stage = "v2_manifest"
            v2_manifest = self._ports.finalize_v2_manifest()
            manifested = _verify_v2_final_manifest(v2_manifest, self._trust_store)
            if manifested != resolved:
                raise ValueError("V2 final manifest differs from resolved authority")
            stage = "release_attestation"
            release_payload = verify_release_attestation(
                self._release, trust_store=self._trust_store
            )
            if release_payload["tier"] != ReleaseTier.PRODUCTION.value:
                raise ValueError("cutover handoff requires production release evidence")
            stage = "verify_v3"
            initialization = self._ports.verify_v3()
            if (
                not isinstance(initialization, V3InitializationSnapshot)
                or not initialization.verified
                or initialization.release_attestation_digest != self._release.body_digest
            ):
                raise ValueError("V3 initialization is not verified against release evidence")
            stage = "consumer_rehearsal"
            rehearsal_digest = self._ports.rehearse_consumer()
            _require_digest(rehearsal_digest, "consumer rehearsal digest")
            if rehearsal_digest != initialization.isolated_rehearsal_digest:
                raise ValueError("consumer rehearsal differs from V3 initialization")
            require_production_cng_signer(self._signer)
            payload = {
                "schema_version": "strathmark-v3-authority-handoff-v1",
                "status": "cutover_ready",
                "current_authority": Authority.V2.value,
                "next_authority": Authority.V3.value,
                "endpoint_switched": False,
                "v2_audit_only": False,
                "requires_explicit_release_authorization": True,
                "release_attestation_digest": self._release.body_digest,
                "v2_final_manifest_digest": v2_manifest.body_digest,
                "v3_database_digest": initialization.database_digest,
                "v3_bundle_digest": initialization.bundle_digest,
                "consumer_contract_digest": initialization.consumer_contract_digest,
                "isolated_rehearsal_digest": rehearsal_digest,
            }
            handoff = sign_manifest(
                "authority_handoff", payload, signer=self._signer, created_at=timestamp
            )
            return CutoverAttempt(True, Authority.V2, None, "cutover_ready", handoff)
        except Exception:
            if frozen:
                try:
                    self._ports.resume_v2()
                except Exception:
                    return CutoverAttempt(
                        False,
                        Authority.TRADITIONAL_MANUAL,
                        stage,
                        "v2_resume_failed_manual_authority_required",
                        None,
                    )
            return CutoverAttempt(False, Authority.V2, stage, "cutover_preparation_failed", None)


def verify_authority_handoff(
    manifest: SignedManifest, *, trust_store: IntegrityTrustStore
) -> dict[str, Any]:
    if not isinstance(manifest, SignedManifest) or manifest.kind != "authority_handoff":
        raise ValueError("authority handoff manifest kind differs")
    payload = verify_manifest(manifest, trust_store)
    expected = {
        "schema_version",
        "status",
        "current_authority",
        "next_authority",
        "endpoint_switched",
        "v2_audit_only",
        "requires_explicit_release_authorization",
        "release_attestation_digest",
        "v2_final_manifest_digest",
        "v3_database_digest",
        "v3_bundle_digest",
        "consumer_contract_digest",
        "isolated_rehearsal_digest",
    }
    if set(payload) != expected:
        raise ValueError("authority handoff fields differ")
    if (
        payload["schema_version"] != "strathmark-v3-authority-handoff-v1"
        or payload["status"] != "cutover_ready"
        or payload["current_authority"] != Authority.V2.value
        or payload["next_authority"] != Authority.V3.value
        or payload["endpoint_switched"] is not False
        or payload["v2_audit_only"] is not False
        or payload["requires_explicit_release_authorization"] is not True
    ):
        raise ValueError("authority handoff is not a pre-switch cutover-ready assertion")
    for name in (
        "release_attestation_digest",
        "v2_final_manifest_digest",
        "v3_database_digest",
        "v3_bundle_digest",
        "consumer_contract_digest",
        "isolated_rehearsal_digest",
    ):
        _require_digest(payload[name], name.replace("_", " "))
    identity = trust_store.identity(manifest.key_id)
    if identity.key_class is not IntegrityKeyClass.PRODUCTION_CNG:
        raise ValueError("authority handoff lacks production CNG identity")
    return payload


def verify_windows_capacity_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    top = {
        "schema_version",
        "recorded_at",
        "candidate_tier",
        "machine",
        "declared_envelope",
        "measured",
        "stress_matrix",
        "artifact_pins",
        "limitations",
    }
    if not isinstance(value, Mapping) or set(value) != top:
        raise ValueError("Windows capacity manifest fields differ")
    if value["schema_version"] != "strathmark-v3-windows-capacity-manifest-v1":
        raise ValueError("Windows capacity manifest schema differs")
    require_utc_milliseconds(value["recorded_at"])
    if value["candidate_tier"] not in {"rehearsal", "production"}:
        raise ValueError("Windows capacity candidate tier differs")
    machine = value["machine"]
    machine_fields = {
        "operating_system",
        "architecture",
        "python",
        "processor",
        "logical_cpu_count",
        "memory_total_bytes",
        "gpu",
        "gpu_vram_mib",
    }
    if not isinstance(machine, Mapping) or set(machine) != machine_fields:
        raise ValueError("Windows capacity machine identity differs")
    if (
        not str(machine["operating_system"]).startswith("Windows-")
        or machine["architecture"] != "x86_64"
        or any(
            isinstance(machine[name], bool) or not isinstance(machine[name], int)
            for name in ("gpu_vram_mib", "memory_total_bytes", "logical_cpu_count")
        )
        or machine["gpu_vram_mib"] < 8_000
        or machine["memory_total_bytes"] < 16 * 1024**3
        or machine["logical_cpu_count"] < 4
    ):
        raise ValueError("Windows capacity machine is outside the designated envelope")
    envelope = value["declared_envelope"]
    expected_envelope = {
        "max_open_tournaments": 1,
        "max_round_entrants": 48,
        "max_field_entrants": 12,
        "max_plausible_qualifiers": 48,
        "max_context_cards": 48,
        "max_queued_jobs": 384,
        "heat_cadence_ms": 600_000,
        "final_turnaround_ms": 300_000,
        "result_to_ready_ms": 120_000,
        "field_assembly_ms_exclusive": 2_000,
    }
    if envelope != expected_envelope:
        raise ValueError("Windows capacity declared envelope differs")
    measured = value["measured"]
    measured_fields = {
        "field_assembly_runs",
        "field_assembly_failures",
        "field_assembly_cold_ms",
        "field_assembly_p99_ms",
        "field_assembly_worst_ms",
        "saturated_recovery_p99_ms",
        "saturated_recovery_worst_ms",
        "critical_restart_worst_ms",
        "writer_wall_p99_ms",
        "caller_wall_p99_ms",
        "projection_rebuild_ms",
        "rss_growth_bytes",
    }
    if not isinstance(measured, Mapping) or set(measured) != measured_fields:
        raise ValueError("Windows capacity measurements differ")
    if any(
        isinstance(measured[name], bool) or not isinstance(measured[name], (int, float))
        for name in measured_fields
    ):
        raise ValueError("Windows capacity measurements must be numeric")
    if (
        measured["field_assembly_runs"] < 100
        or measured["field_assembly_failures"] != 0
        or measured["field_assembly_p99_ms"] >= 2_000
        or measured["field_assembly_worst_ms"] >= 2_000
        or measured["saturated_recovery_worst_ms"] > 250
        or measured["critical_restart_worst_ms"] > 5_000
        or measured["writer_wall_p99_ms"] > 100
        or measured["caller_wall_p99_ms"] > 100
        or measured["projection_rebuild_ms"] > 30_000
        or measured["rss_growth_bytes"] > 256 * 1024**2
    ):
        raise ValueError("Windows capacity measurement exceeds a hard budget")
    stress = value["stress_matrix"]
    if (
        not isinstance(stress, list)
        or tuple(item.get("name") for item in stress if isinstance(item, Mapping))
        != _WINDOWS_STRESS
        or any(
            set(item) != {"name", "method", "result"}
            or item["result"] != "passed"
            or not isinstance(item["method"], str)
            or not item["method"]
            for item in stress
        )
    ):
        raise ValueError("Windows capacity stress matrix is incomplete or failed")
    pins = value["artifact_pins"]
    if not isinstance(pins, Mapping) or set(pins) != {
        "field_assembly_manifest_sha256",
        "rolling_restart_manifest_sha256",
        "job_capacity_manifest_sha256",
    }:
        raise ValueError("Windows capacity artifact pins differ")
    for digest in pins.values():
        _require_digest(digest, "Windows capacity artifact pin")
    limitations = value["limitations"]
    if (
        not isinstance(limitations, list)
        or not limitations
        or any(not isinstance(item, str) or not item for item in limitations)
    ):
        raise ValueError("Windows capacity limitations must be explicit")
    return dict(value)


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a SHA-256 digest")
    return value


__all__ = [
    "Authority",
    "CutoverAttempt",
    "CutoverCoordinator",
    "CutoverPorts",
    "EvidenceReceipt",
    "REQUIRED_RELEASE_EVIDENCE",
    "ReleaseTier",
    "V2FreezeSnapshot",
    "V3InitializationSnapshot",
    "create_release_attestation",
    "create_v2_final_manifest",
    "verify_authority_handoff",
    "verify_release_attestation",
    "verify_windows_capacity_manifest",
]
