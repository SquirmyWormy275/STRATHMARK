"""External ECDSA integrity anchors and zero-loss issue recovery journal.

Private signing material is supplied through a narrow signer port and never serialized.
Development uses an explicitly marked ephemeral P-256 key.  Production readiness accepts
only a Windows-CNG identity whose implementation keeps the private key non-exportable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.evidence import require_utc_milliseconds
from strathmark.v3.contracts.identifiers import require_identifier

_TOKEN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE_ALGORITHM = "ecdsa-p256-sha256"
_REGISTRY_RECORD_MAX_BYTES = 1_048_576
_REGISTRY_READ_ATTEMPTS = 5


class IntegrityError(RuntimeError):
    """A signed integrity or critical-recovery assertion failed closed."""


class IntegrityKeyClass(str, Enum):
    DEVELOPMENT_EPHEMERAL = "development_ephemeral"
    PRODUCTION_CNG = "production_cng"


@dataclass(frozen=True, slots=True)
class IntegrityKeyIdentity:
    """Public verification metadata, never proof that a production private key exists."""

    key_id: str
    key_class: IntegrityKeyClass
    provider: str
    public_key_der_b64: str

    def __post_init__(self) -> None:
        _require_token(self.key_id, "integrity key id")
        if not isinstance(self.key_class, IntegrityKeyClass):
            raise IntegrityError("integrity key class must be explicit")
        _require_token(self.provider, "integrity key provider")
        try:
            encoded = base64.b64decode(self.public_key_der_b64, validate=True)
        except (TypeError, ValueError) as exc:
            raise IntegrityError("integrity public key must be canonical base64 DER") from exc
        if not 64 <= len(encoded) <= 512:
            raise IntegrityError("integrity public key DER length is invalid")
        _load_public_key(encoded)

    def to_dict(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "key_class": self.key_class.value,
            "provider": self.provider,
            "public_key_der_b64": self.public_key_der_b64,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> IntegrityKeyIdentity:
        _require_fields(value, {"key_id", "key_class", "provider", "public_key_der_b64"})
        try:
            key_class = IntegrityKeyClass(value["key_class"])
        except (TypeError, ValueError) as exc:
            raise IntegrityError("unknown integrity key class") from exc
        return cls(value["key_id"], key_class, value["provider"], value["public_key_der_b64"])


class P256Signer(Protocol):
    @property
    def identity(self) -> IntegrityKeyIdentity: ...

    def sign(self, payload: bytes) -> bytes: ...


class P256EphemeralSigner:
    """Ephemeral CI/development signer that production readiness always rejects."""

    def __init__(self, key_id: str, private_key: Any) -> None:
        serialization, _ec, _hashes, _invalid = _crypto()
        public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._private_key = private_key
        self._identity = IntegrityKeyIdentity(
            key_id,
            IntegrityKeyClass.DEVELOPMENT_EPHEMERAL,
            "cryptography_ephemeral_p256_sha256",
            base64.b64encode(public_der).decode("ascii"),
        )

    @classmethod
    def generate(cls, key_id: str) -> P256EphemeralSigner:
        _serialization, ec, _hashes, _invalid = _crypto()
        return cls(key_id, ec.generate_private_key(ec.SECP256R1()))

    @property
    def identity(self) -> IntegrityKeyIdentity:
        return self._identity

    def sign(self, payload: bytes) -> bytes:
        if not isinstance(payload, bytes):
            raise IntegrityError("signing payload must be immutable bytes")
        _serialization, ec, hashes, _invalid = _crypto()
        return self._private_key.sign(payload, ec.ECDSA(hashes.SHA256()))


class P256ExternalSigner:
    """Rehearsal adapter for callback-backed keys; never production-authoritative."""

    def __init__(self, identity: IntegrityKeyIdentity, callback: Callable[[bytes], bytes]) -> None:
        if not isinstance(identity, IntegrityKeyIdentity):
            raise IntegrityError("external signer requires an integrity key identity")
        if identity.key_class is IntegrityKeyClass.PRODUCTION_CNG:
            raise IntegrityError(
                "callback-backed signers cannot assert production Windows CNG authority"
            )
        if not callable(callback):
            raise IntegrityError("external signer callback must be callable")
        self._identity = identity
        self._callback = callback

    @property
    def identity(self) -> IntegrityKeyIdentity:
        return self._identity

    def sign(self, payload: bytes) -> bytes:
        signature = self._callback(payload)
        if not isinstance(signature, bytes) or not signature:
            raise IntegrityError("external signer returned an invalid signature")
        _verify_signature(self.identity, payload, signature)
        return signature


_CNG_SIGNER_TOKEN = object()
_CNG_PROVIDER_NAMES = {
    "Microsoft Software Key Storage Provider",
    "Microsoft Platform Crypto Provider",
}


class P256WindowsCNGSigner:
    """Provider-attested non-exportable P-256 signer opened directly from Windows CNG.

    No constructor accepts a callback or private key.  ``open`` asks NCrypt for an existing
    persisted key, verifies that its export policy is zero, exports only its P-256 public
    coordinates, and retains the provider handle for signing.  Production gates re-attest
    the provider/key binding instead of trusting serialized key-class/provider strings.
    """

    def __init__(self, backend: Any, *, _token: object | None = None) -> None:
        if _token is not _CNG_SIGNER_TOKEN or not isinstance(backend, _WindowsCNGProviderKey):
            raise IntegrityError("production CNG signer must be opened through the OS provider")
        self._backend = backend
        public_der = backend.attest_public_key()
        key_id = f"integrity-key:cng-{canonical_digest({'public_key_der': base64.b64encode(public_der).decode('ascii')})}"
        self._identity = IntegrityKeyIdentity(
            key_id,
            IntegrityKeyClass.PRODUCTION_CNG,
            "windows_cng_p256_sha256",
            base64.b64encode(public_der).decode("ascii"),
        )

    @classmethod
    def open(
        cls,
        key_name: str,
        *,
        provider_name: str = "Microsoft Software Key Storage Provider",
    ) -> P256WindowsCNGSigner:
        if (
            not isinstance(key_name, str)
            or not key_name
            or len(key_name) > 512
            or "\x00" in key_name
        ):
            raise IntegrityError("Windows CNG key name must be a bounded nonempty string")
        if provider_name not in _CNG_PROVIDER_NAMES:
            raise IntegrityError("Windows CNG provider is not approved for production integrity")
        backend = _WindowsCNGProviderKey.open(provider_name, key_name)
        return cls(backend, _token=_CNG_SIGNER_TOKEN)

    @property
    def identity(self) -> IntegrityKeyIdentity:
        return self._identity

    def attest_provider(self) -> None:
        observed = self._backend.attest_public_key()
        expected = base64.b64decode(self.identity.public_key_der_b64, validate=True)
        if observed != expected:
            raise IntegrityError("Windows CNG provider key changed after attestation")

    def sign(self, payload: bytes) -> bytes:
        if not isinstance(payload, bytes):
            raise IntegrityError("signing payload must be immutable bytes")
        self.attest_provider()
        signature = self._backend.sign_digest(hashlib.sha256(payload).digest())
        _verify_signature(self.identity, payload, signature)
        return signature


class _WindowsCNGProviderKey:
    """Small NCrypt handle adapter; only public material ever leaves the provider."""

    def __init__(self, library: Any, provider_handle: Any, key_handle: Any) -> None:
        self._library = library
        self._provider_handle = provider_handle
        self._key_handle = key_handle

    @classmethod
    def open(cls, provider_name: str, key_name: str) -> _WindowsCNGProviderKey:
        if os.name != "nt":
            raise IntegrityError("production Windows CNG signing is unavailable on this OS")
        import ctypes
        from ctypes import wintypes

        library = ctypes.WinDLL("ncrypt.dll", use_last_error=True)
        provider_handle = wintypes.HANDLE()
        key_handle = wintypes.HANDLE()
        status = library.NCryptOpenStorageProvider(ctypes.byref(provider_handle), provider_name, 0)
        if status != 0:
            raise IntegrityError(f"Windows CNG provider open failed (0x{status & 0xFFFFFFFF:08x})")
        try:
            status = library.NCryptOpenKey(
                provider_handle, ctypes.byref(key_handle), key_name, 0, 0x40
            )
            if status != 0:
                raise IntegrityError(f"Windows CNG key open failed (0x{status & 0xFFFFFFFF:08x})")
            result = cls(library, provider_handle, key_handle)
            result._require_nonexportable()
            result.attest_public_key()
            return result
        except Exception:
            if key_handle.value:
                library.NCryptFreeObject(key_handle)
            library.NCryptFreeObject(provider_handle)
            raise

    def _require_nonexportable(self) -> None:
        import ctypes
        from ctypes import wintypes

        policy = wintypes.DWORD()
        returned = wintypes.DWORD()
        status = self._library.NCryptGetProperty(
            self._key_handle,
            "Export Policy",
            ctypes.byref(policy),
            ctypes.sizeof(policy),
            ctypes.byref(returned),
            0,
        )
        if status != 0 or returned.value != ctypes.sizeof(policy) or policy.value != 0:
            raise IntegrityError("Windows CNG integrity key is exportable or cannot prove policy")

    def attest_public_key(self) -> bytes:
        import ctypes
        from ctypes import wintypes

        size = wintypes.DWORD()
        status = self._library.NCryptExportKey(
            self._key_handle, 0, "ECCPUBLICBLOB", None, None, 0, ctypes.byref(size), 0
        )
        if status != 0 or size.value != 72:
            raise IntegrityError("Windows CNG key is not an exportable P-256 public identity")
        buffer = (ctypes.c_ubyte * size.value)()
        status = self._library.NCryptExportKey(
            self._key_handle,
            0,
            "ECCPUBLICBLOB",
            None,
            buffer,
            size.value,
            ctypes.byref(size),
            0,
        )
        if status != 0:
            raise IntegrityError(
                f"Windows CNG public-key export failed (0x{status & 0xFFFFFFFF:08x})"
            )
        blob = bytes(buffer)
        magic, coordinate_bytes = struct.unpack("<II", blob[:8])
        if magic != 0x31534345 or coordinate_bytes != 32 or len(blob) != 72:
            raise IntegrityError("Windows CNG key is not ECDSA P-256")
        _serialization, ec, _hashes, _invalid = _crypto()
        public = ec.EllipticCurvePublicNumbers(
            int.from_bytes(blob[8:40], "big"),
            int.from_bytes(blob[40:72], "big"),
            ec.SECP256R1(),
        ).public_key()
        return public.public_bytes(
            _serialization.Encoding.DER,
            _serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def sign_digest(self, digest: bytes) -> bytes:
        import ctypes
        from ctypes import wintypes

        size = wintypes.DWORD()
        status = self._library.NCryptSignHash(
            self._key_handle, None, digest, len(digest), None, 0, ctypes.byref(size), 0
        )
        if status != 0 or size.value != 64:
            raise IntegrityError("Windows CNG P-256 signature sizing failed")
        buffer = (ctypes.c_ubyte * size.value)()
        status = self._library.NCryptSignHash(
            self._key_handle,
            None,
            digest,
            len(digest),
            buffer,
            size.value,
            ctypes.byref(size),
            0,
        )
        if status != 0:
            raise IntegrityError(f"Windows CNG signing failed (0x{status & 0xFFFFFFFF:08x})")
        _serialization, _ec, _hashes, _invalid = _crypto()
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        raw = bytes(buffer)
        return encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        )


def require_production_cng_signer(signer: P256Signer) -> IntegrityKeyIdentity:
    """Return identity only after a live OS-provider re-attestation."""

    if not isinstance(signer, P256WindowsCNGSigner):
        raise IntegrityError("production authority requires an OS-attested Windows CNG signer")
    signer.attest_provider()
    return signer.identity


@dataclass(frozen=True, slots=True)
class SignedManifest:
    kind: str
    body_json: str
    body_digest: str
    key_id: str
    signature_der_b64: str
    schema_version: str = "strathmark-v3-signed-manifest-v1"

    def __post_init__(self) -> None:
        _require_token(self.kind, "manifest kind")
        _require_token(self.key_id, "manifest key id")
        _require_digest(self.body_digest, "manifest body digest")
        try:
            value = json.loads(self.body_json)
            encoded = canonical_bytes(value)
        except Exception as exc:
            raise IntegrityError("signed manifest body is not canonical JSON") from exc
        if encoded.decode("utf-8") != self.body_json or canonical_digest(value) != self.body_digest:
            raise IntegrityError("signed manifest body digest or encoding differs")
        try:
            signature = base64.b64decode(self.signature_der_b64, validate=True)
        except (TypeError, ValueError) as exc:
            raise IntegrityError("manifest signature must be canonical base64 DER") from exc
        if not signature:
            raise IntegrityError("manifest signature cannot be empty")
        if self.schema_version != "strathmark-v3-signed-manifest-v1":
            raise IntegrityError("unsupported signed manifest version")

    def body(self) -> dict[str, Any]:
        value = json.loads(self.body_json)
        if not isinstance(value, dict):
            raise IntegrityError("signed manifest body must be an object")
        return value

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "body_json": self.body_json,
            "body_digest": self.body_digest,
            "key_id": self.key_id,
            "signature_der_b64": self.signature_der_b64,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SignedManifest:
        _require_fields(
            value,
            {
                "schema_version",
                "kind",
                "body_json",
                "body_digest",
                "key_id",
                "signature_der_b64",
            },
        )
        return cls(
            value["kind"],
            value["body_json"],
            value["body_digest"],
            value["key_id"],
            value["signature_der_b64"],
            value["schema_version"],
        )


class IntegrityTrustStore:
    def __init__(self, identities: tuple[IntegrityKeyIdentity, ...]) -> None:
        if not isinstance(identities, tuple) or not identities:
            raise IntegrityError("integrity trust store requires an immutable nonempty key set")
        if not all(isinstance(item, IntegrityKeyIdentity) for item in identities):
            raise IntegrityError("integrity trust store contains an invalid key")
        material = {item.key_id: item for item in identities}
        if len(material) != len(identities):
            raise IntegrityError("integrity trust store cannot repeat a key id")
        self._identities = MappingProxyType(material)

    def identity(self, key_id: str) -> IntegrityKeyIdentity:
        try:
            return self._identities[key_id]
        except KeyError as exc:
            raise IntegrityError("manifest signer is not trusted") from exc

    def add(self, identity: IntegrityKeyIdentity) -> IntegrityTrustStore:
        if identity.key_id in self._identities:
            raise IntegrityError("rotated integrity key id already exists")
        return IntegrityTrustStore((*self._identities.values(), identity))

    @property
    def identities(self) -> tuple[IntegrityKeyIdentity, ...]:
        return tuple(self._identities.values())


def sign_manifest(
    kind: str,
    payload: Mapping[str, Any],
    *,
    signer: P256Signer,
    created_at: str,
) -> SignedManifest:
    _require_token(kind, "manifest kind")
    if not isinstance(payload, Mapping):
        raise IntegrityError("manifest payload must be a mapping")
    timestamp = require_utc_milliseconds(created_at)
    identity = signer.identity
    body = {
        "schema_version": "strathmark-v3-integrity-body-v1",
        "kind": kind,
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": identity.key_id,
        "created_at": timestamp,
        "payload": dict(payload),
    }
    encoded = canonical_bytes(body)
    signature = signer.sign(encoded)
    return SignedManifest(
        kind,
        encoded.decode("utf-8"),
        canonical_digest(body),
        identity.key_id,
        base64.b64encode(signature).decode("ascii"),
    )


def verify_manifest(manifest: SignedManifest, trust_store: IntegrityTrustStore) -> dict[str, Any]:
    if not isinstance(manifest, SignedManifest) or not isinstance(trust_store, IntegrityTrustStore):
        raise IntegrityError("manifest verification requires typed manifest and trust store")
    body = manifest.body()
    if (
        body.get("schema_version") != "strathmark-v3-integrity-body-v1"
        or body.get("kind") != manifest.kind
        or body.get("algorithm") != SIGNATURE_ALGORITHM
        or body.get("key_id") != manifest.key_id
    ):
        raise IntegrityError("signed manifest body binding differs")
    identity = trust_store.identity(manifest.key_id)
    signature = base64.b64decode(manifest.signature_der_b64, validate=True)
    _verify_signature(identity, manifest.body_json.encode("utf-8"), signature)
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise IntegrityError("signed manifest payload must be an object")
    return payload


def sign_key_rotation(
    current_signer: P256Signer,
    next_identity: IntegrityKeyIdentity,
    *,
    created_at: str,
) -> SignedManifest:
    return sign_manifest(
        "key_rotation",
        {"next_key": next_identity.to_dict()},
        signer=current_signer,
        created_at=created_at,
    )


def apply_key_rotation(
    trust_store: IntegrityTrustStore, rotation: SignedManifest
) -> IntegrityTrustStore:
    if rotation.kind != "key_rotation":
        raise IntegrityError("integrity rotation requires a key-rotation manifest")
    payload = verify_manifest(rotation, trust_store)
    next_key = payload.get("next_key")
    if not isinstance(next_key, dict):
        raise IntegrityError("key rotation does not contain a next key")
    return trust_store.add(IntegrityKeyIdentity.from_dict(next_key))


@dataclass(frozen=True, slots=True)
class TrustedCheckpoint:
    """A checkpoint whose signature and registry chain have been independently verified."""

    manifest: SignedManifest
    checkpoint_sequence: int
    authority_sequence: int
    authority_digest: str
    schema_digest: str
    projection_digest: str
    aggregate_heads_digest: str


class CheckpointRegistry:
    """External append-only trust, rotation, and authority-checkpoint registry.

    The registry lives outside SQLite.  Numbered immutable records make gaps and
    reordering observable, rotations retain every old public key, and each checkpoint
    links to its predecessor.  A database can advance beyond the latest checkpoint,
    but it can never verify after losing the independently anchored event.
    """

    def __init__(self, root: Path | str, *, bootstrap_identity: IntegrityKeyIdentity) -> None:
        if not isinstance(bootstrap_identity, IntegrityKeyIdentity):
            raise IntegrityError("checkpoint registry requires a bootstrap key identity")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.rotations_root = self.root / "rotations"
        self.checkpoints_root = self.root / "checkpoints"
        self.rotations_root.mkdir(parents=True, exist_ok=True)
        self.checkpoints_root.mkdir(parents=True, exist_ok=True)
        bootstrap_path = self.root / "bootstrap-key.json"
        bootstrap_bytes = canonical_bytes(bootstrap_identity.to_dict())
        won = _publish_no_clobber(bootstrap_path, bootstrap_bytes)
        try:
            observed = bootstrap_path.read_bytes()
        except OSError as exc:
            raise IntegrityError("checkpoint bootstrap identity cannot be read") from exc
        if observed != bootstrap_bytes:
            raise IntegrityError("checkpoint bootstrap identity differs")
        self._bootstrap_identity = bootstrap_identity
        self._trust_store, self._active_identity, self._rotation_digest = self._load_rotations()
        self._checkpoints = self._load_checkpoints()

    @property
    def trust_store(self) -> IntegrityTrustStore:
        return self._trust_store

    @property
    def active_identity(self) -> IntegrityKeyIdentity:
        return self._active_identity

    def rotate_key(
        self,
        current_signer: P256Signer,
        next_identity: IntegrityKeyIdentity,
        *,
        created_at: str,
    ) -> SignedManifest:
        if current_signer.identity != self._active_identity:
            raise IntegrityError("key rotation must be signed by the active registry key")
        sequence = len(tuple(self.rotations_root.glob("*.json"))) + 1
        payload = {
            "rotation_sequence": sequence,
            "previous_rotation_digest": self._rotation_digest,
            "next_key": next_identity.to_dict(),
        }
        manifest = sign_manifest(
            "key_rotation", payload, signer=current_signer, created_at=created_at
        )
        path = self.rotations_root / f"{sequence:016d}.json"
        encoded = canonical_bytes(manifest.to_dict())
        if not _publish_no_clobber(path, encoded):
            winner = _read_signed_registry_record(path, "key_rotation")
            if verify_manifest(winner, self._trust_store) != payload:
                raise IntegrityError("rotation sequence already binds different material")
            manifest = winner
        self._trust_store, self._active_identity, self._rotation_digest = self._load_rotations()
        return manifest

    def create_checkpoint(
        self,
        database_path: Path | str,
        *,
        signer: P256Signer,
        created_at: str,
        outbox: Any | None = None,
    ) -> TrustedCheckpoint:
        if signer.identity != self._active_identity:
            raise IntegrityError("checkpoint must be signed by the active registry key")
        previous = self._checkpoints[-1] if self._checkpoints else None
        facts = _database_checkpoint_facts(
            Path(database_path),
            required_ancestor=(
                None
                if previous is None
                else (previous.authority_sequence, previous.authority_digest)
            ),
        )
        if (
            previous is not None
            and facts["authority_anchor"]["global_sequence"] == previous.authority_sequence
        ):
            if (
                facts["authority_anchor"]["event_digest"] != previous.authority_digest
                or facts["schema_digest"] != previous.schema_digest
                or facts["projection_digest"] != previous.projection_digest
                or facts["aggregate_heads_digest"] != previous.aggregate_heads_digest
            ):
                raise IntegrityError(
                    "checkpoint at an unchanged authority sequence differs from its predecessor"
                )
        sequence = 1 if previous is None else previous.checkpoint_sequence + 1
        payload = {
            "checkpoint_sequence": sequence,
            "previous_checkpoint_digest": (
                "0" * 64 if previous is None else previous.manifest.body_digest
            ),
            **facts,
        }
        manifest = sign_manifest("checkpoint", payload, signer=signer, created_at=created_at)
        path = self.checkpoints_root / f"{sequence:016d}.json"
        encoded = canonical_bytes(manifest.to_dict())
        if not _publish_no_clobber(path, encoded):
            winner = _read_signed_registry_record(path, "checkpoint")
            if verify_manifest(winner, self._trust_store) != payload:
                raise IntegrityError("checkpoint sequence already binds different material")
            manifest = winner
            encoded = canonical_bytes(winner.to_dict())
        self._checkpoints = self._load_checkpoints()
        checkpoint = self._checkpoints[-1]
        if outbox is not None:
            outbox.enqueue(
                outbox_id=f"checkpoint:{manifest.body_digest}",
                destination="integrity-checkpoint-export",
                payload={
                    "schema_version": "strathmark-v3-checkpoint-export-v1",
                    "signed_checkpoint_b64": base64.b64encode(encoded).decode("ascii"),
                },
                created_at=created_at,
                source_global_sequence=(checkpoint.authority_sequence or None),
            )
        return checkpoint

    def latest_checkpoint(self) -> TrustedCheckpoint:
        if not self._checkpoints:
            raise IntegrityError("checkpoint registry contains no trusted checkpoint")
        return self._checkpoints[-1]

    def verify_checkpoint(self, manifest: SignedManifest) -> TrustedCheckpoint:
        if not any(item.manifest == manifest for item in self._checkpoints):
            raise IntegrityError("checkpoint is not present in the immutable registry")
        payload = verify_manifest(manifest, self._trust_store)
        return _trusted_checkpoint(manifest, payload)

    def verify_database(
        self, database_path: Path | str, *, require_current: bool
    ) -> TrustedCheckpoint:
        checkpoint = self.latest_checkpoint()
        from strathmark.v3.infrastructure.sqlite.event_store import (
            AuthorityAnchor,
            verify_read_only_authority,
        )

        anchor = AuthorityAnchor(checkpoint.authority_sequence, checkpoint.authority_digest)
        try:
            current = verify_read_only_authority(Path(database_path), trusted_anchor=anchor)
        except Exception as exc:
            raise IntegrityError("authority does not contain the signed checkpoint") from exc
        if require_current:
            facts = _database_checkpoint_facts(Path(database_path))
            if (
                current.global_sequence != checkpoint.authority_sequence
                or current.event_digest != checkpoint.authority_digest
                or facts["schema_digest"] != checkpoint.schema_digest
                or facts["projection_digest"] != checkpoint.projection_digest
                or facts["aggregate_heads_digest"] != checkpoint.aggregate_heads_digest
            ):
                raise IntegrityError("database differs from the current signed checkpoint")
        return checkpoint

    def _load_rotations(
        self,
    ) -> tuple[IntegrityTrustStore, IntegrityKeyIdentity, str]:
        trust = IntegrityTrustStore((self._bootstrap_identity,))
        active = self._bootstrap_identity
        previous_digest = "0" * 64
        paths = _numbered_registry_paths(self.rotations_root, "rotation")
        for expected_sequence, path in enumerate(paths, start=1):
            manifest = _read_signed_registry_record(path, "key_rotation")
            if manifest.key_id != active.key_id:
                raise IntegrityError("rotation is not signed by the active key")
            payload = verify_manifest(manifest, trust)
            if payload.get("rotation_sequence") != expected_sequence:
                raise IntegrityError("rotation sequence has a gap or reorder")
            if payload.get("previous_rotation_digest") != previous_digest:
                raise IntegrityError("rotation digest chain has a gap or reorder")
            next_key = payload.get("next_key")
            if not isinstance(next_key, dict):
                raise IntegrityError("rotation next key is malformed")
            active = IntegrityKeyIdentity.from_dict(next_key)
            trust = trust.add(active)
            previous_digest = manifest.body_digest
        return trust, active, previous_digest

    def _load_checkpoints(self) -> tuple[TrustedCheckpoint, ...]:
        records: list[TrustedCheckpoint] = []
        previous_digest = "0" * 64
        paths = _numbered_registry_paths(self.checkpoints_root, "checkpoint")
        for expected_sequence, path in enumerate(paths, start=1):
            manifest = _read_signed_registry_record(path, "checkpoint")
            payload = verify_manifest(manifest, self._trust_store)
            checkpoint = _trusted_checkpoint(manifest, payload)
            if checkpoint.checkpoint_sequence != expected_sequence:
                raise IntegrityError("checkpoint sequence has a gap or reorder")
            if payload.get("previous_checkpoint_digest") != previous_digest:
                raise IntegrityError("checkpoint digest chain has a gap or reorder")
            records.append(checkpoint)
            previous_digest = manifest.body_digest
        return tuple(records)


@dataclass(frozen=True, slots=True)
class StorageIdentity:
    device_id: str
    host_id: str
    site_id: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.device_id, "device id"),
            (self.host_id, "host id"),
            (self.site_id, "site id"),
        ):
            _require_token(value, label)

    def to_dict(self) -> dict[str, str]:
        return {"device_id": self.device_id, "host_id": self.host_id, "site_id": self.site_id}


_TOPOLOGY_PROBE_TOKEN = object()


class VerifiedRecoveryTopology:
    """OS-derived physical-device proof; caller labels cannot construct this type."""

    def __init__(
        self,
        *args: object,
        _probe_token: object | None = None,
        primary: StorageIdentity | None = None,
        recovery: StorageIdentity | None = None,
    ) -> None:
        if _probe_token is not _TOPOLOGY_PROBE_TOKEN or args:
            raise IntegrityError("recovery topology must be created by the OS probe")
        assert primary is not None and recovery is not None
        self.primary = primary
        self.recovery = recovery
        self.distinct_physical_devices = primary.device_id != recovery.device_id

    @classmethod
    def probe(
        cls,
        primary_path: Path | str,
        recovery_path: Path | str,
        *,
        host_id: str,
        site_id: str,
    ) -> VerifiedRecoveryTopology:
        _require_token(host_id, "recovery host id")
        _require_token(site_id, "recovery site id")
        primary = Path(primary_path).expanduser().resolve(strict=True)
        recovery = Path(recovery_path).expanduser().resolve(strict=True)
        return cls(
            _probe_token=_TOPOLOGY_PROBE_TOKEN,
            primary=StorageIdentity(_physical_device_id(primary), host_id, site_id),
            recovery=StorageIdentity(_physical_device_id(recovery), host_id, site_id),
        )


@dataclass(frozen=True, slots=True)
class IssueRecoveryReadiness:
    ready: bool
    reasons: tuple[str, ...]


def issue_recovery_readiness(
    primary: StorageIdentity,
    recovery: StorageIdentity,
    signer: P256Signer,
) -> IssueRecoveryReadiness:
    reasons: list[str] = []
    try:
        require_production_cng_signer(signer)
    except IntegrityError:
        reasons.append("development_signing_key")
    if primary.device_id == recovery.device_id:
        reasons.append("recovery_device_not_distinct")
    return IssueRecoveryReadiness(not reasons, tuple(reasons))


@dataclass(frozen=True, slots=True)
class CriticalIssueIntent:
    command_id: str
    command_digest: str
    approval_snapshot_digest: str
    expected_versions: tuple[tuple[str, int], ...]
    receipt_ids: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        _require_token(self.command_id, "critical command id")
        _require_digest(self.command_digest, "critical command digest")
        _require_digest(self.approval_snapshot_digest, "approval snapshot digest")
        if (
            not isinstance(self.expected_versions, tuple)
            or tuple(sorted(self.expected_versions)) != self.expected_versions
        ):
            raise IntegrityError("critical expected versions must be an immutable sorted tuple")
        if not self.expected_versions or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or isinstance(item[1], bool)
            or not isinstance(item[1], int)
            or item[1] < 0
            for item in self.expected_versions
        ):
            raise IntegrityError("critical expected versions are invalid")
        try:
            aggregate_ids = tuple(
                str(require_identifier(item[0])) for item in self.expected_versions
            )
        except Exception as exc:
            raise IntegrityError(
                "critical expected versions contain an invalid aggregate id"
            ) from exc
        if len(set(aggregate_ids)) != len(aggregate_ids):
            raise IntegrityError("critical expected versions cannot repeat an aggregate")
        if not isinstance(self.receipt_ids, tuple) or not self.receipt_ids:
            raise IntegrityError("critical issue intent requires immutable receipt identities")
        for receipt in self.receipt_ids:
            _require_token(receipt, "critical receipt id")
        if len(set(self.receipt_ids)) != len(self.receipt_ids):
            raise IntegrityError("critical issue intent cannot repeat a receipt")
        require_utc_milliseconds(self.created_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_digest": self.command_digest,
            "approval_snapshot_digest": self.approval_snapshot_digest,
            "expected_versions": [list(item) for item in self.expected_versions],
            "receipt_ids": list(self.receipt_ids),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CriticalDatabaseCommit:
    global_sequence: int
    result_digest: str
    receipt_ids: tuple[str, ...]
    intent_digest: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.global_sequence, bool)
            or not isinstance(self.global_sequence, int)
            or self.global_sequence <= 0
        ):
            raise IntegrityError("critical commit sequence must be positive")
        _require_digest(self.result_digest, "critical result digest")
        _require_digest(self.intent_digest, "critical intent digest")
        if not isinstance(self.receipt_ids, tuple) or not self.receipt_ids:
            raise IntegrityError("critical commit receipt identities are required")
        for receipt in self.receipt_ids:
            _require_token(receipt, "critical commit receipt id")
        if len(set(self.receipt_ids)) != len(self.receipt_ids):
            raise IntegrityError("critical commit cannot repeat a receipt")

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_sequence": self.global_sequence,
            "result_digest": self.result_digest,
            "receipt_ids": list(self.receipt_ids),
            "intent_digest": self.intent_digest,
        }


class RecoveryState(str, Enum):
    INTENT_ONLY = "intent_only"
    COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    command_id: str
    state: RecoveryState
    commit: CriticalDatabaseCommit | None


class CriticalJournal:
    def __init__(
        self,
        root: Path | str,
        *,
        signer: P256Signer,
        trust_store: IntegrityTrustStore,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.signer = signer
        self.trust_store = trust_store

    def intent_path(self, command_id: str) -> Path:
        _require_token(command_id, "critical command id")
        return self.root / f"{canonical_digest({'command_id': command_id})}.intent.json"

    def marker_path(self, command_id: str) -> Path:
        _require_token(command_id, "critical command id")
        return self.root / f"{canonical_digest({'command_id': command_id})}.commit.json"

    def record_intent(self, intent: CriticalIssueIntent) -> SignedManifest:
        manifest = sign_manifest(
            "critical_intent", intent.to_dict(), signer=self.signer, created_at=intent.created_at
        )
        path = self.intent_path(intent.command_id)
        won = _publish_no_clobber(path, canonical_bytes(manifest.to_dict()))
        winner = manifest if won else self._read(path, "critical_intent")
        if verify_manifest(winner, self.trust_store) != intent.to_dict():
            raise IntegrityError("critical command identity binds different intent")
        return winner

    def record_commit(
        self, command_id: str, commit: CriticalDatabaseCommit, *, created_at: str
    ) -> SignedManifest:
        payload = {"command_id": command_id, **commit.to_dict()}
        manifest = sign_manifest(
            "critical_commit", payload, signer=self.signer, created_at=created_at
        )
        path = self.marker_path(command_id)
        won = _publish_no_clobber(path, canonical_bytes(manifest.to_dict()))
        winner = manifest if won else self._read(path, "critical_commit")
        if verify_manifest(winner, self.trust_store) != payload:
            raise IntegrityError("critical command identity binds different commit marker")
        return winner

    def reconcile(
        self,
        *,
        database_lookup: Callable[[str], CriticalDatabaseCommit | None],
        manager_receipts: Mapping[str, tuple[str, ...]],
    ) -> tuple[RecoveryRecord, ...]:
        if not callable(database_lookup) or not isinstance(manager_receipts, Mapping):
            raise IntegrityError("critical reconciliation requires lookup and manager mapping")
        all_json = set(self.root.glob("*.json"))
        intent_paths = set(self.root.glob("*.intent.json"))
        marker_paths = set(self.root.glob("*.commit.json"))
        if all_json != intent_paths | marker_paths:
            raise IntegrityError("critical journal contains an unexplained manifest file")
        intent_stems = {path.name.removesuffix(".intent.json") for path in intent_paths}
        marker_stems = {path.name.removesuffix(".commit.json") for path in marker_paths}
        if marker_stems - intent_stems:
            raise IntegrityError("critical journal contains an orphan commit marker")
        records: list[RecoveryRecord] = []
        seen_commands: set[str] = set()
        for path in sorted(self.root.glob("*.intent.json")):
            manifest = self._read(path, "critical_intent")
            payload = verify_manifest(manifest, self.trust_store)
            intent = _intent_from_payload(payload)
            seen_commands.add(intent.command_id)
            commit = database_lookup(intent.command_id)
            manager = manager_receipts.get(intent.command_id)
            if commit is None:
                if manager is not None:
                    raise IntegrityError("manager claims receipts absent from database commit")
                records.append(RecoveryRecord(intent.command_id, RecoveryState.INTENT_ONLY, None))
                continue
            if (
                commit.intent_digest != manifest.body_digest
                or commit.receipt_ids != intent.receipt_ids
            ):
                raise IntegrityError("database critical commit differs from signed intent")
            if manager is not None and manager != commit.receipt_ids:
                raise IntegrityError("manager receipt identity differs from critical commit")
            marker_path = self.marker_path(intent.command_id)
            if marker_path.exists():
                marker = self._read(marker_path, "critical_commit")
                marker_payload = verify_manifest(marker, self.trust_store)
                if marker_payload != {"command_id": intent.command_id, **commit.to_dict()}:
                    raise IntegrityError("critical marker differs from database commit")
            else:
                self.record_commit(intent.command_id, commit, created_at=intent.created_at)
            records.append(RecoveryRecord(intent.command_id, RecoveryState.COMMITTED, commit))
        if set(manager_receipts) - seen_commands:
            raise IntegrityError("manager claims an issue without a signed critical intent")
        return tuple(records)

    def _read(self, path: Path, expected_kind: str) -> SignedManifest:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            manifest = SignedManifest.from_dict(value)
        except Exception as exc:
            raise IntegrityError("signed journal manifest cannot be decoded") from exc
        if manifest.kind != expected_kind:
            raise IntegrityError("signed journal manifest has the wrong kind")
        return manifest


class CriticalIssueCoordinator:
    """Execute the intent -> SQLite commit -> commit-marker issue protocol."""

    def __init__(
        self,
        journal: CriticalJournal,
        *,
        rehearsal: bool,
        topology: VerifiedRecoveryTopology | None,
    ) -> None:
        self.journal = journal
        self.rehearsal = rehearsal
        self.topology = topology

    @classmethod
    def for_rehearsal(cls, journal: CriticalJournal) -> CriticalIssueCoordinator:
        return cls(journal, rehearsal=True, topology=None)

    @classmethod
    def for_production(
        cls,
        journal: CriticalJournal,
        *,
        authority_database_path: Path | str,
        host_id: str,
        site_id: str,
    ) -> CriticalIssueCoordinator:
        topology = VerifiedRecoveryTopology.probe(
            authority_database_path,
            journal.root,
            host_id=host_id,
            site_id=site_id,
        )
        readiness = issue_recovery_readiness(topology.primary, topology.recovery, journal.signer)
        if not readiness.ready or not topology.distinct_physical_devices:
            raise IntegrityError("production issue recovery readiness is not satisfied")
        return cls(journal, rehearsal=False, topology=topology)

    def execute(
        self,
        intent: CriticalIssueIntent,
        *,
        database_commit: Callable[[str], CriticalDatabaseCommit],
        fault_hook: Callable[[str], None] | None = None,
    ) -> CriticalDatabaseCommit:
        signed_intent = self.journal.record_intent(intent)
        _fault(fault_hook, "after_intent")
        commit = database_commit(signed_intent.body_digest)
        if not isinstance(commit, CriticalDatabaseCommit):
            raise IntegrityError("database issue callback returned an invalid commit")
        if (
            commit.intent_digest != signed_intent.body_digest
            or commit.receipt_ids != intent.receipt_ids
        ):
            raise IntegrityError("database issue callback committed different issue material")
        _fault(fault_hook, "after_database_commit")
        self.journal.record_commit(intent.command_id, commit, created_at=intent.created_at)
        _fault(fault_hook, "after_marker")
        return commit


def _physical_device_id(path: Path) -> str:
    if os.name != "nt":
        return f"posix-device:{path.stat().st_dev}"
    anchor = path.anchor.rstrip("\\").casefold()
    if not re.fullmatch(r"[a-z]:", anchor):
        raise IntegrityError("Windows recovery path has no probeable local volume")
    import ctypes
    from ctypes import wintypes

    class StorageDeviceNumber(ctypes.Structure):
        _fields_ = (
            ("device_type", wintypes.DWORD),
            ("device_number", wintypes.DWORD),
            ("partition_number", wintypes.DWORD),
        )

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    device_io = kernel.DeviceIoControl
    device_io.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    volume = rf"\\.\{anchor.upper()}"
    handle = create_file(volume, 0, 0x1 | 0x2, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise IntegrityError(
            f"Windows physical-device probe could not open volume ({ctypes.get_last_error()})"
        )
    number = StorageDeviceNumber()
    returned = wintypes.DWORD()
    try:
        if not device_io(
            handle,
            0x002D1080,
            None,
            0,
            ctypes.byref(number),
            ctypes.sizeof(number),
            ctypes.byref(returned),
            None,
        ):
            raise IntegrityError(
                f"Windows physical-device probe failed ({ctypes.get_last_error()})"
            )
    finally:
        close(handle)
    return f"windows-device:{number.device_number}"


def _intent_from_payload(payload: Mapping[str, Any]) -> CriticalIssueIntent:
    _require_fields(
        payload,
        {
            "command_id",
            "command_digest",
            "approval_snapshot_digest",
            "expected_versions",
            "receipt_ids",
            "created_at",
        },
    )
    versions = payload["expected_versions"]
    receipts = payload["receipt_ids"]
    if not isinstance(versions, list) or not isinstance(receipts, list):
        raise IntegrityError("critical intent arrays are malformed")
    return CriticalIssueIntent(
        payload["command_id"],
        payload["command_digest"],
        payload["approval_snapshot_digest"],
        tuple(tuple(item) for item in versions),
        tuple(receipts),
        payload["created_at"],
    )


def _numbered_registry_paths(root: Path, label: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in root.iterdir():
        if path.name.startswith(".") or path.suffix == ".tmp":
            continue
        if not path.is_file() or re.fullmatch(r"[0-9]{16}\.json", path.name) is None:
            raise IntegrityError(f"{label} registry contains an unexplained record")
        paths.append(path)
    paths.sort(key=lambda item: item.name)
    for expected, path in enumerate(paths, start=1):
        if path.name != f"{expected:016d}.json":
            raise IntegrityError(f"{label} registry sequence has a gap or reorder")
    return tuple(paths)


def _read_signed_registry_record(path: Path, expected_kind: str) -> SignedManifest:
    try:
        encoded = _read_registry_record_bytes(path)
        value = json.loads(encoded.decode("utf-8"))
        if not isinstance(value, dict):
            raise IntegrityError("registry record must be a JSON object")
        manifest = SignedManifest.from_dict(value)
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError("signed registry record is damaged") from exc
    if manifest.kind != expected_kind:
        raise IntegrityError("signed registry record has the wrong kind")
    if canonical_bytes(manifest.to_dict()) != encoded:
        raise IntegrityError("signed registry record bytes are not canonical")
    return manifest


def _read_registry_record_bytes(path: Path) -> bytes:
    """Read one bounded immutable record, tolerating only transient OS read failures."""

    last_error: OSError | None = None
    for attempt in range(_REGISTRY_READ_ATTEMPTS):
        try:
            with path.open("rb") as handle:
                encoded = handle.read(_REGISTRY_RECORD_MAX_BYTES + 1)
        except OSError as exc:
            last_error = exc
            if attempt + 1 < _REGISTRY_READ_ATTEMPTS:
                time.sleep(0.005 * (attempt + 1))
            continue
        if len(encoded) > _REGISTRY_RECORD_MAX_BYTES:
            raise IntegrityError("signed registry record exceeds the bounded size limit")
        return encoded
    raise IntegrityError("signed registry record is temporarily unreadable") from last_error


def _trusted_checkpoint(manifest: SignedManifest, payload: Mapping[str, Any]) -> TrustedCheckpoint:
    _require_fields(
        payload,
        {
            "checkpoint_sequence",
            "previous_checkpoint_digest",
            "authority_anchor",
            "schema_digest",
            "projection_digest",
            "aggregate_heads_digest",
        },
    )
    sequence = payload["checkpoint_sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise IntegrityError("checkpoint sequence must be positive")
    _require_digest(payload["previous_checkpoint_digest"], "previous checkpoint digest")
    anchor = payload["authority_anchor"]
    if not isinstance(anchor, Mapping):
        raise IntegrityError("checkpoint authority anchor is malformed")
    _require_fields(anchor, {"global_sequence", "event_digest"})
    authority_sequence = anchor["global_sequence"]
    if (
        isinstance(authority_sequence, bool)
        or not isinstance(authority_sequence, int)
        or authority_sequence < 0
    ):
        raise IntegrityError("checkpoint authority sequence must be non-negative")
    authority_digest = _require_digest(anchor["event_digest"], "checkpoint authority digest")
    schema_digest = _require_digest(payload["schema_digest"], "checkpoint schema digest")
    projection_digest = _require_digest(
        payload["projection_digest"], "checkpoint projection digest"
    )
    aggregate_heads_digest = _require_digest(
        payload["aggregate_heads_digest"], "checkpoint aggregate-head digest"
    )
    return TrustedCheckpoint(
        manifest,
        sequence,
        authority_sequence,
        authority_digest,
        schema_digest,
        projection_digest,
        aggregate_heads_digest,
    )


def _database_checkpoint_facts(
    database_path: Path,
    *,
    required_ancestor: tuple[int, str] | None = None,
) -> dict[str, Any]:
    from strathmark.v3.infrastructure.sqlite.connection import open_v3_connection
    from strathmark.v3.infrastructure.sqlite.event_store import (
        ZERO_DIGEST,
        AuthorityAnchor,
        verify_read_only_authority,
    )
    from strathmark.v3.infrastructure.sqlite.migrations import (
        EXPECTED_SCHEMA_DIGEST,
        canonical_schema_digest,
    )
    from strathmark.v3.infrastructure.sqlite.projections import SQLiteProjectionStore

    with open_v3_connection(database_path, read_only=True) as connection:
        schema_digest = canonical_schema_digest(connection)
        if schema_digest != EXPECTED_SCHEMA_DIGEST:
            raise IntegrityError("checkpoint database schema differs from this runtime")
        projection_digest = SQLiteProjectionStore.projection_digest(connection)
        tip = connection.execute(
            "SELECT global_sequence, event_digest FROM v3_events "
            "ORDER BY global_sequence DESC LIMIT 1"
        ).fetchone()
        heads = [
            {
                "aggregate_kind": str(row[0]),
                "aggregate_id": str(row[1]),
                "aggregate_version": int(row[2]),
                "event_digest": str(row[3]),
            }
            for row in connection.execute(
                "SELECT aggregate_kind, aggregate_id, aggregate_version, event_digest "
                "FROM v3_aggregate_heads ORDER BY aggregate_kind, aggregate_id"
            )
        ]
    anchor = (
        AuthorityAnchor(0, ZERO_DIGEST)
        if tip is None
        else AuthorityAnchor(int(tip[0]), str(tip[1]))
    )
    if required_ancestor is not None:
        ancestor_sequence, ancestor_digest = required_ancestor
        if anchor.global_sequence < ancestor_sequence:
            raise IntegrityError("checkpoint authority sequence rolls back its predecessor")
        try:
            observed_tip = verify_read_only_authority(
                database_path,
                trusted_anchor=AuthorityAnchor(ancestor_sequence, ancestor_digest),
            )
        except Exception as exc:
            raise IntegrityError(
                "checkpoint authority does not descend from its predecessor"
            ) from exc
        if observed_tip != anchor:
            raise IntegrityError(
                "checkpoint ancestry verification returned a different authority tip"
            )
    verify_read_only_authority(database_path, trusted_anchor=anchor)
    return {
        "authority_anchor": {
            "global_sequence": anchor.global_sequence,
            "event_digest": anchor.event_digest,
        },
        "schema_digest": schema_digest,
        "projection_digest": projection_digest,
        "aggregate_heads_digest": canonical_digest(
            {
                "schema_version": "strathmark-v3-aggregate-heads-v1",
                "heads": heads,
            }
        ),
    }


def _publish_no_clobber(path: Path, payload: bytes) -> bool:
    """Durably publish one final name without ever replacing a concurrent winner."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            return _windows_write_through_no_clobber(temporary, path)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _windows_write_through_no_clobber(source: Path, destination: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    if move_file(str(source), str(destination), 0x8):
        return True
    error = ctypes.get_last_error()
    if error in {80, 183}:
        return False
    raise IntegrityError(f"Windows write-through journal rename failed ({error})")


def _verify_signature(identity: IntegrityKeyIdentity, payload: bytes, signature: bytes) -> None:
    _serialization, ec, hashes, invalid_signature = _crypto()
    public = _load_public_key(base64.b64decode(identity.public_key_der_b64, validate=True))
    try:
        public.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
    except invalid_signature as exc:
        raise IntegrityError("ECDSA P-256 manifest signature is invalid") from exc


def _load_public_key(encoded: bytes) -> Any:
    serialization, ec, _hashes, _invalid = _crypto()
    try:
        public = serialization.load_der_public_key(encoded)
    except (TypeError, ValueError) as exc:
        raise IntegrityError("integrity public key DER is invalid") from exc
    if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(
        public.curve, ec.SECP256R1
    ):
        raise IntegrityError("integrity public key must use ECDSA P-256")
    return public


def _crypto() -> tuple[Any, Any, Any, type[Exception]]:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as exc:
        raise IntegrityError("ECDSA P-256 support requires the cryptography runtime extra") from exc
    return serialization, ec, hashes, InvalidSignature


def _require_fields(value: Mapping[str, Any], expected: set[str]) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise IntegrityError("integrity object has unknown or missing fields")


def _require_token(value: object, label: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise IntegrityError(f"{label} must be a bounded opaque token")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise IntegrityError(f"{label} must be a lower-case SHA-256 digest")
    return value


def _fault(hook: Callable[[str], None] | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


__all__ = [
    "CheckpointRegistry",
    "CriticalDatabaseCommit",
    "CriticalIssueCoordinator",
    "CriticalIssueIntent",
    "CriticalJournal",
    "IntegrityError",
    "IntegrityKeyClass",
    "IntegrityKeyIdentity",
    "IntegrityTrustStore",
    "IssueRecoveryReadiness",
    "P256EphemeralSigner",
    "P256ExternalSigner",
    "P256WindowsCNGSigner",
    "RecoveryRecord",
    "RecoveryState",
    "SIGNATURE_ALGORITHM",
    "SignedManifest",
    "StorageIdentity",
    "TrustedCheckpoint",
    "VerifiedRecoveryTopology",
    "apply_key_rotation",
    "issue_recovery_readiness",
    "require_production_cng_signer",
    "sign_key_rotation",
    "sign_manifest",
    "verify_manifest",
]
