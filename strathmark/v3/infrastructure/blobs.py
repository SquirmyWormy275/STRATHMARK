"""Immutable content-addressed blob storage for bounded V3 evidence.

Blob construction completes outside SQLite.  A caller may reference the returned
descriptor only after :meth:`ContentAddressedBlobStore.publish` returns: content and
metadata have then been flushed, atomically renamed, and the containing directory has
been flushed.  A crash can therefore leave only a disposable temporary file or a complete
unreferenced orphan, never a reference to partially published bytes.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from strathmark.v3.contracts.canonical import canonical_bytes, canonical_digest
from strathmark.v3.contracts.commands import (
    MAX_BLOB_BYTES,
    BlobReferenceV2,
    BlobRetentionClass,
)
from strathmark.v3.contracts.identifiers import deterministic_identifier

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = re.compile(r"^strathmark-v3-[a-z0-9][a-z0-9-]{0,94}-v[1-9][0-9]*$")
_MEDIA_TYPES = frozenset({"application/json", "application/octet-stream"})
BLOB_DURABILITY_STAGES = (
    (
        "after_temp_write",
        "after_file_fsync",
        "after_durable_rename",
        "after_descriptor_write",
        "after_descriptor_fsync",
        "after_descriptor_durable_rename",
    )
    if os.name == "nt"
    else (
        "after_temp_write",
        "after_file_fsync",
        "after_rename",
        "after_directory_fsync",
        "after_descriptor_write",
        "after_descriptor_fsync",
        "after_descriptor_rename",
        "after_descriptor_directory_fsync",
    )
)


class BlobStoreError(RuntimeError):
    """Base failure for immutable blob publication."""


class BlobIntegrityError(BlobStoreError):
    """A required content or descriptor cannot be verified exactly."""


class InjectedBlobFailure(BlobStoreError):
    """Fault-injection signal used to prove each durability prefix."""


BlobRetention = BlobRetentionClass


@dataclass(frozen=True, slots=True)
class BlobMetadata:
    media_type: str
    payload_schema_version: str
    retention: BlobRetentionClass

    def __post_init__(self) -> None:
        if self.media_type not in _MEDIA_TYPES:
            raise BlobStoreError("unsupported blob media type")
        if (
            not isinstance(self.payload_schema_version, str)
            or _SCHEMA.fullmatch(self.payload_schema_version) is None
        ):
            raise BlobStoreError("invalid blob payload schema version")
        if not isinstance(self.retention, BlobRetentionClass):
            raise BlobStoreError("blob retention must be a BlobRetention value")

    def to_dict(self) -> dict[str, str]:
        return {
            "media_type": self.media_type,
            "payload_schema_version": self.payload_schema_version,
            "retention": self.retention.value,
        }

    @classmethod
    def from_reference(cls, reference: BlobReferenceV2) -> BlobMetadata:
        if not isinstance(reference, BlobReferenceV2):
            raise BlobStoreError("metadata requires a BlobReferenceV2")
        return cls(
            reference.media_type,
            reference.payload_schema_version,
            reference.retention_class,
        )


# Compatibility name for early U6 callers.  The authoritative object is the contract
# embedded byte-for-byte in commands, events, receipts, backups, and archive proofs.
StoredBlobReference = BlobReferenceV2


FaultHook = Callable[[str], None]


class ContentAddressedBlobStore:
    """One immutable SHA-256 namespace rooted outside SQLite."""

    def __init__(self, root: Path | str, *, create: bool = True) -> None:
        if isinstance(root, bool) or not isinstance(root, (Path, str)):
            raise BlobStoreError("blob root must be a filesystem path")
        if not isinstance(create, bool):
            raise BlobStoreError("blob create mode must be boolean")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.read_only = not create
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.root.is_dir():
            raise BlobIntegrityError("existing blob root is missing")

    def publish(
        self,
        payload: bytes,
        *,
        metadata: BlobMetadata,
        fault_hook: FaultHook | None = None,
    ) -> StoredBlobReference:
        if not isinstance(payload, bytes):
            raise BlobStoreError("blob payload must be immutable bytes")
        if self.read_only:
            raise BlobStoreError("read-only blob store cannot publish")
        if len(payload) > MAX_BLOB_BYTES:
            raise BlobStoreError("blob payload exceeds the declared limit")
        if not isinstance(metadata, BlobMetadata):
            raise BlobStoreError("blob metadata is required")
        if fault_hook is not None and not callable(fault_hook):
            raise BlobStoreError("blob fault hook must be callable")

        digest = hashlib.sha256(payload).hexdigest()
        reference = BlobReferenceV2(
            blob_id=deterministic_identifier("blob", {"digest": digest}),
            digest=digest,
            byte_count=len(payload),
            media_type=metadata.media_type,
            payload_schema_version=metadata.payload_schema_version,
            retention_class=metadata.retention,
        )
        directory = self._directory(digest)
        directory.mkdir(parents=True, exist_ok=True)
        content_path = self.path_for(digest)
        metadata_path = self._metadata_path(reference)
        content_temp = directory / f".{digest}.{uuid.uuid4().hex}.tmp"
        metadata_temp = directory / f".{digest}.{uuid.uuid4().hex}.meta.tmp"
        try:
            if content_path.exists() and metadata_path.exists():
                return self.verify(reference)
            if not content_path.exists():
                self._write_fsynced(
                    content_temp,
                    payload,
                    fault_hook=fault_hook,
                    write_stage="after_temp_write",
                    fsync_stage="after_file_fsync",
                )
                _atomic_replace(content_temp, content_path)
                _finish_rename_durability(
                    directory,
                    fault_hook=fault_hook,
                    rename_stage="after_rename",
                    directory_stage="after_directory_fsync",
                    windows_stage="after_durable_rename",
                )
            elif _file_digest(content_path) != digest:
                raise BlobIntegrityError("existing content-addressed blob is corrupt")

            descriptor = canonical_bytes(reference.to_dict())
            self._write_fsynced(
                metadata_temp,
                descriptor,
                fault_hook=fault_hook,
                write_stage="after_descriptor_write",
                fsync_stage="after_descriptor_fsync",
            )
            _atomic_replace(metadata_temp, metadata_path)
            _finish_rename_durability(
                directory,
                fault_hook=fault_hook,
                rename_stage="after_descriptor_rename",
                directory_stage="after_descriptor_directory_fsync",
                windows_stage="after_descriptor_durable_rename",
            )
            return self.verify(reference)
        finally:
            for temporary in (content_temp, metadata_temp):
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def verify(self, reference: StoredBlobReference) -> StoredBlobReference:
        if not isinstance(reference, StoredBlobReference):
            raise BlobStoreError("verify requires a StoredBlobReference")
        content_path = self.path_for(reference.digest)
        metadata_path = self._metadata_path(reference)
        if not content_path.is_file() or not metadata_path.is_file():
            raise BlobIntegrityError("required blob or metadata is missing")
        stat = content_path.stat()
        if stat.st_size != reference.byte_count:
            raise BlobIntegrityError("required blob length differs from its reference")
        if _file_digest(content_path) != reference.digest:
            raise BlobIntegrityError("required blob digest differs from its reference")
        expected = canonical_bytes(reference.to_dict())
        try:
            observed = metadata_path.read_bytes()
        except OSError as exc:
            raise BlobIntegrityError("required blob metadata cannot be read") from exc
        if observed != expected:
            raise BlobIntegrityError("required blob metadata differs from its reference")
        return reference

    def verify_required(
        self, references: tuple[StoredBlobReference, ...]
    ) -> tuple[StoredBlobReference, ...]:
        if not isinstance(references, tuple):
            raise BlobStoreError("required blob references must be an immutable tuple")
        return tuple(self.verify(reference) for reference in references)

    def read(self, reference: StoredBlobReference) -> bytes:
        self.verify(reference)
        return self.path_for(reference.digest).read_bytes()

    def path_for(self, digest: str) -> Path:
        self._require_digest(digest)
        return self._directory(digest) / f"{digest}.blob"

    def complete_paths(self) -> tuple[Path, ...]:
        return tuple(sorted(self.root.glob("[0-9a-f][0-9a-f]/*.blob")))

    def _directory(self, digest: str) -> Path:
        self._require_digest(digest)
        return self.root / digest[:2]

    def _metadata_path(self, reference: StoredBlobReference) -> Path:
        descriptor_digest = canonical_digest(BlobMetadata.from_reference(reference).to_dict())
        return self._directory(reference.digest) / f"{reference.digest}.{descriptor_digest}.json"

    @staticmethod
    def _require_digest(digest: str) -> None:
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise BlobStoreError("blob digest must be lower-case SHA-256")

    @staticmethod
    def _write_fsynced(
        path: Path,
        payload: bytes,
        *,
        fault_hook: FaultHook | None,
        write_stage: str,
        fsync_stage: str,
    ) -> None:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            _fault(fault_hook, write_stage)
            os.fsync(handle.fileno())
            _fault(fault_hook, fsync_stage)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1_048_576):
                digest.update(chunk)
    except OSError as exc:
        raise BlobIntegrityError("required blob cannot be read") from exc
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(source: Path, destination: Path) -> None:
    if os.name != "nt":
        os.replace(source, destination)
        return
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    # MOVEFILE_WRITE_THROUGH makes the rename's directory metadata durable before
    # returning.  It is the Windows equivalent of rename followed by directory fsync.
    if not move_file(str(source), str(destination), 0x1 | 0x8):
        raise BlobStoreError(
            f"Windows write-through blob rename failed ({ctypes.get_last_error()})"
        )


def _finish_rename_durability(
    directory: Path,
    *,
    fault_hook: FaultHook | None,
    rename_stage: str,
    directory_stage: str,
    windows_stage: str,
) -> None:
    if os.name == "nt":
        # MoveFileExW(MOVEFILE_WRITE_THROUGH) above combines atomic replacement and
        # metadata durability; Windows exposes no separately flushable directory boundary.
        _fault(fault_hook, windows_stage)
        return
    _fault(fault_hook, rename_stage)
    _fsync_directory(directory)
    _fault(fault_hook, directory_stage)


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


__all__ = [
    "BlobIntegrityError",
    "BlobMetadata",
    "BlobRetention",
    "BlobStoreError",
    "BLOB_DURABILITY_STAGES",
    "ContentAddressedBlobStore",
    "InjectedBlobFailure",
    "MAX_BLOB_BYTES",
    "StoredBlobReference",
]
