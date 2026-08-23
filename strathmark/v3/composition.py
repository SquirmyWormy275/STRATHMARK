"""Side-effect-free construction of the immutable V3 runtime configuration.

This is the only V3 module allowed to read process environment.  Resolving a
snapshot validates paths but does not create directories, open storage, load a
provider, or otherwise construct runtime clients.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from strathmark.v3.contracts.canonical import DEFAULT_MAX_BYTES, DEFAULT_MAX_DEPTH, canonical_bytes
from strathmark.v3.contracts.errors import ConfigurationError

if TYPE_CHECKING:
    from strathmark.v3.factory.ml_training import (
        TrustedMLAuditAuthority,
        TrustedMLRoleAuthority,
    )
    from strathmark.v3.infrastructure.integrity import (
        P256EphemeralSigner,
        SignedManifest,
    )

_KNOWN_PRODUCTION_IDENTIFIERS = frozenset({"iordtvxryrdhqvdkfgzf", "production", "prod"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


@dataclass(frozen=True, slots=True)
class V3RuntimeConfig:
    """Validated immutable settings resolved before any runtime client exists."""

    database_path: Path
    temp_path: Path
    blob_root: Path
    bundle_root: Path
    archive_root: Path
    backup_root: Path
    recovery_root: Path
    integrity_key_root: Path
    test_mode: bool
    canonical_max_bytes: int = DEFAULT_MAX_BYTES
    canonical_max_depth: int = DEFAULT_MAX_DEPTH


def resolve_runtime_config(environment: Mapping[str, str] | None = None) -> V3RuntimeConfig:
    """Resolve and validate one immutable configuration snapshot without I/O."""

    source = os.environ if environment is None else environment
    test_mode = _parse_flag(source.get("STRATHMARK_TEST_DB", ""), "STRATHMARK_TEST_DB")
    default_root = Path.home() / ".strathmark" / "v3"
    database_path = _absolute_path(
        source.get("STRATHMARK_V3_DB_PATH", str(default_root / "strathmark-v3.sqlite3")),
        "STRATHMARK_V3_DB_PATH",
    )
    temp_path = _absolute_path(
        source.get("STRATHMARK_V3_TEMP_PATH", str(default_root / "runtime")),
        "STRATHMARK_V3_TEMP_PATH",
    )
    artifact_default_root = temp_path if test_mode else default_root
    blob_root = _absolute_path(
        source.get("STRATHMARK_V3_BLOB_ROOT", str(artifact_default_root / "blobs")),
        "STRATHMARK_V3_BLOB_ROOT",
    )
    bundle_root = _absolute_path(
        source.get("STRATHMARK_V3_BUNDLE_ROOT", str(artifact_default_root / "bundles")),
        "STRATHMARK_V3_BUNDLE_ROOT",
    )
    archive_root = _absolute_path(
        source.get("STRATHMARK_V3_ARCHIVE_ROOT", str(artifact_default_root / "archive")),
        "STRATHMARK_V3_ARCHIVE_ROOT",
    )
    backup_root = _absolute_path(
        source.get("STRATHMARK_V3_BACKUP_ROOT", str(artifact_default_root / "backup")),
        "STRATHMARK_V3_BACKUP_ROOT",
    )
    recovery_root = _absolute_path(
        source.get("STRATHMARK_V3_RECOVERY_ROOT", str(artifact_default_root / "recovery")),
        "STRATHMARK_V3_RECOVERY_ROOT",
    )
    integrity_key_root = _absolute_path(
        source.get(
            "STRATHMARK_V3_INTEGRITY_KEY_ROOT", str(artifact_default_root / "integrity-keys")
        ),
        "STRATHMARK_V3_INTEGRITY_KEY_ROOT",
    )
    max_bytes = _positive_integer(
        source.get("STRATHMARK_V3_CANONICAL_MAX_BYTES", str(DEFAULT_MAX_BYTES)),
        "STRATHMARK_V3_CANONICAL_MAX_BYTES",
    )
    max_depth = _positive_integer(
        source.get("STRATHMARK_V3_CANONICAL_MAX_DEPTH", str(DEFAULT_MAX_DEPTH)),
        "STRATHMARK_V3_CANONICAL_MAX_DEPTH",
    )

    mutable_paths = (
        database_path,
        temp_path,
        blob_root,
        bundle_root,
        archive_root,
        backup_root,
        recovery_root,
        integrity_key_root,
    )
    if len(set(mutable_paths)) != len(mutable_paths) or temp_path in database_path.parents:
        raise ConfigurationError("V3 database and mutable runtime paths must be separate")
    if test_mode:
        for path in mutable_paths:
            _reject_production_test_path(path)

    return V3RuntimeConfig(
        database_path=database_path,
        temp_path=temp_path,
        blob_root=blob_root,
        bundle_root=bundle_root,
        archive_root=archive_root,
        backup_root=backup_root,
        recovery_root=recovery_root,
        integrity_key_root=integrity_key_root,
        test_mode=test_mode,
        canonical_max_bytes=max_bytes,
        canonical_max_depth=max_depth,
    )


def compose_test_ml_candidate_authority(
    signed_manifest: SignedManifest,
    *,
    signer: P256EphemeralSigner,
) -> TrustedMLRoleAuthority:
    """Create an explicitly non-production ephemeral candidate authority for tests."""

    from strathmark.v3.factory.ml_training import (
        MLAuthorityEnvironment,
        _compose_ml_candidate_authority,
    )

    return _compose_ml_candidate_authority(
        signed_manifest,
        signer.identity,
        signer,
        environment=MLAuthorityEnvironment.TEST_EPHEMERAL,
    )


def compose_test_ml_audit_authority(
    signed_manifest: SignedManifest,
    *,
    signer: P256EphemeralSigner,
) -> TrustedMLAuditAuthority:
    """Create an explicitly non-production ephemeral audit authority for tests."""

    from strathmark.v3.factory.ml_training import (
        MLAuthorityEnvironment,
        _compose_ml_audit_authority,
    )

    return _compose_ml_audit_authority(
        signed_manifest,
        signer.identity,
        signer,
        environment=MLAuthorityEnvironment.TEST_EPHEMERAL,
    )


def compose_production_ml_authorities(
    config: V3RuntimeConfig,
) -> tuple[TrustedMLRoleAuthority, TrustedMLAuditAuthority]:
    """Load both ML authorities only from installation-owned CNG registry material."""

    if not isinstance(config, V3RuntimeConfig) or config.test_mode:
        raise ConfigurationError(
            "production ML composition requires a non-test runtime configuration"
        )
    candidate = _load_production_ml_authority(config, "candidate")
    audit = _load_production_ml_authority(config, "audit")
    return candidate, audit


def _load_production_ml_authority(
    config: V3RuntimeConfig, role_set: str
) -> TrustedMLRoleAuthority | TrustedMLAuditAuthority:
    from strathmark.v3.factory.ml_training import (
        MLAuthorityEnvironment,
        _compose_ml_audit_authority,
        _compose_ml_candidate_authority,
    )
    from strathmark.v3.infrastructure.integrity import (
        IntegrityError,
        IntegrityKeyClass,
        IntegrityKeyIdentity,
        P256WindowsCNGSigner,
        SignedManifest,
    )

    root = config.integrity_key_root
    identity_value = _read_installation_mapping(
        root / f"ml-{role_set}-public-identity.json", config
    )
    try:
        identity = IntegrityKeyIdentity.from_dict(identity_value)
    except IntegrityError as exc:
        raise ConfigurationError("installed ML public identity is invalid") from exc
    if identity.key_class is not IntegrityKeyClass.PRODUCTION_CNG:
        raise ConfigurationError(
            "production ML composition rejects non-CNG or test-ephemeral identity"
        )
    manifest_value = _read_installation_mapping(
        root / f"ml-{role_set}-role-manifest.json", config
    )
    try:
        manifest = SignedManifest.from_dict(manifest_value)
        key_name = _read_installation_key_name(
            root / f"ml-{role_set}-cng-key-name.txt"
        )
        signer = P256WindowsCNGSigner.open(key_name)
    except IntegrityError as exc:
        raise ConfigurationError("installed ML CNG authority material is invalid") from exc
    if signer.identity != identity:
        raise ConfigurationError(
            "installed ML public identity differs from the live Windows CNG key"
        )
    compose = (
        _compose_ml_audit_authority
        if role_set == "audit"
        else _compose_ml_candidate_authority
    )
    return compose(
        manifest,
        identity,
        signer,
        environment=MLAuthorityEnvironment.PRODUCTION_CNG,
    )


def _read_installation_mapping(
    path: Path, config: V3RuntimeConfig
) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError("installed ML authority file cannot be read") from exc
    if not raw or len(raw) > config.canonical_max_bytes:
        raise ConfigurationError("installed ML authority file exceeds its byte bound")
    try:
        value = json.loads(raw)
        encoded = canonical_bytes(
            value,
            max_bytes=config.canonical_max_bytes,
            max_depth=config.canonical_max_depth,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("installed ML authority file is not canonical JSON") from exc
    if not isinstance(value, Mapping) or encoded != raw:
        raise ConfigurationError("installed ML authority file is not a canonical object")
    return value


def _read_installation_key_name(path: Path) -> str:
    try:
        raw = path.read_bytes()
        value = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("installed ML CNG key name cannot be read") from exc
    if not value or len(raw) > 512 or value != value.strip() or "\x00" in value:
        raise ConfigurationError("installed ML CNG key name is invalid")
    return value


def _absolute_path(raw_value: str, label: str) -> Path:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ConfigurationError(f"{label} must be a nonempty filesystem path")
    if raw_value != raw_value.strip() or "\x00" in raw_value:
        raise ConfigurationError(f"{label} contains invalid path characters")
    try:
        return Path(raw_value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError(f"{label} is not a valid filesystem path") from exc


def _parse_flag(raw_value: str, label: str) -> bool:
    normalized = str(raw_value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{label} must be an explicit boolean flag")


def _positive_integer(raw_value: str, label: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} must be a positive integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{label} must be a positive integer")
    return value


def _reject_production_test_path(path: Path) -> None:
    normalized_parts = {part.casefold() for part in path.parts}
    normalized_text = path.as_posix().casefold()
    if (
        normalized_parts.intersection(_KNOWN_PRODUCTION_IDENTIFIERS)
        or "iordtvxryrdhqvdkfgzf" in normalized_text
        or normalized_text.endswith("/.strathmark/results.db")
        or normalized_text.endswith("/.strathmark/v3/strathmark-v3.sqlite3")
    ):
        raise ConfigurationError(
            "test configuration refuses a known production database or runtime path"
        )


__all__ = [
    "V3RuntimeConfig",
    "compose_production_ml_authorities",
    "compose_test_ml_audit_authority",
    "compose_test_ml_candidate_authority",
    "resolve_runtime_config",
]
