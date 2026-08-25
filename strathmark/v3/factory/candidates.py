"""Closed, local-only candidate construction for the V3 model factory.

The builder is intentionally incapable of accepting audit-role data or fetching model
material.  It receives already-local immutable bytes, verifies their safe bounded shape,
and produces lineage identities that ignore cosmetic names but bind every material input.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Mapping

from strathmark.v3.contracts.canonical import canonical_digest

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_BYTES = 512 * 1024 * 1024
MAX_ARTIFACTS = 256
REQUIRED_COMPONENT_ROLES = (
    "calibration",
    "capability",
    "credibility",
    "disagreement_gate",
    "formula",
    "llm_members",
    "llm_prompts_schemas",
    "ml",
    "optimizer",
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PROHIBITED_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".jar",
    ".joblib",
    ".js",
    ".msi",
    ".pkl",
    ".pickle",
    ".ps1",
    ".py",
    ".pyc",
    ".scr",
    ".sh",
}
_SECRET_MARKERS = (
    b"-----begin private key-----",
    b"-----begin rsa private key-----",
    b"api_key=",
    b"apikey=",
    b"authorization: bearer ",
    b"password=",
    b"secret_access_key",
)


class CandidateError(ValueError):
    """Candidate construction violated a frozen safety or lineage contract."""


class FactoryRole(str, Enum):
    TRAIN = "train"
    TUNE = "tune"
    CALIBRATION = "calibration"
    AUDIT = "audit"


@dataclass(frozen=True, slots=True, order=True)
class RoleSnapshot:
    role: FactoryRole
    generation_id: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, FactoryRole):
            raise CandidateError("factory role must use the closed vocabulary")
        _token(self.generation_id, "role generation")
        _digest(self.digest, "role snapshot")

    def to_dict(self) -> dict[str, str]:
        return {
            "role": self.role.value,
            "generation_id": self.generation_id,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class CandidateBundle:
    display_name: str
    code_revision: str
    code_digest: str
    dependency_lock_digest: str
    data_snapshot_digest: str
    role_snapshots: tuple[RoleSnapshot, ...]
    component_digests: Mapping[str, str]
    artifact_payloads: Mapping[str, bytes]
    artifact_manifest: Mapping[str, Mapping[str, object]]
    local_model_ids: tuple[str, ...]
    cloud_model_ids: tuple[str, ...]
    compatibility_contract_digest: str
    rollback_parent_digest: str
    lineage_digest: str
    candidate_digest: str

    def __post_init__(self) -> None:
        _token(self.display_name, "candidate display name")
        _token(self.code_revision, "code revision")
        for value, label in (
            (self.code_digest, "code"),
            (self.dependency_lock_digest, "dependency lock"),
            (self.data_snapshot_digest, "data snapshot"),
            (self.compatibility_contract_digest, "compatibility contract"),
            (self.rollback_parent_digest, "rollback parent"),
            (self.lineage_digest, "candidate lineage"),
            (self.candidate_digest, "candidate"),
        ):
            _digest(value, label)
        if not isinstance(self.role_snapshots, tuple) or not self.role_snapshots:
            raise CandidateError("candidate role snapshots must be a nonempty immutable tuple")
        if (
            tuple(sorted(self.role_snapshots, key=lambda item: item.role.value))
            != self.role_snapshots
        ):
            raise CandidateError("candidate role snapshots must be uniquely sorted")
        if len({item.role for item in self.role_snapshots}) != len(self.role_snapshots):
            raise CandidateError("candidate role snapshots cannot repeat a role")
        if len({item.digest for item in self.role_snapshots}) != len(self.role_snapshots):
            raise CandidateError("train, tune, and calibration snapshots must be disjoint")
        if any(item.role is FactoryRole.AUDIT for item in self.role_snapshots):
            raise CandidateError("candidate material cannot contain the locked audit role")
        if {item.role for item in self.role_snapshots} != {
            FactoryRole.TRAIN,
            FactoryRole.TUNE,
            FactoryRole.CALIBRATION,
        }:
            raise CandidateError("candidate must bind train, tune, and calibration roles")
        if tuple(self.component_digests) != REQUIRED_COMPONENT_ROLES:
            raise CandidateError("candidate must cover every required component role")
        for value in self.component_digests.values():
            _digest(value, "component")
        if tuple(self.artifact_payloads) != tuple(sorted(self.artifact_payloads)):
            raise CandidateError("candidate artifact paths must be sorted")
        if tuple(self.artifact_manifest) != tuple(self.artifact_payloads):
            raise CandidateError("candidate artifact manifest must exactly cover payloads")
        expected_manifest = _artifact_manifest(self.artifact_payloads)
        if {
            name: dict(value) for name, value in self.artifact_manifest.items()
        } != expected_manifest:
            raise CandidateError("candidate artifact manifest differs from immutable payloads")
        expected_lineage = _lineage_value(self)
        if canonical_digest(expected_lineage) != self.lineage_digest:
            raise CandidateError("candidate lineage digest differs")
        if (
            canonical_digest(self.manifest_value(include_display_name=True))
            != self.candidate_digest
        ):
            raise CandidateError("candidate digest differs")

    def manifest_value(self, *, include_display_name: bool) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": "strathmark-v3-factory-candidate-v1",
            "lineage_digest": self.lineage_digest,
            "code_revision": self.code_revision,
            "code_digest": self.code_digest,
            "dependency_lock_digest": self.dependency_lock_digest,
            "data_snapshot_digest": self.data_snapshot_digest,
            "role_snapshots": [item.to_dict() for item in self.role_snapshots],
            "component_digests": dict(self.component_digests),
            "artifact_manifest": {
                name: dict(identity) for name, identity in self.artifact_manifest.items()
            },
            "local_model_ids": list(self.local_model_ids),
            "cloud_model_ids": list(self.cloud_model_ids),
            "compatibility_contract_digest": self.compatibility_contract_digest,
            "rollback_parent_digest": self.rollback_parent_digest,
        }
        if include_display_name:
            value["display_name"] = self.display_name
        return value


class CandidateBuilder:
    """Construct candidates only from explicitly configured, already-local material."""

    def __init__(
        self,
        *,
        allowed_local_models: tuple[str, ...],
        allowed_cloud_models: tuple[str, ...],
    ) -> None:
        self._allowed_local = _model_set(allowed_local_models, "local")
        self._allowed_cloud = _model_set(allowed_cloud_models, "cloud")

    def build(
        self,
        *,
        display_name: str,
        code_revision: str,
        code_digest: str,
        dependency_lock_digest: str,
        data_snapshot_digest: str,
        role_snapshots: tuple[RoleSnapshot, ...],
        component_digests: Mapping[str, str],
        artifact_payloads: Mapping[str, bytes],
        local_model_ids: tuple[str, ...],
        cloud_model_ids: tuple[str, ...],
        compatibility_contract_digest: str,
        rollback_parent_digest: str,
    ) -> CandidateBundle:
        _token(display_name, "candidate display name")
        _token(code_revision, "code revision")
        for value, label in (
            (code_digest, "code"),
            (dependency_lock_digest, "dependency lock"),
            (data_snapshot_digest, "data snapshot"),
            (compatibility_contract_digest, "compatibility contract"),
            (rollback_parent_digest, "rollback parent"),
        ):
            _digest(value, label)
        if not isinstance(role_snapshots, tuple):
            raise CandidateError("candidate roles must be immutable")
        if any(item.role is FactoryRole.AUDIT for item in role_snapshots):
            raise CandidateError("candidate builder cannot read or bind the audit role")
        roles = tuple(sorted(role_snapshots, key=lambda item: item.role.value))
        components = _components(component_digests)
        payloads = _payloads(artifact_payloads)
        manifest = _artifact_manifest(payloads)
        local = _selected_models(local_model_ids, self._allowed_local, "local")
        cloud = _selected_models(cloud_model_ids, self._allowed_cloud, "cloud")
        shell = _CandidateShell(
            code_revision,
            code_digest,
            dependency_lock_digest,
            data_snapshot_digest,
            roles,
            components,
            manifest,
            local,
            cloud,
            compatibility_contract_digest,
            rollback_parent_digest,
        )
        lineage_digest = canonical_digest(_lineage_value(shell))
        candidate_digest = canonical_digest(
            _candidate_manifest_value(
                shell, display_name=display_name, lineage_digest=lineage_digest
            )
        )
        candidate = CandidateBundle(
            display_name,
            code_revision,
            code_digest,
            dependency_lock_digest,
            data_snapshot_digest,
            roles,
            MappingProxyType(components),
            MappingProxyType(payloads),
            MappingProxyType(
                {name: MappingProxyType(identity) for name, identity in manifest.items()}
            ),
            local,
            cloud,
            compatibility_contract_digest,
            rollback_parent_digest,
            lineage_digest,
            candidate_digest,
        )
        return candidate


@dataclass(frozen=True, slots=True)
class _CandidateShell:
    code_revision: str
    code_digest: str
    dependency_lock_digest: str
    data_snapshot_digest: str
    role_snapshots: tuple[RoleSnapshot, ...]
    component_digests: Mapping[str, str]
    artifact_manifest: Mapping[str, Mapping[str, object]]
    local_model_ids: tuple[str, ...]
    cloud_model_ids: tuple[str, ...]
    compatibility_contract_digest: str
    rollback_parent_digest: str


def _lineage_value(candidate: CandidateBundle | _CandidateShell) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-candidate-lineage-v1",
        "code_revision": candidate.code_revision,
        "code_digest": candidate.code_digest,
        "dependency_lock_digest": candidate.dependency_lock_digest,
        "data_snapshot_digest": candidate.data_snapshot_digest,
        "role_snapshots": [item.to_dict() for item in candidate.role_snapshots],
        "component_digests": dict(candidate.component_digests),
        "artifact_manifest_digest": canonical_digest(
            {
                "schema_version": "strathmark-v3-candidate-artifacts-v1",
                "files": {
                    name: dict(identity) for name, identity in candidate.artifact_manifest.items()
                },
            }
        ),
        "local_model_ids": list(candidate.local_model_ids),
        "cloud_model_ids": list(candidate.cloud_model_ids),
        "compatibility_contract_digest": candidate.compatibility_contract_digest,
        "rollback_parent_digest": candidate.rollback_parent_digest,
    }


def _candidate_manifest_value(
    candidate: CandidateBundle | _CandidateShell,
    *,
    display_name: str,
    lineage_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": "strathmark-v3-factory-candidate-v1",
        "lineage_digest": lineage_digest,
        "code_revision": candidate.code_revision,
        "code_digest": candidate.code_digest,
        "dependency_lock_digest": candidate.dependency_lock_digest,
        "data_snapshot_digest": candidate.data_snapshot_digest,
        "role_snapshots": [item.to_dict() for item in candidate.role_snapshots],
        "component_digests": dict(candidate.component_digests),
        "artifact_manifest": {
            name: dict(identity) for name, identity in candidate.artifact_manifest.items()
        },
        "local_model_ids": list(candidate.local_model_ids),
        "cloud_model_ids": list(candidate.cloud_model_ids),
        "compatibility_contract_digest": candidate.compatibility_contract_digest,
        "rollback_parent_digest": candidate.rollback_parent_digest,
        "display_name": display_name,
    }


def _components(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(REQUIRED_COMPONENT_ROLES):
        raise CandidateError("candidate must include every required component digest")
    result = {role: value[role] for role in REQUIRED_COMPONENT_ROLES}
    for digest in result.values():
        _digest(digest, "component")
    return result


def _payloads(value: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(value, Mapping) or not value or len(value) > MAX_ARTIFACTS:
        raise CandidateError("candidate requires a bounded nonempty artifact set")
    result: dict[str, bytes] = {}
    total = 0
    for name in sorted(value):
        path = PurePosixPath(name)
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 240
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or path.suffix.lower() in _PROHIBITED_SUFFIXES
        ):
            raise CandidateError("candidate contains an executable or unsafe artifact path")
        raw = value[name]
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARTIFACT_BYTES:
            raise CandidateError("candidate artifact bytes are empty, mutable, or oversized")
        folded = raw.lower()
        if any(marker in folded for marker in _SECRET_MARKERS):
            raise CandidateError("candidate artifacts cannot contain credential or secret material")
        total += len(raw)
        if total > MAX_CANDIDATE_BYTES:
            raise CandidateError("candidate artifact set exceeds the bounded size")
        result[name] = bytes(raw)
    return result


def _artifact_manifest(payloads: Mapping[str, bytes]) -> dict[str, dict[str, object]]:
    return {
        name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in payloads.items()
    }


def _model_set(values: tuple[str, ...], label: str) -> frozenset[str]:
    if not isinstance(values, tuple):
        raise CandidateError(f"configured {label} models must be immutable")
    if values != tuple(sorted(set(values))):
        raise CandidateError(f"configured {label} models must be unique and sorted")
    for value in values:
        _pinned_model(value)
    return frozenset(values)


def _selected_models(
    values: tuple[str, ...], configured: frozenset[str], label: str
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or values != tuple(sorted(set(values))):
        raise CandidateError(f"selected {label} models must be unique and sorted")
    for value in values:
        _pinned_model(value)
        if value not in configured:
            raise CandidateError(f"selected {label} model is not explicitly configured")
    return values


def _pinned_model(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 200
        or ":" not in value
        or "@" not in value
        or any(alias in value.casefold() for alias in ("latest", "current", "stable"))
    ):
        raise CandidateError("model identity must be an exact pinned provider revision")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise CandidateError(f"{label} digest must be lower-case SHA-256")
    return value


def _token(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise CandidateError(f"{label} must be a bounded opaque token")
    return value


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_CANDIDATE_BYTES",
    "REQUIRED_COMPONENT_ROLES",
    "CandidateBuilder",
    "CandidateBundle",
    "CandidateError",
    "FactoryRole",
    "RoleSnapshot",
]
