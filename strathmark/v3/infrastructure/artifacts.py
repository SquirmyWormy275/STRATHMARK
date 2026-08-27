"""Immutable signed whole-system bundle publication and verification."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.factory.candidates import REQUIRED_COMPONENT_ROLES, CandidateBundle
from strathmark.v3.factory.evaluator import SignedEvaluationReport
from strathmark.v3.infrastructure.integrity import (
    IntegrityError,
    IntegrityTrustStore,
    P256Signer,
    SignedManifest,
    require_production_cng_signer,
    sign_manifest,
    verify_manifest,
)

MAX_BUNDLE_MANIFEST_BYTES = 2 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ArtifactError(RuntimeError):
    """Bundle material failed identity, authorization, or safe-publication checks."""


class ActivationPurpose(str, Enum):
    NEW_ACTIVATION = "new_activation"
    NEW_TOURNAMENT = "new_tournament"
    PINNED_TOURNAMENT = "pinned_tournament"
    HISTORICAL_VERIFY = "historical_verify"


@dataclass(frozen=True, slots=True)
class FactoryTrustPolicy:
    bundle_trust_store: IntegrityTrustStore
    evaluator_trust_store: IntegrityTrustStore
    retired_key_ids: tuple[str, ...] = ()
    revoked_key_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_trust_store, IntegrityTrustStore) or not isinstance(
            self.evaluator_trust_store, IntegrityTrustStore
        ):
            raise ArtifactError("factory trust policy requires separate typed trust stores")
        for values, label in (
            (self.retired_key_ids, "retired"),
            (self.revoked_key_ids, "revoked"),
        ):
            if not isinstance(values, tuple) or values != tuple(sorted(set(values))):
                raise ArtifactError(f"{label} signer identities must be uniquely sorted")
            for key_id in values:
                try:
                    self.bundle_trust_store.identity(key_id)
                except IntegrityError as exc:
                    raise ArtifactError(f"{label} signer must remain in historical trust") from exc
        if set(self.retired_key_ids) & set(self.revoked_key_ids):
            raise ArtifactError("a factory signer cannot be both retired and revoked")

    def authorize(self, key_id: str, purpose: ActivationPurpose) -> None:
        try:
            self.bundle_trust_store.identity(key_id)
        except IntegrityError as exc:
            raise ArtifactError("bundle signer is not trusted") from exc
        if not isinstance(purpose, ActivationPurpose):
            raise ArtifactError("bundle verification purpose must be explicit")
        if purpose is ActivationPurpose.HISTORICAL_VERIFY:
            return
        if key_id in self.revoked_key_ids:
            raise ArtifactError("revoked bundle signer blocks new or pinned operational use")
        if purpose in {ActivationPurpose.NEW_ACTIVATION, ActivationPurpose.NEW_TOURNAMENT} and (
            key_id in self.retired_key_ids
        ):
            raise ArtifactError("retired bundle signer cannot authorize new use")


@dataclass(frozen=True, slots=True)
class InstalledBundle:
    bundle_digest: str
    path: Path
    signer_key_id: str
    candidate_digest: str
    evaluator_report_digest: str
    rollback_parent_digest: str
    manifest: SignedManifest

    def __post_init__(self) -> None:
        for value, label in (
            (self.bundle_digest, "bundle"),
            (self.candidate_digest, "candidate"),
            (self.evaluator_report_digest, "evaluator report"),
            (self.rollback_parent_digest, "rollback parent"),
        ):
            _digest(value, label)
        if not isinstance(self.path, Path) or not isinstance(self.manifest, SignedManifest):
            raise ArtifactError("installed bundle path or manifest is invalid")


@dataclass(frozen=True, slots=True)
class BundleRuntimeInventory:
    dependency_lock_digest: str
    compatibility_contract_digest: str
    installed_local_model_ids: tuple[str, ...]
    warmed_local_model_ids: tuple[str, ...]
    configured_cloud_model_ids: tuple[str, ...]
    cloud_credentials_configured: bool
    offline_fallbacks_ready: bool
    download_attempted: bool

    def __post_init__(self) -> None:
        _digest(self.dependency_lock_digest, "runtime dependency lock")
        _digest(self.compatibility_contract_digest, "runtime compatibility contract")
        for values, label in (
            (self.installed_local_model_ids, "installed local models"),
            (self.warmed_local_model_ids, "warmed local models"),
            (self.configured_cloud_model_ids, "configured cloud models"),
        ):
            if not isinstance(values, tuple) or values != tuple(sorted(set(values))):
                raise ArtifactError(f"{label} must be immutable, unique, and sorted")
        for value in (
            self.cloud_credentials_configured,
            self.offline_fallbacks_ready,
            self.download_attempted,
        ):
            if not isinstance(value, bool):
                raise ArtifactError("runtime preflight flags must be boolean")

    def body(self) -> dict[str, object]:
        return {
            "schema_version": "strathmark-v3-bundle-runtime-inventory-v1",
            "dependency_lock_digest": self.dependency_lock_digest,
            "compatibility_contract_digest": self.compatibility_contract_digest,
            "installed_local_model_ids": list(self.installed_local_model_ids),
            "warmed_local_model_ids": list(self.warmed_local_model_ids),
            "configured_cloud_model_ids": list(self.configured_cloud_model_ids),
            "cloud_credentials_configured": self.cloud_credentials_configured,
            "offline_fallbacks_ready": self.offline_fallbacks_ready,
            "download_attempted": self.download_attempted,
        }


@dataclass(frozen=True, slots=True)
class BundlePreflightAttestation:
    bundle_digest: str
    inventory_digest: str
    checks: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.bundle_digest, "preflight bundle")
        _digest(self.inventory_digest, "preflight inventory")
        if self.checks != tuple(sorted(set(self.checks))):
            raise ArtifactError("preflight checks must be immutable, unique, and sorted")


class BundleRepository:
    """Publish only local inert bytes, then verify them for each operational purpose."""

    def __init__(
        self,
        root: str | Path,
        *,
        trust_policy: FactoryTrustPolicy,
        production: bool = False,
    ) -> None:
        if not isinstance(trust_policy, FactoryTrustPolicy):
            raise ArtifactError("bundle repository requires a factory trust policy")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.trust_policy = trust_policy
        if not isinstance(production, bool):
            raise ArtifactError("bundle repository production mode must be explicit")
        self.production = production

    def publish(
        self,
        candidate: CandidateBundle,
        evaluation_report: SignedEvaluationReport,
        *,
        signer: P256Signer,
        created_at: str,
        fault_hook: Callable[[str], None] | None = None,
    ) -> InstalledBundle:
        if not isinstance(candidate, CandidateBundle):
            raise ArtifactError("bundle publication requires a closed candidate")
        if not callable(getattr(signer, "sign", None)) or not hasattr(signer, "identity"):
            raise ArtifactError("bundle publication requires an external signer")
        try:
            trusted = self.trust_policy.bundle_trust_store.identity(signer.identity.key_id)
        except IntegrityError as exc:
            raise ArtifactError("bundle signer is not trusted") from exc
        if trusted != signer.identity:
            raise ArtifactError("bundle signer identity differs from trusted material")
        if self.production:
            try:
                require_production_cng_signer(signer)
            except IntegrityError as exc:
                raise ArtifactError(
                    "production bundle publication requires the live non-exportable CNG signer"
                ) from exc
        self.trust_policy.authorize(signer.identity.key_id, ActivationPurpose.NEW_ACTIVATION)
        _verify_report_for_publication(
            evaluation_report,
            candidate,
            evaluator_trust_store=self.trust_policy.evaluator_trust_store,
        )
        if not evaluation_report.passed:
            raise ArtifactError("failed evaluator report cannot publish a bundle")
        if evaluation_report.manifest.key_id == signer.identity.key_id:
            raise ArtifactError("evaluator and bundle authorization require separate signers")
        body = {
            "schema_version": "strathmark-v3-whole-system-bundle-v1",
            "candidate_digest": candidate.candidate_digest,
            "candidate_lineage_digest": candidate.lineage_digest,
            "code_revision": candidate.code_revision,
            "code_digest": candidate.code_digest,
            "dependency_lock_digest": candidate.dependency_lock_digest,
            "data_snapshot_digest": candidate.data_snapshot_digest,
            "component_digests": dict(candidate.component_digests),
            "artifacts": {
                name: dict(identity) for name, identity in candidate.artifact_manifest.items()
            },
            "local_model_ids": list(candidate.local_model_ids),
            "cloud_model_ids": list(candidate.cloud_model_ids),
            "compatibility_contract_digest": candidate.compatibility_contract_digest,
            "rollback_parent_digest": candidate.rollback_parent_digest,
            "evaluator_report_digest": evaluation_report.report_digest,
            "evaluator_key_id": evaluation_report.manifest.key_id,
            "audit_generation_id": evaluation_report.generation_id,
            "evaluation_harness_digest": evaluation_report.harness_digest,
            "whole_domain": True,
        }
        bundle_digest = canonical_digest(body)
        payload = {**body, "bundle_digest": bundle_digest}
        destination = self.root / bundle_digest
        if destination.exists():
            installed = self.verify(bundle_digest, purpose=ActivationPurpose.NEW_ACTIVATION)
            _require_same_publication(installed, candidate, evaluation_report)
            return installed
        signed = sign_manifest("factory_bundle", payload, signer=signer, created_at=created_at)
        manifest_bytes = canonical_bytes(signed.to_dict(), max_bytes=MAX_BUNDLE_MANIFEST_BYTES)
        stage = self.root / f".staging-{bundle_digest}-{uuid.uuid4().hex}"
        try:
            stage.mkdir(parents=False, exist_ok=False)
            for relative, raw in candidate.artifact_payloads.items():
                path = stage.joinpath(*relative.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            _fault(fault_hook, "after_artifacts")
            manifest_path = stage / "manifest.json"
            with manifest_path.open("xb") as handle:
                handle.write(manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            _fault(fault_hook, "after_manifest")
            _verify_staged(stage, signed, candidate)
            _make_read_only(stage)
            _fault(fault_hook, "before_install")
            try:
                _atomic_install_directory(stage, destination)
            except FileExistsError:
                installed = self.verify(bundle_digest, purpose=ActivationPurpose.NEW_ACTIVATION)
                _require_same_publication(installed, candidate, evaluation_report)
                return installed
            _fault(fault_hook, "after_install")
            return self.verify(bundle_digest, purpose=ActivationPurpose.NEW_ACTIVATION)
        except Exception:
            if stage.exists():
                _make_writable(stage)
                shutil.rmtree(stage)
            raise

    def verify(self, bundle_digest: str, *, purpose: ActivationPurpose) -> InstalledBundle:
        _digest(bundle_digest, "bundle")
        directory = self.root / bundle_digest
        manifest_path = directory / "manifest.json"
        if (
            not directory.is_dir()
            or directory.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.is_symlink()
        ):
            raise ArtifactError("bundle manifest is missing or unsafe")
        try:
            raw = manifest_path.read_bytes()
            if len(raw) > MAX_BUNDLE_MANIFEST_BYTES:
                raise ArtifactError("bundle manifest exceeds its bound")
            value = json.loads(raw)
            if not isinstance(value, dict) or canonical_bytes(value) != raw:
                raise ArtifactError("bundle manifest is not canonical")
            manifest = SignedManifest.from_dict(value)
            if manifest.kind != "factory_bundle":
                raise ArtifactError("bundle manifest has the wrong signed kind")
            payload = verify_manifest(manifest, self.trust_policy.bundle_trust_store)
        except ArtifactError:
            raise
        except (IntegrityError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ArtifactError("bundle manifest signature or encoding is invalid") from exc
        self.trust_policy.authorize(manifest.key_id, purpose)
        expected_fields = {
            "schema_version",
            "candidate_digest",
            "candidate_lineage_digest",
            "code_revision",
            "code_digest",
            "dependency_lock_digest",
            "data_snapshot_digest",
            "component_digests",
            "artifacts",
            "local_model_ids",
            "cloud_model_ids",
            "compatibility_contract_digest",
            "rollback_parent_digest",
            "evaluator_report_digest",
            "evaluator_key_id",
            "audit_generation_id",
            "evaluation_harness_digest",
            "whole_domain",
            "bundle_digest",
        }
        if set(payload) != expected_fields or payload["schema_version"] != (
            "strathmark-v3-whole-system-bundle-v1"
        ):
            raise ArtifactError("bundle payload schema is not closed")
        body = {name: payload[name] for name in payload if name != "bundle_digest"}
        if (
            payload["bundle_digest"] != bundle_digest
            or canonical_digest(body) != bundle_digest
            or payload["whole_domain"] is not True
        ):
            raise ArtifactError("bundle digest or whole-domain binding differs")
        if payload["evaluator_key_id"] == manifest.key_id:
            raise ArtifactError("evaluator and bundle signer identities are not separated")
        try:
            self.trust_policy.evaluator_trust_store.identity(payload["evaluator_key_id"])
        except IntegrityError as exc:
            raise ArtifactError("bundle evaluator signer is not trusted") from exc
        components = payload["component_digests"]
        if not isinstance(components, dict) or tuple(sorted(components)) != (
            REQUIRED_COMPONENT_ROLES
        ):
            raise ArtifactError("bundle component coverage is incomplete")
        for digest in components.values():
            _digest(digest, "bundle component")
        for name in ("local_model_ids", "cloud_model_ids"):
            models = payload[name]
            if (
                not isinstance(models, list)
                or models != sorted(set(models))
                or any(not _is_pinned_model_id(model) for model in models)
            ):
                raise ArtifactError("bundle model identities are not exact pinned revisions")
        artifacts = payload["artifacts"]
        if not isinstance(artifacts, dict) or not artifacts:
            raise ArtifactError("bundle artifact manifest is malformed")
        expected_paths = {"manifest.json", *artifacts}
        observed_paths = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file()
        }
        if observed_paths != expected_paths:
            raise ArtifactError("bundle contains missing or unexplained files")
        for relative, identity in artifacts.items():
            relative_path = PurePosixPath(relative)
            if (
                not isinstance(relative, str)
                or not relative
                or len(relative) > 240
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or "\\" in relative
                or not isinstance(identity, dict)
                or set(identity) != {"bytes", "sha256"}
                or isinstance(identity["bytes"], bool)
                or not isinstance(identity["bytes"], int)
                or identity["bytes"] <= 0
            ):
                raise ArtifactError("bundle artifact identity is malformed")
            _digest(identity["sha256"], "bundle artifact")
            path = directory.joinpath(*relative.split("/"))
            if not path.is_file() or path.is_symlink():
                raise ArtifactError("bundle artifact path is missing or unsafe")
            raw_artifact = path.read_bytes()
            if (
                len(raw_artifact) != identity["bytes"]
                or canonical_file_digest(raw_artifact) != identity["sha256"]
            ):
                raise ArtifactError("bundle artifact digest or size differs")
        for name in (
            "candidate_digest",
            "candidate_lineage_digest",
            "code_digest",
            "dependency_lock_digest",
            "data_snapshot_digest",
            "compatibility_contract_digest",
            "rollback_parent_digest",
            "evaluator_report_digest",
            "evaluation_harness_digest",
        ):
            _digest(payload[name], name.replace("_", " "))
        return InstalledBundle(
            bundle_digest,
            directory,
            manifest.key_id,
            payload["candidate_digest"],
            payload["evaluator_report_digest"],
            payload["rollback_parent_digest"],
            manifest,
        )

    def installed_digests(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                path.name
                for path in self.root.iterdir()
                if path.is_dir() and _DIGEST.fullmatch(path.name) is not None
            )
        )

    def preflight(
        self,
        bundle_digest: str,
        inventory: BundleRuntimeInventory,
        *,
        purpose: ActivationPurpose,
    ) -> BundlePreflightAttestation:
        if not isinstance(inventory, BundleRuntimeInventory):
            raise ArtifactError("bundle preflight requires a closed runtime inventory")
        installed = self.verify(bundle_digest, purpose=purpose)
        payload = verify_manifest(installed.manifest, self.trust_policy.bundle_trust_store)
        if inventory.download_attempted:
            raise ArtifactError("runtime preflight forbids model, package, or bundle downloads")
        if inventory.dependency_lock_digest != payload["dependency_lock_digest"]:
            raise ArtifactError("runtime dependency lock differs from pinned bundle")
        if inventory.compatibility_contract_digest != payload["compatibility_contract_digest"]:
            raise ArtifactError("runtime compatibility contract differs from pinned bundle")
        required_local = set(payload["local_model_ids"])
        if not required_local.issubset(inventory.installed_local_model_ids):
            raise ArtifactError("a pinned local model is not installed")
        if not required_local.issubset(inventory.warmed_local_model_ids):
            raise ArtifactError("a pinned local model is not warmed")
        if tuple(payload["cloud_model_ids"]) != inventory.configured_cloud_model_ids:
            raise ArtifactError("configured cloud model IDs or API revisions differ")
        if payload["cloud_model_ids"] and not inventory.cloud_credentials_configured:
            raise ArtifactError("cloud credentials are not configured outside the manifest")
        if not inventory.offline_fallbacks_ready:
            raise ArtifactError("offline fallback is not ready")
        parent = installed.rollback_parent_digest
        if parent != "0" * 64:
            self.verify(parent, purpose=ActivationPurpose.HISTORICAL_VERIFY)
        checks = (
            "artifacts_digest_verified",
            "cloud_ids_pinned",
            "credentials_external",
            "dependency_lock_matched",
            "downloads_forbidden",
            "local_models_present",
            "local_models_warmed",
            "offline_fallback_ready",
            "rollback_parent_present",
        )
        return BundlePreflightAttestation(
            bundle_digest,
            canonical_digest(inventory.body()),
            tuple(sorted(checks)),
        )

    def bundle_payload(self, bundle_digest: str) -> Mapping[str, object]:
        installed = self.verify(bundle_digest, purpose=ActivationPurpose.HISTORICAL_VERIFY)
        payload = verify_manifest(installed.manifest, self.trust_policy.bundle_trust_store)
        return MappingProxyType(payload)


def canonical_file_digest(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def _verify_report_for_publication(
    report: SignedEvaluationReport,
    candidate: CandidateBundle,
    *,
    evaluator_trust_store: IntegrityTrustStore,
) -> None:
    if not isinstance(report, SignedEvaluationReport):
        raise ArtifactError("bundle publication requires a typed evaluator report")
    try:
        payload = verify_manifest(report.manifest, evaluator_trust_store)
    except IntegrityError as exc:
        raise ArtifactError("evaluator report signer is not trusted") from exc
    if (
        payload.get("schema_version") != "strathmark-v3-factory-evaluation-report-v1"
        or payload.get("candidate_digest") != candidate.candidate_digest
        or payload.get("candidate_lineage_digest") != candidate.lineage_digest
        or payload.get("generation_id") != report.generation_id
        or payload.get("harness_digest") != report.harness_digest
        or payload.get("passed") is not report.passed
        or payload.get("failed_gates") != list(report.failed_gates)
    ):
        raise ArtifactError("evaluator report binding differs from candidate or typed result")


def _verify_staged(stage: Path, manifest: SignedManifest, candidate: CandidateBundle) -> None:
    observed = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
    if observed != {"manifest.json", *candidate.artifact_payloads}:
        raise ArtifactError("staged bundle file coverage differs")
    if canonical_bytes(manifest.to_dict()) != (stage / "manifest.json").read_bytes():
        raise ArtifactError("staged signed manifest differs before install")
    for relative, raw in candidate.artifact_payloads.items():
        if stage.joinpath(*relative.split("/")).read_bytes() != raw:
            raise ArtifactError("staged artifact bytes differ before install")


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o444)


def _make_writable(root: Path) -> None:
    for path in root.rglob("*"):
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
    try:
        root.chmod(0o700)
    except OSError:
        pass


def _atomic_install_directory(source: Path, destination: Path) -> None:
    """Install one staged directory without replacement and with durable rename semantics."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        if move(str(source), str(destination), 0x8):
            return
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(destination)
        raise ArtifactError(f"Windows write-through bundle install failed ({error})")
    os.rename(source, destination)
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_same_publication(
    installed: InstalledBundle,
    candidate: CandidateBundle,
    report: SignedEvaluationReport,
) -> None:
    if (
        installed.candidate_digest != candidate.candidate_digest
        or installed.evaluator_report_digest != report.report_digest
        or installed.rollback_parent_digest != candidate.rollback_parent_digest
    ):
        raise ArtifactError("existing bundle digest binds different publication material")


def _fault(hook: Callable[[str], None] | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ArtifactError(f"{label} digest must be lower-case SHA-256")
    return value


def _is_pinned_model_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 200
        and ":" in value
        and "@" in value
        and all(alias not in value.casefold() for alias in ("latest", "current", "stable"))
    )


__all__ = [
    "ActivationPurpose",
    "ArtifactError",
    "BundlePreflightAttestation",
    "BundleRepository",
    "BundleRuntimeInventory",
    "FactoryTrustPolicy",
    "InstalledBundle",
]
